"""Adzuna client - OPTIONAL (no Israel coverage; good for remote / UK / US / EU).

Docs: https://developer.adzuna.com/
"""
from __future__ import annotations

import requests

from .. import config
from .base import JobResult, JobSource

# Adzuna supported countries (no IL). Default to GB; override via location like
# "remote:us" or just pass a 2-letter country in `location`.
_SUPPORTED = {
    "gb", "us", "at", "au", "be", "br", "ca", "de", "es",
    "fr", "in", "it", "mx", "nl", "nz", "pl", "sg", "za",
}


class AdzunaSource(JobSource):
    name = "adzuna"

    def is_configured(self) -> bool:
        return bool(config.ADZUNA_APP_ID and config.ADZUNA_APP_KEY)

    def search(self, query: str, location: str = "gb",
               limit: int = 20) -> list[JobResult]:
        if not self.is_configured():
            return []

        country = (location or "gb").lower().strip()
        if country not in _SUPPORTED:
            country = "gb"  # Adzuna has no Israel; fall back.

        url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/1"
        params = {
            "app_id": config.ADZUNA_APP_ID,
            "app_key": config.ADZUNA_APP_KEY,
            "what": query,
            "results_per_page": min(50, max(1, limit)),
            "content-type": "application/json",
        }
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        rows = resp.json().get("results") or []

        results: list[JobResult] = []
        for j in rows[:limit]:
            salary = ""
            if j.get("salary_min") or j.get("salary_max"):
                salary = f"{j.get('salary_min', '?')}-{j.get('salary_max', '?')}"
            results.append(JobResult(
                source=self.name,
                title=j.get("title", ""),
                company=(j.get("company") or {}).get("display_name", ""),
                location=(j.get("location") or {}).get("display_name", ""),
                url=j.get("redirect_url", ""),
                description=(j.get("description") or "")[:5000],
                salary=salary,
                posted=j.get("created", ""),
                external_id=str(j.get("id", "")),
                raw=j,
            ))
        return results
