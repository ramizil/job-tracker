"""Idle watchdog: shut the local server down when no browser tab is alive.

The web UI pings ``/heartbeat`` every few seconds. If no heartbeat arrives for
``timeout`` seconds (e.g. the window/tab was closed), the server exits so it
doesn't keep running unnecessarily in the background.
"""
from __future__ import annotations

import os
import threading
import time

_lock = threading.Lock()
_last_ping = time.monotonic()
_enabled = False
_timeout = 90.0
_started = False


def ping() -> None:
    """Record that a live page just checked in."""
    global _last_ping
    with _lock:
        _last_ping = time.monotonic()


def seconds_idle() -> float:
    with _lock:
        return time.monotonic() - _last_ping


def is_enabled() -> bool:
    return _enabled


def start(timeout: float = 90.0) -> None:
    """Begin watching. Safe to call once; subsequent calls are ignored."""
    global _enabled, _timeout, _started, _last_ping
    if _started:
        return
    _enabled = True
    _timeout = max(20.0, float(timeout))
    with _lock:
        _last_ping = time.monotonic()
    _started = True
    threading.Thread(target=_run, name="jobtracker-watchdog", daemon=True).start()


def _run() -> None:
    # Give the browser time to open and send its first heartbeat.
    grace = min(_timeout, 45.0)
    time.sleep(grace)
    while True:
        time.sleep(5)
        if _enabled and seconds_idle() > _timeout:
            # No live page for a while -> stop the server.
            os._exit(0)
