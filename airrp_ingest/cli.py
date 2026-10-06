"""airrp-ingest command line."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys

from .bundle import build_bundle, write_bundle
from .curation import field_hash, load_curation
from .masters import DuplicateSuspect, create_master, update_master
from .oscal import load_catalog
from .pipeline import PlanBlocked, apply_plan, build_plan, verify_repository
from .similarity import suggest
from .store import open_repository
from .validate import load_manifest


def _print_plan(plan, limit=15, show_warnings=False):
    s = plan.summary()
    print(json.dumps(s, indent=2))
    for cid, issues in list(plan.quarantined.items())[:limit]:
        for i in issues:
            print(f"  QUARANTINED {cid}: [{i.code}] {i.message}")
    for cid, _, _ in plan.conflicts[:limit]:
        print(f"  CONFLICT {cid}")
    for n in plan.correction_notes:
        print(f"  CORRECTION {n['control_id']} {n['field']}: {n['state']}" + (f" ({n['detail']})" if n["detail"] else ""))
    if show_warnings:
        for i in plan.issues:
            if i.severity != "error":
                print(f"  warning {i.control_id or '-'}: [{i.code}] {i.message}")


def cmd_import(a):
    repo = open_repository(a.db, four_eyes=not a.solo)
    parsed = load_catalog(a.catalog, a.framework_code)
    plan = build_plan(parsed, repo, load_manifest(a.manifest), load_curation(a.curation))
    _print_plan(plan, show_warnings=a.show_warnings)
    if not a.apply:
        print("dry run: nothing written (use --apply)")
        return 3 if plan.blocked else 0
    if a.strict and plan.quarantined:
        print("strict: refusing to apply while controls are quarantined")
        return 2
    try:
        run_id = apply_plan(plan, repo, a.actor or getpass.getuser(), a.catalog)
    except PlanBlocked as exc:
        print(f"BLOCKED: {exc}")
        return 3
    print(f"applied as import_run {run_id}")
    return 2 if plan.quarantined else 0


def _find(parsed, control_id):
    from .normalize import canonical_control_id
    wanted = canonical_control_id(control_id) or control_id
    for c in parsed.controls:
        if c.control_id == wanted:
            return c
    raise SystemExit(f"{control_id}: no such control in this catalog")


def _evidence(parsed, c, field):
    """What the catalog itself says about a control, laid out for the person deciding. Evidence, not an answer."""
    from .normalize import normalize_text
    peers = [o.control_id for o in parsed.controls if o.control_id != c.control_id and o.status == "active"
             and normalize_text(getattr(o, field)) == normalize_text(getattr(c, field)) and normalize_text(getattr(c, field))]
    return {
        "published_value": getattr(c, field),
        "other_controls_with_the_identical_text": peers,
        "parameters": [{"id": p.id, "label": p.label, "choices": list(p.choices)} for p in c.parameters],
        "assessment_objectives": c.assessment_objectives,
        "clauses": len(c.clauses),
        "note": "evidence from the catalog file only; the correct wording comes from the official publication",
    }


def cmd_corrections(a):
    from .normalize import normalize_text
    if a.action == "list":
        repo = open_repository(a.db, four_eyes=not a.solo)
        rows = repo.correction_history(a.framework_code, a.version)
        if not a.history:
            latest = {(r["control_id"], r["field"]): r for r in rows}
            rows = [r for r in latest.values() if r["action"] == "set"]
        for r in rows:
            print(f"{r['control_id']:<10} {r['field']:<9} r{r['revision']} {r['action']:<6} by {r['reviewer']} on {r['reviewed_at']}"
                  f" - {r['problem']}" + (f" [{r['citation']}]" if r["citation"] else ""))
        print(f"{len(rows)} correction record(s)" + ("" if a.history else " in force"))
        return 0
    parsed = load_catalog(a.catalog, a.framework_code)
    if a.action == "check":
        repo = open_repository(a.db, four_eyes=not a.solo)
        plan = build_plan(parsed, repo, load_manifest(a.manifest), load_curation(a.curation))
        for n in plan.correction_notes:
            print(f"{n['control_id']:<10} {n['field']:<9} {n['state']}" + (f" - {n['detail']}" if n["detail"] else ""))
        bad = [i for i in plan.issues if i.code.startswith("CORRECTION_")]
        for i in bad:
            print(f"  {i.severity.upper():<7} {i.control_id or '-'} [{i.code}] {i.message}")
        print(f"{plan.corrected} control(s) read differently from the published text; {len(plan.corrections)} record(s) would be written")
        return 1 if any(i.severity == "error" for i in bad) else 0
    c = _find(parsed, a.control)
    if a.action == "show":
        ev = _evidence(parsed, c, a.field or "statement")
        repo = open_repository(a.db, four_eyes=not a.solo)
        for r in repo.correction_history(parsed.framework.code, parsed.framework.version, c.control_id):
            print(f"recorded: {r['field']} r{r['revision']} {r['action']} by {r['reviewer']} on {r['reviewed_at']}: {r['problem']}")
        print(json.dumps({"control_id": c.control_id, "title": c.title, **ev}, indent=2, ensure_ascii=False))
        return 0
    # draft: a skeleton entry for a person to complete; it will not load until every required field is filled in
    field = a.field or "statement"
    if os.path.exists(a.out):
        raise SystemExit(f"{a.out} already exists; choose a new file and copy the finished entry into the curation file")
    entry = {"framework_version": parsed.framework.version, "control_id": c.control_id, "field": field,
             "value": "", "source_value_sha256": field_hash(getattr(c, field)), "problem": "", "citation": "",
             "reviewer": "", "reviewed_at": "", "_evidence": _evidence(parsed, c, field)}
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump({"corrections": [entry]}, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"wrote {a.out}: fill in value, problem, citation, reviewer and reviewed_at from the official text, then move the "
          "entry into the curation file and run import (dry run first)")
    return 0


def cmd_verify(a):
    repo = open_repository(a.db, four_eyes=not a.solo)
    problems = verify_repository(repo, load_catalog(a.catalog, a.framework_code), load_curation(a.curation))
    for p in problems:
        print("DRIFT", p)
    print("repository verified" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_master(a):
    repo = open_repository(a.db, four_eyes=not a.solo)
    actor = a.actor or getpass.getuser()
    if a.action == "approve":
        repo.approve_master_control(a.id, actor)
        print("approved", a.id)
    elif a.action == "update":
        flags = update_master(repo, actor, a.id, domain=a.domain, frequency=a.frequency, control_type=a.control_type,
                              objective=a.objective, description=a.description, evidence=a.evidence,
                              test_procedure=a.test_procedure)
        print("updated", a.id, "remaining flags:", flags or "none")
    elif a.action == "todo":
        import csv
        rows = repo.fetchall("SELECT * FROM master_control WHERE status='draft' AND quality_flags<>'[]' ORDER BY id")
        with open(a.out, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["id", "name", "flags", "domain", "frequency", "control_type", "objective", "description",
                        "evidence", "test_procedure"])
            for m in rows:
                w.writerow([m["id"], m["name"], m["quality_flags"], m["domain"], m["frequency"], m["control_type"],
                            m["objective"], m["description"], m["evidence"], m["test_procedure"]])
        print(f"wrote {len(rows)} flagged draft(s) to {a.out}; edit the cells, then: master apply-csv --out {a.out}")
    elif a.action == "apply-csv":
        import csv
        done = failed = 0
        with open(a.out, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                try:
                    update_master(repo, actor, row["id"], **{k: row[k] for k in (
                        "domain", "frequency", "control_type", "objective", "description", "evidence", "test_procedure")})
                    done += 1
                except ValueError as exc:
                    failed += 1
                    print(f"  {row['id']}: {exc}")
        print(f"updated {done}, rejected {failed}")
        return 1 if failed else 0
    elif a.action == "list":
        for m in repo.master_controls():
            flags = json.loads(m["quality_flags"])
            print(m["id"], m["status"], m["domain"], m["name"], ("  FLAGS: " + ",".join(flags)) if flags else "")
    else:
        try:
            mid = create_master(repo, actor, name=a.name, objective=a.objective, description=a.description,
                                domain=a.domain, frequency=a.frequency, control_type=a.control_type,
                                evidence=a.evidence, test_procedure=a.test_procedure,
                                distinct_from_reason=a.distinct_reason or "")
        except DuplicateSuspect as exc:
            print(f"refused: looks like an existing master control: {exc}\n"
                  "re-run with --distinct-reason '<why it is different>' if it really is distinct")
            return 1
        print("created draft", mid)
    return 0


def cmd_suggest(a):
    n = suggest(open_repository(a.db, four_eyes=not a.solo), a.framework_code, a.version, a.min_score, a.top_k)
    print(f"{n} new suggestion(s) queued for review")
    return 0


def cmd_review(a):
    repo = open_repository(a.db, four_eyes=not a.solo)
    if a.action == "list":
        for r in repo.open_suggestions():
            print(f"#{r['id']:<5} {r['score']:.2f}  {r['control_id']:<10} -> {r['master_id']} {r['master']}  [{r['evidence']}]")
        return 0
    repo.decide_mapping(a.id, "approved" if a.action == "approve" else "rejected", a.reviewer or getpass.getuser(),
                        a.relationship, a.rationale, a.primary)
    print(a.action, a.id)
    return 0


def cmd_export(a):
    bundle = build_bundle(open_repository(a.db, four_eyes=not a.solo), a.framework_code, a.version)
    if a.format == "requirements":  # exactly the payload shape the existing AIRRP requirement import uses
        keys = ("code", "name", "frequency", "legalText", "legalTitle", "description", "ownerFunction",
                "obligationType", "regulationCode", "sourceReference")
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump([{k: r[k] for k in keys} for r in bundle["requirements"]], fh, ensure_ascii=False, indent=1)
    else:
        write_bundle(bundle, a.out)
    print(f"wrote {a.out}: {len(bundle['requirements'])} requirements, {len(bundle['master_controls'])} master controls,"
          f" {len(bundle['mappings'])} mappings (sha256 {bundle['bundle_sha256'][:12]})")
    return 0


def cmd_migrate(a):
    from .migrate import migrate_dry_or_apply, parse_legacy, read_rows
    repo = open_repository(a.db, four_eyes=not a.solo)
    report = migrate_dry_or_apply(repo, parse_legacy(read_rows(a.workbook)), a.framework_code, a.version,
                                  a.actor or getpass.getuser(), a.apply)
    if a.report:
        with open(a.report, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1, ensure_ascii=False)
    brief = {k: (v if not isinstance(v, list) else len(v)) for k, v in report.items()}
    print(json.dumps(brief, indent=1))
    print("applied" if a.apply else "dry run: nothing written (use --apply)")
    return 0


def cmd_prove(a):
    from .proof import PostgresProofEngine, SqliteProofEngine, render, run_proof
    engines = [SqliteProofEngine()]
    if a.postgres != "off":
        engines.append(PostgresProofEngine(None if a.postgres == "auto" else a.postgres))
    claims, meta = run_proof(a.catalog, a.manifest, a.curation, a.workbook, engines,
                             schema_path=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema", "import-bundle.schema.json"))
    text = render(claims, meta)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"wrote {a.out}")
    failed = [c for c in claims if c.status == "FAIL"]
    print(f"{sum(c.status == 'PASS' for c in claims)} passed, {len(failed)} failed, {sum(c.status == 'SKIP' for c in claims)} skipped")
    return 1 if failed else 0


def cmd_serve(a):
    from .review_ui import serve
    serve(a.db, a.framework_code, a.version, a.port, four_eyes=not a.solo)
    return 0


def cmd_report(a):
    repo = open_repository(a.db, four_eyes=not a.solo)
    print(json.dumps(repo.coverage(a.framework_code, a.version), indent=2))
    for row in repo.fetchall(
            "SELECT code, severity, status, COUNT(*) n FROM review_item GROUP BY 1,2,3 ORDER BY n DESC"):
        print(f"review_item {row['status']:<7} {row['severity']:<7} {row['code']:<28} {row['n']}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="airrp-ingest")
    p.add_argument("--db", default="airrp.db", help="SQLite file path or postgresql:// URL")
    p.add_argument("--actor")
    p.add_argument("--solo", action="store_true", help="allow the same person to create and approve a master control")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("import"); s.set_defaults(fn=cmd_import)
    s.add_argument("catalog"); s.add_argument("--framework-code", default="NIST-SP-800-53")
    s.add_argument("--manifest"); s.add_argument("--curation"); s.add_argument("--apply", action="store_true")
    s.add_argument("--strict", action="store_true"); s.add_argument("--show-warnings", action="store_true")

    s = sub.add_parser("verify"); s.set_defaults(fn=cmd_verify)
    s.add_argument("catalog"); s.add_argument("--framework-code", default="NIST-SP-800-53"); s.add_argument("--curation")

    s = sub.add_parser("corrections"); s.set_defaults(fn=cmd_corrections)
    s.add_argument("action", choices=["check", "show", "draft", "list"])
    s.add_argument("catalog", nargs="?"); s.add_argument("--framework-code", default="NIST-SP-800-53")
    s.add_argument("--version"); s.add_argument("--manifest"); s.add_argument("--curation")
    s.add_argument("--control"); s.add_argument("--field", choices=["title", "statement", "guidance"])
    s.add_argument("--out"); s.add_argument("--history", action="store_true")

    s = sub.add_parser("master"); s.set_defaults(fn=cmd_master)
    s.add_argument("action", choices=["add", "approve", "list", "update", "todo", "apply-csv"])
    s.add_argument("--id"); s.add_argument("--out")
    for f in ("name", "objective", "description", "domain", "frequency", "control-type", "evidence",
              "test-procedure", "distinct-reason"):
        s.add_argument("--" + f)

    s = sub.add_parser("suggest"); s.set_defaults(fn=cmd_suggest)
    s.add_argument("--framework-code", default="NIST-SP-800-53"); s.add_argument("--version", required=True)
    s.add_argument("--min-score", type=float, default=0.25); s.add_argument("--top-k", type=int, default=3)

    s = sub.add_parser("review"); s.set_defaults(fn=cmd_review)
    s.add_argument("action", choices=["list", "approve", "reject"]); s.add_argument("--id", type=int)
    s.add_argument("--reviewer"); s.add_argument("--relationship"); s.add_argument("--rationale", default="")
    s.add_argument("--primary", action="store_true")

    s = sub.add_parser("export"); s.set_defaults(fn=cmd_export)
    s.add_argument("--framework-code", default="NIST-SP-800-53"); s.add_argument("--version", required=True)
    s.add_argument("--format", choices=["bundle", "requirements"], default="bundle"); s.add_argument("--out", required=True)

    s = sub.add_parser("migrate-workbook"); s.set_defaults(fn=cmd_migrate)
    s.add_argument("workbook"); s.add_argument("--framework-code", default="NIST-SP-800-53")
    s.add_argument("--version", required=True); s.add_argument("--report"); s.add_argument("--apply", action="store_true")

    s = sub.add_parser("prove"); s.set_defaults(fn=cmd_prove)
    s.add_argument("--catalog", required=True); s.add_argument("--manifest", required=True)
    s.add_argument("--curation"); s.add_argument("--workbook"); s.add_argument("--out")
    s.add_argument("--postgres", default="auto", help="auto (embedded server if pgserver is installed) | off | postgresql://… URL")

    s = sub.add_parser("serve"); s.set_defaults(fn=cmd_serve)
    s.add_argument("--framework-code", default="NIST-SP-800-53"); s.add_argument("--version", required=True)
    s.add_argument("--port", type=int, default=8765)

    s = sub.add_parser("report"); s.set_defaults(fn=cmd_report)
    s.add_argument("--framework-code", default="NIST-SP-800-53"); s.add_argument("--version", required=True)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
