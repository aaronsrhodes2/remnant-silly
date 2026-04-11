#!/usr/bin/env python3
"""native-sanity.py — sanity test for the native Windows launcher (remnant_launcher.py).

Runs the same battery of checks as docker-sanity.py, but against the native stack:

  1. Stops docker compose if it is holding port 1582 (both stacks share that port).
  2. Starts:  python -X utf8 executable/remnant_launcher.py --no-browser
  3. Waits up to STARTUP_TIMEOUT_S for nginx to come up on :1582.
  4. Delegates the full check suite to docker-sanity.py (same 9 sections).
  5. Sends Ctrl+Break to the launcher subprocess and waits for clean shutdown.

USAGE:
    python -X utf8 scripts/native-sanity.py [--no-stop-docker] [--timeout 60]

Exit code:
    0 — all checks passed
    1 — one or more critical failures
    2 — launcher failed to start / timed out

NOTE: Requires ollama and nginx to be installed and on PATH (or in bin/).
      Runs natively — no Docker required.
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

ROOT = Path(__file__).parent.parent.resolve()
LAUNCHER = ROOT / "executable" / "remnant_launcher.py"
SANITY   = ROOT / "scripts" / "docker-sanity.py"
PORT     = 1582
STARTUP_TIMEOUT_S = 90   # nginx is fast; services take longer but nginx is all we need


# ── ANSI ──────────────────────────────────────────────────────────────────────
def _ok(s):   return f"\033[32m{s}\033[0m"
def _warn(s): return f"\033[33m{s}\033[0m"
def _err(s):  return f"\033[31m{s}\033[0m"
def _dim(s):  return f"\033[2m{s}\033[0m"
def _bold(s): return f"\033[1m{s}\033[0m"


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def wait_for_port(port: int, timeout: float, label: str = "") -> bool:
    """Poll until port is open or timeout expires. Returns True if opened."""
    deadline = time.time() + timeout
    dots = 0
    while time.time() < deadline:
        if port_open(port):
            print()
            return True
        elapsed = timeout - (deadline - time.time())
        print(f"\r  waiting for :{port}{' ' + label if label else ''}… {elapsed:.0f}s", end="", flush=True)
        time.sleep(1)
        dots += 1
    print()
    return False


def stop_docker_compose() -> bool:
    """Attempt to stop docker compose. Returns True if successful or not running."""
    print(f"  {_warn('⚠')} Port {PORT} in use — stopping docker compose…")
    result = subprocess.run(
        ["docker", "compose", "stop"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode == 0:
        print(f"  {_ok('✓')} docker compose stopped")
        # Give it a moment to release the port
        for _ in range(10):
            if not port_open(PORT):
                return True
            time.sleep(1)
        if not port_open(PORT):
            return True
        print(f"  {_err('✗')} port {PORT} still in use after compose stop")
        return False
    else:
        print(f"  {_err('✗')} docker compose stop failed: {result.stderr.strip()[:120]}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--no-stop-docker", action="store_true",
        help="Don't attempt to stop docker compose even if port 1582 is in use",
    )
    parser.add_argument(
        "--timeout", type=int, default=STARTUP_TIMEOUT_S,
        help=f"Seconds to wait for nginx to come up (default: {STARTUP_TIMEOUT_S})",
    )
    parser.add_argument(
        "--base", default=f"http://localhost:{PORT}",
        help=f"Gateway URL to test against (default: http://localhost:{PORT})",
    )
    parser.add_argument(
        "--leave-up", action="store_true",
        help="Leave native stack running after tests (don't stop launcher)",
    )
    parser.add_argument(
        "--expected-composite",
        help="If given, fail if composite_sha256 doesn't match this value",
    )
    args = parser.parse_args()

    print(_bold(f"\n=== Remnant Native Launcher Sanity Test ==="))
    print(f"  Launcher : {LAUNCHER}")
    print(f"  Gateway  : {args.base}")
    print()

    # ── Pre-flight: verify launcher script exists ─────────────────────────────
    if not LAUNCHER.exists():
        print(_err(f"✗ launcher not found: {LAUNCHER}"))
        return 2
    if not SANITY.exists():
        print(_err(f"✗ docker-sanity.py not found: {SANITY}"))
        return 2

    # ── Pre-flight: handle port conflict ─────────────────────────────────────
    print(_bold("Pre-flight"))
    if port_open(PORT):
        if args.no_stop_docker:
            print(_err(f"✗ port {PORT} already in use — pass without --no-stop-docker to auto-stop docker compose"))
            return 2
        try:
            ok = stop_docker_compose()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            # docker not installed or timed out
            ok = False
        if not ok:
            print(_err(f"✗ cannot free port {PORT} — stop docker compose manually and retry"))
            return 2
    else:
        print(f"  {_ok('✓')} port {PORT} free")

    # ── Start native launcher ─────────────────────────────────────────────────
    print(_bold("\nStarting native stack"))
    print(f"  {_dim('cmd:')} python -X utf8 {LAUNCHER.name} --no-browser")

    launcher_env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(LAUNCHER), "--no-browser"],
            cwd=ROOT,
            env=launcher_env,
        )
    except Exception as e:
        print(_err(f"✗ failed to start launcher: {e}"))
        return 2

    # ── Wait for nginx gateway ────────────────────────────────────────────────
    print(f"\n  waiting for nginx on :{PORT}…")
    up = wait_for_port(PORT, timeout=args.timeout, label="(nginx)")
    if not up:
        print(_err(f"✗ nginx did not come up in {args.timeout}s"))
        _stop_launcher(proc)
        return 2
    print(f"  {_ok('✓')} nginx up on :{PORT}")

    # ── Run the shared sanity suite ───────────────────────────────────────────
    print(_bold("\nRunning sanity suite (docker-sanity.py)"))
    print(_dim(f"  (same 9-section checks as docker sanity)\n"))

    sanity_cmd = [sys.executable, "-X", "utf8", str(SANITY), "--base", args.base]
    if args.expected_composite:
        sanity_cmd += ["--expected-composite", args.expected_composite]
    result = subprocess.run(sanity_cmd, cwd=ROOT, env=launcher_env)
    sanity_code = result.returncode

    # ── Shutdown ──────────────────────────────────────────────────────────────
    if args.leave_up:
        print(_bold("\nLeaving native stack running (--leave-up)"))
        print(f"  {_ok('✓')} stack running at {args.base}")
    else:
        print(_bold("\nShutting down native stack"))
        _stop_launcher(proc)
        print(f"  {_ok('✓')} launcher stopped")

    # ── Final verdict ─────────────────────────────────────────────────────────
    print()
    if sanity_code == 0:
        print(_ok("✓ Native stack sanity: PASSED"))
        return 0
    else:
        print(_err(f"✗ Native stack sanity: FAILED (sanity exit code {sanity_code})"))
        return 1


def _stop_launcher(proc: subprocess.Popen):
    """Send Ctrl+Break (Windows) or SIGTERM (Unix) and wait for clean exit."""
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
