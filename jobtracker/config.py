"""Central configuration: filesystem paths and environment-backed settings."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Project root = the folder that contains the `jobtracker` package.
BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env from the project root if present.
load_dotenv(BASE_DIR / ".env")

# Local, git-ignored data directory (holds the SQLite DB and generated profile).
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DB_PATH = Path(os.getenv("JOBTRACKER_DB", DATA_DIR / "jobtracker.db"))
PROFILE_PATH = DATA_DIR / "profile.yaml"

# Resume used to build the matching profile. Falls back to the bundled sample.
DEFAULT_RESUME = BASE_DIR / "sample_resume.html"
RESUME_PATH = Path(os.getenv("RESUME_PATH") or DEFAULT_RESUME)

# --- API keys (all optional) ------------------------------------------------
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "").strip()
JOOBLE_API_KEY = os.getenv("JOOBLE_API_KEY", "").strip()
ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID", "").strip()
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY", "").strip()
