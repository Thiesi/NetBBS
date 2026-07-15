"""
Integration test for the one flush point Phase 2 Track 5e's mailbox +
next-prompt private-message delivery relies on (design doc round 32,
sign-off round 46): the top of `netbbs.net.login_flow._draw_main_menu`,
called on entry to `_main_menu` and again after returning from every
submenu -- the one choke point every screen (boards, files, directory,
profile, chat) passes through before its next redraw. Library-level
`MessageMailbox` behavior is covered separately in
tests/test_chat_mailbox.py; the actual `/msg` delivery wiring is
covered in tests/test_chat_flow_private.py.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import User, create_user
from netbbs.chat import MessageMailbox, PresenceRegistry
from netbbs.net import login_flow
from netbbs.net.char_input import InputHistory
from netbbs.storage.database import Database

_T = "2026-01-01T00:00:00.000000Z"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


class FakeSession:
    def __init__(self, keys: list[str] | None = None):
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
        return next(self._keys)

    @property
    def output(self) -> str:
        return "".join(self.written)


def _make_user(db: Database) -> User:
    # A real DB-backed account, not a synthetic dataclass (GitHub issue
    # #29's cross-process revalidation boundary re-fetches by username
    # on every main-menu action, so a user with no actual row would
    # always look deleted).
    return create_user(db, "alice", password="hunter2", user_level=0)


def test_pending_private_message_shown_before_the_menu_on_entry(db):
    async def scenario():
        mailbox = MessageMailbox()
        session = FakeSession(keys=["l"])  # logoff immediately
        # Delivered by session (GitHub issue #27), not by username --
        # the same session object _main_menu will later flush() by.
        mailbox.deliver(session, "*** Private message from bob: hi there", _T)
        await login_flow._main_menu(session, db, object(), PresenceRegistry(), mailbox, InputHistory(), _make_user(db))
        return session

    session = asyncio.run(scenario())
    output = session.output
    assert "Private message from bob: hi there" in output
    # It genuinely arrived *before* the menu, not just somewhere in the output.
    assert output.index("Private message from bob") < output.index("Main menu:")


def test_message_is_only_shown_once_not_on_every_redraw(db):
    async def scenario():
        mailbox = MessageMailbox()
        session = FakeSession(keys=["l"])
        mailbox.deliver(session, "*** Private message from bob: only once", _T)
        await login_flow._main_menu(session, db, object(), PresenceRegistry(), mailbox, InputHistory(), _make_user(db))
        return session

    session = asyncio.run(scenario())
    assert session.output.count("only once") == 1


def test_no_extra_output_when_mailbox_is_empty(db):
    async def scenario():
        mailbox = MessageMailbox()
        session = FakeSession(keys=["l"])
        await login_flow._main_menu(session, db, object(), PresenceRegistry(), mailbox, InputHistory(), _make_user(db))
        return session

    session = asyncio.run(scenario())
    assert "Private message" not in session.output


def test_a_second_pending_message_delivered_after_returning_to_the_menu(db, monkeypatch):
    # Confirms the flush isn't a one-shot thing tied only to the very
    # first draw -- _draw_main_menu is the same function called again
    # after every submenu return, so a message queued while "in" a
    # submenu shows up the next time the menu itself is redrawn.
    mailbox = MessageMailbox()
    user = _make_user(db)

    async def fake_browse_boards(session, db, u):
        # Simulate a message arriving while the user was off in another
        # screen entirely. Delivered by `session` (GitHub issue #27) --
        # the exact object passed in here is the same one _main_menu is
        # about to flush() by.
        mailbox.deliver(session, "*** Private message from bob: while you were away", _T)

    monkeypatch.setattr(login_flow, "_browse_boards", fake_browse_boards)

    async def scenario():
        session = FakeSession(keys=["m", "l"])  # boards, then logoff
        await login_flow._main_menu(session, db, object(), PresenceRegistry(), mailbox, InputHistory(), user)
        return session

    session = asyncio.run(scenario())
    assert "while you were away" in session.output
