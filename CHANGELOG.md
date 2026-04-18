# Changelog

## [4.1.6] — 2026-04-18

### Features
- **Narrator-state input bar** — `#player-input` border color signals narrator readiness at a glance: green glow when idle (response will be immediate), amber when busy (will be queued), amber pulse animation when input is submitted while narrator is mid-turn. VAD speaking state (pulsing orange) overrides via CSS cascade.
- **Status bar "Ready" glow** — status dot now shows a breathing green animation and green text label when the game is idle and waiting for player input; replaces the near-invisible white ghost it showed before.
- **`/ready` poll fix** — narrator/llm lights now correctly reflect live generation state. Previous code read `rd.state` (always `undefined`); fixed to `rd.generating` / `rd.classifying` boolean fields.
- **Server-queued turn indicator** — when the fortress fires a queued narrator turn server-side (bypassing `sendInput()`), the `· · ·` thinking indicator is now injected automatically on the first `chunk` SSE event.

### Upgrade
- **STT model `medium.en`** — Whisper upgraded from `small.en` (461 MB) to `medium.en` (~1.5 GB) for substantially better transcription accuracy. Requires one-time bootstrap: `docker compose --profile bootstrap up bootstrap-stt`.

---

## [4.1.5] — 2026-04-18

### Fixes
- **TTS/STT lights no longer go black** — lights show their color at dormant glow when the service key is absent from the health snapshot, rather than going dark. Only a confirmed down service triggers the dark `off` state.

### Features (4.1.3 → 4.1.5)
- **12-light status bar** — gate|conn / narr|llm|img|musc|tts|stt / gpu|vram|cpu|ram in three visual groups with separators. Each light wired to its specific event source (SSE for real-time busy/active, 3s poll for idle/off).
- **Fortress generation state** — `/ready` endpoint polled in parallel; narr/llm lights show `generating` or `classifying` state between SSE events.
- **Deep service health** — `/diagnostics/ai.json` probes Ollama via `/api/tags`, TTS via `/health`, STT via root reachability. `reachable=false` means the service didn't answer a real request.
- **Crisis simulator** — `_testCrisis()` / `_testCrisisReset()` in browser console cycles 6 diagnostic scenarios.
- **Toggle groups** — clicking `musc` or `tts` toggles the whole audio sense together. Right-click `musc` skips to next track.

---

## [4.1.2] — 2026-04-18

### Fixes
- **Feed display bugs** — `DRESSED` and `AVATAR [base64]` no longer appear as raw text entries in the chat feed. `player_dressed` and `player_portrait` meta events are now silent (side effects still run: portrait map updated, avatar `img` tags backfilled).
- **Player name in feed** — Optimistic player input entries now show the known player name (e.g. `RONNY`) instead of the hardcoded `YOU`.

### Features
- **Music inactivity fade** — After 2 minutes of no player/narrator activity the ambient music fades to silence over 3 minutes, then the audio source is stopped. On the next activity (player input or narrator response) music fades back up over 8 seconds; if the source had been fully stopped the current mood track restarts automatically.
- **STT accuracy** — Upgraded Whisper model from `base.en` (74 MB) to `small.en` (461 MB). Requires one-time `docker compose --profile bootstrap up bootstrap-stt` to pull the new model.
- **VAD focus gate** — Microphone no longer records speech when the game window does not have browser focus.
- **VAD trailing silence** — Silence window extended from 1 400 ms to 1 800 ms; fewer clipped words at the end of utterances.
- **SFX keyword coverage** — Static SFX library keywords dramatically expanded to cover injected fallback descriptions (hydraulic hiss, automaton footfalls, ambient machinery hum, galley sounds, alarm pulses, etc.).

---

## [4.1.1] — 2026-04-18

### Fixes
- **Voice input (STT) now works end-to-end.** The `onerahmet/openai-whisper-asr-webservice` container was crash-looping on startup because play-net (runtime network) has no egress and the image tried to download the Whisper model from `openaipublic.azureedge.net` on every boot.
  - Added `whisper-model` named Docker volume to cache the model between restarts.
  - Added `bootstrap-stt` service (profile: `bootstrap`) on `bootstrap-net` that pre-downloads `base.en` (139 MB) once and stamps `/remnant-status/stt-ready`.
  - Fixed JS endpoint: was calling `/api/stt/v1/audio/transcriptions` (OpenAI API format); the service only exposes `/asr`. Updated to `/api/stt/asr?task=transcribe&output=json` with `audio_file` field.

---

## [4.1.0] — 2026-04-17

### Architecture
- Renamed `diag` service → `fortress` (core game server; narrator pipeline, world state, SSE event bus, Sorting Hat intent classifier)
- Removed dead functions: `_unload_ollama_vram`, `_schedule_sense_enrichment`, `_do_sense_enrichment`
- Added docstrings to `_stream_ollama_chat`, `_ingest_narrator_turn_into_world`, `_build_messages`
- 5 new smoke tests in `docker-sanity.py` covering Phase 1–5 sprint features (Section 10)
- Player drawer speaker label now shows known player name instead of hardcoded "You"

### Features (landed in v4.0.0 polish sprint, shipping as 4.1.0)
- SFX volume hierarchy: SFX 1.00 / narrator TTS 0.95 / NPC TTS 0.90 / music 0.45 / bridge 0.28
- Action SFX scanner: prose scanned for player-action and env-event verbs before prose renders
- Name blocklist: "Unknown", "traveler" etc. never stored as `canonical_name`
- BPM tier system: ambient 72 / tension 100 / action 124 / climax 140; tier + bpm in mood SSE
- Artist anchoring: Moby, Hybrid, Aphex Twin, Daft Punk, Zimmer, Schachner, van Dyk, RealX in every MusicGen prompt
- 60-second music loops (flask-music `MAX_DURATION` 60 s)
- LoRA narrator auto-select: `remnant-narrator:latest` preferred when available
- Player name shown in drawer speaker label once known

### Known Bugs / Limitations
- **VRAM**: 10–16 GB strongly recommended. v4.0.0 was unstable on lower configs; v4.1.0 is substantially better.
- **Narrator stage directions**: DO channel occasionally echoes NPC tone descriptors on consecutive turns (cosmetic).
- **Music on CPU**: ~30–40 s/clip vs 1–5 s on GPU; music lags narrative on CPU-only configs.
- **ChromaDB semantic memory**: disabled in current Docker build; falls back to static world seed.
- **STT**: VAD wired up but server crash-loop made it non-functional. Fixed in 4.1.1.

---

## [4.0.0] — 2026-04-17
- All 15 pilot quest beats pass; richness score 90/100
- Full sensory pipeline operational (images, mood music, SFX, TTS narrator + NPC voices)
- Player persona enrichment: appearance, traits, and name stored in world graph
- Sorting Hat intent classifier routes SAY / DO / SENSE channels
- ChromaDB semantic memory (disabled in Docker; available in native dev)
