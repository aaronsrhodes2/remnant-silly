#!/usr/bin/env python3
"""gen-icon.py — Generate Remnant favicon assets.

Creates web/assets/favicon.ico (multi-size) and web/assets/favicon.png (512×512).
Run once after any icon design changes, then commit the output files.

Requires: pip install Pillow
"""

from PIL import Image, ImageDraw
from pathlib import Path

VOID  = (4,   4,  10, 255)   # #04040a — void black
AMBER = (212, 168, 75, 255)  # #d4a84b — golden amber


def make_frame(size: int) -> Image.Image:
    """Draw a hollow amber diamond on void black at the given pixel size."""
    img = Image.new("RGBA", (size, size), VOID)
    draw = ImageDraw.Draw(img)
    cx, cy = size / 2, size / 2

    # Outer diamond (amber fill)
    r = size * 0.40
    outer = [(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)]
    draw.polygon(outer, fill=AMBER)

    # Inner cutout (void black) — creates a fortress-window silhouette
    r2 = size * 0.18
    inner = [(cx, cy - r2), (cx + r2, cy), (cx, cy + r2), (cx - r2, cy)]
    draw.polygon(inner, fill=VOID)

    return img


def main():
    out = Path(__file__).parent.parent / "web" / "assets"
    out.mkdir(parents=True, exist_ok=True)

    sizes = [16, 32, 48, 256]
    frames = [make_frame(s) for s in sizes]

    ico_path = out / "favicon.ico"
    png_path = out / "favicon.png"

    # ICO: largest frame first, rest appended
    frames[-1].save(
        ico_path,
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=frames[:-1],
    )

    # PNG: 512×512 source
    make_frame(512).save(png_path, format="PNG")

    print(f"Written {ico_path}")
    print(f"Written {png_path}")


if __name__ == "__main__":
    main()
