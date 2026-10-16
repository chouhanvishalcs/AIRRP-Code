# Entity profile from public websites (`airrp_entity`)

Builds a profile of an organisation from its own public website, with every value tied to the page and the words it
came from, and (optionally) corroborated against the public GLEIF LEI registry. Zero runtime dependencies
(Python 3.10+, stdlib only); `--render` optionally uses Playwright for JavaScript-built sites.

The same rule as the rest of this repository applies: **facts are extracted deterministically and shown with their
evidence; judgements stay with a person.** A field is left empty rather than guessed.

## Quick start (Sony as the example)

```bash
# one organisation: give its home page; the crawler finds the legal notice, corporate data, IR, contact and region pages
python -m airrp_entity profile https://www.sony.com/en/ --out sony.json --markdown sony.md

# a page it did not find on its own can be added (same organisation, any number of URLs)
python -m airrp_entity profile https://www.sony.com/en/ <url-of-the-corporate-data-page> --out sony.json

# you already know the LEI (20 characters, ISO 17442): skip the registry name search
python -m airrp_entity profile https://www.sony.com/en/ --lei <LEI> --out sony.json

# subsidiaries: every entity that reports Sony Group as its parent to GLEIF, one CSV row each with all twelve fields
python -m airrp_entity profile https://www.sony.com/en/ --out sony.json --subsidiaries-csv sony-subsidiaries.csv

# ... and also crawl up to 30 subsidiary websites (country sites linked from sony.com, or your own list) to add
# the fields only a website has (industry, markets, regulated activity, regulator)
python -m airrp_entity profile https://www.sony.com/en/ --crawl-subsidiaries 30 --subsidiaries-csv sony-subsidiaries.csv
python -m airrp_entity profile https://www.sony.com/en/ --crawl-subsidiaries 30 --subsidiary-sites my-sites.csv

# many organisations: CSV with columns url (required), name, lei  ->  one JSON + Markdown each, plus summary.csv
python -m airrp_entity batch examples/organisations.csv --out-dir profiles/
```

Pick the site of the **legal entity you mean**. Large groups run many entities: `www.sony.com` is Sony Group
Corporation (the listed parent); `www.sony.co.jp/en/corporate/` describes Sony Corporation (the Entertainment,
Technology & Services subsidiary); `electronics.sony.com`, `playstation.com` and so on belong to other subsidiaries. Each
gets its own profile; `examples/organisations.csv` profiles the first two side by side.

Output on the terminal is one line per field with its confidence; `--out` writes everything (values, alternatives,
evidence, subsidiaries, pages fetched, documents found, notes), `--markdown` a readable report, `--csv` one summary
row, `--subsidiaries-csv` one row per subsidiary.

Needs outbound HTTPS to the organisation's site and, for the registry cross-check, to `api.gleif.org`
(`--no-registry` to skip it). In a restricted network, allow those hosts.

## When a site refuses automated requests

Large corporate sites (Sony's among them) often sit behind bot protection that answers a script with HTTP 403. The
terminal then says so (`note: No page could be fetched: https://…: HTTP 403.`) and lists the ways round it:

1. **`--render`**: fetch with a real browser (Chromium through Playwright). One-time setup: `pip install playwright`,
   then `python -m playwright install chromium`. Pages the server refuses to the script are retried in the browser,
   and pages built with JavaScript are rendered.
2. **`--from-files`**: open the pages in your own browser and save each one (Ctrl+S, "Webpage, HTML only") into a
   folder. Useful pages are the home page, corporate data / company profile, legal notice / terms, investor relations,
   and contact / locations. Then run:
   `python -m airrp_entity profile --from-files C:\path\to\folder --out sony.json --markdown sony.md`.
   Each page's address comes from the "saved from url" comment that Chrome and Edge write, else its canonical link.
   Links between the saved pages are not followed, so save every page you want read.
3. **Registry only**: `--lei <LEI>` or `--name "Sony Group Corporation"` gives the registry fields and the
   subsidiaries without reading the website at all. The options combine, e.g.
   `--from-files folder --name "Sony Group Corporation" --subsidiaries-csv subs.csv`.

CSV files are written as UTF-8 with a byte-order mark, so Excel shows Japanese and other non-Latin names correctly.

## What each field is built from

| Field | From the website | From the registry (GLEIF) |
|---|---|---|
| Registered legal name | labelled "Corporate name / Company name / 商号 / Firma…", schema.org `legalName`, "operated by …", "… (the Company)", name line of an imprint, copyright notice | `legalName` (+ other / transliterated names as aliases) |
| Corporate registration reference | identifiers with their scheme label, check digits verified: LEI, UK company no., Japan corporate number (法人番号, invoice T-number), India CIN/GSTIN, German HRB/HRA + court, Austrian FN, Swiss UID, French SIREN/SIRET/RCS, Dutch KvK, Belgian KBO, ABN/ACN, NZBN, UEN, Canadian BN, EIN, SEC CIK, China USCC, Korean BRN, CNPJ, EU VAT, D-U-N-S | LEI; `registeredAs` at `registeredAt` (registration authority) |
| Legal entity type | labelled "Legal form / Rechtsform…", "is a public limited company" wording, CIN class; otherwise inferred from the name's suffix (`Corporation` + Japan = kabushiki kaisha, `GmbH`, `plc`, `Pvt. Ltd.`…) | ISO 20275 ELF code and its name |
| Registration jurisdiction | "registered in England and Wales", "incorporated under the laws of …", "a Delaware corporation", jurisdiction implied by a labelled register number, country of the registered office | `jurisdiction` |
| Registered office | labelled "Registered office / Sitz / siège social / 本店", "registered office: …" statements; a "Head office" is only a weak hint | `legalAddress` |
| Operating location | labelled "Head office / Headquarters / 本社 / Address", schema.org address (and `subOrganization` addresses), `<address>` blocks, every office on a locations/contact page, "headquartered in …" | `headquartersAddress`; one entry per country with active subsidiaries |
| Entity operating status | explicit statements about *this* entity ("we are in administration", "… i.L."), stock-exchange listing, live and maintained site (weak) | entity `status` (ACTIVE / INACTIVE, expiry reason, successor) |
| Government or public-sector status | official government domains (.gov, .go.jp, .gov.uk…), schema.org `GovernmentOrganization`, self-description ("we are a non-departmental public body", "wholly owned by the government"), India CIN class GOI/SGC, stock-exchange listing (private sector) | `category` (RESIDENT_GOVERNMENT_ENTITY, INTERNATIONAL_ORGANIZATION…) |
| Enterprise industry / sector | labelled "Business / Line of business / 事業内容", schema.org NAICS/ISIC, SIC/NACE codes, keyword evidence mapped to GICS-style sectors (`data/sectors.json`) | - |
| Market or country served | hreflang alternates (localised sites), country/region selector links, stated "in more than N countries", schema.org `areaServed`, country-code domain | - |
| Regulated activity | activities implied by a regulator named with a regulatory cue; keyword evidence (`data/activities.json`: banking, insurance, investment, payments, credit, crypto, telecoms, broadcasting, medical devices, pharma, energy, gambling, AI systems …) | - |
| Regulator or licence | regulator named after "authorised and regulated by / licensed by / member of / 登録 …" (`data/regulators.json`, 100 regulators in 26 jurisdictions), licence numbers (FCA FRN, NMLS, FINRA CRD, AFSL, Japan Local Finance Bureau 金商 registrations, SEBI, IRDAI, BaFin-ID, CySEC …) | - |

## Offices and subsidiaries

A multinational's offices mostly belong to its subsidiaries (Sony Europe B.V., Sony Electronics Inc., …), each a separate
legal entity with its own name, registration, legal form and regulators. The tool covers them in three ways:

1. **Offices named on the organisation's own site.** Every office on a locations/contact page (name, then address lines),
   `<address>` blocks, and JSON-LD `subOrganization` addresses become operating locations. A subsidiary's name in JSON-LD
   is never taken as the site's own legal name.
2. **Subsidiaries from GLEIF** (on by default once the parent's LEI record is found). GLEIF publishes who owns whom:
   each entity reports its accounting-consolidation parents. The tool reads the parent's *ultimate children* (falling
   back to *direct children*), up to `--max-subsidiaries` (1000), and profiles every subsidiary from its own LEI record:
   registered legal name, LEI and register number, legal form, jurisdiction, registered office, head office, status,
   government category. Active subsidiaries also feed the parent's operating locations as "<country>: N subsidiaries
   (cities)". Inactive ones are listed but not counted.
3. **Subsidiary websites, on request** (`--crawl-subsidiaries N`). Sites given with `--subsidiary-sites` (CSV: url,
   optional name, lei) come first, then other domains of the same brand linked from the parent's site (country sites
   such as `sony.co.uk`, brand sites such as `sonypictures.com`; listed under `related_sites`). Each site gets
   `--subsidiary-pages` (8) pages. It is attached to a subsidiary **only** by an LEI on the site, the LEI given in the CSV,
   or one exact legal-name match. Its website evidence then completes that subsidiary's industry, markets, regulated
   activity and regulator fields and corroborates the registry ones. A site that matches no subsidiary is reported
   under `subsidiaries.unmatched_sites` with what it says about itself, marked "verify it belongs to the group". It
   could be a distributor.

Output: `subsidiaries` in the JSON (counts, `by_country`, and one entry per subsidiary with its twelve fields and best
evidence), a Subsidiaries section in the Markdown report, and `--subsidiaries-csv` (one row per subsidiary; in `batch`
mode `<name>-subsidiaries.csv`).

Limits: GLEIF lists only subsidiaries that hold an LEI **and** have reported their parent. Branches, representative
offices and entities without an LEI do not appear, and a group's own count of consolidated subsidiaries (annual
report, Form 20-F exhibit 8) can be much higher. Those documents are listed under `documents_for_review` when the
crawler meets them. Without the registry (`--no-registry`), `--crawl-subsidiaries` still profiles the related sites,
but every one of them stays unverified.

## Confidence, status and conflicts

Each piece of evidence has a weight for how direct it is. A labelled row on a corporate-data or legal page, a statement
in prose, a registry value and a validated identifier are strong. A keyword in a menu, a copyright line or a generic
mention is weak. Equal values are merged (same identifier, same address with or without its country, a registry
alias). A group's score is its strongest evidence, plus a little for each *independent* method that agrees and for
registry corroboration. Repeating the same footer on twenty pages adds nothing.

| Confidence | Score | Meaning |
|---|---|---|
| high | ≥ 0.80 | a direct statement, a validated identifier, or the registry |
| medium | ≥ 0.55 | indirect but specific (copyright notice, implied by an identifier, inferred legal form) |
| low | below | a hint for a reviewer (keywords, "the website is live") |

Field status: `confirmed` (a high-confidence value and no other high-confidence value), `conflict` (two high-confidence
sources disagree, for example the website and the registry: a person decides, and the notes say so), `inferred` (best
value is medium/low), `found` / `weak` (multi-valued fields), `not_found` (nothing on the pages fetched supports a
value: nothing is invented). `corroborated: true` marks values the registry and the website agree on.

## Registry cross-check (GLEIF)

[GLEIF](https://www.gleif.org) publishes every LEI record with the legal name, legal form, jurisdiction, registered and
headquarters addresses, status and the registration number held by the registration authority, through a free API
without a key. The record is used only when it is reached by:

1. an LEI published on the website, or one given with `--lei`, or
2. an **exact** match of the website's legal name (after case, punctuation and `Corp.`/`Corporation`-style
   normalisation) with the record's legal name or one of its other names. Branch records are set aside, and a tie is
   broken by the website's jurisdiction. If several records still match, none is chosen: the notes list their LEIs
   so you can re-run with `--lei`.

For a Japanese company the registered legal name is the Japanese one (e.g. `…株式会社`). The English name from the
website is kept as `also_seen_as`. An address from the registry and one from the website are treated as the same
when the registry's postal code appears in the website's address.

GLEIF covers entities that hold an LEI (all listed companies, banks, insurers, most groups' main subsidiaries). It is
not the commercial register itself. For a legally authoritative extract use the register of the jurisdiction
(Companies House, Handelsregister, the Japanese Legal Affairs Bureau / NTA corporate-number site, MCA21, SEC EDGAR …),
which are natural next adapters (`gleif.py` is the template).

## Politeness and limits

* Fetches at most `--max-pages` (25) pages per organisation, one request per host per `--delay` (1 s), honours
  `robots.txt` as RFC 9309 defines it (a 4xx answer for robots.txt means no restrictions, a 5xx answer means none
  allowed), identifies itself with a descriptive User-Agent (`--user-agent` to change it), stays on the
  organisation's own domain(s) (`--also-domain` adds more), and follows the most relevant links first (legal notice,
  corporate data, IR, regulatory, contact, region selector). News, products, careers and shop pages are skipped.
* PDFs (annual reports, Form 20-F, securities reports, licences) are listed under `documents_for_review`, not parsed.
* Sites that build their pages with JavaScript give little text; the notes say so. Re-run with `--render`
  (`pip install playwright`, then `python -m playwright install chromium`). Some sites refuse automated clients outright; those pages appear under `skipped`.
* Label recognition covers English, German, French, Spanish, Italian, Dutch and Japanese corporate-page wording.
  Industry and activity keywords are English (plus a few Japanese terms). Extend `airrp_entity/data/*.json`: the
  code reads them at start-up.
* Industry, regulated activity and markets are *proposals*. Regulators named without a regulatory cue are kept as low
  candidates. "Active" from the website alone is never more than `inferred`.

## Output (schema `airrp-entity-profile/1`)

```json
{
  "schema": "airrp-entity-profile/1",
  "summary": {"registered_legal_name": "...", "corporate_registration_reference": ["..."], "...": "..."},
  "fields": {
    "registered_legal_name": {
      "label": "Registered legal name", "status": "confirmed", "value": "...", "confidence": "high",
      "candidates": [{"value": "...", "confidence": "high", "score": 0.95, "corroborated": true,
                      "sources": ["registry", "website"], "also_seen_as": ["..."], "details": ["..."],
                      "evidence": [{"source": "website", "url": "...", "method": "labelled field 'Corporate Name'",
                                    "snippet": "Corporate Name: ..."}]}]
    }
  },
  "registry": {"lei": "...", "matched_by": "...", "record_url": "https://search.gleif.org/#/record/...", "...": "..."},
  "pages": [{"url": "...", "kind": "corporate", "title": "...", "sha256": "...", "fetched_at": "..."}],
  "skipped": [{"url": "...", "reason": "disallowed by robots.txt"}],
  "documents_for_review": [{"url": ".../annual-report.pdf", "text": "Annual Report"}],
  "notes": ["..."]
}
```

Tests: `python -m unittest tests.test_entity_identifiers tests.test_entity_profile tests.test_entity_subsidiaries tests.test_entity_cli tests.test_entity_access`. They run
offline against fictional sites in `tests/fixtures/entity_sites/` and a local HTTP server.
