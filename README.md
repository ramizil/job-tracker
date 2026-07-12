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
- **Web dashboard (Flask)**: visual funnel, metric cards, and a drag-and-drop
  **Kanban board** to move applications through the pipeline.
- SQLite-backed application tracker (CRUD + full **status history**).
- **Rejection logging**: capture the stage and reason for every rejection.
- **Resume profile**: parses your HTML CV into weighted skill keywords.
- **Match scoring**: every job is scored 0–100% against your resume.
- **Search**: query aggregators, rank by match, import top matches.
- **Analytics**: pipeline funnel, response/interview rates, rejection breakdowns,
  per-source effectiveness, and a match-score signal (advanced vs rejected).
- **AI fit analysis** (Gemini / OpenAI / Anthropic / Groq / Cursor): for any job, get a
  recruiter-style verdict (YES/MAYBE/NO), a requirement-by-requirement match,
  risks, and concrete **resume-fix suggestions** — bilingual, rendered in
  side-by-side English/Hebrew columns.
- **AI salary research**: expected **₪/month gross** range for the role — from
  published data via live web search (Gemini) when available, otherwise
  estimated from the company's size, growth and industry.
- **AI resume tailoring**: pick the suggestions, generate a tailored CV (same
  styling), review/edit it, and export to **PDF** (browser Print = pixel-perfect;
  server-side PDF as a fallback).
- **Gmail job alerts**: point your LinkedIn job-alert emails at a Gmail label
  (default `linkedin-jobs`), connect the mailbox once (read-only OAuth), and the
  **Job Alerts** page lists every job from those emails, flags the ones you
  haven't applied to yet (fuzzy company+title matching against your
  applications), and prefills the Paste-a-job form for one-click capture.
- **CSV / Excel export** of all applications.

## Setup

### Windows (PowerShell)
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

### macOS / Linux (bash/zsh)
```bash
cd ~/GIT/job-tracker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env          # then edit .env (resume path + API keys)
python -m jobtracker init
python -m jobtracker profile --rebuild
```

> Requires **Python 3.10+**. For the standalone app window and high-fidelity
> PDF export, install **Google Chrome**, **Microsoft Edge**, **Brave** or
> **Chromium** (any one). Without a Chromium browser the app still works and
> opens in your default browser; PDF export falls back to the built-in renderer.

## Configuration — two ways
1. **From the UI (easiest):** start the app and open **⚙ Settings**. Paste your
   keys, resume path and Gemini model; they save to `.env` and apply instantly
   (no restart). The Settings page also rebuilds your match profile.
2. **By hand:** copy `.env.example` to `.env` and fill in the values.

### Adding LinkedIn
LinkedIn has **no public jobs API**. This app surfaces LinkedIn postings through
**JSearch** (Google-for-Jobs). Add a `RAPIDAPI_KEY` in Settings and LinkedIn jobs
show up in **Search**. You can also add any single LinkedIn job by hand on the
**Applications** page (paste the URL + description).

## API keys (only needed for `search` / AI)
| Key | Source | Notes |
|-----|--------|-------|
| `RAPIDAPI_KEY` | [JSearch](https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch) | **Primary**, Israel + LinkedIn, free tier |
| `JOOBLE_API_KEY` | [Jooble](https://jooble.org/api/about) | Free, Israel coverage |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | [Adzuna](https://developer.adzuna.com/) | Optional, **no Israel** (remote/UK/US/EU) |
| `GEMINI_API_KEY` | [Google AI Studio](https://aistudio.google.com/app/apikey) | Default AI provider; the only one with web grounding + voice |
| `OPENAI_API_KEY` | [OpenAI](https://platform.openai.com/api-keys) | Alternative AI provider (GPT) |
| `ANTHROPIC_API_KEY` | [Anthropic](https://console.anthropic.com/) | Alternative AI provider (Claude) |
| `GROQ_API_KEY` | [Groq](https://console.groq.com/keys) | Alternative AI provider — fast open models, free tier |
| `CURSOR_API_KEY` | [Cursor](https://cursor.com/dashboard/integrations) | Alternative AI provider — see note below |

> The AI provider is selected via `AI_PROVIDER` (gemini | openai | anthropic |
> groq | cursor); each has its own model setting. Gemini (default `gemini-2.5-flash`)
> automatically falls back to other models when overloaded/out of quota.
> With `AI_FALLBACK=1` (Settings → "Auto-switch provider on failure") a failed
> request is transparently retried with the other providers that have a key.
> **Company research web-grounding and voice transcription are Gemini-only** —
> other providers fall back gracefully.

### The Cursor provider
Cursor has **no native chat-completions API**, so this app calls it through a
local OpenAI-compatible proxy that wraps the Cursor Agent CLI:
[`cursor-agent-api-proxy`](https://github.com/tageecc/cursor-agent-api-proxy)
(`npm install -g cursor-agent-api-proxy`). `start.command` auto-starts the
proxy on the port from `CURSOR_BASE_URL` (default `http://localhost:8080/v1`)
whenever `AI_PROVIDER=cursor`. Model default is `auto` (Cursor picks). Note:
responses are slower than direct APIs (an agent is spawned per request).

## Launch (double-click)

### Windows
- **`start.bat`** — double-click to start. On first run it creates the venv and
  installs dependencies, then opens the dashboard in a **standalone, maximized
  app window** (Edge/Chrome app mode — no tabs/address bar). A console window
  stays open; press `Ctrl+C` there (or click **⏻ Quit** in the app) to stop.
- **`JobTracker.vbs`** — same, but with **no console window** (pure app feel).
  Stop it with the in-app **⏻ Quit** button.

> Tip: right-click `start.bat` → *Send to → Desktop (create shortcut)* for a
> one-click desktop launcher. You can change its icon in the shortcut's
> Properties.

### macOS / Linux
- **`start.command`** — double-click in Finder to start (macOS). On first run it
  creates the venv and installs dependencies, then opens the dashboard in a
  **standalone app window** (Chrome/Edge app mode). A Terminal window stays
  open; press `Ctrl+C` there (or click **⏻ Quit** in the app) to stop.
  - If macOS blocks it the first time, right-click → **Open**, or run
    `chmod +x start.command` once in Terminal.
  - On Linux you can run it from a shell: `./start.command`.

> Tip (macOS): drag `start.command` to the Dock, or use *Finder → File → Make
> Alias* and put the alias on your Desktop for a one-click launcher.

## Web dashboard
```powershell
python -m jobtracker web              # opens an app window at http://127.0.0.1:5001
python -m jobtracker web --port 8080
python -m jobtracker web --no-open    # don't auto-open a window
python -m jobtracker web --fullscreen # open the app window fullscreen
```
- **Dashboard** – funnel, response/interview rates, rejection breakdowns.
- **Board** – drag cards between columns to update status.
- **Applications** – add/edit, paste the job description (powers AI + matching).
- **Application detail** – run *Analyze fit with Gemini*, then *Generate tailored
  resume* from the suggested fixes, review/edit, and Print/Download as PDF.
- **Search** – query aggregators and save jobs (needs an API key).
- **Export** – CSV / Excel buttons in the top bar.

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

# AI fit analysis (also available in the web UI)
python -m jobtracker analyze 3

# Launch the web dashboard
python -m jobtracker web
```

## Status pipeline
`saved → applied → screening → interview → offer → accepted`
(plus `rejected`, `withdrawn`, `ghosted`)

## Project layout
```
jobtracker/
  config.py     paths + env (API keys, Gemini model)
  db.py         SQLite schema/connection + migrations
  models.py     statuses, rejection stages/reasons
  resume.py     HTML CV -> weighted skill profile
  matcher.py    job <-> resume match scoring
  tracker.py    application CRUD + history + rejection + AI fields
  analytics.py  funnel / rejection / source analysis
  ai.py         Gemini fit analysis + resume tailoring
  exporter.py   CSV / Excel export
  sources/      jsearch.py, jooble.py, adzuna.py
  web/          Flask app (views, templates, static)
  cli.py        Typer CLI (incl. `web`, `analyze`)
data/           local SQLite DB + profile.yaml + tailored/ (git-ignored)
.cursor/rules/  agent playbook: architecture conventions + workflow preferences
```

## Conventions (the rules of the road)
- **Settings** are added in one place: `config.EDITABLE_KEYS` (+ globals,
  `reload()`, `current_settings()`) — the Settings UI picks them up.
- **DB columns** are added via `EXTRA_COLUMNS` in `db.py` (auto-migration).
- **Bilingual AI output** uses separate English/Hebrew fields (never mixed
  strings) rendered in two columns (`.bi` grid); templates stay backward
  compatible with previously stored JSON shapes.
- Full details live in `.cursor/rules/` so AI agents follow them consistently.

## Roadmap
- Auto follow-up reminders for stale "applied" rows.
- Multi-resume variants and A/B tracking of which CV gets more responses.
