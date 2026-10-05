"""airrp-ingest command line."""
from __future__ import annotations

import argparse
import getpass
import json
import sys

from .curation import load_curation
from .masters import DuplicateSuspect, create_master
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
    repo = SqliteRepository(a.db)
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
    repo = SqliteRepository(a.db)
    problems = verify_repository(repo, load_catalog(a.catalog, a.framework_code), load_curation(a.curation))
    for p in problems:
        print("DRIFT", p)
    print("repository verified" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_master(a):
    repo = SqliteRepository(a.db)
    actor = a.actor or getpass.getuser()
    if a.action == "approve":
        repo.approve_master_control(a.id, actor)
        print("approved", a.id)
    elif a.action == "list":
        for m in repo.master_controls():
            print(m["id"], m["status"], m["domain"], m["name"])
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
    n = suggest(SqliteRepository(a.db), a.framework_code, a.version, a.min_score, a.top_k)
    print(f"{n} new suggestion(s) queued for review")
    return 0


def cmd_review(a):
    repo = SqliteRepository(a.db)
    if a.action == "list":
        for r in repo.open_suggestions():
            print(f"#{r['id']:<5} {r['score']:.2f}  {r['control_id']:<10} -> {r['master_id']} {r['master']}  [{r['evidence']}]")
        return 0
    repo.decide_mapping(a.id, "approved" if a.action == "approve" else "rejected", a.reviewer or getpass.getuser(),
                        a.relationship, a.rationale, a.primary)
    print(a.action, a.id)
    return 0


def cmd_report(a):
    repo = SqliteRepository(a.db)
    print(json.dumps(repo.coverage(a.framework_code, a.version), indent=2))
    for row in repo.conn.execute(
            "SELECT code, severity, status, COUNT(*) n FROM review_item GROUP BY 1,2,3 ORDER BY n DESC"):
        print(f"review_item {row['status']:<7} {row['severity']:<7} {row['code']:<28} {row['n']}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="airrp-ingest")
    p.add_argument("--db", default="airrp.db")
    p.add_argument("--actor")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("import"); s.set_defaults(fn=cmd_import)
    s.add_argument("catalog"); s.add_argument("--framework-code", default="NIST-SP-800-53")
    s.add_argument("--manifest"); s.add_argument("--curation"); s.add_argument("--apply", action="store_true")
    s.add_argument("--strict", action="store_true"); s.add_argument("--show-warnings", action="store_true")

    s = sub.add_parser("verify"); s.set_defaults(fn=cmd_verify)
    s.add_argument("catalog"); s.add_argument("--framework-code", default="NIST-SP-800-53"); s.add_argument("--curation")

    s = sub.add_parser("master"); s.set_defaults(fn=cmd_master)
    s.add_argument("action", choices=["add", "approve", "list"]); s.add_argument("--id")
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

    s = sub.add_parser("report"); s.set_defaults(fn=cmd_report)
    s.add_argument("--framework-code", default="NIST-SP-800-53"); s.add_argument("--version", required=True)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
