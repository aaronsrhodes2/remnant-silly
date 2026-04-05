# NOTICES

Third-party components, model weights, and assets used by The Remnant,
with their licenses and authoritative sources.

This document is the single source of truth for licensing in the
project. If you are redistributing The Remnant (in any form — source,
binary, container image, forked game), you must preserve this file and
comply with every license listed below.

---

## TL;DR — can I sell this?

**No, not as a proprietary closed-source product.** See
[§ Commercial use](#commercial-use) below for the full analysis.

**Can I fork it, mod it, ship my own variant, teach with it, learn
from it, build on top of it?** Yes — that is the whole point. See
[§ The concept is set free](#the-concept-is-set-free).

---

## The project's own license

The Remnant is licensed under the **GNU Affero General Public License,
version 3 or later** (`AGPL-3.0-or-later`, SPDX).

Full text: <https://www.gnu.org/licenses/agpl-3.0.txt>
See `LICENSE` in the project root for the copyright notice.

---

## Third-party runtime components

The Remnant is a bundle. The following components are downloaded,
built into images, or linked at runtime and are the property of their
respective authors. Each is listed with the SPDX identifier, the
authoritative license URL, and a note on how the project uses it.

### Application runtimes

| Component | Version | License | SPDX | Source |
|---|---|---|---|---|
| **SillyTavern** | upstream `latest` (ghcr.io/sillytavern/sillytavern) | GNU Affero GPL v3 or later | `AGPL-3.0-or-later` | <https://github.com/SillyTavern/SillyTavern/blob/release/LICENSE> |
| **Ollama** | upstream `latest` (ollama/ollama) | MIT | `MIT` | <https://github.com/ollama/ollama/blob/main/LICENSE> |
| **nginx** | `1.27-alpine` | BSD 2-Clause ("Simplified") | `BSD-2-Clause` | <https://nginx.org/LICENSE> |
| **Python** | `3.11-alpine` | Python Software Foundation License | `PSF-2.0` | <https://docs.python.org/3/license.html> |
| **PyTorch (CUDA runtime)** | `pytorch/pytorch:2.1.2-cuda12.1-cudnn8-runtime` | BSD 3-Clause | `BSD-3-Clause` | <https://github.com/pytorch/pytorch/blob/main/LICENSE> |
| **Alpine Linux** (base for nginx + diag) | packaged in base images | MIT + various (busybox, musl, etc.) | `MIT` and others | <https://alpinelinux.org/about/> |

**Critical detail on SillyTavern**: The Remnant bundles SillyTavern
inside `docker/sillytavern/Dockerfile` and installs its extension
(`extension/`) directly into SillyTavern's `public/scripts/extensions/`
directory at image build time. This is a derivative work under the
AGPL's definition, and the entire distribution is therefore bound by
AGPL-3.0-or-later.

### Python libraries (flask-sd backend)

Pinned in `docker/flask-sd/requirements.txt`. All are imported at
runtime inside the `flask-sd` container.

| Package | Version | License | SPDX | Source |
|---|---|---|---|---|
| **Flask** | 3.0.3 | BSD 3-Clause | `BSD-3-Clause` | <https://flask.palletsprojects.com/en/latest/license/> |
| **Flask-CORS** | 4.0.1 | MIT | `MIT` | <https://github.com/corydolphin/flask-cors/blob/master/LICENSE> |
| **diffusers** (HuggingFace) | 0.27.2 | Apache 2.0 | `Apache-2.0` | <https://github.com/huggingface/diffusers/blob/main/LICENSE> |
| **transformers** (HuggingFace) | 4.41.2 | Apache 2.0 | `Apache-2.0` | <https://github.com/huggingface/transformers/blob/main/LICENSE> |
| **accelerate** (HuggingFace) | 0.30.1 | Apache 2.0 | `Apache-2.0` | <https://github.com/huggingface/accelerate/blob/main/LICENSE> |
| **safetensors** (HuggingFace) | 0.4.3 | Apache 2.0 | `Apache-2.0` | <https://github.com/huggingface/safetensors/blob/main/LICENSE> |
| **huggingface_hub** | 0.25.2 | Apache 2.0 | `Apache-2.0` | <https://github.com/huggingface/huggingface_hub/blob/main/LICENSE> |
| **Pillow** | 10.3.0 | HPND (Historical Permission Notice and Disclaimer) | `HPND` | <https://github.com/python-pillow/Pillow/blob/main/LICENSE> |

The **diag** sidecar (`docker/diag/app.py`) uses **only Python stdlib**
and therefore adds no extra third-party Python dependencies beyond the
Python interpreter itself.

---

## Model weights (downloaded at first boot, not redistributed)

These are fetched on the end user's machine at first boot from their
original authors' canonical hosting. The Remnant distribution itself
does **not** redistribute the weights — it redistributes the pinned
manifest (`docker/models.lock.json`) that tells the bootstrap
downloader where and what to fetch. Each user's local copy is subject
to the license terms below.

### Stable Diffusion v1.5 (portrait rendering)

| Field | Value |
|---|---|
| **Repository** | `runwayml/stable-diffusion-v1-5` |
| **Variant** | fp16 |
| **License** | CreativeML Open RAIL-M |
| **License text** | <https://huggingface.co/runwayml/stable-diffusion-v1-5/blob/main/LICENSE.md> |
| **Use restrictions** | Yes — Attachment A of the license lists prohibited uses (illegal content, impersonation, harassment, medical/legal advice, generating content for disinformation, etc.). Redistributors and users are bound by these restrictions. |
| **Commercial use** | Permitted, subject to Attachment A. |

**Important:** CreativeML Open RAIL-M is a "responsible AI license"
(RAIL). It is **not** an OSI-approved open-source license because it
places behavioral restrictions on how the model's outputs may be used.
Any party using The Remnant must comply with Attachment A, and any
party building on The Remnant's image-generation pipeline must pass
those restrictions through to their own users. Read the full license
before redistributing.

### IP-Adapter Plus (portrait identity conditioning)

| Field | Value |
|---|---|
| **Repository** | `h94/IP-Adapter` |
| **File** | `models/ip-adapter-plus_sd15.bin` |
| **License** | Apache 2.0 |
| **License text** | <https://huggingface.co/h94/IP-Adapter/blob/main/LICENSE> |
| **Commercial use** | Permitted without additional restrictions. |

### Mistral 7B Instruct (narrator text generation)

| Field | Value |
|---|---|
| **Distribution** | Ollama registry — `mistral` tag (defaults to Mistral 7B Instruct) |
| **License** | Apache 2.0 |
| **License text** | <https://ollama.com/library/mistral> / upstream <https://mistral.ai/news/announcing-mistral-7b/> |
| **Commercial use** | Permitted without additional restrictions. |

---

## Copyleft chain reaction

Here is the practical consequence of the component stack, expressed
plainly:

1. **SillyTavern is AGPL-3.0-or-later.** This is the strongest license
   in the bundle and it is viral in a specific way: the AGPL's
   section 13 closes the "SaaS loophole" — anyone who interacts with
   a modified version of the work over a network has the right to
   receive the corresponding source.
2. **The Remnant's extension loads into SillyTavern's runtime** and
   **The Remnant's Docker images redistribute SillyTavern**. Either
   fact alone is enough to make the combined work a derivative; both
   together leave no ambiguity.
3. **Therefore the entire project — backend, extension, scripts,
   docker bundle, nginx splash, diag sidecar — must be offered under
   AGPL-3.0-or-later or a license the FSF recognizes as compatible
   with it.** There is no subset of this project that can be closed.
4. **The SD 1.5 RAIL-M behavioral restrictions flow downstream** to
   every user who generates images through the pipeline. This is not a
   copyleft obligation on the *code*; it is a use-case obligation on
   the *model outputs*, and it is independent of the AGPL.
5. **Mistral 7B Instruct, IP-Adapter, Ollama, nginx, and all the
   Python libraries are permissively licensed** and impose no
   additional constraints beyond preserving their notices (which this
   file satisfies).

The effective license of the whole work is **AGPL-3.0-or-later, with
the additional use-case restrictions of CreativeML Open RAIL-M
applying specifically to the image-generation pipeline's outputs**.

---

## Commercial use

### Can this be sold as a proprietary closed-source game?

**No.** The AGPL-3.0 inheritance from SillyTavern forbids it. You
cannot take The Remnant, strip the source, wrap a storefront EULA
around it, and sell it as a closed product. Any binary or container
you distribute must be accompanied by (or offer) the complete
corresponding source, and any modification must be released under the
same license.

### Can it be sold at all?

Technically yes — AGPL does not prohibit charging money. But:

- Any customer who receives a copy has the legal right to redistribute
  it to anyone else, for free or for any price they choose.
- Any user who interacts with a hosted version over a network must be
  able to obtain the full source code of that specific version,
  including all modifications.
- You cannot add proprietary DRM, closed update channels, or exclusive
  feature tiers that are not themselves released under AGPL.

In practice, this means the shape of "selling" that makes sense for
AGPL software is selling *support, hosting, curation, or installation
services* around the free artifact — not selling exclusivity over the
artifact itself.

### Can it be forked, modded, extended, taught, studied, rebuilt?

**Yes. Without asking. That is the whole point.** The only conditions
are the ones the licenses already state: preserve attribution, publish
your changes under a compatible license, and if you expose a modified
version over a network, make the source of that modified version
available to its users.

---

## The concept is set free

The Remnant is a demonstration that a genre — self-integrated,
model-shipping, air-gapped, narrator-driven interactive fiction — can
be built by one person on a laptop, packaged into a single
double-clickable Docker image, and handed to another human being who
needs only an internet connection for the first boot and then never
needs it again.

That demonstration is worth more to the author as a **precedent** than
it would be worth as a **product**. The author's position, stated for
the record and documented here so the intent travels with the code:

> I am not going to get rich from *selling* this game. I am going to
> get rich because *I made this game*. The concept must be set free
> for it to flourish.

Accordingly, every design decision in this project that could have
been locked down has been left open:

- The license stack (AGPL + permissive dependencies + RAIL-M on the
  image model only) is the strongest freedom-preserving configuration
  available given the components.
- Models are pinned by repository and revision in
  `docker/models.lock.json` so any fork can reproduce the bundle
  byte-for-byte.
- The diagnostic surface (`/diagnostics/ai.json`, `/diagnostics/actions`)
  is open to any AI agent or human operator on the same host — there
  is no authentication layer to strip, because there is no author-only
  access tier to protect.
- The Fortress Senses integration reads that diagnostic surface as
  atmosphere, *read-only*, so forks can rewire the in-lore mapping
  without unpicking any DRM layer.
- Extension, backend, splash, diagnostics, docker branch sync script,
  and asset pipeline are all in the repository. There is no
  "community edition vs. full edition" split, because there is no full
  edition being held back.

If you are reading this because you are thinking about building
something in this shape, or because you forked the repository and
wanted to know what you are allowed to do: build it, fork it, ship it,
rename it, change the genre, rip out the narrator and put in your own,
replace Mistral with something you trained yourself, replace SD 1.5
with something that renders in a different style, strip every file
that has the author's name on it. The license lets you. The author
wants you to.

The only thing the author asks — and this is a social request, not a
legal one — is that if you make something real out of this, you tell
the author so they can point at it.

---

## How to satisfy your redistribution obligations

If you redistribute The Remnant (fork, rebuild, mod, host, bundle into
something else, ship as part of a larger product), you must:

1. **Preserve this `NOTICES.md` file** alongside the project's
   `LICENSE` file in your distribution.
2. **Publish the complete corresponding source** of your modified
   version under AGPL-3.0-or-later or a compatible license. For
   network-hosted deployments, that source must be reachable by your
   users.
3. **Pass through the CreativeML Open RAIL-M use restrictions** to
   the users of your image-generation pipeline. If you replace SD 1.5
   with a differently-licensed model, this obligation evaporates with
   it.
4. **Preserve the upstream attributions and license notices** for
   SillyTavern, Ollama, nginx, Python, PyTorch, HuggingFace libraries,
   and the model authors.

If you are unsure whether a use is permitted, read the linked license
texts. The author of The Remnant is not a lawyer, cannot give you
legal advice, and makes no representation about whether any particular
use qualifies as fair use or falls within a given license's
permissions.

---

*Last updated: 2026-04-05*
*Project version at time of writing: The Remnant 2.7.1 / Fortress runtime 2.8.0*
