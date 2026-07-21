"""Read state + comments for Search results (mailbox-style, like job alerts)."""
from __future__ import annotations

from .db import get_connection, now_iso
from .search_hidden import job_key_for, keys_for


def _ensure_key(url: str, company: str = "", title: str = "") -> str:
    key = job_key_for(url, company, title)
    if not key:
        raise ValueError("Need a URL (or company+title) for this search result")
    return key


def upsert(*, url: str, title: str = "", company: str = "",
           comment: str | None = None, seen: bool | None = None) -> str:
    """Create/update meta for a search hit. Only updates fields that are set."""
    job_key = _ensure_key(url, company, title)
    ts = now_iso()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT comment, seen FROM search_meta WHERE job_key=?",
            (job_key,),
        ).fetchone()
        cur_comment = (row["comment"] if row else "") or ""
        cur_seen = int(row["seen"]) if row else 0
        if comment is not None:
            cur_comment = (comment or "").strip()[:500]
        if seen is not None:
            cur_seen = 1 if seen else 0
        conn.execute(
            """INSERT INTO search_meta
                 (job_key, title, company, url, comment, seen, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(job_key) DO UPDATE SET
                 title=excluded.title,
                 company=excluded.company,
                 url=excluded.url,
                 comment=excluded.comment,
                 seen=excluded.seen,
                 updated_at=excluded.updated_at""",
            (job_key, title or "", company or "", url or "",
             cur_comment, cur_seen, ts),
        )
    return job_key


def set_comment(*, url: str, title: str = "", company: str = "",
                comment: str = "") -> str:
    return upsert(url=url, title=title, company=company, comment=comment)


def set_seen(*, url: str, title: str = "", company: str = "",
             seen: bool = True) -> str:
    return upsert(url=url, title=title, company=company, seen=seen)


def set_seen_many(jobs: list[dict], *, seen: bool = True) -> int:
    """jobs: list of {url, title, company}. Returns how many keys updated."""
    n = 0
    for j in jobs:
        try:
            set_seen(
                url=j.get("url", ""), title=j.get("title", ""),
                company=j.get("company", ""), seen=seen,
            )
            n += 1
        except ValueError:
            continue
    return n


def meta_for(url: str, company: str = "", title: str = "") -> dict:
    """Return {comment, seen, job_key} for a hit (defaults if none stored)."""
    keys = keys_for(url, company, title)
    empty = {"comment": "", "seen": False, "job_key": job_key_for(url, company, title)}
    if not keys:
        return empty
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM search_meta").fetchall()
    for r in rows:
        row_keys = {r["job_key"]} | keys_for(
            r["url"] or "", r["company"] or "", r["title"] or "")
        if keys & row_keys:
            return {
                "comment": r["comment"] or "",
                "seen": bool(r["seen"]),
                "job_key": r["job_key"],
            }
    return empty


def attach_meta(results: list[dict]) -> list[dict]:
    """Add job_key / comment / seen onto each enrich_search_results item."""
    for item in results:
        job = item.get("job")
        if not job:
            continue
        m = meta_for(
            getattr(job, "url", "") or "",
            getattr(job, "company", "") or "",
            getattr(job, "title", "") or "",
        )
        item["job_key"] = m["job_key"]
        item["comment"] = m["comment"]
        item["seen"] = m["seen"]
    return results
