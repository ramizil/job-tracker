"""Open the dashboard in a standalone app window (Edge/Chrome 'app mode').

Falls back to the default browser if neither Edge nor Chrome is found.
Works on Windows, macOS and Linux.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser


def _candidate_paths() -> list[str]:
    """Per-OS locations of Chromium-based browsers (for --app window mode)."""
    # Names resolvable on PATH (Linux, and Windows/macOS when on PATH).
    names = ["msedge", "microsoft-edge", "google-chrome", "google-chrome-stable",
             "chrome", "chromium", "chromium-browser", "brave-browser"]
    by_name = [shutil.which(n) for n in names]

    if sys.platform == "darwin":
        fixed = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
        # Also check per-user installs under ~/Applications.
        home = os.path.expanduser("~")
        fixed += [home + p for p in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        )]
    elif os.name == "nt":
        fixed = [
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
    else:  # Linux / other
        fixed = [
            "/usr/bin/google-chrome",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/microsoft-edge",
            "/snap/bin/chromium",
        ]
    return [p for p in (*by_name, *fixed) if p]


def _find_browser() -> str | None:
    # Prefer a Chromium browser so we can use --app (chromeless window).
    for path in _candidate_paths():
        if os.path.exists(path):
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
