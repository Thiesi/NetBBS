"""
Integration tests for `netbbs.link.transport` (design doc §11) — these
spin up real `LinkServer` instances on OS-assigned loopback ports and
connect real `aiohttp` clients to them, exercising actual HTTP+JSON
traffic end to end, the same "real server, real client, real socket"
convention `tests/test_web.py` already established for `WebServer`.
`tests/test_link_protocol.py` already proves the underlying protocol
logic against a fully synthetic transport (`ScriptedTransport`) —
these tests prove the same logic survives an actual wire (and an
actual database).

The persistence assertions read back through a second,
separately-opened `Database` against the same file the test's own
`DatabaseLane` writes through — the same "one connection for the
lane's worker thread, one for the test's own assertions" split `tests/
test_admin_flow.py`'s `db`/`lane` fixtures already use, since a
`sqlite3.Connection` is bound to whichever thread created it
(`netbbs.storage.execution.DatabaseLane`'s own docstring) and the
lane's connection only ever runs on its dedicated worker thread.
"""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest

from netbbs.link.events import KeyTransition, build_board_genesis, build_endpoint_descriptor, build_link_message
from netbbs.link.node_identity import bootstrap_node_identity, rotate_operational_key
from netbbs.link.protocol import HelloMessage, LinkNode, LinkProtocolError
from netbbs.link.store import load_link_node
from netbbs.link.transport import (
    LINK_PATH_PREFIX,
    LinkServer,
    LinkTransportError,
    deposit_into_relay_mailbox,
    dial_hello,
    pickup_from_relay_mailbox,
    push_events,
    request_peer_list,
    request_relay_consent,
)
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


def _hello_for(node: LinkNode, *, created_at: str = "2026-01-01T00:00:00+00:00") -> HelloMessage:
    return node.build_hello(addresses=None, outgoing_only=True, created_at=created_at)


async def _run_server(
    node: LinkNode,
    own_hello_provider,
    lane: DatabaseLane,
    *,
    relay_serving_enabled: bool = True,
    max_relay_clients: int = 20,
    max_carried_boards: int | None = 500,
    max_peers: int | None = 1000,
    throttle=None,
) -> LinkServer:
    server = LinkServer(
        host="127.0.0.1", port=0, node=node, own_hello_provider=own_hello_provider, lane=lane,
        relay_serving_enabled=relay_serving_enabled, max_relay_clients=max_relay_clients,
        max_carried_boards=max_carried_boards, max_peers=max_peers, throttle=throttle,
    )
    await server.start()
    return server


class _NodeDb:
    """One node's paired `Database` (for the test's own assertions) and
    `DatabaseLane` (for the code under test to dispatch through) against
    the same file -- see this module's docstring for why both are
    needed rather than just one."""

    def __init__(self, tmp_path, name: str) -> None:
        self.db = Database(tmp_path / f"{name}.db")
        self.lane = DatabaseLane(self.db.path)

    def close(self) -> None:
        self.lane.close()
        self.db.close()


# -- hello: real HTTP round trip -------------------------------------------


def test_dial_hello_completes_a_real_http_handshake(tmp_path):
    alice_identity = bootstrap_node_identity("alice")
    bob_identity = bootstrap_node_identity("bob")
    alice_node = LinkNode(identity=alice_identity)
    bob_node = LinkNode(identity=bob_identity)
    alice = _NodeDb(tmp_path, "alice")
    bob = _NodeDb(tmp_path, "bob")

    async def scenario():
        bob_server = await _run_server(bob_node, lambda: _hello_for(bob_node), bob.lane)
        try:
            async with aiohttp.ClientSession() as session:
                return await dial_hello(
                    alice_node, session, f"http://127.0.0.1:{bob_server.port}", _hello_for(alice_node), alice.lane
                )
        finally:
            await bob_server.stop()

    try:
        record = asyncio.run(scenario())
        assert record.fingerprint == bob_identity.fingerprint
        # A real, single POST/response round trip mutually introduced both
        # sides -- alice's dial handed her hello to bob's server (which
        # recorded it), and bob's own hello came back in the response,
        # which dial_hello fed into alice's own handle_hello.
        assert bob_identity.fingerprint in alice_node.peers
        assert alice_identity.fingerprint in bob_node.peers

        # Both sides persisted the peer they just learned about.
        alice_row = alice.db.connection.execute(
            "SELECT fingerprint FROM link_peers WHERE fingerprint = ?", (bob_identity.fingerprint,)
        ).fetchone()
        bob_row = bob.db.connection.execute(
            "SELECT fingerprint FROM link_peers WHERE fingerprint = ?", (alice_identity.fingerprint,)
        ).fetchone()
        assert alice_row is not None
        assert bob_row is not None
    finally:
        alice.close()
        bob.close()


def test_dial_hello_raises_link_protocol_error_for_a_forged_returned_hello(tmp_path):
    """If the *peer's own* returned hello fails verification, that's a
    LinkProtocolError from node.handle_hello, not a transport error --
    propagated unwrapped, same exception every other caller already
    handles."""
    alice_identity = bootstrap_node_identity("alice")
    bob_identity = bootstrap_node_identity("bob")
    mallory_identity = bootstrap_node_identity("mallory")
    alice_node = LinkNode(identity=alice_identity)
    bob_node = LinkNode(identity=bob_identity)
    alice = _NodeDb(tmp_path, "alice")
    bob = _NodeDb(tmp_path, "bob")

    forged_descriptor = build_endpoint_descriptor(
        signing_identity=mallory_identity.signing_key,
        subject_fingerprint=bob_identity.fingerprint,
        addresses=None,
        outgoing_only=True,
        created_at="2026-01-01T00:00:00+00:00",
    )

    def _forged_bob_hello() -> HelloMessage:
        real = _hello_for(bob_node)
        return HelloMessage(
            root_public_key=real.root_public_key, transitions=real.transitions, descriptor=forged_descriptor
        )

    async def scenario():
        bob_server = await _run_server(bob_node, _forged_bob_hello, bob.lane)
        try:
            async with aiohttp.ClientSession() as session:
                await dial_hello(
                    alice_node, session, f"http://127.0.0.1:{bob_server.port}", _hello_for(alice_node), alice.lane
                )
        finally:
            await bob_server.stop()

    try:
        with pytest.raises(LinkProtocolError):
            asyncio.run(scenario())
    finally:
        alice.close()
        bob.close()


def test_dial_hello_raises_link_transport_error_when_the_server_rejects_it(tmp_path):
    """A client hello the server's handle_hello refuses (here: a
    descriptor claiming the wrong subject) surfaces as the server's own
    HTTP 400 -- dial_hello wraps that as LinkTransportError, not
    LinkProtocolError, since nothing local re-ran the verification that
    actually failed."""
    alice_identity = bootstrap_node_identity("alice")
    bob_identity = bootstrap_node_identity("bob")
    alice_node = LinkNode(identity=alice_identity)
    bob_node = LinkNode(identity=bob_identity)
    alice = _NodeDb(tmp_path, "alice")
    bob = _NodeDb(tmp_path, "bob")

    mismatched_descriptor = build_endpoint_descriptor(
        signing_identity=alice_identity.signing_key,
        subject_fingerprint="some-other-fingerprint",
        addresses=None,
        outgoing_only=True,
        created_at="2026-01-01T00:00:00+00:00",
    )
    real_alice_hello = _hello_for(alice_node)
    forged_alice_hello = HelloMessage(
        root_public_key=real_alice_hello.root_public_key,
        transitions=real_alice_hello.transitions,
        descriptor=mismatched_descriptor,
    )

    async def scenario():
        bob_server = await _run_server(bob_node, lambda: _hello_for(bob_node), bob.lane)
        try:
            async with aiohttp.ClientSession() as session:
                await dial_hello(
                    alice_node, session, f"http://127.0.0.1:{bob_server.port}", forged_alice_hello, alice.lane
                )
        finally:
            await bob_server.stop()

    try:
        with pytest.raises(LinkTransportError):
            asyncio.run(scenario())
    finally:
        alice.close()
        bob.close()


def test_dial_hello_raises_link_transport_error_when_nothing_is_listening(tmp_path):
    alice_node = LinkNode(identity=bootstrap_node_identity("alice"))
    alice = _NodeDb(tmp_path, "alice")

    async def scenario():
        async with aiohttp.ClientSession() as session:
            await dial_hello(alice_node, session, "http://127.0.0.1:1", _hello_for(alice_node), alice.lane)

    try:
        with pytest.raises(LinkTransportError):
            asyncio.run(scenario())
    finally:
        alice.close()


# -- issue #11: duplicate JSON keys are rejected at the wire boundary -------


def test_server_rejects_a_hello_body_with_a_duplicate_json_key(tmp_path):
    """Design doc §7.2/issue #11: a wire JSON object containing the same
    key twice must be rejected outright, not silently resolved to
    "last one wins" -- see `netbbs.link.events.strict_json_loads`'s own
    docstring for why. Sends genuinely malformed raw bytes no `HelloMessage`
    could ever produce, so this has to bypass `dial_hello`/`.to_dict()`
    and POST by hand."""
    bob_node = LinkNode(identity=bootstrap_node_identity("bob"))
    bob = _NodeDb(tmp_path, "bob")

    raw_body = '{"root_public_key": "aaaa", "root_public_key": "bbbb"}'

    async def scenario():
        bob_server = await _run_server(bob_node, lambda: _hello_for(bob_node), bob.lane)
        try:
            url = f"http://127.0.0.1:{bob_server.port}{LINK_PATH_PREFIX}/hello"
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, data=raw_body.encode("utf-8"), headers={"Content-Type": "application/json"}
                ) as response:
                    return response.status, await response.json()
        finally:
            await bob_server.stop()

    try:
        status, body = asyncio.run(scenario())
        assert status == 400
        assert "duplicate key" in body["error"]
    finally:
        bob.close()


def test_server_rejects_an_events_body_with_a_duplicate_json_key_in_a_nested_object(tmp_path):
    """Same rule as the hello test above, applied to `/events`, whose
    body is a *list* of envelopes -- confirms `object_pairs_hook`
    catches a duplicate inside a nested object, not just at the
    top level."""
    bob_node = LinkNode(identity=bootstrap_node_identity("bob"))
    bob = _NodeDb(tmp_path, "bob")

    raw_body = '[{"envelope": {"object_type": "key_transition", "object_type": "board_post"}}]'

    async def scenario():
        bob_server = await _run_server(bob_node, lambda: _hello_for(bob_node), bob.lane)
        try:
            url = f"http://127.0.0.1:{bob_server.port}{LINK_PATH_PREFIX}/events/{'a' * 8}"
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, data=raw_body.encode("utf-8"), headers={"Content-Type": "application/json"}
                ) as response:
                    return response.status, await response.json()
        finally:
            await bob_server.stop()

    try:
        status, body = asyncio.run(scenario())
        assert status == 400
        assert "duplicate key" in body["error"]
    finally:
        bob.close()


# -- events: real HTTP gossip push ------------------------------------------


def test_push_events_succeeds_on_the_very_first_pass_after_a_hello(tmp_path):
    """Regression test: this is the *minimal* case that exposed the
    bug -- no rotation involved at all. Every node's hello already
    carries its "signing"-purpose transitions, and push_events sends
    *every* transition of both purposes moments later, in the same
    sync pass -- meaning the very first push after the very first
    hello, for any node, always resends at least one transition the
    hello already delivered. Without the chain-membership check,
    `known_event_ids` didn't have it yet (handle_hello never touches
    that set), so it fell through to a duplicate append and got
    rejected as a forged fork, aborting the *entire* push (push_events
    had a 100% failure rate on every real sync pass, silently
    swallowed by _sync_one_seed's own catch-and-log). Only the
    genuinely-new transport-purpose transition (never in the hello)
    should be reported as newly accepted."""
    alice_identity = bootstrap_node_identity("alice")
    bob_identity = bootstrap_node_identity("bob")
    alice_node = LinkNode(identity=alice_identity)
    bob_node = LinkNode(identity=bob_identity)
    alice = _NodeDb(tmp_path, "alice")
    bob = _NodeDb(tmp_path, "bob")

    async def scenario():
        bob_server = await _run_server(bob_node, lambda: _hello_for(bob_node), bob.lane)
        try:
            async with aiohttp.ClientSession() as session:
                await dial_hello(
                    alice_node, session, f"http://127.0.0.1:{bob_server.port}", _hello_for(alice_node), alice.lane
                )
                # No rotation -- alice_node.identity is exactly what
                # bootstrap_node_identity produced: one signing
                # transition (already in the hello bob just accepted)
                # and one transport transition (never sent yet).
                return await push_events(
                    alice_node, session, f"http://127.0.0.1:{bob_server.port}", list(alice_identity.transitions)
                )
        finally:
            await bob_server.stop()

    try:
        accepted = asyncio.run(scenario())
        signing_transition, transport_transition = alice_identity.transitions
        assert accepted == [transport_transition.content_id]  # the signing one was already known, a no-op
        peer_content_ids = {t.content_id for t in bob_node.peers[alice_identity.fingerprint].transitions}
        assert signing_transition.content_id in peer_content_ids
        assert transport_transition.content_id in peer_content_ids
    finally:
        alice.close()
        bob.close()


def test_push_events_gossips_a_real_key_rotation_over_http(tmp_path):
    alice_identity = bootstrap_node_identity("alice")
    bob_identity = bootstrap_node_identity("bob")
    alice_node = LinkNode(identity=alice_identity)
    bob_node = LinkNode(identity=bob_identity)
    alice = _NodeDb(tmp_path, "alice")
    bob = _NodeDb(tmp_path, "bob")

    async def scenario():
        bob_server = await _run_server(bob_node, lambda: _hello_for(bob_node), bob.lane)
        try:
            async with aiohttp.ClientSession() as session:
                await dial_hello(
                    alice_node, session, f"http://127.0.0.1:{bob_server.port}", _hello_for(alice_node), alice.lane
                )

                rotated = rotate_operational_key(alice_identity, purpose="signing")
                alice_node.identity = rotated
                revoke, authorize = rotated.transitions[-2:]

                return await push_events(
                    alice_node, session, f"http://127.0.0.1:{bob_server.port}", [revoke, authorize]
                )
        finally:
            await bob_server.stop()

    try:
        accepted = asyncio.run(scenario())
        revoke_id = alice_node.identity.transitions[-2].content_id
        authorize_id = alice_node.identity.transitions[-1].content_id
        assert accepted == [revoke_id, authorize_id]
        assert bob_node.peers[alice_identity.fingerprint].transitions[-1].content_id == authorize_id

        # Both accepted events, and bob's updated peer record (its
        # extended transitions chain), landed in bob's database.
        rows = bob.db.connection.execute("SELECT content_id FROM link_events").fetchall()
        assert {row["content_id"] for row in rows} == {revoke_id, authorize_id}
        peer_row = bob.db.connection.execute(
            "SELECT transitions_json FROM link_peers WHERE fingerprint = ?", (alice_identity.fingerprint,)
        ).fetchone()
        # content_id is a computed hash, never stored literally -- parse
        # and reconstruct rather than substring-matching for it.
        stored_transitions = [KeyTransition.from_dict(t) for t in json.loads(peer_row["transitions_json"])]
        assert stored_transitions[-1].content_id == authorize_id
    finally:
        alice.close()
        bob.close()


def test_push_events_raises_link_transport_error_for_a_stranger(tmp_path):
    """bob's server refuses events from a peer with no completed hello
    on file -- surfaces as a 400, wrapped as LinkTransportError here,
    same shape as any other server-side rejection."""
    alice_identity = bootstrap_node_identity("alice")
    bob_node = LinkNode(identity=bootstrap_node_identity("bob"))
    alice_node = LinkNode(identity=alice_identity)
    bob = _NodeDb(tmp_path, "bob")

    async def scenario():
        bob_server = await _run_server(bob_node, lambda: _hello_for(bob_node), bob.lane)
        try:
            async with aiohttp.ClientSession() as session:
                rotated = rotate_operational_key(alice_identity, purpose="signing")
                alice_node.identity = rotated
                await push_events(
                    alice_node, session, f"http://127.0.0.1:{bob_server.port}", list(rotated.transitions[-2:])
                )
        finally:
            await bob_server.stop()

    try:
        with pytest.raises(LinkTransportError):
            asyncio.run(scenario())
    finally:
        bob.close()


# -- persistence survives a restart ------------------------------------


def test_a_restarted_node_recovers_its_peer_and_events_from_disk(tmp_path):
    """Proof, distinct from the inline DB-row checks above: a *second*,
    freshly-constructed `LinkNode` -- not the original in-memory object
    bob's server used -- hydrated from the same database file via
    `load_link_node`, has bob's peer and event state intact. This is
    what a real node restart looks like."""
    alice_identity = bootstrap_node_identity("alice")
    bob_identity = bootstrap_node_identity("bob")
    alice_node = LinkNode(identity=alice_identity)
    bob_node = LinkNode(identity=bob_identity)
    alice = _NodeDb(tmp_path, "alice")
    bob = _NodeDb(tmp_path, "bob")

    async def scenario():
        bob_server = await _run_server(bob_node, lambda: _hello_for(bob_node), bob.lane)
        try:
            async with aiohttp.ClientSession() as session:
                await dial_hello(
                    alice_node, session, f"http://127.0.0.1:{bob_server.port}", _hello_for(alice_node), alice.lane
                )
                rotated = rotate_operational_key(alice_identity, purpose="signing")
                alice_node.identity = rotated
                revoke, authorize = rotated.transitions[-2:]
                await push_events(
                    alice_node, session, f"http://127.0.0.1:{bob_server.port}", [revoke, authorize]
                )
        finally:
            await bob_server.stop()

    try:
        asyncio.run(scenario())

        restarted_bob = load_link_node(bob.db, bob_identity)
        assert restarted_bob is not bob_node
        assert alice_identity.fingerprint in restarted_bob.peers
        restarted_peer = restarted_bob.peers[alice_identity.fingerprint]
        assert restarted_peer.transitions[-1].content_id == alice_node.identity.transitions[-1].content_id

        revoke_id = alice_node.identity.transitions[-2].content_id
        authorize_id = alice_node.identity.transitions[-1].content_id
        assert restarted_bob.known_event_ids >= {revoke_id, authorize_id}
        assert authorize_id in restarted_bob.events
    finally:
        alice.close()
        bob.close()


def test_link_server_port_raises_before_start(tmp_path):
    alice = _NodeDb(tmp_path, "alice")
    try:
        server = LinkServer(
            host="127.0.0.1", port=0, node=LinkNode(identity=bootstrap_node_identity("alice")),
            own_hello_provider=lambda: None,
            lane=alice.lane,
        )
        with pytest.raises(RuntimeError):
            _ = server.port
    finally:
        alice.close()


def test_request_peer_list_records_a_real_peers_candidates_over_http(tmp_path):
    """Peer-list exchange over a real socket: bob already knows carol
    (a completed hello); alice requests bob's peer list and records
    carol as an unverified candidate."""
    alice_identity = bootstrap_node_identity("alice")
    bob_identity = bootstrap_node_identity("bob")
    carol_identity = bootstrap_node_identity("carol")
    alice_node = LinkNode(identity=alice_identity)
    bob_node = LinkNode(identity=bob_identity)
    alice = _NodeDb(tmp_path, "alice")
    bob = _NodeDb(tmp_path, "bob")

    carol_hello = _hello_for(LinkNode(identity=carol_identity))
    bob_node.handle_hello(carol_hello)

    async def scenario():
        bob_server = await _run_server(bob_node, lambda: _hello_for(bob_node), bob.lane)
        try:
            async with aiohttp.ClientSession() as session:
                await dial_hello(
                    alice_node, session, f"http://127.0.0.1:{bob_server.port}", _hello_for(alice_node), alice.lane
                )
                return await request_peer_list(
                    alice_node, session, f"http://127.0.0.1:{bob_server.port}", bob_identity.fingerprint, alice.lane
                )
        finally:
            await bob_server.stop()

    try:
        recorded = asyncio.run(scenario())
        assert recorded == [carol_identity.fingerprint]
        assert carol_identity.fingerprint in alice_node.candidate_descriptors
        assert carol_identity.fingerprint not in alice_node.peers  # not promoted, just a candidate

        row = alice.db.connection.execute(
            "SELECT fingerprint FROM link_peer_candidates"
        ).fetchone()
        assert row["fingerprint"] == carol_identity.fingerprint
    finally:
        alice.close()
        bob.close()


# -- relay consent: a real synchronous request/response round trip --------


def test_request_relay_consent_completes_a_real_http_round_trip(tmp_path):
    """Issue #58's relay-consent exchange over a real socket: alice
    (outgoing-only) asks bob to relay for her, and gets a signed
    accept back in the *same* HTTP response -- the shape that has to
    work for an outgoing-only requester who can never be dialed back."""
    alice_identity = bootstrap_node_identity("alice")
    bob_identity = bootstrap_node_identity("bob")
    alice_node = LinkNode(identity=alice_identity)
    bob_node = LinkNode(identity=bob_identity)
    alice = _NodeDb(tmp_path, "alice")
    bob = _NodeDb(tmp_path, "bob")

    async def scenario():
        bob_server = await _run_server(bob_node, lambda: _hello_for(bob_node), bob.lane)
        try:
            async with aiohttp.ClientSession() as session:
                await dial_hello(
                    alice_node, session, f"http://127.0.0.1:{bob_server.port}", _hello_for(alice_node), alice.lane
                )
                return await request_relay_consent(
                    alice_node, session, f"http://127.0.0.1:{bob_server.port}", bob_identity.fingerprint, alice.lane
                )
        finally:
            await bob_server.stop()

    try:
        response = asyncio.run(scenario())
        assert response.payload["accepted"] is True

        # Both sides applied the grant to their own in-memory bookkeeping...
        assert alice_node.relays_serving_me == {bob_identity.fingerprint: response.payload["created_at"]}
        assert bob_node.relaying_for == {alice_identity.fingerprint: response.payload["created_at"]}
        # ...and the requester's own outstanding-request bookkeeping was
        # cleared once the synchronous reply came back.
        assert alice_node.pending_own_relay_requests == {}

        # Both sides persisted the grant (issue #58).
        alice_row = alice.db.connection.execute(
            "SELECT fingerprint, role, accepted_at FROM link_relay_consents"
        ).fetchone()
        bob_row = bob.db.connection.execute(
            "SELECT fingerprint, role, accepted_at FROM link_relay_consents"
        ).fetchone()
        assert alice_row["fingerprint"] == bob_identity.fingerprint
        assert alice_row["role"] == "relay_for_me"
        assert bob_row["fingerprint"] == alice_identity.fingerprint
        assert bob_row["role"] == "i_relay_for"
    finally:
        alice.close()
        bob.close()


def test_request_relay_consent_declines_once_the_relay_is_at_capacity(tmp_path):
    alice_identity = bootstrap_node_identity("alice")
    bob_identity = bootstrap_node_identity("bob")
    alice_node = LinkNode(identity=alice_identity)
    bob_node = LinkNode(identity=bob_identity)
    alice = _NodeDb(tmp_path, "alice")
    bob = _NodeDb(tmp_path, "bob")

    async def scenario():
        bob_server = await _run_server(bob_node, lambda: _hello_for(bob_node), bob.lane, max_relay_clients=0)
        try:
            async with aiohttp.ClientSession() as session:
                await dial_hello(
                    alice_node, session, f"http://127.0.0.1:{bob_server.port}", _hello_for(alice_node), alice.lane
                )
                return await request_relay_consent(
                    alice_node, session, f"http://127.0.0.1:{bob_server.port}", bob_identity.fingerprint, alice.lane
                )
        finally:
            await bob_server.stop()

    try:
        response = asyncio.run(scenario())
        assert response.payload["accepted"] is False
        # A decline never mutates either side's granted-relay bookkeeping,
        # and is never persisted (netbbs.link.store.save_relay_consent's
        # own "only ever persist a completed grant" scope).
        assert alice_node.relays_serving_me == {}
        assert bob_node.relaying_for == {}
        assert alice.db.connection.execute("SELECT * FROM link_relay_consents").fetchone() is None
        assert bob.db.connection.execute("SELECT * FROM link_relay_consents").fetchone() is None
    finally:
        alice.close()
        bob.close()


def test_request_relay_consent_declines_when_relay_serving_is_opted_out(tmp_path):
    """Issue #58: an operator's `relay_serving_enabled=False`
    (`netbbs.net.nodeconfig.LinkConfig`'s own opt-out) always declines,
    even with plenty of capacity to spare -- separate knob from the
    resource cap tested just above."""
    alice_identity = bootstrap_node_identity("alice")
    bob_identity = bootstrap_node_identity("bob")
    alice_node = LinkNode(identity=alice_identity)
    bob_node = LinkNode(identity=bob_identity)
    alice = _NodeDb(tmp_path, "alice")
    bob = _NodeDb(tmp_path, "bob")

    async def scenario():
        bob_server = await _run_server(
            bob_node, lambda: _hello_for(bob_node), bob.lane, relay_serving_enabled=False, max_relay_clients=20
        )
        try:
            async with aiohttp.ClientSession() as session:
                await dial_hello(
                    alice_node, session, f"http://127.0.0.1:{bob_server.port}", _hello_for(alice_node), alice.lane
                )
                return await request_relay_consent(
                    alice_node, session, f"http://127.0.0.1:{bob_server.port}", bob_identity.fingerprint, alice.lane
                )
        finally:
            await bob_server.stop()

    try:
        response = asyncio.run(scenario())
        assert response.payload["accepted"] is False
        assert alice_node.relays_serving_me == {}
        assert bob_node.relaying_for == {}
    finally:
        alice.close()
        bob.close()


def test_request_relay_consent_raises_link_protocol_error_for_a_forged_response(tmp_path):
    """A malicious/buggy relay answering with a response signed by the
    wrong key must not be accepted -- same "verify what came back"
    discipline `test_dial_hello_raises_link_protocol_error_for_a_forged_
    returned_hello` already proves for hello."""
    from aiohttp import web

    from netbbs.link.events import build_relay_consent_response

    alice_identity = bootstrap_node_identity("alice")
    bob_identity = bootstrap_node_identity("bob")
    mallory_identity = bootstrap_node_identity("mallory")
    alice_node = LinkNode(identity=alice_identity)
    alice = _NodeDb(tmp_path, "alice")

    async def _forged_relay_consent(request: web.Request) -> web.Response:
        body = await request.json()
        from netbbs.link.events import RelayConsentRequest

        real_request = RelayConsentRequest.from_dict(body)
        forged = build_relay_consent_response(
            signing_identity=mallory_identity.signing_key,  # wrong key
            request_content_id=real_request.content_id,
            relay_fingerprint=bob_identity.fingerprint,
            requester_fingerprint=alice_identity.fingerprint,
            accepted=True,
            created_at="2026-01-01T00:00:01+00:00",
        )
        return web.json_response(forged.to_dict())

    async def _bob_hello(request: web.Request) -> web.Response:
        return web.json_response(_hello_for(LinkNode(identity=bob_identity)).to_dict())

    async def scenario():
        app = web.Application()
        app.router.add_post("/link/v1/hello", _bob_hello)
        app.router.add_post("/link/v1/relay-consent/{fingerprint}", _forged_relay_consent)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        try:
            async with aiohttp.ClientSession() as session:
                await dial_hello(
                    alice_node, session, f"http://127.0.0.1:{site.port}", _hello_for(alice_node), alice.lane
                )
                await request_relay_consent(
                    alice_node, session, f"http://127.0.0.1:{site.port}", bob_identity.fingerprint, alice.lane
                )
        finally:
            await runner.cleanup()

    try:
        with pytest.raises(LinkProtocolError):
            asyncio.run(scenario())
        # The forged response never got applied.
        assert alice_node.relays_serving_me == {}
        assert alice_node.pending_own_relay_requests == {}
    finally:
        alice.close()


# -- relay mailbox: deposit + pickup over real HTTP ---------------------------


def _link_message_for(
    sender_identity, recipient_fingerprint: str, *, created_at: str = "2026-01-01T00:00:00+00:00"
):
    return build_link_message(
        signing_identity=sender_identity.signing_key,
        home_node_fingerprint=sender_identity.fingerprint,
        local_user_id="alice-the-user",
        recipient_home_node_fingerprint=recipient_fingerprint,
        recipient_local_user_id="carol-the-user",
        confidentiality_tier="tier1_home_node_key",
        ciphertext=b"opaque-sealed-bytes",
        created_at=created_at,
    )


def test_deposit_and_pickup_relay_mailbox_round_trips_over_http(tmp_path):
    """Issue #58: alice deposits a link_message for carol at bob
    (acting as carol's relay); carol picks it up over a real
    socket. Alice needs no prior relationship with bob at all -- see
    `LinkServer._handle_relay_mailbox_deposit`'s own docstring."""
    alice_identity = bootstrap_node_identity("alice")
    bob_identity = bootstrap_node_identity("bob")
    carol_identity = bootstrap_node_identity("carol")
    bob_node = LinkNode(identity=bob_identity)
    bob = _NodeDb(tmp_path, "bob")

    # bob has already granted carol relay consent (exercised separately
    # in test_request_relay_consent_completes_a_real_http_round_trip) --
    # set up directly here to keep this test focused on mailbox behavior.
    bob_node.relaying_for[carol_identity.fingerprint] = "2026-01-01T00:00:00+00:00"

    message = _link_message_for(alice_identity, carol_identity.fingerprint)

    async def scenario():
        bob_server = await _run_server(bob_node, lambda: _hello_for(bob_node), bob.lane)
        try:
            async with aiohttp.ClientSession() as session:
                await deposit_into_relay_mailbox(
                    session, f"http://127.0.0.1:{bob_server.port}", carol_identity.fingerprint, message
                )
                carol_node = LinkNode(identity=carol_identity)
                return await pickup_from_relay_mailbox(
                    session,
                    f"http://127.0.0.1:{bob_server.port}",
                    _hello_for(carol_node),
                )
        finally:
            await bob_server.stop()

    try:
        picked_up = asyncio.run(scenario())
        assert len(picked_up) == 1
        assert picked_up[0].content_id == message.content_id

        # The deposit was persisted, then removed on pickup (issue #58).
        row = bob.db.connection.execute("SELECT * FROM link_relay_mailbox").fetchone()
        assert row is None
    finally:
        bob.close()


def test_pickup_returns_nothing_held_for_a_different_fingerprint(tmp_path):
    bob_identity = bootstrap_node_identity("bob")
    carol_identity = bootstrap_node_identity("carol")
    dan_identity = bootstrap_node_identity("dan")
    alice_identity = bootstrap_node_identity("alice")
    bob_node = LinkNode(identity=bob_identity)
    bob = _NodeDb(tmp_path, "bob")

    bob_node.relaying_for[carol_identity.fingerprint] = "2026-01-01T00:00:00+00:00"
    message = _link_message_for(alice_identity, carol_identity.fingerprint)

    async def scenario():
        bob_server = await _run_server(bob_node, lambda: _hello_for(bob_node), bob.lane)
        try:
            async with aiohttp.ClientSession() as session:
                await deposit_into_relay_mailbox(
                    session, f"http://127.0.0.1:{bob_server.port}", carol_identity.fingerprint, message
                )
                dan_node = LinkNode(identity=dan_identity)
                return await pickup_from_relay_mailbox(
                    session, f"http://127.0.0.1:{bob_server.port}", _hello_for(dan_node)
                )
        finally:
            await bob_server.stop()

    try:
        picked_up = asyncio.run(scenario())
        assert picked_up == []
    finally:
        bob.close()


def test_deposit_is_refused_when_not_relaying_for_the_recipient(tmp_path):
    bob_identity = bootstrap_node_identity("bob")
    carol_identity = bootstrap_node_identity("carol")
    alice_identity = bootstrap_node_identity("alice")
    bob_node = LinkNode(identity=bob_identity)  # bob has granted no one consent
    bob = _NodeDb(tmp_path, "bob")

    message = _link_message_for(alice_identity, carol_identity.fingerprint)

    async def scenario():
        bob_server = await _run_server(bob_node, lambda: _hello_for(bob_node), bob.lane)
        try:
            async with aiohttp.ClientSession() as session:
                await deposit_into_relay_mailbox(
                    session, f"http://127.0.0.1:{bob_server.port}", carol_identity.fingerprint, message
                )
        finally:
            await bob_server.stop()

    try:
        with pytest.raises(LinkTransportError):
            asyncio.run(scenario())
    finally:
        bob.close()


def test_deposit_is_refused_once_the_recipients_mailbox_is_full(tmp_path):
    from netbbs.link.relay_mailbox import MAX_MAILBOX_ENVELOPES_PER_RECIPIENT

    bob_identity = bootstrap_node_identity("bob")
    carol_identity = bootstrap_node_identity("carol")
    alice_identity = bootstrap_node_identity("alice")
    bob_node = LinkNode(identity=bob_identity)
    bob = _NodeDb(tmp_path, "bob")
    bob_node.relaying_for[carol_identity.fingerprint] = "2026-01-01T00:00:00+00:00"

    async def scenario():
        bob_server = await _run_server(bob_node, lambda: _hello_for(bob_node), bob.lane)
        try:
            async with aiohttp.ClientSession() as session:
                for i in range(MAX_MAILBOX_ENVELOPES_PER_RECIPIENT):
                    message = _link_message_for(
                        alice_identity, carol_identity.fingerprint, created_at=f"2026-01-01T00:{i:02d}:00+00:00"
                    )
                    await deposit_into_relay_mailbox(
                        session, f"http://127.0.0.1:{bob_server.port}", carol_identity.fingerprint, message
                    )
                # One more than the cap must be refused.
                one_too_many = _link_message_for(
                    alice_identity, carol_identity.fingerprint, created_at="2026-01-01T23:59:00+00:00"
                )
                await deposit_into_relay_mailbox(
                    session, f"http://127.0.0.1:{bob_server.port}", carol_identity.fingerprint, one_too_many
                )
        finally:
            await bob_server.stop()

    try:
        with pytest.raises(LinkTransportError):
            asyncio.run(scenario())
    finally:
        bob.close()


# -- quotas (design doc §13.9, issue #60's third operational slice) --------


def test_events_push_still_succeeds_once_the_carried_board_cap_is_reached(tmp_path):
    """A board_genesis pushed once bob is already at his own max_
    carried_boards must still be accepted as a genuine, gossipable
    event (HTTP 200, in known_event_ids, still reaches bob.boards) --
    only *local materialization* (a real, locally browsable `Board`
    row) is refused. Proves the whole point of `BoardCarryLimitError`
    being a distinct exception from every other handle_events rejection
    (see that class's own docstring): the request as a whole must not
    fail just because this node declined to carry one more board."""
    alice_identity = bootstrap_node_identity("alice")
    bob_identity = bootstrap_node_identity("bob")
    alice_node = LinkNode(identity=alice_identity)
    bob_node = LinkNode(identity=bob_identity)
    alice = _NodeDb(tmp_path, "alice")
    bob = _NodeDb(tmp_path, "bob")

    genesis = build_board_genesis(
        signing_identity=alice_identity.signing_key,
        origin_fingerprint=alice_identity.fingerprint,
        board_id="remote-board-id",
        name="Remote Discussion",
        created_at="2026-01-01T00:00:00+00:00",
    )

    async def scenario():
        bob_server = await _run_server(bob_node, lambda: _hello_for(bob_node), bob.lane, max_carried_boards=0)
        try:
            async with aiohttp.ClientSession() as session:
                await dial_hello(
                    alice_node, session, f"http://127.0.0.1:{bob_server.port}", _hello_for(alice_node), alice.lane
                )
                return await push_events(
                    alice_node, session, f"http://127.0.0.1:{bob_server.port}", [genesis]
                )
        finally:
            await bob_server.stop()

    try:
        accepted = asyncio.run(scenario())
        assert accepted == [genesis.content_id]  # not a 500, not dropped from the response
        assert genesis.content_id in bob_node.known_event_ids
        assert "remote-board-id" in bob_node.boards  # protocol-level acceptance, unaffected by the cap

        row = bob.db.connection.execute(
            "SELECT 1 FROM boards WHERE board_id = ?", ("remote-board-id",)
        ).fetchone()
        assert row is None  # but never actually materialized locally
    finally:
        alice.close()
        bob.close()


def test_handle_hello_over_http_rejects_a_new_peer_once_bobs_max_peers_cap_is_reached(tmp_path):
    alice_identity = bootstrap_node_identity("alice")
    carol_identity = bootstrap_node_identity("carol")
    bob_identity = bootstrap_node_identity("bob")
    alice_node = LinkNode(identity=alice_identity)
    carol_node = LinkNode(identity=carol_identity)
    bob_node = LinkNode(identity=bob_identity)
    alice = _NodeDb(tmp_path, "alice")
    carol = _NodeDb(tmp_path, "carol")
    bob = _NodeDb(tmp_path, "bob")

    async def scenario():
        bob_server = await _run_server(bob_node, lambda: _hello_for(bob_node), bob.lane, max_peers=1)
        try:
            async with aiohttp.ClientSession() as session:
                # Alice takes bob's one available peer slot.
                await dial_hello(
                    alice_node, session, f"http://127.0.0.1:{bob_server.port}", _hello_for(alice_node), alice.lane
                )
                # Carol, a brand new fingerprint, is refused now that bob is at his own cap.
                with pytest.raises(LinkTransportError):
                    await dial_hello(
                        carol_node, session, f"http://127.0.0.1:{bob_server.port}", _hello_for(carol_node), carol.lane
                    )
        finally:
            await bob_server.stop()

    try:
        asyncio.run(scenario())
        assert alice_identity.fingerprint in bob_node.peers
        assert carol_identity.fingerprint not in bob_node.peers
    finally:
        alice.close()
        carol.close()
        bob.close()


def test_client_max_size_rejects_an_oversized_request_body(tmp_path):
    """Design doc §13.9: turns aiohttp's implicit 1 MiB `client_max_
    size` default into a deliberate, documented value on `LinkServer`'s
    own `web.Application` -- proved here against a real oversized POST,
    not just a config-value assertion."""
    bob_node = LinkNode(identity=bootstrap_node_identity("bob"))
    bob = _NodeDb(tmp_path, "bob")

    async def scenario():
        bob_server = await _run_server(bob_node, lambda: _hello_for(bob_node), bob.lane)
        try:
            async with aiohttp.ClientSession() as session:
                oversized = b"x" * (3 * 1024 * 1024)  # bigger than _LINK_CLIENT_MAX_SIZE_BYTES (2 MiB)
                async with session.post(
                    f"http://127.0.0.1:{bob_server.port}{LINK_PATH_PREFIX}/hello", data=oversized
                ) as response:
                    return response.status
        finally:
            await bob_server.stop()

    try:
        status = asyncio.run(scenario())
        assert status == 413
    finally:
        bob.close()


def test_rate_limit_middleware_rejects_once_the_throttle_is_exhausted(tmp_path):
    from netbbs.net.throttle import LinkRequestThrottle

    bob_node = LinkNode(identity=bootstrap_node_identity("bob"))
    bob = _NodeDb(tmp_path, "bob")
    throttle = LinkRequestThrottle(capacity=1, refill_per_minute=0.0, max_tracked_sources=100)

    async def scenario():
        bob_server = await _run_server(bob_node, lambda: _hello_for(bob_node), bob.lane, throttle=throttle)
        try:
            async with aiohttp.ClientSession() as session:
                url = f"http://127.0.0.1:{bob_server.port}{LINK_PATH_PREFIX}/peers"
                async with session.get(url) as first_response:
                    first_status = first_response.status
                async with session.get(url) as second_response:
                    second_status = second_response.status
                return first_status, second_status
        finally:
            await bob_server.stop()

    try:
        first_status, second_status = asyncio.run(scenario())
        assert first_status == 200
        assert second_status == 429
    finally:
        bob.close()


def test_rate_limit_middleware_is_a_no_op_when_no_throttle_is_configured(tmp_path):
    bob_node = LinkNode(identity=bootstrap_node_identity("bob"))
    bob = _NodeDb(tmp_path, "bob")

    async def scenario():
        bob_server = await _run_server(bob_node, lambda: _hello_for(bob_node), bob.lane)  # throttle=None default
        try:
            async with aiohttp.ClientSession() as session:
                url = f"http://127.0.0.1:{bob_server.port}{LINK_PATH_PREFIX}/peers"
                statuses = []
                for _ in range(5):
                    async with session.get(url) as response:
                        statuses.append(response.status)
                return statuses
        finally:
            await bob_server.stop()

    try:
        assert asyncio.run(scenario()) == [200] * 5
    finally:
        bob.close()
