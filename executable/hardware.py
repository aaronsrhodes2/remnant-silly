"""hardware.py - GPU/RAM/CPU detection and performance estimation for Remnant.

Used by the Windows launcher to tell the user what response speed to expect
before the first turn is taken. All detection is best-effort; missing data
degrades gracefully to conservative estimates.

No pip dependencies beyond psutil (bundled in the launcher exe via PyInstaller).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Optional

try:
    import psutil
    _HAVE_PSUTIL = True
except ImportError:
    _HAVE_PSUTIL = False


# ---------------------------------------------------------------------------
# Performance lookup table
#
# Maps VRAM brackets to (response_min_s, response_max_s, tier_label) for the
# default language model (qwen2.5:14b at Q4_K_M, ~8.9 GB).
# "Response time" = wall-clock seconds from Enter to first visible token,
# measured on typical narrative-length prompts (~2000-token context).
# These are real-world medians, not marketing benchmarks.
# ---------------------------------------------------------------------------
@dataclass
class PerfTier:
    label: str            # e.g. "Fast (gaming GPU)"
    min_s: int            # optimistic wall-clock seconds per response
    max_s: int            # pessimistic wall-clock seconds per response
    tok_s: str            # displayed tokens/second range
    note: str = ""        # extra context shown to user

# VRAM threshold (GB) → PerfTier - evaluated highest-first
_VRAM_TIERS: list[tuple[float, PerfTier]] = [
    (24.0, PerfTier(
        label="Excellent",
        min_s=4, max_s=10,
        tok_s="35-60",
        note="High-end GPU - snappy responses, near real-time.",
    )),
    (16.0, PerfTier(
        label="Great",
        min_s=8, max_s=18,
        tok_s="20-40",
        note="Mid-high GPU - comfortable play speed.",
    )),
    (12.0, PerfTier(
        label="Good",
        min_s=12, max_s=25,
        tok_s="14-28",
        note="Mid-range GPU - a slight pause between turns.",
    )),
    (8.0, PerfTier(
        label="Moderate",
        min_s=18, max_s=40,
        tok_s="8-18",
        note="Entry GPU - model runs quantized. Noticeable pause.",
    )),
    (6.0, PerfTier(
        label="Slow",
        min_s=30, max_s=75,
        tok_s="4-10",
        note="Minimal VRAM - model may offload layers to RAM.",
    )),
    (0.0, PerfTier(
        label="CPU-only",
        min_s=60, max_s=180,
        tok_s="1-4",
        note="No GPU acceleration. Long waits - best with a fast CPU.",
    )),
]

# Recommended model selections per performance tier.
# Keys are env var names read by each service. Values are HuggingFace/Ollama
# model IDs. Lower tiers prioritise speed; higher tiers prioritise quality.
# User-set env vars always override these recommendations.
_MODEL_PROFILES: dict[str, dict[str, str]] = {
    "Excellent": {       # 24+ GB VRAM
        "OLLAMA_MODEL":   "qwen2.5:32b",
        "WHISPER_MODEL":  "large-v3",
        "MUSICGEN_MODEL": "facebook/musicgen-small",  # CPU-based; small = ~4x realtime vs medium 18x
    },
    "Great": {           # 16+ GB VRAM
        "OLLAMA_MODEL":   "qwen2.5:14b",
        "WHISPER_MODEL":  "medium.en",
        "MUSICGEN_MODEL": "facebook/musicgen-small",  # CPU-based; small = ~4x realtime vs medium 18x
    },
    "Good": {            # 12+ GB VRAM
        "OLLAMA_MODEL":   "qwen2.5:14b",
        "WHISPER_MODEL":  "small.en",
        "MUSICGEN_MODEL": "facebook/musicgen-small",  # CPU-based; small = ~4x realtime vs medium 18x
    },
    "Moderate": {        # 8+ GB VRAM
        "OLLAMA_MODEL":   "qwen2.5:7b",
        "WHISPER_MODEL":  "base.en",
        "MUSICGEN_MODEL": "facebook/musicgen-small",
    },
    "Slow": {            # 6+ GB VRAM
        "OLLAMA_MODEL":   "qwen2.5:3b",
        "WHISPER_MODEL":  "base.en",
        "MUSICGEN_MODEL": "facebook/musicgen-small",
    },
    "CPU-only": {        # <6 GB VRAM
        "OLLAMA_MODEL":   "qwen2.5:3b",
        "WHISPER_MODEL":  "tiny.en",
        "MUSICGEN_MODEL": "facebook/musicgen-small",
    },
}


@dataclass
class HardwareProfile:
    gpu_name: str = "Unknown"
    gpu_vram_gb: float = 0.0
    ram_gb: float = 0.0
    cpu_name: str = "Unknown"
    cpu_cores: int = 0
    detection_warnings: list[str] = field(default_factory=list)

    @property
    def perf_tier(self) -> PerfTier:
        for threshold, tier in _VRAM_TIERS:
            if self.gpu_vram_gb >= threshold:
                return tier
        return _VRAM_TIERS[-1][1]

    @property
    def has_gpu(self) -> bool:
        return self.gpu_vram_gb > 0.0

    def recommended_models(self) -> dict[str, str]:
        """Return recommended model env vars for this hardware tier.

        Respects explicit user overrides — if an env var is already set in the
        environment, that value is kept instead of the auto-selected one.
        """
        import os  # noqa: PLC0415
        base = _MODEL_PROFILES.get(self.perf_tier.label, _MODEL_PROFILES["CPU-only"])
        return {k: os.environ.get(k, v) for k, v in base.items()}


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], timeout: float = 5.0) -> Optional[str]:
    """Run a command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


def _detect_gpu_nvidia() -> tuple[str, float]:
    """Return (gpu_name, vram_gb) from nvidia-smi, or ('', 0.0)."""
    out = _run([
        "nvidia-smi",
        "--query-gpu=name,memory.total",
        "--format=csv,noheader,nounits",
    ])
    if not out:
        return "", 0.0
    first = out.splitlines()[0]
    parts = [p.strip() for p in first.split(",")]
    if len(parts) < 2:
        return "", 0.0
    name = parts[0]
    try:
        vram_mb = float(parts[1])
        return name, round(vram_mb / 1024, 1)
    except ValueError:
        return name, 0.0


def _detect_gpu_wmi() -> tuple[str, float]:
    """Fallback: query WMI for GPU name (Windows only, no VRAM info)."""
    if sys.platform != "win32":
        return "", 0.0
    out = _run([
        "powershell", "-NoProfile", "-Command",
        "(Get-WmiObject Win32_VideoController | Select-Object -First 1 Name).Name",
    ])
    if out:
        return out, 0.0
    return "", 0.0


def _detect_gpu_rocm() -> tuple[str, float]:
    """AMD ROCm: rocm-smi --showmeminfo vram --json."""
    if not shutil.which("rocm-smi"):
        return "", 0.0
    out = _run(["rocm-smi", "--showmeminfo", "vram", "--json"])
    if not out:
        return "", 0.0
    try:
        import json
        data = json.loads(out)
        # rocm-smi JSON varies by version; try a few key shapes
        for card_data in data.values():
            if isinstance(card_data, dict):
                total = card_data.get("VRAM Total Memory (B)") or card_data.get("vram_total")
                if total:
                    vram_gb = round(int(total) / (1024 ** 3), 1)
                    name = card_data.get("Card Series") or card_data.get("name") or "AMD GPU"
                    return name, vram_gb
    except Exception:
        pass
    return "AMD GPU (ROCm)", 0.0


def _detect_cpu() -> tuple[str, int]:
    """Return (cpu_name, physical_core_count)."""
    name = "Unknown"
    cores = 0

    if _HAVE_PSUTIL:
        cores = psutil.cpu_count(logical=False) or psutil.cpu_count() or 0

    if sys.platform == "win32":
        out = _run([
            "powershell", "-NoProfile", "-Command",
            "(Get-WmiObject Win32_Processor | Select-Object -First 1 Name).Name",
        ])
        if out:
            name = out
    elif sys.platform == "darwin":
        out = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
        if out:
            name = out
    else:
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if "model name" in line:
                        name = line.split(":", 1)[1].strip()
                        break
        except Exception:
            pass

    return name, cores


def _detect_ram_gb() -> float:
    if _HAVE_PSUTIL:
        return round(psutil.virtual_memory().total / (1024 ** 3), 1)
    if sys.platform == "win32":
        out = _run([
            "powershell", "-NoProfile", "-Command",
            "(Get-WmiObject Win32_ComputerSystem).TotalPhysicalMemory",
        ])
        if out:
            try:
                return round(int(out) / (1024 ** 3), 1)
            except ValueError:
                pass
    return 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect() -> HardwareProfile:
    """Detect all hardware, returning a HardwareProfile.  Never raises."""
    hw = HardwareProfile()
    warnings: list[str] = []

    # GPU - try nvidia-smi first (fastest + most accurate)
    gpu_name, vram_gb = _detect_gpu_nvidia()
    if not gpu_name:
        gpu_name, vram_gb = _detect_gpu_rocm()
    if not gpu_name:
        gpu_name, _ = _detect_gpu_wmi()
        if gpu_name:
            warnings.append(
                f"GPU detected ({gpu_name}) but VRAM unknown - "
                "install NVIDIA drivers or ROCm for accurate estimate"
            )
    if gpu_name:
        hw.gpu_name = gpu_name
        hw.gpu_vram_gb = vram_gb

    # RAM
    hw.ram_gb = _detect_ram_gb()

    # CPU
    hw.cpu_name, hw.cpu_cores = _detect_cpu()
    hw.detection_warnings = warnings
    return hw


def format_summary(hw: HardwareProfile) -> str:
    """Return a human-readable multi-line performance summary."""
    tier = hw.perf_tier
    lines = [
        "Hardware detected:",
        f"  GPU  : {hw.gpu_name}" + (f"  ({hw.gpu_vram_gb} GB VRAM)" if hw.gpu_vram_gb else "  (no VRAM detected)"),
        f"  RAM  : {hw.ram_gb} GB" if hw.ram_gb else "  RAM  : unknown",
        f"  CPU  : {hw.cpu_name}" + (f"  ({hw.cpu_cores} cores)" if hw.cpu_cores else ""),
        "",
        f"Performance estimate: {tier.label}",
        f"  Expected response time : {tier.min_s}-{tier.max_s} seconds per turn",
        f"  Token generation speed : ~{tier.tok_s} tokens/second",
    ]
    if tier.note:
        lines.append(f"  Note : {tier.note}")
    if hw.detection_warnings:
        lines.append("")
        for w in hw.detection_warnings:
            lines.append(f"  [!] {w}")
    return "\n".join(lines)


def to_status_dict(hw: HardwareProfile) -> dict:
    """Return a JSON-serialisable dict for writing to status/hardware-profile.json."""
    tier = hw.perf_tier
    return {
        "gpu_name": hw.gpu_name,
        "gpu_vram_gb": hw.gpu_vram_gb,
        "ram_gb": hw.ram_gb,
        "cpu_name": hw.cpu_name,
        "cpu_cores": hw.cpu_cores,
        "perf_tier": {
            "label": tier.label,
            "min_s": tier.min_s,
            "max_s": tier.max_s,
            "tok_s": tier.tok_s,
            "note": tier.note,
        },
        "detection_warnings": hw.detection_warnings,
    }


if __name__ == "__main__":
    hw = detect()
    print(format_summary(hw))
