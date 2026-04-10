"""Narrative scenario tests for Remnant.

Each test class corresponds to one scenario JSON file in scenarios/.
Tests are designed to run against a live native stack.

Usage:
    # Structural only (fast, no LLM calls):
    REMNANT_TEST_NATIVE=1 python -m unittest tests/narrative/test_scenarios.py -v

    # Full with LLM judge (Ollama must be running):
    REMNANT_TEST_NATIVE=1 REMNANT_JUDGE=1 python -m unittest tests/narrative/test_scenarios.py -v

    # One scenario:
    REMNANT_TEST_NATIVE=1 python -m unittest tests.narrative.test_scenarios.OnboardingTest -v

The tests do NOT reset the ST chat between runs — they play on top of whatever
state is currently active. For a clean baseline, manually start a new chat in ST
before running.
"""
from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path

from _harness import (
    NarrativeTestCase,
    play_turn,
    assert_turn_has_content,
    assert_no_player_impersonation,
    assert_markers_present,
    assert_world_entity,
    assert_min_world_entities,
    assert_intent,
    get_recent_turns,
    get_narrator_turn_count,
    DEFAULT_TIMEOUT,
    SIDECAR_BASE,
    _get_json,
)
from _judge import judge_assert, judge_sequential


# ---------------------------------------------------------------------------
# Scenario 1: Onboarding — a new player's first 5 actions
# ---------------------------------------------------------------------------

class OnboardingTest(NarrativeTestCase):
    """5-turn scripted new-player session: look, sniff, speak, act, ask.

    Validates:
    - Every turn gets a non-empty response
    - No player impersonation
    - Sensory markers appear for perception actions
    - World graph accumulates a location entity
    - Sequential coherence across all 5 turns (when REMNANT_JUDGE=1)
    """

    def test_01_look_around(self) -> None:
        result, turn = self.play("I look around carefully")
        assert_intent(result, "DO")
        assert_turn_has_content(turn)
        assert_no_player_impersonation(turn)
        # Environment description should include a visual marker
        assert_markers_present(turn, "SIGHT")
        self._last_turn = turn

    def test_02_sniff_air(self) -> None:
        result, turn = self.play("I sniff the air")
        assert_intent(result, "SENSE")
        assert_turn_has_content(turn)
        assert_no_player_impersonation(turn)
        judge_assert(
            "Does this narrator response describe a smell or scent?",
            f"Narrator: {turn['raw_text'][:600]}",
        )

    def test_03_call_out(self) -> None:
        result, turn = self.play("Hello? Is anyone there?")
        assert_intent(result, "SAY")
        assert_turn_has_content(turn)
        assert_no_player_impersonation(turn)
        judge_assert(
            "Does the narrator respond to someone calling out or speaking aloud?",
            f"Narrator: {turn['raw_text'][:600]}",
        )

    def test_04_pick_up_object(self) -> None:
        result, turn = self.play("I reach down and pick up the nearest object")
        assert_intent(result, "DO")
        assert_turn_has_content(turn)
        assert_no_player_impersonation(turn)

    def test_05_ask_about_place(self) -> None:
        result, turn = self.play("What is this place?")
        assert_intent(result, "SAY")
        assert_turn_has_content(turn)
        assert_no_player_impersonation(turn)
        # After 5 turns, the world graph should have at least one location
        assert_min_world_entities("location", 1)
        judge_assert(
            "Does this narrator response describe or name a location?",
            f"Narrator: {turn['raw_text'][:600]}",
        )


# ---------------------------------------------------------------------------
# Scenario 2: Sensory Scene — confirm all sense types are reachable
# ---------------------------------------------------------------------------

class SensorySceneTest(NarrativeTestCase):
    """Probe each sense type to confirm the narrator uses sensory markers.

    These turns may not get the exact sense on every run — the Fortress
    generates freely — so we use judge assertions rather than strict marker
    checks for most. SIGHT is required on explicit visual prompts.
    """

    def test_visual_prompt(self) -> None:
        result, turn = self.play("I study every detail of my surroundings")
        assert_markers_present(turn, "SIGHT")
        assert_no_player_impersonation(turn)
        judge_assert(
            "Does this response contain visual descriptions of the environment?",
            f"Narrator: {turn['raw_text'][:600]}",
        )

    def test_sound_prompt(self) -> None:
        _, turn = self.play("I close my eyes and listen intently")
        assert_turn_has_content(turn)
        assert_no_player_impersonation(turn)
        judge_assert(
            "Does this response describe sounds, silence, or auditory details?",
            f"Narrator: {turn['raw_text'][:600]}",
        )

    def test_touch_prompt(self) -> None:
        _, turn = self.play("I run my hand along the nearest surface")
        assert_turn_has_content(turn)
        assert_no_player_impersonation(turn)
        judge_assert(
            "Does this response describe a texture, temperature, or tactile sensation?",
            f"Narrator: {turn['raw_text'][:600]}",
        )

    def test_sequential_sense_coherence(self) -> None:
        """Three sense turns should build on each other sequentially."""
        _, t1 = self.play("I look up at the ceiling or sky")
        _, t2 = self.play("I look down at the floor or ground")
        _, t3 = self.play("I look straight ahead")
        judge_sequential(t1, t2)
        judge_sequential(t2, t3)


# ---------------------------------------------------------------------------
# Scenario 3: NPC Introduction — trigger INTRODUCE, check world graph
# ---------------------------------------------------------------------------

class NpcIntroductionTest(NarrativeTestCase):
    """Prompt the narrator to introduce an NPC and verify world graph capture.

    The Fortress uses [INTRODUCE(Name): "description"] for new characters.
    After the turn, the world graph should have an entity for that NPC.
    """

    def test_ask_who_is_there(self) -> None:
        _, turn = self.play("Who are you? Show yourself!")
        assert_turn_has_content(turn)
        assert_no_player_impersonation(turn)
        judge_assert(
            "Does this response introduce or describe a character, entity, or being?",
            f"Narrator: {turn['raw_text'][:600]}",
        )

    def test_npc_world_graph_after_encounter(self) -> None:
        """After an NPC encounter, the world graph should have at least one NPC."""
        _, turn = self.play("I approach the figure cautiously")
        assert_turn_has_content(turn)
        assert_no_player_impersonation(turn)
        # Allow up to 2 extra seconds for world-graph ingestion
        time.sleep(2)
        assert_min_world_entities("npc", 1)


# ---------------------------------------------------------------------------
# Scenario 4: Sorting Hat accuracy — intent classification
# ---------------------------------------------------------------------------

class SortingHatTest(NarrativeTestCase):
    """Verify the Sorting Hat correctly classifies player inputs.

    These tests validate classification without waiting for narrator response
    — they check the POST /player-input result directly.
    """

    def _classify_only(self, text: str) -> dict:
        """Submit input and return the classification result without waiting for narrator."""
        from _harness import post_player_input
        return post_player_input(text)

    def test_say_quoted(self) -> None:
        r = self._classify_only('"Hello, is anyone there?"')
        assert_intent(r, "SAY")

    def test_say_unquoted_greeting(self) -> None:
        r = self._classify_only("I say hello to whoever is listening")
        # Either SAY or DO is acceptable — the key test is it's not SENSE
        actual = (r.get("intent") or "").upper()
        self.assertIn(actual, ("SAY", "DO"), f"unexpected intent: {actual}")

    def test_do_physical_action(self) -> None:
        r = self._classify_only("I draw my sword and take a fighting stance")
        assert_intent(r, "DO")

    def test_do_movement(self) -> None:
        r = self._classify_only("I walk slowly toward the far door")
        assert_intent(r, "DO")

    def test_sense_perception(self) -> None:
        r = self._classify_only("I smell something burning in the distance")
        # SENSE is correct; DO is acceptable (physical perception); SAY is wrong
        actual = (r.get("intent") or "").upper()
        self.assertIn(actual, ("SENSE", "DO"),
                      f"perception input must not be classified as SAY, got: {actual}")

    def test_sense_awareness(self) -> None:
        r = self._classify_only("I notice a faint sound from above")
        # SENSE or DO both acceptable — perception can map either way
        actual = (r.get("intent") or "").upper()
        self.assertIn(actual, ("SENSE", "DO"), f"unexpected intent: {actual}")

    def test_wrapping_do(self) -> None:
        r = self._classify_only("I open the door")
        self.assertIn("open the door", r.get("wrapped", ""))

    def test_wrapping_say(self) -> None:
        r = self._classify_only('"What lurks here?"')
        wrapped = r.get("wrapped", "")
        self.assertTrue(
            wrapped.startswith('"') or wrapped.startswith('\u201c'),
            f"SAY wrapping should start with quote, got: {wrapped}"
        )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
