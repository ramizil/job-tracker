"""Central configuration: filesystem paths and environment-backed settings.

Settings live in the project-root ``.env`` file. They can be edited by hand or
from the web UI (Settings page). ``reload()`` re-reads ``.env`` at runtime so
changes take effect without restarting the server.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Project root = the folder that contains the `jobtracker` package.
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"

# Local, git-ignored data directory (holds the SQLite DB and generated profile).
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DB_PATH = Path(os.getenv("JOBTRACKER_DB", DATA_DIR / "jobtracker.db"))
PROFILE_PATH = DATA_DIR / "profile.yaml"

# Personal "about me" pitch / interview script (the global base version).
PITCH_PATH = DATA_DIR / "pitch.md"

# Resume built by the conversational Resume Builder (AI-generated, English).
BUILT_RESUME_PATH = DATA_DIR / "built_resume.html"

# Tailored resumes (AI-generated) are written here.
TAILORED_DIR = DATA_DIR / "tailored"
TAILORED_DIR.mkdir(exist_ok=True)

# Local working tree used to mirror the data to a private GitHub repo
# (see jobtracker/gitbackup.py). Kept inside the git-ignored data dir.
GIT_BACKUP_DIR = DATA_DIR / ".git-backup"

DEFAULT_RESUME = BASE_DIR / "sample_resume.html"


def _default_backup_dir() -> Path:
    """Prefer a cloud-synced folder (auto-synced + private), else a local folder.

    Windows: OneDrive. macOS: iCloud Drive or a OneDrive folder under the home
    directory. Falls back to a local ``backups/`` folder if none are found.
    """
    for var in ("OneDriveCommercial", "OneDrive", "OneDriveConsumer"):
        root = os.getenv(var)
        if root and Path(root).exists():
            return Path(root) / "JobTrackerBackups"

    if sys.platform == "darwin":
        home = Path.home()
        candidates = [
            home / "Library" / "Mobile Documents" / "com~apple~CloudDocs",  # iCloud Drive
            home / "OneDrive",
        ]
        for root in candidates:
            if root.exists():
                return root / "JobTrackerBackups"

    return BASE_DIR / "backups"

# Keys that the Settings UI manages (editable). Maps env var -> description.
EDITABLE_KEYS: dict[str, str] = {
    "RAPIDAPI_KEY": "JSearch (RapidAPI) - includes LinkedIn listings, Israel",
    "JOOBLE_API_KEY": "Jooble - free job search, Israel",
    "ADZUNA_APP_ID": "Adzuna app id (optional, no Israel)",
    "ADZUNA_APP_KEY": "Adzuna app key (optional)",
    "AI_PROVIDER": "AI provider: gemini | openai | anthropic | cursor",
    "GEMINI_API_KEY": "Gemini AI key (fit analysis + resume tailoring)",
    "GEMINI_MODEL": "Gemini model (default gemini-2.5-flash)",
    "OPENAI_API_KEY": "OpenAI API key (GPT models)",
    "OPENAI_MODEL": "OpenAI model (default gpt-4o-mini)",
    "ANTHROPIC_API_KEY": "Anthropic API key (Claude models)",
    "ANTHROPIC_MODEL": "Anthropic model (default claude-3-5-sonnet-latest)",
    "CURSOR_API_KEY": "Cursor API key (crsr_...) - used via a local OpenAI-compatible proxy",
    "CURSOR_MODEL": "Cursor model (default auto)",
    "CURSOR_BASE_URL": "Cursor proxy base URL (default http://localhost:8080/v1)",
    "RESUME_PATH": "Path to your resume (HTML, PDF, Word .docx, or text)",
    "BACKUP_DIR": "Folder for backups (a OneDrive path = auto-synced & private)",
    "DATA_BACKUP_REMOTE": "Private git repo URL to mirror your data to (GitHub backup)",
}

# Module-level settings (re-assigned by reload()).
RAPIDAPI_KEY = ""
JOOBLE_API_KEY = ""
ADZUNA_APP_ID = ""
ADZUNA_APP_KEY = ""
AI_PROVIDER = "gemini"
GEMINI_API_KEY = ""
GEMINI_MODEL = "gemini-2.5-flash"
OPENAI_API_KEY = ""
OPENAI_MODEL = "gpt-4o-mini"
ANTHROPIC_API_KEY = ""
ANTHROPIC_MODEL = "claude-3-5-sonnet-latest"
CURSOR_API_KEY = ""
CURSOR_MODEL = "auto"
CURSOR_BASE_URL = "http://localhost:8080/v1"
RESUME_PATH = DEFAULT_RESUME
BACKUP_DIR = _default_backup_dir()
DATA_BACKUP_REMOTE = ""


def reload() -> None:
    """(Re)load values from .env into this module's globals."""
    global RAPIDAPI_KEY, JOOBLE_API_KEY, ADZUNA_APP_ID, ADZUNA_APP_KEY
    global AI_PROVIDER, GEMINI_API_KEY, GEMINI_MODEL
    global OPENAI_API_KEY, OPENAI_MODEL, ANTHROPIC_API_KEY, ANTHROPIC_MODEL
    global CURSOR_API_KEY, CURSOR_MODEL, CURSOR_BASE_URL
    global RESUME_PATH, BACKUP_DIR, DATA_BACKUP_REMOTE

    load_dotenv(ENV_PATH, override=True)
    RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "").strip()
    JOOBLE_API_KEY = os.getenv("JOOBLE_API_KEY", "").strip()
    ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID", "").strip()
    ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY", "").strip()
    AI_PROVIDER = (os.getenv("AI_PROVIDER", "gemini").strip().lower() or "gemini")
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
    ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest").strip()
    CURSOR_API_KEY = os.getenv("CURSOR_API_KEY", "").strip()
    CURSOR_MODEL = os.getenv("CURSOR_MODEL", "auto").strip()
    CURSOR_BASE_URL = os.getenv("CURSOR_BASE_URL", "http://localhost:8080/v1").strip()
    RESUME_PATH = Path(os.getenv("RESUME_PATH") or DEFAULT_RESUME)
    BACKUP_DIR = Path(os.getenv("BACKUP_DIR") or _default_backup_dir())
    DATA_BACKUP_REMOTE = os.getenv("DATA_BACKUP_REMOTE", "").strip()


def current_settings() -> dict[str, str]:
    """Current values for the editable keys (for display in the UI)."""
    return {
        "RAPIDAPI_KEY": RAPIDAPI_KEY,
        "JOOBLE_API_KEY": JOOBLE_API_KEY,
        "ADZUNA_APP_ID": ADZUNA_APP_ID,
        "ADZUNA_APP_KEY": ADZUNA_APP_KEY,
        "AI_PROVIDER": AI_PROVIDER,
        "GEMINI_API_KEY": GEMINI_API_KEY,
        "GEMINI_MODEL": GEMINI_MODEL,
        "OPENAI_API_KEY": OPENAI_API_KEY,
        "OPENAI_MODEL": OPENAI_MODEL,
        "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
        "ANTHROPIC_MODEL": ANTHROPIC_MODEL,
        "CURSOR_API_KEY": CURSOR_API_KEY,
        "CURSOR_MODEL": CURSOR_MODEL,
        "CURSOR_BASE_URL": CURSOR_BASE_URL,
        "RESUME_PATH": str(RESUME_PATH),
        "BACKUP_DIR": str(BACKUP_DIR),
        "DATA_BACKUP_REMOTE": DATA_BACKUP_REMOTE,
    }


def update_env_file(updates: dict[str, str]) -> None:
    """Write/update keys in .env (preserving other lines), then reload().

    Empty string values are written through (so a key can be cleared).
    Keys whose value is None are left untouched.
    """
    updates = {k: v for k, v in updates.items() if v is not None}

    lines: list[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()

    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(line)

    for key, val in updates.items():
        if key not in seen:
            out.append(f"{key}={val}")

    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    reload()


# Initial load on import.
reload()
