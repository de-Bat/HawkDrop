"""Build HawkSense's app icons from the logo.

    pip install pillow        # only needed for this script
    python scripts/make_icons.py

Reads assets/hawksense-logo.webp (the hawk-and-chart square above the
"HawkSense" wordmark) and writes hawksense/web/icons/*.png plus
assets/hawksense-logo.png for the README. iOS needs PNG touch icons.
"""

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "assets" / "hawksense-logo.webp"
OUT = ROOT / "hawksense" / "web" / "icons"
BOX = (238, 140, 1018, 890)  # the app-icon square inside the logo image
CORNER = 153  # its corner radius, in source pixels


def rounded_mask(size: tuple[int, int], radius: float, inset: int = 0, scale: int = 4) -> Image.Image:
    """Anti-aliased rounded-rectangle mask, optionally inset from the edges."""
    big = Image.new("L", (size[0] * scale, size[1] * scale), 0)
    i = inset * scale
    ImageDraw.Draw(big).rounded_rectangle((i, i, big.width - 1 - i, big.height - 1 - i), (radius - inset) * scale,
                                          fill=255)
    return big.resize(size, Image.LANCZOS)


def edge_color(crop: Image.Image) -> tuple[int, int, int]:
    """Average colour of a thin ring just inside the square's edge, so the filled corners blend in."""
    ring = ImageChops.subtract(rounded_mask(crop.size, CORNER, inset=8), rounded_mask(crop.size, CORNER, inset=16))
    flat = lambda img: getattr(img, "get_flattened_data", img.getdata)()  # noqa: E731 (Pillow 12+ / older)
    pixels = [p for p, m in zip(flat(crop), flat(ring)) if m > 128 and max(p) < 140]
    return tuple(sum(c) // len(pixels) for c in zip(*pixels))


def artwork() -> Image.Image:
    """The icon square as a full-bleed square: the white corners of the logo become navy."""
    crop = Image.open(SRC).convert("RGB").crop(BOX)
    side = max(crop.size)
    square = Image.new("RGB", (side, side), edge_color(crop))
    # inset a little so the light anti-aliased rim of the logo's square doesn't show
    square.paste(crop, ((side - crop.width) // 2, (side - crop.height) // 2), rounded_mask(crop.size, CORNER, inset=6))
    return square


def rounded(img: Image.Image, size: int) -> Image.Image:
    icon = img.resize((size, size), Image.LANCZOS).convert("RGBA")
    icon.putalpha(rounded_mask((size, size), size * 0.2))
    return icon


def main():
    art = artwork()
    OUT.mkdir(parents=True, exist_ok=True)
    art.resize((180, 180), Image.LANCZOS).save(OUT / "apple-touch-icon.png", optimize=True)  # iOS rounds it
    rounded(art, 192).save(OUT / "icon-192.png", optimize=True)
    rounded(art, 512).save(OUT / "icon-512.png", optimize=True)
    rounded(art, 32).save(OUT / "favicon-32.png", optimize=True)
    # maskable: launchers crop to a circle or squircle, so keep the hawk inside the 80% safe zone
    maskable = Image.new("RGB", (512, 512), art.getpixel((2, 2)))
    inner = art.resize((410, 410), Image.LANCZOS)
    maskable.paste(inner, (51, 51))
    maskable.save(OUT / "icon-maskable-512.png", optimize=True)
    logo = Image.open(SRC).convert("RGB")
    logo.resize((480, 480), Image.LANCZOS).save(ROOT / "assets" / "hawksense-logo.png", optimize=True)
    print(f"wrote icons to {OUT}")


if __name__ == "__main__":
    main()
