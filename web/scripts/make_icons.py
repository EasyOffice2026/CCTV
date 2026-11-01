"""Draws the app icons in public/icons (a camera lens on the brand blue).

    python web/scripts/make_icons.py      (needs Pillow; the repo's .venv has it)

"any" icons are a rounded square; "maskable" ones fill the whole square with the lens
inside the middle 60 %, because Android cuts them into its own shape.
"""

from pathlib import Path

from PIL import Image, ImageDraw

BRAND = (34, 89, 194)  # --color-brand-600
WHITE = (255, 255, 255)
RECORD = (239, 68, 68)
OUT = Path(__file__).resolve().parent.parent / "public" / "icons"
SCALE = 4  # drawn larger, then shrunk, for smooth edges


def icon(size: int, maskable: bool) -> Image.Image:
    s = size * SCALE
    image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if maskable:
        draw.rectangle((0, 0, s, s), fill=BRAND)
        lens = s * 0.24  # the lens and its red dot stay inside the safe circle (40 %)
    else:
        draw.rounded_rectangle((0, 0, s - 1, s - 1), radius=s * 0.22, fill=BRAND)
        lens = s * 0.34
    c = s / 2
    ring = lens * 0.22
    draw.ellipse((c - lens, c - lens, c + lens, c + lens), fill=WHITE)
    inner = lens - ring
    draw.ellipse((c - inner, c - inner, c + inner, c + inner), fill=BRAND)
    pupil = inner * 0.45
    draw.ellipse((c - pupil, c - pupil, c + pupil, c + pupil), fill=WHITE)
    dot = lens * 0.2
    dx, dy = c + lens * 0.95, c - lens * 0.95
    draw.ellipse((dx - dot, dy - dot, dx + dot, dy + dot), fill=RECORD)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, size, maskable in [
        ("icon-192.png", 192, False),
        ("icon-512.png", 512, False),
        ("maskable-512.png", 512, True),
        ("apple-touch-icon.png", 180, True),  # iOS rounds the corners itself
        ("favicon-32.png", 32, False),
    ]:
        icon(size, maskable).save(OUT / name, optimize=True)
        print("wrote", OUT / name)


if __name__ == "__main__":
    main()
