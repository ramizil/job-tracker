"""File-backed API usage counters.

Tracks consumption of limited free tiers (currently Jooble's 500-request
allowance). Counts are stored per API key, so swapping in a new key resets the
counter automatically.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import config

_USAGE_PATH = config.DATA_DIR / "usage.json"
JOOBLE_FREE_LIMIT = 500
# Warn the user once the remaining requests drop to/under this.
JOOBLE_WARN_AT = 50


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _key_id(key: str) -> str:
    """Short, non-secret identifier for a key (first + last 4 chars)."""
    key = key or ""
    return f"{key[:4]}…{key[-4:]}" if len(key) >= 8 else key


def _load() -> dict:
    if _USAGE_PATH.exists():
        try:
            return json.loads(_USAGE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save(data: dict) -> None:
    try:
        _USAGE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def record_jooble_request(key: str, n: int = 1) -> None:
    """Increment the Jooble request counter for the current key."""
    if not key:
        return
    data = _load()
    entry = data.get("jooble") or {}
    if entry.get("key") != _key_id(key):  # new key -> fresh counter
        entry = {"key": _key_id(key), "count": 0, "since": _now()}
    entry["count"] = int(entry.get("count", 0)) + n
    entry["last"] = _now()
    data["jooble"] = entry
    _save(data)


def jooble_usage(key: str, limit: int = JOOBLE_FREE_LIMIT) -> dict:
    """Return usage stats for the given Jooble key."""
    entry = _load().get("jooble") or {}
    same = bool(key) and entry.get("key") == _key_id(key)
    count = int(entry.get("count", 0)) if same else 0
    remaining = max(0, limit - count)
    return {
        "tracked": same,
        "count": count,
        "limit": limit,
        "remaining": remaining,
        "since": entry.get("since"),
        "last": entry.get("last"),
        "low": remaining <= JOOBLE_WARN_AT,
        "exhausted": remaining <= 0,
    }
