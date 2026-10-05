"""Source reconciliation. Every check is deterministic; any unwaived ERROR keeps a control out of the repository."""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Optional

from .model import ERROR, WARNING, Issue, ParsedCatalog
from .normalize import normalize_text


def load_manifest(path: Optional[str]) -> Optional[dict]:
    if not path:
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def validate(parsed: ParsedCatalog, manifest: Optional[dict] = None) -> list:
    issues = list(parsed.issues)
    controls = parsed.controls
    by_id = {c.control_id: c for c in controls}

    for c in controls:
        if not c.title:
            issues.append(Issue(ERROR, "EMPTY_TITLE", c.control_id, "control has no title"))
        if c.kind == "enhancement":
            if c.parent_id is None or c.parent_id not in by_id:
                issues.append(Issue(ERROR, "ORPHAN_ENHANCEMENT", c.control_id, f"parent '{c.parent_id}' not in catalog"))
        if c.status == "active":
            if not normalize_text(c.statement):
                issues.append(Issue(ERROR, "EMPTY_STATEMENT", c.control_id, "active control has no statement text"))
            if "{{" in c.statement or "{{" in c.guidance:
                issues.append(Issue(ERROR, "UNRENDERED_PLACEHOLDER", c.control_id, "unrendered {{ }} placeholder left in text"))
        else:
            if not any(l.rel in ("incorporated-into", "moved-to") for l in c.links):
                issues.append(Issue(WARNING, "WITHDRAWN_WITHOUT_SUCCESSOR", c.control_id,
                                    "withdrawn control names no successor"))

    # Identical statement text under different ids means one of them is wrong (or needs a documented waiver).
    groups = defaultdict(list)
    for c in controls:
        if c.status == "active" and normalize_text(c.statement):
            groups[normalize_text(c.statement)].append(c.control_id)
    for ids in groups.values():
        if len(ids) > 1:
            for cid in ids:
                issues.append(Issue(ERROR, "DUPLICATE_STATEMENT", cid,
                                    f"statement text is identical to {sorted(set(ids) - {cid})}; verify against the official publication"))

    if manifest:
        issues.extend(_reconcile(parsed, manifest))
    return issues


def _reconcile(parsed: ParsedCatalog, m: dict) -> list:
    got = {
        "controls_total": len(parsed.controls),
        "active": sum(c.status == "active" for c in parsed.controls),
        "withdrawn": sum(c.status == "withdrawn" for c in parsed.controls),
        "enhancements": sum(c.kind == "enhancement" for c in parsed.controls),
        "groups": parsed.framework.groups,
    }
    out = []
    if m.get("framework_version") not in (None, parsed.framework.version):
        out.append(Issue(ERROR, "MANIFEST_VERSION", None,
                         f"manifest is for {m['framework_version']}, catalog is {parsed.framework.version}"))
    for key, want in m.get("expected", {}).items():
        if got.get(key) != want:
            out.append(Issue(ERROR, "COUNT_MISMATCH", None, f"{key}: catalog has {got.get(key)}, manifest expects {want}"))
    want_sha = m.get("source_sha256")
    if want_sha and want_sha != parsed.framework.source_sha256:
        out.append(Issue(WARNING, "SOURCE_HASH_DIFFERS", None,
                         "catalog file differs from the one the manifest was built from (counts still checked)"))
    want_ids = m.get("control_ids_sha256")
    if want_ids:
        from .normalize import sha256_hex
        have = sha256_hex("\n".join(sorted(c.control_id for c in parsed.controls)))
        if have != want_ids:
            out.append(Issue(ERROR, "ID_SET_MISMATCH", None, "the set of control ids differs from the manifest"))
    return out
