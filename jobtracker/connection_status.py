"""Persist Google connection auth failures for a global UI alert.

When Sheets / Gmail tokens expire or are revoked (``invalid_grant``), the
background loops used to swallow the error and leave the UI looking fine.
This module records those failures so every page can show a reconnect banner
until the user signs in again.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from . import config
from .db import now_iso

_FILE = "connection_alerts.json"
_lock = threading.Lock()

# Stable keys used in the JSON file and UI.
SHEETS = "sheets"
GMAIL_ALERTS = "gmail_alerts"
GMAIL_REJECTIONS = "gmail_rejections"

_LABELS = {
    SHEETS: "Google Sheets sync",
    GMAIL_ALERTS: "Gmail job alerts",
    GMAIL_REJECTIONS: "Gmail rejections",
}


def _path() -> Path:
    return Path(config.PROFILE_DIR) / _FILE


def _read() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def mark_inactive(key: str, reason: str = "") -> None:
    """Record that a previously working Google connection needs reconnect."""
    if key not in _LABELS:
        return
    with _lock:
        data = _read()
        data[key] = {
            "inactive": True,
            "reason": (reason or "").strip()[:400],
            "at": now_iso(),
            "label": _LABELS[key],
        }
        _write(data)


def clear(key: str) -> None:
    """Clear an inactive alert (after reconnect or intentional disconnect)."""
    if key not in _LABELS:
        return
    with _lock:
        data = _read()
        if key in data:
            data.pop(key, None)
            _write(data)


def clear_all() -> None:
    with _lock:
        _write({})


def inactive_alerts() -> list[dict]:
    """List of inactive connections for the global banner / Settings."""
    with _lock:
        data = _read()
    out: list[dict] = []
    for key, label in _LABELS.items():
        entry = data.get(key) or {}
        if not entry.get("inactive"):
            continue
        out.append({
            "key": key,
            "label": entry.get("label") or label,
            "reason": entry.get("reason") or "",
            "at": (entry.get("at") or "")[:16].replace("T", " "),
            "settings_hash": "#tab-google",
        })
    return out


def is_inactive(key: str) -> bool:
    with _lock:
        entry = (_read().get(key) or {})
    return bool(entry.get("inactive"))


def probe_all() -> None:
    """Try refreshing each stored token; mark inactive on auth failure.

    Safe to call from a background thread. Does not raise.
    """
    try:
        from . import gsheets
        gsheets.probe_credentials()
    except Exception:
        pass
    try:
        from . import gmail_alerts
        gmail_alerts.probe_credentials()
    except Exception:
        pass
    try:
        from . import gmail_rejections
        gmail_rejections.probe_credentials()
    except Exception:
        pass


_probe_started = False
_probe_lock = threading.Lock()


def start_health_probe(delay_s: float = 8.0, interval_s: float = 1800.0) -> None:
    """Background probe shortly after startup, then periodically."""
    global _probe_started
    with _probe_lock:
        if _probe_started:
            return
        _probe_started = True

    def _loop() -> None:
        import time
        time.sleep(delay_s)
        while True:
            try:
                probe_all()
            except Exception:
                pass
            time.sleep(interval_s)

    threading.Thread(target=_loop, name="jobtracker-conn-probe",
                     daemon=True).start()
