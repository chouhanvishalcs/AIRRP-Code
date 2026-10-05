import json
import os
import shutil
import sys
import tempfile
import unittest

from airrp_ingest.store import SqliteRepository

HERE = os.path.dirname(__file__)
FIXTURE = os.path.join(HERE, "fixtures", "mini_catalog.json")
MANIFEST = os.path.join(HERE, "fixtures", "mini_manifest.json")


class SqliteEngine:
    name = "sqlite"

    def new(self, four_eyes=True):
        path = os.path.join(tempfile.mkdtemp(), "t.db")
        return SqliteRepository(path, four_eyes=four_eyes, check_same_thread=False), path


class PostgresEngine:
    """One embedded PostgreSQL server per test run (pgserver), one fresh schema per repository."""
    name = "postgres"
    _server = None
    _counter = 0

    @classmethod
    def _uri(cls):
        try:
            import pgserver
            import psycopg  # noqa: F401
        except ImportError:
            raise unittest.SkipTest('pip install pgserver "psycopg[binary]" to run the PostgreSQL contract tests')
        if cls._server is None:
            import atexit
            cls._server = pgserver.get_server(tempfile.mkdtemp())
            atexit.register(cls._server.cleanup)
        return cls._server.get_uri()

    def new(self, four_eyes=True):
        from airrp_ingest.pg_store import PostgresRepository
        uri = self._uri()
        type(self)._counter += 1
        schema = f"t{os.getpid()}_{type(self)._counter}"
        repo = PostgresRepository(uri, four_eyes=four_eyes, schema=schema)
        sep = "&" if "?" in uri else "?"
        return repo, f"{uri}{sep}options=-csearch_path%3D{schema}"


ENGINES = {"sqlite": SqliteEngine(), "postgres": PostgresEngine()}


def for_each_engine(mixin):
    """Turn a mixin of test methods into one TestCase per storage engine (the adapter contract suite)."""
    module = sys.modules[mixin.__module__]
    for name, engine in ENGINES.items():
        cls = type(f"{mixin.__name__}_{name}", (mixin, unittest.TestCase), {"engine": engine})
        setattr(module, cls.__name__, cls)
    return mixin


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
