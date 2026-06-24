"""JSearch (RapidAPI) client - primary source, covers Israel via Google for Jobs.

Docs: https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch
Returns postings aggregated from LinkedIn, Indeed, Glassdoor, company sites, etc.
"""
from __future__ import annotations

import requests

from .. import config
from .base import JobResult, JobSource

_HOST = "jsearch.p.rapidapi.com"
_BASE = "https://jsearch.p.rapidapi.com"
# Newer JSearch deployments expose "/search-v2" (response: data.jobs[]),
# older ones expose "/search" (response: data[]). We try v2 first and fall
# back automatically, so the client works regardless of the subscription.
_ENDPOINTS = ("/search-v2", "/search")

# Map a free-text location to an ISO country code for the API's `country` filter.
_COUNTRY = {
    "israel": "il", "tel aviv": "il", "haifa": "il", "jerusalem": "il",
    "united states": "us", "usa": "us", "uk": "gb", "united kingdom": "gb",
    "germany": "de", "remote": None,
}


class JSearchSource(JobSource):
    name = "jsearch"

    def is_configured(self) -> bool:
        return bool(config.RAPIDAPI_KEY)

    def _country_for(self, location: str) -> str | None:
        loc = (location or "").strip().lower()
        for key, code in _COUNTRY.items():
            if key in loc:
                return code
        return None

    def search(self, query: str, location: str = "Israel",
               limit: int = 20) -> list[JobResult]:
        if not self.is_configured():
            return []

        # JSearch packs location into the free-text query.
        q = f"{query} in {location}" if location else query
        num_pages = max(1, min(3, (limit + 9) // 10))
        params = {
            "query": q,
            "page": "1",
            "num_pages": str(num_pages),
        }
        country = self._country_for(location)
        if country:
            params["country"] = country
        headers = {
            "X-RapidAPI-Key": config.RAPIDAPI_KEY,
            "X-RapidAPI-Host": _HOST,
        }

        payload = None
        last_exc: Exception | None = None
        for ep in _ENDPOINTS:
            try:
                resp = requests.get(_BASE + ep, headers=headers,
                                    params=params, timeout=30)
            except Exception as exc:  # network/SSL issues
                last_exc = exc
                continue
            if resp.status_code == 404:
                # Endpoint not available on this subscription; try the next one.
                continue
            resp.raise_for_status()
            payload = resp.json()
            break
        if payload is None:
            if last_exc:
                raise last_exc
            return []

        # Normalise both response shapes into a flat list of job dicts.
        raw = payload.get("data")
        if isinstance(raw, dict):
            data = raw.get("jobs") or []
        elif isinstance(raw, list):
            data = raw
        else:
            data = []

        results: list[JobResult] = []
        for j in data[:limit]:
            city = j.get("job_city") or ""
            country = j.get("job_country") or ""
            loc = ", ".join(p for p in (city, country) if p)
            salary = ""
            if j.get("job_min_salary") or j.get("job_max_salary"):
                lo = j.get("job_min_salary") or "?"
                hi = j.get("job_max_salary") or "?"
                cur = j.get("job_salary_currency") or ""
                salary = f"{lo}-{hi} {cur}".strip()
            results.append(JobResult(
                source=self.name,
                title=j.get("job_title", ""),
                company=j.get("employer_name", ""),
                location=loc,
                url=j.get("job_apply_link") or j.get("job_google_link") or "",
                description=(j.get("job_description") or "")[:5000],
                salary=salary,
                posted=j.get("job_posted_at_datetime_utc") or "",
                external_id=j.get("job_id", ""),
                raw=j,
            ))
        return results
