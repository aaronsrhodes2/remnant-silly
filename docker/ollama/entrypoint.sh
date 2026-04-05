#!/bin/sh
# Dual-mode entrypoint — MODE=bootstrap|runtime (default: runtime).
#
# bootstrap: ephemerally runs ollama serve + python downloader with
#            progress streamed to /remnant-status/ollama.json, then
#            exits so compose treats this as a one-shot.
# runtime:   fails fast if the model isn't in the shared volume, then
#            runs ollama serve as PID 1. No network egress expected.

set -e

MODEL="${OLLAMA_MODEL:-mistral}"
MODE="${MODE:-runtime}"
DIAG_LOG="${STATUS_DIR:-/remnant-status}/diagnostics.log"

# Shared diagnostics log — surfaced via nginx /diagnostics/log.txt on
# port 1582. Best-effort; never fails the entrypoint.
diag() {
    mkdir -p "$(dirname "$DIAG_LOG")" 2>/dev/null || true
    printf '[%s] [ollama:%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$MODE" "$*" >> "$DIAG_LOG" 2>/dev/null || true
}
diag "entrypoint start (model=$MODEL)"

wait_for_api() {
    i=0
    until ollama list >/dev/null 2>&1; do
        i=$((i+1))
        if [ "$i" -gt 60 ]; then
            echo "[ollama] ERROR: ollama server never came up" >&2
            return 1
        fi
        sleep 1
    done
}

model_present() {
    ollama list | awk '{print $1}' | grep -q "^${MODEL}\(:\|$\)"
}

case "$MODE" in
    bootstrap)
        echo "[ollama:bootstrap] starting ollama serve (temporary)"
        ollama serve &
        SERVE_PID=$!
        trap 'kill $SERVE_PID 2>/dev/null || true' EXIT

        wait_for_api || exit 1
        echo "[ollama:bootstrap] API ready, running downloader for ${MODEL}"

        # OLLAMA_HOST points python script at local serve.
        OLLAMA_HOST=http://localhost:11434 \
        OLLAMA_MODEL="$MODEL" \
        STATUS_DIR="${STATUS_DIR:-/remnant-status}" \
            python3 /app/download_ollama.py

        diag "download complete — shutting down temp server"
        echo "[ollama:bootstrap] download complete, shutting down temp server"
        kill $SERVE_PID 2>/dev/null || true
        wait $SERVE_PID 2>/dev/null || true
        exit 0
        ;;

    runtime)
        # Wait for bootstrap sentinel before launching serve. On a warm
        # boot the sentinel already exists from a previous bootstrap
        # run in the persistent remnant-status volume.
        STATUS_DIR="${STATUS_DIR:-/remnant-status}"
        SENTINEL="${WAIT_FOR_SENTINEL:-$STATUS_DIR/ollama-ready}"
        TIMEOUT="${WAIT_TIMEOUT_SECONDS:-1800}"

        mkdir -p "$STATUS_DIR"

        if [ ! -f "$SENTINEL" ]; then
            echo "[ollama:runtime] waiting for bootstrap sentinel: $SENTINEL (timeout ${TIMEOUT}s)"
            waited=0
            while [ ! -f "$SENTINEL" ]; do
                if [ "$waited" -ge "$TIMEOUT" ]; then
                    echo "[ollama:runtime] FATAL: sentinel not present after ${TIMEOUT}s." >&2
                    echo "[ollama:runtime] Run: docker compose --profile bootstrap up" >&2
                    exit 1
                fi
                sleep 2
                waited=$((waited + 2))
            done
            echo "[ollama:runtime] sentinel present after ${waited}s"
        fi

        echo "[ollama:runtime] starting ollama serve (no network egress expected)"
        ollama serve &
        SERVE_PID=$!

        wait_for_api || { kill $SERVE_PID 2>/dev/null || true; exit 1; }

        if ! model_present; then
            echo "[ollama:runtime] FATAL: model ${MODEL} is not present in /root/.ollama" >&2
            echo "[ollama:runtime] Run 'docker compose --profile bootstrap up' first." >&2
            diag "FATAL: model ${MODEL} missing from /root/.ollama"
            kill $SERVE_PID 2>/dev/null || true
            exit 1
        fi
        echo "[ollama:runtime] model ${MODEL} ready"
        diag "model ${MODEL} ready — serving"

        # Write runtime status so nginx splash sees ollama as ready
        # immediately on a warm restart (where bootstrap doesn't run).
        mkdir -p "${STATUS_DIR:-/remnant-status}"
        cat > "${STATUS_DIR:-/remnant-status}/ollama.json" <<JSON
{"service":"ollama","phase":"ready","models":[{"key":"${MODEL}","name":"${MODEL} (Ollama)","license":"Apache 2.0","bytes_done":1,"bytes_total":1}],"error":null}
JSON

        wait $SERVE_PID
        ;;

    *)
        echo "[ollama] unknown MODE=$MODE (expected: bootstrap|runtime)" >&2
        exit 2
        ;;
esac
