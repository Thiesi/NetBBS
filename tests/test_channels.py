"""Tests for netbbs.chat.channels — channel creation, lookup, content IDs."""

from __future__ import annotations

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.chat.categories import create_category
from netbbs.chat.channels import (
    ChannelError,
    create_channel,
    delete_channel,
    get_channel_by_name,
    list_channels,
    update_channel,
)
from netbbs.chat.membership import add_member
from netbbs.chat.moderation import mute_user
from netbbs.chat.scrollback import record_message
from netbbs.moderation.log import list_actions_for_object
from netbbs.moderation.roles import ChannelPermission, grant_permissions
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=0)


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)


def test_create_channel(db, alice):
    channel = create_channel(db, "lobby", description="General chat", creator=alice)
    assert channel.name == "lobby"
    assert channel.description == "General chat"
    assert channel.min_level == 0


def test_create_channel_generates_content_addressed_id(db, alice):
    channel = create_channel(db, "lobby", creator=alice)
    assert len(channel.channel_id) == 64
    int(channel.channel_id, 16)


def test_create_duplicate_channel_name_fails(db, alice):
    create_channel(db, "lobby", creator=alice)
    with pytest.raises(ChannelError):
        create_channel(db, "lobby", creator=alice)


def test_get_channel_by_name(db, alice):
    create_channel(db, "lobby", creator=alice)
    channel = get_channel_by_name(db, "lobby")
    assert channel.name == "lobby"


def test_get_nonexistent_channel_fails(db):
    with pytest.raises(ChannelError):
        get_channel_by_name(db, "nope")


def test_list_channels_returns_all_in_creation_order(db, alice):
    create_channel(db, "first", creator=alice)
    create_channel(db, "second", creator=alice)
    channels = list_channels(db)
    assert [c.name for c in channels] == ["first", "second"]


# -- update/delete (design doc -- channel management round) --------------


def test_create_channel_records_an_audit_entry(db, alice):
    channel = create_channel(db, "lobby", creator=alice)
    entries = list_actions_for_object(db, "channel", channel.id)
    assert any(e.action == "create_channel" for e in entries)


def test_update_channel_replaces_the_full_state(db, alice):
    channel = create_channel(db, "lobby", creator=alice)
    updated = update_channel(
        db, channel, name="lobby2", description="new desc", min_level=5, category_id=None,
        pinned=True, hidden=True, members_only=True, allow_member_invites=True,
        min_age=18, name_requirement="verified", changed_by=alice,
    )
    assert updated.name == "lobby2"
    assert updated.description == "new desc"
    assert updated.min_level == 5
    assert updated.pinned is True
    assert updated.hidden is True
    assert updated.members_only is True
    assert updated.allow_member_invites is True
    assert updated.min_age == 18
    assert updated.name_requirement == "verified"
    entries = list_actions_for_object(db, "channel", channel.id)
    assert any(e.action == "update_channel" for e in entries)


def test_update_channel_rejects_a_name_collision(db, alice):
    create_channel(db, "taken", creator=alice)
    channel = create_channel(db, "lobby", creator=alice)
    with pytest.raises(ChannelError):
        update_channel(
            db, channel, name="taken", description=None, min_level=0, category_id=None,
            pinned=False, hidden=False, members_only=False, allow_member_invites=False,
            min_age=None, name_requirement=None, changed_by=alice,
        )


def test_update_channel_rejects_invalid_name_requirement(db, alice):
    channel = create_channel(db, "lobby", creator=alice)
    with pytest.raises(ChannelError, match="name_requirement"):
        update_channel(
            db, channel, name="lobby", description=None, min_level=0, category_id=None,
            pinned=False, hidden=False, members_only=False, allow_member_invites=False,
            min_age=None, name_requirement="bogus", changed_by=alice,
        )


def test_create_channel_rejects_invalid_name_requirement(db, alice):
    with pytest.raises(ChannelError, match="name_requirement"):
        create_channel(db, "lobby", creator=alice, name_requirement="bogus")


def test_create_channel_defaults_no_age_or_name_gate(db, alice):
    channel = create_channel(db, "lobby", creator=alice)
    assert channel.min_age is None
    assert channel.name_requirement is None


def test_delete_channel_cascades_scrollback_restrictions_membership_and_grants(db, alice, bob, sysop):
    channel = create_channel(db, "lobby", creator=alice)
    record_message(db, channel, kind="message", author_label="bob", body="hi")
    mute_user(db, channel, bob, duration=None, reason=None, muted_by=sysop)
    add_member(db, channel, bob, granted_by=sysop)
    grant_permissions(
        db, bob, object_type="channel", object_id=channel.id, permissions=ChannelPermission.MODERATE,
        granted_by=sysop,
    )

    delete_channel(db, channel, deleted_by=alice)

    with pytest.raises(ChannelError):
        get_channel_by_name(db, "lobby")
    assert db.connection.execute("SELECT COUNT(*) FROM channel_messages").fetchone()[0] == 0
    assert db.connection.execute("SELECT COUNT(*) FROM channel_restrictions").fetchone()[0] == 0
    assert db.connection.execute("SELECT COUNT(*) FROM channel_members").fetchone()[0] == 0
    assert db.connection.execute("SELECT COUNT(*) FROM moderator_grants").fetchone()[0] == 0


def test_delete_channel_does_not_touch_its_category(db, alice):
    category = create_category(db, "Vintage", created_by=alice)
    channel = create_channel(db, "lobby", category_id=category.id, creator=alice)
    delete_channel(db, channel, deleted_by=alice)
    from netbbs.chat.categories import get_category_by_id

    still_there = get_category_by_id(db, category.id)
    assert still_there.name == "Vintage"


def test_delete_channel_records_an_audit_entry_before_deleting(db, alice):
    channel = create_channel(db, "lobby", creator=alice)
    channel_id = channel.id
    delete_channel(db, channel, deleted_by=alice)
    entries = list_actions_for_object(db, "channel", channel_id)
    assert any(e.action == "delete_channel" for e in entries)


def test_two_channels_have_different_content_ids(db, alice):
    a = create_channel(db, "channel-a", creator=alice)
    b = create_channel(db, "channel-b", creator=alice)
    assert a.channel_id != b.channel_id


def test_channel_min_level_defaults_to_zero(db, alice):
    channel = create_channel(db, "lobby", creator=alice)
    assert channel.min_level == 0


def test_channel_min_level_can_be_set(db, alice):
    channel = create_channel(db, "staff-only", min_level=50, creator=alice)
    assert channel.min_level == 50
