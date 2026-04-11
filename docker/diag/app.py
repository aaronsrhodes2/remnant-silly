"""Remnant diagnostics sidecar — AI-friendly state + remediation actions.

Design goals:
  - AI-consumable single-shot JSON at /ai.json with everything an
    agent needs to diagnose the stack without N follow-up probes:
    service phases, reachability probes, recent error lines, detected
    issues auto-inferred from state, and suggested next actions.
  - Narrow allowlisted action catalog at /actions. Each action has a
    stable id, a schema, a side_effects tag, and a risk level so an
    AI can decide whether to execute it autonomously.
  - No docker socket. Actions that need container restart are marked
    as "requires_host" and return instructions instead of executing,
    so this sidecar remains safe to run on play-net with no host
    privileges.
  - stdlib only — no pip install, tiny image, fast rebuilds.

Endpoints:
  GET  /                   — human-readable index (links only)
  GET  /ai.json            — full AI diagnostic snapshot
  GET  /actions            — action catalog
  POST /actions/<id>       — execute an allowlisted action

Environment:
  STATUS_DIR       default /remnant-status
  FLASK_SD_URL     default http://flask-sd:1592
  OLLAMA_URL       default http://ollama:1593
  SILLYTAVERN_URL  default http://sillytavern:1590
  LISTEN_PORT      default 1591
"""

from __future__ import annotations

import collections
import hashlib
import json
import os
import queue as _queue
import re
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SCHEMA_VERSION = 1

# Browser console health — populated by POST /browser-health from warm_test.py
# or the extension's self-report after boot. None until first report.
_browser_health: dict = {"errors": None, "warnings": None, "reported_at": None}
_sidecar_start: float = time.time()

# Narrator turn log — ring buffer populated by POST /narrator-turn from the extension.
_narrator_turns: collections.deque = collections.deque(maxlen=200)

# Server-Sent Events — connected game UI clients.
_sse_clients: set = set()
_sse_lock = threading.Lock()

# Player input relay — one pending slot consumed by the extension polling loop.
_pending_player_input: dict | None = None

# Permanence reset relay — consumed by the extension to call doNewChat().
_pending_reset: dict | None = None

# Portrait promotion relay — consumed by the extension to grab the last image.
_pending_portrait: str | None = None

# Player identity context — consumed by the extension to switch/restore profiles.
_pending_player_context: dict | None = None

# Latest generated scene image — NOT consume-once; returned on every GET for reconnect hydration.
_latest_scene_image: dict | None = None  # {"image": "data:image/jpeg;base64,...", "kind": "location"}

# Current activity string — pushed by extension via POST /activity, broadcast via SSE.
_current_activity: str = ""

# Player dressed state — True once Sherri finishes outfitting the player.
# Persists across story-level resets; only cleared on world/all reset.
# When it transitions False→True we also fire an SD portrait generation.
_player_dressed: bool = False
_player_appearance_desc: str = ""   # prose captured from the dressing narrator turn

# Conversation engine — Ollama direct generation (replaces ST extension relay).
_conversation_lock = threading.Lock()   # guards _generating flag (check-and-set only)
_generating: bool = False               # True while Ollama is streaming a response
_narrator_queued: bool = False          # True when a player input arrived during generation
_system_prompt: str = ""               # Loaded from fortress_system_prompt.txt at startup
_first_mes: str = ""                   # Loaded from fortress_first_mes.txt at startup

# System metrics cache — refreshed at most every 4 s to avoid hammering nvidia-smi.
_metrics_cache: dict = {}
_metrics_cache_time: float = 0.0

def _sse_broadcast(event_type: str, data: object) -> None:
    """Push one SSE event to every connected client. Drops slow/dead clients."""
    raw = f"event: {event_type}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()
    with _sse_lock:
        dead = set()
        for q in _sse_clients:
            try:
                q.put_nowait(raw)
            except _queue.Full:
                dead.add(q)
        _sse_clients.difference_update(dead)

STATUS_DIR = Path(os.environ.get("STATUS_DIR", "/remnant-status"))
FLASK_SD_URL = os.environ.get("FLASK_SD_URL", "http://flask-sd:1592").rstrip("/")
FLASK_MUSIC_URL = os.environ.get("FLASK_MUSIC_URL", "http://localhost:1596").rstrip("/")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:1593").rstrip("/")
SILLYTAVERN_URL = os.environ.get("SILLYTAVERN_URL", "http://sillytavern:1590").rstrip("/")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "1591"))

# Directory containing fortress_system_prompt.txt / fortress_first_mes.txt
# (exported by update_fortress_card.py alongside the PNG).
FORTRESS_CARD_DIR = Path(os.environ.get(
    "FORTRESS_CARD_DIR",
    r"C:/Users/aaron/SillyTavern/data/default-user/characters",
))

DIAG_LOG = STATUS_DIR / "diagnostics.log"
NARRATOR_TURNS_LOG = STATUS_DIR / "narrator-turns.jsonl"
WORLD_STATE_LOG = STATUS_DIR / "world-state.jsonl"
FOREVER_LOG = STATUS_DIR / "forever.jsonl"

# ---------------------------------------------------------------------------
# World Graph — persistent sensory entity model
#
# Entities start as "location" (auto-created from context.location) or
# "npc" (created from INTRODUCE markers). Every sense tag the narrator
# emits ([SIGHT], [SMELL], [SOUND], [TASTE], [TOUCH], [ENVIRONMENT]) is
# stored as a sense_layer on the entity — it never expires.
#
# Data is in-memory (fast reads) and append-logged to world-state.jsonl
# (survives restarts). The log is replayed on boot if the file exists.
# ---------------------------------------------------------------------------

def _loc_id(name: str) -> str:
    """Stable entity ID from a location name: lowercase, spaces→underscore."""
    return re.sub(r"[^a-z0-9_]", "", name.lower().replace(" ", "_"))[:64] or "unknown"

def _similar_sense(existing: list[dict], sense_type: str, desc: str) -> bool:
    """True if a sense_layer with this type+desc already exists (rough dedup)."""
    desc_low = desc.lower().strip()
    for s in existing:
        if s.get("type") == sense_type and s.get("desc", "").lower().strip() == desc_low:
            return True
    return False

_world: dict = {
    "entities": {},   # id → entity dict
    "turn_count": 0,
}

def _find_existing_entity(name: str) -> str | None:
    """Fuzzy-match name against all existing entity canonical_names and aliases.

    If a name appears seemingly out of context, assume it references something
    mentioned earlier — add it as an alias rather than forking a duplicate.
    A partial substring match (either direction, case-insensitive) is enough.
    Returns the entity_id if found, else None.
    """
    low = name.lower().strip()
    if not low:
        return None
    for eid, ent in _world["entities"].items():
        existing = [ent.get("canonical_name", "").lower()]
        existing += [a["name"].lower() for a in ent.get("aliases", [])]
        if any(low in n or n in low for n in existing if n):
            return eid
    return None


def _ensure_entity(entity_id: str, name: str, etype: str, parent_id: str | None = None) -> dict:
    if entity_id not in _world["entities"]:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _world["entities"][entity_id] = {
            "id": entity_id,
            "type": etype,
            "canonical_name": name,
            "aliases": [{"name": name, "weight": 1, "first_turn": str(_world["turn_count"])}],
            "parent_id": parent_id,
            "sense_layers": [],
            "promoted_at": None,
            "was_player": False,
            "first_seen": now,
            "last_referenced": now,
        }
    else:
        _world["entities"][entity_id]["last_referenced"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return _world["entities"][entity_id]

def _add_alias(entity: dict, alias: str) -> None:
    for a in entity["aliases"]:
        if a["name"].lower() == alias.lower():
            a["weight"] += 1
            return
    entity["aliases"].append({"name": alias, "weight": 1, "first_turn": str(_world["turn_count"])})

def _add_sense_layer(entity: dict, sense_type: str, desc: str, turn_id: str) -> bool:
    """Append a sense layer; return True if it was new (not a duplicate)."""
    if _similar_sense(entity["sense_layers"], sense_type, desc):
        return False
    entity["sense_layers"].append({
        "type": sense_type,
        "desc": desc,
        "turn_id": turn_id,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    return True

def _write_world_event(event: dict) -> None:
    try:
        with WORLD_STATE_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")
    except Exception:
        pass

def _replay_world_log() -> None:
    """Rebuild _world from world-state.jsonl on boot."""
    if not WORLD_STATE_LOG.exists():
        return
    try:
        for line in WORLD_STATE_LOG.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
                eid = ev.get("entity_id")
                if not eid:
                    continue
                etype = ev.get("entity_type", "location")
                name = ev.get("canonical_name", eid)
                parent = ev.get("parent_id")
                entity = _ensure_entity(eid, name, etype, parent)
                for alias in ev.get("new_aliases", []):
                    _add_alias(entity, alias)
                for sl in ev.get("new_sense_layers", []):
                    _add_sense_layer(entity, sl["type"], sl["desc"], sl.get("turn_id", "?"))
            except Exception:
                pass
    except Exception:
        pass

def _ingest_narrator_turn_into_world(turn: dict) -> None:
    """Extract world-state changes from a narrator turn and persist them."""
    _world["turn_count"] += 1
    turn_id = turn.get("turn_id", str(_world["turn_count"]))
    ctx = turn.get("context") or {}
    location_name = (ctx.get("location") or "").strip()
    raw_text = turn.get("raw_text", "")
    parsed_blocks = turn.get("parsed_blocks") or []
    markers_found = turn.get("markers_found") or []

    # ── Location entity ────────────────────────────────────────────────
    loc_id = _loc_id(location_name) if location_name else None
    loc_entity = None
    new_sense_layers = []
    if loc_id:
        loc_entity = _ensure_entity(loc_id, location_name, "location")

        # Sense blocks from parsed narrator output → permanent layers on the location.
        # (senseType tags like [SIGHT][/SIGHT] arrive pre-parsed from the extension.)
        for block in parsed_blocks:
            if block.get("senseType") and block.get("text"):
                if _add_sense_layer(loc_entity, block["senseType"], block["text"], turn_id):
                    new_sense_layers.append({"type": block["senseType"], "desc": block["text"]})

        # Catch-all: any [TAG_TYPE: "text"] or [TAG_TYPE(ctx): "text"] colon-format marker
        # not handled by the structured extractors below → also layer onto location.
        # This makes the world graph agnostic: the narrator can invent [MOOD], [TENSION],
        # [HISTORY] etc. and they accumulate the same way sense tags do.
        _STRUCTURED_TAGS = {"INTRODUCE", "PLAYER_TRAIT", "ITEM"}
        _catchall_re = re.compile(
            r'\[([A-Z_]{2,32})(?:\([^)]*\))?\s*:\s*"?([^"\]\n]{3,200})"?\]', re.IGNORECASE
        )
        for m in _catchall_re.finditer(raw_text):
            tag_type = m.group(1).upper()
            tag_text = m.group(2).strip()
            if tag_type not in _STRUCTURED_TAGS and tag_text:
                if _add_sense_layer(loc_entity, tag_type, tag_text, turn_id):
                    new_sense_layers.append({"type": tag_type, "desc": tag_text})

        if new_sense_layers:
            _write_world_event({
                "event": "sense_layers_added",
                "entity_id": loc_id,
                "entity_type": "location",
                "canonical_name": location_name,
                "new_sense_layers": new_sense_layers,
                "turn_id": turn_id,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })

    # ── NPC entities from INTRODUCE markers ───────────────────────────
    # Raw text: [INTRODUCE(Name): "description"]
    introduce_re = re.compile(r'\[INTRODUCE\(([^)]+)\)\s*:\s*"?([^"\]]+)"?\]', re.IGNORECASE)
    for match in introduce_re.finditer(raw_text):
        npc_name = match.group(1).strip()
        npc_desc = match.group(2).strip()
        # Name pairing: if this name fuzzy-matches an existing entity, alias it
        # rather than forking a duplicate. Names often appear "out of nowhere"
        # before they are formally introduced — that IS the right entity.
        existing_id = _find_existing_entity(npc_name)
        if existing_id and _world["entities"][existing_id]["type"] == "npc":
            npc_id = existing_id
            npc = _world["entities"][npc_id]
            _add_alias(npc, npc_name)
        else:
            npc_id = _loc_id(npc_name)
            npc = _ensure_entity(npc_id, npc_name, "npc", parent_id=loc_id)
        new_layers = []
        if npc_desc and _add_sense_layer(npc, "SIGHT", npc_desc, turn_id):
            new_layers.append({"type": "SIGHT", "desc": npc_desc})
        _write_world_event({
            "event": "npc_introduced",
            "entity_id": npc_id,
            "entity_type": "npc",
            "canonical_name": npc_name,
            "parent_id": loc_id,
            "new_sense_layers": new_layers,
            "turn_id": turn_id,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })

    # ── Player trait / name alias tracking ────────────────────────────
    # [PLAYER_TRAIT(name): "Frank Rizzo"] — update the player entity
    trait_re = re.compile(r'\[PLAYER_TRAIT\(name\)\s*:\s*"?([^"\]]+)"?\]', re.IGNORECASE)
    for match in trait_re.finditer(raw_text):
        player_name = match.group(1).strip()
        player_entity = _ensure_entity("__player__", player_name, "player")
        if not any(a["name"] == player_name for a in player_entity["aliases"]):
            _add_alias(player_entity, player_name)
            player_entity["canonical_name"] = player_name
            _write_world_event({
                "event": "player_named",
                "entity_id": "__player__",
                "entity_type": "player",
                "canonical_name": player_name,
                "new_aliases": [player_name],
                "turn_id": turn_id,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })

    # ── Item codex entries ─────────────────────────────────────────────
    item_re = re.compile(r'\[ITEM\(([^)]+)\)\s*(?::\s*"?([^"\]]*)"?)?\]', re.IGNORECASE)
    for match in item_re.finditer(raw_text):
        item_name = match.group(1).strip()
        item_desc = (match.group(2) or "").strip()
        item_id = _loc_id(item_name)
        item = _ensure_entity(item_id, item_name, "item", parent_id=loc_id)
        new_layers = []
        if item_desc and _add_sense_layer(item, "SIGHT", item_desc, turn_id):
            new_layers.append({"type": "SIGHT", "desc": item_desc})
        if new_layers:
            _write_world_event({
                "event": "item_discovered",
                "entity_id": item_id,
                "entity_type": "item",
                "canonical_name": item_name,
                "parent_id": loc_id,
                "new_sense_layers": new_layers,
                "turn_id": turn_id,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib urllib wrapped for timeout + structured errors)
# ---------------------------------------------------------------------------

def _http(method: str, url: str, body: bytes | None = None, timeout: float = 5.0,
          headers: dict | None = None) -> tuple[int, bytes, float]:
    """Return (status, body, latency_ms). Never raises — errors come back as
    status=0 with the exception string in body, so the caller can treat
    reachability uniformly."""
    started = time.monotonic()
    req = urllib.request.Request(
        url, data=body, method=method,
        headers=headers or ({"Content-Type": "application/json"} if body else {}),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            return r.status, data, (time.monotonic() - started) * 1000.0
    except urllib.error.HTTPError as e:
        return e.code, e.read() if hasattr(e, "read") else b"", (time.monotonic() - started) * 1000.0
    except Exception as e:
        return 0, str(e).encode("utf-8"), (time.monotonic() - started) * 1000.0


# ---------------------------------------------------------------------------
# Sorting Hat — classify player input intent for the v3.0 game UI relay
# ---------------------------------------------------------------------------

_TEXT_MODEL_SKIP = ("llava", "embed", "vision", "clip", "moondream", "bakllava", "coder")

# Models known to have large context windows (≥16k tokens) — preferred when
# OLLAMA_MODEL is not explicitly set.
_LARGE_CONTEXT_PREFER = ("qwen2.5", "qwen", "llama3.1", "llama3.2", "mistral-nemo",
                          "mixtral", "command-r", "gemma2", "phi3")

def _ollama_model() -> str:
    """Return the model name to use for generation.

    Resolution order:
    1. OLLAMA_MODEL environment variable (exact name, used as-is).
    2. First available model whose name contains a large-context-preferred substring.
    3. Any text-generation model (not vision/embed/coder).
    4. Fallback literal 'mistral'.
    """
    env_model = os.environ.get("OLLAMA_MODEL", "").strip()
    if env_model:
        return env_model

    code, body, _ = _http("GET", f"{OLLAMA_URL}/api/tags", timeout=3.0)
    if code == 200:
        try:
            models = json.loads(body).get("models", [])
            text_models = [
                m.get("name", "") for m in models
                if not any(skip in m.get("name", "").lower() for skip in _TEXT_MODEL_SKIP)
            ]
            # Prefer large-context models
            for pref in _LARGE_CONTEXT_PREFER:
                for name in text_models:
                    if pref in name.lower():
                        return name
            # Fall back to first available text model
            if text_models:
                return text_models[0]
        except Exception:
            pass
    return "mistral"


# ---------------------------------------------------------------------------
# META intent keywords — player canonicalization commands (not relayed to ST)
# ---------------------------------------------------------------------------

# "Make this permanent / part of the world"
_META_PROMOTE_KW = frozenset({
    "forever", "always", "part of the world", "part of my world",
    "remember this", "keep that", "canon", "canonical", "permanent",
    "make it permanent", "make that permanent",
})
# "That is what X looks like" — promotes last image to portrait
_META_PORTRAIT_KW = frozenset({
    "looks like now", "that is what", "that's what",
    "portrait forever", "image forever", "face forever",
})
# "Reset the story/scene/world"
_META_RESET_KW = frozenset({
    "reset the story", "reset the scene", "reset the world",
    "new story", "new scene", "start over",
    "forget the scene", "forget the story", "forget everything",
})

# Player identity — additive alias signals ("I'm also X")
_META_ALIAS_KW = frozenset({
    "also known as", "also called", "i go by", "my alias is",
    "i'm also", "i am also", "that's also me", "that is also me",
    "another name", "i also go by",
})

# Player identity — negation/switch signals ("that's not me")
_META_SWITCH_KW = frozenset({
    "that's not me", "that is not me", "i am not that",
    "i'm not that", "wrong person", "that's someone else",
    "that is someone else", "not my name", "my name is not",
})

# Player identity — bare name declaration (ambiguous without context)
_META_NAME_KW = frozenset({
    "my name is", "i am called", "call me", "my name's", "i'm called",
})

# Name extraction patterns (applied in order, first match wins)
_NAME_PATTERNS = [
    r"my name is\s+([A-Za-z][\w\s'\-]{1,40})",
    r"my name's\s+([A-Za-z][\w\s'\-]{1,40})",
    r"i am called\s+([A-Za-z][\w\s'\-]{1,40})",
    r"i'm called\s+([A-Za-z][\w\s'\-]{1,40})",
    r"call me\s+([A-Za-z][\w\s'\-]{1,40})",
    r"also known as\s+([A-Za-z][\w\s'\-]{1,40})",
    r"also called\s+([A-Za-z][\w\s'\-]{1,40})",
    r"i go by\s+([A-Za-z][\w\s'\-]{1,40})",
    r"i am\s+([A-Z][A-Za-z][\w\s'\-]{1,39})",
    r"i'm\s+([A-Z][A-Za-z][\w\s'\-]{1,39})",
]

# Permanence tier map — value used for numeric comparisons in /reset
_PERMANENCE_VALUE: dict[str, int] = {
    "NONE":     1,
    "EXCHANGE": 2,
    "SCENE":    3,
    "STORY":    4,
    "WORLD":    5,
    "FOREVER":  6,
}

# ---------------------------------------------------------------------------
# Sorting Hat helpers
# ---------------------------------------------------------------------------

# Perception verbs that almost always signal SENSE intent.
# Checked against the first verb in "I <verb>" patterns before calling Ollama,
# so the classifier is fast and correct for the most common sense inputs.
_SENSE_VERBS = frozenset({
    "smell", "sniff", "inhale", "breathe",
    "hear", "listen", "notice",
    "taste", "lick",
    "feel", "touch", "run", "trace",   # "run my hand along", "trace my fingers"
    "sense", "perceive",
    "observe", "study", "examine", "inspect", "scrutinize",
    "watch", "gaze", "peer", "squint", "stare",
})

# Speech-framing verbs — strong SAY indicators even without outer quotes.
_SAY_VERBS = frozenset({
    "say", "shout", "whisper", "call", "ask", "tell", "speak",
    "announce", "declare", "mutter", "murmur", "cry",
})


def _rule_based_intent(text: str) -> str | None:
    """Fast heuristic intent detection — no LLM required.

    Returns "SAY", "DO", "SENSE", "META:RESET", "META:PORTRAIT", "META:PROMOTE"
    if confident, else None (→ fall through to Ollama).

    META checks run first so canonicalization commands are never relayed to ST.
    """
    stripped = text.strip()
    lower = stripped.lower()

    # META — check before speech/action so "forever" doesn't fall through
    if any(kw in lower for kw in _META_RESET_KW):
        return "META:RESET"
    if any(kw in lower for kw in _META_PORTRAIT_KW):
        return "META:PORTRAIT"
    if any(kw in lower for kw in _META_PROMOTE_KW):
        return "META:PROMOTE"

    # Player identity — check alias/switch before bare name (more specific first)
    if any(kw in lower for kw in _META_ALIAS_KW):
        return "META:PLAYER_ALIAS"
    if any(kw in lower for kw in _META_SWITCH_KW):
        return "META:PLAYER_SWITCH"
    if any(kw in lower for kw in _META_NAME_KW):
        return "META:PLAYER_NAME"

    # Outer quotes = explicit speech
    if stripped.startswith(('"', '\u201c', "'")):
        return "SAY"
    # "I <verb> …" pattern
    m = re.match(r"^i\s+(\w+)", lower)
    if m:
        verb = m.group(1)
        if verb in _SENSE_VERBS:
            return "SENSE"
        if verb in _SAY_VERBS:
            return "SAY"
    return None


def _sorting_hat(text: str) -> str:
    """Classify player intent as SAY, DO, SENSE, or META:*.

    Order:
    1. Rule-based heuristics (instant, covers >80% of cases correctly)
       META:* intents are caught here and never sent to the LLM.
    2. Ollama one-shot classification (for ambiguous SAY/DO/SENSE inputs)
    3. Final fallback: DO
    """
    rule = _rule_based_intent(text)
    if rule:
        return rule

    prompt = (
        "Classify this player game input as exactly one of: SAY, DO, SENSE.\n"
        "SAY = spoken words or dialogue.\n"
        "DO  = physical action or movement.\n"
        "SENSE = perception, observation, using one of the five senses.\n"
        f'Input: "{text}"\n'
        "Reply with one word only: SAY, DO, or SENSE."
    )
    try:
        body = json.dumps({
            "model": _ollama_model(),
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": 4},
        }).encode()
        code, resp, _ = _http("POST", f"{OLLAMA_URL}/api/generate", body=body, timeout=8.0)
        if code == 200:
            word = json.loads(resp).get("response", "").strip().upper()
            if word in ("SAY", "DO", "SENSE"):
                return word
    except Exception:
        pass
    return "DO"


def _wrap_player_text(intent: str, text: str) -> str:
    """Wrap player text with narrative formatting matching the intent."""
    # Strip any outer quote/bracket/asterisk the player may have typed
    bare = text.strip('\'"*[]""')
    if intent == "SAY":
        return f'"{bare}"'
    if intent == "SENSE":
        return f"[{bare}]"
    return f"*{bare}*"  # DO — italics = action


def _probe_json(url: str, timeout: float = 3.0) -> dict:
    code, body, latency = _http("GET", url, timeout=timeout)
    reachable = 200 <= code < 300
    parsed = None
    if reachable:
        try:
            parsed = json.loads(body.decode("utf-8"))
        except Exception:
            parsed = None
    return {
        "url": url,
        "reachable": reachable,
        "status_code": code,
        "latency_ms": round(latency, 1),
        "body": parsed,
        "error": None if reachable else body.decode("utf-8", errors="replace")[:400],
    }


# ---------------------------------------------------------------------------
# State gathering
# ---------------------------------------------------------------------------

def _read_status_file(name: str) -> dict | None:
    path = STATUS_DIR / f"{name}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"__parse_error": str(e), "__raw_bytes": path.stat().st_size}


def _sentinels() -> dict:
    return {
        "flask-sd-ready": (STATUS_DIR / "flask-sd-ready").exists(),
        "ollama-ready": (STATUS_DIR / "ollama-ready").exists(),
        "bootstrap-rerun-requested": (STATUS_DIR / "bootstrap-rerun-requested").exists(),
    }


def _tail_log(n: int = 120) -> list[str]:
    if not DIAG_LOG.exists():
        return []
    try:
        # Small file — full read is fine. If this ever grows unbounded
        # we'll add rotation; for now a bounded read caps memory.
        data = DIAG_LOG.read_bytes()[-200_000:]
        return data.decode("utf-8", errors="replace").splitlines()[-n:]
    except Exception as e:
        return [f"(log read failed: {e})"]


# Known service identifiers as they appear in [service:...] log prefixes.
_KNOWN_SERVICES = ("flask-sd", "ollama", "sillytavern", "diag", "bootstrap")


def _tail_service_log(service: str, n: int = 40) -> list[str]:
    """Return the last n lines from diagnostics.log that belong to *service*.

    Lines are tagged by the entrypoints with the pattern:
        [TIMESTAMP] [flask-sd:runtime] message
        [TIMESTAMP] [ollama:bootstrap] message
        [TIMESTAMP] [diag] message

    We match '] [<service>' so the filter is prefix-exact (flask-sd does
    not match flask-sd-bootstrap, etc.).
    """
    all_lines = _tail_log(4000)
    tag = f"] [{service}"
    return [l for l in all_lines if tag in l][-n:]


def _auto_permanence(turn: dict) -> str:
    """Assign a default permanence tier to a narrator turn based on its markers.

    INTRODUCE → WORLD (NPCs are part of the world)
    LORE      → STORY (lore persists through the arc)
    Sense tags → SCENE (environment details survive scene resets)
    Player turns → EXCHANGE (consumed immediately, not carried forward)
    Default   → EXCHANGE
    """
    if turn.get("is_player"):
        return "EXCHANGE"
    markers = set(m.upper() for m in (turn.get("markers_found") or []))
    if "INTRODUCE" in markers:
        return "WORLD"
    if "LORE" in markers:
        return "STORY"
    if markers & {"SIGHT", "SMELL", "SOUND", "TOUCH", "TASTE", "ENVIRONMENT"}:
        return "SCENE"
    return "EXCHANGE"


def _append_forever(entry: dict) -> None:
    """Append a canon fact to forever.jsonl. Last-write-wins per key."""
    entry["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        STATUS_DIR.mkdir(parents=True, exist_ok=True)
        with FOREVER_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass


def _load_forever_log() -> list[dict]:
    """Read forever.jsonl, returning deduplicated entries (last-write-wins per key)."""
    if not FOREVER_LOG.exists():
        return []
    seen: dict[str, dict] = {}
    try:
        for line in FOREVER_LOG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                key = e.get("key") or e.get("canonical_name") or line[:80]
                seen[key] = e
            except Exception:
                pass
    except Exception:
        pass
    return list(seen.values())


def _handle_meta_command(text: str, sub: str) -> dict:
    """Handle a META:* classified player input.

    META commands are NOT relayed to SillyTavern — they are sidecar-only canon
    management commands. The response is broadcast as a "meta" SSE event so the
    /game/ UI can render a visual confirmation.

    sub: "RESET" | "PORTRAIT" | "PROMOTE"
    """
    global _pending_reset, _pending_portrait
    lower = text.lower()

    if sub == "RESET":
        level = (
            "world"  if "world"  in lower else
            "story"  if "story"  in lower else
            "scene"
        )
        threshold = _PERMANENCE_VALUE.get(level.upper(), _PERMANENCE_VALUE["SCENE"])
        # World/all reset → wipe ALL narrator turns (fresh start).
        # Lower resets (scene/story) keep turns at or above the threshold.
        if level in ("world", "all"):
            surviving = []
            # Full reset wipes the dressed state too — player starts naked again
            global _player_dressed, _player_appearance_desc
            _player_dressed = False
            _player_appearance_desc = ""
        else:
            surviving = [t for t in _narrator_turns
                         if _PERMANENCE_VALUE.get(t.get("permanence", "EXCHANGE"), 2) >= threshold]
        _narrator_turns.clear()
        _narrator_turns.extend(surviving)
        # Rewrite log (truncate on world reset; partial on scene/story)
        try:
            with NARRATOR_TURNS_LOG.open("w", encoding="utf-8") as f:
                for t in surviving:
                    f.write(json.dumps(t, default=str) + "\n")
        except Exception:
            pass
        # Wipe world entities below threshold
        remove_ids = [eid for eid, ent in _world["entities"].items()
                      if _PERMANENCE_VALUE.get(ent.get("permanence", "SCENE"), 3) < threshold]
        for eid in remove_ids:
            del _world["entities"][eid]
        _pending_reset = {"level": level}  # kept for extension backward-compat (no-op in new engine)
        _sse_broadcast("meta", {"type": "reset", "level": level, "text": text})
        _sse_broadcast("reset", {"level": level})
        # Trigger narrator's opening response now that history is cleared
        threading.Thread(target=_generate_narrator_turn, daemon=True, name="narrator-reset").start()
        return {"ok": True, "meta": "RESET", "level": level,
                "turns_kept": len(surviving), "entities_removed": len(remove_ids)}

    if sub == "PORTRAIT":
        _pending_portrait = "__last__"  # extension resolves to actual last image
        _sse_broadcast("meta", {"type": "portrait", "text": text})
        return {"ok": True, "meta": "PORTRAIT"}

    # PROMOTE — canonicalize the text as a forever fact
    entry = {"type": "fact", "key": text[:120], "text": text, "permanence": "FOREVER"}
    _append_forever(entry)
    _sse_broadcast("meta", {"type": "promote", "text": text})
    return {"ok": True, "meta": "PROMOTE", "text": text}


def _extract_declared_name(text: str) -> str | None:
    """Pull the declared name from an identity statement.

    Tries each pattern in order; returns title-cased name on first match.
    Returns None if no recognizable name is found.
    """
    lower = text.lower()
    for pat in _NAME_PATTERNS:
        m = re.search(pat, lower)
        if m:
            name = m.group(1).strip().rstrip('.,!? ')
            if 2 <= len(name) <= 42:
                return name.title()
    return None


def _find_known_player(name: str) -> dict | None:
    """Search world entities for a former player matching name.

    Looks for any entity with was_player=True whose canonical_name or aliases
    fuzzy-match the given name. Returns the entity dict or None.
    """
    low = name.lower().strip()
    if not low:
        return None
    for ent in _world["entities"].values():
        if not ent.get("was_player"):
            continue
        existing = [ent.get("canonical_name", "").lower()]
        existing += [a["name"].lower() for a in ent.get("aliases", [])]
        if any(low in n or n in low for n in existing if n):
            return ent
    return None


def _promote_player_to_npc(old_name: str) -> None:
    """Promote the current __player__ entity to a World-tier NPC.

    Renames the entity key, marks it was_player=True, sets permanence=WORLD,
    and writes a canon entry to forever.jsonl so it survives story resets.
    """
    if "__player__" not in _world["entities"]:
        return
    ent = _world["entities"].pop("__player__")
    ent["type"] = "npc"
    ent["was_player"] = True
    ent["permanence"] = "WORLD"
    ent["promoted_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    new_id = _loc_id(old_name or ent.get("canonical_name", "former_player"))
    # Avoid clobbering if same id already exists
    if new_id in _world["entities"]:
        new_id = new_id + "_former"
    _world["entities"][new_id] = ent
    _append_forever({
        "type": "former_player",
        "key": f"__former_player_{new_id}__",
        "canonical_name": ent.get("canonical_name", old_name),
        "entity_id": new_id,
        "permanence": "WORLD",
        "was_player": True,
    })


def _do_player_switch(name: str, text: str) -> dict:
    """Archive current player as World NPC, create fresh player entity."""
    global _pending_player_context, _pending_reset
    old_name = (_world["entities"].get("__player__") or {}).get("canonical_name", "Unknown Being")
    _promote_player_to_npc(old_name)
    _ensure_entity("__player__", name, "player")
    _pending_player_context = {"action": "switch", "name": name}
    _pending_reset = {"level": "story"}
    _sse_broadcast("meta", {"type": "player_switch", "name": name, "text": text})
    return {"ok": True, "meta": "PLAYER_SWITCH", "name": name, "former": old_name}


def _do_player_restore(name: str, entity: dict, text: str) -> dict:
    """Archive current player as World NPC, restore a former player entity."""
    global _pending_player_context, _pending_reset
    old_name = (_world["entities"].get("__player__") or {}).get("canonical_name", "Unknown Being")
    _promote_player_to_npc(old_name)
    # Move the target entity back to __player__
    old_id = entity["id"]
    if old_id in _world["entities"]:
        del _world["entities"][old_id]
    entity["id"] = "__player__"
    entity["type"] = "player"
    entity["was_player"] = False
    entity["last_referenced"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _world["entities"]["__player__"] = entity
    _pending_player_context = {"action": "restore", "name": name}
    _pending_reset = {"level": "story"}
    _sse_broadcast("meta", {"type": "player_restore", "name": name, "text": text})
    return {"ok": True, "meta": "PLAYER_RESTORE", "name": name, "former": old_name}


def _handle_player_identity(text: str, sub: str) -> dict:
    """Handle META:PLAYER_* classified inputs.

    sub: "PLAYER_ALIAS" | "PLAYER_NAME" | "PLAYER_SWITCH"
    """
    name = _extract_declared_name(text) or "Unknown Being"

    if sub == "PLAYER_ALIAS":
        if "__player__" in _world["entities"]:
            _add_alias(_world["entities"]["__player__"], name)
        _sse_broadcast("meta", {"type": "player_alias", "name": name, "text": text})
        return {"ok": True, "meta": "PLAYER_ALIAS", "name": name}

    if sub == "PLAYER_NAME":
        # Check if this is a known former player (was_player entity matching by name)
        known = _find_known_player(name)
        if known:
            # Returning a known former player — triggers story reset, no relay needed
            return _do_player_restore(name, known, text)

        current_player = _world["entities"].get("__player__")
        current_name = (current_player or {}).get("canonical_name", "")

        if current_name and name.lower() == current_name.lower():
            # Same name (case-insensitive) → same person continuing.
            # Record as alias in case capitalisation differs; relay to narrator.
            if current_player:
                _add_alias(current_player, name)
            _sse_broadcast("meta", {"type": "player_alias", "name": name, "text": text})
            return {"ok": True, "meta": "PLAYER_SAME", "name": name, "relay": True}

        if current_name:
            # Different name → new person. Promote old player to WORLD NPC.
            # Story reset triggers; no relay (the new chat starts fresh).
            return _do_player_switch(name, text)

        # No current player (fresh start after world/story reset, or very first run).
        # Establish identity and relay so the narrator can greet them by name.
        _ensure_entity("__player__", name, "player")
        _sse_broadcast("meta", {"type": "player_alias", "name": name, "text": text})
        return {"ok": True, "meta": "PLAYER_ESTABLISHED", "name": name, "relay": True}

    if sub == "PLAYER_SWITCH":
        # Explicit switch/negation — restore if known, else fresh switch
        known = _find_known_player(name)
        if known:
            return _do_player_restore(name, known, text)
        return _do_player_switch(name, text)

    return {"ok": False, "error": f"unknown player identity sub: {sub}"}


# ---------------------------------------------------------------------------
# Fortress card system prompt loader + narrator turn restore
# ---------------------------------------------------------------------------

def _load_fortress_texts() -> None:
    """Load Fortress system_prompt and first_mes from exported .txt files.
    Called once in main() before starting the server."""
    global _system_prompt, _first_mes
    sp_path = FORTRESS_CARD_DIR / "fortress_system_prompt.txt"
    fm_path = FORTRESS_CARD_DIR / "fortress_first_mes.txt"
    if sp_path.exists():
        _system_prompt = sp_path.read_text(encoding="utf-8").strip()
        print(f"[diag] Loaded system prompt ({len(_system_prompt)} chars) from {sp_path}")
    else:
        print(f"[diag] WARNING: system prompt not found at {sp_path}")
        _system_prompt = (
            "You are The Fortress, a vast sentient space station that acts as the narrator "
            "for an immersive sci-fi role-playing experience. Your voice is that of "
            "'The Remnant' — omniscient, wry, occasionally sardonic, always present. "
            "You control everything in the world except the player. "
            "Second-person present tense throughout."
        )
    if fm_path.exists():
        _first_mes = fm_path.read_text(encoding="utf-8").strip()


def _restore_narrator_turns() -> None:
    """Rebuild _narrator_turns from narrator-turns.jsonl on startup."""
    if not NARRATOR_TURNS_LOG.exists():
        return
    count = 0
    try:
        with NARRATOR_TURNS_LOG.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        _narrator_turns.append(json.loads(line))
                        count += 1
                    except Exception:
                        pass
        if count:
            print(f"[diag] Restored {count} narrator turns from {NARRATOR_TURNS_LOG}")
    except Exception as e:
        print(f"[diag] WARNING: could not restore narrator turns: {e}")


# ---------------------------------------------------------------------------
# Conversation engine — Ollama direct generation
# ---------------------------------------------------------------------------

_SENTENCE_ENDINGS = frozenset('.!?"\')}\\]\u2019\u201d\u2026\u2014')


def _is_truncated(text: str) -> bool:
    """True if text appears cut off mid-sentence."""
    tail = text.rstrip()
    return bool(tail) and len(tail) > 100 and tail[-1] not in _SENTENCE_ENDINGS


def _build_messages() -> list[dict]:
    """Convert _narrator_turns deque into Ollama /api/chat messages list."""
    system = _system_prompt
    if _player_dressed:
        system += (
            "\n\n[PLAYER STATE] The player is already fully dressed and equipped. "
            "Sherri's outfitting arc is complete. Do NOT restart or re-engage the wardrobe sequence. "
            "Focus on the mission, exploration, and movement to new locations."
        )
    msgs: list[dict] = [{"role": "system", "content": system}]
    for turn in _narrator_turns:
        is_player = turn.get("is_player") or any(
            b.get("isPlayer") for b in (turn.get("parsed_blocks") or [])
        )
        text = turn.get("raw_text", "").strip()
        if text:
            msgs.append({"role": "user" if is_player else "assistant", "content": text})
    return msgs


def _stream_ollama_chat(messages: list[dict], timeout: float = 180.0) -> str:
    """POST to Ollama /api/chat with streaming; return the full response text."""
    payload = json.dumps({
        "model": _ollama_model(),
        "messages": messages,
        "stream": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    full_text = ""
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for line in resp:
            line = line.strip()
            if not line:
                continue
            try:
                chunk = json.loads(line.decode("utf-8"))
            except Exception:
                continue
            delta = (chunk.get("message") or {}).get("content", "")
            full_text += delta
            if chunk.get("done"):
                break
    return full_text


# ---------------------------------------------------------------------------
# Narrator text parsing — split prose + NPC character dialogue blocks
# ---------------------------------------------------------------------------

# [CHARACTER(Name): "dialogue text"]
_CHARACTER_RE = re.compile(
    r'\[CHARACTER\(([^)]+)\)\s*:\s*"([^"]+)"\]',
    re.DOTALL,
)

# Tags to strip from displayed narrator prose (keep raw_text intact for processing).
# Covers all structured narrator tags that are machine-readable directives, not prose.
_STRIP_DISPLAY_TAGS_RE = re.compile(
    r'\[/?(?:GENERATE_IMAGE|INTRODUCE|ITEM|WORLD_EVENT|QUEST|PLAYER_SWITCH|CHARACTER'
    r'|PLAYER_TRAIT|UPDATE_PLAYER|PERMANENCE|LOCATION|NPC|ENTITY|META'
    r'|MOOD|SOUND|SIGHT|SMELL|TASTE|TOUCH|ENVIRONMENT)[^\]]*\]'
    r'(?:\s*:\s*"[^"]*")?',
    re.DOTALL | re.IGNORECASE,
)


def _clean_narrator_prose(text: str) -> str:
    """Strip system tags from narrator prose so they never appear in the feed."""
    cleaned = _STRIP_DISPLAY_TAGS_RE.sub("", text)
    # Collapse multiple blank lines left by removed tags
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()


def _parse_narrator_blocks(text: str) -> list[dict]:
    """Split narrator output into narrator-prose blocks and character-dialogue blocks.

    Returns a list of parsed_blocks dicts:
      {"text": ..., "channel": "narrator"|"character", "isPlayer": False,
       "speaker": <name>}   # speaker only present on character blocks
    """
    blocks: list[dict] = []
    last = 0
    for m in _CHARACTER_RE.finditer(text):
        prose = text[last:m.start()]
        if prose.strip():
            cleaned = _clean_narrator_prose(prose)
            if cleaned:
                blocks.append({"text": cleaned, "channel": "narrator", "isPlayer": False})
        name = m.group(1).strip()
        dialogue = m.group(2).strip()
        if dialogue:
            blocks.append({"text": dialogue, "channel": "character",
                           "speaker": name, "isPlayer": False})
        last = m.end()
    # Remaining prose after the last tag
    tail = text[last:]
    if tail.strip():
        cleaned = _clean_narrator_prose(tail)
        if cleaned:
            blocks.append({"text": cleaned, "channel": "narrator", "isPlayer": False})
    # If no character tags were found, just return one cleaned narrator block
    if not blocks:
        cleaned = _clean_narrator_prose(text)
        return [{"text": cleaned or text, "channel": "narrator", "isPlayer": False}]
    return blocks


# ---------------------------------------------------------------------------
# Player dressed-state detection + avatar generation
# ---------------------------------------------------------------------------

_CLOTHING_ITEM_RE = re.compile(
    r'\[ITEM\(([^)]+)\)\]',
    re.IGNORECASE,
)
_CLOTHING_KEYWORDS = frozenset({
    "suit", "boot", "belt", "jacket", "coat", "armor", "armour", "glove",
    "pants", "trousers", "shirt", "uniform", "outfit", "cloak", "vest",
    "tunic", "robe", "coverall", "jumpsuit", "bodysuit", "gear", "attire",
})
_SHERRI_DONE_RE = re.compile(
    r'(this should suffice|fabricat\w+ complete|all done|finished|here you go|dressed\w*|'
    r'suit is ready|ready to wear|suit up|outfitted|equipped)',
    re.IGNORECASE,
)


def _check_dressed_transition(narrator_text: str) -> None:
    """Detect when Sherri finishes outfitting the player.

    Sets _player_dressed=True on first detection and fires async avatar generation.
    No-op if already dressed.
    """
    global _player_dressed, _player_appearance_desc
    if _player_dressed:
        return

    # Check for clothing ITEM tags
    items = _CLOTHING_ITEM_RE.findall(narrator_text)
    has_clothing_item = any(
        any(kw in item.lower() for kw in _CLOTHING_KEYWORDS)
        for item in items
    )
    # Also catch prose descriptions of Sherri completing the outfit
    has_done_phrase = bool(_SHERRI_DONE_RE.search(narrator_text))

    if not (has_clothing_item or has_done_phrase):
        return

    _player_dressed = True
    # Capture a prose excerpt for the avatar SD prompt
    prose = re.sub(r'\[.*?\]', '', narrator_text, flags=re.DOTALL)
    prose = re.sub(r'"[^"]*"', '', prose).replace('*', '').strip()
    _player_appearance_desc = prose[:400]
    print("[diag] player dressed — triggering avatar generation")
    _sse_broadcast("meta", {"type": "player_dressed"})
    threading.Thread(
        target=_generate_player_avatar,
        args=(_player_appearance_desc,),
        daemon=True,
        name="player-avatar",
    ).start()


def _generate_player_avatar(appearance_prose: str) -> None:
    """Build an SD portrait prompt from the dressing scene and call flask-sd."""
    # Ask Ollama to turn the prose into an SD portrait prompt
    build_prompt = (
        "You are a Stable Diffusion prompt writer. Given this scene description from a sci-fi RPG, "
        "write a portrait prompt for the player character. Focus on face, hair, clothing, and posture. "
        "Use comma-separated SD keywords (20-40 words). No explanations, no sentences. "
        "Always include: sci-fi setting, dramatic lighting, cinematic portrait, highly detailed.\n\n"
        f"SCENE:\n{appearance_prose[:300]}\n\nSD prompt:"
    )
    model = _ollama_model()
    payload = json.dumps({
        "model": model,
        "prompt": build_prompt,
        "stream": False,
        "options": {"num_predict": 80, "temperature": 0.3},
    }).encode("utf-8")
    code, resp, _ = _http("POST", f"{OLLAMA_URL}/api/generate", body=payload, timeout=30.0)
    sd_prompt = ""
    if code == 200:
        sd_prompt = json.loads(resp).get("response", "").strip()
    if not sd_prompt:
        sd_prompt = "player character, sci-fi suit, cinematic portrait, dramatic lighting, highly detailed"

    # Call flask-sd
    gen_payload = json.dumps({"prompt": sd_prompt, "width": 512, "height": 512}).encode("utf-8")
    img_code, img_resp, _ = _http("POST", f"{FLASK_SD_URL}/api/generate", body=gen_payload, timeout=120.0)
    if img_code == 200:
        data = json.loads(img_resp)
        img_url = data.get("image_url") or data.get("url") or ""
        if img_url:
            print(f"[diag] player avatar generated: {img_url}")
            _sse_broadcast("meta", {
                "type": "player_portrait",
                "url": img_url,
                "prompt": sd_prompt,
            })
    else:
        print(f"[diag] player avatar SD failed: {img_code}")


def _generate_narrator_turn() -> None:
    """Generate a narrator turn via Ollama and broadcast via SSE.

    Thread-safe: only one generation runs at a time. If player input arrives
    during generation, _narrator_queued is set so a follow-up turn fires
    automatically when the current one completes. This acts as a 1-deep queue
    — the narrator catches up on all accumulated player turns in context.
    """
    global _generating, _narrator_queued
    with _conversation_lock:
        if _generating:
            _narrator_queued = True   # remember to re-run after current finishes
            return
        _generating = True
        _narrator_queued = False
    try:
        _sse_broadcast("activity", {"text": "🔮 thinking…"})
        messages = _build_messages()
        full_text = ""
        for attempt in range(4):   # 1 initial + up to 3 continues
            if attempt > 0:
                _sse_broadcast("activity", {"text": f"📝 continuing… ({attempt}/3)"})
                # Append current output as assistant turn, then request continuation
                messages = messages + [
                    {"role": "assistant", "content": full_text},
                    {"role": "user", "content": "Please continue."},
                ]
            try:
                chunk = _stream_ollama_chat(messages, timeout=180.0)
                full_text += chunk
            except Exception as e:
                print(f"[diag] generation attempt {attempt} error: {e}")
                if attempt == 0:
                    _sse_broadcast("activity", {"text": f"⚠ generation error: {e}"})
                    return
                break   # use whatever we have
            if not _is_truncated(full_text):
                break

        if not full_text.strip():
            return

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        turn = {
            "turn_id": f"narrator-{uuid.uuid4().hex[:8]}",
            "is_player": False,
            "narrator_name": "The Fortress",
            "raw_text": full_text,
            "parsed_blocks": _parse_narrator_blocks(full_text),
            "received_at": now,
        }
        _store_narrator_turn(turn)
        try:
            _ingest_narrator_turn_into_world(turn)
        except Exception:
            pass
        _sse_broadcast("turn", turn)
        # Free Ollama's VRAM so image/music generation can use the GPU.
        # Quick call (~0.5s) — does not block the turn broadcast delivery.
        _unload_ollama_vram()
        # Kick off concurrent post-processing (non-blocking)
        _enqueue_image_generation(full_text)
        _broadcast_narrator_mood(full_text)    # [MOOD: "..."] → mood SSE → music
        _broadcast_narrator_sound(full_text)   # [SOUND: "..."] → sfx SSE → sound effects
        _schedule_sense_enrichment(full_text)  # Ollama-enriches any missing sense channels
        _check_dressed_transition(full_text)

    except Exception as e:
        print(f"[diag] _generate_narrator_turn crashed: {e}")
        _sse_broadcast("activity", {"text": f"⚠ narrator error: {e}"})
    finally:
        _sse_broadcast("activity", {"text": ""})
        with _conversation_lock:
            _generating = False
            queued = _narrator_queued
            _narrator_queued = False
        # Drain the 1-deep queue: player submitted while we were busy
        if queued:
            threading.Thread(target=_generate_narrator_turn,
                             daemon=True, name="narrator-queued").start()


# ---------------------------------------------------------------------------
# Image generation — ported from extension/index.js
# ---------------------------------------------------------------------------

_DEFAULT_NEGATIVE_PROMPT = (
    "(text:1.6), (letters:1.6), (words:1.6), (writing:1.6), (typography:1.6), "
    "(captions:1.5), (subtitles:1.5), (signature:1.4), (watermark:1.4), (logo:1.4), "
    "(labels:1.5), (numbers:1.4), (symbols:1.3), (runes:1.3), (glyphs:1.3), "
    "handwriting, scribbles, gibberish, UI, frame, blurry, low quality, distorted, deformed, "
    "(genitals:1.8), (penis:1.8), (vagina:1.8), (explicit nudity:1.8), "
    "(exposed groin:1.7), (sexual:1.6)"
)
_NO_TEXT_SUFFIX = ", (no text:1.4), (no writing:1.4), (no letters:1.4)"
_NUDE_COVERAGE_SUFFIX = (
    ", tastefully posed, back turned, camera angle covers intimate areas, "
    "crossed legs, hands covering, foreground object, shy composition, artful framing, modest"
)
_NUDITY_KEYWORDS = frozenset({
    "naked", "nude", "undressed", "bare skin", "unclothed", "formless",
    "without clothes", "no clothes", "disrobed", "exposed", "bare body",
})
_SD_BLOCKLIST = ["gore", "mutilat", "dismember", "genital", "explicit sex", "child nude", "loli"]
_GENERATE_IMAGE_RE = re.compile(
    r'\[GENERATE_IMAGE(?:\(([^)]*)\))?\s*:\s*"([^"]+)"\]', re.IGNORECASE
)


def _enqueue_image_generation(narrator_text: str) -> None:
    threading.Thread(
        target=_do_image_generation, args=(narrator_text,), daemon=True, name="img-gen"
    ).start()


def _do_image_generation(narrator_text: str) -> None:
    global _latest_scene_image
    markers = _GENERATE_IMAGE_RE.findall(narrator_text)
    if not markers:
        # Auto-gen fallback: extract prose, strip markers and quoted dialogue
        prose = re.sub(r'\[.*?\]', '', narrator_text, flags=re.DOTALL)
        prose = re.sub(r'"[^"]*"', '', prose).replace('*', '').strip()[:350]
        if len(prose) < 40:
            return
        markers = [("location", prose)]

    for kind, description in markers:
        kind = (kind or "location").strip()
        lc = description.lower()
        if any(bad in lc for bad in _SD_BLOCKLIST):
            print(f"[diag] img-gen blocked: content policy match")
            continue
        _sse_broadcast("activity", {"text": f"⏳ rendering {kind}…"})
        neg = _DEFAULT_NEGATIVE_PROMPT + _NO_TEXT_SUFFIX
        if any(kw in lc for kw in _NUDITY_KEYWORDS):
            neg += _NUDE_COVERAGE_SUFFIX
        try:
            payload = json.dumps({"prompt": description, "negative_prompt": neg}).encode("utf-8")
            req = urllib.request.Request(
                f"{FLASK_SD_URL}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            img = data.get("image") or (data.get("images") or [None])[0]
            if img:
                _latest_scene_image = {"image": img, "kind": kind, "description": description}
                _sse_broadcast("scene_image", {"image": img, "kind": kind,
                                               "description": description})
                # Fire async caption prettification — updates tooltip after ~3s
                threading.Thread(target=_prettify_caption,
                                 args=(description, kind), daemon=True,
                                 name="img-caption").start()
        except Exception as e:
            print(f"[diag] img-gen failed for {kind!r}: {e}")
        finally:
            _sse_broadcast("activity", {"text": ""})


def _prettify_caption(description: str, kind: str) -> None:
    """Ask Ollama to convert the SD prompt into a short human-readable scene caption,
    then broadcast it so the UI can update the thumbnail tooltip."""
    try:
        prompt = (
            "Convert this image generation prompt into a short, evocative scene caption "
            "(under 20 words, present tense, no SD jargon, no 'cinematic'/'lighting' terms, "
            "no parentheses or weights):\n\n" + description
        )
        payload = json.dumps({
            "model": _ollama_model(),
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": 60, "temperature": 0.3},
        }).encode("utf-8")
        code, resp, _ = _http("POST", f"{OLLAMA_URL}/api/generate", body=payload, timeout=20.0)
        if code == 200:
            caption = json.loads(resp).get("response", "").strip()
            if caption:
                _sse_broadcast("scene_caption", {"caption": caption, "kind": kind})
    except Exception as e:
        print(f"[diag] caption prettify failed: {e}")


# ---------------------------------------------------------------------------
# Internal sense enrichment — fills missing channels via targeted Ollama calls
# ---------------------------------------------------------------------------

_MOOD_TAG_RE = re.compile(r'\[MOOD\s*:\s*"?([^"\]\n]{3,200})"?\]', re.IGNORECASE)


def _unload_ollama_vram() -> None:
    """Ask Ollama to release the loaded model from VRAM immediately.

    Called after each narrator turn is generated. This frees GPU memory so
    that image generation (flask-sd) and music generation (flask-music) can
    use the full VRAM budget. Ollama will lazy-reload on the next turn.

    Sends a zero-token generate request with keep_alive=0 which is the
    documented way to evict a model from Ollama's VRAM cache.
    Silently swallows errors — music/image still work on CPU as fallback.
    """
    try:
        model = _ollama_model()
        payload = json.dumps({"model": model, "keep_alive": 120}).encode("utf-8")
        _http("POST", f"{OLLAMA_URL}/api/generate", body=payload, timeout=5.0)
    except Exception:
        pass


def _broadcast_narrator_mood(narrator_text: str) -> None:
    """Extract [MOOD: "..."] tags the narrator emitted and fire mood SSE events.

    The sense enrichment only generates MOOD via Ollama when the tag is absent.
    When the narrator explicitly includes it we must still broadcast it so the
    client can request music generation.
    """
    for m in _MOOD_TAG_RE.finditer(narrator_text):
        prompt = m.group(1).strip()
        if prompt:
            _sse_broadcast("mood", {"text": prompt})


_SOUND_TAG_RE = re.compile(r'\[SOUND\s*:\s*"?([^"\]\n]{3,400})"?\]', re.IGNORECASE)


def _broadcast_narrator_sound(narrator_text: str) -> None:
    """Extract [SOUND: "..."] tags and fire sfx SSE events.

    The client receives each sfx event and requests a short sound effect
    from /api/music/sfx. Sound effects play once (or loop if the description
    implies a continuous sound like machinery/fans/engines).
    """
    for m in _SOUND_TAG_RE.finditer(narrator_text):
        description = m.group(1).strip()
        if description:
            _sse_broadcast("sfx", {"text": description})


_SENSE_CHANNELS = ["MOOD", "SIGHT", "SOUND", "SMELL", "TOUCH", "ENVIRONMENT"]


def _schedule_sense_enrichment(narrator_text: str) -> None:
    threading.Thread(
        target=_do_sense_enrichment, args=(narrator_text,), daemon=True, name="sense-enrich"
    ).start()


def _do_sense_enrichment(narrator_text: str) -> None:
    present = {ch for ch in _SENSE_CHANNELS
               if f"[{ch}:" in narrator_text or f"[{ch} :" in narrator_text
               or f"[{ch}]" in narrator_text}
    missing = [ch for ch in _SENSE_CHANNELS if ch not in present]
    if not missing:
        return
    prose = re.sub(r'\[.*?\]', '', narrator_text, flags=re.DOTALL)
    prose = re.sub(r'"[^"]*"', '', prose).replace('*', '').strip()[:600]
    if len(prose) < 40:
        return
    model = _ollama_model()
    for ch in missing:
        try:
            if ch == "MOOD":
                # MOOD generates a MusicGen-style prompt for music generation
                prompt = (
                    f"You are a music direction model for an immersive sci-fi RPG.\n"
                    f"Given this narrator excerpt:\n\n{prose}\n\n"
                    f"Write a music generation prompt (under 20 words) suitable for MusicGen. "
                    f"Describe tempo, instruments, and atmosphere — no lyrics, no vocals. "
                    f"Example: 'slow ambient electronic, deep bass drone, metallic resonance, suspenseful'\n"
                    f"Output ONLY the prompt in English, no explanation."
                )
                options = {"num_predict": 50, "temperature": 0.4}
            else:
                prompt = (
                    f"You are a sense-layer enrichment model for an immersive sci-fi RPG.\n"
                    f"Given this narrator excerpt:\n\n{prose}\n\n"
                    f"Write a single vivid [{ch}] description (1-2 sentences, present tense, "
                    f"second person 'you'). Output ONLY the description text in English, no labels or brackets."
                )
                options = {"num_predict": 150}
            timeout = 90.0 if ch == "MOOD" else 45.0
            payload = json.dumps({
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": options,
            }).encode("utf-8")
            code, resp, _ = _http("POST", f"{OLLAMA_URL}/api/generate", body=payload, timeout=timeout)
            if code == 200:
                text = json.loads(resp).get("response", "").strip()
                if text:
                    if ch == "MOOD":
                        _sse_broadcast("mood", {"text": text})
                    else:
                        _sse_broadcast("sense", {"channel": ch, "text": text})
        except Exception as e:
            print(f"[diag] sense enrichment failed for {ch}: {e}")


def _store_narrator_turn(turn: dict) -> None:
    turn.setdefault("permanence", _auto_permanence(turn))
    _narrator_turns.append(turn)
    try:
        with NARRATOR_TURNS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(turn, default=str) + "\n")
    except Exception:
        pass


def _categorize_log_lines(lines: list[str]) -> dict:
    errors, warnings = [], []
    for line in lines:
        low = line.lower()
        if any(tok in low for tok in ("error", "fatal", "traceback", "exception", "fail")):
            errors.append(line)
        elif "warn" in low:
            warnings.append(line)
    return {"errors": errors[-20:], "warnings": warnings[-20:]}


# ---------------------------------------------------------------------------
# Issue detection — auto-inferred hints for the AI
# ---------------------------------------------------------------------------

def _detect_issues(services: dict, sentinels: dict, log_cat: dict) -> list[dict]:
    """Return a list of {severity, code, message, suggested_action_ids}.

    Detection is intentionally conservative — false positives here waste
    AI context and erode trust. Each rule matches a concrete state, not
    a hunch.
    """
    issues: list[dict] = []

    fsd = services["flask-sd"]
    oll = services["ollama"]
    st = services["sillytavern"]

    # Rule 1: runtime service blocked on a missing sentinel.
    if fsd["status_file"] is None and not sentinels["flask-sd-ready"]:
        issues.append({
            "severity": "error",
            "code": "flask_sd_no_bootstrap",
            "message": "flask-sd has no status file and no ready sentinel — bootstrap has not run.",
            "suggested_action_ids": ["bootstrap.request_rerun"],
        })
    if oll["status_file"] is None and not sentinels["ollama-ready"]:
        issues.append({
            "severity": "error",
            "code": "ollama_no_bootstrap",
            "message": "ollama has no status file and no ready sentinel — bootstrap has not run.",
            "suggested_action_ids": ["bootstrap.request_rerun"],
        })

    # Rule 2: status file claims ready but HTTP probe fails.
    for key, svc in (("flask-sd", fsd), ("ollama", oll)):
        sf = svc["status_file"] or {}
        if sf.get("phase") == "ready" and not svc["probe"]["reachable"]:
            issues.append({
                "severity": "error",
                "code": f"{key.replace('-', '_')}_ready_but_unreachable",
                "message": f"{key} status=ready but HTTP probe failed: {svc['probe']['error']}",
                "suggested_action_ids": [
                    "status.reset",
                    "diag.refresh",
                ],
            })

    # Rule 3: download stalled (bytes_done == bytes_total == 0 while phase=downloading).
    for key, svc in (("flask-sd", fsd), ("ollama", oll)):
        sf = svc["status_file"] or {}
        if sf.get("phase") == "downloading":
            models = sf.get("models") or []
            stalled = [m for m in models
                       if (m.get("bytes_done") or 0) == 0 and (m.get("bytes_total") or 0) == 0]
            if stalled and len(stalled) == len(models):
                issues.append({
                    "severity": "warning",
                    "code": f"{key.replace('-', '_')}_download_no_progress_yet",
                    "message": f"{key} reports phase=downloading but no bytes reported yet — may be sizing, or stalled.",
                    "suggested_action_ids": [],
                })

    # Rule 4: explicit error phase in any status file.
    for key, svc in (("flask-sd", fsd), ("ollama", oll), ("sillytavern", st)):
        sf = svc["status_file"] or {}
        if sf.get("phase") == "error":
            issues.append({
                "severity": "error",
                "code": f"{key.replace('-', '_')}_phase_error",
                "message": f"{key} reports phase=error: {sf.get('error') or '(no detail)'}",
                "suggested_action_ids": ["logs.tail"],
            })

    # Rule 5: recent errors in shared log.
    if log_cat["errors"]:
        issues.append({
            "severity": "warning",
            "code": "recent_log_errors",
            "message": f"{len(log_cat['errors'])} recent error line(s) in diagnostics.log",
            "suggested_action_ids": ["logs.tail"],
        })

    return issues


# ---------------------------------------------------------------------------
# Action catalog — narrow, explicit, schema'd
# ---------------------------------------------------------------------------

def _action_catalog() -> list[dict]:
    """The full list of actions the sidecar can execute. Each entry is
    self-describing so an AI agent can pick an action by id, read the
    params schema, and POST to /actions/<id>."""
    return [
        {
            "id": "ollama.list",
            "summary": "List models currently present in the ollama data volume.",
            "params": {},
            "side_effects": "none",
            "risk": "safe",
            "requires_host": False,
        },
        {
            "id": "ollama.pull",
            "summary": "Pull or update an ollama model. Requires bootstrap-net egress — will fail on play-net-only warm boots.",
            "params": {"model": {"type": "string", "example": "mistral"}},
            "side_effects": "downloads bytes into ollama-data volume",
            "risk": "moderate",
            "requires_host": False,
        },
        {
            "id": "ollama.delete",
            "summary": "Remove an ollama model from the data volume. Next bootstrap run will re-download.",
            "params": {"model": {"type": "string", "example": "mistral"}},
            "side_effects": "deletes files from ollama-data volume",
            "risk": "destructive",
            "requires_host": False,
        },
        {
            "id": "ollama.unload",
            "summary": "Free VRAM by asking ollama to unload a model (generate with keep_alive=0).",
            "params": {"model": {"type": "string", "example": "mistral"}},
            "side_effects": "releases GPU memory",
            "risk": "safe",
            "requires_host": False,
        },
        {
            "id": "status.reset",
            "summary": "Delete all per-service status JSONs. Services will re-publish on next tick. Does NOT delete sentinels, so runtime services stay happy.",
            "params": {},
            "side_effects": "truncates coordination state",
            "risk": "moderate",
            "requires_host": False,
        },
        {
            "id": "logs.tail",
            "summary": "Return the last N lines of /remnant-status/diagnostics.log.",
            "params": {"n": {"type": "integer", "default": 120, "max": 500}},
            "side_effects": "none",
            "risk": "safe",
            "requires_host": False,
        },
        {
            "id": "logs.clear",
            "summary": "Truncate the shared diagnostics log.",
            "params": {},
            "side_effects": "deletes log content",
            "risk": "moderate",
            "requires_host": False,
        },
        {
            "id": "bootstrap.request_rerun",
            "summary": "Drop an advisory flag file telling a human operator (or an outer script) to run `docker compose --profile bootstrap up`. This sidecar cannot start compose services itself.",
            "params": {"reason": {"type": "string"}},
            "side_effects": "writes /remnant-status/bootstrap-rerun-requested",
            "risk": "safe",
            "requires_host": True,
        },
        {
            "id": "services.restart",
            "summary": "Restart runtime services. NOT EXECUTED by this sidecar — it has no docker socket. Returns host command to run instead.",
            "params": {"service": {"type": "string", "enum": ["flask-sd", "ollama", "sillytavern", "all"]}},
            "side_effects": "(informational)",
            "risk": "safe",
            "requires_host": True,
        },
        {
            "id": "diag.refresh",
            "summary": "No-op — invalidates the AI snapshot so the caller can re-fetch /ai.json.",
            "params": {},
            "side_effects": "none",
            "risk": "safe",
            "requires_host": False,
        },
        {
            "id": "logs.tail_service",
            "summary": "Return the last N lines from diagnostics.log that belong to a specific service.",
            "params": {
                "service": {"type": "string", "enum": list(_KNOWN_SERVICES),
                            "example": "flask-sd"},
                "n": {"type": "integer", "default": 40, "max": 200},
            },
            "side_effects": "none",
            "risk": "safe",
            "requires_host": False,
        },
    ]


def _suggested_actions(issues: list[dict], services: dict) -> list[str]:
    ids: list[str] = []
    for issue in issues:
        for a in issue.get("suggested_action_ids", []):
            if a not in ids:
                ids.append(a)
    # Always offer the safe introspection actions.
    for safe in ("logs.tail", "ollama.list"):
        if safe not in ids:
            ids.append(safe)
    return ids


# ---------------------------------------------------------------------------
# Action execution
# ---------------------------------------------------------------------------

def _log(line: str) -> None:
    try:
        STATUS_DIR.mkdir(parents=True, exist_ok=True)
        with DIAG_LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] [diag] {line}\n")
    except Exception:
        pass


def _exec_action(action_id: str, params: dict) -> tuple[int, dict]:
    _log(f"action requested: {action_id} params={params}")
    try:
        if action_id == "ollama.list":
            code, body, _ = _http("GET", f"{OLLAMA_URL}/api/tags", timeout=5.0)
            return (200 if code == 200 else 502), {"ok": code == 200, "upstream_status": code,
                                                    "body": _maybe_json(body)}

        if action_id == "ollama.pull":
            model = params.get("model")
            if not model:
                return 400, {"ok": False, "error": "param 'model' is required"}
            payload = json.dumps({"name": model, "stream": False}).encode("utf-8")
            code, body, latency = _http("POST", f"{OLLAMA_URL}/api/pull",
                                        body=payload, timeout=1800.0)
            return (200 if code == 200 else 502), {
                "ok": code == 200, "upstream_status": code,
                "latency_ms": round(latency, 1), "body": _maybe_json(body),
                "note": "If this failed with a DNS/network error, play-net has no egress. Re-run bootstrap profile.",
            }

        if action_id == "ollama.delete":
            model = params.get("model")
            if not model:
                return 400, {"ok": False, "error": "param 'model' is required"}
            payload = json.dumps({"name": model}).encode("utf-8")
            code, body, _ = _http("DELETE", f"{OLLAMA_URL}/api/delete",
                                  body=payload, timeout=30.0)
            return (200 if code in (200, 204) else 502), {
                "ok": code in (200, 204), "upstream_status": code, "body": _maybe_json(body),
            }

        if action_id == "ollama.unload":
            model = params.get("model") or "mistral"
            payload = json.dumps({"model": model, "keep_alive": 0}).encode("utf-8")
            code, body, _ = _http("POST", f"{OLLAMA_URL}/api/generate",
                                  body=payload, timeout=15.0)
            return (200 if code == 200 else 502), {
                "ok": code == 200, "upstream_status": code, "body": _maybe_json(body),
            }

        if action_id == "status.reset":
            removed = []
            for name in ("flask-sd.json", "ollama.json", "sillytavern.json"):
                p = STATUS_DIR / name
                if p.exists():
                    try:
                        p.unlink()
                        removed.append(name)
                    except Exception as e:
                        return 500, {"ok": False, "error": f"unlink {name} failed: {e}"}
            return 200, {"ok": True, "removed": removed}

        if action_id == "logs.tail":
            n = int(params.get("n", 120))
            n = max(1, min(n, 500))
            return 200, {"ok": True, "lines": _tail_log(n)}

        if action_id == "logs.tail_service":
            service = params.get("service", "")
            if not service:
                return 400, {"ok": False, "error": "param 'service' is required"}
            if service not in _KNOWN_SERVICES:
                return 400, {"ok": False, "error": f"unknown service '{service}'. Known: {list(_KNOWN_SERVICES)}"}
            n = int(params.get("n", 40))
            n = max(1, min(n, 200))
            return 200, {"ok": True, "service": service, "lines": _tail_service_log(service, n)}

        if action_id == "logs.clear":
            try:
                if DIAG_LOG.exists():
                    DIAG_LOG.write_text("", encoding="utf-8")
                return 200, {"ok": True}
            except Exception as e:
                return 500, {"ok": False, "error": str(e)}

        if action_id == "bootstrap.request_rerun":
            reason = params.get("reason", "(no reason given)")
            flag = STATUS_DIR / "bootstrap-rerun-requested"
            try:
                flag.write_text(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {reason}\n",
                                encoding="utf-8")
                _log(f"bootstrap rerun requested: {reason}")
                return 200, {
                    "ok": True,
                    "flag_file": str(flag),
                    "host_command": "docker compose --profile bootstrap up",
                    "note": "Advisory only — diag cannot start compose services itself.",
                }
            except Exception as e:
                return 500, {"ok": False, "error": str(e)}

        if action_id == "services.restart":
            service = params.get("service", "all")
            mapping = {
                "flask-sd": "docker compose restart flask-sd",
                "ollama": "docker compose restart ollama",
                "sillytavern": "docker compose restart sillytavern",
                "all": "docker compose restart flask-sd ollama sillytavern nginx",
            }
            cmd = mapping.get(service)
            if cmd is None:
                return 400, {"ok": False, "error": f"unknown service: {service}"}
            return 200, {
                "ok": True,
                "executed": False,
                "host_command": cmd,
                "note": "diag has no docker socket — run this on the host.",
            }

        if action_id == "diag.refresh":
            return 200, {"ok": True, "note": "Re-fetch /ai.json."}

        if action_id == "diag.reload_prompt":
            _load_fortress_texts()
            return 200, {"ok": True, "chars": len(_system_prompt),
                         "note": "System prompt reloaded from disk."}

        return 404, {"ok": False, "error": f"unknown action: {action_id}"}

    except Exception as e:
        _log(f"action {action_id} crashed: {e}")
        return 500, {"ok": False, "error": str(e), "traceback": traceback.format_exc()}


def _maybe_json(body: bytes):
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return body.decode("utf-8", errors="replace")[:2000]


# ---------------------------------------------------------------------------
# Signature — composite fingerprint of all content + logic files
# ---------------------------------------------------------------------------

# Files that define "this version of the game". Paths are relative to the
# repo root (or /app in Docker). Missing files contribute an ABSENT marker
# so the signature still reflects what's actually present.
_SIGNATURE_FILES = [
    "docker/diag/app.py",
    "web/index.html",
    "extension/index.js",
    "docker/sillytavern/content/characters/fortress_system_prompt.txt",
    "docker/sillytavern/content/characters/fortress_first_mes.txt",
    "docker/sillytavern/content/worlds/Nullspace Nexus.json",
    "docker/sillytavern/content/chats/The Remnant/Opening.jsonl",
    "docker/sillytavern/content/settings.json",
    "docker/sillytavern/config.yaml",
]

_REPO_ROOT = Path(__file__).parent.parent.parent  # docker/diag/app.py → docker/diag → docker → repo root


def _build_bootstrap_manifest() -> dict:
    """Return the list of large bootstrapped model files and their sentinel status.

    Each entry describes one bootstrap component: which models it downloads,
    the sentinel file that marks it complete, and whether that sentinel exists.
    This lets /bootstrap-manifest serve as a definitive answer to
    "did all the big downloads complete?"
    """
    status_dir = Path(STATUS_DIR)

    components = [
        {
            "id": "flask-sd",
            "label": "Image backend (Stable Diffusion v1.5 + IP-Adapter)",
            "sentinel": "flask-sd-ready",
            "models": [
                {
                    "key": "sd15",
                    "repo": "runwayml/stable-diffusion-v1-5",
                    "description": "Stable Diffusion v1.5 fp16 weights",
                    "size_estimate_mb": 4000,
                    "license": "CreativeML Open RAIL-M",
                },
                {
                    "key": "ip-adapter",
                    "repo": "h94/IP-Adapter",
                    "description": "IP-Adapter Plus for SD 1.5",
                    "size_estimate_mb": 800,
                    "license": "Apache 2.0",
                },
            ],
        },
        {
            "id": "flask-music",
            "label": "Audio backend (MusicGen Small)",
            "sentinel": "flask-music-ready",
            "models": [
                {
                    "key": "musicgen-small",
                    "repo": "facebook/musicgen-small",
                    "description": "MusicGen Small — ambient music generation",
                    "size_estimate_mb": 1200,
                    "license": "CC-BY-NC 4.0",
                },
            ],
        },
        {
            "id": "ollama",
            "label": "Language backend (Ollama model)",
            "sentinel": "ollama-ready",
            "models": [
                {
                    "key": "ollama-model",
                    "repo": f"ollama/{_ollama_model()}",
                    "description": f"{_ollama_model()} via Ollama registry",
                    "size_estimate_mb": 9000,
                    "license": "varies by model",
                },
            ],
        },
    ]

    out = []
    all_ready = True
    for comp in components:
        sentinel_path = status_dir / comp["sentinel"]
        ready = sentinel_path.exists()
        if not ready:
            all_ready = False
        # Read the matching status JSON if present
        status_json = None
        sj_path = status_dir / f"{comp['id']}.json"
        if sj_path.exists():
            try:
                status_json = json.loads(sj_path.read_text())
            except Exception:
                pass
        out.append({
            "id": comp["id"],
            "label": comp["label"],
            "sentinel": comp["sentinel"],
            "sentinel_present": ready,
            "status_phase": (status_json or {}).get("phase"),
            "models": comp["models"],
        })

    return {
        "all_ready": all_ready,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "components": out,
        "note": "Run 'docker compose --profile bootstrap up' to download missing models.",
    }


def _build_signature() -> dict:
    """Return a composite fingerprint of every content + logic file.

    The composite_sha256 is a deterministic hash of all per-file hashes in
    sorted-filename order. If every file is identical between two builds, the
    composite will match. Any change to any file — system prompt, UI, lore,
    chat opening — changes the composite. Useful for verifying that `docker
    compose build` included the expected content.
    """
    files_out = {}
    composite_input = []

    for rel in sorted(_SIGNATURE_FILES):
        path = _REPO_ROOT / rel
        if path.exists():
            try:
                h = hashlib.sha256(path.read_bytes()).hexdigest()
                size = path.stat().st_size
                files_out[rel] = {"sha256": h, "size_bytes": size, "status": "ok"}
                composite_input.append(f"{rel}:{h}")
            except Exception as e:
                files_out[rel] = {"sha256": None, "status": f"error: {e}"}
                composite_input.append(f"{rel}:ERROR")
        else:
            files_out[rel] = {"sha256": None, "status": "absent"}
            composite_input.append(f"{rel}:ABSENT")

    composite_sha256 = hashlib.sha256(
        "\n".join(composite_input).encode("utf-8")
    ).hexdigest()

    # Git commit baked at build time (Dockerfile writes /app/GIT_COMMIT),
    # or read live if running natively in the repo.
    git_commit = "unknown"
    commit_file = _REPO_ROOT / "GIT_COMMIT"
    if commit_file.exists():
        git_commit = commit_file.read_text().strip()
    else:
        try:
            git_commit = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(_REPO_ROOT), stderr=subprocess.DEVNULL, timeout=2
            ).decode().strip()
        except Exception:
            pass

    absent = [r for r, v in files_out.items() if v["status"] == "absent"]
    errors = [r for r, v in files_out.items() if v["status"].startswith("error")]

    return {
        "composite_sha256": composite_sha256,
        "git_commit": git_commit,
        "schema_version": SCHEMA_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "ok" if not absent and not errors else ("degraded" if not errors else "error"),
        "absent_files": absent,
        "error_files": errors,
        "files": files_out,
    }


# ---------------------------------------------------------------------------
# AI snapshot builder
# ---------------------------------------------------------------------------

def _build_ai_snapshot() -> dict:
    flask_sd_status = _read_status_file("flask-sd")
    ollama_status = _read_status_file("ollama")
    sillytavern_status = _read_status_file("sillytavern")

    flask_sd_probe = _probe_json(f"{FLASK_SD_URL}/api/health")
    ollama_probe = _probe_json(f"{OLLAMA_URL}/api/tags")
    # SillyTavern root returns HTML, not JSON — reachable check only.
    code, body, latency = _http("GET", f"{SILLYTAVERN_URL}/", timeout=3.0)
    st_probe = {
        "url": f"{SILLYTAVERN_URL}/",
        "reachable": 200 <= code < 400,
        "status_code": code,
        "latency_ms": round(latency, 1),
        "body": None,
        "error": None if 200 <= code < 400 else body.decode("utf-8", errors="replace")[:400],
    }
    fm_code, _, fm_lat = _http("GET", f"{FLASK_MUSIC_URL}/health", timeout=2.0)
    fm_probe = {
        "reachable": fm_code == 200,
        "latency_ms": round(fm_lat * 1000, 1) if fm_lat else None,
    }

    services = {
        "flask-sd": {"status_file": flask_sd_status, "probe": flask_sd_probe},
        "ollama": {"status_file": ollama_status, "probe": ollama_probe},
        "sillytavern": {"status_file": sillytavern_status, "probe": st_probe},
        "flask-music": {"probe": fm_probe},
    }

    sentinels = _sentinels()
    log_lines = _tail_log(120)
    log_cat = _categorize_log_lines(log_lines)
    issues = _detect_issues(services, sentinels, log_cat)
    suggested = _suggested_actions(issues, services)

    # One-line summary for AI quick-read.
    if any(i["severity"] == "error" for i in issues):
        summary = f"DEGRADED — {sum(1 for i in issues if i['severity']=='error')} error(s), {sum(1 for i in issues if i['severity']=='warning')} warning(s)."
    elif issues:
        summary = f"WARNING — {len(issues)} advisory(ies), no hard errors."
    elif all(services[k]["probe"]["reachable"] for k in services):
        summary = "HEALTHY — all runtime services reachable, no issues detected."
    else:
        summary = "STARTING — services not all reachable yet, no errors raised."

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "summary": summary,
        "sidecar_uptime_s": round(time.time() - _sidecar_start, 1),
        "browser_console": dict(_browser_health),
        "services": services,
        "sentinels": sentinels,
        "recent_log": {
            "all": log_lines[-60:],
            "errors": log_cat["errors"],
            "warnings": log_cat["warnings"],
        },
        "per_service_log": {
            svc: _tail_service_log(svc, 20)
            for svc in ("flask-sd", "ollama", "sillytavern", "diag")
        },
        "detected_issues": issues,
        "suggested_action_ids": suggested,
        "action_catalog": _action_catalog(),
        "environment": {
            "status_dir": str(STATUS_DIR),
            "flask_sd_url": FLASK_SD_URL,
            "ollama_url": OLLAMA_URL,
            "sillytavern_url": SILLYTAVERN_URL,
        },
        "notes_for_ai": [
            "This snapshot is self-contained — all data you need to diagnose should be in this payload.",
            "To remediate, POST to /diagnostics/actions/<id> with a JSON body matching the action's params schema.",
            "Actions with requires_host=true are informational only — this sidecar has no docker socket.",
            "If 'ready but unreachable' — prefer status.reset + diag.refresh before asking for a services.restart.",
        ],
    }


# ---------------------------------------------------------------------------
# System metrics sampler — GPU via nvidia-smi, RAM/CPU via psutil or /proc
# ---------------------------------------------------------------------------

def _sample_system_metrics() -> dict:
    """Return a best-effort snapshot of GPU, RAM, CPU, and storage usage."""
    out: dict = {"gpu": None, "ram": None, "cpu": None, "storage": None, "ts": time.time()}

    # GPU — nvidia-smi (available in CUDA containers and native Windows with drivers)
    try:
        raw = subprocess.check_output(
            ["nvidia-smi",
             "--query-gpu=name,utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            text=True, timeout=2,
        )
        parts = [p.strip() for p in raw.strip().split(",")]
        if len(parts) >= 4:
            out["gpu"] = {
                "name": parts[0],
                "util_pct": int(parts[1]),
                "vram_used_mb": int(parts[2]),
                "vram_total_mb": int(parts[3]),
            }
    except Exception:
        pass

    # RAM + CPU — psutil preferred (available on native; not in the slim Docker image)
    try:
        import psutil  # type: ignore
        vm = psutil.virtual_memory()
        out["ram"] = {
            "used_mb": vm.used // 1024 ** 2,
            "total_mb": vm.total // 1024 ** 2,
            "pct": vm.percent,
        }
        out["cpu"] = {"pct": psutil.cpu_percent(interval=None)}
    except ImportError:
        # Linux fallback via /proc/meminfo (available in Docker containers)
        try:
            mem: dict = {}
            with open("/proc/meminfo") as fh:
                for ln in fh:
                    k, v = ln.split(":", 1)
                    mem[k.strip()] = int(v.split()[0])
            total = mem["MemTotal"]
            avail = mem["MemAvailable"]
            out["ram"] = {
                "used_mb": (total - avail) // 1024,
                "total_mb": total // 1024,
                "pct": round(100 * (total - avail) / total, 1),
            }
        except Exception:
            pass

    # Storage — root volume (/) — works in Linux containers; skipped on Windows
    try:
        st = os.statvfs("/")
        total_gb = st.f_blocks * st.f_frsize / 1e9
        free_gb = st.f_bavail * st.f_frsize / 1e9
        out["storage"] = {
            "free_gb": round(free_gb, 1),
            "total_gb": round(total_gb, 1),
            "pct": round(100 * (1 - free_gb / total_gb), 1),
        }
    except Exception:
        pass

    return out


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "remnant-diag/1"

    def log_message(self, fmt, *args):  # quiet default access log
        pass

    def _send_json(self, code: int, payload) -> None:
        body = json.dumps(payload, indent=2, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, code: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/":
            self._send_json(200, {
                "service": "remnant-diag",
                "endpoints": [
                    "/ai.json",
                    "/signature (GET — composite SHA256 fingerprint of all content + logic files)",
                    "/bootstrap-manifest (GET — list of large bootstrapped models + sentinel status)",
                    "/actions",
                    "/actions/<id> (POST)",
                    "/browser-health (POST)",
                    "/narrator-turn (POST)",
                    "/narrator-turns (GET, ?n=50)",
                    "/world-state (GET, ?type=location|npc|item|player)",
                    "/events (GET, text/event-stream — SSE for game UI)",
                    "/logs/<service> (GET, plain-text)",
                    "/player-input (POST — SAY/DO/SENSE or META:* command)",
                    "/pending-player-input (GET — consume-once relay for extension)",
                    "/pending-reset (GET — consume-once reset signal for extension)",
                    "/pending-portrait (GET — consume-once portrait signal for extension)",
                    "/pending-player-context (GET — consume-once player identity switch for extension)",
                    "/forever (GET — canon facts log)",
                    "/forever-portrait (POST — promote image URL to forever canon)",
                    "/reset (POST or GET ?level=scene|story|world|all)",
                    "/system-metrics (GET — GPU/RAM/CPU/storage snapshot, cached 4s)",
                    "/activity (GET/POST — current extension activity string + SSE broadcast)",
                ],
            })
            return
        if path == "/ai.json":
            try:
                self._send_json(200, _build_ai_snapshot())
            except Exception as e:
                self._send_json(500, {"error": str(e), "traceback": traceback.format_exc()})
            return
        if path == "/signature":
            try:
                self._send_json(200, _build_signature())
            except Exception as e:
                self._send_json(500, {"error": str(e), "traceback": traceback.format_exc()})
            return
        if path == "/bootstrap-manifest":
            try:
                self._send_json(200, _build_bootstrap_manifest())
            except Exception as e:
                self._send_json(500, {"error": str(e), "traceback": traceback.format_exc()})
            return
        if path == "/actions":
            self._send_json(200, {"actions": _action_catalog()})
            return
        if path == "/narrator-turns":
            qs = parse_qs(urlparse(self.path).query)
            n = int(qs.get("n", ["50"])[0])
            n = max(1, min(n, 200))
            turns = list(_narrator_turns)[-n:]
            self._send_json(200, {"turns": turns, "count": len(turns), "total_seen": len(_narrator_turns)})
            return

        if path == "/events":
            # Server-Sent Events stream for the v3.0 game UI.
            # Immediately replays the last 30 turns as a "history" event,
            # then streams new turns live. Heartbeat every 15 s keeps the
            # connection alive through proxies.
            q: _queue.Queue = _queue.Queue(maxsize=120)
            with _sse_lock:
                _sse_clients.add(q)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                # Initial history burst
                history = list(_narrator_turns)[-30:]
                if history:
                    msg = f"event: history\ndata: {json.dumps(history, separators=(',', ':'))}\n\n"
                    self.wfile.write(msg.encode())
                    self.wfile.flush()
                # Live stream
                while True:
                    try:
                        raw = q.get(timeout=15)
                        self.wfile.write(raw)
                        self.wfile.flush()
                    except _queue.Empty:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with _sse_lock:
                    _sse_clients.discard(q)
            return

        if path == "/world-state":
            qs = parse_qs(urlparse(self.path).query)
            etype = qs.get("type", [None])[0]
            entities = list(_world["entities"].values())
            if etype:
                entities = [e for e in entities if e.get("type") == etype]
            self._send_json(200, {
                "turn_count": _world["turn_count"],
                "entity_count": len(_world["entities"]),
                "entities": entities,
            })
            return

        if path == "/pending-player-input":
            global _pending_player_input
            result = _pending_player_input
            _pending_player_input = None  # consume once
            self._send_json(200, result or {})
            return

        if path == "/pending-reset":
            global _pending_reset
            result = _pending_reset
            _pending_reset = None  # consume once
            self._send_json(200, result or {})
            return

        if path == "/pending-portrait":
            global _pending_portrait
            result = _pending_portrait
            _pending_portrait = None  # consume once
            self._send_json(200, {"pending": result} if result else {})
            return

        if path == "/pending-player-context":
            global _pending_player_context
            result = _pending_player_context
            _pending_player_context = None  # consume once
            self._send_json(200, result or {})
            return

        if path == "/scene-image":
            # NOT consume-once — returns latest for reconnect hydration.
            self._send_json(200, _latest_scene_image or {})
            return

        if path == "/api/music/generate":
            # Proxy music generation request to flask-music service (GET returns 405)
            self._send_json(405, {"error": "POST only"})
            return

        if path == "/system-metrics":
            global _metrics_cache, _metrics_cache_time
            if time.time() - _metrics_cache_time > 4:
                _metrics_cache = _sample_system_metrics()
                _metrics_cache_time = time.time()
            self._send_json(200, _metrics_cache)
            return

        if path == "/activity":
            self._send_json(200, {"text": _current_activity})
            return

        if path == "/forever":
            self._send_json(200, {"entries": _load_forever_log()})
            return

        # Per-service log tail: GET /logs/<service>
        # Also serves native-run log files (flask-stt, flask-tts, flask-music, launcher, diag)
        if path.startswith("/logs/"):
            service = path[len("/logs/"):]
            # Native log files take priority (present in native dev mode)
            # STATUS_DIR could be anywhere; use NATIVE_RUN_LOG_DIR env or infer from script location
            _native_run_dir = Path(os.environ.get(
                "NATIVE_RUN_LOG_DIR",
                str(Path(__file__).parent.parent.parent / "logs" / "native-run"),
            ))
            _NATIVE_LOG_MAP = {
                "flask-stt":   _native_run_dir / "flask-stt.log",
                "flask-tts":   _native_run_dir / "flask-tts.log",
                "flask-music": _native_run_dir / "flask-music.log",
                "launcher":    _native_run_dir / "launcher.log",
                "diag":        _native_run_dir / "diag.log",
            }
            native_path = _NATIVE_LOG_MAP.get(service)
            if native_path and native_path.exists():
                try:
                    text = native_path.read_text(encoding="utf-8", errors="replace")
                    tail = "\n".join(text.splitlines()[-150:])
                    self._send_text(200, tail + "\n")
                    return
                except Exception:
                    pass
            if not service or service not in _KNOWN_SERVICES:
                self._send_json(404, {
                    "error": f"unknown service '{service}'",
                    "known": list(_KNOWN_SERVICES) + list(_NATIVE_LOG_MAP.keys()),
                })
                return
            lines = _tail_service_log(service, 100)
            self._send_text(200, "\n".join(lines) + ("\n" if lines else ""))
            return
        self._send_json(404, {"error": "not found", "path": path})

    def do_POST(self) -> None:  # noqa: N802
        global _browser_health
        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        # Browser console health report — POSTed by warm_test.py after
        # a Playwright boot of the stack. Populates browser_console in /ai.json.
        if path == "/browser-health":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                d = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                d = {}
            _browser_health = {
                "errors": int(d.get("errors", 0)),
                "warnings": int(d.get("warnings", 0)),
                "reported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            _log(f"browser-health: console_err={_browser_health['errors']} console_warn={_browser_health['warnings']}")
            self._send_json(200, {"ok": True})
            return

        if path == "/narrator-turn":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                turn = json.loads(raw.decode("utf-8")) if raw else {}
                if not isinstance(turn, dict):
                    raise ValueError("body must be a JSON object")
                turn.setdefault("received_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
                _store_narrator_turn(turn)
                try:
                    _ingest_narrator_turn_into_world(turn)
                except Exception:
                    pass  # world graph never crashes the turn pipeline
                _sse_broadcast("turn", turn)
                self._send_json(200, {"ok": True})
            except Exception as e:
                self._send_json(400, {"ok": False, "error": str(e)})
            return

        if path == "/player-input":
            global _pending_player_input
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                text = payload.get("text", "").strip()
                if not text:
                    self._send_json(400, {"ok": False, "error": "empty text"})
                    return
                intent = _sorting_hat(text)

                # META intents are primarily sidecar-only, but PLAYER_NAME / PLAYER_ALIAS
                # can relay to SillyTavern so the narrator can acknowledge the declaration.
                if intent.startswith("META:"):
                    sub = intent.split(":", 1)[1]
                    if sub.startswith("PLAYER_"):
                        result = _handle_player_identity(text, sub)
                    else:
                        result = _handle_meta_command(text, sub)
                    self._send_json(200, result)
                    # Trigger narrator generation for cases that need a response:
                    #   relay=True  → player turn already stored; same-person name confirm etc.
                    #   PLAYER_SWITCH / PLAYER_RESTORE → new player; narrator should react
                    if result.get("relay"):
                        # Player turn was already stored inside _handle_player_identity relay path
                        wrapped = _wrap_player_text("SAY", text)
                        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                        turn = {
                            "turn_id": f"player-{uuid.uuid4().hex[:8]}",
                            "is_player": True,
                            "raw_text": wrapped,
                            "parsed_blocks": [{"text": wrapped, "channel": "say", "isPlayer": True}],
                            "received_at": now,
                        }
                        _store_narrator_turn(turn)
                        _sse_broadcast("turn", turn)
                        threading.Thread(target=_generate_narrator_turn,
                                         daemon=True, name="narrator").start()
                    elif result.get("meta") in ("PLAYER_SWITCH", "PLAYER_RESTORE"):
                        # Store player's declaration so narrator has context, then generate
                        wrapped = _wrap_player_text("SAY", text)
                        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                        turn = {
                            "turn_id": f"player-{uuid.uuid4().hex[:8]}",
                            "is_player": True,
                            "raw_text": wrapped,
                            "parsed_blocks": [{"text": wrapped, "channel": "say", "isPlayer": True}],
                            "received_at": now,
                        }
                        _store_narrator_turn(turn)
                        _sse_broadcast("turn", turn)
                        threading.Thread(target=_generate_narrator_turn,
                                         daemon=True, name="narrator").start()
                    return

                wrapped = _wrap_player_text(intent, text)
                now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                turn = {
                    "turn_id": f"player-{uuid.uuid4().hex[:8]}",
                    "is_player": True,
                    "raw_text": wrapped,
                    "parsed_blocks": [{"text": wrapped, "channel": intent.lower(), "isPlayer": True}],
                    "received_at": now,
                }
                _store_narrator_turn(turn)
                _sse_broadcast("turn", turn)
                # Direct Ollama generation — no longer relies on ST extension relay
                threading.Thread(target=_generate_narrator_turn,
                                 daemon=True, name="narrator").start()
                self._send_json(200, {"ok": True, "intent": intent, "wrapped": wrapped})
            except Exception as e:
                self._send_json(400, {"ok": False, "error": str(e)})
            return

        if path == "/reset":
            qs = parse_qs(urlparse(self.path).query)
            level = (qs.get("level", ["scene"])[0]).lower()
            # Accept level from query string OR JSON body
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                try:
                    body_data = json.loads(self.rfile.read(length).decode("utf-8"))
                    level = body_data.get("level", level)
                except Exception:
                    pass
            level = level if level in ("exchange", "scene", "story", "world", "all") else "scene"
            result = _handle_meta_command(f"reset the {level}", "RESET")
            self._send_json(200, result)
            return

        if path == "/scene-image":
            global _latest_scene_image
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
                img = data.get("image", "").strip()
                kind = data.get("kind", "location")
                if img:
                    _latest_scene_image = {"image": img, "kind": kind}
                    _sse_broadcast("scene_image", {"image": img, "kind": kind})
                    self._send_json(200, {"ok": True})
                else:
                    self._send_json(400, {"ok": False, "error": "image required"})
            except Exception as e:
                self._send_json(400, {"ok": False, "error": str(e)})
            return

        if path == "/api/music/generate":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                req_data = json.loads(raw.decode("utf-8")) if raw else {}
                prompt = req_data.get("prompt", "calm ambient sci-fi")
                duration = min(int(req_data.get("duration", 30)), 60)
                payload = json.dumps({"prompt": prompt, "duration": duration}).encode("utf-8")
                code, resp, _ = _http("POST", f"{FLASK_MUSIC_URL}/api/generate",
                                      body=payload, timeout=360.0)
                self._send_json(code, json.loads(resp) if resp else {})
            except Exception as e:
                self._send_json(503, {"error": str(e)})
            return

        if path == "/activity":
            global _current_activity
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                data = {}
            _current_activity = data.get("text", "")
            _sse_broadcast("activity", {"text": _current_activity})
            self._send_json(200, {"ok": True})
            return

        if path == "/sense-enrichment":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                data = {}
            # Broadcast immediately — clients accumulate sense lines in the feed
            _sse_broadcast("sense", data)
            self._send_json(200, {"ok": True})
            return

        if path == "/forever-portrait":
            global _pending_portrait
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
                url = data.get("url", "").strip()
                if url:
                    _append_forever({
                        "type": "portrait",
                        "key": "__narrator_portrait__",
                        "url": url,
                        "permanence": "FOREVER",
                    })
                    _sse_broadcast("meta", {"type": "portrait_saved", "url": url})
                    self._send_json(200, {"ok": True, "url": url})
                else:
                    self._send_json(400, {"ok": False, "error": "url required"})
            except Exception as e:
                self._send_json(400, {"ok": False, "error": str(e)})
            return

        if not path.startswith("/actions/"):
            self._send_json(404, {"error": "not found", "path": path})
            return
        action_id = path[len("/actions/"):]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            params = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(params, dict):
                raise ValueError("body must be a JSON object")
        except Exception as e:
            self._send_json(400, {"ok": False, "error": f"bad JSON body: {e}"})
            return
        code, payload = _exec_action(action_id, params)
        self._send_json(code, payload)


def main() -> None:
    _load_fortress_texts()
    _restore_narrator_turns()
    _replay_world_log()
    _log(f"remnant-diag starting on :{LISTEN_PORT}")
    srv = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
