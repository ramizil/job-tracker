"""Client-side relevance filter for job search results.

Aggregator APIs (esp. Remotive) are loose with short queries like ``QA`` and
often return sales / unrelated roles. We require a real keyword hit — for
abbreviations, preferably in the title.
"""
from __future__ import annotations

import re

# Synonyms when the user searches a short QA / testing term.
_QA_QUERY = re.compile(
    r"^(qa|qe|qc|sdet|qae|automation|testing|tester)$", re.I
)
_QA_TITLE = re.compile(
    r"\b(qa|q\.?a\.?|qe|sdet|qae|quality\s*assurance|test(\s|/)?automation|"
    r"automation\s*(engineer|qa|tester|technician|developer|architect)|"
    r"software\s*test|manual\s*test|"
    r"qa\s*engineer|test\s*engineer|testing)\b",
    re.I,
)
_QA_DESC = re.compile(
    r"\b(qa\s*engineer|quality\s*assurance|test\s*automation|sdet|"
    r"automation\s*engineer|software\s*tester)\b",
    re.I,
)
_SALES_TITLE = re.compile(
    r"\b(sales|account\s*executive|sdr|bdr|closer|high[- ]ticket|"
    r"business\s*development|inside\s*sales|outbound\s*sales|"
    r"financial\s*sales|sales\s*specialist|sales\s*contractor)\b",
    re.I,
)


def _tokens(query: str) -> list[str]:
    q = (query or "").strip()
    if not q:
        return []
    if re.search(r"\s+OR\s+", q, re.I):
        return [p.strip().strip('"') for p in re.split(r"\s+OR\s+", q, flags=re.I)
                if p.strip()]
    return [q.strip().strip('"')]


def _term_matches(term: str, title: str, description: str) -> bool:
    term = (term or "").strip()
    if not term:
        return True
    title = title or ""
    desc = (description or "")[:4000]
    tlow = title.lower()

    # QA / testing family — title must look like a QA role (not "sales" with
    # a buried "qa" mention in the company blurb).
    if _QA_QUERY.match(term) or term.lower() in ("qa automation", "automation qa"):
        if _SALES_TITLE.search(title) and not _QA_TITLE.search(title):
            return False
        if _QA_TITLE.search(title):
            return True
        # Description-only: strong phrase only, and title not sales.
        if _QA_DESC.search(desc) and not _SALES_TITLE.search(title):
            return True
        return False

    # Short abbreviations: whole-word in title.
    if len(term) <= 3:
        return bool(re.search(rf"\b{re.escape(term)}\b", title, re.I))

    # Multi-word / normal: all words in title, or phrase in title/desc.
    if term.lower() in tlow:
        return True
    words = [w for w in re.split(r"\s+", term.lower()) if len(w) > 1]
    if words and all(w in tlow for w in words):
        return True
    # At least the full phrase somewhere in title+desc for longer queries.
    blob = f"{title}\n{desc}".lower()
    if term.lower() in blob:
        return True
    if words and sum(1 for w in words if re.search(rf"\b{re.escape(w)}\b", blob)) >= max(1, len(words) - 1):
        # Prefer title hit for at least one content word.
        return any(re.search(rf"\b{re.escape(w)}\b", tlow) for w in words if len(w) > 2)
    return False


def job_matches_query(query: str, *, title: str, description: str = "") -> bool:
    """True if this job is relevant enough to show for ``query``."""
    tokens = _tokens(query)
    if not tokens:
        return True
    # OR semantics: any token may match (blank search expands to title ORs).
    return any(_term_matches(t, title, description) for t in tokens)
