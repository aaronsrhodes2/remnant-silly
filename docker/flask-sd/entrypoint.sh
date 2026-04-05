#!/bin/sh
# Runtime entrypoint for the flask-sd service.
#
# 1. Waits for the bootstrap sentinel so the shared hf-cache volume
#    is populated before we try to load the pipeline. On a warm boot
#    the sentinel already exists from a previous bootstrap run, so
#    this loop exits immediately.
# 2. Writes a "ready" status JSON for the splash.
# 3. Execs image_generator_api.py as PID 1.
#
# Runtime has no network egress. HF_HUB_OFFLINE=1 in compose env
# ensures that any accidental cache-miss inside the pipeline fails
# loudly instead of silently hanging on a dead DNS lookup.

set -e

STATUS_DIR="${STATUS_DIR:-/remnant-status}"
SENTINEL="${WAIT_FOR_SENTINEL:-$STATUS_DIR/flask-sd-ready}"
TIMEOUT="${WAIT_TIMEOUT_SECONDS:-1800}"
DIAG_LOG="$STATUS_DIR/diagnostics.log"

mkdir -p "$STATUS_DIR"

# Shared diagnostics log — surfaced via nginx /diagnostics/log.txt on
# port 1582. Append milestone lines so dev can peek at service state
# without attaching to container stdout.
diag() {
    printf '[%s] [flask-sd:runtime] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$DIAG_LOG" 2>/dev/null || true
}
diag "entrypoint start (sentinel=$SENTINEL timeout=${TIMEOUT}s)"

if [ ! -f "$SENTINEL" ]; then
    echo "[flask-sd:runtime] waiting for bootstrap sentinel: $SENTINEL (timeout ${TIMEOUT}s)"
    diag "waiting for bootstrap sentinel"
    waited=0
    while [ ! -f "$SENTINEL" ]; do
        if [ "$waited" -ge "$TIMEOUT" ]; then
            echo "[flask-sd:runtime] FATAL: sentinel not present after ${TIMEOUT}s." >&2
            echo "[flask-sd:runtime] Run: docker compose --profile bootstrap up" >&2
            diag "FATAL: sentinel not present after ${TIMEOUT}s — run: docker compose --profile bootstrap up"
            exit 1
        fi
        sleep 2
        waited=$((waited + 2))
    done
    echo "[flask-sd:runtime] sentinel present after ${waited}s"
    diag "sentinel present after ${waited}s"
fi

cat > "$STATUS_DIR/flask-sd.json" <<'JSON'
{"service":"flask-sd","phase":"ready","models":[{"key":"sd15","name":"stable-diffusion-v1-5 (fp16)","license":"CreativeML Open RAIL-M","bytes_done":1,"bytes_total":1},{"key":"ip-adapter","name":"IP-Adapter Plus (SD 1.5)","license":"Apache 2.0","bytes_done":1,"bytes_total":1}],"error":null}
JSON

diag "status ready — exec image_generator_api.py"
exec python image_generator_api.py
