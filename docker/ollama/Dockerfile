# Ollama image — dual-mode.
#
# MODE=bootstrap (one-shot, with internet):
#   starts ollama serve briefly, runs the python downloader which
#   streams /api/pull progress into /remnant-status/ollama.json, then
#   exits. This runs under the `bootstrap` compose profile on
#   bootstrap-net (egress allowed).
#
# MODE=runtime (default, no internet):
#   verifies OLLAMA_MODEL is already present in the shared ollama-data
#   volume (populated by the bootstrap step), then serves as PID 1 on
#   play-net (egress blocked). Refuses to start if the model is
#   missing — the splash waits on the bootstrap step.

FROM ollama/ollama:latest

# python3 is needed for the bootstrap downloader. Keep the install
# minimal — ollama base is already ~10 GB, an extra ~30 MB is fine.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY docker/ollama/entrypoint.sh /entrypoint.sh
COPY scripts/splash/download_ollama.py /app/download_ollama.py
RUN chmod +x /entrypoint.sh

ENV OLLAMA_HOST=0.0.0.0
ENV OLLAMA_MODEL=mistral
ENV MODE=runtime
ENV STATUS_DIR=/remnant-status

EXPOSE 11434

ENTRYPOINT ["/entrypoint.sh"]
