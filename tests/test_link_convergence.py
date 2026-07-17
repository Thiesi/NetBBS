"""
Multi-node convergence and fault-injection tests for `netbbs.link`
(design doc round 122, closing issue #59's harness gate: "expanded to
cover at least 3 nodes, duplicate/reordered delivery, restart, partition,
and convergence... before the first end-to-end Linked feature is treated
as complete"). Driven entirely through `tests/link_harness.py`'s
`ScriptedTransport`, same as `tests/test_link_protocol.py` — no real
socket, deterministic delivery order under full test control.

**Scope boundary, deliberate**: no relay/flood-fill gossip exists yet
(round 116's "no relay from a stranger," still open in every round's own
gap list) — a node only ever learns about a peer it has *directly*
exchanged a hello with. "Convergence" here means N nodes that each sync
*directly* with every other one reach consistent state despite
duplication, reordering, and restarts — not multi-hop propagation via an
intermediate node, which isn't built. The partition/heal scenario below
exists specifically to pin that boundary down as a tested fact, not just
a documented gap.
"""

from __future__ import annotations

import json

import pytest

from netbbs.link.node_identity import resolve_current_operational_key, rotate_operational_key
from netbbs.link.protocol import HelloMessage, LinkNode, LinkProtocolError
from netbbs.link.store import load_link_node, save_event, save_peer
from tests.link_harness import FakeClock, ScriptedTransport, spawn_node


def _hello_bytes(node: LinkNode, *, clock: FakeClock, outgoing_only: bool = True) -> HelloMessage:
    return node.build_hello(addresses=None, outgoing_only=outgoing_only, created_at=clock.now_iso())


def _exchange_hellos(transport: ScriptedTransport, a, a_node: LinkNode, b, b_node: LinkNode, clock: FakeClock) -> None:
    """Mutual hello between two harness nodes, delivered and applied
    immediately -- the harness-level equivalent of a real dial_hello's
    single round trip (round 117), expressed as two directed messages
    since ScriptedTransport has no built-in request/response shape."""
    transport.send(a, b, json.dumps(_hello_bytes(a_node, clock=clock).to_dict()).encode())
    transport.send(b, a, json.dumps(_hello_bytes(b_node, clock=clock).to_dict()).encode())
    transport.deliver_all()
    for message in transport.inbox(b):
        if message.sender == a.label:
            b_node.handle_hello(HelloMessage.from_dict(json.loads(message.payload)))
    for message in transport.inbox(a):
        if message.sender == b.label:
            a_node.handle_hello(HelloMessage.from_dict(json.loads(message.payload)))


def _resolved_signing_key(node: LinkNode, subject_fingerprint: str) -> str | None:
    peer = node.peers[subject_fingerprint]
    return resolve_current_operational_key(
        peer.transitions, root_verify_key=peer.root_verify_key, subject_fingerprint=subject_fingerprint,
        purpose="signing",
    )


@pytest.fixture
def clock():
    return FakeClock()


# -- 3-node convergence -------------------------------------------------


def test_three_nodes_converge_on_a_key_rotation_via_direct_pairwise_sync(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    carol = spawn_node(tmp_path, "carol")
    transport = ScriptedTransport()
    for node in (alice, bob, carol):
        transport.register(node)

    alice_node = LinkNode(identity=alice.identity)
    bob_node = LinkNode(identity=bob.identity)
    carol_node = LinkNode(identity=carol.identity)

    # Every pair says hello directly -- no relay exists, so convergence
    # can only ever happen this way (see module docstring).
    _exchange_hellos(transport, alice, alice_node, bob, bob_node, clock)
    _exchange_hellos(transport, alice, alice_node, carol, carol_node, clock)
    _exchange_hellos(transport, bob, bob_node, carol, carol_node, clock)

    rotated = rotate_operational_key(alice.identity, purpose="signing")
    alice.identity = rotated
    alice_node.identity = rotated
    pair = [t.to_dict() for t in rotated.transitions[-2:]]

    transport.send(alice, bob, json.dumps(pair).encode())
    transport.send(alice, carol, json.dumps(pair).encode())
    transport.deliver_all()

    [to_bob] = [m for m in transport.inbox(bob) if m.sender == alice.label and m.payload == json.dumps(pair).encode()]
    bob_node.handle_events(alice.fingerprint, json.loads(to_bob.payload))
    [to_carol] = [
        m for m in transport.inbox(carol) if m.sender == alice.label and m.payload == json.dumps(pair).encode()
    ]
    carol_node.handle_events(alice.fingerprint, json.loads(to_carol.payload))

    expected_key = resolve_current_operational_key(
        rotated.transitions, root_verify_key=rotated.root.verify_key, subject_fingerprint=alice.fingerprint,
        purpose="signing",
    )
    assert _resolved_signing_key(bob_node, alice.fingerprint) == expected_key
    assert _resolved_signing_key(carol_node, alice.fingerprint) == expected_key

    alice.close()
    bob.close()
    carol.close()


# -- duplicate delivery ---------------------------------------------------


def test_duplicate_delivery_of_the_same_event_is_a_pure_no_op(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    transport = ScriptedTransport()
    transport.register(alice)
    transport.register(bob)

    alice_node = LinkNode(identity=alice.identity)
    bob_node = LinkNode(identity=bob.identity)
    _exchange_hellos(transport, alice, alice_node, bob, bob_node, clock)

    rotated = rotate_operational_key(alice.identity, purpose="signing")
    alice.identity = rotated
    alice_node.identity = rotated
    pair = [t.to_dict() for t in rotated.transitions[-2:]]
    payload = json.dumps(pair).encode()

    # The network delivers the same message twice -- two independent
    # sends of identical bytes, not one message replayed (round 122:
    # ScriptedTransport needs no new primitive for this).
    transport.send(alice, bob, payload)
    transport.send(alice, bob, payload)
    transport.deliver_all()

    first, second = [m for m in transport.inbox(bob) if m.sender == alice.label and m.payload == payload]
    accepted_first = bob_node.handle_events(alice.fingerprint, json.loads(first.payload))
    accepted_second = bob_node.handle_events(alice.fingerprint, json.loads(second.payload))

    revoke_id, authorize_id = (t.content_id for t in rotated.transitions[-2:])
    assert accepted_first == [revoke_id, authorize_id]
    assert accepted_second == []  # pure no-op, not an error, not re-applied

    alice.close()
    bob.close()


# -- reordered delivery ---------------------------------------------------


def test_reordered_delivery_is_rejected_then_converges_on_a_full_resend(tmp_path, clock):
    """The two halves of a rotation sent as *separate* messages,
    delivered out of order: the second-in-chain one (authorize) arrives
    first and must be safely rejected (its previous_transition_id points
    at a revoke bob doesn't have yet), not silently misapplied. A later
    resend of *both together, in order* -- round 119's actual "push
    everything every pass" design -- converges correctly. This is the
    project's real recovery model: push-and-retry, not reorder-tolerant
    single-message delivery."""
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    transport = ScriptedTransport()
    transport.register(alice)
    transport.register(bob)

    alice_node = LinkNode(identity=alice.identity)
    bob_node = LinkNode(identity=bob.identity)
    _exchange_hellos(transport, alice, alice_node, bob, bob_node, clock)

    rotated = rotate_operational_key(alice.identity, purpose="signing")
    alice.identity = rotated
    alice_node.identity = rotated
    revoke_transition, authorize_transition = rotated.transitions[-2:]

    transport.send(alice, bob, json.dumps([revoke_transition.to_dict()]).encode())
    transport.send(alice, bob, json.dumps([authorize_transition.to_dict()]).encode())
    # Deliver out of send order: authorize (index 1) before revoke (index 0).
    authorize_message = transport.deliver(1)
    revoke_message = transport.deliver(0)

    with pytest.raises(LinkProtocolError):
        bob_node.handle_events(alice.fingerprint, json.loads(authorize_message.payload))

    # The out-of-order rejection didn't corrupt anything -- revoke alone
    # (correctly ordered relative to what bob already has) still applies.
    accepted = bob_node.handle_events(alice.fingerprint, json.loads(revoke_message.payload))
    assert accepted == [revoke_transition.content_id]
    assert _resolved_signing_key(bob_node, alice.fingerprint) is None  # revoked, nothing authorized yet

    # Recovery: a full resend of everything, in order (round 119's real
    # sync behavior), converges -- revoke is now a no-op (round 121),
    # authorize is newly accepted.
    full_pair = [revoke_transition.to_dict(), authorize_transition.to_dict()]
    accepted = bob_node.handle_events(alice.fingerprint, full_pair)
    assert accepted == [authorize_transition.content_id]

    expected_key = resolve_current_operational_key(
        rotated.transitions, root_verify_key=rotated.root.verify_key, subject_fingerprint=alice.fingerprint,
        purpose="signing",
    )
    assert _resolved_signing_key(bob_node, alice.fingerprint) == expected_key

    alice.close()
    bob.close()


# -- partition, then heal --------------------------------------------------


def test_a_partitioned_node_never_relays_and_converges_only_after_a_direct_hello(tmp_path, clock):
    """A and C never exchange a message directly during the "partition"
    phase, even though both talk fine to B -- confirms today's real
    architectural boundary (no relay, round 116) as a tested fact: B
    learning something about A must never let C learn it too. Healing
    the partition (A and C finally say hello directly) is the only way
    they converge."""
    a = spawn_node(tmp_path, "a")
    b = spawn_node(tmp_path, "b")
    c = spawn_node(tmp_path, "c")
    transport = ScriptedTransport()
    for node in (a, b, c):
        transport.register(node)

    a_node = LinkNode(identity=a.identity)
    b_node = LinkNode(identity=b.identity)
    c_node = LinkNode(identity=c.identity)

    # -- partitioned phase: a<->b and b<->c talk; a and c never do --
    _exchange_hellos(transport, a, a_node, b, b_node, clock)
    _exchange_hellos(transport, b, b_node, c, c_node, clock)

    rotated = rotate_operational_key(a.identity, purpose="signing")
    a.identity = rotated
    a_node.identity = rotated
    pair = [t.to_dict() for t in rotated.transitions[-2:]]
    transport.send(a, b, json.dumps(pair).encode())
    transport.deliver_all()
    [to_b] = [m for m in transport.inbox(b) if m.sender == a.label and m.payload == json.dumps(pair).encode()]
    accepted = b_node.handle_events(a.fingerprint, json.loads(to_b.payload))
    assert len(accepted) == 2  # b, talking directly to a, converges fine

    # c never heard from a at all, despite being fully synced with b --
    # b does not relay a's events onward. This is the real boundary.
    assert a.fingerprint not in c_node.peers

    # -- heal: a and c finally say hello directly --
    _exchange_hellos(transport, a, a_node, c, c_node, clock)
    assert a.fingerprint in c_node.peers
    # c only has what a's *current* hello carries (post-rotation) -- it
    # never received the individual rotation events, but converges on
    # the same resolved key via the fresh hello's own transitions bundle.
    expected_key = resolve_current_operational_key(
        rotated.transitions, root_verify_key=rotated.root.verify_key, subject_fingerprint=a.fingerprint,
        purpose="signing",
    )
    assert _resolved_signing_key(c_node, a.fingerprint) == expected_key

    a.close()
    b.close()
    c.close()


# -- restart mid-sequence ---------------------------------------------------


def test_a_restarted_node_continues_converging_after_reordered_and_duplicate_delivery(tmp_path, clock):
    """Combines round 120's real persistence with harness-level fault
    injection: bob accepts alice's first rotation, "restarts" (a fresh
    LinkNode hydrated from the same on-disk database, not the original
    in-memory object), then a *second* rotation arrives duplicated and
    reordered -- the restarted node must still converge correctly.
    Uses netbbs.link.store's plain sync functions directly (no
    DatabaseLane/asyncio needed here -- this harness is in-process and
    synchronous, matching netbbs.link.transport's own persistence calls
    without the real-socket machinery around them)."""
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")  # HarnessNode.db is a real Database (round 92)
    transport = ScriptedTransport()
    transport.register(alice)
    transport.register(bob)

    alice_node = LinkNode(identity=alice.identity)
    bob_node = LinkNode(identity=bob.identity)
    _exchange_hellos(transport, alice, alice_node, bob, bob_node, clock)
    save_peer(bob.db, bob_node.peers[alice.fingerprint])

    first_rotation = rotate_operational_key(alice.identity, purpose="signing")
    alice.identity = first_rotation
    alice_node.identity = first_rotation
    first_pair = first_rotation.transitions[-2:]
    accepted = bob_node.handle_events(alice.fingerprint, [t.to_dict() for t in first_pair])
    assert accepted == [t.content_id for t in first_pair]
    for transition in first_pair:
        save_event(
            bob.db, sender_fingerprint=alice.fingerprint, content_id=transition.content_id,
            object_type="key_transition", envelope=transition.to_dict(),
        )
    save_peer(bob.db, bob_node.peers[alice.fingerprint])

    # -- restart: a fresh LinkNode, not the one bob_node object above --
    restarted_bob_node = load_link_node(bob.db, bob.identity)
    assert restarted_bob_node is not bob_node
    assert alice.fingerprint in restarted_bob_node.peers

    # -- a second rotation, delivered duplicated *and* reordered, after
    # the restart --
    second_rotation = rotate_operational_key(alice.identity, purpose="signing")
    alice.identity = second_rotation
    alice_node.identity = second_rotation
    revoke2, authorize2 = second_rotation.transitions[-2:]

    transport.send(alice, bob, json.dumps([authorize2.to_dict()]).encode())  # sent first, arrives out of order
    transport.send(alice, bob, json.dumps([revoke2.to_dict()]).encode())
    transport.send(alice, bob, json.dumps([revoke2.to_dict()]).encode())  # duplicate

    authorize_msg = transport.deliver(0)
    revoke_msg_a = transport.deliver(0)
    revoke_msg_b = transport.deliver(0)

    with pytest.raises(LinkProtocolError):
        restarted_bob_node.handle_events(alice.fingerprint, json.loads(authorize_msg.payload))

    accepted_a = restarted_bob_node.handle_events(alice.fingerprint, json.loads(revoke_msg_a.payload))
    assert accepted_a == [revoke2.content_id]
    accepted_b = restarted_bob_node.handle_events(alice.fingerprint, json.loads(revoke_msg_b.payload))
    assert accepted_b == []  # duplicate, pure no-op

    full_resend = restarted_bob_node.handle_events(
        alice.fingerprint, [revoke2.to_dict(), authorize2.to_dict()]
    )
    assert full_resend == [authorize2.content_id]

    expected_key = resolve_current_operational_key(
        second_rotation.transitions, root_verify_key=second_rotation.root.verify_key,
        subject_fingerprint=alice.fingerprint, purpose="signing",
    )
    assert _resolved_signing_key(restarted_bob_node, alice.fingerprint) == expected_key

    alice.close()
    bob.close()
