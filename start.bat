@echo off
REM ============================================================
REM  Job Tracker launcher (Windows)
REM  Double-click to start. On launch it pulls the latest code
REM  from git, then (first run only) creates a virtual environment
REM  and installs dependencies. After that, dependencies are only
REM  re-installed when requirements.txt actually changes, so normal
REM  launches are fast. An app window opens.
REM  Press Ctrl+C in this window (or click Quit in the app) to stop.
REM
REM  Tips:
REM    set JT_NO_PULL=1   skip the git pull for the fastest start
REM ============================================================
cd /d "%~dp0"
title Job Tracker (server) - close or Ctrl+C to stop

REM --- Pull latest, then re-launch fresh so edits to this .bat take effect ---
if defined JT_PULLED goto :afterpull
set JT_PULLED=1
if defined JT_NO_PULL goto :pulldone
where git >nul 2>nul
if %errorlevel%==0 (
    echo [update] Pulling the latest version from git...
    git pull --ff-only --quiet
) else (
    echo [update] git not found - skipping update.
)
:pulldone
call "%~f0" %*
exit /b %errorlevel%

:afterpull
set "PY=.venv\Scripts\python.exe"

REM --- First run: create the virtual environment ---
if not exist "%PY%" (
    echo [setup] Creating virtual environment ^(first run only^)...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo ERROR: Python was not found. Install Python 3.10+ and try again.
        pause
        exit /b 1
    )
    "%PY%" -m pip install --upgrade pip --disable-pip-version-check
)

REM --- Install dependencies only when requirements.txt changed since last run ---
fc /b requirements.txt ".venv\requirements.lock" >nul 2>nul
if errorlevel 1 (
    echo [setup] Installing / updating dependencies...
    "%PY%" -m pip install -q -r requirements.txt --disable-pip-version-check
    if errorlevel 1 (
        echo.
        echo ERROR: dependency install failed. Check your internet/proxy and try again.
        pause
        exit /b 1
    )
    copy /y requirements.txt ".venv\requirements.lock" >nul
) else (
    echo [setup] Dependencies already up to date.
)

echo.
echo Starting Job Tracker... an app window will open shortly.
"%PY%" -m jobtracker web --port 5001

echo.
echo Job Tracker stopped.
pause
