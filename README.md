# Job Tracker

A personal, local-first **job-application tracker** with **resume-aware job search**.

Track every job you apply to, record *why* you were rejected (stage + reason), and
analyse your funnel to see what's working. Discover new roles from job aggregators
(JSearch / Jooble) ranked against your resume — including LinkedIn postings (via
JSearch's Google-for-Jobs index), without scraping or risking your LinkedIn account.

> Your tracked data lives in a local SQLite DB under `data/` and is **git-ignored**.

## Why not the LinkedIn API directly?
LinkedIn has **no open Jobs API** for individuals, and scraping violates their ToS
and can get your account restricted. Instead this tool uses legitimate aggregator
APIs (**JSearch** via RapidAPI is primary and covers Israel; **Jooble** is a free
fallback) that already index LinkedIn/Indeed/Glassdoor listings.

## Features
- SQLite-backed application tracker (CRUD + full **status history**).
- **Rejection logging**: capture the stage and reason for every rejection.
- **Resume profile**: parses your HTML CV into weighted skill keywords.
- **Match scoring**: every job is scored 0–100% against your resume.
- **Search**: query aggregators, rank by match, import top matches in one command.
- **Analytics**: pipeline funnel, response/interview rates, rejection breakdowns,
  per-source effectiveness, and a match-score signal (advanced vs rejected).

## Setup (Windows / PowerShell)
```powershell
cd C:\GIT\RAMI
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Configure
copy .env.example .env        # then edit .env (resume path + API keys)
python -m jobtracker init
python -m jobtracker profile --rebuild
```

## API keys (only needed for `search`)
| Key | Source | Notes |
|-----|--------|-------|
| `RAPIDAPI_KEY` | [JSearch](https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch) | **Primary**, Israel coverage, free tier |
| `JOOBLE_API_KEY` | [Jooble](https://jooble.org/api/about) | Free, Israel coverage |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | [Adzuna](https://developer.adzuna.com/) | Optional, **no Israel** (remote/UK/US/EU) |

## Usage
```powershell
# Build / view your resume profile
python -m jobtracker profile

# Search (defaults to your target titles, location Israel) and rank by match
python -m jobtracker search "performance test engineer" --location Israel --limit 20
python -m jobtracker search --save 10        # import the top 10 matches as 'saved'

# Add a job you found manually (e.g. on LinkedIn)
python -m jobtracker add -c "Acme" -t "SDET" -u "https://..." --status applied

# Move through the pipeline
python -m jobtracker ls
python -m jobtracker ls --status applied
python -m jobtracker status 3 interview -m "1st round with hiring manager"
python -m jobtracker show 3

# Log a rejection with stage + reason (this powers the analysis)
python -m jobtracker reject 3 --stage technical_interview --reason missing_skill

# Notes + analytics
python -m jobtracker note 3 "Recruiter said they want more AWS depth"
python -m jobtracker stats
python -m jobtracker match 3
```

## Status pipeline
`saved → applied → screening → interview → offer → accepted`
(plus `rejected`, `withdrawn`, `ghosted`)

## Project layout
```
jobtracker/
  config.py     paths + env
  db.py         SQLite schema/connection
  models.py     statuses, rejection stages/reasons
  resume.py     HTML CV -> weighted skill profile
  matcher.py    job <-> resume match scoring
  tracker.py    application CRUD + history + rejection
  analytics.py  funnel / rejection / source analysis
  sources/      jsearch.py, jooble.py, adzuna.py
  cli.py        Typer CLI
data/           local SQLite DB + profile.yaml (git-ignored)
```

## Roadmap
- Flask web dashboard (visual funnel + Kanban board).
- CSV/Excel export of applications.
- Auto follow-up reminders for stale "applied" rows.
