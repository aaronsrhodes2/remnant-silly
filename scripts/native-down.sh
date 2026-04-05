#!/usr/bin/env bash
# native-down.sh — stop the dev stack started by scripts/native-up.sh.
#
# Stops: native-nginx container, diag python process, SillyTavern
# node process. Leaves flask-sd and ollama alone (they're owned by
# the developer, not this script).

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

NATIVE_STATUS_DIR="$REPO_ROOT/scripts/splash/status"
NATIVE_RUN_DIR="$NATIVE_STATUS_DIR/.native-run"
ST_PID_FILE="$NATIVE_RUN_DIR/sillytavern.pid"
DIAG_PID_FILE="$NATIVE_RUN_DIR/diag.pid"
NGINX_CONTAINER="${NGINX_CONTAINER:-remnant-native-nginx}"

log() { echo "[native-down] $*"; }

# Kill by port — works even if the PID file is stale or lost its
# child process (node's launch pattern forks, so $! doesn't always
# point at the listening process).
kill_port() {
    local port="$1" name="$2"
    # Match any local bind address (127.0.0.1, 0.0.0.0, [::], [::1]).
    # Python stdlib HTTPServer binds 0.0.0.0 by default, node ST binds
    # 127.0.0.1, so we can't hard-code one.
    local pids
    pids=$(netstat -ano -p tcp 2>/dev/null \
        | awk '
            /LISTENING/ && ($2 ~ /(127\.0\.0\.1|0\.0\.0\.0|\[::\]|\[::1\]):'"$port"'$/) {print $NF}
        ' \
        | sort -u)
    if [ -z "$pids" ]; then
        log "$name: nothing listening on :$port"
        return 0
    fi
    for pid in $pids; do
        log "$name: killing pid $pid (port $port)"
        # MINGW taskkill wrapper or native taskkill — both accept //F //PID.
        taskkill //F //PID "$pid" >/dev/null 2>&1 || kill -9 "$pid" 2>/dev/null || true
    done
}

# 1. nginx container
if docker ps -a --format '{{.Names}}' | grep -qx "$NGINX_CONTAINER"; then
    log "removing $NGINX_CONTAINER"
    docker rm -f "$NGINX_CONTAINER" >/dev/null
else
    log "$NGINX_CONTAINER not present"
fi

# 2. diag
kill_port "${DIAG_PORT:-8700}" "diag"
rm -f "$DIAG_PID_FILE"

# 3. SillyTavern
kill_port "${ST_PORT:-8000}" "SillyTavern"
rm -f "$ST_PID_FILE"

log "done. flask-sd and ollama left running (manage those yourself)."
