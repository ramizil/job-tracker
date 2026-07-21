"""Matrix careers board — RSS of QA / automation category pages (no key)."""
from __future__ import annotations

import re
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from .base import JobResult, JobSource
from .relevance import job_matches_query

_BASE = "https://www.matrix.co.il"
_TIMEOUT = 20
_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

# Category path segment under /jobs/משרות/ → human label.
_CATEGORIES = (
    ("testing-automation", "בדיקות תוכנה / automation"),
    ("פיתוח-אוטומציה", "פיתוח אוטומציה"),
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    return _WS_RE.sub(" ", unescape(_TAG_RE.sub(" ", text or ""))).strip()


def _feed_url(slug: str, page: int = 1) -> str:
    # Keep Hebrew slugs percent-encoded so requests doesn't mangle them.
    path = quote(f"jobs/משרות/{slug}/feed/", safe="/")
    url = f"{_BASE}/{path}"
    if page > 1:
        url += f"?paged={page}"
    return url


def _parse_pub(pub: str) -> str:
    pub = (pub or "").strip()
    if not pub:
        return ""
    try:
        return parsedate_to_datetime(pub).date().isoformat()
    except (TypeError, ValueError, IndexError):
        if "T" in pub:
            return pub.split("T", 1)[0][:10]
        return pub[:10] if len(pub) >= 10 else pub


def _parse_feed(xml_text: str, category: str) -> list[JobResult]:
    soup = BeautifulSoup(xml_text, "xml")
    out: list[JobResult] = []
    for item in soup.find_all("item"):
        title = (item.title.get_text(strip=True) if item.title else "") or ""
        link = (item.link.get_text(strip=True) if item.link else "") or ""
        if not title or not link:
            continue
        desc_el = item.find("description") or item.find("content:encoded")
        desc = _strip_html(desc_el.get_text() if desc_el else "")[:5000]
        posted = _parse_pub(item.pubDate.get_text(strip=True) if item.pubDate else "")
        slug = link.rstrip("/").rsplit("/", 1)[-1]
        out.append(JobResult(
            source="matrix",
            title=title,
            company="Matrix",
            location="Israel",
            url=link,
            description=desc or f"Matrix category: {category}",
            posted=posted,
            external_id=slug,
            raw={"category": category},
        ))
    return out


class MatrixSource(JobSource):
    name = "matrix"

    def is_configured(self) -> bool:
        return True

    def search(self, query: str, location: str = "Israel",
               limit: int = 20) -> list[JobResult]:
        results: list[JobResult] = []
        seen: set[str] = set()
        try:
            for slug, label in _CATEGORIES:
                if len(results) >= limit:
                    break
                for page in range(1, 6):
                    if len(results) >= limit:
                        break
                    resp = requests.get(
                        _feed_url(slug, page), headers=_UA, timeout=_TIMEOUT)
                    if resp.status_code >= 400:
                        break
                    batch = _parse_feed(resp.text, label)
                    if not batch:
                        break
                    for job in batch:
                        if job.url in seen:
                            continue
                        if not job_matches_query(
                                query, title=job.title,
                                description=job.description or ""):
                            continue
                        seen.add(job.url)
                        if location and location.strip():
                            job.location = location.strip()
                        results.append(job)
                        if len(results) >= limit:
                            break
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(f"Matrix unreachable: {exc}") from exc
        return results
