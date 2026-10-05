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
