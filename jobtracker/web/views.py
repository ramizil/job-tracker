"""All web routes for the job-tracker dashboard."""
from __future__ import annotations

import html as html_lib
import re

from flask import (
    Blueprint, Response, abort, flash, redirect, render_template,
    request, send_file, url_for,
)

from .. import ai, analytics, backup, config, exporter, tracker, usage
from .. import resume as resume_mod
from ..config import TAILORED_DIR
from ..matcher import score_job
from ..models import COMMON_REJECTION_REASONS, REJECTION_STAGES, STATUSES
from ..sources import get_sources

bp = Blueprint("main", __name__)

# Columns shown on the Kanban board (drop the terminal/parked ones into a lane).
BOARD_LANES = ["saved", "applied", "screening", "interview", "offer", "accepted"]


def _tailored_path(app_id: int):
    return TAILORED_DIR / f"{app_id}.html"


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
        env_path=str(config.ENV_PATH),
        backup_dir=str(config.BACKUP_DIR),
        data_dir=str(config.DATA_DIR),
        jooble_usage=usage.jooble_usage(config.JOOBLE_API_KEY) if config.JOOBLE_API_KEY else None,
    )


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
        port=(request.host.split(":", 1) + ["5000"])[1] if ":" in request.host else "5000",
        python=sys.executable,
        ai_on=ai.is_configured(),
        sources=[s.name for s in get_sources()],
    )


@bp.route("/")
def dashboard():
    funnel = analytics.funnel()
    totals = analytics.totals()
    recent = tracker.list_applications()[:10]
    return render_template(
        "dashboard.html", funnel=funnel, totals=totals, recent=recent,
        rej_stage=analytics.rejection_by_stage(),
        rej_reason=analytics.rejection_by_reason(),
        sources=analytics.source_stats(),
        insight=analytics.match_score_insight(),
        ai_on=ai.is_configured(),
    )


@bp.route("/board")
def board():
    lanes = {st: [] for st in BOARD_LANES}
    parked = []
    for r in tracker.list_applications():
        st = r["status"]
        if st in lanes:
            lanes[st].append(r)
        else:
            parked.append(r)
    return render_template("board.html", lanes=lanes, parked=parked,
                           lane_order=BOARD_LANES)


@bp.route("/applications")
def applications():
    status = request.args.get("status") or None
    rows = tracker.list_applications(status=status)
    return render_template("applications.html", rows=rows, statuses=STATUSES,
                           active=status)


@bp.route("/application/<int:app_id>")
def detail(app_id: int):
    r = tracker.get_application(app_id)
    if not r:
        abort(404)
    analysis = tracker.get_ai_analysis(app_id)
    return render_template(
        "detail.html", app=r, history=tracker.get_history(app_id),
        analysis=analysis, statuses=STATUSES, stages=REJECTION_STAGES,
        reasons=COMMON_REJECTION_REASONS, ai_on=ai.is_configured(),
        has_tailored=_tailored_path(app_id).exists(),
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
    if request.method == "GET":
        return render_template("paste.html", statuses=STATUSES,
                               ai_on=ai.is_configured())

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
    flash(f"Captured job as #{app_id}. Review and adjust the details below.", "ok")
    return redirect(url_for("main.detail", app_id=app_id))


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
    html = _tailored_path(app_id).read_text(encoding="utf-8")
    try:
        from xhtml2pdf import pisa
    except ImportError:
        flash("xhtml2pdf not installed - use the Print button (Save as PDF).", "error")
        return redirect(url_for("main.resume_review", app_id=app_id))
    import io
    out = io.BytesIO()
    try:
        result = pisa.CreatePDF(src=html, dest=out, encoding="utf-8")
        failed = bool(result.err)
    except Exception:
        failed = True
    if failed or out.getbuffer().nbytes == 0:
        flash("Server-side PDF couldn't render this resume's CSS. Use the "
              "Print / Save as PDF button for a pixel-perfect PDF.", "error")
        return redirect(url_for("main.resume_review", app_id=app_id))
    out.seek(0)
    r = tracker.get_application(app_id)
    name = re.sub(r"[^A-Za-z0-9]+", "_", f"resume_{r['company']}_{r['title']}").strip("_")
    return send_file(out, mimetype="application/pdf", as_attachment=True,
                     download_name=f"{name}.pdf")


# --------------------------------------------------------------------------- #
@bp.route("/search", methods=["GET", "POST"])
def search():
    results = []
    query = request.values.get("query", "")
    location = request.values.get("location", "Israel")
    configured = [s.name for s in get_sources()]
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
                           jooble_usage=ju)


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
