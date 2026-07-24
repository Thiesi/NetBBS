"""
Tests for the interactive remote-file-catalogue browse/fetch UI (design
doc, issue #92) -- `netbbs.net.file_flow._show_area`'s `/remote` command
and `_browse_remote_files`/`_fetch_remote_file`. The full real-transport
browse -> fetch -> verify/promote -> ordinary download scenario lives in
`tests/test_link_end_to_end.py` (needs a real second node to fetch from);
this file covers the UI-level edge cases that don't need one: no
catalogue entries yet, an already-fetched entry, cancelling the fetch
prompt, and an unreachable origin.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.files.areas import create_file_area
from netbbs.link.boards import LinkContext
from netbbs.link.files import link_file_area, materialize_carried_file_area, materialize_carried_file_descriptor
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import LinkNode
from netbbs.net import file_flow
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_chat_flow_picker_authorization import FakeSession


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def node_identity():
    return bootstrap_node_identity("thisnode")


@pytest.fixture
def remote_node_identity():
    return bootstrap_node_identity("elsewhere")


def _link_context_for(node_identity) -> LinkContext:
    return LinkContext(node_identity=node_identity, link_node=LinkNode(identity=node_identity))


def _written(session: FakeSession) -> str:
    return "".join(session.written)


def test_remote_hint_hidden_without_link_context(db, lane, alice):
    area = create_file_area(db, "downloads", creator=alice)
    session = FakeSession(["b"])

    asyncio.run(file_flow._show_area(session, lane, area, alice))

    assert "/remote" not in _written(session)


def test_remote_command_reports_no_catalogue_entries(db, lane, alice, node_identity):
    area = create_file_area(db, "downloads", creator=alice)
    link_file_area(db, area, node_identity=node_identity)
    link_context = _link_context_for(node_identity)
    session = FakeSession(["/remote"])

    asyncio.run(file_flow._show_area(session, lane, area, alice, link_context=link_context))

    assert "has no remote catalogue entries" in _written(session)


def test_remote_hint_shown_even_with_zero_local_uploads(db, lane, alice, node_identity):
    """A Linked area can have remote catalogue entries even with zero
    *local* uploads of its own -- /remote must be reachable from the
    'has no files yet' fallback prompt, not just the paginated listing."""
    area = create_file_area(db, "downloads", creator=alice)
    link_file_area(db, area, node_identity=node_identity)
    link_context = _link_context_for(node_identity)
    session = FakeSession(["/remote"])

    asyncio.run(file_flow._show_area(session, lane, area, alice, link_context=link_context))

    assert "has no files yet" in _written(session)
    assert "/remote" in _written(session)
    assert "has no remote catalogue entries" in _written(session)


def _carried_area_with_one_remote_file(db, node_identity, remote_node_identity, *, size_bytes=1000):
    from netbbs.link.events import build_file_area_genesis, build_file_descriptor

    genesis = build_file_area_genesis(
        signing_identity=remote_node_identity.signing_key,
        origin_fingerprint=remote_node_identity.fingerprint,
        area_id="remote-area-id",
        name="Remote Downloads",
        created_at="2026-01-01T00:00:00.000000Z",
    )
    area = materialize_carried_file_area(db, genesis)
    descriptor = build_file_descriptor(
        signing_identity=remote_node_identity.signing_key,
        area_id="remote-area-id",
        file_id="remote-file-content-id",
        filename="game.bin",
        size_bytes=size_bytes,
        sha256="a" * 64,
        created_at="2026-01-01T00:00:00.000000Z",
    )
    remote_file = materialize_carried_file_descriptor(db, descriptor, sender_fingerprint=remote_node_identity.fingerprint)
    return area, remote_file


def test_remote_command_lists_a_catalogued_but_not_yet_fetched_file(db, lane, alice, node_identity, remote_node_identity):
    area, remote_file = _carried_area_with_one_remote_file(db, node_identity, remote_node_identity)
    link_context = _link_context_for(node_identity)
    # /remote -> picker shows item 01 -> pick it -> decline the fetch prompt
    session = FakeSession(["/remote", "0", "1", "n"])

    asyncio.run(file_flow._show_area(session, lane, area, alice, link_context=link_context))

    output = _written(session)
    assert "game.bin" in output
    assert "not yet fetched" in output
    assert "Cancelled" in output


def test_remote_command_reports_an_already_fetched_entry_without_offering_to_fetch(
    db, lane, alice, node_identity, remote_node_identity
):
    area, remote_file = _carried_area_with_one_remote_file(db, node_identity, remote_node_identity)
    # Simulate a completed fetch: a real files row must exist first,
    # satisfying remote_files.fetched_file_id's own foreign key.
    db.connection.execute(
        """
        INSERT INTO files
            (file_id, area_id, filename, description, size_bytes, sha256, storage_path,
             uploader_user_id, uploader_label, uploader_fingerprint, created_at, status)
        VALUES (?, ?, ?, NULL, ?, ?, ?, NULL, ?, ?, ?, 'approved')
        """,
        (
            remote_file.file_id, area.id, remote_file.filename, remote_file.size_bytes, remote_file.sha256,
            f"/tmp/{remote_file.file_id}", f"remote@{remote_node_identity.fingerprint}",
            remote_node_identity.fingerprint, remote_file.created_at,
        ),
    )
    db.connection.execute(
        "UPDATE remote_files SET fetched_file_id = ? WHERE file_id = ?", (remote_file.file_id, remote_file.file_id)
    )
    db.connection.commit()
    link_context = _link_context_for(node_identity)
    session = FakeSession(["/remote", "0", "1"])

    asyncio.run(file_flow._show_area(session, lane, area, alice, link_context=link_context))

    output = _written(session)
    assert "already available locally" in output
    assert "Fetch it from its origin now?" not in output


def test_fetch_reports_an_unreachable_origin(db, lane, alice, node_identity, remote_node_identity):
    """remote_node_identity is never registered as a peer at all here --
    the origin has no completed hello, let alone a dialable address, so
    the fetch must fail clearly rather than hang or crash."""
    area, remote_file = _carried_area_with_one_remote_file(db, node_identity, remote_node_identity)
    link_context = _link_context_for(node_identity)
    session = FakeSession(["/remote", "0", "1", "y"])

    asyncio.run(file_flow._show_area(session, lane, area, alice, link_context=link_context))

    output = _written(session)
    assert "not currently reachable directly" in output
    row = db.connection.execute(
        "SELECT fetched_file_id FROM remote_files WHERE file_id = ?", (remote_file.file_id,)
    ).fetchone()
    assert row["fetched_file_id"] is None
