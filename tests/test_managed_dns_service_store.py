"""Tests for services.managed_dns.store (issue #201)."""

from __future__ import annotations

import sqlite3

import pytest

from services.managed_dns.store import (
    Database,
    count_registrations,
    count_registrations_for_node,
    get_registration_by_credential_hash,
    get_registration_by_name,
    hash_credential,
    insert_registration,
)


def test_hash_credential_is_deterministic_and_not_the_raw_secret():
    digest = hash_credential("super-secret-token")
    assert digest == hash_credential("super-secret-token")
    assert digest != "super-secret-token"


def test_insert_and_get_registration_roundtrip(tmp_path):
    db = Database(tmp_path / "managed_dns.db")
    registration = insert_registration(
        db, name="myboard", credential_hash=hash_credential("secret1"),
        node_fingerprint="node-fp-1", dynamic=True, created_at="2026-09-02T00:00:00+00:00",
    )
    assert registration.name == "myboard"
    assert registration.status == "pending"
    assert registration.dynamic is True
    assert registration.matured_at is None
    assert registration.last_contact_at is None
    assert registration.released_at is None

    fetched = get_registration_by_name(db, "myboard")
    assert fetched == registration
    db.close()


def test_get_registration_by_name_returns_none_when_absent(tmp_path):
    db = Database(tmp_path / "managed_dns.db")
    assert get_registration_by_name(db, "nope") is None
    db.close()


def test_get_registration_by_credential_hash_roundtrip(tmp_path):
    db = Database(tmp_path / "managed_dns.db")
    credential_hash = hash_credential("secret1")
    insert_registration(
        db, name="myboard", credential_hash=credential_hash,
        node_fingerprint="node-fp-1", dynamic=False, created_at="2026-09-02T00:00:00+00:00",
    )
    fetched = get_registration_by_credential_hash(db, credential_hash)
    assert fetched is not None
    assert fetched.name == "myboard"
    db.close()


def test_get_registration_by_credential_hash_returns_none_for_a_wrong_secret(tmp_path):
    db = Database(tmp_path / "managed_dns.db")
    insert_registration(
        db, name="myboard", credential_hash=hash_credential("secret1"),
        node_fingerprint="node-fp-1", dynamic=False, created_at="2026-09-02T00:00:00+00:00",
    )
    assert get_registration_by_credential_hash(db, hash_credential("wrong-secret")) is None
    db.close()


def test_insert_registration_rejects_a_name_already_taken(tmp_path):
    """First-come-first-served (design doc §16 Decision 3) enforced at
    the database level via the primary key, not a check-then-insert
    race a concurrent caller could slip through."""
    db = Database(tmp_path / "managed_dns.db")
    insert_registration(
        db, name="myboard", credential_hash=hash_credential("secret1"),
        node_fingerprint="node-fp-1", dynamic=False, created_at="2026-09-02T00:00:00+00:00",
    )
    with pytest.raises(sqlite3.IntegrityError):
        insert_registration(
            db, name="myboard", credential_hash=hash_credential("secret2"),
            node_fingerprint="node-fp-2", dynamic=False, created_at="2026-09-02T00:01:00+00:00",
        )
    db.close()


def test_count_registrations_for_node_only_counts_given_statuses(tmp_path):
    db = Database(tmp_path / "managed_dns.db")
    insert_registration(
        db, name="board-a", credential_hash=hash_credential("secret-a"),
        node_fingerprint="node-fp-1", dynamic=False, created_at="2026-09-02T00:00:00+00:00",
    )
    db.connection.execute("UPDATE registrations SET status = 'released' WHERE name = 'board-a'")
    insert_registration(
        db, name="board-b", credential_hash=hash_credential("secret-b"),
        node_fingerprint="node-fp-1", dynamic=False, created_at="2026-09-02T00:01:00+00:00",
    )
    db.connection.commit()

    assert count_registrations_for_node(db, "node-fp-1", statuses=("pending", "matured")) == 1
    assert count_registrations_for_node(db, "node-fp-1", statuses=("released",)) == 1
    assert count_registrations_for_node(db, "node-fp-1", statuses=("pending", "matured", "released")) == 2
    assert count_registrations_for_node(db, "node-fp-nonexistent", statuses=("pending", "matured")) == 0
    db.close()


def test_count_registrations_is_service_wide(tmp_path):
    db = Database(tmp_path / "managed_dns.db")
    insert_registration(
        db, name="board-a", credential_hash=hash_credential("secret-a"),
        node_fingerprint="node-fp-1", dynamic=False, created_at="2026-09-02T00:00:00+00:00",
    )
    insert_registration(
        db, name="board-b", credential_hash=hash_credential("secret-b"),
        node_fingerprint="node-fp-2", dynamic=False, created_at="2026-09-02T00:01:00+00:00",
    )
    assert count_registrations(db, statuses=("pending", "matured")) == 2
    db.close()


def test_status_check_constraint_rejects_an_invalid_status(tmp_path):
    db = Database(tmp_path / "managed_dns.db")
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            """
            INSERT INTO registrations
                (name, credential_hash, node_fingerprint, status, dynamic, created_at)
            VALUES ('bad', 'hash', 'fp', 'not-a-real-status', 0, '2026-09-02T00:00:00+00:00')
            """
        )
    db.close()


def test_migrations_are_idempotent_across_reopen(tmp_path):
    """Reopening an already-migrated database file must not re-run or
    fail migrations -- the same `PRAGMA user_version`-gated behavior
    `netbbs.storage.database.Database` already relies on."""
    path = tmp_path / "managed_dns.db"
    db1 = Database(path)
    insert_registration(
        db1, name="myboard", credential_hash=hash_credential("secret1"),
        node_fingerprint="node-fp-1", dynamic=False, created_at="2026-09-02T00:00:00+00:00",
    )
    db1.close()

    db2 = Database(path)
    assert get_registration_by_name(db2, "myboard") is not None
    db2.close()
