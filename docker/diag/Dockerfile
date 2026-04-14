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

FROM python:3.11-alpine

# Build deps for chromadb's native extensions (hnswlib needs a C++ compiler).
RUN apk add --no-cache gcc g++ musl-dev

WORKDIR /app

# Install chromadb (semantic memory / RAG retrieval for narrator context).
# IMPORTANT: This step must come BEFORE ARG GIT_COMMIT so Docker layer cache
# survives both app.py edits AND build-arg changes. ARG invalidates all layers
# below it, so any pip install below an ARG can never be cached.
RUN pip install --no-cache-dir chromadb

# Bake the git commit hash so /signature can report exactly which build
# this container came from. Passed via --build-arg GIT_COMMIT=$(git rev-parse --short HEAD).
# Placed AFTER pip install so the expensive install layer is always cached.
ARG GIT_COMMIT=unknown
ENV GIT_COMMIT_ENV=${GIT_COMMIT}

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

# Write the baked commit hash where _build_signature() looks for it
RUN echo "${GIT_COMMIT}" > /app/GIT_COMMIT

EXPOSE 1591
CMD ["python", "-u", "/app/docker/diag/app.py"]
