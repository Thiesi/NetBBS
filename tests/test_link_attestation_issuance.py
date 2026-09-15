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

import base64
import re
from datetime import datetime, timedelta, timezone

import nacl.signing
import pytest

from netbbs.attestation import (
    AttestationError,
    attest_age,
    attest_name,
    set_attestation_link_visible,
    withdraw_link_visibility,
)
from netbbs.auth.users import SYSOP_LEVEL, create_user, delete_user
from netbbs.link.remote_attestation import (
    ATTESTATION_PULL_REQUEST_OBJECT_TYPE,
    AttestationPullRequest,
    MAX_ACTIVE_ATTESTATIONS_PER_ISSUER,
    MAX_ATTESTATION_OBJECTS_PER_RESPONSE,
    REMOTE_ATTESTATION_OBJECT_TYPE,
    REMOTE_ATTESTATION_REVOCATION_OBJECT_TYPE,
    UnknownAttestationSubject,
    build_remote_attestation,
    build_attestation_pull_request,
    configure_attestation_authority,
    get_remote_attestation_state,
    ingest_remote_attestation,
    list_attestation_authority_fingerprints,
    list_issued_attestations,
    load_attestation_pull_cursor,
    load_issued_attestation_page,
    reconcile_issued_attestations,
    remote_meets_age,
    save_attestation_pull_cursor,
)
from netbbs.identity.keys import Identity, IdentityKind
from netbbs.link.events import canonical_bytes
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


# -- what a SysOp can see and stop (issue #584 follow-up) --------------------


def test_the_operator_listing_shows_a_published_attestation(db, node_identity, alice):
    set_attestation_link_visible(db, alice, "name", True)
    reconcile(db, node_identity)

    records = list_issued_attestations(db, now_iso=stamp(NOW))

    assert [(r.username, r.attribute, r.status) for r in records] == [
        ("alice", "name", "published")
    ]
    assert records[0].is_live


def test_the_operator_listing_never_carries_the_attested_value(db, node_identity, alice):
    """A listing of everything the node publishes is exactly the screen design
    doc §5.5 keeps a verified real name off: the SysOp's question here is what
    is asserted about whom and until when, not what the value is."""
    set_attestation_link_visible(db, alice, "name", True)
    reconcile(db, node_identity)

    record = list_issued_attestations(db, now_iso=stamp(NOW))[0]

    assert "Alice Example" not in repr(record)
    assert not hasattr(record, "attested_value")


def test_the_listing_agrees_with_the_reconcile_about_what_is_withdrawing(db, node_identity, alice):
    """The status has to be derived from the predicate the reconcile revokes
    on, or the screen tells a SysOp one thing and the next pass does another.

    Each case below is one of the reconcile's own revocation reasons.
    """
    sysop = create_user(db, "sysop-b", password="password", user_level=SYSOP_LEVEL)
    set_attestation_link_visible(db, alice, "name", True)
    set_attestation_link_visible(db, alice, "age", True)
    reconcile(db, node_identity)
    assert {r.status for r in list_issued_attestations(db, now_iso=stamp(NOW))} == {"published"}

    # consent_withdrawn
    set_attestation_link_visible(db, alice, "name", False)
    # attested_value_replaced: re-verifying clears link_visible too, so this is
    # a distinct reason only in the reconcile; both must read as withdrawing.
    attest_age(db, alice, datetime(1991, 5, 2).date(), verifier=sysop)

    records = {r.attribute: r.status for r in list_issued_attestations(db, now_iso=stamp(NOW))}

    assert records == {"name": "withdrawing", "age": "withdrawing"}
    # And the reconcile does revoke exactly those two.
    changes = reconcile(db, node_identity, at=NOW + timedelta(hours=1))
    assert sorted(c.action for c in changes) == ["revoked", "revoked"]


def test_expired_and_revoked_rows_are_hidden_until_asked_for(db, node_identity, alice):
    set_attestation_link_visible(db, alice, "name", True)
    reconcile(db, node_identity)
    set_attestation_link_visible(db, alice, "name", False)
    reconcile(db, node_identity, at=NOW + timedelta(hours=1))

    later = stamp(NOW + timedelta(hours=2))
    assert list_issued_attestations(db, now_iso=later) == []
    history = list_issued_attestations(db, include_inactive=True, now_iso=later)
    assert [r.status for r in history] == ["revoked"]


def test_a_deleted_accounts_row_is_still_nameable_as_removed(db, node_identity, alice):
    sysop = create_user(db, "sysop-c", password="password", user_level=SYSOP_LEVEL)
    set_attestation_link_visible(db, alice, "name", True)
    reconcile(db, node_identity)
    delete_user(db, alice, deleted_by=sysop)

    record = list_issued_attestations(db, now_iso=stamp(NOW))[0]

    assert record.username is None
    assert record.user_id is None
    assert record.status == "withdrawing"


def test_a_sysop_withdrawal_revokes_and_does_not_come_back(db, node_identity, alice):
    """The trap this action exists to avoid: signing a revocation while the
    caller's consent is still set means the very next reconcile re-mints what
    was just revoked, so the withdrawal silently undoes itself."""
    sysop = create_user(db, "sysop-d", password="password", user_level=SYSOP_LEVEL)
    set_attestation_link_visible(db, alice, "name", True)
    reconcile(db, node_identity)

    assert withdraw_link_visibility(db, alice, "name", actor=sysop) is True
    changes = reconcile(db, node_identity, at=NOW + timedelta(hours=1))
    assert [(c.action, c.reason) for c in changes] == [("revoked", "consent_withdrawn")]

    # Three more passes, spread over the renewal window: nothing comes back.
    for days in (1, 40, 80):
        assert reconcile(db, node_identity, at=NOW + timedelta(days=days)) == []
    assert list_issued_attestations(db, now_iso=stamp(NOW + timedelta(days=80))) == []


def test_a_sysop_withdrawal_is_recorded_against_the_account(db, node_identity, alice):
    """A caller's own toggle is an ordinary preference; one person overriding
    another's is what the moderation log is for."""
    sysop = create_user(db, "sysop-e", password="password", user_level=SYSOP_LEVEL)
    set_attestation_link_visible(db, alice, "name", True)

    withdraw_link_visibility(db, alice, "name", actor=sysop)

    entries = [
        row["action"] for row in db.connection.execute(
            "SELECT action FROM moderation_log WHERE target_user_id = ?", (alice.id,)
        ).fetchall()
    ]
    assert "withdraw_link_name" in entries


def test_withdrawing_twice_is_not_an_error_and_logs_once(db, node_identity, alice):
    sysop = create_user(db, "sysop-f", password="password", user_level=SYSOP_LEVEL)
    set_attestation_link_visible(db, alice, "name", True)

    assert withdraw_link_visibility(db, alice, "name", actor=sysop) is True
    assert withdraw_link_visibility(db, alice, "name", actor=sysop) is False

    count = db.connection.execute(
        "SELECT COUNT(*) FROM moderation_log WHERE action = 'withdraw_link_name'"
    ).fetchone()[0]
    assert count == 1


def test_withdrawing_a_never_shared_attribute_changes_nothing(db, node_identity, alice):
    """A SysOp who removed the attestation outright has already withdrawn the
    consent attached to it, so this is a no-op rather than an error."""
    sysop = create_user(db, "sysop-g", password="password", user_level=SYSOP_LEVEL)

    assert withdraw_link_visibility(db, alice, "age", actor=sysop) is False
    with pytest.raises(AttestationError, match="unknown attestation attribute"):
        withdraw_link_visibility(db, alice, "shoe-size", actor=sysop)


# -- Codex review of #590 ---------------------------------------------------


def test_a_timestamp_that_overflows_normalization_is_a_rejected_object(db, node_identity):
    """One signed object from one peer must not end the whole sync task.

    `0001-01-01T00:00:00+23:59` parses fine and then overflows in
    `astimezone`, and `OverflowError` is not a `ValueError`, so it escaped
    every per-object handler in the pull loop.
    """
    key = nacl.signing.SigningKey.generate()
    subject = TrustSubject.user("home-node-fingerprint", "alice")
    register_subject(db, subject, first_accepted_at=stamp(NOW), now_iso=stamp(NOW))
    wire = build_remote_attestation(
        key, issuer_fingerprint=HOME, subject=subject, attribute="name",
        attested_value="Alice Example", subject_opt_in=True,
        issued_at=stamp(NOW), expires_at=stamp(NOW + timedelta(days=30)),
    )
    wire["envelope"]["payload"]["issued_at"] = "0001-01-01T00:00:00+23:59"

    with pytest.raises(ValueError):
        ingest_remote_attestation(
            db, wire, issuer_verify_key=key.verify_key, now_iso=stamp(NOW)
        )


def test_an_issuer_cannot_accumulate_attestations_without_bound(db, node_identity):
    """Per-page and per-pass limits bounded the rate; nothing bounded the
    total, so a compromised authority could grow the database until the disk
    filled by minting fresh objects for a subject it had already attested."""
    key = nacl.signing.SigningKey.generate()
    subject = TrustSubject.user("issuer-node", "alice")
    register_subject(db, subject, first_accepted_at=stamp(NOW), now_iso=stamp(NOW))
    configure_attestation_authority(
        db, "issuer-node", attributes=["name"], reason="peer", now_iso=stamp(NOW)
    )
    db.connection.executemany(
        """INSERT INTO link_remote_attestations
           (content_id, issuer_fingerprint, subject_id, attribute, attested_value,
            subject_opt_in, issued_at, expires_at, envelope_json, signature_b64, received_at)
           VALUES (?, 'issuer-node', ?, 'name', 'x', 1, ?, ?, '{}', '', ?)""",
        [
            (f"{index:064x}", subject.subject_id, stamp(NOW),
             stamp(NOW + timedelta(days=30)), stamp(NOW))
            for index in range(MAX_ACTIVE_ATTESTATIONS_PER_ISSUER)
        ],
    )
    db.connection.commit()

    wire = build_remote_attestation(
        key, issuer_fingerprint="issuer-node", subject=subject, attribute="name",
        attested_value="One More", subject_opt_in=True,
        issued_at=stamp(NOW), expires_at=stamp(NOW + timedelta(days=30)),
    )
    with pytest.raises(ValueError, match="active-attestation quota"):
        ingest_remote_attestation(
            db, wire, issuer_verify_key=key.verify_key, now_iso=stamp(NOW)
        )


def test_an_unknown_subject_is_a_named_rejection_a_later_event_can_undo(db):
    """The one rejection that is retryable, so the puller can tell it from a
    permanent one and leave its cursor where it is."""
    key = nacl.signing.SigningKey.generate()
    subject = TrustSubject.user("issuer-node", "never-met")
    configure_attestation_authority(
        db, "issuer-node", attributes=["name"], reason="peer", now_iso=stamp(NOW)
    )
    wire = build_remote_attestation(
        key, issuer_fingerprint="issuer-node", subject=subject, attribute="name",
        attested_value="Alice Example", subject_opt_in=True,
        issued_at=stamp(NOW), expires_at=stamp(NOW + timedelta(days=30)),
    )

    with pytest.raises(UnknownAttestationSubject):
        ingest_remote_attestation(
            db, wire, issuer_verify_key=key.verify_key, now_iso=stamp(NOW)
        )


def test_rotating_the_signing_key_reissues_a_live_attestation(db, node_identity, alice):
    """A subscriber resolves only the issuer's *current* operational key, so
    an object signed by the previous one stops verifying the moment the node
    rotates. Waiting for the ordinary renewal would leave the attestation
    silently broken for months."""
    set_attestation_link_visible(db, alice, "name", True)
    reconcile(db, node_identity)
    first = load_issued_attestation_page(db)[0]

    rotated = Identity(
        kind=IdentityKind.NODE, label="issuer",
        signing_key=nacl.signing.SigningKey.generate(), created_at=stamp(NOW),
    )
    changes = reconcile_issued_attestations(
        db, rotated, home_node_fingerprint=HOME, now_iso=stamp(NOW + timedelta(hours=1))
    )

    assert [(c.action, c.reason) for c in changes] == [("renewed", "signing_key_rotated")]
    served = load_issued_attestation_page(db)[0]
    assert len(served) == 2
    # The replacement verifies against the key a subscriber would now resolve.
    newest = served[-1]
    rotated.verify_key.verify(
        canonical_bytes(newest["envelope"]),
        base64.b64decode(newest["signature"]),
    )
    assert _content_id(first[0]) != _content_id(newest)


def test_a_value_that_can_never_be_exported_is_reported_not_swallowed(db, node_identity):
    """A real name past the wire's 128-byte limit is accepted locally, so the
    caller's toggle goes on and every pass then fails to build an object. That
    is not the transient race the handler was written for."""
    sysop = create_user(db, "sysop-h", password="password", user_level=SYSOP_LEVEL)
    user = create_user(db, "verbose", password="password")
    attest_name(db, user, "A" * 200, verifier=sysop)
    set_attestation_link_visible(db, user, "name", True)

    changes = reconcile(db, node_identity)

    assert [(c.action, c.attribute) for c in changes] == [("refused", "name")]
    assert "not_exportable" in changes[0].reason
    assert load_issued_attestation_page(db)[0] == []


def test_the_served_stream_survives_the_issuers_clock_going_backwards(db, node_identity, alice):
    """A cursor into a wall-clock-ordered stream skips every object signed
    while the clock was behind it, permanently -- including a revocation, so a
    subscriber would keep trusting withdrawn identity data."""
    set_attestation_link_visible(db, alice, "name", True)
    reconcile(db, node_identity)
    cursor = _content_id(load_issued_attestation_page(db)[0][0])

    # The clock steps back an hour, and consent is withdrawn in that window.
    set_attestation_link_visible(db, alice, "name", False)
    reconcile(db, node_identity, at=NOW - timedelta(hours=1))

    page, _ = load_issued_attestation_page(db, after_content_id=cursor)

    assert [item["envelope"]["object_type"] for item in page] == [
        REMOTE_ATTESTATION_REVOCATION_OBJECT_TYPE
    ]
