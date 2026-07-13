"""
Tests for netbbs.chat.membership (design doc §8/round 33 points 8/9/11,
Phase 2 Track 5h): persistent channel membership (`channel_members`)
and the invite-then-accept flow (`channel_invitations`), both gated by
`ChannelPermission.MANAGE_MEMBERS` -- library-level, distinct from the
real command wiring covered in tests/test_chat_flow_membership.py.
"""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.membership import (
    MembershipError,
    accept_invitation,
    add_member,
    create_invitation,
    has_pending_invitation,
    is_member,
    list_members,
    remove_member,
    revoke_invitation,
)
from netbbs.moderation import ChannelPermission, grant_permissions
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=100)


@pytest.fixture
def alice(db):
    """A channel membership manager, once granted MANAGE_MEMBERS."""
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


@pytest.fixture
def channel(db, sysop):
    return create_channel(db, "general", creator=sysop, members_only=True)


def _grant_manage_members(db, user, channel):
    grant_permissions(
        db, user, object_type="channel", object_id=channel.id,
        permissions=ChannelPermission.MANAGE_MEMBERS, granted_by=user,
    )


# -- direct membership --------------------------------------------------


def test_not_a_member_by_default(db, bob, channel):
    assert is_member(db, channel, bob) is False


def test_add_member_requires_manage_members(db, alice, bob, channel):
    with pytest.raises(MembershipError):
        add_member(db, channel, bob, granted_by=alice)
    assert is_member(db, channel, bob) is False


def test_add_member_with_permission_grants_access(db, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    add_member(db, channel, bob, granted_by=alice)
    assert is_member(db, channel, bob) is True


def test_add_member_twice_does_not_raise(db, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    add_member(db, channel, bob, granted_by=alice)
    add_member(db, channel, bob, granted_by=alice)  # must not raise
    assert is_member(db, channel, bob) is True


def test_remove_member_requires_manage_members(db, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    add_member(db, channel, bob, granted_by=alice)

    carol = create_user(db, "carol", password="hunter2", user_level=10)
    with pytest.raises(MembershipError):
        remove_member(db, channel, bob, removed_by=carol)
    assert is_member(db, channel, bob) is True


def test_remove_member_with_permission_revokes_access(db, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    add_member(db, channel, bob, granted_by=alice)
    remove_member(db, channel, bob, removed_by=alice)
    assert is_member(db, channel, bob) is False


def test_remove_member_never_added_does_not_raise(db, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    remove_member(db, channel, bob, removed_by=alice)  # must not raise


def test_list_members_returns_every_granted_user(db, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    carol = create_user(db, "carol", password="hunter2", user_level=10)
    add_member(db, channel, bob, granted_by=alice)
    add_member(db, channel, carol, granted_by=alice)
    members = list_members(db, channel)
    assert {u.username for u in members} == {"bob", "carol"}


def test_list_members_empty_by_default(db, channel):
    assert list_members(db, channel) == []


def test_membership_is_scoped_to_its_own_channel(db, alice, bob, sysop, channel):
    _grant_manage_members(db, alice, channel)
    add_member(db, channel, bob, granted_by=alice)
    other = create_channel(db, "offtopic", creator=sysop, members_only=True)
    assert is_member(db, other, bob) is False


# -- invitations ----------------------------------------------------------


def test_no_pending_invitation_by_default(db, bob, channel):
    assert has_pending_invitation(db, channel, bob) is False


def test_create_invitation_requires_manage_members_or_membership_plus_opt_in(db, alice, bob, channel):
    with pytest.raises(MembershipError):
        create_invitation(db, channel, bob, invited_by=alice)
    assert has_pending_invitation(db, channel, bob) is False


def test_create_invitation_with_manage_members_succeeds(db, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)
    assert has_pending_invitation(db, channel, bob) is True


def test_create_invitation_via_member_opt_in(db, alice, bob, channel):
    # alice holds MANAGE_MEMBERS and grants bob plain membership (no
    # grant of his own) -- the channel opts into member-issued
    # invitations (design doc round 33 point 11), so bob, a member with
    # no permission grant at all, can still invite someone else.
    _grant_manage_members(db, alice, channel)
    open_channel = create_channel(
        db, "welcoming", creator=alice, members_only=True, allow_member_invites=True
    )
    _grant_manage_members(db, alice, open_channel)
    add_member(db, open_channel, bob, granted_by=alice)

    carol = create_user(db, "carol", password="hunter2", user_level=10)
    create_invitation(db, open_channel, carol, invited_by=bob)
    assert has_pending_invitation(db, open_channel, carol) is True


def test_create_invitation_via_member_opt_in_requires_actually_being_a_member(db, alice, bob, channel):
    open_channel = create_channel(db, "welcoming", creator=alice, allow_member_invites=True)
    # bob is not a member of open_channel and holds no grant on it.
    with pytest.raises(MembershipError):
        create_invitation(db, open_channel, bob, invited_by=bob)


def test_create_invitation_without_opt_in_is_refused_even_for_a_member(db, alice, bob, channel):
    # channel (the fixture) is members_only but allow_member_invites
    # defaults to False -- membership alone isn't enough.
    _grant_manage_members(db, alice, channel)
    add_member(db, channel, bob, granted_by=alice)

    carol = create_user(db, "carol", password="hunter2", user_level=10)
    with pytest.raises(MembershipError):
        create_invitation(db, channel, carol, invited_by=bob)


def test_revoke_invitation_requires_manage_members(db, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)

    carol = create_user(db, "carol", password="hunter2", user_level=10)
    with pytest.raises(MembershipError):
        revoke_invitation(db, channel, bob, revoked_by=carol)
    assert has_pending_invitation(db, channel, bob) is True


def test_revoke_invitation_clears_pending_status(db, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)
    revoke_invitation(db, channel, bob, revoked_by=alice)
    assert has_pending_invitation(db, channel, bob) is False


def test_revoke_invitation_with_none_pending_raises(db, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    with pytest.raises(MembershipError):
        revoke_invitation(db, channel, bob, revoked_by=alice)


def test_re_inviting_after_revocation_creates_a_new_pending_invitation(db, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)
    revoke_invitation(db, channel, bob, revoked_by=alice)
    assert has_pending_invitation(db, channel, bob) is False
    create_invitation(db, channel, bob, invited_by=alice)
    assert has_pending_invitation(db, channel, bob) is True


def test_accept_invitation_marks_it_accepted_and_it_is_no_longer_pending(db, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)
    accept_invitation(db, channel, bob)
    assert has_pending_invitation(db, channel, bob) is False


def test_accepting_with_no_pending_invitation_does_not_raise(db, bob, channel):
    accept_invitation(db, channel, bob)  # must not raise


def test_invitations_are_scoped_to_their_own_channel(db, alice, bob, sysop, channel):
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)
    other = create_channel(db, "offtopic", creator=sysop, members_only=True)
    assert has_pending_invitation(db, other, bob) is False


def test_expired_invitation_is_not_pending(db, alice, bob, channel):
    # Nothing in the command surface sets expires_at yet (no /invite
    # duration argument), but the schema/query must still honor it if
    # ever set directly -- same expiry-filtering contract
    # channel_restrictions already has, exercised directly at the row
    # level here.
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)
    db.connection.execute(
        "UPDATE channel_invitations SET expires_at = '2000-01-01T00:00:00.000000Z' "
        "WHERE channel_id = ? AND invited_user_id = ?",
        (channel.id, bob.id),
    )
    db.connection.commit()
    assert has_pending_invitation(db, channel, bob) is False
