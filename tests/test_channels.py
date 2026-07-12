"""Tests for netbbs.chat.channels — channel creation, lookup, content IDs."""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import ChannelError, create_channel, get_channel_by_name, list_channels
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


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
