"""Application CRUD, status transitions (with history) and rejection logging."""
from __future__ import annotations

import re
import sqlite3
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import parse_qs, urlparse

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
    resume_id: int | None = None,
    contact: str = "",
    date_applied: str | None = None,
    notes: str = "",
) -> int:
    """Insert an application. Returns its id (or the existing id on duplicate)."""
    status = normalize_status(status)
    ts = now_iso()
    if status != "saved" and not date_applied:
        date_applied = ts
    # Keep resume_version in sync with the library label when a resume_id is set.
    if resume_id and not resume_version:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT label FROM resumes WHERE id=?", (resume_id,)
            ).fetchone()
            if row:
                resume_version = row["label"] or ""
    with get_connection() as conn:
        try:
            cur = conn.execute(
                """
                INSERT INTO applications
                  (company, title, location, source, url, description, salary,
                   status, match_score, resume_version, resume_id, contact,
                   date_found, date_applied, notes, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (company, title, location, source, url, description, salary,
                 status, match_score, resume_version, resume_id, contact, ts,
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


def _norm_match_text(s: str) -> str:
    return re.sub(r"[^a-z0-9\u0590-\u05ff ]+", " ", (s or "").lower()).strip()


def _text_sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _norm_job_url(url: str) -> str:
    """Host + path, plus significant query ids (e.g. AllJobs JobID).

    Query strings are usually noise (utm_*), but boards that put the job id
    only in ``?JobID=`` must keep it — otherwise every AllJobs URL collapses
    to the same key and search falsely marks every hit as already applied.
    """
    if not url:
        return ""
    p = urlparse(url.strip())
    host = (p.netloc or "").lower().removeprefix("www.")
    path = (p.path or "").rstrip("/")
    base = f"{host}{path}"
    for k, vals in parse_qs(p.query or "").items():
        if k.lower() in ("jobid", "job_id") and vals and vals[0]:
            return f"{base}?jobid={vals[0]}"
    return base


def _job_url_keys(url: str) -> set[str]:
    """Stable identifiers for cross-matching search hits to saved applications."""
    keys: set[str] = set()
    nu = _norm_job_url(url)
    if nu:
        keys.add(nu)
    patterns = [
        (r"linkedin\.com/(?:comm/)?jobs/view/(?:[^\s/?#]*?-)?(\d{6,})", "linkedin"),
        (r"greenhouse\.io/(?:[^/]+/)?jobs/(\d+)", "greenhouse"),
        (r"lever\.co/[^/]+/([0-9a-f-]{36})", "lever"),
        (r"comeet\.com/jobs/[^/]+/([^/?#]+)", "comeet"),
        (r"smartrecruiters\.com/[^/]+/(\d+)", "smartrecruiters"),
        (r"alljobs\.co\.il/[^?\s]*[?&]JobID=(\d+)", "alljobs"),
        (r"drushim\.co\.il/job/(\d+)", "drushim"),
        (r"matrix\.co\.il/jobs/משרה/([^/?#]+)", "matrix"),
        (r"matrix\.co\.il/jobs/%d7%9e%d7%a9%d7%a8%d7%94/([^/?#]+)", "matrix"),
        (r"indeed\.[^/\s\"']+/[^?\s\"']*[?&](?:jk|vjk)=([a-f0-9]{10,20})", "indeed"),
    ]
    for pat, label in patterns:
        m = re.search(pat, url or "", re.I)
        if m:
            keys.add(f"{label}:{m.group(1).lower()}")
    return keys


def _company_matches_search(needle: str, haystack: str) -> bool:
    a = _norm_match_text(needle)
    b = _norm_match_text(haystack)
    if not a or not b:
        return False
    return (a == b or a in b or b in a or _text_sim(a, b) >= 0.85)


def _title_matches_search(needle: str, haystack: str) -> bool:
    a = _norm_match_text(needle)
    b = _norm_match_text(haystack)
    if not a or not b:
        return False
    return a == b or a in b or b in a or _text_sim(a, b) >= 0.55


def match_job_to_application(*, url: str = "", company: str = "", title: str = "",
                             apps: list[sqlite3.Row] | None = None,
                             url_index: dict[str, sqlite3.Row] | None = None
                             ) -> sqlite3.Row | None:
    """Best application match for a search hit: URL id first, then fuzzy title+company."""
    if apps is None:
        with get_connection() as conn:
            apps = conn.execute(
                "SELECT id, company, title, url, status FROM applications"
            ).fetchall()
    if url_index is None:
        url_index = {}
        for app in apps:
            for key in _job_url_keys(app["url"] or ""):
                url_index.setdefault(key, app)

    for key in _job_url_keys(url):
        hit = url_index.get(key)
        if hit:
            return hit

    j_company = _norm_match_text(company)
    j_title = _norm_match_text(title)
    if not j_company or not j_title:
        return None
    # Staffing-board placeholders are not real employers — never fuzzy-match
    # on them (would glue every SQLink/AllJobs hit to one application).
    if j_company in {"alljobs", "sqlink", "matrix", "unknown", "via sqlink"}:
        return None
    for app in apps:
        if (_company_matches_search(company, app["company"])
                and _title_matches_search(title, app["title"])):
            return app
    return None


def enrich_search_results(results: list[dict]) -> list[dict]:
    """Add app_id + app_status ('new' or saved status) to search result dicts."""
    if not results:
        return results
    with get_connection() as conn:
        apps = conn.execute(
            "SELECT id, company, title, url, status FROM applications"
        ).fetchall()
    url_index: dict[str, sqlite3.Row] = {}
    for app in apps:
        for key in _job_url_keys(app["url"] or ""):
            url_index.setdefault(key, app)
    enriched: list[dict] = []
    for item in results:
        job = item["job"]
        hit = match_job_to_application(
            url=job.url, company=job.company, title=job.title,
            apps=apps, url_index=url_index,
        )
        row = dict(item)
        if hit:
            row["app_id"] = int(hit["id"])
            row["app_status"] = hit["status"]
        else:
            row["app_id"] = None
            row["app_status"] = "new"
        enriched.append(row)
    return enriched


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


def _compact_status_path(path: list[str], current: str = "") -> list[str]:
    """Collapse a status trail for compact UI (e.g. applied → rejected)."""
    cleaned: list[str] = []
    for s in path:
        s = (s or "").strip()
        if not s or s == "saved":
            continue
        if not cleaned or cleaned[-1] != s:
            cleaned.append(s)
    cur = (current or "").strip()
    if cur and cur != "saved":
        if not cleaned:
            cleaned = [cur]
        elif cleaned[-1] != cur:
            cleaned.append(cur)
    elif not cleaned and cur:
        cleaned = [cur]
    return cleaned


def status_paths_for_apps(app_ids: list[int]) -> dict[int, list[str]]:
    """Ordered status lifecycle per application (e.g. ['applied', 'rejected']).

    Built from status_history when present; otherwise just the current status.
    "saved" and consecutive duplicates are dropped for a compact trail.
    """
    ids = sorted({int(i) for i in app_ids if i is not None})
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    with get_connection() as conn:
        apps = {
            int(r["id"]): r["status"]
            for r in conn.execute(
                f"SELECT id, status FROM applications WHERE id IN ({placeholders})",
                ids,
            )
        }
        hist = conn.execute(
            f"""SELECT application_id, old_status, new_status, changed_at
                  FROM status_history
                 WHERE application_id IN ({placeholders})
                 ORDER BY changed_at ASC, id ASC""",
            ids,
        ).fetchall()

    raw: dict[int, list[str]] = {i: [] for i in ids}
    for h in hist:
        aid = int(h["application_id"])
        path = raw[aid]
        if not path and h["old_status"]:
            path.append(h["old_status"])
        new = h["new_status"]
        if new and (not path or path[-1] != new):
            path.append(new)
    return {
        aid: _compact_status_path(raw.get(aid, []), apps.get(aid, ""))
        for aid in ids if aid in apps
    }


def format_contact(
    *,
    name: str = "",
    role: str = "",
    email: str = "",
    phone: str = "",
    other: str = "",
) -> str:
    """Build a readable contact block from structured fields."""
    lines: list[str] = []
    name = (name or "").strip()
    role = (role or "").strip()
    email = (email or "").strip()
    phone = (phone or "").strip()
    other = (other or "").strip()
    if name and role:
        lines.append(f"{name} ({role})")
    elif name:
        lines.append(name)
    elif role:
        lines.append(role)
    if email:
        lines.append(email)
    if phone:
        lines.append(phone)
    if other:
        lines.append(other)
    return "\n".join(lines)


def set_contact(app_id: int, contact: str) -> bool:
    """Save recruiter / interviewer contact details for an application."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM applications WHERE id=?", (app_id,)
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE applications SET contact=?, updated_at=? WHERE id=?",
            ((contact or "").strip(), now_iso(), app_id),
        )
        return True


def update_status(app_id: int, new_status: str, note: str = "",
                  contact: str | None = None) -> bool:
    """Transition status, recording history. Auto-stamps date_applied.

    If ``contact`` is a non-empty string, it is saved on the application
    (typical when moving applied → screening and capturing the recruiter).
    """
    new_status = normalize_status(new_status)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT status, date_applied FROM applications WHERE id=?", (app_id,)
        ).fetchone()
        if not row:
            return False
        ts = now_iso()
        date_applied = row["date_applied"]
        # Re-apply refreshes the applied date; first move past "saved" stamps it.
        if new_status == "reapplied":
            date_applied = ts
        elif new_status != "saved" and not date_applied:
            date_applied = ts
        if contact is not None and str(contact).strip():
            conn.execute(
                "UPDATE applications SET status=?, date_applied=?, contact=?, "
                "updated_at=? WHERE id=?",
                (new_status, date_applied, str(contact).strip(), ts, app_id),
            )
        else:
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


def mark_reapplied(
    app_id: int,
    *,
    resume_id: int | None = None,
    note: str = "",
    description: str = "",
    url: str = "",
) -> bool:
    """Mark an existing application as reapplied (ghosted / role reposted).

    Keeps the same application row, stamps a fresh ``date_applied``, optionally
    refreshes description/URL from a new paste, and links a new resume while
    archiving the previous one in ``application_resume_history``.
    """
    from . import resumes as resumes_mod

    row = get_application(app_id)
    if not row:
        return False
    hist_note = (note or "").strip() or "reapplied (job resurfaced)"
    if not update_status(app_id, "reapplied", hist_note):
        return False
    with get_connection() as conn:
        ts = now_iso()
        fields: list[str] = []
        vals: list[Any] = []
        if description and description.strip():
            fields.append("description=?")
            vals.append(description.strip())
        if url and url.strip():
            fields.append("url=?")
            vals.append(url.strip())
        if fields:
            fields.append("updated_at=?")
            vals.extend([ts, app_id])
            conn.execute(
                f"UPDATE applications SET {', '.join(fields)} WHERE id=?",
                vals,
            )
    if resume_id is not None:
        resumes_mod.attach_to_application(
            app_id, resume_id, note="reapplied — previous CV archived")
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


def toggle_star(app_id: int) -> int | None:
    """Flip the preferred-job star. Returns the new value (or None if missing)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT starred FROM applications WHERE id=?", (app_id,)
        ).fetchone()
        if not row:
            return None
        new = 0 if row["starred"] else 1
        conn.execute("UPDATE applications SET starred=?, updated_at=? WHERE id=?",
                     (new, now_iso(), app_id))
        return new


def set_star(app_id: int, starred: bool) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE applications SET starred=?, updated_at=? WHERE id=?",
                     (1 if starred else 0, now_iso(), app_id))


def auto_ghost_stale(days: int = 30) -> list[sqlite3.Row]:
    """Mark applied/reapplied rows with no movement for `days`+ days as ghosted.

    Uses the application date (falling back to the last update) so a job that
    has sat waiting for over a month without any response is closed out
    automatically. Each transition is recorded in the status history.
    """
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="seconds")
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT id, company, title, status FROM applications
                WHERE status IN ('applied', 'reapplied')
                  AND COALESCE(date_applied, updated_at, created_at) < ?""",
            (cutoff,)).fetchall()
        ts = now_iso()
        for r in rows:
            conn.execute(
                "UPDATE applications SET status='ghosted', updated_at=? WHERE id=?",
                (ts, r["id"]))
            conn.execute(
                """INSERT INTO status_history
                     (application_id, old_status, new_status, note, changed_at)
                   VALUES (?,?,?,?,?)""",
                (r["id"], r["status"], "ghosted",
                 f"auto-ghosted after {days}+ days with no response", ts))
        return rows


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


def set_description(app_id: int, description: str,
                    *, rescore: bool = True) -> bool:
    """Replace the job description (e.g. after a bad Alerts capture).

    When ``rescore`` is True, also refresh the keyword match_score from the
    new text so Overview / AI inputs stay consistent.
    """
    text = description or ""
    score = None
    if rescore:
        from .matcher import score_job
        row = get_application(app_id)
        if not row:
            return False
        score = score_job(row["title"] or "", text).score
    with get_connection() as conn:
        if score is None:
            cur = conn.execute(
                "UPDATE applications SET description=?, updated_at=? WHERE id=?",
                (text, now_iso(), app_id),
            )
        else:
            cur = conn.execute(
                """UPDATE applications
                      SET description=?, match_score=?, updated_at=? WHERE id=?""",
                (text, score, now_iso(), app_id),
            )
        return cur.rowcount > 0


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


def set_qa_exercise(app_id: int, text: str) -> bool:
    """Persist a (generated or edited) QA testing-scenario exercise (Markdown)."""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE applications SET qa_exercise=?, qa_exercise_at=?, updated_at=? WHERE id=?",
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


def set_rejection_analysis(app_id: int, data: dict[str, Any]) -> bool:
    """Persist the AI rejection post-mortem (stored as JSON, cached forever)."""
    import json
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE applications SET rejection_analysis=?, rejection_analysis_at=?, "
            "updated_at=? WHERE id=?",
            (json.dumps(data, ensure_ascii=False), now_iso(), now_iso(), app_id),
        )
        return cur.rowcount > 0


def get_rejection_analysis(app_id: int) -> dict[str, Any] | None:
    import json
    row = get_application(app_id)
    if not row or not row["rejection_analysis"]:
        return None
    try:
        data = json.loads(row["rejection_analysis"])
        return data if isinstance(data, dict) else None
    except (TypeError, ValueError):
        return None


def set_ats_check(app_id: int, data: dict[str, Any]) -> bool:
    """Persist the ATS keyword-screen result (stored as JSON)."""
    import json
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE applications SET ats_check=?, ats_check_at=?, updated_at=? WHERE id=?",
            (json.dumps(data, ensure_ascii=False), now_iso(), now_iso(), app_id),
        )
        return cur.rowcount > 0


def get_ats_check(app_id: int) -> dict[str, Any] | None:
    import json
    row = get_application(app_id)
    if not row or not row["ats_check"]:
        return None
    try:
        data = json.loads(row["ats_check"])
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
