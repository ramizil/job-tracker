"""Aggregate analytics: pipeline funnel, rejection analysis, source stats."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from .db import get_connection
from .models import ACTIVE_STATUSES, NEGATIVE_STATUSES, STATUSES


def _age_days(ts: str | None) -> int | None:
    """Whole days between an ISO timestamp and now (UTC). None if unparseable."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).days


def saved_reminders(stale_days: int = 3) -> dict[str, Any]:
    """Saved jobs that were never applied to or rejected — a to-handle list.

    ``stale`` counts the ones sitting untouched for at least ``stale_days`` days.
    Sorted oldest-first so the most overdue bubble to the top.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT id, company, title, created_at, updated_at
               FROM applications WHERE status='saved'
               ORDER BY COALESCE(updated_at, created_at) ASC"""
        ).fetchall()
    items: list[dict[str, Any]] = []
    for r in rows:
        days = _age_days(r["updated_at"] or r["created_at"])
        items.append({
            "id": r["id"], "company": r["company"], "title": r["title"],
            "days": days, "stale": days is not None and days >= stale_days,
        })
    return {
        "count": len(items),
        "stale": sum(1 for it in items if it["stale"]),
        "stale_days": stale_days,
        "items": items,
    }


def _count(sql: str, params: tuple = ()) -> list[tuple[str, int]]:
    with get_connection() as conn:
        return [(r[0], r[1]) for r in conn.execute(sql, params).fetchall()]


def funnel() -> dict[str, int]:
    """Count of applications per status, in pipeline order."""
    counts = dict(_count(
        "SELECT status, COUNT(*) FROM applications GROUP BY status"
    ))
    return {s: counts.get(s, 0) for s in STATUSES}


def totals() -> dict[str, Any]:
    f = funnel()
    total = sum(f.values())
    # "Applied+" = real applications: everything past "saved", except the ones
    # the candidate withdrew (those weren't pursued, so they don't count).
    applied = sum(v for k, v in f.items() if k not in ("saved", "withdrawn"))
    rejected = f.get("rejected", 0)
    interviews = f.get("interview", 0) + f.get("offer", 0) + f.get("accepted", 0)
    active = sum(v for k, v in f.items() if k in ACTIVE_STATUSES)
    negative = sum(v for k, v in f.items() if k in NEGATIVE_STATUSES)
    # response rate = anyone who moved beyond "applied" / total applied
    responded = sum(f.get(s, 0) for s in ("screening", "interview", "offer", "accepted", "rejected"))
    return {
        "total": total,
        "saved": f.get("saved", 0),
        "applied_or_beyond": applied,
        "active": active,
        "interviews_reached": interviews,
        "rejected": rejected,
        "negative": negative,
        "response_rate_pct": round(responded / applied * 100, 1) if applied else 0.0,
        "interview_rate_pct": round(interviews / applied * 100, 1) if applied else 0.0,
        "rejection_rate_pct": round(rejected / applied * 100, 1) if applied else 0.0,
    }


def rejection_by_stage() -> list[tuple[str, int]]:
    return _count(
        """SELECT COALESCE(NULLIF(rejection_stage,''),'(unspecified)'), COUNT(*)
           FROM applications WHERE status='rejected'
           GROUP BY rejection_stage ORDER BY COUNT(*) DESC"""
    )


def rejection_by_reason() -> list[tuple[str, int]]:
    return _count(
        """SELECT COALESCE(NULLIF(rejection_reason,''),'(unspecified)'), COUNT(*)
           FROM applications WHERE status='rejected'
           GROUP BY rejection_reason ORDER BY COUNT(*) DESC"""
    )


def source_stats() -> list[dict[str, Any]]:
    """Per-source effectiveness: total, interviews, rejections, avg match."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT source,
                   COUNT(*)                                              AS total,
                   SUM(CASE WHEN status IN ('interview','offer','accepted')
                            THEN 1 ELSE 0 END)                           AS interviews,
                   SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END)    AS rejected,
                   AVG(match_score)                                      AS avg_match
            FROM applications
            GROUP BY source ORDER BY total DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def rejection_baseline() -> dict[str, Any]:
    """Pipeline context for the AI rejection analysis (and its dashboard page).

    Compares AI fit scores of rejected vs. advancing applications so the
    overall analysis can judge whether rejections track fit or something else.
    """
    with get_connection() as conn:
        fit_rej = conn.execute(
            "SELECT AVG(ai_fit_score) FROM applications "
            "WHERE status='rejected' AND ai_fit_score IS NOT NULL").fetchone()[0]
        fit_adv = conn.execute(
            """SELECT AVG(ai_fit_score) FROM applications
               WHERE status IN ('screening','interview','offer','accepted')
                 AND ai_fit_score IS NOT NULL""").fetchone()[0]
        fit_all = conn.execute(
            "SELECT AVG(ai_fit_score) FROM applications "
            "WHERE ai_fit_score IS NOT NULL").fetchone()[0]
    return {
        "totals": totals(),
        "by_stage": rejection_by_stage(),
        "by_reason": rejection_by_reason(),
        "avg_fit_rejected": round(fit_rej, 1) if fit_rej is not None else None,
        "avg_fit_advanced": round(fit_adv, 1) if fit_adv is not None else None,
        "avg_fit_overall": round(fit_all, 1) if fit_all is not None else None,
    }


def match_score_insight() -> dict[str, float | None]:
    """Compare average match score of rejected vs interview-reaching apps.

    A large gap suggests your CV/keyword fit correlates with progress.
    """
    with get_connection() as conn:
        rej = conn.execute(
            "SELECT AVG(match_score) FROM applications WHERE status='rejected' AND match_score IS NOT NULL"
        ).fetchone()[0]
        adv = conn.execute(
            """SELECT AVG(match_score) FROM applications
               WHERE status IN ('interview','offer','accepted') AND match_score IS NOT NULL"""
        ).fetchone()[0]
    return {
        "avg_match_rejected": round(rej, 1) if rej is not None else None,
        "avg_match_advanced": round(adv, 1) if adv is not None else None,
    }
