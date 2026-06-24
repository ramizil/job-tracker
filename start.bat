@echo off
REM ============================================================
REM  Job Tracker launcher
REM  Double-click this file to start the app. On first run it
REM  creates a virtual environment and installs dependencies.
REM  A standalone app window opens automatically.
REM  Press Ctrl+C in this window (or click Quit in the app) to stop.
REM ============================================================
cd /d "%~dp0"
title Job Tracker (server) - close or Ctrl+C to stop

if not exist ".venv\Scripts\python.exe" (
    echo [setup] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo ERROR: Python was not found. Install Python 3.10+ and try again.
        pause
        exit /b 1
    )
    echo [setup] Installing dependencies ^(first run only^)...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

".venv\Scripts\python.exe" -m jobtracker init
echo.
echo Starting Job Tracker... an app window will open shortly.
".venv\Scripts\python.exe" -m jobtracker web --port 5000

echo.
echo Job Tracker stopped.
pause
