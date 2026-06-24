"""Central configuration: filesystem paths and environment-backed settings.

Settings live in the project-root ``.env`` file. They can be edited by hand or
from the web UI (Settings page). ``reload()`` re-reads ``.env`` at runtime so
changes take effect without restarting the server.
"""
from __future__ import annotations

import os
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

# Tailored resumes (AI-generated) are written here.
TAILORED_DIR = DATA_DIR / "tailored"
TAILORED_DIR.mkdir(exist_ok=True)

DEFAULT_RESUME = BASE_DIR / "sample_resume.html"

# Keys that the Settings UI manages (editable). Maps env var -> description.
EDITABLE_KEYS: dict[str, str] = {
    "RAPIDAPI_KEY": "JSearch (RapidAPI) - includes LinkedIn listings, Israel",
    "JOOBLE_API_KEY": "Jooble - free job search, Israel",
    "ADZUNA_APP_ID": "Adzuna app id (optional, no Israel)",
    "ADZUNA_APP_KEY": "Adzuna app key (optional)",
    "GEMINI_API_KEY": "Gemini AI key (fit analysis + resume tailoring)",
    "GEMINI_MODEL": "Gemini model (default gemini-2.5-flash)",
    "RESUME_PATH": "Path to your resume HTML",
}

# Module-level settings (re-assigned by reload()).
RAPIDAPI_KEY = ""
JOOBLE_API_KEY = ""
ADZUNA_APP_ID = ""
ADZUNA_APP_KEY = ""
GEMINI_API_KEY = ""
GEMINI_MODEL = "gemini-2.5-flash"
RESUME_PATH = DEFAULT_RESUME


def reload() -> None:
    """(Re)load values from .env into this module's globals."""
    global RAPIDAPI_KEY, JOOBLE_API_KEY, ADZUNA_APP_ID, ADZUNA_APP_KEY
    global GEMINI_API_KEY, GEMINI_MODEL, RESUME_PATH

    load_dotenv(ENV_PATH, override=True)
    RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "").strip()
    JOOBLE_API_KEY = os.getenv("JOOBLE_API_KEY", "").strip()
    ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID", "").strip()
    ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY", "").strip()
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
    RESUME_PATH = Path(os.getenv("RESUME_PATH") or DEFAULT_RESUME)


def current_settings() -> dict[str, str]:
    """Current values for the editable keys (for display in the UI)."""
    return {
        "RAPIDAPI_KEY": RAPIDAPI_KEY,
        "JOOBLE_API_KEY": JOOBLE_API_KEY,
        "ADZUNA_APP_ID": ADZUNA_APP_ID,
        "ADZUNA_APP_KEY": ADZUNA_APP_KEY,
        "GEMINI_API_KEY": GEMINI_API_KEY,
        "GEMINI_MODEL": GEMINI_MODEL,
        "RESUME_PATH": str(RESUME_PATH),
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
