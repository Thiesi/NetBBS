"""
Issue #671: a carried resource whose name is already in use on this node.

Names are unique per table here and not identities in Link, so the same name
from two origins is the ordinary case. Before this, the insert raised
`sqlite3.IntegrityError` out of the sync pass and the resource was never
carried. Each kind is exercised through its own materializer, against a real
SQLite database.
"""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.boards.boards import create_board
from netbbs.chat.channels import create_channel
from netbbs.files.areas import create_file_area
from netbbs.link.boards import BoardCarryLimitError, materialize_carried_board
from netbbs.link.channels import ChannelCarryLimitError, materialize_carried_channel
from netbbs.link.events import build_board_genesis, build_channel_genesis, build_file_area_genesis
from netbbs.link.files import FileAreaCarryLimitError, materialize_carried_file_area
from netbbs.link.local_names import free_local_name
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.storage.database import Database

RESOURCE_ID = "0123456789abcdef0123456789abcdef"

KINDS = {
    "board": dict(
        table="boards", create=create_board, build=build_board_genesis, id_field="board_id",
        materialize=materialize_carried_board, refused=BoardCarryLimitError,
    ),
    "channel": dict(
        table="channels", create=create_channel, build=build_channel_genesis, id_field="channel_id",
        materialize=materialize_carried_channel, refused=ChannelCarryLimitError,
    ),
    "file_area": dict(
        table="file_areas", create=create_file_area, build=build_file_area_genesis, id_field="area_id",
        materialize=materialize_carried_file_area, refused=FileAreaCarryLimitError,
    ),
}


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture(scope="module")
def remote():
    return bootstrap_node_identity("remote-origin")


def _genesis(kind, remote, name, resource_id=RESOURCE_ID):
    spec = KINDS[kind]
    return spec["build"](
        signing_identity=remote.signing_key,
        origin_fingerprint=remote.fingerprint,
        name=name,
        created_at="2026-01-01T00:00:00Z",
        **{spec["id_field"]: resource_id},
    )


def _name_of(db, kind, resource_id=RESOURCE_ID):
    spec = KINDS[kind]
    id_column = "area_id" if kind == "file_area" else spec["id_field"]
    row = db.connection.execute(
        f"SELECT name FROM {spec['table']} WHERE {id_column} = ?", (resource_id,)
    ).fetchone()
    return None if row is None else row["name"]


@pytest.mark.parametrize("kind", KINDS)
def test_a_carried_resource_whose_name_is_taken_is_carried_under_a_suffixed_name(db, alice, remote, kind):
    KINDS[kind]["create"](db, "general", creator=alice)

    KINDS[kind]["materialize"](db, _genesis(kind, remote, "general"))

    assert _name_of(db, kind) == f"general-{RESOURCE_ID[:8]}"


@pytest.mark.parametrize("kind", KINDS)
def test_names_collide_regardless_of_case(db, alice, remote, kind):
    KINDS[kind]["create"](db, "General", creator=alice)

    KINDS[kind]["materialize"](db, _genesis(kind, remote, "general"))

    assert _name_of(db, kind) == f"general-{RESOURCE_ID[:8]}"


@pytest.mark.parametrize("kind", KINDS)
def test_a_free_name_is_kept_as_it_is(db, remote, kind):
    KINDS[kind]["materialize"](db, _genesis(kind, remote, "general"))

    assert _name_of(db, kind) == "general"


@pytest.mark.parametrize("kind", KINDS)
def test_a_resend_of_a_resource_carried_under_its_own_name_is_still_idempotent(db, remote, kind):
    """The name check runs after the idempotent return, so the resource does
    not find itself in the way and gain a suffix -- or, for channels, get
    refused -- on the second sync."""
    first = KINDS[kind]["materialize"](db, _genesis(kind, remote, "general"))
    second = KINDS[kind]["materialize"](db, _genesis(kind, remote, "general"))

    assert first.id == second.id
    assert _name_of(db, kind) == "general"


@pytest.mark.parametrize("kind", KINDS)
def test_with_every_candidate_taken_the_resource_is_refused_the_way_the_cap_refuses(db, alice, remote, kind):
    """The last candidate carries the whole id, so this takes a local resource
    named after another resource's id. It is refused with the carry-limit
    family the transport already tolerates, never an IntegrityError."""
    for name in ("general", f"general-{RESOURCE_ID[:8]}", f"general-{RESOURCE_ID[:16]}", f"general-{RESOURCE_ID}"):
        KINDS[kind]["create"](db, name, creator=alice)

    with pytest.raises(KINDS[kind]["refused"]):
        KINDS[kind]["materialize"](db, _genesis(kind, remote, "general"))

    assert _name_of(db, kind) is None


def test_two_carried_resources_with_one_name_both_land(db, remote):
    """Two origins, one name: the second arrival takes the suffix."""
    other = "fedcba9876543210fedcba9876543210"
    materialize_carried_board(db, _genesis("board", remote, "general"))
    materialize_carried_board(db, _genesis("board", remote, "general", resource_id=other))

    assert _name_of(db, "board") == "general"
    assert _name_of(db, "board", other) == f"general-{other[:8]}"


@pytest.mark.parametrize("resource_id", [12345, "", None])
def test_a_resource_id_that_is_not_a_string_is_refused_not_raised(db, resource_id):
    """A genesis's id is not type-checked before projection; slicing an
    integer used to raise `TypeError` out of the sync pass."""
    assert free_local_name(db, "boards", "general", resource_id) is None


def test_free_local_name_refuses_a_table_it_does_not_know(db):
    with pytest.raises(ValueError):
        free_local_name(db, "users", "general", RESOURCE_ID)
