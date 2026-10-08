# Mercury Agent logo kit

Every file here is exported from the masters in `../final/` by `../tools/kit_export.sh`. Edit the
masters, never these. Rules (clear space, minimum sizes, colours) are in `../GUIDELINES.md` and the
brand book (`../book/index.html`).

Quick picks:
- **Screens, light background:** `lockup/mercury-lockup-horizontal-black.svg` (or the `mono-6d53d3` one).
- **Screens, dark background:** `lockup/mercury-lockup-horizontal-white.svg`.
- **Small square spot (avatar, sidebar):** `app-icon/mercury-tile.svg`.
- **Browser tab:** everything in `web/`, wired with `web/head-snippet.html`.
- **Printers:** send the SVG. The PNGs are for screens and office documents.

## symbol/

| File | Use it for |
|---|---|
| mercury-symbol-black.svg + -64/-256/-512/-1024/-2048.png | The symbol on light backgrounds, 32 px and up. |
| mercury-symbol-white.svg + PNGs | On ink, #14111E or photos. Drawn from the reversed cut (smaller dot, wider gap), so it doesn't look heavier than the black one. |
| mercury-symbol-mono-6d53d3.svg + PNGs | Accent violet on white or lavender. |
| mercury-symbol-small-{black,white,mono-6d53d3}.svg + -16/-24/-32/-48.png | The small cut. Use at 24 px and below. |

## lockup/

| File | Use it for |
|---|---|
| mercury-lockup-horizontal-{black,white,mono-6d53d3}.svg + -600/-1200/-2400.png | The primary logo: README header, site header, slides. Minimum 160 px wide. |
| mercury-lockup-stacked-{black,white,mono-6d53d3}.svg + PNGs | Square-ish spaces: social cards, title slides, stickers. Minimum 120 px wide. |

The white lockups use the reversed symbol cut.

## wordmark/

| File | Use it for |
|---|---|
| mercury-wordmark-{black,white,mono-6d53d3}.svg + PNGs | Only where the symbol already appears nearby (for example the tile is in the same view) or the space can't hold a symbol. Minimum 120 px wide. |

## app-icon/

| File | Use it for |
|---|---|
| mercury-tile.svg, mercury-tile-{1024,512,192,180,64,48,32}.png | The app icon: violet gradient tile, white symbol, rounded 29 %. Use 32 px and up. |
| mercury-tile-small.svg, mercury-tile-small-{32,24,16}.png | The same tile with the small cut, larger in the tile. Use at 16-32 px. |
| mercury-tile-square.svg, -1024.png, -180.png | Full-bleed square for platforms that round the corners themselves (iOS, app stores). |
| mercury-tile-maskable.svg, -512.png | Android / PWA maskable icon: symbol inside the 80 % safe zone. |

## web/

| File | Use it for |
|---|---|
| favicon.svg | Modern browsers. The small-cut symbol in #6D53D3, switches to #B7A4F7 when the browser is in dark mode. |
| favicon.ico (16, 32, 48) and favicon-16/32/48.png | Fallback. The small tile, so it reads on light and dark tab bars. |
| apple-touch-icon.png (180) | iOS home screen (full-bleed gradient). |
| icon-192.png, icon-512.png, maskable-512.png | PWA icons referenced by site.webmanifest. |
| site.webmanifest | Name "Mercury Agent", short name "Mercury", lavender theme colour. |
| head-snippet.html | Paste into `<head>`. Includes light and dark `theme-color`. |

## motion/

| File | Use it for |
|---|---|
| mercury-heartbeat.svg | The "agent running" animation. 2.4 s loop: wake, act, ready, sent. Inline it to inherit the dashboard's `--accent`, or use it as an `<img>` (follows the system theme). Stops in the logo pose under reduced motion. |
| mercury-heartbeat-light.gif, mercury-heartbeat-dark.gif | The same loop for places that can't run SVG animation (README, chat, slides). 256 px, 25 fps. |
| heartbeat.svg, heartbeat-storyboard.png | The five-frame storyboard, for docs and the book. |
| mercury-dot.svg | The dot alone, accent violet. Agent presence next to the agent's name. It is not a status pill: status still uses an icon plus a word. |

## Naming

`mercury-<version>[-<cut>]-<colour>[-<size>].<ext>`: version = symbol, lockup-horizontal,
lockup-stacked, wordmark, tile; cut = small, square, maskable; colour = black, white, mono-6d53d3;
size = pixel width.
