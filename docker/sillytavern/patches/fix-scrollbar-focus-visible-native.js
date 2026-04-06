// Same as fix-scrollbar-focus-visible.js but targets the native ST install.
// Run: node docker/sillytavern/patches/fix-scrollbar-focus-visible-native.js
const fs = require('fs');
const path = require('path');

const ST_ROOT = process.env.ST_ROOT || path.join(process.env.USERPROFILE || process.env.HOME, 'SillyTavern');
const FILE = path.join(ST_ROOT, 'public', 'scripts', 'dynamic-styles.js');

const src = fs.readFileSync(FILE, 'utf8');

const SENTINEL = '// [patch] skip ::-webkit-scrollbar :focus-visible';
if (src.includes(SENTINEL)) {
    console.log('[fix-scrollbar-focus-visible-native] already patched, skipping');
    process.exit(0);
}

// Support both LF (docker/unix) and CRLF (Windows native install)
const NL = src.includes('\r\n') ? '\r\n' : '\n';

const ANCHOR =
    "const focusSelector = rule.selectorText.replace(/:hover/g, ':focus-visible');" + NL +
    '            let focusRule = `${focusSelector} { ${rule.style.cssText} }`;';

if (!src.includes(ANCHOR)) {
    console.error('[fix-scrollbar-focus-visible-native] anchor not found — upstream may have changed');
    console.error('Expected to find in:', FILE);
    process.exit(1);
}

const REPLACEMENT =
    "const focusSelector = rule.selectorText.replace(/:hover/g, ':focus-visible');" + NL +
    '            ' + SENTINEL + NL +
    '            if (/::[-\\w]*scrollbar/.test(focusSelector)) return;' + NL +
    '            let focusRule = `${focusSelector} { ${rule.style.cssText} }`;';

fs.writeFileSync(FILE, src.replace(ANCHOR, REPLACEMENT));
console.log('[fix-scrollbar-focus-visible-native] patched', FILE);
