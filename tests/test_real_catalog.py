"""Runs against the full NIST catalog when OSCAL_CATALOG points at it (CI downloads it; see docs)."""
import os
import unittest

from airrp_ingest.oscal import load_catalog
from airrp_ingest.pipeline import apply_plan, build_plan, verify_repository
from airrp_ingest.store import SqliteRepository
from airrp_ingest.validate import load_manifest

CATALOG = os.environ.get("OSCAL_CATALOG")
MANIFEST = os.path.join(os.path.dirname(__file__), "..", "manifests", "nist-sp-800-53-5.2.0.json")


@unittest.skipUnless(CATALOG and os.path.exists(CATALOG), "set OSCAL_CATALOG to the NIST 800-53 r5.2.0 OSCAL catalog")
class RealCatalogTests(unittest.TestCase):
    def test_full_catalog_reconciles_and_round_trips(self):
        parsed = load_catalog(CATALOG)
        self.assertEqual(len(parsed.controls), 1196)
        repo = SqliteRepository(":memory:")
        plan = build_plan(parsed, repo, load_manifest(MANIFEST))
        self.assertEqual(plan.blockers, [])
        self.assertEqual(sorted(plan.quarantined), ["SA-15(12)", "SA-15(13)"])
        apply_plan(plan, repo, "ci")
        self.assertEqual(len(build_plan(parsed, repo, load_manifest(MANIFEST)).to_add), 0)
        self.assertEqual(verify_repository(repo, parsed), [])
        self.assertEqual(repo.coverage("NIST-SP-800-53", "5.2.0")["active_requirements"], 1012)


if __name__ == "__main__":
    unittest.main()
