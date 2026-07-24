"""
Tests for `netbbs.link.file_transfer` -- the on-demand, bounded,
resumable chunk-transfer mechanism (design doc §11.3, issue #89). All
`db`-first, no real network I/O (see `tests/test_link_transport.py` for
the real-socket counterpart, and `tests/test_link_end_to_end.py` for the
full vertical slice) -- this file proves the transfer-state machine
itself: partial transfer + resume, duplicate chunk requests, and
rejection of malformed/oversized claims, per the issue's own acceptance
criteria.
"""

from __future__ import annotations

import hashlib
import os

import pytest

from netbbs.auth.users import create_user
from netbbs.files.areas import create_file_area
from netbbs.files.entries import upload_file
from netbbs.link.file_transfer import (
    FileTransferError,
    apply_received_chunk,
    build_chunk_for_serving,
    compute_transfer_id,
    get_or_create_transfer,
)
from netbbs.link.files import (
    link_file_area,
    materialize_carried_file_area,
    materialize_carried_file_descriptor,
    queue_file_descriptor_if_linked,
)
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.storage.database import Database


@pytest.fixture
def origin_db(tmp_path):
    database = Database(tmp_path / "origin.db")
    yield database
    database.close()


@pytest.fixture
def puller_db(tmp_path):
    database = Database(tmp_path / "puller.db")
    yield database
    database.close()


@pytest.fixture
def origin_identity():
    return bootstrap_node_identity("origin")


@pytest.fixture
def puller_identity():
    return bootstrap_node_identity("puller")


def _linked_remote_file(origin_db, puller_db, origin_identity, puller_identity, *, content: bytes, chunk_size=None):
    """Sets up origin_db with a real uploaded file on a Linked area, and
    puller_db with the corresponding catalogued (not yet fetched)
    RemoteFile -- the common setup every chunk-transfer test needs."""
    creator = create_user(origin_db, "alice", password="hunter2", user_level=10)
    area = create_file_area(origin_db, "files", creator=creator)
    entry = upload_file(origin_db, area, creator, "game.bin", content)
    link_file_area(origin_db, area, node_identity=origin_identity)
    descriptor = queue_file_descriptor_if_linked(origin_db, entry, area, node_identity=origin_identity)

    genesis_row = origin_db.connection.execute(
        "SELECT link_genesis_json FROM file_areas WHERE id = ?", (area.id,)
    ).fetchone()
    import json

    from netbbs.link.events import FileAreaGenesis

    genesis = FileAreaGenesis.from_dict(json.loads(genesis_row["link_genesis_json"]))
    materialize_carried_file_area(puller_db, genesis, own_fingerprint=puller_identity.fingerprint)
    remote_file = materialize_carried_file_descriptor(
        puller_db, descriptor, sender_fingerprint=origin_identity.fingerprint
    )
    return entry, remote_file


def _drive_transfer(origin_db, puller_db, remote_file, *, requester_fingerprint, chunk_size):
    """Fetches every remaining chunk in order, mirroring exactly what
    `netbbs.link.transport.fetch_next_file_chunk` does over real HTTP,
    minus the network hop and signature verification (covered
    separately in test_link_transport.py/test_link_end_to_end.py)."""
    transfer = get_or_create_transfer(
        puller_db, remote_file, requester_fingerprint=requester_fingerprint, chunk_size=chunk_size
    )
    while transfer.status == "in_progress":
        idx = transfer.next_chunk_index
        chunk_bytes, _chunk_size, _total_size, is_last = build_chunk_for_serving(
            origin_db, file_id=remote_file.file_id, chunk_index=idx, max_chunk_size=transfer.chunk_size,
        )
        chunk_sha256 = hashlib.sha256(chunk_bytes).hexdigest()
        transfer = apply_received_chunk(
            puller_db, transfer, chunk_index=idx, chunk_bytes=chunk_bytes,
            claimed_chunk_sha256=chunk_sha256, is_last=is_last, remote_file=remote_file,
        )
    return transfer


def test_compute_transfer_id_is_deterministic(origin_db, puller_db, origin_identity, puller_identity):
    entry, remote_file = _linked_remote_file(
        origin_db, puller_db, origin_identity, puller_identity, content=b"hello world"
    )
    first = compute_transfer_id(remote_file.file_id, puller_identity.fingerprint)
    second = compute_transfer_id(remote_file.file_id, puller_identity.fingerprint)
    assert first == second


def test_get_or_create_transfer_is_idempotent(origin_db, puller_db, origin_identity, puller_identity):
    entry, remote_file = _linked_remote_file(
        origin_db, puller_db, origin_identity, puller_identity, content=b"hello world"
    )
    first = get_or_create_transfer(puller_db, remote_file, requester_fingerprint=puller_identity.fingerprint)
    second = get_or_create_transfer(puller_db, remote_file, requester_fingerprint=puller_identity.fingerprint)
    assert first.transfer_id == second.transfer_id
    assert first.id == second.id


def test_zero_byte_file_starts_completed(origin_db, puller_db, origin_identity, puller_identity):
    entry, remote_file = _linked_remote_file(origin_db, puller_db, origin_identity, puller_identity, content=b"")
    transfer = get_or_create_transfer(puller_db, remote_file, requester_fingerprint=puller_identity.fingerprint)
    assert transfer.status == "completed"
    assert transfer.next_chunk_index is None


def test_full_transfer_across_multiple_chunks_reassembles_correctly(
    origin_db, puller_db, origin_identity, puller_identity
):
    content = os.urandom(300_000)  # larger than one small test chunk
    entry, remote_file = _linked_remote_file(
        origin_db, puller_db, origin_identity, puller_identity, content=content
    )

    transfer = get_or_create_transfer(
        puller_db, remote_file, requester_fingerprint=puller_identity.fingerprint, chunk_size=100_000,
    )
    assert transfer.status == "in_progress"

    final = _drive_transfer(
        origin_db, puller_db, remote_file, requester_fingerprint=puller_identity.fingerprint, chunk_size=100_000,
    )

    assert final.status == "completed"
    assert final.bytes_received == len(content)
    row = puller_db.connection.execute("SELECT * FROM files WHERE file_id = ?", (remote_file.file_id,)).fetchone()
    assert row is not None
    from pathlib import Path

    assert Path(row["storage_path"]).read_bytes() == content
    remote_row = puller_db.connection.execute(
        "SELECT fetched_file_id FROM remote_files WHERE file_id = ?", (remote_file.file_id,)
    ).fetchone()
    assert remote_row["fetched_file_id"] == remote_file.file_id


def test_partial_transfer_resumes_from_the_next_expected_chunk(
    origin_db, puller_db, origin_identity, puller_identity
):
    content = os.urandom(300_000)
    entry, remote_file = _linked_remote_file(
        origin_db, puller_db, origin_identity, puller_identity, content=content
    )
    transfer = get_or_create_transfer(
        puller_db, remote_file, requester_fingerprint=puller_identity.fingerprint, chunk_size=100_000,
    )

    # Apply only the first chunk, simulating an interrupted transfer.
    chunk_bytes, _cs, _ts, is_last = build_chunk_for_serving(
        origin_db, file_id=remote_file.file_id, chunk_index=0, max_chunk_size=transfer.chunk_size,
    )
    transfer = apply_received_chunk(
        puller_db, transfer, chunk_index=0, chunk_bytes=chunk_bytes,
        claimed_chunk_sha256=hashlib.sha256(chunk_bytes).hexdigest(), is_last=is_last, remote_file=remote_file,
    )
    assert transfer.status == "in_progress"
    assert transfer.bytes_received == 100_000

    # "Resume" -- get_or_create_transfer against the same remote_file/
    # requester finds the same row, already partway through.
    resumed = get_or_create_transfer(
        puller_db, remote_file, requester_fingerprint=puller_identity.fingerprint, chunk_size=100_000,
    )
    assert resumed.transfer_id == transfer.transfer_id
    assert resumed.next_chunk_index == 1

    final = _drive_transfer(
        origin_db, puller_db, remote_file, requester_fingerprint=puller_identity.fingerprint, chunk_size=100_000,
    )
    assert final.status == "completed"
    row = puller_db.connection.execute("SELECT * FROM files WHERE file_id = ?", (remote_file.file_id,)).fetchone()
    from pathlib import Path

    assert Path(row["storage_path"]).read_bytes() == content


def test_duplicate_chunk_request_is_idempotent_and_does_not_corrupt_reassembly(
    origin_db, puller_db, origin_identity, puller_identity
):
    """The acceptance criterion's own 'duplicate chunk requests' case --
    re-applying the same already-received chunk_index must be a safe
    no-op, never double-appended to the staging file (which would
    otherwise corrupt the eventual reassembly)."""
    content = os.urandom(300_000)
    entry, remote_file = _linked_remote_file(
        origin_db, puller_db, origin_identity, puller_identity, content=content
    )
    transfer = get_or_create_transfer(
        puller_db, remote_file, requester_fingerprint=puller_identity.fingerprint, chunk_size=100_000,
    )

    chunk_bytes, _cs, _ts, is_last = build_chunk_for_serving(
        origin_db, file_id=remote_file.file_id, chunk_index=0, max_chunk_size=transfer.chunk_size,
    )
    chunk_sha256 = hashlib.sha256(chunk_bytes).hexdigest()
    transfer = apply_received_chunk(
        puller_db, transfer, chunk_index=0, chunk_bytes=chunk_bytes,
        claimed_chunk_sha256=chunk_sha256, is_last=is_last, remote_file=remote_file,
    )
    bytes_after_first = transfer.bytes_received

    # The exact same chunk_index requested/applied again.
    transfer = apply_received_chunk(
        puller_db, transfer, chunk_index=0, chunk_bytes=chunk_bytes,
        claimed_chunk_sha256=chunk_sha256, is_last=is_last, remote_file=remote_file,
    )
    assert transfer.bytes_received == bytes_after_first  # unchanged, not double-counted

    final = _drive_transfer(
        origin_db, puller_db, remote_file, requester_fingerprint=puller_identity.fingerprint, chunk_size=100_000,
    )
    assert final.status == "completed"
    row = puller_db.connection.execute("SELECT * FROM files WHERE file_id = ?", (remote_file.file_id,)).fetchone()
    from pathlib import Path

    assert Path(row["storage_path"]).read_bytes() == content  # not corrupted by the duplicate


def test_apply_received_chunk_rejects_content_not_matching_its_own_claimed_hash(
    origin_db, puller_db, origin_identity, puller_identity
):
    entry, remote_file = _linked_remote_file(
        origin_db, puller_db, origin_identity, puller_identity, content=b"x" * 500_000
    )
    transfer = get_or_create_transfer(
        puller_db, remote_file, requester_fingerprint=puller_identity.fingerprint, chunk_size=100_000,
    )
    chunk_bytes, _cs, _ts, is_last = build_chunk_for_serving(
        origin_db, file_id=remote_file.file_id, chunk_index=0, max_chunk_size=transfer.chunk_size,
    )

    with pytest.raises(FileTransferError):
        apply_received_chunk(
            puller_db, transfer, chunk_index=0, chunk_bytes=chunk_bytes,
            claimed_chunk_sha256="0" * 64,  # doesn't match chunk_bytes' real hash
            is_last=is_last, remote_file=remote_file,
        )


def test_apply_received_chunk_rejects_out_of_order_delivery(origin_db, puller_db, origin_identity, puller_identity):
    entry, remote_file = _linked_remote_file(
        origin_db, puller_db, origin_identity, puller_identity, content=b"x" * 500_000
    )
    transfer = get_or_create_transfer(
        puller_db, remote_file, requester_fingerprint=puller_identity.fingerprint, chunk_size=100_000,
    )
    # Request chunk_index 1 (a real, validly-served chunk) before chunk 0
    # has ever been applied -- the transfer still expects 0 first.
    chunk_bytes, _cs, _ts, is_last = build_chunk_for_serving(
        origin_db, file_id=remote_file.file_id, chunk_index=1, max_chunk_size=transfer.chunk_size,
    )
    with pytest.raises(FileTransferError):
        apply_received_chunk(
            puller_db, transfer, chunk_index=1, chunk_bytes=chunk_bytes,
            claimed_chunk_sha256=hashlib.sha256(chunk_bytes).hexdigest(), is_last=is_last, remote_file=remote_file,
        )


def test_finalize_rejects_a_reassembly_not_matching_the_catalogued_hash(
    origin_db, puller_db, origin_identity, puller_identity
):
    """A peer whose actual bytes don't match its own file_descriptor's
    claimed sha256 is refused at reassembly time, never silently
    accepted (design doc §11.3)."""
    entry, remote_file = _linked_remote_file(
        origin_db, puller_db, origin_identity, puller_identity, content=b"y" * 1000
    )
    # Tamper with the catalogued hash the puller believes is correct,
    # simulating a dishonest/broken origin whose descriptor doesn't
    # match what it actually serves.
    from dataclasses import replace

    tampered_remote_file = replace(remote_file, sha256="0" * 64)

    transfer = get_or_create_transfer(
        puller_db, tampered_remote_file, requester_fingerprint=puller_identity.fingerprint, chunk_size=100_000,
    )
    chunk_bytes, _cs, _ts, is_last = build_chunk_for_serving(
        origin_db, file_id=remote_file.file_id, chunk_index=0, max_chunk_size=transfer.chunk_size,
    )
    assert is_last  # the whole (small) file fits in one chunk

    with pytest.raises(FileTransferError):
        apply_received_chunk(
            puller_db, transfer, chunk_index=0, chunk_bytes=chunk_bytes,
            claimed_chunk_sha256=hashlib.sha256(chunk_bytes).hexdigest(), is_last=is_last,
            remote_file=tampered_remote_file,
        )
    failed = get_or_create_transfer(
        puller_db, tampered_remote_file, requester_fingerprint=puller_identity.fingerprint, chunk_size=100_000,
    )
    assert failed.status == "failed"


def test_build_chunk_for_serving_rejects_unknown_file_id(origin_db, puller_db, origin_identity, puller_identity):
    _linked_remote_file(origin_db, puller_db, origin_identity, puller_identity, content=b"hello")
    with pytest.raises(FileTransferError):
        build_chunk_for_serving(origin_db, file_id="never-uploaded-file-id", chunk_index=0, max_chunk_size=1024)


def test_build_chunk_for_serving_rejects_an_out_of_range_chunk_index(
    origin_db, puller_db, origin_identity, puller_identity
):
    entry, remote_file = _linked_remote_file(origin_db, puller_db, origin_identity, puller_identity, content=b"hi")
    with pytest.raises(FileTransferError):
        build_chunk_for_serving(origin_db, file_id=remote_file.file_id, chunk_index=99, max_chunk_size=1024)


def test_build_chunk_for_serving_marks_the_final_chunk(origin_db, puller_db, origin_identity, puller_identity):
    entry, remote_file = _linked_remote_file(
        origin_db, puller_db, origin_identity, puller_identity, content=b"x" * 10
    )
    chunk_bytes, chunk_size, total_size, is_last = build_chunk_for_serving(
        origin_db, file_id=remote_file.file_id, chunk_index=0, max_chunk_size=1024,
    )
    assert chunk_bytes == b"x" * 10
    assert total_size == 10
    assert is_last is True
