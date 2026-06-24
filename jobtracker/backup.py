"""Backup / restore of the local database, match profile, settings (.env) and
generated documents (tailored resumes).

A backup is a self-contained folder you can keep anywhere — the recommended
spot is a OneDrive-synced directory so it is both private and auto-backed-up.
"""
from __future__ import annotations

import io
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from . import config


def _files() -> list[Path]:
    """Individual files worth backing up (skip whatever doesn't exist yet)."""
    candidates = [config.DB_PATH, config.PROFILE_PATH, config.ENV_PATH]
    return [p for p in candidates if p.exists()]


def make_backup(dest_dir: Path | str | None = None) -> Path:
    """Copy the DB, profile, .env and tailored resumes into a timestamped
    folder under ``dest_dir`` (defaults to ``config.BACKUP_DIR``). Returns the
    created folder path."""
    dest = Path(dest_dir) if dest_dir else config.BACKUP_DIR
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = dest / f"jobtracker-backup-{stamp}"
    folder.mkdir(parents=True, exist_ok=True)

    for path in _files():
        shutil.copy2(path, folder / path.name)

    if config.TAILORED_DIR.exists() and any(config.TAILORED_DIR.iterdir()):
        shutil.copytree(config.TAILORED_DIR, folder / "tailored", dirs_exist_ok=True)

    return folder


def backup_zip_bytes() -> bytes:
    """Return an in-memory .zip of the same content (for browser download)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in _files():
            zf.write(path, path.name)
        if config.TAILORED_DIR.exists():
            for f in config.TAILORED_DIR.glob("*"):
                if f.is_file():
                    zf.write(f, f"tailored/{f.name}")
    buf.seek(0)
    return buf.getvalue()


def restore_from(folder: Path | str) -> list[str]:
    """Restore DB / profile / .env / tailored resumes from a backup folder.
    Returns the list of restored file names. Existing files are overwritten."""
    src = Path(folder)
    if not src.is_dir():
        raise FileNotFoundError(f"Backup folder not found: {src}")

    restored: list[str] = []
    mapping = {
        config.DB_PATH.name: config.DB_PATH,
        config.PROFILE_PATH.name: config.PROFILE_PATH,
        config.ENV_PATH.name: config.ENV_PATH,
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
