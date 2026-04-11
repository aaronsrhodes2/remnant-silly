#!/bin/sh
# Run the diagnostics sidecar natively against localhost services.
#
# The SAME code that ships in the docker/diag container — no fork, no
# copy. Parity between native and docker "ready" state is guaranteed
# because the oracle is one file with different env vars.
#
# Usage:
#     scripts/run-diag-native.sh
#
# Assumes:
#   - flask-sd     running on http://localhost:5000   (python backend/image_generator_api.py)
#   - ollama       running on http://localhost:11434  (ollama serve)
#   - sillytavern  running on http://localhost:8000   (node server.js, your install)
#
# Exposes:
#   GET  http://localhost:1580/ai.json      — AI-friendly state snapshot
#   GET  http://localhost:1580/actions      — action catalog
#   POST http://localhost:1580/actions/<id> — execute an allowlisted action
#
# The SillyTavern extension's Fortress Senses module will find this
# endpoint automatically via ST's /proxy/ middleware (see index.js).

set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# Native status dir — mirrors the remnant-status named volume in docker.
# Downloader prototype (scripts/splash/download_*.py) writes here too,
# so diag.py will see the same status JSON files the native splash does.
NATIVE_STATUS_DIR="$REPO_ROOT/scripts/splash/status"
mkdir -p "$NATIVE_STATUS_DIR"

export STATUS_DIR="$NATIVE_STATUS_DIR"
export FLASK_SD_URL="http://localhost:1592"
export OLLAMA_URL="http://localhost:1593"
export SILLYTAVERN_URL="http://localhost:1581"   # native ST stays on egress port 1581
export LISTEN_PORT="1591"

echo "[diag-native] status_dir     = $STATUS_DIR"
echo "[diag-native] flask-sd       = $FLASK_SD_URL"
echo "[diag-native] ollama         = $OLLAMA_URL"
echo "[diag-native] sillytavern    = $SILLYTAVERN_URL"
echo "[diag-native] listening on   = http://localhost:$LISTEN_PORT"
echo ""
echo "[diag-native] endpoints:"
echo "               http://localhost:$LISTEN_PORT/ai.json"
echo "               http://localhost:$LISTEN_PORT/actions"
echo ""

exec python docker/diag/app.py
