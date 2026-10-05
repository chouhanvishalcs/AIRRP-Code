"""Shared vocabulary of the governance model. The database enforces the same sets in CHECK constraints and triggers."""

# mapping: pair-level facts
ROLES = ("primary", "composite")
COVERAGES = ("exact", "broader", "partial")  # NISTIR 8477: equivalent / requirement-subset-of-control / intersects
MAPPING_STATUSES = ("suggested", "proposed", "approved", "rejected", "revalidation_required", "superseded")
# allowed status transitions (anything else is refused by the database)
MAPPING_TRANSITIONS = {
    "suggested": {"proposed", "rejected"},
    "proposed": {"approved", "rejected", "superseded"},
    "approved": {"revalidation_required", "superseded"},
    "revalidation_required": {"proposed", "rejected", "superseded"},
    "rejected": set(),
    "superseded": set(),
}

# requirement-level dispositions (separate from any mapping)
DISPOSITIONS = ("under_review", "needs_new_master", "validation_required", "no_master_required")

# master control revisions
REVISION_STATUSES = ("draft", "active", "superseded", "retired", "rejected")
CHANGE_CLASSES = ("initial", "editorial", "material", "retiring")
MASTER_STATUSES = ("draft", "active", "retired")

# quality flags: identity flags block "active for mapping"; assessment flags only block "assessment-ready"
IDENTITY_FLAGS = ("EMPTY_OBJECTIVE", "EMPTY_DESCRIPTION")
ASSESSMENT_FLAGS = ("PLACEHOLDER_EVIDENCE", "PLACEHOLDER_TEST_PROCEDURE", "UNCONFIRMED_FREQUENCY")

# review signals (advisory): thresholds
BROAD_MASTER_FAMILIES = 5
HIGH_FAN_IN = 40

INSTRUMENT_TYPES = ("Law", "Regulation", "Standard", "ControlFramework", "RegulatoryGuideline")
OBJECT_TYPES = ("base", "enhancement", "clause", "requirement", "obligation", "section", "article", "rule",
                "guideline", "safeguard", "assessment_procedure")  # base/enhancement = NIST Control/ControlEnhancement
