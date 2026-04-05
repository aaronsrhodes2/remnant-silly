// Patches upstream ST's content-manager.js to teach it a new
// `chat` content-type, so chat files declared in default/content/
// index.json are installed into each user's chats/<character>/
// subdir on first boot.
//
// Why: upstream's populator handles characters, worlds, settings,
// themes, presets — but not chat history. That leaves a fresh
// clone with an active_character set but no chat to load, which
// trips SillyTavern's welcome-screen gate at
// scripts/welcome-screen.js:175 and paints the generic "Assistant"
// stub card instead of the configured Remnant opening.
//
// The patch is three targeted string replaces on the committed
// upstream source. All three are idempotent: running the patcher
// twice is a no-op on the second pass because the sentinel strings
// no longer match. If any single replace fails to find its anchor,
// the script exits non-zero so the image build fails loudly rather
// than shipping a silently-broken install.
//
// Scope of the patch:
//   1. CONTENT_TYPES enum: add CHAT: 'chat'
//   2. getTargetByType switch: case CHAT -> directories.chats
//   3. seedContentForUser copy: for chat type, preserve the
//      "<character>/<file>.jsonl" suffix under the filename instead
//      of flattening via path.parse().base. We strip the leading
//      "chats/" from the declared filename so the on-disk layout
//      in default/content/chats/ mirrors the target layout in
//      directories.chats/ exactly.
//
// Run: node docker/sillytavern/patches/add-chat-seed.js
// (invoked from the sillytavern Dockerfile against the live image
// copy of /home/node/app/src/endpoints/content-manager.js).

const fs = require('fs');

const FILE = '/home/node/app/src/endpoints/content-manager.js';
const src = fs.readFileSync(FILE, 'utf8');

// --- Replace 1: add CHAT to the CONTENT_TYPES enum --------------
const enumAnchor = "SETTINGS: 'settings',";
const enumReplacement = "SETTINGS: 'settings',\n    CHAT: 'chat',";
if (!src.includes(enumAnchor)) {
    console.error('[add-chat-seed] enum anchor not found');
    process.exit(1);
}
let out = src.replace(enumAnchor, enumReplacement);

// --- Replace 2: add CHAT case to getTargetByType switch ---------
const switchAnchor =
    'case CONTENT_TYPES.SETTINGS:\n            return directories.root;';
const switchReplacement =
    'case CONTENT_TYPES.SETTINGS:\n            return directories.root;\n' +
    '        case CONTENT_TYPES.CHAT:\n            return directories.chats;';
if (!out.includes(switchAnchor)) {
    console.error('[add-chat-seed] switch anchor not found');
    process.exit(1);
}
out = out.replace(switchAnchor, switchReplacement);

// --- Replace 3: preserve filename subdirs for chat type ---------
// Original: const basePath = path.parse(contentItem.filename).base;
// Patched : chat type uses the full filename minus the leading
// "chats/" prefix, so "chats/The Remnant/Opening.jsonl" -> targets
// directories.chats/The Remnant/Opening.jsonl.
const baseAnchor = 'const basePath = path.parse(contentItem.filename).base;';
const baseReplacement =
    "const basePath = contentItem.type === 'chat' " +
    "? contentItem.filename.replace(/^chats\\//, '') " +
    ': path.parse(contentItem.filename).base;';
if (!out.includes(baseAnchor)) {
    console.error('[add-chat-seed] basePath anchor not found');
    process.exit(1);
}
out = out.replace(baseAnchor, baseReplacement);

fs.writeFileSync(FILE, out);
console.log('[add-chat-seed] patched content-manager.js for chat type');
