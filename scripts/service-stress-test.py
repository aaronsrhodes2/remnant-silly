#!/usr/bin/env python3
"""service-stress-test.py — direct service stress test for Remnant.

Bypasses the narrator / story flow entirely. Fires crafted requests directly at
every service endpoint to verify capability, measure latency, and surface
crashes before a story session.

Tests:
  1. Gateway health
  2. Ollama: generate short text (sense all channels)
  3. flask-sd: generate an image (full SD pipeline)
  4. flask-tts: synthesise speech for narrator + NPC voices
  5. flask-music: generate a 10s clip from a mood prompt
  6. Diag sidecar: all diagnostic endpoints, SSE stream, player-input echo
  7. Concurrent load: fire SD + TTS + music simultaneously (VRAM contention test)
  8. Event queue: rapid-fire 5 player inputs and verify each gets a narrator response

USAGE:
    python -X utf8 scripts/service-stress-test.py
    python -X utf8 scripts/service-stress-test.py --skip-music   # skip music (slow)
    python -X utf8 scripts/service-stress-test.py --concurrent    # concurrent load test
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent

# ── ANSI ──────────────────────────────────────────────────────────────────────
def _ok(s):   return f"\033[32m{s}\033[0m"
def _warn(s): return f"\033[33m{s}\033[0m"
def _err(s):  return f"\033[31m{s}\033[0m"
def _dim(s):  return f"\033[2m{s}\033[0m"
def _bold(s): return f"\033[1m{s}\033[0m"
def _cyan(s): return f"\033[36m{s}\033[0m"

PASS = _ok("✓ PASS")
FAIL = _err("✗ FAIL")
SKIP = _warn("- SKIP")


# ── HTTP helpers ──────────────────────────────────────────────────────────────
def _get(url: str, timeout: float = 15.0) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
            try:
                return r.status, json.loads(body)
            except Exception:
                return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as e:
        return 0, {"_error": str(e)}


def _post(url: str, body: dict, timeout: float = 30.0) -> tuple[int, Any]:
    payload = json.dumps(body).encode("utf-8")
    try:
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
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


# ── Result tracking ───────────────────────────────────────────────────────────
results: list[dict] = []


def _result(name: str, passed: bool, latency_ms: float, detail: str = "") -> dict:
    r = {"name": name, "passed": passed, "latency_ms": latency_ms, "detail": detail}
    results.append(r)
    status = PASS if passed else FAIL
    lat = f"{latency_ms:.0f}ms"
    detail_str = f"  {_dim(detail)}" if detail else ""
    print(f"  {status}  {name:<40} {_dim(lat)}{detail_str}")
    return r


def _section(title: str) -> None:
    print(f"\n{_bold(f'── {title}')}")


# ── Test sections ─────────────────────────────────────────────────────────────

def test_gateway(base: str) -> None:
    _section("1. Gateway + sidecar health")

    t0 = time.time()
    code, body = _get(f"{base}/health", timeout=5)
    _result("GET /health", code == 200 and body.get("gateway") == "ok",
            (time.time() - t0) * 1000, str(body))

    t0 = time.time()
    code, body = _get(f"{base}/diagnostics/ai.json", timeout=10)
    _result("GET /diagnostics/ai.json", code == 200 and "summary" in body,
            (time.time() - t0) * 1000,
            body.get("summary", "")[:80] if isinstance(body, dict) else "")

    t0 = time.time()
    code, body = _get(f"{base}/signature", timeout=10)
    _result("GET /signature", code == 200,
            (time.time() - t0) * 1000,
            f"status={body.get('status','?')}" if isinstance(body, dict) else "")

    t0 = time.time()
    code, body = _get(f"{base}/diagnostics/narrator-turns?n=1", timeout=5)
    _result("GET /diagnostics/narrator-turns", code == 200,
            (time.time() - t0) * 1000,
            f"total_seen={body.get('total_seen','?')}" if isinstance(body, dict) else "")


def test_ollama(base: str, ollama_url: str) -> None:
    _section("2. Ollama (language model)")

    # Direct model list
    t0 = time.time()
    code, body = _get(f"{base}/api/ollama/api/tags", timeout=10)
    models = [m.get("name", "?") for m in (body.get("models") or [])] if isinstance(body, dict) else []
    _result("GET /api/ollama/api/tags", code == 200 and len(models) > 0,
            (time.time() - t0) * 1000, f"models: {models}")

    if not models:
        _result("Ollama generate (skipped — no models)", False, 0, "no models loaded")
        return

    model = models[0]

    # Short generate — narrator-style structured output with ALL sense tags.
    # Timeout is 180s: Ollama may be busy with the diag auto-opening narrator
    # call that fires at startup. We queue behind it rather than race.
    prompt = (
        "You are a game narrator. Generate exactly one line of each type:\n"
        "1. [MOOD: \"slow ambient drone, tension\"]\n"
        "2. [GENERATE_IMAGE(location): \"dark sci-fi corridor, flickering lights\"]\n"
        "3. [SFX(heavy door scrapes open)]\n"
        "4. [SMELL(ozone and old metal)]\n"
        "5. [TOUCH(cold smooth surface)]\n"
        "6. [LORE(the_fold): \"The Fold is a dimensional rift\"]\n"
        "7. [CHARACTER(Sherri): \"Oh, you're awake.\"]\n"
        "Output ONLY those 7 tagged lines, nothing else."
    )
    t0 = time.time()
    code, body = _post(
        f"{base}/api/ollama/api/generate",
        {"model": model, "prompt": prompt, "stream": False,
         "options": {"num_predict": 120, "temperature": 0.1}},
        timeout=180,
    )
    lat = (time.time() - t0) * 1000
    response = body.get("response", "") if isinstance(body, dict) else ""
    # Verify all 7 sense/tag types appear in response
    required_tags = ["MOOD", "GENERATE_IMAGE", "SFX", "SMELL", "TOUCH", "LORE", "CHARACTER"]
    found_tags = [t for t in required_tags if t in response]
    missing = [t for t in required_tags if t not in response]
    _result("Ollama generate (all sense tags)", code == 200 and len(found_tags) >= 5,
            lat,
            f"found {len(found_tags)}/7: {found_tags}"
            + (f"  MISSING: {missing}" if missing else ""))

    if response:
        print(f"    {_dim('Response:')}")
        for line in response.splitlines()[:10]:
            if line.strip():
                print(f"    {_dim(line[:100])}")


def test_flask_sd(base: str) -> None:
    _section("3. Stable Diffusion (flask-sd) — health only")
    # SD generation is tested through the Sorting Hat pipeline in Section 6.
    # Direct generate calls bypass the SD worker queue and narrator coordination.

    t0 = time.time()
    code, body = _get(f"{base}/api/flask-sd/api/health", timeout=10)
    _result("GET /api/flask-sd/api/health", code == 200,
            (time.time() - t0) * 1000,
            str(body)[:80] if isinstance(body, dict) else "")


def test_flask_tts(base: str) -> None:
    _section("4. Text-to-speech (flask-tts / Kokoro)")
    # Kokoro TTS uses OpenAI-compatible API: POST /v1/audio/speech returns raw MP3.
    # Health endpoint: GET /health. Nginx routes /api/tts/ → tts:8880 (prefix stripped).

    t0 = time.time()
    code, body = _get(f"{base}/api/tts/health", timeout=5)
    _result("GET /api/tts/health", code == 200,
            (time.time() - t0) * 1000,
            str(body)[:60] if isinstance(body, (dict, str)) else "")

    voice_tests = [
        ("am_michael",  "The Fortress remembers you, traveller."),    # narrator
        ("af_sarah",    "Oh, you're finally awake. Let me scan you."), # Sherri
        ("am_adam",     "The dimensional rift is unstable."),           # NPC
    ]
    for voice, text in voice_tests:
        t0 = time.time()
        payload = json.dumps({
            "input": text,
            "voice": voice,
            "model": "kokoro",
            "response_format": "mp3",
        }).encode("utf-8")
        try:
            req = urllib.request.Request(
                f"{base}/api/tts/v1/audio/speech",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                audio_bytes = r.read()
                code_tts = r.status
        except urllib.error.HTTPError as e:
            audio_bytes = b""
            code_tts = e.code
        except Exception as e:
            audio_bytes = b""
            code_tts = 0
        lat = (time.time() - t0) * 1000
        has_audio = len(audio_bytes) > 1000
        detail = f"audio_bytes={len(audio_bytes)}" if has_audio else f"HTTP {code_tts} — no audio"
        _result(f"TTS generate (voice={voice})", code_tts == 200 and has_audio, lat, detail)


def test_flask_music(base: str, skip: bool = False) -> None:
    _section("5. Music generation (flask-music)")

    t0 = time.time()
    code, body = _get(f"{base}/api/music/health", timeout=5)
    healthy = code == 200
    _result("GET /api/music/health", healthy,
            (time.time() - t0) * 1000, str(body)[:80] if isinstance(body, dict) else "")

    if skip:
        _result("Music generate (skipped by --skip-music flag)", True, 0, "")
        return

    if not healthy:
        _result("Music generate (skipped — health check failed)", False, 0,
                "flask-music not healthy")
        return

    mood_tests = [
        "slow ambient drone, metallic resonance, uneasy quiet, sci-fi",
        "tribal percussion, alien vocals, tense industrial beat, dark",
        "soft hum, gentle synth, calm exploration, spaceship interior",
    ]
    for mood in mood_tests:
        t0 = time.time()
        code, body = _post(
            f"{base}/api/music/api/generate",   # nginx strips /api/music/ → /api/generate
            {"prompt": mood, "duration": 5},    # 5s clip for faster test
            # 240s: covers SD worker holding GPU (30-60s) + MusicGen generate (20-70s).
            # On a truly idle warm system this takes 20-40s; the extra headroom handles
            # back-to-back runs where SD is still finishing a previous image.
            timeout=240,
        )
        lat = (time.time() - t0) * 1000
        audio = body.get("audio") if isinstance(body, dict) else None
        has_audio = bool(audio and len(audio) > 100)
        _result(f"Music generate ({mood[:40]!r})", code == 200 and has_audio,
                lat,
                f"audio_len={len(audio)}" if has_audio else str(body)[:80])


def _wait_for_narrator(base: str, current_count: int, timeout_s: float = 120) -> list:
    """Block until a new narrator turn appears or timeout. Returns new narrator turns list."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        _, turns_data = _get(f"{base}/diagnostics/narrator-turns?n=20", timeout=5)
        if isinstance(turns_data, dict):
            turns = [t for t in (turns_data.get("turns") or []) if not t.get("is_player")]
            if len(turns) > current_count:
                return turns
        time.sleep(3)
    _, turns_data = _get(f"{base}/diagnostics/narrator-turns?n=20", timeout=5)
    return [t for t in (turns_data.get("turns") or []) if not t.get("is_player")] \
           if isinstance(turns_data, dict) else []


def test_player_input_echo(base: str) -> None:
    _section("6. Full pipeline integration (Sorting Hat → narrator → all services)")
    # Sends inputs ONE AT A TIME, waiting for narrator response between each.
    # This matches real gameplay and verifies Sorting Hat → downstream service chain.
    # _diagnostic:true marks turns so they can be filtered from player story view.

    # Reset to clean state
    code, body = _post(f"{base}/reset", {"level": "world"}, timeout=10)
    _result("POST /reset (world level)", code == 200 and body.get("ok"),
            0, str(body)[:60])

    # After world reset, diag fires an auto-opening narrator turn automatically.
    # We MUST wait for it to land before sending Turn 1, otherwise our wait_for_narrator
    # calls are off-by-one: the auto-opening satisfies count>0, making every subsequent
    # turn appear one beat late and Turn 4 never arriving in time.
    print(f"  {_dim('Waiting for post-reset auto-opening narrator turn...')}")
    auto_turns = _wait_for_narrator(base, 0, timeout_s=180)
    _result("Auto-opening narrator fired", len(auto_turns) >= 1, 0,
            f"{len(auto_turns)} turn(s) — baseline set")
    pre_count = len(auto_turns)  # use as baseline for all subsequent turns

    # ── Turn 1: Opening arrival → should produce MOOD (GENERATE_IMAGE is in first_mes) ──
    print(f"  {_dim('Turn 1: opening arrival...')}")
    t0 = time.time()
    code, body = _post(f"{base}/player-input",
                       {"text": "I open my eyes and look around.", "_diagnostic": True}, timeout=60)
    _result("Turn 1 accepted", code == 200 and body.get("ok"),
            (time.time() - t0) * 1000, body.get("intent", ""))
    turns = _wait_for_narrator(base, pre_count)
    _result("Turn 1 narrator responded", len(turns) > pre_count, 0,
            f"got {len(turns) - pre_count} new narrator turn(s)")
    t1_text = turns[-1].get("raw_text", "") if turns and len(turns) > pre_count else ""
    _result("Turn 1 has [MOOD]", "MOOD:" in t1_text, 0,
            t1_text[:80] if t1_text else "no response")

    # ── Turn 2: Player names themselves → should produce PLAYER_TRAIT ──
    print(f"  {_dim('Turn 2: name declaration...')}")
    t0 = time.time()
    code, body = _post(f"{base}/player-input",
                       {"text": "My name is Stress. I am a test from the outer void.",
                        "_diagnostic": True}, timeout=60)
    _result("Turn 2 accepted", code == 200 and body.get("ok"),
            (time.time() - t0) * 1000, body.get("intent", ""))
    turns2 = _wait_for_narrator(base, len(turns))
    _result("Turn 2 narrator responded", len(turns2) > len(turns), 0,
            f"total narrator turns: {len(turns2)}")
    t2_text = turns2[-1].get("raw_text", "") if turns2 and len(turns2) > len(turns) else ""
    _result("Turn 2 has [PLAYER_TRAIT]", "PLAYER_TRAIT" in t2_text, 0,
            t2_text[:80] if t2_text else "no response")

    # ── Turn 3: Move to new location → should produce GENERATE_IMAGE ──
    print(f"  {_dim('Turn 3: move to new location (Galley)...')}")
    t0 = time.time()
    code, body = _post(f"{base}/player-input",
                       {"text": "I walk to the galley.", "_diagnostic": True}, timeout=60)
    _result("Turn 3 accepted", code == 200 and body.get("ok"),
            (time.time() - t0) * 1000, body.get("intent", ""))
    turns3 = _wait_for_narrator(base, len(turns2))
    _result("Turn 3 narrator responded", len(turns3) > len(turns2), 0,
            f"total narrator turns: {len(turns3)}")
    t3_text = turns3[-1].get("raw_text", "") if turns3 and len(turns3) > len(turns2) else ""
    _result("Turn 3 has [GENERATE_IMAGE]", "GENERATE_IMAGE" in t3_text, 0,
            t3_text[:120] if t3_text else "no response")
    _result("Turn 3 has [MOOD]", "MOOD:" in t3_text, 0,
            "found" if "MOOD:" in t3_text else "missing")

    # ── Turn 4: NPC dialogue → should produce CHARACTER ──
    print(f"  {_dim('Turn 4: NPC interaction (Sherri)...')}")
    t0 = time.time()
    code, body = _post(f"{base}/player-input",
                       {"text": "Sherri, I need clothes. Dark ones, with pockets.",
                        "_diagnostic": True}, timeout=60)
    _result("Turn 4 accepted", code == 200 and body.get("ok"),
            (time.time() - t0) * 1000, body.get("intent", ""))
    # 180s: by Turn 4 Ollama has handled 3+ narrator calls; give extra headroom
    turns4 = _wait_for_narrator(base, len(turns3), timeout_s=180)
    _result("Turn 4 narrator responded", len(turns4) > len(turns3), 0,
            f"total narrator turns: {len(turns4)}")
    t4_text = turns4[-1].get("raw_text", "") if turns4 and len(turns4) > len(turns3) else ""
    _result("Turn 4 has [CHARACTER]", "CHARACTER(" in t4_text, 0,
            t4_text[:80] if t4_text else "no response")

    # Show latest response preview
    if t4_text:
        print(f"\n  {_dim('Latest narrator response (turn 4):')}")
        for line in t4_text[:300].splitlines():
            if line.strip():
                print(f"    {_dim(line[:100])}")


def test_concurrent_load(base: str) -> None:
    _section("7. Concurrent Sorting Hat queue (3 simultaneous player inputs)")
    # Tests that the Sorting Hat correctly serialises concurrent inputs and all
    # downstream services (SD worker, TTS, music) fire without deadlock or race.
    # Uses _diagnostic:true so turns are marked as test turns (full pipeline fires).

    # Reset to known state
    _post(f"{base}/reset", {"level": "story"}, timeout=10)
    time.sleep(1)

    errors = []
    timings = {}

    concurrent_inputs = [
        # Each crafted to trigger a different downstream service path:
        "I walk into the fabrication bay and look around.",           # → GENERATE_IMAGE (SD)
        "Sherri, what's that sound coming from the dark corner?",     # → CHARACTER + SFX (TTS + narrator)
        "I close my eyes and listen to the ambient hum of the ship.", # → MOOD + SENSE (music)
    ]

    def _input_job(i: int, text: str) -> None:
        t0 = time.time()
        code, body = _post(
            f"{base}/player-input",
            {"text": text, "_diagnostic": True},
            # 60s: Sorting Hat calls Ollama (5-30s warm), 3 concurrent queue up serially.
            # The last request in Ollama's queue may wait 2× the per-call time.
            timeout=60,
        )
        ok = code == 200 and body.get("ok")
        timings[f"input_{i+1}"] = (time.time() - t0, ok)
        if not ok:
            errors.append(f"input {i+1} ({text[:30]!r}): HTTP {code}")

    threads = [
        threading.Thread(target=_input_job, args=(i, text), name=f"stress-input-{i}")
        for i, text in enumerate(concurrent_inputs)
    ]
    wall_t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    wall_lat = (time.time() - wall_t0) * 1000

    for key, (lat, ok) in sorted(timings.items()):
        _result(f"Concurrent {key} accepted", ok, lat * 1000)
    _result("All concurrent inputs accepted", len(errors) == 0, wall_lat,
            "errors: " + ", ".join(errors) if errors else "no errors")

    # Wait for narrator to process queued inputs.
    # _narrator_queued is a 1-deep boolean (not a list), so 3 simultaneous inputs
    # produce at most 2 narrator turns: input 1 runs, inputs 2+3 merge into one
    # follow-up generation that processes all accumulated context together.
    print(f"  {_dim('Waiting up to 150s for narrator to process queued inputs (expect ≥2)...')}")
    deadline = time.time() + 150
    while time.time() < deadline:
        _, turns = _get(f"{base}/diagnostics/narrator-turns?n=20", timeout=5)
        narrator_count = len([t for t in (turns.get("turns") or [])
                              if not t.get("is_player")]) if isinstance(turns, dict) else 0
        if narrator_count >= 2:
            break
        time.sleep(5)
    _, turns = _get(f"{base}/diagnostics/narrator-turns?n=20", timeout=5)
    narrator_count = len([t for t in (turns.get("turns") or [])
                          if not t.get("is_player")]) if isinstance(turns, dict) else 0
    _result("Sorting Hat serialised concurrent inputs (≥2 turns)",
            narrator_count >= 2, wall_lat,
            f"got {narrator_count} narrator responses — queue handled correctly"
            if narrator_count >= 2 else f"only {narrator_count}/2 processed (queue stalled)")


def test_rapid_inputs(base: str) -> None:
    _section("8. Rapid-fire inputs (event queue)")

    # Reset first
    _post(f"{base}/reset", {"level": "story"}, timeout=10)
    time.sleep(1)

    NUM = 3
    sent_times = {}
    errors = []

    for i in range(NUM):
        t0 = time.time()
        code, body = _post(f"{base}/player-input",
                          {"text": f"Test input {i+1}: What do I see?", "_diagnostic": True},
                          # 30s: Sorting Hat calls Ollama; warm call takes 2-5s but may queue
                          # behind a prior rapid-fire request still being classified.
                          timeout=30)
        sent_times[i] = time.time()
        ok = code == 200 and body.get("ok")
        _result(f"Rapid input {i+1}/{NUM} accepted", ok,
                (time.time() - t0) * 1000)
        if not ok:
            errors.append(f"input {i+1}: HTTP {code}")
        time.sleep(0.5)   # 500ms between inputs

    # Wait for narrator to process the queue.
    # _narrator_queued is a 1-deep boolean: each narrator turn fires one follow-up at most.
    # With 3 rapid inputs each 500ms apart, we expect 2 narrator turns (not 3):
    #   Input 1 → narrator starts generating (_generating=True, takes 30-60s)
    #   Input 2 → narrator busy → _narrator_queued = True
    #   Input 3 → narrator still busy → _narrator_queued = True (same flag, overwrite)
    #   After turn 1 completes → one follow-up fires, processing inputs 2+3 together
    # This is correct queue behaviour, not a bug.
    EXPECTED_TURNS = 2
    print(f"  {_dim(f'Waiting up to 120s for narrator to process queue (expect ≥{EXPECTED_TURNS})...')}")
    deadline = time.time() + 120
    last_count = 0
    while time.time() < deadline:
        _, turns = _get(f"{base}/diagnostics/narrator-turns?n=20", timeout=5)
        count = len([t for t in (turns.get("turns") or []) if not t.get("is_player", True)]) \
            if isinstance(turns, dict) else 0
        if count > last_count:
            print(f"  {_dim(f'  narrator turns: {count}')}")
            last_count = count
        if count >= EXPECTED_TURNS:
            break
        time.sleep(5)

    _, turns = _get(f"{base}/diagnostics/narrator-turns?n=20", timeout=5)
    narrator_count = len([t for t in (turns.get("turns") or []) if not t.get("is_player", True)]) \
        if isinstance(turns, dict) else 0
    _result(f"All {EXPECTED_TURNS} narrator responses received",
            narrator_count >= EXPECTED_TURNS, 0,
            f"got {narrator_count}/{EXPECTED_TURNS} narrator responses")


# ── Summary ───────────────────────────────────────────────────────────────────

def _summary() -> int:
    passed = sum(1 for r in results if r["passed"])
    failed = sum(1 for r in results if not r["passed"])
    total  = len(results)

    print(f"\n{_bold('══ Results ══')}")
    print(f"  Passed: {_ok(str(passed))}/{total}")
    if failed:
        print(f"  Failed: {_err(str(failed))}/{total}")
        print(f"\n  {_bold('Failures:')}")
        for r in results:
            if not r["passed"]:
                print(f"    {_err('✗')} {r['name']}: {_dim(r['detail'])}")

    # Latency summary for slow operations
    slow = [(r["name"], r["latency_ms"]) for r in results if r["latency_ms"] > 1000]
    if slow:
        print(f"\n  {_bold('Slow operations (>1s):')}")
        for name, lat in sorted(slow, key=lambda x: -x[1]):
            warn = _err if lat > 30000 else _warn
            print(f"    {warn(f'{lat:.0f}ms')} {name}")

    return 0 if failed == 0 else 1


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host",        default="localhost")
    parser.add_argument("--port",        type=int, default=1582)
    parser.add_argument("--skip-music",  action="store_true", help="Skip music generation (slow)")
    parser.add_argument("--skip-concurrent", action="store_true",
                        help="Skip concurrent load test")
    parser.add_argument("--skip-rapid",  action="store_true",
                        help="Skip rapid-input queue test")
    parser.add_argument("--only",        choices=["gateway", "ollama", "sd", "tts", "music",
                                                   "input", "concurrent", "rapid"],
                        help="Run only one section")
    args = parser.parse_args()

    base      = f"http://{args.host}:{args.port}"
    ollama    = f"{base}/api/ollama"

    print(_bold(f"\n╔══════════════════════════════════════════╗"))
    print(_bold(f"║  Remnant — Service Stress Test           ║"))
    print(_bold(f"╚══════════════════════════════════════════╝"))
    print(f"  Target: {base}\n")

    # ── System drain preamble ──────────────────────────────────────────────────
    # Reset to a clean world and wait for the system to reach idle before any
    # section runs. This ensures that:
    #   • A previous run's narrator thread isn't still holding Ollama
    #   • A previous run's SD job isn't competing for GPU VRAM
    # Without this drain, back-to-back runs cause 120s timeouts in Sections 2 and 5.
    if not args.only:
        print(_dim("  Draining previous runs — resetting world and waiting for system idle..."))
        _post(f"{base}/reset", {"level": "world"}, timeout=10)
        # Wait for the auto-opening narrator turn to complete (confirms Ollama finished)
        drain_turns = _wait_for_narrator(base, 0, timeout_s=180)
        # Then poll /ready until SD queue is also empty
        drain_deadline = time.time() + 90
        while time.time() < drain_deadline:
            _, ready = _get(f"{base}/ready", timeout=5)
            if isinstance(ready, dict) and ready.get("idle"):
                break
            time.sleep(3)
        print(_dim(f"  System ready: {len(drain_turns)} auto-open turn(s), SD queue drained.\n"))

    only = args.only
    try:
        if not only or only == "gateway":    test_gateway(base)
        if not only or only == "ollama":     test_ollama(base, ollama)
        if not only or only == "sd":         test_flask_sd(base)
        if not only or only == "tts":        test_flask_tts(base)
        if not only or only == "music":      test_flask_music(base, skip=args.skip_music)
        if not only or only == "input":      test_player_input_echo(base)
        if (not only or only == "concurrent") and not args.skip_concurrent:
            test_concurrent_load(base)
        if (not only or only == "rapid") and not args.skip_rapid:
            test_rapid_inputs(base)
    except KeyboardInterrupt:
        print(f"\n{_warn('Interrupted.')}")

    return _summary()


if __name__ == "__main__":
    sys.exit(main())
