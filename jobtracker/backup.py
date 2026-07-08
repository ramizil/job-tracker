"""Backup / restore of ALL profiles: every profile's database, settings,
match profile, pitch and generated documents (tailored resumes).

A backup is a self-contained folder you can keep anywhere — the recommended
spot is a OneDrive-synced directory so it is both private and auto-backed-up.

Layout (new): ``.env`` (default profile settings), ``active_profile`` and one
``profiles/<name>/`` folder per profile. Backups made before multi-profile
support (flat files, no ``profiles/`` folder) can still be restored — they load
into the currently active profile.
"""
from __future__ import annotations

import io
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from . import config
from .profiles import list_profiles

# Transient / cache entries never worth backing up from a profile folder.
_SKIP = {"tts_cache"}


def _profile_files(name: str) -> list[Path]:
    """Files to back up from one profile's folder (recursive, skips caches)."""
    pdir = config.PROFILES_DIR / name
    if not pdir.is_dir():
        return []
    return [p for p in pdir.rglob("*")
            if p.is_file() and not (set(p.relative_to(pdir).parts) & _SKIP)]


def make_backup(dest_dir: Path | str | None = None) -> Path:
    """Copy all profiles (+ root .env and the active-profile marker) into a
    timestamped folder under ``dest_dir`` (defaults to ``config.BACKUP_DIR``).
    Returns the created folder path."""
    dest = Path(dest_dir) if dest_dir else config.BACKUP_DIR
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = dest / f"jobtracker-backup-{stamp}"
    folder.mkdir(parents=True, exist_ok=True)

    if config.ROOT_ENV_PATH.exists():
        shutil.copy2(config.ROOT_ENV_PATH, folder / ".env")
    if config.ACTIVE_PROFILE_FILE.exists():
        shutil.copy2(config.ACTIVE_PROFILE_FILE, folder / "active_profile")

    for name in list_profiles():
        pdir = config.PROFILES_DIR / name
        for src in _profile_files(name):
            rel = src.relative_to(pdir)
            target = folder / "profiles" / name / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)

    return folder


def backup_zip_bytes() -> bytes:
    """Return an in-memory .zip of the same content (for browser download)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if config.ROOT_ENV_PATH.exists():
            zf.write(config.ROOT_ENV_PATH, ".env")
        if config.ACTIVE_PROFILE_FILE.exists():
            zf.write(config.ACTIVE_PROFILE_FILE, "active_profile")
        for name in list_profiles():
            pdir = config.PROFILES_DIR / name
            for src in _profile_files(name):
                zf.write(src, f"profiles/{name}/{src.relative_to(pdir)}")
    buf.seek(0)
    return buf.getvalue()


def restore_from(folder: Path | str) -> list[str]:
    """Restore from a backup folder, overwriting current data.

    New-style backups (with a ``profiles/`` folder) restore every profile plus
    the root ``.env`` and the active-profile marker. Old flat backups restore
    their DB / profile / .env into the currently active profile.
    """
    src = Path(folder)
    if not src.is_dir():
        raise FileNotFoundError(f"Backup folder not found: {src}")

    restored: list[str] = []
    profiles_src = src / "profiles"

    if profiles_src.is_dir():
        for pdir in sorted(p for p in profiles_src.iterdir() if p.is_dir()):
            dest = config.PROFILES_DIR / pdir.name
            shutil.copytree(pdir, dest, dirs_exist_ok=True)
            restored.append(f"profiles/{pdir.name}/")
        if (src / ".env").exists():
            shutil.copy2(src / ".env", config.ROOT_ENV_PATH)
            restored.append(".env")
        if (src / "active_profile").exists():
            shutil.copy2(src / "active_profile", config.ACTIVE_PROFILE_FILE)
            restored.append("active_profile")
    else:
        # Legacy flat backup -> current active profile.
        mapping = {
            config.DB_PATH.name: config.DB_PATH,
            config.PROFILE_PATH.name: config.PROFILE_PATH,
            ".env": config.ENV_PATH,
        }
        for name, target in mapping.items():
            candidate = src / name
            if candidate.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(candidate, target)
                restored.append(name)
        tailored_src = src / "tailored"
        if tailored_src.is_dir():
            config.TAILORED_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copytree(tailored_src, config.TAILORED_DIR, dirs_exist_ok=True)
            restored.append("tailored/")

    config.reload()
    return restored
