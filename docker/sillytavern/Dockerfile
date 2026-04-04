# SillyTavern with the image-generator extension pre-installed and a
# pre-seeded config.yaml that enables the built-in CORS proxy and
# binds to 0.0.0.0 so it is reachable from outside the container.

FROM ghcr.io/sillytavern/sillytavern:latest

# The upstream image's app root. Adjust if the base image changes its layout.
WORKDIR /home/node/app

# --- Drop in the image-generator extension ---
# The extension sits at public/scripts/extensions/<name>/ in upstream layouts.
COPY --chown=node:node extension/ /home/node/app/public/scripts/extensions/image-generator/

# Patch the extension's backend URL to use the docker service name instead
# of localhost. (The committed source uses localhost for native dev.)
# Use Node for the substitution to avoid sed / platform quoting headaches.
RUN node -e "const f='/home/node/app/public/scripts/extensions/image-generator/index.js';const fs=require('fs');const s=fs.readFileSync(f,'utf8').replace('/proxy/http://localhost:5000','/proxy/http://flask-sd:5000');fs.writeFileSync(f,s);console.log('[sillytavern] patched IMG_GEN_API to flask-sd:5000');"

# --- Pre-seed config.yaml ---
# Enables CORS proxy, listens on all interfaces, disables whitelist for
# the compose network. Users can still override via mounted volume.
COPY --chown=node:node docker/sillytavern/config.yaml /home/node/app/default/config.yaml

# --- Pre-seed the Game Master character card ---
# Creates The Remnant character on first boot so the user has zero setup.
COPY --chown=node:node docker/sillytavern/characters/ /home/node/app/default/content/characters/

EXPOSE 8000

# Upstream image's CMD/ENTRYPOINT handles `node server.js`
