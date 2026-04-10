"""Ollama LLM judge for fuzzy narrative assertions.

Asks a local Ollama model yes/no questions about narrator output.
Used only when REMNANT_JUDGE=1 — structural tests run without it.

All calls are short (num_predict=4) so they're fast (~0.5–2s each).
The judge prompt is designed to elicit a clear YES/NO with minimal
hallucination: it gives the full context and asks a binary question.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Optional

OLLAMA_BASE   = os.environ.get("OLLAMA_BASE", "http://localhost:11434")
JUDGE_MODEL   = os.environ.get("REMNANT_JUDGE_MODEL", "mistral:latest")
JUDGE_ENABLED = os.environ.get("REMNANT_JUDGE") == "1"

# Skip vision/embed models — they're bad at text reasoning
_SKIP_KEYWORDS = ("llava", "embed", "vision", "clip", "moondream", "bakllava")


def _best_text_model() -> str:
    """Pick the best available Ollama text model, preferring JUDGE_MODEL."""
    try:
        req = urllib.request.Request(f"{OLLAMA_BASE}/api/tags")
        with urllib.request.urlopen(req, timeout=4.0) as r:
            data = json.loads(r.read())
        models = [m["name"] for m in data.get("models", [])]
        # If preferred model is available, use it
        for m in models:
            if m.startswith(JUDGE_MODEL.split(":")[0]):
                return m
        # Otherwise first text model
        for m in models:
            if not any(skip in m for skip in _SKIP_KEYWORDS):
                return m
    except Exception:
        pass
    return JUDGE_MODEL


def _ollama_generate(prompt: str, model: Optional[str] = None,
                     num_predict: int = 4) -> str:
    """Call Ollama /api/generate and return the response text."""
    body = json.dumps({
        "model": model or _best_text_model(),
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": num_predict, "temperature": 0},
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_BASE}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20.0) as r:
            return json.loads(r.read()).get("response", "").strip()
    except Exception as e:
        return f"ERROR: {e}"


def judge(question: str, context: str,
          model: Optional[str] = None) -> tuple[bool, str]:
    """Ask a yes/no question about a narrative context.

    Returns (passed: bool, raw_response: str).
    Always returns (True, "SKIPPED") when REMNANT_JUDGE != 1.

    Example:
        ok, raw = judge(
            "Does this response describe an environment?",
            f"Narrator: {turn['raw_text'][:500]}"
        )
        assert ok, f"Judge failed: {raw}"
    """
    if not JUDGE_ENABLED:
        return True, "SKIPPED"

    prompt = (
        f"{context}\n\n"
        f"Question: {question}\n"
        f"Answer with YES or NO only."
    )
    raw = _ollama_generate(prompt, model=model, num_predict=4)
    passed = raw.strip().upper().startswith("Y")
    return passed, raw


def judge_assert(question: str, context: str,
                 model: Optional[str] = None) -> None:
    """Like judge() but raises AssertionError on failure.

    Use inside unittest test methods:
        judge_assert(
            "Does this response reference the player's action?",
            f"Player: '{player_input}'\nNarrator: '{turn['raw_text'][:500]}'"
        )
    """
    passed, raw = judge(question, context, model=model)
    if not passed:
        raise AssertionError(f"LLM judge said NO.\nQ: {question}\nA: {raw}")


def judge_sequential(turn_prev: dict, turn_curr: dict,
                     model: Optional[str] = None) -> None:
    """Assert that turn_curr follows naturally from turn_prev.

    Raises AssertionError if the judge says NO.
    Skips if REMNANT_JUDGE != 1.
    """
    if not JUDGE_ENABLED:
        return
    prev_text = (turn_prev.get("raw_text") or "")[:400]
    curr_text = (turn_curr.get("raw_text") or "")[:400]
    judge_assert(
        "Does the second narrator passage continue naturally from the first, "
        "as sequential turns in the same narrative?",
        f"Turn 1:\n{prev_text}\n\nTurn 2:\n{curr_text}",
        model=model,
    )
