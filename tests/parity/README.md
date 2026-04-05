# Parity tests

Zero-dependency (stdlib only) tests that assert the **native dev stack
and the Docker stack present identical diagnostic surfaces**. This is
the invariant that lets Fortress Senses and the speaking gate work the
same way in both environments.

## What they assert

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

## How to run

Tests opt-in per stack via environment variables:

    # Native only (scripts/run-diag-native.sh must be running)
    REMNANT_TEST_NATIVE=1 python -m unittest discover tests/parity

    # Docker only (docker compose up must be running)
    REMNANT_TEST_DOCKER=1 python -m unittest discover tests/parity

    # Both — parity tests actually assert equivalence
    REMNANT_TEST_NATIVE=1 REMNANT_TEST_DOCKER=1 python -m unittest discover tests/parity

If neither variable is set the tests all skip. If only one is set, the
single-stack tests run and the cross-stack convergence test is skipped.

## URLs under test

- Docker:  `http://localhost:1582/diagnostics/ai.json`   (via nginx gateway)
- Native:  `http://localhost:1580/ai.json`               (direct to diag.py)

Both endpoints are served by the exact same file (`docker/diag/app.py`).
The tests exist to catch the one thing that file cannot self-verify:
that the *environment* around it is wired up identically.

## Dependencies

**None.** Uses only `unittest`, `urllib.request`, `json`, and `os`.
No pytest, no requests, no fixtures package. Runs under any Python 3.8+.
