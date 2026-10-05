"""airrp-ingest command line."""
from __future__ import annotations

import argparse
import getpass
import json
import sys

from .bundle import build_bundle, write_bundle
from .curation import load_curation
from .masters import DuplicateSuspect, create_master, update_master
from .oscal import load_catalog
from .pipeline import PlanBlocked, apply_plan, build_plan, verify_repository
from .similarity import suggest
from .store import SqliteRepository
from .validate import load_manifest


def _print_plan(plan, limit=15, show_warnings=False):
    s = plan.summary()
    print(json.dumps(s, indent=2))
    for cid, issues in list(plan.quarantined.items())[:limit]:
        for i in issues:
            print(f"  QUARANTINED {cid}: [{i.code}] {i.message}")
    for cid, _, _ in plan.conflicts[:limit]:
        print(f"  CONFLICT {cid}")
    if show_warnings:
        for i in plan.issues:
            if i.severity != "error":
                print(f"  warning {i.control_id or '-'}: [{i.code}] {i.message}")


def cmd_import(a):
    repo = SqliteRepository(a.db, four_eyes=not a.solo)
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


def cmd_verify(a):
    repo = SqliteRepository(a.db, four_eyes=not a.solo)
    problems = verify_repository(repo, load_catalog(a.catalog, a.framework_code), load_curation(a.curation))
    for p in problems:
        print("DRIFT", p)
    print("repository verified" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_master(a):
    repo = SqliteRepository(a.db, four_eyes=not a.solo)
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
        rows = repo.conn.execute("SELECT * FROM master_control WHERE status='draft' AND quality_flags<>'[]' ORDER BY id").fetchall()
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
    n = suggest(SqliteRepository(a.db, four_eyes=not a.solo), a.framework_code, a.version, a.min_score, a.top_k)
    print(f"{n} new suggestion(s) queued for review")
    return 0


def cmd_review(a):
    repo = SqliteRepository(a.db, four_eyes=not a.solo)
    if a.action == "list":
        for r in repo.open_suggestions():
            print(f"#{r['id']:<5} {r['score']:.2f}  {r['control_id']:<10} -> {r['master_id']} {r['master']}  [{r['evidence']}]")
        return 0
    repo.decide_mapping(a.id, "approved" if a.action == "approve" else "rejected", a.reviewer or getpass.getuser(),
                        a.relationship, a.rationale, a.primary)
    print(a.action, a.id)
    return 0


def cmd_export(a):
    bundle = build_bundle(SqliteRepository(a.db, four_eyes=not a.solo), a.framework_code, a.version)
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
    repo = SqliteRepository(a.db, four_eyes=not a.solo)
    report = migrate_dry_or_apply(repo, parse_legacy(read_rows(a.workbook)), a.framework_code, a.version,
                                  a.actor or getpass.getuser(), a.apply)
    if a.report:
        with open(a.report, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1, ensure_ascii=False)
    brief = {k: (v if not isinstance(v, list) else len(v)) for k, v in report.items()}
    print(json.dumps(brief, indent=1))
    print("applied" if a.apply else "dry run: nothing written (use --apply)")
    return 0


def cmd_serve(a):
    from .review_ui import serve
    serve(a.db, a.framework_code, a.version, a.port, four_eyes=not a.solo)
    return 0


def cmd_report(a):
    repo = SqliteRepository(a.db, four_eyes=not a.solo)
    print(json.dumps(repo.coverage(a.framework_code, a.version), indent=2))
    for row in repo.conn.execute(
            "SELECT code, severity, status, COUNT(*) n FROM review_item GROUP BY 1,2,3 ORDER BY n DESC"):
        print(f"review_item {row['status']:<7} {row['severity']:<7} {row['code']:<28} {row['n']}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="airrp-ingest")
    p.add_argument("--db", default="airrp.db")
    p.add_argument("--actor")
    p.add_argument("--solo", action="store_true", help="allow the same person to create and approve a master control")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("import"); s.set_defaults(fn=cmd_import)
    s.add_argument("catalog"); s.add_argument("--framework-code", default="NIST-SP-800-53")
    s.add_argument("--manifest"); s.add_argument("--curation"); s.add_argument("--apply", action="store_true")
    s.add_argument("--strict", action="store_true"); s.add_argument("--show-warnings", action="store_true")

    s = sub.add_parser("verify"); s.set_defaults(fn=cmd_verify)
    s.add_argument("catalog"); s.add_argument("--framework-code", default="NIST-SP-800-53"); s.add_argument("--curation")

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

    s = sub.add_parser("serve"); s.set_defaults(fn=cmd_serve)
    s.add_argument("--framework-code", default="NIST-SP-800-53"); s.add_argument("--version", required=True)
    s.add_argument("--port", type=int, default=8765)

    s = sub.add_parser("report"); s.set_defaults(fn=cmd_report)
    s.add_argument("--framework-code", default="NIST-SP-800-53"); s.add_argument("--version", required=True)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
