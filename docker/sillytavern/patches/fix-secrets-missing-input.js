// Patches ST's secrets.js to demote "No input elements found for key: X"
// from console.warn to console.debug.
//
// Why: secrets.js iterates INPUT_MAP (every known API key → CSS selector)
// and tries to attach autocomplete/datalist UI to each matching input in
// the DOM. Keys whose extensions aren't loaded (e.g. api_key_comfy_runpod
// when ComfyUI isn't installed) produce a warn on every boot — even though
// this is expected, normal, and not actionable by the user.
//
// Fix: change the warn to debug so it's still inspectable in DevTools
// verbose mode but doesn't pollute the default console view.
//
// Idempotent: sentinel string prevents double-apply.
// Run: node docker/sillytavern/patches/fix-secrets-missing-input.js

const fs = require('fs');
const path = require('path');

const inDocker = !process.argv.includes('--native');
const ST_ROOT = inDocker
    ? '/home/node/app'
    : (process.env.ST_ROOT || path.join(process.env.USERPROFILE || process.env.HOME, 'SillyTavern'));

const FILE = path.join(ST_ROOT, 'public', 'scripts', 'secrets.js');
const src = fs.readFileSync(FILE, 'utf8');
const NL = src.includes('\r\n') ? '\r\n' : '\n';

const SENTINEL = '// [patch] demoted to debug';
if (src.includes(SENTINEL)) {
    console.log('[fix-secrets-missing-input] already patched, skipping');
    process.exit(0);
}

const ANCHOR = 'console.warn(`No input elements found for key: ${key}`);';
if (!src.includes(ANCHOR)) {
    console.error('[fix-secrets-missing-input] anchor not found — upstream may have changed');
    process.exit(1);
}

const REPLACEMENT = SENTINEL + NL +
    '            console.debug(`No input elements found for key: ${key}`);';

fs.writeFileSync(FILE, src.replace(ANCHOR, REPLACEMENT));
console.log('[fix-secrets-missing-input] patched', FILE);
