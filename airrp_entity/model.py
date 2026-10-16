"""Data model: every value is a candidate with evidence; nothing is reported without a source."""
from __future__ import annotations

from dataclasses import dataclass, field

# (key, label, multi-valued)
FIELDS = (
    ("registered_legal_name", "Registered legal name", False),
    ("corporate_registration_reference", "Corporate registration reference", True),
    ("legal_entity_type", "Legal entity type", False),
    ("registration_jurisdiction", "Registration jurisdiction", False),
    ("registered_office", "Registered office", False),
    ("operating_location", "Operating location", True),
    ("entity_operating_status", "Entity operating status", False),
    ("government_public_sector_status", "Government or public-sector status", False),
    ("industry_sector", "Enterprise industry / sector", True),
    ("markets_served", "Market or country served", True),
    ("regulated_activity", "Regulated activity", True),
    ("regulator_or_licence", "Regulator or licence", True),
)
FIELD_KEYS = tuple(k for k, _, _ in FIELDS)

# weight -> confidence label
HIGH, MEDIUM = 0.8, 0.55


def confidence(score: float) -> str:
    return "high" if score >= HIGH else "medium" if score >= MEDIUM else "low"


@dataclass
class Evidence:
    source: str          # "website" | "registry" | "derived"
    url: str
    method: str          # how the value was obtained, e.g. "labelled field 'Corporate Name'"
    snippet: str = ""

    def as_dict(self) -> dict:
        return {"source": self.source, "url": self.url, "method": self.method, "snippet": self.snippet}


@dataclass
class Candidate:
    field: str
    value: str                       # what a person reads
    key: str                         # what equal values share (normalised); merging is by key
    weight: float                    # strength of this single piece of evidence, 0..1
    evidence: Evidence
    aliases: list[str] = field(default_factory=list)    # keys that denote the same thing (e.g. English name of a JP entity)
    contains: list[str] = field(default_factory=list)   # a candidate whose key contains all of these is the same thing
    details: list[str] = field(default_factory=list)    # extra facts carried with the value (licence numbers, listings...)
    note: str = ""

    def __post_init__(self):
        if self.field not in FIELD_KEYS:
            raise ValueError(f"unknown field {self.field}")
        self.weight = max(0.0, min(0.99, self.weight))
