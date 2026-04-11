# Remnant diagnostics sidecar.
#
# Tiny stdlib-only HTTP service. Exposes:
#   GET  /ai.json               — rich AI-consumable state snapshot
#   GET  /signature             — composite SHA256 fingerprint of all content + logic files
#   GET  /actions               — action catalog (JSON schema per action)
#   POST /actions/<id>          — execute a narrow allowlisted remediation
#
# Attaches to play-net only (no egress), reads/writes the shared
# remnant-status volume, and reaches the other runtime services over
# docker DNS. nginx proxies host:1582/diagnostics/ai.json and
# host:1582/diagnostics/actions/* to this service.
#
# No pip install: we use only Python stdlib to keep the image tiny
# and keep build time fast. If the feature set grows we can revisit.

FROM python:3.11-alpine

# Bake the git commit hash so /signature can report exactly which build
# this container came from. Passed via --build-arg GIT_COMMIT=$(git rev-parse --short HEAD).
ARG GIT_COMMIT=unknown
ENV GIT_COMMIT_ENV=${GIT_COMMIT}

WORKDIR /app

# Copy the diag service
COPY docker/diag/app.py /app/app.py

# Copy all content + logic files that /signature fingerprints.
# Mirrors _SIGNATURE_FILES in app.py exactly.
COPY docker/diag/app.py                                              /app/docker/diag/app.py
COPY web/index.html                                                  /app/web/index.html
COPY extension/index.js                                              /app/extension/index.js
COPY docker/sillytavern/content/characters/fortress_system_prompt.txt /app/docker/sillytavern/content/characters/fortress_system_prompt.txt
COPY docker/sillytavern/content/characters/fortress_first_mes.txt    /app/docker/sillytavern/content/characters/fortress_first_mes.txt
COPY "docker/sillytavern/content/worlds/Nullspace Nexus.json"        "/app/docker/sillytavern/content/worlds/Nullspace Nexus.json"
COPY "docker/sillytavern/content/chats/The Remnant/Opening.jsonl"    "/app/docker/sillytavern/content/chats/The Remnant/Opening.jsonl"
COPY docker/sillytavern/content/settings.json                        /app/docker/sillytavern/content/settings.json
COPY docker/sillytavern/config.yaml                                  /app/docker/sillytavern/config.yaml

# Write the baked commit hash where _build_signature() looks for it
RUN echo "${GIT_COMMIT}" > /app/GIT_COMMIT

EXPOSE 1591
CMD ["python", "-u", "/app/app.py"]
