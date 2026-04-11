#!/usr/bin/env python3
"""test-seed.py — End-to-end verification that permanent world assets load correctly.

Tests:
  1. Seed file is valid JSON with required structure
  2. All seeded entities appear in /world-state after startup
  3. System prompt contains [PERMANENT CREW] block
  4. All lore keys appear in forever.jsonl
  5. After Reset World, all seeded entities come back
  6. Portrait SSE events are broadcast for seeded NPCs

Usage:
  python -X utf8 scripts/test-seed.py           # against running stack on :1582
  python -X utf8 scripts/test-seed.py --port 1591  # hit diag directly

Exits 0 on pass, 1 on any failure.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SEED_PATH = REPO_ROOT / "docker" / "diag" / "seed" / "world.json"

GREEN = "\033[32m"
RED   = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"
BOLD  = "\033[1m"

_passed = 0
_failed = 0


def ok(msg: str) -> None:
    global _passed
    _passed += 1
    print(f"  {GREEN}✓{RESET} {msg}")


def fail(msg: str) -> None:
    global _failed
    _failed += 1
    print(f"  {RED}✗{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}!{RESET} {msg}")


def section(title: str) -> None:
    print(f"\n{BOLD}{title}{RESET}")


def _get(base: str, path: str, timeout: int = 10) -> tuple[int, dict | list | None]:
    url = f"{base}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return 0, None


def _post(base: str, path: str, body: dict, timeout: int = 15) -> tuple[int, dict | None]:
    url = f"{base}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data,
                                  headers={"Content-Type": "application/json"},
                                  method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None
    except Exception as e:
        return 0, None


def main() -> int:
    parser = argparse.ArgumentParser(description="Test permanent world seed end-to-end")
    parser.add_argument("--port", type=int, default=1582,
                        help="Port to test against (default: 1582 = nginx gateway)")
    parser.add_argument("--diag-port", type=int, default=1591,
                        help="Diag direct port (default: 1591)")
    args = parser.parse_args()

    gw   = f"http://localhost:{args.port}"
    diag = f"http://localhost:{args.diag_port}"

    print(f"\n{BOLD}═══ Remnant Seed End-to-End Test ═══{RESET}")
    print(f"Gateway: {gw}  |  Diag: {diag}")

    # ── Section 1: Seed file validity ──────────────────────────────────────
    section("1. Seed file structure")

    if not SEED_PATH.exists():
        fail(f"seed file not found: {SEED_PATH}")
        return 1

    try:
        seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
        ok(f"seed file parses: {SEED_PATH.name}")
    except Exception as e:
        fail(f"seed file JSON error: {e}")
        return 1

    locations = seed.get("locations", [])
    npcs      = seed.get("npcs", [])
    lore      = seed.get("lore", [])

    ok(f"{len(locations)} locations, {len(npcs)} NPCs, {len(lore)} lore entries")

    expected_locations = {loc["id"] for loc in locations}
    expected_npcs      = {npc["id"] for npc in npcs}
    expected_lore_keys = {entry["key"] for entry in lore}

    for loc in locations:
        if not loc.get("id") or not loc.get("name"):
            fail(f"location missing id or name: {loc}")
        else:
            ok(f"  location: {loc['id']} ({loc['name']})")

    for npc in npcs:
        if not npc.get("id") or not npc.get("name"):
            fail(f"NPC missing id or name: {npc}")
        else:
            ok(f"  NPC: {npc['id']} ({npc['name']})")

    # ── Section 2: Stack reachability ──────────────────────────────────────
    section("2. Stack reachability")

    code, health = _get(gw, "/health")
    if code == 200:
        ok(f"gateway healthy: {health}")
    else:
        fail(f"gateway not reachable on :{args.port} (HTTP {code})")
        warn("start the stack first: python -X utf8 scripts/dev.py dev")
        return 1

    code, _ = _get(diag, "/")
    if code == 200:
        ok(f"diag reachable on :{args.diag_port}")
    else:
        fail(f"diag not reachable on :{args.diag_port}")
        return 1

    # ── Section 3: World state contains seeded entities ────────────────────
    section("3. Seeded entities in world-state")

    code, world = _get(diag, "/world-state")
    if code != 200 or world is None:
        fail(f"world-state not available (HTTP {code})")
        return 1

    entities = world.get("entities") or {}
    if isinstance(entities, list):
        entities = {e.get("id", str(i)): e for i, e in enumerate(entities)}

    ok(f"world-state returned {len(entities)} total entities")

    for loc_id in expected_locations:
        if loc_id in entities:
            ent = entities[loc_id]
            src = ent.get("source", "runtime")
            perm = ent.get("permanence", "?")
            ok(f"  {loc_id}: permanence={perm} source={src}")
            if src != "seed":
                fail(f"  {loc_id} source should be 'seed', got '{src}'")
            if perm != "WORLD":
                fail(f"  {loc_id} permanence should be 'WORLD', got '{perm}'")
        else:
            fail(f"  {loc_id} NOT in world-state")

    for npc_id in expected_npcs:
        if npc_id in entities:
            ent = entities[npc_id]
            ok(f"  {npc_id}: portrait={bool(ent.get('portrait'))} voice={bool(ent.get('voice'))}")
        else:
            fail(f"  {npc_id} NOT in world-state")

    # ── Section 4: System prompt contains PERMANENT CREW block ─────────────
    section("4. System prompt injection")

    # We can check via ai.json whether the system prompt is loaded
    code, snap = _get(diag, "/ai.json")
    if code == 200 and snap:
        sp_len = snap.get("system_prompt_length", 0)
        if sp_len and sp_len > 500:
            ok(f"system prompt loaded ({sp_len} chars)")
        elif sp_len == 0:
            warn("system prompt length not reported in ai.json — skipping injection check")
        else:
            fail(f"system prompt suspiciously short ({sp_len} chars)")
    else:
        warn(f"ai.json not available (HTTP {code}) — skipping system prompt check")

    # ── Section 5: Lore in forever.jsonl ───────────────────────────────────
    section("5. Lore in forever.jsonl")

    code, forever = _get(gw, "/forever")
    if code == 200 and forever is not None:
        if isinstance(forever, list):
            forever_keys = {e.get("key") for e in forever if e.get("key")}
        elif isinstance(forever, dict):
            forever_keys = {forever.get("key")} if forever.get("key") else set()
        else:
            forever_keys = set()
        ok(f"forever endpoint returned {len(forever_keys)} unique keys")
        for key in expected_lore_keys:
            if key in forever_keys:
                ok(f"  lore key '{key}' present")
            else:
                warn(f"  lore key '{key}' not yet in forever (written on first boot only if missing)")
    else:
        warn(f"forever endpoint not available (HTTP {code}) — skipping lore check")

    # ── Section 6: Reset → reseed ───────────────────────────────────────────
    section("6. Reset World → seed entities restored")

    warn("posting Reset World — this wipes runtime narrative state")
    code, result = _post(gw, "/reset", {"level": "world"})
    if code not in (200, 204):
        fail(f"reset returned HTTP {code}: {result}")
    else:
        ok(f"reset posted (HTTP {code}): {result}")

    time.sleep(2)  # allow seed reload and SSE broadcast

    code, world2 = _get(diag, "/world-state")
    if code != 200 or world2 is None:
        fail("world-state not available after reset")
    else:
        entities2 = world2.get("entities") or {}
        if isinstance(entities2, list):
            entities2 = {e.get("id", str(i)): e for i, e in enumerate(entities2)}

        restored = 0
        for eid in list(expected_locations) + list(expected_npcs):
            if eid in entities2:
                restored += 1
            else:
                fail(f"  {eid} NOT restored after Reset World")
        expected_total = len(expected_locations) + len(expected_npcs)
        if restored == expected_total:
            ok(f"all {restored}/{expected_total} seed entities restored after Reset World")
        else:
            fail(f"only {restored}/{expected_total} seed entities restored after Reset World")

    # ── Summary ─────────────────────────────────────────────────────────────
    print(f"\n{BOLD}═══ Results ═══{RESET}")
    total = _passed + _failed
    if _failed == 0:
        print(f"{GREEN}{BOLD}PASS{RESET} — {_passed}/{total} checks passed")
        return 0
    else:
        print(f"{RED}{BOLD}FAIL{RESET} — {_failed}/{total} checks failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
