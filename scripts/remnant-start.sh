#!/usr/bin/env bash
# remnant-start.sh — GOLDEN startup script for the Remnant native dev stack.
#
# Kills all Remnant services and restarts them on canonical ports.
# This is the single command you run after a reboot or after any service
# gets onto a wrong port.
#
# PORT MAP (canonical):
#   1580  nginx            (egress — open this in browser)
#   1581  SillyTavern      (managed by native-up.sh)
#   1591  diag sidecar     (managed by native-up.sh)
#   1592  flask-sd         (started here)
#   1593  ollama           (started here)
#   1596  flask-music      (managed by native-up.sh)
#
# USAGE:
#   bash scripts/remnant-start.sh
#   Then open: http://localhost:1580/

set -eu

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Port assignments ────────────────────────────────────────────────────────
FLASK_SD_PORT="${FLASK_SD_PORT:-1592}"
OLLAMA_PORT="${OLLAMA_PORT:-1593}"
FLASK_MUSIC_PORT="${FLASK_MUSIC_PORT:-1596}"
DIAG_PORT="${DIAG_PORT:-1591}"
ST_PORT="${ST_PORT:-1581}"
NGINX_PORT="${NGINX_PORT:-1580}"
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:14b}"

log()  { echo "[remnant-start] $*"; }
warn() { echo "[remnant-start] WARN: $*" >&2; }
die()  { echo "[remnant-start] ERROR: $*" >&2; exit 1; }

# ── Helpers ─────────────────────────────────────────────────────────────────
port_listening() {
    local port="$1"
    netstat -ano -p tcp 2>/dev/null \
        | awk '
            /LISTENING/ && ($2 ~ /(127\.0\.0\.1|0\.0\.0\.0|\[::\]|\[::1\]):'"$port"'$/) {found=1}
            END{exit !found}
        '
}

kill_port() {
    local port="$1" name="$2"
    local pids
    pids=$(netstat -ano -p tcp 2>/dev/null \
        | awk '
            /LISTENING/ && ($2 ~ /(127\.0\.0\.1|0\.0\.0\.0|\[::\]|\[::1\]):'"$port"'$/) {print $NF}
        ' | sort -u)
    if [ -z "$pids" ]; then
        log "  $name (:$port): not running"
        return 0
    fi
    for pid in $pids; do
        log "  $name (:$port): killing PID $pid"
        taskkill //F //PID "$pid" >/dev/null 2>&1 || kill -9 "$pid" 2>/dev/null || true
    done
    sleep 0.5
}

# ── 1. Kill everything ───────────────────────────────────────────────────────
log "=== STOPPING all Remnant services ==="
# nginx — kill all nginx.exe (Windows taskkill is most reliable)
if command -v taskkill >/dev/null 2>&1; then
    taskkill //F //IM nginx.exe //T >/dev/null 2>&1 && log "  nginx: killed" || log "  nginx: not running"
fi
kill_port "$ST_PORT"          "SillyTavern"
kill_port "$DIAG_PORT"        "diag"
kill_port "$FLASK_SD_PORT"    "flask-sd"
kill_port "$OLLAMA_PORT"      "ollama"
kill_port "$FLASK_MUSIC_PORT" "flask-music"

# Clean up PID files from previous run
NATIVE_RUN_DIR="$REPO_ROOT/scripts/splash/status/.native-run"
rm -f "$NATIVE_RUN_DIR"/*.pid 2>/dev/null || true

log "all services stopped."
sleep 1

# ── 2. Find flask-sd ─────────────────────────────────────────────────────────
# flask-sd backend lives in $REPO_ROOT/backend/image_generator_api.py
FLASK_SD_SCRIPT="${FLASK_SD_SCRIPT:-$REPO_ROOT/backend/image_generator_api.py}"

# ── 3. Start flask-sd ────────────────────────────────────────────────────────
log "=== STARTING flask-sd on :$FLASK_SD_PORT ==="
if port_listening "$FLASK_SD_PORT"; then
    log "  flask-sd already up on :$FLASK_SD_PORT — skipping"
elif [ -f "$FLASK_SD_SCRIPT" ]; then
    log "  launching $FLASK_SD_SCRIPT"
    (cd "$(dirname "$FLASK_SD_SCRIPT")/.." && \
        FLASK_PORT="$FLASK_SD_PORT" \
        nohup python backend/image_generator_api.py \
        >"$NATIVE_RUN_DIR/flask-sd.log" 2>&1 &
        echo $! >"$NATIVE_RUN_DIR/flask-sd.pid")
    log "  PID $(cat "$NATIVE_RUN_DIR/flask-sd.pid" 2>/dev/null || echo '?') — log: $NATIVE_RUN_DIR/flask-sd.log"
else
    warn "flask-sd not found at: $FLASK_SD_SCRIPT"
    warn "Set FLASK_SD_SCRIPT=/path/to/backend/image_generator_api.py and re-run."
    warn "flask-sd will not be started — images won't work."
fi

# ── 4. Start ollama ──────────────────────────────────────────────────────────
log "=== STARTING ollama on :$OLLAMA_PORT ==="
if port_listening "$OLLAMA_PORT"; then
    log "  ollama already up on :$OLLAMA_PORT — skipping"
elif command -v ollama >/dev/null 2>&1; then
    log "  launching ollama serve (OLLAMA_HOST=127.0.0.1:$OLLAMA_PORT)"
    OLLAMA_HOST="127.0.0.1:$OLLAMA_PORT" \
        nohup ollama serve \
        >"$NATIVE_RUN_DIR/ollama.log" 2>&1 &
    log "  PID $! — log: $NATIVE_RUN_DIR/ollama.log"
    # Give ollama a moment to bind the port before native-up.sh checks it
    sleep 3
else
    die "ollama not in PATH. Install from https://ollama.com and re-run."
fi

# ── 5. Pull model if needed ──────────────────────────────────────────────────
log "=== Verifying model: $OLLAMA_MODEL ==="
if OLLAMA_HOST="127.0.0.1:$OLLAMA_PORT" ollama list 2>/dev/null | grep -q "$OLLAMA_MODEL"; then
    log "  $OLLAMA_MODEL: already pulled"
else
    log "  pulling $OLLAMA_MODEL (this may take a while on first run)…"
    OLLAMA_HOST="127.0.0.1:$OLLAMA_PORT" ollama pull "$OLLAMA_MODEL" || warn "pull failed — check your internet connection"
fi

# ── 6. Hand off to native-up.sh ─────────────────────────────────────────────
log "=== HANDING OFF to native-up.sh ==="
log "  This manages: SillyTavern, diag, flask-music, nginx, and the splash wake-up."
echo ""
exec bash "$SCRIPT_DIR/native-up.sh"
