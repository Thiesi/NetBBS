"""Automatic equivocation signals (issue #589, design doc §12.5-§12.9).

A node that sees another node sign two different objects into the same slot of
one chain keeps both as evidence, quarantines that node's identity integrity
locally, and -- unless its SysOp turned automatic signals off -- signs a trust
signal carrying both objects for the nodes that name it a reporter. Those
reproduce the proof themselves before it counts (issue #1036).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest

from netbbs.identity.keys import Identity, IdentityKind
from netbbs.link import sync as link_sync
from netbbs.link.events import (
    build_board_genesis,
    build_board_post,
    build_board_post_edit,
    build_key_transition,
)
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import LinkNode, LinkProtocolError
from netbbs.link.transport import dial_hello, request_trust_objects
from netbbs.link.trust import (
    TrustDimension,
    TrustState,
    TrustSubject,
    clear_local_observation,
    configure_trust_domain,
    configure_trusted_reporter,
    get_effective_trust_state,
    list_local_observations,
    recompute_all_trust_states,
    withdraw_observation_publication,
)
from netbbs.link.trust_issuance import (
    MAX_AUTOMATIC_SIGNALS_PER_DAY,
    list_issued_signals,
    reconcile_issued_signals,
    record_observed_equivocation,
    set_automatic_signals_enabled,
)
from netbbs.link.trust_wire import SignedTrustObject, build_trust_pull_request, ingest_trust_objects
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_link_transport import _run_server

NOW = datetime.now(timezone.utc).replace(microsecond=0)
BOARD = "b" * 64


def stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def hello(node: LinkNode, *, addresses=None):
    return node.build_hello(
        addresses=addresses, outgoing_only=addresses is None, created_at="2026-01-01T00:00:00+00:00",
    )


class _NodeDb:
    def __init__(self, tmp_path, name: str) -> None:
        self.db = Database(tmp_path / f"{name}.db")
        self.lane = DatabaseLane(self.db.path)

    def close(self) -> None:
        self.lane.close()
        self.db.close()


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


def board_and_post(author: LinkNode) -> tuple[list[dict], dict]:
    identity = author.identity
    genesis = build_board_genesis(
        signing_identity=identity.signing_key, origin_fingerprint=identity.fingerprint,
        board_id=BOARD, name="Forks", created_at="2026-08-14T10:00:00+00:00",
    )
    post = build_board_post(
        signing_identity=identity.signing_key, home_node_fingerprint=identity.fingerprint,
        local_user_id="nib", board_id=BOARD, subject="Hello", body="first",
        created_at="2026-08-14T10:01:00+00:00",
    )
    return [genesis.to_dict(), post.to_dict()], post.to_dict()


def edit(author: LinkNode, post: dict, body: str, *, previous: str) -> dict:
    return build_board_post_edit(
        signing_identity=author.identity.signing_key, author=post["envelope"]["payload"]["author"],
        board_id=BOARD, root_post_id=post_id(post), previous_event_id=previous,
        subject="Hello", body=body, created_at="2026-08-14T10:02:00+00:00",
    ).to_dict()


def post_id(raw: dict) -> str:
    from netbbs.link.events import event_content_id

    return event_content_id(raw["envelope"])


def forked_edits(author: LinkNode, observer: LinkNode) -> tuple[dict, dict]:
    """Feed `observer` a post and one edit, then a second edit of the same head."""
    observer.handle_hello(hello(author))
    setup, post = board_and_post(author)
    first = edit(author, post, "edit one", previous=post_id(post))
    second = edit(author, post, "edit two", previous=post_id(post))
    observer.handle_events(author.identity.fingerprint, [*setup, first])
    with pytest.raises(LinkProtocolError):
        observer.handle_events(author.identity.fingerprint, [second])
    return first, second


def identity_state(db, fingerprint):
    return get_effective_trust_state(db, TrustSubject.node(fingerprint), TrustDimension.IDENTITY_INTEGRITY)


# -- detection -------------------------------------------------------------------


def test_two_edits_of_one_head_are_kept_as_evidence():
    author = LinkNode(identity=bootstrap_node_identity("forker"))
    observer = LinkNode(identity=bootstrap_node_identity("observer"))
    forked_edits(author, observer)

    [(subject, evidence)] = observer.observed_equivocations
    assert subject == author.identity.fingerprint
    assert evidence["data"]["kind"] == "signed_equivocation"
    assert len(evidence["data"]["objects"]) == 2


def test_an_edit_that_arrives_before_the_one_it_extends_is_not_a_fork():
    """Reordering: the head it extends is simply not here yet."""
    author = LinkNode(identity=bootstrap_node_identity("forker"))
    observer = LinkNode(identity=bootstrap_node_identity("observer"))
    observer.handle_hello(hello(author))
    setup, post = board_and_post(author)
    first = edit(author, post, "edit one", previous=post_id(post))
    second = edit(author, post, "edit two", previous=post_id(first))
    observer.handle_events(author.identity.fingerprint, setup)
    with pytest.raises(LinkProtocolError):
        observer.handle_events(author.identity.fingerprint, [second])

    assert observer.observed_equivocations == []


def test_a_resend_of_the_same_edit_is_not_a_fork():
    author = LinkNode(identity=bootstrap_node_identity("forker"))
    observer = LinkNode(identity=bootstrap_node_identity("observer"))
    observer.handle_hello(hello(author))
    setup, post = board_and_post(author)
    first = edit(author, post, "edit one", previous=post_id(post))
    observer.handle_events(author.identity.fingerprint, [*setup, first])
    observer.handle_events(author.identity.fingerprint, [first])

    assert observer.observed_equivocations == []


def test_a_root_key_that_forks_its_own_chain_is_kept_as_evidence():
    author = LinkNode(identity=bootstrap_node_identity("forker"))
    observer = LinkNode(identity=bootstrap_node_identity("observer"))
    observer.handle_hello(hello(author))
    signing = [t for t in author.identity.transitions if t.payload["purpose"] == "signing"]
    fork = build_key_transition(
        root=author.identity.root, purpose="signing", action="authorize",
        operational_key=Identity.generate(IdentityKind.NODE, "second").verify_key,
        previous_transition_id=signing[0].payload.get("previous_transition_id"),
        created_at="2026-08-14T10:00:00+00:00",
    )
    with pytest.raises(LinkProtocolError):
        observer.handle_events(author.identity.fingerprint, [fork.to_dict()])

    [(subject, evidence)] = observer.observed_equivocations
    assert subject == author.identity.fingerprint
    assert {o["envelope"]["object_type"] for o in evidence["data"]["objects"]} == {"key_transition"}


# -- recording and issuing -------------------------------------------------------


def _observed(db, author=None):
    author = author or LinkNode(identity=bootstrap_node_identity("forker"))
    observer = LinkNode(identity=bootstrap_node_identity("observer"))
    forked_edits(author, observer)
    [(subject, evidence)] = observer.observed_equivocations
    assert record_observed_equivocation(db, subject, evidence, now_iso=stamp(NOW))
    return observer, subject


def test_observed_equivocation_quarantines_here(db):
    _observer, subject = _observed(db)
    state = identity_state(db, subject)
    assert (state.state, state.reason_code) == (TrustState.QUARANTINED, "local_self_verifying_evidence")


def test_it_is_published_automatically_and_revoked_when_cleared(db):
    observer, subject = _observed(db)
    own = observer.identity.fingerprint

    changes = reconcile_issued_signals(db, observer.identity.signing_key, home_node_fingerprint=own,
                                       now_iso=stamp(NOW))
    assert [(c.action, c.subject_fingerprint) for c in changes] == [("issued", subject)]
    assert reconcile_issued_signals(db, observer.identity.signing_key, home_node_fingerprint=own,
                                    now_iso=stamp(NOW)) == []

    [observation] = list_local_observations(db, TrustSubject.node(subject))
    clear_local_observation(db, observation.observation_id, now_iso=stamp(NOW))
    changes = reconcile_issued_signals(db, observer.identity.signing_key, home_node_fingerprint=own,
                                       now_iso=stamp(NOW))
    assert [(c.action, c.reason) for c in changes] == [("revoked", "observation_cleared")]
    [signal] = list_issued_signals(db, home_node_fingerprint=own)
    assert signal.revoked_at is not None


def test_the_switch_off_issues_nothing_and_revokes_what_is_live(db):
    observer, subject = _observed(db)
    own = observer.identity.fingerprint
    set_automatic_signals_enabled(db, False)
    assert reconcile_issued_signals(db, observer.identity.signing_key, home_node_fingerprint=own,
                                    now_iso=stamp(NOW)) == []

    set_automatic_signals_enabled(db, True)
    reconcile_issued_signals(db, observer.identity.signing_key, home_node_fingerprint=own, now_iso=stamp(NOW))
    set_automatic_signals_enabled(db, False)
    changes = reconcile_issued_signals(db, observer.identity.signing_key, home_node_fingerprint=own,
                                       now_iso=stamp(NOW))
    assert [(c.action, c.reason) for c in changes] == [("revoked", "automatic_signals_off")]


def test_a_withdrawn_signal_is_revoked_and_never_signed_again(db):
    observer, subject = _observed(db)
    own = observer.identity.fingerprint
    reconcile_issued_signals(db, observer.identity.signing_key, home_node_fingerprint=own, now_iso=stamp(NOW))
    [observation] = list_local_observations(db, TrustSubject.node(subject))

    withdraw_observation_publication(db, observation.observation_id, now_iso=stamp(NOW))
    changes = reconcile_issued_signals(db, observer.identity.signing_key, home_node_fingerprint=own,
                                       now_iso=stamp(NOW))
    assert [(c.action, c.reason) for c in changes] == [("revoked", "withdrawn_by_sysop")]
    assert reconcile_issued_signals(db, observer.identity.signing_key, home_node_fingerprint=own,
                                    now_iso=stamp(NOW)) == []
    # The evidence itself stays: the subject is still quarantined here.
    assert identity_state(db, subject).state == TrustState.QUARANTINED


def test_at_most_a_few_signals_a_day(db):
    observer = LinkNode(identity=bootstrap_node_identity("observer"))
    own = observer.identity.fingerprint
    for number in range(MAX_AUTOMATIC_SIGNALS_PER_DAY + 2):
        author = LinkNode(identity=bootstrap_node_identity(f"forker-{number}"))
        local = LinkNode(identity=bootstrap_node_identity("local"))
        forked_edits(author, local)
        [(subject, evidence)] = local.observed_equivocations
        record_observed_equivocation(db, subject, evidence, now_iso=stamp(NOW))

    changes = reconcile_issued_signals(db, observer.identity.signing_key, home_node_fingerprint=own,
                                       now_iso=stamp(NOW))
    assert [c.action for c in changes].count("issued") == MAX_AUTOMATIC_SIGNALS_PER_DAY
    assert [c.action for c in changes].count("deferred") == 2
    later = reconcile_issued_signals(db, observer.identity.signing_key, home_node_fingerprint=own,
                                     now_iso=stamp(NOW + timedelta(days=1, minutes=1)))
    assert [c.action for c in later].count("issued") == 2


def test_never_about_this_node_itself(db):
    """Even with evidence on file about it, a node does not accuse itself."""
    observer, subject = _observed(db)
    changes = reconcile_issued_signals(
        db, observer.identity.signing_key, home_node_fingerprint=subject, now_iso=stamp(NOW),
    )
    assert changes == []


# -- recovery --------------------------------------------------------------------


def test_equivocation_recovery_waits_for_a_sysop_to_clear_it(db):
    _observer, subject = _observed(db)
    after_expiry = NOW + timedelta(days=91)
    recompute_all_trust_states(db, now_iso=stamp(after_expiry))
    state = identity_state(db, subject)
    assert (state.state, state.reason_code) == (TrustState.QUARANTINED, "equivocation_review_required")

    recompute_all_trust_states(db, now_iso=stamp(after_expiry + timedelta(days=30)))
    assert identity_state(db, subject).reason_code == "equivocation_review_required"

    [observation] = list_local_observations(db, TrustSubject.node(subject))
    clear_local_observation(db, observation.observation_id, now_iso=stamp(after_expiry + timedelta(days=30)))
    assert identity_state(db, subject).reason_code == "recovery_hold"
    recompute_all_trust_states(db, now_iso=stamp(after_expiry + timedelta(days=31, hours=1)))
    assert identity_state(db, subject).reason_code == "automatic_recovery"


# -- end to end: observed, signed, pulled, reproduced, counted -------------------


def test_two_observers_signals_reach_a_subscriber_that_verifies_and_counts_them(tmp_path):
    """B and D each see A fork a post, record it and sign a signal through
    the sync pass's own steps. S names both reporters, in two domains, pulls
    both over real transport, reproduces the proof and quarantines A."""
    author = LinkNode(identity=bootstrap_node_identity("forker"))
    subscriber = LinkNode(identity=bootstrap_node_identity("subscriber"))
    subscriber_db = _NodeDb(tmp_path, "subscriber")
    subscriber.handle_hello(hello(author))
    observers = []
    for name, domain in (("observer-b", "domain-b"), ("observer-d", "domain-d")):
        node = LinkNode(identity=bootstrap_node_identity(name))
        store = _NodeDb(tmp_path, name)
        forked_edits(author, node)
        asyncio.run(link_sync._record_observed_equivocations(node, store.lane))
        asyncio.run(link_sync._reconcile_own_signals(node, store.lane))
        configure_trust_domain(subscriber_db.db, domain, display_name=domain)
        configure_trusted_reporter(
            subscriber_db.db, node.identity.fingerprint, domain_id=domain,
            scopes=[(TrustDimension.IDENTITY_INTEGRITY, "signed_equivocation")],
        )
        observers.append((node, store))

    async def pull(observer: LinkNode, store: _NodeDb) -> None:
        server = await _run_server(observer, lambda: hello(observer), store.lane)
        try:
            url = f"http://127.0.0.1:{server.port}"
            async with aiohttp.ClientSession() as session:
                await dial_hello(subscriber, session, url, hello(subscriber), subscriber_db.lane)
                request = build_trust_pull_request(
                    signing_identity=subscriber.identity.signing_key,
                    requester_fingerprint=subscriber.identity.fingerprint,
                    responder_fingerprint=observer.identity.fingerprint,
                    issuer_fingerprint=observer.identity.fingerprint,
                )
                raw, _more = await request_trust_objects(subscriber, session, url, request)
            key = subscriber.resolve_peer_signing_key(observer.identity.fingerprint)
            parsed = [SignedTrustObject.from_dict(item, issuer_verify_key=key) for item in raw]
            assert len(parsed) == 1
            await subscriber_db.lane.run(
                ingest_trust_objects, parsed, subject_keys=link_sync._signal_subject_keys(subscriber, parsed),
            )
        finally:
            await server.stop()

    try:
        asyncio.run(pull(*observers[0]))
        assert identity_state(subscriber_db.db, author.identity.fingerprint).state != TrustState.QUARANTINED
        asyncio.run(pull(*observers[1]))
        state = identity_state(subscriber_db.db, author.identity.fingerprint)
        assert (state.state, state.reason_code) == (TrustState.QUARANTINED, "remote_domain_threshold")
        assert all(item["evidence_verified"] for item in state.explanation["active_remote_evidence"])
    finally:
        subscriber_db.close()
        for _node, store in observers:
            store.close()
