"""Common types for job sources."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class JobResult:
    source: str
    title: str
    company: str
    location: str = ""
    url: str = ""
    description: str = ""
    salary: str = ""
    posted: str = ""
    external_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class JobSource:
    """Base class for a job-board client."""

    name: str = "base"

    def is_configured(self) -> bool:  # pragma: no cover - trivial
        return False

    def search(self, query: str, location: str = "Israel",
               limit: int = 20) -> list[JobResult]:  # pragma: no cover
        raise NotImplementedError
