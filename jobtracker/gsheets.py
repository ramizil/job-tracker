"""Sync applications to a Google Sheet in a user-chosen Drive folder.

Auth is a one-time OAuth sign-in (browser window) using a Google Cloud
"Desktop app" OAuth client. The client-secret JSON path and the Drive folder
URL live in Settings; the refresh token is stored in the active profile's
folder (each profile can connect its own Google account).

The spreadsheet mirrors the Excel export (same columns, jobtracker/exporter.py)
and is created inside the folder on first sync if it doesn't exist yet. Uses
the minimal ``drive.file`` scope: the app can only see files it created.
"""
from __future__ import annotations

import json
import re
import threading

from . import config
from .exporter import COLUMNS
from .tracker import list_applications

SHEET_NAME = "Job Tracker Applications"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def _token_path():
    # Per active profile: each profile can connect its own Google account.
    return config.PROFILE_DIR / "google_token.json"

_MIME_SHEET = "application/vnd.google-apps.spreadsheet"


class SyncError(Exception):
    """User-readable Google Sheets sync failure."""


def folder_id_from_url(url: str) -> str:
    """Accept a full Drive folder URL or a bare folder id."""
    url = (url or "").strip()
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", url)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", url):
        return url
    raise SyncError(
        "Google Drive folder isn't set or isn't recognised — paste the folder "
        "URL (https://drive.google.com/drive/.../folders/...) in Settings.")


def is_configured() -> bool:
    return bool(config.GDRIVE_FOLDER)


def is_connected() -> bool:
    return _token_path().exists()


def connect() -> None:
    """Run the one-time OAuth browser flow and store the token."""
    from pathlib import Path

    secret = Path(str(config.GOOGLE_CLIENT_SECRET))
    if not secret.exists():
        raise SyncError(
            f"OAuth client file not found at {secret}. Create a Desktop-app "
            "OAuth client in Google Cloud Console, download its JSON, and put "
            "it there (or set its path in Settings).")
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise SyncError(
            "Google client libraries are missing — restart via start.command "
            "to install dependencies.") from exc
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True,
                                  authorization_prompt_message="")
    _token_path().write_text(creds.to_json(), encoding="utf-8")


def disconnect() -> None:
    _token_path().unlink(missing_ok=True)


def _credentials():
    token_path = _token_path()
    if not token_path.exists():
        raise SyncError("Google account isn't connected yet — click "
                        "\u201cConnect Google\u201d in Settings first.")
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_info(
        json.loads(token_path.read_text(encoding="utf-8")), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
    if not creds.valid:
        raise SyncError("Google login expired — click \u201cConnect Google\u201d "
                        "in Settings to sign in again.")
    return creds


def _find_or_create_sheet(drive, folder_id: str) -> str:
    q = (f"name = '{SHEET_NAME}' and '{folder_id}' in parents "
         f"and mimeType = '{_MIME_SHEET}' and trashed = false")
    found = drive.files().list(q=q, fields="files(id)", pageSize=1).execute()
    files = found.get("files", [])
    if files:
        return files[0]["id"]
    created = drive.files().create(
        body={"name": SHEET_NAME, "mimeType": _MIME_SHEET,
              "parents": [folder_id]},
        fields="id").execute()
    return created["id"]


def sync() -> str:
    """Write all applications to the sheet (created on demand). Returns its URL."""
    folder_id = folder_id_from_url(config.GDRIVE_FOLDER)
    creds = _credentials()
    try:
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ImportError as exc:
        raise SyncError(
            "Google client libraries are missing — restart via start.command "
            "to install dependencies.") from exc

    try:
        drive = build("drive", "v3", credentials=creds)
        sheets = build("sheets", "v4", credentials=creds)
        sheet_id = _find_or_create_sheet(drive, folder_id)

        header = [c.replace("_", " ").title() for c in COLUMNS]
        values = [header]
        for r in list_applications():
            values.append(["" if (c not in r.keys() or r[c] is None) else str(r[c])
                           for c in COLUMNS])

        sheets.spreadsheets().values().clear(
            spreadsheetId=sheet_id, range="A:ZZ").execute()
        sheets.spreadsheets().values().update(
            spreadsheetId=sheet_id, range="A1", valueInputOption="RAW",
            body={"values": values}).execute()
    except HttpError as exc:
        if exc.resp.status == 404:
            raise SyncError(
                "Drive folder not found — check the folder URL in Settings "
                "and that it belongs to the Google account you connected.")
        raise SyncError(f"Google API error: {exc.reason or exc}") from exc
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}"


# ---- Debounced background auto-sync (called after data mutations) ---------- #
_timer: threading.Timer | None = None
_timer_lock = threading.Lock()


def _sync_quietly() -> None:
    try:
        sync()
    except Exception:
        pass  # background best-effort; the manual button surfaces errors


def schedule_sync(delay_s: float = 10.0) -> None:
    """Debounced sync: bursts of edits produce one API write."""
    if not (is_configured() and is_connected()):
        return
    global _timer
    with _timer_lock:
        if _timer is not None:
            _timer.cancel()
        _timer = threading.Timer(delay_s, _sync_quietly)
        _timer.daemon = True
        _timer.start()
