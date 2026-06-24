"""Jooble client - free job-search API with Israel coverage.

Docs: https://jooble.org/api/about  (POST https://jooble.org/api/<key>)
"""
from __future__ import annotations

import requests

from .. import config, usage
from .base import JobResult, JobSource

_ENDPOINT = "https://jooble.org/api/"


class JoobleQuotaError(RuntimeError):
    """Raised when Jooble rejects the key (exhausted free tier or invalid)."""


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
        if resp.status_code in (401, 403):
            raise JoobleQuotaError(
                "Jooble rejected the API key (likely the 500-request free tier "
                "is used up, or the key is invalid). Get a fresh key at "
                "https://jooble.org/api/about and paste it in Settings."
            )
        resp.raise_for_status()
        # A successful call consumes one request from the free allowance.
        usage.record_jooble_request(config.JOOBLE_API_KEY)
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
