import json
import os
import shutil
import tempfile

HERE = os.path.dirname(__file__)
FIXTURE = os.path.join(HERE, "fixtures", "mini_catalog.json")
MANIFEST = os.path.join(HERE, "fixtures", "mini_manifest.json")


def mutated_catalog(mutate):
    """Write a modified copy of the fixture to a temp file and return its path."""
    with open(FIXTURE, encoding="utf-8") as fh:
        doc = json.load(fh)
    mutate(doc["catalog"])
    d = tempfile.mkdtemp()
    path = os.path.join(d, "catalog.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    return path, lambda: shutil.rmtree(d, ignore_errors=True)


def find_control(catalog, oscal_id):
    def walk(ctrls):
        for c in ctrls:
            if c["id"] == oscal_id:
                return c
            r = walk(c.get("controls", []))
            if r:
                return r
    for g in catalog["groups"]:
        r = walk(g["controls"])
        if r:
            return r
