@echo off
REM ============================================================
REM  Job Tracker launcher (Windows)
REM  Double-click to start. On launch it pulls the latest code
REM  from git, then (on first run) creates a virtual environment
REM  and installs/updates all dependencies. An app window opens.
REM  Press Ctrl+C in this window (or click Quit in the app) to stop.
REM ============================================================
cd /d "%~dp0"
title Job Tracker (server) - close or Ctrl+C to stop

REM --- Pull latest, then re-launch fresh so edits to this .bat take effect ---
if defined JT_PULLED goto :afterpull
set JT_PULLED=1
where git >nul 2>nul
if %errorlevel%==0 (
    echo [update] Pulling the latest version from git...
    git pull --ff-only
) else (
    echo [update] git not found - skipping update.
)
call "%~f0" %*
exit /b %errorlevel%

:afterpull
REM --- First run: create the virtual environment ---
if not exist ".venv\Scripts\python.exe" (
    echo [setup] Creating virtual environment ^(first run only^)...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo ERROR: Python was not found. Install Python 3.10+ and try again.
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
)

REM --- Always make sure dependencies are present (picks up new ones after a pull) ---
echo [setup] Checking / installing dependencies...
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt

".venv\Scripts\python.exe" -m jobtracker init
echo.
echo Starting Job Tracker... an app window will open shortly.
".venv\Scripts\python.exe" -m jobtracker web --port 5000

echo.
echo Job Tracker stopped.
pause
