"""System drawings for the Mercury Agent kit: construction.svg, heartbeat.svg (storyboard)
and mercury-heartbeat.svg (the animated 'agent running' mark). Writes into design/brand/final/.

    $PY tools/kit_system.py
"""
import math
from pathlib import Path
import pathops
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.svgLib.path import parse_path

OUT = Path(__file__).resolve().parent.parent / "final"
S = 216 / 220
CX, CY = 128 - S * 10 + 4, 128.0
DOTX = CX + 80 * S


def f(v):
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def circle_d(cx, cy, rr):
    return (f"M{f(cx - rr)} {f(cy)}A{f(rr)} {f(rr)} 0 1 1 {f(cx + rr)} {f(cy)}"
            f"A{f(rr)} {f(rr)} 0 1 1 {f(cx - rr)} {f(cy)}Z")


def circ(cx, cy, rr):
    p = pathops.Path()
    parse_path(circle_d(cx, cy, rr), p.getPen())
    return p


def pd(p):
    pen = SVGPathPen(None, ntos=f)
    p.draw(pen)
    return pen.getCommands()


def frame(dd, bite_k=1.0, dot=True, dot_k=1.0):
    """Design units, disc R 100 at the origin. dd = disc centre -> dot centre."""
    shape = circ(0, 0, 100)
    if bite_k > 0:
        shape = pathops.op(shape, circ(dd, 0, 60 * bite_k), pathops.PathOp.DIFFERENCE)
    if dot:
        shape = pathops.op(shape, circ(dd, 0, 40 * dot_k), pathops.PathOp.UNION)
    return pd(shape)


INK, ACC, SOFT, LINE, MUTED = "#211C35", "#6D53D3", "#ECE6FD", "#D9CFFA", "#716A90"
FONT = "font-family=\"Geist, -apple-system, 'Segoe UI', sans-serif\""
MONO = "font-family=\"'Geist Mono', ui-monospace, Menlo, monospace\""

# ---- construction.svg ----------------------------------------------------
def construction():
    u = 20
    x0, y0, w, h = -150, -150, 420, 300
    o = []
    # grid
    for gx in range(-120, 161, u):
        o.append(f'<line x1="{gx}" y1="-120" x2="{gx}" y2="120" stroke="{LINE}" stroke-width="0.5"/>')
    for gy in range(-120, 121, u):
        o.append(f'<line x1="-120" y1="{gy}" x2="160" y2="{gy}" stroke="{LINE}" stroke-width="0.5"/>')
    # the mark, soft
    o.append(f'<path fill="{SOFT}" stroke="{INK}" stroke-width="0.8" d="{frame(80)}"/>')
    # axes
    o.append(f'<line x1="-135" y1="0" x2="170" y2="0" stroke="{ACC}" stroke-width="0.8"/>')
    o.append(f'<line x1="0" y1="0" x2="{f(130*math.cos(math.pi/4))}" y2="{f(-130*math.sin(math.pi/4))}" '
             f'stroke="{MUTED}" stroke-width="0.6" stroke-dasharray="3 3"/>')
    # circles
    o.append(f'<circle cx="0" cy="0" r="100" fill="none" stroke="{ACC}" stroke-width="0.8" stroke-dasharray="4 3"/>')
    o.append(f'<circle cx="80" cy="0" r="60" fill="none" stroke="{ACC}" stroke-width="0.8" stroke-dasharray="4 3"/>')
    o.append(f'<circle cx="80" cy="0" r="40" fill="none" stroke="{ACC}" stroke-width="0.8"/>')
    # 3-4-5 triangle
    o.append(f'<path d="M0 0H80V-60Z" fill="none" stroke="{ACC}" stroke-width="1.4" stroke-linejoin="round"/>')
    o.append(f'<path d="M72 0V-8H80" fill="none" stroke="{ACC}" stroke-width="0.8"/>')
    for cx in (0, 80):
        o.append(f'<circle cx="{cx}" cy="0" r="2.2" fill="{ACC}"/>')
    o.append(f'<circle cx="80" cy="-60" r="2.2" fill="{ACC}"/><circle cx="80" cy="60" r="2.2" fill="{ACC}"/>')
    # labels
    def t(x, y, s, anchor="start", col=INK, size=9, mono=False, weight=500):
        fam = MONO if mono else FONT
        common = f'x="{f(x)}" y="{f(y)}" {fam} font-size="{size}" font-weight="{weight}" text-anchor="{anchor}"'
        o.append(f'<text {common} fill="none" stroke="#FFFFFF" stroke-width="3" stroke-linejoin="round">{s}</text>')
        o.append(f'<text {common} fill="{col}">{s}</text>')
    t(40, 12, "d = 4u", "middle", ACC, mono=True)
    t(84, -30, "B = 3u", "start", ACC, mono=True)
    t(34, -30, "R = 5u", "middle", ACC, mono=True)
    t(80, 26, "r = 2u", "middle", ACC, mono=True)
    # gap callout: radial between dot (40) and bite (60) on the lower-right 45
    gx1, gy1 = 80 + 40 * math.cos(math.radians(-40)), -40 * math.sin(math.radians(-40))
    gx2, gy2 = 80 + 60 * math.cos(math.radians(-40)), -60 * math.sin(math.radians(-40))
    o.append(f'<line x1="{f(gx1)}" y1="{f(gy1)}" x2="{f(gx2)}" y2="{f(gy2)}" stroke="{INK}" stroke-width="1.2"/>')
    t(gx2 + 4, gy2 + 10, "g = 1u", "start", INK, mono=True)
    t(168, -5, "travel axis", "end", ACC, size=8)
    t(96, -96, "45°, the badge corner, not used", "start", MUTED, size=8)
    t(-120, -128, "Grid unit u = 20 (disc radius 100)", "start", INK, size=9, weight=600)
    t(-120, 138, "g : r : B : d : R  =  1 : 2 : 3 : 4 : 5", "start", INK, size=9, mono=True)
    t(160, -128, "d² + B² = R²", "end", ACC, size=9, mono=True)
    body = "\n  ".join(o)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0} {y0} {w} {h}" width="{w*2}" height="{h*2}" '
            f'role="img">\n  <title>Mercury Agent construction</title>\n  <rect x="{x0}" y="{y0}" width="{w}" '
            f'height="{h}" fill="#FFFFFF"/>\n  {body}\n</svg>\n')


# ---- heartbeat.svg (storyboard) -------------------------------------------
FRAMES = [  # (label, time, dd, bite_k, dot, note)
    ("Asleep", "0.00 s", 0, 0, False, "Closed disc"),
    ("Wake", "0.29 s", 24, 1, True, "A dot appears inside"),
    ("Act", "0.45 s", 58, 1, True, "It crosses the rim"),
    ("Ready", "1.39–2.11 s", 80, 1, True, "Waits for your yes"),
    ("Sent", "2.11–2.40 s", 88, 0.45, True, "Goes out, the disc heals"),
]


def storyboard():
    fw, fh = 320, 330
    o = []
    for i, (lab, tm, dd, bk, dot, note) in enumerate(FRAMES):
        x = i * fw + 135
        o.append(f'<rect x="{i*fw+8}" y="8" width="{fw-16}" height="{fh-16}" rx="12" fill="#FFFFFF" '
                 f'stroke="#E8E3F5" stroke-width="2"/>')
        o.append(f'<path transform="translate({x} 140) scale(0.85)" fill="{INK}" d="{frame(dd, bk, dot, 0.45 if lab == 'Sent' else 1.0)}"/>')
        o.append(f'<text x="{i*fw+32}" y="286" {FONT} font-size="20" font-weight="600" fill="{INK}">{lab}</text>')
        o.append(f'<text x="{i*fw+fw-32}" y="286" {MONO} font-size="16" fill="{MUTED}" text-anchor="end">{tm}</text>')
        o.append(f'<text x="{i*fw+32}" y="306" {FONT} font-size="15" fill="{MUTED}">{note}</text>')
    W = fw * len(FRAMES)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {fh}" width="{W}" height="{fh}" role="img">\n'
            f'  <title>Mercury Agent heartbeat storyboard</title>\n  ' + "\n  ".join(o) + "\n</svg>\n")


# ---- mercury-heartbeat.svg (animated) --------------------------------------
def off(dd):
    return f((dd - 80) * S)


def animated():
    R, r, B = 100 * S, 40 * S, 60 * S
    css = f"""
    .ink {{ fill: var(--accent, #6D53D3); }}
    @media (prefers-color-scheme: dark) {{ .ink {{ fill: var(--accent, #B7A4F7); }} }}
    .mv {{ transform-box: fill-box; transform-origin: center; animation: 2.4s infinite both; }}
    .dot {{ animation-name: am-dot; }}
    .bite {{ animation-name: am-bite; }}
    @keyframes am-dot {{
      0%   {{ transform: translateX({off(24)}px) scale(0); animation-timing-function: cubic-bezier(.33,1,.68,1); }}
      12%  {{ transform: translateX({off(24)}px) scale(1); animation-timing-function: cubic-bezier(.16,1,.3,1); }}
      58%  {{ transform: translateX(0) scale(1); }}
      88%  {{ transform: translateX(0) scale(1); animation-timing-function: cubic-bezier(.5,0,.75,0); }}
      100% {{ transform: translateX({off(90)}px) scale(0); }}
    }}
    @keyframes am-bite {{
      0%   {{ transform: translateX({off(24)}px) scale(0); animation-timing-function: cubic-bezier(.33,1,.68,1); }}
      12%  {{ transform: translateX({off(24)}px) scale(1); animation-timing-function: cubic-bezier(.16,1,.3,1); }}
      58%  {{ transform: translateX(0) scale(1); }}
      88%  {{ transform: translateX(0) scale(1); animation-timing-function: cubic-bezier(.5,0,.75,0); }}
      100% {{ transform: translateX({off(90)}px) scale(0); }}
    }}
    @media (prefers-reduced-motion: reduce) {{ .mv {{ animation: none; }} }}
  """
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="256" height="256" role="img">
  <title>Mercury Agent is running</title>
  <style>{css}</style>
  <mask id="am-bite-mask" maskUnits="userSpaceOnUse" x="0" y="0" width="256" height="256">
    <rect width="256" height="256" fill="#000"/>
    <circle cx="{f(CX)}" cy="128" r="{f(R)}" fill="#FFF"/>
    <circle class="mv bite" cx="{f(DOTX)}" cy="128" r="{f(B)}" fill="#000"/>
  </mask>
  <circle class="ink" cx="{f(CX)}" cy="128" r="{f(R)}" mask="url(#am-bite-mask)"/>
  <circle class="ink mv dot" cx="{f(DOTX)}" cy="128" r="{f(r)}"/>
</svg>
"""


if __name__ == "__main__":
    for name, fn in (("construction.svg", construction), ("heartbeat.svg", storyboard),
                     ("mercury-heartbeat.svg", animated)):
        (OUT / name).write_text(fn())
        print("wrote", name)
