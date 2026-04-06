#!/usr/bin/env python3
"""
MCP server — Flask SD (Stable Diffusion image generation)

Exposes the local SD pipeline as MCP tools so Claude Code can generate
actual images for Remnant assets — character portraits, scene art, UI
elements — instead of describing them or leaving them as placeholders.

Port: 5000 (flask-sd default)
Golden rule: use this for ALL image generation on Remnant content.
"""

import base64
import httpx
from mcp.server.fastmcp import FastMCP

SD_BASE = "http://localhost:5000"

mcp = FastMCP("flask-sd")


def _check_health() -> bool:
    try:
        r = httpx.get(f"{SD_BASE}/api/health", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


@mcp.tool()
def sd_health() -> str:
    """Check whether the Stable Diffusion server is running and ready."""
    try:
        r = httpx.get(f"{SD_BASE}/api/health", timeout=5)
        d = r.json()
        return f"SD server running. Device: {d.get('device', 'unknown')}"
    except Exception as e:
        return f"SD server unreachable at {SD_BASE}: {e}\nStart it with: python backend/image_generator_api.py"


@mcp.tool()
def sd_generate(
    prompt: str,
    negative_prompt: str = "blurry, low quality, distorted, watermark, text",
    steps: int = 25,
    guidance_scale: float = 7.5,
) -> str:
    """
    Generate an image from a text prompt using the local Stable Diffusion pipeline.

    Returns the image as a base64-encoded PNG data URL and the gallery image_id
    for future reference. The image is automatically stored in the SD gallery.

    Golden rule: call this for ANY image asset needed for the Remnant world —
    character portraits, scene backgrounds, item art, UI elements.

    Args:
        prompt:          Positive prompt describing what to generate.
        negative_prompt: Things to avoid in the image.
        steps:           Inference steps (25 = fast, 50 = quality).
        guidance_scale:  How strictly to follow the prompt (7.5 is default).
    """
    if not _check_health():
        return (
            "SD server is not running. Start it with:\n"
            "  python backend/image_generator_api.py\n"
            "Then retry."
        )
    try:
        r = httpx.post(
            f"{SD_BASE}/api/generate",
            json={
                "prompt": prompt,
                "negative_prompt": negative_prompt,
                "steps": steps,
                "guidance_scale": guidance_scale,
            },
            timeout=180,  # SD takes up to 60s on CPU
        )
        r.raise_for_status()
        d = r.json()
        if not d.get("success"):
            return f"Generation failed: {d.get('error', 'unknown error')}"
        image_id = d.get("image_id", "unknown")
        data_url = d.get("image", "")
        return (
            f"Image generated successfully.\n"
            f"image_id: {image_id}\n"
            f"data_url_prefix: {data_url[:60]}...\n"
            f"Full data URL available — use image_id '{image_id}' to retrieve via sd_get_image."
        )
    except Exception as e:
        return f"SD generation error: {e}"


@mcp.tool()
def sd_gallery_list() -> str:
    """List all images currently stored in the SD gallery with their IDs and descriptions."""
    try:
        r = httpx.get(f"{SD_BASE}/api/gallery", timeout=10)
        r.raise_for_status()
        images = r.json().get("images", [])
        if not images:
            return "Gallery is empty."
        lines = [f"  [{img['id']}] {img.get('description', '')[:80]}" for img in images[:40]]
        return f"Gallery ({len(images)} images):\n" + "\n".join(lines)
    except Exception as e:
        return f"Gallery unavailable: {e}"


@mcp.tool()
def sd_gallery_search(description: str) -> str:
    """
    Search the SD gallery for an existing image that matches a description.
    Use this before generating to avoid duplicates.

    Args:
        description: Text description to search for.
    """
    try:
        r = httpx.post(
            f"{SD_BASE}/api/gallery/search",
            json={"description": description},
            timeout=10,
        )
        r.raise_for_status()
        d = r.json()
        if d.get("found"):
            return (
                f"Match found!\n"
                f"image_id: {d.get('id')}\n"
                f"description: {d.get('match_description')}\n"
                f"data_url: {d.get('image', '')[:60]}..."
            )
        return "No matching image found in gallery — safe to generate new."
    except Exception as e:
        return f"Gallery search failed: {e}"


if __name__ == "__main__":
    mcp.run()
