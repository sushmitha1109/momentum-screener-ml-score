#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  launch.sh — Momentum Screener launcher
#
#  Usage:
#    ./launch.sh              # start backend + open browser
#    ./launch.sh --port 8080  # custom Flask port
#    ./launch.sh --no-browser # headless (server only)
#    ./launch.sh --api-key    # auto-generate and set an API key
#    ./launch.sh --stop       # kill any running backend
#
#  Place this file in the same folder as:
#    momentum_screener.py
#    index.html
#    requirements.txt
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── defaults ──────────────────────────────────────────────────────────────────
PORT=5000
OPEN_BROWSER=true
GEN_API_KEY=false
STOP_ONLY=false
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/.screener.pid"
LOG_FILE="$SCRIPT_DIR/.screener.log"

# ── colours ───────────────────────────────────────────────────────────────────
G="\033[1;32m"; R="\033[1;31m"; Y="\033[1;33m"; C="\033[1;36m"; D="\033[2m"; RS="\033[0m"
ok()   { echo -e "  ${G}✓${RS}  $*"; }
err()  { echo -e "  ${R}✗${RS}  $*" >&2; }
warn() { echo -e "  ${Y}⚠${RS}  $*"; }
info() { echo -e "  ${D}·${RS}  $*"; }
head() { echo -e "\n${G}$*${RS}"; }

# ── arg parsing ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)       PORT="$2"; shift 2 ;;
    --no-browser) OPEN_BROWSER=false; shift ;;
    --api-key)    GEN_API_KEY=true; shift ;;
    --stop)       STOP_ONLY=true; shift ;;
    -h|--help)
      echo ""
      echo "  Usage: ./launch.sh [options]"
      echo ""
      echo "  Options:"
      echo "    --port <n>      Flask port (default: 5000)"
      echo "    --no-browser    Don't auto-open the browser"
      echo "    --api-key       Generate and set a random API key"
      echo "    --stop          Stop a running backend"
      echo ""
      exit 0 ;;
    *) warn "Unknown arg: $1"; shift ;;
  esac
done

# ── banner ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${G}╔════════════════════════════════════════╗${RS}"
echo -e "${G}║      MOMENTUM SCREENER  launcher       ║${RS}"
echo -e "${G}╚════════════════════════════════════════╝${RS}"

# ── stop mode ─────────────────────────────────────────────────────────────────
if $STOP_ONLY; then
  if [[ -f "$PID_FILE" ]]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
      kill "$PID"
      rm -f "$PID_FILE"
      ok "Stopped backend (PID $PID)"
    else
      warn "Process $PID not running — cleaning up stale PID file"
      rm -f "$PID_FILE"
    fi
  else
    # Fallback: find by port
    if command -v lsof &>/dev/null; then
      PID=$(lsof -ti tcp:$PORT 2>/dev/null || true)
      if [[ -n "$PID" ]]; then
        kill "$PID" && ok "Stopped process on :$PORT (PID $PID)" || err "Could not kill PID $PID"
      else
        warn "Nothing running on :$PORT"
      fi
    else
      warn "No PID file found and lsof unavailable. Kill manually."
    fi
  fi
  exit 0
fi

# ── cd to project directory ───────────────────────────────────────────────────
cd "$SCRIPT_DIR"

# ── check required files ──────────────────────────────────────────────────────
head "Checking files..."
for f in momentum_screener.py index.html; do
  if [[ -f "$f" ]]; then
    ok "$f"
  else
    err "$f not found in $SCRIPT_DIR"
    exit 1
  fi
done

# ── check Python ─────────────────────────────────────────────────────────────
head "Checking Python..."
PYTHON=""
for cmd in python3 python; do
  if command -v "$cmd" &>/dev/null; then
    PYTHON="$cmd"
    PYVER=$("$cmd" --version 2>&1)
    ok "$cmd → $PYVER"
    break
  fi
done

if [[ -z "$PYTHON" ]]; then
  err "Python not found. Install from https://python.org"
  exit 1
fi

# ── install dependencies ──────────────────────────────────────────────────────
head "Checking dependencies..."

# Build requirements list (with flask/cors added automatically)
REQUIRED_PKGS=("yfinance" "pandas" "tabulate" "colorama" "flask" "flask_cors")

MISSING=()
for pkg in "${REQUIRED_PKGS[@]}"; do
  if ! $PYTHON -c "import $pkg" 2>/dev/null; then
    MISSING+=("$pkg")
  fi
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
  warn "Missing packages: ${MISSING[*]}"
  info "Installing..."
  # Map flask_cors import name back to pip package name
  PIP_PKGS=()
  for pkg in "${MISSING[@]}"; do
    case "$pkg" in
      flask_cors) PIP_PKGS+=("flask-cors") ;;
      *)          PIP_PKGS+=("$pkg") ;;
    esac
  done
  $PYTHON -m pip install --quiet "${PIP_PKGS[@]}" \
    && ok "Installed: ${PIP_PKGS[*]}" \
    || { err "pip install failed. Try:  pip install ${PIP_PKGS[*]}"; exit 1; }
else
  ok "All dependencies present"
fi

# ── kill any existing backend on this port ────────────────────────────────────
if [[ -f "$PID_FILE" ]]; then
  OLD_PID=$(cat "$PID_FILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    info "Stopping previous backend (PID $OLD_PID)..."
    kill "$OLD_PID" 2>/dev/null || true
    sleep 0.5
  fi
  rm -f "$PID_FILE"
fi

# Also free the port if something else grabbed it
if command -v lsof &>/dev/null; then
  EXISTING=$(lsof -ti tcp:$PORT 2>/dev/null || true)
  if [[ -n "$EXISTING" ]]; then
    warn "Port $PORT in use by PID $EXISTING — killing..."
    kill "$EXISTING" 2>/dev/null || true
    sleep 0.5
  fi
fi

# ── API key setup ─────────────────────────────────────────────────────────────
# On Render/cloud: set API_KEY in the dashboard Environment tab — it persists.
# On local dev: auto-generate a fresh key each session (never written to disk).
head "API key..."
if [[ -n "${API_KEY:-}" ]]; then
  ok "Using existing API_KEY from environment (Render / shell export)"
else
  SESSION_KEY=$($PYTHON -c "import secrets; print(secrets.token_urlsafe(32))")
  export API_KEY="$SESSION_KEY"
  ok "Session key generated (local dev — not stored to disk)"
fi

# ── start Flask backend ───────────────────────────────────────────────────────
head "Starting Flask backend on :$PORT ..."

$PYTHON momentum_screener.py --serve --port "$PORT" \
  > "$LOG_FILE" 2>&1 &
BACKEND_PID=$!
echo "$BACKEND_PID" > "$PID_FILE"

# Wait up to 6 seconds for Flask to become ready
READY=false
for i in $(seq 1 12); do
  sleep 0.5
  if curl -sf "http://localhost:$PORT/health" &>/dev/null; then
    READY=true; break
  fi
  # Also check process is still alive
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    err "Backend process died. Last log output:"
    tail -20 "$LOG_FILE" | sed 's/^/    /'
    exit 1
  fi
done

if $READY; then
  ok "Backend running (PID $BACKEND_PID) — http://localhost:$PORT"
else
  warn "Backend may still be starting (health check timed out)."
  warn "Check log: $LOG_FILE"
fi

# ── open frontend in browser ──────────────────────────────────────────────────
# ── open browser pointing at Flask (not file://) ─────────────────────────────
# Flask serves index.html at http://localhost:PORT/ with the API key injected.
# Opening the file directly would bypass the key injection.
if $OPEN_BROWSER; then
  head "Opening frontend via Flask..."
  FRONTEND_URL="http://localhost:$PORT"

  IS_WSL=false
  if grep -qi microsoft /proc/version 2>/dev/null; then
    IS_WSL=true
  fi

  if $IS_WSL; then
    cmd.exe /c start "" "$FRONTEND_URL" 2>/dev/null \
      && ok "Opened in Windows browser (WSL): $FRONTEND_URL" \
      || { warn "cmd.exe open failed. Open manually:"; echo ""; echo "    $FRONTEND_URL"; echo ""; }
  elif command -v open &>/dev/null; then
    open "$FRONTEND_URL" && ok "Opened in browser (macOS): $FRONTEND_URL"
  elif command -v xdg-open &>/dev/null; then
    # Suppress errors on headless servers (Render, CI) where no display exists
    xdg-open "$FRONTEND_URL" 2>/dev/null && ok "Opened in browser (Linux): $FRONTEND_URL"       || info "Headless environment — no browser to open. Visit: $FRONTEND_URL"
  else
    info "No browser opener found. Visit:"
    echo ""
    echo "    $FRONTEND_URL"
    echo ""
  fi
fi

# ── done ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "  ${G}════════════════════════════════════════${RS}"
echo -e "  ${G}Screener is live!${RS}"
echo ""
echo -e "  ${C}Frontend:${RS} http://localhost:$PORT  (Flask-served, key injected)"
echo -e "  ${C}API:     ${RS} http://localhost:$PORT/api/scan?preset=watchlist"
echo -e "  ${C}Health:  ${RS} http://localhost:$PORT/health"
echo -e "  ${C}Log:     ${RS} $LOG_FILE"
echo ""
echo -e "  ${D}Stop with:  ./launch.sh --stop${RS}"
echo -e "  ${G}════════════════════════════════════════${RS}"
echo ""

# ── tail log so Ctrl+C here also stops the background process cleanly ─────────
trap 'echo ""; info "Caught Ctrl+C — stopping backend (PID $BACKEND_PID)..."; kill $BACKEND_PID 2>/dev/null; rm -f "$PID_FILE"; echo ""; ok "Stopped."; exit 0' INT TERM

# Keep showing backend output until user presses Ctrl+C
info "Showing backend log (Ctrl+C to stop everything):"
echo ""
tail -f "$LOG_FILE"