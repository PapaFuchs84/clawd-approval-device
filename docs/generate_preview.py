#!/usr/bin/env python3
"""Renders PNG preview mockups of each display state, using the real
generated sprite data (firmware/include/sprites.h) and the exact colors and
layout from firmware/src/main.cpp. Useful for documentation screenshots
without needing physical hardware on hand.

Usage:
    python3 -m venv .venv && .venv/bin/pip install Pillow
    .venv/bin/python generate_preview.py
Writes PNGs to docs/images/.
"""
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).parent.parent
SPRITES_H = REPO_ROOT / "firmware" / "include" / "sprites.h"
OUT_DIR = Path(__file__).parent / "images"

# Display geometry, matches firmware/src/main.cpp exactly
DISPLAY_W, DISPLAY_H = 240, 135
SCALE = 3  # upscale for readability in the README
BAR_H = 22
SPRITE_SIZE = 64
SPRITE_X, SPRITE_Y = 6, 30
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_PATH_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

COLOR_IDLE = (0x38, 0x38, 0x38)
COLOR_WORKING = (0x00, 0xA8, 0xF8)
COLOR_WAITING = (0xFF, 0xA5, 0x00)
COLOR_SUCCESS = (0x00, 0xE0, 0x40)
COLOR_ERROR = (0xFF, 0x00, 0x00)
COLOR_DISCONNECTED = (0x7B, 0xEF, 0x7B)
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)

# state -> (label, bar color, badge type, badge color, sample summary text)
STATES = {
    "IDLE":              ("IDLE",     COLOR_IDLE,         None, None,           "Daemon connected"),
    "WORKING":           ("WORKING",  COLOR_WORKING,       None, None,           "Tool: Bash"),
    "WAITING_APPROVAL":  ("CONFIRM?", COLOR_WAITING,       "!",  COLOR_WAITING,   "Bash: rm -rf build/"),
    "SUCCESS":           ("DONE",     COLOR_SUCCESS,       "v",  COLOR_SUCCESS,   "Response finished"),
    "ERROR":             ("ERROR",    COLOR_ERROR,         None, None,           "Build failed: exit code 1"),
    "DISCONNECTED":      ("OFFLINE",  COLOR_DISCONNECTED,  "x",  COLOR_DISCONNECTED, "Daemon unreachable"),
}


def load_sprite(state: str) -> Image.Image:
    text = SPRITES_H.read_text()
    m = re.search(r"sprite_" + state + r"\[\d+\*\d+\] PROGMEM = \{(.*?)\};", text, re.S)
    vals = [int(v, 16) for v in re.findall(r"0x([0-9A-Fa-f]{4})", m.group(1))]
    img = Image.new("RGB", (SPRITE_SIZE, SPRITE_SIZE))
    px = img.load()
    for i, v in enumerate(vals):
        r = (v >> 11) & 0x1F
        g = (v >> 5) & 0x3F
        b = v & 0x1F
        px[i % SPRITE_SIZE, i // SPRITE_SIZE] = (r << 3, g << 2, b << 3)
    return img


def draw_badge(draw: ImageDraw.ImageDraw, cx, cy, kind, color):
    r = 9
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color, outline=BLACK, width=1)
    if kind == "!":
        draw.rectangle((cx - 1, cy - 5, cx + 1, cy + 1), fill=BLACK)
        draw.rectangle((cx - 1, cy + 3, cx + 1, cy + 5), fill=BLACK)
    elif kind == "v":
        draw.line((cx - 4, cy, cx - 1, cy + 3, cx + 4, cy - 3), fill=BLACK, width=2)
    elif kind == "x":
        draw.line((cx - 4, cy - 4, cx + 4, cy + 4), fill=BLACK, width=2)
        draw.line((cx - 4, cy + 4, cx + 4, cy - 4), fill=BLACK, width=2)


def wrap_text(draw, text, font, max_width):
    words = text.split(" ")
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) > max_width and cur:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def render_state(state: str) -> Image.Image:
    label, bar_color, badge, badge_color, summary = STATES[state]
    sprite = load_sprite(state)

    img = Image.new("RGB", (DISPLAY_W, DISPLAY_H), BLACK)
    draw = ImageDraw.Draw(img)

    draw.rectangle((0, 0, DISPLAY_W, BAR_H), fill=bar_color)
    label_font = ImageFont.truetype(FONT_PATH, 16)
    draw.text((6, 2), label, font=label_font, fill=BLACK)

    img.paste(sprite, (SPRITE_X, SPRITE_Y))
    if badge:
        draw_badge(draw, SPRITE_X + SPRITE_SIZE - 8, SPRITE_Y + SPRITE_SIZE - 8, badge, badge_color)

    text_font = ImageFont.truetype(FONT_PATH_REGULAR, 12)
    text_x = SPRITE_X + SPRITE_SIZE + 8
    max_width = DISPLAY_W - text_x - 4
    for i, line in enumerate(wrap_text(draw, summary, text_font, max_width)):
        draw.text((text_x, 32 + i * 14), line, font=text_font, fill=WHITE)

    return img.resize((DISPLAY_W * SCALE, DISPLAY_H * SCALE), Image.NEAREST)


def main():
    OUT_DIR.mkdir(exist_ok=True)
    for state in STATES:
        out = OUT_DIR / f"{state.lower()}.png"
        render_state(state).save(out)
        print(f"Wrote {out}")


if __name__ == "__main__":
    main()
