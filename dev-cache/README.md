# dev-cache/

Local bind-mount targets for the frozen-world cache volumes, used
by `docker-compose.dev.yml` so clean iteration on the docker stack
doesn't re-download multi-GB assets from the internet on every run.

## Single-egress + smart-client caching

nginx is the single egress keyhole for the whole docker stack.
Every other service attaches only to `play-net` (which is
`internal: true` — no direct internet). All upstream fetches
flow through nginx:

- **HuggingFace** — `bootstrap-flask-sd` uses the `huggingface_hub`
  Python client (HF's own repository manager) with
  `HF_ENDPOINT=http://nginx/hf`. nginx reverse-proxies the requests
  as a **passthrough** to `huggingface.co` and its CDNs
  (`cdn-lfs.huggingface.co`, `cas-bridge.xethub.hf.co`) via
  `proxy_redirect` rewriting. Caching is done by the `hf_hub`
  client itself in its on-disk cache layout, persisted in the
  `hf-cache` volume. nginx does NOT `proxy_cache` HF because the
  HF protocol (API + LFS + Xet redirects, presigned URLs, backend
  migrations) is not a clean fit for a generic HTTP cache — so we
  let HF's own library handle it.
- **pypi / pytorch.org / ollama.com static CDNs** — nginx's
  `proxy_cache` handles these directly via the `/pypi/`, `/pytorch/`,
  `/ollama-dl/` location blocks. Stored in the `nginx-cache`
  volume. Unused today; forward-compat for the slimming work that
  moves torch wheels + ollama CUDA libs to first-boot download.
- **Ollama model registry** — the one hybrid exception.
  `registry.ollama.ai` speaks a protocol that isn't cleanly
  proxyable, so `bootstrap-ollama` keeps its own `bootstrap-net`
  egress and writes into the `ollama-data` volume directly.

In dev mode, all three volumes (`hf-cache`, `nginx-cache`,
`ollama-data`) are bind-mounted to `dev-cache/` subdirs on the
host, so a clean `docker compose down -v` doesn't blow them away.
First run populates over the real internet (~5–10 min for ~5 GB
of HF weights). Every subsequent clean run finishes in seconds.

## What lives here

| Subdir | Bound to volume | Owner | Contents |
|---|---|---|---|
| `hf-cache/` | `hf-cache` (flask-sd, bootstrap-flask-sd) | `huggingface_hub` client | Standard HF hub cache layout — `models--<org>--<name>/` directories with snapshots + blobs + refs. Human-readable paths. This is the bulk of the frozen-world download (~5 GB). |
| `nginx-cache/` | `nginx-cache` (nginx service) | nginx `proxy_cache` | Opaque hashed-key store for pypi / pytorch.org / ollama.com static assets. Empty today (forward-compat). Not human-readable. |
| `ollama-data/` | `ollama-data` (ollama service) | ollama server | Ollama model store — mistral blobs + manifests. Hybrid exception (see above). |

These dirs are **gitignored** (see `dev-cache/.gitignore` and the
root `.gitignore`).

## Usage

```sh
# From repo root:
scripts/dev-up.sh
# Which expands to:
#   docker compose -f docker-compose.yml -f docker-compose.dev.yml \
#       --profile bootstrap up
```

First run hits the real internet and populates this directory.
Every subsequent run reuses the populated cache.

## Verifying cache behavior

For HF fetches, the authoritative check is whether `hf-cache/`
has populated `models--*/` directories after a cold run. On a warm
run, `bootstrap-flask-sd` logs will show `hf_hub` skipping downloads
because local files match the remote ETags.

For the nginx `proxy_cache` locations (`/pypi/`, `/pytorch/`,
`/ollama-dl/`), nginx emits an `X-Cache-Status` header with values
`MISS` / `HIT` / `EXPIRED` / `BYPASS`. These are unused today but
will matter once the slimming work activates.

## When to wipe

- **Testing a real cold boot end-to-end:** `rm -rf dev-cache/hf-cache/* dev-cache/nginx-cache/* dev-cache/ollama-data/*`
- **Forcing a model refresh** (rare — models are supposed to be frozen):
  wipe the specific subtree and re-run.
- Never on a whim — repopulating costs bandwidth and time.

## What NOT to do

- **Don't commit anything here.** The gitignore catches it but
  don't push your luck.
- **Don't edit files inside `hf-cache/`, `nginx-cache/`, or
  `ollama-data/` by hand.** They're managed by `huggingface_hub`,
  nginx, and ollama respectively; hand-edits will corrupt their
  indexes and checksums.
- **Don't confuse this with the living-world volumes.** Chats,
  personas, world info, lorebooks, and generated images live in
  `sillytavern-data` and `flask-gallery` named volumes (NOT bind-
  mounted here). Those are the player's irreplaceable save data
  and are untouched by anything in this directory.
