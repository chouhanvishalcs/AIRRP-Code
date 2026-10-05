"""Candidate generation only. Scores never create, approve or reject anything - a person does."""
from __future__ import annotations

import math
import re
from collections import Counter

from .normalize import normalize_text

_STOP = frozenset("""a an and are as at be by for from has have in is it its of on or that the their this to with
within which who will shall such than these those into any all each other using use used""".split())


def tokens(text: str) -> list:
    out = []
    for w in re.findall(r"[a-z0-9]+", normalize_text(text).lower()):
        if len(w) < 3 or w in _STOP:
            continue
        if len(w) > 4 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


class TfIdfScorer:
    """Pluggable: anything with .rank(text) -> [(doc_id, score, evidence)] can replace this (e.g. embeddings)."""

    def __init__(self, docs: dict):
        self.docs = {k: Counter(tokens(v)) for k, v in docs.items()}
        n = max(len(self.docs), 1)
        df = Counter(t for c in self.docs.values() for t in c)
        self.idf = {t: math.log((1 + n) / (1 + d)) + 1 for t, d in df.items()}
        self.vec = {k: self._vec(c) for k, c in self.docs.items()}

    def _vec(self, counts: Counter) -> dict:
        v = {t: (1 + math.log(c)) * self.idf.get(t, 1.0) for t, c in counts.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / norm for t, x in v.items()}

    def rank(self, text: str, top_k: int = 3, min_score: float = 0.0) -> list:
        q = self._vec(Counter(tokens(text)))
        scored = []
        for doc_id, d in self.vec.items():
            shared = {t: q[t] * d[t] for t in q.keys() & d.keys()}
            score = sum(shared.values())
            if score >= min_score and shared:
                top = sorted(shared, key=shared.get, reverse=True)[:6]
                scored.append((doc_id, round(score, 4), ", ".join(top)))
        scored.sort(key=lambda r: (-r[1], r[0]))
        return scored[:top_k]


def suggest(repo, framework_code: str, version: str, min_score: float = 0.25, top_k: int = 3,
            scorer_factory=TfIdfScorer) -> int:
    """Queue mapping suggestions for active requirements that have no approved mapping yet."""
    masters = repo.master_controls("approved")
    if not masters:
        return 0
    scorer = scorer_factory({m["id"]: f"{m['name']} {m['objective']} {m['description']}" for m in masters})
    created = 0
    with repo.transaction():
        for r in repo.requirements(framework_code, version, "active"):
            already = repo.conn.execute(
                "SELECT 1 FROM mapping WHERE requirement_id=? AND status='approved'", (r["id"],)).fetchone()
            if already:
                continue
            for master_id, score, evidence in scorer.rank(f"{r['title']} {r['statement']}", top_k, min_score):
                created += repo.suggest_mapping(r["id"], master_id, score, evidence)
    return created
