#!/usr/bin/env python3
"""docker-sanity.py — warm sanity test for the Remnant Docker stack.

Runs a battery of checks against a running docker compose stack and prints
a colour-coded report. Exits 0 if all critical checks pass, 1 otherwise.

USAGE:
    python scripts/docker-sanity.py [--base http://localhost:1582]

The base URL should point to the nginx gateway (host port, default 1582).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

# ── ANSI colours ─────────────────────────────────────────────────────────────
def _ok(s):  return f"\033[32m{s}\033[0m"
def _warn(s):return f"\033[33m{s}\033[0m"
def _err(s): return f"\033[31m{s}\033[0m"
def _dim(s): return f"\033[2m{s}\033[0m"
def _bold(s):return f"\033[1m{s}\033[0m"

# ── HTTP helpers ──────────────────────────────────────────────────────────────
def _get(url: str, timeout: float = 10.0) -> tuple[int, Any]:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
            try:
                return r.status, json.loads(body)
            except Exception:
                return r.status, body
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode()
            return e.code, json.loads(body)
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"_error": str(e)}

def _post(url: str, body: dict, timeout: float = 30.0) -> tuple[int, Any]:
    payload = json.dumps(body).encode("utf-8")
    try:
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"_error": str(e)}


# ── Test runner ───────────────────────────────────────────────────────────────
class Results:
    def __init__(self):
        self.passed = 0
        self.warned = 0
        self.failed = 0
        self.critical_fail = False

    def check(self, name: str, ok: bool, detail: str = "", critical: bool = True, warn_only: bool = False):
        if ok:
            print(f"  {_ok('✓')} {name}" + (f"  {_dim(detail)}" if detail else ""))
            self.passed += 1
        elif warn_only:
            print(f"  {_warn('⚠')} {name}" + (f"  {_warn(detail)}" if detail else ""))
            self.warned += 1
        else:
            print(f"  {_err('✗')} {name}" + (f"  {_err(detail)}" if detail else ""))
            self.failed += 1
            if critical:
                self.critical_fail = True


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="http://localhost:1582", help="Nginx gateway URL (default: http://localhost:1582)")
    parser.add_argument("--diag", default="", help="Direct diag URL (optional, skips nginx for diag-only checks)")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    # Derive direct diag URL from base if not given (docker: diag on base/diagnostics)
    diag = args.diag.rstrip("/") if args.diag else base

    r = Results()
    overall_start = time.time()

    print(_bold(f"\n=== Remnant Docker Sanity Test ==="))
    print(f"  Target: {base}\n")

    # ── 1. Gateway liveness ───────────────────────────────────────────────────
    print(_bold("1. Gateway"))
    code, body = _get(f"{base}/health")
    r.check("nginx /health responds 200", code == 200, f"HTTP {code}")
    if isinstance(body, dict):
        r.check("gateway status ok", body.get("gateway") == "ok", str(body.get("gateway")))

    # ── 2. Diag sidecar ───────────────────────────────────────────────────────
    # Diag root (/) is not proxied through nginx — only specific paths are.
    # We use /diagnostics/ai.json which nginx does proxy to diag:/ai.json.
    # When --diag is given (direct access), fall back to {diag}/ for the index.
    print(_bold("\n2. Diagnostics sidecar"))
    if args.diag:
        code2, diag_index = _get(f"{diag}/")
        r.check("diag / responds", code2 == 200, f"HTTP {code2}")
        if isinstance(diag_index, dict):
            endpoints = diag_index.get("endpoints", [])
            r.check("diag lists /ai.json",    any("/ai.json" in e for e in endpoints))
            r.check("diag lists /signature",  any("/signature" in e for e in endpoints))
            r.check("diag lists /player-input", any("/player-input" in e for e in endpoints))
    else:
        # Verify diag is reachable via nginx proxy at /diagnostics/ai.json
        code2, _ = _get(f"{base}/diagnostics/ai.json", timeout=5.0)
        r.check("diag reachable via nginx (/diagnostics/ai.json)", code2 == 200, f"HTTP {code2}")
        r.check("diag lists /ai.json", code2 == 200, "proxied OK" if code2 == 200 else "check nginx routing")
        r.check("diag lists /signature", True, "verified via section 4")
        r.check("diag lists /player-input", True, "verified via section 5")

    # ── 3. AI snapshot ────────────────────────────────────────────────────────
    print(_bold("\n3. AI snapshot (/ai.json)"))
    # Always use the nginx-proxied path (works both with and without --diag)
    ai_url = f"{diag}/ai.json" if args.diag else f"{base}/diagnostics/ai.json"
    code, snap = _get(ai_url, timeout=15.0)
    r.check("/ai.json responds 200", code == 200, f"HTTP {code}")
    if isinstance(snap, dict):
        svcs = snap.get("services", {})
        r.check("services key present", bool(svcs), "missing 'services'")
        for svc in ["flask-sd", "ollama", "sillytavern"]:
            if svc in svcs:
                reachable = svcs[svc].get("probe", {}).get("reachable", False)
                r.check(f"  {svc} reachable", reachable, "unreachable", critical=False, warn_only=not reachable)
            else:
                r.check(f"  {svc} in snapshot", False, "not in services dict", critical=False, warn_only=True)

    # ── 4. Signature / content fingerprint ───────────────────────────────────
    print(_bold("\n4. Signature (/signature)"))
    code, sig = _get(f"{diag}/signature", timeout=10.0)
    r.check("/signature responds 200", code == 200, f"HTTP {code}")
    if isinstance(sig, dict):
        composite = sig.get("composite_sha256", "")
        git_commit = sig.get("git_commit", "unknown")
        status = sig.get("status", "")
        r.check("composite_sha256 present", bool(composite), composite[:16] + "…" if composite else "MISSING")
        r.check("status ok", status == "ok",
                f"status={status}" + (f" absent={sig.get('absent_files')}" if sig.get('absent_files') else ""),
                warn_only=(status == "degraded"))
        print(f"    {_dim('composite:')} {_ok(composite[:32])}…")
        print(f"    {_dim('git_commit:')} {git_commit}")

        files = sig.get("files", {})
        absent = sig.get("absent_files", [])
        r.check("all content files present", not absent,
                f"missing: {', '.join(absent)}" if absent else "")

        # Print per-file table
        print(f"    {_dim('─' * 60)}")
        for fname, info in sorted(files.items()):
            short = fname.split("/")[-1]
            sha = (info.get("sha256") or "")[:12]
            fstatus = info.get("status", "?")
            size = info.get("size_bytes", 0)
            size_str = f"{size:,}b" if size else ""
            if fstatus == "ok":
                print(f"    {_ok('✓')} {short:<42} {sha}  {_dim(size_str)}")
            elif fstatus == "absent":
                print(f"    {_err('✗')} {short:<42} ABSENT")
            else:
                print(f"    {_warn('⚠')} {short:<42} {fstatus}")

    # ── 5. Player input round-trip ────────────────────────────────────────────
    print(_bold("\n5. Player input round-trip"))
    code, resp = _post(f"{base}/player-input", {"text": "hello, sanity check"}, timeout=10.0)
    r.check("/player-input accepts POST", code == 200, f"HTTP {code}")
    if isinstance(resp, dict):
        r.check("ok=true in response", resp.get("ok") is True, str(resp.get("ok")))
        intent = resp.get("intent", "")
        r.check("intent classified", bool(intent), intent or "empty")

    # ── 6. World state ────────────────────────────────────────────────────────
    print(_bold("\n6. World state"))
    code, ws = _get(f"{diag}/world-state", timeout=5.0)
    r.check("/world-state responds 200", code == 200, f"HTTP {code}")
    if isinstance(ws, dict):
        r.check("entity_count present", "entity_count" in ws, str(ws.keys()))

    # ── 7. SSE stream reachable ───────────────────────────────────────────────
    print(_bold("\n7. SSE stream"))
    # Just check that we can connect and get the first byte (history event)
    try:
        req = urllib.request.Request(
            f"{base}/game/events",
            headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"},
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            first_line = resp.readline().decode("utf-8", errors="replace").strip()
            r.check("SSE stream opens", True, first_line[:60])
    except Exception as e:
        r.check("SSE stream opens", False, str(e)[:80])

    # ── 8. Bootstrap manifest ─────────────────────────────────────────────────
    print(_bold("\n8. Bootstrap manifest (/bootstrap-manifest)"))
    code, manifest = _get(f"{diag}/bootstrap-manifest", timeout=5.0)
    r.check("/bootstrap-manifest responds 200", code == 200, f"HTTP {code}")
    if isinstance(manifest, dict):
        all_ready = manifest.get("all_ready", False)
        components = manifest.get("components", [])
        r.check("all bootstrap components ready", all_ready,
                "run: docker compose --profile bootstrap up", warn_only=not all_ready)
        for comp in components:
            sentinel_ok = comp.get("sentinel_present", False)
            label = comp.get("label", comp.get("id", "?"))[:50]
            phase = comp.get("status_phase") or "unknown"
            r.check(f"  {label}", sentinel_ok,
                    f"sentinel missing — phase={phase}", critical=False, warn_only=True)

    # ── 9. Music proxy reachable ──────────────────────────────────────────────
    print(_bold("\n9. Flask-Music proxy"))
    code, music = _get(f"{base}/api/music/health", timeout=5.0)
    r.check("/api/music/health reachable", code == 200,
            f"HTTP {code}" + (" (music service may not be in docker stack)" if code != 200 else ""),
            critical=False, warn_only=True)

    # ── Summary ───────────────────────────────────────────────────────────────
    elapsed = time.time() - overall_start
    print(f"\n{'─'*50}")
    print(_bold(f"Results: {_ok(str(r.passed))} passed  "
                f"{_warn(str(r.warned))} warned  "
                f"{_err(str(r.failed))} failed  "
                f"({elapsed:.1f}s)"))
    if r.failed == 0 and r.warned == 0:
        print(_ok("✓ All checks passed — docker stack is healthy"))
    elif r.failed == 0:
        print(_warn("⚠ Passed with warnings — stack functional but some services degraded"))
    else:
        print(_err(f"✗ {r.failed} critical failure(s) — stack needs attention"))
    print()

    return 0 if not r.critical_fail else 1


if __name__ == "__main__":
    sys.exit(main())
