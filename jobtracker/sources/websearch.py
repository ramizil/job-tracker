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

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import requests

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


_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

# Snippet/location text that clearly means "not Israel".
_ABROAD_MARKERS = (
    "united states", "usa", "u.s.a", " us ", " us-", "us remote", "north america",
    "united kingdom", " uk ", "uk remote", "london,", "london -", "germany",
    "france", "spain", "poland", "india", "singapore", "canada", "australia",
    "europe remote", "emea", "apac", "latin america", "netherlands", "ireland",
    "switzerland", "sweden", "portugal", "italy", "turkey", "dubai", "uae",
)
# Cities/areas that mean "Israel" on job pages that don't say the country.
_IL_HINTS = (
    "israel", "tel aviv", "tel-aviv", "jerusalem", "haifa", "herzliya",
    "herzliyya", "ramat gan", "ramat-gan", "netanya", "beer sheva",
    "be'er sheva", "beersheba", "raanana", "ra'anana", "petah tikva",
    "petach tikva", "rehovot", "kfar saba", "hod hasharon", "yokneam",
    "caesarea", "rosh haayin", "rosh ha'ayin", "bnei brak", "givatayim",
    "holon", "or yehuda", "airport city", "modiin", "modi'in", "yehud",
    "kiryat ono", "kiryat gat", "haifa bay",
)

# Older settings used sub-paths that DDG now indexes poorly — map to root domains.
_SITE_ALIASES = {
    "comeet.com/jobs": "comeet.com",
    "www.comeet.com/jobs": "comeet.com",
    "boards.greenhouse.io": "greenhouse.io",
    "job-boards.greenhouse.io": "greenhouse.io",
    "careers.smartrecruiters.com": "jobs.smartrecruiters.com",
    "www.careers.smartrecruiters.com": "jobs.smartrecruiters.com",
}

# Search-result titles that aren't the real job title (filled in during verify).
_WEAK_TITLES = {
    "", "job", "jobs", "careers", "career", "opening", "openings",
    "position", "positions", "apply", "hiring",
}

_DDG_BACKENDS = ("auto", "bing", "yahoo")
# Sticky preference: the last backend that returned hits (DDG is flaky).
_preferred_backend: str = "auto"


def _looks_like_listing_page(url: str, title: str) -> bool:
    """Skip company job-board index pages and malformed job URLs."""
    p = urlparse(url)
    host = p.netloc.lower()
    parts = [s for s in p.path.split("/") if s]
    q = parse_qs(p.query)

    if "comeet.com" in host:
        # Real posting: /jobs/<co>/<uid>/<slug>[/uid]
        return not (len(parts) >= 4 and parts[0] == "jobs")

    if "greenhouse.io" in host:
        if parts and parts[0] == "embed":
            return not bool(q.get("token"))
        # Real posting: /<co>/jobs/<numeric-id>
        return not (len(parts) >= 3 and parts[1] == "jobs" and parts[2].isdigit())

    if "lever.co" in host:
        # Real posting: /<co>/<uuid>[/apply]
        if len(parts) < 2:
            return True
        return not bool(_UUID.match(parts[1]))

    if "smartrecruiters.com" in host:
        # Real posting: /<co>/<id>-slug
        return not (len(parts) >= 2 and re.search(r"\d", parts[1]))

    t = (title or "").strip()
    if re.match(r"^(jobs at|careers?|current openings at)\b", t, re.IGNORECASE):
        return True
    return bool(re.search(r"\b(all )?(open )?(positions|careers|jobs)\s*$",
                          t, re.IGNORECASE)) and len(parts) <= 2


def _normalize_site(site: str) -> str:
    s = (site or "").strip().lower().removeprefix("www.")
    return _SITE_ALIASES.get(s, s)


def _quote_query(query: str) -> str:
    q = (query or "").strip()
    if not q:
        return q
    if q.startswith('"') and q.endswith('"'):
        return q
    # DDG treats quoted OR strings literally — leave them unquoted.
    if re.search(r"\s+OR\s+", q, re.I):
        return q
    return f'"{q}"' if " " in q else q


def _search_terms(query: str) -> list[str]:
    """Split OR-combined queries (default blank search) into separate DDG lookups."""
    q = (query or "").strip()
    if not q:
        return [""]
    if re.search(r"\s+OR\s+", q, re.I):
        return [p.strip() for p in re.split(r"\s+OR\s+", q, flags=re.I) if p.strip()][:3]
    return [q]


def _query_variants(site: str, query: str, location: str) -> list[str]:
    """A few query shapes — DDG is flaky and different phrasings surface jobs."""
    domain = _normalize_site(site)
    quoted = _quote_query(query)
    bare = (query or "").strip().strip('"')
    loc = (location or "").strip()
    variants: list[str] = []
    if loc:
        # Most reliable for Comeet / Greenhouse (DDG chokes on bare site: queries).
        variants.append(f"{quoted} {loc} site:{domain}")
        variants.append(f"site:{domain} {quoted} {loc}")
        if bare and bare != quoted:
            variants.append(f"{bare} {loc} site:{domain}")
        variants.append(f"{quoted} jobs {loc} site:{domain}")
    variants.append(f"site:{domain} {quoted}")
    if bare and bare != quoted:
        variants.append(f"site:{domain} {bare}")
    # Dedupe while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _ddg_text(ddgs, query: str, *, max_results: int) -> list[dict]:
    """Run a DDG text search with backend fallbacks (single backend often fails)."""
    global _preferred_backend
    last_exc: Exception | None = None
    order = [_preferred_backend] + [b for b in _DDG_BACKENDS if b != _preferred_backend]
    for backend in order:
        try:
            items = ddgs.text(query, max_results=max_results, backend=backend) or []
            if items:
                _preferred_backend = backend
                return items
        except Exception as exc:
            last_exc = exc
    if last_exc:
        raise last_exc
    return []


def _weak_title(title: str) -> bool:
    return (title or "").strip().lower() in _WEAK_TITLES


def _text_location_ok(text: str, requested: str) -> bool:
    """Pre-filter on search snippets before opening the link."""
    req = (requested or "").strip().lower()
    if not req:
        return True
    t = (text or "").lower().replace("’", "'")
    if not t:
        return True
    if "israel" in req:
        if any(h in t for h in _IL_HINTS):
            return True
        if any(m in t for m in _ABROAD_MARKERS):
            return False
        return True   # snippet silent — let the page fetch decide
    return req in t


# ------------------------------------------------------------------ #
# Live verification: search indexes go stale (closed jobs 404 or bounce
# to the board index), so every candidate link is opened before it is
# shown — and the job's real location is read off the page.

_HDRS = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36")}

_DEAD_MARKERS = (
    "page not found", "job board you were viewing is no longer active",
    "couldn't find anything here", "might have closed, or it has been removed",
    "position is no longer open", "posting is no longer available",
    "job not found", "this job is no longer available",
)
# Lever serves closed postings as HTTP 200 with a 404 *title*.
_DEAD_TITLE = re.compile(r"<title>[^<]*(?:not found|404)[^<]*</title>", re.IGNORECASE)
_BOARD_PAGE = re.compile(r"current openings at\b", re.IGNORECASE)


def _extract_location(html: str) -> str:
    """The job's location as stated on the ATS page itself.

    All four supported ATS platforms embed schema.org JobPosting JSON-LD;
    a couple of HTML fallbacks cover pages that don't.
    """
    for m in re.finditer(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>",
                         html, re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        for d in (data if isinstance(data, list) else [data]):
            if not (isinstance(d, dict) and d.get("@type") == "JobPosting"):
                continue
            locs = d.get("jobLocation") or []
            parts: list[str] = []
            for loc in (locs if isinstance(locs, list) else [locs]):
                addr = loc.get("address", {}) if isinstance(loc, dict) else {}
                if isinstance(addr, str):
                    parts.append(addr)
                    continue
                for key in ("addressLocality", "addressRegion", "addressCountry"):
                    v = addr.get(key)
                    if isinstance(v, dict):
                        v = v.get("name")
                    if v:
                        parts.append(str(v))
            if parts:
                return ", ".join(dict.fromkeys(p.strip() for p in parts if p.strip()))
    # Comeet embeds the position's location as a JSON object:
    #   "location": {"name": "HQ", "country": "IL", "city": "Petah Tikva", ...}
    m = re.search(r'"location"\s*:\s*\{[^{}]*\}', html)
    if m:
        try:
            obj = json.loads(m.group(0).split(":", 1)[1])
            parts = [obj.get("city"), obj.get("state"), obj.get("country")]
            joined = ", ".join(p for p in parts if p)
            if joined:
                return joined
            if obj.get("name"):
                return str(obj["name"])
        except Exception:
            pass
    # Greenhouse job-boards (Remix UI): "job_post_location":"Tel Aviv, Israel"
    m = re.search(r'"job_post_location"\s*:\s*"([^"]{2,80})"', html)
    if m:
        return m.group(1).strip()
    # Older Greenhouse UI: "location":"Tel Aviv, Israel"
    m = re.search(r'"location"\s*:\s*"([^"]{2,80})"', html)
    if m:
        return m.group(1).strip()
    # Lever: <div class="posting-category ... location">Tel Aviv, Israel</div>
    m = re.search(r'class="[^"]*posting-categor[^"]*location[^"]*"[^>]*>([^<]+)<', html)
    if not m:
        m = re.search(r'class="[^"]*\blocation\b[^"]*"[^>]*>\s*([^<]+?)\s*<', html)
    if m:
        return m.group(1).strip()
    # SmartRecruiters: <spl-job-location formattedAddress="Tel Aviv, Israel" …>
    m = re.search(r'<spl-job-location[^>]*\sformattedAddress="([^"]{2,120})"', html, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r'itemprop="addressLocality"[^>]*content="([^"]{2,80})"', html, re.I)
    if m:
        country = re.search(r'itemprop="addressCountry"[^>]*content="([^"]{2,40})"',
                            html, re.I)
        if country:
            return f"{m.group(1).strip()}, {country.group(1).strip()}"
        return m.group(1).strip()
    return ""


def _extract_title(html: str) -> str:
    """Real job title from the ATS page (search snippets often say just \"Jobs\")."""
    for m in re.finditer(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>",
                         html, re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        for d in (data if isinstance(data, list) else [data]):
            if isinstance(d, dict) and d.get("@type") == "JobPosting":
                title = (d.get("title") or "").strip()
                if title and not _weak_title(title):
                    return title
    for pat in (
        r'"job_post_title"\s*:\s*"([^"]{3,120})"',
        r'"posting_title"\s*:\s*"([^"]{3,120})"',
        r"<title>([^<|]{3,120})",
    ):
        m = re.search(pat, html, re.I)
        if m:
            title = _TITLE_NOISE.sub("", m.group(1)).strip()
            if title and not _weak_title(title):
                return title
    return ""


def _page_text_location_ok(text: str, requested: str) -> bool:
    """Fallback when structured location is missing — scan visible page text."""
    req = (requested or "").strip().lower()
    if not req:
        return True
    t = (text or "").lower().replace("'", "'")
    if "israel" in req:
        if any(h in t for h in _IL_HINTS):
            return True
        if any(m in t for m in _ABROAD_MARKERS):
            return False
        return False
    return req in t


def _location_ok(extracted: str, requested: str) -> bool:
    """Keep the job only if its on-page location matches the requested one."""
    req = (requested or "").strip().lower()
    if not req:
        return True
    e = (extracted or "").strip().lower().replace("’", "'")
    if not e:
        return False   # location requested but page didn't state one — drop
    if req in e:
        return True
    if "israel" in req:
        if any(h in e for h in _IL_HINTS):
            return True
        if any(m in e for m in _ABROAD_MARKERS):
            return False
        # Country code as a whole token (IL, not Brazil).
        return bool(re.search(r"\bil\b", e))
    return False


def _verify(result: JobResult, requested_location: str) -> JobResult | str | None:
    """Open the link; drop dead postings and location mismatches.

    Returns the (possibly enriched) result, None when it must be dropped, or
    "neterr" when the page couldn't be fetched at all (network problem).
    """
    resp = None
    for attempt in (1, 2):   # one retry: first hit can time out on cold DNS/TLS
        try:
            resp = requests.get(result.url, headers=_HDRS, timeout=12,
                                allow_redirects=True)
            break
        except Exception:
            if attempt == 2:
                return "neterr"
    final_url = resp.url.lower()
    # Greenhouse bounces closed jobs to the board index with ?error=true.
    if resp.status_code >= 400 or "error=true" in final_url:
        return None
    if _looks_like_listing_page(resp.url, ""):
        return None
    html = resp.text[:300_000]
    low = html.lower()
    if (any(marker in low for marker in _DEAD_MARKERS)
            or _DEAD_TITLE.search(html)
            or _BOARD_PAGE.search(low)):
        return None
    title = _extract_title(html)
    if title and _weak_title(result.title):
        result.title = title
    loc = _extract_location(html)
    if loc:
        result.location = loc
    if _location_ok(loc, requested_location):
        return result
    if loc:
        return None
    if _page_text_location_ok(low, requested_location):
        if not result.location:
            result.location = requested_location
        return result
    return None


def _mark_soft(results: list[JobResult]) -> list[JobResult]:
    for r in results:
        raw = dict(r.raw or {})
        raw["soft_verify"] = True
        r.raw = raw
    return results


def _verify_all(candidates: list[JobResult], requested_location: str,
                limit: int) -> list[JobResult]:
    # Many candidates die on verification — check a deeper pool.
    pool = candidates[: max(limit * 5, limit)]
    with ThreadPoolExecutor(max_workers=10) as ex:
        checked = list(ex.map(lambda r: _verify(r, requested_location), pool))
    # If (nearly) every fetch failed at the network level, the machine likely
    # can't reach the job boards directly (proxy/firewall) — better to show
    # unverified results than nothing.
    neterrs = sum(1 for c in checked if c == "neterr")
    if pool and neterrs >= max(3, int(len(pool) * 0.7)):
        return _mark_soft(candidates[:limit])
    good = [c for c in checked if isinstance(c, JobResult)]
    if good:
        return good[:limit]
    # Verification dropped everything (strict location / flaky pages) but DDG
    # did find candidates — return soft results rather than an empty page.
    if candidates:
        soft = [
            c for c in candidates
            if _text_location_ok(f"{c.title} {c.location} {c.description}",
                                 requested_location)
        ] or candidates
        return _mark_soft(soft[:limit])
    return []


class WebSearchSource(JobSource):
    """Job search via DuckDuckGo `site:` queries over configured ATS sites."""

    name = "websearch"

    def is_configured(self) -> bool:
        return bool(self.sites())

    @staticmethod
    def sites() -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for raw in config.WEB_SEARCH_SITES.split(","):
            site = _normalize_site(raw.strip())
            if site and site not in seen:
                seen.add(site)
                out.append(site)
        return out

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

        ddgs = DDGS(timeout=14)
        terms = _search_terms(query)
        # Second-chance keywords: long phrases / OR-lists often get zero DDG
        # hits — also try a short head word (and common QA shorthand).
        extras: list[str] = []
        head = (query or "").strip().split()
        if len(head) >= 2:
            extras.append(head[0])
        qlow = (query or "").lower()
        if any(k in qlow for k in ("automation", "sdet", "qa ", " qa", "test")):
            for short in ("automation", "QA", "SDET"):
                if short.lower() not in {t.lower() for t in terms}:
                    extras.append(short)
        seen_t = {t.lower() for t in terms}
        for e in extras:
            if e and e.lower() not in seen_t:
                terms.append(e)
                seen_t.add(e.lower())

        def _collect(search_terms: list[str]) -> tuple[list[list[JobResult]], list[str]]:
            per_site: list[list[JobResult]] = []
            seen_urls: set[str] = set()
            errs: list[str] = []
            for i, site in enumerate(sites):
                bucket: list[JobResult] = []
                for term in search_terms:
                    for q in _query_variants(site, term, location):
                        try:
                            if i or bucket:
                                time.sleep(0.45)
                            items = _ddg_text(ddgs, q, max_results=per_site)
                        except Exception as exc:
                            errs.append(f"{site}: {exc}")
                            continue
                        for it in items:
                            url = it.get("href") or ""
                            if not url or url in seen_urls:
                                continue
                            seen_urls.add(url)
                            title_raw = it.get("title") or ""
                            if _looks_like_listing_page(url, title_raw):
                                continue
                            snippet = (it.get("body") or "")[:5000]
                            if not _text_location_ok(f"{title_raw} {snippet}", location):
                                continue
                            url_co = _company_from_url(url)
                            title, company = _split_title(title_raw, url_co)
                            bucket.append(JobResult(
                                source=f"web:{_ats_label(url)}",
                                title=title,
                                company=url_co or company,
                                location=location,
                                url=url,
                                description=snippet,
                                external_id=url,
                                raw=dict(it),
                            ))
                        if bucket:
                            break
                    if bucket:
                        break
                per_site.append(bucket)
            return per_site, errs

        per_site_results, errors = _collect(terms)

        if not any(per_site_results) and errors:
            # Surface a real problem instead of a silent empty list.
            raise RuntimeError("; ".join(errors[:2]))

        # Interleave across sites so the limit doesn't crowd out the boards
        # that were queried last.
        candidates: list[JobResult] = []
        for rank in range(max((len(b) for b in per_site_results), default=0)):
            for bucket in per_site_results:
                if rank < len(bucket):
                    candidates.append(bucket[rank])

        # Open every link before showing it: drops closed/dead postings and
        # jobs whose on-page location doesn't match the requested one.
        return _verify_all(candidates, location, limit)
