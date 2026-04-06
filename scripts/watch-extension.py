#!/usr/bin/env python3
"""Watch extension/ for file changes and write a reload token to the status dir.

The Remnant extension polls /status/extension-version.json every second.
When the css_version hash changes it hot-swaps the stylesheet in-place.
When the js_version hash changes it triggers a full page reload.
Neither case requires the developer to touch the browser.

Usage (called automatically by native-up.sh):
  python scripts/watch-extension.py [ext_dir] [status_dir]

Defaults:
  ext_dir    = <repo_root>/extension
  status_dir = <repo_root>/scripts/splash/status
"""
from __future__ import annotations
import hashlib, json, os, sys, time


def _md5(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except OSError:
        return ""


def _snapshot(ext_dir: str) -> tuple[str, str]:
    """Return (js_hash, css_hash) for files in ext_dir."""
    js_parts: list[str] = []
    css_parts: list[str] = []
    try:
        for name in sorted(os.listdir(ext_dir)):
            path = os.path.join(ext_dir, name)
            if not os.path.isfile(path):
                continue
            if name.endswith(".js"):
                js_parts.append(_md5(path))
            elif name.endswith(".css"):
                css_parts.append(_md5(path))
    except OSError:
        pass
    js_hash  = hashlib.md5("|".join(js_parts).encode()).hexdigest()[:12]
    css_hash = hashlib.md5("|".join(css_parts).encode()).hexdigest()[:12]
    return js_hash, css_hash


def _write_token(status_dir: str, js_version: str, css_version: str) -> None:
    os.makedirs(status_dir, exist_ok=True)
    token_path = os.path.join(status_dir, "extension-version.json")
    tmp = token_path + ".tmp"
    payload = {"js_version": js_version, "css_version": css_version,
               "ts": int(time.time())}
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, token_path)


def main() -> None:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ext_dir    = sys.argv[1] if len(sys.argv) > 1 else os.path.join(repo_root, "extension")
    status_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.join(repo_root, "scripts", "splash", "status")

    js_ver, css_ver = _snapshot(ext_dir)
    _write_token(status_dir, js_ver, css_ver)
    print(f"[watch-ext] watching {ext_dir}")
    print(f"[watch-ext] initial  js={js_ver} css={css_ver}")

    try:
        while True:
            time.sleep(0.6)
            new_js, new_css = _snapshot(ext_dir)
            if new_js != js_ver or new_css != css_ver:
                changed = []
                if new_js  != js_ver:  changed.append(f"js={new_js}")
                if new_css != css_ver: changed.append(f"css={new_css}")
                js_ver, css_ver = new_js, new_css
                _write_token(status_dir, js_ver, css_ver)
                print(f"[watch-ext] changed  {' '.join(changed)}")
    except KeyboardInterrupt:
        print("[watch-ext] stopped")


if __name__ == "__main__":
    main()
