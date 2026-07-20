"""AllJobs.co.il guest search — HTML scrape via freetxt (no key)."""
from __future__ import annotations

import re

import requests
from bs4 import BeautifulSoup

from .base import JobResult, JobSource
from .relevance import job_matches_query

_SEARCH = "https://www.alljobs.co.il/SearchResultsGuest.aspx"
_BASE = "https://www.alljobs.co.il"
_TIMEOUT = 20
_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}
_BOX_ID = re.compile(r"^job-box(\d+)$")
_JOB_HREF = re.compile(r"UploadSingle\.aspx\?JobID=(\d+)", re.I)


def _search_term(query: str) -> str:
    q = (query or "").strip()
    if not q:
        return "QA"
    if re.search(r"\s+OR\s+", q, re.I):
        return q.split(" OR ")[0].strip().strip('"')[:80]
    return q.strip().strip('"')[:80]


def _parse_box(box) -> dict | None:
    mid = _BOX_ID.match(box.get("id") or "")
    if not mid:
        return None
    job_id = mid.group(1)
    title = ""
    href = ""
    for a in box.find_all("a", href=True):
        m = _JOB_HREF.search(a["href"])
        if not m:
            continue
        t = a.get_text(" ", strip=True)
        if t and len(t) > 2:
            title = t
            href = a["href"].strip()
            break
    if not title:
        return None
    url = href if href.startswith("http") else f"{_BASE}{href}"
    text = box.get_text("\n", strip=True)
    company = ""
    # Heuristic: company name often appears on its own line near the title.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # Skip boilerplate prefixes.
    skip = {
        "חברת השמה / כח אדם", "משרה בלעדית", "מיקום המשרה:", "סוג משרה:",
        "מספר מקומות", "משרה מלאה", "שליחה", "תיאור",
    }
    for i, ln in enumerate(lines):
        if ln == title or title in ln:
            # next non-meta line is often the company
            for nxt in lines[i + 1:i + 6]:
                if nxt in skip or nxt.endswith(":") or re.match(r"^\d+\s*ימים", nxt):
                    continue
                if len(nxt) < 40 and not nxt.startswith("לחברה"):
                    company = nxt
                    break
            break
    locs: list[str] = []
    if "מיקום המשרה:" in text:
        after = text.split("מיקום המשרה:", 1)[1]
        chunk = after.split("סוג משרה:", 1)[0]
        for ln in chunk.splitlines():
            ln = ln.strip()
            if ln and ln not in ("מספר מקומות",) and len(ln) < 40:
                locs.append(ln)
    # Description snippet: last longer paragraph-ish line.
    desc = ""
    for ln in reversed(lines):
        if len(ln) > 60 and "דיווח" not in ln and "תודה" not in ln:
            desc = ln
            break
    return {
        "job_id": job_id,
        "title": title,
        "url": url,
        "company": company or "AllJobs",
        "location": ", ".join(locs[:5]),
        "description": desc[:3000],
    }


class AllJobsSource(JobSource):
    name = "alljobs"

    def is_configured(self) -> bool:
        return True

    def search(self, query: str, location: str = "Israel",
               limit: int = 20) -> list[JobResult]:
        term = _search_term(query)
        results: list[JobResult] = []
        try:
            page = 1
            while len(results) < limit and page <= 3:
                resp = requests.get(
                    _SEARCH,
                    params={
                        "page": page,
                        "position": "",
                        "type": "",
                        "city": "",
                        "region": "",
                        "freetxt": term,
                    },
                    headers=_UA,
                    timeout=_TIMEOUT,
                )
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "html.parser")
                boxes = soup.find_all("div", id=_BOX_ID)
                if not boxes:
                    break
                for box in boxes:
                    parsed = _parse_box(box)
                    if not parsed:
                        continue
                    if not job_matches_query(
                            query, title=parsed["title"],
                            description=parsed["description"]):
                        continue
                    results.append(JobResult(
                        source="alljobs",
                        title=parsed["title"],
                        company=parsed["company"],
                        location=parsed["location"] or (location or "Israel"),
                        url=parsed["url"],
                        description=parsed["description"],
                        external_id=parsed["job_id"],
                        raw={"job_id": parsed["job_id"]},
                    ))
                    if len(results) >= limit:
                        break
                page += 1
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(f"AllJobs unreachable: {exc}") from exc
        return results
