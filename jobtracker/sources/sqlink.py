"""SQLink career board — HTML scrape of /career/db/ category pages (no key)."""
from __future__ import annotations

import re
from urllib.parse import urljoin, unquote

import requests
from bs4 import BeautifulSoup

from .base import JobResult, JobSource
from .relevance import job_matches_query

_BASE = "https://www.sqlink.com"
_DB = f"{_BASE}/career/db/"
_TIMEOUT = 20
_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

# Query keywords → SQLink category path segments (Hebrew URL slugs).
_QA_CATS = (
    "פיתוח-אוטומציה",
    "qa",
    "qa-webmobile",
    "qa-ראש-צוות",
    "ראש-צוות-אוטומציה",
    "qa-מולטידיסיפלינרי",
    "qa-פיננסיביטוח",
    "qa-תקשורת",
)
_JOB_HREF = re.compile(
    r"/career/db/([^/]+)/([^/?#]+)/?$", re.I
)


def _query_terms(query: str) -> list[str]:
    q = (query or "").strip()
    if not q:
        return ["QA", "automation"]
    if re.search(r"\s+OR\s+", q, re.I):
        return [p.strip().strip('"') for p in re.split(r"\s+OR\s+", q, flags=re.I)
                if p.strip()]
    return [q.strip().strip('"')]


def _categories_for(query: str) -> list[str]:
    terms = " ".join(_query_terms(query)).lower()
    if any(k in terms for k in (
        "qa", "qe", "sdet", "test", "automation", "אוטומצ", "בדיק", "quality",
    )):
        return list(_QA_CATS)
    # Unknown query — browse a couple of broad QA cats + let title filter work.
    return list(_QA_CATS[:3])


def _abs(url: str) -> str:
    return urljoin(_BASE, url)


def _parse_jobs(html: str, category: str) -> list[tuple[str, str, str]]:
    """Return (title, url, category) from a category listing page."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        m = _JOB_HREF.search(href.replace("\\", "/"))
        if not m:
            continue
        cat, slug = m.group(1), m.group(2)
        # Skip the category index itself (no extra slug segment meaningfully).
        if not slug or slug == cat:
            continue
        title = a.get_text(" ", strip=True)
        if not title or len(title) < 4:
            continue
        # Drop nav / share noise.
        if title.lower() in ("facebook", "linkedin", "twitter", "whatsapp"):
            continue
        full = _abs(href)
        if full in seen:
            continue
        seen.add(full)
        out.append((title, full, unquote(cat)))
    return out


class SQLinkSource(JobSource):
    name = "sqlink"

    def is_configured(self) -> bool:
        return True

    def search(self, query: str, location: str = "Israel",
               limit: int = 20) -> list[JobResult]:
        # SQLink is Israel staffing — location filter is informational only.
        cats = _categories_for(query)
        results: list[JobResult] = []
        seen_urls: set[str] = set()
        try:
            for cat in cats:
                if len(results) >= limit:
                    break
                url = f"{_DB}{cat}/"
                resp = requests.get(url, headers=_UA, timeout=_TIMEOUT)
                if resp.status_code >= 400:
                    continue
                for title, job_url, cat_name in _parse_jobs(resp.text, cat):
                    if job_url in seen_urls:
                        continue
                    if not job_matches_query(query, title=title, description=""):
                        continue
                    seen_urls.add(job_url)
                    results.append(JobResult(
                        source="sqlink",
                        title=title,
                        company="SQLink",
                        location=location or "Israel",
                        url=job_url,
                        description=f"SQLink category: {cat_name}",
                        external_id=job_url.rstrip("/").rsplit("/", 1)[-1],
                        raw={"category": cat_name},
                    ))
                    if len(results) >= limit:
                        break
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(f"SQLink unreachable: {exc}") from exc
        return results
