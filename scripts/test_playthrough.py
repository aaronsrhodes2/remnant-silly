#!/usr/bin/env python3
"""
Remnant integration test — scripted playthrough.

Sends a 9-step scenario through the game API, waits for all models to finish
(narrator + image generation + sense enrichment) before advancing to the next
step, and reports empirical results for each turn.

Usage:
    python scripts/test_playthrough.py [--base http://localhost:1580]

Options:
    --base URL   Nginx base URL (default: http://localhost:1580)
    --no-color   Disable ANSI colour output

Expected outputs per step:
  ✓ Narrator turn received
  ✓ Scene image generated
  ● N sense-enrichment channels broadcast
  ✗ (failure / timeout)
"""

import argparse
import json
import random
import sys
import threading
import time
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# Force UTF-8 output on Windows (cp1252 default doesn't support box-drawing / bullet chars)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Colour helpers ─────────────────────────────────────────────────────────

_USE_COLOR = sys.stdout.isatty()

def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text

def ok(s):   return _c("32", s)   # green
def err(s):  return _c("31", s)   # red
def warn(s): return _c("33", s)   # yellow
def dim(s):  return _c("2", s)    # dim
def bold(s): return _c("1", s)    # bold


# ── Randomised scenario values ─────────────────────────────────────────────

NAMES  = ["Zara", "Kael", "Mira", "Dex", "Nova", "Sable", "Orion", "Vesper", "Lyra", "Cade"]
COLORS = [
    "cobalt blue", "crimson red", "forest green", "deep violet",
    "amber orange", "slate teal", "burnt sienna", "soft gold",
]
FOODS  = [
    "protein synth-steak", "algae noodle soup", "reconstituted chicken curry",
    "hydro-bread with synth-butter", "bean and lentil stew", "mushroom broth",
]


# ── SSE listener ───────────────────────────────────────────────────────────

_events: list[dict] = []
_events_lock = threading.Lock()


def _sse_listener(base: str) -> None:
    """Background thread: connect to /game/events, append typed events to _events."""
    url = f"{base}/game/events"
    try:
        req = Request(url, headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"})
        with urlopen(req) as resp:
            event_type = "message"
            for line_bytes in resp:
                line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith(":"):
                    continue  # keepalive comment
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
        print(f"\n{err('[SSE]')} listener died: {exc}", flush=True)


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
    """Simple GET → dict. Returns {"ok": False, "error": ...} on failure."""
    req = Request(f"{base}{path}", headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Diagnostic endpoint checks ─────────────────────────────────────────────

def _check_diag(base: str) -> dict:
    """
    Cross-check diag endpoints after a step completes.

    Returns:
      narrator_turns_total   int — total turns in /narrator-turns (from diag memory)
      world_entities         int — entity count from /world-state
      scene_image_cached     bool — whether /scene-image has a cached image
      diag_ok                bool — diag responded to all queries
    """
    out = {
        "narrator_turns_total": 0,
        "world_entities": 0,
        "scene_image_cached": False,
        "diag_ok": True,
    }
    # /narrator-turns
    nt = _get(base, "/narrator-turns", timeout=4.0)
    if nt.get("ok") is False:
        out["diag_ok"] = False
    else:
        out["narrator_turns_total"] = nt.get("total_seen", nt.get("count", 0))
    # /world-state
    ws = _get(base, "/world-state", timeout=4.0)
    if ws.get("ok") is False:
        out["diag_ok"] = False
    else:
        out["world_entities"] = ws.get("entity_count", 0)
    # /scene-image
    si = _get(base, "/scene-image", timeout=4.0)
    if si.get("ok") is False:
        # 404 is OK — no image yet
        out["scene_image_cached"] = False
    else:
        out["scene_image_cached"] = bool(si.get("image"))
    return out


# ── Wait logic ─────────────────────────────────────────────────────────────

def _wait_for_idle(
    step_start_idx: int,
    timeout: float = 240.0,
    idle_grace: float = 12.0,
    activity_wait: float = 25.0,
) -> dict:
    """
    Wait until all models have finished processing the current step.

    Completion criteria:
      - At least one narrator (non-player) turn received since step_start_idx
      - AND either:
        a) We saw non-empty activity, which then went quiet for idle_grace seconds, OR
        b) We never saw activity and activity_wait seconds have elapsed since the
           narrator turn (handles very fast responses or races)

    Returns a stats dict:
      ok           bool  — whether a narrator turn was received
      narrator_turns int
      scene_images   int  — distinct scene_image events with image data
      sense_events   int  — distinct sense events with channel + text
      sense_channels list — channel names seen
      elapsed        float — seconds waited
      timed_out      bool
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
    scanned = step_start_idx

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
                idle_for = now - last_nonempty_activity_at
                if idle_for >= idle_grace:
                    break
            elif not saw_any_activity:
                # Never saw activity — assume fast completion
                if (now - narrator_turn_at) >= activity_wait:
                    break

        time.sleep(0.5)

    elapsed = time.time() - t0
    return {
        "ok": narrator_turn_at is not None,
        "narrator_turns": narrator_turns,
        "scene_images": scene_images,
        "sense_events": sense_events,
        "sense_channels": sense_channels,
        "elapsed": elapsed,
        "timed_out": time.time() >= deadline,
    }


# ── Main test runner ───────────────────────────────────────────────────────

def _run_step(
    idx: int,
    total: int,
    label: str,
    message: str,
    base: str,
    expect_image: bool = True,
    is_reset: bool = False,
) -> dict:
    """
    Execute one test step: send the message and wait for completion.
    Returns result dict.
    """
    print(f"\n{bold(f'Step {idx}/{total}')} — {label}")
    print(f"  {dim('→')} Sending: {json.dumps(message)}")

    with _events_lock:
        start_idx = len(_events)

    send_text = message
    result = _post(base, "/player-input", {"text": send_text})
    if not result.get("ok"):
        print(f"  {err('✗')} Send failed: {result.get('error', result)}")
        return {"ok": False, "step": idx, "label": label, "elapsed": 0}

    intent = result.get("intent", "?")
    print(f"  {dim('→')} Intent classified: {intent}")

    # Wait for the pipeline to complete
    print(f"  {dim('→')} Waiting for models to finish (timeout 240s)…", end="", flush=True)
    stats = _wait_for_idle(start_idx)
    elapsed = stats["elapsed"]
    print(f" done ({elapsed:.1f}s)")

    # Primary assertions (SSE stream)
    if stats["ok"]:
        print(f"  {ok('✓')} Narrator turn received ({stats['narrator_turns']} turns)")
    else:
        print(f"  {err('✗')} No narrator turn received")

    if expect_image:
        if stats["scene_images"] > 0:
            print(f"  {ok('✓')} Scene image generated ({stats['scene_images']} image(s))")
        else:
            print(f"  {warn('⚠')} No scene image received (SD may have skipped this turn)")
    else:
        if stats["scene_images"] > 0:
            print(f"  {dim('·')} Scene image: {stats['scene_images']} (not required for this step)")

    if stats["sense_events"] > 0:
        channels = ", ".join(stats["sense_channels"])
        print(f"  {ok('●')} Sense enrichment: {stats['sense_events']} channels — {channels}")
    else:
        print(f"  {dim('·')} Sense enrichment: 0 channels (may be enriched by narrator directly)")

    if stats["timed_out"]:
        print(f"  {err('⚠')} Step timed out — some outputs may be missing")

    # Secondary assertions (diagnostic endpoints cross-check)
    diag = _check_diag(base)
    if diag["diag_ok"]:
        parts = [f"turns in memory={diag['narrator_turns_total']}",
                 f"world entities={diag['world_entities']}",
                 f"scene cached={'yes' if diag['scene_image_cached'] else 'no'}"]
        print(f"  {dim('·')} Diag: {', '.join(parts)}")
    else:
        print(f"  {warn('⚠')} Diag endpoints unresponsive (secondary checks skipped)")

    return {
        "ok": stats["ok"],
        "step": idx,
        "label": label,
        "narrator_turns": stats["narrator_turns"],
        "scene_images": stats["scene_images"],
        "sense_events": stats["sense_events"],
        "sense_channels": stats["sense_channels"],
        "elapsed": elapsed,
        "timed_out": stats["timed_out"],
        "diag_narrator_total": diag["narrator_turns_total"],
        "diag_world_entities": diag["world_entities"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="http://localhost:1580",
                        help="Nginx base URL (default: http://localhost:1580)")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colour output")
    args = parser.parse_args()

    global _USE_COLOR
    if args.no_color:
        _USE_COLOR = False

    base = args.base.rstrip("/")

    # Randomise scenario values
    name  = random.choice(NAMES)
    color = random.choice(COLORS)
    food  = random.choice(FOODS)

    print(bold("\n=== Remnant Integration Test -- Scripted Playthrough ==="))
    print(f"  Base URL : {base}")
    print(f"  Name     : {name}")
    print(f"  Color    : {color}")
    print(f"  Food     : {food}")
    print()

    # Start SSE listener
    t = threading.Thread(target=_sse_listener, args=(base,), daemon=True, name="sse-listener")
    t.start()
    print(dim("  Connecting to SSE stream…"), end="", flush=True)
    time.sleep(1.5)  # give the listener a moment to connect
    print(dim(" connected."))

    # ── Script ────────────────────────────────────────────────────────────

    STEPS = [
        # (label, message, expect_image, is_reset)
        ("Reset world → Fabrication Bay", "reset the world",     True,  True),
        (f"Introduce name: {name}",       f"My name is {name}.", False, False),
        ("Request clothing",
         "I need to put on some clothing.",                       True,  False),
        (f"Order clothing in {color}",
         f"Make me some jeans and a t-shirt and a sweatshirt. "
         f"My favorite color is {color}.",                        True,  False),
        ("Move to galley",
         "I am hungry. Can Sherri take me to the galley for a meal?", True, False),
        (f"Order food: {food}",
         f"I would like {food}.",                                 False, False),
        ("Move to quarters",
         "That was great. Now I'd like to rest. Take me to my quarters.", True, False),
        ("Go to sleep",
         "I go to sleep.",                                        False, False),
        ("Call to adventure",
         "Remnant, let's go on an adventure!",                    True,  False),
    ]

    total = len(STEPS)
    results = []
    overall_start = time.time()

    for i, (label, message, expect_image, is_reset) in enumerate(STEPS, 1):
        r = _run_step(i, total, label, message, base,
                      expect_image=expect_image, is_reset=is_reset)
        results.append(r)

        # Brief pause between steps so the game has a moment to settle
        if i < total:
            time.sleep(2.0)

    # ── Summary ───────────────────────────────────────────────────────────

    total_elapsed = time.time() - overall_start
    steps_ok    = sum(1 for r in results if r["ok"])
    images_ok   = sum(1 for r in results if r.get("scene_images", 0) > 0)
    sense_total = sum(r.get("sense_events", 0) for r in results)
    timeouts    = sum(1 for r in results if r.get("timed_out"))

    print(f"\n{bold('=== Summary ===')}")
    print(f"  Steps completed : {steps_ok}/{total}")
    print(f"  Images received : {images_ok} turns had at least one scene image")
    print(f"  Sense events    : {sense_total} total enrichment channels broadcast")
    print(f"  Timeouts        : {timeouts}")
    print(f"  Total time      : {total_elapsed/60:.1f} min ({total_elapsed:.0f}s)")
    print()
    print(f"  {'Step':<4} {'Label':<40} {'Turns':>5} {'Images':>6} {'Senses':>6} {'Time':>7}")
    print(f"  {'─'*4} {'─'*40} {'─'*5} {'─'*6} {'─'*6} {'─'*7}")
    for r in results:
        status = ok("✓") if r["ok"] else err("✗")
        label  = r["label"][:39]
        turns  = str(r.get("narrator_turns", 0))
        imgs   = str(r.get("scene_images", 0))
        senses = str(r.get("sense_events", 0))
        secs   = f"{r.get('elapsed', 0):.0f}s"
        to_flag = warn(" ⚠timeout") if r.get("timed_out") else ""
        print(f"  {status} {r['step']:<3} {label:<40} {turns:>5} {imgs:>6} {senses:>6} {secs:>7}{to_flag}")

    print()
    sys.exit(0 if steps_ok == total else 1)


if __name__ == "__main__":
    main()
