"""Tests for services.managed_dns.store (issue #201)."""

from __future__ import annotations

import sqlite3

import pytest

from services.managed_dns.store import (
    cancel_pending_replacement,
    Database,
    MIGRATIONS,
    count_registrations,
    count_registrations_for_node,
    delete_expired_registrations,
    delete_registration,
    get_registration_by_credential_hash,
    get_registration_by_name,
    hash_credential,
    insert_registration,
    list_stale_active_registrations,
    mark_abandoned,
    mark_matured,
    mark_released,
    reclaim,
    set_last_known_address,
    set_contact_window,
    set_last_contact_at,
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


def test_contact_window_migration_preserves_pending_registration_history(tmp_path):
    path = tmp_path / "managed_dns.db"
    connection = sqlite3.connect(path)
    connection.executescript(MIGRATIONS[0].sql)
    connection.execute("PRAGMA user_version = 1")
    connection.executemany(
        """
        INSERT INTO registrations
            (name, credential_hash, node_fingerprint, status, dynamic,
             created_at, last_contact_at)
        VALUES (?, ?, ?, ?, 0, ?, ?)
        """,
        [
            ("contacted", "hash-1", "fp-1", "pending", "2026-09-01T00:00:00+00:00",
             "2026-09-01T23:00:00+00:00"),
            ("never-contacted", "hash-2", "fp-2", "pending",
             "2026-09-01T00:00:00+00:00", None),
            ("released-before-maturation", "hash-3", "fp-3", "released",
             "2026-09-01T00:00:00+00:00", "2026-09-01T23:00:00+00:00"),
        ],
    )
    connection.commit()
    connection.close()

    db = Database(path)
    assert get_registration_by_name(db, "contacted").contact_started_at == (
        "2026-09-01T00:00:00+00:00"
    )
    assert get_registration_by_name(db, "never-contacted").contact_started_at is None
    assert get_registration_by_name(
        db, "released-before-maturation"
    ).contact_started_at == "2026-09-01T00:00:00+00:00"
    assert db.connection.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    db.close()


# -- release / reclaim / abandonment (issue #201 Phase 4) ------------------


def _insert(db, name="myboard", **overrides):
    kwargs = dict(
        name=name, credential_hash=hash_credential(f"secret-{name}"),
        node_fingerprint="fp-1", dynamic=False, created_at="2026-09-02T00:00:00+00:00",
    )
    kwargs.update(overrides)
    return insert_registration(db, **kwargs)


def test_mark_released_transitions_a_pending_registration(tmp_path):
    db = Database(tmp_path / "managed_dns.db")
    _insert(db)
    mark_released(db, "myboard", released_at="2026-09-02T01:00:00+00:00")
    registration = get_registration_by_name(db, "myboard")
    assert registration.status == "released"
    assert registration.released_at == "2026-09-02T01:00:00+00:00"
    db.close()


def test_mark_released_transitions_a_matured_registration(tmp_path):
    db = Database(tmp_path / "managed_dns.db")
    _insert(db)
    mark_matured(db, "myboard", matured_at="2026-09-02T00:30:00+00:00")
    mark_released(db, "myboard", released_at="2026-09-02T01:00:00+00:00")
    registration = get_registration_by_name(db, "myboard")
    assert registration.status == "released"
    assert registration.matured_at == "2026-09-02T00:30:00+00:00"  # preserved, not cleared
    db.close()


def test_mark_released_is_a_no_op_on_an_already_released_registration(tmp_path):
    db = Database(tmp_path / "managed_dns.db")
    _insert(db)
    mark_released(db, "myboard", released_at="2026-09-02T01:00:00+00:00")
    mark_released(db, "myboard", released_at="2026-09-02T02:00:00+00:00")  # must not clobber the first
    registration = get_registration_by_name(db, "myboard")
    assert registration.released_at == "2026-09-02T01:00:00+00:00"
    db.close()


def test_reclaim_restores_pending_when_never_matured(tmp_path):
    db = Database(tmp_path / "managed_dns.db")
    _insert(db)
    mark_released(db, "myboard", released_at="2026-09-02T01:00:00+00:00")
    reclaim(db, "myboard", matured=False)
    registration = get_registration_by_name(db, "myboard")
    assert registration.status == "pending"
    assert registration.released_at is None
    db.close()


def test_reclaim_can_refresh_contact_without_resetting_the_earned_window(tmp_path):
    db = Database(tmp_path / "managed_dns.db")
    _insert(db)
    set_contact_window(
        db, "myboard", last_contact_at="2026-09-02T00:30:00+00:00",
        contact_started_at="2026-09-02T00:00:00+00:00",
    )
    mark_released(db, "myboard", released_at="2026-09-02T00:31:00+00:00")

    reclaim(
        db, "myboard", matured=False,
        last_contact_at="2026-09-02T00:32:00+00:00",
        contact_started_at="2026-09-02T00:00:00+00:00",
    )

    registration = get_registration_by_name(db, "myboard")
    assert registration.last_contact_at == "2026-09-02T00:32:00+00:00"
    assert registration.contact_started_at == "2026-09-02T00:00:00+00:00"
    db.close()


def test_reclaim_restores_matured_when_previously_matured(tmp_path):
    db = Database(tmp_path / "managed_dns.db")
    _insert(db)
    mark_matured(db, "myboard", matured_at="2026-09-02T00:30:00+00:00")
    mark_released(db, "myboard", released_at="2026-09-02T01:00:00+00:00")
    reclaim(db, "myboard", matured=True)
    registration = get_registration_by_name(db, "myboard")
    assert registration.status == "matured"
    assert registration.released_at is None
    assert registration.matured_at == "2026-09-02T00:30:00+00:00"  # unchanged, same row
    db.close()


def test_mark_abandoned_transitions_a_stale_registration(tmp_path):
    db = Database(tmp_path / "managed_dns.db")
    _insert(db)
    mark_abandoned(db, "myboard", released_at="2026-09-02T01:00:00+00:00")
    registration = get_registration_by_name(db, "myboard")
    assert registration.status == "abandoned"
    assert registration.released_at == "2026-09-02T01:00:00+00:00"
    db.close()


def test_list_stale_active_registrations_uses_last_contact_when_present(tmp_path):
    db = Database(tmp_path / "managed_dns.db")
    _insert(db, name="fresh", created_at="2026-09-01T00:00:00+00:00")
    set_last_contact_at(db, "fresh", "2026-09-02T00:00:00+00:00")
    _insert(db, name="stale", created_at="2026-09-01T00:00:00+00:00")
    set_last_contact_at(db, "stale", "2026-08-30T00:00:00+00:00")

    stale = list_stale_active_registrations(db, older_than="2026-09-01T12:00:00+00:00")
    assert [r.name for r in stale] == ["stale"]
    db.close()


def test_list_stale_active_registrations_falls_back_to_created_at_when_never_contacted(tmp_path):
    db = Database(tmp_path / "managed_dns.db")
    _insert(db, name="never-contacted", created_at="2026-08-01T00:00:00+00:00")

    stale = list_stale_active_registrations(db, older_than="2026-09-01T00:00:00+00:00")
    assert [r.name for r in stale] == ["never-contacted"]
    db.close()


def test_list_stale_active_registrations_ignores_released_and_abandoned(tmp_path):
    db = Database(tmp_path / "managed_dns.db")
    _insert(db, name="released", created_at="2026-08-01T00:00:00+00:00")
    mark_released(db, "released", released_at="2026-08-02T00:00:00+00:00")

    stale = list_stale_active_registrations(db, older_than="2026-09-01T00:00:00+00:00")
    assert stale == []
    db.close()


def test_delete_expired_registrations_removes_only_fully_expired_rows(tmp_path):
    db = Database(tmp_path / "managed_dns.db")
    _insert(db, name="long-expired")
    mark_released(db, "long-expired", released_at="2026-01-01T00:00:00+00:00")
    _insert(db, name="recently-released")
    mark_released(db, "recently-released", released_at="2026-09-01T00:00:00+00:00")
    _insert(db, name="still-active")

    removed = delete_expired_registrations(db, older_than="2026-06-01T00:00:00+00:00")

    assert removed == 1
    assert get_registration_by_name(db, "long-expired") is None
    assert get_registration_by_name(db, "recently-released") is not None
    assert get_registration_by_name(db, "still-active") is not None
    db.close()


def test_delete_registration_removes_the_row(tmp_path):
    db = Database(tmp_path / "managed_dns.db")
    _insert(db)
    delete_registration(db, "myboard")
    assert get_registration_by_name(db, "myboard") is None
    db.close()


def test_cancel_pending_replacement_revives_the_previous_row_in_the_same_transaction(tmp_path):
    db = Database(tmp_path / "managed_dns.db")
    _insert(db, name="old-name")
    _insert(db, name="new-name", replaces_name="old-name")
    mark_abandoned(db, "old-name", released_at="2026-09-03T12:00:00+00:00")

    assert cancel_pending_replacement(
        db, "new-name", "old-name", revive_previous=True, contact_at="2026-09-04T00:00:00+00:00",
    )

    assert get_registration_by_name(db, "new-name") is None
    revived = get_registration_by_name(db, "old-name")
    assert revived.status == "pending"  # never matured, so back into the age gate
    assert revived.released_at is None
    assert revived.last_contact_at == revived.contact_started_at == "2026-09-04T00:00:00+00:00"
    assert not db.connection.in_transaction
    db.close()


def test_cancel_pending_replacement_restores_matured_for_a_previously_live_row(tmp_path):
    db = Database(tmp_path / "managed_dns.db")
    _insert(db, name="old-name")
    mark_matured(db, "old-name", matured_at="2026-09-02T01:00:00+00:00")
    set_last_known_address(db, "old-name", "192.0.2.1")
    _insert(db, name="new-name", replaces_name="old-name")
    mark_abandoned(db, "old-name", released_at="2026-09-03T12:00:00+00:00")

    assert cancel_pending_replacement(
        db, "new-name", "old-name", revive_previous=True, contact_at="2026-09-04T00:00:00+00:00",
    )
    revived = get_registration_by_name(db, "old-name")
    assert revived.status == "matured"
    assert revived.last_known_address is None
    db.close()


def test_cancel_pending_replacement_changes_nothing_when_no_rename_is_pending(tmp_path):
    db = Database(tmp_path / "managed_dns.db")
    _insert(db, name="old-name")
    mark_abandoned(db, "old-name", released_at="2026-09-03T12:00:00+00:00")

    assert not cancel_pending_replacement(
        db, "never-reserved", "old-name", revive_previous=True, contact_at="2026-09-04T00:00:00+00:00",
    )
    assert get_registration_by_name(db, "old-name").status == "abandoned"
    db.close()
