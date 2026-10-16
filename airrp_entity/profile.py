"""Build the entity profile: crawl -> website candidates -> (registry) -> derived candidates -> resolved fields.

Resolution never invents a value: a field is "not_found" when nothing supports it, "inferred" when the best support
is indirect, "confirmed" when a direct statement (or the registry) supports it, and "conflict" when two direct
statements disagree. Every value keeps its evidence (URL, method, quoted text) so a person can check it.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

from . import __version__
from . import knowledge as kb
from .crawl import crawl
from .extract import SiteContext, extract_website
from .fetch import Fetcher
from .gleif import GleifClient, candidates as registry_candidates, lookup as registry_lookup
from .model import FIELDS, HIGH, MEDIUM, Candidate, Evidence, confidence

SCHEMA = "airrp-entity-profile/1"
MULTI = {k: m for k, _, m in FIELDS}
LABEL = {k: lab for k, lab, _ in FIELDS}
CAVEATS = [
    "Website statements are what the organisation says about itself. Confirm registration facts (number, legal form, "
    "status) in the official register of the jurisdiction before relying on them.",
    "Industry, regulated activity and market values inferred from keywords are proposals for a person to confirm.",
]


class _Group:
    def __init__(self, c: Candidate):
        self.members = [c]
        self.keys = {c.key, *c.aliases}
        self.contains = list(c.contains)

    def matches(self, c: Candidate) -> bool:
        if c.key in self.keys or any(a in self.keys for a in c.aliases):
            return True
        if self.contains and all(t in c.key for t in self.contains):
            return True
        return bool(c.contains) and any(all(t in k for t in c.contains) for k in self.keys)

    def add(self, c: Candidate):
        self.members.append(c)
        self.keys |= {c.key, *c.aliases}

    @property
    def value(self) -> str:
        return self.members[0].value

    def score(self) -> float:
        best = max(m.weight for m in self.members)
        methods = {(m.evidence.source, m.evidence.method) for m in self.members}
        bonus = 0.05 * min(len(methods) - 1, 2)
        if self.corroborated:
            bonus += 0.05
        return round(min(0.99, best + bonus), 2)

    @property
    def sources(self) -> set[str]:
        return {m.evidence.source for m in self.members}

    @property
    def corroborated(self) -> bool:
        """Registry agrees with what the website states (directly, or derived from website values)."""
        return "registry" in self.sources and bool(self.sources & {"website", "derived"})

    def as_dict(self) -> dict:
        s = self.score()
        evidence, seen = [], set()
        for m in sorted(self.members, key=lambda m: -m.weight):
            k = (m.evidence.url, m.evidence.method, m.evidence.snippet)
            if k not in seen:
                seen.add(k)
                evidence.append(m.evidence.as_dict())
        details = list(dict.fromkeys(d for m in self.members for d in m.details))
        also = list(dict.fromkeys(m.value for m in self.members if m.value != self.value))
        notes = list(dict.fromkeys(m.note for m in self.members if m.note))
        out = {"value": self.value, "confidence": confidence(s), "score": s, "corroborated": self.corroborated,
               "sources": sorted(self.sources), "evidence_count": len(self.members), "evidence": evidence[:6]}
        if also:
            out["also_seen_as"] = also[:6]
        if details:
            out["details"] = details[:12]
        if notes:
            out["notes"] = notes
        return out


def group_candidates(cands: list[Candidate]) -> list[_Group]:
    groups: list[_Group] = []
    for c in sorted(cands, key=lambda c: (-c.weight, c.evidence.source != "registry")):
        g = next((g for g in groups if g.matches(c)), None)
        if g:
            g.add(c)
        else:
            groups.append(_Group(c))
    return sorted(groups, key=lambda g: (-g.score(), -len(g.members), g.value))


def resolve_field(key: str, cands: list[Candidate]) -> dict:
    groups = group_candidates(cands)
    out = {"label": LABEL[key], "multi_valued": MULTI[key]}
    if not groups:
        return {**out, "status": "not_found", "value": [] if MULTI[key] else None, "confidence": None, "candidates": []}
    top = groups[0]
    if MULTI[key]:
        chosen = [g for g in groups if g.score() >= MEDIUM] or [top]
        status = "found" if top.score() >= MEDIUM else "weak"
        value = [g.value for g in chosen]
    else:
        highs = [g for g in groups if g.score() >= HIGH]
        status = "conflict" if len(highs) > 1 else "confirmed" if top.score() >= HIGH else "inferred"
        value = top.value
    return {**out, "status": status, "value": value, "confidence": confidence(top.score()),
            "candidates": [g.as_dict() for g in groups[:25]]}


def _derive(cands: list[Candidate]) -> list[Candidate]:
    """Values implied by other fields: legal form from the name's suffix, jurisdiction from the registered office."""
    def by(field_):
        return group_candidates([c for c in cands if c.field == field_])

    out = []
    jur_groups = by("registration_jurisdiction")
    jur = jur_groups[0].members[0].key.split(":", 1)[1] if jur_groups and jur_groups[0].score() >= MEDIUM else None
    for g in [g for g in by("registered_legal_name") if g.score() >= MEDIUM][:2]:
        for name in dict.fromkeys(m.value for m in g.members):
            form = kb.legal_form_of(name, jur)
            if form:
                single = len(form["countries"]) == 1
                fits = bool(jur) and jur[:2] in form["countries"]
                how = f"inferred from the legal-name suffix '{form['suffix']}'" + (
                    f" and jurisdiction {jur}" if (form["refined_by"] or fits) else "")
                out.append(Candidate("legal_entity_type", form["type"], "form:" + form["key"],
                                     0.6 if (form["refined_by"] or single or fits) else 0.5,
                                     Evidence("derived", g.members[0].evidence.url, how, name)))
                if single and not jur:
                    code = form["countries"][0]
                    out.append(Candidate("registration_jurisdiction", kb.country_name(code), "jur:" + code, 0.4,
                                         Evidence("derived", g.members[0].evidence.url,
                                                  f"legal form '{form['suffix']}' exists only in {code}", name)))
    for g in [g for g in by("registered_office") if g.score() >= MEDIUM][:1]:
        for value in dict.fromkeys(m.value for m in g.members):
            code = kb.country_in_text(value)
            if code:
                out.append(Candidate("registration_jurisdiction", kb.country_name(code), "jur:" + code, 0.6,
                                     Evidence("derived", g.members[0].evidence.url,
                                              "country of the registered office", value)))
                break
    return out


def build_profile(urls: list[str], *, fetcher: Fetcher | None = None, max_pages: int = 25, registry: bool = True,
                  extra_domains=(), renderer=None, lei: str | None = None, log=None, today: date | None = None,
                  gleif_api: str | None = None) -> dict:
    urls = [u if "://" in u else "https://" + u for u in urls]
    fetcher = fetcher or Fetcher()
    result = crawl(urls, fetcher, max_pages=max_pages, extra_domains=extra_domains, renderer=renderer, log=log)
    ctx = SiteContext(urls, result.site_keys, today or date.today())
    cands, identifiers = extract_website(result.pages, ctx)
    notes: list[str] = []
    registry_summary = None

    if not result.pages:
        notes.append("No page could be fetched (see 'skipped'): the site may block automated requests, need a "
                     "browser (--render), or be unreachable from this network.")
    elif sum(len(p.parsed.text) for p in result.pages) / len(result.pages) < 300 and not renderer:
        notes.append("The fetched pages contain very little text; the site may build its pages with JavaScript. "
                     "Re-run with --render (needs Playwright).")
    if result.pages and not any(p.kind in ("legal", "corporate", "regulatory") for p in result.pages):
        notes.append("No legal-notice or corporate-information page was found; pass its URL as an extra argument.")

    if registry:
        client = GleifClient(fetcher, **({"api": gleif_api} if gleif_api else {}))
        leis = [lei] if lei else []
        leis += [i.value for i, _ in identifiers if i.scheme == "LEI" and i.value not in leis]
        name_groups = [g for g in group_candidates([c for c in cands if c.field == "registered_legal_name"])
                       if g.score() >= MEDIUM][:3]
        names = list(dict.fromkeys(m.value for g in name_groups for m in g.members))[:4]
        jurs = [g.members[0].key.split(":", 1)[1] for g in
                group_candidates([c for c in cands if c.field == "registration_jurisdiction"]) if g.score() >= MEDIUM]
        rec, matched_by, reg_notes = registry_lookup(client, leis, names, jurs)
        notes += reg_notes
        if rec:
            reg_cands, registry_summary = registry_candidates(rec, client, matched_by)
            cands += reg_cands
        notes += client.errors

    cands += _derive(cands)
    fields = {k: resolve_field(k, [c for c in cands if c.field == k]) for k, _, _ in FIELDS}
    for k, f in fields.items():
        if f["status"] == "conflict":
            notes.append(f"{LABEL[k]}: sources disagree ({' vs '.join(c['value'] for c in f['candidates'][:3])}); "
                         "a person must decide.")
    return {
        "schema": SCHEMA,
        "tool": {"name": "airrp-entity", "version": __version__},
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input": {"urls": urls, "max_pages": max_pages, "registry": registry, "lei": lei},
        "summary": {k: fields[k]["value"] for k, _, _ in FIELDS},
        "fields": fields,
        "registry": registry_summary,
        "pages": [p.as_dict() for p in result.pages],
        "skipped": result.skipped,
        "documents_for_review": result.documents,
        "notes": list(dict.fromkeys(notes)) + CAVEATS,
    }


# ---------------------------------------------------------------- renderers
def _cell(v) -> str:
    if isinstance(v, list):
        v = "; ".join(v) if len(v) <= 12 else "; ".join(v[:12]) + f"; … (+{len(v) - 12} more)"
    return (v or "—").replace("|", "\\|").replace("\n", " ")


def to_markdown(profile: dict) -> str:
    f = profile["fields"]
    name = f["registered_legal_name"]["value"] or profile["input"]["urls"][0]
    sites = ", ".join(dict.fromkeys(urlsplit(p["url"]).netloc for p in profile["pages"])) or "no site"
    reg = profile["registry"]
    out = [f"# Entity profile: {name}", "",
           f"Generated {profile['generated_at']} by airrp-entity {profile['tool']['version']} from "
           f"{len(profile['pages'])} page(s) of {sites}.",
           f"Registry: GLEIF record [{reg['lei']}]({reg['record_url']}), matched by {reg['matched_by']}." if reg else
           "Registry: not used or no match (see notes).", "",
           "| Field | Value | Confidence | Status | Basis |", "|---|---|---|---|---|"]
    for key, label, _ in FIELDS:
        fd = f[key]
        basis = ""
        if fd["candidates"]:
            top = fd["candidates"][0]
            ev = top["evidence"][0]
            basis = f"{ev['method']} ([source]({ev['url']}))" + (" · registry ✓" if top["corroborated"] else "")
        out.append(f"| {label} | {_cell(fd['value'])} | {fd['confidence'] or '—'} | {fd['status']} | {_cell(basis)} |")
    out += ["", "## Evidence and alternatives", ""]
    for key, label, _ in FIELDS:
        fd = f[key]
        out.append(f"### {label} ({fd['status']})")
        if not fd["candidates"]:
            out += ["Nothing found on the pages fetched.", ""]
            continue
        for c in fd["candidates"][:8]:
            extra = f"; also: {', '.join(c['also_seen_as'])}" if c.get("also_seen_as") else ""
            out.append(f"- **{c['value']}** ({c['confidence']}, {' + '.join(c['sources'])}{extra})")
            for d in c.get("details", [])[:6]:
                out.append(f"  - {d}")
            for ev in c["evidence"][:3]:
                quote = f" — “{ev['snippet']}”" if ev["snippet"] else ""
                out.append(f"  - {ev['method']} — {ev['url']}{quote}")
        out.append("")
    if profile["documents_for_review"]:
        out += ["## Documents to review (not parsed)", ""]
        out += [f"- [{d['text'] or d['url']}]({d['url']})" for d in profile["documents_for_review"]] + [""]
    out += ["## Pages fetched", ""] + [f"- {p['kind']}: [{p['title'] or p['url']}]({p['url']})" for p in profile["pages"]]
    if profile["skipped"]:
        out += ["", "## Skipped", ""] + [f"- {s['url']}: {s['reason']}" for s in profile["skipped"]]
    out += ["", "## Notes", ""] + [f"- {n}" for n in profile["notes"]]
    return "\n".join(out) + "\n"


CSV_COLUMNS = ["input_url"] + [c for k, _, _ in FIELDS for c in (k, f"{k}_confidence")] + ["gleif_lei", "pages_fetched"]


def to_csv_row(profile: dict) -> dict:
    row = {"input_url": " ".join(profile["input"]["urls"]), "gleif_lei": (profile["registry"] or {}).get("lei", ""),
           "pages_fetched": len(profile["pages"])}
    for k, _, _ in FIELDS:
        v = profile["fields"][k]["value"]
        row[k] = "; ".join(v) if isinstance(v, list) else (v or "")
        row[f"{k}_confidence"] = profile["fields"][k]["confidence"] or ""
    return row


def to_csv(profiles: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
    w.writeheader()
    for p in profiles:
        w.writerow(to_csv_row(p))
    return buf.getvalue()
