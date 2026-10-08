"""Build the Mercury Agent brand book: book/index.html (self-contained apart from the repo fonts and
the dashboard screenshots, both linked by relative path).

    $PY tools/book_build.py && $PY tools/book_render.py
"""
import base64
import io
import json
import math
import re
from pathlib import Path

import cairosvg
import pathops
from PIL import Image
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.svgLib.path import parse_path

ROOT = Path(__file__).resolve().parent.parent
FINAL, BOOK = ROOT / "final", ROOT / "book"
ICONS = json.loads((ROOT / "tools" / "phosphor-subset.json").read_text())
NPAGES = 18


def rd(name):
    return (FINAL / name).read_text()


def path_d(name):
    return re.search(r' d="([^"]+)"', rd(name)).group(1)


def vb(name):
    return re.search(r'viewBox="([^"]+)"', rd(name)).group(1)


SYM, REV, SMALL = path_d("mercury-symbol.svg"), path_d("mercury-symbol-reversed.svg"), path_d("mercury-symbol-small.svg")
LOCK_H, LOCK_S, WORD = path_d("mercury-lockup-horizontal.svg"), path_d("mercury-lockup-stacked.svg"), path_d("mercury-wordmark.svg")
VB_H, VB_S, VB_W = vb("mercury-lockup-horizontal.svg"), vb("mercury-lockup-stacked.svg"), vb("mercury-wordmark.svg")
UID = [0]


def uid(p):
    UID[0] += 1
    return f"{p}{UID[0]}"


def svgp(d, viewbox, fill="currentColor", cls="", style="", extra=""):
    return (f'<svg class="{cls}" style="{style}" viewBox="{viewbox}" role="img" aria-label="Mercury Agent" '
            f'{extra}><path fill="{fill}" d="{d}"/></svg>')


def sym(fill="currentColor", cls="", style="", rev=False):
    return svgp(REV if rev else SYM, "0 0 256 256", fill, cls, style)


def small(fill="currentColor", cls="", style=""):
    return svgp(SMALL, "0 0 256 256", fill, cls, style)


def lockup(fill="currentColor", cls="", style="", stacked=False, rev=False):
    d = LOCK_S if stacked else LOCK_H
    if rev:
        m = re.match(r"(M[^M]*Z)(M[^M]*Z)", d)
        x0, y0 = [float(v) for v in re.match(r"M(-?[\d.]+) (-?[\d.]+)", m.group(0)).groups()]
        d = shift(REV, x0 - 200.73, y0 - 69.09) + d[len(m.group(0)):]
    return svgp(d, VB_S if stacked else VB_H, fill, cls, style)


def word(fill="currentColor", cls="", style=""):
    return svgp(WORD, VB_W, fill, cls, style)


def f2(v):
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def shift(d, tx, ty):
    out = []
    for cmd, args in re.findall(r"([MAZ])([^MAZ]*)", d):
        n = [float(v) for v in re.findall(r"-?[\d.]+", args)]
        if cmd == "M":
            out.append(f"M{f2(n[0]+tx)} {f2(n[1]+ty)}")
        elif cmd == "A":
            for i in range(0, len(n), 7):
                a = n[i:i + 7]
                out.append(f"A{f2(a[0])} {f2(a[1])} {f2(a[2])} {int(a[3])} {int(a[4])} {f2(a[5]+tx)} {f2(a[6]+ty)}")
        else:
            out.append("Z")
    return "".join(out)


def tile(cls="", style="", small_cut=False, square=False):
    g = uid("tg")
    raw = rd("mercury-tile-small.svg" if small_cut else "mercury-tile.svg")
    inner = re.search(r"</title>(.*)</svg>", raw, re.S).group(1).replace("tile-grad", g)
    if square:
        inner = inner.replace('rx="74"', 'rx="0"')
    return f'<svg class="{cls}" style="{style}" viewBox="0 0 256 256" role="img" aria-label="Mercury Agent">{inner}</svg>'


def inline(name, cls="", style=""):
    raw = rd(name)
    raw = re.sub(r"<\?xml[^>]*>", "", raw)
    raw = re.sub(r'\swidth="[\d.]+"\s+height="[\d.]+"', "", raw, count=1)
    return raw.replace("<svg ", f'<svg class="{cls}" style="{style}" ', 1)


def heartbeat(cls="", style=""):
    raw = rd("mercury-heartbeat.svg")
    raw = raw.replace("am-bite-mask", uid("abm"))
    raw = re.sub(r'\swidth="256" height="256"', "", raw, count=1)
    return raw.replace("<svg ", f'<svg class="{cls}" style="{style}" ', 1)


def ico(name, cls=""):
    return f'<svg class="ph {cls}" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true">{ICONS[name]}</svg>'


def badge(tone, icon, text):
    return f'<span class="badge t-{tone}">{ico(icon)}{text}</span>'


def png_uri(svg_text, w, h, bg=None, scale=1):
    png = cairosvg.svg2png(bytestring=svg_text.encode(), output_width=w, output_height=h,
                           background_color=bg)
    im = Image.open(io.BytesIO(png))
    if scale != 1:
        im = im.resize((w * scale, h * scale), Image.NEAREST)
    b = io.BytesIO()
    im.save(b, "PNG")
    return "data:image/png;base64," + base64.b64encode(b.getvalue()).decode()


# ---- colour maths -------------------------------------------------------------
def rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def lum(h):
    def c(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = rgb(h)
    return 0.2126 * c(r) + 0.7152 * c(g) + 0.0722 * c(b)


def contrast(a, b):
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def cmyk(h):
    r, g, b = [v / 255 for v in rgb(h)]
    k = 1 - max(r, g, b)
    if k >= 1:
        return (0, 0, 0, 100)
    return tuple(round(v * 100) for v in ((1 - r - k) / (1 - k), (1 - g - k) / (1 - k), (1 - b - k) / (1 - k), k))


# ---- shapes for the misuse page -------------------------------------------------
def circ(cx, cy, rr):
    p = pathops.Path()
    parse_path(f"M{cx-rr} {cy}A{rr} {rr} 0 1 1 {cx+rr} {cy}A{rr} {rr} 0 1 1 {cx-rr} {cy}Z", p.getPen())
    return p


def frame(dot_xy, bite_r=60, dot_r=40, bite_xy=None):
    bx, by = bite_xy or dot_xy
    shape = pathops.op(circ(0, 0, 100), circ(bx, by, bite_r), pathops.PathOp.DIFFERENCE)
    shape = pathops.op(shape, circ(dot_xy[0], dot_xy[1], dot_r), pathops.PathOp.UNION)
    pen = SVGPathPen(None, ntos=f2)
    shape.draw(pen)
    return pen.getCommands()


# ================================================================================
CSS = r"""
@font-face { font-family: 'Geist'; font-style: normal; font-weight: 400 600; src: url('../../../mercury/web/fonts/geist.woff2') format('woff2'); }
@font-face { font-family: 'Geist Mono'; font-style: normal; font-weight: 400 500; src: url('../../../mercury/web/fonts/geist-mono.woff2') format('woff2'); }
:root {
  --sans: 'Geist', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --mono: 'Geist Mono', ui-monospace, 'SF Mono', Menlo, monospace;
  --r-sm: 8px; --r-md: 12px; --r-pill: 999px;
  --bg: #F7F5FD; --panel: #FFFFFF; --panel-raised: #F2EEFC; --border: #E8E3F5; --border-strong: #D5CCEC;
  --text: #211C35; --text-2: #575073; --text-3: #716A90;
  --accent: #6D53D3; --accent-deep: #5639BD; --accent-contrast: #FFFFFF; --accent-soft: #ECE6FD; --accent-line: #D9CFFA;
  --s-good: #2F6B47; --s-wait: #8A5B12; --s-bad: #A3343A; --s-active: #2D5F8A; --s-note: #9B3570;
  --shadow-sm: 0 1px 2px rgba(64, 44, 140, 0.05), 0 1px 1px rgba(64, 44, 140, 0.03);
  --shadow: 0 12px 32px rgba(64, 44, 140, 0.12), 0 2px 6px rgba(64, 44, 140, 0.06);
  --wash: radial-gradient(1200px 420px at 12% -10%, #EDE6FF 0%, transparent 60%),
          radial-gradient(900px 380px at 100% 0%, #F6E9FB 0%, transparent 55%);
  --tile: linear-gradient(220deg, #C7B6FF 0%, #9C84F0 55%, #7A5EDB 100%);
}
.dark {
  --bg:#14111E; --panel:#1B1728; --panel-raised:#242036; --border:#2C2740; --border-strong:#3D3657;
  --text:#EDE9F8; --text-2:#ADA5C8; --text-3:#8A82A8;
  --accent:#B7A4F7; --accent-deep:#C9BAFA; --accent-contrast:#1A1530;
  --accent-soft:rgba(183,164,247,0.14); --accent-line:rgba(183,164,247,0.32);
  --s-good:#8CCBA3; --s-wait:#E3BC74; --s-bad:#EE9AA0; --s-active:#93BDE6; --s-note:#EBA1CB;
  --shadow-sm: 0 1px 2px rgba(0,0,0,0.3); --shadow: 0 12px 32px rgba(0,0,0,0.5);
  --wash: radial-gradient(1200px 420px at 12% -10%, rgba(140,110,240,0.16) 0%, transparent 60%);
  color: var(--text); background: var(--bg);
}
@page { size: 1600px 900px; margin: 0; }
* { margin: 0; padding: 0; box-sizing: border-box; }
html { background: #8A82A8; }
body { font-family: var(--sans); color: var(--text); font-size: 15px; line-height: 1.55;
  -webkit-font-smoothing: antialiased; font-feature-settings: 'cv11', 'ss01'; }
@media screen { body { padding: 32px 0; } .page { margin: 0 auto 32px; box-shadow: var(--shadow); } }
.page { width: 1600px; height: 900px; padding: 52px 80px 56px; position: relative; overflow: hidden;
  background: var(--wash), var(--bg); color: var(--text); display: flex; flex-direction: column;
  break-after: page; }
.page-head { display: flex; justify-content: space-between; align-items: center; font-size: 13px;
  color: var(--text-3); margin-bottom: 28px; }
.page-head .num { font-family: var(--mono); font-variant-numeric: tabular-nums; }
.page-head .who { display: flex; align-items: center; gap: 8px; font-weight: 500; }
.page-head .who svg { width: 18px; height: 18px; color: var(--accent); }
h1 { font-size: 72px; font-weight: 600; letter-spacing: -0.035em; line-height: 1.02; }
h2 { font-size: 40px; font-weight: 600; letter-spacing: -0.03em; line-height: 1.1; }
h3 { font-size: 17px; font-weight: 600; letter-spacing: -0.015em; line-height: 1.3; }
.lede { font-size: 18px; color: var(--text-2); margin-top: 10px; max-width: 70ch; text-wrap: pretty; }
p { text-wrap: pretty; }
.small { font-size: 13px; color: var(--text-3); }
.muted { color: var(--text-2); }
.num, .mono { font-family: var(--mono); font-variant-numeric: tabular-nums; }
.body { flex: 1; min-height: 0; margin-top: 28px; display: grid; gap: 24px; }
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: var(--r-md); overflow: hidden;
  box-shadow: var(--shadow-sm); min-width: 0; min-height: 0; position: relative; }
.panel-pad { padding: 22px 24px; }
.panel-head { padding: 16px 20px 0; }
.panel-head p { font-size: 13px; color: var(--text-3); margin-top: 2px; }
.panel-foot { padding: 11px 20px; font-size: 13px; color: var(--text-2); background: var(--panel-raised);
  border-top: 1px solid var(--border); }
.stage { display: flex; align-items: center; justify-content: center; }
.ph { width: 1em; height: 1em; flex: none; display: inline-block; vertical-align: -0.125em; }
.badge { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 500; color: var(--text-2); white-space: nowrap; }
.badge .ph { font-size: 15px; color: var(--text-3); }
.badge.t-waiting .ph { color: #C98A1E; } .badge.t-active .ph { color: var(--s-active); }
.badge.t-good .ph { color: #2F9460; } .badge.t-bad .ph, .badge.t-bad { color: var(--s-bad); }
.badge.t-idle { color: var(--text-3); } .badge.t-note .ph { color: var(--s-note); }
.dark .badge.t-good .ph { color: var(--s-good); } .dark .badge.t-waiting .ph { color: var(--s-wait); }
.btn { display: inline-flex; align-items: center; gap: 7px; font-family: var(--sans); font-size: 14px; font-weight: 500;
  padding: 9px 16px; border-radius: var(--r-sm); border: 1px solid transparent; white-space: nowrap; }
.btn-primary { background: var(--accent); color: var(--accent-contrast); box-shadow: 0 1px 2px rgba(64,44,140,.18), inset 0 1px 0 rgba(255,255,255,.18); }
.btn-secondary { background: var(--panel); border-color: var(--border-strong); color: var(--text); }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th { text-align: left; font-size: 12px; font-weight: 600; color: var(--text-3); padding: 0 12px 8px 0; border-bottom: 1px solid var(--border); }
td { padding: 8px 12px 8px 0; border-bottom: 1px solid var(--border); vertical-align: top; }
tr:last-child td { border-bottom: none; }
.tight td { padding: 5px 12px 5px 0; }
.cap { font-size: 13px; color: var(--text-3); margin-top: 10px; }
.list { display: flex; flex-direction: column; gap: 14px; }
.voice h3 { font-size: 19px; } .voice h3 + p { font-size: 16px !important; }
.list h3 + p { margin-top: 3px; color: var(--text-2); font-size: 15px; }
.chiprow { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
.word { display: inline-flex; align-items: center; padding: 5px 12px; border-radius: var(--r-pill); border: 1px solid var(--border-strong);
  background: var(--panel); font-size: 14px; font-weight: 500; }
.word.no { color: var(--text-3); background: none; border-style: dashed; }
.tilebg { background: var(--tile); }
.swatch { height: 52px; border-bottom: 1px solid var(--border); }
.kv { display: grid; grid-template-columns: auto 1fr; gap: 3px 14px; font-size: 13px; }
.kv dt { color: var(--text-3); } .kv dd { font-family: var(--mono); font-variant-numeric: tabular-nums; }
.pix { image-rendering: pixelated; display: block; }
.term { font-family: var(--mono); font-size: 14px; line-height: 1.35; white-space: pre; }
/* the dashboard sidebar brand block, verbatim from app.css */
.brand { display: flex; align-items: center; gap: 10px; padding: 2px 6px; }
.brand .mark { width: 30px; height: 30px; flex: none; border-radius: 9px; display: block;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.35), 0 2px 8px rgba(109, 83, 211, 0.25); }
.brand .mark svg { width: 30px; height: 30px; display: block; }
.brand h4 { font-size: 15.5px; font-weight: 600; letter-spacing: -0.02em; line-height: 1.1; }
.brand .tagline { font-size: 11.5px; font-weight: 500; color: var(--text-3); line-height: 1.2; margin-top: 2px; }
.agent-card { display: flex; align-items: center; gap: 10px; padding: 11px 12px; background: var(--panel);
  border: 1px solid var(--border); border-radius: var(--r-md); box-shadow: var(--shadow-sm); }
.agent-card .t { font-size: 12.5px; font-weight: 600; } .agent-card .s { font-size: 11.5px; color: var(--text-3); }
.zoom2 { transform: scale(2); transform-origin: 0 0; }
.dont { display: flex; flex-direction: column; }
.dont .stage { flex: 1; background: var(--panel); min-height: 0; }
.dont .panel-foot { display: flex; align-items: center; }
"""


def head(n, section):
    return (f'<div class="page-head"><span class="who">{sym(cls="")}Mercury Agent brand book</span>'
            f'<span><span class="num">{n:02d}</span>&nbsp;&nbsp;{section}</span></div>')


def page(n, section, title, lede, body, cls="", body_style=""):
    t = f"<h2>{title}</h2>" if title else ""
    l = f'<p class="lede">{lede}</p>' if lede else ""
    return (f'<section class="page {cls}" id="p{n:02d}">{head(n, section)}{t}{l}'
            f'<div class="body" style="{body_style}">{body}</div></section>')


P = []

# 01 Cover ------------------------------------------------------------------------
P.append(f'''<section class="page" id="p01" style="justify-content:space-between;padding:72px 96px">
  <div class="page-head" style="margin:0"><span>EBSY</span><span class="num">v1.0, October 2026</span></div>
  <div>
    {lockup(fill="var(--text)", style="height:132px;width:auto;display:block;margin-left:-12px")}
    <h1 style="margin-top:56px">Brand book</h1>
    <p class="lede" style="font-size:22px;max-width:46ch">The logo, the dot, colour, type and voice for Mercury&nbsp;Agent,
      the sales agent that runs on your own machine.</p>
  </div>
  <div style="display:flex;justify-content:space-between;align-items:flex-end">
    <p class="small">Mercury Agent by EBSY. Mercury proposes; you confirm.</p>
    <div style="display:flex;gap:16px;align-items:flex-end">
      {tile(style="width:52px;height:52px;border-radius:15px;box-shadow:var(--shadow-sm)")}
      {tile(style="width:30px;height:30px;border-radius:9px")}
      {tile(small_cut=True, style="width:16px;height:16px")}
    </div>
  </div>
</section>''')

# 02 The idea ---------------------------------------------------------------------
P.append(page(2, "The idea", "A messenger that never sleeps, and still waits for your yes.",
  "Mercury was the messenger of the gods. It is also the smallest, fastest planet, and the one that works "
  "closest to the sun. A few times a century it crosses the sun's face: a dark dot moving along a straight "
  "line. That crossing, the transit, is the mark.",
  f'''<div class="panel panel-pad list" style="gap:22px;justify-content:center">
        <div><h3 style="font-size:20px">What it does</h3><p style="font-size:16px">Every 15 minutes Mercury Agent wakes, finds prospects, writes email and reads
          replies. Then it stops and waits. Nothing goes out until a person says yes.</p></div>
        <div><h3 style="font-size:20px">What the mark says</h3><p style="font-size:16px">A body leaving a disc, caught half out. The work is done and ready to go,
          and it is still waiting for your yes.</p></div>
        <div><h3 style="font-size:20px">The one line</h3><p style="font-size:28px;color:var(--text);font-weight:600;letter-spacing:-0.02em;margin-top:6px">
          Mercury proposes; you confirm.</p></div>
    </div>
    <div class="panel stage" style="background:var(--accent-soft);border-color:var(--accent-line);flex-direction:column;gap:18px">
      {sym(fill="var(--accent)", style="width:260px;height:260px")}
      <p class="small" style="font-size:14px;color:var(--text-2)">Proposal ready. Waiting for your yes.</p></div>
    <div style="display:grid;grid-template-rows:1fr 1fr;gap:24px">
      <div class="panel panel-pad"><h3>It should feel</h3>
        <div class="chiprow"><span class="word">Tireless</span><span class="word">Calm</span><span class="word">Accountable</span>
          <span class="word">Precise</span><span class="word">Warm</span></div>
        <p class="cap">Round forms, one steady axis, no sharp tricks, a soft violet.</p></div>
      <div class="panel panel-pad"><h3>It should never feel</h3>
        <div class="chiprow"><span class="word no">Hype or growth hack</span><span class="word no">Robotic sci-fi AI</span>
          <span class="word no">Cold corporate</span></div>
        <p class="cap">No rockets, sparkles, paper planes, envelopes, robot faces, winged helmets or ringed planets.</p></div>
    </div>''', body_style="grid-template-columns:1.05fr 0.85fr 1fr"))

# 03 The mark ---------------------------------------------------------------------
P.append(page(3, "The mark", "", "",
  f'''<div class="panel stage" style="background:var(--panel)">{sym(fill="var(--text)", style="width:500px;height:500px")}</div>
    <div style="display:flex;flex-direction:column;justify-content:center;gap:28px;padding-right:12px">
      <div><h2>The transit</h2><p class="lede">A dot crossing the edge of a disc along the horizontal.</p></div>
      <div class="list">
        <div><h3>The disc</h3><p>The work, and the sun the planet crosses. Solid, round and steady, so the mark
          feels calm at any size.</p></div>
        <div><h3>The dot at the limb</h3><p>Mercury itself, the agent. It sits half out at three o'clock, on the
          line the eye reads along, pointing into the name.</p></div>
        <div><h3>The gap</h3><p>The pause before sending. A clean ring of space keeps the dot apart from the disc:
          the work is ready, and nothing leaves until you say yes.</p></div>
      </div>
    </div>''', body_style="grid-template-columns:600px 1fr;margin-top:0"))

# 04 Construction -----------------------------------------------------------------
P.append(page(4, "Construction", "Five numbers, one triangle",
  "Everything is built from one grid unit. The gap, the dot, the bite, the travel and the disc are 1, 2, 3, 4 and 5 units.",
  f'''<div class="panel stage" style="padding:12px">{inline("construction.svg", style="width:100%;height:100%")}</div>
    <div style="display:flex;flex-direction:column;gap:20px">
      <div class="panel panel-pad"><table>
        <tr><th>Part</th><th>Units</th><th>On the 256 canvas</th></tr>
        <tr><td>Gap g</td><td class="num">1u</td><td class="num">19.64</td></tr>
        <tr><td>Dot radius r</td><td class="num">2u</td><td class="num">39.27</td></tr>
        <tr><td>Bite radius B = r + g</td><td class="num">3u</td><td class="num">58.91</td></tr>
        <tr><td>Disc centre to dot centre d</td><td class="num">4u</td><td class="num">78.55</td></tr>
        <tr><td>Disc radius R</td><td class="num">5u</td><td class="num">98.18</td></tr>
      </table></div>
      <div class="panel panel-pad list" style="gap:10px">
        <p class="muted"><b style="color:var(--text)">3, 4, 5.</b> Because d² + B² = R², the bite meets the rim exactly
          above and below the dot's centre. Both horns are clean 53° points.</p>
        <p class="muted"><b style="color:var(--text)">Optical centre.</b> The disc carries the weight, so the mark sits
          4 units right of its box centre (margins 24 left, 16 right).</p>
        <p class="muted"><b style="color:var(--text)">One path.</b> Six anchors, all arcs, mirror-symmetric about the
          horizontal. Never redraw it; use the master files.</p>
      </div>
    </div>''', body_style="grid-template-columns:1fr 520px;margin-top:20px"))

# 05 The dot as a system ----------------------------------------------------------
P.append(page(5, "The dot as a system", "The dot is the agent",
  "On its own the dot means Mercury is here. Moving, it shows the heartbeat: wake, act, wait for your yes, send.",
  f'''<div class="panel" style="grid-column:1 / -1">{inline("heartbeat.svg", style="width:100%;height:auto;display:block")}</div>
    <div class="panel panel-pad"><h3>Motion</h3>
      <table style="margin-top:10px;font-size:13px">
        <tr><th>Phase</th><th>Time</th><th>cubic-bezier</th></tr>
        <tr><td>Wake</td><td class="num" style="white-space:nowrap">0–0.29 s</td><td class="num">.33,1,.68,1</td></tr>
        <tr><td>Act</td><td class="num" style="white-space:nowrap">0.29–1.39 s</td><td class="num">.16,1,.3,1</td></tr>
        <tr><td>Ready</td><td class="num" style="white-space:nowrap">1.39–2.11 s</td><td class="num">hold</td></tr>
        <tr><td>Sent</td><td class="num" style="white-space:nowrap">2.11–2.40 s</td><td class="num">.5,0,.75,0</td></tr>
      </table>
      <p class="cap">One loop is 2.4 s. Reduced motion stops in the logo pose.</p></div>
    <div class="panel panel-pad"><h3>States in the dashboard</h3>
      <table style="margin-top:10px;font-size:13px">
        <tr><th>Agent</th><th>Mark</th><th>Status stays a badge</th></tr>
        <tr><td>Quiet hours or stopped</td><td>Closed disc, no dot</td><td>{badge("idle", "moon", "Asleep until 7:00")}</td></tr>
        <tr><td>Running a cycle</td><td>The heartbeat loop</td><td>{badge("active", "pulse", "Running")}</td></tr>
        <tr><td>Proposal ready</td><td>Still, in the logo pose</td><td>{badge("waiting", "clock", "Needs you")}</td></tr>
      </table></div>
    <div class="panel panel-pad" style="display:flex;flex-direction:column;gap:14px"><h3>Presence, not status</h3>
      <div class="agent-card">{heartbeat(style="width:22px;height:22px;flex:none")}
        <div style="flex:1"><div class="t">Mercury is running</div><div class="s" style="font-size:12px">Next heartbeat 10:45</div></div>
        {badge("active", "pulse", "Running")}</div>
      <p class="small" style="font-size:13px">The dot sits next to the agent's name. Status is still a Phosphor icon
        plus a word. The dot is always accent violet, never green, amber or red.</p></div>''',
  body_style="grid-template-columns:1fr 1.15fr 0.95fr;grid-template-rows:auto 1fr;margin-top:20px;gap:20px"))

# 06 Lockups ---------------------------------------------------------------------
P.append(page(6, "Lockups", "Four ways to sign",
  "Use the horizontal lockup first. Use the others when the space asks for them. Never rebuild them by hand.",
  f'''<div class="panel" style="grid-column:1 / span 2;display:flex;flex-direction:column">
      <div class="stage" style="flex:1">{lockup(fill="var(--text)", style="width:640px;height:auto")}</div>
      <div class="panel-foot">Horizontal. The primary logo: headers, README, slides.</div></div>
    <div class="panel" style="display:flex;flex-direction:column">
      <div class="stage" style="flex:1">{lockup(fill="var(--text)", stacked=True, style="width:250px;height:auto")}</div>
      <div class="panel-foot">Stacked. Square spaces, social cards.</div></div>
    <div class="panel" style="display:flex;flex-direction:column">
      <div class="stage" style="flex:1">{sym(fill="var(--text)", style="width:150px;height:150px")}</div>
      <div class="panel-foot">Symbol. Icons, avatars, small spaces.</div></div>
    <div class="panel" style="display:flex;flex-direction:column">
      <div class="stage" style="flex:1">{word(fill="var(--text)", style="width:300px;height:auto")}</div>
      <div class="panel-foot">Wordmark. Only when the symbol is already near.</div></div>
    <div class="panel" style="display:flex;flex-direction:column">
      <div class="stage" style="flex:1;flex-direction:column;align-items:flex-start;padding:0 34px">
        {lockup(fill="var(--text)", style="width:300px;height:auto;margin-left:-5px")}
        <div style="margin:2px 0 0 {300*(240+78.55)/float(VB_H.split()[2]) - 5 - 1:.0f}px;font-size:13px;font-weight:500;color:var(--text-2)">by EBSY</div></div>
      <div class="panel-foot">Endorsement. Geist 500, text-2, left-aligned with the M.</div></div>''',
  body_style="grid-template-columns:1fr 1fr 1fr;grid-template-rows:1fr 1fr;margin-top:24px;gap:20px"))

# 07 Clear space & minimum sizes ---------------------------------------------------
LW = float(VB_H.split()[2])          # horizontal lockup width, from the master's viewBox
def zone(art, w, h, ink, unit):
    """art drawn at w x h; ink = (l, t, r, b) in px inside it; unit = 2x in px."""
    l, t, r, b = ink
    x = unit
    W, H = w + 2 * x, h + 2 * x
    dots = "".join(
        f'<div style="position:absolute;left:{cx - x/2:.1f}px;top:{cy - x/2:.1f}px;width:{x:.1f}px;height:{x:.1f}px;border-radius:50%;background:var(--accent-line)"></div>'
        for cx, cy in ((x + l - x / 2, x + (t + b) / 2), (x + r + x / 2, x + (t + b) / 2),
                       (x + (l + r) / 2, x + t - x / 2), (x + (l + r) / 2, x + b + x / 2)))
    return f'''<div style="position:relative;width:{W:.0f}px;height:{H:.0f}px">
      <div style="position:absolute;left:{l:.1f}px;top:{t:.1f}px;width:{r - l + 2*x:.1f}px;height:{b - t + 2*x:.1f}px;
        border:1.5px dashed var(--accent);border-radius:4px;background:var(--accent-soft)"></div>
      {dots}
      <div style="position:absolute;left:{x:.1f}px;top:{x:.1f}px;width:{w}px;height:{h:.1f}px">{art}</div>
    </div>'''


def zone_lockup(width):
    k = width / LW
    return zone(lockup(fill="var(--text)", style=f"width:{width}px;height:auto;display:block"), width, 256 * k,
                (24 * k, 30.3 * k, (LW - 24) * k, 225.7 * k), 2 * 39.27 * k)


def zone_symbol(size):
    k = size / 256
    return zone(sym(fill="var(--text)", style=f"width:{size}px;height:{size}px;display:block"), size, size,
                (24 * k, 30.3 * k, 240 * k, 225.7 * k), 2 * 39.27 * k)


P.append(page(7, "Clear space and minimum sizes", "Give it one dot of room",
  "The unit x is the dot's radius. Keep 2x, one dot, clear on every side. It scales with the logo. The gap between symbol and name is one dot too.",
  f'''<div class="panel stage" style="flex-direction:column;gap:30px">{zone_lockup(640)}
      <div style="display:flex;align-items:center;gap:40px">{zone_symbol(150)}
        <p class="small" style="max-width:30ch;font-size:14px">The shaded field is the clear space. Each lavender circle is one dot, 2x across.</p></div></div>
    <div style="display:flex;flex-direction:column;gap:20px">
      <div class="panel panel-pad"><table>
        <tr><th>Version</th><th>Screen</th><th>Print</th></tr>
        <tr><td>Horizontal lockup</td><td class="num">160 px wide</td><td class="num">35 mm</td></tr>
        <tr><td>Stacked lockup</td><td class="num">120 px wide</td><td class="num">25 mm</td></tr>
        <tr><td>Wordmark</td><td class="num">120 px wide</td><td class="num">25 mm</td></tr>
        <tr><td>Symbol, master</td><td class="num">32 px</td><td class="num">8 mm</td></tr>
        <tr><td>Symbol, small cut</td><td class="num">16 to 24 px</td><td class="num">5 mm</td></tr>
      </table></div>
      <div class="panel panel-pad"><h3>At minimum size, actual pixels</h3>
        <div style="display:flex;align-items:flex-end;gap:28px;margin-top:16px">
          {lockup(fill="var(--text)", style="width:160px;height:auto")}
          {lockup(fill="var(--text)", stacked=True, style="width:120px;height:auto")}
          {sym(fill="var(--text)", style="width:32px;height:32px")}
          {small(fill="var(--text)", style="width:16px;height:16px")}
        </div>
        <p class="cap">In the sidebar and the browser tab the tile is the clear space.</p></div>
    </div>''', body_style="grid-template-columns:1fr 470px;margin-top:24px"))

# 08 Small-size cut ---------------------------------------------------------------
def pixrow(d, label):
    def cells(fg, bg):
        out = ""
        for sz, z in ((16, 12), (24, 8), (32, 6)):
            src = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256"><path fill="{fg}" d="{d}"/></svg>'
            big = png_uri(src, sz, sz, bg=bg, scale=z)
            out += f'''<div style="display:flex;flex-direction:column;align-items:center;gap:8px">
              <img class="pix" src="{big}" style="width:{sz*z}px;height:{sz*z}px;border-radius:4px">
              <div style="display:flex;align-items:center;gap:10px">
                <span style="background:{bg};display:inline-flex;padding:3px;border-radius:4px"><svg viewBox="0 0 256 256" style="width:{sz}px;height:{sz}px"><path fill="{fg}" d="{d}"/></svg></span>
                <span class="small num">{sz} px</span></div></div>'''
        return f'<div style="display:flex;justify-content:space-between">{out}</div>'
    return f'''<div class="panel panel-pad" style="display:flex;flex-direction:column;gap:16px">
      <h3>{label}</h3>{cells("#211C35", "#FFFFFF")}{cells("#FFFFFF", "#14111E")}</div>'''


P.append(page(8, "Small sizes", "A heavier cut for 24 px and below",
  "The small cut fills its square, lands the disc edge on whole pixels at 16 px and opens the gap. Switch to it at 24 px and below; use the master from 32 px up.",
  pixrow(SYM, "Master") + pixrow(SMALL, "Small cut"),
  body_style="grid-template-columns:1fr 1fr;margin-top:24px"))

# 09 Colour versions --------------------------------------------------------------
def cv(bg, art, label, dark=False, extra=""):
    return f'''<div class="panel" style="display:flex;flex-direction:column">
      <div class="stage" style="flex:1;background:{bg};{extra}">{art}</div>
      <div class="panel-foot">{label}</div></div>'''


P.append(page(9, "Colour versions", "Approved logo and background pairs",
  "One colour at a time. The reversed cut is used for every white or light-on-dark version, so it doesn't look heavier than the black one.",
  cv("#FFFFFF", lockup(fill="#211C35", style="width:380px"), "Ink #211C35 on white. The default.")
  + cv("#211C35", lockup(fill="#FFFFFF", rev=True, style="width:380px"), "White on ink, reversed cut.")
  + cv("#ECE6FD", lockup(fill="#6D53D3", style="width:380px"), "Accent #6D53D3 on lavender #ECE6FD.")
  + cv("var(--tile)", sym(fill="#FFFFFF", rev=True, style="width:170px;height:170px"), "White on the tile gradient, reversed cut.")
  + cv("#14111E", lockup(fill="#FFFFFF", rev=True, style="width:380px"), "White on dark #14111E, reversed cut.")
  + cv("#14111E", lockup(fill="#B7A4F7", rev=True, style="width:380px"), "Dark-mode accent #B7A4F7 on #14111E."),
  body_style="grid-template-columns:1fr 1fr 1fr;grid-template-rows:1fr 1fr;margin-top:24px;gap:20px"))

# 10 App icon & favicon -----------------------------------------------------------
def tabbar(dark):
    fav = (f'<svg viewBox="0 0 256 256" style="width:16px;height:16px;flex:none"><path fill="{"#B7A4F7" if dark else "#6D53D3"}" d="{SMALL}"/></svg>')
    bar, tabc, txt, txt2 = (("#14111E", "#242036", "#EDE9F8", "#8A82A8") if dark else ("#E8E3F5", "#FFFFFF", "#211C35", "#716A90"))
    return f'''<div style="background:{bar};padding:8px 10px 0;border-radius:8px 8px 0 0;display:flex;gap:4px">
      <div style="background:{tabc};border-radius:8px 8px 0 0;padding:8px 12px;display:flex;align-items:center;gap:8px;width:220px">
        {fav}<span style="font-size:12px;color:{txt};flex:1">Today · Mercury Agent</span><span style="font-size:12px;color:{txt2}">✕</span></div>
      <div style="padding:8px 12px;display:flex;align-items:center;gap:8px;width:180px">
        <span style="width:16px;height:16px;border-radius:50%;background:{txt2};opacity:.35"></span><span style="font-size:12px;color:{txt2}">Inbox</span></div></div>'''


def brandblock(dark=False):
    return f'''<div class="{'dark' if dark else ''}" style="background:var(--bg);padding:14px 8px;border-radius:var(--r-md);width:252px">
      <div class="brand"><span class="mark">{tile(style="border-radius:9px")}</span>
      <div><h4>Mercury Agent</h4><div class="tagline">Outreach agent</div></div></div></div>'''


P.append(page(10, "App icon and favicon", "The tile",
  "The violet gradient tile from the dashboard, now holding the mark. Corner radius 29 % of the size. Below 32 px use the tile with the small cut.",
  f'''<div class="panel panel-pad" style="grid-column:1 / -1;display:flex;align-items:flex-end;gap:36px">
      {tile(style="width:200px;height:200px;border-radius:58px")}
      <div style="display:flex;flex-direction:column;align-items:center;gap:8px">{tile(style="width:120px;height:120px")}<span class="small num">120</span></div>
      <div style="display:flex;flex-direction:column;align-items:center;gap:8px">{tile(style="width:64px;height:64px")}<span class="small num">64</span></div>
      <div style="display:flex;flex-direction:column;align-items:center;gap:8px">{tile(style="width:52px;height:52px")}<span class="small num">52</span></div>
      <div style="display:flex;flex-direction:column;align-items:center;gap:8px">{tile(style="width:30px;height:30px")}<span class="small num">30</span></div>
      <div style="display:flex;flex-direction:column;align-items:center;gap:8px">{tile(small_cut=True, style="width:24px;height:24px")}<span class="small num">24</span></div>
      <div style="display:flex;flex-direction:column;align-items:center;gap:8px">{tile(small_cut=True, style="width:16px;height:16px")}<span class="small num">16</span></div>
      <div style="margin-left:auto;display:flex;flex-direction:column;align-items:center;gap:8px">
        {tile(square=True, style="width:120px;height:120px;border-radius:50%")}<span class="small">Circle crop, safe</span></div>
    </div>
    <div class="panel"><div class="panel-head"><h3>Browser tab</h3><p>favicon.svg follows the browser theme; favicon.ico is the small tile.</p></div>
      <div style="padding:18px 20px 20px;display:flex;flex-direction:column;gap:14px">{tabbar(False)}{tabbar(True)}
        <div style="display:flex;gap:14px;align-items:center">
          <div style="width:320px;height:66px;overflow:hidden;border-radius:8px"><div class="zoomed" style="zoom:1.5;width:440px">{tabbar(False)}</div></div>
          <div style="width:320px;height:66px;overflow:hidden;border-radius:8px"><div class="zoomed" style="zoom:1.5;width:440px">{tabbar(True)}</div></div>
        </div><p class="small">Above at 1x, below at 1.5x.</p></div></div>
    <div class="panel"><div class="panel-head"><h3>Dashboard sidebar, 30 px tile</h3><p>The .brand block from app.css with the new mark, light and dark, shown at 1x.</p></div>
      <div style="padding:18px 20px 20px;display:flex;flex-direction:column;gap:14px">
        <div style="display:flex;gap:16px">{brandblock(False)}{brandblock(True)}</div>
        <div style="display:flex;gap:16px">
          <div style="width:318px;height:106px;overflow:hidden;border-radius:var(--r-md)"><div class="zoomed" style="zoom:1.8">{brandblock(False)}</div></div>
          <div style="width:318px;height:106px;overflow:hidden;border-radius:var(--r-md)"><div class="zoomed" style="zoom:1.8">{brandblock(True)}</div></div>
        </div><p class="small">Above at 1x, below at 1.8x.</p></div></div>''',
  body_style="grid-template-columns:1fr 1fr;grid-template-rows:auto 1fr;margin-top:24px;gap:20px"))

# 11 Palette, brand -----------------------------------------------------------------
BRAND = [("Accent", "#6D53D3", "Logo on light, primary actions, selection"),
         ("Accent deep", "#5639BD", "Accent as text, hover"),
         ("Ink", "#211C35", "Type, one-colour logo"),
         ("Lavender mist", "#F7F5FD", "Page background"),
         ("Lavender", "#ECE6FD", "Selected surfaces, logo background"),
         ("Dark", "#14111E", "Dark-mode background"),
         ("Accent on dark", "#B7A4F7", "Logo and accent in dark mode")]
sw = ""
for n, h, use in BRAND:
    r, g, b = rgb(h)
    c, m, y, k = cmyk(h)
    sw += f'''<div class="panel" style="display:flex;flex-direction:column">
      <div class="swatch" style="background:{h}"></div>
      <div style="padding:10px 14px"><h3 style="font-size:15px">{n}</h3>
        <dl class="kv" style="margin-top:6px"><dt>HEX</dt><dd>{h}</dd><dt>RGB</dt><dd>{r} {g} {b}</dd><dt>CMYK</dt><dd>{c} {m} {y} {k}</dd></dl>
        <p class="small" style="margin-top:8px">{use}</p></div></div>'''
PAIRS = [("#6D53D3", "#FFFFFF", "Accent on white"), ("#5639BD", "#FFFFFF", "Accent deep on white"),
         ("#211C35", "#F7F5FD", "Ink on lavender mist"), ("#FFFFFF", "#6D53D3", "White on accent (buttons)"),
         ("#6D53D3", "#ECE6FD", "Accent on lavender"), ("#B7A4F7", "#14111E", "Accent on dark"),
         ("#FFFFFF", "#9C84F0", "White on tile, middle"), ("#FFFFFF", "#C7B6FF", "White on tile, lightest corner")]
rows = ""
for fg, bg, lab in PAIRS:
    cr = contrast(fg, bg)
    aa = badge("good", "check-circle", "Pass") if cr >= 4.5 else badge("bad", "x-circle", "Fail")
    lg = badge("good", "check-circle", "Pass") if cr >= 3 else badge("bad", "x-circle", "Fail")
    rows += (f'<tr><td><span style="display:inline-flex;align-items:center;justify-content:center;width:40px;height:24px;'
             f'border-radius:6px;background:{bg};color:{fg};font-weight:600;font-size:13px;border:1px solid var(--border);margin-right:10px">Aa</span>{lab}</td>'
             f'<td class="num">{cr:.2f} : 1</td><td>{aa}</td><td>{lg}</td></tr>')
P.append(page(11, "Palette: brand", "One violet, warm neutrals",
  "The accent is the only colour in the mark. CMYK values are a straight conversion, so proof them before print.",
  f'''<div style="display:grid;grid-template-columns:repeat(7,1fr);gap:14px">{sw}</div>
    <div class="panel" style="padding:16px 24px"><table style="font-size:13px" class="tight">
      <tr><th>Pair</th><th>Contrast</th><th>AA text, 4.5 : 1</th><th>Large text and logo, 3 : 1</th></tr>{rows}</table>
      <p class="cap">White on the lightest corner of the tile is only for the mark, which is large and solid. Never set small text on the tile.</p></div>''',
  body_style="grid-template-rows:auto 1fr;margin-top:20px;gap:20px"))

# 12 Palette, UI tokens ------------------------------------------------------------
LIGHT = [("--bg", "#F7F5FD"), ("--panel", "#FFFFFF"), ("--panel-raised", "#F2EEFC"), ("--border", "#E8E3F5"),
         ("--border-strong", "#D5CCEC"), ("--text", "#211C35"), ("--text-2", "#575073"), ("--text-3", "#716A90"),
         ("--accent", "#6D53D3"), ("--accent-deep", "#5639BD"), ("--accent-soft", "#ECE6FD"), ("--accent-line", "#D9CFFA")]
DARK = [("--bg", "#14111E"), ("--panel", "#1B1728"), ("--panel-raised", "#242036"), ("--border", "#2C2740"),
        ("--border-strong", "#3D3657"), ("--text", "#EDE9F8"), ("--text-2", "#ADA5C8"), ("--text-3", "#8A82A8"),
        ("--accent", "#B7A4F7"), ("--accent-deep", "#C9BAFA"), ("--accent-soft", "14 % accent"), ("--accent-line", "32 % accent")]
STATUS = [("Good", "#2F9460", "#8CCBA3", "check-circle", "good", "Sent"),
          ("Waiting", "#C98A1E", "#E3BC74", "clock", "waiting", "Needs you"),
          ("Bad", "#A3343A", "#EE9AA0", "x-circle", "bad", "Bounced"),
          ("Active", "#2D5F8A", "#93BDE6", "pulse", "active", "Running"),
          ("Note", "#9B3570", "#EBA1CB", "chat-circle-text", "note", "Replied")]


def tokgrid(toks, dark):
    out = ""
    for n, h in toks:
        col = h if h.startswith("#") else ("rgba(183,164,247,0.14)" if "14" in h else "rgba(183,164,247,0.32)")
        out += (f'<div style="display:flex;align-items:center;gap:10px"><span style="width:32px;height:32px;border-radius:8px;'
                f'background:{col};border:1px solid var(--border-strong);flex:none"></span><div><div class="mono" style="font-size:13px">{n}</div>'
                f'<div class="small mono">{h}</div></div></div>')
    return f'<div style="display:grid;grid-template-columns:repeat(2,1fr);gap:12px 12px;margin-top:14px">{out}</div>'


st = "".join(f'''<tr><td>{badge(t, i, w)}</td><td class="mono">{l}</td><td class="mono">{d}</td><td class="small">{n}</td></tr>'''
             for n, l, d, i, t, w in STATUS)
P.append(page(12, "Palette: interface", "The dashboard tokens",
  "Light is the default; dark is the same design in deep aubergine. Status hues belong to meaning. They live only in the icon glyph, and never in the logo, the dot or the animation.",
  f'''<div class="panel panel-pad"><h3>Light</h3>{tokgrid(LIGHT, False)}</div>
    <div class="panel panel-pad dark"><h3>Dark</h3>{tokgrid(DARK, True)}</div>
    <div class="panel panel-pad"><h3>Status, reserved for meaning</h3>
      <table style="margin-top:12px;font-size:13px"><tr><th>Badge</th><th>Light glyph</th><th>Dark glyph</th><th>Meaning</th></tr>{st}</table>
      <p class="cap">An icon plus a word. No tinted pills, no coloured dots, no coloured chips.</p></div>''',
  body_style="grid-template-columns:1fr 1fr 1fr;margin-top:24px;gap:20px"))

# 13 Typography -------------------------------------------------------------------
SCALE = [(28, 600, "-0.03em", "KPI figures, page display", "412 companies"),
         (24, 600, "-0.03em", "Section titles", "Today"),
         (18, 600, "-0.02em", "Subjects, drawer titles", "12 emails waiting for your approval"),
         (15, 600, "-0.015em", "Panel titles", "Needs you"),
         (14, 400, "0", "Body", "Mercury drafted a reply with a 15-minute call link."),
         (13, 400, "0", "Secondary text, tables", "Decisions only you can make, most blocking first."),
         (12, 500, "0", "Badges, labels, footnotes", "Waiting for your yes")]
sc = "".join(f'''<tr><td class="num small" style="width:90px">{s} / {w}</td>
  <td><span style="font-size:{s}px;font-weight:{w};letter-spacing:{ls};line-height:1.2">{ex}</span></td>
  <td class="small" style="width:200px">{use}</td></tr>''' for s, w, ls, use, ex in SCALE)
P.append(page(13, "Typography", "Geist, and Geist Mono for every figure",
  "One family in three weights, 400, 500 and 600, plus its monospaced cut so numbers line up. No serif anywhere.",
  f'''<div class="panel panel-pad" style="display:flex;flex-direction:column"><h3>Scale, from app.css</h3><table style="margin-top:8px">{sc}</table>
      <div style="margin-top:auto;display:grid;grid-template-columns:repeat(3,1fr);gap:14px;padding-top:18px;border-top:1px solid var(--border)">
        <div><div style="font-size:44px;font-weight:400;letter-spacing:-0.03em;line-height:1">Aa</div><p class="small" style="margin-top:6px">400 Regular. Body text.</p></div>
        <div><div style="font-size:44px;font-weight:500;letter-spacing:-0.03em;line-height:1">Aa</div><p class="small" style="margin-top:6px">500 Medium. Labels, buttons.</p></div>
        <div><div style="font-size:44px;font-weight:600;letter-spacing:-0.03em;line-height:1">Aa</div><p class="small" style="margin-top:6px">600 Semibold. Titles, the wordmark.</p></div>
      </div></div>
    <div style="display:flex;flex-direction:column;gap:20px">
      <div class="panel panel-pad"><h3>Geist Mono, tabular</h3>
        <div class="num" style="font-size:40px;font-weight:500;letter-spacing:-0.03em;margin-top:8px;line-height:1.1">1,906&nbsp;&nbsp;10:41</div>
        <div class="num" style="font-size:15px;color:var(--text-2);margin-top:8px">412 · 380 · 214 · 36 · 7</div>
        <p class="cap">Every figure is monospaced so a changing value doesn't shift the layout.</p></div>
      <div class="panel panel-pad"><h3>The wordmark</h3>
        {word(fill="var(--text)", style="width:100%;height:auto;margin-top:12px")}
        <dl class="kv" style="margin-top:12px;font-size:13px"><dt>Face</dt><dd>Geist 600, one weight</dd><dt>Tracking</dt><dd>-20 (-2 %)</dd>
          <dt>Cap height</dt><dd>112 on the 256 lockup, the dot's centre on the cap midline</dd><dt>Files</dt><dd>Outlined. Never type it.</dd></dl></div>
    </div>''', body_style="grid-template-columns:1.25fr 1fr;margin-top:24px;gap:20px"))

# 14 Voice & tone ------------------------------------------------------------------
DOS = [("12 emails waiting for your approval.", "Supercharge your pipeline with AI-powered outreach!"),
       ("Mercury drafted a reply. Check it before it goes out.", "Your AI SDR is crushing it 24/7."),
       ("Found 48 roofers in Denver. It cost $0.02.", "Unlock limitless leads in seconds."),
       ("Nothing is sent until you say yes.", "Sit back and let the magic happen."),
       ("Quiet hours until 7:00.", "Mercury is resting its circuits.")]
dt = "".join(f'<tr><td style="padding:11px 12px 11px 0">{badge("good", "check-circle", "")}&nbsp;{a}</td><td style="color:var(--text-3);padding:11px 12px 11px 0">{badge("bad", "x-circle", "")}&nbsp;{b}</td></tr>' for a, b in DOS)
P.append(page(14, "Voice and tone", "Plain, short, honest",
  "Mercury writes like a careful colleague, not a marketer. Say what happened, what it cost and what is waiting on the person.",
  f'''<div class="panel panel-pad list voice" style="gap:22px">
      <div><h3>Say what is true</h3><p>Counts, times and costs. Never invent a detail or round up a result.</p></div>
      <div><h3>Propose, then wait</h3><p>Mercury suggests; the person decides. Make the yes easy and the default safe.</p></div>
      <div><h3>One point at a time</h3><p>Short sentences. One idea per line, one question per email.</p></div>
      <div><h3>No AI voice</h3><p>No hype words, no exclamation marks, no em-dashes, no all-caps no emoji.</p></div>
      <div style="margin-top:auto;background:var(--accent-soft);border:1px solid var(--accent-line);border-radius:var(--r-sm);padding:14px 16px;font-size:16px;font-weight:500">
        Read it out loud. If it sounds like AI wrote it, rewrite it.</div>
    </div>
    <div style="display:flex;flex-direction:column;gap:20px">
      <div class="panel panel-pad"><table style="font-size:15px"><tr><th>Write this</th><th>Not this</th></tr>{dt}</table>
      <p class="cap">Banned in product copy and in Mercury's email: unlock, supercharge, elevate, seamless, leverage, game-changer.</p></div>
      <div class="panel"><div class="panel-head"><h3>In the product</h3><p>The voice on the Today page: what is waiting, why, and one clear action.</p></div>
        <div style="display:flex;align-items:center;gap:20px;padding:16px 20px 18px">
          <div style="flex:1">{badge("waiting", "clock", "Needs you")}
            <div style="font-size:15px;font-weight:600;margin-top:5px">12 emails waiting for your approval</div>
            <div style="font-size:14px;color:var(--text-2);margin-top:2px">Opening emails to roofers in Denver and Boulder. Mercury sends them on a human-like schedule once you approve.</div></div>
          <span class="btn btn-secondary">Review outbox</span></div></div>
    </div>''',
  body_style="grid-template-columns:0.8fr 1.2fr;margin-top:24px;gap:20px"))

# 15 In the product ---------------------------------------------------------------
def shot(name, dark, k, crop=None):
    bg = "#14111E" if dark else "#F7F5FD"
    cw, ch = (crop if crop else (1440 * k, 1000 * k))
    return f'''<div style="width:{cw:.0f}px;height:{ch:.0f}px;overflow:hidden;border-radius:var(--r-md);border:1px solid var(--border);box-shadow:var(--shadow-sm)">
      <div class="{'dark' if dark else ''} zoomed" style="width:1440px;height:1000px;position:relative;zoom:{k};
        background:url('../../exports/{name}.png') 0 0 / 1440px 1000px no-repeat">
        <div style="position:absolute;left:14px;top:16px;width:236px;height:46px;background:{bg};display:flex;align-items:center">
          <div class="brand"><span class="mark">{tile(style="border-radius:9px")}</span>
          <div style="text-rendering:geometricPrecision"><h4>Mercury Agent</h4><div class="tagline">Outreach agent</div></div></div></div>
      </div></div>'''


P.append(page(15, "In the product", "The mark in the dashboard",
  "The new tile replaces the plain M in the sidebar, light and dark. Everything else on screen is unchanged.",
  f'''<div>{shot("today-light", False, 0.45)}<p class="cap">Today, light.</p></div>
    <div>{shot("today-dark", True, 0.45)}<p class="cap">Today, dark.</p></div>
    <div style="grid-column:1 / -1;display:flex;gap:20px;align-items:center">
      {shot("today-light", False, 1.0, crop=(246, 70))}{shot("today-dark", True, 1.0, crop=(246, 70))}
      <p class="small" style="font-size:14px;max-width:44ch">The sidebar at 1x: the 30 px tile with the reversed cut, "Mercury&nbsp;Agent" in Geist 600 and the tagline in text-3.</p></div>''',
  body_style="grid-template-columns:auto auto;justify-content:start;margin-top:22px;gap:14px 26px"))

# 16 Applications -----------------------------------------------------------------
def banner():
    im = Image.open(io.BytesIO(cairosvg.svg2png(bytestring=f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256"><path d="{SMALL}"/></svg>'.encode(),
                                                output_width=20, output_height=20))).getchannel("A")
    rows = []
    for y in range(0, 20, 2):
        line = ""
        for x in range(20):
            a, b = im.getpixel((x, y)) > 110, im.getpixel((x, y + 1)) > 110
            line += "█" if a and b else "▀" if a else "▄" if b else " "
        rows.append(line)
    return rows


br = banner()
text = ["$ mercury run", "", "Mercury Agent 1.0", "Mercury proposes; you confirm.", "",
        "Heartbeat every 15 min. Quiet 22:00 to 7:00.", "12 emails waiting for your yes: mercury outbox"]
term = ('<div style="display:flex;gap:28px;align-items:center"><div class="term" style="color:var(--accent);line-height:1;font-size:13px;font-family:&quot;DejaVu Sans Mono&quot;,Menlo,Consolas,monospace">'
        + "\n".join(br) + '</div><div class="term">' + "\n".join(t for t in text if t or True).strip("\n") + "</div></div>")
P.append(page(16, "Applications", "Where people meet it", "",
  f'''<div class="panel" style="display:flex;flex-direction:column">
      <div class="panel-head"><h3>README header, light</h3></div>
      <div style="padding:22px 24px;flex:1;display:flex;flex-direction:column;gap:12px">
        {lockup(fill="var(--text)", style="width:320px;height:auto")}
        <p class="muted" style="font-size:14px">A sales agent that runs on your own machine. It finds prospects, writes cold email and handles replies. Nothing goes out until you say yes.</p><div class="term" style="margin-top:auto;background:var(--panel-raised);border:1px solid var(--border);border-radius:var(--r-sm);padding:10px 12px;font-size:13px">pip install -e .\nmercury run</div></div></div>
    <div class="panel dark" style="display:flex;flex-direction:column">
      <div class="panel-head"><h3>README header, dark</h3></div>
      <div style="padding:22px 24px;flex:1;display:flex;flex-direction:column;gap:12px">
        {lockup(fill="var(--text)", rev=True, style="width:320px;height:auto")}
        <p class="muted" style="font-size:14px">A sales agent that runs on your own machine. It finds prospects, writes cold email and handles replies. Nothing goes out until you say yes.</p><div class="term" style="margin-top:auto;background:var(--panel-raised);border:1px solid var(--border);border-radius:var(--r-sm);padding:10px 12px;font-size:13px">pip install -e .\nmercury run</div></div></div>
    <div class="panel" style="display:flex;flex-direction:column">
      <div class="panel-head"><h3>Social avatar</h3><p>The full-bleed tile in a circle.</p></div>
      <div style="flex:1;display:flex;align-items:center;gap:18px;padding:0 24px">
        {tile(square=True, style="width:96px;height:96px;border-radius:50%")}
        <div><div style="font-weight:600">Mercury Agent</div><div class="small">@mercuryagent</div></div>
        {tile(square=True, style="width:32px;height:32px;border-radius:50%;margin-left:auto")}</div></div>
    <div class="panel dark" style="grid-column:1 / span 2;display:flex;flex-direction:column">
      <div class="panel-head"><h3>Terminal, mercury run</h3><p>Half-block banner drawn from the small cut.</p></div>
      <div style="padding:16px 24px;color:var(--text-2)">{term}</div></div>
    <div class="panel" style="display:flex;flex-direction:column;background:var(--wash),var(--bg)">
      <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 20px;border-bottom:1px solid var(--border)">
        {lockup(fill="var(--text)", style="width:118px;height:auto")}<span class="small">Docs&nbsp;&nbsp;&nbsp;GitHub</span></div>
      <div style="padding:20px;display:flex;gap:16px;align-items:center;flex:1">
        <div style="flex:1"><div style="font-size:23px;font-weight:600;letter-spacing:-0.03em;line-height:1.15">A messenger that never sleeps, and still waits for your yes.</div>
          <div style="display:flex;gap:8px;margin-top:14px"><span class="btn btn-primary">Install Mercury</span><span class="btn btn-secondary">How it works</span></div></div>
        {heartbeat(style="width:110px;height:110px;flex:none")}</div></div>''',
  body_style="grid-template-columns:1fr 1fr 1fr;grid-template-rows:1fr 1.15fr;margin-top:24px;gap:20px"))

# 17 Misuse -----------------------------------------------------------------------
def dsvg(d, viewbox="-110 -110 240 220", fill="var(--text)", style="width:150px;height:150px", extra=""):
    return f'<svg viewBox="{viewbox}" style="{style}" {extra}><path fill="{fill}" d="{d}"/></svg>'


def dont(art, text, bg="var(--panel)"):
    return f'''<div class="panel dont"><div class="stage" style="background:{bg}">{art}</div>
      <div class="panel-foot">{badge("bad", "x-circle", text)}</div></div>'''


badge_pose = frame((80 * math.cos(math.radians(45)), -80 * math.sin(math.radians(45))))
tight_gap = frame((80, 0), bite_r=48, dot_r=40)
D = [
    dont(sym(fill="var(--text)", style="width:150px;height:150px;transform:rotate(-90deg)"), "Don't rotate it. It turns into a person icon."),
    dont(sym(fill="var(--text)", style="width:150px;height:150px;transform:scaleX(-1)"), "Don't mirror it. The dot leads into the name."),
    dont(svgp(SYM, "0 0 256 256", "var(--text)", style="width:260px;height:120px", extra='preserveAspectRatio="none"'), "Don't stretch or squash it."),
    dont(sym(fill="#2F9460", style="width:150px;height:150px"), "Don't use status hues. Green means sent."),
    dont(sym(fill="var(--accent)", style="width:150px;height:150px;filter:drop-shadow(6px 8px 6px rgba(33,28,53,.45)) drop-shadow(0 0 14px #C7B6FF)"), "Don't add shadows, glows or effects."),
    dont(lockup(fill="var(--text)", style="width:300px"), "Don't place it on a busy picture.",
         bg="url('../../exports/today-light.png') 40% 30% / 900px auto"),
    dont(f'''<div style="display:flex;align-items:center;gap:14px">{sym(fill="var(--text)", style="width:64px;height:64px")}
        <span style="font-family:Georgia,'Times New Roman',serif;font-size:34px;color:var(--text)">Mercury Agent</span></div>''', "Don't set the name in another font."),
    dont(dsvg(badge_pose), "Don't move the dot. At 1:30 it reads as a badge."),
    dont(dsvg(tight_gap), "Don't change the gap or the dot size."),
]
P.append(page(17, "Misuse", "Please don't", "", "".join(D),
  body_style="grid-template-columns:repeat(3,1fr);grid-template-rows:repeat(3,1fr);margin-top:24px;gap:16px"))

# 18 Files ------------------------------------------------------------------------
TREE_A = """design/brand/
  final/                      masters, edit these
    mercury-symbol.svg          32 px and up
    mercury-symbol-small.svg    16 to 24 px
    mercury-symbol-reversed.svg white on dark
    mercury-dot.svg             the dot alone
    mercury-lockup-horizontal.svg
    mercury-lockup-stacked.svg
    mercury-wordmark.svg
    mercury-tile.svg            app icon
    mercury-tile-small.svg      app icon, 16 to 32 px
    mercury-heartbeat.svg       agent running
    heartbeat.svg               storyboard
    construction.svg
    NOTES.md
  GUIDELINES.md               the rules, short
  book/                       this book, PDF, pages"""
TREE_B = """  kit/                        exports, never edit
    README.md                   which file, when
    symbol/     black, white, mono-6d53d3
                SVG + 64 to 2048 px PNG
                small cut: 16, 24, 32, 48 px
    lockup/     horizontal, stacked
                black, white, mono-6d53d3
                SVG + 600, 1200, 2400 px
    wordmark/   black, white, mono-6d53d3
    app-icon/   tile 1024 to 32 px
                tile-small 32, 24, 16 px
                square (iOS), maskable (Android)
    web/        favicon.svg, favicon.ico,
                apple-touch-icon, icon-192/512,
                site.webmanifest, head-snippet
    motion/     mercury-heartbeat.svg + GIFs,
                storyboard, mercury-dot.svg"""
P.append(page(18, "Files", "Where everything lives",
  "Masters live in final/. Everything in kit/ is exported from them by tools/kit_export.sh, so fix a master and export again.",
  f'''<div class="panel panel-pad"><div class="term" style="font-size:14px;line-height:1.75">{TREE_A}</div></div>
    <div class="panel panel-pad"><div class="term" style="font-size:14px;line-height:1.75">{TREE_B}</div></div>
    <div class="panel panel-pad" style="display:flex;flex-direction:column;gap:18px">
      <div><h3>Naming</h3><p class="mono" style="font-size:13px;margin-top:6px;color:var(--text-2)">mercury-&lt;version&gt;[-&lt;cut&gt;]-&lt;colour&gt;[-&lt;px&gt;]</p>
        <p class="small" style="margin-top:6px">mercury-lockup-horizontal-white-1200.png<br>mercury-symbol-small-mono-6d53d3-24.png</p></div>
      <div><h3>Which file to send</h3><p class="muted" style="font-size:14px;margin-top:4px">SVG to developers and printers. PNG for slides,
        documents and chat. Never send a screenshot of the logo.</p></div>
      <div><h3>Questions</h3><p class="muted" style="font-size:14px;margin-top:4px">Carlos, EBSY. Master files: design/brand/final.</p></div>
      <div style="margin-top:auto">{lockup(fill="var(--text)", style="width:200px;height:auto")}</div>
    </div>''', body_style="grid-template-columns:1.05fr 1fr 0.9fr;margin-top:24px;gap:20px"))

assert len(P) == NPAGES, len(P)
html = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Mercury Agent brand book v1.0</title>
<meta name="viewport" content="width=1600">
<link rel="icon" href="../kit/web/favicon.svg" type="image/svg+xml">
<style>{CSS}</style></head>
<body>
{"".join(P)}
</body></html>
'''
(BOOK / "index.html").write_text(html)
print("wrote book/index.html", len(html) // 1024, "KB")
