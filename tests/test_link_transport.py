"""
Integration tests for `netbbs.link.transport` (design doc §11, round
117) — these spin up real `LinkServer` instances on OS-assigned
loopback ports and connect real `aiohttp` clients to them, exercising
actual HTTP+JSON traffic end to end, the same "real server, real
client, real socket" convention `tests/test_web.py` already established
for `WebServer`. `tests/test_link_protocol.py` already proves the
underlying protocol logic against a fully synthetic transport
(`ScriptedTransport`) — these tests prove the same logic survives an
actual wire.
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest

from netbbs.link.events import build_endpoint_descriptor
from netbbs.link.node_identity import bootstrap_node_identity, rotate_operational_key
from netbbs.link.protocol import HelloMessage, LinkNode, LinkProtocolError
from netbbs.link.transport import LinkServer, LinkTransportError, dial_hello, push_events


def _hello_for(node: LinkNode, *, created_at: str = "2026-01-01T00:00:00+00:00") -> HelloMessage:
    return node.build_hello(addresses=None, outgoing_only=True, created_at=created_at)


async def _run_server(node: LinkNode, own_hello_provider) -> LinkServer:
    server = LinkServer(host="127.0.0.1", port=0, node=node, own_hello_provider=own_hello_provider)
    await server.start()
    return server


# -- hello: real HTTP round trip -------------------------------------------


def test_dial_hello_completes_a_real_http_handshake():
    alice_identity = bootstrap_node_identity("alice")
    bob_identity = bootstrap_node_identity("bob")
    alice_node = LinkNode(identity=alice_identity)
    bob_node = LinkNode(identity=bob_identity)

    async def scenario():
        bob_server = await _run_server(bob_node, lambda: _hello_for(bob_node))
        try:
            async with aiohttp.ClientSession() as session:
                return await dial_hello(
                    alice_node, session, f"http://127.0.0.1:{bob_server.port}", _hello_for(alice_node)
                )
        finally:
            await bob_server.stop()

    record = asyncio.run(scenario())
    assert record.fingerprint == bob_identity.fingerprint
    # A real, single POST/response round trip mutually introduced both
    # sides -- alice's dial handed her hello to bob's server (which
    # recorded it), and bob's own hello came back in the response,
    # which dial_hello fed into alice's own handle_hello.
    assert bob_identity.fingerprint in alice_node.peers
    assert alice_identity.fingerprint in bob_node.peers


def test_dial_hello_raises_link_protocol_error_for_a_forged_returned_hello():
    """If the *peer's own* returned hello fails verification, that's a
    LinkProtocolError from node.handle_hello, not a transport error --
    propagated unwrapped, same exception every other caller already
    handles."""
    alice_identity = bootstrap_node_identity("alice")
    bob_identity = bootstrap_node_identity("bob")
    mallory_identity = bootstrap_node_identity("mallory")
    alice_node = LinkNode(identity=alice_identity)
    bob_node = LinkNode(identity=bob_identity)

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
        bob_server = await _run_server(bob_node, _forged_bob_hello)
        try:
            async with aiohttp.ClientSession() as session:
                await dial_hello(
                    alice_node, session, f"http://127.0.0.1:{bob_server.port}", _hello_for(alice_node)
                )
        finally:
            await bob_server.stop()

    with pytest.raises(LinkProtocolError):
        asyncio.run(scenario())


def test_dial_hello_raises_link_transport_error_when_the_server_rejects_it():
    """A client hello the server's handle_hello refuses (here: a
    descriptor claiming the wrong subject) surfaces as the server's own
    HTTP 400 -- dial_hello wraps that as LinkTransportError, not
    LinkProtocolError, since nothing local re-ran the verification that
    actually failed."""
    alice_identity = bootstrap_node_identity("alice")
    bob_identity = bootstrap_node_identity("bob")
    alice_node = LinkNode(identity=alice_identity)
    bob_node = LinkNode(identity=bob_identity)

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
        bob_server = await _run_server(bob_node, lambda: _hello_for(bob_node))
        try:
            async with aiohttp.ClientSession() as session:
                await dial_hello(
                    alice_node, session, f"http://127.0.0.1:{bob_server.port}", forged_alice_hello
                )
        finally:
            await bob_server.stop()

    with pytest.raises(LinkTransportError):
        asyncio.run(scenario())


def test_dial_hello_raises_link_transport_error_when_nothing_is_listening():
    alice_node = LinkNode(identity=bootstrap_node_identity("alice"))

    async def scenario():
        async with aiohttp.ClientSession() as session:
            await dial_hello(alice_node, session, "http://127.0.0.1:1", _hello_for(alice_node))

    with pytest.raises(LinkTransportError):
        asyncio.run(scenario())


# -- events: real HTTP gossip push ------------------------------------------


def test_push_events_gossips_a_real_key_rotation_over_http():
    alice_identity = bootstrap_node_identity("alice")
    bob_identity = bootstrap_node_identity("bob")
    alice_node = LinkNode(identity=alice_identity)
    bob_node = LinkNode(identity=bob_identity)

    async def scenario():
        bob_server = await _run_server(bob_node, lambda: _hello_for(bob_node))
        try:
            async with aiohttp.ClientSession() as session:
                await dial_hello(
                    alice_node, session, f"http://127.0.0.1:{bob_server.port}", _hello_for(alice_node)
                )

                rotated = rotate_operational_key(alice_identity, purpose="signing")
                alice_node.identity = rotated
                revoke, authorize = rotated.transitions[-2:]

                return await push_events(
                    alice_node, session, f"http://127.0.0.1:{bob_server.port}", [revoke, authorize]
                )
        finally:
            await bob_server.stop()

    accepted = asyncio.run(scenario())
    revoke_id = alice_node.identity.transitions[-2].content_id
    authorize_id = alice_node.identity.transitions[-1].content_id
    assert accepted == [revoke_id, authorize_id]
    assert bob_node.peers[alice_identity.fingerprint].transitions[-1].content_id == authorize_id


def test_push_events_raises_link_transport_error_for_a_stranger():
    """bob's server refuses events from a peer with no completed hello
    on file (round 116's own scope) -- surfaces as a 400, wrapped as
    LinkTransportError here, same shape as any other server-side
    rejection."""
    alice_identity = bootstrap_node_identity("alice")
    bob_node = LinkNode(identity=bootstrap_node_identity("bob"))
    alice_node = LinkNode(identity=alice_identity)

    async def scenario():
        bob_server = await _run_server(bob_node, lambda: _hello_for(bob_node))
        try:
            async with aiohttp.ClientSession() as session:
                rotated = rotate_operational_key(alice_identity, purpose="signing")
                alice_node.identity = rotated
                await push_events(
                    alice_node, session, f"http://127.0.0.1:{bob_server.port}", list(rotated.transitions[-2:])
                )
        finally:
            await bob_server.stop()

    with pytest.raises(LinkTransportError):
        asyncio.run(scenario())


def test_link_server_port_raises_before_start():
    server = LinkServer(
        host="127.0.0.1", port=0, node=LinkNode(identity=bootstrap_node_identity("alice")),
        own_hello_provider=lambda: None,
    )
    with pytest.raises(RuntimeError):
        _ = server.port
