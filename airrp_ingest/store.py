"""Repository port, a portable SQL implementation of it, and the SQLite adapter.

``Repository`` is the only thing the pipeline knows about storage. ``SqlRepository`` holds every domain rule as plain,
portable SQL; a dialect subclass (SQLite here, PostgreSQL in ``pg_store``) only supplies the connection, the DDL
(constraints and triggers are where the invariants live, so a bug in application code cannot create a duplicate,
rewrite source text or approve an unreviewed mapping) and a handful of engine-specific primitives.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Protocol

from .model import Correction, FrameworkInfo, SourceControl
from .normalize import canonical_json, master_canonical_key

RELATIONSHIPS = ("equivalent", "subset", "superset", "intersects")  # NISTIR 8477 set-theory relationships


class Repository(Protocol):
    def transaction(self): ...
    def lock_framework(self, code: str, version: str) -> None: ...
    def framework_hash(self, code: str, version: str): ...
    def requirement_hashes(self, code: str, version: str) -> dict: ...
    def add_framework(self, fw: FrameworkInfo) -> None: ...
    def add_requirement(self, c: SourceControl, run_id: int) -> None: ...
    def latest_corrections(self, code: str, version: str) -> dict: ...
    def correction_revision(self, code: str, version: str, control_id: str, field: str) -> int: ...
    def add_correction(self, x: Correction, run_id: int) -> None: ...
    def sync_review_items(self, run_id: int, code: str, version: str, items: list) -> None: ...
    def start_run(self, **kw) -> int: ...
    def finish_run(self, run_id: int, summary: dict) -> None: ...
    def audit(self, actor: str, action: str, entity: str, entity_id: str, detail: dict) -> None: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SQLITE_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS framework (
  code TEXT NOT NULL, version TEXT NOT NULL, title TEXT NOT NULL, oscal_version TEXT,
  last_modified TEXT, source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64), imported_at TEXT NOT NULL,
  PRIMARY KEY (code, version)
);

CREATE TABLE IF NOT EXISTS import_run (
  id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, actor TEXT NOT NULL, source_path TEXT,
  source_sha256 TEXT, framework_code TEXT, framework_version TEXT, summary_json TEXT
);

-- Immutable mirror of the authoritative source. Corrections never edit it; a new version is a new row set.
CREATE TABLE IF NOT EXISTS source_requirement (
  id INTEGER PRIMARY KEY,
  framework_code TEXT NOT NULL, framework_version TEXT NOT NULL, control_id TEXT NOT NULL,
  oscal_id TEXT NOT NULL, parent_id TEXT, kind TEXT NOT NULL CHECK (kind IN ('base','enhancement')),
  family TEXT NOT NULL, title TEXT NOT NULL CHECK (length(trim(title)) > 0),
  status TEXT NOT NULL CHECK (status IN ('active','withdrawn')),
  statement TEXT NOT NULL, guidance TEXT NOT NULL, payload_json TEXT NOT NULL,
  content_hash TEXT NOT NULL CHECK (length(content_hash) = 64), import_run_id INTEGER NOT NULL REFERENCES import_run(id),
  CHECK (status = 'withdrawn' OR length(trim(statement)) > 0),
  UNIQUE (framework_code, framework_version, control_id),
  FOREIGN KEY (framework_code, framework_version) REFERENCES framework(code, version)
);
CREATE TRIGGER IF NOT EXISTS source_requirement_no_update BEFORE UPDATE ON source_requirement
  BEGIN SELECT RAISE(ABORT, 'source_requirement is immutable'); END;
CREATE TRIGGER IF NOT EXISTS source_requirement_no_delete BEFORE DELETE ON source_requirement
  BEGIN SELECT RAISE(ABORT, 'source_requirement is immutable'); END;

-- A person's corrections of text the source published wrongly. Append-only: a changed correction is a new revision,
-- a withdrawn one is a 'retire' row. It never edits source_requirement; the pair is read through requirement_effective.
CREATE TABLE IF NOT EXISTS source_correction (
  id INTEGER PRIMARY KEY,
  framework_code TEXT NOT NULL, framework_version TEXT NOT NULL, control_id TEXT NOT NULL,
  field TEXT NOT NULL CHECK (field IN ('title','statement','guidance')),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  action TEXT NOT NULL CHECK (action IN ('set','retire')),
  value TEXT NOT NULL,
  source_value_sha256 TEXT NOT NULL CHECK (length(source_value_sha256) = 64),
  problem TEXT NOT NULL CHECK (length(trim(problem)) > 0),
  citation TEXT NOT NULL DEFAULT '',
  reviewer TEXT NOT NULL CHECK (length(trim(reviewer)) > 0),
  reviewed_at TEXT NOT NULL CHECK (length(trim(reviewed_at)) > 0),
  recorded_at TEXT NOT NULL, import_run_id INTEGER NOT NULL REFERENCES import_run(id),
  CHECK (action = 'retire' OR (length(trim(value)) > 0 AND length(trim(citation)) > 0)),
  CHECK (action = 'set' OR value = ''),
  UNIQUE (framework_code, framework_version, control_id, field, revision),
  FOREIGN KEY (framework_code, framework_version, control_id)
    REFERENCES source_requirement(framework_code, framework_version, control_id)
);
CREATE TRIGGER IF NOT EXISTS source_correction_no_update BEFORE UPDATE ON source_correction
  BEGIN SELECT RAISE(ABORT, 'source_correction is append-only'); END;
CREATE TRIGGER IF NOT EXISTS source_correction_no_delete BEFORE DELETE ON source_correction
  BEGIN SELECT RAISE(ABORT, 'source_correction is append-only'); END;
CREATE TRIGGER IF NOT EXISTS source_correction_chain BEFORE INSERT ON source_correction
  BEGIN
    SELECT RAISE(ABORT, 'a correction revision must directly follow the previous one')
      WHERE NEW.revision <> COALESCE((SELECT MAX(p.revision) FROM source_correction p
        WHERE p.framework_code = NEW.framework_code AND p.framework_version = NEW.framework_version
          AND p.control_id = NEW.control_id AND p.field = NEW.field), 0) + 1;
    SELECT RAISE(ABORT, 'only a correction that is in force can be retired')
      WHERE NEW.action = 'retire' AND COALESCE((SELECT p.action FROM source_correction p
        WHERE p.framework_code = NEW.framework_code AND p.framework_version = NEW.framework_version
          AND p.control_id = NEW.control_id AND p.field = NEW.field AND p.revision = NEW.revision - 1), 'retire') <> 'set';
  END;

CREATE TABLE IF NOT EXISTS master_control (
  id TEXT PRIMARY KEY, canonical_key TEXT NOT NULL UNIQUE, name TEXT NOT NULL CHECK (length(trim(name)) > 0),
  objective TEXT NOT NULL, description TEXT NOT NULL, domain TEXT NOT NULL, frequency TEXT NOT NULL,
  control_type TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '', test_procedure TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('draft','approved','deprecated')),
  created_by TEXT NOT NULL, created_at TEXT NOT NULL, approved_by TEXT, approved_at TEXT,
  source TEXT NOT NULL DEFAULT 'manual',
  quality_flags TEXT NOT NULL DEFAULT '[]',  -- JSON list; a master with open flags cannot be approved
  CHECK (status <> 'approved' OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)),
  CHECK (status <> 'approved' OR quality_flags = '[]')
);

CREATE TABLE IF NOT EXISTS mapping (
  id INTEGER PRIMARY KEY,
  requirement_id INTEGER NOT NULL REFERENCES source_requirement(id),
  master_control_id TEXT NOT NULL REFERENCES master_control(id),
  status TEXT NOT NULL CHECK (status IN ('suggested','approved','rejected')),
  relationship TEXT CHECK (relationship IN ('equivalent','subset','superset','intersects')),
  is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0,1)),
  rationale TEXT NOT NULL DEFAULT '', score REAL, evidence TEXT, reviewer TEXT, reviewed_at TEXT,
  UNIQUE (requirement_id, master_control_id),
  CHECK (status <> 'approved' OR (relationship IS NOT NULL AND reviewer IS NOT NULL AND length(trim(rationale)) > 0)),
  CHECK (is_primary = 0 OR status = 'approved')
);
CREATE UNIQUE INDEX IF NOT EXISTS one_primary_mapping ON mapping(requirement_id) WHERE is_primary = 1;
CREATE TRIGGER IF NOT EXISTS mapping_approve_guard BEFORE UPDATE OF status ON mapping
  WHEN NEW.status = 'approved' AND (
        (SELECT status FROM source_requirement WHERE id = NEW.requirement_id) <> 'active'
     OR (SELECT status FROM master_control WHERE id = NEW.master_control_id) <> 'approved')
  BEGIN SELECT RAISE(ABORT, 'approved mappings need an active requirement and an approved master control'); END;
CREATE TRIGGER IF NOT EXISTS mapping_insert_guard BEFORE INSERT ON mapping
  WHEN NEW.status = 'approved'
  BEGIN SELECT RAISE(ABORT, 'mappings are created as suggested and approved by a reviewer'); END;

-- One row per (framework version, control, issue code): re-imports update it, they never pile up duplicates.
CREATE TABLE IF NOT EXISTS review_item (
  id INTEGER PRIMARY KEY, import_run_id INTEGER NOT NULL REFERENCES import_run(id),
  framework_code TEXT NOT NULL, framework_version TEXT NOT NULL, severity TEXT NOT NULL,
  code TEXT NOT NULL, control_id TEXT NOT NULL, message TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('open','waived','resolved')),
  UNIQUE (framework_code, framework_version, control_id, code)
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  entity TEXT NOT NULL, entity_id TEXT NOT NULL, detail_json TEXT NOT NULL
);
"""


# The corrections in force, and the requirements as the rest of the system reads them (published text with the
# corrections in force laid over it). Written once in portable SQL; each dialect only supplies CREATE VIEW's spelling.
CORRECTION_VIEWS = (
    ("correction_current",
     "SELECT c.id, c.framework_code, c.framework_version, c.control_id, c.field, c.revision, c.value,"
     " c.source_value_sha256, c.problem, c.citation, c.reviewer, c.reviewed_at, c.recorded_at, c.import_run_id"
     " FROM source_correction c WHERE c.action = 'set' AND c.revision = ("
     "SELECT MAX(l.revision) FROM source_correction l WHERE l.framework_code = c.framework_code"
     " AND l.framework_version = c.framework_version AND l.control_id = c.control_id AND l.field = c.field)"),
    ("requirement_effective",
     "SELECT r.id, r.framework_code, r.framework_version, r.control_id, r.oscal_id, r.parent_id, r.kind, r.family,"
     " COALESCE(t.value, r.title) AS title, r.status, COALESCE(s.value, r.statement) AS statement,"
     " COALESCE(g.value, r.guidance) AS guidance, r.payload_json, r.content_hash AS source_content_hash,"
     " r.import_run_id, CASE WHEN t.id IS NULL AND s.id IS NULL AND g.id IS NULL THEN 0 ELSE 1 END AS corrected"
     " FROM source_requirement r"
     " LEFT JOIN correction_current t ON t.framework_code = r.framework_code AND t.framework_version = r.framework_version"
     " AND t.control_id = r.control_id AND t.field = 'title'"
     " LEFT JOIN correction_current s ON s.framework_code = r.framework_code AND s.framework_version = r.framework_version"
     " AND s.control_id = r.control_id AND s.field = 'statement'"
     " LEFT JOIN correction_current g ON g.framework_code = r.framework_code AND g.framework_version = r.framework_version"
     " AND g.control_id = r.control_id AND g.field = 'guidance'"),
)

SQLITE_SCHEMA += "".join(f"CREATE VIEW IF NOT EXISTS {n} AS {b};\n" for n, b in CORRECTION_VIEWS)


class Row(dict):
    """dict row that also answers ``row[0]`` like sqlite3.Row, so both engines return the same shape."""
    __slots__ = ()

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return dict.__getitem__(self, key)

    def __iter__(self):  # like sqlite3.Row: iterating yields values (use .keys() for names)
        return iter(list(self.values()))


class SqlRepository:
    """All domain rules, written once in portable SQL (``?`` placeholders; dialect subclasses translate)."""

    BEGIN = "BEGIN"
    IntegrityError: type = sqlite3.IntegrityError
    DatabaseError: type = sqlite3.DatabaseError

    def __init__(self, four_eyes: bool = True):
        self.four_eyes = four_eyes  # a master control cannot be approved by the person who created it
        self._depth = 0

    # -- dialect hooks ------------------------------------------------------------------------------
    def execute(self, sql: str, params=()):
        raise NotImplementedError

    def _insert_returning_id(self, sql: str, params=()) -> int:
        raise NotImplementedError

    def lock_framework(self, code: str, version: str) -> None:
        """Serialise concurrent imports of the same framework version (no-op where writers are already serialised)."""

    def fetchone(self, sql: str, params=()):
        return self.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params=()):
        return self.execute(sql, params).fetchall()

    @contextmanager
    def transaction(self):
        """All-or-nothing; nested use joins the outer transaction."""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self.execute(self.BEGIN)
        self._depth = 1
        try:
            yield
            self.execute("COMMIT")
        except BaseException:
            self.execute("ROLLBACK")
            raise
        finally:
            self._depth = 0

    # -- source layer ------------------------------------------------------------------------------
    def framework_hash(self, code, version):
        row = self.execute("SELECT source_sha256 FROM framework WHERE code=? AND version=?", (code, version)).fetchone()
        return row[0] if row else None

    def requirement_hashes(self, code, version):
        rows = self.execute(
            "SELECT control_id, content_hash FROM source_requirement WHERE framework_code=? AND framework_version=?",
            (code, version))
        return {r[0]: r[1] for r in rows}

    def add_framework(self, fw):
        self.execute(
            "INSERT INTO framework VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            (fw.code, fw.version, fw.title, fw.oscal_version, fw.last_modified, fw.source_sha256, _now()))

    def add_requirement(self, c, run_id):
        if c.overrides_applied or c.corrections:  # the mirror holds the published text, never a corrected control
            raise ValueError(f"{c.control_id}: a corrected control cannot be stored as source text; "
                             "record the correction with add_correction and store the published control")
        payload = asdict(c)
        del payload["corrections"]  # keeps the stored payload exactly as it was before the correction layer existed
        self.execute(
            "INSERT INTO source_requirement (framework_code, framework_version, control_id, oscal_id, parent_id, kind,"
            " family, title, status, statement, guidance, payload_json, content_hash, import_run_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (c.framework_code, c.framework_version, c.control_id, c.oscal_id, c.parent_id, c.kind, c.family, c.title,
             c.status, c.statement, c.guidance, canonical_json(payload), c.content_hash, run_id))

    def sync_review_items(self, run_id, code, version, items):
        """items: [(severity, code, control_id, message, status)]. Upsert current findings, resolve the rest."""
        seen = set()
        for severity, icode, control_id, message, status in items:
            seen.add((control_id, icode))
            self.execute(
                "INSERT INTO review_item (import_run_id, framework_code, framework_version, severity, code, control_id,"
                " message, status) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT (framework_code, framework_version, control_id, code) DO UPDATE SET"
                " import_run_id=excluded.import_run_id, severity=excluded.severity, message=excluded.message,"
                " status=excluded.status", (run_id, code, version, severity, icode, control_id, message, status))
        for row in self.execute(
                "SELECT id, control_id, code FROM review_item WHERE framework_code=? AND framework_version=?"
                " AND status <> 'resolved'", (code, version)).fetchall():
            if (row["control_id"], row["code"]) not in seen:
                self.execute("UPDATE review_item SET status='resolved', import_run_id=? WHERE id=?", (run_id, row["id"]))

    def start_run(self, actor, source_path, source_sha256, framework_code, framework_version):
        return self._insert_returning_id(
            "INSERT INTO import_run (started_at, actor, source_path, source_sha256, framework_code, framework_version)"
            " VALUES (?,?,?,?,?,?)", (_now(), actor, source_path, source_sha256, framework_code, framework_version))

    def finish_run(self, run_id, summary):
        self.execute("UPDATE import_run SET finished_at=?, summary_json=? WHERE id=?",
                          (_now(), canonical_json(summary), run_id))

    def audit(self, actor, action, entity, entity_id, detail):
        self.execute("INSERT INTO audit_log (ts, actor, action, entity, entity_id, detail_json) VALUES (?,?,?,?,?,?)",
                          (_now(), actor, action, entity, str(entity_id), canonical_json(detail)))

    def requirements(self, code, version, status="active"):
        return self.execute(
            "SELECT * FROM source_requirement WHERE framework_code=? AND framework_version=? AND status=? ORDER BY id",
            (code, version, status)).fetchall()

    def requirements_effective(self, code, version, status="active"):
        """The requirements as everything downstream reads them: published text with corrections in force laid over it.
        ``source_content_hash`` is the hash of the published text; ``corrected`` says whether anything was laid over it."""
        return self.execute(
            "SELECT * FROM requirement_effective WHERE framework_code=? AND framework_version=? AND status=? ORDER BY id",
            (code, version, status)).fetchall()

    # -- corrections -------------------------------------------------------------------------------
    @staticmethod
    def _correction(row) -> Correction:
        return Correction(framework_code=row["framework_code"], framework_version=row["framework_version"],
                          control_id=row["control_id"], field=row["field"],
                          value=row["value"], source_value_sha256=row["source_value_sha256"], problem=row["problem"],
                          citation=row["citation"], reviewer=row["reviewer"], reviewed_at=row["reviewed_at"],
                          revision=int(row["revision"]), action=row["action"])

    def correction_history(self, code, version, control_id=None) -> list:
        """Every revision ever recorded, oldest first (what was decided, by whom, and when it was recorded)."""
        sql = ("SELECT * FROM source_correction WHERE framework_code=? AND framework_version=?"
               + (" AND control_id=?" if control_id else "") + " ORDER BY control_id, field, revision")
        return [dict(r) for r in self.execute(sql, (code, version, *([control_id] if control_id else []))).fetchall()]

    def latest_corrections(self, code, version) -> dict:
        """{(control_id, field): the latest revision} - a 'retire' row counts: it is the latest word on that field."""
        out = {}
        for row in self.correction_history(code, version):  # ordered by revision, so the last row per key wins
            out[(row["control_id"], row["field"])] = self._correction(row)
        return out

    def current_corrections(self, code, version) -> dict:
        """{control_id: [Correction in force]} - what the effective controls are built from."""
        out = {}
        for (cid, _), x in self.latest_corrections(code, version).items():
            if x.action == "set":
                out.setdefault(cid, []).append(x)
        return out

    def correction_revision(self, code, version, control_id, field) -> int:
        row = self.execute(
            "SELECT MAX(revision) FROM source_correction WHERE framework_code=? AND framework_version=?"
            " AND control_id=? AND field=?", (code, version, control_id, field)).fetchone()
        return int(row[0] or 0)

    def add_correction(self, x, run_id):
        """Append one revision. The schema, not this method, enforces that it follows the previous one."""
        self.execute(
            "INSERT INTO source_correction (framework_code, framework_version, control_id, field, revision, action, value,"
            " source_value_sha256, problem, citation, reviewer, reviewed_at, recorded_at, import_run_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (x.framework_code, x.framework_version, x.control_id, x.field,
             x.revision, x.action, x.value, x.source_value_sha256, x.problem, x.citation, x.reviewer, x.reviewed_at,
             _now(), run_id))

    # -- master layer ------------------------------------------------------------------------------
    def master_controls(self, status=None):
        sql, args = "SELECT * FROM master_control", ()
        if status:
            sql, args = sql + " WHERE status=?", (status,)
        return self.execute(sql + " ORDER BY id", args).fetchall()

    def add_master_control(self, mc: dict, actor: str, flags=(), source: str = "manual"):
        """Insert as draft. Uniqueness of the canonical name is enforced by the schema."""
        key = master_canonical_key(mc["name"])
        with self.transaction():
            self.execute(
                "INSERT INTO master_control (id, canonical_key, name, objective, description, domain, frequency,"
                " control_type, evidence, test_procedure, status, created_by, created_at, source, quality_flags)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mc["id"], key, mc["name"], mc["objective"], mc["description"], mc["domain"], mc["frequency"],
                 mc["control_type"], mc.get("evidence", ""), mc.get("test_procedure", ""), "draft", actor, _now(),
                 source, json.dumps(sorted(flags))))
            self.audit(actor, "master.create", "master_control", mc["id"], {**mc, "source": source, "flags": sorted(flags)})

    def update_master_draft(self, master_id: str, changes: dict, flags: list, actor: str):
        """Edit a draft. ``flags`` is the full new flag list computed by the caller from the new values."""
        allowed = {"objective", "description", "domain", "frequency", "control_type", "evidence", "test_procedure"}
        if not changes or set(changes) - allowed:
            raise ValueError(f"changes must be a non-empty subset of {sorted(allowed)}")
        with self.transaction():
            sets = ", ".join(f"{k}=?" for k in changes)
            n = self.execute(f"UPDATE master_control SET {sets}, quality_flags=? WHERE id=? AND status='draft'",
                                  (*changes.values(), json.dumps(sorted(flags)), master_id)).rowcount
            if n != 1:
                raise ValueError(f"{master_id!r} is not a draft master control")
            self.audit(actor, "master.update", "master_control", master_id, {"changes": changes, "flags": sorted(flags)})

    def approve_master_control(self, master_id: str, actor: str):
        if not (actor and actor.strip()):
            raise ValueError("an approver is required")
        row = self.execute("SELECT created_by, quality_flags FROM master_control WHERE id=?", (master_id,)).fetchone()
        if row and json.loads(row["quality_flags"]):
            raise ValueError(f"cannot approve while quality flags are open: {json.loads(row['quality_flags'])}")
        if row and self.four_eyes and row["created_by"] == actor:
            raise ValueError("four-eyes rule: a different person must approve this master control (use --solo to disable)")
        with self.transaction():
            n = self.execute(
                "UPDATE master_control SET status='approved', approved_by=?, approved_at=? WHERE id=? AND status='draft'",
                (actor, _now(), master_id)).rowcount
            if n != 1:
                raise ValueError(f"master control {master_id!r} is not a draft")
            self.audit(actor, "master.approve", "master_control", master_id, {})

    # -- mapping layer -----------------------------------------------------------------------------
    def suggest_mapping(self, requirement_id, master_id, score, evidence):
        """Idempotent: an existing row (suggested/approved/rejected) is never overwritten."""
        cur = self.execute(
            "INSERT INTO mapping (requirement_id, master_control_id, status, score, evidence)"
            " VALUES (?,?,'suggested',?,?) ON CONFLICT DO NOTHING", (requirement_id, master_id, score, evidence))
        return cur.rowcount == 1

    def decide_mapping(self, mapping_id, decision, reviewer, relationship=None, rationale="", primary=False):
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be approved or rejected")
        if not (reviewer and reviewer.strip()):
            raise ValueError("a reviewer is required")
        if decision == "approved" and relationship not in RELATIONSHIPS:
            raise ValueError(f"relationship must be one of {RELATIONSHIPS}")
        if not rationale.strip():
            raise ValueError("a rationale is required")
        with self.transaction():
            n = self.execute(
                "UPDATE mapping SET status=?, relationship=?, rationale=?, is_primary=?, reviewer=?, reviewed_at=?"
                " WHERE id=? AND status='suggested'",
                (decision, relationship if decision == "approved" else None, rationale,
                 int(primary and decision == "approved"), reviewer, _now(), mapping_id)).rowcount
            if n != 1:
                raise ValueError(f"mapping {mapping_id} is not awaiting review")
            self.audit(reviewer, f"mapping.{decision}", "mapping", mapping_id,
                       {"relationship": relationship, "rationale": rationale, "primary": primary})

    def queue(self, query: str = "", limit: int = 25, offset: int = 0):
        like = f"%{query.lower()}%"
        return self.execute(
            "SELECT m.id, m.score, m.evidence, r.control_id, r.title, r.statement, r.corrected, c.id AS master_id,"
            " c.name AS master, c.objective, c.description, c.domain, c.frequency, c.control_type"
            " FROM mapping m JOIN requirement_effective r ON r.id=m.requirement_id"
            " JOIN master_control c ON c.id=m.master_control_id"
            " WHERE m.status='suggested' AND c.status='approved' AND (? = 1 OR lower(r.control_id||' '||r.title||' '||c.name) LIKE ?)"
            " ORDER BY (m.score IS NULL), m.score DESC, m.id LIMIT ? OFFSET ?", (int(not query), like, limit, offset)).fetchall()

    def unmapped(self, code: str, version: str, query: str = "", limit: int = 25, offset: int = 0):
        like = f"%{query.lower()}%"
        return self.execute(
            "SELECT r.id, r.control_id, r.title, r.statement, r.corrected FROM requirement_effective r"
            " WHERE r.framework_code=? AND r.framework_version=? AND r.status='active'"
            " AND NOT EXISTS (SELECT 1 FROM mapping m WHERE m.requirement_id=r.id AND m.status='approved')"
            " AND (? = 1 OR lower(r.control_id||' '||r.title||' '||r.statement) LIKE ?)"
            " ORDER BY r.id LIMIT ? OFFSET ?", (code, version, int(not query), like, limit, offset)).fetchall()

    def map_requirement(self, code, version, control_id, master_id, reviewer, relationship, rationale, primary=False):
        """Reviewer-initiated mapping (e.g. for requirements the suggester missed): suggest + decide atomically."""
        with self.transaction():
            row = self.execute(
                "SELECT id FROM source_requirement WHERE framework_code=? AND framework_version=? AND control_id=?",
                (code, version, control_id)).fetchone()
            if row is None:
                raise ValueError(f"unknown requirement {control_id}")
            self.suggest_mapping(row["id"], master_id, None, "manual")
            m = self.execute("SELECT id, status FROM mapping WHERE requirement_id=? AND master_control_id=?",
                                  (row["id"], master_id)).fetchone()
            if m["status"] != "suggested":
                raise ValueError(f"this requirement/master pair is already {m['status']}")
            self.decide_mapping(m["id"], "approved", reviewer, relationship, rationale, primary)

    def open_suggestions(self, ready_only: bool = True):
        """Suggestions a reviewer can act on now (their master control is approved) - or all of them."""
        return self.execute(
            "SELECT m.id, r.control_id, r.title AS requirement, c.id AS master_id, c.name AS master, m.score, m.evidence"
            " FROM mapping m JOIN requirement_effective r ON r.id=m.requirement_id"
            " JOIN master_control c ON c.id=m.master_control_id WHERE m.status='suggested'"
            " AND (? = 0 OR c.status='approved') ORDER BY (m.score IS NULL), m.score DESC, m.id",
            (int(ready_only),)).fetchall()

    def blocked_suggestions(self) -> int:
        return self.execute(
            "SELECT COUNT(*) FROM mapping m JOIN master_control c ON c.id=m.master_control_id"
            " WHERE m.status='suggested' AND c.status<>'approved'").fetchone()[0]

    def coverage(self, code, version):
        row = self.execute(
            "SELECT COUNT(*) total, SUM(CASE WHEN EXISTS(SELECT 1 FROM mapping m WHERE m.requirement_id=r.id AND m.status='approved') THEN 1 ELSE 0 END) mapped"
            " FROM source_requirement r WHERE framework_code=? AND framework_version=? AND status='active'",
            (code, version)).fetchone()
        return {"active_requirements": int(row["total"]), "with_approved_mapping": int(row["mapped"] or 0)}


class SqliteRepository(SqlRepository):
    BEGIN = "BEGIN IMMEDIATE"  # takes the write lock up front: concurrent importers queue instead of racing

    def __init__(self, path: str = ":memory:", four_eyes: bool = True, check_same_thread: bool = True):
        super().__init__(four_eyes)
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=check_same_thread, timeout=60)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SQLITE_SCHEMA)

    def execute(self, sql, params=()):
        return self.conn.execute(sql, tuple(params))

    def _insert_returning_id(self, sql, params=()):
        return self.execute(sql, params).lastrowid

    def close(self):
        self.conn.close()

    def _simulate_out_of_band_tampering(self, what="mirror"):
        """Test hook: what someone with DDL rights could do. The pipeline must still detect the damage."""
        if what == "corrections":
            self.execute("DROP TRIGGER source_correction_no_update")
            self.execute("DROP TRIGGER source_correction_chain")
        else:
            self.execute("DROP TRIGGER source_requirement_no_update")


def open_repository(spec: str, four_eyes: bool = True, **kw):
    """``postgresql://...`` -> PostgreSQL, anything else is a SQLite file path (or ':memory:')."""
    if spec.startswith(("postgres://", "postgresql://")):
        from .pg_store import PostgresRepository
        return PostgresRepository(spec, four_eyes=four_eyes, **kw)
    return SqliteRepository(spec, four_eyes=four_eyes, **kw)
