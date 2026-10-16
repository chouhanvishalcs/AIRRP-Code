"""Offline fetcher for the airrp_entity tests: serves tests/fixtures/entity_sites/<site>/ for fictional hosts."""
import json
import os
from urllib.parse import unquote, urlsplit

from airrp_entity.fetch import Fetcher, Response

SITES = os.path.join(os.path.dirname(__file__), "fixtures", "entity_sites")
HOSTS = {"www.northwind.example": "northwind", "www.sakura-denki.example": "sakura", "www.beispiel.example": "beispiel",
         "www.example-psu.example": "psu", "www.bare.example": "bare"}
GLEIF_API = "https://api.gleif.example/api/v1"
TYPES = {".html": "text/html", ".txt": "text/plain", ".json": "application/vnd.api+json", ".pdf": "application/pdf",
         ".xml": "application/xml"}


def gleif_routes(*records, legal_forms=None, authorities=None, searches=None):
    """Routes in the shape of the public GLEIF API: records by LEI, name searches, legal forms, authorities."""
    routes = {}
    for rec in records:
        lei = rec["data"]["attributes"]["lei"]
        routes[f"{GLEIF_API}/lei-records/{lei}"] = json.dumps(rec)
    for code, doc in (legal_forms or {}).items():
        routes[f"{GLEIF_API}/entity-legal-forms/{code}"] = json.dumps(doc)
    for code, doc in (authorities or {}).items():
        routes[f"{GLEIF_API}/registration-authorities/{code}"] = json.dumps(doc)
    for query, recs in (searches or {}).items():
        routes[f"{GLEIF_API}/lei-records?{query}"] = json.dumps({"data": [r["data"] for r in recs]})
    return routes


def fixture_json(name):
    with open(os.path.join(SITES, "gleif", name), encoding="utf-8") as fh:
        return json.load(fh)


class FixtureFetcher(Fetcher):
    def __init__(self, routes=None, **kw):
        super().__init__(delay=0, **kw)
        self.routes = routes or {}
        self.log: list[str] = []

    def _get(self, url, accept):
        self.log.append(url)
        self.requests += 1
        if url in self.routes:
            return Response(url, 200, "application/vnd.api+json", self.routes[url].encode("utf-8"), "utf-8")
        p = urlsplit(url)
        site = HOSTS.get(p.hostname or "")
        if not site:
            return Response(url, 404, "", b"", error="HTTP 404")
        path = os.path.join(SITES, site, unquote(p.path).lstrip("/"))
        for candidate in (path, os.path.join(path, "index.html")):
            if os.path.isfile(candidate):
                with open(candidate, "rb") as fh:
                    body = fh.read()
                ctype = TYPES.get(os.path.splitext(candidate)[1], "application/octet-stream")
                return Response(url, 200, ctype, body, "utf-8" if ctype.startswith("text") else None)
        return Response(url, 404, "", b"", error="HTTP 404")
