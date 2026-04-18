# fortress — core game server for Remnant.
#
# Owns: narrator pipeline (Ollama orchestration), world state (entity graph,
# player profile, codex), SSE event bus (mood/sfx/image/narrator-turn), and
# the Sorting Hat intent classifier. Exposes /diagnostics/* for observability.
#
# Attaches to play-net only (no egress), reads/writes the shared
# remnant-status volume, and reaches the other runtime services over
# docker DNS. nginx proxies all game traffic to this service.
#
# ZERO internet dependencies at build time — all steps are COPY-only.
# chromadb is imported at runtime with graceful fallback (semantic memory
# degrades to disabled if the package is absent). Install it via the
# bootstrap profile or by adding a pip-cache layer to docker-compose.bootstrap.yml.

FROM python:3.11-alpine

WORKDIR /app

# Copy all content + logic files that /signature fingerprints.
# The main app.py is placed at its "correct" repo-relative path
# (/app/docker/fortress/app.py) so that Path(__file__).parent resolves to
# /app/docker/fortress/ — the same math that works natively in the repo.
# The seed/ directory (system prompt, first_mes, world.json) is co-located
# at /app/docker/fortress/seed/ and is the authoritative source for the narrator.
# Mirrors _SIGNATURE_FILES in app.py exactly.
COPY docker/fortress/app.py                                              /app/docker/fortress/app.py
COPY docker/fortress/seed/                                               /app/docker/fortress/seed/
COPY web/index.html                                                  /app/web/index.html
COPY extension/index.js                                              /app/extension/index.js

# Bake the git commit hash so /signature can report exactly which build
# this container came from. Passed via --build-arg GIT_COMMIT=$(git rev-parse --short HEAD).
# Placed AFTER the COPY steps so layer cache for source files is never
# invalidated by build-arg changes.
ARG GIT_COMMIT=unknown
ENV GIT_COMMIT_ENV=${GIT_COMMIT}
RUN echo "${GIT_COMMIT}" > /app/GIT_COMMIT

EXPOSE 1591
CMD ["python", "-u", "/app/docker/fortress/app.py"]
