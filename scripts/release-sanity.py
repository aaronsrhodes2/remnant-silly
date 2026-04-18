#!/usr/bin/env python3
"""release-sanity.py — Cross-build release verification for Remnant.

Verifies that native dev, docker, and exe builds all serve identical story content
by comparing each build's composite_sha256 against the native dev source of truth.

SEQUENCE:
  1. Native dev  — stops docker if running, starts launcher --no-browser, runs full
                   sanity suite, records composite_sha256 as source of truth, stops launcher.
  2. Docker      — docker compose up -d, runs sanity with --expected-composite, compose stop.
  3. Exe         — starts via native-sanity.py --leave-up (exe stays running at end).

USAGE:
    python -X utf8 scripts/release-sanity.py [options]

OPTIONS:
    --skip-native      Skip phase 1 (assume native already verified; requires --truth)
    --skip-docker      Skip phase 2
    --skip-exe         Skip phase 3
    --truth HASH       Use this composite as source of truth (with --skip-native)
    --tag              After all phases pass, write version.json via tag-version.py
    --timeout N        Seconds to wait for nginx per phase (default: 90)
    --base URL         Gateway URL (default: http://localhost:1582)

EXIT CODE:
    0  — all phases passed, composites match
    1  — one or more phases failed or composites diverge
    2  — infrastructure error (can't start stack)
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT     = Path(__file__).parent.parent.resolve()
LAUNCHER = ROOT / "executable" / "remnant_launcher.py"
SANITY   = ROOT / "scripts" / "docker-sanity.py"
NATIVE   = ROOT / "executable" / "native-sanity.py"
TAGGER   = ROOT / "scripts" / "tag-version.py"
PORT     = 1582


# ── ANSI ──────────────────────────────────────────────────────────────────────
def _ok(s):   return f"\033[32m{s}\033[0m"
def _warn(s): return f"\033[33m{s}\033[0m"
def _err(s):  return f"\033[31m{s}\033[0m"
def _dim(s):  return f"\033[2m{s}\033[0m"
def _bold(s): return f"\033[1m{s}\033[0m"
def _cyan(s): return f"\033[36m{s}\033[0m"

def _head(msg: str):
    print(f"\n{_bold('═' * 60)}")
    print(_bold(f"  {msg}"))
    print(_bold('═' * 60))


# ── Networking helpers ────────────────────────────────────────────────────────
def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _wait_for_port(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open(port):
            print()
            return True
        elapsed = timeout - (deadline - time.time())
        print(f"\r  waiting for :{port}… {elapsed:.0f}s", end="", flush=True)
        time.sleep(1)
    print()
    return False


def _wait_port_free(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _port_open(port):
            return True
        time.sleep(1)
    return False


def _fetch_composite(base: str = f"http://localhost:{PORT}") -> str:
    try:
        req = urllib.request.Request(
            f"{base}/signature",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
            return data.get("composite_sha256", "")
    except Exception as e:
        print(f"  {_warn('⚠')} could not fetch /signature: {e}")
        return ""


def _stop_proc(proc: subprocess.Popen):
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


def _kill_stale_nginx():
    """Kill any stale native nginx.exe processes (Windows)."""
    if sys.platform == "win32":
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-Process nginx -ErrorAction SilentlyContinue | Stop-Process -Force"],
            capture_output=True,
        )


# ── Phase 1: Native dev (source of truth) ────────────────────────────────────
def phase_native(args) -> tuple[int, str]:
    """Start native launcher, run sanity, record composite, stop launcher.
    Returns (exit_code, truth_composite)."""
    _head("Phase 1 — Native dev (source of truth)")

    # Pre-flight: free the port
    if _port_open(PORT):
        print(f"  {_warn('⚠')} Port {PORT} in use — stopping docker compose and stale nginx…")
        subprocess.run(["docker", "compose", "stop"], cwd=ROOT,
                       capture_output=True, timeout=60)
        _kill_stale_nginx()
        if not _wait_port_free(PORT, 20):
            print(_err(f"✗ Could not free port {PORT}"))
            return 2, ""

    if not LAUNCHER.exists():
        print(_err(f"✗ Launcher not found: {LAUNCHER}"))
        return 2, ""

    # Start launcher
    print(f"  {_dim('cmd:')} python -X utf8 {LAUNCHER.name} --no-browser\n")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    # CREATE_NEW_PROCESS_GROUP isolates the launcher's process group so that
    # send_signal(CTRL_BREAK_EVENT) in _stop_proc only kills the launcher
    # tree, not release-sanity.py itself (Windows console signal propagation).
    create_flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(LAUNCHER), "--no-browser"],
        cwd=ROOT, env=env,
        creationflags=create_flags,
    )

    # Wait for nginx
    up = _wait_for_port(PORT, args.timeout)
    if not up:
        print(_err(f"✗ nginx did not come up in {args.timeout}s"))
        _stop_proc(proc)
        return 2, ""
    print(f"  {_ok('✓')} nginx up on :{PORT}")

    # Fetch composite (source of truth)
    time.sleep(2)  # brief settle so fortress is warm
    truth = _fetch_composite(args.base)
    if truth:
        print(f"  {_ok('✓')} truth composite: {_cyan(truth[:32])}…")
    else:
        print(f"  {_warn('⚠')} could not fetch composite — checks will still run")

    # Run sanity suite
    print()
    rc = subprocess.run(
        [sys.executable, "-X", "utf8", str(SANITY), "--base", args.base],
        cwd=ROOT, env=env,
    ).returncode

    # Stop launcher
    print(_bold("\nStopping native stack…"))
    _stop_proc(proc)
    print(f"  {_ok('✓')} native stack stopped")

    return rc, truth


# ── Phase 2: Docker ───────────────────────────────────────────────────────────
def phase_docker(truth: str, args) -> int:
    """docker compose up → sanity with --expected-composite → compose stop."""
    _head("Phase 2 — Docker")

    # Free the port first
    if _port_open(PORT):
        print(f"  {_warn('⚠')} Port {PORT} in use — killing stale nginx…")
        _kill_stale_nginx()
        if not _wait_port_free(PORT, 15):
            print(_err(f"✗ Port {PORT} occupied — cannot start docker"))
            return 2

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    # Rebuild images that bake in source files (nginx bakes web/, fortress bakes app.py)
    print(f"  {_dim('cmd:')} docker compose build nginx fortress\n")
    build_result = subprocess.run(
        ["docker", "compose", "build", "nginx", "fortress"],
        cwd=ROOT, env=env,
    )
    if build_result.returncode != 0:
        print(_err("✗ docker compose build failed"))
        return 2

    print(f"  {_dim('cmd:')} docker compose up -d\n")
    result = subprocess.run(
        ["docker", "compose", "up", "-d"],
        cwd=ROOT, env=env,
    )
    if result.returncode != 0:
        print(_err("✗ docker compose up failed"))
        return 2

    up = _wait_for_port(PORT, args.timeout)
    if not up:
        print(_err(f"✗ nginx did not come up in {args.timeout}s"))
        subprocess.run(["docker", "compose", "stop"], cwd=ROOT, capture_output=True)
        return 2
    print(f"  {_ok('✓')} nginx up on :{PORT}")

    # Fetch composite and echo it
    docker_composite = _fetch_composite(args.base)
    if docker_composite:
        print(f"  {_dim('docker composite:')} {docker_composite[:32]}…")

    # Run sanity
    sanity_cmd = [sys.executable, "-X", "utf8", str(SANITY), "--base", args.base]
    if truth:
        sanity_cmd += ["--expected-composite", truth]
    print()
    rc = subprocess.run(sanity_cmd, cwd=ROOT, env=env).returncode

    # Stop docker
    print(_bold("\nStopping docker stack…"))
    subprocess.run(["docker", "compose", "stop"], cwd=ROOT, capture_output=True, timeout=60)
    if _wait_port_free(PORT, 15):
        print(f"  {_ok('✓')} docker stack stopped")
    else:
        print(f"  {_warn('⚠')} port {PORT} may still be held — proceeding anyway")

    return rc


# ── Phase 3: Exe ──────────────────────────────────────────────────────────────
def phase_exe(truth: str, args) -> int:
    """Start native stack via native-sanity.py --leave-up. Exe stays running."""
    _head("Phase 3 — Exe (leaves stack running)")

    if not NATIVE.exists():
        print(_err(f"✗ native-sanity.py not found: {NATIVE}"))
        return 2

    # Free the port
    if _port_open(PORT):
        print(f"  {_warn('⚠')} Port {PORT} in use — stopping docker and killing stale nginx…")
        subprocess.run(["docker", "compose", "stop"], cwd=ROOT, capture_output=True, timeout=60)
        _kill_stale_nginx()
        if not _wait_port_free(PORT, 20):
            print(_err(f"✗ Could not free port {PORT}"))
            return 2

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    cmd = [
        sys.executable, "-X", "utf8", str(NATIVE),
        "--leave-up",
        "--timeout", str(args.timeout),
        "--base", args.base,
    ]
    if truth:
        cmd += ["--expected-composite", truth]

    print(f"  {_dim('cmd:')} native-sanity.py --leave-up ...\n")
    rc = subprocess.run(cmd, cwd=ROOT, env=env).returncode

    if rc == 0:
        print(f"\n  {_ok('✓')} Exe stack is running at {_cyan(args.base)}")
        print(f"  {_dim('→ Close the launcher console to stop services.')}")
    else:
        print(f"\n  {_err('✗')} Exe sanity failed (rc={rc})")

    return rc


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--skip-native", action="store_true", help="Skip phase 1")
    parser.add_argument("--skip-docker", action="store_true", help="Skip phase 2")
    parser.add_argument("--skip-exe",    action="store_true", help="Skip phase 3")
    parser.add_argument("--truth", default="",
                        help="Use this composite as source of truth (with --skip-native)")
    parser.add_argument("--tag", action="store_true",
                        help="Write version.json after all phases pass")
    parser.add_argument("--timeout", type=int, default=90,
                        help="Seconds to wait for nginx per phase (default: 90)")
    parser.add_argument("--base", default=f"http://localhost:{PORT}",
                        help=f"Gateway URL (default: http://localhost:{PORT})")
    args = parser.parse_args()

    print(_bold(f"\n{'═'*60}"))
    print(_bold(f"  Remnant Release Sanity  —  cross-build verification"))
    print(_bold(f"{'═'*60}"))
    phases_run: list[str] = []
    results: dict[str, int] = {}
    truth = args.truth

    # ── Phase 1 ───────────────────────────────────────────────────────────────
    if not args.skip_native:
        rc, truth = phase_native(args)
        results["native"] = rc
        phases_run.append("native")
        if rc == 2:
            print(_err("\n✗ Native phase infrastructure error — aborting."))
            return 2
    else:
        if not truth:
            print(_err("✗ --skip-native requires --truth <composite>"))
            return 1
        print(f"\n  {_dim('Skipping phase 1 — using truth composite:')} {truth[:32]}…")

    # ── Phase 2 ───────────────────────────────────────────────────────────────
    if not args.skip_docker:
        rc = phase_docker(truth, args)
        results["docker"] = rc
        phases_run.append("docker")
    else:
        print(f"\n  {_dim('Skipping phase 2 (docker)')}")

    # ── Phase 3 ───────────────────────────────────────────────────────────────
    if not args.skip_exe:
        rc = phase_exe(truth, args)
        results["exe"] = rc
        phases_run.append("exe")
    else:
        print(f"\n  {_dim('Skipping phase 3 (exe)')}")

    # ── Final verdict ─────────────────────────────────────────────────────────
    _head("Release Sanity Results")
    all_ok = True
    for phase, rc in results.items():
        if rc == 0:
            print(f"  {_ok('✓')} {phase:<10} PASSED")
        elif rc == 58:
            print(f"  {_warn('⚠')} {phase:<10} PASSED WITH WARNINGS")
        else:
            print(f"  {_err('✗')} {phase:<10} FAILED  (rc={rc})")
            all_ok = False

    if truth:
        print(f"\n  {_dim('source of truth composite:')} {_cyan(truth[:32])}…")

    print()
    if all_ok:
        print(_ok("✓ All builds verified — story content is consistent across builds."))
        if args.tag:
            if TAGGER.exists():
                print(f"\n  {_dim('Tagging version…')}")
                subprocess.run([sys.executable, "-X", "utf8", str(TAGGER),
                                "--base", args.base], cwd=ROOT)
            else:
                print(_warn(f"⚠ --tag requested but {TAGGER} not found"))
        return 0
    else:
        print(_err("✗ Build verification FAILED — composites diverge or sanity failed."))
        return 1


if __name__ == "__main__":
    sys.exit(main())
