"""Build the final Mercury Agent kit SVGs into design/brand/final/.

Geometry (design units, disc radius R = 100; grid unit u = 20):
  gap g = 1u (20)   dot r = 2u (40)   bite B = r + g = 3u (60)
  disc centre -> dot centre d = 4u (80)   disc R = 5u (100)
  d^2 + B^2 = R^2 (3-4-5), so the bite meets the rim exactly on the dot's vertical diameter.
Master canvas 256: scale 216/220, disc centre (122.18, 128).

    $PY tools/kit_build.py
"""
import math, re, sys
from pathlib import Path
import pathops
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.svgLib.path import parse_path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import geist_text  # noqa: E402

OUT = HERE.parent / "final"
TITLE = "Mercury Agent"
S = 216 / 220
CX, CY = 128 - S * 10 + 4, 128.0          # disc centre on the 256 canvas
R, r, B, D = 100 * S, 40 * S, 60 * S, 80 * S
DOTX = CX + D

def f(v):
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s

def svg(vb_w, vb_h, body, extra=""):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {f(vb_w)} {f(vb_h)}" width="{f(vb_w)}" '
            f'height="{f(vb_h)}" role="img"{extra}>\n  <title>{TITLE}</title>\n{body}</svg>\n')

def symbol_d(cx=CX, cy=CY, R=R, r=r, B=B, D=D):
    """Disc minus round bite (one contour) + dot (second contour). Arcs only, 6 anchors."""
    hx = cx + (D * D + R * R - B * B) / (2 * D)
    hy = math.sqrt(R * R - (hx - cx) ** 2)
    dx = cx + D
    return (f"M{f(hx)} {f(cy - hy)}A{f(R)} {f(R)} 0 1 0 {f(hx)} {f(cy + hy)}"
            f"A{f(B)} {f(B)} 0 0 1 {f(hx)} {f(cy - hy)}Z"
            f"M{f(dx - r)} {f(cy)}A{f(r)} {f(r)} 0 1 1 {f(dx + r)} {f(cy)}"
            f"A{f(r)} {f(r)} 0 1 1 {f(dx - r)} {f(cy)}Z")

MASTER_D = symbol_d()
# reversed cut (white on dark): dot r 39, gap 21 in design units, bite unchanged (from round 3, v11)
REV_D = ("M201.53 68.82A98.63 98.63 0 1 0 201.53 187.18A59.18 59.18 0 0 1 201.53 68.82Z"
         "M163.07 128A38.47 38.47 0 1 1 240 128A38.47 38.47 0 1 1 163.07 128Z")  # same silhouette as master
# small cut (16-24 px): own tight canvas, disc R 112 = 7 px at 16 px, bite 64, dot 40 (round 3, v10)
SMALL_D = "M209.91 64A112 112 0 1 0 209.91 192A64 64 0 1 1 209.91 64ZM169.91 128A40 40 0 1 1 249.91 128A40 40 0 1 1 169.91 128Z"

def ppath(d, tx=0.0, ty=0.0, k=1.0):
    p = pathops.Path()
    parse_path(d, TransformPen(p.getPen(), (k, 0, 0, k, tx, ty)))
    return p

def pd(p):
    pen = SVGPathPen(None, ntos=f)
    p.draw(pen)
    return pen.getCommands()

def text_path(text, cap, weight=600, tracking=-20, tx=0.0, ty=0.0):
    d, w, h, m = geist_text.outline(text, weight, tracking, "sans", cap)
    p = ppath(d, tx, ty)
    p.simplify(fix_winding=True)          # union overlapping glyph contours
    return p, m

def write(name, content):
    (OUT / name).write_text(content)
    print("wrote", name)

def fill_path(d, colour="#000"):
    return f'  <path fill="{colour}" d="{d}"/>\n'

# ---- symbols -------------------------------------------------------------
write("mercury-symbol.svg", svg(256, 256, fill_path(MASTER_D)))
write("mercury-symbol-reversed.svg", svg(256, 256, fill_path(REV_D)))
write("mercury-symbol-small.svg", svg(256, 256, fill_path(SMALL_D)))
# the dot alone: status / presence element, drawn in the accent
write("mercury-dot.svg", svg(80, 80, fill_path("M0 40A40 40 0 1 1 80 40A40 40 0 1 1 0 40Z", "#6D53D3")))

# ---- lockups -------------------------------------------------------------
CAP = 112
GAP = 2 * r                                # one dot diameter between dot and "M"
def lockup_h(sym_d, name):
    p0, m = text_path("Mercury Agent", CAP)
    x0 = p0.bounds[0]
    tx = DOTX + r + GAP - x0
    ty = 128 - CAP / 2                      # cap midline on the dot's centre line
    p, _ = text_path("Mercury Agent", CAP, tx=tx, ty=ty)
    W = math.ceil(p.bounds[2] + 24)
    write(name, svg(W, 256, fill_path(sym_d + pd(p))))
    return W
WH = lockup_h(MASTER_D, "mercury-lockup-horizontal.svg")

SCAP = 60
def lockup_s(sym_d, name, pad=40):
    p0, _ = text_path("Mercury Agent", SCAP)
    tw = p0.bounds[2] - p0.bounds[0]
    sil_w = (DOTX + r) - (CX - R)
    W = math.ceil(max(tw, sil_w) + 2 * pad)
    # symbol: silhouette centred, then the canvas optical shift (+4 of 256 -> the disc carries the mass)
    sx = W / 2 - ((CX - R) + (DOTX + r)) / 2 + 4
    sy = pad - (CY - R)
    sym = ppath(sym_d, sx, sy)             # only used for bounds; keep the arcs in the file
    gap = r
    ty = sy + CY + R + gap
    tx = W / 2 - (p0.bounds[0] + p0.bounds[2]) / 2
    p, _ = text_path("Mercury Agent", SCAP, tx=tx, ty=ty)
    H = math.ceil(p.bounds[3] + pad)
    # translate the arc path numerically (no transform attribute in the master)
    body = fill_path(shift_d(sym_d, sx, sy) + pd(p))
    write(name, svg(W, H, body))
def shift_d(d, tx, ty):
    """Translate an absolute M/A/Z path (the symbol) by (tx, ty)."""
    out = []
    for cmd, args in re.findall(r"([MAZ])([^MAZ]*)", d):
        n = [float(v) for v in re.findall(r"-?[\d.]+", args)]
        if cmd == "M":
            out.append(f"M{f(n[0]+tx)} {f(n[1]+ty)}")
        elif cmd == "A":
            for i in range(0, len(n), 7):
                a = n[i:i+7]
                out.append(f"A{f(a[0])} {f(a[1])} {f(a[2])} {int(a[3])} {int(a[4])} {f(a[5]+tx)} {f(a[6]+ty)}")
        else:
            out.append("Z")
    return "".join(out)
lockup_s(MASTER_D, "mercury-lockup-stacked.svg")

# wordmark only
pw, _ = text_path("Mercury Agent", CAP)
bx0, by0, bx1, by1 = pw.bounds
pad = 24
pw2, _ = text_path("Mercury Agent", CAP, tx=pad - bx0, ty=pad - by0)
write("mercury-wordmark.svg", svg(math.ceil(bx1 - bx0 + 2 * pad), math.ceil(by1 - by0 + 2 * pad), fill_path(pd(pw2))))

# ---- tiles ---------------------------------------------------------------
GRAD = ('  <defs>\n    <linearGradient id="tile-grad" gradientUnits="userSpaceOnUse" x1="243.91" y1="-10.14" '
        'x2="12.09" y2="266.14">\n      <stop offset="0" stop-color="#C7B6FF"/>\n'
        '      <stop offset="0.55" stop-color="#9C84F0"/>\n      <stop offset="1" stop-color="#7A5EDB"/>\n'
        '    </linearGradient>\n  </defs>\n')
def tile(sym_d, sil, frac, lift, rx=74, cxo=128.0):
    """sil = (x0, x1) of the artwork's silhouette on its own canvas; centre it, scale to frac of the tile."""
    k = frac * 256 / (sil[1] - sil[0])
    tx = cxo - k * (sil[0] + sil[1]) / 2
    ty = 128 - k * 128 - lift
    p = ppath(sym_d, 0, 0)
    return (GRAD + f'  <rect width="256" height="256" rx="{rx}" fill="url(#tile-grad)"/>\n'
            f'  <path fill="#FFFFFF" d="{shift_scale_d(sym_d, k, tx, ty)}"/>\n')
def shift_scale_d(d, k, tx, ty):
    out = []
    for cmd, args in re.findall(r"([MAZ])([^MAZ]*)", d):
        n = [float(v) for v in re.findall(r"-?[\d.]+", args)]
        if cmd == "M":
            out.append(f"M{f(n[0]*k+tx)} {f(n[1]*k+ty)}")
        elif cmd == "A":
            for i in range(0, len(n), 7):
                a = n[i:i+7]
                out.append(f"A{f(a[0]*k)} {f(a[1]*k)} {f(a[2])} {int(a[3])} {int(a[4])} {f(a[5]*k+tx)} {f(a[6]*k+ty)}")
        else:
            out.append("Z")
    return "".join(out)
# master tile: reversed cut, silhouette 68 % of the tile width; +4 of 256 optical shift kept (scaled)
SIL_M = (CX - R - 4, DOTX + r - 4)        # silhouette centred on canvas = without the optical shift
SIL_M = (CX - R, DOTX + r)
k_m = 0.68 * 256 / (SIL_M[1] - SIL_M[0])
# round-3 placement: 68 % of the tile width, lifted 2, canvas optical shift kept
write("mercury-tile.svg", svg(256, 256, GRAD + '  <rect width="256" height="256" rx="74" fill="url(#tile-grad)"/>\n'
      f'  <path fill="#FFFFFF" d="{shift_scale_d(REV_D, 0.8059, 24.84, 22.84)}"/>\n'))
SIL_S = (6.0, 249.91)
TILE_SMALL_FRAC = float(sys.argv[1]) if len(sys.argv) > 1 else 0.78
k_s = TILE_SMALL_FRAC * 256 / (SIL_S[1] - SIL_S[0])
write("mercury-tile-small.svg", svg(256, 256, tile(SMALL_D, SIL_S, TILE_SMALL_FRAC, 0, rx=74, cxo=128 + 4 * k_s * 0.5)))

print("lockup width", WH)
