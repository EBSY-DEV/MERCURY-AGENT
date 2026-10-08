"""Pixel ladder: render marks at true small sizes, then blow the pixels up.

A 16 px render shown at 16 px is too small to judge in an image viewer, and a
smooth upscale lies about it. This renders each SVG at 16/24/32/48 px with
cairosvg, enlarges every pixel with nearest-neighbour, and lays the result out
on light, dark and accent backgrounds, one row per SVG.

    python ladder.py a.svg b.svg -o ladder.png
"""
import argparse
import io
import re
from pathlib import Path

import cairosvg
from PIL import Image, ImageDraw

SIZES = (16, 24, 32, 48)
ZOOM = 6
BGS = (("#FFFFFF", "#000000"), ("#14111E", "#FFFFFF"), ("#6D53D3", "#FFFFFF"))
GAP = 24


def recolour(svg, colour):
    """Force every fill/stroke to one colour (one-colour test)."""
    svg = re.sub(r'(fill|stroke)="(?!none)[^"]*"', lambda m: f'{m.group(1)}="{colour}"', svg)
    svg = re.sub(r'(fill|stroke):\s*(?!none)[^;"]+', lambda m: f'{m.group(1)}:{colour}', svg)
    if "fill=" not in svg.split(">", 1)[0]:
        svg = svg.replace("<svg", f'<svg fill="{colour}"', 1)
    return svg


def render(svg_text, px):
    png = cairosvg.svg2png(bytestring=svg_text.encode(), output_width=px, output_height=px)
    return Image.open(io.BytesIO(png)).convert("RGBA")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("-o", "--out", default="ladder.png")
    ap.add_argument("--keep-colour", action="store_true", help="don't force one colour")
    a = ap.parse_args()

    cell_w = sum(s * ZOOM for s in SIZES) + GAP * (len(SIZES) + 1)
    row_h = max(SIZES) * ZOOM + 2 * GAP + 18
    W = cell_w * len(BGS)
    H = row_h * len(a.files)
    sheet = Image.new("RGB", (W, H), "#FFFFFF")
    draw = ImageDraw.Draw(sheet)
    for r, f in enumerate(a.files):
        src = Path(f).read_text()
        for b, (bg, fg) in enumerate(BGS):
            x0, y0 = b * cell_w, r * row_h
            draw.rectangle([x0, y0, x0 + cell_w, y0 + row_h], fill=bg)
            svg = src if a.keep_colour else recolour(src, fg)
            x = x0 + GAP
            for s in SIZES:
                tile = Image.new("RGBA", (s, s), bg)
                im = render(svg, s)
                tile.alpha_composite(im)
                big = tile.resize((s * ZOOM, s * ZOOM), Image.NEAREST)
                sheet.paste(big.convert("RGB"), (x, y0 + GAP))
                draw.text((x, y0 + GAP + max(SIZES) * ZOOM + 4), f"{s}px", fill="#888888")
                x += s * ZOOM + GAP
            if b == 0:
                draw.text((x0 + 4, y0 + 4), Path(f).name, fill="#888888")
    sheet.save(a.out)
    print(f"wrote {a.out}  ({W}x{H})")


if __name__ == "__main__":
    main()
