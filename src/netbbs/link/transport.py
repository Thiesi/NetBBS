"""
Real `aiohttp`-based transport for `netbbs.link.protocol` (design doc
§11, round 117) — the client dial functions and server route handlers
that translate `LinkNode`'s message-passing interface (round 116) into
actual HTTP+JSON requests over a real socket.

Mirrors `netbbs.net.web.WebServer`'s own `AppRunner`/`TCPSite` start/
stop/`port` lifecycle — the shape every server this codebase stands up
already uses, not a new one invented for Link.

This module is deliberately the *only* place that imports both
`aiohttp` and `netbbs.link.protocol` together — `protocol.py` itself
stays untouched and provably transport-agnostic, matching round 116's
whole point in building it that way. `LinkNode.handle_hello`/
`handle_events` do all the actual verification; this module's job is
only "get bytes to the right place and hand what arrives to the right
method."

Route shape: `POST {LINK_PATH_PREFIX}/hello` (mutual — a peer's own
hello comes back in the response body, matching round 116's design-doc
note on how store-and-forward's *promise* is preserved even though a
successful dial's response can still opportunistically carry a prompt
reply) and `POST {LINK_PATH_PREFIX}/events/{fingerprint}` (gossip push,
`fingerprint` naming whose own events these are — this design only
ever gossips a node's *own* key_transitions, never relays on another's
behalf yet, matching round 116's "no relay from a stranger" scope
note).

**Deliberately not wired into node startup/config this round** — no
`netbbs.net.nodeconfig`/`netbbs.__main__` changes, no persistent
`own_hello_provider` beyond what a caller (today: only tests) supplies
directly. See design doc round 117 sign-off note for the full list of
what's still open.

**Round 120 adds a required `lane: DatabaseLane` to `LinkServer` and
`dial_hello`** — the only three call sites in this codebase that
mutate a `LinkNode`'s peer table or event store (`_handle_hello`,
`_handle_events`, and `dial_hello`'s own trailing `handle_hello` call)
now persist what changed via `netbbs.link.store`, off the event loop,
after `netbbs.link.protocol`'s own in-memory verification succeeds.
`push_events` is untouched — it never mutates local `LinkNode` state.
See design doc round 120 for the full reasoning on why persistence
lives here rather than inside `netbbs.link.protocol` itself.
"""

from __future__ import annotations

from typing import Callable

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from netbbs.link.events import KeyTransition
from netbbs.link.protocol import HelloMessage, LinkNode, LinkProtocolError, PeerRecord
from netbbs.link.store import save_event, save_peer
from netbbs.storage.execution import DatabaseLane

LINK_PATH_PREFIX = "/link/v1"

_DEFAULT_TIMEOUT_SECONDS = 10.0


class LinkTransportError(Exception):
    """Raised for anything wrong at the transport level: a connection
    failure, a request timeout, a non-200 response, or a response body
    that doesn't parse as the message it was supposed to carry. Kept
    distinct from `LinkProtocolError` (still raised unwrapped, never
    caught here) — that one means "the message arrived fine but didn't
    verify," a different failure a caller may want to handle
    differently (e.g. log-and-drop a hostile peer vs. retry a flaky
    connection)."""


class LinkServer:
    """
    Accepts real inbound Link HTTP+JSON traffic for one `LinkNode`.

    `own_hello_provider` is a plain callable returning this node's
    current `HelloMessage` on demand — deliberately not something this
    class computes itself (addresses/outgoing-only/timestamp are
    deployment/node-config concerns, out of scope here, same reasoning
    `LinkNode.build_hello` itself already applies at the protocol
    layer, one level down).

    `lane` (round 120): the background `DatabaseLane` this server
    persists accepted peers/events through, off the event loop, after
    `node`'s own in-memory verification accepts them.
    """

    def __init__(
        self,
        host: str,
        port: int,
        node: LinkNode,
        own_hello_provider: Callable[[], HelloMessage],
        lane: DatabaseLane,
    ) -> None:
        self._host = host
        self._port = port
        self._node = node
        self._own_hello_provider = own_hello_provider
        self._lane = lane
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @property
    def port(self) -> int:
        if self._site is None:
            raise RuntimeError("server has not been started yet")
        return self._site.port

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post(f"{LINK_PATH_PREFIX}/hello", self._handle_hello)
        app.router.add_post(f"{LINK_PATH_PREFIX}/events/{{fingerprint}}", self._handle_events)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _handle_hello(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            hello = HelloMessage.from_dict(body)
        except (KeyError, ValueError, TypeError) as exc:
            return web.json_response({"error": f"malformed hello: {exc}"}, status=400)

        try:
            peer = self._node.handle_hello(hello)
        except LinkProtocolError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        await self._lane.run(save_peer, peer)
        return web.json_response(self._own_hello_provider().to_dict())

    async def _handle_events(self, request: web.Request) -> web.Response:
        fingerprint = request.match_info["fingerprint"]
        try:
            raw_events = await request.json()
        except ValueError as exc:
            return web.json_response({"error": f"malformed events: {exc}"}, status=400)

        try:
            accepted = self._node.handle_events(fingerprint, raw_events)
        except LinkProtocolError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except (KeyError, TypeError) as exc:
            return web.json_response({"error": f"malformed events: {exc}"}, status=400)

        for content_id in accepted:
            envelope = self._node.events[content_id]
            await self._lane.run(
                save_event,
                sender_fingerprint=fingerprint,
                content_id=content_id,
                object_type=envelope["envelope"]["object_type"],
                envelope=envelope,
            )
        if accepted:
            # sender.transitions grew -- one updated write, not one per
            # accepted event (round 120).
            await self._lane.run(save_peer, self._node.peers[fingerprint])

        return web.json_response({"accepted": accepted})


async def dial_hello(
    node: LinkNode,
    session: ClientSession,
    base_url: str,
    hello: HelloMessage,
    lane: DatabaseLane,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> PeerRecord:
    """
    Say hello to a peer at `base_url` (e.g. `"http://198.51.100.7:7862"`,
    no trailing slash): POST `hello`, feed the peer's own hello — carried
    back in the response — into `node.handle_hello`, persist the
    resulting `PeerRecord` via `lane` (round 120), and return it.

    Raises `LinkTransportError` for anything transport-level gone wrong
    (connection failure, timeout, non-200, an unparseable response
    body). If the peer's own returned hello fails verification,
    `LinkProtocolError` propagates unwrapped from `node.handle_hello` —
    same exception every other caller of that method already handles.
    """
    url = f"{base_url}{LINK_PATH_PREFIX}/hello"
    try:
        async with session.post(
            url, json=hello.to_dict(), timeout=ClientTimeout(total=timeout)
        ) as response:
            if response.status != 200:
                text = await response.text()
                raise LinkTransportError(f"hello to {url} failed: HTTP {response.status}: {text}")
            body = await response.json()
    except ClientError as exc:
        raise LinkTransportError(f"could not reach {url}: {exc}") from exc

    try:
        peer_hello = HelloMessage.from_dict(body)
    except (KeyError, ValueError, TypeError) as exc:
        raise LinkTransportError(f"malformed hello response from {url}: {exc}") from exc

    peer = node.handle_hello(peer_hello)
    await lane.run(save_peer, peer)
    return peer


async def push_events(
    node: LinkNode,
    session: ClientSession,
    base_url: str,
    transitions: list[KeyTransition],
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> list[str]:
    """
    Push `transitions` — this node's *own* `key_transition`s, per
    round 116's "no relay from a stranger" scope note — to a peer at
    `base_url`. Returns whichever content_ids the peer newly accepted;
    purely informational, since the sender's own copies are already
    known-good on its own side.

    Raises `LinkTransportError` for a transport-level failure. A
    peer rejecting one of the pushed events (e.g. an inconsistent
    chain) also surfaces as `LinkTransportError` here — unlike
    `dial_hello`, the rejection reason lives only in the peer's HTTP
    error body, not as a `LinkProtocolError` raised locally, since
    nothing on this side re-runs the peer's own verification.
    """
    url = f"{base_url}{LINK_PATH_PREFIX}/events/{node.identity.fingerprint}"
    payload = [t.to_dict() for t in transitions]
    try:
        async with session.post(
            url, json=payload, timeout=ClientTimeout(total=timeout)
        ) as response:
            if response.status != 200:
                text = await response.text()
                raise LinkTransportError(f"events push to {url} failed: HTTP {response.status}: {text}")
            body = await response.json()
    except ClientError as exc:
        raise LinkTransportError(f"could not reach {url}: {exc}") from exc

    try:
        return body["accepted"]
    except (KeyError, TypeError) as exc:
        raise LinkTransportError(f"malformed events response from {url}: {exc}") from exc
