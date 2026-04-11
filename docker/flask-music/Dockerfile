# Flask-Music — MusicGen ambient audio service.
#
# CPU-only torch (no CUDA) to avoid VRAM conflict with Ollama at runtime.
# MusicGen-small generates ~30s clips in ~25-60s on CPU, fine for ambient bg.
#
# Bootstrap: run `docker compose --profile bootstrap up bootstrap-flask-music`
#   Downloads facebook/musicgen-small into the shared hf-cache volume and
#   writes /remnant-status/flask-music-ready sentinel.
#
# Runtime: waits for sentinel, then serves /health and /api/generate.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/root/.cache/huggingface \
    TRANSFORMERS_CACHE=/root/.cache/huggingface

WORKDIR /app

COPY docker/flask-music/requirements.txt .
RUN pip install -r requirements.txt

COPY docker/flask-music/app.py .
COPY scripts/splash/download_flask_music.py /app/download_flask_music.py
COPY docker/flask-music/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 1596

ENTRYPOINT ["/entrypoint.sh"]
