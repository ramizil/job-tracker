"""JSearch (RapidAPI) client - primary source, covers Israel via Google for Jobs.

Docs: https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch
Returns postings aggregated from LinkedIn, Indeed, Glassdoor, company sites, etc.
"""
from __future__ import annotations

import requests

from .. import config
from .base import JobResult, JobSource

_ENDPOINT = "https://jsearch.p.rapidapi.com/search"
_HOST = "jsearch.p.rapidapi.com"


class JSearchSource(JobSource):
    name = "jsearch"

    def is_configured(self) -> bool:
        return bool(config.RAPIDAPI_KEY)

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
        headers = {
            "X-RapidAPI-Key": config.RAPIDAPI_KEY,
            "X-RapidAPI-Host": _HOST,
        }
        resp = requests.get(_ENDPOINT, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json().get("data") or []

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
