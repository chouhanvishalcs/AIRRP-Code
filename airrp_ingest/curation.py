"""Human-reviewed corrections to a specific source version.

* waiver   - acknowledges one issue code on one control (the issue stays recorded, it just stops blocking).
* override - replaces the text of one field, guarded by the hash of the source value it was written
             against, so an upstream fix makes a stale override fail loudly instead of silently winning.
Both require a reviewer, a date and a reason/citation, and apply to exactly one framework version.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Optional

from .model import ERROR, Issue, SourceControl
from .normalize import normalize_text, sha256_hex

OVERRIDABLE = ("title", "statement", "guidance")
_REQ_WAIVER = ("framework_version", "control_id", "code", "reason", "reviewer", "reviewed_at")
_REQ_OVERRIDE = ("framework_version", "control_id", "field", "value", "source_value_sha256", "citation",
                 "reviewer", "reviewed_at")


@dataclass
class Curation:
    waivers: list = field(default_factory=list)
    overrides: list = field(default_factory=list)


def field_hash(value: str) -> str:
    return sha256_hex(normalize_text(value))


def load_curation(path: Optional[str]) -> Curation:
    if not path:
        return Curation()
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    cur = Curation(waivers=doc.get("waivers", []), overrides=doc.get("overrides", []))
    for entry, required, kind in [(w, _REQ_WAIVER, "waiver") for w in cur.waivers] + \
                                 [(o, _REQ_OVERRIDE, "override") for o in cur.overrides]:
        missing = [k for k in required if not str(entry.get(k, "")).strip()]
        if missing:
            raise ValueError(f"{kind} {entry.get('control_id')!r} is missing {missing}")
    for o in cur.overrides:
        if o["field"] not in OVERRIDABLE:
            raise ValueError(f"override of field {o['field']!r} is not allowed (allowed: {OVERRIDABLE})")
    return cur


def apply_overrides(controls: list, curation: Curation, version: str):
    """Return (controls, issues). A stale or unmatched override is an ERROR, never silently ignored."""
    index = {c.control_id: i for i, c in enumerate(controls)}
    out, issues = list(controls), []
    for o in curation.overrides:
        if o["framework_version"] != version:
            continue
        i = index.get(o["control_id"])
        if i is None:
            issues.append(Issue(ERROR, "OVERRIDE_TARGET_MISSING", o["control_id"], "override targets an unknown control"))
            continue
        current = getattr(out[i], o["field"])
        if field_hash(current) != o["source_value_sha256"]:
            issues.append(Issue(ERROR, "OVERRIDE_STALE", o["control_id"],
                                f"source {o['field']} changed since the override was reviewed; re-review it"))
            continue
        out[i] = replace(out[i], **{o["field"]: o["value"]},
                         overrides_applied=out[i].overrides_applied + ((o["field"], o["citation"]),))
    return out, issues


def waived(issue: Issue, curation: Curation, version: str) -> Optional[dict]:
    for w in curation.waivers:
        if (w["framework_version"] == version and w["control_id"] == issue.control_id
                and w["code"] == issue.code):
            return w
    return None
