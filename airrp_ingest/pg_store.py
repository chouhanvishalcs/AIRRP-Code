"""PostgreSQL adapter. Same domain rules as the SQLite adapter (they live in ``SqlRepository``); only the DDL,
connection handling and a few primitives differ. Needs ``pip install "psycopg[binary]"``.

Invariants are enforced by the database: constraints, partial unique indexes and plpgsql triggers.
"""
from __future__ import annotations

from .store import Row, SqlRepository

try:
    import psycopg
except ImportError as exc:  # keep the core dependency-free
    raise ImportError('PostgreSQL support needs: pip install "psycopg[binary]"') from exc

PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS framework (
  code TEXT NOT NULL, version TEXT NOT NULL, title TEXT NOT NULL, oscal_version TEXT,
  last_modified TEXT, source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64), imported_at TEXT NOT NULL,
  PRIMARY KEY (code, version)
);

CREATE TABLE IF NOT EXISTS import_run (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, actor TEXT NOT NULL,
  source_path TEXT, source_sha256 TEXT, framework_code TEXT, framework_version TEXT, summary_json TEXT
);

CREATE TABLE IF NOT EXISTS source_requirement (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  framework_code TEXT NOT NULL, framework_version TEXT NOT NULL, control_id TEXT NOT NULL,
  oscal_id TEXT NOT NULL, parent_id TEXT, kind TEXT NOT NULL CHECK (kind IN ('base','enhancement')),
  family TEXT NOT NULL, title TEXT NOT NULL CHECK (length(trim(title)) > 0),
  status TEXT NOT NULL CHECK (status IN ('active','withdrawn')),
  statement TEXT NOT NULL, guidance TEXT NOT NULL, payload_json TEXT NOT NULL,
  content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
  import_run_id BIGINT NOT NULL REFERENCES import_run(id),
  CHECK (status = 'withdrawn' OR length(trim(statement)) > 0),
  UNIQUE (framework_code, framework_version, control_id),
  FOREIGN KEY (framework_code, framework_version) REFERENCES framework(code, version)
);

CREATE OR REPLACE FUNCTION airrp_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'source_requirement is immutable'; END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'source_requirement_immutable'
                 AND tgrelid = 'source_requirement'::regclass) THEN
    CREATE TRIGGER source_requirement_immutable BEFORE UPDATE OR DELETE ON source_requirement
      FOR EACH ROW EXECUTE FUNCTION airrp_immutable();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'source_requirement_no_truncate'
                 AND tgrelid = 'source_requirement'::regclass) THEN
    CREATE TRIGGER source_requirement_no_truncate BEFORE TRUNCATE ON source_requirement
      FOR EACH STATEMENT EXECUTE FUNCTION airrp_immutable();
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS master_control (
  id TEXT PRIMARY KEY, canonical_key TEXT NOT NULL UNIQUE, name TEXT NOT NULL CHECK (length(trim(name)) > 0),
  objective TEXT NOT NULL, description TEXT NOT NULL, domain TEXT NOT NULL, frequency TEXT NOT NULL,
  control_type TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '', test_procedure TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('draft','approved','deprecated')),
  created_by TEXT NOT NULL, created_at TEXT NOT NULL, approved_by TEXT, approved_at TEXT,
  source TEXT NOT NULL DEFAULT 'manual',
  quality_flags TEXT NOT NULL DEFAULT '[]',
  CHECK (status <> 'approved' OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)),
  CHECK (status <> 'approved' OR quality_flags = '[]')
);

CREATE TABLE IF NOT EXISTS mapping (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  requirement_id BIGINT NOT NULL REFERENCES source_requirement(id),
  master_control_id TEXT NOT NULL REFERENCES master_control(id),
  status TEXT NOT NULL CHECK (status IN ('suggested','approved','rejected')),
  relationship TEXT CHECK (relationship IN ('equivalent','subset','superset','intersects')),
  is_primary SMALLINT NOT NULL DEFAULT 0 CHECK (is_primary IN (0,1)),
  rationale TEXT NOT NULL DEFAULT '', score DOUBLE PRECISION, evidence TEXT, reviewer TEXT, reviewed_at TEXT,
  UNIQUE (requirement_id, master_control_id),
  CHECK (status <> 'approved' OR (relationship IS NOT NULL AND reviewer IS NOT NULL AND length(trim(rationale)) > 0)),
  CHECK (is_primary = 0 OR status = 'approved')
);
CREATE UNIQUE INDEX IF NOT EXISTS one_primary_mapping ON mapping(requirement_id) WHERE is_primary = 1;

CREATE OR REPLACE FUNCTION airrp_mapping_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' AND NEW.status = 'approved' THEN
    RAISE EXCEPTION 'mappings are created as suggested and approved by a reviewer';
  END IF;
  IF NEW.status = 'approved' AND (
        (SELECT status FROM source_requirement WHERE id = NEW.requirement_id) <> 'active'
     OR (SELECT status FROM master_control WHERE id = NEW.master_control_id) <> 'approved') THEN
    RAISE EXCEPTION 'approved mappings need an active requirement and an approved master control';
  END IF;
  RETURN NEW;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'mapping_guard' AND tgrelid = 'mapping'::regclass) THEN
    CREATE TRIGGER mapping_guard BEFORE INSERT OR UPDATE OF status ON mapping
      FOR EACH ROW EXECUTE FUNCTION airrp_mapping_guard();
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS review_item (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, import_run_id BIGINT NOT NULL REFERENCES import_run(id),
  framework_code TEXT NOT NULL, framework_version TEXT NOT NULL, severity TEXT NOT NULL,
  code TEXT NOT NULL, control_id TEXT NOT NULL, message TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('open','waived','resolved')),
  UNIQUE (framework_code, framework_version, control_id, code)
);

CREATE TABLE IF NOT EXISTS audit_log (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, ts TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  entity TEXT NOT NULL, entity_id TEXT NOT NULL, detail_json TEXT NOT NULL
);
"""


def _row_factory(cursor):
    cols = [d.name for d in cursor.description] if cursor.description else []
    return lambda values: Row(zip(cols, values))


class PostgresRepository(SqlRepository):
    IntegrityError = psycopg.IntegrityError
    DatabaseError = psycopg.DatabaseError

    def __init__(self, dsn: str, four_eyes: bool = True, schema: str = None, **_ignored):
        super().__init__(four_eyes)
        self.schema = schema
        if schema:  # create the schema on a throwaway connection, then bind the working one to it
            with psycopg.connect(dsn, autocommit=True) as boot:
                boot.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            self.conn = psycopg.connect(dsn, autocommit=True, options=f"-c search_path={schema}")
        else:
            self.conn = psycopg.connect(dsn, autocommit=True)
        self.conn.row_factory = _row_factory
        # serialise schema creation between processes that connect at the same moment
        self.conn.execute("SELECT pg_advisory_lock(727001)")
        try:
            self.conn.execute(PG_SCHEMA)
        finally:
            self.conn.execute("SELECT pg_advisory_unlock(727001)")

    def execute(self, sql, params=()):
        return self.conn.execute(sql.replace("%", "%%").replace("?", "%s"), tuple(params))

    def _insert_returning_id(self, sql, params=()):
        return self.execute(sql + " RETURNING id", params).fetchone()["id"]

    def lock_framework(self, code, version):
        """Transaction-scoped advisory lock: concurrent importers of one framework version queue up."""
        self.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (f"airrp-import:{code}:{version}",))

    def close(self):
        self.conn.close()

    def __del__(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def _simulate_out_of_band_tampering(self):
        """Test hook: what someone with DDL rights could do. The pipeline must still detect the damage."""
        self.execute("ALTER TABLE source_requirement DISABLE TRIGGER source_requirement_immutable")
