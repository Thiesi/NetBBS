"""What a SAUCE record drives (issue #929, step 3): iCE colours, the width
fallback, and the art editor writing SAUCE on save."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.net.session import Session, write_preformatted_line
from netbbs.net.welcome_banner import load_welcome_banner
from netbbs.net.main_menu_banner import load_main_menu_banner
from netbbs.rendering import decode_banner_bytes_fitting, ice_to_bright_background
from netbbs.rendering.charset import CP437, UTF8
from netbbs.rendering.sauce import build_sauce, split_sauce
from netbbs.storage.database import Database

ESC = "\x1b"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


class _Session(Session):
    write = Session.write

    def __init__(self, charset: str) -> None:
        self.output_charset = charset
        self.terminal_width = 80
        self.terminal_wraps_immediately = False
        self.sent: list[str] = []

    async def _send_text(self, text: str) -> None:
        self.sent.append(text)

    async def read_line(self, *args, **kwargs) -> str:
        raise AssertionError("unused")

    async def read_key(self, *args, **kwargs) -> str:
        raise AssertionError("unused")

    async def read_editor_key(self):
        raise AssertionError("unused")

    async def close(self) -> None:
        pass


def _sent(charset: str, text: str) -> str:
    session = _Session(charset)
    asyncio.run(write_preformatted_line(session, text))
    return "".join(session.sent)


# -- iCE colours ---------------------------------------------------------


def test_blink_with_a_background_becomes_a_bright_background():
    assert ice_to_bright_background(f"{ESC}[0;5;44mX") == f"{ESC}[0;104mX"


def test_blink_set_before_the_background_still_converts():
    assert ice_to_bright_background(f"{ESC}[5m{ESC}[41mX") == f"{ESC}[5m{ESC}[101;25mX"


def test_blink_without_a_background_stays_blink():
    assert ice_to_bright_background(f"{ESC}[5mX") == f"{ESC}[5mX"


def test_blink_off_restores_the_normal_background():
    text = ice_to_bright_background(f"{ESC}[5;42mA{ESC}[25mB")
    assert text == f"{ESC}[102mA{ESC}[42mB"


def test_a_reset_clears_the_ice_state():
    text = ice_to_bright_background(f"{ESC}[5;43mA{ESC}[0mB{ESC}[44mC")
    assert text == f"{ESC}[103mA{ESC}[0mB{ESC}[44mC"


def test_extended_colours_pass_through():
    text = ice_to_bright_background(f"{ESC}[38;5;214;48;2;1;2;3mX")
    assert text == f"{ESC}[38;5;214;48;2;1;2;3mX"


def test_other_sequences_are_untouched():
    text = f"{ESC}[2J{ESC}[1;1H{ESC}[1;33mHi"
    assert ice_to_bright_background(text) == text


def test_a_utf8_terminal_gets_bright_backgrounds_not_blink():
    out = _sent(UTF8, f"{ESC}[0;5;44mNIB{ESC}[0m")
    assert f"{ESC}[0;104m" in out
    assert ";5;" not in out and "[5m" not in out


def test_a_cp437_terminal_gets_ctermss_bright_background_mode_around_the_art():
    out = _sent(CP437, f"{ESC}[0;5;44mNIB{ESC}[0m")
    assert out.startswith(f"{ESC}[?33h")
    assert f"{ESC}[?33l" in out
    assert f"{ESC}[0;104m" in out


def test_art_without_bright_backgrounds_gets_no_mode_switch():
    out = _sent(CP437, f"{ESC}[0;44mNIB{ESC}[0m")
    assert "?33" not in out


# -- width fallback ------------------------------------------------------


ART = f"{ESC}[0m██ wide art\r\n".encode("cp437")


def test_art_wider_than_the_terminal_falls_back():
    data = ART + build_sauce(width=132, lines=1)
    assert decode_banner_bytes_fitting(data, 80) is None
    assert decode_banner_bytes_fitting(data, 132) is not None
    assert decode_banner_bytes_fitting(data, None) is not None


def test_art_without_a_width_is_always_shown():
    assert decode_banner_bytes_fitting(ART, 40) is not None


def _enable(db, kind: str, data: bytes) -> None:
    from netbbs.net import main_menu_banner, welcome_banner

    if kind == "welcome":
        welcome_banner.banner_path(db).write_bytes(data)
        welcome_banner.set_welcome_banner_enabled(db, True)
    else:
        main_menu_banner.main_menu_banner_path(db).write_bytes(data)
        main_menu_banner.set_main_menu_banner_enabled(db, True)


def test_a_too_wide_welcome_banner_falls_back_to_the_default(db):
    _enable(db, "welcome", ART + build_sauce(width=132, lines=1))
    narrow = load_welcome_banner(db, max_width=80)
    wide = load_welcome_banner(db, max_width=132)
    assert "wide art" not in narrow
    assert "wide art" in wide


def test_a_too_wide_masthead_shows_nothing(db):
    _enable(db, "main", ART + build_sauce(width=132, lines=1))
    assert load_main_menu_banner(db, max_width=80) == ""
    assert "wide art" in load_main_menu_banner(db, max_width=132)


# -- the editor writes SAUCE ----------------------------------------------


def test_the_editor_saves_a_sauce_record_keeping_the_loaded_credits():
    from netbbs.net.ansi_editor import _saved_bytes
    from netbbs.rendering import ScreenBuffer
    from netbbs.rendering.sauce import Sauce

    buffer = ScreenBuffer(80, 24)
    loaded = Sauce(
        title="Nib Logo", author="InkWell", group="Quill", date="20200101", file_size=0,
        data_type=1, file_type=1, tinfo1=80, tinfo2=24, tinfo3=0, tinfo4=0, tflags=0,
        font="IBM VGA", comments=(),
    )
    body, sauce = split_sauce(_saved_bytes(buffer, loaded))
    assert sauce is not None
    assert (sauce.width, sauce.tinfo2, sauce.font) == (80, 24, "IBM VGA")
    assert (sauce.title, sauce.author, sauce.group) == ("Nib Logo", "InkWell", "Quill")
    assert not sauce.ice_colors
    assert sauce.file_size == len(body)
    _, fresh = split_sauce(_saved_bytes(buffer, None))
    assert fresh is not None and fresh.credit == ""
