"""Remotive free remote-jobs API — no key required.

Docs: https://remotive.com/api/remote-jobs
Complements flaky web-search / blocked Jooble with a stable JSON feed.
When location is Israel, only Israel-eligible postings are kept (not
Brazil/Mexico/US “remote” roles that Remotive also returns for a keyword).
"""
from __future__ import annotations

import re

import requests

from .base import JobResult, JobSource
from .relevance import job_matches_query

_URL = "https://remotive.com/api/remote-jobs"
_TIMEOUT = 20
_IL = re.compile(
    r"\b(israel|tel[\s-]?aviv|jerusalem|haifa|herzliy[ay]|רעננה|ישראל|"
    r"remote[\s-]?israel|israel[\s-]?remote)\b",
    re.I,
)
# Locations that clearly are not Israel (even if the job is "remote").
_NOT_IL = re.compile(
    r"\b(brazil|brasil|mexico|méxico|uruguay|argentina|chile|colombia|"
    r"united states|usa|u\.s\.|canada|uk|united kingdom|germany|france|"
    r"india|australia|spain|italy|netherlands|poland|portugal|ireland|"
    r"americas|latin america|latam|emea|apac|europe only|asia only|"
    r"africa|oceania)\b",
    re.I,
)


def _location_matches(loc: str, description: str, requested: str) -> bool:
    """True when this Remotive job fits the requested location filter."""
    req = (requested or "").strip().lower()
    if not req:
        return True
    loc = (loc or "").strip()
    blob = f"{loc} {description or ''}"

    if "israel" in req:
        # Must mention Israel (location field preferred); never keep LATAM/US/etc.
        if _NOT_IL.search(loc) and not _IL.search(loc):
            return False
        if _IL.search(loc):
            return True
        # Soft: Israel only in description, and location is empty / worldwide.
        loc_l = loc.lower()
        if loc_l in ("", "worldwide", "anywhere", "remote", "global", "world"):
            return bool(_IL.search(blob))
        return False

    # Generic: requested location string must appear in the job location.
    return req in loc.lower() or req in blob.lower()


class RemotiveSource(JobSource):
    name = "remotive"

    def is_configured(self) -> bool:
        return True  # public API, no credentials

    def search(self, query: str, location: str = "Israel",
               limit: int = 20) -> list[JobResult]:
        q = (query or "").strip()
        # Fetch a deeper pool — Israel-eligible remotes are a small fraction.
        params: dict[str, str | int] = {"limit": max(limit * 8, 100)}
        if q:
            params["search"] = q.split(" OR ")[0].strip().strip('"')[:80]
        try:
            resp = requests.get(_URL, params=params, timeout=_TIMEOUT)
            resp.raise_for_status()
            jobs = resp.json().get("jobs") or []
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(f"Remotive unreachable: {exc}") from exc

        results: list[JobResult] = []
        for j in jobs:
            title = (j.get("title") or "").strip()
            company = (j.get("company_name") or "").strip()
            loc = (j.get("candidate_required_location") or "").strip()
            url = (j.get("url") or "").strip()
            desc = (j.get("description") or "")[:5000]
            if not title or not url:
                continue
            if not _location_matches(loc, desc, location):
                continue
            if not job_matches_query(q, title=title, description=desc):
                continue
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
