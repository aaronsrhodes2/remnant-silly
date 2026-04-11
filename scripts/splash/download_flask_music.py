"""
Downloader worker for the flask-music service.

Fetches facebook/musicgen-small from HuggingFace, reporting byte-level
progress to a shared status JSON file that the nginx splash polls.

Contract with splash.js:
    {
      "service": "flask-music",
      "phase": "pending" | "downloading" | "ready" | "error",
      "models": [
        {"key": "musicgen-small", "name": "...", "license": "...",
         "bytes_done": <int>, "bytes_total": <int>}
      ],
      "error": null | "<message>"
    }

Writes are atomic (tmp + rename) so the splash never reads a half-written JSON.

Usage:
    # Native dev
    python download_flask_music.py

    # Docker — writes into the shared remnant-status volume
    STATUS_DIR=/remnant-status python download_flask_music.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List

from huggingface_hub import snapshot_download, HfApi


# ---------------------------------------------------------------------------
# Status file shape and atomic write (same pattern as download_flask_sd.py)
# ---------------------------------------------------------------------------

SERVICE_NAME = "flask-music"
STATUS_DIR = Path(os.environ.get("STATUS_DIR", Path(__file__).parent / "status"))
STATUS_FILE = STATUS_DIR / f"{SERVICE_NAME}.json"

MODELS: List[Dict[str, Any]] = [
    {
        "key": "musicgen-small",
        "name": "MusicGen Small (facebook)",
        "license": "CC-BY-NC 4.0",
        "bytes_done": 0,
        "bytes_total": 0,
    },
]

_status_lock = threading.Lock()
_state: Dict[str, Any] = {
    "service": SERVICE_NAME,
    "phase": "pending",
    "models": MODELS,
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
            for attempt in range(5):
                try:
                    os.replace(tmp_path, STATUS_FILE)
                    return
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05)
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


def update_model(key: str, *, bytes_done: int | None = None, bytes_total: int | None = None) -> None:
    with _status_lock:
        for m in _state["models"]:
            if m["key"] == key:
                if bytes_total is not None:
                    m["bytes_total"] = bytes_total
                if bytes_done is not None:
                    m["bytes_done"] = bytes_done
                break
    write_status()


# ---------------------------------------------------------------------------
# Disk-sampling progress tracker (same pattern as download_flask_sd.py)
# ---------------------------------------------------------------------------

def _get_repo_bytes_total(repo_id: str) -> int:
    api = HfApi()
    try:
        info = api.repo_info(repo_id=repo_id, files_metadata=True)
        files = getattr(info, "siblings", []) or []
        total = 0
        for f in files:
            size = getattr(f, "size", None) or getattr(f, "lfs", None)
            if isinstance(size, dict):
                size = size.get("size")
            if isinstance(size, int):
                total += size
        return total
    except Exception:
        return 1_200_000_000  # ~1.2 GB fallback estimate for musicgen-small


def _dir_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for root, _, files in os.walk(path, followlinks=True):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def _hf_cache_dir_for_repo(repo_id: str) -> Path:
    hf_home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    slug = "models--" + repo_id.replace("/", "--")
    return hf_home / "hub" / slug


class DiskProgressTracker:
    def __init__(self, model_key: str, watch_dir: Path, bytes_total: int, interval: float = 0.5):
        self.model_key = model_key
        self.watch_dir = watch_dir
        self.bytes_total = bytes_total
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._baseline = _dir_bytes(watch_dir)

    def _loop(self):
        update_model(self.model_key, bytes_done=0, bytes_total=self.bytes_total)
        while not self._stop.is_set():
            current = _dir_bytes(self.watch_dir)
            done = max(0, current - self._baseline)
            update_model(self.model_key, bytes_done=min(done, self.bytes_total), bytes_total=self.bytes_total)
            if self._stop.wait(self.interval):
                break

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True, name=f"disk-progress-{self.model_key}")
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        current = _dir_bytes(self.watch_dir)
        done = max(0, current - self._baseline)
        update_model(self.model_key, bytes_done=min(done, self.bytes_total), bytes_total=self.bytes_total)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _download_musicgen_small() -> None:
    repo_id = "facebook/musicgen-small"
    total = _get_repo_bytes_total(repo_id)
    cache_dir = _hf_cache_dir_for_repo(repo_id)
    tracker = DiskProgressTracker("musicgen-small", cache_dir, bytes_total=total).start()
    try:
        snapshot_download(repo_id=repo_id)
    finally:
        tracker.stop()


def main() -> int:
    set_phase("downloading")
    print(f"[{SERVICE_NAME}] writing status to {STATUS_FILE}", flush=True)
    try:
        _download_musicgen_small()
    except Exception as e:
        print(f"[{SERVICE_NAME}] download failed: {e}", file=sys.stderr, flush=True)
        traceback.print_exc()
        set_phase("error", error=f"{type(e).__name__}: {e}")
        return 1

    with _status_lock:
        for m in _state["models"]:
            if m["bytes_total"] > 0:
                m["bytes_done"] = m["bytes_total"]
    set_phase("ready")
    print(f"[{SERVICE_NAME}] ready", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
