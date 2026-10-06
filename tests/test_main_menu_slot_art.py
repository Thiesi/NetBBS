"""The main menu drawn as the SysOp's art with live slots (issue #929,
step 4), and every case where a caller gets the generated menu instead."""

from __future__ import annotations

import asyncio

from netbbs.auth.users import create_user
from netbbs.chat.mailbox import MessageMailbox
from netbbs.net.main_menu import _draw_main_menu
from netbbs.net.main_menu_banner import (
    MASTHEAD_MODE,
    SLOTS_MODE,
    load_main_menu_banner,
    main_menu_art_mode,
    main_menu_banner_path,
    set_main_menu_art_mode,
    set_main_menu_banner_enabled,
)
from netbbs.rendering.ansi_parse import parse_ansi_into_buffer
from netbbs.rendering.screen_buffer import ScreenBuffer
from netbbs.storage.database import Database

ART = "\r\n".join([
    "*" * 60,
    "* The Nib & Quill            Hello, {user 14}          *",
    "* {menu 56x6}                                              *",
    "*",
    "*",
    "*",
    "*",
    "*",
    "* {mail 20}" + " " * 15 + "{node 16}" + " " * 5 + "*",
    "*" * 60,
    "Your choice: {prompt}",
]).encode("ascii")


class FakeSession:
    def __init__(self, *, width: int = 80, height: int = 24, charset: str = "utf-8"):
        self.written: list[str] = []
        self.terminal_width = width
        self.physical_width = width
        self.terminal_height = height
        self.output_charset = charset
        self.node_display_name = "The Nib & Quill"
        self.node_name_gradient = None
        self.peer_address = "203.0.113.5"
        self.supports_truecolor = False

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\r\n")


def _setup(tmp_path, art: bytes = ART):
    db = Database(tmp_path / "node.db")
    user = create_user(db, "OldNib", password="parker51", user_level=10)
    main_menu_banner_path(db).write_bytes(art)
    set_main_menu_banner_enabled(db, True)
    set_main_menu_art_mode(db, SLOTS_MODE)
    return db, user


def _draw(db, user, session, **kwargs) -> str:
    asyncio.run(_draw_main_menu(session, db, MessageMailbox(), user, **kwargs))
    return "".join(session.written)


def _screen(text: str, width: int = 80, height: int = 24) -> list[str]:
    buffer = ScreenBuffer(width, height)
    parse_ansi_into_buffer(text, buffer)
    return ["".join(buffer.get_cell(r, c).char for c in range(width)).rstrip() for r in range(height)]


def test_the_mode_defaults_to_masthead(tmp_path):
    db = Database(tmp_path / "node.db")
    assert main_menu_art_mode(db) == MASTHEAD_MODE
    db.close()


def test_slot_art_replaces_the_generated_menu(tmp_path):
    db, user = _setup(tmp_path)
    text = _draw(db, user, FakeSession())
    screen = _screen(text)
    assert "Main menu" not in text
    assert screen[1].startswith("* The Nib & Quill            Hello, OldNib")
    menu = "\n".join(screen[2:8])
    for key in ("[M]essage boards", "[C]hat", "[F]iles", "[?] Help", "[P]rofile", "[L]ogoff"):
        assert key in menu
    assert "mail caught up" in screen[8]
    assert "The Nib & Quill" in screen[8]
    db.close()


def test_the_prompt_goes_to_its_slot(tmp_path):
    db, user = _setup(tmp_path)
    text = _draw(db, user, FakeSession())
    # The last cursor move before the prompt is to the {prompt} cell.
    assert text.rstrip().endswith("Choice:")
    assert "\x1b[11;14H" in text
    db.close()


def test_the_art_frame_survives_around_the_slots(tmp_path):
    db, user = _setup(tmp_path)
    screen = _screen(_draw(db, user, FakeSession()))
    assert screen[0] == "*" * 60
    assert screen[9] == "*" * 60
    assert screen[2].endswith("*")
    db.close()


def test_masthead_mode_art_is_not_shown_as_a_masthead_in_slots_mode(tmp_path):
    db, _user = _setup(tmp_path)
    assert load_main_menu_banner(db) == ""
    db.close()


def test_items_that_do_not_fit_fall_back_to_the_generated_menu(tmp_path):
    db, user = _setup(tmp_path, b"{menu 20x2}\r\n{prompt}")
    text = _draw(db, user, FakeSession())
    assert "Main menu" in text
    assert "{menu" not in text
    db.close()


def test_art_with_problems_falls_back(tmp_path):
    db, user = _setup(tmp_path, b"no menu slot here {user 10}")
    assert "Main menu" in _draw(db, user, FakeSession())
    db.close()


def test_an_ascii_caller_gets_the_generated_menu(tmp_path):
    db, user = _setup(tmp_path)
    assert "Main menu" in _draw(db, user, FakeSession(charset="ascii"))
    db.close()


def test_a_terminal_narrower_than_the_art_gets_the_generated_menu(tmp_path):
    db, user = _setup(tmp_path)
    assert "Main menu" in _draw(db, user, FakeSession(width=50))
    db.close()


def test_a_terminal_too_short_for_the_art_gets_the_generated_menu(tmp_path):
    db, user = _setup(tmp_path)
    assert "Main menu" in _draw(db, user, FakeSession(height=11))
    assert "Main menu" not in _draw(db, user, FakeSession(height=12))
    db.close()


def test_a_notice_goes_below_the_art(tmp_path):
    db, user = _setup(tmp_path)
    screen = _screen(_draw(db, user, FakeSession(), notice="Your level changed."))
    assert "Your level changed." in screen[11]
    db.close()


def test_a_multi_line_notice_counts_every_row_it_takes(tmp_path):
    # The art is 11 rows with the prompt at its slot. A one-row notice fits a
    # 13-row terminal (11 + 1 < 13); the same notice as four CR LF-joined
    # lines (an access change) needs 15 rows and gets the generated menu.
    db, user = _setup(tmp_path)
    assert "Main menu" not in _draw(db, user, FakeSession(height=13), notice="Level changed.")
    four = "\r\n".join(["Level changed.", "Verify granted.", "Staff granted.", "Staff removed."])
    assert "Main menu" in _draw(db, user, FakeSession(height=13), notice=four)
    assert "Main menu" not in _draw(db, user, FakeSession(height=16), notice=four)
    db.close()


def test_a_disabled_banner_draws_the_generated_menu(tmp_path):
    db, user = _setup(tmp_path)
    set_main_menu_banner_enabled(db, False)
    assert "Main menu" in _draw(db, user, FakeSession())
    db.close()


def test_the_prompt_goes_below_the_art_without_a_prompt_slot(tmp_path):
    art = b"{menu 70x6}\r\n\r\n\r\n\r\n\r\n\r\nend of art"
    db, user = _setup(tmp_path, art)
    text = _draw(db, user, FakeSession())
    screen = _screen(text)
    assert screen[6] == "end of art"
    assert screen[7].startswith("Choice:")
    db.close()


def test_the_console_masthead_preview_explains_slots_mode(tmp_path):
    from netbbs.net.admin_flow import _preview_main_menu_banner_screen
    from netbbs.storage.execution import DatabaseLane

    db, user = _setup(tmp_path)

    class ConsoleSession(FakeSession):
        async def read_any_key(self):
            return " "

    session = ConsoleSession()
    lane = DatabaseLane(db.path)
    try:
        asyncio.run(_preview_main_menu_banner_screen(session, lane, user))
    finally:
        lane.close()
    text = "".join(session.written)
    assert "(1 of 2: as you see it)" in text
    assert "no masthead" not in text
    db.close()
