"""Reference data (airrp_entity/data/*.json): countries, legal forms, regulators, sectors, regulated activities."""
from __future__ import annotations

import functools
import json
import os
import re
import unicodedata

DATA = os.path.join(os.path.dirname(__file__), "data")
CJK = re.compile(r"[　-ヿ㐀-鿿가-힯＀-￯]")


def _load(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as fh:
        return json.load(fh)


def nfkc(s: str | None) -> str:
    return unicodedata.normalize("NFKC", s or "").replace("\xa0", " ")


def fold(s: str | None) -> str:
    """Comparison key: NFKC, case-folded, '&' = 'and', punctuation removed, whitespace collapsed."""
    s = nfkc(s).casefold().replace("&", " and ")
    return " ".join(re.sub(r"[^\w]+", " ", s).split())


def words_rx(alternatives, flags=re.I) -> re.Pattern:
    """One regex matching any alternative on word boundaries (CJK alternatives match anywhere)."""
    alts = sorted(set(alternatives), key=len, reverse=True)
    latin = [a for a in alts if not CJK.search(a)]
    cjk = [a for a in alts if CJK.search(a)]
    parts = []
    if latin:
        parts.append(r"(?<![\w-])(?:" + "|".join(latin) + r")(?![\w-])")
    if cjk:
        parts.append("(?:" + "|".join(cjk) + ")")
    return re.compile("|".join(parts) or r"(?!x)x", flags)


# ---------------------------------------------------------------- countries and places
@functools.lru_cache(None)
def _countries():
    return _load("countries.json")


def country_name(code: str | None) -> str:
    if not code:
        return ""
    d = _countries()
    if code in d["countries"]:
        return d["countries"][code]["name"]
    if code in d["us_states"]:
        return f"{d['us_states'][code]}, United States"
    if code == "EU":
        return "European Union"
    return code


@functools.lru_cache(None)
def _place_index():
    d = _countries()
    countries, states = {}, {}
    for code, c in d["countries"].items():
        countries.setdefault(fold(c["name"]), code)
        for a in c["aliases"]:
            countries.setdefault(fold(a), code)
    for code, name in d["us_states"].items():
        states[fold(name)] = code
    nations = {}
    for k in d["uk_nations"]:
        nations.setdefault(fold(k), k)
    return countries, states, nations


def lookup_place(text: str, prefer_states: bool = False) -> tuple[str, str] | None:
    """('JP', 'Japan'), ('US-DE', 'Delaware, United States'), ('GB', 'United Kingdom (England and Wales)') or None."""
    countries, states, nations = _place_index()
    key = fold(text)
    key = re.sub(r"^(?:the )?(?:state of |commonwealth of |republic of |kingdom of )", "", key)
    if key in nations:
        return "GB", f"United Kingdom ({nations[key]})"
    first, second = (states, countries) if prefer_states else (countries, states)
    for table in (first, second):
        if key in table:
            code = table[key]
            return code, country_name(code)
    return None


def country_in_text(text: str) -> str | None:
    """Country named at the end of an address-like string ('..., Tokyo 108-0075 Japan' -> 'JP')."""
    countries, _, nations = _place_index()
    segments = [s for s in re.split(r"[,\n;|]", nfkc(text)) if s.strip()]
    for seg in reversed(segments[-3:]):
        words = fold(seg).split()
        for n in (4, 3, 2, 1):
            if len(words) >= n:
                tail = " ".join(words[-n:])
                if tail in nations:
                    return "GB"
                if tail in countries:
                    return countries[tail]
    return None


def is_country(code: str) -> bool:
    return code in _countries()["countries"]


def country_names_index() -> dict[str, str]:
    """folded country name/alias -> code (for country-selector link text)."""
    return _place_index()[0]


# ---------------------------------------------------------------- legal forms
@functools.lru_cache(None)
def _legal_forms():
    d = _load("legal_forms.json")
    compiled = []
    for s in d["suffixes"]:
        flags = 0 if s.get("cs") else re.I
        if s.get("anywhere"):
            rx = re.compile(s["pattern"], flags)
        else:
            rx = re.compile(r"(?:^|(?<=[\s,(]))(?:" + s["pattern"] + r")(?=\s*\)?\s*$)", flags)
        compiled.append((rx, s))
    return d["forms"], compiled


@functools.lru_cache(maxsize=8192)
def legal_form_of(name: str, jurisdiction: str | None = None) -> dict | None:
    """Legal form implied by a name's suffix, refined by jurisdiction. None when the name carries no known suffix."""
    forms, compiled = _legal_forms()
    n = nfkc(name).strip().rstrip(",;")
    best = None
    for rx, s in compiled:
        m = rx.search(n)
        if m and (best is None or len(m.group(0)) > len(best[0])):
            best = (m.group(0), s)
    if not best:
        return None
    suffix, s = best
    country = (jurisdiction or "")[:2] or None
    key = s.get("in", {}).get(country, s["form"]) if country else s["form"]
    form = forms[key]
    return {"key": key, "type": form["type"], "suffix": suffix.strip(), "countries": form.get("countries", []),
            "refined_by": country if key != s["form"] else None}


def form_type(key: str) -> str:
    return _legal_forms()[0][key]["type"]


@functools.lru_cache(None)
def legal_suffix_pattern() -> str:
    """Alternation of the end-of-name legal suffixes, for finding entity names in prose."""
    parts = []
    for s in _load("legal_forms.json")["suffixes"]:
        if s.get("anywhere"):
            continue
        parts.append(s["pattern"] if s.get("cs") else "(?i:" + s["pattern"] + ")")
    return "(?:" + "|".join(parts) + ")"


# ---------------------------------------------------------------- regulators, sectors, activities
@functools.lru_cache(None)
def regulators():
    out = []
    for r in _load("regulators.json")["regulators"]:
        name_rx = words_rx([re.escape(n) for n in r["names"]])
        acr_rx = words_rx([re.escape(a) for a in r["acronyms"]], flags=0) if r["acronyms"] else None
        out.append((r, name_rx, acr_rx))
    return out


def regulator_display(r: dict) -> str:
    name = next((n for n in r["names"] if not CJK.search(n)), r["names"][0])
    acr = f" ({r['acronyms'][0]})" if r["acronyms"] else ""
    return f"{name}{acr}, {country_name(r['country'])}"


@functools.lru_cache(None)
def industries():
    return [(i, words_rx(i["keywords"])) for i in _load("sectors.json")["industries"]]


@functools.lru_cache(None)
def activities():
    return [(a, words_rx(a["patterns"]) if a["patterns"] else None) for a in _load("activities.json")["activities"]]


def activity_for_sector(sector: str) -> dict | None:
    for a, _ in activities():
        if sector in a["sectors"]:
            return a
    return None
