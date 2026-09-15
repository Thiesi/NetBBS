"""The issuing half of remote identity attestation (design doc §5.5, issue #584).

`tests/test_remote_attestation.py` covers the receiving half. It mints every
object by calling a builder directly, which is exactly why it stayed green
while nothing in production called one — the shape
`tests/test_link_production_callers.py` now guards against.

These tests therefore never call `build_link_visible_remote_attestation`
themselves. Everything below starts from a caller's own act — the Profile
screen's per-attribute opt-in — and asks what a *subscriber* ends up holding
after the node's ordinary reconcile and the subscriber's ordinary pull. The
round trip is driven end to end in `test_a_toggle_reaches_a_subscriber` and
`test_withdrawing_consent_reaches_a_subscriber`.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import nacl.signing
import pytest

from netbbs.attestation import (
    attest_age,
    attest_name,
    set_attestation_link_visible,
)
from netbbs.auth.users import SYSOP_LEVEL, create_user, delete_user
from netbbs.link.remote_attestation import (
    ATTESTATION_PULL_REQUEST_OBJECT_TYPE,
    AttestationPullRequest,
    MAX_ATTESTATION_OBJECTS_PER_RESPONSE,
    REMOTE_ATTESTATION_OBJECT_TYPE,
    REMOTE_ATTESTATION_REVOCATION_OBJECT_TYPE,
    build_attestation_pull_request,
    configure_attestation_authority,
    get_remote_attestation_state,
    ingest_remote_attestation,
    list_attestation_authority_fingerprints,
    load_attestation_pull_cursor,
    load_issued_attestation_page,
    reconcile_issued_attestations,
    remote_meets_age,
    save_attestation_pull_cursor,
)
from netbbs.identity.keys import Identity, IdentityKind
from netbbs.link.trust import TrustSubject, register_subject
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


NOW = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
HOME = "home-node-fingerprint"


def stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "issuer.db")
    yield database
    database.close()


@pytest.fixture
def node_identity():
    """The node's current operational signing identity.

    An `Identity`, not a bare nacl key, because that is what a caller actually
    holds: `LinkNode.identity.signing_key` is the current operational
    `Identity` (design doc §12.6 -- the detached signature is made by that
    key, never the root).
    """
    return Identity(
        kind=IdentityKind.NODE, label="issuer",
        signing_key=nacl.signing.SigningKey.generate(), created_at=stamp(NOW),
    )


@pytest.fixture
def alice(db):
    sysop = create_user(db, "sysop", password="password", user_level=SYSOP_LEVEL)
    user = create_user(db, "alice", password="password")
    attest_name(db, user, "Alice Example", verifier=sysop)
    attest_age(db, user, datetime(1990, 4, 1).date(), verifier=sysop)
    return user


def reconcile(db, node_identity, *, at=NOW):
    return reconcile_issued_attestations(
        db, node_identity, home_node_fingerprint=HOME, now_iso=stamp(at)
    )


def served(db):
    objects, more = load_issued_attestation_page(db)
    assert not more
    return objects


def object_types(objects):
    return [item["envelope"]["object_type"] for item in objects]


# -- what consent does and does not produce ---------------------------------


def test_nothing_is_signed_without_an_opt_in(db, node_identity, alice):
    """A verified attestation alone is not consent to publish it."""
    assert reconcile(db, node_identity) == []
    assert served(db) == []


def test_opting_in_signs_exactly_that_attribute(db, node_identity, alice):
    set_attestation_link_visible(db, alice, "name", True)

    changes = reconcile(db, node_identity)

    assert [(c.action, c.attribute, c.reason) for c in changes] == [
        ("issued", "name", "consent_granted")
    ]
    payload = served(db)[0]["envelope"]["payload"]
    assert payload["attribute"] == "name"
    assert payload["attested_value"] == "Alice Example"
    assert payload["subject_opt_in"] is True
    assert payload["issuer_fingerprint"] == HOME
    assert payload["subject"] == {
        "kind": "user", "node_fingerprint": HOME, "opaque_user_id": "alice",
    }


def test_the_unshared_attribute_is_never_signed(db, node_identity, alice):
    """Opting in to name must not publish the birthdate as well."""
    set_attestation_link_visible(db, alice, "name", True)
    reconcile(db, node_identity)

    values = [item["envelope"]["payload"]["attested_value"] for item in served(db)]
    assert values == ["Alice Example"]
    assert "1990-04-01" not in values


def test_reconciling_again_signs_nothing_new(db, node_identity, alice):
    set_attestation_link_visible(db, alice, "name", True)
    reconcile(db, node_identity)

    assert reconcile(db, node_identity, at=NOW + timedelta(days=1)) == []
    assert len(served(db)) == 1


# -- withdrawal ---------------------------------------------------------------


def test_switching_the_toggle_off_signs_a_revocation(db, node_identity, alice):
    set_attestation_link_visible(db, alice, "name", True)
    reconcile(db, node_identity)
    issued = served(db)[0]
    issued_id = _content_id(issued)

    set_attestation_link_visible(db, alice, "name", False)
    changes = reconcile(db, node_identity, at=NOW + timedelta(hours=1))

    assert [(c.action, c.reason) for c in changes] == [("revoked", "consent_withdrawn")]
    objects = served(db)
    assert object_types(objects) == [
        REMOTE_ATTESTATION_OBJECT_TYPE, REMOTE_ATTESTATION_REVOCATION_OBJECT_TYPE,
    ]
    assert objects[1]["envelope"]["payload"]["revoked_content_id"] == issued_id


def test_re_verifying_a_value_retires_the_old_object(db, node_identity, alice):
    """`_store_attestation` clears `link_visible`, so a replacement value is
    never published under consent granted to the value it replaced."""
    sysop = create_user(db, "sysop2", password="password", user_level=SYSOP_LEVEL)
    set_attestation_link_visible(db, alice, "name", True)
    reconcile(db, node_identity)

    attest_name(db, alice, "Alice Different", verifier=sysop)
    changes = reconcile(db, node_identity, at=NOW + timedelta(hours=1))

    assert [(c.action, c.reason) for c in changes] == [("revoked", "consent_withdrawn")]
    published = [
        item["envelope"]["payload"].get("attested_value")
        for item in served(db)
        if item["envelope"]["object_type"] == REMOTE_ATTESTATION_OBJECT_TYPE
    ]
    assert published == ["Alice Example"]
    assert "Alice Different" not in published


def test_a_deleted_account_is_revoked_rather_than_left_standing(db, node_identity, alice):
    """The `SET NULL` on `user_id` exists for exactly this: the signed row has
    to outlive the account long enough to be revoked."""
    sysop = create_user(db, "sysop3", password="password", user_level=SYSOP_LEVEL)
    set_attestation_link_visible(db, alice, "name", True)
    reconcile(db, node_identity)

    delete_user(db, alice, deleted_by=sysop)
    changes = reconcile(db, node_identity, at=NOW + timedelta(hours=1))

    assert [(c.action, c.reason) for c in changes] == [("revoked", "account_removed")]
    assert REMOTE_ATTESTATION_REVOCATION_OBJECT_TYPE in object_types(served(db))


# -- lifetime -----------------------------------------------------------------


def test_the_issued_lifetime_stays_inside_the_protocol_ceiling(db, node_identity, alice):
    set_attestation_link_visible(db, alice, "name", True)
    reconcile(db, node_identity)

    payload = served(db)[0]["envelope"]["payload"]
    issued = datetime.fromisoformat(payload["issued_at"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
    assert timedelta(days=0) < expires - issued <= timedelta(days=365)
    assert expires - issued == timedelta(days=90)


def test_an_object_is_renewed_before_it_expires(db, node_identity, alice):
    set_attestation_link_visible(db, alice, "name", True)
    reconcile(db, node_identity)

    # Nothing yet at day 30: 60 days of life remain.
    assert reconcile(db, node_identity, at=NOW + timedelta(days=30)) == []
    changes = reconcile(db, node_identity, at=NOW + timedelta(days=61))

    assert [(c.action, c.reason) for c in changes] == [("renewed", "approaching_expiry")]
    # The old object is left to expire rather than revoked -- a revocation
    # would tell a subscriber to stop trusting a value being re-asserted in
    # the same breath.
    assert object_types(served(db)) == [
        REMOTE_ATTESTATION_OBJECT_TYPE, REMOTE_ATTESTATION_OBJECT_TYPE,
    ]


# -- the served page ----------------------------------------------------------


def test_the_page_keeps_revoked_and_expired_objects(db, node_identity, alice):
    """A subscriber that has been away needs the revocation that retired an
    object it still holds, so the stream cannot depend on when it is read."""
    set_attestation_link_visible(db, alice, "name", True)
    reconcile(db, node_identity)
    set_attestation_link_visible(db, alice, "name", False)
    reconcile(db, node_identity, at=NOW + timedelta(hours=1))

    much_later = load_issued_attestation_page(db)[0]
    assert len(much_later) == 2


def test_the_cursor_resumes_rather_than_restarts(db, node_identity, alice):
    set_attestation_link_visible(db, alice, "name", True)
    reconcile(db, node_identity)
    set_attestation_link_visible(db, alice, "age", True)
    reconcile(db, node_identity, at=NOW + timedelta(hours=1))

    first, more = load_issued_attestation_page(db, limit=1)
    assert more
    rest, more = load_issued_attestation_page(db, after_content_id=_content_id(first[0]), limit=1)
    assert not more
    assert _content_id(rest[0]) != _content_id(first[0])


def test_an_unknown_cursor_is_refused(db, node_identity):
    with pytest.raises(ValueError, match="unknown attestation pull cursor"):
        load_issued_attestation_page(db, after_content_id="f" * 64)


def test_the_pull_cursor_round_trips(db):
    assert load_attestation_pull_cursor(db, "responder", "issuer") is None
    save_attestation_pull_cursor(db, "responder", "issuer", "a" * 64, now_iso=stamp(NOW))
    assert load_attestation_pull_cursor(db, "responder", "issuer") == "a" * 64
    save_attestation_pull_cursor(db, "responder", "issuer", "b" * 64, now_iso=stamp(NOW))
    assert load_attestation_pull_cursor(db, "responder", "issuer") == "b" * 64


# -- the subscription set -----------------------------------------------------


def test_the_subscription_set_is_the_attestation_authorities(db):
    assert list_attestation_authority_fingerprints(db) == []
    configure_attestation_authority(
        db, "peer-b", attributes=["age"], reason="known operator", now_iso=stamp(NOW)
    )
    configure_attestation_authority(
        db, "peer-a", attributes=["name"], reason="known operator", now_iso=stamp(NOW)
    )
    assert list_attestation_authority_fingerprints(db) == ["peer-a", "peer-b"]


# -- the pull request ---------------------------------------------------------


def test_the_pull_request_is_its_own_signed_object_type(db, node_identity):
    request = build_attestation_pull_request(
        signing_identity=node_identity,
        requester_fingerprint="me", responder_fingerprint="them",
        issuer_fingerprint="them", created_at=stamp(NOW),
    )
    assert request.verifies(node_identity.verify_key)
    assert "revocations_only" not in request.payload
    assert ATTESTATION_PULL_REQUEST_OBJECT_TYPE == "remote_attestation_pull_request"


def test_a_trust_pull_signature_cannot_be_re_aimed_at_the_attestation_endpoint(db, node_identity):
    """The object type is inside the signature, so the two subscriptions
    cannot borrow each other's signed requests."""
    from netbbs.link.trust_wire import build_trust_pull_request

    trust_pull = build_trust_pull_request(
        signing_identity=node_identity,
        requester_fingerprint="me", responder_fingerprint="them",
        issuer_fingerprint="them", created_at=stamp(NOW),
    )
    borrowed = {k: v for k, v in trust_pull.to_dict().items() if k != "revocations_only"}

    assert not AttestationPullRequest.from_dict(borrowed).verifies(node_identity.verify_key)


def test_the_pull_request_rejects_a_malformed_wire(db, node_identity):
    good = build_attestation_pull_request(
        signing_identity=node_identity,
        requester_fingerprint="me", responder_fingerprint="them",
        issuer_fingerprint="them", created_at=stamp(NOW),
    ).to_dict()
    with pytest.raises(ValueError, match="invalid attestation pull request fields"):
        AttestationPullRequest.from_dict({**good, "unexpected": 1})
    with pytest.raises(ValueError, match="nonce"):
        AttestationPullRequest.from_dict({**good, "nonce": "not-hex" + "0" * 25})
    with pytest.raises(ValueError, match="limit"):
        AttestationPullRequest.from_dict(
            {**good, "limit": MAX_ATTESTATION_OBJECTS_PER_RESPONSE + 1}
        )
    with pytest.raises(ValueError, match="cursor"):
        AttestationPullRequest.from_dict({**good, "after_content_id": "short"})


# -- the round trip -----------------------------------------------------------


@pytest.fixture
def subscriber(tmp_path):
    database = Database(tmp_path / "subscriber.db")
    yield database
    database.close()


def _content_id(item) -> str:
    import hashlib

    from netbbs.link.events import canonical_bytes

    return hashlib.sha256(canonical_bytes(item["envelope"])).hexdigest()


def _subscribe(subscriber, node_identity):
    """A subscriber that has met the issuer and granted it age authority."""
    subject = TrustSubject.user(HOME, "alice")
    register_subject(
        subscriber, subject, first_accepted_at=stamp(NOW - timedelta(days=1)), now_iso=stamp(NOW)
    )
    configure_attestation_authority(
        subscriber, HOME, attributes=["age", "name"],
        reason="peer operator", now_iso=stamp(NOW),
    )
    return subject


def test_a_toggle_reaches_a_subscriber(db, node_identity, alice, subscriber):
    """The whole loop: a caller opts in, the issuer reconciles and serves, the
    subscriber pulls and ingests, and a remote age gate that refused before
    now passes."""
    subject = _subscribe(subscriber, node_identity)
    assert not remote_meets_age(subscriber, subject, 18, now_iso=stamp(NOW))

    set_attestation_link_visible(db, alice, "age", True)
    reconcile(db, node_identity)

    for item in served(db):
        ingest_remote_attestation(
            subscriber, item, issuer_verify_key=node_identity.verify_key, now_iso=stamp(NOW)
        )

    assert remote_meets_age(subscriber, subject, 18, now_iso=stamp(NOW))
    state = get_remote_attestation_state(subscriber, subject, "age", now_iso=stamp(NOW))
    assert state.accepted
    assert state.attestation.attested_value == "1990-04-01"


def test_withdrawing_consent_reaches_a_subscriber(db, node_identity, alice, subscriber):
    subject = _subscribe(subscriber, node_identity)
    set_attestation_link_visible(db, alice, "age", True)
    reconcile(db, node_identity)
    for item in served(db):
        ingest_remote_attestation(
            subscriber, item, issuer_verify_key=node_identity.verify_key, now_iso=stamp(NOW)
        )
    assert remote_meets_age(subscriber, subject, 18, now_iso=stamp(NOW))

    set_attestation_link_visible(db, alice, "age", False)
    later = NOW + timedelta(hours=1)
    reconcile(db, node_identity, at=later)
    for item in served(db):
        try:
            ingest_remote_attestation(
                subscriber, item, issuer_verify_key=node_identity.verify_key, now_iso=stamp(later)
            )
        except ValueError:  # already-seen objects are replays, not failures
            pass

    assert not remote_meets_age(subscriber, subject, 18, now_iso=stamp(later))


def test_derived_timestamps_use_the_sortable_storage_format(db, node_identity, alice):
    """Every liveness query here is a string comparison against a stored `now`.

    `utc_now_iso()`'s fixed six decimals and `Z` suffix are what make that
    work. `datetime.isoformat()` writes `+00:00` and a variable number of
    decimals, and `"...+00:00" < "...Z"` at the same instant -- so an
    `expires_at` written that way sorts as already expired against a `now` in
    the storage format, and the reconcile would re-mint on every pass.
    """
    set_attestation_link_visible(db, alice, "name", True)
    reconcile(db, node_identity)

    row = db.connection.execute(
        """SELECT issued_at, expires_at, created_at
           FROM link_issued_remote_attestations"""
    ).fetchone()
    for column, value in zip(("issued_at", "expires_at", "created_at"), row):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", value), (
            f"{column} is not in the sortable storage format: {value!r}"
        )
    # The property that format exists for, asserted directly against a real
    # `utc_now_iso()` rather than against the test's own frozen clock.
    assert row["expires_at"] > utc_now_iso()


def test_a_caller_supplied_now_in_another_format_still_sorts(db, node_identity, alice):
    """`_parse_time` accepts `+00:00`, so a caller can hand one in; the stored
    timestamps must not inherit it."""
    set_attestation_link_visible(db, alice, "name", True)

    reconcile_issued_attestations(
        db, node_identity, home_node_fingerprint=HOME,
        now_iso=NOW.isoformat(),  # "2026-09-15T12:00:00+00:00"
    )

    row = db.connection.execute(
        "SELECT expires_at, created_at FROM link_issued_remote_attestations"
    ).fetchone()
    assert row["created_at"].endswith("Z")
    assert row["expires_at"].endswith("Z")
    # And the object is seen as live by a second pass, which is the thing that
    # breaks when the formats disagree.
    assert reconcile(db, node_identity, at=NOW + timedelta(days=1)) == []
