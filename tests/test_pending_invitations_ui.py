"""
UI-level regression tests for GitHub issue #42: an offline invitee
previously had no notification mechanism at all for a channel invitation
(the mailbox `_deliver_private_message` uses is session-addressed and
ephemeral -- see its own docstring -- so it silently reached nobody with
no active session at `/invite` time). These drive the real
`netbbs.net.login_flow` entry points -- `run_authenticated_session`'s
post-login announcement, `_draw_main_menu`'s conditional `[I]nvitations`
option, and `_show_pending_invitations`'s full-detail screen -- rather
than just the underlying `netbbs.chat.membership.
list_pending_invitations_for_user` (covered separately, at the library
level, in tests/test_channel_membership.py).
"""

from __future__ import annotations

import asyncio

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.membership import create_invitation
from netbbs.chat.presence import PresenceRegistry
from netbbs.moderation import ChannelPermission, grant_permissions
from netbbs.net.char_input import InputHistory
from netbbs.net.login_flow import _announce_pending_invitations, _main_menu, _show_pending_invitations, run_authenticated_session
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


def _grant_manage_members(db, user, channel):
    grant_permissions(
        db, user, object_type="channel", object_id=channel.id,
        permissions=ChannelPermission.MANAGE_MEMBERS, granted_by=user,
    )


# -- _announce_pending_invitations (post-login, one-time) -------------------


def test_announce_says_nothing_with_no_pending_invitations(tmp_path):
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    session = FakeSession()

    asyncio.run(_announce_pending_invitations(session, db, bob))

    assert _written_text(session) == ""
    db.close()


def test_announce_reports_a_single_pending_invitation(tmp_path):
    db = Database(tmp_path / "node.db")
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    channel = create_channel(db, "lobby", creator=alice, members_only=True)
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)
    session = FakeSession()

    asyncio.run(_announce_pending_invitations(session, db, bob))

    text = _written_text(session)
    assert "1 pending channel invitation." in text  # singular, no trailing 's'
    assert "[I]nvitations" in text
    db.close()


def test_announce_reports_multiple_pending_invitations_with_correct_plural(tmp_path):
    db = Database(tmp_path / "node.db")
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    first = create_channel(db, "lobby", creator=alice, members_only=True)
    second = create_channel(db, "vip", creator=alice, members_only=True)
    for ch in (first, second):
        _grant_manage_members(db, alice, ch)
        create_invitation(db, ch, bob, invited_by=alice)
    session = FakeSession()

    asyncio.run(_announce_pending_invitations(session, db, bob))

    assert "2 pending channel invitations." in _written_text(session)
    db.close()


# -- _show_pending_invitations (on-demand, full detail) ----------------------


def test_show_pending_invitations_with_none_pending(tmp_path):
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    session = FakeSession()

    asyncio.run(_show_pending_invitations(session, db, bob))

    assert "no pending channel invitations" in _written_text(session)
    db.close()


def test_show_pending_invitations_lists_channel_and_inviter(tmp_path):
    db = Database(tmp_path / "node.db")
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    channel = create_channel(db, "secret-club", creator=alice, members_only=True, hidden=True)
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)
    session = FakeSession()

    asyncio.run(_show_pending_invitations(session, db, bob))

    text = _written_text(session)
    assert "secret-club" in text
    assert "alice" in text
    assert "/join" in text  # points the invitee at the real acceptance mechanism


# -- [I]nvitations main-menu gating ------------------------------------------


def test_main_menu_hides_invitations_option_with_none_pending(tmp_path):
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    session = FakeSession(keys=["l"])  # logoff immediately

    asyncio.run(
        _main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), bob)
    )

    # "nvitations", not the bracketed "[I]nvitations" -- menu_key() ANSI-
    # colors just the letter, same convention test_board_pagination_ui.py
    # uses for "[O]lder"/"[N]ewer" ("lder"/"ewer").
    assert "nvitations" not in _written_text(session)
    db.close()


def test_main_menu_shows_invitations_option_when_pending(tmp_path):
    db = Database(tmp_path / "node.db")
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    channel = create_channel(db, "lobby", creator=alice, members_only=True)
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)
    session = FakeSession(keys=["l"])

    asyncio.run(
        _main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), bob)
    )

    assert "nvitations" in _written_text(session)
    db.close()


def test_main_menu_i_key_shows_the_pending_invitation_screen(tmp_path):
    db = Database(tmp_path / "node.db")
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    channel = create_channel(db, "lobby", creator=alice, members_only=True)
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)
    session = FakeSession(keys=["i", "l"])

    asyncio.run(
        _main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), bob)
    )

    assert "Pending invitations:" in _written_text(session)
    assert "lobby" in _written_text(session)
    db.close()


def test_main_menu_i_key_is_rejected_with_none_pending(tmp_path):
    """[I]nvitations isn't offered, and pressing 'i' anyway must be
    rejected the same way any other unrecognized key is -- not silently
    open a screen the menu itself doesn't advertise."""
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    session = FakeSession(keys=["i", "l"])

    asyncio.run(
        _main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), bob)
    )

    assert "Pending invitations:" not in _written_text(session)
    db.close()


# -- wired at the real login boundary (run_authenticated_session) -----------


def test_offline_invitee_sees_the_notice_and_menu_option_on_next_login(tmp_path):
    """The end-to-end acceptance scenario from the issue itself: invite
    an offline user, then log them in -- they see the pending
    invitation and channel name, with no session/mailbox state
    involved at any point."""
    db = Database(tmp_path / "node.db")
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    channel = create_channel(db, "lobby", creator=alice, members_only=True)
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)  # bob is offline the whole time

    session = FakeSession(keys=["i", "l"])

    asyncio.run(
        run_authenticated_session(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), bob)
    )

    text = _written_text(session)
    assert "pending channel invitation" in text
    assert "Pending invitations:" in text
    assert "lobby" in text
    db.close()
