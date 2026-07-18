"""
Regression tests for the main menu's SysOp entry: keystroke "s"
(BBS-conventional "SysOp" naming, Thiesi's own explicit request),
replacing the previous generic "a"/"Admin" -- `netbbs.net.login_flow.
_draw_main_menu`'s label and `_main_menu`'s dispatch branch.
"""

from __future__ import annotations

import asyncio
import re

from netbbs.auth.users import create_user
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.net.char_input import InputHistory
from netbbs.net.login_flow import _draw_main_menu, _main_menu
from netbbs.storage.database import Database


class FakeSession:
    def __init__(self, keys=None):
        self._keys = iter(keys or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_key(self, echo: bool = True) -> str:
        key = next(self._keys, None)
        if key is None:
            raise AssertionError("FakeSession.read_key() called with no more scripted keys")
        return key

    async def read_line(self, echo: bool = True) -> str:
        raise AssertionError("read_line should not be reached by these tests")


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _visible_text(session: FakeSession) -> str:
    """`_written_text` with SGR escape sequences stripped -- needed
    since `menu_key` colors only the bracketed hotkey letter itself, so
    e.g. "SysOp" is split across a run of raw bytes as "S" + an SGR
    reset + "ysOp", not one contiguous substring, in the unstripped
    written text."""
    return _ANSI_ESCAPE_RE.sub("", _written_text(session))


def test_main_menu_shows_sysop_option_for_a_sysop_level_user(tmp_path):
    db = Database(tmp_path / "node.db")
    sysop = create_user(db, "root", password="hunter2", user_level=255)
    session = FakeSession()

    asyncio.run(_draw_main_menu(session, db, MessageMailbox(), sysop))

    text = _visible_text(session)
    assert "[S]ysOp" in text
    assert "Admin" not in text
    db.close()


def test_main_menu_hides_sysop_option_for_an_ordinary_user(tmp_path):
    db = Database(tmp_path / "node.db")
    user = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession()

    asyncio.run(_draw_main_menu(session, db, MessageMailbox(), user))

    assert "SysOp" not in _visible_text(session)
    db.close()


def test_pressing_s_reaches_the_sysop_branch_for_a_sysop_level_user(tmp_path):
    db = Database(tmp_path / "node.db")
    sysop = create_user(db, "root", password="hunter2", user_level=255)
    # No `lane` supplied (matches every other bare _main_menu() test call
    # in this suite) -- routes into the "not available in this context"
    # fallback rather than a real admin_menu, but that's still enough to
    # prove "s" reaches the SysOp branch at all, which is what's under
    # test here, not admin_menu's own behavior (covered separately in
    # tests/test_admin_flow.py).
    session = FakeSession(keys=["s", "l"])

    asyncio.run(
        _main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), sysop)
    )

    assert "SysOp menu is not available in this context." in _written_text(session)
    db.close()


def test_pressing_a_is_now_an_invalid_key_for_a_sysop_level_user(tmp_path):
    """"a" used to be the Admin keystroke -- confirms it's been fully
    retired, not left as a silent second way in, the same "only one
    real keystroke per option" invariant every other menu here has."""
    db = Database(tmp_path / "node.db")
    sysop = create_user(db, "root", password="hunter2", user_level=255)
    session = FakeSession(keys=["a", "l"])

    asyncio.run(
        _main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), sysop)
    )

    text = _written_text(session)
    assert "\b \b\a" in text  # rejected as an invalid keystroke -- bell only
    assert "SysOp menu is not available in this context." not in text
    db.close()


def test_pressing_s_does_nothing_for_a_non_sysop_user(tmp_path):
    db = Database(tmp_path / "node.db")
    user = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(keys=["s", "l"])

    asyncio.run(
        _main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), user)
    )

    text = _written_text(session)
    assert "\b \b\a" in text  # rejected -- meets_level(SYSOP_LEVEL) fails
    assert "SysOp menu is not available in this context." not in text
    db.close()
