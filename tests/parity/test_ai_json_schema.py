"""Schema parity test — asserts /ai.json matches a frozen shape on
every enabled stack.

This test catches regressions like:
  - field renames (bytes_total -> total_bytes)
  - type drift (list becoming dict)
  - missing required keys
  - new enum values for phase/severity that the extension can't handle

Running it against multiple stacks in one invocation is how we catch
divergence between native dev and docker. It never compares stacks to
each other directly — that's `test_readiness_convergence`. This file
just validates that each stack *individually* presents the contract.
"""

import unittest

from ._common import enabled_stacks, ai_json_url, fetch_json

# Valid enum values. Extend these lists when legitimate new values are
# added to docker/diag/app.py — the test will then fail-loud until both
# stacks roll out the change, which is exactly the safety net we want.
VALID_PHASES = {"downloading", "ready", "error", None}
VALID_SEVERITIES = {"error", "warning"}

# Top-level keys required by the extension's Fortress Senses consumer.
REQUIRED_TOP_LEVEL = {
    "schema_version",
    "generated_at",
    "summary",
    "services",
    "sentinels",
    "recent_log",
    "detected_issues",
    "suggested_action_ids",
    "action_catalog",
    "environment",
}

# Service keys the extension's faculty-name table knows about. Adding a
# new service is a deliberate act that should update the table AND this
# set together.
EXPECTED_SERVICE_KEYS = {"flask-sd", "ollama", "sillytavern"}


class AiJsonSchemaTest(unittest.TestCase):
    """One test method per enabled stack. unittest's TestCase doesn't
    parametrize cleanly across stdlib versions, so we loop inside each
    test and report which stack failed in the assertion message."""

    def test_schema_shape(self):
        stacks = enabled_stacks()
        if not stacks:
            self.skipTest("no REMNANT_TEST_{NATIVE,DOCKER}=1 set")

        for stack in stacks:
            with self.subTest(stack=stack):
                url = ai_json_url(stack)
                snapshot = fetch_json(url)
                self.assertIsNotNone(
                    snapshot, f"{stack}: GET {url} returned nothing — is the stack up?"
                )

                # Top-level shape.
                missing = REQUIRED_TOP_LEVEL - set(snapshot.keys())
                self.assertFalse(missing, f"{stack}: missing top-level keys {missing}")

                # schema_version is an integer we can pin later.
                self.assertIsInstance(snapshot["schema_version"], int,
                                      f"{stack}: schema_version must be int")

                # summary must be a non-empty string starting with one of
                # the known state words.
                summary = snapshot["summary"]
                self.assertIsInstance(summary, str, f"{stack}: summary must be str")
                self.assertTrue(
                    any(summary.upper().startswith(prefix)
                        for prefix in ("HEALTHY", "WARNING", "DEGRADED", "STARTING")),
                    f"{stack}: unexpected summary prefix: {summary!r}",
                )

                # services keys.
                services = snapshot["services"]
                self.assertIsInstance(services, dict, f"{stack}: services must be dict")
                self.assertSetEqual(
                    set(services.keys()), EXPECTED_SERVICE_KEYS,
                    f"{stack}: services keys mismatch — extension's faculty table needs update",
                )

                # Per-service shape.
                for key, svc in services.items():
                    self.assertIn("status_file", svc, f"{stack}:{key} missing status_file")
                    self.assertIn("probe", svc, f"{stack}:{key} missing probe")
                    probe = svc["probe"]
                    self.assertIn("reachable", probe, f"{stack}:{key} probe missing reachable")
                    self.assertIsInstance(probe["reachable"], bool,
                                          f"{stack}:{key} probe.reachable not bool")
                    sf = svc["status_file"]
                    if sf is not None:
                        self.assertIn("phase", sf, f"{stack}:{key} status_file missing phase")
                        self.assertIn(sf.get("phase"), VALID_PHASES,
                                      f"{stack}:{key} phase={sf.get('phase')!r} not in {VALID_PHASES}")

                # detected_issues severities are bounded.
                for issue in snapshot["detected_issues"]:
                    self.assertIn("severity", issue, f"{stack}: issue missing severity")
                    self.assertIn(issue["severity"], VALID_SEVERITIES,
                                  f"{stack}: severity={issue['severity']!r} not in {VALID_SEVERITIES}")
                    self.assertIn("code", issue, f"{stack}: issue missing code")
                    self.assertIn("message", issue, f"{stack}: issue missing message")

                # action_catalog is a list of dicts with required fields.
                catalog = snapshot["action_catalog"]
                self.assertIsInstance(catalog, list, f"{stack}: action_catalog must be list")
                self.assertGreater(len(catalog), 0, f"{stack}: action_catalog empty")
                for entry in catalog:
                    for field in ("id", "summary", "params", "side_effects", "risk", "requires_host"):
                        self.assertIn(field, entry, f"{stack}: action {entry.get('id')} missing {field}")


if __name__ == "__main__":
    unittest.main()
