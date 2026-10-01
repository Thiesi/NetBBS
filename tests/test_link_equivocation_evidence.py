"""A self-verifying trust signal counts only if its evidence reproduces here
(issue #1036, design doc §12.6).

Before this, `ingest_trust_objects` stored an embedded `self_verifying` signal
and counted it on its label: a reporter in scope could quarantine a node with
"evidence" of `{"proof": 1}`. The only integrity violation a receiver can prove
to itself is signed equivocation -- two different objects one key signed into
the same slot of one chain -- so that is what reproduces, judged against the
keys the receiver itself holds for the subject.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from netbbs.identity.keys import Identity, IdentityKind
from netbbs.link import sync as link_sync
from netbbs.link.equivocation import (
    SubjectKeys,
    build_equivocation_evidence,
    reproduce_equivocation,
    subject_keys_from_record,
)
from netbbs.link.events import build_board_post_edit, build_key_transition
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import HelloMessage, LinkNode
from netbbs.link.trust import (
    EvidenceClass,
    TrustDimension,
    TrustState,
    TrustSubject,
    configure_trust_domain,
    configure_trusted_reporter,
    get_effective_trust_state,
)
from netbbs.link.trust_wire import build_trust_signal, ingest_trust_objects
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

# The real clock: the per-pass re-check reads the time itself.
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def hello(node: LinkNode) -> HelloMessage:
    return node.build_hello(addresses=None, outgoing_only=True, created_at="2026-01-01T00:00:00+00:00")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def reporter():
    return Identity.generate(IdentityKind.NODE, "reporter")


@pytest.fixture
def subject_node():
    return LinkNode(identity=bootstrap_node_identity("equivocator"))


def configure_reporter(db, fingerprint, categories=("signed_equivocation",), *, domain="domain-a"):
    configure_trust_domain(db, domain, display_name=domain, now_iso=stamp(NOW))
    configure_trusted_reporter(
        db, fingerprint, domain_id=domain,
        scopes=[(TrustDimension.IDENTITY_INTEGRITY, category) for category in categories],
        now_iso=stamp(NOW),
    )


def keys_of(node: LinkNode) -> SubjectKeys:
    receiver = LinkNode(identity=bootstrap_node_identity("receiver"))
    receiver.handle_hello(hello(node))
    return subject_keys_from_record(receiver.known_identity(node.identity.fingerprint))


def edit(signer, body: str, *, previous: str = "head-1") -> dict:
    return build_board_post_edit(
        signing_identity=signer, author={"kind": "node_vouched_user", "home_node_fingerprint": "x",
                                         "local_user_id": "nib"},
        board_id="b" * 64, root_post_id="r" * 64, previous_event_id=previous,
        subject="Re", body=body, created_at="2026-08-14T10:00:00+00:00",
    ).to_dict()


def key_fork(node: LinkNode) -> tuple[dict, dict]:
    first = node.identity.transitions[0]
    twins = [
        build_key_transition(
            root=node.identity.root, purpose="signing", action="authorize",
            operational_key=Identity.generate(IdentityKind.NODE, f"k{n}").verify_key,
            previous_transition_id=first.content_id, created_at="2026-08-14T10:00:00+00:00",
        ).to_dict()
        for n in (1, 2)
    ]
    return twins[0], twins[1]


def signal(reporter, subject_fingerprint, evidence, *, category="signed_equivocation", number=1):
    return build_trust_signal(
        signing_identity=reporter, issuer_fingerprint=reporter.fingerprint, signal_id=f"s-{number}",
        subject=TrustSubject.node(subject_fingerprint), dimension=TrustDimension.IDENTITY_INTEGRITY,
        category=category, evidence_class=EvidenceClass.SELF_VERIFYING, evidence=evidence,
        observed_at=stamp(NOW - timedelta(hours=2)), issued_at=stamp(NOW - timedelta(hours=1)),
        expires_at=stamp(NOW + timedelta(days=30)),
    )


def verified_at(db, content_id):
    return db.connection.execute(
        "SELECT evidence_verified_at FROM link_trust_signals WHERE content_id = ?", (content_id,)
    ).fetchone()[0]


def identity_state(db, fingerprint):
    return get_effective_trust_state(db, TrustSubject.node(fingerprint), TrustDimension.IDENTITY_INTEGRITY)


# -- reproduction itself -------------------------------------------------------


def test_two_edits_one_key_signed_into_one_slot_reproduce(subject_node):
    signer = subject_node.identity.signing_key
    evidence = build_equivocation_evidence(edit(signer, "one"), edit(signer, "two"))
    assert reproduce_equivocation(
        evidence["data"], subject_fingerprint=subject_node.identity.fingerprint, keys=keys_of(subject_node),
    )


def test_a_root_key_that_forks_its_own_chain_reproduces(subject_node):
    evidence = build_equivocation_evidence(*key_fork(subject_node))
    assert reproduce_equivocation(
        evidence["data"], subject_fingerprint=subject_node.identity.fingerprint, keys=keys_of(subject_node),
    )


def test_what_is_not_equivocation_does_not_reproduce(subject_node):
    fingerprint = subject_node.identity.fingerprint
    keys = keys_of(subject_node)
    signer = subject_node.identity.signing_key
    stranger = Identity.generate(IdentityKind.NODE, "stranger")
    same = edit(signer, "one")
    cases = {
        "the same object twice": {"kind": "signed_equivocation", "objects": [same, same]},
        "two different slots": build_equivocation_evidence(
            edit(signer, "one"), edit(signer, "two", previous="head-2"))["data"],
        "signed by someone else": build_equivocation_evidence(
            edit(stranger, "one"), edit(stranger, "two"))["data"],
        "one of each": build_equivocation_evidence(edit(signer, "one"), edit(stranger, "two"))["data"],
        "a bare claim": {"proof": 1},
        "one object": {"kind": "signed_equivocation", "objects": [same]},
        "garbage": {"kind": "signed_equivocation", "objects": [{"envelope": 1, "signature": "!"}, same]},
    }
    for name, data in cases.items():
        assert not reproduce_equivocation(data, subject_fingerprint=fingerprint, keys=keys), name
    # A key-transition fork proves something only about the node whose root signed it.
    other = LinkNode(identity=bootstrap_node_identity("other"))
    assert not reproduce_equivocation(
        build_equivocation_evidence(*key_fork(other))["data"], subject_fingerprint=fingerprint, keys=keys,
    )


# -- admission -------------------------------------------------------------------


def test_reproduced_evidence_counts_toward_the_two_domain_threshold(db, reporter, subject_node):
    """Verified proof counts; it does not stand in for a second, independent
    domain, so one reporter alone does not quarantine (design doc §12.6)."""
    second = Identity.generate(IdentityKind.NODE, "second-reporter")
    configure_reporter(db, reporter.fingerprint)
    configure_reporter(db, second.fingerprint, domain="domain-b")
    fingerprint = subject_node.identity.fingerprint
    signer = subject_node.identity.signing_key
    evidence = build_equivocation_evidence(edit(signer, "one"), edit(signer, "two"))
    keys = {fingerprint: keys_of(subject_node)}

    first = signal(reporter, fingerprint, evidence)
    ingest_trust_objects(db, [first], now_iso=stamp(NOW), subject_keys=keys)
    assert verified_at(db, first.content_id) is not None
    assert identity_state(db, fingerprint).state == TrustState.PROBATIONARY

    other = signal(second, fingerprint, evidence, number=2)
    ingest_trust_objects(db, [other], now_iso=stamp(NOW), subject_keys=keys)
    state = identity_state(db, fingerprint)
    assert state.state == TrustState.QUARANTINED
    assert state.reason_code == "remote_domain_threshold"
    assert not state.explanation["active_local_evidence"]


def test_labelled_claims_without_proof_are_kept_and_count_for_nothing(db, reporter, subject_node):
    """Before #1036, two domains' worth of these quarantined the subject."""
    second = Identity.generate(IdentityKind.NODE, "second-reporter")
    configure_reporter(db, reporter.fingerprint)
    configure_reporter(db, second.fingerprint, domain="domain-b")
    fingerprint = subject_node.identity.fingerprint
    claims = [signal(issuer, fingerprint, {"mode": "embedded", "data": {"proof": n}}, number=n)
              for n, issuer in enumerate((reporter, second))]

    for claim in claims:
        result = ingest_trust_objects(
            db, [claim], now_iso=stamp(NOW), subject_keys={fingerprint: keys_of(subject_node)},
        )
        assert result[0] == [claim.content_id]
        assert verified_at(db, claim.content_id) is None
    assert identity_state(db, fingerprint).state != TrustState.QUARANTINED


def test_a_forged_proof_counts_for_nothing(db, reporter, subject_node):
    configure_reporter(db, reporter.fingerprint)
    fingerprint = subject_node.identity.fingerprint
    forger = Identity.generate(IdentityKind.NODE, "forger")
    obj = signal(reporter, fingerprint, build_equivocation_evidence(edit(forger, "one"), edit(forger, "two")))

    ingest_trust_objects(db, [obj], now_iso=stamp(NOW), subject_keys={fingerprint: keys_of(subject_node)})

    assert verified_at(db, obj.content_id) is None
    assert identity_state(db, fingerprint).state != TrustState.QUARANTINED


def test_other_integrity_categories_never_verify_from_a_remote_issuer(db, reporter, subject_node):
    """A revoked key's signature, an invalid signature or a disputed authority
    proves nothing a receiver can check on its own (design doc §12.5)."""
    categories = ("revoked_key_use", "invalid_authority", "invalid_signature_delivery")
    configure_reporter(db, reporter.fingerprint, categories)
    fingerprint = subject_node.identity.fingerprint
    signer = subject_node.identity.signing_key
    evidence = build_equivocation_evidence(edit(signer, "one"), edit(signer, "two"))
    objects = [signal(reporter, fingerprint, evidence, category=c, number=n) for n, c in enumerate(categories)]

    ingest_trust_objects(db, objects, now_iso=stamp(NOW), subject_keys={fingerprint: keys_of(subject_node)})

    assert [verified_at(db, obj.content_id) for obj in objects] == [None, None, None]
    assert identity_state(db, fingerprint).state != TrustState.QUARANTINED


def test_evidence_about_a_node_not_yet_known_verifies_once_it_is(tmp_path, reporter, subject_node):
    """Unknown keys at admission are not a failure: the next sync pass after
    this node learns them re-checks the evidence."""
    db = Database(tmp_path / "node.db")
    lane = DatabaseLane(db.path)
    try:
        configure_reporter(db, reporter.fingerprint)
        fingerprint = subject_node.identity.fingerprint
        signer = subject_node.identity.signing_key
        obj = signal(reporter, fingerprint, build_equivocation_evidence(edit(signer, "one"), edit(signer, "two")))
        ingest_trust_objects(db, [obj], now_iso=stamp(NOW), subject_keys={})
        assert verified_at(db, obj.content_id) is None

        receiver = LinkNode(identity=bootstrap_node_identity("receiver"))
        asyncio.run(link_sync._reverify_signal_evidence(receiver, lane))
        assert verified_at(db, obj.content_id) is None, "still unknown: nothing to check against"

        receiver.handle_hello(hello(subject_node))
        asyncio.run(link_sync._reverify_signal_evidence(receiver, lane))
        assert verified_at(db, obj.content_id) is not None
    finally:
        lane.close()
        db.close()
