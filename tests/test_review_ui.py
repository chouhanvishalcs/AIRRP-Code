import http.client
import json
import threading
import unittest

from airrp_ingest.masters import create_master
from airrp_ingest.oscal import load_catalog
from airrp_ingest.pipeline import apply_plan, build_plan
from airrp_ingest.review_ui import make_server
from airrp_ingest.similarity import suggest

from .helpers import FIXTURE, for_each_engine
from .test_store_masters import MASTER


class ReviewUiTests:
    @classmethod
    def setUpClass(cls):
        cls.repo, _ = cls.engine.new()
        cls.httpd = make_server(cls.repo, "NIST-SP-800-53", "5.2.0", port=0, token="tok")
        apply_plan(build_plan(load_catalog(FIXTURE), cls.repo), cls.repo, "tester")
        mid = create_master(cls.repo, "alice", **MASTER)
        cls.repo.approve_master_control(mid, "bob")
        suggest(cls.repo, "NIST-SP-800-53", "5.2.0", min_score=0.1)
        cls.mid = mid
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def call(self, method, path, body=None, token="tok", host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            headers["X-Token"] = token
        data = None
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body), "application/json"
        conn.request(method, path, data, headers)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, (json.loads(raw) if resp.getheader("Content-Type", "").startswith("application/json") else raw)

    def test_requires_token_and_local_host(self):
        self.assertEqual(self.call("GET", "/api/summary", token=None)[0], 403)
        self.assertEqual(self.call("GET", "/api/summary", token="wrong")[0], 403)
        self.assertEqual(self.call("GET", "/api/summary", host="evil.example.com")[0], 403)
        self.assertEqual(self.call("GET", "/api/summary")[0], 200)

    def test_page_is_served_with_token_in_query(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/?t=tok", headers={"Host": f"127.0.0.1:{self.port}"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertIn(b"AIRRP Review Console", resp.read())

    def test_queue_shows_requirement_text_and_candidate(self):
        status, rows = self.call("GET", "/api/queue?limit=5")
        self.assertEqual(status, 200)
        top = rows[0]
        self.assertTrue(top["control_id"].startswith("AC-2"))
        self.assertIn("accounts", top["statement"].lower())
        self.assertEqual(top["master_id"], self.mid)

    def test_decision_needs_reviewer_and_rationale_and_is_final(self):
        _, rows = self.call("GET", "/api/queue?limit=5&q=AC-2(1)")
        mapping_id = rows[0]["id"]
        body = dict(mapping_id=mapping_id, decision="approved", relationship="equivalent", rationale="ok", primary=False)
        self.assertEqual(self.call("POST", "/api/decide", body)[0], 400)  # no reviewer
        self.assertEqual(self.call("POST", "/api/decide", {**body, "reviewer": "carol", "rationale": " "})[0], 400)
        self.assertEqual(self.call("POST", "/api/decide", {**body, "reviewer": "carol"})[0], 200)
        self.assertEqual(self.call("POST", "/api/decide", {**body, "reviewer": "carol"})[0], 400)  # already decided
        row = self.repo.fetchone("SELECT status, reviewer FROM mapping WHERE id=?", (mapping_id,))
        self.assertEqual((row["status"], row["reviewer"]), ("approved", "carol"))

    def test_manual_mapping_for_requirement_the_suggester_missed(self):
        _, before = self.call("GET", "/api/unmapped?q=SA-15")
        self.assertTrue(any(r["control_id"] == "SA-15" for r in before))
        body = dict(control_id="SA-15", master_id=self.mid, reviewer="carol", relationship="intersects",
                    rationale="Developer account controls overlap", primary=False)
        self.assertEqual(self.call("POST", "/api/map", body)[0], 200)
        _, after = self.call("GET", "/api/unmapped?q=SA-15")
        self.assertFalse(any(r["control_id"] == "SA-15" for r in after))
        self.assertEqual(self.call("POST", "/api/map", body)[0], 400)  # same pair twice

    def test_master_creation_duplicate_guard_returns_409_and_approval_is_four_eyes(self):
        near = {**MASTER, "name": "Manage Users and System Account Lifecycle", "reviewer": "carol"}
        status, body = self.call("POST", "/api/master", near)
        self.assertEqual(status, 409)
        self.assertEqual(body["candidates"][0]["id"], self.mid)
        status, body = self.call("POST", "/api/master", {**near, "distinct_reason": "service accounts only"})
        self.assertEqual(status, 200)
        self.assertEqual(self.call("POST", "/api/master/approve", {"id": body["id"], "reviewer": "carol"})[0], 400)
        self.assertEqual(self.call("POST", "/api/master/approve", {"id": body["id"], "reviewer": "dave"})[0], 200)

    def test_rejects_non_json_and_unknown_routes(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("POST", "/api/decide", "x=1", {"Host": f"127.0.0.1:{self.port}", "X-Token": "tok",
                                                    "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(conn.getresponse().status, 400)
        self.assertEqual(self.call("GET", "/nope")[0], 404)


for_each_engine(ReviewUiTests)


if __name__ == "__main__":
    unittest.main()
