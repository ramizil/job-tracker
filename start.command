#!/usr/bin/env bash
# ============================================================
#  Job Tracker launcher (macOS / Linux)
#  Double-click in Finder (or run ./start.command). On launch it
#  pulls the latest code from git, then (on first run) creates a
#  virtual environment and installs/updates all dependencies.
#  Press Ctrl+C in this window to stop the server.
# ============================================================
cd "$(dirname "$0")" || exit 1

# --- Pull latest, then re-exec fresh so edits to this script take effect ---
if [ -z "$JT_PULLED" ]; then
    export JT_PULLED=1
    if command -v git >/dev/null 2>&1; then
        echo "[update] Pulling the latest version from git..."
        git pull --ff-only || echo "[update] git pull skipped/failed - continuing."
    else
        echo "[update] git not found - skipping update."
    fi
    exec "$0" "$@"
fi

PY="${PYTHON:-python3}"

# --- First run: create the virtual environment ---
if [ ! -x ".venv/bin/python" ]; then
    echo "[setup] Creating virtual environment (first run only)..."
    if ! "$PY" -m venv .venv; then
        echo
        echo "ERROR: Python 3.10+ was not found. Install it (e.g. 'brew install python') and try again."
        exit 1
    fi
    ".venv/bin/python" -m pip install --upgrade pip
fi

# --- Always make sure dependencies are present (picks up new ones after a pull) ---
echo "[setup] Checking / installing dependencies..."
".venv/bin/python" -m pip install -q -r requirements.txt

".venv/bin/python" -m jobtracker init
echo
echo "Starting Job Tracker... open http://127.0.0.1:5000 in your browser."
".venv/bin/python" -m jobtracker web --port 5000

echo
echo "Job Tracker stopped."
