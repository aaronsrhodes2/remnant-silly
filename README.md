# The Remnant Fortress

A local AI-powered storytelling game system. You play **MGSgt Aaron Rhodes**, a
combat engineer abducted by an ancient AI called **The Remnant** into a
spherical alien fortress in null space.

Narration is generated locally by Mistral via Ollama. Scene images are
generated locally by Stable Diffusion. Everything runs on your own machine —
no cloud APIs, no external dependencies, no telemetry.

## Architecture

```
Browser (you)
   |
   v
SillyTavern  (Node.js, port 8001)  <-- game UI + chat + extension
   |                \
   |                 '--> /proxy/ --> Flask API  (Python, port 5000)
   |                                     |
   |                                     v
   |                              Stable Diffusion (GPU)
   v
Ollama  (port 11434)
   |
   v
Mistral (text generation)
```

Three voices play together:

1. **Text narration** — Mistral generates prose in The Remnant's voice.
2. **Inline sensory markers** — The narration contains colored `[GENERATE_IMAGE: "..."]`,
   `[SMELL: "..."]`, `[SOUND: "..."]`, etc. tags that the extension renders as
   colored spans for a synesthetic reading experience.
3. **Generated images** — Visual markers trigger automatic Stable Diffusion
   generation, displayed in a right-side gallery panel.

## Repository layout

```
toh-silly/
├── README.md                 this file
├── backend/
│   ├── image_generator_api.py    Flask API wrapping Stable Diffusion
│   └── image_gallery.py          Persistent image storage/lookup
├── game-content/
│   └── game_master_prompt.txt    Game Master instructions for Mistral
├── extension/                SillyTavern extension (v1.3.0)
│   ├── manifest.json
│   ├── index.js
│   ├── style.css
│   ├── locales/
│   └── templates/
├── docker-compose.yml        one-command full stack
├── .env.example
└── docker/
    ├── ollama/               Ollama + auto-pull entrypoint
    ├── flask-sd/             Flask + Stable Diffusion (CUDA)
    └── sillytavern/          SillyTavern + pre-seeded config + character card
```

## Sensory marker system

Each marker is rendered inline in a distinct color:

| Marker                        | Color          | Use                                   |
| ----------------------------- | -------------- | ------------------------------------- |
| `[GENERATE_IMAGE: "..."]`     | Orange         | Sight — also triggers image gen       |
| `[SIGHT: "..."]`              | Orange         | Alias for `GENERATE_IMAGE`            |
| `[SMELL: "..."]`              | Green          | Olfactory                             |
| `[SOUND: "..."]`              | Blue           | Auditory                              |
| `[TASTE: "..."]`              | Yellow         | Gustatory                             |
| `[TOUCH: "..."]`              | Violet         | Direct tactile contact                |
| `[ENVIRONMENT: "..."]`        | Light Blue     | Ambient atmosphere (heat, wind, etc.) |

Quotes are preferred but optional — the regex accepts both quoted and
unquoted descriptions.

## Quickstart — Docker (recommended)

The entire stack runs in one command. No credentials, no API keys, no
accounts — everything is local inference against Ollama and Stable Diffusion.

### Prerequisites
- Docker 24+ and Docker Compose v2
- **NVIDIA GPU** with up-to-date drivers
- [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) installed on the host
- ~15 GB free disk (models download on first run into named volumes)

### Run it

```bash
git clone https://github.com/aaronsrhodes2/remnant-silly.git
cd remnant-silly
docker compose up
```

First boot takes 5–15 minutes while models download:
- Ollama pulls `mistral` (~4 GB)
- Stable Diffusion v1.5 downloads from HuggingFace on first image request (~4 GB)

Subsequent boots are fast — model weights persist in docker volumes.

When the logs show `remnant-sillytavern  | SillyTavern is listening...`, open:

**http://localhost:8001**

The Remnant character is pre-seeded. Point Text Completions at
`http://ollama:11434` (pre-configured in the included config) and start talking.

### Configuration

All optional. Copy `.env.example` to `.env` to override:

| Variable | Default | Purpose |
|---|---|---|
| `HOST_PORT` | `8001` | Port SillyTavern is exposed on |
| `OLLAMA_MODEL` | `mistral` | Which model Ollama auto-pulls |

### Architecture inside the container network

```
Host :8001 ─────▶ sillytavern (SillyTavern UI + /proxy/ middleware)
                       │
                       ├─▶ ollama:11434      (Mistral text generation)
                       └─▶ flask-sd:5000     (Stable Diffusion, GPU)
```

Only port 8001 is published to the host. `ollama` and `flask-sd` are
invisible to your machine and to the outside world — they exist only on
the internal compose network. The browser only ever talks to SillyTavern,
which server-side proxies image requests to Flask via the built-in
`/proxy/` middleware. Same-origin. No CORS. No secrets anywhere.

---

## Manual install (for development)

### Prerequisites

- Python 3.11+ with: `flask`, `flask-cors`, `diffusers`, `torch` (CUDA build
  recommended), `transformers`, `accelerate`
- Node.js 20+
- Ollama with the `mistral` model pulled
- SillyTavern (tested against v1.17.0)

### 1. Backend — Flask image API

```bash
pip install flask flask-cors diffusers torch transformers accelerate
cp backend/image_generator_api.py ~/
cp backend/image_gallery.py ~/
python ~/image_generator_api.py
```

The API listens on `http://localhost:5000`. First request triggers a
~30–60 second one-time model download and load.

### 2. SillyTavern extension

```bash
cp -r extension ~/SillyTavern/public/scripts/extensions/image-generator
```

In `~/SillyTavern/config.yaml`, ensure:

```yaml
enableCorsProxy: true
```

This lets the extension call the Flask API via SillyTavern's built-in
server-side proxy (`/proxy/http://localhost:5000`), avoiding browser CORS
blocks entirely.

Start SillyTavern:

```bash
cd ~/SillyTavern && npm start
```

Open `http://localhost:8001` (or `:8000` if no port conflict).

### 3. Game content

Paste the contents of `game-content/game_master_prompt.txt` into a
SillyTavern character card / scenario / system prompt for "The Remnant".

### 4. Ollama

```bash
ollama pull mistral
```

In SillyTavern, configure Text Completions → Ollama → `http://localhost:11434`
→ select `mistral`.

## Running the game

1. Ensure Ollama is running: `ollama serve` (usually auto-started)
2. Start the Flask API: `python ~/image_generator_api.py`
3. Start SillyTavern: `cd ~/SillyTavern && npm start`
4. Open `http://localhost:8001`
5. Talk to The Remnant. Images generate automatically from the first
   `[GENERATE_IMAGE: "..."]` marker in each response and appear in the
   right-side gallery panel.

## Extension feature map (current state)

The SillyTavern extension (`extension/`, currently **v2.5.0**) is the
bulk of the game logic. Below is the load-bearing list of what it does
so a fresh session can pick up work without re-reading every commit.

**Narrator voice — second-person present tense (v2.5.0).** The Narrator
card was rewritten to address the player as "you / your". The LLM no
longer has a proper name to wrap in dialogue attribution, which
structurally prevents the impersonation class of bugs that plagued 2.4.x.
The only place third-person "Aaron" still appears is inside
`[GENERATE_IMAGE]` / `[UPDATE_PLAYER]` image prompts (where Stable
Diffusion needs a named subject) and inside NPC quoted dialogue when an
NPC addresses the player by rank. Extension injection strings
(location, codex) follow suit: `"You are in X"`, `"Known items you have
encountered"`.

**Synesthetic sensory markers.** Six channel types rendered inline as
color-coded spans: `[GENERATE_IMAGE]` (sight, also triggers SD gen),
`[SMELL]`, `[SOUND]`, `[TASTE]`, `[TOUCH]`, `[ENVIRONMENT]`. Also
`[INTRODUCE(Name)]`, `[UPDATE_PLAYER]`, `[UPDATE_APPEARANCE(Name)]`,
`[ITEM(Name)]`, `[LORE(Name)]`, `[RESET_STORY]`. Attributed-sense form
(`[SMELL(Sherri): "..."]`) routes a sense through an NPC's voice.

**Play-script layout.** Bracket markers at top → short italic opener →
`Name: "line"` dialogue, each on its own row, followed by a short
italic flavor beat. `.npc-dialogue { display: block }` + a regex that
tolerates `Name: *tone* "line"` (with `.npc-tone` styling). Every NPC
has a deterministic signature color derived from their name hash.

**Auto-NPC cards + roster spotlight.** NPCs introduced via
`[INTRODUCE(...)]` get a locked portrait (generated by SD, preserved
across turns via IP-Adapter reference conditioning in the Flask
backend). Left-side roster shows all NPCs + the player as the first
card. Active speaker gets spotlighted. Clicking a card opens a detail
modal.

**Player avatar auto-update.** `[UPDATE_PLAYER: "..."]` markers
generate a locked portrait for Aaron, upload it as the active persona's
avatar, and include it as SD reference in every future scene.

**Codex (items + lore).** `[ITEM(...)]` and `[LORE(...)]` markers
populate a persistent codex panel. Seeded from narrator messages via
`handleCodexEntries`, and re-injected into every LLM prompt so the
narrator doesn't re-introduce things it has already tagged. **The Fold**
(the always-on neural comm link between player and The Remnant, installed
via nanovirus at pod-insertion) is seeded programmatically as the first
built-in item so existing chats always see it.

**Location memory.** The most recent `[GENERATE_IMAGE(location)]` shot
becomes the chat wallpaper AND is captured into persistent state as
"where you are now", re-injected into the LLM prompt every turn so the
narrator stops referring back to rooms you already left.

**Post-reset safety net.** After `[RESET_STORY]`, the extension waits
for the Flask backend to finish restarting before re-issuing scene
generation, and re-seeds the opening image so the new timeline has a
wallpaper immediately.

**Sense bar.** Icon row above each message with click/hover tooltips,
auto-selecting the first sense on load so the description window is
never empty.

**Impersonation scrubbing (belt-and-suspenders).** Even with the
second-person voice, `scrubPlayerDialogue()` post-processes every
narrator turn to strip legacy `Aaron: "..."` dialogue, `Aaron: *action*`
stage directions, `*Aaron ...*` italic blocks, and both third-person
AND second-person pod-wake re-narration sentences (`Aaron stirs`,
`you stir`, `the pod dissolves around you`, etc.). Scrubs mutate
`chat[i].mes` + `saveChatDebounced()` so the LLM doesn't see its own
hallucinations on the next turn and compound.

## Critical file pointers

- `extension/index.js` — the entire extension (markers, senses, codex,
  roster, player portrait, location memory, scrub, reset flow). **Also
  mirrored** to `C:\Users\aaron\SillyTavern\public\scripts\extensions\image-generator\`
  — any edit here must be mirrored there and vice versa.
- `extension/style.css` — sense colors, NPC dialogue block layout,
  `.npc-tone`, roster cards.
- `extension/manifest.json` — version bump lives here.
- `.scratch_update_card.py` — **the source of truth for the Narrator
  character card** (SYSTEM_PROMPT, FIRST_MES, MES_EXAMPLE,
  POST_HISTORY). Edits here must be followed by running the script to
  patch `C:\Users\aaron\SillyTavern\data\default-user\characters\The Remnant.png`
  (both `chara` v2 and `ccv3` v3 tEXt chunks). This file is gitignored
  as developer scratch but is the single place the system prompt
  content lives.
- `backend/image_generator_api.py` — Flask + SD + IP-Adapter reference
  conditioning for character consistency.

## Known design decisions (so you don't re-litigate them)

- **Aaron is NOT a SillyTavern NPC character card.** The old
  `Master-Gunnery Sergeant Aaron Rhodes.png` was renamed to
  `_disabled_*.bak-v2.4.6` in the ST characters folder. Having him as
  a character card caused ST to inject his description into the LLM
  context, which in turn made the LLM eager to voice him. Removing
  him as a card fixed the root cause.
- **Second-person voice > dialogue filtering.** v2.4.x tried to filter
  out player impersonation post-hoc via regex. v2.5.0 makes it
  structurally impossible by removing the proper name from narration
  entirely. Filters remain as belt-and-suspenders only.
- **Pod emergence is a ONE-TIME event.** first_mes has it; no other
  turn should re-narrate waking up, stirring, or climbing out of a pod.
  The card enforces this, and the scrub catches leaks.
- **The Remnant is the default scene partner.** Via The Fold, the
  player is never alone — empty-corridor scenes still have dialogue
  because The Remnant speaks through the neural link.
- **`[UPDATE_PLAYER]` is the one place third-person is allowed in the
  card**, because its quoted content feeds a Stable Diffusion prompt
  that needs a described subject.

## Version history

### 2.5.0 — Second-person present-tense narrator
- Full Narrator card rewrite: SYSTEM_PROMPT, FIRST_MES, MES_EXAMPLE,
  POST_HISTORY all in second-person present. New NARRATOR VOICE section
  declares the voice and explains why. SELF-CHECK 1c added as a voice
  audit step. 8b/8b-ii/8b-iii/8b-iv rewritten with second-person
  forbidden-phrase lists.
- Extension: `FOLD_ITEM_DESCRIPTION`, location injection, and codex
  label flipped to second-person. `POD_RESET_SENTENCES` gained
  second-person variants alongside the legacy third-person patterns.

### 2.4.7 — Dialogue line-break + The Fold as first item
- Dialogue regex tolerates `Name: *tone* "line"` so the tone marker
  doesn't break play-script row layout. `.npc-tone` styling added.
- `[ITEM(The Fold): "..."]` marker in first_mes; programmatic codex
  seed at init so existing chats see it as item #1. Bug fix: seed
  needed a `name` field to render in the panel.
- Plain-prose pod-wake leak scrubbing: sentence-level regex battery
  (`POD_RESET_SENTENCES`) catching "Aaron stirs / wakes / sits up /
  the pod dissolves" etc. Skips bracket content to preserve image
  prompts.

### 2.4.6 — The Fold lore + pod-emergence one-time rule
- WORLD FACTS teaches the LLM that pod emergence is a ONE-TIME event
  finished in first_mes, with a forbidden-phrase list. SELF-CHECK
  8b-iv enforces.
- The Fold added to WORLD FACTS as the always-on neural comm link
  so empty-corridor scenes default to The Remnant as scene partner.
- Sense bar auto-selects the first sense on load.

### 2.4.5 — Italic Aaron stage-direction scrub
- `PLAYER_ITALIC_RE` catches `*Aaron stirs...*` italic fragments.
- SELF-CHECK 8b-ii forbids narrating Aaron's body reactions in any
  form; 8b-iii forbids italic blocks with Aaron as subject.
- Aaron NPC character card renamed out of the ST characters folder —
  having him as a card was injecting his description into LLM context
  and encouraging impersonation.

### 2.0 – 2.4 — Extension feature build-out
Numerous incremental features consolidated into the feature map above:
clickable character modal, player roster card, inline spotlight,
location-only wallpaper, body-less codex markers, direct-URL avatar
swap, orphan-marker cleanup, sense-bar icon row, top-bar toggle.

### 1.3.1 — Docker stack tuned for 12+ GB VRAM GPUs

### 1.3.0 — Dockerized distribution
- Full-stack `docker compose up` experience: three services (Ollama,
  Flask+SD, SillyTavern) on an internal network, only port 8001 exposed
- Zero-input install: no API keys, no accounts, no config files to edit
- Ollama auto-pulls the configured model on first boot via an init
  entrypoint; healthchecks gate SillyTavern startup until dependencies
  are actually ready
- Flask backend reads `FLASK_HOST`, `FLASK_PORT`, and `IMAGE_GALLERY_DIR`
  from env vars so the same code runs native and in-container
- `image_gallery.py` honors `IMAGE_GALLERY_DIR`, falling back to the
  native-dev location for backward compatibility
- SillyTavern image pre-seeds `config.yaml` (CORS proxy on, listen:true)
  and drops in The Remnant character card so the user has zero setup
- Extension `IMG_GEN_API` URL is patched at build time to use the docker
  service name (`flask-sd`) instead of `localhost`
- Model weights persist in named volumes (`ollama-data`, `hf-cache`)
  so only the first boot pays the download cost

### 1.2.0 — Synesthetic narration
- Multi-sensory marker system: six marker types render as colored spans
  inline in the narration (sight/smell/sound/taste/touch/environment)
- `ENVIRONMENT` distinguished from `TOUCH`: ambient atmosphere vs. direct
  tactile contact
- Accepts both quoted and unquoted descriptions in all markers
- CORS fixed via SillyTavern's built-in `/proxy/` middleware
- UTF-8 stdout reconfiguration in Flask API to prevent Windows `charmap`
  encoding errors on Unicode log output
- Progress logging in Flask API: per-step generation progress with elapsed
  time, model-load timing
- 180-second fetch timeout with AbortController in extension
- Improved error logging in the extension for easier debugging

### 1.0.0 — Initial release
- SillyTavern extension with right-side gallery panel
- Auto-detection of `[GENERATE_IMAGE: "..."]` markers
- Flask + Stable Diffusion backend
- Image gallery with character consistency tracking
