#!/usr/bin/env bash
# dev-up.sh — bring up the docker stack in dev mode with the local
# cache bind mounts from dev-cache/.
#
# First invocation populates dev-cache/ from the real internet
# (~10 GB, 10–20 min). Every subsequent invocation finds files
# already on disk and the bootstrap finishes in seconds.
#
# Any args passed to this script are forwarded to `docker compose`,
# so you can do things like:
#     scripts/dev-up.sh --build           # force rebuild images
#     scripts/dev-up.sh -d                # detached
#     scripts/dev-up.sh --no-deps flask-sd
#
# To bring the stack down (and wipe volumes so the next run is a
# clean test against the populated cache):
#     docker compose -f docker-compose.yml -f docker-compose.dev.yml down -v

set -euo pipefail

# cd to repo root regardless of where the user invoked us from, so
# ${PWD} inside docker-compose.dev.yml resolves to the repo root and
# the bind mounts land in the right place.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# Sanity check: the bind-mount targets must exist before compose
# tries to mount them, or docker will create them as root-owned dirs
# with unpredictable permissions.
mkdir -p dev-cache/hf-cache dev-cache/nginx-cache dev-cache/ollama-data

echo "[dev-up] repo root: $REPO_ROOT"
echo "[dev-up] dev-cache: $REPO_ROOT/dev-cache"
echo "[dev-up] invoking docker compose with bootstrap profile..."

exec docker compose \
    -f docker-compose.yml \
    -f docker-compose.dev.yml \
    --profile bootstrap \
    up \
    "$@"
