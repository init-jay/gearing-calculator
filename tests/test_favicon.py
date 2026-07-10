"""Check the gear favicon, and that it still matches the script that draws it.

``favicon.ico`` is a binary blob nobody reads by eye, so the load-bearing test is
the last one: regenerate from ``scripts/make_favicon.py`` and compare bytes. Edit
the geometry without re-running the script and this file says so.
"""

import importlib.util
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
ICO = STATIC / "favicon.ico"
SVG = STATIC / "favicon.svg"

spec = importlib.util.spec_from_file_location("make_favicon", ROOT / "scripts" / "make_favicon.py")
make_favicon = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["make_favicon"] = make_favicon
spec.loader.exec_module(make_favicon)

RAW = ICO.read_bytes()


def _entries():
    reserved, kind, count = struct.unpack("<HHH", RAW[:6])
    assert (reserved, kind) == (0, 1)  # 0, and type 1 = icon
    for i in range(count):
        yield struct.unpack("<BBBBHHII", RAW[6 + 16 * i : 22 + 16 * i])


def test_ico_is_an_icon_not_a_cursor():
    reserved, kind, count = struct.unpack("<HHH", RAW[:6])
    assert (reserved, kind) == (0, 1)
    assert count == len(make_favicon.SIZES)


def test_ico_carries_the_expected_sizes_in_truecolour():
    got = [(w, h, bpp) for w, h, _, _, _, bpp, _, _ in _entries()]
    assert got == [(s, s, 32) for s in make_favicon.SIZES]


def test_every_ico_entry_points_inside_the_file():
    for *_, size, offset in _entries():
        assert offset + size <= len(RAW)


def test_each_ico_bitmap_header_agrees_with_its_directory_entry():
    # The classic way an ICO goes subtly wrong: biHeight must be twice the image
    # height, because it counts the XOR bitmap plus the (here empty) AND mask.
    for w, h, _, _, _, _, size, offset in _entries():
        hdr = struct.unpack("<IiiHHIIiiII", RAW[offset : offset + 40])
        assert hdr[0] == 40  # biSize
        assert hdr[1] == w  # biWidth
        assert hdr[2] == h * 2  # biHeight
        assert hdr[4] == 32  # biBitCount
        assert hdr[5] == 0  # BI_RGB, uncompressed


def test_the_icon_has_transparency_and_only_the_one_colour():
    # BGRA, bottom-up. A gear is a hole in a disc: it must have both fully
    # transparent and fully opaque pixels, and every visible pixel is the amber.
    r, g, b = make_favicon.COLOR
    for w, h, _, _, _, _, _, offset in _entries():
        pixels = RAW[offset + 40 : offset + 40 + w * h * 4]
        alphas = pixels[3::4]
        assert min(alphas) == 0, "no transparent pixels: the bore is filled in"
        assert max(alphas) == 255, "nothing is fully opaque"
        for i in range(0, len(pixels), 4):
            if pixels[i + 3]:
                assert (pixels[i + 2], pixels[i + 1], pixels[i]) == (r, g, b)


def test_svg_is_a_single_amber_path_with_a_knocked_out_bore():
    text = SVG.read_text(encoding="utf-8")
    assert 'viewBox="0 0 100 100"' in text
    assert "#%02x%02x%02x" % make_favicon.COLOR in text
    # `evenodd` is what turns the bore circle into a hole rather than a lid.
    assert 'fill-rule="evenodd"' in text
    assert text.count("<path") == 1


def test_the_committed_files_match_the_generator():
    # Both are generated from one gear profile; if they drift, the tab icon and
    # the vector icon stop being the same gear.
    assert RAW == make_favicon.build_ico(make_favicon.SIZES)
    assert SVG.read_text(encoding="utf-8") == make_favicon.build_svg()


def test_index_html_links_both_icons():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'href="favicon.ico"' in html
    assert 'href="favicon.svg"' in html
    assert 'type="image/svg+xml"' in html


def test_the_gear_profile_is_a_gear():
    # Teeth reach past the body, the body clears the bore, and a tooth is a
    # trapezoid: flanks at RISE and FALL, a flat tip between.
    assert make_favicon.R_TIP > make_favicon.R_ROOT > make_favicon.R_BORE > 0
    assert 0 < make_favicon.RISE < make_favicon.TIP_END < make_favicon.FALL < 1
    assert make_favicon.TEETH >= 6

    slot = 2 * 3.141592653589793 / make_favicon.TEETH
    assert make_favicon.tooth_radius(slot * 0.25) == make_favicon.R_TIP  # on the tip
    assert make_favicon.tooth_radius(slot * 0.75) == make_favicon.R_ROOT  # in the gap
    # The bore is a hole, so the centre is outside the shape.
    assert not make_favicon.inside(0.0, 0.0)
    assert make_favicon.inside(0.0, (make_favicon.R_BORE + make_favicon.R_ROOT) / 2)
