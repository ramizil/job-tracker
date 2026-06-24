"""SQLite connection helper and schema management."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .config import DB_PATH

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
"""


def now_iso() -> str:
    """UTC timestamp in ISO-8601 (seconds resolution)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


# Columns added after the initial release. Applied idempotently on init.
EXTRA_COLUMNS: dict[str, str] = {
    "ai_fit_level": "TEXT",        # YES / MAYBE / NO
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
}


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(applications)")}
    for name, decl in EXTRA_COLUMNS.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE applications ADD COLUMN {name} {decl}")


def init_db() -> None:
    """Create tables/indexes if missing, then apply column migrations."""
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


# Alias used by the web app at startup.
ensure_schema = init_db
