"""Tests for netbbs.files.storage — content-addressed filesystem storage
for uploaded file bytes."""

from __future__ import annotations

import hashlib

from netbbs.files.storage import (
    compute_sha256,
    move_temp_file_into_storage,
    new_incoming_temp_path,
    read_bytes,
    storage_root,
    store_bytes,
)
from netbbs.storage.database import Database


def test_storage_root_is_derived_from_db_path(tmp_path):
    db = Database(tmp_path / "netbbs.db")
    assert storage_root(db) == tmp_path / "netbbs_files"
    db.close()


def test_compute_sha256_matches_stdlib(tmp_path):
    data = b"hello world"
    assert compute_sha256(data) == hashlib.sha256(data).hexdigest()


def test_store_bytes_writes_file_and_returns_hash_and_path(tmp_path):
    db = Database(tmp_path / "netbbs.db")
    sha256, path = store_bytes(db, b"hello world")
    assert sha256 == hashlib.sha256(b"hello world").hexdigest()
    assert path.exists()
    assert path.read_bytes() == b"hello world"
    db.close()


def test_store_bytes_shards_by_first_two_hex_characters(tmp_path):
    db = Database(tmp_path / "netbbs.db")
    sha256, path = store_bytes(db, b"hello world")
    assert path.parent.name == sha256[:2]
    assert path.name == sha256
    db.close()


def test_store_bytes_is_idempotent_for_identical_content(tmp_path):
    db = Database(tmp_path / "netbbs.db")
    _, path_a = store_bytes(db, b"hello world")
    _, path_b = store_bytes(db, b"hello world")
    assert path_a == path_b
    assert path_a.read_bytes() == b"hello world"
    db.close()


def test_store_bytes_different_content_gets_different_paths(tmp_path):
    db = Database(tmp_path / "netbbs.db")
    _, path_a = store_bytes(db, b"content a")
    _, path_b = store_bytes(db, b"content b")
    assert path_a != path_b
    db.close()


def test_read_bytes_roundtrip(tmp_path):
    db = Database(tmp_path / "netbbs.db")
    _, path = store_bytes(db, b"roundtrip me")
    assert read_bytes(path) == b"roundtrip me"
    db.close()


# -- GitHub issue #34: streaming receive path (no complete bytes in memory) --


def test_new_incoming_temp_path_returns_a_fresh_path_under_storage_root(tmp_path):
    db = Database(tmp_path / "netbbs.db")
    path = new_incoming_temp_path(db)
    assert path.parent.parent == storage_root(db)
    assert path.parent.name == ".incoming"
    assert not path.exists()  # caller is expected to create/write it themselves
    db.close()


def test_new_incoming_temp_path_returns_a_different_path_each_call(tmp_path):
    db = Database(tmp_path / "netbbs.db")
    assert new_incoming_temp_path(db) != new_incoming_temp_path(db)
    db.close()


def test_move_temp_file_into_storage_places_content_at_its_hash_path(tmp_path):
    db = Database(tmp_path / "netbbs.db")
    temp_path = new_incoming_temp_path(db)
    temp_path.write_bytes(b"hello world")
    sha256 = hashlib.sha256(b"hello world").hexdigest()

    final_path = move_temp_file_into_storage(db, temp_path, sha256)

    assert final_path == storage_root(db) / sha256[:2] / sha256
    assert final_path.read_bytes() == b"hello world"
    assert not temp_path.exists()  # moved, not copied
    db.close()


def test_move_temp_file_into_storage_matches_store_bytes_for_identical_content(tmp_path):
    """The streaming and non-streaming paths must be interchangeable --
    identical content ends up at the identical final path regardless of
    which one wrote it."""
    db = Database(tmp_path / "netbbs.db")
    expected_sha256, expected_path = store_bytes(db, b"hello world")

    temp_path = new_incoming_temp_path(db)
    temp_path.write_bytes(b"hello world")
    final_path = move_temp_file_into_storage(db, temp_path, expected_sha256)

    assert final_path == expected_path
    db.close()


def test_move_temp_file_into_storage_discards_the_temp_file_when_already_stored(tmp_path):
    db = Database(tmp_path / "netbbs.db")
    store_bytes(db, b"hello world")  # already stored once
    sha256 = hashlib.sha256(b"hello world").hexdigest()

    temp_path = new_incoming_temp_path(db)
    temp_path.write_bytes(b"hello world")  # a second, independent upload of the same content
    final_path = move_temp_file_into_storage(db, temp_path, sha256)

    assert final_path.read_bytes() == b"hello world"
    assert not temp_path.exists()  # discarded, not left behind as a duplicate
    db.close()
