"""A carried message's `created_at` is remote input (Codex review).

Chat timestamps are on by default now, so entering a channel formats the
`created_at` of every message in its scrollback. That turned a field
nothing used to look at into one that runs on every render, for data a
peer supplied.

The real-time frame path has always validated it
(`netbbs.link.protocol._validate_channel_message_payload`). The durable
gossip path did not, so a signed peer could store
`created_at="invalid"` -- and one such row would have made the channel
permanently unenterable, with nothing on screen saying why.

Both halves are held here: new rows are refused at ingest, and a row
stored before that boundary existed still renders.
"""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.timestamps import format_with_preference, set_timestamps_enabled
from netbbs.link.channels import (
    LinkChannelsError,
    materialize_carried_channel,
    materialize_carried_channel_message,
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
def origin():
    return bootstrap_node_identity("elsewhere")


@pytest.fixture
def carried(db, origin):
    genesis = build_channel_genesis(
        signing_identity=origin.signing_key,
        origin_fingerprint=origin.fingerprint,
        channel_id="carried-lobby",
        name="lobby",
        created_at="2026-01-01T00:00:00.000000Z",
    )
    return materialize_carried_channel(db, genesis)


def _message(origin, carried, *, created_at):
    return build_channel_message(
        signing_identity=origin.signing_key,
        channel_id=carried.channel_id,
        home_node_fingerprint=origin.fingerprint,
        local_user_id="7",
        body="hello from elsewhere",
        created_at=created_at,
    )


# -- the ingest boundary ------------------------------------------------


def test_a_well_formed_carried_message_is_still_accepted(db, origin, carried):
    stored = materialize_carried_channel_message(
        db, _message(origin, carried, created_at="2026-01-01T12:00:00.000000Z"),
        sender_fingerprint=origin.fingerprint,
    )
    assert stored is not None
    assert stored.body == "hello from elsewhere"


@pytest.mark.parametrize(
    "created_at",
    [
        "invalid",
        "",
        "2026-13-45T99:99:99Z",
        "2026-01-01 12:00:00",          # no timezone
        "0001-01-01T00:00:00+23:59",    # parses, then overflows converting to UTC
    ],
)
def test_a_malformed_timestamp_is_refused_rather_than_stored(db, origin, carried, created_at):
    with pytest.raises(LinkChannelsError):
        materialize_carried_channel_message(
            db, _message(origin, carried, created_at=created_at),
            sender_fingerprint=origin.fingerprint,
        )
    # Nothing landed: the row a later render would have tripped over does
    # not exist, and neither does a half-written `link_events` entry.
    assert db.connection.execute("SELECT COUNT(*) FROM channel_messages").fetchone()[0] == 0
    assert db.connection.execute("SELECT COUNT(*) FROM link_events").fetchone()[0] == 0


# -- the render path ----------------------------------------------------


def test_an_unparseable_stored_timestamp_costs_the_stamp_not_the_line(db):
    """Belt to the boundary's braces: a row written before the boundary
    existed still has to render. This runs once per message in
    scrollback, so raising here shuts a caller out of the channel
    entirely, every time they try.
    """
    user = create_user(db, "alice", password="hunter2", user_level=10)
    create_channel(db, "lobby", creator=user)
    set_timestamps_enabled(db, user, True)

    rendered = format_with_preference(db, user, "<bob> hello", "invalid")

    assert rendered == "<bob> hello"


def test_a_good_timestamp_still_gets_its_stamp(db):
    user = create_user(db, "alice", password="hunter2", user_level=10)
    set_timestamps_enabled(db, user, True)

    rendered = format_with_preference(db, user, "<bob> hello", "2026-01-01T12:34:00.000000Z")

    assert "12:34" in rendered
    assert "<bob> hello" in rendered
