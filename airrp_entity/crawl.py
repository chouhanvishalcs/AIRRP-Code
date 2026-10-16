"""Find and fetch the pages of a site that carry entity facts (legal notice, corporate data, IR, contact, regions)."""
from __future__ import annotations

import hashlib
import heapq
import os
import pathlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from .fetch import Fetcher, decode
from .htmlparse import ParsedPage, parse_html

# second-level public suffixes, enough to find the registrable domain of most corporate sites
MULTI_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "ltd.uk", "plc.uk", "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp", "lg.jp",
    "com.au", "net.au", "org.au", "gov.au", "co.in", "net.in", "org.in", "gov.in", "nic.in", "co.nz", "govt.nz",
    "com.br", "gov.br", "com.cn", "gov.cn", "com.hk", "gov.hk", "com.sg", "gov.sg", "com.mx", "gob.mx", "co.kr", "go.kr",
    "co.za", "gov.za", "com.tr", "com.tw", "co.id", "com.my", "co.th", "com.ar", "com.co", "com.pe", "co.il", "com.ph",
    "com.vn", "com.sa", "com.eg", "com.pk", "com.ng", "gc.ca", "gouv.fr", "gob.es", "gv.at",
}
STRONG = ("imprint", "impressum", "legal notice", "legal-notice", "mentions legales", "mentions-legales", "aviso legal",
          "aviso-legal", "note legali", "colofon", "corporate data", "corporate-data", "corporatedata", "company data",
          "company profile", "company-profile", "corporate profile", "corporate-profile", "company information",
          "company-information", "company info", "corporate information", "corporate-information", "corporate info",
          "corporateinfo", "company overview", "company-overview", "at a glance", "facts and figures", "fact sheet",
          "会社概要", "企業情報", "会社情報", "企業概要", "会社案内", "outline", "regulatory", "disclosure", "licen",
          "important information", "who we are", "who-we-are", "about us", "about-us", "aboutus",
          "country/region", "country region", "select a country", "select your country", "choose your country",
          "select-country", "selectcountry", "country-selector", "region selector", "select region", "our locations",
          "office locations", "global network", "worldwide")
MEDIUM = ("about", "company", "corporate", "profile", "overview", "legal", "terms", "conditions", "investor", "/ir/",
          "stock information", "shareholder", "contact", "offices", "headquarter", "location", "business", "segments",
          "what we do", "our businesses", "governance", "countries", "regions", "global")
WEAK = ("privacy", "cookie", "sitemap", "history", "facts", "data")
NEGATIVE = ("news", "press-release", "pressrelease", "/blog", "career", "jobs", "recruit", "/product", "/shop", "/store",
            "/cart", "login", "signin", "sign-in", "register", "/support/", "download", "/video", "/event", "/search",
            "/tag/", "/category/", ".jpg", ".png", ".gif", ".mp4", "?page=", "/feed")
DOC_EXT = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip")
DOC_HINT = re.compile(r"annual[\s_-]*report|integrated[\s_-]*report|\b20-?f\b|\b10-?k\b|securities[\s_-]*report|yuho|"
                      r"financial[\s_-]*statements|articles[\s_-]*of[\s_-]*(?:incorporation|association)|certificate|"
                      r"licen[cs]e|prospectus|有価証券報告書|統合報告書|定款", re.I)
LOCALE = re.compile(r"^[a-z]{2}(?:[-_][a-z]{2})?$", re.I)
KIND_RULES = (
    ("legal", re.compile(r"imprint|impressum|legal|terms|conditions|mentions|aviso|disclaimer|privacy|cookie|colofon|"
                         r"note[- ]legali|利用規約|免責", re.I)),
    ("regulatory", re.compile(r"regulat|disclosure|licen[cs]|complaint|important[- ]information|compliance", re.I)),
    ("corporate", re.compile(r"about|company|corporate|profile|overview|who[- ]we[- ]are|outline|at[- ]a[- ]glance|facts|"
                             r"会社|企業|governance|history|business|segment|organi[sz]ation", re.I)),
    ("investor", re.compile(r"investor|/ir(?:/|$)|stock|shareholder|financial", re.I)),
    ("contact", re.compile(r"contact|location|office|address|access|worldwide|global|countr|region", re.I)),
)
WELL_KNOWN = ("about", "about-us", "company", "corporate", "legal", "legal-notice", "imprint", "impressum", "contact", "terms")


def site_key(host: str) -> str:
    labels = (host or "").lower().strip(".").split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in MULTI_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def brand_token(key: str) -> str:
    return key.split(".")[0]


def normalize_url(url: str) -> str:
    p = urlsplit(url)
    netloc = (p.hostname or "").lower()
    if p.port and not ((p.scheme == "http" and p.port == 80) or (p.scheme == "https" and p.port == 443)):
        netloc += f":{p.port}"
    return urlunsplit((p.scheme.lower(), netloc, p.path or "/", p.query, ""))


def is_document(url: str) -> bool:
    return urlsplit(url).path.lower().endswith(DOC_EXT)


def _locale_of(url: str) -> str | None:
    seg = urlsplit(url).path.strip("/").split("/")[0]
    return seg.lower() if LOCALE.match(seg or "") else None


def link_score(url: str, text: str, seed_locale: str | None = None) -> int:
    hay = (unquote(urlsplit(url).path) + " " + urlsplit(url).query + " " + (text or "")).lower().replace("_", "-")
    score = 3 * sum(k in hay for k in STRONG) + 2 * sum(k in hay for k in MEDIUM) + sum(k in hay for k in WEAK)
    score -= 3 * sum(k in hay for k in NEGATIVE)
    loc = _locale_of(url)
    if seed_locale and loc:
        score += 1 if loc == seed_locale else -3
    return score


def page_kind(url: str, title: str, depth: int) -> str:
    path = urlsplit(url).path
    if depth == 0 and path.strip("/") in ("", *(p for p in [_locale_of(url) or ""] if p)):
        return "home"
    hay = unquote(path) + " " + (title or "")
    for kind, rx in KIND_RULES:
        if rx.search(hay):
            return kind
    return "other"


@dataclass
class FetchedPage:
    url: str
    final_url: str
    status: int
    depth: int
    kind: str
    parsed: ParsedPage
    sha256: str
    fetched_at: str
    rendered: bool = False

    def as_dict(self) -> dict:
        return {"url": self.final_url, "requested": self.url, "status": self.status, "kind": self.kind,
                "title": self.parsed.title, "sha256": self.sha256, "fetched_at": self.fetched_at,
                "rendered": self.rendered}


@dataclass
class CrawlResult:
    pages: list[FetchedPage] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    documents: list[dict] = field(default_factory=list)
    site_keys: set[str] = field(default_factory=set)
    related_sites: list[dict] = field(default_factory=list)   # other domains of the same brand (country sites ...)


def crawl(seeds, fetcher: Fetcher, max_pages: int = 25, extra_domains=(), renderer=None, log=None) -> CrawlResult:
    res = CrawlResult(site_keys={site_key(urlsplit(u).hostname or "") for u in seeds} | {site_key(d) for d in extra_domains})
    seed_locale = _locale_of(seeds[0]) if seeds else None
    heap: list = []
    seen: set[str] = set()
    doc_seen: set[str] = set()
    counter = [0]

    def push(url, depth, score, via=""):
        counter[0] += 1
        heapq.heappush(heap, (-score, depth, counter[0], url, via))

    def in_scope(url):
        p = urlsplit(url)
        return p.scheme in ("http", "https") and site_key(p.hostname or "") in res.site_keys

    def add_document(url, text, found_on):
        n = normalize_url(url)
        if n not in doc_seen and len(res.documents) < 30:
            doc_seen.add(n)
            res.documents.append({"url": url, "text": text, "found_on": found_on})

    def drain():
        while heap and len(res.pages) < max_pages:
            _, depth, _, url, via = heapq.heappop(heap)
            norm = normalize_url(url)
            if norm in seen:
                continue
            seen.add(norm)
            if depth and not in_scope(url):
                continue
            if is_document(url):
                add_document(url, via, "")
                continue
            if log:
                log(f"fetch {url}")
            resp = fetcher.get(url)
            rendered = None
            if not resp.ok and renderer and resp.status >= 400:    # refused by the server: try a real browser
                rendered = renderer.render(url)
            if not resp.ok and not rendered:
                res.skipped.append({"url": url, "reason": (resp.error or f"HTTP {resp.status}")
                                    + (" (also refused to the browser)" if renderer and resp.status >= 400 else "")})
                continue
            final_url = rendered[1] if rendered else resp.url
            final = normalize_url(final_url)
            if final != norm:
                if final in seen and depth:
                    continue
                seen.add(final)
            if depth == 0:          # a seed may redirect to the organisation's main domain (e.g. brand.net -> brand.com)
                res.site_keys.add(site_key(urlsplit(final_url).hostname or ""))
            elif not in_scope(final_url):
                res.skipped.append({"url": url, "reason": f"redirected off-site to {final_url}"})
                continue
            if not rendered and resp.content_type == "application/pdf":
                add_document(resp.url, via, "")
                continue
            if not rendered and not resp.is_html:
                res.skipped.append({"url": url, "reason": f"not HTML ({resp.content_type or 'unknown type'})"})
                continue
            body = rendered[0].encode("utf-8") if rendered else resp.body
            parsed = parse_html(rendered[0] if rendered else resp.text(), final_url)
            if renderer and not rendered and (len(parsed.text) < 600 or len(parsed.links) < 5):
                rendered = renderer.render(final_url)          # little text: probably built by JavaScript
                if rendered:
                    final_url, body = rendered[1], rendered[0].encode("utf-8")
                    parsed = parse_html(rendered[0], final_url)
            res.pages.append(FetchedPage(url, final_url, resp.status if resp.ok else 200, depth,
                                         page_kind(final_url, parsed.title, depth), parsed,
                                         hashlib.sha256(body).hexdigest(),
                                         datetime.now(timezone.utc).isoformat(timespec="seconds"), bool(rendered)))
            for link in parsed.links:
                score = link_score(link.url, link.text, seed_locale)
                if is_document(link.url):
                    if in_scope(link.url) and (DOC_HINT.search(link.url + " " + link.text) or score >= 2):
                        add_document(link.url, link.text, final_url)
                    continue
                if not in_scope(link.url) or normalize_url(link.url) in seen:
                    continue
                if (depth == 0 and score >= 2) or (depth == 1 and score >= 3):
                    push(link.url, depth + 1, score, link.text)

    for u in seeds:
        push(u, 0, 1000)
    drain()

    relevant = sum(p.kind not in ("home", "other") for p in res.pages)
    if seeds and relevant < 3 and len(res.pages) < max_pages and res.pages:
        base = res.pages[0].final_url
        for url in _from_sitemaps(fetcher, base, seed_locale):
            push(url, 1, link_score(url, "", seed_locale), "sitemap")
        if relevant < 2:
            prefix = f"/{seed_locale}/" if seed_locale else "/"
            for path in WELL_KNOWN:
                push(urljoin(base, prefix + path), 1, 2, "well-known path")
        drain()
    res.related_sites = related_sites(res.pages, res.site_keys)
    return res


def related_sites(pages: list[FetchedPage], site_keys: set[str], limit: int = 300) -> list[dict]:
    """Links to other domains whose name starts with the organisation's brand (sony.co.uk, sonypictures.com): usually
    country sites or subsidiaries. Listed for --crawl-subsidiaries, never crawled as part of the organisation."""
    brands = {brand_token(k) for k in site_keys if len(brand_token(k)) >= 4}
    out, seen = [], set()
    for page in pages:
        for link in page.parsed.links:
            p = urlsplit(link.url)
            key = site_key(p.hostname or "")
            if (p.scheme in ("http", "https") and key not in site_keys and key not in seen
                    and any(brand_token(key).startswith(b) for b in brands)):
                seen.add(key)
                out.append({"url": link.url, "site": key, "text": " ".join(link.text.split())[:80],
                            "found_on": page.final_url})
    return out[:limit]


SAVED_FROM = re.compile(r"<!--\s*saved from url=\(\d+\)\s*(\S+?)\s*-->", re.I)
HTML_FILES = (".html", ".htm", ".xhtml")


def pages_from_files(paths) -> CrawlResult:
    """Pages saved from a browser (File > Save page as), for sites that refuse automated requests. A page's address is
    taken from the browser's 'saved from url' comment, else its canonical link or og:url, else the file itself."""
    files = []
    for path in paths:
        if os.path.isdir(path):
            files += sorted(os.path.join(d, f) for d, _, names in os.walk(path) for f in names
                            if f.lower().endswith(HTML_FILES))
        else:
            files.append(path)
    res = CrawlResult()
    for i, path in enumerate(files):
        with open(path, "rb") as fh:
            body = fh.read()
        html = decode(body)
        first = parse_html(html, "https://localhost/")
        m = SAVED_FROM.search(html[:4096])
        found = [m.group(1) if m else None, first.meta.get("canonical"), first.meta.get("og:url")]
        url = next((u for u in found if u and re.match(r"https?://(?!localhost/)", u)), None) \
            or pathlib.Path(path).resolve().as_uri()
        parsed = parse_html(html, url)
        parts = urlsplit(url)
        depth = 0 if parts.path.strip("/") in ("", _locale_of(url) or "") and parts.scheme != "file" else 1
        mtime = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).isoformat(timespec="seconds")
        res.pages.append(FetchedPage(path, url, 200, depth, page_kind(url, parsed.title + " " + os.path.basename(path), depth),
                                     parsed, hashlib.sha256(body).hexdigest(), mtime))
        if parts.scheme in ("http", "https"):
            res.site_keys.add(site_key(parts.hostname or ""))
    seen = set()
    for page in res.pages:
        for link in page.parsed.links:
            if is_document(link.url) and DOC_HINT.search(link.url + " " + link.text) and link.url not in seen:
                seen.add(link.url)
                res.documents.append({"url": link.url, "text": link.text, "found_on": page.final_url})
    res.related_sites = related_sites(res.pages, res.site_keys)
    return res


def _from_sitemaps(fetcher: Fetcher, base: str, seed_locale: str | None, limit: int = 10) -> list[str]:
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(base))
    maps = fetcher.sitemaps(base) or [origin + "/sitemap.xml"]
    urls, fetched = [], 0
    while maps and fetched < 4 and len(urls) < 20000:
        sm = maps.pop(0)
        fetched += 1
        r = fetcher.get(sm, accept="application/xml,text/xml,*/*")
        if not r.ok:
            continue
        body = r.text()
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body)
        if "<sitemapindex" in body:
            maps.extend(locs[:3])
        else:
            urls.extend(locs)
    scored = sorted(((link_score(u, "", seed_locale), u) for u in urls if not is_document(u)), reverse=True)
    return [u for s, u in scored[:limit] if s >= 3]
