"""
Downloader worker for the flask-sd service.

Fetches Stable Diffusion v1.5 (fp16 variant) and the IP-Adapter weight
file that image_generator_api.py loads, reporting byte-level progress
to a shared status JSON file that the nginx splash polls.

Contract with splash.js:
    {
      "service": "flask-sd",
      "phase": "pending" | "downloading" | "ready" | "error",
      "models": [
        {"name": "...", "license": "...",
         "bytes_done": <int>, "bytes_total": <int>}
      ],
      "error": null | "<message>"
    }

Writes are atomic (tmp + rename) so the splash never reads a half-
written JSON.

Usage:
    # Native dev — writes into ./status/ beside the script
    python download_flask_sd.py

    # Docker — writes into the shared remnant-status volume
    STATUS_DIR=/remnant-status python download_flask_sd.py
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

# huggingface_hub is already pinned at 0.25.2 in docker/flask-sd/requirements.txt
from huggingface_hub import snapshot_download, hf_hub_download, HfApi


# ---------------------------------------------------------------------------
# Status file shape and atomic write.
# ---------------------------------------------------------------------------

SERVICE_NAME = "flask-sd"
STATUS_DIR = Path(os.environ.get("STATUS_DIR", Path(__file__).parent / "status"))
STATUS_FILE = STATUS_DIR / f"{SERVICE_NAME}.json"

# Model descriptors in the order the splash should display them.
# bytes_total is filled in at runtime as soon as tqdm sees the first
# "total" update; we seed with a reasonable estimate so the bar isn't
# empty on frame 1.
MODELS: List[Dict[str, Any]] = [
    {
        "key": "sd15",
        "name": "stable-diffusion-v1-5 (fp16)",
        "license": "CreativeML Open RAIL-M",
        "bytes_done": 0,
        "bytes_total": 0,
    },
    {
        "key": "ip-adapter",
        "name": "IP-Adapter Plus (SD 1.5)",
        "license": "Apache 2.0",
        "bytes_done": 0,
        "bytes_total": 0,
    },
]

# Guards concurrent writes from tqdm callbacks on multiple threads.
_status_lock = threading.Lock()
_state: Dict[str, Any] = {
    "service": SERVICE_NAME,
    "phase": "pending",
    "models": MODELS,
    "error": None,
}


def write_status() -> None:
    """Atomically persist _state to STATUS_FILE.

    Serialised through _status_lock end-to-end so concurrent tqdm
    threads don't race each other into os.replace. On Windows,
    os.replace can transiently fail with PermissionError if another
    handle (antivirus, indexer) has the target open — retry briefly.
    """
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    with _status_lock:
        payload = json.dumps(_state, indent=2)
        fd, tmp_path = tempfile.mkstemp(prefix=f".{SERVICE_NAME}.", suffix=".tmp", dir=STATUS_DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            # Retry os.replace a few times for Windows races.
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
# tqdm shim — huggingface_hub lets us pass tqdm_class into snapshot_download
# and hf_hub_download. We subclass tqdm and forward n/total into _state.
#
# HF creates one tqdm per file (for snapshot_download). The "desc" kwarg
# contains the filename, which we can use to route per-file progress to
# the right top-level model entry. For our purposes we aggregate all
# files of a given model under one bar.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Disk-sampling progress tracker.
#
# huggingface_hub's progress mechanism has shifted across versions: 0.x
# drove tqdm via update(), 1.x mutates tqdm internals directly and often
# leaves placeholder bars that never emit events. Trying to ride the
# tqdm API is fragile. Instead we treat the HF cache directory on disk
# as the ground truth: size the repo up front via HfApi, then poll the
# cache dir's total bytes on an interval from a background thread and
# forward that to the status JSON.
# ---------------------------------------------------------------------------


def _get_repo_bytes_total(repo_id: str, allow_patterns: list[str] | None = None, filename: str | None = None) -> int:
    """Sum the sizes of files we're about to download.

    If filename is given, return just that file's size. Otherwise walk
    the repo tree and sum files matching allow_patterns.
    """
    import fnmatch
    api = HfApi()
    info = api.repo_info(repo_id=repo_id, files_metadata=True)
    files = getattr(info, "siblings", []) or []
    total = 0
    for f in files:
        name = getattr(f, "rfilename", None) or getattr(f, "path", None)
        size = getattr(f, "size", None) or getattr(f, "lfs", None)
        if isinstance(size, dict):
            size = size.get("size")
        if not name or not isinstance(size, int):
            continue
        if filename is not None:
            if name == filename:
                return size
            continue
        if allow_patterns and not any(fnmatch.fnmatch(name, pat) for pat in allow_patterns):
            continue
        total += size
    return total


def _dir_bytes(path: Path) -> int:
    """Walk a directory and sum file sizes. Follows symlinks, ignores errors."""
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


class DiskProgressTracker:
    """Background thread that polls a directory size and updates status JSON."""

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
        # Final snapshot — ensure we report the true on-disk delta.
        current = _dir_bytes(self.watch_dir)
        done = max(0, current - self._baseline)
        update_model(self.model_key, bytes_done=min(done, self.bytes_total), bytes_total=self.bytes_total)


SD15_ALLOW_PATTERNS = [
    "*.json",
    "*.txt",
    "tokenizer/*",
    "feature_extractor/*",
    "scheduler/*",
    "text_encoder/*.fp16.safetensors",
    "text_encoder/config.json",
    "unet/*.fp16.safetensors",
    "unet/config.json",
    "vae/*.fp16.safetensors",
    "vae/config.json",
    "model_index.json",
]


def _hf_cache_dir_for_repo(repo_id: str) -> Path:
    """Return the HF hub cache subdirectory for a given repo."""
    hf_home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    slug = "models--" + repo_id.replace("/", "--")
    return hf_home / "hub" / slug


def _download_sd15() -> None:
    """Download SD v1.5 fp16 shards via snapshot_download."""
    repo_id = "runwayml/stable-diffusion-v1-5"
    total = _get_repo_bytes_total(repo_id, allow_patterns=SD15_ALLOW_PATTERNS)
    tracker = DiskProgressTracker("sd15", _hf_cache_dir_for_repo(repo_id), bytes_total=total).start()
    try:
        snapshot_download(repo_id=repo_id, allow_patterns=SD15_ALLOW_PATTERNS)
    finally:
        tracker.stop()


def _download_ip_adapter() -> None:
    """Download the specific IP-Adapter weight file image_generator_api.py loads."""
    repo_id = "h94/IP-Adapter"
    filename = "models/ip-adapter-plus_sd15.bin"
    total = _get_repo_bytes_total(repo_id, filename=filename)
    tracker = DiskProgressTracker("ip-adapter", _hf_cache_dir_for_repo(repo_id), bytes_total=total).start()
    try:
        hf_hub_download(repo_id=repo_id, filename=filename)
    finally:
        tracker.stop()


def main() -> int:
    set_phase("downloading")
    print(f"[{SERVICE_NAME}] writing status to {STATUS_FILE}", flush=True)
    try:
        _download_sd15()
        _download_ip_adapter()
    except Exception as e:
        print(f"[{SERVICE_NAME}] download failed: {e}", file=sys.stderr, flush=True)
        traceback.print_exc()
        set_phase("error", error=f"{type(e).__name__}: {e}")
        return 1

    # Ensure every bar reads 100% on completion, even if the last tqdm
    # update lagged behind the final byte.
    with _status_lock:
        for m in _state["models"]:
            if m["bytes_total"] > 0:
                m["bytes_done"] = m["bytes_total"]
    set_phase("ready")
    print(f"[{SERVICE_NAME}] ready", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
