# SillyTavern with The Remnant extension pre-installed and a
# pre-seeded config.yaml that enables the built-in CORS proxy and
# binds to 0.0.0.0 so it is reachable from outside the container.

FROM ghcr.io/sillytavern/sillytavern:latest

# The upstream image's app root. Adjust if the base image changes its layout.
WORKDIR /home/node/app

# --- Drop in The Remnant extension ---
# The extension sits at public/scripts/extensions/<name>/ in upstream layouts.
# Target dir MUST be `image-generator` because the extension code itself
# hardcodes that URL segment for its static assets (see
# extension/index.js:287 `scripts/extensions/image-generator/assets/...`).
# This mirrors the native dev install path, which is the source of truth.
# Renaming away from `image-generator` will break fortress-interior.jpg
# and the player portrait seed.
COPY --chown=node:node extension/ /home/node/app/public/scripts/extensions/image-generator/

# Patch the extension's backend URL to use the docker service name instead
# of localhost. (The committed source uses localhost for native dev.)
# Use Node for the substitution to avoid sed / platform quoting headaches.
RUN node -e "const f='/home/node/app/public/scripts/extensions/image-generator/index.js';const fs=require('fs');const s=fs.readFileSync(f,'utf8').replace('/proxy/http://localhost:5000','/proxy/http://flask-sd:5000');fs.writeFileSync(f,s);console.log('[sillytavern] patched IMG_GEN_API to flask-sd:5000');"

# --- Pre-seed config.yaml ---
# Enables CORS proxy, listens on all interfaces, disables whitelist for
# the compose network. Users can still override via mounted volume.
COPY --chown=node:node docker/sillytavern/config.yaml /home/node/app/default/config.yaml

# --- Teach the content populator a `chat` type ---
# Upstream's default-content installer handles characters, worlds,
# settings, themes, presets — but not chat history. Without a seeded
# chat, a fresh boot with active_character set still trips the
# welcome-screen gate (scripts/welcome-screen.js:175) because
# currentChatId is undefined and chat[] is empty. The patch adds a
# CHAT content type that copies files from default/content/chats/
# into each user's chats/ subtree with subdir structure preserved.
# See docker/sillytavern/patches/add-chat-seed.js for the full story.
COPY --chown=node:node docker/sillytavern/patches/ /tmp/st-patches/
RUN node /tmp/st-patches/add-chat-seed.js \
 && node /tmp/st-patches/fix-scrollbar-focus-visible.js \
 && node /tmp/st-patches/fix-i18n-locale-fallback.js \
 && node /tmp/st-patches/fix-secrets-missing-input.js \
 && rm -rf /tmp/st-patches

# --- Pre-seed canonical Remnant content (characters, worlds, chats, settings) ---
#
# Everything tracked under docker/sillytavern/content/ in the repo
# overlays onto SillyTavern's default/content/ tree and gets installed
# into any empty data volume on first boot by upstream's content
# populator. The tree layout mirrors ST's default-user layout exactly
# (characters/, worlds/, chats/, top-level settings.json), so adding
# a new seed file is just:
#
#   1. drop the file into the matching subdir
#   2. commit
#
# .gitignore controls what's tracked here (see repo-root .gitignore
# for the allowlist + junk exclusions). Do NOT hand-pick files in
# this Dockerfile — the directory contents, filtered by ignore rules,
# are the single source of truth.
COPY --chown=node:node docker/sillytavern/content/ /home/node/app/default/content/

# Splice the newly copied files into default/content/index.json so
# upstream's first-boot installer picks them up. Scans characters/,
# worlds/, chats/<character>/ on disk, plus a top-level settings.json
# check, and appends any entry that isn't already present. Driven
# entirely by filesystem contents — no hand-list.
#
# Note: upstream ships its own settings.json entry already (Horde
# default), so we OVERWRITE its file on disk via the COPY above and
# leave the index.json entry untouched — the populator will seed our
# replacement bytes under the same filename.
RUN node -e "\
const fs=require('fs'),path=require('path');\
const root='/home/node/app/default/content';\
const idxPath=root+'/index.json';\
const idx=JSON.parse(fs.readFileSync(idxPath,'utf8'));\
const have=new Set(idx.map(e=>e.filename));\
const scan=(dir,type)=>{const p=path.join(root,dir);if(!fs.existsSync(p))return;for(const f of fs.readdirSync(p)){const rel=dir+'/'+f;if(fs.statSync(path.join(p,f)).isFile()&&!have.has(rel)){idx.push({filename:rel,type});console.log('[seed] +',type,rel);}}};\
scan('characters','character');\
scan('worlds','world');\
const chatsRoot=path.join(root,'chats');\
if(fs.existsSync(chatsRoot)){for(const chr of fs.readdirSync(chatsRoot)){const chrDir=path.join(chatsRoot,chr);if(!fs.statSync(chrDir).isDirectory())continue;for(const f of fs.readdirSync(chrDir)){const rel='chats/'+chr+'/'+f;if(fs.statSync(path.join(chrDir,f)).isFile()&&!have.has(rel)){idx.push({filename:rel,type:'chat'});console.log('[seed] + chat',rel);}}}}\
fs.writeFileSync(idxPath,JSON.stringify(idx,null,4));\
console.log('[seed] index.json now has',idx.length,'entries');\
"

EXPOSE 8000

# Upstream image's CMD/ENTRYPOINT handles `node server.js`
