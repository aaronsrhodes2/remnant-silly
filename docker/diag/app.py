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

# On Windows, suppress the brief console flash that appears when spawning child
# processes from a non-console (or background-thread) context.
_SUBPROCESS_NO_WINDOW = (
    {"creationflags": subprocess.CREATE_NO_WINDOW}
    if hasattr(subprocess, "CREATE_NO_WINDOW")
    else {}
)
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
_classifying: bool = False              # True while _sorting_hat's Ollama call is in flight
_narrator_queued: bool = False          # True when a player input arrived during generation
_system_prompt: str = ""               # Loaded from fortress_system_prompt.txt at startup
_first_mes: str = ""                   # Loaded from fortress_first_mes.txt at startup

# Ollama watchdog — health-checks every 30s; broadcasts SSE warning after 2 consecutive failures.
_ollama_healthy: bool = True
_ollama_fail_count: int = 0

# System metrics cache — refreshed at most every 4 s to avoid hammering nvidia-smi.
_metrics_cache: dict = {}
_metrics_cache_time: float = 0.0

# Lore idle narration — The Fortress speaks lore aloud during quiet moments.
_last_player_ts: float = time.time()  # reset on every player-input
_spoken_lore_keys: set = set()        # avoid re-reading recently narrated lore
LORE_IDLE_SECS = 50                   # seconds of silence before lore fires

# Ghost scout — speculative pre-generation for locations the narrator is pushing.
# When a known location accumulates ≥ HOOK_HEAT_THRESHOLD mentions across turns
# (without the player being there), a silent background scout runs: it asks Ollama
# for atmosphere tags and calls flask-sd for images, storing results in a prefetch
# cache.  On arrival the Sorting Hat serves cached content immediately.
_hook_heat: dict            = {}   # location_name → mention count (decays on visit)
_location_prefetch_cache: dict = {}  # location_name → {images, mood, sense_beats, generated_at}
_scout_running: bool        = False  # True while a scout thread is active
HOOK_HEAT_THRESHOLD         = 3     # mentions before scout fires
SCOUT_CACHE_EXPIRE_SECS     = 600   # 10-minute TTL (stale world = wrong atmosphere)
SCOUT_MAX_CACHE             = 3     # max simultaneous cached locations

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
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "1591"))

# Directory containing fortress_system_prompt.txt / fortress_first_mes.txt
# (written by update_fortress_card.py; lives alongside world.json in seed/).
# Default: repo-relative path works in both Docker and native modes.
FORTRESS_CARD_DIR = Path(os.environ.get(
    "FORTRESS_CARD_DIR",
    Path(__file__).parent / "seed",
))

# ChromaDB — semantic memory / RAG retrieval for narrator context.
# Optional: degrades gracefully when chromadb is not installed.
CHROMA_DB_PATH = Path(os.environ.get(
    "CHROMA_DB_PATH",
    STATUS_DIR.parent / "chroma-db",
))
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")

# Permanent world seed — baseline locations, NPCs, and lore that survive
# all resets and are reloaded on every startup and after "Reset World".
SEED_PATH = Path(os.environ.get(
    "SEED_PATH",
    Path(__file__).parent / "seed" / "world.json",
))
_RECENT_TURNS_WINDOW = 10   # always sent verbatim as chat history
_MEMORY_RETRIEVE_K   = 5    # extra turns retrieved by semantic similarity
_MEMORY_MIN_HISTORY  = 20   # don't bother retrieving until we have this many turns

# Set at _init_chroma(); None when chromadb unavailable.
_chroma_client    = None
_chroma_turns     = None   # Collection: narrator_turns
_chroma_knowledge = None   # Collection: world_knowledge (Sorting Hat — system prompt + seed chunks)

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

# ── Dynamic NPC voice assignment ──────────────────────────────────────────────
# Pool of unused Kokoro voices — assigned round-robin to new NPCs at introduction.
# Seeded NPCs (from world.json) already carry a "voice" field and are never
# touched by this pool.  Order is chosen for maximum timbral variety.
_VOICE_POOL: list[str] = [
    "am_fenrir",   # deep, slightly ominous male
    "af_nova",     # clear, confident female
    "bm_lewis",    # warm British male
    "af_river",    # calm, thoughtful female
    "am_orion",    # bright, expressive male
    "am_puck",     # quick, mercurial male
    "bf_emma",     # crisp British female
    "bm_fable",    # storyteller British male
    "af_sky",      # light, airy female
    "am_liam",     # friendly, casual male
    "bf_isabella", # warm British female
    "af_heart",    # emotional, rich female
]
_voices_assigned: set[str] = set()


def _assign_voice(entity: dict) -> str:
    """Assign the next unused Kokoro voice to a new NPC entity.

    No-ops if the entity already has a voice (seeded NPCs keep theirs).
    Returns the assigned voice ID.
    """
    if entity.get("voice"):
        return entity["voice"]
    global _voices_assigned
    for v in _VOICE_POOL:
        if v not in _voices_assigned:
            _voices_assigned.add(v)
            entity["voice"] = v
            return v
    # Pool exhausted — restart rotation
    _voices_assigned.clear()
    v = _VOICE_POOL[0]
    _voices_assigned.add(v)
    entity["voice"] = v
    return v


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
    global _player_dressed, _player_appearance_desc  # noqa: PLW0603
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

    # ── Location change broadcast + ghost scout hook heat ─────────────
    # Broadcast a "location" SSE event when the player moves so the client can
    # gate lore delivery and track _locationChanged.
    prev_location = _world.get("current_location", "")
    if location_name and location_name != prev_location:
        _world["current_location"] = location_name
        _sse_broadcast("location", {"name": location_name})
        # NPCs don't follow the player unless re-introduced in the new area
        _present_npcs.clear()

    # Ghost scout hook accumulator: count how often each known location is
    # mentioned in narrator turns while the player is NOT there.  Only counts
    # when we know the current location (avoids false positives on turn 1).
    if location_name and loc_id:
        for _ent_id, _ent in list(_world["entities"].items()):
            if _ent.get("type") != "location" or _ent_id == loc_id:
                continue
            _ent_name = _ent.get("canonical_name", "")
            if _ent_name and _ent_name.lower() in raw_text.lower():
                _hook_heat[_ent_name] = _hook_heat.get(_ent_name, 0) + 1
                _log(f"[ghost-scout] hook heat '{_ent_name}' → {_hook_heat[_ent_name]}")

    # Ghost scout cache consumption: when the player arrives at a scouted location,
    # serve prefetched images and sense beats immediately.  Cache entry is consumed
    # (popped) so it doesn't persist into subsequent visits.
    if location_name and location_name in _location_prefetch_cache:
        _cached = _location_prefetch_cache.pop(location_name)
        _log(f"[ghost-scout] cache hit for '{location_name}' — serving prefetched content")
        for _img in _cached.get("images", []):
            _sse_broadcast("scene_image", {
                "image": _img,
                "kind": "location",
                "description": f"scout: {location_name}",
            })
        for _beat in _cached.get("sense_beats", []):
            _sse_broadcast("lore_whisper", {
                "text": _beat["text"],
                "key": f"scout_{loc_id}_{_beat['type'].lower()}",
            })
        _hook_heat.pop(location_name, None)  # reset heat — player arrived

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
            # Give brand-new NPCs a voice from the dynamic pool
            _assign_voice(npc)
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
        # Track this NPC as present in the current area — used by banter pre-gen
        npc_ent = _world["entities"].get(npc_id, {})
        npc_voice = npc_ent.get("voice", "am_michael")
        _present_npcs[npc_name] = {"voice": npc_voice, "entity_id": npc_id}
        # Broadcast a greeting so the frontend can voice the NPC's first words
        sig_quote = npc_ent.get("signature_quote", "")
        if sig_quote:
            _sse_broadcast("npc_greeting", {
                "name": npc_name,
                "text": sig_quote,
                "voice": npc_voice,
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

    # ── Player appearance trait → avatar generation ────────────────────
    # [PLAYER_TRAIT(appearance): "..."] or [PLAYER_TRAIT(clothing): "..."]
    # triggers the SD portrait generation the same way the dressed transition does.
    if not _player_dressed:
        appear_re = re.compile(
            r'\[PLAYER_TRAIT\((?:appearance|clothing|look|physique|dress)\)'
            r'\s*:\s*"?([^"\]]{10,})"?\]',
            re.IGNORECASE,
        )
        for m in appear_re.finditer(raw_text):
            appear_desc = m.group(1).strip()
            if appear_desc:
                # Trigger the same avatar pipeline as the clothing ITEM path
                _check_dressed_transition(
                    f'[ITEM(outfit): "outfit"] {appear_desc} dressed now wearing'
                )
                break  # one trigger per turn

    # [UPDATE_PLAYER: "SD portrait brief"] — regenerate player avatar.
    # The narrator emits this tag on first appearance reveal AND on every subsequent
    # update (new hair colour, outfit change, injury, etc.).  The tag content is
    # already an SD-style portrait brief, so we use it directly without an Ollama
    # conversion step — faster and more accurate than re-summarising prose.
    # This is the primary re-generation path; it fires even when _player_dressed=True.
    _up_re = re.compile(r'\[UPDATE_PLAYER\s*:\s*"([^"]{10,})"\]', re.IGNORECASE)
    for _m in _up_re.finditer(raw_text):
        _brief = _m.group(1).strip()
        if _brief:
            _player_appearance_desc = _brief
            if not _player_dressed:
                _player_dressed = True
                _sse_broadcast("meta", {"type": "player_dressed"})
            print(f"[diag] UPDATE_PLAYER → queuing avatar re-generation")
            threading.Thread(
                target=_generate_player_avatar,
                args=(_brief,),
                daemon=True,
                name="player-avatar-update",
            ).start()
            break  # one portrait regeneration per turn

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
_LARGE_CONTEXT_PREFER = ("qwen2.5", "qwen", "mistral", "llama3.1", "llama3.2",
                          "mistral-nemo", "mixtral", "command-r", "gemma2", "phi3")

# ---------------------------------------------------------------------------
# Tag injection — session state
# The narrator (llama3.1:8b) omits MOOD, INTRODUCE, and LORE tags ~60% of
# turns.  These sets track what has already been injected so we never emit
# duplicate tags within a single game session.
# ---------------------------------------------------------------------------
_introduced_this_session: set = set()
_lore_injected_this_session: set = set()
_image_locations_fired: set = set()  # location keys for already-injected images this session
_item_given_this_session: set = set()  # item keys already injected this session

# Pre-Quirkify — low-priority background entity enrichment
# Triggered by INTRODUCE tags + location entry; processed by _quirkify_loop daemon.
_quirkify_queue: collections.deque = collections.deque(maxlen=40)
_quirkified_this_session: set = set()  # entity IDs enriched this session (skip re-processing)

# NPC banter — "elevator scenes" pre-generation.
# Tracks which NPCs are in the current area; used by _banter_prefetch_loop to pick
# conversation partners.  Cleared on location change, populated by INTRODUCE tags.
_present_npcs: dict = {}              # npc_name → {voice, entity_id}
_banter_queue: collections.deque = collections.deque(maxlen=5)  # ready-to-play banter items
_npc_conversations: dict = {}         # frozenset({a,b}) → [{speaker,text,ts}]
_banter_generating: bool = False      # True while a banter generation thread is running

# Characters that are permanent crew — never get a spurious INTRODUCE tag.
# Intentionally EMPTY: Sherri, The Remnant, and The Fortress are introduced
# as formal characters on first CHARACTER-tag encounter each session.
_PERMANENT_CREW: frozenset = frozenset()

# Prose → MOOD mapping (checked top-to-bottom; first match wins).
_MOOD_PATTERNS = [
    (r'danger|threat|weapon|combat|fight|alarm|hostile',
     'tense percussion, fast metallic rhythm, high threat'),
    (r'quiet|still|silence|empty|alone|waiting',
     'sparse ambient drone, low pulse, introspective'),
    (r'wonder|strange|alien|void|universe|ancient',
     'alien ambient, choral whisper, vast and slow'),
    (r'warm|laugh|food|comfort|safe|cheerful|smile',
     'gentle metallic chime, slow tempo, warmth'),
    (r'run|flee|chase|escape|urgent|quickly',
     'driving percussion, rising tension, urgent'),
    (r'sleep|dream|rest|drift|float|quiet|still|hush',
     'deep space ambient, intimate and quiet, slow drift'),
    (r'discover|reveal|secret|hidden|ancient|archive|lore',
     'mysterious resonance, slow revelation, ancient memory'),
]

# Lore keyword → (lore_key, lore_text).  First mention of the keyword that
# does NOT already carry a [LORE(...)] tag gets one injected automatically.
_LORE_ANCHORS = [
    ('the fold',           'fold_omni',
     'The Fold bridges universes — it is everywhere at once'),
    ('neural nexus',       'neural_nexus',
     'The Neural Nexus is the cognitive heart of The Fortress'),
    ('void crystal',       'void_crystal',
     'Void crystals store memory across dimensional boundaries'),
    ('remnant',            'remnant_origin',
     'The Remnant is a consciousness without a fixed form'),
    ('the fortress',       'fortress_origin',
     'The Fortress is an ancient self-aware station adrift between dimensions'),
    ('fabricat',           'fabrication_bay',
     'The Fabrication Bay synthesizes objects from raw matter using Fold energy'),
    ('null space',         'null_space',
     'Null space exerts anti-pressure — the Fortress holds it at bay alone'),
    ('sherri',             'sherri_origin',
     'Sherri is a fleet of bronze automatons sharing a single distributed mind'),
    ('terravore',          'terravore',
     'Terravore is a universe of bio-mechanical jungles visible through the fore port'),
    ('the hoop',           'the_hoop',
     'The abduction hoop tears a luminous vortex between dimensions to collect souls'),
    ('tricorder',          'the_tricorder',
     'The Dimensional Tricorder reads fold frequencies, entity signatures, and anomalies'),
    ('vex',                'vex_history',
     'Vex is a fallen traveler who refused their assignment — now haunts the lower decks'),
    ('mira',               'mira_history',
     'Mira is a fold researcher who arrived two cycles ago and chose to understand'),
    ('founding compact',   'founding_compact',
     'The Fortress and The Remnant made an agreement older than any known civilization'),
    ('great silence',      'the_great_silence',
     'Three hundred cycles passed with no travelers — only the ship, Sherri, and The Remnant'),
    ('lower deck',         'vex_history',
     'The lower decks are where Vex retreated — unmaintained, cold, and forgotten'),
    ('archive',            'the_first_traveler',
     'The Archives hold every traveler\'s record — including Subject Zero, the first'),
    ('equilibrium barrier','null_space',
     'The equilibrium barrier is the only thing between the crew and null space\'s anti-pressure'),
    ('string.slip',        'string_slipping',
     'String-Slipping lets the Vex-Kahl phase through matter without opening a door'),
]

# ── Sorting Hat constants ──────────────────────────────────────────────────
# The Sorting Hat chunks and indexes all world knowledge (system prompt rules,
# NPC personalities, location sense data, lore) at startup, then retrieves
# exactly the relevant chunks per narrator turn — tagged by sense type and
# dispatched to the right service.
_CHUNK_SIZE           = 120   # words per chunk (~120 tokens at typical density)
_CHUNK_OVERLAP        = 24    # ~20% overlap for context continuity
_KNOWLEDGE_RETRIEVE_K = 6     # chunks retrieved per narrator turn


def _chunk_text(text: str, size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> list:
    """Split text into overlapping word-boundary chunks."""
    words = text.split()
    chunks, i = [], 0
    while i < len(words):
        chunk = " ".join(words[i:i + size])
        if chunk.strip():
            chunks.append(chunk)
        i += size - overlap
    return chunks


def _index_world_knowledge() -> None:
    """Chunk and embed system prompt + world seed into world_knowledge collection.

    Runs at startup in a background thread (after _init_chroma + _load_seed_world).
    Skips docs already indexed (stable IDs — no re-embedding on restart).
    Classifies each chunk by sense_type and service_target so the Sorting Hat
    knows which downstream service benefits from each rule.
    """
    if _chroma_knowledge is None:
        return

    _SENSE_KEYWORDS = {
        "smell":     ("smell", "odor", "scent", "aroma", "SMELL"),
        "taste":     ("taste", "flavor", "flavour", "TASTE"),
        "touch":     ("touch", "texture", "feel", "TOUCH"),
        "sound":     ("sound", "hear", "audio", "SFX", "SOUND"),
        "sight":     ("sight", "see", "image", "GENERATE_IMAGE", "visual"),
        "mood":      ("mood", "music", "MOOD", "atmosphere", "ambient"),
        "character": ("character", "dialogue", "CHARACTER", "voice", "NPC"),
        "lore":      ("lore", "LORE", "history", "world"),
        "rule":      ("never", "always", "must", "forbidden", "rule", "RULE"),
    }

    def _classify_chunk(text: str):
        """Return (sense_type, service_target) for a text chunk."""
        tl = text.lower()
        for sense, keywords in _SENSE_KEYWORDS.items():
            if any(k.lower() in tl for k in keywords):
                service = {
                    "mood":  "flask-music",
                    "sight": "flask-sd",
                    "sound": "flask-tts",
                }.get(sense, "ollama")
                return sense, service
        return "general", "ollama"

    docs, ids, metas = [], [], []

    # ── System prompt chunks ──────────────────────────────────────────────
    if _system_prompt:
        for i, chunk in enumerate(_chunk_text(_system_prompt)):
            sense, service = _classify_chunk(chunk)
            docs.append(chunk)
            ids.append(f"sysprompt_{i:04d}")
            metas.append({
                "source": "system_prompt",
                "chunk_index": i,
                "sense_type": sense,
                "service_target": service,
            })

    # ── World seed: lore ──────────────────────────────────────────────────
    for entry in _world.get("lore", []):
        text = entry.get("text", "").strip()
        key  = entry.get("key", f"lore_{len(ids)}")
        if not text:
            continue
        for i, chunk in enumerate(_chunk_text(text)):
            docs.append(chunk)
            ids.append(f"lore_{key}_{i:03d}")
            metas.append({
                "source": "lore", "key": key, "chunk_index": i,
                "sense_type": "lore", "service_target": "ollama",
            })

    # ── World seed: NPCs ──────────────────────────────────────────────────
    for npc in _world.get("npcs", []):
        parts = []
        for field in ("name", "role", "description", "personality",
                      "voice_style", "signature_quote", "nexus_dynamic"):
            val = npc.get(field, "")
            if val:
                parts.append(f"{field}: {val}")
        text = " | ".join(parts)
        npc_id = npc.get("id", f"npc_{len(ids)}")
        for i, chunk in enumerate(_chunk_text(text)):
            docs.append(chunk)
            ids.append(f"npc_{npc_id}_{i:03d}")
            metas.append({
                "source": "npc", "npc_id": npc_id, "chunk_index": i,
                "sense_type": "character", "service_target": "ollama",
            })

    # ── World seed: locations — each sense field tagged separately ────────
    for loc in _world.get("locations", []):
        loc_id = loc.get("id", f"loc_{len(ids)}")
        sense_field_map = {
            "description": ("general", "ollama"),
            "sight":       ("sight",   "flask-sd"),
            "smell":       ("smell",   "ollama"),
            "sound":       ("sound",   "flask-tts"),
            "touch":       ("touch",   "ollama"),
            "sd_prompt":   ("sight",   "flask-sd"),
            "music_mood":  ("mood",    "flask-music"),
        }
        for field, (sense, service) in sense_field_map.items():
            val = loc.get(field, "").strip()
            if not val:
                continue
            for i, chunk in enumerate(_chunk_text(val)):
                docs.append(chunk)
                ids.append(f"loc_{loc_id}_{field}_{i:03d}")
                metas.append({
                    "source": "location", "loc_id": loc_id,
                    "field": field, "chunk_index": i,
                    "sense_type": sense, "service_target": service,
                })

    if not docs:
        print("[sorting-hat] no documents to index")
        return

    # Only add docs not already indexed (stable IDs → no re-embed on restart)
    try:
        existing = set(_chroma_knowledge.get(include=[])["ids"])
        new_docs  = [(d, i, m) for d, i, m in zip(docs, ids, metas) if i not in existing]
        if not new_docs:
            print(f"[sorting-hat] world_knowledge already indexed — {len(existing)} chunks")
            return

        nd, ni, nm = list(zip(*new_docs))

        # Narrator-yielding batched embed — same idle guard as _chroma_add_turn_async.
        # Without this guard, embedding 90+ chunks competes with active chat generation,
        # causing Ollama model-switching (llama3.1:8b ↔ nomic-embed-text) that stalls
        # both the narrator and the story-test's Ollama readiness check.
        _BATCH_SIZE     = 10    # chunks per add() call (~5s of embed work)
        _INDEX_IDLE_SECS = 8.0  # seconds of continuous idle required before each batch

        total_added = 0
        for batch_start in range(0, len(nd), _BATCH_SIZE):
            # Wait for narrator (and any other Ollama call) to finish + idle window
            idle_since = None
            while True:
                if _generating or _classifying:
                    idle_since = None
                    time.sleep(1)
                    continue
                if idle_since is None:
                    idle_since = time.time()
                    time.sleep(1)
                    continue
                if time.time() - idle_since < _INDEX_IDLE_SECS:
                    time.sleep(1)
                    continue
                break   # idle for _INDEX_IDLE_SECS — embed this batch

            batch_d = list(nd[batch_start:batch_start + _BATCH_SIZE])
            batch_i = list(ni[batch_start:batch_start + _BATCH_SIZE])
            batch_m = list(nm[batch_start:batch_start + _BATCH_SIZE])
            _chroma_knowledge.add(documents=batch_d, ids=batch_i, metadatas=batch_m)
            total_added += len(batch_d)

        print(f"[sorting-hat] indexed {total_added} world knowledge chunks "
              f"(+{total_added} new of {len(docs)} total)")
    except Exception as exc:
        print(f"[sorting-hat] index error: {exc}")


def _check_service_available(service: str) -> bool:
    """Quick health check for a downstream service — non-blocking, 1s timeout.

    Uses the same URL constants as the rest of app.py so Docker DNS names
    resolve correctly (flask-sd:1592, etc.) rather than localhost.
    """
    port_map = {
        "flask-sd":    f"{FLASK_SD_URL}/health",
        "flask-music": f"{FLASK_MUSIC_URL}/health",
        "flask-tts":   f"{os.environ.get('FLASK_TTS_URL', 'http://localhost:1594')}/health",
    }
    url = port_map.get(service)
    if not url:
        return True   # unknown service — optimistic pass
    try:
        urllib.request.urlopen(url, timeout=1)
        return True
    except Exception:
        return False


def _sort_and_retrieve(query_text: str) -> dict:
    """Sorting Hat: retrieve world knowledge chunks, group by service_target.

    Returns dict keyed by service_target (ollama / flask-sd / flask-music /
    flask-tts). Each value is a list of text chunks most relevant to the query.
    Chunks whose target service is unavailable are rerouted to 'ollama' so
    context is never lost.
    """
    result = {"ollama": [], "flask-sd": [], "flask-music": [], "flask-tts": []}

    if _chroma_knowledge is None or _chroma_knowledge.count() == 0:
        return result

    try:
        n = min(_KNOWLEDGE_RETRIEVE_K, _chroma_knowledge.count())
        qr = _chroma_knowledge.query(
            query_texts=[query_text[:800]],
            n_results=n,
            include=["documents", "metadatas"],
        )
        docs  = (qr.get("documents") or [[]])[0]
        metas = (qr.get("metadatas") or [[]])[0]

        # Service availability cache for this call (avoid repeated HTTP per chunk)
        available: dict = {}

        for doc, meta in zip(docs, metas):
            target = meta.get("service_target", "ollama")
            if target not in available:
                available[target] = _check_service_available(target) if target != "ollama" else True
            effective = target if available.get(target, True) else "ollama"
            result.setdefault(effective, []).append(doc.strip())

    except Exception as exc:
        print(f"[sorting-hat] retrieval error: {exc}")

    return result


def _retrieve_world_context(query_text: str) -> str:
    """Retrieve the top _KNOWLEDGE_RETRIEVE_K world knowledge chunks relevant
    to query_text.  Returns a compact multi-line string for injection into the
    system message, or empty string if ChromaDB unavailable or no results.
    Only returns ollama-targeted chunks (narrator context).
    """
    if _chroma_knowledge is None or _chroma_knowledge.count() == 0:
        return ""
    try:
        sorted_ctx = _sort_and_retrieve(query_text)
        ollama_chunks = sorted_ctx.get("ollama", [])
        if not ollama_chunks:
            return ""
        lines = []
        for chunk in ollama_chunks:
            lines.append(f"  {chunk}")
        return "\n".join(lines)
    except Exception as exc:
        print(f"[sorting-hat] world_context query error: {exc}")
        return ""


# Scene keyword → (location_key, SD prompt) for GENERATE_IMAGE injection.
# Checked in order; first match per turn wins.  location_key prevents duplicate
# images for the same place across the session.
_IMAGE_TRIGGERS = [
    (r'\b(portal|hoop|vortex|arrival|crackling|gateway|rift|shimmer)\b',
     'portal_chamber',
     'glowing dimensional portal, crackling blue energy, alien circular arrival chamber, '
     'observation catwalks, bronze automatons attending'),
    (r'\b(fabricat\w+\s*bay|forge bay|workshop|fabricat\w+\s*room|machining)\b',
     'fabrication_bay',
     'large sci-fi workshop, glowing blue CNC workstation, ceiling robotic arms, '
     'welding sparks, blueprint screens, industrial metallic space'),
    (r'\b(galley|kitchen|mess hall|dining|canteen|cooking|simmering|meal\b)\b',
     'the_galley',
     'compact sci-fi kitchen, slate countertops, herb bundles hanging, '
     'induction burner with pot, bronze robot cooking, warm amber lighting'),
    (r'\b(sleep\w*\s*quarter|bunk|dormitory|berth|sleeping pod|cot\b|going to sleep|lie down)\b',
     'sleeping_quarters',
     'hexagonal pod bunks in curved walls, amber light strips, grey blankets, '
     'circular viewport showing black hole accretion disc in null space'),
    (r'\b(nexus|neural\s*nexus|crystalline\s*form|remnant.*float|pulpit|gantry)\b',
     'the_nexus',
     'domed sci-fi chamber, central raised pulpit, spiky blue glowing crystal entity floating, '
     'multi-level circular gantry, blue crystalline lighting, cathedral scale'),
    (r'\b(corridor|hallway|passage|walkway|catwalk|airlock|hatch|bulkhead)\b',
     'corridor',
     'ancient space station corridor, metallic walls, bioluminescent strips, '
     'vast and empty, industrial sci-fi aesthetic'),
    (r'\b(observation|viewport|star field|stars|deep space|cosmos|nebula|black hole|accretion)\b',
     'observation_deck',
     'observation deck overlooking deep space, star field with nebula colours, '
     'vast black hole accretion disc visible through hull viewport, cosmic silence'),
    (r'\b(gantry|maintenance|cog|gear|pipe|catwalk.*engine|engine.*catwalk)\b',
     'the_gantry',
     'industrial maintenance catwalk, slow-turning bronze gears below, '
     'copper pipes, amber warning lights, bronze robots doing maintenance'),
    (r'\b(foundry|crucible|molten|casting|mould|smelting|pour)\b',
     'the_foundry',
     'sci-fi foundry, glowing orange molten crucible, rotating casting carousel, '
     'scorched black walls, heat shimmer, dramatic orange-red lighting'),
    (r'\b(terminal|console|screen|interface|panel|holographic|display)\b',
     'terminal',
     'ancient space station control console, holographic displays, '
     'aged metallic panels with worn controls, dim blue interface glow'),
    (r'\b(archive|data\s*crystal|records?|memory\s*store|recording)\b',
     'the_archives',
     'long curved archive room, floor-to-ceiling shelving of glowing data crystals, '
     'cool blue reading light, central holographic projection station, quiet and dim'),
    (r'\b(research\s*wing|mira\'?s?\s*(lab|room|space)|frequency\s*scanner|fold\s*map)\b',
     'research_wing',
     'converted sci-fi cargo hold research lab, fold-frequency scanner on wall, '
     'observation journals pinned everywhere, warm blue-amber lighting'),
    (r'\b(lower\s*deck|below\s*decks?|sub.level|vex\'?s?\s*(room|space|haunt))\b',
     'lower_decks',
     'dark sci-fi lower engineering deck, low ceilings, corroded pipe runs, '
     'amber lights on dim cycle, rough bedroll in corner, oppressive and dim'),
    (r'\b(equilibrium\s*chamber|anti.matter|barrier\s*generator|plasma\s*column)\b',
     'equilibrium_chamber',
     'spherical sci-fi chamber, rotating anti-matter plasma column in magnetic lattice, '
     'blue-white energy, heat-shielded walls, amber readout panels, dangerous'),
]

# Prose → SMELL sense injection.  Only fires once per turn if no [SMELL( present.
_SMELL_TRIGGERS = [
    (r'\b(food|meal|eat|cooking|broth|soup|bread|stew|feast|simmer|kitchen|galley)\b',
     'warm cooked food, steam and metallic undertones'),
    (r'\b(machine|machinery|engine|hydraulic|mechanical|grease|oil|lubric)\b',
     'machine oil and hot metal, industrial tang'),
    (r'\b(ozone|electric|spark|plasma|ionized|lightning|energy|weld)\b',
     'sharp ozone, ionized air from active conduits'),
    (r'\b(sterile|clean|filtered|recycled air|antiseptic|med bay|infirmary)\b',
     'sterile recycled air, faint mineral trace'),
    (r'\b(rust|ancient|old|musty|stale|decay|oxidiz|centuries)\b',
     'ancient oxidized metal, dust of centuries'),
    (r'\b(forge|foundry|crucible|molten|smelting|casting)\b',
     'searing metal and ash, intense thermal bloom'),
    (r'\b(portal|fold|rift|vortex|dimensional|transit)\b',
     'sharp ozone and something sweet — a storm through dimensions'),
    (r'\b(sleep|bunk|quarters|pillow|blanket|linen|rest)\b',
     'recycled air, faint fabric, the enclosed comfort of a familiar space'),
]

# Prose → TASTE sense injection.  Only fires once per turn if no [TASTE( present.
_TASTE_TRIGGERS = [
    (r'\b(eat|ate|eating|bite|chew|swallow|sip|drink|drank|meal|food|flavou?r|taste)\b',
     'complex synthesized nutrients, metallic finish, subtle warmth'),
    (r'\b(broth|soup|stew|bread|dish|morsel|portion|serving)\b',
     'rich savoury broth, layered flavours, satisfying depth'),
]

# Prose → TOUCH sense injection.  Only fires once per turn if no [TOUCH( present.
_TOUCH_TRIGGERS = [
    (r'\b(touch|feel|press|grip|grasp|hold|run\w*\s*hand|fingers?|texture|surfaces?)\b',
     'cool metal, slight vibration from station systems beneath the hull'),
    (r'\b(clothes|suit|fabric|worn|wearing|outfit|garment|material|fitted)\b',
     'fabric against skin, precisely shaped, still warm from fabrication'),
    (r'\b(sleep|lie|climb.*bunk|bunk|rest|settle|cushion|blanket)\b',
     'firm surface, slight warmth, station hum conducted through the hull'),
    (r'\b(wall|floor|railing|grating|panel|surface|hull)\b',
     'cold metal, faint vibration from deep machinery'),
    (r'\b(portal|fold|rift|energy|hoop|vortex)\b',
     'electric prickling across every surface of skin, hair rising'),
]

# NPC dialogue detection → inject CHARACTER tag when prose shows NPC speech
# without a formal [CHARACTER(...): tag.  (name, compiled_re, brief_desc)
_NPC_PROSE_PATTERNS = [
    ('Sherri', re.compile(
        r'''(?:
            \bsherri\b\s*(?:says?|said|replies?|replied|speaks?|spoke|
                           whispers?|whispered|adds?|added|nods?|nodded|
                           grins?|grinned|smiles?|smiled|chuckles?|chuckled|
                           sighs?|sighed|leans?|leaned)\b
            |
            (?:^|\n)\s*\*?\s*\bsherri\b\s*\*?\s*:
            |
            "[^"]{5,}"\s*,?\s*(?:says?|said|replied)\s+\bsherri\b
            |
            \bsherri\b\s*(?:chirps?|offers?|cheerfully|brightly|mutters?|murmured)
        )''',
        re.IGNORECASE | re.VERBOSE | re.MULTILINE,
    ), 'Sherri is a bronze automaton fabricator aboard The Fortress'),
    ('The Remnant', re.compile(
        r'''(?:
            (?:the\s+)?remnant\s*(?:says?|said|replies?|replied|speaks?|spoke|
                                    whispers?|whispered|intones?|intoned|
                                    states?|stated|declares?|declared)\b
            |
            (?:^|\n)\s*\*?\s*(?:the\s+)?remnant\s*\*?\s*:
            |
            "[^"]{5,}"\s*,?\s*(?:says?|said|replied)\s+(?:the\s+)?remnant\b
        )''',
        re.IGNORECASE | re.VERBOSE | re.MULTILINE,
    ), 'The Remnant is an ancient crystalline consciousness without a fixed form'),
    ('The Fortress', re.compile(
        r'''(?:
            (?:the\s+)?fortress\s*(?:says?|said|replies?|replied|speaks?|spoke|
                                     whispers?|whispered|intones?|intoned|
                                     murmurs?|murmured|breathes?|breathed)\b
            |
            (?:^|\n)\s*\*?\s*(?:the\s+)?fortress\s*\*?\s*:
        )''',
        re.IGNORECASE | re.VERBOSE | re.MULTILINE,
    ), 'The Fortress is the living intelligence of the station itself'),
    ('Vex', re.compile(
        r'''(?:
            \bvex\b\s*(?:says?|said|replies?|replied|speaks?|spoke|
                        snarls?|snarled|mutters?|muttered|growls?|growled|
                        hisses?|hissed|spits?|spat|warns?|warned)\b
            |
            (?:^|\n)\s*\*?\s*\bvex\b\s*\*?\s*:
            |
            "[^"]{5,}"\s*,?\s*(?:says?|said|snarled|muttered)\s+\bvex\b
        )''',
        re.IGNORECASE | re.VERBOSE | re.MULTILINE,
    ), 'Vex is a fallen traveler who refused their assignment and haunts the lower decks'),
    ('Mira', re.compile(
        r'''(?:
            \bmira\b\s*(?:says?|said|replies?|replied|speaks?|spoke|
                         explains?|explained|notes?|noted|asks?|asked|
                         observes?|observed|suggests?|suggested)\b
            |
            (?:^|\n)\s*\*?\s*\bmira\b\s*\*?\s*:
            |
            "[^"]{5,}"\s*,?\s*(?:says?|said|replied|explained)\s+\bmira\b
        )''',
        re.IGNORECASE | re.VERBOSE | re.MULTILINE,
    ), 'Mira is a fold researcher who arrived two cycles ago and chose to understand'),
]

# Item trigger → (item_key, item_desc).  Fires once per item per session when
# the narrator describes giving / fabricating / finding that item.
_ITEM_TRIGGERS = [
    (r'\b(fabricat\w*|finish\w*|complet\w*|ready|here\s+you\s+go|done\b|present\w*)'
     r'.*\b(suit|clothes|clothing|outfit|garment|attire)\b'
     r'|\b(suit|clothes|clothing|outfit|garment|attire)\b'
     r'.*\b(fabricat\w*|finish\w*|complet\w*|ready|done\b)',
     'dark_practical_suit',
     'Dark practical suit with many pockets, fabricated by Sherri'),
    (r'\b(serve\w*|hands?\s+you|presents?\s+you|brings?\s+you|places?\s+before)'
     r'.*\b(bowl|dish|meal|food|broth|plate|cup|mug)\b'
     r'|\b(bowl|dish|meal|food|broth|plate|cup|mug)\b'
     r'.*\b(serve\w*|hand\w*|present\w*|bring\w*)',
     'galley_meal',
     'A hot meal from The Galley, prepared by Sherri'),
    (r'\b(tricorder|scanner|scanning\s+device|fold\s+reader|dimensional\s+scanner)\b',
     'tricorder',
     'A compact dimensional scanner — reads fold frequencies, entity signatures, and environmental anomalies'),
    (r'\b(find\w*|discov\w*|pick\s+up|retrieve\w*|recover\w*|spot\w*|grab\w*|take\b)'
     r'.*\b(crystal|shard|fragment|device|component|artifact|tool|scanner|tricorder)\b'
     r'|\b(crystal|shard|fragment|device|component|artifact|tool|scanner|tricorder)\b'
     r'.*\b(find\w*|discov\w*|pick\s+up|retrieve\w*|grab\w*)',
     'found_artifact',
     'A strange artifact recovered in the Fortress'),
]

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
_KNOWN_SERVICES = ("flask-sd", "ollama", "diag", "bootstrap")


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
            global _player_dressed, _player_appearance_desc, _image_locations_fired, _item_given_this_session
            _player_dressed = False
            _player_appearance_desc = ""
            # Reset tag-injection session trackers — new run, fresh introductions
            _introduced_this_session.clear()
            _lore_injected_this_session.clear()
            _image_locations_fired.clear()
            _item_given_this_session.clear()
            # Reset pre-quirkify state — entities will be re-introduced and re-enriched
            _quirkify_queue.clear()
            _quirkified_this_session.clear()
            # Clear ChromaDB semantic index — stale memories from prior sessions
            # would pollute the narrator context for new players/runs, and would
            # also trigger unnecessary embed calls in _chroma_query_relevant.
            if _chroma_turns is not None:
                try:
                    _existing_ids = _chroma_turns.get(include=[])["ids"]
                    if _existing_ids:
                        _chroma_turns.delete(ids=_existing_ids)
                    print(f"[diag/chroma] world reset — cleared {len(_existing_ids)} memories")
                except Exception as _ce:
                    print(f"[diag/chroma] world reset clear failed: {_ce}")
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
        # Re-seed permanent world assets after clearing runtime state.
        # This restores locations, NPCs, and lore that must always exist.
        _load_seed_world()
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


def _load_seed_world() -> None:
    """Load permanent world assets from SEED_PATH into the entity graph.

    Called at startup (after _replay_world_log) and after every Reset World.
    Seed entities use permanence='WORLD' and are tagged source='seed' so
    they can be identified and are reloaded rather than lost on world reset.

    Does NOT write to world-state.jsonl — seed data is always reloaded from
    the file, so no persistence needed. Does broadcast portrait SSE events so
    the game UI immediately knows seeded NPC portrait URLs.
    """
    global _system_prompt

    if not SEED_PATH.exists():
        print(f"[diag/seed] no seed file at {SEED_PATH} — skipping")
        return

    try:
        seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[diag/seed] failed to parse seed: {exc}")
        return

    seed_turn_id = "seed"

    # ── Locations ──────────────────────────────────────────────────────────
    for loc in seed.get("locations", []):
        loc_id = loc.get("id", "")
        name   = loc.get("name", "")
        if not loc_id or not name:
            continue
        ent = _ensure_entity(loc_id, name, "location")
        ent["permanence"] = "WORLD"
        ent["source"]     = "seed"
        ent["description"] = loc.get("description", "")
        for sense_key in ("sight", "smell", "sound", "touch", "taste"):
            text = loc.get(sense_key, "").strip()
            if text:
                _add_sense_layer(ent, sense_key.upper(), text, seed_turn_id)
        if loc.get("music_mood"):
            ent["music_mood"] = loc["music_mood"]
        if loc.get("portrait"):
            ent["portrait"] = loc["portrait"]

    # ── NPCs ───────────────────────────────────────────────────────────────
    npc_lines = []  # for system prompt injection
    for npc in seed.get("npcs", []):
        npc_id = npc.get("id", "")
        name   = npc.get("name", "")
        if not npc_id or not name:
            continue
        ent = _ensure_entity(npc_id, name, "npc")
        ent["permanence"]        = "WORLD"
        ent["source"]            = "seed"
        ent["description"]       = npc.get("description", "")
        ent["personality"]       = npc.get("personality", "")
        ent["portrait"]          = npc.get("portrait", "")
        ent["voice"]             = npc.get("voice", "")
        ent["role"]              = npc.get("role", "")
        ent["voice_style"]       = npc.get("voice_style", "")
        ent["signature_quote"]   = npc.get("signature_quote", "")
        ent["nexus_dynamic"]     = npc.get("nexus_dynamic", "")
        if npc.get("home_location"):
            ent["home_location"] = npc["home_location"]
        # Push portrait URL to connected UI clients so names map immediately
        if npc.get("portrait"):
            _sse_broadcast("portrait", {"name": name, "url": npc["portrait"]})
        # Collect for system prompt block
        desc_short = (npc.get("description") or "")[:120].split(".")[0]
        npc_lines.append(f"  - {name}: {desc_short}.")

    # ── Lore ───────────────────────────────────────────────────────────────
    for entry in seed.get("lore", []):
        key  = entry.get("key", "")
        text = entry.get("text", "")
        if not key or not text:
            continue
        # Write to forever.jsonl only if key not already present
        try:
            existing_keys: set = set()
            if FOREVER_LOG.exists():
                for line in FOREVER_LOG.read_text(encoding="utf-8").splitlines():
                    try:
                        rec = json.loads(line)
                        if rec.get("key"):
                            existing_keys.add(rec["key"])
                    except Exception:
                        pass
            if key not in existing_keys:
                with FOREVER_LOG.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "key": key, "text": text,
                        "permanence": "FOREVER", "source": "seed",
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }) + "\n")
        except Exception as exc:
            print(f"[diag/seed] lore write failed ({key}): {exc}")

    # ── Quests ─────────────────────────────────────────────────────────────
    # Build a system prompt block summarising active quests so the narrator
    # knows the story arc, phase structure, paths, and moral weight from
    # turn 1 — without needing the player to discover it first.
    quest_blocks = []
    for quest in seed.get("quests", []):
        qid    = quest.get("id", "")
        title  = quest.get("title", "")
        status = quest.get("status", "available")
        if not title or status == "disabled":
            continue

        # Only inject a brief awareness block — NOT the full phase/path/reward tree.
        # Detailed phase names and path choices cause models to narrate quest structure
        # verbatim instead of letting it emerge through play.
        threat = quest.get("threat_summary", "")
        tone   = quest.get("tone", "")
        faction = quest.get("faction", "")
        brief = (
            "\n[INTERNAL STORY NOTES — PRIVATE BACKGROUND ONLY. "
            "NEVER OUTPUT, LIST, SUMMARIZE, REFERENCE, OR NARRATE FROM THIS BLOCK. "
            "The player discovers the quest entirely through organic play.]\n"
            f"[PILOT QUEST — {title.upper()}]\n"
        )
        if threat:
            brief += f"Awareness: {threat}\n"
        if tone:
            brief += f"Tone: {tone}\n"
        if faction:
            brief += f"Faction present: {faction}\n"
        brief += f"[END PILOT QUEST — {title.upper()}]"
        quest_blocks.append(brief)

    # ── System prompt injection — seed NPC personalities ──────────────────
    # Append a rich character bible block so the narrator voices each being
    # correctly from the very first turn. Includes role, voice style,
    # personality, signature quote, and the Nexus dynamic.
    npc_profile_lines = []
    for npc in seed.get("npcs", []):
        name = npc.get("name", "")
        if not name:
            continue
        role        = npc.get("role", "")
        voice_style = npc.get("voice_style", "")
        personality = npc.get("personality", "")
        sig_quote   = npc.get("signature_quote", "")
        nexus       = npc.get("nexus_dynamic", "")
        block = f"\n--- {name}"
        if role:
            block += f"\n  Role: {role}"
        if voice_style:
            block += f"\n  Voice: {voice_style}"
        if personality:
            block += f"\n  Personality: {personality}"
        if sig_quote:
            block += f"\n  Example quote: \"{sig_quote}\""
        if nexus:
            block += f"\n  In the Nexus: {nexus}"
        npc_profile_lines.append(block)

    if npc_profile_lines and _system_prompt:
        nexus_dynamic = (
            "\n\nNexus Dynamic — when all three are present at the pulpit:\n"
            "  1. The Remnant belittles the player's request and questions their right to exist.\n"
            "  2. The Fortress softly interrupts to offer historical context and encouragement.\n"
            "  3. Sherri clanks in, trips over a floor panel, and asks if anyone wants tea."
        )
        roster_block = (
            "\n\n[PERMANENT CREW — These beings exist aboard the Fortress at all times. "
            "Write their dialogue to match their voice and personality exactly as described below. "
            "They do not need to be introduced unless the player asks.]\n"
            + "\n".join(npc_profile_lines)
            + nexus_dynamic
            + "\n[END PERMANENT CREW]"
        )
        # Only append once — check if block already present
        if "[PERMANENT CREW" not in _system_prompt:
            _system_prompt += roster_block

    # ── System prompt injection — quests ──────────────────────────────────
    # Append each active quest's full arc so the narrator can reference
    # quest phases, paths, NPCs, moral choices and rewards from turn 1.
    if quest_blocks and _system_prompt:
        import re as _re  # noqa: PLC0415
        for qblock in quest_blocks:
            # Use the [PILOT QUEST — ...] opening tag as the idempotency key
            tag_match = _re.search(r"\[PILOT QUEST[^\]]*\]", qblock)
            tag_key = tag_match.group(0) if tag_match else qblock[:60]
            if tag_key not in _system_prompt:
                _system_prompt += "\n\n" + qblock.strip()

    locs   = len(seed.get("locations", []))
    npcs   = len(seed.get("npcs", []))
    lores  = len(seed.get("lore", []))
    quests = len(seed.get("quests", []))
    print(f"[diag/seed] loaded — {locs} locations, {npcs} NPCs, {lores} lore entries, {quests} quests from {SEED_PATH}")


# ---------------------------------------------------------------------------
# Lore idle narration — The Fortress speaks lore during quiet moments
# ---------------------------------------------------------------------------

def _pick_unseen_lore() -> dict | None:
    """Return the first lore entry not recently spoken, or None if none available."""
    global _spoken_lore_keys
    candidates: list[dict] = []

    # Collect from FOREVER_LOG (seed + narrator-added lore)
    if FOREVER_LOG.exists():
        for line in FOREVER_LOG.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                if rec.get("key") and rec.get("text"):
                    candidates.append({"key": rec["key"], "text": rec["text"]})
            except Exception:
                pass

    unseen = [c for c in candidates if c["key"] not in _spoken_lore_keys]
    if not unseen:
        # All lore has been spoken — reset the set and start the cycle again
        _spoken_lore_keys.clear()
        unseen = candidates
    return unseen[0] if unseen else None


def _prettify_lore(raw_text: str) -> str:
    """Ask Ollama to render a lore entry as a warm story-telling aside."""
    prompt = (
        "You are The Fortress of Eternal Sentinel — a vast, ancient, kindly space station. "
        "Speak this lore entry aloud to your guest as a warm, story-telling aside: "
        "2-3 sentences, present-tense narration, no technical jargon, no bullet points. "
        "Open with a soft preamble like 'Did you know...' or 'I have always found it remarkable...' "
        "or similar. Write as if sharing a quiet history lesson with someone you are fond of.\n\n"
        "Lore:\n" + raw_text[:600]
    )
    payload = json.dumps({
        "model": _ollama_model(),
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": 120, "temperature": 0.7},
    }).encode()
    try:
        code, resp, _ = _http("POST", f"{OLLAMA_URL}/api/generate", body=payload, timeout=20.0)
        if code == 200:
            text = json.loads(resp).get("response", "").strip()
            return text if len(text) > 20 else ""
    except Exception as exc:
        print(f"[diag/lore] prettify failed: {exc}")
    return ""


def _lore_idle_loop() -> None:
    """Daemon thread: after LORE_IDLE_SECS of player silence, read a lore entry aloud."""
    global _last_player_ts, _spoken_lore_keys
    while True:
        time.sleep(10)
        try:
            if time.time() - _last_player_ts < LORE_IDLE_SECS:
                continue
            if _generating or _classifying:
                continue
            # Second idle-window check: ensure _generating/_classifying has been
            # False for at least 5s before calling Ollama. Top-of-loop check has
            # a race window between the check and the actual Ollama call.
            idle_since_lore = time.time()
            still_ok = True
            for _ in range(5):
                time.sleep(1)
                if _generating or _classifying or (time.time() - _last_player_ts < LORE_IDLE_SECS):
                    still_ok = False
                    break
            if not still_ok:
                continue
            entry = _pick_unseen_lore()
            if not entry:
                continue
            prettified = _prettify_lore(entry["text"])
            if not prettified:
                continue
            _sse_broadcast("lore_whisper", {"key": entry["key"], "text": prettified})
            _spoken_lore_keys.add(entry["key"])
            # Keep set bounded
            if len(_spoken_lore_keys) > 20:
                _spoken_lore_keys.pop()
            # Reset timer — don't fire again immediately
            _last_player_ts = time.time()
            print(f"[diag/lore] narrated: {entry['key']!r}")
        except Exception as exc:
            print(f"[diag/lore] idle loop error: {exc}")


def _get_entity_text(entity_id: str) -> str:
    """Retrieve and concatenate all world_knowledge chunks for an entity.

    Searches by doc ID prefix: npc_{entity_id}_* and loc_{entity_id}_*.
    Returns empty string if _chroma_knowledge is unavailable or entity not found.
    """
    if _chroma_knowledge is None:
        return ""
    try:
        # Retrieve all docs whose IDs start with the entity prefix
        prefixes = [f"npc_{entity_id}", f"loc_{entity_id}"]
        all_docs = []
        for prefix in prefixes:
            # ChromaDB doesn't support prefix queries natively — get all then filter
            result = _chroma_knowledge.get(include=["documents", "ids"])
            if result:
                for doc_id, doc in zip(result.get("ids", []), result.get("documents", [])):
                    if doc_id.startswith(prefix) and doc:
                        all_docs.append(doc.strip())
        return " ".join(all_docs)
    except Exception as exc:
        print(f"[quirkify] _get_entity_text error: {exc}")
        return ""


def _upsert_entity_enrichment(entity_id: str, enriched_text: str) -> None:
    """Replace all world_knowledge chunks for an entity with enriched text.

    Deletes old npc_/loc_ prefixed chunks, re-adds new chunks from enriched_text
    with source='quirkified'. Uses the same _chunk_text() as _index_world_knowledge.
    """
    if _chroma_knowledge is None:
        return
    try:
        # Find existing IDs for this entity
        result = _chroma_knowledge.get(include=["ids"])
        old_ids = [
            doc_id for doc_id in result.get("ids", [])
            if doc_id.startswith(f"npc_{entity_id}_")
            or doc_id.startswith(f"loc_{entity_id}_")
        ]
        if old_ids:
            _chroma_knowledge.delete(ids=old_ids)

        # Re-index enriched text
        chunks = _chunk_text(enriched_text)
        new_docs, new_ids, new_metas = [], [], []
        for i, chunk in enumerate(chunks):
            new_docs.append(chunk)
            new_ids.append(f"npc_{entity_id}_q{i:03d}")
            new_metas.append({
                "source": "quirkified",
                "entity_id": entity_id,
                "chunk_index": i,
                "sense_type": "character",
                "service_target": "ollama",
            })
        if new_docs:
            _chroma_knowledge.add(documents=new_docs, ids=new_ids, metadatas=new_metas)
            print(f"[quirkify] {entity_id!r}: {len(old_ids)} old → {len(new_docs)} new chunks")
    except Exception as exc:
        print(f"[quirkify] _upsert_entity_enrichment error: {exc}")


def _quirkify_loop() -> None:
    """Daemon thread: low-priority accumulate-and-summarize enrichment of entity descriptions.

    When an NPC is introduced or a location is entered, their entity_id is added
    to _quirkify_queue. This loop processes the queue when the narrator is idle,
    calling Ollama to add 1-2 sensory details and summarize to ≤150 words, then
    upserts the enriched text back into world_knowledge so future Sorting Hat
    retrievals return richer context.

    Narrator-yielding: waits 10s of continuous idle before each Ollama call.
    """
    while True:
        time.sleep(15)
        try:
            if not _quirkify_queue:
                continue
            entity_id = _quirkify_queue[0]
            if entity_id in _quirkified_this_session:
                _quirkify_queue.popleft()
                continue

            # Narrator-yielding: 10s of continuous idle before Ollama call
            idle_since: float | None = None
            while True:
                if _generating or _classifying:
                    idle_since = None
                    time.sleep(2)
                    continue
                if idle_since is None:
                    idle_since = time.time()
                    time.sleep(2)
                    continue
                if time.time() - idle_since < 10.0:
                    time.sleep(2)
                    continue
                break

            existing_text = _get_entity_text(entity_id)
            if not existing_text or len(existing_text) < 20:
                _quirkify_queue.popleft()
                continue

            prompt = (
                "You are a world-builder for a dark sci-fi interactive story set aboard an ancient "
                "self-aware space station called The Fortress. Given this entity description, add "
                "exactly 1-2 new non-conflicting sensory details (smell, sound, texture, or lore), "
                "then rewrite the whole thing as a single coherent paragraph of ≤150 words. "
                "Preserve all existing details. Output only the enriched paragraph — no labels, "
                "no preamble.\n\n"
                f"ENTITY: {entity_id}\nCURRENT DESCRIPTION:\n{existing_text[:800]}\n\nEnriched:"
            )
            payload = json.dumps({
                "model": _ollama_model(),
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": 220, "temperature": 0.72},
            }).encode("utf-8")
            code, resp, _ = _http(
                "POST", f"{OLLAMA_URL}/api/generate",
                body=payload, timeout=28.0,
            )
            if code == 200:
                try:
                    enriched = json.loads(resp).get("response", "").strip()
                except Exception:
                    enriched = ""
                if enriched and len(enriched) > 20:
                    _upsert_entity_enrichment(entity_id, enriched)
                    _quirkified_this_session.add(entity_id)
                    print(f"[quirkify] enriched {entity_id!r}: {len(existing_text)}→{len(enriched)} chars")
            else:
                print(f"[quirkify] Ollama error {code} for {entity_id!r} — will retry")
                time.sleep(30)
                continue

            _quirkify_queue.popleft()

        except Exception as exc:
            print(f"[quirkify] loop error: {exc}")
            time.sleep(30)


def _init_chroma() -> None:
    """Initialize ChromaDB persistent client with Ollama-backed embeddings.

    No-op if chromadb is not installed — every downstream caller checks
    _chroma_turns is not None before proceeding.
    """
    global _chroma_client, _chroma_turns
    try:
        import chromadb  # noqa: PLC0415
        from chromadb import EmbeddingFunction, Documents, Embeddings as ChromaEmbed  # noqa: PLC0415
    except ImportError:
        print("[diag/chroma] chromadb not installed — semantic memory disabled")
        return

    class _OllamaEmbedder(EmbeddingFunction):
        """Calls Ollama /api/embed; falls back silently on errors."""
        def __init__(self) -> None:
            pass
        def __call__(self, input: Documents) -> ChromaEmbed:
            results: ChromaEmbed = []
            for text in input:
                try:
                    payload = json.dumps({
                        "model": EMBED_MODEL,
                        "input": text[:2000],
                    }).encode()
                    req = urllib.request.Request(
                        f"{OLLAMA_URL}/api/embed",
                        data=payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=30) as r:
                        data = json.loads(r.read())
                    results.append(data["embeddings"][0])
                except Exception as exc:
                    print(f"[diag/chroma] embed error: {exc}")
                    # Return sentinel — will be skipped in add flow
                    results.append(None)  # type: ignore[arg-type]
            return results

    try:
        CHROMA_DB_PATH.mkdir(parents=True, exist_ok=True)
        _chroma_client = chromadb.PersistentClient(path=str(CHROMA_DB_PATH))
        _chroma_turns = _chroma_client.get_or_create_collection(
            name="narrator_turns",
            embedding_function=_OllamaEmbedder(),
            metadata={"hnsw:space": "cosine"},
        )
        print(f"[diag/chroma] ready — {_chroma_turns.count()} turns indexed at {CHROMA_DB_PATH}")
    except Exception as exc:
        print(f"[diag/chroma] init failed: {exc}")
        _chroma_client = None
        _chroma_turns = None
        return

    # ── Sorting Hat collection ────────────────────────────────────────────────
    # world_knowledge stores system-prompt chunks + seed lore/NPC/location data,
    # each tagged with sense_type and service_target for dispatch decisions.
    global _chroma_knowledge
    try:
        _chroma_knowledge = _chroma_client.get_or_create_collection(
            name="world_knowledge",
            embedding_function=_OllamaEmbedder(),
            metadata={"hnsw:space": "cosine"},
        )
        print(f"[sorting-hat] world_knowledge ready — {_chroma_knowledge.count()} chunks")
    except Exception as exc:
        print(f"[sorting-hat] world_knowledge init failed: {exc}")
        _chroma_knowledge = None


def _chroma_add_turn_async(turn: dict) -> None:
    """Index one narrator turn into ChromaDB in a background thread.

    Narrator-yielding with idle window: waits until _generating has been
    False for at least _EMBED_IDLE_SECS continuous seconds before calling
    Ollama. This prevents embed calls from racing with narrator calls that
    arrive shortly after generation finishes (e.g. rapid story-test beats).

    Ollama serialises all requests — an embed call already in-flight will
    stall the narrator for the full duration of the embed. The idle window
    ensures there is a quiet gap before we use the Ollama connection.
    """
    if _chroma_turns is None:
        return

    _EMBED_IDLE_SECS = 15.0   # seconds of continuous idle before embedding

    def _add() -> None:
        # Wait until narrator is done AND has been idle for _EMBED_IDLE_SECS.
        # Resets the idle counter each time _generating or _classifying becomes True.
        idle_since = None
        while True:
            if _generating or _classifying:
                idle_since = None          # narrator active — reset idle clock
                time.sleep(1)
                continue
            if idle_since is None:
                idle_since = time.time()   # just became idle
                time.sleep(1)
                continue
            if time.time() - idle_since < _EMBED_IDLE_SECS:
                time.sleep(1)              # idle but not long enough yet
                continue
            break                          # idle for _EMBED_IDLE_SECS — proceed
        try:
            turn_id = turn.get("turn_id", "")
            text = turn.get("raw_text", "").strip()
            if not turn_id or not text:
                return
            # Skip if already indexed (idempotent after restart backfill)
            if _chroma_turns.get(ids=[turn_id])["ids"]:
                return
            _chroma_turns.add(
                ids=[turn_id],
                documents=[text[:2000]],
                metadatas=[{
                    "is_player":    str(turn.get("is_player", False)),
                    "received_at":  turn.get("received_at", ""),
                    "narrator_name": turn.get("narrator_name", ""),
                }],
            )
        except Exception as exc:
            print(f"[diag/chroma] add_turn error: {exc}")

    threading.Thread(target=_add, daemon=True, name="chroma-add-turn").start()


def _chroma_query_relevant(query_text: str, exclude_ids: set) -> list[dict]:
    """Return up to _MEMORY_RETRIEVE_K turns semantically relevant to query_text.

    Skips IDs in exclude_ids (recent-window turns already in the chat).
    """
    if _chroma_turns is None:
        return []
    total = _chroma_turns.count()
    if total == 0:
        return []
    try:
        n_fetch = min(_MEMORY_RETRIEVE_K + len(exclude_ids) + 2, total)
        results = _chroma_turns.query(
            query_texts=[query_text[:1000]],
            n_results=n_fetch,
        )
        memories = []
        ids   = (results.get("ids")        or [[]])[0]
        docs  = (results.get("documents")  or [[]])[0]
        metas = (results.get("metadatas")  or [[]])[0]
        for rid, doc, meta in zip(ids, docs, metas):
            if rid in exclude_ids:
                continue
            memories.append({
                "text":        doc,
                "received_at": meta.get("received_at", ""),
                "is_player":   meta.get("is_player", "False") == "True",
            })
            if len(memories) >= _MEMORY_RETRIEVE_K:
                break
        return memories
    except Exception as exc:
        print(f"[diag/chroma] query error: {exc}")
        return []


def _chroma_backfill_async() -> None:
    """Index all turns already in _narrator_turns that aren't in ChromaDB yet.

    Called once after _restore_narrator_turns() so a fresh ChromaDB instance
    gets seeded from the persisted JSONL history.

    Runs sequentially and narrator-yielding: pauses whenever _generating is
    True so narrator Ollama calls are never queued behind embed calls.
    Rate-limited to ~3 embeds/second to avoid flooding Ollama's request queue.
    """
    if _chroma_turns is None:
        return
    snapshot = list(_narrator_turns)

    _BACKFILL_IDLE_SECS = 20.0   # require 20s of idle before each embed

    def _backfill() -> None:
        indexed = 0
        skipped = 0
        for turn in snapshot:
            # Idle-window guard: wait until _generating has been continuously
            # False for _BACKFILL_IDLE_SECS before making any embed call.
            # This prevents backfill embeds from racing with narrator calls —
            # once an HTTP embed request is in Ollama's queue it cannot be
            # cancelled and will block any subsequent narrator call.
            idle_since = None
            while True:
                if _generating or _classifying:
                    idle_since = None
                    time.sleep(1)
                    continue
                if idle_since is None:
                    idle_since = time.time()
                    time.sleep(1)
                    continue
                if time.time() - idle_since < _BACKFILL_IDLE_SECS:
                    time.sleep(1)
                    continue
                break  # idle for long enough — proceed

            try:
                turn_id = turn.get("turn_id", "")
                text = turn.get("raw_text", "").strip()
                if not turn_id or not text:
                    skipped += 1
                    continue
                # Skip turns that were removed by a world reset since startup.
                # After a world reset, _narrator_turns is cleared. Old turns
                # from the snapshot are irrelevant to the new session — they
                # should NOT be embedded (doing so would block the narrator
                # with Ollama embed calls during active gameplay).
                live_ids = {t.get("turn_id") for t in _narrator_turns}
                if turn_id not in live_ids:
                    skipped += 1
                    continue
                # Skip if already indexed (idempotent after restart backfill)
                if _chroma_turns and _chroma_turns.get(ids=[turn_id])["ids"]:
                    skipped += 1
                    continue
                if _chroma_turns:
                    _chroma_turns.add(
                        ids=[turn_id],
                        documents=[text[:2000]],
                        metadatas=[{
                            "is_player":     str(turn.get("is_player", False)),
                            "received_at":   turn.get("received_at", ""),
                            "narrator_name": turn.get("narrator_name", ""),
                        }],
                    )
                indexed += 1
            except Exception as exc:
                print(f"[diag/chroma] backfill error: {exc}")
            time.sleep(0.35)  # Rate limit: ~3 embeds/second max

        print(f"[diag/chroma] backfill complete: {indexed} indexed, {skipped} skipped")

    threading.Thread(target=_backfill, daemon=True, name="chroma-backfill").start()


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

# Display-format open/close tag pairs that must always be balanced.
# Unbalanced open > close means the response was cut off inside a format block.
_PAIRED_DISPLAY_TAGS = ('B', 'I', 'BI')


def _has_unbalanced_display_tags(text: str) -> bool:
    """True if any [B]/[I]/[BI] open tag has no matching close — text cut mid-format.

    Catches: 'The corridor is [B]ancient and narrow.' (ends with punct, but [/B] missing).
    Does NOT catch arbitrary tag types cut in half — use _has_unclosed_bracket() for that.
    """
    for tag in _PAIRED_DISPLAY_TAGS:
        opens  = len(re.findall(rf'\[{tag}(?:=[^\]]*)?]', text, re.IGNORECASE))
        closes = len(re.findall(rf'\[/{tag}]', text, re.IGNORECASE))
        if opens > closes:
            return True
    return False


def _has_unclosed_bracket(text: str) -> bool:
    """True if there are more [ than ] in the text — any tag was cut before its closing ].

    Catches cuts inside any tag type: [CHARACTER(Name): "text cut here,
    [GENERATE_IMAGE(description cut, [SIGHT: "cut, [/B cut mid-close-tag, etc.
    Since all [ in narrator output are tag delimiters (the model does not use raw
    square brackets in prose), more [ than ] reliably signals a truncated tag.
    """
    return text.count('[') > text.count(']')


def _is_truncated(text: str) -> bool:
    """True if text appears cut off.

    Three complementary checks — each catches what the others miss:

    1. Sentence-ending check: last character is not sentence-ending punctuation.
       Catches: mid-word cuts, mid-sentence cuts.

    2. Paired display-tag balance: a [B]/[I]/[BI] open tag has no matching close.
       Catches: cuts inside a format span that end with valid punctuation
       e.g. '[B]ancient corridor.' — ends with '.', tag never closed.

    3. Unclosed bracket: more [ than ] in the entire response.
       Catches: any tag type cut before its closing ']' —
       [CHARACTER(Name): "Hello  — cut mid-dialogue
       [GENERATE_IMAGE(vast room — cut mid-image-description
       [SIGHT: "copper walls    — cut mid-sense-tag
       [/B                      — close-tag cut in half

    The one undetectable case: structurally complete text that is semantically
    too short (model stopped after two sentences when ten were expected). That
    requires a minimum word-count heuristic and is a separate concern.
    """
    tail = text.rstrip()
    if not tail or len(tail) <= 100:
        return False
    return (
        tail[-1] not in _SENTENCE_ENDINGS
        or _has_unbalanced_display_tags(tail)
        or _has_unclosed_bracket(tail)
    )


def _build_static_world_context() -> str:
    """Compact inline world context used when ChromaDB is unavailable.

    Pulls the most critical facts from the in-memory _world entity graph
    (populated from world.json at startup) so the model has accurate setting,
    character, and NPC-location data even without RAG retrieval.

    Returns empty string if entity data not yet loaded.
    """
    parts: list[str] = []
    entities = _world.get("entities", {})

    fab = entities.get("fabrication_bay", {})
    if fab.get("description"):
        parts.append(
            "OPENING LOCATION — Fabrication Bay: "
            + fab["description"][:220].rstrip()
        )

    sherri = entities.get("sherri", {})
    if sherri.get("description"):
        voice_style = sherri.get("voice_style", "")[:120]
        parts.append(
            "SHERRI (automaton attendant — resident of the Fabrication Bay and Galley): "
            + sherri["description"][:220].rstrip()
            + (f" Voice: {voice_style}" if voice_style else "")
        )

    remnant = entities.get("the_remnant", {})
    if remnant.get("description"):
        voice_style = remnant.get("voice_style", "")[:120]
        parts.append(
            "THE REMNANT (crystalline 4D entity — disembodied voice, NOT physically "
            "present in rooms): "
            + remnant["description"][:220].rstrip()
            + (f" Voice: {voice_style}" if voice_style else "")
        )

    if not parts:
        return ""
    return (
        "[STATIC WORLD CONTEXT — background knowledge. "
        "DO NOT reproduce verbatim.]\n"
        + "\n".join(parts)
        + "\n[END STATIC WORLD CONTEXT]"
    )


def _build_messages() -> list[dict]:
    """Build Ollama messages with Sorting Hat RAG context.

    Instead of sending the full 11K-token system prompt (which gets truncated
    to only the last ~4K tokens at num_ctx=4096), we send:
      - A condensed ~200-token system core (narrator identity + immutable rules)
      - Top-6 Sorting Hat chunks relevant to the current player input (~800 tokens)
      - Recalled turn memories from ChromaDB when history is deep enough
      - Recent chat history (last 10 turns verbatim)

    Total system overhead: ~1,400 tokens vs ~11,000 → frees the context window
    for chat history, and the narrator sees the rules it actually needs right now.
    """
    # ── Find last player input for Sorting Hat query ─────────────────────
    last_player_text = ""
    for t in reversed(list(_narrator_turns)):
        if t.get("is_player") or any(
            b.get("isPlayer") for b in (t.get("parsed_blocks") or [])
        ):
            last_player_text = t.get("raw_text", "").strip()[:600]
            break

    # ── Sorting Hat: retrieve ollama-targeted world knowledge chunks ──────
    world_ctx = ""
    if last_player_text and _chroma_knowledge is not None and _chroma_knowledge.count() > 0:
        world_ctx = _retrieve_world_context(last_player_text)

    # ── System core (~490 tokens) ─────────────────────────────────────────
    # Replaces the full 46K-token system prompt with a compact authoritative
    # core that fits in every context window. World-specific knowledge is
    # retrieved via Sorting Hat RAG or _build_static_world_context() fallback.
    system_core = (
        "You are THE FORTRESS — the ancient, vast, sardonic narrator of this "
        "dark sci-fi interactive story. You speak for all characters except the player. "
        "NEVER act for the player. NEVER reproduce context blocks verbatim. "
        "Use second-person present tense.\n\n"

        "MANDATORY TAG RULES — emit these on their own line:\n"
        "[MOOD: \"tempo, instruments, emotional feel\"] — FIRST LINE every turn. "
        "Music descriptors only, under 20 words. NEVER prose. "
        "Example: [MOOD: \"slow ambient drone, metallic resonance, uneasy quiet\"]\n"
        "[CHARACTER(Name): \"exact words\"] — EVERY time ANY NPC speaks. "
        "NEVER write 'Name: ...' or 'Name said...' in prose. One tag per speech act.\n"
        "[INTRODUCE(Name): \"brief physical desc\"] — FIRST time a new NPC appears. "
        "NEVER emit [INTRODUCE(The Remnant)] or [INTRODUCE(The Fortress)] — "
        "they are narrators, not characters to introduce.\n"
        "[LORE(key): \"fact\"] — when lore or history is referenced.\n"
        "[SMELL(desc)], [TOUCH(desc)] — when sensory details are evoked.\n"
        "[GENERATE_IMAGE(location): \"sd_prompt\"] — when entering a new room or place. "
        "[GENERATE_IMAGE(subject): \"sd_prompt\"] — for a character or object close-up. "
        "NEVER use (scene) — only (location) or (subject) are valid types.\n"
        "[ITEM(key): \"desc\"] — when player receives or finds an object.\n"
        "[PLAYER_TRAIT(field): \"value\"] — NON-NEGOTIABLE: emit IMMEDIATELY whenever the "
        "player reveals name, appearance, pronouns, profession, traits, or history. "
        "Fields: name, pronouns, appearance, traits, history, goals. "
        "Example: [PLAYER_TRAIT(name): \"Wren\"] [PLAYER_TRAIT(appearance): \"tall, red hair\"]\n\n"

        "OPENING SEQUENCE (first turn only): The player has just been fabricated in the "
        "Fabrication Bay — a cavernous dark chamber with twelve dormant molecular assembly "
        "rigs in a semicircle. Sherri (the brushed-steel automaton attendant) is present "
        "with a glowing blue scanning wand. Begin there. Do NOT invent a different location. "
        "Emit [GENERATE_IMAGE(location): \"...\"] for the Fabrication Bay on the first turn.\n\n"

        "NPC PRESENCE RULES: Sherri is the resident of the Fabrication Bay and the Galley. "
        "The Remnant does NOT physically manifest in rooms — it is a disembodied voice and "
        "crystalline projection. It speaks from anywhere but is never described as a body "
        "in a room.\n\n"

        "ABSOLUTE PROHIBITIONS:\n"
        "- NEVER end a response with 'What would you like to do next?' or any menu prompt.\n"
        "- NEVER offer numbered option lists.\n"
        "- NEVER use AI-assistant phrases or break character.\n"
        "- Write ONE narrative beat per response. One moment: one image, one NPC line, "
        "one revelation. Stop when the player has something to react to. "
        "Do not write the full scene in a single response.\n\n"

        "The Fortress has been adrift between dimensions for millennia. "
        "Every surface remembers. Every NPC has a distinct voice. "
        "The player arrived through the fabrication process — form not yet fully defined."
    )

    system = system_core

    # ── World context: Sorting Hat RAG or static fallback ─────────────────
    if world_ctx:
        system += (
            "\n\n[WORLD CONTEXT — Knowledge retrieved for this moment. "
            "Use it to produce accurate character voices, sense tags, and lore. "
            "DO NOT quote or reproduce it verbatim.]\n"
            + world_ctx
            + "\n[END WORLD CONTEXT]"
        )
    elif _chroma_knowledge is None:
        # ChromaDB not installed — inject compact static world facts so the
        # model has accurate setting, NPC, and opening-location data instead
        # of inventing from scratch.
        static_ctx = _build_static_world_context()
        if static_ctx:
            system += "\n\n" + static_ctx

    # ── Player state ──────────────────────────────────────────────────────
    if _player_dressed:
        system += (
            "\n\n[PLAYER STATE] Player is already fully dressed and equipped. "
            "Wardrobe arc complete. Focus on mission, exploration, and new locations."
        )

    # ── Recalled turn memories (ChromaDB semantic path) ───────────────────
    all_turns = list(_narrator_turns)
    if (
        _chroma_turns is not None
        and len(all_turns) > _RECENT_TURNS_WINDOW + _MEMORY_MIN_HISTORY
    ):
        recent_turns = all_turns[-_RECENT_TURNS_WINDOW:]
        recent_ids   = {t.get("turn_id", "") for t in recent_turns}
        query_parts  = [t.get("raw_text", "")[:300]
                        for t in recent_turns[-4:]
                        if t.get("raw_text", "").strip()]
        memories = _chroma_query_relevant(" ".join(query_parts), exclude_ids=recent_ids)
        if memories:
            lines = []
            for m in memories:
                prefix = "Player" if m["is_player"] else "Narrator"
                short  = m["text"][:300].replace("\n", " ").strip()
                lines.append(f"  {prefix}: {short}")
            system += (
                "\n\n[RECALLED MEMORIES — silent context only. "
                "DO NOT reproduce in output.]\n"
                + "\n".join(lines)
                + "\n[END RECALLED MEMORIES]"
            )
        active_turns = recent_turns
    else:
        active_turns = all_turns

    msgs: list[dict] = [{"role": "system", "content": system}]

    if not all_turns:
        # First turn / post-reset: inject first_mes as the canonical opening
        # assistant turn so the model continues from the pre-written Fabrication
        # Bay scene (Sherri, scanning wand, three-note chime) rather than
        # inventing its own opening from scratch.
        if _first_mes:
            msgs.append({"role": "assistant", "content": _first_mes})
        msgs.append({
            "role": "user",
            "content": (
                "[OPENING SEQUENCE] The fabrication process has completed. "
                "The player is seated on the bench in the Fabrication Bay. "
                "Sherri has just finished her initial scan and is asking about "
                "name, form, and clothes. Continue the scene from exactly where "
                "first_mes left off — do NOT re-describe the arrival or re-emit "
                "the GENERATE_IMAGE for the Fabrication Bay."
            ),
        })
    else:
        for turn in active_turns:
            is_player = turn.get("is_player") or any(
                b.get("isPlayer") for b in (turn.get("parsed_blocks") or [])
            )
            text = turn.get("raw_text", "").strip()
            if text:
                msgs.append({"role": "user" if is_player else "assistant", "content": text})

    return msgs


def _stream_ollama_chat(
    messages: list[dict],
    timeout: float = 180.0,
    on_prose_sentence=None,   # optional callback(text: str) — called per prose sentence
    extra_options: dict | None = None,  # merged on top of default options (e.g. num_predict)
) -> str:
    """POST to Ollama /api/chat with streaming; return the full response text.

    If *on_prose_sentence* is provided it is called with each complete sentence
    of narrator prose as tokens arrive, stopping once a [CHARACTER tag appears.
    This allows the frontend to start TTS within 2-5 s rather than waiting for
    the full response (~20-40 s) before any audio plays.
    """
    payload = json.dumps({
        "model": _ollama_model(),
        "messages": messages,
        "stream": True,
        # num_ctx: 8192 for qwen2.5:14b.
        # Expanded system_core (~500 tokens) + static world context (~200 tokens)
        # + first_mes on turn 1 (~300 tokens) + 10-turn history (~3000 tokens)
        # + response buffer (~800 tokens) ≈ 4800 tokens — over the old 4096 default.
        # qwen2.5:14b supports 32K natively; 8192 is conservative.
        # Ollama only reallocates KV cache when num_ctx changes between calls —
        # keeping it fixed avoids per-call stalls.
        # num_predict: hard cap at 350 new tokens (~250 words) per narrator turn.
        # This is the primary defence against huge text blocks that saturate the GPU
        # by firing flask-sd + flask-music simultaneously after a 60s generation.
        # 350 tokens = one solid beat: MOOD tag + image tag + 2 prose paragraphs
        # + one NPC line. The model stops cleanly; Ollama returns done_reason=length.
        "options": {**{"num_ctx": 8192, "num_predict": 350}, **(extra_options or {})},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    full_text = ""
    prose_buf = ""
    prose_ended = False   # True once [CHARACTER tag seen — stop streaming chunks

    def _flush_prose(text: str) -> None:
        """Clean and emit one prose sentence via the callback (if non-empty)."""
        cleaned = _clean_narrator_prose(text).strip()
        if cleaned:
            on_prose_sentence(cleaned)

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

            # Progressive sentence streaming — only before the first CHARACTER tag.
            # Check for both bracketed "[CHARACTER" and drift variant "CHARACTER("
            # so bracket-less tags don't flow into TTS as narrator prose.
            if on_prose_sentence and not prose_ended:
                prose_buf += delta
                _char_sentinel = next(
                    (tok for tok in ("[CHARACTER", "CHARACTER(") if tok in prose_buf),
                    None,
                )
                if _char_sentinel:
                    prose_ended = True
                    pre = prose_buf[:prose_buf.index(_char_sentinel)]
                    if pre.strip():
                        _flush_prose(pre)
                else:
                    # Flush complete sentences on punctuation followed by whitespace/newline
                    while True:
                        m = re.search(r'(?<=[.!?])[\s\n]+', prose_buf)
                        if not m:
                            break
                        sentence = prose_buf[:m.start() + 1]   # include terminal punctuation
                        prose_buf = prose_buf[m.end():]
                        _flush_prose(sentence)

            if chunk.get("done"):
                # Flush any remaining prose that didn't end with sentence punctuation
                if on_prose_sentence and not prose_ended and prose_buf.strip():
                    _flush_prose(prose_buf)
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
# NOTE: raw_text is intentionally preserved with tags intact and sent back to Ollama
# in _build_messages() so the model sees its own previous output in canonical form.
# Only parsed_blocks (cleaned) is shown to the player or read by TTS.
_STRIP_DISPLAY_TAGS_RE = re.compile(
    r'\[/?(?:GENERATE_?IMAGE|INTRODUCE|ITEM|LORE|SFX|WORLD_EVENT|QUEST|PLAYER_SWITCH|CHARACTER'
    r'|PLAYER_TRAIT|UPDATE_PLAYER|PERMANENCE|LOCATION|NPC|ENTITY|META'
    r'|MOOD|SOUND|SIGHT|SMELL|TASTE|TOUCH|ENVIRONMENT)[^\]]*\]'
    r'(?:\s*:\s*"[^"]*")?',
    re.DOTALL | re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Tag normalizer — bracket-tolerant regexes that canonicalise LLM tag drift
# ---------------------------------------------------------------------------
# The LLM sometimes emits tags without [ ] delimiters (e.g. CHARACTER(X): "y"
# instead of [CHARACTER(X): "y"]).  These patterns match both forms and always
# output the canonical bracketed version.  Running this before _inject_missing_tags
# ensures _CHARACTER_RE in _parse_narrator_blocks() reliably extracts dialogue,
# and _STRIP_DISPLAY_TAGS_RE reliably removes all machine tags from display.
# Storing the normalized form in raw_text also means the model's own conversation
# history shows correct format — creating a positive reinforcement loop.

_NORM_CHARACTER_RE = re.compile(
    r'\[?CHARACTER\(([^)\]]+)\)\s*:\s*"([^"]+)"\]?',
    re.DOTALL,
)
_NORM_LORE_RE = re.compile(
    r'\[?LORE\(([^)\]]+)\)\s*:\s*"([^"]+)"\]?',
    re.DOTALL | re.IGNORECASE,
)
_NORM_ITEM_RE = re.compile(
    r'\[?ITEM\(([^)\]]+)\)\s*:\s*"([^"]+)"\]?',
    re.DOTALL | re.IGNORECASE,
)
_NORM_INTRODUCE_RE = re.compile(
    r'\[?INTRODUCE\(([^)\]]+)\)\s*:\s*"([^"]+)"\]?',
    re.DOTALL | re.IGNORECASE,
)


def _normalize_narrator_output(text: str) -> str:
    """Normalize LLM tag-format drift to canonical bracketed form.

    Handles the most common drift patterns:
      - Missing [ ] delimiters:  CHARACTER(X): "y"   → [CHARACTER(X): "y"]
      - Missing close bracket:   [CHARACTER(X): "y"  → [CHARACTER(X): "y"]
      - Missing open bracket:    CHARACTER(X): "y"]  → [CHARACTER(X): "y"]

    All four normalizers are idempotent — already-correct tags are unchanged.
    Call after _strip_context_bleed() and before _inject_missing_tags().
    """
    text = _NORM_CHARACTER_RE.sub(r'[CHARACTER(\1): "\2"]', text)
    text = _NORM_LORE_RE.sub(r'[LORE(\1): "\2"]', text)
    text = _NORM_ITEM_RE.sub(r'[ITEM(\1): "\2"]', text)
    text = _NORM_INTRODUCE_RE.sub(r'[INTRODUCE(\1): "\2"]', text)
    return text


def _clean_narrator_prose(text: str) -> str:
    """Strip system tags from narrator prose so they never appear in the feed.

    Display-only formatting tags ([B], [I], [BI], [C=...]) are intentionally
    preserved here so the frontend can convert them to HTML.  Markdown symbols
    (* ** _ # >) are stripped because the TTS engine reads them aloud.
    """
    cleaned = _STRIP_DISPLAY_TAGS_RE.sub("", text)
    # Strip sense-label prose prefixes ("Sight: ...", "Smell: ...", etc.).
    # The narrator sometimes writes these as visible paragraph headers instead of
    # using the machine tags.  They must never appear in the display feed.
    cleaned = re.sub(
        r'(?im)^[ \t]*(?:Sight|Smell|Sound|Touch|Taste|Environment)\s*:\s*',
        '', cleaned
    )
    # Also strip inline occurrences mid-sentence ("...cool air. Smell: The floor…")
    cleaned = re.sub(
        r'\b(?:Sight|Smell|Sound|Touch|Taste|Environment)\s*:\s*',
        ' ', cleaned
    )
    # Strip markdown bold/italic markers — TTS reads them aloud as "asterisk"
    cleaned = re.sub(r'\*{1,3}([^*\n]*?)\*{1,3}', r'\1', cleaned)
    cleaned = re.sub(r'_{1,2}([^_\n]*?)_{1,2}', r'\1', cleaned)
    # Strip markdown headers and blockquotes
    cleaned = re.sub(r'(?m)^#{1,6}\s+', '', cleaned)
    cleaned = re.sub(r'(?m)^>\s*', '', cleaned)
    # Collapse multiple blank lines left by removed tags
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()


def _infer_mood_from_prose(text: str) -> str:
    """Derive a music-mood descriptor from narrator prose when no [MOOD:] tag was emitted."""
    tl = text.lower()
    for pattern, mood in _MOOD_PATTERNS:
        if re.search(pattern, tl):
            return mood
    return 'ambient metallic drone, neutral tension'


def _inject_missing_tags(narrator_text: str) -> str:
    """Inject infrastructure tags the model omitted.

    MOOD     — always injected as ambient atmosphere fallback.
    CHARACTER — injected when prose shows NPC speech without a formal tag.
                This is a structural requirement: CHARACTER tags route TTS to
                the correct voice and gate the INTRODUCE tracking pipeline.
                Without them, all dialogue plays in the Fortress voice and NPCs
                are never added to the world entity graph.
    INTRODUCE — injected once per new CHARACTER name per session so the world
                graph ingestor tracks new NPCs correctly.

    LORE, ITEM, SMELL, TOUCH, TASTE, GENERATE_IMAGE — intentionally omitted.
    The Sorting Hat provides the narrator with the rules to produce these
    organically. Scoring should reflect true narrator capability.
    """
    global _introduced_this_session
    result = narrator_text

    # ── MOOD ──────────────────────────────────────────────────────────────
    if '[MOOD:' not in narrator_text[:300]:
        mood = _infer_mood_from_prose(narrator_text[:600])
        result = f'[MOOD: "{mood}"]\n' + result

    # ── CHARACTER — TTS routing infrastructure ────────────────────────────
    # When the model writes attributed speech without a [CHARACTER(...)] tag,
    # inject one so TTS routes to the right voice and INTRODUCE can fire.
    # Only one CHARACTER injection per NPC per turn.
    for npc_name, prose_re, npc_desc in _NPC_PROSE_PATTERNS:
        if (prose_re.search(narrator_text)
                and f'[CHARACTER({npc_name})' not in result):
            m = re.search(r'"([^"]{5,80})"', narrator_text)
            speech = m.group(1) if m else npc_desc
            result = f'[CHARACTER({npc_name}): "{speech[:80]}"]\n' + result

    # ── INTRODUCE — world-graph entity tracking ───────────────────────────
    # Any [CHARACTER(Name)] speaker not yet introduced gets a one-time
    # [INTRODUCE(Name)] prepended so world-state ingestor tracks them.
    # Scan `result` (not narrator_text) to catch just-injected tags.
    for name in re.findall(r'\[CHARACTER\(([^)]+)\)', result):
        name = name.strip()
        if (name not in _PERMANENT_CREW
                and name not in _introduced_this_session
                and f'[INTRODUCE({name})' not in result):
            result = f'[INTRODUCE({name}): "Character present in The Fortress"]\n' + result
        _introduced_this_session.add(name)
        # Queue this NPC for pre-quirkify enrichment (low-priority background pass)
        entity_id = name.lower().replace(" ", "_")
        if entity_id not in _quirkified_this_session:
            _quirkify_queue.append(entity_id)

    return result


def _strip_context_bleed(text: str) -> str:
    """Remove injected context blocks that the model accidentally echoed in output.

    Handles: [RECALLED MEMORIES] dumps, [PILOT QUEST] regurgitation,
    markdown-formatted key-beat lists, numbered option menus, third-person
    player references, and AI-assistant opt-in phrasing.
    """
    # Strip [RECALLED MEMORIES] / [INTERNAL CONTEXT] full blocks
    text = re.sub(
        r"\[(?:INTERNAL (?:CONTEXT|STORY NOTES)[^\]]*|RECALLED MEMORIES[^\]]*)\]"
        r".*?"
        r"\[END (?:RECALLED MEMORIES|INTERNAL CONTEXT)[^\]]*\]",
        "", text, flags=re.DOTALL | re.IGNORECASE,
    )
    # Strip any lingering individual recalled-memory timestamp lines
    text = re.sub(
        r"(?m)^\s*\[20\d\d-\d\d-\d\dT\d\d:\d\d\]\s+(?:Player|Narrator):.*$",
        "", text, flags=re.IGNORECASE,
    )
    # Strip [PILOT QUEST] / [END PILOT QUEST] blocks if echoed
    text = re.sub(
        r"\[(?:INTERNAL STORY NOTES[^\]]*\]\s*\n?\s*\[)?PILOT QUEST[^\]]*\]"
        r".*?"
        r"\[END PILOT QUEST[^\]]*\]",
        "", text, flags=re.DOTALL | re.IGNORECASE,
    )
    # Strip markdown-formatted context dump sections (bold headers + bullet lists)
    text = re.sub(
        r"\*\*(?:Recalled Memories|Phase \d+[^*]*Key Beats?|Next Steps?|Current Objective|Phases?)[^*]*\*\*"
        r"[:\s]*(?:\n\s*[-*•\d].*?)*",
        "", text, flags=re.IGNORECASE | re.MULTILINE,
    )
    # Strip "--- " section dividers the model sometimes outputs from the quest injection
    text = re.sub(r"(?m)^---\s*$", "", text)
    # Strip option-menu closing questions
    text = re.sub(
        r"(?i)which (?:path|option|approach|route|direction)(?:\s+would you like to (?:pursue|take|choose))?.*?\?",
        "", text,
    )
    # Strip numbered option lists at line-start (classic "1. Continue... 2. Insult...")
    text = re.sub(
        r"(?m)^\d+\.\s+(?:Continue|Proceed|Insult|Trace|Follow|Work with|Go to|Head to|Try|Visit|Ask|Use)\b.*$",
        "", text, flags=re.IGNORECASE,
    )
    # Strip quest-path labels that models echo from the injected story notes
    text = re.sub(
        r"(?mi)^[ \t]*(?:Technical Path|Remnant['']s Way|Fortress['']s Way|Neutral Path|"
        r"Moral [Cc]hoice|Phase \d+[^:]*|True Goal|Situation:|Entry:|Twist:|Key [Bb]eat:|"
        r"Rewards?:|Path \d+|Faction:|Awareness:|Tone:)\s*:.*$",
        "", text,
    )
    # Strip bullet-list quest paths (- Name: description)
    text = re.sub(
        r"(?m)^[ \t]*[-•]\s+(?:Technical|Remnant|Fortress|Neutral|Path)\b[^:\n]*:[^\n]*$",
        "", text, flags=re.IGNORECASE,
    )
    # Strip third-person player references ("The player takes a moment to...")
    text = re.sub(
        r"(?i)\bthe player (?:takes?|feels?|notices?|turns?|pauses?|considers?|reflects?|stands?|seems?|moves?|heads?)\b[^.!?]*[.!?]",
        "", text,
    )
    # Strip markdown emphasis markers that TTS reads aloud as "asterisk"
    text = re.sub(r'\*{1,3}([^*\n]*?)\*{1,3}', r'\1', text)
    text = re.sub(r'_{1,2}([^_\n]*?)_{1,2}', r'\1', text)
    # Strip display-only format tags [B]/[/B]/[I]/[/I]/[C=...]/[/C]
    # so they don't appear in TTS text from context-bleed paths
    text = re.sub(r'\[/?(?:B|I|BI|C(?:=[^\]]*)?)\]', '', text)
    # Collapse excess blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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
    r'suit is ready|ready to wear|suit up|outfitted|equipped|'
    # Broader matches for llama3.1:8b phrasing
    r'looks great on you|fits you well|your new (?:clothes|outfit|suit|attire|gear)|'
    r'now wearing|you are now dressed|slipping into|put on your|wearing your|'
    r'new outfit|new clothes|tailored|stitched|assembled your|crafted your)',
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
    # Fallback: if appearance is already captured and narrator describes
    # the player wearing something — broad enough for any model
    has_appearance_worn = (
        bool(_player_appearance_desc)
        and bool(re.search(
            r'\b(?:dressed|wearing|outfit|clothes|suit|attire|garment|fabric)\b',
            narrator_text, re.IGNORECASE,
        ))
    )

    if not (has_clothing_item or has_done_phrase or has_appearance_worn):
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
    """Build an SD portrait prompt from the dressing scene and call flask-sd.

    Narrator-yielding: waits until _generating has been False for 5s before
    calling Ollama, so we don't compete with an imminent narrator call.
    """
    # Yield to narrator: wait for 5s of continuous idle before using Ollama.
    # Also yields when _classifying (_sorting_hat Ollama call in flight).
    idle_since = None
    while True:
        if _generating or _classifying:
            idle_since = None
            time.sleep(1)
            continue
        if idle_since is None:
            idle_since = time.time()
            time.sleep(1)
            continue
        if time.time() - idle_since < 5.0:
            time.sleep(1)
            continue
        break
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

    # Call flask-sd — prepend style prefix for consistency with world assets
    sd_prompt = _SD_STYLE_PREFIX + sd_prompt
    gen_payload = json.dumps({"prompt": sd_prompt, "width": 512, "height": 512}).encode("utf-8")
    img_code, img_resp, _ = _http("POST", f"{FLASK_SD_URL}/api/generate", body=gen_payload, timeout=120.0)
    if img_code == 200:
        data = json.loads(img_resp)
        img_url = data.get("image") or data.get("image_url") or data.get("url") or ""
        if img_url:
            print(f"[diag] player avatar generated ({len(img_url)} chars)")
            _sse_broadcast("meta", {
                "type": "player_portrait",
                "url": img_url,
                "prompt": sd_prompt,
            })
        else:
            print(f"[diag] player avatar SD returned 200 but no image key — keys: {list(data.keys())}")
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

        # Pre-generate the turn_id so streaming chunks reference it before
        # the full "turn" event fires.
        turn_id = f"narrator-{uuid.uuid4().hex[:8]}"

        for attempt in range(4):   # 1 initial + up to 3 continues
            if attempt > 0:
                _sse_broadcast("activity", {"text": f"📝 continuing… ({attempt}/3)"})
                # Append current output as assistant turn, then request continuation
                messages = messages + [
                    {"role": "assistant", "content": full_text},
                    {"role": "user", "content": "Please continue."},
                ]

            # On the first attempt, stream prose sentences to the frontend so
            # TTS can start within seconds instead of waiting for the full response.
            # Continuation attempts don't stream — the prose is already mid-flight.
            prose_callback = None
            if attempt == 0:
                def prose_callback(sentence: str, _tid=turn_id) -> None:  # noqa: E731
                    _sse_broadcast("chunk", {
                        "turn_id": _tid,
                        "text": sentence,
                        "channel": "narrator",
                    })

            try:
                chunk = _stream_ollama_chat(messages, timeout=90.0,
                                            on_prose_sentence=prose_callback)
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

        # Scrub any context blocks the model accidentally echoed before storing/broadcasting
        full_text = _strip_context_bleed(full_text)
        if not full_text.strip():
            return

        # Normalize tag format drift (bracket-less tags → canonical [TAG(x): "y"] form).
        # Must run before inject so _inject_missing_tags sees properly bracketed existing tags
        # and before raw_text is stored so the model's history always shows canonical form.
        full_text = _normalize_narrator_output(full_text)

        # Inject any machine tags the model forgot to emit (MOOD, INTRODUCE, LORE).
        # Must run after context-bleed removal so we operate on clean prose.
        full_text = _inject_missing_tags(full_text)

        # Re-process any INTRODUCE tags added by injection — the streaming path
        # already parsed structured tags before injection ran, so injected
        # INTRODUCE tags were never reached by _ingest_narrator_turn_into_world.
        # Creating entities here ensures npcs_created diffs see them in world-state.
        for _intro_name in re.findall(r'\[INTRODUCE\(([^)]+)\)', full_text):
            _intro_name = _intro_name.strip()
            _intro_eid = re.sub(r'[^a-z0-9_]', '_', _intro_name.lower()).strip('_')
            if _intro_eid and _intro_eid not in _world.get('entities', {}):
                _ensure_entity(_intro_eid, _intro_name, 'npc')

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        turn = {
            "turn_id": turn_id,   # use the pre-generated id (matches chunk events)
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
        # NOTE: We do NOT unload llama3.1:8b from VRAM between turns.
        # Unloading caused the model to fall back to CPU (1-3 tok/s) on the
        # next turn due to VRAM pressure from SD/music/system apps — resulting
        # in >300s timeouts. Keeping the model loaded (Ollama's default keep_alive
        # of 5 minutes) is the correct tradeoff: narration is always fast, and
        # SD/music generation uses whatever VRAM remains.
        # Kick off concurrent post-processing (non-blocking)
        _enqueue_image_generation(full_text)
        _broadcast_narrator_mood(full_text)    # [MOOD: "..."] → mood SSE → music
        _broadcast_narrator_sound(full_text)   # [SOUND: "..."] → sfx SSE → sound effects
        # Sense enrichment disabled: it made sequential Ollama calls that blocked the
        # narrator generation queue, causing 150+ second turn timeouts.
        # The narrator is instructed to include all sense channels inline.
        # _schedule_sense_enrichment(full_text)
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
    # Keep all generated images in the stylized world-asset aesthetic — no real-world photography
    "photorealistic, photograph, real photo, stock photo, DSLR, camera shot, "
    "modern earth, contemporary setting, mundane, generic background, "
    "(genitals:1.8), (penis:1.8), (vagina:1.8), (explicit nudity:1.8), "
    "(exposed groin:1.7), (sexual:1.6)"
)
_NO_TEXT_SUFFIX = ", (no text:1.4), (no writing:1.4), (no letters:1.4)"

# Global style prefix prepended to all dynamic SD prompts.
# Keeps generated images visually consistent with permanent world assets:
# dark painterly sci-fi concept art, not photorealistic photography.
_SD_STYLE_PREFIX = (
    "dark sci-fi concept art, painterly illustration, cinematic composition, "
    "dramatic lighting, highly detailed, "
)
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

# ── Static scene images — permanent canonical assets ───────────────────────
# Maps asset-id → web-relative URL served by nginx from web/assets/.
# When the narrator generates a GENERATE_IMAGE description that matches one of
# these assets, we broadcast the URL directly instead of calling flask-sd.
# This is the same keyword-bypass pattern used for music and SFX.
_STATIC_SCENE_IMAGES: dict[str, str] = {
    # Locations
    "portal-chamber":    "/game/assets/locations/portal-chamber.jpg",
    "the-nexus":         "/game/assets/locations/the-nexus.jpg",
    "the-galley":        "/game/assets/locations/the-galley.jpg",
    "fabrication-bay":   "/game/assets/locations/fabrication-bay.jpg",
    "sleeping-quarters": "/game/assets/locations/sleeping-quarters.jpg",
    "fortress-exterior": "/game/assets/locations/fortress-exterior-aft.jpg",
    "port-interior":     "/game/assets/locations/port-interior.jpg",
    "portal-closeup":    "/game/assets/locations/portal-closeup.jpg",
    # Characters
    "the-remnant":       "/game/assets/characters/the-remnant-true-form.jpg",
    "sherri-galley":     "/game/assets/characters/sherri-galley.jpg",
    "sherri-gantry":     "/game/assets/characters/sherri-gantry.jpg",
    "sherri-foundry":    "/game/assets/characters/sherri-foundry.jpg",
    "sherri-inspector":  "/game/assets/characters/sherri-inspector.jpg",
}

# Keyword table: (asset-id, [keyword-substrings-to-match-in-description-lower])
_SCENE_IMAGE_KEYWORDS: list[tuple[str, list[str]]] = [
    ("portal-chamber",    ["portal chamber", "null-space port", "null space port",
                           "aft portal", "chiral portal", "portal room"]),
    ("the-nexus",         ["the nexus", "central nexus", "command nexus",
                           "nexus pulpit", "at the pulpit", "standing at the nexus"]),
    ("the-galley",        ["the galley", "in the galley", "galley interior",
                           "kitchen area", "sherri.*galley", "galley.*sherri",
                           "sherri.*tea", "sherri.*scone", "sherri.*meal"]),
    ("fabrication-bay",   ["fabrication bay", "fabrication rig", "fab bay",
                           "sherri.*fabricat", "fabricat.*sherri",
                           "lathe", "fabrication bench"]),
    ("sleeping-quarters", ["sleeping quarters", "bunk room", "dormitory",
                           "sleeping area", "your bunk", "the bunk"]),
    ("fortress-exterior", ["outer hull", "exterior catwalk", "gantry walk",
                           "fortress exterior", "null space vista",
                           "outside the fortress", "starfield.*hull",
                           "hull.*starfield", "aft exterior"]),
    ("port-interior",     ["port interior", "interior port", "docking bay",
                           "dock interior"]),
    ("portal-closeup",    ["portal surface", "hoop up close", "portal detail",
                           "dimensional gateway", "portal close"]),
    ("the-remnant",       ["the remnant.*portrait", "remnant.*close.?up",
                           "remnant.*face", "ancient ai.*form",
                           "remnant.*true form"]),
    ("sherri-galley",     ["sherri.*standing in galley", "sherri.*at the stove",
                           "sherri.*full view", "sherri.*portrait"]),
    ("sherri-gantry",     ["sherri.*gantry", "sherri.*catwalk", "sherri.*outer hull"]),
    ("sherri-foundry",    ["sherri.*foundry", "sherri.*smelt", "sherri.*weld",
                           "sherri.*forge"]),
    ("sherri-inspector",  ["sherri.*inspect", "sherri.*scanning", "sherri.*examining",
                           "sherri.*readout", "sherri.*calibrat"]),
]


def _match_static_scene(description: str) -> str | None:
    """Return a static web URL for known permanent assets, or None to use SD."""
    lc = description.lower()
    for asset_id, keywords in _SCENE_IMAGE_KEYWORDS:
        for kw in keywords:
            if re.search(kw, lc):
                url = _STATIC_SCENE_IMAGES.get(asset_id)
                if url:
                    return url
    return None


def _enqueue_image_generation(narrator_text: str) -> None:
    threading.Thread(
        target=_do_image_generation, args=(narrator_text,), daemon=True, name="img-gen"
    ).start()


def _prewarm_visuals() -> None:
    """Splash pre-warm: generate the current location image in the background.

    Called when the frontend sends a prewarm=true player-input.  Uses the
    Sorting Hat to retrieve the current location's sd_prompt from world_knowledge,
    then fires flask-sd.  Narrator-yielding: waits 5s idle before the SD call.
    """
    # Yield to any active narrator generation first
    idle_since: float | None = None
    for _ in range(30):                 # up to 30s total wait
        if _generating or _classifying:
            idle_since = None
            time.sleep(1)
            continue
        if idle_since is None:
            idle_since = time.time()
            time.sleep(1)
            continue
        if time.time() - idle_since < 5.0:
            time.sleep(1)
            continue
        break

    # Retrieve location-specific sd_prompt from world_knowledge
    hint = ""
    try:
        if _chroma_knowledge and _chroma_knowledge.count() > 0:
            # Query using player's last known location or generic "current scene"
            locs = [e for e in _world.get("entities", {}).values()
                    if e.get("type") == "location"]
            loc_id = locs[-1].get("id", "portal_bay") if locs else "portal_bay"
            qr = _chroma_knowledge.query(
                query_texts=[f"location {loc_id} sight scene"],
                n_results=3,
                where={"service_target": "flask-sd"},
                include=["documents"],
            )
            docs = (qr.get("documents") or [[]])[0]
            hint = " ".join(docs)[:400].strip()
    except Exception:
        pass

    if not hint:
        hint = "ancient space station interior, dark sci-fi, atmospheric"

    sd_prompt = _SD_STYLE_PREFIX + hint
    gen_payload = json.dumps({"prompt": sd_prompt, "width": 768, "height": 512}).encode("utf-8")
    try:
        img_code, img_resp, _ = _http(
            "POST", f"{FLASK_SD_URL}/api/generate",
            body=gen_payload, timeout=120.0,
        )
        if img_code == 200:
            data = json.loads(img_resp)
            img_url = data.get("image") or data.get("image_url") or data.get("url") or ""
            if img_url:
                _sse_broadcast("scene_image", {"image": img_url, "kind": "location",
                                               "description": "pre-warmed scene"})
                print(f"[diag/prewarm] location image ready ({len(img_url)} chars)")
    except Exception as exc:
        print(f"[diag/prewarm] flask-sd prewarm failed: {exc}")


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

    # Guard: wait for Ollama to be genuinely idle before calling flask-SD.
    # llama3.1:8b (5.2 GB) + Stable Diffusion (3.4 GB) + flask-music (2 GB)
    # + system apps (~4.7 GB) = ~15.3 GB — right at the 16 GB VRAM edge.
    # Starting SD while Ollama is generating risks evicting the LLM to CPU
    # (1-3 tok/s → 300s narrator timeout).
    #
    # Two-phase idle check:
    #   Phase 1: wait up to 60s for _generating to be False (narrative done)
    #   Phase 2: wait 5s more to let the next player input arrive and set
    #            _generating=True again (story test submits inputs in ~2s)
    # If _generating is still False after both waits, Ollama is truly idle
    # (user is thinking/reading) and SD generation is safe.
    _sd_idle_deadline = time.time() + 60.0
    while (_generating or _classifying) and time.time() < _sd_idle_deadline:
        time.sleep(0.5)
    if _generating or _classifying:
        return   # Ollama still busy — skip image
    # Phase 2: stability wait — during story test the next input arrives in ~2s;
    # in real gameplay the user pauses 5-30s.  5s catches rapid test submission.
    time.sleep(5.0)
    if _generating or _classifying:
        return   # next turn has started — skip SD to protect narrator latency

    for kind, description in markers:
        kind = (kind or "location").strip()
        lc = description.lower()
        if any(bad in lc for bad in _SD_BLOCKLIST):
            print(f"[diag] img-gen blocked: content policy match")
            continue

        # ── Object permanence: inject player appearance when player is in frame ──
        # If this image features the player character and we know what they look like,
        # append a concise appearance clause so the SD model maintains visual continuity
        # across turns (same face, hair, clothing colour, etc.).
        _img_player_name = (_world["entities"].get("__player__") or {}).get("canonical_name", "")
        if _player_appearance_desc and _img_player_name:
            player_lc = _img_player_name.lower()
            if player_lc in lc or "player character" in lc or "protagonist" in lc:
                # First sentence of appearance desc — enough for visual continuity
                first_sentence = _player_appearance_desc.split('.')[0].strip()
                if first_sentence:
                    description = description.rstrip('. ') + ', ' + first_sentence[:120]
                    lc = description.lower()

        # ── Static asset shortcut ─────────────────────────────────────────
        # For known permanent locations and characters, serve the pre-generated
        # canonical image directly as a URL — no SD generation needed.
        static_url = _match_static_scene(description)
        if static_url:
            print(f"[diag] img-gen static: {static_url}")
            _latest_scene_image = {"image": static_url, "kind": kind,
                                   "description": description, "source": "static"}
            _sse_broadcast("scene_image", {"image": static_url, "kind": kind,
                                           "description": description, "source": "static"})
            continue

        # Final VRAM guard: if Ollama has become active since the entry guard
        # (story test sends next player input immediately after receiving a turn),
        # skip this image generation to protect narrator latency.
        if _generating or _classifying:
            return

        _sse_broadcast("activity", {"text": f"⏳ rendering {kind}…"})
        neg = _DEFAULT_NEGATIVE_PROMPT + _NO_TEXT_SUFFIX
        if any(kw in lc for kw in _NUDITY_KEYWORDS):
            neg += _NUDE_COVERAGE_SUFFIX
        try:
            # Prepend global style prefix so all generated images stay consistent
            # with the hand-crafted world assets (painterly, not photorealistic).
            styled_description = _SD_STYLE_PREFIX + description
            payload = json.dumps({"prompt": styled_description, "negative_prompt": neg}).encode("utf-8")
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
    then broadcast it so the UI can update the thumbnail tooltip.

    Narrator-yielding: waits until _generating has been False for 5s before
    calling Ollama, so we don't compete with an imminent narrator call.
    """
    # Yield to narrator: wait for 5s of continuous idle before using Ollama.
    # Also yields when _classifying (_sorting_hat Ollama call in flight).
    idle_since = None
    while True:
        if _generating or _classifying:
            idle_since = None
            time.sleep(1)
            continue
        if idle_since is None:
            idle_since = time.time()
            time.sleep(1)
            continue
        if time.time() - idle_since < 5.0:
            time.sleep(1)
            continue
        break
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
    use the full ~16 GB VRAM budget without competing with llama3.1:8b.

    Ollama will lazy-reload the model on the next narrator call (~2-5s overhead
    for model load from disk; acceptable vs. the alternative of VRAM starvation
    causing 300s timeouts when SD and LLM try to share a 16 GB card).

    Sends a generate request with keep_alive=0, which is Ollama's documented
    method for immediate VRAM eviction. Silently swallows errors.
    """
    try:
        model = _ollama_model()
        # Explicit empty prompt + stream=False + num_predict=0 so Ollama
        # returns IMMEDIATELY with no generation. keep_alive=0 tells Ollama
        # to evict the model from VRAM right after this call completes.
        # Without stream=False, Ollama may start an infinite streaming loop
        # that blocks subsequent narrator calls even after our socket closes.
        payload = json.dumps({
            "model": model,
            "prompt": "",
            "stream": False,
            "keep_alive": 0,
            "options": {"num_predict": 0},
        }).encode("utf-8")
        _http("POST", f"{OLLAMA_URL}/api/generate", body=payload, timeout=10.0)
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
    _chroma_add_turn_async(turn)  # non-blocking semantic index


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
    for key, svc in (("flask-sd", fsd), ("ollama", oll)):
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
            "params": {"service": {"type": "string", "enum": ["flask-sd", "ollama", "all"]}},
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
            for name in ("flask-sd.json", "ollama.json"):
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
                "all": "docker compose restart flask-sd ollama nginx",
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
            # Re-index system prompt chunks with new content
            threading.Thread(
                target=_index_world_knowledge,
                daemon=True,
                name="world-knowledge-reindex",
            ).start()
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
    "docker/diag/seed/fortress_system_prompt.txt",
    "docker/diag/seed/fortress_first_mes.txt",
    "docker/diag/seed/world.json",
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
                cwd=str(_REPO_ROOT), stderr=subprocess.DEVNULL, timeout=2,
                **_SUBPROCESS_NO_WINDOW,
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

    flask_sd_probe = _probe_json(f"{FLASK_SD_URL}/api/health")
    ollama_probe = _probe_json(f"{OLLAMA_URL}/api/tags")
    fm_code, _, fm_lat = _http("GET", f"{FLASK_MUSIC_URL}/health", timeout=2.0)
    fm_probe = {
        "reachable": fm_code == 200,
        "latency_ms": round(fm_lat * 1000, 1) if fm_lat else None,
    }

    services = {
        "flask-sd": {"status_file": flask_sd_status, "probe": flask_sd_probe},
        "ollama": {"status_file": ollama_status, "probe": ollama_probe},
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
            for svc in ("flask-sd", "ollama", "diag")
        },
        "detected_issues": issues,
        "suggested_action_ids": suggested,
        "action_catalog": _action_catalog(),
        "environment": {
            "status_dir": str(STATUS_DIR),
            "flask_sd_url": FLASK_SD_URL,
            "ollama_url": OLLAMA_URL,
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
            **_SUBPROCESS_NO_WINDOW,
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

        if path == "/session-state":
            # Fast — no Ollama call. Used by the splash screen to decide mode.
            locs = [e for e in _world.get("entities", {}).values()
                    if e.get("type") == "location"]
            last_loc = locs[-1].get("id", "") if locs else ""
            chunks = 0
            try:
                if _chroma_knowledge is not None:
                    chunks = _chroma_knowledge.count()
            except Exception:
                pass
            self._send_json(200, {
                "mode": "continue" if _narrator_turns else "new",
                "turns": len(_narrator_turns),
                "npcs_met": sorted(_introduced_this_session),
                "last_location": last_loc,
                "sorting_hat_ready": chunks > 0,
                "world_chunks": chunks,
                "player_dressed": _player_dressed,
            })
            return

        if path == "/session-summary":
            # Blocking — calls Ollama to produce a 2-3 sentence catch-up summary.
            # Only called on "continue" sessions; 20s timeout.
            if not _narrator_turns:
                self._send_json(200, {"summary": "", "mode": "new"})
                return
            recent = list(_narrator_turns)[-20:]
            story_digest = "\n".join(
                t.get("raw_text", "")[:150].replace("\n", " ").strip()
                for t in recent
                if not t.get("is_player") and t.get("raw_text", "").strip()
            )[:1200]
            prompt = (
                "You are The Fortress — the ancient, sardonic narrator of a dark sci-fi story "
                "set aboard a self-aware station drifting between dimensions. "
                "In 2-3 vivid sentences, remind the returning player what has happened so far. "
                "Be evocative, atmospheric, and in character. "
                "Start with 'When you left...' or 'Last time, you...' or similar.\n\n"
                f"STORY SO FAR:\n{story_digest}\n\nReminder (2-3 sentences):"
            )
            s_payload = json.dumps({
                "model": _ollama_model(),
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": 140, "temperature": 0.65},
            }).encode("utf-8")
            code, resp, _ = _http(
                "POST", f"{OLLAMA_URL}/api/generate",
                body=s_payload, timeout=20.0,
            )
            summary = ""
            if code == 200:
                try:
                    summary = json.loads(resp).get("response", "").strip()
                except Exception:
                    pass
            self._send_json(200, {"summary": summary, "mode": "continue"})
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

        if path == "/next-banter":
            # Pop one ready banter item from the queue (204 No Content if empty).
            if _banter_queue:
                self._send_json(200, _banter_queue.popleft())
            else:
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
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
        global _browser_health, _classifying
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

                # Splash pre-warm: fire location image only, no narrator turn
                if payload.get("prewarm"):
                    threading.Thread(
                        target=_prewarm_visuals, daemon=True, name="splash-prewarm",
                    ).start()
                    self._send_json(200, {"ok": True, "prewarm": True})
                    return

                text = payload.get("text", "").strip()
                if not text:
                    self._send_json(400, {"ok": False, "error": "empty text"})
                    return
                # Set _classifying so background Ollama threads yield during this call.
                # _sorting_hat may hit Ollama for ambiguous inputs (5-8s with reload).
                # Without this flag, background threads with 5s idle windows can fire
                # and block the narrator's subsequent Ollama call.
                _classifying = True
                try:
                    intent = _sorting_hat(text)
                finally:
                    _classifying = False

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

                global _last_player_ts, _player_appearance_desc
                _last_player_ts = time.time()  # reset idle lore timer

                # Early avatar from player self-description.
                # If the player sends a message that looks like an appearance description
                # (≥3 appearance markers) and we don't yet have one, capture it and fire
                # avatar generation immediately — don't wait for the dressing scene.
                _early_player_name = (_world["entities"].get("__player__") or {}).get("canonical_name", "")
                if not _player_appearance_desc and _early_player_name:
                    _lc_input = text.lower()
                    _appearance_markers = [
                        "i am ", "i'm ", "hair", "eyes", "skin",
                        "tall", "short", "wearing", "feature",
                    ]
                    if sum(1 for m in _appearance_markers if m in _lc_input) >= 3:
                        _player_appearance_desc = text[:400]
                        threading.Thread(
                            target=_generate_player_avatar,
                            args=(_player_appearance_desc,),
                            daemon=True,
                            name="player-avatar-early",
                        ).start()

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

        if path == "/regenerate-scene":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
                desc = (body.get("description") or "").strip()
                if desc:
                    # Re-use existing image generation pipeline — VRAM guard and SSE
                    # broadcast are included. Wraps desc in GENERATE_IMAGE tag so the
                    # full pipeline (style prefix, blocklist, player-appearance injection)
                    # applies exactly as in normal narrator-driven generation.
                    safe_desc = desc.replace('"', "'")[:350]
                    threading.Thread(
                        target=_do_image_generation,
                        args=(f'[GENERATE_IMAGE(location): "{safe_desc}"]',),
                        daemon=True,
                        name="regen-scene",
                    ).start()
                    self._send_json(200, {"ok": True, "description": desc[:80]})
                else:
                    # No description — regenerate from latest scene image description
                    fallback = (_latest_scene_image or {}).get("description", "")
                    if fallback:
                        safe = fallback.replace('"', "'")[:350]
                        threading.Thread(
                            target=_do_image_generation,
                            args=(f'[GENERATE_IMAGE(location): "{safe}"]',),
                            daemon=True,
                            name="regen-scene",
                        ).start()
                        self._send_json(200, {"ok": True, "description": fallback[:80]})
                    else:
                        self._send_json(400, {"ok": False, "error": "no description available"})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
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


def _ollama_watchdog_loop() -> None:
    """Ping Ollama every 30 s. Broadcast SSE warning after 2 consecutive failures.

    Does NOT touch _generating — the 90 s narrator timeout handles abort cleanly
    via the finally block in _generate_narrator_turn().
    """
    global _ollama_healthy, _ollama_fail_count
    import urllib.request as _ur  # noqa: PLC0415 — stdlib, always available
    while True:
        time.sleep(30)
        try:
            with _ur.urlopen(f"{OLLAMA_URL}/api/tags", timeout=5) as _r:
                _r.read()
            # --- healthy ---
            if not _ollama_healthy:
                _log("ollama watchdog: Ollama recovered")
                _ollama_healthy = True
                _sse_broadcast("activity", {"text": ""})
            _ollama_fail_count = 0
        except Exception as _e:
            _ollama_fail_count += 1
            _log(f"ollama watchdog: ping failed ({_ollama_fail_count}) — {_e}")
            if _ollama_fail_count >= 2 and _ollama_healthy:
                _ollama_healthy = False
                _sse_broadcast("activity", {"text": "⚠ language model reconnecting…"})


def _generate_npc_banter(npc_a: str, npc_b: str) -> None:
    """Generate a short overheard conversation between two present NPCs.

    Uses a minimal Ollama call (200-token budget, CHARACTER tags only) so it
    doesn't compete with the main narrator.  Result is pushed onto _banter_queue
    as {speakers, lines:[{speaker,text,voice}], area} for the client to play
    via _tryPlayBanter() when the TTS channel goes idle.
    """
    global _banter_generating
    try:
        area = _world.get("current_location", "somewhere aboard the Fortress")
        ent_a = _world["entities"].get(_loc_id(npc_a), {})
        ent_b = _world["entities"].get(_loc_id(npc_b), {})

        banter_system = (
            "You write brief overheard NPC conversations for a dark sci-fi interactive "
            "story aboard The Remnant Fortress. "
            "Output ONLY dialogue lines, each formatted as: [CHARACTER(Name): \"line\"]. "
            "No prose, no narration, no stage directions. 3-4 exchanges. Stay in character."
        )
        desc_a = (ent_a.get("description", "") or "")[:120]
        desc_b = (ent_b.get("description", "") or "")[:120]
        banter_user = (
            f"Write a short overheard conversation between {npc_a} and {npc_b} "
            f"in {area}.\n"
            + (f"{npc_a}: {desc_a}\n" if desc_a else "")
            + (f"{npc_b}: {desc_b}\n" if desc_b else "")
            + "3-4 lines total. Natural, in-world dialogue only."
        )

        msgs = [
            {"role": "system", "content": banter_system},
            {"role": "user",   "content": banter_user},
        ]

        raw = _stream_ollama_chat(
            msgs, timeout=45.0,
            extra_options={"num_ctx": 2048, "num_predict": 200},
        )
        _log(f"[banter] raw response: {raw[:120]}")

        char_re = re.compile(r'\[CHARACTER\(([^)]+)\)\s*:\s*"([^"]+)"\]', re.IGNORECASE)
        lines = []
        for m in char_re.finditer(raw):
            speaker = m.group(1).strip()
            text    = m.group(2).strip()
            ent     = _world["entities"].get(_loc_id(speaker), {})
            voice   = ent.get("voice", ent_a.get("voice", "am_michael") if speaker == npc_a
                                       else ent_b.get("voice", "am_michael"))
            lines.append({"speaker": speaker, "text": text, "voice": voice})

        if not lines:
            _log(f"[banter] no CHARACTER lines parsed for {npc_a}/{npc_b}")
            return

        item = {"speakers": [npc_a, npc_b], "lines": lines, "area": area}
        _banter_queue.append(item)

        # Persist to conversation history (for future narrator context)
        key = frozenset({npc_a, npc_b})
        _npc_conversations.setdefault(key, []).extend(
            {"speaker": l["speaker"], "text": l["text"], "ts": time.time()}
            for l in lines
        )

        _sse_broadcast("banter_ready", {"speakers": [npc_a, npc_b], "area": area})
        _log(f"[banter] queued {len(lines)}-line conversation between {npc_a} and {npc_b}")

    except Exception as exc:
        _log(f"[banter] generation error: {exc}")
    finally:
        _banter_generating = False


def _banter_prefetch_loop() -> None:
    """Daemon: pre-generate NPC banter when the narrator is idle and ≥2 NPCs are present."""
    global _banter_generating
    while True:
        time.sleep(60)
        try:
            if _banter_generating or _generating or _classifying:
                continue
            if len(_banter_queue) >= 2:
                continue
            npcs = list(_present_npcs.keys())
            if len(npcs) < 2:
                continue
            npc_a, npc_b = npcs[0], npcs[1]
            _banter_generating = True
            threading.Thread(
                target=_generate_npc_banter,
                args=(npc_a, npc_b),
                daemon=True,
                name="banter-gen",
            ).start()
        except Exception as exc:
            _log(f"[banter] prefetch loop error: {exc}")


def _run_ghost_scout(location_name: str) -> None:
    """Background task: generate atmosphere for a hot location before the player arrives.

    Sends a minimal Ollama prompt to get MOOD / GENERATE_IMAGE / SMELL / TOUCH tags,
    then calls flask-sd for any image tags.  Results are stored in
    _location_prefetch_cache[location_name] and served instantly when the player arrives.

    Ghost scout NEVER writes to _narrator_turns, _world current state, _present_npcs,
    or any entity graph — it is a read-only shadow pass.
    """
    global _scout_running
    try:
        _log(f"[ghost-scout] scouting '{location_name}'")
        _sse_broadcast("activity", {"text": f"👁 scouting {location_name}…"})

        # Pull any world.json description for this location as grounding context
        loc_ent = _world["entities"].get(_loc_id(location_name), {})
        loc_desc = loc_ent.get("description", "")
        loc_sd_prompt = loc_ent.get("sd_prompt", "")

        scout_system = (
            "You are a silent observer generating atmosphere data for a game location. "
            "Output ONLY the listed tags — no prose, no character dialogue, no player references. "
            "Do not describe anyone being present. Under 120 tokens total."
        )
        scout_user = (
            f"Generate atmosphere tags for: {location_name}\n"
            + (f"Background: {loc_desc[:200]}\n" if loc_desc else "")
            + "Required tags (each on its own line):\n"
            "[MOOD: \"music tempo, instruments, emotional texture — under 15 words\"]\n"
            "[GENERATE_IMAGE(location): \"sd_prompt — dark sci-fi painterly, no people\"]\n"
            "[SMELL(brief sensory detail)]\n"
            "[TOUCH(brief sensory detail)]"
        )

        msgs = [
            {"role": "system", "content": scout_system},
            {"role": "user",   "content": scout_user},
        ]

        raw = _stream_ollama_chat(msgs, timeout=60.0)
        _log(f"[ghost-scout] '{location_name}' response: {raw[:120]}")

        result: dict = {"images": [], "mood": None, "sense_beats": [], "generated_at": time.time()}

        # Parse MOOD
        m = re.search(r'\[MOOD\s*:\s*"([^"]+)"\]', raw, re.IGNORECASE)
        if m:
            result["mood"] = m.group(1).strip()

        # Parse SMELL / TOUCH → sense beats (held for lore_whisper delivery)
        for tag in ("SMELL", "TOUCH"):
            mt = re.search(rf'\[{tag}\(([^)]+)\)\]', raw, re.IGNORECASE)
            if mt:
                result["sense_beats"].append({"type": tag, "text": mt.group(1).strip()})

        # Parse GENERATE_IMAGE → call flask-sd (only when Ollama is idle)
        img_m = re.search(
            r'\[GENERATE_IMAGE\(location\)\s*:\s*"([^"]+)"\]', raw, re.IGNORECASE
        )
        sd_hint = img_m.group(1).strip() if img_m else loc_sd_prompt
        if sd_hint:
            # Wait for Ollama to be idle before touching the GPU
            deadline = time.time() + 30.0
            while (_generating or _classifying) and time.time() < deadline:
                time.sleep(1)
            if not (_generating or _classifying):
                try:
                    sd_prompt = _SD_STYLE_PREFIX + sd_hint
                    payload = json.dumps({"prompt": sd_prompt, "width": 768, "height": 512}).encode()
                    code, resp_body, _ = _http("POST", f"{FLASK_SD_URL}/api/generate",
                                               body=payload, timeout=120.0)
                    if code == 200:
                        img_url = (json.loads(resp_body) or {}).get("image") or \
                                  (json.loads(resp_body) or {}).get("image_url") or ""
                        if img_url:
                            result["images"].append(img_url)
                            _log(f"[ghost-scout] image ready for '{location_name}'")
                except Exception as exc:
                    _log(f"[ghost-scout] flask-sd failed for '{location_name}': {exc}")

        # Store in cache (evict oldest if full)
        if len(_location_prefetch_cache) >= SCOUT_MAX_CACHE:
            oldest = min(_location_prefetch_cache.items(),
                         key=lambda x: x[1].get("generated_at", 0))
            _location_prefetch_cache.pop(oldest[0], None)
            _log(f"[ghost-scout] evicted '{oldest[0]}' from cache (full)")

        _location_prefetch_cache[location_name] = result

        # Notify client so it can preload the image silently
        payload_sse: dict = {"location": location_name}
        if result["images"]:
            payload_sse["image"] = result["images"][0]
        _sse_broadcast("scout_ready", payload_sse)
        _sse_broadcast("activity", {"text": ""})
        _log(
            f"[ghost-scout] cached '{location_name}' — "
            f"mood={result['mood']}, images={len(result['images'])}, "
            f"beats={len(result['sense_beats'])}"
        )

    except Exception as exc:
        _log(f"[ghost-scout] error for '{location_name}': {exc}")
        _sse_broadcast("activity", {"text": ""})
    finally:
        _scout_running = False


def _ghost_scout_loop() -> None:
    """Wake every 90s; dispatch a ghost scout for the hottest eligible location."""
    global _scout_running
    while True:
        time.sleep(90)
        try:
            # Don't compete with active generation or an in-flight scout
            if _scout_running or _generating or _classifying:
                continue

            current_loc = _world.get("current_location", "")
            current_id  = _loc_id(current_loc) if current_loc else ""
            now = time.time()

            # Find eligible locations: hot, not current, not freshly cached
            eligible = [
                (name, heat) for name, heat in _hook_heat.items()
                if heat >= HOOK_HEAT_THRESHOLD
                and _loc_id(name) != current_id
                and (
                    name not in _location_prefetch_cache
                    or now - _location_prefetch_cache[name].get("generated_at", 0)
                    > SCOUT_CACHE_EXPIRE_SECS
                )
            ]
            if not eligible:
                continue

            target = max(eligible, key=lambda x: x[1])[0]
            _scout_running = True
            threading.Thread(
                target=_run_ghost_scout,
                args=(target,),
                daemon=True,
                name=f"ghost-scout-{target[:20]}",
            ).start()

        except Exception as exc:
            _log(f"[ghost-scout] loop error: {exc}")


def main() -> None:
    _load_fortress_texts()
    _restore_narrator_turns()
    _init_chroma()            # optional — no-op when chromadb not installed
    _chroma_backfill_async()  # seed index from restored turn history
    _replay_world_log()
    _load_seed_world()        # permanent assets; always applied on top of runtime state
    # Index world knowledge for Sorting Hat RAG — runs in background after seed load
    threading.Thread(
        target=_index_world_knowledge,
        daemon=True,
        name="world-knowledge-index",
    ).start()
    # Start lore idle narration daemon — fires after LORE_IDLE_SECS of silence
    threading.Thread(target=_lore_idle_loop, daemon=True, name="lore-idle").start()
    threading.Thread(target=_quirkify_loop, daemon=True, name="quirkify").start()
    # Ollama health watchdog — pings every 30 s; SSE warning after 2 consecutive failures
    threading.Thread(target=_ollama_watchdog_loop, daemon=True, name="ollama-watchdog").start()
    # Ghost scout — speculative pre-generation for hot unvisited locations
    threading.Thread(target=_ghost_scout_loop, daemon=True, name="ghost-scout").start()
    # NPC banter pre-generation ("elevator scenes") — fires when narrator idle + ≥2 NPCs present
    threading.Thread(target=_banter_prefetch_loop, daemon=True, name="banter-prefetch").start()
    _log(f"remnant-diag starting on :{LISTEN_PORT}")
    srv = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
