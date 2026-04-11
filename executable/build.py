#!/usr/bin/env python3
"""Build Remnant.exe via PyInstaller.

Usage:
    pip install pyinstaller psutil pywebview
    python executable/build.py

Produces: dist/Remnant.exe  (~25-40 MB)
"""

import subprocess
import sys
from pathlib import Path

ROOT         = Path(__file__).parent.parent.resolve()
LAUNCHER     = ROOT / "executable" / "remnant_launcher.py"
HARDWARE     = ROOT / "executable" / "hardware.py"
VERSION_JSON = ROOT / "version.json"
DIST         = ROOT / "dist"

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--onefile",
    "--name", "Remnant",
    "--noconsole",                             # no launcher console window — logs go to logs/
    "--add-data", f"{HARDWARE};.",             # hardware.py at root of _MEIPASS (Windows: ;)
    *([f"--add-data", f"{VERSION_JSON};."] if VERSION_JSON.exists() else []),
    "--hidden-import", "psutil",
    "--hidden-import", "psutil._pswindows",
    "--hidden-import", "psutil._psutil_windows",
    "--collect-all", "pywebview",              # WebView2 backend + all submodules
    "--distpath", str(DIST),
    "--workpath", str(ROOT / "build" / "pyinstaller"),
    "--specpath", str(ROOT / "build"),
    str(LAUNCHER),
]

print("Building Remnant.exe...")
print(f"  Source : {LAUNCHER}")
print(f"  Output : {DIST / 'Remnant.exe'}")
print()

result = subprocess.run(cmd, cwd=ROOT)
if result.returncode == 0:
    exe = DIST / "Remnant.exe"
    size_mb = exe.stat().st_size / (1024 * 1024) if exe.exists() else 0
    print(f"\n  Remnant.exe built ({size_mb:.1f} MB) → {exe}")
else:
    print("\n  Build failed — see output above.", file=sys.stderr)
    sys.exit(1)
