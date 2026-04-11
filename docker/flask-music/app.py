"""flask-music — MusicGen wrapper for Remnant ambient music + SFX generation.

Generates ambient music clips and short sound effects from text prompts using
Meta's MusicGen (via HuggingFace transformers). Returns base64-encoded WAV.

Environment:
  LISTEN_PORT     default 1596
  MUSICGEN_MODEL  default facebook/musicgen-small  (small/medium/large)
  MAX_DURATION    default 30 (seconds, capped at 60)

Endpoints:
  GET  /health          — service health check
  POST /api/generate    — generate ambient music (looping, 8-30s)
    Request:  {"prompt": "...", "duration": 8}
    Response: {"audio": "<base64 WAV>", "sample_rate": 32000, ...}
  POST /api/sfx         — generate a short sound effect (3-6s, fire-and-forget)
  POST /sfx             — same (Docker nginx strips /api/music/ prefix)
"""

from __future__ import annotations

import base64
import io
import os
import time
import traceback
import threading

from flask import Flask, jsonify, request

app = Flask(__name__)

LISTEN_PORT    = int(os.environ.get("LISTEN_PORT", "1596"))
MUSICGEN_MODEL = os.environ.get("MUSICGEN_MODEL", "facebook/musicgen-small")
MAX_DURATION   = int(os.environ.get("MAX_DURATION", "30"))

# MusicGen EnCodec frame rate: 50 tokens/second
_FRAME_RATE = 50

# Lazy-loaded model and processor — first request triggers load (~10-30s on GPU)
_model     = None
_processor = None
_device    = None
_sample_rate = 32000
_model_lock  = threading.Lock()


def _get_model():
    global _model, _processor, _device, _sample_rate
    if _model is not None:
        return _model, _processor
    with _model_lock:
        if _model is not None:
            return _model, _processor
        import torch
        from transformers import MusicgenForConditionalGeneration, AutoProcessor

        # Prefer CUDA if available. The diag unloads Ollama (keep_alive=0)
        # after each narrator turn, freeing VRAM for music generation.
        # On CPU, musicgen-small takes ~30-40s for an 8s clip. On GPU it's
        # ~1-5s — well within the 30-second response target.
        import torch
        _device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"[flask-music] Loading {MUSICGEN_MODEL} on {_device}…")

        _processor = AutoProcessor.from_pretrained(MUSICGEN_MODEL)
        _model = MusicgenForConditionalGeneration.from_pretrained(MUSICGEN_MODEL)
        _model = _model.to(_device)
        _model.eval()

        _sample_rate = _model.config.audio_encoder.sampling_rate
        print(f"[flask-music] Model loaded — sample_rate={_sample_rate} device={_device}")
        return _model, _processor


@app.route("/health")
def health():
    return jsonify({"status": "ok", "model": MUSICGEN_MODEL,
                    "loaded": _model is not None})


@app.route("/api/generate", methods=["POST"])
def generate():
    data     = request.get_json(force=True, silent=True) or {}
    prompt   = str(data.get("prompt", "calm ambient sci-fi")).strip()[:500]
    duration = min(int(data.get("duration", MAX_DURATION)), 60)

    if not prompt:
        return jsonify({"error": "prompt required"}), 400

    try:
        import torch
        import soundfile as sf

        model, processor = _get_model()

        inputs = processor(
            text=[prompt],
            padding=True,
            return_tensors="pt",
        ).to(_device)

        max_new_tokens = duration * _FRAME_RATE

        t0 = time.monotonic()
        with torch.no_grad():
            audio_values = model.generate(**inputs, max_new_tokens=max_new_tokens)
        elapsed = time.monotonic() - t0
        print(f"[flask-music] Generated {duration}s in {elapsed:.1f}s — {prompt!r}")

        # audio_values: [batch=1, channels=1, samples]
        audio_np = audio_values[0, 0].cpu().float().numpy()

        buf = io.BytesIO()
        sf.write(buf, audio_np, _sample_rate, format="WAV", subtype="PCM_16")
        buf.seek(0)
        audio_b64 = base64.b64encode(buf.read()).decode("ascii")

        return jsonify({
            "audio":       audio_b64,
            "sample_rate": _sample_rate,
            "prompt":      prompt,
            "duration":    duration,
            "elapsed_s":   round(elapsed, 2),
        })

    except Exception:
        traceback.print_exc()
        return jsonify({"error": "generation failed"}), 500


@app.route("/sfx", methods=["POST"])
@app.route("/api/sfx", methods=["POST"])
def generate_sfx():
    """Generate a short sound effect using MusicGen.

    Uses the same model as music generation but with shorter clips (3-6s) and
    a sound-design prompt prefix that steers MusicGen away from music toward
    ambient/environmental audio textures.

    The JSON response includes a "loops" boolean: True when the source
    description implies a sustained/looping sound (engine, fan, motor, etc.).
    The client should loop the audio only when this flag is set.
    """
    data     = request.get_json(force=True, silent=True) or {}
    prompt   = str(data.get("prompt", "ambient sound")).strip()[:500]
    duration = min(int(data.get("duration", 4)), 10)

    if not prompt:
        return jsonify({"error": "prompt required"}), 400

    # Detect looping nature from the description (continuous, sustained sounds)
    _LOOP_KEYWORDS = ("engine", "motor", "fan", "hum", "drone", "buzz", "whirr",
                      "rumble", "running", "spinning", "rotating", "continuous",
                      "constant", "loop", "idling", "ventilation", "vent",
                      "machinery", "generator", "turbine", "air", "wind",
                      "robotic arm", "fabricator")
    should_loop = any(kw in prompt.lower() for kw in _LOOP_KEYWORDS)

    # Steer MusicGen toward sound design, not melodic music
    sfx_prompt = (
        f"sound effect, {prompt}, "
        f"no melody, no beat, no chords, no music, "
        f"ambient audio texture, field recording, sound design"
    )

    try:
        import torch
        import soundfile as sf

        model, processor = _get_model()

        inputs = processor(
            text=[sfx_prompt],
            padding=True,
            return_tensors="pt",
        ).to(_device)

        max_new_tokens = duration * _FRAME_RATE

        t0 = time.monotonic()
        with torch.no_grad():
            audio_values = model.generate(**inputs, max_new_tokens=max_new_tokens)
        elapsed = time.monotonic() - t0
        print(f"[flask-music] SFX {duration}s in {elapsed:.1f}s loops={should_loop} — {prompt!r}")

        audio_np = audio_values[0, 0].cpu().float().numpy()

        buf = io.BytesIO()
        sf.write(buf, audio_np, _sample_rate, format="WAV", subtype="PCM_16")
        buf.seek(0)
        audio_b64 = base64.b64encode(buf.read()).decode("ascii")

        return jsonify({
            "audio":       audio_b64,
            "sample_rate": _sample_rate,
            "prompt":      prompt,
            "duration":    duration,
            "elapsed_s":   round(elapsed, 2),
            "loops":       should_loop,
        })

    except Exception:
        traceback.print_exc()
        return jsonify({"error": "sfx generation failed"}), 500


if __name__ == "__main__":
    print(f"[flask-music] Starting on :{LISTEN_PORT} | model={MUSICGEN_MODEL}")
    # threaded=True: allows /health to respond while a generation is in progress.
    # PyTorch CPU ops release the GIL, so concurrent health checks work fine.
    app.run(host="0.0.0.0", port=LISTEN_PORT, threaded=True)
