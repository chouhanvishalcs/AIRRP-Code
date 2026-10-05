"""Deterministic normalisation helpers. Nothing here ever rewrites source text that is stored."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Optional

_WS = re.compile(r"\s+")
_LABEL = re.compile(r"^([A-Za-z]{2})-0*(\d{1,3})(?:\(0*(\d{1,3})\))?$")
_OSCAL_ID = re.compile(r"^([a-z]{2})-(\d{1,3})(?:\.(\d{1,3}))?$")


def normalize_text(value: Optional[str]) -> str:
    """Comparison form: NFC, NBSP/zero-width removed, whitespace collapsed. Used for hashing and matching only."""
    text = unicodedata.normalize("NFC", value or "")
    text = text.replace(" ", " ").replace("​", "")
    return _WS.sub(" ", text).strip()


def canonical_control_id(label: str) -> Optional[str]:
    """'AC-02(10)' / 'ac-2(10)' -> 'AC-2(10)'. Returns None when the label is not a control id."""
    m = _LABEL.match((label or "").strip())
    if not m:
        return None
    base = f"{m.group(1).upper()}-{int(m.group(2))}"
    return f"{base}({int(m.group(3))})" if m.group(3) else base


def oscal_id_to_canonical(oscal_id: str) -> Optional[str]:
    """'ac-2.10' -> 'AC-2(10)'."""
    m = _OSCAL_ID.match(oscal_id or "")
    if not m:
        return None
    base = f"{m.group(1).upper()}-{int(m.group(2))}"
    return f"{base}({int(m.group(3))})" if m.group(3) else base


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def master_canonical_key(name: str) -> str:
    """Case/punctuation-insensitive identity for a master control name (DB-unique)."""
    text = normalize_text(name).lower().replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", text))


def content_hash(control) -> str:
    """Hash of every semantic field of a SourceControl (never of provenance such as source_ref)."""
    payload = {
        "framework": [control.framework_code, control.framework_version],
        "id": control.control_id,
        "oscal_id": control.oscal_id,
        "parent": control.parent_id,
        "kind": control.kind,
        "family": control.family,
        "title": normalize_text(control.title),
        "status": control.status,
        "statement": normalize_text(control.statement),
        "guidance": normalize_text(control.guidance),
        "parameters": [
            [p.id, normalize_text(p.label), [normalize_text(c) for c in p.choices], p.how_many, list(p.aggregates)]
            for p in control.parameters
        ],
        "objectives": normalize_text(control.assessment_objectives),
        "methods": [[m, normalize_text(o)] for m, o in control.assessment_methods],
        "links": sorted([l.rel, l.target, l.kind] for l in control.links),
        "level": control.implementation_level,
        "clauses": [[c.ref, c.parent_ref, c.label, normalize_text(c.text), c.is_leaf] for c in control.clauses],
    }
    return sha256_hex(canonical_json(payload))


def obligation_hash(control) -> str:
    """Hash of what a mapping decision depends on: the obligation text, not commentary or cross-references.

    A change to guidance, related-control links or assessment procedures does not change it, so those edits
    never force a mapping to be re-reviewed; any change to title, statement or parameters does.
    """
    payload = {
        "kind": control.kind,
        "parent": control.parent_id,
        "status": control.status,
        "title": normalize_text(control.title),
        "statement": normalize_text(control.statement),
        "parameters": [
            [p.id, normalize_text(p.label), [normalize_text(c) for c in p.choices], p.how_many, list(p.aggregates)]
            for p in control.parameters
        ],
    }
    return sha256_hex(canonical_json(payload))
