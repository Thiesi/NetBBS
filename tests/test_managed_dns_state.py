"""Tests for netbbs.managed_dns.state — node-wide managed-DNS registration state (issue #201)."""

from __future__ import annotations

import sqlite3

import pytest

from netbbs.managed_dns import state
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
    set_previous_name,
    set_previous_published,
    set_previous_status,
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
            service_url="https://dns.example",
        )

    assert get_registered_name(db) == "old-name"
    assert get_registration_status(db) is RegistrationStatus.MATURED
    assert get_published(db)
    assert not get_dynamic(db)
    assert get_opt_in(db) is OptIn.DECLINED
    db.close()


def test_registration_result_state_clears_expired_rename_metadata(tmp_path):
    db = Database(tmp_path / "node.db")
    set_previous_name(db, "expired-old")
    set_previous_status(db, RegistrationStatus.ABANDONED)
    set_previous_published(db, False)

    set_registration_result_state(
        db, name="fresh-name", status=RegistrationStatus.PENDING, dynamic=True,
        service_url="https://dns.example",
    )

    assert get_previous_name(db) is None
    assert get_previous_status(db) is None
    assert not get_previous_published(db)
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


# -- the shipped default service address (issue #583) ------------------------


def test_the_service_url_falls_back_to_the_shipped_default(tmp_path, monkeypatch):
    """The whole of issue #583: a node that has been told nothing still
    knows where the project's own instance is, so the opt-in a SysOp
    accepted at first run can actually lead somewhere."""
    monkeypatch.setattr(state, "DEFAULT_SERVICE_URL", "https://dns.netbbs.org")
    db = Database(tmp_path / "node.db")

    assert get_service_url(db) == "https://dns.netbbs.org"
    db.close()


def test_a_configured_service_url_wins_over_the_shipped_default(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "DEFAULT_SERVICE_URL", "https://dns.netbbs.org")
    db = Database(tmp_path / "node.db")

    set_service_url(db, "http://127.0.0.1:8099")

    assert get_service_url(db) == "http://127.0.0.1:8099"
    db.close()


def test_clearing_a_configured_service_url_returns_to_the_shipped_default(tmp_path, monkeypatch):
    """`netbbs.__main__.run` writes `None` here on every startup whose
    configuration carries no `[managed_dns] service_url`, so an operator
    who removes the setting must land back on the shipped address rather
    than stay pinned to the one they configured once."""
    monkeypatch.setattr(state, "DEFAULT_SERVICE_URL", "https://dns.netbbs.org")
    db = Database(tmp_path / "node.db")
    set_service_url(db, "http://127.0.0.1:8099")

    set_service_url(db, None)

    assert get_service_url(db) == "https://dns.netbbs.org"
    db.close()


def test_the_shipped_default_is_unset_until_the_service_is_deployed():
    """A guard, not a preference: `services.managed_dns` is not standing
    anywhere yet, and shipping an address that resolves to nothing would
    turn every node's registration into a connection error instead of
    the plain "not running yet" the flow says today. Flip this test in
    the same commit that flips the constant."""
    assert state.DEFAULT_SERVICE_URL is None


# -- the credential's issuing service (Codex review of PR #587) --------------


def test_a_registration_records_the_service_that_issued_its_credential(tmp_path):
    db = Database(tmp_path / "node.db")

    set_registration_result_state(
        db, name="myboard", status=RegistrationStatus.PENDING, dynamic=True,
        service_url="https://dns.example",
    )

    assert state.get_credential_service_url(db) == "https://dns.example"
    assert state.foreign_credential_service_url(db, "https://dns.example") is None
    db.close()


def test_a_changed_service_address_marks_the_stored_credential_foreign(tmp_path):
    """The whole reason the credential's issuer is recorded: since issue
    #583 the service address is an operator setting, so it can change
    under a node that already holds a bearer secret for another
    service."""
    db = Database(tmp_path / "node.db")
    set_registration_result_state(
        db, name="myboard", status=RegistrationStatus.PENDING, dynamic=True,
        service_url="https://dns.example",
    )

    assert state.foreign_credential_service_url(db, "https://other.example") == "https://dns.example"
    db.close()


def test_a_node_that_never_registered_has_no_foreign_credential(tmp_path):
    """Nothing to compare, so nothing is held back."""
    db = Database(tmp_path / "node.db")

    assert state.get_credential_service_url(db) is None
    assert state.foreign_credential_service_url(db, "https://dns.example") is None
    db.close()


def test_an_equivalent_spelling_of_the_same_address_is_not_a_service_change(tmp_path):
    """Codex review of PR #587. The issuer comparison decides whether a
    node heartbeats or pauses, so a case change, an explicit default
    port and a trailing slash must not read as three different
    services."""
    db = Database(tmp_path / "node.db")
    set_registration_result_state(
        db, name="myboard", status=RegistrationStatus.PENDING, dynamic=True,
        service_url="https://dns.example",
    )

    for equivalent in (
        "https://dns.example",
        "https://DNS.EXAMPLE",
        "https://dns.example:443",
        "https://dns.example/",
        "https://Dns.Example:443/",
    ):
        assert state.foreign_credential_service_url(db, equivalent) is None, equivalent

    # A different scheme, host, path or non-default port is a real change.
    for different in (
        "http://dns.example",
        "https://other.example",
        "https://dns.example:8443",
        "https://dns.example/staging",
    ):
        assert state.foreign_credential_service_url(db, different) == "https://dns.example", different
    db.close()


def test_canonicalisation_leaves_an_unparseable_address_alone(tmp_path):
    """Only validated addresses reach this, and its only job is deciding
    whether two match -- so a string it cannot parse is compared as
    written rather than raised over."""
    assert state.canonical_service_url("http://[") == "http://["


def test_a_path_parameter_is_part_of_which_service_this_is(tmp_path):
    """Codex review of PR #587. `urlparse` peels a final-segment path
    parameter off into `params`, so a canonical form built from `path`
    alone reads two tenants of the same host as one service -- and hands
    the first one's credential to the second. Path parameters are
    deliberately accepted by the config validation (a reverse-proxy
    subpath), so they have to be part of the identity."""
    db = Database(tmp_path / "node.db")
    set_registration_result_state(
        db, name="myboard", status=RegistrationStatus.PENDING, dynamic=True,
        service_url="https://dns.example/api;tenant=a",
    )

    assert state.foreign_credential_service_url(db, "https://dns.example/api;tenant=a") is None
    assert (
        state.foreign_credential_service_url(db, "https://dns.example/api;tenant=b")
        == "https://dns.example/api;tenant=a"
    )
    db.close()


def test_two_spellings_of_one_ip_literal_are_the_same_service(tmp_path):
    """Codex review of PR #587. `::1` and `0:0:0:0:0:0:0:1` are one
    address; re-bracketing alone left them comparing differently, which
    would pause a node over a spelling change."""
    db = Database(tmp_path / "node.db")
    set_registration_result_state(
        db, name="myboard", status=RegistrationStatus.PENDING, dynamic=True,
        service_url="http://[::1]:8099",
    )

    assert state.foreign_credential_service_url(db, "http://[0:0:0:0:0:0:0:1]:8099") is None
    assert state.foreign_credential_service_url(db, "http://[::1]:8100") == "http://[::1]:8099"
    db.close()


# -- listener facts (issue #603) and the recovery note (issue #600) ----------


def test_local_listeners_default_to_none_and_round_trip(tmp_path):
    from netbbs.managed_dns.state import ListenerFacts, get_local_listeners, set_local_listeners

    db = Database(tmp_path / "node.db")
    assert get_local_listeners(db) is None
    facts = ListenerFacts(telnet_port=None, ssh_port=2222, web_port=8080, web_public_url="https://b.example")
    set_local_listeners(db, facts)
    assert get_local_listeners(db) == facts
    set_local_listeners(db, ListenerFacts(telnet_port=23, ssh_port=22, web_port=None, web_public_url=None))
    assert get_local_listeners(db).ssh_port == 22
    assert get_local_listeners(db).web_port is None
    db.close()


def test_recovery_note_round_trips_and_is_cleared_by_any_authoritative_answer(tmp_path):
    """The note describes an automatic reclaim attempt made after the
    current abandonment; a registration result or a heartbeat
    reconciliation is the service's own answer and supersedes it."""
    from netbbs.managed_dns.state import (
        RecoveryNote, get_recovery_note, set_heartbeat_reconciliation_state, set_recovery_note,
        set_registration_result_state,
    )

    db = Database(tmp_path / "node.db")
    assert get_recovery_note(db) is None
    set_recovery_note(db, RecoveryNote(at="2026-09-16T10:00:00+00:00", text="refused"))
    assert get_recovery_note(db) == RecoveryNote(at="2026-09-16T10:00:00+00:00", text="refused")

    set_registration_result_state(
        db, name="myboard", status=RegistrationStatus.PENDING, dynamic=True, service_url="https://dns.example",
    )
    assert get_recovery_note(db) is None

    set_recovery_note(db, RecoveryNote(at="2026-09-16T11:00:00+00:00", text="refused again"))
    set_heartbeat_reconciliation_state(
        db, name="myboard", status=RegistrationStatus.ABANDONED, published=False, last_contact_at=None,
        previous_name=None, previous_status=None, previous_published=False,
    )
    assert get_recovery_note(db) is None
    set_recovery_note(db, None)
    assert get_recovery_note(db) is None
    db.close()
