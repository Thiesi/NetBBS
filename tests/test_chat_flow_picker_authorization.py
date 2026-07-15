"""
End-to-end regression tests for GitHub issue #28, reopened a third
time: `browse_channels()`'s picker path used to hand a selected channel
straight to `_chat_loop()` with no membership/invitation check at all --
only `/join` ever enforced `members_only`/invitation acceptance. These
drive the *real* `_pick_channel`/`pick_item` (not a monkeypatched stand-
in, unlike tests/test_chat_flow_join.py's own browse_channels tests,
which isolate its outer-loop dispatch specifically because pick_item
needs a read_key()-capable session that file's borrowed FakeSession
doesn't implement) so a session here needs both read_key (the picker's
2-digit selection) and read_line (_chat_loop's send_loop) from one
ordered input queue -- same shape tests/test_login_flow_fullscreen_
editor.py's own FakeSession already established for a different
mixed-input scenario (menu keys plus a fullscreen editor).
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.membership import create_invitation, has_pending_invitation, is_member
from netbbs.chat.presence import PresenceRegistry
from netbbs.moderation import ChannelPermission, grant_permissions
from netbbs.net import chat_flow
from netbbs.net.char_input import InputHistory
from netbbs.net.session import Session
from netbbs.storage.database import Database


class FakeSession(Session):
    """One ordered input queue serves both `read_key()` (the picker's
    two-digit selection) and `read_line()` (`_chat_loop`'s send_loop) --
    a scenario can freely mix "01" (a picker choice) with "/quit" (a
    chat command) in one scripted list."""

    def __init__(self, inputs: list[str] | None = None):
        self._inputs = list(inputs or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_key(self, echo: bool = True) -> str:
        if not self._inputs:
            await asyncio.Event().wait()  # blocks forever, like unread real input
            raise AssertionError("unreachable")
        return self._inputs.pop(0)

    async def read_line(self, echo: bool = True, history=None, completer=None) -> str:
        if not self._inputs:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        return self._inputs.pop(0)

    async def read_editor_key(self):
        raise NotImplementedError

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def hub():
    return ChatHub()


@pytest.fixture
def presence():
    return PresenceRegistry()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


def _grant_manage_members(db, user, channel):
    grant_permissions(
        db, user, object_type="channel", object_id=channel.id,
        permissions=ChannelPermission.MANAGE_MEMBERS, granted_by=user,
    )


async def _run(db, hub, presence, user, inputs):
    session = FakeSession(inputs)
    history = InputHistory()
    mailbox = MessageMailbox()
    await asyncio.wait_for(
        chat_flow.browse_channels(session, db, hub, presence, mailbox, history, user), timeout=2
    )
    return session


def test_selecting_a_members_only_channel_without_access_is_refused(db, hub, presence, alice, bob):
    """The core bug: picking a visible members_only channel directly
    from the browse list -- never typing /join -- used to grant entry
    with no check at all."""
    channel = create_channel(db, "vip", creator=alice, members_only=True)

    # "01" selects the (only) channel on the picker's first page; the
    # loop must land back at the picker (never entering chat), and "b"
    # backs all the way out.
    session = asyncio.run(_run(db, hub, presence, bob, ["0", "1", "b"]))

    assert "not authorized" in _written_text(session)
    assert is_member(db, channel, bob) is False


def test_selecting_a_hidden_invited_channel_from_the_picker_accepts_and_enters(db, hub, presence, alice, bob):
    """The invited-user path: selecting the channel directly from the
    picker (not /join) must still atomically accept the invitation,
    create real persistent membership, and actually enter chat."""
    channel = create_channel(db, "secret-club", creator=alice, members_only=True, hidden=True)
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)

    session = asyncio.run(_run(db, hub, presence, bob, ["0", "1", "/quit"]))

    assert "not authorized" not in _written_text(session)
    assert "Joined" in _written_text(session)
    assert is_member(db, channel, bob) is True
    assert has_pending_invitation(db, channel, bob) is False  # consumed, not left dangling


def test_hidden_channel_with_no_invitation_is_invisible_to_an_unrelated_user(db, hub, presence, alice):
    carol = create_user(db, "carol", password="hunter2", user_level=10)
    create_channel(db, "secret-club", creator=alice, members_only=True, hidden=True)

    # Nothing to select -- the picker has no channels at all for carol.
    session = asyncio.run(_run(db, hub, presence, carol, []))

    assert "No chat channels are available" in _written_text(session)


def test_leaving_and_reselecting_via_the_picker_succeeds_from_persistent_membership(db, hub, presence, alice, bob):
    """Matches the original bug report's exact leave-then-rejoin
    scenario, but through the picker end-to-end instead of the
    library-level accept_invitation() call test_channel_membership.py
    already covers."""
    channel = create_channel(db, "vip", creator=alice, members_only=True)
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)

    # First entry via the picker (accepts the invitation), /leave back
    # to the picker, then re-select the same channel again -- must
    # succeed purely from the now-persistent membership, no invitation
    # left to consume the second time.
    session = asyncio.run(_run(db, hub, presence, bob, ["0", "1", "/leave", "0", "1", "/quit"]))

    assert _written_text(session).count("not authorized") == 0
    assert is_member(db, channel, bob) is True


def test_selecting_an_open_channel_still_works_unaffected(db, hub, presence, alice, bob):
    """Regression guard: an ordinary, non-members_only channel must
    still be enterable directly from the picker exactly as before --
    _authorize_channel_entry must not accidentally tighten this path."""
    create_channel(db, "lobby", creator=alice)

    session = asyncio.run(_run(db, hub, presence, bob, ["0", "1", "/quit"]))

    assert "not authorized" not in _written_text(session)
    assert "Joined" in _written_text(session)
