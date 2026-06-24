"""Jooble client - free job-search API.

Docs: https://jooble.org/api/about  (POST https://<country>.jooble.org/api/<key>)

Jooble serves results per country-specific domain. The global ``jooble.org``
host is US-centric, so for local coverage we route to the matching country
host (e.g. ``il.jooble.org`` for Israel). The same API key is used across
hosts. Note: some country hosts may be unreachable behind restrictive
corporate proxies — that surfaces as a timeout, which the caller handles.
"""
from __future__ import annotations

import requests

from .. import config, usage
from .base import JobResult, JobSource

# Map a free-text location to the Jooble country sub-domain that indexes it.
# Anything not listed falls back to the global host.
_COUNTRY_HOST = {
    "israel": "il", "ישראל": "il", "tel aviv": "il", "תל אביב": "il",
    "haifa": "il", "jerusalem": "il", "ירושלים": "il", "herzliya": "il",
    "united kingdom": "uk", "london": "uk",
    "germany": "de", "berlin": "de",
}
_TIMEOUT = 25


def _host_for(location: str) -> str:
    loc = (location or "").strip().lower()
    for needle, code in _COUNTRY_HOST.items():
        if needle in loc:
            return f"{code}.jooble.org"
    return "jooble.org"


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

        host = _host_for(location)
        payload = {"keywords": query, "location": location}
        try:
            resp = requests.post(
                f"https://{host}/api/{config.JOOBLE_API_KEY}",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=_TIMEOUT,
            )
        except requests.exceptions.RequestException as exc:
            # Country hosts like il.jooble.org can be blocked by corporate
            # proxies; make the reason actionable rather than a raw traceback.
            raise RuntimeError(
                f"Couldn't reach {host} ({type(exc).__name__}). This Jooble "
                "country domain may be blocked on your network — try from a "
                "home/non-corporate connection."
            ) from exc
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
