#!/usr/bin/env python3
"""flask-tts — Native Kokoro TTS service for Remnant.

Provides an OpenAI-compatible /v1/audio/speech endpoint backed by kokoro-onnx.
Matches the docker Kokoro-FastAPI container API so the game UI needs no changes
between docker and native modes.

Models download automatically from HuggingFace on first request (~300 MB).

Port: LISTEN_PORT env var (default 1594)
Voice: KOKORO_VOICE env var (default am_michael — deep authoritative male)
"""

from __future__ import annotations

import io
import logging
import os

from flask import Flask, Response, jsonify, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="[flask-tts] %(message)s")
log = logging.getLogger(__name__)

TTS = None
DEFAULT_VOICE = os.environ.get("KOKORO_VOICE", "am_michael")


def _get_tts():
    global TTS
    if TTS is None:
        log.info("downloading Kokoro model files from HuggingFace (first run only)…")
        try:
            from huggingface_hub import hf_hub_download
            onnx  = hf_hub_download(repo_id="hexgrad/Kokoro-82M", filename="kokoro-v0_19.onnx")
            voices = hf_hub_download(repo_id="hexgrad/Kokoro-82M", filename="voices.bin")
        except Exception as exc:
            log.error("model download failed: %s", exc)
            raise
        from kokoro_onnx import Kokoro  # noqa: PLC0415
        TTS = Kokoro(onnx, voices)
        log.info("Kokoro model ready")
    return TTS


def _to_wav(samples, sample_rate: int) -> bytes:
    """Convert float32 samples to WAV bytes using stdlib only."""
    import struct, wave  # noqa: PLC0415
    buf = io.BytesIO()
    pcm = bytes(struct.pack("<" + "h" * len(samples),
                            *[max(-32768, min(32767, int(s * 32767))) for s in samples]))
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


@app.get("/health")
def health():
    return jsonify({"status": "ok", "voice": DEFAULT_VOICE})


@app.post("/v1/audio/speech")
def speak():
    """OpenAI-compatible TTS endpoint.

    Request body: {"model": "kokoro", "input": "text", "voice": "am_michael"}
    Response: audio/wav bytes
    """
    data = request.get_json(force=True, silent=True) or {}
    text  = (data.get("input") or "").strip()
    voice = data.get("voice") or DEFAULT_VOICE

    if not text:
        return jsonify({"error": "no input text"}), 400

    try:
        tts = _get_tts()
        samples, sample_rate = tts.create(text, voice=voice, speed=1.0, lang="en-us")
        wav_bytes = _to_wav(samples, sample_rate)
    except Exception as exc:
        log.exception("TTS failed: %s", exc)
        return jsonify({"error": str(exc)}), 500

    return Response(wav_bytes, mimetype="audio/wav", status=200)


if __name__ == "__main__":
    port = int(os.environ.get("LISTEN_PORT", 1594))
    log.info("flask-tts starting on :%d  (voice=%s)", port, DEFAULT_VOICE)
    app.run(host="0.0.0.0", port=port, threaded=True)
