"""Entity identifiers in free text: scheme patterns, normalisation and check-digit validation (stdlib only).

A number is only reported with a scheme when the scheme's label is next to it or the number is self-identifying
(LEI, Indian CIN/GSTIN, Swiss UID) and passes its check digit. Check digits are verified wherever the scheme has one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .knowledge import nfkc


@dataclass
class Identifier:
    scheme: str                 # e.g. "LEI", "JP-CN", "UK-CRN"
    label: str                  # human name of the scheme
    value: str                  # normalised value
    country: str | None         # jurisdiction the scheme implies (ISO 3166), None if it implies none
    checksum: bool | None       # True = check digit valid; None = scheme has no check digit
    labelled: bool              # the scheme's label was found next to the number
    start: int = 0
    end: int = 0
    extra: dict = field(default_factory=dict)

    def display(self) -> str:
        return f"{self.label}: {self.value}"


# ---------------------------------------------------------------- check digits
def lei_ok(s: str) -> bool:
    """ISO 17442: ISO 7064 MOD 97-10 over the 20 characters."""
    if not re.fullmatch(r"[0-9A-Z]{18}[0-9]{2}", s):
        return False
    return int("".join(str(int(c, 36)) for c in s)) % 97 == 1


def jp_corporate_number_ok(s: str) -> bool:
    """Japan corporate number (houjin bangou): leading check digit = 9 - (sum(P_n * Q_n) mod 9), Q_n = 1 (odd n) or 2."""
    if not re.fullmatch(r"\d{13}", s):
        return False
    total = sum(int(d) * (1 if i % 2 == 0 else 2) for i, d in enumerate(reversed(s[1:])))
    return int(s[0]) == 9 - total % 9


def luhn_ok(s: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(s)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def abn_ok(s: str) -> bool:
    d = [int(c) for c in s]
    d[0] -= 1
    return sum(a * b for a, b in zip((10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19), d)) % 89 == 0


def acn_ok(s: str) -> bool:
    t = sum(int(c) * w for c, w in zip(s[:8], (8, 7, 6, 5, 4, 3, 2, 1)))
    return (10 - t % 10) % 10 == int(s[8])


def che_ok(s: str) -> bool:
    t = sum(int(c) * w for c, w in zip(s[:8], (5, 4, 3, 2, 7, 6, 5, 4)))
    c = 11 - t % 11
    c = 0 if c == 11 else c
    return c != 10 and c == int(s[8])


def be_enterprise_ok(s: str) -> bool:
    return 97 - int(s[:8]) % 97 == int(s[8:])


def gstin_ok(s: str) -> bool:
    cs = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    t = 0
    for i, ch in enumerate(s[:14]):
        v = cs.index(ch) * (2 if i % 2 else 1)
        t += v // 36 + v % 36
    return cs[(36 - t % 36) % 36] == s[14]


def uscc_ok(s: str) -> bool:
    cs = "0123456789ABCDEFGHJKLMNPQRTUWXY"
    if len(s) != 18 or any(c not in cs for c in s):
        return False
    w = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)
    c = 31 - sum(cs.index(ch) * x for ch, x in zip(s[:17], w)) % 31
    return cs[0 if c == 31 else c] == s[17]


def cnpj_ok(s: str) -> bool:
    def dv(base, weights):
        r = sum(int(a) * b for a, b in zip(base, weights)) % 11
        return 0 if r < 2 else 11 - r
    w1 = (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
    return len(set(s)) > 1 and dv(s[:12], w1) == int(s[12]) and dv(s[:13], (6,) + w1) == int(s[13])


def gs1_ok(s: str) -> bool:
    t = sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(s[:-1]))
    return (10 - t % 10) % 10 == int(s[-1])


def kr_brn_ok(s: str) -> bool:
    d = [int(c) for c in s]
    t = sum(a * b for a, b in zip(d[:9], (1, 3, 7, 1, 3, 7, 1, 3, 5))) + d[8] * 5 // 10
    return (10 - t % 10) % 10 == d[9]


# ---------------------------------------------------------------- schemes
NO = r"(?:no\.?|nr\.?|number|num\.?|#|n°|nº)"
UK_PREFIX = r"(?:SC|NI|OC|SO|NC|R0|LP|SL|FC|BR|IP|RS)"
UK_CONTEXT = re.compile(r"England|Wales|Scotland|Northern Ireland|Companies House|United Kingdom|\bUK\b")
REGULATORY_CONTEXT = re.compile(r"\b(?:FCA|PRA|SEC|FINRA|NMLS|SEBI|IRDAI?|RBI|FRN|licen[cs]\w*|regulat\w*|authori[sz]\w*|"
                                r"Bureau|broker|agent|insurance|trade ?mark)\b|財務局|登録番号\s*[（(]", re.I)
DIGITS = re.compile(r"\D")
CIN_OWNERSHIP = {"PLC": "public limited company", "PTC": "private limited company", "GOI": "Government of India company",
                 "SGC": "State Government company", "FLC": "financial lease company", "GAP": "general association (public)",
                 "NPL": "not-for-profit licence company", "ULL": "public unlimited company", "ULT": "private unlimited company",
                 "OPC": "one person company", "FTC": "foreign subsidiary (private)", "GAT": "general association (private)"}
VAT_PREFIX = r"(?:AT|BE|BG|CY|CZ|DE|DK|EE|EL|ES|FI|FR|GB|HR|HU|IE|IT|LT|LU|LV|MT|NL|PL|PT|RO|SE|SI|SK|XI)"

_SPECS: list = []


def scheme(name, label, country, pattern, flags=re.I):
    rx = re.compile(pattern, flags)

    def register(fn):
        _SPECS.append((name, label, country, rx, fn))
        return fn
    return register


def _before(text, m, n=60):
    """Up to n characters before the match, within the same sentence."""
    window = text[max(0, m.start() - n):m.start()]
    return re.split(r"(?<=[.;。])\s+|\n", window)[-1]


@scheme("LEI", "Legal Entity Identifier (LEI)", None,
        r"\b(?:LEI(?:\s*code)?|legal\s+entity\s+identifier)(?:\s*" + NO + r")?\s*[:：.\-]?\s*([0-9A-Za-z]{18}[0-9]{2})\b")
def _lei_labelled(m, text):
    v = m.group(1).upper()
    return (v, lei_ok(v), True, {}) if lei_ok(v) else None


@scheme("LEI", "Legal Entity Identifier (LEI)", None, r"\b([0-9A-Z]{18}[0-9]{2})\b", flags=0)
def _lei_bare(m, text):
    v = m.group(1)
    if not lei_ok(v) or not re.search(r"[A-Z]", v[:18]) or not re.search(r"\d", v[:18]):
        return None
    return v, True, bool(re.search(r"\bLEI\b|legal entity identifier", _before(text, m), re.I)), {}


@scheme("JP-CN", "Japan corporate number (houjin bangou)", "JP",
        r"(?:法人番号|corporate\s+(?:identification\s+)?(?:number|no\.?)|houjin\s+bangou)\s*[:：]?\s*T?\s*(\d[\d \-]{11,16}\d)")
def _jp_cn(m, text):
    v = DIGITS.sub("", m.group(1))
    return (v, True, True, {}) if jp_corporate_number_ok(v) else None


@scheme("JP-CN", "Japan corporate number (from qualified-invoice registration number)", "JP",
        r"(?:適格請求書発行事業者登録番号|登録番号|(?:qualified\s+)?invoice\s+(?:issuer\s+)?registration\s+(?:number|no\.?))\s*[:：]?\s*T\s?(\d[\d \-]{11,16}\d)")
def _jp_invoice(m, text):
    v = DIGITS.sub("", m.group(1))
    return (v, True, True, {"invoice_number": "T" + v}) if jp_corporate_number_ok(v) else None


@scheme("IN-CIN", "India Corporate Identity Number (CIN)", "IN",
        r"\b([LU]\d{5}[A-Z]{2}\d{4}(?:" + "|".join(CIN_OWNERSHIP) + r")\d{6})\b", flags=0)
def _cin(m, text):
    v = m.group(1)
    labelled = bool(re.search(r"\bCIN\b|corporate identi(?:ty|fication) number", _before(text, m), re.I))
    extra = {"listed": v[0] == "L", "state": v[6:8], "year": v[8:12], "class": v[12:15],
             "class_meaning": CIN_OWNERSHIP[v[12:15]]}
    return v, None, labelled, extra


@scheme("IN-LLPIN", "India LLP Identification Number (LLPIN)", "IN", r"\bLLPIN\s*[:.]?\s*([A-Z]{3}-\d{4})\b")
def _llpin(m, text):
    return m.group(1).upper(), None, True, {}


@scheme("IN-GSTIN", "India GST Identification Number (GSTIN)", "IN", r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b", flags=0)
def _gstin(m, text):
    v = m.group(1)
    if not gstin_ok(v):
        return None
    return v, True, bool(re.search(r"\bGST", _before(text, m), re.I)), {}


@scheme("UK-CRN", "UK Companies House company number", "GB",
        r"\b(?:registered|incorporated)\s+in\s+(?:England(?:\s*(?:and|&)\s*Wales)?|Scotland|Northern\s+Ireland|Wales)\b"
        r"[^\n]{0,40}?\b(" + UK_PREFIX + r"?\s?\d{5,8})\b")
def _uk_crn_place(m, text):
    return uk_company_number(m.group(1)), None, True, {}


@scheme("UK-CRN", "UK Companies House company number", "GB",
        r"\b(?:company|registration|registered|reg\.?|crn)\s*(?:registration\s*|registered\s*)?(?:" + NO + r")\s*[:.\-]?\s*("
        + UK_PREFIX + r"?\s?\d{5,8})\b")
def _uk_crn(m, text):
    window = text[max(0, m.start() - 200):m.end() + 200]
    if not UK_CONTEXT.search(window) or REGULATORY_CONTEXT.search(_before(text, m, 40)):
        return None
    return uk_company_number(m.group(1)), None, True, {}


def uk_company_number(raw):
    raw = raw.upper().replace(" ", "")
    prefix = re.match(r"[A-Z]*", raw).group(0)
    return prefix + raw[len(prefix):].zfill(8 - len(prefix))


@scheme("IE-CRO", "Ireland Companies Registration Office number", "IE",
        r"\b(?:registered\s+in\s+(?:the\s+Republic\s+of\s+)?Ireland\b[^\n]{0,40}?|CRO\s*(?:" + NO + r")?\s*[:.]?\s*)(\d{4,6})\b")
def _ie_cro(m, text):
    return m.group(1), None, True, {}


@scheme("DE-HR", "German commercial register (Handelsregister)", "DE",
        r"\b(HR[AB])\s*(?:Nr\.?\s*)?(\d{1,6}(?:\s?[A-Z]{1,2}\b)?)", flags=0)
def _de_hr(m, text):
    window = text[max(0, m.start() - 150):m.end() + 150]
    court = re.search(r"(?:Amtsgericht|Registergericht|Registration court|Register court|Local court|District court)\s*:?\s*"
                      r"(?:Amtsgericht\s+)?([A-ZÄÖÜ][\wäöüß\-]+(?:\s(?:am|an der|im|in der)\s[A-ZÄÖÜ][\wäöüß\-]+)?)", window)
    v = f"{m.group(1)} {m.group(2).strip()}"
    extra = {"section": m.group(1)}
    if court:
        extra["court"] = court.group(1)
        v += f" (Amtsgericht {court.group(1)})"
    return v, None, True, extra


@scheme("AT-FN", "Austrian company register (Firmenbuchnummer)", "AT",
        r"\b(?:FN|Firmenbuch(?:nummer)?|Firmenbuch-?Nr\.?)\s*[:.]?\s*(\d{1,6}\s?[a-zA-Z])\b")
def _at_fn(m, text):
    return m.group(1).replace(" ", "").lower(), None, True, {}


@scheme("CH-UID", "Swiss enterprise identification number (UID)", "CH", r"\bCHE[-\s]?(\d{3})[.\s]?(\d{3})[.\s]?(\d{3})\b", flags=0)
def _che(m, text):
    d = m.group(1) + m.group(2) + m.group(3)
    return (f"CHE-{m.group(1)}.{m.group(2)}.{m.group(3)}", True, True, {}) if che_ok(d) else None


@scheme("FR-SIREN", "France SIREN (RCS)", "FR",
        r"\b(?:RCS|R\.C\.S\.)\s*(?:de\s+)?([A-ZÉÈ][A-Za-zÀ-ÿ\-' ]{1,30}?)\s*(?:[AB]\s+)?(\d{3}\s?\d{3}\s?\d{3})\b", flags=0)
def _fr_rcs(m, text):
    v = DIGITS.sub("", m.group(2))
    return (v, True, True, {"rcs": m.group(1).strip()}) if luhn_ok(v) else None


@scheme("FR-SIREN", "France SIREN (RCS)", "FR", r"\b(\d{3}\s?\d{3}\s?\d{3})\s*(?:RCS|R\.C\.S\.)\s+([A-ZÉÈ][A-Za-zÀ-ÿ\-]+)", flags=0)
def _fr_rcs_after(m, text):
    v = DIGITS.sub("", m.group(1))
    return (v, True, True, {"rcs": m.group(2)}) if luhn_ok(v) else None


@scheme("FR-SIREN", "France SIREN", "FR", r"\bSIREN\s*(?:" + NO + r")?\s*[:.]?\s*(\d{3}\s?\d{3}\s?\d{3})\b")
def _fr_siren(m, text):
    v = DIGITS.sub("", m.group(1))
    return (v, True, True, {}) if luhn_ok(v) else None


@scheme("FR-SIRET", "France SIRET (establishment)", "FR", r"\bSIRET\s*(?:" + NO + r")?\s*[:.]?\s*(\d{3}\s?\d{3}\s?\d{3}\s?\d{5})\b")
def _fr_siret(m, text):
    v = DIGITS.sub("", m.group(1))
    return (v, True, True, {}) if luhn_ok(v) else None


@scheme("NL-KVK", "Netherlands Chamber of Commerce (KvK) number", "NL",
        r"\b(?:KvK|K\.v\.K\.|Kamer\s+van\s+Koophandel|(?:Dutch\s+)?Chamber\s+of\s+Commerce)"
        r"(?:[-\s]?(?:nummer|number|nr\.?|no\.?|registration(?:\s+number)?))?[^\d\n.;]{0,30}?(\d{8})\b")
def _kvk(m, text):
    return m.group(1), None, True, {}


@scheme("BE-KBO", "Belgium enterprise number (KBO/BCE)", "BE",
        r"\b(?:BCE|KBO|enterprise\s+(?:number|no\.?)|ondernemingsnummer|num[ée]ro\s+d'entreprise|RPR|RPM)\s*[:.]?\s*"
        r"(?:BE\s?)?(0?[01]\d{2}[.\s]?\d{3}[.\s]?\d{3})\b")
def _be(m, text):
    v = DIGITS.sub("", m.group(1)).zfill(10)
    return (f"{v[:4]}.{v[4:7]}.{v[7:]}", True, True, {}) if be_enterprise_ok(v) else None


@scheme("AU-ABN", "Australian Business Number (ABN)", "AU", r"\bABN\s*[:.]?\s*(\d{2}\s?\d{3}\s?\d{3}\s?\d{3})\b")
def _abn(m, text):
    v = DIGITS.sub("", m.group(1))
    return (v, True, True, {}) if abn_ok(v) else None


@scheme("AU-ACN", "Australian Company Number (ACN)", "AU", r"\bACN\s*[:.]?\s*(\d{3}\s?\d{3}\s?\d{3})\b")
def _acn(m, text):
    v = DIGITS.sub("", m.group(1))
    return (v, True, True, {}) if acn_ok(v) else None


@scheme("NZ-NZBN", "New Zealand Business Number (NZBN)", "NZ", r"\bNZBN\s*[:.]?\s*(94\d{11})\b")
def _nzbn(m, text):
    return (m.group(1), True, True, {}) if gs1_ok(m.group(1)) else None


@scheme("SG-UEN", "Singapore Unique Entity Number (UEN)", "SG",
        r"\bUEN\s*(?:" + NO + r")?\s*[:.]?\s*(\d{8,9}[A-Za-z]|[TSRtsr]\d{2}[A-Za-z]{2}\d{4}[A-Za-z])\b")
def _uen(m, text):
    return m.group(1).upper(), None, True, {}


@scheme("CA-BN", "Canada Business Number (BN)", "CA",
        r"\b(?:Business\s+Number|BN)\s*(?:" + NO + r")?\s*[:.]?\s*(\d{9})(?:\s?R[TCP]\s?\d{4})?\b", flags=0)
def _ca_bn(m, text):
    return (m.group(1), True, True, {}) if luhn_ok(m.group(1)) else None


@scheme("US-EIN", "US Employer Identification Number (EIN)", "US",
        r"\b(?:EIN|FEIN|employer\s+identification\s+(?:no\.?|number)|federal\s+(?:tax|employer)\s+id(?:entification)?"
        r"(?:\s*(?:no\.?|number))?|tax\s+id(?:entification)?(?:\s+number|\s+no\.?)?)\s*[:.#]?\s*(\d{2}-\d{7})\b")
def _ein(m, text):
    return m.group(1), None, True, {}


@scheme("US-CIK", "SEC Central Index Key (CIK)", "US", r"\bCIK\s*(?:" + NO + r"|code)?\s*[:.]?\s*(\d{4,10})\b")
def _cik(m, text):
    return m.group(1).zfill(10), None, True, {}


@scheme("CN-USCC", "China Unified Social Credit Code (USCC)", "CN",
        r"(?:统一社会信用代码|unified\s+social\s+credit\s+(?:code|identifier)|USCC)\s*[:：]?\s*([0-9A-Za-z]{18})")
def _uscc(m, text):
    v = m.group(1).upper()
    return (v, True, True, {}) if uscc_ok(v) else None


@scheme("KR-BRN", "Korea business registration number", "KR",
        r"(?:사업자\s*등록\s*번호|business\s+registration\s+(?:no\.?|number))\s*[:：]?\s*(\d{3}-\d{2}-\d{5})")
def _kr_brn(m, text):
    v = DIGITS.sub("", m.group(1))
    return (m.group(1), True, True, {}) if kr_brn_ok(v) else None


@scheme("BR-CNPJ", "Brazil CNPJ", "BR", r"\bCNPJ(?:/MF)?\s*(?:n[º°o]\.?)?\s*[:.]?\s*(\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2})\b")
def _cnpj(m, text):
    v = DIGITS.sub("", m.group(1))
    return (m.group(1), True, True, {}) if cnpj_ok(v) else None


@scheme("VAT", "VAT identification number", None,
        r"(?:\bVAT|\bV\.A\.T\.|\bUSt-?Id(?:Nr)?\.?|\bUSt\.?-?ID|\bUmsatzsteuer-?(?:Identifikationsnummer|ID)|\bTVA|\bIVA|"
        r"\bP\.\s?IVA|\bPartita\s+IVA|\bBTW|\bMwSt\.?|\bNIF|\bCIF|\bmoms(?:reg)?)"
        r"[\w .\-()/]{0,30}?[:.]?\s*(" + VAT_PREFIX + r")\s?([0-9A-Z](?:[0-9A-Z]|[ .](?=\d)){6,15}[0-9A-Z])\b")
def _vat(m, text):
    prefix = m.group(1).upper()
    body = re.sub(r"[ .]", "", m.group(2)).upper()
    if not (8 <= len(body) <= 12) or not re.search(r"\d{6}", body):
        return None
    country = {"EL": "GR", "XI": "GB"}.get(prefix, prefix)
    return prefix + body, None, True, {"vat_country": country}


@scheme("VAT", "VAT registration number (country not stated)", None,
        r"\bVAT\s*(?:reg(?:istration)?\.?\s*)?(?:no\.?|number)\s*[:.]?\s*(\d{3}\s?\d{4}\s?\d{2}(?:\s?\d{3})?)\b")
def _vat_plain(m, text):
    return DIGITS.sub("", m.group(1)), None, True, {}


@scheme("DUNS", "D-U-N-S number", None, r"\b(?:D-U-N-S|DUNS)\s*®?\s*(?:" + NO + r")?\s*[:.]?\s*(\d{2}-?\d{3}-?\d{4})\b")
def _duns(m, text):
    return DIGITS.sub("", m.group(1)), None, True, {}


@scheme("REG", "Registration number (scheme not identified)", None,
        r"\b(?:company|corporate|business|commercial|companies)\s+(?:registration|register|registry|reg\.?)\s*(?:"
        + NO + r")?\s*[:.]?\s*([A-Z0-9][A-Z0-9\-/.]{3,20}[A-Z0-9])\b")
def _reg(m, text):
    v = m.group(1)
    if not re.search(r"\d{3}", v) or REGULATORY_CONTEXT.search(_before(text, m, 40)):
        return None
    return v.upper(), None, True, {}


@scheme("REG", "Registration number (scheme not identified)", None,
        r"\b(?:registration|registered|reg\.?)\s*(?:" + NO + r")\s*[:.]?\s*([A-Z0-9][A-Z0-9\-/.]{3,20}[A-Z0-9])\b")
def _reg2(m, text):
    return _reg(m, text)


def find_identifiers(text: str) -> list[Identifier]:
    """All identifiers in a text. Earlier (more specific) schemes win overlapping spans; duplicates are dropped."""
    text = nfkc(text)
    taken: list[tuple[int, int]] = []
    out: dict[tuple[str, str], Identifier] = {}
    for scheme_name, label, country, rx, build in _SPECS:
        for m in rx.finditer(text):
            groups = [g for g in range(1, (rx.groups or 0) + 1) if m.start(g) >= 0] or [0]
            span = (min(m.start(g) for g in groups), max(m.end(g) for g in groups))
            if any(s < span[1] and span[0] < e for s, e in taken):
                continue
            built = build(m, text)
            if not built:
                continue
            value, checksum, labelled, extra = built
            taken.append(span)
            implied = extra.get("vat_country", country)
            ident = Identifier(scheme_name, label, value, implied, checksum, labelled, span[0], span[1], extra)
            out.setdefault((scheme_name, value), ident)
    return sorted(out.values(), key=lambda i: i.start)
