"""plan -> (human reads the diff) -> apply, plus an independent verify pass."""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Optional

from .curation import Curation, apply_overrides, waived
from .model import ERROR, Issue, Link, Parameter, ParsedCatalog, SourceControl
from .normalize import content_hash
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
    blockers: list = field(default_factory=list)

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
        }


def build_plan(parsed: ParsedCatalog, repo, manifest: Optional[dict] = None,
               curation: Optional[Curation] = None) -> Plan:
    curation = curation or Curation()
    fw = parsed.framework
    controls, override_issues = apply_overrides(parsed.controls, curation, fw.version)
    controls = [replace(c, content_hash=content_hash(c)) for c in controls]
    plan = Plan(framework=fw)

    checked = ParsedCatalog(framework=fw, controls=controls, references=parsed.references, issues=parsed.issues)
    issues = validate(checked, manifest) + override_issues
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
    for c in controls:
        if c.control_id in plan.quarantined:
            continue
        if c.control_id not in stored:
            plan.to_add.append(c)
        elif stored[c.control_id] == c.content_hash:
            plan.unchanged.append(c.control_id)
        else:
            plan.conflicts.append((c.control_id, stored[c.control_id], c.content_hash))
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
        run_id = repo.start_run(actor=actor, source_path=source_path, source_sha256=fw.source_sha256,
                                framework_code=fw.code, framework_version=fw.version)
        repo.add_framework(fw)
        for c in plan.to_add:
            repo.add_requirement(c, run_id)
        items = [(i.severity, i.code, cid, i.message, "open")
                 for cid, issues in plan.quarantined.items() for i in issues]
        items += [(i.severity, i.code, i.control_id,
                   f"{i.message} [waived by {w['reviewer']} on {w['reviewed_at']}: {w['reason']}]", "waived")
                  for i, w in plan.waived]
        repo.sync_review_items(run_id, fw.code, fw.version, items)
        repo.audit(actor, "import.apply", "import_run", run_id, plan.summary())
        repo.finish_run(run_id, plan.summary())
    return run_id


def control_from_payload(payload: str) -> SourceControl:
    d = json.loads(payload)
    d["parameters"] = tuple(Parameter(id=p["id"], label=p["label"], choices=tuple(p["choices"]),
                                      how_many=p["how_many"], aggregates=tuple(p["aggregates"]))
                            for p in d["parameters"])
    d["links"] = tuple(Link(**l) for l in d["links"])
    d["assessment_methods"] = tuple(tuple(m) for m in d["assessment_methods"])
    d["overrides_applied"] = tuple(tuple(o) for o in d["overrides_applied"])
    return SourceControl(**d)


def verify_repository(repo, parsed: ParsedCatalog, curation: Optional[Curation] = None) -> list:
    """Independent audit: (1) stored rows re-hash to their stored hash, (2) they still equal the source file."""
    problems = []
    fw = parsed.framework
    plan = build_plan(parsed, repo, curation=curation)
    for cid, stored, new in plan.conflicts:
        problems.append(f"{cid}: stored content differs from source file")
    for row in repo.requirements(fw.code, fw.version, "active") + repo.requirements(fw.code, fw.version, "withdrawn"):
        c = control_from_payload(row["payload_json"])
        if content_hash(c) != row["content_hash"]:
            problems.append(f"{row['control_id']}: stored payload no longer matches its content hash")
        if (c.statement, c.title, c.guidance, c.status) != (row["statement"], row["title"], row["guidance"], row["status"]):
            problems.append(f"{row['control_id']}: columns disagree with payload")
    return problems
