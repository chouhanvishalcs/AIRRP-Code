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
    overrides_applied: tuple = ()  # ((field, citation), ...): set only on an *effective* control, never on a stored mirror row
    corrections: tuple = ()  # the Correction records behind overrides_applied (effective controls only)


@dataclass(frozen=True)
class Correction:
    """A person's correction of one text field of one source control.

    It is written against a specific published value (``source_value_sha256``) and never edits that value: the source
    stays verbatim, the correction sits over it, and the pair is what the rest of the system reads as the control.
    """

    framework_code: str
    framework_version: str
    control_id: str
    field: str  # "title" | "statement" | "guidance"
    value: str  # the corrected text
    source_value_sha256: str  # hash of the published value this was written against
    problem: str  # what is wrong with the published value (for a retirement: why the correction is withdrawn)
    citation: str  # where the right wording comes from
    reviewer: str  # the person who decided
    reviewed_at: str
    revision: int = 1  # assigned when the correction is recorded
    action: str = "set"  # "set" = this value is in force; "retire" = withdrawn, the published value is in force again


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
