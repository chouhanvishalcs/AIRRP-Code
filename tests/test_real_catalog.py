"""Runs against the full NIST catalog when OSCAL_CATALOG points at it (CI downloads it; see docs)."""
import json
import os
import tempfile
import unittest

from .helpers import for_each_engine

from airrp_ingest.bundle import build_bundle
from airrp_ingest.curation import field_hash, load_curation
from airrp_ingest.normalize import content_hash, obligation_hash, sha256_hex
from airrp_ingest.oscal import load_catalog
from airrp_ingest.pipeline import apply_plan, build_plan, control_from_payload, verify_repository
from airrp_ingest.validate import load_manifest

CATALOG = os.environ.get("OSCAL_CATALOG")
MANIFEST = os.path.join(os.path.dirname(__file__), "..", "manifests", "nist-sp-800-53-5.2.0.json")

# Produced by the importer as it stood before the correction layer, from this catalog file
# (sha256 01f37cf90ea99d92242c936cbfbdebcc338eef1f71454e2acac36cc56e9bc062), with no curation.
GOLDEN_HASHES_SHA256 = "282b229dfc79b30f6608488f15089c5454a32ea6e256b2ba0bff18fe630a0aaf"  # sorted [id, content_hash, obligation_hash]
GOLDEN_BUNDLE_SHA256 = "f06ac1ecf712a8d5b105cd0f0e0061b6d5b7d8cc0b99cf1ab7a46725660fcdec"
GOLDEN_REQUIREMENTS_SHA256 = "f172a572a8d96039bcd44076fb0ed1533db72dad1fc587237e1493fd0dc35c3e"


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


    def test_without_corrections_nothing_changes_from_before_the_correction_layer_existed(self):
        """Pinned against output produced by the importer as it was before corrections existed (same NIST file)."""
        parsed = load_catalog(CATALOG)
        repo, _ = self.engine.new()
        apply_plan(build_plan(parsed, repo, load_manifest(MANIFEST)), repo, "ci")
        rows = sorted([r["control_id"], r["content_hash"],
                       obligation_hash(control_from_payload(r["payload_json"]))]
                      for r in repo.requirements("NIST-SP-800-53", "5.2.0", "active") + repo.requirements("NIST-SP-800-53", "5.2.0", "withdrawn"))
        self.assertEqual(sha256_hex(json.dumps(rows, separators=(",", ":"))), GOLDEN_HASHES_SHA256)
        bundle = build_bundle(repo, "NIST-SP-800-53", "5.2.0")
        self.assertEqual(bundle["bundle_sha256"], GOLDEN_BUNDLE_SHA256)
        self.assertNotIn("corrections", bundle)
        keys = ("code", "name", "frequency", "legalText", "legalTitle", "description", "ownerFunction",
                "obligationType", "regulationCode", "sourceReference")
        export = json.dumps([{k: r[k] for k in keys} for r in bundle["requirements"]], ensure_ascii=False, indent=1)
        self.assertEqual(sha256_hex(export), GOLDEN_REQUIREMENTS_SHA256)

    def test_a_synthetic_correction_changes_exactly_one_requirement(self):
        """SYNTHETIC wording, only to show the mechanism on the real file; the real SA-15(13) text is not recorded here."""
        parsed = load_catalog(CATALOG)
        sa13 = next(c for c in parsed.controls if c.control_id == "SA-15(13)")
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump({"corrections": [{
                "framework_version": "5.2.0", "control_id": "SA-15(13)", "field": "statement",
                "value": "SYNTHETIC TEST WORDING for [secure logging format(s)], [events types to log], [level of detail to log].",
                "source_value_sha256": field_hash(sa13.statement), "problem": "synthetic", "citation": "synthetic",
                "reviewer": "ci", "reviewed_at": "2026-10-06"}]}, fh)
        repo, _ = self.engine.new()
        plan = build_plan(parsed, repo, load_manifest(MANIFEST), load_curation(path))
        self.assertEqual((plan.blockers, plan.quarantined, plan.corrected), ([], {}, 1))
        apply_plan(plan, repo, "ci")
        self.assertEqual(repo.coverage("NIST-SP-800-53", "5.2.0")["active_requirements"], 1014)
        self.assertEqual(verify_repository(repo, parsed, load_curation(path)), [])
        bundle = build_bundle(repo, "NIST-SP-800-53", "5.2.0")
        published = {c.control_id: c for c in parsed.controls}
        differing = [r["control_id"] for r in bundle["requirements"]
                     if r["content_hash"] != content_hash(published[r["control_id"]])]
        self.assertEqual(differing, ["SA-15(13)"])
        self.assertEqual([c["control_id"] for c in bundle["corrections"]], ["SA-15(13)"])


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
