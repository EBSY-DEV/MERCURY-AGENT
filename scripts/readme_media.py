"""Record the README's screenshots and clips from the seeded demo.

Seeds a throwaway demo (scripts/seed_demo.py) into a temp directory, serves
the dashboard on a spare port, then drives it with Playwright:

* stills: 1440x900 at 2x, light and dark, into docs/images/ (``today.png``,
  ``today-dark.png``, ...);
* clips: Chrome's screencast at 2x, encoded by ffmpeg into looping GIFs in
  docs/media/ (``outbox.gif``, ``outbox-dark.gif``, ...). A drawn cursor, a
  click ring and a key badge are overlaid so the viewer can follow along.

Every clip starts from a fresh copy of the demo database, so approving a
draft in one never shows up in the next.

Usage (from the repo root):

    env -u APPIMAGE .venv/bin/python scripts/readme_media.py               # everything
    env -u APPIMAGE .venv/bin/python scripts/readme_media.py outbox today  # just these

Needs ffmpeg on PATH and Playwright's Chromium (python -m playwright install
chromium). Nothing is sent: the demo mailboxes point at smtp.invalid, and no
clip presses Run on a paid source.
"""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.async_api import Page, async_playwright

ROOT = Path(__file__).resolve().parent.parent
IMAGES = ROOT / "docs" / "images"
MEDIA = ROOT / "docs" / "media"
THEMES = ("light", "dark")

STILL_VIEW = {"width": 1440, "height": 900}
CLIP_VIEW = {"width": 1280, "height": 800}
GIF_WIDTH = 1280
GIF_FPS = 12

# Recording aids, drawn in the page: they use the dashboard's own tokens, so
# they follow the theme. Not part of the product UI.
OVERLAY = """
(() => {
  const css = `
    #rm-cursor { position: fixed; left: 0; top: 0; z-index: 2147483647; pointer-events: none;
      transform: translate(-200px, -200px); filter: drop-shadow(0 1px 2px rgba(0,0,0,.35)); }
    .rm-ring { position: fixed; width: 30px; height: 30px; margin: -15px 0 0 -15px; z-index: 2147483646;
      border-radius: 50%; border: 2px solid var(--accent); pointer-events: none;
      animation: rm-ring .5s ease-out forwards; }
    @keyframes rm-ring { from { transform: scale(.3); opacity: 1; } to { transform: scale(1.5); opacity: 0; } }
    #rm-key { position: fixed; left: 50%; bottom: 32px; z-index: 2147483647; pointer-events: none;
      display: flex; gap: 10px; align-items: center; padding: 10px 16px 10px 10px;
      border-radius: var(--r-md); background: var(--text); color: var(--panel);
      font: 500 15px/1 var(--sans); opacity: 0; transform: translate(-50%, 8px);
      transition: opacity .15s, transform .15s; box-shadow: 0 8px 24px rgba(0,0,0,.18); }
    #rm-key.on { opacity: 1; transform: translate(-50%, 0); }
    #rm-key kbd { font: 600 15px/1 var(--mono); padding: 6px 9px; border-radius: 6px;
      background: var(--panel); color: var(--text); }
  `;
  const arrow = '<svg width="22" height="26" viewBox="0 0 22 26"><path d="M2 2 L2 20 L7 15.5 L10.5 23.5 ' +
    'L13.8 22 L10.4 14.2 L17 14.2 Z" fill="#111" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
  const mount = () => {
    if (document.getElementById('rm-cursor')) return;
    const style = document.createElement('style');
    style.textContent = css;
    document.head.appendChild(style);
    const cursor = document.createElement('div');
    cursor.id = 'rm-cursor';
    cursor.innerHTML = arrow;
    const key = document.createElement('div');
    key.id = 'rm-key';
    document.body.append(cursor, key);
    if (window.__rmPos) cursor.style.transform = `translate(${window.__rmPos[0] - 2}px, ${window.__rmPos[1] - 2}px)`;
  };
  document.addEventListener('mousemove', e => {
    window.__rmPos = [e.clientX, e.clientY];
    const c = document.getElementById('rm-cursor');
    if (c) c.style.transform = `translate(${e.clientX - 2}px, ${e.clientY - 2}px)`;
  }, true);
  document.addEventListener('mousedown', e => {
    const ring = document.createElement('div');
    ring.className = 'rm-ring';
    ring.style.left = e.clientX + 'px';
    ring.style.top = e.clientY + 'px';
    document.body.appendChild(ring);
    setTimeout(() => ring.remove(), 600);
  }, true);
  window.__rmKey = (k, label) => {
    const el = document.getElementById('rm-key');
    el.innerHTML = '<kbd>' + k + '</kbd>' + label;
    el.classList.add('on');
    clearTimeout(window.__rmKeyTimer);
    window.__rmKeyTimer = setTimeout(() => el.classList.remove('on'), 1100);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
  else mount();
})();
"""


# ── Demo server ──────────────────────────────────────────────────────

class Demo:
    """A seeded database, a pristine copy of it, and a dashboard serving it."""

    def __init__(self, workdir: Path):
        self.db = workdir / "demo.db"
        self.pristine = workdir / "pristine.db"
        self.config = workdir / "demo.mercury.yaml"
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}/"
        self.proc: subprocess.Popen | None = None

    def start(self):
        env = {k: v for k, v in os.environ.items() if k != "APPIMAGE"}
        subprocess.run([sys.executable, str(ROOT / "scripts" / "seed_demo.py"), str(self.db)],
                       check=True, cwd=ROOT, env=env, stdout=subprocess.DEVNULL)
        shutil.copy(self.db, self.pristine)
        env.update(MERCURY_DB_PATH=str(self.db), MERCURY_CONFIG=str(self.config),
                   MAILBOX_DEMO_PASSWORD="demo")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "mercury.cli", "dashboard", "--port", str(self.port)],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(80):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.25).close()
                return
            except OSError:
                time.sleep(0.25)
        raise SystemExit("The demo dashboard did not start.")

    def reset(self):
        """Put the database back the way the seed left it."""
        for suffix in ("-wal", "-shm"):
            Path(str(self.db) + suffix).unlink(missing_ok=True)
        shutil.copy(self.pristine, self.db)

    def stop(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=10)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── Driving the page ─────────────────────────────────────────────────

class Hand:
    """Moves the visible cursor the way a person would: eased, never teleporting."""

    def __init__(self, page: Page, x: float, y: float):
        self.page, self.x, self.y = page, x, y

    async def park(self):
        await self.page.mouse.move(self.x, self.y)

    async def glide(self, x: float, y: float, ms: int = 650):
        steps = max(2, ms // 16)
        x0, y0 = self.x, self.y
        for i in range(1, steps + 1):
            t = i / steps
            e = t * t * (3 - 2 * t)
            await self.page.mouse.move(x0 + (x - x0) * e, y0 + (y - y0) * e)
            await self.page.wait_for_timeout(16)
        self.x, self.y = x, y

    async def to(self, selector: str, dx: float = 0.5, dy: float = 0.5, ms: int = 650):
        box = await self.page.locator(selector).first.bounding_box()
        await self.glide(box["x"] + box["width"] * dx, box["y"] + box["height"] * dy, ms)

    async def click(self, selector: str = "", dx: float = 0.5, dy: float = 0.5, ms: int = 650):
        if selector:
            await self.to(selector, dx, dy, ms)
        await self.page.mouse.down()
        await self.page.wait_for_timeout(70)
        await self.page.mouse.up()

    async def key(self, key: str, label: str):
        await self.page.evaluate("([k, l]) => window.__rmKey(k, l)", [key.upper(), label])
        await self.page.wait_for_timeout(180)
        await self.page.keyboard.press(key)

    async def type(self, text: str, delay: int = 55):
        await self.page.keyboard.type(text, delay=delay)


async def scroll_to(page: Page, selector: str, offset: int = 80, ms: int = 900):
    await page.evaluate(
        """([sel, off]) => {
             const el = document.querySelector(sel);
             const y = el.getBoundingClientRect().top + window.scrollY - off;
             window.scrollTo({top: y, behavior: 'smooth'});
           }""", [selector, offset])
    await page.wait_for_timeout(ms)


# A clicked nav button keeps its focus ring; a person's click wouldn't linger.
BLUR = "document.activeElement && document.activeElement.blur()"


async def open_tab(page: Page, tab: str, settle: int = 1200):
    await page.click(f'.side-nav [data-tab="{tab}"]')
    await page.evaluate(BLUR)
    await page.wait_for_timeout(settle)


# ── Screencast → GIF ─────────────────────────────────────────────────

class Screencast:
    def __init__(self, page: Page, folder: Path, view: dict):
        self.page, self.folder, self.view = page, folder, view
        self.frames: list[tuple[float, Path]] = []

    async def start(self):
        self.cdp = await self.page.context.new_cdp_session(self.page)
        self.cdp.on("Page.screencastFrame", self._frame)
        await self.cdp.send("Page.startScreencast", {
            "format": "jpeg", "quality": 92, "everyNthFrame": 1,
            "maxWidth": self.view["width"] * 2, "maxHeight": self.view["height"] * 2,
        })

    def _frame(self, params):
        path = self.folder / f"{len(self.frames):05d}.jpg"
        path.write_bytes(base64.b64decode(params["data"]))
        self.frames.append((params["metadata"]["timestamp"], path))
        asyncio.ensure_future(
            self.cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]}))

    async def stop(self, hold: float = 1.5):
        await self.cdp.send("Page.stopScreencast")
        await self.cdp.detach()
        self.hold = hold

    def encode(self, out: Path):
        if not self.frames:
            raise SystemExit(f"No frames captured for {out.name}.")
        lines = []
        for (ts, path), nxt in zip(self.frames, self.frames[1:] + [None]):
            duration = (nxt[0] - ts) if nxt else self.hold
            lines += [f"file '{path.name}'", f"duration {max(duration, 0.001):.4f}"]
        lines.append(f"file '{self.frames[-1][1].name}'")
        listing = self.folder / "frames.txt"
        listing.write_text("\n".join(lines) + "\n")
        graph = (f"fps={GIF_FPS},scale={GIF_WIDTH}:-1:flags=lanczos,split[a][b];"
                 "[a]palettegen=max_colors=256:stats_mode=diff[p];"
                 "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                        "-i", str(listing), "-vf", graph, "-loop", "0", str(out)], check=True)


# ── Clips ────────────────────────────────────────────────────────────

async def clip_tour(page: Page, hand: Hand):
    """Today, then the board, the calendar and the inboxes."""
    await page.wait_for_timeout(1000)
    await hand.glide(900, 560, 800)                        # across the trend chart
    await hand.glide(1150, 600, 600)
    await page.wait_for_timeout(300)
    for tab, hold in (("pipeline", 1500), ("calendar", 1500), ("mailboxes", 1700)):
        await hand.click(f'.side-nav [data-tab="{tab}"]', ms=550)
        await page.evaluate(BLUR)
        await page.wait_for_timeout(hold)
    await hand.click('.side-nav [data-tab="today"]', ms=600)
    await page.evaluate(BLUR)
    await page.wait_for_timeout(900)


async def clip_outbox(page: Page, hand: Hand):
    """Move through the queue, fix a word in a draft, approve it."""
    await page.wait_for_timeout(900)
    await hand.glide(760, 420, 700)                        # reading the draft
    await page.wait_for_timeout(700)
    await hand.key("j", "next draft")
    await page.wait_for_timeout(1200)
    await hand.key("k", "previous")
    await page.wait_for_timeout(1000)
    x, y = await page.evaluate(WORD_AT, ["#desk-body", "morning"])
    await hand.glide(x, y, 650)
    await page.mouse.dblclick(x, y)
    await page.wait_for_timeout(350)
    await hand.type("week", delay=90)
    await page.wait_for_timeout(600)
    await hand.click('.desk-pane button:has-text("Approve")', dx=0.35, ms=700)
    await page.wait_for_timeout(2200)


# Where a word sits in a textarea, for a double-click that selects it. Assumes
# the word is on the first visual line of its paragraph.
WORD_AT = """([sel, word]) => {
  const ta = document.querySelector(sel), cs = getComputedStyle(ta);
  const ctx = document.createElement('canvas').getContext('2d');
  ctx.font = `${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
  const lines = ta.value.split('\\n');
  const i = lines.findIndex(l => l.includes(word));
  const before = lines[i].slice(0, lines[i].indexOf(word));
  const r = ta.getBoundingClientRect(), lh = parseFloat(cs.lineHeight);
  return [r.left + parseFloat(cs.borderLeftWidth) + parseFloat(cs.paddingLeft)
            + ctx.measureText(before).width + ctx.measureText(word).width / 2,
          r.top + parseFloat(cs.borderTopWidth) + parseFloat(cs.paddingTop)
            + lh * (i + 0.5) - ta.scrollTop];
}"""


async def clip_cohort(page: Page, hand: Hand):
    """Narrow twelve companies to a target list with three clicks."""
    await page.wait_for_timeout(500)
    await scroll_to(page, "#cohort-builder", offset=110, ms=1100)
    pick = "#cohort-builder label.cohort-pick:has(input[onchange*=\"'{key}','{code}'\"])"
    for key, code in (("require", "RUNNING_GOOGLE_ADS"), ("require", "NO_ONLINE_BOOKING"),
                      ("exclude", "INCUMBENT_AGENCY")):
        await hand.click(pick.format(key=key, code=code), dx=0.12, ms=700)
        await page.wait_for_timeout(1400)
    await scroll_to(page, "#cohort-result", offset=330, ms=1000)  # the list itself
    await hand.glide(1180, 470, 600)
    await page.wait_for_timeout(1800)


async def clip_discover(page: Page, hand: Hand):
    """Pick a paid source, name the cities, and see the price before anything runs."""
    await page.wait_for_timeout(700)
    await hand.click('.prov-card:has-text("DataForSEO Business Listings")', dx=0.3, dy=0.2)
    await page.wait_for_timeout(700)
    await scroll_to(page, "#disc-cities", offset=360, ms=1000)
    await hand.click("#disc-cities", dx=0.2, ms=550)
    await hand.type("Denver, CO; Boulder, CO; Lakewood, CO")
    await page.wait_for_timeout(400)
    await hand.click('button:has-text("Estimate cost")', ms=550)
    await page.wait_for_timeout(900)
    await scroll_to(page, "#discover-estimate", offset=300, ms=900)
    await hand.to("#disc-run", dx=0.95, dy=0.9, ms=700)   # now it says what it will spend
    await page.wait_for_timeout(1800)


CLIPS = {
    "tour": ("today", clip_tour),
    "outbox": ("outbox", clip_outbox),
    "cohort": ("signals", clip_cohort),
    "discover": ("discover", clip_discover),
}


# ── Stills ───────────────────────────────────────────────────────────

async def still_heatmap(page: Page, out: Path):
    panel = page.locator("#today-heatmap")
    await panel.scroll_into_view_if_needed()
    await page.wait_for_timeout(300)
    await panel.screenshot(path=str(out))


STILLS = {
    "today": "today", "pipeline": "pipeline", "calendar": "calendar",
    "outbox": "outbox", "mailboxes": "mailboxes", "heatmap": "today",
}


def _name(base: str, theme: str, ext: str) -> str:
    return f"{base}{'' if theme == 'light' else '-dark'}.{ext}"


async def record(demo: Demo, wanted: set[str]):
    IMAGES.mkdir(parents=True, exist_ok=True)
    MEDIA.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        for theme in THEMES:
            for name, tab in STILLS.items():
                if wanted and f"still:{name}" not in wanted and name not in wanted:
                    continue
                demo.reset()
                ctx = await browser.new_context(viewport=STILL_VIEW, device_scale_factor=2,
                                                color_scheme=theme)
                page = await ctx.new_page()
                await page.goto(demo.url)
                await page.wait_for_timeout(1200)
                await open_tab(page, tab, settle=1500)
                out = IMAGES / _name(name, theme, "png")
                if name == "heatmap":
                    await still_heatmap(page, out)
                else:
                    await page.screenshot(path=str(out))
                await ctx.close()
                print(f"  {out.relative_to(ROOT)}")

            for name, (tab, play) in CLIPS.items():
                if wanted and f"clip:{name}" not in wanted and name not in wanted:
                    continue
                demo.reset()
                ctx = await browser.new_context(viewport=CLIP_VIEW, device_scale_factor=2,
                                                color_scheme=theme)
                await ctx.add_init_script(OVERLAY)
                page = await ctx.new_page()
                await page.goto(demo.url)
                await page.wait_for_timeout(1000)
                await open_tab(page, tab)
                hand = Hand(page, CLIP_VIEW["width"] * 0.62, CLIP_VIEW["height"] * 0.58)
                await hand.park()
                await page.wait_for_timeout(300)
                with tempfile.TemporaryDirectory() as frames:
                    cast = Screencast(page, Path(frames), CLIP_VIEW)
                    await cast.start()
                    await play(page, hand)
                    await cast.stop()
                    out = MEDIA / _name(name, theme, "gif")
                    cast.encode(out)
                await ctx.close()
                print(f"  {out.relative_to(ROOT)}  {out.stat().st_size / 1e6:.1f} MB")
        await browser.close()


def main(argv: list[str]) -> int:
    if not shutil.which("ffmpeg"):
        print("ffmpeg is not on PATH.", file=sys.stderr)
        return 2
    wanted = set(argv[1:])
    with tempfile.TemporaryDirectory() as workdir:
        demo = Demo(Path(workdir))
        demo.start()
        try:
            asyncio.run(record(demo, wanted))
        finally:
            demo.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
