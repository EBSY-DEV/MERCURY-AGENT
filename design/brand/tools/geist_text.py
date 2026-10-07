"""Set text in Geist as SVG outlines: no live <text> in logo files.

Shapes with HarfBuzz (real kerning) at any weight on the variable font's
wght axis, then draws each glyph with fontTools. Output is one <path> in
font units scaled to the requested cap height.

    python geist_text.py "Mercury Agent" --weight 600 --tracking -20 -o wm.svg
    python geist_text.py "Mercury Agent" --weight 600 --json   # path + metrics

Tracking is in thousandths of an em (Figma-style: -20 = -2%).
Needs: fonttools, brotli, uharfbuzz (all in ~/.cache/logo-tools).
"""
import argparse
import json
from pathlib import Path

import uharfbuzz as hb
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

FONTS = Path(__file__).resolve().parents[3] / "mercury" / "web" / "fonts"
FACES = {"sans": FONTS / "geist.woff2", "mono": FONTS / "geist-mono.woff2"}


def outline(text, weight=600, tracking=0, face="sans", cap_height=None):
    """Return (path_d, width, height, metrics) in output units.

    With cap_height set, the path is scaled so capitals are that tall and
    the baseline sits at y = cap_height (ascender overshoot may go above 0).
    Without it, units are font units (1000/em) and the baseline is y = 0.
    """
    font = TTFont(FACES[face])
    font["fvar"]  # variable: instance via the glyph set's location
    upem = font["head"].unitsPerEm
    cap = font["OS/2"].sCapHeight
    desc = font["hhea"].descent
    asc = font["hhea"].ascent

    blob = hb.Blob(font_bytes(font))
    hbfont = hb.Font(hb.Face(blob))
    hbfont.set_variations({"wght": weight})
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(hbfont, buf, {"kern": True, "liga": False})

    gs = font.getGlyphSet(location={"wght": weight})
    order = font.getGlyphOrder()
    scale = (cap_height / cap) if cap_height else 1.0
    track = tracking / 1000 * upem

    pen = SVGPathPen(gs, ntos=lambda v: f"{v:.2f}".rstrip("0").rstrip("."))
    x = 0.0
    n = len(buf.glyph_infos)
    for i, (info, pos) in enumerate(zip(buf.glyph_infos, buf.glyph_positions)):
        name = order[info.codepoint]
        # flip y (font is y-up), move baseline to cap height, scale.
        t = (scale, 0, 0, -scale, (x + pos.x_offset) * scale,
             (cap if cap_height else 0) * scale - pos.y_offset * scale)
        gs[name].draw(TransformPen(pen, t))
        x += pos.x_advance + (track if i < n - 1 else 0)

    width = x * scale
    return pen.getCommands(), width, cap * scale, {
        "advance_width": round(width, 3), "cap_height": round(cap * scale, 3),
        "ascender": round(asc * scale, 3), "descender": round(desc * scale, 3),
        "units_per_em": upem, "scale": scale, "weight": weight, "tracking": tracking,
    }


def font_bytes(font):
    """HarfBuzz needs raw sfnt bytes; woff2 must be decompressed first."""
    import io
    out = io.BytesIO()
    f = TTFont()
    f.flavor = None
    font.flavor = None
    font.save(out)
    return out.getvalue()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text")
    ap.add_argument("--weight", type=float, default=600)
    ap.add_argument("--tracking", type=float, default=0, help="thousandths of an em")
    ap.add_argument("--face", choices=FACES, default="sans")
    ap.add_argument("--cap-height", type=float, default=100, help="output cap height in px")
    ap.add_argument("--fill", default="#000")
    ap.add_argument("--pad", type=float, default=0)
    ap.add_argument("--json", action="store_true", help="print path + metrics instead of an SVG")
    ap.add_argument("-o", "--out")
    a = ap.parse_args()

    d, w, h, m = outline(a.text, a.weight, a.tracking, a.face, a.cap_height)
    if a.json:
        print(json.dumps({"d": d, **m}))
        return
    # canvas: cap height plus descender room so y/g tails aren't clipped
    desc = -m["descender"]
    vb_h = h + desc + 2 * a.pad
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{-a.pad:g} {-a.pad:g} '
           f'{w + 2 * a.pad:.2f} {vb_h:.2f}"><path fill="{a.fill}" d="{d}"/></svg>\n')
    if a.out:
        Path(a.out).write_text(svg)
        print(f"wrote {a.out}  width={w:.1f} cap={h:.1f}")
    else:
        print(svg)


if __name__ == "__main__":
    main()
