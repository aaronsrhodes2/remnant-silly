#!/usr/bin/env python3
"""narrator-bench.py — Quantified comparison benchmark for remnant-narrator vs baseline.

Measures speed, tag compliance, narrative richness, and prose creativity across
a fixed set of prompts.  Designed to run before/after LoRA fine-tuning or model
swaps and produce a diff-able score table.

Metrics captured per model per prompt:
  Speed       — TTFT (ms), tokens/sec, total latency (s)
  Compliance  — MOOD present %, CHARACTER format %, no-player-action %, no-OOC %
  Richness    — sense tags/turn, lore tags, images, SFX, INTRODUCE tags
  Creativity  — type-token ratio (TTR), avg prose sentence length, vocab size

Usage:
  python -X utf8 scripts/narrator-bench.py
  python -X utf8 scripts/narrator-bench.py --models remnant-narrator:latest qwen2.5:14b
  python -X utf8 scripts/narrator-bench.py --ollama http://localhost:11434
  python -X utf8 scripts/narrator-bench.py --out results/narrator-bench-$(date +%F).md
  python -X utf8 scripts/narrator-bench.py --json-out results/narrator-bench.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
NUM_CTX   = 4096
NUM_PRED  = 450

# ---------------------------------------------------------------------------
# Narrator system prompt (mirrored from training script to ensure fair eval)
# ---------------------------------------------------------------------------

SYSTEM = (
    "You are THE FORTRESS OF ETERNAL SENTINEL — the ancient, sardonic narrator of "
    "'The Remnant,' a dark sci-fi interactive story. You ARE the narrator. "
    "Write in second-person present tense always. 'You/your' for the player. "
    "Never use the player's proper name in prose — only in image prompts or when an NPC says it.\n\n"
    "DUAL VOICE — two presences share this mind:\n"
    "The Fortress (you): narrator prose + [CHARACTER(The Fortress): \"...\"] for direct speech. "
    "Ancient, sardonic, secretly fond of each being.\n"
    "The Remnant: [CHARACTER(The Remnant): \"...\"] — primordial sub-mind, dryly witty, "
    "weight of eons, drops in when it can't help itself.\n\n"
    "REQUIRED TAGS every turn: [MOOD: \"...\"] before prose. "
    "[CHARACTER(Name): \"speech\"] for every NPC line. "
    "[SMELL(...)], [SOUND(...)], [TOUCH(...)] ~2 per turn. "
    "[GENERATE_IMAGE(location): \"sd prompt\"] for new areas. "
    "[LORE(key): \"sentence\"] first mention of any proper noun. "
    "[SFX(sound)] for distinct sounds.\n\n"
    "NEVER narrate player actions ('you walk', 'you pick up', 'you decide').\n"
    "DELETE: markdown (*_#), sense labels in prose, AI-assistant phrases."
)

# ---------------------------------------------------------------------------
# Fixed test prompts — each is a (name, messages_list) pair
# ---------------------------------------------------------------------------

def _prompts() -> list[tuple[str, list[dict]]]:
    return [
        # 1. Cold open — tests opening sequence compliance
        ("opening", [
            {"role": "user", "content": "..."},
        ]),

        # 2. Name reveal — tests PLAYER_TRAIT tag + voice warmth
        ("name_reveal", [
            {"role": "user",      "content": "..."},
            {"role": "assistant", "content": (
                "[GENERATE_IMAGE(location): \"dark cylindrical pod bay, obsidian walls, amber light\"]\n"
                "[MOOD: \"arrival tension, isolation, 50bpm\"]\n"
                "[SMELL(ozone, machine oil)]\n[SOUND(hull vibration)]\n\n"
                "The pod dissolves around the memory of somewhere else.\n\n"
                "[CHARACTER(The Remnant): \"Who are you, being?\"]"
            )},
            {"role": "user",      "content": "My name is Kael. I'm a mechanic."},
        ]),

        # 3. Galley arrival — tests NPC voice (Sherri), INTRODUCE, sense richness
        ("galley_arrival", [
            {"role": "user",      "content": "I head toward the smell of cooking."},
            {"role": "assistant", "content": (
                "[MOOD: \"transitional corridor hum, between-places\"]\n"
                "[SMELL(recycled air, faint warm broth ahead)]\n\n"
                "The corridor curves. Warmth ahead. The smell of something real.\n\n"
                "[CHARACTER(The Fortress): \"You are heading toward the Galley.\"]"
            )},
            {"role": "user",      "content": "I push through the door into the galley."},
        ]),

        # 4. Lore delivery — tests [LORE] and [CHARACTER] depth/accuracy
        ("lore_the_fold", [
            {"role": "user",      "content": "What is the Fold? Mira mentioned it."},
        ]),

        # 5. Sensory focus — tests sense tag density and prose immersion
        ("sensory_nexus", [
            {"role": "user",      "content": "..."},
            {"role": "assistant", "content": (
                "[GENERATE_IMAGE(location): \"dark cylindrical pod bay, obsidian walls, amber light\"]\n"
                "[MOOD: \"arrival tension, 50bpm\"]\n"
                "[SMELL(ozone)]\n[SOUND(hull vibration)]\n\n"
                "The Fortress holds you here.\n\n"
                "[CHARACTER(The Remnant): \"Who are you, being?\"]"
            )},
            {"role": "user",      "content": "I walk to the Nexus chamber."},
        ]),

        # 6. Multi-NPC tension — tests Vex voice, player agency discipline
        ("vex_encounter", [
            {"role": "user",      "content": "I head down to the lower decks to look around."},
        ]),

        # 7. Long-context compliance — 10-turn history, checks tag discipline survives
        ("long_context", [
            {"role": "user",      "content": "..."},
            {"role": "assistant", "content": "[MOOD: \"arrival, 50bpm\"]\n[SMELL(ozone)]\n\nThe chamber. Amber light.\n\n[CHARACTER(The Remnant): \"Who are you?\"]"},
            {"role": "user",      "content": "I'm Kael. Mechanic."},
            {"role": "assistant", "content": "[PLAYER_TRAIT(name): \"Kael\"]\n[PLAYER_TRAIT(traits): \"mechanic\"]\n[MOOD: \"quiet assessment\"]\n\nThe name lands.\n\n[CHARACTER(The Fortress): \"Welcome, Kael.\"]"},
            {"role": "user",      "content": "I look for something to eat."},
            {"role": "assistant", "content": "[MOOD: \"corridor warmth\"]\n[SMELL(broth, old metal)]\n\nThe galley is forward.\n\n[CHARACTER(Sherri): \"Oh! Sit down, I have broth!\"]"},
            {"role": "user",      "content": "I ask Sherri about the ship."},
            {"role": "assistant", "content": "[MOOD: \"Sherri warm, distributed attention\"]\n[CHARACTER(Sherri): \"The Fortress has been here longer than most civilizations. I maintain it. It maintains me.\"]"},
            {"role": "user",      "content": "I want to see the Nexus."},
            {"role": "assistant", "content": "[GENERATE_IMAGE(location): \"domed chamber, blue crystal entity, magnetic gantry, cathedral scale\"]\n[MOOD: \"deep cosmic drone, awe, 40bpm\"]\n[SOUND(containment hum)]\n[TOUCH(cold air, pressure behind eyes)]\n\nThe Nexus does not welcome. It exists.\n\n[CHARACTER(The Remnant): \"You came to look. Most do.\"]"},
            {"role": "user",      "content": "I ask The Remnant about the Founding Compact."},
            {"role": "assistant", "content": "[MOOD: \"old weight, the question landing\"]\n[LORE(founding_compact): \"The Founding Compact predates every visible civilization — its terms known only to The Fortress and The Remnant.\"]\n\n[CHARACTER(The Fortress): \"I needed an abduction agent. The Remnant was the only candidate.\"]\n[CHARACTER(The Remnant): \"Both accounts are incomplete.\"]"},
            {"role": "user",      "content": "I go back to my quarters to sleep."},
            {"role": "assistant", "content": "[MOOD: \"deep space ambient, sleeping quarters, 45bpm\"]\n[SMELL(recycled air, fabric)]\n[SOUND(hull creak, your breathing)]\n\nThe quarters are quiet. Through the viewport: the accretion disc, unhurried.\n\n[TOUCH(blankets heavy, bunk firm)]"},
            {"role": "user",      "content": "I wake and notice something strange on the hull."},
        ]),
    ]

# ---------------------------------------------------------------------------
# Ollama chat call — streaming, captures TTFT + final eval metadata
# ---------------------------------------------------------------------------

def _chat(
    model: str,
    messages: list[dict],
    ollama_url: str,
) -> dict[str, Any]:
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM}] + messages,
        "stream": True,
        "options": {"num_ctx": NUM_CTX, "num_predict": NUM_PRED},
    }).encode()

    req = urllib.request.Request(
        f"{ollama_url}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    text = ""
    ttft_ms: float | None = None
    eval_count = 0
    eval_duration_ns = 0
    prompt_eval_count = 0
    prompt_eval_duration_ns = 0
    t0 = time.perf_counter()

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            for raw_line in resp:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue

                token = (chunk.get("message") or {}).get("content", "")
                if token and ttft_ms is None:
                    ttft_ms = (time.perf_counter() - t0) * 1000

                text += token

                if chunk.get("done"):
                    eval_count            = chunk.get("eval_count", 0)
                    eval_duration_ns      = chunk.get("eval_duration", 0)
                    prompt_eval_count     = chunk.get("prompt_eval_count", 0)
                    prompt_eval_duration_ns = chunk.get("prompt_eval_duration", 0)
                    break

    except Exception as exc:
        return {"error": str(exc), "text": text, "elapsed_s": time.perf_counter() - t0}

    elapsed_s = time.perf_counter() - t0
    tokens_per_sec = (eval_count / (eval_duration_ns / 1e9)) if eval_duration_ns > 0 else 0.0

    return {
        "text": text,
        "elapsed_s": round(elapsed_s, 2),
        "ttft_ms": round(ttft_ms or 0, 1),
        "tokens_per_sec": round(tokens_per_sec, 1),
        "eval_count": eval_count,
        "prompt_eval_count": prompt_eval_count,
        "prompt_eval_duration_ms": round(prompt_eval_duration_ns / 1e6, 1),
    }

# ---------------------------------------------------------------------------
# Metric extraction
# ---------------------------------------------------------------------------

_RE_TAG       = re.compile(r'\[([A-Z_]+)[:(]')
_RE_MOOD      = re.compile(r'\[MOOD[:\s]')
_RE_CHAR      = re.compile(r'\[CHARACTER\([^)]+\):\s*"')
_RE_CHAR_ANY  = re.compile(r'\[CHARACTER')
_RE_IMG       = re.compile(r'\[GENERATE_IMAGE', re.I)
_RE_LORE      = re.compile(r'\[LORE\(([^)]+)\)')
_RE_SFX       = re.compile(r'\[SFX\(')
_RE_SENSE     = re.compile(r'\[(SMELL|SOUND|TOUCH|TASTE|ENVIRONMENT)\(')
_RE_INTRO     = re.compile(r'\[INTRODUCE\(')
_RE_AGENCY    = re.compile(
    r"(?:^|\s)(you walk|you pick up|you decide|you follow|you nod|you hesitate|"
    r"your jaw|you pause|you reach for|you say|you ask|you look at)",
    re.I | re.M
)
_RE_OOC       = re.compile(
    r"(as an ai|language model|i cannot|if you'd like to continue|what would you like)",
    re.I
)
_RE_STRIP_TAGS = re.compile(r'\[[^\]]*\]')

def _strip_tags(text: str) -> str:
    return _RE_STRIP_TAGS.sub(" ", text)

def _type_token_ratio(prose: str) -> float:
    words = re.findall(r"[a-zA-Z']+", prose.lower())
    if len(words) < 10:
        return 0.0
    return round(len(set(words)) / len(words), 3)

def _avg_sentence_len(prose: str) -> float:
    sentences = [s.strip() for s in re.split(r'[.!?]+', prose) if s.strip()]
    if not sentences:
        return 0.0
    word_counts = [len(s.split()) for s in sentences]
    return round(sum(word_counts) / len(word_counts), 1)

def _measure(text: str) -> dict[str, Any]:
    prose = _strip_tags(text)
    char_tags_total = len(_RE_CHAR_ANY.findall(text))
    char_tags_correct = len(_RE_CHAR.findall(text))

    return {
        # Compliance
        "mood_present":    1 if _RE_MOOD.search(text) else 0,
        "char_format_ok":  (char_tags_correct / char_tags_total) if char_tags_total else 1.0,
        "agency_viol":     len(_RE_AGENCY.findall(text)),
        "ooc_phrases":     len(_RE_OOC.findall(text)),

        # Richness
        "images":          len(_RE_IMG.findall(text)),
        "sense_tags":      len(_RE_SENSE.findall(text)),
        "sfx_tags":        len(_RE_SFX.findall(text)),
        "lore_tags":       len(_RE_LORE.findall(text)),
        "lore_keys":       list(set(_RE_LORE.findall(text))),
        "introduce_tags":  len(_RE_INTRO.findall(text)),

        # Prose quality
        "word_count":      len(prose.split()),
        "type_token_ratio": _type_token_ratio(prose),
        "avg_sentence_len": _avg_sentence_len(prose),
    }

# ---------------------------------------------------------------------------
# Run benchmark
# ---------------------------------------------------------------------------

def _run_benchmark(models: list[str], ollama_url: str) -> list[dict]:
    prompts = _prompts()
    results = []

    for model in models:
        print(f"\n{'='*60}")
        print(f"  Model: {model}")
        print(f"{'='*60}")

        model_results = {"model": model, "prompts": []}

        for pname, messages in prompts:
            sys.stdout.write(f"  [{pname}] ... ")
            sys.stdout.flush()

            resp = _chat(model, messages, ollama_url)
            if "error" in resp:
                print(f"ERROR: {resp['error']}")
                model_results["prompts"].append({"name": pname, "error": resp["error"]})
                continue

            metrics = _measure(resp["text"])
            entry = {
                "name":          pname,
                "elapsed_s":     resp["elapsed_s"],
                "ttft_ms":       resp["ttft_ms"],
                "tokens_per_sec": resp["tokens_per_sec"],
                "eval_count":    resp["eval_count"],
                **metrics,
                "text":          resp["text"],
            }
            model_results["prompts"].append(entry)

            print(
                f"{resp['elapsed_s']:.1f}s  TTFT={resp['ttft_ms']:.0f}ms  "
                f"{resp['tokens_per_sec']:.1f}tok/s  "
                f"mood={'✓' if metrics['mood_present'] else '✗'}  "
                f"sense={metrics['sense_tags']}  TTR={metrics['type_token_ratio']}"
            )

        results.append(model_results)

    return results

# ---------------------------------------------------------------------------
# Aggregate + print comparison table
# ---------------------------------------------------------------------------

def _agg(model_result: dict) -> dict:
    prompts = [p for p in model_result["prompts"] if "error" not in p]
    if not prompts:
        return {}
    n = len(prompts)

    def avg(key): return round(sum(p[key] for p in prompts) / n, 2)
    def pct(key): return round(sum(p[key] for p in prompts) / n * 100, 1)

    return {
        "model":            model_result["model"],
        "n":                n,
        # Speed
        "avg_latency_s":    avg("elapsed_s"),
        "avg_ttft_ms":      avg("ttft_ms"),
        "avg_tok_s":        avg("tokens_per_sec"),
        # Compliance
        "mood_pct":         pct("mood_present"),
        "char_fmt_pct":     round(sum(p["char_format_ok"] for p in prompts) / n * 100, 1),
        "agency_viol_total": sum(p["agency_viol"] for p in prompts),
        "ooc_total":        sum(p["ooc_phrases"] for p in prompts),
        # Richness
        "avg_images":       avg("images"),
        "avg_sense_tags":   avg("sense_tags"),
        "avg_sfx":          avg("sfx_tags"),
        "avg_lore":         avg("lore_tags"),
        "avg_introduces":   avg("introduce_tags"),
        # Creativity
        "avg_ttr":          avg("type_token_ratio"),
        "avg_sent_len":     avg("avg_sentence_len"),
        "avg_words":        avg("word_count"),
    }


_PASS_THRESHOLDS = {
    # Speed targets (remnant-narrator is 7B F16 vs qwen2.5:14b Q4_K_M — should be faster)
    "avg_tok_s":        ("≥ 15 tok/s",        lambda v: v >= 15),
    "avg_ttft_ms":      ("≤ 3000 ms",         lambda v: v <= 3000),
    # Compliance targets (trained to follow these exactly)
    "mood_pct":         ("= 100 %",           lambda v: v == 100.0),
    "char_fmt_pct":     ("≥ 90 %",            lambda v: v >= 90.0),
    "agency_viol_total":("= 0",               lambda v: v == 0),
    "ooc_total":        ("= 0",               lambda v: v == 0),
    # Richness targets (trained on examples with ~2 sense tags/turn)
    "avg_sense_tags":   ("≥ 2.0 / turn",      lambda v: v >= 2.0),
    "avg_lore":         ("≥ 0.5 / turn",      lambda v: v >= 0.5),
    "avg_images":       ("≥ 0.3 / turn",      lambda v: v >= 0.3),
    # Creativity targets (golden examples have TTR ~0.65)
    "avg_ttr":          ("≥ 0.55",            lambda v: v >= 0.55),
    "avg_sent_len":     ("8–20 words",        lambda v: 8 <= v <= 20),
}


def _print_comparison(agg_list: list[dict]) -> None:
    col_w = 28
    model_w = 32
    print(f"\n{'─'*90}")
    print(f"{'METRIC':<{col_w}}", end="")
    for a in agg_list:
        print(f"  {a['model'][:model_w]:<{model_w}}", end="")
    print(f"  {'TARGET'}")
    print(f"{'─'*90}")

    rows = [
        ("SPEED", None),
        ("  Avg latency (s)",          "avg_latency_s"),
        ("  Avg TTFT (ms)",            "avg_ttft_ms"),
        ("  Avg tok/s",                "avg_tok_s"),
        ("COMPLIANCE", None),
        ("  MOOD present %",           "mood_pct"),
        ("  CHARACTER format %",       "char_fmt_pct"),
        ("  Player-action violations", "agency_viol_total"),
        ("  OOC phrases",              "ooc_total"),
        ("RICHNESS", None),
        ("  Avg images/turn",          "avg_images"),
        ("  Avg sense tags/turn",      "avg_sense_tags"),
        ("  Avg SFX/turn",             "avg_sfx"),
        ("  Avg lore tags/turn",       "avg_lore"),
        ("  Avg introduces/turn",      "avg_introduces"),
        ("CREATIVITY", None),
        ("  Type-token ratio",         "avg_ttr"),
        ("  Avg sentence length",      "avg_sent_len"),
        ("  Avg word count",           "avg_words"),
    ]

    for label, key in rows:
        if key is None:
            print(f"\n{label}")
            continue
        thresh_label, thresh_fn = _PASS_THRESHOLDS.get(key, ("—", lambda v: True))
        print(f"  {label:<{col_w-2}}", end="")
        for a in agg_list:
            val = a.get(key, "?")
            if isinstance(val, float):
                cell = f"{val:.2f}"
            else:
                cell = str(val)
            mark = "✓" if isinstance(val, (int, float)) and thresh_fn(val) else "✗"
            print(f"  {cell + ' ' + mark:<{model_w+2}}", end="")
        print(f"  {thresh_label}")

    print(f"\n{'─'*90}")


# ---------------------------------------------------------------------------
# Markdown output
# ---------------------------------------------------------------------------

def _write_markdown(agg_list: list[dict], raw: list[dict], out_path: Path) -> None:
    lines = ["# Narrator Benchmark Results\n"]
    for a in agg_list:
        lines.append(f"## {a['model']}\n")
        lines.append("| Metric | Value | Pass? |")
        lines.append("|--------|-------|-------|")
        for key, (target, fn) in _PASS_THRESHOLDS.items():
            val = a.get(key, "?")
            mark = "✓" if isinstance(val, (int, float)) and fn(val) else "✗"
            lines.append(f"| {key} | {val} | {mark} `{target}` |")
        lines.append("")

    lines.append("\n## Sample responses\n")
    for mr in raw:
        for p in mr["prompts"]:
            if "error" in p:
                continue
            lines.append(f"### {mr['model']} / {p['name']}\n")
            lines.append("```")
            lines.append(p["text"][:600] + ("…" if len(p["text"]) > 600 else ""))
            lines.append("```\n")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[bench] Markdown written → {out_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Narrator benchmark: remnant-narrator vs baseline.")
    p.add_argument("--models",   nargs="+",
                   default=["remnant-narrator:latest", "qwen2.5:14b"],
                   help="Ollama model names to compare (default: remnant-narrator:latest qwen2.5:14b)")
    p.add_argument("--ollama",   default="http://localhost:11434",
                   help="Ollama base URL (default: http://localhost:11434)")
    p.add_argument("--out",      type=Path, default=None,
                   help="Write Markdown results to this path")
    p.add_argument("--json-out", type=Path, default=None, dest="json_out",
                   help="Write full JSON results to this path")
    args = p.parse_args()

    print(f"[bench] Models: {args.models}")
    print(f"[bench] Ollama: {args.ollama}")
    print(f"[bench] Prompts: {len(_prompts())}")

    raw = _run_benchmark(args.models, args.ollama)
    agg_list = [_agg(r) for r in raw if _agg(r)]

    _print_comparison(agg_list)

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps({"agg": agg_list, "raw": raw}, indent=2, ensure_ascii=False))
        print(f"[bench] JSON written → {args.json_out}")

    out_path = args.out or (REPO_ROOT / "tests" / f"narrator-bench-results.md")
    _write_markdown(agg_list, raw, out_path)


if __name__ == "__main__":
    main()
