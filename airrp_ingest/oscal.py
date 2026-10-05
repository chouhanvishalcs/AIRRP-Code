"""NIST OSCAL catalog -> canonical SourceControl records.

Text is rendered deterministically and verbatim: parameter insertions become ``[label]`` or
``[choice | choice]``; nothing is paraphrased, trimmed to "key points" or defaulted.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from .model import (ERROR, WARNING, FrameworkInfo, Issue, Link, ParsedCatalog, Parameter, Reference,
                    SourceControl)
from .normalize import (canonical_control_id, content_hash, normalize_text, oscal_id_to_canonical,
                        sha256_file)

_INSERT = re.compile(r"\{\{\s*insert:\s*param,\s*([^\s}]+)\s*\}\}")
_PART_REF = re.compile(r"^(.+?)_(?:smt|gdn|obj)(?:\..*)?$")


def _walk(controls, parent=None):
    for ctl in controls:
        yield ctl, parent
        yield from _walk(ctl.get("controls", []), ctl)


def _prop(props, name, cls=None) -> Optional[str]:
    for p in props or []:
        if p.get("name") == name and p.get("class") == cls:
            return p.get("value")
    return None


class _Ctx:
    def __init__(self, params: dict, issues: list, control_id: str = ""):
        self.params, self.issues, self.control_id = params, issues, control_id

    def issue(self, severity, code, message):
        self.issues.append(Issue(severity, code, self.control_id or None, message))


def _render_param(pid: str, ctx: _Ctx, seen: tuple) -> str:
    param = ctx.params.get(pid)
    if param is None:
        ctx.issue(ERROR, "UNRESOLVED_PARAM", f"parameter '{pid}' is not defined in the catalog")
        return "{{ insert: param, %s }}" % pid
    if pid in seen:
        ctx.issue(ERROR, "PARAM_CYCLE", f"parameter '{pid}' references itself")
        return "[?]"
    if param.label:
        return f"[{_resolve(param.label, ctx, seen + (pid,))}]"
    if param.choices:
        return "[" + " | ".join(_resolve(c, ctx, seen + (pid,)) for c in param.choices) + "]"
    if param.aggregates:
        return " ".join(_render_param(a, ctx, seen + (pid,)) for a in param.aggregates)
    ctx.issue(ERROR, "EMPTY_PARAM", f"parameter '{pid}' has no label, choices or aggregates")
    return "[?]"


def _resolve(text: str, ctx: _Ctx, seen: tuple = ()) -> str:
    return _INSERT.sub(lambda m: _render_param(m.group(1), ctx, seen), text or "")


def _render_parts(parts, ctx: _Ctx) -> list:
    """Flatten nested parts to lines: '<label> <prose>' in document order."""
    lines = []
    for part in parts or []:
        label = _prop(part.get("props"), "label") or ""
        prose = _resolve(part.get("prose", ""), ctx).strip()
        line = f"{label} {prose}".strip()
        if line:
            lines.append(line)
        lines.extend(_render_parts(part.get("parts"), ctx))
    return lines


def _parse_param(p: dict) -> Parameter:
    select = p.get("select") or {}
    return Parameter(
        id=p["id"],
        label=p.get("label"),
        choices=tuple(select.get("choice", ())),
        how_many=select.get("how-many"),
        aggregates=tuple(q["value"] for q in p.get("props", []) if q.get("name") == "aggregates"),
    )


def _link_target(href: str, controls: dict, resources: set):
    target = href[1:] if href.startswith("#") else href
    if target in controls:
        return controls[target], "control"
    if target in resources:
        return target, "reference"
    m = _PART_REF.match(target)
    if m and m.group(1) in controls:
        return controls[m.group(1)], "part"
    return href, "unresolved"


def load_catalog(path: str, framework_code: str = "NIST-SP-800-53") -> ParsedCatalog:
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if "catalog" not in doc:
        raise ValueError("not an OSCAL catalog: missing top-level 'catalog'")
    cat = doc["catalog"]
    meta = cat.get("metadata", {})
    framework = FrameworkInfo(
        code=framework_code,
        version=str(meta.get("version", "")).strip(),
        title=meta.get("title", ""),
        oscal_version=str(meta.get("oscal-version", "")),
        last_modified=str(meta.get("last-modified", "")),
        source_sha256=sha256_file(path),
        groups=len(cat.get("groups", [])),
    )
    parsed = ParsedCatalog(framework=framework)
    if not framework.version:
        parsed.issues.append(Issue(ERROR, "MISSING_VERSION", None, "catalog metadata has no version"))

    resources = {r["uuid"]: r for r in cat.get("back-matter", {}).get("resources", [])}
    parsed.references = [
        Reference(uuid=u, title=r.get("title", ""), citation=(r.get("citation") or {}).get("text", ""),
                  href=((r.get("rlinks") or [{}])[0]).get("href", ""))
        for u, r in resources.items()
    ]

    flat = [(g["id"], c, p) for g in cat.get("groups", []) for c, p in _walk(g.get("controls", []))]
    params = {p["id"]: _parse_param(p) for _, c, _ in flat for p in c.get("params", [])}
    id_map, seen_ids = {}, {}
    for _, ctl, _ in flat:
        canon = oscal_id_to_canonical(ctl["id"])
        if canon is None:
            parsed.issues.append(Issue(ERROR, "BAD_ID", ctl["id"], f"cannot canonicalise OSCAL id '{ctl['id']}'"))
            continue
        id_map[ctl["id"]] = canon
        seen_ids[ctl["id"]] = seen_ids.get(ctl["id"], 0) + 1
    for oid, n in seen_ids.items():
        if n > 1:
            parsed.issues.append(Issue(ERROR, "DUPLICATE_ID", id_map[oid], f"source id '{oid}' appears {n} times"))

    for group_id, ctl, parent in flat:
        if ctl["id"] not in id_map:
            continue
        canon = id_map[ctl["id"]]
        ctx = _Ctx(params, parsed.issues, canon)
        props = ctl.get("props", [])
        label = canonical_control_id(_prop(props, "label") or "")
        if label != canon:
            ctx.issue(ERROR, "ID_LABEL_MISMATCH", f"label '{_prop(props, 'label')}' does not match id '{ctl['id']}'")
        family = group_id.upper()
        if not canon.startswith(family + "-"):
            ctx.issue(ERROR, "FAMILY_MISMATCH", f"id '{canon}' is filed under group '{group_id}'")
        status = "withdrawn" if _prop(props, "status") == "withdrawn" else "active"
        kind = "enhancement" if parent is not None else "base"
        if (ctl.get("class") == "SP800-53-enhancement") != (kind == "enhancement"):
            ctx.issue(ERROR, "KIND_MISMATCH", f"class '{ctl.get('class')}' disagrees with nesting ({kind})")

        by_name = {}
        for part in ctl.get("parts", []):
            by_name.setdefault(part.get("name"), []).append(part)
        statement = "\n".join(_render_parts(by_name.get("statement"), ctx))
        guidance = "\n".join(_render_parts(by_name.get("guidance"), ctx))
        objectives = "\n".join(_render_parts(by_name.get("assessment-objective"), ctx))
        methods = tuple(
            (_prop(m.get("props"), "method") or "", "\n".join(_render_parts(m.get("parts"), ctx)))
            for m in by_name.get("assessment-method", [])
        )
        links = []
        for lk in ctl.get("links", []):
            target, lkind = _link_target(lk["href"], id_map, resources)
            if lkind == "unresolved":
                ctx.issue(WARNING, "DANGLING_LINK", f"link '{lk['href']}' ({lk['rel']}) does not resolve")
            links.append(Link(lk["rel"], target, lkind))

        control = SourceControl(
            framework_code=framework.code, framework_version=framework.version, control_id=canon,
            oscal_id=ctl["id"], parent_id=id_map.get(parent["id"]) if parent else None, kind=kind,
            family=family, title=normalize_text(ctl.get("title", "")), status=status,
            statement=statement, guidance=guidance,
            parameters=tuple(_parse_param(p) for p in ctl.get("params", [])),
            assessment_objectives=objectives, assessment_methods=methods, links=tuple(links),
            implementation_level=_prop(props, "implementation-level"),
            source_ref=f"{framework.code}@{framework.version}#{ctl['id']}",
        )
        parsed.controls.append(control)
    return parsed
