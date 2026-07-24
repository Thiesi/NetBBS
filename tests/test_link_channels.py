"""
Tests for `netbbs.link.channels` — the local-origination bridge turning
an existing local channel/message into a signed `channel_genesis`/
`channel_message` event (design doc §9.6, issue #87).
"""

from __future__ import annotations

import json

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel, get_channel_by_name
from netbbs.chat.scrollback import get_scrollback, record_message, set_scrollback_limit
from netbbs.link.channels import (
    ChannelCarryLimitError,
    LinkChannelsError,
    carried_channel_count,
    is_channel_linked,
    link_channel,
    load_own_channel_events,
    materialize_carried_channel,
    materialize_carried_channel_message,
    queue_channel_message_if_linked,
)
from netbbs.link.events import build_channel_genesis, build_channel_message
from netbbs.link.node_identity import bootstrap_node_identity
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
def node_identity():
    return bootstrap_node_identity("roanoke")


# -- link_channel ------------------------------------------------------------


def test_link_channel_references_existing_channel_id_not_a_new_one(db, alice, node_identity):
    channel = create_channel(db, "lobby", creator=alice)

    genesis = link_channel(db, channel, node_identity=node_identity)

    assert genesis.payload["channel_id"] == channel.channel_id
    assert genesis.payload["origin_fingerprint"] == node_identity.fingerprint


def test_link_channel_persists_genesis_on_the_channel_row(db, alice, node_identity):
    channel = create_channel(db, "lobby", creator=alice)
    assert not is_channel_linked(db, channel)

    genesis = link_channel(db, channel, node_identity=node_identity)

    assert is_channel_linked(db, channel)
    row = db.connection.execute(
        "SELECT link_genesis_json FROM channels WHERE id = ?", (channel.id,)
    ).fetchone()
    assert row["link_genesis_json"] is not None
    stored = json.loads(row["link_genesis_json"])
    assert stored["envelope"]["payload"]["channel_id"] == channel.channel_id
    assert stored["signature"] == genesis.to_dict()["signature"]


def test_link_channel_refuses_to_relink_an_already_linked_channel(db, alice, node_identity):
    channel = create_channel(db, "lobby", creator=alice)
    link_channel(db, channel, node_identity=node_identity)

    with pytest.raises(LinkChannelsError):
        link_channel(db, channel, node_identity=node_identity)


def test_link_channel_carries_cascading_scalar_defaults(db, alice, node_identity):
    channel = create_channel(db, "lobby", creator=alice, min_level=1)

    genesis = link_channel(
        db, channel, node_identity=node_identity, default_min_level=1, default_min_age=18,
        default_name_requirement="verified",
    )

    assert genesis.payload["default_min_level"] == 1
    assert genesis.payload["default_min_age"] == 18
    assert genesis.payload["default_name_requirement"] == "verified"


def test_link_channel_defaults_description_to_the_channels_own(db, alice, node_identity):
    channel = create_channel(db, "lobby", description="General chat", creator=alice)

    genesis = link_channel(db, channel, node_identity=node_identity)

    assert genesis.payload["description"] == "General chat"


# -- materialize_carried_channel (design doc §9.6) ---------------------------


@pytest.fixture
def remote_node_identity():
    return bootstrap_node_identity("elsewhere")


def _remote_channel_genesis(remote_node_identity, *, channel_id="remote-channel-id", name="Remote Lobby", **kwargs):
    return build_channel_genesis(
        signing_identity=remote_node_identity.signing_key,
        origin_fingerprint=remote_node_identity.fingerprint,
        channel_id=channel_id,
        name=name,
        created_at="2026-01-01T00:00:00Z",
        **kwargs,
    )


def test_materialize_carried_channel_creates_a_local_row_with_the_genesis_channel_id(db, remote_node_identity):
    genesis = _remote_channel_genesis(remote_node_identity)

    materialized = materialize_carried_channel(db, genesis)

    assert materialized.channel_id == "remote-channel-id"
    assert materialized.name == "Remote Lobby"
    assert is_channel_linked(db, materialized)


def test_materialize_carried_channel_is_idempotent(db, remote_node_identity):
    genesis = _remote_channel_genesis(remote_node_identity)

    first = materialize_carried_channel(db, genesis)
    second = materialize_carried_channel(db, genesis)

    assert first.id == second.id


def test_materialize_carried_channel_seeds_settings_from_defaults(db, remote_node_identity):
    genesis = _remote_channel_genesis(
        remote_node_identity, default_min_level=2, default_min_age=21, default_name_requirement="verified"
    )

    materialized = materialize_carried_channel(db, genesis)

    assert materialized.min_level == 2
    assert materialized.min_age == 21
    assert materialized.name_requirement == "verified"


def test_materialize_carried_channel_is_locally_browsable(db, remote_node_identity):
    genesis = _remote_channel_genesis(remote_node_identity)
    materialize_carried_channel(db, genesis)

    found = get_channel_by_name(db, "Remote Lobby")
    assert found.channel_id == "remote-channel-id"


def test_materialize_carried_channel_rejects_a_new_channel_once_at_cap(db, remote_node_identity, node_identity):
    first_genesis = _remote_channel_genesis(remote_node_identity, channel_id="chan-1", name="Chan One")
    materialize_carried_channel(db, first_genesis, own_fingerprint=node_identity.fingerprint, max_carried_channels=1)

    second_genesis = _remote_channel_genesis(remote_node_identity, channel_id="chan-2", name="Chan Two")
    with pytest.raises(ChannelCarryLimitError):
        materialize_carried_channel(
            db, second_genesis, own_fingerprint=node_identity.fingerprint, max_carried_channels=1
        )


def test_materialize_carried_channel_idempotent_resend_is_exempt_from_the_cap(db, remote_node_identity, node_identity):
    genesis = _remote_channel_genesis(remote_node_identity)
    materialize_carried_channel(db, genesis, own_fingerprint=node_identity.fingerprint, max_carried_channels=1)

    # Resent, at the cap -- must still succeed (already-materialized, not new).
    materialize_carried_channel(db, genesis, own_fingerprint=node_identity.fingerprint, max_carried_channels=1)


def test_materialize_carried_channel_is_unbounded_by_default(db, remote_node_identity):
    for i in range(3):
        genesis = _remote_channel_genesis(remote_node_identity, channel_id=f"chan-{i}", name=f"Chan {i}")
        materialize_carried_channel(db, genesis)  # no max_carried_channels given -- never refused


# -- materialize_carried_channel_message (design doc §9.6) -------------------


def _carried_channel(db, remote_node_identity, *, channel_id="remote-channel-id"):
    genesis = _remote_channel_genesis(remote_node_identity, channel_id=channel_id)
    materialize_carried_channel(db, genesis)
    return channel_id


def _remote_channel_message(remote_node_identity, *, channel_id="remote-channel-id", **kwargs):
    return build_channel_message(
        signing_identity=remote_node_identity.signing_key,
        home_node_fingerprint=remote_node_identity.fingerprint,
        local_user_id="wanderer",
        channel_id=channel_id,
        body=kwargs.pop("body", "hello there"),
        created_at=kwargs.pop("created_at", "2026-01-01T00:00:01Z"),
        **kwargs,
    )


def test_materialize_carried_channel_message_creates_a_local_row(db, remote_node_identity):
    channel_id = _carried_channel(db, remote_node_identity)
    message = _remote_channel_message(remote_node_identity, channel_id=channel_id)

    materialized = materialize_carried_channel_message(db, message, sender_fingerprint=remote_node_identity.fingerprint)

    assert materialized is not None
    assert materialized.kind == "message"
    assert materialized.body == "hello there"
    assert materialized.author_label == f"wanderer@{remote_node_identity.fingerprint}"

    row = db.connection.execute(
        "SELECT 1 FROM link_events WHERE content_id = ?", (message.content_id,)
    ).fetchone()
    assert row is not None  # the underlying signed event was persisted too, same call


def test_materialize_carried_channel_message_is_idempotent(db, remote_node_identity):
    channel_id = _carried_channel(db, remote_node_identity)
    message = _remote_channel_message(remote_node_identity, channel_id=channel_id)

    first = materialize_carried_channel_message(db, message, sender_fingerprint=remote_node_identity.fingerprint)
    second = materialize_carried_channel_message(db, message, sender_fingerprint=remote_node_identity.fingerprint)

    assert first.id == second.id
    # And no duplicate row exists.
    count = db.connection.execute(
        "SELECT COUNT(*) FROM channel_messages WHERE link_content_id = ?", (message.content_id,)
    ).fetchone()[0]
    assert count == 1


def test_materialize_carried_channel_message_returns_none_if_channel_not_locally_carried(db, remote_node_identity):
    message = _remote_channel_message(remote_node_identity, channel_id="never-carried")

    result = materialize_carried_channel_message(db, message, sender_fingerprint=remote_node_identity.fingerprint)

    assert result is None


def test_materialize_carried_channel_message_respects_the_bounded_scrollback_trim(db, remote_node_identity):
    """Design doc §9.6's own stated consequence: a materialized message
    is subject to the same trim-to-limit a local one already is -- not a
    bug, the identical bound every channel's own local history has."""
    channel_id = _carried_channel(db, remote_node_identity)
    set_scrollback_limit(db, 2)

    for i in range(3):
        message = _remote_channel_message(
            remote_node_identity, channel_id=channel_id, body=f"msg {i}",
            nonce=f"nonce-{i}", created_at=f"2026-01-01T00:00:0{i}Z",
        )
        materialize_carried_channel_message(db, message, sender_fingerprint=remote_node_identity.fingerprint)

    channel = get_channel_by_name(db, "Remote Lobby")
    scrollback = get_scrollback(db, channel)
    assert [m.body for m in scrollback] == ["msg 1", "msg 2"]


# -- queue_channel_message_if_linked (design doc §9.6) -----------------------


def test_queue_channel_message_if_linked_builds_a_valid_event(db, alice, node_identity):
    channel = create_channel(db, "lobby", creator=alice)
    link_channel(db, channel, node_identity=node_identity)
    message = record_message(db, channel, kind="message", author_label="alice", body="hi there")

    queued = queue_channel_message_if_linked(db, message, channel, node_identity=node_identity)

    assert queued is not None
    assert queued.payload["channel_id"] == channel.channel_id
    assert queued.payload["body"] == "hi there"


def test_queue_channel_message_if_linked_is_a_noop_for_an_unlinked_channel(db, alice, node_identity):
    channel = create_channel(db, "lobby", creator=alice)
    message = record_message(db, channel, kind="message", author_label="alice", body="hi there")

    queued = queue_channel_message_if_linked(db, message, channel, node_identity=node_identity)

    assert queued is None


def test_queue_channel_message_if_linked_ignores_non_message_kinds(db, alice, node_identity):
    channel = create_channel(db, "lobby", creator=alice)
    link_channel(db, channel, node_identity=node_identity)
    join_event = record_message(db, channel, kind="join", author_label="alice", author_fingerprint=None)

    queued = queue_channel_message_if_linked(db, join_event, channel, node_identity=node_identity)

    assert queued is None


def test_queue_channel_message_if_linked_is_idempotent(db, alice, node_identity):
    channel = create_channel(db, "lobby", creator=alice)
    link_channel(db, channel, node_identity=node_identity)
    message = record_message(db, channel, kind="message", author_label="alice", body="hi there")

    first = queue_channel_message_if_linked(db, message, channel, node_identity=node_identity)
    second = queue_channel_message_if_linked(db, message, channel, node_identity=node_identity)

    assert first.content_id == second.content_id


# -- load_own_channel_events (design doc §9.6) -------------------------------


def test_load_own_channel_events_includes_genesis_and_queued_messages(db, alice, node_identity):
    channel = create_channel(db, "lobby", creator=alice)
    genesis = link_channel(db, channel, node_identity=node_identity)
    message = record_message(db, channel, kind="message", author_label="alice", body="hi there")
    queued = queue_channel_message_if_linked(db, message, channel, node_identity=node_identity)

    events = load_own_channel_events(db, node_identity.fingerprint)

    event_ids = {e.content_id for e in events}
    assert event_ids == {genesis.content_id, queued.content_id}


def test_load_own_channel_events_excludes_a_carried_channels_genesis(db, remote_node_identity, node_identity):
    """Mirrors `netbbs.link.boards.load_own_board_events`'s own
    origin-filter reasoning -- a carried channel's genesis is not this
    node's own to re-push as if it originated it."""
    genesis = _remote_channel_genesis(remote_node_identity)
    materialize_carried_channel(db, genesis)

    events = load_own_channel_events(db, node_identity.fingerprint)

    assert events == []


def test_carried_channel_count_excludes_self_originated(db, alice, node_identity, remote_node_identity):
    own_channel = create_channel(db, "lobby", creator=alice)
    link_channel(db, own_channel, node_identity=node_identity)
    carried_genesis = _remote_channel_genesis(remote_node_identity, channel_id="carried-chan")
    materialize_carried_channel(db, carried_genesis)

    assert carried_channel_count(db, node_identity.fingerprint) == 1
