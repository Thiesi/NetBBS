"""
Tests for netbbs.chat.membership (design doc §8/round 33 points 8/9/11,
Phase 2 Track 5h): persistent channel membership (`channel_members`)
and the invite-then-accept flow (`channel_invitations`), both gated by
`ChannelPermission.MANAGE_MEMBERS` -- library-level, distinct from the
real command wiring covered in tests/test_chat_flow_membership.py.
"""

from __future__ import annotations

import sqlite3

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
    list_pending_invitations_for_user,
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


# -- GitHub issue #28 (reopened): return value + atomicity -----------------


def test_accept_invitation_returns_true_on_success(db, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)
    assert accept_invitation(db, channel, bob) is True


def test_accept_invitation_returns_false_when_nothing_to_accept(db, bob, channel):
    assert accept_invitation(db, channel, bob) is False


class _ProxyConnection:
    """Wraps a real sqlite3.Connection, forwarding everything except
    `execute()`, which runs `on_execute(sql)` first -- lets a test
    intercept one specific statement inside accept_invitation()'s own
    SAVEPOINT scope without needing to modify accept_invitation itself.
    Needed because sqlite3.Connection instances refuse arbitrary
    attribute assignment (`execute` is read-only on the real object),
    so the swap happens at the netbbs.storage.database.Database level
    (`db.connection`), a plain, freely reassignable attribute."""

    def __init__(self, real, on_execute):
        self._real = real
        self._on_execute = on_execute

    def execute(self, sql, *args, **kwargs):
        self._on_execute(sql)
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_accept_invitation_rolls_back_cleanly_if_the_membership_insert_fails(
    db, alice, bob, channel, monkeypatch
):
    """Failure-injection regression test for the reopened issue: the
    invitation-status UPDATE and the channel_members INSERT must be one
    atomic unit on the shared connection. Before the SAVEPOINT fix, a
    failure partway through (simulated here) would leave whichever
    statement already ran sitting uncommitted on the connection --
    silently vulnerable to being persisted by a later, unrelated
    commit() elsewhere, rather than being cleanly discarded."""
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)

    real_connection = db.connection

    def _on_execute(sql: str) -> None:
        if sql.strip().upper().startswith("INSERT INTO CHANNEL_MEMBERS"):
            raise sqlite3.OperationalError("simulated failure")

    monkeypatch.setattr(db, "connection", _ProxyConnection(real_connection, _on_execute))
    try:
        with pytest.raises(sqlite3.OperationalError):
            accept_invitation(db, channel, bob)
    finally:
        monkeypatch.undo()

    # Nothing must have been persisted -- still pending, not silently
    # left "accepted" with no membership row, and no membership row.
    assert has_pending_invitation(db, channel, bob) is True
    assert is_member(db, channel, bob) is False

    # A later, completely unrelated write+commit on this same connection
    # must not resurrect any part of the aborted attempt either -- the
    # rollback-to-savepoint has to have fully discarded it already, not
    # merely left it uncommitted and hoping nothing else commits first.
    carol = create_user(db, "carol", password="hunter2", user_level=10)
    create_invitation(db, channel, carol, invited_by=alice)
    assert has_pending_invitation(db, channel, bob) is True
    assert is_member(db, channel, bob) is False


def test_accept_invitation_raises_if_the_row_stopped_being_pending_before_its_own_update(
    db, alice, bob, channel, monkeypatch
):
    """Direct unit test of the defensive `WHERE status = 'pending'` +
    `rowcount != 1` check on accept_invitation()'s own UPDATE: whatever
    the cause, if the invitation row is no longer 'pending' by the time
    this call reaches its own UPDATE (its earlier SELECT notwithstanding),
    it must raise rather than report success. Simulated deterministically
    here by flipping the row's status directly, on the same connection,
    in the narrow window between accept_invitation()'s SELECT and its own
    UPDATE -- accept_invitation() itself has no `await` points, so on
    this project's single shared, single-threaded event-loop connection
    there is no way for another *in-process* coroutine to genuinely
    interleave here; this test exists to pin the guard's own behavior
    (defense in depth against a changed assumption elsewhere), not to
    model a specific real-world trigger for it."""
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)

    real_connection = db.connection
    injected = {"done": False}

    def _on_execute(sql: str) -> None:
        if not injected["done"] and sql.strip().upper().startswith("INSERT INTO CHANNEL_MEMBERS"):
            injected["done"] = True
            real_connection.execute(
                "UPDATE channel_invitations SET status = 'revoked' "
                "WHERE channel_id = ? AND invited_user_id = ?",
                (channel.id, bob.id),
            )

    monkeypatch.setattr(db, "connection", _ProxyConnection(real_connection, _on_execute))
    try:
        with pytest.raises(MembershipError):
            accept_invitation(db, channel, bob)
    finally:
        monkeypatch.undo()

    # Rolled back cleanly: no membership row, and the injected 'revoked'
    # status itself was inside accept_invitation()'s own SAVEPOINT, so
    # its rollback undid that too -- back to 'pending', as if this call
    # had never run at all.
    assert is_member(db, channel, bob) is False
    assert has_pending_invitation(db, channel, bob) is True


# -- GitHub issue #28: accepting an invitation grants real membership ------


def test_accept_invitation_grants_persistent_membership(db, alice, bob, channel):
    """Regression test for the core bug: accepting used to only flip
    the invitation's own status, never actually inserting a
    channel_members row -- so access silently reverted to "not a
    member" the moment the invited user next left the channel."""
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)
    assert is_member(db, channel, bob) is False  # not yet -- only invited

    accept_invitation(db, channel, bob)

    assert is_member(db, channel, bob) is True


def test_accept_invitation_records_the_inviter_as_granted_by(db, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)
    accept_invitation(db, channel, bob)

    row = db.connection.execute(
        "SELECT granted_by_user_id FROM channel_members WHERE channel_id = ? AND user_id = ?",
        (channel.id, bob.id),
    ).fetchone()
    assert row["granted_by_user_id"] == alice.id


def test_membership_survives_leaving_and_rejoining_after_accepting(db, alice, bob, channel):
    """The exact scenario the bug report described: invite -> first
    join (accept) -> leave -> second join must succeed, since accepted
    membership is supposed to persist until explicitly revoked."""
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)
    accept_invitation(db, channel, bob)  # first "join"

    # Simulates "leaving" -- nothing about leaving a channel touches
    # channel_members at all (that's the point: it's persistent), so
    # this just re-confirms membership is still there afterward with
    # no further action needed.
    assert is_member(db, channel, bob) is True  # second "join" would succeed


def test_accept_invitation_is_a_no_op_when_already_a_direct_member(db, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    add_member(db, channel, bob, granted_by=alice)
    accept_invitation(db, channel, bob)  # no pending invitation -- must not raise
    assert is_member(db, channel, bob) is True


def test_invitations_are_scoped_to_their_own_channel(db, alice, bob, sysop, channel):
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)
    other = create_channel(db, "offtopic", creator=sysop, members_only=True)
    assert has_pending_invitation(db, other, bob) is False


def test_expired_invitation_is_not_pending(db, alice, bob, channel):
    # Forces an already-past expires_at directly rather than relying on
    # create_invitation's own default duration (GitHub issue #28) being
    # short enough to wait out in a test -- same expiry-filtering
    # contract channel_restrictions already has.
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)
    db.connection.execute(
        "UPDATE channel_invitations SET expires_at = '2000-01-01T00:00:00.000000Z' "
        "WHERE channel_id = ? AND invited_user_id = ?",
        (channel.id, bob.id),
    )
    db.connection.commit()
    assert has_pending_invitation(db, channel, bob) is False


def test_create_invitation_sets_a_real_expiry_by_default(db, alice, bob, channel):
    """GitHub issue #28: expires_at used to always be written as NULL
    -- the schema/model's own expiry support was structurally present
    but permanently unused."""
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)

    row = db.connection.execute(
        "SELECT expires_at FROM channel_invitations WHERE channel_id = ? AND invited_user_id = ?",
        (channel.id, bob.id),
    ).fetchone()
    assert row["expires_at"] is not None


def test_expired_invitation_cannot_be_accepted(db, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)
    db.connection.execute(
        "UPDATE channel_invitations SET expires_at = '2000-01-01T00:00:00.000000Z' "
        "WHERE channel_id = ? AND invited_user_id = ?",
        (channel.id, bob.id),
    )
    db.connection.commit()

    accept_invitation(db, channel, bob)

    assert is_member(db, channel, bob) is False


# -- GitHub issue #42: durable, account-wide pending-invitation view ------


def test_list_pending_invitations_for_user_is_empty_by_default(db, bob):
    assert list_pending_invitations_for_user(db, bob) == []


def test_list_pending_invitations_for_user_includes_channel_and_inviter(db, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)

    pending = list_pending_invitations_for_user(db, bob)

    assert len(pending) == 1
    assert pending[0].channel_id == channel.id
    assert pending[0].channel_name == channel.name
    assert pending[0].invited_by_username == "alice"


def test_list_pending_invitations_for_user_spans_every_channel(db, alice, bob, sysop, channel):
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)
    other = create_channel(db, "offtopic", creator=sysop, members_only=True)
    _grant_manage_members(db, sysop, other)
    create_invitation(db, other, bob, invited_by=sysop)

    pending = list_pending_invitations_for_user(db, bob)

    assert {p.channel_name for p in pending} == {"general", "offtopic"}


def test_list_pending_invitations_for_user_only_returns_this_users_own(db, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    carol = create_user(db, "carol", password="hunter2", user_level=10)
    create_invitation(db, channel, carol, invited_by=alice)

    assert list_pending_invitations_for_user(db, bob) == []


def test_list_pending_invitations_for_user_excludes_accepted(db, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)
    accept_invitation(db, channel, bob)

    assert list_pending_invitations_for_user(db, bob) == []


def test_list_pending_invitations_for_user_excludes_revoked(db, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)
    revoke_invitation(db, channel, bob, revoked_by=alice)

    assert list_pending_invitations_for_user(db, bob) == []


def test_list_pending_invitations_for_user_excludes_expired(db, alice, bob, channel):
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)
    db.connection.execute(
        "UPDATE channel_invitations SET expires_at = '2000-01-01T00:00:00.000000Z' "
        "WHERE channel_id = ? AND invited_user_id = ?",
        (channel.id, bob.id),
    )
    db.connection.commit()

    assert list_pending_invitations_for_user(db, bob) == []


def test_list_pending_invitations_for_user_survives_every_session_disconnecting(db, alice, bob, channel):
    """The actual point of GitHub issue #42: unlike the ephemeral,
    session-addressed /msg mailbox, this reflects the durable
    channel_invitations row directly -- there is no session state here
    at all to lose, so "the invitee was never online, or disconnected"
    changes nothing about what this returns."""
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)

    assert len(list_pending_invitations_for_user(db, bob)) == 1
