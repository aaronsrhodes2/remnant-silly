#!/usr/bin/env python3
"""story-test.py — 100-turn narrative arc test for Remnant.

Drives the game through a complete story arc:
  portal arrival → name → outfit → galley → sleep → Nexus → quest

Measures story richness at every beat (images, moods, SFX, senses, character
lines, lore, items, NPC introductions, new world entities) and saves a
Ollama-summarised story excerpt to tests/story-results/.

USAGE:
    python -X utf8 scripts/story-test.py
    python -X utf8 scripts/story-test.py --player-name "Wren" --max-turns 80
    python -X utf8 scripts/story-test.py --skip-reset
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPTS_DIR       = Path(__file__).parent
ROOT_DIR          = SCRIPTS_DIR.parent
STORY_RESULTS_DIR = ROOT_DIR / "tests" / "story-results"

# ── ANSI colours ──────────────────────────────────────────────────────────────
def _ok(s):    return f"\033[32m{s}\033[0m"
def _warn(s):  return f"\033[33m{s}\033[0m"
def _err(s):   return f"\033[31m{s}\033[0m"
def _dim(s):   return f"\033[2m{s}\033[0m"
def _bold(s):  return f"\033[1m{s}\033[0m"
def _cyan(s):  return f"\033[36m{s}\033[0m"
def _gold(s):  return f"\033[93m{s}\033[0m"

# ── HTTP helpers (copied from docker-sanity.py) ────────────────────────────────
def _get(url: str, timeout: float = 15.0) -> tuple[int, Any]:
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


# ── Exceptions ────────────────────────────────────────────────────────────────
class StepFailed(Exception):
    pass


# ── Shared state ──────────────────────────────────────────────────────────────
metrics: dict[str, Any] = {
    "images":           0,
    "moods":            0,
    "sfx":              0,
    "smell":            0,
    "taste":            0,
    "touch":            0,
    "environment":      0,
    "character_lines":  0,
    "lore_tags":        0,
    "item_tags":        0,
    "player_trait_tags":0,
    "update_player":    0,
    "introduce_tags":   0,
    "lore_whispers":    0,
    "locations_visited":set(),
    "npcs_met":         set(),
    "npcs_created":     set(),
    "items_acquired":   [],
    "lore_collected":   [],
    "narrator_turns":   0,
    "player_turns":     0,
    "warnings":         [],
}

soft_results: list[tuple[str, bool, str]] = []
story_text:   list[str] = []
seen_ids:     set[str]  = set()

BASE_URL     = "http://localhost:1582"
DIAG_URL     = "http://localhost:1591"
TURN_TIMEOUT = 150  # 150s — generous for 16K-context mistral turns
MAX_TURNS    = 100

# ── Tag extraction regexes ────────────────────────────────────────────────────
_RE_IMAGE    = re.compile(r'\[GENERATE_IMAGE')
_RE_MOOD     = re.compile(r'\[MOOD\(')
_RE_SOUND    = re.compile(r'\[SOUND\(')
_RE_SMELL    = re.compile(r'\[SMELL\(')
_RE_TASTE    = re.compile(r'\[TASTE\(')
_RE_TOUCH    = re.compile(r'\[TOUCH\(')
_RE_ENV      = re.compile(r'\[ENVIRONMENT\(')
_RE_CHAR     = re.compile(r'\[CHARACTER\(([^)]+)\)\]')
_RE_LORE     = re.compile(r'\[LORE\(([^)]+)\)\]')
_RE_ITEM     = re.compile(r'\[ITEM\(([^)]+)\)\]')
_RE_TRAIT    = re.compile(r'\[PLAYER_TRAIT\(')
_RE_UPDATE   = re.compile(r'\[UPDATE_PLAYER\]')
_RE_INTRO    = re.compile(r'\[INTRODUCE\(([^)]+)\)\]')
_RE_WARN     = re.compile(r'(?i)(feel free to|as an ai|which path would|how can i help|let me know if|great question)', re.I)

# Strip all system tags from prose
_RE_STRIP_TAGS = re.compile(
    r'\[(?:GENERATE_IMAGE|MOOD|SOUND|SMELL|TASTE|TOUCH|ENVIRONMENT|CHARACTER|'
    r'LORE|ITEM|PLAYER_TRAIT|UPDATE_PLAYER|INTRODUCE|SCENE_IMAGE|SFX)\([^)]*\)\]'
    r'|\[(?:GENERATE_IMAGE|UPDATE_PLAYER)\]',
    re.I
)


def _clean_for_story(raw: str) -> str:
    """Strip system tags and collapse whitespace for clean story prose."""
    text = _RE_STRIP_TAGS.sub("", raw)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _ingest_turn(turn: dict) -> None:
    """Extract metrics from a narrator turn and accumulate."""
    raw = turn.get("raw_text", "")

    metrics["images"]           += len(_RE_IMAGE.findall(raw))
    metrics["moods"]            += len(_RE_MOOD.findall(raw))
    metrics["sfx"]              += len(_RE_SOUND.findall(raw))
    metrics["smell"]            += len(_RE_SMELL.findall(raw))
    metrics["taste"]            += len(_RE_TASTE.findall(raw))
    metrics["touch"]            += len(_RE_TOUCH.findall(raw))
    metrics["environment"]      += len(_RE_ENV.findall(raw))
    metrics["player_trait_tags"]+= len(_RE_TRAIT.findall(raw))
    metrics["update_player"]    += len(_RE_UPDATE.findall(raw))

    chars = _RE_CHAR.findall(raw)
    metrics["character_lines"]  += len(chars)

    lore_matches = _RE_LORE.findall(raw)
    metrics["lore_tags"]        += len(lore_matches)
    for k in lore_matches:
        if k not in metrics["lore_collected"]:
            metrics["lore_collected"].append(k)

    item_matches = _RE_ITEM.findall(raw)
    metrics["item_tags"]        += len(item_matches)
    for k in item_matches:
        if k not in metrics["items_acquired"]:
            metrics["items_acquired"].append(k)

    intro_matches = _RE_INTRO.findall(raw)
    metrics["introduce_tags"]   += len(intro_matches)
    for name in intro_matches:
        metrics["npcs_met"].add(name.strip())

    # Detect out-of-character warnings
    if _RE_WARN.search(raw):
        metrics["warnings"].append(f"OOC in turn {turn.get('id','?')}: {raw[:80]}")

    metrics["narrator_turns"]   += 1

    clean = _clean_for_story(raw)
    if clean:
        story_text.append(clean)


def _snap_world_entities(base: str) -> dict:
    """Return entities as a {id: entity} dict from /world-state."""
    code, data = _get(f"{base}/world-state", timeout=10.0)
    if code != 200 or not isinstance(data, dict):
        return {}
    raw = data.get("entities", [])
    if isinstance(raw, list):
        return {e["id"]: e for e in raw if "id" in e}
    return raw  # already a dict


def _update_npcs_created(current_entities: dict, baseline_ids: set) -> None:
    """Diff current entities against baseline; add new NPCs to metrics."""
    for eid, ent in current_entities.items():
        if eid not in baseline_ids and eid != "__player__":
            if isinstance(ent, dict) and ent.get("type") in ("npc", "character", "person"):
                metrics["npcs_created"].add(eid)


def assert_soft(condition: bool, label: str, detail: str = "") -> None:
    soft_results.append((label, bool(condition), detail))


def assert_hard(condition: bool, message: str) -> None:
    if not condition:
        raise StepFailed(message)


# ── Narrator turn polling ─────────────────────────────────────────────────────
def _wait_for_narrator_turn(base: str, timeout: float = TURN_TIMEOUT) -> dict:
    """Poll narrator-turns endpoint until a new NARRATOR turn (not in seen_ids) appears.

    The endpoint returns both player and narrator turns.  We must filter for
    narrator-only (is_player == False) so we don't mistake the player's own
    just-posted input for the narrator's response.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, data = _get(f"{base}/diagnostics/narrator-turns?n=50", timeout=10.0)
        if code == 200 and isinstance(data, dict):
            turns = data.get("turns", [])
            new = [
                t for t in turns
                if str(t.get("turn_id", "")) not in seen_ids
                and not t.get("is_player", False)
            ]
            if new:
                latest = new[-1]
                seen_ids.add(str(latest.get("turn_id", "")))
                return latest
        time.sleep(2.0)
    raise StepFailed(f"Timed out after {timeout}s waiting for narrator turn")


# ── SSE background listener (lore_whisper events) ─────────────────────────────
def _sse_listener(base: str) -> None:
    """Daemon thread: stream /game/events and count lore_whisper events."""
    url = f"{base}/game/events"
    while True:
        try:
            req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
            with urllib.request.urlopen(req, timeout=300) as resp:
                event_type = None
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:") and event_type == "lore_whisper":
                        metrics["lore_whispers"] += 1
                        print(f"\n  {_gold('◈')} {_gold('Lore whisper received!')} "
                              f"(total: {metrics['lore_whispers']})")
                        event_type = None
                    elif line == "":
                        event_type = None
        except Exception:
            time.sleep(5.0)  # Reconnect after error


# ── Play a beat ───────────────────────────────────────────────────────────────
def play(text: str, label: str, base: str,
         pause_after: float = 0.0,
         timeout: float = TURN_TIMEOUT) -> dict:
    """Post player input, wait for narrator, ingest turn. Returns narrator turn dict."""
    print(f"\n  {_cyan('▶')} {_bold(label)}")
    print(f"    {_dim('Player:')} {text[:100]}")

    code, resp = _post(f"{base}/player-input", {"text": text}, timeout=30.0)
    assert_hard(code == 200, f"player-input returned HTTP {code}: {resp}")
    metrics["player_turns"] += 1

    turn = _wait_for_narrator_turn(base, timeout=timeout)
    _ingest_turn(turn)

    prose = _clean_for_story(turn.get("raw_text", ""))
    preview = (prose[:140] + "…") if len(prose) > 140 else prose
    print(f"    {_dim('Narrator:')} {preview}")

    if pause_after > 0:
        print(f"    {_dim(f'Waiting {pause_after:.0f}s for idle lore…')}")
        time.sleep(pause_after)

    return turn


# ── Richness scoring ─────────────────────────────────────────────────────────
def _compute_score(beats_done: int, starting_items: set) -> int:
    """Weighted richness score 0-100."""
    def _frac(val, target):
        return min(1.0, val / max(target, 1))

    senses = metrics["smell"] + metrics["taste"] + metrics["touch"]
    new_items = [i for i in metrics["items_acquired"] if i not in starting_items]

    score = (
        _frac(metrics["images"],         16) * 15 +
        _frac(metrics["moods"],          14) * 10 +
        _frac(senses,                    12) * 10 +
        _frac(beats_done,                14) * 25 +
        _frac(metrics["character_lines"], 30) * 10 +
        _frac(metrics["introduce_tags"],  3) * 10 +
        _frac(len(new_items),             3) * 10 +
        (5 if metrics["lore_whispers"] >= 1 else 0) +
        (5 if not metrics["warnings"] else 0)
    )
    return round(score)


# ── Ollama story summary ──────────────────────────────────────────────────────
def _summarise_story(player_name: str, diag_url: str) -> str:
    """Ask Ollama to summarise the session as second-person fiction."""
    excerpt = "\n\n".join(story_text[:60])
    if not excerpt.strip():
        return "(no story text captured)"

    prompt = (
        f"Summarize this game session as a 200-300 word story excerpt. Write in "
        f"second-person present tense in the narrative voice of The Fortress — an "
        f"ancient, kindly space station. Preserve character names and key moments. "
        f"The player's name is {player_name}.\n\nSession text:\n\n{excerpt[:8000]}"
    )

    # Try Ollama via diag port
    try:
        code, data = _post(
            f"{diag_url}/api/generate",
            {"model": "mistral", "prompt": prompt, "stream": False},
            timeout=120.0
        )
        if code == 200 and isinstance(data, dict):
            resp = data.get("response", "").strip()
            if resp:
                return resp
    except Exception:
        pass

    # Fallback: try direct Ollama on 11434
    try:
        code, data = _post(
            "http://localhost:11434/api/generate",
            {"model": "mistral", "prompt": prompt, "stream": False},
            timeout=120.0
        )
        if code == 200 and isinstance(data, dict):
            resp = data.get("response", "").strip()
            if resp:
                return resp
    except Exception:
        pass

    return "(Ollama unavailable — summary skipped)"


# ── Save results ─────────────────────────────────────────────────────────────
def _save_results(player_name: str, beats_done: int, score: int,
                  composite_sha: str, summary: str,
                  starting_items: set) -> Path:
    """Write markdown report to tests/story-results/."""
    STORY_RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    ts       = datetime.now(tz=timezone.utc)
    ts_str   = ts.strftime("%Y-%m-%dT%H-%M-%S")
    sha8     = composite_sha[:8] if composite_sha else "unknown"
    safe     = re.sub(r'[^\w-]', '-', player_name.lower())
    filename = f"{safe}-{sha8}-{ts_str}.md"

    new_items = [i for i in metrics["items_acquired"] if i not in starting_items]
    senses    = metrics["smell"] + metrics["taste"] + metrics["touch"]

    metrics_json = {
        k: (list(v) if isinstance(v, set) else v)
        for k, v in metrics.items()
    }

    content = f"""# The Story of {player_name}
**Version:** {sha8}
**Date:** {ts.isoformat()}
**Turns:** {metrics['narrator_turns']} narrator / {metrics['player_turns']} player | **Beats:** {beats_done}/14 | **Richness:** {score}/100
**Locations:** {', '.join(sorted(metrics['locations_visited'])) or '(none logged)'}
**NPCs met:** {', '.join(sorted(metrics['npcs_met'])) or '(none)'}
**NPCs created in world:** {', '.join(sorted(metrics['npcs_created'])) or '(none)'}
**Items acquired:** {', '.join(metrics['items_acquired']) or '(none)'}
**New items (beyond start):** {', '.join(new_items) or '(none)'}
**Lore collected:** {', '.join(metrics['lore_collected']) or '(none)'}
**Lore whispers heard:** {metrics['lore_whispers']}

---

{summary}

---

## Story Richness Score: {score}/100

| Metric | Value | Target |
|--------|-------|--------|
| Images generated | {metrics['images']} | ≥ 16 |
| Mood changes | {metrics['moods']} | ≥ 14 |
| SFX events | {metrics['sfx']} | ≥ 20 |
| Sense moments (smell/taste/touch) | {senses} | ≥ 12 |
| Character dialogue lines | {metrics['character_lines']} | ≥ 30 |
| LORE tags collected | {metrics['lore_tags']} | ≥ 5 |
| Lore whispers (idle) | {metrics['lore_whispers']} | ≥ 1 |
| NPCs introduced | {metrics['introduce_tags']} | ≥ 3 |
| New NPCs created in world | {len(metrics['npcs_created'])} | ≥ 2 |
| New items acquired | {len(new_items)} | ≥ 3 |
| Beats completed | {beats_done}/14 | 14/14 |
| Warnings (OOC) | {len(metrics['warnings'])} | 0 |

## Raw Metrics

```json
{json.dumps(metrics_json, indent=2)}
```
"""

    out_path = STORY_RESULTS_DIR / filename
    out_path.write_text(content, encoding="utf-8")
    return out_path


# ── Main test ─────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host",        default="localhost")
    parser.add_argument("--port",        type=int, default=1582)
    parser.add_argument("--diag-port",   type=int, default=1591)
    parser.add_argument("--max-turns",   type=int, default=MAX_TURNS)
    parser.add_argument("--player-name", default="Kael")
    parser.add_argument("--skip-reset",  action="store_true",
                        help="Skip world reset (use current world state)")
    args = parser.parse_args()

    base        = f"http://{args.host}:{args.port}"
    diag        = f"http://{args.host}:{args.diag_port}"
    player_name = args.player_name
    max_turns   = args.max_turns

    print(_bold(f"\n╔══════════════════════════════════════════╗"))
    print(_bold(f"║   Remnant — 100-Turn Story Arc Test      ║"))
    print(_bold(f"╚══════════════════════════════════════════╝"))
    print(f"  Target:  {base}")
    print(f"  Player:  {player_name}")
    print(f"  Budget:  {max_turns} turns\n")

    # ── Start SSE lore listener ───────────────────────────────────────────────
    sse_thread = threading.Thread(
        target=_sse_listener, args=(base,), daemon=True, name="sse-listener"
    )
    sse_thread.start()

    beats_done   = 0
    composite_sha = ""
    starting_items: set = set()
    baseline_entity_ids: set = set()

    try:
        # ── Beat 0: Setup ─────────────────────────────────────────────────────
        print(_bold("── Beat 0: Setup"))

        if not args.skip_reset:
            code, _ = _post(f"{base}/reset", {}, timeout=30.0)
            assert_hard(code == 200, f"/reset returned HTTP {code}")
            print(f"  {_ok('✓')} World reset OK")
            time.sleep(3.0)  # Let world settle

        code, ws = _get(f"{base}/world-state", timeout=15.0)
        assert_hard(code == 200, f"/world-state returned HTTP {code}")
        # /world-state returns entities as a list — index by id for convenience
        _raw_ents = ws.get("entities", [])
        if isinstance(_raw_ents, list):
            entities = {e["id"]: e for e in _raw_ents if "id" in e}
        else:
            entities = _raw_ents  # already a dict (future-proof)
        assert_hard(len(entities) >= 1, f"World has too few entities: {len(entities)}")
        print(f"  {_ok('✓')} World state: {len(entities)} entities")

        has_remnant = any(
            "remnant" in str(e.get("canonical_name", "")).lower() or
            "remnant" in str(eid).lower()
            for eid, e in entities.items()
        )
        assert_soft(has_remnant, "The Remnant in world-state",
                    detail="Remnant entity not found — check seed")
        print(f"  {_ok('✓') if has_remnant else _warn('⚠')} The Remnant in world")

        # Grab composite sha for filename
        code2, sig = _get(f"{base}/signature", timeout=10.0)
        if code2 == 200 and isinstance(sig, dict):
            composite_sha = sig.get("composite_sha256", "")

        # Snapshot entity baseline
        baseline_entity_ids = set(entities.keys())

        # Snapshot starting items from player entity
        player_ent = entities.get("__player__", {})
        inv = player_ent.get("inventory", [])
        if isinstance(inv, list):
            starting_items = {str(i) for i in inv}
        elif isinstance(inv, dict):
            starting_items = set(inv.keys())
        print(f"  {_dim('Starting items:')} {starting_items or '(none)'}")

        # Seed seen_ids with current turns
        _, initial_turns_data = _get(f"{base}/diagnostics/narrator-turns?n=50")
        for t in initial_turns_data.get("turns", []):
            seen_ids.add(str(t.get("turn_id", "")))

        # ── Beat 1: Portal arrival ────────────────────────────────────────────
        print(_bold("\n── Beat 1: Portal Arrival"))
        turn = play(
            "I open my eyes and look around.",
            "Portal arrival", base,
        )
        raw1 = turn.get("raw_text", "")
        assert_hard(True, "narrator responded")  # play() already asserts this
        assert_soft(_RE_IMAGE.search(raw1), "GENERATE_IMAGE on arrival",
                    "No image generated at portal arrival")
        beats_done += 1

        # ── Beat 2: Name yourself ─────────────────────────────────────────────
        print(_bold("\n── Beat 2: Name Yourself"))
        turn = play(
            f"My name is {player_name}. I am a traveller from a world of rain.",
            "Name yourself", base,
        )
        raw2 = turn.get("raw_text", "")
        assert_soft(re.search(r'\[PLAYER_TRAIT\(', raw2), "PLAYER_TRAIT(name) fires",
                    "No PLAYER_TRAIT tag after naming")
        assert_soft(player_name.lower() in raw2.lower(), "Name acknowledged",
                    f"{player_name!r} not reflected in narrator response")
        beats_done += 1

        # ── Beat 3: Follow Sherri ─────────────────────────────────────────────
        print(_bold("\n── Beat 3: Follow Sherri"))
        turn = play(
            "I follow Sherri to the Fabrication Bay.",
            "Follow Sherri", base,
        )
        raw3 = turn.get("raw_text", "")
        assert_soft(_RE_IMAGE.search(raw3), "GENERATE_IMAGE in Fabrication Bay",
                    "No image in Fabrication Bay")
        assert_soft(
            "sherri" in raw3.lower() or _RE_CHAR.search(raw3),
            "Sherri speaks", "Sherri has no dialogue"
        )
        beats_done += 1

        # ── Beat 4: Describe appearance ───────────────────────────────────────
        print(_bold("\n── Beat 4: Describe Appearance"))
        turn = play(
            "I am tall, with dark curly hair and sharp green eyes. Weathered hands.",
            "Describe appearance", base,
        )
        raw4 = turn.get("raw_text", "")
        assert_soft(re.search(r'\[PLAYER_TRAIT\(', raw4), "PLAYER_TRAIT(appearance) fires",
                    "No PLAYER_TRAIT after appearance description")
        assert_soft(re.search(r'\[UPDATE_PLAYER\]', raw4), "UPDATE_PLAYER fires",
                    "Avatar not refreshed after appearance")
        beats_done += 1

        # ── Beat 5: Ask for clothes ───────────────────────────────────────────
        print(_bold("\n── Beat 5: Ask for Clothes"))
        turn = play(
            "Sherri, can you make me some clothes? Dark practical ones, lots of pockets.",
            "Ask for clothes", base,
        )
        raw5 = turn.get("raw_text", "")
        assert_soft(
            "sherri" in raw5.lower() or _RE_CHAR.search(raw5),
            "Sherri responds about fabrication", "No Sherri response"
        )
        beats_done += 1

        # ── Beat 6: Clothes applied ───────────────────────────────────────────
        print(_bold("\n── Beat 6: Clothes Applied"))
        turn = play(
            "I put on the new clothes. What do I look like?",
            "Clothes applied", base,
        )
        raw6 = turn.get("raw_text", "")
        assert_soft(re.search(r'\[UPDATE_PLAYER\]', raw6) or re.search(r'\[ITEM\(', raw6),
                    "UPDATE_PLAYER or ITEM fires", "No avatar update or item tag")
        beats_done += 1

        # ── Beat 7: To the galley ─────────────────────────────────────────────
        print(_bold("\n── Beat 7: To the Galley"))
        turn = play(
            "Sherri, I'm hungry. Take me to the galley.",
            "To the galley", base,
        )
        raw7 = turn.get("raw_text", "")
        assert_soft(
            "galley" in raw7.lower() or _RE_IMAGE.search(raw7),
            "Galley location/image", "No galley mention or image"
        )
        assert_soft(
            "sherri" in raw7.lower() or _RE_CHAR.search(raw7),
            "Sherri speaks in galley", "Sherri silent"
        )
        beats_done += 1

        # ── Beat 8: Order meal ────────────────────────────────────────────────
        print(_bold("\n── Beat 8: Order Meal"))
        turn = play(
            "Sherri, whatever smells best today.",
            "Order meal", base,
        )
        raw8 = turn.get("raw_text", "")
        assert_soft(_RE_SMELL.search(raw8), "SMELL on meal order",
                    "No smell sense when ordering food")
        beats_done += 1

        # ── Beat 9: Eat the meal ──────────────────────────────────────────────
        print(_bold("\n── Beat 9: Eat the Meal"))
        turn = play(
            "I eat slowly, taking in every flavour.",
            "Eat the meal", base,
        )
        raw9 = turn.get("raw_text", "")
        assert_soft(_RE_SMELL.search(raw9) or _RE_TASTE.search(raw9),
                    "SMELL or TASTE while eating", "No smell/taste during meal")
        beats_done += 1

        # ── Beat 10: To sleeping quarters ─────────────────────────────────────
        print(_bold("\n── Beat 10: To Sleeping Quarters"))
        turn = play(
            "Sherri, show me where I can sleep.",
            "To sleeping quarters", base,
        )
        raw10 = turn.get("raw_text", "")
        assert_soft(_RE_IMAGE.search(raw10), "GENERATE_IMAGE for sleeping quarters",
                    "No image for sleeping quarters")
        beats_done += 1

        # ── Beat 11: Go to sleep (65s idle window for lore whisper) ──────────
        print(_bold("\n── Beat 11: Go to Sleep"))
        print(f"  {_dim('(Will pause 65s after response to allow lore whisper…)')}")
        lore_before = metrics["lore_whispers"]
        turn = play(
            "I climb into a bunk and close my eyes.",
            "Go to sleep", base,
            pause_after=65.0,   # idle window for _lore_idle_loop (fires at 50s)
        )
        raw11 = turn.get("raw_text", "")
        assert_soft(
            _RE_SOUND.search(raw11) or _RE_ENV.search(raw11) or _RE_SMELL.search(raw11),
            "Sense markers while sleeping", "No sense tags on sleep"
        )
        beats_done += 1

        # ── Beat 12: Wake up (another 65s window) ────────────────────────────
        print(_bold("\n── Beat 12: Wake Up"))
        print(f"  {_dim('(Another 65s pause for second lore whisper opportunity…)')}")
        turn = play(
            "I wake up. How long was I asleep?",
            "Wake up", base,
            pause_after=65.0,
        )
        raw12 = turn.get("raw_text", "")
        assert_soft(_RE_MOOD.search(raw12), "MOOD shift on waking",
                    "No mood change on waking")
        assert_soft(metrics["lore_whispers"] > lore_before, "Lore whisper fired during sleep",
                    f"lore_whispers={metrics['lore_whispers']} (was {lore_before})")
        beats_done += 1

        # ── Beat 13: To the Nexus ─────────────────────────────────────────────
        print(_bold("\n── Beat 13: To the Nexus"))
        turn = play(
            "Sherri, take me to the Nexus. I wish to speak with the Remnant.",
            "To the Nexus", base,
        )
        raw13 = turn.get("raw_text", "")
        assert_soft(_RE_IMAGE.search(raw13), "GENERATE_IMAGE for the Nexus",
                    "No image for Nexus")
        assert_soft(
            "remnant" in raw13.lower() or _RE_CHAR.search(raw13),
            "The Remnant speaks", "Remnant silent in Nexus"
        )
        beats_done += 1

        # ── Beat 14: Ask the Remnant ──────────────────────────────────────────
        print(_bold("\n── Beat 14: Ask the Remnant"))
        turn = play(
            "Remnant — why did you bring me here? What do you need from me?",
            "Ask the Remnant", base,
        )
        raw14 = turn.get("raw_text", "")
        assert_soft(_RE_CHAR.search(raw14), "CHARACTER(The Remnant) tag",
                    "No CHARACTER tag for Remnant")
        beats_done += 1

        # ── Beat 15: Accept quest ─────────────────────────────────────────────
        print(_bold("\n── Beat 15: Accept Quest"))
        turn = play(
            "I'll do it. Whatever it takes. Tell me how to begin.",
            "Accept quest", base,
        )
        raw15 = turn.get("raw_text", "")
        assert_soft(
            "quest" in raw15.lower() or "mission" in raw15.lower()
            or "task" in raw15.lower() or "begin" in raw15.lower(),
            "Quest acknowledged", "Quest not reflected in response"
        )
        beats_done += 1

        # ── Quest arc (beats 16–max_turns) ────────────────────────────────────
        print(_bold(f"\n── Quest Arc (turns 16–{max_turns})"))

        _arc_fallbacks = [
            "I push forward. What happens next?",
            "I examine my surroundings carefully.",
            "I ask the Remnant what to do.",
            "I look for anything useful in this room.",
            "I listen for any sounds nearby.",
        ]
        _arc_idx = 0
        quest_turn = 0
        last_intro_turn = -99
        prev_world_snap: dict = {}

        while metrics["player_turns"] + metrics["narrator_turns"] < max_turns:
            quest_turn += 1

            # Snapshot world entities every 5 quest turns
            if quest_turn % 5 == 0:
                current_ents = _snap_world_entities(base)
                _update_npcs_created(current_ents, baseline_entity_ids)

            # Choose player input
            last_raw = story_text[-1] if story_text else ""

            if quest_turn % 8 == 0:
                text = "Tell me more about this place. What do you know of its history?"
            elif quest_turn % 5 == 0:
                text = "Is there anything here worth taking?"
            elif "introduce" in last_raw.lower() and quest_turn != last_intro_turn:
                text = "Tell me about yourself. Who are you?"
                last_intro_turn = quest_turn
            elif "fabrication" in last_raw.lower() and "sherri" in last_raw.lower():
                text = "I work with Sherri to recalibrate the rigs."
            elif "nexus" in last_raw.lower() and "frequency" in last_raw.lower():
                text = "I goad the Remnant into revealing what it knows."
            elif "cold" in last_raw.lower() and "spot" in last_raw.lower():
                text = "I follow the cold trail through the sleeping quarters."
            elif "portal" in last_raw.lower():
                text = "I step through the portal."
            else:
                text = _arc_fallbacks[_arc_idx % len(_arc_fallbacks)]
                _arc_idx += 1

            label = f"Quest arc turn {quest_turn}"
            try:
                turn = play(text, label, base)
            except StepFailed as e:
                print(f"  {_err('✗')} {label} failed: {e}")
                break

            raw = turn.get("raw_text", "")
            assert_soft(_RE_IMAGE.search(raw) or _RE_MOOD.search(raw) or _RE_CHAR.search(raw),
                        f"Quest turn {quest_turn} richness",
                        "No image/mood/character in quest turn")

        # Final world entity diff
        current_ents = _snap_world_entities(base)
        _update_npcs_created(current_ents, baseline_entity_ids)

    except StepFailed as e:
        print(f"\n  {_err('HARD FAIL:')} {e}")
        # Still proceed to scoring

    # ── Scoring ───────────────────────────────────────────────────────────────
    score = _compute_score(beats_done, starting_items)
    new_items = [i for i in metrics["items_acquired"] if i not in starting_items]
    senses    = metrics["smell"] + metrics["taste"] + metrics["touch"]

    print(_bold(f"\n{'═'*50}"))
    print(_bold("  STORY RICHNESS SCORE"))
    print(_bold(f"{'═'*50}"))

    rows = [
        ("Images generated",           metrics["images"],           "≥ 16"),
        ("Mood changes",                metrics["moods"],            "≥ 14"),
        ("SFX events",                  metrics["sfx"],              "≥ 20"),
        ("Sense moments (smell/taste/touch)", senses,                "≥ 12"),
        ("Character dialogue lines",    metrics["character_lines"],  "≥ 30"),
        ("Lore tags collected",         metrics["lore_tags"],        "≥ 5"),
        ("Lore whispers (idle)",        metrics["lore_whispers"],    "≥ 1"),
        ("NPCs introduced",             metrics["introduce_tags"],   "≥ 3"),
        ("New NPCs in world",           len(metrics["npcs_created"]),"≥ 2"),
        ("New items acquired",          len(new_items),              "≥ 3"),
    ]
    for label, val, target in rows:
        flag = _ok("✓") if val >= int(target.split("≥")[1].strip()) else _warn("·")
        print(f"  {flag}  {label:<38} {val:>4}   {_dim(target)}")

    print(_dim(f"\n  Narrator turns:  {metrics['narrator_turns']}"))
    print(_dim(f"  Player turns:    {metrics['player_turns']}"))
    print(_dim(f"  Warnings (OOC):  {len(metrics['warnings'])}"))
    if metrics["warnings"]:
        for w in metrics["warnings"][:3]:
            print(f"     {_warn(w[:100])}")

    print(_dim(f"\n  NPCs met:        {', '.join(sorted(metrics['npcs_met'])) or '(none)'}"))
    print(_dim(f"  NPCs created:    {', '.join(sorted(metrics['npcs_created'])) or '(none)'}"))
    print(_dim(f"  Items acquired:  {', '.join(metrics['items_acquired']) or '(none)'}"))
    print(_dim(f"  Lore collected:  {', '.join(metrics['lore_collected']) or '(none)'}"))
    print(_dim(f"  Locations:       {', '.join(sorted(metrics['locations_visited'])) or '(none)'}"))

    print(f"\n  Beats completed: {_bold(str(beats_done))}/14")
    print(f"  {_bold('RICHNESS SCORE:')} {_cyan(str(score))}/100\n")

    # ── Soft results summary ──────────────────────────────────────────────────
    soft_pass = sum(1 for _, ok, _ in soft_results if ok)
    soft_fail = sum(1 for _, ok, _ in soft_results if not ok)
    if soft_fail:
        print(_bold("  Soft assertions failed:"))
        for label, ok, detail in soft_results:
            if not ok:
                print(f"    {_warn('⚠')}  {label}: {_dim(detail)}")

    print(_dim(f"\n  Soft checks: {soft_pass} passed, {soft_fail} failed"))

    # ── Ollama summary ────────────────────────────────────────────────────────
    print(f"\n  {_dim('Generating story summary via Ollama…')}")
    summary = _summarise_story(player_name, diag)
    print(f"  {_dim('Summary generated.')}")

    # ── Save ──────────────────────────────────────────────────────────────────
    out_path = _save_results(player_name, beats_done, score,
                             composite_sha, summary, starting_items)
    print(f"\n  {_ok('✓')} Story saved to: {_bold(str(out_path))}")
    print()

    return 0 if beats_done >= 12 and score >= 40 else 1


if __name__ == "__main__":
    sys.exit(main())
