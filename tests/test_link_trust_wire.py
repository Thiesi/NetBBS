"""Signed trust-object and bounded-ingress tests (design §12.6–12.7, issue #127)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from netbbs.identity.keys import Identity, IdentityKind
from netbbs.link.trust import (
    EvidenceClass,
    TrustDimension,
    TrustSubject,
    configure_trust_domain,
    configure_trusted_reporter,
)
from netbbs.link.trust_wire import (
    SignedTrustObject,
    TrustWireError,
    activate_reproduced_digest_signal,
    build_trust_revocation,
    build_trust_signal,
    build_trust_vouch,
    ingest_trust_objects,
    load_trust_object_page,
    verify_evidence_bytes,
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
def reporter():
    return Identity.generate(IdentityKind.NODE, "reporter")


def configure_reporter(db, fingerprint: str, *, vouch: bool = True) -> None:
    configure_trust_domain(db, "independent-a", display_name="Independent A", now_iso=stamp(NOW))
    configure_trusted_reporter(
        db,
        fingerprint,
        domain_id="independent-a",
        scopes=[(TrustDimension.IDENTITY_INTEGRITY, "signed_equivocation")],
        can_vouch_nodes=vouch,
        now_iso=stamp(NOW),
    )


def signal(reporter, number: int = 1, *, evidence=None):
    return build_trust_signal(
        signing_identity=reporter,
        issuer_fingerprint=reporter.fingerprint,
        signal_id=f"signal-{number}",
        subject=TrustSubject.node("subject-node"),
        dimension=TrustDimension.IDENTITY_INTEGRITY,
        category="signed_equivocation",
        evidence_class=EvidenceClass.SELF_VERIFYING,
        evidence=evidence or {"mode": "embedded", "data": {"proof": number}},
        observed_at=stamp(NOW - timedelta(hours=2)),
        issued_at=stamp(NOW - timedelta(hours=1)),
        expires_at=stamp(NOW + timedelta(days=120)),
    )


def test_signed_signal_round_trips_and_rejects_the_wrong_key(reporter):
    original = signal(reporter)
    parsed = SignedTrustObject.from_dict(original.to_dict(), issuer_verify_key=reporter.verify_key)
    assert parsed == original
    assert len(parsed.content_id) == 64

    wrong = Identity.generate(IdentityKind.NODE, "wrong")
    with pytest.raises(TrustWireError, match="does not verify"):
        SignedTrustObject.from_dict(original.to_dict(), issuer_verify_key=wrong.verify_key)


def test_configured_reporter_ingestion_is_deduplicated_and_carrier_safe(db, reporter):
    configure_reporter(db, reporter.fingerprint)
    original = signal(reporter)

    accepted, replayed = ingest_trust_objects(db, [original], now_iso=stamp(NOW))
    assert accepted == [original.content_id]
    assert replayed == []

    accepted, replayed = ingest_trust_objects(db, [original], now_iso=stamp(NOW))
    assert accepted == []
    assert replayed == [original.content_id]

    page, more = load_trust_object_page(db, issuer_fingerprint=reporter.fingerprint)
    assert page == [original.to_dict()]
    assert not more


def test_unconfigured_issuer_and_unconfigured_scope_are_rejected(db, reporter):
    with pytest.raises(TrustWireError, match="not a configured reporter"):
        ingest_trust_objects(db, [signal(reporter)], now_iso=stamp(NOW))

    configure_reporter(db, reporter.fingerprint)
    wrong_scope = build_trust_signal(
        signing_identity=reporter,
        issuer_fingerprint=reporter.fingerprint,
        signal_id="wrong-scope",
        subject=TrustSubject.node("subject-node"),
        dimension=TrustDimension.RESOURCE_BEHAVIOR,
        category="request_flood",
        evidence_class=EvidenceClass.OBSERVER_ATTESTED,
        evidence={"mode": "embedded", "data": {"request_count": 5}},
        observed_at=stamp(NOW - timedelta(hours=2)),
        issued_at=stamp(NOW - timedelta(hours=1)),
        expires_at=stamp(NOW + timedelta(days=1)),
    )
    with pytest.raises(TrustWireError, match="not configured"):
        ingest_trust_objects(db, [wrong_scope], now_iso=stamp(NOW))


def test_vouch_and_revocations_preserve_original_wire_objects(db, reporter):
    configure_reporter(db, reporter.fingerprint)
    vouch = build_trust_vouch(
        signing_identity=reporter,
        issuer_fingerprint=reporter.fingerprint,
        vouch_id="vouch-1",
        subject=TrustSubject.node("subject-node"),
        issued_at=stamp(NOW - timedelta(hours=1)),
        expires_at=stamp(NOW + timedelta(days=200)),
    )
    ingest_trust_objects(db, [vouch], now_iso=stamp(NOW))
    revocation = build_trust_revocation(
        signing_identity=reporter,
        issuer_fingerprint=reporter.fingerprint,
        revocation_id="revoke-vouch-1",
        revoked_content_id=vouch.content_id,
        issued_at=stamp(NOW),
        vouch=True,
    )
    ingest_trust_objects(db, [revocation], now_iso=stamp(NOW))

    row = db.connection.execute(
        "SELECT revoked_by_content_id FROM link_trust_vouches WHERE content_id = ?",
        (vouch.content_id,),
    ).fetchone()
    assert row[0] == revocation.content_id
    assert db.connection.execute("SELECT COUNT(*) FROM link_trust_wire_objects").fetchone()[0] == 2


def test_per_subject_category_quota_is_visible_and_atomic(db, reporter):
    configure_reporter(db, reporter.fingerprint)
    for number in range(10):
        ingest_trust_objects(db, [signal(reporter, number)], now_iso=stamp(NOW))
    with pytest.raises(TrustWireError, match="active-signal quota"):
        ingest_trust_objects(db, [signal(reporter, 11)], now_iso=stamp(NOW))
    assert db.connection.execute("SELECT COUNT(*) FROM link_trust_signals").fetchone()[0] == 10


def test_digest_evidence_stays_inactive_until_verified_and_reproduced(db, reporter):
    configure_reporter(db, reporter.fingerprint)
    body = json.dumps({"proof": "reproducible"}).encode()
    evidence = {
        "mode": "digest",
        "sha256": hashlib.sha256(body).hexdigest(),
        "size": len(body),
        "locator": "https://reporter.invalid/evidence/1",
    }
    digest_signal = signal(reporter, evidence=evidence)
    ingest_trust_objects(db, [digest_signal], now_iso=stamp(NOW))
    assert db.connection.execute(
        "SELECT COUNT(*) FROM link_trust_signals WHERE content_id = ?", (digest_signal.content_id,)
    ).fetchone()[0] == 0
    assert verify_evidence_bytes(evidence, body) == {"proof": "reproducible"}
    with pytest.raises(TrustWireError, match="hash"):
        verify_evidence_bytes(evidence, body[:-1] + b"x")
    with pytest.raises(TrustWireError, match="could not be independently reproduced"):
        activate_reproduced_digest_signal(
            db, digest_signal.content_id, body, observation_id="failed-proof",
            reproduce=lambda parsed: False, now_iso=stamp(NOW),
        )
    assert activate_reproduced_digest_signal(
        db, digest_signal.content_id, body, observation_id="local-proof",
        reproduce=lambda parsed: parsed == {"proof": "reproducible"}, now_iso=stamp(NOW),
    )
    assert db.connection.execute(
        "SELECT COUNT(*) FROM link_trust_signals WHERE content_id = ?", (digest_signal.content_id,)
    ).fetchone()[0] == 1
    assert db.connection.execute(
        "SELECT COUNT(*) FROM link_trust_local_observations WHERE observation_id = 'local-proof'"
    ).fetchone()[0] == 1


def test_revocation_can_cancel_a_digest_signal_before_activation(db, reporter):
    configure_reporter(db, reporter.fingerprint)
    body = b'{"proof":true}'
    pending = signal(reporter, evidence={
        "mode": "digest", "sha256": hashlib.sha256(body).hexdigest(),
        "size": len(body), "locator": "/evidence/pending",
    })
    ingest_trust_objects(db, [pending], now_iso=stamp(NOW))
    revocation = build_trust_revocation(
        signing_identity=reporter, issuer_fingerprint=reporter.fingerprint,
        revocation_id="revoke-pending", revoked_content_id=pending.content_id,
        issued_at=stamp(NOW),
    )
    ingest_trust_objects(db, [revocation], now_iso=stamp(NOW))
    with pytest.raises(TrustWireError, match="revoked"):
        activate_reproduced_digest_signal(
            db, pending.content_id, body, observation_id="too-late",
            reproduce=lambda parsed: True, now_iso=stamp(NOW),
        )


def test_pull_pagination_uses_a_stable_content_cursor(db, reporter):
    configure_reporter(db, reporter.fingerprint)
    objects = [signal(reporter, number) for number in range(3)]
    ingest_trust_objects(db, objects, now_iso=stamp(NOW))

    first, more = load_trust_object_page(db, issuer_fingerprint=reporter.fingerprint, limit=2)
    assert len(first) == 2 and more
    cursor = SignedTrustObject.from_dict(first[-1], issuer_verify_key=reporter.verify_key).content_id
    second, more = load_trust_object_page(
        db, issuer_fingerprint=reporter.fingerprint, after_content_id=cursor, limit=2
    )
    assert len(second) == 1 and not more


def test_containment_pull_returns_only_revocations(db, reporter):
    configure_reporter(db, reporter.fingerprint)
    original = signal(reporter)
    ingest_trust_objects(db, [original], now_iso=stamp(NOW))
    revocation = build_trust_revocation(
        signing_identity=reporter, issuer_fingerprint=reporter.fingerprint,
        revocation_id="containment-revocation", revoked_content_id=original.content_id,
        issued_at=stamp(NOW),
    )
    ingest_trust_objects(db, [revocation], now_iso=stamp(NOW))
    page, more = load_trust_object_page(
        db, issuer_fingerprint=reporter.fingerprint, revocations_only=True
    )
    assert page == [revocation.to_dict()]
    assert not more
