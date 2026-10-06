"""plan -> (human reads the diff) -> apply, plus an independent verify pass."""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Optional

from .curation import Curation, effective_control, resolve_corrections, waived
from .model import ERROR, Clause, Correction, Issue, Link, Parameter, ParsedCatalog, SourceControl
from .curation import field_hash
from .normalize import content_hash, obligation_hash
from .validate import validate


class PlanBlocked(Exception):
    """The run must not be applied (global error or source drift). Nothing is written."""


@dataclass
class Plan:
    framework: object
    to_add: list = field(default_factory=list)
    unchanged: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)  # (control_id, stored_hash, new_hash)
    quarantined: dict = field(default_factory=dict)  # control_id -> [Issue]
    issues: list = field(default_factory=list)  # every issue found, waived or not
    waived: list = field(default_factory=list)  # (Issue, waiver dict)
    corrections: list = field(default_factory=list)  # Correction rows to append: only for controls that will be in the mirror
    correction_notes: list = field(default_factory=list)  # [{control_id, field, state, detail}] what each entry means
    corrected: int = 0  # controls whose effective text differs from the published text, as of this run
    blockers: list = field(default_factory=list)
    added: int = 0  # rows actually inserted by apply_plan (may be fewer than to_add if another import won the race)

    @property
    def blocked(self) -> bool:
        return bool(self.blockers)

    def summary(self) -> dict:
        return {
            "framework": f"{self.framework.code}@{self.framework.version}",
            "to_add": len(self.to_add), "unchanged": len(self.unchanged), "conflicts": len(self.conflicts),
            "quarantined": len(self.quarantined), "waived": len(self.waived),
            "warnings": sum(1 for i in self.issues if i.severity != ERROR),
            "blockers": [b.message for b in self.blockers],
            **({"corrected": self.corrected, "corrections_to_record": len(self.corrections)}
               if self.corrected or self.corrections else {}),
        }


def build_plan(parsed: ParsedCatalog, repo, manifest: Optional[dict] = None,
               curation: Optional[Curation] = None) -> Plan:
    curation = curation or Curation()
    fw = parsed.framework
    # What is stored is the published text, hashed as published. What is *checked* is the effective text: the published
    # text with the corrections in force (recorded ones, plus whatever this run's curation file adds) laid over it.
    controls = [replace(c, content_hash=content_hash(c), obligation_hash=obligation_hash(c)) for c in parsed.controls]
    resolution = resolve_corrections(controls, repo.latest_corrections(fw.code, fw.version), curation, fw.version)
    effective = resolution.effective
    plan = Plan(framework=fw, correction_notes=resolution.notes)
    plan.corrected = sum(1 for c in effective if c.corrections)

    checked = ParsedCatalog(framework=fw, controls=effective, references=parsed.references, issues=parsed.issues)
    issues = validate(checked, manifest) + resolution.issues
    plan.issues = issues

    blocking = defaultdict(list)
    for issue in issues:
        w = waived(issue, curation, fw.version) if issue.control_id else None
        if w:
            plan.waived.append((issue, w))
        elif issue.severity == ERROR:
            if issue.control_id is None:
                plan.blockers.append(issue)
            else:
                blocking[issue.control_id].append(issue)

    # An enhancement is never loaded without its (verified) base control.
    changed = True
    while changed:
        changed = False
        for c in controls:
            if c.control_id not in blocking and c.parent_id in blocking:
                blocking[c.control_id].append(
                    Issue(ERROR, "PARENT_QUARANTINED", c.control_id, f"parent {c.parent_id} is quarantined"))
                changed = True
    plan.quarantined = dict(blocking)

    stored = repo.requirement_hashes(fw.code, fw.version)
    for c in controls:  # the *published* control: the mirror is verbatim
        if c.control_id in plan.quarantined:
            continue
        if c.control_id not in stored:
            plan.to_add.append(c)
        elif stored[c.control_id] == c.content_hash:
            plan.unchanged.append(c.control_id)
        else:
            plan.conflicts.append((c.control_id, stored[c.control_id], c.content_hash))
    # A correction is only written for a control that is, or this run makes, part of the mirror.
    recordable = {c.control_id for c in plan.to_add} | set(plan.unchanged)
    plan.corrections = [x for x in resolution.to_record if x.control_id in recordable]
    for n in plan.correction_notes:
        if n["state"] in ("new", "new_revision", "retire") and n["control_id"] not in recordable:
            n["detail"] += "; not recorded because the control is held back"
    if plan.conflicts:
        plan.blockers.append(Issue(ERROR, "SOURCE_CHANGED_UNDER_SAME_VERSION", None,
                                   f"{len(plan.conflicts)} control(s) differ from what was imported for "
                                   f"{fw.code}@{fw.version}; publish as a new version or investigate"))
    return plan


def apply_plan(plan: Plan, repo, actor: str, source_path: str = "") -> int:
    if plan.blocked:
        raise PlanBlocked("; ".join(i.message for i in plan.blockers))
    fw = plan.framework
    with repo.transaction():
        repo.lock_framework(fw.code, fw.version)
        # The plan was built before we held the lock: re-check against what is stored *now*, so two importers
        # racing on the same version end with one set of rows and no duplicates, never a half-applied mix.
        stored = repo.requirement_hashes(fw.code, fw.version)
        to_add = []
        for c in plan.to_add:
            if c.control_id not in stored:
                to_add.append(c)
            elif stored[c.control_id] != c.content_hash:
                raise PlanBlocked(f"{c.control_id} was changed by another import while this one was running")
        for x in plan.corrections:  # same race guard for the overlay: the plan numbered each revision from what it saw
            if repo.correction_revision(x.framework_code, x.framework_version, x.control_id, x.field) != x.revision - 1:
                raise PlanBlocked(f"the corrections of {x.control_id} {x.field} were changed by another import while this one was running")
        plan.added = len(to_add)
        run_id = repo.start_run(actor=actor, source_path=source_path, source_sha256=fw.source_sha256,
                                framework_code=fw.code, framework_version=fw.version)
        repo.add_framework(fw)
        for c in to_add:
            repo.add_requirement(c, run_id)
        for x in plan.corrections:
            repo.add_correction(x, run_id)
            repo.audit(actor, f"correction.{x.action}", "source_correction",
                       f"{x.framework_code}@{x.framework_version}/{x.control_id}/{x.field}/r{x.revision}",
                       {"reviewer": x.reviewer, "reviewed_at": x.reviewed_at, "problem": x.problem, "citation": x.citation,
                        "source_value_sha256": x.source_value_sha256, "value_sha256": field_hash(x.value) if x.value else None})
        items = [(i.severity, i.code, cid, i.message, "open")
                 for cid, issues in plan.quarantined.items() for i in issues]
        items += [(i.severity, i.code, i.control_id,
                   f"{i.message} [waived by {w['reviewer']} on {w['reviewed_at']}: {w['reason']}]", "waived")
                  for i, w in plan.waived]
        repo.sync_review_items(run_id, fw.code, fw.version, items)
        repo.audit(actor, "import.apply", "import_run", run_id, {**plan.summary(), "added": plan.added})
        repo.finish_run(run_id, {**plan.summary(), "added": plan.added})
    return run_id


def control_from_payload(payload: str) -> SourceControl:
    d = json.loads(payload)
    d["parameters"] = tuple(Parameter(id=p["id"], label=p["label"], choices=tuple(p["choices"]),
                                      how_many=p["how_many"], aggregates=tuple(p["aggregates"]))
                            for p in d["parameters"])
    d["links"] = tuple(Link(**l) for l in d["links"])
    d["clauses"] = tuple(Clause(**c) for c in d.get("clauses", []))
    d["assessment_methods"] = tuple(tuple(m) for m in d["assessment_methods"])
    d["overrides_applied"] = tuple(tuple(o) for o in d.get("overrides_applied", []))
    d["corrections"] = tuple(Correction(**x) for x in d.get("corrections", []))
    return SourceControl(**d)


def verify_repository(repo, parsed: ParsedCatalog, curation: Optional[Curation] = None) -> list:
    """Independent audit of what is stored.

    (1) the mirror still equals the source file and re-hashes to its stored hash, and holds published text only;
    (2) the corrections laid over it are an unbroken, still-applicable history;
    (3) the effective text the database serves equals the effective text computed here, and nothing stored is now invalid.
    """
    problems = []
    fw = parsed.framework
    plan = build_plan(parsed, repo, curation=curation)
    for cid, stored, new in plan.conflicts:
        problems.append(f"{cid}: stored content differs from source file")
    mirror = {}
    for row in repo.requirements(fw.code, fw.version, "active") + repo.requirements(fw.code, fw.version, "withdrawn"):
        c = control_from_payload(row["payload_json"])
        mirror[row["control_id"]] = (row, c)
        if content_hash(c) != row["content_hash"]:
            problems.append(f"{row['control_id']}: stored payload no longer matches its content hash")
        if (c.statement, c.title, c.guidance, c.status) != (row["statement"], row["title"], row["guidance"], row["status"]):
            problems.append(f"{row['control_id']}: columns disagree with payload")
        if c.overrides_applied or c.corrections:
            problems.append(f"{row['control_id']}: the stored source text carries a correction; the mirror must hold the "
                            "published text (an importer from before the correction layer applied overrides before storing)")

    history = {}
    for r in repo.correction_history(fw.code, fw.version):
        history.setdefault((r["control_id"], r["field"]), []).append(r)
    for (cid, fld), rows in history.items():
        if [r["revision"] for r in rows] != list(range(1, len(rows) + 1)):
            problems.append(f"{cid} {fld}: the correction revisions are not an unbroken 1..{len(rows)} sequence")
        if rows[0]["action"] != "set" or any(r["action"] == "retire" and rows[i]["action"] != "set"
                                              for i, r in enumerate(rows[1:])):
            problems.append(f"{cid} {fld}: a correction was withdrawn that was not in force")
        latest = rows[-1]
        if latest["action"] == "set" and cid in mirror:
            if latest["source_value_sha256"] != field_hash(getattr(mirror[cid][1], fld)):
                problems.append(f"{cid} {fld}: the correction in force was written against text that is not the stored published {fld}")

    in_force = repo.current_corrections(fw.code, fw.version)
    for row in repo.requirements_effective(fw.code, fw.version, "active") + repo.requirements_effective(fw.code, fw.version, "withdrawn"):
        cid = row["control_id"]
        want = effective_control(mirror[cid][1], in_force.get(cid, [])) if cid in mirror else None
        if want is not None and (want.title, want.statement, want.guidance) != (row["title"], row["statement"], row["guidance"]):
            problems.append(f"{cid}: the effective text served by the database differs from published text plus corrections")
    stored_ids = set(mirror)
    for cid, issues in plan.quarantined.items():
        if cid in stored_ids:
            problems.append(f"{cid}: stored, but with its corrections its text is now held back: "
                            + "; ".join(f"[{i.code}] {i.message}" for i in issues))
    return problems
