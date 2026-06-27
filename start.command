#!/bin/bash
# ============================================================
#  Job Tracker launcher (macOS / Linux)
#  Double-click this file in Finder to start the app. On first
#  run it creates a virtual environment and installs deps.
#  A standalone app window opens automatically.
#  Press Ctrl+C in this window (or click Quit in the app) to stop.
# ============================================================
set -e

# Always run from the folder this script lives in.
cd "$(dirname "$0")"

# Pick a Python 3.10+ interpreter (the deps require it).
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
            PY="$cand"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    echo
    echo "ERROR: Python 3.10+ was not found (some dependencies require it)."
    echo "       macOS:  brew install python@3.12"
    echo "       or download from https://www.python.org/downloads/"
    read -r -p "Press Enter to close..."
    exit 1
fi
echo "[setup] Using $("$PY" --version 2>&1) at $(command -v "$PY")"

if [ ! -x ".venv/bin/python" ]; then
    echo "[setup] Creating virtual environment..."
    "$PY" -m venv .venv
    echo "[setup] Installing dependencies (first run only)..."
    ".venv/bin/python" -m pip install --upgrade pip
    ".venv/bin/python" -m pip install -r requirements.txt
fi

".venv/bin/python" -m jobtracker init
echo
echo "Starting Job Tracker... an app window will open shortly."
".venv/bin/python" -m jobtracker web --port 5000

echo
echo "Job Tracker stopped."
read -r -p "Press Enter to close..."
