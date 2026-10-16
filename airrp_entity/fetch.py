"""Polite HTTP fetching (stdlib urllib): robots.txt, per-host delay, size cap, gzip, charset detection.

Proxies come from the usual environment variables (HTTPS_PROXY ...); CA certificates from SSL_CERT_FILE if set.
"""
from __future__ import annotations

import gzip
import re
import time
import urllib.error
import urllib.request
import urllib.robotparser
import zlib
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import __version__

DEFAULT_USER_AGENT = f"airrp-entity-profile/{__version__} (+https://github.com/chouhanvishalcs/AIRRP-Code)"
HTML_ACCEPT = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"
CHARSET_ALIASES = {"shift_jis": "cp932", "shift-jis": "cp932", "sjis": "cp932", "x-sjis": "cp932", "windows-31j": "cp932",
                   "iso-8859-1": "cp1252", "latin-1": "cp1252", "latin1": "cp1252", "us-ascii": "utf-8", "ascii": "utf-8"}


@dataclass
class Response:
    url: str                 # final URL after redirects
    status: int
    content_type: str
    body: bytes
    charset: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300

    @property
    def is_html(self) -> bool:
        return self.content_type in ("text/html", "application/xhtml+xml") or (
            not self.content_type and self.body[:512].lstrip().lower().startswith((b"<!doctype html", b"<html")))

    def text(self) -> str:
        return decode(self.body, self.charset)


def decode(body: bytes, declared: str | None = None) -> str:
    if body.startswith(b"\xef\xbb\xbf"):
        return body[3:].decode("utf-8", "replace")
    candidates = []
    if declared:
        candidates.append(declared)
    m = re.search(rb"""<meta[^>]+charset\s*=\s*["']?([\w.:-]+)""", body[:4096], re.I)
    if m:
        candidates.append(m.group(1).decode("ascii", "ignore"))
    candidates += ["utf-8", "cp1252"]
    for cs in candidates:
        cs = CHARSET_ALIASES.get(cs.lower().strip(), cs.lower().strip())
        try:
            return body.decode(cs)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", "replace")


class Fetcher:
    def __init__(self, user_agent: str = DEFAULT_USER_AGENT, delay: float = 1.0, timeout: float = 20.0,
                 max_bytes: int = 5_000_000, respect_robots: bool = True, accept_language: str = "en"):
        self.user_agent, self.delay, self.timeout, self.max_bytes = user_agent, delay, timeout, max_bytes
        self.respect_robots, self.accept_language = respect_robots, accept_language
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._robots_note: dict[str, str] = {}
        self._last: dict[str, float] = {}
        self.requests = 0

    # -- robots.txt
    def robots(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            rp = urllib.robotparser.RobotFileParser(origin + "/robots.txt")
            r = self._get(origin + "/robots.txt", "text/plain,*/*")
            if r.status == 0:
                rp = None                      # unreachable: the page request itself reports why
            elif r.status >= 500:              # RFC 9309 2.3.1.4: server error -> assume complete disallow
                rp.disallow_all = True
                self._robots_note[origin] = f"robots.txt unavailable (HTTP {r.status}): treated as disallow-all"
            elif r.status >= 400:              # RFC 9309 2.3.1.3: 4xx (401/403 too) -> no restrictions
                rp.allow_all = True
            else:
                rp.parse(r.text().splitlines())
            self._robots[origin] = rp
        return self._robots[origin]

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        rp = self.robots(url)
        return rp is None or rp.can_fetch(self.user_agent, url)

    def sitemaps(self, url: str) -> list[str]:
        rp = self.robots(url)
        return list((rp.site_maps() if rp else None) or [])

    # -- requests
    def get(self, url: str, accept: str = HTML_ACCEPT, check_robots: bool = True) -> Response:
        if check_robots and not self.allowed(url):
            origin = "{0.scheme}://{0.netloc}".format(urlsplit(url))
            return Response(url, 0, "", b"", error=self._robots_note.get(origin, "disallowed by robots.txt"))
        return self._get(url, accept)

    def _get(self, url: str, accept: str) -> Response:
        host = urlsplit(url).netloc
        wait = self.delay - (time.monotonic() - self._last.get(host, -1e9))
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={
            "User-Agent": self.user_agent, "Accept": accept, "Accept-Language": f"{self.accept_language},en;q=0.8",
            "Accept-Encoding": "gzip, deflate"})
        self.requests += 1
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return self._read(resp, url)
        except urllib.error.HTTPError as exc:
            return Response(exc.geturl() or url, exc.code, "", b"", error=f"HTTP {exc.code}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return Response(url, 0, "", b"", error=f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}")
        finally:
            self._last[host] = time.monotonic()

    def _read(self, resp, url) -> Response:
        body = resp.read(self.max_bytes + 1)
        error = f"truncated at {self.max_bytes} bytes" if len(body) > self.max_bytes else None
        body = body[:self.max_bytes]
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        try:
            if enc == "gzip":
                body = gzip.decompress(body)
            elif enc == "deflate":
                body = zlib.decompress(body, -zlib.MAX_WBITS) if body[:1] != b"\x78" else zlib.decompress(body)
        except (OSError, EOFError, zlib.error):
            return Response(resp.geturl(), resp.status, "", b"", error=f"could not decode {enc} body")
        ctype = resp.headers.get_content_type() if resp.headers.get("Content-Type") else ""
        r = Response(resp.geturl() or url, resp.status, ctype, body, resp.headers.get_content_charset())
        if error and r.is_html:
            r.error = None                     # a truncated HTML page is still usable
        elif error:
            r.error = error
        return r


class BrowserRenderer:
    """Optional: render JavaScript-built pages with Playwright (pip install playwright). Used only on request."""

    def __init__(self, timeout: float = 30.0, locale: str = "en-US"):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("--render needs Playwright: pip install playwright, then: "
                               "python -m playwright install chromium") from exc
        import os
        self._pw = sync_playwright().start()
        exe = os.environ.get("CHROMIUM_PATH")      # otherwise Playwright's own browser (PLAYWRIGHT_BROWSERS_PATH)
        try:
            self._browser = self._pw.chromium.launch(**({"executable_path": exe} if exe else {}))
        except Exception as exc:  # noqa: BLE001 - usually the browser has not been downloaded yet
            self._pw.stop()
            raise RuntimeError(f"could not start Chromium for --render ({exc}). Run: python -m playwright install "
                               "chromium") from exc
        self._context = self._browser.new_context(locale=locale)   # the browser identifies itself as what it is
        self.timeout_ms = int(timeout * 1000)

    def render(self, url: str) -> tuple[str, str] | None:
        """(html, final url) of the page as a browser shows it, or None if the browser could not load it."""
        page = self._context.new_page()
        try:
            resp = page.goto(url, wait_until="networkidle", timeout=self.timeout_ms)
            if resp is not None and resp.status >= 400:
                return None
            return page.content(), page.url
        except Exception:      # noqa: BLE001 - a failed render falls back to the static result
            return None
        finally:
            page.close()

    def close(self):
        self._browser.close()
        self._pw.stop()
