"""Bring the existing workbook's master controls and mappings into the repository as *drafts and suggestions*.

Nothing from the legacy normaliser is trusted: masters arrive as drafts (with quality flags for placeholder values),
mappings arrive as ``suggested`` with their legacy decision recorded as evidence, and a person approves each one.
The workbook has no header row; columns are addressed by position (verified against the shipped example).
"""
from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict

from .masters import import_legacy_master

COL = dict(seq=0, code=1, name=3, decision=11, master_id=12, master_name=13, objective=14, description=15,
           domain=16, control_type=17, frequency=18, evidence=19, test_procedure=20, rationale=10, map_status=9)
MIN_COLUMNS = 21
_SCORE = re.compile(r"(?:scored|at score)\s+(\d\.\d+)")


def read_rows(path: str) -> list:
    """.xlsx (needs openpyxl) or .tsv/.csv -> list of row lists (strings), headerless."""
    if path.lower().endswith((".xlsx", ".xlsm")):
        try:
            import openpyxl
        except ImportError as exc:  # keep the core dependency-free
            raise SystemExit("reading .xlsx needs openpyxl: pip install openpyxl (or export the sheet as .tsv)") from exc
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        rows = [["" if v is None else str(v) for v in r] for r in wb.worksheets[0].iter_rows(values_only=True)]
    else:
        with open(path, encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh, delimiter="\t" if path.lower().endswith(".tsv") else ","))
    return [r for r in rows if any(c.strip() for c in r)]


def _clean(v: str) -> str:
    return (v or "").replace("_x000D_", "").strip()


def parse_legacy(rows: list) -> list:
    out = []
    for n, r in enumerate(rows, 1):
        if len(r) < MIN_COLUMNS or not r[COL["seq"]].strip().isdigit():
            raise ValueError(f"row {n}: does not look like the legacy export (expected {MIN_COLUMNS}+ columns, "
                             "first column = row number, no header)")
        name = _clean(r[COL["name"]])
        m = _SCORE.search(r[COL["rationale"]])
        out.append({
            "control_id": name.split(" — ", 1)[0].strip(), "decision": _clean(r[COL["decision"]]),
            "master_id": _clean(r[COL["master_id"]]), "master_name": _clean(r[COL["master_name"]]),
            "objective": _clean(r[COL["objective"]]), "description": _clean(r[COL["description"]]),
            "domain": _clean(r[COL["domain"]]), "control_type": _clean(r[COL["control_type"]]),
            "frequency": _clean(r[COL["frequency"]]), "evidence": _clean(r[COL["evidence"]]),
            "test_procedure": _clean(r[COL["test_procedure"]]), "score": float(m.group(1)) if m else None})
    return out


def migrate(repo, legacy: list, framework_code: str, version: str, actor: str, apply: bool = False) -> dict:
    masters, mappings, unmapped = {}, [], []
    for row in legacy:
        if not row["master_id"]:
            unmapped.append(row["control_id"])
            continue
        prev = masters.setdefault(row["master_id"], row)
        if (prev["master_name"], prev["objective"], prev["domain"]) != (row["master_name"], row["objective"], row["domain"]):
            raise ValueError(f"legacy master {row['master_id']} has inconsistent attributes across rows")
        mappings.append(row)

    flag_counts, imported, errors = Counter(), 0, []
    families = defaultdict(set)
    for r in mappings:
        families[r["master_id"]].add(r["control_id"].split("-")[0])
    report = {"legacy_rows": len(legacy), "legacy_masters": len(masters), "legacy_unmapped_requirements": sorted(set(unmapped)),
              "broad_masters_over_5_families": sorted(m for m, f in families.items() if len(f) > 5)}

    with repo.transaction():
        for mid, row in masters.items():
            legacy_master = {"id": mid, "name": row["master_name"], **{k: row[k] for k in (
                "objective", "description", "domain", "frequency", "control_type", "evidence", "test_procedure")}}
            existing = repo.fetchone("SELECT 1 FROM master_control WHERE id=?", (mid,))
            if existing:
                continue
            try:
                flags = import_legacy_master(repo, actor, legacy_master)
            except Exception as exc:  # report, never guess
                errors.append(f"{mid}: {exc}")
                continue
            imported += 1
            flag_counts.update(flags)
        suggested, skipped, dupes = 0, [], 0
        for r in mappings:
            req = repo.fetchone(
                "SELECT id FROM source_requirement WHERE framework_code=? AND framework_version=? AND control_id=?"
                " AND status='active'", (framework_code, version, r["control_id"]))
            has_master = repo.fetchone("SELECT 1 FROM master_control WHERE id=?", (r["master_id"],))
            if req is None or has_master is None:
                skipped.append(f"{r['control_id']} -> {r['master_id']}: " + ("requirement not loaded" if req is None else "master not imported"))
                continue
            if repo.suggest_mapping(req["id"], r["master_id"], r["score"], f"legacy workbook: {r['decision']}"):
                suggested += 1
            else:
                dupes += 1
        report.update(masters_imported=imported, master_flags=dict(flag_counts), mappings_suggested=suggested,
                      mappings_skipped=skipped, duplicate_pairs_ignored=dupes, errors=errors)
        if not apply:
            raise _DryRun(report)
    return report


class _DryRun(Exception):
    def __init__(self, report):
        self.report = report


def migrate_dry_or_apply(repo, legacy, framework_code, version, actor, apply=False) -> dict:
    try:
        return migrate(repo, legacy, framework_code, version, actor, apply)
    except _DryRun as dry:  # rolled back by the transaction context manager
        return dry.report
