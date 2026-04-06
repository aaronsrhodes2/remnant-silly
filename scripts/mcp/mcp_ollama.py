#!/usr/bin/env python3
"""
MCP server — Ollama (language / world text generation)

Exposes Ollama's inference API as MCP tools so Claude Code can generate
world text, NPC dialogue, lore, and descriptions using the local model
stack instead of making content up.

Port: 11434 (Ollama default)
Golden rule: use this for ALL language generation on Remnant content.
"""

import httpx
from mcp.server.fastmcp import FastMCP

OLLAMA_BASE = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5:14b"   # best general-purpose model in the stack
FAST_MODEL    = "mistral:latest" # faster, lighter for quick tasks

mcp = FastMCP("ollama")


@mcp.tool()
def ollama_list_models() -> str:
    """List all models currently available in the local Ollama instance."""
    try:
        r = httpx.get(f"{OLLAMA_BASE}/api/tags", timeout=8)
        r.raise_for_status()
        models = r.json().get("models", [])
        if not models:
            return "No models found."
        lines = [f"  {m['name']}  ({round(m.get('size', 0) / 1e9, 1)} GB)" for m in models]
        return "Available Ollama models:\n" + "\n".join(lines)
    except Exception as e:
        return f"Ollama unreachable: {e}"


@mcp.tool()
def ollama_generate(
    prompt: str,
    model: str = DEFAULT_MODEL,
    system: str = "",
) -> str:
    """
    Generate text from a prompt using a local Ollama model.

    Use this for: lore writing, location descriptions, NPC backstories,
    item flavour text, world-building copy — anything that needs language.

    Args:
        prompt: The prompt to generate from.
        model:  Ollama model name (default: qwen2.5:14b).
                Use 'mistral:latest' for faster/lighter tasks.
                Use 'llava:latest' for vision+language (pass image in prompt as base64).
        system: Optional system prompt to steer tone/style.
    """
    payload: dict = {"model": model, "prompt": prompt, "stream": False}
    if system:
        payload["system"] = system
    try:
        r = httpx.post(
            f"{OLLAMA_BASE}/api/generate",
            json=payload,
            timeout=120,
        )
        r.raise_for_status()
        return r.json().get("response", "").strip()
    except Exception as e:
        return f"Generation failed: {e}"


@mcp.tool()
def ollama_chat(
    messages: list[dict],
    model: str = DEFAULT_MODEL,
    system: str = "",
) -> str:
    """
    Multi-turn chat with a local Ollama model.

    Each message is {"role": "user"|"assistant", "content": "..."}.
    Use this when you need back-and-forth context (e.g. iterating on lore).

    Args:
        messages: List of {"role": ..., "content": ...} dicts.
        model:    Ollama model name (default: qwen2.5:14b).
        system:   Optional system prompt.
    """
    payload: dict = {"model": model, "messages": messages, "stream": False}
    if system:
        payload["system"] = system
    try:
        r = httpx.post(
            f"{OLLAMA_BASE}/api/chat",
            json=payload,
            timeout=120,
        )
        r.raise_for_status()
        msg = r.json().get("message", {})
        return msg.get("content", "").strip()
    except Exception as e:
        return f"Chat failed: {e}"


if __name__ == "__main__":
    mcp.run()
