"""Repository port + SQLite reference adapter.

``Repository`` is the only thing the pipeline knows about storage. The AIRRP adapter implements the same
methods against the real system; the SQLite adapter proves the rules by putting them in the schema, so
a bug in application code cannot create a duplicate, rewrite source text or approve an unreviewed mapping.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Protocol

from .model import FrameworkInfo, SourceControl
from .normalize import canonical_json, master_canonical_key

RELATIONSHIPS = ("equivalent", "subset", "superset", "intersects")  # NISTIR 8477 set-theory relationships


class Repository(Protocol):
    def transaction(self): ...
    def framework_hash(self, code: str, version: str): ...
    def requirement_hashes(self, code: str, version: str) -> dict: ...
    def add_framework(self, fw: FrameworkInfo) -> None: ...
    def add_requirement(self, c: SourceControl, run_id: int) -> None: ...
    def sync_review_items(self, run_id: int, code: str, version: str, items: list) -> None: ...
    def start_run(self, **kw) -> int: ...
    def finish_run(self, run_id: int, summary: dict) -> None: ...
    def audit(self, actor: str, action: str, entity: str, entity_id: str, detail: dict) -> None: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
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

CREATE TABLE IF NOT EXISTS master_control (
  id TEXT PRIMARY KEY, canonical_key TEXT NOT NULL UNIQUE, name TEXT NOT NULL CHECK (length(trim(name)) > 0),
  objective TEXT NOT NULL, description TEXT NOT NULL, domain TEXT NOT NULL, frequency TEXT NOT NULL,
  control_type TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '', test_procedure TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('draft','approved','deprecated')),
  created_by TEXT NOT NULL, created_at TEXT NOT NULL, approved_by TEXT, approved_at TEXT,
  CHECK (status <> 'approved' OR (approved_by IS NOT NULL AND approved_at IS NOT NULL))
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


class SqliteRepository:
    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._depth = 0

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
        self.conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        finally:
            self._depth = 0

    # -- source layer ------------------------------------------------------------------------------
    def framework_hash(self, code, version):
        row = self.conn.execute("SELECT source_sha256 FROM framework WHERE code=? AND version=?", (code, version)).fetchone()
        return row[0] if row else None

    def requirement_hashes(self, code, version):
        rows = self.conn.execute(
            "SELECT control_id, content_hash FROM source_requirement WHERE framework_code=? AND framework_version=?",
            (code, version))
        return {r[0]: r[1] for r in rows}

    def add_framework(self, fw):
        self.conn.execute(
            "INSERT OR IGNORE INTO framework VALUES (?,?,?,?,?,?,?)",
            (fw.code, fw.version, fw.title, fw.oscal_version, fw.last_modified, fw.source_sha256, _now()))

    def add_requirement(self, c, run_id):
        payload = asdict(c)
        self.conn.execute(
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
            self.conn.execute(
                "INSERT INTO review_item (import_run_id, framework_code, framework_version, severity, code, control_id,"
                " message, status) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT (framework_code, framework_version, control_id, code) DO UPDATE SET"
                " import_run_id=excluded.import_run_id, severity=excluded.severity, message=excluded.message,"
                " status=excluded.status", (run_id, code, version, severity, icode, control_id, message, status))
        for row in self.conn.execute(
                "SELECT id, control_id, code FROM review_item WHERE framework_code=? AND framework_version=?"
                " AND status <> 'resolved'", (code, version)).fetchall():
            if (row["control_id"], row["code"]) not in seen:
                self.conn.execute("UPDATE review_item SET status='resolved', import_run_id=? WHERE id=?", (run_id, row["id"]))

    def start_run(self, actor, source_path, source_sha256, framework_code, framework_version):
        cur = self.conn.execute(
            "INSERT INTO import_run (started_at, actor, source_path, source_sha256, framework_code, framework_version)"
            " VALUES (?,?,?,?,?,?)", (_now(), actor, source_path, source_sha256, framework_code, framework_version))
        return cur.lastrowid

    def finish_run(self, run_id, summary):
        self.conn.execute("UPDATE import_run SET finished_at=?, summary_json=? WHERE id=?",
                          (_now(), canonical_json(summary), run_id))

    def audit(self, actor, action, entity, entity_id, detail):
        self.conn.execute("INSERT INTO audit_log (ts, actor, action, entity, entity_id, detail_json) VALUES (?,?,?,?,?,?)",
                          (_now(), actor, action, entity, str(entity_id), canonical_json(detail)))

    def requirements(self, code, version, status="active"):
        return self.conn.execute(
            "SELECT * FROM source_requirement WHERE framework_code=? AND framework_version=? AND status=? ORDER BY id",
            (code, version, status)).fetchall()

    # -- master layer ------------------------------------------------------------------------------
    def master_controls(self, status=None):
        sql, args = "SELECT * FROM master_control", ()
        if status:
            sql, args = sql + " WHERE status=?", (status,)
        return self.conn.execute(sql + " ORDER BY id", args).fetchall()

    def add_master_control(self, mc: dict, actor: str):
        """Insert as draft. Uniqueness of the canonical name is enforced by the schema."""
        key = master_canonical_key(mc["name"])
        with self.transaction():
            self.conn.execute(
                "INSERT INTO master_control (id, canonical_key, name, objective, description, domain, frequency,"
                " control_type, evidence, test_procedure, status, created_by, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mc["id"], key, mc["name"], mc["objective"], mc["description"], mc["domain"], mc["frequency"],
                 mc["control_type"], mc.get("evidence", ""), mc.get("test_procedure", ""), "draft", actor, _now()))
            self.audit(actor, "master.create", "master_control", mc["id"], mc)

    def approve_master_control(self, master_id: str, actor: str):
        with self.transaction():
            n = self.conn.execute(
                "UPDATE master_control SET status='approved', approved_by=?, approved_at=? WHERE id=? AND status='draft'",
                (actor, _now(), master_id)).rowcount
            if n != 1:
                raise ValueError(f"master control {master_id!r} is not a draft")
            self.audit(actor, "master.approve", "master_control", master_id, {})

    # -- mapping layer -----------------------------------------------------------------------------
    def suggest_mapping(self, requirement_id, master_id, score, evidence):
        """Idempotent: an existing row (suggested/approved/rejected) is never overwritten."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO mapping (requirement_id, master_control_id, status, score, evidence)"
            " VALUES (?,?,'suggested',?,?)", (requirement_id, master_id, score, evidence))
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
            n = self.conn.execute(
                "UPDATE mapping SET status=?, relationship=?, rationale=?, is_primary=?, reviewer=?, reviewed_at=?"
                " WHERE id=? AND status='suggested'",
                (decision, relationship if decision == "approved" else None, rationale,
                 int(primary and decision == "approved"), reviewer, _now(), mapping_id)).rowcount
            if n != 1:
                raise ValueError(f"mapping {mapping_id} is not awaiting review")
            self.audit(reviewer, f"mapping.{decision}", "mapping", mapping_id,
                       {"relationship": relationship, "rationale": rationale, "primary": primary})

    def open_suggestions(self):
        return self.conn.execute(
            "SELECT m.id, r.control_id, r.title AS requirement, c.id AS master_id, c.name AS master, m.score, m.evidence"
            " FROM mapping m JOIN source_requirement r ON r.id=m.requirement_id"
            " JOIN master_control c ON c.id=m.master_control_id WHERE m.status='suggested' ORDER BY m.score DESC, m.id").fetchall()

    def coverage(self, code, version):
        row = self.conn.execute(
            "SELECT COUNT(*) total, SUM(EXISTS(SELECT 1 FROM mapping m WHERE m.requirement_id=r.id AND m.status='approved')) mapped"
            " FROM source_requirement r WHERE framework_code=? AND framework_version=? AND status='active'",
            (code, version)).fetchone()
        return {"active_requirements": row["total"], "with_approved_mapping": row["mapped"] or 0}
