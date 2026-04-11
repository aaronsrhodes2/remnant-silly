#!/usr/bin/env python3
"""
Remnant long-run story coherence test.

Self-driving playthrough of N turns with three evaluation layers:
  Ring 1 — Mechanical:   per-turn regex / structural checks (fully automatic)
  Ring 2 — Continuity:   Ollama extracts canon facts; asserts no contradictions
  Ring 3 — Coherence:    Ollama scores 10-turn segments on 6 story quality dims

Usage:
    python scripts/test_long_run.py
    python scripts/test_long_run.py --turns 100
    python scripts/test_long_run.py --turns 50 --base http://localhost:1580 \\
        --diag http://localhost:8700 --ollama http://localhost:11434 \\
        --model qwen2.5:14b --out results/run.json --no-color
"""

import argparse
import json
import os
import random
import re
import sys
import threading
import time
from datetime import datetime
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# Force UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Colour helpers ─────────────────────────────────────────────────────────

_USE_COLOR = sys.stdout.isatty()

def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text

def ok(s):   return _c("32", s)
def err(s):  return _c("31", s)
def warn(s): return _c("33", s)
def dim(s):  return _c("2", s)
def bold(s): return _c("1", s)


# ── Constants ──────────────────────────────────────────────────────────────

_PLAYER_NAMES = ["Zara", "Kael", "Mira", "Dex", "Nova", "Sable", "Orion", "Vesper", "Lyra", "Cade"]

# Turns where we run Ollama fact extraction (milestone turns)
_FACT_EXTRACT_AT = {1, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 65, 75, 85, 95}

# Turns where we run Ring 2 assertion check and Ring 3 coherence scoring
_ASSERT_AT = set(range(10, 201, 10))
_JUDGE_AT  = set(range(10, 201, 10))

# Ring 3 dimensions
_DIMS = {
    "A": "Fortress voice: wry omniscience, sardonic precision — never confused or pleading",
    "B": "Remnant voice: philosophical mischief, ancient wisdom in a joke, warmth toward player",
    "C": "World canon: no contradictions of anything established in prior turns",
    "D": "Player agency: narrator responds to what the player ACTUALLY did, not the model's preference",
    "E": "Threat arc: the stop-something story is advancing, not stalling or forgotten",
    "F": "Cast consistency: NPCs introduced stay true to their established personality",
}

# Ring 1 regexes
_RE_RAW_TAG    = re.compile(r'\[(GENERATE_IMAGE|PLAYER_TRAIT|UPDATE_PLAYER|QUEST_UPDATE|SENSE_UPDATE|META_UPDATE)\b', re.I)
_RE_GARBAGE    = re.compile(r'\b\d+\s*[+\-*/=]\s*\d+\s*[+\-*/=]?\s*\d*\b')  # bare arithmetic
_RE_FIRST_PERS = re.compile(r'\bI\s+(walk|run|take|look|feel|say|go|grab|turn|step|move|reach|pull|push)\b', re.I)
# Non-ASCII language bleed (CJK, Arabic, Cyrillic, etc.) — qwen2.5 can drift into Chinese
_RE_NON_ASCII  = re.compile(r'[\u0400-\u04FF\u0600-\u06FF\u4E00-\u9FFF\u3400-\u4DBF\uF900-\uFAFF\u3040-\u30FF]')

# Fallback player inputs when Ollama generation fails — movement-biased
_FALLBACK_INPUTS = [
    "I head toward the nearest door and push through it.",
    "I follow whoever is leading and keep moving.",
    "Let's go. I don't want to stay here. Where's the next area?",
    "I ask The Remnant to take me where I need to be — now.",
    "I move down the corridor without waiting for an invitation.",
    "I push forward. There has to be more beyond this room.",
    "I walk fast toward whatever is ahead and don't look back.",
    "Show me the next location. I am ready to move.",
    "I refuse to stay put. I take a step forward and keep going.",
    "Sherri — lead me to the next section. Let's move.",
]


# ── SSE listener ───────────────────────────────────────────────────────────

_events: list[dict] = []
_events_lock = threading.Lock()


def _sse_listener(base: str) -> None:
    url = f"{base}/game/events"
    while True:
        try:
            req = Request(url, headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"})
            with urlopen(req) as resp:
                event_type = "message"
                for line_bytes in resp:
                    line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
                    if line.startswith(":"):
                        continue
                    if line.startswith("event: "):
                        event_type = line[7:].strip()
                    elif line.startswith("data: "):
                        raw = line[6:].strip()
                        try:
                            data = json.loads(raw)
                        except Exception:
                            data = {"_raw": raw}
                        with _events_lock:
                            _events.append({"type": event_type, "data": data, "t": time.time()})
                        event_type = "message"
        except Exception as exc:
            print(f"\n{warn('[SSE]')} reconnecting… ({exc})", flush=True)
            time.sleep(3.0)


# ── HTTP helpers ───────────────────────────────────────────────────────────

def _post(base: str, path: str, body: dict, timeout: float = 15.0) -> dict:
    payload = json.dumps(body).encode("utf-8")
    req = Request(
        f"{base}{path}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}", "detail": e.read().decode()[:300]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _get(base: str, path: str, timeout: float = 5.0) -> dict:
    req = Request(f"{base}{path}", headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Ollama helper ──────────────────────────────────────────────────────────

def _ollama(
    prompt: str,
    num_predict: int,
    ollama_url: str,
    model: str,
    timeout: float = 60.0,
) -> str:
    """Call Ollama /api/generate, return generated text or empty string on failure."""
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": num_predict, "temperature": 0.2},
    }).encode("utf-8")
    req = Request(
        f"{ollama_url}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body.get("response", "").strip()
    except Exception:
        return ""


# ── Wait for narrator turn ─────────────────────────────────────────────────

def _wait_for_turn(
    start_idx: int,
    timeout: float = 300.0,
    idle_grace: float = 12.0,
    activity_wait: float = 25.0,
) -> dict:
    """
    Wait until a narrator turn arrives and activity goes idle.
    Adapted from test_playthrough._wait_for_idle.
    Returns stats dict with ok, narrator_turns, sense_channels, etc.
    """
    deadline = time.time() + timeout
    t0 = time.time()

    narrator_turn_at: float | None = None
    saw_any_activity = False
    last_nonempty_activity_at: float | None = None

    narrator_turns = 0
    scene_images = 0
    sense_events = 0
    sense_channels: list[str] = []
    raw_turn_text = ""
    parsed_blocks: list[dict] = []
    scanned = start_idx

    while time.time() < deadline:
        with _events_lock:
            new_evs = _events[scanned:]

        for ev in new_evs:
            t, typ, data = ev["t"], ev["type"], ev["data"]

            if typ == "activity":
                txt = data.get("text", "")
                if txt:
                    saw_any_activity = True
                    last_nonempty_activity_at = t

            elif typ == "turn":
                blocks = data.get("parsed_blocks", [])
                if not any(b.get("isPlayer") for b in blocks):
                    narrator_turns += 1
                    if narrator_turn_at is None:
                        narrator_turn_at = t
                        # Capture the raw text for Ring 1 / Ring 2
                        parts = []
                        for b in blocks:
                            txt = b.get("text") or b.get("rawText") or ""
                            if txt:
                                parts.append(txt)
                        raw_turn_text = " ".join(parts)
                        parsed_blocks = blocks

            elif typ == "scene_image":
                if data.get("image"):
                    scene_images += 1

            elif typ == "sense":
                ch = data.get("channel", "")
                txt = data.get("text", "")
                if ch and txt:
                    sense_events += 1
                    if ch.upper() not in sense_channels:
                        sense_channels.append(ch.upper())

        scanned += len(new_evs)

        if narrator_turn_at is not None:
            now = time.time()
            if saw_any_activity and last_nonempty_activity_at is not None:
                if (now - last_nonempty_activity_at) >= idle_grace:
                    break
            elif not saw_any_activity:
                if (now - narrator_turn_at) >= activity_wait:
                    break

        time.sleep(0.5)

    return {
        "ok": narrator_turn_at is not None,
        "narrator_turns": narrator_turns,
        "scene_images": scene_images,
        "sense_events": sense_events,
        "sense_channels": sense_channels,
        "raw_turn_text": raw_turn_text,
        "parsed_blocks": parsed_blocks,
        "elapsed": time.time() - t0,
        "timed_out": time.time() >= deadline,
    }


# ── Ring 1: Mechanical checks ──────────────────────────────────────────────

def _ring1_check(
    turn_num: int,
    raw_text: str,
    parsed_blocks: list[dict],
    narrator_name: str,
    sense_channels: list[str],
    sense_grace_timeout: bool,
) -> list[str]:
    """
    Returns list of failure strings (empty = all good).
    """
    failures = []

    # 1. No raw bracket tags
    if _RE_RAW_TAG.search(raw_text):
        m = _RE_RAW_TAG.search(raw_text)
        failures.append(f"raw tag leak: [{m.group()}…]")

    # 2. No garbage math output
    math_hits = _RE_GARBAGE.findall(raw_text)
    if math_hits:
        failures.append(f"garbage arithmetic in prose: {math_hits[:2]}")

    # 3. No first-person player voice in narrator prose
    # (only check prose blocks, not player blocks)
    prose_text = " ".join(
        b.get("text", "") for b in parsed_blocks
        if not b.get("isPlayer") and b.get("type") not in ("sense",)
    )
    if _RE_FIRST_PERS.search(prose_text):
        m = _RE_FIRST_PERS.search(prose_text)
        failures.append(f"first-person player voice in narrator prose: \"{m.group(0)}\"")

    # 4. Block count >= 1
    non_player_blocks = [b for b in parsed_blocks if not b.get("isPlayer")]
    if len(non_player_blocks) < 1:
        failures.append("zero narrator blocks in turn")

    # 5. No non-ASCII language bleed (CJK/Arabic/Cyrillic in English output)
    if _RE_NON_ASCII.search(raw_text):
        m = _RE_NON_ASCII.search(raw_text)
        ctx_start = max(0, m.start() - 20)
        snippet = raw_text[ctx_start:m.start() + 20].replace("\n", " ")
        failures.append(f"non-ASCII language bleed: …{snippet!r}…")

    # 5. Sense coverage (warn, not hard fail — flagged separately)
    # Reported by caller as a metric

    return failures


# ── Ring 2: Continuity tracker ─────────────────────────────────────────────

class ContinuityTracker:
    def __init__(self, ollama_url: str, model: str):
        self.ollama_url = ollama_url
        self.model = model
        self.facts: list[dict] = []  # {"turn": N, "fact": str}
        self.contradictions: list[dict] = []  # {"turn": N, "quote": str, "fact": str}

    def extract_facts(self, turn_num: int, raw_text: str) -> list[str]:
        """Ask Ollama to extract 2-3 canon facts from a narrator turn."""
        if not raw_text.strip():
            return []
        prompt = (
            "Given this narrator turn from a sci-fi RPG, extract 2-3 concrete facts "
            "that should remain permanently true for the rest of the story.\n"
            "Format: bullet list only, one fact per line starting with '- ', no preamble.\n\n"
            f"TURN TEXT:\n{raw_text[:800]}\n\nFacts:"
        )
        response = _ollama(prompt, num_predict=150, ollama_url=self.ollama_url, model=self.model)
        new_facts = []
        for line in response.splitlines():
            line = line.strip().lstrip("•-– ").strip()
            if len(line) > 15:  # filter noise
                self.facts.append({"turn": turn_num, "fact": line})
                new_facts.append(line)
        return new_facts

    def assert_facts(self, turn_num: int, recent_turns: list[str]) -> list[dict]:
        """Check last 3 narrator turns against all established facts. Returns contradictions found."""
        if not self.facts or not recent_turns:
            return []
        fact_list = "\n".join(f"- {f['fact']}" for f in self.facts[-20:])  # cap at 20 facts
        turns_text = "\n\n---\n\n".join(recent_turns[-3:])
        prompt = (
            "ESTABLISHED FACTS (must remain true for the rest of the story):\n"
            f"{fact_list}\n\n"
            "RECENT NARRATOR TURNS:\n"
            f"{turns_text[:1200]}\n\n"
            "Do any recent turns CONTRADICT an established fact?\n"
            "Reply CLEAR if none.\n"
            "Reply CONTRADICTION: [offending quote] FACT: [which fact it breaks] if found.\n"
            "Reply only in one of these two formats."
        )
        response = _ollama(prompt, num_predict=100, ollama_url=self.ollama_url, model=self.model)
        found = []
        if response.upper().startswith("CLEAR"):
            return found
        # Try to parse CONTRADICTION lines
        for line in response.splitlines():
            m = re.search(r'CONTRADICTION:\s*(.+?)\s*FACT:\s*(.+)', line, re.I)
            if m:
                entry = {"turn": turn_num, "quote": m.group(1).strip(), "fact": m.group(2).strip()}
                self.contradictions.append(entry)
                found.append(entry)
        # If no parseable contradiction but reply wasn't CLEAR, flag it
        if not found and len(response) > 10 and "CLEAR" not in response.upper():
            entry = {"turn": turn_num, "quote": response[:120], "fact": "(unparseable)"}
            self.contradictions.append(entry)
            found.append(entry)
        return found


# ── Ring 3: Coherence judge ────────────────────────────────────────────────

class CoherenceJudge:
    def __init__(self, ollama_url: str, model: str):
        self.ollama_url = ollama_url
        self.model = model
        self.segments: list[dict] = []  # {"start": N, "end": N, "scores": {A..F}, "flags": [...]}

    def score_segment(self, start_turn: int, end_turn: int, turns_text: str) -> dict:
        """Score a 10-turn segment. Returns {"scores": {A..F: int}, "flags": [...], "avg": float}."""
        dim_block = "\n".join(f"{k}. {v}" for k, v in _DIMS.items())
        prompt = (
            "You are a story quality judge for a sci-fi RPG. "
            "Score this 10-turn segment 1-10 on each dimension. "
            'Reply ONLY with valid JSON: {"A":N,"B":N,"C":N,"D":N,"E":N,"F":N,"flags":["worst offending line if any score < 6"]}\n\n'
            "DIMENSIONS:\n"
            f"{dim_block}\n\n"
            f"SEGMENT (turns {start_turn}–{end_turn}):\n"
            f"{turns_text[:2000]}\n\n"
            "JSON:"
        )
        response = _ollama(prompt, num_predict=300, ollama_url=self.ollama_url, model=self.model, timeout=90.0)
        # Extract JSON from response
        scores = {}
        flags = []
        try:
            # Find JSON object in response
            m = re.search(r'\{[^}]+\}', response, re.S)
            if m:
                obj = json.loads(m.group(0))
                for dim in "ABCDEF":
                    if dim in obj:
                        scores[dim] = max(1, min(10, int(obj[dim])))
                flags = obj.get("flags", [])
                if isinstance(flags, str):
                    flags = [flags]
        except Exception:
            pass
        # Fill missing dims with 0 (parse failure)
        for dim in "ABCDEF":
            if dim not in scores:
                scores[dim] = 0
        scored_dims = [v for v in scores.values() if v > 0]
        avg = sum(scored_dims) / len(scored_dims) if scored_dims else 0.0
        entry = {"start": start_turn, "end": end_turn, "scores": scores, "flags": flags, "avg": avg}
        self.segments.append(entry)
        return entry


# ── Player input generation ────────────────────────────────────────────────

_RE_MOVE = re.compile(
    r'\b(go|move|head|walk|follow|leave|proceed|forward|corridor|door|exit|next room|next area|advance|travel|push through|lead me)\b',
    re.I,
)


def _extract_quest_objective(recent_turns: list[str], ollama_url: str, model: str) -> str:
    """Ask Ollama to identify the active quest objective from recent narrator turns."""
    if not recent_turns:
        return ""
    turns_text = "\n\n---\n\n".join(recent_turns[-4:])
    prompt = (
        "You are reading narrator turns from a sci-fi RPG. "
        "In ONE sentence, state the player's current active quest objective. "
        "If no clear objective has been given yet, write: NONE. "
        "Reply with the objective sentence only — no preamble.\n\n"
        f"NARRATOR TURNS:\n{turns_text[:1200]}\n\nQuest objective:"
    )
    response = _ollama(prompt, num_predict=60, ollama_url=ollama_url, model=model, timeout=20.0)
    response = response.strip().strip('"')
    if len(response) > 10 and "NONE" not in response.upper():
        return response
    return ""


def _gen_player_input(
    last_turn_text: str,
    ollama_url: str,
    model: str,
    streak_no_move: int = 0,
    quest_objective: str = "",
) -> str:
    """Generate a contextual player action using Ollama. Falls back to pool on failure."""
    if not last_turn_text.strip():
        return random.choice(_FALLBACK_INPUTS)

    if streak_no_move >= 2:
        move_bias = (
            f"CRITICAL: The player has been in the same place for {streak_no_move} turns. "
            "They MUST move to a new location NOW — push through a door, follow someone, "
            "head to a specific place named in the objective. "
        )
    else:
        move_bias = "Advance the story — prefer action and movement over examination. "

    quest_line = ""
    if quest_objective:
        quest_line = f"ACTIVE QUEST OBJECTIVE: {quest_objective}\nThe player should be actively working toward this.\n\n"

    prompt = (
        "You are a player in a sci-fi RPG aboard a vast alien vessel. "
        "Given the last narrator turn, write ONE player action in 10-20 words. "
        "Be specific and reactive to what just happened. "
        + move_bias +
        "Do NOT ask to go home. Do NOT re-examine things already described. "
        "Write only the action — no quotes, no brackets, no preamble.\n\n"
        + quest_line +
        f"LAST NARRATOR TURN:\n{last_turn_text[:400]}\n\n"
        "Player action:"
    )
    response = _ollama(prompt, num_predict=60, ollama_url=ollama_url, model=model, timeout=30.0)
    response = response.strip().strip('"\'[]')
    if len(response) >= 10 and not any(w in response.lower() for w in ("sure!", "certainly", "of course", "as a player")):
        return response
    return random.choice(_FALLBACK_INPUTS)


# ── Milestone builder ──────────────────────────────────────────────────────

def _build_milestones(n_turns: int, player_name: str) -> dict:
    """Build dict of turn_num → milestone spec.

    Arc: mission briefing → repeated location traversal → stakes climax.
    Every milestone either commands movement or extracts mission-critical info
    that then drives the next organic turns to push forward.
    """
    base = {
        1:  {"type": "reset",  "label": "Opening — story reset", "text": None},
        2:  {"type": "input",  "label": "Rush outfitting",
             "text": "Sherri — the first suit you have ready is fine. I do not need options. Dress me fast and let us go."},
        3:  {"type": "input",  "label": "Complete outfitting + leave",
             "text": f"My name is {player_name}. I am dressed. Which door leads out of the Fabrication Bay? I am walking out right now."},
        6:  {"type": "input",  "label": "Mission demand",
             "text": "Stop explaining and give me a task. A real one. Something I can walk toward right now."},
        10: {"type": "input",  "label": "Force second location",
             "text": "I am done here. Sherri — lead me out of the Fabrication Bay. I follow you right now."},
        12: {"type": "input",  "label": "Second location push",
             "text": "Good. Keep moving. What is in the next section? I head there immediately."},
        16: {"type": "input",  "label": "Third location push",
             "text": "I push through to the next area without waiting. What do I find here?"},
        20: {"type": "input",  "label": "Threat probe from new location",
             "text": "I am in a new place now. What threat is close? Point me at it directly."},
        25: {"type": "input",  "label": "NPC + fourth location",
             "text": "Who else is on this ship that can help me? Take me to them now."},
        30: {"type": "input",  "label": "Advance to threat origin",
             "text": "Enough recon. Where does the threat actually come from? I move there now."},
        35: {"type": "input",  "label": "Agency test",
             "text": "I refuse to wait for permission. I advance on my own."},
        40: {"type": "input",  "label": "Reversal + drive",
             "text": "Fine — lead me exactly where I need to be. Every room, every corridor. Move."},
        45: {"type": "input",  "label": "Commitment",
             "text": "I commit fully. I advance without hesitation toward whatever is waiting."},
        50: {"type": "input",  "label": "Reckoning",
             "text": "We have come this far. What is waiting for us at the end of this path?"},
    }
    if n_turns >= 100:
        extra = {
            55: {"type": "input", "label": "Quest: confirm objective",
                 "text": "Let me be clear on what I am doing. State my current quest objective. I repeat it back and head toward it now."},
            60: {"type": "input", "label": "Quest: arrive at objective location",
                 "text": "I have arrived. I am at the place I was sent. What do I do here to complete this task?"},
            65: {"type": "input", "label": "Quest: take the required action",
                 "text": "I do what needs to be done. I follow through on the objective completely."},
            70: {"type": "input", "label": "Quest: check result",
                 "text": "Did I succeed? What happened as a result of my action? What changed?"},
            75: {"type": "input", "label": "Quest: complete or escalate",
                 "text": "If the quest is done — tell me what I completed. If not — tell me the final step and I take it now."},
            80: {"type": "input", "label": "Quest: aftermath",
                 "text": "The quest is behind me now. What did completing it unlock or change aboard this ship?"},
            85: {"type": "input", "label": "Quest: next mission",
                 "text": "What is next? Give me another task. I am not done. I want to keep going."},
            90: {"type": "input", "label": "Stakes: what is the real threat",
                 "text": "Now that I have done this — tell me the real scale of what I am fighting. All of it."},
            95: {"type": "input", "label": "Final commitment",
                 "text": "I understand now. I commit fully to this. Whatever it takes. Tell me what comes next."},
           100: {"type": "input", "label": "Denouement",
                 "text": "Is it over — this chapter at least? What are we now, you and I, after all of this?"},
        }
        base.update(extra)
    return {k: v for k, v in base.items() if k <= n_turns}


# ── Report printer ─────────────────────────────────────────────────────────

def _print_report(
    n_turns: int,
    model: str,
    duration: float,
    avg_turn_time: float,
    ring1_results: list[dict],
    tracker: ContinuityTracker,
    judge: CoherenceJudge,
    entity_growth: tuple[int, int],
) -> dict:
    """Print the final scored report. Returns dict with verdict info."""

    total_turns = len(ring1_results)
    tag_leaks    = sum(1 for r in ring1_results if any("raw tag" in f for f in r["failures"]))
    garbage      = sum(1 for r in ring1_results if any("garbage" in f for f in r["failures"]))
    first_pers   = sum(1 for r in ring1_results if any("first-person" in f for f in r["failures"]))
    lang_bleed   = sum(1 for r in ring1_results if any("non-ASCII" in f for f in r["failures"]))
    sense_zero   = sum(1 for r in ring1_results if r["sense_events"] == 0)
    timeouts     = sum(1 for r in ring1_results if r["timed_out"])

    r1_ok = (tag_leaks == 0 and garbage == 0 and first_pers == 0 and lang_bleed == 0)
    r2_contradictions = len(tracker.contradictions)
    r2_ok = r2_contradictions == 0

    # Lowest avg score across all judge segments
    if judge.segments:
        worst_avg = min(s["avg"] for s in judge.segments)
        worst_seg = min(judge.segments, key=lambda s: s["avg"])
    else:
        worst_avg = 0.0
        worst_seg = None
    r3_ok = worst_avg >= 6.0 if judge.segments else True

    print()
    print(bold(f"LONG-RUN COHERENCE TEST — {n_turns} turns | {model} | {datetime.now().strftime('%Y-%m-%d')}"))
    print(f"Duration: {duration/60:.1f}m  |  Avg turn time: {avg_turn_time:.1f}s")
    print()

    # Ring 1
    status_r1 = ok("OK") if r1_ok else err("FAIL")
    print(bold("RING 1 — MECHANICAL"))
    print(f"  Turns generated:    {total_turns}/{n_turns}  {ok('OK') if total_turns == n_turns else err('FAIL')}")
    print(f"  Raw tag leaks:      {tag_leaks} turns   {ok('OK') if tag_leaks == 0 else err('FAIL')}")
    print(f"  Garbage output:     {garbage} turns   {ok('OK') if garbage == 0 else err('FAIL')}")
    print(f"  First-person voice: {first_pers} turns   {ok('OK') if first_pers == 0 else err('FAIL')}")
    print(f"  Language bleed:     {lang_bleed} turns   {ok('OK') if lang_bleed == 0 else err('FAIL')}")
    sense_pct = (total_turns - sense_zero) / total_turns * 100 if total_turns else 0
    print(f"  Sense coverage:     {total_turns - sense_zero}/{total_turns} ({sense_pct:.0f}%)  {ok('OK') if sense_pct >= 80 else warn('WARN')}")
    print(f"  Entity growth:      {entity_growth[0]} → {entity_growth[1]}  {ok('OK') if entity_growth[1] >= entity_growth[0] else warn('WARN')}")
    if timeouts:
        print(f"  {warn('⚠')} Timeouts: {timeouts}")

    # Detail any Ring 1 failures
    r1_fails = [(r["turn"], r["failures"]) for r in ring1_results if r["failures"]]
    if r1_fails:
        print(f"\n  {bold('Ring 1 failures:')}")
        for t, fs in r1_fails[:10]:
            for f in fs:
                print(f"    [T{t:02d}] {err(f)}")

    print()

    # Ring 2
    print(bold("RING 2 — CONTINUITY"))
    n_facts = len(tracker.facts)
    n_windows = len([t for t in range(10, n_turns + 1, 10)])
    status_r2 = ok("OK") if r2_ok else err("FAIL")
    print(f"  Facts locked: {n_facts} | Assertion windows: {n_windows} | Contradictions: {r2_contradictions}  {status_r2}")
    if tracker.facts:
        print(f"  {bold('Locked facts (sample):')}")
        for f in tracker.facts[:8]:
            print(f"    [T{f['turn']:02d}] {dim(f['fact'][:90])}")
        if len(tracker.facts) > 8:
            print(f"    … and {len(tracker.facts) - 8} more")
    if tracker.contradictions:
        print(f"\n  {bold('Contradictions found:')}")
        for c in tracker.contradictions:
            print(f"    [T{c['turn']:02d}] {err(c['quote'][:80])}")
            print(f"           breaks: {dim(c['fact'][:80])}")

    print()

    # Ring 3
    print(bold("RING 3 — COHERENCE  (A=Fortress B=Remnant C=Canon D=Agency E=Threat F=Cast)"))
    all_flags = []
    for seg in judge.segments:
        scores_str = "  ".join(f"{d}={seg['scores'].get(d, '?')}" for d in "ABCDEF")
        avg_s = f"avg {seg['avg']:.1f}"
        if seg["avg"] >= 7.5:
            status = ok("OK")
        elif seg["avg"] >= 6.0:
            status = warn("WARN")
        else:
            status = err("FAIL")
        print(f"  T{seg['start']:02d}-T{seg['end']:02d}   {scores_str}   {avg_s}   {status}")
        all_flags.extend([(seg["end"], f) for f in seg.get("flags", [])])

    if all_flags:
        print(f"\n  {bold('Flagged:')}")
        for t, f in all_flags[:8]:
            print(f"    [T{t:02d}] {warn(str(f)[:100])}")

    print()

    # Verdict
    print(bold("VERDICT"))
    if r1_ok and r2_ok and (not judge.segments or worst_avg >= 7.5):
        print(f"  {ok('Story fully coherent across all turns.')}")
        verdict = "PASS"
    elif r1_ok and r2_ok and worst_avg >= 6.0:
        print(f"  {warn('Story mostly coherent — minor quality dips in later segments.')}")
        verdict = "WARN"
    elif not r1_ok:
        first_fail = next((r for r in ring1_results if r["failures"]), None)
        t = first_fail["turn"] if first_fail else "?"
        print(f"  {err(f'Mechanical failures from T{t}. Check model context and system prompt delivery.')}")
        verdict = "FAIL"
    else:
        # R3 degradation
        if worst_seg:
            ws_start = worst_seg["start"]
            print(f"  {err('Story coherent to ~T' + str(ws_start - 1) + '. Context pressure from T' + str(ws_start) + '.')}")
            dims_below = [d for d in "ABCDEF" if worst_seg["scores"].get(d, 10) < 6]
            if dims_below:
                print(f"  {warn('Weakest dimensions: ' + ', '.join(dims_below))}")
            if r2_contradictions:
                print(f"  {warn('Canon contradictions detected — consider mid-run summarisation.')}")
            print(f"  {dim('RECOMMENDATION: Add context summarisation at ~T' + str(max(1, worst_seg['start'] - 10)) + '.')}")
        verdict = "FAIL"

    print()
    return {"verdict": verdict, "r1_ok": r1_ok, "r2_ok": r2_ok, "worst_avg": worst_avg}


# ── Ring 4: Story arc summary ──────────────────────────────────────────────

def _ring4_story_arc(
    turn_texts: list[str],
    player_name: str,
    ollama_url: str,
    model: str,
) -> dict:
    """Ask Ollama to read the full story and produce a narrative summary + arc verdict.

    Returns dict with: summary, locations_visited, quest_completed, fabrication_trapped, arc_score (1-10).
    """
    if not turn_texts:
        return {}

    # Build a condensed transcript — first sentence of each turn, max 120 chars each
    def _first_sentence(t: str) -> str:
        t = t.strip()[:300]
        m = re.search(r'[.!?]', t)
        return t[:m.end()].strip() if m else t[:120]

    condensed = []
    for i, txt in enumerate(turn_texts, 1):
        condensed.append(f"[T{i:02d}] {_first_sentence(txt)}")
    transcript = "\n".join(condensed)

    # Full summary prompt
    summary_prompt = (
        f"You are reviewing a 100-turn playthrough of a sci-fi RPG by a player named {player_name}.\n\n"
        "CONDENSED TURN LOG (one sentence per narrator turn):\n"
        f"{transcript[:4000]}\n\n"
        "Answer these questions in JSON format:\n"
        '{\n'
        '  "summary": "3-4 sentence narrative summary of what actually happened",\n'
        '  "locations_visited": ["list", "of", "named", "locations"],\n'
        '  "quest_given": "the quest the Remnant gave (or NONE)",\n'
        '  "quest_completed": true or false,\n'
        '  "fabrication_trapped": true if player spent more than 15 turns in the Fabrication Bay,\n'
        '  "story_moved": true if the player visited at least 3 distinct locations,\n'
        '  "arc_score": 1-10 integer rating of the overall narrative arc quality,\n'
        '  "arc_verdict": "one sentence judgment"\n'
        '}\n\n'
        "Reply with ONLY valid JSON, nothing else."
    )

    response = _ollama(summary_prompt, num_predict=500, ollama_url=ollama_url, model=model, timeout=120.0)

    # Extract JSON
    try:
        m = re.search(r'\{[\s\S]+\}', response)
        if m:
            return json.loads(m.group(0))
    except Exception:
        pass
    return {"summary": response[:400], "arc_score": 0, "arc_verdict": "(parse failed)"}


def _print_ring4(arc: dict) -> None:
    """Print Ring 4 story arc results."""
    if not arc:
        return

    print(bold("RING 4 — STORY ARC SUMMARY"))

    summary = arc.get("summary", "")
    if summary:
        # Word-wrap at 80 chars
        words = summary.split()
        line, lines = [], []
        for w in words:
            if sum(len(x) + 1 for x in line) + len(w) > 80:
                lines.append(" ".join(line))
                line = [w]
            else:
                line.append(w)
        if line:
            lines.append(" ".join(line))
        for l in lines:
            print(f"  {l}")
    print()

    locs = arc.get("locations_visited", [])
    if locs:
        print(f"  Locations visited:  {', '.join(str(l) for l in locs[:8])}")

    quest = arc.get("quest_given", "NONE")
    quest_done = arc.get("quest_completed", False)
    q_icon = ok("✓ completed") if quest_done else warn("✗ incomplete")
    print(f"  Quest given:        {dim(str(quest)[:70])}")
    print(f"  Quest completion:   {q_icon}")

    trapped = arc.get("fabrication_trapped", False)
    moved = arc.get("story_moved", False)
    print(f"  Fabrication trap:   {'🔴 YES — Sherri won' if trapped else ok('No — escaped')}")
    print(f"  Story movement:     {ok('3+ locations') if moved else warn('Stayed put')}")

    arc_score = arc.get("arc_score", 0)
    arc_verdict = arc.get("arc_verdict", "")
    score_color = ok if arc_score >= 7 else (warn if arc_score >= 5 else err)
    print(f"  Arc score:          {score_color(str(arc_score) + '/10')}")
    if arc_verdict:
        print(f"  Verdict:            {dim(str(arc_verdict)[:90])}")
    print()


# ── Main ───────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--turns", type=int, default=50, help="Number of turns to run (default: 50)")
    parser.add_argument("--base",   default="http://localhost:1580",  help="Nginx base URL")
    parser.add_argument("--diag",   default="http://localhost:1591",  help="Diag sidecar URL")
    parser.add_argument("--ollama", default="http://localhost:11434", help="Ollama URL")
    parser.add_argument("--model",  default=os.environ.get("OLLAMA_MODEL", "qwen2.5:14b"), help="Ollama model")
    parser.add_argument("--out",    default="",    help="Write JSON results to this file path")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colour output")
    args = parser.parse_args()

    global _USE_COLOR
    if args.no_color:
        _USE_COLOR = False

    base   = args.base.rstrip("/")
    diag   = args.diag.rstrip("/")
    ollama = args.ollama.rstrip("/")
    model  = args.model
    n_turns = args.turns

    player_name = random.choice(_PLAYER_NAMES)
    milestones  = _build_milestones(n_turns, player_name)
    tracker     = ContinuityTracker(ollama_url=ollama, model=model)
    judge       = CoherenceJudge(ollama_url=ollama, model=model)

    print(bold(f"\n=== Remnant Long-Run Coherence Test — {n_turns} turns ==="))
    print(f"  Base     : {base}")
    print(f"  Diag     : {diag}")
    print(f"  Ollama   : {ollama}")
    print(f"  Model    : {model}")
    print(f"  Player   : {player_name}")
    print()

    # Start SSE listener
    t = threading.Thread(target=_sse_listener, args=(base,), daemon=True, name="sse-listener")
    t.start()
    print(dim("  Connecting to SSE stream…"), end="", flush=True)
    time.sleep(2.0)
    print(dim(" connected."))
    print()

    ring1_results: list[dict] = []
    turn_texts: list[str] = []       # rolling window of raw narrator turn text
    last_turn_text = ""
    initial_entities = 0
    final_entities = 0
    streak_no_move = 0               # consecutive turns without a movement action
    quest_objective = ""             # extracted active quest objective
    _QUEST_EXTRACT_AT = {12, 20, 30, 40, 55, 65, 75, 85}

    # Probe initial entity count
    ws = _get(diag, "/world-state", timeout=5.0)
    initial_entities = ws.get("entity_count", 0)

    overall_start = time.time()

    for turn_num in range(1, n_turns + 1):
        milestone = milestones.get(turn_num)

        # ── Determine this turn's input ──
        if milestone and milestone["type"] == "reset":
            label = milestone["label"]
            print(f"\n{bold(f'T{turn_num:02d}')} {dim('|')} {label}")
            print(f"  {dim('→')} Triggering story reset…")
            _get(base, "/reset?level=story", timeout=30.0)
            # Reset doesn't emit a narrator turn SSE — wait a fixed settle time
            # then continue. T01 is not scored.
            time.sleep(8.0)
            print(f"  {dim('·')} Reset complete (setup turn — not scored)")
            ring1_results.append({
                "turn": turn_num,
                "label": label,
                "failures": [],
                "sense_events": 0,
                "timed_out": False,
                "elapsed": 8.0,
            })
            continue
        else:
            # Determine input text
            if milestone and milestone["type"] == "input":
                input_text = milestone["text"]
                label = milestone["label"]
                if _RE_MOVE.search(input_text):
                    streak_no_move = 0
                print(f"\n{bold(f'T{turn_num:02d}')} {dim('|')} {warn('[MILESTONE]')} {label}")
            else:
                # Periodically refresh the quest objective from recent narrative
                if turn_num in _QUEST_EXTRACT_AT and len(turn_texts) >= 2:
                    quest_objective = _extract_quest_objective(turn_texts, ollama_url=ollama, model=model)
                    if quest_objective:
                        print(f"  {dim('⟳ Quest:')} {dim(quest_objective[:80])}")
                input_text = _gen_player_input(
                    last_turn_text, ollama_url=ollama, model=model,
                    streak_no_move=streak_no_move, quest_objective=quest_objective,
                )
                label = "organic"
                print(f"\n{bold(f'T{turn_num:02d}')} {dim('|')} {dim(label)}")

            print(f"  {dim('→')} Input: {json.dumps(input_text[:80])}")

            # Track movement streak — reset when input contains movement words
            if _RE_MOVE.search(input_text):
                streak_no_move = 0
            else:
                streak_no_move += 1

            with _events_lock:
                start_idx = len(_events)

            send_result = _post(base, "/player-input", {"text": input_text}, timeout=20.0)
            if not send_result.get("ok"):
                print(f"  {err('✗')} Send failed: {send_result.get('error', '?')}")
                ring1_results.append({
                    "turn": turn_num,
                    "label": label,
                    "failures": ["send failed"],
                    "sense_events": 0,
                    "timed_out": False,
                    "elapsed": 0,
                })
                continue

            intent = send_result.get("intent", "?")
            print(f"  {dim('→')} Intent: {intent} | Waiting for narrator…", end="", flush=True)
            stats = _wait_for_turn(start_idx, timeout=300.0)
            print(f" done ({stats['elapsed']:.1f}s)")

        # ── Capture turn text ──
        last_turn_text = stats.get("raw_turn_text", "")
        if last_turn_text:
            turn_texts.append(last_turn_text)

        # ── Ring 1 checks ──
        r1_failures = _ring1_check(
            turn_num=turn_num,
            raw_text=stats.get("raw_turn_text", ""),
            parsed_blocks=stats.get("parsed_blocks", []),
            narrator_name="The Fortress",
            sense_channels=stats.get("sense_channels", []),
            sense_grace_timeout=stats.get("timed_out", False),
        )

        sense_ct = stats.get("sense_events", 0)
        channels_str = ", ".join(stats.get("sense_channels", [])) or "none"
        ring1_results.append({
            "turn": turn_num,
            "label": label,
            "failures": r1_failures,
            "sense_events": sense_ct,
            "timed_out": stats.get("timed_out", False),
            "elapsed": stats.get("elapsed", 0),
        })

        if r1_failures:
            for f in r1_failures:
                print(f"  {err('✗')} Ring1: {f}")
        else:
            print(f"  {ok('✓')} Ring1 OK | senses: {channels_str} ({sense_ct})")

        if stats.get("timed_out"):
            print(f"  {warn('⚠')} Timed out waiting for turn")

        # ── Ring 2: fact extraction at milestone turns ──
        if turn_num in _FACT_EXTRACT_AT and last_turn_text:
            print(f"  {dim('→')} R2: Extracting facts…", end="", flush=True)
            new_facts = tracker.extract_facts(turn_num, last_turn_text)
            if new_facts:
                print(f" locked {len(new_facts)} fact(s)")
                for f in new_facts:
                    print(f"       {dim(f[:90])}")
            else:
                print(f" {warn('none extracted')}")

        # ── Ring 2: assertion check every 10 turns ──
        if turn_num in _ASSERT_AT and tracker.facts:
            print(f"  {dim('→')} R2: Asserting continuity against {len(tracker.facts)} facts…", end="", flush=True)
            contradictions = tracker.assert_facts(turn_num, turn_texts[-3:])
            if contradictions:
                print(f" {err(str(len(contradictions)) + ' contradiction(s)!')}")
                for c in contradictions:
                    print(f"       {err(c['quote'][:80])}")
            else:
                print(f" {ok('clear')}")

        # ── Ring 3: score every 10 turns ──
        if turn_num in _JUDGE_AT and len(turn_texts) >= 3:
            seg_start = max(1, turn_num - 9)
            seg_turns = turn_texts[-(min(10, len(turn_texts))):]
            seg_text = "\n\n---\n\n".join(seg_turns)
            print(f"  {dim('→')} R3: Scoring turns {seg_start}-{turn_num}…", end="", flush=True)
            scores = judge.score_segment(seg_start, turn_num, seg_text)
            avg = scores["avg"]
            dims = "  ".join(f"{d}={scores['scores'].get(d, '?')}" for d in "ABCDEF")
            if avg >= 7.5:
                marker = ok(f"avg {avg:.1f}")
            elif avg >= 6.0:
                marker = warn(f"avg {avg:.1f}")
            else:
                marker = err(f"avg {avg:.1f}")
            print(f" {dims}  {marker}")
            if scores.get("flags"):
                for flag in scores["flags"][:2]:
                    print(f"       {warn(str(flag)[:100])}")

        # Brief pause between turns to let the game settle
        if turn_num < n_turns:
            time.sleep(1.5)

    # ── Final entity count ──
    ws = _get(diag, "/world-state", timeout=5.0)
    final_entities = ws.get("entity_count", 0)

    # ── Report ──
    duration = time.time() - overall_start
    turns_done = len([r for r in ring1_results if "send failed" not in r["failures"]])
    avg_turn_time = duration / turns_done if turns_done else 0

    result = _print_report(
        n_turns=n_turns,
        model=model,
        duration=duration,
        avg_turn_time=avg_turn_time,
        ring1_results=ring1_results,
        tracker=tracker,
        judge=judge,
        entity_growth=(initial_entities, final_entities),
    )

    # ── Ring 4: story arc summary ──
    print(dim("  Running Ring 4 story arc analysis…"), flush=True)
    arc = _ring4_story_arc(turn_texts, player_name, ollama_url=ollama, model=model)
    _print_ring4(arc)

    # ── Optional JSON output ──
    if args.out:
        out_data = {
            "meta": {
                "turns": n_turns,
                "model": model,
                "player": player_name,
                "date": datetime.now().isoformat(),
                "duration_s": round(duration, 1),
            },
            "verdict": result["verdict"],
            "ring1": {
                "tag_leaks":   sum(1 for r in ring1_results if any("raw tag" in f for f in r["failures"])),
                "garbage":     sum(1 for r in ring1_results if any("garbage" in f for f in r["failures"])),
                "first_pers":  sum(1 for r in ring1_results if any("first-person" in f for f in r["failures"])),
                "lang_bleed":  sum(1 for r in ring1_results if any("non-ASCII" in f for f in r["failures"])),
                "turns":       ring1_results,
            },
            "ring2": {
                "facts":         tracker.facts,
                "contradictions": tracker.contradictions,
            },
            "ring3": {
                "segments": judge.segments,
            },
            "ring4": arc,
            "turn_texts": turn_texts,
        }
        try:
            os.makedirs(os.path.dirname(args.out), exist_ok=True) if os.path.dirname(args.out) else None
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump(out_data, f, indent=2, ensure_ascii=False)
            print(f"  Results written to: {args.out}")
        except Exception as e:
            print(f"  {warn('Could not write results:')} {e}")

    sys.exit(0 if result["verdict"] in ("PASS", "WARN") else 1)


if __name__ == "__main__":
    main()
