# UI-parity tests

Playwright-based tests that drive a real headless Chromium against
SillyTavern and assert on what the extension actually renders. They
complement `tests/parity/` (HTTP-level, diag sidecar + ST JSON API)
by catching bugs that only show up in the DOM — panels that load but
don't mount, console errors thrown during boot, characters installed
server-side that never reach the left nav, etc.

## One-time setup

```
pip install playwright
python -m playwright install chromium
```

The suite is stdlib `unittest` otherwise — no pytest, no new runner.

## Running

Same stack-gating envvars as `tests/parity/`:

```
REMNANT_TEST_NATIVE=1 python -m unittest discover tests/ui_parity -v
REMNANT_TEST_DOCKER=1 python -m unittest discover tests/ui_parity -v
REMNANT_TEST_NATIVE=1 REMNANT_TEST_DOCKER=1 python -m unittest discover tests/ui_parity -v
```

A test class that iterates `enabled_stacks()` runs once per stack via
`subTest(stack=...)`. Cross-stack tests (`ResetWorldParityTest`,
`StartingSceneCrossStackTest`) skip cleanly unless both flags are set.

## The parity model

**The canonical cross-stack comparison is post-reset, not boot-time.**

Native accumulates play state over weeks of use: dozens of chats,
custom personas, gallery images generated during play, grown NPC
cards, codex entries that emerged from narrator markers. A fresh
`git clone && docker compose up` has only the committed seed. You
cannot make their *live* state match without either shipping play
history (privacy + size disaster) or wiping native (destructive).

The answer is the extension's **Reset World** button. It wipes
accumulated state back to `initSettings()` defaults plus the
permanent residents (The Remnant, The Fortress, The Fold, the
fortress-interior gallery seed) and runs `doNewChat`. Those defaults
are identical on both stacks by construction — any divergence after
a reset is a real bug in `initSettings()` or in the reset path.

`ResetWorldParityTest.test_extension_state_matches_after_reset` is
the test that encodes this model:

1. For each enabled stack, open a fresh browser context.
2. Wait for the boot barrier (`#chat` present + networkidle).
3. Call `STPage.reset_world()` — clicks `#img-gen-end-story` and
   waits for `#img-gen-reset-overlay` to detach (the overlay is
   created synchronously on click and removed at the end of the
   5-second countdown + `doNewChat` cascade).
4. Snapshot `extension_settings.remnant` via
   `SillyTavern.getContext().extensionSettings.remnant`.
5. Strip `DRIFT_ALLOWLIST` keys (timestamps, `remnantMemory`,
   `playerArchive`).
6. Deep-diff every remaining leaf. Any divergence is a failure
   with the exact dotted path reported.

Driving Reset World from tests requires the button to be a single-
click action. As of v2.10.0, the two `window.confirm` dialogs that
used to gate End Story and Reset World are gone — the 5-second
in-overlay countdown is the only confirmation step, and it's
visual, not a modal gate. See `extension/index.js:3351-3394`.

## Critical code paths

- `extension/index.js:2436-2620` — `handleRunEnd()`, the shared
  soft/hard reset implementation. 5s overlay countdown, hard vs
  soft wipe, re-seed of permanent residents on hard reset,
  post-reset `doNewChat`.
- `extension/index.js:3351-3394` — the two button click handlers
  (`#img-gen-restart-story` = End Story soft,
  `#img-gen-end-story` = Reset World hard — the DOM ids are
  inverted relative to the button labels for historical reasons).
- `extension/index.js:104-340` — `initSettings()`, which is the
  source of truth for what "reset state" looks like. If you add a
  new seeded key here, `ResetWorldParityTest` starts asserting on
  it automatically.
- `tests/ui_parity/_browser.py` — `STPage` wrapper, `st_boot`
  context manager, `reset_world()` / `end_story()` drivers.
