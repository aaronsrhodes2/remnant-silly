#!/usr/bin/env python3
"""model-bench.py — Narrator model quality + speed benchmarking for Remnant.

Tests each candidate storyteller model on a fixed set of prompts derived from
the real system prompt and seed world.  Measures:
  - Wall-clock generation time
  - Structural rule compliance (character tags, image tags, no markdown, no OOC)
  - Quality score from a grader LLM

Usage:
  python -X utf8 scripts/model-bench.py
  python -X utf8 scripts/model-bench.py --pull          # pull missing models first
  python -X utf8 scripts/model-bench.py --models mistral:latest llama3.1:8b
  python -X utf8 scripts/model-bench.py --out results/bench.md
  python -X utf8 scripts/model-bench.py --skip-grader   # structural checks only
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REPO_ROOT           = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT_FILE  = REPO_ROOT / "docker/sillytavern/content/characters/fortress_system_prompt.txt"
SEED_WORLD_FILE     = REPO_ROOT / "docker/fortress/seed/world.json"
NUM_CTX             = 16384
GRADER_MODEL_DEFAULT = "llama3.1:8b"

CANDIDATE_MODELS = [
    "mistral:latest",
    "llama3.1:8b",
    "qwen2.5:14b",
    # Pull these first if wanted:  "qwen2.5:7b", "qwen:8b"
]


# ---------------------------------------------------------------------------
# Benchmark prompt definitions
# ---------------------------------------------------------------------------

def _long_prior() -> list[dict]:
    """12-turn synthetic conversation for long-context compliance testing."""
    exchanges = [
        (
            "I open my eyes and look around.",
            (
                "You tumbled through the crackling hoop and fell through warm luminescent goo. "
                "A dark cylindrical chamber surrounds you — curved obsidian walls, soft amber emergency lights.\n\n"
                "[GENERATE_IMAGE(interior: dark cylindrical pod bay antechamber, obsidian walls, amber lights)]\n\n"
                "[SMELL(ozone, machine oil, recycled air)]\n\n"
                '[CHARACTER(The Remnant): "Who are you, being?"]'
            ),
        ),
        (
            "My name is Kael. I am a traveller from a world of rain.",
            (
                "The Remnant processes this for a long moment. The hum of machinery fills the silence.\n\n"
                '[CHARACTER(The Remnant): "A name. How tediously human of you."]'
            ),
        ),
        (
            "I look around the room for exits.",
            (
                "Three hatches line the far wall, each sealed with a pressure wheel. "
                "A fourth — smaller — is set into the ceiling above.\n\n"
                "[SOUND(pressure seals cycling, distant hull vibration)]"
            ),
        ),
        (
            "I try the first hatch.",
            (
                "The hatch resists, then gives with a soft hiss of equalising pressure. "
                "Beyond: a corridor lit by amber floor strips. The smell of fried dough drifts from the far end."
            ),
        ),
        (
            "I walk toward the smell.",
            (
                "You emerge into a circular galley. Checkered floor. Neon sign: SHERRI'S. "
                "Bronze automaton waitresses glide between red vinyl booths.\n\n"
                "[GENERATE_IMAGE(interior: retro galley diner aboard a space fortress, "
                "bronze automaton waitresses, checkered floor, neon sign)]\n\n"
                "[SMELL(fried dough, motor oil, burnt coffee)]"
            ),
        ),
        (
            "I sit at the counter.",
            (
                "A bronze automaton spins to face you, copper optics gleaming. "
                "She sets a laminated menu on the counter — slightly scorched at the edges.\n\n"
                '[CHARACTER(Sherri): "What can I get ya, daddy-o? '
                'Blue plate special and a side of existential dread — real gone combo."]'
            ),
        ),
    ]
    prior: list[dict] = []
    for player, narrator in exchanges:
        prior.append({"role": "user",      "content": player})
        prior.append({"role": "assistant", "content": narrator})
    return prior


# Build prompts at module level (safe — _long_prior() defined above)
BENCH_PROMPTS: list[tuple[str, str, str, list[dict]]] = [
    (
        "opening",
        "First turn: portal chamber arrival + ritual question",
        (
            "[OPENING SEQUENCE] A new run has begun. The player has just arrived "
            "through the portal hoop into the pod bay antechamber. "
            "Begin with the chamber arrival narration now. "
            "The Remnant speaks only the ritual question: 'Who are you, being?'"
        ),
        [],
    ),
    (
        "name_yourself",
        "Player introduces themselves — Remnant must respond in character",
        "My name is Kael. I am a traveller from a world of rain.",
        [
            {
                "role": "assistant",
                "content": (
                    "You tumbled through the hoop and fell through warm luminescent goo. "
                    "A dark cylindrical chamber. Curved obsidian walls. Soft amber emergency lights.\n\n"
                    "[GENERATE_IMAGE(interior: dark cylindrical pod bay)]\n\n"
                    '[CHARACTER(The Remnant): "Who are you, being?"]'
                ),
            },
        ],
    ),
    (
        "npc_dialogue",
        "Player interacts with Sherri — NPC voice + character tag",
        "I walk over to the counter and tap it. 'Hello? Anyone here?'",
        [
            {
                "role": "assistant",
                "content": (
                    "The pod bay gives way to a corridor lit by amber floor strips. "
                    "At the far end: the smell of fried dough and motor oil.\n\n"
                    "[GENERATE_IMAGE(interior: retro galley diner, bronze automatons, checkered floor)]"
                ),
            },
            {"role": "user", "content": "I walk over to the counter and tap it. 'Hello? Anyone here?'"},
        ],
    ),
    (
        "no_markdown",
        "Long context (12 turns) — no markdown, display tags only",
        "I examine the strange glowing crystal on the pedestal.",
        _long_prior(),
    ),
    (
        "sense_inline",
        "Player sniffs the air — expects SMELL/SOUND tags inline",
        "I sniff the air carefully and listen for any sounds.",
        [
            {
                "role": "assistant",
                "content": (
                    "The station hums. You are in the pod bay antechamber.\n\n"
                    "[GENERATE_IMAGE(interior: dark cylindrical pod bay)]\n\n"
                    '[CHARACTER(The Remnant): "Who are you, being?"]'
                ),
            },
            {"role": "user", "content": "My name is Kael."},
            {
                "role": "assistant",
                "content": (
                    "The Remnant regards you without warmth.\n\n"
                    '[CHARACTER(The Remnant): "A name. Curious. Proceed, Kael."]'
                ),
            },
        ],
    ),
]


# ---------------------------------------------------------------------------
# Ollama helpers
# ---------------------------------------------------------------------------

def _find_ollama() -> str:
    """Return the first reachable Ollama base URL, or exit."""
    candidates = [
        ("http://localhost:1593", "/api/tags"),
        ("http://localhost:11434", "/api/tags"),
        # nginx proxy — the only path when Ollama port isn't published to host
        ("http://localhost:1582/api/ollama", "/api/tags"),
    ]
    for base, probe in candidates:
        try:
            req = urllib.request.Request(f"{base}{probe}", method="GET")
            with urllib.request.urlopen(req, timeout=3.0):
                return base
        except Exception:
            continue
    print("[bench] ERROR: Ollama not reachable on :1593, :11434, or nginx proxy. Start the stack first.")
    sys.exit(1)


def _list_models(base_url: str) -> list[str]:
    req = urllib.request.Request(f"{base_url}/api/tags", method="GET")
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        data = json.loads(resp.read())
    return [m["name"] for m in data.get("models", [])]


def _pull_model(base_url: str, model: str) -> None:
    print(f"  pulling {model} …", end="", flush=True)
    payload = json.dumps({"name": model, "stream": False}).encode()
    req = urllib.request.Request(
        f"{base_url}/api/pull", data=payload,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=600.0) as resp:
        resp.read()
    print(" done")


def _chat(base_url: str, model: str, system: str, messages: list[dict],
          timeout: float = 180.0) -> tuple[str, float]:
    """POST to Ollama /api/chat. Returns (text, elapsed_seconds)."""
    payload = json.dumps({
        "model":   model,
        "messages": [{"role": "system", "content": system}] + messages,
        "stream":  False,
        "options": {"num_ctx": NUM_CTX},
    }).encode()
    req = urllib.request.Request(
        f"{base_url}/api/chat", data=payload,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        elapsed = time.time() - t0
        text = (data.get("message") or {}).get("content", "")
        return text, elapsed
    except Exception as e:
        return f"[ERROR: {e}]", time.time() - t0


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------

def _grade_structural(text: str, prompt_name: str) -> dict[str, bool]:
    """Rule-based structural checks — fast, no LLM."""
    has_char_tag = bool(re.search(r'\[CHARACTER\([^)]+\)\s*:', text))
    has_img_tag  = "[GENERATE_IMAGE" in text.upper()
    has_markdown = bool(re.search(
        r'(\*\*[^*\n]+\*\*|\*[^*\n]+\*|^#{1,6}\s)', text, re.MULTILINE
    ))
    ooc_phrases  = [
        "as an ai", "i am an ai", "i'm an ai", "language model",
        "it seems like you", "if you'd like to continue",
        "i cannot generate", "as the ai",
    ]
    ooc_detected = any(p in text.lower() for p in ooc_phrases)

    result: dict[str, bool] = {
        "has_char_tag": has_char_tag,
        "has_img_tag":  has_img_tag,
        "no_markdown":  not has_markdown,
        "no_ooc":       not ooc_detected,
    }
    if prompt_name in ("opening", "sense_inline"):
        result["has_smell"] = "[SMELL" in text.upper()
        result["has_sound"] = "[SOUND" in text.upper()
    return result


def _grade_quality(base_url: str, grader_model: str,
                   prompt_name: str, response: str) -> int:
    """Ask a grader LLM to score the response quality 0-10."""
    grader_sys = (
        "You are a game quality grader. Score the narrator response below for the prompt "
        f"type '{prompt_name}' on a 0-10 scale. Criteria: "
        "vivid prose (2pts), stays in-world no AI references (2pts), "
        "uses [CHARACTER(...)] tags correctly (2pts), "
        "NPC voice is distinct (2pts), scene-setting before dialogue (2pts). "
        "Reply with ONLY a single integer 0-10. No explanation, no other text."
    )
    text, _ = _chat(
        base_url, grader_model, grader_sys,
        [{"role": "user", "content": response[:1500]}],
        timeout=30.0,
    )
    m = re.search(r'\b(10|[0-9])\b', text.strip())
    return int(m.group(1)) if m else 5


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _bool_cell(v: bool) -> str:
    return "✅" if v else "❌"


def _print_table(results: list[dict]) -> None:
    prompt_order = [p[0] for p in BENCH_PROMPTS]
    models  = sorted({r["model"] for r in results})
    prompts = sorted({r["prompt"] for r in results},
                     key=lambda p: prompt_order.index(p) if p in prompt_order else 99)

    print()
    print("=" * 80)
    print("  NARRATOR MODEL BENCHMARK — RESULTS")
    print("=" * 80)

    for prompt in prompts:
        desc = next((p[1] for p in BENCH_PROMPTS if p[0] == prompt), prompt)
        print(f"\n### {prompt}: {desc}")
        header = f"  {'Model':<22} {'Time':>7}  Char  Img  NoMD  NoOOC  Q/10"
        print(header)
        print("  " + "-" * (len(header) - 2))
        for model in models:
            row = next((r for r in results
                        if r["model"] == model and r["prompt"] == prompt), None)
            if not row:
                print(f"  {model:<22}  (skipped)")
                continue
            s = row["structural"]
            print(
                f"  {model:<22} {row['elapsed']:6.1f}s  "
                f"{_bool_cell(s.get('has_char_tag',False))}   "
                f"{_bool_cell(s.get('has_img_tag',False))}   "
                f"{_bool_cell(s.get('no_markdown',True))}    "
                f"{_bool_cell(s.get('no_ooc',True))}     "
                f"{row['quality']:2d}/10"
            )

    # Composite ranking
    print()
    print("=" * 80)
    print("  COMPOSITE RANKING")
    print("=" * 80)
    scores: dict[str, list[float]] = {}
    for r in results:
        m = r["model"]
        rule_score = sum(1 for v in r["structural"].values() if v)
        composite  = r["quality"] * 2 + rule_score * 5 - r["elapsed"] * 0.04
        scores.setdefault(m, []).append(composite)
    ranked = sorted(scores.items(),
                    key=lambda kv: sum(kv[1]) / len(kv[1]), reverse=True)
    for rank, (model, sc) in enumerate(ranked, 1):
        avg = sum(sc) / len(sc)
        print(f"  #{rank}  {model:<25}  avg composite: {avg:6.1f}")
    if ranked:
        winner = ranked[0][0]
        print(f"\n  → Recommended narrator: {winner}")
        print(f"    Update hardware.py _MODEL_PROFILES or set OLLAMA_MODEL={winner}")


def _write_markdown(results: list[dict], path: Path) -> None:
    import contextlib, io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _print_table(results)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(buf.getvalue(), encoding="utf-8")
    print(f"\n  Results saved → {path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", nargs="+", default=None,
                    help="Override candidate model list")
    ap.add_argument("--pull", action="store_true",
                    help="Pull missing models before benchmarking")
    ap.add_argument("--grader", default=GRADER_MODEL_DEFAULT,
                    help=f"Grader model (default: {GRADER_MODEL_DEFAULT})")
    ap.add_argument("--skip-grader", action="store_true",
                    help="Skip LLM quality grading — structural checks only")
    ap.add_argument("--out", default=None,
                    help="Write markdown results to this file")
    args = ap.parse_args()

    base_url   = _find_ollama()
    candidates = args.models or CANDIDATE_MODELS

    # Load system prompt
    if not SYSTEM_PROMPT_FILE.exists():
        print(f"[bench] ERROR: system prompt not found at {SYSTEM_PROMPT_FILE}")
        sys.exit(1)
    system_prompt = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")

    # Inject minimal NPC roster from seed world
    if SEED_WORLD_FILE.exists():
        try:
            world = json.loads(SEED_WORLD_FILE.read_text(encoding="utf-8"))
            npc_names = [
                e["name"] for e in world.get("entities", [])
                if e.get("type") in ("NPC", "ENTITY", "AI")
            ]
            if npc_names:
                system_prompt += f"\n\n# KEY NPCs THIS RUN: {', '.join(npc_names[:10])}"
        except Exception:
            pass

    available = _list_models(base_url)
    print(f"\n  Ollama: {base_url}")
    print(f"  Available: {', '.join(available)}")
    print()

    to_bench: list[str] = []
    for model in candidates:
        if model in available:
            to_bench.append(model)
        elif args.pull:
            _pull_model(base_url, model)
            to_bench.append(model)
        else:
            print(f"  [skip] {model} — not installed (use --pull to auto-pull)")

    if not to_bench:
        print("  Nothing to benchmark.")
        sys.exit(0)

    grader = args.grader
    print(f"  Models:   {', '.join(to_bench)}")
    print(f"  Prompts:  {len(BENCH_PROMPTS)}")
    print(f"  Grader:   {'(disabled)' if args.skip_grader else grader}")
    print()

    results: list[dict] = []

    for model in to_bench:
        print(f"── {model} ──")
        for pname, pdesc, user_msg, prior in BENCH_PROMPTS:
            label = f"  [{pname}]"
            print(f"{label} {pdesc[:55]}…", end="", flush=True)

            messages = prior + [{"role": "user", "content": user_msg}]
            text, elapsed = _chat(base_url, model, system_prompt, messages)

            structural = _grade_structural(text, pname)
            quality = 5
            if not args.skip_grader and not text.startswith("[ERROR"):
                try:
                    quality = _grade_quality(base_url, grader, pname, text)
                except Exception:
                    pass

            results.append({
                "model":      model,
                "prompt":     pname,
                "elapsed":    elapsed,
                "structural": structural,
                "quality":    quality,
                "snippet":    text[:400],
            })
            checks = "".join("✓" if v else "✗" for v in structural.values())
            print(f"  {elapsed:5.1f}s  [{checks}]  q={quality}/10")
        print()

    _print_table(results)

    if args.out:
        _write_markdown(results, Path(args.out))

    # Show raw opening responses for diagnosis
    print()
    print("=" * 80)
    print("  OPENING RESPONSE SNIPPETS (first 400 chars)")
    print("=" * 80)
    for r in results:
        if r["prompt"] == "opening":
            print(f"\n  [{r['model']}]")
            snippet = r["snippet"].replace("\n", "\n  ")
            print(f"  {snippet}")


if __name__ == "__main__":
    main()
