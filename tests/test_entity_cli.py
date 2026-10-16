"""The real urllib fetcher and the command line, against a local HTTP server serving the fixture site."""
import csv
import gzip
import http.server
import json
import os
import tempfile
import threading
import unittest

from airrp_entity.cli import main
from airrp_entity.fetch import Fetcher

from .entity_helpers import SITES

SJIS_PAGE = ("<html><head><meta charset='Shift_JIS'><title>会社概要</title></head><body>"
             "<dl><dt>商号</dt><dd>さくら電機株式会社</dd></dl></body></html>")


class _Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=os.path.join(SITES, "northwind"), **kw)

    def log_message(self, *a):
        pass

    def _send(self, body, ctype, extra=()):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        for k, v in extra:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/sjis/":
            return self._send(SJIS_PAGE.encode("cp932"), "text/html")              # charset only in the <meta>
        if self.path == "/gz/":
            return self._send(gzip.compress(b"<html><body><p>compressed page</p></body></html>"),
                              "text/html; charset=utf-8", [("Content-Encoding", "gzip")])
        if self.path == "/old-legal/":
            self.send_response(301)
            self.send_header("Location", "/legal/")
            self.end_headers()
            return None
        return super().do_GET()


class LocalServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.tmp = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_fetcher_decodes_and_follows_the_rules(self):
        f = Fetcher(delay=0, timeout=5)
        self.assertIn("さくら電機株式会社", f.get(self.base + "/sjis/").text())
        self.assertIn("compressed page", f.get(self.base + "/gz/").text())
        r = f.get(self.base + "/old-legal/")
        self.assertEqual((r.status, r.url), (200, self.base + "/legal/"))
        r = f.get(self.base + "/private/secret/")
        self.assertEqual((r.ok, r.error), (False, "disallowed by robots.txt"))
        self.assertEqual(f.get(self.base + "/missing/").error, "HTTP 404")

    def test_profile_command_writes_json_markdown_and_csv(self):
        out, md, row, subs = (os.path.join(self.tmp, n) for n in ("p.json", "p.md", "p.csv", "s.csv"))
        code = main(["profile", self.base + "/", "--no-registry", "--delay", "0", "--out", out, "--markdown", md,
                     "--csv", row, "--subsidiaries-csv", subs, "--quiet"])
        self.assertEqual(code, 0)
        with open(out, encoding="utf-8") as fh:
            p = json.load(fh)
        self.assertEqual(p["schema"], "airrp-entity-profile/1")
        self.assertEqual(p["summary"]["registered_legal_name"], "Northwind Example Holdings plc")
        self.assertIn({"url": self.base + "/private/secret/", "reason": "disallowed by robots.txt"}, p["skipped"])
        with open(md, encoding="utf-8") as fh:
            self.assertIn("# Entity profile: Northwind Example Holdings plc", fh.read())
        with open(row, encoding="utf-8") as fh:
            self.assertEqual(next(csv.DictReader(fh))["registered_legal_name"], "Northwind Example Holdings plc")
        with open(subs, encoding="utf-8") as fh:            # no registry: no subsidiaries, just the header
            self.assertEqual([r for r in csv.reader(fh)][0][:3], ["lei", "name", "relation"])

    def test_batch_command(self):
        listing = os.path.join(self.tmp, "orgs.csv")
        with open(listing, "w", encoding="utf-8") as fh:
            fh.write(f"name,url\nNorthwind,{self.base}/\nNowhere,http://127.0.0.1:9/\n")
        out_dir = os.path.join(self.tmp, "batch")
        code = main(["batch", listing, "--out-dir", out_dir, "--no-registry", "--delay", "0", "--timeout", "2", "--quiet"])
        self.assertEqual(code, 1)                                  # one organisation could not be fetched
        with open(os.path.join(out_dir, "summary.csv"), encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual([r["registered_legal_name"] for r in rows], ["Northwind Example Holdings plc", ""])
        self.assertTrue(os.path.exists(os.path.join(out_dir, "northwind.json")))


if __name__ == "__main__":
    unittest.main()
