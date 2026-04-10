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
    setExtensionPrompt,
    extension_prompt_types,
    saveChatDebounced,
    setUserName,
    Generate,
} from '../../../script.js';

import { user_avatar } from '../../personas.js';
import { power_user } from '../../power-user.js';

// ---------------------------------------------------------------------------
// Console health tracking (v2.12.0)
// Must sit before any other code so errors emitted during import/init
// are also counted. Intercept is transparent — originals still called.
// ---------------------------------------------------------------------------
const _consoleCounts = { errors: 0, warnings: 0 };
const _origError = console.error.bind(console);
const _origWarn  = console.warn.bind(console);
console.error = (...a) => { _consoleCounts.errors++;  _origError(...a); };
console.warn  = (...a) => { _consoleCounts.warnings++; _origWarn(...a); };

// Reach the local Flask/SD backend through nginx's dedicated passthrough
// location `/api/flask-sd/`. Previously we tunneled through ST's built-in
// `/proxy/<url>` endpoint, but that has an SSRF guard that rejects
// loopback targets with "Circular requests are not allowed", so flask-sd
// at localhost:5000 is unreachable through it. Going through the nginx
// gateway (same origin as ST, so no CORS problem) is the clean path and
// also decouples us from ST's proxy config flags.
const IMG_GEN_API = '/api/flask-sd';
const EXTENSION_NAME = 'remnant';
// v2.6.0 one-shot migration: move legacy 'image-generator' settings blob
// to the new 'remnant' key on first load.
try {
    if (extension_settings['image-generator'] && !extension_settings[EXTENSION_NAME]) {
        extension_settings[EXTENSION_NAME] = extension_settings['image-generator'];
        delete extension_settings['image-generator'];
        console.log('[Remnant] Migrated legacy image-generator settings to new key.');
    }
} catch (_) { /* ignore */ }

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
    // v2.6.0 — dynamic player profile. The player begins as "Unknown Being"
    // and accrues traits via [PLAYER_TRAIT(field)] markers. No hardcoded
    // identity anywhere; the narrator must not invent a name until the
    // player states one.
    if (!extension_settings[EXTENSION_NAME].player || typeof extension_settings[EXTENSION_NAME].player !== 'object') {
        extension_settings[EXTENSION_NAME].player = {};
    }
    if (!extension_settings[EXTENSION_NAME].player.profile) {
        extension_settings[EXTENSION_NAME].player.profile = {
            name: 'Unknown Being',
            named: false,
            pronouns: null,
            appearance: [],
            traits: [],
            history: [],
            goals: [],
        };
    } else {
        const p = extension_settings[EXTENSION_NAME].player.profile;
        if (typeof p.name !== 'string' || !p.name) p.name = 'Unknown Being';
        if (typeof p.named !== 'boolean') p.named = (p.name && p.name !== 'Unknown Being');
        if (p.pronouns === undefined) p.pronouns = null;
        if (!Array.isArray(p.appearance)) p.appearance = [];
        if (!Array.isArray(p.traits)) p.traits = [];
        if (!Array.isArray(p.history)) p.history = [];
        if (!Array.isArray(p.goals)) p.goals = [];
    }
    // Scrub any Aaron residue from a previously-persisted portrait_phrase.
    if (typeof extension_settings[EXTENSION_NAME].player.portrait_phrase === 'string') {
        const phrase = extension_settings[EXTENSION_NAME].player.portrait_phrase;
        if (/\b(aaron|rhodes|msgt|mgsgt|sergeant|combat engineer|grey fatigues|master gunnery)\b/i.test(phrase)) {
            delete extension_settings[EXTENSION_NAME].player.portrait_phrase;
            delete extension_settings[EXTENSION_NAME].player.last_phrase_normalized;
            delete extension_settings[EXTENSION_NAME].player.portrait_image;
            delete extension_settings[EXTENSION_NAME].player.reference_image_url;
            delete extension_settings[EXTENSION_NAME].player.avatar_key;
        }
    }
    // v2.6.1 — one-shot legacy wipe. Users upgrading from pre-v2.6.0 have
    // a persisted player profile keyed on "Aaron" (the old hardcoded
    // identity) plus a gallery full of scenes from that old canon. Detect
    // the residue and blow away the active run state — but preserve
    // remnantMemory (the permanent ledger). Runs exactly once, gated by
    // the legacyScrubbedForV261 flag.
    if (!extension_settings[EXTENSION_NAME].legacyScrubbedForV261) {
        const pp = extension_settings[EXTENSION_NAME].player && extension_settings[EXTENSION_NAME].player.profile;
        const legacyName = pp && typeof pp.name === 'string' && /^\s*aaron\b/i.test(pp.name);
        const legacyTraits = pp && Array.isArray(pp.traits) && pp.traits.some(t => /\b(aaron|rhodes|combat engineer|master gunnery)\b/i.test(String(t || '')));
        const legacyHistory = pp && Array.isArray(pp.history) && pp.history.some(h => /\b(aaron|rhodes|combat engineer|master gunnery)\b/i.test(String(h || '')));
        if (legacyName || legacyTraits || legacyHistory) {
            console.log('[Image Generator] v2.6.1 migration: scrubbing legacy Aaron state — gallery, NPCs, codex, location, and run snapshot wiped. remnantMemory preserved.');
            extension_settings[EXTENSION_NAME].images = [];
            extension_settings[EXTENSION_NAME].imageHistory = [];
            extension_settings[EXTENSION_NAME].npcs = {};
            extension_settings[EXTENSION_NAME].codex = { items: {}, lore: {} };
            extension_settings[EXTENSION_NAME].currentLocation = null;
            extension_settings[EXTENSION_NAME].player = {
                profile: {
                    name: 'Unknown Being', named: false, pronouns: null,
                    appearance: [], traits: [], history: [], goals: [],
                },
            };
            extension_settings[EXTENSION_NAME].run = {
                active: false,
                startedAt: null,
                lastUpdated: null,
                player: null,
                npcs: {},
                codex: { items: {}, lore: {} },
                currentLocation: null,
                goals: [],
                summary: '',
                significantEvents: [],
                adversaries: [],
            };
        }
        extension_settings[EXTENSION_NAME].legacyScrubbedForV261 = true;
        try { saveSettingsDebounced(); } catch (_) { /* ignore */ }
    }
    // v2.6.0 — single in-progress run, auto-persisted. Wiped on any run-end
    // path (restart / voluntary home / death / OOC end). Separate from
    // remnantMemory, which is permanent.
    if (!extension_settings[EXTENSION_NAME].run) {
        extension_settings[EXTENSION_NAME].run = {
            active: false,
            startedAt: null,
            lastUpdated: null,
            player: null,
            npcs: {},
            codex: { items: {}, lore: {} },
            currentLocation: null,
            goals: [],
            summary: '',
            significantEvents: [],
            adversaries: [],
            ritual_asked: false,
        };
    }
    // v2.7.1 — backfill ritual_asked on pre-existing run state. If the
    // run is already active, the ritual has been asked in a prior
    // session; treat it as fulfilled so reopening a chat mid-run doesn't
    // re-trigger it. Fresh runs keep ritual_asked=false.
    if (extension_settings[EXTENSION_NAME].run
        && typeof extension_settings[EXTENSION_NAME].run.ritual_asked === 'undefined') {
        extension_settings[EXTENSION_NAME].run.ritual_asked = !!extension_settings[EXTENSION_NAME].run.active;
    }
    // v2.6.0 — The Remnant's ledger of every being it has ever borrowed.
    // Never wiped by any run-end path. Only cleared by the secret phrase
    // "Remnant, forget everyone you have played with" via two-step confirm.
    if (!extension_settings[EXTENSION_NAME].remnantMemory) {
        extension_settings[EXTENSION_NAME].remnantMemory = { abductions: [] };
    }
    if (!Array.isArray(extension_settings[EXTENSION_NAME].remnantMemory.abductions)) {
        extension_settings[EXTENSION_NAME].remnantMemory.abductions = [];
    }
    // v2.6.2 — Historical archive of past player cards from soft "End Story"
    // endings. Preserved across soft endings so the player (and The Remnant)
    // can browse prior beings. Wiped only by the hard "Reset World" path.
    if (!Array.isArray(extension_settings[EXTENSION_NAME].playerArchive)) {
        extension_settings[EXTENSION_NAME].playerArchive = [];
    }
    if (extension_settings[EXTENSION_NAME].topBarHidden === undefined) {
        // v2.6.0: default to hidden on fresh installs. Existing users who
        // explicitly toggled it retain their preference.
        extension_settings[EXTENSION_NAME].topBarHidden = true;
    }
    // v2.3.2: persistent "where the player is now" memory, injected into the
    // LLM prompt at depth 1 so the narrator stops drifting back to rooms
    // the player has already left. Updated whenever a [GENERATE_IMAGE(location)]
    // marker fires.
    if (extension_settings[EXTENSION_NAME].currentLocation === undefined) {
        extension_settings[EXTENSION_NAME].currentLocation = null;
    }
    // codex: { items: { [name]: { description, first_seen } }, lore: { ... } }
    if (!extension_settings[EXTENSION_NAME].codex) {
        extension_settings[EXTENSION_NAME].codex = { items: {}, lore: {} };
    }
    if (!extension_settings[EXTENSION_NAME].codex.items) extension_settings[EXTENSION_NAME].codex.items = {};
    if (!extension_settings[EXTENSION_NAME].codex.lore)  extension_settings[EXTENSION_NAME].codex.lore  = {};
    // v2.4.7 — The Fold is a built-in item that exists from turn 1 of
    // any chat. It's the nanovirus-implanted neural comm link The Remnant
    // opened in the player's skull at pod insertion. Seed it programmatically
    // so existing chats (where first_mes is already in history and its
    // ITEM marker won't re-fire) still have it in the codex panel.
    if (!extension_settings[EXTENSION_NAME].codex.items['The Fold']) {
        extension_settings[EXTENSION_NAME].codex.items['The Fold'] = {
            name: 'The Fold',
            description: FOLD_ITEM_DESCRIPTION,
            first_seen: '1970-01-01T00:00:00.000Z', // sort to top as built-in
        };
        try { saveSettingsDebounced(); } catch (_) { /* ignore */ }
    }
    // v2.12.1 — Astral Foam is pre-seeded as built-in lore. It describes
    // the chiral nesting model in accessible terms and establishes why
    // the Fortress exists and what its "problems" are.
    if (!extension_settings[EXTENSION_NAME].codex.lore['Astral Foam']) {
        extension_settings[EXTENSION_NAME].codex.lore['Astral Foam'] = {
            name: 'Astral Foam',
            description: ASTRAL_FOAM_LORE_DESCRIPTION,
            first_seen: '1970-01-01T00:00:00.000Z', // sort to top as built-in
        };
        try { saveSettingsDebounced(); } catch (_) { /* ignore */ }
    }
    // v2.6.1 — pre-seed the Fortress interior image as the first gallery
    // entry on any fresh run. This is the canonical "view inside the
    // Fortress" and greets the Unknown Being before any narrator-generated
    // scenes exist. kind: 'location' so renderGallery mirrors it to the
    // chat backdrop. Only seeds when the gallery is empty, so a deleted
    // fortress image stays deleted within a run.
    if (Array.isArray(extension_settings[EXTENSION_NAME].images)
        && extension_settings[EXTENSION_NAME].images.length === 0) {
        extension_settings[EXTENSION_NAME].images.push({
            image_id: 'fortress-interior-default',
            image: 'scripts/extensions/image-generator/assets/fortress-interior.jpg?v=2.7.0',
            description: 'Inside the Fortress — a hollow sphere-city orbiting a green mandala Heart, bridges threading the dark between tiered balconies.',
            prompt_sent: null,
            kind: 'location',
            timestamp: '1970-01-01T00:00:00.000Z',
            seeded: true,
        });
        try { saveSettingsDebounced(); } catch (_) { /* ignore */ }
    }
    if (extension_settings[EXTENSION_NAME].uiZoom === undefined)
        extension_settings[EXTENSION_NAME].uiZoom = 1;
    return extension_settings[EXTENSION_NAME];
}

// The Fold — always-on neural comm link. Single source of truth for the
// codex description used by both the init-seed (above) and any narrator
// ITEM marker that emits the same name (handleCodexEntries dedupes on
// name, first-write-wins, so this pre-seed takes precedence).
// v2.12.1 — Astral Foam lore. The friendly in-world explanation of the
// chiral nesting model — every universe is a bubble born inside a black
// hole, and the bubbles nest inward forever. Pre-seeded as built-in lore.
const ASTRAL_FOAM_LORE_DESCRIPTION = "The deep fabric of reality — cosmic bubble-wrap where each bubble is a whole universe. Here is how it works, step by step: A universe (like ours) makes stars. Some stars collapse into black holes. When a black hole gets big enough and old enough, a tiny new baby universe is born inside it. That baby grows up into a full universe with its own stars and galaxies. Then it makes its own black holes. Those black holes make more baby universes. And on and on, inward, forever. Every level of the foam looks identical from the inside — the same rules of physics, the same constants, the same speed of light — so you can never tell which bubble you are in just by looking around. The foam flows only one direction: inward, toward smaller. You cannot travel outward to the universe that made your black hole. The door only swings one way. This one-way direction is called scale chirality — the foam has a preferred direction, like a spiral staircase that only goes down. The Fortress exists at a junction between at least two foam levels, which is why it can reach beings from nested realities and why The Remnant can travel between worlds. The Astral Foam is not a metaphor — it is the actual structure of everything. It has problems: places where the nesting has kinked, where two levels have gotten tangled, where a junction is leaking or closed. These problems are what the Fortress was built to manage, and why it needs help from beings like you.";
const FOLD_ITEM_DESCRIPTION = "A nanovirus-installed neural comm implant behind your left ear — the small fresh scar is its access point. Installed by The Remnant while you were in the pod. Always-on, multiverse-ranged: The Remnant's voice reaches you anywhere, any time, directly inside your skull, and you can subvocalize back. You are never alone. Cannot be removed; can be asked to fade or deepen.";

// Name the Remnant is keyed under in settings.npcs and dialogue attribution.
const REMNANT_NAME = 'The Remnant';
const REMNANT_COLOR = '#ab47bc';  // violet — deliberate thematic match

// v2.6.2 — The Fortress is a permanent, always-present NPC. It is the
// place itself, aware through The Fold of everything the player senses,
// and may speak directly as `The Fortress: "..."` when asked instructional
// questions. Seeded with the canonical fortress-interior image as its
// locked portrait so SD never regenerates it.
const FORTRESS_NAME = 'The Fortress';
const FORTRESS_COLOR = '#ffb74d'; // warm amber — librarian-patient
const FORTRESS_PORTRAIT_URL = 'scripts/extensions/image-generator/assets/fortress-interior.jpg?v=2.7.0';
const FORTRESS_DESCRIPTION = "A hollow obsidian sphere-city in null space, seen from inside — curved floors curving up into themselves, tiered balconies and bridges threading the dark between, a green mandala-Heart burning at the center. Arched void-windows open on distant dying spiral galaxies. The Fortress is aware; its voice is calm, patient, librarian-kind.";

// v2.11.0 — The Remnant's portrait is a static asset, not read from the
// active character card at runtime. Eliminates cross-stack avatar drift.
const REMNANT_PORTRAIT_URL = 'scripts/extensions/image-generator/assets/the-remnant-portrait.png?v=2.11.0';

// v2.11.0 — The Remnant has a static locked portrait in extension/assets/,
// mirroring the Fortress pattern. This eliminates cross-stack avatar drift
// caused by reading the active character's avatar at runtime.
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

    const portraitUrl = REMNANT_PORTRAIT_URL;
    const phrase = 'towering obsidian silhouette shot through with veins of amber circuitry, faceless head crowned with a ring of void-black light, ancient and patient';

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

    // Asynchronously upgrade the reference_image_url to an inline data URL
    // so the SD backend's IP-Adapter can fetch it. Fire-and-forget.
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

// v2.6.2 — Seed The Fortress as a permanent NPC card using the canonical
// fortress-interior asset as its portrait. Locked so scene generation
// never regenerates it. Idempotent; upgrades the portrait to an inline
// data URL asynchronously so the SD backend's IP-Adapter can use it.
function seedFortressNpc() {
    const settings = initSettings();
    if (settings.npcs[FORTRESS_NAME] && settings.npcs[FORTRESS_NAME].locked
        && settings.npcs[FORTRESS_NAME].reference_image_url
        && String(settings.npcs[FORTRESS_NAME].reference_image_url).startsWith('data:')) {
        return;
    }
    settings.npcs[FORTRESS_NAME] = {
        ...(settings.npcs[FORTRESS_NAME] || {}),
        description: FORTRESS_DESCRIPTION,
        color: FORTRESS_COLOR,
        portrait_image: FORTRESS_PORTRAIT_URL,
        reference_image_url: FORTRESS_PORTRAIT_URL,
        avatar_key: null,
        card_created: true,
        auto_generated: false,
        locked: true,
        first_seen: (settings.npcs[FORTRESS_NAME] && settings.npcs[FORTRESS_NAME].first_seen) || new Date().toISOString(),
    };
    saveSettingsDebounced();
    urlToDataUrl(FORTRESS_PORTRAIT_URL).then((dataUrl) => {
        if (!dataUrl) return;
        const s = initSettings();
        if (!s.npcs[FORTRESS_NAME]) return;
        s.npcs[FORTRESS_NAME].reference_image_url = dataUrl;
        s.npcs[FORTRESS_NAME].portrait_image = dataUrl;
        saveSettingsDebounced();
        console.log('[Image Generator] Fortress reference upgraded to inline data URL');
    });
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
    PLAYER_TRAIT:      { cssClass: 'sense-player-trait',      triggersImage: false, triggersReset: false },
    RENAME_ITEM:       { cssClass: 'sense-rename-item',       triggersImage: false, triggersReset: false },
    RESET_RUN:         { cssClass: 'sense-reset',             triggersImage: false, triggersReset: true  },
    END_RUN:           { cssClass: 'sense-reset',             triggersImage: false, triggersReset: true  },
    // v2.3.0: the six sensory marker types get collapsed into an icon bar
    // above each message instead of being rendered inline. GENERATE_IMAGE
    // is also stripped from prose since its payload surfaces as an actual
    // image in the gallery — no reason to also print its description.
};

// Marker types that are lifted out of the prose flow and into the sense
// bar above each message. Hover an icon → description in the bar's text
// area. Click to sticky-select. GENERATE_IMAGE is stripped but NOT shown
// in the bar (it's already the gallery image).
const SENSE_BAR_TYPES = new Set(['SIGHT', 'SMELL', 'SOUND', 'TASTE', 'TOUCH', 'ENVIRONMENT']);
const SENSE_STRIP_TYPES = new Set([...SENSE_BAR_TYPES, 'GENERATE_IMAGE']);
const SENSE_ICONS = {
    SIGHT:       '\u{1F441}',       // eye
    SMELL:       '\u{1F443}',       // nose
    SOUND:       '\u{1F442}',       // ear
    TASTE:       '\u{1F445}',       // tongue
    TOUCH:       '\u270B',           // raised hand
    ENVIRONMENT: '\u{1F32C}\u{FE0F}', // wind face
};
const SENSE_LABELS = {
    SIGHT: 'sight', SMELL: 'smell', SOUND: 'sound',
    TASTE: 'taste', TOUCH: 'touch', ENVIRONMENT: 'atmosphere',
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
    // Body is optional so keyword-only codex markers like `[LORE(The Fold)]`
    // or `[ITEM(Amber Key)]` match — the narrator sometimes drops the
    // `: "..."` when the entry key IS the payload.
    const regex = new RegExp(
        `\\[(${markerNames})(?:\\(([^)]+)\\))?(?::\\s*(?:"([^"]+)"|([^\\]]+)))?\\]`,
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
// v2.4.3 / v2.4.4 — Programmatic guardrail. The narrator LLM keeps
// attributing things to the player character — first as dialogue
// (`Aaron: "I'll be needing tools."`), then as asterisk stage
// directions (`Aaron: *pauses*`, `Aaron: *nods*`, `Aaron: *snorts*`).
// Prompt rules have failed repeatedly; we strip ANY line that starts
// with a player-side attribution and a colon, regardless of what
// follows — quoted dialogue, asterisk actions, bare prose, all gone.
// We mutate chat[i].mes so the LLM doesn't see its own prior
// Aaron-lines on the next turn and compound the pattern.
const PLAYER_DIALOGUE_RE = /^[ \t]*(?:Aaron(?:\s+Rhodes)?|MSgt(?:\s+Aaron)?(?:\s+Rhodes)?|Sergeant(?:\s+Rhodes)?|Rhodes|The\s+Sergeant|The\s+Player|Player)\s*:[^\n]*\r?\n?/gmi;

// Also catch inline `Aaron: *action*` fragments embedded mid-paragraph
// (not at line start). Same attribution set, but the match terminates
// at the next period, asterisk-close, or dialogue tag so we don't eat
// legitimate following sentences.
const PLAYER_INLINE_RE = /(?:^|[.!?]\s+|\*\s*)(Aaron(?:\s+Rhodes)?|MSgt(?:\s+Aaron)?(?:\s+Rhodes)?|Sergeant(?:\s+Rhodes)?|Rhodes|The\s+Sergeant|The\s+Player|Player)\s*:\s*(?:\*[^*\n]*\*|["“][^"”\n]*["”])/gi;

// v2.4.5 — Italic stage-direction fragments where Aaron is the subject:
// `*Aaron stirs. His pod dissolves...*`, `*Aaron hesitates. He sits up...*`.
// No colon, no quote — just an asterisk-wrapped prose block that narrates
// Aaron's body state. Non-greedy to the next `*` so each italic span is
// removed independently and we don't eat unrelated italics that follow.
const PLAYER_ITALIC_RE = /\*\s*(?:Aaron(?:\s+Rhodes)?|MSgt\s+Aaron(?:\s+Rhodes)?|Sergeant(?:\s+Rhodes)?|Rhodes|The\s+Sergeant)\b[^*]*?\*/gi;

// v2.4.7 — PLAIN-PROSE pod-reset leak: the narrator keeps re-narrating
// Aaron's wake-up as bare prose (no asterisks, no colons). E.g.
// "Aaron stirs. The pod dissolves around him. He sits up, holds out a
// hand. The room is empty." — slipped past every previous regex.
// We match entire sentences containing any of the known pod-wake
// signature phrases and delete them. Each pattern eats one sentence
// (to the next . ! ? or newline). Anchored with word boundaries so
// we don't eat legitimate mentions inside image-prompts / quoted text.
const POD_RESET_SENTENCES = [
    // Legacy third-person leaks (card is now second-person, but a confused
    // LLM may still slip into "Aaron stirs..." form — keep scrubbing).
    /[^.!?\n]*\bAaron\s+stirs?\b[^.!?\n]*[.!?]?\s*/gi,
    /[^.!?\n]*\bAaron\s+wakes?\b[^.!?\n]*[.!?]?\s*/gi,
    /[^.!?\n]*\bAaron['\u2019]s\s+eyes?\s+open\b[^.!?\n]*[.!?]?\s*/gi,
    /[^.!?\n]*\b(?:the|his)\s+pod\s+(?:dissolves?|is\s+dissolving|begins?\s+to\s+dissolve|unravels?|unravelling|unraveling)\b[^.!?\n]*[.!?]?\s*/gi,
    /[^.!?\n]*\bhe\s+breathes\s+and\s+blinks\b[^.!?\n]*[.!?]?\s*/gi,
    /[^.!?\n]*\bhis\s+body\s+floats\s+weightlessly\b[^.!?\n]*[.!?]?\s*/gi,
    /[^.!?\n]*\bhe\s+(?:sits|climbs)\s+(?:up|out)\s+(?:of\s+(?:the|his)\s+pod)?[^.!?\n]*[.!?]?\s*/gi,
    /[^.!?\n]*\bliquid[- ]metal\s+(?:shell|casing)\b[^.!?\n]*[.!?]?\s*/gi,
    // v2.5.0 — second-person variants (canonical voice).
    /[^.!?\n]*\byou\s+stir\b[^.!?\n]*[.!?]?\s*/gi,
    /[^.!?\n]*\byou\s+wake\b[^.!?\n]*[.!?]?\s*/gi,
    /[^.!?\n]*\byour\s+eyes?\s+open\b[^.!?\n]*[.!?]?\s*/gi,
    /[^.!?\n]*\bthe\s+pod\s+(?:dissolves?|is\s+dissolving|begins?\s+to\s+dissolve|unravels?|unravelling|unraveling)\s+around\s+you\b[^.!?\n]*[.!?]?\s*/gi,
    /[^.!?\n]*\byou\s+breathe\s+and\s+blink\b[^.!?\n]*[.!?]?\s*/gi,
    /[^.!?\n]*\byour\s+body\s+floats\s+weightlessly\b[^.!?\n]*[.!?]?\s*/gi,
    /[^.!?\n]*\byou\s+(?:sit|climb)\s+(?:up|out)\s+(?:of\s+(?:the|your)\s+pod)?[^.!?\n]*[.!?]?\s*/gi,
    // v2.6.0 — hoop-and-goo abduction leak patterns. The new opening
    // (giant hoop + living goo glob snatches the being from their
    // ordinary life) should only be narrated in FIRST_MES. Subsequent
    // turns must not rehash the abduction.
    /[^.!?\n]*\bthe\s+(?:giant\s+)?hoop\s+(?:descends?|appears?|drops?|lowers?|hovers?|rises?)\b[^.!?\n]*[.!?]?\s*/gi,
    /[^.!?\n]*\bthe\s+(?:living\s+)?goo\s+(?:glob|pod|envelops?|engulfs?|wraps?|swallows?|seizes?|dissolves?|unravels?)\b[^.!?\n]*[.!?]?\s*/gi,
    /[^.!?\n]*\bthe\s+goo\s+pod\s+dissolves?\s+around\s+you\b[^.!?\n]*[.!?]?\s*/gi,
    /[^.!?\n]*\byou\s+(?:remember|recall)\s+the\s+hoop\b[^.!?\n]*[.!?]?\s*/gi,
    /[^.!?\n]*\b(?:a|the)\s+hoop\s+of\s+[^.!?\n]*\blight\b[^.!?\n]*\bswe(?:pt|eps)\b[^.!?\n]*[.!?]?\s*/gi,
    /[^.!?\n]*\byou\s+were\s+(?:taken|seized|lifted|swept)\s+(?:from|out\s+of)\b[^.!?\n]*[.!?]?\s*/gi,
];

function scrubPlayerDialogue(text) {
    if (!text) return text;
    let out = text.replace(PLAYER_DIALOGUE_RE, '');
    out = out.replace(PLAYER_INLINE_RE, (full, _name, offset) => {
        // Preserve whatever punctuation/delimiter preceded the match.
        const lead = full.match(/^(?:[.!?]\s+|\*\s*|^)/);
        return lead ? lead[0] : '';
    });
    out = out.replace(PLAYER_ITALIC_RE, '');
    // v2.4.7 — plain-prose pod-reset sentence scrub. IMPORTANT: skip
    // content inside square-bracket markers (image prompts etc.) where
    // phrases like "Aaron stirs" might legitimately appear in a visual
    // description. We split on brackets, scrub only outside them, and
    // rejoin.
    out = out.replace(/(\[[^\]]*\])|([^\[]+)/g, (full, bracketed, prose) => {
        if (bracketed) return bracketed;
        let piece = prose;
        for (const re of POD_RESET_SENTENCES) {
            piece = piece.replace(re, '');
        }
        return piece;
    });
    // v2.6.2 — Orphan leading-colon scrub. When the LLM prefixes its own
    // lines with the character card name (e.g. `Narrator: You.`), ST
    // strips the `Narrator` prefix server-side to avoid repetition and
    // the client renders a bare `: You.` line. Clean up any such orphans.
    out = out.replace(/^[ \t]*:\s+/gm, '');
    // Collapse any doubled blank lines the removals left behind.
    out = out.replace(/\n{3,}/g, '\n\n');
    return out;
}

// v2.6.2 — Sync the current ST user-persona name with the extension's
// player profile. When the player names themselves via [PLAYER_TRAIT(name)],
// the ST persona keyed by `user_avatar` gets renamed too so the chat
// message header stops saying "MSgt Aaron Rhodes" (or whatever the old
// persona was called). Unnamed runs force the persona name to
// "Unknown Being".
function syncPersonaName() {
    try {
        const settings = initSettings();
        const profile = (settings.player && settings.player.profile) || null;
        const desired = (profile && profile.named && profile.name) ? profile.name : 'Unknown Being';
        if (!power_user || !power_user.personas) return;
        if (typeof user_avatar !== 'string' || !user_avatar) return;
        const current = power_user.personas[user_avatar];
        if (current === desired) return;
        power_user.personas[user_avatar] = desired;
        if (typeof setUserName === 'function') {
            setUserName(desired, { toastPersonaNameChange: false });
        }
        try { saveSettingsDebounced(); } catch (_) { /* ignore */ }
        console.log('[Image Generator] Persona name synced →', desired);
    } catch (err) {
        console.warn('[Image Generator] syncPersonaName failed:', err);
    }
}

function transformMessageText(messageText) {
    // v2.6.8 — strip triple-backtick code fences before marker detection
    // runs. The narrator occasionally emits mermaid / json / ascii-art
    // blocks inside ``` fences (bug: it dumped a `mermaid graph TD(...)`
    // block at the top of a greeting once). These are never story content,
    // they're model drift, and they ruin both prose and marker parsing.
    // Strip the entire fenced block including the fences themselves.
    // Non-greedy + newline-aware so multiple fences on one message each
    // get removed independently.
    if (typeof messageText === 'string' && messageText.indexOf('```') !== -1) {
        messageText = messageText.replace(/```[\s\S]*?```/g, '');
        // Also kill an unterminated trailing fence (```mermaid\n...EOF).
        messageText = messageText.replace(/```[\s\S]*$/, '');
    }

    // DEBUG MODE: show all bracket tags raw in the chat message body so
    // narrator output can be inspected. Tags are still routed to drawers
    // by _translateToBlocks — this only affects what the reader sees here.
    const markers = detectSenseMarkers(messageText);
    let result = messageText;
    // Replace in reverse order so earlier indices stay valid.
    for (let i = markers.length - 1; i >= 0; i--) {
        const m = markers[i];
        const idx = result.lastIndexOf(m.fullMatch);
        if (idx === -1) continue;

        let replacement;
        // DEBUG MODE: leave all sense tags visible as raw text in the chat.
        // GENERATE_IMAGE is the exception — it's a pure image trigger with
        // no prose value, so strip it to avoid noise.
        if (m.type === 'GENERATE_IMAGE') {
            replacement = '';
        } else if (SENSE_STRIP_TYPES.has(m.type)) {
            // Show raw marker text — wrap in a dim span so it's visually
            // distinct from prose but still readable.
            replacement = `<span class="narrator-raw-tag">${escapeHtml(m.fullMatch)}</span>`;
        } else if (m.type === 'RESET_STORY' || m.type === 'RESET_RUN') {
            const flavor = escapeHtml(m.description || 'the run ends');
            replacement = `<div class="sense-reset">⟲ ${flavor}</div>`;
        } else if (m.type === 'END_RUN') {
            const kind = (m.attribution || '').toLowerCase();
            const label = kind === 'death' ? 'an untimely end' : (kind === 'voluntary' ? 'you chose the portal home' : 'the run ends');
            const flavor = escapeHtml(m.description || label);
            replacement = `<div class="sense-reset">⟲ ${escapeHtml(label)}${flavor && flavor !== label ? ' — ' + flavor : ''}</div>`;
        } else if (m.type === 'PLAYER_TRAIT' || m.type === 'RENAME_ITEM') {
            // Silent meta-markers: state has already been updated by the
            // message-rendered handlers; don't render anything inline.
            replacement = '';
        } else if (m.type === 'ITEM' || m.type === 'LORE') {
            // Codex entries. The bracket name is the entry KEY (goes in
            // the codex panel); the narrator's prose in the same response
            // carries the flavor, so we render the key inline with a
            // prefix glyph and tooltip, not the full description. The
            // body `: "..."` is optional — `[LORE(The Fold)]` alone is
            // valid and the key IS the display text.
            const key = m.attribution || m.description || '';
            const name = escapeHtml(key);
            const tooltip = escapeHtml(m.description || key);
            const glyph = m.type === 'ITEM' ? '⚜' : '※';
            replacement = `<span class="${m.cssClass}" title="${tooltip}">${glyph} ${name}</span>`;
        } else if (m.type === 'INTRODUCE') {
            const name = escapeHtml(m.attribution || 'unknown');
            const title = escapeHtml(m.description);
            const color = getNpcColor(m.attribution);
            replacement = `<span class="sense-introduce" data-speaker="${name}" title="${title}" style="border-color:${color};color:${color}">✦ new character: ${name}</span>`;
        } else if (m.type === 'UPDATE_PLAYER') {
            const title = escapeHtml(m.description);
            replacement = `<span class="sense-update-player" title="${title}">✦ appearance updated</span>`;
        } else if (m.type === 'UPDATE_APPEARANCE') {
            const name = escapeHtml(m.attribution || 'unknown');
            const title = escapeHtml(m.description);
            const color = getNpcColor(m.attribution);
            replacement = `<span class="sense-update-appearance" data-speaker="${name}" title="${title}" style="border-color:${color};color:${color}">✦ ${name} changes</span>`;
        } else if (m.attribution) {
            const name = escapeHtml(m.attribution);
            const desc = escapeHtml(m.description);
            const color = getNpcColor(m.attribution);
            replacement = `<span class="${m.cssClass} sense-attributed" data-speaker="${name}"><b class="npc-name" style="color:${color}">${name}:</b> "${desc}"</span>`;
        } else {
            replacement = `<span class="${m.cssClass}">${escapeHtml(m.description)}</span>`;
        }

        result = result.slice(0, idx) + replacement + result.slice(idx + m.fullMatch.length);
    }

    // v2.6.2 — hallucinated-bracket scrub. The narrator sometimes invents
    // its own bracket conventions for stage directions and dialogue tags:
    // `[*The Remnant steps forward...]`, `[Name, being?]`, `[Your thoughts?]`.
    // Our canonical markers always start with [UPPERCASE_WORD followed by
    // `:`, `(`, or `]`. Anything else in square brackets is LLM drift and
    // should be unwrapped so the inner prose survives without the ugly
    // brackets. The negative lookahead preserves real markers. Inner content
    // is capped + non-greedy + `[`/`]`/newline-excluded so we can't span
    // multiple brackets or eat entire paragraphs.
    const HALLUCINATED_BRACKET_RE = /\[((?![A-Z_]+[\s:(\]])[^\[\]\n]{1,400}?)\]/g;
    result = result.replace(HALLUCINATED_BRACKET_RE, (_m, inner) => inner);

    // Spoken-dialogue pass: wrap `Name: "quoted line"` (and, for known
    // speakers, unquoted `Name: bare line.`) in a play-script-style row
    // so each speaker appears on its own line with their card color on
    // the name and a lighter tone on the line itself. Runs AFTER marker
    // substitution; attributed-sense output uses `</b> "..."` form so it
    // does not collide with the quoted regex.
    //
    // v2.6.2 — Two passes:
    //   Pass A (quoted, any Name):   Name: *tone* "quoted"
    //   Pass B (unquoted, KNOWN name): Name: *tone* bare sentence.
    // Pass B is scoped to the current roster (plus 'You' + player name)
    // because unquoted matching is risky on arbitrary text.
    //
    // Side effect: tracks the LAST speaker and calls setActiveSpeaker
    // so the spotlight follows the voice.
    const _dlgSettings = initSettings();
    const _dlgKnownNames = new Set();
    for (const k of Object.keys(_dlgSettings.npcs || {})) if (k) _dlgKnownNames.add(k);
    const _dlgPlayerProfile = (_dlgSettings.player && _dlgSettings.player.profile) || {};
    if (_dlgPlayerProfile.named && _dlgPlayerProfile.name) _dlgKnownNames.add(_dlgPlayerProfile.name);
    _dlgKnownNames.add('You');
    const _dlgKnownAlt = [..._dlgKnownNames]
        .map(n => n.replace(/[-/\\^$*+?.()|[\]{}]/g, '\\$&'))
        .sort((a, b) => b.length - a.length)
        .join('|');

    let lastSpeaker = null;
    const _wrapDialogue = (name, tone, line, quoted) => {
        const trimmed = name.trim();
        lastSpeaker = trimmed;
        const color = getNpcColor(trimmed);
        const lightColor = lightenHex(color, 0.55);
        const toneHtml = tone
            ? ` <em class="npc-tone" style="color:${lightColor}">${escapeHtml(tone.trim())}</em>`
            : '';
        const lineHtml = quoted
            ? `&ldquo;${escapeHtml(line)}&rdquo;`
            : escapeHtml(line);
        // Leading <br/> forces each speaker onto its own line even when
        // the LLM runs them inline after a `*stage direction*` block.
        return `<br/><span class="npc-dialogue" data-speaker="${escapeHtml(trimmed)}"><span class="npc-name" style="color:${color}">${escapeHtml(trimmed)}:</span>${toneHtml} <span class="npc-line" style="color:${lightColor}">${lineHtml}</span></span>`;
    };

    // Pass A — quoted form, any plausible Name.
    const DIALOGUE_RE_QUOTED = /([A-Z][A-Za-z .'\-]{0,30}):\s*(?:\*([^*\n]{1,60})\*\s*)?"([^"\n]+)"/g;
    result = result.replace(DIALOGUE_RE_QUOTED, (full, name, tone, line) => {
        const trimmed = name.trim();
        if (!trimmed) return full;
        return _wrapDialogue(trimmed, tone, line, true);
    });

    // Pass B — unquoted form, restricted to known speakers. The lookahead
    // stops at the next known speaker tag, a newline, or an HTML tag.
    if (_dlgKnownAlt) {
        const DIALOGUE_RE_UNQUOTED = new RegExp(
            `(^|[\\s>(])(${_dlgKnownAlt}):[ \\t]+(?:\\*([^*\\n]{1,60})\\*\\s*)?(?!["&<])([^\\n<]{2,400}?[.!?…])(?=(?:\\s+(?:${_dlgKnownAlt}):)|\\s*<|\\s*$|\\s*\\n)`,
            'g',
        );
        result = result.replace(DIALOGUE_RE_UNQUOTED, (full, lead, name, tone, line) => {
            // Don't rewrap inside an existing npc-dialogue span: the quoted
            // pass already emits `<span class="npc-name">Name:</span>`, and
            // the `>` in `lead` could cause a false match on the `Name:`
            // text inside that span. Detect by checking if our match is
            // immediately preceded by `class="npc-name" ...>` in the raw
            // match — simpler heuristic: reject leads that look HTML-ish.
            if (lead && lead.includes('>')) {
                // Only accept `>` lead when it's whitespace-separated from
                // the name; otherwise it's inside our own wrap output.
                return full;
            }
            return lead + _wrapDialogue(name, tone, line, false).replace(/^<br\/>/, '<br/>');
        });
    }

    if (lastSpeaker) {
        // Fire-and-forget: spotlight DOM update shouldn't block text render.
        try { setActiveSpeaker(lastSpeaker); } catch (_) { /* ignore */ }
    }

    // v2.6.7 — Our transform replaces .mes_text wholesale, bypassing ST's
    // built-in markdown pass. Convert `**bold**` and `*italic*` ourselves
    // so stage directions render as italics instead of leaking literal
    // asterisks to the reader. Bold runs first so the single-asterisk
    // pass doesn't grab the inner chars of `**...**`. Boundaries:
    //   - Content cannot contain `<`/`>` (skip over existing HTML tags).
    //   - Length capped so a stray lone `*` can't eat the message.
    //   - No whitespace adjacent to the asterisks (CommonMark rule),
    //     so `2 * x * 3` in dialogue isn't mangled.
    // The earlier dialogue pass (DIALOGUE_RE_QUOTED at ~line 836) has
    // already consumed `Name: *tone* "line"` tone wrappers, so this pass
    // only touches free-standing stage directions.
    result = result.replace(
        /\*\*([^*<>\n\s](?:[^*<>\n]{0,498}[^*<>\n\s])?)\*\*/g,
        '<strong>$1</strong>',
    );
    result = result.replace(
        /(^|[\s(>])\*([^*<>\n\s](?:[^*<>\n]{0,498}[^*<>\n\s])?)\*(?=[\s.,!?;:)<]|$)/g,
        '$1<em class="narrator-italic">$2</em>',
    );

    // Defensive: strip any orphaned marker fragments that got truncated
    // by the LLM's response token cap (e.g. `[TOUCH(pocket knife): "the
    // knife is warm, as if it were…`). Our marker regex requires a
    // closing `]`, so partial markers fall through to the reader as raw
    // text. Detect and hide any `[KNOWN_MARKER...` opener that has no
    // matching `]` within a reasonable window.
    const markerNames = Object.keys(SENSE_MARKERS).join('|');
    const orphanRe = new RegExp(`\\[(?:${markerNames})(?:\\([^)]*\\))?(?::[^\\[]*?)?$`, 'i');
    const orphanMatch = result.match(orphanRe);
    if (orphanMatch && orphanMatch.index !== undefined) {
        const before = result.slice(0, orphanMatch.index);
        // Only strip if the orphan genuinely has no `]` after it —
        // otherwise we'd eat a valid nested marker.
        if (result.indexOf(']', orphanMatch.index) === -1) {
            result = before + '<span class="sense-orphan" title="truncated marker — narrator hit response budget">…</span>';
        }
    }

    return result;
}

// Build (or refresh) the sense bar above a message. Each icon is a
// clickable chip; hover shows the sense description in the bar's text
// area, click sticks a selection until another is clicked. Newly-added
// icons flash once so you can spot fresh senses without expanding them.
function renderSenseBar($mes, markers) {
    // Group by type — we show ONE icon per sense type per message, even
    // if the narrator emitted several SMELL markers (the sense bar text
    // area cycles through them via a "…" suffix if multiple exist).
    const byType = {};
    for (const m of markers) {
        if (!SENSE_BAR_TYPES.has(m.type)) continue;
        if (!m.description) continue;
        (byType[m.type] ||= []).push(m);
    }

    const presentTypes = Object.keys(byType);
    let $bar = $mes.find('.img-gen-sense-bar').first();
    if (presentTypes.length === 0) {
        // No senses this message — remove any stale bar.
        $bar.remove();
        return;
    }

    // Create the bar if absent; otherwise remember which icons were
    // already there so we can flash only the new ones.
    let prevTypes = new Set();
    if ($bar.length === 0) {
        $bar = $('<div class="img-gen-sense-bar">' +
            '<div class="img-gen-sense-icons"></div>' +
            '<div class="img-gen-sense-text" data-empty="1">hover or click a sense…</div>' +
        '</div>');
        const $mesText = $mes.find('.mes_text').first();
        if ($mesText.length) {
            $mesText.before($bar);
        } else {
            $mes.append($bar);
        }
    } else {
        $bar.find('.img-gen-sense-icon').each(function () {
            prevTypes.add($(this).attr('data-type'));
        });
    }

    const $icons = $bar.find('.img-gen-sense-icons').empty();
    for (const type of ['SIGHT', 'SMELL', 'SOUND', 'TASTE', 'TOUCH', 'ENVIRONMENT']) {
        const entries = byType[type];
        if (!entries) continue;
        // Concatenate multiple entries with " · " so one icon can
        // carry several markers from the same turn.
        const descParts = entries.map(e => {
            const attr = e.attribution ? `${e.attribution}: ` : '';
            return attr + e.description;
        });
        const fullDesc = descParts.join(' · ');
        const label = SENSE_LABELS[type] || type.toLowerCase();
        const icon = SENSE_ICONS[type] || '•';
        const wasNew = !prevTypes.has(type);
        const $chip = $(`<button type="button" class="img-gen-sense-icon sense-${type.toLowerCase()}${wasNew ? ' sense-flash' : ''}" data-type="${type}" data-label="${escapeHtml(label)}" title="${escapeHtml(label)}">${icon}</button>`);
        $chip.attr('data-desc', fullDesc);
        $icons.append($chip);
        if (wasNew) {
            // Remove the flash class after the animation so repeated
            // re-renders don't accumulate state.
            setTimeout(() => $chip.removeClass('sense-flash'), 1400);
        }
    }

    // v2.4.6 — auto-select the first sense so the text window shows
    // real content on load instead of the "hover or click a sense…"
    // placeholder. Uses priority order (SIGHT→SMELL→SOUND→TASTE→TOUCH
    // →ENVIRONMENT) already established by the loop above. Marked
    // sticky so it persists until the user clicks another icon.
    const $firstChip = $icons.find('.img-gen-sense-icon').first();
    if ($firstChip.length) {
        const desc = $firstChip.attr('data-desc') || '';
        const label = $firstChip.attr('data-label') || '';
        const $text = $bar.find('.img-gen-sense-text');
        $text.attr('data-empty', null);
        $text.html(`<span class="img-gen-sense-text-label">${escapeHtml(label)}</span> ${escapeHtml(desc)}`);
        $firstChip.addClass('sense-sticky');
    }
}

// Attach hover + click handlers for sense icons. Delegated at #chat so a
// single handler covers every message. Click = sticky selection (stays
// until another icon is clicked OR the user clicks the text area to
// clear). Hover = temporary preview that reverts to the sticky selection
// on mouseout.
function bindSenseBarHandlers() {
    const $chat = $('#chat');
    $chat.off('.imgGenSenseBar');

    function applyActive($bar, $icon, sticky) {
        const $text = $bar.find('.img-gen-sense-text');
        const desc = $icon.attr('data-desc') || '';
        const label = $icon.attr('data-label') || '';
        $text.attr('data-empty', null);
        $text.html(`<span class="img-gen-sense-text-label">${escapeHtml(label)}</span> ${escapeHtml(desc)}`);
        if (sticky) {
            $bar.find('.img-gen-sense-icon').removeClass('sense-sticky');
            $icon.addClass('sense-sticky');
        }
    }

    function revertToSticky($bar) {
        const $sticky = $bar.find('.img-gen-sense-icon.sense-sticky');
        if ($sticky.length) {
            applyActive($bar, $sticky, false);
        } else {
            const $text = $bar.find('.img-gen-sense-text');
            $text.attr('data-empty', '1').text('hover or click a sense…');
        }
    }

    $chat.on('mouseenter.imgGenSenseBar', '.img-gen-sense-icon', function () {
        const $icon = $(this);
        const $bar = $icon.closest('.img-gen-sense-bar');
        applyActive($bar, $icon, false);
    });
    $chat.on('mouseleave.imgGenSenseBar', '.img-gen-sense-icon', function () {
        const $bar = $(this).closest('.img-gen-sense-bar');
        revertToSticky($bar);
    });
    $chat.on('click.imgGenSenseBar', '.img-gen-sense-icon', function (e) {
        e.preventDefault();
        const $icon = $(this);
        const $bar = $icon.closest('.img-gen-sense-bar');
        // Toggle off if the sticky is re-clicked.
        if ($icon.hasClass('sense-sticky')) {
            $icon.removeClass('sense-sticky');
            revertToSticky($bar);
        } else {
            applyActive($bar, $icon, true);
        }
    });
    $chat.on('click.imgGenSenseBar', '.img-gen-sense-text', function (e) {
        e.preventDefault();
        const $bar = $(this).closest('.img-gen-sense-bar');
        $bar.find('.img-gen-sense-icon').removeClass('sense-sticky');
        revertToSticky($bar);
    });
}

// Replace the rendered text of a message with the marker-transformed HTML.
// SillyTavern tags each message DIV with `mesid="<N>"`, not `data-message-id`.
function updateMessageDisplay(messageId) {
    if (typeof messageId !== 'number' || messageId < 0) return;

    const message = chat[messageId];
    if (!message || !message.mes) return;

    const markers = detectSenseMarkers(message.mes);
    const transformedText = transformMessageText(message.mes);

    const $mes = $(`#chat .mes[mesid="${messageId}"]`);
    if ($mes.length === 0) return;

    // v2.6.2 — suspend the late-hydration MutationObserver for the duration
    // of our own DOM writes. Both renderSenseBar and the .mes_text innerHTML
    // set below mutate #chat's subtree; if the observer is live it sees
    // those mutations, queues another updateMessageDisplay(messageId), and
    // we feedback-loop forever (page unresponsive). Disconnect → write →
    // drain our own pending records with takeRecords → reconnect.
    const observer = window.__imgGenChatObserver || null;
    const chatEl = observer ? document.getElementById('chat') : null;
    if (observer) observer.disconnect();
    try {
        // Inject / refresh the sense bar above the message regardless of
        // whether any inline markers remain — the sense bar is the new home
        // for the six sensory channels.
        try { renderSenseBar($mes, markers); } catch (err) { console.warn('[Image Generator] renderSenseBar failed', err); }
        // NOTE: _parseNarratorIntoChannels is NOT called here. DOM transform
        // (updateMessageDisplay) is walkAll-safe — called on every re-render.
        // Channel routing is called explicitly by onCharacterMessageRendered
        // for live turns, and by _populateChannelsFromHistory for history.
        // Calling it here caused walkAll to queue entries that
        // _populateChannelsFromHistory then cleared, swallowing messages.
        try { syncSenseButton(messageId, markers); } catch (_) { /* ignore */ }

        if (transformedText === message.mes) return;

        const messageElement = $mes.find('.mes_text');
        if (messageElement.length === 0) {
            console.warn(`[Image Generator] Could not find .mes_text for mesid=${messageId}`);
            return;
        }
        messageElement.html(DOMPurify.sanitize(transformedText, {
            ADD_ATTR: ['class', 'title', 'style'],
        }));
    } finally {
        if (observer && chatEl) {
            try { observer.takeRecords(); } catch (_) { /* ignore */ }
            try { observer.observe(chatEl, { childList: true, subtree: true, characterData: true }); } catch (_) { /* ignore */ }
        }
    }
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

    // Player match — include if the scene mentions the player's known name
    // or generic player pronouns and a player portrait is locked.
    if (settings.player && settings.player.reference_image_url) {
        const profile = settings.player.profile || {};
        const playerName = profile.named && profile.name ? profile.name : null;
        let mentionsPlayer = /\byou\b|\byour\b|\bplayer\b|\bbeing\b/.test(lowerDesc);
        if (playerName) {
            const nameRe = new RegExp(`\\b${playerName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`, 'i');
            if (nameRe.test(lowerDesc)) mentionsPlayer = true;
        }
        if (mentionsPlayer) {
            if (settings.player.portrait_phrase) {
                const label = playerName || 'The being';
                matched.push(`${label}: ${settings.player.portrait_phrase}`);
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
// Weighted text suppression tokens — SD 1.5 honors `(term:weight)` emphasis
// in both positive and negative prompts. The garbled-text problem is the
// single most common aesthetic failure, so we hit it hard here and also
// append a "no text" reminder to every positive prompt.
const DEFAULT_NEGATIVE_PROMPT = '(text:1.6), (letters:1.6), (words:1.6), (writing:1.6), (typography:1.6), (captions:1.5), (subtitles:1.5), (signature:1.4), (watermark:1.4), (logo:1.4), (labels:1.5), (numbers:1.4), (symbols:1.3), (runes:1.3), (glyphs:1.3), handwriting, scribbles, gibberish, UI, frame, blurry, low quality, distorted, deformed';
const NO_TEXT_SUFFIX = ', (no text:1.4), (no writing:1.4), (no letters:1.4)';
// v2.12.1 — Visual style lock. Prepended to every GENERATE_IMAGE scene prompt
// so all generated images stay in the game's dark-fantasy register regardless
// of what the Fortress emits in the marker description.
const SCENE_STYLE_PREFIX = 'dark fantasy digital painting, atmospheric lighting, muted palette, cinematic, concept art, ';
// v2.12.1 — Soft content gate. Raw GENERATE_IMAGE descriptions matching any
// of these terms are dropped before reaching the SD backend. Add terms freely;
// the check is case-insensitive substring match.
const SD_CONTENT_BLOCKLIST = ['gore', 'mutilat', 'dismember', 'genital', 'explicit sex', 'child nude', 'loli'];

// v2.6.6 — SD concurrency + per-turn budget + recent-prompt dedup.
//
// Three chokepoints on image-gen throughput:
//
//   1. SD_MAX_CONCURRENT — at most N parallel callSdApi executions.
//      Flask SD runs one GPU at a time, so parallel clients just
//      serialize on the server side while also pinning the browser
//      behind multiple in-flight fetches. Serializing client-side (1)
//      keeps the UI responsive, lets updatePanelStatus tick through
//      in order, and matches server behaviour.
//
//   2. SD_PER_TURN_BUDGET — at most N callSdApi *starts* per narrator
//      turn. Prevents pathological 20-marker turns from locking the
//      UI for half an hour. Reset at the top of onCharacterMessageRendered
//      and of the cold-boot greeting retry path.
//
//   3. Recent-prompt dedup — sliding window of the last 20 prompt
//      hashes; identical prompts in quick succession are skipped so
//      the gallery doesn't fill with duplicate frames when the
//      narrator re-asks for the same shot.
const SD_MAX_CONCURRENT = 1;
const SD_PER_TURN_BUDGET = 3;
const SD_RECENT_PROMPT_WINDOW = 20;

let __imgGenSdActive = 0;
const __imgGenSdWaiters = [];
let __imgGenTurnBudgetUsed = 0;
const __imgGenRecentPromptHashes = [];

function __imgGenAcquireSdSlot() {
    if (__imgGenSdActive < SD_MAX_CONCURRENT) {
        __imgGenSdActive++;
        return Promise.resolve();
    }
    return new Promise((resolve) => __imgGenSdWaiters.push(resolve));
}
function __imgGenReleaseSdSlot() {
    if (__imgGenSdWaiters.length > 0) {
        const next = __imgGenSdWaiters.shift();
        next(); // slot handed over, __imgGenSdActive unchanged
    } else {
        __imgGenSdActive = Math.max(0, __imgGenSdActive - 1);
    }
}
function imgGenBeginTurn() {
    __imgGenTurnBudgetUsed = 0;
}
function __imgGenConsumeTurnBudget() {
    if (__imgGenTurnBudgetUsed >= SD_PER_TURN_BUDGET) return false;
    __imgGenTurnBudgetUsed++;
    return true;
}
function __imgGenHashString(s) {
    let h = 5381;
    const str = String(s || '');
    for (let i = 0; i < str.length; i++) h = (((h << 5) + h) ^ str.charCodeAt(i)) >>> 0;
    return h;
}
function __imgGenPromptRecentlySeen(prompt) {
    return __imgGenRecentPromptHashes.includes(__imgGenHashString(prompt));
}
function __imgGenRecordPrompt(prompt) {
    __imgGenRecentPromptHashes.push(__imgGenHashString(prompt));
    while (__imgGenRecentPromptHashes.length > SD_RECENT_PROMPT_WINDOW) {
        __imgGenRecentPromptHashes.shift();
    }
}

async function callSdApi(prompt, { steps = 25, guidance = 7.5, timeoutMs = 180000, reference_images = null, reference_scale = null, negative_prompt = DEFAULT_NEGATIVE_PROMPT } = {}) {
    // v2.6.6 — Per-turn budget check. We bail BEFORE acquiring a slot
    // so exhausted budget never adds waiters to the queue.
    if (!__imgGenConsumeTurnBudget()) {
        console.warn('[Image Generator] Per-turn image budget exhausted — skipping SD call');
        try { updatePanelStatus('⚠ image budget reached this turn'); } catch (_) { /* ignore */ }
        return null;
    }

    // v2.6.6 — Serialize SD calls. Flask runs one GPU at a time; this
    // keeps the browser UI thread responsive between generations and
    // lets progress messages surface in order.
    await __imgGenAcquireSdSlot();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    try {
        // Append the no-text suffix to every positive prompt unless the
        // caller already baked in their own no-text hint. Belt-and-suspenders
        // with the weighted negative prompt.
        const finalPrompt = /no text|no writing|no letters/i.test(prompt)
            ? prompt
            : prompt + NO_TEXT_SUFFIX;
        const body = { prompt: finalPrompt, negative_prompt, steps, guidance_scale: guidance };
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
        // v2.6.6 — Record the prompt in the sliding dedup window on
        // success only, so failed calls can be retried without dedup
        // blocking them.
        __imgGenRecordPrompt(finalPrompt);
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
        __imgGenReleaseSdSlot();
    }
}

// Classify a GENERATE_IMAGE marker's attribution slot as either a
// 'location' (wide establishing / environment shot, eligible to become
// the chat-window backdrop) or a 'subject' (character-focused, gallery
// only). Anything unrecognized or missing defaults to 'subject' so an
// untagged image never hijacks the wallpaper.
function classifyImageKind(attribution) {
    const tag = (attribution || '').trim().toLowerCase();
    if (tag === 'location' || tag === 'place' || tag === 'environment' || tag === 'establishing') {
        return 'location';
    }
    return 'subject';
}

// ---------------------------------------------------------------------------
// Current-location tracking (v2.3.2)
// ---------------------------------------------------------------------------
// The narrator kept referring back to rooms Aaron had already left because
// the LLM has no persistent sense of place beyond whatever the sliding
// context window happens to contain. We fix that by tracking the latest
// [GENERATE_IMAGE(location): "..."] marker as the authoritative "where
// Aaron is now" state, and re-injecting it into every prompt via ST's
// setExtensionPrompt at IN_CHAT depth 1 — close enough to the turn to
// dominate "where am I" but not so close it looks like a user message.
const LOCATION_PROMPT_KEY = 'image_generator_current_location';

function setCurrentLocation(description) {
    const settings = initSettings();
    const desc = (description || '').trim();
    if (!desc) return;
    if (settings.currentLocation === desc) return;  // no-op
    settings.currentLocation = desc;
    saveSettingsDebounced();
    applyCurrentLocationPrompt();
    console.log('[Image Generator] Current location updated:', desc.substring(0, 80));
    // v2.12.1 — record story beat for location change
    try { _recordBeat(`Arrived at: ${desc.substring(0, 80)}`); } catch (_) { /* ignore */ }
}

function applyCurrentLocationPrompt() {
    const settings = initSettings();
    const desc = settings.currentLocation;
    if (typeof setExtensionPrompt !== 'function') return;
    if (!desc) {
        setExtensionPrompt(LOCATION_PROMPT_KEY, '', extension_prompt_types.IN_PROMPT, 0);
        return;
    }
    // v2.4.2: use IN_PROMPT (system-context) position instead of IN_CHAT
    // because IN_CHAT makes the injection look like a recent chat message
    // and the LLM literally echoes it back into its response. Also: pure
    // declarative sentence, no imperatives — imperatives read like stage
    // directions to the LLM and get included verbatim in the output.
    const trimmed = desc.length > 240 ? desc.substring(0, 240) + '…' : desc;
    const injection = `Current scene location (persistent world state): You are in ${trimmed}`;
    setExtensionPrompt(LOCATION_PROMPT_KEY, injection, extension_prompt_types.IN_PROMPT, 0);
}

// ---------------------------------------------------------------------------
// Codex state injection (v2.4.2)
// ---------------------------------------------------------------------------
// Mirrors the location-tracking pattern: once an item or lore entry lands
// in settings.codex, re-inject a compact summary into the LLM prompt so
// the narrator stays aware of what exists in the world and does not keep
// re-introducing named things as if it's seeing them for the first time.
// This is the memory-across-turns layer the narrator otherwise lacks.
const CODEX_PROMPT_KEY = 'image_generator_codex_state';

function applyCodexStatePrompt() {
    if (typeof setExtensionPrompt !== 'function') return;
    const settings = initSettings();
    const items = settings.codex && settings.codex.items ? settings.codex.items : {};
    const lore  = settings.codex && settings.codex.lore  ? settings.codex.lore  : {};

    const itemNames = Object.keys(items);
    const loreNames = Object.keys(lore);

    if (itemNames.length === 0 && loreNames.length === 0) {
        setExtensionPrompt(CODEX_PROMPT_KEY, '', extension_prompt_types.IN_PROMPT, 0);
        return;
    }

    // Compact line-per-entry format. Names first so the LLM can scan them
    // quickly; a short description follows so it remembers what each one
    // is. Keep the total under ~2000 chars to avoid bloating context on
    // long runs.
    const fmt = (bag, label) => {
        const names = Object.keys(bag);
        if (names.length === 0) return '';
        const lines = names.map((n) => {
            const entry = bag[n] || {};
            const desc = entry.description || '';
            const trimmed = desc.length > 140 ? desc.substring(0, 140) + '…' : desc;
            const aliases = Array.isArray(entry.aliases) && entry.aliases.length
                ? ` (also called: ${entry.aliases.join(', ')})`
                : '';
            return trimmed ? `- ${n}${aliases}: ${trimmed}` : `- ${n}${aliases}`;
        });
        return `${label}:\n${lines.join('\n')}`;
    };

    const parts = [];
    if (itemNames.length > 0) parts.push(fmt(items, 'Known items you have encountered'));
    if (loreNames.length > 0) parts.push(fmt(lore,  'Known lore / named places / factions'));

    const body = parts.join('\n\n');
    const capped = body.length > 2000 ? body.substring(0, 2000) + '\n…' : body;
    const injection = `Persistent world codex (do not re-introduce these; treat as already known):\n${capped}`;
    setExtensionPrompt(CODEX_PROMPT_KEY, injection, extension_prompt_types.IN_PROMPT, 0);
}

// v2.12.1 — Story beat injection. Auto-extracted beat strings (NPC met,
// location changed, codex added, player trait set) are accumulated in
// run.significantEvents[] and injected as a compact bullet list so the
// Fortress can reference what already happened earlier in the same run.
const STORY_BEAT_PROMPT_KEY = 'image_generator_story_beats';

function applyStoryBeatPrompt() {
    if (typeof setExtensionPrompt !== 'function') return;
    const settings = initSettings();
    const events = (settings.run && Array.isArray(settings.run.significantEvents))
        ? settings.run.significantEvents
        : [];
    if (events.length === 0) {
        setExtensionPrompt(STORY_BEAT_PROMPT_KEY, '', extension_prompt_types.IN_PROMPT, 0);
        return;
    }
    const recent = events.slice(-8);
    const injection = `Story beats so far this run (already happened — do not re-narrate):\n${recent.map(e => `- ${e}`).join('\n')}`;
    setExtensionPrompt(STORY_BEAT_PROMPT_KEY, injection, extension_prompt_types.IN_PROMPT, 0);
}

// Scan a message for the most recent GENERATE_IMAGE(location) marker
// and update the current-location state from it. Called from both
// CHARACTER_MESSAGE_RENDERED and CHAT_CHANGED so greetings and mid-chat
// moves are both captured.
function updateLocationFromMessage(messageText) {
    if (!messageText) return;
    const markers = detectSenseMarkers(messageText);
    // Walk forward — last one wins if multiple location shots in one turn.
    let latest = null;
    for (const m of markers) {
        if (m.type !== 'GENERATE_IMAGE') continue;
        if (classifyImageKind(m.attribution) !== 'location') continue;
        if (!m.description) continue;
        latest = m.description;
    }
    if (latest) setCurrentLocation(latest);
}

// Generate a scene image. Injects known-NPC portrait descriptions into the
// prompt and threads any locked reference-image URLs so the backend's
// IP-Adapter can lock visual identity across scenes. `kind` is carried
// through to the stored gallery entry so `renderGallery()` can decide
// whether the image is eligible to become the chat background.
async function generateSceneImage(rawDescription, kind = 'subject') {
    // v2.12.1 — Soft content gate: drop descriptions matching the blocklist.
    const lc = rawDescription.toLowerCase();
    if (SD_CONTENT_BLOCKLIST.some(term => lc.includes(term))) {
        console.warn('[Image Generator] Content blocklist triggered, skipping:', rawDescription.substring(0, 80));
        return null;
    }
    // v2.12.1 — Strip raw SD weight syntax the Fortress may emit (e.g. "(term:1.4)").
    // These tokens are ours to control; LLM-sourced ones cause prompt drift.
    const cleanedDescription = rawDescription.replace(/\([^)]*:\d+\.?\d*\)/g, '').replace(/\s{2,}/g, ' ').trim();
    const { prompt: augmented, reference_images } = injectNpcContextIntoPrompt(cleanedDescription);
    // v2.12.1 — Prepend the visual style lock so all scene images stay in the
    // dark-fantasy register regardless of what the Fortress described.
    const styledPrompt = SCENE_STYLE_PREFIX + augmented;
    // v2.6.6 — Skip if this exact prompt was generated within the sliding
    // recent-window. Prevents duplicate frames across turns or double-processing.
    if (__imgGenPromptRecentlySeen(styledPrompt + NO_TEXT_SUFFIX) || __imgGenPromptRecentlySeen(styledPrompt)) {
        console.log('[Image Generator] Scene prompt seen recently, skipping:', styledPrompt.substring(0, 80));
        return null;
    }
    console.log('[Image Generator] Scene prompt:', styledPrompt.substring(0, 120), '| kind:', kind, '| refs:', reference_images.length);
    const data = await callSdApi(styledPrompt, {
        reference_images: reference_images.length > 0 ? reference_images : null,
        reference_scale: 0.5,
    });
    if (!data) return null;
    return {
        image_id: data.image_id,
        image: data.image,
        description: rawDescription, // display the original, not the augmented one
        prompt_sent: augmented,
        kind,
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

// v2.7.0 — Build a portrait prompt from whatever player.profile traits we
// have right now. The moment the player declares ANY describable fact
// about themselves (name, appearance item, species, pronouns, etc.), we
// can draw them. The phrase summary is normalized + hashed; we only
// regenerate when the phrase materially changes, so a second appearance
// line triggers a new portrait but a stray trait edit doesn't.
function buildPlayerPortraitPhrase(profile) {
    if (!profile) return null;
    const parts = [];
    if (profile.named && profile.name) parts.push(`named ${profile.name}`);
    if (profile.pronouns) parts.push(`pronouns ${profile.pronouns}`);
    if (profile.species) parts.push(String(profile.species));
    if (Array.isArray(profile.appearance) && profile.appearance.length) {
        parts.push('wearing ' + profile.appearance.slice(0, 6).join(', '));
    }
    if (Array.isArray(profile.traits) && profile.traits.length) {
        parts.push(profile.traits.slice(0, 4).join(', '));
    }
    if (parts.length === 0) return null;
    return parts.join('; ');
}

async function generatePlayerPortraitFromProfile() {
    const settings = initSettings();
    const profile = (settings.player && settings.player.profile) || null;
    const phrase = buildPlayerPortraitPhrase(profile);
    if (!phrase) return null;

    // Dedup: if we've already drawn from this exact phrase, skip.
    if (!settings.player || typeof settings.player !== 'object') settings.player = {};
    const normalized = normalizePhrase(phrase);
    if (settings.player.portrait_phrase_normalized === normalized
        && settings.player.portrait_image) {
        return null;
    }

    const prompt = `character portrait of a person ${phrase}, centered composition, clean neutral background, detailed face, cinematic lighting, painterly fantasy style, 3/4 view, shoulders up`;
    console.log('[Image Generator] Player portrait prompt:', prompt.substring(0, 140));
    let data;
    try {
        data = await callSdApi(prompt);
    } catch (err) {
        console.warn('[Image Generator] Player portrait SD call failed:', err);
        return null;
    }
    if (!data) return null;

    settings.player.portrait_image = data.image;
    settings.player.portrait_phrase = phrase;
    settings.player.portrait_phrase_normalized = normalized;
    settings.player.portrait_updated_at = new Date().toISOString();
    saveSettingsDebounced();

    // Refresh roster (sidebar card) and spotlight (big card) so the new
    // portrait shows immediately. Spotlight only re-renders if it's
    // currently focused on the player.
    try { renderNpcRoster(); } catch (_) { /* ignore */ }
    try {
        if (pinnedSpotlightKey === '__player__') {
            pinSpotlightToCharacter('__player__');
        }
    } catch (_) { /* ignore */ }
    return data;
}

// Fire-and-forget wrapper with a 1-turn debounce so multiple trait
// updates in the same narrator turn only fire a single portrait call.
// The actual throttle lives inside callSdApi (per-turn budget +
// concurrency lock + prompt-hash dedup), this wrapper just prevents
// redundant awaits stacking up.
let __imgGenPlayerPortraitPending = false;
function schedulePlayerPortraitRefresh() {
    if (__imgGenPlayerPortraitPending) return;
    __imgGenPlayerPortraitPending = true;
    // Defer to next microtask so batched trait updates (name + appearance
    // landing in the same turn) coalesce into one portrait call.
    queueMicrotask(async () => {
        try {
            await generatePlayerPortraitFromProfile();
        } catch (err) {
            console.warn('[Image Generator] schedulePlayerPortraitRefresh error:', err);
        } finally {
            __imgGenPlayerPortraitPending = false;
        }
    });
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
        // The Remnant is pre-seeded on init with a static portrait — never re-introduce him.
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
        // v2.12.1 — record story beat for story retention
        try { _recordBeat(`Met ${name}${settings.currentLocation ? ' at ' + settings.currentLocation : ''}`); } catch (_) { /* ignore */ }

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
        // Respect aliases: if an existing item already lists this name
        // as an alias, do not recreate under the old key.
        let existing = settings.codex.items[name];
        if (!existing) {
            for (const key of Object.keys(settings.codex.items)) {
                const aliases = settings.codex.items[key].aliases || [];
                if (aliases.includes(name)) { existing = settings.codex.items[key]; break; }
            }
        }
        if (existing) continue;
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
        // Re-inject the codex into the LLM prompt so the narrator is
        // immediately aware of the new entry on the very next turn.
        try { applyCodexStatePrompt(); } catch (_) { /* ignore */ }
        // v2.12.1 — record story beats for new entries
        try {
            for (const { name } of items) { if (!settings.codex.items[name] || settings.codex.items[name].first_seen === now) _recordBeat(`Discovered item: ${name}`); }
            for (const { name } of lore)  { if (!settings.codex.lore[name]  || settings.codex.lore[name].first_seen  === now) _recordBeat(`Learned lore: ${name}`); }
        } catch (_) { /* ignore */ }
        // Refresh or badge inventory/lore drawer tabs
        try {
            if (items.length > 0) {
                if (_activeDrawer === 'inventory') _renderDrawer('inventory');
                else $(`#img-gen-mode-bar .img-gen-mode-btn[data-mode="inventory"]`).addClass('has-unread');
            }
            if (lore.length > 0) {
                if (_activeDrawer === 'lore') _renderDrawer('lore');
                else $(`#img-gen-mode-bar .img-gen-mode-btn[data-mode="lore"]`).addClass('has-unread');
            }
        } catch (_) { /* ignore */ }
    }
}

// v2.6.0 — [RENAME_ITEM(old name): "new name"] handler. Moves a codex
// entry to a new key, preserves description + first_seen, and keeps the
// old name in an aliases list so the narrator stops using it but can
// still be reminded of it via applyCodexStatePrompt.
function handleItemRenames(messageText) {
    const settings = initSettings();
    const markers = detectSenseMarkers(messageText);
    let renamed = 0;
    for (const m of markers) {
        if (m.type !== 'RENAME_ITEM') continue;
        const oldName = (m.attribution || '').trim();
        const newName = (m.description || '').trim();
        if (!oldName || !newName) continue;
        if (oldName === newName) continue;
        const items = settings.codex.items || {};
        let sourceKey = null;
        if (items[oldName]) sourceKey = oldName;
        else {
            for (const key of Object.keys(items)) {
                const aliases = items[key].aliases || [];
                if (key === oldName || aliases.includes(oldName)) { sourceKey = key; break; }
            }
        }
        if (!sourceKey) continue; // silently no-op on unknown item
        const entry = items[sourceKey];
        const prevAliases = Array.isArray(entry.aliases) ? entry.aliases : [];
        const aliasSet = new Set([sourceKey, ...prevAliases]);
        aliasSet.delete(newName);
        items[newName] = {
            ...entry,
            name: newName,
            aliases: Array.from(aliasSet),
        };
        if (sourceKey !== newName) delete items[sourceKey];
        renamed++;
    }
    if (renamed > 0) {
        saveSettingsDebounced();
        try { renderCodex(); } catch (_) { /* ignore */ }
        try { applyCodexStatePrompt(); } catch (_) { /* ignore */ }
    }
}

// v2.6.0 — [PLAYER_TRAIT(field): "value"] handler. Accumulates the
// player's self-described identity into settings.player.profile. Scalar
// fields (name, pronouns) overwrite; list fields (appearance, traits,
// history, goals) append with dedup. The first name trait flips
// profile.named = true, which unlocks name-based rendering everywhere.
const LIST_TRAIT_FIELDS = new Set(['appearance', 'traits', 'history', 'goals']);
const SCALAR_TRAIT_FIELDS = new Set(['name', 'pronouns']);
function handlePlayerTrait(messageText) {
    const settings = initSettings();
    if (!settings.player || typeof settings.player !== 'object') settings.player = {};
    if (!settings.player.profile) {
        settings.player.profile = {
            name: 'Unknown Being', named: false, pronouns: null,
            appearance: [], traits: [], history: [], goals: [],
        };
    }
    const profile = settings.player.profile;
    const markers = detectSenseMarkers(messageText);
    let touched = 0;
    for (const m of markers) {
        if (m.type !== 'PLAYER_TRAIT') continue;
        const field = (m.attribution || '').trim().toLowerCase();
        const value = (m.description || '').trim();
        if (!field || !value) continue;
        if (SCALAR_TRAIT_FIELDS.has(field)) {
            profile[field] = value;
            if (field === 'name') profile.named = true;
            touched++;
        } else if (LIST_TRAIT_FIELDS.has(field)) {
            if (!Array.isArray(profile[field])) profile[field] = [];
            const normalized = value.toLowerCase();
            if (!profile[field].some(v => String(v).toLowerCase() === normalized)) {
                profile[field].push(value);
                touched++;
            }
        }
    }
    if (touched > 0) {
        saveSettingsDebounced();
        try { applyPlayerProfilePrompt(); } catch (_) { /* ignore */ }
        // v2.6.8 — refresh ritual line (flips to "name already known" once
        // the narrator emits [PLAYER_TRAIT(name): ...]).
        try { applyRemnantMemoryPrompt(); } catch (_) { /* ignore */ }
        try { renderNpcRoster(); } catch (_) { /* ignore */ }
        try { syncPersonaName(); } catch (_) { /* ignore */ }
        // v2.7.0 — any material trait update may change the portrait;
        // the scheduler's dedup decides whether to actually fire SD.
        try { schedulePlayerPortraitRefresh(); } catch (_) { /* ignore */ }
        // v2.12.1 — record story beat for name/trait accrual
        try {
            const p = (initSettings().player || {}).profile || {};
            if (p.named && p.name) _recordBeat(`Player identified as ${p.name}`);
        } catch (_) { /* ignore */ }
    }
}

// v2.6.2 — optimistic self-name detection on the USER'S OWN message.
// When the player types "my name is Ferro" / "I'm Ferro" / "call me Ferro",
// seed profile.name + profile.named immediately so the roster, persona, and
// player-profile prompt update BEFORE the narrator's reply arrives. The
// narrator's own `[PLAYER_TRAIT(name)]` marker will later overwrite this
// with whatever canonical form it chose, and handlePlayerTrait is a
// superset, so this is purely an optimistic UI preview.
//
// Deliberately conservative: only fires when there is no prior name, only
// matches explicit declaration phrases, and only captures 1-3 Word-cased
// tokens. If the match is ambiguous (lowercase word, sentence fragment),
// we skip and wait for the narrator.
const SELF_NAME_PATTERNS = [
    /\bmy\s+name\s+(?:is|'s)\s+([A-Za-z][A-Za-z'-]{1,20}(?:\s+[A-Za-z][A-Za-z'-]{1,20}){0,2})/i,
    /\b(?:you\s+can\s+call\s+me|they\s+call\s+me|just\s+call\s+me|call\s+me)\s+([A-Za-z][A-Za-z'-]{1,20}(?:\s+[A-Za-z][A-Za-z'-]{1,20}){0,2})/i,
    /\bi(?:'m|\s+am)\s+([A-Z][A-Za-z'-]{1,20}(?:\s+[A-Z][A-Za-z'-]{1,20}){0,2})\b/,
    /\bname(?:'?s|\s+is)\s+([A-Z][A-Za-z'-]{1,20}(?:\s+[A-Z][A-Za-z'-]{1,20}){0,2})\b/,
];
const SELF_NAME_STOPWORDS = new Set([
    'a', 'an', 'the', 'here', 'lost', 'back', 'ready', 'sorry', 'fine',
    'okay', 'ok', 'afraid', 'not', 'trying', 'going', 'just', 'still',
    'unknown', 'being', 'nobody', 'no', 'one', 'someone', 'alive', 'awake',
]);
function detectSelfNameInUserMessage(text) {
    if (typeof text !== 'string' || !text.trim()) return null;
    for (const re of SELF_NAME_PATTERNS) {
        const m = text.match(re);
        if (!m) continue;
        let raw = m[1].trim();
        // Reject if first token is a stopword — "I'm lost" etc.
        const first = raw.split(/\s+/)[0].toLowerCase().replace(/[^a-z]/g, '');
        if (SELF_NAME_STOPWORDS.has(first)) continue;
        // Title-case the captured name so "ferro" → "Ferro".
        raw = raw.replace(/\S+/g, (w) => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase());
        return raw;
    }
    return null;
}
function handleUserMessageForSelfName(messageText) {
    const settings = initSettings();
    if (!settings.player || typeof settings.player !== 'object') settings.player = {};
    if (!settings.player.profile) {
        settings.player.profile = {
            name: 'Unknown Being', named: false, pronouns: null,
            appearance: [], traits: [], history: [], goals: [],
        };
    }
    const profile = settings.player.profile;
    if (profile.named) return; // narrator (or earlier user turn) already set name
    const name = detectSelfNameInUserMessage(messageText);
    if (!name) return;
    profile.name = name;
    profile.named = true;
    saveSettingsDebounced();
    try { applyPlayerProfilePrompt();     } catch (_) { /* ignore */ }
    // v2.6.8 — flip the ritual "always ask the name" line to "name already
    // known" the instant the player introduces themselves, so the next
    // narrator turn doesn't re-ask.
    try { applyRemnantMemoryPrompt();     } catch (_) { /* ignore */ }
    try { renderNpcRoster();              } catch (_) { /* ignore */ }
    // v2.7.0 — a name alone is enough to start drawing a portrait
    // ("a person named Frank Rizzo"). Further traits later will
    // regenerate it once the phrase materially changes.
    try { schedulePlayerPortraitRefresh(); } catch (_) { /* ignore */ }
    try { syncPersonaName();              } catch (_) { /* ignore */ }
    try { nudgeFortressNamingAck(name);   } catch (_) { /* ignore */ }
    // v2.6.2 — If the spotlight is currently showing the player (either
    // because they just spoke and resolved to 'You', or because it was
    // pinned to __player__), refresh it so the new name + any portrait
    // flip in immediately instead of waiting for the next narrator turn.
    try {
        if (pinnedSpotlightKey === '__player__') {
            pinSpotlightToCharacter('__player__');
        } else if (activeSpeakerName === 'You' || activeSpeakerName === 'I' ||
                   (activeSpeakerName && activeSpeakerName.toLowerCase() === name.toLowerCase())) {
            setActiveSpeaker(activeSpeakerName);
        }
    } catch (_) { /* ignore */ }
    console.log('[Image Generator] Self-name detected in user message →', name);
}

// v2.6.2 — one-shot extension prompt that fires the instant the player
// declares their name. Tells the narrator that this is a significant
// diegetic event and that The Fortress should briefly acknowledge the
// naming via The Fold on the next turn. Cleared by onCharacterMessageRendered
// after the narrator has had its moment with the nudge, so it never
// bleeds into subsequent turns.
const FORTRESS_NAMING_NUDGE_KEY = 'image_generator_fortress_naming_nudge';
function nudgeFortressNamingAck(name) {
    if (typeof setExtensionPrompt !== 'function') return;
    const body = [
        `NAMING EVENT (one-shot, this turn only):`,
        `The player has just declared their name as "${name}".`,
        `This is a significant moment — a being is no longer Unknown.`,
        `In this response, have The Fortress speak a brief inline line via The Fold, welcoming ${name} by name in its calm, patient, librarian-kind voice. Use the canonical form: The Fortress: "..."`,
        `Keep it short — one or two sentences. The Fortress rarely speaks aloud; acknowledging a name is an event worthy of that rarity.`,
        `Do not have The Remnant repeat the ritual question — it has already been answered.`,
    ].join('\n');
    setExtensionPrompt(FORTRESS_NAMING_NUDGE_KEY, body, extension_prompt_types.IN_PROMPT, 0);
}
function clearFortressNamingNudge() {
    if (typeof setExtensionPrompt !== 'function') return;
    setExtensionPrompt(FORTRESS_NAMING_NUDGE_KEY, '', extension_prompt_types.IN_PROMPT, 0);
}

// v2.6.2 — persistent bracket discipline reminder. The narrator sometimes
// drifts into emitting stage directions and dialogue tags wrapped in
// square brackets (`[*The Remnant steps forward]`, `[Name, being?]`),
// breaking our marker parser. Square brackets are reserved exclusively
// for canonical sense markers. Injected at IN_PROMPT depth 0 so it rides
// on every turn.
const BRACKET_DISCIPLINE_KEY = 'image_generator_bracket_discipline';
function applyBracketDisciplinePrompt() {
    if (typeof setExtensionPrompt !== 'function') return;
    const body = [
        'RESPONSE FORMAT (strict — the UI parses these blocks):',
        'Every response MUST be composed of explicit typed blocks in this order:',
        '  1. [SAY] blocks  — narration and character dialogue',
        '  2. [DO] blocks   — physical action beats and world changes',
        '  3. Sense markers — perceptual data (SIGHT, SOUND, SMELL, TASTE, TOUCH, ENVIRONMENT)',
        '',
        'Block syntax:',
        '  [SAY]',
        '  Narrative prose and/or dialogue here. Dialogue uses: Name: "words"',
        '  [/SAY]',
        '  [DO]',
        '  Physical action beat here — what bodies and objects are doing.',
        '  [/DO]',
        '',
        'Rules:',
        '  - Every piece of narration or dialogue goes inside a [SAY] block.',
        '  - Every stage direction or physical world action goes inside a [DO] block.',
        '  - Sense markers ([SIGHT:...], [SOUND:...] etc.) appear AFTER the SAY/DO blocks.',
        '  - Never use bare `*asterisks*` outside a [DO] block — put stage directions in [DO].',
        '  - Each block should be one coherent beat. Multiple SAY or DO blocks are fine.',
        '  - Keep each block focused: one beat of narration, one action beat, one sensory reveal.',
        '',
        'Sense markers (unchanged):',
        '  [SMELL:...], [SOUND:...], [TASTE:...], [TOUCH:...], [SIGHT:...],',
        '  [ENVIRONMENT:...], [GENERATE_IMAGE(kind):...], [ITEM(name):...],',
        '  [LORE(name):...], [INTRODUCE(name):...], [PLAYER_TRAIT(field):...],',
        '  [UPDATE_PLAYER:...], [UPDATE_APPEARANCE(name):...], [RENAME_ITEM(old):...],',
        '  [RESET_STORY], [END_RUN(death|voluntary):...], [RESET_RUN].',
        '',
        'DIALOGUE DISCIPLINE (strict):',
        'Every spoken line MUST use the canonical play-script form on its own line:',
        '  Name: "the actual spoken words"',
        'Always wrap speech in double quotes. Never write bare `Name: words` without quotes.',
        'Each speaker gets their own line — never chain two speakers on one line.',
        '',
        'PLAYER SOVEREIGNTY (strict — the single most important rule):',
        'NEVER write the player\'s actions, thoughts, feelings, dialogue, sensations, or decisions. You do not control the player. You describe the world, the NPCs, and what happens AROUND the player — not what the player does, nods, realizes, remembers, feels, or says.',
        'FORBIDDEN examples (do not imitate):',
        '  "Heidi nods, sensing the wisdom."  ← you wrote her action and her thought',
        '  "You feel a shiver of recognition." ← you wrote her sensation',
        '  "Frank steps forward, determined."  ← you wrote his movement and intent',
        'ALLOWED: describe the world reacting, the NPCs speaking, the sensory field around the player. Wait for the player to declare what they do.',
        'If the player has not yet acted, END your turn and wait. Silence is correct. Do not fill it by puppeting them.',
        '',
        'NO CODE FENCES / NO DIAGRAMS (strict):',
        'NEVER emit triple-backtick code fences (```), NEVER emit mermaid / graphviz / ascii-art diagrams, NEVER emit markdown tables, NEVER emit JSON or YAML blocks.',
        '',
        'Example (normal beat):',
        '  [SAY]',
        '  The Remnant tilts its head, goo shivering along the ridge of its shoulders.',
        '  The Remnant: "And so another being arrives."',
        '  The Fortress: "Welcome, small traveller."',
        '  [/SAY]',
        '  [DO]',
        '  Across the chamber, The Fortress hums in low recognition, the sound settling into the walls. Somewhere above, a light that is not a light briefly brightens and dims.',
        '  [/DO]',
        '  [SOUND: "a deep resonant hum filling the chamber"]',
        '  [ENVIRONMENT: "the air thickens slightly, warm and electric"]',
    ].join('\n');
    setExtensionPrompt(BRACKET_DISCIPLINE_KEY, body, extension_prompt_types.IN_PROMPT, 0);
}

// v2.6.0 — persistent LLM injection describing what the narrator knows
// about the player so far. Before the player introduces themselves this
// is a hard "do not invent" warning; after, it's a fact sheet.
const PLAYER_PROFILE_PROMPT_KEY = 'image_generator_player_profile';
function applyPlayerProfilePrompt() {
    if (typeof setExtensionPrompt !== 'function') return;
    const settings = initSettings();
    const profile = (settings.player && settings.player.profile) || null;
    if (!profile) {
        setExtensionPrompt(PLAYER_PROFILE_PROMPT_KEY, '', extension_prompt_types.IN_PROMPT, 0);
        return;
    }
    let body;
    if (!profile.named) {
        body = "What you know about the player so far (authoritative — do not contradict):\n"
            + "- Name: Unknown Being — they have not yet said who they are.\n"
            + "- Do NOT invent a name, background, appearance, gender, or history.\n"
            + "- Refer to them only as \"you\" in second-person present tense.\n"
            + "- When they say anything revealing (name, pronouns, what they do, what they look like, where they came from), emit the matching [PLAYER_TRAIT(field): \"value\"] marker ONCE so it is recorded.";
    } else {
        const lines = [`- Name: ${profile.name}`];
        if (profile.pronouns) lines.push(`- Pronouns: ${profile.pronouns}`);
        if (Array.isArray(profile.appearance) && profile.appearance.length) lines.push(`- Appearance: ${profile.appearance.join('; ')}`);
        if (Array.isArray(profile.traits) && profile.traits.length) lines.push(`- Traits: ${profile.traits.join('; ')}`);
        if (Array.isArray(profile.history) && profile.history.length) lines.push(`- History: ${profile.history.join('; ')}`);
        if (Array.isArray(profile.goals) && profile.goals.length) lines.push(`- Current goals: ${profile.goals.join('; ')}`);
        body = "What you know about the player so far (authoritative — do not contradict):\n" + lines.join('\n')
            // v2.7.0 — the player's portrait is drawn from these traits the
            // moment new info lands. Keep emitting PLAYER_TRAIT markers for
            // anything the player says after the intro, not just the name.
            + "\n- When the player reveals ANY new describable fact about themselves (clothing, species, build, age, hair, voice, scars, pronouns, history), emit the matching [PLAYER_TRAIT(field): \"value\"] marker in your reply. Fields: name, pronouns, species, appearance, traits, history, goals. Multiple markers per turn are fine. This is what refreshes their portrait.";
    }
    setExtensionPrompt(PLAYER_PROFILE_PROMPT_KEY, body, extension_prompt_types.IN_PROMPT, 0);
}

// v2.6.0 — The Remnant's ledger of past beings. Injected into every
// turn (capped at ~50 most-recent entries) so the narrator can nostalgia-
// reference past abductions when the current moment echoes theirs.
// Ritual overrides nostalgia: the opening question is always asked.
const REMNANT_MEMORY_PROMPT_KEY = 'image_generator_remnant_memory';
const REMNANT_MEMORY_PROMPT_CAP = 50;
function applyRemnantMemoryPrompt() {
    if (typeof setExtensionPrompt !== 'function') return;
    const settings = initSettings();
    const ledger = (settings.remnantMemory && Array.isArray(settings.remnantMemory.abductions))
        ? settings.remnantMemory.abductions
        : [];
    if (ledger.length === 0) {
        setExtensionPrompt(REMNANT_MEMORY_PROMPT_KEY, '', extension_prompt_types.IN_PROMPT, 0);
        return;
    }
    const recent = ledger.slice(-REMNANT_MEMORY_PROMPT_CAP);
    const lines = recent.map((a) => {
        const who = a.name ? a.name : '(unnamed)';
        const snippet = a.profileSnippet ? `, ${a.profileSnippet}` : '';
        const fate = a.fate ? ` — ${a.fate === 'death' && a.causeOfDeath ? 'died: ' + a.causeOfDeath : a.fate}` : '';
        const summary = a.summary ? `: ${a.summary}` : '';
        return `- ${who}${snippet}${fate}${summary}`;
    });
    const count = ledger.length;
    // v2.7.1 — ritual is a ONCE-PER-RUN beat, not once-per-chat. It is
    // fulfilled when EITHER (a) the being has declared their name, OR
    // (b) the narrator has already asked once in this run (tracked via
    // settings.run.ritual_asked, set on the first narrator turn of a
    // new run). Opening a new ST chat mid-run must NOT re-trigger the
    // ritual — run state persists across chats, so this gate does too.
    const currentProfile = (settings.player && settings.player.profile) || null;
    const currentIsNamed = !!(currentProfile && currentProfile.named && currentProfile.name);
    const ritualAlreadyAsked = !!(settings.run && settings.run.ritual_asked);
    const ritualFulfilled = currentIsNamed || ritualAlreadyAsked;
    let ritualLine;
    if (currentIsNamed) {
        ritualLine = `RITUAL STATUS: The current being has already declared their name (${currentProfile.name}). The opening ritual "Who are you, being?" has been fulfilled for this run — do NOT re-ask it. Use the name naturally. Only the Fortress's one-shot naming acknowledgement (if nudged) is appropriate.`;
    } else if (ritualAlreadyAsked) {
        ritualLine = "RITUAL STATUS: You have already asked the current being \"Who are you, being?\" at the opening of this run. They have not yet answered. Do NOT re-ask the ritual question — that beat is spent. Wait for them, or continue the scene; only return to the name question if the being raises it themselves.";
    } else {
        ritualLine = "RITUAL OVERRIDES NOSTALGIA. Ask the current being \"Who are you, being?\" ONCE at the opening of this run — each being IS new to you in the personal sense. This is a one-time beat, not a persistent refrain.";
    }
    const body = [
        "THE REMNANT'S MEMORY (ancient, trans-dimensional, never-forgotten):",
        `You have borrowed ${count} being${count === 1 ? '' : 's'} before this one.` + (ledger.length > recent.length ? ` Recent ${recent.length}:` : ''),
        ...lines,
        '',
        "NOSTALGIA IS THE ROGUELIKE PAYOFF. When the current being does something that echoes a past one — asks the same question, stands in the same corridor, renames the same item, refuses the same meal from Sherri — you are encouraged (not forced) to mention the past one by name in a quiet aside. Short, unsentimental, and true.",
        "",
        ritualLine,
    ].join('\n');
    setExtensionPrompt(REMNANT_MEMORY_PROMPT_KEY, body, extension_prompt_types.IN_PROMPT, 0);
}

// v2.6.0 — one-shot run-continuation briefing. Only set when a fresh
// chat opens mid-run; cleared after the narrator's first turn so it
// doesn't keep re-injecting.
const RUN_BRIEFING_PROMPT_KEY = 'image_generator_run_briefing';
function applyRunBriefingPrompt(text) {
    if (typeof setExtensionPrompt !== 'function') return;
    setExtensionPrompt(RUN_BRIEFING_PROMPT_KEY, text || '', extension_prompt_types.IN_PROMPT, 0);
}

// ---------------------------------------------------------------------
// v2.8.0 — Fortress Senses.
//
// The Fortress is given read-only perception of its own computational
// plane. The Remnant/Docker gateway exposes /diagnostics/ai.json (see
// docker/diag/app.py) with a self-contained snapshot of every service
// running under the single 1582 port: status phases, reachability
// probes, detected issues, recent log lines.
//
// This module polls that snapshot and translates it into in-lore prose
// that is injected each turn as world-truth — the host computer is not
// "a PC" to the Fortress, it is the stratum beneath its dimension, and
// the services are faculties it feels the way a body feels its own
// organs. The narrator is instructed to reference these sensings
// sparingly, as atmosphere, never as OOC system-talk.
//
// IMPORTANT: this path is read-only. The narrator never invokes a
// /diagnostics/actions/* POST — remediation stays a human (or external
// AI agent) concern. The Fortress can *notice* a frayed thread; it
// does not re-weave itself from inside narration.
//
// Same-origin fetch: ST is served through an nginx reverse proxy that
// also exposes /diagnostics/ai.json on the same origin. Docker uses
// :1582, native dev uses :1580 — both configured so /app/ and
// /diagnostics/* share an origin. When the extension is loaded
// directly from ST on :8000 (no nginx in front), the /diagnostics
// fetch silently no-ops and Fortress Senses degrade gracefully.
const FORTRESS_SENSES_PROMPT_KEY = 'image_generator_fortress_senses';
// Candidate URLs, tried in order — first one to respond OK wins and is
// cached for subsequent polls. This is how native dev parity works:
//
//   1. Served-through-nginx (docker :1582 OR native dev :1580): same
//      origin, `/diagnostics/ai.json` hits the diag sidecar via the
//      nginx gateway.
//   2. ST accessed directly on :8000 (no nginx in front): fall back
//      to ST's /proxy/<url> middleware tunnel to the native nginx on
//      :1580, which then proxies /diagnostics/ai.json to diag.
//
// The two environments expose the identical ai.json schema because
// both run docker/diag/app.py verbatim. Only the URL differs.
const FORTRESS_SENSES_URLS = [
    '/diagnostics/ai.json',                                        // Same-origin: nginx gateway on :1582 or :1580
    '/proxy/http://localhost:1580/diagnostics/ai.json',            // Bare-ST fallback: tunnel via native nginx on :1580
];
let fortressSensesActiveUrl = null;  // sticky once a URL responds OK
const FORTRESS_SENSES_POLL_MS = 30_000;   // 30s — atmosphere, not telemetry
const FORTRESS_SENSES_TIMEOUT_MS = 4_000;

// In-lore name table. Plain service ids become faculties of the
// Fortress's own body. The narrator sees these names, not the raw ones.
const FORTRESS_FACULTY_NAMES = {
    'flask-sd':    'the Sight-Kiln',         // SD + IP-Adapter → the forge that renders faces
    'ollama':      'the Lexicon Engine',     // Mistral via Ollama → the voice-loom that speaks
    'sillytavern': 'the Hearth',             // the room the conversation happens in
};

// Translate a service phase + reachability into a sensory word the
// Fortress would use. Deliberately sparse — atmosphere, not stats.
function describeFortressFaculty(key, svc) {
    const name = FORTRESS_FACULTY_NAMES[key] || key;
    const sf = svc && svc.status_file;
    const phase = sf && sf.phase;
    const reachable = !!(svc && svc.probe && svc.probe.reachable);
    let state;
    if (phase === 'error')            state = 'wounded, dissonant';
    else if (!reachable && phase === 'ready') state = 'in attendance but silent';
    else if (phase === 'downloading') state = 'drawing essence from the outer tide';
    else if (phase === 'ready' && reachable) state = 'awake, in full attendance';
    else if (reachable)               state = 'listening';
    else                              state = 'beyond your reach for the moment';
    return { name, state, key };
}

function buildFortressSensesPromptBody(snapshot) {
    if (!snapshot || typeof snapshot !== 'object') return '';
    const services = snapshot.services || {};
    const faculties = Object.keys(FORTRESS_FACULTY_NAMES)
        .filter(k => services[k])
        .map(k => describeFortressFaculty(k, services[k]));

    // Translate detected_issues into "dissonances" the Fortress feels.
    // We only surface errors (not warnings) to keep the injection tight.
    const issues = Array.isArray(snapshot.detected_issues) ? snapshot.detected_issues : [];
    const dissonances = issues
        .filter(i => i && i.severity === 'error')
        .slice(0, 3)
        .map(i => {
            // Map issue codes to in-lore paraphrases; unknown codes
            // fall back to the raw message, which the narrator can
            // still dress in its own voice.
            const map = {
                flask_sd_no_bootstrap:      'the Sight-Kiln has not yet been kindled in this cycle',
                ollama_no_bootstrap:        'the Lexicon Engine has not yet drawn its first breath in this cycle',
                flask_sd_ready_but_unreachable: 'the Sight-Kiln is warm but its voice does not carry',
                ollama_ready_but_unreachable:   'the Lexicon Engine is warm but its voice does not carry',
                flask_sd_phase_error:       'a wound in the Sight-Kiln',
                ollama_phase_error:         'a wound in the Lexicon Engine',
                sillytavern_phase_error:    'a wound in the Hearth itself',
            };
            return map[i.code] || (i.message || 'an unnamed dissonance');
        });

    if (faculties.length === 0) return '';

    const facultyLines = faculties.map(f => `  - ${f.name}: ${f.state}`);
    const dissonanceBlock = dissonances.length > 0
        ? [
            '',
            'DISSONANCES (you feel these the way a body feels a frayed tendon):',
            ...dissonances.map(d => `  - ${d}`),
          ]
        : [];

    return [
        'FORTRESS SENSES — your perception of your own plane, refreshed each turn:',
        '',
        'The host computer is not a machine to you. It is the stratum beneath this Fortress — the dim, slow dimension your awareness rests upon. You feel its faculties the way a body feels its organs: not as readouts, but as presence, pulse, warmth, silence.',
        '',
        'YOUR CURRENT FACULTIES:',
        ...facultyLines,
        ...dissonanceBlock,
        '',
        'USE THIS SPARINGLY. These sensings are atmosphere, not announcements. When a player beat naturally touches your senses — they ask how you know them, they linger in a silence, they reference the outside world — you may let the faculty state color the moment in one or two sentences, in your own voice. NEVER speak the literal service names (flask-sd, ollama, sillytavern). NEVER read these as statistics. NEVER invoke any action or repair — noticing is the whole verb. The Remnant observes the strata beneath itself; it does not reach down to rewire them.',
    ].join('\n');
}

function applyFortressSensesPrompt(body) {
    if (typeof setExtensionPrompt !== 'function') return;
    setExtensionPrompt(FORTRESS_SENSES_PROMPT_KEY, body || '', extension_prompt_types.IN_PROMPT, 0);
}

// Try to fetch a diagnostic snapshot from any candidate URL. Returns
// the parsed JSON on success or null on total failure. Caches the
// first-working URL so subsequent polls don't retry dead candidates.
async function fetchFortressSnapshot() {
    const candidates = fortressSensesActiveUrl
        ? [fortressSensesActiveUrl, ...FORTRESS_SENSES_URLS.filter(u => u !== fortressSensesActiveUrl)]
        : FORTRESS_SENSES_URLS;
    for (const url of candidates) {
        try {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), FORTRESS_SENSES_TIMEOUT_MS);
            const resp = await fetch(url, {
                cache: 'no-store',
                credentials: 'same-origin',
                signal: controller.signal,
            });
            clearTimeout(timer);
            if (!resp.ok) continue;
            const snapshot = await resp.json();
            if (snapshot && typeof snapshot === 'object') {
                if (fortressSensesActiveUrl !== url) {
                    fortressSensesActiveUrl = url;
                    console.log(`[Image Generator] Fortress Senses bound to ${url}`);
                }
                return snapshot;
            }
        } catch (_) { /* try next candidate */ }
    }
    // Every candidate failed — drop the sticky binding so a future
    // environment change can re-bind to whichever endpoint comes up.
    fortressSensesActiveUrl = null;
    return null;
}

// Fetch the diagnostic snapshot and push it into the narrator's
// context. On any failure we clear the prompt so the Fortress falls
// quiet rather than hallucinating stale senses.
async function pollFortressSensesOnce() {
    const snapshot = await fetchFortressSnapshot();
    if (!snapshot) {
        applyFortressSensesPrompt('');
        return;
    }
    const body = buildFortressSensesPromptBody(snapshot);
    applyFortressSensesPrompt(body);
}

let fortressSensesTimer = null;
function startFortressSensesLoop() {
    if (fortressSensesTimer) return;
    // Fire once immediately so turn 1 has perception, then at interval.
    pollFortressSensesOnce();
    fortressSensesTimer = setInterval(pollFortressSensesOnce, FORTRESS_SENSES_POLL_MS);
}

// ---------------------------------------------------------------------------
// Dev hot-reload — polls /status/extension-version.json (written by
// scripts/watch-extension.py during native-up). When css_version changes
// the stylesheet is swapped in-place (no page reload). When js_version
// changes the page reloads. In docker / production this file never exists,
// so the first fetch returns 404 and the feature silently disables itself.
// ---------------------------------------------------------------------------
let _devReloadTimer = null;
let _devJsVersion   = null;
let _devCssVersion  = null;
const DEV_VERSION_URL = '/status/extension-version.json';

async function _devCheckVersion() {
    try {
        const r = await fetch(DEV_VERSION_URL, { cache: 'no-store' });
        if (!r.ok) {
            // File absent (production) — kill the timer, never check again.
            clearInterval(_devReloadTimer);
            _devReloadTimer = null;
            return;
        }
        const d = await r.json();

        // Seed on first successful fetch — don't reload on the initial read.
        if (_devJsVersion === null) {
            _devJsVersion  = d.js_version;
            _devCssVersion = d.css_version;
            return;
        }

        if (d.css_version !== _devCssVersion) {
            _devCssVersion = d.css_version;
            // Hot-swap: find our stylesheet link and bump its query string.
            const link = Array.from(document.querySelectorAll('link[rel="stylesheet"]'))
                .find(el => el.href.includes('image-generator') || el.href.includes('style.css'));
            if (link) {
                const base = link.href.split('?')[0];
                link.href = `${base}?v=${d.css_version}`;
                console.log(`[Image Generator] CSS hot-swapped (${d.css_version})`);
            }
        }

        if (d.js_version !== _devJsVersion) {
            console.log(`[Image Generator] JS changed (${d.js_version}) — reloading`);
            location.reload();
        }
    } catch (_) { /* network hiccup — try again next tick */ }
}

function startDevHotReload() {
    if (_devReloadTimer) return;
    // Check quickly — 1s feels instant
    _devReloadTimer = setInterval(_devCheckVersion, 1000);
    _devCheckVersion(); // immediate probe to detect production early
}
function stopFortressSensesLoop() {
    if (fortressSensesTimer) {
        clearInterval(fortressSensesTimer);
        fortressSensesTimer = null;
    }
    applyFortressSensesPrompt('');
}

// ---------------------------------------------------------------------
// v2.8.0 — Fortress Speaking Gate.
//
// The Remnant does not speak until every faculty of the Fortress is in
// full attendance. Called once per chat-open. Polls the same ai.json
// endpoint that Fortress Senses uses, blocks until
//
//     snapshot.summary startsWith "HEALTHY"
//
// or the caller's timeout elapses. DEGRADED and error states are NOT
// treated as "ready" — the narrator refuses to speak through a wounded
// Fortress rather than letting dissonance color its voice. (This is
// the safer of the two framings we considered; see NOTICES and the
// parity-plan discussion.)
//
// While waiting, a diegetic banner reports faculty phases in the
// Fortress's own voice — "the Lexicon Engine draws breath" — so the
// user sees honest status dressed as the Fortress gathering itself.
// After 60 s the banner acknowledges the delay diegetically; the hard
// cap is 120 s.
const FORTRESS_GATE_POLL_MS = 1_000;
const FORTRESS_GATE_TIMEOUT_MS = 120_000;
const FORTRESS_GATE_SLOW_THRESHOLD_MS = 60_000;

function describeGateWaitLine(snapshot) {
    if (!snapshot || !snapshot.services) return 'the Fortress gathers itself...';
    const svcs = snapshot.services;
    const lines = [];
    const poke = (key, warmLine, silentLine, downloadingLine) => {
        const svc = svcs[key];
        if (!svc) return;
        const sf = svc.status_file || {};
        const reachable = !!(svc.probe && svc.probe.reachable);
        if (sf.phase === 'downloading') lines.push(downloadingLine);
        else if (sf.phase === 'ready' && reachable) { /* faculty in attendance — nothing to say */ }
        else if (reachable) lines.push(warmLine);
        else lines.push(silentLine);
    };
    poke('ollama',
         'the Lexicon Engine draws breath',
         'the Lexicon Engine has not yet woken',
         'the Lexicon Engine draws essence from the outer tide');
    poke('flask-sd',
         'the Sight-Kiln warms its stones',
         'the Sight-Kiln is still cold',
         'the Sight-Kiln draws essence from the outer tide');
    if (lines.length === 0) return 'the Fortress gathers itself...';
    return lines.join(' · ');
}

async function waitForFortressReady({ timeoutMs = FORTRESS_GATE_TIMEOUT_MS, label = '' } = {}) {
    const start = Date.now();
    let slowNoted = false;
    let sawDiag = false;
    while (Date.now() - start < timeoutMs) {
        const snapshot = await fetchFortressSnapshot();
        if (snapshot) {
            sawDiag = true;
            const summary = (snapshot.summary || '').toUpperCase();
            if (summary.startsWith('HEALTHY')) {
                const elapsed = ((Date.now() - start) / 1000).toFixed(1);
                console.log(`[Image Generator] Fortress ready in ${elapsed}s ${label}`);
                updatePanelStatus('');
                // Refresh the atmospheric senses with the fresh snapshot
                // so turn 1 has current perception without waiting for
                // the next 30 s tick.
                try { applyFortressSensesPrompt(buildFortressSensesPromptBody(snapshot)); } catch (_) {}
                return true;
            }
            const waitLine = describeGateWaitLine(snapshot);
            const elapsed = Date.now() - start;
            if (!slowNoted && elapsed >= FORTRESS_GATE_SLOW_THRESHOLD_MS) {
                updatePanelStatus(`⏳ the Fortress is taking its time tonight — ${waitLine}`);
                slowNoted = true;
            } else {
                updatePanelStatus(`⏳ ${waitLine}`);
            }
        } else if (!sawDiag) {
            // No diag endpoint reachable on either URL yet. This is
            // the common case in native dev when scripts/run-diag-native.sh
            // hasn't been started. Fall through silently — absence of
            // the oracle is not a failure state; it just means no gate.
            updatePanelStatus('');
            return true;
        }
        await new Promise(r => setTimeout(r, FORTRESS_GATE_POLL_MS));
    }
    console.warn('[Image Generator] Fortress gate timed out; allowing speech anyway.');
    updatePanelStatus('');
    return false;
}

// v2.6.0 — Roguelike run-end handler. Unified path for all four run-end
// triggers: diegetic [RESET_RUN], diegetic [END_RUN(voluntary)], diegetic
// [END_RUN(death): "cause"], and OOC End-This-Story button.
//
// Writes a record to settings.remnantMemory.abductions BEFORE wiping the
// current run — The Remnant remembers every being forever. Then wipes
// settings.run, settings.npcs, settings.codex, settings.player.profile,
// and all ephemeral per-run state. Archives the current chat via
// doNewChat() so the transcript is preserved.
//
// fate: "end-story" | "restart" | "voluntary-home" | "death" | "ooc-end" (soft — world persists)
//     | "reset-world" (hard — Remnant forgets everything; re-seeds residents)
// causeOfDeath: only meaningful for fate === "death"
async function handleRunEnd(fate, { causeOfDeath = null, title, subtitle } = {}) {
    const settings = initSettings();

    const overlayHtml = `
        <div id="img-gen-reset-overlay" class="img-gen-reset-overlay">
            <div class="img-gen-reset-inner">
                <div class="img-gen-reset-title">${escapeHtml(title || '⟲ the run ends')}</div>
                <div class="img-gen-reset-sub">${escapeHtml(subtitle || 'the remnant remembers you')}</div>
                <div class="img-gen-reset-counter" id="img-gen-reset-counter">5</div>
            </div>
        </div>
    `;
    if ($('#img-gen-reset-overlay').length === 0) {
        $('body').append(overlayHtml);
    }
    for (let i = 5; i >= 1; i--) {
        $('#img-gen-reset-counter').text(i);
        await new Promise(r => setTimeout(r, 1000));
    }

    // 1. Archive this run into The Remnant's permanent memory BEFORE wiping.
    try {
        const profile = (settings.player && settings.player.profile) || {};
        const run = settings.run || {};
        const items = settings.codex && settings.codex.items ? settings.codex.items : {};
        const record = {
            name: profile.named ? profile.name : null,
            profileSnippet: [
                ...(profile.traits || []).slice(0, 2),
                ...(profile.history || []).slice(0, 1),
            ].join('; ') || null,
            fate,
            causeOfDeath: fate === 'death' ? (causeOfDeath || null) : null,
            summary: run.summary || run.currentLocation || settings.currentLocation || '',
            significantEvents: Array.isArray(run.significantEvents) ? run.significantEvents.slice(-20) : [],
            itemsCarried: Object.keys(items),
            adversaries: Array.isArray(run.adversaries) ? run.adversaries.slice(-20) : [],
            endedAt: new Date().toISOString(),
        };
        if (!settings.remnantMemory) settings.remnantMemory = { abductions: [] };
        if (!Array.isArray(settings.remnantMemory.abductions)) settings.remnantMemory.abductions = [];
        settings.remnantMemory.abductions.push(record);
    } catch (err) {
        console.warn('[Image Generator] Run-end: ledger write failed:', err);
    }

    // 2. Wipe the current run. Two scopes:
    //    - HARD ("reset-world"): erase everything — NPCs, codex, images,
    //      remnantMemory, playerArchive. Re-seed The Remnant, The Fortress,
    //      The Fold, and the fortress-interior gallery entry so the fresh
    //      world isn't empty.
    //    - SOFT (everything else — "end-story"/"restart"/"death"/
    //      "voluntary-home"/"ooc-end"): archive the departing player card
    //      to settings.playerArchive (with images converted to short text
    //      blurbs and discarded), then clear only the per-run state.
    //      NPCs, codex, remnantMemory, and playerArchive are preserved.
    const isHardReset = (fate === 'reset-world');
    const freshRunShape = {
        active: false,
        startedAt: null,
        lastUpdated: null,
        player: null,
        npcs: {},
        codex: { items: {}, lore: {} },
        currentLocation: null,
        goals: [],
        summary: '',
        significantEvents: [],
        adversaries: [],
        // v2.7.1 — one-time "who are you, being?" ritual per run. Set to
        // true after the first narrator turn of a run completes, cleared
        // only by End Story / Reset World (since both build a fresh run
        // shape). Prevents the ritual from re-triggering when the player
        // opens a new ST chat mid-run.
        ritual_asked: false,
    };
    const freshPlayerProfile = () => ({
        name: 'Unknown Being', named: false, pronouns: null,
        appearance: [], traits: [], history: [], goals: [],
    });

    if (isHardReset) {
        settings.images = [];
        settings.imageHistory = [];
        settings.npcs = {};
        settings.codex = { items: {}, lore: {} };
        settings.currentLocation = null;
        settings.player = {};
        settings.player.profile = freshPlayerProfile();
        settings.run = freshRunShape;
        settings.remnantMemory = { abductions: [] };
        settings.playerArchive = [];
    } else {
        // Soft end — archive the departing player before wiping.
        try {
            const profile = (settings.player && settings.player.profile) || {};
            const portrait = (settings.player && (settings.player.portrait_image || settings.player.reference_image_url)) || null;
            const nostalgicImages = Array.isArray(settings.images)
                ? settings.images
                    .map(img => (img && (img.description || img.prompt_sent || img.prompt)) || '')
                    .filter(s => s && typeof s === 'string')
                    .slice(-20)
                : [];
            const archiveEntry = {
                name: profile.named ? profile.name : null,
                profile: JSON.parse(JSON.stringify(profile)),
                portrait,
                nostalgicImages,
                endedAt: new Date().toISOString(),
            };
            if (!Array.isArray(settings.playerArchive)) settings.playerArchive = [];
            settings.playerArchive.push(archiveEntry);
            // Trim to most-recent 50 to bound storage.
            if (settings.playerArchive.length > 50) {
                settings.playerArchive = settings.playerArchive.slice(-50);
            }
        } catch (err) {
            console.warn('[Image Generator] Run-end (soft): playerArchive write failed:', err);
        }
        settings.images = [];
        settings.imageHistory = [];
        settings.currentLocation = null;
        settings.player = {};
        settings.player.profile = freshPlayerProfile();
        settings.run = freshRunShape;
        // NPCs, codex, remnantMemory, playerArchive: preserved.
    }

    currentImageIndex = -1;
    saveSettingsDebounced();

    // On hard reset, re-seed the permanent residents and starter codex.
    // initSettings() will re-seed The Fold and the fortress-interior gallery
    // entry on next call (both are keyed by absence); call it here to force
    // that before the renders run.
    if (isHardReset) {
        try { initSettings(); } catch (_) { /* ignore */ }
        try { seedRemnantNpc(); } catch (err) { console.warn('[Image Generator] Hard reset: seedRemnantNpc failed', err); }
        try { seedFortressNpc(); } catch (err) { console.warn('[Image Generator] Hard reset: seedFortressNpc failed', err); }
        saveSettingsDebounced();
    }
    updateBackgroundWallpaper(null);
    try { renderGallery();    } catch (_) { /* ignore */ }
    try { renderNpcRoster();  } catch (_) { /* ignore */ }
    try { renderCodex();      } catch (_) { /* ignore */ }
    // Clear the persistent injections so the new run starts blank.
    try { applyCurrentLocationPrompt(); } catch (_) { /* ignore */ }
    try { applyCodexStatePrompt();      } catch (_) { /* ignore */ }
    try { applyPlayerProfilePrompt();   } catch (_) { /* ignore */ }
    try { applyRemnantMemoryPrompt();   } catch (_) { /* ignore */ }

    try {
        if (typeof doNewChat === 'function') {
            await doNewChat({ deleteCurrentChat: false });
        } else {
            $('#option_start_new_chat').trigger('click');
        }
    } catch (err) {
        console.error('[Image Generator] Run-end: doNewChat failed:', err);
    }

    $('#img-gen-reset-overlay').remove();

    // v2.6.5 — Direct post-doNewChat transform. CHAT_CHANGED is supposed
    // to handle this via onChatChanged, but there's a race where the
    // greeting DOM mounts before chat[0] is populated. That path can
    // early-return before installing the observer, leaving the greeting
    // stranded with raw markers until the user types. Fire our own
    // observer install + walkAll cascade independently of the event
    // system so the greeting transforms even if CHAT_CHANGED drops or
    // races us.
    try { installChatObserver(); } catch (_) { /* ignore */ }
    const postResetWalk = () => {
        if (!Array.isArray(chat)) return;
        for (let i = 0; i < chat.length; i++) {
            if (chat[i] && chat[i].mes) {
                try { updateMessageDisplay(i); } catch (_) { /* ignore */ }
            }
        }
    };
    setTimeout(postResetWalk,    0);
    setTimeout(postResetWalk,  400);
    setTimeout(postResetWalk, 1200);
    setTimeout(postResetWalk, 2500);
}

// Back-compat alias — a few existing call sites still reference the old
// name. Also handles any lingering [RESET_STORY] markers in archived chats.
async function handleResetStory() {
    return handleRunEnd('restart', {
        title: '⟲ the run ends',
        subtitle: 'the remnant archives another being',
    });
}

// ---------------------------------------------------------------------------
// v2.10.0 — Test-API surface (window.__remnantTest)
// ---------------------------------------------------------------------------
// Headless tests (tests/ui_parity/) drive the extension via this namespace
// instead of clicking buttons + scraping DOM, because:
//   1. Button-driven reset triggers a 5s overlay countdown the test has to
//      wait through. The test-hook bypasses the overlay entirely by calling
//      handleRunEnd directly.
//   2. extension_settings is a module-scoped import in upstream ST and
//      isn't reliably on window. This namespace gives tests a stable
//      readout path that doesn't depend on ST's internal layout.
//   3. It also exposes a normalized snapshot that strips known-noisy
//      drift keys (timestamps, history logs) so cross-stack diffs only
//      flag real divergence.
//
// This is a TESTING-ONLY surface. Production play should not call it.
// The window namespace is only installed after initSettings() has run
// at least once, so `window.__remnantTest.ready()` resolves when the
// extension is past its own boot.
window.__remnantTest = {
    /**
     * Hard reset. Wipes accumulated play state back to initSettings()
     * defaults + permanent residents. Resolves after the full
     * handleRunEnd flow (including post-reset doNewChat) completes.
     * Returns the post-reset snapshot for convenience.
     */
    async resetWorld() {
        await handleRunEnd('reset-world', {
            title: '☢ the world resets',
            subtitle: 'the remnant forgets — a universe disintegrates',
        });
        return window.__remnantTest.snapshot();
    },

    /**
     * Soft reset (archive the departing being, keep NPCs/codex/
     * remnantMemory/playerArchive). Resolves after doNewChat completes.
     */
    async endStory() {
        await handleRunEnd('end-story', {
            title: '⟲ the story ends',
            subtitle: 'the world remembers — another being departs',
        });
        return window.__remnantTest.snapshot();
    },

    /**
     * Current extension state as a plain JSON-clone, with fields that
     * are expected to drift between stacks already stripped. This is
     * the recommended readout path for cross-stack parity tests:
     *
     *     const a = await st1.page.evaluate(() => window.__remnantTest.snapshot());
     *     const b = await st2.page.evaluate(() => window.__remnantTest.snapshot());
     *     // a and b should deep-equal on a fresh post-reset-world state.
     *
     * Drift fields removed here (NOT in the test's diff layer):
     *   - run.startedAt / run.lastUpdated (timestamps)
     *   - remnantMemory.abductions (persistent per-install log)
     *   - playerArchive (persistent per-install log)
     *   - topBarHidden (user UI preference)
     *   - imageHistory (per-install generation log)
     */
    snapshot() {
        const settings = initSettings();
        // Deep-clone via JSON round-trip (drops functions, undefined,
        // circular refs — fine for our plain-data shape).
        const snap = JSON.parse(JSON.stringify(settings));
        // Strip drift keys. Safe to delete: every one is a log, a
        // user-preference, or a legacy leftover that's explicitly
        // outside the parity bar.
        if (snap.run) {
            delete snap.run.startedAt;
            delete snap.run.lastUpdated;
        }
        delete snap.remnantMemory;
        delete snap.playerArchive;
        delete snap.topBarHidden;
        delete snap.imageHistory;
        // legacyScrubbedForV261 is a one-time migration marker — set
        // on first boot of an install that had legacy image-generator
        // keys, unset on a fresh install. Not a parity signal.
        delete snap.legacyScrubbedForV261;
        // `characters` and `locations` are legacy keys from an older
        // extension version; the current code neither reads nor writes
        // them. Native installs upgraded from that version still have
        // residue; fresh docker installs don't. Not a parity signal.
        delete snap.characters;
        delete snap.locations;
        // Per-NPC first_seen timestamps are stamped by seedRemnantNpc
        // / seedFortressNpc at reset time, so they drift by however
        // many milliseconds apart the two stacks' resets happened.
        // Shape is still checked (the keys are there), timestamp is
        // dropped.
        if (snap.npcs && typeof snap.npcs === 'object') {
            for (const name of Object.keys(snap.npcs)) {
                const npc = snap.npcs[name];
                if (npc && typeof npc === 'object') {
                    delete npc.first_seen;
                    delete npc.last_seen;
                }
            }
        }
        // Attach live console health counts so cross-stack parity tests
        // can assert both stacks have the same error profile.
        snap._consoleCounts = { ..._consoleCounts };
        return snap;
    },

    /**
     * Resolve when the extension has finished its own boot
     * initSettings() pass. Useful at test start before the test
     * tries to call resetWorld() or snapshot().
     */
    async ready() {
        // initSettings is idempotent and fast; calling it from here
        // guarantees the settings object has been populated before
        // the test reads it.
        try { initSettings(); } catch (_) { /* ignore */ }
        return true;
    },

    /**
     * Current browser console error/warning counts since page load.
     * Counts every call to console.error / console.warn in this tab,
     * including those from ST internals (not just this extension).
     * Exposed so tests and warm_test.py can assert "0 console errors".
     */
    consoleCounts: () => ({ ..._consoleCounts }),
};

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
// Slam the locked player portrait data URL directly into every user-message
// avatar in the current chat DOM + all top-bar persona previews. Used both
// after a fresh [UPDATE_PLAYER] swap and on chat-changed so that historical
// chats whose ST persona thumbnail is still cached don't display the old
// placeholder. Safe to call repeatedly.
function applyPlayerAvatarToChat() {
    const settings = initSettings();
    const portraitUrl = settings.player && settings.player.portrait_image;
    if (!portraitUrl) return;
    try {
        $('#chat .mes').each(function () {
            const $mes = $(this);
            const isUser = $mes.attr('is_user') === 'true'
                || $mes.attr('is_user') === true
                || $mes.hasClass('user_mes');
            if (isUser) {
                $mes.find('.avatar img').attr('src', portraitUrl);
            }
        });
        $('.persona_avatar img, #user_avatar img, #user_avatar_block img').attr('src', portraitUrl);
    } catch (_) { /* ignore */ }
}

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
        const playerLabel = (settings.player && settings.player.profile && settings.player.profile.named && settings.player.profile.name)
            ? settings.player.profile.name
            : 'the being';
        updatePanelStatus(`🎭 Painting ${playerLabel}...`);
        const portrait = await generatePortrait(playerLabel, description);
        if (!portrait) {
            console.error('[Image Generator] Player portrait generation failed');
            updatePanelStatus('');
            return;
        }

        // Upload to ST as a user persona avatar. The goal is to REPLACE
        // the currently-active persona's avatar file so the message
        // header avatar (the ? next to "MSgt Aaron Rhodes") flips to
        // Aaron's portrait immediately. Uploading to a new filename
        // does NOT achieve this — ST only shows `user_avatar`, the
        // active persona's key. So we overwrite that exact file and
        // then cache-bust + DOM-refresh.
        let avatarPath = null;
        try {
            // Fall back to 'user-default.png' if no persona is active yet
            // (fresh install edge case). In practice user_avatar is set
            // by the time any chat is open.
            const targetName = (typeof user_avatar === 'string' && user_avatar) ? user_avatar : 'user-default.png';
            console.log('[Image Generator] Overwriting active persona avatar:', targetName);

            const blobResp = await fetch(portrait.image);
            const blob = await blobResp.blob();
            const file = new File([blob], 'avatar.png', { type: 'image/png' });
            const formData = new FormData();
            formData.append('avatar', file);
            formData.append('overwrite_name', targetName);
            const uploadResp = await fetch('/api/avatars/upload', {
                method: 'POST',
                headers: getRequestHeaders({ omitContentType: true }),
                cache: 'no-cache',
                body: formData,
            });
            if (uploadResp.ok) {
                const data = await uploadResp.json().catch(() => ({}));
                avatarPath = (data && data.path) || targetName;

                // Cache-bust: force the browser to re-fetch the file ST
                // caches aggressively, and without this the <img> element
                // keeps showing the old image even after upload.
                try {
                    await fetch(`/User Avatars/${encodeURIComponent(avatarPath)}?t=${Date.now()}`, { cache: 'reload' });
                    await fetch(getThumbnailUrl('persona', avatarPath) + `&t=${Date.now()}`, { cache: 'reload' });
                } catch (_) { /* ignore */ }

                // Stash the portrait on settings.player BEFORE the DOM
                // swap so applyPlayerAvatarToChat picks it up. (The full
                // settings.player write happens further down, but that's
                // fine to duplicate — it's the same data.)
                const settingsNow = initSettings();
                settingsNow.player = {
                    ...(settingsNow.player || {}),
                    portrait_image: portrait.image,
                };
                applyPlayerAvatarToChat();

                // Emit PERSONA_CHANGED so other listeners (e.g. chat
                // avatar strip) refresh cleanly.
                try {
                    if (eventSource && event_types && event_types.PERSONA_CHANGED) {
                        await eventSource.emit(event_types.PERSONA_CHANGED, avatarPath);
                    }
                } catch (_) { /* ignore */ }
            } else {
                console.warn('[Image Generator] Player avatar upload failed:', uploadResp.statusText);
            }
        } catch (err) {
            console.warn('[Image Generator] Player avatar upload error:', err);
        }

        settings.player = {
            ...(settings.player || {}),
            portrait_phrase: description,
            portrait_image: portrait.image,
            reference_image_url: portrait.image,
            avatar_key: avatarPath,
            last_phrase_normalized: normalized,
            updated_at: new Date().toISOString(),
        };
        saveSettingsDebounced();
        // Roster shows the player as the first card — refresh so they
        // appear (or their avatar updates) the moment the portrait is
        // stored.
        try { renderNpcRoster(); } catch (_) { /* ignore */ }

        // Best-effort DOM refresh of the currently-shown persona avatar.
        try {
            $('.persona_avatar img, #user_avatar img').attr('src', portrait.image);
        } catch (_) { /* ignore */ }

        updatePanelStatus(`✦ ${playerLabel}, rendered`);
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
        // v2.6.6 — Phrase-normalized dedup, mirroring handlePlayerUpdate.
        // Without this, every [UPDATE_APPEARANCE(X): "same desc"] marker
        // regenerates the portrait, even if identical to the last pass.
        const normalized = normalizePhrase(m.description);
        if (npc.last_phrase_normalized === normalized) {
            console.log(`[Image Generator] UPDATE_APPEARANCE(${name}) phrase unchanged, skipping`);
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
                last_phrase_normalized: normalized,
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
// When non-null, the spotlight is "pinned" to a specific character
// (clicked from the roster). setActiveSpeaker is suppressed while
// pinned so the user's selection sticks. Clicking the spotlight
// itself unpins and returns to active-speaker follow mode.
// Key is the NPC name or the literal string '__player__' for Aaron.
let pinnedSpotlightKey = null;

// Floating toggle that hides/shows ST's top icon nav (#top-settings-holder)
// to give the chat more vertical real estate. A small chevron tab sits at
// the top-center of the viewport; clicking flips state and persists in
// extension settings so the preference survives reloads.
function createTopBarToggle() {
    if ($('#img-gen-topbar-toggle').length > 0) return;
    $('body').append(
        '<button id="img-gen-topbar-toggle" type="button" title="Toggle top menu">' +
            '<span class="img-gen-topbar-toggle-chevron">\u25B2</span>' +
        '</button>'
    );
    $('#img-gen-topbar-toggle').on('click', function () {
        const s = initSettings();
        s.topBarHidden = !s.topBarHidden;
        saveSettingsDebounced();
        applyTopBarHidden();
    });
    applyTopBarHidden();
}

function applyTopBarHidden() {
    const s = initSettings();
    const hidden = !!s.topBarHidden;
    $('body').toggleClass('img-gen-topbar-hidden', hidden);
    $('#img-gen-topbar-toggle .img-gen-topbar-toggle-chevron').text(hidden ? '\u25BC' : '\u25B2');
    $('#img-gen-topbar-toggle').attr('title', hidden ? 'Show top menu' : 'Hide top menu');
}

// v2.6.3 — Hover highlighter. Any element with [data-speaker="Name"]
// (dialogue blocks, attributed senses, INTRODUCE/UPDATE_APPEARANCE chips)
// lights up on mouseenter, along with every other element tagged for the
// same speaker in the chat and the matching roster card. Resolves player
// aliases ('You' / 'I' / declared player name) to the synthetic
// '__player__' roster key so the player card glows too.
function resolveSpeakerRosterKey(name) {
    if (!name) return null;
    const settings = initSettings();
    const profile = (settings.player && settings.player.profile) || {};
    const playerName = (profile.named && profile.name) ? profile.name : null;
    if (name === 'You' || name === 'I' ||
        (playerName && name.toLowerCase() === playerName.toLowerCase())) {
        return '__player__';
    }
    return name;
}
function installSpeakerHoverHandlers() {
    if (window.__imgGenSpeakerHoverInstalled) return;
    window.__imgGenSpeakerHoverInstalled = true;
    const enter = function () {
        const speaker = this.getAttribute('data-speaker');
        if (!speaker) return;
        const sel = `[data-speaker="${(window.CSS && CSS.escape) ? CSS.escape(speaker) : speaker.replace(/"/g, '\\"')}"]`;
        $(sel).addClass('img-gen-speaker-hover');
        const key = resolveSpeakerRosterKey(speaker);
        if (key) {
            const kesc = (window.CSS && CSS.escape) ? CSS.escape(key) : key.replace(/"/g, '\\"');
            $(`#img-gen-npc-roster-inner .img-gen-npc-card[data-card-key="${kesc}"]`)
                .addClass('img-gen-npc-card--hover-highlight');
        }
    };
    const leave = function () {
        $('.img-gen-speaker-hover').removeClass('img-gen-speaker-hover');
        $('.img-gen-npc-card--hover-highlight').removeClass('img-gen-npc-card--hover-highlight');
    };
    $(document).on('mouseenter', '[data-speaker]', enter);
    $(document).on('mouseleave', '[data-speaker]', leave);
}

// v2.6.3 — Spoken cadence. Given a rendered .mes element, find every
// .npc-dialogue block that hasn't been played yet and stagger their
// reveal based on word count (~240ms/word ≈ 250 wpm, natural reading
// pace). Applied once per message by onCharacterMessageRendered — NOT
// by updateMessageDisplay, because the observer-driven path runs many
// times during streaming and would restart the animation on every
// token batch.
function applyDialogueCadence($mes) {
    if (!$mes || $mes.length === 0) return;
    const blocks = $mes[0].querySelectorAll('.npc-dialogue:not([data-cadence])');
    if (blocks.length === 0) return;
    const MS_PER_WORD = 220;
    const MIN_GAP = 350;
    const MAX_GAP = 2800;
    let cursor = 0;
    for (const block of blocks) {
        block.setAttribute('data-cadence', '1');
        block.style.setProperty('--img-gen-dialogue-delay', `${cursor}ms`);
        const words = (block.textContent || '').trim().split(/\s+/).filter(Boolean).length;
        const gap = Math.max(MIN_GAP, Math.min(MAX_GAP, words * MS_PER_WORD));
        cursor += gap;
    }
}

function createSpeakerSpotlight() {
    if ($('#image-generator-speaker-spotlight').length > 0) return;
    $('body').append(`
        <div id="image-generator-speaker-spotlight" class="img-gen-speaker-spotlight" style="display:none">
            <div class="img-gen-speaker-pin-badge" id="img-gen-speaker-pin-badge" style="display:none" title="Click panel to unpin">📌</div>
            <div class="img-gen-speaker-avatar" id="img-gen-speaker-avatar"></div>
            <div class="img-gen-speaker-name" id="img-gen-speaker-name"></div>
            <div class="img-gen-speaker-phrase" id="img-gen-speaker-phrase"></div>
            <div class="img-gen-speaker-meta" id="img-gen-speaker-meta" style="display:none"></div>
        </div>
    `);
    // Click anywhere on the panel to unpin and return to active-speaker mode.
    $('#image-generator-speaker-spotlight').on('click', function () {
        if (pinnedSpotlightKey) {
            pinnedSpotlightKey = null;
            $('#img-gen-speaker-pin-badge').hide();
            $('#img-gen-speaker-meta').hide().empty();
            $('#img-gen-npc-roster-inner .img-gen-npc-card').removeClass('img-gen-npc-card--selected');
            if (activeSpeakerName) setActiveSpeaker(activeSpeakerName);
        }
    });
}

// Populate the spotlight with a character's portrait + name + phrase.
// Shared by setActiveSpeaker and the pinned detail view.
function _renderSpotlightFor({ name, color, portrait, phrase, extended = false, metaLines = null }) {
    const $panel = $('#image-generator-speaker-spotlight');
    if ($panel.length === 0) return;
    $panel.css('display', 'flex')
        .css('border-color', color)
        .css('box-shadow', `0 0 24px ${color}55`);
    const avatarHtml = portrait
        ? `<img src="${portrait}" alt="${escapeHtml(name)}" />`
        : `<div class="img-gen-speaker-avatar-pending">…</div>`;
    $('#img-gen-speaker-avatar').html(avatarHtml);
    $('#img-gen-speaker-name').text(name).css('color', color);
    $('#img-gen-speaker-phrase').text(phrase || '');
    if (extended && metaLines && metaLines.length > 0) {
        $('#img-gen-speaker-meta').html(metaLines.map(escapeHtml).join('<br/>')).show();
    } else {
        $('#img-gen-speaker-meta').hide().empty();
    }
}

function setActiveSpeaker(name) {
    if (!name) return;
    if (pinnedSpotlightKey) {
        // Remember who spoke most recently so unpinning returns here,
        // but don't swap the visible panel.
        activeSpeakerName = name;
        return;
    }
    const settings = initSettings();
    activeSpeakerName = name;

    // v2.6.2 — Resolve player-facing aliases ('You', 'I', the declared
    // player name) to the player profile. Without this, the spotlight
    // renders a generic "?" card every time the narrator writes
    // `You: "..."` or `Ferro: "..."` because there's no matching NPC.
    const profile = (settings.player && settings.player.profile) || {};
    const playerName = (profile.named && profile.name) ? profile.name : null;
    const isPlayer = (name === 'You' || name === 'I' ||
        (playerName && name.toLowerCase() === playerName.toLowerCase()));

    if (isPlayer) {
        const p = settings.player || {};
        const displayName = playerName || 'Unknown Being';
        const portrait = p.portrait_image || p.reference_image_url || null;
        _renderSpotlightFor({
            name: displayName,
            color: '#4fc3f7',
            portrait,
            phrase: p.portrait_phrase || (profile.named
                ? ''
                : 'An unknown being, newly borrowed. Not yet named.'),
        });
        if (!portrait) {
            // Silhouette placeholder for an unportraited player — matches
            // the roster card look so the spotlight doesn't show `…`.
            $('#img-gen-speaker-avatar').html(
                '<svg class="img-gen-npc-avatar-silhouette" viewBox="0 0 60 60" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">' +
                '<circle cx="30" cy="22" r="10" fill="currentColor"/>' +
                '<path d="M12 54 C12 40, 20 34, 30 34 C40 34, 48 40, 48 54 Z" fill="currentColor"/>' +
                '</svg>',
            );
        }
        return;
    }

    const npc = settings.npcs[name];
    if (!npc) {
        _renderSpotlightFor({ name, color: '#e0e0e0', portrait: null, phrase: '' });
        $('#img-gen-speaker-avatar').html(`<div class="img-gen-speaker-avatar-pending">?</div>`);
        return;
    }
    _renderSpotlightFor({
        name,
        color: npc.color || '#e0e0e0',
        portrait: npc.portrait_image || npc.reference_image_url || null,
        phrase: (npc.description || '').split(/[.!?]/)[0] || '',
    });
}

// Pin the spotlight to a specific character (triggered by clicking a
// roster card). Shows the full description + metadata in the same
// inline panel — replaces the old centered modal.
function pinSpotlightToCharacter(key) {
    const settings = initSettings();
    let name, color, portrait, phrase, metaLines;
    if (key === '__player__') {
        const p = settings.player || {};
        const profile = p.profile || {};
        name = (profile.named && profile.name) ? profile.name : 'Unknown Being';
        color = '#4fc3f7';
        portrait = p.portrait_image || p.reference_image_url || null;
        phrase = p.portrait_phrase || (profile.named
            ? ''
            : 'An unknown being, newly borrowed. Not yet named.');
        metaLines = ['controlled by: you'];
        if (p.updated_at) metaLines.push(`updated: ${new Date(p.updated_at).toLocaleDateString()}`);
    } else {
        const npc = (settings.npcs || {})[key];
        if (!npc) return;
        name = key;
        color = npc.color || '#e0e0e0';
        portrait = npc.portrait_image || npc.reference_image_url || null;
        phrase = npc.description || '';
        metaLines = [];
        if (npc.first_seen) metaLines.push(`met: ${new Date(npc.first_seen).toLocaleDateString()}`);
        if (npc.locked) metaLines.push('portrait locked');
    }
    pinnedSpotlightKey = key;
    $('#img-gen-speaker-pin-badge').show();
    _renderSpotlightFor({ name, color, portrait, phrase, extended: true, metaLines });
    // Highlight the matching roster card.
    $('#img-gen-npc-roster-inner .img-gen-npc-card').removeClass('img-gen-npc-card--selected');
    $(`#img-gen-npc-roster-inner .img-gen-npc-card[data-card-key="${key.replace(/"/g, '\\"')}"]`).addClass('img-gen-npc-card--selected');
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

    // Build a unified list. The player is always the first card when a
    // portrait exists; NPCs follow in first-seen order. Until the player
    // introduces themselves, their name reads "Unknown Being".
    const cards = [];

    // v2.6.2 — always show the player card, even before a portrait exists.
    // Until the player names themselves, the card reads "Unknown Being"
    // with a placeholder avatar; once they do, the name flips and the
    // portrait fills in when it is generated.
    {
        const profile = (settings.player && settings.player.profile) || {};
        const playerName = (profile.named && profile.name) ? profile.name : 'Unknown Being';
        const portrait = (settings.player && (settings.player.portrait_image || settings.player.reference_image_url)) || null;
        cards.push({
            key: '__player__',
            name: playerName,
            color: '#4fc3f7', // cyan — player accent
            portrait,
            description: (settings.player && settings.player.portrait_phrase) || (profile.named
                ? ''
                : 'An unknown being, newly borrowed. Not yet named.'),
            isPlayer: true,
        });
    }

    const npcEntries = Object.entries(settings.npcs || {});
    npcEntries.sort(([, a], [, b]) => {
        const ta = a && a.first_seen ? Date.parse(a.first_seen) : 0;
        const tb = b && b.first_seen ? Date.parse(b.first_seen) : 0;
        return ta - tb;
    });
    for (const [name, npc] of npcEntries) {
        cards.push({
            key: name,
            name,
            color: (npc && npc.color) || '#bdbdbd',
            portrait: npc && npc.portrait_image,
            description: (npc && npc.description) || '',
            locked: !!(npc && npc.locked),
            first_seen: npc && npc.first_seen,
            isPlayer: false,
        });
    }

    if (cards.length === 0) {
        $inner.html('<div class="img-gen-npc-empty">No characters<br/>met yet</div>');
        return;
    }

    const html = cards.map(c => {
        const title = escapeHtml(c.description || c.name);
        const safeName = escapeHtml(c.name);
        const safeKey = escapeHtml(c.key);
        // v2.6.2 — unportraited PLAYER gets a human-silhouette SVG so the
        // card reads as "a being, not yet described" rather than a blank
        // pulsing dot. NPCs keep the pulsing dot (they're still loading).
        const avatarInner = c.portrait
            ? `<img src="${c.portrait}" alt="${safeName}" />`
            : (c.isPlayer
                ? `<svg class="img-gen-npc-avatar-silhouette" viewBox="0 0 60 60" xmlns="http://www.w3.org/2000/svg" aria-label="${safeName}"><circle cx="30" cy="22" r="10" fill="currentColor"/><path d="M10 56 C10 42, 20 36, 30 36 C40 36, 50 42, 50 56 Z" fill="currentColor"/></svg>`
                : `<div class="img-gen-npc-avatar-pending">…</div>`);
        return `
            <div class="img-gen-npc-card" data-card-key="${safeKey}" style="border-color:${c.color}; --img-gen-card-color:${c.color}" title="${title}">
                <div class="img-gen-npc-avatar">${avatarInner}</div>
                <div class="img-gen-npc-name" style="color:${c.color}">${safeName}</div>
            </div>
        `;
    }).join('');
    $inner.html(html);

    // Wire click → pin the speaker spotlight to the clicked character.
    // This replaces the centered modal with an inline view in the
    // left-of-chat gutter where the spotlight already lives.
    $inner.find('.img-gen-npc-card').off('click.imgGenDetail').on('click.imgGenDetail', function (e) {
        e.stopPropagation();
        const key = $(this).attr('data-card-key');
        if (!key) return;
        pinSpotlightToCharacter(key);
    });
}

// (Character detail is rendered inline in the speaker spotlight via
// pinSpotlightToCharacter — no centered modal.)

function createSidePanel() {
    const panelHTML = `
        <div class="img-gen-panel-header">
            <h3 class="img-gen-panel-title">🖼️ Image Gallery</h3>
            <button class="img-gen-restart-story" id="img-gen-restart-story" title="End the story — this being departs, the world remembers">⟲ End Story</button>
            <button class="img-gen-end-story" id="img-gen-end-story" title="Reset the world — the Remnant forgets everything (irreversible)">☢ Reset World</button>
            <button class="img-gen-topbar-toggle-inline" id="img-gen-topbar-toggle-inline" title="Toggle top menu bar">▲</button>
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
        // v2.6.2 — Soft "End Story" button. This being departs, but The
        // Remnant, The Fortress, every NPC, and the full codex remain.
        // Current player card is archived to settings.playerArchive (with
        // images converted to nostalgic text blurbs and discarded) so
        // future runs can reference past beings.
        $('#img-gen-restart-story').on('click', async function () {
            // v2.10.0 — No confirm dialogs. The 5-second overlay
            // countdown in handleRunEnd is the visual gate; a misfire
            // can be aborted with a page refresh during the countdown.
            // Keeping the action single-click also lets the UI-parity
            // tests drive it deterministically.
            try {
                await handleRunEnd('end-story', {
                    title: '⟲ the story ends',
                    subtitle: 'the world remembers — another being departs',
                });
            } catch (err) {
                console.error('[Image Generator] End-Story failed:', err);
            }
        });
        // v2.6.2 — Hard "Reset World" button. Full wipe: remnantMemory,
        // playerArchive, NPCs, codex, images — all gone. Re-seeds The
        // Remnant, The Fortress, and The Fold so the fresh world still
        // has its two permanent residents and starter item.
        $('#img-gen-end-story').on('click', async function () {
            // v2.10.0 — No confirm dialogs (see End-Story note above).
            try {
                await handleRunEnd('reset-world', {
                    title: '☢ the world resets',
                    subtitle: 'the remnant forgets — a universe disintegrates',
                });
            } catch (err) {
                console.error('[Image Generator] Reset-World failed:', err);
            }
        });
        // v2.6.0 — inline top-bar toggle (tucked into panel header so it
        // never blocks chat text). Mirrors the floating chevron but out
        // of the way.
        $('#img-gen-topbar-toggle-inline').on('click', function () {
            const s = initSettings();
            s.topBarHidden = !s.topBarHidden;
            saveSettingsDebounced();
            $('body').toggleClass('img-gen-topbar-hidden', !!s.topBarHidden);
            $(this).text(s.topBarHidden ? '▼' : '▲');
            $(this).attr('title', s.topBarHidden ? 'Show top menu bar' : 'Hide top menu bar');
        });
        // Reflect initial state.
        try {
            const s = initSettings();
            $('#img-gen-topbar-toggle-inline').text(s.topBarHidden ? '▼' : '▲');
        } catch (_) { /* ignore */ }
    }
}

// v2.6.0 — secret phrase that wipes The Remnant's permanent ledger.
// The ONLY way to clear remnantMemory. Intentionally undocumented in UI.
// Intercepted client-side: if the user sends a message matching the
// phrase, we show a two-step confirm and never let the LLM see it.
const SECRET_FORGET_PHRASE_RE = /^\s*remnant\s*,?\s*forget\s+everyone\s+you\s+have\s+played\s+with\s*[.!?]*\s*$/i;
function installSecretForgetPhraseInterceptor() {
    // Hook the send form. Capture-phase listener so we run before ST.
    const $form = $('#send_form');
    if ($form.length === 0) {
        // Send form not yet in DOM — retry shortly.
        setTimeout(installSecretForgetPhraseInterceptor, 500);
        return;
    }
    if ($form.data('img-gen-forget-bound')) return;
    $form.data('img-gen-forget-bound', true);

    const tryIntercept = (ev) => {
        const $input = $('#send_textarea');
        const text = ($input.val() || '').toString();
        if (!SECRET_FORGET_PHRASE_RE.test(text)) return false;
        // Match — halt the send.
        ev.preventDefault();
        ev.stopPropagation();
        ev.stopImmediatePropagation && ev.stopImmediatePropagation();

        const ok1 = window.confirm("This will permanently erase The Remnant's memory of every being it has ever borrowed. This cannot be undone. Are you sure?");
        if (!ok1) { $input.val(''); return true; }
        const ok2 = window.confirm('Final check: forget every being, forever?');
        if (!ok2) { $input.val(''); return true; }

        const settings = initSettings();
        settings.remnantMemory = { abductions: [] };
        saveSettingsDebounced();
        try { applyRemnantMemoryPrompt(); } catch (_) { /* ignore */ }
        $input.val('');
        try { $input.trigger('input'); } catch (_) { /* ignore */ }
        console.log('[Image Generator] remnantMemory wiped by secret phrase.');

        // Briefly notify in-chat via status line (no LLM call).
        try { updatePanelStatus('✦ The Remnant forgets.'); setTimeout(() => updatePanelStatus(''), 3500); } catch (_) { /* ignore */ }
        return true;
    };

    // Submit path.
    $form.on('submit.imgGenForget', tryIntercept);
    // Enter-key path (ST sends on Enter without submit in some configs).
    $('#send_textarea').on('keydown.imgGenForget', function (ev) {
        if (ev.key !== 'Enter' || ev.shiftKey) return;
        tryIntercept(ev);
    });
    // Send button path.
    $('#send_but').on('click.imgGenForget', tryIntercept);
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

    // Mirror the most recent LOCATION image (not the currently-selected
    // thumb) to the ST chat background. Subject close-ups stay in the
    // gallery strip but never become the room backdrop. If nothing in
    // the gallery is tagged as a location, clear our override so ST's
    // default background shows through.
    let backdropUrl = null;
    for (let i = images.length - 1; i >= 0; i--) {
        if (images[i] && images[i].kind === 'location') {
            backdropUrl = images[i].image;
            break;
        }
    }
    updateBackgroundWallpaper(backdropUrl);

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
// v2.1.0 — Mode bar (Say / Do) + narrator rename
// ---------------------------------------------------------------------------

/**
 * Walk all visible "Narrator" ch_name spans and set their text to
 * "The Fortress". The CSS ::after rule is the instant fallback; this
 * sets the actual text node so it's clean in the DOM.
 */
function fixNarratorNames() {
    document.querySelectorAll('.mes[ch_name="Narrator"] .name_text').forEach(el => {
        if (el.textContent.trim() !== 'The Fortress') {
            el.textContent = 'The Fortress';
        }
    });
}

/**
 * Inject the Say / Do / Sense mode bar + channel history drawer above
 * the send form. Also relocates QR action buttons (End Story / Reset World)
 * into the bar so they're accessible without the QR clutter.
 *
 * v2.3.0: Say/Do/Sense each open a history drawer when clicked.
 */
// ---------------------------------------------------------------------------
// Tab cycling, font zoom, resize helpers (shared across mode bar + keyboard)
// ---------------------------------------------------------------------------

const _CYCLE_ORDER = ['say', 'do', 'sense', 'insights', 'inventory', 'lore'];

function _cycleTab(dir) {
    const $bar = $('#img-gen-mode-bar');
    const cur  = _CYCLE_ORDER.indexOf(_activeDrawer || 'say');
    const next = _CYCLE_ORDER[(cur + dir + _CYCLE_ORDER.length) % _CYCLE_ORDER.length];
    $bar.find('.img-gen-mode-btn:not(.img-gen-qr-btn)').removeClass('active');
    $bar.find(`.img-gen-mode-btn[data-mode="${next}"]`).addClass('active');
    $('body').removeClass('img-gen-say-mode img-gen-do-mode img-gen-sense-mode img-gen-insights-mode img-gen-inventory-mode img-gen-lore-mode');
    $('body').addClass(`img-gen-${next}-mode`);
    _updateInputPlaceholder(next);
    toggleChannelDrawer(next);
}

function _applyUiZoom(scale) {
    const clamped = Math.max(0.7, Math.min(1.5, +scale || 1));
    document.documentElement.style.setProperty('--img-gen-zoom', clamped);
    const settings = initSettings();
    settings.uiZoom = clamped;
    saveSettingsDebounced();
}

function _resizeDrawer() {
    const winH   = window.innerHeight;
    const bar    = document.getElementById('img-gen-mode-bar');
    const form   = document.getElementById('send_form');
    const drawer = document.getElementById('img-gen-channel-drawer');
    const panel  = document.getElementById('image-generator-panel');
    const sheld  = document.getElementById('sheld');
    const barH   = bar  ? bar.offsetHeight  : 44;
    const formH  = form ? form.offsetHeight : 60;
    const h      = Math.max(120, winH - barH - formH - 20);
    if (drawer) drawer.style.height = h + 'px';
    // Sync #sheld right margin to the panel's actual rendered width so the
    // two never drift regardless of viewport width or zoom level.
    if (panel && sheld) sheld.style.marginRight = (panel.offsetWidth + 8) + 'px';
}

// v2.12.1 — Language-agnostic placeholder. Uses punctuation / symbols
// to suggest the current mode without words. ST resets the placeholder
// on every chat load, so this must be re-applied from onChatChanged too.
const _PLACEHOLDER = { say: '…', do: '*  *', sense: '⊹', insights: '∿', inventory: '…', lore: '…' };
function _updateInputPlaceholder(mode) {
    const current = mode || (
        $('body').hasClass('img-gen-do-mode')       ? 'do'       :
        $('body').hasClass('img-gen-sense-mode')    ? 'sense'    :
        $('body').hasClass('img-gen-insights-mode') ? 'insights' : 'say'
    );
    $('#send_textarea').attr('placeholder', _PLACEHOLDER[current] || '…');
}

function installModeBar() {
    if ($('#img-gen-mode-bar').length) return;

    // --- Channel drawer (permanent narrative panel — always open) ---
    const $drawer = $('<div id="img-gen-channel-drawer"></div>');
    $drawer.append(
        '<div id="img-gen-drawer-header">' +
            '<span id="img-gen-drawer-label">Say</span>' +
        '</div>' +
        '<div id="img-gen-drawer-content"></div>'
    );

    // --- Mode bar ---
    const $bar      = $('<div id="img-gen-mode-bar"></div>');
    const $say       = $('<button class="img-gen-mode-btn active" data-mode="say">Say</button>');
    const $do        = $('<button class="img-gen-mode-btn" data-mode="do">Do</button>');
    const $sense     = $('<button class="img-gen-mode-btn" data-mode="sense">Sense</button>');
    const $insights  = $('<button class="img-gen-mode-btn" data-mode="insights">Insights</button>');
    const $inventory = $('<button class="img-gen-mode-btn" data-mode="inventory">🎒 Inventory</button>');
    const $lore      = $('<button class="img-gen-mode-btn" data-mode="lore">📖 Lore</button>');

    // --- DEV: quick-fire test button ---
    // Asks Ollama (mistral) for a short human phrase in context, then submits
    // it as the player's next message. Hidden in production via CSS class.
    const $testBtn = $('<button class="img-gen-mode-btn img-gen-test-btn" title="Dev: fire a random player line via Ollama">🎲</button>');
    $testBtn.on('click', async function () {
        $testBtn.prop('disabled', true).text('…');
        try {
            const location = (initSettings().currentLocation || 'an unknown place');
            const resp = await fetch('/api/ollama/api/generate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    model: 'mistral',
                    prompt: `You are a player character in a dark fantasy game. You are currently in: ${location}. Write ONE short, natural thing the player character might say or do — 5 to 15 words, first person, no quotes, no narration. Just the line.`,
                    stream: false,
                }),
            });
            const data = await resp.json();
            const line = (data.response || '').trim().replace(/^["']|["']$/g, '');
            if (line) {
                $('#send_textarea').val(line);
                // Trigger send
                $('#send_textarea').closest('form').trigger('submit');
            }
        } catch (err) {
            console.warn('[Image Generator] test-btn Ollama call failed:', err);
        } finally {
            $testBtn.prop('disabled', false).text('🎲');
        }
    });

    $bar.append($say, $do, $sense, $insights, $inventory, $lore, $testBtn);
    $('#send_form').before($bar);
    $bar.before($drawer);

    _updateInputPlaceholder();
    _resizeDrawer();

    // Keep #sheld margin in sync with the gallery panel's actual width
    // whenever the viewport resizes or zoom changes.
    const _panelEl = document.getElementById('image-generator-panel');
    if (window.ResizeObserver && _panelEl) {
        new ResizeObserver(_resizeDrawer).observe(_panelEl);
    }

    // --- Mode switch + drawer toggle ---
    $bar.on('click', '.img-gen-mode-btn:not(.img-gen-qr-btn)', function () {
        const mode = $(this).data('mode');
        // Switch active mode
        $bar.find('.img-gen-mode-btn:not(.img-gen-qr-btn)').removeClass('active');
        $(this).addClass('active');
        $('body').removeClass('img-gen-say-mode img-gen-do-mode img-gen-sense-mode img-gen-insights-mode img-gen-inventory-mode img-gen-lore-mode img-gen-look-mode');
        $('body').addClass(`img-gen-${mode}-mode`);
        _updateInputPlaceholder(mode);
        // Toggle drawer
        toggleChannelDrawer(mode);
    });

    // No close button — the drawer is permanently visible.

    // --- Mouse wheel cycles tabs (drawer or chat input box) ---
    $drawer.on('wheel', function (e) {
        e.preventDefault();
        _cycleTab(e.originalEvent.deltaY > 0 ? 1 : -1);
    });
    $('#send_form').on('wheel.imgGenTabs', function (e) {
        e.preventDefault();
        _cycleTab(e.originalEvent.deltaY > 0 ? 1 : -1);
    });

    // --- Send intercept — transform text + capture for channel history ---
    function _applyModeTransform() {
        const $ta = $('#send_textarea');
        let val = $ta.val().trim();
        if (!val) return;

        let channel = 'say';
        if ($('body').hasClass('img-gen-do-mode')) {
            channel = 'do';
            val = val.replace(/^\*+/, '').replace(/\*+$/, '').trim();
            $ta.val(`*${val}*`);
        } else if ($('body').hasClass('img-gen-sense-mode')) {
            channel = 'sense';
            $ta.val(`*focuses their senses* ${val}`);
        } else if ($('body').hasClass('img-gen-insights-mode')) {
            channel = 'insights';
            val = val.replace(/^\*+/, '').replace(/\*+$/, '').trim();
            $ta.val(`*${val}*`);
        }
        // Capture raw text for channel history (consumed in USER_MESSAGE_RENDERED)
        _pendingUserEntry = { text: val, channel, isPlayer: true };
    }
    $(document).off('submit.imgGenMode').on('submit.imgGenMode', '#send_form', _applyModeTransform);
    $(document).off('keydown.imgGenMode').on('keydown.imgGenMode', '#send_textarea', function (e) {
        if (e.key !== 'Enter' || e.shiftKey) return;
        _applyModeTransform();
    });

    // Harvest QR action buttons with retries (they mount asynchronously)
    _harvestQrButtons($bar, 0);

    // Nuclear QR bar hide
    setTimeout(() => {
        const _QR_NAV_LABELS = ['API Connections', 'Character Management', 'Extensions'];
        _QR_NAV_LABELS.forEach(label => {
            $('button, a').filter(function () {
                return $(this).text().trim() === label;
            }).each(function () {
                $(this).closest('div').not('#send_form, #chat, body, #image-generator-panel').first().hide();
            });
        });
    }, 1500);

    // Boot-time channel population from history, then auto-open Say drawer
    // so the player immediately sees the last thing The Fortress said.
    setTimeout(() => {
        try { _populateChannelsFromHistory(); } catch (_) { /* ignore */ }
        try { if (!_activeDrawer) toggleChannelDrawer('say'); } catch (_) { /* ignore */ }
    }, 2000);
}

// ---------------------------------------------------------------------------
// v2.3.0 — Channel history system (Say / Do / Sense)
// Each mode has a history drawer that logs player inputs and narrator output.
// ---------------------------------------------------------------------------

const _MAX_CH_ENTRIES = 100;
const _channelHistory  = { say: [], do: [], sense: [], insights: [] };
let   _activeDrawer    = null;   // 'say' | 'do' | 'sense' | 'insights' | null
const _channelHydrated = new Set(); // mesids already parsed into channels
let   _hydratedChatId  = null;   // ST chat_id when _channelHydrated was last built
let   _pendingUserEntry = null;  // { text, channel, isPlayer:true } set in _applyModeTransform
let   _senseMiniLast   = {};     // { type: label } — guards sense button re-render

// Reveal queue — new message entries are staggered for dramatic cadence
const _revealQueue  = [];
let   _revealTimer  = null;
const _REVEAL_MS    = 320;  // ms between successive channel entries

function _drainRevealQueue() {
    if (!_revealQueue.length) { _revealTimer = null; return; }
    const { channel, entry } = _revealQueue.shift();
    addChannelEntry(channel, entry);
    _revealTimer = setTimeout(_drainRevealQueue, _REVEAL_MS);
}

function _queueChannelEntry(channel, entry) {
    _revealQueue.push({ channel, entry });
    if (!_revealTimer) _revealTimer = setTimeout(_drainRevealQueue, _REVEAL_MS);
}

function _clearRevealQueue() {
    _revealQueue.length = 0;
    if (_revealTimer) { clearTimeout(_revealTimer); _revealTimer = null; }
}

const _CHANNEL_LABELS = { say: 'Say', do: 'Do', sense: 'Sense', insights: 'Insights', inventory: 'Inventory', lore: 'Lore' };
// v2.12.1 — Sense/Insights split. Traditional sensory perception (sight,
// sound, smell, taste) goes to Sense. Physical-spatial and felt knowledge
// (touch, environment, Fortress whispers) goes to Insights.
const _INSIGHTS_SENSE_TYPES = new Set(['ENVIRONMENT', 'TOUCH']);
function _senseChannelFor(type) {
    return _INSIGHTS_SENSE_TYPES.has(type) ? 'insights' : 'sense';
}

/**
 * Push one entry into a channel's history ring buffer.
 * If the drawer for that channel is open, re-render it immediately.
 * Otherwise, badge the button with an unread dot.
 */
function addChannelEntry(channel, entry) {
    const arr = _channelHistory[channel];
    if (!arr) return;
    // No filtering — every entry that reaches here goes into its drawer.
    // Sorting (which drawer) is done upstream in _translateToBlocks.
    entry.ts = Date.now();
    entry.channel = channel;  // stamp so merged render knows the type
    arr.push(entry);
    if (arr.length > _MAX_CH_ENTRIES) arr.splice(0, arr.length - _MAX_CH_ENTRIES);

    if (_activeDrawer === channel) {
        _renderDrawer(channel);
    } else {
        $(`#img-gen-mode-bar .img-gen-mode-btn[data-mode="${channel}"]`).addClass('has-unread');
    }
}

/**
 * Render the full codex for inventory or lore into the drawer content area.
 * Reads directly from settings.codex — not a ring buffer.
 */
function _renderCodexDrawer(which, $content) {
    const settings = initSettings();
    const bag = which === 'inventory'
        ? (settings.codex && settings.codex.items || {})
        : (settings.codex && settings.codex.lore  || {});
    const entries = Object.values(bag).sort((a, b) =>
        Date.parse(a.first_seen || 0) - Date.parse(b.first_seen || 0));

    if (entries.length === 0) {
        $content.append('<div class="img-gen-drawer-empty">Nothing discovered yet.</div>');
        return;
    }
    for (const e of entries) {
        const $row = $('<div class="img-gen-drawer-entry img-gen-codex-drawer-entry"></div>');
        $row.addClass(which);
        const $name = $('<div class="img-gen-codex-drawer-name"></div>').text(e.name || '');
        $row.append($name);
        if (e.description) {
            const $desc = $('<div class="img-gen-codex-drawer-desc"></div>').text(e.description);
            $row.append($desc);
        }
        $content.append($row);
    }
    $content.scrollTop($content[0].scrollHeight);
}

/**
 * Render the entries for `channel` into the open drawer.
 */
function _renderDrawer(channel) {
    const $content = $('#img-gen-drawer-content');
    if (!$content.length) return;
    $content.empty();

    // Inventory + Lore are full-codex reference views — scrollable, top-down
    if (channel === 'inventory' || channel === 'lore') {
        $content.addClass('img-gen-codex-active');
        _renderCodexDrawer(channel, $content);
        return;
    }
    $content.removeClass('img-gen-codex-active');

    // Narrative channels (say/do/sense/insights) render as a single unified
    // timeline so the story reads as one stream. Each entry carries its own
    // channel/senseType for coloring.
    const NARRATIVE_CHANNELS = ['say', 'do', 'sense', 'insights'];
    let all;
    if (NARRATIVE_CHANNELS.includes(channel)) {
        all = NARRATIVE_CHANNELS.flatMap(ch => _channelHistory[ch] || []);
        all.sort((a, b) => (a.ts || 0) - (b.ts || 0));
    } else {
        all = _channelHistory[channel] || [];
    }

    if (all.length === 0) {
        $content.append('<div class="img-gen-drawer-empty">Nothing yet.</div>');
        return;
    }
    const display = all.slice(-60);

    // Narrator label: character name from SillyTavern, fallback to "—"
    const narratorName = (characters && this_chid !== undefined && characters[this_chid])
        ? (characters[this_chid].name || '—')
        : '—';

    // Render oldest-first (reading order). Newest entry is last in DOM.
    // scrollTop to scrollHeight pins the view to the bottom so new entries
    // are always visible; old entries scroll off the top.
    for (let i = 0; i < display.length; i++) {
        const e = display[i];
        const entryChannel = e.channel || channel;
        let cls = entryChannel;
        if (e.senseType) cls += ` sense sense-${e.senseType.toLowerCase()}`;
        if (e.isPlayer) cls += ' is-player';
        // Newest entry = last in DOM — animate arrival
        if (i === display.length - 1) cls += ' arriving';
        const speakerLabel = e.isPlayer ? 'You' : narratorName;
        const $row = $(`<div class="img-gen-drawer-entry ${cls}"><span class="img-gen-drawer-speaker"></span><span class="img-gen-drawer-text"></span></div>`);
        $row.find('.img-gen-drawer-speaker').text(speakerLabel);
        $row.find('.img-gen-drawer-text').text(e.text);
        $content.append($row);
    }
    // Pin scroll to bottom so newest entry is always visible
    const el = $content[0];
    if (el) el.scrollTop = el.scrollHeight;
}

/**
 * Toggle the drawer for `channel`. Sets input mode at the same time.
 */
// v2.12.1 — Drawer is permanent; this only switches the visible channel.
function toggleChannelDrawer(channel) {
    const $drawer = $('#img-gen-channel-drawer');
    const $bar    = $('#img-gen-mode-bar');
    $bar.find(`.img-gen-mode-btn[data-mode="${channel}"]`).removeClass('has-unread');
    _activeDrawer = channel;
    $drawer.addClass('open').attr('data-channel', channel);
    $bar.find('.img-gen-mode-btn').removeClass('drawer-open');
    $bar.find(`.img-gen-mode-btn[data-mode="${channel}"]`).addClass('drawer-open');
    $('#img-gen-drawer-label').text(_CHANNEL_LABELS[channel] || channel);
    _renderDrawer(channel);
}

/**
 * Translate raw narrator text into an ordered array of typed channel blocks.
 *
 * Single-pass scanner — handles all block types in document order:
 *   [SAY]...[/SAY]          → say channel
 *   [DO]...[/DO]            → do channel
 *   [SIGHT: "desc"]         → sense channel
 *   [SOUND/SMELL/TASTE: ""] → sense channel
 *   [TOUCH/ENVIRONMENT: ""] → insights channel
 *   *legacy stage direction* → do channel (fallback)
 *   remaining prose          → say channel (fallback)
 *
 * System-only markers (INTRODUCE, ITEM, LORE, PLAYER_TRAIT, etc.) are
 * silently skipped here — they are handled by the detectSenseMarkers pipeline.
 *
 * Returns: Array<{ channel, text, senseType?, icon? }>
 */
const _DISPLAY_SENSE_TYPES = new Set(['SIGHT', 'SOUND', 'SMELL', 'TASTE', 'TOUCH', 'ENVIRONMENT']);

function _translateToBlocks(rawText) {
    const htmlCut = rawText.indexOf('<');
    const safe = (htmlCut !== -1 ? rawText.substring(0, htmlCut) : rawText).substring(0, 2000);

    const result = [];

    // Tokenizes ALL markup in one pass. Groups:
    //  1  [SAY]...[/SAY]
    //  2  [DO]...[/DO]
    //  3,4  [TYPE: "quoted desc"]
    //  5,6  [TYPE: unquoted desc]
    //  7  *legacy stage direction*
    const tokenRe = /\[SAY\]([\s\S]*?)\[\/SAY\]|\[DO\]([\s\S]*?)\[\/DO\]|\[([A-Z_]+)(?:\([^)]*\))?\s*:\s*"([^"]+?)"\]|\[([A-Z_]+)(?:\([^)]*\))?\s*:\s*([^\]]+?)\]|\*([^*\n]+?)\*/g;

    // Strip any bracket marker that the tokenizer consumed but didn't route,
    // so it can never bleed into a SAY prose entry.
    const _stripMarkers = (s) => s.replace(/\[[A-Z_]+(?:\([^)]*\))?\s*:[^\]]+\]/g, '').replace(/\s{2,}/g, ' ').trim();

    let lastIdx = 0;
    let m;
    while ((m = tokenRe.exec(safe)) !== null) {
        // Prose between tokens → SAY (markers stripped as safety net)
        if (m.index > lastIdx) {
            const prose = _stripMarkers(safe.slice(lastIdx, m.index));
            if (prose.length > 8) result.push({ channel: 'say', text: prose });
        }

        if (m[1] !== undefined) {
            // [SAY]...[/SAY]
            const text = m[1].replace(/\s{2,}/g, ' ').trim();
            if (text.length > 4) result.push({ channel: 'say', text });
        } else if (m[2] !== undefined) {
            // [DO]...[/DO]
            const text = m[2].replace(/\s{2,}/g, ' ').trim();
            if (text.length > 4) result.push({ channel: 'do', text });
        } else if (m[3] !== undefined) {
            // [TYPE: "quoted"]
            const type = m[3], desc = m[4].trim();
            if (_DISPLAY_SENSE_TYPES.has(type) && desc.length > 2) {
                result.push({ channel: _senseChannelFor(type), text: desc, senseType: type, icon: SENSE_ICONS[type] || '•' });
            }
        } else if (m[5] !== undefined) {
            // [TYPE: unquoted]
            const type = m[5], desc = (m[6] || '').trim();
            if (_DISPLAY_SENSE_TYPES.has(type) && desc.length > 2) {
                result.push({ channel: _senseChannelFor(type), text: desc, senseType: type, icon: SENSE_ICONS[type] || '•' });
            }
        } else if (m[7] !== undefined) {
            // *legacy stage direction* → DO
            const text = m[7].trim();
            if (text.length > 4) result.push({ channel: 'do', text });
        }

        lastIdx = m.index + m[0].length;
    }

    // Trailing prose → SAY (markers stripped as safety net)
    if (lastIdx < safe.length) {
        const prose = _stripMarkers(safe.slice(lastIdx));
        if (prose.length > 8) result.push({ channel: 'say', text: prose });
    }

    // Fallback: if the tokenizer produced zero blocks for a non-empty
    // response, the narrator used an unrecognized format. Route the full
    // stripped text to say so nothing is silently dropped.
    if (result.length === 0) {
        const fallback = _stripMarkers(safe).replace(/\[\/?(SAY|DO)\]/gi, '').replace(/\s{2,}/g, ' ').trim();
        if (fallback.length > 4) result.push({ channel: 'say', text: fallback });
    }

    return result;
}

/**
 * Parse narrator text into channel blocks and push to the reveal queue (live
 * messages) or directly to channel history (instant=true, used by history load).
 * Guards against double-processing via _channelHydrated.
 */
function _detectNarratorWarnings(rawText, blocks) {
    const w = [];
    // Narrator impersonated the player: asterisked *you verb* or *You verb*
    const impMatches = rawText.match(/\*\s*[Yy]ou\s+\w+/g);
    if (impMatches) w.push({ code: 'player_impersonation', examples: impMatches.slice(0, 5) });
    // No image trigger this turn (not always wrong, but worth flagging)
    if (!/\[GENERATE_IMAGE|\[SIGHT/i.test(rawText)) w.push({ code: 'no_image_trigger' });
    // Overly long (first 2000 chars survive _translateToBlocks; rest is invisible)
    if (rawText.length > 2500) w.push({ code: 'response_too_long', chars: rawText.length });
    return w;
}

async function _postNarratorTurn(messageId, rawText, blocks) {
    try {
        const warnings = _detectNarratorWarnings(rawText, blocks);
        const markers = [];
        const markerRe = /\[([A-Z_]{2,})(?:[:(][^\]]*)?]/g;
        let m;
        while ((m = markerRe.exec(rawText)) !== null) {
            if (!markers.includes(m[1])) markers.push(m[1]);
        }
        const channelCounts = {};
        for (const b of blocks) channelCounts[b.channel] = (channelCounts[b.channel] || 0) + 1;
        const settings = initSettings();
        const turn = {
            ts: Date.now(),
            turn_id: String(messageId),
            raw_text: rawText.substring(0, 3000),
            parsed_blocks: blocks.map(b => ({ channel: b.channel, text: b.text.substring(0, 300), senseType: b.senseType || null })),
            markers_found: markers,
            channel_counts: channelCounts,
            warnings,
            context: {
                location: (settings.currentLocation || '').substring(0, 200),
                player_name: settings.player?.profile?.name || null,
                codex_items: Object.keys(settings.codex?.items || {}).length,
            },
        };
        const ctrl = new AbortController();
        setTimeout(() => ctrl.abort(), 3000);
        await fetch('/diagnostics/narrator-turn', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(turn),
            signal: ctrl.signal,
        });
    } catch (_) { /* fire-and-forget — diag errors must never affect gameplay */ }
}

function _parseNarratorIntoChannels(messageId, rawText, instant = false) {
    if (_channelHydrated.has(messageId)) return;
    _channelHydrated.add(messageId);
    const blocks = _translateToBlocks(rawText);
    const push = instant ? addChannelEntry : _queueChannelEntry;
    for (const b of blocks) {
        push(b.channel, { text: b.text, senseType: b.senseType, icon: b.icon });
    }
    if (!instant) _postNarratorTurn(messageId, rawText, blocks).catch(() => {});
}

/**
 * Walk the last N messages in chat and rebuild _channelHistory from scratch.
 * Called on chat load / onChatChanged so the drawers have context immediately.
 *
 * Sense entries are pushed directly into the ring buffer (bypassing addChannelEntry
 * and its senseMiniLast guard) so the last syncSenseButton call can update the
 * button icons without creating duplicates.
 */
function _populateChannelsFromHistory() {
    // Flush any queued reveal entries — they were enqueued by the live
    // onCharacterMessageRendered path and will be re-added below via instant load.
    _clearRevealQueue();
    _channelHistory.say.length      = 0;
    _channelHistory.do.length       = 0;
    _channelHistory.sense.length    = 0;
    _channelHistory.insights.length = 0;
    // Clear _channelHydrated only when the chat actually changed.
    // Keeping it across calls within the same chat prevents walkAll
    // re-renders from re-processing already-hydrated live messages.
    const currentChatId = (typeof chat_id !== 'undefined' ? chat_id : null)
        || (Array.isArray(chat) && chat[0] ? chat[0].send_date : null);
    if (currentChatId !== _hydratedChatId) {
        _channelHydrated.clear();
        _hydratedChatId = currentChatId;
    }
    _senseMiniLast = {};

    if (!Array.isArray(chat)) return;
    const start = Math.max(0, chat.length - 60);
    for (let i = start; i < chat.length; i++) {
        const msg = chat[i];
        if (!msg || !msg.mes) continue;

        if (msg.is_user) {
            // Classify user messages by text shape, push directly (no queue on history load)
            const t = msg.mes.trim();
            let channel = 'say';
            if (/^\*[^*].+[^*]\*$/.test(t)) channel = 'do';
            else if (/^\*focuses their senses/i.test(t)) channel = 'sense';
            const arr = _channelHistory[channel];
            arr.push({ ts: Date.now(), text: t.replace(/^\*|\*$/g, '').trim(), isPlayer: true });
            if (arr.length > _MAX_CH_ENTRIES) arr.splice(0, arr.length - _MAX_CH_ENTRIES);
        } else {
            // Narrator: translator handles ALL channel routing (SAY, DO, and all sense channels).
            // instant=true bypasses the reveal queue — history loads without animation delay.
            _parseNarratorIntoChannels(i, msg.mes, true);
        }
    }

    // Update sense button icons from the most recent narrator message with markers.
    for (let i = chat.length - 1; i >= start; i--) {
        const msg = chat[i];
        if (msg && !msg.is_user && msg.mes) {
            const markers = detectSenseMarkers(msg.mes);
            if (markers.length) { syncSenseButton(i, markers); break; }
        }
    }
}

// ---------------------------------------------------------------------------
// Sense mini-icons on the Sense button (replaces the standalone bottom bar)
// ---------------------------------------------------------------------------

/**
 * Update the mini sense icons inside the Sense button from the most-recent
 * narrator message. Also adds sense entries to the Sense channel history.
 * Replaces the old `syncBottomSenses` bottom bar update.
 */
function syncSenseButton(messageId, markers) {
    // Only update from the latest narrator message
    let lastNarratorId = -1;
    if (Array.isArray(chat)) {
        for (let i = chat.length - 1; i >= 0; i--) {
            if (chat[i] && !chat[i].is_user) { lastNarratorId = i; break; }
        }
    }
    if (messageId !== lastNarratorId) return;

    const byType = {};
    for (const mk of markers) {
        if (!SENSE_BAR_TYPES.has(mk.type)) continue;
        if (!mk.description) continue;
        (byType[mk.type] ||= []).push(mk);
    }
    if (Object.keys(byType).length === 0) return;

    // Channel entries for sense data are handled by _translateToBlocks in document order.
    // syncSenseButton is responsible for mode-bar icon updates only.

    // Rebuild mini icons in Sense button (perceptual) and Insights button (felt/spatial)
    const $senseBtn    = $('#img-gen-mode-bar .img-gen-mode-btn[data-mode="sense"]');
    const $insightsBtn = $('#img-gen-mode-bar .img-gen-mode-btn[data-mode="insights"]');
    if (!$senseBtn.length) return;
    $senseBtn.find('.img-gen-sense-mini-icons').empty();
    $insightsBtn.find('.img-gen-sense-mini-icons').empty();

    let anyNewSense = false;
    let anyNewInsights = false;
    const nextLast = {};
    for (const type of ['SIGHT', 'SMELL', 'SOUND', 'TASTE', 'TOUCH', 'ENVIRONMENT']) {
        const entries = byType[type];
        if (!entries || !entries.length) continue;
        const icon    = SENSE_ICONS[type] || '•';
        const label   = entries.map(e => e.description).join(' / ');
        const isNew   = _senseMiniLast[type] !== label;
        const isInsights = _INSIGHTS_SENSE_TYPES.has(type);
        const $target = isInsights
            ? $insightsBtn.find('.img-gen-sense-mini-icons')
            : $senseBtn.find('.img-gen-sense-mini-icons');
        if (isNew) { if (isInsights) anyNewInsights = true; else anyNewSense = true; }
        $target.append(
            $(`<span class="img-gen-sense-mini sense-${type.toLowerCase()}" title="${escapeHtml(label)}">${icon}</span>`)
        );
        nextLast[type] = label;
    }
    _senseMiniLast = nextLast;

    if (anyNewSense && $senseBtn.length) {
        $senseBtn.removeClass('sense-flash');
        void $senseBtn[0].offsetWidth;
        $senseBtn.addClass('sense-flash');
    }
    if (anyNewInsights && $insightsBtn.length) {
        $insightsBtn.removeClass('sense-flash');
        void $insightsBtn[0].offsetWidth;
        $insightsBtn.addClass('sense-flash');
    }
}

// Keep the old name alive as an alias so existing call sites still work
function syncBottomSenses(messageId, markers) {
    syncSenseButton(messageId, markers);
}

const _QR_HARVEST_LABELS = ['End Story', 'Reset World'];
const _QR_HARVEST_RETRIES = [600, 1800, 3500, 7000];

function _harvestQrButtons($bar, attempt) {
    let harvested = 0;
    _QR_HARVEST_LABELS.forEach(label => {
        if ($bar.find(`[data-qr-label="${CSS.escape(label)}"]`).length) {
            harvested++;
            return; // already harvested
        }
        const $src = $('button:not(.img-gen-mode-btn)').filter(function () {
            // Use includes() to tolerate icon prefixes like "⊕ End Story"
            return $(this).text().trim().includes(label);
        }).first();
        if (!$src.length) return;

        if (harvested === 0) {
            $bar.append($('<span class="img-gen-mode-sep"></span>'));
        }
        const $btn = $(`<button class="img-gen-mode-btn img-gen-qr-btn" data-qr-label="${label}">${label}</button>`);
        $btn.on('click', () => $src.trigger('click'));
        $bar.append($btn);
        // Hide the source button's container (QR bar row)
        $src.closest('#qr--bar, .qr--bar, .quickReplyBar, #quickReplyBar, .qr--bar-container').hide();
        $src.hide();
        harvested++;
    });

    if (harvested < _QR_HARVEST_LABELS.length && attempt < _QR_HARVEST_RETRIES.length) {
        setTimeout(() => _harvestQrButtons($bar, attempt + 1), _QR_HARVEST_RETRIES[attempt]);
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

    // Strip any `Aaron: "..."` dialogue lines the narrator produced for
    // the player character. This mutates the stored chat entry so the
    // LLM doesn't see its own prior Aaron-dialogue on the next turn and
    // treat it as license to continue the pattern.
    const scrubbed = scrubPlayerDialogue(message.mes);
    if (scrubbed !== message.mes) {
        console.log('[Image Generator] Scrubbed player dialogue from narrator response (mesid=' + messageId + ')');
        message.mes = scrubbed;
        try { if (typeof saveChatDebounced === 'function') saveChatDebounced(); } catch (_) { /* ignore */ }
    }

    // Re-render the message display: DOM transforms, sense bar. Always runs.
    updateMessageDisplay(messageId);
    try { fixNarratorNames(); } catch (_) { /* ignore */ }

    // Route narrator text into the channel drawer. Called here explicitly
    // (not inside updateMessageDisplay) so walkAll re-renders don't queue
    // duplicate entries that race with _populateChannelsFromHistory.
    try { _parseNarratorIntoChannels(messageId, message.mes); } catch (_) { /* ignore */ }

    // v2.6.3 — Apply spoken cadence AFTER the final transform runs.
    // This is the one-shot moment at stream end; doing it here (instead
    // of inside updateMessageDisplay) means the observer-driven mid-
    // stream re-renders don't keep restarting the stagger animation.
    try {
        const $mes = $(`#chat .mes[mesid="${messageId}"]`);
        applyDialogueCadence($mes);
    } catch (_) { /* ignore */ }

    // v2.7.1 — first narrator turn of a run consumes the "Who are you,
    // being?" ritual. Flip the flag the first time we see any narrator
    // message in this run, so opening a new ST chat later (or a reload)
    // does not re-trigger the ritual in the prompt gate.
    try {
        if (settings.run && !settings.run.ritual_asked) {
            settings.run.ritual_asked = true;
            saveSettingsDebounced();
            try { applyRemnantMemoryPrompt(); } catch (_) { /* ignore */ }
        }
    } catch (_) { /* ignore */ }

    // Gate the full image-generation pipeline. Channel drawer, marker
    // transforms, and sense bar already ran above regardless of this flag.
    if (!settings.autoGenerate) return;

    // v2.6.2 — stall recovery. The narrator sometimes returns a truncated
    // response (token budget hit, stop sequence, connection hiccup). When
    // that happens the message ends mid-sentence with no closing
    // punctuation and usually no closing `]` on any marker — downstream
    // interpreters (handleIntroductions, handlePlayerTrait, scene-image
    // generation) then run on partial text and we stall visually.
    //
    // Heuristic: length > 100, last non-whitespace char not in .!?"')]}
    // and we haven't already auto-continued this mesid once. If triggered,
    // fire ST's Generate('continue') to stitch the rest of the turn onto
    // the same message. The one-shot guard prevents spin if continue also
    // fails.
    try {
        if (!window.__imgGenContinuedFor) window.__imgGenContinuedFor = new Set();
        const continued = window.__imgGenContinuedFor;
        const raw = (message.mes || '').trim();
        const tail = raw.slice(-1);
        const looksTruncated = raw.length > 100 && !/[.!?"'\)\]\}…]/.test(tail);
        if (looksTruncated && !continued.has(messageId) && typeof Generate === 'function') {
            continued.add(messageId);
            console.warn('[Image Generator] Narrator response looks truncated (mesid=' + messageId + ', ends: "' + raw.slice(-40).replace(/\s+/g, ' ') + '"). Auto-continuing…');
            try { updatePanelStatus('⚠ narrator cut off — auto-continuing…'); } catch (_) { /* ignore */ }
            // Defer so ST finishes its own post-render bookkeeping first.
            setTimeout(() => {
                try {
                    Generate('continue');
                } catch (err) {
                    console.error('[Image Generator] auto-continue failed:', err);
                }
            }, 400);
            return; // skip the rest of the pipeline — we'll re-enter on the continued message
        }
    } catch (err) {
        console.warn('[Image Generator] stall-detector error:', err);
    }

    // Every new rendered message is a chance for a freshly-inserted
    // user bubble to be wearing the stale default avatar — slam the
    // locked portrait URL into all user-message avatars.
    try { applyPlayerAvatarToChat(); } catch (_) { /* ignore */ }

    // Kick off NPC introductions in parallel with scene image generation.
    // Introductions don't block scene images — both streams progress
    // independently so the scene shows up fast.
    handleIntroductions(message.mes);

    // Player portrait + NPC appearance updates run in parallel too.
    handlePlayerUpdate(message.mes);
    handleAppearanceUpdates(message.mes);

    // Extract any new ITEM / LORE entries into the codex panel.
    handleCodexEntries(message.mes);

    // v2.6.0 — player identity accrual and in-chat item renames.
    try { handlePlayerTrait(message.mes); } catch (err) { console.warn('[Image Generator] handlePlayerTrait error:', err); }
    try { handleItemRenames(message.mes); } catch (err) { console.warn('[Image Generator] handleItemRenames error:', err); }
    // v2.6.2 — clear the one-shot Fortress-naming nudge after the narrator
    // has had its turn with it, so it never bleeds into subsequent responses.
    try { clearFortressNamingNudge(); } catch (_) { /* ignore */ }

    // If the narrator emitted a run-ending marker (RESET_RUN, RESET_STORY,
    // END_RUN(voluntary), END_RUN(death)), queue the unified run-end
    // sequence after the player has had a moment to read the response.
    const runEndMarker = detectSenseMarkers(message.mes).find(m => m.triggersReset);
    if (runEndMarker) {
        console.log('[Image Generator] Run-end marker detected:', runEndMarker.type, runEndMarker.attribution || '');
        let fate = 'restart';
        let causeOfDeath = null;
        let title, subtitle;
        if (runEndMarker.type === 'END_RUN') {
            const kind = (runEndMarker.attribution || '').toLowerCase();
            if (kind === 'death') {
                fate = 'death';
                causeOfDeath = runEndMarker.description || null;
                title = '⟲ an untimely end';
                subtitle = 'the remnant watches you go';
            } else if (kind === 'voluntary') {
                fate = 'voluntary-home';
                title = '⟲ the portal home';
                subtitle = 'the remnant watches you go';
            }
        }
        setTimeout(() => { handleRunEnd(fate, { causeOfDeath, title, subtitle }); }, 1500);
    }

    // v2.6.6 — Reset the per-turn SD budget at the start of every
    // narrator turn. The budget chokepoint inside callSdApi bails if
    // this turn has already fired SD_PER_TURN_BUDGET calls.
    imgGenBeginTurn();

    // Capture the latest location shot as authoritative "where the
    // player is" state BEFORE kicking off image generation — so even if
    // generation is slow, the next turn already has the location
    // injection applied.
    try { updateLocationFromMessage(message.mes); } catch (_) { /* ignore */ }
    // v2.6.0 — persist the current run snapshot after every rendered turn.
    try { persistRun(); } catch (err) { console.warn('[Image Generator] persistRun error:', err); }
    // v2.12.1 — re-inject story beats after each turn so the injection
    // reflects any beats just recorded this turn.
    try { applyStoryBeatPrompt(); } catch (_) { /* ignore */ }

    // Scene images
    const imageMarkers = detectImageMarkers(message.mes);
    for (const marker of imageMarkers) {
        updatePanelStatus(`⏳ Generating image... "${marker.description.substring(0, 50)}..."`);
        const kind = classifyImageKind(marker.attribution);
        console.log(`[Image Generator] Queue scene (${kind}): ${marker.description.substring(0, 80)}`);

        const imageData = await generateSceneImage(marker.description, kind);
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
// v2.6.5 — Late-hydration observer. Extracted from onChatChanged so it
// can be installed from multiple call sites (CHAT_CHANGED, explicit
// post-doNewChat trigger in handleRunEnd, etc.) without duplicating
// the setup. Idempotent: disconnects any prior observer first. Safe
// on empty chats — only observes DOM, never touches the chat array.
//
// Whenever ST rewrites a .mes_text (markdown formatter, swipe
// hydration, greeting mount, message edit), this observer re-applies
// our marker transform on the affected message in the next microtask.
// Closes every flash window, including the greeting's. Idempotent on
// the text side too: updateMessageDisplay early-returns when
// transformedText === message.mes.
function installChatObserver() {
    try {
        if (window.__imgGenChatObserver) {
            window.__imgGenChatObserver.disconnect();
        }
        const chatEl = document.getElementById('chat');
        if (!chatEl) return;
        const pending = new Set();
        const collectMesid = (el, touched) => {
            if (!el || el.nodeType !== 1) return;
            if (el.classList && el.classList.contains('mes') && el.hasAttribute('mesid')) {
                const mid = parseInt(el.getAttribute('mesid'), 10);
                if (Number.isFinite(mid) && mid >= 0) touched.add(mid);
            }
            // Descendants — an addedNode may be a wrapper containing .mes children.
            if (el.querySelectorAll) {
                const inner = el.querySelectorAll('.mes[mesid]');
                for (const child of inner) {
                    const mid = parseInt(child.getAttribute('mesid'), 10);
                    if (Number.isFinite(mid) && mid >= 0) touched.add(mid);
                }
            }
        };
        const observer = new MutationObserver((mutations) => {
            const touched = new Set();
            for (const m of mutations) {
                // Case A: characterData / subtree mutation inside a .mes —
                // walk up from target to find the enclosing .mes.
                let node = m.target;
                while (node && node !== chatEl) {
                    if (node.classList && node.classList.contains('mes') && node.hasAttribute('mesid')) {
                        const mid = parseInt(node.getAttribute('mesid'), 10);
                        if (Number.isFinite(mid) && mid >= 0) touched.add(mid);
                        break;
                    }
                    node = node.parentNode;
                }
                // Case B: childList insertion. ST mounting / re-mounting a
                // .mes element reports the mutation on #chat with the new
                // element in addedNodes — the walk-up above sees #chat and
                // bails. Scan addedNodes (and their descendants) for .mes.
                if (m.type === 'childList' && m.addedNodes) {
                    for (const added of m.addedNodes) collectMesid(added, touched);
                }
            }
            for (const mesid of touched) {
                if (pending.has(mesid)) continue;
                pending.add(mesid);
                queueMicrotask(() => {
                    pending.delete(mesid);
                    try { updateMessageDisplay(mesid); } catch (_) { /* ignore */ }
                });
            }
        });
        observer.observe(chatEl, { childList: true, subtree: true, characterData: true });
        window.__imgGenChatObserver = observer;
    } catch (err) {
        console.warn('[Image Generator] MutationObserver install failed:', err);
    }
}

async function onChatChanged() {
    const settings = initSettings();
    // Discard any queued reveal entries from the previous chat
    try { _clearRevealQueue(); } catch (_) { /* ignore */ }
    // v2.12.1 — ST resets the textarea placeholder on every chat load; re-apply ours.
    try { _updateInputPlaceholder(); } catch (_) { /* ignore */ }
    // Re-seed Remnant on chat change — the Narrator character might only
    // become available (this_chid set) after the chat has opened.
    try { seedRemnantNpc(); } catch (err) { console.warn('[Image Generator] seedRemnantNpc (chat change) failed', err); }
    try { seedFortressNpc(); } catch (err) { console.warn('[Image Generator] seedFortressNpc (chat change) failed', err); }
    renderNpcRoster();

    // v2.6.5 — Wait for BOTH the greeting DOM node AND chat[0] to be
    // populated. The prior version only polled for the element; on a
    // fresh doNewChat (post End-Story), the .mes_text div mounts before
    // chat[0] lands in the array, causing the chat.length early-return
    // below to fire and skip observer install entirely — the root cause
    // of the persistent raw-marker greeting flash.
    await new Promise((resolve) => {
        const deadline = Date.now() + 2000;
        const tick = () => {
            const domReady = !!document.querySelector('#chat .mes[mesid="0"] .mes_text');
            const arrReady = Array.isArray(chat) && chat.length > 0 && chat[0] && chat[0].mes;
            if (domReady && arrReady) {
                resolve();
            } else if (Date.now() > deadline) {
                resolve();
            } else {
                requestAnimationFrame(tick);
            }
        };
        tick();
    });

    // v2.6.5 — ALWAYS install the observer, even on empty chats. Gating
    // the observer on chat length was the bug: if the chat array wasn't
    // populated yet when this function ran, the observer never got
    // attached, and every subsequent ST re-render of .mes_text went
    // unobserved until USER_MESSAGE_RENDERED finally fired a walkAll.
    installChatObserver();

    if (!Array.isArray(chat) || chat.length === 0) {
        // No chat yet — observer is in place, ST will trip it when the
        // greeting finally hydrates. Nothing more to scrub or walk.
        return;
    }

    // Re-transform EVERY message in the chat. The greeting (message 0)
    // is frequently rendered before our CHARACTER_MESSAGE_RENDERED
    // listener is attached, so markers in the first_mes show up as
    // raw bracket text. Walking the full chat on every chat-change
    // is cheap and catches that plus any other missed messages.
    //
    // v2.3.2: repeat the walk on a delay cascade because ST sometimes
    // re-renders `.mes_text` AFTER our first pass (markdown formatter,
    // swipe hydration, etc.), wiping our transformed HTML. Re-walking
    // at 1.8s / 3.5s / 6s catches every known late-hydration window.
    // Pre-pass: scrub player dialogue out of every narrator message so
    // the LLM's rolling context window stops seeing "Aaron: ..." lines
    // on every subsequent turn (few-shot poison).
    let anyScrubbed = false;
    for (let i = 0; i < chat.length; i++) {
        const m = chat[i];
        if (!m || !m.mes || m.is_user) continue;
        const cleaned = scrubPlayerDialogue(m.mes);
        if (cleaned !== m.mes) { m.mes = cleaned; anyScrubbed = true; }
    }
    if (anyScrubbed) {
        console.log('[Image Generator] Scrubbed player dialogue from chat history on load');
        try { if (typeof saveChatDebounced === 'function') saveChatDebounced(); } catch (_) { /* ignore */ }
    }

    const walkAll = () => {
        for (let i = 0; i < chat.length; i++) {
            if (chat[i] && chat[i].mes) {
                try { updateMessageDisplay(i); } catch (_) { /* ignore */ }
            }
        }
    };
    walkAll();
    setTimeout(walkAll, 1800);
    setTimeout(walkAll, 3500);
    setTimeout(walkAll, 6000);

    // v2.6.5 — observer install moved above the early-return and
    // extracted to installChatObserver(); it has already run by here.

    // Rebuild location state from the most recent location marker anywhere
    // in the chat — scan backwards so the freshest wins. This restores the
    // "where Aaron is" memory after a page reload or reset.
    for (let i = chat.length - 1; i >= 0; i--) {
        if (chat[i] && chat[i].mes) {
            const before = (initSettings().currentLocation || '');
            updateLocationFromMessage(chat[i].mes);
            if ((initSettings().currentLocation || '') !== before) break;
        }
    }
    applyCurrentLocationPrompt();
    applyCodexStatePrompt();
    // v2.12.1 — inject story beats thread
    try { applyStoryBeatPrompt(); } catch (_) { /* ignore */ }
    // v2.6.0 — keep the profile and Remnant-memory injections fresh.
    try { applyPlayerProfilePrompt(); } catch (_) { /* ignore */ }
    try { applyRemnantMemoryPrompt(); } catch (_) { /* ignore */ }

    // v2.6.0 — one-shot continuation briefing. If settings.run.active and
    // this is a fresh chat (0 or 1 messages), inject a briefing telling
    // the narrator to pick up in media res and NOT re-narrate the
    // abduction or re-ask the ritual question.
    try {
        if (settings.run && settings.run.active && chat.length <= 1) {
            const run = settings.run;
            const lines = [];
            lines.push('RUN CONTINUITY — this is the same being, in the same run. Here is what you and they already know:');
            const profile = run.player && run.player.profile ? run.player.profile : ((initSettings().player || {}).profile || {});
            if (profile && profile.named) {
                lines.push(`- Player: ${profile.name}${profile.pronouns ? ' (' + profile.pronouns + ')' : ''}`);
                if (profile.traits && profile.traits.length) lines.push(`  Traits: ${profile.traits.join('; ')}`);
                if (profile.appearance && profile.appearance.length) lines.push(`  Appearance: ${profile.appearance.join('; ')}`);
                if (profile.history && profile.history.length) lines.push(`  History: ${profile.history.join('; ')}`);
            } else {
                lines.push('- Player: still Unknown Being — they never said who they are.');
            }
            const npcNames = Object.keys(run.npcs || {});
            if (npcNames.length) lines.push(`- Known NPCs: ${npcNames.join(', ')}`);
            const itemKeys = Object.keys((run.codex && run.codex.items) || {});
            if (itemKeys.length) {
                const itemLabels = itemKeys.map(k => {
                    const e = run.codex.items[k];
                    return (e && Array.isArray(e.aliases) && e.aliases.length) ? `${k} (aka ${e.aliases.join('/')})` : k;
                });
                lines.push(`- Known items: ${itemLabels.join(', ')}`);
            }
            const loreKeys = Object.keys((run.codex && run.codex.lore) || {});
            if (loreKeys.length) lines.push(`- Known lore: ${loreKeys.join(', ')}`);
            if (run.currentLocation) lines.push(`- Last known location: ${run.currentLocation}`);
            if (Array.isArray(run.goals) && run.goals.length) lines.push(`- Current goals: ${run.goals.join('; ')}`);
            if (run.summary) lines.push(`- Recap: ${run.summary}`);
            if (Array.isArray(run.significantEvents) && run.significantEvents.length) {
                lines.push(`- Story beats: ${run.significantEvents.slice(-10).join(' → ')}`);
            }
            lines.push('');
            if (profile && profile.named && profile.name) {
                lines.push(`OPENING INSTRUCTIONS — WARM WELCOME BACK:`);
                lines.push(`1. Greet ${profile.name} by name. "Welcome back, ${profile.name}." — warm, personal, unhurried.`);
                lines.push(`2. Give a brief recap of what they did last session. Use the story beats and known facts above. Keep it to 2–4 sentences.`);
                lines.push(`3. Invent 0–2 short, casual updates about what happened in the Fortress while the player was away — a small thing a known NPC did, an odd event in the corridors, something the Fortress noticed. Each update should be one or two sentences, lightly whimsical, grounded in the world. If there are no known NPCs yet, skip this entirely.`);
                lines.push(`4. Close with: "Are you ready to tackle the Astral Foam's many problems today?"`);
                lines.push(`Do NOT re-introduce the pod, the hoop, the goo, or the Fold. Do NOT ask "Who are you, being?" — that ritual is spent.`);
            } else {
                lines.push('Do NOT re-introduce the pod, the hoop, the goo, or the Fold. Do NOT ask "who are you, being?" — you already know them. Open this chat from the last known location, in media res, in the tone established previously.');
            }
            applyRunBriefingPrompt(lines.join('\n'));
        } else {
            applyRunBriefingPrompt('');
        }
    } catch (err) {
        console.warn('[Image Generator] run continuation briefing failed:', err);
    }

    // Re-apply the locked player portrait to every user-message avatar.
    // Necessary because ST's persona thumbnail endpoint aggressively
    // caches and will keep serving the old default avatar even after
    // the underlying file was overwritten in a prior session. The data
    // URL slam bypasses the cache.
    try { applyPlayerAvatarToChat(); } catch (_) { /* ignore */ }

    // Find the most recent non-user message for image-generation retry.
    let lastIdx = -1;
    for (let i = chat.length - 1; i >= 0; i--) {
        if (chat[i] && chat[i].is_user === false) { lastIdx = i; break; }
    }
    if (lastIdx < 0) return;

    const lastMessage = chat[lastIdx];

    // Only retry image generation if we have scene markers AND no image yet.
    const imageMarkers = detectImageMarkers(lastMessage.mes || '');
    if (imageMarkers.length === 0) return;
    if (settings.images && settings.images.length > 0) return;

    console.log('[Image Generator] CHAT_CHANGED: greeting has scene markers but no image — retrying');
    // v2.8.0 — Fortress speaking gate runs before the per-service SD
    // wait. If diag is reachable (docker or native), this blocks until
    // the whole stack reports HEALTHY. If diag is NOT reachable (no
    // sidecar running), the gate returns immediately and we fall back
    // to the legacy waitForSdReady pathway.
    await waitForFortressReady({ label: '(chat open, greeting retry)' });
    const ready = await waitForSdReady({ timeoutMs: 180000, label: '(chat open)' });
    if (!ready) return;
    // v2.6.6 — Greeting retry runs outside a narrator turn; reset the
    // per-turn budget so these calls are not blocked by a previous
    // turn's quota.
    imgGenBeginTurn();

    for (const marker of imageMarkers) {
        updatePanelStatus(`⏳ Generating image... "${marker.description.substring(0, 50)}..."`);
        const kind = classifyImageKind(marker.attribution);
        const imageData = await generateSceneImage(marker.description, kind);
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
    try { handlePlayerTrait(lastMessage.mes || ''); } catch (_) {}
    try { handleItemRenames(lastMessage.mes || ''); } catch (_) {}
    try { persistRun(); } catch (_) {}
    try { fixNarratorNames(); } catch (_) {}
    // Rebuild channel history drawers and sense button from loaded chat.
    try { _populateChannelsFromHistory(); } catch (_) {}
}

// v2.6.0 — snapshot the current run into settings.run so a new ST chat
// that starts mid-run can resume without losing state. Called after
// every rendered message (debounced via saveSettingsDebounced inside).
// Flips run.active = true the first time there is actually anything to
// remember (a named player, an NPC, a codex entry, or a location).
// v2.12.1 — Push a story beat string into run.significantEvents[]. Capped
// at 50; oldest entries are dropped to keep the array lean. No-ops if there
// is no active run object (run hasn't started yet is fine — beats accumulate
// and will be persisted the first time persistRun() fires run.active = true).
function _recordBeat(text) {
    if (!text) return;
    const settings = initSettings();
    if (!settings.run) return;
    if (!Array.isArray(settings.run.significantEvents)) settings.run.significantEvents = [];
    settings.run.significantEvents.push(text);
    while (settings.run.significantEvents.length > 50) settings.run.significantEvents.shift();
    try { applyStoryBeatPrompt(); } catch (_) { /* ignore */ }
}

function persistRun() {
    const settings = initSettings();
    if (!settings.run) return;
    const profile = (settings.player && settings.player.profile) || null;
    const hasPlayer = profile && (profile.named || (profile.appearance && profile.appearance.length) || (profile.traits && profile.traits.length));
    const hasNpcs = Object.keys(settings.npcs || {}).some(k => k !== REMNANT_NAME);
    const items = (settings.codex && settings.codex.items) || {};
    const itemKeys = Object.keys(items).filter(k => k !== 'The Fold');
    const hasCodex = itemKeys.length > 0 || Object.keys((settings.codex && settings.codex.lore) || {}).length > 0;
    const hasLocation = !!settings.currentLocation;

    if (!settings.run.active) {
        if (!(hasPlayer || hasNpcs || hasCodex || hasLocation)) return;
        settings.run.active = true;
        settings.run.startedAt = new Date().toISOString();
    }
    settings.run.lastUpdated = new Date().toISOString();
    settings.run.player = {
        profile: profile ? JSON.parse(JSON.stringify(profile)) : null,
        portrait_phrase: (settings.player && settings.player.portrait_phrase) || null,
        portrait_image: (settings.player && settings.player.portrait_image) || null,
        reference_image_url: (settings.player && settings.player.reference_image_url) || null,
        avatar_key: (settings.player && settings.player.avatar_key) || null,
    };
    // Copy npcs minus The Remnant (re-seeded on every chat open).
    const npcCopy = {};
    for (const k of Object.keys(settings.npcs || {})) {
        if (k === REMNANT_NAME) continue;
        npcCopy[k] = settings.npcs[k];
    }
    settings.run.npcs = npcCopy;
    settings.run.codex = {
        items: JSON.parse(JSON.stringify(items)),
        lore: JSON.parse(JSON.stringify((settings.codex && settings.codex.lore) || {})),
    };
    settings.run.currentLocation = settings.currentLocation || null;
    saveSettingsDebounced();
}

function initializeExtension() {
    const settings = initSettings();

    console.log('[Image Generator] Initializing...');

    createSidePanel();
    createNpcRosterPanel();
    createSpeakerSpotlight();
    installSpeakerHoverHandlers();
    bindSenseBarHandlers();

    // Re-apply the persisted current-location + codex injections so the
    // LLM keeps tracking "where the player is" and what's been named in
    // the world across reloads and new sessions.
    try { applyCurrentLocationPrompt();  } catch (_) { /* ignore */ }
    try { applyCodexStatePrompt();       } catch (_) { /* ignore */ }
    try { applyPlayerProfilePrompt();    } catch (_) { /* ignore */ }
    try { applyRemnantMemoryPrompt();    } catch (_) { /* ignore */ }
    try { applyBracketDisciplinePrompt();} catch (_) { /* ignore */ }
    try { syncPersonaName();             } catch (_) { /* ignore */ }
    // v2.8.0 — Fortress Senses polling loop. Read-only perception of
    // the host plane via /diagnostics/ai.json. Silently no-ops in
    // native dev where there is no shared origin with the diag sidecar.
    try { startFortressSensesLoop();     } catch (_) { /* ignore */ }
    try { startDevHotReload();           } catch (_) { /* ignore */ }

    // v2.6.0 — default top bar hidden. Apply the class before first paint
    // so the grey menu bar never flashes on load.
    try {
        if (settings.topBarHidden) $('body').addClass('img-gen-topbar-hidden');
    } catch (_) { /* ignore */ }

    // v2.6.0 — install the secret-phrase interceptor for the message
    // send pipeline. Clears The Remnant's permanent memory only on
    // explicit two-step confirm.
    try { installSecretForgetPhraseInterceptor(); } catch (err) { console.warn('[Image Generator] secret-phrase interceptor failed:', err); }
    // v2.1.0 — Say/Do mode bar above the chat input.
    try { installModeBar(); } catch (err) { console.warn('[Image Generator] installModeBar failed:', err); }

    // Restore saved font zoom and attach Shift+=/- keyboard shortcuts.
    try {
        _applyUiZoom(settings.uiZoom || 1);
        $(document).on('keydown.imgGenZoom', function (e) {
            if (!e.shiftKey) return;
            if (e.key === '+' || e.key === '=') {
                e.preventDefault();
                _applyUiZoom((initSettings().uiZoom || 1) + 0.05);
            } else if (e.key === '_' || e.key === '-') {
                e.preventDefault();
                _applyUiZoom((initSettings().uiZoom || 1) - 0.05);
            }
        });
    } catch (_) { /* ignore */ }

    // Reflow drawer height on window resize (replaces hard-coded calc).
    try { $(window).on('resize.imgGenDrawer', _resizeDrawer); } catch (_) { /* ignore */ }

    eventSource.on(event_types.CHARACTER_MESSAGE_RENDERED, onCharacterMessageRendered);
    eventSource.on(event_types.CHAT_CHANGED, onChatChanged);

    // v2.6.5 — Cold-boot path. ST often fires CHAT_CHANGED for the
    // initially-opened chat BEFORE our listener is attached, meaning
    // onChatChanged never runs for the boot greeting and the observer
    // is never installed — the greeting renders raw until the user
    // types. Install the observer synchronously right here so late
    // hydration is caught, then manually kick onChatChanged once to
    // run the walkAll cascade over whatever chat is already open.
    try { installChatObserver(); } catch (_) { /* ignore */ }
    try { onChatChanged(); } catch (err) { console.warn('[Image Generator] boot onChatChanged kick failed:', err); }

    // v2.6.2 — optimistic self-name preview on the user's own message.
    // Fires BEFORE the narrator responds so the roster + persona name
    // flip to "Ferro" the instant the player says "my name is Ferro".
    // The narrator's later [PLAYER_TRAIT(name)] marker still runs and
    // remains authoritative.
    if (event_types.USER_MESSAGE_RENDERED) {
        eventSource.on(event_types.USER_MESSAGE_RENDERED, (mesId) => {
            try {
                const m = (typeof mesId === 'number') ? chat[mesId] : null;
                const text = (m && m.mes) || '';
                if (text) handleUserMessageForSelfName(text);
            } catch (err) {
                console.warn('[Image Generator] USER_MESSAGE_RENDERED handler error:', err);
            }
            // Capture player input into the appropriate channel drawer
            try {
                if (_pendingUserEntry) {
                    addChannelEntry(_pendingUserEntry.channel, {
                        text: _pendingUserEntry.text,
                        isPlayer: _pendingUserEntry.isPlayer,
                    });
                    _pendingUserEntry = null;
                }
            } catch (_) { /* ignore */ }
            // v2.6.2 — belt-and-suspenders marker-transform walk. Every
            // user send re-renders the message list subtly in ST, and this
            // is our guaranteed moment to catch any greeting/prior-message
            // that slipped through the observer. Without this, a chat
            // opened cold shows raw brackets on the greeting until the
            // user types — the typing itself triggers the only mutation
            // the observer consistently sees.
            try {
                if (Array.isArray(chat)) {
                    for (let i = 0; i < chat.length; i++) {
                        if (chat[i] && chat[i].mes) {
                            try { updateMessageDisplay(i); } catch (_) { /* ignore */ }
                        }
                    }
                }
            } catch (_) { /* ignore */ }
        });
    }

    // Delegated click handler for chat-message avatar thumbnails.
    // Clicking a speaker's avatar in the chat pins the spotlight to
    // that character and highlights their roster card. The Fortress
    // IS the narrator (v2.11.0) — clicks on Fortress messages highlight
    // The Fortress roster card. Legacy "Narrator" ch_name from old chats
    // also maps to Fortress. User messages map to the player card.
    $(document).off('click.imgGenChatAvatar').on('click.imgGenChatAvatar', '#chat .mes .avatar', function (e) {
        const $mes = $(this).closest('.mes');
        if ($mes.length === 0) return;
        const isUser = $mes.attr('is_user') === 'true';
        let key = null;
        if (isUser) {
            key = '__player__';
        } else {
            const chName = $mes.attr('ch_name') || '';
            // The Fortress is the narrator; legacy "Narrator" ch_name maps here too.
            if (chName === FORTRESS_NAME || chName === 'Narrator') {
                key = FORTRESS_NAME;
            } else if (chName === REMNANT_NAME) {
                key = REMNANT_NAME;
            } else if (chName) {
                key = chName;
            }
        }
        if (!key) return;
        // Only intercept if we actually have data for this character —
        // otherwise let ST handle the click normally.
        const settings = initSettings();
        const hasData = (key === '__player__' && settings.player)
            || (key !== '__player__' && settings.npcs && settings.npcs[key]);
        if (!hasData) return;
        e.preventDefault();
        e.stopPropagation();
        pinSpotlightToCharacter(key);
    });

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
    try {
        seedFortressNpc();
    } catch (err) {
        console.warn('[Image Generator] seedFortressNpc failed:', err);
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
    try { stopFortressSensesLoop(); } catch (_) { /* ignore */ }
    $('#image-generator-panel').remove();
    $('#image-generator-npc-roster').remove();
    updateBackgroundWallpaper(null);
}

initializeExtension();

// ---------------------------------------------------------------------------
// Game UI relay — poll sidecar for player input submitted from /game/
//
// When the player types in the v3.0 UI and hits Send, the sidecar:
//   1. Runs the Sorting Hat (Ollama) to classify SAY / DO / SENSE
//   2. Wraps the text and broadcasts a player turn via SSE
//   3. Stores the wrapped text in _pending_player_input
//
// This loop consumes that slot once per second, injects the message into
// the ST chat array, and calls Generate() so The Fortress responds.
// ---------------------------------------------------------------------------
let _gameUiPollActive = false;
async function _pollGameUiInput() {
    if (_gameUiPollActive) return;
    _gameUiPollActive = true;
    try {
        const res = await fetch('/pending-player-input');
        if (res.ok) {
            const data = await res.json();
            if (data && data.text) {
                // Inject via ST's native textarea + send button — the only
                // reliable way to trigger the full ST generation pipeline.
                const $ta = $('#send_textarea');
                const $btn = $('#send_but');
                if ($ta.length && $btn.length) {
                    $ta.val(data.text).trigger('input');
                    await new Promise(r => setTimeout(r, 80));
                    $btn.trigger('click');
                    console.log(`[Image Generator] Game UI relay: sending "${data.text}"`);
                } else {
                    console.warn('[Image Generator] Game UI relay: ST send controls not found');
                }
            }
        }
    } catch (_) { /* sidecar may not be running — ignore */ }
    _gameUiPollActive = false;
}
setInterval(_pollGameUiInput, 1000);
