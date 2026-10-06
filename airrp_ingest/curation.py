"""Human-reviewed curation of a specific source version: waivers and source corrections.

* waiver      - acknowledges one issue code on one control (the issue stays recorded, it just stops blocking).
* correction  - replaces the text of one field of one control *where the published text is wrong*. The published
                text is never edited: it stays in the immutable mirror, the correction is recorded beside it with who
                decided, why and from what source, and the pair is what the rest of the system reads as the control
                (the "effective" control). A correction is written against the hash of the published value it
                replaces, so a source that later changes under it fails loudly instead of silently winning.

Everything here is a pure function of its inputs: ``resolve_corrections`` decides what each entry means (new, changed,
unchanged, withdrawn, stale, adopted upstream, ...) and ``effective_control`` computes the corrected control. Storage and
the plan/apply pipeline live elsewhere.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Optional

from .model import ERROR, WARNING, Correction, Issue, SourceControl
from .normalize import content_hash, normalize_text, obligation_hash, sha256_hex

OVERRIDABLE = ("title", "statement", "guidance")
CORRECTABLE = OVERRIDABLE  # the fields a correction may replace
_REQ_WAIVER = ("framework_version", "control_id", "code", "reason", "reviewer", "reviewed_at")
_REQ_CORRECTION = ("framework_version", "control_id", "field", "value", "source_value_sha256", "problem", "citation",
                   "reviewer", "reviewed_at")
_REQ_RETIRED = ("reviewer", "reviewed_at", "reason")
_HASH = re.compile(r"^[0-9a-f]{64}$")


@dataclass
class Curation:
    waivers: list = field(default_factory=list)
    corrections: list = field(default_factory=list)

    @property
    def overrides(self) -> list:  # the name this list had before the correction layer existed
        return self.corrections


def field_hash(value: str) -> str:
    return sha256_hex(normalize_text(value))


def _missing(entry: dict, required) -> list:
    return [k for k in required if not str(entry.get(k, "")).strip()]


def load_curation(path: Optional[str]) -> Curation:
    if not path:
        return Curation()
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if "corrections" in doc and "overrides" in doc and doc["overrides"]:
        raise ValueError("a curation file has 'corrections' (current) or 'overrides' (the old name), not both")
    cur = Curation(waivers=doc.get("waivers", []), corrections=doc.get("corrections", doc.get("overrides", [])))
    for w in cur.waivers:
        missing = _missing(w, _REQ_WAIVER)
        if missing:
            raise ValueError(f"waiver {w.get('control_id')!r} is missing {missing}")
    seen = set()
    for c in cur.corrections:
        where = f"correction of {c.get('control_id')!r} {c.get('field')!r}"
        missing = _missing(c, _REQ_CORRECTION)
        if missing:
            hint = " ('problem' is new: say what is wrong with the published text)" if missing == ["problem"] else ""
            raise ValueError(f"{where} is missing {missing}{hint}")
        if c["field"] not in CORRECTABLE:
            raise ValueError(f"{where}: field {c['field']!r} cannot be corrected (allowed: {CORRECTABLE})")
        if not _HASH.match(str(c["source_value_sha256"])):
            raise ValueError(f"{where}: source_value_sha256 must be a 64-character lowercase hex sha256")
        if "retired" in c:
            retired = c["retired"]
            if not isinstance(retired, dict) or _missing(retired, _REQ_RETIRED):
                raise ValueError(f"{where}: 'retired' needs {list(_REQ_RETIRED)}")
        key = (c["framework_version"], c["control_id"], c["field"])
        if key in seen:
            raise ValueError(f"{where} appears twice for version {c['framework_version']}")
        seen.add(key)
    return cur


# ---------------------------------------------------------------------------------------------------------------------
# effective control

def _stored_value(field_name: str, value: str) -> str:
    """The form a correction's text is stored (and read back) in. Titles are held in the comparison form the importer
    already gives published titles; statement and guidance keep their line structure."""
    return normalize_text(value) if field_name == "title" else value.strip()


def clause_problem(c: SourceControl) -> Optional[str]:
    """Why a statement correction cannot be applied cleanly to ``c`` (None when it can)."""
    if len(c.clauses) > 1:
        return (f"the statement has {len(c.clauses)} addressable clauses; one replacement text would leave the clauses "
                "and the statement saying different things")
    if len(c.clauses) == 1 and c.clauses[0].label:
        return "the statement is a single labelled clause; the label cannot be carried through a replacement"
    return None


def effective_control(c: SourceControl, corrections) -> SourceControl:
    """``c`` with the given *in-force* corrections applied and its hashes recomputed. With none, ``c`` is returned as is."""
    corrections = [x for x in corrections if x.action == "set"]
    if not corrections:
        return c
    changes = {}
    for x in sorted(corrections, key=lambda x: CORRECTABLE.index(x.field)):
        changes[x.field] = _stored_value(x.field, x.value)
        if x.field == "statement":
            problem = clause_problem(c)
            if problem:
                raise ValueError(f"{c.control_id}: {problem}")
            if c.clauses:
                changes["clauses"] = (replace(c.clauses[0], text=changes["statement"]),)
    out = replace(c, **changes,
                  overrides_applied=tuple((x.field, x.citation) for x in corrections), corrections=tuple(corrections))
    return replace(out, content_hash=content_hash(out), obligation_hash=obligation_hash(out))


# ---------------------------------------------------------------------------------------------------------------------
# what each entry means

@dataclass
class Resolution:
    effective: list = field(default_factory=list)  # every control, corrections applied; hashes recomputed
    to_record: list = field(default_factory=list)  # Correction rows this run would append (revision assigned)
    in_force: dict = field(default_factory=dict)  # control_id -> [Correction]
    issues: list = field(default_factory=list)
    notes: list = field(default_factory=list)  # [{control_id, field, state, detail}] for display


def _same(a: Correction, entry: dict, stored_value: str) -> bool:
    return (a.value == stored_value and a.source_value_sha256 == entry["source_value_sha256"]
            and (a.problem, a.citation, a.reviewer, a.reviewed_at) ==
            tuple(str(entry[k]).strip() for k in ("problem", "citation", "reviewer", "reviewed_at")))


def _unreferenced_parameters(c: SourceControl, value: str) -> bool:
    """True when the control defines parameters and the replacement text mentions none of them."""
    labelled = [normalize_text(p.label).lower() for p in c.parameters if p.label]
    if not labelled:
        return False
    text = normalize_text(value).lower()
    return not any(f"[{label}]" in text or label in text for label in labelled)


def resolve_corrections(published: list, stored: dict, curation: Curation, version: str) -> Resolution:
    """Decide what every correction means for this run.

    ``published``  the controls exactly as parsed from the source (hashes of the *published* content).
    ``stored``     {(control_id, field): latest recorded Correction (action set or retire)} for this framework version.
    Returns the effective controls and the Correction rows to append. Issues use the ordinary Issue type; an ERROR with a
    control id keeps that control out of the repository, an ERROR without one blocks the run.
    """
    res = Resolution()
    by_id = {c.control_id: c for c in published}
    desired = {}  # (control_id, field) -> (Correction, origin)
    for key, cur in stored.items():
        if cur.action == "set":
            desired[key] = (cur, "stored")

    def note(cid, fld, state, detail=""):
        res.notes.append({"control_id": cid, "field": fld, "state": state, "detail": detail})

    for e in curation.corrections:
        if e["framework_version"] != version:
            continue
        cid, fld = e["control_id"], e["field"]
        key = (cid, fld)
        c = by_id.get(cid)
        if c is None:
            res.issues.append(Issue(ERROR, "CORRECTION_TARGET_MISSING", None,
                                    f"correction targets {cid}, which is not in this catalog ({version}); fix or remove the entry"))
            note(cid, fld, "target_missing")
            continue
        prior = stored.get(key)
        entry_hash = field_hash(getattr(c, fld))

        if "retired" in e:
            if prior is not None and prior.action == "set":
                r = e["retired"]
                res.to_record.append(Correction(
                    framework_code=c.framework_code, framework_version=version, control_id=cid, field=fld, value="",
                    source_value_sha256=prior.source_value_sha256, problem=str(r["reason"]).strip(), citation="",
                    reviewer=str(r["reviewer"]).strip(), reviewed_at=str(r["reviewed_at"]).strip(),
                    revision=prior.revision + 1, action="retire"))
                desired.pop(key, None)
                note(cid, fld, "retire", f"revision {prior.revision + 1}: the published {fld} is in force again")
            else:
                desired.pop(key, None)
                note(cid, fld, "retired_already", "nothing is in force for this field")
            continue

        value = _stored_value(fld, str(e["value"]))
        if e["source_value_sha256"] != entry_hash:
            if field_hash(value) == entry_hash:
                res.issues.append(Issue(WARNING, "CORRECTION_ADOPTED_UPSTREAM", cid,
                                        f"the published {fld} now already reads as the corrected text; the correction is not applied"
                                        " (retire it so the record says so)"))
                desired.pop(key, None)
                note(cid, fld, "adopted_upstream")
            else:
                res.issues.append(Issue(ERROR, "CORRECTION_STALE", cid,
                                        f"the published {fld} is not the text this correction was written against; re-review it"))
                note(cid, fld, "stale")
            continue
        if field_hash(value) == entry_hash:
            res.issues.append(Issue(ERROR, "CORRECTION_NO_CHANGE", cid,
                                    f"the corrected {fld} reads exactly like the published {fld}"))
            note(cid, fld, "no_change")
            continue
        if fld == "statement" and clause_problem(c):
            res.issues.append(Issue(ERROR, "CORRECTION_CLAUSES", cid, f"cannot correct the statement: {clause_problem(c)}"))
            note(cid, fld, "clauses")
            continue

        proposed = Correction(
            framework_code=c.framework_code, framework_version=version, control_id=cid, field=fld, value=value, source_value_sha256=e["source_value_sha256"],
            problem=str(e["problem"]).strip(), citation=str(e["citation"]).strip(),
            reviewer=str(e["reviewer"]).strip(), reviewed_at=str(e["reviewed_at"]).strip(), action="set")
        if prior is not None and prior.action == "set" and _same(prior, e, value):
            desired[key] = (prior, "stored")
            note(cid, fld, "unchanged", f"revision {prior.revision}")
        else:
            revision = (prior.revision if prior else 0) + 1
            proposed = replace(proposed, revision=revision)
            desired[key] = (proposed, "file")
            res.to_record.append(proposed)
            note(cid, fld, "new" if prior is None else "new_revision",
                 f"revision {revision}" + (" (replaces a withdrawn correction)" if prior and prior.action == "retire" else
                                          " (replaces the one in force)" if prior else ""))
        if fld == "statement" and _unreferenced_parameters(c, value):
            res.issues.append(Issue(WARNING, "CORRECTION_PARAMETERS_UNREFERENCED", cid,
                                    f"the control defines {len(c.parameters)} parameter(s) and the corrected statement mentions none of them"))

    for key, (corr, origin) in list(desired.items()):
        if origin != "stored":
            continue
        cid, fld = key
        c = by_id.get(cid)
        if c is None:
            res.issues.append(Issue(WARNING, "CORRECTION_ORPHANED", None,
                                    f"a recorded correction of {cid} {fld} has no control in this catalog"))
            desired.pop(key)
            continue
        if corr.source_value_sha256 != field_hash(getattr(c, fld)):
            if not any(i.code == "CORRECTION_STALE" and i.control_id == cid for i in res.issues):
                res.issues.append(Issue(ERROR, "CORRECTION_STALE", cid,
                                        f"the published {fld} is not the text the recorded correction (revision "
                                        f"{corr.revision}) was written against"))
            desired.pop(key)

    for key, (corr, _) in desired.items():
        res.in_force.setdefault(key[0], []).append(corr)
    for c in published:
        mine = res.in_force.get(c.control_id, [])
        try:
            res.effective.append(effective_control(c, mine))
        except ValueError as exc:  # a stored correction that no longer fits (e.g. the clauses changed): visible, not silent
            res.issues.append(Issue(ERROR, "CORRECTION_CLAUSES", c.control_id, str(exc)))
            res.effective.append(c)
    return res


def waived(issue: Issue, curation: Curation, version: str) -> Optional[dict]:
    for w in curation.waivers:
        if (w["framework_version"] == version and w["control_id"] == issue.control_id
                and w["code"] == issue.code):
            return w
    return None
