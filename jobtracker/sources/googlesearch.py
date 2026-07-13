"""Google Programmable Search source — `site:` searches over ATS job pages.

Uses the official Custom Search JSON API (free tier: 100 queries/day) to run
queries like `site:comeet.com/jobs automation Israel`, which surfaces jobs
hosted on ATS platforms (Comeet, Greenhouse, Lever, SmartRecruiters…) that the
aggregator APIs often miss — especially for the Israeli market.

Docs: https://developers.google.com/custom-search/v1/overview
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

import requests

from .. import config
from .base import JobResult, JobSource

_API = "https://www.googleapis.com/customsearch/v1"

# Suffixes Google appends to page titles, e.g. " | Comeet", " - Greenhouse".
_TITLE_NOISE = re.compile(
    r"\s*[|\-–·]\s*(comeet|greenhouse|lever|smartrecruiters|careers?|jobs?)\s*$",
    re.IGNORECASE)


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
        return _slug_to_name(parts[0])
    if "lever.co" in host and parts:
        return _slug_to_name(parts[0])
    if "smartrecruiters.com" in host and parts:
        return _slug_to_name(parts[0])
    return ""


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _split_title(title: str, url_company: str = "") -> tuple[str, str]:
    """Best-effort (job title, company) from a search-result title.

    Title order varies by ATS ("Title - Company" vs "Company - Title"), so when
    the company derived from the URL matches one half, the other half wins as
    the job title.
    """
    t = _TITLE_NOISE.sub("", title or "").strip()
    # Greenhouse: "Job Application for Automation Engineer at Acme"
    m = re.match(r"(?:job application for\s+)?(.+?)\s+at\s+(.+)$", t, re.IGNORECASE)
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


def _looks_like_listing_page(url: str, title: str) -> bool:
    """Skip company job-board index pages ('All open positions at …')."""
    p = urlparse(url)
    parts = [s for s in p.path.split("/") if s]
    if "comeet.com" in p.netloc and len(parts) <= 2:      # /jobs/<company>
        return True
    if "greenhouse.io" in p.netloc and len(parts) <= 1:   # /<company>
        return True
    if "lever.co" in p.netloc and len(parts) <= 1:        # /<company>
        return True
    return bool(re.search(r"\b(all )?(open )?(positions|careers|jobs)\s*$",
                          title or "", re.IGNORECASE)) and len(parts) <= 2


class GoogleSearchSource(JobSource):
    """Job search via Google Custom Search over configured `site:` filters."""

    name = "google"

    def is_configured(self) -> bool:
        return bool(config.GOOGLE_CSE_KEY and config.GOOGLE_CSE_CX)

    @staticmethod
    def sites() -> list[str]:
        return [s.strip() for s in config.GOOGLE_CSE_SITES.split(",") if s.strip()]

    def search(self, query: str, location: str = "Israel",
               limit: int = 20) -> list[JobResult]:
        if not self.is_configured():
            return []

        sites = self.sites()
        if not sites:
            return []
        per_site = max(1, min(10, -(-limit // len(sites))))  # ceil, API max 10

        results: list[JobResult] = []
        seen: set[str] = set()
        errors: list[str] = []
        for site in sites:
            q = f"site:{site} {query}"
            if location:
                q += f" {location}"
            try:
                resp = requests.get(_API, params={
                    "key": config.GOOGLE_CSE_KEY,
                    "cx": config.GOOGLE_CSE_CX,
                    "q": q,
                    "num": per_site,
                }, timeout=30)
                if resp.status_code == 429:
                    raise RuntimeError("daily free quota (100 queries) used up")
                if resp.status_code == 403:
                    detail = ""
                    try:
                        detail = resp.json()["error"]["message"]
                    except Exception:
                        pass
                    raise RuntimeError(
                        f"access denied — check that the Custom Search API is "
                        f"enabled and the key/cx are correct. {detail}".strip())
                resp.raise_for_status()
                items = resp.json().get("items") or []
            except Exception as exc:
                errors.append(f"{site}: {exc}")
                continue

            for it in items:
                url = it.get("link") or ""
                if not url or url in seen:
                    continue
                seen.add(url)
                title_raw = it.get("title") or ""
                if _looks_like_listing_page(url, title_raw):
                    continue
                url_co = _company_from_url(url)
                title, company = _split_title(title_raw, url_co)
                company = url_co or company
                # og: metadata (when Google indexed it) beats the plain snippet.
                meta = {}
                try:
                    meta = (it.get("pagemap", {}).get("metatags") or [{}])[0]
                except Exception:
                    pass
                desc = meta.get("og:description") or it.get("snippet") or ""
                og_title = _split_title(meta.get("og:title") or "", url_co)[0]
                results.append(JobResult(
                    source=self.name,
                    title=og_title or title,
                    company=company,
                    location=location,
                    url=url,
                    description=desc[:5000],
                    external_id=url,
                    raw=it,
                ))

        if not results and errors:
            # Surface a config/quota problem instead of a silent empty list.
            raise RuntimeError("; ".join(errors[:2]))
        return results[:limit]
