#!/usr/bin/env bash
# native-up.sh — bring up the Remnant dev stack on the host machine.
#
# USER WORKFLOW
# -------------
#   scripts/native-up.sh    # start everything, leave running in foreground
#   (Ctrl+C to stop)        # or scripts/native-down.sh in another shell
#
#   Then:   http://localhost:1580/
#
# ARCHITECTURE
# ------------
# The dev stack is entirely Docker-free. Every process runs natively
# on the host machine; no Docker Desktop is required for development.
#
#   Prerequisite (already running, managed by you):
#     flask-sd           127.0.0.1:5000   (python backend/image_generator_api.py)
#     ollama             127.0.0.1:11434  (ollama serve)
#
#   Started by THIS script:
#     SillyTavern        127.0.0.1:8001   (node server.js from your ST install)
#     diag sidecar       127.0.0.1:8700   (python docker/diag/app.py)
#     nginx              0.0.0.0:1580     (native nginx, installed via winget)
#                        reverse-proxies to 127.0.0.1:{5000,8001,8700,11434}
#                        serves splash + diagnostics static HTML
#
# The Docker stack (docker-compose.yml) uses the same nginx config pattern
# but on port 1582 with Docker-network DNS names as upstreams instead of
# 127.0.0.1. That is the ONLY difference between dev and Docker.
#
# nginx prerequisite: winget install nginxinc.nginx
#
# Why native nginx instead of a container? Because the entire point of
# the native dev stack is to avoid needing Docker Desktop running. Using
# Docker just for nginx negates that completely.
#
# Why bash instead of PowerShell? Every other script in this repo is bash,
# parity tests are bash-callable, and the docker orchestration uses bash.
# Staying on one shell language keeps the project approachable.

set -eu

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# ---------------------------------------------------------------
# Config — override any of these with env vars if needed.
# ---------------------------------------------------------------
ST_DIR="${ST_DIR:-/c/Users/aaron/SillyTavern}"
ST_PORT="${ST_PORT:-1581}"          # egress: 1580=nginx 1581=ST 1582=docker-nginx
DIAG_PORT="${DIAG_PORT:-1591}"      # internal 159x sequence (was 8700)
FLASK_SD_PORT="${FLASK_SD_PORT:-1592}"  # internal 159x sequence (was 5000)
OLLAMA_PORT="${OLLAMA_PORT:-1593}"  # internal 159x sequence (was 11434)
TTS_PORT="${TTS_PORT:-1594}"        # internal 159x sequence (was 8880) — optional, not auto-started
STT_PORT="${STT_PORT:-1595}"        # internal 159x sequence (was 9000) — optional, not auto-started
FLASK_MUSIC_PORT="${FLASK_MUSIC_PORT:-1596}"  # MusicGen service — optional, not auto-started
NGINX_PORT="${NGINX_PORT:-1580}"    # egress
# Language model — qwen2.5:14b has 32k context, required for the 36k-char system prompt.
# The diag's _ollama_model() will auto-prefer large-context models even without this set,
# but pinning it here avoids surprises on systems with many models installed.
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:14b}"

# Status dir — flat under repo root (gitignored: /status/).
NATIVE_STATUS_DIR="$REPO_ROOT/status"
mkdir -p "$NATIVE_STATUS_DIR"

# PID + log staging directory (gitignored: /logs/).
NATIVE_RUN_DIR="$REPO_ROOT/logs/native-run"
mkdir -p "$NATIVE_RUN_DIR"

ST_PID_FILE="$NATIVE_RUN_DIR/sillytavern.pid"
DIAG_PID_FILE="$NATIVE_RUN_DIR/diag.pid"
ST_LOG="$NATIVE_RUN_DIR/sillytavern.log"
DIAG_LOG="$NATIVE_RUN_DIR/diag.log"

# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------
log() { echo "[native-up] $*"; }
die() { echo "[native-up] ERROR: $*" >&2; exit 1; }

# Is a tcp port listening on any local address? We accept 127.0.0.1,
# 0.0.0.0, and [::] — Python's stdlib HTTPServer (diag) binds 0.0.0.0
# while node/ST defaults to 127.0.0.1, so we can't assume just one.
port_listening() {
    local port="$1"
    netstat -ano -p tcp 2>/dev/null \
        | awk '
            /LISTENING/ && ($2 ~ /(127\.0\.0\.1|0\.0\.0\.0|\[::\]|\[::1\]):'"$port"'$/) {found=1}
            END{exit !found}
        '
}

wait_for_port() {
    local port="$1" name="$2" timeout="${3:-30}"
    local elapsed=0
    while [ $elapsed -lt $timeout ]; do
        if port_listening "$port"; then
            log "$name is listening on :$port"
            return 0
        fi
        sleep 1
        elapsed=$((elapsed+1))
    done
    die "$name did not come up on :$port within ${timeout}s"
}

# ---------------------------------------------------------------
# Splash status JSON writers.
#
# The splash (scripts/splash/splash.js) polls /status/flask-sd.json
# and /status/ollama.json every second and redirects to /app/ when
# both report phase=ready. It also renders phase=error with a human-
# readable error message, so we use that for hard failures the user
# needs to act on.
#
# All writes are atomic (tmp + mv) so the splash never reads a
# half-written JSON during a refresh.
# ---------------------------------------------------------------
# JSON-escape a bash string. Only handles backslashes and double-quotes;
# our error/detail messages are single-line English so this is enough.
# Pure bash parameter expansion — avoids sed entirely so stdin bytes
# (like em-dashes) can't trip shell quoting.
json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    printf '%s' "$s"
}

# Build a JSON field value: either literal null or a quoted string.
json_field() {
    if [ -z "$1" ] || [ "$1" = "null" ]; then
        printf 'null'
    else
        printf '"%s"' "$(json_escape "$1")"
    fi
}

write_status_flask_sd() {
    local phase="$1" error_msg="${2:-null}" detail_msg="${3:-null}"
    local err_json det_json
    err_json=$(json_field "$error_msg")
    det_json=$(json_field "$detail_msg")
    local tmp="$NATIVE_STATUS_DIR/.flask-sd.$$.tmp"
    cat >"$tmp" <<JSON
{
  "service": "flask-sd",
  "phase": "$phase",
  "models": [
    {"key": "sd15", "name": "stable-diffusion-v1-5 (fp16)", "license": "CreativeML Open RAIL-M", "bytes_done": 1, "bytes_total": 1},
    {"key": "ip-adapter", "name": "IP-Adapter Plus (SD 1.5)", "license": "Apache 2.0", "bytes_done": 1, "bytes_total": 1}
  ],
  "error": $err_json,
  "detail": $det_json
}
JSON
    mv -f "$tmp" "$NATIVE_STATUS_DIR/flask-sd.json"
}

write_status_ollama() {
    local phase="$1" error_msg="${2:-null}" detail_msg="${3:-null}"
    local model="${OLLAMA_MODEL:-mistral}"
    local err_json det_json
    err_json=$(json_field "$error_msg")
    det_json=$(json_field "$detail_msg")
    local tmp="$NATIVE_STATUS_DIR/.ollama.$$.tmp"
    cat >"$tmp" <<JSON
{
  "service": "ollama",
  "phase": "$phase",
  "models": [
    {"key": "$model", "name": "$model (Ollama)", "license": "Apache 2.0", "bytes_done": 1, "bytes_total": 1}
  ],
  "error": $err_json,
  "detail": $det_json
}
JSON
    mv -f "$tmp" "$NATIVE_STATUS_DIR/ollama.json"
}

# Wake-up probes — the "greeting ritual."
#
# These are NOT mere reachability checks. The splash refuses to lift
# until each backend has actually served a real inference request of
# the exact shape the game's extension will fire at it: a text
# generation for ollama/mistral, a diffusion step for flask-sd.
# This catches "bound the port but model not loaded into VRAM yet"
# and "pipeline imported but IP-Adapter weights missing" — real
# failure modes that /api/health and /api/tags don't detect.
#
# Each probe writes neutral `detail` updates every few seconds so the
# splash narrates the ritual rather than sitting on a blank card.

# Ollama wake-up: ask mistral for a single word. First call forces
# the model into VRAM, so timeout must allow for cold load (~20-60s
# on a warm disk, longer on a cold one). We don't parse the response
# body strictly — any non-empty "response" field means inference
# completed successfully.
probe_ollama_wake() {
    local timeout="${1:-180}" elapsed=0
    local model="${OLLAMA_MODEL:-mistral}"
    local body tmpfile
    body='{"model":"'"$model"'","prompt":"Respond with one word: awake.","stream":false}'
    tmpfile="$NATIVE_RUN_DIR/ollama-wake.resp"

    # First, make sure the port is at least open. Cheap gate so we
    # don't spam generate calls at a dead socket.
    while [ $elapsed -lt $timeout ]; do
        if curl -fsS "http://127.0.0.1:$OLLAMA_PORT/api/tags" >/dev/null 2>&1; then
            break
        fi
        if [ $((elapsed % 3)) -eq 0 ]; then
            write_status_ollama "pending" "null" \
                "waiting for ollama to open :$OLLAMA_PORT — ${elapsed}s / ${timeout}s"
        fi
        sleep 1
        elapsed=$((elapsed+1))
    done
    [ $elapsed -lt $timeout ] || return 1

    # Real wake-up call. One attempt — ollama's own generate endpoint
    # blocks until inference finishes (or errors), so we don't need a
    # retry loop; we just need a generous timeout for cold model load.
    write_status_ollama "pending" "null" \
        "asking the Lexicon Engine for a first word — loading '$model' into VRAM…"
    local remaining=$((timeout - elapsed))
    [ $remaining -lt 30 ] && remaining=30
    if curl -fsS --max-time "$remaining" \
        -H "Content-Type: application/json" \
        -d "$body" \
        "http://127.0.0.1:$OLLAMA_PORT/api/generate" \
        >"$tmpfile" 2>/dev/null; then
        # Success shape: {"model":"...","response":"awake", ...}
        if grep -q '"response"[[:space:]]*:' "$tmpfile"; then
            return 0
        fi
    fi
    return 1
}

# Flask-sd wake-up: run a minimal 2-step diffusion. The very first
# call forces the SD 1.5 pipeline + IP-Adapter weights into VRAM,
# which is the real bottleneck (20-90s depending on disk and GPU).
# steps=2 keeps the inference itself under ~5s once the pipeline
# is warm, so the total wall time is dominated by load, not compute.
probe_flask_sd_wake() {
    local timeout="${1:-240}" elapsed=0
    local body tmpfile
    body='{"prompt":"a single candle flame on black","steps":2,"guidance_scale":5.0}'
    tmpfile="$NATIVE_RUN_DIR/flask-sd-wake.resp"

    # Port gate first.
    while [ $elapsed -lt $timeout ]; do
        if curl -fsS "http://127.0.0.1:$FLASK_SD_PORT/api/health" >/dev/null 2>&1; then
            break
        fi
        if [ $((elapsed % 3)) -eq 0 ]; then
            write_status_flask_sd "pending" "null" \
                "waiting for flask-sd to open :$FLASK_SD_PORT — ${elapsed}s / ${timeout}s"
        fi
        sleep 1
        elapsed=$((elapsed+1))
    done
    [ $elapsed -lt $timeout ] || return 1

    write_status_flask_sd "pending" "null" \
        "asking the Sight-Kiln to render a test spark — warming SD 1.5 + IP-Adapter…"
    local remaining=$((timeout - elapsed))
    [ $remaining -lt 60 ] && remaining=60
    if curl -fsS --max-time "$remaining" \
        -H "Content-Type: application/json" \
        -d "$body" \
        "http://127.0.0.1:$FLASK_SD_PORT/api/generate" \
        >"$tmpfile" 2>/dev/null; then
        # Success shape: {"success": true, "image": "data:image/png;base64,...", ...}
        if grep -q '"success"[[:space:]]*:[[:space:]]*true' "$tmpfile"; then
            return 0
        fi
    fi
    return 1
}

# ---------------------------------------------------------------
# 1. Sanity: flask-sd + ollama must already be running (the user
#    manages these separately; this script doesn't start them).
# ---------------------------------------------------------------
log "checking prerequisite host services..."
port_listening "$FLASK_SD_PORT" \
    || die "flask-sd not listening on 127.0.0.1:$FLASK_SD_PORT (start it manually: python backend/image_generator_api.py)"
port_listening "$OLLAMA_PORT" \
    || die "ollama not listening on 127.0.0.1:$OLLAMA_PORT (start 'ollama serve' in another terminal)"
log "  flask-sd  : OK on :$FLASK_SD_PORT"
log "  ollama    : OK on :$OLLAMA_PORT"

# ---------------------------------------------------------------
# 2. Ensure the extension junction is in place.
#
# The Remnant extension lives in $REPO_ROOT/extension/. Rather than
# copying files, we junction ST's image-generator slot directly at
# the repo directory — edits are visible on hard-refresh with zero
# extra steps, forever. PowerShell New-Item is used because Git Bash
# ln -s requires Developer Mode, while junctions need no elevation.
# ---------------------------------------------------------------
EXT_SLOT="$ST_DIR/public/scripts/extensions/image-generator"
EXT_SRC="$REPO_ROOT/extension"
# Convert MSYS/Git-Bash path to Windows path for PowerShell
EXT_SLOT_WIN="$(cygpath -w "$EXT_SLOT")"
EXT_SRC_WIN="$(cygpath -w "$EXT_SRC")"

if [ -L "$EXT_SLOT" ] || powershell -NoProfile -NonInteractive -c "[System.IO.Directory]::Exists('$EXT_SLOT_WIN') -and ((Get-Item '$EXT_SLOT_WIN').LinkType -eq 'Junction')" 2>/dev/null | grep -qi true; then
    # Verify it still points at the right target
    actual=$(powershell -NoProfile -NonInteractive -c "(Get-Item '$EXT_SLOT_WIN').Target" 2>/dev/null | tr -d '\r')
    if [ "$actual" = "$EXT_SRC_WIN" ]; then
        log "extension junction OK: $EXT_SLOT -> $EXT_SRC"
    else
        log "extension junction target mismatch ('$actual' != '$EXT_SRC_WIN') — re-creating"
        powershell -NoProfile -NonInteractive -c "Remove-Item -Force -Recurse '$EXT_SLOT_WIN'" 2>/dev/null || rm -rf "$EXT_SLOT"
        powershell -NoProfile -NonInteractive -c "New-Item -ItemType Junction -Path '$EXT_SLOT_WIN' -Target '$EXT_SRC_WIN'" >/dev/null
        log "extension junction re-created"
    fi
else
    # Not a junction — may be a real directory (fresh ST install) or absent.
    if [ -d "$EXT_SLOT" ]; then
        log "removing real directory at $EXT_SLOT (replacing with junction)"
        rm -rf "$EXT_SLOT"
    fi
    powershell -NoProfile -NonInteractive -c "New-Item -ItemType Junction -Path '$EXT_SLOT_WIN' -Target '$EXT_SRC_WIN'" >/dev/null
    log "extension junction created: $EXT_SLOT -> $EXT_SRC"
fi

# ---------------------------------------------------------------
# 2b. Start extension file watcher (dev hot-reload).
#
# Polls extension/ for JS/CSS changes every 0.6s and writes
# /status/extension-version.json. The extension polls that file
# at 1s intervals: CSS changes are hot-swapped in-place, JS
# changes trigger a full page reload. No browser interaction needed.
# ---------------------------------------------------------------
WATCH_LOG="$NATIVE_RUN_DIR/watch-extension.log"
WATCH_PID_FILE="$NATIVE_RUN_DIR/watch-extension.pid"

# Kill any stale watcher from a previous run
if [ -f "$WATCH_PID_FILE" ]; then
    old_pid=$(cat "$WATCH_PID_FILE" 2>/dev/null || true)
    if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
        kill "$old_pid" 2>/dev/null || true
    fi
    rm -f "$WATCH_PID_FILE"
fi

log "starting extension watcher (log: $WATCH_LOG)"
nohup python "$REPO_ROOT/scripts/watch-extension.py" \
    "$EXT_SRC" "$NATIVE_STATUS_DIR" \
    >"$WATCH_LOG" 2>&1 &
echo $! >"$WATCH_PID_FILE"

# ---------------------------------------------------------------
# 3. Start SillyTavern (if not already running).
# ---------------------------------------------------------------
if port_listening "$ST_PORT"; then
    log "SillyTavern already running on :$ST_PORT — leaving alone"
else
    [ -f "$ST_DIR/server.js" ] || die "ST_DIR=$ST_DIR does not contain server.js"
    log "starting SillyTavern from $ST_DIR (log: $ST_LOG)"
    # --listen isn't needed for host.docker.internal reachability on
    # Docker Desktop Windows (it forwards to loopback), but --port
    # ensures our expected port regardless of user's config.yaml.
    ( cd "$ST_DIR" && nohup node server.js --port "$ST_PORT" >"$ST_LOG" 2>&1 & echo $! >"$ST_PID_FILE" )
    wait_for_port "$ST_PORT" "SillyTavern" 60
fi

# ---------------------------------------------------------------
# 4. Start diag sidecar (stdlib-only python, no venv needed).
# ---------------------------------------------------------------
if port_listening "$DIAG_PORT"; then
    log "diag already running on :$DIAG_PORT — leaving alone"
else
    log "starting diag sidecar on :$DIAG_PORT (log: $DIAG_LOG)"
    STATUS_DIR="$NATIVE_STATUS_DIR" \
    FLASK_SD_URL="http://127.0.0.1:$FLASK_SD_PORT" \
    OLLAMA_URL="http://127.0.0.1:$OLLAMA_PORT" \
    OLLAMA_MODEL="$OLLAMA_MODEL" \
    FLASK_MUSIC_URL="http://127.0.0.1:$FLASK_MUSIC_PORT" \
    SILLYTAVERN_URL="http://127.0.0.1:$ST_PORT" \
    LISTEN_PORT="$DIAG_PORT" \
        nohup python docker/diag/app.py >"$DIAG_LOG" 2>&1 &
    echo $! >"$DIAG_PID_FILE"
    wait_for_port "$DIAG_PORT" "diag" 15
fi

# ── Flask-Music (MusicGen) ──────────────────────────────────────────────────
FLASK_MUSIC_PID_FILE="$NATIVE_RUN_DIR/flask-music.pid"
FLASK_MUSIC_LOG="$NATIVE_RUN_DIR/flask-music.log"
if port_listening "$FLASK_MUSIC_PORT"; then
    log "flask-music already running on :$FLASK_MUSIC_PORT — leaving alone"
else
    log "starting Flask-Music (MusicGen) on :$FLASK_MUSIC_PORT (log: $FLASK_MUSIC_LOG)"
    LISTEN_PORT="$FLASK_MUSIC_PORT" \
        nohup python "$(pwd)/docker/flask-music/app.py" >"$FLASK_MUSIC_LOG" 2>&1 &
    echo $! >"$FLASK_MUSIC_PID_FILE"
    # Don't wait_for_port — model lazy-loads on first request, health may be slow
    log "  Flask-Music PID $(cat "$FLASK_MUSIC_PID_FILE") started"
fi

# ---------------------------------------------------------------
# 4b. Stamp splash status JSONs in "pending" state.
#
# We write these BEFORE nginx starts so the very first browser fetch
# of /status/flask-sd.json and /status/ollama.json gets a clean,
# meaningful "preparing" state instead of stale JSON from a previous
# run or a 404. The splash renders phase=pending as "waiting" — the
# user sees a tidy preparing card, not a broken download bar.
#
# These are overwritten to "ready" (or "error") in step 5 below
# after nginx is up and we've HTTP-probed the underlying services.
# ---------------------------------------------------------------
log "stamping splash status JSONs (pending)"
write_status_flask_sd "pending"
write_status_ollama   "pending"

# ---------------------------------------------------------------
# 5. Start native nginx gateway.
# ---------------------------------------------------------------
# Verify nginx is installed.
NGINX_EXEC="${NGINX_EXEC:-$(which nginx 2>/dev/null || true)}"
if [ -z "$NGINX_EXEC" ]; then
    # Not in PATH — scan the WinGet packages directory (default install location for
    # `winget install nginxinc.nginx`).  $LOCALAPPDATA is a Windows env var that
    # Git Bash inherits; cygpath converts it to a POSIX path.
    _winget="$(cygpath "$LOCALAPPDATA/Microsoft/WinGet/Packages" 2>/dev/null || true)"
    [ -n "$_winget" ] && NGINX_EXEC="$(find "$_winget" -name "nginx.exe" 2>/dev/null | head -1 || true)"
fi
if [ -z "$NGINX_EXEC" ]; then
    die "nginx not found in PATH or WinGet packages. Install: winget install nginxinc.nginx"
fi

# Find nginx's install directory.
# winget layout: nginx.exe and conf/ are siblings inside <install_dir>/.
# We start nginx with -p <install_dir> so it finds its own temp/, logs/,
# and conf/ for core files. Our config overrides pid and error_log with
# absolute paths inside the run dir.
NGINX_INSTALL_DIR="$(dirname "$NGINX_EXEC")"
NGINX_INSTALL_WIN="$(cygpath -m "$NGINX_INSTALL_DIR" 2>/dev/null || echo "$NGINX_INSTALL_DIR")"

MIME_TYPES=""
for candidate in \
    "$NGINX_INSTALL_DIR/conf/mime.types" \
    "$NGINX_INSTALL_DIR/../conf/mime.types" \
    "$NGINX_INSTALL_DIR/mime.types" \
    "/etc/nginx/mime.types"; do
    if [ -f "$candidate" ]; then
        MIME_TYPES="$(cygpath -m "$(realpath "$candidate")" 2>/dev/null || realpath "$candidate")"
        break
    fi
done
[ -n "$MIME_TYPES" ] || die "Cannot locate nginx mime.types. Set MIME_TYPES=/path/to/mime.types and re-run."
log "nginx mime.types: $MIME_TYPES"

# Stop any nginx already holding our port (idempotent re-runs).
# On Windows, kill-by-pid is unreliable across restarts — kill ALL nginx.exe
# processes first so stale workers can't accumulate across re-runs.
NGINX_PID_FILE="$NATIVE_RUN_DIR/nginx.pid"
if command -v taskkill >/dev/null 2>&1; then
    taskkill //F //IM nginx.exe //T >/dev/null 2>&1 || true
else
    # Non-Windows fallback: kill by pid file only
    if [ -f "$NGINX_PID_FILE" ]; then
        old_pid=$(cat "$NGINX_PID_FILE" 2>/dev/null || true)
        [ -n "$old_pid" ] && kill "$old_pid" 2>/dev/null || true
    fi
fi
rm -f "$NGINX_PID_FILE"
sleep 0.5

# Generate the resolved nginx config by substituting placeholders.
NGINX_CONF="$NATIVE_RUN_DIR/nginx.conf"
SPLASH_ROOT="$(cygpath -m "$REPO_ROOT/scripts/splash")"
DIAG_HTML_DIR="$(cygpath -m "$REPO_ROOT/docker/nginx")"
GAME_HTML_DIR="$(cygpath -m "$REPO_ROOT/web")"
STATUS_DIR="$(cygpath -m "$NATIVE_STATUS_DIR")"
CACHE_DIR_PATH="$(cygpath -m "$REPO_ROOT/local-cache/nginx-cache")"
NGINX_PID_WIN="$(cygpath -m "$NGINX_PID_FILE")"
NGINX_ERROR_LOG="$(cygpath -m "$NATIVE_RUN_DIR/nginx-error.log")"
NGINX_ACCESS_LOG="$(cygpath -m "$NATIVE_RUN_DIR/nginx-access.log")"

mkdir -p "$REPO_ROOT/local-cache/nginx-cache"

sed \
    -e "s|{{NGINX_PORT}}|${NGINX_PORT}|g" \
    -e "s|{{ST_UPSTREAM}}|127.0.0.1:${ST_PORT}|g" \
    -e "s|{{FLASK_SD_UPSTREAM}}|127.0.0.1:${FLASK_SD_PORT}|g" \
    -e "s|{{OLLAMA_UPSTREAM}}|127.0.0.1:${OLLAMA_PORT}|g" \
    -e "s|{{DIAG_UPSTREAM}}|127.0.0.1:${DIAG_PORT}|g" \
    -e "s|{{TTS_UPSTREAM}}|127.0.0.1:${TTS_PORT}|g" \
    -e "s|{{STT_UPSTREAM}}|127.0.0.1:${STT_PORT}|g" \
    -e "s|{{FLASK_MUSIC_UPSTREAM}}|127.0.0.1:${FLASK_MUSIC_PORT}|g" \
    -e "s|{{SPLASH_ROOT}}|${SPLASH_ROOT}|g" \
    -e "s|{{DIAG_HTML_DIR}}|${DIAG_HTML_DIR}|g" \
    -e "s|{{GAME_HTML_DIR}}|${GAME_HTML_DIR}|g" \
    -e "s|{{STATUS_DIR}}|${STATUS_DIR}|g" \
    -e "s|{{CACHE_DIR}}|${CACHE_DIR_PATH}|g" \
    -e "s|{{MIME_TYPES}}|${MIME_TYPES}|g" \
    -e "s|{{NGINX_PID_FILE}}|${NGINX_PID_WIN}|g" \
    -e "s|{{NGINX_ERROR_LOG}}|${NGINX_ERROR_LOG}|g" \
    -e "s|{{NGINX_ACCESS_LOG}}|${NGINX_ACCESS_LOG}|g" \
    "$REPO_ROOT/scripts/native-nginx.conf" \
    > "$NGINX_CONF"

log "starting native nginx on :$NGINX_PORT (config: $NGINX_CONF)"
NGINX_CONF_WIN="$(cygpath -m "$NGINX_CONF")"
# -p sets nginx's prefix (for temp/, logs/ relative paths in nginx core).
# Our config overrides pid + error_log with absolute paths in the run dir.
"$NGINX_EXEC" -p "$NGINX_INSTALL_WIN" -c "$NGINX_CONF_WIN"

# Wait for nginx to accept connections.
log "waiting for nginx gateway on :$NGINX_PORT..."
for i in $(seq 1 20); do
    if curl -fsS "http://localhost:$NGINX_PORT/health" >/dev/null 2>&1; then
        log "nginx gateway is up"
        break
    fi
    sleep 0.5
done

# Drift guard: assert that /csrf-token is emitting no-store.
#
# History: the pre-no-store response had an ETag and no Cache-Control,
# and Chrome cached it aggressively. csrf-sync reuses tokens per
# session, so a stale cached body whose token was minted against a
# prior session cookie causes silent CSRF 403s on every subsequent
# POST (settings/save, tokenizers, worldinfo). The fix — a
# `location = /csrf-token` block with `Cache-Control: no-store` and
# ETag/Last-Modified hidden — lives in scripts/native-nginx.conf, but
# if the container is running stale config (e.g. the user restarted
# ST but not nginx), the fix isn't live. This check catches that drift
# loudly at boot rather than silently at first POST.
log "verifying /csrf-token cache headers (drift guard)..."
csrf_hdrs=$(curl -sSI "http://localhost:$NGINX_PORT/csrf-token" 2>/dev/null | tr -d '\r')
if echo "$csrf_hdrs" | grep -qi '^cache-control:.*no-store'; then
    log "  /csrf-token: no-store OK"
else
    log "  WARN: /csrf-token is NOT emitting Cache-Control: no-store"
    log "  WARN: nginx may be running stale config — expect silent CSRF 403s"
    log "  WARN: live cache-control header: $(echo "$csrf_hdrs" | grep -i '^cache-control:' || echo '<none>')"
fi
if echo "$csrf_hdrs" | grep -qi '^etag:'; then
    log "  WARN: /csrf-token still emitting ETag — browser will revalidate and serve stale tokens"
fi

# ---------------------------------------------------------------
# 6. Probe host services and stamp final status.
#
# The splash is now open and polling /status/*.json every second.
# These probes are STRONGER than port_listening: they actually hit
# the service's health endpoint, which fails if the process bound
# the port but hasn't finished loading models / mounting routes yet
# (a real failure mode for flask-sd, which takes ~20-40s after
# startup to load SD v1.5 into VRAM and become ready).
#
# On success we stamp phase=ready — the splash sees it within 1s
# and auto-redirects to /app/. No refresh needed on the user side.
#
# On failure we stamp phase=error with an actionable message — the
# splash renders the error inline so the user knows what's wrong
# without reading terminal logs.
# ---------------------------------------------------------------
log "performing greeting ritual — asking each backend for a real response..."
log "  (this runs the exact shape of call the game's extension will fire;"
log "   splash holds until every model answers for itself)"

log "  flask-sd: running a 2-step diffusion wake-up..."
if probe_flask_sd_wake 240; then
    log "  flask-sd: the Sight-Kiln answered"
    write_status_flask_sd "ready"
else
    msg="flask-sd on 127.0.0.1:$FLASK_SD_PORT did not return a generated image within 240s. The pipeline may be failing to load SD 1.5 or IP-Adapter weights — check the backend terminal."
    log "  flask-sd: ERROR — $msg"
    write_status_flask_sd "error" "$msg"
fi

log "  ollama: asking ${OLLAMA_MODEL:-mistral} for a first word..."
if probe_ollama_wake 180; then
    log "  ollama: the Lexicon Engine answered"
    write_status_ollama "ready"
else
    msg="ollama on 127.0.0.1:$OLLAMA_PORT did not complete a generate call with '${OLLAMA_MODEL:-mistral}' within 180s. Verify the model is pulled (ollama pull ${OLLAMA_MODEL:-mistral}) and that ollama has GPU/CPU capacity to load it."
    log "  ollama: ERROR — $msg"
    write_status_ollama "error" "$msg"
fi

# ---------------------------------------------------------------
# 7. Report.
# ---------------------------------------------------------------
echo ""
log "dev stack is up. endpoints:"
echo "    http://localhost:$NGINX_PORT/                       SillyTavern (reverse-proxied)"
echo "    http://localhost:$NGINX_PORT/splash.html             splash / wake-up page"
echo "    http://localhost:$NGINX_PORT/diagnostics/           diagnostics dashboard"
echo "    http://localhost:$NGINX_PORT/diagnostics/ai.json    AI diagnostic snapshot"
echo "    http://localhost:$NGINX_PORT/health                 gateway liveness"
echo ""
log "librarian locations (the single egress keyhole):"
echo "    http://localhost:$NGINX_PORT/hf/                    HuggingFace passthrough (hf_hub)"
echo "    http://localhost:$NGINX_PORT/pypi/                  pip wheels"
echo "    http://localhost:$NGINX_PORT/npm/                   npm tarballs"
echo "    http://localhost:$NGINX_PORT/pytorch/               pytorch wheels"
echo "    http://localhost:$NGINX_PORT/ollama-dl/             ollama.com static assets"
echo ""
log "to route host hf_hub / pip / npm through nginx: source scripts/native-env.sh"
log "to stop: scripts/native-down.sh"
log "logs: $NATIVE_RUN_DIR/"
