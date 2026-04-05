"""
Downloader worker for the ollama service.

Talks to a running ollama daemon over HTTP, streams the NDJSON
response from POST /api/pull, and translates per-frame progress
into the same status-JSON shape the splash polls.

Native dev: assumes the host's ollama is running on :11434 already.
Docker: the bootstrap service launches an ephemeral `ollama serve`
pointed at the shared ollama-data volume, pulls into it, and exits.
Either way the downloader script itself is network-agnostic.

Environment:
    OLLAMA_HOST   — default "http://localhost:11434"
    OLLAMA_MODEL  — default "mistral"
    STATUS_DIR    — default ./status (native) or /remnant-status (docker)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import traceback
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, List


SERVICE_NAME = "ollama"
STATUS_DIR = Path(os.environ.get("STATUS_DIR", Path(__file__).parent / "status"))
STATUS_FILE = STATUS_DIR / f"{SERVICE_NAME}.json"

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "mistral")

# Licenses for the models we ship. Keep this tiny whitelist explicit
# rather than scraping — the splash UI pulls from here directly.
MODEL_LICENSES: Dict[str, str] = {
    "mistral": "Apache 2.0",
}


_status_lock = threading.Lock()
_state: Dict[str, Any] = {
    "service": SERVICE_NAME,
    "phase": "pending",
    "models": [
        {
            "key": OLLAMA_MODEL,
            "name": f"{OLLAMA_MODEL} (Ollama)",
            "license": MODEL_LICENSES.get(OLLAMA_MODEL, "see model card"),
            "bytes_done": 0,
            "bytes_total": 0,
        }
    ],
    "error": None,
}


def write_status() -> None:
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    with _status_lock:
        payload = json.dumps(_state, indent=2)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{SERVICE_NAME}.", suffix=".tmp", dir=STATUS_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_path, STATUS_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        traceback.print_exc()


def set_phase(phase: str, error: str | None = None) -> None:
    with _status_lock:
        _state["phase"] = phase
        _state["error"] = error
    write_status()


def update_progress(bytes_done: int, bytes_total: int) -> None:
    with _status_lock:
        m = _state["models"][0]
        m["bytes_done"] = bytes_done
        m["bytes_total"] = bytes_total
    write_status()


def already_present() -> bool:
    """Return True if OLLAMA_MODEL is already in `ollama list`."""
    try:
        req = urllib.request.Request(f"{OLLAMA_HOST}/api/tags")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        names = [m.get("name", "") for m in data.get("models", [])]
        # Ollama's list returns e.g. "mistral:latest" — match on either form.
        return any(n == OLLAMA_MODEL or n.startswith(f"{OLLAMA_MODEL}:") for n in names)
    except Exception as e:
        print(f"[{SERVICE_NAME}] /api/tags probe failed: {e}", file=sys.stderr, flush=True)
        return False


def pull_streaming() -> None:
    """POST /api/pull with stream=true, parse NDJSON, forward bytes."""
    body = json.dumps({"name": OLLAMA_MODEL, "stream": True}).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/pull",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # Ollama pull frames look like:
    #   {"status": "pulling manifest"}
    #   {"status": "pulling <digest>", "digest": "...", "total": N, "completed": M}
    #   {"status": "verifying sha256 digest"}
    #   {"status": "writing manifest"}
    #   {"status": "success"}
    #
    # Per-layer "total" values are distinct — we sum them into an aggregate.
    layer_totals: Dict[str, int] = {}
    layer_done: Dict[str, int] = {}

    with urllib.request.urlopen(req) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue

            digest = frame.get("digest")
            total = frame.get("total")
            completed = frame.get("completed")

            if digest and isinstance(total, int):
                layer_totals[digest] = total
            if digest and isinstance(completed, int):
                layer_done[digest] = completed

            if layer_totals:
                update_progress(
                    bytes_done=sum(layer_done.values()),
                    bytes_total=sum(layer_totals.values()),
                )

            status = frame.get("status", "")
            if status == "success":
                # Flush to 100%.
                total_sum = sum(layer_totals.values())
                update_progress(bytes_done=total_sum, bytes_total=total_sum)
                return
            if "error" in frame:
                raise RuntimeError(frame["error"])


def main() -> int:
    print(f"[{SERVICE_NAME}] writing status to {STATUS_FILE}", flush=True)
    print(f"[{SERVICE_NAME}] target: {OLLAMA_HOST}/api/pull model={OLLAMA_MODEL}", flush=True)

    if already_present():
        print(f"[{SERVICE_NAME}] {OLLAMA_MODEL} already present, marking ready", flush=True)
        # Show the bar as filled — we don't know the exact size without
        # pulling, so fake a 1/1 to render "ready".
        update_progress(bytes_done=1, bytes_total=1)
        set_phase("ready")
        return 0

    set_phase("downloading")
    try:
        pull_streaming()
    except urllib.error.URLError as e:
        set_phase("error", error=f"cannot reach {OLLAMA_HOST}: {e}")
        return 1
    except Exception as e:
        traceback.print_exc()
        set_phase("error", error=f"{type(e).__name__}: {e}")
        return 1

    set_phase("ready")
    print(f"[{SERVICE_NAME}] ready", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
