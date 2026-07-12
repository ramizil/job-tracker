"""SQLite connection helper and schema management."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company         TEXT NOT NULL,
    title           TEXT NOT NULL,
    location        TEXT,
    source          TEXT,                 -- linkedin / jsearch / jooble / manual ...
    url             TEXT,
    description     TEXT,
    salary          TEXT,
    status          TEXT NOT NULL DEFAULT 'saved',
    match_score     REAL,                 -- 0-100 vs resume profile
    resume_version  TEXT,                 -- which CV variant you sent
    contact         TEXT,                 -- recruiter / referral
    date_found      TEXT,
    date_applied    TEXT,
    rejection_stage TEXT,
    rejection_reason TEXT,
    rejection_date  TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(company, title, url)
);

CREATE TABLE IF NOT EXISTS status_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL,
    old_status     TEXT,
    new_status     TEXT NOT NULL,
    note           TEXT,
    changed_at     TEXT NOT NULL,
    FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_app_status ON applications(status);
CREATE INDEX IF NOT EXISTS idx_hist_app ON status_history(application_id);

-- Job postings extracted from Gmail job-alert emails (see gmail_alerts.py).
CREATE TABLE IF NOT EXISTS job_alerts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key        TEXT UNIQUE,          -- LinkedIn job id (stable dedupe key)
    title          TEXT NOT NULL,
    company        TEXT,
    location       TEXT,
    url            TEXT,
    gmail_id       TEXT,                 -- source email (Gmail message id)
    alert_at       TEXT,                 -- when the alert email arrived
    matched_app_id INTEGER,              -- application this alert matches (if any)
    dismissed      INTEGER DEFAULT 0,
    seen           INTEGER DEFAULT 0,    -- acknowledged in the UI (badge reset)
    times_seen     INTEGER DEFAULT 1,    -- how many alert emails contained this job
    last_alert_at  TEXT,                 -- most recent email that mentioned it
    ignored        INTEGER DEFAULT 0,    -- ignore list: hidden, never notifies again
    created_at     TEXT NOT NULL
);

-- Alert emails already parsed, so a fetch never re-processes them.
CREATE TABLE IF NOT EXISTS alert_emails (
    gmail_id   TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    """UTC timestamp in ISO-8601 (seconds resolution)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_connection() -> sqlite3.Connection:
    # Resolved at call time so switching profiles changes the DB immediately.
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


# Columns added after the initial release. Applied idempotently on init.
EXTRA_COLUMNS: dict[str, str] = {
    "ai_fit_level": "TEXT",        # YES / MAYBE / NO
    "ai_fit_score": "INTEGER",     # 0-100 AI fit score (shown as a % badge)
    "ai_verdict": "TEXT",          # one-line verdict
    "ai_analysis_json": "TEXT",    # full structured analysis (JSON)
    "ai_analyzed_at": "TEXT",
    "tailored_at": "TEXT",         # when a tailored resume was generated
    "cover_letter": "TEXT",        # generated/edited cover letter
    "cover_letter_at": "TEXT",
    "recruiter_note": "TEXT",      # short outreach message to the recruiter
    "recruiter_note_at": "TEXT",
    "interview_prep": "TEXT",      # interview / test prep guide (Markdown)
    "interview_prep_at": "TEXT",
    "mock_interview": "TEXT",      # mock interview Q&A simulation (JSON)
    "mock_interview_at": "TEXT",
    "qa_exercise": "TEXT",         # practice QA testing-scenario exercise (Markdown)
    "qa_exercise_at": "TEXT",
    "pitch": "TEXT",               # per-job tailored about-me pitch (script text)
    "pitch_notes": "TEXT",         # latest AI tailoring suggestions for the pitch
    "pitch_at": "TEXT",
    "company_brief": "TEXT",       # AI web research about the company (Markdown)
    "company_brief_at": "TEXT",
    "salary_research": "TEXT",     # AI expected-salary research (JSON)
    "salary_research_at": "TEXT",
    "ats_check": "TEXT",           # ATS keyword screen of resume vs job (JSON)
    "ats_check_at": "TEXT",
    "rejection_analysis": "TEXT",  # AI post-mortem of why this app was rejected (JSON)
    "rejection_analysis_at": "TEXT",
    "feedback_request": "TEXT",    # polite letter asking why I was rejected
    "feedback_request_at": "TEXT",
    "rejection_note": "TEXT",      # free-text note captured on rejection
    "starred": "INTEGER DEFAULT 0",  # preferred / favourite job flag
}


def _migrate(conn: sqlite3.Connection) -> None:
    # Columns added to job_alerts after the alerts feature shipped.
    alert_cols = {r["name"] for r in conn.execute("PRAGMA table_info(job_alerts)")}
    if alert_cols:
        if "seen" not in alert_cols:
            # Pre-existing alerts are treated as already acknowledged.
            conn.execute("ALTER TABLE job_alerts ADD COLUMN seen INTEGER DEFAULT 0")
            conn.execute("UPDATE job_alerts SET seen = 1")
        if "times_seen" not in alert_cols:
            conn.execute(
                "ALTER TABLE job_alerts ADD COLUMN times_seen INTEGER DEFAULT 1")
        if "last_alert_at" not in alert_cols:
            conn.execute("ALTER TABLE job_alerts ADD COLUMN last_alert_at TEXT")
            conn.execute("UPDATE job_alerts SET last_alert_at = alert_at")
        if "ignored" not in alert_cols:
            conn.execute(
                "ALTER TABLE job_alerts ADD COLUMN ignored INTEGER DEFAULT 0")

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(applications)")}
    added = []
    for name, decl in EXTRA_COLUMNS.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE applications ADD COLUMN {name} {decl}")
            added.append(name)

    # Backfill the AI fit score from the stored analysis JSON for rows analysed
    # before this column existed (best-effort; needs SQLite's JSON1 extension).
    if "ai_fit_score" in added:
        try:
            conn.execute(
                """UPDATE applications
                      SET ai_fit_score = CAST(json_extract(ai_analysis_json,
                                                            '$.fit_score') AS INTEGER)
                    WHERE ai_fit_score IS NULL
                      AND ai_analysis_json IS NOT NULL
                      AND json_extract(ai_analysis_json, '$.fit_score') IS NOT NULL"""
            )
        except sqlite3.Error:
            pass  # JSON1 unavailable — scores fill in as jobs are re-analysed

    # Backfill rejection notes recorded before this column existed: they only
    # lived in status_history. Skip the auto-generated "stage=..." fallbacks,
    # which just duplicate the rejection_stage/rejection_reason columns.
    if "rejection_note" in added:
        conn.execute(
            """UPDATE applications
                  SET rejection_note = (
                      SELECT note FROM status_history h
                       WHERE h.application_id = applications.id
                         AND h.new_status = 'rejected'
                         AND h.note != ''
                         AND h.note NOT LIKE 'stage=%'
                       ORDER BY h.changed_at DESC LIMIT 1)
                WHERE status = 'rejected'"""
        )


def init_db() -> None:
    """Create tables/indexes if missing, then apply column migrations."""
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


# Alias used by the web app at startup.
ensure_schema = init_db
