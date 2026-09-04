"""Tests for netbbs.chat.scrollback — bounded, disk-backed chat history."""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.scrollback import (
    SCROLLBACK_LIMIT_CONFIG_KEY,
    get_scrollback,
    get_scrollback_limit,
    record_message,
    set_scrollback_limit,
)
from netbbs.config import set_config
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
def lobby(db, alice):
    return create_channel(db, "lobby", creator=alice)


# -- recording and retrieval -------------------------------------------------


def test_record_message_returns_the_created_row(db, lobby):
    message = record_message(db, lobby, kind="message", author_label="alice", body="hello")
    assert message.channel_id == lobby.id
    assert message.kind == "message"
    assert message.author_label == "alice"
    assert message.body == "hello"


def test_get_scrollback_returns_oldest_first(db, lobby):
    record_message(db, lobby, kind="message", author_label="alice", body="first")
    record_message(db, lobby, kind="message", author_label="alice", body="second")
    scrollback = get_scrollback(db, lobby)
    assert [m.body for m in scrollback] == ["first", "second"]


def test_message_requires_body(db, lobby):
    with pytest.raises(ValueError):
        record_message(db, lobby, kind="message", author_label="alice")


def test_join_and_leave_events_have_no_body(db, lobby):
    joined = record_message(db, lobby, kind="join", author_label="alice")
    left = record_message(db, lobby, kind="leave", author_label="alice")
    assert joined.body is None
    assert left.body is None


def test_scrollback_is_per_channel(db, alice):
    lobby = create_channel(db, "lobby", creator=alice)
    other = create_channel(db, "other", creator=alice)
    record_message(db, lobby, kind="message", author_label="alice", body="in lobby")
    record_message(db, other, kind="message", author_label="alice", body="in other")

    assert [m.body for m in get_scrollback(db, lobby)] == ["in lobby"]
    assert [m.body for m in get_scrollback(db, other)] == ["in other"]


def test_author_fingerprint_recorded_when_present(db, lobby):
    message = record_message(
        db, lobby, kind="message", author_label="alice", author_fingerprint="deadbeef", body="hi"
    )
    assert message.author_fingerprint == "deadbeef"


# -- retention limit -----------------------------------------------------


def test_get_scrollback_limit_default(db):
    assert get_scrollback_limit(db) == 100


def test_set_scrollback_limit_takes_effect(db):
    set_scrollback_limit(db, 5)
    assert get_scrollback_limit(db) == 5


def test_set_scrollback_limit_rejects_non_positive(db):
    with pytest.raises(ValueError):
        set_scrollback_limit(db, 0)
    with pytest.raises(ValueError):
        set_scrollback_limit(db, -1)


def test_malformed_scrollback_limit_config_falls_back_to_default(db):
    # Mirrors test_timeutil's malformed-config coverage: a value that
    # bypassed the validated setter (written directly via
    # netbbs.config.set_config) should not break retrieval.
    set_config(db, SCROLLBACK_LIMIT_CONFIG_KEY, "not-a-number")
    assert get_scrollback_limit(db) == 100


def test_zero_or_negative_scrollback_limit_config_falls_back_to_default(db):
    set_config(db, SCROLLBACK_LIMIT_CONFIG_KEY, "0")
    assert get_scrollback_limit(db) == 100


def test_scrollback_is_trimmed_to_configured_limit(db, lobby):
    set_scrollback_limit(db, 3)
    for i in range(5):
        record_message(db, lobby, kind="message", author_label="alice", body=f"msg {i}")

    scrollback = get_scrollback(db, lobby)
    assert [m.body for m in scrollback] == ["msg 2", "msg 3", "msg 4"]


def test_scrollback_trimming_counts_join_leave_events_too(db, lobby):
    # Join/leave events share the same retained window as chat messages,
    # not a separate budget -- trimming should treat them identically.
    set_scrollback_limit(db, 2)
    record_message(db, lobby, kind="join", author_label="alice")
    record_message(db, lobby, kind="message", author_label="alice", body="hi")
    record_message(db, lobby, kind="leave", author_label="alice")

    scrollback = get_scrollback(db, lobby)
    assert [m.kind for m in scrollback] == ["message", "leave"]


def test_trimming_does_not_affect_other_channels(db, alice):
    lobby = create_channel(db, "lobby", creator=alice)
    other = create_channel(db, "other", creator=alice)
    set_scrollback_limit(db, 1)
    record_message(db, other, kind="message", author_label="alice", body="keep me")
    for i in range(3):
        record_message(db, lobby, kind="message", author_label="alice", body=f"msg {i}")

    assert [m.body for m in get_scrollback(db, other)] == ["keep me"]
    assert [m.body for m in get_scrollback(db, lobby)] == ["msg 2"]


def test_record_message_returns_its_own_row_when_another_writer_lands_first(db, lobby):
    """Review of #275 (security): the MRC bridge records inbound lines on
    the background lane, so a row can land between a local caller's
    commit and its re-read. "The channel's newest row" then handed the
    caller someone else's line to sign and push over Link. A trigger
    stands in for that concurrent writer."""
    db.connection.execute(
        """
        CREATE TRIGGER shadow_insert AFTER INSERT ON channel_messages
        BEGIN
            INSERT INTO channel_messages
                (channel_id, kind, author_label, author_fingerprint, body, created_at, external_source)
            SELECT NEW.channel_id, 'message', 'bob@Other (MRC)', NULL, 'from the hub', NEW.created_at, 'mrc'
            WHERE NEW.external_source IS NULL;
        END
        """
    )
    recorded = record_message(db, lobby, kind="message", author_label="alice", body="mine")
    assert (recorded.author_label, recorded.body, recorded.external_source) == ("alice", "mine", None)
    assert [m.body for m in get_scrollback(db, lobby)] == ["mine", "from the hub"]


def test_external_source_round_trips(db, lobby):
    recorded = record_message(
        db, lobby, kind="message", author_label="bob@Other (MRC)", body="hi", external_source="mrc",
    )
    assert recorded.external_source == "mrc"
    assert get_scrollback(db, lobby)[-1].external_source == "mrc"
    local = record_message(db, lobby, kind="message", author_label="alice", body="hello")
    assert local.external_source is None


def test_record_message_returns_the_row_even_when_the_limit_is_one(db, lobby):
    """Review of #275: the row is captured inside the transaction, before
    the trim, so a limit of 1 plus another writer's trim cannot delete it
    before it is read back."""
    set_scrollback_limit(db, 1)
    first = record_message(db, lobby, kind="message", author_label="alice", body="first")
    second = record_message(db, lobby, kind="message", author_label="alice", body="second")
    assert (first.body, second.body) == ("first", "second")
    assert [m.body for m in get_scrollback(db, lobby)] == ["second"]
