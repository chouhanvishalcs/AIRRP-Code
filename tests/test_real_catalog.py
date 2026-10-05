"""Runs against the full NIST catalog when OSCAL_CATALOG points at it (CI downloads it; see docs)."""
import os
import unittest

from .helpers import for_each_engine

from airrp_ingest.oscal import load_catalog
from airrp_ingest.pipeline import apply_plan, build_plan, verify_repository
from airrp_ingest.validate import load_manifest

CATALOG = os.environ.get("OSCAL_CATALOG")
MANIFEST = os.path.join(os.path.dirname(__file__), "..", "manifests", "nist-sp-800-53-5.2.0.json")


class RealCatalogTests:
    def setUp(self):
        if not (CATALOG and os.path.exists(CATALOG)):
            self.skipTest("set OSCAL_CATALOG to the NIST 800-53 r5.2.0 OSCAL catalog")

    def test_full_catalog_reconciles_and_round_trips(self):
        parsed = load_catalog(CATALOG)
        self.assertEqual(len(parsed.controls), 1196)
        repo, _ = self.engine.new()
        plan = build_plan(parsed, repo, load_manifest(MANIFEST))
        self.assertEqual(plan.blockers, [])
        self.assertEqual(sorted(plan.quarantined), ["SA-15(12)", "SA-15(13)"])
        apply_plan(plan, repo, "ci")
        self.assertEqual(len(build_plan(parsed, repo, load_manifest(MANIFEST)).to_add), 0)
        self.assertEqual(verify_repository(repo, parsed), [])
        self.assertEqual(repo.coverage("NIST-SP-800-53", "5.2.0")["active_requirements"], 1012)


for_each_engine(RealCatalogTests)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(CATALOG and os.path.exists(CATALOG), "set OSCAL_CATALOG to the NIST 800-53 r5.2.0 OSCAL catalog")
class ProofHarnessTests(unittest.TestCase):
    """The proof must pass on the genuine catalog and must fail on a damaged one (it is not vacuous)."""

    def _run(self, catalog):
        import io
        from contextlib import redirect_stdout

        from airrp_ingest.proof import SqliteProofEngine, run_proof
        with redirect_stdout(io.StringIO()):
            claims, _ = run_proof(catalog, MANIFEST, os.path.join(os.path.dirname(MANIFEST), "..", "curation", "nist-sp-800-53.json"),
                                  engines=[SqliteProofEngine()])
        return {c.id: c.status for c in claims}

    def test_all_claims_pass_on_the_real_catalog(self):
        status = self._run(CATALOG)
        self.assertEqual({k: v for k, v in status.items() if v != "PASS" and k != "C4"}, {})

    def test_damaged_catalog_fails_the_proof(self):
        import json
        import tempfile
        with open(CATALOG, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["catalog"]["groups"][0]["controls"][1]["controls"].pop(0)  # silently drop AC-2(1)
        path = os.path.join(tempfile.mkdtemp(), "bad.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        status = self._run(path)
        self.assertEqual((status["C1"], status["E1"]), ("FAIL", "FAIL"))
