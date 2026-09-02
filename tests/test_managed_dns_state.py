"""Tests for netbbs.managed_dns.state — node-wide managed-DNS registration state (issue #201)."""

from __future__ import annotations

from netbbs.managed_dns.state import (
    OptIn,
    RegistrationStatus,
    get_dynamic,
    get_last_contact_at,
    get_opt_in,
    get_registered_name,
    get_registration_status,
    set_dynamic,
    set_last_contact_at,
    set_opt_in,
    set_registered_name,
    set_registration_status,
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
