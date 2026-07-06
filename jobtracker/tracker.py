"""Application CRUD, status transitions (with history) and rejection logging."""
from __future__ import annotations

import sqlite3
from typing import Any

from .db import get_connection, now_iso
from .models import NEGATIVE_STATUSES, normalize_status
from .sources.base import JobResult


def add_application(
    *,
    company: str,
    title: str,
    location: str = "",
    source: str = "manual",
    url: str = "",
    description: str = "",
    salary: str = "",
    status: str = "saved",
    match_score: float | None = None,
    resume_version: str = "",
    contact: str = "",
    date_applied: str | None = None,
    notes: str = "",
) -> int:
    """Insert an application. Returns its id (or the existing id on duplicate)."""
    status = normalize_status(status)
    ts = now_iso()
    if status != "saved" and not date_applied:
        date_applied = ts
    with get_connection() as conn:
        try:
            cur = conn.execute(
                """
                INSERT INTO applications
                  (company, title, location, source, url, description, salary,
                   status, match_score, resume_version, contact, date_found,
                   date_applied, notes, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (company, title, location, source, url, description, salary,
                 status, match_score, resume_version, contact, ts,
                 date_applied, notes, ts, ts),
            )
            app_id = int(cur.lastrowid)
            conn.execute(
                """INSERT INTO status_history
                     (application_id, old_status, new_status, note, changed_at)
                   VALUES (?,?,?,?,?)""",
                (app_id, None, status, "created", ts),
            )
            return app_id
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT id FROM applications WHERE company=? AND title=? AND url=?",
                (company, title, url),
            ).fetchone()
            return int(row["id"]) if row else -1


def import_job_result(job: JobResult, match_score: float | None = None,
                      status: str = "saved") -> int:
    """Persist a search-result JobResult as an application row."""
    return add_application(
        company=job.company or "(unknown)",
        title=job.title or "(unknown)",
        location=job.location,
        source=job.source,
        url=job.url,
        description=job.description,
        salary=job.salary,
        status=status,
        match_score=match_score,
    )


def find_duplicates(title: str, company: str) -> list[sqlite3.Row]:
    """Existing applications with the same title AND company (case-insensitive,
    whitespace-trimmed). Used to warn when capturing a job you already have."""
    t = (title or "").strip().lower()
    c = (company or "").strip().lower()
    if not t or not c:
        return []
    with get_connection() as conn:
        return conn.execute(
            """SELECT * FROM applications
                 WHERE lower(trim(title)) = ? AND lower(trim(company)) = ?
                 ORDER BY updated_at DESC""",
            (t, c),
        ).fetchall()


def get_application(app_id: int) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM applications WHERE id=?", (app_id,)
        ).fetchone()


def list_applications(status: str | None = None,
                      order_by: str = "updated_at DESC") -> list[sqlite3.Row]:
    sql = "SELECT * FROM applications"
    params: tuple[Any, ...] = ()
    if status:
        sql += " WHERE status=?"
        params = (normalize_status(status),)
    sql += f" ORDER BY {order_by}"
    with get_connection() as conn:
        return conn.execute(sql, params).fetchall()


def get_history(app_id: int) -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM status_history WHERE application_id=? ORDER BY changed_at",
            (app_id,),
        ).fetchall()


def update_status(app_id: int, new_status: str, note: str = "") -> bool:
    """Transition status, recording history. Auto-stamps date_applied."""
    new_status = normalize_status(new_status)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT status, date_applied FROM applications WHERE id=?", (app_id,)
        ).fetchone()
        if not row:
            return False
        ts = now_iso()
        date_applied = row["date_applied"]
        if new_status != "saved" and not date_applied:
            date_applied = ts
        conn.execute(
            "UPDATE applications SET status=?, date_applied=?, updated_at=? WHERE id=?",
            (new_status, date_applied, ts, app_id),
        )
        conn.execute(
            """INSERT INTO status_history
                 (application_id, old_status, new_status, note, changed_at)
               VALUES (?,?,?,?,?)""",
            (app_id, row["status"], new_status, note, ts),
        )
        return True


def set_rejection(app_id: int, *, stage: str = "", reason: str = "",
                  note: str = "") -> bool:
    """Mark an application rejected and capture why (for later analysis)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT status FROM applications WHERE id=?", (app_id,)
        ).fetchone()
        if not row:
            return False
        ts = now_iso()
        conn.execute(
            """UPDATE applications
                 SET status='rejected', rejection_stage=?, rejection_reason=?,
                     rejection_note=?, rejection_date=?, updated_at=? WHERE id=?""",
            (stage, reason, note, ts, ts, app_id),
        )
        conn.execute(
            """INSERT INTO status_history
                 (application_id, old_status, new_status, note, changed_at)
               VALUES (?,?,?,?,?)""",
            (app_id, row["status"], "rejected",
             note or f"stage={stage}; reason={reason}", ts),
        )
        return True


def add_note(app_id: int, text: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT notes FROM applications WHERE id=?", (app_id,)
        ).fetchone()
        if not row:
            return False
        existing = row["notes"] or ""
        stamped = f"[{now_iso()}] {text}"
        merged = f"{existing}\n{stamped}".strip()
        conn.execute(
            "UPDATE applications SET notes=?, updated_at=? WHERE id=?",
            (merged, now_iso(), app_id),
        )
        return True


def set_ai_analysis(app_id: int, analysis: dict[str, Any]) -> bool:
    """Persist a Gemini fit-analysis result on the application."""
    import json
    score = _coerce_fit_score(analysis.get("fit_score"))
    with get_connection() as conn:
        cur = conn.execute(
            """UPDATE applications
                 SET ai_fit_level=?, ai_fit_score=?, ai_verdict=?, ai_analysis_json=?,
                     ai_analyzed_at=?, updated_at=? WHERE id=?""",
            (analysis.get("fit_level", ""), score, analysis.get("verdict", ""),
             json.dumps(analysis, ensure_ascii=False), now_iso(), now_iso(), app_id),
        )
        return cur.rowcount > 0


def _coerce_fit_score(value: Any) -> int | None:
    """Clamp an AI fit_score to a 0-100 int, or None if it isn't a number."""
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return None


def get_ai_analysis(app_id: int) -> dict[str, Any] | None:
    import json
    row = get_application(app_id)
    if not row or not row["ai_analysis_json"]:
        return None
    try:
        return json.loads(row["ai_analysis_json"])
    except (TypeError, json.JSONDecodeError):
        return None


def set_cover_letter(app_id: int, text: str) -> bool:
    """Persist (generated or edited) cover letter text."""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE applications SET cover_letter=?, cover_letter_at=?, updated_at=? WHERE id=?",
            (text, now_iso(), now_iso(), app_id),
        )
        return cur.rowcount > 0


def set_recruiter_note(app_id: int, text: str) -> bool:
    """Persist (generated or edited) recruiter outreach note."""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE applications SET recruiter_note=?, recruiter_note_at=?, updated_at=? WHERE id=?",
            (text, now_iso(), now_iso(), app_id),
        )
        return cur.rowcount > 0


def set_interview_prep(app_id: int, text: str) -> bool:
    """Persist (generated or edited) interview / test prep guide."""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE applications SET interview_prep=?, interview_prep_at=?, updated_at=? WHERE id=?",
            (text, now_iso(), now_iso(), app_id),
        )
        return cur.rowcount > 0


def set_mock_interview(app_id: int, text: str) -> bool:
    """Persist a generated mock-interview Q&A simulation (JSON string)."""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE applications SET mock_interview=?, mock_interview_at=?, updated_at=? WHERE id=?",
            (text, now_iso(), now_iso(), app_id),
        )
        return cur.rowcount > 0


def set_pitch(app_id: int, script: str, notes: str | None = None) -> bool:
    """Persist the per-job about-me pitch. ``notes`` (AI suggestions) is only
    updated when provided, so hand-edits to the script keep the last notes."""
    with get_connection() as conn:
        if notes is None:
            cur = conn.execute(
                "UPDATE applications SET pitch=?, pitch_at=?, updated_at=? WHERE id=?",
                (script, now_iso(), now_iso(), app_id),
            )
        else:
            cur = conn.execute(
                """UPDATE applications
                     SET pitch=?, pitch_notes=?, pitch_at=?, updated_at=? WHERE id=?""",
                (script, notes, now_iso(), now_iso(), app_id),
            )
        return cur.rowcount > 0


def set_feedback_request(app_id: int, text: str) -> bool:
    """Persist the polite 'why was I rejected' feedback letter."""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE applications SET feedback_request=?, feedback_request_at=?, updated_at=? WHERE id=?",
            (text, now_iso(), now_iso(), app_id),
        )
        return cur.rowcount > 0


def set_company_brief(app_id: int, data: str | dict[str, Any]) -> bool:
    """Persist AI web-research about the company (JSON dict or legacy Markdown)."""
    import json
    payload = (json.dumps(data, ensure_ascii=False)
               if isinstance(data, dict) else data)
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE applications SET company_brief=?, company_brief_at=?, updated_at=? WHERE id=?",
            (payload, now_iso(), now_iso(), app_id),
        )
        return cur.rowcount > 0


def get_company_brief(app_id: int) -> dict[str, Any] | None:
    """Return company brief as {en, he, sources?, grounded?} or None."""
    import json
    row = get_application(app_id)
    if not row or not row["company_brief"]:
        return None
    raw = row["company_brief"]
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and ("en" in data or "he" in data):
            return data
    except (TypeError, ValueError):
        pass
    return {"en": raw, "he": "", "sources": [], "grounded": None}


def set_salary_research(app_id: int, data: dict[str, Any]) -> bool:
    """Persist AI expected-salary research (stored as JSON)."""
    import json
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE applications SET salary_research=?, salary_research_at=?, updated_at=? WHERE id=?",
            (json.dumps(data, ensure_ascii=False), now_iso(), now_iso(), app_id),
        )
        return cur.rowcount > 0


def get_salary_research(app_id: int) -> dict[str, Any] | None:
    import json
    row = get_application(app_id)
    if not row or not row["salary_research"]:
        return None
    try:
        data = json.loads(row["salary_research"])
        return data if isinstance(data, dict) else None
    except (TypeError, ValueError):
        return None


def mark_tailored(app_id: int) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE applications SET tailored_at=?, updated_at=? WHERE id=?",
            (now_iso(), now_iso(), app_id),
        )
        return cur.rowcount > 0


def delete_application(app_id: int) -> bool:
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM applications WHERE id=?", (app_id,))
        return cur.rowcount > 0


def is_negative(status: str) -> bool:
    return status in NEGATIVE_STATUSES
