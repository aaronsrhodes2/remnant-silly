"""Narrative test harness for Remnant.

Drives the game via the sidecar API:
  POST /player-input  — submit a player action (classified by Sorting Hat)
  GET  /narrator-turns — poll for new narrator turn
  GET  /world-state    — assert world graph state

Stdlib only. No playwright, no pytest.

Environment variables:
  REMNANT_TEST_NATIVE=1   — enable (stack must be running on :1580/:8700)
  REMNANT_JUDGE=1         — enable Ollama LLM fuzzy assertions (slower)
  REMNANT_TIMEOUT=45      — per-turn response timeout in seconds (default 45)
"""
from __future__ import annotations

import json
import os
import time
import unittest
import urllib.error
import urllib.request
from typing import Optional

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# nginx gateway — /player-input is proxied here (same path as real game UI)
NATIVE_BASE   = "http://localhost:1580"
# Sidecar direct — narrator-turns, world-state (no nginx rewrite needed)
SIDECAR_BASE  = "http://localhost:8700"
# Ollama — used by _judge.py
OLLAMA_BASE   = "http://localhost:11434"

DEFAULT_TIMEOUT = int(os.environ.get("REMNANT_TIMEOUT", "45"))
JUDGE_ENABLED   = os.environ.get("REMNANT_JUDGE") == "1"
NATIVE_ENABLED  = os.environ.get("REMNANT_TEST_NATIVE") == "1"


# ---------------------------------------------------------------------------
# HTTP primitives
# ---------------------------------------------------------------------------

def _http(method: str, url: str, body: Optional[bytes] = None,
          timeout: float = 10.0) -> tuple[int, bytes]:
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json"} if body else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if hasattr(e, "read") else b""
    except Exception as e:
        return 0, str(e).encode()


def _get_json(url: str, timeout: float = 5.0) -> Optional[dict]:
    code, body = _http("GET", url, timeout=timeout)
    if code == 200:
        try:
            return json.loads(body)
        except Exception:
            pass
    return None


def _post_json(url: str, payload: dict, timeout: float = 10.0) -> Optional[dict]:
    body = json.dumps(payload).encode()
    code, resp = _http("POST", url, body=body, timeout=timeout)
    if code == 200:
        try:
            return json.loads(resp)
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# Sidecar helpers
# ---------------------------------------------------------------------------

def get_narrator_turn_count() -> int:
    """Return total narrator turns seen by the sidecar (includes player turns)."""
    d = _get_json(f"{SIDECAR_BASE}/narrator-turns?n=1")
    return (d or {}).get("total_seen", 0)


def get_recent_turns(n: int = 10) -> list[dict]:
    d = _get_json(f"{SIDECAR_BASE}/narrator-turns?n={n}")
    return (d or {}).get("turns", [])


def get_world_state(entity_type: Optional[str] = None) -> dict:
    url = f"{SIDECAR_BASE}/world-state"
    if entity_type:
        url += f"?type={entity_type}"
    return _get_json(url) or {}


def post_player_input(text: str) -> dict:
    """Submit player input through nginx (same path the /game/ UI uses)."""
    result = _post_json(f"{NATIVE_BASE}/player-input", {"text": text}, timeout=15.0)
    return result or {}


# ---------------------------------------------------------------------------
# Core: play a turn and wait for the narrator to respond
# ---------------------------------------------------------------------------

def play_turn(text: str, timeout: int = DEFAULT_TIMEOUT) -> tuple[dict, dict]:
    """Submit player input and wait for a non-player narrator turn.

    Returns (player_result, narrator_turn) where:
      - player_result: the POST /player-input response (intent, wrapped text)
      - narrator_turn: the narrator's response turn dict

    Raises AssertionError on timeout.
    """
    before_count = get_narrator_turn_count()

    player_result = post_player_input(text)
    if not player_result.get("ok"):
        raise AssertionError(f"POST /player-input failed for '{text}': {player_result}")

    # Poll for a new non-player narrator turn
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        turns = get_recent_turns(n=10)
        for t in reversed(turns):
            if t.get("is_player"):
                continue
            if t.get("turn_id", "").startswith("player-"):
                continue
            # Verify this turn arrived after our submission by checking total_seen
            current_count = get_narrator_turn_count()
            if current_count > before_count:
                return player_result, t
        time.sleep(1.0)

    raise AssertionError(
        f"No narrator response to '{text}' within {timeout}s "
        f"(turns before={before_count}, after={get_narrator_turn_count()})"
    )


# ---------------------------------------------------------------------------
# Structural assertions (deterministic — no LLM needed)
# ---------------------------------------------------------------------------

def assert_turn_has_content(turn: dict) -> None:
    assert turn.get("raw_text"), "narrator turn has empty raw_text"
    blocks = turn.get("parsed_blocks") or []
    narrator_blocks = [b for b in blocks if not b.get("isPlayer")]
    assert narrator_blocks, "narrator turn has no narrator-authored blocks"


def assert_no_player_impersonation(turn: dict) -> None:
    warnings = turn.get("warnings") or []
    bad = [w for w in warnings if w.get("code") == "player_impersonation"]
    assert not bad, f"narrator impersonated the player: {bad}"


def assert_markers_present(turn: dict, *expected: str) -> None:
    """Assert that all expected marker names appear in markers_found."""
    found = set(m.upper() for m in (turn.get("markers_found") or []))
    missing = [m for m in expected if m.upper() not in found]
    assert not missing, (
        f"expected markers {missing} not found; got {sorted(found)}"
    )


def assert_world_entity(name_fragment: str,
                        entity_type: Optional[str] = None) -> dict:
    """Assert a world-graph entity exists whose name contains name_fragment.

    Returns the matching entity dict.
    """
    state = get_world_state(entity_type)
    entities = state.get("entities") or []
    needle = name_fragment.lower()
    for ent in entities:
        if needle in ent.get("canonical_name", "").lower():
            return ent
        for alias in (ent.get("aliases") or []):
            if needle in alias.get("name", "").lower():
                return ent
    raise AssertionError(
        f"no world entity matching '{name_fragment}' "
        f"(type_filter={entity_type}, total_entities={len(entities)})"
    )


def assert_min_world_entities(entity_type: str, minimum: int) -> None:
    state = get_world_state(entity_type)
    entities = state.get("entities") or []
    count = len(entities)
    assert count >= minimum, (
        f"expected >= {minimum} entities of type='{entity_type}', got {count}"
    )


def assert_intent(player_result: dict, expected: str) -> None:
    """Assert Sorting Hat classified the intent correctly."""
    actual = (player_result.get("intent") or "").upper()
    assert actual == expected.upper(), (
        f"expected Sorting Hat intent={expected}, got={actual} "
        f"(wrapped='{player_result.get('wrapped')}')"
    )


# ---------------------------------------------------------------------------
# Base test class
# ---------------------------------------------------------------------------

class NarrativeTestCase(unittest.TestCase):
    """Base class for narrative scenario tests.

    Skips itself when REMNANT_TEST_NATIVE != 1, or sidecar is unreachable.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if not NATIVE_ENABLED:
            raise unittest.SkipTest(
                "set REMNANT_TEST_NATIVE=1 to run narrative tests"
            )
        d = _get_json(f"{SIDECAR_BASE}/", timeout=3.0)
        if not d or d.get("service") != "remnant-diag":
            raise unittest.SkipTest(
                "sidecar not reachable on :8700 — is the native stack running?"
            )

    def play(self, text: str, timeout: int = DEFAULT_TIMEOUT) -> tuple[dict, dict]:
        """Play one turn: post input, wait for response, auto-assert quality."""
        player_result, turn = play_turn(text, timeout=timeout)
        assert_turn_has_content(turn)
        assert_no_player_impersonation(turn)
        return player_result, turn

    # Static helpers exposed as methods for natural test authoring
    assert_markers_present    = staticmethod(assert_markers_present)
    assert_world_entity       = staticmethod(assert_world_entity)
    assert_min_world_entities = staticmethod(assert_min_world_entities)
    assert_intent             = staticmethod(assert_intent)
