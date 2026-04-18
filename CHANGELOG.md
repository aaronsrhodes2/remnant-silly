# Changelog

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
