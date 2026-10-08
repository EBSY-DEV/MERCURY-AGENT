# Mercury Agent, logo guidelines (compact)

The full version with pictures is the brand book: `book/index.html` (PDF: `book/mercury-agent-brand-book.pdf`).
Masters: `final/`. Ready-to-use files: `kit/` (see `kit/README.md`).

## 1. The logo
- **Idea:** a dot crossing the edge of a disc along the horizontal: the transit of Mercury. The dot is half
  out at three o'clock. The work is ready, and it waits for your yes. ("Mercury proposes; you confirm.")
- **Name:** "Mercury Agent", two words, title case. "Mercury" alone in tight UI. "by EBSY" is an optional
  endorsement and never part of the logo.
- **Versions:** horizontal lockup (primary), stacked lockup, symbol, wordmark. Small cut of the symbol for 24 px
  and below. App tile (violet gradient) for icons. The dot alone for agent presence.
- **Files:** SVG for screens, developers and printers. PNG for slides, documents and chat.

## 2. Clear space
Unit **x = the dot's radius**. Keep **2x (one dot) clear on every side** of the symbol and of every lockup.
It scales with the logo. In the horizontal lockup, the space between the symbol and the name is also 2x.
Inside a tile (favicon, app icon) the tile is the clear space. The dashboard sidebar uses the bare small cut in the accent colour, with no tile.

## 3. Minimum size
| Version | Screen | Print |
|---|---|---|
| Horizontal lockup | 160 px wide | 35 mm wide |
| Stacked lockup | 120 px wide | 25 mm wide |
| Wordmark | 120 px wide | 25 mm wide |
| Symbol, master | 32 px | 8 mm |
| Symbol, small cut (`-small`) | 16-24 px | 5 mm |
| App tile | 32 px; `tile-small` at 16-32 px | n/a |

## 4. Colour
| Name | HEX | RGB | CMYK (approx.) | Use |
|---|---|---|---|---|
| Accent | #6D53D3 | 109 83 211 | 48 61 0 17 | Logo on light, primary actions |
| Accent deep | #5639BD | 86 57 189 | 54 70 0 26 | Accent as text |
| Ink | #211C35 | 33 28 53 | 38 47 0 79 | Type, one-colour logo |
| Lavender mist | #F7F5FD | 247 245 253 | 2 3 0 1 | Page background |
| Lavender | #ECE6FD | 236 230 253 | 7 9 0 1 | Logo background, selection |
| Dark | #14111E | 20 17 30 | 33 43 0 88 | Dark-mode background |
| Accent on dark | #B7A4F7 | 183 164 247 | 26 34 0 3 | Logo and accent in dark mode |
| Tile gradient | #C7B6FF > #9C84F0 > #7A5EDB, 220 deg | | | App icon only |

CMYK is a straight conversion: proof before print. No Pantone match has been chosen yet.

**Approved pairs:** ink on white · white on ink · accent on lavender · white on the tile gradient · white on
#14111E · #B7A4F7 on #14111E. Every white or light-on-dark version uses the **reversed cut**
(`*-white` files), which is drawn slightly lighter so it doesn't look heavier.

**Status hues** (green, amber, red, blue, pink) mean status in the product. They never appear in the logo,
the dot or the animation.

## 5. Typography
- Geist 400 / 500 / 600 for everything; Geist Mono (tabular) for every figure. No serif. Both are SIL Open
  Font License, in `mercury/web/fonts/`.
- Wordmark: Geist 600, tracking -20 (-2 %), outlined. Never type it; use the files.
- Endorsement: "by EBSY" in Geist 500, text-2 colour, cap height about 40 % of the wordmark's, set directly
  under the name and left-aligned with the "M". It is never baked into the logo files.

## 6. The dot and the heartbeat
- The dot alone (`mercury-dot.svg`, accent) = Mercury is here. It sits next to the agent's name. It is not
  a status pill: status is still a Phosphor icon plus a word (`.badge`).
- `mercury-heartbeat.svg` = agent running. 2.4 s loop: wake (0-0.29 s), act (to 1.39 s, ease
  cubic-bezier(.16,1,.3,1)), ready (hold to 2.11 s), sent (to 2.40 s). Stops in the logo pose under reduced
  motion. Asleep (quiet hours) = closed disc, no dot. Proposal waiting = the still logo pose.

## 7. Don'ts
Don't rotate it (turned 90 deg it becomes a person icon) · don't mirror it · don't stretch or squash it ·
don't recolour it in status hues or outside the palette · don't add shadows, glows, outlines or gradients
(except the approved tile) · don't place it on a busy picture without the tile · don't set the name in another
font or retype it · don't move the dot, change the gap or resize the dot · don't rearrange the lockups.

## 8. Contact
Questions: Carlos, EBSY. Master files: `design/brand/final/`. Rebuild the kit with `tools/kit_export.sh`
and the book with `tools/book_build.py` then `tools/book_render.py`.
