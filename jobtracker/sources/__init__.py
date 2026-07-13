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

ALL_SOURCES: list[JobSource] = [JSearchSource(), JoobleSource(), AdzunaSource(),
                                WebSearchSource()]


def get_sources(only: str | None = None) -> list[JobSource]:
    """Return configured (credentialed) sources, optionally filtered by name."""
    sources = [s for s in ALL_SOURCES if s.is_configured()]
    if only:
        sources = [s for s in sources if s.name == only.lower()]
    return sources


__all__ = ["JobResult", "JobSource", "get_sources", "ALL_SOURCES"]
