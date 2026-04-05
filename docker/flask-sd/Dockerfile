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
COPY docker/flask-sd/requirements.txt .
RUN pip install -r requirements.txt

# Copy backend source (image_gallery must be importable alongside the API)
COPY backend/image_generator_api.py .
COPY backend/image_gallery.py .

# Bake Stable Diffusion v1.5 + IP-Adapter weights into the image so the
# container is fully self-contained and needs no network at runtime.
# This mirrors the exact load sequence used by image_generator_api.py so
# the runtime load hits the cache instead of HuggingFace.
RUN python -c "import torch; \
from diffusers import StableDiffusionPipeline; \
pipe = StableDiffusionPipeline.from_pretrained('runwayml/stable-diffusion-v1-5', torch_dtype=torch.float16, safety_checker=None); \
pipe.load_ip_adapter('h94/IP-Adapter', subfolder='models', weight_name='ip-adapter-plus_sd15.bin'); \
print('[bake] SD v1.5 + IP-Adapter cached at', '/root/.cache/huggingface')"

# Create default gallery dir (mounted as a volume in compose)
RUN mkdir -p /data/gallery

EXPOSE 5000

# Bind to all interfaces so sibling containers can reach us on the
# compose network. Healthcheck pings /api/health.
CMD ["python", "image_generator_api.py"]
