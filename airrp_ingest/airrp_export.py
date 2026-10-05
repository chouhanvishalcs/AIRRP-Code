"""Render SourceControl records in the shape the existing AIRRP requirement payload already uses.

The field layout and naming below were reverse-engineered from the ``code/name/legalText/...`` JSON in the
current workbook and are proven byte-identical for all 1,014 active NIST SP 800-53 r5.2.0 controls
(see tests/test_export_bundle.py). Other frameworks get their own profile.
"""
from __future__ import annotations

from dataclasses import dataclass

NIST_PDF = "https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-53r5.pdf"


@dataclass(frozen=True)
class Profile:
    code_prefix: str  # requirement code = f"{code_prefix}-REQ-{id with ( ) -> -}"
    regulation_code: str
    source_url: str  # sourceReference = f"{source_url}#control={oscal_id}"
    separator: str = " — "  # name = f"{control_id}{separator}{title}"
    obligation_type: str = "SourceRequirement"


NIST_800_53 = Profile(
    code_prefix="NIST-SP-800-53-SECURITY-AND-PRIVACY-CONTROLS",
    regulation_code="NIST-SP-800-53-SECURITY-AND-PRIVACY-CONTROLS-FRAMEWORK",
    source_url=NIST_PDF,
)


def requirement_code(profile: Profile, control_id: str) -> str:
    return f"{profile.code_prefix}-REQ-{control_id.replace('(', '-').replace(')', '')}"


def airrp_requirement(c, profile: Profile = NIST_800_53) -> dict:
    name = f"{c.control_id}{profile.separator}{c.title}"
    return {
        "code": requirement_code(profile, c.control_id),
        "name": name,
        "frequency": "",
        "legalText": c.statement,
        "legalTitle": name,
        "description": c.statement,
        "ownerFunction": "",
        "obligationType": profile.obligation_type,
        "regulationCode": profile.regulation_code,
        "sourceReference": f"{profile.source_url}#control={c.oscal_id}",
    }
