"""Shared job-board search + last-search cache (manual UI + hourly auto)."""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from . import config, search_hidden
from .db import now_iso
from .matcher import score_job
from .sources import get_sources
from .sources.base import JobResult
from .sources.relevance import job_matches_query

log = logging.getLogger(__name__)

AUTO_SEARCH_INTERVAL_S = 3600  # 1 hour
_PER_SOURCE_LIMIT = 20
_SOURCE_FETCH_LIMIT = 40

_auto_started = False
_auto_lock = threading.Lock()


def last_search_path() -> Path:
    return Path(config.PROFILE_DIR) / "last_search.json"


def job_result_to_dict(job: JobResult) -> dict:
    return {
        "source": job.source,
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "url": job.url,
        "description": job.description,
        "salary": job.salary,
        "posted": job.posted,
        "external_id": job.external_id,
    }


def job_result_from_dict(data: dict) -> JobResult:
    return JobResult(
        source=data.get("source", ""),
        title=data.get("title", ""),
        company=data.get("company", ""),
        location=data.get("location", ""),
        url=data.get("url", ""),
        description=data.get("description", ""),
        salary=data.get("salary", ""),
        posted=data.get("posted", ""),
        external_id=data.get("external_id", ""),
    )


def save_last_search(query: str, location: str, results: list) -> None:
    payload = {
        "query": query,
        "location": location,
        "searched_at": now_iso(),
        "results": [
            {"job": job_result_to_dict(item["job"]), "score": item["score"]}
            for item in results
        ],
    }
    path = last_search_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def load_last_search() -> dict | None:
    try:
        data = json.loads(last_search_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def resolve_query_location(
        query: str = "", location: str = "") -> tuple[str, str]:
    """Pick query/location: explicit args → last search → profile titles."""
    cached = load_last_search() or {}
    q = (query or "").strip() or (cached.get("query") or "").strip()
    loc = (location or "").strip() or (cached.get("location") or "").strip()
    if not q:
        try:
            from . import resume as resume_mod
            titles = resume_mod.load_profile().get("target_titles") or []
            q = " OR ".join(titles[:3])
        except Exception:
            q = ""
    if not loc:
        loc = "Israel"
    return q, loc


def run_search(query: str, location: str, *,
               flash_cb=None) -> list[dict]:
    """Search all enabled sources. Returns ``[{job, score}, ...]`` sorted.

    ``flash_cb(message, category)`` is optional (web UI only). Background
    auto-search omits it.
    """
    def _flash(msg: str, cat: str = "ok") -> None:
        if flash_cb:
            try:
                flash_cb(msg, cat)
            except Exception:
                pass

    q = (query or "").strip()
    loc = (location or "").strip() or "Israel"
    if not q:
        return []
    sources = get_sources()
    if not sources:
        return []

    from . import resume as resume_mod
    try:
        prof = resume_mod.load_profile()
    except Exception:
        prof = {}

    hide_keys = search_hidden.hidden_key_set()
    results: list[dict] = []
    for src in sources:
        try:
            count = 0
            for job in src.search(q, location=loc, limit=_SOURCE_FETCH_LIMIT):
                if not job_matches_query(
                        q, title=job.title, description=job.description or ""):
                    continue
                if search_hidden.is_hidden(
                        job.url, job.company, job.title, key_set=hide_keys):
                    continue
                m = score_job(job.title, job.description, prof)
                results.append({"job": job, "score": m.score})
                count += 1
                if count >= _PER_SOURCE_LIMIT:
                    break
            if src.name != "websearch":
                _flash(f"{src.name}: {count} result(s).", "ok")
            else:
                soft = sum(
                    1 for item in results
                    if getattr(item.get("job"), "raw", None)
                    and (item["job"].raw or {}).get("soft_verify")
                ) if count else 0
                if count and soft:
                    _flash(
                        f"websearch: {count} result(s) for “{loc or 'any'}” "
                        f"— some links could not be fully verified live.",
                        "ok")
                elif count:
                    _flash(f"websearch: {count} live posting(s) in "
                           f"“{loc or 'any'}”.", "ok")
                else:
                    others = sum(
                        1 for item in results
                        if not str(getattr(item.get("job"), "source", "")
                                   ).startswith("web:")
                    )
                    tip = ("DuckDuckGo found nothing useful this time "
                           f"for “{loc or 'any'}”. Wait ~1 min and "
                           "retry, or use a shorter keyword.")
                    if others:
                        _flash(f"websearch: skipped — {tip} "
                               f"({others} result(s) from other sources).",
                               "ok")
                    else:
                        _flash(f"websearch: {tip}", "error")
        except Exception as exc:
            _flash(f"{src.name}: {exc}", "error")
            log.warning("search source %s failed: %s", src.name, exc)

    results.sort(key=lambda x: x["score"], reverse=True)
    save_last_search(q, loc, results)
    return results


def run_auto_search() -> dict:
    """Hourly background search using last query (or profile titles)."""
    if not getattr(config, "AUTO_SEARCH", True):
        return {"skipped": "disabled"}
    if not get_sources():
        return {"skipped": "no sources"}
    query, location = resolve_query_location()
    if not query:
        return {"skipped": "no query"}
    results = run_search(query, location)
    return {
        "query": query,
        "location": location,
        "count": len(results),
        "searched_at": now_iso(),
    }


def start_auto_search() -> None:
    """Search job boards every hour while the server is up."""
    global _auto_started
    with _auto_lock:
        if _auto_started:
            return
        _auto_started = True
    threading.Thread(target=_auto_loop, name="jobtracker-auto-search",
                     daemon=True).start()


def _auto_loop() -> None:
    time.sleep(90)  # stagger past Gmail / connection probes
    while True:
        try:
            summary = run_auto_search()
            if "count" in summary:
                log.info("auto-search: %s result(s) for %r @ %r",
                         summary["count"], summary.get("query"),
                         summary.get("location"))
            else:
                log.debug("auto-search skipped: %s", summary.get("skipped"))
        except Exception:
            log.exception("auto-search failed")
        time.sleep(AUTO_SEARCH_INTERVAL_S)
