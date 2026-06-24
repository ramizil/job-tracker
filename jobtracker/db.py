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


def init_db() -> None:
    """Create tables/indexes if they do not exist."""
    with get_connection() as conn:
        conn.executescript(SCHEMA)
