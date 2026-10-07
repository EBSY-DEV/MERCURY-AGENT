"""What Mercury prints when ``mercury run`` starts: the mark, drawn in the
terminal with quadrant blocks, and the name beside it.

The mark is rasterised from the symbol's own geometry (the small cut in
design/brand/final/mercury-symbol-small.svg) rather than drawn by hand, so it
stays the same shape as the logo. Colour is the dark-mode accent (#B7A4F7) and
is left out when NO_COLOR is set. Nothing is printed when output is not a
terminal, so a log file or a service journal keeps only its own lines. On a
narrow or non-UTF-8 terminal it falls back to the plain dot and the name.
"""

import os
import shutil
import sys
from importlib import metadata
from pathlib import Path

ACCENT = "\x1b[38;2;183;164;247m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
RESET = "\x1b[0m"

TAGLINE = "Outreach agent"

# Small cut of the symbol, in its 256-unit design space: (centre x, centre y, radius).
_DISC = (117.99, 128.0, 112.0)
_BITE = (209.91, 128.0, 64.0)  # cut out of the disc
_DOT = (209.91, 128.0, 40.0)   # the dot crossing the right edge

MARK_ROWS = 6
_TEXT_ROWS = (2, 3, 4)  # which mark rows carry the name, tagline and folder
_GAP = "  "
_MIN_COLUMNS = 46


def _inside(circle, x: float, y: float) -> bool:
    cx, cy, r = circle
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _lit(x: float, y: float) -> bool:
    return (_inside(_DISC, x, y) and not _inside(_BITE, x, y)) or _inside(_DOT, x, y)


# Quadrant glyphs by which quarters of the cell are filled, as a 4-bit mask:
# upper-left = 1, upper-right = 2, lower-left = 4, lower-right = 8.
_QUADRANTS = " ▘▝▀▖▌▞▛▗▚▐▜▄▙▟█"


def mark(rows: int = MARK_ROWS, samples: int = 8) -> list[str]:
    """The symbol as ``rows`` lines of quadrant-block characters.

    A terminal cell is about twice as tall as it is wide, so a round disc is
    ``2 * rows`` columns across. Each cell is cut into four quarters and gets
    the glyph that fills the quarters the shape covers, which gives twice the
    horizontal detail of half-blocks. The grid is sized so the disc's centre
    falls on a cell edge, which keeps the disc symmetric.
    """
    row_h = 2 * _DISC[2] / rows   # design units per terminal row
    col_w = row_h / 2             # ...and per column
    left, top = _DISC[0] - _DISC[2], _DISC[1] - _DISC[2]
    width = round((_DOT[0] + _DOT[2] - left) / col_w)

    def filled(col: int, row: int, qx: int, qy: int) -> bool:
        hits = sum(
            _lit(left + (col + (qx + (a + 0.5) / samples) / 2) * col_w,
                 top + (row + (qy + (b + 0.5) / samples) / 2) * row_h)
            for a in range(samples) for b in range(samples)
        )
        return hits * 2 >= samples * samples

    lines = []
    for r in range(rows):
        line = ""
        for c in range(width):
            bits = (filled(c, r, 0, 0) | filled(c, r, 1, 0) << 1
                    | filled(c, r, 0, 1) << 2 | filled(c, r, 1, 1) << 3)
            line += _QUADRANTS[bits]
        lines.append(line.rstrip())
    return lines


def _version() -> str:
    try:
        return metadata.version("mercury-agent")
    except metadata.PackageNotFoundError:
        return ""


def _folder() -> str:
    cwd = Path.cwd()
    try:
        return "~/" + cwd.relative_to(Path.home()).as_posix() if cwd != Path.home() else "~"
    except ValueError:
        return str(cwd)


def _wants_colour(stream) -> bool:
    return not os.environ.get("NO_COLOR") and stream.isatty()


def _can_draw(stream) -> bool:
    encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
    columns = shutil.get_terminal_size((80, 24)).columns
    return encoding == "utf8" and columns >= _MIN_COLUMNS


def render(colour: bool = False, version: str | None = None,
           folder: str | None = None, draw_mark: bool = True) -> str:
    """The banner text, with or without ANSI colour or the drawn mark."""
    version = _version() if version is None else version
    folder = _folder() if folder is None else folder
    name = "Mercury Agent"
    tagline = f"{TAGLINE} · v{version}" if version else TAGLINE

    def paint(text: str, *codes: str) -> str:
        return f"{''.join(codes)}{text}{RESET}" if colour and text else text

    if not draw_mark:
        return (f"\n  {paint('●', ACCENT)}  {paint(name, BOLD)}\n"
                f"     {paint(tagline, DIM)}\n")

    art = mark()
    width = max(len(line) for line in art)
    texts = dict(zip(_TEXT_ROWS, (paint(name, BOLD), paint(tagline, DIM), paint(folder, DIM))))
    rows = []
    for i, line in enumerate(art):
        pad = " " * (width - len(line))
        rows.append(f"  {paint(line, ACCENT)}{pad}{_GAP}{texts.get(i, '')}".rstrip())
    return "\n" + "\n".join(rows) + "\n"


def print_banner(stream=None) -> None:
    """Print the banner to a terminal; do nothing when output is piped."""
    stream = stream or sys.stdout
    if not hasattr(stream, "isatty") or not stream.isatty():
        return
    print(render(colour=_wants_colour(stream), draw_mark=_can_draw(stream)), file=stream)
