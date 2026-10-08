"""Second half of the kit export (run by kit_export.sh): reversed white lockups, app-icon PNGs from the
gradient tiles, the web icon overrides (gradient tiles instead of a flat square), and the motion files."""
import asyncio
import json
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FINAL, KIT = ROOT / "final", ROOT / "kit"
sys.path.insert(0, os.environ.get("LOGO_SKILL_SCRIPTS", os.path.expanduser("~/.claude/skills/logo-design/scripts")))
import render_png  # noqa: E402

sys.dont_write_bytecode = True
MASTER_M = (200.73, 69.09)
REV_D = ("M201.53 68.82A98.63 98.63 0 1 0 201.53 187.18A59.18 59.18 0 0 1 201.53 68.82Z"
         "M163.07 128A38.47 38.47 0 1 1 240 128A38.47 38.47 0 1 1 163.07 128Z")


def f(v):
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def shift_d(d, tx, ty):
    out = []
    for cmd, args in re.findall(r"([MAZ])([^MAZ]*)", d):
        n = [float(v) for v in re.findall(r"-?[\d.]+", args)]
        if cmd == "M":
            out.append(f"M{f(n[0]+tx)} {f(n[1]+ty)}")
        elif cmd == "A":
            for i in range(0, len(n), 7):
                a = n[i:i + 7]
                out.append(f"A{f(a[0])} {f(a[1])} {f(a[2])} {int(a[3])} {int(a[4])} {f(a[5]+tx)} {f(a[6]+ty)}")
        else:
            out.append("Z")
    return "".join(out)


def png(src, dst, w, h=None):
    h = h or w
    if not render_png.render(str(src), str(dst), w, h):
        raise SystemExit(f"render failed: {dst}")


def size_of(svg_text):
    w, h = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg_text).groups()
    return float(w), float(h)


# ---- white lockups, with the reversed (irradiation) cut -------------------
for name in ("horizontal", "stacked"):
    src = (FINAL / f"mercury-lockup-{name}.svg").read_text()
    d = re.search(r' d="([^"]+)"', src).group(1)
    # the symbol is the first two contours (disc + dot, arcs only); the rest is the wordmark
    m = re.match(r"(M[^M]*Z)(M[^M]*Z)", d)
    sym = m.group(0)
    x0, y0 = [float(v) for v in re.match(r"M(-?[\d.]+) (-?[\d.]+)", sym).groups()]
    tx, ty = x0 - MASTER_M[0], y0 - MASTER_M[1]
    out = src.replace(d, shift_d(REV_D, tx, ty) + d[len(sym):]).replace('fill="#000"', 'fill="#FFFFFF"')
    dst = KIT / "lockup" / f"mercury-lockup-{name}-white.svg"
    dst.write_text(out)
    w, h = size_of(out)
    for W in (600, 1200, 2400):
        png(dst, dst.with_name(f"{dst.stem}-{W}.png"), W, round(W * h / w))

# ---- app icons -------------------------------------------------------------
app = KIT / "app-icon"
for n in ("mercury-tile.svg", "mercury-tile-small.svg"):
    shutil.copy(FINAL / n, app / n)
square = (FINAL / "mercury-tile.svg").read_text().replace('rx="74"', 'rx="0"')
(app / "mercury-tile-square.svg").write_text(square)       # full bleed: iOS and Android mask it themselves
# maskable: the mark inside the 80 % safe circle
mask = re.sub(r'(<path fill="#FFFFFF" )', r'<g transform="translate(25.6 25.6) scale(0.8)">\1', square)
mask = mask.replace("/>\n</svg>", "/></g>\n</svg>")
(app / "mercury-tile-maskable.svg").write_text(mask)
for s in (1024, 512, 192, 180, 64, 48, 32):
    png(app / "mercury-tile.svg", app / f"mercury-tile-{s}.png", s)
for s in (32, 24, 16):
    png(app / "mercury-tile-small.svg", app / f"mercury-tile-small-{s}.png", s)
for s in (1024, 180):
    png(app / "mercury-tile-square.svg", app / f"mercury-tile-square-{s}.png", s)
png(app / "mercury-tile-maskable.svg", app / "mercury-tile-maskable-512.png", 512)

# ---- web: replace the flat icons from export_variants with the brand tiles -------
web = KIT / "web"
png(app / "mercury-tile-square.svg", web / "apple-touch-icon.png", 180)
png(app / "mercury-tile.svg", web / "icon-192.png", 192)
png(app / "mercury-tile.svg", web / "icon-512.png", 512)
png(app / "mercury-tile-maskable.svg", web / "maskable-512.png", 512)
for s in (16, 32, 48):
    png(app / "mercury-tile-small.svg", web / f"favicon-{s}.png", s)
render_png.write_ico([str(web / f"favicon-{s}.png") for s in (16, 32, 48)], str(web / "favicon.ico"))
# favicon.svg: the small-cut mark in the accent, lighter accent on dark browser chrome
small_d = re.search(r' d="([^"]+)"', (FINAL / "mercury-symbol-small.svg").read_text()).group(1)
(web / "favicon.svg").write_text(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="256" height="256">'
    "<title>Mercury Agent</title><style>path{fill:#6D53D3}"
    "@media (prefers-color-scheme: dark){path{fill:#B7A4F7}}</style>"
    f'<path d="{small_d}"/></svg>\n')
for stale in web.glob("mercury-symbol-favicon*.svg"):
    stale.unlink()
(web / "site.webmanifest").write_text(json.dumps({
    "name": "Mercury Agent", "short_name": "Mercury",
    "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
              {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
              {"src": "/maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}],
    "theme_color": "#F7F5FD", "background_color": "#F7F5FD", "display": "standalone"}, indent=2) + "\n")
(web / "head-snippet.html").write_text(
    '<link rel="icon" href="/favicon.ico" sizes="48x48">\n'
    '<link rel="icon" href="/favicon.svg" type="image/svg+xml">\n'
    '<link rel="apple-touch-icon" href="/apple-touch-icon.png">\n'
    '<link rel="manifest" href="/site.webmanifest">\n'
    '<meta name="theme-color" content="#F7F5FD" media="(prefers-color-scheme: light)">\n'
    '<meta name="theme-color" content="#14111E" media="(prefers-color-scheme: dark)">\n')

# ---- motion -------------------------------------------------------------------
mo = KIT / "motion"
for n in ("mercury-heartbeat.svg", "heartbeat.svg", "mercury-dot.svg"):
    shutil.copy(FINAL / n, mo / n)
png(mo / "heartbeat.svg", mo / "heartbeat-storyboard.png", 1600, 330)


async def gifs():
    from playwright.async_api import async_playwright
    from PIL import Image
    svg = (mo / "mercury-heartbeat.svg").read_text()
    async with async_playwright() as p:
        b = await p.chromium.launch()
        for scheme, bg in (("light", "#FFFFFF"), ("dark", "#14111E")):
            pg = await b.new_page(viewport={"width": 256, "height": 256}, color_scheme=scheme)
            await pg.set_content(f'<body style="margin:0;background:{bg}">{svg}</body>')
            frames = []
            for i in range(60):                       # 25 fps x 2.4 s
                await pg.evaluate(f"document.getAnimations().forEach(a=>{{a.pause();a.currentTime={i*40}}})")
                tmp = mo / "_f.png"
                await pg.screenshot(path=str(tmp))
                frames.append(Image.open(tmp).convert("RGB").quantize(64))
            tmp.unlink()
            frames[0].save(mo / f"mercury-heartbeat-{scheme}.gif", save_all=True, append_images=frames[1:],
                           duration=40, loop=0, optimize=True)
            await pg.close()
        await b.close()

asyncio.run(gifs())
print("post-export done")
