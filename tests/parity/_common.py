"""Shared helpers for parity tests. Stdlib only — no pytest, no requests.

Each stack under test is identified by a short key (`native`, `docker`)
and has a base URL at which the diag sidecar's `/ai.json` endpoint
lives. Tests opt in per stack via environment variables so a dev can
run against whichever stack they have up without failing the others.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional

# Base URLs — these match scripts/run-diag-native.sh and docker-compose.yml.
# Native: diag.py listens directly on :1580 (no nginx gateway in native dev).
# Docker: nginx on :1582 proxies /diagnostics/ai.json to the diag sidecar.
STACK_URLS = {
    "native": "http://localhost:1580",
    "docker": "http://localhost:1582/diagnostics",
}

# Env vars that opt a stack into the test run. Both default to off so
# a clean checkout with no services up produces a clean "all skipped"
# rather than a wall of failures.
STACK_ENV_FLAGS = {
    "native": "REMNANT_TEST_NATIVE",
    "docker": "REMNANT_TEST_DOCKER",
}


def enabled_stacks() -> list[str]:
    """Return the list of stacks the current env has opted into."""
    return [key for key, flag in STACK_ENV_FLAGS.items() if os.environ.get(flag) == "1"]


def ai_json_url(stack: str) -> str:
    base = STACK_URLS[stack]
    # Docker stack exposes /diagnostics/ai.json; native exposes /ai.json.
    # The diag sidecar itself always serves at /ai.json — the difference
    # is the nginx rewrite on the docker side.
    if stack == "docker":
        return f"{base}/ai.json"
    return f"{base}/ai.json"


def actions_url(stack: str) -> str:
    base = STACK_URLS[stack]
    return f"{base}/actions"


def fetch_json(url: str, timeout: float = 5.0) -> Optional[dict]:
    """Fetch JSON from a URL. Returns parsed dict or None on any error.

    None lets callers distinguish "stack not up" from "stack up but
    broken" without wrapping every call in try/except.
    """
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            data = r.read()
        return json.loads(data.decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ConnectionError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------
# SillyTavern HTTP surface helpers
# ---------------------------------------------------------------------
#
# The diag sidecar exposes its JSON at /diagnostics/* behind nginx on
# docker and directly on :1580 on native. SillyTavern itself is served
# at the root path on both stacks — these helpers target *ST*, not
# diag, so tests can assert that the character roster, world list, API
# backend, and extension static assets match across environments.

ST_BASE_URLS = {
    "native": "http://localhost:1580",
    "docker": "http://localhost:1582",
}


def st_base_url(stack: str) -> str:
    return ST_BASE_URLS[stack]


def _st_session(stack: str) -> tuple[urllib.request.OpenerDirector, Optional[str]]:
    """Open a fresh session against ST: fetches /csrf-token (which sets
    the session cookie) and returns an opener wired to the cookie jar
    plus the CSRF token the same session expects on its POST bodies.

    ST's CSRF middleware binds the token to the session cookie — the
    token returned by /csrf-token is only valid for requests carrying
    the cookie jar that received it. Each call here is a fresh jar, so
    tests get independent sessions.
    """
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        with opener.open(f"{st_base_url(stack)}/csrf-token", timeout=5.0) as r:
            if r.status != 200:
                return opener, None
            data = json.loads(r.read().decode("utf-8"))
        return opener, data.get("token")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            ConnectionError, json.JSONDecodeError):
        return opener, None


def st_post_json(stack: str, path: str, body: dict, timeout: float = 10.0) -> Optional[dict]:
    """POST a JSON body to an ST API path with session cookie + CSRF.

    Returns parsed JSON on success, or None on any error — same
    contract as fetch_json so callers can distinguish "stack not up"
    from "stack up but broken" by checking for None.
    """
    opener, token = _st_session(stack)
    if not token:
        return None
    url = f"{st_base_url(stack)}{path}"
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": token,
                "Accept": "application/json",
            },
            method="POST",
        )
        with opener.open(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            ConnectionError, json.JSONDecodeError):
        return None


def st_get_settings(stack: str) -> Optional[dict]:
    """Fetch and parse the user's ST settings.json via /api/settings/get.

    ST returns the file as a JSON-encoded string under the top-level
    "settings" key; callers want the parsed object. Also exposes the
    sibling fields (world_names, themes, etc.) on the return dict
    under a "_meta" key, so tests asserting on worlds don't need a
    second round-trip.
    """
    envelope = st_post_json(stack, "/api/settings/get", {})
    if not envelope or not isinstance(envelope, dict):
        return None
    raw = envelope.get("settings")
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    parsed["_meta"] = {k: v for k, v in envelope.items() if k != "settings"}
    return parsed


def st_fetch_bytes(stack: str, path: str, timeout: float = 5.0) -> Optional[bytes]:
    """GET raw bytes from an ST URL. Used for static-asset parity."""
    try:
        url = f"{st_base_url(stack)}{path}"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read() if r.status == 200 else None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ConnectionError):
        return None


def poll_until_healthy(stack: str, timeout_s: float = 120.0, interval_s: float = 2.0) -> Optional[dict]:
    """Block until the stack's ai.json reports HEALTHY or timeout.

    Returns the final snapshot on HEALTHY, or the last snapshot on
    timeout (so tests can inspect what state it got stuck in), or None
    if the endpoint never responded at all.
    """
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        snapshot = fetch_json(ai_json_url(stack))
        if snapshot is not None:
            last = snapshot
            summary = (snapshot.get("summary") or "").upper()
            if summary.startswith("HEALTHY"):
                return snapshot
        time.sleep(interval_s)
    return last
