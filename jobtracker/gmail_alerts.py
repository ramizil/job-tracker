"""Read job-alert emails from Gmail (LinkedIn + Indeed) and track them as leads.

A dedicated mailbox (or Gmail labels such as ``linkedin-jobs`` and
``indeed-job-posting``) collects job-alert emails. On demand we fetch new
messages under those labels via the Gmail API (read-only scope), extract the
individual job postings (title, company, location, link) from each email, and
store them in the ``job_alerts`` table. Each alert is then cross-checked
against the applications list, so the UI can show "applied" vs "not applied yet".

Auth mirrors gsheets.py: the same Desktop-app OAuth client JSON, but a
separate per-profile token (``gmail_token.json``) with the minimal
``gmail.readonly`` scope — sign in with the alerts mailbox account, which
may differ from the Drive/Sheets account.
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
from difflib import SequenceMatcher

from . import config
from .db import get_connection, now_iso

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Background auto-fetch cadence while the server is running.
AUTO_FETCH_INTERVAL_S = 600  # 10 minutes


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
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_info(
        json.loads(token_path.read_text(encoding="utf-8")), SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
        except RefreshError as exc:
            # invalid_grant — token revoked / expired / password changed.
            token_path.unlink(missing_ok=True)
            raise AlertsError(
                "Gmail login expired or was revoked. Open Settings → "
                "Gmail job alerts → Connect Gmail and sign in again "
                "(one-time browser login)."
            ) from exc
    if not creds.valid:
        token_path.unlink(missing_ok=True)
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

# Indeed job key in viewjob / click-through URLs (stable ~16-char hex).
_INDEED_JK_RE = re.compile(
    r"(?:[?&#](?:jk|vjk)=|/viewjob[^\"'\s]*?[?&]jk=)([a-f0-9]{10,20})",
    re.I,
)
_INDEED_HOST_RE = re.compile(r"indeed\.[a-z.]+", re.I)

# Anchor texts that are buttons/boilerplate, never a job title.
_BOILERPLATE = {
    "view job", "view jobs", "see all jobs", "see more jobs", "see jobs",
    "apply", "apply now", "easy apply", "save", "saved", "see similar jobs",
    "job alert", "manage job alerts", "unsubscribe", "help", "view all",
    "click here", "learn more", "sign in", "update alert", "delete alert",
}


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def _split_company_loc(text: str) -> tuple[str, str] | None:
    """LinkedIn cards show 'Company · Location' — split on the middle dot."""
    if "·" not in text and "•" not in text and "∙" not in text:
        return None
    parts = [p.strip() for p in re.split(r"[·•∙]", text) if p.strip()]
    if not parts:
        return None
    return parts[0], ", ".join(parts[1:])


def _subject_title_company(subject: str) -> tuple[str, str]:
    """Indeed often uses 'Title @ Company' (or 'Title at Company') as subject."""
    subj = _clean(subject or "")
    # Strip common prefixes: "Indeed: ", "New job: ", etc.
    subj = re.sub(
        r"^(?:indeed\s*[:\-–]\s*|new jobs?\s*[:\-–]\s*)",
        "", subj, flags=re.I,
    ).strip()
    m = re.match(r"^(.+?)\s+[@＠]\s+(.+)$", subj)
    if m:
        return m.group(1).strip()[:200], m.group(2).strip()[:120]
    m = re.match(r"^(.+?)\s+at\s+(.+)$", subj, re.I)
    if m and len(m.group(1)) > 3:
        return m.group(1).strip()[:200], m.group(2).strip()[:120]
    return (subj[:200] if subj else ""), ""


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


def _indeed_jk(href: str) -> str | None:
    if not href:
        return None
    # Prefer explicit jk= / vjk=; ignore bare indeed.com without a job key.
    m = _INDEED_JK_RE.search(href)
    if m:
        return m.group(1).lower()
    # Some redirects encode jk later in the URL after unescape
    from urllib.parse import unquote
    m = _INDEED_JK_RE.search(unquote(href))
    return m.group(1).lower() if m else None


def parse_indeed_alert(html: str, subject: str = "") -> list[dict]:
    """Extract job postings from an Indeed job-alert email (HTML body)."""
    from bs4 import BeautifulSoup
    from urllib.parse import urlparse, parse_qs, unquote

    soup = BeautifulSoup(html or "", "html.parser")
    jobs: dict[str, dict] = {}
    anchor_of: dict[str, object] = {}

    for a in soup.find_all("a", href=True):
        href = unquote(a["href"] or "")
        # Skip obvious non-job indeed links
        if _INDEED_HOST_RE.search(href) is None and "jk=" not in href.lower():
            continue
        jk = _indeed_jk(href)
        if not jk:
            continue
        # Canonicalise URL
        url = f"https://www.indeed.com/viewjob?jk={jk}"
        # Prefer a cleaner host from the href when it's an indeed domain
        try:
            p = urlparse(href)
            if p.netloc and "indeed." in p.netloc.lower():
                qs = parse_qs(p.query)
                if "jk" in qs or "vjk" in qs:
                    url = f"https://{p.netloc.split('@')[-1]}/viewjob?jk={jk}"
        except Exception:
            pass

        job = jobs.setdefault(jk, {
            "job_key": f"indeed:{jk}",
            "title": "", "company": "", "location": "",
            "url": url,
        })
        anchor_of.setdefault(jk, a)
        text = _clean(a.get_text(" ", strip=True))
        if not text or len(text) < 2 or text.lower() in _BOILERPLATE:
            continue
        # Title is usually the link text; company rarely is.
        if not job["title"] and not text.lower().startswith("http"):
            job["title"] = text[:200]

    # Enrich company / location from nearby card text (stay local — don't walk
    # all the way to <body>, or every job inherits the last card's company).
    for jk, job in jobs.items():
        a = anchor_of.get(jk)
        if a is None:
            continue
        snippets: list[str] = []

        def _add_segs(node, limit: int = 6):
            if node is None:
                return
            for segment in getattr(node, "stripped_strings", []) or []:
                seg = _clean(segment)
                if not seg or seg == job["title"] or seg.lower() in _BOILERPLATE:
                    continue
                if seg.lower().startswith("http"):
                    continue
                if seg not in snippets:
                    snippets.append(seg)
                if len(snippets) >= limit:
                    return

        # 1) Immediate following siblings of the link
        for sib in (a.next_siblings or []):
            _add_segs(sib, 4)
            if len(snippets) >= 4:
                break
        # 2) Only if the link had no nearby text, try the parent's following siblings
        #    (don't do this when siblings already gave us lines — next card would leak in).
        parent = a.parent
        if parent is not None and not snippets:
            for sib in (parent.next_siblings or []):
                _add_segs(sib, 6)
                if len(snippets) >= 6:
                    break
        # 3) One level up only if that parent doesn't contain other job links
        if parent is not None and len(snippets) < 2:
            gp = parent.parent
            if gp is not None:
                other_jks = {
                    _indeed_jk(unquote(x.get("href") or ""))
                    for x in gp.find_all("a", href=True)
                }
                other_jks.discard(None)
                other_jks.discard(jk)
                if not other_jks:
                    _add_segs(gp, 6)
        for seg in snippets:
            pair = _split_company_loc(seg)
            if pair:
                if not job["company"]:
                    job["company"], job["location"] = pair
                break
        if not job["company"]:
            for seg in snippets:
                if seg == job["title"]:
                    continue
                if re.search(
                    r"(\$|₪|€|£|\d[\d,]+\s*/\s*(?:yr|year|mo|month|hr|hour))",
                    seg, re.I,
                ):
                    continue
                if re.match(r"^(today|just posted|\d+\s*day)", seg, re.I):
                    continue
                if 2 <= len(seg) <= 80 and not job["company"]:
                    job["company"] = seg[:120]
                    continue
                if job["company"] and not job["location"] and 2 <= len(seg) <= 80:
                    job["location"] = seg[:120]
                    break

    # Subject fallback for single-job alerts: "Title @ Company"
    subj_title, subj_company = _subject_title_company(subject)
    if len(jobs) == 1:
        job = next(iter(jobs.values()))
        if not job["title"] and subj_title:
            job["title"] = subj_title
        if not job["company"] and subj_company:
            job["company"] = subj_company
    elif not jobs and subj_title:
        # No parseable links — nothing to store without a stable key
        pass

    return [j for j in jobs.values() if j["title"]]


def parse_alert_email(html: str, subject: str = "",
                      from_addr: str = "") -> list[dict]:
    """Parse a job-alert email — LinkedIn and/or Indeed."""
    found: list[dict] = []
    if html:
        found.extend(parse_linkedin_alert(html))
        # Indeed alerts (and LinkedIn-labelled Indeed forwards) use indeed URLs
        indeed = parse_indeed_alert(html, subject=subject)
        # Prefer Indeed parse when the sender is Indeed or we found Indeed URLs
        if indeed:
            # Merge: Indeed jobs aren't in LinkedIn list (different keys)
            found.extend(indeed)
        elif not found and _INDEED_HOST_RE.search(from_addr or ""):
            # Sender is Indeed but HTML had no jk= — try subject-only is useless
            # without a key; leave empty.
            pass
    return found


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


def _header(msg: dict, name: str) -> str:
    for h in (msg.get("payload") or {}).get("headers") or []:
        if (h.get("name") or "").lower() == name.lower():
            return h.get("value") or ""
    return ""


def _label_names() -> list[str]:
    """GMAIL_LABEL may be a single label or comma-separated list."""
    raw = (config.GMAIL_LABEL or "").strip() or "linkedin-jobs"
    names = [p.strip() for p in raw.split(",") if p.strip()]
    return names or ["linkedin-jobs"]


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
        # Union of messages across all configured labels (LinkedIn + Indeed, …).
        message_ids: list[str] = []
        seen_mid: set[str] = set()
        for name in _label_names():
            label = _label_id(svc, name)
            page_token = None
            while True:
                listing = svc.users().messages().list(
                    userId="me", labelIds=[label], maxResults=100,
                    pageToken=page_token).execute()
                for m in listing.get("messages", []) or []:
                    mid = m["id"]
                    if mid not in seen_mid:
                        seen_mid.add(mid)
                        message_ids.append(mid)
                page_token = listing.get("nextPageToken")
                if not page_token or len(message_ids) >= 2000:
                    break

        with get_connection() as conn:
            seen = {r["gmail_id"] for r in
                    conn.execute("SELECT gmail_id FROM alert_emails")}
            # Re-parse emails we already saw but that yielded no jobs (e.g. Indeed
            # messages ingested before the Indeed parser existed).
            orphans = {
                r["gmail_id"] for r in conn.execute(
                    """SELECT e.gmail_id FROM alert_emails e
                        WHERE NOT EXISTS (
                          SELECT 1 FROM job_alerts a WHERE a.gmail_id = e.gmail_id
                        )""")
            }
        # New messages first, then orphans that are still under a scanned label
        new_ids = [mid for mid in message_ids if mid not in seen]
        retry_ids = [mid for mid in message_ids
                     if mid in orphans and mid not in set(new_ids)]
        to_process = new_ids + retry_ids

        new_emails = 0
        new_jobs = 0
        for mid in to_process:
            msg = svc.users().messages().get(
                userId="me", id=mid, format="full").execute()
            html = _html_body(msg.get("payload", {}))
            subject = _header(msg, "Subject")
            from_addr = _header(msg, "From")
            found = parse_alert_email(html, subject=subject, from_addr=from_addr)
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
                              gmail_id, alert_at, last_alert_at, created_at)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (job["job_key"], job["title"], job["company"],
                         job["location"], job["url"], mid, ts, ts, now_iso()))
                    new_jobs += cur.rowcount
                    if not cur.rowcount:
                        # Known job resurfacing: bump count, mark unread again
                        # (dismissed/ignored kept; badge only counts active).
                        conn.execute(
                            """UPDATE job_alerts
                                  SET times_seen = times_seen + 1,
                                      last_alert_at = MAX(COALESCE(last_alert_at,
                                                                   alert_at, ''), ?),
                                      seen = CASE WHEN ignored = 1 THEN seen ELSE 0 END
                                WHERE job_key = ?""",
                            (ts, job["job_key"]))
                conn.execute(
                    "INSERT OR IGNORE INTO alert_emails (gmail_id, fetched_at) "
                    "VALUES (?,?)", (mid, now_iso()))
            if mid in set(new_ids):
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
    from .tracker import _job_url_keys

    key = alert["job_key"] or ""
    alert_keys = {key} if key else set()
    # Also derive keys from the stored URL (Indeed jk, LinkedIn id, …).
    # job_alerts rows don't always have url in the SELECT — handle both shapes.
    url = ""
    try:
        url = alert["url"] or ""
    except (KeyError, IndexError, TypeError):
        url = ""
    alert_keys |= _job_url_keys(url)
    # LinkedIn legacy keys are bare numeric ids
    if key.isdigit():
        alert_keys.add(f"linkedin:{key}")
    if key.startswith("indeed:"):
        alert_keys.add(key)

    for app in apps:
        app_keys = _job_url_keys(app["url"] or "")
        # Legacy LinkedIn bare-id equality
        m = _JOB_URL_RE.search(app["url"] or "")
        if m:
            app_keys.add(m.group(1))
            app_keys.add(f"linkedin:{m.group(1)}")
        if alert_keys & app_keys:
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
            "SELECT id, job_key, title, company, url FROM job_alerts").fetchall()
        for alert in alerts:
            conn.execute("UPDATE job_alerts SET matched_app_id=? WHERE id=?",
                         (_match_one(alert, apps), alert["id"]))


# --------------------------------------------------------------------------- #
# Queries for the UI
# --------------------------------------------------------------------------- #
def list_alerts(include_dismissed: bool = False, ignored: bool = False,
                queue: bool = False):
    """Alerts for the UI.

    queue=True → daily action list: not applied, not dismissed/ignored,
    unread first. ignored=True → ignore list. include_dismissed → active+dismissed.
    """
    if ignored:
        q = "SELECT * FROM job_alerts WHERE ignored = 1"
        q += " ORDER BY alert_at DESC, id DESC"
    elif queue:
        q = ("SELECT * FROM job_alerts "
             "WHERE ignored = 0 AND dismissed = 0 AND matched_app_id IS NULL "
             "ORDER BY seen ASC, alert_at DESC, id DESC")
    elif include_dismissed:
        q = "SELECT * FROM job_alerts WHERE ignored = 0"
        q += " ORDER BY alert_at DESC, id DESC"
    else:
        q = "SELECT * FROM job_alerts WHERE ignored = 0 AND dismissed = 0"
        q += " ORDER BY seen ASC, alert_at DESC, id DESC"
    with get_connection() as conn:
        return conn.execute(q).fetchall()


def new_alert_count() -> int:
    """Unseen, non-dismissed, non-ignored, unmatched alerts (nav badge)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM job_alerts "
            "WHERE seen = 0 AND dismissed = 0 AND ignored = 0 "
            "AND matched_app_id IS NULL"
        ).fetchone()
        return int(row["n"])


def action_queue_count() -> int:
    """Active alerts still needing a decision (not applied / dismissed / ignored)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM job_alerts "
            "WHERE dismissed = 0 AND ignored = 0 AND matched_app_id IS NULL"
        ).fetchone()
        return int(row["n"])


def get_alert(alert_id: int):
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM job_alerts WHERE id=?", (alert_id,)
        ).fetchone()


def link_application(alert_id: int, app_id: int) -> None:
    """Point an alert at a captured application and mark it read."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE job_alerts SET matched_app_id=?, seen=1 WHERE id=?",
            (app_id, alert_id))


def mark_all_seen() -> None:
    """Reset the nav badge: acknowledge every alert currently in the list."""
    with get_connection() as conn:
        conn.execute("UPDATE job_alerts SET seen = 1 WHERE seen = 0")


def set_seen(alert_id: int, seen: bool = True) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE job_alerts SET seen=? WHERE id=?",
                     (1 if seen else 0, alert_id))


def set_seen_many(alert_ids: list[int], seen: bool = True) -> int:
    ids = [int(i) for i in alert_ids if i is not None]
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    with get_connection() as conn:
        cur = conn.execute(
            f"UPDATE job_alerts SET seen=? WHERE id IN ({placeholders})",
            (1 if seen else 0, *ids))
        return cur.rowcount


def set_dismissed(alert_id: int, dismissed: bool) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE job_alerts SET dismissed=?, seen=1 WHERE id=?",
            (1 if dismissed else 0, alert_id))


def set_dismissed_many(alert_ids: list[int], dismissed: bool = True) -> int:
    ids = [int(i) for i in alert_ids if i is not None]
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    with get_connection() as conn:
        cur = conn.execute(
            f"UPDATE job_alerts SET dismissed=?, seen=1 "
            f"WHERE id IN ({placeholders})",
            (1 if dismissed else 0, *ids))
        return cur.rowcount


def set_ignored(alert_id: int, ignored: bool) -> None:
    """Ignore list: the job stays counted but is hidden and never notifies."""
    with get_connection() as conn:
        conn.execute("UPDATE job_alerts SET ignored=?, seen=1 WHERE id=?",
                     (1 if ignored else 0, alert_id))


def set_ignored_many(alert_ids: list[int], ignored: bool = True) -> int:
    ids = [int(i) for i in alert_ids if i is not None]
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    with get_connection() as conn:
        cur = conn.execute(
            f"UPDATE job_alerts SET ignored=?, seen=1 "
            f"WHERE id IN ({placeholders})",
            (1 if ignored else 0, *ids))
        return cur.rowcount


def alert_url(alert_id: int) -> str | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT url FROM job_alerts WHERE id=?", (alert_id,)
        ).fetchone()
        return (row["url"] if row else None) or None


def set_comment(alert_id: int, comment: str) -> None:
    """Save a free-text note on an alert. Kept across resurfacing (same job_key)."""
    text = (comment or "").strip()
    with get_connection() as conn:
        conn.execute(
            "UPDATE job_alerts SET comment=? WHERE id=?",
            (text or None, alert_id))


def max_alert_id() -> int:
    """Highest alert row id — lets the UI detect that new alerts arrived."""
    with get_connection() as conn:
        row = conn.execute("SELECT MAX(id) AS m FROM job_alerts").fetchone()
        return int(row["m"] or 0)


# --------------------------------------------------------------------------- #
# Background auto-fetch (every AUTO_FETCH_INTERVAL_S while the server runs)
# --------------------------------------------------------------------------- #
_auto_started = False
_auto_lock = threading.Lock()


def start_auto_fetch() -> None:
    """Fetch alerts periodically in the background. Safe to call repeatedly."""
    global _auto_started
    with _auto_lock:
        if _auto_started:
            return
        _auto_started = True
    threading.Thread(target=_auto_loop, name="jobtracker-alerts-fetch",
                     daemon=True).start()


def _auto_loop() -> None:
    time.sleep(30)  # let the server finish starting first
    while True:
        try:
            if is_connected():
                fetch_alerts()
        except Exception:
            pass  # best-effort; the manual Fetch button surfaces errors
        time.sleep(AUTO_FETCH_INTERVAL_S)
