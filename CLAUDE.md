# Remnant — Dev Notes for Claude

## Build modes

All three build modes serve on **port 1582**. Only one can run at a time.
Before starting any mode, check what holds :1582 and stop it first.

| Mode | How to start | How to stop |
|---|---|---|
| **dev** (native source) | `python -X utf8 executable/remnant_launcher.py --no-browser` | Ctrl+C in launcher console |
| **docker** | `docker compose up -d` | `docker compose stop` |
| **exe** | `dist/Remnant.exe` (or native-sanity.py --leave-up) | Close the exe console window |

## "Bring up" commands (say these to Claude)

Claude runs `scripts/dev.py` — do NOT improvise port-conflict resolution manually.

```bash
# Open the game window (pywebview/browser, frameless, blocks until window closed)
python -X utf8 scripts/dev.py exe

# Start native dev launcher headless (console attached, Ctrl+C to stop)
python -X utf8 scripts/dev.py dev

# Start docker compose stack (detached, use 'down' to stop)
python -X utf8 scripts/dev.py docker

# Headless sanity check then leave stack running for API access
python -X utf8 scripts/dev.py check

# Stop everything (docker + stale nginx)
python -X utf8 scripts/dev.py down

# What's running on :1582?
python -X utf8 scripts/dev.py status
```

`dev.py` handles: stop docker → kill stale nginx → start requested mode → wait for :1582.

**exe vs check:**
- `exe` = full game window (what the user sees). Blocks until window is closed.
- `check` = headless start + sanity test + leave stack running. Claude drives via API.
- When testing together, use `exe` for the user's window + Claude uses HTTP API in parallel.

## Port layout

| Port | Service |
|---|---|
| 1582 | nginx gateway (all builds) |
| 1591 | diag sidecar |
| 1592 | flask-sd |
| 1593 | ollama |
| 1594 | tts (Kokoro) |
| 1595 | stt (optional) |
| 1596 | flask-music |

## Key scripts

| Script | Purpose |
|---|---|
| `scripts/release-sanity.py` | Full three-phase release check (native → docker → exe) |
| `scripts/docker-sanity.py` | Warm sanity suite (9 sections + AI trace) — runs against any :1582 stack |
| `executable/native-sanity.py` | Starts native stack, runs sanity, optionally leaves up |
| `scripts/tag-version.py` | Stamps version.json from live /signature composite |
| `executable/build.py` | Builds dist/Remnant.exe via PyInstaller |

## API access (for Claude to drive the game)

When any build is running on :1582, Claude can interact via:

- `GET  http://localhost:1582/health` — gateway liveness
- `POST http://localhost:1582/player-input {"text": "..."}` — send player action
- `GET  http://localhost:1582/diagnostics/narrator-turns?n=10` — recent narrator output (what appeared on screen)
- `GET  http://localhost:1582/diagnostics/ai.json` — service health snapshot
- `GET  http://localhost:1582/signature` — content fingerprint / composite_sha256
- `GET  http://localhost:1582/game/events` — SSE stream of all game events

MCP tools also available when stack is running:
- `mcp__flask-sd__*` → image generation
- `mcp__ollama__*` → direct LLM access (note: may use default port 11434, not 1593)

## Permanent world assets — dev pipeline

**Golden rule:** When the user says "permanent world asset" with any content, Claude:
1. Identifies type: NPC / location / lore / 3D mesh / music
2. Edits `docker/diag/seed/world.json` with the content
3. For images: checks `C:/Users/aaron/Downloads/` or asks for the path; reads the file and writes it to `web/assets/characters/` (NPCs) or `web/assets/locations/` (backgrounds) or `web/assets/characters/NAME-mesh.png` (3D references)
4. Commits both the JSON and any image files

**Asset directories (all tracked in git via `!web/assets/**`):**
- `web/assets/characters/` — NPC portraits + mesh reference sheets
- `web/assets/locations/` — location background art
- `web/assets/music/` — future static music samples

**Seed file:** `docker/diag/seed/world.json` — edit this to add/modify permanent locations, NPCs, and lore. Loaded at startup and after every Reset World. Restart diag to pick up changes (no Docker rebuild needed).

**3D mesh assets** — store as reference PNGs in `web/assets/characters/NAME-mesh.png`. The `physical_spec` and `sd_prompt` fields in the seed JSON lock the appearance for Stable Diffusion generation. Future: 3D awareness will be added to world entities as they are interacted with.

## Release verification sequence

```bash
# Verify all three builds match story content (leaves exe running at end)
python -X utf8 scripts/release-sanity.py

# Skip phases for partial runs
python -X utf8 scripts/release-sanity.py --skip-docker --skip-exe   # native only
python -X utf8 scripts/release-sanity.py --skip-native --skip-exe   # docker only
python -X utf8 scripts/release-sanity.py --skip-native --skip-docker # exe only

# Tag a release (writes version.json after all three pass)
python -X utf8 scripts/release-sanity.py --tag
```
