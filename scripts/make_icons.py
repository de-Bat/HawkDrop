"""Render HawkDrop's app icons (PNG) with no dependencies.

    python scripts/make_icons.py

Writes hawkdrop/web/icons/*.png: a white price "drop" with a down arrow on a
teal-to-indigo gradient. iOS needs PNG touch icons (it ignores SVG manifest icons).
"""

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "hawkdrop" / "web" / "icons"
TOP, BOTTOM = (15, 118, 110), (49, 46, 129)  # teal-700 -> indigo-900
WHITE = (255, 255, 255)


def png(path: Path, size: int, pixels):
    raw = b"".join(b"\x00" + bytes(c for px in row for c in px) for row in pixels)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def make_shape(scale: float):
    """Return inside(x, y) for the glyph in unit coords, glyph scaled around the centre."""
    cx, cy, r = 0.5, 0.60, 0.215
    ax, ay = 0.5, 0.15
    d = math.hypot(ax - cx, ay - cy)
    ang = math.acos(r / d)
    ux, uy = (ax - cx) / d, (ay - cy) / d
    tangents = [(cx + r * (ux * math.cos(s * ang) - uy * math.sin(s * ang)),
                 cy + r * (ux * math.sin(s * ang) + uy * math.cos(s * ang))) for s in (1, -1)]
    (lx, ly), (rx, ry) = tangents

    def in_tri(x, y):
        def side(x1, y1, x2, y2):
            return (x - x2) * (y1 - y2) - (x1 - x2) * (y - y2)
        s1, s2, s3 = side(ax, ay, lx, ly), side(lx, ly, rx, ry), side(rx, ry, ax, ay)
        return not ((s1 < 0 or s2 < 0 or s3 < 0) and (s1 > 0 or s2 > 0 or s3 > 0))

    arrow = [((0.5, 0.47), (0.5, 0.70)), ((0.415, 0.615), (0.5, 0.70)), ((0.585, 0.615), (0.5, 0.70))]

    def inside(x, y):
        x, y = 0.5 + (x - 0.5) / scale, 0.5 + (y - 0.5) / scale
        in_drop = math.hypot(x - cx, y - cy) <= r or in_tri(x, y)
        if not in_drop:
            return 0
        if any(seg_dist(x, y, *a, *b) < 0.03 for a, b in arrow):
            return 2  # arrow cut-out
        return 1
    return inside


def render(size: int, scale: float, rounded: bool, ss: int = 3):
    inside = make_shape(scale)
    radius = 0.2237 if rounded else 0  # iOS-like squircle approximation
    rows = []
    for py in range(size):
        row = []
        for px in range(size):
            acc = [0.0, 0.0, 0.0, 0.0]
            for sy in range(ss):
                for sx in range(ss):
                    x = (px + (sx + 0.5) / ss) / size
                    y = (py + (sy + 0.5) / ss) / size
                    if rounded:
                        qx, qy = max(abs(x - 0.5) - (0.5 - radius), 0), max(abs(y - 0.5) - (0.5 - radius), 0)
                        if math.hypot(qx, qy) > radius:
                            continue
                    t = (x + y) / 2
                    bg = tuple(TOP[i] + (BOTTOM[i] - TOP[i]) * t for i in range(3))
                    col = WHITE if inside(x, y) == 1 else bg
                    for i in range(3):
                        acc[i] += col[i]
                    acc[3] += 255
            n = ss * ss
            alpha = acc[3] / n
            cov = acc[3] / 255 or 1
            row.append((int(acc[0] / cov), int(acc[1] / cov), int(acc[2] / cov), int(alpha)))
        rows.append(row)
    return rows


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = [
        ("apple-touch-icon.png", 180, 1.0, False),  # iOS applies its own mask; must be opaque
        ("icon-192.png", 192, 1.0, True),
        ("icon-512.png", 512, 1.0, True),
        ("icon-maskable-512.png", 512, 0.78, False),  # glyph inside the 80% safe zone
        ("favicon-32.png", 32, 1.0, True),
    ]
    for name, size, scale, rounded in jobs:
        png(OUT / name, size, render(size, scale, rounded, ss=2 if size >= 512 else 3))
        print("wrote", OUT / name)
