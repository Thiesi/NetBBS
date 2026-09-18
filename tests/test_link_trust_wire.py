"""Signed trust-object and bounded-ingress tests (design §12.6–12.7, issue #127)."""

from __future__ import annotations

import base64
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
    load_trust_pull_cursor,
    save_trust_pull_cursor,
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


def test_oversized_embedded_and_digest_evidence_are_rejected_before_signing(reporter):
    with pytest.raises(TrustWireError, match="exceeds 256 KiB"):
        signal(reporter, evidence={"mode": "embedded", "data": {"blob": "x" * (256 * 1024)}})

    with pytest.raises(TrustWireError, match="exceeds 256 KiB"):
        signal(reporter, evidence={
            "mode": "digest", "sha256": "0" * 64,
            "size": 256 * 1024 + 1, "locator": "/evidence/oversized",
        })


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


def test_an_unconfigured_issuer_is_rejected_and_an_unconfigured_scope_is_skipped(db, reporter):
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
    # Skipped, not fatal (issue #589). Aborting rolled the whole batch back
    # and left the cursor where it was, so the same object was met first on
    # every later pass and the subscription never moved again.
    in_scope = signal(reporter)
    result = ingest_trust_objects(db, [wrong_scope, in_scope], now_iso=stamp(NOW))
    accepted, replayed = result
    assert accepted == [in_scope.content_id] and replayed == []
    assert result.skipped == [wrong_scope.content_id]
    stored = [row[0] for row in db.connection.execute("SELECT content_id FROM link_trust_wire_objects")]
    assert stored == [in_scope.content_id]


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


# -- one object this node has no use for must not stop the rest (issue #589) --


def _vouch_for(reporter, subject, vouch_id="vouch"):
    return build_trust_vouch(
        signing_identity=reporter, issuer_fingerprint=reporter.fingerprint, vouch_id=vouch_id,
        subject=subject, issued_at=stamp(NOW - timedelta(hours=1)),
        expires_at=stamp(NOW + timedelta(days=90)),
    )


def test_a_vouch_for_a_kind_the_reporter_was_not_granted_is_skipped_with_its_revocation(db, reporter):
    """The first real issuer reaches this at once: its SysOp vouches for a
    caller, and one subscriber only ever granted it node vouches."""
    configure_reporter(db, reporter.fingerprint)  # nodes only
    user_vouch = _vouch_for(reporter, TrustSubject.user("home-node", "carol"), "user-vouch")
    node_vouch = _vouch_for(reporter, TrustSubject.node("subject-node"), "node-vouch")
    revocation = build_trust_revocation(
        signing_identity=reporter, issuer_fingerprint=reporter.fingerprint,
        revocation_id="revoke-user-vouch", revoked_content_id=user_vouch.content_id,
        issued_at=stamp(NOW), vouch=True,
    )

    result = ingest_trust_objects(db, [user_vouch, node_vouch, revocation], now_iso=stamp(NOW))

    assert result[0] == [node_vouch.content_id]
    assert result.skipped == [user_vouch.content_id, revocation.content_id]
    assert db.connection.execute("SELECT COUNT(*) FROM link_trust_vouches").fetchone()[0] == 1


def test_a_revocation_for_another_issuers_object_still_rejects_the_batch(db, reporter):
    """Skipping is for what local configuration has no use for. An object that
    lies about what it may revoke is not that."""
    configure_reporter(db, reporter.fingerprint)
    other = Identity.generate(IdentityKind.NODE, "other")
    configure_trusted_reporter(
        db, other.fingerprint, domain_id="independent-a", scopes=[], can_vouch_nodes=True,
        now_iso=stamp(NOW),
    )
    theirs = _vouch_for(other, TrustSubject.node("subject-node"))
    ingest_trust_objects(db, [theirs], now_iso=stamp(NOW))
    forged = build_trust_revocation(
        signing_identity=reporter, issuer_fingerprint=reporter.fingerprint,
        revocation_id="not-mine", revoked_content_id=theirs.content_id,
        issued_at=stamp(NOW), vouch=True,
    )

    with pytest.raises(TrustWireError, match="another issuer"):
        ingest_trust_objects(db, [forged], now_iso=stamp(NOW))


def test_widening_a_reporters_grant_lets_the_next_pull_reach_what_was_skipped(db, reporter):
    """A skipped object is not stored, and the subscription cursor has moved
    past it all the same. The cursor names a position, so a grant that now
    covers the object would never see it again unless the cursor goes."""
    configure_reporter(db, reporter.fingerprint)
    save_trust_pull_cursor(db, reporter.fingerprint, reporter.fingerprint, "a" * 64, now_iso=stamp(NOW))
    other = "another-reporter-fingerprint"
    save_trust_pull_cursor(db, other, other, "b" * 64, now_iso=stamp(NOW))

    configure_trusted_reporter(
        db, reporter.fingerprint, domain_id="independent-a", scopes=[],
        can_vouch_nodes=True, can_vouch_users=True, now_iso=stamp(NOW),
    )

    assert load_trust_pull_cursor(db, reporter.fingerprint, reporter.fingerprint) is None
    assert load_trust_pull_cursor(db, other, other) == "b" * 64


def test_the_page_is_ordered_by_insertion_not_by_the_receipt_clock(db, reporter):
    """A revocation is always inserted after the object it retires, whatever
    the wall clock said at the time."""
    configure_reporter(db, reporter.fingerprint)
    vouch = _vouch_for(reporter, TrustSubject.node("subject-node"))
    ingest_trust_objects(db, [vouch], now_iso=stamp(NOW))
    revocation = build_trust_revocation(
        signing_identity=reporter, issuer_fingerprint=reporter.fingerprint,
        revocation_id="revoke", revoked_content_id=vouch.content_id,
        issued_at=stamp(NOW - timedelta(hours=2)), vouch=True,
    )
    ingest_trust_objects(db, [revocation], now_iso=stamp(NOW - timedelta(hours=1)))

    page, _ = load_trust_object_page(db, issuer_fingerprint=reporter.fingerprint)
    assert page == [vouch.to_dict(), revocation.to_dict()]
    after_vouch, _ = load_trust_object_page(
        db, issuer_fingerprint=reporter.fingerprint, after_content_id=vouch.content_id
    )
    assert after_vouch == [revocation.to_dict()]


def test_a_second_revocation_of_the_same_object_is_skipped_not_fatal(db, reporter):
    """An issuer restored from a backup taken before a withdrawal signs the
    revocation again. Every subscriber already holding the first one used to
    reject the batch on it, on every pass, for good."""
    configure_reporter(db, reporter.fingerprint)
    vouch = _vouch_for(reporter, TrustSubject.node("subject-node"))
    first, second = (
        build_trust_revocation(
            signing_identity=reporter, issuer_fingerprint=reporter.fingerprint,
            revocation_id=revocation_id, revoked_content_id=vouch.content_id,
            issued_at=stamp(NOW), vouch=True,
        )
        for revocation_id in ("first", "second")
    )
    ingest_trust_objects(db, [vouch, first], now_iso=stamp(NOW))
    later = _vouch_for(reporter, TrustSubject.node("another-node"), "later")

    result = ingest_trust_objects(db, [second, later], now_iso=stamp(NOW))

    assert result.skipped == [second.content_id]
    assert result[0] == [later.content_id]


def test_an_authentic_object_of_an_unknown_type_is_a_payload_refusal_a_forged_one_a_signature_refusal(reporter):
    """The signature is checked before the protocol version and object type.
    Those two are what a newer issuer will one day send; a subscriber may move
    its cursor past an authentic object it cannot use, and must not move it
    past anything it could not authenticate. Checked the other way round, the
    first new object type stopped every subscriber on this release for good."""
    from netbbs.link.events import build_envelope, canonical_bytes
    from netbbs.link.trust_wire import TrustPayloadError, TrustSignatureError

    future = build_envelope("trust_something_new", {"issuer_fingerprint": reporter.fingerprint})
    authentic = {
        "envelope": future,
        "signature": base64.b64encode(reporter.sign(canonical_bytes(future))).decode("ascii"),
    }
    forged = {"envelope": future, "signature": base64.b64encode(b"x" * 64).decode("ascii")}

    with pytest.raises(TrustPayloadError, match="unsupported trust object type"):
        SignedTrustObject.from_dict(authentic, issuer_verify_key=reporter.verify_key)
    with pytest.raises(TrustSignatureError):
        SignedTrustObject.from_dict(forged, issuer_verify_key=reporter.verify_key)


def test_an_envelope_that_cannot_be_canonicalized_is_a_wire_error_not_an_escape(reporter):
    from netbbs.link.events import build_envelope

    envelope = build_envelope("trust_vouch", {"issuer_fingerprint": reporter.fingerprint, "weight": 1.5})
    with pytest.raises(TrustWireError, match="canonicalized"):
        SignedTrustObject.from_dict(
            {"envelope": envelope, "signature": base64.b64encode(b"x" * 64).decode("ascii")},
            issuer_verify_key=reporter.verify_key,
        )
