# Flask + Stable Diffusion image-generation backend.
# CUDA runtime base for NVIDIA GPU acceleration via NVIDIA Container Toolkit.

FROM pytorch/pytorch:2.1.2-cuda12.1-cudnn8-runtime

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/root/.cache/huggingface \
    TRANSFORMERS_CACHE=/root/.cache/huggingface

WORKDIR /app

# Install Python dependencies. torch/cuda already provided by base image.
#
# Note: `docker build` runs on docker's default network, not the compose
# play-net, so routing this pip install through the nginx librarian at
# /pypi/ isn't free — it would require the nginx container to be up
# BEFORE the build, plus --add-host=host.docker.internal:host-gateway
# and --build-arg PIP_INDEX_URL=http://host.docker.internal:1580/pypi/simple.
# The single-egress invariant (nginx is the only internet-touching
# process) applies at RUNTIME — enforced by HF_ENDPOINT=http://nginx/hf
# and play-net internal:true. Build-time fetches are a dev-machine
# operation outside that boundary, same as `docker pull` of the base
# image itself.
COPY docker/flask-sd/requirements.txt .
RUN pip install -r requirements.txt

# Copy backend source (image_gallery must be importable alongside the API)
COPY backend/image_generator_api.py .
COPY backend/image_gallery.py .

# Bootstrap downloader (runs under the bootstrap compose profile with
# internet). Fills /root/.cache/huggingface via disk-polling progress
# that nginx's splash displays. Runtime flask-sd never runs this —
# it reads the already-populated HF cache and has no egress.
COPY scripts/splash/download_flask_sd.py /app/download_flask_sd.py

# Runtime entrypoint — writes a "ready" status file then execs the API.
COPY docker/flask-sd/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Create default gallery dir (mounted as a volume in compose)
RUN mkdir -p /data/gallery

EXPOSE 5000

# Default entrypoint is the runtime wrapper. The bootstrap service
# overrides both entrypoint and command in docker-compose.yml to run
# download_flask_sd.py and exit.
ENTRYPOINT ["/entrypoint.sh"]
