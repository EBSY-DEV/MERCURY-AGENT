"""The line Mercury prints when ``mercury run`` starts.

Brand rule (design/brand/GUIDELINES.md): in a terminal the mark is the plain
dot beside the name. A half-block drawing of the symbol reads as Pac-Man at
that size, so none is used. Colour is the dark-mode accent (#B7A4F7), and it is
left out when the output is not a terminal or NO_COLOR is set.
"""

import os
import sys
from importlib import metadata

ACCENT = "\x1b[38;2;183;164;247m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
RESET = "\x1b[0m"

TAGLINE = "Outreach agent"


def _version() -> str:
    try:
        return metadata.version("mercury-agent")
    except metadata.PackageNotFoundError:
        return ""


def _wants_colour(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return hasattr(stream, "isatty") and stream.isatty()


def render(colour: bool = False, version: str | None = None) -> str:
    """The banner text, with or without ANSI colour."""
    version = _version() if version is None else version
    sub = f"{TAGLINE} · v{version}" if version else TAGLINE
    dot, name, sub_line = "●", "Mercury Agent", sub
    if colour:
        dot = f"{ACCENT}{dot}{RESET}"
        name = f"{BOLD}{name}{RESET}"
        sub_line = f"{DIM}{sub}{RESET}"
    return f"\n  {dot}  {name}\n     {sub_line}\n"


def print_banner(stream=None) -> None:
    """Print the banner to a terminal. Does nothing when output is piped, so
    a log file or a service journal keeps only its own lines."""
    stream = stream or sys.stdout
    if not hasattr(stream, "isatty") or not stream.isatty():
        return
    print(render(colour=_wants_colour(stream)), file=stream)
