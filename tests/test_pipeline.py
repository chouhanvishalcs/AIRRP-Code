import json
import os
import tempfile
import unittest

from airrp_ingest.curation import field_hash, load_curation
from airrp_ingest.oscal import load_catalog
from airrp_ingest.pipeline import PlanBlocked, apply_plan, build_plan, verify_repository
from airrp_ingest.validate import load_manifest

from .helpers import FIXTURE, MANIFEST, find_control, for_each_engine, mutated_catalog

DUP_TEXT = "Require the developer of the system or system component to minimize the use of personally identifiable information in development and test environments."


def curation_file(**doc):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(doc, fh)
    return path


class PipelineTests:
    def setUp(self):
        self.repo, self.spec = self.engine.new()
        self.manifest = load_manifest(MANIFEST)

    def plan(self, path=FIXTURE, curation=None):
        return build_plan(load_catalog(path), self.repo, self.manifest, curation)

    def test_dry_run_writes_nothing_and_quarantines_the_duplicate_pair(self):
        plan = self.plan()
        self.assertEqual((len(plan.to_add), sorted(plan.quarantined)), (5, ["SA-15(12)", "SA-15(13)"]))
        self.assertEqual(self.repo.fetchone("SELECT COUNT(*) FROM source_requirement")[0], 0)

    def test_apply_is_idempotent(self):
        apply_plan(self.plan(), self.repo, "tester")
        again = self.plan()
        self.assertEqual((len(again.to_add), len(again.unchanged), len(again.conflicts)), (0, 5, 0))
        apply_plan(again, self.repo, "tester")
        self.assertEqual(self.repo.fetchone("SELECT COUNT(*) FROM source_requirement")[0], 5)
        self.assertEqual(self.repo.fetchone("SELECT COUNT(*) FROM review_item WHERE status='open'")[0], 2)

    def test_quarantined_controls_never_reach_the_repository(self):
        apply_plan(self.plan(), self.repo, "tester")
        ids = set(self.repo.requirement_hashes("NIST-SP-800-53", "5.2.0"))
        self.assertFalse({"SA-15(12)", "SA-15(13)"} & ids)

    def test_quarantined_parent_blocks_its_enhancements(self):
        def mutate(cat):
            find_control(cat, "ac-2")["title"] = ""
        path, cleanup = mutated_catalog(mutate)
        self.addCleanup(cleanup)
        plan = self.plan(path)
        self.assertEqual(plan.quarantined["AC-2(1)"][0].code, "PARENT_QUARANTINED")

    def test_source_changed_under_same_version_blocks_the_whole_run(self):
        apply_plan(self.plan(), self.repo, "tester")
        path, cleanup = mutated_catalog(lambda cat: find_control(cat, "ac-2").update(title="Account Management (edited)"))
        self.addCleanup(cleanup)
        plan = self.plan(path)
        self.assertTrue(plan.blocked)
        self.assertEqual([c[0] for c in plan.conflicts], ["AC-2"])
        with self.assertRaises(PlanBlocked):
            apply_plan(plan, self.repo, "tester")

    def test_manifest_failure_blocks_apply(self):
        path, cleanup = mutated_catalog(lambda cat: cat["groups"][0]["controls"].pop())
        self.addCleanup(cleanup)
        plan = self.plan(path)
        self.assertTrue(plan.blocked)
        with self.assertRaises(PlanBlocked):
            apply_plan(plan, self.repo, "tester")

    def test_failed_apply_rolls_back_completely(self):
        plan = self.plan()
        self.repo.add_requirement = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            apply_plan(plan, self.repo, "tester")
        self.assertEqual(self.repo.fetchone("SELECT COUNT(*) FROM framework")[0], 0)
        self.assertEqual(self.repo.fetchone("SELECT COUNT(*) FROM import_run")[0], 0)

    def test_verify_detects_tampering(self):
        apply_plan(self.plan(), self.repo, "tester")
        parsed = load_catalog(FIXTURE)
        self.assertEqual(verify_repository(self.repo, parsed), [])
        self.repo._simulate_out_of_band_tampering()
        self.repo.execute("UPDATE source_requirement SET statement='tampered' WHERE control_id='AC-1'")
        self.assertTrue(any("AC-1" in p for p in verify_repository(self.repo, parsed)))


class CurationTests:
    def setUp(self):
        self.repo, self.spec = self.engine.new()
        self.manifest = load_manifest(MANIFEST)

    def plan(self, cur):
        return build_plan(load_catalog(FIXTURE), self.repo, self.manifest, load_curation(cur))

    def test_waiver_unblocks_only_the_named_control_and_code(self):
        w = dict(framework_version="5.2.0", control_id="SA-15(12)", code="DUPLICATE_STATEMENT",
                 reason="verified against NIST publication", reviewer="a.reviewer", reviewed_at="2026-10-05")
        plan = self.plan(curation_file(waivers=[w]))
        self.assertEqual(sorted(plan.quarantined), ["SA-15(13)"])
        apply_plan(plan, self.repo, "tester")
        status = dict(self.repo.fetchall("SELECT control_id, status FROM review_item"))
        self.assertEqual(status, {"SA-15(12)": "waived", "SA-15(13)": "open"})

    def test_waiver_for_another_version_does_not_apply(self):
        w = dict(framework_version="9.9.9", control_id="SA-15(12)", code="DUPLICATE_STATEMENT",
                 reason="x", reviewer="r", reviewed_at="2026-10-05")
        self.assertEqual(len(self.plan(curation_file(waivers=[w])).quarantined), 2)

    def test_incomplete_waiver_is_rejected(self):
        with self.assertRaises(ValueError):
            load_curation(curation_file(waivers=[{"control_id": "SA-15(12)", "code": "DUPLICATE_STATEMENT"}]))

    def entry(self, **kw):
        return dict(framework_version="5.2.0", control_id="SA-15(13)", field="statement",
                    value="Require the developer to use a defined secure logging format.",
                    source_value_sha256=field_hash(DUP_TEXT), problem="the published text repeats SA-15(12)",
                    citation="NIST SP 800-53 Rev 5.2.0 p.X", reviewer="a.reviewer", reviewed_at="2026-10-05", **kw)

    def test_correction_unblocks_the_control_and_leaves_the_source_verbatim(self):
        plan = self.plan(curation_file(corrections=[self.entry()]))
        self.assertEqual(plan.quarantined, {})
        added = {c.control_id: c for c in plan.to_add}
        self.assertEqual(added["SA-15(13)"].statement, DUP_TEXT)  # what is stored is what was published
        self.assertEqual(added["SA-15(13)"].overrides_applied, ())
        self.assertEqual([(x.control_id, x.field, x.revision) for x in plan.corrections], [("SA-15(13)", "statement", 1)])
        self.assertEqual(added["SA-15(12)"].statement, DUP_TEXT)  # untouched

    def test_the_old_overrides_key_still_loads(self):
        plan = self.plan(curation_file(overrides=[self.entry()]))
        self.assertEqual(plan.quarantined, {})

    def test_a_correction_must_say_what_is_wrong(self):
        e = self.entry()
        del e["problem"]
        with self.assertRaises(ValueError):
            load_curation(curation_file(corrections=[e]))

    def test_stale_correction_fails_loudly(self):
        plan = self.plan(curation_file(corrections=[{**self.entry(), "source_value_sha256": "0" * 64}]))
        self.assertIn("CORRECTION_STALE", [i.code for v in plan.quarantined.values() for i in v])

    def test_only_text_fields_can_be_corrected(self):
        o = {**self.entry(), "control_id": "AC-1", "field": "control_id", "value": "AC-9"}
        with self.assertRaises(ValueError):
            load_curation(curation_file(corrections=[o]))


for_each_engine(PipelineTests)
for_each_engine(CurationTests)


if __name__ == "__main__":
    unittest.main()
