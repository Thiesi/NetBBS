"""
Smoke tests for the deterministic Link-protocol test harness (design doc
round 92, issue #59's minimal-harness gate).

These tests only prove the harness scaffolding itself (node spawning,
fake clock, scripted transport) behaves correctly and deterministically
in isolation -- see `tests/test_link_protocol.py` (round 116) for real
protocol code (`netbbs.link.protocol`) actually plugged into this same
harness.
"""

from __future__ import annotations

import pytest

from netbbs.identity.keys import verify_signature
from tests.link_harness import FakeClock, ScriptedTransport, spawn_node


def test_spawn_node_creates_isolated_identity_and_database(tmp_path):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")

    assert alice.fingerprint != bob.fingerprint
    assert alice.db.path != bob.db.path
    assert alice.db.path.exists()

    alice.close()
    bob.close()


def test_fake_clock_only_advances_and_never_goes_backward():
    clock = FakeClock()
    start = clock.now()

    clock.advance(hours=1)
    after_one_hour = clock.now()

    assert after_one_hour > start
    assert (after_one_hour - start).total_seconds() == 3600

    with pytest.raises(ValueError):
        clock.advance(seconds=-1)


def test_fake_clock_start_point_is_fixed_not_real_wall_clock():
    # Two independently constructed clocks with no start_iso override must
    # agree exactly -- proving neither reads real wall-clock time.
    clock_a = FakeClock()
    clock_b = FakeClock()
    assert clock_a.now() == clock_b.now()


def test_scripted_transport_delivers_only_when_told_to(tmp_path):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    transport = ScriptedTransport()
    transport.register(alice)
    transport.register(bob)

    transport.send(alice, bob, b"hello")
    assert transport.pending() != []
    assert transport.inbox(bob) == []  # not delivered yet -- fully test-controlled

    transport.deliver_all()
    assert transport.pending() == []
    [message] = transport.inbox(bob)
    assert message.payload == b"hello"
    assert message.sender == "alice"

    # Signature verifies against the sender's real signing operational
    # key -- the harness signs with a real Ed25519 key, not a stub.
    assert verify_signature(alice.identity.signing_key.verify_key, message.payload, message.signature)

    alice.close()
    bob.close()


def test_scripted_transport_delivery_order_is_explicitly_controlled(tmp_path):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    transport = ScriptedTransport()
    transport.register(alice)
    transport.register(bob)

    transport.send(alice, bob, b"first")
    transport.send(alice, bob, b"second")

    # Deliver out of send order -- proving ordering is never implicit.
    second = transport.deliver(1)
    first = transport.deliver(0)

    assert second.payload == b"second"
    assert first.payload == b"first"
    assert [message.payload for message in transport.inbox(bob)] == [b"second", b"first"]

    alice.close()
    bob.close()


def test_three_isolated_nodes_can_exchange_signed_messages(tmp_path):
    # Acceptance-criteria-adjacent: issue #59 asks for at least 3-5
    # independent node identities/databases -- confirms the harness
    # supports that trivially, even though full multi-node convergence
    # assertions are a later gate.
    nodes = [spawn_node(tmp_path, name) for name in ("alice", "bob", "carol")]
    transport = ScriptedTransport()
    for node in nodes:
        transport.register(node)

    alice, bob, carol = nodes
    transport.send(alice, bob, b"to bob")
    transport.send(alice, carol, b"to carol")
    transport.deliver_all()

    assert transport.inbox(bob)[0].payload == b"to bob"
    assert transport.inbox(carol)[0].payload == b"to carol"
    assert len({node.fingerprint for node in nodes}) == 3  # all distinct identities

    for node in nodes:
        node.close()
