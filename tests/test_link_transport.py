"""
Integration tests for `netbbs.link.transport` (design doc §11, round
117; round 120 adds persistence) — these spin up real `LinkServer`
instances on OS-assigned loopback ports and connect real `aiohttp`
clients to them, exercising actual HTTP+JSON traffic end to end, the
same "real server, real client, real socket" convention `tests/
test_web.py` already established for `WebServer`. `tests/
test_link_protocol.py` already proves the underlying protocol logic
against a fully synthetic transport (`ScriptedTransport`) — these
tests prove the same logic survives an actual wire (and, since round
120, an actual database).

Round 120's persistence assertions read back through a second,
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

from netbbs.link.events import KeyTransition, build_endpoint_descriptor
from netbbs.link.node_identity import bootstrap_node_identity, rotate_operational_key
from netbbs.link.protocol import HelloMessage, LinkNode, LinkProtocolError
from netbbs.link.store import load_link_node
from netbbs.link.transport import LinkServer, LinkTransportError, dial_hello, push_events
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


def _hello_for(node: LinkNode, *, created_at: str = "2026-01-01T00:00:00+00:00") -> HelloMessage:
    return node.build_hello(addresses=None, outgoing_only=True, created_at=created_at)


async def _run_server(node: LinkNode, own_hello_provider, lane: DatabaseLane) -> LinkServer:
    server = LinkServer(host="127.0.0.1", port=0, node=node, own_hello_provider=own_hello_provider, lane=lane)
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

        # Round 120: both sides persisted the peer they just learned about.
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


# -- events: real HTTP gossip push ------------------------------------------


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

        # Round 120: both accepted events, and bob's updated peer record
        # (its extended transitions chain), landed in bob's database.
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
    on file (round 116's own scope) -- surfaces as a 400, wrapped as
    LinkTransportError here, same shape as any other server-side
    rejection."""
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
    """The actual round-120 proof, distinct from the inline DB-row
    checks above: a *second*, freshly-constructed `LinkNode` -- not the
    original in-memory object bob's server used -- hydrated from the
    same database file via `load_link_node`, has bob's peer and event
    state intact. This is what a real node restart looks like."""
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
