"""Reference implementation of the consumer side of docs/INTEGRATION.md.

It stands in for "your application's database": three tables with foreign keys, an idempotent bundle apply, and a log of
applied bundles. Works on any ``SqlRepository`` (SQLite or PostgreSQL). Your real consumer should follow the same rules:
verify the hash, upsert by natural key, never delete implicitly, fail hard (and roll back) on a dangling reference.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .bundle import BUNDLE_SCHEMA, bundle_hash

TARGET_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS app_requirement (
         code TEXT PRIMARY KEY, name TEXT NOT NULL, frequency TEXT NOT NULL, legal_text TEXT NOT NULL,
         legal_title TEXT NOT NULL, description TEXT NOT NULL, owner_function TEXT NOT NULL,
         obligation_type TEXT NOT NULL, regulation_code TEXT NOT NULL, source_reference TEXT NOT NULL,
         control_id TEXT NOT NULL, kind TEXT NOT NULL, parent_control_id TEXT, content_hash TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS app_master_control (
         id TEXT PRIMARY KEY, name TEXT NOT NULL, objective TEXT NOT NULL, description TEXT NOT NULL,
         domain TEXT NOT NULL, frequency TEXT NOT NULL, control_type TEXT NOT NULL, evidence TEXT NOT NULL,
         test_procedure TEXT NOT NULL, approved_by TEXT NOT NULL, approved_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS app_mapping (
         requirement_code TEXT NOT NULL REFERENCES app_requirement(code),
         master_control_id TEXT NOT NULL REFERENCES app_master_control(id),
         relationship TEXT NOT NULL, is_primary SMALLINT NOT NULL, rationale TEXT NOT NULL, reviewer TEXT NOT NULL,
         reviewed_at TEXT NOT NULL, PRIMARY KEY (requirement_code, master_control_id))""",
    """CREATE TABLE IF NOT EXISTS app_bundle_log (
         bundle_sha256 TEXT PRIMARY KEY, applied_at TEXT NOT NULL, counts_json TEXT NOT NULL)""",
]
_REQ = ("name", "frequency", "legalText", "legalTitle", "description", "ownerFunction", "obligationType",
        "regulationCode", "sourceReference", "control_id", "kind", "parent_control_id", "content_hash")
_REQ_COLS = ("name", "frequency", "legal_text", "legal_title", "description", "owner_function", "obligation_type",
             "regulation_code", "source_reference", "control_id", "kind", "parent_control_id", "content_hash")
_MC = ("name", "objective", "description", "domain", "frequency", "control_type", "evidence", "test_procedure",
       "approved_by", "approved_at")
_MAP = ("relationship", "is_primary", "rationale", "reviewer", "reviewed_at")


class BundleRejected(Exception):
    pass


def ensure_target_schema(repo) -> None:
    for ddl in TARGET_SCHEMA:
        repo.execute(ddl)


def _upsert(repo, table, key_cols, key, cols, values, counts):
    """Insert, or update only when something differs. Returns nothing; bumps counts['added'|'changed'|'unchanged']."""
    row = repo.fetchone(f"SELECT {', '.join(cols)} FROM {table} WHERE " + " AND ".join(f"{k}=?" for k in key_cols), key)
    if row is None:
        repo.execute(f"INSERT INTO {table} ({', '.join(key_cols + cols)}) VALUES ({', '.join('?' * (len(key_cols) + len(cols)))})",
                     (*key, *values))
        counts[f"{table}.added"] = counts.get(f"{table}.added", 0) + 1
    elif [row[c] for c in cols] != list(values):
        repo.execute(f"UPDATE {table} SET {', '.join(c + '=?' for c in cols)} WHERE " + " AND ".join(f"{k}=?" for k in key_cols),
                     (*values, *key))
        counts[f"{table}.changed"] = counts.get(f"{table}.changed", 0) + 1
    else:
        counts[f"{table}.unchanged"] = counts.get(f"{table}.unchanged", 0) + 1


def apply_bundle(repo, bundle: dict) -> dict:
    """Idempotently apply a bundle in one transaction. Returns the counts, or {'skipped': True} if already applied."""
    if bundle.get("schema") != BUNDLE_SCHEMA:
        raise BundleRejected(f"unsupported bundle schema {bundle.get('schema')!r}")
    if bundle_hash(bundle) != bundle.get("bundle_sha256"):
        raise BundleRejected("bundle_sha256 does not match the content (file altered or corrupted)")
    ensure_target_schema(repo)
    if repo.fetchone("SELECT 1 FROM app_bundle_log WHERE bundle_sha256=?", (bundle["bundle_sha256"],)):
        return {"skipped": True}
    counts: dict = {}
    with repo.transaction():
        for r in bundle["requirements"]:
            _upsert(repo, "app_requirement", ["code"], (r["code"],), list(_REQ_COLS), [r[k] for k in _REQ], counts)
        for m in bundle["master_controls"]:
            _upsert(repo, "app_master_control", ["id"], (m["id"],), list(_MC), [m[k] for k in _MC], counts)
        known_req = {r["code"] for r in bundle["requirements"]}
        for mp in bundle["mappings"]:
            if mp["requirement_code"] not in known_req and not repo.fetchone(
                    "SELECT 1 FROM app_requirement WHERE code=?", (mp["requirement_code"],)):
                raise BundleRejected(f"mapping references unknown requirement {mp['requirement_code']}")
            if not repo.fetchone("SELECT 1 FROM app_master_control WHERE id=?", (mp["master_control_id"],)):
                raise BundleRejected(f"mapping references unknown master control {mp['master_control_id']}")
            _upsert(repo, "app_mapping", ["requirement_code", "master_control_id"],
                    (mp["requirement_code"], mp["master_control_id"]), list(_MAP),
                    [int(mp[k]) if k == "is_primary" else mp[k] for k in _MAP], counts)
        repo.execute("INSERT INTO app_bundle_log VALUES (?,?,?)",
                     (bundle["bundle_sha256"], datetime.now(timezone.utc).isoformat(timespec="seconds"), json.dumps(counts)))
    return counts
