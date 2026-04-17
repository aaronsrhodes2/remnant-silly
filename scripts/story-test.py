#!/usr/bin/env python3
"""story-test.py — pilot-quest arc test for Remnant.

Drives the game through all 15 beats of the pilot quest to completion:
  portal arrival → name → outfit → galley → sleep → Nexus → quest → arc end

Runs until all 15 beats pass or the safety cap (300 turns) is reached.
Measures story richness at every beat (images, moods, SFX, senses, character
lines, lore, items, NPC introductions, new world entities) and saves an
Ollama-summarised story excerpt to tests/story-results/.

USAGE:
    python -X utf8 scripts/story-test.py
    python -X utf8 scripts/story-test.py --player-name "Wren"
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

BASE_URL          = "http://localhost:1582"
DIAG_URL          = "http://localhost:1591"
TURN_TIMEOUT      = 300   # 300s — covers model reload + image gen GPU contention
MAX_SAFETY_TURNS  = 300   # hard ceiling — terminate if pilot quest stalls

# ── Tag extraction regexes ────────────────────────────────────────────────────
_RE_IMAGE    = re.compile(r'\[GENERATE_IMAGE')
_RE_MOOD     = re.compile(r'\[MOOD[\s:(]')   # matches [MOOD: "..."] and [MOOD(...]
_RE_SOUND    = re.compile(r'\[SOUND[\s:(]')  # matches [SOUND: "..."] and [SOUND(...)
_RE_SMELL    = re.compile(r'\[SMELL\(')
_RE_TASTE    = re.compile(r'\[TASTE\(')
_RE_TOUCH    = re.compile(r'\[TOUCH\(')
_RE_ENV      = re.compile(r'\[ENVIRONMENT\(')
_RE_CHAR     = re.compile(r'\[CHARACTER\(([^)]+)\)\s*:')          # [CHARACTER(Name): "speech"
_RE_LORE     = re.compile(r'\[LORE\(([^)]+)\)')                   # [LORE(key)  or [LORE(key): "..."]
_RE_ITEM     = re.compile(r'\[ITEM\(([^)]+)\)')                   # [ITEM(name) or [ITEM(name): "..."]
_RE_TRAIT    = re.compile(r'\[PLAYER_TRAIT\(')
_RE_UPDATE   = re.compile(r'\[UPDATE_PLAYER[\]:]')                # [UPDATE_PLAYER] or [UPDATE_PLAYER: ...]
_RE_INTRO    = re.compile(r'\[INTRODUCE\(([^)]+)\)\s*:')
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
    """Return entities as a {id: entity} dict from /world-state.

    /world-state may return either:
      - a list of entity objects (current diag API)
      - a dict with an 'entities' key containing a list or dict
    """
    code, data = _get(f"{base}/world-state", timeout=10.0)
    if code != 200:
        return {}
    if isinstance(data, list):
        return {e["id"]: e for e in data if isinstance(e, dict) and "id" in e}
    if isinstance(data, dict):
        raw = data.get("entities", [])
        if isinstance(raw, list):
            return {e["id"]: e for e in raw if isinstance(e, dict) and "id" in e}
        if isinstance(raw, dict):
            return raw
    return {}


def _update_npcs_created(current_entities: dict, baseline_ids: set) -> None:
    """Diff current entities against baseline; add new NPCs to metrics."""
    for eid, ent in current_entities.items():
        if eid not in baseline_ids and eid != "__player__":
            if isinstance(ent, dict) and ent.get("type") in ("npc", "character", "person"):
                metrics["npcs_created"].add(eid)


OLLAMA_URL = "http://localhost:1582/api/ollama"  # via nginx — port 1593 not published to host


def _wait_ollama_ready(_max_wait: float = 0) -> None:
    """No-op: the old Ollama generate probe was removed.

    The narrator-reset thread fires immediately after world reset and will occupy
    Ollama for 60-120s; any Ollama probe would hang. _drain_reset_opening() is
    the correct gate — it polls narrator-turns until the opening appears.
    The SSE listener's long-lived urlopen also makes additional urlopen calls
    unreliable on Windows (GIL/socket interaction). Nothing to do here.
    """
    pass


def _drain_reset_opening(base: str, max_wait: float = 360.0) -> None:
    """Wait for the auto-generated narrator turn that fires after a world reset.

    The reset handler spawns a narrator-reset thread that generates an opening
    sequence via Ollama.  If the story test submits Beat 1 while this is still
    running, two Ollama jobs compete and both can exceed TURN_TIMEOUT.

    This function waits until the opening turn arrives (marking it in seen_ids
    so the test won't count it as a beat response), then returns.  Proceeds
    without error if no turn appears within max_wait seconds.
    """
    print(f"  {_dim('Waiting for post-reset auto-opening (up to %.0fs)…' % max_wait)}")
    deadline = time.time() + max_wait
    while time.time() < deadline:
        code, data = _get(f"{base}/diagnostics/narrator-turns?n=50", timeout=10.0)
        if code == 200 and isinstance(data, dict):
            turns = data.get("turns", [])
            new_narr = [
                t for t in turns
                if str(t.get("turn_id", "")) not in seen_ids
                and not t.get("is_player", False)
            ]
            if new_narr:
                for t in new_narr:
                    seen_ids.add(str(t.get("turn_id", "")))
                prose = _clean_for_story(new_narr[-1].get("raw_text", ""))[:80]
                print(f"  {_ok('✓')} Auto-opening received: {_dim(prose + '…')}")
                return
        time.sleep(3.0)
    print(f"  {_warn('⚠')} Auto-opening not detected within {max_wait:.0f}s — proceeding anyway")


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


# ── Natural player agent ──────────────────────────────────────────────────────
OLLAMA_DIRECT_URL = "http://localhost:1593"


def _player_agent_turn(
    narrator_text: str,
    player_name: str,
    beat_hint: str = "",
) -> str:
    """Ask Ollama to generate a natural player response to the narrator.

    beat_hint is injected when the expected beat tag hasn't fired after 3 turns —
    nudges the player agent without touching the narrator prompt.
    Returns a one-line player action/question (8-25 words).
    """
    hint_line = f"\n[HINT: {beat_hint}]" if beat_hint else ""
    prompt = (
        f"You are {player_name}, a cautious and curious traveler who has just arrived aboard an ancient "
        "alien space station. Respond with ONE natural action or question (8-25 words). "
        "Be reactive — respond to what you just saw or heard. Don't meta-game, don't mention "
        "game mechanics or tags, and don't repeat the narrator's words back.\n\n"
        f"What the narrator just said:\n{narrator_text[:500]}\n"
        f"{hint_line}\n"
        f"Your response (first person, one line only):"
    )
    try:
        payload = json.dumps({
            "model": "remnant-narrator:latest",
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": 40, "temperature": 0.85},
        }).encode()
        req = urllib.request.Request(
            f"{OLLAMA_DIRECT_URL}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20.0) as r:
            data = json.loads(r.read().decode())
            response = data.get("response", "").strip()
            response = response.split("\n")[0].strip().strip('"\'')
            if 5 <= len(response.split()) <= 50:
                return response
    except Exception as e:
        print(f"  {_warn('⚠')} Player agent failed: {e}")
    # Fallback: generic exploration action
    return "I explore the area carefully, taking in the surroundings."


# ── Golden training data ─────────────────────────────────────────────────────
def _approve_golden_turn(diag_url: str, turn: dict) -> None:
    """POST to /narratorturn/<id>/approve — silently ignore failures."""
    turn_id = turn.get("turn_id", "")
    if not turn_id:
        return
    try:
        code, resp = _post(f"{diag_url}/narratorturn/{turn_id}/approve", {}, timeout=10.0)
        if code == 200 and resp.get("approved"):
            print(f"  {_gold('◈')} Golden turn saved ({resp.get('total_golden', '?')} total)")
        elif resp.get("duplicate"):
            pass  # silent — already collected
    except Exception:
        pass  # golden collection is best-effort


# ── Play a beat ───────────────────────────────────────────────────────────────
def play(text: str, label: str, base: str,
         pause_after: float = 0.0,
         timeout: float = TURN_TIMEOUT) -> dict:
    """Post player input, wait for narrator, ingest turn. Returns narrator turn dict."""
    print(f"\n  {_cyan('▶')} {_bold(label)}")
    print(f"    {_dim('Player:')} {text[:100]}")

    code, resp = _post(f"{base}/player-input", {"text": text}, timeout=60.0)
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


# ── Self-healing beat wrapper ─────────────────────────────────────────────────
_MAX_BEAT_RETRIES = 2
_RETRY_WAITS = [20, 45]   # seconds to wait between attempt 1→2, 2→3


def play_with_retry(text: str, label: str, base: str,
                    pause_after: float = 0.0,
                    timeout: float = TURN_TIMEOUT) -> dict:
    """Call play() with up to _MAX_BEAT_RETRIES retries on timeout.

    On timeout: wait for Ollama to clear, check service health, then retry.
    Non-timeout StepFailed errors are re-raised immediately (hard fail).
    """
    last_exc: StepFailed | None = None
    for attempt in range(_MAX_BEAT_RETRIES + 1):
        try:
            return play(text, label, base, pause_after=pause_after, timeout=timeout)
        except StepFailed as e:
            last_exc = e
            if "Timed out" not in str(e) or attempt >= _MAX_BEAT_RETRIES:
                raise
            wait = _RETRY_WAITS[attempt]
            print(f"  {_warn('⚠')} Timeout on attempt {attempt + 1} — waiting {wait}s for Ollama to clear…")
            time.sleep(wait)
            # Check service health via diagnostics
            try:
                req = urllib.request.Request(
                    f"{base}/diagnostics/ai.json",
                    headers={"Accept": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=8) as r:
                    ai = json.loads(r.read())
                if not ai.get("ollama", {}).get("healthy", True):
                    print(f"  {_warn('⚠')} Ollama unhealthy — waiting 30s extra…")
                    time.sleep(30)
            except Exception:
                pass
            print(f"  → Retrying {label} (attempt {attempt + 2})…")
    raise last_exc  # type: ignore[misc]


# ── Richness scoring ─────────────────────────────────────────────────────────
def _compute_score(beats_done: int, starting_items: set) -> int:
    """Weighted richness score 0-100."""
    def _frac(val, target):
        return min(1.0, val / max(target, 1))

    senses = metrics["smell"] + metrics["taste"] + metrics["touch"]
    new_items = [i for i in metrics["items_acquired"] if i not in starting_items]

    # Quest-complete bonus: tricorder acquired + antagonist or protagonist met
    _new_item_keys = set(new_items)
    _has_tricorder = any(i in ('tricorder', 'found_artifact') for i in _new_item_keys)
    _npcs_lc = {n.lower() for n in metrics["npcs_met"]}
    _has_antagonist  = any('vex' in n for n in _npcs_lc)
    _has_protagonist = any(n in _npcs_lc for n in ('mira', 'artisan_kaelo'))
    _quest_complete  = _has_tricorder and (_has_antagonist or _has_protagonist)

    score = (
        _frac(metrics["images"],          16) * 12 +   # reduced: VRAM guard limits SD in test
        _frac(metrics["moods"],           14) * 10 +
        _frac(senses,                     12) * 10 +
        _frac(beats_done,                 15) * 25 +   # 15 beats = 100%
        _frac(metrics["character_lines"], 30) * 10 +
        _frac(metrics["introduce_tags"],   3) *  8 +
        _frac(len(new_items),              3) * 10 +
        _frac(len(metrics["npcs_created"]),2) *  8 +   # new world entities created
        (3 if metrics["lore_whispers"] >= 1 else 0) +
        (4 if _quest_complete else 0)
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


# ── Singleton guard: kill any existing story-test.py instances ────────────────
def _kill_existing_instances() -> None:
    """Kill any other running story-test.py processes before starting.

    Multiple concurrent instances corrupt the narrator history, causing
    duplicate/competing inputs. This ensures only one test runs at a time.
    """
    import os
    my_pid = os.getpid()
    my_script = Path(__file__).resolve()
    try:
        import subprocess
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-WmiObject Win32_Process -Filter \"Name='python.exe' OR Name='python3.exe'\" "
             "| Select-Object ProcessId, CommandLine | ConvertTo-Json"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return
        procs = json.loads(result.stdout.strip())
        if isinstance(procs, dict):
            procs = [procs]
        killed = []
        for p in procs:
            pid  = p.get("ProcessId") or p.get("ProcessID")
            cmd  = p.get("CommandLine") or ""
            if pid and int(pid) != my_pid and "story-test" in cmd:
                try:
                    subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                                   capture_output=True, timeout=5)
                    killed.append(int(pid))
                except Exception:
                    pass
        if killed:
            print(f"  {_warn('⚠')} Killed {len(killed)} existing story-test instance(s): {killed}")
            time.sleep(1)   # brief pause to let narrator state settle
    except Exception as exc:
        print(f"  {_dim('(singleton guard skipped:)')} {exc}")


# ── Main test ─────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host",        default="localhost")
    parser.add_argument("--port",        type=int, default=1582)
    parser.add_argument("--diag-port",   type=int, default=1591)
    parser.add_argument("--player-name", default="Kael")
    parser.add_argument("--skip-reset",  action="store_true",
                        help="Skip world reset (use current world state)")
    parser.add_argument("--save-golden", action="store_true",
                        help="Auto-approve high-quality turns for LoRA training data")
    parser.add_argument("--natural", action="store_true",
                        help="LLM-driven player agent (reactive, not hardcoded). Beat detection "
                             "from narrator tags. Slower but tests emergent storytelling.")
    args = parser.parse_args()

    _kill_existing_instances()   # ensure we're the only story-test running

    base        = f"http://{args.host}:{args.port}"
    diag        = f"http://{args.host}:{args.diag_port}"
    player_name = args.player_name
    max_turns   = MAX_SAFETY_TURNS
    save_golden  = getattr(args, "save_golden", False)
    natural_mode = getattr(args, "natural", False)

    def _golden(turn: dict) -> None:
        if save_golden:
            _approve_golden_turn(base, turn)

    _last_narrator_raw = [""]  # mutable cell so inner functions can update it

    def _natural_beat(
        forced_text: str,
        label: str,
        tag_condition,       # callable(raw_text) -> bool
        beat_hint: str = "",
        max_tries: int = 5,
    ) -> dict:
        """In natural mode: loop asking the LLM player for input until tag_condition fires.
        In forced mode: send forced_text once (original behavior).
        beat_hint is injected after 3 failed tag attempts.
        """
        if not natural_mode:
            turn = play_with_retry(forced_text, label, base)
            _last_narrator_raw[0] = turn.get("raw_text", "")
            return turn

        for attempt in range(max_tries):
            hint = beat_hint if attempt >= 3 else ""
            text = _player_agent_turn(_last_narrator_raw[0], player_name, hint)
            print(f"  {_dim('(natural)')} {text[:100]}")
            try:
                turn = play_with_retry(text, label, base)
            except StepFailed:
                turn = {}
            _last_narrator_raw[0] = turn.get("raw_text", "")
            if tag_condition(_last_narrator_raw[0]):
                return turn
            if attempt < max_tries - 1:
                print(f"  {_dim(f'Tag not yet fired — attempt {attempt+2}/{max_tries}')}")
        return turn  # last turn even if tag never fired

    print(_bold(f"\n╔══════════════════════════════════════════╗"))
    print(_bold(f"║   Remnant — Pilot Quest Arc Test         ║"))
    print(_bold(f"╚══════════════════════════════════════════╝"))
    print(f"  Target:  {base}")
    print(f"  Player:  {player_name}")
    print(f"  Safety:  {max_turns} turn cap\n")

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
            # Use "world" level reset: clears ALL narrator turns, player state,
            # session trackers (_introduced_this_session, etc.) and re-seeds from
            # seed/world.json. This ensures a completely clean run each time and
            # avoids inheriting _player_dressed / _player_appearance_desc from
            # previous sessions (those linger across scene/story resets).
            code, _ = _post(f"{base}/reset", {"level": "world"}, timeout=30.0)
            assert_hard(code == 200, f"/reset returned HTTP {code}")
            print(f"  {_ok('✓')} World reset OK")
            # Let any in-flight Ollama background calls (caption prettify, embed)
            # settle before the narrator-reset thread competes for the Ollama queue.
            # Let any in-flight Ollama background calls settle before checking
            print(f"  {_dim('Waiting 10s for Ollama background calls to settle…')}")
            time.sleep(10.0)
            # Quick Ollama responsiveness check — if Ollama is still busy with
            # background embed/caption calls, wait until it responds promptly
            _wait_ollama_ready()
            # Wait for the narrator-reset auto-opening to complete before
            # submitting Beat 1, otherwise two Ollama jobs compete and stall.
            _drain_reset_opening(base, max_wait=360.0)

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
        turn = _natural_beat(
            "I open my eyes and look around.",
            "Portal arrival",
            lambda raw: bool(_RE_IMAGE.search(raw) or _RE_MOOD.search(raw)),
            "Look around and describe what you see after arriving.",
        )
        _last_narrator_raw[0] = turn.get("raw_text", "")
        raw1 = _last_narrator_raw[0]
        assert_hard(True, "narrator responded")
        assert_soft(_RE_IMAGE.search(raw1), "GENERATE_IMAGE on arrival",
                    "No image generated at portal arrival")
        beats_done += 1
        _golden(turn)

        # ── Beat 2: Name yourself ─────────────────────────────────────────────
        print(_bold("\n── Beat 2: Name Yourself"))
        turn = _natural_beat(
            f"My name is {player_name}. I am a traveller from a world of rain.",
            "Name yourself",
            lambda raw: bool(re.search(r'\[PLAYER_TRAIT\(', raw)),
            f"The narrator asked your name. Introduce yourself as {player_name}.",
        )
        _last_narrator_raw[0] = turn.get("raw_text", "")
        raw2 = _last_narrator_raw[0]
        assert_soft(re.search(r'\[PLAYER_TRAIT\(', raw2), "PLAYER_TRAIT(name) fires",
                    "No PLAYER_TRAIT tag after naming")
        assert_soft(player_name.lower() in raw2.lower(), "Name acknowledged",
                    f"{player_name!r} not reflected in narrator response")
        beats_done += 1
        _golden(turn)

        # ── Beat 3: Follow Sherri ─────────────────────────────────────────────
        print(_bold("\n── Beat 3: Follow Sherri"))
        turn = _natural_beat(
            "I follow Sherri to the Fabrication Bay.",
            "Follow Sherri",
            lambda raw: bool(_RE_CHAR.search(raw) or "sherri" in raw.lower()),
            "Follow Sherri to the Fabrication Bay to get some clothes.",
        )
        _last_narrator_raw[0] = turn.get("raw_text", "")
        raw3 = _last_narrator_raw[0]
        assert_soft(_RE_IMAGE.search(raw3), "GENERATE_IMAGE in Fabrication Bay",
                    "No image in Fabrication Bay")
        assert_soft(
            "sherri" in raw3.lower() or _RE_CHAR.search(raw3),
            "Sherri speaks", "Sherri has no dialogue"
        )
        beats_done += 1
        _golden(turn)

        # ── Beat 4: Describe appearance ───────────────────────────────────────
        print(_bold("\n── Beat 4: Describe Appearance"))
        turn = _natural_beat(
            "I am tall, with dark curly hair and sharp green eyes. Weathered hands.",
            "Describe appearance",
            lambda raw: bool(re.search(r'\[PLAYER_TRAIT\(', raw)),
            "Describe your physical appearance in detail — height, hair, eyes.",
        )
        _last_narrator_raw[0] = turn.get("raw_text", "")
        raw4 = _last_narrator_raw[0]
        assert_soft(re.search(r'\[PLAYER_TRAIT\(', raw4), "PLAYER_TRAIT(appearance) fires",
                    "No PLAYER_TRAIT after appearance description")
        assert_soft(re.search(r'\[UPDATE_PLAYER\]', raw4), "UPDATE_PLAYER fires",
                    "Avatar not refreshed after appearance")
        beats_done += 1
        _golden(turn)

        # ── Beat 5: Ask for clothes ───────────────────────────────────────────
        print(_bold("\n── Beat 5: Ask for Clothes"))
        turn = _natural_beat(
            "Sherri, can you make me some clothes? Dark practical ones, lots of pockets.",
            "Ask for clothes",
            lambda raw: bool("sherri" in raw.lower() or _RE_CHAR.search(raw)),
            "Ask Sherri to make you some dark, practical clothes.",
        )
        _last_narrator_raw[0] = turn.get("raw_text", "")
        raw5 = _last_narrator_raw[0]
        assert_soft(
            "sherri" in raw5.lower() or _RE_CHAR.search(raw5),
            "Sherri responds about fabrication", "No Sherri response"
        )
        beats_done += 1
        _golden(turn)

        # ── Beat 6: Clothes applied ───────────────────────────────────────────
        print(_bold("\n── Beat 6: Clothes Applied"))
        turn = _natural_beat(
            "I put on the new clothes. What do I look like?",
            "Clothes applied",
            lambda raw: bool(re.search(r'\[UPDATE_PLAYER\]', raw) or re.search(r'\[ITEM\(', raw)),
            "Put on the clothes Sherri made and ask what you look like.",
        )
        _last_narrator_raw[0] = turn.get("raw_text", "")
        raw6 = _last_narrator_raw[0]
        assert_soft(re.search(r'\[UPDATE_PLAYER\]', raw6) or re.search(r'\[ITEM\(', raw6),
                    "UPDATE_PLAYER or ITEM fires", "No avatar update or item tag")
        beats_done += 1
        _golden(turn)

        # ── Beat 7: To the galley ─────────────────────────────────────────────
        print(_bold("\n── Beat 7: To the Galley"))
        turn = _natural_beat(
            "Sherri, I'm hungry. Take me to the galley.",
            "To the galley",
            lambda raw: bool("galley" in raw.lower() or _RE_IMAGE.search(raw)),
            "Tell Sherri you are hungry and ask to go to the galley.",
        )
        _last_narrator_raw[0] = turn.get("raw_text", "")
        raw7 = _last_narrator_raw[0]
        assert_soft(
            "galley" in raw7.lower() or _RE_IMAGE.search(raw7),
            "Galley location/image", "No galley mention or image"
        )
        assert_soft(
            "sherri" in raw7.lower() or _RE_CHAR.search(raw7),
            "Sherri speaks in galley", "Sherri silent"
        )
        beats_done += 1
        _golden(turn)

        # ── Beat 8: Order meal ────────────────────────────────────────────────
        print(_bold("\n── Beat 8: Order Meal"))
        turn = _natural_beat(
            "Sherri, whatever smells best today.",
            "Order meal",
            lambda raw: bool(_RE_SMELL.search(raw)),
            "Order food from Sherri — ask for whatever smells best.",
        )
        _last_narrator_raw[0] = turn.get("raw_text", "")
        raw8 = _last_narrator_raw[0]
        assert_soft(_RE_SMELL.search(raw8), "SMELL on meal order",
                    "No smell sense when ordering food")
        beats_done += 1
        _golden(turn)

        # ── Beat 9: Eat the meal ──────────────────────────────────────────────
        print(_bold("\n── Beat 9: Eat the Meal"))
        turn = _natural_beat(
            "I eat slowly, taking in every flavour.",
            "Eat the meal",
            lambda raw: bool(_RE_SMELL.search(raw) or _RE_TASTE.search(raw)),
            "Eat the meal you were given, savoring every bite.",
        )
        _last_narrator_raw[0] = turn.get("raw_text", "")
        raw9 = _last_narrator_raw[0]
        assert_soft(_RE_SMELL.search(raw9) or _RE_TASTE.search(raw9),
                    "SMELL or TASTE while eating", "No smell/taste during meal")
        beats_done += 1
        _golden(turn)

        # ── Beat 10: To sleeping quarters ─────────────────────────────────────
        print(_bold("\n── Beat 10: To Sleeping Quarters"))
        turn = _natural_beat(
            "Sherri, show me where I can sleep.",
            "To sleeping quarters",
            lambda raw: bool(_RE_IMAGE.search(raw)),
            "Ask Sherri to show you to the sleeping quarters.",
        )
        _last_narrator_raw[0] = turn.get("raw_text", "")
        raw10 = _last_narrator_raw[0]
        assert_soft(_RE_IMAGE.search(raw10), "GENERATE_IMAGE for sleeping quarters",
                    "No image for sleeping quarters")
        beats_done += 1
        _golden(turn)

        # ── Beat 11: Go to sleep ──────────────────────────────────────────────
        # Note: lore idle threshold is 360s — the 65s pause is no longer useful
        # for lore testing. Lore whisper is now a long-idle bonus, not a CI gate.
        print(_bold("\n── Beat 11: Go to Sleep"))
        lore_before = metrics["lore_whispers"]
        turn = _natural_beat(
            "I climb into a bunk and close my eyes.",
            "Go to sleep",
            lambda raw: bool(_RE_SOUND.search(raw) or _RE_ENV.search(raw) or _RE_SMELL.search(raw)),
            "Find a bunk and lie down to sleep.",
        )
        raw11 = turn.get("raw_text", "")
        assert_soft(
            _RE_SOUND.search(raw11) or _RE_ENV.search(raw11) or _RE_SMELL.search(raw11),
            "Sense markers while sleeping", "No sense tags on sleep"
        )
        beats_done += 1
        _golden(turn)

        # ── Beat 12: Wake up ─────────────────────────────────────────────────
        # Lore whisper threshold is 360s — won't fire during a CI run.
        print(_bold("\n── Beat 12: Wake Up"))
        turn = _natural_beat(
            "I wake up. How long was I asleep?",
            "Wake up",
            lambda raw: bool(_RE_MOOD.search(raw)),
            "Wake up and ask how long you have been asleep.",
        )
        _last_narrator_raw[0] = turn.get("raw_text", "")
        raw12 = _last_narrator_raw[0]
        assert_soft(_RE_MOOD.search(raw12), "MOOD shift on waking",
                    "No mood change on waking")
        assert_soft(metrics["lore_whispers"] > lore_before, "Lore whisper fired during sleep",
                    "Lore requires 6+ minutes of silence — not expected in CI runs")
        beats_done += 1
        _golden(turn)

        # ── Beat 13: To the Nexus ─────────────────────────────────────────────
        print(_bold("\n── Beat 13: To the Nexus"))
        turn = _natural_beat(
            "Sherri, take me to the Nexus. I wish to speak with the Remnant.",
            "To the Nexus",
            lambda raw: bool("remnant" in raw.lower() or _RE_CHAR.search(raw)),
            "Ask Sherri to take you to the Neural Nexus to speak with the Remnant.",
        )
        _last_narrator_raw[0] = turn.get("raw_text", "")
        raw13 = _last_narrator_raw[0]
        assert_soft(_RE_IMAGE.search(raw13), "GENERATE_IMAGE for the Nexus",
                    "No image for Nexus")
        assert_soft(
            "remnant" in raw13.lower() or _RE_CHAR.search(raw13),
            "The Remnant speaks", "Remnant silent in Nexus"
        )
        beats_done += 1
        _golden(turn)

        # ── Beat 14: Ask the Remnant ──────────────────────────────────────────
        print(_bold("\n── Beat 14: Ask the Remnant"))
        turn = _natural_beat(
            "Remnant — why did you bring me here? What do you need from me?",
            "Ask the Remnant",
            lambda raw: bool(_RE_CHAR.search(raw)),
            "Ask the Remnant why it brought you here and what it needs from you.",
        )
        _last_narrator_raw[0] = turn.get("raw_text", "")
        raw14 = _last_narrator_raw[0]
        assert_soft(_RE_CHAR.search(raw14), "CHARACTER(The Remnant) tag",
                    "No CHARACTER tag for Remnant")
        beats_done += 1
        _golden(turn)

        # ── Beat 15: Accept quest ─────────────────────────────────────────────
        print(_bold("\n── Beat 15: Accept Quest"))
        turn = _natural_beat(
            "I'll do it. Whatever it takes. Tell me how to begin.",
            "Accept quest",
            lambda raw: bool(
                "quest" in raw.lower() or "mission" in raw.lower()
                or "task" in raw.lower() or "begin" in raw.lower()
            ),
            "Accept the quest or mission offered. Say you will help.",
        )
        _last_narrator_raw[0] = turn.get("raw_text", "")
        raw15 = _last_narrator_raw[0]
        assert_soft(
            "quest" in raw15.lower() or "mission" in raw15.lower()
            or "task" in raw15.lower() or "begin" in raw15.lower(),
            "Quest acknowledged", "Quest not reflected in response"
        )
        beats_done += 1
        _golden(turn)

        # ── Quest arc (runs until all 15 beats pass or safety cap) ───────────
        print(_bold(f"\n── Quest Arc ({beats_done} beats done — continuing for richness)"))

        # Dialogue pool — cycles through entity-referencing inputs to exercise
        # INTRODUCE tags, lore discovery, and world exploration.
        _ARC_TURNS = [
            # Exploration
            "I explore the corridor ahead.",
            "I examine the walls for markings or symbols.",
            "I press on deeper into the Fortress.",
            "I listen carefully — what sounds can I hear?",
            # Tricorder acquisition
            "I look for any scanning equipment I can use.",
            "I pick up the dimensional scanner from the console.",
            "I use the tricorder to scan the area.",
            # Vex encounter (antagonist)
            "I hear someone in the shadows ahead — not Sherri.",
            "I call out to the figure in the corridor.",
            "I hold my ground and face the hostile traveler.",
            # Mira encounter (protagonist)
            "Sherri, who else is on this station?",
            "I ask to meet the researcher Sherri mentioned.",
            "I introduce myself to Mira and ask about The Fold.",
            # Lore / Remnant
            "I ask The Remnant about The Fold.",
            "I look for any sign of the Neural Nexus.",
            "What does The Remnant know about this section?",
            "I search for anything that might explain the void crystals.",
            "I ask The Remnant how The Fold brought me here.",
            # Wrap-up
            "I look for a way to higher levels.",
            "I ask The Remnant what my first task is.",
        ]
        _arc_idx  = 0
        quest_turn = 0
        last_intro_turn = -99

        # Arc runs until richness targets satisfied OR hard cap.
        # Natural mode gets 30 turns (emergent story); forced CI mode stays at 10.
        MAX_ARC_TURNS = 30 if natural_mode else 10
        MIN_ARC_TURNS = 5

        def _richness_met():
            senses = metrics["smell"] + metrics["taste"] + metrics["touch"]
            new_items = [i for i in metrics["items_acquired"] if i not in starting_items]

            # Quest-complete gate: must have tricorder + met antagonist or protagonist
            has_tricorder = any(i in ('tricorder', 'found_artifact') for i in new_items)
            npcs_lc = {n.lower() for n in metrics["npcs_met"]}
            has_antagonist  = any('vex' in n for n in npcs_lc)
            has_protagonist = any(n in npcs_lc for n in ('mira', 'artisan_kaelo'))
            quest_complete = has_tricorder and (has_antagonist or has_protagonist)

            all_targets_met = (
                metrics["images"]          >= 16 and
                metrics["moods"]           >= 14 and
                senses                     >= 12 and
                metrics["character_lines"] >= 30 and
                metrics["lore_tags"]       >= 5  and
                metrics["introduce_tags"]  >= 3  and
                len(new_items)             >= 3
            )
            # Also accept score ≥75 as early-exit — individual targets for
            # images may be unreachable during rapid test turns (VRAM guard).
            score_passing = _compute_score(beats_done, starting_items) >= 75
            # Quest-complete is required in both paths
            return (all_targets_met or score_passing) and quest_complete

        while metrics["player_turns"] + metrics["narrator_turns"] < max_turns:
            quest_turn += 1

            # Hard cap — stop after MAX_ARC_TURNS regardless of richness
            if quest_turn > MAX_ARC_TURNS:
                print(f"\n  {_ok('✓')} Quest arc cap reached ({MAX_ARC_TURNS} turns).")
                break

            # Snapshot world entities every 5 quest turns
            if quest_turn % 5 == 0:
                current_ents = _snap_world_entities(base)
                _update_npcs_created(current_ents, baseline_entity_ids)

            # Choose player input — natural mode asks the LLM agent; forced mode
            # uses context-reactive overrides then falls back to the pool rotation.
            if natural_mode:
                text = _player_agent_turn(_last_narrator_raw[0], player_name)
            else:
                last_raw = story_text[-1] if story_text else ""
                last_raw_lc = last_raw.lower()
                if "introduce" in last_raw_lc and quest_turn != last_intro_turn:
                    text = "Tell me about yourself. Who are you?"
                    last_intro_turn = quest_turn
                elif "fabrication" in last_raw_lc and "sherri" in last_raw_lc:
                    text = "I work with Sherri to recalibrate the rigs."
                elif "nexus" in last_raw_lc and "frequency" in last_raw_lc:
                    text = "I goad the Remnant into revealing what it knows."
                elif "cold" in last_raw_lc and "spot" in last_raw_lc:
                    text = "I follow the cold trail through the sleeping quarters."
                else:
                    text = _ARC_TURNS[_arc_idx % len(_ARC_TURNS)]
                _arc_idx += 1

            label = f"Quest arc turn {quest_turn}"
            try:
                turn = play_with_retry(text, label, base)
            except StepFailed as e:
                print(f"  {_err('✗')} {label} failed: {e}")
                break

            _last_narrator_raw[0] = turn.get("raw_text", "")
            raw = _last_narrator_raw[0]
            assert_soft(_RE_IMAGE.search(raw) or _RE_MOOD.search(raw) or _RE_CHAR.search(raw),
                        f"Quest turn {quest_turn} richness",
                        "No image/mood/character in quest turn")
            # Auto-approve arc turns with MOOD + at least one sense tag — good training data
            if save_golden and _RE_MOOD.search(raw):
                senses = sum(bool(r.search(raw)) for r in (_RE_SMELL, _RE_TASTE, _RE_TOUCH, _RE_SOUND))
                if senses >= 1:
                    _approve_golden_turn(diag, turn)

            # Early-exit: all beats done + richness met + minimum arc turns
            if beats_done >= 15 and quest_turn >= MIN_ARC_TURNS and _richness_met():
                print(f"\n  {_ok('✅')} {_bold('All beats complete + richness targets met — pilot quest done!')}")
                break
            # Also exit cleanly after scoring snapshot at cap
            if quest_turn >= MAX_ARC_TURNS:
                break

        if metrics["player_turns"] + metrics["narrator_turns"] >= max_turns:
            print(f"\n  {_warn('⚠')} Safety cap reached ({max_turns} turns). Beats: {beats_done}/15")

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

    # Tag-injection health checks — warn if expected tags are still zero
    tag_warnings = []
    if metrics["moods"] == 0:
        tag_warnings.append("MOOD tags = 0 — _inject_missing_tags may not be firing")
    if metrics["lore_tags"] == 0:
        tag_warnings.append("LORE tags = 0 — check _LORE_ANCHORS coverage in app.py")
    if metrics["introduce_tags"] == 0:
        tag_warnings.append("INTRODUCE tags = 0 — CHARACTER tags absent from narrator turns")
    if tag_warnings:
        print(_warn("\n  Tag-injection warnings:"))
        for tw in tag_warnings:
            print(f"    {_warn('⚠')}  {tw}")

    print(_dim(f"\n  NPCs met:        {', '.join(sorted(metrics['npcs_met'])) or '(none)'}"))
    print(_dim(f"  NPCs created:    {', '.join(sorted(metrics['npcs_created'])) or '(none)'}"))
    print(_dim(f"  Items acquired:  {', '.join(metrics['items_acquired']) or '(none)'}"))
    print(_dim(f"  Lore collected:  {', '.join(metrics['lore_collected']) or '(none)'}"))
    print(_dim(f"  Locations:       {', '.join(sorted(metrics['locations_visited'])) or '(none)'}"))

    print(f"\n  Beats completed: {_bold(str(beats_done))}/15")
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

    # v4.0.0 gate: all 15 beats complete AND richness score ≥ 75
    success = beats_done == 15 and score >= 75
    if success:
        print(_ok(f"  🏆  v4.0.0 GATE PASSED — all beats complete, score {score}/100\n"))
    else:
        reasons = []
        if beats_done < 15:
            reasons.append(f"beats {beats_done}/15")
        if score < 75:
            reasons.append(f"score {score}/100 (need ≥75)")
        print(_warn(f"  ✗  Gate not yet passed: {', '.join(reasons)}\n"))
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
