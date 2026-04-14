#!/usr/bin/env python3
"""
test-player-portrait.py — Drives the player portrait + avatar pipeline end-to-end.

What this tests:
  1. Player describes appearance → POST /player-input
  2. Narrator turn contains [PLAYER_TRAIT(appearance):...] or [UPDATE_PLAYER:...]
  3. Diag generates SD prompt via Ollama and calls flask-sd
  4. SSE broadcasts meta{type:player_portrait, url:...}
  5. Image saved to test-output/ so we can inspect it visually

Usage:
    python -X utf8 scripts/test-player-portrait.py
"""
from __future__ import annotations
import base64, json, sys, time, threading, urllib.request, urllib.error
from pathlib import Path

BASE    = "http://localhost:1582"
DIAG    = "http://localhost:1591"
TIMEOUT = 180   # seconds to wait for portrait SSE

# A detailed self-description that should reliably trigger PLAYER_TRAIT/UPDATE_PLAYER.
APPEARANCE = (
    "I pause and take stock of myself. I am tall — six-two — with a lean, angular build. "
    "My hair is dark brown, almost black, cut short on the sides but longer on top, "
    "slightly unkempt from the journey. My eyes are a pale, storm-grey. "
    "There is a thin scar along my left jawline from an old dispute I'd rather forget. "
    "I'm wearing a worn leather duster, dark tactical trousers, and heavy boots. "
    "Underneath the coat, a close-fitting charcoal shirt. I look like someone who has "
    "been travelling hard and living lean."
)

OUT_DIR = Path("test-output")
OUT_DIR.mkdir(exist_ok=True)

portrait_event = threading.Event()
portrait_result: dict = {}

def _post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req  = urllib.request.Request(f"{BASE}{path}", data=data,
                                  headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def _sse_listen() -> None:
    """Connect to /game/events and watch for player_portrait meta event."""
    try:
        req = urllib.request.Request(f"{BASE}/game/events")
        with urllib.request.urlopen(req, timeout=TIMEOUT + 10) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line.startswith("data:"):
                    try:
                        payload = json.loads(line[5:].strip())
                    except Exception:
                        continue
                    # SSE wraps events as {"event": "...", "data": {...}}
                    # but EventSource also emits named events separately.
                    # Handle both patterns.
                    evt  = payload.get("event") or ""
                    data = payload.get("data") or payload
                    if evt == "meta" or data.get("type") == "player_portrait":
                        inner = data if "type" in data else (data.get("data") or {})
                        if inner.get("type") == "player_portrait":
                            portrait_result["url"]  = inner.get("url", "")
                            portrait_result["name"] = inner.get("name", "")
                            portrait_event.set()
                            return
    except Exception as e:
        print(f"  [sse error] {e}")

def _poll_sse_separately() -> None:
    """Alternative: poll the SSE stream for meta events the simple way."""
    url = f"{BASE}/game/events"
    try:
        req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
        conn = urllib.request.urlopen(req, timeout=TIMEOUT + 10)
        event_type = ""
        for raw_line in conn:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                try:
                    data = json.loads(line[5:].strip())
                except Exception:
                    continue
                if event_type == "meta" and data.get("type") == "player_portrait":
                    portrait_result["url"] = data.get("url", "")
                    portrait_event.set()
                    return
                event_type = ""
    except Exception as e:
        print(f"  [sse poll error] {e}")

def main() -> None:
    print("\n" + "="*60)
    print("  PLAYER PORTRAIT PIPELINE TEST")
    print("="*60 + "\n")

    # ── Health check ──
    print("Checking stack…")
    try:
        with urllib.request.urlopen(f"{BASE}/health", timeout=5) as r:
            h = json.loads(r.read())
        print(f"  gateway: {h.get('gateway')}  mode: {h.get('mode')}")
    except Exception as e:
        print(f"  ERROR: stack not reachable — {e}")
        sys.exit(1)

    try:
        with urllib.request.urlopen(f"{DIAG}/session-state", timeout=5) as r:
            ss = json.loads(r.read())
        print(f"  session: {ss.get('mode')}  turns: {ss.get('turns')}  "
              f"dressed: {ss.get('player_dressed')}")
    except Exception as e:
        print(f"  diag not reachable: {e}")
        sys.exit(1)

    # ── Start SSE listener in background ──
    print("\nOpening SSE listener…")
    t = threading.Thread(target=_poll_sse_separately, daemon=True)
    t.start()
    time.sleep(1)  # give it a moment to connect

    # ── Send appearance description ──
    print(f"\nSending appearance description ({len(APPEARANCE)} chars):")
    print(f"  \"{APPEARANCE[:120]}…\"")
    try:
        resp = _post("/player-input", {"text": APPEARANCE})
        print(f"  → accepted (turn_id: {resp.get('turn_id', '?')})")
    except Exception as e:
        print(f"  ERROR sending input: {e}")
        sys.exit(1)

    # ── Wait for narrator turn with tags ──
    print("\nWaiting for narrator turn…")
    deadline = time.time() + 90
    narrator_raw = ""
    while time.time() < deadline:
        time.sleep(3)
        try:
            with urllib.request.urlopen(
                f"{BASE}/diagnostics/narrator-turns?n=3", timeout=5
            ) as r:
                data = json.loads(r.read())
                turns = data.get("turns", data) if isinstance(data, dict) else data
            narrator_turns = [t for t in turns if not t.get("is_player")]
            if narrator_turns:
                narrator_raw = narrator_turns[-1].get("raw_text", "")
                if narrator_raw:
                    break
        except Exception:
            pass

    if narrator_raw:
        print("\nNarrator raw text (first 400 chars):")
        print(f"  {narrator_raw[:400]}")
        # Look for the tags we care about
        import re
        found_tags = []
        for tag in ["PLAYER_TRAIT", "UPDATE_PLAYER"]:
            if re.search(rf'\[{tag}', narrator_raw, re.IGNORECASE):
                found_tags.append(tag)
        if found_tags:
            print(f"\n  ✓ Found expected tags: {found_tags}")
        else:
            print(f"\n  ✗ No PLAYER_TRAIT or UPDATE_PLAYER tag found in narrator turn.")
            print("    Avatar generation will NOT trigger — check system prompt.")
    else:
        print("  ✗ No narrator turn received within 90s")

    # ── Wait for portrait SSE event ──
    print(f"\nWaiting up to {TIMEOUT}s for player_portrait SSE event…")
    got_portrait = portrait_event.wait(timeout=TIMEOUT)

    if not got_portrait:
        print("\n  ✗ TIMEOUT — no player_portrait event received.")
        print("    Check: diag logs, flask-sd reachability, Ollama idle state.")
        sys.exit(1)

    url = portrait_result.get("url", "")
    if not url:
        print("\n  ✗ player_portrait event received but URL was empty.")
        sys.exit(1)

    print(f"\n  ✓ player_portrait event received! URL length: {len(url)} chars")

    # ── Save image ──
    if url.startswith("data:image/"):
        header, b64data = url.split(",", 1)
        ext = "png" if "png" in header else "jpg"
        img_bytes = base64.b64decode(b64data)
        out = OUT_DIR / f"player-portrait-{int(time.time())}.{ext}"
        out.write_bytes(img_bytes)
        print(f"  ✓ Saved portrait to: {out}  ({len(img_bytes)//1024} KB)")
    elif url.startswith("/game/assets/"):
        print(f"  ✓ Static asset portrait: {url}")
    else:
        print(f"  ✓ Portrait URL: {url[:80]}")

    print("\n" + "="*60)
    print("  PASS — portrait pipeline working end-to-end")
    print("="*60)
    print("\nNext: hard-reload the game and check the player avatar slot")
    print("(top-left of each player turn row) shows the generated portrait.\n")

if __name__ == "__main__":
    main()
