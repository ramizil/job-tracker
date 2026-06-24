"""Open the dashboard in a standalone app window (Edge/Chrome 'app mode').

Falls back to the default browser if neither Edge nor Chrome is found.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import webbrowser


def _find_browser() -> str | None:
    # Prefer a Chromium browser so we can use --app (chromeless window).
    candidates = [
        shutil.which("msedge"),
        shutil.which("chrome"),
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def open_app_window(url: str, fullscreen: bool = False) -> None:
    """Open `url` in a separate, maximized (or fullscreen) app window."""
    browser = _find_browser()
    if not browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
        return

    # A dedicated profile dir keeps the app window isolated from normal tabs.
    profile = os.path.join(os.path.expanduser("~"), ".jobtracker_app")
    args = [
        browser,
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-fullscreen" if fullscreen else "--start-maximized",
    ]
    try:
        subprocess.Popen(args, close_fds=True)
    except Exception:
        try:
            webbrowser.open(url)
        except Exception:
            pass
