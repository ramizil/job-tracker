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
from html import escape, unescape

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
    r"(?:thank(s| you) for (your )?(applying|application|interest)|"
    r"we (have )?received your application|application (was )?received|"
    r"we got (?:your application|it))",
    re.I)
# Still in play — not a rejection (body must not contain decision language).
_IN_PROGRESS_ONLY = re.compile(
    r"we (?:have )?received your application|application (?:has been |was )received|"
    r"we got (?:your application|it)|successfully received|"
    r"(?:under|being|currently) review|in the review process|reviewing your application|"
    r"(?:we(?:'re| are)|our team is) reviewing|"
    r"recruiter will (?:be in )?touch|will be in touch(?: with you)?(?: soon)?|"
    r"contact you (?:soon|shortly)|keep you (?:updated|posted|informed)|"
    r"next steps (?:in|of) (?:the|our) (?:process|hiring)|"
    r"move forward with your (?:application|candidacy)|"
    r"would like to (?:schedule|invite)|pleased to invite|congratulations|"
    r"בקשתך התקבלה|קיבלנו את מועמדותך|בבדיקה|ניצור עמך קשר",
    re.I)
# Decision language — must appear in the BODY for a definite rejection.
_DECIDED_MARKERS = re.compile(
    r"after (?:careful )?review(?:ing)?(?: applications)?,?\s*we(?:'ve| have) decided|"
    r"we(?:'ve| have) decided (?:not to|to move forward with other)|"
    r"we regret|unfortunately|regret to inform|"
    r"not (?:be )?moving forward(?: with your application)?|"
    r"not to move forward(?: with your application)?|"
    r"will not be (?:moving|proceeding)(?: with your application)?|"
    r"won'?t be proceeding|cannot offer you|unable to (?:move forward|offer)|"
    r"not selected|other candidates|chosen to pursue other|pursue other candidates|"
    r"not advance(?: to the next stage)?|no longer under consideration|"
    r"position (?:has been |is )filled|was not successful|not be progressing|"
    r"will not be able to offer|decided not to proceed|"
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


def highlight_rejection_body(text: str) -> str:
    """Return escaped email body with definite-rejection phrases highlighted."""
    if not text:
        return ""
    safe = escape(text)

    def _mark(m: re.Match) -> str:
        return f'<mark class="rej-hit">{m.group(0)}</mark>'

    return _DECIDED_MARKERS.sub(_mark, safe)


def _looks_like_rejection(subject: str, from_addr: str, text: str) -> bool:
    """True only when the email body contains definite rejection language.

    Subjects and Gmail labels are unreliable — "Thank you for applying" and
    broad filters catch acks and in-review mail. We require decision wording
    in the body (unfortunately, decided not to move forward, other candidates…).
    """
    body = (text or "").strip()
    if not body:
        return False
    if _DECIDED_MARKERS.search(body):
        return True
    # Ack / in-review only — never show as a rejection.
    if _IN_PROGRESS_ONLY.search(body):
        return False
    if _ACK_ONLY.search(subject or "") and not _DECIDED_MARKERS.search(body):
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


def _valid_company_name(name: str) -> bool:
    low = name.lower().strip()
    if len(name) < 2 or len(name) > 60:
        return False
    junk = {
        "no reply", "noreply", "do not reply", "recruiting", "recruitment",
        "talent", "hr", "hiring", "team", "human resources", "people",
        "application", "your", "our", "dear", "hi", "hello", "the",
        "candidate", "candidates",
    }
    if low in junk:
        return False
    words = [w for w in re.split(r"\s+", low) if w]
    if words and all(w in junk for w in words):
        return False
    return True


def _clean_company_name(name: str) -> str:
    name = _clean(name)
    name = re.sub(r"^the\s+", "", name, flags=re.I)
    name = re.sub(r"(?:'s|'s)$", "", name.strip(), flags=re.I).strip(" .,-")
    name = re.sub(r"\s+(?:recruiting|recruitment|talent|hr|hiring|people)$",
                  "", name, flags=re.I).strip()
    return name


def _parse_signature_company(text: str, out: dict) -> None:
    """Extract company from email sign-off (e.g. \"Regards, Tenable's Recruiting Team\")."""
    if out.get("company"):
        return
    raw = text or ""
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    tail = "\n".join(lines[-14:]) if lines else raw[-900:]
    if len(tail) < 15:
        tail = raw[-900:]

    team = (
        r"(?:recruiting|recruitment|talent(?:\s+acquisition)?|hr|hiring|"
        r"people|human resources)(?:\s+team)?"
    )
    patterns = [
        # Regards, Tenable's Recruiting Team
        rf"(?:regards|best regards|kind regards|sincerely|thanks|thank you|"
        rf"בברכה|בתודה)\s*,?\s*(?:the\s+)?(.+?)(?:'s|')?\s+{team}",
        # Tenable's Recruiting Team (no salutation)
        rf"(?:^|\n)\s*(?:the\s+)?(.+?)(?:'s|')?\s+{team}\s*$",
        # Regards, The Acme Team
        r"(?:regards|best regards|kind regards|sincerely|thanks|thank you)\s*,?\s*"
        r"(?:the\s+)?(.+?)\s+team\s*$",
        # ...from the Tenable team
        rf"from (?:the\s+)?(.+?)(?:'s|')?\s+{team}",
    ]
    for pat in patterns:
        m = re.search(pat, tail, re.I | re.M)
        if not m:
            continue
        company = _clean_company_name(m.group(1))
        if company and _valid_company_name(company):
            out["company"] = company
            return


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
    """Return parsed rejection fields, or None if this isn't a definite rejection.

    ``from_label`` only means the email came from the user's Gmail label — we
    still require rejection wording in the body so acks / in-review mail are
    dropped even when the label is broad.
    """
    text = plain or _html_to_text(html)
    if not _looks_like_rejection(subject, from_addr, text):
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
    _parse_signature_company(text, out)

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


def _company_matches(a_company: str, app_company: str) -> bool:
    ac = _norm(a_company)
    c = _norm(app_company)
    if not ac or not c:
        return False
    return (c == ac or c in ac or ac in c or _sim(c, ac) >= 0.82)


def _title_matches(a_title: str, app_title: str, *, min_score: float = 0.5) -> bool:
    at = _norm(a_title)
    t = _norm(app_title)
    if not at or not t:
        return False
    return _sim(t, at) >= min_score


def _app_matches_parsed(app, *, company: str, title: str) -> bool:
    if app["status"] in ("rejected", "withdrawn"):
        return False
    a_company = (company or "").strip()
    a_title = (title or "").strip()
    if not a_company and not a_title:
        return True
    company_ok = _company_matches(a_company, app["company"]) if a_company else True
    title_ok = _title_matches(a_title, app["title"]) if a_title else True
    if a_company and a_title:
        return company_ok and title_ok
    if a_company:
        return company_ok
    return title_ok


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
        company_ok = _company_matches(a_company, app["company"]) if a_company else False
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
            rd = dict(row)
            if not rd.get("company") and rd.get("body_text"):
                patched = {"title": rd.get("title") or "", "company": ""}
                _parse_signature_company(rd["body_text"], patched)
                if patched["company"]:
                    rd["company"] = patched["company"]
                    conn.execute(
                        "UPDATE rejection_inbox SET company=? WHERE id=?",
                        (patched["company"], row["id"]))
            app_id, conf = _match_one(rd, apps)
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
                         plain[:12000], parsed.get("title", ""),
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
    _dismiss_noise()
    return {
        "emails": new_emails,
        "rejections": new_rejections,
        "used_fallback": used_fallback,
    }


# --------------------------------------------------------------------------- #
# UI queries / actions
# --------------------------------------------------------------------------- #
def _dismiss_noise() -> None:
    """Auto-dismiss inbox rows that fail the definite-rejection body check."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, subject, from_addr, body_text, snippet "
            "FROM rejection_inbox WHERE status = 'pending'"
        ).fetchall()
        for r in rows:
            if not _looks_like_rejection(
                    r["subject"] or "", r["from_addr"] or "",
                    r["body_text"] or r["snippet"] or ""):
                conn.execute(
                    "UPDATE rejection_inbox SET status='dismissed', seen=1 "
                    "WHERE id=?", (r["id"],))


def list_inbox(*, include_dismissed: bool = False):
    if include_dismissed:
        q = "SELECT * FROM rejection_inbox WHERE status != 'confirmed'"
    else:
        q = "SELECT * FROM rejection_inbox WHERE status = 'pending'"
    q += " ORDER BY mail_at DESC, id DESC"
    with get_connection() as conn:
        rows = conn.execute(q).fetchall()
    # Re-check stored body — drop rows imported before stricter filtering.
    return [r for r in rows
            if _looks_like_rejection(r["subject"] or "", r["from_addr"] or "",
                                     r["body_text"] or r["snippet"] or "")]


def pending_count() -> int:
    """Unseen pending rejections with a match — nav badge."""
    return sum(1 for r in list_inbox()
               if not r["seen"] and r["matched_app_id"])


def confirm_count() -> int:
    """All pending rejection emails waiting for user confirmation."""
    return len(list_inbox())


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


def list_applications_for_picker(*, company: str = "", title: str = "",
                                 matched_app_id: int | None = None):
    """Applications for the confirm dropdown — filtered by parsed company name.

    Title is shown for context but not used for filtering (rejection subjects
    often differ slightly from the stored application title). Rejected
    applications are included so a mailbox rejection can still be linked.
    """
    with get_connection() as conn:
        apps = conn.execute(
            """SELECT id, company, title, status, date_applied
                 FROM applications
                WHERE status != 'withdrawn'
                ORDER BY date_applied DESC, id DESC"""
        ).fetchall()

    company = (company or "").strip()
    if not company:
        return list(apps)

    matched = [a for a in apps if _company_matches(company, a["company"])]
    if matched_app_id:
        have = {a["id"] for a in matched}
        if matched_app_id not in have:
            with get_connection() as conn:
                extra = conn.execute(
                    "SELECT id, company, title, status, date_applied "
                    "FROM applications WHERE id=?", (matched_app_id,)
                ).fetchone()
            if extra:
                matched = [extra] + matched
    return matched


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
