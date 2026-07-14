"""Central configuration: filesystem paths and environment-backed settings.

The app supports multiple *profiles*, each with its own database, settings and
generated artefacts under ``data/profiles/<name>/``. The active profile name
is stored in ``data/active_profile``; ``reload()`` recomputes every per-profile
path and re-reads that profile's ``.env``, so switching profiles (or editing
settings from the web UI) takes effect without restarting the server.

The **default** profile keeps its settings in the historical project-root
``.env`` (the launcher scripts read it), while every other profile has its own
``.env`` inside its profile folder.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Project root = the folder that contains the `jobtracker` package.
BASE_DIR = Path(__file__).resolve().parent.parent
ROOT_ENV_PATH = BASE_DIR / ".env"

# Local, git-ignored data directory. Shared (profile-independent) files live
# directly in it; per-profile data lives under data/profiles/<name>/.
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

PROFILES_DIR = DATA_DIR / "profiles"
ACTIVE_PROFILE_FILE = DATA_DIR / "active_profile"
DEFAULT_PROFILE = "default"

# Local working tree used to mirror the data to a private GitHub repo
# (see jobtracker/gitbackup.py). Shared: it mirrors ALL profiles.
GIT_BACKUP_DIR = DATA_DIR / ".git-backup"

DEFAULT_RESUME = BASE_DIR / "sample_resume.html"


def env_path_for(profile: str) -> Path:
    """Settings file for a profile (root .env for the historical default)."""
    if profile == DEFAULT_PROFILE:
        return ROOT_ENV_PATH
    return PROFILES_DIR / profile / ".env"


def _migrate_legacy_layout() -> None:
    """One-time move of pre-profiles data/ files into data/profiles/default/.

    Shared assets stay in data/: tts_cache (content-addressed), the Google
    OAuth *client* JSON (app identity, not account) and the git-backup mirror.
    """
    if PROFILES_DIR.exists():
        return
    default_dir = PROFILES_DIR / DEFAULT_PROFILE
    default_dir.mkdir(parents=True)
    for pattern in ("jobtracker.db*", "profile.yaml", "pitch.md",
                    "built_resume.html", "usage.json", "google_token.json"):
        for src in DATA_DIR.glob(pattern):
            if src.is_file():
                src.rename(default_dir / src.name)
    legacy_tailored = DATA_DIR / "tailored"
    if legacy_tailored.is_dir():
        legacy_tailored.rename(default_dir / "tailored")


def _read_active_profile() -> str:
    try:
        name = ACTIVE_PROFILE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return DEFAULT_PROFILE
    if name and (PROFILES_DIR / name).is_dir():
        return name
    return DEFAULT_PROFILE


# Per-profile paths — (re)assigned by reload().
ACTIVE_PROFILE = DEFAULT_PROFILE
PROFILE_DIR = PROFILES_DIR / DEFAULT_PROFILE
ENV_PATH = ROOT_ENV_PATH
DB_PATH = PROFILE_DIR / "jobtracker.db"
PROFILE_PATH = PROFILE_DIR / "profile.yaml"
PITCH_PATH = PROFILE_DIR / "pitch.md"
BUILT_RESUME_PATH = PROFILE_DIR / "built_resume.html"
TAILORED_DIR = PROFILE_DIR / "tailored"


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
    "WEB_SEARCH_SITES": "Comma-separated site: filters for the web-search source (ATS job boards)",
    "SOURCES_DISABLED": "Comma-separated source names to skip (jsearch, jooble, adzuna, websearch)",
    "AI_PROVIDER": "AI provider: gemini | openai | anthropic | groq | cursor",
    "AI_FALLBACK": "Auto-switch to another configured AI provider when one fails (1/0)",
    "GEMINI_API_KEY": "Gemini AI key (fit analysis + resume tailoring)",
    "GEMINI_MODEL": "Gemini model (default gemini-2.5-flash)",
    "OPENAI_API_KEY": "OpenAI API key (GPT models)",
    "OPENAI_MODEL": "OpenAI model (default gpt-4o-mini)",
    "ANTHROPIC_API_KEY": "Anthropic API key (Claude models)",
    "ANTHROPIC_MODEL": "Anthropic model (default claude-3-5-sonnet-latest)",
    "GROQ_API_KEY": "Groq API key (fast open models, free tier)",
    "GROQ_MODEL": "Groq model (default openai/gpt-oss-120b)",
    "CURSOR_API_KEY": "Cursor API key (crsr_...) - used via a local OpenAI-compatible proxy",
    "CURSOR_MODEL": "Cursor model (default auto)",
    "CURSOR_BASE_URL": "Cursor proxy base URL (default http://localhost:8080/v1)",
    "RESUME_PATH": "Path to your resume (HTML, PDF, Word .docx, or text)",
    "BACKUP_DIR": "Folder for backups (a OneDrive path = auto-synced & private)",
    "DATA_BACKUP_REMOTE": "Private git repo URL to mirror your data to (GitHub backup)",
    "GDRIVE_FOLDER": "Google Drive folder URL for the online applications sheet",
    "GOOGLE_CLIENT_SECRET": "Path to the Google OAuth client JSON (Desktop app)",
    "GMAIL_LABEL": "Gmail label whose emails are scanned for job alerts",
    "GMAIL_REJECTION_LABEL": "Gmail label for rejection emails (rejections mailbox)",
}

# Module-level settings (re-assigned by reload()).
RAPIDAPI_KEY = ""
JOOBLE_API_KEY = ""
ADZUNA_APP_ID = ""
ADZUNA_APP_KEY = ""
# ATS platforms popular with Israeli companies, searched via `site:` queries.
DEFAULT_WEB_SEARCH_SITES = ("comeet.com/jobs, boards.greenhouse.io, jobs.lever.co, "
                            "careers.smartrecruiters.com")
WEB_SEARCH_SITES = DEFAULT_WEB_SEARCH_SITES
SOURCES_DISABLED = ""
AI_PROVIDER = "gemini"
AI_FALLBACK = False
GEMINI_API_KEY = ""
GEMINI_MODEL = "gemini-2.5-flash"
OPENAI_API_KEY = ""
OPENAI_MODEL = "gpt-4o-mini"
ANTHROPIC_API_KEY = ""
ANTHROPIC_MODEL = "claude-3-5-sonnet-latest"
GROQ_API_KEY = ""
GROQ_MODEL = "openai/gpt-oss-120b"
CURSOR_API_KEY = ""
CURSOR_MODEL = "auto"
CURSOR_BASE_URL = "http://localhost:8080/v1"
RESUME_PATH = DEFAULT_RESUME
BACKUP_DIR = _default_backup_dir()
DATA_BACKUP_REMOTE = ""
GDRIVE_FOLDER = ""
GOOGLE_CLIENT_SECRET = DATA_DIR / "google_client_secret.json"
GMAIL_LABEL = "linkedin-jobs"
GMAIL_REJECTION_LABEL = "job-rejection"


def reload() -> None:
    """(Re)compute per-profile paths and load the active profile's .env."""
    global ACTIVE_PROFILE, PROFILE_DIR, ENV_PATH
    global DB_PATH, PROFILE_PATH, PITCH_PATH, BUILT_RESUME_PATH, TAILORED_DIR
    global RAPIDAPI_KEY, JOOBLE_API_KEY, ADZUNA_APP_ID, ADZUNA_APP_KEY
    global WEB_SEARCH_SITES, SOURCES_DISABLED
    global AI_PROVIDER, AI_FALLBACK, GEMINI_API_KEY, GEMINI_MODEL
    global OPENAI_API_KEY, OPENAI_MODEL, ANTHROPIC_API_KEY, ANTHROPIC_MODEL
    global GROQ_API_KEY, GROQ_MODEL
    global CURSOR_API_KEY, CURSOR_MODEL, CURSOR_BASE_URL
    global RESUME_PATH, BACKUP_DIR, DATA_BACKUP_REMOTE
    global GDRIVE_FOLDER, GOOGLE_CLIENT_SECRET, GMAIL_LABEL, GMAIL_REJECTION_LABEL

    _migrate_legacy_layout()
    ACTIVE_PROFILE = _read_active_profile()
    PROFILE_DIR = PROFILES_DIR / ACTIVE_PROFILE
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    ENV_PATH = env_path_for(ACTIVE_PROFILE)
    DB_PATH = Path(os.getenv("JOBTRACKER_DB") or PROFILE_DIR / "jobtracker.db")
    PROFILE_PATH = PROFILE_DIR / "profile.yaml"
    PITCH_PATH = PROFILE_DIR / "pitch.md"
    BUILT_RESUME_PATH = PROFILE_DIR / "built_resume.html"
    TAILORED_DIR = PROFILE_DIR / "tailored"
    TAILORED_DIR.mkdir(exist_ok=True)

    # Start from a clean slate so keys absent from this profile's .env don't
    # leak in from a previously loaded profile.
    for key in EDITABLE_KEYS:
        os.environ.pop(key, None)
    load_dotenv(ENV_PATH, override=True)
    RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "").strip()
    JOOBLE_API_KEY = os.getenv("JOOBLE_API_KEY", "").strip()
    ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID", "").strip()
    ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY", "").strip()
    # GOOGLE_CSE_SITES is honoured as a legacy fallback (pre-DuckDuckGo backend).
    WEB_SEARCH_SITES = (os.getenv("WEB_SEARCH_SITES", "").strip()
                        or os.getenv("GOOGLE_CSE_SITES", "").strip()
                        or DEFAULT_WEB_SEARCH_SITES)
    SOURCES_DISABLED = os.getenv("SOURCES_DISABLED", "").strip().lower()
    AI_PROVIDER = (os.getenv("AI_PROVIDER", "gemini").strip().lower() or "gemini")
    AI_FALLBACK = os.getenv("AI_FALLBACK", "").strip().lower() in ("1", "true", "yes", "on")
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
    ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest").strip()
    GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
    GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()
    CURSOR_API_KEY = os.getenv("CURSOR_API_KEY", "").strip()
    CURSOR_MODEL = os.getenv("CURSOR_MODEL", "auto").strip()
    CURSOR_BASE_URL = os.getenv("CURSOR_BASE_URL", "http://localhost:8080/v1").strip()
    RESUME_PATH = Path(os.getenv("RESUME_PATH") or DEFAULT_RESUME)
    BACKUP_DIR = Path(os.getenv("BACKUP_DIR") or _default_backup_dir())
    DATA_BACKUP_REMOTE = os.getenv("DATA_BACKUP_REMOTE", "").strip()
    GDRIVE_FOLDER = os.getenv("GDRIVE_FOLDER", "").strip()
    GOOGLE_CLIENT_SECRET = Path(
        os.getenv("GOOGLE_CLIENT_SECRET") or DATA_DIR / "google_client_secret.json")
    GMAIL_LABEL = os.getenv("GMAIL_LABEL", "").strip() or "linkedin-jobs"
    GMAIL_REJECTION_LABEL = (os.getenv("GMAIL_REJECTION_LABEL", "").strip()
                               or "job-rejection")


def current_settings() -> dict[str, str]:
    """Current values for the editable keys (for display in the UI)."""
    return {
        "RAPIDAPI_KEY": RAPIDAPI_KEY,
        "JOOBLE_API_KEY": JOOBLE_API_KEY,
        "ADZUNA_APP_ID": ADZUNA_APP_ID,
        "ADZUNA_APP_KEY": ADZUNA_APP_KEY,
        "WEB_SEARCH_SITES": WEB_SEARCH_SITES,
        "SOURCES_DISABLED": SOURCES_DISABLED,
        "AI_PROVIDER": AI_PROVIDER,
        "AI_FALLBACK": "1" if AI_FALLBACK else "",
        "GEMINI_API_KEY": GEMINI_API_KEY,
        "GEMINI_MODEL": GEMINI_MODEL,
        "OPENAI_API_KEY": OPENAI_API_KEY,
        "OPENAI_MODEL": OPENAI_MODEL,
        "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
        "ANTHROPIC_MODEL": ANTHROPIC_MODEL,
        "GROQ_API_KEY": GROQ_API_KEY,
        "GROQ_MODEL": GROQ_MODEL,
        "CURSOR_API_KEY": CURSOR_API_KEY,
        "CURSOR_MODEL": CURSOR_MODEL,
        "CURSOR_BASE_URL": CURSOR_BASE_URL,
        "RESUME_PATH": str(RESUME_PATH),
        "BACKUP_DIR": str(BACKUP_DIR),
        "DATA_BACKUP_REMOTE": DATA_BACKUP_REMOTE,
        "GDRIVE_FOLDER": GDRIVE_FOLDER,
        "GOOGLE_CLIENT_SECRET": str(GOOGLE_CLIENT_SECRET),
        "GMAIL_LABEL": GMAIL_LABEL,
        "GMAIL_REJECTION_LABEL": GMAIL_REJECTION_LABEL,
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
