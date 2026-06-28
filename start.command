#!/usr/bin/env bash
# ============================================================
#  Job Tracker launcher (macOS / Linux)
#  Double-click in Finder (or run ./start.command). On launch it
#  pulls the latest code from git, then (first run only) creates a
#  virtual environment and installs dependencies. After that,
#  dependencies are only re-installed when requirements.txt actually
#  changes, so normal launches are fast.
#  Press Ctrl+C in this window to stop the server.
#
#  Tips:
#    JT_NO_PULL=1 ./start.command   skip the git pull for the fastest start
# ============================================================
cd "$(dirname "$0")" || exit 1

# --- Pull latest, then re-exec fresh so edits to this script take effect ---
if [ -z "$JT_PULLED" ]; then
    export JT_PULLED=1
    if [ -z "$JT_NO_PULL" ] && command -v git >/dev/null 2>&1; then
        echo "[update] Pulling the latest version from git..."
        git pull --ff-only --quiet || echo "[update] git pull skipped/failed - continuing."
    fi
    exec "$0" "$@"
fi

PY="${PYTHON:-python3}"
VENV_PY=".venv/bin/python"

# --- First run: create the virtual environment ---
if [ ! -x "$VENV_PY" ]; then
    echo "[setup] Creating virtual environment (first run only)..."
    if ! "$PY" -m venv .venv; then
        echo
        echo "ERROR: Python 3.10+ was not found. Install it (e.g. 'brew install python') and try again."
        exit 1
    fi
    "$VENV_PY" -m pip install --upgrade pip --disable-pip-version-check
fi

# --- Install dependencies only when requirements.txt changed since last run ---
if ! cmp -s requirements.txt .venv/requirements.lock; then
    echo "[setup] Installing / updating dependencies..."
    if "$VENV_PY" -m pip install -q -r requirements.txt --disable-pip-version-check; then
        cp requirements.txt .venv/requirements.lock
    else
        echo
        echo "ERROR: dependency install failed. Check your internet/proxy and try again."
        exit 1
    fi
else
    echo "[setup] Dependencies already up to date."
fi

echo
echo "Starting Job Tracker... open http://127.0.0.1:5000 in your browser."
"$VENV_PY" -m jobtracker web --port 5000

echo
echo "Job Tracker stopped."
