#!/usr/bin/env python3
"""
Remnant Fortress — Windows Launcher
Configuration-free entry point for the Remnant AI game.

Manages all native services without requiring Docker:
  ollama      :1593  language model (LLM)
  flask-sd    :1592  image generation
  flask-music :1596  ambient music (MusicGen)
  sillytavern :1590  chat/narrative engine
  diag        :1591  diagnostics sidecar
  nginx       :1582  reverse proxy (the only exposed port)

Usage:
    python executable/remnant_launcher.py          # normal start
    python executable/remnant_launcher.py --setup  # first-run setup only
    python executable/remnant_launcher.py --status # show status and exit

Packaged into Remnant.exe via:
    python executable/build.py
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

# ── Paths ─────────────────────────────────────────────────────────────────────

def _find_repo_root() -> Path:
    """Return the repo root. Works whether running from source or packaged exe."""
    if getattr(sys, "frozen", False):
        # PyInstaller: exe is in the same directory as Remnant.exe
        return Path(sys.executable).parent
    # Running from source: executable/remnant_launcher.py → repo root
    return Path(__file__).parent.parent.resolve()

REPO_ROOT  = _find_repo_root()
APP_DIR    = Path(os.environ.get("LOCALAPPDATA", REPO_ROOT)) / "Remnant"
# All runtime artifacts live flat under REPO_ROOT (gitignored: /status/, /logs/, /bin/)
STATUS_DIR = REPO_ROOT / "status"
RUN_DIR    = REPO_ROOT / "logs" / "native-run"
BIN_DIR    = REPO_ROOT / "bin"

# Port assignments (canonical — matches port-layout golden rule)
PORTS = {
    "nginx":       1582,
    "sillytavern": 1590,
    "diag":        1591,
    "flask-sd":    1592,
    "ollama":      1593,
    "flask-music": 1596,
}

OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:14b")

# ── ANSI console helpers ───────────────────────────────────────────────────────
# Works on Windows 10+ (ENABLE_VIRTUAL_TERMINAL_PROCESSING).

_USE_COLOUR = sys.stdout.isatty() and platform.system() == "Windows" or os.environ.get("FORCE_COLOR")

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOUR else text

def green(t):  return _c("32", t)
def yellow(t): return _c("33", t)
def red(t):    return _c("31", t)
def cyan(t):   return _c("36", t)
def bold(t):   return _c("1",  t)
def dim(t):    return _c("2",  t)

def log(msg: str, level: str = "info"):
    prefix = {
        "info":  f"[{dim('·')}]",
        "ok":    f"[{green('✓')}]",
        "warn":  f"[{yellow('!')}]",
        "error": f"[{red('✗')}]",
        "head":  f"[{cyan('→')}]",
    }.get(level, "[·]")
    print(f"  {prefix} {msg}", flush=True)


# ── Hardware detection (thin wrapper around hardware.py) ──────────────────────

def _load_hardware_module():
    """Import hardware.py from executable/ — works in source and packaged mode."""
    try:
        import importlib.util
        hw_path = Path(__file__).parent / "hardware.py"
        spec = importlib.util.spec_from_file_location("hardware", hw_path)
        mod = importlib.util.module_from_spec(spec)
        # Must register in sys.modules before exec_module so @dataclass can
        # resolve annotation types via sys.modules[cls.__module__] (Python 3.12+).
        sys.modules["hardware"] = mod
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:
        log(f"hardware.py load failed: {e}", "warn")
        return None


def detect_and_show_hardware() -> dict:
    """Detect hardware, print summary, write hardware-profile.json. Returns status dict."""
    STATUS_DIR.mkdir(parents=True, exist_ok=True)   # ensure dir exists regardless of hw detection
    hw_mod = _load_hardware_module()
    if hw_mod is None:
        return {}

    hw = hw_mod.detect()
    print()
    print(bold("  ┌─ Hardware Profile ──────────────────────────────────────────┐"))
    for line in hw_mod.format_summary(hw).splitlines():
        print(f"  │  {line}")
    print(bold("  └─────────────────────────────────────────────────────────────┘"))
    print()

    # Write to status dir so the splash can read it
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    profile_path = STATUS_DIR / "hardware-profile.json"
    _atomic_write(profile_path, json.dumps(hw_mod.to_status_dict(hw), indent=2))
    return hw_mod.to_status_dict(hw)


def _atomic_write(path: Path, content: str):
    """Write content atomically via tmp+rename."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


# ── Port / process checks ──────────────────────────────────────────────────────

def port_open(port: int) -> bool:
    """Return True if something is listening on localhost:port."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def http_get(url: str, timeout: float = 5.0) -> Optional[dict]:
    """GET url, return parsed JSON or None."""
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


# ── Dependency resolution ──────────────────────────────────────────────────────

def _find_exe(names: list[str], extra_dirs: list[Path] = None) -> Optional[Path]:
    """Locate an exe in PATH or extra_dirs."""
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)
    if extra_dirs:
        for d in extra_dirs:
            for name in names:
                p = d / name
                if p.exists():
                    return p
    return None


def _find_ollama() -> Optional[Path]:
    return _find_exe(
        ["ollama", "ollama.exe"],
        extra_dirs=[
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama",
            Path("C:/Program Files/Ollama"),
        ],
    )


def _find_node() -> Optional[Path]:
    return _find_exe(
        ["node", "node.exe"],
        extra_dirs=[BIN_DIR / "node"],
    )


def _find_nginx() -> Optional[Path]:
    # Check PATH, WinGet packages, and our own bin dir
    p = _find_exe(["nginx", "nginx.exe"])
    if p:
        return p
    winget_base = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if winget_base.exists():
        for candidate in winget_base.rglob("nginx.exe"):
            return candidate
    return _find_exe(["nginx", "nginx.exe"], extra_dirs=[BIN_DIR / "nginx"])


def _find_st_dir() -> Optional[Path]:
    """Locate SillyTavern installation directory."""
    candidates = [
        REPO_ROOT / "SillyTavern",                          # bundled
        Path("C:/SillyTavern"),
        Path(os.environ.get("USERPROFILE", "")) / "SillyTavern",
        Path(os.environ.get("APPDATA", "")) / "SillyTavern",
    ]
    for c in candidates:
        if (c / "server.js").exists():
            return c
    return None


def _find_python() -> Optional[Path]:
    """Return a Python interpreter suitable for running Flask service scripts.

    In source mode: the current interpreter is already Python, so use it.
    In frozen mode (Remnant.exe): sys.executable IS the exe, not a Python
    interpreter. We must find a real python in PATH or the bundled winget
    install location instead.
    """
    if not getattr(sys, "frozen", False):
        return Path(sys.executable)
    # Frozen: find a real Python on PATH
    for name in ["python", "python3", "py"]:
        found = shutil.which(name)
        if found:
            return Path(found)
    # Last resort: winget default Python location
    winget_py = (Path(os.environ.get("LOCALAPPDATA", "")) /
                 "Microsoft" / "WindowsApps" / "python3.exe")
    if winget_py.exists():
        return winget_py
    return None


# ── First-run setup ────────────────────────────────────────────────────────────

def _winget(package_id: str, display_name: str):
    """Install a package via winget (silent, no interaction)."""
    log(f"installing {display_name} via winget...", "head")
    result = subprocess.run(
        ["winget", "install", "--id", package_id, "--silent", "--accept-package-agreements",
         "--accept-source-agreements"],
        capture_output=False,
    )
    if result.returncode != 0:
        log(f"winget install {package_id} failed (exit {result.returncode})", "warn")
    else:
        log(f"{display_name} installed", "ok")


def _download(url: str, dest: Path, label: str):
    """Download url to dest, showing a progress bar."""
    log(f"downloading {label}...")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            chunk = 65536
            with open(dest, "wb") as f:
                while True:
                    data = resp.read(chunk)
                    if not data:
                        break
                    f.write(data)
                    done += len(data)
                    if total:
                        pct = done * 100 // total
                        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
                        print(f"\r    [{bar}] {pct:3d}%", end="", flush=True)
        print()
        log(f"{label} downloaded", "ok")
    except Exception as e:
        log(f"download failed: {e}", "error")
        raise


def _extract_zip(src: Path, dest: Path):
    """Extract a zip archive."""
    import zipfile
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src, "r") as z:
        z.extractall(dest)


def _ensure_cache_junctions():
    """Junction system tool default dirs → local-cache/ so everything writes there.

    This makes bare `ollama pull` and `huggingface-cli download` (run outside the
    launcher) land in local-cache automatically, without needing env vars.
    Docker uses bind-mounts to the same local-cache/ subdirs.

    Junctions are only created when the target path doesn't exist yet.
    If the user already has data there (real directory), we leave it alone
    and rely on env vars set at launch time instead.
    """
    LOCAL_CACHE = REPO_ROOT / "local-cache"
    user = Path(os.environ.get("USERPROFILE", ""))
    junctions = [
        # (link_path,                                      target)
        (user / ".ollama",                                LOCAL_CACHE / "ollama-data"),
        (user / ".cache" / "huggingface",                 LOCAL_CACHE / "hf-cache"),
    ]
    for link, target in junctions:
        target.mkdir(parents=True, exist_ok=True)
        # Already a junction pointing to target — nothing to do
        if link.is_symlink() or (link.exists() and link.is_junction() if hasattr(link, "is_junction") else False):
            log(f"  cache junction already exists: {link.name}", "ok")
            continue
        # Real directory with content — don't touch it, env vars handle redirection
        if link.exists():
            log(f"  {link.name} is a real directory — using env vars for redirection", "warn")
            continue
        # Doesn't exist — create junction
        link.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run([
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            f"New-Item -ItemType Junction -Path '{link}' -Target '{target}' | Out-Null"
        ], capture_output=True)
        if result.returncode == 0:
            log(f"  junction created: {link} → {target}", "ok")
        else:
            log(f"  junction creation failed for {link.name} — env vars active", "warn")


def run_setup(args) -> bool:
    """First-run setup: install missing prerequisites. Returns True if ready."""
    print()
    print(bold("  ── First-run Setup ───────────────────────────────────────────"))
    print()

    ok = True

    # 0. Wire local-cache via junctions (idempotent)
    log("wiring local-cache junctions...", "head")
    _ensure_cache_junctions()
    print()

    # 1. Ollama
    ollama = _find_ollama()
    if ollama:
        log(f"ollama found: {ollama}", "ok")
    else:
        log("ollama not found", "warn")
        try:
            _winget("Ollama.Ollama", "Ollama")
            ollama = _find_ollama()
            if not ollama:
                log("ollama still not found after install — restart and try again", "error")
                ok = False
        except FileNotFoundError:
            log("winget not available — download Ollama from https://ollama.com/download", "error")
            ok = False

    # 2. Node.js
    node = _find_node()
    if node:
        log(f"Node.js found: {node}", "ok")
    else:
        log("Node.js not found", "warn")
        try:
            _winget("OpenJS.NodeJS.LTS", "Node.js LTS")
            node = _find_node()
            if not node:
                log("Node.js still not found after install — restart and try again", "error")
                ok = False
        except FileNotFoundError:
            log("winget not available — download Node.js from https://nodejs.org", "error")
            ok = False

    # 3. nginx
    nginx = _find_nginx()
    if nginx:
        log(f"nginx found: {nginx}", "ok")
    else:
        log("nginx not found — downloading portable build...", "warn")
        try:
            _winget("nginxinc.nginx", "nginx")
            nginx = _find_nginx()
        except FileNotFoundError:
            pass
        if not nginx:
            # Fallback: download portable nginx
            nginx_url = "https://nginx.org/download/nginx-1.26.2.zip"
            nginx_zip = BIN_DIR / "nginx.zip"
            _download(nginx_url, nginx_zip, "nginx for Windows")
            _extract_zip(nginx_zip, BIN_DIR / "nginx")
            nginx = _find_exe([], extra_dirs=[BIN_DIR / "nginx"])
            nginx_zip.unlink(missing_ok=True)
        if nginx:
            log(f"nginx ready: {nginx}", "ok")
        else:
            log("nginx setup failed — install via: winget install nginxinc.nginx", "error")
            ok = False

    # 4. SillyTavern
    st_dir = _find_st_dir()
    if st_dir:
        log(f"SillyTavern found: {st_dir}", "ok")
        # Ensure npm deps are installed
        if not (st_dir / "node_modules").exists():
            log("installing SillyTavern dependencies (npm install)...", "head")
            subprocess.run(["npm", "install"], cwd=st_dir, check=True)
    else:
        log("SillyTavern not found — cloning...", "warn")
        st_target = REPO_ROOT / "SillyTavern"
        try:
            subprocess.run(
                ["git", "clone", "--depth=1",
                 "https://github.com/SillyTavern/SillyTavern.git",
                 str(st_target)],
                check=True,
            )
            subprocess.run(["npm", "install"], cwd=st_target, check=True)
            log("SillyTavern installed", "ok")
        except Exception as e:
            log(f"SillyTavern install failed: {e}", "error")
            ok = False

    # 5. Python Flask service deps
    python = _find_python()
    flask_sd_reqs  = REPO_ROOT / "docker" / "flask-sd"  / "requirements.txt"
    flask_mus_reqs = REPO_ROOT / "docker" / "flask-music" / "requirements.txt"
    for label, reqs in [("flask-sd", flask_sd_reqs), ("flask-music", flask_mus_reqs)]:
        if reqs.exists():
            log(f"installing Python deps for {label}...", "head")
            subprocess.run(
                [str(python), "-m", "pip", "install", "-r", str(reqs), "--quiet"],
                check=False,
            )
            log(f"{label} Python deps installed", "ok")

    # 6. Pull Ollama model
    if ollama and ok:
        log(f"pulling Ollama model {OLLAMA_MODEL} (may take a while on first run)...", "head")
        subprocess.run([str(ollama), "pull", OLLAMA_MODEL], check=False)

    if ok:
        log("setup complete — run the launcher again to start the game", "ok")
    else:
        log("setup finished with errors — review messages above", "warn")
    return ok


# ── Service lifecycle ──────────────────────────────────────────────────────────

def _ensure_extension_junction(st_dir: Path):
    """Junction ST's image-generator slot to repo extension/ dir (idempotent)."""
    slot = st_dir / "public" / "scripts" / "extensions" / "image-generator"
    src  = REPO_ROOT / "extension"
    if not src.exists():
        return
    subprocess.run([
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        f"if (!(Test-Path '{slot}')) "
        f"{{ New-Item -ItemType Junction -Path '{slot}' -Target '{src}' | Out-Null }}"
    ], capture_output=True)


class ManagedProcess:
    def __init__(self, name: str, cmd: list[str], env: dict = None,
                 cwd: Path = None, log_file: Path = None):
        self.name     = name
        self.cmd      = cmd
        self.env      = {**os.environ, **(env or {})}
        self.cwd      = cwd or REPO_ROOT
        self.log_file = log_file or (RUN_DIR / f"{name}.log")
        self._proc: Optional[subprocess.Popen] = None

    def start(self) -> bool:
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.log_file, "w") as lf:
                self._proc = subprocess.Popen(
                    self.cmd,
                    env=self.env,
                    cwd=str(self.cwd),
                    stdout=lf, stderr=lf,
                    # Don't open a console window for background processes
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
            return True
        except Exception as e:
            log(f"{self.name}: failed to start: {e}", "error")
            return False

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass


class ServiceManager:
    def __init__(self):
        self._procs: list[ManagedProcess] = []
        self._nginx_proc: Optional[ManagedProcess] = None

    def _build_nginx_conf(self, nginx_path: Path) -> Optional[Path]:
        """Generate nginx.conf from the native template, return path or None."""
        template = REPO_ROOT / "scripts" / "native-nginx.conf"
        if not template.exists():
            log("native-nginx.conf not found", "error")
            return None

        # Locate nginx's own conf dir for mime.types
        nginx_dir = nginx_path.parent
        mime = None
        for candidate in [
            nginx_dir / "conf" / "mime.types",
            nginx_dir.parent / "conf" / "mime.types",
            Path("/etc/nginx/mime.types"),
        ]:
            if candidate.exists():
                mime = str(candidate).replace("\\", "/")
                break
        if not mime:
            log("mime.types not found — nginx may fail to start", "warn")
            mime = str(nginx_dir / "conf" / "mime.types").replace("\\", "/")

        def _p(p: Path) -> str:
            """Convert path to forward-slash string for nginx config."""
            return str(p).replace("\\", "/")

        STATUS_DIR.mkdir(parents=True, exist_ok=True)

        conf_text = template.read_text(encoding="utf-8")
        replacements = {
            "{{NGINX_PORT}}":         str(PORTS["nginx"]),
            "{{ST_UPSTREAM}}":        f"127.0.0.1:{PORTS['sillytavern']}",
            "{{FLASK_SD_UPSTREAM}}":  f"127.0.0.1:{PORTS['flask-sd']}",
            "{{OLLAMA_UPSTREAM}}":    f"127.0.0.1:{PORTS['ollama']}",
            "{{DIAG_UPSTREAM}}":      f"127.0.0.1:{PORTS['diag']}",
            "{{TTS_UPSTREAM}}":       f"127.0.0.1:1594",
            "{{STT_UPSTREAM}}":       f"127.0.0.1:1595",
            "{{FLASK_MUSIC_UPSTREAM}}": f"127.0.0.1:{PORTS['flask-music']}",
            "{{SPLASH_ROOT}}":        _p(REPO_ROOT / "scripts" / "splash"),
            "{{DIAG_HTML_DIR}}":      _p(REPO_ROOT / "docker" / "nginx"),
            "{{GAME_HTML_DIR}}":      _p(REPO_ROOT / "web"),
            "{{STATUS_DIR}}":         _p(STATUS_DIR),
            "{{CACHE_DIR}}":          _p(REPO_ROOT / "local-cache" / "nginx-cache"),
            "{{MIME_TYPES}}":         mime,
            "{{NGINX_PID_FILE}}":     _p(RUN_DIR / "nginx.pid"),
            "{{NGINX_ERROR_LOG}}":    _p(RUN_DIR / "nginx-error.log"),
            "{{NGINX_ACCESS_LOG}}":   _p(RUN_DIR / "nginx-access.log"),
        }
        for k, v in replacements.items():
            conf_text = conf_text.replace(k, v)

        conf_path = RUN_DIR / "nginx.conf"
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        conf_path.write_text(conf_text, encoding="utf-8")
        return conf_path

    def start_all(self) -> bool:
        """Start all services. Returns True if nginx comes up."""
        python = _find_python()
        ollama = _find_ollama()
        node   = _find_node()
        nginx  = _find_nginx()
        st_dir = _find_st_dir()

        missing = []
        if not python:  missing.append("Python 3.10+ (run: winget install Python.Python.3.12)")
        if not ollama:  missing.append("ollama (run: winget install Ollama.Ollama)")
        if not node:    missing.append("Node.js (run: winget install OpenJS.NodeJS.LTS)")
        if not nginx:   missing.append("nginx (run: winget install nginxinc.nginx)")
        if not st_dir:  missing.append("SillyTavern (run with --setup to auto-install)")
        if missing:
            log("missing prerequisites:", "error")
            for m in missing:
                log(f"  • {m}", "error")
            log("run with --setup to install automatically", "warn")
            return False

        # Kill any stale nginx
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/IM", "nginx.exe", "/T"],
                           capture_output=True)
            time.sleep(0.5)

        STATUS_DIR.mkdir(parents=True, exist_ok=True)
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        (REPO_ROOT / "local-cache" / "nginx-cache").mkdir(parents=True, exist_ok=True)

        # Shared local cache — same directory used by docker bind-mounts and
        # native mode, so models are downloaded once and reused across all runners.
        LOCAL_CACHE = REPO_ROOT / "local-cache"
        LOCAL_CACHE.mkdir(parents=True, exist_ok=True)
        (LOCAL_CACHE / "ollama-data").mkdir(exist_ok=True)
        (LOCAL_CACHE / "hf-cache").mkdir(exist_ok=True)

        env_base = {
            **dict(os.environ),
            # HuggingFace model cache (flask-sd, flask-music)
            "HF_HOME":             str(LOCAL_CACHE / "hf-cache"),
            "TRANSFORMERS_CACHE":  str(LOCAL_CACHE / "hf-cache"),
            "HF_HUB_CACHE":        str(LOCAL_CACHE / "hf-cache"),
        }

        # ── ollama ──────────────────────────────────────────────────────────
        if not port_open(PORTS["ollama"]):
            log("starting ollama...", "head")
            p = ManagedProcess("ollama", [str(ollama), "serve"],
                env={**env_base,
                     "OLLAMA_HOST":   f"127.0.0.1:{PORTS['ollama']}",
                     "OLLAMA_MODELS": str(LOCAL_CACHE / "ollama-data" / "models")},
                log_file=RUN_DIR / "ollama.log")
            p.start()
            self._procs.append(p)
        else:
            log(f"ollama already on :{PORTS['ollama']}", "ok")

        # ── extension junction (must precede ST startup) ─────────────────────
        _ensure_extension_junction(st_dir)

        # ── SillyTavern ─────────────────────────────────────────────────────
        if not port_open(PORTS["sillytavern"]):
            log("starting SillyTavern...", "head")
            p = ManagedProcess(
                "sillytavern",
                [str(node), "server.js", "--port", str(PORTS["sillytavern"])],
                cwd=st_dir,
                log_file=RUN_DIR / "sillytavern.log",
            )
            p.start()
            self._procs.append(p)
        else:
            log(f"SillyTavern already on :{PORTS['sillytavern']}", "ok")

        # ── diag ────────────────────────────────────────────────────────────
        if not port_open(PORTS["diag"]):
            log("starting diag sidecar...", "head")
            p = ManagedProcess(
                "diag",
                [str(python), str(REPO_ROOT / "docker" / "diag" / "app.py")],
                env={
                    **env_base,
                    "STATUS_DIR":       str(STATUS_DIR),
                    "FLASK_SD_URL":     f"http://127.0.0.1:{PORTS['flask-sd']}",
                    "OLLAMA_URL":       f"http://127.0.0.1:{PORTS['ollama']}",
                    "FLASK_MUSIC_URL":  f"http://127.0.0.1:{PORTS['flask-music']}",
                    "SILLYTAVERN_URL":  f"http://127.0.0.1:{PORTS['sillytavern']}",
                    "OLLAMA_MODEL":     OLLAMA_MODEL,
                    "LISTEN_PORT":      str(PORTS["diag"]),
                },
            )
            p.start()
            self._procs.append(p)
        else:
            log(f"diag already on :{PORTS['diag']}", "ok")

        # ── flask-sd ─────────────────────────────────────────────────────────
        if not port_open(PORTS["flask-sd"]):
            # Native flask-sd is backend/image_generator_api.py (not docker image)
            flask_sd_app = REPO_ROOT / "backend" / "image_generator_api.py"
            if flask_sd_app.exists():
                log("starting flask-sd (image generation)...", "head")
                p = ManagedProcess(
                    "flask-sd",
                    [str(python), str(flask_sd_app)],
                    env={
                        **env_base,
                        "FLASK_PORT": str(PORTS["flask-sd"]),
                    },
                )
                p.start()
                self._procs.append(p)
            else:
                log("backend/image_generator_api.py not found — image generation unavailable", "warn")
        else:
            log(f"flask-sd already on :{PORTS['flask-sd']}", "ok")

        # ── flask-music ──────────────────────────────────────────────────────
        if not port_open(PORTS["flask-music"]):
            flask_music_app = REPO_ROOT / "docker" / "flask-music" / "app.py"
            if flask_music_app.exists():
                log("starting flask-music (ambient music)...", "head")
                p = ManagedProcess(
                    "flask-music",
                    [str(python), str(flask_music_app)],
                    env={
                        **env_base,
                        "LISTEN_PORT":     str(PORTS["flask-music"]),
                        "HF_HUB_OFFLINE":  "0",
                    },
                )
                p.start()
                self._procs.append(p)
            else:
                log("flask-music/app.py not found — music generation unavailable", "warn")
        else:
            log(f"flask-music already on :{PORTS['flask-music']}", "ok")

        # ── nginx (foreground, needed for port 1582) ─────────────────────────
        log("starting nginx gateway on :1582...", "head")
        conf_path = self._build_nginx_conf(nginx)
        if not conf_path:
            return False
        nginx_dir = str(nginx.parent)
        p = ManagedProcess(
            "nginx",
            [str(nginx), "-p", nginx_dir, "-c", str(conf_path)],
            log_file=RUN_DIR / "nginx-launcher.log",
        )
        p.start()
        self._nginx_proc = p
        self._procs.append(p)

        # Wait for nginx to come up
        for _ in range(20):
            if port_open(PORTS["nginx"]):
                log(f"nginx gateway up on :{PORTS['nginx']}", "ok")
                break
            time.sleep(0.5)
        else:
            log("nginx did not come up in 10s — check logs/nginx-error.log", "error")
            return False

        return True

    def stop_all(self):
        # Stop nginx first (sends SIGTERM to worker processes on Windows)
        nginx = _find_nginx()
        if nginx:
            nginx_dir = str(nginx.parent)
            subprocess.run([str(nginx), "-p", nginx_dir, "-s", "quit"],
                           capture_output=True)
        for p in reversed(self._procs):
            p.stop()
        # Belt-and-suspenders: kill any stale nginx.exe
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/IM", "nginx.exe", "/T"],
                           capture_output=True)

    def status(self) -> list[dict]:
        results = []
        for name, port in PORTS.items():
            up = port_open(port)
            results.append({"service": name, "port": port, "up": up})
        return results


# ── Service health wait ────────────────────────────────────────────────────────

def _wait_for_port(port: int, name: str, timeout: int = 60) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        if port_open(port):
            return True
        time.sleep(1)
    log(f"{name} did not come up on :{port} within {timeout}s", "warn")
    return False


def _stamp_status(filename: str, phase: str, detail: str = ""):
    """Write a minimal status JSON to the splash status dir."""
    path = STATUS_DIR / filename
    _atomic_write(path, json.dumps({
        "service": filename.replace(".json", ""),
        "phase": phase,
        "detail": detail or None,
        "models": [],
    }, indent=2))


# ── Console banner ─────────────────────────────────────────────────────────────

BANNER = r"""
  ██████╗ ███████╗███╗   ███╗███╗   ██╗ █████╗ ███╗   ██╗████████╗
  ██╔══██╗██╔════╝████╗ ████║████╗  ██║██╔══██╗████╗  ██║╚══██╔══╝
  ██████╔╝█████╗  ██╔████╔██║██╔██╗ ██║███████║██╔██╗ ██║   ██║
  ██╔══██╗██╔══╝  ██║╚██╔╝██║██║╚██╗██║██╔══██║██║╚██╗██║   ██║
  ██║  ██║███████╗██║ ╚═╝ ██║██║ ╚████║██║  ██║██║ ╚████║   ██║
  ╚═╝  ╚═╝╚══════╝╚═╝     ╚═╝╚═╝  ╚═══╝╚═╝  ╚═╝╚═╝  ╚═══╝   ╚═╝
"""


def print_banner():
    """Print the Remnant ASCII banner to stdout."""
    # Enable ANSI on Windows 10+
    if sys.platform == "win32":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.SetConsoleMode(k32.GetStdHandle(-11), 7)
        except Exception:
            pass
    print(cyan(BANNER))
    print(bold("  The Remnant Fortress — Launcher"))
    print(dim(f"  Repo: {REPO_ROOT}"))
    print()


# ── Browser / window helpers ──────────────────────────────────────────────────

def _run_webview(url: str, sm: "ServiceManager"):
    """Open a frameless WebView2 window. Blocks on the main thread until closed."""
    import webview  # pywebview — requires WebView2 (pre-installed on Win10/11)

    ico = REPO_ROOT / "web" / "assets" / "favicon.ico"
    win = webview.create_window(
        "The Remnant",
        url,
        frameless=True,
        easy_drag=True,
        resizable=True,
        min_size=(1024, 768),
        icon=str(ico) if ico.exists() else None,
    )

    def on_closed():
        sm.stop_all()

    win.events.closed += on_closed
    webview.start()  # blocks until the window is closed
    sys.exit(0)


def _open_app_window(url: str, sm: "ServiceManager"):
    """Open game in a standalone app window — own taskbar entry, no URL bar.

    Uses Edge or Chrome in --app mode. Both use the same WebView2 engine as
    pywebview. Runs in a clean separate profile so the user's browser sessions
    are unaffected. Falls back to system browser only as last resort.
    """
    import shutil

    # Isolated profile dir so the game window is always separate
    profile_dir = REPO_ROOT / "local-cache" / "edge-game-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    app_args_edge = [
        f"--app={url}",
        f"--user-data-dir={profile_dir}",
        "--window-size=1280,800",
        "--no-first-run",
        "--disable-extensions",
    ]

    # Edge (msedge) — pre-installed on all Win10/11
    lappdata = Path(os.environ.get("LOCALAPPDATA", ""))
    progfiles   = Path(os.environ.get("PROGRAMFILES",       "C:/Program Files"))
    progfiles86 = Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"))
    edge_candidates = [
        lappdata   / "Microsoft/Edge/Application/msedge.exe",
        progfiles86 / "Microsoft/Edge/Application/msedge.exe",
        progfiles   / "Microsoft/Edge/Application/msedge.exe",
    ]
    edge_which = shutil.which("msedge")
    if edge_which:
        edge_candidates.insert(0, Path(edge_which))

    for edge_path in edge_candidates:
        if edge_path.exists():
            log(f"opening standalone window via Edge app mode", "head")
            subprocess.Popen([str(edge_path)] + app_args_edge)
            _wait_loop(sm)
            return

    # Chrome app mode fallback
    chrome_candidates = [
        progfiles   / "Google/Chrome/Application/chrome.exe",
        progfiles86 / "Google/Chrome/Application/chrome.exe",
        lappdata    / "Google/Chrome/Application/chrome.exe",
    ]
    chrome_which = shutil.which("chrome")
    if chrome_which:
        chrome_candidates.insert(0, Path(chrome_which))

    for chrome_path in chrome_candidates:
        if chrome_path.exists():
            log(f"opening standalone window via Chrome app mode", "head")
            subprocess.Popen([str(chrome_path), f"--app={url}",
                              f"--user-data-dir={profile_dir}"])
            _wait_loop(sm)
            return

    # Last resort: system browser (opens in existing browser — dev/docker only)
    import webbrowser
    log("no standalone browser found — opening in system browser", "warn")
    webbrowser.open(url)
    _wait_loop(sm)


def _wait_loop(sm: "ServiceManager"):
    """Console keep-alive for --no-browser / headless mode. Ctrl+C stops all."""
    def _shutdown(sig, frame):
        print()
        log("shutting down...", "head")
        sm.stop_all()
        log("all services stopped", "ok")
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print(dim("  Press Ctrl+C to stop all services."))
    try:
        while True:
            time.sleep(10)
            if sm._nginx_proc and not sm._nginx_proc.alive():
                log("nginx exited unexpectedly — check logs/native-run/nginx-error.log", "error")
    except KeyboardInterrupt:
        _shutdown(None, None)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Remnant Fortress Windows Launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--setup", action="store_true",
                        help="Run first-time setup (install prerequisites, pull models)")
    parser.add_argument("--status", action="store_true",
                        help="Show service status and exit")
    parser.add_argument("--no-browser", action="store_true",
                        help="Don't open browser automatically")
    parser.add_argument("--no-colour", action="store_true",
                        help="Disable ANSI colour output")
    args = parser.parse_args()

    if args.no_colour:
        global _USE_COLOUR
        _USE_COLOUR = False

    print_banner()

    # ── Status-only mode ────────────────────────────────────────────────────
    if args.status:
        sm = ServiceManager()
        statuses = sm.status()
        for s in statuses:
            icon = green("●") if s["up"] else red("○")
            print(f"  {icon}  {s['service']:<14} :{s['port']}")
        return 0

    # ── Setup mode ──────────────────────────────────────────────────────────
    if args.setup:
        detect_and_show_hardware()
        ok = run_setup(args)
        return 0 if ok else 1

    # ── Normal launch ───────────────────────────────────────────────────────
    hw_profile = detect_and_show_hardware()

    log("starting services...", "head")
    sm = ServiceManager()

    # Write pending status so splash shows something immediately
    for svc in ["flask-sd", "ollama"]:
        _stamp_status(f"{svc}.json", "pending", "starting…")

    ok = sm.start_all()
    if not ok:
        sm.stop_all()
        return 1

    # Wait for critical services
    log("waiting for services to be ready...", "head")
    for name, port in [("diag", PORTS["diag"]), ("sillytavern", PORTS["sillytavern"])]:
        _wait_for_port(port, name, timeout=60)

    # Stamp flask-sd / ollama status
    _stamp_status("flask-sd.json", "ready" if port_open(PORTS["flask-sd"]) else "pending")
    _stamp_status("ollama.json",   "ready" if port_open(PORTS["ollama"])   else "pending")

    # Service status summary
    url = f"http://localhost:{PORTS['nginx']}/splash.html"
    print()
    print(bold("  ── Services ──────────────────────────────────────────────────"))
    for s in sm.status():
        icon = green("●") if s["up"] else yellow("○")
        note = "" if s["up"] else dim("  (will be available once models load)")
        print(f"  {icon}  {s['service']:<14} :{s['port']}{note}")
    print()

    # Performance reminder
    if hw_profile.get("perf_tier"):
        tier = hw_profile["perf_tier"]
        print(f"  {dim('Expected response time:')} {bold(tier['min_s'])}-{bold(tier['max_s'])} s  "
              f"{dim('(' + tier['label'] + ')')}")
        print()

    # ── Open game window ────────────────────────────────────────────────────
    if args.no_browser:
        log(f"services up — access at {cyan(url)}", "ok")
        _wait_loop(sm)
        return 0

    log(f"opening game window: {cyan(url)}", "head")
    try:
        _run_webview(url, sm)   # pywebview: frameless, own taskbar entry (preferred)
    except ImportError:
        _open_app_window(url, sm)  # Edge/Chrome --app: own taskbar entry, no URL bar
    return 0


if __name__ == "__main__":
    sys.exit(main())
