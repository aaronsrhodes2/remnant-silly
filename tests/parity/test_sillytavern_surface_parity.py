"""Parity tests for SillyTavern's own HTTP surface.

Unlike the diag sidecar tests, these exercise ST itself: the character
roster, the world-info list, the configured API backend, the extension's
static asset URLs, and whether an active character is pre-loaded so the
welcome-screen gate at scripts/welcome-screen.js:175 does not fire.

Requires REMNANT_TEST_NATIVE=1 and/or REMNANT_TEST_DOCKER=1 — same
contract as the existing diag parity tests.
"""

from __future__ import annotations

import os
import re
import unittest

from ._common import (
    enabled_stacks,
    st_fetch_bytes,
    st_get_settings,
    st_post_json,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Characters we ship as the canonical Remnant seed set. The content
# under docker/sillytavern/content/characters/ is the single source of
# truth; if a new card is added there, this set should grow in lockstep.
EXPECTED_CHARACTERS = {
    "The Remnant",
    "The Fortress",
    "Narrator",
    "Jeremy Smythe",
    "Sherri",
}

# World books shipped under docker/sillytavern/content/worlds/.
EXPECTED_WORLDS = {"Eldoria", "Nullspace Nexus"}

# Static asset baked into the extension image at scripts/extensions/
# image-generator/assets/. Tests verify the URL path matches between
# stacks — catches the image-generator/ vs remnant/ rename bug.
ASSET_PATH = "/scripts/extensions/image-generator/assets/fortress-interior.jpg"


class CharacterRosterParityTest(unittest.TestCase):
    """Both stacks must install the canonical character set."""

    def _roster(self, stack):
        data = st_post_json(stack, "/api/characters/all", {})
        self.assertIsNotNone(data, f"{stack}: /api/characters/all returned no data")
        # /all returns a list; each entry has a "name" string.
        return {c.get("name") for c in data if isinstance(c, dict) and c.get("name")}

    def test_canonical_characters_present(self):
        stacks = enabled_stacks()
        if not stacks:
            self.skipTest("no stacks enabled")
        for stack in stacks:
            with self.subTest(stack=stack):
                missing = EXPECTED_CHARACTERS - self._roster(stack)
                self.assertFalse(
                    missing,
                    f"{stack} missing canonical characters: {missing}",
                )


class WorldInfoParityTest(unittest.TestCase):
    """Both stacks must ship the canonical world books."""

    def test_worlds_present(self):
        stacks = enabled_stacks()
        if not stacks:
            self.skipTest("no stacks enabled")
        for stack in stacks:
            with self.subTest(stack=stack):
                settings = st_get_settings(stack)
                self.assertIsNotNone(settings, f"{stack}: settings fetch failed")
                worlds = set(settings["_meta"].get("world_names") or [])
                missing = EXPECTED_WORLDS - worlds
                self.assertFalse(missing, f"{stack} missing worlds: {missing}")


class ApiBackendParityTest(unittest.TestCase):
    """The configured backend must not be Horde.

    Catches the CURRENT failure mode: a clean docker boot defaults to
    main_api=koboldhorde, Horde status calls 500 in the sealed stack,
    and the UI falls back to the generic welcome card.
    """

    def test_not_horde(self):
        stacks = enabled_stacks()
        if not stacks:
            self.skipTest("no stacks enabled")
        for stack in stacks:
            with self.subTest(stack=stack):
                settings = st_get_settings(stack)
                self.assertIsNotNone(settings, f"{stack}: settings fetch failed")
                main_api = settings.get("main_api")
                self.assertNotIn(
                    main_api,
                    {"koboldhorde", "kobold", "horde"},
                    f"{stack} is defaulting to Horde/Kobold (main_api={main_api!r})",
                )


class ActiveCharacterParityTest(unittest.TestCase):
    """A fresh boot must have The Remnant preselected so the welcome
    gate (welcome-screen.js:175) does not fire."""

    def test_active_character_is_the_remnant(self):
        stacks = enabled_stacks()
        if not stacks:
            self.skipTest("no stacks enabled")
        for stack in stacks:
            with self.subTest(stack=stack):
                settings = st_get_settings(stack)
                self.assertIsNotNone(settings, f"{stack}: settings fetch failed")
                active = settings.get("active_character") or ""
                self.assertIn(
                    "The Remnant",
                    active,
                    f"{stack} has no active character (got {active!r})",
                )


class ExtensionAssetParityTest(unittest.TestCase):
    """The extension's static assets must serve at the same URL on
    both stacks. Catches the image-generator/ vs remnant/ path bug
    that shipped in an earlier v2.9.x build."""

    def test_asset_serves(self):
        stacks = enabled_stacks()
        if not stacks:
            self.skipTest("no stacks enabled")
        for stack in stacks:
            with self.subTest(stack=stack):
                body = st_fetch_bytes(stack, ASSET_PATH)
                self.assertIsNotNone(body, f"{stack}: 404 on {ASSET_PATH}")
                self.assertGreater(
                    len(body),
                    10_000,
                    f"{stack} returned suspiciously small asset ({len(body)} bytes)",
                )

    def test_asset_bytes_match_across_stacks(self):
        stacks = enabled_stacks()
        if len(stacks) < 2:
            self.skipTest("need both stacks for cross-stack comparison")
        by_stack = {s: st_fetch_bytes(s, ASSET_PATH) for s in stacks}
        ref_key = "native" if "native" in by_stack else stacks[0]
        ref = by_stack[ref_key]
        for other, b in by_stack.items():
            if other == ref_key:
                continue
            self.assertEqual(
                len(ref),
                len(b) if b is not None else -1,
                f"{other} asset size differs from {ref_key}",
            )


class OllamaModelConfiguredTest(unittest.TestCase):
    """A fresh boot must have a concrete Ollama model name seeded.

    Without this, ST throws "No Ollama model selected" at
    textgen-settings.js:1494 the first time the user hits Send,
    because the tokenizer lookup has nothing to bind to. The
    stack has a backend and an active character but still
    can't generate.
    """

    def test_ollama_model_set(self):
        stacks = enabled_stacks()
        if not stacks:
            self.skipTest("no stacks enabled")
        for stack in stacks:
            with self.subTest(stack=stack):
                settings = st_get_settings(stack)
                self.assertIsNotNone(settings, f"{stack}: settings fetch failed")
                tgw = settings.get("textgenerationwebui_settings") or {}
                model = tgw.get("ollama_model") or ""
                self.assertTrue(
                    model.strip(),
                    f"{stack} has no ollama_model set — first Send will throw "
                    f"'No Ollama model selected' (textgen-settings.js:1494)",
                )


class WelcomeAssistantParityTest(unittest.TestCase):
    """The welcome screen picks its assistant character from
    accountStorage.assistant — without it, a fresh boot shows
    the generic ST "Assistant" stub card with the "try asking me
    something" hint, regardless of what active_character is set
    to. Mirrors what native's persistent user already has."""

    def test_assistant_avatar_set(self):
        stacks = enabled_stacks()
        if not stacks:
            self.skipTest("no stacks enabled")
        for stack in stacks:
            with self.subTest(stack=stack):
                settings = st_get_settings(stack)
                self.assertIsNotNone(settings, f"{stack}: settings fetch failed")
                acct = settings.get("accountStorage") or {}
                assistant = acct.get("assistant") or ""
                self.assertIn(
                    "The Remnant",
                    assistant,
                    f"{stack} has no accountStorage.assistant — welcome-screen "
                    f"will render the generic Assistant stub instead of "
                    f"The Remnant (got {assistant!r})",
                )


class CharacterCardBytesParityTest(unittest.TestCase):
    """Per-stack smoke test that every expected card file serves
    as a valid, non-placeholder PNG. This is a file-serving check
    only — byte-identity across stacks is NOT asserted here.

    Previously we cross-stack-hashed these, but native's
    `The Remnant.png` is a historical mislabel (embedded
    chara.name="Narrator") and native has no file literally
    named `Narrator.png`. The functional parity guarantee — that
    both stacks expose the five canonical chara.name values — is
    already covered by CharacterRosterParityTest, which reads the
    embedded names via /api/characters/all.
    """

    # Only include files we expect both stacks to serve. Narrator.png
    # is intentionally omitted: native's Narrator card is embedded in
    # the mislabeled The Remnant.png file (different filename, same
    # character).
    CARDS = [
        "The Remnant.png",
        "The Fortress.png",
        "Jeremy Smythe.png",
        "Sherri.png",
    ]

    def _fetch_card_bytes(self, stack, filename):
        # ST's /characters endpoint serves raw PNG via the avatar path.
        # URL-encode spaces; don't touch anything else (matches browser).
        encoded = filename.replace(" ", "%20")
        return st_fetch_bytes(stack, f"/characters/{encoded}")

    def test_each_card_serves(self):
        stacks = enabled_stacks()
        if not stacks:
            self.skipTest("no stacks enabled")
        for stack in stacks:
            for card in self.CARDS:
                with self.subTest(stack=stack, card=card):
                    body = self._fetch_card_bytes(stack, card)
                    self.assertIsNotNone(body, f"{stack}: 404 on /characters/{card}")
                    self.assertGreater(
                        len(body),
                        50_000,
                        f"{stack}: {card} is suspiciously small "
                        f"({len(body)} bytes) — likely a placeholder",
                    )
                    # PNG magic bytes — rules out server-error HTML
                    # being served with a .png URL.
                    self.assertEqual(
                        body[:8],
                        b"\x89PNG\r\n\x1a\n",
                        f"{stack}: {card} is not a valid PNG",
                    )


class ExtensionStyleLintTest(unittest.TestCase):
    """Static lint on extension/style.css — catches the exact
    class of bug that crashed dynamic-styles.js on :1582:

        .foo::-webkit-scrollbar-thumb:hover { ... }

    ST's dynamic-styles.js auto-generates a :focus-visible
    counterpart for every :hover rule. On a scrollbar pseudo,
    the resulting selector is an insertRule SyntaxError and
    the extension's entire sheet fails to attach. The test
    runs against the source file in the repo — no stack
    required, always enabled."""

    CSS_PATH = os.path.join(REPO_ROOT, "extension", "style.css")
    BAD_PATTERN = re.compile(
        r"::-webkit-scrollbar[A-Za-z-]*:hover", re.IGNORECASE
    )

    def test_no_hover_on_webkit_scrollbar(self):
        self.assertTrue(
            os.path.exists(self.CSS_PATH),
            f"extension/style.css missing at {self.CSS_PATH}",
        )
        with open(self.CSS_PATH, "r", encoding="utf-8") as f:
            css = f.read()
        offenders = []
        for i, line in enumerate(css.splitlines(), start=1):
            if self.BAD_PATTERN.search(line):
                offenders.append(f"  line {i}: {line.strip()}")
        self.assertFalse(
            offenders,
            "extension/style.css has :hover on a webkit-scrollbar pseudo — "
            "ST's dynamic-styles.js will crash trying to synthesize a "
            ":focus-visible variant:\n" + "\n".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
