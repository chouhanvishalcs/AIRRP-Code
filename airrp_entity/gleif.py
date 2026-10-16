"""Optional corroboration from the public GLEIF LEI registry (https://api.gleif.org, no key required).

The website says what an organisation calls itself; a GLEIF record holds what was validated against its registration
authority (legal name, legal form, jurisdiction, addresses, status, registration number). A record is used only when it
is reached through an LEI published on the website or through an exact (normalised) legal-name match. Several equally
good matches are reported, never chosen between.
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote

from . import knowledge as kb
from .identifiers import find_identifiers, jp_corporate_number_ok, lei_ok, uk_company_number
from .model import Candidate, Evidence

API = "https://api.gleif.org/api/v1"
RECORD_URL = "https://search.gleif.org/#/record/{lei}"
NAME_TOKENS = {"corp": "corporation", "inc": "incorporated", "ltd": "limited", "co": "company", "the": "",
               "public limited company": "plc", "l l c": "llc", "kabushiki kaisha": "kk"}


def canonical_name(name: str) -> str:
    s = " " + kb.fold(name) + " "
    for a, b in sorted(NAME_TOKENS.items(), key=lambda x: -len(x[0])):
        s = s.replace(f" {a} ", f" {b} " if b else " ")
    return " ".join(s.split())


def record_names(rec: dict) -> list[str]:
    e = rec.get("attributes", {}).get("entity", {})
    names = [(e.get("legalName") or {}).get("name")]
    for k in ("otherNames", "transliteratedOtherNames"):
        names += [n.get("name") for n in (e.get(k) or [])]
    return [n for n in names if n]


class GleifClient:
    def __init__(self, fetcher, api: str = API):
        self.fetcher, self.api = fetcher, api.rstrip("/")
        self.errors: list[str] = []
        self._cache: dict[str, dict] = {}       # legal forms and registration authorities repeat across a group

    def _json(self, path: str):
        r = self.fetcher.get(self.api + path, accept="application/vnd.api+json, application/json", check_robots=False)
        if not r.ok:
            if r.status != 404:
                self.errors.append(f"GLEIF {path}: {r.error or r.status}")
            return None
        try:
            return json.loads(r.text())
        except ValueError:
            self.errors.append(f"GLEIF {path}: response is not JSON")
            return None

    def record(self, lei: str) -> dict | None:
        d = self._json(f"/lei-records/{quote(lei)}")
        return (d or {}).get("data")

    def search(self, name: str) -> list[dict]:
        """Records whose legal or other name equals `name` after normalisation."""
        want = canonical_name(name)
        for flt in ("entity.legalName", "fulltext"):
            d = self._json(f"/lei-records?filter%5B{flt}%5D={quote(name)}&page%5Bsize%5D=20")
            matches = [r for r in (d or {}).get("data") or [] if want in {canonical_name(n) for n in record_names(r)}]
            if matches:
                return matches
        return []

    def _attributes(self, path: str) -> dict:
        if path not in self._cache:
            self._cache[path] = ((self._json(path) or {}).get("data") or {}).get("attributes") or {}
        return self._cache[path]

    def legal_form_names(self, code: str) -> list[str]:
        attrs = self._attributes(f"/entity-legal-forms/{quote(code)}")
        out = []
        for n in attrs.get("names") or []:
            for k, v in n.items():
                if isinstance(v, str) and v and ("name" in k.lower() or "abbreviation" in k.lower()):
                    out.append(v)
        return list(dict.fromkeys(out))

    def group_entities(self, lei: str, limit: int = 1000) -> tuple[list[dict], int, str]:
        """LEI records of the entities consolidated under `lei` (ultimate children, else direct children), as
        reported to GLEIF: (records, total GLEIF reports, relation used)."""
        for relation in ("ultimate-children", "direct-children"):
            records, total, page = [], 0, 1
            while len(records) < limit:
                d = self._json(f"/lei-records/{quote(lei)}/{relation}?page%5Bnumber%5D={page}&page%5Bsize%5D=200") or {}
                data = d.get("data") or []
                records += data
                pagination = (d.get("meta") or {}).get("pagination") or {}
                total = pagination.get("total") or len(records)
                if not data or page >= (pagination.get("lastPage") or page):
                    break
                page += 1
            if records:
                return records[:limit], total, relation
        return [], 0, ""

    def registration_authority(self, code: str) -> str | None:
        attrs = self._attributes(f"/registration-authorities/{quote(code)}")
        return attrs.get("internationalOrganizationName") or attrs.get("internationalName") or attrs.get("localName") \
            or attrs.get("localOrganizationName")


def lookup(client: GleifClient, leis: list[str], names: list[str], jurisdictions: list[str]) -> tuple[dict | None, str, list[str]]:
    """(record, how it was matched, notes)."""
    notes = []
    for lei in leis:
        rec = client.record(lei)
        if rec:
            return rec, f"LEI {lei} published on the website", notes
        notes.append(f"LEI {lei} found on the website is not in the GLEIF registry (or GLEIF was unreachable)")
    for name in names:
        matches = client.search(name)
        if not matches:
            continue
        pool = [r for r in matches if r["attributes"]["entity"].get("category") != "BRANCH"] or matches
        if len(pool) > 1 and jurisdictions:
            pool = [r for r in pool if (r["attributes"]["entity"].get("jurisdiction") or "")[:2] in
                    {j[:2] for j in jurisdictions}] or pool
        if len(pool) > 1:
            pool = [r for r in pool if r["attributes"]["entity"].get("status") == "ACTIVE"] or pool
        if len(pool) == 1:
            return pool[0], f"exact legal-name match for '{name}'", notes
        notes.append(f"{len(pool)} GLEIF records match '{name}' equally well ("
                     + ", ".join(r["attributes"]["lei"] for r in pool[:6]) + "); none was chosen - pick the right LEI and "
                     "re-run with --lei")
        return None, "", notes
    if names or leis:
        notes.append("no GLEIF record matched the LEI or legal name found on the website")
    return None, "", notes


def _address(a: dict | None) -> tuple[str, str]:
    if not a:
        return "", ""
    lines = [x for x in (a.get("addressLines") or []) if x]
    for k in ("addressNumber", "addressNumberWithinBuilding", "mailRouting"):
        if a.get(k) and a[k] not in lines:
            lines.append(a[k])
    parts = lines + [a.get("city"), a.get("region"), a.get("postalCode"), a.get("country")]
    return ", ".join(p for p in parts if p), kb.fold(a.get("postalCode") or "")


def _registration_key(jurisdiction: str, registered_as: str, ra: str) -> tuple[str, list[str]]:
    """Key matching the website identifier of the same registration (so the two corroborate each other)."""
    v = registered_as.strip()
    c = (jurisdiction or "")[:2]
    digits = re.sub(r"\D", "", v)
    if c == "JP" and jp_corporate_number_ok(digits):
        return f"JP-CN:{digits}", []
    if c == "GB" and re.fullmatch(r"[A-Z]{0,2}\d{5,8}", v.upper().replace(" ", "")):
        return f"UK-CRN:{uk_company_number(v)}", []
    if c == "IE" and digits and len(digits) <= 6:
        return f"IE-CRO:{digits}", []
    if c == "IN" and re.fullmatch(r"[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}", v):
        return f"IN-CIN:{v}", []
    if c == "FR" and len(digits) == 9:
        return f"FR-SIREN:{digits}", []
    if c == "NL" and len(digits) == 8:
        return f"NL-KVK:{digits}", []
    if c == "AU" and len(digits) in (9, 11):
        return (f"AU-ACN:{digits}" if len(digits) == 9 else f"AU-ABN:{digits}"), []
    m = re.match(r"(HR[AB])\s*(\d+(?:\s?[A-Z]{1,2})?)", v)
    if c == "DE" and m:
        return f"DE-HR:{m.group(1)} {m.group(2)}", [f"DE-HR:{m.group(1)} {m.group(2)}"]
    ids = find_identifiers(v)
    if ids:
        return f"{ids[0].scheme}:{ids[0].value}", []
    return f"RA:{ra}:{kb.fold(v)}", []


def candidates(rec: dict, client: GleifClient, matched_by: str) -> tuple[list[Candidate], dict]:
    a = rec["attributes"]
    e, reg, lei = a["entity"], a.get("registration") or {}, a["lei"]
    url = RECORD_URL.format(lei=lei)
    out: list[Candidate] = []

    def add(field_, value, key, method, snip, weight=0.95, **kw):
        out.append(Candidate(field_, value, key, weight, Evidence("registry", url, f"GLEIF LEI record: {method}", snip), **kw))

    legal = (e.get("legalName") or {}).get("name") or ""
    others = [n for n in record_names(rec) if n != legal]
    if legal:
        add("registered_legal_name", legal, "name:" + kb.fold(legal), "legalName", f"legalName: {legal}",
            aliases=["name:" + kb.fold(n) for n in others], details=[f"also registered as: {n}" for n in others])
    add("corporate_registration_reference", f"Legal Entity Identifier (LEI): {lei}" + (" (check digit valid)" if lei_ok(lei) else ""),
        f"LEI:{lei}", "lei", lei, details=[f"LEI registration status: {reg.get('status')}",
                                           f"next renewal: {(reg.get('nextRenewalDate') or '')[:10]}",
                                           f"corroboration level: {reg.get('corroborationLevel')}"])
    jurisdiction = e.get("jurisdiction") or ""
    ra = (e.get("registeredAt") or {}).get("id") or ""
    if e.get("registeredAs"):
        ra_name = (client.registration_authority(ra) if ra and ra != "RA999999" else None) or ra or "registration authority"
        key, contains = _registration_key(jurisdiction, e["registeredAs"], ra)
        add("corporate_registration_reference", f"{ra_name}: {e['registeredAs']}", key, "registeredAs / registeredAt",
            f"registeredAt {ra}, registeredAs {e['registeredAs']}", contains=contains)
    if jurisdiction:
        add("registration_jurisdiction", kb.country_name(jurisdiction), "jur:" + jurisdiction, "jurisdiction", jurisdiction)
    form = e.get("legalForm") or {}
    if form.get("id") or form.get("other"):
        code = form.get("id") or ""
        names = (client.legal_form_names(code) if code and code != "8888" else []) or ([form["other"]] if form.get("other") else [])
        known = next((f for n in names for f in [kb.legal_form_of("X " + n, jurisdiction) or kb.legal_form_of(n, jurisdiction)] if f), None)
        label = " / ".join(names[:2]) or f"ELF code {code}"
        add("legal_entity_type", known["type"] if known else label,
            "form:" + known["key"] if known else f"form:ELF:{code or kb.fold(label)}", "legalForm",
            f"legalForm {code}: {label}", details=[f"ISO 20275 ELF code {code}: {label}"] if code else [label])
    if e.get("category") == "BRANCH":
        add("legal_entity_type", "Branch", "form:BRANCH", "category BRANCH", "category: BRANCH", weight=0.9)
    for field_, addr_key, kind in (("registered_office", "legalAddress", "LEGAL"),
                                   ("operating_location", "headquartersAddress", "HEADQUARTERS")):
        text, postal = _address(e.get(addr_key))
        if text:
            alt = [_address(o)[0] for o in e.get("otherAddresses") or [] if kind in (o.get("type") or "")]
            add(field_, text, "addr:" + kb.fold(text), addr_key, text, contains=[postal] if len(postal) >= 4 else [],
                details=[f"also recorded as: {x}" for x in alt if x])
    status = e.get("status")
    if status in ("ACTIVE", "INACTIVE"):
        exp = e.get("expiration") or {}
        details = [f"LEI registration status: {reg.get('status')}"]
        if exp.get("reason"):
            details.append(f"expiration: {exp.get('reason')} {(exp.get('date') or '')[:10]}".strip())
        succ = e.get("successorEntity") or {}
        if succ.get("lei") or succ.get("name"):
            details.append(f"successor: {succ.get('name') or ''} {succ.get('lei') or ''}".strip())
        add("entity_operating_status", "Active" if status == "ACTIVE" else "Inactive",
            "active" if status == "ACTIVE" else "inactive", "entity status", f"entity.status: {status}", details=details)
    category = e.get("category")
    if category == "RESIDENT_GOVERNMENT_ENTITY":
        add("government_public_sector_status", "Government / public-sector body", "government", "category",
            f"category: {category}", details=[f"sub-category: {e.get('subCategory')}"] if e.get("subCategory") else [])
    elif category == "INTERNATIONAL_ORGANIZATION":
        add("government_public_sector_status", "International / intergovernmental organisation", "international",
            "category", f"category: {category}")
    elif category:
        add("government_public_sector_status", "Not recorded as a government entity", "non-government", "category",
            f"category: {category} (state-owned companies are also GENERAL)", weight=0.5)
    summary = {"lei": lei, "matched_by": matched_by, "record_url": url, "api_url": f"{client.api}/lei-records/{lei}",
               "legal_name": legal, "jurisdiction": jurisdiction, "entity_status": status, "category": category,
               "registration_status": reg.get("status"), "last_update": reg.get("lastUpdateDate"),
               "corroboration_level": reg.get("corroborationLevel")}
    return out, summary


def _display_name(e: dict) -> str:
    """Legal name, followed by an English/Latin other name when the legal name is in another script."""
    legal = (e.get("legalName") or {}).get("name") or ""
    if legal and kb.CJK.search(legal):
        latin = next((n["name"] for k in ("otherNames", "transliteratedOtherNames") for n in (e.get(k) or [])
                      if n.get("name") and not kb.CJK.search(n["name"])), "")
        return f"{legal} ({latin})" if latin else legal
    return legal


def group_overview(records: list[dict], parent_hq_country: str | None, relation: str, parent_lei: str):
    """Subsidiaries (entities with an LEI that report this parent) -> (one row per entity, operating-location
    candidates of the parent: one per country with active subsidiaries). Inactive entities are listed, not counted."""
    rows, by_country = [], {}
    for r in records:
        a = r.get("attributes") or {}
        e = a.get("entity") or {}
        hq = e.get("headquartersAddress") or e.get("legalAddress") or {}
        lei = a.get("lei") or r.get("id") or ""
        row = {"lei": lei, "name": _display_name(e), "country": hq.get("country") or "", "city": hq.get("city") or "",
               "headquarters_address": _address(hq)[0], "status": e.get("status") or "",
               "record_url": RECORD_URL.format(lei=lei)}
        rows.append(row)
        if row["status"] == "ACTIVE" and row["country"]:
            by_country.setdefault(row["country"], []).append(row)
    cands = []
    for code, group in sorted(by_country.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        n, first = len(group), group[0]
        cities = sorted({x["city"] for x in group if x["city"]})
        where = f" ({', '.join(cities[:4])}{'…' if len(cities) > 4 else ''})" if cities else ""
        cands.append(Candidate(
            "operating_location", f"{kb.country_name(code)}: {n} subsidiar{'y' if n == 1 else 'ies'}{where}",
            "group-country:" + code, 0.8 if code == parent_hq_country else 0.7,
            Evidence("registry", first["record_url"], f"GLEIF relationship data ({relation} of {parent_lei})",
                     f"{first['name']}, {first['headquarters_address']}"),
            details=[f"{x['name']}: {x['headquarters_address']} (LEI {x['lei']})" for x in group[:8]]
            + ([f"… and {n - 8} more (see subsidiaries)"] if n > 8 else [])))
    by_country_list = [{"country": c, "name": kb.country_name(c), "subsidiaries": len(v)}
                       for c, v in sorted(by_country.items(), key=lambda kv: (-len(kv[1]), kv[0]))]
    return rows, cands, by_country_list
