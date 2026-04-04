/**
 * Image Generator Extension for SillyTavern
 *
 * Core responsibilities:
 *   1. Parse sensory-marker syntax in narrator responses and render each
 *      marker as a color-coded span in the chat display.
 *   2. Auto-generate a scene image for every [GENERATE_IMAGE: "..."] marker
 *      and present the gallery with a main-image-plus-thumbnails UX.
 *   3. Use the newest image as the SillyTavern chat background wallpaper.
 *   4. On [INTRODUCE(Name): "..."] markers, generate a locked portrait for
 *      that NPC, create a SillyTavern character card for them, and inject
 *      their portrait description into all future scene prompts so Stable
 *      Diffusion draws them consistently.
 */

import {
    extension_settings,
} from '../../extensions.js';

import {
    eventSource,
    event_types,
    chat,
    characters,
    this_chid,
    saveSettingsDebounced,
    getRequestHeaders,
    getCharacters,
    getThumbnailUrl,
    doNewChat,
} from '../../../script.js';

// Use SillyTavern's built-in CORS proxy to reach the local Flask/SD backend
// without cross-origin blocks. Requires enableCorsProxy: true in config.yaml.
const IMG_GEN_API = '/proxy/http://localhost:5000';
const EXTENSION_NAME = 'image-generator';

// Gallery navigation state (not persisted — starts at newest on each load).
let currentImageIndex = -1;

// Deduplication for introductions that are in-flight. An introduction can
// fire multiple times if the narrator emits the same marker across swipes
// or regenerations before the card has finished creating.
const pendingIntroductions = new Set();

// Palette of signature colors assigned to NPCs in introduction order.
// Chosen to be bright, high-contrast against ST's dark background, and
// deliberately distinct from the six sense colors (which own orange,
// green, sound-blue, yellow, violet, light-blue). Palette wraps on
// overflow.
const NPC_COLOR_PALETTE = [
    '#e91e63', // rose
    '#ab47bc', // violet-pink
    '#7e57c2', // indigo
    '#5c6bc0', // slate blue
    '#26a69a', // teal
    '#66bb6a', // lime-green
    '#ffca28', // gold
    '#ff7043', // coral
    '#8d6e63', // warm brown
    '#bdbdbd', // ivory
];

function pickNpcColor(settings) {
    const used = new Set(
        Object.values(settings.npcs || {})
            .map(n => (n && n.color) || null)
            .filter(Boolean)
    );
    for (const color of NPC_COLOR_PALETTE) {
        if (!used.has(color)) return color;
    }
    // Wrap: pick by count modulo palette length.
    const count = Object.keys(settings.npcs || {}).length;
    return NPC_COLOR_PALETTE[count % NPC_COLOR_PALETTE.length];
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

function initSettings() {
    if (!extension_settings[EXTENSION_NAME]) {
        extension_settings[EXTENSION_NAME] = {
            enabled: true,
            autoGenerate: true,
            generateEvery: 1,
            images: [],
            imageHistory: [],
            // npcs: { [name]: { description, portrait_image, avatar_key, card_created, first_seen, color, locked, reference_image_url } }
            npcs: {},
            // player: { portrait_phrase, portrait_image, reference_image_url, avatar_key, updated_at, last_phrase_normalized }
            player: null,
        };
    }
    // Migrate older settings blobs that may lack the npcs key.
    if (!extension_settings[EXTENSION_NAME].npcs) {
        extension_settings[EXTENSION_NAME].npcs = {};
    }
    if (extension_settings[EXTENSION_NAME].player === undefined) {
        extension_settings[EXTENSION_NAME].player = null;
    }
    // codex: { items: { [name]: { description, first_seen } }, lore: { ... } }
    if (!extension_settings[EXTENSION_NAME].codex) {
        extension_settings[EXTENSION_NAME].codex = { items: {}, lore: {} };
    }
    if (!extension_settings[EXTENSION_NAME].codex.items) extension_settings[EXTENSION_NAME].codex.items = {};
    if (!extension_settings[EXTENSION_NAME].codex.lore)  extension_settings[EXTENSION_NAME].codex.lore  = {};
    return extension_settings[EXTENSION_NAME];
}

// Name the Remnant is keyed under in settings.npcs and dialogue attribution.
const REMNANT_NAME = 'The Remnant';
const REMNANT_COLOR = '#ab47bc';  // violet — deliberate thematic match

// Pre-seed the Remnant NPC entry using the Narrator character's own avatar
// as his locked portrait. The Remnant is the narrator's in-world voice
// (canonically the fortress historian), not a separately-introduced NPC,
// so his visual is the Narrator's card image. Locked so later scene-time
// logic never tries to regenerate him.
// Fetch a same-origin URL (e.g. /thumbnail?...) and return a data: URL
// the Flask backend can decode locally. We do this in the browser where
// ST session cookies / auth headers work; the SD backend only sees an
// inline base64 blob and never has to reach back through ST.
async function urlToDataUrl(url) {
    if (!url) return null;
    if (url.startsWith('data:')) return url;
    try {
        const resp = await fetch(url, { credentials: 'same-origin' });
        if (!resp.ok) return null;
        const blob = await resp.blob();
        return await new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(reader.result);
            reader.onerror = reject;
            reader.readAsDataURL(blob);
        });
    } catch (err) {
        console.warn('[Image Generator] urlToDataUrl failed for', url, err);
        return null;
    }
}

function seedRemnantNpc() {
    const settings = initSettings();
    if (settings.npcs[REMNANT_NAME] && settings.npcs[REMNANT_NAME].locked
        && settings.npcs[REMNANT_NAME].reference_image_url
        && String(settings.npcs[REMNANT_NAME].reference_image_url).startsWith('data:')) {
        // Already seeded with an inlined data URL — nothing to do.
        return;
    }

    let portraitUrl = null;
    let phrase = 'towering obsidian silhouette shot through with veins of amber circuitry, faceless head crowned with a ring of void-black light, ancient and patient';
    try {
        if (Array.isArray(characters) && typeof this_chid !== 'undefined' && characters[this_chid]) {
            const ch = characters[this_chid];
            if (ch.avatar && typeof getThumbnailUrl === 'function') {
                portraitUrl = getThumbnailUrl('avatar', ch.avatar);
            }
            if (ch.description) {
                // Keep phrase short — first sentence of the narrator description.
                const first = String(ch.description).split(/[.!?]/)[0];
                if (first && first.length > 20 && first.length < 260) phrase = first.trim();
            }
        }
    } catch (err) {
        console.warn('[Image Generator] seedRemnantNpc: could not read Narrator avatar', err);
    }

    settings.npcs[REMNANT_NAME] = {
        ...(settings.npcs[REMNANT_NAME] || {}),
        description: phrase,
        color: REMNANT_COLOR,
        portrait_image: portraitUrl,
        reference_image_url: portraitUrl,
        avatar_key: null,
        card_created: true,
        auto_generated: false,
        locked: true,
        first_seen: (settings.npcs[REMNANT_NAME] && settings.npcs[REMNANT_NAME].first_seen) || new Date().toISOString(),
    };
    saveSettingsDebounced();

    // Asynchronously upgrade the reference_image_url to an inline data
    // URL so the SD backend's IP-Adapter can actually fetch it. Relative
    // paths like /thumbnail?... fail when Flask tries to requests.get()
    // them, which is why scene images of The Remnant diverge from his
    // locked avatar. Fire-and-forget.
    if (portraitUrl && !portraitUrl.startsWith('data:')) {
        urlToDataUrl(portraitUrl).then((dataUrl) => {
            if (!dataUrl) return;
            const s = initSettings();
            if (!s.npcs[REMNANT_NAME]) return;
            s.npcs[REMNANT_NAME].reference_image_url = dataUrl;
            s.npcs[REMNANT_NAME].portrait_image = dataUrl;
            saveSettingsDebounced();
            console.log('[Image Generator] Remnant reference upgraded to inline data URL');
        });
    }
}

// ---------------------------------------------------------------------------
// Marker parsing
// ---------------------------------------------------------------------------

// Sensory marker types. Each maps to a CSS color class. Visual markers
// additionally trigger scene image generation. INTRODUCE is a special
// "meta" marker that creates/locks an NPC portrait and character card.
const SENSE_MARKERS = {
    GENERATE_IMAGE:    { cssClass: 'sense-visual',            triggersImage: true,  triggersReset: false },
    SIGHT:             { cssClass: 'sense-visual',            triggersImage: true,  triggersReset: false },
    SMELL:             { cssClass: 'sense-smell',             triggersImage: false, triggersReset: false },
    SOUND:             { cssClass: 'sense-sound',             triggersImage: false, triggersReset: false },
    TASTE:             { cssClass: 'sense-taste',             triggersImage: false, triggersReset: false },
    TOUCH:             { cssClass: 'sense-touch',             triggersImage: false, triggersReset: false },
    ENVIRONMENT:       { cssClass: 'sense-environment',       triggersImage: false, triggersReset: false },
    INTRODUCE:         { cssClass: 'sense-introduce',         triggersImage: false, triggersReset: false },
    RESET_STORY:       { cssClass: 'sense-reset',             triggersImage: false, triggersReset: true  },
    ITEM:              { cssClass: 'sense-item',              triggersImage: false, triggersReset: false },
    LORE:              { cssClass: 'sense-lore',              triggersImage: false, triggersReset: false },
    UPDATE_PLAYER:     { cssClass: 'sense-update-player',     triggersImage: false, triggersReset: false },
    UPDATE_APPEARANCE: { cssClass: 'sense-update-appearance', triggersImage: false, triggersReset: false },
};

// Parse all sensory markers. Supports three forms:
//   [MARKER: "quoted description"]              — bare marker (e.g. SMELL)
//   [MARKER: unquoted description]              — bare marker, unquoted
//   [MARKER(Attribution): "quoted description"] — attributed marker
//
// The attributed form is used for:
//   - INTRODUCE(Name): "portrait description"  → creates the NPC card
//   - SMELL(Sherri): "ozone and old paper"     → routes the sense through an NPC
//
// Returns: [{ type, attribution, description, fullMatch, cssClass, triggersImage }]
function detectSenseMarkers(messageText) {
    const markerNames = Object.keys(SENSE_MARKERS).join('|');
    const regex = new RegExp(
        `\\[(${markerNames})(?:\\(([^)]+)\\))?:\\s*(?:"([^"]+)"|([^\\]]+))\\]`,
        'gi'
    );
    const matches = [];
    let match;
    while ((match = regex.exec(messageText)) !== null) {
        const type = match[1].toUpperCase();
        const attribution = match[2] ? match[2].trim() : null;
        const description = (match[3] || match[4] || '').trim();
        const config = SENSE_MARKERS[type];
        matches.push({
            type,
            attribution,
            description,
            fullMatch: match[0],
            cssClass: config.cssClass,
            triggersImage: config.triggersImage,
            triggersReset: config.triggersReset,
        });
    }
    return matches;
}

// Return only markers that should trigger scene image generation.
function detectImageMarkers(messageText) {
    return detectSenseMarkers(messageText).filter(m => m.triggersImage);
}

// Return only INTRODUCE markers as [{ name, description }]. The narrator
// uses the attribution slot for the NPC name: [INTRODUCE(Sherri): "..."].
function extractIntroductions(messageText) {
    return detectSenseMarkers(messageText)
        .filter(m => m.type === 'INTRODUCE' && m.attribution)
        .map(m => ({ name: m.attribution, description: m.description }));
}

// Return codex entries as { items: [{name, description}], lore: [...] }.
// Both forms are accepted:
//   [ITEM: "obsidian key"]                                (name = description)
//   [ITEM(obsidian key): "fused obsidian and amber, warm to the touch"]
// Same for LORE.
function extractCodexEntries(messageText) {
    const items = [];
    const lore = [];
    for (const m of detectSenseMarkers(messageText)) {
        if (m.type !== 'ITEM' && m.type !== 'LORE') continue;
        const name = (m.attribution || m.description || '').trim();
        if (!name) continue;
        const entry = {
            name,
            description: m.attribution ? m.description : '',
        };
        (m.type === 'ITEM' ? items : lore).push(entry);
    }
    return { items, lore };
}

// ---------------------------------------------------------------------------
// Message display transformation
// ---------------------------------------------------------------------------

function escapeHtml(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

// Look up an NPC's signature color. Returns a neutral fallback if the NPC
// is not yet in the registry (e.g. before their INTRODUCE marker has
// fired) so spoken dialogue still renders sensibly.
function getNpcColor(name) {
    if (!name) return '#e0e0e0';
    const settings = initSettings();
    const npc = settings.npcs[name] || settings.npcs[name.trim()];
    return (npc && npc.color) || '#e0e0e0';
}

// Mix a hex color toward white by `pct` (0-1). Used to tint spoken
// dialogue text so it visually associates with the NPC's signature
// color while staying readable against the dark chat background.
function lightenHex(hex, pct) {
    const h = String(hex || '').replace('#', '');
    if (h.length !== 6) return hex;
    const r = parseInt(h.substring(0, 2), 16);
    const g = parseInt(h.substring(2, 4), 16);
    const b = parseInt(h.substring(4, 6), 16);
    if ([r, g, b].some(v => Number.isNaN(v))) return hex;
    const mix = (c) => Math.round(c + (255 - c) * pct);
    return `#${[mix(r), mix(g), mix(b)].map(v => v.toString(16).padStart(2, '0')).join('')}`;
}

// Replace every sensory marker with a color-coded span, then wrap any
// `Name: "quoted line"` dialogue in a larger bold span tinted with the
// NPC's signature color. Special marker cases:
//   - INTRODUCE renders as a compact "new character" badge in the NPC's
//     signature color (description is for portrait generation, not for
//     the reader to parse inline).
//   - Attributed sense markers (e.g. SMELL(Sherri)) render as "Sherri:"
//     in the NPC color followed by the sensed phrase in the sense color.
function transformMessageText(messageText) {
    const markers = detectSenseMarkers(messageText);
    let result = messageText;
    // Replace in reverse order so earlier indices stay valid.
    for (let i = markers.length - 1; i >= 0; i--) {
        const m = markers[i];
        const idx = result.lastIndexOf(m.fullMatch);
        if (idx === -1) continue;

        let replacement;
        if (m.type === 'RESET_STORY') {
            const flavor = escapeHtml(m.description || 'temporal displacement');
            replacement = `<div class="sense-reset">⟲ the timeline frays — ${flavor}</div>`;
        } else if (m.type === 'ITEM' || m.type === 'LORE') {
            // Codex entries. The bracket name is the entry KEY (goes in
            // the codex panel); the narrator's prose in the same response
            // carries the flavor, so we render the key inline with a
            // prefix glyph and tooltip, not the full description.
            const name = escapeHtml(m.attribution || m.description);
            const tooltip = escapeHtml(m.description);
            const glyph = m.type === 'ITEM' ? '⚜' : '※';
            replacement = `<span class="${m.cssClass}" title="${tooltip}">${glyph} ${name}</span>`;
        } else if (m.type === 'INTRODUCE') {
            const name = escapeHtml(m.attribution || 'unknown');
            const title = escapeHtml(m.description);
            const color = getNpcColor(m.attribution);
            replacement = `<span class="sense-introduce" title="${title}" style="border-color:${color};color:${color}">✦ new character: ${name}</span>`;
        } else if (m.type === 'UPDATE_PLAYER') {
            const title = escapeHtml(m.description);
            replacement = `<span class="sense-update-player" title="${title}">✦ appearance updated</span>`;
        } else if (m.type === 'UPDATE_APPEARANCE') {
            const name = escapeHtml(m.attribution || 'unknown');
            const title = escapeHtml(m.description);
            const color = getNpcColor(m.attribution);
            replacement = `<span class="sense-update-appearance" title="${title}" style="border-color:${color};color:${color}">✦ ${name} changes</span>`;
        } else if (m.attribution) {
            const name = escapeHtml(m.attribution);
            const desc = escapeHtml(m.description);
            const color = getNpcColor(m.attribution);
            replacement = `<span class="${m.cssClass} sense-attributed"><b class="npc-name" style="color:${color}">${name}:</b> "${desc}"</span>`;
        } else {
            replacement = `<span class="${m.cssClass}">${escapeHtml(m.description)}</span>`;
        }

        result = result.slice(0, idx) + replacement + result.slice(idx + m.fullMatch.length);
    }

    // Spoken-dialogue pass: match `Name: "quoted line"` and wrap in a
    // larger bold span with the NPC's signature color on the name. This
    // runs AFTER marker substitution so attributed-sense output (which
    // contains `<b>Name:</b>` inside a span) does not double-match: the
    // regex requires a literal `"` immediately after the colon, and
    // attributed-sense output has `</b> "..."` instead.
    //
    // Side effect: tracks the LAST speaker in the message and, after
    // transformation, we call setActiveSpeaker(lastName) so the spotlight
    // panel updates to that NPC.
    const DIALOGUE_RE = /([A-Z][A-Za-z .'\-]{0,30}):\s*"([^"\n]+)"/g;
    let lastSpeaker = null;
    result = result.replace(DIALOGUE_RE, (full, name, line) => {
        const trimmed = name.trim();
        if (!trimmed) return full;
        lastSpeaker = trimmed;
        const color = getNpcColor(trimmed);
        const lightColor = lightenHex(color, 0.55);
        return `<span class="npc-dialogue"><span class="npc-name" style="color:${color}">${escapeHtml(trimmed)}:</span> <span class="npc-line" style="color:${lightColor}">&ldquo;${escapeHtml(line)}&rdquo;</span></span>`;
    });
    if (lastSpeaker) {
        // Fire-and-forget: spotlight DOM update shouldn't block text render.
        try { setActiveSpeaker(lastSpeaker); } catch (_) { /* ignore */ }
    }

    return result;
}

// Replace the rendered text of a message with the marker-transformed HTML.
// SillyTavern tags each message DIV with `mesid="<N>"`, not `data-message-id`.
function updateMessageDisplay(messageId) {
    if (typeof messageId !== 'number' || messageId < 0) return;

    const message = chat[messageId];
    if (!message || !message.mes) return;

    const transformedText = transformMessageText(message.mes);
    if (transformedText === message.mes) return;

    const messageElement = $(`#chat .mes[mesid="${messageId}"]`).find('.mes_text');
    if (messageElement.length === 0) {
        console.warn(`[Image Generator] Could not find .mes_text for mesid=${messageId}`);
        return;
    }
    messageElement.html(DOMPurify.sanitize(transformedText, {
        ADD_ATTR: ['class', 'title', 'style'],
    }));
}

// ---------------------------------------------------------------------------
// NPC consistency injection
// ---------------------------------------------------------------------------

// Scan a scene description for any known NPC names from the registry and
// append each match's locked portrait phrase to the SD prompt, AND gather
// their locked reference image URLs so the backend can use them as
// IP-Adapter conditioning. Player reference is also included if the
// scene mentions Aaron/the player and a player portrait is locked.
//
// Returns: { prompt, reference_images: [url, ...] }
function injectNpcContextIntoPrompt(description) {
    const settings = initSettings();
    const matched = [];
    const referenceImages = [];
    const lowerDesc = description.toLowerCase();

    // NPC matches
    for (const name of Object.keys(settings.npcs || {})) {
        if (!lowerDesc.includes(name.toLowerCase())) continue;
        const npc = settings.npcs[name];
        if (!npc) continue;
        if (npc.description) matched.push(`${name}: ${npc.description}`);
        if (npc.reference_image_url) referenceImages.push(npc.reference_image_url);
    }

    // Player match — include if the scene mentions the player pronouns or name
    // and a player portrait is locked.
    if (settings.player && settings.player.reference_image_url) {
        const mentionsPlayer = /\baaron\b|\brhodes\b|\bsergeant\b|\bplayer\b/.test(lowerDesc);
        if (mentionsPlayer) {
            if (settings.player.portrait_phrase) {
                matched.push(`Aaron: ${settings.player.portrait_phrase}`);
            }
            referenceImages.push(settings.player.reference_image_url);
        }
    }

    const prompt = matched.length === 0
        ? description
        : `${description}. Character reference${matched.length > 1 ? 's' : ''}: ${matched.join('; ')}.`;
    return { prompt, reference_images: referenceImages };
}

// ---------------------------------------------------------------------------
// Image generation
// ---------------------------------------------------------------------------

// Default negative prompt. SD1.5 cannot render readable English text,
// and when it tries it produces garbled pseudo-glyphs baked into the
// image. We broadly suppress text/letters/writing unless a caller
// overrides. (Environmental signs are better handled by leaving them
// unrendered and letting the narrator's prose describe them.)
const DEFAULT_NEGATIVE_PROMPT = 'blurry, low quality, distorted, deformed, text, letters, words, writing, typography, watermark, signature, caption, subtitle, logo, scribbles, handwriting, gibberish, captions, labels, UI, frame';

async function callSdApi(prompt, { steps = 25, guidance = 7.5, timeoutMs = 180000, reference_images = null, reference_scale = null, negative_prompt = DEFAULT_NEGATIVE_PROMPT } = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    try {
        const body = { prompt, negative_prompt, steps, guidance_scale: guidance };
        if (Array.isArray(reference_images) && reference_images.length > 0) {
            body.reference_images = reference_images;
            if (typeof reference_scale === 'number') body.reference_scale = reference_scale;
        }
        const response = await fetch(`${IMG_GEN_API}/api/generate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
            signal: controller.signal,
        });
        if (!response.ok) {
            console.error('[Image Generator] SD API error', response.status, response.statusText);
            return null;
        }
        const data = await response.json();
        if (!data.success) {
            console.error('[Image Generator] SD reported failure', data);
            return null;
        }
        return data; // { success, image, image_id, prompt }
    } catch (err) {
        if (err.name === 'AbortError') {
            console.error('[Image Generator] SD request timed out');
        } else {
            console.error('[Image Generator] SD request error', err);
        }
        return null;
    } finally {
        clearTimeout(timeout);
    }
}

// Generate a scene image. Injects known-NPC portrait descriptions into the
// prompt and threads any locked reference-image URLs so the backend's
// IP-Adapter can lock visual identity across scenes.
async function generateSceneImage(rawDescription) {
    const { prompt: augmented, reference_images } = injectNpcContextIntoPrompt(rawDescription);
    console.log('[Image Generator] Scene prompt:', augmented.substring(0, 120), '| refs:', reference_images.length);
    const data = await callSdApi(augmented, {
        reference_images: reference_images.length > 0 ? reference_images : null,
        reference_scale: 0.5,
    });
    if (!data) return null;
    return {
        image_id: data.image_id,
        image: data.image,
        description: rawDescription, // display the original, not the augmented one
        prompt_sent: augmented,
        timestamp: new Date().toISOString(),
    };
}

// Generate a portrait for an NPC. Uses a portrait-flavored template to keep
// output consistent across characters: centered, clean background, 3/4 view.
async function generatePortrait(npcName, npcDescription) {
    const prompt = `character portrait of ${npcDescription}, centered composition, clean neutral background, detailed face, cinematic lighting, painterly fantasy style, 3/4 view, shoulders up`;
    console.log(`[Image Generator] Portrait prompt for ${npcName}:`, prompt.substring(0, 120));
    const data = await callSdApi(prompt);
    if (!data) return null;
    return {
        image_id: data.image_id,
        image: data.image,
        description: npcDescription,
        prompt_sent: prompt,
        timestamp: new Date().toISOString(),
    };
}

function storeSceneImage(imageData) {
    const settings = initSettings();
    settings.images.push(imageData);
    settings.imageHistory.push(imageData.image_id);
    saveSettingsDebounced();
}

// ---------------------------------------------------------------------------
// NPC character card creation (ST API)
// ---------------------------------------------------------------------------

// Create a new SillyTavern character card via the /api/characters/create
// endpoint, then upload the generated portrait as the card's avatar. Returns
// the avatar key on success, or null on failure.
async function createNpcCharacterCard(name, description, portraitDataUrl, color) {
    // Portable metadata: the portrait phrase and signature color live in
    // the card's `extensions` field so they survive extension-settings
    // reset or Docker rebuild, and are user-editable from ST's character
    // editor (Advanced Definitions → extensions JSON).
    const extensionsBlob = {
        'image-generator': {
            color: color || null,
            portrait_phrase: description,
            auto_generated: true,
        },
    };
    const characterData = {
        ch_name: name,
        description: description,
        first_mes: `*${name} regards you in their particular way.*`,
        personality: '',
        scenario: 'Met in the Fortress of Eternal Sentinel.',
        mes_example: '',
        creator_notes: `Auto-created by the Image Generator extension on first [INTRODUCE] marker from the Narrator. Locked portrait description: "${description}"`,
        system_prompt: '',
        post_history_instructions: '',
        creator: 'the-remnant-fortress/narrator',
        character_version: '1.0.0',
        tags: ['auto-generated', 'npc', 'remnant-fortress'],
        talkativeness: '0.5',
        world: '',
        depth_prompt_prompt: '',
        depth_prompt_depth: '4',
        depth_prompt_role: 'system',
        fav: 'false',
        alternate_greetings: [],
        extensions: JSON.stringify(extensionsBlob),
    };

    let avatarKey;
    try {
        const resp = await fetch('/api/characters/create', {
            method: 'POST',
            headers: getRequestHeaders(),
            body: JSON.stringify(characterData),
        });
        if (!resp.ok) {
            console.error(`[Image Generator] Card create failed for ${name}:`, await resp.text());
            return null;
        }
        avatarKey = await resp.text();
    } catch (err) {
        console.error(`[Image Generator] Card create error for ${name}:`, err);
        return null;
    }

    // Upload the avatar image (portraitDataUrl is "data:image/png;base64,...").
    try {
        const blobResp = await fetch(portraitDataUrl);
        const blob = await blobResp.blob();
        const formData = new FormData();
        formData.append('avatar', blob, 'avatar.png');
        formData.append('avatar_url', avatarKey);
        const uploadResp = await fetch('/api/characters/edit-avatar', {
            method: 'POST',
            headers: getRequestHeaders({ omitContentType: true }),
            body: formData,
        });
        if (!uploadResp.ok) {
            console.error(`[Image Generator] Avatar upload failed for ${name}:`, await uploadResp.text());
            // Card was still created, just without the portrait avatar — keep going.
        }
    } catch (err) {
        console.error(`[Image Generator] Avatar upload error for ${name}:`, err);
    }

    // Refresh the character list so the new card appears in the sidebar.
    try {
        if (typeof getCharacters === 'function') {
            await getCharacters();
        }
    } catch (err) {
        console.warn('[Image Generator] getCharacters refresh failed:', err);
    }

    return avatarKey;
}

// Parse INTRODUCE markers out of a message and, for each new NPC:
//   1. Generate a locked portrait via SD
//   2. Create a SillyTavern character card with that portrait as avatar
//   3. Store the entry in settings.npcs so future scenes inject it
async function handleIntroductions(messageText) {
    const intros = extractIntroductions(messageText);
    if (intros.length === 0) return;

    const settings = initSettings();

    for (const { name, description } of intros) {
        // The Remnant is the narrator's in-world voice and is pre-seeded
        // on init with the Narrator's own avatar — never re-introduce him.
        if (name === REMNANT_NAME) continue;
        if (settings.npcs[name] && settings.npcs[name].card_created) continue;  // already known
        if (settings.npcs[name] && settings.npcs[name].locked) continue;        // locked entry
        if (pendingIntroductions.has(name)) continue;                            // in-flight
        pendingIntroductions.add(name);

        // Assign a signature color BEFORE generating so the intro badge
        // + any spoken dialogue in the same message renders in the NPC's
        // color as soon as transformMessageText re-runs.
        const color = pickNpcColor(settings);
        settings.npcs[name] = {
            description,
            color,
            portrait_image: null,
            portrait_image_id: null,
            avatar_key: null,
            card_created: false,
            first_seen: new Date().toISOString(),
        };
        saveSettingsDebounced();
        renderNpcRoster();

        try {
            updatePanelStatus(`🎭 Sketching ${name}...`);
            const portrait = await generatePortrait(name, description);
            if (!portrait) {
                console.error(`[Image Generator] Portrait generation failed for ${name}`);
                continue;
            }

            updatePanelStatus(`📇 Creating character card for ${name}...`);
            const avatarKey = await createNpcCharacterCard(name, description, portrait.image, color);

            settings.npcs[name] = {
                ...settings.npcs[name],
                portrait_image: portrait.image,
                portrait_image_id: portrait.image_id,
                avatar_key: avatarKey,
                card_created: !!avatarKey,
            };
            saveSettingsDebounced();
            renderNpcRoster();

            updatePanelStatus(`✦ ${name} has joined the cast`);
            setTimeout(() => updatePanelStatus(''), 2500);
            console.log(`[Image Generator] NPC locked: ${name} (card=${!!avatarKey}, color=${color})`);
        } finally {
            pendingIntroductions.delete(name);
        }
    }
}

// Extract ITEM/LORE markers from a rendered message and merge any new
// entries into the codex. Entries are keyed by name; first occurrence
// wins on description (later mentions don't overwrite) so the codex
// preserves the narrator's original flavor text for each thing. Re-
// renders the codex panel whenever something new is added.
function handleCodexEntries(messageText) {
    const { items, lore } = extractCodexEntries(messageText);
    if (items.length === 0 && lore.length === 0) return;
    const settings = initSettings();
    const now = new Date().toISOString();
    let added = 0;
    for (const { name, description } of items) {
        if (settings.codex.items[name]) continue;
        settings.codex.items[name] = { name, description, first_seen: now };
        added++;
    }
    for (const { name, description } of lore) {
        if (settings.codex.lore[name]) continue;
        settings.codex.lore[name] = { name, description, first_seen: now };
        added++;
    }
    if (added > 0) {
        saveSettingsDebounced();
        renderCodex();
    }
}

// Handle a [RESET_STORY: "..."] marker from the Narrator. This is the
// in-story temporal-reset mechanic: Aaron says "Remnant, reset the
// story" (or similar), the Remnant monologues about abducting Aaron
// from a few moments BEFORE the original abduction — wiping this
// timeline — and ends the response with the RESET_STORY marker. The
// extension lets the player read the speech, then archives the chat
// via ST's doNewChat() and clears all acquired state so the next chat
// starts truly clean.
//
// deleteCurrentChat stays false → the old chat is archived to
// ~/SillyTavern/data/default-user/chats/Narrator/*.jsonl (recoverable).
async function handleResetStory() {
    const settings = initSettings();

    // Visible countdown so the player has time to read the Remnant's
    // final monologue before the timeline collapses.
    const overlayHtml = `
        <div id="img-gen-reset-overlay" class="img-gen-reset-overlay">
            <div class="img-gen-reset-inner">
                <div class="img-gen-reset-title">⟲ timeline collapsing</div>
                <div class="img-gen-reset-sub">the remnant is abducting you from a few moments before the original abduction</div>
                <div class="img-gen-reset-counter" id="img-gen-reset-counter">5</div>
            </div>
        </div>
    `;
    if ($('#img-gen-reset-overlay').length === 0) {
        $('body').append(overlayHtml);
    }

    // 5-second countdown.
    for (let i = 5; i >= 1; i--) {
        $('#img-gen-reset-counter').text(i);
        await new Promise(r => setTimeout(r, 1000));
    }

    // Clear extension state that's tied to the old timeline.
    // Intentionally NOT cleared: settings.player — Aaron's locked
    // appearance (portrait_phrase, reference_image_url, avatar_key)
    // persists across timelines so the narrator doesn't have to
    // re-learn who Aaron is every reset. IP-Adapter conditioning
    // continues to reference the prior portrait automatically via
    // injectNpcContextIntoPrompt.
    settings.images = [];
    settings.imageHistory = [];
    settings.npcs = {};
    settings.codex = { items: {}, lore: {} };
    currentImageIndex = -1;
    saveSettingsDebounced();
    updateBackgroundWallpaper(null);
    renderGallery();
    renderNpcRoster();
    renderCodex();

    // Archive the current chat and start a fresh one. Safe mode — the
    // old chat is preserved in the Narrator's chat history folder.
    try {
        if (typeof doNewChat === 'function') {
            await doNewChat({ deleteCurrentChat: false });
        } else {
            // Fallback: click ST's "start new chat" UI button.
            $('#option_start_new_chat').trigger('click');
        }
    } catch (err) {
        console.error('[Image Generator] Reset — doNewChat failed:', err);
    }

    $('#img-gen-reset-overlay').remove();
}

// ---------------------------------------------------------------------------
// Player portrait auto-update
// ---------------------------------------------------------------------------

// Normalize a phrase for dedup comparison — lowercase, collapse whitespace,
// strip punctuation. Prevents regenerating on trivial wording tweaks.
function normalizePhrase(s) {
    return String(s || '').toLowerCase().replace(/[^a-z0-9\s]/g, ' ').replace(/\s+/g, ' ').trim();
}

// Extract player-update markers and regenerate Aaron's portrait, upload it
// as the ST user avatar, and store as a reference for future scenes.
async function handlePlayerUpdate(messageText) {
    const markers = detectSenseMarkers(messageText).filter(m => m.type === 'UPDATE_PLAYER');
    if (markers.length === 0) {
        // Visible in the console so it's obvious when the narrator
        // failed to emit the marker vs. when the extension silently
        // dropped it. Helps diagnose "player avatar didn't update"
        // reports.
        if (/UPDATE_PLAYER/i.test(messageText)) {
            console.warn('[Image Generator] Message contains "UPDATE_PLAYER" text but detectSenseMarkers matched zero markers — check bracket syntax in the narrator output.');
        } else {
            console.log('[Image Generator] handlePlayerUpdate: no [UPDATE_PLAYER] marker in this turn.');
        }
        return;
    }
    console.log('[Image Generator] handlePlayerUpdate: marker found →', markers[markers.length - 1].description);

    const settings = initSettings();
    // Most recent marker wins if there are multiple in one message.
    const description = markers[markers.length - 1].description;
    if (!description) return;

    const normalized = normalizePhrase(description);
    if (settings.player && settings.player.last_phrase_normalized === normalized) {
        console.log('[Image Generator] Player phrase unchanged, skipping regen');
        return;
    }

    try {
        updatePanelStatus('🎭 Painting Aaron...');
        const portrait = await generatePortrait('Aaron', description);
        if (!portrait) {
            console.error('[Image Generator] Player portrait generation failed');
            updatePanelStatus('');
            return;
        }

        // Upload to ST as a user persona avatar. Replicates personas.js
        // uploadUserAvatar() — fetch the data URL, wrap as FormData, POST.
        let avatarPath = null;
        try {
            const blobResp = await fetch(portrait.image);
            const blob = await blobResp.blob();
            const file = new File([blob], 'avatar.png', { type: 'image/png' });
            const formData = new FormData();
            formData.append('avatar', file);
            formData.append('overwrite_name', 'aaron.png');
            const uploadResp = await fetch('/api/avatars/upload', {
                method: 'POST',
                headers: getRequestHeaders({ omitContentType: true }),
                cache: 'no-cache',
                body: formData,
            });
            if (uploadResp.ok) {
                const data = await uploadResp.json().catch(() => ({}));
                avatarPath = (data && data.path) || 'aaron.png';
            } else {
                console.warn('[Image Generator] Player avatar upload failed:', uploadResp.statusText);
            }
        } catch (err) {
            console.warn('[Image Generator] Player avatar upload error:', err);
        }

        settings.player = {
            portrait_phrase: description,
            portrait_image: portrait.image,
            reference_image_url: portrait.image,
            avatar_key: avatarPath,
            last_phrase_normalized: normalized,
            updated_at: new Date().toISOString(),
        };
        saveSettingsDebounced();

        // Best-effort DOM refresh of the currently-shown persona avatar.
        try {
            $('.persona_avatar img, #user_avatar img, .avatar img[src*="aaron"]').attr('src', portrait.image);
        } catch (_) { /* ignore */ }

        updatePanelStatus('✦ Aaron, rendered');
        setTimeout(() => updatePanelStatus(''), 2500);
        console.log('[Image Generator] Player portrait updated');
    } catch (err) {
        console.error('[Image Generator] handlePlayerUpdate error:', err);
        updatePanelStatus('');
    }
}

// Handle UPDATE_APPEARANCE markers for already-introduced NPCs. Regenerates
// their locked portrait unless the entry is `locked` (e.g. The Remnant).
async function handleAppearanceUpdates(messageText) {
    const markers = detectSenseMarkers(messageText)
        .filter(m => m.type === 'UPDATE_APPEARANCE' && m.attribution);
    if (markers.length === 0) return;

    const settings = initSettings();
    for (const m of markers) {
        const name = m.attribution;
        const npc = settings.npcs[name];
        if (!npc) {
            console.warn(`[Image Generator] UPDATE_APPEARANCE for unknown NPC: ${name}`);
            continue;
        }
        if (npc.locked) {
            console.log(`[Image Generator] Skipping appearance update for locked NPC: ${name}`);
            continue;
        }
        try {
            updatePanelStatus(`🎭 Repainting ${name}...`);
            const portrait = await generatePortrait(name, m.description);
            if (!portrait) continue;
            settings.npcs[name] = {
                ...npc,
                description: m.description,
                portrait_image: portrait.image,
                portrait_image_id: portrait.image_id,
                reference_image_url: portrait.image,
                updated_at: new Date().toISOString(),
            };
            saveSettingsDebounced();
            renderNpcRoster();
            updatePanelStatus(`✦ ${name} repainted`);
            setTimeout(() => updatePanelStatus(''), 2500);
        } catch (err) {
            console.error(`[Image Generator] Appearance update error for ${name}:`, err);
        }
    }
}

// ---------------------------------------------------------------------------
// Speaker spotlight
// ---------------------------------------------------------------------------

// The speaker spotlight panel is a wider card that sits between the left
// roster strip and the chat text. It shows whoever most recently spoke
// in the current message, in their signature color. Persists between
// turns (no blanking) so the reader has a visual anchor for the last-
// heard voice.
let activeSpeakerName = null;

function createSpeakerSpotlight() {
    if ($('#image-generator-speaker-spotlight').length > 0) return;
    $('body').append(`
        <div id="image-generator-speaker-spotlight" class="img-gen-speaker-spotlight" style="display:none">
            <div class="img-gen-speaker-avatar" id="img-gen-speaker-avatar"></div>
            <div class="img-gen-speaker-name" id="img-gen-speaker-name"></div>
            <div class="img-gen-speaker-phrase" id="img-gen-speaker-phrase"></div>
        </div>
    `);
}

function setActiveSpeaker(name) {
    if (!name) return;
    const settings = initSettings();
    const npc = settings.npcs[name];
    if (!npc) {
        // Unknown speaker — show a placeholder card with just the name.
        activeSpeakerName = name;
        const $panel = $('#image-generator-speaker-spotlight');
        if ($panel.length === 0) return;
        $panel.css('display', 'flex').css('border-color', '#e0e0e0');
        $('#img-gen-speaker-avatar').html(`<div class="img-gen-speaker-avatar-pending">?</div>`);
        $('#img-gen-speaker-name').text(name).css('color', '#e0e0e0');
        $('#img-gen-speaker-phrase').text('');
        return;
    }
    activeSpeakerName = name;
    const color = npc.color || '#e0e0e0';
    const portrait = npc.portrait_image || npc.reference_image_url || null;
    const $panel = $('#image-generator-speaker-spotlight');
    if ($panel.length === 0) return;
    $panel.css('display', 'flex').css('border-color', color).css('box-shadow', `0 0 24px ${color}55`);
    const avatarHtml = portrait
        ? `<img src="${portrait}" alt="${escapeHtml(name)}" />`
        : `<div class="img-gen-speaker-avatar-pending">…</div>`;
    $('#img-gen-speaker-avatar').html(avatarHtml);
    $('#img-gen-speaker-name').text(name).css('color', color);
    const phrase = (npc.description || '').split(/[.!?]/)[0] || '';
    $('#img-gen-speaker-phrase').text(phrase);
}

// ---------------------------------------------------------------------------
// Flask SD readiness
// ---------------------------------------------------------------------------

// Wait until the Flask SD backend is reachable. Used on boot and after
// chat changes so that the very first scene marker in a greeting is not
// silently lost because the model was still loading. Returns true on
// success, false on timeout. Polls /api/health every 2s.
async function waitForSdReady({ timeoutMs = 120000, label = '' } = {}) {
    const start = Date.now();
    let attempts = 0;
    while (Date.now() - start < timeoutMs) {
        try {
            const resp = await fetch(`${IMG_GEN_API}/api/health`, { cache: 'no-cache' });
            if (resp.ok) {
                if (attempts > 0) console.log(`[Image Generator] SD ready after ${attempts} poll(s) ${label}`);
                return true;
            }
        } catch (_) { /* backend not up yet */ }
        attempts++;
        if (attempts === 1) updatePanelStatus('⏳ Waiting for image backend...');
        await new Promise(r => setTimeout(r, 2000));
    }
    console.warn('[Image Generator] SD backend not ready within timeout');
    updatePanelStatus('');
    return false;
}

// Hydrate settings.npcs from ST's character list. Any ST character whose
// card carries extensions['image-generator'] metadata (color, portrait
// phrase) gets merged into the registry. This makes the colors portable
// across extension-settings resets and lets the user edit colors
// directly from ST's character editor (Advanced Definitions → extensions
// JSON). Runs on extension init after characters are loaded.
function hydrateNpcsFromCards() {
    const settings = initSettings();
    if (!Array.isArray(characters) || characters.length === 0) return;
    for (const ch of characters) {
        if (!ch || !ch.name) continue;
        let meta = null;
        try {
            const raw = ch.data && ch.data.extensions && ch.data.extensions['image-generator'];
            if (raw && typeof raw === 'object') meta = raw;
        } catch (_) { /* ignore */ }
        if (!meta) continue;
        const existing = settings.npcs[ch.name] || {};
        settings.npcs[ch.name] = {
            ...existing,
            description: existing.description || meta.portrait_phrase || '',
            color: existing.color || meta.color || null,
            avatar_key: existing.avatar_key || ch.avatar || null,
            card_created: true,
        };
    }
}

// ---------------------------------------------------------------------------
// Gallery UI
// ---------------------------------------------------------------------------

// Build the left-side NPC roster panel. This mirrors the gallery panel on
// the right but is narrower and vertical: every introduced NPC gets a
// small avatar card bordered in their signature color. Re-rendered
// whenever an NPC is added.
function createNpcRosterPanel() {
    if ($('#image-generator-npc-roster').length > 0) return;
    $('body').append('<div id="image-generator-npc-roster" class="img-gen-npc-roster"><div class="img-gen-npc-roster-inner" id="img-gen-npc-roster-inner"></div></div>');
}

// Render the codex panel (items + lore sections) from settings.codex.
// Entries are keyed by name; the list is sorted by first_seen so the
// oldest discoveries stay at the top and new ones append below. Shows
// nothing for empty sections.
function renderCodex() {
    const settings = initSettings();
    const codex = settings.codex || { items: {}, lore: {} };

    function renderSection(which) {
        const entries = Object.values(codex[which] || {});
        entries.sort((a, b) => {
            const ta = a.first_seen ? Date.parse(a.first_seen) : 0;
            const tb = b.first_seen ? Date.parse(b.first_seen) : 0;
            return ta - tb;
        });
        $(`#img-gen-codex-count-${which}`).text(entries.length);
        const $list = $(`#img-gen-codex-entries-${which}`);
        if (entries.length === 0) {
            $list.html(`<div class="img-gen-codex-empty">none yet</div>`);
            return;
        }
        const html = entries.map(e => {
            const name = escapeHtml(e.name);
            const desc = escapeHtml(e.description || '');
            return `<div class="img-gen-codex-entry" title="${desc}">
                <div class="img-gen-codex-name">${name}</div>
                ${desc ? `<div class="img-gen-codex-desc">${desc}</div>` : ''}
            </div>`;
        }).join('');
        $list.html(html);
    }

    if ($('#img-gen-codex').length === 0) return; // panel not mounted yet
    renderSection('items');
    renderSection('lore');
}

function renderNpcRoster() {
    const settings = initSettings();
    const $inner = $('#img-gen-npc-roster-inner');
    if ($inner.length === 0) return;

    const entries = Object.entries(settings.npcs || {});
    if (entries.length === 0) {
        $inner.html('<div class="img-gen-npc-empty">No NPCs<br/>met yet</div>');
        return;
    }

    // Sort by first_seen ascending so the oldest acquaintance is at the top.
    entries.sort(([, a], [, b]) => {
        const ta = a && a.first_seen ? Date.parse(a.first_seen) : 0;
        const tb = b && b.first_seen ? Date.parse(b.first_seen) : 0;
        return ta - tb;
    });

    const html = entries.map(([name, npc]) => {
        const color = (npc && npc.color) || '#bdbdbd';
        const portrait = npc && npc.portrait_image;
        const title = escapeHtml((npc && npc.description) || name);
        const safeName = escapeHtml(name);
        const avatarInner = portrait
            ? `<img src="${portrait}" alt="${safeName}" />`
            : `<div class="img-gen-npc-avatar-pending">…</div>`;
        return `
            <div class="img-gen-npc-card" style="border-color:${color}" title="${title}">
                <div class="img-gen-npc-avatar">${avatarInner}</div>
                <div class="img-gen-npc-name" style="color:${color}">${safeName}</div>
            </div>
        `;
    }).join('');
    $inner.html(html);
}

function createSidePanel() {
    const panelHTML = `
        <div class="img-gen-panel-header">
            <h3 class="img-gen-panel-title">🖼️ Image Gallery</h3>
            <button class="img-gen-close" id="img-gen-close" title="Close panel">✕</button>
        </div>
        <div class="img-gen-status"></div>
        <div class="img-gen-main">
            <button class="img-gen-nav img-gen-prev" id="img-gen-prev" title="Previous image">◀</button>
            <div class="img-gen-main-frame">
                <img class="img-gen-main-img" id="img-gen-main-img" alt="" />
                <div class="img-gen-main-empty">
                    <p>Awaiting generated images...<br/><small>They'll appear here as the narrator creates scenes</small></p>
                </div>
            </div>
            <button class="img-gen-nav img-gen-next" id="img-gen-next" title="Next image">▶</button>
        </div>
        <div class="img-gen-main-meta">
            <span class="img-gen-counter" id="img-gen-counter"></span>
            <p class="img-gen-main-desc" id="img-gen-main-desc"></p>
        </div>
        <div class="img-gen-thumbs" id="img-gen-thumbs"></div>
        <div class="img-gen-codex" id="img-gen-codex">
            <div class="img-gen-codex-section" data-section="items">
                <div class="img-gen-codex-header">
                    <span class="img-gen-codex-glyph">⚜</span>
                    <span class="img-gen-codex-title">Items</span>
                    <span class="img-gen-codex-count" id="img-gen-codex-count-items">0</span>
                </div>
                <div class="img-gen-codex-entries" id="img-gen-codex-entries-items"></div>
            </div>
            <div class="img-gen-codex-section" data-section="lore">
                <div class="img-gen-codex-header">
                    <span class="img-gen-codex-glyph">※</span>
                    <span class="img-gen-codex-title">Lore</span>
                    <span class="img-gen-codex-count" id="img-gen-codex-count-lore">0</span>
                </div>
                <div class="img-gen-codex-entries" id="img-gen-codex-entries-lore"></div>
            </div>
        </div>
    `;

    if ($('#image-generator-panel').length === 0) {
        $('body').append(`<div id="image-generator-panel" class="img-gen-panel">${panelHTML}</div>`);

        $('#img-gen-close').on('click', () => $('#image-generator-panel').toggle());
        $('#img-gen-prev').on('click', goPrevImage);
        $('#img-gen-next').on('click', goNextImage);
        $('#img-gen-thumbs').on('click', '.img-gen-thumb', function () {
            const idx = parseInt($(this).attr('data-index'), 10);
            if (!Number.isNaN(idx)) gotoImage(idx);
        });
    }
}

// Set the SillyTavern chat background wallpaper to the given image URL.
// Clearing (null) removes our inline override so ST's normal background
// takes over again.
function updateBackgroundWallpaper(imageUrl) {
    const $bg = $('#bg1');
    if ($bg.length === 0) return;
    if (imageUrl) {
        $bg.css('background-image', `url("${imageUrl}")`);
        $bg.attr('data-image-generator-bg', '1');
    } else if ($bg.attr('data-image-generator-bg')) {
        $bg.css('background-image', '');
        $bg.removeAttr('data-image-generator-bg');
    }
}

// Render the currently-selected image in the main slot, rebuild the thumb
// strip, and mirror the current image as the ST chat background wallpaper.
function renderGallery() {
    const settings = initSettings();
    const images = settings.images;
    const $frame = $('#image-generator-panel .img-gen-main-frame');
    const $img = $('#img-gen-main-img');
    const $empty = $frame.find('.img-gen-main-empty');
    const $desc = $('#img-gen-main-desc');
    const $counter = $('#img-gen-counter');
    const $thumbs = $('#img-gen-thumbs');
    const $prev = $('#img-gen-prev');
    const $next = $('#img-gen-next');

    if (images.length === 0) {
        $img.hide().attr('src', '');
        $empty.show();
        $desc.text('');
        $counter.text('');
        $thumbs.empty();
        $prev.prop('disabled', true);
        $next.prop('disabled', true);
        updateBackgroundWallpaper(null);
        return;
    }

    if (currentImageIndex < 0 || currentImageIndex >= images.length) {
        currentImageIndex = images.length - 1;
    }

    const current = images[currentImageIndex];
    $empty.hide();
    $img.attr('src', current.image).attr('alt', current.description || '').show();
    $desc.text(current.description || '');
    $counter.text(`${currentImageIndex + 1} / ${images.length}`);
    $prev.prop('disabled', currentImageIndex <= 0);
    $next.prop('disabled', currentImageIndex >= images.length - 1);

    // Mirror current image to the ST chat background.
    updateBackgroundWallpaper(current.image);

    // Thumbnail strip, newest on the left.
    const thumbsHtml = images
        .map((img, i) => {
            const activeCls = i === currentImageIndex ? ' img-gen-thumb-active' : '';
            return `<div class="img-gen-thumb${activeCls}" data-index="${i}" title="${escapeHtml(img.description || '').slice(0, 80)}">
                <img src="${img.image}" alt="" />
            </div>`;
        })
        .reverse()
        .join('');
    $thumbs.html(thumbsHtml);

    const $activeThumb = $thumbs.find('.img-gen-thumb-active');
    if ($activeThumb.length && $activeThumb[0].scrollIntoView) {
        $activeThumb[0].scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
    }
}

function goPrevImage() {
    if (currentImageIndex > 0) {
        currentImageIndex -= 1;
        renderGallery();
    }
}

function goNextImage() {
    const settings = initSettings();
    if (currentImageIndex < settings.images.length - 1) {
        currentImageIndex += 1;
        renderGallery();
    }
}

function gotoImage(idx) {
    const settings = initSettings();
    if (idx >= 0 && idx < settings.images.length) {
        currentImageIndex = idx;
        renderGallery();
    }
}

// Snap the view to the newest image. storeSceneImage() has already pushed
// the new entry into settings.images by the time this runs.
function snapToNewestImage() {
    const settings = initSettings();
    currentImageIndex = settings.images.length - 1;
    renderGallery();
}

function updatePanelStatus(message) {
    const status = $('#image-generator-panel .img-gen-status');
    if (status.length === 0) return;
    if (message) {
        status.text(message).show();
    } else {
        status.hide();
    }
}

// ---------------------------------------------------------------------------
// Main event handler
// ---------------------------------------------------------------------------

async function onCharacterMessageRendered(messageId) {
    if (typeof messageId !== 'number' || messageId < 0) return;

    const message = chat[messageId];
    if (!message || !message.mes) return;

    const settings = initSettings();
    if (!settings.autoGenerate) return;

    // Always re-render the message display so marker spans are applied,
    // even if there's nothing to image-generate.
    updateMessageDisplay(messageId);

    // Kick off NPC introductions in parallel with scene image generation.
    // Introductions don't block scene images — both streams progress
    // independently so the scene shows up fast.
    handleIntroductions(message.mes);

    // Player portrait + NPC appearance updates run in parallel too.
    handlePlayerUpdate(message.mes);
    handleAppearanceUpdates(message.mes);

    // Extract any new ITEM / LORE entries into the codex panel.
    handleCodexEntries(message.mes);

    // If the Narrator emitted [RESET_STORY: "..."], run the timeline-
    // collapse sequence after the player has had a moment to read the
    // response. Schedule it asynchronously so the rest of the render
    // (image generation, marker spans) completes first.
    const resetMarker = detectSenseMarkers(message.mes).find(m => m.triggersReset);
    if (resetMarker) {
        console.log('[Image Generator] RESET_STORY marker detected — queueing timeline collapse');
        setTimeout(() => { handleResetStory(); }, 1500);
        // Continue rendering scene images below — the overlay will
        // appear on top of them, then wipe everything.
    }

    // Scene images
    const imageMarkers = detectImageMarkers(message.mes);
    for (const marker of imageMarkers) {
        updatePanelStatus(`⏳ Generating image... "${marker.description.substring(0, 50)}..."`);
        console.log(`[Image Generator] Queue scene: ${marker.description.substring(0, 80)}`);

        const imageData = await generateSceneImage(marker.description);
        if (imageData) {
            storeSceneImage(imageData);
            snapToNewestImage();
            updatePanelStatus('');
        } else {
            updatePanelStatus('❌ Image generation failed. Check console.');
        }
    }
}

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

// Safety net: if a chat opens and there are scene markers in the last
// assistant message but no generated images yet, retry the scene
// generation path once. Handles the case where the extension booted
// before the Flask SD backend was reachable (common on cold docker
// start) and the greeting's [GENERATE_IMAGE] was dropped silently.
async function onChatChanged() {
    const settings = initSettings();
    // Re-seed Remnant on chat change — the Narrator character might only
    // become available (this_chid set) after the chat has opened.
    try { seedRemnantNpc(); } catch (err) { console.warn('[Image Generator] seedRemnantNpc (chat change) failed', err); }
    renderNpcRoster();

    // Give ST a moment to finish rendering the greeting DOM.
    await new Promise(r => setTimeout(r, 600));
    if (!Array.isArray(chat) || chat.length === 0) return;

    // Find the most recent non-user message.
    let lastIdx = -1;
    for (let i = chat.length - 1; i >= 0; i--) {
        if (chat[i] && chat[i].is_user === false) { lastIdx = i; break; }
    }
    if (lastIdx < 0) return;

    const lastMessage = chat[lastIdx];
    updateMessageDisplay(lastIdx);

    // Only retry image generation if we have scene markers AND no image yet.
    const imageMarkers = detectImageMarkers(lastMessage.mes || '');
    if (imageMarkers.length === 0) return;
    if (settings.images && settings.images.length > 0) return;

    console.log('[Image Generator] CHAT_CHANGED: greeting has scene markers but no image — retrying');
    const ready = await waitForSdReady({ timeoutMs: 180000, label: '(chat open)' });
    if (!ready) return;

    for (const marker of imageMarkers) {
        updatePanelStatus(`⏳ Generating image... "${marker.description.substring(0, 50)}..."`);
        const imageData = await generateSceneImage(marker.description);
        if (imageData) {
            storeSceneImage(imageData);
            snapToNewestImage();
            updatePanelStatus('');
        } else {
            updatePanelStatus('❌ Image generation failed. Check console.');
        }
    }

    // Also fire off introduction / codex / player handlers for the greeting.
    try { handleIntroductions(lastMessage.mes || ''); } catch (_) {}
    try { handlePlayerUpdate(lastMessage.mes || ''); } catch (_) {}
    try { handleCodexEntries(lastMessage.mes || ''); } catch (_) {}
}

function initializeExtension() {
    const settings = initSettings();

    console.log('[Image Generator] Initializing...');

    createSidePanel();
    createNpcRosterPanel();
    createSpeakerSpotlight();

    eventSource.on(event_types.CHARACTER_MESSAGE_RENDERED, onCharacterMessageRendered);
    eventSource.on(event_types.CHAT_CHANGED, onChatChanged);

    currentImageIndex = settings.images.length - 1;
    renderGallery();

    // Hydrate NPCs from ST character cards that already carry our
    // extensions metadata (previous sessions, hand-edited cards). This
    // is best-effort — if characters haven't loaded yet, the roster
    // will simply start empty and fill as new NPCs are introduced.
    try {
        hydrateNpcsFromCards();
    } catch (err) {
        console.warn('[Image Generator] NPC hydration failed:', err);
    }

    // Pre-seed The Remnant using the Narrator character's own avatar so
    // he appears in the roster from turn 1 and dialogue styling picks up
    // his violet color on his very first spoken line.
    try {
        seedRemnantNpc();
    } catch (err) {
        console.warn('[Image Generator] seedRemnantNpc failed:', err);
    }

    renderNpcRoster();
    renderCodex();

    const npcCount = Object.keys(settings.npcs).length;
    console.log(`[Image Generator] Ready. Images: ${settings.images.length}, NPCs locked: ${npcCount}`);
}

export function activate() {
    initializeExtension();
}

export function disable() {
    eventSource.off(event_types.CHARACTER_MESSAGE_RENDERED, onCharacterMessageRendered);
    $('#image-generator-panel').remove();
    $('#image-generator-npc-roster').remove();
    updateBackgroundWallpaper(null);
}

initializeExtension();
