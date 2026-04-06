#!/usr/bin/env python3
"""Timed warm-test driver for the Remnant docker stack.

Workflow:
  1. docker compose build sillytavern  (timed)
  2. Tear down old container + volume, bring stack back up
  3. Poll /diagnostics/ai.json until HEALTHY or timeout (timed)
  4. Launch Playwright headless, boot :1582, read consoleCounts() (timed)
  5. POST browser health report to sidecar
  6. Print JSON timing report

Usage:
  python scripts/warm_test.py [--no-build] [--timeout 180]

Flags:
  --no-build   Skip the docker build step (useful when testing the polling)
  --timeout N  HEALTHY poll timeout in seconds (default 180)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request


DIAG_URL = "http://localhost:1582/diagnostics/ai.json"
BROWSER_HEALTH_URL = "http://localhost:1582/diagnostics/browser-health"
ST_URL = "http://localhost:1582"


def _fetch_json(url: str, timeout: float = 5.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _post_json(url: str, payload: dict, timeout: float = 5.0) -> bool:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:
        return False


def _run(cmd: list[str], label: str) -> float:
    """Run a shell command, stream output, return elapsed seconds."""
    print(f"\n[warm_test] {label}")
    t0 = time.time()
    proc = subprocess.run(cmd, check=True)
    elapsed = time.time() - t0
    print(f"[warm_test] {label} done in {elapsed:.1f}s")
    return elapsed


def _poll_healthy(timeout_s: float) -> tuple[bool, float]:
    """Poll DIAG_URL until summary starts with HEALTHY. Return (ok, elapsed_s)."""
    t0 = time.time()
    dots = 0
    while True:
        elapsed = time.time() - t0
        if elapsed >= timeout_s:
            print()
            return False, elapsed
        data = _fetch_json(DIAG_URL)
        if data and str(data.get("summary", "")).startswith("HEALTHY"):
            print()
            return True, time.time() - t0
        print(".", end="", flush=True)
        dots += 1
        if dots % 60 == 0:
            print(f"  {elapsed:.0f}s")
        time.sleep(2)


def _playwright_boot() -> tuple[int, int, float]:
    """Boot :1582 in a headless browser, wait for ready(), return (errors, warnings, elapsed_s)."""
    t0 = time.time()
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        print("[warm_test] playwright not installed — skipping browser check (pip install playwright)")
        return -1, -1, 0.0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        console_errors = 0
        console_warnings = 0

        def on_console(msg):
            nonlocal console_errors, console_warnings
            if msg.type == "error":
                console_errors += 1
            elif msg.type == "warning":
                console_warnings += 1

        page.on("console", on_console)

        try:
            page.goto(ST_URL, timeout=30_000, wait_until="networkidle")
            # Wait for extension boot gate
            page.wait_for_function(
                "typeof window.__remnantTest !== 'undefined'",
                timeout=20_000,
            )
            page.wait_for_function(
                "window.__remnantTest.ready()",
                timeout=20_000,
            )
            # Extra settle for async errors
            page.wait_for_timeout(1500)
            # Read counts from the extension's own tracker (may differ from
            # raw on-console handler above if extension loads after some errors)
            counts = page.evaluate("window.__remnantTest.consoleCounts()")
            if counts:
                console_errors = counts.get("errors", console_errors)
                console_warnings = counts.get("warnings", console_warnings)
        except Exception as e:
            print(f"\n[warm_test] Playwright error: {e}")
        finally:
            browser.close()

    elapsed = time.time() - t0
    return console_errors, console_warnings, elapsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-build", action="store_true",
                        help="Skip docker compose build")
    parser.add_argument("--timeout", type=int, default=180,
                        help="Seconds to wait for HEALTHY (default 180)")
    args = parser.parse_args()

    report: dict = {}
    t_total_start = time.time()

    # Step 1: build (sillytavern + diag + nginx all track code in this repo)
    build_s = 0.0
    if not args.no_build:
        try:
            build_s = _run(
                ["docker", "compose", "build", "sillytavern", "diag", "nginx"],
                "docker compose build sillytavern diag nginx",
            )
        except subprocess.CalledProcessError as e:
            print(f"[warm_test] build failed: {e}")
            sys.exit(1)

    # Step 2: teardown + restart
    # Restart nginx + diag (may have new config/code), nuke the ST data volume
    # for a truly clean first-boot seed, then bring everything up.
    print("\n[warm_test] Tearing down sillytavern, refreshing nginx + diag...")
    try:
        subprocess.run(["docker", "compose", "stop", "sillytavern"], check=True)
        subprocess.run(["docker", "compose", "rm", "-f", "sillytavern"], check=True)
        subprocess.run(["docker", "volume", "rm", "remnant-silly_sillytavern-data"],
                       check=False)  # ok if volume didn't exist
        subprocess.run(["docker", "compose", "up", "-d", "sillytavern", "diag", "nginx"], check=True)
    except subprocess.CalledProcessError as e:
        print(f"[warm_test] container restart failed: {e}")
        sys.exit(1)

    # Step 3: poll healthy
    print(f"\n[warm_test] Polling {DIAG_URL} for HEALTHY (timeout {args.timeout}s)...")
    ok, boot_s = _poll_healthy(args.timeout)
    if not ok:
        print(f"[warm_test] TIMED OUT after {args.timeout}s — stack did not reach HEALTHY")
        report = {
            "build_s": round(build_s, 1),
            "boot_to_healthy_s": None,
            "console_errors": None,
            "console_warnings": None,
            "total_s": round(time.time() - t_total_start, 1),
            "passed": False,
            "failure": "healthy_timeout",
        }
        print(json.dumps(report, indent=2))
        sys.exit(1)

    print(f"[warm_test] HEALTHY in {boot_s:.1f}s")

    # Step 4: Playwright console check
    print("\n[warm_test] Booting browser, checking console health...")
    errors, warnings, browser_s = _playwright_boot()

    # Step 5: POST health to sidecar
    if errors >= 0:
        posted = _post_json(BROWSER_HEALTH_URL,
                            {"errors": errors, "warnings": warnings})
        print(f"[warm_test] Posted browser-health to sidecar: {'ok' if posted else 'failed (sidecar may be down)'}")

    # Step 6: report
    total_s = time.time() - t_total_start
    passed = (errors == 0) if errors >= 0 else None

    report = {
        "build_s": round(build_s, 1),
        "boot_to_healthy_s": round(boot_s, 1),
        "browser_check_s": round(browser_s, 1),
        "console_errors": errors,
        "console_warnings": warnings,
        "total_s": round(total_s, 1),
        "passed": passed,
    }

    print("\n" + "=" * 60)
    print("[warm_test] RESULT")
    print("=" * 60)
    print(json.dumps(report, indent=2))

    if not passed and errors >= 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
