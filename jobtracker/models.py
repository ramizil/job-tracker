"""Shared vocabularies: application statuses and rejection metadata."""
from __future__ import annotations

# Ordered pipeline statuses. Order matters for funnel analytics.
STATUSES: list[str] = [
    "saved",       # discovered / interesting, not applied yet
    "applied",     # application submitted
    "screening",   # recruiter / HR screen
    "interview",   # one or more interviews
    "offer",       # received an offer
    "accepted",    # accepted an offer
    "rejected",    # rejected by the company
    "withdrawn",   # you withdrew
    "ghosted",     # no response after a long time
]

# Statuses that represent a "live" (still-in-play) application.
ACTIVE_STATUSES = {"saved", "applied", "screening", "interview", "offer"}

# Statuses that count as a negative outcome (for analysis).
NEGATIVE_STATUSES = {"rejected", "withdrawn", "ghosted"}

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
