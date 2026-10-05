"""Canonical in-memory model shared by every stage of the pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class Issue:
    """A finding raised by parsing/validation. Unwaived ERRORs quarantine the control."""

    severity: str
    code: str
    control_id: Optional[str]
    message: str

    def key(self) -> tuple:
        return (self.code, self.control_id)


@dataclass(frozen=True)
class Parameter:
    id: str
    label: Optional[str] = None
    choices: tuple = ()
    how_many: Optional[str] = None
    aggregates: tuple = ()


@dataclass(frozen=True)
class Link:
    rel: str
    target: str  # canonical control id, reference uuid, or raw href when unresolved
    kind: str  # "control" | "reference" | "part" | "unresolved"


@dataclass(frozen=True)
class Clause:
    """A stable, addressable part of a source object (e.g. OSCAL part id ``ac-2_smt.k``)."""

    ref: str
    parent_ref: Optional[str]
    label: str
    text: str
    is_leaf: bool


@dataclass(frozen=True)
class SourceControl:
    """One control or control enhancement exactly as published by the source framework."""

    framework_code: str
    framework_version: str
    control_id: str  # canonical, e.g. "AC-2(6)"
    oscal_id: str  # source id, e.g. "ac-2.6"
    parent_id: Optional[str]
    kind: str  # "base" | "enhancement"
    family: str  # "AC"
    title: str
    status: str  # "active" | "withdrawn"
    statement: str = ""
    guidance: str = ""
    parameters: tuple = ()
    assessment_objectives: str = ""
    assessment_methods: tuple = ()  # ((method, objects), ...)
    links: tuple = ()
    implementation_level: Optional[str] = None
    source_ref: str = ""
    content_hash: str = ""  # integrity: covers everything stored
    obligation_hash: str = ""  # mapping-relevant content only (title, statement, parameters): drives carry-forward
    clauses: tuple = ()
    overrides_applied: tuple = ()  # ((field, citation), ...)


@dataclass(frozen=True)
class Reference:
    uuid: str
    title: str
    citation: str = ""
    href: str = ""


@dataclass
class FrameworkInfo:
    code: str
    version: str
    title: str
    oscal_version: str
    last_modified: str
    source_sha256: str
    groups: int = 0


@dataclass
class ParsedCatalog:
    framework: FrameworkInfo
    controls: list = field(default_factory=list)
    references: list = field(default_factory=list)
    issues: list = field(default_factory=list)
