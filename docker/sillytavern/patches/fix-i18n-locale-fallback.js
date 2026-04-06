// Patches ST's i18n.js to fall back to the base language code when the
// full locale tag has no match (e.g. browser returns 'en-us', we only
// have 'en.json' → silently use 'en' rather than logging a warning).
//
// Why: navigator.language on en-US systems returns 'en-US', which
// i18n.js lowercases to 'en-us'. findLang() does an exact match against
// the langs array. 'en-us' doesn't match 'en', so it warns and returns
// undefined — falling back to English anyway, but with console noise.
//
// Fix: when the exact match fails, try the base tag (everything before
// the first '-'). If that matches, return it silently. Only warn if
// even the base tag doesn't match anything.
//
// Idempotent: sentinel string prevents double-apply.
// Run: node docker/sillytavern/patches/fix-i18n-locale-fallback.js
// (invoked from Dockerfile; for native pass --native flag or set ST_ROOT)

const fs = require('fs');
const path = require('path');

const inDocker = !process.argv.includes('--native');
const ST_ROOT = inDocker
    ? '/home/node/app'
    : (process.env.ST_ROOT || path.join(process.env.USERPROFILE || process.env.HOME, 'SillyTavern'));

const FILE = path.join(ST_ROOT, 'public', 'scripts', 'i18n.js');
const src = fs.readFileSync(FILE, 'utf8');
const NL = src.includes('\r\n') ? '\r\n' : '\n';

const SENTINEL = '// [patch] base-language fallback';
if (src.includes(SENTINEL)) {
    console.log('[fix-i18n-locale-fallback] already patched, skipping');
    process.exit(0);
}

// Anchor: the body of findLang — works for both LF and CRLF
// Two possible anchors: unpatched original, or already-partially-patched
// (base fallback added but warn condition not yet fixed).
const ANCHOR_ORIGINAL =
    'function findLang(language) {' + NL +
    '    const supportedLang = langs.find(x => x.lang === language);' + NL +
    NL +
    '    if (!supportedLang && language !== \'en\') {' + NL +
    '        console.warn(`Unsupported language: ${language}`);' + NL +
    '    }' + NL +
    '    return supportedLang;' + NL +
    '}';

const ANCHOR_PARTIAL =
    'function findLang(language) {' + NL +
    '    let supportedLang = langs.find(x => x.lang === language);' + NL +
    '    ' + SENTINEL + NL +
    '    if (!supportedLang) {' + NL +
    '        const base = language.split(\'-\')[0];' + NL +
    '        supportedLang = langs.find(x => x.lang === base);' + NL +
    '    }' + NL +
    '    if (!supportedLang && language !== \'en\') {' + NL +
    '        console.warn(`Unsupported language: ${language}`);' + NL +
    '    }' + NL +
    '    return supportedLang;' + NL +
    '}';

const ANCHOR = src.includes(ANCHOR_ORIGINAL) ? ANCHOR_ORIGINAL
    : src.includes(ANCHOR_PARTIAL) ? ANCHOR_PARTIAL
    : null;

if (!ANCHOR) {
    console.error('[fix-i18n-locale-fallback] anchor not found — upstream may have changed');
    process.exit(1);
}

// Final form: base-lang lookup + suppress warn for all en-* variants
// (en is not in lang.json since it is the implicit default; base lookup
// always returns undefined for en-us/en-gb/etc — so check startsWith).
const REPLACEMENT =
    'function findLang(language) {' + NL +
    '    let supportedLang = langs.find(x => x.lang === language);' + NL +
    '    ' + SENTINEL + NL +
    '    if (!supportedLang) {' + NL +
    '        const base = language.split(\'-\')[0];' + NL +
    '        supportedLang = langs.find(x => x.lang === base);' + NL +
    '    }' + NL +
    // en is the implicit default (no lang.json entry); en-* variants
    // (en-us, en-gb, …) also map to it — suppress the warn for all of them.
    '    if (!supportedLang && language !== \'en\' && !language.startsWith(\'en-\')) {' + NL +
    '        console.warn(`Unsupported language: ${language}`);' + NL +
    '    }' + NL +
    '    return supportedLang;' + NL +
    '}';

fs.writeFileSync(FILE, src.replace(ANCHOR, REPLACEMENT));
console.log('[fix-i18n-locale-fallback] patched', FILE);
