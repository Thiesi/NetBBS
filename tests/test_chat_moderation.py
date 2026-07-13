"""Tests for netbbs.chat.moderation — mute/ban/kick (design doc §13,
sign-off round 37), gated by ChannelPermission.MODERATE."""

from __future__ import annotations

import datetime

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.moderation import (
    ChatModerationError,
    DurationError,
    ban_user,
    is_banned,
    is_muted,
    kick_user,
    list_channel_restrictions,
    mute_user,
    parse_duration,
    unban_user,
    unmute_user,
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
    """A channel moderator, once granted MODERATE."""
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


@pytest.fixture
def channel(db, sysop):
    return create_channel(db, "general", creator=sysop)


def _grant_moderate(db, user, channel):
    grant_permissions(
        db, user, object_type="channel", object_id=channel.id,
        permissions=ChannelPermission.MODERATE, granted_by=user,
    )


# -- parse_duration -----------------------------------------------------


def test_parse_duration_none_or_empty_is_indefinite():
    assert parse_duration(None) is None
    assert parse_duration("") is None


def test_parse_duration_bare_number_is_minutes():
    assert parse_duration("30") == datetime.timedelta(minutes=30)


def test_parse_duration_suffixes():
    assert parse_duration("45s") == datetime.timedelta(seconds=45)
    assert parse_duration("10m") == datetime.timedelta(minutes=10)
    assert parse_duration("2h") == datetime.timedelta(hours=2)
    assert parse_duration("1d") == datetime.timedelta(days=1)
    assert parse_duration("2w") == datetime.timedelta(weeks=2)
    assert parse_duration("1y") == datetime.timedelta(days=365)


def test_parse_duration_rejects_unknown_unit():
    with pytest.raises(DurationError):
        parse_duration("10x")


def test_parse_duration_rejects_non_numeric():
    with pytest.raises(DurationError):
        parse_duration("abc")


def test_parse_duration_rejects_zero_or_negative():
    with pytest.raises(DurationError):
        parse_duration("0")
    with pytest.raises(DurationError):
        parse_duration("-5m")


# -- permission gating ----------------------------------------------------


def test_mute_requires_moderate_permission(db, bob, alice, channel):
    with pytest.raises(ChatModerationError):
        mute_user(db, channel, bob, duration=None, reason=None, muted_by=alice)


def test_ban_requires_moderate_permission(db, bob, alice, channel):
    with pytest.raises(ChatModerationError):
        ban_user(db, channel, bob, duration=None, reason=None, banned_by=alice)


def test_kick_requires_moderate_permission(db, bob, alice, channel):
    with pytest.raises(ChatModerationError):
        kick_user(db, channel, bob, reason=None, kicked_by=alice)


def test_unmute_requires_moderate_permission(db, bob, alice, channel):
    with pytest.raises(ChatModerationError):
        unmute_user(db, channel, bob, unmuted_by=alice)


def test_unban_requires_moderate_permission(db, bob, alice, channel):
    with pytest.raises(ChatModerationError):
        unban_user(db, channel, bob, unbanned_by=alice)


# -- mute -----------------------------------------------------------------


def test_mute_user_creates_indefinite_restriction(db, alice, bob, channel):
    _grant_moderate(db, alice, channel)
    restriction = mute_user(db, channel, bob, duration=None, reason="spamming", muted_by=alice)
    assert restriction.kind == "mute"
    assert restriction.expires_at is None
    assert restriction.reason == "spamming"


def test_mute_user_with_duration_sets_expires_at(db, alice, bob, channel):
    _grant_moderate(db, alice, channel)
    restriction = mute_user(db, channel, bob, duration=datetime.timedelta(minutes=10), reason=None, muted_by=alice)
    assert restriction.expires_at is not None


def test_is_muted_true_for_active_mute(db, alice, bob, channel):
    _grant_moderate(db, alice, channel)
    mute_user(db, channel, bob, duration=None, reason=None, muted_by=alice)
    assert is_muted(db, channel, bob) is not None


def test_is_muted_false_when_never_muted(db, bob, channel):
    assert is_muted(db, channel, bob) is None


def test_is_muted_false_after_expiry(db, alice, bob, channel):
    _grant_moderate(db, alice, channel)
    mute_user(db, channel, bob, duration=datetime.timedelta(minutes=10), reason=None, muted_by=alice)
    # Backdate expires_at into the past to simulate elapsed time.
    db.connection.execute(
        "UPDATE channel_restrictions SET expires_at = '2020-01-01T00:00:00.000000Z' "
        "WHERE channel_id = ? AND user_id = ? AND kind = 'mute'",
        (channel.id, bob.id),
    )
    db.connection.commit()
    assert is_muted(db, channel, bob) is None


def test_remuting_replaces_existing_restriction(db, alice, bob, channel):
    _grant_moderate(db, alice, channel)
    mute_user(db, channel, bob, duration=datetime.timedelta(minutes=5), reason="first", muted_by=alice)
    updated = mute_user(db, channel, bob, duration=None, reason="second", muted_by=alice)
    assert updated.expires_at is None
    assert updated.reason == "second"
    # Still exactly one row -- upsert, not a second accumulated one.
    assert len(list_channel_restrictions(db, channel)) == 1


def test_unmute_removes_restriction(db, alice, bob, channel):
    _grant_moderate(db, alice, channel)
    mute_user(db, channel, bob, duration=None, reason=None, muted_by=alice)
    unmute_user(db, channel, bob, unmuted_by=alice)
    assert is_muted(db, channel, bob) is None


def test_unmute_is_idempotent_when_never_muted(db, alice, bob, channel):
    _grant_moderate(db, alice, channel)
    unmute_user(db, channel, bob, unmuted_by=alice)  # must not raise


# -- ban --------------------------------------------------------------------


def test_ban_user_creates_restriction(db, alice, bob, channel):
    _grant_moderate(db, alice, channel)
    restriction = ban_user(db, channel, bob, duration=None, reason="abuse", banned_by=alice)
    assert restriction.kind == "ban"
    assert restriction.reason == "abuse"


def test_is_banned_true_for_active_ban(db, alice, bob, channel):
    _grant_moderate(db, alice, channel)
    ban_user(db, channel, bob, duration=None, reason=None, banned_by=alice)
    assert is_banned(db, channel, bob) is not None


def test_is_banned_false_when_never_banned(db, bob, channel):
    assert is_banned(db, channel, bob) is None


def test_unban_removes_restriction(db, alice, bob, channel):
    _grant_moderate(db, alice, channel)
    ban_user(db, channel, bob, duration=None, reason=None, banned_by=alice)
    unban_user(db, channel, bob, unbanned_by=alice)
    assert is_banned(db, channel, bob) is None


def test_mute_and_ban_are_independent_restrictions(db, alice, bob, channel):
    _grant_moderate(db, alice, channel)
    mute_user(db, channel, bob, duration=None, reason=None, muted_by=alice)
    assert is_banned(db, channel, bob) is None
    ban_user(db, channel, bob, duration=None, reason=None, banned_by=alice)
    assert is_muted(db, channel, bob) is not None
    assert is_banned(db, channel, bob) is not None


# -- kick (no persisted state) ------------------------------------------


def test_kick_user_does_not_persist_a_restriction(db, alice, bob, channel):
    _grant_moderate(db, alice, channel)
    kick_user(db, channel, bob, reason="disruptive", kicked_by=alice)
    assert is_muted(db, channel, bob) is None
    assert is_banned(db, channel, bob) is None
    assert list_channel_restrictions(db, channel) == []


# -- audit logging --------------------------------------------------------


def test_mute_and_unmute_are_logged(db, alice, bob, channel):
    from netbbs.moderation import list_actions_for_target_user

    _grant_moderate(db, alice, channel)
    mute_user(db, channel, bob, duration=None, reason=None, muted_by=alice)
    unmute_user(db, channel, bob, unmuted_by=alice)
    actions = [e.action for e in list_actions_for_target_user(db, bob.id)]
    assert actions == ["mute", "unmute"]


def test_kick_is_logged(db, alice, bob, channel):
    from netbbs.moderation import list_actions_for_target_user

    _grant_moderate(db, alice, channel)
    kick_user(db, channel, bob, reason="disruptive", kicked_by=alice)
    actions = list_actions_for_target_user(db, bob.id)
    assert actions[-1].action == "kick"
    assert actions[-1].detail == "disruptive"


# -- listing ------------------------------------------------------------


def test_list_channel_restrictions_returns_all(db, alice, bob, channel):
    sysop2 = create_user(db, "carol", password="hunter2", user_level=10)
    _grant_moderate(db, alice, channel)
    mute_user(db, channel, bob, duration=None, reason=None, muted_by=alice)
    ban_user(db, channel, sysop2, duration=None, reason=None, banned_by=alice)
    assert len(list_channel_restrictions(db, channel)) == 2
