"""
Tests for channel topics (design doc §8):
`netbbs.chat.channels.set_topic`, gated by `ChannelPermission.EDIT` --
already reserved for exactly this in `netbbs.moderation.roles`.
"""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import TopicError, create_channel, get_channel_by_name, set_topic
from netbbs.moderation import ChannelPermission, grant_permissions
from netbbs.moderation.log import list_actions_for_object
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
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def channel(db, sysop):
    return create_channel(db, "lobby", creator=sysop)


def test_new_channel_has_no_topic(channel):
    assert channel.topic is None


def test_set_topic_requires_edit_permission(db, channel, alice):
    with pytest.raises(TopicError):
        set_topic(db, channel, "new topic", set_by=alice)
    # Rejected attempt must not have changed anything.
    assert get_channel_by_name(db, channel.name).topic is None


def test_set_topic_succeeds_with_per_object_edit_grant(db, channel, alice, sysop):
    grant_permissions(
        db,
        alice,
        object_type="channel",
        object_id=channel.id,
        permissions=ChannelPermission.EDIT,
        granted_by=sysop,
    )

    updated = set_topic(db, channel, "Retro computing chat", set_by=alice)

    assert updated.topic == "Retro computing chat"
    # Re-fetched independently, not just trusting the returned object.
    assert get_channel_by_name(db, channel.name).topic == "Retro computing chat"


def test_set_topic_succeeds_with_local_blanket_edit_grant(db, channel, alice, sysop):
    grant_permissions(
        db,
        alice,
        object_type="channel",
        object_id=None,  # local-blanket, per design doc §13
        permissions=ChannelPermission.EDIT,
        granted_by=sysop,
    )

    set_topic(db, channel, "Blanket-granted topic", set_by=alice)

    assert get_channel_by_name(db, channel.name).topic == "Blanket-granted topic"


def test_moderate_permission_alone_does_not_allow_setting_topic(db, channel, alice, sysop):
    # EDIT and MODERATE are deliberately different bits -- a chat
    # moderator who can kick/mute/ban shouldn't automatically also be
    # able to change the topic.
    grant_permissions(
        db,
        alice,
        object_type="channel",
        object_id=channel.id,
        permissions=ChannelPermission.MODERATE,
        granted_by=sysop,
    )
    with pytest.raises(TopicError):
        set_topic(db, channel, "should not work", set_by=alice)


def test_clearing_topic_with_empty_string_stores_none(db, channel, sysop):
    grant_permissions(
        db,
        sysop,
        object_type="channel",
        object_id=channel.id,
        permissions=ChannelPermission.EDIT,
        granted_by=sysop,
    )
    set_topic(db, channel, "Something", set_by=sysop)

    set_topic(db, channel, "", set_by=sysop)

    assert get_channel_by_name(db, channel.name).topic is None


def test_set_topic_is_recorded_in_the_moderation_log(db, channel, sysop):
    grant_permissions(
        db,
        sysop,
        object_type="channel",
        object_id=channel.id,
        permissions=ChannelPermission.EDIT,
        granted_by=sysop,
    )
    set_topic(db, channel, "Logged topic", set_by=sysop)

    entries = list_actions_for_object(db, "channel", channel.id)
    topic_entries = [e for e in entries if e.action == "topic"]
    assert len(topic_entries) == 1
    assert topic_entries[0].actor_user_id == sysop.id
    assert topic_entries[0].detail == "Logged topic"
