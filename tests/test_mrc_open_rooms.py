"""
Issue #300 at the domain layer: the open-room settings, materializing
an `mrc:<room>` channel on demand, the reserved name prefix, adoption,
retirement, the sweeper, and Link's refusal -- all `db`-first and
synchronous, on a real SQLite file.
"""

from __future__ import annotations

import datetime

import pytest

from netbbs.activity import follow
from netbbs.auth.users import create_user
from netbbs.chat.channels import ChannelError, create_channel, get_channel_by_name, update_channel
from netbbs.chat.scrollback import get_scrollback, record_message
from netbbs.link.channels import LinkChannelsError, link_channel
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.mrc.settings import (
    OPEN_ROOM_ORIGIN,
    MrcSettingsError,
    OpenRoomSettings,
    adopt_open_room,
    count_open_rooms,
    get_mrc_mapping,
    list_mrc_mappings,
    list_open_rooms,
    load_open_room_settings,
    materialize_open_room,
    open_room_channel_ids,
    retire_open_room,
    save_open_room_settings,
    set_mrc_room,
    sweep_open_rooms,
    touch_open_room,
)
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=255)


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


def _on(**overrides) -> OpenRoomSettings:
    return OpenRoomSettings(enabled=True, **overrides)


def test_open_room_settings_default_off_and_round_trip(db):
    assert load_open_room_settings(db) == OpenRoomSettings()
    saved = save_open_room_settings(
        db, OpenRoomSettings(enabled=True, min_level=5, min_age=18, name_requirement="verified", cap=10,
                             retention_days=3, blocklist=("Bad Room", "bad_room", "|04evil")),
    )
    assert saved.blocklist == ("Bad_Room", "evil")
    assert load_open_room_settings(db) == saved
    # A room name may contain a comma (MRC allows it): the list must not
    # split on it, so `foo,bar` blocks `foo,bar` and nothing else.
    saved = save_open_room_settings(db, OpenRoomSettings(blocklist=("foo,bar",)))
    assert load_open_room_settings(db).blocklist == ("foo,bar",)
    assert saved.blocks("FOO,BAR") and not saved.blocks("foo") and not saved.blocks("bar")


@pytest.mark.parametrize(
    "bad",
    [
        OpenRoomSettings(min_level=300), OpenRoomSettings(min_age=-1), OpenRoomSettings(name_requirement="maybe"),
        OpenRoomSettings(cap=0), OpenRoomSettings(cap=10_000), OpenRoomSettings(retention_days=0),
    ],
)
def test_open_room_settings_are_validated(db, bad):
    with pytest.raises(MrcSettingsError):
        save_open_room_settings(db, bad)


def test_materialize_creates_a_real_channel_with_the_default_gates(db):
    mapping = materialize_open_room(db, "#Lobby", open_settings=_on(min_level=7, min_age=16, name_requirement="verified"))
    assert mapping.is_open_room and mapping.origin == OPEN_ROOM_ORIGIN and mapping.room == "Lobby"
    channel = mapping.channel
    assert channel.name == "mrc:Lobby"
    assert (channel.min_level, channel.min_age, channel.name_requirement) == (7, 16, "verified")
    assert channel.hidden is False and channel.members_only is False and channel.category_id is None
    assert mapping.last_active_at is not None
    assert get_channel_by_name(db, "mrc:Lobby").id == channel.id
    assert open_room_channel_ids(db) == {channel.id}
    assert [m.room for m in list_open_rooms(db)] == ["Lobby"]
    # Idempotent, case-insensitively, and the id is content-addressed
    # from the room so a retired room reopened later is the same id.
    again = materialize_open_room(db, "lobby", open_settings=_on())
    assert again.channel.id == channel.id
    assert count_open_rooms(db) == 1


def test_materialize_refuses_when_off_blocked_or_at_the_cap(db):
    with pytest.raises(MrcSettingsError, match="switched off"):
        materialize_open_room(db, "lobby", open_settings=OpenRoomSettings())
    with pytest.raises(MrcSettingsError, match="blocked MRC room #SECRET"):
        materialize_open_room(db, "SECRET", open_settings=_on(blocklist=("secret",)))
    with pytest.raises(MrcSettingsError, match="printable"):
        materialize_open_room(db, "   ", open_settings=_on())
    settings = _on(cap=2)
    materialize_open_room(db, "one", open_settings=settings)
    materialize_open_room(db, "two", open_settings=settings)
    with pytest.raises(MrcSettingsError, match="already has 2 MRC rooms open"):
        materialize_open_room(db, "three", open_settings=settings)
    # The cap refuses, it never evicts: the two rooms are still there,
    # and an already-open room is still reachable past the cap.
    assert count_open_rooms(db) == 2
    assert materialize_open_room(db, "ONE", open_settings=settings).room == "one"


def test_a_room_the_sysop_mapped_is_that_channel(db, sysop):
    general = create_channel(db, "general", creator=sysop, min_level=20)
    set_mrc_room(db, general, "lobby")
    mapping = materialize_open_room(db, "lobby", open_settings=_on())
    assert mapping.channel.id == general.id and not mapping.is_open_room
    assert count_open_rooms(db) == 0


def test_the_mrc_prefix_is_reserved_for_open_rooms(db, sysop):
    with pytest.raises(ChannelError, match="reserved"):
        create_channel(db, "mrc:lobby", creator=sysop)
    with pytest.raises(ChannelError, match="reserved"):
        create_channel(db, "MRC:Lobby", creator=sysop)
    plain = create_channel(db, "plain", creator=sysop)
    with pytest.raises(ChannelError, match="reserved"):
        update_channel(
            db, plain, name="mrc:plain", description=None, min_level=0, category_id=None, pinned=False,
            hidden=False, members_only=False, allow_member_invites=False, min_age=None,
            name_requirement=None, community_id=None, changed_by=sysop,
        )
    # Editing an open room's other settings under its own name is fine.
    opened = materialize_open_room(db, "lobby", open_settings=_on()).channel
    updated = update_channel(
        db, opened, name=opened.name, description="the hub's lobby", min_level=1, category_id=None,
        pinned=False, hidden=False, members_only=False, allow_member_invites=False, min_age=None,
        name_requirement=None, community_id=None, changed_by=sysop,
    )
    assert updated.description == "the hub's lobby"


def test_adopt_turns_an_open_room_into_a_mapped_channel(db, sysop):
    opened = materialize_open_room(db, "lobby", open_settings=_on()).channel
    record_message(db, opened, kind="message", author_label="bob@Other (MRC)", author_fingerprint=None,
                   body="kept", external_source="mrc", index_body="kept")
    adopted = adopt_open_room(db, opened)
    assert not adopted.is_open_room and adopted.room == "lobby" and adopted.last_active_at is None
    assert [m.body for m in get_scrollback(db, opened)] == ["kept"]
    assert count_open_rooms(db) == 0 and [m.room for m in list_mrc_mappings(db)] == ["lobby"]
    with pytest.raises(MrcSettingsError):
        adopt_open_room(db, opened)
    with pytest.raises(MrcSettingsError):
        retire_open_room(db, opened)
    plain = create_channel(db, "plain", creator=sysop)
    with pytest.raises(MrcSettingsError):
        retire_open_room(db, plain)


def test_retire_removes_the_room_and_its_scrollback(db):
    opened = materialize_open_room(db, "lobby", open_settings=_on()).channel
    record_message(db, opened, kind="message", author_label="bob@Other (MRC)", author_fingerprint=None,
                   body="gone", external_source="mrc", index_body="gone")
    retire_open_room(db, opened)
    with pytest.raises(ChannelError):
        get_channel_by_name(db, "mrc:lobby")
    assert db.connection.execute("SELECT COUNT(*) AS n FROM channel_messages WHERE channel_id = ?", (opened.id,)).fetchone()["n"] == 0
    assert db.connection.execute("SELECT COUNT(*) AS n FROM channel_message_search WHERE channel_id = ?", (opened.id,)).fetchone()["n"] == 0


def test_sweep_retires_only_idle_unoccupied_unfollowed_open_rooms(db, sysop, alice):
    settings = _on(retention_days=7)
    idle = materialize_open_room(db, "idle", open_settings=settings).channel
    busy = materialize_open_room(db, "busy", open_settings=settings).channel
    followed = materialize_open_room(db, "followed", open_settings=settings).channel
    recent = materialize_open_room(db, "recent", open_settings=settings).channel
    kept = materialize_open_room(db, "kept", open_settings=settings).channel
    mapped = create_channel(db, "general", creator=sysop)
    set_mrc_room(db, mapped, "general")
    long_ago = "2020-01-01T00:00:00.000000Z"
    for channel in (idle, busy, followed, kept):
        touch_open_room(db, channel, now=long_ago)
    follow(db, alice, "channel", followed.id)
    adopt_open_room(db, kept)
    now = datetime.datetime(2026, 9, 5, tzinfo=datetime.timezone.utc)

    retired = sweep_open_rooms(db, retention_days=7, occupied_channel_ids={busy.id}, now=now)
    assert [m.room for m in retired] == ["idle"]
    assert {m.room for m in list_open_rooms(db)} == {"busy", "followed", "recent"}
    assert get_mrc_mapping(db, mapped) is not None and get_mrc_mapping(db, kept) is not None
    # A room never touched ages from its creation; a touch on a mapped
    # channel is a no-op.
    touch_open_room(db, mapped)
    assert get_mrc_mapping(db, mapped).last_active_at is None
    later = now + datetime.timedelta(days=8)
    retired = sweep_open_rooms(db, retention_days=7, occupied_channel_ids=set(), now=later)
    assert [m.room for m in retired] == ["busy", "recent"]
    assert {m.room for m in list_open_rooms(db)} == {"followed"}


def test_link_refuses_an_open_room_but_not_an_adopted_one(db, tmp_path):
    identity = bootstrap_node_identity("roanoke")
    opened = materialize_open_room(db, "lobby", open_settings=_on()).channel
    with pytest.raises(LinkChannelsError, match="cannot be Linked"):
        link_channel(db, opened, node_identity=identity)
    adopt_open_room(db, opened)
    # Adopted but still named into the prefix: Link asks for a rename
    # first, since every receiving node would rename the genesis anyway.
    with pytest.raises(LinkChannelsError, match="rename it first"):
        link_channel(db, opened, node_identity=identity)
    sysop = create_user(db, "sysop2", password="hunter2", user_level=255)
    renamed = update_channel(
        db, opened, name="lobby-talk", description=None, min_level=0, category_id=None, pinned=False,
        hidden=False, members_only=False, allow_member_invites=False, min_age=None, name_requirement=None,
        community_id=None, changed_by=sysop,
    )
    assert link_channel(db, renamed, node_identity=identity) is not None


def test_open_room_ids_are_namespaced_per_node(db, tmp_path):
    """Second review of #300: the same room opened on two nodes must not
    share a channel id, or one node's adopted-and-Linked room would alias
    the other's open room in `materialize_carried_channel`."""
    other = Database(tmp_path / "other.db")
    try:
        here = materialize_open_room(db, "lobby", open_settings=_on()).channel
        there = materialize_open_room(other, "lobby", open_settings=_on()).channel
        assert here.channel_id != there.channel_id
        # Stable on one node: reopening after retirement keeps the id.
        retire_open_room(db, here)
        assert materialize_open_room(db, "lobby", open_settings=_on()).channel.channel_id == here.channel_id
    finally:
        other.close()


def test_link_refuses_a_genesis_that_squats_or_aliases_an_open_room(db):
    """Second review of #300: a peer's genesis may neither wear the
    reserved prefix nor claim an open room's id, and a message naming an
    open room's id is projected nowhere."""
    from netbbs.link.channels import ChannelCarryRefusedError, materialize_carried_channel
    from netbbs.link.events import build_channel_genesis
    from netbbs.timeutil import utc_now_iso

    peer = bootstrap_node_identity("peer")
    opened = materialize_open_room(db, "lobby", open_settings=_on()).channel

    def _genesis(channel_id: str, name: str):
        return build_channel_genesis(
            signing_identity=peer.signing_key, origin_fingerprint=peer.fingerprint, channel_id=channel_id,
            name=name, created_at=utc_now_iso(), description=None, default_min_level=0,
            default_min_age=None, default_name_requirement=None,
        )

    # A genesis wearing the prefix (Linked before the prefix was reserved,
    # or a squat) is carried under local-mrc:..., never as mrc:...
    carried = materialize_carried_channel(db, _genesis("f" * 64, "mrc:elsewhere"))
    assert carried.name == "local-mrc:elsewhere"
    with pytest.raises(ChannelError):
        get_channel_by_name(db, "mrc:elsewhere")
    # ... and the room of that name can still be opened here.
    assert materialize_open_room(db, "elsewhere", open_settings=_on()).channel.name == "mrc:elsewhere"
    # A second genesis colliding on the renamed name gets a suffix; a
    # third, with both names taken, is a tolerated refusal rather than an
    # IntegrityError the transport would not expect.
    second = materialize_carried_channel(db, _genesis("e" * 64, "MRC:elsewhere"))
    assert second.name == "local-MRC:elsewhere-eeeeeeee"
    sysop = create_user(db, "sysop3", password="hunter2", user_level=255)
    create_channel(db, "local-mrc:elsewhere-dddddddd", creator=sysop)
    with pytest.raises(ChannelCarryRefusedError, match="already local channel names"):
        materialize_carried_channel(db, _genesis("d" * 64, "mrc:elsewhere"))
    with pytest.raises(ChannelCarryRefusedError, match="belongs to an MRC room"):
        materialize_carried_channel(db, _genesis(opened.channel_id, "innocent"))
    assert get_mrc_mapping(db, opened).is_open_room


def test_existing_rows_survive_the_migration_with_no_origin(db, sysop):
    """The additive columns land NULL on every existing channel: a
    SysOp-mapped bridge made before this release is not an open room."""
    general = create_channel(db, "general", creator=sysop)
    set_mrc_room(db, general, "lobby")
    record_message(db, general, kind="message", author_label="sysop", author_fingerprint=None, body="hi")
    mapping = get_mrc_mapping(db, general)
    assert mapping is not None and mapping.origin is None and mapping.last_active_at is None
    assert not mapping.is_open_room
    columns = {row["name"] for row in db.connection.execute("PRAGMA table_info(channels)").fetchall()}
    assert {"mrc_origin", "mrc_last_active_at"} <= columns
