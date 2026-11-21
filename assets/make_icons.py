"""Generate the ANIXO icons with Pillow only (no external image tools).

Run from anywhere::

    python assets/make_icons.py

Writes ``assets/anixo.ico`` (app) and ``assets/anixo-installer.ico``
(installer) with 16, 32, 48, 64, 128 and 256 pixel frames.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SIZES = (16, 32, 48, 64, 128, 256)
CANVAS = 256
ROOT = Path(__file__).resolve().parent

CHARCOAL_TOP = (26, 20, 46)
CHARCOAL_BOTTOM = (9, 9, 15)
VIOLET = (124, 58, 237)
VIOLET_LIGHT = (168, 85, 247)
VIOLET_DEEP = (76, 29, 149)
BLUE = (56, 189, 248)
WHITE = (246, 244, 255)


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


def _linear_gradient(top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    image = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 255))
    draw = ImageDraw.Draw(image)
    for y in range(CANVAS):
        color = _lerp(top, bottom, y / max(1, CANVAS - 1))
        draw.line((0, y, CANVAS, y), fill=(*color, 255))
    return image


def _radial_gradient(center: tuple[int, int, int], edge: tuple[int, int, int]) -> Image.Image:
    image = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 255))
    pixels = image.load()
    mid = (CANVAS - 1) / 2
    for y in range(CANVAS):
        for x in range(CANVAS):
            distance = min(1.0, math.hypot(x - mid, y - mid) / mid)
            color = _lerp(center, edge, distance)
            pixels[x, y] = (*color, 255)
    return image


def _rounded_mask(radius: int) -> Image.Image:
    mask = Image.new("L", (CANVAS, CANVAS), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, CANVAS - 1, CANVAS - 1), radius=radius, fill=255)
    return mask


def _circle_mask() -> Image.Image:
    mask = Image.new("L", (CANVAS, CANVAS), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((4, 4, CANVAS - 5, CANVAS - 5), fill=255)
    return mask


def _app_icon() -> Image.Image:
    icon = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    background = _linear_gradient(CHARCOAL_TOP, CHARCOAL_BOTTOM)
    icon.paste(background, (0, 0), _rounded_mask(58))

    # Violet aurora stripe sweeping across the tile.
    glow = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.polygon(
        [
            (-30, 196),
            (286, 18),
            (286, 104),
            (-30, 268),
        ],
        fill=(*VIOLET_LIGHT, 205),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(20))
    icon.paste(glow, (0, 0), _rounded_mask(58))

    draw = ImageDraw.Draw(icon)

    # Orbit around the stylized A.
    draw.arc(
        (38, 34, 220, 216),
        start=198,
        end=502,
        fill=(*BLUE, 235),
        width=6,
    )
    draw.ellipse((186, 50, 210, 74), fill=(*BLUE, 255))

    # Stylized "A" with a violet shadow for depth.
    strokes = [
        [(78, 194), (128, 56)],
        [(128, 56), (180, 194)],
        [(97, 137), (161, 137)],
    ]
    for points in strokes:
        draw.line([(x + 5, y + 6) for x, y in points], fill=(*VIOLET_DEEP, 190), width=20, joint="curve")
    for points in strokes:
        draw.line(points, fill=(*WHITE, 255), width=17, joint="curve")

    draw.rounded_rectangle(
        (1, 1, CANVAS - 2, CANVAS - 2),
        radius=58,
        outline=(*VIOLET_LIGHT, 150),
        width=3,
    )
    return icon


def _installer_icon() -> Image.Image:
    icon = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    background = _radial_gradient(VIOLET_LIGHT, VIOLET_DEEP)
    icon.paste(background, (0, 0), _circle_mask())

    draw = ImageDraw.Draw(icon)
    draw.ellipse((14, 14, 242, 242), outline=(*BLUE, 210), width=5)
    draw.arc((30, 30, 226, 226), start=210, end=330, fill=(*WHITE, 110), width=4)

    # Download arrow: shaft, chevron and tray line.
    draw.line((128, 72, 128, 166), fill=(*WHITE, 255), width=18)
    draw.line((76, 126, 128, 180), fill=(*WHITE, 255), width=18, joint="curve")
    draw.line((128, 180, 180, 126), fill=(*WHITE, 255), width=18, joint="curve")
    draw.rounded_rectangle((78, 192, 178, 204), radius=6, fill=(*WHITE, 225))
    return icon


def _save_ico(image: Image.Image, path: Path) -> None:
    image.save(path, format="ICO", sizes=[(size, size) for size in SIZES])
    print(f"wrote {path} ({', '.join(str(size) for size in SIZES)} px)")


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    app_icon = _app_icon()
    _save_ico(app_icon, ROOT / "anixo.ico")
    _save_ico(_installer_icon(), ROOT / "anixo-installer.ico")
    app_icon.save(ROOT / "anixo.png", format="PNG")
    print(f"wrote {ROOT / 'anixo.png'} (source for macOS .icns)")


if __name__ == "__main__":
    main()
