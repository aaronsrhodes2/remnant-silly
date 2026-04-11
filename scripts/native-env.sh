# native-env.sh — source this in any shell where you'll run host-side
# package managers (hf_hub / pip / npm) during native dev.
#
#     source scripts/native-env.sh
#
# Effect: every public-internet fetch those tools make gets routed
# through the native nginx gateway at http://localhost:1580, which
# is the single egress keyhole for the dev environment. This mirrors
# what the docker stack enforces at :1582 via play-net isolation.
#
# Requires: scripts/native-up.sh is running (nginx container must
# be up on :1580), otherwise every fetch will fail instantly.
#
# Why `source` instead of a wrapper: dev ergonomics. These are env
# vars that tools look up themselves — no wrapper scripts, no PATH
# shadowing, no surprise when a tool works slightly differently than
# its docs describe. `unset` or open a fresh shell to revert.

# HuggingFace — consumed by huggingface_hub (both the Python library
# and the `huggingface-cli` command). Points the HF client at the
# nginx /hf/ passthrough; the client still does all its own caching
# under ~/.cache/huggingface/hub, but every network byte goes
# through nginx.
export HF_ENDPOINT=http://localhost:1580/hf

# pip — pypi.org + pytorch wheel index through nginx.
# PIP_TRUSTED_HOST suppresses the "insecure HTTP index" warning
# that pip emits when the index URL is not HTTPS; nginx terminates
# TLS on the real upstream hop, so the only plaintext leg is the
# one between pip and localhost.
export PIP_INDEX_URL=http://localhost:1580/pypi/simple
export PIP_EXTRA_INDEX_URL=http://localhost:1580/pytorch
export PIP_TRUSTED_HOST=localhost

# npm — package tarballs through nginx. npm's config precedence
# honors `npm_config_<key>` env vars above ~/.npmrc.
export npm_config_registry=http://localhost:1580/npm/

# ollama — host-side `ollama pull` still talks to the user's own
# ollama install on 127.0.0.1:11434, not to nginx. Ollama itself
# then fetches from registry.ollama.ai directly; that's a non-
# cacheable protocol and matches the bootstrap-ollama hybrid
# exception on the docker side.
export OLLAMA_HOST=127.0.0.1:1593

echo "[native-env] routed hf_hub + pip + npm through http://localhost:1580"
echo "[native-env]   HF_ENDPOINT          = $HF_ENDPOINT"
echo "[native-env]   PIP_INDEX_URL        = $PIP_INDEX_URL"
echo "[native-env]   PIP_EXTRA_INDEX_URL  = $PIP_EXTRA_INDEX_URL"
echo "[native-env]   npm_config_registry  = $npm_config_registry"
echo "[native-env]   OLLAMA_HOST          = $OLLAMA_HOST (direct to host ollama)"
