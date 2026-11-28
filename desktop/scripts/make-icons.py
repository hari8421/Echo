"""Generate the macOS/Windows app icons (requires Pillow, dev-only)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 1024
OUT = Path(__file__).resolve().parent.parent / "build"


def make_icon() -> Image.Image:
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, SIZE - 1, SIZE - 1), radius=224, fill=255)

    background = Image.new("RGBA", (SIZE, SIZE))
    draw = ImageDraw.Draw(background)
    for y in range(SIZE):
        t = y / (SIZE - 1)
        color = (
            int(16 + 8 * t),
            int(32 + 15 * t),
            int(50 + 22 * t),
            255,
        )
        draw.line((0, y, SIZE, y), fill=color)
    image.paste(background, (0, 0), mask)

    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, SIZE - 5, SIZE - 5), radius=220, outline=(64, 105, 146, 255), width=7)

    cx = cy = SIZE // 2
    arcs = (
        (300, 205, 335, 30, (124, 196, 255, 255)),
        (216, 200, 340, 26, (155, 227, 180, 255)),
        (132, 195, 345, 22, (124, 196, 255, 225)),
    )
    for radius, start, end, width, color in arcs:
        draw.arc(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            start,
            end,
            fill=color,
            width=width,
        )
    draw.ellipse((cx - 22, cy - 22, cx + 22, cy + 22), fill=(234, 241, 248, 255))
    return image


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    icon = make_icon()
    icon.save(OUT / "icon.png")
    icon.save(OUT / "icon.icns")
    icon.save(
        OUT / "icon.ico",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print("Wrote", ", ".join(str(path) for path in sorted(OUT.glob("icon.*"))))


if __name__ == "__main__":
    main()
