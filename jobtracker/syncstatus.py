"""Persisted last-backup / last-Sheets-sync timestamps for UI confidence."""
from __future__ import annotations

import json
from pathlib import Path

from . import config
from .db import now_iso


def _path(name: str) -> Path:
    return Path(config.PROFILE_DIR) / name


def _read(name: str) -> dict:
    try:
        data = json.loads(_path(name).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(name: str, data: dict) -> None:
    path = _path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def record_backup(*, folder: str = "", zip_download: bool = False) -> None:
    _write("last_backup.json", {
        "at": now_iso(),
        "folder": folder,
        "zip_download": zip_download,
    })


def record_sheets_sync(*, url: str = "") -> None:
    _write("last_sheets_sync.json", {
        "at": now_iso(),
        "url": url,
    })


def last_backup() -> dict:
    return _read("last_backup.json")


def last_sheets_sync() -> dict:
    return _read("last_sheets_sync.json")


def status_summary() -> dict:
    """Compact status for the top bar / Settings."""
    b = last_backup()
    s = last_sheets_sync()
    return {
        "backup_at": (b.get("at") or "")[:16].replace("T", " "),
        "backup_folder": b.get("folder") or "",
        "sheets_at": (s.get("at") or "")[:16].replace("T", " "),
        "sheets_url": s.get("url") or "",
    }
