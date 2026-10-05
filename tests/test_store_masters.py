import unittest

from airrp_ingest.masters import DuplicateSuspect, create_master, master_id_for
from airrp_ingest.oscal import load_catalog
from airrp_ingest.pipeline import apply_plan, build_plan
from airrp_ingest.similarity import suggest

from .helpers import FIXTURE, for_each_engine

MASTER = dict(
    name="Manage User and System Accounts", objective="Ensure accounts are uniquely identified, authorised and removed.",
    description="Define account types, assign managers, require approvals, monitor use and disable inactive accounts.",
    domain="Identity and Access", frequency="Continuous", control_type="Preventive",
    evidence="Account inventory, approvals and removal tickets.", test_procedure="Sample accounts across lifecycle events.")


def loaded_repo(engine):
    repo, _ = engine.new()
    apply_plan(build_plan(load_catalog(FIXTURE), repo), repo, "tester")
    return repo


def requirement_id(repo, control_id):
    return repo.fetchone("SELECT id FROM source_requirement WHERE control_id=?", (control_id,))[0]


class StoreRuleTests:
    def setUp(self):
        self.repo = loaded_repo(self.engine)
        self.mid = create_master(self.repo, "alice", **MASTER)
        self.repo.approve_master_control(self.mid, "bob")

    def test_source_requirements_are_immutable(self):
        for sql in ("UPDATE source_requirement SET title='x'", "DELETE FROM source_requirement"):
            with self.assertRaises(self.repo.DatabaseError):
                self.repo.execute(sql)

    def test_duplicate_requirement_row_is_impossible(self):
        c = load_catalog(FIXTURE).controls[0]
        with self.assertRaises(self.repo.IntegrityError):
            self.repo.add_requirement(c, 1)

    def test_master_name_variants_collide(self):
        for name in ("manage user and system accounts", "  Manage   User & System-Accounts!! "):
            with self.assertRaises(self.repo.IntegrityError):
                self.repo.add_master_control({**MASTER, "name": name, "id": "OTHER-" + name[:3]}, "alice")

    def test_mapping_cannot_be_inserted_as_approved(self):
        with self.assertRaises(self.repo.DatabaseError):
            self.repo.execute(
                "INSERT INTO mapping (requirement_id, master_control_id, status, relationship, rationale, reviewer)"
                " VALUES (?,?, 'approved','equivalent','r','x')", (requirement_id(self.repo, "AC-2"), self.mid))

    def test_approval_needs_reviewer_relationship_and_rationale(self):
        rid = requirement_id(self.repo, "AC-2")
        self.repo.suggest_mapping(rid, self.mid, 0.5, "account")
        mapping = self.repo.open_suggestions()[0]["id"]
        for kwargs in (dict(reviewer="", relationship="equivalent", rationale="ok"),
                       dict(reviewer="bob", relationship="similar", rationale="ok"),
                       dict(reviewer="bob", relationship="equivalent", rationale="  ")):
            with self.assertRaises(ValueError):
                self.repo.decide_mapping(mapping, "approved", **kwargs)
        self.repo.decide_mapping(mapping, "approved", "bob", "equivalent", "AC-2 is account management", primary=True)
        self.assertEqual(self.repo.coverage("NIST-SP-800-53", "5.2.0")["with_approved_mapping"], 1)

    def test_decision_is_final_and_suggestions_are_not_duplicated(self):
        rid = requirement_id(self.repo, "AC-2")
        self.assertTrue(self.repo.suggest_mapping(rid, self.mid, 0.5, "a"))
        self.assertFalse(self.repo.suggest_mapping(rid, self.mid, 0.9, "b"))
        mapping = self.repo.open_suggestions()[0]["id"]
        self.repo.decide_mapping(mapping, "rejected", "bob", rationale="different scope")
        with self.assertRaises(ValueError):
            self.repo.decide_mapping(mapping, "approved", "bob", "equivalent", "changed my mind")
        self.assertFalse(self.repo.suggest_mapping(rid, self.mid, 0.9, "c"))  # rejected stays rejected

    def test_only_one_primary_mapping_per_requirement(self):
        other = create_master(self.repo, "alice", **{**MASTER, "name": "Review Account Privileges Regularly",
                              "objective": "Recertify access rights.", "description": "Periodic recertification campaigns."})
        self.repo.approve_master_control(other, "bob")
        rid = requirement_id(self.repo, "AC-2")
        for m in (self.mid, other):
            self.repo.suggest_mapping(rid, m, 0.5, "x")
        first, second = [r["id"] for r in self.repo.open_suggestions()]
        self.repo.decide_mapping(first, "approved", "bob", "equivalent", "r", primary=True)
        with self.assertRaises(self.repo.IntegrityError):
            self.repo.decide_mapping(second, "approved", "bob", "subset", "r", primary=True)

    def test_withdrawn_requirement_cannot_be_mapped_and_draft_master_cannot_receive_mappings(self):
        draft = create_master(self.repo, "alice", **{**MASTER, "name": "Unreviewed Draft Control",
                              "objective": "Something else entirely.", "description": "Not yet approved."})
        self.repo.suggest_mapping(requirement_id(self.repo, "AC-2"), draft, 0.4, "x")
        self.repo.suggest_mapping(requirement_id(self.repo, "AC-2(10)"), self.mid, 0.4, "x")
        self.assertEqual(len(self.repo.open_suggestions()), 1)  # the draft master's suggestion is not reviewable yet
        self.assertEqual(self.repo.blocked_suggestions(), 1)
        for row in self.repo.open_suggestions(ready_only=False):
            with self.assertRaises(self.repo.DatabaseError):
                self.repo.decide_mapping(row["id"], "approved", "bob", "equivalent", "r")

    def test_failed_transaction_rolls_back(self):
        with self.assertRaises(RuntimeError):
            with self.repo.transaction():
                self.repo.execute("DELETE FROM audit_log")
                self.repo.audit("x", "probe", "e", "1", {})
                raise RuntimeError
        self.assertEqual(self.repo.fetchone("SELECT COUNT(*) FROM audit_log WHERE action='probe'")[0], 0)


class MasterCreationTests:
    def setUp(self):
        self.repo, _ = self.engine.new()

    def test_aliases_are_normalised_and_id_is_deterministic(self):
        mid = create_master(self.repo, "alice", **MASTER)
        row = self.repo.master_controls()[0]
        self.assertEqual((row["domain"], mid), ("IdentityAndAccess", master_id_for("manage USER and system accounts")))

    def test_unknown_vocabulary_and_missing_values_are_rejected(self):
        for change in ({"domain": "Misc"}, {"frequency": "Sometimes"}, {"control_type": "Magic"},
                       {"evidence": ""}, {"test_procedure": " "}, {"objective": ""}):
            with self.assertRaises(ValueError, msg=str(change)):
                create_master(self.repo, "alice", **{**MASTER, **change})

    def test_near_duplicate_is_refused_unless_reason_given(self):
        create_master(self.repo, "alice", **MASTER)
        similar = {**MASTER, "name": "Manage Users and System Account Lifecycle"}
        with self.assertRaises(DuplicateSuspect):
            create_master(self.repo, "alice", **similar)
        create_master(self.repo, "alice", distinct_from_reason="Covers service accounts only", **similar)
        self.assertEqual(len(self.repo.master_controls()), 2)

    def test_draft_to_approved_requires_a_draft(self):
        mid = create_master(self.repo, "alice", **MASTER)
        self.repo.approve_master_control(mid, "bob")
        with self.assertRaises(ValueError):
            self.repo.approve_master_control(mid, "bob")


class SuggestionTests:
    def test_suggestions_are_candidates_only_and_idempotent(self):
        repo = loaded_repo(self.engine)
        self.assertEqual(suggest(repo, "NIST-SP-800-53", "5.2.0"), 0)  # no approved masters yet
        mid = create_master(repo, "alice", **MASTER)
        repo.approve_master_control(mid, "bob")
        first = suggest(repo, "NIST-SP-800-53", "5.2.0", min_score=0.1)
        self.assertGreater(first, 0)
        self.assertEqual(suggest(repo, "NIST-SP-800-53", "5.2.0", min_score=0.1), 0)
        statuses = {r[0] for r in repo.fetchall("SELECT status FROM mapping")}
        self.assertEqual(statuses, {"suggested"})
        top = repo.open_suggestions()[0]
        self.assertTrue(top["control_id"].startswith("AC-2"))
        self.assertEqual(repo.coverage("NIST-SP-800-53", "5.2.0")["with_approved_mapping"], 0)


for cls in (StoreRuleTests, MasterCreationTests, SuggestionTests):
    for_each_engine(cls)


if __name__ == "__main__":
    unittest.main()
