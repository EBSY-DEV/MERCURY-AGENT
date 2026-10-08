"""Render book/index.html to book/mercury-agent-brand-book.pdf and book/pages/NN.png with Chromium."""
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

BOOK = Path(__file__).resolve().parent.parent / "book"


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1600, "height": 900}, device_scale_factor=2)
        await pg.goto((BOOK / "index.html").as_uri())
        await pg.evaluate("document.fonts.ready")
        await pg.wait_for_load_state("networkidle")
        # hold every heartbeat in the logo pose (the 'ready' phase) for static output
        await pg.evaluate("document.getAnimations().forEach(a => { a.pause(); a.currentTime = 1700; })")
        # overflow check: any element whose box spills out of its page
        spill = await pg.evaluate("""() => {
          const out = [];
          document.querySelectorAll('.page').forEach(p => {
            const pr = p.getBoundingClientRect();
            p.querySelectorAll('*').forEach(el => {
              const r = el.getBoundingClientRect();
              if (r.width && (r.right > pr.right + 0.5 || r.bottom > pr.bottom + 0.5))
                out.push(p.id + ' ' + el.tagName + '.' + (el.className.baseVal ?? el.className) + ' ' + Math.round(r.right - pr.right) + ',' + Math.round(r.bottom - pr.bottom));
            });
          });
          return out.slice(0, 40);
        }""")
        print("spill:", spill or "none")
        small = await pg.evaluate("""() => {
          const out = new Set();
          document.querySelectorAll('.page *').forEach(el => {
            if (el.closest('svg') || el.closest('.zoomed')) return;
            const t = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
            if (!t) return;
            const fs = parseFloat(getComputedStyle(el).fontSize);
            if (fs < 12) out.add(el.closest('.page').id + ' ' + fs + 'px ' + el.textContent.trim().slice(0, 30));
          });
          return [...out].slice(0, 40);
        }""")
        print("text under 12px:", small or "none")
        (BOOK / "pages").mkdir(exist_ok=True)
        pages = await pg.query_selector_all(".page")
        for i, el in enumerate(pages, 1):
            await el.screenshot(path=str(BOOK / "pages" / f"{i:02d}.png"))
        await pg.emulate_media(media="print")
        await pg.pdf(path=str(BOOK / "mercury-agent-brand-book.pdf"), width="1600px", height="900px",
                     print_background=True, margin={"top": "0", "right": "0", "bottom": "0", "left": "0"})
        await b.close()
    print("rendered", len(pages), "pages")

asyncio.run(main())
