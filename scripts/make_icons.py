"""Génère les icônes de l'appli (PNG) : python scripts/make_icons.py  (nécessite Pillow ; le résultat est commité)."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "web" / "static" / "icons"
TOP, BOTTOM = (92, 184, 255), (167, 139, 250)      # --accent -> --accent-2 (thème sombre)
SS = 4                                             # sur-échantillonnage


def gradient(size: int) -> Image.Image:
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * (size - 1))
            px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM))
    return img


def icon(size: int, rounded: bool, glyph_scale: float) -> Image.Image:
    big = size * SS
    base = gradient(big).convert("RGBA")
    d = ImageDraw.Draw(base)
    c, r = big / 2, big * glyph_scale / 2
    w = round(big * glyph_scale * 0.17)
    d.arc([c - r, c - r, c + r, c + r], start=-90, end=200, fill=(255, 255, 255, 255), width=w)   # anneau ouvert
    dot = w * 0.9
    d.ellipse([c - dot, c - dot, c + dot, c + dot], fill=(255, 255, 255, 255))                   # point central
    if rounded:
        mask = Image.new("L", (big, big), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, big - 1, big - 1], radius=big * 0.22, fill=255)
        base.putalpha(mask)
    return base.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    icon(192, True, 0.56).save(OUT / "icon-192.png")
    icon(512, True, 0.56).save(OUT / "icon-512.png")
    icon(512, False, 0.40).save(OUT / "icon-maskable-512.png")             # plein cadre, glyphe dans la zone sûre (80 %)
    icon(180, False, 0.56).convert("RGB").save(OUT / "apple-touch-icon.png")   # iOS arrondit lui-même, pas de transparence


if __name__ == "__main__":
    main()
