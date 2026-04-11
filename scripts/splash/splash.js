// Remnant splash — polls /status/<service>.json and renders progress.
//
// Contract with downloaders (see download_flask_sd.py, download_ollama.sh):
//   {
//     "service": "flask-sd",
//     "phase": "pending" | "downloading" | "ready" | "error",
//     "models": [
//       {"name": "...", "license": "...", "bytes_done": N, "bytes_total": M}
//     ],
//     "error": null | "message",
//     "detail": null | "message"     // optional neutral in-progress line
//   }
//
// All three services must reach phase=ready before we redirect.
// Redirect target is `/` — nginx hosts ST at root (not under /app/)
// so that ST's absolute-path imports resolve to the same URL as its
// relative imports. In native dev without the proxy, override by
// setting window.REMNANT_APP_URL on the page before this script runs,
// or editing REDIRECT_TO below.

// Only services that actually download models appear here. The game
// client (SillyTavern) has no download phase — it's baked into its
// image — so it's not gated through the splash; nginx proxies to it
// after this splash redirects.
const SERVICES = [
    { id: "flask-sd", label: "The Sight-Kiln — vision and memory" },
    { id: "ollama",   label: "The Lexicon Engine — language and will" },
];

const POLL_INTERVAL_MS = 1000;
const REDIRECT_TO = window.REMNANT_APP_URL || "/";

// Initial placeholder shape for a service whose status file doesn't
// exist yet — lets the UI render something immediately.
function emptyState(id) {
    return { service: id, phase: "pending", models: [], error: null };
}

function humanBytes(n) {
    if (n == null || !isFinite(n)) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    let v = n;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function render(states) {
    const container = document.getElementById("services");
    container.innerHTML = "";

    let grandDone = 0;
    let grandTotal = 0;
    let allReady = true;

    for (const svc of SERVICES) {
        const state = states[svc.id] || emptyState(svc.id);
        if (state.phase !== "ready") allReady = false;

        const article = document.createElement("article");
        article.className = `service ${state.phase}`;

        const h2 = document.createElement("h2");
        const name = document.createElement("span");
        name.textContent = svc.label;
        const phase = document.createElement("span");
        phase.className = "phase";
        phase.textContent = state.phase;
        h2.append(name, phase);
        article.appendChild(h2);

        if (state.error) {
            const err = document.createElement("div");
            err.className = "error-msg";
            err.textContent = state.error;
            article.appendChild(err);
        } else if (state.detail) {
            // Neutral in-progress status (e.g. "probing flask-sd 12s/90s").
            // Only shown when there's no hard error, so an error message
            // always takes visual precedence.
            const det = document.createElement("div");
            det.className = "detail-msg";
            det.textContent = state.detail;
            article.appendChild(det);
        }

        const ul = document.createElement("ul");
        ul.className = "models";
        for (const m of state.models || []) {
            const done = m.bytes_done || 0;
            const total = m.bytes_total || 0;
            grandDone += done;
            grandTotal += total;

            const li = document.createElement("li");
            li.className = "model";

            const head = document.createElement("div");
            head.className = "model-head";
            const nm = document.createElement("span");
            nm.className = "model-name";
            nm.textContent = m.name;
            const lic = document.createElement("span");
            lic.className = "model-license";
            lic.textContent = m.license || "";
            head.append(nm, lic);

            const bar = document.createElement("div");
            bar.className = "progress";
            const fill = document.createElement("div");
            fill.className = "bar";
            const pct = total > 0 ? Math.min(100, (done / total) * 100) : (state.phase === "ready" ? 100 : 0);
            fill.style.width = `${pct}%`;
            bar.appendChild(fill);

            const bytes = document.createElement("div");
            bytes.className = "model-bytes";
            bytes.textContent = total > 0
                ? `${humanBytes(done)} / ${humanBytes(total)}`
                : (state.phase === "ready" ? "ready" : "waiting");

            li.append(head, bar, bytes);
            ul.appendChild(li);
        }
        if ((state.models || []).length === 0) {
            const li = document.createElement("li");
            li.className = "model";
            li.innerHTML = `<div class="model-head"><span class="model-name" style="font-style:italic;opacity:0.6">The forge is cold. Awaiting the heat of matter…</span></div>`;
            ul.appendChild(li);
        }
        article.appendChild(ul);
        container.appendChild(article);
    }

    // Overall bar.
    const overallPct = grandTotal > 0 ? (grandDone / grandTotal) * 100 : 0;
    document.getElementById("overall-bar").style.width = `${Math.min(100, overallPct)}%`;
    document.getElementById("overall-pct").textContent = `${Math.floor(overallPct)}%`;

    return allReady;
}

async function fetchStatus(id) {
    try {
        const res = await fetch(`status/${id}.json`, { cache: "no-store" });
        if (!res.ok) return emptyState(id);
        return await res.json();
    } catch (_) {
        return emptyState(id);
    }
}

async function tick() {
    const entries = await Promise.all(
        SERVICES.map(async s => [s.id, await fetchStatus(s.id)])
    );
    const states = Object.fromEntries(entries);
    const allReady = render(states);
    if (allReady) {
        // Small delay so the user sees the "ready" state before the jump.
        setTimeout(() => { window.location.href = REDIRECT_TO; }, 1200);
        return;
    }
    setTimeout(tick, POLL_INTERVAL_MS);
}

tick();

// ── Hardware profile — written by the Windows launcher before nginx starts ───
// Silently no-ops in Docker mode (file won't exist → fetch returns non-200).
async function loadHardwareProfile() {
    try {
        const r = await fetch("status/hardware-profile.json", { cache: "no-store" });
        if (!r.ok) return;
        const hw = await r.json();
        const tier = hw.perf_tier;
        if (!tier) return;
        const el = document.getElementById("hw-perf");
        if (!el) return;
        el.textContent = `${tier.label} · ${tier.min_s}–${tier.max_s}s per turn · ~${tier.tok_s} tok/s`;
        el.title = tier.note || "";
        // GPU line beneath
        if (hw.gpu_name && hw.gpu_name !== "Unknown") {
            const gpu = document.createElement("div");
            gpu.id = "hw-gpu";
            gpu.textContent = hw.gpu_vram_gb
                ? `${hw.gpu_name}  (${hw.gpu_vram_gb} GB VRAM)`
                : hw.gpu_name;
            el.insertAdjacentElement("afterend", gpu);
        }
    } catch (_) {}
}
loadHardwareProfile();
