import io

import pytest

from mercury import banner


class _Tty(io.StringIO):
    encoding = "utf-8"

    def isatty(self):
        return True


def test_mark_is_a_disc_with_the_dot_on_its_right_edge():
    art = banner.mark()
    assert len(art) == banner.MARK_ROWS
    assert set("".join(art)) <= set(banner._QUADRANTS)
    # The disc starts on the left and the dot is the last thing on the right.
    middle = art[banner.MARK_ROWS // 2]
    assert middle.startswith("█") and middle.rstrip()[-1] != " "
    assert " " in middle.strip()  # the gap between the disc and the dot


def test_mark_is_symmetric_top_to_bottom():
    art = banner.mark()
    flip = {"▘": "▖", "▖": "▘", "▝": "▗", "▗": "▝", "▀": "▄", "▄": "▀", "▛": "▙",
            "▙": "▛", "▜": "▟", "▟": "▜", "▚": "▞", "▞": "▚", "▌": "▌", "▐": "▐", "█": "█", " ": " "}
    width = max(len(line) for line in art)
    padded = [line.ljust(width) for line in art]
    mirrored = ["".join(flip[c] for c in line) for line in reversed(padded)]
    assert padded == mirrored


def test_render_puts_the_name_beside_the_mark():
    out = banner.render(colour=False, version="1.2.3", folder="~/work")
    assert "Mercury Agent" in out
    assert "Outreach agent · v1.2.3" in out
    assert "~/work" in out
    assert "█" in out and "\x1b" not in out


def test_render_without_the_mark_is_the_plain_dot():
    out = banner.render(colour=False, version="1.2.3", draw_mark=False)
    assert "●  Mercury Agent" in out and "█" not in out


def test_render_without_a_version_drops_it():
    assert "· v" not in banner.render(colour=False, version="", folder="~")


def test_colour_uses_the_dark_mode_accent():
    assert "38;2;183;164;247" in banner.render(colour=True, version="1", folder="~")


def test_nothing_is_printed_when_output_is_piped():
    buf = io.StringIO()
    banner.print_banner(buf)
    assert buf.getvalue() == ""


def test_terminal_gets_colour_unless_no_color_is_set(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    tty = _Tty()
    banner.print_banner(tty)
    assert "\x1b[" in tty.getvalue()

    monkeypatch.setenv("NO_COLOR", "1")
    plain = _Tty()
    banner.print_banner(plain)
    assert "Mercury Agent" in plain.getvalue() and "\x1b" not in plain.getvalue()


@pytest.mark.parametrize("encoding", ["ascii", "cp1252", "utf-8"])
def test_banner_can_be_written_to_the_actual_terminal_encoding(encoding, monkeypatch):
    class EncodedTty(io.TextIOWrapper):
        def isatty(self):
            return True

    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(banner, "_version", lambda: "1.2.3")
    monkeypatch.setattr(banner, "_folder", lambda: "~/José/水星")
    buffer = io.BytesIO()
    tty = EncodedTty(buffer, encoding=encoding)
    banner.print_banner(tty)
    tty.flush()
    output = buffer.getvalue().decode(encoding)
    assert "Mercury Agent" in output and "v1.2.3" in output
    if encoding != "utf-8":
        assert ".  Mercury Agent" in output and "Outreach agent - v1.2.3" in output


def test_narrow_utf8_terminal_keeps_the_plain_dot(monkeypatch):
    monkeypatch.setenv("COLUMNS", "30")
    tty = _Tty()
    assert not banner._can_draw(tty)
    monkeypatch.setenv("NO_COLOR", "1")
    banner.print_banner(tty)
    assert "●  Mercury Agent" in tty.getvalue()
