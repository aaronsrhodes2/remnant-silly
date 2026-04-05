"""Shared helpers for parity tests. Stdlib only — no pytest, no requests.

Each stack under test is identified by a short key (`native`, `docker`)
and has a base URL at which the diag sidecar's `/ai.json` endpoint
lives. Tests opt in per stack via environment variables so a dev can
run against whichever stack they have up without failing the others.
"""

from __future__ import annotations

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
