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

from netbbs.auth.users import create_user
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.link.boards import LinkContext
from netbbs.link.events import build_endpoint_descriptor
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import LinkNode, PeerRecord
from netbbs.link.store import save_peer
from netbbs.mail import list_inbox, list_sent, send_mail
from netbbs.net.char_input import InputHistory
from netbbs.net.main_menu import _main_menu
from netbbs.net.mail_flow import browse_mail
from netbbs.rendering import (
    ACCENT_COLOR,
    ERROR_COLOR,
    LABEL_COLOR,
    METADATA_COLOR,
    SUCCESS_COLOR,
    VALUE_COLOR,
    colored,
)
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


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
    assert "[NEW] Hello" in text  # explicit unread marker on the inbox listing
    assert "1 unread message" in text
    assert "How are you?" in text
    assert "NetBBS › Mail › Inbox › Hello" in _visible_text(session)
    assert colored("From: ", fg_color=LABEL_COLOR) in text
    assert colored("alice", fg_color=ACCENT_COLOR) in text
    assert colored("Date: ", fg_color=LABEL_COLOR) in text
    assert f"\x1b[38;5;{METADATA_COLOR}m" in text
    assert colored("How are you?", fg_color=VALUE_COLOR) in text
    assert list_inbox(db, bob)[0].is_read is True
    lane.close()
    db.close()


def test_inbox_delete_removes_message(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    send_mail(db, alice, bob, "Hello", "body")

    session = FakeSession(keys=["i", "0", "1", "d", "b", "b"], lines=["y"])
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
    session = FakeSession(keys=["i", "0", "1", "d", "b", "b", "b"], lines=[""])
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
        keys=["i", "0", "1", "r", "s", "b", "b", "b"],
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

    session = FakeSession(keys=["s", "0", "1", "d", "b", "b"], lines=["y"])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice))

    text = _written_text(session)
    assert "to bob" in text
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
            "bob", "Original subject", "first", "second", "",
            "carol", "Revised subject", "/edit 1", "FIRST", "/delete 2", "",
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
        lines=["bob", "Recoverable", "draft body", "", "carol"],
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


def _link_context_with_known_peer(db, node_identity, peer_identity):
    descriptor = build_endpoint_descriptor(
        signing_identity=peer_identity.signing_key,
        subject_fingerprint=peer_identity.fingerprint,
        addresses=None,
        outgoing_only=True,
        created_at="2026-01-01T00:00:00+00:00",
        friendly_name="Farpoint",
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
    return LinkContext(node_identity=node_identity, link_node=LinkNode(identity=node_identity))


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


def test_compose_prompt_mentions_link_address_option_when_link_context_given(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    node_identity = bootstrap_node_identity("roanoke")
    link_context = LinkContext(node_identity=node_identity, link_node=LinkNode(identity=node_identity))

    session = FakeSession(keys=["c", "b"], lines=[""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))

    assert "node-name-or-dns" in _written_text(session)
    lane.close()
    db.close()


def test_compose_rejects_a_link_address_for_a_node_never_seen(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    node_identity = bootstrap_node_identity("roanoke")
    link_context = LinkContext(node_identity=node_identity, link_node=LinkNode(identity=node_identity))

    session = FakeSession(keys=["c", "s", "c", "b"], lines=["bob@neverseenfingerprint", "Hello", "World", ""])
    lane = DatabaseLane(db_path)
    asyncio.run(browse_mail(session, lane, alice, link_context=link_context))

    assert "Could not send" in _written_text(session)
    assert db.connection.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0] == 0
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
