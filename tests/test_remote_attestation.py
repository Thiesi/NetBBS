"""Phase-4 remote identity-attestation policy tests (issue #130)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import nacl.signing
import pytest

from netbbs.link.remote_attestation import (
    build_link_visible_remote_attestation,
    build_remote_attestation,
    build_remote_attestation_revocation,
    clear_remote_attestation_override,
    configure_attestation_authority,
    get_remote_attestation_state,
    format_remote_name_for_resource,
    ingest_remote_attestation,
    list_attestation_authorities,
    remote_meets_age,
    remote_meets_name_requirement,
    remove_attestation_authority,
    set_remote_attestation_override,
)
from netbbs.attestation import attest_name, set_attestation_link_visible
from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.link.trust import (
    TrustDimension,
    TrustState,
    TrustSubject,
    configure_trust_domain,
    configure_trusted_reporter,
    register_subject,
    set_trust_override,
)
from netbbs.storage.database import Database


NOW = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)


def stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def subject(db):
    value = TrustSubject.user("home-node", "opaque-alice")
    register_subject(db, value, first_accepted_at=stamp(NOW - timedelta(days=30)), now_iso=stamp(NOW))
    return value


@pytest.fixture
def authority_key():
    return nacl.signing.SigningKey.generate()


def attestation_wire(
    key, subject, *, attribute="name", value="Alice Example", opt_in=True,
    issued_at=None, expires_at=None, issuer="identity-authority",
):
    return build_remote_attestation(
        key,
        issuer_fingerprint=issuer,
        subject=subject,
        attribute=attribute,
        attested_value=value,
        subject_opt_in=opt_in,
        issued_at=stamp(issued_at or NOW - timedelta(minutes=1)),
        expires_at=stamp(expires_at or NOW + timedelta(days=30)),
    )


def test_authority_is_separate_from_trusted_reporter_role(db, subject, authority_key):
    configure_trust_domain(db, "reports", display_name="Reports", now_iso=stamp(NOW))
    configure_trusted_reporter(
        db,
        "identity-authority",
        domain_id="reports",
        scopes=[(TrustDimension.IDENTITY_INTEGRITY, "signed_equivocation")],
        now_iso=stamp(NOW),
    )
    ingest_remote_attestation(
        db, attestation_wire(authority_key, subject),
        issuer_verify_key=authority_key.verify_key, now_iso=stamp(NOW),
    )
    state = get_remote_attestation_state(db, subject, "name", now_iso=stamp(NOW))
    assert state.accepted is False
    assert state.reason_code == "no_current_trusted_attestation"

    configure_attestation_authority(
        db, "identity-authority", attributes=["name"],
        reason="contracted identity verifier", now_iso=stamp(NOW),
    )
    assert list_attestation_authorities(db)[0].attributes == ("name",)
    assert get_remote_attestation_state(
        db, subject, "name", now_iso=stamp(NOW)
    ).accepted is True
    assert get_remote_attestation_state(
        db, subject, "age", now_iso=stamp(NOW)
    ).accepted is False


def test_reasoned_sysop_override_can_accept_current_signed_unconfigured_record(
    db, subject, authority_key
):
    ingest_remote_attestation(
        db, attestation_wire(authority_key, subject),
        issuer_verify_key=authority_key.verify_key, now_iso=stamp(NOW),
    )
    state = get_remote_attestation_state(db, subject, "name", now_iso=stamp(NOW))
    assert state.accepted is False
    override_id = set_remote_attestation_override(
        db, subject, "name", accepted=True,
        reason="reviewed original signed evidence locally", now_iso=stamp(NOW),
    )
    state = get_remote_attestation_state(db, subject, "name", now_iso=stamp(NOW))
    assert state.accepted is True
    assert state.reason_code == "sysop_accept"
    assert state.explanation["override_id"] == override_id


def test_invalid_signature_and_missing_subject_opt_in_fail_before_persistence(
    db, subject, authority_key
):
    configure_attestation_authority(
        db, "identity-authority", attributes=["name"], reason="reviewed", now_iso=stamp(NOW)
    )
    wrong_key = nacl.signing.SigningKey.generate()
    with pytest.raises(ValueError, match="signature is invalid"):
        ingest_remote_attestation(
            db, attestation_wire(authority_key, subject),
            issuer_verify_key=wrong_key.verify_key, now_iso=stamp(NOW),
        )
    with pytest.raises(ValueError, match="explicit Link opt-in"):
        ingest_remote_attestation(
            db, attestation_wire(authority_key, subject, opt_in=False),
            issuer_verify_key=authority_key.verify_key, now_iso=stamp(NOW),
        )
    assert db.connection.execute("SELECT COUNT(*) FROM link_remote_attestations").fetchone()[0] == 0


def test_local_export_requires_current_per_attribute_opt_in(db, authority_key):
    sysop = create_user(db, "sysop", password="password", user_level=SYSOP_LEVEL)
    alice = create_user(db, "alice", password="password")
    attest_name(db, alice, "Alice Example", verifier=sysop)
    with pytest.raises(ValueError, match="not explicitly Link-visible"):
        build_link_visible_remote_attestation(
            db, alice, authority_key, home_node_fingerprint="home-node",
            attribute="name", issued_at=stamp(NOW),
            expires_at=stamp(NOW + timedelta(days=30)),
        )
    set_attestation_link_visible(db, alice, "name", True)
    wire = build_link_visible_remote_attestation(
        db, alice, authority_key, home_node_fingerprint="home-node",
        attribute="name", issued_at=stamp(NOW),
        expires_at=stamp(NOW + timedelta(days=30)),
    )
    assert wire["envelope"]["payload"]["subject"] == {
        "kind": "user", "node_fingerprint": "home-node", "opaque_user_id": "alice"
    }


def test_expiry_and_revocation_remove_future_gate_satisfaction_without_deleting_history(
    db, subject, authority_key
):
    configure_attestation_authority(
        db, "identity-authority", attributes=["name"], reason="reviewed", now_iso=stamp(NOW)
    )
    content_id = ingest_remote_attestation(
        db,
        attestation_wire(authority_key, subject, expires_at=NOW + timedelta(hours=1)),
        issuer_verify_key=authority_key.verify_key,
        now_iso=stamp(NOW),
    )
    assert remote_meets_name_requirement(db, subject, "verified", now_iso=stamp(NOW))
    assert not remote_meets_name_requirement(
        db, subject, "verified", now_iso=stamp(NOW + timedelta(hours=2))
    )

    second_id = ingest_remote_attestation(
        db, attestation_wire(authority_key, subject, issued_at=NOW, expires_at=NOW + timedelta(days=2)),
        issuer_verify_key=authority_key.verify_key, now_iso=stamp(NOW),
    )
    revocation = build_remote_attestation_revocation(
        authority_key,
        issuer_fingerprint="identity-authority",
        revoked_content_id=second_id,
        issued_at=stamp(NOW + timedelta(minutes=1)),
    )
    ingest_remote_attestation(
        db, revocation, issuer_verify_key=authority_key.verify_key,
        now_iso=stamp(NOW + timedelta(minutes=1)),
    )
    assert not remote_meets_name_requirement(
        db, subject, "verified", now_iso=stamp(NOW + timedelta(hours=2))
    )
    assert db.connection.execute(
        "SELECT COUNT(*) FROM link_remote_attestations WHERE content_id IN (?, ?)",
        (content_id, second_id),
    ).fetchone()[0] == 2


def test_locally_distrusted_authority_stops_satisfying_gates(db, subject, authority_key):
    authority_subject = TrustSubject.node("identity-authority")
    register_subject(
        db, authority_subject, first_accepted_at=stamp(NOW - timedelta(days=40)), now_iso=stamp(NOW)
    )
    set_trust_override(
        db, authority_subject, TrustDimension.IDENTITY_INTEGRITY, TrustState.ESTABLISHED,
        reason="authority reviewed", now_iso=stamp(NOW),
    )
    configure_attestation_authority(
        db, "identity-authority", attributes=["name"], reason="reviewed", now_iso=stamp(NOW)
    )
    ingest_remote_attestation(
        db, attestation_wire(authority_key, subject),
        issuer_verify_key=authority_key.verify_key, now_iso=stamp(NOW),
    )
    assert remote_meets_name_requirement(db, subject, "verified", now_iso=stamp(NOW))

    set_trust_override(
        db, authority_subject, TrustDimension.IDENTITY_INTEGRITY, TrustState.BLOCKED,
        reason="authority compromised", now_iso=stamp(NOW + timedelta(minutes=1)),
    )
    state = get_remote_attestation_state(
        db, subject, "name", now_iso=stamp(NOW + timedelta(minutes=1))
    )
    assert state.accepted is False
    assert state.explanation["rejected_issuers"] == [
        {"issuer": "identity-authority", "reason": "authority_blocked"}
    ]


def test_age_name_gates_overrides_and_restart_are_fail_closed(db, subject, authority_key):
    configure_attestation_authority(
        db, "identity-authority", attributes=["age", "name"], reason="reviewed", now_iso=stamp(NOW)
    )
    ingest_remote_attestation(
        db, attestation_wire(authority_key, subject, attribute="age", value="2000-08-15"),
        issuer_verify_key=authority_key.verify_key, now_iso=stamp(NOW),
    )
    assert remote_meets_age(db, subject, 18, now_iso=stamp(NOW))
    assert not remote_meets_age(db, subject, 30, now_iso=stamp(NOW))
    override_id = set_remote_attestation_override(
        db, subject, "age", accepted=False, reason="document under review", now_iso=stamp(NOW)
    )
    assert not remote_meets_age(db, subject, 18, now_iso=stamp(NOW))

    reopened = Database(db.path)
    try:
        assert not remote_meets_age(reopened, subject, 18, now_iso=stamp(NOW))
        clear_remote_attestation_override(reopened, override_id, now_iso=stamp(NOW + timedelta(minutes=1)))
        assert remote_meets_age(reopened, subject, 18, now_iso=stamp(NOW + timedelta(minutes=1)))
    finally:
        reopened.close()


def test_removing_authority_reverses_acceptance_without_removing_signed_record(
    db, subject, authority_key
):
    configure_attestation_authority(
        db, "identity-authority", attributes=["name"], reason="reviewed", now_iso=stamp(NOW)
    )
    ingest_remote_attestation(
        db, attestation_wire(authority_key, subject),
        issuer_verify_key=authority_key.verify_key, now_iso=stamp(NOW),
    )
    remove_attestation_authority(db, "identity-authority", now_iso=stamp(NOW + timedelta(minutes=1)))
    assert not remote_meets_name_requirement(
        db, subject, "verified", now_iso=stamp(NOW + timedelta(minutes=1))
    )
    assert db.connection.execute("SELECT COUNT(*) FROM link_remote_attestations").fetchone()[0] == 1


def test_remote_real_name_disclosure_stays_resource_scoped(db, subject, authority_key):
    configure_attestation_authority(
        db, "identity-authority", attributes=["name"], reason="reviewed", now_iso=stamp(NOW)
    )
    ingest_remote_attestation(
        db, attestation_wire(authority_key, subject, value="Private Real Name"),
        issuer_verify_key=authority_key.verify_key, now_iso=stamp(NOW),
    )
    assert format_remote_name_for_resource(
        db, subject, "alice@home", name_requirement=None, now_iso=stamp(NOW)
    ) == "alice@home"
    disclosed = format_remote_name_for_resource(
        db, subject, "alice@home", name_requirement="verified_and_displayed", now_iso=stamp(NOW)
    )
    assert disclosed.startswith("alice@home ")
    assert "(=Private Real Name=)" in disclosed


def test_an_unknown_link_identity_is_answered_rather_than_crashed(db):
    """A gate asked about an identity this node has never accepted anything
    from must say no, not raise.

    `link_remote_attestation_effective` has a foreign key to
    `link_trust_subjects`, so recomputing a projection for an unregistered
    subject fails with an integrity error. Nothing hit that while the three
    gates had no production caller (issue #584); the first one -- a carried
    board post from a remote author on an age-gated board -- hits it on the
    ordinary path, because the overwhelmingly common case is an author with no
    attestation at all.
    """
    stranger = TrustSubject.user("never-met-node", "opaque-stranger")

    state = get_remote_attestation_state(db, stranger, "age", now_iso=stamp(NOW))

    assert not state.accepted
    assert state.reason_code == "unknown_link_identity"
    assert state.attestation is None
    assert not remote_meets_age(db, stranger, 18, now_iso=stamp(NOW))
    assert not remote_meets_name_requirement(db, stranger, "verified", now_iso=stamp(NOW))
    # An absent gate still passes: "no requirement" is not "denied".
    assert remote_meets_age(db, stranger, None, now_iso=stamp(NOW))
    assert remote_meets_name_requirement(db, stranger, None, now_iso=stamp(NOW))
    # And nothing was persisted on the way to that answer.
    assert db.connection.execute(
        "SELECT COUNT(*) FROM link_remote_attestation_effective"
    ).fetchone()[0] == 0
