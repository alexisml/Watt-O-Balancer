#!/usr/bin/env python3
"""Generate the Watt-O-Balancer brand artwork and rasterise it to PNG.

Design: a 1970s "Watt-O-Matic" era mark -- harvest-gold / burnt-orange /
avocado palette, a 270-degree gauge dial, and a fat slab wordmark in which the
dial stands in for the middle "O" of WATT-O-BALANCER.

Outputs land in custom_components/watt_o_balancer/brand/ and are what Home Assistant
2026.3+ serves locally; the same files are what gets submitted to
home-assistant/brands for older cores. See
docs/documentation/09-brand-images.md.

macOS only. Rasterising uses headless Chrome (true alpha -- Quick Look
flattens onto opaque white) and a small pure-stdlib PNG trimmer, so nothing
has to be installed.
"""
from __future__ import annotations

import argparse
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "custom_components", "watt_o_balancer", "brand")
SVG_DIR = os.path.join(ROOT, "assets", "brand")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# ---------------------------------------------------------------- palette --
CREAM = "#F4E3C1"
GOLD = "#E8A33D"
ORANGE = "#C75B29"
RUST = "#A33B1F"
AVOCADO = "#7D8C44"
BROWN = "#3B2416"

ICON_PX = 512          # @2x icon edge; 1x is half of this
LOGO_PX = 512          # @2x logo height (shortest side); 1x is half


# ------------------------------------------------------------ png helpers --
def _png_chunks(data: bytes):
    pos = 8
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        yield data[pos + 4:pos + 8], data[pos + 8:pos + 8 + length]
        pos += 12 + length


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def read_rgba(path: str):
    """Decode a non-interlaced 8-bit RGBA PNG into (width, height, pixels)."""
    data = open(path, "rb").read()
    idat = b""
    width = height = 0
    for tag, body in _png_chunks(data):
        if tag == b"IHDR":
            width, height, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", body)
            if (depth, colour, interlace) != (8, 6, 0):
                raise SystemExit(f"{path}: expected non-interlaced 8-bit RGBA")
        elif tag == b"IDAT":
            idat += body
    raw = zlib.decompress(idat)
    bpp, stride = 4, width * 4
    pixels = bytearray(height * stride)
    prev = bytearray(stride)
    pos = 0
    for y in range(height):
        ftype = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if ftype == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                upleft = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(left, prev[i], upleft)) & 0xFF
        elif ftype != 0:
            raise SystemExit(f"{path}: unsupported filter type {ftype}")
        pixels[y * stride:(y + 1) * stride] = line
        prev = line
    return width, height, pixels


def write_rgba(path: str, width: int, height: int, pixels: bytes) -> None:
    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw += pixels[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    open(path, "wb").write(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b""))


def alpha_bbox(width: int, height: int, pixels: bytes):
    stride = width * 4
    x0, y0, x1, y1 = width, height, -1, -1
    for y in range(height):
        alphas = pixels[y * stride + 3:(y + 1) * stride:4]
        first = next((x for x, a in enumerate(alphas) if a), None)
        if first is None:
            continue
        last = len(alphas) - 1 - next(x for x, a in enumerate(reversed(alphas)) if a)
        x0, x1 = min(x0, first), max(x1, last)
        y0, y1 = (y if y1 < 0 else y0), y
    if x1 < 0:
        raise SystemExit("rendered image is fully transparent")
    return x0, y0, x1, y1


def crop(width: int, pixels: bytes, box) -> tuple[int, int, bytearray]:
    x0, y0, x1, y1 = box
    stride = width * 4
    out = bytearray()
    for y in range(y0, y1 + 1):
        out += pixels[y * stride + x0 * 4:y * stride + (x1 + 1) * 4]
    return x1 - x0 + 1, y1 - y0 + 1, out


# ----------------------------------------------------------- rasterising --
def svg_document(width: int, height: int, body: str, scale: float = 1.0) -> str:
    if scale != 1.0:
        width, height = round(width * scale), round(height * scale)
        body = f'<g transform="scale({scale:.6f})">\n{body}\n</g>'
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            f'width="{width}" height="{height}">\n{body}\n</svg>\n')


def rasterise(svg: str, out_png: str, profile: str):
    """Render an SVG to a transparent PNG and trim the empty border away.

    Chrome lays the page out at the window size, and squashes the document when
    the window is very short, so the window is always comfortably larger than
    the artboard and the surplus is trimmed off afterwards.
    """
    match = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
    width, height = int(match.group(1)), int(match.group(2))
    with tempfile.TemporaryDirectory() as tmp:
        page = os.path.join(tmp, "page.html")
        with open(page, "w") as handle:
            handle.write("<!doctype html><meta charset='utf-8'>"
                         "<style>html,body{margin:0;padding:0;background:transparent}"
                         "svg{display:block}</style>\n" + svg)
        raw = os.path.join(tmp, "raw.png")
        result = subprocess.run(
            [CHROME, "--headless", "--disable-gpu", "--hide-scrollbars", "--no-sandbox",
             f"--user-data-dir={profile}", "--default-background-color=00000000",
             "--force-device-scale-factor=1",
             f"--window-size={width + 160},{max(height + 160, 700)}",
             f"--screenshot={raw}", "file://" + page],
            capture_output=True, text=True)
        if not os.path.exists(raw):
            raise SystemExit(f"Chrome produced no screenshot:\n{result.stderr[-2000:]}")
        img_w, img_h, pixels = read_rgba(raw)
    new_w, new_h, cropped = crop(img_w, pixels, alpha_bbox(img_w, img_h, pixels))
    write_rgba(out_png, new_w, new_h, cropped)
    return new_w, new_h


def downscale(src: str, dst: str, width: int, height: int) -> None:
    """Box-filter downscale. Good enough at exactly 2x, and keeps alpha sane."""
    src_w, src_h, pixels = read_rgba(src)
    out = bytearray(width * height * 4)
    for y in range(height):
        y0, y1 = y * src_h // height, max((y + 1) * src_h // height, y * src_h // height + 1)
        for x in range(width):
            x0, x1 = x * src_w // width, max((x + 1) * src_w // width, x * src_w // width + 1)
            r = g = b = a = n = 0
            for sy in range(y0, y1):
                row = sy * src_w * 4
                for sx in range(x0, x1):
                    i = row + sx * 4
                    alpha = pixels[i + 3]
                    r += pixels[i] * alpha
                    g += pixels[i + 1] * alpha
                    b += pixels[i + 2] * alpha
                    a += alpha
                    n += 1
            o = (y * width + x) * 4
            if a:
                out[o] = min(255, r // a)
                out[o + 1] = min(255, g // a)
                out[o + 2] = min(255, b // a)
            out[o + 3] = a // n
    write_rgba(dst, width, height, out)


# ---------------------------------------------------------------- artwork --
DIAL_BAND = 72
DIAL_RADIUS = 168
DIAL_FACE = 124
POINTER_DEG = 62
DIAL_SPAN = 2 * (DIAL_RADIUS + DIAL_BAND / 2)   # outer diameter, 408 units

WORDMARK = dict(family="American Typewriter", weight=700, size=150, tracking=2)
TAGLINE = dict(family="Futura", weight=700, size=44, tracking=16)


# A pre-war coupe in profile -- long hood, upright cabin, sweeping fenders over
# big spoked wheels -- to match the "-O-" era the name comes from. The ray
# marks it as electric. Both are kept chunky so they survive a 24px favicon.
CAR_BODY = ("M -132 26 Q -140 26 -140 12 L -140 -2 Q -140 -12 -128 -14 L -84 -20 "
            "L -66 -50 Q -60 -62 -44 -62 L 24 -62 Q 44 -62 50 -48 L 66 -18 "
            "L 120 -8 Q 140 -4 140 14 L 140 16 Q 140 26 132 26 Z")
BOLT_POINTS = [(54, 0), (10, 86), (44, 86), (26, 150), (92, 60), (50, 60), (78, 0)]


def car(cx: float, cy: float, scale: float, fill: str, hub: str) -> str:
    return (f'  <g transform="translate({cx} {cy}) scale({scale})">'
            f'<path d="{CAR_BODY}" fill="{fill}"/>'
            f'<path d="M -150 40 A 62 62 0 0 1 -26 40 Z" fill="{fill}"/>'
            f'<path d="M 22 40 A 62 62 0 0 1 146 40 Z" fill="{fill}"/>'
            f'<rect x="-38" y="16" width="68" height="24" rx="8" fill="{fill}"/>'
            f'<circle cx="-88" cy="40" r="44" fill="{fill}"/>'
            f'<circle cx="84" cy="40" r="44" fill="{fill}"/>'
            f'<circle cx="-88" cy="40" r="17" fill="{hub}"/>'
            f'<circle cx="84" cy="40" r="17" fill="{hub}"/></g>')


def ray(cx: float, cy: float, height: float, fill: str) -> str:
    scale = height / 150.0
    steps = ("M" if i == 0 else "L" for i in range(len(BOLT_POINTS)))
    path = " ".join(f"{step} {cx + (x - 46) * scale:.1f} {cy + (y - 75) * scale:.1f}"
                    for step, (x, y) in zip(steps, BOLT_POINTS)) + " Z"
    return f'  <path d="{path}" fill="{fill}"/>'


def pointer(degrees: float, fill: str = CREAM) -> str:
    """The gauge spindle: a triangle on the band, apex pointing outwards.

    Pale rather than dark, so it stays visible where it crosses the band and
    does not read as a notch cut out of it.
    """
    angle = math.radians(degrees)
    radial = (math.cos(angle), -math.sin(angle))
    tangent = (-math.sin(angle), -math.cos(angle))
    base_r = DIAL_RADIUS - DIAL_BAND / 2
    apex_r = DIAL_RADIUS + DIAL_BAND / 2 + 6
    half = 24
    ax, ay = 256 + apex_r * radial[0], 256 + apex_r * radial[1]
    bx, by = 256 + base_r * radial[0], 256 + base_r * radial[1]
    lx, ly = bx + half * tangent[0], by + half * tangent[1]
    rx, ry = bx - half * tangent[0], by - half * tangent[1]
    return (f'  <path d="M {ax:.1f} {ay:.1f} L {lx:.1f} {ly:.1f} '
            f'L {rx:.1f} {ry:.1f} Z" fill="{fill}"/>')


def dial(face: str = CREAM) -> str:
    """A 270-degree gauge: coloured band, pointer, and a classic EV on the face.

    The three band colours are drawn as overlapping arcs that all finish at the
    same end angle, each painting over the last. Butting three separate arcs end
    to end instead leaves hairline seams at the colour boundaries.

    The pointer is a spindle riding on the band rather than a needle pivoting
    from the centre, which keeps the face clear for the ray and the car.
    """
    def point(degrees: float, radius: float):
        angle = math.radians(degrees)
        return 256 + radius * math.cos(angle), 256 - radius * math.sin(angle)

    def arc(colour: str, start: float, end: float = -45) -> str:
        x0, y0 = point(start, DIAL_RADIUS)
        x1, y1 = point(end, DIAL_RADIUS)
        large = 1 if (start - end) > 180 else 0
        return (f'  <path d="M {x0:.1f} {y0:.1f} A {DIAL_RADIUS} {DIAL_RADIUS} 0 '
                f'{large} 1 {x1:.1f} {y1:.1f}" fill="none" stroke="{colour}" '
                f'stroke-width="{DIAL_BAND}"/>')

    car_scale = 0.56
    return "\n".join([
        arc(AVOCADO, 225), arc(GOLD, 135), arc(ORANGE, 45),
        f'  <circle cx="256" cy="256" r="{DIAL_FACE}" fill="{face}"/>',
        ray(256, 230, 150, ORANGE),
        car(256, 306, car_scale, BROWN, face),
        pointer(POINTER_DEG),
    ])


def icon_body() -> str:
    return (f'  <rect x="0" y="0" width="512" height="512" rx="112" ry="112" fill="{BROWN}"/>\n'
            + dial())


def _text(x: float, y: float, content: str, font: dict, fill: str,
          anchor: str = "start") -> str:
    return (f'  <text x="{x:.0f}" y="{y:.0f}" text-anchor="{anchor}" '
            f'font-family="{font["family"]}" font-weight="{font["weight"]}" '
            f'font-size="{font["size"]}" letter-spacing="{font["tracking"]}" '
            f'fill="{fill}">{content}</text>')


def logo_body(ink: str, accent: str, measure) -> str:
    """WATT-(dial)-BALANCER over a rule, with the tagline beneath."""
    cap_height = measure("H", WORDMARK)[1]
    scale = (cap_height * 1.62) / DIAL_SPAN
    baseline, centre_x = 420, 1500
    centre_y = baseline - cap_height / 2
    half = DIAL_SPAN * scale / 2
    gap = WORDMARK["size"] * 0.06
    left_w = measure("WATT-", WORDMARK)[0]
    right_w = measure("-BALANCER", WORDMARK)[0]
    rule_x0 = centre_x - half - gap - left_w
    rule_x1 = centre_x + half + gap + right_w
    # The car and bolt on the dial face stay dark whatever the lettering does,
    # so the dial reads the same in both the light and dark lockups.
    return "\n".join([
        f'  <g transform="translate({centre_x - 256 * scale:.1f} '
        f'{centre_y - 256 * scale:.1f}) scale({scale:.4f})">',
        dial(),
        '  </g>',
        _text(centre_x - half - gap, baseline, "WATT-", WORDMARK, ink, anchor="end"),
        _text(centre_x + half + gap, baseline, "-BALANCER", WORDMARK, ink),
        f'  <rect x="{rule_x0:.0f}" y="{baseline + 40:.0f}" '
        f'width="{rule_x1 - rule_x0:.0f}" height="22" rx="11" fill="{accent}"/>',
        _text((rule_x0 + rule_x1) / 2, baseline + 128, "EV LOAD BALANCING",
              TAGLINE, accent, anchor="middle"),
    ])


# ------------------------------------------------------------------ build --
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=OUT_DIR, help="where the PNGs are written")
    parser.add_argument("--svg-out", default=SVG_DIR, help="where the SVG sources are written")
    args = parser.parse_args()

    if not os.path.exists(CHROME):
        raise SystemExit(f"Google Chrome not found at {CHROME}")
    os.makedirs(args.out, exist_ok=True)
    os.makedirs(args.svg_out, exist_ok=True)

    with tempfile.TemporaryDirectory() as profile:
        cache: dict[tuple, tuple[int, int]] = {}

        def measure(text: str, font: dict) -> tuple[int, int]:
            """Ink size of a text run, measured by rendering and trimming it."""
            key = (text, font["family"], font["weight"], font["size"], font["tracking"])
            if key not in cache:
                with tempfile.TemporaryDirectory() as tmp:
                    png = os.path.join(tmp, "m.png")
                    cache[key] = rasterise(
                        svg_document(4000, 900, _text(200, 600, text, font, "#000")),
                        png, profile)
            return cache[key]

        sources = {
            "icon.svg": svg_document(512, 512, icon_body()),
            "logo.svg": svg_document(3000, 620, logo_body(BROWN, GOLD, measure)),
            "dark_logo.svg": svg_document(3000, 620, logo_body(CREAM, GOLD, measure)),
        }
        for name, text in sources.items():
            with open(os.path.join(args.svg_out, name), "w") as handle:
                handle.write(text)

        written = []
        icon2x = os.path.join(args.out, "icon@2x.png")
        rasterise(sources["icon.svg"], icon2x, profile)
        written.append(icon2x)

        for name in ("logo", "dark_logo"):
            path = os.path.join(args.out, f"{name}@2x.png")
            ink = BROWN if name == "logo" else CREAM
            body = logo_body(ink, GOLD, measure)
            # The trimmed height is only known once it has been rendered, so
            # render, then re-render scaled towards LOGO_PX. Rounding can leave
            # it a pixel out, so correct until it lands exactly.
            scale = 1.0
            _, height = rasterise(sources[f"{name}.svg"], path, profile)
            for _ in range(4):
                if height == LOGO_PX:
                    break
                scale *= LOGO_PX / height
                _, height = rasterise(svg_document(3000, 620, body, scale=scale),
                                      path, profile)
            else:
                raise SystemExit(f"{name}: could not settle on a {LOGO_PX}px height")
            written.append(path)

        for src in list(written):
            dst = src.replace("@2x", "")
            width, height, _ = read_rgba(src)
            downscale(src, dst, width // 2, height // 2)
            written.append(dst)

    for path in sorted(written):
        width, height, _ = read_rgba(path)
        print(f"  {os.path.basename(path):18} {width}x{height}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
