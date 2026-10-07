"""What Mercury prints when ``mercury run`` starts: the mark, drawn in the
terminal with half-blocks, and the name beside it.

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


def mark(rows: int = MARK_ROWS, samples: int = 8) -> list[str]:
    """The symbol as ``rows`` lines of half-block characters.

    Each character cell is two square pixels tall. The grid is sized so the
    disc is exactly ``2 * rows`` pixels across and its centre falls on a pixel
    edge, which keeps the disc symmetric.
    """
    size = 2 * _DISC[2] / (2 * rows)
    left, top = _DISC[0] - _DISC[2], _DISC[1] - _DISC[2]
    width = round((_DOT[0] + _DOT[2] - left) / size)

    def pixel(col: int, row: int) -> bool:
        hits = sum(
            _lit(left + (col + (a + 0.5) / samples) * size,
                 top + (row + (b + 0.5) / samples) * size)
            for a in range(samples) for b in range(samples)
        )
        return hits * 2 >= samples * samples

    lines = []
    for r in range(rows):
        line = ""
        for c in range(width):
            upper, lower = pixel(c, 2 * r), pixel(c, 2 * r + 1)
            line += "█" if upper and lower else "▀" if upper else "▄" if lower else " "
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
