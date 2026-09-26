"""
Tests for `netbbs.link.relay_selection` (design doc §12, issue
#58) -- the pure "what to do next" relay-candidate-ranking and self-
healing-detection logic, deliberately built on plain `LinkNode.peers`/
`candidate_descriptors` state rather than a real handshake: this module
never verifies anything (that already happened when those dicts were
populated), so a test only needs *shape*, not cryptographically valid
signatures, matching `netbbs.link.reliability`'s own "plain db-first
functions" testing style rather than `test_link_protocol.py`'s full
two-node handshake setup.
"""

from __future__ import annotations

import pytest

from netbbs.identity.keys import Identity, IdentityKind
from netbbs.link.events import EndpointDescriptor, build_endpoint_descriptor
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import LinkNode, PeerRecord
from netbbs.link.relay_selection import (
    TARGET_RELAY_COUNT,
    relays_needing_replacement,
    select_relay_candidates,
)
from netbbs.link.reliability import record_dial_outcome
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice_node():
    return LinkNode(identity=bootstrap_node_identity("alice"))


def _full_peer_descriptor(fingerprint: str, *, outgoing_only: bool = False) -> EndpointDescriptor:
    signer = Identity.generate(IdentityKind.NODE, fingerprint)
    addresses = None if outgoing_only else [{"protocol": "http", "address": "198.51.100.1", "port": 7862}]
    return build_endpoint_descriptor(
        signing_identity=signer,
        subject_fingerprint=fingerprint,
        addresses=addresses,
        outgoing_only=outgoing_only,
        created_at="2026-01-01T00:00:00Z",
    )


def _add_peer(node: LinkNode, fingerprint: str, *, outgoing_only: bool = False) -> None:
    node.peers[fingerprint] = PeerRecord(
        fingerprint=fingerprint,
        root_public_key=b"\x00" * 32,
        transitions=(),
        descriptor=_full_peer_descriptor(fingerprint, outgoing_only=outgoing_only),
    )


def _add_candidate(node: LinkNode, fingerprint: str, *, outgoing_only: bool = False) -> None:
    node.candidate_descriptors[fingerprint] = _full_peer_descriptor(fingerprint, outgoing_only=outgoing_only)


# -- select_relay_candidates --------------------------------------------------


def test_select_relay_candidates_ranks_most_reliable_first(db, alice_node):
    _add_peer(alice_node, "steady-bob")
    _add_peer(alice_node, "flaky-carol")
    _add_peer(alice_node, "never-dialed-dan")

    record_dial_outcome(db, "steady-bob", succeeded=True)
    record_dial_outcome(db, "steady-bob", succeeded=True)
    record_dial_outcome(db, "flaky-carol", succeeded=False)
    record_dial_outcome(db, "flaky-carol", succeeded=False)

    selected = select_relay_candidates(db, alice_node)

    assert selected[0] == "steady-bob"
    assert selected[-1] == "flaky-carol"
    assert "never-dialed-dan" in selected


def test_select_relay_candidates_excludes_outgoing_only_peers(db, alice_node):
    _add_peer(alice_node, "full-peer-bob", outgoing_only=False)
    _add_peer(alice_node, "outgoing-only-carol", outgoing_only=True)

    selected = select_relay_candidates(db, alice_node)

    assert "full-peer-bob" in selected
    assert "outgoing-only-carol" not in selected


def test_select_relay_candidates_includes_unverified_candidates(db, alice_node):
    _add_candidate(alice_node, "candidate-bob")

    selected = select_relay_candidates(db, alice_node)

    assert "candidate-bob" in selected


def test_select_relay_candidates_excludes_already_serving(db, alice_node):
    _add_peer(alice_node, "already-serving-bob")
    _add_peer(alice_node, "not-yet-asked-carol")
    alice_node.relays_serving_me["already-serving-bob"] = "2026-01-01T00:00:00Z"

    selected = select_relay_candidates(db, alice_node)

    assert "already-serving-bob" not in selected
    assert "not-yet-asked-carol" in selected


def test_select_relay_candidates_excludes_already_pending(db, alice_node):
    from netbbs.link.events import build_relay_consent_request

    _add_peer(alice_node, "already-asked-bob")
    signer = Identity.generate(IdentityKind.NODE, "alice")
    alice_node.pending_own_relay_requests["already-asked-bob"] = build_relay_consent_request(
        signing_identity=signer,
        requester_fingerprint=alice_node.identity.fingerprint,
        relay_fingerprint="already-asked-bob",
        created_at="2026-01-01T00:00:00Z",
    )

    selected = select_relay_candidates(db, alice_node)

    assert "already-asked-bob" not in selected


def test_select_relay_candidates_never_includes_self(db, alice_node):
    _add_peer(alice_node, alice_node.identity.fingerprint)

    selected = select_relay_candidates(db, alice_node)

    assert alice_node.identity.fingerprint not in selected


def test_select_relay_candidates_returns_empty_once_target_count_reached(db, alice_node):
    for i in range(TARGET_RELAY_COUNT):
        alice_node.relays_serving_me[f"relay-{i}"] = "2026-01-01T00:00:00Z"
    _add_peer(alice_node, "spare-candidate")

    assert select_relay_candidates(db, alice_node) == []


def test_select_relay_candidates_limits_to_remaining_slots(db, alice_node):
    alice_node.relays_serving_me["relay-0"] = "2026-01-01T00:00:00Z"
    for i in range(10):
        _add_peer(alice_node, f"candidate-{i}")

    selected = select_relay_candidates(db, alice_node)

    assert len(selected) == TARGET_RELAY_COUNT - 1


# -- relays_needing_replacement -----------------------------------------------


def test_relays_needing_replacement_flags_a_relay_below_the_floor(db, alice_node):
    alice_node.relays_serving_me["unreliable-bob"] = "2026-01-01T00:00:00Z"
    for _ in range(5):
        record_dial_outcome(db, "unreliable-bob", succeeded=False)

    assert relays_needing_replacement(db, alice_node) == ["unreliable-bob"]


def test_relays_needing_replacement_spares_a_healthy_relay(db, alice_node):
    alice_node.relays_serving_me["reliable-carol"] = "2026-01-01T00:00:00Z"
    for _ in range(5):
        record_dial_outcome(db, "reliable-carol", succeeded=True)

    assert relays_needing_replacement(db, alice_node) == []


def test_relays_needing_replacement_spares_a_never_dialed_relay(db, alice_node):
    # Neutral prior (0.5) is well above the floor -- a relay this node
    # hasn't yet had the chance to dial again since being granted isn't
    # flagged as unreliable by default.
    alice_node.relays_serving_me["untested-dan"] = "2026-01-01T00:00:00Z"

    assert relays_needing_replacement(db, alice_node) == []


# -- issue #712: a known relay must not lose to untested candidates ----------


def test_a_known_peer_outranks_untested_candidates_at_equal_scores(db, alice_node):
    """Every never-observed candidate scores the same neutral prior. The
    candidates used to pass through a set, so which three were asked was
    down to hash order, and a working relay could lose to a peer list's worth
    of strangers indefinitely."""
    for i in range(10):
        _add_candidate(alice_node, f"stranger-{i:02d}")
    _add_peer(alice_node, "known-relay")

    assert select_relay_candidates(db, alice_node)[0] == "known-relay"


def test_candidates_that_cannot_be_reached_fall_behind_a_reached_one(db, alice_node):
    for name in ("dead-a", "dead-b", "dead-c"):
        _add_candidate(alice_node, name)
        record_dial_outcome(db, name, succeeded=False)
    _add_peer(alice_node, "seed")
    record_dial_outcome(db, "seed", succeeded=True)

    assert select_relay_candidates(db, alice_node)[0] == "seed"


def test_a_relay_consent_attempt_records_whether_the_candidate_was_reached(tmp_path, monkeypatch):
    """The relay-consent path never recorded an outcome, so the ranking above
    had nothing to rank by (issue #712)."""
    import asyncio

    import netbbs.link.sync as sync
    from netbbs.link.reliability import reliability_score
    from netbbs.link.transport import LinkTransportError
    from netbbs.storage.execution import DatabaseLane

    database = Database(tmp_path / "relay.db")
    lane = DatabaseLane(database.path)
    node = LinkNode(identity=bootstrap_node_identity("alice"))

    async def unreachable(*_args, **_kwargs):
        raise LinkTransportError("connection refused")

    async def refused_consent(*_args, **_kwargs):
        raise LinkTransportError("403 link_policy_node_probationary_read_only")

    async def reached(*_args, **_kwargs):
        return None

    try:
        monkeypatch.setattr(sync, "dial_hello", unreachable)
        assert asyncio.run(sync._request_one_relay_consent(
            node, None, "http://203.0.113.9:7862", "dead", lambda: None, lane,
        )) is False
        # Reached, then refused by the relay's policy: up, just unwilling.
        monkeypatch.setattr(sync, "dial_hello", reached)
        monkeypatch.setattr(sync, "request_relay_consent", refused_consent)
        assert asyncio.run(sync._request_one_relay_consent(
            node, None, "http://relay.example:7862", "unwilling", lambda: None, lane,
        )) is False
        assert reliability_score(database, "dead") == 0.0
        assert reliability_score(database, "unwilling") == 1.0
    finally:
        lane.close()
        database.close()
