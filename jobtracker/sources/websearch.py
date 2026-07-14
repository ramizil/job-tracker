"""Web-search job source — `site:` searches over ATS job pages, no API key.

Runs queries like `site:comeet.com/jobs automation Israel` through DuckDuckGo
(via the `ddgs` library), which surfaces jobs hosted on ATS platforms (Comeet,
Greenhouse, Lever, SmartRecruiters…) that the aggregator APIs often miss —
especially for the Israeli market.

Replaces the original Google Custom Search backend: Google closed the Custom
Search JSON API to new customers (retired entirely Jan 2027), so new projects
get a permanent 403. DuckDuckGo needs no key, no quota, no Cloud project.
"""
from __future__ import annotations

import re
import time
from urllib.parse import parse_qs, urlparse

from .. import config
from .base import JobResult, JobSource

# Suffixes search engines show after page titles, e.g. " | Comeet".
_TITLE_NOISE = re.compile(
    r"\s*[|\-–·]\s*(comeet(\.com)?|greenhouse|lever|smartrecruiters|careers?|jobs?)\s*$",
    re.IGNORECASE)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _slug_to_name(slug: str) -> str:
    """'check-point-software' -> 'Check Point Software'."""
    return re.sub(r"[-_]+", " ", slug).strip().title()


def _company_from_url(url: str) -> str:
    """Derive the company from the ATS URL structure (most reliable signal)."""
    p = urlparse(url)
    host = p.netloc.lower()
    parts = [s for s in p.path.split("/") if s]
    if "comeet.com" in host and len(parts) >= 2 and parts[0] == "jobs":
        return _slug_to_name(parts[1])
    if "greenhouse.io" in host and parts:
        if parts[0] == "embed":  # boards.greenhouse.io/embed/job_app?for=acme
            co = (parse_qs(p.query).get("for") or [""])[0]
            return _slug_to_name(co)
        return _slug_to_name(parts[0])
    if "lever.co" in host and parts:
        return _slug_to_name(parts[0])
    if "smartrecruiters.com" in host and parts:
        return _slug_to_name(parts[0])
    return ""


def _split_title(title: str, url_company: str = "") -> tuple[str, str]:
    """Best-effort (job title, company) from a search-result title.

    Title order varies by ATS ("Title - Company" vs "Company - Title"), so when
    the company derived from the URL matches one half, the other half wins as
    the job title.
    """
    t = _TITLE_NOISE.sub("", title or "").strip()
    # Comeet: "Job opportunity: X at Acme" / Greenhouse: "Job Application for X at Acme"
    m = re.match(r"(?:job application for\s+|job opportunity:\s*)?(.+?)\s+at\s+(.+)$",
                 t, re.IGNORECASE)
    if m and m.group(2):
        return m.group(1).strip(), m.group(2).strip()
    for sep in (" - ", " – ", " | "):
        if sep in t:
            left, right = (p.strip() for p in t.split(sep, 1))
            if url_company:
                nc = _norm(url_company)
                if nc and (nc in _norm(left) or _norm(left) in nc):
                    return right, left   # "Company - Title" ordering
            return left, right
    return t, ""


def _ats_label(url: str) -> str:
    """Short board name for the result's source tag, e.g. 'comeet'."""
    host = urlparse(url).netloc.lower()
    for name in ("comeet", "greenhouse", "lever", "smartrecruiters"):
        if name in host:
            return name
    return host.removeprefix("www.") or "web"


def _looks_like_listing_page(url: str, title: str) -> bool:
    """Skip company job-board index pages ('Jobs at Acme', 'All open positions')."""
    p = urlparse(url)
    parts = [s for s in p.path.split("/") if s]
    # Comeet: jobs are /jobs/<company>/<uid>/<slug>/<uid>; anything shorter is an index.
    if "comeet.com" in p.netloc and len(parts) <= 3:
        return True
    if "greenhouse.io" in p.netloc and len(parts) <= 1:   # /<company>
        return True
    if "lever.co" in p.netloc and len(parts) <= 1:        # /<company>
        return True
    # SmartRecruiters: jobs are /<company>/<id>-slug; /<company> is an index.
    if "smartrecruiters.com" in p.netloc and len(parts) <= 1:
        return True
    t = (title or "").strip()
    if re.match(r"^(jobs at|careers?)\b", t, re.IGNORECASE):
        return True
    return bool(re.search(r"\b(all )?(open )?(positions|careers|jobs)\s*$",
                          t, re.IGNORECASE)) and len(parts) <= 2


class WebSearchSource(JobSource):
    """Job search via DuckDuckGo `site:` queries over configured ATS sites."""

    name = "websearch"

    def is_configured(self) -> bool:
        return bool(self.sites())

    @staticmethod
    def sites() -> list[str]:
        return [s.strip() for s in config.WEB_SEARCH_SITES.split(",") if s.strip()]

    def search(self, query: str, location: str = "Israel",
               limit: int = 20) -> list[JobResult]:
        try:
            from ddgs import DDGS
        except ImportError as exc:
            raise RuntimeError(
                "the 'ddgs' package is missing — run: pip install ddgs") from exc

        sites = self.sites()
        if not sites:
            return []
        # Fetch generously per site: search engines rank, they don't enumerate,
        # so a broad query ("automation") needs a deep top-N for good coverage.
        per_site = max(10, min(20, -(-limit // len(sites))))  # ceil

        ddgs = DDGS()
        per_site_results: list[list[JobResult]] = []
        seen: set[str] = set()
        errors: list[str] = []
        for i, site in enumerate(sites):
            q = f"site:{site} {query}"
            if location:
                q += f" {location}"
            try:
                if i:
                    time.sleep(0.7)   # be polite; avoids DDG rate-limiting
                items = ddgs.text(q, max_results=per_site) or []
            except Exception as exc:
                errors.append(f"{site}: {exc}")
                continue

            bucket: list[JobResult] = []
            for it in items:
                url = it.get("href") or ""
                if not url or url in seen:
                    continue
                seen.add(url)
                title_raw = it.get("title") or ""
                if _looks_like_listing_page(url, title_raw):
                    continue
                url_co = _company_from_url(url)
                title, company = _split_title(title_raw, url_co)
                bucket.append(JobResult(
                    source=f"web:{_ats_label(url)}",
                    title=title,
                    company=url_co or company,
                    location=location,
                    url=url,
                    description=(it.get("body") or "")[:5000],
                    external_id=url,
                    raw=dict(it),
                ))
            per_site_results.append(bucket)

        if not any(per_site_results) and errors:
            # Surface a real problem instead of a silent empty list.
            raise RuntimeError("; ".join(errors[:2]))

        # Interleave across sites so the limit doesn't crowd out the boards
        # that were queried last.
        results: list[JobResult] = []
        for rank in range(max((len(b) for b in per_site_results), default=0)):
            for bucket in per_site_results:
                if rank < len(bucket):
                    results.append(bucket[rank])
        return results[:limit]
