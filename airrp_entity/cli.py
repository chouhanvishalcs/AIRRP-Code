"""airrp-entity command line: build entity profiles from public websites."""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys

from .fetch import DEFAULT_USER_AGENT, BrowserRenderer, Fetcher
from .model import FIELDS
from .profile import build_profile, to_csv, to_markdown


def _common(p):
    p.add_argument("--max-pages", type=int, default=25, help="pages to fetch per organisation (default 25)")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests to the same host (default 1)")
    p.add_argument("--timeout", type=float, default=20.0, help="seconds per request (default 20)")
    p.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    p.add_argument("--lang", default="en", help="Accept-Language to ask for (default en)")
    p.add_argument("--no-registry", action="store_true", help="do not cross-check against the public GLEIF LEI registry")
    p.add_argument("--render", action="store_true", help="render JavaScript-built pages with Playwright (pip install playwright)")
    p.add_argument("--also-domain", action="append", default=[], metavar="DOMAIN",
                   help="another domain of the same organisation to include (repeatable)")
    p.add_argument("--quiet", action="store_true")


def _fetcher(a):
    return Fetcher(user_agent=a.user_agent, delay=a.delay, timeout=a.timeout, accept_language=a.lang)


def _run(a, urls, fetcher, renderer, lei=None):
    log = None if a.quiet else (lambda m: print(m, file=sys.stderr))
    return build_profile(urls, fetcher=fetcher, max_pages=a.max_pages, registry=not a.no_registry,
                         extra_domains=a.also_domain, renderer=renderer, lei=lei, log=log)


def _print_summary(profile):
    width = max(len(label) for _, label, _ in FIELDS)
    for key, label, _ in FIELDS:
        f = profile["fields"][key]
        v = f["value"]
        if isinstance(v, list):
            v = "; ".join(v[:8]) + (f"; … (+{len(v) - 8})" if len(v) > 8 else "")
        print(f"{label:<{width}}  {v or '—'}  [{f['confidence'] or 'not found'}{', ' + f['status'] if f['value'] else ''}]")
    for n in profile["notes"][:-2]:
        print(f"note: {n}")


def cmd_profile(a):
    renderer = BrowserRenderer(a.user_agent) if a.render else None
    try:
        profile = _run(a, a.urls, _fetcher(a), renderer, lei=a.lei)
    finally:
        if renderer:
            renderer.close()
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(profile, fh, ensure_ascii=False, indent=2)
    if a.markdown:
        with open(a.markdown, "w", encoding="utf-8") as fh:
            fh.write(to_markdown(profile))
    if a.csv:
        with open(a.csv, "w", encoding="utf-8", newline="") as fh:
            fh.write(to_csv([profile]))
    if not a.quiet:
        _print_summary(profile)
    return 0 if profile["pages"] else 1


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60] or "org"


def cmd_batch(a):
    with open(a.input, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows or not any(k and k.strip().lower() in ("url", "urls", "website") for k in rows[0]):
        print("input CSV needs a 'url' column (optional: 'name', 'lei'; several URLs separated by spaces)", file=sys.stderr)
        return 2
    os.makedirs(a.out_dir, exist_ok=True)
    fetcher, renderer = _fetcher(a), (BrowserRenderer(a.user_agent) if a.render else None)
    profiles, failed = [], 0
    try:
        for row in rows:
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            urls = (row.get("url") or row.get("urls") or row.get("website") or "").replace(";", " ").split()
            if not urls:
                continue
            profile = _run(a, urls, fetcher, renderer, lei=row.get("lei") or None)
            profiles.append(profile)
            failed += not profile["pages"]
            base = os.path.join(a.out_dir, _slug(row.get("name") or urls[0].split("//")[-1]))
            with open(base + ".json", "w", encoding="utf-8") as fh:
                json.dump(profile, fh, ensure_ascii=False, indent=2)
            with open(base + ".md", "w", encoding="utf-8") as fh:
                fh.write(to_markdown(profile))
            if not a.quiet:
                print(f"{urls[0]}: {profile['summary']['registered_legal_name'] or '(legal name not found)'} -> {base}.json",
                      file=sys.stderr)
    finally:
        if renderer:
            renderer.close()
    with open(os.path.join(a.out_dir, "summary.csv"), "w", encoding="utf-8", newline="") as fh:
        fh.write(to_csv(profiles))
    print(f"{len(profiles)} organisation(s), {failed} without any fetched page; summary: {os.path.join(a.out_dir, 'summary.csv')}")
    return 0 if profiles and not failed else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="airrp-entity", description="Evidence-backed entity profile from public websites.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("profile", help="profile one organisation (give its home page; add more URLs of the same organisation)")
    p.add_argument("urls", nargs="+", help="e.g. https://www.sony.com/en/ (extra URLs: legal notice, corporate data page)")
    p.add_argument("--out", help="write the full profile (values + evidence) as JSON")
    p.add_argument("--markdown", help="write a readable report")
    p.add_argument("--csv", help="write one summary row as CSV")
    p.add_argument("--lei", help="use this LEI for the registry cross-check instead of searching")
    _common(p)
    p.set_defaults(fn=cmd_profile)
    b = sub.add_parser("batch", help="profile every organisation in a CSV (columns: url, optional name, lei)")
    b.add_argument("input")
    b.add_argument("--out-dir", required=True, help="one JSON + Markdown per organisation, plus summary.csv")
    _common(b)
    b.set_defaults(fn=cmd_batch)
    a = ap.parse_args(argv)
    return a.fn(a)
