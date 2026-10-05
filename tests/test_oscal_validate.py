import unittest

from airrp_ingest.model import ERROR
from airrp_ingest.oscal import load_catalog
from airrp_ingest.validate import load_manifest, validate

from .helpers import FIXTURE, MANIFEST, find_control, mutated_catalog


def codes(issues, severity=None):
    return sorted((i.code, i.control_id) for i in issues if severity in (None, i.severity))


class OscalParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parsed = load_catalog(FIXTURE)
        cls.by_id = {c.control_id: c for c in cls.parsed.controls}

    def test_canonical_ids_and_structure(self):
        self.assertEqual(sorted(self.by_id), ["AC-1", "AC-2", "AC-2(1)", "AC-2(10)", "SA-15", "SA-15(12)", "SA-15(13)"])
        self.assertEqual(self.by_id["AC-2(1)"].parent_id, "AC-2")
        self.assertEqual(self.by_id["AC-2(1)"].kind, "enhancement")
        self.assertEqual(self.by_id["AC-2"].kind, "base")

    def test_statement_is_rendered_verbatim_with_parameters_in_brackets(self):
        text = self.by_id["AC-1"].statement
        self.assertIn("a. Develop, document, and disseminate to [organization-defined personnel or roles]:", text)
        self.assertIn("1. [organization-level | mission/business process-level | system-level] access control policy that:", text)
        self.assertIn("(a) Addresses purpose, scope, roles", text)
        self.assertNotIn("{{", text)

    def test_withdrawn_control_keeps_successor_link(self):
        c = self.by_id["AC-2(10)"]
        self.assertEqual(c.status, "withdrawn")
        self.assertEqual([(l.rel, l.target) for l in c.links if l.rel == "incorporated-into"], [("incorporated-into", "AC-2")])

    def test_content_hash_is_stable_across_loads(self):
        again = {c.control_id: c for c in load_catalog(FIXTURE).controls}
        from airrp_ingest.normalize import content_hash
        for cid, c in self.by_id.items():
            self.assertEqual(content_hash(c), content_hash(again[cid]))


class ValidationTests(unittest.TestCase):
    def test_identical_statements_under_different_ids_are_flagged(self):
        issues = validate(load_catalog(FIXTURE), load_manifest(MANIFEST))
        self.assertEqual(codes(issues, ERROR), [("DUPLICATE_STATEMENT", "SA-15(12)"), ("DUPLICATE_STATEMENT", "SA-15(13)")])

    def test_manifest_count_mismatch_is_a_global_error(self):
        path, cleanup = mutated_catalog(lambda c: c["groups"][0]["controls"].pop())  # drop AC-2 (+children)
        self.addCleanup(cleanup)
        issues = validate(load_catalog(path), load_manifest(MANIFEST))
        self.assertIn("COUNT_MISMATCH", [i.code for i in issues if i.control_id is None])

    def test_undefined_parameter_is_an_error(self):
        def mutate(cat):
            find_control(cat, "ac-1")["params"] = []
        path, cleanup = mutated_catalog(mutate)
        self.addCleanup(cleanup)
        issues = validate(load_catalog(path))
        self.assertTrue(any(i.code == "UNRESOLVED_PARAM" and i.control_id == "AC-1" for i in issues))

    def test_label_that_disagrees_with_id_is_an_error(self):
        def mutate(cat):
            for p in find_control(cat, "ac-2")["props"]:
                if p["name"] == "label" and "class" not in p:
                    p["value"] = "AC-9"
        path, cleanup = mutated_catalog(mutate)
        self.addCleanup(cleanup)
        self.assertIn(("ID_LABEL_MISMATCH", "AC-2"), codes(validate(load_catalog(path)), ERROR))

    def test_empty_active_statement_is_an_error(self):
        def mutate(cat):
            c = find_control(cat, "ac-2.1")
            c["parts"] = [p for p in c["parts"] if p["name"] != "statement"]
        path, cleanup = mutated_catalog(mutate)
        self.addCleanup(cleanup)
        self.assertIn(("EMPTY_STATEMENT", "AC-2(1)"), codes(validate(load_catalog(path)), ERROR))


if __name__ == "__main__":
    unittest.main()
