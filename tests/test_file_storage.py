"""Tests for netbbs.files.storage — content-addressed filesystem storage
for uploaded file bytes."""

from __future__ import annotations

import hashlib

from netbbs.files.storage import compute_sha256, read_bytes, storage_root, store_bytes
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
