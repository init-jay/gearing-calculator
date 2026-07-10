#!/usr/bin/env python3
"""Draw the gear favicon: ``app/static/favicon.ico`` and ``favicon.svg``.

Both come out of the one gear profile below, so the crisp vector icon and the
rasterised tab icon cannot drift apart. Run after changing any of the geometry:

    uv run scripts/make_favicon.py

Standard library only. There is no image library in this project and adding one
to draw a single 48x48 gear would be a poor trade, so the ICO is assembled by
hand: an ICONDIR of entries, each pointing at an uncompressed bottom-up 32-bit
BGRA bitmap. PNG-in-ICO would be shorter but is not read by every tool that
looks at a favicon.
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"

# The gear, in a unit circle. Teeth reach R_TIP; the body between them stops at
# R_ROOT; the bore is R_BORE. Sized so the teeth just clear the icon's edge.
TEETH = 8
R_TIP = 0.97
R_ROOT = 0.72
R_BORE = 0.34

# Where in each tooth's angular slot the flanks rise and fall, as fractions of
# the slot. The gap between 0.50 and 1.0 is the root arc to the next tooth.
RISE, TIP_END, FALL = 0.15, 0.35, 0.50

# Classic BMW instrument amber, matching --dial-amber in styles.css.
COLOR = (0xF5, 0xA1, 0x1D)

SIZES = (16, 32, 48)
SUPERSAMPLE = 4  # NxN samples per pixel; the teeth are all diagonals


def tooth_radius(theta: float) -> float:
    """The gear's outline radius at angle ``theta``: a trapezoidal tooth profile."""
    slot = 2.0 * math.pi / TEETH
    p = (theta % slot) / slot  # position within this tooth's slot, in [0, 1)
    if p < RISE:
        return R_ROOT + (R_TIP - R_ROOT) * (p / RISE)
    if p < TIP_END:
        return R_TIP
    if p < FALL:
        return R_TIP - (R_TIP - R_ROOT) * ((p - TIP_END) / (FALL - TIP_END))
    return R_ROOT


def inside(x: float, y: float) -> bool:
    """Is the unit-circle point inside the gear (outline, minus the bore)?"""
    d = math.hypot(x, y)
    if d < R_BORE or d > R_TIP:
        return False
    return d <= tooth_radius(math.atan2(y, x))


def coverage(size: int, px: int, py: int) -> float:
    """Fraction of a pixel covered by the gear, by supersampling."""
    hits = 0
    for sy in range(SUPERSAMPLE):
        for sx in range(SUPERSAMPLE):
            # Sample centre -> unit square -> unit circle centred on the icon.
            u = (px + (sx + 0.5) / SUPERSAMPLE) / size * 2.0 - 1.0
            v = (py + (sy + 0.5) / SUPERSAMPLE) / size * 2.0 - 1.0
            hits += inside(u, v)
    return hits / (SUPERSAMPLE * SUPERSAMPLE)


def bitmap(size: int) -> bytes:
    """One ICO image: BITMAPINFOHEADER, bottom-up BGRA pixels, then an AND mask."""
    r, g, b = COLOR
    rows: list[bytes] = []
    for py in range(size):
        row = bytearray()
        for px in range(size):
            alpha = round(coverage(size, px, py) * 255)
            # Premultiplication is not used here; ICO alpha is straight.
            row += bytes((b, g, r, alpha))
        rows.append(bytes(row))

    header = struct.pack(
        "<IiiHHIIiiII",
        40,           # biSize
        size,         # biWidth
        size * 2,     # biHeight: the XOR bitmap and the AND mask, stacked
        1,            # biPlanes
        32,           # biBitCount
        0,            # biCompression = BI_RGB
        0,            # biSizeImage (may be 0 for BI_RGB)
        0, 0, 0, 0,   # resolution, palette counts
    )
    # Bottom-up: the last row of the image comes first.
    xor = b"".join(reversed(rows))
    # A 1bpp mask, rows padded to 4 bytes. Zeroed: the alpha channel does the work.
    mask_stride = ((size + 31) // 32) * 4
    and_mask = b"\x00" * (mask_stride * size)
    return header + xor + and_mask


def build_ico(sizes: tuple[int, ...]) -> bytes:
    images = [bitmap(s) for s in sizes]
    offset = 6 + 16 * len(sizes)  # ICONDIR, then one ICONDIRENTRY per image

    out = bytearray(struct.pack("<HHH", 0, 1, len(sizes)))  # reserved, type=icon, count
    for size, image in zip(sizes, images):
        out += struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,  # 0 means 256
            size if size < 256 else 0,
            0,            # colours in palette
            0,            # reserved
            1,            # planes
            32,           # bits per pixel
            len(image),
            offset,
        )
        offset += len(image)
    for image in images:
        out += image
    return bytes(out)


def build_svg() -> str:
    """The same profile as a polygon, with the bore knocked out by `evenodd`."""
    slot = 2.0 * math.pi / TEETH
    # Extra samples along the root arc keep it from collapsing into a straight line.
    stops = (0.0, RISE, TIP_END, FALL, 0.65, 0.80, 0.95)

    points: list[str] = []
    for i in range(TEETH):
        for frac in stops:
            theta = (i + frac) * slot
            radius = tooth_radius(theta)
            x = 50.0 + 48.0 * radius * math.cos(theta)
            y = 50.0 + 48.0 * radius * math.sin(theta)
            points.append(f"{x:.2f},{y:.2f}")

    bore = 48.0 * R_BORE
    color = "#%02x%02x%02x" % COLOR
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">\n'
        "  <!-- Generated by scripts/make_favicon.py; edit the geometry there. -->\n"
        f'  <path fill="{color}" fill-rule="evenodd"\n'
        f'        d="M{" L".join(points)} Z '
        f'M50,{50 - bore:.2f} A{bore:.2f},{bore:.2f} 0 1,0 50,{50 + bore:.2f} '
        f'A{bore:.2f},{bore:.2f} 0 1,0 50,{50 - bore:.2f} Z" />\n'
        "</svg>\n"
    )


def main() -> int:
    ico = STATIC / "favicon.ico"
    svg = STATIC / "favicon.svg"
    ico.write_bytes(build_ico(SIZES))
    svg.write_text(build_svg(), encoding="utf-8")
    print(f"Wrote {ico} ({ico.stat().st_size / 1024:.1f} KB, sizes {', '.join(map(str, SIZES))})")
    print(f"Wrote {svg} ({svg.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
