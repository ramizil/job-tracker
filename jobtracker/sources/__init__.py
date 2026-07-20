"""Job-board aggregator clients.

Each source implements `search(query, location, limit) -> list[JobResult]`.
`get_sources()` returns the clients that have credentials configured.
"""
from __future__ import annotations

from .base import JobResult, JobSource
from .jsearch import JSearchSource
from .jooble import JoobleSource
from .adzuna import AdzunaSource
from .websearch import WebSearchSource
from .remotive import RemotiveSource
from .drushim import DrushimSource
from .sqlink import SQLinkSource
from .alljobs import AllJobsSource
from .relevance import job_matches_query

ALL_SOURCES: list[JobSource] = [
    JSearchSource(), JoobleSource(), AdzunaSource(),
    WebSearchSource(), RemotiveSource(),
    DrushimSource(), SQLinkSource(), AllJobsSource(),
]

def get_sources(only: str | None = None) -> list[JobSource]:
    """Return configured (credentialed) sources, optionally filtered by name.

    Sources listed in SOURCES_DISABLED are skipped even when credentialed
    (e.g. Jooble blocked on a corporate network) — toggleable in Settings.
    """
    from .. import config
    disabled = {s.strip() for s in config.SOURCES_DISABLED.split(",") if s.strip()}
    sources = [s for s in ALL_SOURCES
               if s.is_configured() and s.name not in disabled]
    if only:
        sources = [s for s in sources if s.name == only.lower()]
    return sources


__all__ = ["JobResult", "JobSource", "get_sources", "ALL_SOURCES",
           "job_matches_query"]
