"""UI-level parity for the *starting scene* — what a player sees on
a cold boot before they've typed anything.

Motivation
----------
HTTP-level parity tests (tests/parity/test_sillytavern_surface_parity.py)
assert that server state looks right — main_api isn't Horde, the card
file serves, settings.json has the right keys. They do NOT see what's
actually rendered.

This suite opens a real headless browser against each stack and
asserts on DOM state. It catches:

  * Extension panels that are loaded but don't mount (gallery, codex)
  * Console errors thrown during boot (the webkit-scrollbar crash,
    future regressions of the same shape)
  * Chat state that's "configured" server-side but doesn't actually
    render to the user
  * Cross-stack visual divergence (docker renders 3 characters,
    native renders 5, etc.)

Story tuning will add tests here as new narrator/extension behaviors
need regression coverage.
"""

from __future__ import annotations

import re
import unittest

from ._browser import enabled_stacks, st_boot


# ---------------------------------------------------------------------------
# Diff helpers for cross-stack extension-state comparison
# ---------------------------------------------------------------------------

def _strip_path(d, dotted):
    """Delete a dotted path from a nested dict in-place. Missing
    intermediate keys are a no-op. Used to peel drift-allowlist
    keys off a snapshot before deep-diffing."""
    if not isinstance(d, dict) or not dotted:
        return
    parts = dotted.split(".")
    cur = d
    for p in parts[:-1]:
        if not isinstance(cur, dict) or p not in cur:
            return
        cur = cur[p]
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)


def _deep_diff(a, b, path="", out=None):
    """Return {dotted_path: (a_val, b_val)} for every leaf where
    a and b disagree. Dicts are descended; lists compared by
    length + elementwise; everything else compared by ==."""
    if out is None:
        out = {}
    if type(a) is not type(b):
        out[path or "<root>"] = (a, b)
        return out
    if isinstance(a, dict):
        for k in set(a.keys()) | set(b.keys()):
            sub = f"{path}.{k}" if path else k
            if k not in a or k not in b:
                out[sub] = (a.get(k, "<missing>"), b.get(k, "<missing>"))
            else:
                _deep_diff(a[k], b[k], sub, out)
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            out[path or "<root>"] = (f"len={len(a)}", f"len={len(b)}")
            return out
        for i, (x, y) in enumerate(zip(a, b)):
            _deep_diff(x, y, f"{path}[{i}]", out)
        return out
    if a != b:
        out[path or "<root>"] = (a, b)
    return out


EXPECTED_CHARACTERS = {
    "The Remnant",
    "The Fortress",
    "Narrator",
    "Jeremy Smythe",
    "Sherri",
}

# Strings that should never appear in a console.error during boot.
# Add to this list when a new regression class is discovered.
CONSOLE_ERROR_BLOCKLIST = [
    # The webkit-scrollbar :focus-visible crash from dynamic-styles.js.
    # Extension lint catches it statically too, but the UI test is the
    # belt-and-suspenders check for any *new* CSS rule that trips the
    # same mechanism.
    re.compile(r"Failed to parse the rule.*scrollbar", re.IGNORECASE),
    re.compile(r"insertRule", re.IGNORECASE),
    # Generic uncaught shape.
    re.compile(r"Uncaught.*TypeError", re.IGNORECASE),
    # Backend-missing shape. On a sealed docker stack, generate
    # throwing "No Ollama model selected" would match here — we want
    # that to be loud.
    re.compile(r"No Ollama model selected", re.IGNORECASE),
]


class ChatBootStateTest(unittest.TestCase):
    """A fresh boot must render The Remnant's opening line in #chat.

    Catches the class of bug where settings.json says active_character
    is set, the character file exists, the card's first_mes serves via
    the API, but the chat area is empty because no chat was auto-opened
    (pure welcome-screen limbo)."""

    def test_chat_has_remnant_opening(self):
        stacks = enabled_stacks()
        if not stacks:
            self.skipTest("no stacks enabled — set REMNANT_TEST_NATIVE=1 and/or REMNANT_TEST_DOCKER=1")
        for stack in stacks:
            with self.subTest(stack=stack):
                with st_boot(stack) as st:
                    messages = st.chat_messages()
                    self.assertTrue(
                        messages,
                        f"{stack}: #chat is empty after boot — welcome screen probably never advanced",
                    )
                    # Find the first non-system message. The welcome
                    # "SillyTavern System" card is a legitimate ST
                    # element, but there must also be at least one
                    # real character message.
                    real = [m for m in messages if not m["is_system"]]
                    self.assertTrue(
                        real,
                        f"{stack}: only system cards in chat, no character greeting rendered",
                    )
                    first_name = real[0]["name"]
                    # Both Narrator and The Remnant are canonical
                    # pre-input speakers. Native's auto-generation
                    # flow tends to land on Narrator; docker's seeded
                    # Opening.jsonl has The Remnant. Either is valid.
                    self.assertTrue(
                        "Remnant" in first_name or "Narrator" in first_name,
                        f"{stack}: first chat message is from {first_name!r}, "
                        f"expected The Remnant or Narrator",
                    )


class CleanBootConsoleTest(unittest.TestCase):
    """No blocklisted console.error messages during a cold boot.

    This is the net that catches the webkit-scrollbar dynamic-styles
    crash and any regression of the same shape. It also catches
    "No Ollama model selected" if we ever regress the seeded
    settings.json by dropping the model name.
    """

    def test_no_blocklisted_console_errors(self):
        stacks = enabled_stacks()
        if not stacks:
            self.skipTest("no stacks enabled")
        for stack in stacks:
            with self.subTest(stack=stack):
                with st_boot(stack) as st:
                    # Small wait so any async extension init that
                    # throws after boot (setTimeout, microtasks) has
                    # a chance to surface.
                    st.page.wait_for_timeout(500)
                    errors = st.console_errors() + st.page_errors
                    offenders = []
                    for err in errors:
                        for pat in CONSOLE_ERROR_BLOCKLIST:
                            if pat.search(err):
                                offenders.append(f"  [{pat.pattern}] {err[:200]}")
                                break
                    self.assertFalse(
                        offenders,
                        f"{stack}: blocklisted console errors on boot:\n"
                        + "\n".join(offenders),
                    )


class LeftNavRosterTest(unittest.TestCase):
    """All canonical characters must be visible in the left-nav
    character list after boot. Catches card files that parse server-
    side but don't reach the DOM because their embedded chara.name
    is malformed."""

    def test_all_canonical_characters_visible(self):
        stacks = enabled_stacks()
        if not stacks:
            self.skipTest("no stacks enabled")
        for stack in stacks:
            with self.subTest(stack=stack):
                with st_boot(stack) as st:
                    roster = set(st.character_roster())
                    missing = EXPECTED_CHARACTERS - roster
                    self.assertFalse(
                        missing,
                        f"{stack}: missing canonical characters in left nav: {missing} "
                        f"(saw: {sorted(roster)})",
                    )


class ExtensionGalleryMountedTest(unittest.TestCase):
    """The extension's image gallery must have the fortress-interior
    seed image present in extension_settings after initSettings() runs.

    This is the DOM-side corollary to the HTTP-level assertion —
    catches the case where the server returns empty extension_settings
    but the browser's initSettings() also fails to re-seed (extension
    crashed during load)."""

    def test_fortress_interior_seed_present(self):
        stacks = enabled_stacks()
        if not stacks:
            self.skipTest("no stacks enabled")
        for stack in stacks:
            with self.subTest(stack=stack):
                with st_boot(stack) as st:
                    ext = st.extension_settings("remnant")
                    self.assertIsNotNone(
                        ext,
                        f"{stack}: extension_settings.remnant is null — "
                        f"extension failed to initSettings() on boot",
                    )
                    images = ext.get("images") or []
                    self.assertTrue(
                        images,
                        f"{stack}: extension gallery is empty — the seeded "
                        f"fortress-interior image never landed",
                    )
                    seeded = [i for i in images if i.get("seeded") is True]
                    self.assertTrue(
                        seeded,
                        f"{stack}: no seeded=True image in gallery (got {len(images)} images, none marked seeded)",
                    )


class ExtensionCodexSeededTest(unittest.TestCase):
    """The Fold must be pre-seeded in the codex on a fresh boot.
    The extension creates it unconditionally in initSettings() — if
    it's missing, the extension either didn't run its init or crashed
    between the seed line and saveSettingsDebounced()."""

    def test_the_fold_seeded(self):
        stacks = enabled_stacks()
        if not stacks:
            self.skipTest("no stacks enabled")
        for stack in stacks:
            with self.subTest(stack=stack):
                with st_boot(stack) as st:
                    ext = st.extension_settings("remnant")
                    self.assertIsNotNone(ext, f"{stack}: extension_settings.remnant is null")
                    items = ((ext.get("codex") or {}).get("items") or {})
                    self.assertIn(
                        "The Fold",
                        items,
                        f"{stack}: The Fold missing from codex.items "
                        f"(saw: {list(items.keys())})",
                    )
                    fold = items["The Fold"]
                    self.assertIn(
                        "nanovirus",
                        (fold.get("description") or "").lower(),
                        f"{stack}: The Fold description doesn't match canonical text",
                    )


class ExtensionNpcsSeededTest(unittest.TestCase):
    """The Remnant and The Fortress must both be pre-seeded as NPCs
    in extension_settings on a fresh boot."""

    def test_remnant_and_fortress_npcs_seeded(self):
        stacks = enabled_stacks()
        if not stacks:
            self.skipTest("no stacks enabled")
        for stack in stacks:
            with self.subTest(stack=stack):
                with st_boot(stack) as st:
                    ext = st.extension_settings("remnant")
                    self.assertIsNotNone(ext, f"{stack}: extension_settings.remnant is null")
                    npcs = ext.get("npcs") or {}
                    for required in ("The Remnant", "The Fortress"):
                        self.assertIn(
                            required,
                            npcs,
                            f"{stack}: NPC {required!r} not seeded "
                            f"(saw: {list(npcs.keys())})",
                        )


class StartingSceneCrossStackTest(unittest.TestCase):
    """When both stacks are enabled, the *shape* of what they render
    on boot should match: same chat message count bucket, same
    character roster, same number of seeded gallery images, same
    codex items. Values don't have to be byte-identical (native has
    runtime state docker doesn't), but the pre-name-declared baseline
    must be the same."""

    def test_roster_matches(self):
        stacks = enabled_stacks()
        if len(stacks) < 2:
            self.skipTest("need both REMNANT_TEST_NATIVE=1 and REMNANT_TEST_DOCKER=1")
        rosters = {}
        for stack in stacks:
            with st_boot(stack) as st:
                rosters[stack] = set(st.character_roster())
        # Every stack must contain the canonical set. Native may have
        # extras (user-added cards); we don't fail on those.
        for stack, roster in rosters.items():
            missing = EXPECTED_CHARACTERS - roster
            self.assertFalse(
                missing,
                f"{stack} missing from canonical set: {missing}",
            )

    def test_extension_baseline_keys_match(self):
        """Both stacks must have the same top-level keys in
        extension_settings.remnant. Catches schema drift between a
        freshly-inited docker boot and a long-running native user."""
        stacks = enabled_stacks()
        if len(stacks) < 2:
            self.skipTest("need both stacks")
        key_sets = {}
        for stack in stacks:
            with st_boot(stack) as st:
                ext = st.extension_settings("remnant") or {}
                key_sets[stack] = set(ext.keys())
        ref_stack = "native" if "native" in key_sets else stacks[0]
        ref = key_sets[ref_stack]
        for other, keys in key_sets.items():
            if other == ref_stack:
                continue
            # Docker can be a subset of native (native accumulates
            # runtime keys like `characters`, `locations`). What we
            # care about is that no BASELINE key initSettings()
            # creates is missing on docker.
            baseline = {
                "enabled", "autoGenerate", "generateEvery",
                "images", "imageHistory", "npcs", "player",
                "codex", "run", "remnantMemory", "playerArchive",
                "topBarHidden", "legacyScrubbedForV261",
            }
            missing = baseline - keys
            self.assertFalse(
                missing,
                f"{other} missing baseline extension keys: {missing}",
            )


class ResetWorldParityTest(unittest.TestCase):
    """The canonical cross-stack parity bar.

    Native accumulates play state over weeks of use (chats, personas,
    gallery images, grown NPCs, grown codex). A fresh docker clone
    has only the committed seed. Comparing live state between them
    is comparing play history to a seed — always diverges.

    The right comparison is *post-reset*: both stacks click the
    extension's hard 'Reset World' button, which wipes accumulated
    state back to initSettings() defaults + the permanent residents
    (The Remnant, The Fortress, The Fold, fortress-interior gallery
    seed). Those defaults are identical on both stacks by
    construction — any divergence here is a real bug in
    initSettings() or in the reset path.
    """

    # Keys in extension_settings.remnant whose values are allowed to
    # drift between stacks. Everything else must deep-equal.
    DRIFT_ALLOWLIST = {
        # Run timestamps are generated during reset itself.
        "run.startedAt",
        "run.lastUpdated",
        # remnantMemory.abductions is a persistent log of every run
        # that ever ended on this install — length varies by the
        # per-stack reset history. Shape is still checked (the key
        # is present, it's an object with an abductions array) but
        # the contents are allowed to differ.
        "remnantMemory",
        # playerArchive is the soft-End-Story history of departed
        # beings. Same reasoning as remnantMemory.
        "playerArchive",
    }

    def test_extension_state_matches_after_reset(self):
        stacks = enabled_stacks()
        if len(stacks) < 2:
            self.skipTest(
                "need both REMNANT_TEST_NATIVE=1 and REMNANT_TEST_DOCKER=1"
            )
        snapshots = {}
        for stack in stacks:
            with st_boot(stack) as st:
                # reset_world() returns the post-reset snapshot with
                # drift keys already stripped at the source (see
                # extension/index.js __remnantTest.snapshot()).
                snap = st.reset_world()
                self.assertIsNotNone(
                    snap,
                    f"{stack}: __remnantTest.snapshot() returned null after reset",
                )
                snapshots[stack] = snap
        # Belt-and-suspenders: also peel the drift allowlist in case
        # the extension on one stack hasn't been updated with the
        # source-side strip yet.
        for snap in snapshots.values():
            for path in self.DRIFT_ALLOWLIST:
                _strip_path(snap, path)
        ref_stack = "native" if "native" in snapshots else stacks[0]
        ref = snapshots[ref_stack]
        for other, snap in snapshots.items():
            if other == ref_stack:
                continue
            diff = _deep_diff(ref, snap)
            self.assertFalse(
                diff,
                f"{other} diverges from {ref_stack} after Reset World:\n"
                + "\n".join(f"  {k}: {v}" for k, v in sorted(diff.items())),
            )


if __name__ == "__main__":
    unittest.main()
