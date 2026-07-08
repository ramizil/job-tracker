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
                pitch, tracker, tts, usage)
from .. import profiles as profiles_mod
from .. import resume as resume_mod
from ..matcher import score_job
from ..models import COMMON_REJECTION_REASONS, REJECTION_STAGES, STATUSES
from ..sources import get_sources

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
def inject_profiles():
    """Expose the profile list + active name to every template (topbar switcher)."""
    try:
        return {"profiles": profiles_mod.list_profiles(),
                "active_profile": config.ACTIVE_PROFILE}
    except Exception:
        return {"profiles": [], "active_profile": ""}

def _tailored_path(app_id: int):
    return config.TAILORED_DIR / f"{app_id}.html"


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
    mode (same engine as the Print button). Returns None if unavailable."""
    browser = _find_browser()
    if not browser:
        return None
    tmp = tempfile.mkdtemp(prefix="jt-pdf-")
    src = os.path.join(tmp, "resume.html")
    pdf = os.path.join(tmp, "resume.pdf")
    profile = os.path.join(tmp, "prof")
    try:
        with open(src, "w", encoding="utf-8") as f:
            f.write(html)
        url = "file:///" + src.replace("\\", "/")
        base = [
            browser, "--disable-gpu", "--no-sandbox", "--no-first-run",
            "--no-pdf-header-footer", f"--user-data-dir={profile}",
            f"--print-to-pdf={pdf}", url,
        ]
        for headless in ("--headless=new", "--headless"):
            try:
                subprocess.run([browser, headless, *base[1:]], timeout=60,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               check=False)
            except Exception:
                continue
            if os.path.exists(pdf) and os.path.getsize(pdf) > 0:
                with open(pdf, "rb") as f:
                    return f.read()
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
        updates = {k: request.form.get(k, "").strip() for k in config.EDITABLE_KEYS}
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
        gs_secret_found=Path(str(config.GOOGLE_CLIENT_SECRET)).exists(),
        env_path=str(config.ENV_PATH),
        backup_dir=str(config.BACKUP_DIR),
        data_dir=str(config.PROFILE_DIR),
        jooble_usage=usage.jooble_usage(config.JOOBLE_API_KEY) if config.JOOBLE_API_KEY else None,
        gemini_models=ai.list_models(),
        openai_models=ai.OPENAI_MODELS,
        anthropic_models=ai.ANTHROPIC_MODELS,
        cursor_models=ai.CURSOR_MODELS,
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
                           ai_on=ai.is_configured())


@bp.route("/pitch/draft", methods=["POST"])
def my_pitch_draft():
    """Draft a fresh base pitch from the resume with Gemini (Hebrew)."""
    try:
        pitch.save_base_pitch(ai.pitch_from_resume(language="he"))
        flash("Drafted a new pitch from your resume.", "ok")
    except ai.AIError as exc:
        flash(str(exc), "error")
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
        ai_on=ai.is_configured(),
    )


@bp.route("/applications")
def applications():
    status = request.args.get("status") or None
    rows = tracker.list_applications(status=status,
                                     order_by="starred DESC, updated_at DESC")
    # Gap-free display numbers in creation order (oldest = 1), stable across
    # deletes, filters and sorting — computed over ALL applications so a job
    # keeps the same number on filtered views too.
    all_ids = sorted(r["id"] for r in tracker.list_applications())
    seq = {app_id: n for n, app_id in enumerate(all_ids, start=1)}
    return render_template("applications.html", rows=rows, statuses=STATUSES,
                           active=status, seq=seq)


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
    return render_template(
        "detail.html", app=r, history=tracker.get_history(app_id),
        analysis=analysis, mock=mock, statuses=STATUSES, stages=REJECTION_STAGES,
        reasons=COMMON_REJECTION_REASONS, ai_on=ai.is_configured(),
        has_tailored=_tailored_path(app_id).exists(),
        base_pitch=pitch.load_base_pitch(),
        salary=tracker.get_salary_research(app_id),
        company_brief=tracker.get_company_brief(app_id),
    )


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
    if request.method == "GET":
        return render_template("paste.html", statuses=STATUSES,
                               ai_on=ai.is_configured(), ready=rd,
                               form={}, duplicates=None)

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

    # Warn if an application with the same title + company already exists, but
    # let the user proceed (re-applying, or a genuinely different posting). The
    # "Proceed anyway" submit resends with confirm_duplicate=1 to skip this.
    if not f.get("confirm_duplicate") and company != "(unknown)":
        duplicates = tracker.find_duplicates(title, company)
        if duplicates:
            return render_template(
                "paste.html", statuses=STATUSES, ai_on=ai.is_configured(),
                ready=rd, duplicates=duplicates,
                form={
                    "url": url, "description": text, "title": title,
                    "company": company, "location": location, "salary": salary,
                    "status": f.get("status", "saved"),
                    "source": (f.get("source") or "").strip(),
                    "autogen": bool(f.get("autogen")),
                    "starred": bool(f.get("starred")),
                },
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
    )
    if f.get("starred"):
        tracker.set_star(app_id, True)
    flash(f"Captured job as #{app_id}.", "ok")

    # Auto-run the most useful AI artefacts right after capture (opt-out via the
    # checkbox). Company research and fit analysis are bilingual.
    if ai.is_configured() and f.get("autogen"):
        r = tracker.get_application(app_id)
        # (item-key, language) — order = what the user sees populate first.
        # ("pitch" ignores the language hint: it is always tailored in Hebrew,
        # keeping the base pitch verbatim + a job-specific closing station.)
        plan = [("company", "en"), ("analyze", "en"), ("salary", "en"),
                ("note", "en"), ("cover", "en"), ("pitch", "he")]
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

    return redirect(url_for("main.detail", app_id=app_id))


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


@bp.route("/application/<int:app_id>/delete", methods=["POST"])
def delete(app_id: int):
    tracker.delete_application(app_id)
    _tailored_path(app_id).unlink(missing_ok=True)
    flash(f"Deleted #{app_id}.", "ok")
    return redirect(url_for("main.applications"))


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
        flash("AI fit analysis complete.", "ok")
    except ai.AIError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.detail", app_id=app_id))


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


# Items the one-click "Generate with AI" panel can produce, in display order.
_BATCH_ITEMS = {
    "analyze": "fit analysis",
    "cover": "cover letter",
    "note": "recruiter note",
    "prep": "interview prep",
    "mock": "mock interview",
    "pitch": "about-me pitch",
    "company": "company research",
    "salary": "salary research",
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
    for idx, key in enumerate(selected):
        if idx:
            time.sleep(_AUTOGEN_GAP_S)  # ease off the per-minute rate limit
        try:
            _generate_one(app_id, key, r, language=language, instructions=instructions)
            done.append(_BATCH_ITEMS[key])
        except Exception as exc:  # keep going so one failure doesn't lose the rest
            failed.append(f"{_BATCH_ITEMS[key]} ({exc})")

    if done:
        flash("Generated: " + ", ".join(done) + ".", "ok")
    if failed:
        flash("Failed: " + "; ".join(failed) + ".", "error")
    return redirect(url_for("main.detail", app_id=app_id))


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
    return render_template("resume_review.html", app=r, tailored_html=html)


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
        _tailored_path(app_id).write_text(html, encoding="utf-8")
        tracker.mark_tailored(app_id)
        flash("Resume refined with Gemini.", "ok")
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
@bp.route("/search", methods=["GET", "POST"])
def search():
    results = []
    query = request.values.get("query", "")
    location = request.values.get("location", "Israel")
    configured = [s.name for s in get_sources()]
    rd = _readiness()
    if request.method == "POST" and not rd["ready"]:
        for msg in rd["issues"]:
            flash(msg, "error")
        return render_template("search.html", results=[], query=query,
                               location=location, configured=configured,
                               jooble_usage=None, ready=rd)
    if request.method == "POST" and configured:
        prof = resume_mod.load_profile()
        if not query:
            query = " OR ".join(prof.get("target_titles", [])[:3])
        for src in get_sources():
            try:
                count = 0
                for job in src.search(query, location=location, limit=20):
                    m = score_job(job.title, job.description, prof)
                    results.append({"job": job, "score": m.score})
                    count += 1
                flash(f"{src.name}: {count} result(s).", "ok")
            except Exception as exc:
                flash(f"{src.name}: {exc}", "error")
        results.sort(key=lambda x: x["score"], reverse=True)
    # Jooble free-tier usage feedback.
    ju = usage.jooble_usage(config.JOOBLE_API_KEY) if config.JOOBLE_API_KEY else None
    if ju and ju["tracked"]:
        if ju["exhausted"]:
            flash("Jooble free quota (500) is used up — get a new key at "
                  "jooble.org/api/about and update it in Settings.", "error")
        elif ju["low"]:
            flash(f"Heads-up: only {ju['remaining']} Jooble requests left of "
                  f"{ju['limit']}. Consider getting a fresh key soon.", "error")
    return render_template("search.html", results=results, query=query,
                           location=location, configured=configured,
                           jooble_usage=ju, ready=rd)


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
