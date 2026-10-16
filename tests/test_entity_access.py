"""Sites that refuse automated requests: robots.txt rules, the browser fallback, saved pages, registry-only runs."""
import os
import shutil
import tempfile
import unittest
from urllib.parse import urlsplit

from airrp_entity.cli import main
from airrp_entity.fetch import Fetcher, Response
from airrp_entity.profile import build_profile

from .entity_helpers import GLEIF_API, SITES, FixtureFetcher
from .test_entity_subsidiaries import group_routes

BLOCKED = "https://www.blocked.example/"


class RobotsTests(unittest.TestCase):
    def fetcher(self, robots_status):
        class F(Fetcher):
            def _get(self, url, accept):
                if url.endswith("/robots.txt"):
                    if robots_status == 0:
                        return Response(url, 0, "", b"", error="URLError: timed out")
                    return Response(url, robots_status, "text/plain", b"", error=f"HTTP {robots_status}")
                return Response(url, 200, "text/html", b"<html><body>ok</body></html>")
        return F(delay=0)

    def test_rfc9309(self):
        for status in (401, 403, 404):          # 4xx: robots.txt unavailable, no restrictions (not "disallow all")
            self.assertTrue(self.fetcher(status).get("https://x.example/a").ok, status)
        self.assertTrue(self.fetcher(0).get("https://x.example/a").ok)          # unreachable: the page decides
        r = self.fetcher(503).get("https://x.example/a")                      # 5xx: assume complete disallow
        self.assertEqual(r.error, "robots.txt unavailable (HTTP 503): treated as disallow-all")


class BlockedSiteTests(unittest.TestCase):
    def test_the_reason_and_the_ways_round_it_are_reported(self):
        p = build_profile([BLOCKED], fetcher=FixtureFetcher(), registry=False)
        self.assertEqual(p["pages"], [])
        self.assertEqual(p["notes"][0], f"No page could be fetched: {BLOCKED}: HTTP 403.")
        self.assertIn("--render", p["notes"][1])
        self.assertIn("--from-files", p["notes"][1])
        self.assertIn("--lei", p["notes"][1])

    def test_a_refused_page_is_retried_in_a_real_browser(self):
        class Browser:                              # stands in for Playwright: serves the Northwind pages
            def render(self, url):
                path = os.path.join(SITES, "northwind", urlsplit(url).path.lstrip("/"), "index.html")
                if not os.path.exists(path):
                    return None
                with open(path, encoding="utf-8") as fh:
                    return fh.read(), url
        p = build_profile([BLOCKED], fetcher=FixtureFetcher(), registry=False, renderer=Browser())
        self.assertTrue(p["pages"] and all(page["rendered"] for page in p["pages"]))
        self.assertEqual(p["fields"]["registered_legal_name"]["value"], "Northwind Example Holdings plc")
        self.assertIn(BLOCKED + "legal/", [page["url"] for page in p["pages"]])


class SavedPagesTests(unittest.TestCase):
    def test_pages_saved_from_a_browser(self):
        tmp = tempfile.mkdtemp()
        for name, url in (("legal", "https://www.northwind.example/legal/"),
                          ("about", "https://www.northwind.example/about/"),
                          ("regulatory", "https://www.northwind.example/regulatory/")):
            with open(os.path.join(SITES, "northwind", name, "index.html"), encoding="utf-8") as fh:
                html = fh.read()
            with open(os.path.join(tmp, f"{name}.html"), "w", encoding="utf-8") as fh:    # what Chrome/Edge write
                fh.write(f"<!-- saved from url=({len(url):04d}){url} -->\n" + html)
        shutil.copy(os.path.join(SITES, "northwind", "contact", "index.html"), os.path.join(tmp, "contact.htm"))
        p = build_profile([], files=[tmp], registry=False)
        urls = {page["url"] for page in p["pages"]}
        self.assertIn("https://www.northwind.example/legal/", urls)
        self.assertTrue(any(u.startswith("file:") for u in urls))          # no saved-from comment: the file itself
        self.assertEqual(next(pg["kind"] for pg in p["pages"] if pg["url"].endswith("/legal/")), "legal")
        f = p["fields"]
        self.assertEqual((f["registered_legal_name"]["value"], f["registered_legal_name"]["status"]),
                         ("Northwind Example Holdings plc", "confirmed"))
        self.assertIn("UK Companies House company number: 01234567", f["corporate_registration_reference"]["value"])
        self.assertIn("Financial Conduct Authority (FCA), United Kingdom", f["regulator_or_licence"]["value"])


class RegistryOnlyTests(unittest.TestCase):
    def test_lei_or_name_without_a_website(self):
        for kw in ({"lei": "9845002SKRDENKI00040"}, {"names": ["Sakura Denki Corporation"]}):
            p = build_profile([], fetcher=FixtureFetcher(group_routes()), gleif_api=GLEIF_API, **kw)
            self.assertEqual(p["fields"]["registered_legal_name"]["value"], "さくら電機株式会社", kw)
            self.assertEqual(p["subsidiaries"]["fetched"], 4)
            self.assertIn("No website given: registry data only.", p["notes"])

    def test_cli_needs_something_to_profile(self):
        self.assertEqual(main(["profile", "--quiet"]), 2)


if __name__ == "__main__":
    unittest.main()
