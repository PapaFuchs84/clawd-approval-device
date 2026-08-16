#!/usr/bin/env python3
"""Renders the Clawd Tank SVG sprites to PNG (via rsvg-convert) and converts
them to RGB565 C arrays for TFT_eSPI::pushImage(), writing them to
../include/sprites.h.

Requires: rsvg-convert (e.g. `apt install librsvg2-bin`) and Pillow
(`pip install Pillow`).

Source SVGs: marciogranzotto/clawd-tank, assets/svg-animations/ (MIT
license, see ../../THIRD_PARTY_NOTICES.md). These SVGs are CSS-animated;
rsvg-convert renders the static base pose (t=0) only, since it does not
evaluate CSS @keyframes. For some states (WAITING_APPROVAL, SUCCESS) the
static base pose is visually identical to the idle pose - the firmware
differentiates those with a colored status bar and a drawn badge icon
instead (see firmware/src/main.cpp).
"""
import subprocess
import sys
from pathlib import Path
from PIL import Image

SIZE = 64  # target size in px, square
SVG_DIR = Path(__file__).parent / "svg"
OUT_FILE = Path(__file__).parent.parent / "include" / "sprites.h"

# state -> source SVG stem (multiple states may share the same source)
STATES = {
    "IDLE": "clawd-idle-living",
    "WORKING": "clawd-working-typing",
    # notification/happy show no difference from the idle pose in the CSS
    # static frame (t=0) - expression only animates in over time - so reuse
    # the base crab; differentiation happens in main.cpp via color + badge.
    "WAITING_APPROVAL": "clawd-idle-living",
    "SUCCESS": "clawd-idle-living",
    "ERROR": "clawd-dizzy",
    "DISCONNECTED": "clawd-disconnected",
}


def render_svg_to_png(stem: str) -> Path:
    svg_path = SVG_DIR / f"{stem}.svg"
    png_path = SVG_DIR / f"{stem}.png"
    if not svg_path.exists():
        sys.exit(f"Missing source SVG: {svg_path}")
    subprocess.run(
        ["rsvg-convert", "-w", "200", "-h", "200", "--background-color=none",
         str(svg_path), "-o", str(png_path)],
        check=True,
    )
    return png_path


def rgb888_to_565(r, g, b):
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


def process(png_path: Path) -> list[int]:
    img = Image.open(png_path).convert("RGBA")
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)
    # Pad to a square so the character is centered
    w, h = img.size
    side = max(w, h)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(img, ((side - w) // 2, (side - h) // 2))
    square = square.resize((SIZE, SIZE), Image.LANCZOS)

    # Composite onto a black background (matches the display background)
    bg = Image.new("RGB", (SIZE, SIZE), (0, 0, 0))
    bg.paste(square, (0, 0), square)

    pixels = []
    for y in range(SIZE):
        for x in range(SIZE):
            r, g, b = bg.getpixel((x, y))
            pixels.append(rgb888_to_565(r, g, b))
    return pixels


def emit_c_array(name: str, pixels: list[int]) -> str:
    lines = [f"const uint16_t sprite_{name}[{SIZE}*{SIZE}] PROGMEM = {{"]
    for i in range(0, len(pixels), 16):
        chunk = pixels[i:i + 16]
        lines.append("  " + ",".join(f"0x{p:04X}" for p in chunk) + ",")
    lines.append("};")
    return "\n".join(lines)


def main():
    out = [
        "#pragma once",
        "// Auto-generated from Clawd Tank SVG assets (marciogranzotto/clawd-tank,",
        "// assets/svg-animations/) via tools/svg_to_rgb565.py. Static base pose per",
        "// state (SVG CSS animation is not evaluated when rendering).",
        "#include <Arduino.h>",
        f"#define SPRITE_SIZE {SIZE}",
        "",
    ]
    for state, stem in STATES.items():
        png = render_svg_to_png(stem)
        pixels = process(png)
        out.append(emit_c_array(state, pixels))
        out.append("")

    OUT_FILE.write_text("\n".join(out))
    print(f"Written: {OUT_FILE} ({OUT_FILE.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
