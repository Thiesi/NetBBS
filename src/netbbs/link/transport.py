"""
Real `aiohttp`-based transport for `netbbs.link.protocol` (design doc
§11) — the client dial functions and server route handlers
that translate `LinkNode`'s message-passing interface into
actual HTTP+JSON requests over a real socket.

Mirrors `netbbs.net.web.WebServer`'s own `AppRunner`/`TCPSite` start/
stop/`port` lifecycle — the shape every server this codebase stands up
already uses, not a new one invented for Link.

This module is deliberately the *only* place that imports both
`aiohttp` and `netbbs.link.protocol` together — `protocol.py` itself
stays untouched and provably transport-agnostic, matching the
whole point in building it that way. `LinkNode.handle_hello`/
`handle_events` do all the actual verification; this module's job is
only "get bytes to the right place and hand what arrives to the right
method."

Route shape: `POST {LINK_PATH_PREFIX}/hello` (mutual — a peer's own
hello comes back in the response body, matching the design-doc
note on how store-and-forward's *promise* is preserved even though a
successful dial's response can still opportunistically carry a prompt
reply) and `POST {LINK_PATH_PREFIX}/events/{fingerprint}` (gossip push,
`fingerprint` naming whose own events these are — this design only
ever gossips a node's *own* key_transitions, never relays on another's
behalf yet, matching the "no relay from a stranger" scope
note).

**`LinkServer`/`dial_hello` require a `lane: DatabaseLane`** — the only
three call sites in this codebase that
mutate a `LinkNode`'s peer table or event store (`_handle_hello`,
`_handle_events`, and `dial_hello`'s own trailing `handle_hello` call)
persist what changed via `netbbs.link.store`, off the event loop,
after `netbbs.link.protocol`'s own in-memory verification succeeds.
`push_events` is untouched — it never mutates local `LinkNode` state.
See the design doc for the full reasoning on why persistence
lives here rather than inside `netbbs.link.protocol` itself.
"""

from __future__ import annotations

import logging
from typing import Callable

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from netbbs.link.boards import (
    BoardCarryLimitError,
    materialize_carried_board,
    materialize_carried_post,
    materialize_carried_post_edit,
    record_board_origin_change,
)
from netbbs.link.events import (
    BOARD_GENESIS_OBJECT_TYPE,
    BOARD_ORIGIN_TRANSFER_ACCEPTED_OBJECT_TYPE,
    BOARD_POST_EDIT_OBJECT_TYPE,
    BOARD_POST_OBJECT_TYPE,
    LINK_MESSAGE_ACCEPTED_OBJECT_TYPE,
    LINK_MESSAGE_BOUNCED_OBJECT_TYPE,
    LINK_MESSAGE_OBJECT_TYPE,
    BoardGenesis,
    BoardOriginTransferAccepted,
    BoardOriginTransferOffer,
    BoardPost,
    BoardPostEdit,
    KeyTransition,
    LinkMessage,
    LinkMessageAccepted,
    LinkMessageBounced,
    RelayConsentRequest,
    RelayConsentResponse,
    build_relay_consent_request,
    build_relay_consent_response,
    strict_json_loads,
)
from netbbs.link.mail import apply_link_message_accepted, apply_link_message_bounced, deliver_link_message
from netbbs.link.protocol import (
    _MAX_EVENTS_PER_REQUEST,
    HelloMessage,
    InventoryRequest,
    LinkNode,
    LinkProtocolError,
    PeerListMessage,
    PeerRecord,
)
from netbbs.link.relay_mailbox import (
    RelayMailboxFullError,
    deposit_relay_mailbox_envelope,
    pickup_relay_mailbox_envelopes,
)
from netbbs.link.store import board_event_diff, save_candidate_descriptor, save_event, save_peer, save_relay_consent
from netbbs.net.throttle import LinkRequestThrottle
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import utc_now_iso

_logger = logging.getLogger(__name__)

_LINK_THROTTLE_APP_KEY: web.AppKey[LinkRequestThrottle] = web.AppKey("link_throttle", LinkRequestThrottle)

LINK_PATH_PREFIX = "/link/v1"

_DEFAULT_TIMEOUT_SECONDS = 10.0

# Issue #58: `LinkServer`'s own default resource cap on relay-
# serving when a caller doesn't supply `max_relay_clients` explicitly
# (every test in this codebase predating that parameter, plus any
# caller that doesn't care to tune it) -- `netbbs.net.nodeconfig.
# LinkConfig.max_relay_clients` carries the real, SysOp-adjustable
# value for an actual running node (see `netbbs.__main__`'s own
# `LinkServer(...)` construction).
_DEFAULT_MAX_RELAY_CLIENTS = 20

# Design doc §13.9 (issue #60's third operational slice): same "own
# default, real config value lives in netbbs.net.nodeconfig.LinkConfig"
# split as _DEFAULT_MAX_RELAY_CLIENTS above, for the three quotas added
# this slice that `LinkServer` itself now enforces.
_DEFAULT_MAX_PEERS = 1000
_DEFAULT_MAX_CARRIED_BOARDS = 500

# Turns aiohttp's implicit 1 MiB `client_max_size` default into a
# deliberate, documented value -- sized to comfortably fit `netbbs.link.
# protocol._MAX_EVENTS_PER_REQUEST` (200) worth of events.
_LINK_CLIENT_MAX_SIZE_BYTES = 2 * 1024 * 1024


@web.middleware
async def _rate_limit_middleware(request: web.Request, handler):
    """Design doc §13.9: applied to every route on this server,
    including the two unauthenticated ones (`/hello`, `/peers`) -- a
    stranger's request must be rate-limited before anything else runs,
    not just an already-verified peer's. `request.app["link_throttle"]`
    is `None` when a caller didn't supply one (every test predating this
    middleware, plus any caller that doesn't care to tune it) -- a no-op
    pass-through in that case, matching this project's existing
    opt-in-by-construction convention for every other optional resource
    cap in this module."""
    throttle: LinkRequestThrottle | None = request.app.get(_LINK_THROTTLE_APP_KEY)
    if throttle is not None and not throttle.allow(request.remote):
        return web.json_response({"error": "rate limit exceeded"}, status=429)
    return await handler(request)


async def persist_accepted_events(
    lane: DatabaseLane,
    node: LinkNode,
    accepted: list[str],
    *,
    sender_fingerprint: str,
    max_carried_boards: int | None,
) -> None:
    """
    Persist and follow up on every content_id `LinkNode.handle_events`
    just returned as newly accepted -- shared by `LinkServer._handle_
    events` (direct push, `sender_fingerprint` is the wire-level peer)
    and `netbbs.link.sync`'s inventory-response handling (issue #85,
    `sender_fingerprint` is whichever peer this node happened to pull
    the response from -- possibly a relay, not the content's own
    author/origin; harmless here, since this parameter only ever feeds
    `link_events.sender_fingerprint` bookkeeping/`materialize_carried_
    post`'s own diagnostic column, never anything `handle_events` has
    already independently verified by the time accepted content reaches
    this function).

    `sender.transitions` growing (a `key_transition` acceptance) is
    **not** persisted here -- callers with a real peer relationship to
    update (`LinkServer._handle_events`) do that themselves afterward,
    since an inventory response has no `key_transition` events to begin
    with (design doc §8.8's board-only scope) and no single
    `sender_fingerprint` here is guaranteed to even be an existing
    `node.peers` entry worth re-saving.
    """
    for content_id in accepted:
        envelope = node.events[content_id]
        object_type = envelope["envelope"]["object_type"]
        # Design doc §9.3/issue #73: board_post/board_post_edit skip
        # the generic save_event dispatch below entirely --
        # materialize_carried_post/_edit each persist the underlying
        # link_events row themselves, in the same transaction as the
        # posts projection, closing the crash window every other
        # object type here still has between save_event and its own
        # follow-up (materialize_carried_board's own docstring notes
        # this same gap, not fixed for genesis).
        if object_type == BOARD_POST_OBJECT_TYPE:
            await lane.run(
                materialize_carried_post, BoardPost.from_dict(envelope), sender_fingerprint=sender_fingerprint
            )
            continue
        elif object_type == BOARD_POST_EDIT_OBJECT_TYPE:
            await lane.run(
                materialize_carried_post_edit, BoardPostEdit.from_dict(envelope), sender_fingerprint=sender_fingerprint
            )
            continue

        await lane.run(
            save_event,
            sender_fingerprint=sender_fingerprint,
            content_id=content_id,
            object_type=object_type,
            envelope=envelope,
        )
        # Link messages (design doc) need real follow-up
        # beyond persisting the envelope -- decrypt/deliver into a
        # local mailbox or bounce, and apply an incoming
        # acknowledgement to the outbound row it's about.
        # Issue #53's carry-materialization gap means board_genesis
        # and board_origin_transfer_accepted both need real follow-up
        # too: a received genesis has nothing a local user could
        # browse without also becoming a real Board row (see
        # materialize_carried_board's own docstring for why this was
        # missing even for a board this node has carried all along),
        # and an accepted transfer must update this node's own
        # locally-materialized copy's current-origin record even when
        # this node was only a bystander to the transfer, not a party
        # to it (see record_board_origin_change's own docstring).
        if object_type == LINK_MESSAGE_OBJECT_TYPE:
            await lane.run(deliver_link_message, envelope, node_identity=node.identity)
        elif object_type == LINK_MESSAGE_ACCEPTED_OBJECT_TYPE:
            await lane.run(apply_link_message_accepted, envelope)
        elif object_type == LINK_MESSAGE_BOUNCED_OBJECT_TYPE:
            await lane.run(apply_link_message_bounced, envelope)
        elif object_type == BOARD_GENESIS_OBJECT_TYPE:
            try:
                await lane.run(
                    materialize_carried_board,
                    BoardGenesis.from_dict(envelope),
                    own_fingerprint=node.identity.fingerprint,
                    max_carried_boards=max_carried_boards,
                )
            except BoardCarryLimitError as exc:
                # Design doc §13.9: the genesis event above is
                # already accepted/persisted (save_event, earlier in
                # this loop) and keeps gossiping normally -- only
                # this node's own local materialization is refused,
                # logged rather than surfaced as a failed request
                # (the peer that pushed it did nothing wrong; this
                # node simply declined to carry one more board).
                _logger.warning("Link sync: %s", exc)
        elif object_type == BOARD_ORIGIN_TRANSFER_ACCEPTED_OBJECT_TYPE:
            transfer_accepted = BoardOriginTransferAccepted.from_dict(envelope)
            await lane.run(
                record_board_origin_change,
                transfer_accepted.payload["board_id"],
                transfer_accepted.payload["new_origin_fingerprint"],
            )


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

    `lane`: the background `DatabaseLane` this server
    persists accepted peers/events through, off the event loop, after
    `node`'s own in-memory verification accepts them.

    `relay_serving_enabled`/`max_relay_clients` (issue #58):
    this node's own policy for `_handle_relay_consent` -- whether to
    ever grant a relay-consent request at all, and the cap on how many
    simultaneous grants to hold once serving is enabled (design doc
    §12: "a conservative resource cap... and an easy opt-out"). Plain
    constructor parameters, not read from `netbbs.net.nodeconfig`
    directly -- this module has no config-loading concern of its own
    (matching `own_hello_provider`'s own "deployment concerns are the
    caller's job" reasoning just above); `netbbs.__main__` is the one
    real caller that threads `LinkConfig`'s values through.

    `max_peers`/`max_carried_boards`/`throttle` (design doc §13.9,
    issue #60's third operational slice): same "plain constructor
    parameter, safe default, real value threaded through by `netbbs.
    __main__`" shape as `max_relay_clients` just above. `throttle`
    (`netbbs.net.throttle.LinkRequestThrottle`) defaults to `None` --
    unbounded, matching every other quota parameter's own default here
    -- rather than manufacturing one internally, since its token-bucket
    state is meant to be node-lifetime and constructed once, the same
    reasoning `LoginThrottle` is already built once in `netbbs.__main__`
    rather than per-server.
    """

    def __init__(
        self,
        host: str,
        port: int,
        node: LinkNode,
        own_hello_provider: Callable[[], HelloMessage],
        lane: DatabaseLane,
        *,
        relay_serving_enabled: bool = True,
        max_relay_clients: int = _DEFAULT_MAX_RELAY_CLIENTS,
        max_peers: int | None = _DEFAULT_MAX_PEERS,
        max_carried_boards: int | None = _DEFAULT_MAX_CARRIED_BOARDS,
        throttle: LinkRequestThrottle | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._node = node
        self._own_hello_provider = own_hello_provider
        self._lane = lane
        self._relay_serving_enabled = relay_serving_enabled
        self._max_relay_clients = max_relay_clients
        self._max_peers = max_peers
        self._max_carried_boards = max_carried_boards
        self._throttle = throttle
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @property
    def port(self) -> int:
        if self._site is None:
            raise RuntimeError("server has not been started yet")
        return self._site.port

    async def start(self) -> None:
        app = web.Application(client_max_size=_LINK_CLIENT_MAX_SIZE_BYTES, middlewares=[_rate_limit_middleware])
        app[_LINK_THROTTLE_APP_KEY] = self._throttle
        app.router.add_post(f"{LINK_PATH_PREFIX}/hello", self._handle_hello)
        app.router.add_post(f"{LINK_PATH_PREFIX}/events/{{fingerprint}}", self._handle_events)
        app.router.add_get(f"{LINK_PATH_PREFIX}/peers", self._handle_peers)
        app.router.add_post(f"{LINK_PATH_PREFIX}/relay-consent/{{fingerprint}}", self._handle_relay_consent)
        app.router.add_post(
            f"{LINK_PATH_PREFIX}/relay-mailbox/{{fingerprint}}/deposit", self._handle_relay_mailbox_deposit
        )
        app.router.add_post(f"{LINK_PATH_PREFIX}/relay-mailbox/pickup", self._handle_relay_mailbox_pickup)
        app.router.add_post(f"{LINK_PATH_PREFIX}/inventory/{{fingerprint}}", self._handle_inventory)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _handle_hello(self, request: web.Request) -> web.Response:
        try:
            body = await request.json(loads=strict_json_loads)
            hello = HelloMessage.from_dict(body)
        except (KeyError, ValueError, TypeError) as exc:
            return web.json_response({"error": f"malformed hello: {exc}"}, status=400)

        try:
            peer = self._node.handle_hello(hello, max_peers=self._max_peers)
        except LinkProtocolError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        await self._lane.run(save_peer, peer)
        return web.json_response(self._own_hello_provider().to_dict())

    async def _handle_events(self, request: web.Request) -> web.Response:
        fingerprint = request.match_info["fingerprint"]
        try:
            raw_events = await request.json(loads=strict_json_loads)
        except ValueError as exc:
            return web.json_response({"error": f"malformed events: {exc}"}, status=400)

        try:
            accepted = self._node.handle_events(fingerprint, raw_events)
        except LinkProtocolError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except (KeyError, TypeError) as exc:
            return web.json_response({"error": f"malformed events: {exc}"}, status=400)

        await persist_accepted_events(
            self._lane, self._node, accepted,
            sender_fingerprint=fingerprint, max_carried_boards=self._max_carried_boards,
        )
        if accepted:
            # sender.transitions grew -- one updated write, not one per
            # accepted event.
            await self._lane.run(save_peer, self._node.peers[fingerprint])

        return web.json_response({"accepted": accepted})

    async def _handle_inventory(self, request: web.Request) -> web.Response:
        """
        Design doc §8.8, issue #85: the responder side of pull-based
        catch-up. Deliberately no signature/peer-membership check here,
        unlike `_handle_events` -- this endpoint reads and returns
        already-accepted, already-verified events (no new signed
        content is being asserted), so there is nothing here for a
        signature to attest to, the same "reachability/bootstrap data
        isn't trust-gated" reasoning `_handle_peers` already applies,
        extended to board *content* rather than endpoint addresses.
        Board content this node carries is already pushed to every
        configured seed indiscriminately (§12) -- answering an
        inventory request is not a new confidentiality exposure beyond
        that existing behavior, only a differently-shaped read of it.
        Bounded the same way every other route here is: the rate-limit
        middleware and `client_max_size`, plus `board_event_diff`'s own
        `limit` argument capping the response itself.
        """
        try:
            body = await request.json(loads=strict_json_loads)
            inventory_request = InventoryRequest.from_dict(body)
        except (KeyError, ValueError, TypeError) as exc:
            return web.json_response({"error": f"malformed inventory request: {exc}"}, status=400)

        events, more_available = await self._lane.run(
            board_event_diff, inventory_request.boards, limit=_MAX_EVENTS_PER_REQUEST
        )
        return web.json_response({"events": events, "more_available": more_available})

    async def _handle_peers(self, request: web.Request) -> web.Response:
        """
        Peer-list exchange: shares this node's own currently-
        verified peers' endpoint descriptors with whoever asks.
        Deliberately unauthenticated, like `/hello` itself — the design doc
        already treats reachability information as discoverable
        bootstrap data, not something trust-gated ("a seed only ever
        supplies reachability information; it grants no trust"). A
        bodyless GET carries no signed claim about who's asking, so
        there is nothing here to verify even if this endpoint wanted to
        gate on it.
        """
        return web.json_response(self._node.build_peer_list().to_dict())

    async def _handle_relay_consent(self, request: web.Request) -> web.Response:
        """
        Issue #58: answer a `relay_consent_request` synchronously,
        in the *same* HTTP response -- the only shape that works for a
        requester who may itself be outgoing-only and can never be dialed
        back (see `RelayConsentRequest`'s own docstring). Mirrors `_handle_
        hello`'s own "reply carried in the response body" shape exactly.

        The opt-out/resource-cap policy decision (`self._relay_serving_
        enabled`/`self._max_relay_clients`, issue #58) lives
        here, not in `LinkNode` itself -- `handle_relay_consent_request`
        only ever verifies, deliberately never decides (see that
        method's own docstring: this pure/in-memory layer has no config
        to judge capacity against). A declined request -- whether from
        the opt-out or the cap -- is still a normal, signed `accepted=
        False` response, not an HTTP error: declining is an ordinary
        outcome of this exchange, not a protocol violation.
        """
        fingerprint = request.match_info["fingerprint"]
        try:
            body = await request.json(loads=strict_json_loads)
            consent_request = RelayConsentRequest.from_dict(body)
        except (KeyError, ValueError, TypeError) as exc:
            return web.json_response({"error": f"malformed relay_consent_request: {exc}"}, status=400)

        try:
            self._node.handle_relay_consent_request(fingerprint, consent_request)
        except LinkProtocolError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        accepted = self._relay_serving_enabled and len(self._node.relaying_for) < self._max_relay_clients
        decided_at = utc_now_iso()

        response = build_relay_consent_response(
            signing_identity=self._node.identity.signing_key,
            request_content_id=consent_request.content_id,
            relay_fingerprint=self._node.identity.fingerprint,
            requester_fingerprint=fingerprint,
            accepted=accepted,
            created_at=decided_at,
        )

        if accepted:
            self._node.relaying_for[fingerprint] = decided_at
            await self._lane.run(save_relay_consent, fingerprint, role="i_relay_for", accepted_at=decided_at)

        return web.json_response(response.to_dict())

    async def _handle_relay_mailbox_deposit(self, request: web.Request) -> web.Response:
        """
        Issue #58: accept one opaque `link_message` for
        `recipient_fingerprint`, held until that recipient itself picks
        it up (`_handle_relay_mailbox_pickup`). Unlike every other route
        on this server, the depositing caller need not be a completed
        peer -- receiving on behalf of a stranger is the entire point of
        relaying (see `netbbs.link.relay_mailbox`'s own module docstring
        for why no signature verification happens here either: this node
        can't meaningfully check a signature for an identity chain it
        may have never seen, and doesn't need to -- the recipient re-
        verifies everything itself after pickup).
        """
        recipient_fingerprint = request.match_info["fingerprint"]
        try:
            body = await request.json(loads=strict_json_loads)
            message = LinkMessage.from_dict(body)
        except (KeyError, ValueError, TypeError) as exc:
            return web.json_response({"error": f"malformed link_message: {exc}"}, status=400)

        if message.envelope.get("object_type") != LINK_MESSAGE_OBJECT_TYPE:
            return web.json_response(
                {"error": "only link_message may be deposited into a relay mailbox"}, status=400
            )

        if recipient_fingerprint not in self._node.relaying_for:
            return web.json_response(
                {"error": f"this node is not currently relaying for {recipient_fingerprint}"}, status=404
            )

        try:
            await self._lane.run(deposit_relay_mailbox_envelope, recipient_fingerprint, message)
        except RelayMailboxFullError as exc:
            return web.json_response({"error": str(exc)}, status=507)

        return web.json_response({"deposited": True})

    async def _handle_relay_mailbox_pickup(self, request: web.Request) -> web.Response:
        """
        Issue #58: hand back (and clear) whatever mail this
        relay is currently holding for the caller. Authenticated by
        requiring a fresh, verifiable `hello` as the request body rather
        than inventing a new signed message type — a hello already
        cryptographically proves the caller's identity (its descriptor
        signature verifies against the claimed fingerprint's own
        resolved signing key, the same check `_handle_hello` already
        performs), which is exactly the property picking up someone
        else's held mail needs and a bare GET keyed only by a URL path
        fingerprint would not have (see this method's own module-level
        context: `netbbs.link.relay_mailbox` deliberately has no notion
        of who's *allowed* to pick up, since it isn't the layer that can
        check that).
        """
        try:
            body = await request.json(loads=strict_json_loads)
            hello = HelloMessage.from_dict(body)
        except (KeyError, ValueError, TypeError) as exc:
            return web.json_response({"error": f"malformed hello: {exc}"}, status=400)

        try:
            peer = self._node.handle_hello(hello, max_peers=self._max_peers)
        except LinkProtocolError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        await self._lane.run(save_peer, peer)

        envelopes = await self._lane.run(pickup_relay_mailbox_envelopes, peer.fingerprint)
        return web.json_response({"envelopes": [e.to_dict() for e in envelopes]})


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
    resulting `PeerRecord` via `lane`, and return it.

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
            body = await response.json(loads=strict_json_loads)
    except (ClientError, ValueError) as exc:
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
    events: list[
        KeyTransition | BoardGenesis | BoardPost | BoardPostEdit
        | BoardOriginTransferOffer | BoardOriginTransferAccepted
        | LinkMessage | LinkMessageAccepted | LinkMessageBounced
    ],
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> list[str]:
    """
    Push `events` — this node's *own* originated events (`key_
    transition`s, `board_genesis`/`board_post`/`board_post_edit`,
    `board_origin_transfer_offer`/`board_origin_transfer_
    accepted` (issue #53), and `link_message`/`link_
    message_accepted`/`link_message_bounced`) — per the
    "no relay from a stranger" scope note —
    to a peer at `base_url`. Returns whichever content_ids the peer
    newly accepted; purely informational, since the sender's own copies
    are already known-good on its own side.

    Raises `LinkTransportError` for a transport-level failure. A
    peer rejecting one of the pushed events (e.g. an inconsistent
    chain) also surfaces as `LinkTransportError` here — unlike
    `dial_hello`, the rejection reason lives only in the peer's HTTP
    error body, not as a `LinkProtocolError` raised locally, since
    nothing on this side re-runs the peer's own verification.
    """
    url = f"{base_url}{LINK_PATH_PREFIX}/events/{node.identity.fingerprint}"
    payload = [e.to_dict() for e in events]
    try:
        async with session.post(
            url, json=payload, timeout=ClientTimeout(total=timeout)
        ) as response:
            if response.status != 200:
                text = await response.text()
                raise LinkTransportError(f"events push to {url} failed: HTTP {response.status}: {text}")
            body = await response.json(loads=strict_json_loads)
    except (ClientError, ValueError) as exc:
        raise LinkTransportError(f"could not reach {url}: {exc}") from exc

    try:
        return body["accepted"]
    except (KeyError, TypeError) as exc:
        raise LinkTransportError(f"malformed events response from {url}: {exc}") from exc


async def request_inventory(
    node: LinkNode,
    session: ClientSession,
    base_url: str,
    inventory_request: InventoryRequest,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> tuple[list[dict], bool]:
    """
    Design doc §8.8, issue #85: ask a peer at `base_url` what it has for
    `inventory_request.boards` that this node doesn't already. Returns
    the raw event dicts it reports (already in `push_events`'s own wire
    shape -- the caller feeds them through `LinkNode.handle_events`
    exactly as it would a push response, with no translation) and
    whether more remain beyond the peer's own response cap.

    Deliberately returns the raw dicts rather than applying them itself
    -- unlike `push_events` (whose sender already trusts its own
    events), this side must run real verification before trusting
    anything the peer claims to have, and `handle_events` is a `LinkNode`
    method with no I/O of its own; the caller (`netbbs.link.sync`) is
    the one already holding both `node` and a `DatabaseLane` to persist
    whatever gets accepted, the same shape `_pickup_relay_mail` already
    uses for an analogous "verify and persist what a fetch returned"
    step.

    Raises `LinkTransportError` for a transport-level failure, matching
    every other client function in this module.
    """
    url = f"{base_url}{LINK_PATH_PREFIX}/inventory/{node.identity.fingerprint}"
    try:
        async with session.post(
            url, json=inventory_request.to_dict(), timeout=ClientTimeout(total=timeout)
        ) as response:
            if response.status != 200:
                text = await response.text()
                raise LinkTransportError(f"inventory request to {url} failed: HTTP {response.status}: {text}")
            body = await response.json(loads=strict_json_loads)
    except (ClientError, ValueError) as exc:
        raise LinkTransportError(f"could not reach {url}: {exc}") from exc

    try:
        return body["events"], bool(body["more_available"])
    except (KeyError, TypeError) as exc:
        raise LinkTransportError(f"malformed inventory response from {url}: {exc}") from exc


async def request_peer_list(
    node: LinkNode,
    session: ClientSession,
    base_url: str,
    peer_fingerprint: str,
    lane: DatabaseLane,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> list[str]:
    """
    Request `base_url`'s own peer list (design doc §12) and feed it into
    `node.handle_peer_list`, persisting each newly recorded/refreshed
    candidate via `lane` (`netbbs.link.store.save_candidate_descriptor`)
    the same way `dial_hello` persists its own resulting `PeerRecord` —
    returns the fingerprints newly recorded.

    `peer_fingerprint` is the caller's to supply, not derived from the
    response — unlike a hello, a bodyless peer-list response carries no
    self-identifying claim about who answered it, so the caller (who
    already completed a real hello with whoever is at `base_url` before
    ever calling this) is the only one who actually knows. Raises
    `LinkProtocolError` unwrapped if `peer_fingerprint` turns out not to
    be a completed peer after all — same division of responsibility
    `dial_hello`'s own `node.handle_hello` call already has.
    """
    url = f"{base_url}{LINK_PATH_PREFIX}/peers"
    try:
        async with session.get(url, timeout=ClientTimeout(total=timeout)) as response:
            if response.status != 200:
                text = await response.text()
                raise LinkTransportError(f"peer list request to {url} failed: HTTP {response.status}: {text}")
            body = await response.json(loads=strict_json_loads)
    except (ClientError, ValueError) as exc:
        raise LinkTransportError(f"could not reach {url}: {exc}") from exc

    try:
        message = PeerListMessage.from_dict(body)
    except (KeyError, ValueError, TypeError) as exc:
        raise LinkTransportError(f"malformed peer list response from {url}: {exc}") from exc

    recorded = node.handle_peer_list(peer_fingerprint, message)
    for candidate_fingerprint in recorded:
        await lane.run(
            save_candidate_descriptor, candidate_fingerprint, node.candidate_descriptors[candidate_fingerprint]
        )
    return recorded


async def request_relay_consent(
    node: LinkNode,
    session: ClientSession,
    base_url: str,
    relay_fingerprint: str,
    lane: DatabaseLane,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> RelayConsentResponse:
    """
    Ask the peer at `base_url` (already a completed peer named
    `relay_fingerprint` -- same "caller already completed a real hello"
    precondition `request_peer_list` documents) to relay for this node
    (design doc §12, issue #58): build and sign a `relay_
    consent_request`, POST it to `/relay-consent/{this node's own
    fingerprint}`, and verify the answer carried back in the *same* HTTP
    response (`LinkServer._handle_relay_consent`'s own synchronous-reply
    shape -- see `RelayConsentRequest`'s docstring for why this can't be
    a `push_events`-style fire-and-forget the way every gossiped event
    pair is).

    On an accepted response, records `relay_fingerprint` into `node.
    relays_serving_me` and persists the grant via `lane`. A declined
    response is returned as-is, unpersisted (see `save_relay_consent`'s
    own docstring for why) -- not an error, an ordinary outcome of this
    exchange the caller (relay *selection*, issue #58 task #25) decides
    what to do about, e.g. trying the next-ranked candidate.

    Raises `LinkTransportError` for a transport-level failure. If the
    returned response fails verification, `LinkProtocolError` propagates
    unwrapped from `node.handle_relay_consent_response` — same division
    of responsibility every other caller of a `handle_*` method already
    has.
    """
    created_at = utc_now_iso()
    consent_request = build_relay_consent_request(
        signing_identity=node.identity.signing_key,
        requester_fingerprint=node.identity.fingerprint,
        relay_fingerprint=relay_fingerprint,
        created_at=created_at,
    )
    node.pending_own_relay_requests[relay_fingerprint] = consent_request

    url = f"{base_url}{LINK_PATH_PREFIX}/relay-consent/{node.identity.fingerprint}"
    try:
        async with session.post(
            url, json=consent_request.to_dict(), timeout=ClientTimeout(total=timeout)
        ) as response:
            if response.status != 200:
                text = await response.text()
                raise LinkTransportError(f"relay consent request to {url} failed: HTTP {response.status}: {text}")
            body = await response.json(loads=strict_json_loads)
    except (ClientError, ValueError) as exc:
        raise LinkTransportError(f"could not reach {url}: {exc}") from exc
    finally:
        node.pending_own_relay_requests.pop(relay_fingerprint, None)

    try:
        consent_response = RelayConsentResponse.from_dict(body)
    except (KeyError, ValueError, TypeError) as exc:
        raise LinkTransportError(f"malformed relay consent response from {url}: {exc}") from exc

    node.handle_relay_consent_response(relay_fingerprint, consent_response, original_request=consent_request)

    if consent_response.payload["accepted"]:
        accepted_at = consent_response.payload["created_at"]
        node.relays_serving_me[relay_fingerprint] = accepted_at
        await lane.run(save_relay_consent, relay_fingerprint, role="relay_for_me", accepted_at=accepted_at)

    return consent_response


async def deposit_into_relay_mailbox(
    session: ClientSession,
    relay_base_url: str,
    recipient_fingerprint: str,
    message: LinkMessage,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """
    Leave `message` (a `link_message` this node couldn't deliver
    directly) at the relay reachable at `relay_base_url`, for
    `recipient_fingerprint` to pick up on its own next outbound sync
    pass (design doc §12, issue #58). Does not require a
    completed hello with the relay first — see `LinkServer._handle_
    relay_mailbox_deposit`'s own docstring for why depositing is the one
    route on this server that's intentionally open to a stranger.

    Raises `LinkTransportError` for a transport-level failure, including
    the relay reporting it isn't currently relaying for `recipient_
    fingerprint`, or that its mailbox for that recipient is full — both
    surface as a non-200 response, same as any other rejected request
    on this transport.
    """
    url = f"{relay_base_url}{LINK_PATH_PREFIX}/relay-mailbox/{recipient_fingerprint}/deposit"
    try:
        async with session.post(
            url, json=message.to_dict(), timeout=ClientTimeout(total=timeout)
        ) as response:
            if response.status != 200:
                text = await response.text()
                raise LinkTransportError(f"relay mailbox deposit to {url} failed: HTTP {response.status}: {text}")
    except ClientError as exc:
        raise LinkTransportError(f"could not reach {url}: {exc}") from exc


async def pickup_from_relay_mailbox(
    session: ClientSession,
    relay_base_url: str,
    hello: HelloMessage,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> list[LinkMessage]:
    """
    Pick up (and clear) whatever mail the relay at `relay_base_url` is
    currently holding for this node -- design doc §12, issue
    #58. `hello` is this node's own current hello bundle, the caller's
    to supply (same "deployment config isn't this layer's concern"
    reasoning `dial_hello` already applies to its own `hello` parameter)
    -- it's what authenticates this call (`_handle_relay_mailbox_
    pickup`'s own docstring explains why a hello, not a new signed
    message type).

    Returns raw, **not yet verified** `LinkMessage`s -- the caller
    (issue #58 task #25's sync-loop wiring) is responsible for running
    each one through `LinkNode.handle_events` (keyed by that message's
    own claimed sender, not this relay) before treating it as accepted,
    same as `netbbs.link.relay_mailbox.pickup_relay_mailbox_envelopes`'s
    own docstring already documents on the server side.

    Raises `LinkTransportError` for a transport-level failure.
    """
    url = f"{relay_base_url}{LINK_PATH_PREFIX}/relay-mailbox/pickup"
    try:
        async with session.post(
            url, json=hello.to_dict(), timeout=ClientTimeout(total=timeout)
        ) as response:
            if response.status != 200:
                text = await response.text()
                raise LinkTransportError(f"relay mailbox pickup from {url} failed: HTTP {response.status}: {text}")
            body = await response.json(loads=strict_json_loads)
    except (ClientError, ValueError) as exc:
        raise LinkTransportError(f"could not reach {url}: {exc}") from exc

    try:
        raw_envelopes = body["envelopes"]
    except (KeyError, TypeError) as exc:
        raise LinkTransportError(f"malformed relay mailbox pickup response from {url}: {exc}") from exc

    try:
        return [LinkMessage.from_dict(raw) for raw in raw_envelopes]
    except (KeyError, ValueError, TypeError) as exc:
        raise LinkTransportError(f"malformed envelope in relay mailbox pickup response from {url}: {exc}") from exc
