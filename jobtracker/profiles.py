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

# Personal artefacts — never copied unless the user explicitly opts in.
_IMPORT_PERSONAL_FILES = (
    "profile.yaml", "pitch.md", "pitch.html",
    "pitch_recruiter.md", "pitch_recruiter.html",
    "built_resume.html",
)

# .env keys that identify a person / their paths — stripped on normal import.
_PERSONAL_ENV_KEYS = frozenset({
    "RESUME_PATH",
    "BACKUP_DIR",
    "DATA_BACKUP_REMOTE",
    "GDRIVE_FOLDER",
    "LINKEDIN_URL",
    "GITHUB_URL",
})


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


def _copy_env(src_profile: str, dest_profile: str, *, copy_personal: bool) -> None:
    """Copy .env; by default omit personal paths and profile links."""
    src = config.env_path_for(src_profile)
    if not src.exists():
        return
    dest = config.env_path_for(dest_profile)
    if copy_personal:
        shutil.copy2(src, dest)
        return
    lines_out: list[str] = []
    for line in src.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            lines_out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in _PERSONAL_ENV_KEYS:
            continue
        lines_out.append(line)
    dest.write_text("\n".join(lines_out) + ("\n" if lines_out else ""),
                    encoding="utf-8")


def create_profile(name: str, import_from: str | None = None,
                   *, copy_personal: bool = False) -> str:
    """Create a new profile; optionally import non-personal settings.

    By default, importing copies API keys and shared tool settings only —
    not resume path, backup remotes, LinkedIn/GitHub links, pitch, or match
    profile. Pass ``copy_personal=True`` to also copy those (explicit opt-in).

    The applications database is never copied.
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
        _copy_env(import_from, name, copy_personal=copy_personal)
        if copy_personal:
            src_dir = config.PROFILES_DIR / import_from
            for fname in _IMPORT_PERSONAL_FILES:
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
