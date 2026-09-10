"""
Tests for `netbbs.link.files` — the local-origination bridge turning an
existing local file area/upload into a signed `file_area_genesis`/`file_
descriptor` Link event (design doc §11, issue #89). Mirrors `tests/
test_link_boards.py`'s own structure as closely as the underlying local
resources allow -- see `netbbs.link.files`' own module docstring for the
one real structural difference (a carried file's catalogue entry never
becomes a `files` row until fetched).
"""

from __future__ import annotations

import json

import pytest

from netbbs.auth.users import create_user
from netbbs.files.areas import create_file_area, get_file_area_by_name
from netbbs.files.entries import get_file, upload_file
from netbbs.link.events import FileAreaGenesis, build_file_area_genesis, build_file_descriptor
from netbbs.link.files import (
    FileAreaCarryLimitError,
    LinkFilesError,
    RemoteFileCatalogueLimitError,
    carried_file_area_count,
    file_area_origin_fingerprint,
    get_remote_file,
    has_queued_file_descriptor,
    is_area_linked,
    link_file_area,
    list_remote_files,
    load_own_file_area_events,
    materialize_carried_file_area,
    materialize_carried_file_descriptor,
    queue_file_descriptor_if_linked,
    remote_file_count_for_area,
)
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


@pytest.fixture
def remote_node_identity():
    return bootstrap_node_identity("elsewhere")


# -- link_file_area / is_area_linked ---------------------------------------


def test_link_file_area_references_existing_area_id_not_a_new_one(db, alice, node_identity):
    area = create_file_area(db, "downloads", creator=alice)
    genesis = link_file_area(db, area, node_identity=node_identity)
    assert genesis.payload["area_id"] == area.area_id


def test_link_file_area_persists_genesis_on_the_area_row(db, alice, node_identity):
    area = create_file_area(db, "downloads", creator=alice)
    genesis = link_file_area(db, area, node_identity=node_identity)

    row = db.connection.execute("SELECT link_genesis_json FROM file_areas WHERE id = ?", (area.id,)).fetchone()
    assert FileAreaGenesis.from_dict(json.loads(row["link_genesis_json"])).content_id == genesis.content_id


def test_link_file_area_refuses_to_relink_an_already_linked_area(db, alice, node_identity):
    area = create_file_area(db, "downloads", creator=alice)
    link_file_area(db, area, node_identity=node_identity)
    with pytest.raises(LinkFilesError):
        link_file_area(db, area, node_identity=node_identity)


def test_is_area_linked_false_before_linking(db, alice):
    area = create_file_area(db, "downloads", creator=alice)
    assert is_area_linked(db, area) is False


def test_link_file_area_carries_cascading_scalar_defaults(db, alice, node_identity):
    area = create_file_area(db, "downloads", creator=alice)
    genesis = link_file_area(
        db, area, node_identity=node_identity,
        default_min_read_level=5, default_min_write_level=10,
        default_moderated=True, default_max_file_age_days=30,
    )
    assert genesis.payload["default_min_read_level"] == 5
    assert genesis.payload["default_min_write_level"] == 10
    assert genesis.payload["default_moderated"] is True
    assert genesis.payload["default_max_file_age_days"] == 30


# -- materialize_carried_file_area ------------------------------------------


def _remote_genesis(remote_node_identity, *, area_id="remote-area-id", name="Remote Files", **kwargs):
    return build_file_area_genesis(
        signing_identity=remote_node_identity.signing_key,
        origin_fingerprint=remote_node_identity.fingerprint,
        area_id=area_id,
        name=name,
        created_at="2026-01-01T00:00:00Z",
        **kwargs,
    )


def test_materialize_carried_file_area_creates_a_local_row(db, remote_node_identity):
    genesis = _remote_genesis(remote_node_identity)
    area = materialize_carried_file_area(db, genesis)
    assert area.area_id == genesis.payload["area_id"]
    assert area.name == "Remote Files"


def test_materialize_carried_file_area_is_idempotent(db, remote_node_identity):
    genesis = _remote_genesis(remote_node_identity)
    first = materialize_carried_file_area(db, genesis)
    second = materialize_carried_file_area(db, genesis)
    assert first.id == second.id


def test_materialize_carried_file_area_is_locally_browsable(db, remote_node_identity):
    genesis = _remote_genesis(remote_node_identity)
    materialize_carried_file_area(db, genesis)
    found = get_file_area_by_name(db, "Remote Files")
    assert found.area_id == genesis.payload["area_id"]


def test_materialize_carried_file_area_rejects_a_new_area_once_at_cap(db, remote_node_identity, node_identity):
    first_genesis = _remote_genesis(remote_node_identity, area_id="area-a", name="Area A")
    materialize_carried_file_area(
        db, first_genesis, own_fingerprint=node_identity.fingerprint, max_carried_file_areas=1
    )
    second_genesis = _remote_genesis(remote_node_identity, area_id="area-b", name="Area B")
    with pytest.raises(FileAreaCarryLimitError):
        materialize_carried_file_area(
            db, second_genesis, own_fingerprint=node_identity.fingerprint, max_carried_file_areas=1
        )
    assert get_file_area_by_name(db, "Area A") is not None


def test_materialize_carried_file_area_idempotent_resend_is_exempt_from_the_cap(db, remote_node_identity, node_identity):
    genesis = _remote_genesis(remote_node_identity)
    first = materialize_carried_file_area(
        db, genesis, own_fingerprint=node_identity.fingerprint, max_carried_file_areas=1
    )
    second = materialize_carried_file_area(
        db, genesis, own_fingerprint=node_identity.fingerprint, max_carried_file_areas=1
    )
    assert first.id == second.id


def test_carried_file_area_count_excludes_self_originated_areas(db, alice, node_identity, remote_node_identity):
    local_area = create_file_area(db, "local-files", creator=alice)
    link_file_area(db, local_area, node_identity=node_identity)  # self-originated -- not "carried"
    materialize_carried_file_area(db, _remote_genesis(remote_node_identity))  # genuinely carried
    assert carried_file_area_count(db, node_identity.fingerprint) == 1


# -- materialize_carried_file_descriptor ------------------------------------


def _carried_area(db, remote_node_identity, *, area_id="remote-area-id"):
    genesis = _remote_genesis(remote_node_identity, area_id=area_id)
    materialize_carried_file_area(db, genesis)
    return area_id


def _remote_descriptor(remote_node_identity, *, area_id="remote-area-id", file_id="remote-file-id", **kwargs):
    return build_file_descriptor(
        signing_identity=remote_node_identity.signing_key,
        area_id=area_id,
        file_id=file_id,
        filename=kwargs.pop("filename", "game.zip"),
        size_bytes=kwargs.pop("size_bytes", 12345),
        sha256=kwargs.pop("sha256", "a" * 64),
        created_at=kwargs.pop("created_at", "2026-01-01T00:00:00Z"),
        **kwargs,
    )


def test_materialize_carried_file_descriptor_creates_a_catalogue_row(db, remote_node_identity):
    area_id = _carried_area(db, remote_node_identity)
    descriptor = _remote_descriptor(remote_node_identity, area_id=area_id)

    remote_file = materialize_carried_file_descriptor(db, descriptor, sender_fingerprint=remote_node_identity.fingerprint)

    assert remote_file is not None
    assert remote_file.file_id == descriptor.payload["file_id"]
    assert remote_file.filename == "game.zip"
    assert remote_file.size_bytes == 12345
    assert remote_file.origin_fingerprint == remote_node_identity.fingerprint
    assert remote_file.fetched_file_id is None

    row = db.connection.execute("SELECT 1 FROM link_events WHERE content_id = ?", (descriptor.content_id,)).fetchone()
    assert row is not None  # the underlying signed event was persisted too


def test_materialize_carried_file_descriptor_is_idempotent(db, remote_node_identity):
    area_id = _carried_area(db, remote_node_identity)
    descriptor = _remote_descriptor(remote_node_identity, area_id=area_id)

    first = materialize_carried_file_descriptor(db, descriptor, sender_fingerprint=remote_node_identity.fingerprint)
    second = materialize_carried_file_descriptor(db, descriptor, sender_fingerprint=remote_node_identity.fingerprint)

    assert first.id == second.id
    assert len(db.connection.execute("SELECT 1 FROM remote_files WHERE file_id = ?", (first.file_id,)).fetchall()) == 1


def test_materialize_carried_file_descriptor_returns_none_if_area_not_locally_carried(db, remote_node_identity):
    # No _carried_area call -- the area was never materialized.
    descriptor = _remote_descriptor(remote_node_identity)
    result = materialize_carried_file_descriptor(db, descriptor, sender_fingerprint=remote_node_identity.fingerprint)
    assert result is None


def test_materialize_carried_file_descriptor_rejects_a_new_file_once_at_cap(db, remote_node_identity):
    area_id = _carried_area(db, remote_node_identity)
    first_descriptor = _remote_descriptor(remote_node_identity, area_id=area_id, file_id="file-a")
    materialize_carried_file_descriptor(
        db, first_descriptor, sender_fingerprint=remote_node_identity.fingerprint, max_remote_files_per_area=1
    )
    second_descriptor = _remote_descriptor(remote_node_identity, area_id=area_id, file_id="file-b")
    with pytest.raises(RemoteFileCatalogueLimitError):
        materialize_carried_file_descriptor(
            db, second_descriptor, sender_fingerprint=remote_node_identity.fingerprint, max_remote_files_per_area=1
        )


def test_materialize_carried_file_descriptor_idempotent_resend_is_exempt_from_the_cap(db, remote_node_identity):
    area_id = _carried_area(db, remote_node_identity)
    descriptor = _remote_descriptor(remote_node_identity, area_id=area_id)
    first = materialize_carried_file_descriptor(
        db, descriptor, sender_fingerprint=remote_node_identity.fingerprint, max_remote_files_per_area=1
    )
    second = materialize_carried_file_descriptor(
        db, descriptor, sender_fingerprint=remote_node_identity.fingerprint, max_remote_files_per_area=1
    )
    assert first.id == second.id


def test_remote_file_count_for_area_counts_only_that_area(db, remote_node_identity):
    genesis_a = _remote_genesis(remote_node_identity, area_id="area-a", name="Area A")
    genesis_b = _remote_genesis(remote_node_identity, area_id="area-b", name="Area B")
    materialize_carried_file_area(db, genesis_a)
    materialize_carried_file_area(db, genesis_b)
    materialize_carried_file_descriptor(
        db, _remote_descriptor(remote_node_identity, area_id="area-a", file_id="fa"),
        sender_fingerprint=remote_node_identity.fingerprint,
    )
    materialize_carried_file_descriptor(
        db, _remote_descriptor(remote_node_identity, area_id="area-b", file_id="fb"),
        sender_fingerprint=remote_node_identity.fingerprint,
    )
    area_a_local = get_file_area_by_name(db, "Area A")
    assert remote_file_count_for_area(db, area_a_local.id) == 1


def test_list_remote_files_and_get_remote_file(db, remote_node_identity):
    area_id = _carried_area(db, remote_node_identity)
    descriptor = _remote_descriptor(remote_node_identity, area_id=area_id)
    materialize_carried_file_descriptor(db, descriptor, sender_fingerprint=remote_node_identity.fingerprint)

    area = get_file_area_by_name(db, "Remote Files")
    listed = list_remote_files(db, area)
    assert [f.file_id for f in listed] == [descriptor.payload["file_id"]]

    fetched = get_remote_file(db, descriptor.payload["file_id"])
    assert fetched is not None
    assert fetched.filename == "game.zip"
    assert get_remote_file(db, "never-catalogued") is None


# -- queue_file_descriptor_if_linked -----------------------------------------


def test_queue_file_descriptor_is_a_noop_when_area_is_not_linked(db, alice, node_identity):
    area = create_file_area(db, "downloads", creator=alice)
    entry = upload_file(db, area, alice, "game.zip", b"content")
    assert queue_file_descriptor_if_linked(db, entry, area, node_identity=node_identity) is None


def test_queue_file_descriptor_is_a_noop_for_a_still_pending_file(db, alice, node_identity):
    area = create_file_area(db, "downloads", creator=alice, moderated=True)
    link_file_area(db, area, node_identity=node_identity)
    entry = upload_file(db, area, alice, "game.zip", b"content")
    assert entry.status == "pending"
    assert queue_file_descriptor_if_linked(db, entry, area, node_identity=node_identity) is None


def test_queue_file_descriptor_builds_and_persists_for_an_approved_file(db, alice, node_identity):
    area = create_file_area(db, "downloads", creator=alice)
    link_file_area(db, area, node_identity=node_identity)
    entry = upload_file(db, area, alice, "game.zip", b"content")
    assert entry.status == "approved"

    descriptor = queue_file_descriptor_if_linked(db, entry, area, node_identity=node_identity)

    assert descriptor is not None
    assert descriptor.payload["file_id"] == entry.file_id
    assert descriptor.payload["sha256"] == entry.sha256
    row = db.connection.execute("SELECT link_event_json FROM files WHERE file_id = ?", (entry.file_id,)).fetchone()
    assert row["link_event_json"] is not None


def test_queue_file_descriptor_is_idempotent(db, alice, node_identity):
    area = create_file_area(db, "downloads", creator=alice)
    link_file_area(db, area, node_identity=node_identity)
    entry = upload_file(db, area, alice, "game.zip", b"content")

    first = queue_file_descriptor_if_linked(db, entry, area, node_identity=node_identity)
    second = queue_file_descriptor_if_linked(db, entry, area, node_identity=node_identity)

    assert first.content_id == second.content_id


# -- load_own_file_area_events ------------------------------------------------


def test_load_own_file_area_events_returns_genesis_and_descriptors(db, alice, node_identity):
    area = create_file_area(db, "downloads", creator=alice)
    genesis = link_file_area(db, area, node_identity=node_identity)
    entry = upload_file(db, area, alice, "game.zip", b"content")
    descriptor = queue_file_descriptor_if_linked(db, entry, area, node_identity=node_identity)

    events = load_own_file_area_events(db, node_identity.fingerprint)

    content_ids = {e.content_id for e in events}
    assert genesis.content_id in content_ids
    assert descriptor.content_id in content_ids


def test_load_own_file_area_events_excludes_carried_areas_genesis(db, alice, node_identity, remote_node_identity):
    materialize_carried_file_area(db, _remote_genesis(remote_node_identity))
    events = load_own_file_area_events(db, node_identity.fingerprint)
    assert events == []


def test_a_peers_description_is_cut_to_a_locally_renderable_shape(db, remote_node_identity):
    """Issue #463: the protocol layer bounds a descriptor's description
    in *bytes*, which still allows thousands of lines of it. The file
    listing renders every line, so remote text is fitted on arrival --
    the same treatment an over-long FILE_ID.DIZ gets, since a wordy peer
    is not a protocol violation."""
    area_id = _carried_area(db, remote_node_identity)
    descriptor = _remote_descriptor(
        remote_node_identity,
        area_id=area_id,
        description="\n".join(f"line {i}" for i in range(500)) + "\x1b[2J",
    )

    remote_file = materialize_carried_file_descriptor(
        db, descriptor, sender_fingerprint=remote_node_identity.fingerprint
    )

    assert len(remote_file.description.split("\n")) == 10
    assert "\x1b" not in remote_file.description


def test_a_carried_area_never_gets_a_descriptor_this_node_signed(db, alice, node_identity, remote_node_identity):
    """Codex review of issue #464: a carried area is a real, writable
    local area, but a peer verifies every `file_descriptor` against the
    *area's genesis origin* key. One signed here would verify nowhere,
    sit on the row permanently, and be re-pushed and re-rejected with
    the rest of its batch."""
    area_id = _carried_area(db, remote_node_identity)
    area = get_file_area_by_name(db, _remote_genesis(remote_node_identity, area_id=area_id).payload["name"])
    entry = upload_file(db, area, alice, "game.zip", b"payload")

    assert is_area_linked(db, area)
    assert file_area_origin_fingerprint(db, area) == remote_node_identity.fingerprint
    assert queue_file_descriptor_if_linked(db, entry, area, node_identity=node_identity) is None
    assert not has_queued_file_descriptor(db, entry)
    assert load_own_file_area_events(db, node_identity.fingerprint) == []


def test_has_queued_file_descriptor_is_about_the_file_not_its_area(db, alice, node_identity):
    """A file approved before its area was Linked has no descriptor and
    never gets one — pre-Link history is deliberately not backfilled —
    so "is this area Linked" is the wrong question to answer with
    (Codex review)."""
    area = create_file_area(db, "docs", creator=alice)
    older = upload_file(db, area, alice, "older.zip", b"payload")
    link_file_area(db, area, node_identity=node_identity)
    area = get_file_area_by_name(db, "docs")
    newer = upload_file(db, area, alice, "newer.zip", b"payload")
    queue_file_descriptor_if_linked(db, newer, area, node_identity=node_identity)

    assert is_area_linked(db, area)
    assert not has_queued_file_descriptor(db, older)
    assert has_queued_file_descriptor(db, newer)


def test_a_file_no_peer_would_accept_is_not_announced(db, alice, node_identity):
    """Codex review: a descriptor a peer refuses is not a harmless
    no-op — it is stored on the row permanently and re-pushed on every
    pass, where it takes down the whole request it travels in."""
    area = create_file_area(db, "docs", creator=alice)
    link_file_area(db, area, node_identity=node_identity)
    area = get_file_area_by_name(db, "docs")

    # A filename inside the local 255-*character* cap but past the
    # descriptor's 255-*byte* one.
    entry = upload_file(db, area, alice, "\u00e4" * 200 + ".zip", b"payload")
    assert queue_file_descriptor_if_linked(db, entry, area, node_identity=node_identity) is None
    assert not has_queued_file_descriptor(db, entry)

    ordinary = upload_file(db, area, alice, "fine.zip", b"payload")
    assert queue_file_descriptor_if_linked(db, ordinary, area, node_identity=node_identity) is not None


def test_a_file_larger_than_a_catalogue_entry_may_claim_is_not_announced(db, alice, node_identity):
    from netbbs.link.protocol import MAX_CATALOGUED_FILE_SIZE_BYTES

    area = create_file_area(db, "docs", creator=alice)
    link_file_area(db, area, node_identity=node_identity)
    area = get_file_area_by_name(db, "docs")
    entry = upload_file(db, area, alice, "huge.bin", b"payload")
    # Rewritten rather than actually uploading ten gigabytes.
    db.connection.execute(
        "UPDATE files SET size_bytes = ? WHERE file_id = ?",
        (MAX_CATALOGUED_FILE_SIZE_BYTES + 1, entry.file_id),
    )
    db.connection.commit()

    assert queue_file_descriptor_if_linked(
        db, get_file(db, entry.file_id), area, node_identity=node_identity
    ) is None
