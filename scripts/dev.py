#!/usr/bin/env python3
"""dev.py — Remnant build-mode switcher.

Single script for bringing up or tearing down any of the three Remnant build
modes. Handles port conflict resolution automatically (stops the other builds
before starting the requested one).

All three builds share port 1582 — only one can run at a time.

USAGE:
    python -X utf8 scripts/dev.py dev     # native dev launcher (no window)
    python -X utf8 scripts/dev.py docker  # docker compose up -d
    python -X utf8 scripts/dev.py exe     # native launcher via exe sanity, leaves running
    python -X utf8 scripts/dev.py down    # stop everything
    python -X utf8 scripts/dev.py status  # what's on :1582?
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT     = Path(__file__).parent.parent.resolve()
LAUNCHER = ROOT / "executable" / "remnant_launcher.py"
NATIVE   = ROOT / "executable" / "native-sanity.py"
PORT     = 1582


# ── ANSI ──────────────────────────────────────────────────────────────────────
def _ok(s):   return f"\033[32m{s}\033[0m"
def _warn(s): return f"\033[33m{s}\033[0m"
def _err(s):  return f"\033[31m{s}\033[0m"
def _dim(s):  return f"\033[2m{s}\033[0m"
def _bold(s): return f"\033[1m{s}\033[0m"
def _cyan(s): return f"\033[36m{s}\033[0m"


# ── Port helpers ──────────────────────────────────────────────────────────────
def _port_open(port: int = PORT) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def _port_holder_pid() -> int | None:
    """Return PID of the process listening on PORT, or None."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"(Get-NetTCPConnection -LocalPort {PORT} -State Listen -ErrorAction SilentlyContinue"
             f" | Select-Object -First 1).OwningProcess"],
            capture_output=True, text=True, timeout=5,
        )
        pid_str = result.stdout.strip()
        return int(pid_str) if pid_str.isdigit() else None
    except Exception:
        return None


def _port_holder_name() -> str:
    pid = _port_holder_pid()
    if pid is None:
        return "unknown"
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue).ProcessName"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _wait_port_free(timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _port_open():
            return True
        time.sleep(1)
    return False


def _wait_port_open(timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open():
            print()
            return True
        elapsed = timeout - (deadline - time.time())
        print(f"\r  waiting for :{PORT}… {elapsed:.0f}s", end="", flush=True)
        time.sleep(1)
    print()
    return False


# ── Teardown helpers ──────────────────────────────────────────────────────────
def _kill_stale_nginx():
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "Get-Process nginx -ErrorAction SilentlyContinue | Stop-Process -Force"],
        capture_output=True,
    )


def _stop_docker():
    print(f"  {_dim('stopping docker compose…')}")
    subprocess.run(["docker", "compose", "stop"], cwd=ROOT,
                   capture_output=True, timeout=60)


def cmd_down(args) -> int:
    """Stop all build modes."""
    print(_bold("\n[down] Stopping all Remnant builds…"))
    _stop_docker()
    _kill_stale_nginx()
    if _wait_port_free(15):
        print(f"  {_ok('✓')} port {PORT} is free")
    else:
        print(f"  {_warn('⚠')} port {PORT} still occupied — may need manual intervention")
    return 0


def _free_port(reason: str) -> bool:
    """Stop whatever is holding port 1582. Returns True if port is free."""
    if not _port_open():
        return True
    name = _port_holder_name()
    print(f"  {_warn('⚠')} port {PORT} held by {name} — stopping for {reason}…")
    _stop_docker()
    _kill_stale_nginx()
    if _wait_port_free(20):
        print(f"  {_ok('✓')} port {PORT} free")
        return True
    print(_err(f"✗ could not free port {PORT}"))
    return False


# ── dev: native launcher ──────────────────────────────────────────────────────
def _detect_model_env() -> dict:
    """Detect hardware and return hardware-adaptive model env vars."""
    try:
        import sys as _sys
        _sys.path.insert(0, str(ROOT / "executable"))
        import hardware as _hw_mod
        hw = _hw_mod.detect()
        models = hw.recommended_models()
        print(f"  {_dim('hardware:')} {hw.perf_tier.label} tier → "
              f"LLM={models['OLLAMA_MODEL']}, STT={models['WHISPER_MODEL']}")
        return models
    except Exception:
        return {}


def cmd_dev(args) -> int:
    """Start native dev launcher in --no-browser mode."""
    print(_bold("\n[dev] Starting native dev launcher…"))
    if not LAUNCHER.exists():
        print(_err(f"✗ launcher not found: {LAUNCHER}"))
        return 2
    if not _free_port("dev"):
        return 2

    model_env = _detect_model_env()
    env = {**os.environ, **model_env, "PYTHONIOENCODING": "utf-8"}
    print(f"  {_dim('cmd:')} python -X utf8 {LAUNCHER.name} --no-browser\n")
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(LAUNCHER), "--no-browser"],
        cwd=ROOT, env=env,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
    )

    if not _wait_port_open(90):
        print(_err(f"✗ nginx did not come up in 90s"))
        proc.terminate()
        return 2

    print(f"  {_ok('✓')} dev stack running at {_cyan(f'http://localhost:{PORT}')}")
    print(f"  {_dim('→ Stack is attached to this console. Ctrl+C or close this window to stop.')}")

    # Keep this process alive; the launcher manages its own lifecycle
    try:
        proc.wait()
    except KeyboardInterrupt:
        pass
    return 0


# ── docker: compose up ────────────────────────────────────────────────────────
def cmd_docker(args) -> int:
    """Start docker compose stack."""
    print(_bold("\n[docker] Starting docker compose stack…"))
    if not _free_port("docker"):
        return 2

    model_env = _detect_model_env()
    env = {**os.environ, **model_env, "PYTHONIOENCODING": "utf-8"}
    print(f"  {_dim('cmd:')} docker compose up -d\n")
    result = subprocess.run(["docker", "compose", "up", "-d"], cwd=ROOT, env=env)
    if result.returncode != 0:
        print(_err("✗ docker compose up failed"))
        return 2

    if not _wait_port_open(120):
        print(_err("✗ nginx did not come up in 120s"))
        return 2

    print(f"  {_ok('✓')} docker stack running at {_cyan(f'http://localhost:{PORT}')}")
    print(f"  {_dim('→ docker compose stop   to shut down')}")
    return 0


# ── exe: native launcher with game window ────────────────────────────────────
def cmd_exe(args) -> int:
    """Start exe build with the game window (pywebview or browser fallback).

    This is the full user-facing game experience: services start, then a
    frameless WebView2 window opens to the /game/ UI. Closing the window
    stops all services.

    Claude can still drive the game via the HTTP API while the window is open.
    """
    print(_bold("\n[exe] Starting game window…"), flush=True)
    if not LAUNCHER.exists():
        print(_err(f"✗ launcher not found: {LAUNCHER}"))
        return 2
    if not _free_port("exe"):
        return 2

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    url = f"http://localhost:{PORT}/game/"
    print(f"  {_dim('cmd:')} python -X utf8 {LAUNCHER.name}\n", flush=True)

    # Start launcher WITHOUT --no-browser so pywebview window opens.
    # CREATE_NEW_PROCESS_GROUP isolates CTRL_BREAK so it doesn't kill dev.py.
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(LAUNCHER)],
        cwd=ROOT, env=env,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
    )

    if not _wait_port_open(90):
        print(_err("✗ nginx did not come up in 90s"))
        proc.terminate()
        return 2

    print(f"  {_ok('✓')} game running at {_cyan(url)}")
    print(f"  {_dim('→ Close the game window to stop all services.')}")
    print(f"  {_dim('→ Claude can drive the game via the HTTP API while the window is open.')}")

    # Wait for the launcher/window to exit (user closes the window)
    try:
        proc.wait()
    except KeyboardInterrupt:
        pass
    return 0


# ── check: headless sanity test, leave stack running ─────────────────────────
def cmd_check(args) -> int:
    """Run headless sanity test via native-sanity.py --leave-up.

    Starts the native stack (if not already running), runs the full 10-section
    sanity suite, then leaves the stack running. Used before play sessions or
    as a quick CI check without the full release-sanity.py sequence.
    """
    print(_bold("\n[check] Headless sanity check (leave up)…"), flush=True)
    if not NATIVE.exists():
        print(_err(f"✗ native-sanity.py not found: {NATIVE}"))
        return 2
    if not _free_port("check"):
        return 2

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    rc = subprocess.run(
        [sys.executable, "-X", "utf8", str(NATIVE), "--leave-up"],
        cwd=ROOT, env=env,
    ).returncode

    if rc == 0:
        print(f"\n  {_ok('✓')} stack running at {_cyan(f'http://localhost:{PORT}')}")
        print(f"  {_dim('→ Claude can now drive the game via the HTTP API.')}")
    return rc


# ── status: what's running ────────────────────────────────────────────────────
def cmd_status(args) -> int:
    """Report what's currently on port 1582."""
    print(_bold(f"\n[status] Port {PORT}:"))
    if _port_open():
        name = _port_holder_name()
        pid  = _port_holder_pid()
        print(f"  {_ok('●')} LISTENING  process={name}  pid={pid}")
        # Quick health check
        try:
            import urllib.request, json
            with urllib.request.urlopen(f"http://localhost:{PORT}/health", timeout=3) as r:
                body = json.loads(r.read())
                print(f"  {_dim('health:')} {body}")
        except Exception:
            print(f"  {_dim('health: (no response)')}")
    else:
        print(f"  {_dim('○')} nothing listening on :{PORT}")
    return 0


# ── main ──────────────────────────────────────────────────────────────────────
COMMANDS = {
    "dev":    cmd_dev,     # native dev launcher, headless, console attached
    "docker": cmd_docker,  # docker compose up -d
    "exe":    cmd_exe,     # game window (pywebview/browser), blocks until closed
    "check":  cmd_check,   # headless sanity test, leave stack running for API access
    "down":   cmd_down,    # stop everything
    "status": cmd_status,  # what's on :1582?
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=list(COMMANDS.keys()),
                        help="Build mode to start, or 'down'/'status'")
    args = parser.parse_args()
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
