# Correcting text the source published wrongly

Source files do contain mistakes. In the NIST SP 800-53 r5.2.0 OSCAL catalog, SA-15(13) "Logging Syntax" carries the
same sentence as SA-15(12) (minimising personally identifiable information). The importer notices this and holds both
controls back (`DUPLICATE_STATEMENT`). This page is what a person does next, and what the system does with the answer.

## The rule

**The published text is never edited.** `source_requirement` keeps exactly what the source said, for ever. A correction
is a separate, append-only record (`source_correction`) saying *which* published value it replaces (by hash), *what is
wrong* with it, *where the right wording comes from*, and *who decided and when*. The published text with the
corrections in force laid over it is the **effective** text, and the effective text is what everything downstream reads:
the review queue, similarity suggestions, the export bundle, the hashes your application compares.

The tool never proposes wording. It shows evidence from the catalog file and stops; the wording comes from the official
publication, entered by a person who has read it.

## Workflow

```bash
# 1. See what is held back and why (dry run, writes nothing)
python -m airrp_ingest --db airrp.db import data/catalog.json --manifest manifests/nist-sp-800-53-5.2.0.json --curation curation/nist-sp-800-53.json

# 2. Look at the evidence the catalog itself offers for one control
python -m airrp_ingest --db airrp.db corrections show data/catalog.json --control "SA-15(13)" --field statement

# 3. Get a skeleton entry with the published value's hash filled in (it will not load until a person completes it)
python -m airrp_ingest corrections draft data/catalog.json --control "SA-15(13)" --field statement --out draft.json

# 4. A person with the official publication fills in value, problem, citation, reviewer, reviewed_at,
#    and moves the entry into curation/nist-sp-800-53.json (under "corrections"; reviewed like any other change).

# 5. Check what the entry means, then dry-run, then apply
python -m airrp_ingest --db airrp.db corrections check data/catalog.json --manifest manifests/nist-sp-800-53-5.2.0.json --curation curation/nist-sp-800-53.json
python -m airrp_ingest --db airrp.db import data/catalog.json --manifest ... --curation ... --apply

# 6. What is in force, and the full history
python -m airrp_ingest --db airrp.db corrections list --version 5.2.0 [--history]
python -m airrp_ingest --db airrp.db verify data/catalog.json --curation curation/nist-sp-800-53.json
```

The curation file is the single way in: it lives in version control, the dry run shows the effect before anything is
written, and apply records it with the importer's identity in the audit log.

## An entry

```json
{
  "corrections": [{
    "framework_version": "5.2.0",
    "control_id": "SA-15(13)",
    "field": "statement",
    "value": "<the correct wording, from the official publication>",
    "source_value_sha256": "<hash of the published value this was written against; `corrections draft` fills it in>",
    "problem": "the published text repeats SA-15(12) word for word",
    "citation": "<publication, revision, page or section the wording comes from>",
    "reviewer": "<who decided>",
    "reviewed_at": "<date>"
  }]
}
```

`field` is `title`, `statement` or `guidance`. All of `value`, `problem`, `citation`, `reviewer`, `reviewed_at` are
required. Keys that start with `_` (the draft's `_evidence`) are ignored. The old name for this list, `overrides`, still loads.

## What each entry turns out to mean

| State | Meaning | Effect |
|---|---|---|
| `new` | First correction of this field | Recorded as revision 1 |
| `new_revision` | The entry changed since it was recorded (or replaces a withdrawn one) | Recorded as the next revision; the old one stays on record |
| `unchanged` | Identical to what is already recorded | Nothing written |
| `retire` | The entry carries a `retired` block (`reviewer`, `reviewed_at`, `reason`) | A `retire` revision is recorded; the published text is in force again |
| `retired_already` | Retired, but nothing was in force | Nothing written |
| `adopted_upstream` | The source now already reads as the corrected text | Warning; correction not applied (retire it so the record says so) |
| `stale` | The published value is not the one the entry was written against | Error: the control is held back until the entry is re-reviewed |
| `no_change` | The corrected text reads exactly like the published text | Error: the control is held back |
| `clauses` | The statement has several clauses (or one labelled clause) | Error: see Limits |
| `target_missing` | The control is not in this catalog | The whole run is blocked: the entry is probably mistyped |

A correction is recorded only for a control that is, or this run makes, part of the repository. If retiring a correction
would bring a defect back (SA-15(13) reverting to the duplicate), both controls are held back again and the retirement is
not recorded, unless a person waives the defect on purpose. A parameter warning (`CORRECTION_PARAMETERS_UNREFERENCED`) is
raised when a corrected statement mentions none of the control's parameters; it does not block.

## What the rest of the system sees

* Controls without a correction are **byte-identical** to before the correction layer existed: stored rows, hashes,
  `bundle_sha256` and the requirements export. This is pinned by a test against output produced by the previous
  importer from the same NIST file, so a regression shows up as a failing test.
* A corrected control's `content_hash` and `obligation_hash` are recomputed from its effective text. Only that control changes.
* The bundle (route 1) carries the effective text and, only when corrections exist, a top-level `corrections` array: what was
  published, what replaces it, why, by whom, from what source. A consumer that ignores it still gets the right text and hash.
* The requirements payload (route 2) carries the effective text. It has no field for provenance, so none travels with it.
* The reviewer console marks corrected requirements and lists the corrections on its Summary tab.
* `verify` checks the mirror still equals the source file, the correction history is an unbroken sequence, every correction
  in force still matches the stored published text, the effective text the database serves equals published text plus
  corrections, and no stored control has become invalid because of a correction.

## What changes downstream when a correction is made

A correction changes a control's `obligation_hash`. In the governed mapping layer a decision made against the old wording is
sent back for revalidation, which is intended: corrected wording is a different obligation until a person confirms the
mapping still holds. For SA-15(13) there is nothing to revalidate, because it has never been imported.

## Limits

* **Statement corrections work on single-clause statements.** A statement made of several addressable clauses is refused
  rather than left with a statement and clauses that disagree. SA-15(13) is single-clause. Multi-clause correction needs a
  decision on how clause-level text is corrected and has not been built.
* A correction is scoped to one framework version. A new NIST version starts with no corrections; each one is re-reviewed.
* Only `title`, `statement` and `guidance` can be corrected; ids, structure, parameters and links cannot.
* A repository imported by an importer that predates this layer, with an override applied before storing, is reported by
  `verify` (the stored text is not the published text). None is expected: the curation file shipped with this repository contained no overrides.
* **The real SA-15(13) wording is not recorded in this repository.** It needs the official NIST publication, a citation and a
  reviewer, from a person. The catalog file's own evidence, for whoever does that: SA-15(13) is titled "Logging Syntax"; it
  defines three parameters (`sa-15.13_odp.01` secure logging format(s), `sa-15.13_odp.02` events types to log,
  `sa-15.13_odp.03` level of detail to log); and its SP 800-53A objective reads "the developer of the system, system component,
  or system service uses [secure logging format(s)] to log [events types to log] at [level of detail to log]". That is evidence about
  the topic, not the control text.

## Where it lives

`airrp_ingest/curation.py` (the rules, as pure functions), `pipeline.py` (plan, apply, verify), `store.py` / `pg_store.py`
(`source_correction`, its triggers, the views `correction_current` and `requirement_effective`), `bundle.py`, `cli.py`
(`corrections`), `review_ui.py`. Tests: `tests/test_corrections.py` (both engines), the pinned regression in
`tests/test_real_catalog.py`, and proof claim E9. All correction text in tests and in the proof is marked SYNTHETIC.
