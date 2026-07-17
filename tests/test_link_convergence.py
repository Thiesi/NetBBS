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

**Round 134 extends this file's existing `key_transition` coverage to
`board_genesis`/`board_post`/`board_post_edit`** (design doc rounds
124/129, implemented rounds 125/130) — named as "still open" in every
worklog round since 129: those three event types had unit/protocol
coverage (`tests/test_link_protocol.py`) but had never been run through
this module's multi-node fault-injection harness the way `key_transition`
has, so they hadn't actually cleared issue #59's harness gate yet. No
production code changes this round — `LinkNode.handle_events` and
`netbbs.link.store` already handled all of this correctly (traced, not
assumed); this closes a test-coverage gap, not a design or implementation
one. One real asymmetry worth noting, discovered while writing the
partition/heal scenario below: a `key_transition` rotation rides along in
a peer's *hello* (its `transitions` bundle is resent on every hello, round
89), so a healed partition converges automatically the moment two nodes
say hello again. `board_genesis`/`board_post`/`board_post_edit` carry no
such bundle — a hello only ever carries key-lifecycle state — so healing
a partition for linked-board state requires an explicit resend of the
board events themselves, not just a fresh hello. The partition/heal test
below makes that resend explicit rather than leaving it implied.

**This file also extends coverage to `link_message`/`link_message_
accepted`/`link_message_bounced`** (design doc round 93, wired up in the
same round as this extension). Unlike board events, a link_message has
no natural "N-node convergence" or "reordered chain" shape (design doc
round 93: exactly one intended recipient, no per-object chain to
extend) -- the scenarios below are reshaped to fit what this event
family actually does rather than mechanically forcing the same five
categories: a full point-to-point round trip (message out, accepted
back) with an uninvolved third node confirmed to never learn anything
about it (the meaningful equivalent of "partition" for something that
was never broadcast in the first place), duplicate delivery of both the
message and its acknowledgement, and restart-mid-sequence (proving
`load_link_node`'s existing generic `link_events` restoration is
already sufficient here, with no round-127-shaped gap to fix).
"""

from __future__ import annotations

import json

import pytest

from netbbs.link.events import (
    build_board_genesis,
    build_board_post,
    build_board_post_edit,
    build_link_message,
    build_link_message_accepted,
)
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
    round 122 already established for key_transition, applied here to
    board_post_edit's own single-linear-chain shape (design doc round
    129)."""
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
    """Combines round 120's real persistence with harness-level fault
    injection, the same shape as the key_transition restart test above,
    applied to board_genesis/board_post/board_post_edit: bob accepts
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
    key_transition partition test above already confirms (no relay,
    round 116), now pinned down for `node.boards`/`node.events` state
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
    broadcast the way a board_post is (design doc round 93: exactly one
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
    # (same as board_genesis/board_post local origination, round 128).
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
    `link_events` restoration (round 126's own finding: "already
    persists any accepted event generically... no type-specific code of
    its own") is already sufficient for `link_message`/`link_message_
    accepted` -- no round-127-shaped restart gap to fix here."""
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
