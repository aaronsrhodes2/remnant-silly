"""Action catalog parity — asserts the set of remediation actions
exposed by each stack is identical.

Adding a new action to docker/diag/app.py's `_action_catalog()` should
fail this test against any stack that hasn't rolled out the change.
That's the point: the remediation surface is a contract that an AI
agent or human dev picks an id from, and silent drift between stacks
would let a doc/code/agent reference become invalid for one but not
the other.

Two assertions:
  1. For each enabled stack, `/actions` returns a catalog that matches
     the one embedded in `/ai.json.action_catalog` on the same stack.
  2. If both stacks are enabled, the action IDs match across stacks.
"""

import unittest

from ._common import enabled_stacks, ai_json_url, actions_url, fetch_json


def _action_ids(catalog):
    return sorted(entry["id"] for entry in catalog)


class ActionCatalogParityTest(unittest.TestCase):

    def test_catalog_consistent_between_endpoints(self):
        stacks = enabled_stacks()
        if not stacks:
            self.skipTest("no REMNANT_TEST_{NATIVE,DOCKER}=1 set")
        for stack in stacks:
            with self.subTest(stack=stack):
                ai = fetch_json(ai_json_url(stack))
                actions = fetch_json(actions_url(stack))
                self.assertIsNotNone(ai, f"{stack}: /ai.json returned nothing")
                self.assertIsNotNone(actions, f"{stack}: /actions returned nothing")

                # /actions returns {"actions": [...]}.
                self.assertIn("actions", actions, f"{stack}: /actions missing 'actions' key")
                from_actions_endpoint = _action_ids(actions["actions"])
                from_ai_json = _action_ids(ai["action_catalog"])

                self.assertEqual(
                    from_actions_endpoint, from_ai_json,
                    f"{stack}: /actions vs /ai.json.action_catalog drift: "
                    f"{from_actions_endpoint} vs {from_ai_json}",
                )

    def test_action_ids_match_across_stacks(self):
        stacks = enabled_stacks()
        if len(stacks) < 2:
            self.skipTest("need both REMNANT_TEST_NATIVE=1 and REMNANT_TEST_DOCKER=1 for cross-stack parity")

        catalogs = {}
        for stack in stacks:
            ai = fetch_json(ai_json_url(stack))
            self.assertIsNotNone(ai, f"{stack}: /ai.json returned nothing")
            catalogs[stack] = _action_ids(ai["action_catalog"])

        reference_key = list(catalogs.keys())[0]
        reference = catalogs[reference_key]
        for stack, ids in catalogs.items():
            self.assertEqual(
                ids, reference,
                f"action id sets differ: {stack}={ids} vs {reference_key}={reference}",
            )


if __name__ == "__main__":
    unittest.main()
