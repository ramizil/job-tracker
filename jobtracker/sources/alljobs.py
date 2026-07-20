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
_MORE_ABOUT = re.compile(
    r"(?:לעוד משרות ומידע על|more jobs.*?(?:at|about))\s*(.+?)\s*>?\s*$",
    re.I,
)


def _search_term(query: str) -> str:
    q = (query or "").strip()
    if not q:
        return "QA"
    if re.search(r"\s+OR\s+", q, re.I):
        return q.split(" OR ")[0].strip().strip('"')[:80]
    return q.strip().strip('"')[:80]


def _text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


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

    # Prefer structured AllJobs markup over free-text heuristics.
    company = ""
    desc_el = box.select_one(
        ".job-content-top-desc, .job-content-top-desc-ltr"
    )
    if desc_el:
        m = _MORE_ABOUT.search(_text(desc_el))
        if m:
            company = m.group(1).strip(" >\u200f\u200e")

    if not company:
        # Title block often ends with "Title CompanyName".
        title_el = box.select_one(
            ".job-content-top-title, .job-content-top-title-ltr"
        )
        block = _text(title_el)
        if block.startswith(title) and len(block) > len(title) + 1:
            company = block[len(title):].strip()

    loc_el = box.select_one(
        ".job-content-top-location, .job-content-top-location-ltr"
    )
    loc_raw = _text(loc_el)
    for prefix in ("מיקום המשרה:", "Location:", "מיקום המשרה"):
        if loc_raw.lower().startswith(prefix.lower()):
            loc_raw = loc_raw[len(prefix):].strip()
            break
    loc_raw = re.sub(r"^מספר מקומות\s*", "", loc_raw).strip()
    location = re.sub(r"\s+", " ", loc_raw)

    # Never treat a city/location line as the company.
    if company and location and (
            company == location
            or company in location.split()
            or location.startswith(company)):
        # Ambiguous — keep company only if desc-el gave it.
        if not desc_el:
            company = ""

    desc = ""
    for sel in (".job-content-top-details", ".job-content", ".job-box"):
        el = box.select_one(sel) if sel != ".job-box" else box
        if not el:
            continue
        blob = el.get_text("\n", strip=True)
        for ln in blob.splitlines():
            ln = ln.strip()
            if len(ln) > 80 and "דיווח" not in ln and "תודה" not in ln:
                desc = ln
                break
        if desc:
            break

    return {
        "job_id": job_id,
        "title": title,
        "url": url,
        "company": company or "AllJobs",
        "location": location,
        "description": (desc or "")[:3000],
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
