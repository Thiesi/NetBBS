"""The hour-long Link waits, seen and ended (issue #700).

Several things a node cannot use or deliver are put off for an hour so that an
unfixable refusal does not cost a download on every pass. This covers what
releases them -- every SysOp trust change, automatic graduation, and the
SysOp's own `[R]etry now` -- and that a fresher descriptor for a node known
only by introduction is used at once rather than at the next hourly refresh.
"""

from __future__ import annotations

import asyncio
import time

from netbbs.link.events import build_endpoint_descriptor
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import (
    REPORTER_REFRESH,
    DeferredEvents,
    HelloMessage,
    LinkNode,
    PeerExchange,
    PeerListMessage,
)
from netbbs.link.sync import run_link_sync
from netbbs.link.trust import (
    TrustDimension,
    TrustState,
    TrustSubject,
    register_subject,
    set_trust_override,
    trust_policy_generation,
)
from netbbs.link.trust_carriage import (
    record_trust_deposit_refusal,
    relays_refusing_trust_deposits,
    release_trust_deposit_backoffs,
    trust_deposit_refused_recently,
    trust_deposits_waiting,
)
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

FAR = 9e12


def _hello(node: LinkNode, *, created_at: str = "2026-01-01T00:00:00+00:00") -> HelloMessage:
    return node.build_hello(addresses=None, outgoing_only=True, created_at=created_at)


# -- in memory -------------------------------------------------------------------------------


def test_waiting_groups_set_aside_events_by_why_and_soonest_retry():
    held = DeferredEvents()
    held.entries["1" * 64] = ("boards", "b", None, 300.0, "remote", None)
    held.entries["2" * 64] = ("boards", "b", None, 200.0, "remote", None)
    held.entries["3" * 64] = ("boards", "b", "stranger", 400.0, None, None)
    held.entries["4" * 64] = ("boards", "b", None, 500.0, None, None)

    assert held.waiting() == {
        ("held", "remote"): (2, 200.0),
        ("unknown", "stranger"): (1, 400.0),
        ("waiting", None): (1, 500.0),
    }
    assert held.release_all() == 4
    assert held.entries == {}


def test_release_waits_forgets_every_hour_long_wait_in_memory():
    node = LinkNode(identity=bootstrap_node_identity("waiting"))
    node.deferred_events.entries["1" * 64] = ("boards", "b", "x", FAR, None, None)
    node.peer_exchange["peer"] = PeerExchange(set_aside={"2" * 64: ("reason", FAR), "3" * 64: ("reason", FAR)})
    node.unanswered_identities[("carrier", "who")] = FAR
    node.unanswered_identities[(REPORTER_REFRESH, "reporter")] = FAR

    released = node.release_waits()

    assert (released.set_aside, released.refused_at_peers, released.introductions) == (1, 2, 2)
    assert node.deferred_events.entries == {}
    assert node.peer_exchange["peer"].set_aside == {}
    assert node.unanswered_identities == {}


def test_a_newer_signed_descriptor_refreshes_a_node_known_only_by_introduction():
    """From the Phase 4 exercise: a reporter known by introduction gained a
    relay; its new descriptor arrived in a peer list and sat among the
    candidates while the trust pull read the hour-old introduced copy."""
    r, a, b = (LinkNode(identity=bootstrap_node_identity(name)) for name in ("R", "A", "B"))
    for dialer in (a, b):
        r.handle_hello(_hello(dialer))
        dialer.handle_hello(_hello(r))
    b.handle_introduction(_hello(a))
    assert a.identity.fingerprint in b.introduced

    newer = build_endpoint_descriptor(
        signing_identity=a.identity.signing_key, subject_fingerprint=a.identity.fingerprint,
        addresses=None, outgoing_only=True, created_at="2026-02-01T00:00:00+00:00",
        relays=[r.identity.fingerprint],
    )
    forged = build_endpoint_descriptor(
        signing_identity=bootstrap_node_identity("mallory").signing_key,
        subject_fingerprint=a.identity.fingerprint, addresses=None, outgoing_only=True,
        created_at="2026-03-01T00:00:00+00:00", relays=["elsewhere"],
    )

    b.handle_peer_list(r.identity.fingerprint, PeerListMessage(descriptors=(forged,)))
    assert b.introduced[a.identity.fingerprint].descriptor.payload.get("relays") in (None, [])

    recorded = b.handle_peer_list(r.identity.fingerprint, PeerListMessage(descriptors=(newer,)))
    assert a.identity.fingerprint in recorded
    assert b.introduced[a.identity.fingerprint].descriptor.payload["relays"] == [r.identity.fingerprint]
    assert a.identity.fingerprint not in b.peers, "still only known, never a peer"


# -- the sync loop ---------------------------------------------------------------------------


class _Db:
    def __init__(self, tmp_path, name):
        self.db = Database(tmp_path / f"{name}.db")
        self.lane = DatabaseLane(self.db.path)

    def close(self):
        self.lane.close()
        self.db.close()


def _run(node, store, *, interval: float, on_pass):
    """Run `run_link_sync` with no seeds; `on_pass(n)` is called at the top of
    pass n and returns True to make it the last."""
    stop = asyncio.Event()
    passes = {"n": 0}

    class Hello:
        async def refresh(self, lane):
            passes["n"] += 1
            if on_pass(passes["n"]):
                stop.set()

        def __call__(self):
            return _hello(node)

    async def scenario():
        await asyncio.wait_for(
            run_link_sync(node, None, [], Hello(), store.lane, interval_seconds=interval, stop_event=stop),
            timeout=20,
        )

    asyncio.run(scenario())
    return passes["n"]


def test_wake_sync_runs_a_pass_at_once_instead_of_after_the_interval(tmp_path):
    node = LinkNode(identity=bootstrap_node_identity("woken"))
    store = _Db(tmp_path, "woken")
    started = time.monotonic()

    def on_pass(n):
        if n == 1:
            node.wake_sync()  # the SysOp pressed [R]etry now during this pass
        return n >= 2

    try:
        assert _run(node, store, interval=600.0, on_pass=on_pass) == 2
    finally:
        store.close()
    assert time.monotonic() - started < 15, "the second pass did not wait out the ten-minute interval"
    assert not node.sync_wake.is_set(), "the wake is spent once it has been answered"


def test_a_trust_change_made_offline_releases_what_was_set_aside(tmp_path):
    """`python -m netbbs.admin` cannot reach the running node; the change is
    seen on the next pass from the trust tables themselves."""
    node = LinkNode(identity=bootstrap_node_identity("watching"))
    store = _Db(tmp_path, "watching")
    subject = TrustSubject.node("remote-node")
    register_subject(store.db, subject, first_accepted_at="2026-08-01T00:00:00.000000Z")
    before = trust_policy_generation(store.db)

    def on_pass(n):
        if n == 1:
            node.deferred_events.entries["1" * 64] = ("boards", "b", None, FAR, "remote-node", None)
            node.deferred_events.entries["2" * 64] = ("boards", "b", None, FAR, "someone-else", None)
            # What the offline CLI writes, between this pass and the next.
            set_trust_override(
                store.db, subject, TrustDimension.IDENTITY_INTEGRITY, TrustState.ESTABLISHED, reason="known",
            )
            node.wake_sync()
        return n >= 2

    try:
        assert trust_policy_generation(store.db) == before
        _run(node, store, interval=600.0, on_pass=on_pass)
        assert trust_policy_generation(store.db) != before
    finally:
        store.close()
    assert node.deferred_events.entries == {}


def test_a_pass_without_a_trust_change_keeps_what_was_set_aside(tmp_path):
    node = LinkNode(identity=bootstrap_node_identity("steady"))
    store = _Db(tmp_path, "steady")

    def on_pass(n):
        if n == 1:
            node.deferred_events.entries["1" * 64] = ("boards", "b", None, FAR, "remote-node", None)
            node.wake_sync()
        return n >= 2

    try:
        _run(node, store, interval=600.0, on_pass=on_pass)
    finally:
        store.close()
    assert list(node.deferred_events.entries) == ["1" * 64]


def test_a_node_that_graduates_on_its_own_has_its_content_retried(tmp_path):
    """Automatic graduation or recovery is a trust change nobody made: the
    re-evaluation every pass runs releases what was held back from that node."""
    from datetime import datetime, timedelta, timezone

    from netbbs.link.trust import EvidenceClass, clear_local_observation, record_local_observation

    def stamp(value):
        return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    node = LinkNode(identity=bootstrap_node_identity("recovering"))
    store = _Db(tmp_path, "recovering")
    now = datetime.now(timezone.utc)
    subject = TrustSubject.node("held-node")
    register_subject(store.db, subject, first_accepted_at=stamp(now - timedelta(days=60)),
                     now_iso=stamp(now - timedelta(days=60)))
    record_local_observation(
        store.db, observation_id="proof", subject=subject,
        dimension=TrustDimension.IDENTITY_INTEGRITY, category="signed_equivocation",
        evidence_class=EvidenceClass.SELF_VERIFYING,
        observed_at=stamp(now - timedelta(hours=26)), now_iso=stamp(now - timedelta(hours=26)),
    )
    clear_local_observation(store.db, "proof", now_iso=stamp(now - timedelta(hours=25)))
    node.deferred_events.entries["1" * 64] = ("boards", "b", None, FAR, "held-node", None)
    # Waiting to learn of another node: nothing about this graduation helps it.
    node.deferred_events.entries["2" * 64] = ("boards", "b", "another-node", FAR, None, None)

    try:
        _run(node, store, interval=600.0, on_pass=lambda n: True)
    finally:
        store.close()
    assert list(node.deferred_events.entries) == ["2" * 64]


# -- the trust-deposit backoff ---------------------------------------------------------------


def test_releasing_the_deposit_backoff_keeps_what_the_relay_said(tmp_path):
    db = Database(tmp_path / "node.db")
    try:
        record_trust_deposit_refusal(db, "relay-1", "HTTP 403: not established")
        assert trust_deposit_refused_recently(db, "relay-1")
        [(relay, retry_at)] = trust_deposits_waiting(db)
        assert relay == "relay-1" and retry_at > "2026"

        assert release_trust_deposit_backoffs(db) == 1
        assert not trust_deposit_refused_recently(db, "relay-1")
        assert trust_deposits_waiting(db) == []
        # The vouch screen still says which relay refused, until it answers.
        assert relays_refusing_trust_deposits(db, ["relay-1"]) == ["relay-1"]
        assert release_trust_deposit_backoffs(db) == 0
    finally:
        db.close()
