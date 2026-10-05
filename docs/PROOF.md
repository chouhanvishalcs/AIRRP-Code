# Proof run

**Verdict: ALL CLAIMS PASSED** - 21 passed, 0 failed, 0 skipped.

* Source: `catalog.json` (sha256 `01f37cf90ea99d92…`), NIST-SP-800-53@5.2.0
* Engines exercised: SQLite 3.45.1, PostgreSQL 16.2
* Tool: airrp-ingest 0.1.0, Python 3.11.15 on Linux
* Existing-system comparison: included

Reproduce: `python -m airrp_ingest prove --catalog data/catalog.json --manifest manifests/nist-sp-800-53-5.2.0.json --workbook Book1.xlsx --out docs/PROOF.md`

| # | Claim | Engine | Result |
|---|---|---|---|
| C1 | The whole catalog is extracted, nothing missing, nothing invented | all | PASS |
| C2 | A defect inside NIST's own file is detected instead of imported | all | PASS |
| C3 | Corrupted or tampered source files are caught before anything is written | all | PASS |
| C4 | Output matches what your current system already produced for NIST SP 800-53 | all | PASS |
| E1 | Only verified data is loaded, and the stored data equals the source | sqlite | PASS |
| E2 | Re-importing never creates duplicates | sqlite | PASS |
| E3 | Concurrent imports of the same catalog cannot create duplicates | sqlite | PASS |
| E4 | Stored source data cannot be changed, and out-of-band edits are detected | sqlite | PASS |
| E5 | Master controls cannot be duplicated | sqlite | PASS |
| E6 | Nothing becomes 'approved' without a named human, a reason and a valid target | sqlite | PASS |
| E7 | Reviewed data reaches an application database losslessly and idempotently | sqlite | PASS |
| E8 | The existing workbook can be migrated without trusting any of it | sqlite | PASS |
| E1 | Only verified data is loaded, and the stored data equals the source | postgres | PASS |
| E2 | Re-importing never creates duplicates | postgres | PASS |
| E3 | Concurrent imports of the same catalog cannot create duplicates | postgres | PASS |
| E4 | Stored source data cannot be changed, and out-of-band edits are detected | postgres | PASS |
| E5 | Master controls cannot be duplicated | postgres | PASS |
| E6 | Nothing becomes 'approved' without a named human, a reason and a valid target | postgres | PASS |
| E7 | Reviewed data reaches an application database losslessly and idempotently | postgres | PASS |
| E8 | The existing workbook can be migrated without trusting any of it | postgres | PASS |
| X1 | Different storage engines end up with byte-identical source data | all | PASS |

### C1 · The whole catalog is extracted, nothing missing, nothing invented (all) - PASS

* 1196 controls = 324 base + 872 enhancements, 20 families; 1014 active, 182 withdrawn
* control-id set hash, totals and per-kind counts equal the manifest; every id appears exactly once
* 0 unresolved parameters, 0 unrendered placeholders, 0 empty statements, 0 orphan enhancements
* 1 non-blocking dangling-link warning(s) reported, not hidden
* (0.0s)

### C2 · A defect inside NIST's own file is detected instead of imported (all) - PASS

* flagged: ['SA-15(12)', 'SA-15(13)']
* both carry the sentence 'Require the developer of the system or system component to minimize the use of personally …' although SA-15(13) is titled 'Logging Syntax'
* both are held back from the repository until a person waives or overrides them with a citation
* (0.0s)

### C3 · Corrupted or tampered source files are caught before anything is written (all) - PASS

* caught: delete one control (SC-7(4)) -> ['COUNT_MISMATCH', 'ID_SET_MISMATCH'] (run blocked)
* caught: duplicate a control id (AC-3) -> ['COUNT_MISMATCH', 'DUPLICATE_ID'] (run blocked)
* caught: bump catalog version without a manifest -> ['MANIFEST_VERSION'] (run blocked)
* caught: break a parameter reference (AC-2, which has enhancements) -> ['UNRESOLVED_PARAM'] (16 controls quarantined, 1180 others still loadable)
* caught: copy AC-3's text into AC-4 -> ['DUPLICATE_STATEMENT'] (51 controls quarantined, 1145 others still loadable)
* caught: label disagrees with id (AU-6) -> ['ID_LABEL_MISMATCH'] (13 controls quarantined, 1183 others still loadable)
* caught: remove an active control's statement (IA-2) -> ['EMPTY_STATEMENT'] (16 controls quarantined, 1180 others still loadable)
* caught: truncate the file at 50% -> JSONDecodeError, nothing partially parsed
* caught: not a catalog at all -> ValueError, nothing partially parsed
* (4.7s)

### C4 · Output matches what your current system already produced for NIST SP 800-53 (all) - PASS

* 1014 requirement payloads from your current system vs 1014 generated from the catalog; same code set: True
* 1013 byte-identical, 1 differ only by leading/trailing whitespace (NIST-SP-800-53-SECURITY-AND-PRIVACY-CONTROLS-REQ-SA-4-7), 0 differ in content
* (0.6s)

### E1 · Only verified data is loaded, and the stored data equals the source (sqlite) - PASS

* 1194 rows written = 1196 catalog controls - 2 quarantined; table holds 1194
* independent verify (re-hash every stored row, compare to the source file): 0 problem(s)
* content fingerprint of the stored set: 5b90e0cf505c4c81…
* (0.9s)

### E2 · Re-importing never creates duplicates (sqlite) - PASS

* two more full imports added [0, 0] rows and recognised [1194, 1194] as unchanged
* 1194 rows, 1194 distinct control ids (duplicates: 0)
* (0.4s)

### E3 · Concurrent imports of the same catalog cannot create duplicates (sqlite) - PASS

* 4 importers started the same import at the same instant; rows added per importer: [1194, 0, 0, 0] (errors: none)
* final table: 1194 rows, 1194 distinct ids; exactly one importer won, the others recognised the data as already stored
* (1.2s)

### E4 · Stored source data cannot be changed, and out-of-band edits are detected (sqlite) - PASS

* rejected by the database: UPDATE source_requirement SET statement='x'
* rejected by the database: DELETE FROM source_requirement
* after someone with DDL rights disables the guard and edits AC-1, verify reports: ['AC-1: columns disagree with payload']
* (0.5s)

### E5 · Master controls cannot be duplicated (sqlite) - PASS

* refused: same name, different case
* refused: same name, ampersand + punctuation
* refused: reworded duplicate
* id derives from the canonical name: AIRRP-CTRL-2AD6B7D10454
* master table still holds 1 row
* (0.0s)

### E6 · Nothing becomes 'approved' without a named human, a reason and a valid target (sqlite) - PASS

* rejected: approve a mapping with no reviewer
* rejected: approve with a blank rationale
* rejected: approve with an invented relationship
* rejected: approve a mapping to a draft master control
* rejected: approve a mapping for a withdrawn control
* rejected: insert a mapping directly as approved
* rejected: approve a master control you created yourself
* rejected: approve a master control that has quality flags
* the one properly reviewed mapping went through; approved mappings in the table: 1
* (0.0s)

### E7 · Reviewed data reaches an application database losslessly and idempotently (sqlite) - PASS

* bundle: 1012 requirements, 1 approved master controls, 1 approved mappings
* applied to a fresh application database (separate schema, foreign keys on): {'app_requirement.added': 1012, 'app_master_control.added': 1, 'app_mapping.added': 1}
* applying the same bundle again: {'skipped': True}
* every requirement's content hash in the application equals the source: True
* altered bundle rejected: bundle_sha256 does not match the content (file altered or corrupted)
* bundle with a dangling reference rejected and rolled back: mapping references unknown master control AIRRP-CTRL-DOESNOTEXIST (mappings before/after: 1/1)
* bundle validates against schema/import-bundle.schema.json
* (0.4s)

### E8 · The existing workbook can be migrated without trusting any of it (sqlite) - PASS

* workbook: 1087 rows, 123 master controls, 101 unmapped requirements
* imported 122 masters as drafts and 977 mappings as suggestions; approved masters/mappings: 0/0
* {'PLACEHOLDER_EVIDENCE': 111, 'PLACEHOLDER_TEST_PROCEDURE': 111, 'UNCONFIRMED_FREQUENCY': 111} quality flags raised; approval of 5 flagged drafts was refused 5/5 times
* not imported, with reasons: 7 mappings skipped, 1 master error(s)
* (0.4s)

### E1 · Only verified data is loaded, and the stored data equals the source (postgres) - PASS

* 1194 rows written = 1196 catalog controls - 2 quarantined; table holds 1194
* independent verify (re-hash every stored row, compare to the source file): 0 problem(s)
* content fingerprint of the stored set: 5b90e0cf505c4c81…
* (1.4s)

### E2 · Re-importing never creates duplicates (postgres) - PASS

* two more full imports added [0, 0] rows and recognised [1194, 1194] as unchanged
* 1194 rows, 1194 distinct control ids (duplicates: 0)
* (0.4s)

### E3 · Concurrent imports of the same catalog cannot create duplicates (postgres) - PASS

* 4 importers started the same import at the same instant; rows added per importer: [1194, 0, 0, 0] (errors: none)
* final table: 1194 rows, 1194 distinct ids; exactly one importer won, the others recognised the data as already stored
* (1.8s)

### E4 · Stored source data cannot be changed, and out-of-band edits are detected (postgres) - PASS

* rejected by the database: UPDATE source_requirement SET statement='x'
* rejected by the database: DELETE FROM source_requirement
* after someone with DDL rights disables the guard and edits AC-1, verify reports: ['AC-1: columns disagree with payload']
* (0.5s)

### E5 · Master controls cannot be duplicated (postgres) - PASS

* refused: same name, different case
* refused: same name, ampersand + punctuation
* refused: reworded duplicate
* id derives from the canonical name: AIRRP-CTRL-2AD6B7D10454
* master table still holds 1 row
* (0.1s)

### E6 · Nothing becomes 'approved' without a named human, a reason and a valid target (postgres) - PASS

* rejected: approve a mapping with no reviewer
* rejected: approve with a blank rationale
* rejected: approve with an invented relationship
* rejected: approve a mapping to a draft master control
* rejected: approve a mapping for a withdrawn control
* rejected: insert a mapping directly as approved
* rejected: approve a master control you created yourself
* rejected: approve a master control that has quality flags
* the one properly reviewed mapping went through; approved mappings in the table: 1
* (0.0s)

### E7 · Reviewed data reaches an application database losslessly and idempotently (postgres) - PASS

* bundle: 1012 requirements, 1 approved master controls, 1 approved mappings
* applied to a fresh application database (separate schema, foreign keys on): {'app_requirement.added': 1012, 'app_master_control.added': 1, 'app_mapping.added': 1}
* applying the same bundle again: {'skipped': True}
* every requirement's content hash in the application equals the source: True
* altered bundle rejected: bundle_sha256 does not match the content (file altered or corrupted)
* bundle with a dangling reference rejected and rolled back: mapping references unknown master control AIRRP-CTRL-DOESNOTEXIST (mappings before/after: 1/1)
* bundle validates against schema/import-bundle.schema.json
* (1.0s)

### E8 · The existing workbook can be migrated without trusting any of it (postgres) - PASS

* workbook: 1087 rows, 123 master controls, 101 unmapped requirements
* imported 122 masters as drafts and 977 mappings as suggestions; approved masters/mappings: 0/0
* {'PLACEHOLDER_EVIDENCE': 111, 'PLACEHOLDER_TEST_PROCEDURE': 111, 'UNCONFIRMED_FREQUENCY': 111} quality flags raised; approval of 5 flagged drafts was refused 5/5 times
* not imported, with reasons: 7 mappings skipped, 1 master error(s)
* (1.7s)

### X1 · Different storage engines end up with byte-identical source data (all) - PASS

* sqlite: 5b90e0cf505c4c81bcde6a1f…
* postgres: 5b90e0cf505c4c81bcde6a1f…
* identical content fingerprint across engines
* (0.0s)

## What this does and does not prove

Proven here, by running the real code on the real catalog: extraction is complete and exact relative to the source file; defects in the source or in transit are detected rather than imported; the repository cannot hold duplicates or silently altered source data, even under concurrent imports; no mapping or master control becomes approved without the required human inputs; reviewed data can be handed to another database losslessly and idempotently; the behaviour is the same on every engine tested.

Not provable by software: that NIST's published text is itself correct (C2 shows one place where it is not), and that a reviewer's judgement about which master control implements a requirement is right. Those stay with people; the tool's job is to make every such decision explicit, attributable and irreversible-by-accident.

