"""Master control creation: controlled vocabulary, explicit values (no defaults), duplicate guard."""
from __future__ import annotations

import json
import os

from .normalize import master_canonical_key, sha256_hex
from .similarity import TfIdfScorer

_VOCAB_PATH = os.path.join(os.path.dirname(__file__), "vocab.json")
NEAR_DUPLICATE = 0.80


class DuplicateSuspect(Exception):
    def __init__(self, candidates):
        self.candidates = candidates
        super().__init__("; ".join(f"{i} ({s:.2f})" for i, s, _ in candidates))


def load_vocab(path: str = _VOCAB_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _canon(value: str, allowed: list, aliases: dict, label: str) -> str:
    value = (value or "").strip()
    value = aliases.get(value, value)
    if value not in allowed:
        raise ValueError(f"{label} {value!r} is not in the controlled vocabulary {allowed}")
    return value


def master_id_for(name: str) -> str:
    """Deterministic: the same canonical name yields the same id on every machine."""
    return "AIRRP-CTRL-" + sha256_hex(master_canonical_key(name))[:12].upper()


def create_master(repo, actor: str, *, name: str, objective: str, description: str, domain: str, frequency: str,
                  control_type: str, evidence: str = "", test_procedure: str = "", master_id: str = "",
                  distinct_from_reason: str = "", vocab: dict = None) -> str:
    vocab = vocab or load_vocab()
    for label, val in (("name", name), ("objective", objective), ("description", description), ("evidence", evidence),
                       ("test_procedure", test_procedure)):
        if label in ("name", "objective", "description") and not (val or "").strip():
            raise ValueError(f"{label} is required")
    if not (evidence or "").strip() or not (test_procedure or "").strip():
        raise ValueError("evidence and test_procedure are required (no boilerplate defaults)")
    mc = {
        "id": master_id or master_id_for(name), "name": name.strip(), "objective": objective.strip(),
        "description": description.strip(), "evidence": evidence.strip(), "test_procedure": test_procedure.strip(),
        "domain": _canon(domain, vocab["domains"], vocab["domain_aliases"], "domain"),
        "frequency": _canon(frequency, vocab["frequencies"], vocab["frequency_aliases"], "frequency"),
        "control_type": _canon(control_type, vocab["control_types"], vocab["control_type_aliases"], "control_type"),
    }
    blocking = [f for f in quality_flags(mc, confirmed=("frequency",))]
    if blocking:
        raise ValueError(f"placeholder values are not allowed for new master controls: {blocking}")
    existing = repo.master_controls()
    if existing and not distinct_from_reason.strip():
        scorer = TfIdfScorer({m["id"]: f"{m['name']} {m['objective']}" for m in existing})
        near = [c for c in scorer.rank(f"{mc['name']} {mc['objective']}", 3, NEAR_DUPLICATE)]
        if near:
            raise DuplicateSuspect(near)
    repo.add_master_control(mc, actor)
    if distinct_from_reason.strip():
        repo.audit(actor, "master.distinct_reason", "master_control", mc["id"], {"reason": distinct_from_reason})
    return mc["id"]


# Values the legacy normaliser used as defaults. They are not evidence or test procedures.
PLACEHOLDER_EVIDENCE_PREFIXES = ("retain governed evidence demonstrating",)
PLACEHOLDER_TESTS = ("evidencereview", "")


def quality_flags(mc: dict, confirmed: tuple = ()) -> list:
    """Flags that block approval. ``confirmed`` names fields a reviewer explicitly confirmed."""
    flags = []
    ev = (mc.get("evidence") or "").strip().lower()
    if not ev or ev.startswith(PLACEHOLDER_EVIDENCE_PREFIXES):
        flags.append("PLACEHOLDER_EVIDENCE")
    if (mc.get("test_procedure") or "").strip().lower() in PLACEHOLDER_TESTS:
        flags.append("PLACEHOLDER_TEST_PROCEDURE")
    if mc.get("frequency") == "AsRequired" and "frequency" not in confirmed:
        flags.append("UNCONFIRMED_FREQUENCY")
    for key in ("objective", "description"):
        if not (mc.get(key) or "").strip():
            flags.append(f"EMPTY_{key.upper()}")
    return flags


def import_legacy_master(repo, actor: str, legacy: dict, vocab: dict = None) -> list:
    """Bring an existing master control in as a *draft* with its original id and any quality flags."""
    vocab = vocab or load_vocab()
    mc = {
        "id": legacy["id"], "name": legacy["name"].strip(), "objective": (legacy.get("objective") or "").strip(),
        "description": (legacy.get("description") or "").strip(), "evidence": (legacy.get("evidence") or "").strip(),
        "test_procedure": (legacy.get("test_procedure") or "").strip(),
        "domain": _canon(legacy.get("domain"), vocab["domains"], vocab["domain_aliases"], "domain"),
        "frequency": _canon(legacy.get("frequency"), vocab["frequencies"], vocab["frequency_aliases"], "frequency"),
        "control_type": _canon(legacy.get("control_type"), vocab["control_types"], vocab["control_type_aliases"], "control_type"),
    }
    flags = quality_flags(mc)
    repo.add_master_control(mc, actor, flags=flags, source="legacy-workbook")
    return flags


def update_master(repo, actor: str, master_id: str, vocab: dict = None, **fields) -> list:
    """Edit a draft. Submitting a flagged field with a real value clears its flag (AsRequired counts once confirmed)."""
    vocab = vocab or load_vocab()
    row = repo.fetchone("SELECT * FROM master_control WHERE id=?", (master_id,))
    if row is None:
        raise ValueError(f"unknown master control {master_id!r}")
    changes = {k: v.strip() for k, v in fields.items() if v is not None and str(v).strip() != ""}
    if "domain" in changes:
        changes["domain"] = _canon(changes["domain"], vocab["domains"], vocab["domain_aliases"], "domain")
    if "frequency" in changes:
        changes["frequency"] = _canon(changes["frequency"], vocab["frequencies"], vocab["frequency_aliases"], "frequency")
    if "control_type" in changes:
        changes["control_type"] = _canon(changes["control_type"], vocab["control_types"], vocab["control_type_aliases"], "control_type")
    merged = {**dict(row), **changes}
    flags = quality_flags(merged, confirmed=tuple(changes))
    repo.update_master_draft(master_id, changes, flags, actor)
    return flags
