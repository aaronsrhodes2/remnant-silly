/**
 * Image Generator Extension for SillyTavern
 * Automatically generates images from AI narration with visual consistency
 */

import {
    getContext,
    extension_settings,
    renderExtensionTemplateAsync,
} from '../../extensions.js';

import {
    eventSource,
    event_types,
    chat,
    saveSettingsDebounced,
} from '../../../script.js';

// Use SillyTavern's built-in CORS proxy to avoid browser cross-origin blocks
// Requires enableCorsProxy: true in config.yaml
const IMG_GEN_API = '/proxy/http://localhost:5000';
const EXTENSION_NAME = 'image-generator';

// Initialize extension settings
function initSettings() {
    if (!extension_settings[EXTENSION_NAME]) {
        extension_settings[EXTENSION_NAME] = {
            enabled: true,
            autoGenerate: true,
            generateEvery: 1, // Generate every N messages
            images: [],
            characters: {},
            locations: {},
            imageHistory: [],
        };
    }
    return extension_settings[EXTENSION_NAME];
}

// Sensory marker types - each maps to a CSS color class
// Visual markers also trigger image generation
const SENSE_MARKERS = {
    GENERATE_IMAGE: { cssClass: 'sense-visual',      triggersImage: true  },
    SIGHT:          { cssClass: 'sense-visual',      triggersImage: true  },
    SMELL:          { cssClass: 'sense-smell',       triggersImage: false },
    SOUND:          { cssClass: 'sense-sound',       triggersImage: false },
    TASTE:          { cssClass: 'sense-taste',       triggersImage: false },
    TOUCH:          { cssClass: 'sense-touch',       triggersImage: false },
    ENVIRONMENT:    { cssClass: 'sense-environment', triggersImage: false },
};

// Detect all sensory markers: [MARKER: "description"] or [MARKER: description]
// Returns array of { type, description, fullMatch, triggersImage, cssClass }
function detectSenseMarkers(messageText) {
    const markerNames = Object.keys(SENSE_MARKERS).join('|');
    // Match: [MARKER: "quoted desc"] OR [MARKER: unquoted desc ending before ]]
    const regex = new RegExp(
        `\\[(${markerNames}):\\s*(?:"([^"]+)"|([^\\]]+))\\]`,
        'gi'
    );
    const matches = [];
    let match;
    while ((match = regex.exec(messageText)) !== null) {
        const type = match[1].toUpperCase();
        const description = (match[2] || match[3]).trim();
        const config = SENSE_MARKERS[type];
        matches.push({
            type: type,
            description: description,
            fullMatch: match[0],
            cssClass: config.cssClass,
            triggersImage: config.triggersImage,
        });
    }
    return matches;
}

// Backwards-compat wrapper: returns only image-generating markers
function detectImageMarkers(messageText) {
    return detectSenseMarkers(messageText).filter(m => m.triggersImage);
}

// Extract character name from message context
function extractCharacterName(messageText) {
    const characterKeywords = [
        'sherri', 'the remnant', 'remnant', 'aaron', 'rhodes',
        'automaton', 'ai', 'artificial intelligence'
    ];

    const lower = messageText.toLowerCase();
    for (const keyword of characterKeywords) {
        if (lower.includes(keyword)) {
            return keyword.charAt(0).toUpperCase() + keyword.slice(1);
        }
    }
    return null;
}

// Generate an image via the API
async function generateImage(description, characterName = null) {
    try {
        console.log('[Image Generator] Starting generation for:', description.substring(0, 50));

        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 180000); // 3 minute timeout for Stable Diffusion first run

        const response = await fetch(`${IMG_GEN_API}/api/generate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                prompt: description,
                steps: 25,
                guidance_scale: 7.5,
            }),
            signal: controller.signal,
        });

        clearTimeout(timeout);

        if (!response.ok) {
            console.error('[Image Generator] API returned error:', response.status, response.statusText);
            return null;
        }

        const data = await response.json();
        console.log('[Image Generator] API response:', data.success ? 'SUCCESS' : 'FAILED', data);

        if (data.success) {
            console.log('[Image Generator] Image generated successfully, storing...');
            return {
                image_id: data.image_id,
                image: data.image,
                description: description,
                character: characterName,
                timestamp: new Date().toISOString(),
            };
        } else {
            console.error('[Image Generator] API reported failure:', data);
        }
    } catch (error) {
        if (error.name === 'AbortError') {
            console.error('[Image Generator] Request timeout (180 seconds exceeded)');
        } else {
            console.error('[Image Generator] Generation error:', error);
        }
    }
    return null;
}

// Store image in gallery and extract character info
function storeImage(imageData) {
    const settings = initSettings();

    settings.images.push(imageData);
    settings.imageHistory.push(imageData.image_id);

    // Extract and store character description if applicable
    if (imageData.character) {
        if (!settings.characters[imageData.character]) {
            settings.characters[imageData.character] = {
                description: imageData.description,
                image_id: imageData.image_id,
                first_appearance: imageData.timestamp,
            };
        }
    }

    saveSettingsDebounced();
}

// Escape HTML to prevent XSS when wrapping descriptions
function escapeHtml(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

// Transform message text: replace sensory markers with colored <span> tags
// [GENERATE_IMAGE: "..."] -> <span class="sense-visual">...</span>
// [SMELL: "..."]          -> <span class="sense-smell">...</span>
// etc. Handles both quoted and unquoted descriptions.
function transformMessageText(messageText) {
    const markers = detectSenseMarkers(messageText);
    let result = messageText;
    // Replace in reverse order to keep earlier indices valid
    for (let i = markers.length - 1; i >= 0; i--) {
        const m = markers[i];
        const idx = result.lastIndexOf(m.fullMatch);
        if (idx === -1) continue;
        const replacement = `<span class="${m.cssClass}">${escapeHtml(m.description)}</span>`;
        result = result.slice(0, idx) + replacement + result.slice(idx + m.fullMatch.length);
    }
    return result;
}

// Legacy alias - kept for any callers that might still reference it
function cleanMessageText(messageText) {
    return transformMessageText(messageText);
}

// Update a message in the display (transform markers into colored spans)
function updateMessageDisplay(messageId) {
    if (typeof messageId !== 'number' || messageId < 0) return;

    const message = chat[messageId];
    if (!message || !message.mes) return;

    const transformedText = transformMessageText(message.mes);
    if (transformedText !== message.mes) {
        const messageElement = $(`[data-message-id="${messageId}"]`).find('.mes_text');
        if (messageElement.length > 0) {
            // DOMPurify with relaxed span/class to allow our coloring
            messageElement.html(DOMPurify.sanitize(transformedText, {
                ADD_ATTR: ['class'],
            }));
        }
    }
}

// Build character context for prompts
function buildCharacterContext() {
    const settings = initSettings();
    const characters = settings.characters;

    if (Object.keys(characters).length === 0) {
        return '';
    }

    let context = '\n[CHARACTER VISUAL REFERENCES]\n';
    for (const [name, data] of Object.entries(characters)) {
        context += `${name}: ${data.description}\n`;
    }

    return context;
}

// Handle character message rendered event
async function onCharacterMessageRendered(messageId) {
    if (typeof messageId !== 'number' || messageId < 0) {
        return;
    }

    const message = chat[messageId];
    if (!message || !message.mes) return;

    const settings = initSettings();
    if (!settings.autoGenerate) return;

    // Detect markers
    const markers = detectImageMarkers(message.mes);
    if (markers.length === 0) return;

    // Clean the message display
    updateMessageDisplay(messageId);

    // Generate images
    for (const marker of markers) {
        const characterName = extractCharacterName(message.mes);

        // Show loading indicator in side panel
        updatePanelStatus(`⏳ Generating image... (may take 60-120 seconds on first run)\n"${marker.description.substring(0, 50)}..."`);
        console.log(`[Image Generator] Queue image generation: ${marker.description.substring(0, 60)}`);

        const imageData = await generateImage(marker.description, characterName);

        if (imageData) {
            console.log(`[Image Generator] Image received, adding to panel`);
            storeImage(imageData);
            addImageToPanel(imageData);
            updatePanelStatus('');
        } else {
            console.error(`[Image Generator] Failed to generate image for: ${marker.description.substring(0, 50)}`);
            updatePanelStatus('❌ Image generation failed. Check console for details.');
        }
    }
}

// Create and show the side panel
function createSidePanel() {
    // Create panel HTML directly instead of loading template
    const panelHTML = `
        <div class="img-gen-panel-header">
            <h3 class="img-gen-panel-title">🖼️ Image Gallery</h3>
            <button class="img-gen-close" id="img-gen-close" title="Close panel">✕</button>
        </div>
        <div class="img-gen-status"></div>
        <div class="img-gen-gallery">
            <div class="img-gen-empty">
                <p>Awaiting generated images...<br/><small>They'll appear here as the narrator creates scenes</small></p>
            </div>
        </div>
    `;

    // Add panel to DOM if not exists
    if ($('#image-generator-panel').length === 0) {
        $('body').append(`<div id="image-generator-panel" class="img-gen-panel">${panelHTML}</div>`);

        // Close button handler
        $('#img-gen-close').on('click', () => {
            $('#image-generator-panel').toggle();
        });
    }
}

// Add image to side panel
function addImageToPanel(imageData) {
    const panel = $('#image-generator-panel .img-gen-gallery');
    if (panel.length === 0) return;

    const imageHtml = `
        <div class="img-gen-item" data-image-id="${imageData.image_id}">
            <img src="${imageData.image}" alt="${imageData.description}" />
            <p class="img-gen-description">${imageData.description.substring(0, 100)}</p>
            ${imageData.character ? `<p class="img-gen-character">${imageData.character}</p>` : ''}
        </div>
    `;

    panel.prepend(imageHtml);
}

// Update panel status message
function updatePanelStatus(message) {
    const status = $('#image-generator-panel .img-gen-status');
    if (status.length > 0) {
        if (message) {
            status.text(message).show();
        } else {
            status.hide();
        }
    }
}

// Inject character context before generation (hook into message sending)
function injectCharacterContext() {
    // This would hook into the generation prompt
    // For now, the character context is available if needed
    const context = buildCharacterContext();
    console.log('Character context available:', context);
}

// Initialize extension
function initializeExtension() {
    const settings = initSettings();

    console.log(`[Image Generator] Initializing...`);

    // Create side panel UI
    createSidePanel();

    // Listen for character messages
    eventSource.on(event_types.CHARACTER_MESSAGE_RENDERED, onCharacterMessageRendered);

    // Restore previous images to panel
    for (const imageData of settings.images.slice(-10)) { // Show last 10 images
        addImageToPanel(imageData);
    }

    console.log(`[Image Generator] Ready! Images stored: ${settings.images.length}, Characters: ${Object.keys(settings.characters).length}`);
}

// Export activate hook
export function activate() {
    initializeExtension();
}

// Export disable hook
export function disable() {
    eventSource.off(event_types.CHARACTER_MESSAGE_RENDERED, onCharacterMessageRendered);
    $('#image-generator-panel').remove();
}

// Initialize on load
initializeExtension();
