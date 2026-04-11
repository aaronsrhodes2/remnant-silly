#!/usr/bin/env python3
"""Build Remnant.exe via PyInstaller.

Usage:
    pip install pyinstaller psutil
    python launcher/build.py

Produces: dist/Remnant.exe  (~15-25 MB)
"""

import subprocess
import sys
from pathlib import Path

ROOT     = Path(__file__).parent.parent.resolve()
LAUNCHER = ROOT / "launcher" / "remnant_launcher.py"
HARDWARE = ROOT / "launcher" / "hardware.py"
DIST     = ROOT / "dist"

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--onefile",
    "--name", "Remnant",
    "--console",                               # keep console window (log output)
    "--add-data", f"{HARDWARE};launcher",      # hardware.py alongside exe (Windows: ;)
    "--hidden-import", "psutil",
    "--hidden-import", "psutil._pswindows",
    "--hidden-import", "psutil._psutil_windows",
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
