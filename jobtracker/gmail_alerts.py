"""Read LinkedIn job-alert emails from Gmail and track them as leads.

A dedicated mailbox (or a Gmail label such as ``linkedin-jobs``) collects
LinkedIn job-alert emails. On demand we fetch new messages under that label
via the Gmail API (read-only scope), extract the individual job postings
(title, company, location, link) from each email, and store them in the
``job_alerts`` table. Each alert is then cross-checked against the
applications list, so the UI can show "applied" vs "not applied yet".

Auth mirrors gsheets.py: the same Desktop-app OAuth client JSON, but a
separate per-profile token (``gmail_token.json``) with the minimal
``gmail.readonly`` scope — sign in with the alerts mailbox account, which
may differ from the Drive/Sheets account.
"""
from __future__ import annotations

import base64
import json
import re
from difflib import SequenceMatcher

from . import config
from .db import get_connection, now_iso

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class AlertsError(Exception):
    """User-readable Gmail job-alerts failure."""


def _token_path():
    # Per active profile, like the Sheets token (and covered by backups).
    return config.PROFILE_DIR / "gmail_token.json"


def is_connected() -> bool:
    return _token_path().exists()


def connect() -> None:
    """Run the one-time OAuth browser flow and store the Gmail token."""
    from pathlib import Path

    secret = Path(str(config.GOOGLE_CLIENT_SECRET))
    if not secret.exists():
        raise AlertsError(
            f"OAuth client file not found at {secret}. It's the same Desktop-app "
            "client JSON used for Google Sheets — set its path in Settings.")
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise AlertsError(
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
        raise AlertsError("Gmail isn't connected yet — click "
                          "\u201cConnect Gmail\u201d in Settings first.")
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_info(
        json.loads(token_path.read_text(encoding="utf-8")), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
    if not creds.valid:
        raise AlertsError("Gmail login expired — click \u201cConnect Gmail\u201d "
                          "in Settings to sign in again.")
    return creds


def _service():
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise AlertsError(
            "Google client libraries are missing — restart via start.command "
            "to install dependencies.") from exc
    return build("gmail", "v1", credentials=_credentials())


def _label_key(name: str) -> str:
    """Normalise a label name the way Gmail search does: 'LinkedIn Jobs',
    'linkedin-jobs' and 'linkedin_jobs' all refer to the same label."""
    return re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")


def _label_id(svc, name: str) -> str:
    labels = svc.users().labels().list(userId="me").execute().get("labels", [])
    want = _label_key(name)
    for lab in labels:
        if _label_key(lab.get("name", "")) == want:
            return lab["id"]
    user_labels = sorted(lab["name"] for lab in labels
                         if lab.get("type") == "user")
    hint = ("Labels in this mailbox: " + ", ".join(user_labels) + ". "
            if user_labels else "")
    raise AlertsError(
        f"Gmail label \u201c{name}\u201d wasn't found in the connected mailbox. "
        f"{hint}Fix the label name in Settings (or create the label in Gmail "
        "and point your LinkedIn alert filter at it).")


# --------------------------------------------------------------------------- #
# Email parsing
# --------------------------------------------------------------------------- #
_JOB_URL_RE = re.compile(
    r"linkedin\.com/(?:comm/)?jobs/view/(?:[^\s/?#]*?-)?(\d{6,})")

# Anchor texts that are buttons/boilerplate, never a job title.
_BOILERPLATE = {
    "view job", "view jobs", "see all jobs", "see more jobs", "see jobs",
    "apply", "apply now", "easy apply", "save", "saved", "see similar jobs",
    "job alert", "manage job alerts", "unsubscribe", "help", "view all",
}


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def _split_company_loc(text: str) -> tuple[str, str] | None:
    """LinkedIn cards show 'Company · Location' — split on the middle dot."""
    if "·" not in text:
        return None
    parts = [p.strip() for p in text.split("·") if p.strip()]
    if not parts:
        return None
    return parts[0], ", ".join(parts[1:])


def parse_linkedin_alert(html: str) -> list[dict]:
    """Extract job postings from a LinkedIn job-alert email (HTML body)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    jobs: dict[str, dict] = {}
    anchor_of: dict[str, object] = {}

    for a in soup.find_all("a", href=True):
        m = _JOB_URL_RE.search(a["href"])
        if not m:
            continue
        jid = m.group(1)
        job = jobs.setdefault(jid, {
            "job_key": jid, "title": "", "company": "", "location": "",
            "url": f"https://www.linkedin.com/jobs/view/{jid}",
        })
        anchor_of.setdefault(jid, a)
        text = _clean(a.get_text(" ", strip=True))
        if not text or len(text) < 2 or text.lower() in _BOILERPLATE:
            continue
        pair = _split_company_loc(text)
        if pair and not job["company"]:
            job["company"], job["location"] = pair
        elif not job["title"]:
            job["title"] = text[:200]

    # Fill missing company/location from the surrounding card text
    # ("Company · Location" often isn't inside an anchor).
    for jid, job in jobs.items():
        if job["company"]:
            continue
        a = anchor_of.get(jid)
        card = a
        for _ in range(6):
            if card is None:
                break
            card = card.parent
            if card is None:
                break
            for segment in card.stripped_strings:
                seg = _clean(segment)
                if seg == job["title"]:
                    continue
                pair = _split_company_loc(seg)
                if pair:
                    job["company"], job["location"] = pair
                    break
            if job["company"]:
                break

    return [j for j in jobs.values() if j["title"]]


def _html_body(payload: dict) -> str:
    """Walk a Gmail message payload for the text/html part (base64url)."""
    stack = [payload]
    while stack:
        part = stack.pop()
        if part.get("mimeType") == "text/html":
            data = part.get("body", {}).get("data")
            if data:
                return base64.urlsafe_b64decode(data + "===").decode(
                    "utf-8", errors="replace")
        stack.extend(part.get("parts", []) or [])
    return ""


# --------------------------------------------------------------------------- #
# Fetch + store
# --------------------------------------------------------------------------- #
def fetch_alerts() -> dict:
    """Pull new alert emails, parse them, store new jobs. Returns a summary."""
    svc = _service()
    try:
        from googleapiclient.errors import HttpError
    except ImportError as exc:  # pragma: no cover - checked in _service already
        raise AlertsError("Google client libraries are missing.") from exc

    try:
        label = _label_id(svc, config.GMAIL_LABEL)
        # Walk the WHOLE label (paginated). Listing only returns message ids —
        # cheap — and already-parsed emails are skipped below, so every fetch
        # after the first only downloads what's new.
        message_ids: list[str] = []
        page_token = None
        while True:
            listing = svc.users().messages().list(
                userId="me", labelIds=[label], maxResults=100,
                pageToken=page_token).execute()
            message_ids.extend(m["id"] for m in listing.get("messages", []))
            page_token = listing.get("nextPageToken")
            if not page_token or len(message_ids) >= 2000:
                break

        with get_connection() as conn:
            seen = {r["gmail_id"] for r in
                    conn.execute("SELECT gmail_id FROM alert_emails")}
        new_ids = [mid for mid in message_ids if mid not in seen]

        new_emails = 0
        new_jobs = 0
        for mid in new_ids:
            msg = svc.users().messages().get(
                userId="me", id=mid, format="full").execute()
            html = _html_body(msg.get("payload", {}))
            found = parse_linkedin_alert(html) if html else []
            # Email arrival time (ms epoch) -> ISO, for the alert list ordering.
            try:
                from datetime import datetime, timezone
                ts = datetime.fromtimestamp(
                    int(msg.get("internalDate", 0)) / 1000, tz=timezone.utc
                ).isoformat(timespec="seconds")
            except (TypeError, ValueError, OSError):
                ts = now_iso()
            with get_connection() as conn:
                for job in found:
                    cur = conn.execute(
                        """INSERT OR IGNORE INTO job_alerts
                             (job_key, title, company, location, url,
                              gmail_id, alert_at, created_at)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (job["job_key"], job["title"], job["company"],
                         job["location"], job["url"], mid, ts, now_iso()))
                    new_jobs += cur.rowcount
                conn.execute(
                    "INSERT OR IGNORE INTO alert_emails (gmail_id, fetched_at) "
                    "VALUES (?,?)", (mid, now_iso()))
            new_emails += 1
    except HttpError as exc:
        if exc.resp.status == 403:
            raise AlertsError(
                "Gmail API access denied — enable the Gmail API for your "
                "Google Cloud project (console.cloud.google.com → APIs & "
                "Services → Library → Gmail API), then reconnect.") from exc
        raise AlertsError(f"Gmail API error: {exc.reason or exc}") from exc

    refresh_matches()
    return {"emails": new_emails, "jobs": new_jobs}


# --------------------------------------------------------------------------- #
# Matching alerts against the applications list
# --------------------------------------------------------------------------- #
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9\u0590-\u05ff ]+", " ", (s or "").lower()).strip()


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _match_one(alert, apps) -> int | None:
    """Best application match for an alert: URL job-id first, then fuzzy."""
    key = alert["job_key"] or ""
    for app in apps:
        m = _JOB_URL_RE.search(app["url"] or "")
        if m and key and m.group(1) == key:
            return app["id"]

    a_company = _norm(alert["company"])
    a_title = _norm(alert["title"])
    if not a_company or not a_title:
        return None
    for app in apps:
        c = _norm(app["company"])
        if not c:
            continue
        company_ok = (c == a_company or c in a_company or a_company in c
                      or _sim(c, a_company) >= 0.85)
        if company_ok and _sim(_norm(app["title"]), a_title) >= 0.55:
            return app["id"]
    return None


def refresh_matches() -> None:
    """Recompute + persist which alerts correspond to existing applications."""
    with get_connection() as conn:
        apps = conn.execute(
            "SELECT id, company, title, url FROM applications").fetchall()
        alerts = conn.execute(
            "SELECT id, job_key, title, company FROM job_alerts").fetchall()
        for alert in alerts:
            conn.execute("UPDATE job_alerts SET matched_app_id=? WHERE id=?",
                         (_match_one(alert, apps), alert["id"]))


# --------------------------------------------------------------------------- #
# Queries for the UI
# --------------------------------------------------------------------------- #
def list_alerts(include_dismissed: bool = False):
    q = "SELECT * FROM job_alerts"
    if not include_dismissed:
        q += " WHERE dismissed = 0"
    q += " ORDER BY alert_at DESC, id DESC"
    with get_connection() as conn:
        return conn.execute(q).fetchall()


def new_alert_count() -> int:
    """Non-dismissed alerts with no matching application (nav badge)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM job_alerts "
            "WHERE dismissed = 0 AND matched_app_id IS NULL").fetchone()
        return int(row["n"])


def set_dismissed(alert_id: int, dismissed: bool) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE job_alerts SET dismissed=? WHERE id=?",
                     (1 if dismissed else 0, alert_id))
