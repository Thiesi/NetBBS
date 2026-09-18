"""Issuing this node's own trust vouches (design doc §12.4, issue #589 slice 1).

`tests/test_link_trust_wire.py` covers the receiving half, and mints every
object by calling a builder directly -- which is how that half stayed green
while no node in production could issue anything, the shape
`tests/test_link_production_callers.py` guards against.

Nothing below calls `build_trust_vouch` itself. Every test starts from the act
a SysOp performs, recording or withdrawing an intent to vouch, and asks what a
*subscriber* ends up holding after the issuer's ordinary reconcile and the
subscriber's ordinary ingest of the issuer's ordinary served page.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from netbbs.identity.keys import Identity, IdentityKind
from netbbs.link.trust import (
    TrustDimension,
    TrustState,
    TrustSubject,
    configure_trust_domain,
    configure_trusted_reporter,
    register_subject,
    set_trust_override,
)
from netbbs.link.trust_issuance import (
    ISSUED_VOUCH_LIFETIME,
    MAX_VOUCH_EXPLANATION_CHARS,
    VouchIntentError,
    get_vouch_intent,
    list_vouch_intent_history,
    list_vouch_intents,
    reconcile_issued_vouches,
    record_vouch_intent,
    withdraw_vouch_intent,
)
from netbbs.link.trust_wire import (
    TRUST_VOUCH_OBJECT_TYPE,
    TRUST_VOUCH_REVOCATION_OBJECT_TYPE,
    SignedTrustObject,
    ingest_trust_objects,
    load_trust_object_page,
)
from netbbs.storage.database import Database

NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
FRIEND = TrustSubject.node("friend-node-fingerprint")
CALLER = TrustSubject.user("friend-node-fingerprint", "carol")


def stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def issuer():
    return Identity.generate(IdentityKind.NODE, "issuer")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "issuer.db")
    for subject in (FRIEND, CALLER):
        register_subject(database, subject, first_accepted_at=stamp(NOW - timedelta(days=40)), now_iso=stamp(NOW))
    yield database
    database.close()


@pytest.fixture
def subscriber(tmp_path, issuer):
    """A node that has named the issuer a reporter allowed to vouch for nodes and users."""
    database = Database(tmp_path / "subscriber.db")
    configure_trust_domain(database, "friends", display_name="Friends", now_iso=stamp(NOW))
    configure_trusted_reporter(
        database, issuer.fingerprint, domain_id="friends", scopes=[],
        can_vouch_nodes=True, can_vouch_users=True, now_iso=stamp(NOW),
    )
    yield database
    database.close()


def reconcile(db, issuer, *, at=NOW, identity=None):
    return reconcile_issued_vouches(
        db, identity or issuer, home_node_fingerprint=issuer.fingerprint, now_iso=stamp(at)
    )


def served(db, issuer, **kwargs):
    objects, more = load_trust_object_page(db, issuer_fingerprint=issuer.fingerprint, **kwargs)
    assert not more
    return objects


def types(objects):
    return [item["envelope"]["object_type"] for item in objects]


def deliver(db, issuer, subscriber, *, at=NOW, verify_key=None):
    """One subscriber pull: parse under the issuer's current key, then ingest."""
    parsed = [
        SignedTrustObject.from_dict(item, issuer_verify_key=verify_key or issuer.verify_key)
        for item in served(db, issuer)
    ]
    return ingest_trust_objects(subscriber, parsed, now_iso=stamp(at))


def held_vouches(subscriber, subject):
    return subscriber.connection.execute(
        """SELECT content_id, revoked_at, explanation FROM link_trust_vouches
           WHERE subject_id = ? ORDER BY received_at""",
        (subject.subject_id,),
    ).fetchall()


# -- an intent is what gets signed ---------------------------------------------------


def test_nothing_is_signed_without_an_intent(db, issuer):
    assert reconcile(db, issuer) == []
    assert served(db, issuer) == []


def test_recording_an_intent_signs_nothing_by_itself(db, issuer):
    """The screen that records an intent may have no key at all -- the offline
    admin console has no node identity -- so signing is the reconcile's."""
    record_vouch_intent(db, FRIEND, explanation="met at the 2026 meet", now_iso=stamp(NOW))

    assert served(db, issuer) == []
    intent = get_vouch_intent(db, FRIEND, home_node_fingerprint=issuer.fingerprint, now_iso=stamp(NOW))
    assert intent.status == "pending" and intent.expires_at is None


def test_the_reconcile_signs_a_vouch_a_subscriber_can_verify(db, issuer):
    record_vouch_intent(db, FRIEND, explanation="met at the 2026 meet", now_iso=stamp(NOW))

    changes = reconcile(db, issuer)

    assert [(c.action, c.reason, c.subject) for c in changes] == [("issued", "intent_recorded", FRIEND)]
    [item] = served(db, issuer)
    vouch = SignedTrustObject.from_dict(item, issuer_verify_key=issuer.verify_key)
    assert vouch.object_type == TRUST_VOUCH_OBJECT_TYPE
    assert vouch.issuer_fingerprint == issuer.fingerprint
    assert vouch.payload["subject"] == {"kind": "node", "node_fingerprint": FRIEND.node_fingerprint}
    assert vouch.payload["explanation"] == "met at the 2026 meet"
    assert vouch.payload["expires_at"] == stamp(NOW + ISSUED_VOUCH_LIFETIME)
    intent = get_vouch_intent(db, FRIEND, home_node_fingerprint=issuer.fingerprint, now_iso=stamp(NOW))
    assert intent.status == "published" and intent.expires_at == stamp(NOW + ISSUED_VOUCH_LIFETIME)


def test_reconciling_again_signs_nothing_new(db, issuer):
    record_vouch_intent(db, FRIEND, explanation="known operator", now_iso=stamp(NOW))
    reconcile(db, issuer)

    assert reconcile(db, issuer, at=NOW + timedelta(hours=1)) == []
    assert len(served(db, issuer)) == 1


def test_an_own_vouch_has_no_effect_on_this_nodes_own_policy(db, issuer):
    """A node is not its own reporter. Establishing a subject *here* is an
    override, which is the local act; a vouch is a statement to others."""
    record_vouch_intent(db, FRIEND, explanation="known operator", now_iso=stamp(NOW))
    reconcile(db, issuer)

    assert db.connection.execute("SELECT COUNT(*) FROM link_trust_vouches").fetchone()[0] == 0


# -- the whole loop ---------------------------------------------------------------------


def test_a_vouch_reaches_a_subscriber_and_counts_there(db, issuer, subscriber):
    """What issue #589 said no dogfood run could ever show: trust propagating."""
    record_vouch_intent(db, CALLER, explanation="long-standing caller", now_iso=stamp(NOW))
    reconcile(db, issuer)

    result = deliver(db, issuer, subscriber)

    assert len(result[0]) == 1 and result.skipped == []
    [row] = held_vouches(subscriber, CALLER)
    assert row["revoked_at"] is None and row["explanation"] == "long-standing caller"
    explanation = json.loads(subscriber.connection.execute(
        """SELECT explanation_json FROM link_trust_effective_states
           WHERE subject_id = ? AND dimension = 'content_conduct'""",
        (CALLER.subject_id,),
    ).fetchone()[0])
    assert explanation["vouch_domains"] == ["friends"]


def test_withdrawing_reaches_a_subscriber(db, issuer, subscriber):
    record_vouch_intent(db, FRIEND, explanation="known operator", now_iso=stamp(NOW))
    reconcile(db, issuer)
    deliver(db, issuer, subscriber)

    assert withdraw_vouch_intent(db, FRIEND, now_iso=stamp(NOW + timedelta(hours=1)))
    changes = reconcile(db, issuer, at=NOW + timedelta(hours=1))
    deliver(db, issuer, subscriber, at=NOW + timedelta(hours=1))

    assert [(c.action, c.reason) for c in changes] == [("revoked", "intent_withdrawn")]
    assert types(served(db, issuer)) == [TRUST_VOUCH_OBJECT_TYPE, TRUST_VOUCH_REVOCATION_OBJECT_TYPE]
    [row] = held_vouches(subscriber, FRIEND)
    assert row["revoked_at"] is not None
    assert list_vouch_intents(db, home_node_fingerprint=issuer.fingerprint) == []


def test_withdrawing_nothing_is_not_an_error(db, issuer):
    assert withdraw_vouch_intent(db, FRIEND, now_iso=stamp(NOW)) is False
    assert reconcile(db, issuer) == []


def test_a_new_reason_retires_the_old_vouch_and_signs_a_fresh_one(db, issuer, subscriber):
    """The reason is inside the signed payload, so it cannot be edited in
    place; and the listing must not call the about-to-be-revoked one published."""
    record_vouch_intent(db, FRIEND, explanation="first wording", now_iso=stamp(NOW))
    reconcile(db, issuer)
    later = NOW + timedelta(hours=1)
    record_vouch_intent(db, FRIEND, explanation="better wording", now_iso=stamp(later))

    pending = get_vouch_intent(db, FRIEND, home_node_fingerprint=issuer.fingerprint, now_iso=stamp(later))
    assert pending.status == "pending"
    changes = reconcile(db, issuer, at=later)
    deliver(db, issuer, subscriber, at=later)

    assert [(c.action, c.reason) for c in changes] == [
        ("revoked", "explanation_replaced"), ("issued", "intent_recorded"),
    ]
    live = [row["explanation"] for row in held_vouches(subscriber, FRIEND) if row["revoked_at"] is None]
    assert live == ["better wording"]


# -- a node does not vouch for what it refuses to deal with ---------------------------------


def test_an_identity_quarantined_here_cannot_be_vouched_for(db, issuer):
    set_trust_override(
        db, FRIEND, TrustDimension.RESOURCE_BEHAVIOR, TrustState.QUARANTINED,
        reason="flooding", actor_user_id=None, now_iso=stamp(NOW),
    )

    with pytest.raises(VouchIntentError, match="quarantined or blocked"):
        record_vouch_intent(db, FRIEND, explanation="known operator", now_iso=stamp(NOW))


def test_quarantining_a_vouched_identity_withdraws_the_vouch_and_lifting_it_restores_it(db, issuer, subscriber):
    record_vouch_intent(db, FRIEND, explanation="known operator", now_iso=stamp(NOW))
    reconcile(db, issuer)
    deliver(db, issuer, subscriber)

    day = NOW + timedelta(days=1)
    set_trust_override(
        db, FRIEND, TrustDimension.RESOURCE_BEHAVIOR, TrustState.BLOCKED,
        reason="flooding", actor_user_id=None, now_iso=stamp(day),
    )
    changes = reconcile(db, issuer, at=day)
    deliver(db, issuer, subscriber, at=day)

    assert [(c.action, c.reason) for c in changes] == [("revoked", "subject_restricted_here")]
    assert get_vouch_intent(
        db, FRIEND, home_node_fingerprint=issuer.fingerprint, now_iso=stamp(day)
    ).status == "suspended"
    assert [row["revoked_at"] is None for row in held_vouches(subscriber, FRIEND)] == [False]

    # The intent stood throughout. Once the restriction goes, so does the
    # reason not to vouch, and nobody has to remember to issue it again.
    later = day + timedelta(days=1)
    set_trust_override(
        db, FRIEND, TrustDimension.RESOURCE_BEHAVIOR, TrustState.ESTABLISHED,
        reason="resolved", actor_user_id=None, now_iso=stamp(later),
    )
    assert [(c.action, c.reason) for c in reconcile(db, issuer, at=later)] == [("issued", "intent_recorded")]
    deliver(db, issuer, subscriber, at=later)
    assert [row["revoked_at"] is None for row in held_vouches(subscriber, FRIEND)] == [False, True]


# -- renewal and rotation ---------------------------------------------------------------------


def test_a_vouch_is_renewed_before_it_expires_and_the_old_one_left_to_run_out(db, issuer, subscriber):
    record_vouch_intent(db, FRIEND, explanation="known operator", now_iso=stamp(NOW))
    reconcile(db, issuer)

    assert reconcile(db, issuer, at=NOW + timedelta(days=59)) == []
    renewal_day = NOW + timedelta(days=61)
    changes = reconcile(db, issuer, at=renewal_day)
    deliver(db, issuer, subscriber, at=renewal_day)

    assert [(c.action, c.reason) for c in changes] == [("renewed", "approaching_expiry")]
    # No revocation: receivers count domains, never vouches, so the overlap
    # adds no weight, and revoking would withdraw support being re-asserted.
    assert types(served(db, issuer)) == [TRUST_VOUCH_OBJECT_TYPE, TRUST_VOUCH_OBJECT_TYPE]
    assert reconcile(db, issuer, at=renewal_day + timedelta(hours=1)) == []


def test_an_expired_vouch_is_signed_again_while_the_intent_stands(db, issuer):
    """A node whose Link was down past a vouch's whole lifetime."""
    record_vouch_intent(db, FRIEND, explanation="known operator", now_iso=stamp(NOW))
    reconcile(db, issuer)

    changes = reconcile(db, issuer, at=NOW + timedelta(days=200))

    assert [(c.action, c.reason) for c in changes] == [("issued", "intent_recorded")]


def test_rotating_the_signing_key_reissues_a_live_vouch(db, issuer, subscriber):
    """A subscriber resolves only the issuer's *current* operational key, so
    what the previous key signed stops verifying the moment the node rotates."""
    record_vouch_intent(db, FRIEND, explanation="known operator", now_iso=stamp(NOW))
    reconcile(db, issuer)
    rotated = Identity.generate(IdentityKind.NODE, "issuer-rotated")

    changes = reconcile(db, issuer, at=NOW + timedelta(hours=1), identity=rotated)

    assert [(c.action, c.reason) for c in changes] == [("renewed", "signing_key_rotated")]
    newest = served(db, issuer)[-1]
    assert SignedTrustObject.from_dict(newest, issuer_verify_key=rotated.verify_key).payload["subject"]
    assert reconcile(db, issuer, at=NOW + timedelta(hours=2), identity=rotated) == []


def test_a_withdrawal_survives_the_issuers_clock_going_backwards(db, issuer, subscriber):
    """The page is ordered by insertion. Ordered by wall clock, a revocation
    signed while the clock was behind sorts *before* the vouch it retires: a
    fresh subscriber meets a revocation for an object it does not hold, skips
    it, then admits the vouch, and a withdrawn vouch stays live there."""
    record_vouch_intent(db, FRIEND, explanation="known operator", now_iso=stamp(NOW))
    reconcile(db, issuer)
    withdraw_vouch_intent(db, FRIEND, now_iso=stamp(NOW - timedelta(hours=1)))
    reconcile(db, issuer, at=NOW - timedelta(hours=1))

    assert types(served(db, issuer)) == [TRUST_VOUCH_OBJECT_TYPE, TRUST_VOUCH_REVOCATION_OBJECT_TYPE]
    deliver(db, issuer, subscriber)
    [row] = held_vouches(subscriber, FRIEND)
    assert row["revoked_at"] is not None


# -- what cannot be vouched for ------------------------------------------------------------------


def test_a_reason_is_required_and_bounded_because_it_is_published(db, issuer):
    with pytest.raises(VouchIntentError, match="needs a reason"):
        record_vouch_intent(db, FRIEND, explanation="   ", now_iso=stamp(NOW))
    with pytest.raises(VouchIntentError, match="at most"):
        record_vouch_intent(db, FRIEND, explanation="x" * (MAX_VOUCH_EXPLANATION_CHARS + 1), now_iso=stamp(NOW))
    assert list_vouch_intents(db, home_node_fingerprint=issuer.fingerprint) == []


def test_an_identity_this_node_has_never_met_cannot_be_vouched_for(db, issuer):
    with pytest.raises(VouchIntentError, match="not a trust subject"):
        record_vouch_intent(db, TrustSubject.node("a-stranger"), explanation="hearsay", now_iso=stamp(NOW))


def test_a_node_cannot_vouch_for_itself_or_its_own_users(db, issuer):
    own_user = TrustSubject.user(issuer.fingerprint, "alice")
    register_subject(db, own_user, first_accepted_at=stamp(NOW), now_iso=stamp(NOW))

    with pytest.raises(VouchIntentError, match="itself or for its own users"):
        record_vouch_intent(
            db, own_user, explanation="my caller", own_node_fingerprint=issuer.fingerprint,
            now_iso=stamp(NOW),
        )


def test_a_withdrawn_intent_keeps_its_row_as_the_history(db, issuer):
    record_vouch_intent(db, FRIEND, explanation="known operator", actor_user_id=None, now_iso=stamp(NOW))
    withdraw_vouch_intent(db, FRIEND, now_iso=stamp(NOW + timedelta(hours=1)))
    record_vouch_intent(db, FRIEND, explanation="known operator, again", now_iso=stamp(NOW + timedelta(hours=2)))

    rows = db.connection.execute(
        "SELECT explanation, withdrawn_at FROM link_trust_vouch_intents ORDER BY intent_id"
    ).fetchall()
    assert [(row[0], row[1] is None) for row in rows] == [
        ("known operator", False), ("known operator, again", True),
    ]
    history = list_vouch_intent_history(db)
    assert [(entry.explanation, entry.withdrawn_at is None) for entry in history] == [
        ("known operator, again", True), ("known operator", False),
    ]
    assert all(entry.subject == FRIEND for entry in history)


def test_the_listing_without_a_known_fingerprint_claims_nothing_is_published(db, issuer):
    """A node that has not started since the fingerprint cache existed cannot
    look its own published side up, and must not guess."""
    record_vouch_intent(db, FRIEND, explanation="known operator", now_iso=stamp(NOW))
    reconcile(db, issuer)

    [intent] = list_vouch_intents(db, home_node_fingerprint=None, now_iso=stamp(NOW))
    assert intent.status == "pending"
