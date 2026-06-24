"""Jooble client - free job-search API with Israel coverage.

Docs: https://jooble.org/api/about  (POST https://jooble.org/api/<key>)
"""
from __future__ import annotations

import requests

from .. import config
from .base import JobResult, JobSource

_ENDPOINT = "https://jooble.org/api/"


class JoobleSource(JobSource):
    name = "jooble"

    def is_configured(self) -> bool:
        return bool(config.JOOBLE_API_KEY)

    def search(self, query: str, location: str = "Israel",
               limit: int = 20) -> list[JobResult]:
        if not self.is_configured():
            return []

        payload = {"keywords": query, "location": location}
        resp = requests.post(
            _ENDPOINT + config.JOOBLE_API_KEY,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        jobs = resp.json().get("jobs") or []

        results: list[JobResult] = []
        for j in jobs[:limit]:
            results.append(JobResult(
                source=self.name,
                title=j.get("title", ""),
                company=j.get("company", ""),
                location=j.get("location", ""),
                url=j.get("link", ""),
                description=(j.get("snippet") or "")[:5000],
                salary=j.get("salary", "") or "",
                posted=j.get("updated", "") or "",
                external_id=str(j.get("id", "")),
                raw=j,
            ))
        return results
