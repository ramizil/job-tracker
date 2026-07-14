"""Read rejection emails from Gmail and match them to applications.

A dedicated mailbox (e.g. ``zilber.rami@gmail.com``) collects rejection
notifications. We fetch messages under a Gmail label (or a built-in search
query when the label is missing), parse company/title/stage from each email,
and cross-check against the applications list. Nothing is auto-marked rejected
— the UI lets you confirm, dismiss, or fix a wrong match.

Auth uses the same Desktop-app OAuth client as Sheets/alerts, but a separate
per-profile token (``gmail_rejections_token.json``) — sign in with the
rejections mailbox, which may differ from the job-alerts account.
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
from difflib import SequenceMatcher
from html import unescape

from . import config
from .db import get_connection, now_iso

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
AUTO_FETCH_INTERVAL_S = 600  # 10 minutes

# Gmail search used when the configured label doesn't exist yet.
# Subjects alone are unreliable — the query casts a wide net; body parsing
# below decides what is a real rejection vs a mere "thanks for applying".
_FALLBACK_QUERY = (
    "(\"thank you for applying\" OR \"update on your application\" "
    "OR \"your application\" OR \"interest in joining\" OR unfortunately "
    "OR \"not selected\" OR \"not moving\" OR \"other candidates\" "
    "OR \"after reviewing\" OR \"תודה על פנייתך\" OR \"לא נבחר\") "
    "-in:spam -in:trash newer_than:1y"
)

_JOB_URL_RE = re.compile(
    r"https?://[^\s\"'<>]+(?:comeet\.com/jobs|boards\.greenhouse\.io|"
    r"jobs\.lever\.co|linkedin\.com/(?:comm/)?jobs/view|"
    r"careers\.smartrecruiters\.com|myworkdayjobs\.com)[^\s\"'<>]*",
    re.I)

_LINKEDIN_JOB_RE = re.compile(
    r"linkedin\.com/(?:comm/)?jobs/view/(?:[^\s/?#]*?-)?(\d{6,})")

_ACK_ONLY = re.compile(
    r"(?:thank(s| you) for (your )?(applying|application)|"
    r"we (have )?received your application|application (was )?received)",
    re.I)
# Decision language — usually in the BODY, not the subject.
_DECIDED_MARKERS = re.compile(
    r"after reviewing|we(?:'ve| have) decided|we regret|unfortunately|"
    r"regret to inform|not (?:be )?moving forward|not to move forward|"
    r"will not be (?:moving|proceeding)|won'?t be proceeding|"
    r"other candidates|not selected|cannot offer you|unable to move forward|"
    r"chosen to pursue other|pursue other candidates|not advance|"
    r"no longer under consideration|position (?:has been |is )filled|"
    r"was not successful|not be progressing|"
    r"לא נבחר|לא נמשיך|מועמדים אחרים|מצטערים|לא נבחרת",
    re.I)
_REJECTION_MARKERS = _DECIDED_MARKERS  # alias for note extraction


class RejectionsError(Exception):
    """User-readable Gmail rejection-inbox failure."""


def _token_path():
    return config.PROFILE_DIR / "gmail_rejections_token.json"


def is_connected() -> bool:
    return _token_path().exists()


def connect() -> None:
    """Run the one-time OAuth browser flow for the rejections mailbox."""
    from pathlib import Path

    secret = Path(str(config.GOOGLE_CLIENT_SECRET))
    if not secret.exists():
        raise RejectionsError(
            f"OAuth client file not found at {secret}. It's the same Desktop-app "
            "client JSON used for Google Sheets — set its path in Settings.")
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise RejectionsError(
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
        raise RejectionsError("Rejections Gmail isn't connected yet — click "
                              "\u201cConnect rejections Gmail\u201d in Settings.")
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_info(
        json.loads(token_path.read_text(encoding="utf-8")), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
    if not creds.valid:
        raise RejectionsError("Rejections Gmail login expired — reconnect in "
                              "Settings.")
    return creds


def _service():
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RejectionsError(
            "Google client libraries are missing — restart via start.command "
            "to install dependencies.") from exc
    return build("gmail", "v1", credentials=_credentials())


def _label_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")


def _label_id(svc, name: str) -> str | None:
    labels = svc.users().labels().list(userId="me").execute().get("labels", [])
    want = _label_key(name)
    for lab in labels:
        if _label_key(lab.get("name", "")) == want:
            return lab["id"]
    return None


def _clean(text: str) -> str:
    return " ".join(unescape(text or "").split())


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return _clean(soup.get_text("\n", strip=True))


def _header(headers: list[dict], name: str) -> str:
    want = name.lower()
    for h in headers or []:
        if (h.get("name") or "").lower() == want:
            return h.get("value") or ""
    return ""


def _html_body(payload: dict) -> str:
    stack = [payload]
    while stack:
        part = stack.pop()
        if part.get("mimeType") == "text/html":
            data = part.get("body", {}).get("data")
            if data:
                return base64.urlsafe_b64decode(data + "===").decode(
                    "utf-8", errors="replace")
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data")
            if data:
                return base64.urlsafe_b64decode(data + "===").decode(
                    "utf-8", errors="replace")
        stack.extend(part.get("parts", []) or [])
    return ""


def _looks_like_rejection(subject: str, from_addr: str, text: str) -> bool:
    """True when the email BODY (not just subject) signals a rejection.

    Many ATS emails use polite subjects like "Thank you for applying…" and
    only say "unfortunately / after reviewing we've decided…" in the body.
    """
    subj = subject or ""
    body = text or ""
    combined = f"{subj}\n{body}"
    if _DECIDED_MARKERS.search(body):
        return True
    if _DECIDED_MARKERS.search(subj):
        return True
    # "Thanks for applying" with no decision text yet → still in review, skip.
    if _ACK_ONLY.search(subj) and not _DECIDED_MARKERS.search(body):
        return False
    return False


def _guess_stage(subject: str, text: str) -> str:
    blob = f"{subject}\n{text}".lower()
    if re.search(r"interview|ראיון", blob):
        if re.search(r"technical|טכני|coding|home assignment|מטלה", blob):
            return "technical_interview"
        if re.search(r"manager|מנהל", blob):
            return "manager_interview"
        return "hr_interview"
    if re.search(r"phone screen|recruiter|מגייס", blob):
        return "recruiter_screen"
    if re.search(r"cv|resume|ats|מועמדות", blob):
        return "cv_screen"
    return "cv_screen"


def _guess_reason(text: str) -> str:
    blob = (text or "").lower()
    if re.search(r"filled|closed|no longer accepting|התפקיד אינו", blob):
        return "position_closed"
    if re.search(r"experience|ניסיון", blob):
        return "experience_gap"
    if re.search(r"skill|כישור", blob):
        return "missing_skill"
    if re.search(r"salary|שכר", blob):
        return "salary_mismatch"
    return "no_feedback"


def _extract_urls(blob: str) -> list[str]:
    urls = []
    for m in _JOB_URL_RE.finditer(blob or ""):
        u = m.group(0).rstrip(").,;]")
        if u not in urls:
            urls.append(u)
    return urls


def _parse_subject(subject: str, out: dict) -> None:
    subj = _clean(subject)
    patterns = [
        # "Thank you for applying for the QA Engineer position at Untrama"
        r"thank you for applying for the (.+?) position at (.+?)(?:\s*[-–—]|$)",
        # LinkedIn: Your application was not selected for TITLE at COMPANY
        r"(?:not selected|update).{0,40}?\bfor\s+(.+?)\s+at\s+(.+?)(?:\s*[-–|]|$)",
        r"your application (?:to|for)\s+(.+?)\s+at\s+(.+?)(?:\s*[-–|]|$)",
        r"application (?:to|for)\s+(.+?)\s+at\s+(.+?)(?:\s*[-–|]|$)",
        r"update (?:on|regarding) your application (?:to|for)\s+(.+?)\s+at\s+(.+?)(?:\s*[-–|]|$)",
        # "Update on your application - Untrama - Hi Rami…"
        r"update on your application\s*[-–—]\s*([^-–—]+?)(?:\s*[-–—]|$)",
        # "Thanks for your recent interest in joining Varonis"
        r"interest in joining (.+?)(?:\s*[-–—]|$)",
    ]
    for pat in patterns:
        m = re.search(pat, subj, re.I)
        if not m:
            continue
        if m.lastindex == 1:
            company = _clean(m.group(1))
            if company and not out["company"]:
                out["company"] = company
            continue
        a, b = _clean(m.group(1)), _clean(m.group(2))
        if len(a) < 2 or len(b) < 2:
            continue
        if not out["title"]:
            out["title"] = a if "application" not in a.lower() else b
        if not out["company"]:
            out["company"] = b if out["title"] == a else a
        if out["title"] and out["company"]:
            break


def _parse_body(text: str, out: dict) -> None:
    """Extract title + company from the email body (where ATS puts the truth)."""
    blob = _clean(text)
    # (regex, title_group, company_group) — 1-based group index; None = absent
    patterns: list[tuple[str, int, int | None]] = [
        (r"applying for the (.+?) position at (.+?)(?:[.,;]| and | but |$)", 1, 2),
        (r"apply(?:ing|ied) (?:to|for) the (.+?) position at (.+?)(?:[.,;]| and |$)", 1, 2),
        (r"for the (.+?) (?:position|role) at (.+?)(?:[.,;]| and |$)", 1, 2),
        (r"considering (.+?) as your next .{0,50}?applying for the role of (.+?)(?:[.,;]|$)",
         2, 1),
        (r"applying for the role of (.+?)(?:[.,;]| unfortunately|$)", 1, None),
    ]
    for pat, tg, cg in patterns:
        m = re.search(pat, blob, re.I)
        if not m:
            continue
        title = _clean(m.group(tg)) if tg else ""
        company = (_clean(m.group(cg))
                   if cg and m.lastindex and m.lastindex >= cg else "")
        if title and len(title) > 2 and not out["title"]:
            out["title"] = title
        if company and len(company) > 1 and not out["company"]:
            out["company"] = company
        if out["title"] and out["company"]:
            break


def _parse_from_display(from_addr: str, out: dict) -> None:
    """Use the sender display name when it is the company (Cyera, Varonis…)."""
    m = re.match(r"^([^<]+)<", from_addr or "")
    if not m:
        return
    name = _clean(m.group(1))
    low = name.lower()
    if not name or low in ("no reply", "noreply", "do not reply"):
        return
    if any(x in low for x in ("recruiting", "recruitment", "talent", "hr")):
        name = re.sub(r"\b(recruiting|recruitment|talent|hr)\b", "", name,
                      flags=re.I).strip(" .")
    if name and not out["company"]:
        out["company"] = name


def _parse_company_from_from_header(from_addr: str) -> str:
    m = re.search(r"<[^@]+@([^.>]+)", from_addr or "")
    if not m:
        return ""
    host = m.group(1).lower()
    if host in ("comeet", "greenhouse", "lever", "linkedin", "smartrecruiters"):
        return ""
    return host.replace("-", " ").title()


def _extract_note(text: str, limit: int = 280) -> str:
    for line in (text or "").splitlines():
        line = _clean(line)
        if len(line) < 30:
            continue
        if _REJECTION_MARKERS.search(line):
            return line[:limit]
    return _clean(text)[:limit]


def parse_rejection_email(*, subject: str, from_addr: str,
                          html: str, plain: str,
                          from_label: bool = False) -> dict | None:
    """Return parsed rejection fields, or None if this isn't a rejection.

    When ``from_label`` is True (email already under the user's rejection
    label), we trust the label and always parse — the body/subject extract
    company and title even if detection heuristics would skip it.
    """
    text = plain or _html_to_text(html)
    if not from_label and not _looks_like_rejection(subject, from_addr, text):
        return None

    out = {
        "title": "", "company": "", "job_url": "",
        "stage": _guess_stage(subject, text),
        "reason": _guess_reason(text),
        "note": _extract_note(text),
        "snippet": _clean(subject)[:200],
    }

    blob = f"{subject}\n{text}\n{html or ''}"
    urls = _extract_urls(blob)
    if urls:
        out["job_url"] = urls[0]

    _parse_body(text, out)
    _parse_from_display(from_addr, out)
    _parse_subject(subject, out)

    if not out["company"]:
        out["company"] = _parse_company_from_from_header(from_addr)

    if not out["snippet"] and out["note"]:
        out["snippet"] = out["note"][:200]

    return out


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9\u0590-\u05ff ]+", " ", (s or "").lower()).strip()


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _match_one(row: dict, apps) -> tuple[int | None, str]:
    """Return (app_id, confidence)."""
    url = row.get("job_url") or ""
    for app in apps:
        if url and app["url"] and _norm(url) == _norm(app["url"]):
            return app["id"], "high"
        m = _LINKEDIN_JOB_RE.search(url)
        n = _LINKEDIN_JOB_RE.search(app["url"] or "")
        if m and n and m.group(1) == n.group(1):
            return app["id"], "high"

    a_company = _norm(row.get("company"))
    a_title = _norm(row.get("title"))
    if not a_company and not a_title:
        return None, "none"

    best_id, best_score = None, 0.0
    for app in apps:
        if app["status"] in ("rejected", "withdrawn"):
            continue
        c = _norm(app["company"])
        t = _norm(app["title"])
        if not c and not t:
            continue
        company_ok = False
        if a_company and c:
            company_ok = (c == a_company or c in a_company or a_company in c
                          or _sim(c, a_company) >= 0.82)
        title_score = _sim(t, a_title) if a_title and t else 0.0
        if a_company and a_title and company_ok and title_score >= 0.5:
            score = 0.5 + title_score * 0.5
        elif a_company and company_ok and not a_title:
            score = 0.7
        elif a_title and title_score >= 0.72 and not a_company:
            score = title_score
        else:
            continue
        if score > best_score:
            best_score = score
            best_id = app["id"]

    if best_id and best_score >= 0.85:
        return best_id, "high"
    if best_id and best_score >= 0.6:
        return best_id, "medium"
    if best_id:
        return best_id, "low"
    return None, "none"


def refresh_matches() -> None:
    with get_connection() as conn:
        apps = conn.execute(
            "SELECT id, company, title, url, status FROM applications"
        ).fetchall()
        rows = conn.execute(
            "SELECT * FROM rejection_inbox WHERE status = 'pending'"
        ).fetchall()
        for row in rows:
            app_id, conf = _match_one(dict(row), apps)
            conn.execute(
                """UPDATE rejection_inbox
                      SET matched_app_id=?, match_confidence=?
                    WHERE id=?""",
                (app_id, conf, row["id"]))


# --------------------------------------------------------------------------- #
# Fetch + store
# --------------------------------------------------------------------------- #
def fetch_rejections() -> dict:
    """Pull new rejection emails, parse and store. Returns a summary."""
    svc = _service()
    try:
        from googleapiclient.errors import HttpError
    except ImportError as exc:
        raise RejectionsError("Google client libraries are missing.") from exc

    label_name = config.GMAIL_REJECTION_LABEL
    label = _label_id(svc, label_name)
    used_fallback = label is None

    message_ids: list[str] = []
    page_token = None
    while True:
        kwargs: dict = {"userId": "me", "maxResults": 100, "pageToken": page_token}
        if label:
            kwargs["labelIds"] = [label]
        else:
            kwargs["q"] = _FALLBACK_QUERY
        listing = svc.users().messages().list(**kwargs).execute()
        message_ids.extend(m["id"] for m in listing.get("messages", []))
        page_token = listing.get("nextPageToken")
        if not page_token or len(message_ids) >= 2000:
            break

    with get_connection() as conn:
        seen = {r["gmail_id"] for r in
                conn.execute("SELECT gmail_id FROM rejection_mail_seen")}

    new_ids = [mid for mid in message_ids if mid not in seen]
    new_emails = 0
    new_rejections = 0

    try:
        for mid in new_ids:
            msg = svc.users().messages().get(
                userId="me", id=mid, format="full").execute()
            headers = msg.get("payload", {}).get("headers", [])
            subject = _header(headers, "Subject")
            from_addr = _header(headers, "From")
            html = _html_body(msg.get("payload", {}))
            plain = ""
            if html and "<" in html:
                plain = _html_to_text(html)
            elif html:
                plain = html

            parsed = parse_rejection_email(
                subject=subject, from_addr=from_addr, html=html, plain=plain,
                from_label=bool(label))
            try:
                from datetime import datetime, timezone
                ts = datetime.fromtimestamp(
                    int(msg.get("internalDate", 0)) / 1000, tz=timezone.utc
                ).isoformat(timespec="seconds")
            except (TypeError, ValueError, OSError):
                ts = now_iso()

            with get_connection() as conn:
                if parsed:
                    app_id, conf = _match_one(parsed, conn.execute(
                        "SELECT id, company, title, url, status FROM applications"
                    ).fetchall())
                    cur = conn.execute(
                        """INSERT OR IGNORE INTO rejection_inbox
                             (gmail_id, subject, from_addr, snippet, body_text,
                              title, company, stage, reason, note, job_url,
                              mail_at, matched_app_id, match_confidence,
                              status, seen, created_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (mid, subject, from_addr, parsed.get("snippet", ""),
                         plain[:4000], parsed.get("title", ""),
                         parsed.get("company", ""), parsed.get("stage", "cv_screen"),
                         parsed.get("reason", "no_feedback"),
                         parsed.get("note", ""), parsed.get("job_url", ""),
                         ts, app_id, conf, "pending", 0, now_iso()))
                    new_rejections += cur.rowcount
                conn.execute(
                    "INSERT OR IGNORE INTO rejection_mail_seen (gmail_id, fetched_at) "
                    "VALUES (?,?)", (mid, now_iso()))
            new_emails += 1
    except HttpError as exc:
        if exc.resp.status == 403:
            raise RejectionsError(
                "Gmail API access denied — enable the Gmail API for your "
                "Google Cloud project, then reconnect.") from exc
        raise RejectionsError(f"Gmail API error: {exc.reason or exc}") from exc

    refresh_matches()
    return {
        "emails": new_emails,
        "rejections": new_rejections,
        "used_fallback": used_fallback,
    }


# --------------------------------------------------------------------------- #
# UI queries / actions
# --------------------------------------------------------------------------- #
def list_inbox(*, include_dismissed: bool = False):
    if include_dismissed:
        q = "SELECT * FROM rejection_inbox WHERE status != 'confirmed'"
    else:
        q = "SELECT * FROM rejection_inbox WHERE status = 'pending'"
    q += " ORDER BY mail_at DESC, id DESC"
    with get_connection() as conn:
        return conn.execute(q).fetchall()


def pending_count() -> int:
    """Unseen pending rejections with a match — nav badge."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM rejection_inbox "
            "WHERE status = 'pending' AND seen = 0 AND matched_app_id IS NOT NULL"
        ).fetchone()
        return int(row["n"])


def mark_all_seen() -> None:
    with get_connection() as conn:
        conn.execute("UPDATE rejection_inbox SET seen = 1 WHERE seen = 0")


def set_dismissed(row_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE rejection_inbox SET status='dismissed', seen=1 WHERE id=?",
            (row_id,))


def set_match(row_id: int, app_id: int | None) -> None:
    with get_connection() as conn:
        conf = "high" if app_id else "none"
        conn.execute(
            """UPDATE rejection_inbox
                  SET matched_app_id=?, match_confidence=?, status='pending'
                WHERE id=?""",
            (app_id, conf, row_id))


def confirm(row_id: int, *, app_id: int, stage: str, reason: str,
            note: str) -> bool:
    """Mark the matched application rejected and close the inbox row."""
    from . import tracker

    if not tracker.set_rejection(app_id, stage=stage, reason=reason, note=note):
        return False
    with get_connection() as conn:
        conn.execute(
            """UPDATE rejection_inbox
                  SET status='confirmed', matched_app_id=?, stage=?, reason=?,
                      note=?, seen=1
                WHERE id=?""",
            (app_id, stage, reason, note, row_id))
    return True


def max_inbox_id() -> int:
    with get_connection() as conn:
        row = conn.execute("SELECT MAX(id) AS m FROM rejection_inbox").fetchone()
        return int(row["m"] or 0)


def list_applications_for_picker():
    """Active applications for the manual-match dropdown."""
    with get_connection() as conn:
        return conn.execute(
            """SELECT id, company, title, status, date_applied
                 FROM applications
                WHERE status NOT IN ('rejected', 'withdrawn')
                ORDER BY date_applied DESC, id DESC"""
        ).fetchall()


# --------------------------------------------------------------------------- #
# Background auto-fetch
# --------------------------------------------------------------------------- #
_auto_started = False
_auto_lock = threading.Lock()


def start_auto_fetch() -> None:
    global _auto_started
    with _auto_lock:
        if _auto_started:
            return
        _auto_started = True
    threading.Thread(target=_auto_loop, name="jobtracker-rejections-fetch",
                     daemon=True).start()


def _auto_loop() -> None:
    time.sleep(45)  # stagger from job-alerts fetch
    while True:
        try:
            if is_connected():
                fetch_rejections()
        except Exception:
            pass
        time.sleep(AUTO_FETCH_INTERVAL_S)
