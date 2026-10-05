import csv
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from airrp_ingest.cli import main
from airrp_ingest.masters import create_master, quality_flags, update_master
from airrp_ingest.migrate import COL, migrate_dry_or_apply, parse_legacy
from airrp_ingest.oscal import load_catalog
from airrp_ingest.pipeline import apply_plan, build_plan

from .helpers import FIXTURE, for_each_engine
from .test_store_masters import MASTER

PLACEHOLDER_EVIDENCE = "Retain governed evidence demonstrating the control operates."


def legacy_row(seq, control, title, decision, mid, name, *, domain="Identity and Access", freq="Annual",
               evidence="Approved policy set and review records.", test="Inspect approvals and sampled records.",
               rationale="AUTO: Existing canonical Master Control 'X' meets semantic reuse threshold 0.88 at score 0.913."):
    row = [""] * 38
    for key, val in {"seq": str(seq), "name": f"{control} — {title}", "decision": decision, "master_id": mid,
                     "master_name": name, "objective": f"Objective of {name}.", "description": f"Description of {name}.",
                     "domain": domain, "control_type": "Preventive", "frequency": freq, "evidence": evidence,
                     "test_procedure": test, "rationale": rationale}.items():
        row[COL[key]] = val
    return row


def rows():
    return [
        legacy_row(1, "AC-1", "Policy and Procedures", "REUSED_ACTIVE", "MCL-GV-001", "Security governance", domain="Governance"),
        legacy_row(2, "AC-2", "Account Management", "WORKSPACE_PROPOSED", "AIRRP-CTRL-AAA", "Manage User and System Accounts",
                   freq="AsRequired", evidence=PLACEHOLDER_EVIDENCE, test="EvidenceReview",
                   rationale="Best existing 'X' scored 0.350, below reuse threshold 0.88; AIRRP will create a proposal."),
        legacy_row(3, "AC-2(1)", "Automated System Account Management", "WORKSPACE_PROPOSED", "AIRRP-CTRL-AAA",
                   "Manage User and System Accounts", freq="AsRequired", evidence=PLACEHOLDER_EVIDENCE, test="EvidenceReview"),
        legacy_row(4, "AC-2(1)", "Automated System Account Management", "WORKSPACE_PROPOSED", "AIRRP-CTRL-AAA",
                   "Manage User and System Accounts", freq="AsRequired", evidence=PLACEHOLDER_EVIDENCE, test="EvidenceReview"),
        legacy_row(5, "SA-15(12)", "Minimize PII", "REUSED_ACTIVE", "MCL-GV-001", "Security governance", domain="Governance"),
        legacy_row(6, "SA-15", "Development Process", "", "", ""),  # legacy 'unmapped'
        legacy_row(7, "AC-2", "Account Management", "WORKSPACE_PROPOSED", "AIRRP-CTRL-BAD", "Odd Frequency Control", freq="Periodic"),
    ]


class MigrateTests:
    def setUp(self):
        self.repo, _ = self.engine.new()
        apply_plan(build_plan(load_catalog(FIXTURE), self.repo), self.repo, "tester")  # SA-15(12) is quarantined

    def run_migration(self, apply):
        return migrate_dry_or_apply(self.repo, parse_legacy(rows()), "NIST-SP-800-53", "5.2.0", "vishal", apply)

    def test_dry_run_reports_but_writes_nothing(self):
        report = self.run_migration(False)
        self.assertEqual((report["masters_imported"], report["mappings_suggested"]), (2, 3))
        self.assertEqual(self.repo.fetchone("SELECT COUNT(*) FROM master_control")[0], 0)
        self.assertEqual(self.repo.fetchone("SELECT COUNT(*) FROM mapping")[0], 0)

    def test_apply_imports_drafts_and_suggestions_only(self):
        report = self.run_migration(True)
        self.assertEqual(report["duplicate_pairs_ignored"], 1)  # the repeated AC-2(1) row
        self.assertEqual(report["legacy_unmapped_requirements"], ["SA-15"])
        self.assertEqual(sorted(report["mappings_skipped"]), [
            "AC-2 -> AIRRP-CTRL-BAD: master not imported", "SA-15(12) -> MCL-GV-001: requirement not loaded"])
        self.assertEqual(len(report["errors"]), 1)  # 'Periodic' is not in the controlled vocabulary
        self.assertEqual({r[0] for r in self.repo.fetchall("SELECT status FROM master_control")}, {"draft"})
        self.assertEqual({r[0] for r in self.repo.fetchall("SELECT status FROM mapping")}, {"suggested"})
        self.assertEqual(report["master_flags"], {"PLACEHOLDER_EVIDENCE": 1, "PLACEHOLDER_TEST_PROCEDURE": 1, "UNCONFIRMED_FREQUENCY": 1})

    def test_migration_is_idempotent(self):
        self.run_migration(True)
        again = self.run_migration(True)
        self.assertEqual((again["masters_imported"], again["mappings_suggested"]), (0, 0))

    def test_legacy_scores_and_decisions_are_kept_as_evidence(self):
        self.run_migration(True)
        row = self.repo.fetchone(
            "SELECT m.score, m.evidence FROM mapping m JOIN source_requirement r ON r.id=m.requirement_id"
            " WHERE r.control_id='AC-1'")
        self.assertEqual((row["score"], row["evidence"]), (0.913, "legacy workbook: REUSED_ACTIVE"))

    def test_malformed_export_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_legacy([["not", "the", "export"]])


class QualityGateTests:
    def setUp(self):
        self.repo, _ = self.engine.new()
        migrate_dry_or_apply(self.repo, parse_legacy(rows()), "NIST-SP-800-53", "5.2.0", "vishal", True)

    def test_flagged_master_cannot_be_approved_even_by_a_second_person(self):
        with self.assertRaisesRegex(ValueError, "quality flags"):
            self.repo.approve_master_control("AIRRP-CTRL-AAA", "someone-else")
        with self.assertRaises(self.repo.IntegrityError):  # and the schema backs it up
            self.repo.execute("UPDATE master_control SET status='approved', approved_by='x', approved_at='y'"
                              " WHERE id='AIRRP-CTRL-AAA'")

    def test_clean_legacy_master_can_be_approved_by_a_second_person(self):
        self.repo.approve_master_control("MCL-GV-001", "bob")
        self.assertEqual(self.repo.fetchone("SELECT status FROM master_control WHERE id='MCL-GV-001'")[0], "approved")

    def test_fixing_the_fields_clears_the_flags(self):
        self.assertEqual(update_master(self.repo, "sme", "AIRRP-CTRL-AAA", evidence="Account inventory and removal tickets."),
                         ["PLACEHOLDER_TEST_PROCEDURE", "UNCONFIRMED_FREQUENCY"])
        flags = update_master(self.repo, "sme", "AIRRP-CTRL-AAA", test_procedure="Sample accounts across the lifecycle.",
                              frequency="AsRequired")  # explicitly confirming AsRequired is allowed
        self.assertEqual(flags, [])
        self.repo.approve_master_control("AIRRP-CTRL-AAA", "bob")

    def test_placeholder_text_cannot_be_submitted_as_a_fix(self):
        flags = update_master(self.repo, "sme", "AIRRP-CTRL-AAA", evidence=PLACEHOLDER_EVIDENCE)
        self.assertIn("PLACEHOLDER_EVIDENCE", flags)

    def test_new_master_controls_cannot_use_placeholders(self):
        for change in ({"evidence": PLACEHOLDER_EVIDENCE}, {"test_procedure": "EvidenceReview"}):
            with self.assertRaises(ValueError):
                create_master(self.engine.new()[0], "alice", **{**MASTER, **change})

    def test_only_drafts_can_be_edited(self):
        self.repo.approve_master_control("MCL-GV-001", "bob")
        with self.assertRaises(ValueError):
            update_master(self.repo, "sme", "MCL-GV-001", objective="changed after approval")

    def test_todo_csv_round_trip(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "todo.csv")
        repo, db = self.engine.new()
        apply_plan(build_plan(load_catalog(FIXTURE), repo), repo, "tester")
        migrate_dry_or_apply(repo, parse_legacy(rows()), "NIST-SP-800-53", "5.2.0", "vishal", True)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--db", db, "master", "todo", "--out", path]), 0)
        with open(path, encoding="utf-8", newline="") as fh:
            table = list(csv.DictReader(fh))
        self.assertEqual([r["id"] for r in table], ["AIRRP-CTRL-AAA"])
        table[0]["evidence"], table[0]["test_procedure"], table[0]["frequency"] = "Account inventory", "Sample accounts", "Continuous"
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(table[0]))
            w.writeheader()
            w.writerows(table)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--db", db, "master", "apply-csv", "--out", path]), 0)
        flags = repo.fetchone("SELECT quality_flags FROM master_control WHERE id='AIRRP-CTRL-AAA'")[0]
        self.assertEqual(json.loads(flags), [])


for cls in (MigrateTests, QualityGateTests):
    for_each_engine(cls)


if __name__ == "__main__":
    unittest.main()
