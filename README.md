# airrp-ingest

Imports controls, control enhancements and related information from NIST OSCAL JSON into a central,
duplicate-free master control repository, with every step checked and every judgement call made by a person.

Zero runtime dependencies (Python 3.10+, stdlib only).

```bash
# 1. get the authoritative source (pinned in manifests/)
mkdir -p data && curl -o data/catalog.json \
  https://raw.githubusercontent.com/usnistgov/oscal-content/main/nist.gov/SP800-53/rev5/json/NIST_SP-800-53_rev5_catalog.json

# 2. dry run: parse, validate, reconcile against the manifest, show the diff. Writes nothing.
python -m airrp_ingest --db airrp.db import data/catalog.json \
  --manifest manifests/nist-sp-800-53-5.2.0.json --curation curation/nist-sp-800-53.json --show-warnings

# 3. apply (one transaction; re-running is a no-op). Exit code 2 = loaded, but some controls are quarantined.
python -m airrp_ingest --db airrp.db import data/catalog.json --manifest ... --curation ... --apply

# 4. audit the repository against the source file at any time
python -m airrp_ingest --db airrp.db verify data/catalog.json

# 5. master controls and mappings (humans decide, the tool proposes)
python -m airrp_ingest --db airrp.db master add --name ... --objective ... --description ... --domain ... \
    --frequency ... --control-type ... --evidence ... --test-procedure ...
python -m airrp_ingest --db airrp.db master approve --id AIRRP-CTRL-XXXXXXXXXXXX
python -m airrp_ingest --db airrp.db suggest --version 5.2.0
python -m airrp_ingest --db airrp.db review list
python -m airrp_ingest --db airrp.db review approve --id 12 --relationship equivalent --rationale "..." --primary
python -m airrp_ingest --db airrp.db report --version 5.2.0
```

### Reviewer console and migration

```bash
# local web console (127.0.0.1 only, random token printed at start): approve/reject suggestions,
# map requirements the suggester missed, add and approve master controls, see quarantined source issues
python -m airrp_ingest --db airrp.db serve --version 5.2.0

# bring the existing workbook in as DRAFT masters + SUGGESTED mappings (nothing is trusted or approved)
python -m airrp_ingest --db airrp.db migrate-workbook Book1.xlsx --version 5.2.0 --report migration.json   # dry run
python -m airrp_ingest --db airrp.db migrate-workbook Book1.xlsx --version 5.2.0 --apply                    # (.xlsx needs: pip install openpyxl)

# masters with placeholder evidence/test/frequency are flagged and cannot be approved: fix them in bulk
python -m airrp_ingest --db airrp.db master todo --out todo.csv        # edit in Excel
python -m airrp_ingest --db airrp.db master apply-csv --out todo.csv

# hand reviewed data to your application (see docs/INTEGRATION.md)
python -m airrp_ingest --db airrp.db export --version 5.2.0 --format bundle --out bundle.json
python -m airrp_ingest --db airrp.db export --version 5.2.0 --format requirements --out requirements.json
```

### When the source itself is wrong

The published text is never edited. A person records a correction beside it (what is wrong, the citation, who decided);
the two together are what the system reads. Workflow, limits and the one correction this repository does *not* contain
(the real SA-15(13) wording needs the official publication): [docs/CORRECTIONS.md](docs/CORRECTIONS.md).

```bash
python -m airrp_ingest --db airrp.db corrections show data/catalog.json --control "SA-15(13)"      # evidence from the catalog file
python -m airrp_ingest corrections draft data/catalog.json --control "SA-15(13)" --out draft.json   # skeleton for a person to complete
python -m airrp_ingest --db airrp.db corrections check data/catalog.json --curation curation/nist-sp-800-53.json
python -m airrp_ingest --db airrp.db corrections list --version 5.2.0 --history
```

A master control cannot be approved by the person who created it (`--solo` disables this for one-person teams).

### Proof and storage engines

```bash
# executable proof (22 claims with a workbook, 21 without) on SQLite and PostgreSQL; exit code 1 if any claim fails. Result of the last run: docs/PROOF.md
pip install -r requirements-dev.txt
python -m airrp_ingest prove --catalog data/catalog.json --manifest manifests/nist-sp-800-53-5.2.0.json \
    --curation curation/nist-sp-800-53.json --workbook Book1.xlsx --out PROOF.md
# against your own PostgreSQL instead of the embedded one (a throw-away schema is created and left behind):
python -m airrp_ingest prove ... --postgres postgresql://user:pass@host/db

# every command takes --db <sqlite file> or --db postgresql://user:pass@host/db
python -m airrp_ingest --db postgresql://user:pass@host/db import data/catalog.json --manifest ... --apply
```

Tests: `python -m unittest discover -s tests -t .` (set `OSCAL_CATALOG=data/catalog.json` to also run the
full-catalog tests; the storage tests run on SQLite and, when `pgserver`+`psycopg` are installed, on PostgreSQL). Design and the rules the schema enforces: [docs/DESIGN.md](docs/DESIGN.md).
