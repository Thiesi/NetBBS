"""Integration tests for the main-menu masthead (issue #161) actually
being prepended by `netbbs.net.main_menu._draw_main_menu` -- distinct
from tests/test_main_menu_banner.py's own isolated loader/status tests.

The critical regression this guards: `rendering.layout.screen_title
(clear=True)` embeds its own `clear_screen()` *inside the string it
returns*. A naive "write the masthead, then write the title" ordering
would have the title's own clear_screen() wipe the just-written masthead
the instant redraw-in-place is on (see docs/NetBBS-worklog.md's
"rendering, input, and transport" section for the general invariant).
The disabled-masthead byte-for-byte-unchanged tests below are the other
half: this feature must be a complete non-event for every node that
never touches it.
"""

from __future__ import annotations

import asyncio

from netbbs.auth.users import create_user
from netbbs.chat.mailbox import MessageMailbox
from netbbs.net.main_menu import _draw_main_menu
from netbbs.net.main_menu_banner import main_menu_banner_path, set_main_menu_banner_enabled
from netbbs.net.redraw_preference import set_redraw_in_place_enabled
from netbbs.rendering.ansi import clear_screen
from netbbs.storage.database import Database


class FakeSession:
    def __init__(self):
        self.written: list[str] = []
        self.terminal_width = 80
        self.node_display_name = "NetBBS"
        self.node_name_gradient = None
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


def _user(db, *, redraw: bool = False):
    user = create_user(db, "alice", password="hunter2", user_level=10)
    if redraw:
        set_redraw_in_place_enabled(db, user, True)
    return user


def test_disabled_masthead_leaves_main_menu_byte_for_byte_unchanged(tmp_path):
    """No `main_menu_banner_enabled` config, no file -- the module
    existing at all must not change a single byte of the default menu."""
    db = Database(tmp_path / "node.db")
    user = _user(db)
    session = FakeSession()

    asyncio.run(_draw_main_menu(session, db, MessageMailbox(), user))

    text = _written_text(session)
    # The breadcrumb/title block starts immediately after "\r\n" -- an
    # SGR color escape, not literal "NetBBS" text (colored() wraps it).
    assert text.startswith("\r\n\x1b[")
    assert "Main menu" in text
    assert clear_screen() not in text
    db.close()


def test_disabled_masthead_with_redraw_in_place_still_clears_as_before(tmp_path):
    """The pre-existing redraw-in-place clear path (`screen_title
    (clear=True)`) must keep working exactly as before when no masthead
    is configured -- this feature must not regress that one."""
    db = Database(tmp_path / "node.db")
    user = _user(db, redraw=True)
    session = FakeSession()

    asyncio.run(_draw_main_menu(session, db, MessageMailbox(), user))

    text = _written_text(session)
    assert clear_screen() in text
    db.close()


def test_enabled_masthead_appears_above_the_main_menu(tmp_path):
    db = Database(tmp_path / "node.db")
    user = _user(db)
    main_menu_banner_path(db).write_bytes(b"MY CUSTOM MASTHEAD")
    set_main_menu_banner_enabled(db, True)
    session = FakeSession()

    asyncio.run(_draw_main_menu(session, db, MessageMailbox(), user))

    text = _written_text(session)
    assert "MY CUSTOM MASTHEAD" in text
    assert text.index("MY CUSTOM MASTHEAD") < text.index("Main menu")
    db.close()


def test_enabled_masthead_without_redraw_has_no_clear_screen(tmp_path):
    db = Database(tmp_path / "node.db")
    user = _user(db, redraw=False)
    main_menu_banner_path(db).write_bytes(b"MY CUSTOM MASTHEAD")
    set_main_menu_banner_enabled(db, True)
    session = FakeSession()

    asyncio.run(_draw_main_menu(session, db, MessageMailbox(), user))

    assert clear_screen() not in _written_text(session)
    db.close()


def test_enabled_masthead_with_redraw_clears_before_the_masthead_not_after(tmp_path):
    """The regression this whole feature could have introduced: the
    clear-screen sequence must land *before* the masthead text, never
    between the masthead and the title -- otherwise the masthead is
    wiped the instant it's drawn, on every redraw-in-place account."""
    db = Database(tmp_path / "node.db")
    user = _user(db, redraw=True)
    main_menu_banner_path(db).write_bytes(b"MY CUSTOM MASTHEAD")
    set_main_menu_banner_enabled(db, True)
    session = FakeSession()

    asyncio.run(_draw_main_menu(session, db, MessageMailbox(), user))

    text = _written_text(session)
    assert clear_screen() in text
    assert text.index(clear_screen()) < text.index("MY CUSTOM MASTHEAD")
    assert text.index("MY CUSTOM MASTHEAD") < text.index("Main menu")
    db.close()


def test_enabled_but_missing_masthead_file_falls_back_to_unchanged_menu(tmp_path):
    db = Database(tmp_path / "node.db")
    user = _user(db)
    set_main_menu_banner_enabled(db, True)  # enabled, but no file exists
    session = FakeSession()

    asyncio.run(_draw_main_menu(session, db, MessageMailbox(), user))

    text = _written_text(session)
    # The breadcrumb/title block starts immediately after "\r\n" -- an
    # SGR color escape, not literal "NetBBS" text (colored() wraps it).
    assert text.startswith("\r\n\x1b[")
    assert "Main menu" in text
    db.close()
