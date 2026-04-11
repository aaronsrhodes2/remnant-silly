#!/bin/sh
# Runtime entrypoint for the flask-music service.
#
# 1. Waits for the bootstrap sentinel so the shared hf-cache volume
#    is populated before we try to load MusicGen. On a warm boot
#    the sentinel already exists from a previous bootstrap run.
# 2. Writes a "ready" status JSON for the splash.
# 3. Execs app.py as PID 1.
#
# Runtime has no network egress. HF_HUB_OFFLINE=1 in compose env
# ensures any accidental cache-miss fails loudly.

set -e

STATUS_DIR="${STATUS_DIR:-/remnant-status}"
SENTINEL="${WAIT_FOR_SENTINEL:-$STATUS_DIR/flask-music-ready}"
TIMEOUT="${WAIT_TIMEOUT_SECONDS:-1800}"
DIAG_LOG="$STATUS_DIR/diagnostics.log"

mkdir -p "$STATUS_DIR"

diag() {
    printf '[%s] [flask-music:runtime] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$DIAG_LOG" 2>/dev/null || true
}
diag "entrypoint start (sentinel=$SENTINEL timeout=${TIMEOUT}s)"

if [ ! -f "$SENTINEL" ]; then
    echo "[flask-music:runtime] waiting for bootstrap sentinel: $SENTINEL (timeout ${TIMEOUT}s)"
    diag "waiting for bootstrap sentinel"
    waited=0
    while [ ! -f "$SENTINEL" ]; do
        if [ "$waited" -ge "$TIMEOUT" ]; then
            echo "[flask-music:runtime] FATAL: sentinel not present after ${TIMEOUT}s." >&2
            echo "[flask-music:runtime] Run: docker compose --profile bootstrap up bootstrap-flask-music" >&2
            diag "FATAL: sentinel not present after ${TIMEOUT}s"
            exit 1
        fi
        sleep 2
        waited=$((waited + 2))
    done
    echo "[flask-music:runtime] sentinel present after ${waited}s"
    diag "sentinel present after ${waited}s"
fi

cat > "$STATUS_DIR/flask-music.json" <<'JSON'
{"service":"flask-music","phase":"ready","models":[{"key":"musicgen-small","name":"MusicGen Small (facebook)","license":"CC-BY-NC 4.0","bytes_done":1,"bytes_total":1}],"error":null}
JSON

diag "status ready — exec app.py"
exec python /app/app.py
