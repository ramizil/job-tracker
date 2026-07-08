"""Command-line interface for the job-application tracker."""
from __future__ import annotations

from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import analytics, tracker
from . import resume as resume_mod
from .db import init_db
from .matcher import score_job, score_text
from .models import COMMON_REJECTION_REASONS, REJECTION_STAGES, STATUSES
from .sources import get_sources

app = typer.Typer(
    add_completion=False,
    help="Personal job-application tracker with resume-aware job search.",
)
console = Console()


def _status_style(status: str) -> str:
    return {
        "saved": "cyan", "applied": "blue", "screening": "yellow",
        "interview": "magenta", "offer": "green", "accepted": "bold green",
        "rejected": "red", "withdrawn": "dim", "ghosted": "dim red",
    }.get(status, "white")


def _fmt_score(score) -> str:
    if score is None:
        return "-"
    s = float(score)
    color = "green" if s >= 60 else "yellow" if s >= 35 else "red"
    return f"[{color}]{s:.0f}%[/{color}]"


# --------------------------------------------------------------------------- #
@app.command()
def init():
    """Create the SQLite database and tables."""
    init_db()
    console.print("[green]Database initialized.[/green]")


@app.command()
def profile(rebuild: bool = typer.Option(False, "--rebuild", "-r",
                                          help="Rebuild from the resume file.")):
    """Show (or rebuild) the resume-derived matching profile."""
    prof = resume_mod.build_profile() if rebuild else resume_mod.load_profile()
    console.print(f"[bold]Resume:[/bold] {prof.get('resume_path')}")
    console.print(f"[bold]Location:[/bold] {prof.get('location')}")
    table = Table(title="Detected skills (weighted)")
    table.add_column("Skill"); table.add_column("Weight", justify="right")
    table.add_column("Aliases matched")
    weights = prof.get("weights", {})
    for skill, aliases in prof.get("skills", {}).items():
        table.add_row(skill, str(weights.get(skill, 1.0)), ", ".join(aliases))
    console.print(table)
    console.print(f"[dim]Target titles:[/dim] {', '.join(prof.get('target_titles', []))}")


@app.command()
def search(
    query: Optional[str] = typer.Argument(None, help="Search text. Defaults to your target titles."),
    location: str = typer.Option("Israel", "--location", "-l"),
    limit: int = typer.Option(20, "--limit", "-n"),
    source: Optional[str] = typer.Option(None, "--source", "-s", help="jsearch | jooble | adzuna"),
    min_score: float = typer.Option(0.0, "--min-score", help="Hide jobs below this match %."),
    save: int = typer.Option(0, "--save", help="Import the top N matches as 'saved'."),
):
    """Search aggregators and rank results against your resume."""
    prof = resume_mod.load_profile()
    if not query:
        query = " OR ".join(prof.get("target_titles", [])[:3]) or "test engineer"

    sources = get_sources(source)
    if not sources:
        console.print("[yellow]No job source is configured.[/yellow]")
        console.print("Add an API key to your [bold].env[/bold] (see .env.example):")
        console.print("  - [bold]RAPIDAPI_KEY[/bold]  -> JSearch (recommended, Israel)")
        console.print("  - [bold]JOOBLE_API_KEY[/bold] -> Jooble (free, Israel)")
        raise typer.Exit(code=1)

    results = []
    for src in sources:
        try:
            console.print(f"[dim]Querying {src.name} ...[/dim]")
            results.extend(src.search(query, location=location, limit=limit))
        except Exception as exc:  # network / quota / auth
            console.print(f"[red]{src.name} failed:[/red] {exc}")

    scored = []
    for job in results:
        m = score_job(job.title, job.description, prof)
        if m.score >= min_score:
            scored.append((m.score, job, m))
    scored.sort(key=lambda t: t[0], reverse=True)

    if not scored:
        console.print("[yellow]No results (try a broader query or lower --min-score).[/yellow]")
        raise typer.Exit()

    table = Table(title=f"{len(scored)} jobs for '{query}' in {location}")
    table.add_column("#", justify="right"); table.add_column("Match", justify="right")
    table.add_column("Title"); table.add_column("Company")
    table.add_column("Location"); table.add_column("Src")
    for i, (sc, job, _m) in enumerate(scored, 1):
        table.add_row(str(i), _fmt_score(sc), job.title[:46],
                      job.company[:26], job.location[:22], job.source)
    console.print(table)

    if save > 0:
        n = 0
        for sc, job, _m in scored[:save]:
            app_id = tracker.import_job_result(job, match_score=sc, status="saved")
            if app_id > 0:
                n += 1
        console.print(f"[green]Saved {n} job(s) to the tracker (status=saved).[/green]")
    else:
        console.print("[dim]Tip: add --save N to import the top N into your tracker.[/dim]")


@app.command()
def add(
    company: str = typer.Option(..., "--company", "-c"),
    title: str = typer.Option(..., "--title", "-t"),
    location: str = typer.Option("", "--location", "-l"),
    url: str = typer.Option("", "--url", "-u"),
    status: str = typer.Option("applied", "--status", help=f"One of: {', '.join(STATUSES)}"),
    source: str = typer.Option("manual", "--source", "-s"),
    salary: str = typer.Option("", "--salary"),
    contact: str = typer.Option("", "--contact"),
    notes: str = typer.Option("", "--notes"),
):
    """Add an application manually (e.g. a LinkedIn job you found)."""
    prof = resume_mod.load_profile()
    score = score_text(f"{title}", prof).score
    app_id = tracker.add_application(
        company=company, title=title, location=location, url=url,
        status=status, source=source, salary=salary, contact=contact,
        notes=notes, match_score=score,
    )
    console.print(f"[green]Added application #{app_id}[/green] ({company} - {title})")


@app.command("ls")
def list_cmd(status: Optional[str] = typer.Option(None, "--status", "-s")):
    """List applications (optionally filtered by status)."""
    rows = tracker.list_applications(status=status)
    if not rows:
        console.print("[yellow]No applications yet.[/yellow]")
        raise typer.Exit()
    table = Table(title=f"{len(rows)} application(s)")
    for col in ("ID", "Status", "Match", "Company", "Title", "Loc", "Src", "Applied"):
        table.add_column(col)
    for r in rows:
        st = r["status"]
        table.add_row(
            str(r["id"]),
            f"[{_status_style(st)}]{st}[/{_status_style(st)}]",
            _fmt_score(r["match_score"]),
            (r["company"] or "")[:24],
            (r["title"] or "")[:36],
            (r["location"] or "")[:18],
            r["source"] or "",
            (r["date_applied"] or "")[:10],
        )
    console.print(table)


@app.command()
def show(app_id: int = typer.Argument(...)):
    """Show full detail + status history for one application."""
    r = tracker.get_application(app_id)
    if not r:
        console.print(f"[red]No application #{app_id}[/red]"); raise typer.Exit(1)
    console.print(f"[bold]#{r['id']} {r['company']} - {r['title']}[/bold]")
    for k in ("status", "match_score", "location", "source", "url", "salary",
              "contact", "resume_version", "date_found", "date_applied",
              "rejection_stage", "rejection_reason", "rejection_date"):
        if r[k] not in (None, ""):
            console.print(f"  [cyan]{k}[/cyan]: {r[k]}")
    if r["notes"]:
        console.print(f"  [cyan]notes[/cyan]:\n{r['notes']}")
    hist = tracker.get_history(app_id)
    if hist:
        t = Table(title="History")
        t.add_column("When"); t.add_column("From"); t.add_column("To"); t.add_column("Note")
        for h in hist:
            t.add_row((h["changed_at"] or "")[:19], h["old_status"] or "-",
                      h["new_status"], h["note"] or "")
        console.print(t)


@app.command()
def status(app_id: int = typer.Argument(...),
           new_status: str = typer.Argument(..., help=f"One of: {', '.join(STATUSES)}"),
           note: str = typer.Option("", "--note", "-m")):
    """Move an application to a new pipeline status."""
    if tracker.update_status(app_id, new_status, note):
        console.print(f"[green]#{app_id} -> {new_status}[/green]")
    else:
        console.print(f"[red]No application #{app_id}[/red]")


@app.command()
def reject(app_id: int = typer.Argument(...),
           stage: str = typer.Option("", "--stage", help=f"One of: {', '.join(REJECTION_STAGES)}"),
           reason: str = typer.Option("", "--reason", help=f"e.g. {', '.join(COMMON_REJECTION_REASONS[:5])} ..."),
           note: str = typer.Option("", "--note", "-m")):
    """Mark an application rejected and record stage + reason (for analysis)."""
    if not stage:
        stage = typer.prompt(f"Stage [{'/'.join(REJECTION_STAGES)}]", default="no_response")
    if not reason:
        reason = typer.prompt("Reason (free text or a common code)", default="no_feedback")
    if tracker.set_rejection(app_id, stage=stage, reason=reason, note=note):
        console.print(f"[red]#{app_id} marked rejected[/red] (stage={stage}, reason={reason})")
    else:
        console.print(f"[red]No application #{app_id}[/red]")


@app.command()
def note(app_id: int = typer.Argument(...), text: str = typer.Argument(...)):
    """Append a timestamped note to an application."""
    if tracker.add_note(app_id, text):
        console.print(f"[green]Note added to #{app_id}[/green]")
    else:
        console.print(f"[red]No application #{app_id}[/red]")


@app.command("rm")
def remove(app_id: int = typer.Argument(...),
           yes: bool = typer.Option(False, "--yes", "-y")):
    """Delete an application."""
    if not yes:
        typer.confirm(f"Delete application #{app_id}?", abort=True)
    if tracker.delete_application(app_id):
        console.print(f"[green]Deleted #{app_id}[/green]")
    else:
        console.print(f"[red]No application #{app_id}[/red]")


@app.command()
def match(app_id: int = typer.Argument(...)):
    """Show the resume match breakdown for a saved application."""
    r = tracker.get_application(app_id)
    if not r:
        console.print(f"[red]No application #{app_id}[/red]"); raise typer.Exit(1)
    m = score_job(r["title"] or "", r["description"] or "")
    console.print(f"[bold]#{app_id} match: {_fmt_score(m.score)}[/bold]")
    console.print(f"  [green]matched[/green]: {', '.join(m.matched) or '-'}")
    console.print(f"  [red]missing[/red]: {', '.join(m.missing) or '-'}")


def _hide_dev_server_warning() -> None:
    """Drop only Werkzeug's "development server" banner line from its log.

    Werkzeug prints that warning together with the "Running on …" line in one
    record, so we strip just the warning line and keep everything else
    (including the per-request access logs). This is a local, single-user tool,
    so the production warning is noise here.
    """
    import logging

    def _strip(record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "This is a development server" in msg:
            kept = [ln for ln in msg.splitlines()
                    if "This is a development server" not in ln]
            record.msg = "\n".join(kept).strip("\n")
            record.args = None
        return True

    logging.getLogger("werkzeug").addFilter(_strip)


def _resolve_web_port(port: int) -> int:
    """Avoid macOS AirPlay Receiver on port 5000 (returns HTTP 403 / AirTunes).

    When AirPlay owns 5000 and Flask is not running, the browser hits AirPlay
    instead of the dashboard. Default to 5001; still auto-bump if --port 5000.
    """
    if port != 5000:
        return port
    import urllib.error
    import urllib.request

    try:
        urllib.request.urlopen("http://127.0.0.1:5000/", timeout=0.5)
    except urllib.error.HTTPError as exc:
        if exc.headers.get("Server", "").startswith("AirTunes"):
            console.print(
                "[yellow]Port 5000 is used by macOS AirPlay Receiver "
                "(HTTP 403); using 5001 instead.[/yellow]"
            )
            console.print(
                "[dim]Or disable AirPlay: System Settings → General → "
                "AirDrop & Handoff → AirPlay Receiver → Off.[/dim]"
            )
            return 5001
    except OSError:
        pass
    return port


@app.command()
def web(host: str = typer.Option("127.0.0.1", "--host"),
        port: int = typer.Option(5001, "--port", "-p"),
        debug: bool = typer.Option(False, "--debug"),
        open_window: bool = typer.Option(True, "--open/--no-open",
                                         help="Open a standalone app window."),
        fullscreen: bool = typer.Option(False, "--fullscreen",
                                        help="Open the app window fullscreen."),
        auto_shutdown: bool = typer.Option(
            True, "--auto-shutdown/--no-auto-shutdown",
            help="Stop the server automatically when the browser is closed."),
        idle_timeout: int = typer.Option(
            90, "--idle-timeout",
            help="Seconds with no open tab before auto-shutdown.")):
    """Launch the Flask web dashboard (funnel, applications, AI, export)."""
    from .web import create_app
    _hide_dev_server_warning()
    port = _resolve_web_port(port)
    disp = "127.0.0.1" if host in ("0.0.0.0", "") else host
    url = f"http://{disp}:{port}/"
    console.print(f"[green]Dashboard:[/green] {url}")
    console.print("[dim]Press Ctrl+C in this window to stop the server.[/dim]")
    if auto_shutdown:
        console.print(f"[dim]Auto-shutdown: on (stops ~{idle_timeout}s after the "
                      "window is closed).[/dim]")
    if open_window:
        import threading
        from .launcher import open_app_window
        threading.Timer(
            1.5, open_app_window, kwargs={"url": url, "fullscreen": fullscreen}
        ).start()
    if auto_shutdown:
        from .web import watchdog
        watchdog.start(timeout=idle_timeout)
    create_app().run(host=host, port=port, debug=debug, threaded=True)


@app.command()
def analyze(app_id: int = typer.Argument(...)):
    """Run a Gemini resume-fit analysis for one application."""
    from . import ai
    r = tracker.get_application(app_id)
    if not r:
        console.print(f"[red]No application #{app_id}[/red]"); raise typer.Exit(1)
    try:
        result = ai.analyze_fit(
            title=r["title"], company=r["company"], location=r["location"] or "",
            description=r["description"] or "",
        )
    except ai.AIError as exc:
        console.print(f"[red]{exc}[/red]"); raise typer.Exit(1)
    tracker.set_ai_analysis(app_id, result)
    console.print(f"[bold]{result.get('fit_level')}[/bold] - {result.get('verdict')}")
    for s in result.get("suggestions", []):
        console.print(f"  [cyan]{s.get('target')}[/cyan]: {s.get('action')}")
    console.print("[dim]Open the web dashboard to tailor & export the resume.[/dim]")


@app.command()
def stats():
    """Pipeline funnel + rejection analysis + source effectiveness."""
    t = analytics.totals()
    console.print("[bold]Pipeline funnel[/bold]")
    ft = Table(); ft.add_column("Status"); ft.add_column("Count", justify="right")
    for st, n in analytics.funnel().items():
        ft.add_row(f"[{_status_style(st)}]{st}[/{_status_style(st)}]", str(n))
    console.print(ft)

    kt = Table(title="Key metrics")
    kt.add_column("Metric"); kt.add_column("Value", justify="right")
    kt.add_row("Total tracked", str(t["total"]))
    kt.add_row("Applied or beyond", str(t["applied_or_beyond"]))
    kt.add_row("Still active", str(t["active"]))
    kt.add_row("Reached interview+", str(t["interviews_reached"]))
    kt.add_row("Response rate", f"{t['response_rate_pct']}%")
    kt.add_row("Interview rate", f"{t['interview_rate_pct']}%")
    kt.add_row("Rejection rate", f"{t['rejection_rate_pct']}%")
    console.print(kt)

    stage = analytics.rejection_by_stage()
    if stage:
        rt = Table(title="Rejections by stage")
        rt.add_column("Stage"); rt.add_column("Count", justify="right")
        for s, n in stage:
            rt.add_row(s, str(n))
        console.print(rt)

    reason = analytics.rejection_by_reason()
    if reason:
        rr = Table(title="Rejections by reason")
        rr.add_column("Reason"); rr.add_column("Count", justify="right")
        for s, n in reason:
            rr.add_row(s, str(n))
        console.print(rr)

    srcs = analytics.source_stats()
    if srcs:
        st = Table(title="Source effectiveness")
        for c in ("Source", "Total", "Interviews", "Rejected", "Avg match"):
            st.add_column(c)
        for s in srcs:
            avg = s["avg_match"]
            st.add_row(s["source"] or "-", str(s["total"]), str(s["interviews"]),
                       str(s["rejected"]), f"{avg:.0f}%" if avg is not None else "-")
        console.print(st)

    insight = analytics.match_score_insight()
    a, rj = insight["avg_match_advanced"], insight["avg_match_rejected"]
    if a is not None or rj is not None:
        console.print(
            f"[bold]Match-score signal:[/bold] advanced avg = "
            f"{a if a is not None else '-'}%, rejected avg = "
            f"{rj if rj is not None else '-'}%"
        )


if __name__ == "__main__":
    app()
