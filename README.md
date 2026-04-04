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
└── extension/                SillyTavern extension (v1.2.0)
    ├── manifest.json
    ├── index.js
    ├── style.css
    ├── locales/
    └── templates/
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

## Installation

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

## Version history

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
