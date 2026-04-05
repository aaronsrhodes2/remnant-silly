"""Shared Playwright helpers for UI-parity tests.

Runtime model
-------------
These tests load SillyTavern in a real (headless) Chromium so they can
assert on what the extension *actually renders* — chat messages, the
image-gallery panel, the codex/items panel, left-nav character slots,
and any console errors thrown during boot. HTTP-level parity tests
(tests/parity/) can only see server state; this suite is for DOM state.

Stack gating mirrors tests/parity/_common.py:

    REMNANT_TEST_NATIVE=1   → run against http://localhost:1580
    REMNANT_TEST_DOCKER=1   → run against http://localhost:1582

Both may be enabled; cross-stack tests iterate subTest(stack=...).

Dependency model
----------------
Requires:  pip install playwright && python -m playwright install chromium
See tests/ui_parity/README.md for the full one-time install.

Design notes
------------
- Sync API, not async: unittest is sync-first and we want simple test
  authoring without asyncio event loops. The cost is one browser
  process per test invocation, which is fine for a < 30 test suite.
- One Browser, many contexts: module-level fixture launches a single
  Chromium and hands out fresh BrowserContexts per test so cookies
  and localStorage don't leak between tests.
- Boot barrier: st_boot() waits until ST has finished mounting its
  extensions — specifically until #chat exists AND either a .mes
  element or the welcome system card is present. Extensions mount
  on the `app_ready` event which fires after this.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator, List

from playwright.sync_api import (
    Browser,
    BrowserContext,
    ConsoleMessage,
    Page,
    Playwright,
    sync_playwright,
)


ST_BASE_URLS = {
    "native": "http://localhost:1580",
    "docker": "http://localhost:1582",
}

# Keep in lockstep with tests/parity/_common.py — same envvars, same
# semantics — so a dev enabling both suites only flips two flags.
def enabled_stacks() -> List[str]:
    stacks: List[str] = []
    if os.environ.get("REMNANT_TEST_NATIVE") == "1":
        stacks.append("native")
    if os.environ.get("REMNANT_TEST_DOCKER") == "1":
        stacks.append("docker")
    return stacks


# Module-scoped Playwright + Browser. Started lazily on first use,
# reused across every test, and closed at interpreter exit. Avoids
# the ~500ms Chromium cold-start on every test method.
_playwright: Playwright | None = None
_browser: Browser | None = None


def _get_browser() -> Browser:
    global _playwright, _browser
    if _browser is not None:
        return _browser
    _playwright = sync_playwright().start()
    _browser = _playwright.chromium.launch(headless=True)
    import atexit

    def _shutdown():
        try:
            if _browser is not None:
                _browser.close()
        finally:
            if _playwright is not None:
                _playwright.stop()

    atexit.register(_shutdown)
    return _browser


class STPage:
    """Wrapper around playwright.Page with ST-specific helpers.

    Captures console messages and page errors as they happen so tests
    can assert 'no errors during boot' without having to hook listeners
    in every test.
    """

    def __init__(self, page: Page, stack: str):
        self.page = page
        self.stack = stack
        self.console_messages: List[ConsoleMessage] = []
        self.page_errors: List[str] = []
        page.on("console", lambda msg: self.console_messages.append(msg))
        page.on("pageerror", lambda err: self.page_errors.append(str(err)))

    # ---- raw console / error access ----

    def console_errors(self) -> List[str]:
        """Return text of every console message at level 'error'."""
        return [m.text for m in self.console_messages if m.type == "error"]

    def console_warnings(self) -> List[str]:
        return [m.text for m in self.console_messages if m.type == "warning"]

    # ---- DOM probes ----

    def chat_messages(self) -> list[dict]:
        """Return [{name, is_user, is_system, text}, ...] for every
        rendered .mes in the chat area."""
        return self.page.evaluate(
            """
            () => [...document.querySelectorAll('#chat .mes')].map(m => ({
                name: m.querySelector('.name_text')?.textContent || '',
                is_user: m.getAttribute('is_user') === 'true',
                is_system: m.getAttribute('is_system') === 'true',
                text: (m.querySelector('.mes_text')?.textContent || '').trim().slice(0, 500),
            }))
            """
        )

    def character_roster(self) -> list[str]:
        """Names of every character in the left-nav character panel."""
        return self.page.evaluate(
            """
            () => [...document.querySelectorAll('#rm_print_characters_block .character_select .ch_name')]
                .map(e => e.textContent.trim())
                .filter(Boolean)
            """
        )

    def extension_settings(self, key: str = "remnant") -> dict:
        """Dump the live in-page extension_settings[key]. This is the
        state the extension ACTUALLY sees after initSettings() has
        run — server-side /api/settings/get doesn't show the latest
        until saveSettingsDebounced() flushes.

        `extension_settings` is a module-scoped import in upstream ST
        and is NOT reliably on window. The public surface is
        `SillyTavern.getContext().extensionSettings` (or the
        `extension_settings` field on the same context object on
        older builds). Try both, fall back to window for dev builds
        that explicitly re-exposed it.
        """
        return self.page.evaluate(
            f"""
            () => {{
                try {{
                    const ctx = (window.SillyTavern && typeof window.SillyTavern.getContext === 'function')
                        ? window.SillyTavern.getContext()
                        : null;
                    const es = (ctx && (ctx.extensionSettings || ctx.extension_settings))
                        || window.extension_settings
                        || {{}};
                    return es[{key!r}] || null;
                }} catch (e) {{ return null; }}
            }}
            """
        )

    def gallery_images(self) -> list[dict]:
        """Gallery panel images currently rendered by the extension."""
        return self.page.evaluate(
            """
            () => [...document.querySelectorAll('#image-gen-gallery .gallery-thumb, .img-gen-panel .gallery-thumb')]
                .map(el => ({
                    src: el.querySelector('img')?.src?.split('?')[0]?.split('/').pop() || '',
                    caption: el.getAttribute('title') || '',
                }))
            """
        )

    def codex_item_names(self) -> list[str]:
        """Names of codex/items entries currently rendered."""
        return self.page.evaluate(
            """
            () => [...document.querySelectorAll('.codex-item-name, .item-entry .item-name, [data-codex-item]')]
                .map(e => e.textContent.trim())
                .filter(Boolean)
            """
        )

    def current_chat_id(self) -> str | None:
        return self.page.evaluate("() => window.currentChatId || null")

    def this_chid(self):
        return self.page.evaluate("() => window.this_chid")

    # ---- extension action drivers ----
    #
    # The extension exposes a `window.__remnantTest` namespace (see
    # extension/index.js around the v2.10.0 Test-API section) that lets
    # tests drive reset/end-story and read a normalized snapshot without
    # going through DOM clicks + the 5-second overlay countdown. These
    # helpers are thin wrappers around that namespace.

    def remnant_ready(self, timeout_ms: int = 15_000) -> None:
        """Wait until the extension's __remnantTest namespace is mounted
        and has finished initSettings(). Call once after st_boot() before
        reading state."""
        self.page.wait_for_function(
            "() => window.__remnantTest && typeof window.__remnantTest.ready === 'function'",
            timeout=timeout_ms,
        )
        self.page.evaluate("async () => await window.__remnantTest.ready()")

    def reset_world(self, timeout_ms: int = 30_000) -> dict:
        """Hard reset via the test-API. Returns the post-reset snapshot
        (drift keys already stripped at the source). Bypasses the 5s
        overlay countdown by calling handleRunEnd directly."""
        self.remnant_ready(timeout_ms=timeout_ms)
        return self.page.evaluate(
            "async () => await window.__remnantTest.resetWorld()",
        )

    def end_story(self, timeout_ms: int = 30_000) -> dict:
        """Soft End Story via the test-API. Returns the post-end-story
        snapshot."""
        self.remnant_ready(timeout_ms=timeout_ms)
        return self.page.evaluate(
            "async () => await window.__remnantTest.endStory()",
        )

    def remnant_snapshot(self, timeout_ms: int = 15_000) -> dict:
        """Current normalized extension snapshot — drift keys already
        stripped. This is the canonical readout path for parity tests."""
        self.remnant_ready(timeout_ms=timeout_ms)
        return self.page.evaluate("() => window.__remnantTest.snapshot()")

    def is_welcome_screen_showing(self) -> bool:
        """Welcome system card is visible and no real chat loaded."""
        return self.page.evaluate(
            """
            () => {
                const systemCards = [...document.querySelectorAll('#chat .mes[is_system="true"]')];
                return systemCards.length > 0 && (window.currentChatId === undefined || window.currentChatId === null);
            }
            """
        )


@contextmanager
def st_boot(stack: str, *, timeout_ms: int = 20_000) -> Iterator[STPage]:
    """Open `stack` in a fresh browser context and yield an STPage once
    SillyTavern has finished mounting its first screen.

    Boot barrier details:
      - Wait for the #chat DOM node to exist (ST's core layout has
        rendered).
      - Wait for networkidle (ST's /api/* chatter on boot has
        settled — settings fetch, characters list, extensions).
      - Give one extra paint tick for extension initSettings() to
        synchronously push its defaults into extension_settings.

    We intentionally do NOT wait on `window.characters` or
    `window.extension_settings` — both are module-scoped imports in
    upstream ST and aren't guaranteed to be on `window`. Tests that
    need those values go through the dedicated STPage helpers which
    read them via `page.evaluate()` after the barrier clears.
    """
    if stack not in ST_BASE_URLS:
        raise ValueError(f"unknown stack {stack!r}")
    browser = _get_browser()
    context: BrowserContext = browser.new_context()
    page = context.new_page()
    st = STPage(page, stack)
    try:
        page.goto(ST_BASE_URLS[stack], wait_until="domcontentloaded", timeout=timeout_ms)
        # Core DOM ready.
        page.wait_for_selector("#chat", timeout=timeout_ms)
        # ST fires a lot of XHRs on boot. Wait for them to stop.
        try:
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            # Some stacks keep a long-lived SSE / keepalive open that
            # prevents networkidle from ever firing. Fall through —
            # the selector + settle below is enough in practice.
            pass
        # One extra settle window for extension initSettings() +
        # welcome-screen resolve to finish writing state.
        page.wait_for_timeout(750)
        yield st
    finally:
        context.close()
