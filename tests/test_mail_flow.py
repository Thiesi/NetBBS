"""
UI-level tests for `netbbs.net.mail_flow` (design doc round 93/104):
local asynchronous personal mail wired into the main menu. The
underlying persistence/quota/deletion semantics are covered at the
library level in tests/test_mail.py -- these drive the real
`netbbs.net.login_flow._main_menu` / `netbbs.net.mail_flow.browse_mail`
entry points instead.

`netbbs.net.mail_flow` is the first module migrated onto design doc
round 91's two-lane database execution model (issue #57/round 111) --
`browse_mail` (and everything it calls) now takes a `DatabaseLane`
instead of a `Database`, so every test here constructs one instead.
Direct `Database` calls (`create_user`, `send_mail`, `list_inbox`, etc.)
used purely for test setup/assertions -- not exercising mail_flow.py's
own code -- are untouched, matching every other test file's existing
style: only the call *into* mail_flow.py/`_main_menu` needs a lane.
"""

from __future__ import annotations

import asyncio

from netbbs.auth.users import create_user
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.mail import list_inbox, list_sent, send_mail
from netbbs.net.char_input import InputHistory
from netbbs.net.login_flow import _main_menu
from netbbs.net.mail_flow import browse_mail
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


class FakeSession:
    def __init__(self, keys=None, lines=None):
        self._keys = iter(keys or [])
        self._lines = iter(lines or [])
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

    async def read_line(self, echo: bool = True, history=None, completer=None, *, live_buffer=None, lock=None) -> str:
        return next(self._lines, "")


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


# -- main menu integration ---------------------------------------------------


def test_main_menu_shows_mail_option_with_no_unread_badge(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    session = FakeSession(keys=["l"])
    lane = DatabaseLane(db_path)

    asyncio.run(
        _main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), bob, lane=lane)
    )

    text = _written_text(session)
    assert "-mail" in text
    assert "unread" not in text
    lane.close()
    db.close()


def test_main_menu_shows_unread_count_badge(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    send_mail(db, alice, bob, "Hello", "body")
    session = FakeSession(keys=["l"])
    lane = DatabaseLane(db_path)

    asyncio.run(
        _main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), bob, lane=lane)
    )

    assert "(1 unread)" in _written_text(session)
    lane.close()
    db.close()


def test_main_menu_e_key_opens_mail(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    session = FakeSession(keys=["e", "b", "l"])
    lane = DatabaseLane(db_path)

    asyncio.run(
        _main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), bob, lane=lane)
    )

    assert "Mail:" in _written_text(session)
    lane.close()
    db.close()


def test_main_menu_mail_unavailable_without_a_lane(tmp_path):
    """`lane=None` (the default -- every other `_main_menu` test in the
    codebase doesn't supply one) degrades gracefully rather than
    crashing, the same "hidden/unavailable in this context" shape
    `node_controls=None` already uses for the `[N]ode` admin option."""
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    session = FakeSession(keys=["e", "l"])

    asyncio.run(_main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), bob))

    assert "Mail is not available in this context." in _written_text(session)
    db.close()


# -- inbox --------------------------------------------------------------------


def test_inbox_empty_shows_empty_message(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    session = FakeSession(keys=["i", "b"])
    lane = DatabaseLane(db_path)

    asyncio.run(browse_mail(session, lane, bob))

    assert "Your inbox is empty." in _written_text(session)
    lane.close()
    db.close()


def test_inbox_shows_unread_marker_and_opening_marks_read(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    send_mail(db, alice, bob, "Hello", "How are you?")

    # Open inbox, select item 01 (marks read), back out of message, back
    # out of inbox, back out of mail menu.
    session = FakeSession(keys=["i", "0", "1", "b", "b", "b"])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, bob))

    text = _written_text(session)
    assert "* Hello" in text  # unread marker on the inbox listing
    assert "How are you?" in text
    assert list_inbox(db, bob)[0].is_read is True
    lane.close()
    db.close()


def test_inbox_delete_removes_message(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    send_mail(db, alice, bob, "Hello", "body")

    session = FakeSession(keys=["i", "0", "1", "d", "b", "b"])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, bob))

    assert "Message deleted." in _written_text(session)
    assert list_inbox(db, bob) == []
    lane.close()
    db.close()


def test_inbox_reply_sends_a_new_message(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    send_mail(db, alice, bob, "Hello", "body")

    session = FakeSession(
        keys=["i", "0", "1", "r", "b", "b", "b"],
        lines=["", "Sure thing, blank line to finish"],
    )
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, bob))

    assert "Message sent." in _written_text(session)
    sent = list_sent(db, bob)
    assert len(sent) == 1
    assert sent[0].subject == "Re: Hello"
    assert sent[0].recipient_user_id == alice.id
    lane.close()
    db.close()


# -- sent ----------------------------------------------------------------------


def test_sent_empty_shows_empty_message(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    session = FakeSession(keys=["s", "b"])
    lane = DatabaseLane(db_path)

    asyncio.run(browse_mail(session, lane, bob))

    assert "You haven't sent any mail." in _written_text(session)
    lane.close()
    db.close()


def test_sent_lists_recipient_and_delete_removes_it(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    send_mail(db, alice, bob, "Hello", "body")

    session = FakeSession(keys=["s", "0", "1", "d", "b", "b"])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    text = _written_text(session)
    assert "to bob" in text
    assert "Message deleted." in text
    assert list_sent(db, alice) == []
    lane.close()
    db.close()


# -- compose --------------------------------------------------------------------


def test_compose_sends_a_message(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)

    session = FakeSession(keys=["c", "b"], lines=["bob", "Hello", "How are you?", ""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    assert "Message sent." in _written_text(session)
    inbox = list_inbox(db, bob)
    assert len(inbox) == 1
    assert inbox[0].subject == "Hello"
    assert inbox[0].body == "How are you?"
    lane.close()
    db.close()


def test_compose_rejects_unknown_recipient(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)

    session = FakeSession(keys=["c", "b"], lines=["nobody"])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    assert "No such user" in _written_text(session)
    lane.close()
    db.close()


def test_compose_cancels_on_blank_recipient(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)

    session = FakeSession(keys=["c", "b"], lines=[""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    assert "Cancelled." in _written_text(session)
    lane.close()
    db.close()


def test_compose_rejects_blank_subject(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    create_user(db, "bob", password="hunter2pw", user_level=10)

    session = FakeSession(keys=["c", "b"], lines=["bob", "   "])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    assert "a subject is required" in _written_text(session)
    lane.close()
    db.close()


def test_compose_rejects_blank_body(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    create_user(db, "bob", password="hunter2pw", user_level=10)

    session = FakeSession(keys=["c", "b"], lines=["bob", "Hello", ""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    assert "message body cannot be blank" in _written_text(session)
    lane.close()
    db.close()


def test_compose_reports_bounce_when_mailbox_is_full(tmp_path, monkeypatch):
    import netbbs.mail as mail_module

    monkeypatch.setattr(mail_module, "MAX_MAIL_PER_RECIPIENT", 1)

    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    send_mail(db, alice, bob, "First", "body")  # left unread -- fills the (patched) cap

    session = FakeSession(keys=["c", "b"], lines=["bob", "Second", "body", ""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    assert "mailbox is full" in _written_text(session)
    assert len(list_inbox(db, bob)) == 1
    lane.close()
    db.close()
