"""``airrp-ingest prove``: an executable proof that the import is complete, duplicate-free, tamper-evident and handed over
losslessly, run against the real catalog on every storage engine. Every claim records the numbers it observed; any failed
claim makes the command exit non-zero. Nothing here is mocked: it uses the same code paths as a production import.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field

from . import __version__
from .airrp_export import airrp_requirement
from .bundle import build_bundle
from .curation import Curation, field_hash, load_curation
from .normalize import content_hash
from .masters import DuplicateSuspect, create_master, master_id_for, update_master
from .migrate import migrate_dry_or_apply, parse_legacy, read_rows
from .oscal import load_catalog
from .pipeline import PlanBlocked, apply_plan, build_plan, verify_repository
from .reference_consumer import BundleRejected, apply_bundle
from .store import SqliteRepository, open_repository
from .validate import load_manifest, validate

FW = "NIST-SP-800-53"
MASTER_A = dict(
    name="Manage User and System Accounts",
    objective="Ensure accounts are uniquely identified, authorised, reviewed and removed.",
    description="Define account types, assign managers, require approvals, monitor use and disable inactive accounts.",
    domain="Identity and Access", frequency="Continuous", control_type="Preventive",
    evidence="Account inventory, approval records and removal tickets.",
    test_procedure="Sample accounts across joiner, mover and leaver events and verify authorisation and timely removal.")
MASTER_B = dict(
    name="Enforce Information Flow Control", objective="Control where information may flow between system components.",
    description="Define flow policies and enforce them at boundaries with approved mechanisms.",
    domain="NetworkSecurity", frequency="Continuous", control_type="Preventive",
    evidence="Flow policy, boundary configurations and enforcement logs.",
    test_procedure="Attempt prohibited flows and verify they are blocked and logged.")


@dataclass
class Claim:
    id: str
    title: str
    engine: str
    status: str  # PASS | FAIL | SKIP
    evidence: list = field(default_factory=list)


class Skip(Exception):
    pass


class Runner:
    def __init__(self):
        self.claims: list = []

    def run(self, cid, title, engine, fn):
        started = time.time()
        try:
            ok, evidence = fn()
            status = "PASS" if ok else "FAIL"
        except Skip as why:
            status, evidence = "SKIP", [str(why)]
        except Exception as exc:  # a crash is a failed claim, never a silent pass
            status, evidence = "FAIL", [f"{type(exc).__name__}: {exc}", traceback.format_exc(limit=3).strip().splitlines()[-1]]
        evidence = list(evidence) + [f"({time.time() - started:.1f}s)"]
        self.claims.append(Claim(cid, title, engine, status, evidence))
        print(f"  [{status}] {cid} {title} ({engine})", flush=True)
        return status == "PASS"


# ----------------------------------------------------------------------------------------------- engines
class SqliteProofEngine:
    name = "sqlite"
    label = f"SQLite {sqlite3.sqlite_version}"

    def new(self):
        path = os.path.join(tempfile.mkdtemp(prefix="airrp-proof-"), "proof.db")
        return SqliteRepository(path, check_same_thread=False), path


class PostgresProofEngine:
    name = "postgres"

    def __init__(self, url: str = None):
        self.url, self._server, self._n = url, None, 0
        self.label = "PostgreSQL"

    def _dsn(self):
        if self.url:
            return self.url
        try:
            import pgserver
            import psycopg  # noqa: F401
        except ImportError:
            raise Skip('PostgreSQL not available: pip install pgserver "psycopg[binary]" (embedded server) '
                       "or pass --postgres postgresql://user:pass@host/db")
        if self._server is None:
            self._server = pgserver.get_server(tempfile.mkdtemp(prefix="airrp-proof-pg-"))
        return self._server.get_uri()

    def new(self):
        from .pg_store import PostgresRepository
        dsn = self._dsn()
        self._n += 1
        schema = f"proof_{os.getpid()}_{self._n}_{int(time.time())}"
        repo = PostgresRepository(dsn, schema=schema)
        version = repo.fetchone("SHOW server_version")[0]
        self.label = f"PostgreSQL {version}"
        return repo, f"{dsn}{'&' if '?' in dsn else '?'}options=-csearch_path%3D{schema}"

    def close(self):
        if self._server is not None:
            self._server.cleanup()


def _sqlish(repo):
    return repo.fetchone("SELECT COUNT(*) FROM source_requirement")[0]


def fingerprint(repo) -> str:
    rows = repo.fetchall("SELECT control_id, content_hash FROM source_requirement ORDER BY control_id")
    return hashlib.sha256("\n".join(f"{r['control_id']}:{r['content_hash']}" for r in rows).encode()).hexdigest()


# ----------------------------------------------------------------------------------------------- mutations
def _controls_of(cat):
    def walk(ctrls, holder):
        for c in list(ctrls):
            yield c, ctrls
            yield from walk(c.get("controls", []), c.get("controls", []))
    for g in cat["groups"]:
        yield from walk(g["controls"], g["controls"])


def _find(cat, oscal_id):
    for c, holder in _controls_of(cat):
        if c["id"] == oscal_id:
            return c, holder
    raise KeyError(oscal_id)


def _write(doc, text=None):
    d = tempfile.mkdtemp(prefix="airrp-fault-")
    path = os.path.join(d, "catalog.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text if text is not None else json.dumps(doc))
    return path


def _mut_delete(cat):
    c, holder = _find(cat, "sc-7.4")
    holder.remove(c)


def _mut_duplicate(cat):
    c, _ = _find(cat, "ac-3")
    cat["groups"][0]["controls"].append(copy.deepcopy(c))


def _mut_param(cat):
    c, _ = _find(cat, "ac-2")
    c["params"][0]["id"] = "renamed-param"


def _mut_identical(cat):
    a, _ = _find(cat, "ac-3")
    b, _ = _find(cat, "ac-4")
    b["parts"] = [copy.deepcopy(p) for p in a["parts"] if p["name"] == "statement"] + \
                 [p for p in b["parts"] if p["name"] != "statement"]


def _mut_label(cat):
    c, _ = _find(cat, "au-6")
    for p in c["props"]:
        if p["name"] == "label" and "class" not in p:
            p["value"] = "AU-7"


def _mut_version(cat):
    cat["metadata"]["version"] = "5.3.0"


def _mut_empty_statement(cat):
    c, _ = _find(cat, "ia-2")
    c["parts"] = [p for p in c["parts"] if p["name"] != "statement"]


FAULTS = [  # name, mutation, issue codes that must be raised, must the whole run be blocked?
    ("delete one control (SC-7(4))", _mut_delete, {"COUNT_MISMATCH", "ID_SET_MISMATCH"}, True),
    ("duplicate a control id (AC-3)", _mut_duplicate, {"DUPLICATE_ID", "COUNT_MISMATCH"}, True),
    ("bump catalog version without a manifest", _mut_version, {"MANIFEST_VERSION"}, True),
    ("break a parameter reference (AC-2, which has enhancements)", _mut_param, {"UNRESOLVED_PARAM"}, False),
    ("copy AC-3's text into AC-4", _mut_identical, {"DUPLICATE_STATEMENT"}, False),
    ("label disagrees with id (AU-6)", _mut_label, {"ID_LABEL_MISMATCH"}, False),
    ("remove an active control's statement (IA-2)", _mut_empty_statement, {"EMPTY_STATEMENT"}, False),
]


# ----------------------------------------------------------------------------------------------- the proof
def run_proof(catalog_path, manifest_path, curation_path=None, workbook=None, engines=None, schema_path=None):
    runner = Runner()
    manifest = load_manifest(manifest_path)
    curation = load_curation(curation_path)
    parsed = load_catalog(catalog_path, FW)
    engines = engines or [SqliteProofEngine(), PostgresProofEngine()]
    meta = {"catalog": os.path.basename(catalog_path), "catalog_sha256": parsed.framework.source_sha256,
            "framework": f"{FW}@{parsed.framework.version}", "workbook": bool(workbook)}

    def synthetic(cid, fld, value, **extra):
        c = next(c for c in parsed.controls if c.control_id == cid)
        return {"framework_version": parsed.framework.version, "control_id": cid, "field": fld, "value": value,
                "source_value_sha256": field_hash(getattr(c, fld)), "problem": "SYNTHETIC: exercised by the proof only",
                "citation": "SYNTHETIC: not NIST wording", "reviewer": "proof", "reviewed_at": "1970-01-01", **extra}

    print("independent of storage engine:")
    scratch = SqliteRepository(":memory:")

    def c1():
        issues = validate(parsed, manifest)
        bad = [i for i in issues if i.code in ("COUNT_MISMATCH", "ID_SET_MISMATCH", "MANIFEST_VERSION",
                                                "UNRESOLVED_PARAM", "UNRENDERED_PLACEHOLDER", "EMPTY_STATEMENT",
                                                "ORPHAN_ENHANCEMENT", "DUPLICATE_ID", "ID_LABEL_MISMATCH")]
        ctl = parsed.controls
        ids = [c.control_id for c in ctl]
        fams = {c.family for c in ctl}
        ok = not bad and len(ids) == len(set(ids)) == manifest["expected"]["controls_total"]
        manifest_issues = [i for i in bad if i.code in ("COUNT_MISMATCH", "ID_SET_MISMATCH", "MANIFEST_VERSION")]
        other = [i for i in bad if i not in manifest_issues]
        return ok, [f"{len(ctl)} controls = {sum(c.kind == 'base' for c in ctl)} base + {sum(c.kind == 'enhancement' for c in ctl)} enhancements, "
                    f"{len(fams)} families; {sum(c.status == 'active' for c in ctl)} active, {sum(c.status == 'withdrawn' for c in ctl)} withdrawn",
                    ("control-id set hash, totals and per-kind counts equal the manifest; every id appears exactly once"
                     if not manifest_issues and len(ids) == len(set(ids))
                     else "MISMATCH against the manifest: " + "; ".join(i.message for i in manifest_issues) +
                          ("" if len(ids) == len(set(ids)) else f"; duplicate ids present ({len(ids) - len(set(ids))})")),
                    ("0 unresolved parameters, 0 unrendered placeholders, 0 empty statements, 0 orphan enhancements"
                     if not other else "PROBLEMS: " + "; ".join(f"{i.control_id}: {i.code}" for i in other[:8])),
                    f"{sum(i.code == 'DANGLING_LINK' for i in issues)} non-blocking dangling-link warning(s) reported, not hidden"]
    runner.run("C1", "The whole catalog is extracted, nothing missing, nothing invented", "all", c1)

    def c2():
        dup = [i for i in validate(parsed, manifest) if i.code == "DUPLICATE_STATEMENT"]
        ids = sorted({i.control_id for i in dup})
        by = {c.control_id: c for c in parsed.controls}
        sample = by[ids[0]].statement[:90] if ids else ""
        return ids == ["SA-15(12)", "SA-15(13)"], [
            f"flagged: {ids}", f"both carry the sentence '{sample}…' although SA-15(13) is titled '{by['SA-15(13)'].title}'",
            "both are held back from the repository until a person waives them, or corrects the wording with a citation (see E9)"]
    runner.run("C2", "A defect inside NIST's own file is detected instead of imported", "all", c2)

    def c3():
        results, ok = [], True
        with open(catalog_path, encoding="utf-8") as fh:
            doc = json.load(fh)
        for name, mutate, codes, blocks in FAULTS:
            d = copy.deepcopy(doc)
            mutate(d["catalog"])
            plan = build_plan(load_catalog(_write(d), FW), scratch, manifest, curation)
            seen = {i.code for i in plan.issues}
            hit = codes <= seen and (plan.blocked == blocks if blocks else bool(plan.quarantined))
            loaded_clean = len(plan.to_add) > 1000 if not blocks else True
            ok &= hit and loaded_clean
            results.append(f"{'caught' if hit else 'MISSED'}: {name} -> {sorted(codes & seen)}"
                           f" ({'run blocked' if plan.blocked else str(len(plan.quarantined)) + ' controls quarantined, ' + str(len(plan.to_add)) + ' others still loadable'})")
        with open(catalog_path, encoding="utf-8") as fh:
            text = fh.read()
        for name, payload in (("truncate the file at 50%", text[: len(text) // 2]), ("not a catalog at all", '{"foo": 1}')):
            try:
                load_catalog(_write(None, payload), FW)
                ok = False
                results.append(f"MISSED: {name}")
            except (ValueError, KeyError) as exc:
                results.append(f"caught: {name} -> {type(exc).__name__}, nothing partially parsed")
        return ok, results
    runner.run("C3", "Corrupted or tampered source files are caught before anything is written", "all", c3)

    legacy_rows = None
    if workbook:
        def c4():
            rows = read_rows(workbook)
            payloads = {}
            for r in rows:
                p = json.loads(r[36])
                payloads[p["code"]] = p
            mine = {r["code"]: r for r in map(airrp_requirement, [c for c in parsed.controls if c.status == "active"])}
            exact = sum(payloads[k] == mine[k] for k in payloads if k in mine)
            soft = [k for k in payloads if k in mine and payloads[k] != mine[k]
                    and all(str(payloads[k][f]).strip() == str(mine[k][f]).strip() for f in payloads[k])]
            hard = [k for k in payloads if k in mine and payloads[k] != mine[k] and k not in soft]
            return (set(payloads) == set(mine) and not hard), [
                f"{len(payloads)} requirement payloads from your current system vs {len(mine)} generated from the catalog; same code set: {set(payloads) == set(mine)}",
                f"{exact} byte-identical, {len(soft)} differ only by leading/trailing whitespace ({', '.join(soft) or 'none'}), {len(hard)} differ in content"]
        runner.run("C4", "Output matches what your current system already produced for NIST SP 800-53", "all", c4)
        legacy_rows = read_rows(workbook)
    else:
        runner.claims.append(Claim("C4", "Output matches what your current system already produced", "all", "SKIP",
                                   ["pass --workbook Book1.xlsx to compare against the existing payloads"]))

    base_fp = {}
    for engine in engines:
        print(f"{engine.name}:")
        n = engine.name
        try:
            repo, spec = engine.new()
        except Skip as why:
            runner.claims.append(Claim("E*", "All storage-engine claims", n, "SKIP", [str(why)]))
            print(f"  [SKIP] {why}")
            continue
        meta.setdefault("engines", []).append(engine.label)
        state = {}

        def e1():
            plan = build_plan(parsed, repo, manifest, curation)
            apply_plan(plan, repo, "proof")
            state["plan"] = plan
            total = _sqlish(repo)
            expect = len(parsed.controls) - len(plan.quarantined)
            problems = verify_repository(repo, parsed, curation)
            base_fp[n] = fingerprint(repo)
            return total == expect == plan.added and not problems, [
                f"{plan.added} rows written = {len(parsed.controls)} catalog controls - {len(plan.quarantined)} quarantined; table holds {total}",
                f"independent verify (re-hash every stored row, compare to the source file): {len(problems)} problem(s)",
                f"content fingerprint of the stored set: {base_fp[n][:16]}…"]
        runner.run("E1", "Only verified data is loaded, and the stored data equals the source", n, e1)

        def e2():
            counts = []
            for _ in range(2):
                p = build_plan(parsed, repo, manifest, curation)
                apply_plan(p, repo, "proof")
                counts.append((p.added, len(p.unchanged)))
            distinct = repo.fetchone("SELECT COUNT(DISTINCT control_id) FROM source_requirement")[0]
            total = _sqlish(repo)
            return all(a == 0 for a, _ in counts) and distinct == total, [
                f"two more full imports added {[a for a, _ in counts]} rows and recognised {[u for _, u in counts]} as unchanged",
                f"{total} rows, {distinct} distinct control ids (duplicates: {total - distinct})"]
        runner.run("E2", "Re-importing never creates duplicates", n, e2)

        def e3():
            r2, spec2 = engine.new()
            workers, ready, out, errs = 4, threading.Barrier(4), [], []
            parsed_local = parsed

            def work(i):
                try:
                    r = open_repository(spec2)
                    plan = build_plan(parsed_local, r, manifest, curation)
                    ready.wait(timeout=60)
                    apply_plan(plan, r, f"worker{i}")
                    out.append(plan.added)
                except Exception as exc:  # noqa: BLE001
                    errs.append(f"{type(exc).__name__}: {exc}")
            threads = [threading.Thread(target=work, args=(i,)) for i in range(workers)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            total = _sqlish(r2)
            distinct = r2.fetchone("SELECT COUNT(DISTINCT control_id) FROM source_requirement")[0]
            return (not errs and sum(out) == total == distinct == len(parsed.controls) - 2), [
                f"{workers} importers started the same import at the same instant; rows added per importer: {sorted(out, reverse=True)} (errors: {errs or 'none'})",
                f"final table: {total} rows, {distinct} distinct ids; exactly one importer won, the others recognised the data as already stored"]
        runner.run("E3", "Concurrent imports of the same catalog cannot create duplicates", n, e3)

        def e4():
            ev, ok = [], True
            for sql in ("UPDATE source_requirement SET statement='x' WHERE control_id='AC-1'",
                        "DELETE FROM source_requirement WHERE control_id='AC-1'"):
                try:
                    repo.execute(sql)
                    ok = False
                    ev.append(f"NOT REJECTED: {sql}")
                except repo.DatabaseError:
                    ev.append(f"rejected by the database: {sql.split(' WHERE')[0]}")
            fp_before = fingerprint(repo)
            ok &= fp_before == base_fp[n]
            repo._simulate_out_of_band_tampering()
            repo.execute("UPDATE source_requirement SET statement=statement || ' (edited)' WHERE control_id='AC-1'")
            problems = verify_repository(repo, parsed, curation)
            ok &= len(problems) >= 1 and all("AC-1" in p for p in problems)
            ev.append(f"after someone with DDL rights disables the guard and edits AC-1, verify reports: {problems}")
            return ok, ev
        runner.run("E4", "Stored source data cannot be changed, and out-of-band edits are detected", n, e4)

        def e5():
            r, _ = engine.new()
            first = create_master(r, "alice", **MASTER_A)
            attempts = {
                "same name, different case": {**MASTER_A, "name": "manage user and system accounts"},
                "same name, ampersand + punctuation": {**MASTER_A, "name": "Manage User & System-Accounts!!"},
                "reworded duplicate": {**MASTER_A, "name": "Manage Users and System Account Lifecycle"},
            }
            refused = []
            for label, kw in attempts.items():
                try:
                    create_master(r, "alice", **kw)
                    refused.append(f"NOT REFUSED: {label}")
                except (DuplicateSuspect, r.IntegrityError, r.DatabaseError):
                    refused.append(f"refused: {label}")
            ok = all(x.startswith("refused") for x in refused) and len(r.master_controls()) == 1
            ok &= first == master_id_for("MANAGE user and SYSTEM accounts")
            return ok, refused + [f"id derives from the canonical name: {first}", f"master table still holds {len(r.master_controls())} row"]
        runner.run("E5", "Master controls cannot be duplicated", n, e5)

        def e6():
            a = create_master(repo, "alice", **MASTER_A)
            b = create_master(repo, "alice", **MASTER_B)
            repo.approve_master_control(a, "bob")
            req = repo.fetchone("SELECT id FROM source_requirement WHERE control_id='AC-2'")[0]
            wd = repo.fetchone("SELECT id FROM source_requirement WHERE status='withdrawn' ORDER BY id LIMIT 1")[0]
            repo.suggest_mapping(req, a, 0.5, "proof")
            m_ok = repo.open_suggestions()[0]["id"]
            repo.suggest_mapping(req, b, 0.4, "proof")
            m_draft = repo.fetchone("SELECT id FROM mapping WHERE master_control_id=?", (b,))[0]
            repo.suggest_mapping(wd, a, 0.4, "proof")
            m_wd = repo.fetchone("SELECT id FROM mapping WHERE requirement_id=?", (wd,))[0]
            flagged = create_master(repo, "alice", **{**MASTER_B, "name": "Flagged Legacy Control", "objective": "x.", "description": "y."})
            repo.execute("UPDATE master_control SET quality_flags='[\"PLACEHOLDER_EVIDENCE\"]' WHERE id=?", (flagged,))
            attempts = [
                ("approve a mapping with no reviewer", lambda: repo.decide_mapping(m_ok, "approved", "", "equivalent", "r")),
                ("approve with a blank rationale", lambda: repo.decide_mapping(m_ok, "approved", "bob", "equivalent", "  ")),
                ("approve with an invented relationship", lambda: repo.decide_mapping(m_ok, "approved", "bob", "kinda-similar", "r")),
                ("approve a mapping to a draft master control", lambda: repo.decide_mapping(m_draft, "approved", "bob", "equivalent", "r")),
                ("approve a mapping for a withdrawn control", lambda: repo.decide_mapping(m_wd, "approved", "bob", "equivalent", "r")),
                ("insert a mapping directly as approved", lambda: repo.execute(
                    "INSERT INTO mapping (requirement_id, master_control_id, status, relationship, rationale, reviewer)"
                    " VALUES (?,?,'approved','equivalent','r','x')", (req, a))),
                ("approve a master control you created yourself", lambda: repo.approve_master_control(b, "alice")),
                ("approve a master control that has quality flags", lambda: repo.approve_master_control(flagged, "bob")),
            ]
            rejected = []
            for label, fn in attempts:
                try:
                    fn()
                    rejected.append(f"NOT REJECTED: {label}")
                except (ValueError, repo.DatabaseError):
                    rejected.append(f"rejected: {label}")
            repo.decide_mapping(m_ok, "approved", "bob", "equivalent", "AC-2 is account management", True)
            approved = repo.fetchone("SELECT COUNT(*) FROM mapping WHERE status='approved'")[0]
            ok = all(x.startswith("rejected") for x in rejected) and approved == 1
            return ok, rejected + [f"the one properly reviewed mapping went through; approved mappings in the table: {approved}"]
        runner.run("E6", "Nothing becomes 'approved' without a named human, a reason and a valid target", n, e6)

        def e7():
            bundle = build_bundle(repo, FW, parsed.framework.version)
            target, _ = engine.new()
            counts = apply_bundle(target, bundle)
            again = apply_bundle(target, bundle)
            src = {r["code"]: r["content_hash"] for r in bundle["requirements"]}
            got = {r["code"]: r["content_hash"] for r in target.fetchall("SELECT code, content_hash FROM app_requirement")}
            ev = [f"bundle: {len(bundle['requirements'])} requirements, {len(bundle['master_controls'])} approved master controls, {len(bundle['mappings'])} approved mappings",
                  f"applied to a fresh application database (separate schema, foreign keys on): {counts}", f"applying the same bundle again: {again}",
                  f"every requirement's content hash in the application equals the source: {got == src}"]
            ok = got == src and again.get("skipped") and len(src) == len(parsed.controls) - 2 - sum(c.status == "withdrawn" for c in parsed.controls)
            tampered = json.loads(json.dumps(bundle))
            tampered["requirements"][0]["legalText"] += " "
            try:
                apply_bundle(target, tampered)
                ok = False
                ev.append("NOT REJECTED: altered bundle")
            except BundleRejected as exc:
                ev.append(f"altered bundle rejected: {exc}")
            dangling = json.loads(json.dumps(bundle))
            dangling["mappings"][0]["master_control_id"] = "AIRRP-CTRL-DOESNOTEXIST"
            from .normalize import canonical_json, sha256_hex
            dangling["bundle_sha256"] = sha256_hex(canonical_json({k: v for k, v in dangling.items() if k not in ("bundle_sha256", "generated_at")}))
            before = target.fetchone("SELECT COUNT(*) FROM app_mapping")[0]
            try:
                apply_bundle(target, dangling)
                ok = False
                ev.append("NOT REJECTED: dangling reference")
            except BundleRejected as exc:
                ev.append(f"bundle with a dangling reference rejected and rolled back: {exc} (mappings before/after: {before}/{target.fetchone('SELECT COUNT(*) FROM app_mapping')[0]})")
            if schema_path and os.path.exists(schema_path):
                try:
                    import jsonschema
                    with open(schema_path, encoding="utf-8") as fh:
                        jsonschema.validate(bundle, json.load(fh))
                    ev.append("bundle validates against schema/import-bundle.schema.json")
                except ImportError:
                    ev.append("(jsonschema not installed: schema validation skipped)")
            return ok, ev
        runner.run("E7", "Reviewed data reaches an application database losslessly and idempotently", n, e7)

        if legacy_rows:
            def e8():
                r, _ = engine.new()
                apply_plan(build_plan(parsed, r, manifest, curation), r, "proof")
                report = migrate_dry_or_apply(r, parse_legacy(legacy_rows), FW, parsed.framework.version, "proof", True)
                drafts = r.fetchone("SELECT COUNT(*) FROM master_control WHERE status='draft'")[0]
                approved = r.fetchone("SELECT COUNT(*) FROM master_control WHERE status='approved'")[0]
                suggested = r.fetchone("SELECT COUNT(*) FROM mapping WHERE status='suggested'")[0]
                approved_m = r.fetchone("SELECT COUNT(*) FROM mapping WHERE status<>'suggested'")[0]
                blocked_approvals = 0
                for row in r.fetchall("SELECT id FROM master_control WHERE quality_flags<>'[]' ORDER BY id LIMIT 5"):
                    try:
                        r.approve_master_control(row["id"], "reviewer")
                    except ValueError:
                        blocked_approvals += 1
                return (approved == 0 and approved_m == 0 and drafts == report["masters_imported"]
                        and suggested == report["mappings_suggested"]), [
                    f"workbook: {report['legacy_rows']} rows, {report['legacy_masters']} master controls, {len(report['legacy_unmapped_requirements'])} unmapped requirements",
                    f"imported {drafts} masters as drafts and {suggested} mappings as suggestions; approved masters/mappings: {approved}/{approved_m}",
                    f"{sum(report['master_flags'].values()) and report['master_flags']} quality flags raised; approval of 5 flagged drafts was refused {blocked_approvals}/5 times",
                    f"not imported, with reasons: {len(report['mappings_skipped'])} mappings skipped, {len(report['errors'])} master error(s)"]
            runner.run("E8", "The existing workbook can be migrated without trusting any of it", n, e8)

        def e9():
            r, _ = engine.new()
            sa13 = synthetic("SA-15(13)", "statement", "SYNTHETIC TEST WORDING for [secure logging format(s)], [events types to log], [level of detail to log].")
            ac1 = synthetic("AC-1", "title", "SYNTHETIC TITLE")
            plan = build_plan(parsed, r, manifest, Curation(waivers=curation.waivers, corrections=[sa13, ac1]))
            apply_plan(plan, r, "proof")
            published = {c.control_id: c for c in parsed.controls}
            mirror_ok = all(tuple(r.fetchone("SELECT statement, title FROM source_requirement WHERE control_id=?", (cid,)))
                            == (published[cid].statement, published[cid].title) for cid in ("SA-15(13)", "AC-1"))
            bundle = build_bundle(r, FW, parsed.framework.version)
            differing = sorted(b["control_id"] for b in bundle["requirements"] if b["content_hash"] != content_hash(published[b["control_id"]]))
            immutable = 0
            for sql in ("UPDATE source_correction SET value='x'", "DELETE FROM source_correction"):
                try:
                    r.execute(sql)
                except r.DatabaseError:
                    immutable += 1
            retired = build_plan(parsed, r, manifest, Curation(waivers=curation.waivers, corrections=[sa13, {**ac1, "retired": {
                "reviewer": "proof", "reviewed_at": "1970-01-02", "reason": "SYNTHETIC: exercised by the proof only"}}]))
            apply_plan(retired, r, "proof")
            title_back = r.fetchone("SELECT title, corrected FROM requirement_effective WHERE control_id='AC-1'")
            history = [(h["revision"], h["action"]) for h in r.correction_history(FW, parsed.framework.version, "AC-1")]
            problems = verify_repository(r, parsed, Curation(waivers=curation.waivers, corrections=[sa13, {**ac1, "retired": {
                "reviewer": "proof", "reviewed_at": "1970-01-02", "reason": "x"}}]))
            ok = (not plan.quarantined and mirror_ok and differing == ["AC-1", "SA-15(13)"] and immutable == 2
                  and tuple(title_back) == (published["AC-1"].title, 0) and history == [(1, "set"), (2, "retire")] and not problems)
            return ok, [
                "SYNTHETIC wording on two controls, to exercise the mechanism; this proves behaviour, it records no NIST text",
                f"with a correction on SA-15(13) the duplicate pair is released: {len(plan.quarantined)} controls held back, {plan.added} rows written",
                f"the stored source text of both corrected controls still equals the published text: {mirror_ok}",
                f"requirements whose exported hash differs from the published hash: {differing} (every other requirement is unchanged)",
                f"UPDATE / DELETE on the correction record rejected by the database: {immutable}/2",
                f"retiring the AC-1 correction: published title back in force, revision history {history}",
                f"independent verify after all of that: {len(problems)} problem(s)"]
        runner.run("E9", "A correction sits over the published text without touching it, and can be withdrawn", n, e9)

    if len(base_fp) > 1:
        def x1():
            vals = set(base_fp.values())
            return len(vals) == 1, [f"{k}: {v[:24]}…" for k, v in base_fp.items()] + ["identical content fingerprint across engines"]
        runner.run("X1", "Different storage engines end up with byte-identical source data", "all", x1)
    for engine in engines:
        if hasattr(engine, "close"):
            engine.close()
    return runner.claims, meta


def render(claims, meta) -> str:
    n_pass = sum(c.status == "PASS" for c in claims)
    n_fail = sum(c.status == "FAIL" for c in claims)
    n_skip = sum(c.status == "SKIP" for c in claims)
    lines = [
        "# Proof run", "",
        f"**Verdict: {'ALL CLAIMS PASSED' if not n_fail else f'{n_fail} CLAIM(S) FAILED'}** - {n_pass} passed, {n_fail} failed, {n_skip} skipped.", "",
        f"* Source: `{meta['catalog']}` (sha256 `{meta['catalog_sha256'][:16]}…`), {meta['framework']}",
        f"* Engines exercised: {', '.join(meta.get('engines', [])) or 'none'}",
        f"* Tool: airrp-ingest {__version__}, Python {platform.python_version()} on {platform.system()}",
        f"* Existing-system comparison: {'included' if meta['workbook'] else 'not run (no workbook supplied)'}", "",
        "Reproduce: `python -m airrp_ingest prove --catalog data/catalog.json --manifest manifests/nist-sp-800-53-5.2.0.json "
        "--workbook Book1.xlsx --out docs/PROOF.md`", "", "| # | Claim | Engine | Result |", "|---|---|---|---|"]
    for c in claims:
        lines.append(f"| {c.id} | {c.title} | {c.engine} | {c.status} |")
    lines.append("")
    for c in claims:
        lines += [f"### {c.id} · {c.title} ({c.engine}) - {c.status}", ""] + [f"* {e}" for e in c.evidence] + [""]
    lines += [
        "## What this does and does not prove", "",
        "Proven here, by running the real code on the real catalog: extraction is complete and exact relative to the source file; "
        "defects in the source or in transit are detected rather than imported; the repository cannot hold duplicates or silently "
        "altered source data, even under concurrent imports; no mapping or master control becomes approved without the required human "
        "inputs; reviewed data can be handed to another database losslessly and idempotently; the behaviour is the same on every engine tested.", "",
        "Not provable by software: that NIST's published text is itself correct (C2 shows one place where it is not), and that a reviewer's "
        "judgement about which master control implements a requirement is right. Those stay with people; the tool's job is to make "
        "every such decision explicit, attributable and irreversible-by-accident.", ""]
    return "\n".join(lines)
