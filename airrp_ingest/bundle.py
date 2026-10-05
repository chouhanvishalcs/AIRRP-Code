"""Integration contract: a self-describing, idempotent JSON bundle any application can ingest.

Consumers upsert by natural key and use ``content_hash`` / ``bundle_sha256`` to skip unchanged data:
  requirement      -> ``code``
  master_control   -> ``id``
  mapping          -> (``requirement_code``, ``master_control_id``)
Only reviewed data is exported: active requirements, *approved* master controls, *approved* mappings.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .airrp_export import NIST_800_53, Profile, airrp_requirement, requirement_code
from .pipeline import control_from_payload
from .normalize import canonical_json, sha256_hex

BUNDLE_SCHEMA = "airrp-import-bundle/1"


def build_bundle(repo, framework_code: str, version: str, profile: Profile = NIST_800_53) -> dict:
    fw = repo.fetchone("SELECT * FROM framework WHERE code=? AND version=?", (framework_code, version))
    if fw is None:
        raise ValueError(f"{framework_code}@{version} has not been imported")
    reqs = []
    for row in repo.requirements(framework_code, version, "active"):
        c = control_from_payload(row["payload_json"])
        reqs.append({**airrp_requirement(c, profile), "control_id": c.control_id, "kind": c.kind,
                     "parent_control_id": c.parent_id, "content_hash": row["content_hash"]})
    masters = [{k: m[k] for k in ("id", "name", "objective", "description", "domain", "frequency", "control_type",
                                  "evidence", "test_procedure", "approved_by", "approved_at")}
               for m in repo.master_controls("approved")]
    mappings = [{
        "requirement_code": requirement_code(profile, r["control_id"]), "requirement_control_id": r["control_id"],
        "master_control_id": r["master_control_id"], "relationship": r["relationship"],
        "is_primary": bool(r["is_primary"]), "rationale": r["rationale"], "reviewer": r["reviewer"],
        "reviewed_at": r["reviewed_at"]}
        for r in repo.fetchall(
            "SELECT s.control_id, m.* FROM mapping m JOIN source_requirement s ON s.id=m.requirement_id"
            " WHERE m.status='approved' AND s.framework_code=? AND s.framework_version=?"
            " ORDER BY s.id, m.master_control_id", (framework_code, version))]
    body = {
        "schema": BUNDLE_SCHEMA,
        "framework": {"code": fw["code"], "version": fw["version"], "title": fw["title"],
                      "regulation_code": profile.regulation_code, "source_sha256": fw["source_sha256"]},
        "requirements": reqs, "master_controls": masters, "mappings": mappings,
    }
    body["bundle_sha256"] = sha256_hex(canonical_json(body))  # excludes generated_at on purpose: same data, same hash
    body["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return body


def write_bundle(bundle: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")


def bundle_hash(bundle: dict) -> str:
    """Recompute the content hash a consumer should check before applying a bundle."""
    body = {k: v for k, v in bundle.items() if k not in ("bundle_sha256", "generated_at")}
    return sha256_hex(canonical_json(body))
