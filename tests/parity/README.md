# Parity tests

Zero-dependency (stdlib only) tests that assert the **native dev stack
and the Docker stack present identical diagnostic surfaces**. This is
the invariant that lets Fortress Senses and the speaking gate work the
same way in both environments.

## What they assert

### Diag sidecar parity

1. **Schema parity** (`test_ai_json_schema.py`) — `/ai.json` matches a
   frozen shape: required top-level keys, enum values for `phase` and
   `severity`, action catalog structure. Catches regressions like
   field renames, type drift, or missing keys.
2. **Readiness convergence** (`test_readiness_convergence.py`) — both
   stacks (if running) reach `summary.startswith("HEALTHY")` within a
   timeout, and the set of service keys and detected_issue codes match
   across them. This is the actual "native ≡ docker" assertion.
3. **Action catalog parity** (`test_action_catalog_parity.py`) — the
   set of allowlisted action IDs is identical across stacks. Adding a
   new action to `docker/diag/app.py` forces this test to pass against
   both stacks before the change can merge.

### SillyTavern surface parity (`test_sillytavern_surface_parity.py`)

Exercises ST itself, not the diag sidecar. Hits ST's own JSON API with
a cookie-backed session + CSRF token and asserts:

4. **Character roster** — the canonical Remnant cards (The Remnant,
   The Fortress, Narrator, Jeremy Smythe, Sherri) are installed on
   both stacks.
5. **World books** — Eldoria and Nullspace Nexus are both loadable.
6. **API backend** — `main_api` is **not** `koboldhorde`/`kobold`/
   `horde`. Catches the "fresh docker boot 500s on Horde" regression.
7. **Active character** — `active_character` points at The Remnant on
   boot, defeating the welcome-screen gate at
   `scripts/welcome-screen.js:175` that would otherwise paint the
   generic "Assistant" stub card.
8. **Extension static assets** — `scripts/extensions/image-generator/
   assets/fortress-interior.jpg` serves with the expected byte count,
   and the bytes match across stacks when both are up. Catches the
   `image-generator/` vs `remnant/` URL-path bug from an earlier v2.9.x
   build.

## How to run

Tests opt-in per stack via environment variables:

    # Native only (scripts/run-diag-native.sh must be running)
    REMNANT_TEST_NATIVE=1 python -m unittest discover -s tests/parity -t .

    # Docker only (docker compose up must be running)
    REMNANT_TEST_DOCKER=1 python -m unittest discover -s tests/parity -t .

    # Both — parity tests actually assert equivalence
    REMNANT_TEST_NATIVE=1 REMNANT_TEST_DOCKER=1 python -m unittest discover -s tests/parity -t .

If neither variable is set the tests all skip. If only one is set, the
single-stack tests run and the cross-stack convergence test is skipped.

## URLs under test

**Diag sidecar:**
- Docker:  `http://localhost:1582/diagnostics/ai.json`   (via nginx gateway)
- Native:  `http://localhost:1580/ai.json`               (direct to diag.py)

**SillyTavern:**
- Docker:  `http://localhost:1582/`                      (via nginx gateway)
- Native:  `http://localhost:1580/`                      (direct to ST)

Both endpoints are served by the exact same file (`docker/diag/app.py`).
The tests exist to catch the one thing that file cannot self-verify:
that the *environment* around it is wired up identically.

## Dependencies

**None.** Uses only `unittest`, `urllib.request`, `json`, and `os`.
No pytest, no requests, no fixtures package. Runs under any Python 3.8+.
