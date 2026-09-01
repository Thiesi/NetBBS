"""Test for issue #102's Ctrl-L wiring on the main menu --
`netbbs.net.main_menu._main_menu` redraws itself in place, not a
rejected keystroke, and without consuming an extra logoff-confirmation
answer (it's a no-op, not a real menu action)."""

from __future__ import annotations

import asyncio

from netbbs.auth.users import create_user
from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
from netbbs.net.char_input import REDRAW_KEY, InputHistory
from netbbs.net.main_menu import _main_menu
from netbbs.storage.database import Database


class FakeSession:
    def __init__(self, keys: list[str] | None = None, lines: list[str] | None = None):
        self._keys = iter(keys or [])
        self._lines = iter(lines or [])
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

    async def read_key(self, echo: bool = True) -> str:
        return next(self._keys)

    async def read_line(self, echo: bool = True, history=None, completer=None) -> str:
        return next(self._lines)


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


def test_ctrl_l_redraws_the_main_menu_without_a_bell_or_confirm_prompt(tmp_path):
    database = Database(tmp_path / "node.db")
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    # Only one scripted line ("y"), for the eventual real logoff -- if
    # Ctrl-L wrongly triggered the logoff-confirmation prompt, the
    # second read_line() call would raise StopIteration and fail the
    # test outright, proving it didn't.
    session = FakeSession(keys=[REDRAW_KEY, "l"], lines=["y"])

    asyncio.run(
        _main_menu(session, database, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), alice)
    )

    text = _written_text(session)
    assert "\b \b\a" not in text  # never treated as an invalid keystroke
    assert text.count("Main menu") == 2  # drawn once on entry, once more for Ctrl-L
    database.close()
