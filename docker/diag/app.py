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
  FLASK_SD_URL     default http://flask-sd:5000
  OLLAMA_URL       default http://ollama:11434
  SILLYTAVERN_URL  default http://sillytavern:8000
  LISTEN_PORT      default 8700
"""

from __future__ import annotations

import collections
import json
import os
import queue as _queue
import re
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
FLASK_SD_URL = os.environ.get("FLASK_SD_URL", "http://flask-sd:5000").rstrip("/")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434").rstrip("/")
SILLYTAVERN_URL = os.environ.get("SILLYTAVERN_URL", "http://sillytavern:8000").rstrip("/")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8700"))

DIAG_LOG = STATUS_DIR / "diagnostics.log"
NARRATOR_TURNS_LOG = STATUS_DIR / "narrator-turns.jsonl"
WORLD_STATE_LOG = STATUS_DIR / "world-state.jsonl"

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

_TEXT_MODEL_SKIP = ("llava", "embed", "vision", "clip", "moondream", "bakllava")

def _ollama_model() -> str:
    """Return a text-generation Ollama model name, skipping vision/embed models."""
    code, body, _ = _http("GET", f"{OLLAMA_URL}/api/tags", timeout=3.0)
    if code == 200:
        try:
            models = json.loads(body).get("models", [])
            for m in models:
                name = m.get("name", "")
                if not any(skip in name for skip in _TEXT_MODEL_SKIP):
                    return name
        except Exception:
            pass
    return "mistral"


def _sorting_hat(text: str) -> str:
    """Classify player intent as SAY, DO, or SENSE via Ollama. Falls back to heuristics."""
    prompt = (
        "Classify this player input as exactly one of: SAY, DO, SENSE.\n"
        "SAY=speech or dialogue. DO=physical action or movement. SENSE=perception or observation.\n"
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
    # Heuristic fallback — quotes = speech, else action
    if text.startswith(('"', '\u201c', "'")):
        return "SAY"
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


def _store_narrator_turn(turn: dict) -> None:
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

    services = {
        "flask-sd": {"status_file": flask_sd_status, "probe": flask_sd_probe},
        "ollama": {"status_file": ollama_status, "probe": ollama_probe},
        "sillytavern": {"status_file": sillytavern_status, "probe": st_probe},
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
                    "/actions",
                    "/actions/<id> (POST)",
                    "/browser-health (POST)",
                    "/narrator-turn (POST)",
                    "/narrator-turns (GET, ?n=50)",
                    "/world-state (GET, ?type=location|npc|item|player)",
                    "/events (GET, text/event-stream — SSE for game UI)",
                    "/logs/<service> (GET, plain-text)",
                ],
            })
            return
        if path == "/ai.json":
            try:
                self._send_json(200, _build_ai_snapshot())
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

        # Per-service log tail: GET /logs/<service>
        if path.startswith("/logs/"):
            service = path[len("/logs/"):]
            if not service or service not in _KNOWN_SERVICES:
                self._send_json(404, {
                    "error": f"unknown service '{service}'",
                    "known": list(_KNOWN_SERVICES),
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
                _pending_player_input = {"text": wrapped, "intent": intent}
                self._send_json(200, {"ok": True, "intent": intent, "wrapped": wrapped})
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
    _replay_world_log()
    _log(f"remnant-diag starting on :{LISTEN_PORT}")
    srv = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
