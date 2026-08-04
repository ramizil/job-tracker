"""Shared vocabularies: application statuses and rejection metadata."""
from __future__ import annotations

# Ordered pipeline statuses. Order matters for funnel analytics.
STATUSES: list[str] = [
    "saved",       # discovered / interesting, not applied yet
    "applied",     # application submitted
    "reapplied",   # applied again (e.g. after ghosted / role reposted)
    "screening",   # recruiter / HR screen
    "interview",   # one or more interviews
    "offer",       # received an offer
    "accepted",    # accepted an offer
    "rejected",    # rejected by the company
    "closed",      # posting closed / no longer accepting applications
    "withdrawn",   # you withdrew
    "ghosted",     # no response after a long time
]

# Short UI labels (fallback = the status key itself).
STATUS_LABELS: dict[str, str] = {
    "saved": "saved",
    "applied": "applied",
    "reapplied": "reapplied",
    "screening": "screening",
    "interview": "interview",
    "offer": "offer",
    "accepted": "accepted",
    "rejected": "rejected",
    "closed": "closed — no longer hiring",
    "withdrawn": "withdrawn",
    "ghosted": "ghosted",
}

# Statuses that represent a "live" (still-in-play) application.
ACTIVE_STATUSES = {"saved", "applied", "reapplied", "screening", "interview", "offer"}

# Statuses that count as a negative / ended outcome (for analysis).
NEGATIVE_STATUSES = {"rejected", "withdrawn", "ghosted", "closed"}

# The stage at which a rejection happened (for "why was I rejected" analysis).
REJECTION_STAGES: list[str] = [
    "no_response",       # never heard back
    "cv_screen",         # rejected on CV / ATS
    "recruiter_screen",  # rejected after recruiter call
    "hr_interview",
    "technical_test",    # home assignment / coding test
    "technical_interview",
    "manager_interview",
    "final_interview",
    "offer_declined_by_company",
    "other",
]

# Early = never reached a human process (email / ATS / silence).
EARLY_REJECTION_STAGES = frozenset({"no_response", "cv_screen"})

REJECTION_KIND_LABELS: dict[str, str] = {
    "early": "Early — email / ATS",
    "process": "After process",
    "unknown": "Rejection (stage unset)",
}

# Common, codifiable rejection reasons - free text is also allowed.
COMMON_REJECTION_REASONS: list[str] = [
    "overqualified",
    "underqualified",
    "salary_mismatch",
    "missing_skill",
    "experience_gap",
    "culture_fit",
    "role_filled_internally",
    "position_closed",
    "location_visa",
    "better_candidate",
    "no_feedback",
    "other",
]


def normalize_status(value: str) -> str:
    v = (value or "").strip().lower()
    if v not in STATUSES:
        raise ValueError(
            f"Unknown status '{value}'. Valid: {', '.join(STATUSES)}"
        )
    return v


def rejection_kind(stage: str | None) -> str:
    """Bucket a rejection stage: early | process | unknown."""
    s = (stage or "").strip().lower()
    if not s:
        return "unknown"
    if s in EARLY_REJECTION_STAGES:
        return "early"
    if s in REJECTION_STAGES:
        return "process"
    return "unknown"


def rejection_kind_label(stage: str | None) -> str:
    return REJECTION_KIND_LABELS.get(rejection_kind(stage), REJECTION_KIND_LABELS["unknown"])
