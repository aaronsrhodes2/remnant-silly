# Remnant diagnostics sidecar.
#
# HTTP service providing AI-consumable state, narrator generation, and
# semantic memory via ChromaDB + Ollama embeddings.
#
# Exposes:
#   GET  /ai.json               — rich AI-consumable state snapshot
#   GET  /signature             — composite SHA256 fingerprint of all content + logic files
#   GET  /actions               — action catalog (JSON schema per action)
#   POST /actions/<id>          — execute a narrow allowlisted remediation
#
# Attaches to play-net only (no egress), reads/writes the shared
# remnant-status volume, and reaches the other runtime services over
# docker DNS. nginx proxies host:1582/diagnostics/* to this service.
#
# ZERO internet dependencies at build time — all steps are COPY-only.
# chromadb is imported at runtime with graceful fallback (semantic memory
# degrades to disabled if the package is absent). Install it via the
# bootstrap profile or by adding a pip-cache layer to docker-compose.bootstrap.yml.

FROM python:3.11-alpine

WORKDIR /app

# Copy all content + logic files that /signature fingerprints.
# The main app.py is placed at its "correct" repo-relative path
# (/app/docker/diag/app.py) so that Path(__file__).parent resolves to
# /app/docker/diag/ — the same math that works natively in the repo.
# The seed/ directory (system prompt, first_mes, world.json) is co-located
# at /app/docker/diag/seed/ and is the authoritative source for the narrator.
# Mirrors _SIGNATURE_FILES in app.py exactly.
COPY docker/diag/app.py                                              /app/docker/diag/app.py
COPY docker/diag/seed/                                               /app/docker/diag/seed/
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
CMD ["python", "-u", "/app/docker/diag/app.py"]
