"""Tests for netbbs.managed_dns.state — node-wide managed-DNS registration state (issue #201)."""

from __future__ import annotations

import sqlite3

import pytest

from netbbs.managed_dns.state import (
    OptIn,
    RegistrationStatus,
    get_dynamic,
    get_last_contact_at,
    get_node_fingerprint,
    get_opt_in,
    get_previous_name,
    get_previous_published,
    get_previous_status,
    get_published,
    get_registered_name,
    get_registration_status,
    get_service_url,
    set_dynamic,
    set_last_contact_at,
    set_node_fingerprint,
    set_opt_in,
    set_pending_rename_state,
    set_registration_result_state,
    set_heartbeat_reconciliation_state,
    set_published,
    set_registered_name,
    set_registration_status,
    set_service_url,
)
from netbbs.storage.database import Database


def test_opt_in_defaults_to_undecided(tmp_path):
    db = Database(tmp_path / "node.db")
    assert get_opt_in(db) is OptIn.UNDECIDED
    db.close()


def test_opt_in_roundtrip(tmp_path):
    db = Database(tmp_path / "node.db")
    set_opt_in(db, OptIn.ACCEPTED)
    assert get_opt_in(db) is OptIn.ACCEPTED
    set_opt_in(db, OptIn.DECLINED)
    assert get_opt_in(db) is OptIn.DECLINED
    db.close()


def test_registered_name_defaults_to_none(tmp_path):
    db = Database(tmp_path / "node.db")
    assert get_registered_name(db) is None
    db.close()


def test_registered_name_roundtrip(tmp_path):
    db = Database(tmp_path / "node.db")
    set_registered_name(db, "myboard")
    assert get_registered_name(db) == "myboard"
    db.close()


def test_registered_name_can_be_cleared_back_to_none(tmp_path):
    """`set_registered_name(db, None)` (e.g. after a confirmed release)
    must read back as `None`, not the empty string it's stored as --
    `get_config`'s own default-on-missing-row behavior doesn't apply
    once a row already exists, so this needs its own explicit check."""
    db = Database(tmp_path / "node.db")
    set_registered_name(db, "myboard")
    set_registered_name(db, None)
    assert get_registered_name(db) is None
    db.close()


def test_registration_status_defaults_to_none(tmp_path):
    db = Database(tmp_path / "node.db")
    assert get_registration_status(db) is RegistrationStatus.NONE
    db.close()


def test_registration_status_roundtrip(tmp_path):
    db = Database(tmp_path / "node.db")
    for status in RegistrationStatus:
        set_registration_status(db, status)
        assert get_registration_status(db) is status
    db.close()


def test_last_contact_at_defaults_to_none(tmp_path):
    db = Database(tmp_path / "node.db")
    assert get_last_contact_at(db) is None
    db.close()


def test_last_contact_at_roundtrip(tmp_path):
    db = Database(tmp_path / "node.db")
    set_last_contact_at(db, "2026-09-02T12:00:00+00:00")
    assert get_last_contact_at(db) == "2026-09-02T12:00:00+00:00"
    db.close()


def test_dynamic_defaults_to_false(tmp_path):
    db = Database(tmp_path / "node.db")
    assert get_dynamic(db) is False
    db.close()


def test_dynamic_roundtrip(tmp_path):
    db = Database(tmp_path / "node.db")
    set_dynamic(db, True)
    assert get_dynamic(db) is True
    set_dynamic(db, False)
    assert get_dynamic(db) is False
    db.close()


def test_node_fingerprint_defaults_to_none(tmp_path):
    db = Database(tmp_path / "node.db")
    assert get_node_fingerprint(db) is None
    db.close()


def test_node_fingerprint_roundtrip(tmp_path):
    db = Database(tmp_path / "node.db")
    set_node_fingerprint(db, "abc123fingerprint")
    assert get_node_fingerprint(db) == "abc123fingerprint"
    db.close()


def test_service_url_defaults_to_none(tmp_path):
    db = Database(tmp_path / "node.db")
    assert get_service_url(db) is None
    db.close()


def test_service_url_roundtrip(tmp_path):
    db = Database(tmp_path / "node.db")
    set_service_url(db, "https://managed.netbbs.org")
    assert get_service_url(db) == "https://managed.netbbs.org"
    db.close()


def test_service_url_can_be_cleared_back_to_none(tmp_path):
    db = Database(tmp_path / "node.db")
    set_service_url(db, "https://managed.netbbs.org")
    set_service_url(db, None)
    assert get_service_url(db) is None
    db.close()


def test_pending_rename_state_rolls_back_as_one_transaction(tmp_path):
    db = Database(tmp_path / "node.db")
    set_registered_name(db, "old-name")
    set_registration_status(db, RegistrationStatus.MATURED)
    set_published(db, True)
    db.connection.execute(
        """
        CREATE TRIGGER reject_pending_status
        BEFORE UPDATE OF value ON node_config
        WHEN OLD.key = 'managed_dns_status' AND NEW.value = 'pending'
        BEGIN
            SELECT RAISE(ABORT, 'simulated write failure');
        END
        """
    )
    db.connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="simulated write failure"):
        set_pending_rename_state(
            db,
            name="new-name",
            previous_name="old-name",
            previous_status=RegistrationStatus.MATURED,
            previous_published=True,
        )

    assert get_registered_name(db) == "old-name"
    assert get_registration_status(db) is RegistrationStatus.MATURED
    assert get_published(db)
    assert get_previous_name(db) is None
    assert get_previous_status(db) is None
    assert not get_previous_published(db)
    db.close()


def test_registration_result_state_rolls_back_as_one_transaction(tmp_path):
    db = Database(tmp_path / "node.db")
    set_registered_name(db, "old-name")
    set_registration_status(db, RegistrationStatus.MATURED)
    set_published(db, True)
    set_dynamic(db, False)
    set_opt_in(db, OptIn.DECLINED)
    db.connection.execute(
        """
        CREATE TRIGGER reject_registration_result_status
        BEFORE UPDATE OF value ON node_config
        WHEN OLD.key = 'managed_dns_status' AND NEW.value = 'pending'
        BEGIN
            SELECT RAISE(ABORT, 'simulated registration result failure');
        END
        """
    )
    db.connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="simulated registration result failure"):
        set_registration_result_state(
            db, name="new-name", status=RegistrationStatus.PENDING, dynamic=True,
        )

    assert get_registered_name(db) == "old-name"
    assert get_registration_status(db) is RegistrationStatus.MATURED
    assert get_published(db)
    assert not get_dynamic(db)
    assert get_opt_in(db) is OptIn.DECLINED
    db.close()


def test_heartbeat_reconciliation_rolls_back_as_one_transaction(tmp_path):
    db = Database(tmp_path / "node.db")
    set_registered_name(db, "old-name")
    set_registration_status(db, RegistrationStatus.MATURED)
    set_published(db, True)
    db.connection.execute(
        """
        CREATE TRIGGER reject_reconciled_status
        BEFORE UPDATE OF value ON node_config
        WHEN OLD.key = 'managed_dns_status' AND NEW.value = 'pending'
        BEGIN
            SELECT RAISE(ABORT, 'simulated reconciliation failure');
        END
        """
    )
    db.connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="simulated reconciliation failure"):
        set_heartbeat_reconciliation_state(
            db, name="new-name", status=RegistrationStatus.PENDING,
            published=False, last_contact_at="2026-09-04T00:00:00+00:00",
            previous_name="old-name", previous_status=RegistrationStatus.MATURED,
            previous_published=True,
        )

    assert get_registered_name(db) == "old-name"
    assert get_registration_status(db) is RegistrationStatus.MATURED
    assert get_published(db)
    assert get_previous_name(db) is None
    db.close()
