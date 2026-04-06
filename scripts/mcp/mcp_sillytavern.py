#!/usr/bin/env python3
"""
MCP server — SillyTavern (world state / canon reference)

Exposes SillyTavern's REST API as MCP tools so Claude Code can read the
authoritative world state — current characters, world info, recent chat —
before writing any content that should be canon-consistent.

Port: 8000 (ST default, proxied at 1580)
Golden rule: query this BEFORE inventing characters, locations, or lore.
             ST is the ground truth; don't contradict it.
"""

import httpx
from mcp.server.fastmcp import FastMCP

ST_BASE = "http://localhost:8000"
HEADERS  = {"Content-Type": "application/json"}

mcp = FastMCP("sillytavern")


def _st_post(path: str, body: dict = None) -> dict:
    r = httpx.post(
        f"{ST_BASE}{path}",
        json=body or {},
        headers=HEADERS,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


@mcp.tool()
def st_list_characters() -> str:
    """
    List all character cards currently in SillyTavern.
    Use this to see which NPCs, the Fortress, The Remnant, and player
    personas are defined before adding or referencing any character.
    """
    try:
        data = _st_post("/api/characters/all")
        chars = data if isinstance(data, list) else data.get("characters", [])
        if not chars:
            return "No characters found."
        lines = []
        for c in chars:
            name = c.get("name", c.get("avatar", "unknown"))
            desc = (c.get("description") or "")[:80].replace("\n", " ")
            lines.append(f"  {name}: {desc}")
        return f"Characters ({len(chars)}):\n" + "\n".join(lines)
    except Exception as e:
        return f"ST unreachable or error: {e}"


@mcp.tool()
def st_get_character(avatar_filename: str) -> str:
    """
    Get the full character card for a specific character.

    Args:
        avatar_filename: The avatar filename (e.g. 'The Fortress.png').
                         Get this from st_list_characters().
    """
    try:
        data = _st_post("/api/characters/get", {"avatar": avatar_filename})
        name        = data.get("name", "unknown")
        description = data.get("description", "")
        personality = data.get("personality", "")
        scenario    = data.get("scenario", "")
        first_mes   = data.get("first_mes", "")[:200]
        return (
            f"Character: {name}\n\n"
            f"Description:\n{description}\n\n"
            f"Personality:\n{personality}\n\n"
            f"Scenario:\n{scenario}\n\n"
            f"First message (preview):\n{first_mes}..."
        )
    except Exception as e:
        return f"Failed to get character '{avatar_filename}': {e}"


@mcp.tool()
def st_list_worldinfo() -> str:
    """
    List all world info entries (global lorebook).
    These are the canonical facts the Fortress uses to maintain world
    consistency — locations, factions, items, rules of the world.
    Read this before writing any lore.
    """
    try:
        data = _st_post("/api/worldinfo/list")
        files = data if isinstance(data, list) else data.get("files", [])
        if not files:
            return "No world info files found."
        return "World info files:\n" + "\n".join(f"  {f}" for f in files)
    except Exception as e:
        return f"World info unavailable: {e}"


@mcp.tool()
def st_get_worldinfo(filename: str) -> str:
    """
    Get the contents of a specific world info file (lorebook).

    Args:
        filename: World info file name from st_list_worldinfo().
    """
    try:
        data = _st_post("/api/worldinfo/get", {"name": filename})
        entries = data.get("entries", {})
        if not entries:
            return f"World info '{filename}' is empty."
        lines = []
        for key, entry in list(entries.items())[:50]:  # cap at 50
            keys_str = ", ".join(entry.get("key", []))
            content  = (entry.get("content") or "")[:120].replace("\n", " ")
            lines.append(f"  [{keys_str}]: {content}")
        return f"World info '{filename}' ({len(entries)} entries):\n" + "\n".join(lines)
    except Exception as e:
        return f"Failed to get world info '{filename}': {e}"


@mcp.tool()
def st_recent_chat(n: int = 20) -> str:
    """
    Get the most recent chat messages across all characters.
    Use this to understand the current state of the world — what has
    happened, where the player is, what the Fortress has said recently.

    Args:
        n: Number of most recent messages to return (default 20, max 100).
    """
    try:
        data = _st_post("/api/chats/recent", {"limit": min(n, 100)})
        chats = data if isinstance(data, list) else data.get("chats", [])
        if not chats:
            return "No recent chats found."
        lines = []
        for msg in chats[:n]:
            role    = "Player" if msg.get("is_user") else (msg.get("name") or "Narrator")
            content = (msg.get("mes") or "")[:160].replace("\n", " ")
            lines.append(f"  [{role}]: {content}")
        return f"Recent chat ({len(lines)} messages):\n" + "\n".join(lines)
    except Exception as e:
        return f"Recent chat unavailable: {e}"


@mcp.tool()
def st_get_settings() -> str:
    """
    Get current SillyTavern extension settings for The Remnant.
    Returns the active player profile, current location, known NPCs,
    codex entries, and run state.
    """
    try:
        # Extension settings live in the extension's settings endpoint
        r = httpx.get(f"{ST_BASE}/api/extensions/settings", timeout=10)
        if r.status_code == 404:
            # Fallback: try the main settings endpoint
            r = httpx.post(f"{ST_BASE}/api/settings/get", json={}, timeout=10)
        r.raise_for_status()
        d = r.json()
        # Pull out the image-generator (Remnant) extension settings
        ext = d.get("extensions", {}).get("image-generator", {})
        if not ext:
            return f"Remnant extension settings not found in response. Keys: {list(d.keys())[:10]}"
        player   = ext.get("player", {}).get("profile", {})
        location = ext.get("currentLocation", "unknown")
        npcs     = list((ext.get("npcs") or {}).keys())
        run      = ext.get("run", {})
        return (
            f"Remnant state:\n"
            f"  Player: {player.get('name', 'Unknown Being')}\n"
            f"  Location: {location}\n"
            f"  Known NPCs: {', '.join(npcs) or 'none'}\n"
            f"  Run active: {run.get('active', False)}\n"
            f"  Run started: {run.get('startedAt', 'n/a')}"
        )
    except Exception as e:
        return f"Settings unavailable: {e}"


if __name__ == "__main__":
    mcp.run()
