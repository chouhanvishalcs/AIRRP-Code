# Design: importing controls into the master control repository

## Goal and what "100% accurate" means here

Automation cannot be *proved* semantically right. It can be made incapable of silently being wrong:

* **Facts** (ids, titles, text, parameters, enhancement structure, withdrawn status) are extracted
  deterministically from an authoritative source and reconciled against it. They are either exactly right or the
  run stops / the control is quarantined.
* **Judgements** (which master control implements a requirement, how strongly, whether two master controls are the
  same) are never decided by a score. A person approves them, with a reason, and the decision is audited.

## Three layers

| Layer | What it holds | Who changes it |
|---|---|---|
| `source_requirement` | Verbatim control/enhancement records of one framework **version**, with a content hash | Only the importer; rows are immutable (triggers). A new publication is a new version = new rows |
| `source_correction` | A person's corrections of text the source published wrongly: which published value (hash), what is wrong, the source of the right wording, who decided. Append-only revisions; see [CORRECTIONS.md](CORRECTIONS.md) | A person, through the curation file and an import; rows are append-only (triggers) |
| `master_control` | Reusable, curated controls (`draft -> approved -> deprecated`), controlled vocabulary, deterministic unique key | People |
| `mapping` | requirement <-> master control, `relationship` (NISTIR 8477: equivalent/subset/superset/intersects), one primary, rationale, reviewer | People; the tool only inserts `suggested` rows |

`source_requirement` plus the corrections in force is the *effective* requirement (`requirement_effective` view); everything downstream reads that.

Why separate them: the workbook mixed all three in one row, so a similarity score (a fixed 0.350 for 387 rows) ended up
deciding facts about controls, 673 mappings pointed at unreviewed proposals, and requirements stayed `Approved` while mapped to nothing.

## Pipeline

```
OSCAL JSON -> parse -> corrections (laid over, never stored into the mirror) -> validate/reconcile -> plan (diff) -> apply -> verify
                                          |                  |
                                  quarantine + review_item    one transaction, idempotent
```

1. **Parse** (`oscal.py`): canonical ids (`AC-2(6)`), verbatim statements with parameters rendered as `[label]` or
   `[a | b]`, guidance, parameters, assessment objectives/methods, links (related/required/incorporated-into),
   withdrawn status. The original OSCAL id is kept next to the canonical one.
2. **Validate** (`validate.py`): id/label/family/kind consistency, orphan enhancements, unresolved parameters,
   leftover `{{ }}`, empty text, identical statement text under different ids, dangling links, withdrawn without
   successor; plus a **manifest** (expected totals, id-set hash, source hash) so a truncated or wrong file blocks the run.
3. **Plan** (`pipeline.py`): classify each control as *new*, *unchanged* (same hash), *conflict* (same version, different
   content: blocks the whole run) or *quarantined* (unwaived error; its enhancements follow it). `--apply` is separate from the dry run.
4. **Apply**: single transaction, idempotent, audit-logged. Quarantined items go to `review_item`, one row per
   (version, control, code); fixed ones are marked `resolved` on the next run.
5. **Verify**: re-hashes stored payloads and compares them to the source file; any drift is reported and exits non-zero.

## Curation (`curation/*.json`)

Source files do contain mistakes. The real NIST 5.2.0 OSCAL catalog gives SA-15(13) "Logging Syntax" the same
statement text as SA-15(12) (PII minimisation). The importer quarantines both until a person decides:

* **waiver**: the text is right as published; the issue stays on record.
* **correction**: the text is wrong; a person supplies the right wording from the official publication, with what is wrong,
  a citation, a reviewer, a date and the hash of the published value it was written against. The published text stays
  verbatim in `source_requirement`; the correction is recorded beside it and the two together are the effective text, which
  is what validation, review, suggestions and export read. If the source later changes under a correction it becomes
  `CORRECTION_STALE` (an error) instead of silently winning. Corrections are revised, never edited, and can be retired.
  Full workflow, states and limits: [CORRECTIONS.md](CORRECTIONS.md).

Both are scoped to one framework version. (Before the correction layer, an "override" replaced the text *before* it was
stored, so the mirror held corrected text and the verbatim original was lost. That is why corrections now sit beside it.)

## Rules enforced by the schema (not by application code)

* source rows cannot be updated or deleted; `(framework, version, control_id)` is unique
* correction rows cannot be updated or deleted; each revision must directly follow the previous one; only a correction in force can be retired; a correction needs a reason, a citation, a reviewer and an existing source row
* master control names are unique after case/punctuation normalisation (`&` = `and`)
* mappings cannot be inserted as approved; approval needs reviewer + relationship + non-empty rationale
* approved mappings need an *active* requirement and an *approved* master control
* at most one primary mapping per requirement
* no approved master control without approver and timestamp

## Duplicate handling

* Source level: natural key + content hash. Re-import = no change; changed content = conflict.
* Master level: unique canonical key, plus a near-duplicate guard on creation (TF-IDF >= 0.80 is refused unless
  `--distinct-reason` is given and then audited). Master ids are derived from the canonical name, so the same control cannot get two ids.
* Mapping level: `(requirement, master control)` unique; a rejected suggestion is never re-suggested.

## Plugging into the existing AIRRP system

`store.Repository` is the port the pipeline uses (hashes lookup, add framework/requirement, review items, runs,
audit, transaction). `SqliteRepository` is the reference adapter and executable specification. To integrate, implement the
same methods against AIRRP's storage/API, keep the same invariants, and run the same test-suite against it. The mapping
to AIRRP's current fields is an adapter concern (for example `requirement.code = f"{framework}-REQ-{control_id}"`,
`regulationCode`, `sourceReference = <pdf url>#control=<oscal id>`).

## Master control quality gate and legacy migration

`master_control.quality_flags` lists problems that block approval (`PLACEHOLDER_EVIDENCE`, `PLACEHOLDER_TEST_PROCEDURE`,
`UNCONFIRMED_FREQUENCY`, empty objective/description). The schema refuses `status='approved'` while any flag is open, and
approval additionally needs a person other than the creator (four-eyes). New master controls cannot use placeholders at all.

`migrate-workbook` maps the existing workbook onto this model without trusting it: every legacy master becomes a *draft* with
flags, every legacy mapping becomes a *suggestion* (its legacy decision and score kept as evidence), legacy "unmapped"
requirements stay unmapped, masters with values outside the controlled vocabulary (e.g. frequency `Periodic`) are reported and
skipped, and masters spanning more than five NIST families are listed as probable over-broad groupings. Suggestions only
appear in the reviewer console once their master control is approved.

## Concurrency, engines and proof

Importers of the same framework version serialise on a lock (`BEGIN IMMEDIATE` on SQLite, a transaction-scoped advisory lock on
PostgreSQL). After taking it, `apply_plan` re-reads what is stored and skips anything another importer already wrote (or aborts if
the content differs), so racing importers end with exactly one copy of each row. `prove` (see `docs/PROOF.md`) runs 21 claims
against the real catalog, including four simultaneous importers, fault injection on the source file, tamper detection, the approval
gates, a handoff to a foreign-keyed application schema, and a cross-engine fingerprint comparison. It is also run in CI.

## Known limits / next steps

* TF-IDF is a deliberately simple candidate generator (`similarity.Scorer` is pluggable, e.g. embeddings). It only fills a queue.
* No version-to-version diff yet (new NIST release): load as a new version, then add a `diff` command.
* Withdrawn controls are stored for traceability but cannot be mapped; their `incorporated-into` target tells reviewers where it went.
* `vocab.json` merges formatting variants only. Semantic overlaps (SecurityMonitoring vs LoggingAndMonitoring,
  DataProtection vs DataSecurityAndPrivacy, Resilience vs BusinessContinuity) need an owner decision.
* Other frameworks: add an adapter that produces `SourceControl` records; everything after parsing is framework-neutral.
