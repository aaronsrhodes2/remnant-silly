#!/usr/bin/env python3
"""flask-tts — Native Kokoro TTS service for Remnant.

Provides an OpenAI-compatible /v1/audio/speech endpoint backed by kokoro-onnx.
Matches the docker Kokoro-FastAPI container API so the game UI needs no changes
between docker and native modes.

Models download automatically from GitHub releases on first request (~300 MB).

Port: LISTEN_PORT env var (default 1594)
Voice: KOKORO_VOICE env var (default am_michael — deep authoritative male)
"""

from __future__ import annotations

import io
import logging
import os
import urllib.request
from pathlib import Path

from flask import Flask, Response, jsonify, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="[flask-tts] %(message)s")
log = logging.getLogger(__name__)

TTS = None
DEFAULT_VOICE = os.environ.get("KOKORO_VOICE", "am_michael")

# kokoro-onnx v0.5+ model files are on GitHub Releases (not HuggingFace)
# int8 quantised (92 MB) — fast download, CPU-friendly quality
_MODEL_URL  = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx"
_VOICES_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"

def _model_cache_dir() -> Path:
    """Return a writable directory for Kokoro model files.

    Respects HF_HOME / HF_HUB_CACHE if set (launcher pipes these to local-cache/).
    Falls back to ~/.cache/kokoro-onnx.
    """
    base = os.environ.get("HF_HOME") or os.environ.get("HF_HUB_CACHE")
    if base:
        d = Path(base) / "kokoro-onnx"
    else:
        d = Path.home() / ".cache" / "kokoro-onnx"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _download_if_missing(url: str, dest: Path) -> Path:
    """Download *url* to *dest* if it doesn't exist yet, with progress logging."""
    if dest.exists():
        return dest
    log.info("downloading %s → %s …", url, dest)
    tmp = dest.with_suffix(".tmp")
    try:
        def _reporthook(block, block_size, total):
            if total > 0 and block % 500 == 0:
                pct = min(100, block * block_size * 100 // total)
                log.info("  … %d%%", pct)

        urllib.request.urlretrieve(url, str(tmp), reporthook=_reporthook)
        tmp.rename(dest)
        log.info("saved %s (%.1f MB)", dest.name, dest.stat().st_size / 1_048_576)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return dest


def _get_tts():
    global TTS
    if TTS is None:
        cache = _model_cache_dir()
        onnx   = _download_if_missing(_MODEL_URL,  cache / "kokoro-v1.0.int8.onnx")
        voices = _download_if_missing(_VOICES_URL, cache / "voices-v1.0.bin")
        from kokoro_onnx import Kokoro  # noqa: PLC0415
        TTS = Kokoro(str(onnx), str(voices))
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
    speed = float(data.get("speed") or 1.0)
    speed = max(0.5, min(2.0, speed))  # clamp to sane range

    if not text:
        return jsonify({"error": "no input text"}), 400

    try:
        tts = _get_tts()
        samples, sample_rate = tts.create(text, voice=voice, speed=speed, lang="en-us")
        wav_bytes = _to_wav(samples, sample_rate)
    except Exception as exc:
        log.exception("TTS failed: %s", exc)
        return jsonify({"error": str(exc)}), 500

    return Response(wav_bytes, mimetype="audio/wav", status=200)


if __name__ == "__main__":
    port = int(os.environ.get("LISTEN_PORT", 1594))
    log.info("flask-tts starting on :%d  (voice=%s)", port, DEFAULT_VOICE)
    app.run(host="0.0.0.0", port=port, threaded=True)
