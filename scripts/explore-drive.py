#!/usr/bin/env python3
"""
explore-drive.py — Drive the game through location exploration + NPC conversations.
Focused on triggering character portraits and speech bubbles.

Usage:
    python -X utf8 scripts/explore-drive.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
import urllib.error

BASE = "http://localhost:1582"

# Sequence of player actions designed to:
#   1. Establish identity (name + appearance → avatar generation)
#   2. Meet The Remnant (opening ritual)
#   3. Find clothes with Sherri (new location + character)
#   4. Explore further locations
#   5. Have real conversations with named NPCs
ACTIONS = [
    # Opening — player arrives, The Remnant asks who they are
    ("I am Kael. A traveller from the outer fold.", 35),

    # Dress / appearance (triggers PLAYER_TRAIT avatar generation)
    ("I look around the chamber. I'm tall, dark-haired, wearing a worn leather duster.", 35),

    # Ask about the station
    ("What is this place? Where am I?", 35),

    # Move — request to find people / explore
    ("I want to find other people on this station. Is there anyone else here?", 40),

    # Meet Sherri — fabrication bay
    ("I head toward the fabrication bay to find Sherri.", 40),

    # Greet Sherri
    ("Hello. Are you Sherri? Can you help me get some proper clothes for this place?", 45),

    # Continue conversation with Sherri
    ("What can you tell me about The Fortress? How long have you been here?", 40),

    # Ask about the station's history
    ("What happened here? Why is everyone gone?", 40),

    # Ask Sherri to show me around
    ("Can you take me to see more of the station? I want to understand this place.", 40),

    # Move to a new location
    ("I follow Sherri deeper into the station. Where does this corridor lead?", 40),

    # Explore — the Nexus / Remnant's domain
    ("I want to speak with The Remnant again. Can we go to the Nexus?", 45),

    # Ask The Remnant about the Fold
    ("What is the Fold? How does it connect to this place?", 40),

    # Ask about danger / threat
    ("Is there anything dangerous aboard The Fortress right now?", 40),

    # Close conversation — ask about what to do next
    ("What should I do first to understand my situation?", 35),
]


def _send_input(text: str) -> dict:
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}/player-input",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _latest_turns(n: int = 2) -> list[dict]:
    url = f"{BASE}/diagnostics/narrator-turns?n={n}"
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.loads(r.read())
    return data if isinstance(data, list) else data.get("turns", [])


def _wait_for_narrator(after_player_seq: int, timeout: int = 90) -> str | None:
    """Poll until a new narrator turn appears after the player's turn."""
    deadline = time.time() + timeout
    last_seen = after_player_seq
    while time.time() < deadline:
        time.sleep(2)
        try:
            turns = _latest_turns(4)
            for t in reversed(turns):
                if not t.get("is_player") and t.get("raw_text", "").strip():
                    # Check if this is newer than what we already saw
                    seq = t.get("turn_id", "")
                    if seq != last_seen:
                        return t.get("raw_text", "").strip()
            # Fallback: if the last turn is narrator and we sent input, return it
            narrator_turns = [t for t in turns if not t.get("is_player")]
            if narrator_turns:
                raw = narrator_turns[-1].get("raw_text", "").strip()
                if raw:
                    last_seen = narrator_turns[-1].get("turn_id", "")
        except Exception as e:
            print(f"  [poll error: {e}]")
    return None


def _summarise(raw: str) -> str:
    """Print a short readable summary of the narrator turn."""
    lines = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("[CHARACTER("):
            # Extract speaker and speech
            import re
            m = re.match(r'\[CHARACTER\(([^)]+)\)\s*:\s*"([^"]+)"\]', line)
            if m:
                lines.append(f"  💬 {m.group(1)}: \"{m.group(2)[:120]}\"")
        elif line.startswith("[INTRODUCE("):
            import re
            m = re.match(r'\[INTRODUCE\(([^)]+)\)', line)
            if m:
                lines.append(f"  👤 NEW NPC: {m.group(1)}")
        elif line.startswith("[GENERATE_IMAGE"):
            lines.append(f"  🖼  [image generating]")
        elif line.startswith("[MOOD"):
            import re
            m = re.match(r'\[MOOD\s*:\s*"([^"]+)"\]', line)
            if m:
                lines.append(f"  🎵 mood: {m.group(1)[:60]}")
        elif line.startswith("["):
            pass  # skip other tags
        else:
            lines.append(f"  {line[:140]}")
        if len(lines) >= 8:
            lines.append("  …")
            break
    return "\n".join(lines)


def main() -> None:
    print(f"\n{'='*60}")
    print("  REMNANT — Exploration & Character Conversation Driver")
    print(f"{'='*60}\n")

    # Wait for diag to be ready
    print("Waiting for stack…")
    for _ in range(20):
        try:
            with urllib.request.urlopen(f"{BASE}/health", timeout=3) as r:
                if r.status == 200:
                    break
        except Exception:
            pass
        time.sleep(1)
    else:
        print("ERROR: Stack not reachable at", BASE)
        sys.exit(1)

    session = json.loads(urllib.request.urlopen(f"{BASE}/session-state", timeout=5).read())
    print(f"Session: {session['mode']} | turns: {session['turns']} | location: {session.get('last_location','?')}\n")

    # Track last narrator turn_id to detect new turns
    last_narrator_id = None
    try:
        turns = _latest_turns(1)
        narrator = [t for t in turns if not t.get("is_player")]
        if narrator:
            last_narrator_id = narrator[-1].get("turn_id")
    except Exception:
        pass

    consecutive_timeouts = 0
    total_timeouts = 0

    for i, (action, wait_secs) in enumerate(ACTIONS, 1):
        # Stop sending inputs if we have more than 2 pending unanswered turns
        # (Ollama can only generate one response at a time; flooding it makes all turns stall)
        if consecutive_timeouts >= 2:
            backoff = min(consecutive_timeouts * 15, 90)
            print(f"\n  ⚠  {consecutive_timeouts} consecutive timeouts — pausing {backoff}s for LLM to catch up…")
            time.sleep(backoff)
            consecutive_timeouts = 0  # reset after backoff

        print(f"\n[{i:02d}/{len(ACTIONS)}] PLAYER: {action[:80]}")
        print(f"       (waiting up to {wait_secs}s for narrator…)")

        try:
            _send_input(action)
        except Exception as e:
            print(f"  ERROR sending input: {e}")
            continue

        # Wait for the narrator to respond
        deadline = time.time() + wait_secs
        narrator_text = None
        prev_id = last_narrator_id
        while time.time() < deadline:
            time.sleep(3)
            try:
                turns = _latest_turns(3)
                narrator_turns = [t for t in turns if not t.get("is_player") and t.get("raw_text", "").strip()]
                if narrator_turns:
                    newest = narrator_turns[-1]
                    nid = newest.get("turn_id")
                    if nid != prev_id:
                        narrator_text = newest.get("raw_text", "").strip()
                        last_narrator_id = nid
                        break
            except Exception as e:
                print(f"  [poll error: {e}]")

        if narrator_text:
            print(f"\n  NARRATOR:")
            print(_summarise(narrator_text))
            consecutive_timeouts = 0
        else:
            print(f"  (no narrator response within {wait_secs}s)")
            consecutive_timeouts += 1
            total_timeouts += 1

        # Pause between actions so TTS can play and LLM isn't flooded.
        # Longer pause after a timeout to let Ollama finish the in-flight request.
        pause = 8 if narrator_text else 15
        time.sleep(pause)

    print(f"\n{'='*60}")
    print("  Drive complete.")
    print(f"{'='*60}\n")

    # Final session state
    try:
        session = json.loads(urllib.request.urlopen(f"{BASE}/session-state", timeout=5).read())
        print(f"Final session: {session['turns']} turns | NPCs met: {session.get('npcs_met', [])} | location: {session.get('last_location','?')}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
