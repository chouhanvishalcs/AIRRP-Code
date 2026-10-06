"""The source-correction layer: published text stays verbatim, a person's correction sits over it.

Every correction text below is SYNTHETIC test wording. It is not NIST text and nothing here claims to be the real
SA-15(13) statement.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest

from airrp_ingest import cli
from airrp_ingest.bundle import build_bundle, bundle_hash
from airrp_ingest.curation import effective_control, field_hash, load_curation
from airrp_ingest.masters import create_master
from airrp_ingest.model import Correction
from airrp_ingest.normalize import content_hash, obligation_hash
from airrp_ingest.oscal import load_catalog
from airrp_ingest.pipeline import PlanBlocked, apply_plan, build_plan, control_from_payload, verify_repository
from airrp_ingest.reference_consumer import apply_bundle
from airrp_ingest.similarity import suggest
from airrp_ingest.validate import load_manifest

from .helpers import FIXTURE, MANIFEST, for_each_engine
from .test_pipeline import DUP_TEXT, curation_file
from .test_store_masters import MASTER

CODE, VERSION = "NIST-SP-800-53", "5.2.0"
SYNTHETIC = "SYNTHETIC TEST WORDING: Require the developer to use [secure logging format(s)] to log [events types to log] at [level of detail to log]."


def statement_entry(**kw):
    return {"framework_version": VERSION, "control_id": "SA-15(13)", "field": "statement", "value": SYNTHETIC,
            "source_value_sha256": field_hash(DUP_TEXT), "problem": "the published text repeats SA-15(12) word for word",
            "citation": "SYNTHETIC citation for tests", "reviewer": "a.reviewer", "reviewed_at": "2026-10-06", **kw}


def title_entry(control_id="AC-1", value="Policy and Procedures (synthetic title)", **kw):
    parsed = {c.control_id: c for c in load_catalog(FIXTURE).controls}
    return {"framework_version": VERSION, "control_id": control_id, "field": "title", "value": value,
            "source_value_sha256": field_hash(parsed[control_id].title), "problem": "synthetic title defect",
            "citation": "SYNTHETIC citation for tests", "reviewer": "a.reviewer", "reviewed_at": "2026-10-06", **kw}


class CorrectionTests:
    def setUp(self):
        self.repo, self.spec = self.engine.new()
        self.manifest = load_manifest(MANIFEST)
        self.parsed = load_catalog(FIXTURE)

    def plan(self, *entries, waivers=()):
        cur = load_curation(curation_file(corrections=list(entries), waivers=list(waivers))) if entries or waivers else None
        return build_plan(self.parsed, self.repo, self.manifest, cur)

    def apply(self, *entries, **kw):
        plan = self.plan(*entries, **kw)
        apply_plan(plan, self.repo, "tester")
        return plan

    def row(self, control_id, table="requirement_effective"):
        return self.repo.fetchone(f"SELECT * FROM {table} WHERE control_id=?", (control_id,))

    # -- the mirror stays verbatim --------------------------------------------------------------------
    def test_the_published_text_is_stored_untouched_and_the_correction_sits_beside_it(self):
        self.apply(statement_entry())
        mirror = self.row("SA-15(13)", "source_requirement")
        self.assertEqual(mirror["statement"], DUP_TEXT)
        self.assertEqual(control_from_payload(mirror["payload_json"]).statement, DUP_TEXT)
        self.assertEqual(control_from_payload(mirror["payload_json"]).overrides_applied, ())
        eff = self.row("SA-15(13)")
        self.assertEqual((eff["statement"], eff["corrected"]), (SYNTHETIC, 1))
        self.assertEqual(eff["source_content_hash"], mirror["content_hash"])
        self.assertEqual(self.row("AC-1")["corrected"], 0)

    def test_a_corrected_control_cannot_be_stored_as_source_text(self):
        c = self.parsed.controls[0]
        corrected = effective_control(c, [Correction(CODE, VERSION, c.control_id, "title", "Another title",
                                                     field_hash(c.title), "p", "c", "r", "d")])
        self.assertEqual(corrected.overrides_applied, (("title", "c"),))
        with self.assertRaises(ValueError):
            self.repo.add_requirement(corrected, 1)

    def test_a_correction_releases_both_controls_of_the_duplicate_pair(self):
        plan = self.plan(statement_entry())
        self.assertEqual(plan.quarantined, {})
        self.assertEqual(len(plan.to_add), 7)
        self.assertEqual((plan.corrected, len(plan.corrections)), (1, 1))

    def test_what_is_recorded_says_who_why_and_from_what(self):
        run = self.apply(statement_entry())
        h = self.repo.correction_history(CODE, VERSION)
        self.assertEqual(len(h), 1)
        self.assertEqual((h[0]["control_id"], h[0]["field"], h[0]["revision"], h[0]["action"], h[0]["reviewer"]),
                         ("SA-15(13)", "statement", 1, "set", "a.reviewer"))
        self.assertEqual(h[0]["source_value_sha256"], field_hash(DUP_TEXT))
        audit = self.repo.fetchone("SELECT actor, detail_json FROM audit_log WHERE action='correction.set'")
        self.assertEqual(audit["actor"], "tester")
        self.assertEqual(json.loads(audit["detail_json"])["reviewer"], "a.reviewer")
        self.assertEqual(run.corrected, 1)

    # -- hashes and what leaves the importer ---------------------------------------------------------
    def test_effective_hashes_are_recomputed_for_the_corrected_control_only(self):
        self.apply(statement_entry())
        published = {c.control_id: c for c in self.parsed.controls}
        bundle = build_bundle(self.repo, CODE, VERSION)
        by_id = {r["control_id"]: r for r in bundle["requirements"]}
        eff = effective_control(published["SA-15(13)"], self.repo.current_corrections(CODE, VERSION)["SA-15(13)"])
        self.assertEqual(by_id["SA-15(13)"]["content_hash"], content_hash(eff))
        self.assertNotEqual(by_id["SA-15(13)"]["content_hash"], content_hash(published["SA-15(13)"]))
        self.assertNotEqual(obligation_hash(eff), obligation_hash(published["SA-15(13)"]))
        self.assertEqual(by_id["SA-15(13)"]["legalText"], SYNTHETIC)
        for cid, r in by_id.items():
            if cid != "SA-15(13)":
                self.assertEqual(r["content_hash"], content_hash(published[cid]), cid)

    def test_bundle_lists_corrections_only_when_there_are_some(self):
        plain, _ = self.engine.new()
        apply_plan(build_plan(self.parsed, plain, self.manifest), plain, "tester")
        self.assertNotIn("corrections", build_bundle(plain, CODE, VERSION))
        self.apply(statement_entry())
        b = build_bundle(self.repo, CODE, VERSION)
        self.assertEqual(len(b["corrections"]), 1)
        c = b["corrections"][0]
        self.assertEqual((c["control_id"], c["field"], c["published_value"], c["value"], c["reviewer"]),
                         ("SA-15(13)", "statement", DUP_TEXT, SYNTHETIC, "a.reviewer"))
        self.assertEqual(bundle_hash(b), b["bundle_sha256"])

    def test_a_consumer_that_ignores_corrections_still_gets_the_corrected_text(self):
        self.apply(statement_entry())
        consumer, _ = self.engine.new()
        apply_bundle(consumer, build_bundle(self.repo, CODE, VERSION))
        row = consumer.fetchone("SELECT legal_text, content_hash FROM app_requirement WHERE control_id='SA-15(13)'")
        self.assertEqual(row["legal_text"], SYNTHETIC)

    def test_suggestions_are_scored_on_the_corrected_text(self):
        self.apply(statement_entry())
        mid = create_master(self.repo, "alice", **{**MASTER, "name": "Secure logging format", "objective": "Logs use a secure logging format.",
                                                   "description": "Events are logged in a secure logging format at a defined level of detail."})
        self.repo.approve_master_control(mid, "bob")
        suggest(self.repo, CODE, VERSION, min_score=0.1)
        rows = self.repo.fetchall("SELECT r.control_id FROM mapping m JOIN source_requirement r ON r.id=m.requirement_id"
                                  " WHERE m.master_control_id=?", (mid,))
        self.assertIn("SA-15(13)", [r[0] for r in rows])

    # -- idempotence, persistence, revisions -----------------------------------------------------------
    def test_applying_the_same_file_again_records_nothing_new(self):
        self.apply(statement_entry())
        again = self.plan(statement_entry())
        self.assertEqual((again.corrections, [n["state"] for n in again.correction_notes]), ([], ["unchanged"]))
        apply_plan(again, self.repo, "tester")
        self.assertEqual(self.repo.fetchone("SELECT COUNT(*) FROM source_correction")[0], 1)

    def test_a_recorded_correction_keeps_applying_without_the_curation_file(self):
        self.apply(statement_entry())
        later = self.plan()
        self.assertEqual((later.quarantined, later.corrected, later.corrections, len(later.unchanged)), ({}, 1, [], 7))

    def test_a_changed_entry_is_a_new_revision_and_the_old_one_stays_on_record(self):
        self.apply(statement_entry())
        second = SYNTHETIC + " (revised)"
        plan = self.apply(statement_entry(value=second, citation="SYNTHETIC citation 2"))
        self.assertEqual([n["state"] for n in plan.correction_notes], ["new_revision"])
        h = self.repo.correction_history(CODE, VERSION)
        self.assertEqual([(r["revision"], r["action"], r["value"]) for r in h], [(1, "set", SYNTHETIC), (2, "set", second)])
        self.assertEqual(self.row("SA-15(13)")["statement"], second)

    def test_retiring_a_correction_brings_the_published_text_back_and_keeps_the_history(self):
        self.apply(title_entry())
        self.assertEqual(self.row("AC-1")["title"], "Policy and Procedures (synthetic title)")
        retired = title_entry(retired={"reviewer": "b.reviewer", "reviewed_at": "2026-10-07", "reason": "the published title was right"})
        plan = self.apply(retired)
        self.assertEqual([n["state"] for n in plan.correction_notes], ["retire"])
        self.assertEqual((self.row("AC-1")["title"], self.row("AC-1")["corrected"]), (self.parsed.controls[0].title, 0))
        h = self.repo.correction_history(CODE, VERSION)
        self.assertEqual([(r["revision"], r["action"], r["reviewer"]) for r in h], [(1, "set", "a.reviewer"), (2, "retire", "b.reviewer")])
        self.assertEqual(self.repo.fetchone("SELECT COUNT(*) FROM correction_current")[0], 0)
        self.assertEqual(self.plan().corrected, 0)  # stays retired without the file, too

    def test_a_retired_correction_can_be_reinstated_as_a_new_revision(self):
        self.apply(title_entry())
        self.apply(title_entry(retired={"reviewer": "b", "reviewed_at": "2026-10-07", "reason": "x"}))
        plan = self.apply(title_entry())
        self.assertEqual([n["state"] for n in plan.correction_notes], ["new_revision"])
        self.assertEqual([r["revision"] for r in self.repo.correction_history(CODE, VERSION)], [1, 2, 3])
        self.assertEqual(self.row("AC-1")["corrected"], 1)

    def test_retiring_a_correction_that_was_never_recorded_is_a_no_op(self):
        plan = self.apply(title_entry(retired={"reviewer": "b", "reviewed_at": "d", "reason": "r"}))
        self.assertEqual((plan.corrections, [n["state"] for n in plan.correction_notes]), ([], ["retired_already"]))

    def test_retiring_the_only_fix_for_a_defect_is_held_back_until_someone_waives_it(self):
        self.apply(statement_entry())
        retired = statement_entry(retired={"reviewer": "b", "reviewed_at": "2026-10-07", "reason": "r"})
        plan = self.plan(retired)
        self.assertEqual(sorted(plan.quarantined), ["SA-15(12)", "SA-15(13)"])  # the defect is back
        self.assertEqual(plan.corrections, [])
        self.assertIn("held back", plan.correction_notes[0]["detail"])
        waivers = [dict(framework_version=VERSION, control_id=cid, code="DUPLICATE_STATEMENT", reason="accepted for the test",
                        reviewer="b", reviewed_at="2026-10-07") for cid in ("SA-15(12)", "SA-15(13)")]
        waived = self.plan(retired, waivers=waivers)
        self.assertEqual([x.action for x in waived.corrections], ["retire"])  # a person accepted the defect knowingly

    # -- what a bad entry does -------------------------------------------------------------------------
    def test_a_stale_correction_holds_its_control_back_and_records_nothing(self):
        plan = self.plan(statement_entry(source_value_sha256="0" * 64))
        self.assertIn("CORRECTION_STALE", [i.code for i in plan.quarantined["SA-15(13)"]])
        self.assertEqual(plan.corrections, [])

    def test_text_that_upstream_already_fixed_is_noted_not_applied(self):
        plan = self.plan(statement_entry(value=DUP_TEXT, source_value_sha256="0" * 64))
        self.assertIn("CORRECTION_ADOPTED_UPSTREAM", [i.code for i in plan.issues])
        self.assertEqual((plan.corrections, plan.corrected), ([], 0))

    def test_a_correction_that_changes_nothing_is_refused(self):
        plan = self.plan(statement_entry(value=DUP_TEXT))
        self.assertIn("CORRECTION_NO_CHANGE", [i.code for v in plan.quarantined.values() for i in v])

    def test_a_correction_aimed_at_a_missing_control_blocks_the_run(self):
        plan = self.plan(statement_entry(control_id="ZZ-9"))
        self.assertTrue(plan.blocked)
        with self.assertRaises(PlanBlocked):
            apply_plan(plan, self.repo, "tester")
        self.assertEqual(self.repo.fetchone("SELECT COUNT(*) FROM source_requirement")[0], 0)

    def test_an_entry_for_another_version_is_ignored(self):
        plan = self.plan(statement_entry(framework_version="9.9.9"))
        self.assertEqual((sorted(plan.quarantined), plan.corrections, plan.correction_notes), (["SA-15(12)", "SA-15(13)"], [], []))

    def test_a_multi_clause_statement_is_refused_rather_than_left_inconsistent(self):
        ac2 = next(c for c in self.parsed.controls if c.control_id == "AC-2")
        e = {**statement_entry(), "control_id": "AC-2", "source_value_sha256": field_hash(ac2.statement), "value": "Something else entirely."}
        plan = self.plan(e)
        self.assertIn("CORRECTION_CLAUSES", [i.code for i in plan.quarantined["AC-2"]])

    def test_the_single_clause_follows_the_corrected_statement(self):
        eff = effective_control(next(c for c in self.parsed.controls if c.control_id == "SA-15(13)"),
                                [self.plan(statement_entry()).corrections[0]])
        self.assertEqual([c.text for c in eff.clauses], [SYNTHETIC])
        self.assertEqual(eff.statement, SYNTHETIC)

    def test_wording_that_ignores_the_controls_parameters_is_flagged_not_blocked(self):
        plan = self.plan(statement_entry(value="SYNTHETIC: Require the developer to log things."))
        self.assertIn("CORRECTION_PARAMETERS_UNREFERENCED", [i.code for i in plan.issues])
        self.assertEqual(plan.quarantined, {})
        self.assertNotIn("CORRECTION_PARAMETERS_UNREFERENCED", [i.code for i in self.plan(statement_entry()).issues])

    def test_a_correction_warning_can_be_waived_and_the_correction_still_applies(self):
        waiver = dict(framework_version=VERSION, control_id="SA-15(13)", code="CORRECTION_PARAMETERS_UNREFERENCED",
                      reason="the official text really does not use them", reviewer="a.reviewer", reviewed_at="2026-10-06")
        plan = self.plan(statement_entry(value="SYNTHETIC: Require the developer to log things."), waivers=[waiver])
        self.assertEqual((plan.corrected, [i.code for i, _ in plan.waived]), (1, ["CORRECTION_PARAMETERS_UNREFERENCED"]))

    # -- the database enforces the rules ----------------------------------------------------------------
    def test_the_overlay_is_append_only(self):
        self.apply(statement_entry())
        for sql in ("UPDATE source_correction SET value='x'", "DELETE FROM source_correction"):
            with self.assertRaises(self.repo.DatabaseError):
                self.repo.execute(sql)

    def test_the_database_rejects_a_malformed_revision_chain(self):
        self.apply(statement_entry())
        run = self.repo.fetchone("SELECT id FROM import_run")[0]
        base = dict(framework_code=CODE, framework_version=VERSION, control_id="SA-15(13)", field="statement",
                    value="v", source_value_sha256="0" * 64, problem="p", citation="c", reviewer="r", reviewed_at="d")
        for bad in (dict(revision=3), dict(revision=2, action="retire", value="v"), dict(revision=2, citation=" ")):
            with self.assertRaises(self.repo.DatabaseError, msg=str(bad)):
                self.repo.add_correction(Correction(**{**base, **bad}), run)
        self.repo.add_correction(Correction(**{**base, "revision": 2, "action": "retire", "value": "", "citation": ""}), run)
        with self.assertRaises(self.repo.DatabaseError):  # nothing is in force any more, so there is nothing to retire
            self.repo.add_correction(Correction(**{**base, "revision": 3, "action": "retire", "value": "", "citation": ""}), run)

    def test_a_correction_for_a_control_that_is_not_in_the_mirror_is_impossible(self):
        run = self.repo.start_run("t", "", "0" * 64, CODE, VERSION)
        with self.assertRaises(self.repo.DatabaseError):
            self.repo.add_correction(Correction(CODE, VERSION, "AC-1", "title", "v", "0" * 64, "p", "c", "r", "d"), run)

    # -- verify -----------------------------------------------------------------------------------------
    def test_verify_is_clean_with_corrections_and_catches_damage_to_them(self):
        self.apply(statement_entry())
        self.assertEqual(verify_repository(self.repo, self.parsed), [])
        self.repo._simulate_out_of_band_tampering("corrections")
        self.repo.execute("UPDATE source_correction SET source_value_sha256=?", ("0" * 64,))
        problems = verify_repository(self.repo, self.parsed)
        self.assertTrue(any("SA-15(13)" in p and "written against" in p for p in problems), problems)

    def test_verify_catches_a_broken_revision_sequence(self):
        self.apply(statement_entry())
        self.repo._simulate_out_of_band_tampering("corrections")
        self.repo.execute("UPDATE source_correction SET revision=4")
        self.assertTrue(any("unbroken" in p for p in verify_repository(self.repo, self.parsed)))

    def test_verify_flags_a_mirror_row_that_an_older_importer_stored_already_overridden(self):
        self.apply()
        self.repo._simulate_out_of_band_tampering()
        row = self.row("AC-1", "source_requirement")
        payload = json.loads(row["payload_json"])
        payload["overrides_applied"] = [["statement", "old importer"]]
        self.repo.execute("UPDATE source_requirement SET payload_json=? WHERE control_id='AC-1'", (json.dumps(payload),))
        self.assertTrue(any("AC-1" in p and "carries a correction" in p for p in verify_repository(self.repo, self.parsed)))

    def test_a_correction_that_would_make_a_stored_control_invalid_is_reported_by_verify(self):
        self.apply(statement_entry())
        clash = {**statement_entry(), "control_id": "SA-15(12)"}  # SA-15(12) would now read like the corrected SA-15(13)
        cur = load_curation(curation_file(corrections=[clash]))
        problems = verify_repository(self.repo, self.parsed, cur)
        self.assertTrue(any(p.startswith("SA-15(12)") and "held back" in p for p in problems), problems)
        self.assertEqual(verify_repository(self.repo, self.parsed), [])  # without that entry the repository is sound


for_each_engine(CorrectionTests)


class CorrectionCliTests:
    """The command line: check, show, draft and list, driven through ``main`` against a real repository."""

    def setUp(self):
        self.repo, self.spec = self.engine.new()
        self.db = self.spec

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                code = cli.main(["--db", self.db, *args])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue()

    def test_draft_gives_a_skeleton_that_will_not_load_until_a_person_fills_it_in(self):
        out = os.path.join(tempfile.mkdtemp(), "draft.json")
        code, text = self.run_cli("corrections", "draft", FIXTURE, "--control", "SA-15(13)", "--out", out)
        self.assertEqual(code, 0, text)
        with open(out, encoding="utf-8") as fh:
            doc = json.load(fh)
        entry = doc["corrections"][0]
        self.assertEqual((entry["control_id"], entry["field"], entry["source_value_sha256"]),
                         ("SA-15(13)", "statement", field_hash(DUP_TEXT)))
        self.assertEqual(entry["value"], "")
        self.assertEqual(entry["_evidence"]["other_controls_with_the_identical_text"], ["SA-15(12)"])
        self.assertEqual([p["id"] for p in entry["_evidence"]["parameters"]], ["sa-15.13_odp.01", "sa-15.13_odp.02", "sa-15.13_odp.03"])
        with self.assertRaises(ValueError):
            load_curation(out)
        code, text = self.run_cli("corrections", "draft", FIXTURE, "--control", "SA-15(13)", "--out", out)
        self.assertNotEqual(code, 0)  # never overwrites

    def test_check_show_and_list_follow_a_completed_entry(self):
        cur = curation_file(corrections=[statement_entry()])
        code, text = self.run_cli("corrections", "check", FIXTURE, "--manifest", MANIFEST, "--curation", cur)
        self.assertEqual(code, 0, text)
        self.assertIn("SA-15(13)", text)
        self.assertIn("1 control(s) read differently", text)
        code, text = self.run_cli("import", FIXTURE, "--manifest", MANIFEST, "--curation", cur, "--apply")
        self.assertEqual(code, 0, text)
        self.assertIn("CORRECTION SA-15(13) statement: new", text)
        code, text = self.run_cli("corrections", "list", "--version", VERSION)
        self.assertIn("SA-15(13)", text)
        self.assertIn("1 correction record(s) in force", text)
        code, text = self.run_cli("corrections", "show", FIXTURE, "--control", "SA-15(13)")
        self.assertIn("recorded: statement r1 set by a.reviewer", text)

    def test_check_fails_on_a_stale_entry(self):
        cur = curation_file(corrections=[statement_entry(source_value_sha256="0" * 64)])
        code, text = self.run_cli("corrections", "check", FIXTURE, "--manifest", MANIFEST, "--curation", cur)
        self.assertEqual(code, 1)
        self.assertIn("CORRECTION_STALE", text)


for_each_engine(CorrectionCliTests)


class ReviewConsoleCorrectionTests:
    """The console says which requirements read differently from the published text, and why."""

    @classmethod
    def setUpClass(cls):
        import http.client
        import threading
        from airrp_ingest.review_ui import make_server
        cls.repo, _ = cls.engine.new()
        apply_plan(build_plan(load_catalog(FIXTURE), cls.repo, load_manifest(MANIFEST),
                              load_curation(curation_file(corrections=[statement_entry()]))), cls.repo, "tester")
        cls.httpd = make_server(cls.repo, CODE, VERSION, port=0, token="tok")
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.port = cls.httpd.server_address[1]
        cls.http = http.client

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get(self, path):
        conn = self.http.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}", "X-Token": "tok"})
        return json.loads(conn.getresponse().read())

    def test_unmapped_rows_carry_the_effective_text_and_a_corrected_flag(self):
        rows = {r["control_id"]: r for r in self.get("/api/unmapped?limit=100")}
        self.assertEqual((rows["SA-15(13)"]["corrected"], rows["SA-15(13)"]["statement"]), (1, SYNTHETIC))
        self.assertEqual(rows["SA-15(12)"]["corrected"], 0)

    def test_summary_counts_and_lists_the_corrections(self):
        self.assertEqual(self.get("/api/summary")["corrected_requirements"], 1)
        listed = self.get("/api/corrections")
        self.assertEqual([(c["control_id"], c["field"], c["reviewer"]) for c in listed], [("SA-15(13)", "statement", "a.reviewer")])


for_each_engine(ReviewConsoleCorrectionTests)


if __name__ == "__main__":
    unittest.main()
