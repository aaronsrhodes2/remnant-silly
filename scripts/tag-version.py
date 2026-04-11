#!/usr/bin/env python3
"""tag-version.py — Stamp version.json from a live running stack.

Fetches the /signature composite from a running Remnant gateway and writes
version.json at the repo root.  Normally invoked by release-sanity.py after
cross-build verification succeeds; can also be run standalone.

USAGE:
    python -X utf8 scripts/tag-version.py [--base http://localhost:1582]
"""

from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
VERSION_FILE = ROOT / "version.json"


def _get_json(url: str, timeout: float = 10.0):
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        return None


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except Exception:
        return "unknown"


def _semver_from_git() -> str:
    """Try to extract semver from the last git tag (e.g. v3.5.0 → 3.5.0)."""
    try:
        tag = subprocess.check_output(
            ["git", "describe", "--tags", "--abbrev=0"], cwd=ROOT, text=True
        ).strip()
        return tag.lstrip("v")
    except Exception:
        return "0.0.0"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="http://localhost:1582",
                        help="Gateway URL (default: http://localhost:1582)")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    print(f"\n[tag-version] fetching /signature from {base} …")

    sig = _get_json(f"{base}/signature")
    if not sig:
        print("  FAIL: could not reach /signature — is the stack running?")
        return 1

    composite = sig.get("composite_sha256", "")
    if not composite:
        print("  FAIL: composite_sha256 missing from /signature response")
        return 1

    commit   = _git_commit()
    version  = _semver_from_git()
    now      = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    doc = {
        "version":          version,
        "composite_sha256": composite,
        "git_commit":       commit,
        "tagged_at":        now,
        "files":            sig.get("files", {}),
    }

    VERSION_FILE.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print(f"  composite : {composite[:32]}…")
    print(f"  version   : {version}  commit={commit}")
    print(f"  written   : {VERSION_FILE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
