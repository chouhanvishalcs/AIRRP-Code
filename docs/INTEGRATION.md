# Plugging this into your application

Three routes, from least to most coupling. Start with 1; 2 is a drop-in for the existing requirement import;
3 is only needed if you want the pipeline itself to run against AIRRP's database.

## 1. Import bundle (recommended) - `airrp-ingest export --format bundle`

One JSON file, schema in [`schema/import-bundle.schema.json`](../schema/import-bundle.schema.json). It contains only
reviewed data: active requirements, **approved** master controls, **approved** mappings.

| Collection | Upsert key | Change detection |
|---|---|---|
| `requirements[]` | `code` | `content_hash` |
| `master_controls[]` | `id` | whole-record compare |
| `mappings[]` | (`requirement_code`, `master_control_id`) | whole-record compare |

Consumer contract (what your side should do, in one transaction):

1. Validate against the schema and verify `bundle_sha256` was not seen before (skip the file if it was).
2. Upsert requirements, then master controls, then mappings, by the keys above. Never delete what is missing from a
   bundle: a missing master control/mapping means "not approved in this bundle", handle removals as an explicit deprecation.
3. A mapping whose requirement or master control does not exist is a hard error, not a skip.
4. Keep the `reviewer`/`reviewed_at`/`approved_by` fields; they are your audit trail.

The same bundle can be re-applied any number of times; identical content is a no-op.

## 2. Existing requirement payload - `airrp-ingest export --format requirements`

The array of `{code, name, frequency, legalText, legalTitle, description, ownerFunction, obligationType, regulationCode,
sourceReference}` objects your system already accepts. Proven identical to the workbook's own payloads for the 1,013 of 1,014
active NIST SP 800-53 r5.2.0 controls (the 1,014th, SA-4(7), differs only by a trailing space that the importer trims from titles).
`ownerFunction` and `frequency` are exported as empty strings because the source does not define them.

## 3. Storage adapter - `airrp_ingest.store.SqlRepository`

All domain rules are written once, as portable SQL, in `SqlRepository`. An adapter only supplies the connection, the DDL
(constraints, partial unique indexes, triggers), and three primitives (`execute`, `_insert_returning_id`, `lock_framework`).
Two adapters ship and run the same 118-test contract suite: `SqliteRepository` and `PostgresRepository` (real PostgreSQL 16,
constraints and plpgsql triggers; see `pg_store.py` for what a second engine needs). For another engine, copy `pg_store.py`,
translate the DDL, and add it to `ENGINES` in `tests/helpers.py`; if the contract tests pass, the rules hold.

`reference_consumer.py` is the application side of route 1 (idempotent upsert into foreign-keyed tables, hash check, hard failure
on a dangling reference); its behaviour is part of the proof (claim E7).

## What would make a native adapter possible (send from your laptop if you want one)

* the DDL/entity definitions for requirements, master controls and the mapping between them (and the DB engine)
* how `AIRRP-CTRL-<hash>` ids are currently generated, and what "Active/Proposed/Workspace" mean in the app's lifecycle
* a sample of the *other* JSON files you plan to import, if they are not NIST OSCAL
* the code of the legacy "repo-semantic" normaliser, only if you want its scoring reproduced (not needed to retire it)
