"""Multiple user profiles — each with its own database, settings and artefacts.

A profile is a folder under ``data/profiles/<name>/`` holding that profile's
SQLite DB, ``.env`` settings, match profile, pitch, resumes and Google token.
The active profile name lives in ``data/active_profile``; switching just
rewrites that file and calls ``config.reload()`` (all modules read paths from
``config`` at call time, so no restart is needed).
"""
from __future__ import annotations

import re
import shutil

from . import config
from .db import init_db

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")

# Files copied when a new profile "imports" from an existing one.
# The applications DB is intentionally NOT copied — a new profile starts empty.
_IMPORT_FILES = ("profile.yaml", "pitch.md", "built_resume.html")


class ProfileError(ValueError):
    """User-readable profile management failure."""


def list_profiles() -> list[str]:
    """All profile names, active-profile-independent, default first."""
    config.PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    names = sorted(p.name for p in config.PROFILES_DIR.iterdir() if p.is_dir())
    if config.DEFAULT_PROFILE in names:
        names.remove(config.DEFAULT_PROFILE)
        names.insert(0, config.DEFAULT_PROFILE)
    return names


def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.fullmatch(name):
        raise ProfileError(
            "Profile names use letters, digits, '-' and '_' only (max 40 chars).")
    return name


def create_profile(name: str, import_from: str | None = None) -> str:
    """Create a new profile; optionally import settings from another profile.

    Importing copies the source profile's settings (.env) plus its match
    profile, pitch and built resume — but never the applications database.
    """
    name = _validate_name(name)
    dest = config.PROFILES_DIR / name
    if dest.exists():
        raise ProfileError(f"Profile '{name}' already exists.")

    if import_from is not None:
        import_from = import_from.strip()
        if import_from and import_from not in list_profiles():
            raise ProfileError(f"Profile '{import_from}' not found to import from.")

    dest.mkdir(parents=True)
    if import_from:
        src_env = config.env_path_for(import_from)
        if src_env.exists():
            shutil.copy2(src_env, config.env_path_for(name))
        src_dir = config.PROFILES_DIR / import_from
        for fname in _IMPORT_FILES:
            src = src_dir / fname
            if src.exists():
                shutil.copy2(src, dest / fname)
    return name


def switch_profile(name: str) -> str:
    """Activate a profile: persist the choice, reload config, init its DB."""
    name = _validate_name(name)
    if not (config.PROFILES_DIR / name).is_dir():
        raise ProfileError(f"Profile '{name}' does not exist.")
    config.ACTIVE_PROFILE_FILE.write_text(name + "\n", encoding="utf-8")
    config.reload()
    init_db()  # make sure the (possibly brand-new) DB has the full schema
    return name


def delete_profile(name: str) -> None:
    """Delete a profile folder. The active and default profiles are protected."""
    name = _validate_name(name)
    if name == config.DEFAULT_PROFILE:
        raise ProfileError("The default profile can't be deleted.")
    if name == config.ACTIVE_PROFILE:
        raise ProfileError("Switch to another profile before deleting this one.")
    target = config.PROFILES_DIR / name
    if not target.is_dir():
        raise ProfileError(f"Profile '{name}' does not exist.")
    shutil.rmtree(target)
