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

# --- Cursor AI provider: auto-start the local proxy when needed ---
# The "cursor" provider calls a local OpenAI-compatible proxy that wraps the
# Cursor Agent CLI (see .env.example). Start it here so AI features just work.
# Finder launches get a minimal PATH, so add the usual install locations.
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"
if grep -qE '^AI_PROVIDER=cursor[[:space:]]*$' .env 2>/dev/null; then
    CURSOR_PORT="$(grep -E '^CURSOR_BASE_URL=' .env | sed -nE 's#.*://[^:/]+:([0-9]+).*#\1#p')"
    CURSOR_PORT="${CURSOR_PORT:-8080}"
    if curl -s -m 2 "http://localhost:${CURSOR_PORT}/health" >/dev/null 2>&1; then
        echo "[cursor] Proxy already running on port ${CURSOR_PORT}."
    elif command -v cursor-agent-api >/dev/null 2>&1; then
        echo "[cursor] Starting Cursor proxy on port ${CURSOR_PORT}..."
        CURSOR_API_KEY="$(grep -E '^CURSOR_API_KEY=' .env | cut -d= -f2-)" \
            cursor-agent-api start "${CURSOR_PORT}" \
            || echo "[cursor] Proxy failed to start - AI features will error until it runs (see ~/.cursor-agent-api/server.log)."
    else
        echo "[cursor] cursor-agent-api is not installed - AI features will fail."
        echo "[cursor] Install it with: npm install -g cursor-agent-api-proxy"
    fi
fi

PORT="${JT_PORT:-5001}"
echo
echo "Starting Job Tracker... open http://127.0.0.1:${PORT} in your browser."
echo "(Port 5001 avoids macOS AirPlay Receiver, which hijacks 5000 with HTTP 403.)"
"$VENV_PY" -m jobtracker web --port "$PORT" --fullscreen

echo
echo "Job Tracker stopped."
