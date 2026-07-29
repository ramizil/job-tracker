"""Persist Google connection auth failures for a global UI alert.

When Sheets / Gmail tokens expire or are revoked (``invalid_grant``), the
background loops used to swallow the error and leave the UI looking fine.
This module records those failures so every page can show a reconnect banner
until the user signs in again.

Gmail keys may be feature-wide (``gmail_alerts``) or per-mailbox
(``gmail_alerts:<account_id>``).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from . import config
from .db import now_iso

_FILE = "connection_alerts.json"
_lock = threading.Lock()

# Stable feature-wide keys (also used as prefixes for per-mailbox keys).
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


def _known_key(key: str) -> bool:
    if key in _LABELS:
        return True
    if key.startswith(GMAIL_ALERTS + ":") or key.startswith(GMAIL_REJECTIONS + ":"):
        return True
    return False


def _default_label(key: str) -> str:
    if key in _LABELS:
        return _LABELS[key]
    if key.startswith(GMAIL_ALERTS + ":"):
        return f"Gmail job alerts ({key.split(':', 1)[1]})"
    if key.startswith(GMAIL_REJECTIONS + ":"):
        return f"Gmail rejections ({key.split(':', 1)[1]})"
    return key


def mark_inactive(key: str, reason: str = "", *, label: str = "") -> None:
    """Record that a previously working Google connection needs reconnect."""
    if not _known_key(key):
        return
    with _lock:
        data = _read()
        data[key] = {
            "inactive": True,
            "reason": (reason or "").strip()[:400],
            "at": now_iso(),
            "label": (label or "").strip() or _default_label(key),
        }
        _write(data)


def clear(key: str) -> None:
    """Clear an inactive alert (after reconnect or intentional disconnect)."""
    if not key:
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
    for key, entry in data.items():
        if not isinstance(entry, dict) or not entry.get("inactive"):
            continue
        if not _known_key(key):
            continue
        out.append({
            "key": key,
            "label": entry.get("label") or _default_label(key),
            "reason": entry.get("reason") or "",
            "at": (entry.get("at") or "")[:16].replace("T", " "),
            "settings_hash": "#tab-google",
        })
    # Stable order: sheets, then alerts*, then rejections*
    def _sort_key(item: dict) -> tuple:
        k = item["key"]
        if k == SHEETS:
            return (0, k)
        if k == GMAIL_ALERTS or k.startswith(GMAIL_ALERTS + ":"):
            return (1, k)
        if k == GMAIL_REJECTIONS or k.startswith(GMAIL_REJECTIONS + ":"):
            return (2, k)
        return (3, k)
    out.sort(key=_sort_key)
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
