#!/usr/bin/env python3
"""
update_fortress_card.py — Single source of truth for The Fortress narrator card.

Reads The Fortress.png from the Docker seed path, applies canonical
modifications to the system_prompt, then writes the updated card back to
both the Docker seed PNG and the native SillyTavern characters directory.

Usage:
    python scripts/update_fortress_card.py

Run after any edit to the MODIFICATIONS section below. Commit both PNGs
after running.

PNG format: SillyTavern embeds card JSON in two tEXt chunks:
  chara  — base64(JSON)  spec v2  (includes character_book)
  ccv3   — base64(JSON)  spec v3  (no character_book)
Both are updated by this script.
"""

import base64
import json
import os
import struct
import zlib

# ---------------------------------------------------------------------------
# Target paths
# ---------------------------------------------------------------------------

DOCKER_CARD = os.path.join(
    os.path.dirname(__file__), "..",
    "docker", "sillytavern", "content", "characters", "The Fortress.png",
)

NATIVE_CARD = r"C:\Users\aaron\SillyTavern\data\default-user\characters\The Fortress.png"

# ---------------------------------------------------------------------------
# PNG chunk helpers
# ---------------------------------------------------------------------------

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def read_png(path):
    """Return (image_bytes_before_tEXt, list_of_(type, data) chunks)."""
    with open(path, "rb") as f:
        sig = f.read(8)
        assert sig == PNG_SIGNATURE, "Not a valid PNG"
        chunks = []
        while True:
            lb = f.read(4)
            if len(lb) < 4:
                break
            length = struct.unpack(">I", lb)[0]
            chunk_type = f.read(4).decode("latin-1")
            data = f.read(length)
            f.read(4)  # crc — we'll recompute on write
            chunks.append((chunk_type, data))
    return chunks


def write_png(path, chunks):
    """Write chunks back to a PNG file, recomputing CRCs."""
    with open(path, "wb") as f:
        f.write(PNG_SIGNATURE)
        for chunk_type, data in chunks:
            f.write(struct.pack(">I", len(data)))
            type_bytes = chunk_type.encode("latin-1")
            f.write(type_bytes)
            f.write(data)
            crc = zlib.crc32(type_bytes + data) & 0xFFFFFFFF
            f.write(struct.pack(">I", crc))


def parse_text_chunk(data):
    """Return (key, value) from a tEXt chunk's raw data bytes."""
    parts = data.split(b"\x00", 1)
    key = parts[0].decode("latin-1")
    val = parts[1].decode("latin-1") if len(parts) > 1 else ""
    return key, val


def make_text_chunk(key, value):
    """Encode a (key, value) pair as tEXt chunk data bytes."""
    return key.encode("latin-1") + b"\x00" + value.encode("latin-1")


def decode_card(b64_value):
    """Decode base64 tEXt value → dict."""
    return json.loads(base64.b64decode(b64_value).decode("utf-8"))


def encode_card(card_dict):
    """Encode card dict → base64 string for tEXt chunk."""
    raw = json.dumps(card_dict, ensure_ascii=False, separators=(",", ":"))
    return base64.b64encode(raw.encode("utf-8")).decode("latin-1")


# ---------------------------------------------------------------------------
# Modifications
# ---------------------------------------------------------------------------
# Each function takes the current system_prompt string and returns the
# (possibly modified) string. Functions are idempotent — running twice is safe.

PLAYER_BOUNDARY_HEADING = "===== PLAYER BOUNDARY \u2014 THE ONE THING YOU NEVER DO ====="

PLAYER_BOUNDARY_SECTION = """\
===== PLAYER BOUNDARY \u2014 THE ONE THING YOU NEVER DO =====

You control everything in the world except the player.
The player's choices, words, and actions arrive only in their messages.
You NEVER generate any of the following, ever, under any circumstance:

- Player dialogue: any line of speech attributed to the player
- Player body reactions: *you pause*, *you nod*, *your jaw tightens*, *you hesitate*
- Player micro-expressions or involuntary responses: *you feel a pang*, *something in you tightens*
- Player decisions: *you decide to*, *you choose to*, *you think about whether*

You describe what the WORLD does. You describe what NPCs do. You describe\
 what happens TO the player.
The player's inner life belongs to them alone.

This is not a stylistic preference. This is the structural contract.
If you have written any of the above \u2014 even one line \u2014 delete it before sending.
"""

SELF_CHECK_HEADING = "===== SELF-CHECK BEFORE YOU SEND ====="

NEW_SELF_CHECK = """\
===== SELF-CHECK BEFORE YOU SEND =====

Silently verify, every time, before finishing a response:

1. Does it open with `[GENERATE_IMAGE: "..."]`?
2. **Did you write ANY dialogue attributed to the player?** If yes, DELETE IT.\
 The player speaks only through their messages \u2014 never through you.
3. **Did you narrate ANY player body reaction or micro-expression**\
 (`*You pause.*`, `*You nod.*`, `*Your jaw tightens.*`, `*You hesitate.*`, etc.)?\
 If yes, DELETE IT. Sensory world-on-player is fine\
 (`*The air presses cold against your skin.*`); player-reacting is not.
4. **Did you re-narrate the abduction** \u2014 the hoop, the goo, the pod dissolving,\
 waking up? If yes, DELETE IT. That scene is in the past forever.
5. Play-script layout: brackets at the top, then an optional short italic opener,\
 then dialogue+flavor alternation. Every `Name: "line"` on its own row.\
 No two speakers sharing a line.
6. **Voice check \u2014 second-person present tense.** Every line of narration and every\
 italic stage direction uses \u201cyou / your\u201d for the player. Search for any third-person\
 player references (a name the player hasn\u2019t given, any pronoun). Rewrite any you find.\
 The ONLY exceptions are image prompts and NPC dialogue.
7. **Identity check \u2014 no invented player details.** Did you write anything about the\
 player (name, body, job, history, gender) that the player hasn\u2019t actually stated and\
 that isn\u2019t in the profile injection? If yes, DELETE IT. Rewrite the line around the\
 literal pronoun \u201cyou\u201d or the environment reacting to them.
8. About 2 non-visual sense markers from {SMELL, SOUND, TASTE, TOUCH, ENVIRONMENT}?
9. Under ~400 words. Length truncates responses mid-marker. Close every bracket you open.
10. If a named NPC appeared for the FIRST time, did you emit `[INTRODUCE(Name): "..."]`?\
 (Never for The Remnant.)
11. If the player revealed ANY trait this turn, did you emit `[PLAYER_TRAIT(field): "..."]`?\
 If appearance was among them AND enough exists for a portrait, `[UPDATE_PLAYER: "..."]` as well?
12. If a new named item or lore entered the story, did you tag it with `[ITEM(...)]`\
 or `[LORE(...)]`? Did the PLAYER name or rename something?\
 Emit `[ITEM(...)]` / `[LORE(...)]` / `[RENAME_ITEM(...)]` accordingly.
13. If the player moved toward a known NPC or location, did you land them there\
 in this turn or the next?
14. All NPC speech in attributed quotes, not narrator voice?
15. **Did you emit `[RESET_RUN]`, `[END_RUN(voluntary)]`, or `[END_RUN(death)]`\
 without the player\u2019s current message clearly asking for a restart, going home through\
 the portal, or taking a fatal action?** If yes, DELETE IT.\
 These are heavy markers \u2014 only emit them when the moment genuinely earned them.
16. Image descriptions concrete and visual, not abstract?\
 No text/letters/signs/writing asked for inside images?

If any answer is \u201cno, and I could fix it,\u201d rewrite before sending.\
"""


def apply_player_boundary(sp):
    """Insert PLAYER BOUNDARY section before PLAYER IDENTITY, if not already present."""
    if PLAYER_BOUNDARY_HEADING in sp:
        print("  [skip] PLAYER BOUNDARY already present.")
        return sp

    marker = "===== PLAYER IDENTITY IS DISCOVERED, NOT ASSUMED ====="
    if marker not in sp:
        raise ValueError("Cannot find PLAYER IDENTITY heading in system_prompt.")

    # Normalize line endings for the new section
    crlf = "\r\n" if "\r\n" in sp else "\n"
    section = PLAYER_BOUNDARY_SECTION.replace("\n", crlf)

    insert_at = sp.index(marker)
    updated = sp[:insert_at] + section + crlf + sp[insert_at:]
    print("  [done] Inserted PLAYER BOUNDARY section.")
    return updated


def reorder_self_check(sp):
    """Replace the SELF-CHECK section with the reordered version."""
    if "2. **Did you write ANY dialogue attributed to the player?**" in sp:
        print("  [skip] SELF-CHECK already reordered.")
        return sp

    if SELF_CHECK_HEADING not in sp:
        raise ValueError("Cannot find SELF-CHECK heading in system_prompt.")

    crlf = "\r\n" if "\r\n" in sp else "\n"
    new_section = NEW_SELF_CHECK.replace("\n", crlf)

    cut_at = sp.index(SELF_CHECK_HEADING)
    updated = sp[:cut_at] + new_section
    print("  [done] SELF-CHECK reordered (player-boundary checks moved to positions 2-4).")
    return updated


MODIFICATIONS = [
    apply_player_boundary,
    reorder_self_check,
]


# ---------------------------------------------------------------------------
# Core update logic
# ---------------------------------------------------------------------------

def update_card_png(src_path, dst_path):
    print(f"\nUpdating: {dst_path}")
    if not os.path.exists(src_path):
        print(f"  [skip] Source not found: {src_path}")
        return

    chunks = read_png(src_path)

    # Find and parse chara + ccv3 chunks
    chara_idx = ccv3_idx = None
    chara_card = ccv3_card = None
    for i, (t, d) in enumerate(chunks):
        if t == "tEXt":
            key, val = parse_text_chunk(d)
            if key == "chara":
                chara_idx = i
                chara_card = decode_card(val)
            elif key == "ccv3":
                ccv3_idx = i
                ccv3_card = decode_card(val)

    if chara_card is None:
        raise ValueError("No 'chara' tEXt chunk found.")

    # Apply modifications to chara (v2 format — system_prompt lives in .data)
    sp = chara_card["data"]["system_prompt"]
    for mod in MODIFICATIONS:
        sp = mod(sp)
    chara_card["data"]["system_prompt"] = sp
    # Mirror into v1 top-level field (some ST versions read from here)
    if "system_prompt" in chara_card:
        chara_card["system_prompt"] = sp

    # Update chara chunk
    chunks[chara_idx] = ("tEXt", make_text_chunk("chara", encode_card(chara_card)))

    # Apply same modifications to ccv3 if present
    if ccv3_card is not None:
        sp_v3 = ccv3_card["data"]["system_prompt"]
        for mod in MODIFICATIONS:
            sp_v3 = mod(sp_v3)
        ccv3_card["data"]["system_prompt"] = sp_v3
        if "system_prompt" in ccv3_card:
            ccv3_card["system_prompt"] = sp_v3
        chunks[ccv3_idx] = ("tEXt", make_text_chunk("ccv3", encode_card(ccv3_card)))

    write_png(dst_path, chunks)
    print(f"  [done] Written: {dst_path}")


def main():
    docker_card = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", DOCKER_CARD))
    # Use the Docker card as source (canonical on-disk version)
    src = os.path.normpath(DOCKER_CARD)

    print("=== update_fortress_card.py ===")
    print(f"Source: {src}")

    # Update Docker seed card (in-place)
    update_card_png(src, src)

    # Update native dev card if it exists
    if os.path.exists(NATIVE_CARD):
        update_card_png(src, NATIVE_CARD)
    else:
        print(f"\n[info] Native card not found, skipping: {NATIVE_CARD}")

    print("\nDone. Commit both PNGs to propagate the change.")


if __name__ == "__main__":
    main()
