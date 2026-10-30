#!/usr/bin/env python3
"""Build the launcher's art assets from the source images in ../../img.

Writes TopSpin4Launcher/assets/:
  icon.png (512), icon.ico (16-256)       window / menu / AppImage / .exe icon
  header.png (760 wide)                   banner at the top of the launcher
  steam_grid.png (600x900)                Steam library cover
  steam_wide.png (920x430)                Steam wide capsule
  steam_hero.png (1920x620)               Steam game-page background
Needs Pillow at build time only; the launcher itself loads PNGs through Tk.
"""

import os

from PIL import Image, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
SRC = os.path.join(os.path.dirname(LAUNCHER), "img")
OUT = os.path.join(LAUNCHER, "assets")

ICON = "tennis_ball_icon_512.png"
MAIN_ART = "topspin4_mabec.png"           # wide key art with logo + players
COVER_ART = "topspin4_ball_mabec.png"     # used for the portrait cover


def cover(img, w, h, anchor_y=0.5):
    """Scale to fill w x h and crop; anchor_y picks which part survives."""
    scale = max(w / img.width, h / img.height)
    img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    left = (img.width - w) // 2
    top = round((img.height - h) * anchor_y)
    return img.crop((left, top, left + w, top + h))


def fit_on_blur(img, w, h):
    """Fit the whole image inside w x h over a blurred, darkened fill."""
    back = cover(img, w, h).filter(ImageFilter.GaussianBlur(24))
    back = Image.blend(back, Image.new("RGB", back.size), 0.35)
    scale = min(w / img.width, h / img.height)
    front = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    back.paste(front, ((w - front.width) // 2, (h - front.height) // 2))
    return back


def main():
    os.makedirs(OUT, exist_ok=True)
    icon = Image.open(os.path.join(SRC, ICON)).convert("RGBA")
    icon.save(os.path.join(OUT, "icon.png"))
    icon.save(os.path.join(OUT, "icon.ico"),
              sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])

    art = Image.open(os.path.join(SRC, MAIN_ART)).convert("RGB")
    cover(art, 760, 320, anchor_y=0.0).save(os.path.join(OUT, "header.png"), optimize=True)
    cover(art, 920, 430, anchor_y=0.0).save(os.path.join(OUT, "steam_wide.png"), optimize=True)
    cover(art, 1920, 620, anchor_y=0.15).save(os.path.join(OUT, "steam_hero.png"), optimize=True)

    portrait = Image.open(os.path.join(SRC, COVER_ART)).convert("RGB")
    fit_on_blur(portrait, 600, 900).save(os.path.join(OUT, "steam_grid.png"), optimize=True)

    for name in sorted(os.listdir(OUT)):
        print(f"{name:16s} {os.path.getsize(os.path.join(OUT, name)) // 1024:5d} KB")


if __name__ == "__main__":
    main()
