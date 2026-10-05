import io
import json
import os
import unittest
from contextlib import redirect_stdout

from airrp_ingest.airrp_export import airrp_requirement
from airrp_ingest.bundle import build_bundle
from airrp_ingest.cli import main
from airrp_ingest.masters import create_master
from airrp_ingest.oscal import load_catalog
from airrp_ingest.pipeline import apply_plan, build_plan

from .helpers import FIXTURE, HERE, for_each_engine
from .test_store_masters import MASTER

try:
    import jsonschema
except ImportError:  # optional dev dependency
    jsonschema = None

SCHEMA = os.path.join(HERE, "..", "schema", "import-bundle.schema.json")


def repo_with_one_approved_mapping(engine):
    repo, spec = engine.new()
    apply_plan(build_plan(load_catalog(FIXTURE), repo), repo, "tester")
    mid = create_master(repo, "alice", **MASTER)
    repo.approve_master_control(mid, "bob")
    rid = repo.fetchone("SELECT id FROM source_requirement WHERE control_id='AC-2'")[0]
    repo.suggest_mapping(rid, mid, 0.5, "x")
    repo.decide_mapping(repo.open_suggestions()[0]["id"], "approved", "bob", "equivalent", "AC-2 is account management", True)
    return repo, spec


class AirrpExportTests(unittest.TestCase):
    def test_matches_the_existing_airrp_payloads_byte_for_byte(self):
        with open(os.path.join(HERE, "fixtures", "airrp_requirements_golden.json"), encoding="utf-8") as fh:
            golden = {g["code"]: g for g in json.load(fh)}
        active = {c.control_id: c for c in load_catalog(FIXTURE).controls if c.status == "active"}
        produced = {r["code"]: r for r in map(airrp_requirement, active.values())}
        self.assertEqual(produced, golden)


class BundleTests:
    def test_only_reviewed_data_is_exported(self):
        repo, _ = repo_with_one_approved_mapping(self.engine)
        create_master(repo, "alice", **{**MASTER, "name": "Unreviewed Draft", "objective": "Other.",
                                        "description": "Something unrelated."})
        b = build_bundle(repo, "NIST-SP-800-53", "5.2.0")
        self.assertEqual([m["name"] for m in b["master_controls"]], [MASTER["name"]])  # draft is not exported
        self.assertEqual([(m["requirement_control_id"], m["relationship"], m["is_primary"]) for m in b["mappings"]],
                         [("AC-2", "equivalent", True)])
        self.assertEqual([r["control_id"] for r in b["requirements"]], ["AC-1", "AC-2", "AC-2(1)", "SA-15"])

    def test_bundle_hash_is_deterministic_and_changes_with_content(self):
        repo, _ = repo_with_one_approved_mapping(self.engine)
        first = build_bundle(repo, "NIST-SP-800-53", "5.2.0")
        self.assertEqual(first["bundle_sha256"], build_bundle(repo, "NIST-SP-800-53", "5.2.0")["bundle_sha256"])
        create_master(repo, "alice", **{**MASTER, "name": "Another Control", "objective": "Other.", "description": "Else."})
        repo.approve_master_control(repo.master_controls("draft")[0]["id"], "bob")
        self.assertNotEqual(first["bundle_sha256"], build_bundle(repo, "NIST-SP-800-53", "5.2.0")["bundle_sha256"])

    @unittest.skipIf(jsonschema is None, "pip install jsonschema to validate against the published schema")
    def test_bundle_validates_against_the_published_schema(self):
        with open(SCHEMA, encoding="utf-8") as fh:
            schema = json.load(fh)
        bundle = build_bundle(repo_with_one_approved_mapping(self.engine)[0], "NIST-SP-800-53", "5.2.0")
        jsonschema.validate(bundle, schema)
        bundle["mappings"][0]["relationship"] = "kinda-similar"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(bundle, schema)

    def test_unknown_framework_version_is_an_error(self):
        with self.assertRaises(ValueError):
            build_bundle(self.engine.new()[0], "NIST-SP-800-53", "5.2.0")

    def test_cli_export_writes_both_formats(self):
        import tempfile
        d = tempfile.mkdtemp()
        repo, db = self.engine.new()
        apply_plan(build_plan(load_catalog(FIXTURE), repo), repo, "tester")
        for fmt, name in (("bundle", "b.json"), ("requirements", "r.json")):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--db", db, "export", "--version", "5.2.0", "--format", fmt, "--out", os.path.join(d, name)]), 0)
        with open(os.path.join(d, "r.json"), encoding="utf-8") as fh:
            rows = json.load(fh)
        self.assertEqual(sorted(rows[0]), sorted(["code", "name", "frequency", "legalText", "legalTitle", "description",
                                                  "ownerFunction", "obligationType", "regulationCode", "sourceReference"]))


for_each_engine(BundleTests)


if __name__ == "__main__":
    unittest.main()
