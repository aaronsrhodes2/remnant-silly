// Patches upstream ST's dynamic-styles.js to skip ::-webkit-scrollbar-*
// pseudo-elements when generating :focus-visible counterparts for :hover rules.
//
// Why: dynamic-styles.js takes every :hover rule in every loaded stylesheet
// and synthesises a :focus-visible twin for keyboard-accessibility parity.
// Fine in general, but ::-webkit-scrollbar pseudo-elements combined with
// :focus-visible produce invalid CSS — Chromium rejects the insertRule()
// call and logs a SyntaxError console.warn on every page load:
//
//   Failed to insert focus rule: SyntaxError: Failed to parse the rule
//   '::-webkit-scrollbar-track:focus-visible { ... }'
//
// The rule is also semantically meaningless: scrollbars are not
// keyboard-focusable, so :focus-visible on them would never fire anyway.
//
// Fix: insert a one-line guard immediately after the focusSelector is built
// that returns early when the selector targets a webkit-scrollbar element.
//
// Idempotent: if the sentinel text is already present the replace() finds
// nothing to change and the file is left untouched. If the anchor is missing
// (upstream refactor) the script exits non-zero so the image build fails
// loudly rather than shipping a silently-broken install.
//
// Run: node docker/sillytavern/patches/fix-scrollbar-focus-visible.js
// (invoked from the sillytavern Dockerfile against the live image copy of
// /home/node/app/public/scripts/dynamic-styles.js)

const fs = require('fs');

const FILE = '/home/node/app/public/scripts/dynamic-styles.js';
const src = fs.readFileSync(FILE, 'utf8');

// Guard: skip if already patched (idempotent)
const SENTINEL = '// [patch] skip ::-webkit-scrollbar :focus-visible';
if (src.includes(SENTINEL)) {
    console.log('[fix-scrollbar-focus-visible] already patched, skipping');
    process.exit(0);
}

const ANCHOR =
    "const focusSelector = rule.selectorText.replace(/:hover/g, ':focus-visible');\n" +
    '            let focusRule = `${focusSelector} { ${rule.style.cssText} }`;';

if (!src.includes(ANCHOR)) {
    console.error('[fix-scrollbar-focus-visible] anchor not found — upstream may have changed');
    process.exit(1);
}

const REPLACEMENT =
    "const focusSelector = rule.selectorText.replace(/:hover/g, ':focus-visible');\n" +
    `            ${SENTINEL}\n` +
    '            if (/::[-\\w]*scrollbar/.test(focusSelector)) return;\n' +
    '            let focusRule = `${focusSelector} { ${rule.style.cssText} }`;';

const out = src.replace(ANCHOR, REPLACEMENT);
fs.writeFileSync(FILE, out);
console.log('[fix-scrollbar-focus-visible] patched dynamic-styles.js: webkit-scrollbar :focus-visible rules suppressed');
