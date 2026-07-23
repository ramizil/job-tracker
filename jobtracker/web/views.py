"""All web routes for the job-tracker dashboard."""
from __future__ import annotations

import html as html_lib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from datetime import datetime

from flask import (
    Blueprint, Response, abort, flash, make_response, redirect,
    render_template, request, send_file, url_for,
)

from .. import (ai, analytics, backup, config, exporter, gitbackup, gsheets,
                gmail_alerts, gmail_rejections, pitch, resumes, search_hidden,
                search_meta, syncstatus, tracker, tts,
                usage)
from .. import profiles as profiles_mod
from .. import resume as resume_mod
from ..matcher import score_job
from ..models import COMMON_REJECTION_REASONS, REJECTION_STAGES, STATUSES
from ..sources import get_sources, job_matches_query
from ..sources.base import JobResult
from ..db import now_iso

bp = Blueprint("main", __name__)


@bp.app_context_processor
def inject_saved_alert():
    """Expose a saved-jobs reminder count to every template (nav badge)."""
    try:
        rem = analytics.saved_reminders()
        return {"saved_alert": {"count": rem["count"], "stale": rem["stale"]}}
    except Exception:
        return {"saved_alert": {"count": 0, "stale": 0}}


@bp.app_context_processor
def inject_alerts_badge():
    """Unread job alerts (mailbox-style) — nav badge count."""
    try:
        return {"alerts_badge": gmail_alerts.new_alert_count()}
    except Exception:
        return {"alerts_badge": 0}


@bp.app_context_processor
def inject_sync_status():
    """Last backup / Sheets sync times for the top-bar confidence chip."""
    try:
        return {"sync_status": syncstatus.status_summary()}
    except Exception:
        return {"sync_status": {}}


@bp.app_context_processor
def inject_rejections_badge():
    """Pending matched rejection emails — nav badge count."""
    try:
        return {"rejections_badge": gmail_rejections.pending_count()}
    except Exception:
        return {"rejections_badge": 0}


@bp.app_context_processor
def inject_profiles():
    """Expose the profile list + active name to every template (topbar switcher)."""
    try:
        return {"profiles": profiles_mod.list_profiles(),
                "active_profile": config.ACTIVE_PROFILE}
    except Exception:
        return {"profiles": [], "active_profile": ""}

def _tailored_path(app_id: int):
    return config.TAILORED_DIR / f"{app_id}.html"


def _tailored_draft_path(app_id: int):
    """Pending AI regeneration of the tailored resume, awaiting review."""
    return config.TAILORED_DIR / f"{app_id}.draft.html"


def _readiness() -> dict:
    """Are the mandatory settings (a usable resume + a configured AI) in place?

    Used to gate the Paste-a-job and Search flows, which both rely on the
    resume profile and AI features.
    """
    issues: list[str] = []

    rp = Path(str(config.RESUME_PATH)) if config.RESUME_PATH else None
    if not rp or not rp.exists():
        resume_ok = False
        issues.append(
            f"Your resume file wasn't found — set a valid Resume path in Settings "
            f"(looked for: {rp}).")
    elif rp.suffix.lower() not in resume_mod.SUPPORTED_RESUME_EXTS:
        resume_ok = False
        issues.append(
            f"Resume type “{rp.suffix}” isn't supported — use HTML, PDF, "
            "Word (.docx) or a text file, then update the Resume path in Settings.")
    else:
        resume_ok = True

    ai_ok = ai.is_configured()
    if not ai_ok:
        issues.append(
            f"AI isn't configured — add your {ai.provider_label()} API key on the "
            "Settings page (Gemini has a free tier).")

    return {"resume_ok": resume_ok, "ai_ok": ai_ok,
            "ready": resume_ok and ai_ok, "issues": issues,
            "resume_path": str(rp) if rp else ""}


def _browser_candidates() -> list[str]:
    """Per-OS locations of Chromium-based browsers for headless PDF rendering."""
    if sys.platform == "darwin":
        return [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    if os.name == "nt":
        return [
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
    return [
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/microsoft-edge",
        "/snap/bin/chromium",
    ]


def _find_browser() -> str | None:
    """Locate a local Chromium-based browser (Edge/Chrome) for headless PDF."""
    env = os.environ.get("JOBTRACKER_BROWSER")
    if env and os.path.exists(env):
        return env
    for p in _browser_candidates():
        if os.path.exists(p):
            return p
    for name in ("msedge", "microsoft-edge", "google-chrome",
                 "google-chrome-stable", "chrome", "chromium",
                 "chromium-browser", "brave-browser", "brave"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _render_pdf_with_browser(html: str) -> bytes | None:
    """Render HTML to a pixel-perfect PDF using the local browser in headless
    mode (same engine as the Print button). Returns None if unavailable.

    Recent Chrome versions write the PDF within seconds but the process can
    stay alive indefinitely (background updater services), so we poll for the
    output file instead of waiting for the browser to exit — then kill it.
    """
    browser = _find_browser()
    if not browser:
        return None
    tmp = tempfile.mkdtemp(prefix="jt-pdf-")
    src = os.path.join(tmp, "doc.html")
    pdf = os.path.join(tmp, "doc.pdf")
    profile = os.path.join(tmp, "prof")
    try:
        with open(src, "w", encoding="utf-8") as f:
            f.write(html)
        url = "file:///" + src.replace("\\", "/")
        args = [
            "--disable-gpu", "--no-sandbox", "--no-first-run",
            "--no-pdf-header-footer", f"--user-data-dir={profile}",
            f"--print-to-pdf={pdf}", url,
        ]
        for headless in ("--headless=new", "--headless"):
            try:
                proc = subprocess.Popen(
                    [browser, headless, *args],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                continue
            try:
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    if os.path.exists(pdf) and os.path.getsize(pdf) > 0:
                        # Wait until the file stops growing (fully flushed).
                        size = -1
                        while size != os.path.getsize(pdf):
                            size = os.path.getsize(pdf)
                            time.sleep(0.3)
                        with open(pdf, "rb") as f:
                            return f.read()
                    if proc.poll() is not None:  # browser exited without a PDF
                        break
                    time.sleep(0.3)
            finally:
                if proc.poll() is None:
                    proc.kill()
        return None
    except Exception:
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# Recognise common job sites (incl. Israeli boards) from a pasted URL.
_SOURCE_DOMAINS = [
    ("linkedin.com", "linkedin"),
    ("alljobs.co.il", "alljobs"),
    ("drushim.co.il", "drushim"),
    ("matrix.co.il", "matrix"),
    ("sqlink.com", "sqlink"),
    ("jobmaster.co.il", "jobmaster"),
    ("jobinfo.co.il", "jobinfo"),
    ("ethosia.co.il", "ethosia"),
    ("glassdoor.", "glassdoor"),
    ("indeed.", "indeed"),
    ("jooble.org", "jooble"),
    ("comeet.com", "comeet"),
    ("greenhouse.io", "greenhouse"),
    ("lever.co", "lever"),
    ("facebook.com", "facebook"),
    ("google.com", "google"),
]


def _source_from_url(url: str) -> str:
    """Best-effort job-source label inferred from a posting URL."""
    u = (url or "").lower()
    for needle, label in _SOURCE_DOMAINS:
        if needle in u:
            return label
    return ""


def md_to_html(text: str) -> str:
    """Tiny, safe markdown -> HTML (headings, bold, bullets, line breaks)."""
    if not text:
        return ""
    out_lines: list[str] = []
    in_list = False
    for raw in text.splitlines():
        line = html_lib.escape(raw.rstrip())
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        line = re.sub(r"`(.+?)`", r"<code>\1</code>", line)
        # Markdown links [text](http/https url) -> safe anchors.
        line = re.sub(
            r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
            r'<a href="\2" target="_blank" rel="noopener">\1</a>', line)
        if re.match(r"^\s*[-*•]\s+", line):
            if not in_list:
                out_lines.append("<ul>"); in_list = True
            out_lines.append("<li>" + re.sub(r"^\s*[-*•]\s+", "", line) + "</li>")
            continue
        if in_list:
            out_lines.append("</ul>"); in_list = False
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            lvl = len(m.group(1))
            out_lines.append(f"<h{lvl}>{m.group(2)}</h{lvl}>")
        elif line.strip():
            out_lines.append(f"<p>{line}</p>")
    if in_list:
        out_lines.append("</ul>")
    return "\n".join(out_lines)


# --------------------------------------------------------------------------- #
@bp.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        # getlist: multi-checkbox keys (SOURCES_DISABLED) post several values.
        updates = {k: ",".join(request.form.getlist(k)).strip()
                   for k in config.EDITABLE_KEYS}
        try:
            config.update_env_file(updates)
            flash("Settings saved and applied (no restart needed).", "ok")
        except Exception as exc:
            flash(f"Could not save settings: {exc}", "error")
        return redirect(url_for("main.settings"))
    return render_template(
        "settings.html",
        values=config.current_settings(),
        fields=config.EDITABLE_KEYS,
        sources=[s.name for s in get_sources()],
        ai_on=ai.is_configured(),
        gs_connected=gsheets.is_connected(),
        gmail_connected=gmail_alerts.is_connected(),
        gmail_rejections_connected=gmail_rejections.is_connected(),
        gs_secret_found=Path(str(config.GOOGLE_CLIENT_SECRET)).exists(),
        env_path=str(config.ENV_PATH),
        backup_dir=str(config.BACKUP_DIR),
        data_dir=str(config.PROFILE_DIR),
        jooble_usage=usage.jooble_usage(config.JOOBLE_API_KEY) if config.JOOBLE_API_KEY else None,
        sync_meta=syncstatus.status_summary(),
        gemini_models=ai.list_models(),
        openai_models=ai.OPENAI_MODELS,
        anthropic_models=ai.ANTHROPIC_MODELS,
        groq_models=ai.GROQ_MODELS,
        cursor_models=ai.CURSOR_MODELS,
        fallback_providers=[ai.provider_label(p) for p in ai.fallback_providers()],
    )


@bp.route("/profiles/switch", methods=["POST"])
def profile_switch():
    name = request.form.get("name", "")
    try:
        profiles_mod.switch_profile(name)
        flash(f"Switched to profile “{name}”.", "ok")
    except Exception as exc:
        flash(f"Could not switch profile: {exc}", "error")
    return redirect(request.referrer or url_for("main.dashboard"))


@bp.route("/profiles/create", methods=["POST"])
def profile_create():
    name = request.form.get("name", "")
    import_from = request.form.get("import_from", "").strip() or None
    try:
        profiles_mod.create_profile(name, import_from=import_from)
        profiles_mod.switch_profile(name.strip())
        note = f" (settings imported from “{import_from}”)" if import_from else ""
        flash(f"Profile “{name.strip()}” created and activated{note}.", "ok")
    except Exception as exc:
        flash(f"Could not create profile: {exc}", "error")
    return redirect(url_for("main.settings"))


@bp.route("/profiles/delete", methods=["POST"])
def profile_delete():
    name = request.form.get("name", "")
    try:
        profiles_mod.delete_profile(name)
        flash(f"Profile “{name}” deleted.", "ok")
    except Exception as exc:
        flash(f"Could not delete profile: {exc}", "error")
    return redirect(url_for("main.settings"))


@bp.route("/settings/backup", methods=["POST"])
def backup_now():
    dest = request.form.get("dest", "").strip() or None
    try:
        folder = backup.make_backup(dest)
        flash(f"Backup saved to {folder}", "ok")
    except Exception as exc:
        flash(f"Backup failed: {exc}", "error")
    return redirect(url_for("main.settings"))


@bp.route("/settings/backup/download")
def backup_download():
    import io
    data = backup.backup_zip_bytes()
    stamp = __import__("datetime").datetime.now().strftime("%Y%m%d-%H%M%S")
    return send_file(io.BytesIO(data), mimetype="application/zip",
                     as_attachment=True,
                     download_name=f"jobtracker-backup-{stamp}.zip")


@bp.route("/settings/git-backup", methods=["POST"])
def git_backup():
    try:
        note = gitbackup.push_to_github()
        flash(note, "ok")
    except Exception as exc:
        flash(f"GitHub backup failed: {exc}", "error")
    return redirect(url_for("main.settings"))


@bp.route("/settings/git-restore", methods=["POST"])
def git_restore():
    try:
        restored = gitbackup.restore_from_github()
        flash(f"Restored from GitHub: {', '.join(restored) or 'nothing found'}.", "ok")
    except Exception as exc:
        flash(f"GitHub restore failed: {exc}", "error")
    return redirect(url_for("main.settings"))


@bp.route("/settings/restore", methods=["POST"])
def restore():
    folder = request.form.get("folder", "").strip()
    if not folder:
        flash("Enter the path to a backup folder to restore from.", "error")
        return redirect(url_for("main.settings"))
    try:
        restored = backup.restore_from(folder)
        flash(f"Restored: {', '.join(restored) or 'nothing found'}.", "ok")
    except Exception as exc:
        flash(f"Restore failed: {exc}", "error")
    return redirect(url_for("main.settings"))


@bp.route("/settings/gsheet-connect", methods=["POST"])
def gsheet_connect():
    """One-time Google sign-in (opens a browser window on this machine)."""
    try:
        gsheets.connect()
        flash("Google account connected — you can sync now.", "ok")
    except Exception as exc:
        flash(f"Google connection failed: {exc}", "error")
    return redirect(url_for("main.settings"))


@bp.route("/settings/gsheet-sync", methods=["POST"])
def gsheet_sync():
    try:
        url = gsheets.sync()
        flash(f"Google Sheet updated: {url}", "ok")
    except Exception as exc:
        flash(f"Google Sheet sync failed: {exc}", "error")
    return redirect(url_for("main.settings"))


@bp.route("/settings/gsheet-disconnect", methods=["POST"])
def gsheet_disconnect():
    gsheets.disconnect()
    flash("Google account disconnected.", "ok")
    return redirect(url_for("main.settings"))


@bp.route("/settings/gmail-connect", methods=["POST"])
def gmail_connect():
    """One-time Gmail sign-in (use the job-alerts mailbox account)."""
    try:
        gmail_alerts.connect()
        flash("Gmail connected — open Job Alerts and fetch.", "ok")
    except Exception as exc:
        flash(f"Gmail connection failed: {exc}", "error")
    return redirect(url_for("main.settings"))


@bp.route("/settings/gmail-disconnect", methods=["POST"])
def gmail_disconnect():
    gmail_alerts.disconnect()
    flash("Gmail disconnected.", "ok")
    return redirect(url_for("main.settings"))


@bp.route("/settings/gmail-rejections-connect", methods=["POST"])
def gmail_rejections_connect():
    """One-time Gmail sign-in for the rejections mailbox."""
    try:
        gmail_rejections.connect()
        flash("Rejections Gmail connected — open Rejection inbox and fetch.", "ok")
    except Exception as exc:
        flash(f"Rejections Gmail connection failed: {exc}", "error")
    return redirect(url_for("main.settings"))


@bp.route("/settings/gmail-rejections-disconnect", methods=["POST"])
def gmail_rejections_disconnect():
    gmail_rejections.disconnect()
    flash("Rejections Gmail disconnected.", "ok")
    return redirect(url_for("main.settings"))


@bp.after_app_request
def _auto_gsheet_sync(response):
    """Keep the online sheet fresh: debounce a sync after data mutations."""
    if request.method == "POST" and request.path.startswith(
            ("/application", "/search/save")):
        gsheets.schedule_sync()
    return response


@bp.route("/settings/rebuild-profile", methods=["POST"])
def rebuild_profile():
    try:
        prof = resume_mod.build_profile()
        flash(f"Match profile rebuilt — {len(prof.get('skills', {}))} skills "
              f"detected from your resume.", "ok")
    except Exception as exc:
        flash(f"Could not build profile: {exc}", "error")
    return redirect(url_for("main.settings"))


@bp.route("/heartbeat", methods=["POST", "GET"])
def heartbeat():
    """Liveness ping from an open UI tab; resets the idle-shutdown timer."""
    from . import watchdog
    watchdog.ping()
    return ("", 204)


@bp.route("/quit", methods=["GET", "POST"])
def quit_app():
    """Confirm (GET) then stop the local server (POST)."""
    if request.method == "POST":
        import os
        import threading
        import time

        def _shutdown():
            time.sleep(0.6)
            os._exit(0)

        threading.Thread(target=_shutdown, daemon=True).start()
        return render_template("quit.html", stopped=True)
    return render_template("quit.html", stopped=False)


@bp.route("/help")
def help_page():
    import sys
    return render_template(
        "help.html",
        host=request.host,
        port=(request.host.split(":", 1) + ["5001"])[1] if ":" in request.host else "5001",
        python=sys.executable,
        ai_on=ai.is_configured(),
        sources=[s.name for s in get_sources()],
    )


@bp.route("/pitch", methods=["GET", "POST"])
def my_pitch():
    """View / edit / listen to the global about-me pitch (interview script)."""
    if request.method == "POST":
        pitch.save_base_pitch(request.form.get("text", ""))
        flash("Pitch saved.", "ok")
        return redirect(url_for("main.my_pitch"))
    return render_template("pitch.html", pitch_text=pitch.load_base_pitch(),
                           ai_on=ai.is_configured(), has_draft=pitch.has_draft())


@bp.route("/pitch/draft", methods=["POST"])
def my_pitch_draft():
    """Draft a fresh base pitch from the resume with Gemini (Hebrew)."""
    try:
        pitch.save_base_pitch(ai.pitch_from_resume(language="he"))
        flash("Drafted a new pitch from your resume.", "ok")
    except ai.AIError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.my_pitch"))


def _pitch_sections(text: str) -> tuple[str, list[dict]]:
    """Parse the free-text pitch into (title, sections) for the styled export.

    Heuristic: the first non-empty line is the document title; a short line
    with no sentence-ending punctuation starts a new section (card); numbered
    lines become list items; everything else is a spoken paragraph. Each
    section's body is a list of blocks: {"kind": "list", "items": [...]} or
    {"kind": "quote", "paras": [...]} — consecutive lines of the same type
    are grouped so the export reads like the styled reference document.
    """
    title = ""
    sections: list[dict] = []
    cur: dict | None = None

    def _new_section(heading: str = "", num: str = "") -> dict:
        s = {"heading": heading, "num": num, "blocks": []}
        sections.append(s)
        return s

    def _block(kind: str) -> dict:
        nonlocal cur
        cur = cur or _new_section()
        if not cur["blocks"] or cur["blocks"][-1]["kind"] != kind:
            cur["blocks"].append(
                {"kind": kind, "items": []} if kind == "list"
                else {"kind": kind, "paras": []})
        return cur["blocks"][-1]

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if not title:
            title = line
            continue
        m = re.match(r"^\d+[.)]\s+(.*)", line)
        if m:
            _block("list")["items"].append(m.group(1))
            continue
        m = re.match(r"^[-*•]\s+(.*)", line)
        if m:
            _block("list")["items"].append(m.group(1))
            continue
        looks_heading = (
            len(line) <= 60
            and line[-1] not in '.?!,;:"\'”'
            and not line.startswith(('"', '”', '„')))
        if looks_heading:
            # "1הפתיח…" / "2 העבודה…" -> numbered station heading (circle badge)
            m = re.match(r"^(\d+)\s*[.)]?\s*(\S.*)", line)
            if m and not m.group(2)[0].isdigit():
                cur = _new_section(m.group(2), num=m.group(1))
            else:
                cur = _new_section(line)
        else:
            _block("quote")["paras"].append(line)
    return title, sections


def _render_pitch_export() -> tuple[str, str]:
    """Render the standalone styled pitch document. Returns (html, title)."""
    text = pitch.load_base_pitch()
    title, sections = _pitch_sections(text)
    rtl = bool(re.search(r"[\u0590-\u05FF]", text))
    html = render_template(
        "pitch_export.html", title=title or "My Pitch", sections=sections,
        rtl=rtl, generated=datetime.now().strftime("%Y-%m-%d %H:%M"))
    return html, title


@bp.route("/pitch/export.html")
def pitch_export_html():
    """Download the pitch as a standalone, nicely styled HTML document."""
    html, _ = _render_pitch_export()
    resp = make_response(html)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.headers["Content-Disposition"] = "attachment; filename=my-pitch.html"
    return resp


@bp.route("/pitch/export.pdf")
def pitch_export_pdf():
    """Download the pitch as a PDF (rendered by the local browser engine)."""
    import io
    html, _ = _render_pitch_export()
    pdf = _render_pdf_with_browser(html)
    if not pdf:
        flash("No local Chrome/Edge found for PDF rendering — export the HTML "
              "instead and print it to PDF from your browser (Cmd/Ctrl+P).",
              "error")
        return redirect(url_for("main.my_pitch"))
    return send_file(io.BytesIO(pdf), mimetype="application/pdf",
                     as_attachment=True, download_name="my-pitch.pdf")


@bp.route("/pitch/revise", methods=["POST"])
def pitch_revise():
    """AI-rewrite the base pitch per a free-text instruction -> pending draft."""
    instruction = request.form.get("instruction", "").strip()
    try:
        draft = ai.revise_pitch(base_pitch=pitch.load_base_pitch(),
                                instruction=instruction)
        pitch.save_draft(draft)
        return redirect(url_for("main.pitch_compare"))
    except ai.AIError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.my_pitch"))


def _diff_sides(old: str, new: str) -> tuple[str, str]:
    """Word-level diff -> two HTML strings: old with <del>, new with <ins>."""
    import difflib

    def tokens(s: str) -> list[str]:
        return re.findall(r"\S+|\s+", s or "")

    a, b = tokens(old), tokens(new)
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    left: list[str] = []
    right: list[str] = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        seg_a = html_lib.escape("".join(a[i1:i2]))
        seg_b = html_lib.escape("".join(b[j1:j2]))
        if op == "equal":
            left.append(seg_a)
            right.append(seg_b)
        else:
            if seg_a.strip():
                left.append(f"<del>{seg_a}</del>")
            else:
                left.append(seg_a)
            if seg_b.strip():
                right.append(f"<ins>{seg_b}</ins>")
            else:
                right.append(seg_b)
    return "".join(left), "".join(right)


@bp.route("/pitch/compare")
def pitch_compare():
    """Side-by-side current pitch vs pending AI revision, differences marked."""
    draft = pitch.load_draft()
    if not draft.strip():
        flash("No pending AI revision to compare — generate one first.", "error")
        return redirect(url_for("main.my_pitch"))
    current = pitch.load_base_pitch()
    old_html, new_html = _diff_sides(current, draft)
    return render_template("pitch_compare.html",
                           old_html=old_html, new_html=new_html)


@bp.route("/pitch/draft/apply", methods=["POST"])
def pitch_draft_apply():
    """Accept the pending AI revision: it becomes the base pitch."""
    draft = pitch.load_draft()
    if draft.strip():
        pitch.save_base_pitch(draft)
        pitch.clear_draft()
        flash("AI revision applied — it is now your pitch.", "ok")
    else:
        flash("No pending AI revision to apply.", "error")
    return redirect(url_for("main.my_pitch"))


@bp.route("/pitch/draft/discard", methods=["POST"])
def pitch_draft_discard():
    """Throw away the pending AI revision; the original pitch stays."""
    pitch.clear_draft()
    flash("AI revision discarded — your pitch is unchanged.", "ok")
    return redirect(url_for("main.my_pitch"))


@bp.route("/")
def dashboard():
    ghosted = tracker.auto_ghost_stale()
    if ghosted:
        names = ", ".join(f"{g['company']} — {g['title']}" for g in ghosted[:5])
        flash(f"{len(ghosted)} application(s) with no response for 30+ days "
              f"were marked ghosted: {names}.", "ok")
    funnel = analytics.funnel()
    totals = analytics.totals()
    recent = tracker.list_applications()[:10]
    return render_template(
        "dashboard.html", funnel=funnel, totals=totals, recent=recent,
        rej_stage=analytics.rejection_by_stage(),
        rej_reason=analytics.rejection_by_reason(),
        sources=analytics.source_stats(),
        insight=analytics.match_score_insight(),
        reminders=analytics.saved_reminders(),
        digest=analytics.action_digest(),
        ai_on=ai.is_configured(),
    )


# --------------------------------------------------------------------------- #
# Rejection insights: AI post-mortem per rejection + overall pattern analysis.
# Per-rejection verdicts are cached in the DB and the overall analysis in a
# profile-level JSON file, so AI runs only for NEW rejections.
# --------------------------------------------------------------------------- #
def _rejection_insights_path() -> Path:
    return Path(config.PROFILE_DIR) / "rejection_insights.json"


def _load_rejection_insights() -> dict | None:
    try:
        data = json.loads(_rejection_insights_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _fit_summary_for_prompt(r) -> str:
    """Compact text digest of the stored fit analysis for the AI prompt."""
    try:
        a = json.loads(r["ai_analysis_json"]) if r["ai_analysis_json"] else None
    except (TypeError, ValueError):
        a = None
    if not isinstance(a, dict):
        return ""
    parts = [f"Fit score: {a.get('fit_score', '?')}/100",
             f"Verdict: {a.get('verdict', '')}"]
    gaps = [f"- {q.get('requirement', '')} (evidence: {q.get('evidence', '')})"
            for q in a.get("requirements") or []
            if isinstance(q, dict) and q.get("match") == "gap"]
    if gaps:
        parts.append("Requirements flagged as GAPS:\n" + "\n".join(gaps))
    return "\n".join(parts)


def _rejection_verdicts_digest(rows, analyses) -> str:
    """Compact JSON of all cached per-rejection verdicts for the overall prompt."""
    items = []
    for r in rows:
        a = analyses.get(r["id"])
        if not a:
            continue
        items.append({
            "company": r["company"], "title": r["title"],
            "stage": r["rejection_stage"], "reason": r["rejection_reason"],
            "ai_fit_score": r["ai_fit_score"],
            "cause": a.get("cause"), "confidence": a.get("confidence"),
            "avoidable": a.get("avoidable"),
            "explanation": a.get("explanation"),
            "missed_requirements": [m.get("en") for m in
                                    a.get("missed_requirements") or []],
            "improvement": a.get("improvement"),
        })
    return json.dumps(items, ensure_ascii=False, indent=1)


@bp.route("/rejections")
def rejections():
    rows = tracker.list_applications(status="rejected")
    analyses = {r["id"]: tracker.get_rejection_analysis(r["id"]) for r in rows}
    return render_template(
        "rejections.html", rows=rows, analyses=analyses,
        overall=_load_rejection_insights(),
        baseline=analytics.rejection_baseline(),
        pending=sum(1 for r in rows if not analyses[r["id"]]),
        ai_on=ai.is_configured(),
    )


@bp.route("/rejections/analyze", methods=["POST"])
def rejections_analyze():
    """Analyze rejections. Cached: only NEW (unanalyzed) rejections cost AI
    calls; force=1 re-runs everything from scratch."""
    force = request.form.get("force") == "1"
    rows = tracker.list_applications(status="rejected")
    if not rows:
        flash("No rejected applications to analyze.", "error")
        return redirect(url_for("main.rejections"))
    try:
        resume_txt = ai.resume_text()
    except Exception:
        resume_txt = ""
    done, errors = 0, []
    for r in rows:
        if not force and tracker.get_rejection_analysis(r["id"]):
            continue
        try:
            data = ai.analyze_rejection(
                title=r["title"], company=r["company"],
                description=r["description"] or "",
                stage=r["rejection_stage"] or "",
                reason=r["rejection_reason"] or "",
                note=r["rejection_note"] or "",
                fit_summary=_fit_summary_for_prompt(r),
                resume=resume_txt or None,
            )
            tracker.set_rejection_analysis(r["id"], data)
            done += 1
        except ai.AIError as exc:
            errors.append(f"{r['company']}: {exc}")

    # Overall pattern analysis — regenerated from the cached verdicts whenever
    # anything changed (or it doesn't exist yet). One AI call.
    analyses = {r["id"]: tracker.get_rejection_analysis(r["id"]) for r in rows}
    overall_err = None
    if any(analyses.values()) and (done or force or not _load_rejection_insights()):
        try:
            overall = ai.analyze_rejections_overall(
                verdicts=_rejection_verdicts_digest(rows, analyses),
                baseline=json.dumps(analytics.rejection_baseline(),
                                    ensure_ascii=False, indent=1))
            overall["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            overall["rejections_analyzed"] = sum(1 for v in analyses.values() if v)
            _rejection_insights_path().write_text(
                json.dumps(overall, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except ai.AIError as exc:
            overall_err = str(exc)

    if done:
        flash(f"Analyzed {done} rejection(s) — cached; future runs only "
              "process new rejections.", "ok")
    elif not errors:
        flash("All rejections were already analyzed (cached).", "ok")
    for e in errors[:3]:
        flash(f"Analysis failed for {e}", "error")
    if overall_err:
        flash(f"Overall analysis failed: {overall_err}", "error")
    return redirect(url_for("main.rejections"))


@bp.route("/rejections/<int:app_id>/analyze", methods=["POST"])
def rejection_reanalyze(app_id: int):
    """Re-run the post-mortem for one rejection (e.g. after editing the note)."""
    r = tracker.get_application(app_id)
    if not r:
        abort(404)
    try:
        data = ai.analyze_rejection(
            title=r["title"], company=r["company"],
            description=r["description"] or "",
            stage=r["rejection_stage"] or "", reason=r["rejection_reason"] or "",
            note=r["rejection_note"] or "",
            fit_summary=_fit_summary_for_prompt(r),
        )
        tracker.set_rejection_analysis(app_id, data)
        flash(f"Rejection analysis updated for {r['company']}.", "ok")
    except ai.AIError as exc:
        flash(f"Analysis failed: {exc}", "error")
    return redirect(url_for("main.rejections"))


@bp.route("/rejections/export")
def rejections_export():
    """Standalone HTML dashboard of the rejection insights — email-shareable."""
    rows = tracker.list_applications(status="rejected")
    analyses = {r["id"]: tracker.get_rejection_analysis(r["id"]) for r in rows}
    html = render_template(
        "rejections_export.html", rows=rows, analyses=analyses,
        overall=_load_rejection_insights(),
        baseline=analytics.rejection_baseline(),
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    resp = make_response(html)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.headers["Content-Disposition"] = (
        f"attachment; filename=rejection-insights-"
        f"{datetime.now().strftime('%Y%m%d')}.html")
    return resp


@bp.route("/applications")
def applications():
    status = request.args.get("status") or None
    # Default order = row number descending (newest application on top),
    # matching the '#' column; click a column header to re-sort client-side.
    rows = tracker.list_applications(status=status, order_by="id DESC")
    # Gap-free display numbers in creation order (oldest = 1), stable across
    # deletes, filters and sorting — computed over ALL applications so a job
    # keeps the same number on filtered views too.
    all_ids = sorted(r["id"] for r in tracker.list_applications())
    seq = {app_id: n for n, app_id in enumerate(all_ids, start=1)}
    return render_template("applications.html", rows=rows, statuses=STATUSES,
                           active=status, seq=seq)


@bp.route("/alerts")
def alerts():
    """Job postings collected from Gmail alert emails, vs. what you applied to."""
    show_all = request.args.get("all") == "1"
    show_ignored = request.args.get("ignored") == "1"
    # Default = action queue (needs a decision). ?view=all shows every active row.
    show_queue = (not show_all and not show_ignored
                  and request.args.get("view", "queue") != "all")
    connected = gmail_alerts.is_connected()
    if connected:
        try:  # keep 'applied' badges fresh (cheap: local fuzzy matching only)
            gmail_alerts.refresh_matches()
        except Exception:
            pass
    rows = gmail_alerts.list_alerts(include_dismissed=show_all,
                                    ignored=show_ignored, queue=show_queue)
    apps = tracker.list_applications()
    app_names = {r["id"]: f"{r['company']} — {r['title']}" for r in apps}
    app_dates = {r["id"]: (r["date_applied"] or "")[:10] for r in apps}
    app_status = {r["id"]: r["status"] for r in apps}
    matched_ids = [r["matched_app_id"] for r in rows if r["matched_app_id"]]
    app_paths = tracker.status_paths_for_apps(matched_ids)
    return render_template(
        "alerts.html", rows=rows, show_all=show_all, show_ignored=show_ignored,
        show_queue=show_queue, connected=connected, app_names=app_names,
        app_dates=app_dates, app_status=app_status, app_paths=app_paths,
        label=config.GMAIL_LABEL,
        queue_count=gmail_alerts.action_queue_count(),
        pending=sum(1 for r in rows
                    if not r["dismissed"] and not r["matched_app_id"]
                    and not r["ignored"]))


@bp.route("/alerts/status")
def alerts_status():
    """Polled by every page: lets the UI pop a toast when new alerts arrive."""
    try:
        return {"max_id": gmail_alerts.max_alert_id(),
                "new_count": gmail_alerts.new_alert_count()}
    except Exception:
        return {"max_id": 0, "new_count": 0}


@bp.route("/alerts/fetch", methods=["POST"])
def alerts_fetch():
    try:
        res = gmail_alerts.fetch_alerts()
        if res["emails"]:
            flash(f"Checked {res['emails']} new email(s) — "
                  f"{res['jobs']} new job(s) found.", "ok")
        else:
            flash("No new alert emails since the last fetch.", "ok")
    except Exception as exc:
        flash(f"Could not fetch alerts: {exc}", "error")
    return redirect(url_for("main.alerts"))


@bp.route("/alerts/seen", methods=["POST"])
def alerts_seen():
    """Reset the Alerts nav badge without dismissing anything."""
    gmail_alerts.mark_all_seen()
    flash("Alerts counter reset — the badge now counts only new alerts.", "ok")
    return redirect(url_for("main.alerts", **request.args))


@bp.route("/alerts/<int:alert_id>/read", methods=["POST"])
def alert_read(alert_id: int):
    """Mark one alert as read (mailbox-style)."""
    gmail_alerts.set_seen(alert_id, True)
    if request.headers.get("X-Requested-With") == "fetch":
        return {"ok": True, "id": alert_id}
    return redirect(url_for("main.alerts", **request.args))


@bp.route("/alerts/<int:alert_id>/comment", methods=["POST"])
def alert_comment(alert_id: int):
    """Save a note on an alert — survives when the same job appears again."""
    gmail_alerts.set_comment(alert_id, request.form.get("comment", ""))
    flash("Comment saved.", "ok")
    return redirect(url_for("main.alerts", **request.args))


def _fetch_job_page_text(url: str) -> str:
    """Best-effort job description from a posting URL (for one-click capture)."""
    if not url:
        return ""
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError:
        return ""
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0.0.0 Safari/537.36"),
        "Accept-Language": "en-US,en;q=0.9,he;q=0.8",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
    except Exception:
        return ""
    if resp.status_code >= 400:
        return ""
    html = resp.text[:400_000]
    # Prefer JobPosting JSON-LD description when present.
    for m in re.finditer(
            r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>",
            html, re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        for d in (data if isinstance(data, list) else [data]):
            if isinstance(d, dict) and d.get("@type") == "JobPosting":
                desc = d.get("description") or ""
                if isinstance(desc, str) and len(desc.strip()) >= 80:
                    # description may be HTML
                    return _html_visible_text(desc) if "<" in desc else desc.strip()
    text = _html_visible_text(html)
    # Drop obvious chrome noise
    if len(text) < 80:
        return ""
    return text[:20000]


@bp.route("/alerts/<int:alert_id>/capture", methods=["POST"])
def alert_capture(alert_id: int):
    """One-click capture: fetch the job page, save as an application, link alert.

    If the page can't be fetched (common for LinkedIn), fall back to the Paste
    form prefilled with the alert's fields.
    """
    row = gmail_alerts.get_alert(alert_id)
    if not row:
        flash("Alert not found.", "error")
        return redirect(url_for("main.alerts"))

    url = (row["url"] or "").strip()
    title = (row["title"] or "").strip()
    company = (row["company"] or "").strip()
    location = (row["location"] or "").strip()

    if row["matched_app_id"]:
        flash("This alert is already linked to an application.", "ok")
        return redirect(url_for("main.detail", app_id=row["matched_app_id"]))

    text = _fetch_job_page_text(url)
    if not text:
        gmail_alerts.set_seen(alert_id, True)
        flash("Couldn't fetch the job page automatically — paste the description "
              "below (LinkedIn often blocks direct fetch).", "error")
        return redirect(url_for(
            "main.paste_job", url=url, title=title, company=company,
            location=location))

    # Enrich blank fields with AI when available.
    if ai.is_configured() and not (title and company):
        try:
            parsed = ai.parse_job(text)
            title = title or parsed.get("title", "")
            company = company or parsed.get("company", "")
            location = location or parsed.get("location", "")
        except ai.AIError as exc:
            flash(f"AI extraction skipped: {exc}", "error")

    if not title:
        title = text.splitlines()[0][:120] if text else "(untitled)"
    if not company:
        company = "(unknown)"

    # If a duplicate already exists, just link the alert to it.
    if company != "(unknown)":
        dups = tracker.find_duplicates(title, company)
        if dups:
            app_id = int(dups[0]["id"])
            gmail_alerts.link_application(alert_id, app_id)
            flash(f"Linked alert to existing application #{app_id}.", "ok")
            return redirect(url_for("main.detail", app_id=app_id))

    source = _source_from_url(url) or "linkedin-alert"
    score = score_job(title, text).score
    app_id = tracker.add_application(
        company=company, title=title, location=location, url=url,
        description=text, source=source, status="saved", match_score=score,
    )
    gmail_alerts.link_application(alert_id, app_id)
    flash(f"Captured alert as application #{app_id} — review and apply when ready.",
          "ok")
    return redirect(url_for("main.detail", app_id=app_id))


@bp.route("/alerts/<int:alert_id>/open")
def alert_open(alert_id: int):
    """Open the job URL and mark the alert as read (like opening a mail)."""
    gmail_alerts.set_seen(alert_id, True)
    url = gmail_alerts.alert_url(alert_id)
    if url:
        return redirect(url)
    flash("Alert has no job URL.", "error")
    return redirect(url_for("main.alerts", **request.args))


@bp.route("/alerts/bulk", methods=["POST"])
def alerts_bulk():
    """Apply the same action to several checked alerts."""
    action = (request.form.get("action") or "").strip()
    raw_ids = request.form.getlist("alert_ids")
    ids = []
    for v in raw_ids:
        try:
            ids.append(int(v))
        except (TypeError, ValueError):
            pass
    if not ids:
        flash("Select at least one alert.", "error")
        return redirect(url_for("main.alerts", **request.args))
    if action == "read":
        n = gmail_alerts.set_seen_many(ids, True)
        flash(f"Marked {n} alert(s) as read.", "ok")
    elif action == "dismiss":
        n = gmail_alerts.set_dismissed_many(ids, True)
        flash(f"Dismissed {n} alert(s).", "ok")
    elif action == "ignore":
        n = gmail_alerts.set_ignored_many(ids, True)
        flash(f"Ignored {n} alert(s) — they won't notify again.", "ok")
    elif action == "restore":
        n = gmail_alerts.set_dismissed_many(ids, False)
        flash(f"Restored {n} alert(s).", "ok")
    elif action == "unignore":
        n = gmail_alerts.set_ignored_many(ids, False)
        flash(f"Removed {n} alert(s) from the ignore list.", "ok")
    else:
        flash("Unknown bulk action.", "error")
    return redirect(url_for("main.alerts", **request.args))


@bp.route("/alerts/<int:alert_id>/dismiss", methods=["POST"])
def alert_dismiss(alert_id: int):
    gmail_alerts.set_dismissed(alert_id, True)
    return redirect(url_for("main.alerts", **request.args))


@bp.route("/alerts/<int:alert_id>/restore", methods=["POST"])
def alert_restore(alert_id: int):
    gmail_alerts.set_dismissed(alert_id, False)
    return redirect(url_for("main.alerts", all=1))


@bp.route("/alerts/<int:alert_id>/ignore", methods=["POST"])
def alert_ignore(alert_id: int):
    gmail_alerts.set_ignored(alert_id, True)
    flash("Added to the ignore list — this job won't notify you again.", "ok")
    return redirect(url_for("main.alerts", **request.args))


@bp.route("/alerts/<int:alert_id>/unignore", methods=["POST"])
def alert_unignore(alert_id: int):
    gmail_alerts.set_ignored(alert_id, False)
    return redirect(url_for("main.alerts", ignored=1))


@bp.route("/rejection-inbox")
def rejection_inbox():
    """Rejection emails from Gmail, matched to applications — confirm to log."""
    show_all = request.args.get("all") == "1"
    connected = gmail_rejections.is_connected()
    if connected:
        try:
            gmail_rejections.refresh_matches()
        except Exception:
            pass
    rows = gmail_rejections.list_inbox(include_dismissed=show_all)
    apps = tracker.list_applications()
    app_names = {r["id"]: f"#{r['id']} {r['company']} — {r['title']}" for r in apps}
    picker_by_row: dict[int, list] = {}
    picker_filtered: dict[int, bool] = {}
    for r in rows:
        choices, filtered = gmail_rejections.list_applications_for_picker(
            company=r["company"] or "",
            title=r["title"] or "",
            matched_app_id=r["matched_app_id"],
        )
        picker_by_row[r["id"]] = choices
        picker_filtered[r["id"]] = filtered
    return render_template(
        "rejection_inbox.html", rows=rows, show_all=show_all,
        connected=connected, app_names=app_names, picker_by_row=picker_by_row,
        picker_filtered=picker_filtered,
        label=config.GMAIL_REJECTION_LABEL,
        stages=REJECTION_STAGES, reasons=COMMON_REJECTION_REASONS,
        pending=sum(1 for r in rows if r["status"] == "pending"
                    and r["matched_app_id"]))


@bp.route("/rejection-inbox/status")
def rejection_inbox_status():
    try:
        return {"max_id": gmail_rejections.max_inbox_id(),
                "new_count": gmail_rejections.pending_count()}
    except Exception:
        return {"max_id": 0, "new_count": 0}


@bp.route("/rejection-inbox/fetch", methods=["POST"])
def rejection_inbox_fetch():
    try:
        res = gmail_rejections.fetch_rejections()
        msg = ""
        if res.get("used_fallback"):
            msg = (" (label not found — scanned inbox with a built-in rejection "
                   "query; create the Gmail filter below for cleaner results)")
        if res["emails"]:
            flash(f"Checked {res['emails']} new email(s) — "
                  f"{res['rejections']} rejection(s) found.{msg}", "ok")
        else:
            flash("No new rejection emails since the last fetch.", "ok")
    except Exception as exc:
        flash(f"Could not fetch rejections: {exc}", "error")
    return redirect(url_for("main.rejection_inbox"))


@bp.route("/rejection-inbox/seen", methods=["POST"])
def rejection_inbox_seen():
    gmail_rejections.mark_all_seen()
    flash("Rejection inbox badge reset.", "ok")
    return redirect(url_for("main.rejection_inbox", **request.args))


@bp.route("/rejection-inbox/<int:row_id>/dismiss", methods=["POST"])
def rejection_inbox_dismiss(row_id: int):
    gmail_rejections.set_dismissed(row_id)
    return redirect(url_for("main.rejection_inbox", **request.args))


@bp.route("/rejection-inbox/<int:row_id>/match", methods=["POST"])
def rejection_inbox_match(row_id: int):
    app_id = request.form.get("app_id", "").strip()
    gmail_rejections.set_match(row_id, int(app_id) if app_id else None)
    return redirect(url_for("main.rejection_inbox", **request.args))


def _analyze_rejection_app(app_id: int) -> bool:
    """Run + cache per-rejection AI analysis. Returns True on success."""
    r = tracker.get_application(app_id)
    if not r:
        return False
    data = ai.analyze_rejection(
        title=r["title"], company=r["company"],
        description=r["description"] or "",
        stage=r["rejection_stage"] or "", reason=r["rejection_reason"] or "",
        note=r["rejection_note"] or "",
        fit_summary=_fit_summary_for_prompt(r),
    )
    tracker.set_rejection_analysis(app_id, data)
    return True


def _refresh_rejection_overall() -> None:
    """Best-effort refresh of the overall rejection-insights cache."""
    rows = tracker.list_applications(status="rejected")
    analyses = {r["id"]: tracker.get_rejection_analysis(r["id"]) for r in rows}
    if not any(analyses.values()):
        return
    overall = ai.analyze_rejections_overall(
        verdicts=_rejection_verdicts_digest(rows, analyses),
        baseline=json.dumps(analytics.rejection_baseline(),
                            ensure_ascii=False, indent=1))
    overall["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    overall["rejections_analyzed"] = sum(1 for v in analyses.values() if v)
    _rejection_insights_path().write_text(
        json.dumps(overall, ensure_ascii=False, indent=2), encoding="utf-8")


@bp.route("/rejection-inbox/<int:row_id>/confirm", methods=["POST"])
def rejection_inbox_confirm(row_id: int):
    app_id = int(request.form.get("app_id", "0") or 0)
    stage = request.form.get("stage", "cv_screen")
    reason = request.form.get("reason", "no_feedback")
    note = request.form.get("note", "").strip()
    if not app_id:
        flash("Pick an application to mark as rejected.", "error")
        return redirect(url_for("main.rejection_inbox"))
    if not gmail_rejections.confirm(row_id, app_id=app_id, stage=stage,
                                    reason=reason, note=note):
        flash("Could not update that application.", "error")
        return redirect(url_for("main.rejection_inbox", **request.args))

    flash("Application marked rejected.", "ok")
    # Close the loop: analyze this rejection and open the insights dashboard.
    if ai.is_configured():
        try:
            _analyze_rejection_app(app_id)
            flash("Rejection analysis ready — see insights below.", "ok")
            try:
                _refresh_rejection_overall()
            except ai.AIError as exc:
                flash(f"Overall patterns not refreshed: {exc}", "error")
            return redirect(url_for("main.rejections"))
        except ai.AIError as exc:
            flash(f"Rejection saved, but analysis failed: {exc}", "error")
    return redirect(url_for("main.rejection_inbox", **request.args))


@bp.route("/application/<int:app_id>")
def detail(app_id: int):
    r = tracker.get_application(app_id)
    if not r:
        abort(404)
    analysis = tracker.get_ai_analysis(app_id)
    mock = None
    if r["mock_interview"]:
        try:
            mock = json.loads(r["mock_interview"])
        except (TypeError, ValueError):
            mock = None
    resume_lib = resumes.ensure_defaults()
    sent_resume = resumes.for_application(app_id)
    resume_history = resumes.history_for(app_id)
    return render_template(
        "detail.html", app=r, history=tracker.get_history(app_id),
        analysis=analysis, mock=mock, statuses=STATUSES, stages=REJECTION_STAGES,
        reasons=COMMON_REJECTION_REASONS, ai_on=ai.is_configured(),
        has_tailored=_tailored_path(app_id).exists(),
        has_resume_draft=_tailored_draft_path(app_id).exists(),
        base_pitch=pitch.load_base_pitch(),
        salary=tracker.get_salary_research(app_id),
        company_brief=tracker.get_company_brief(app_id),
        ats=tracker.get_ats_check(app_id),
        resume_lib=resume_lib, sent_resume=sent_resume,
        resume_history=resume_history,
    )


@bp.route("/application/<int:app_id>/resume", methods=["POST"])
def set_sent_resume(app_id: int):
    """Record which resume was sent for this application (library + optional upload)."""
    if not tracker.get_application(app_id):
        abort(404)
    f = request.form
    rid = None
    try:
        rid = resumes.resolve_selection(
            resume_id=f.get("resume_id"),
            upload=request.files.get("resume_file"),
            upload_label=(f.get("resume_label") or "").strip(),
            path_text=(f.get("resume_path") or "").strip(),
        )
        resumes.attach_to_application(app_id, rid)
    except (ValueError, FileNotFoundError, OSError) as exc:
        flash(f"Could not save resume: {exc}", "error")
        return redirect(url_for("main.detail", app_id=app_id))
    if rid:
        row = resumes.get(rid)
        flash(f"Resume recorded: {row['label']}.", "ok")
    else:
        flash("Cleared sent-resume link.", "ok")
    return redirect(url_for("main.detail", app_id=app_id))


@bp.route("/application/add", methods=["POST"])
def add():
    f = request.form
    if not f.get("company") or not f.get("title"):
        flash("Company and title are required.", "error")
        return redirect(request.referrer or url_for("main.applications"))
    score = score_job(f.get("title", ""), f.get("description", "")).score
    app_id = tracker.add_application(
        company=f["company"], title=f["title"], location=f.get("location", ""),
        url=f.get("url", ""), description=f.get("description", ""),
        salary=f.get("salary", ""), source=f.get("source", "manual"),
        status=f.get("status", "saved"), contact=f.get("contact", ""),
        notes=f.get("notes", ""), match_score=score,
    )
    flash(f"Added application #{app_id}.", "ok")
    return redirect(url_for("main.detail", app_id=app_id))


@bp.route("/application/paste", methods=["GET", "POST"])
def paste_job():
    """Capture a job by pasting its text + the URL it came from.

    Title/company/location/salary are auto-extracted with AI when left blank
    (and the AI key is configured); the full pasted text becomes the
    description used for match scoring and fit analysis.
    """
    rd = _readiness()
    resume_lib = resumes.ensure_defaults()
    if request.method == "GET":
        # Query params (from a Job Alerts "Capture" link) prefill the form.
        prefill = {k: request.args.get(k, "")
                   for k in ("url", "title", "company", "location", "salary")}
        return render_template("paste.html", statuses=STATUSES,
                               ai_on=ai.is_configured(), ready=rd,
                               form=prefill, duplicates=None,
                               resume_lib=resume_lib)

    if not rd["ready"]:
        for msg in rd["issues"]:
            flash(msg, "error")
        return redirect(url_for("main.paste_job"))

    f = request.form
    text = (f.get("description") or "").strip()
    url = (f.get("url") or "").strip()
    if not text:
        flash("Paste the job text first.", "error")
        return redirect(url_for("main.paste_job"))

    title = (f.get("title") or "").strip()
    company = (f.get("company") or "").strip()
    location = (f.get("location") or "").strip()
    salary = (f.get("salary") or "").strip()

    # Auto-extract any blank fields with AI when available.
    if ai.is_configured() and not (title and company):
        try:
            parsed = ai.parse_job(text)
            title = title or parsed.get("title", "")
            company = company or parsed.get("company", "")
            location = location or parsed.get("location", "")
            salary = salary or parsed.get("salary", "")
        except ai.AIError as exc:
            flash(f"AI extraction skipped: {exc}", "error")

    # Fallbacks so we always have something usable.
    if not title:
        title = text.splitlines()[0][:120] if text else "(untitled)"
    if not company:
        company = "(unknown)"

    def _form_ctx(**extra):
        rid = extra.pop("resume_id", f.get("resume_id") or "")
        if rid is None:
            rid = ""
        return {
            "url": url, "description": text, "title": title,
            "company": company, "location": location, "salary": salary,
            "status": f.get("status", "saved"),
            "source": (f.get("source") or "").strip(),
            "autogen": bool(f.get("autogen")),
            "starred": bool(f.get("starred")),
            "resume_id": rid,
            "resume_label": (f.get("resume_label") or "").strip(),
            "resume_path": (f.get("resume_path") or "").strip(),
            **extra,
        }

    def _enrich_dups(rows):
        """Attach previous-resume labels so the warning can show them."""
        out = []
        for d in rows:
            item = dict(d)
            rid = d["resume_id"] if "resume_id" in d.keys() else None
            item["resume_label"] = ""
            item["resume_hash"] = ""
            if rid:
                rr = resumes.get(int(rid))
                if rr:
                    item["resume_label"] = rr["label"] or ""
                    item["resume_hash"] = (rr["content_hash"] or "")[:8]
                elif d["resume_version"]:
                    item["resume_label"] = d["resume_version"]
            elif d["resume_version"]:
                item["resume_label"] = d["resume_version"]
            out.append(item)
        return out

    # Resolve which resume was sent (optional). Same file content → same library row.
    resume_id = None
    try:
        resume_id = resumes.resolve_selection(
            resume_id=f.get("resume_id"),
            upload=request.files.get("resume_file"),
            upload_label=(f.get("resume_label") or "").strip(),
            path_text=(f.get("resume_path") or "").strip(),
        )
    except (ValueError, FileNotFoundError, OSError) as exc:
        flash(f"Resume not saved: {exc}", "error")
        return render_template(
            "paste.html", statuses=STATUSES, ai_on=ai.is_configured(),
            ready=rd, duplicates=None, form=_form_ctx(),
            resume_lib=resumes.list_resumes(),
        )

    # Mark an existing duplicate as reapplied (same row; archive previous CV).
    reapply_raw = (f.get("reapply_app_id") or "").strip()
    if reapply_raw:
        try:
            reapply_id = int(reapply_raw)
        except ValueError:
            flash("Invalid reapply target.", "error")
            return redirect(url_for("main.paste_job"))
        existing = tracker.get_application(reapply_id)
        if not existing:
            flash(f"Application #{reapply_id} not found.", "error")
            return redirect(url_for("main.paste_job"))
        ok = tracker.mark_reapplied(
            reapply_id,
            resume_id=resume_id,
            note="reapplied from Paste a Job",
            description=text,
            url=url,
        )
        if not ok:
            flash("Could not mark as reapplied.", "error")
            return redirect(url_for("main.detail", app_id=reapply_id))
        if f.get("starred"):
            tracker.set_star(reapply_id, True)
        flash(
            f"Marked #{reapply_id} as reapplied.",
            "ok",
        )
        if resume_id:
            rr = resumes.get(resume_id)
            if rr:
                flash(f"Linked resume: {rr['label']}.", "ok")
        app_id = reapply_id
        # Fall through to optional autogen, then applications list.
        if ai.is_configured() and f.get("autogen"):
            r = tracker.get_application(app_id)
            plan = [("company", "en"), ("analyze", "en"), ("resume", "en"),
                    ("salary", "en"), ("note", "en"), ("cover", "en"),
                    ("pitch", "he")]
            done: list[str] = []
            failed: list[str] = []
            for idx, (key, lang) in enumerate(plan):
                if idx:
                    time.sleep(_AUTOGEN_GAP_S)
                try:
                    _generate_one(app_id, key, r, language=lang)
                    done.append(_BATCH_ITEMS[key])
                except Exception as exc:
                    failed.append(f"{_BATCH_ITEMS[key]} ({exc})")
            if done:
                flash("Generated: " + ", ".join(done) + ".", "ok")
            if failed:
                flash("Failed: " + "; ".join(failed) + ".", "error")
        return redirect(url_for("main.applications"))

    # Warn if an application with the same title + company already exists, but
    # let the user proceed (re-applying, or a genuinely different posting). The
    # "Proceed anyway" submit resends with confirm_duplicate=1 to skip this.
    if not f.get("confirm_duplicate") and company != "(unknown)":
        duplicates = tracker.find_duplicates(title, company)
        if duplicates:
            return render_template(
                "paste.html", statuses=STATUSES, ai_on=ai.is_configured(),
                ready=rd, duplicates=_enrich_dups(duplicates),
                form=_form_ctx(resume_id=resume_id or ""),
                resume_lib=resumes.list_resumes(),
            )

    # Prefer a source auto-detected from the URL (e.g. linkedin, alljobs,
    # drushim) over the generic default.
    source = (f.get("source") or "").strip()
    if source in ("", "paste"):
        source = _source_from_url(url) or "paste"

    score = score_job(title, text).score
    app_id = tracker.add_application(
        company=company, title=title, location=location, url=url,
        description=text, salary=salary, source=source,
        status=f.get("status", "saved"), match_score=score,
        resume_id=resume_id,
    )
    if f.get("starred"):
        tracker.set_star(app_id, True)
    flash(f"Captured job as #{app_id}.", "ok")
    if resume_id:
        row = resumes.get(resume_id)
        if row:
            flash(f"Linked resume: {row['label']}.", "ok")

    # Auto-run the most useful AI artefacts right after capture (opt-out via the
    # checkbox). Company research and fit analysis are bilingual.
    if ai.is_configured() and f.get("autogen"):
        r = tracker.get_application(app_id)
        # (item-key, language) — order = what the user sees populate first.
        # "resume" runs right after "analyze" so the tailored resume applies the
        # fresh fit-analysis suggestions. ("pitch" ignores the language hint: it
        # is always tailored in Hebrew, keeping the base pitch verbatim + a
        # job-specific closing station.)
        # "analyze" also runs the ATS keyword check (see _generate_one).
        plan = [("company", "en"), ("analyze", "en"), ("resume", "en"),
                ("salary", "en"), ("note", "en"), ("cover", "en"),
                ("pitch", "he")]
        done: list[str] = []
        failed: list[str] = []
        for idx, (key, lang) in enumerate(plan):
            if idx:
                time.sleep(_AUTOGEN_GAP_S)  # ease off the per-minute rate limit
            try:
                _generate_one(app_id, key, r, language=lang)
                done.append(_BATCH_ITEMS[key])
            except Exception as exc:  # keep going; never lose what already ran
                failed.append(f"{_BATCH_ITEMS[key]} ({exc})")
        if done:
            flash("Generated: " + ", ".join(done) + ".", "ok")
        if failed:
            flash("Failed: " + "; ".join(failed) + ".", "error")

    # Back to the applications list — the new capture appears as the top row.
    return redirect(url_for("main.applications"))


@bp.route("/application/<int:app_id>/star", methods=["POST"])
def star(app_id: int):
    new = tracker.toggle_star(app_id)
    if new is None:
        abort(404)
    return {"starred": new}


@bp.route("/application/<int:app_id>/share")
def share(app_id: int):
    """Standalone HTML snapshot of the AI insights — shareable by email."""
    r = tracker.get_application(app_id)
    if not r:
        abort(404)
    html = render_template(
        "share.html", app=r,
        analysis=tracker.get_ai_analysis(app_id),
        salary=tracker.get_salary_research(app_id),
        company_brief=tracker.get_company_brief(app_id),
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    safe_co = re.sub(r"[^A-Za-z0-9_-]+", "-", r["company"] or "job").strip("-")
    resp = make_response(html)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.headers["Content-Disposition"] = (
        f"attachment; filename=job-analysis-{safe_co}-{app_id}.html")
    return resp


@bp.route("/application/<int:app_id>/status", methods=["POST"])
def set_status(app_id: int):
    if request.is_json:
        new = (request.json or {}).get("status")
        note = ""
    else:
        new = request.form.get("status")
        note = request.form.get("note", "")
    ok = tracker.update_status(app_id, new, note)
    if request.is_json:
        return ({"ok": ok}, 200 if ok else 404)
    flash("Status updated." if ok else "Update failed.", "ok" if ok else "error")
    return redirect(request.referrer or url_for("main.detail", app_id=app_id))


@bp.route("/application/<int:app_id>/reject", methods=["POST"])
def reject(app_id: int):
    f = request.form
    tracker.set_rejection(app_id, stage=f.get("stage", ""),
                          reason=f.get("reason", ""), note=f.get("note", ""))
    flash("Marked rejected.", "ok")
    return redirect(url_for("main.detail", app_id=app_id))


@bp.route("/application/<int:app_id>/note", methods=["POST"])
def note(app_id: int):
    text = request.form.get("text", "").strip()
    if text:
        tracker.add_note(app_id, text)
        flash("Note added.", "ok")
    return redirect(url_for("main.detail", app_id=app_id))


@bp.route("/application/<int:app_id>/description", methods=["POST"])
def description_save(app_id: int):
    """Edit / replace the job description (common after LinkedIn wall captures)."""
    if not tracker.get_application(app_id):
        abort(404)
    text = request.form.get("description", "")
    tracker.set_description(app_id, text)
    flash("Job description saved — match score refreshed.", "ok")
    return redirect(url_for("main.detail", app_id=app_id) + "#job-description")


@bp.route("/application/<int:app_id>/delete", methods=["POST"])
def delete(app_id: int):
    tracker.delete_application(app_id)
    _tailored_path(app_id).unlink(missing_ok=True)
    _tailored_draft_path(app_id).unlink(missing_ok=True)
    flash(f"Deleted #{app_id}.", "ok")
    return redirect(url_for("main.applications"))


def _run_ats_check(app_id: int, r) -> dict:
    """Run the existing ATS keyword check and persist it (same section on detail)."""
    data = ai.ats_check(
        title=r["title"] or "", company=r["company"] or "",
        location=r["location"] or "", description=r["description"] or "",
    )
    tracker.set_ats_check(app_id, data)
    return data


@bp.route("/application/<int:app_id>/analyze", methods=["POST"])
def analyze(app_id: int):
    r = tracker.get_application(app_id)
    if not r:
        abort(404)
    language = request.form.get("language", "en")
    try:
        result = ai.analyze_fit(
            title=r["title"], company=r["company"], location=r["location"] or "",
            description=r["description"] or "", language=language,
        )
        tracker.set_ai_analysis(app_id, result)
        try:
            time.sleep(_AUTOGEN_GAP_S)
            ats = _run_ats_check(app_id, r)
            flash(f"AI fit analysis complete — ATS score {ats.get('ats_score', '?')}%.",
                  "ok")
        except ai.AIError as exc:
            flash(f"AI fit analysis complete, but ATS check failed: {exc}", "error")
    except ai.AIError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.detail", app_id=app_id) + "#analysis")


@bp.route("/application/<int:app_id>/cover-letter", methods=["POST"])
def cover_letter(app_id: int):
    r = tracker.get_application(app_id)
    if not r:
        abort(404)
    instructions = request.form.get("instructions", "").strip()
    language = request.form.get("language", "en")
    try:
        text = ai.cover_letter(
            title=r["title"], company=r["company"], location=r["location"] or "",
            description=r["description"] or "", instructions=instructions,
            language=language,
        )
        tracker.set_cover_letter(app_id, text)
        flash("Cover letter generated.", "ok")
    except ai.AIError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.detail", app_id=app_id) + "#cover")


@bp.route("/application/<int:app_id>/cover-letter/save", methods=["POST"])
def cover_letter_save(app_id: int):
    if not tracker.get_application(app_id):
        abort(404)
    tracker.set_cover_letter(app_id, request.form.get("text", ""))
    flash("Cover letter saved.", "ok")
    return redirect(url_for("main.detail", app_id=app_id) + "#cover")


def _cover_letter_html(text: str, r) -> str:
    """Wrap the cover-letter text in a clean, printable A4 letter layout
    (RTL-aware so Hebrew letters render correctly)."""
    paras = re.split(r"\n\s*\n", text.strip())
    body = "\n".join(
        "<p>" + html_lib.escape(p).replace("\n", "<br>") + "</p>"
        for p in paras if p.strip()
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
    @page {{ size: A4; margin: 22mm 20mm; }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; }}
    body {{ font-family: 'Calibri','Segoe UI',Arial,sans-serif; color:#1a1a1a;
            font-size: 11.5pt; line-height: 1.55; }}
    p {{ margin: 0 0 11px; }}
    </style></head><body dir="auto">{body}</body></html>"""


@bp.route("/application/<int:app_id>/cover-letter/pdf", methods=["GET", "POST"])
def cover_letter_pdf(app_id: int):
    """Export the cover letter as a PDF. POST sends the (possibly edited)
    textarea content; GET falls back to the saved letter."""
    r = tracker.get_application(app_id)
    if not r:
        abort(404)
    text = (request.form.get("text") or r["cover_letter"] or "").strip()
    if not text:
        flash("Generate a cover letter first.", "error")
        return redirect(url_for("main.detail", app_id=app_id) + "#cover")

    import io
    html = _cover_letter_html(text, r)
    name = re.sub(r"[^A-Za-z0-9]+", "_",
                  f"cover_letter_{r['company']}_{r['title']}").strip("_")

    data = _render_pdf_with_browser(html)
    if data:
        return send_file(io.BytesIO(data), mimetype="application/pdf",
                         as_attachment=True, download_name=f"{name}.pdf")
    try:
        from xhtml2pdf import pisa
        out = io.BytesIO()
        result = pisa.CreatePDF(src=html, dest=out, encoding="utf-8")
        if not result.err and out.getbuffer().nbytes > 0:
            out.seek(0)
            return send_file(out, mimetype="application/pdf", as_attachment=True,
                             download_name=f"{name}.pdf")
    except Exception:
        pass
    flash("Couldn't build the PDF on the server (no local browser found). "
          "Use your browser's Print / Save as PDF instead.", "error")
    return redirect(url_for("main.detail", app_id=app_id) + "#cover")


@bp.route("/application/<int:app_id>/recruiter-note", methods=["POST"])
def recruiter_note(app_id: int):
    r = tracker.get_application(app_id)
    if not r:
        abort(404)
    instructions = request.form.get("instructions", "").strip()
    language = request.form.get("language", "en")
    try:
        text = ai.recruiter_note(
            title=r["title"], company=r["company"], instructions=instructions,
            language=language,
        )
        tracker.set_recruiter_note(app_id, text)
        flash("Recruiter note generated.", "ok")
    except ai.AIError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.detail", app_id=app_id) + "#note")


@bp.route("/application/<int:app_id>/recruiter-note/save", methods=["POST"])
def recruiter_note_save(app_id: int):
    if not tracker.get_application(app_id):
        abort(404)
    tracker.set_recruiter_note(app_id, request.form.get("text", ""))
    flash("Recruiter note saved.", "ok")
    return redirect(url_for("main.detail", app_id=app_id) + "#note")


@bp.route("/application/<int:app_id>/feedback-request", methods=["POST"])
def feedback_request(app_id: int):
    """Generate a polite 'why was I rejected / how can I improve' email."""
    r = tracker.get_application(app_id)
    if not r:
        abort(404)
    language = request.form.get("language", "en")
    instructions = request.form.get("instructions", "").strip()
    try:
        text = ai.feedback_request(
            title=r["title"], company=r["company"],
            stage=r["rejection_stage"] or "", reason=r["rejection_reason"] or "",
            instructions=instructions, language=language,
        )
        tracker.set_feedback_request(app_id, text)
        flash("Feedback-request letter generated.", "ok")
    except ai.AIError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.detail", app_id=app_id) + "#feedback")


@bp.route("/application/<int:app_id>/feedback-request/save", methods=["POST"])
def feedback_request_save(app_id: int):
    if not tracker.get_application(app_id):
        abort(404)
    tracker.set_feedback_request(app_id, request.form.get("text", ""))
    flash("Feedback-request letter saved.", "ok")
    return redirect(url_for("main.detail", app_id=app_id) + "#feedback")


@bp.route("/application/<int:app_id>/interview-prep", methods=["POST"])
def interview_prep(app_id: int):
    r = tracker.get_application(app_id)
    if not r:
        abort(404)
    instructions = request.form.get("instructions", "").strip()
    language = request.form.get("language", "en")
    try:
        text = ai.interview_prep(
            title=r["title"], company=r["company"], location=r["location"] or "",
            description=r["description"] or "", instructions=instructions,
            language=language,
        )
        tracker.set_interview_prep(app_id, text)
        flash("Interview prep generated.", "ok")
    except ai.AIError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.detail", app_id=app_id) + "#prep")


@bp.route("/application/<int:app_id>/interview-prep/save", methods=["POST"])
def interview_prep_save(app_id: int):
    if not tracker.get_application(app_id):
        abort(404)
    tracker.set_interview_prep(app_id, request.form.get("text", ""))
    flash("Interview prep saved.", "ok")
    return redirect(url_for("main.detail", app_id=app_id) + "#prep")


@bp.route("/application/<int:app_id>/mock-interview", methods=["POST"])
def mock_interview(app_id: int):
    r = tracker.get_application(app_id)
    if not r:
        abort(404)
    language = request.form.get("language", "en")
    try:
        data = ai.mock_interview(
            title=r["title"], company=r["company"], location=r["location"] or "",
            description=r["description"] or "", language=language,
        )
        tracker.set_mock_interview(app_id, json.dumps(data, ensure_ascii=False))
        flash("Mock interview generated.", "ok")
    except ai.AIError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.detail", app_id=app_id) + "#mock")


@bp.route("/application/<int:app_id>/qa-exercise", methods=["POST"])
def qa_exercise(app_id: int):
    """Generate a practice QA testing-scenario exercise tailored to this job."""
    r = tracker.get_application(app_id)
    if not r:
        abort(404)
    language = request.form.get("language", "he")
    try:
        text = ai.qa_exercise(
            title=r["title"], company=r["company"], location=r["location"] or "",
            description=r["description"] or "", language=language,
        )
        tracker.set_qa_exercise(app_id, text)
        flash("QA exercise generated.", "ok")
    except ai.AIError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.detail", app_id=app_id) + "#exercise")


@bp.route("/application/<int:app_id>/qa-exercise/save", methods=["POST"])
def qa_exercise_save(app_id: int):
    if not tracker.get_application(app_id):
        abort(404)
    tracker.set_qa_exercise(app_id, request.form.get("text", ""))
    flash("QA exercise saved.", "ok")
    return redirect(url_for("main.detail", app_id=app_id) + "#exercise")


# Items the one-click "Generate with AI" panel can produce, in display order.
_BATCH_ITEMS = {
    "analyze": "fit analysis",
    "resume": "tailored resume",
    "cover": "cover letter",
    "note": "recruiter note",
    "prep": "interview prep",
    "mock": "mock interview",
    "exercise": "QA exercise",
    "pitch": "about-me pitch",
    "company": "company research",
    "salary": "salary research",
    "ats": "ATS keyword check",
}

# Small pause between back-to-back AI calls so a burst of auto-gen requests
# doesn't trip the provider's per-minute rate limit (e.g. Gemini free tier).
_AUTOGEN_GAP_S = 1.0


def _generate_one(app_id, key, r, language="en", instructions=""):
    """Generate a single AI artefact and persist it. Raises on failure.

    ``company`` honours the given language (so callers can force Hebrew);
    fit analysis is always bilingual and the recruiter note always includes an
    English version, regardless of ``language``.
    """
    title, company = r["title"], r["company"]
    location, description = r["location"] or "", r["description"] or ""
    if key == "analyze":
        tracker.set_ai_analysis(app_id, ai.analyze_fit(
            title=title, company=company, location=location,
            description=description, language=language))
        # Always refresh the ATS keyword section with the same analysis pass.
        time.sleep(_AUTOGEN_GAP_S)
        _run_ats_check(app_id, r)
    elif key == "cover":
        tracker.set_cover_letter(app_id, ai.cover_letter(
            title=title, company=company, location=location,
            description=description, instructions=instructions, language=language))
    elif key == "note":
        tracker.set_recruiter_note(app_id, ai.recruiter_note(
            title=title, company=company, instructions=instructions,
            language=language))
    elif key == "prep":
        tracker.set_interview_prep(app_id, ai.interview_prep(
            title=title, company=company, location=location,
            description=description, instructions=instructions, language=language))
    elif key == "mock":
        data = ai.mock_interview(
            title=title, company=company, location=location,
            description=description, language=language)
        tracker.set_mock_interview(app_id, json.dumps(data, ensure_ascii=False))
    elif key == "exercise":
        tracker.set_qa_exercise(app_id, ai.qa_exercise(
            title=title, company=company, location=location,
            description=description, language=language))
    elif key == "pitch":
        base = (r["pitch"] or "").strip() or pitch.load_base_pitch()
        res = ai.tailor_pitch(
            title=title, company=company, location=location,
            description=description, base_pitch=base, language="he")
        notes = "\n".join(f"- {s}" for s in res.get("suggestions", []))
        tracker.set_pitch(app_id, res["script"], notes=notes)
    elif key == "company":
        tracker.set_company_brief(app_id, ai.company_research(
            company=company, location=location, title=title,
            description=description, language=language))
    elif key == "salary":
        tracker.set_salary_research(app_id, ai.salary_research(
            title=title, company=company, location=location,
            description=description))
    elif key == "ats":
        tracker.set_ats_check(app_id, ai.ats_check(
            title=title, company=company, location=location,
            description=description))
    elif key == "resume":
        # Auto-apply the fit-analysis suggestions (when present) as the
        # tailoring instructions — same as ticking them all on the form.
        analysis = tracker.get_ai_analysis(app_id) or {}
        instructions = "\n".join(
            f"- [{s.get('target', 'general')}] {s.get('action', '')}"
            for s in analysis.get("suggestions") or []
            if isinstance(s, dict) and s.get("action"))
        if not instructions:
            instructions = "Tailor the resume to best match this job posting."
        html = ai.tailor_resume(
            title=title, company=company, description=description,
            instructions=instructions)
        if _tailored_path(app_id).exists():
            # Don't overwrite silently — park as a draft for side-by-side review.
            _tailored_draft_path(app_id).write_text(html, encoding="utf-8")
        else:
            _tailored_path(app_id).write_text(html, encoding="utf-8")
            tracker.mark_tailored(app_id)


@bp.route("/application/<int:app_id>/generate", methods=["POST"])
def generate_batch(app_id: int):
    """Generate several AI artefacts in one request (the checkbox panel).

    Each selected item runs in turn and is saved as soon as it completes, so a
    failure (or the user closing the tab) never loses items already finished.
    """
    r = tracker.get_application(app_id)
    if not r:
        abort(404)
    items = request.form.getlist("items")
    language = request.form.get("language", "en")
    instructions = request.form.get("instructions", "").strip()
    if not items:
        flash("Pick at least one thing to generate.", "error")
        return redirect(url_for("main.detail", app_id=app_id))

    done: list[str] = []
    failed: list[str] = []

    selected = [k for k in _BATCH_ITEMS if k in items]
    # Fit analysis already runs ATS — don't pay for it twice in the same batch.
    if "analyze" in selected and "ats" in selected:
        selected = [k for k in selected if k != "ats"]
    for idx, key in enumerate(selected):
        if idx:
            time.sleep(_AUTOGEN_GAP_S)  # ease off the per-minute rate limit
        try:
            _generate_one(app_id, key, r, language=language, instructions=instructions)
            label = _BATCH_ITEMS[key]
            if key == "analyze":
                label = "fit analysis + ATS check"
            done.append(label)
        except Exception as exc:  # keep going so one failure doesn't lose the rest
            failed.append(f"{_BATCH_ITEMS[key]} ({exc})")

    if done:
        flash("Generated: " + ", ".join(done) + ".", "ok")
    if failed:
        flash("Failed: " + "; ".join(failed) + ".", "error")
    return redirect(url_for("main.detail", app_id=app_id))


@bp.route("/application/<int:app_id>/ats-check", methods=["POST"])
def ats_check(app_id: int):
    """Simulate an ATS keyword screen of the resume against this job."""
    r = tracker.get_application(app_id)
    if not r:
        abort(404)
    try:
        data = _run_ats_check(app_id, r)
        flash(f"ATS keyword check complete — score {data.get('ats_score', '?')}%.",
              "ok")
    except ai.AIError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.detail", app_id=app_id) + "#ats")


@bp.route("/application/<int:app_id>/salary", methods=["POST"])
def salary_research(app_id: int):
    """AI research of the expected monthly gross salary (ILS) for this job."""
    r = tracker.get_application(app_id)
    if not r:
        abort(404)
    try:
        data = ai.salary_research(
            title=r["title"] or "", company=r["company"] or "",
            location=r["location"] or "", description=r["description"] or "",
        )
        tracker.set_salary_research(app_id, data)
        flash("Salary research complete.", "ok")
    except ai.AIError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.detail", app_id=app_id) + "#salary")


@bp.route("/application/<int:app_id>/company", methods=["POST"])
def company_research(app_id: int):
    """AI web research about the company on this application."""
    r = tracker.get_application(app_id)
    if not r:
        abort(404)
    try:
        brief = ai.company_research(
            company=r["company"], location=r["location"] or "",
            title=r["title"] or "", description=r["description"] or "",
        )
        tracker.set_company_brief(app_id, brief)
        flash("Company research complete.", "ok")
    except ai.AIError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.detail", app_id=app_id) + "#company")


@bp.route("/tts", methods=["POST"])
def tts_speak():
    """Synthesize text to natural-voice MP3 (Microsoft neural voices)."""
    text = (request.form.get("text") or "").strip()
    voice = (request.form.get("voice") or "").strip()
    try:
        rate = float(request.form.get("rate") or 1.0)
    except (TypeError, ValueError):
        rate = 1.0
    if not text:
        return Response("Nothing to read.", status=400)
    # Guard against pathologically long inputs (neural TTS is per-request).
    text = text[:8000]
    try:
        audio = tts.synthesize(text, voice, rate)
    except ValueError as exc:
        return Response(str(exc), status=400)
    except Exception as exc:  # network/SSL/service errors
        return Response(f"Voice service unavailable: {exc}", status=502)
    resp = Response(audio, mimetype="audio/mpeg")
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@bp.route("/application/<int:app_id>/pitch", methods=["POST"])
def pitch_save(app_id: int):
    if not tracker.get_application(app_id):
        abort(404)
    tracker.set_pitch(app_id, request.form.get("text", ""))
    flash("Pitch saved for this job.", "ok")
    return redirect(url_for("main.detail", app_id=app_id) + "#pitch")


@bp.route("/application/<int:app_id>/pitch/tailor", methods=["POST"])
def pitch_tailor(app_id: int):
    r = tracker.get_application(app_id)
    if not r:
        abort(404)
    base = (r["pitch"] or "").strip() or pitch.load_base_pitch()
    try:
        result = ai.tailor_pitch(
            title=r["title"], company=r["company"], location=r["location"] or "",
            description=r["description"] or "", base_pitch=base, language="he",
        )
        notes = "\n".join(f"- {s}" for s in result.get("suggestions", []))
        tracker.set_pitch(app_id, result["script"], notes=notes)
        flash("Pitch tailored for this job.", "ok")
    except ai.AIError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.detail", app_id=app_id) + "#pitch")


@bp.route("/application/<int:app_id>/tailor", methods=["POST"])
def tailor(app_id: int):
    r = tracker.get_application(app_id)
    if not r:
        abort(404)
    selected = request.form.getlist("suggestion")
    extra = request.form.get("instructions", "").strip()
    instructions = "\n".join(f"- {s}" for s in selected)
    if extra:
        instructions += f"\n- {extra}"
    if not instructions:
        instructions = "Tailor the resume to best match this job posting."
    try:
        html = ai.tailor_resume(
            title=r["title"], company=r["company"],
            description=r["description"] or "", instructions=instructions,
        )
        if _tailored_path(app_id).exists():
            # A tailored resume already exists: park the new one as a pending
            # draft so the user reviews the differences before it replaces it.
            _tailored_draft_path(app_id).write_text(html, encoding="utf-8")
            flash("New tailored resume generated — review the differences and "
                  "accept or discard it.", "ok")
            return redirect(url_for("main.resume_compare", app_id=app_id))
        _tailored_path(app_id).write_text(html, encoding="utf-8")
        tracker.mark_tailored(app_id)
        flash("Tailored resume generated.", "ok")
        return redirect(url_for("main.resume_review", app_id=app_id))
    except ai.AIError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.detail", app_id=app_id))


@bp.route("/application/<int:app_id>/resume/review")
def resume_review(app_id: int):
    r = tracker.get_application(app_id)
    if not r or not _tailored_path(app_id).exists():
        abort(404)
    html = _tailored_path(app_id).read_text(encoding="utf-8")
    return render_template("resume_review.html", app=r, tailored_html=html,
                           has_draft=_tailored_draft_path(app_id).exists())


@bp.route("/application/<int:app_id>/resume/save", methods=["POST"])
def resume_save(app_id: int):
    html = request.form.get("html", "")
    if html.strip():
        _tailored_path(app_id).write_text(html, encoding="utf-8")
        flash("Saved edits.", "ok")
    return redirect(url_for("main.resume_review", app_id=app_id))


@bp.route("/application/<int:app_id>/resume/refine", methods=["POST"])
def resume_refine(app_id: int):
    """Fine-tune the existing tailored resume with free-form Gemini instructions."""
    r = tracker.get_application(app_id)
    if not r or not _tailored_path(app_id).exists():
        abort(404)
    instructions = request.form.get("instructions", "").strip()
    if not instructions:
        flash("Type a fine-tune instruction first.", "error")
        return redirect(url_for("main.resume_review", app_id=app_id))
    try:
        current = _tailored_path(app_id).read_text(encoding="utf-8")
        html = ai.tailor_resume(
            title=r["title"], company=r["company"],
            description=r["description"] or "", instructions=instructions,
            original_html=current,
        )
        # Never overwrite directly: park the refinement as a pending draft so
        # the user sees exactly what changed before accepting it.
        _tailored_draft_path(app_id).write_text(html, encoding="utf-8")
        flash("Refined resume ready — review the differences and accept or "
              "discard it.", "ok")
        return redirect(url_for("main.resume_compare", app_id=app_id))
    except ai.AIError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.resume_review", app_id=app_id))


@bp.route("/application/<int:app_id>/resume/view")
def resume_view(app_id: int):
    """Raw tailored HTML (shown inside the review iframe / print)."""
    if not _tailored_path(app_id).exists():
        abort(404)
    return Response(_tailored_path(app_id).read_text(encoding="utf-8"),
                    mimetype="text/html")


@bp.route("/application/<int:app_id>/resume/draft/view")
def resume_draft_view(app_id: int):
    """Raw pending-draft HTML (shown inside the compare iframe)."""
    if not _tailored_draft_path(app_id).exists():
        abort(404)
    return Response(_tailored_draft_path(app_id).read_text(encoding="utf-8"),
                    mimetype="text/html")


def _html_visible_text(html: str) -> str:
    """Visible text of an HTML resume, one block element per line (for diffing)."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["style", "script", "head"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


@bp.route("/application/<int:app_id>/resume/compare")
def resume_compare(app_id: int):
    """Current tailored resume vs the pending AI regeneration, side by side."""
    r = tracker.get_application(app_id)
    if not r or not _tailored_draft_path(app_id).exists():
        flash("No pending resume revision to compare — generate one first.", "error")
        return redirect(url_for("main.detail", app_id=app_id))
    if not _tailored_path(app_id).exists():
        # Nothing to compare against — just promote the draft.
        _tailored_draft_path(app_id).rename(_tailored_path(app_id))
        tracker.mark_tailored(app_id)
        return redirect(url_for("main.resume_review", app_id=app_id))
    old_text = _html_visible_text(_tailored_path(app_id).read_text(encoding="utf-8"))
    new_text = _html_visible_text(_tailored_draft_path(app_id).read_text(encoding="utf-8"))
    old_html, new_html = _diff_sides(old_text, new_text)
    return render_template("resume_compare.html", app=r,
                           old_html=old_html, new_html=new_html)


@bp.route("/application/<int:app_id>/resume/draft/apply", methods=["POST"])
def resume_draft_apply(app_id: int):
    """Accept the pending revision: it becomes the tailored resume."""
    draft = _tailored_draft_path(app_id)
    if not draft.exists():
        flash("No pending resume revision to apply.", "error")
        return redirect(url_for("main.detail", app_id=app_id))
    _tailored_path(app_id).write_text(draft.read_text(encoding="utf-8"),
                                      encoding="utf-8")
    draft.unlink(missing_ok=True)
    tracker.mark_tailored(app_id)
    flash("Revision applied — it is now the tailored resume.", "ok")
    return redirect(url_for("main.resume_review", app_id=app_id))


@bp.route("/application/<int:app_id>/resume/draft/discard", methods=["POST"])
def resume_draft_discard(app_id: int):
    """Throw away the pending revision; the current tailored resume stays."""
    _tailored_draft_path(app_id).unlink(missing_ok=True)
    flash("Revision discarded — the tailored resume is unchanged.", "ok")
    return redirect(url_for("main.resume_review", app_id=app_id))


@bp.route("/application/<int:app_id>/resume/pdf")
def resume_pdf(app_id: int):
    if not _tailored_path(app_id).exists():
        abort(404)
    import io
    html = _tailored_path(app_id).read_text(encoding="utf-8")
    r = tracker.get_application(app_id)
    name = re.sub(r"[^A-Za-z0-9]+", "_", f"resume_{r['company']}_{r['title']}").strip("_")

    # 1) Best fidelity: render with the local headless browser (Chromium engine,
    #    identical to the Print button). Handles all modern CSS.
    data = _render_pdf_with_browser(html)
    if data:
        return send_file(io.BytesIO(data), mimetype="application/pdf",
                         as_attachment=True, download_name=f"{name}.pdf")

    # 2) Fallback: xhtml2pdf (limited CSS support, but no browser needed).
    try:
        from xhtml2pdf import pisa
        out = io.BytesIO()
        result = pisa.CreatePDF(src=html, dest=out, encoding="utf-8")
        if not result.err and out.getbuffer().nbytes > 0:
            out.seek(0)
            return send_file(out, mimetype="application/pdf", as_attachment=True,
                             download_name=f"{name}.pdf")
    except Exception:
        pass

    flash("Couldn't build the PDF on the server (no local browser found and the "
          "fallback renderer failed). Use the Print / Save as PDF button for a "
          "pixel-perfect PDF.", "error")
    return redirect(url_for("main.resume_review", app_id=app_id))


# --------------------------------------------------------------------------- #
# Resume Builder: a spoken Hebrew interview that produces an English resume.
@bp.route("/resume-builder")
def resume_builder():
    """The conversational resume-builder page (Hebrew interview → English CV)."""
    rd = _readiness()
    return render_template(
        "resume_builder.html",
        ready=rd,
        ai_on=ai.is_configured(),
        is_gemini=ai.active_provider() == "gemini",
        first_question=ai.RESUME_BUILDER_FIRST_QUESTION,
        has_built=config.BUILT_RESUME_PATH.exists(),
    )


@bp.route("/resume-builder/transcribe", methods=["POST"])
def resume_builder_transcribe():
    """Transcribe an uploaded Hebrew audio answer to text (Gemini)."""
    file = request.files.get("audio")
    if not file:
        return {"error": "No audio was uploaded."}, 400
    data = file.read()
    if not data:
        return {"error": "The audio recording was empty."}, 400
    mime = (file.mimetype or "").lower()
    # Phones often send an empty or generic type for native recordings; infer a
    # sensible audio MIME from the filename so Gemini accepts it.
    if not mime or mime in ("application/octet-stream", "application/x-www-form-urlencoded"):
        import mimetypes
        guessed, _ = mimetypes.guess_type(file.filename or "")
        mime = (guessed or "audio/mp4")
    # Strip any codec suffix (e.g. "audio/webm;codecs=opus").
    mime = mime.split(";", 1)[0].strip()
    try:
        text = ai.transcribe_audio(data, mime_type=mime, language="he")
    except ai.AIError as exc:
        return {"error": str(exc)}, 502
    return {"text": text}


@bp.route("/resume-builder/next", methods=["POST"])
def resume_builder_next():
    """Return the next Hebrew interview question given the conversation so far."""
    payload = request.get_json(silent=True) or {}
    conversation = payload.get("conversation") or []
    try:
        result = ai.interview_question(conversation)
    except ai.AIError as exc:
        return {"error": str(exc)}, 502
    return result


@bp.route("/resume-builder/build", methods=["POST"])
def resume_builder_build():
    """Generate the English resume HTML from the full interview transcript."""
    payload = request.get_json(silent=True) or {}
    conversation = payload.get("conversation") or []
    try:
        html = ai.build_resume_from_interview(conversation)
    except ai.AIError as exc:
        return {"error": str(exc)}, 502
    config.BUILT_RESUME_PATH.write_text(html, encoding="utf-8")
    return {"ok": True, "url": url_for("main.resume_builder_review")}


@bp.route("/resume-builder/review")
def resume_builder_review():
    if not config.BUILT_RESUME_PATH.exists():
        flash("Build a resume from the interview first.", "error")
        return redirect(url_for("main.resume_builder"))
    html = config.BUILT_RESUME_PATH.read_text(encoding="utf-8")
    return render_template("resume_built_review.html", built_html=html)


@bp.route("/resume-builder/view")
def resume_builder_view():
    """Raw built-resume HTML (shown inside the review iframe / print)."""
    if not config.BUILT_RESUME_PATH.exists():
        abort(404)
    return Response(config.BUILT_RESUME_PATH.read_text(encoding="utf-8"),
                    mimetype="text/html")


@bp.route("/resume-builder/save", methods=["POST"])
def resume_builder_save():
    html = request.form.get("html", "")
    if html.strip():
        config.BUILT_RESUME_PATH.write_text(html, encoding="utf-8")
        flash("Saved edits.", "ok")
    return redirect(url_for("main.resume_builder_review"))


@bp.route("/resume-builder/pdf")
def resume_builder_pdf():
    if not config.BUILT_RESUME_PATH.exists():
        abort(404)
    import io
    html = config.BUILT_RESUME_PATH.read_text(encoding="utf-8")

    data = _render_pdf_with_browser(html)
    if data:
        return send_file(io.BytesIO(data), mimetype="application/pdf",
                         as_attachment=True, download_name="resume.pdf")
    try:
        from xhtml2pdf import pisa
        out = io.BytesIO()
        result = pisa.CreatePDF(src=html, dest=out, encoding="utf-8")
        if not result.err and out.getbuffer().nbytes > 0:
            out.seek(0)
            return send_file(out, mimetype="application/pdf", as_attachment=True,
                             download_name="resume.pdf")
    except Exception:
        pass
    flash("Couldn't build the PDF on the server (no local browser found). "
          "Use the Print / Save as PDF button instead.", "error")
    return redirect(url_for("main.resume_builder_review"))


# --------------------------------------------------------------------------- #
# Job search — cache last query + results per profile (web search is slow).
# --------------------------------------------------------------------------- #
def _last_search_path() -> Path:
    return Path(config.PROFILE_DIR) / "last_search.json"


def _job_result_to_dict(job: JobResult) -> dict:
    return {
        "source": job.source,
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "url": job.url,
        "description": job.description,
        "salary": job.salary,
        "posted": job.posted,
        "external_id": job.external_id,
    }


def _job_result_from_dict(data: dict) -> JobResult:
    return JobResult(
        source=data.get("source", ""),
        title=data.get("title", ""),
        company=data.get("company", ""),
        location=data.get("location", ""),
        url=data.get("url", ""),
        description=data.get("description", ""),
        salary=data.get("salary", ""),
        posted=data.get("posted", ""),
        external_id=data.get("external_id", ""),
    )


def _save_last_search(query: str, location: str, results: list) -> None:
    payload = {
        "query": query,
        "location": location,
        "searched_at": now_iso(),
        "results": [
            {"job": _job_result_to_dict(item["job"]), "score": item["score"]}
            for item in results
        ],
    }
    path = _last_search_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_last_search() -> dict | None:
    try:
        data = json.loads(_last_search_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _enrich_search_results(results: list, query: str = "",
                           *, show_hidden: bool = False) -> list:
    if query:
        results = [
            item for item in results
            if job_matches_query(
                query,
                title=getattr(item.get("job"), "title", "") or "",
                description=getattr(item.get("job"), "description", "") or "",
            )
        ]
    if not show_hidden:
        hide_keys = search_hidden.hidden_key_set()
        if hide_keys:
            results = [
                item for item in results
                if not search_hidden.is_hidden(
                    getattr(item.get("job"), "url", "") or "",
                    getattr(item.get("job"), "company", "") or "",
                    getattr(item.get("job"), "title", "") or "",
                    key_set=hide_keys,
                )
            ]
    results = tracker.enrich_search_results(results)
    return search_meta.attach_meta(results)


def _short_posted(posted: str) -> str:
    """Normalize assorted source timestamps to YYYY-MM-DD for the table."""
    s = (posted or "").strip()
    if not s:
        return ""
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    if "T" in s and len(s) >= 10:
        return s.split("T", 1)[0][:10]
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(s).date().isoformat()
    except (TypeError, ValueError, IndexError):
        return s[:16]


def _parse_job_tokens(tokens: list[str]) -> list[dict]:
    """Checkbox values are ``url|||title|||company``."""
    out = []
    for tok in tokens:
        parts = (tok or "").split("|||", 2)
        if len(parts) < 1 or not parts[0].strip():
            continue
        out.append({
            "url": parts[0].strip(),
            "title": parts[1].strip() if len(parts) > 1 else "",
            "company": parts[2].strip() if len(parts) > 2 else "",
        })
    return out


# --------------------------------------------------------------------------- #
@bp.route("/search", methods=["GET", "POST"])
def search():
    results = []
    query = request.values.get("query", "")
    location = request.values.get("location", "Israel")
    show_dismissed = request.args.get("dismissed") == "1"
    show_ignored = request.args.get("ignored") == "1"
    cached_at = None
    configured = [s.name for s in get_sources()]
    rd = _readiness()
    hide_counts = search_hidden.counts()

    # Dismissed / ignored lists — separate from live search results.
    if show_dismissed or show_ignored:
        rows = search_hidden.list_hidden(ignored=True if show_ignored else False)
        return render_template(
            "search.html", results=[], query=query, location=location,
            configured=configured, jooble_usage=None, ready=rd, cached_at=None,
            show_dismissed=show_dismissed, show_ignored=show_ignored,
            hidden_rows=rows, hide_counts=hide_counts,
        )

    if request.method == "POST" and not rd["ready"]:
        for msg in rd["issues"]:
            flash(msg, "error")
        cached = _load_last_search()
        if cached:
            query = cached.get("query", query)
            location = cached.get("location", location)
            cached_at = cached.get("searched_at")
            results = _enrich_search_results([
                {"job": _job_result_from_dict(r["job"]), "score": r["score"]}
                for r in cached.get("results", [])
                if isinstance(r, dict) and isinstance(r.get("job"), dict)
            ], query=query)
        return render_template(
            "search.html", results=results, query=query, location=location,
            configured=configured, jooble_usage=None, ready=rd,
            cached_at=cached_at, show_dismissed=False, show_ignored=False,
            hidden_rows=[], hide_counts=hide_counts,
        )
    if request.method == "GET":
        cached = _load_last_search()
        if cached:
            query = cached.get("query", query)
            location = cached.get("location", location)
            cached_at = cached.get("searched_at")
            results = _enrich_search_results([
                {"job": _job_result_from_dict(r["job"]), "score": r["score"]}
                for r in cached.get("results", [])
                if isinstance(r, dict) and isinstance(r.get("job"), dict)
            ], query=query)
    if request.method == "POST" and configured:
        prof = resume_mod.load_profile()
        if not query:
            query = " OR ".join(prof.get("target_titles", [])[:3])
        hide_keys = search_hidden.hidden_key_set()
        for src in get_sources():
            try:
                count = 0
                # Ask sources for a deeper pool; we filter irrelevant titles
                # (e.g. Remotive returning sales roles for query "QA").
                for job in src.search(query, location=location, limit=40):
                    if not job_matches_query(
                            query, title=job.title,
                            description=job.description or ""):
                        continue
                    if search_hidden.is_hidden(
                            job.url, job.company, job.title, key_set=hide_keys):
                        continue
                    m = score_job(job.title, job.description, prof)
                    results.append({"job": job, "score": m.score})
                    count += 1
                    if count >= 20:
                        break
                if src.name != "websearch":
                    flash(f"{src.name}: {count} result(s).", "ok")
                else:
                    soft = sum(
                        1 for item in results
                        if getattr(item.get("job"), "raw", None)
                        and (item["job"].raw or {}).get("soft_verify")
                    ) if count else 0
                    if count and soft:
                        flash(f"websearch: {count} result(s) for “{location or 'any'}” "
                              f"— some links could not be fully verified live.",
                              "ok")
                    elif count:
                        flash(f"websearch: {count} live posting(s) in "
                              f"“{location or 'any'}”.", "ok")
                    else:
                        # DuckDuckGo is often rate-limited or empty; other
                        # boards (Drushim/AllJobs/Matrix…) usually still work.
                        others = sum(
                            1 for item in results
                            if not str(getattr(item.get("job"), "source", "")
                                       ).startswith("web:")
                        )
                        tip = ("DuckDuckGo found nothing useful this time "
                               f"for “{location or 'any'}”. Wait ~1 min and "
                               "retry, or use a shorter keyword.")
                        if others:
                            flash(f"websearch: skipped — {tip} "
                                  f"({others} result(s) from other sources).",
                                  "ok")
                        else:
                            flash(f"websearch: {tip}", "error")
            except Exception as exc:
                flash(f"{src.name}: {exc}", "error")
        results.sort(key=lambda x: x["score"], reverse=True)
        _save_last_search(query, location, results)
        cached_at = now_iso()
    # Jooble free-tier usage feedback (only while the source is active).
    ju = (usage.jooble_usage(config.JOOBLE_API_KEY)
          if config.JOOBLE_API_KEY and "jooble" in configured else None)
    if ju and ju["tracked"]:
        if ju["exhausted"]:
            flash("Jooble free quota (500) is used up — get a new key at "
                  "jooble.org/api/about and update it in Settings.", "error")
        elif ju["low"]:
            flash(f"Heads-up: only {ju['remaining']} Jooble requests left of "
                  f"{ju['limit']}. Consider getting a fresh key soon.", "error")
    results = _enrich_search_results(results, query=query)
    for item in results:
        job = item.get("job")
        if job is not None:
            item["posted"] = _short_posted(getattr(job, "posted", "") or "")
    return render_template(
        "search.html", results=results, query=query, location=location,
        configured=configured, jooble_usage=ju, ready=rd, cached_at=cached_at,
        show_dismissed=False, show_ignored=False, hidden_rows=[],
        hide_counts=hide_counts,
    )


@bp.route("/search/save", methods=["POST"])
def search_save():
    f = request.form
    score = score_job(f.get("title", ""), f.get("description", "")).score
    app_id = tracker.add_application(
        company=f.get("company", "(unknown)"), title=f.get("title", "(unknown)"),
        location=f.get("location", ""), url=f.get("url", ""),
        description=f.get("description", ""), salary=f.get("salary", ""),
        source=f.get("source", "search"), status="saved", match_score=score,
    )
    flash(f"Saved as #{app_id}.", "ok")
    return redirect(url_for("main.detail", app_id=app_id))


@bp.route("/search/dismiss", methods=["POST"])
def search_dismiss():
    f = request.form
    try:
        search_hidden.hide(
            url=f.get("url", ""), title=f.get("title", ""),
            company=f.get("company", ""), ignored=False,
        )
        flash("Dismissed — hidden from search results (restore from + dismissed).", "ok")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.search"))


@bp.route("/search/ignore", methods=["POST"])
def search_ignore():
    f = request.form
    try:
        search_hidden.hide(
            url=f.get("url", ""), title=f.get("title", ""),
            company=f.get("company", ""), ignored=True,
        )
        flash("Ignored — this job won't appear in search again.", "ok")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.search"))


@bp.route("/search/restore", methods=["POST"])
def search_restore():
    job_key = (request.form.get("job_key") or "").strip()
    if job_key:
        search_hidden.restore(job_key)
        flash("Restored — will show again in search results.", "ok")
    next_view = request.form.get("next") or ""
    if next_view == "ignored":
        return redirect(url_for("main.search", ignored=1))
    if next_view == "dismissed":
        return redirect(url_for("main.search", dismissed=1))
    return redirect(url_for("main.search"))


@bp.route("/search/unignore", methods=["POST"])
def search_unignore():
    job_key = (request.form.get("job_key") or "").strip()
    if job_key:
        search_hidden.set_ignored(job_key, False)
        flash("Moved to dismissed — restore from there to show in results again.", "ok")
    return redirect(url_for("main.search", ignored=1))


@bp.route("/search/open")
def search_open():
    """Open a result URL and mark it read (mailbox-style)."""
    url = (request.args.get("url") or "").strip()
    title = request.args.get("title") or ""
    company = request.args.get("company") or ""
    if url:
        try:
            search_meta.set_seen(url=url, title=title, company=company, seen=True)
        except ValueError:
            pass
        return redirect(url)
    return redirect(url_for("main.search"))


@bp.route("/search/read", methods=["POST"])
def search_read():
    f = request.form
    try:
        search_meta.set_seen(
            url=f.get("url", ""), title=f.get("title", ""),
            company=f.get("company", ""), seen=True,
        )
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.search"))


@bp.route("/search/comment", methods=["POST"])
def search_comment():
    f = request.form
    try:
        search_meta.set_comment(
            url=f.get("url", ""), title=f.get("title", ""),
            company=f.get("company", ""), comment=f.get("comment", ""),
        )
        flash("Comment saved.", "ok")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.search"))


@bp.route("/search/bulk", methods=["POST"])
def search_bulk():
    action = (request.form.get("action") or "").strip()
    jobs = _parse_job_tokens(request.form.getlist("jobs"))
    if not jobs:
        flash("Select at least one search result.", "error")
        return redirect(url_for("main.search"))
    if action == "read":
        n = search_meta.set_seen_many(jobs, seen=True)
        flash(f"Marked {n} result(s) as read.", "ok")
    elif action == "dismiss":
        n = 0
        for j in jobs:
            try:
                search_hidden.hide(
                    url=j["url"], title=j["title"], company=j["company"],
                    ignored=False)
                n += 1
            except ValueError:
                continue
        flash(f"Dismissed {n} result(s).", "ok")
    elif action == "ignore":
        n = 0
        for j in jobs:
            try:
                search_hidden.hide(
                    url=j["url"], title=j["title"], company=j["company"],
                    ignored=True)
                n += 1
            except ValueError:
                continue
        flash(f"Ignored {n} result(s).", "ok")
    else:
        flash("Unknown bulk action.", "error")
    return redirect(url_for("main.search"))


# --------------------------------------------------------------------------- #
@bp.route("/export/csv")
def export_csv():
    return Response(
        exporter.to_csv(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=applications.csv"},
    )


@bp.route("/export/xlsx")
def export_xlsx():
    import io
    data = exporter.to_xlsx()
    return send_file(
        io.BytesIO(data),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name="applications.xlsx",
    )
