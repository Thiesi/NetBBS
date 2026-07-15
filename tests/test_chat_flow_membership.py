"""
Tests for Phase 2 Track 5h (design doc §8/round 33 points 8/9/11):
`/invite`, `/uninvite`, `/grantaccess`, `/revokeaccess`, `/members`
through the real `_chat_loop` dispatcher, plus `/join` consuming a
pending invitation and marking it accepted, and a rejected `/join`
against a `members_only` channel with no invitation. Library-level
membership behavior is covered separately in
tests/test_channel_membership.py.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.membership import MembershipError, add_member, has_pending_invitation, is_member
from netbbs.chat.presence import PresenceRegistry
from netbbs.moderation import ChannelPermission, grant_permissions
from netbbs.moderation.log import list_actions_for_object
from netbbs.net import chat_flow
from netbbs.net.char_input import InputHistory
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.storage.database import Database
from tests.test_chat_flow_moderation import FakeSession


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
def mailbox():
    return MessageMailbox()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


@pytest.fixture
def channel(db, alice):
    return create_channel(db, "lobby", creator=alice, members_only=True)


def _grant_manage_members(db, user, channel):
    grant_permissions(
        db, user, object_type="channel", object_id=channel.id,
        permissions=ChannelPermission.MANAGE_MEMBERS, granted_by=user,
    )


def _written(session: FakeSession) -> str:
    return "\n".join(session.written)


async def _run(db, hub, presence, mailbox, channel, user, lines, *, session_registry=None):
    session = FakeSession(lines)
    history = InputHistory()
    action = await asyncio.wait_for(
        chat_flow._chat_loop(
            session, db, hub, presence, mailbox, history, channel, user, session_registry=session_registry
        ),
        timeout=2,
    )
    return session, action


# -- /invite --------------------------------------------------------------


def test_invite_without_permission_is_refused(db, hub, presence, mailbox, alice, bob, channel):
    session, _ = asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/invite bob", "/quit"]))
    assert "do not have permission" in _written(session)
    assert has_pending_invitation(db, channel, bob) is False


def test_invite_with_manage_members_creates_a_pending_invitation(db, hub, presence, mailbox, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    session, _ = asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/invite bob", "/quit"]))
    assert "Invited bob." in _written(session)
    assert has_pending_invitation(db, channel, bob) is True


def test_invite_with_no_argument_shows_usage(db, hub, presence, mailbox, alice, channel):
    _grant_manage_members(db, alice, channel)
    session, _ = asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/invite", "/quit"]))
    assert "Usage: /invite" in _written(session)


def test_invite_unknown_user_shows_friendly_message(db, hub, presence, mailbox, alice, channel):
    _grant_manage_members(db, alice, channel)
    session, _ = asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/invite nosuchuser", "/quit"]))
    assert "No such user" in _written(session)


# -- GitHub issue #42: offline invitees are no longer silently unnotified --


def test_invite_to_an_offline_user_does_not_claim_a_live_notification_was_sent(
    db, hub, presence, mailbox, alice, bob, channel
):
    """bob has no active session at all -- /invite must still create
    the durable invitation (the actual, now-authoritative notification
    mechanism), but must not tell alice "(sent to bob)" when nothing
    was actually delivered live. Before this fix, _deliver_private_
    message was called unconditionally and always printed that,
    regardless of whether bob had any reachable session."""
    _grant_manage_members(db, alice, channel)
    session, _ = asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/invite bob", "/quit"]))
    assert "Invited bob." in _written(session)
    assert "sent to" not in _written(session)
    assert has_pending_invitation(db, channel, bob) is True


def test_invite_to_an_online_user_still_reports_a_live_notification(
    db, hub, presence, mailbox, alice, bob, channel
):
    _grant_manage_members(db, alice, channel)
    presence.enter("bob")
    session, _ = asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/invite bob", "/quit"]))
    assert "Invited bob." in _written(session)
    assert "sent to bob" in _written(session)


def test_invite_notifies_the_invitee_via_mailbox(db, hub, presence, mailbox, alice, bob, channel):
    """bob has an active session elsewhere (not this channel) -- the
    exact scenario the mailbox-plus-next-prompt mechanism exists for.
    `presence.enter` mirrors what a real login
    (`netbbs.net.login_flow.run_authenticated_session`) always does
    alongside registering the session -- GitHub issue #42's fix gates
    live delivery on `presence.is_online`, the same check `/msg`
    already used, so this needs to reflect a genuinely-online bob, not
    just a session-registry entry with no matching presence state (a
    combination that can't happen via any real login path)."""
    _grant_manage_members(db, alice, channel)
    registry = ActiveSessionRegistry()
    bob_session = FakeSession()

    async def scenario():
        registry.enter(bob_session)  # requires a running event loop
        registry.mark_authenticated(bob_session, "bob")
        presence.enter("bob")
        return await _run(
            db, hub, presence, mailbox, channel, alice, ["/invite bob", "/quit"], session_registry=registry
        )

    asyncio.run(scenario())
    pending = mailbox.flush(bob_session)
    assert len(pending) == 1
    assert "invited to #lobby" in pending[0][0]


def test_invite_via_member_opt_in_without_manage_members_succeeds(db, hub, presence, mailbox, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    open_channel = create_channel(
        db, "welcoming", creator=alice, members_only=True, allow_member_invites=True
    )
    _grant_manage_members(db, alice, open_channel)
    add_member(db, open_channel, bob, granted_by=alice)

    carol = create_user(db, "carol", password="hunter2", user_level=10)
    session, _ = asyncio.run(_run(db, hub, presence, mailbox, open_channel, bob, ["/invite carol", "/quit"]))
    assert "Invited carol." in _written(session)
    assert has_pending_invitation(db, open_channel, carol) is True


# -- /uninvite --------------------------------------------------------------


def test_uninvite_without_permission_is_refused(db, hub, presence, mailbox, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/invite bob", "/quit"]))

    carol = create_user(db, "carol", password="hunter2", user_level=10)
    session, _ = asyncio.run(_run(db, hub, presence, mailbox, channel, carol, ["/uninvite bob", "/quit"]))
    assert "do not have permission" in _written(session) or "no pending invitation" in _written(session)
    assert has_pending_invitation(db, channel, bob) is True


def test_uninvite_revokes_a_pending_invitation(db, hub, presence, mailbox, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/invite bob", "/quit"]))
    session, _ = asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/uninvite bob", "/quit"]))
    assert "revoked" in _written(session)
    assert has_pending_invitation(db, channel, bob) is False


def test_uninvite_with_none_pending_shows_friendly_message(db, hub, presence, mailbox, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    session, _ = asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/uninvite bob", "/quit"]))
    assert "no pending invitation" in _written(session)


# -- /grantaccess / /revokeaccess -------------------------------------------


def test_grantaccess_without_permission_is_refused(db, hub, presence, mailbox, alice, bob, channel):
    session, _ = asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/grantaccess bob", "/quit"]))
    assert "do not have permission" in _written(session)
    assert is_member(db, channel, bob) is False


def test_grantaccess_with_permission_grants_membership(db, hub, presence, mailbox, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    session, _ = asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/grantaccess bob", "/quit"]))
    assert "Granted bob access" in _written(session)
    assert is_member(db, channel, bob) is True


def test_revokeaccess_without_permission_is_refused(db, hub, presence, mailbox, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    add_member(db, channel, bob, granted_by=alice)

    carol = create_user(db, "carol", password="hunter2", user_level=10)
    session, _ = asyncio.run(_run(db, hub, presence, mailbox, channel, carol, ["/revokeaccess bob", "/quit"]))
    assert "do not have permission" in _written(session)
    assert is_member(db, channel, bob) is True


def test_revokeaccess_with_permission_revokes_membership(db, hub, presence, mailbox, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    add_member(db, channel, bob, granted_by=alice)
    session, _ = asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/revokeaccess bob", "/quit"]))
    assert "Revoked bob" in _written(session)
    assert is_member(db, channel, bob) is False


def test_grantaccess_and_revokeaccess_are_logged_in_the_moderation_log(db, hub, presence, mailbox, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/grantaccess bob", "/quit"]))
    asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/revokeaccess bob", "/quit"]))
    actions = {entry.action for entry in list_actions_for_object(db, "channel", channel.id)}
    assert "grantaccess" in actions
    assert "revokeaccess" in actions


# -- /members --------------------------------------------------------------


def test_members_with_none_granted_shows_friendly_message(db, hub, presence, mailbox, alice, channel):
    session, _ = asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/members", "/quit"]))
    assert "No members" in _written(session)


def test_members_lists_every_granted_user(db, hub, presence, mailbox, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    add_member(db, channel, bob, granted_by=alice)
    session, _ = asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/members", "/quit"]))
    output = _written(session)
    assert "bob" in output


def test_members_needs_no_special_permission_to_view(db, hub, presence, mailbox, alice, bob, channel):
    # Viewable by anyone already in the channel -- not gated the way
    # /grantaccess etc. are.
    _grant_manage_members(db, alice, channel)
    add_member(db, channel, bob, granted_by=alice)
    session, _ = asyncio.run(_run(db, hub, presence, mailbox, channel, bob, ["/members", "/quit"]))
    assert "do not have permission" not in _written(session)


# -- /join against a members_only channel ------------------------------


def test_join_members_only_channel_without_access_is_refused(db, hub, presence, mailbox, alice, bob, channel):
    other = create_channel(db, "offtopic", creator=alice)
    session, action = asyncio.run(
        _run(db, hub, presence, mailbox, other, bob, [f"/join {channel.name}", "/quit"])
    )
    assert "not authorized" in _written(session)
    assert isinstance(action, chat_flow._Quit)


def test_join_members_only_channel_as_a_direct_member_succeeds(db, hub, presence, mailbox, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    add_member(db, channel, bob, granted_by=alice)
    other = create_channel(db, "offtopic", creator=alice)
    _, action = asyncio.run(_run(db, hub, presence, mailbox, other, bob, [f"/join {channel.name}"]))
    assert isinstance(action, chat_flow._SwitchTo)
    assert action.channel.id == channel.id


def test_join_members_only_channel_via_pending_invitation_succeeds(db, hub, presence, mailbox, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/invite bob", "/quit"]))

    other = create_channel(db, "offtopic", creator=alice)
    _, action = asyncio.run(_run(db, hub, presence, mailbox, other, bob, [f"/join {channel.name}"]))
    assert isinstance(action, chat_flow._SwitchTo)


def test_joining_via_invitation_marks_it_accepted(db, hub, presence, mailbox, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/invite bob", "/quit"]))

    other = create_channel(db, "offtopic", creator=alice)
    asyncio.run(_run(db, hub, presence, mailbox, other, bob, [f"/join {channel.name}"]))
    # No longer pending -- it was consumed, not left dangling.
    assert has_pending_invitation(db, channel, bob) is False


def test_join_shows_a_friendly_message_when_the_invitation_stopped_being_valid(
    db, hub, presence, mailbox, alice, bob, channel, monkeypatch
):
    """GitHub issue #28 (reopened): /join used to call accept_invitation()
    purely as a side effect after its own separate has_pending_invitation()
    check, ignoring the result -- so an invitation that stopped being
    valid between the two checks (revoked, expired, or already consumed)
    still let the join through anyway. accept_invitation() is now the
    authoritative check; simulated here via its plain "nothing to
    accept" outcome, a False return."""
    _grant_manage_members(db, alice, channel)
    asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/invite bob", "/quit"]))

    monkeypatch.setattr(chat_flow, "accept_invitation", lambda *a, **kw: False)

    other = create_channel(db, "offtopic", creator=alice)
    session, action = asyncio.run(
        _run(db, hub, presence, mailbox, other, bob, [f"/join {channel.name}", "/quit"])
    )
    assert "no longer valid" in _written(session)
    assert not isinstance(action, chat_flow._SwitchTo)


def test_join_shows_a_friendly_message_when_accept_invitation_raises(
    db, hub, presence, mailbox, alice, bob, channel, monkeypatch
):
    """The narrower race variant: accept_invitation() can also report
    "nothing to accept" by raising MembershipError instead of returning
    False (its own defensive rowcount check finding the invitation row
    already changed underneath it) -- /join must treat that identically
    to a False return, not let it escape as an unhandled exception and
    crash the session."""
    _grant_manage_members(db, alice, channel)
    asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/invite bob", "/quit"]))

    def _raise(*args, **kwargs):
        raise MembershipError("invitation was no longer pending")

    monkeypatch.setattr(chat_flow, "accept_invitation", _raise)

    other = create_channel(db, "offtopic", creator=alice)
    session, action = asyncio.run(
        _run(db, hub, presence, mailbox, other, bob, [f"/join {channel.name}", "/quit"])
    )
    assert "no longer valid" in _written(session)
    assert not isinstance(action, chat_flow._SwitchTo)


def test_open_channel_join_is_unaffected_by_members_only_logic(db, hub, presence, mailbox, alice, bob, channel):
    # A plain (non-members_only) channel needs no membership/invitation
    # at all -- regression guard against the new eligibility check
    # accidentally tightening ordinary /join behavior.
    other = create_channel(db, "offtopic", creator=alice)
    _, action = asyncio.run(_run(db, hub, presence, mailbox, channel, bob, [f"/join {other.name}"]))
    assert isinstance(action, chat_flow._SwitchTo)


def test_joining_via_invitation_grants_membership_that_survives_leaving(db, hub, presence, mailbox, alice, bob, channel):
    """End-to-end regression test for GitHub issue #28's core bug,
    driven through the real /invite + /join commands rather than the
    library functions directly: accepting an invitation via /join used
    to only mark the invitation accepted, never actually granting
    persistent membership -- so a second /join after leaving was
    refused."""
    _grant_manage_members(db, alice, channel)
    asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/invite bob", "/quit"]))

    other = create_channel(db, "offtopic", creator=alice)
    asyncio.run(_run(db, hub, presence, mailbox, other, bob, [f"/join {channel.name}"]))  # first join

    assert is_member(db, channel, bob) is True  # real, persistent membership now exists

    # A second /join (simulating having left and come back) must
    # succeed purely on that persistent membership -- no invitation is
    # left pending to fall back on this time.
    _, action = asyncio.run(_run(db, hub, presence, mailbox, other, bob, [f"/join {channel.name}"]))
    assert isinstance(action, chat_flow._SwitchTo)


# -- /revokeaccess ejects a live session in a members_only channel ---------


def test_revokeaccess_ejects_a_present_target_from_a_members_only_channel(db, hub, presence, mailbox, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    add_member(db, channel, bob, granted_by=alice)

    async def scenario():
        history = InputHistory()
        target_session = FakeSession()  # never types anything
        target_task = asyncio.create_task(
            chat_flow._chat_loop(target_session, db, hub, presence, mailbox, history, channel, bob)
        )
        await asyncio.sleep(0)  # let bob actually join before revoking

        mod_session = FakeSession(["/revokeaccess bob", "/quit"])
        await asyncio.wait_for(
            chat_flow._chat_loop(mod_session, db, hub, presence, mailbox, history, channel, alice), timeout=2
        )
        await asyncio.wait_for(target_task, timeout=2)
        return target_session

    target_session = asyncio.run(scenario())
    assert "removed" in _written(target_session)
    assert hub.participant_count(channel.name) == 0


def test_revokeaccess_does_not_eject_from_an_open_channel(db, hub, presence, mailbox, alice, bob):
    """Only a members_only channel's revocation is access-restriction-
    meaningful enough to force an immediate eject (GitHub issue #28) --
    an open channel's membership grant is a lesser, purely persistent-
    access concept, and /kick remains the general "remove someone
    right now" action for that case."""
    open_channel = create_channel(db, "open-lobby", creator=alice)
    _grant_manage_members(db, alice, open_channel)
    add_member(db, open_channel, bob, granted_by=alice)

    async def scenario():
        history = InputHistory()
        target_session = FakeSession(["/quit"])
        target_task = asyncio.create_task(
            chat_flow._chat_loop(target_session, db, hub, presence, mailbox, history, open_channel, bob)
        )
        await asyncio.sleep(0)

        mod_session = FakeSession(["/revokeaccess bob", "/quit"])
        await asyncio.wait_for(
            chat_flow._chat_loop(mod_session, db, hub, presence, mailbox, history, open_channel, alice), timeout=2
        )
        await asyncio.wait_for(target_task, timeout=2)
        return target_session

    target_session = asyncio.run(scenario())
    assert "removed" not in _written(target_session)
