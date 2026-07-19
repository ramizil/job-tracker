"""Remotive free remote-jobs API — no key required.

Docs: https://remotive.com/api/remote-jobs
Complements flaky web-search / blocked Jooble with a stable JSON feed of
remote roles (filter client-side for Israel / query keywords).
"""
from __future__ import annotations

import re

import requests

from .base import JobResult, JobSource

_URL = "https://remotive.com/api/remote-jobs"
_TIMEOUT = 20
_IL = re.compile(
    r"\b(israel|tel[\s-]?aviv|jerusalem|haifa|herzliy[ay]|remote[\s-]?israel)\b",
    re.I,
)


class RemotiveSource(JobSource):
    name = "remotive"

    def is_configured(self) -> bool:
        return True  # public API, no credentials

    def search(self, query: str, location: str = "Israel",
               limit: int = 20) -> list[JobResult]:
        q = (query or "").strip()
        params: dict[str, str | int] = {"limit": max(limit * 3, 40)}
        if q:
            # Remotive search is a single keyword string.
            params["search"] = q.split(" OR ")[0].strip().strip('"')[:80]
        try:
            resp = requests.get(_URL, params=params, timeout=_TIMEOUT)
            resp.raise_for_status()
            jobs = resp.json().get("jobs") or []
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(f"Remotive unreachable: {exc}") from exc

        want_il = "israel" in (location or "").lower()
        results: list[JobResult] = []
        for j in jobs:
            title = (j.get("title") or "").strip()
            company = (j.get("company_name") or "").strip()
            loc = (j.get("candidate_required_location") or "").strip()
            url = (j.get("url") or "").strip()
            if not title or not url:
                continue
            blob = f"{title} {company} {loc} {j.get('description') or ''}"
            if want_il and not (_IL.search(loc) or _IL.search(blob)
                                or loc.lower() in ("", "worldwide", "anywhere",
                                                   "remote", "global")):
                # Keep worldwide/remote; drop clearly other-country-only.
                if any(x in loc.lower() for x in (
                        "united states", "usa", "uk", "germany", "india",
                        "canada", "australia", "france", "europe only")):
                    continue
            desc = (j.get("description") or "")[:5000]
            results.append(JobResult(
                source="remotive",
                title=title,
                company=company or "(unknown)",
                location=loc or location,
                url=url,
                description=desc,
                salary=(j.get("salary") or "").strip(),
                posted=(j.get("publication_date") or "")[:10],
                external_id=str(j.get("id") or url),
                raw=dict(j),
            ))
            if len(results) >= limit:
                break
        return results
