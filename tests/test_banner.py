import io

from mercury import banner


class _Tty(io.StringIO):
    def isatty(self):
        return True


def test_render_plain_is_the_dot_and_the_name():
    out = banner.render(colour=False, version="1.2.3")
    assert "●  Mercury Agent" in out
    assert "Outreach agent · v1.2.3" in out
    assert "\x1b" not in out


def test_render_without_a_version_drops_it():
    assert "· v" not in banner.render(colour=False, version="")


def test_colour_uses_the_dark_mode_accent():
    assert "38;2;183;164;247" in banner.render(colour=True, version="1")


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
