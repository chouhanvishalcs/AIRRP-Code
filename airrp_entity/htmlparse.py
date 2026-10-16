"""HTML -> visible text, links, meta, JSON-LD, hreflang alternates, microdata and label/value pairs (stdlib HTMLParser)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urldefrag

BLOCK = {"address", "article", "aside", "blockquote", "br", "caption", "dd", "details", "div", "dl", "dt", "fieldset",
         "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main",
         "nav", "ol", "option", "p", "pre", "section", "summary", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul"}
SKIP = {"script", "style", "noscript", "template", "svg", "iframe", "canvas", "object", "select"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
MICRODATA = {"legalName", "name", "address", "streetAddress", "postalCode", "addressLocality", "addressRegion",
             "addressCountry", "taxID", "vatID", "leiCode", "duns", "naics", "isicV4", "areaServed", "foundingDate"}


@dataclass
class Link:
    url: str
    text: str


@dataclass
class ParsedPage:
    url: str
    title: str = ""
    lang: str = ""
    text: str = ""
    links: list[Link] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)
    jsonld: list[dict] = field(default_factory=list)
    alternates: list[tuple[str, str]] = field(default_factory=list)   # (hreflang, absolute url)
    pairs: list[tuple[str, str]] = field(default_factory=list)        # (label, value) from th/td and dt/dd
    addresses: list[str] = field(default_factory=list)                # <address> blocks
    microdata: list[tuple[str, str]] = field(default_factory=list)    # (itemprop, value)

    @property
    def lines(self) -> list[str]:
        return self.text.split("\n")


class _Capture:
    __slots__ = ("tag", "kind", "key", "depth", "buf")

    def __init__(self, tag, kind, key=None):
        self.tag, self.kind, self.key, self.depth, self.buf = tag, kind, key, 0, []

    def text(self):
        raw = "".join(self.buf)
        if self.kind == "address":     # keep the line structure of an address as commas
            parts = (" ".join(p.split()).strip(" ,") for p in raw.split("\n"))
            return ", ".join(p for p in parts if p)
        return " ".join(raw.split())


class _Parser(HTMLParser):
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.page = ParsedPage(url=url)
        self.base = url
        self.out: list[str] = []
        self.skip = 0
        self.caps: list[_Capture] = []
        self.jsonld_buf: list[str] | None = None
        self.tables: list[dict] = []     # stack of {"row": [..] | None, "cell": _Capture | None}
        self.dl_label: list[str | None] = []

    # -- helpers
    def _open(self, tag, kind, key=None):
        cap = _Capture(tag, kind, key)
        self.caps.append(cap)
        return cap

    def _close(self, cap):
        if cap in self.caps:
            self.caps.remove(cap)
        value = cap.text()
        if cap.kind == "title" and not self.page.title:
            self.page.title = value
        elif cap.kind == "a" and cap.key:
            self.page.links.append(Link(cap.key, value))
        elif cap.kind == "address" and value:
            self.page.addresses.append(value)
        elif cap.kind == "itemprop" and value:
            self.page.microdata.append((cap.key, value))
        elif cap.kind == "cell" and self.tables and self.tables[-1]["row"] is not None:
            self.tables[-1]["row"].append(value)
        elif cap.kind == "dt" and self.dl_label:
            self.dl_label[-1] = value
        elif cap.kind == "dd" and self.dl_label and self.dl_label[-1] and value:
            self.page.pairs.append((self.dl_label[-1], value))

    def _close_kind(self, kind):
        for cap in [c for c in self.caps if c.kind == kind]:
            self._close(cap)

    def _end_row(self):
        if not self.tables:
            return
        t = self.tables[-1]
        if t["cell"]:
            self._close(t["cell"])
            t["cell"] = None
        row, t["row"] = t["row"], None
        if row and len(row) >= 2 and row[0] and len(row[0]) <= 80 and any(row[1:]):
            self.page.pairs.append((row[0], " | ".join(c for c in row[1:] if c)))

    # -- HTMLParser
    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "script" and "ld+json" in a.get("type", "").lower():
            self.jsonld_buf = []
            self.skip += 1
            return
        if tag in SKIP:
            self.skip += 1
            return
        if tag == "base" and a.get("href"):
            self.base = urljoin(self.page.url, a["href"])
        if tag == "html" and a.get("lang"):
            self.page.lang = a["lang"]
        if tag == "meta":
            key = (a.get("name") or a.get("property") or a.get("itemprop") or "").lower()
            if key and a.get("content"):
                self.page.meta.setdefault(key, a["content"].strip())
                if a.get("itemprop") in MICRODATA:
                    self.page.microdata.append((a["itemprop"], a["content"].strip()))
            return
        if tag == "link":
            if "alternate" in a.get("rel", "").lower() and a.get("hreflang") and a.get("href"):
                self.page.alternates.append((a["hreflang"].lower(), urljoin(self.base, a["href"])))
            return
        if self.skip:
            return
        for cap in self.caps:
            if cap.tag == tag:
                cap.depth += 1
        if tag in BLOCK:
            self._emit("\n")
        if tag == "img" and a.get("alt"):
            for cap in self.caps:
                if cap.kind == "a":
                    cap.buf.append(" " + a["alt"] + " ")
        if tag == "title":
            self._open(tag, "title")
        elif tag == "a" and a.get("href"):
            href = a["href"].strip()
            if not href.lower().startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
                cap = self._open(tag, "a", urldefrag(urljoin(self.base, href))[0])
                if a.get("aria-label"):
                    cap.buf.append(a["aria-label"] + " ")
        elif tag == "address":
            self._open(tag, "address")
        elif tag == "table":
            self.tables.append({"row": None, "cell": None})
        elif tag == "tr" and self.tables:
            self._end_row()
            self.tables[-1]["row"] = []
        elif tag in ("td", "th") and self.tables:
            t = self.tables[-1]
            if t["row"] is None:
                t["row"] = []
            if t["cell"]:
                self._close(t["cell"])
            t["cell"] = self._open(tag, "cell")
        elif tag == "dl":
            self.dl_label.append(None)
        elif tag in ("dt", "dd") and self.dl_label:
            self._close_kind("dt")
            self._close_kind("dd")
            self._open(tag, tag)
        if a.get("itemprop") in MICRODATA and tag not in VOID:
            self._open(tag, "itemprop", a["itemprop"])

    def handle_endtag(self, tag):
        if tag == "script" and self.jsonld_buf is not None:
            self._add_jsonld("".join(self.jsonld_buf))
            self.jsonld_buf = None
            self.skip = max(0, self.skip - 1)
            return
        if tag in SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "table" and self.tables:
            self._end_row()
            self.tables.pop()
        elif tag == "tr":
            self._end_row()
        elif tag in ("td", "th") and self.tables and self.tables[-1]["cell"]:
            self._close(self.tables[-1]["cell"])
            self.tables[-1]["cell"] = None
        elif tag == "dl" and self.dl_label:
            self._close_kind("dt")
            self._close_kind("dd")
            self.dl_label.pop()
        for cap in list(reversed(self.caps)):
            if cap.tag == tag and cap.kind not in ("cell",):
                if cap.depth == 0:
                    self._close(cap)
                else:
                    cap.depth -= 1
        if tag in BLOCK:
            self._emit("\n")

    def handle_data(self, data):
        if self.jsonld_buf is not None:
            self.jsonld_buf.append(data)
            return
        if self.skip:
            return
        self._emit(re.sub(r"\s+", " ", data))      # source line breaks are not text breaks; block tags are

    def _emit(self, s):
        self.out.append(s)
        for cap in self.caps:
            cap.buf.append(s)

    def _add_jsonld(self, raw):
        raw = raw.strip()
        raw = re.sub(r"^\s*(?:<!--|<!\[CDATA\[)|(?:-->|\]\]>)\s*$", "", raw).strip().rstrip(";")
        try:
            data = json.loads(raw, strict=False)
        except ValueError:
            return
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if "@type" in node:
                    self.page.jsonld.append(node)
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))

    def finish(self) -> ParsedPage:
        self.close()
        while self.tables:
            self._end_row()
            self.tables.pop()
        for cap in list(self.caps):
            self._close(cap)
        lines = []
        for line in "".join(self.out).split("\n"):
            line = " ".join(line.split())
            if line:
                lines.append(line)
        self.page.text = "\n".join(lines)
        return self.page


def parse_html(html: str, url: str) -> ParsedPage:
    p = _Parser(url)
    try:
        p.feed(html)
    except (AssertionError, ValueError):   # malformed markup: keep what was parsed
        pass
    return p.finish()
