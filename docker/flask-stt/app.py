#!/usr/bin/env python3
"""flask-stt — Native Whisper STT service for Remnant.

Provides an OpenAI-compatible /v1/audio/transcriptions endpoint backed by
faster-whisper running on GPU. Identical API to the docker whisper-asr-webservice
container so the game UI needs no changes between docker and native modes.

Port: LISTEN_PORT env var (default 1595)
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from flask import Flask, jsonify, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="[flask-stt] %(message)s")
log = logging.getLogger(__name__)

MODEL = None  # lazy — loaded on first transcription request
MODEL_NAME = os.environ.get("WHISPER_MODEL", "base.en")
DEVICE     = os.environ.get("WHISPER_DEVICE", "cuda")
COMPUTE    = os.environ.get("WHISPER_COMPUTE", "float16")


def _get_model():
    global MODEL
    if MODEL is None:
        from faster_whisper import WhisperModel  # noqa: PLC0415
        log.info("loading Whisper model %s on %s/%s…", MODEL_NAME, DEVICE, COMPUTE)
        try:
            MODEL = WhisperModel(MODEL_NAME, device=DEVICE, compute_type=COMPUTE)
        except Exception:
            log.warning("GPU load failed — falling back to CPU/int8")
            MODEL = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8")
        log.info("Whisper model ready")
    return MODEL


def _swap_to_cpu() -> None:
    """Permanently replace the active model with a CPU/int8 instance.

    faster-whisper segments are lazy generators — CUDA errors surface during
    iteration (not at WhisperModel() construction time).  When that happens we
    discard the broken GPU model and reload on CPU so subsequent requests work.
    """
    global MODEL, DEVICE, COMPUTE
    MODEL   = None
    DEVICE  = "cpu"
    COMPUTE = "int8"
    log.warning("CUDA unavailable — switching to CPU/int8 for all future requests")


@app.get("/health")
def health():
    return jsonify({"status": "ok", "model": MODEL_NAME})


@app.post("/v1/audio/transcriptions")
def transcribe():
    """OpenAI-compatible transcription endpoint."""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no audio file in request (field 'file')"}), 400

    suffix = Path(f.filename or "audio.webm").suffix or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = tmp.name
        f.save(tmp_path)

    try:
        model = _get_model()
        try:
            segments, _ = model.transcribe(tmp_path, language="en")
            # Segments are a lazy generator — CUDA errors surface here, not above.
            text = " ".join(s.text for s in segments).strip()
        except RuntimeError as cuda_exc:
            log.warning("GPU inference failed (%s) — retrying on CPU/int8", cuda_exc)
            _swap_to_cpu()
            model = _get_model()
            segments, _ = model.transcribe(tmp_path, language="en")
            text = " ".join(s.text for s in segments).strip()
        log.info("transcribed %d chars", len(text))
    except Exception as exc:
        log.exception("transcription failed: %s", exc)
        return jsonify({"error": str(exc)}), 500
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return jsonify({"text": text})


if __name__ == "__main__":
    port = int(os.environ.get("LISTEN_PORT", 1595))
    log.info("flask-stt starting on :%d", port)
    app.run(host="0.0.0.0", port=port, threaded=True)
