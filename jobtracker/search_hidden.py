"""Dismiss / ignore list for Search results (same idea as job alerts).

Dismissed = hide from the active results list (can restore).
Ignored  = never show this job again across future searches.
Matching is by URL keys (LinkedIn id, Greenhouse id, normalized URL, …).
"""
from __future__ import annotations

from .db import get_connection, now_iso
from .tracker import _job_url_keys, _norm_job_url, _norm_match_text


def _primary_key(url: str, company: str = "", title: str = "") -> str:
    keys = _job_url_keys(url)
    labeled = sorted(k for k in keys if ":" in k)
    if labeled:
        return labeled[0]
    if keys:
        return sorted(keys)[0]
    nu = _norm_job_url(url)
    if nu:
        return nu
    c = _norm_match_text(company)
    t = _norm_match_text(title)
    if c and t:
        return f"ct:{c}|{t}"
    return ""


def _keys_for(url: str, company: str = "", title: str = "") -> set[str]:
    keys = set(_job_url_keys(url))
    pk = _primary_key(url, company, title)
    if pk:
        keys.add(pk)
    return keys


def hide(*, url: str, title: str = "", company: str = "",
         ignored: bool = False) -> str:
    """Dismiss (ignored=False) or ignore forever. Returns the job_key."""
    job_key = _primary_key(url, company, title)
    if not job_key:
        raise ValueError("Need a URL (or company+title) to hide a search result")
    ts = now_iso()
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO search_hidden (job_key, title, company, url, ignored, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(job_key) DO UPDATE SET
                 title=excluded.title,
                 company=excluded.company,
                 url=excluded.url,
                 ignored=MAX(search_hidden.ignored, excluded.ignored)""",
            (job_key, title or "", company or "", url or "",
             1 if ignored else 0, ts),
        )
    return job_key


def restore(job_key: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM search_hidden WHERE job_key=?", (job_key,))


def set_ignored(job_key: str, ignored: bool = True) -> None:
    with get_connection() as conn:
        if ignored:
            conn.execute(
                "UPDATE search_hidden SET ignored=1 WHERE job_key=?", (job_key,))
        else:
            # Unignore → treat as dismissed (still hidden from active, restorable)
            conn.execute(
                "UPDATE search_hidden SET ignored=0 WHERE job_key=?", (job_key,))


def list_hidden(*, ignored: bool | None = None) -> list:
    """ignored=None → all; True → ignore list; False → dismissed only."""
    q = "SELECT * FROM search_hidden"
    args: tuple = ()
    if ignored is True:
        q += " WHERE ignored = 1"
    elif ignored is False:
        q += " WHERE ignored = 0"
    q += " ORDER BY created_at DESC, id DESC"
    with get_connection() as conn:
        return conn.execute(q, args).fetchall()


def hidden_key_set(*, ignored_only: bool = False) -> set[str]:
    """All URL/job keys that should be filtered out of active search results.

    ignored_only=False → dismissed + ignored (default active filter).
    """
    rows = list_hidden(ignored=True if ignored_only else None)
    if ignored_only:
        pass  # already filtered
    keys: set[str] = set()
    for r in rows:
        if ignored_only and not r["ignored"]:
            continue
        keys.add(r["job_key"])
        keys |= _keys_for(r["url"] or "", r["company"] or "", r["title"] or "")
    return keys


def is_hidden(url: str, company: str = "", title: str = "",
              *, key_set: set[str] | None = None) -> bool:
    ks = key_set if key_set is not None else hidden_key_set()
    if not ks:
        return False
    return bool(_keys_for(url, company, title) & ks)


def find_row(url: str, company: str = "", title: str = ""):
    """Return the search_hidden row matching this job, or None."""
    keys = _keys_for(url, company, title)
    if not keys:
        return None
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM search_hidden").fetchall()
    for r in rows:
        row_keys = {r["job_key"]} | _keys_for(
            r["url"] or "", r["company"] or "", r["title"] or "")
        if keys & row_keys:
            return r
    return None


def counts() -> dict[str, int]:
    with get_connection() as conn:
        dismissed = conn.execute(
            "SELECT COUNT(*) AS n FROM search_hidden WHERE ignored=0"
        ).fetchone()["n"]
        ignored = conn.execute(
            "SELECT COUNT(*) AS n FROM search_hidden WHERE ignored=1"
        ).fetchone()["n"]
    return {"dismissed": dismissed, "ignored": ignored}
