"""Readiness convergence — the actual "native == docker" assertion.

When BOTH stacks are enabled in one test run, this test:
  1. Polls each stack until its ai.json reports HEALTHY (or timeout).
  2. Compares the sets of service keys and detected_issue codes.
  3. Asserts they match exactly.

If only one stack is enabled, the single-stack test runs as a smoke
test (reaches HEALTHY at all) and the cross-stack comparison is
skipped. If neither is enabled, everything skips.

This is the test that catches drift — e.g. a service key existing in
docker but not in native because someone forgot to start flask-sd
locally, or a detected_issue firing on docker because the bootstrap
sentinel wasn't persisted to the named volume.
"""

import unittest

from ._common import enabled_stacks, poll_until_healthy

# Bootstrap cold paths (first docker run, downloading models) can take
# 10+ minutes. The per-stack readiness test is the long one; tune up
# via an env var if you're actually running a cold boot.
import os
READINESS_TIMEOUT_S = float(os.environ.get("REMNANT_READINESS_TIMEOUT_S", "120"))


class ReadinessConvergenceTest(unittest.TestCase):

    def test_each_stack_reaches_healthy(self):
        stacks = enabled_stacks()
        if not stacks:
            self.skipTest("no REMNANT_TEST_{NATIVE,DOCKER}=1 set")
        for stack in stacks:
            with self.subTest(stack=stack):
                snapshot = poll_until_healthy(stack, timeout_s=READINESS_TIMEOUT_S)
                self.assertIsNotNone(
                    snapshot, f"{stack}: diag endpoint never responded",
                )
                summary = (snapshot.get("summary") or "").upper()
                self.assertTrue(
                    summary.startswith("HEALTHY"),
                    f"{stack}: never reached HEALTHY in {READINESS_TIMEOUT_S}s. "
                    f"Final summary={snapshot.get('summary')!r}, "
                    f"detected_issues={[i.get('code') for i in snapshot.get('detected_issues', [])]}",
                )

    def test_stacks_converge_to_same_shape(self):
        stacks = enabled_stacks()
        if len(stacks) < 2:
            self.skipTest("need both REMNANT_TEST_NATIVE=1 and REMNANT_TEST_DOCKER=1 for cross-stack parity")

        snapshots = {}
        for stack in stacks:
            snap = poll_until_healthy(stack, timeout_s=READINESS_TIMEOUT_S)
            self.assertIsNotNone(snap, f"{stack}: diag endpoint never responded")
            snapshots[stack] = snap

        # 1. Same set of service keys. The in-lore faculty table in the
        #    extension depends on this being stable across stacks.
        service_sets = {s: set(snap["services"].keys()) for s, snap in snapshots.items()}
        reference_key = list(service_sets.keys())[0]
        reference_services = service_sets[reference_key]
        for stack, services in service_sets.items():
            self.assertSetEqual(
                services, reference_services,
                f"service keys differ: {stack}={services} vs {reference_key}={reference_services}",
            )

        # 2. Same set of detected_issue codes. If one stack reports an
        #    issue the other doesn't, that's the whole point of parity
        #    testing — surface the divergence loud and early.
        issue_sets = {
            s: sorted([i.get("code") for i in snap.get("detected_issues", [])])
            for s, snap in snapshots.items()
        }
        reference_issues = issue_sets[reference_key]
        for stack, issues in issue_sets.items():
            self.assertEqual(
                issues, reference_issues,
                f"detected_issue codes differ: {stack}={issues} vs {reference_key}={reference_issues}",
            )

        # 3. Same schema_version — a bumped schema must roll out to both
        #    stacks at once.
        versions = {s: snap["schema_version"] for s, snap in snapshots.items()}
        reference_version = versions[reference_key]
        for stack, version in versions.items():
            self.assertEqual(
                version, reference_version,
                f"schema_version differs: {stack}={version} vs {reference_key}={reference_version}",
            )


if __name__ == "__main__":
    unittest.main()
