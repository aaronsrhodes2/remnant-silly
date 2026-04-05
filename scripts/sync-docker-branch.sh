#!/bin/sh
# Sync the deployment subtree from main to the `docker` branch.
#
# The `docker` branch is a pure deployment-artifacts view: the
# Dockerfiles, compose, nginx splash bundle, and pinned model
# manifest. Application source code stays on `main` and is COPY'd
# into the images at build time.
#
# Usage:
#     scripts/sync-docker-branch.sh
#
# What it does:
#   1. Verifies we're on `main` with a clean tree.
#   2. Captures the current main SHA for the commit message.
#   3. Creates a git worktree at ../remnant-silly-docker/ pointing at
#      `docker` (creating the branch as an orphan if it doesn't exist).
#   4. rsync's the deployment subtree into the worktree, deleting
#      anything the main side has removed.
#   5. Commits with a message referencing the source SHA.
#   6. Prints next steps. Does NOT push — the user controls that.

set -eu

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

if [ "$(git rev-parse --abbrev-ref HEAD)" != "main" ]; then
    echo "error: must run from the main branch" >&2
    exit 1
fi

if ! git diff-index --quiet HEAD --; then
    echo "error: working tree is dirty. Commit or stash first." >&2
    exit 1
fi

MAIN_SHA=$(git rev-parse --short HEAD)
WORKTREE_DIR="$REPO_ROOT/../remnant-silly-docker"

# Create the worktree. If the branch doesn't exist yet, create it as
# an orphan (no shared history with main — the two branches track
# different kinds of content).
if ! git show-ref --verify --quiet refs/heads/docker; then
    echo "[sync] creating orphan 'docker' branch"
    git worktree add --detach "$WORKTREE_DIR"
    (
        cd "$WORKTREE_DIR"
        git checkout --orphan docker
        git rm -rf --quiet . 2>/dev/null || true
    )
else
    git worktree add "$WORKTREE_DIR" docker
fi

# Subtree paths we sync. Everything else is deliberately excluded —
# the docker branch is NOT a mirror of main.
SYNC_PATHS="
docker/
scripts/splash/
docker-compose.yml
"

echo "[sync] copying deployment subtree from main@${MAIN_SHA} to $WORKTREE_DIR"
for path in $SYNC_PATHS; do
    if [ -e "$REPO_ROOT/$path" ]; then
        # --delete removes files from target that main has removed.
        # Trailing slash on source means "contents of", which matters
        # for directory paths. For single files we'd need no slash.
        if [ -d "$REPO_ROOT/$path" ]; then
            mkdir -p "$WORKTREE_DIR/$path"
            rsync -a --delete "$REPO_ROOT/$path" "$WORKTREE_DIR/$path"
        else
            cp "$REPO_ROOT/$path" "$WORKTREE_DIR/$path"
        fi
    fi
done

# The nested docker/.gitignore travels with the subtree automatically.

cd "$WORKTREE_DIR"

if git diff-index --quiet HEAD -- 2>/dev/null && [ "$(git status --porcelain | wc -l)" = "0" ]; then
    echo "[sync] no changes — docker branch already matches main@${MAIN_SHA}"
    cd "$REPO_ROOT"
    git worktree remove "$WORKTREE_DIR"
    exit 0
fi

git add -A
git commit -m "Sync deployment subtree from main@${MAIN_SHA}"

echo ""
echo "[sync] committed on docker branch. Worktree: $WORKTREE_DIR"
echo "[sync] to push:    git -C $WORKTREE_DIR push origin docker"
echo "[sync] to clean:   git worktree remove $WORKTREE_DIR"
