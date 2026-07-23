"""
Multi-node convergence and fault-injection tests for `netbbs.link`,
closing issue #59's harness gate: "expanded to cover at least 3 nodes,
duplicate/reordered delivery, restart, partition, and convergence...
before the first end-to-end Linked feature is treated as complete".
Driven entirely through `tests/link_harness.py`'s `ScriptedTransport`,
same as `tests/test_link_protocol.py` — no real socket, deterministic
delivery order under full test control.

**Scope boundary, deliberate**: still no flood-fill gossip to a genuine
stranger ("no relay from a stranger") — a node only ever accepts content
whose author/origin it has *independently* exchanged a hello with at some
point, direct delivery or relayed. Most of "convergence" below still means
N nodes that each sync *directly* with every other one reach consistent
state despite duplication, reordering, and restarts. The partition/heal
scenario below exists specifically to pin that boundary down as a tested
fact for the case where the origin is a genuine stranger to the third node
-- not a documented gap anymore for the *other* case: design doc §8.8
(issue #85) adds bounded inventory/pull-based catch-up that *is* genuinely
multi-hop when the origin is already independently known to the receiving
node (a separate deterministic test below, alongside the original
never-relay-a-stranger boundary test, proves both halves of this same
line).

**This file's `key_transition` coverage extends to
`board_genesis`/`board_post`/`board_post_edit`** (design doc §9.1/9.2):
those three event types had unit/protocol coverage (`tests/
test_link_protocol.py`) but had never been run through this module's
multi-node fault-injection harness the way `key_transition` has, so
they hadn't actually cleared issue #59's harness gate yet.
`LinkNode.handle_events` and `netbbs.link.store` already handle all of
this correctly (traced, not assumed); this closes a test-coverage gap,
not a design or implementation one. One real asymmetry worth noting,
discovered while writing the partition/heal scenario below: a
`key_transition` rotation rides along in a peer's *hello* (its
`transitions` bundle is resent on every hello), so a healed partition
converges automatically the moment two nodes say hello again.
`board_genesis`/`board_post`/`board_post_edit` carry no such bundle — a
hello only ever carries key-lifecycle state — so healing a partition
for linked-board state requires an explicit resend of the board events
themselves, not just a fresh hello. The partition/heal test below makes
that resend explicit rather than leaving it implied.

**This file also extends coverage to `link_message`/`link_message_
accepted`/`link_message_bounced`** (design doc §10). Unlike board
events, a link_message has no natural "N-node convergence" or
"reordered chain" shape (exactly one intended recipient, no per-object
chain to extend) -- the scenarios below are reshaped to fit what this
event family actually does rather than mechanically forcing the same
five categories: a full point-to-point round trip (message out,
accepted back) with an uninvolved third node confirmed to never learn
anything about it (the meaningful equivalent of "partition" for
something that was never broadcast in the first place), duplicate
delivery of both the message and its acknowledgement, and
restart-mid-sequence (proving `load_link_node`'s existing generic
`link_events` restoration is already sufficient here).
"""

from __future__ import annotations

import json

import pytest

from netbbs.link.boards import materialize_carried_board, materialize_carried_post
from netbbs.link.events import (
    build_board_genesis,
    build_board_origin_transfer_accepted,
    build_board_origin_transfer_offer,
    build_board_post,
    build_board_post_edit,
    build_link_message,
    build_link_message_accepted,
)
from netbbs.link.node_identity import resolve_current_operational_key, rotate_operational_key
from netbbs.link.protocol import HelloMessage, LinkNode, LinkProtocolError
from netbbs.link.store import board_event_diff, load_link_node, purge_expired_key_transitions, save_event, save_peer
from tests.link_harness import FakeClock, ScriptedTransport, spawn_node


def _hello_bytes(node: LinkNode, *, clock: FakeClock, outgoing_only: bool = True) -> HelloMessage:
    return node.build_hello(addresses=None, outgoing_only=outgoing_only, created_at=clock.now_iso())


def _exchange_hellos(transport: ScriptedTransport, a, a_node: LinkNode, b, b_node: LinkNode, clock: FakeClock) -> None:
    """Mutual hello between two harness nodes, delivered and applied
    immediately -- the harness-level equivalent of a real dial_hello's
    single round trip, expressed as two directed messages since
    ScriptedTransport has no built-in request/response shape."""
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
    # sends of identical bytes, not one message replayed
    # (ScriptedTransport needs no new primitive for this).
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
    resend of *both together, in order* -- the actual "push everything
    every pass" design -- converges correctly. This is the
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

    # Recovery: a full resend of everything, in order (the real sync
    # behavior), converges -- revoke is now a no-op, authorize is
    # newly accepted.
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
    architectural boundary (no relay) as a tested fact: B learning
    something about A must never let C learn it too. Healing
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
    """Combines real persistence with harness-level fault injection:
    bob accepts alice's first rotation, "restarts" (a fresh LinkNode
    hydrated from the same on-disk database, not the original
    in-memory object), then a *second* rotation arrives duplicated and
    reordered -- the restarted node must still converge correctly.
    Uses netbbs.link.store's plain sync functions directly (no
    DatabaseLane/asyncio needed here -- this harness is in-process and
    synchronous, matching netbbs.link.transport's own persistence calls
    without the real-socket machinery around them)."""
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")  # HarnessNode.db is a real Database
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


def test_a_purged_key_transition_self_heals_correctly_after_a_restart(tmp_path, clock):
    """Design doc §8.9, issue #86: the actual proof that purging
    `key_transition` rows is safe, not merely reasoned about --
    `known_event_ids` genuinely forgets the purged content_id after a
    restart (nothing here fakes that), yet a resend of the exact same,
    already-integrated transition must still be recognized as a safe
    no-op via `sender.transitions` (persisted separately in `link_peers.
    transitions_json`, untouched by purging `link_events`), not
    re-verified from scratch or rejected as unknown."""
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    transport = ScriptedTransport()
    transport.register(alice)
    transport.register(bob)

    alice_node = LinkNode(identity=alice.identity)
    bob_node = LinkNode(identity=bob.identity)
    _exchange_hellos(transport, alice, alice_node, bob, bob_node, clock)
    save_peer(bob.db, bob_node.peers[alice.fingerprint])

    rotation = rotate_operational_key(alice.identity, purpose="signing")
    alice.identity = rotation
    alice_node.identity = rotation
    revoke, authorize = rotation.transitions[-2:]
    accepted = bob_node.handle_events(alice.fingerprint, [revoke.to_dict(), authorize.to_dict()])
    assert accepted == [revoke.content_id, authorize.content_id]
    for transition in (revoke, authorize):
        save_event(
            bob.db, sender_fingerprint=alice.fingerprint, content_id=transition.content_id,
            object_type="key_transition", envelope=transition.to_dict(),
        )
    save_peer(bob.db, bob_node.peers[alice.fingerprint])

    # A real purge, not a simulated cache-clear -- backdate both rows
    # past the retention window, then run the actual purge function.
    for transition in (revoke, authorize):
        bob.db.connection.execute(
            "UPDATE link_events SET received_at = '2020-01-01T00:00:00Z' WHERE content_id = ?",
            (transition.content_id,),
        )
    bob.db.connection.commit()
    deleted = purge_expired_key_transitions(bob.db, now_iso=clock.now_iso())
    assert deleted == 2

    # -- restart: a fresh LinkNode hydrated from the now-purged database --
    restarted_bob_node = load_link_node(bob.db, bob.identity)
    assert revoke.content_id not in restarted_bob_node.known_event_ids
    assert authorize.content_id not in restarted_bob_node.known_event_ids

    # A legitimate resend (design doc §8.6's own "push everything every
    # pass" model) must still self-heal, not error or re-verify from
    # scratch as if these were brand-new.
    resent = restarted_bob_node.handle_events(alice.fingerprint, [revoke.to_dict(), authorize.to_dict()])
    assert resent == []  # self-healed via sender.transitions, not re-accepted as new

    expected_key = resolve_current_operational_key(
        rotation.transitions, root_verify_key=rotation.root.verify_key,
        subject_fingerprint=alice.fingerprint, purpose="signing",
    )
    assert _resolved_signing_key(restarted_bob_node, alice.fingerprint) == expected_key

    alice.close()
    bob.close()


# -- linked-board events: 3-node convergence -------------------------------


def _board_genesis(node, clock, *, board_id="existing-local-board-id"):
    return build_board_genesis(
        signing_identity=node.identity.signing_key,
        origin_fingerprint=node.fingerprint,
        board_id=board_id,
        name="Vintage Computing",
        created_at=clock.now_iso(),
    )


def _board_post(node, clock, *, board_id="existing-local-board-id"):
    return build_board_post(
        signing_identity=node.identity.signing_key,
        home_node_fingerprint=node.fingerprint,
        local_user_id="wanderer",
        board_id=board_id,
        subject="hello",
        body="first post",
        created_at=clock.now_iso(),
    )


def test_three_nodes_converge_on_a_linked_board_post_and_edit_via_direct_pairwise_sync(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    carol = spawn_node(tmp_path, "carol")
    transport = ScriptedTransport()
    for node in (alice, bob, carol):
        transport.register(node)

    alice_node = LinkNode(identity=alice.identity)
    bob_node = LinkNode(identity=bob.identity)
    carol_node = LinkNode(identity=carol.identity)

    # Every pair says hello directly -- no relay exists, matching the
    # key_transition convergence test above.
    _exchange_hellos(transport, alice, alice_node, bob, bob_node, clock)
    _exchange_hellos(transport, alice, alice_node, carol, carol_node, clock)
    _exchange_hellos(transport, bob, bob_node, carol, carol_node, clock)

    genesis = _board_genesis(alice, clock)
    post = _board_post(alice, clock)
    edit = build_board_post_edit(
        signing_identity=alice.identity.signing_key,
        author=post.payload["author"],
        board_id="existing-local-board-id",
        root_post_id=post.content_id,
        previous_event_id=post.content_id,
        subject="hello (edited)",
        body="first post, edited",
        created_at=clock.now_iso(),
    )
    payload = json.dumps([genesis.to_dict(), post.to_dict(), edit.to_dict()]).encode()

    transport.send(alice, bob, payload)
    transport.send(alice, carol, payload)
    transport.deliver_all()

    [to_bob] = [m for m in transport.inbox(bob) if m.sender == alice.label and m.payload == payload]
    bob_node.handle_events(alice.fingerprint, json.loads(to_bob.payload))
    [to_carol] = [m for m in transport.inbox(carol) if m.sender == alice.label and m.payload == payload]
    carol_node.handle_events(alice.fingerprint, json.loads(to_carol.payload))

    for node in (bob_node, carol_node):
        assert node.boards["existing-local-board-id"].content_id == genesis.content_id
        assert node.events[post.content_id] == post.to_dict()
        assert node.post_edits[post.content_id][-1].content_id == edit.content_id

    alice.close()
    bob.close()
    carol.close()


# -- linked-board events: duplicate delivery -------------------------------


def test_duplicate_delivery_of_a_board_post_edit_is_a_pure_no_op(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    transport = ScriptedTransport()
    transport.register(alice)
    transport.register(bob)

    alice_node = LinkNode(identity=alice.identity)
    bob_node = LinkNode(identity=bob.identity)
    _exchange_hellos(transport, alice, alice_node, bob, bob_node, clock)

    genesis = _board_genesis(alice, clock)
    post = _board_post(alice, clock)
    setup_payload = json.dumps([genesis.to_dict(), post.to_dict()]).encode()
    transport.send(alice, bob, setup_payload)
    transport.deliver_all()
    [setup_msg] = [m for m in transport.inbox(bob) if m.payload == setup_payload]
    bob_node.handle_events(alice.fingerprint, json.loads(setup_msg.payload))

    edit = build_board_post_edit(
        signing_identity=alice.identity.signing_key,
        author=post.payload["author"],
        board_id="existing-local-board-id",
        root_post_id=post.content_id,
        previous_event_id=post.content_id,
        subject="hello (edited)",
        body="first post, edited",
        created_at=clock.now_iso(),
    )
    edit_payload = json.dumps([edit.to_dict()]).encode()

    # The network delivers the same edit message twice -- two independent
    # sends of identical bytes, matching the key_transition duplicate-
    # delivery scenario above.
    transport.send(alice, bob, edit_payload)
    transport.send(alice, bob, edit_payload)
    transport.deliver_all()

    first, second = [m for m in transport.inbox(bob) if m.payload == edit_payload]
    accepted_first = bob_node.handle_events(alice.fingerprint, json.loads(first.payload))
    accepted_second = bob_node.handle_events(alice.fingerprint, json.loads(second.payload))

    assert accepted_first == [edit.content_id]
    assert accepted_second == []  # pure no-op, not an error, not re-applied

    alice.close()
    bob.close()


# -- linked-board events: reordered delivery --------------------------------


def test_reordered_board_post_edit_chain_is_rejected_then_converges_on_a_full_resend(tmp_path, clock):
    """Two chained edits sent as *separate* messages, delivered out of
    order: the second-in-chain one arrives first and must be safely
    rejected (its previous_event_id points at an edit bob doesn't have
    yet), not silently misapplied. A later resend of *both together, in
    order* converges correctly -- the same push-and-retry recovery model
    already established for key_transition, applied here to
    board_post_edit's own single-linear-chain shape (design doc §9.2)."""
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    transport = ScriptedTransport()
    transport.register(alice)
    transport.register(bob)

    alice_node = LinkNode(identity=alice.identity)
    bob_node = LinkNode(identity=bob.identity)
    _exchange_hellos(transport, alice, alice_node, bob, bob_node, clock)

    genesis = _board_genesis(alice, clock)
    post = _board_post(alice, clock)
    setup_payload = json.dumps([genesis.to_dict(), post.to_dict()]).encode()
    transport.send(alice, bob, setup_payload)
    transport.deliver_all()
    [setup_msg] = [m for m in transport.inbox(bob) if m.payload == setup_payload]
    bob_node.handle_events(alice.fingerprint, json.loads(setup_msg.payload))

    first_edit = build_board_post_edit(
        signing_identity=alice.identity.signing_key,
        author=post.payload["author"],
        board_id="existing-local-board-id",
        root_post_id=post.content_id,
        previous_event_id=post.content_id,
        subject="hello (edited)",
        body="first post, edited",
        created_at=clock.now_iso(),
    )
    second_edit = build_board_post_edit(
        signing_identity=alice.identity.signing_key,
        author=post.payload["author"],
        board_id="existing-local-board-id",
        root_post_id=post.content_id,
        previous_event_id=first_edit.content_id,
        subject="hello (edited again)",
        body="first post, edited again",
        created_at=clock.now_iso(),
    )

    transport.send(alice, bob, json.dumps([first_edit.to_dict()]).encode())
    transport.send(alice, bob, json.dumps([second_edit.to_dict()]).encode())
    # Deliver out of send order: second (index 1) before first (index 0).
    second_message = transport.deliver(1)
    first_message = transport.deliver(0)

    with pytest.raises(LinkProtocolError):
        bob_node.handle_events(alice.fingerprint, json.loads(second_message.payload))

    # The out-of-order rejection didn't corrupt anything -- first (correctly
    # ordered relative to what bob already has) still applies.
    accepted = bob_node.handle_events(alice.fingerprint, json.loads(first_message.payload))
    assert accepted == [first_edit.content_id]

    # Recovery: a full resend of both, in order -- first is now a no-op
    # (already integrated), second is newly accepted.
    full_chain = [first_edit.to_dict(), second_edit.to_dict()]
    accepted = bob_node.handle_events(alice.fingerprint, full_chain)
    assert accepted == [second_edit.content_id]

    assert [e.content_id for e in bob_node.post_edits[post.content_id]] == [
        first_edit.content_id,
        second_edit.content_id,
    ]

    alice.close()
    bob.close()


# -- linked-board events: restart mid-sequence ------------------------------


def test_a_restarted_node_continues_converging_on_linked_board_state_after_reordered_and_duplicate_delivery(
    tmp_path, clock
):
    """Combines real persistence with harness-level fault injection,
    the same shape as the key_transition restart test above, applied
    to board_genesis/board_post/board_post_edit: bob accepts
    alice's genesis and first post, "restarts" (a fresh LinkNode hydrated
    from the same on-disk database), then a chained edit arrives
    duplicated and reordered -- the restarted node must still converge
    correctly."""
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    transport = ScriptedTransport()
    transport.register(alice)
    transport.register(bob)

    alice_node = LinkNode(identity=alice.identity)
    bob_node = LinkNode(identity=bob.identity)
    _exchange_hellos(transport, alice, alice_node, bob, bob_node, clock)
    save_peer(bob.db, bob_node.peers[alice.fingerprint])

    genesis = _board_genesis(alice, clock)
    post = _board_post(alice, clock)
    accepted = bob_node.handle_events(alice.fingerprint, [genesis.to_dict(), post.to_dict()])
    assert accepted == [genesis.content_id, post.content_id]
    save_event(
        bob.db, sender_fingerprint=alice.fingerprint, content_id=genesis.content_id,
        object_type="board_genesis", envelope=genesis.to_dict(),
    )
    save_event(
        bob.db, sender_fingerprint=alice.fingerprint, content_id=post.content_id,
        object_type="board_post", envelope=post.to_dict(),
    )
    save_peer(bob.db, bob_node.peers[alice.fingerprint])

    # -- restart: a fresh LinkNode, not the one bob_node object above --
    restarted_bob_node = load_link_node(bob.db, bob.identity)
    assert restarted_bob_node is not bob_node
    assert restarted_bob_node.boards["existing-local-board-id"].content_id == genesis.content_id

    # -- a chained edit, delivered duplicated *and* reordered, after the
    # restart --
    first_edit = build_board_post_edit(
        signing_identity=alice.identity.signing_key,
        author=post.payload["author"],
        board_id="existing-local-board-id",
        root_post_id=post.content_id,
        previous_event_id=post.content_id,
        subject="hello (edited)",
        body="first post, edited",
        created_at=clock.now_iso(),
    )
    second_edit = build_board_post_edit(
        signing_identity=alice.identity.signing_key,
        author=post.payload["author"],
        board_id="existing-local-board-id",
        root_post_id=post.content_id,
        previous_event_id=first_edit.content_id,
        subject="hello (edited again)",
        body="first post, edited again",
        created_at=clock.now_iso(),
    )

    transport.send(alice, bob, json.dumps([second_edit.to_dict()]).encode())  # sent first, arrives out of order
    transport.send(alice, bob, json.dumps([first_edit.to_dict()]).encode())
    transport.send(alice, bob, json.dumps([first_edit.to_dict()]).encode())  # duplicate

    second_msg = transport.deliver(0)
    first_msg_a = transport.deliver(0)
    first_msg_b = transport.deliver(0)

    with pytest.raises(LinkProtocolError):
        restarted_bob_node.handle_events(alice.fingerprint, json.loads(second_msg.payload))

    accepted_a = restarted_bob_node.handle_events(alice.fingerprint, json.loads(first_msg_a.payload))
    assert accepted_a == [first_edit.content_id]
    accepted_b = restarted_bob_node.handle_events(alice.fingerprint, json.loads(first_msg_b.payload))
    assert accepted_b == []  # duplicate, pure no-op

    full_resend = restarted_bob_node.handle_events(
        alice.fingerprint, [first_edit.to_dict(), second_edit.to_dict()]
    )
    assert full_resend == [second_edit.content_id]

    assert [e.content_id for e in restarted_bob_node.post_edits[post.content_id]] == [
        first_edit.content_id,
        second_edit.content_id,
    ]

    alice.close()
    bob.close()


# -- linked-board events: partition, then heal ------------------------------


def test_a_partitioned_node_never_learns_linked_board_state_and_converges_only_after_a_direct_resend(
    tmp_path, clock
):
    """A and C never exchange a message directly during the "partition"
    phase, even though both talk fine to B -- the same real boundary the
    key_transition partition test above already confirms (no relay),
    now pinned down for `node.boards`/`node.events` state
    too, not just `node.peers`. Unlike a key_transition rotation (which
    rides along in every hello's transitions bundle, per this module's
    docstring), board_genesis/board_post carry no such bundle -- healing
    requires an explicit resend of the board events themselves, not just
    a fresh hello."""
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

    genesis = _board_genesis(a, clock)
    post = _board_post(a, clock)
    payload = json.dumps([genesis.to_dict(), post.to_dict()]).encode()

    transport.send(a, b, payload)
    transport.deliver_all()
    [to_b] = [m for m in transport.inbox(b) if m.sender == a.label and m.payload == payload]
    accepted = b_node.handle_events(a.fingerprint, json.loads(to_b.payload))
    assert accepted == [genesis.content_id, post.content_id]  # b, talking directly to a, converges fine

    # c never heard from a at all, despite being fully synced with b --
    # b does not relay a's board state onward. This is the real boundary.
    assert "existing-local-board-id" not in c_node.boards
    assert a.fingerprint not in c_node.peers

    # -- heal: a and c finally say hello directly --
    _exchange_hellos(transport, a, a_node, c, c_node, clock)
    assert a.fingerprint in c_node.peers
    # ...but the hello alone carries no board state (unlike key_transition
    # -- see this test's own docstring) -- c still knows nothing about the
    # board until a explicitly resends its events.
    assert "existing-local-board-id" not in c_node.boards

    transport.send(a, c, payload)
    transport.deliver_all()
    [to_c] = [m for m in transport.inbox(c) if m.sender == a.label and m.payload == payload]
    accepted = c_node.handle_events(a.fingerprint, json.loads(to_c.payload))
    assert accepted == [genesis.content_id, post.content_id]
    assert c_node.boards["existing-local-board-id"].content_id == genesis.content_id

    a.close()
    b.close()
    c.close()


def test_a_node_converges_via_multi_hop_inventory_when_the_origin_is_already_known(tmp_path, clock):
    """The other half of the module docstring's boundary line, and the
    deterministic-harness counterpart to `tests/test_link_end_to_end.py`'s
    real-transport proof: b carries a's board (direct sync) and stays
    caught up; c has independently said hello to a at some point (so it
    can verify a's signing key) but never receives a's board content
    directly at all -- only b's own inventory response, relayed via b,
    not a. This is exactly the boundary the test above pins down for the
    *opposite* case (c a genuine stranger to a): here c already knows a,
    so relay through b now succeeds instead of being refused."""
    a = spawn_node(tmp_path, "a")
    b = spawn_node(tmp_path, "b")
    c = spawn_node(tmp_path, "c")
    transport = ScriptedTransport()
    for node in (a, b, c):
        transport.register(node)

    a_node = LinkNode(identity=a.identity)
    b_node = LinkNode(identity=b.identity)
    c_node = LinkNode(identity=c.identity)

    # a and c independently say hello -- c can now verify a's signing
    # key, but this alone carries no board state (same as the healed
    # partition test above).
    _exchange_hellos(transport, a, a_node, c, c_node, clock)
    # a and b sync directly: b receives and materializes a's board.
    _exchange_hellos(transport, a, a_node, b, b_node, clock)

    genesis = _board_genesis(a, clock)
    post = _board_post(a, clock)
    payload = json.dumps([genesis.to_dict(), post.to_dict()]).encode()
    transport.send(a, b, payload)
    transport.deliver_all()
    [to_b] = [m for m in transport.inbox(b) if m.sender == a.label and m.payload == payload]
    accepted = b_node.handle_events(a.fingerprint, json.loads(to_b.payload))
    assert accepted == [genesis.content_id, post.content_id]
    materialize_carried_board(b.db, genesis, own_fingerprint=b.fingerprint)
    materialize_carried_post(b.db, post, sender_fingerprint=a.fingerprint)

    # c has never talked to a about the board at all, and has no
    # relationship with b yet either.
    assert "existing-local-board-id" not in c_node.boards

    # c and b say hello, then c "requests inventory" -- b's own diff
    # query (real DB, same function a real /inventory route calls)
    # against what it actually carries, not a's own events.
    _exchange_hellos(transport, b, b_node, c, c_node, clock)
    events, more_available = board_event_diff(b.db, {"existing-local-board-id": []}, limit=200)
    assert more_available is False
    assert len(events) == 2  # genesis + post, nothing pre-known

    # Applied exactly as a real inventory response would be: fed through
    # handle_events keyed by b (the relay), not a (the true origin).
    relayed_accepted = c_node.handle_events(b.fingerprint, events)
    assert set(relayed_accepted) == {genesis.content_id, post.content_id}
    assert c_node.boards["existing-local-board-id"].content_id == genesis.content_id

    a.close()
    b.close()
    c.close()


# -- board_origin_transfer_offer/accepted (design doc §9.4/issue #53) -----


def test_a_bystander_node_correctly_witnesses_a_full_origin_transfer(tmp_path, clock):
    """The scenario `record_board_origin_change`'s own docstring exists
    for: bob is a direct party to neither the offer nor the acceptance
    (alice hands the board to carol), but he *does* directly know both
    of them, and receives both events during ordinary sync -- his own
    view of "who currently owns this board" must end up correct anyway,
    not just alice's and carol's."""
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    carol = spawn_node(tmp_path, "carol")
    transport = ScriptedTransport()
    for node in (alice, bob, carol):
        transport.register(node)

    alice_node = LinkNode(identity=alice.identity)
    bob_node = LinkNode(identity=bob.identity)
    carol_node = LinkNode(identity=carol.identity)
    _exchange_hellos(transport, alice, alice_node, bob, bob_node, clock)
    _exchange_hellos(transport, alice, alice_node, carol, carol_node, clock)
    _exchange_hellos(transport, bob, bob_node, carol, carol_node, clock)

    genesis = _board_genesis(alice, clock)
    # Self-originated, same as the offer below -- alice's own node
    # records her own genesis directly (`_link_board_screen` does this
    # in production too), never via her own handle_events.
    alice_node.boards["existing-local-board-id"] = genesis
    bob_node.handle_events(alice.fingerprint, [genesis.to_dict()])
    carol_node.handle_events(alice.fingerprint, [genesis.to_dict()])

    offer = build_board_origin_transfer_offer(
        signing_identity=alice.identity.signing_key,
        board_id="existing-local-board-id",
        previous_event_id=genesis.content_id,
        old_origin_fingerprint=alice.fingerprint,
        new_origin_fingerprint=carol.fingerprint,
        created_at=clock.now_iso(),
    )
    # alice's own node records her own outstanding offer directly (the
    # same thing `_transfer_board_origin_screen`/`offer_board_origin_
    # transfer` do in production -- self-originated events never pass
    # through alice's own handle_events, same as a self-originated
    # genesis never does), then pushes the offer to both bob and carol,
    # same as an ordinary sync pass would (design doc: an offer alone
    # changes nothing yet).
    alice_node.pending_origin_transfers["existing-local-board-id"] = offer
    bob_node.handle_events(alice.fingerprint, [offer.to_dict()])
    carol_node.handle_events(alice.fingerprint, [offer.to_dict()])
    assert bob_node.current_board_origin("existing-local-board-id") == alice.fingerprint

    accepted_event = build_board_origin_transfer_accepted(
        signing_identity=carol.identity.signing_key,
        board_id="existing-local-board-id",
        previous_event_id=offer.content_id,
        new_origin_fingerprint=carol.fingerprint,
        created_at=clock.now_iso(),
    )
    # carol's own node records her own new-origin status directly too
    # (the same thing `accept_board_origin_transfer`/`record_board_
    # origin_change` do in production -- her own acceptance never
    # passes through her own handle_events either), then pushes it out
    # -- bob, an uninvolved bystander to the handoff itself, still
    # receives it directly from carol during an ordinary sync pass.
    carol_node.board_origin["existing-local-board-id"] = carol.fingerprint
    bob_node.handle_events(carol.fingerprint, [accepted_event.to_dict()])
    alice_node.handle_events(carol.fingerprint, [accepted_event.to_dict()])

    for node in (alice_node, bob_node, carol_node):
        assert node.current_board_origin("existing-local-board-id") == carol.fingerprint

    alice.close()
    bob.close()
    carol.close()


# -- link_message: full round trip, an uninvolved third node isolated -------


def _link_message(sender, recipient, clock):
    return build_link_message(
        signing_identity=sender.identity.signing_key,
        home_node_fingerprint=sender.fingerprint,
        local_user_id="wanderer",
        recipient_home_node_fingerprint=recipient.fingerprint,
        recipient_local_user_id="recipient-user",
        confidentiality_tier="tier1_home_node_key",
        ciphertext=b"opaque sealed bytes",
        created_at=clock.now_iso(),
    )


def test_link_message_full_round_trip_and_an_uninvolved_third_node_never_learns_anything(tmp_path, clock):
    """alice sends a link_message to bob; bob accepts it and sends back
    a link_message_accepted; alice accepts that. carol -- a fully
    hello-connected third node -- never receives either event and never
    learns anything about the exchange, since neither event is
    broadcast the way a board_post is (design doc §10: exactly one
    intended recipient each way). The meaningful equivalent of
    "partition" for something that was never multi-node gossip in the
    first place."""
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    carol = spawn_node(tmp_path, "carol")
    transport = ScriptedTransport()
    for node in (alice, bob, carol):
        transport.register(node)

    alice_node = LinkNode(identity=alice.identity)
    bob_node = LinkNode(identity=bob.identity)
    carol_node = LinkNode(identity=carol.identity)

    _exchange_hellos(transport, alice, alice_node, bob, bob_node, clock)
    _exchange_hellos(transport, alice, alice_node, carol, carol_node, clock)
    _exchange_hellos(transport, bob, bob_node, carol, carol_node, clock)

    message = _link_message(alice, bob, clock)
    message_payload = json.dumps([message.to_dict()]).encode()
    transport.send(alice, bob, message_payload)
    transport.deliver_all()
    [to_bob] = [m for m in transport.inbox(bob) if m.sender == alice.label and m.payload == message_payload]
    accepted = bob_node.handle_events(alice.fingerprint, json.loads(to_bob.payload))
    assert accepted == [message.content_id]

    # alice must already know about her own message to accept an ack
    # about it -- self-origination never passes through handle_events
    # (same as board_genesis/board_post local origination).
    alice_node.known_event_ids.add(message.content_id)
    alice_node.events[message.content_id] = message.to_dict()

    ack = build_link_message_accepted(
        signing_identity=bob.identity.signing_key,
        recipient_node_fingerprint=bob.fingerprint,
        message_content_id=message.content_id,
        created_at=clock.now_iso(),
    )
    ack_payload = json.dumps([ack.to_dict()]).encode()
    transport.send(bob, alice, ack_payload)
    transport.deliver_all()
    [to_alice] = [m for m in transport.inbox(alice) if m.sender == bob.label and m.payload == ack_payload]
    accepted = alice_node.handle_events(bob.fingerprint, json.loads(to_alice.payload))
    assert accepted == [ack.content_id]

    # carol was never sent either event -- confirm she genuinely has
    # nothing, not just that nothing has reached her inbox yet.
    assert carol_node.known_event_ids == set()
    assert carol_node.events == {}

    alice.close()
    bob.close()
    carol.close()


# -- link_message: duplicate delivery ----------------------------------------


def test_duplicate_delivery_of_a_link_message_is_a_pure_no_op(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    transport = ScriptedTransport()
    transport.register(alice)
    transport.register(bob)

    alice_node = LinkNode(identity=alice.identity)
    bob_node = LinkNode(identity=bob.identity)
    _exchange_hellos(transport, alice, alice_node, bob, bob_node, clock)

    message = _link_message(alice, bob, clock)
    payload = json.dumps([message.to_dict()]).encode()
    transport.send(alice, bob, payload)
    transport.send(alice, bob, payload)
    transport.deliver_all()

    first, second = [m for m in transport.inbox(bob) if m.sender == alice.label and m.payload == payload]
    accepted_first = bob_node.handle_events(alice.fingerprint, json.loads(first.payload))
    accepted_second = bob_node.handle_events(alice.fingerprint, json.loads(second.payload))

    assert accepted_first == [message.content_id]
    assert accepted_second == []  # pure no-op, not an error, not re-applied

    alice.close()
    bob.close()


def test_duplicate_delivery_of_a_link_message_accepted_is_a_pure_no_op(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    transport = ScriptedTransport()
    transport.register(alice)
    transport.register(bob)

    alice_node = LinkNode(identity=alice.identity)
    bob_node = LinkNode(identity=bob.identity)
    _exchange_hellos(transport, alice, alice_node, bob, bob_node, clock)

    message = _link_message(alice, bob, clock)
    alice_node.known_event_ids.add(message.content_id)
    alice_node.events[message.content_id] = message.to_dict()

    ack = build_link_message_accepted(
        signing_identity=bob.identity.signing_key,
        recipient_node_fingerprint=bob.fingerprint,
        message_content_id=message.content_id,
        created_at=clock.now_iso(),
    )
    payload = json.dumps([ack.to_dict()]).encode()
    transport.send(bob, alice, payload)
    transport.send(bob, alice, payload)
    transport.deliver_all()

    first, second = [m for m in transport.inbox(alice) if m.sender == bob.label and m.payload == payload]
    accepted_first = alice_node.handle_events(bob.fingerprint, json.loads(first.payload))
    accepted_second = alice_node.handle_events(bob.fingerprint, json.loads(second.payload))

    assert accepted_first == [ack.content_id]
    assert accepted_second == []  # pure no-op, not an error, not re-applied

    alice.close()
    bob.close()


# -- link_message: restart mid-sequence --------------------------------------


def test_a_restarted_node_still_correctly_processes_a_link_message_and_its_acknowledgement(tmp_path, clock):
    """Proves `netbbs.link.store.load_link_node`'s existing generic
    `link_events` restoration ("already persists any accepted event
    generically... no type-specific code of its own") is already
    sufficient for `link_message`/`link_message_accepted` -- no
    restart gap to fix here."""
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    transport = ScriptedTransport()
    transport.register(alice)
    transport.register(bob)

    alice_node = LinkNode(identity=alice.identity)
    bob_node = LinkNode(identity=bob.identity)
    _exchange_hellos(transport, alice, alice_node, bob, bob_node, clock)
    save_peer(bob.db, bob_node.peers[alice.fingerprint])

    message = _link_message(alice, bob, clock)
    accepted = bob_node.handle_events(alice.fingerprint, [message.to_dict()])
    assert accepted == [message.content_id]
    save_event(
        bob.db, sender_fingerprint=alice.fingerprint, content_id=message.content_id,
        object_type="link_message", envelope=message.to_dict(),
    )
    save_peer(bob.db, bob_node.peers[alice.fingerprint])

    # -- restart: a fresh LinkNode, not the one bob_node object above --
    restarted_bob_node = load_link_node(bob.db, bob.identity)
    assert restarted_bob_node is not bob_node
    assert message.content_id in restarted_bob_node.known_event_ids

    # A duplicate resend after the restart is still correctly a no-op.
    duplicate = restarted_bob_node.handle_events(alice.fingerprint, [message.to_dict()])
    assert duplicate == []

    # bob's own acknowledgement, built and applied against the restarted
    # node, round-trips back to alice correctly.
    ack = build_link_message_accepted(
        signing_identity=bob.identity.signing_key,
        recipient_node_fingerprint=bob.fingerprint,
        message_content_id=message.content_id,
        created_at=clock.now_iso(),
    )
    alice_node.known_event_ids.add(message.content_id)
    alice_node.events[message.content_id] = message.to_dict()
    accepted = alice_node.handle_events(bob.fingerprint, [ack.to_dict()])
    assert accepted == [ack.content_id]

    alice.close()
    bob.close()
