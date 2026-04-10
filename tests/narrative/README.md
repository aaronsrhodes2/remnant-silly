# Narrative Tests

Automated playthrough tests for Remnant. Drive the game through scripted scenarios
and validate outputs — structural checks run instantly; fuzzy LLM assertions use
a local Ollama model.

## Requirements

- Native stack running: `bash scripts/native-up.sh`
- SillyTavern open in a browser tab (extension relay requires it)
- Ollama running with at least one text model (e.g. mistral:latest)

## Running

```bash
# Structural only — fast, no LLM calls (Sorting Hat + response count checks):
cd tests/narrative
REMNANT_TEST_NATIVE=1 python -m unittest test_scenarios -v

# Full with LLM judge (one Ollama call per fuzzy assertion, ~1-2s each):
REMNANT_TEST_NATIVE=1 REMNANT_JUDGE=1 python -m unittest test_scenarios -v

# Single test class:
REMNANT_TEST_NATIVE=1 python -m unittest test_scenarios.SortingHatTest -v

# Per-turn timeout (default 45s — increase if your LLM is slow):
REMNANT_TEST_NATIVE=1 REMNANT_TIMEOUT=90 python -m unittest test_scenarios -v
```

## Test Classes

| Class | Turns | Speed | What it checks |
|-------|-------|-------|----------------|
| `SortingHatTest` | 0 (classify only) | Fast | Sorting Hat intent + wrapping accuracy |
| `OnboardingTest` | 5 | ~3-5 min | New player: look/sniff/speak/act/ask; world graph |
| `SensorySceneTest` | 4 | ~2-4 min | All sense types; sequential coherence |
| `NpcIntroductionTest` | 2 | ~1-2 min | NPC encounter; world-graph entity creation |

`SortingHatTest` is always safe to run — it sends inputs but doesn't wait for
narrator responses, so it completes in a few seconds.

## What's Tested

### Structural (always on)
- Response received within timeout
- Response non-empty (`raw_text` + `parsed_blocks`)
- No player impersonation warnings
- Sensory markers present where expected (`markers_found`)
- Sorting Hat intent classification
- World graph entity creation

### Fuzzy (REMNANT_JUDGE=1)
- "Does this response describe an environment?" — Ollama yes/no
- "Does this response reference the player's action?" — Ollama yes/no
- "Does turn N follow naturally from turn N-1?" — sequential coherence

## What Can't Be Automated

- Creative quality / tone
- Whether the Fortress *feels* atmospheric
- Novel unexpected responses (these are features, not bugs)

These are human-review only. Run the tests to catch regressions; play the game
to evaluate quality.

## Adding Scenarios

Add a new test class to `test_scenarios.py` inheriting from `NarrativeTestCase`.
Each test method calls `self.play(text)` which:
1. POSTs to `/player-input` via nginx
2. Waits for a narrator response (up to `REMNANT_TIMEOUT` seconds)
3. Asserts content is non-empty and no player impersonation occurred
4. Returns `(player_result, narrator_turn)` for further assertions
