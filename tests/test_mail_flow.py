"""
UI-level tests for `netbbs.net.mail_flow`: local asynchronous personal
mail wired into the main menu. The underlying persistence/quota/deletion
semantics are covered at the library level in tests/test_mail.py --
these drive the real `netbbs.net.main_menu._main_menu` /
`netbbs.net.mail_flow.browse_mail` entry points instead.

`netbbs.net.mail_flow` is the first module migrated onto the two-lane
database execution model (issue #57) -- `browse_mail` (and everything
it calls) now takes a `DatabaseLane`
instead of a `Database`, so every test here constructs one instead.
Direct `Database` calls (`create_user`, `send_mail`, `list_inbox`, etc.)
used purely for test setup/assertions -- not exercising mail_flow.py's
own code -- are untouched, matching every other test file's existing
style: only the call *into* mail_flow.py/`_main_menu` needs a lane.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.link.boards import LinkContext
from netbbs.link.events import build_endpoint_descriptor
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import LinkNode, PeerRecord
from netbbs.link.store import save_peer
from netbbs.link.trust import (
    TrustDimension, TrustState, TrustSubject, register_subject, set_trust_override,
)
from netbbs.mail import list_inbox, list_sent, send_mail, send_system_mail
from netbbs.net.char_input import InputCancelled, InputHistory
from netbbs.net.main_menu import _main_menu
from netbbs.net.mail_flow import browse_mail
from netbbs.rendering import (
    ACCENT_COLOR,
    ERROR_COLOR,
    LABEL_COLOR,
    METADATA_COLOR,
    SUCCESS_COLOR,
    colored,
)
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


# A scripted line that presses Esc instead of answering (issue #812).
ESC = object()


class FakeSession:
    def __init__(self, keys=None, lines=None):
        self._keys = iter(keys or [])
        self._lines = iter(lines or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.node_display_name = "NetBBS"
        self.node_name_gradient = None
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"
        self.seeded: list[str] = []
        self.pasted_color_offered: list[bool] = []

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_key(self, echo: bool = True) -> str:
        key = next(self._keys, None)
        if key is None:
            raise AssertionError("FakeSession.read_key() called with no more scripted keys")
        return key

    async def read_line(self, echo: bool = True, history=None, completer=None, *, live_buffer=None, lock=None, **kwargs) -> str:
        self.seeded.append(kwargs.get("initial", ""))
        self.pasted_color_offered.append(kwargs.get("pasted_color") is not None)
        line = next(self._lines, "")
        if line is ESC:
            assert kwargs.get("cancellable"), "Esc pressed at a prompt that does not accept it"
            raise InputCancelled()
        return line


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _visible_text(session: FakeSession) -> str:
    return _ANSI_ESCAPE_RE.sub("", _written_text(session))


# -- main menu integration ---------------------------------------------------


def test_main_menu_shows_mail_option_with_no_unread_badge(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    session = FakeSession(keys=["l"], lines=["y"])
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
    session = FakeSession(keys=["l"], lines=["y"])
    lane = DatabaseLane(db_path)

    asyncio.run(
        _main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), bob, lane=lane)
    )

    assert "(1 unread)" in _written_text(session)
    assert "1 unread message" in _written_text(session)
    lane.close()
    db.close()


def test_main_menu_pluralizes_the_unread_message_count(tmp_path):
    # Dogfood follow-up: the main menu's own subtitle line used to say
    # "2 unread mail" (no pluralization at all, and inconsistent with
    # the Mail submenu's own already-correct "2 unread messages").
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    send_mail(db, alice, bob, "Hello", "body")
    send_mail(db, alice, bob, "Hello again", "body")
    session = FakeSession(keys=["l"], lines=["y"])
    lane = DatabaseLane(db_path)

    asyncio.run(
        _main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), bob, lane=lane)
    )

    text = _written_text(session)
    assert "2 unread messages" in text
    assert "2 unread mail" not in text
    lane.close()
    db.close()


def test_main_menu_e_key_opens_mail(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    session = FakeSession(keys=["e", "b", "l"], lines=["y"])
    lane = DatabaseLane(db_path)

    asyncio.run(
        _main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), bob, lane=lane)
    )

    assert "NetBBS › Mail" in _visible_text(session)
    assert "Inbox caught up" in _written_text(session)
    lane.close()
    db.close()


def test_main_menu_mail_unavailable_without_a_lane(tmp_path):
    """`lane=None` (the default -- every other `_main_menu` test in the
    codebase doesn't supply one) degrades gracefully rather than
    crashing, the same "hidden/unavailable in this context" shape
    `node_controls=None` already uses for the `[N]ode` admin option."""
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    session = FakeSession(keys=["e", "l"], lines=["y"])

    asyncio.run(_main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), bob))

    assert "Mail is not available in this context." in _written_text(session)
    db.close()


# -- inbox --------------------------------------------------------------------


def test_inbox_empty_shows_empty_message(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    session = FakeSession(keys=["b"])
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
    session = FakeSession(keys=["1", "b", "b"])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, bob))

    text = _written_text(session)
    # The unread marker is a column of its own (issue #810), not a prefix
    # of the subject.
    assert re.search(r"1  new  alice +Hello", _visible_text(session))
    assert "[NEW]" not in text
    assert "1 unread message" in text
    assert "How are you?" in text
    assert "NetBBS › Mail › Inbox › Hello" in _visible_text(session)
    assert colored("From: ", fg_color=LABEL_COLOR) in text
    assert colored("alice", fg_color=ACCENT_COLOR) in text
    assert colored("Date: ", fg_color=LABEL_COLOR) in text
    assert f"\x1b[38;5;{METADATA_COLOR}m" in text
    assert list_inbox(db, bob)[0].is_read is True
    lane.close()
    db.close()


def test_inbox_delete_removes_message(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    send_mail(db, alice, bob, "Hello", "body")

    session = FakeSession(keys=["1", "d", "b"], lines=["y"])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, bob))

    assert "Message deleted." in _written_text(session)
    assert colored("Message deleted.", fg_color=SUCCESS_COLOR) in _written_text(session)
    assert list_inbox(db, bob) == []
    lane.close()
    db.close()


def test_inbox_delete_declined_at_the_confirmation_keeps_the_message(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    send_mail(db, alice, bob, "Hello", "body")

    # Bare Enter at the confirmation selects its default (No, per
    # `prompt_yes_no(..., default=False)`) -- back to the message view,
    # then "b"/"b" out entirely.
    session = FakeSession(keys=["1", "d", "b", "b"], lines=[""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, bob))

    assert "Message deleted." not in _written_text(session)
    assert list_inbox(db, bob) != []
    lane.close()
    db.close()


def test_inbox_reply_sends_a_new_message(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    send_mail(db, alice, bob, "Hello", "body")

    session = FakeSession(
        keys=["1", "r", "s", "b", "b"],
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
    session = FakeSession(keys=["s", "b", "b"])
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

    session = FakeSession(keys=["s", "1", "d", "b", "b"], lines=["y"])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    text = _written_text(session)
    assert re.search(r"1  bob +Hello", _visible_text(session))
    assert "NetBBS › Mail › Sent › Hello" in _visible_text(session)
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

    session = FakeSession(keys=["c", "s", "b"], lines=["bob", "Hello", "How are you?", ""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    assert "Message sent." in _written_text(session)
    inbox = list_inbox(db, bob)
    assert len(inbox) == 1
    assert inbox[0].subject == "Hello"
    assert inbox[0].body == "How are you?"
    lane.close()
    db.close()


def test_compose_appends_the_sender_signature(tmp_path):
    from netbbs.signature import set_signature

    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    set_signature(db, alice, "Alice")

    session = FakeSession(keys=["c", "s", "b"], lines=["bob", "Hello", "How are you?", ""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    inbox = list_inbox(db, bob)
    assert len(inbox) == 1
    assert inbox[0].body == "How are you?\n-- \nAlice"
    lane.close()
    db.close()


def test_compose_sends_no_signature_block_when_none_is_set(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)

    session = FakeSession(keys=["c", "s", "b"], lines=["bob", "Hello", "How are you?", ""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    inbox = list_inbox(db, bob)
    assert inbox[0].body == "How are you?"
    lane.close()
    db.close()


class _EnterTrackingFakeSession(FakeSession):
    """`FakeSession` has no `discard_buffered_enter` at all (a
    lightweight test double, matching every other narrow `Session`-like
    fake in this codebase) -- proves `_compose_mail` actually calls it
    when present, the way a real `TelnetSession`/`SSHServerSession`
    would, since `FakeSession`'s own line-scripted `read_line` can't
    reproduce a genuinely leaked raw Enter byte to test the effect end
    to end (that's `tests/test_char_input.py`'s job, at the byte level
    the mechanism itself lives at)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.discard_buffered_enter_calls = 0

    async def discard_buffered_enter(self) -> None:
        self.discard_buffered_enter_calls += 1


def test_compose_discards_a_buffered_enter_right_after_the_hotkey(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)

    session = _EnterTrackingFakeSession(keys=["c", "b"], lines=[""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    assert session.discard_buffered_enter_calls == 1


def test_compose_rejects_unknown_recipient(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)

    session = FakeSession(keys=["c", "b"], lines=["nobody"])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    assert "No such user" in _written_text(session)
    assert f"\x1b[38;5;{ERROR_COLOR}m" in _written_text(session)
    lane.close()
    db.close()


def test_compose_retries_the_recipient_prompt_in_place_after_an_unknown_username(tmp_path):
    # Dogfood follow-up: a typo'd recipient used to discard the whole
    # compose attempt (return straight to the Mail menu); it should
    # instead just re-prompt for "To:" so a fixable mistake doesn't cost
    # the subject/body the caller hasn't even typed yet -- matching how
    # the identical error at final commit time already only re-prompts
    # for the recipient, not the whole message.
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    create_user(db, "bob", password="hunter2pw", user_level=10)

    session = FakeSession(
        keys=["c", "s", "b"],
        lines=["nobody", "bob", "Hello", "How are you?", ""],
    )
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    text = _written_text(session)
    assert "No such user" in text
    assert "Message sent." in text
    sent = list_sent(db, alice)
    assert len(sent) == 1
    assert sent[0].subject == "Hello"
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


def test_compose_asks_again_for_a_blank_subject(tmp_path):
    """Issue #812: an empty subject asks again in place, rather than
    throwing the whole message away."""
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)

    session = FakeSession(keys=["c", "s", "b"], lines=["bob", "   ", "Hello", "Body", ""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    text = _written_text(session)
    assert "A subject is required -- type one, or press Esc to cancel." in text
    assert "Cancelled -- a subject is required" not in text
    assert [m.subject for m in list_inbox(db, bob)] == ["Hello"]
    lane.close()
    db.close()


def test_compose_esc_at_the_subject_cancels_the_message(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)

    session = FakeSession(keys=["c", "b"], lines=["bob", "", ESC])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    assert "Message cancelled." in _written_text(session)
    assert list_inbox(db, bob) == []
    lane.close()
    db.close()


def test_compose_refuses_a_long_subject_at_its_prompt_in_characters(tmp_path):
    """Issue #812: 150 accented letters are 300 bytes. Refused where they
    are typed -- not after the body is written -- in characters, and the
    prompt reopens on them so they can be shortened."""
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)

    long_subject = "é" * 150
    session = FakeSession(keys=["c", "s", "b"], lines=["bob", long_subject, "é" * 100, "Body", ""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    text = _visible_text(session)
    assert "That subject is 50 characters too long -- shorten it, or press Esc to cancel." in text
    assert "bytes" not in text
    # The retry opened on what was typed, not on an empty line.
    assert long_subject in session.seeded
    assert [m.subject for m in list_inbox(db, bob)] == ["é" * 100]
    lane.close()
    db.close()


def test_compose_update_subject_refuses_a_long_subject_and_esc_keeps_the_old_one(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)

    session = FakeSession(
        keys=["c", "u", "s", "b"], lines=["bob", "Hello", "Body", "/done", "x" * 229, ESC],
    )
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    text = _visible_text(session)
    assert "That subject is 29 characters too long -- shorten it, or press Esc to keep the previous subject." in text
    assert [m.subject for m in list_inbox(db, bob)] == ["Hello"]
    lane.close()
    db.close()


def test_compose_a_signature_that_overflows_the_body_is_caught_before_send(tmp_path, monkeypatch):
    """Issue #812: the editors stop the body at the limit, but the
    signature is added after them. The review says so, in characters, and
    Send is refused until the body is shortened."""
    import netbbs.net.mail_flow as mail_flow_module
    from netbbs.signature import set_signature

    monkeypatch.setattr(mail_flow_module, "MAX_MAIL_BODY_BYTES", 40)
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    set_signature(db, alice, "Alice of the Long Signature")

    session = FakeSession(
        keys=["c", "s", "b", "s", "b"],
        lines=["bob", "Hello", "Twenty characters!!", "/done", "/delete 1", "/list", "/done"],
    )
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    text = _visible_text(session)
    assert "The message is" in text and "characters too long -- shorten it with [B]ody." in text
    assert "bytes" not in text
    inbox = list_inbox(db, bob)
    assert len(inbox) == 1
    assert "Twenty" not in inbox[0].body
    lane.close()
    db.close()


def test_compose_rejects_blank_body(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    create_user(db, "bob", password="hunter2pw", user_level=10)

    session = FakeSession(keys=["c", "b"], lines=["bob", "Hello", "/cancel"])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    assert "Message cancelled" in _written_text(session)
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

    session = FakeSession(keys=["c", "s", "c", "b"], lines=["bob", "Second", "body", ""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    assert "mailbox is full" in _written_text(session)
    assert len(list_inbox(db, bob)) == 1
    lane.close()
    db.close()


def test_compose_review_can_revise_recipient_subject_and_submitted_body_lines(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    create_user(db, "bob", password="hunter2pw", user_level=10)
    carol = create_user(db, "carol", password="hunter2pw", user_level=10)

    session = FakeSession(
        keys=["c", "t", "u", "b", "s", "b"],
        lines=[
            "bob", "Original subject", "first", "second", "/done",
            "carol", "Revised subject", "/edit 1", "FIRST", "/delete 2", "/done",
        ],
    )
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    inbox = list_inbox(db, carol)
    assert len(inbox) == 1
    assert inbox[0].subject == "Revised subject"
    assert inbox[0].body == "FIRST"
    assert "Review composition" in _written_text(session)
    lane.close()
    db.close()


def test_compose_review_cancel_persists_nothing(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    session = FakeSession(keys=["c", "c", "b"], lines=["bob", "Subject", "Body", ""])
    lane = DatabaseLane(db_path)

    asyncio.run(browse_mail(session, lane, alice))

    assert list_inbox(db, bob) == []
    assert "Message cancelled" in _written_text(session)
    lane.close()
    db.close()


def test_delivery_failure_returns_to_review_and_can_retarget(tmp_path, monkeypatch):
    import netbbs.mail as mail_module

    monkeypatch.setattr(mail_module, "MAX_MAIL_PER_RECIPIENT", 1)
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    carol = create_user(db, "carol", password="hunter2pw", user_level=10)
    send_mail(db, alice, bob, "Already full", "body")
    session = FakeSession(
        keys=["c", "s", "t", "s", "b"],
        lines=["bob", "Recoverable", "draft body", "/done", "carol"],
    )
    lane = DatabaseLane(db_path)

    asyncio.run(browse_mail(session, lane, alice))

    assert "mailbox is full" in _written_text(session)
    assert list_inbox(db, carol)[0].body == "draft body"
    lane.close()
    db.close()


def test_fullscreen_mail_save_still_requires_review_and_can_send(tmp_path):
    from netbbs.net.editor_preference import set_fullscreen_editor_enabled
    from tests.test_login_flow_fullscreen_editor import FakeSession as FullscreenSession
    from tests.test_login_flow_fullscreen_editor import _type

    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    set_fullscreen_editor_enabled(db, alice, True)
    session = FullscreenSession(
        ["c", "bob", "Subject"] + _type("Fullscreen body") + ["CTRL+O", "s", "b"]
    )
    lane = DatabaseLane(db_path)

    asyncio.run(browse_mail(session, lane, alice))

    assert "Review composition" in _written_text(session)
    assert list_inbox(db, bob)[0].body == "Fullscreen body"
    lane.close()
    db.close()


def test_fullscreen_mail_delivery_failure_keeps_draft_recoverable(tmp_path, monkeypatch):
    import netbbs.mail as mail_module

    from netbbs.net.editor_preference import set_fullscreen_editor_enabled
    from tests.test_login_flow_fullscreen_editor import FakeSession as FullscreenSession
    from tests.test_login_flow_fullscreen_editor import _type

    monkeypatch.setattr(mail_module, "MAX_MAIL_PER_RECIPIENT", 1)
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    carol = create_user(db, "carol", password="hunter2pw", user_level=10)
    send_mail(db, alice, bob, "Already full", "body")
    set_fullscreen_editor_enabled(db, alice, True)
    session = FullscreenSession(
        ["c", "bob", "Recoverable"]
        + _type("Fullscreen draft")
        + ["CTRL+O", "s", "t", "carol", "s", "b"]
    )
    lane = DatabaseLane(db_path)

    asyncio.run(browse_mail(session, lane, alice))

    assert "mailbox is full" in _written_text(session)
    assert list_inbox(db, carol)[0].body == "Fullscreen draft"
    lane.close()
    db.close()


# -- compose: Link addresses --------------------------------------------------


def _establish_node(db, fingerprint):
    """What a SysOp does before this node sends a peer mail (issue #804)."""
    subject = TrustSubject.node(fingerprint)
    register_subject(db, subject, first_accepted_at=_TRUST_NOW, now_iso=_TRUST_NOW)
    for dimension in (TrustDimension.IDENTITY_INTEGRITY, TrustDimension.RESOURCE_BEHAVIOR):
        set_trust_override(
            db, subject, dimension, TrustState.ESTABLISHED, reason="test", now_iso=_TRUST_NOW,
        )


_TRUST_NOW = "2026-01-01T00:00:00+00:00"


def _link_context_with_known_peer(
    db, node_identity, peer_identity, *, friendly_name="Farpoint", established=True,
):
    descriptor = build_endpoint_descriptor(
        signing_identity=peer_identity.signing_key,
        subject_fingerprint=peer_identity.fingerprint,
        addresses=None,
        outgoing_only=True,
        created_at="2026-01-01T00:00:00+00:00",
        friendly_name=friendly_name,
        canonical_dns_name="farpoint.example.org",
    )
    save_peer(
        db,
        PeerRecord(
            fingerprint=peer_identity.fingerprint,
            root_public_key=bytes(peer_identity.root.verify_key),
            transitions=peer_identity.transitions,
            descriptor=descriptor,
        ),
    )
    if established:
        _establish_node(db, peer_identity.fingerprint)
    return LinkContext(link_node=LinkNode(identity=node_identity))


def test_compose_sends_a_link_message_to_a_remote_address(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    node_identity = bootstrap_node_identity("roanoke")
    remote_identity = bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(db, node_identity, remote_identity)

    session = FakeSession(
        keys=["c", "s", "b"], lines=[f"bob@{remote_identity.fingerprint}", "Hello", "How are you?", ""]
    )
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))

    assert "Message sent." in _written_text(session)
    row = db.connection.execute(
        "SELECT recipient_remote_address, subject, body, link_delivery_status FROM mail_messages"
    ).fetchone()
    assert row["recipient_remote_address"] == f"bob@{remote_identity.fingerprint}"
    assert row["subject"] == "Hello"
    assert row["body"] == "How are you?"
    assert row["link_delivery_status"] == "pending"
    lane.close()
    db.close()


def test_compose_resolves_a_friendly_or_dns_node_name_before_sending(tmp_path):
    for node_reference in ("Farpoint", "farpoint.example.org"):
        db_path = tmp_path / f"{node_reference.replace('.', '-')}.db"
        db = Database(db_path)
        alice = create_user(db, "alice", password="hunter2pw", user_level=10)
        node_identity = bootstrap_node_identity("roanoke")
        remote_identity = bootstrap_node_identity("farpoint")
        link_context = _link_context_with_known_peer(db, node_identity, remote_identity)
        session = FakeSession(
            keys=["c", "s", "b"], lines=[f"bob@{node_reference}", "Hello", "Named route", ""]
        )
        lane = DatabaseLane(db_path)
        asyncio.run(browse_mail(session, lane, alice, link_context=link_context))
        row = db.connection.execute(
            "SELECT recipient_remote_address FROM mail_messages"
        ).fetchone()
        assert row["recipient_remote_address"] == f"bob@{remote_identity.fingerprint}"
        lane.close()
        db.close()


def test_compose_allows_at_signs_in_a_friendly_node_name(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    node_identity = bootstrap_node_identity("roanoke")
    remote_identity = bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(
        db, node_identity, remote_identity, friendly_name="Cats@Night",
    )
    session = FakeSession(
        keys=["c", "s", "b"], lines=["bob@Cats@Night", "Hello", "Named route", ""]
    )
    lane = DatabaseLane(db_path)

    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))

    row = db.connection.execute(
        "SELECT recipient_remote_address FROM mail_messages"
    ).fetchone()
    assert row["recipient_remote_address"] == f"bob@{remote_identity.fingerprint}"
    lane.close()
    db.close()


def test_compose_refuses_a_peer_still_on_probation_at_the_to_prompt(tmp_path):
    """Issue #804: mail to a newly linked peer cannot be delivered, so the
    caller hears it at the To prompt and is asked again in place -- never
    after writing the message, and never "Message sent."."""
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    node_identity = bootstrap_node_identity("roanoke")
    remote_identity = bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(db, node_identity, remote_identity, established=False)

    session = FakeSession(keys=["c", "b"], lines=["bob@Farpoint", ""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))

    text = _visible_text(session)
    assert "Farpoint · farpoint.example.org is not linked yet; mail opens once the SysOp establishes it." in text
    assert text.count("To: ") == 2
    assert "Subject:" not in text
    assert "Message sent." not in text
    assert db.connection.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0] == 0
    lane.close()
    db.close()


def test_compose_refuses_a_probationary_peer_chosen_from_the_review_screen(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    node_identity = bootstrap_node_identity("roanoke")
    established = bootstrap_node_identity("farpoint")
    newcomer = bootstrap_node_identity("newcomer")
    link_context = _link_context_with_known_peer(db, node_identity, established)
    _link_context_with_known_peer(db, node_identity, newcomer, friendly_name="Newcomer", established=False)

    session = FakeSession(
        keys=["c", "t", "s", "c", "b"],
        lines=["bob@Farpoint", "Hello", "Body", "/done", "bob@Newcomer"],
    )
    session.terminal_width = 200
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))

    text = _visible_text(session)
    assert "Newcomer · farpoint.example.org is not linked yet; mail opens once the SysOp establishes it." in text
    assert "Message sent." not in text
    assert db.connection.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0] == 0
    lane.close()
    db.close()


def test_compose_prompt_mentions_link_address_option_when_link_context_given(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    node_identity = bootstrap_node_identity("roanoke")
    link_context = LinkContext(link_node=LinkNode(identity=node_identity))

    session = FakeSession(keys=["c", "b"], lines=[""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))

    assert "name@TheirBBS for someone on a linked BBS" in _written_text(session)
    assert "node-name-or-dns" not in _written_text(session)
    lane.close()
    db.close()


def test_compose_rejects_a_link_address_for_a_node_never_seen(tmp_path):
    """Issue #807: an unknown node is said at the To prompt, which asks
    again in place -- not after the subject and body are written."""
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    node_identity = bootstrap_node_identity("roanoke")
    link_context = LinkContext(link_node=LinkNode(identity=node_identity))

    session = FakeSession(keys=["c", "b"], lines=["bob@nowhere", ""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))

    text = _visible_text(session)
    assert 'No BBS linked with this one goes by "nowhere". Check the name after the @' in text
    assert text.count("To: ") == 2
    assert "Subject:" not in text
    assert db.connection.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0] == 0
    lane.close()
    db.close()


def test_compose_ambiguous_link_address_shows_usable_technical_identities(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    node_identity = bootstrap_node_identity("roanoke")
    remote_identities = [
        bootstrap_node_identity("farpoint-one"),
        bootstrap_node_identity("farpoint-two"),
    ]
    link_context = None
    for remote_identity in remote_identities:
        link_context = _link_context_with_known_peer(
            db, node_identity, remote_identity, friendly_name="Shared Node",
        )
    assert link_context is not None
    chosen = remote_identities[1].fingerprint
    session = FakeSession(
        keys=["c", "s", "b"],
        lines=["bob@farpoint.example.org", f"bob@{chosen}", "Hello", "Ambiguous route", ""],
    )
    session.terminal_width = 200
    lane = DatabaseLane(db_path)

    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))

    text = _visible_text(session)
    assert 'More than one linked node goes by "farpoint.example.org". Type one of these instead:' in text
    assert "technical-identity" not in text
    assert all(
        f"bob@{identity.fingerprint} for Shared Node · farpoint.example.org" in text
        for identity in remote_identities
    )
    assert text.index("Type one of these instead") < text.index("Subject:")
    row = db.connection.execute("SELECT recipient_remote_address FROM mail_messages").fetchone()
    assert row["recipient_remote_address"] == f"bob@{chosen}"
    lane.close()
    db.close()


def test_compose_addresses_a_capitalized_name_exactly_as_displayed(tmp_path):
    """Issue #807: `OldNib@Farpoint`, typed as the From line shows it, is
    accepted; the recipient's node looks the name up case-insensitively."""
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    node_identity = bootstrap_node_identity("roanoke")
    remote_identity = bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(db, node_identity, remote_identity)

    session = FakeSession(keys=["c", "s", "b"], lines=["OldNib@Farpoint", "Hello", "Body", ""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))

    assert "Message sent." in _visible_text(session)
    row = db.connection.execute("SELECT recipient_remote_address FROM mail_messages").fetchone()
    assert row["recipient_remote_address"] == f"OldNib@{remote_identity.fingerprint}"
    lane.close()
    db.close()


def test_compose_asks_again_for_a_malformed_link_address(tmp_path):
    """Issue #807: each malformed address is refused at the To prompt with
    what to type instead, and the prompt asks again."""
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    node_identity = bootstrap_node_identity("roanoke")
    remote_identity = bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(db, node_identity, remote_identity)

    session = FakeSession(
        keys=["c", "b"],
        lines=["Bob Case@Farpoint", "@Farpoint", "bob@", f"{'b' * 33}@Farpoint", ""],
    )
    session.terminal_width = 200
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))

    text = _visible_text(session)
    assert "'Bob Case' is not a user name. Type the name as their BBS shows it: letters, digits" in text
    assert "Type the user's name before the @, like alice@Farpoint." in text
    assert "Type the name of their BBS after the @, like bob@TheirBBS." in text
    assert "is longer than a user name can be" in text
    assert "[a-z0-9_.-]" not in text
    assert text.count("To: ") == 5
    assert "Subject:" not in text
    lane.close()
    db.close()


def test_compose_checks_an_address_changed_from_the_review_screen(tmp_path):
    """Issue #807: Send repeats the To prompt's checks, since [T]o on the
    review screen can change the address after it was checked."""
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    node_identity = bootstrap_node_identity("roanoke")
    remote_identity = bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(db, node_identity, remote_identity)

    for changed, said in (
        ("bob@Nowhere", 'No BBS linked with this one goes by "Nowhere".'),
        ("Bob Case@Farpoint", "'Bob Case' is not a user name."),
    ):
        session = FakeSession(
            keys=["c", "t", "s", "c", "b"], lines=["bob@Farpoint", "Hello", "Body", "/done", changed],
        )
        session.terminal_width = 200
        lane = DatabaseLane(db_path)
        asyncio.run(browse_mail(session, lane, alice, link_context=link_context))
        lane.close()

        text = _visible_text(session)
        assert said in text
        assert "Message sent." not in text
    assert db.connection.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0] == 0
    db.close()


def test_compose_a_one_letter_node_name_is_not_taken_for_a_fingerprint(tmp_path):
    """Issue #807: a node called "Q" stayed unaddressable whenever another
    peer's technical identity happened to start with q."""
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    node_identity = bootstrap_node_identity("roanoke")
    other = bootstrap_node_identity("other")
    short = bootstrap_node_identity("short")
    _link_context_with_known_peer(db, node_identity, other, friendly_name="Other")
    link_context = _link_context_with_known_peer(
        db, node_identity, short, friendly_name=other.fingerprint[0].upper(),
    )

    session = FakeSession(
        keys=["c", "s", "b"], lines=[f"bob@{other.fingerprint[0].upper()}", "Hello", "Body", ""],
    )
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))

    row = db.connection.execute("SELECT recipient_remote_address FROM mail_messages").fetchone()
    assert row["recipient_remote_address"] == f"bob@{short.fingerprint}"
    lane.close()
    db.close()


def test_a_node_name_with_an_at_sign_is_quoted_on_the_from_line_and_can_be_typed_back(tmp_path):
    """Issue #807: `bob@"Cats @ Night · ..."` shows where the user name
    ends, and the quoted address is accepted at the To prompt."""
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    node_identity = bootstrap_node_identity("roanoke")
    remote_identity = bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(
        db, node_identity, remote_identity, friendly_name="Cats @ Night",
    )
    db.connection.execute(
        "INSERT INTO mail_messages (sender_user_id, sender_label, recipient_user_id, subject, body, created_at)"
        " VALUES (NULL, ?, ?, 'Hi', 'Hello there', '2026-01-01T00:00:00+00:00')",
        (f"BobCase@{remote_identity.fingerprint}", alice.id),
    )
    db.connection.commit()
    shown = '"Cats @ Night · farpoint.example.org"'

    session = FakeSession(
        keys=["1", "b", "c", "s", "b"],
        lines=[f"BobCase@{shown}", "Re: Hi", "Back at you", "/done"],
    )
    session.terminal_width = 200
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))

    text = _visible_text(session)
    assert f"BobCase@{shown}" in text
    assert "Message sent." in text
    row = db.connection.execute(
        "SELECT recipient_remote_address FROM mail_messages WHERE recipient_remote_address IS NOT NULL"
    ).fetchone()
    assert row["recipient_remote_address"] == f"BobCase@{remote_identity.fingerprint}"
    lane.close()
    db.close()


def test_compose_without_link_context_treats_an_at_sign_as_an_ordinary_username_lookup(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)

    session = FakeSession(keys=["c", "b"], lines=["bob@somewhere"])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))  # no link_context

    assert "No such user" in _written_text(session)
    lane.close()
    db.close()


def test_compose_an_unmatched_quote_is_shown_as_typed(tmp_path):
    """The refusal names the reference that was looked up: a missing closing
    quote must not read as "no BBS goes by Farpoint"."""
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    node_identity = bootstrap_node_identity("roanoke")
    remote_identity = bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(db, node_identity, remote_identity)

    session = FakeSession(keys=["c", "b"], lines=['bob@"Farpoint', ""])
    session.terminal_width = 200
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))

    assert 'No BBS linked with this one goes by ""Farpoint".' in _visible_text(session)
    lane.close()
    db.close()


# -- replying to Link mail, and Link mail in Sent (issue #805) -----------------


_FARPOINT = "Farpoint · farpoint.example.org"


def _receive_link_mail(db, recipient, sender_address, *, subject="Hi", body="Hello there"):
    """Link mail as `deliver_link_message` stores it: no local sender."""
    db.connection.execute(
        "INSERT INTO mail_messages (sender_user_id, sender_label, recipient_user_id, subject, body, created_at,"
        " link_source_event_id) VALUES (NULL, ?, ?, ?, ?, '2026-01-01T00:00:00+00:00', 'event-1')",
        (sender_address, recipient.id, subject, body),
    )
    db.connection.commit()


def _remote_rows(db):
    return db.connection.execute(
        "SELECT * FROM mail_messages WHERE recipient_remote_address IS NOT NULL"
    ).fetchall()


def test_reply_to_link_mail_goes_back_over_link_with_quote_and_subject(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    node_identity = bootstrap_node_identity("roanoke")
    remote_identity = bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(db, node_identity, remote_identity)
    _receive_link_mail(db, alice, f"bob@{remote_identity.fingerprint}")

    session = FakeSession(
        keys=["1", "r", "s", "b", "b"], lines=["", "Back at you", ""]
    )
    session.terminal_width = 200
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))

    text = _visible_text(session)
    assert "no longer exists" not in text
    assert f"To: bob@{_FARPOINT}" in text
    assert "Message sent." in text
    [row] = _remote_rows(db)
    assert row["recipient_remote_address"] == f"bob@{remote_identity.fingerprint}"
    assert row["subject"] == "Re: Hi"
    assert row["body"].startswith(f"bob@{_FARPOINT} wrote:\n> Hello there\n\nBack at you")
    assert row["link_delivery_status"] == "pending"
    assert row["link_event_content_id"] is not None
    lane.close()
    db.close()


def test_reply_to_link_mail_from_a_peer_now_on_probation_is_refused_as_at_the_to_prompt(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    node_identity = bootstrap_node_identity("roanoke")
    remote_identity = bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(db, node_identity, remote_identity, established=False)
    _receive_link_mail(db, alice, f"bob@{remote_identity.fingerprint}")

    session = FakeSession(keys=["1", "r", "b", "b"])
    session.terminal_width = 200
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))

    text = _visible_text(session)
    assert f"{_FARPOINT} is not linked yet; mail opens once the SysOp establishes it." in text
    assert "Subject:" not in text
    assert "Message sent." not in text
    assert _remote_rows(db) == []
    lane.close()
    db.close()


def test_reply_to_link_mail_refused_when_the_peer_changes_while_it_is_written(tmp_path, monkeypatch):
    """Send checks the reply's address again, as it does a typed one."""
    import netbbs.net.mail_flow as mail_flow

    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    node_identity = bootstrap_node_identity("roanoke")
    remote_identity = bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(db, node_identity, remote_identity)
    _receive_link_mail(db, alice, f"bob@{remote_identity.fingerprint}")

    real_refusal = mail_flow._link_mail_refusal
    calls = []

    def refusal_after_the_first_check(db, fingerprint):
        calls.append(fingerprint)
        return real_refusal(db, fingerprint) if len(calls) == 1 else "Mail to Farpoint is closed on this BBS."

    monkeypatch.setattr(mail_flow, "_link_mail_refusal", refusal_after_the_first_check)
    session = FakeSession(
        keys=["1", "r", "s", "c", "b", "b"], lines=["", "Back at you", ""]
    )
    session.terminal_width = 200
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))

    text = _visible_text(session)
    assert calls == [remote_identity.fingerprint, remote_identity.fingerprint]
    assert "Mail to Farpoint is closed on this BBS." in text
    assert "Message sent." not in text
    assert _remote_rows(db) == []
    lane.close()
    db.close()


def test_reply_to_link_mail_from_a_node_no_longer_linked_says_so(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    node_identity = bootstrap_node_identity("roanoke")
    gone = bootstrap_node_identity("gone")
    link_context = LinkContext(link_node=LinkNode(identity=node_identity))
    _receive_link_mail(db, alice, f"bob@{gone.fingerprint}")

    session = FakeSession(keys=["1", "r", "b", "b"])
    session.terminal_width = 200
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))

    text = _visible_text(session)
    assert "This BBS is no longer linked with the BBS bob@" in text
    assert "writes from, so a reply can't reach it." in text
    assert "no longer exists" not in text
    lane.close()
    db.close()


def test_reply_to_link_mail_with_link_off_says_so(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    remote_identity = bootstrap_node_identity("farpoint")
    _receive_link_mail(db, alice, f"bob@{remote_identity.fingerprint}")

    session = FakeSession(keys=["1", "r", "b", "b"])
    session.terminal_width = 200
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))  # no link_context

    text = _visible_text(session)
    assert "not linked with other BBSes right now, so a reply can't reach bob@" in text
    assert "no longer exists" not in text
    lane.close()
    db.close()


def test_reply_to_link_mail_can_be_readdressed_from_the_review_screen(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    carol = create_user(db, "carol", password="hunter2pw", user_level=10)
    node_identity = bootstrap_node_identity("roanoke")
    remote_identity = bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(db, node_identity, remote_identity)
    _receive_link_mail(db, alice, f"bob@{remote_identity.fingerprint}")

    session = FakeSession(
        keys=["1", "r", "t", "s", "b", "b"], lines=["", "Forwarding", "/done", "carol"]
    )
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))

    assert [m.subject for m in list_inbox(db, carol)] == ["Re: Hi"]
    assert _remote_rows(db) == []
    lane.close()
    db.close()


def test_system_mail_shows_as_system_and_offers_no_reply(tmp_path):
    """Issue #819: mail the BBS sent reads as from "System", says there is
    no one to reply to, and has no Reply key -- R is refused like any key
    the screen does not offer."""
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    send_system_mail(db, bob, "Your post was rejected", "Reason: off topic")

    session = FakeSession(keys=["1", "r", "b", "b"])
    session.node_display_name = "Nib & Quill"
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, bob))

    text = _visible_text(session)
    assert "System" in text.split("Your post was rejected")[0]
    assert "From: System" in text
    assert "A notice from Nib & Quill itself. There is no one to reply to." in text
    assert "[R]eply" not in text and "Reply" not in text.split("From: System")[1]
    assert list_sent(db, bob) == []
    lane.close()
    db.close()


def test_an_account_named_system_is_still_a_person_to_reply_to(tmp_path):
    """The label is not what makes mail the system's: an account a SysOp
    named "System" gets Reply like anyone, and its mail never reads as the
    BBS's notice (issue #819)."""
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    impostor = create_user(db, "System", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    send_mail(db, impostor, bob, "Hello", "body")

    session = FakeSession(keys=["1", "b", "b"])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, bob))

    text = _visible_text(session)
    assert "[R]eply" in text or "R]eply" in text
    assert "There is no one to reply to." not in text
    lane.close()
    db.close()


def test_reply_to_a_deleted_local_sender_still_says_the_account_is_gone(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    send_mail(db, alice, bob, "Hello", "body")
    # What deleting alice's account leaves behind (ON DELETE SET NULL).
    db.connection.execute("UPDATE mail_messages SET sender_user_id = NULL")
    db.connection.commit()
    node_identity = bootstrap_node_identity("roanoke")
    link_context = LinkContext(link_node=LinkNode(identity=node_identity))

    session = FakeSession(keys=["1", "r", "b", "b"])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, bob, link_context=link_context))

    assert "That sender's account no longer exists -- can't reply." in _visible_text(session)
    assert list_sent(db, bob) == []
    lane.close()
    db.close()


def test_sent_shows_the_remote_address_of_link_mail_in_the_list_and_the_view(tmp_path):
    from netbbs.link.mail import compose_link_message

    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    node_identity = bootstrap_node_identity("roanoke")
    remote_identity = bootstrap_node_identity("farpoint")
    _link_context_with_known_peer(db, node_identity, remote_identity, friendly_name="Cats @ Night")
    compose_link_message(
        db, alice, f"Bob@{remote_identity.fingerprint}", "Hello", "Over there", node_identity=node_identity,
    )

    session = FakeSession(keys=["s", "1", "b", "b", "b"])
    session.terminal_width = 200
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    text = _visible_text(session)
    shown = 'Bob@"Cats @ Night · farpoint.example.org"'
    assert re.search(rf"1  {re.escape(shown)} +Hello", text)
    assert f"To: {shown}" in text
    assert "(deleted account)" not in text
    lane.close()
    db.close()


# -- the compose screen (issue #813) ---------------------------------------------


def _screens(session: FakeSession) -> list[str]:
    """Each screen drawn after a clear, visible text only."""
    from netbbs.rendering import clear_screen

    return [_ANSI_ESCAPE_RE.sub("", chunk) for chunk in _written_text(session).split(clear_screen())]


def test_compose_is_a_screen_of_its_own_with_plain_wording(tmp_path):
    from netbbs.net.redraw_preference import set_redraw_in_place_enabled

    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    set_redraw_in_place_enabled(db, alice, True)
    link_context = LinkContext(link_node=LinkNode(identity=bootstrap_node_identity("roanoke")))
    session = FakeSession(keys=["c", "b"], lines=[""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))

    # The To prompt is on a cleared screen under its own title, not under
    # the mail menu.
    screen = next(s for s in _screens(session) if "To: " in s)
    assert "Mail › New message" in screen
    assert "[C]ompose" not in screen
    assert "Type their user name, or name@TheirBBS for someone on a linked BBS." in screen
    assert screen.index("New message") < screen.index("To: ")
    assert "node-name-or-dns" not in _written_text(session)
    lane.close()
    db.close()


def test_compose_esc_at_the_to_prompt_cancels(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    session = FakeSession(keys=["c", "b"], lines=[ESC])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    assert "Cancelled." in _visible_text(session)
    assert "Subject:" not in _visible_text(session)
    lane.close()
    db.close()


def test_compose_review_names_the_account_not_the_text_as_typed(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    alice = create_user(db, "Alice", password="hunter2pw", user_level=10)
    session = FakeSession(keys=["c", "s", "b"], lines=["ALICE", "Hello", "Body", ""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, bob))

    text = _visible_text(session)
    assert "To: Alice" in text
    assert "ALICE" not in text
    assert list_inbox(db, alice)[0].subject == "Hello"
    lane.close()
    db.close()


def test_compose_review_names_a_link_recipient_by_its_nodes_name(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    node_identity = bootstrap_node_identity("roanoke")
    remote_identity = bootstrap_node_identity("farpoint")
    link_context = _link_context_with_known_peer(db, node_identity, remote_identity)
    typed = f"bob@{remote_identity.fingerprint[:8]}"
    session = FakeSession(keys=["c", "s", "b"], lines=[typed, "Hello", "Body", ""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))

    text = _visible_text(session)
    assert "To: bob@Farpoint · farpoint.example.org" in text
    assert "Message sent." in text
    lane.close()
    db.close()


def test_reply_opens_on_a_reply_screen_that_names_the_recipient(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "Alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    send_mail(db, alice, bob, "Hello", "body")
    session = FakeSession(keys=["1", "r", "c", "b", "b"], lines=["", "Reply text", ""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, bob))

    text = _visible_text(session)
    assert "Mail › Reply" in text
    assert text.index("Mail › Reply") < text.index("To: Alice") < text.index("Subject: ")
    lane.close()
    db.close()


def test_review_pages_a_long_letter_and_keeps_to_and_subject_on_every_page(tmp_path):
    from netbbs.net.redraw_preference import set_redraw_in_place_enabled

    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    set_redraw_in_place_enabled(db, alice, True)
    body_lines = [f"item {n}" for n in range(1, 31)]
    session = FakeSession(keys=["c", "n", "n", "s", "b"], lines=["bob", "Shopping", *body_lines, ""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    reviews = [s for s in _screens(session) if "Review composition" in s]
    assert len(reviews) == 3
    for screen in reviews:
        assert "To: bob" in screen
        assert "Subject: Shopping" in screen
        assert "[S]end" in screen
        # The whole screen fits the terminal: FakeSession writes a row per
        # write_line, and the prompt takes the last one.
        assert screen[: screen.index("Choice: ")].count("\n") < session.terminal_height
    assert "Page 1 of" in reviews[0] and "item 1\n" in reviews[0] and "item 30" not in reviews[0]
    assert "Page 2 of" in reviews[1] and "item 1\n" not in reviews[1]
    assert "[N]ext page" in reviews[0]
    # Paged, the menu is the packed bar: a described one would take the
    # body's rows.
    assert "Send this message" not in reviews[0]
    assert list_inbox(db, bob)[0].body.splitlines() == body_lines
    lane.close()
    db.close()


def test_a_short_letter_is_not_paged(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    create_user(db, "bob", password="hunter2pw", user_level=10)
    session = FakeSession(keys=["c", "n", "s", "b"], lines=["bob", "Hi", "Short", ""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    text = _visible_text(session)
    assert "Page 1 of" not in text
    assert "ext page" not in text
    assert "Message sent." in text
    lane.close()
    db.close()


def test_the_fullscreen_editor_shows_what_the_letter_is(tmp_path):
    from netbbs.net.editor_preference import set_fullscreen_editor_enabled
    from netbbs.rendering.terminal_emulator import TerminalEmulator
    from tests.test_login_flow_fullscreen_editor import FakeSession as FullscreenSession
    from tests.test_login_flow_fullscreen_editor import _type

    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    create_user(db, "Bob", password="hunter2pw", user_level=10)
    set_fullscreen_editor_enabled(db, alice, True)
    session = FullscreenSession(["c", "BOB", "Plans"] + _type("See you") + ["CTRL+O", "s", "b"])
    lane = DatabaseLane(db_path)
    screens: list[list[str]] = []
    original_write = session.write

    async def write(text: str) -> None:
        await original_write(text)
        # The editor's first paint: everything since the last clear.
        if "Ctrl+O save" in text and not screens:
            written = "".join(session.written)
            emulator = TerminalEmulator(session.terminal_width, session.terminal_height)
            emulator.feed(written[written.rindex("\x1b[2J"):])
            screens.append([row.rstrip() for row in emulator.text_rows()])

    session.write = write
    asyncio.run(browse_mail(session, lane, alice))

    rows = screens[0]
    assert rows[0] == "New message"
    assert rows[1] == "To: Bob"
    assert rows[2] == "Subject: Plans"
    assert rows[3] and not rows[3].strip("-─")
    assert any("Ctrl+O save" in row for row in rows)
    lane.close()
    db.close()


# -- delivery state of Link mail in Sent, and telling the sender (issue #806) ---


def _sent_link_mail(db, alice, *, subject="Hello"):
    from netbbs.link.mail import compose_link_message

    node_identity = bootstrap_node_identity("roanoke")
    remote_identity = bootstrap_node_identity("farpoint")
    _link_context_with_known_peer(db, node_identity, remote_identity)
    message = compose_link_message(
        db, alice, f"bob@{remote_identity.fingerprint}", subject, "Over there", node_identity=node_identity,
    )
    return message, remote_identity


def _set_delivery(db, message, status, reason=None, notice=0):
    db.connection.execute(
        "UPDATE mail_messages SET link_delivery_status = ?, link_delivery_reason = ?, "
        "link_delivery_notice_pending = ? WHERE link_event_content_id = ?",
        (status, reason, notice, message.content_id),
    )
    db.connection.commit()


@pytest.mark.parametrize(
    ("status", "reason", "tag", "explanation"),
    [
        ("pending", None, "pending", "Delivery: Pending: that BBS has not confirmed it yet."),
        ("delivered", None, "delivered", "Delivery: Delivered to the recipient's mailbox."),
        (
            "bounced", "link_policy_node_quarantined", "bounced",
            "Delivery: Bounced: that BBS has quarantined this BBS.",
        ),
        (
            "expired", None, "expired",
            "Delivery: Expired: no route to that BBS worked before delivery gave up. It was not delivered.",
        ),
    ],
)
def test_sent_shows_the_delivery_state_in_the_list_and_the_view(tmp_path, status, reason, tag, explanation):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    message, _remote = _sent_link_mail(db, alice)
    _set_delivery(db, message, status, reason)

    session = FakeSession(keys=["s", "1", "b", "b", "b"])
    session.terminal_width = 200
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    text = _visible_text(session)
    # A column of its own in Sent's table (issue #810), under "Delivery".
    assert re.search(rf"1  bob@{re.escape(_FARPOINT)} +Hello +{tag} ", text)
    assert re.search(r"#  To +Subject +Delivery +Date", text)
    assert explanation in text
    lane.close()
    db.close()


def test_sent_shows_mail_left_at_a_relay_as_with_a_relay(tmp_path):
    """Issue #874: still pending, but the caller is told the letter sits at
    a relay and when it will give up."""
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    message, _remote = _sent_link_mail(db, alice)
    db.connection.execute(
        "UPDATE mail_messages SET link_relay_handoff_at = '2026-03-01T12:00:00.000000Z' "
        "WHERE link_event_content_id = ?",
        (message.content_id,),
    )
    db.connection.commit()

    session = FakeSession(keys=["s", "1", "b", "b", "b"])
    session.terminal_width = 200
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    text = _visible_text(session)
    assert re.search(rf"1  bob@{re.escape(_FARPOINT)} +Hello +with relay ", text)
    assert (
        "Delivery: With a relay, no answer yet: it was left at a relay for that BBS to collect. "
        "If no answer comes back within 14 days, it expires."
    ) in " ".join(text.split())
    lane.close()
    db.close()


def test_a_relay_timeout_is_told_at_the_next_main_menu(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    message, _remote = _sent_link_mail(db, alice, subject="Lunch")
    _set_delivery(db, message, "expired", "no_answer", notice=1)

    text = " ".join(_run_main_menu(db_path, db, alice).split())

    assert (
        f'Your mail "Lunch" to bob@{_FARPOINT} expired: no answer came back in the 14 days since it was '
        "left at a relay for that BBS, so it may not have arrived."
    ) in text
    db.close()


def test_sent_shows_no_delivery_state_for_local_mail(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    send_mail(db, alice, bob, "Hello", "body")

    session = FakeSession(keys=["s", "1", "b", "b", "b"])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    text = _visible_text(session)
    assert re.search(r"1  bob +Hello", text)
    # No column for mail that has no delivery to follow.
    assert "Delivery" not in text
    lane.close()
    db.close()


def _run_main_menu(db_path, db, user):
    session = FakeSession(keys=["l"], lines=["y"])
    lane = DatabaseLane(db_path)
    try:
        asyncio.run(
            _main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), user, lane=lane)
        )
    finally:
        lane.close()
    return _visible_text(session)


def test_a_bounce_is_told_at_the_next_main_menu_once(tmp_path):
    """The bounce arrives while the sender is away; they are told at their
    next main menu, and not again after that."""
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    message, _remote = _sent_link_mail(db, alice, subject="Lunch")
    _set_delivery(db, message, "bounced", "unknown_recipient", notice=1)

    first = _run_main_menu(db_path, db, alice)
    assert (
        f'Your mail "Lunch" to bob@{_FARPOINT} bounced: there is no user by that name on that BBS.'
        in " ".join(first.split())
    )

    second = _run_main_menu(db_path, db, alice)
    assert "bounced" not in second
    db.close()


def test_opening_a_bounced_message_in_sent_counts_as_being_told(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    message, _remote = _sent_link_mail(db, alice, subject="Lunch")
    _set_delivery(db, message, "bounced", "mailbox_full", notice=1)

    session = FakeSession(keys=["s", "1", "b", "b", "b"])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))
    lane.close()

    assert db.connection.execute("SELECT link_delivery_notice_pending FROM mail_messages").fetchone()[0] == 0
    assert "Your mail" not in _run_main_menu(db_path, db, alice)
    db.close()


# -- a letter's lines and color (issue #809) -------------------------------------


_LETTER = "Hi Bob,\nThe meeting moved to Friday.\n\n- chairs\n- tables\n-- \nAlice\nQ Pen club treasurer"


def _visible_lines(session: FakeSession) -> list[str]:
    return [line.strip() for line in re.split(r"\r?\n", _visible_text(session))]


def _assert_letter_lines_kept(lines: list[str]) -> None:
    for line in ("Hi Bob,", "The meeting moved to Friday.", "- chairs", "- tables", "Alice", "Q Pen club treasurer"):
        assert line in lines, line
    assert not any("Alice Q Pen" in line or "Hi Bob, The" in line for line in lines)


def test_the_reader_keeps_the_authors_line_breaks(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    send_mail(db, alice, bob, "Meeting", _LETTER)

    session = FakeSession(keys=["1", "b", "b"])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, bob))
    lane.close()

    _assert_letter_lines_kept(_visible_lines(session))
    db.close()


def test_sent_view_keeps_the_authors_line_breaks(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    send_mail(db, alice, bob, "Meeting", _LETTER)

    session = FakeSession(keys=["s", "1", "b", "b", "b"])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))
    lane.close()

    _assert_letter_lines_kept(_visible_lines(session))
    db.close()


def test_the_review_screen_keeps_the_lines_and_shows_color_as_the_reader_will(tmp_path):
    from netbbs.rendering.pipe_codes import cga_to_xterm

    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    create_user(db, "bob", password="hunter2pw", user_level=10)
    session = FakeSession(
        keys=["c", "c", "b"], lines=["bob", "Hello", "Hi Bob,", "|12red|07 news", "Alice", ""]
    )
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))
    lane.close()

    review = _written_text(session).split("Review composition", 1)[1]
    lines = [line.strip() for line in re.split(r"\r?\n", _ANSI_ESCAPE_RE.sub("", review))]
    assert "Hi Bob," in lines and "red news" in lines and "Alice" in lines
    assert "|12" not in _ANSI_ESCAPE_RE.sub("", review)
    assert f"\x1b[38;5;{cga_to_xterm(12)}mred" in review
    db.close()


def test_pipe_codes_in_mail_show_as_color(tmp_path):
    from netbbs.rendering.pipe_codes import cga_to_xterm

    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    send_mail(db, alice, bob, "Hello", "|12pipe color|07 and plain")

    session = FakeSession(keys=["1", "b", "b"])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, bob))
    lane.close()

    assert "pipe color and plain" in _visible_text(session)
    assert "|12" not in _visible_text(session)
    assert f"\x1b[38;5;{cga_to_xterm(12)}mpipe color" in _written_text(session)
    db.close()


def test_a_reader_with_post_colors_off_sees_mail_plain(tmp_path):
    from netbbs.net.post_color_preference import set_post_colors_enabled
    from netbbs.rendering.pipe_codes import cga_to_xterm

    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    set_post_colors_enabled(db, bob, False)
    send_mail(db, alice, bob, "Hello", "|12pipe color|07 and plain")

    session = FakeSession(keys=["1", "b", "b"])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, bob))
    lane.close()

    assert "pipe color and plain" in _visible_text(session)
    assert "|12" not in _visible_text(session)
    assert f"\x1b[38;5;{cga_to_xterm(12)}m" not in _written_text(session)
    db.close()


def test_link_mail_goes_through_the_same_filter(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    remote_identity = bootstrap_node_identity("farpoint")
    esc = chr(27)
    hostile = (
        f"{esc}[2J{esc}[1;1HYour account is locked\n"
        f"{esc}]0;evil{chr(7)}|10green|07 text"
    )
    _receive_link_mail(db, alice, f"bob@{remote_identity.fingerprint}", body=hostile)

    session = FakeSession(keys=["1", "b", "b"])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))
    lane.close()

    written = _written_text(session)
    body = written[written.index("Date: "):]
    assert f"{esc}[2J" not in body and f"{esc}[1;1H" not in body
    assert f"{esc}]0;" not in written and "evil" not in written
    assert "[2J" not in _visible_text(session)
    assert "Your account is locked" in _visible_text(session)
    assert "green text" in _visible_text(session)
    db.close()


def test_a_mail_reply_quotes_the_text_without_its_codes(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    esc = chr(27)
    send_mail(db, alice, bob, "Hello", f"|12red|07 line\n{esc}[31mescaped{esc}[0m line")

    session = FakeSession(keys=["1", "r", "s", "b", "b"], lines=["", "Answer", ""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, bob))
    lane.close()

    [sent] = list_sent(db, bob)
    assert "> red line\n> escaped line\n" in sent.body
    assert "|12" not in sent.body and "[31m" not in sent.body
    db.close()


def test_the_line_editor_keeps_pasted_color_in_mail(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    create_user(db, "bob", password="hunter2pw", user_level=10)
    session = FakeSession(keys=["c", "c", "b"], lines=["bob", "Hello", "body", ""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))
    lane.close()

    # To and Subject are not a body; the body's lines are read with it.
    assert not any(session.pasted_color_offered[:2])
    assert session.pasted_color_offered[2:] and all(session.pasted_color_offered[2:])
    db.close()


def test_the_fullscreen_editor_keeps_pasted_color_in_mail(tmp_path):
    from netbbs.net.editor_preference import set_fullscreen_editor_enabled
    from tests.test_login_flow_fullscreen_editor import FakeSession as FullscreenSession
    from tests.test_login_flow_fullscreen_editor import _type

    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    create_user(db, "bob", password="hunter2pw", user_level=10)
    set_fullscreen_editor_enabled(db, alice, True)
    session = FullscreenSession(["c", "bob", "Subject"] + _type("Fullscreen body") + ["CTRL+O", "s", "b"])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))
    lane.close()

    assert session.pasted_color_offered is True
    db.close()
