# Remnant diagnostics sidecar.
#
# Tiny stdlib-only HTTP service. Exposes:
#   GET  /ai.json               — rich AI-consumable state snapshot
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

WORKDIR /app
COPY docker/diag/app.py /app/app.py

EXPOSE 8700
CMD ["python", "-u", "/app/app.py"]
