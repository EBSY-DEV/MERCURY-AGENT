# Mercury Agent, final mark (Transit)

**Idea:** a dot crossing the edge of a disc along the horizontal: the transit of Mercury. In the logo
pose the dot is half out at the 3:00 limb. A proposal is ready, and it waits for your yes. The dot on
its own is the agent's presence.

Built by `tools/kit_build.py` (masters) and `tools/kit_system.py` (construction, storyboard, animation).
Exports: `tools/kit_export.sh` (runs the skill's export_variants.py, then `tools/kit_post.py`).

## Files

| File | What it is |
|---|---|
| mercury-symbol.svg | Master symbol, 256 canvas, black. Use from 32 px up. |
| mercury-symbol-small.svg | Small cut for 16-24 px (tight canvas, disc rim on whole pixels at 16 px). |
| mercury-symbol-reversed.svg | Cut for white on dark: dot 2 % smaller, gap 7 % wider, same outer silhouette. |
| mercury-dot.svg | The dot alone, accent #6D53D3. Agent presence, not a status pill. |
| mercury-lockup-horizontal.svg | Symbol + "Mercury Agent" (1403 x 256). |
| mercury-lockup-stacked.svg | Symbol over the wordmark (648 x 390). |
| mercury-wordmark.svg | "Mercury Agent" alone. |
| mercury-tile.svg | App icon: 220 deg tile gradient, rx 74 (29 %), reversed cut in white at 68 % width. |
| mercury-tile-small.svg | Tile with the small cut at 78 % width, for 16-32 px. |
| construction.svg | Construction drawing for the book (contains live text: illustration, not a logo file). |
| heartbeat.svg | Five-frame storyboard of the heartbeat. |
| mercury-heartbeat.svg | The animated mark (CSS keyframes, 2.4 s loop) for the "agent running" state. |

Every logo file is one `<path>`, filled only, `<title>Mercury Agent</title>`, no strokes, masks or
filters (the animation is the one file that uses a mask, because the bite has to move).

## Construction

Design units, disc radius R = 100. Grid unit **u = 20**.

| Part | Size | In u |
|---|---|---|
| gap g (dot to disc) | 20 | 1u |
| dot radius r | 40 | 2u |
| bite radius B = r + g | 60 | 3u |
| disc centre to dot centre d | 80 | 4u |
| disc radius R | 100 | 5u |

So g : r : B : d : R = 1 : 2 : 3 : 4 : 5, and d² + B² = R² (the 3-4-5 triangle). Because of that the
bite meets the rim exactly on the dot's vertical diameter, so both horns sit straight above and below
the dot centre. The dot sticks out 20 (1u) past the rim. Silhouette 220 x 200.

On the 256 canvas: scale 216/220. Disc centre (122.18, 128), R 98.18. Dot centre (200.73, 128),
r 39.27. Gap 19.64. Horns (200.73, 69.09) and (200.73, 186.91). Margins L24 R16 T30.3 B30.3: the mark
is shifted 4 units right of bbox centre, because the disc carries the ink mass (centroid x 116).
6 anchors, all arcs. Mirror-symmetric about the horizontal.

**Correction to the round-3 notes:** they said the bite meets the rim at 90 degrees. It doesn't.
d² + B² = R² puts the right angle at the dot centre, not at the horn. The real angle between the
two circles is acos(0.6) = 53.13 degrees, so each horn is a 53 degree wedge. I checked it at 2048 px.
It is clean, with no sliver and no notch, and it reads as a firm point, not a needle. I left it
sharp. A 1-2 unit round would only show above ~1000 px and would add 4 anchors.

## Round 5: renamed to Mercury Agent (October 2026)

The product name changed from the old order (Agent first) to **Mercury Agent**. The symbol, the small and
reversed cuts, the tiles and the motion files are unchanged. Only the type was reset: same Geist 600,
tracking -20, cap 112 (horizontal) and 60 (stacked), cap midline on the dot's centre line.

- The round-4 notes below talk about "Agent" as the first word and a "C Agent" reading. That was the old
  name; it is kept as history.
- **Gap check with "M" first** (rendered at 180, 48 and 28 px tall; gaps 64, 78.55, 88, 96). The M's flat
  stem faces the dot, where the A used to slope away from it, so the same 78.55 gap now measures true at
  the dot's centre line. At 28-48 px gap 64 starts to pull the disc into the word, but at 78.55 the symbol
  stays a separate mark and does not read as "C Mercury" or "CM". 88 and 96 were no better at small sizes
  and look loose at 180 px. **Decision: keep the gap at 2r = 78.55, one dot diameter.** The rule doesn't
  change.
- Horizontal lockup is now 1403 x 256 (was 1414; the new word order sets 11 units narrower). Stacked is
  648 x 390: symbol silhouette centred plus the +4 optical shift, name centred on the canvas, margins 40.
- The "by EBSY" endorsement now left-aligns with the "M".
- Audit: horizontal 90, stacked 91, wordmark 90 (only Geist's own "y" diagonal flagged, as before).

## Round 4 refinements (light, no redesign)

1. **Optical check at 2048 px.** Horns are clean. The ring gap stays even all the way round the dot.
   The dot's outer edge is the rightmost point and sits on the 16-unit margin. Nothing changed in
   the symbol geometry: the master path is identical to round 3.
2. **Pac-Man / "C" study in the lockup** (concepts/transit/round4/lockup-study.png). I tried six
   versions, each rendered at 180, 48 and 28 px tall:
   gap 64 / 78.5 / 96, "Agent" at 600 or 500, and cap 112 or 104.
   - At 28-48 px, gap 64 reads as a letter: "C Agent". The symbol sits in the word's rhythm.
   - **Gap 78.5 = 2r, one dot diameter**, breaks that. The symbol reads as a separate mark and the
     line doesn't look loose at large sizes. Gap 96 looks detached at large sizes.
   - "Agent" at 500 doesn't help with the C. It looks thin next to the solid disc and turns grey at
     small sizes. Kept all 600.
   - Cap 104 makes the symbol more dominant, but the wordmark gets weak at large sizes. Kept 112.
   - **Change: lockup gap 64 to 78.55 (= 2r).** This is clearly better at sidebar and README sizes,
     and it gives the lockup a rule anyone can repeat: the space between symbol and name is one dot.
3. **Stacked lockup** rebuilt to the same rules: Geist 600 at cap 60 (0.54 x the horizontal cap),
   symbol to cap-top gap = r, margins 40.
4. **Tile-small**: tried the small cut at 72, 78 and 84 % of the tile width. 84 % crowds the
   rounded corners at 16 px. 72 % loses the dot. **78 %** keeps a visible violet gap and dot at 16 px.

## Clear space

**Unit x = the dot radius r** (39.27 on the 256 symbol canvas, 0.2 of the symbol height).
**Keep 2x (one dot diameter) clear on every side** of the symbol and of every lockup. It scales
with the logo. In the horizontal lockup the symbol-to-name gap is also 2x. In tight UI (sidebar,
favicon) the container replaces clear space: the tile's own padding is the clear space.

## Minimum sizes

| Version | Screen | Print |
|---|---|---|
| Symbol, master | 32 px | 8 mm |
| Symbol, small cut | 16 px (use 16-24 px) | 5 mm |
| Horizontal lockup | 160 px wide (symbol about 22 px tall) | 35 mm wide |
| Stacked lockup | 120 px wide (name cap height 11 px) | 25 mm wide |
| Wordmark alone | 120 px wide (cap height 12 px) | 25 mm wide |
| App tile | 16 px (tile-small below 32 px) | n/a |

**Switch point:** use the small cut (and tile-small) at 24 px and below. Use the master at 32 px and
above. Between 24 and 32 px either works; prefer the master.

## Colour

Logo colours: ink #211C35 (or black), white, accent #6D53D3 (on light), accent #B7A4F7 (on dark
#14111E), white on the tile gradient #C7B6FF > #9C84F0 > #7A5EDB (220 deg). The status hues (green,
amber, red, blue, pink) never appear in the mark, the dot or the animation.

## Motion: the heartbeat (mercury-heartbeat.svg)

Loop 2.4 s, CSS keyframes inside the SVG. The disc stays put; the dot and its bite move together on
the horizontal axis, so it reads as a body crossing a disc, not a knob in a track.

| Phase | Time | % | What happens | Easing |
|---|---|---|---|---|
| Wake | 0-0.29 s | 0-12 | Dot and bite grow from 0 at d = 24 (inside the disc) | cubic-bezier(.33,1,.68,1) |
| Act | 0.29-1.39 s | 12-58 | Slide d 24 to 80 and settle at the limb | cubic-bezier(.16,1,.3,1) (the dashboard's own ease) |
| Ready | 1.39-2.11 s | 58-88 | Hold in the logo pose: waiting for your yes | none |
| Sent | 2.11-2.40 s | 88-100 | Dot and bite shrink to 0 while drifting 10 units out; the disc heals | cubic-bezier(.5,0,.75,0) |

- Colour: `fill: var(--accent, #6D53D3)` so inline SVG follows the dashboard theme. As an `<img>` it
  uses #6D53D3 on light and #B7A4F7 on dark (prefers-color-scheme).
- `prefers-reduced-motion: reduce` stops it in the logo pose.
- States: **asleep** (quiet hours) = closed disc, no dot. **Running** = the loop. **Waiting for
  approval** = the static logo pose. The dot is agent presence. Status words still use the Phosphor
  `.badge` per CLAUDE.md, and the dot never replaces them.
- Verified by seeking the animations in Chromium at 11 time points, light and dark.

## Tests run (round 4)

- Audit (`svg_audit.py`): symbol 99 (intentional L24/R16), small 100, reversed 99, dot 100,
  horizontal lockup 90, stacked 91, wordmark 90 (each: Geist's native "y" diagonal flagged as a
  near-miss angle; type, not drawing; extra-wide notes), tiles 91 (expected gradient), heartbeat 97
  (style block).
- Ladders at true 16/24/32/48 px on white, #14111E and #6D53D3 (symbol, small cut, four tile cuts).
- Animation seeked in Chromium (11 time points, light and dark) and exported as GIFs.
- Favicons checked at 1x and 8x on light (#F1F3F4) and dark (#35363A) browser tab colours.

## Final artwork checklist (process.md §7)

- [x] One path per logo file, no stray anchors (symbol: 6 anchors, all arcs; type: glyph overlaps unioned with skia-pathops).
- [x] Angles: only Geist's own "y" diagonal is flagged.
- [x] Type outlined; no live text, rasters, filters or strokes in logo files (construction.svg and heartbeat.svg are illustrations and keep live labels).
- [x] viewBoxes tight with consistent padding; symbol optically centred (+4).
- [x] Exact brand colours; black, white (reversed cut), mono #6D53D3, tiles.
- [x] File set matches the brief; working files kept in concepts/transit/round4.

## Open items

- Trademark search and reverse image search before launch (not done here; I can't give clearance).
- "Transit" isn't obvious from the static mark alone. The heartbeat and the status dot carry the story.
- Pac-Man can still be a first reading for someone who sees the symbol alone, cold. The bigger lockup
  gap fixes the "C" reading, not that one.
- Never rotate it: turned 90 degrees it becomes a person icon.
- The terminal half-block banner (book page 16) reads strongly as Pac-Man at that resolution. Consider the
  plain `●` dot plus the name for terminal use instead.
