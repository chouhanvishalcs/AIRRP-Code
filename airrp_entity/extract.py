"""Website pages -> candidates (value + evidence) for the twelve profile fields.

Every rule here is deterministic and records where its value came from. Weights express how direct the evidence is
(a labelled "Registered office" row on a legal page is stronger than a keyword in a menu); they are tuned so that
only direct statements reach "high" confidence.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlsplit

from . import knowledge as kb
from .crawl import FetchedPage, brand_token, site_key
from .identifiers import Identifier, find_identifiers
from .model import Candidate, Evidence

KIND_FACTOR = {"legal": 1.0, "regulatory": 1.0, "corporate": 1.0, "investor": 0.95, "contact": 0.95, "home": 0.92,
               "other": 0.85}
GENERIC_TLDS = {"io", "co", "tv", "me", "ai", "fm", "ly", "to", "gg", "cc", "ws", "sh", "ac", "la", "is", "so", "im",
                "app", "dev", "xyz", "eu", "asia", "info", "biz", "net", "org", "com"}
GOV_DOMAINS = (".gov", ".mil", ".gov.uk", ".gov.au", ".gov.in", ".nic.in", ".go.jp", ".lg.jp", ".gouv.fr", ".gob.es",
               ".gob.mx", ".gov.br", ".gc.ca", ".canada.ca", ".govt.nz", ".gov.sg", ".gov.za", ".gov.cn", ".go.kr",
               ".gov.hk", ".bund.de", ".admin.ch", ".gv.at", ".overheid.nl", ".government.nl", ".gov.ie", ".gov.it",
               ".gov.ae", ".gov.sa")
INTL_DOMAINS = (".int", ".europa.eu", ".un.org")
MICRODATA_ID_LABELS = {"leiCode": "LEI", "taxID": "Tax ID", "vatID": "VAT", "duns": "DUNS"}
# identifiers issued by a company register (not a tax office): they imply the jurisdiction of registration
REGISTRY_SCHEMES = {"JP-CN", "IN-CIN", "IN-LLPIN", "UK-CRN", "IE-CRO", "DE-HR", "AT-FN", "CH-UID", "FR-SIREN", "FR-SIRET",
                    "NL-KVK", "BE-KBO", "AU-ACN", "AU-ABN", "NZ-NZBN", "SG-UEN", "CN-USCC", "BR-CNPJ", "KR-BRN"}
CIN_FORMS = {"PLC": ("IN-LTD", None), "PTC": ("PVT-LTD", None), "OPC": ("PVT-LTD", None),
             "GOI": ("IN-GOVCO", "Government company (Government of India)"),
             "SGC": ("IN-GOVCO-STATE", "Government company (State Government)")}

# ---------------------------------------------------------------- labels of label/value pairs
LABELS = {
    "legal_name": ["corporate name", "company name", "legal name", "registered name", "registered company name",
                   "full legal name", "official name", "official company name", "name of company", "name of the company",
                   "trade name", "firm name", "商号", "会社名", "社名", "firmenname", "name der gesellschaft", "raison sociale",
                   "dénomination sociale", "denomination sociale", "denominación social", "razón social", "ragione sociale",
                   "statutaire naam"],
    "legal_name_generic": ["company", "name", "firma", "entity", "entity name", "名称", "operator", "provider",
                           "website operator", "service provider", "anbieter", "betreiber", "éditeur", "editeur"],
    "registered_office": ["registered office", "registered office address", "registered address", "legal address",
                          "siège social", "siege social", "domicilio social", "sede legale", "本店", "本店所在地"],
    "registered_seat": ["registered seat", "statutory seat", "legal seat", "seat", "domicile", "sitz",
                        "sitz der gesellschaft", "statutaire zetel", "sitz des unternehmens"],
    "head_office": ["head office", "head office address", "headquarters", "headquarters address", "hq",
                    "corporate headquarters", "global headquarters", "principal office", "principal place of business",
                    "principal executive offices", "main office", "本社", "本社所在地", "hauptsitz", "unternehmenssitz",
                    "sede central"],
    "address": ["address", "office address", "postal address", "mailing address", "location", "所在地", "住所",
                "adresse", "anschrift", "dirección", "indirizzo", "adres"],
    "registration": ["company number", "company no", "company registration number", "company registration no",
                     "registration number", "registration no", "registered number", "registered no", "reg no", "crn",
                     "cin", "corporate identity number", "corporate identification number", "corporate number", "法人番号",
                     "commercial register", "commercial registry", "trade register", "handelsregister", "registernummer",
                     "registergericht", "register court", "registration court", "rcs", "siren", "siret", "kvk",
                     "chamber of commerce", "abn", "acn", "uen", "ein", "employer identification number", "duns",
                     "d-u-n-s", "lei", "lei code", "legal entity identifier", "vat", "vat number", "vat no", "vat id",
                     "vat reg no", "vat registration number", "ust-idnr", "ust-id", "umsatzsteuer-identifikationsnummer",
                     "tva", "gst", "gstin", "tax id", "uid", "cnpj", "business registration number", "登録番号"],
    "legal_form": ["legal form", "legal entity type", "entity type", "company type", "type of company", "type of entity",
                   "form of company", "legal status", "corporate form", "rechtsform", "forme juridique", "forma jurídica",
                   "forma giuridica", "法人格"],
    "jurisdiction": ["jurisdiction", "jurisdiction of incorporation", "place of incorporation", "country of incorporation",
                     "state of incorporation", "incorporated in", "registered in", "country of registration",
                     "place of registration"],
    "industry": ["business", "business description", "description of business", "line of business", "lines of business",
                 "principal business", "principal activities", "principal activity", "nature of business",
                 "main business", "main businesses", "business activities", "business activity", "business domain",
                 "business fields", "business areas", "business segments", "segments", "industry", "sector", "事業内容",
                 "主な事業内容", "業種", "unternehmensgegenstand", "gegenstand des unternehmens", "objet social"],
    "regulator": ["regulator", "regulated by", "supervisory authority", "competent authority", "supervisory body",
                  "regulatory authority", "aufsichtsbehörde", "zuständige aufsichtsbehörde", "autorité de contrôle",
                  "licence number", "license number", "licence no", "license no", "frn", "firm reference number",
                  "authorisation", "authorization", "監督官庁", "許可番号", "免許番号"],
    "listing": ["stock exchange listings", "stock exchange listing", "stock listings", "stock listing", "listings",
                "listed on", "listing", "stock exchange", "stock exchanges", "securities code", "stock code", "ticker",
                "ticker symbol", "上場証券取引所", "上場市場", "証券コード"],
    "status": ["status", "company status", "entity status", "operating status"],
    "markets": ["countries", "markets", "markets served", "regions", "global presence", "countries of operation",
                "operations in", "countries and regions", "main markets", "areas served"],
}
_LABEL_INDEX = sorted(((lab, tgt) for tgt, labs in LABELS.items() for lab in labs), key=lambda x: -len(x[0]))
_LABEL_EXACT = {lab: tgt for lab, tgt in _LABEL_INDEX}


def norm_label(s: str) -> str:
    s = kb.nfkc(s).lower()
    s = re.sub(r"[\s　]+", " ", s).strip(" :：-–—|·•*\t")
    return re.sub(r"\.", "", s).strip()


def label_target(label: str) -> str | None:
    nl = norm_label(label)
    if not nl or len(nl) > 60:
        return None
    if nl in _LABEL_EXACT:
        return _LABEL_EXACT[nl]
    for lab, tgt in _LABEL_INDEX:
        if len(lab) >= 4 and nl.startswith(lab) and nl[len(lab):len(lab) + 1] in (" ", "(", "/"):
            return tgt
    return None


# ---------------------------------------------------------------- prose patterns
SUFFIX = kb.legal_suffix_pattern()
NAME_WORD = r"[A-ZÀ-ÖØ-Þ][\w'’&.\-]*"
NAME = (rf"(?:{NAME_WORD}|[0-9][\w'’&.\-]*[A-Za-z][\w'’&.\-]*)(?:[ ](?:{NAME_WORD}|of|and|for|de|du|des|la|le|der|die|das|für|und|y|e|et|&))"
        rf"{{0,7}}?[ ,]+{SUFFIX}(?:[ ,]+{SUFFIX})*(?![\w])")
CJK_CHARS = r"々ー぀-ヿ㐀-鿿ｦ-ﾟ"
CJK_NAME = rf"[{CJK_CHARS}A-Za-z0-9・]{{1,30}}?(?:株式会社|合同会社)|(?:株式会社|合同会社)[{CJK_CHARS}・]{{1,30}}"
LEADING_STOP = {"The", "In", "At", "By", "For", "From", "With", "And", "Of", "Our", "This", "That", "When", "While", "About",
                "Since", "As", "To", "On", "Copyright", "All", "Visit", "Contact", "See", "Welcome"}
COPYRIGHT_RX = re.compile(rf"(?:©|\(c\)|(?i:copyright))\s*(?:\d{{4}}(?:\s*[-–—]\s*\d{{4}})?\s*[,.]?\s*)?(?:(?i:by)\s+)?"
                          rf"(?:(?i:the)\s+)?(?P<name>{NAME}|{CJK_NAME})")
COPYRIGHT_YEAR_RX = re.compile(r"(?:©|\(c\)|copyright)\s*(?:\d{4}\s*[-–—]\s*)?((?:19|20)\d{2})", re.I)
NAME_RX = re.compile(NAME)
NAME_LINE_RX = re.compile(rf"(?:{NAME}|{CJK_NAME})")
OPERATED_RX = re.compile(rf"(?i:(?:owned and operated|operated|provided|published|run|managed|maintained|offered)\s+by)\s+"
                         rf"(?:(?i:the)\s+)?(?P<name>{NAME})")
COMPANY_REF_RX = re.compile(rf"(?P<name>{NAME})\s*[(（]\s*(?i:(?:hereinafter\s+)?(?:referred\s+to\s+as\s+)?)[\"“'‘]?"
                            rf"(?i:(?:the\s+)?[\"“'‘]?(?:Company|Group|Firm|we|us|our)\b)")
IS_COMPANY_RX = re.compile(rf"(?P<name>{NAME})\s+(?i:(?:is|are)\s+an?\s+(?:[\w-]+\s+){{0,4}}?(?:company|corporation|"
                           rf"partnership|entity|firm|bank|insurer)\s+(?:registered|incorporated|organi[sz]ed|established|"
                           rf"authori[sz]ed|regulated))")
JUR_RX = re.compile(r"(?i:(?:registered|incorporated|organi[sz]ed|established|constituted|formed|existing)\s+(?:and\s+existing\s+)?"
                    r"(?:in|under\s+the\s+laws\s+of|pursuant\s+to\s+the\s+laws\s+of)\s+(?:the\s+)?)"
                    r"(?P<place>(?:State\s+of\s+|Commonwealth\s+of\s+|Republic\s+of\s+|Kingdom\s+of\s+)?"
                    r"[A-Z][A-Za-z.'\-]+(?:\s+(?:and|&)\s+Wales|\s+[A-Z][A-Za-z.'\-]+){0,3})")
DEMONYM_RX = re.compile(r"\b(?:an?|the)\s+(?P<place>[A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\s+(?:corporation|company|limited liability "
                        r"company|limited company|public limited company|stock company|joint[- ]stock company|limited "
                        r"partnership|business corporation|statutory trust|GmbH|LLC|LLP)\b")
REG_OFFICE_RX = re.compile(r"(?i:registered\s+(?:office|address|seat)(?:\s+address)?|si[eè]ge\s+social|sitz\s+der\s+gesellschaft)"
                           r"\s*(?i:is|at|located at|situated at)?\s*[:：]?\s*(?:(?i:at)\s+)?(?P<addr>[^\n;|]{8,220})")
ADDR_STOP = re.compile(r"\s*[.,]?\s*(?:Registered in|Registered (?:no|number)|Company (?:No|number|registration)|Registration "
                       r"(?:No|number)|VAT|Tel(?:ephone)?\b|Phone|Fax|E-?mail|Directors?\b|Incorporated|Authori[sz]ed|"
                       r"Regulated|Sitz\b|Registergericht|Handelsregister|Amtsgericht|USt|Managing|CEO\b|LEI\b|\(\s*the\b)", re.I)
HQ_RX = re.compile(r"(?i:headquartered\s+in|head(?:quarter)?s?\s+(?:office\s+)?(?:is\s+|are\s+)?(?:located\s+)?in|"
                   r"principal\s+(?:place\s+of\s+business|executive\s+offices?)\s+(?:is\s+|are\s+)?(?:located\s+)?(?:at|in))"
                   r"\s+(?P<place>[A-Z0-9][^.;()\n]{2,80})")
LEGAL_PHRASE_RX = re.compile(r"\b(?:is|as)\s+an?\s+(?P<form>public limited company|private (?:company )?limited by shares|"
                             r"private limited company|limited liability company|limited liability partnership|limited "
                             r"partnership|joint[- ]stock company|stock corporation|public company limited by shares|"
                             r"société anonyme|kabushiki kaisha|non-profit (?:organi[sz]ation|corporation)|not-for-profit "
                             r"(?:organi[sz]ation|company)|registered charity|charitable incorporated organisation|"
                             r"community interest company|company limited by guarantee|statutory corporation|"
                             r"co-?operative society)\b", re.I)
LEGAL_PHRASE_KEYS = {"public limited company": "PLC", "private company limited by shares": "LTD",
                     "private limited by shares": "LTD", "private limited company": "LTD",
                     "limited liability company": "LLC", "limited liability partnership": "LLP", "limited partnership": "LP",
                     "joint-stock company": "JSC", "joint stock company": "JSC", "kabushiki kaisha": "JP-KK",
                     "société anonyme": "SA"}
LISTING_RXS = (
    re.compile(r"\blisted\s+on\s+(?:the\s+)?(?P<x>(?:Tokyo|New York|London|Nasdaq|NASDAQ|Frankfurt|Euronext|Hong Kong|Singapore|"
               r"Australian|Toronto|Shanghai|Shenzhen|Korea|Bombay|National|SIX Swiss|Swiss|Johannesburg|Taiwan|Stockholm|Oslo|"
               r"Copenhagen|Helsinki|Madrid|Milan|Italian|Mexican|B3|Indonesia|Bursa Malaysia|Philippine|Irish|Vienna|Warsaw)"
               r"[\w ]{0,25}?(?:Stock Exchange|Exchange|Stock Market|Global Select Market|Prime Market|Market))"),
    re.compile(r"\b(?P<x>(?:NYSE|NASDAQ|Nasdaq|TSE|TYO|LSE|LON|ASX|HKEX|SEHK|SGX|TSX|NSE|BSE|SIX|Euronext|XETRA|FWB|KRX|TWSE|"
               r"JSE|BMV)\s*:\s*[A-Z0-9]{1,6}(?:\.[A-Z]{1,2})?)\b"),
    re.compile(r"(?P<x>東京証券取引所(?:プライム市場)?|東証(?:プライム|スタンダード|グロース)(?:市場)?)"),
)
STATUS_RXS = (
    (re.compile(rf"(?:\b(?i:we|the company|our company|the firm|this company)\b|(?P<n>{NAME}))\s+(?i:is|are|was|has been|have been|"
                r"went|entered|was placed|has entered|has gone)\s+(?i:(?:now\s+|currently\s+)?(?:in(?:to)?\s+)?(?:voluntary\s+|"
                r"compulsory\s+|creditors'?\s+voluntary\s+|members'?\s+voluntary\s+)?)(?P<s>(?i:liquidation|administration|"
                r"receivership|dissolved|struck off|wound up|deregistered|insolvent|bankrupt))\b"), None),
    (re.compile(r"\b(?i:we|the company|our company)\s+(?i:has|have)\s+(?i:ceased|stopped)\s+(?i:trading|operations|business)\b"),
     "ceased-trading"),
    (re.compile(r"\b(?i:we are|the company is)\s+no longer\s+(?i:trading|operating|in business)\b"), "ceased-trading"),
    (re.compile(rf"(?P<n>{NAME})\s+(?:i\.\s?L\.|in Liquidation|en liquidation)"), "liquidation"),
)
STATUS_DISPLAY = {"active": "Active", "liquidation": "In liquidation", "insolvency": "In insolvency / administration",
                  "dissolved": "Dissolved", "ceased-trading": "Ceased trading", "inactive": "Inactive"}
STATUS_WORDS = {"liquidation": "liquidation", "wound up": "liquidation", "administration": "insolvency",
                "receivership": "insolvency", "insolvent": "insolvency", "bankrupt": "insolvency", "dissolved": "dissolved",
                "struck off": "dissolved", "deregistered": "dissolved"}
MARKET_COUNT_RX = re.compile(r"(?:in|across|to|over|than|serving|serves|presence in|operations in|operate in|sold in|"
                             r"available in)\s+(?:more than\s+|over\s+|around\s+|approximately\s+|about\s+|nearly\s+|almost\s+)?"
                             r"(?P<n>\d{2,3})\+?\s+(?:countries|markets|nations)(?:\s+and\s+(?:regions|territories))?", re.I)
SIC_RX = re.compile(r"\b(?:SIC|NACE|NAICS|ISIC)(?:\s*(?:code|2007))?s?\s*(?:\(\d{4}\))?\s*[:.]?\s*(?P<code>\d{4,6})"
                    r"(?:\s*[-–:]\s*(?P<desc>[^\n|;]{3,80}))?")
REG_CUE = re.compile(r"(?:authori[sz]ed|regulated|licen[sc]ed|supervised|registered|approved|overseen|permitted|recogni[sz]ed)"
                     r"(?:\s+and\s+(?:regulated|supervised|authori[sz]ed|licen[sc]ed))?\s+(?:by|with|under)\b|\bmember\s+(?:of\s+)?"
                     r"|\bunder\s+the\s+supervision\s+of\b|\bsubject\s+to\s+(?:the\s+)?(?:supervision|regulation|oversight)\s+"
                     r"(?:of|by)\b|\blicen[cs]e\s+(?:issued\s+)?(?:by|from)\b|\bholds?\s+an?\b[^.\n]{0,40}\blicen[cs]e\b|"
                     r"\bregistered\s+as\b[^.\n]{0,60}\bwith\b|登録|許可|免許|監督|認可|beaufsichtigt|unterliegt\s+der\s+aufsicht|"
                     r"zugelassen|agréée?\s+par|autoris[ée]+e?\s+par", re.I)
LICENCE_RXS = (
    (re.compile(r"\b(?:FRN|Firm Reference Number|FCA (?:firm )?(?:reference |registration )?(?:number|no\.?))\s*[:.]?\s*(\d{6,7})\b", re.I),
     "GB-FCA", "FCA Firm Reference Number"),
    (re.compile(r"\bNMLS\s*(?:ID|#|No\.?|number)?\s*[:.#]?\s*(\d{3,8})\b"), "US-NMLS", "NMLS ID"),
    (re.compile(r"\bCRD\s*(?:#|No\.?|number)\s*[:.]?\s*(\d{3,8})\b"), "US-FINRA", "FINRA CRD number"),
    (re.compile(r"\b(?:Australian Financial Services Licen[cs]e|AFSL)\s*(?:No\.?|number)?\s*[:.]?\s*(\d{5,7})\b", re.I),
     "AU-ASIC", "Australian Financial Services Licence"),
    (re.compile(r"\bAustralian Credit Licen[cs]e\s*(?:No\.?|number)?\s*[:.]?\s*(\d{5,7})\b", re.I), "AU-ASIC",
     "Australian Credit Licence"),
    (re.compile(r"\b((?:Kanto|Kinki|Tokai|Hokkaido|Tohoku|Hokuriku|Chugoku|Shikoku|Kyushu|Fukuoka)\s+Local\s+Finance\s+"
                r"(?:Branch\s+)?Bureau\s*\([^)]{2,25}\)\s*No\.?\s*\d{1,6})", re.I), "JP-LFB", "Local Finance Bureau registration"),
    (re.compile(r"((?:関東|近畿|東海|北海道|東北|北陸|中国|四国|九州|福岡)財務(?:支)?局長?\s*[（(][^)）]{1,10}[)）]\s*第\s*\d{1,6}\s*号)"),
     "JP-LFB", "Local Finance Bureau registration"),
    (re.compile(r"\bSEBI\s*(?:Registration|Regn\.?|Reg\.?)\s*(?:No\.?|Number)\s*[:.]?\s*([A-Z]{2,4}\d{6,12})\b", re.I),
     "IN-SEBI", "SEBI registration"),
    (re.compile(r"\bIRDAI?\s*(?:Registration|Regn\.?|Reg\.?|Licen[cs]e)\s*(?:No\.?|Number|Code)\s*[:.]?\s*([\w/.\-]{2,20}\d)", re.I),
     "IN-IRDAI", "IRDAI registration"),
    (re.compile(r"\bBaFin[- ]?ID\s*[:.]?\s*(\d{6,9})\b", re.I), "DE-BAFIN", "BaFin-ID"),
    (re.compile(r"\bCySEC\s+(?:licen[cs]e|authori[sz]ation)\s*(?:No\.?|number)?\s*[:.]?\s*(\d{3}/\d{2})\b", re.I), "CY-CYSEC",
     "CySEC licence"),
    (re.compile(r"電気通信事業(?:者)?(?:届出|登録)番号\s*[:：]?\s*([A-Z0-9\-]{5,20})"), "JP-MIC",
     "Telecommunications business notification/registration"),
    (re.compile(r"\b(?:licen[cs]e|authori[sz]ation|permit)\s+(?:number|no\.?)\s*[:.#]?\s*([A-Z0-9][A-Z0-9\-/]{2,20}\d)\b", re.I),
     None, "Licence number"),
)


def snippet(text: str, start: int, end: int, width: int = 90) -> str:
    s, e = max(0, start - width), min(len(text), end + width)
    return ("…" if s else "") + " ".join(text[s:e].split()) + ("…" if e < len(text) else "")


def clean_name(name: str) -> str:
    words = name.strip(" ,.;:").split()
    while len(words) > 1 and words[0] in LEADING_STOP:
        words = words[1:]
    out = " ".join(words)
    return out if not out.endswith(",") else out[:-1]


def looks_like_address(s: str) -> bool:
    s = s.strip()
    if not (8 <= len(s) <= 250) or "@" in s or "http" in s.lower() or not re.search(r"\d", s):
        return False
    if re.fullmatch(r"[\d\s()+\-./]+", s) or re.match(r"(?i)(?:tel|phone|fax)\b", s):
        return False
    return bool(re.search(r"[,，、]", s) or len(s.split()) >= 3 or kb.CJK.search(s))


def looks_like_name(s: str, need_suffix: bool = False) -> bool:
    s = s.strip()
    if not (2 <= len(s) <= 120) or "http" in s.lower() or "@" in s or len(s.split()) > 14:
        return False
    if not re.search(r"[^\W\d_]", s):
        return False
    return kb.legal_form_of(s) is not None or not need_suffix


POSTCODE_RX = re.compile(r"\b\d{3}-\d{4}\b|\b\d{4,6}(?:-\d{4})?\b|\b[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}\b|\b[A-Z]\d[A-Z] ?\d[A-Z]\d\b")


def _address_under(lines: list[str], n: int) -> tuple[str, int]:
    """The address written on the lines below lines[n] (up to four, stopping at a label or after the country line):
    (address, number of lines used), or ("", 0)."""
    block = []
    for nxt in lines[n + 1:n + 5]:
        if label_target(nxt) or re.match(r"^[^:：]{2,40}[:：]", nxt) or len(nxt) > 80:
            break
        if not block and kb.lookup_place(nxt):        # a country heading starts the next section
            break
        block.append(nxt)
        if kb.country_in_text(nxt):
            break
    addr = ", ".join(block)
    return (addr, len(block)) if len(block) >= 2 and looks_like_address(addr) else ("", 0)


def address_key(s: str) -> str:
    """Same address written with or without its country ('..., United Kingdom' / '..., GB') or a leading
    building/office name ('Customer Centre, 200 Harbour Road, ...') gets the same key."""
    parts = [x for x in re.split(r"[,\n]", kb.nfkc(s)) if x.strip()]
    while len(parts) > 2 and not re.search(r"\d", parts[0]) and any(re.search(r"\d", x) for x in parts[1:]):
        parts = parts[1:]
    if len(parts) > 1 and (kb.country_in_text(parts[-1]) and kb.lookup_place(parts[-1])
                           or kb.is_country(parts[-1].strip().upper())):
        parts = parts[:-1]
    return "addr:" + kb.fold(" ".join(parts))


# ---------------------------------------------------------------- the extractor
@dataclass
class SiteContext:
    seeds: list[str]
    site_keys: set[str]
    today: date = field(default_factory=date.today)

    @property
    def brands(self) -> set[str]:
        return {brand_token(k) for k in self.site_keys if k}


class _Collector:
    def __init__(self, ctx: SiteContext):
        self.ctx = ctx
        self.cands: list[Candidate] = []
        self.mentions: dict[str, list] = defaultdict(list)          # entity names in prose -> [(url, snippet)]
        self.industry_hits: dict[str, dict] = {}
        self.activity_hits: dict[str, dict] = {}
        self.copyright_years: list[int] = []
        self.identifiers: list[tuple[Identifier, str]] = []
        self.listings: list[tuple[str, str, str]] = []                # (listing, url, snippet)
        brands = "|".join(re.escape(b) for b in sorted(self.ctx.brands, key=len, reverse=True) if len(b) >= 3) or r"(?!x)x"
        subj = (rf"(?:\bwe\s+are|\bthe\s+(?:company|group|organi[sz]ation|corporation|bank|authority|agency)\s+is|"
                rf"\b(?i:{brands})\w*(?:\s+[A-Z]\w+){{0,4}}\s+is)")
        self.gov_rxs = (
            (re.compile(subj + r"\s+an?\s+(?:[\w-]+\s+){0,3}?(?:government|governmental|public|statutory|executive|"
                        r"non-departmental|ministerial|non-ministerial|state)\s+(?:agency|authority|body|department|ministry|"
                        r"corporation|institution|enterprise|entity|organi[sz]ation|regulator)\b", re.I),
             "government", "Government / public-sector body"),
            (re.compile(subj + r"\s+an?\s+(?:[\w-]+\s+){0,2}?(?:crown corporation|public sector body|public body|"
                        r"public authority|local authority|municipality|statutory body|arm'?s[- ]length body)\b", re.I),
             "government", "Government / public-sector body"),
            (re.compile(subj + r"\s+(?:an?\s+)?(?:[\w-]+\s+){0,2}?(?:state|government)[- ](?:owned|controlled)\b", re.I),
             "state-owned", "State-owned enterprise"),
            (re.compile(r"\b(?:wholly|majority|fully|100%)[- ](?:owned|controlled)\s+by\s+(?:the\s+)?(?:government|state|"
                        r"crown|federal government|[A-Z][a-z]+ government|government of [A-Z][a-z]+)\b", re.I),
             "state-owned", "State-owned enterprise"),
            (re.compile(subj + r"\s+an?\s+(?:[\w-]+\s+){0,2}?(?:international|intergovernmental|supranational)\s+"
                        r"(?:organi[sz]ation|institution|body)\b", re.I),
             "international", "International / intergovernmental organisation"),
        )

    def add(self, field_, value, key, weight, page: FetchedPage | None, method, snip="", source="website", **kw):
        url = page.final_url if page else (self.ctx.seeds[0] if self.ctx.seeds else "")
        self.cands.append(Candidate(field_, value, key, weight, Evidence(source, url, method, snip[:400]), **kw))

    # -------------------------------------------------- per page
    def page(self, p: FetchedPage):
        pp, text, kf = p.parsed, p.parsed.text, KIND_FACTOR.get(p.kind, 0.85)
        self._jsonld(p)
        self._microdata(p)
        for label, value in self._pairs(pp):
            self._pair(p, label, value, kf)
        self._identifiers(p, text, kf)
        self._names(p, text, kf)
        self._jurisdiction(p, text, kf)
        self._offices(p, text, kf)
        self._legal_phrases(p, text, kf)
        self._regulators(p, text, kf)
        self._status(p, text, kf)
        self._government(p, text, kf)
        self._listings(p, text, kf)
        self._markets(p, text)
        boost = " ".join(filter(None, [pp.meta.get("description", ""), pp.meta.get("og:description", "")]))
        self._keywords(p, text + "\n" + pp.title, boost)
        self.copyright_years += [int(y) for y in COPYRIGHT_YEAR_RX.findall(text)]

    def _pairs(self, pp):
        seen, out = set(), []

        def put(label, value):
            k = (norm_label(label), value.strip())
            if k not in seen and value.strip():
                seen.add(k)
                out.append((label, value.strip()))

        for label, value in pp.pairs:
            put(label, value)
        lines = pp.lines
        for i, line in enumerate(lines):
            m = re.match(r"^([^:：]{2,40}?)\s*[:：]\s*(.{2,300})$", line)
            if m and label_target(m.group(1)):
                put(m.group(1), m.group(2))
                continue
            tgt = label_target(line) if len(line) <= 40 else None
            if tgt and i + 1 < len(lines) and not label_target(lines[i + 1]):
                value = [lines[i + 1]]
                if tgt in ("registered_office", "head_office", "address"):
                    for nxt in lines[i + 2:i + 4]:
                        if label_target(nxt) or len(nxt) > 100 or re.match(r"^[^:：]{2,40}[:：]", nxt):
                            break
                        if kb.country_in_text(value[-1]):
                            break
                        value.append(nxt)
                put(line, ", ".join(v.strip(" ,") for v in value))
        return out

    def _pair(self, p, label, value, kf):
        tgt = label_target(label)
        if not tgt:
            return
        value = re.sub(r"\s*(?:\(?\b(?:map|access map|google maps?|view map)\b\)?|→|›)\s*$", "", value, flags=re.I).strip()
        method = f"labelled field '{label.strip()}'"
        snip = f"{label.strip()}: {value}"
        if tgt in ("legal_name", "legal_name_generic"):
            generic = tgt == "legal_name_generic"
            m = re.match(r"^(.+?)\s*[(（](.+?)[)）]$", value)
            main, alias = (m.group(1), m.group(2)) if m and looks_like_name(m.group(2), True) else (value, None)
            if looks_like_name(main, need_suffix=generic):
                self.add("registered_legal_name", main.strip(), "name:" + kb.fold(main), (0.75 if generic else 0.9) * kf,
                         p, method, snip, aliases=["name:" + kb.fold(alias)] if alias else [])
        elif tgt == "registered_office" and looks_like_address(value):
            self.add("registered_office", value, address_key(value), 0.9 * kf, p, method, snip)
        elif tgt == "registered_seat" and 2 <= len(value) <= 80 and not re.search(r"https?:|@", value):
            self.add("registered_office", f"{value} (registered seat)", address_key(value), 0.8 * kf, p, method, snip,
                     note="the seat is the municipality of registration, not a street address")
        elif tgt == "head_office" and looks_like_address(value):
            self.add("operating_location", value, address_key(value), 0.85 * kf, p, method, snip)
            self.add("registered_office", value, address_key(value), 0.4 * kf, p, method + " (head office; often but not "
                     "always the registered seat)", snip)
        elif tgt == "address" and p.kind in ("corporate", "contact", "legal", "home") and looks_like_address(value):
            self.add("operating_location", value, address_key(value), 0.6 * kf, p, method, snip)
        elif tgt == "legal_form":
            form = kb.legal_form_of(value) or kb.legal_form_of("X " + value)
            key = "form:" + form["key"] if form else "form:text:" + kb.fold(value)
            self.add("legal_entity_type", form["type"] if form else value, key, 0.85 * kf, p, method, snip)
        elif tgt == "jurisdiction":
            code = kb.country_in_text(value)
            place = kb.lookup_place(value) or ((code, kb.country_name(code)) if code else None)
            if place:
                self.add("registration_jurisdiction", place[1], "jur:" + place[0], 0.85 * kf, p, method, snip)
        elif tgt == "industry" and 2 <= len(value) <= 400:
            self.add("industry_sector", f"Stated business: {value}", "stated:" + kb.fold(value)[:120], 0.8 * kf, p,
                     method, snip)
            self._keywords(p, "", value)
        elif tgt == "regulator":
            before = len(self.cands)
            self._regulators(p, f"regulated by {value}", kf, method=method)
            if len(self.cands) == before and len(value) <= 200:
                self.add("regulator_or_licence", value, "regtext:" + kb.fold(value), 0.7 * kf, p, method, snip)
        elif tgt == "listing":
            self.listings.append((value, p.final_url, snip))
        elif tgt == "status":
            low = value.lower()
            key = "active" if re.search(r"\b(?:active|operating|live|trading|registered)\b", low) else next(
                (v for k, v in STATUS_WORDS.items() if k in low), None)
            if key:
                self.add("entity_operating_status", STATUS_DISPLAY[key], key, 0.8 * kf, p, method, snip)
        elif tgt == "markets":
            codes = self._countries_in(value)
            for code in codes:
                self.add("markets_served", kb.country_name(code), "C:" + code, 0.75 * kf, p, method, snip)
            if not codes and len(value) <= 200:
                self.add("markets_served", value, "mtext:" + kb.fold(value), 0.6 * kf, p, method, snip)
        elif tgt == "registration":
            if find_identifiers(f"{label}: {value}"):
                self._identifiers(p, f"{label}: {value}", kf, already_labelled=True)
            elif re.search(r"\d{3}", value) and len(value) <= 60:
                self.add("corporate_registration_reference", f"{label.strip()}: {value}", "REG:" + kb.fold(value),
                         0.7 * kf, p, method, snip)

    def _countries_in(self, text):
        idx = kb.country_names_index()
        found = []
        for part in re.split(r"[,;/|、\n]| and ", kb.nfkc(text)):
            code = idx.get(kb.fold(part))
            if code and code not in found:
                found.append(code)
        return found

    def _jsonld(self, p):
        for node in p.parsed.jsonld:
            types = node.get("@type")
            types = [types] if isinstance(types, str) else [t for t in (types or []) if isinstance(t, str)]
            if not any(t.endswith(("Organization", "Corporation", "Business", "Organisation", "Service", "Store",
                                   "BankOrCreditUnion", "InsuranceAgency", "NGO")) for t in types):
                continue
            src = f"schema.org JSON-LD ({'/'.join(types)})"
            legal, name = _s(node.get("legalName")), _s(node.get("name"))
            if node.get("_via"):           # a group company / parent / unit: only its address is evidence (an office)
                addr = _address_from_jsonld(node.get("address"))
                if addr and looks_like_address(addr):
                    self.add("operating_location", addr, address_key(addr), 0.6, p, f"{src} {node['_via']} address",
                             addr, details=[f"office of {legal or name}"] if (legal or name) else [])
                continue
            if legal and looks_like_name(legal):
                self.add("registered_legal_name", legal, "name:" + kb.fold(legal), 0.85, p, src + " legalName",
                         f"legalName: {legal}")
            elif name and looks_like_name(name, need_suffix=True):
                self.add("registered_legal_name", name, "name:" + kb.fold(name), 0.55, p, src + " name", f"name: {name}")
            for prop, label in (("leiCode", "LEI"), ("taxID", "Tax ID"), ("vatID", "VAT"), ("duns", "DUNS"),
                                ("iso6523Code", "ISO 6523")):
                v = _s(node.get(prop))
                if v:
                    ids = find_identifiers(f"{label} {v}")
                    if ids:
                        for i in ids:
                            self._identifier_candidate(p, i, 0.85, f"{src} {prop}", f"{prop}: {v}")
                    else:
                        self.add("corporate_registration_reference", f"{prop}: {v}", f"{prop}:{kb.fold(v)}", 0.7, p,
                                 f"{src} {prop}", f"{prop}: {v}")
            addr = _address_from_jsonld(node.get("address"))
            if addr and looks_like_address(addr):
                self.add("operating_location", addr, address_key(addr), 0.75, p, src + " address", addr)
            for prop in ("naics", "isicV4"):
                v = _s(node.get(prop))
                if v:
                    self.add("industry_sector", f"{prop.upper()} {v}", f"{prop}:{v}", 0.85, p, f"{src} {prop}", f"{prop}: {v}")
            if node.get("description"):
                self._keywords(p, "", _s(node.get("description")) or "")
            area = node.get("areaServed")
            for a in (area if isinstance(area, list) else [area] if area else []):
                a = _s(a.get("name") if isinstance(a, dict) else a)
                place = kb.lookup_place(a or "")
                if place:
                    self.add("markets_served", kb.country_name(place[0][:2]), "C:" + place[0][:2], 0.8, p,
                             src + " areaServed", f"areaServed: {a}")
            if "GovernmentOrganization" in types:
                self.add("government_public_sector_status", "Government / public-sector body", "government", 0.8, p,
                         src + " @type GovernmentOrganization", f"@type: {'/'.join(types)}")

    def _microdata(self, p):
        for prop, v in p.parsed.microdata:
            if prop == "legalName" and looks_like_name(v):
                self.add("registered_legal_name", v, "name:" + kb.fold(v), 0.85, p, "microdata itemprop=legalName", v)
            elif prop in MICRODATA_ID_LABELS:
                for i in find_identifiers(f"{MICRODATA_ID_LABELS[prop]} {v}"):
                    self._identifier_candidate(p, i, 0.85, f"microdata itemprop={prop}", v)
            elif prop == "address" and looks_like_address(v):
                self.add("operating_location", v, address_key(v), 0.7, p, "microdata itemprop=address", v)

    def _identifiers(self, p, text, kf, already_labelled=False):
        for i in find_identifiers(text):
            labelled = i.labelled or already_labelled
            if i.scheme == "REG":
                w = 0.6
            elif i.scheme == "VAT":
                w = 0.75
            elif labelled:
                w = 0.9 if i.checksum else 0.8
            else:
                w = 0.65 if i.checksum else 0.45
            self._identifier_candidate(p, i, w * kf, "identifier pattern" + (" + check digit" if i.checksum else ""),
                                       snippet(text, i.start, i.end))

    def _identifier_candidate(self, p, i: Identifier, w, method, snip):
        details = [f"{k}: {v}" for k, v in i.extra.items() if k not in ("vat_country",)]
        value = i.display() + (" (check digit valid)" if i.checksum else "")
        self.add("corporate_registration_reference", value, f"{i.scheme}:{i.value}", w, p, method, snip, details=details)
        self.identifiers.append((i, p.final_url))
        if i.country and i.scheme not in ("REG", "DUNS", "LEI"):
            jw = 0.45 if i.scheme == "VAT" else 0.8 if (i.labelled and i.scheme in REGISTRY_SCHEMES) else \
                0.7 if (i.labelled or i.checksum) else 0.5
            what = "tax registration country" if i.scheme == "VAT" else "registry of the identifier"
            self.add("registration_jurisdiction", kb.country_name(i.country), "jur:" + i.country, jw, p,
                     f"implied by {i.label} ({what})", snip)
        if i.scheme == "IN-CIN":
            cls = i.extra["class"]
            if cls in ("GOI", "SGC"):
                self.add("government_public_sector_status", "Government company (India)", "government", 0.9, p,
                         f"CIN ownership class {cls} ({i.extra['class_meaning']})", snip)
            else:
                self.add("government_public_sector_status", "Not a government company (per CIN class)", "non-government",
                         0.7, p, f"CIN ownership class {cls} ({i.extra['class_meaning']})", snip)
            form_key, label = CIN_FORMS.get(cls, ("IN-" + cls, None))
            label = label or (kb.form_type(form_key) if form_key in ("IN-LTD", "PVT-LTD") else
                              i.extra["class_meaning"][:1].upper() + i.extra["class_meaning"][1:])
            self.add("legal_entity_type", label, "form:" + form_key, 0.85, p, f"CIN class {cls}", snip)
            if i.extra["listed"]:
                self.listings.append(("listed (CIN prefix L)", p.final_url, snip))

    def _names(self, p, text, kf):
        for m in COPYRIGHT_RX.finditer(text):
            name = clean_name(m.group("name"))
            if looks_like_name(name):
                self.add("registered_legal_name", name, "name:" + kb.fold(name), 0.6 * kf, p, "copyright notice",
                         snippet(text, m.start(), m.end(), 40))
        for rx, method, w in ((OPERATED_RX, "'operated by' statement", 0.8), (COMPANY_REF_RX, "defined as 'the Company'", 0.8),
                              (IS_COMPANY_RX, "'... is a company registered/incorporated' statement", 0.8)):
            for m in rx.finditer(text):
                name = clean_name(m.group("name"))
                if looks_like_name(name, need_suffix=True):
                    self.add("registered_legal_name", name, "name:" + kb.fold(name), w * kf, p, method,
                             snippet(text, m.start(), m.end()))
        # imprint layout: the operator's name alone on a line, its address below. Only the first such line of a legal
        # notice counts, so a list of group companies does not turn each company into a legal-name candidate.
        if p.kind == "legal":
            lines = p.parsed.lines
            for n, line in enumerate(lines):
                if not (NAME_LINE_RX.fullmatch(line) and looks_like_name(line, need_suffix=True)):
                    continue
                self.add("registered_legal_name", line, "name:" + kb.fold(line), 0.7 * kf, p,
                         "entity name on its own line (legal notice layout)", " / ".join(lines[n:n + 4]))
                addr, _ = _address_under(lines, n)
                if addr:
                    self.add("operating_location", addr, address_key(addr), 0.65 * kf, p,
                             "address under the entity name (legal notice layout)", f"{line} / {addr}")
                    self.add("registered_office", addr, address_key(addr), 0.5 * kf, p,
                             "address under the entity name on the legal notice (usually the address for service)",
                             f"{line} / {addr}")
                break
        if p.kind in ("legal", "corporate", "home", "investor", "regulatory"):
            for m in NAME_RX.finditer(text):
                name = clean_name(m.group(0))
                if looks_like_name(name, need_suffix=True) and len(name.split()) >= 2:
                    self.mentions[name].append((p, snippet(text, m.start(), m.end(), 60)))

    def _jurisdiction(self, p, text, kf):
        for m in JUR_RX.finditer(text):
            place = kb.lookup_place(m.group("place")) or kb.lookup_place(" ".join(m.group("place").split()[:2])) \
                or kb.lookup_place(m.group("place").split()[0])
            if place:
                self.add("registration_jurisdiction", place[1], "jur:" + place[0], 0.85 * kf, p,
                         "'registered/incorporated in' statement", snippet(text, m.start(), m.end()))
        for m in DEMONYM_RX.finditer(text):
            place = kb.lookup_place(m.group("place"), prefer_states=True)
            if place and m.group("place") not in ("The", "This"):
                self.add("registration_jurisdiction", place[1], "jur:" + place[0], 0.75 * kf, p,
                         f"'a {m.group('place')} company' wording", snippet(text, m.start(), m.end()))

    def _offices(self, p, text, kf):
        for m in REG_OFFICE_RX.finditer(text):
            addr = ADDR_STOP.split(m.group("addr"), maxsplit=1)[0].strip(" .,:")
            if looks_like_address(addr):
                self.add("registered_office", addr, address_key(addr), 0.85 * kf, p, "'registered office' statement",
                         snippet(text, m.start(), m.end()))
        for m in HQ_RX.finditer(text):
            place = re.split(r"\s+(?:and|with|where|since|which)\s+", m.group("place"))[0].strip(" ,")
            if 2 <= len(place) <= 80:
                self.add("operating_location", place, address_key(place), 0.6 * kf, p, "'headquartered in' statement",
                         snippet(text, m.start(), m.end()))
        if p.kind in ("contact", "corporate", "legal", "home"):
            for a in p.parsed.addresses:
                if looks_like_address(a):
                    self.add("operating_location", a, address_key(a), 0.55 * kf, p, "<address> element", a)
        if p.kind == "contact":       # office directory: an office name, then its address lines
            lines, n = p.parsed.lines, 1
            while n < len(lines):
                if not re.search(r"\d", lines[n]) or re.search(r"\d", lines[n - 1]) or len(lines[n - 1]) > 80:
                    n += 1
                    continue
                addr, used = _address_under(lines, n - 1)
                if addr and (kb.country_in_text(addr) or POSTCODE_RX.search(addr)):
                    heading = next((h for h in (lines[n - 1], lines[n - 2] if n > 1 else "")
                                    if h and not kb.lookup_place(h)), lines[n - 1])
                    self.add("operating_location", addr, address_key(addr), 0.6 * kf, p,
                             "office listed on a locations/contact page", f"{heading} / {addr}",
                             details=[f"office: {heading}"])
                    n += used
                n += 1

    def _legal_phrases(self, p, text, kf):
        for m in LEGAL_PHRASE_RX.finditer(text):
            phrase = m.group("form").lower()
            key = LEGAL_PHRASE_KEYS.get(phrase.replace("–", "-"))
            label = kb.form_type(key) if key else phrase[:1].upper() + phrase[1:]
            self.add("legal_entity_type", label, "form:" + (key or "text:" + kb.fold(phrase)), 0.75 * kf, p,
                     "legal-form wording", snippet(text, m.start(), m.end()))

    def _regulators(self, p, text, kf, method=None):
        regs = kb.regulators()
        folded = text.casefold()
        for r, name_rx, acr_rx in regs:
            hits = []
            if any(n.casefold() in folded for n in r["names"]):       # cheap pre-check before the regex
                hits += [(m, False) for m in name_rx.finditer(text)]
            if acr_rx and any(a in text for a in r["acronyms"]):
                hits += [(m, True) for m in acr_rx.finditer(text)]
            for m, is_acr in hits:
                window = re.split(r"(?<=[.;。])\s+|\n", text[max(0, m.start() - 140):m.start()])[-1]
                cued = bool(REG_CUE.search(window))
                if is_acr and not cued:
                    continue
                w = (0.85 if cued else 0.45 if p.kind in ("legal", "regulatory", "corporate") else 0.3) * kf
                around = text[max(0, m.start() - 150):m.end() + 200]
                details = [f"{lab}: {lm.group(1)}" for rx, rkey, lab in LICENCE_RXS if rkey in (r["key"], None)
                           for lm in rx.finditer(around)]
                if cued:
                    cue = list(REG_CUE.finditer(window))[-1].group(0)      # the cue nearest to the regulator's name
                    details.insert(0, "stated: " + " ".join(cue.split()))
                self.add("regulator_or_licence", kb.regulator_display(r), "reg:" + r["key"], w, p,
                         method or ("regulator named after a regulatory cue" if cued else "regulator named (no cue)"),
                         snippet(text, m.start(), m.end()), details=details)
                if cued:
                    for sector in r["sectors"]:
                        a = kb.activity_for_sector(sector)
                        if a:
                            self.add("regulated_activity", a["activity"], "act:" + a["key"],
                                     (0.5 if sector == "financial" else 0.6) * kf, p,
                                     f"implied by regulator {kb.regulator_display(r)}", snippet(text, m.start(), m.end()))
        for rx, rkey, label in LICENCE_RXS:
            if rkey is None:
                continue
            for m in rx.finditer(text):
                r = next(x for x, _, _ in regs if x["key"] == rkey)
                self.add("regulator_or_licence", kb.regulator_display(r), "reg:" + rkey, 0.85 * kf, p,
                         f"{label} pattern", snippet(text, m.start(), m.end()), details=[f"{label}: {m.group(1)}"])
                for sector in r["sectors"]:
                    a = kb.activity_for_sector(sector)
                    if a:
                        self.add("regulated_activity", a["activity"], "act:" + a["key"], 0.6 * kf, p,
                                 f"implied by {label}", snippet(text, m.start(), m.end()))

    def _status(self, p, text, kf):
        for rx, fixed in STATUS_RXS:
            for m in rx.finditer(text):
                key = fixed or STATUS_WORDS.get(m.group("s").lower(), "insolvency")
                self.add("entity_operating_status", STATUS_DISPLAY[key], key, (0.6 if p.kind != "other" else 0.45) * kf,
                         p, "status statement on the website (verify in the official registry)",
                         snippet(text, m.start(), m.end()))

    def _government(self, p, text, kf):
        for rx, key, label in self.gov_rxs:
            for m in rx.finditer(text):
                self.add("government_public_sector_status", label, key, (0.65 if p.kind != "other" else 0.5) * kf, p,
                         "self-description on the website", snippet(text, m.start(), m.end()))

    def _listings(self, p, text, kf):
        for rx in LISTING_RXS:
            for m in rx.finditer(text):
                self.listings.append((" ".join(m.group("x").split()), p.final_url, snippet(text, m.start(), m.end())))

    def _markets(self, p, text):
        idx = kb.country_names_index()
        for lang, href in p.parsed.alternates:
            parts = re.split(r"[-_]", lang)
            if len(parts) >= 2 and len(parts[-1]) == 2 and kb.is_country(parts[-1].upper()):
                code = parts[-1].upper()
                self.add("markets_served", kb.country_name(code), "C:" + code, 0.75, p,
                         f"localised site for {lang} (hreflang alternate)", f"hreflang={lang} -> {href}")
        selector = {}
        for link in p.parsed.links:
            head = re.split(r"\s+[-–|/(:]\s*|\s*[(（]", kb.nfkc(link.text).strip())[0]
            code = idx.get(kb.fold(head))
            if not code or len(head) > 40:
                continue
            host = urlsplit(link.url).hostname or ""
            token = brand_token(site_key(host))
            if site_key(host) in self.ctx.site_keys or any(token.startswith(b) for b in self.ctx.brands if len(b) >= 4):
                selector.setdefault(code, (link.text, link.url))
        if len(selector) >= 3:
            for code, (txt, url) in selector.items():
                self.add("markets_served", kb.country_name(code), "C:" + code, 0.7, p, "country/region selector link",
                         f"{txt} -> {url}")
        for m in MARKET_COUNT_RX.finditer(text):
            self.add("markets_served", f"Global (stated presence in {m.group('n')}+ countries/regions)", "stated-count",
                     0.6, p, "stated number of countries", snippet(text, m.start(), m.end()), details=[m.group(0)])

    def _keywords(self, p, text, boost_text=""):
        for table, rx_list in ((self.industry_hits, kb.industries()), (self.activity_hits, kb.activities())):
            for item, rx in rx_list:
                if rx is None:
                    continue
                for src, boosted in ((text, False), (boost_text, True)):
                    for m in rx.finditer(src):
                        h = table.setdefault(item["key"], {"item": item, "terms": defaultdict(int), "urls": set(),
                                                           "boost": False, "first": None})
                        h["terms"][kb.fold(m.group(0))] += 1
                        h["urls"].add(p.final_url)
                        h["boost"] |= boosted
                        if h["first"] is None or (boosted and not h["first"][2]):
                            h["first"] = (p, snippet(src, m.start(), m.end(), 70), boosted)

    # -------------------------------------------------- after all pages
    def finish(self, pages: list[FetchedPage]):
        first = pages[0] if pages else None
        for name, seen in self.mentions.items():
            if len(seen) >= 2:
                self.add("registered_legal_name", name, "name:" + kb.fold(name), min(0.5, 0.3 + 0.02 * len(seen)), seen[0][0],
                         f"entity name mentioned {len(seen)} times", seen[0][1])
        for key, h in self.industry_hits.items():
            terms = sorted(h["terms"], key=lambda t: -h["terms"][t])
            w = min(0.75, 0.3 + 0.1 * (len(terms) - 1) + 0.03 * (len(h["urls"]) - 1) + (0.15 if h["boost"] else 0))
            item = h["item"]
            self.add("industry_sector", f"{item['industry']} ({item['sector']})", "ind:" + key, w, h["first"][0],
                     f"keywords: {', '.join(terms[:6])} ({sum(h['terms'].values())} hits on {len(h['urls'])} pages)",
                     h["first"][1], details=[f"GICS sector: {item['sector']}"])
        for key, h in self.activity_hits.items():
            terms = sorted(h["terms"], key=lambda t: -h["terms"][t])
            w = min(0.65, 0.25 + 0.1 * (len(terms) - 1) + 0.03 * (len(h["urls"]) - 1) + (0.1 if h["boost"] else 0))
            self.add("regulated_activity", h["item"]["activity"], "act:" + key, w, h["first"][0],
                     f"keywords: {', '.join(terms[:6])} ({sum(h['terms'].values())} hits on {len(h['urls'])} pages)",
                     h["first"][1])
        if self.listings:
            uniq = list(dict.fromkeys(x for x, _, _ in self.listings))
            _, url, snip = self.listings[0]
            page = next((q for q in pages if q.final_url == url), first)
            self.add("government_public_sector_status", "Private sector (publicly listed company)", "private-listed", 0.6,
                     page, "stock-exchange listing found on the website", snip, details=uniq[:8])
            self.add("entity_operating_status", "Active", "active", 0.55, page, "publicly listed (stock-exchange listing)",
                     snip, details=uniq[:8])
        if first:
            latest = max(self.copyright_years, default=None)
            recent = latest is not None and latest >= self.ctx.today.year - 1
            self.add("entity_operating_status", "Active", "active", 0.45 if recent else 0.35, first,
                     "website is live" + (f" and maintained (latest copyright year {latest})" if latest else ""),
                     f"{len(pages)} page(s) fetched")
            hosts = {(urlsplit(q.final_url).hostname or "").lower() for q in pages}
            for host in sorted(hosts):
                if host.endswith(GOV_DOMAINS):
                    self.add("government_public_sector_status", "Government / public-sector body", "government", 0.85,
                             first, "official government domain", host)
                elif host.endswith(INTL_DOMAINS):
                    self.add("government_public_sector_status", "International / intergovernmental organisation",
                             "international", 0.8, first, "international-organisation domain", host)
            for seed in self.ctx.seeds:
                tld = (urlsplit(seed).hostname or "").rsplit(".", 1)[-1].lower()
                code = {"uk": "GB"}.get(tld, tld.upper())
                if tld not in GENERIC_TLDS and kb.is_country(code):
                    self.add("markets_served", kb.country_name(code), "C:" + code, 0.45, first, "country-code domain", seed)
        return self.cands


def _s(v):
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        v = v.get("name") or v.get("@value")
    return " ".join(str(v).split()) if v not in (None, "") else None


def _address_from_jsonld(a):
    if isinstance(a, list):
        a = a[0] if a else None
    if isinstance(a, str):
        return " ".join(a.split())
    if isinstance(a, dict):
        country = a.get("addressCountry")
        country = country.get("name") if isinstance(country, dict) else country
        parts = [a.get("streetAddress"), a.get("addressLocality"), a.get("addressRegion"), a.get("postalCode"), country]
        return ", ".join(" ".join(str(x).split()) for x in parts if x)
    return None


def extract_website(pages: list[FetchedPage], ctx: SiteContext) -> tuple[list[Candidate], list[tuple[Identifier, str]]]:
    c = _Collector(ctx)
    for p in pages:
        c.page(p)
    return c.finish(pages), c.identifiers
