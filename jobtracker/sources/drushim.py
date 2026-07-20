"""Drushim.co.il job search — uses their public JSON search API (no key)."""
from __future__ import annotations

import re

import requests

from .base import JobResult, JobSource
from .relevance import job_matches_query

_API = "https://www.drushim.co.il/api/jobs/search"
_BASE = "https://www.drushim.co.il"
_TIMEOUT = 20
_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.drushim.co.il/jobs/search/",
}
_TAG = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    cleaned = _TAG.sub(" ", text or "").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", cleaned).strip()


def _search_term(query: str) -> str:
    q = (query or "").strip()
    if not q:
        return "QA"
    if re.search(r"\s+OR\s+", q, re.I):
        return q.split(" OR ")[0].strip().strip('"')[:80]
    return q.strip().strip('"')[:80]


def _location_ok(regions: list[str], requested: str) -> bool:
    req = (requested or "").strip().lower()
    if not req or "israel" in req or "ישראל" in req:
        return True  # Drushim is Israel-only
    blob = " ".join(regions).lower()
    return req in blob


class DrushimSource(JobSource):
    name = "drushim"

    def is_configured(self) -> bool:
        return True

    def search(self, query: str, location: str = "Israel",
               limit: int = 20) -> list[JobResult]:
        term = _search_term(query)
        results: list[JobResult] = []
        page = 1
        try:
            while len(results) < limit and page <= 5:
                resp = requests.get(
                    _API,
                    params={
                        "searchterm": term,
                        "isaa": "true",
                        "ssaen": 3,
                        "range": 3,
                        "page": page,
                    },
                    headers=_UA,
                    timeout=_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
                jobs = data.get("ResultList") or []
                if not jobs:
                    break
                for j in jobs:
                    content = j.get("JobContent") or {}
                    company = j.get("Company") or {}
                    info = j.get("JobInfo") or {}
                    title = (content.get("Name") or "").strip()
                    link = (info.get("Link") or "").strip()
                    if not title or not link:
                        continue
                    url = link if link.startswith("http") else f"{_BASE}{link}"
                    regions = [
                        (r.get("NameInHebrew") or "").strip()
                        for r in (content.get("Regions") or [])
                        if isinstance(r, dict)
                    ]
                    loc = ", ".join(r for r in regions if r)
                    desc = _strip_html(
                        f"{content.get('Description') or ''}\n"
                        f"{content.get('Requirements') or ''}"
                    )[:5000]
                    if not _location_ok(regions, location):
                        continue
                    if not job_matches_query(query, title=title, description=desc):
                        continue
                    code = j.get("Code") or content.get("JobCode") or url
                    results.append(JobResult(
                        source="drushim",
                        title=title,
                        company=(company.get("CompanyDisplayName")
                                 or company.get("NameInHebrew")
                                 or "(unknown)").strip(),
                        location=loc or "Israel",
                        url=url,
                        description=desc,
                        salary=(content.get("SalaryRangeText") or "") or "",
                        posted="",
                        external_id=str(code),
                        raw={"code": code},
                    ))
                    if len(results) >= limit:
                        break
                total_pages = int(data.get("TotalPagesNumber") or 1)
                if page >= total_pages:
                    break
                page += 1
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(f"Drushim unreachable: {exc}") from exc
        except (ValueError, TypeError, KeyError) as exc:
            raise RuntimeError(f"Drushim returned unexpected data: {exc}") from exc
        return results
