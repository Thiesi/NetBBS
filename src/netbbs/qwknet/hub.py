"""
The QWKnet hub's endpoint (issue #1482, design doc §6.11): where the nodes
this node serves fetch their packets and upload their reply packets.

It is `/qwk.ssjs` on the web listener, speaking exactly what Synchronet's
`qwk.ssjs` speaks, so a Synchronet node calls it with its stock `qnet-http`
script (`?qnet-http -s %s <host> <password>`) and a NetBBS node with its
HTTPS transport:

- `GET` builds (or serves again, until confirmed) the node's packet, named
  `<our ID>.QWK`: 200 with it, 204 when nothing waits;
- `POST ?received=<bytes>` confirms it: 204 when the size is the packet's,
  and only then does the node's cursor move;
- `POST` with a body is a reply packet (`<our ID>.REP`), tossed at once
  through the same path as any QWKnet message: 200, or 400 with the reason
  when it can't be read at all.

**No FTP server.** Serving packets over FTP would mean embedding an FTP
server -- a second listener with PORT, PASV and its data connections to get
right -- for what one HTTP route does on the listener NetBBS already runs.
Synchronet nodes have called hubs over HTTP since `qnet-http` (2019), and a
NetBBS node does too. A hub that wants FTP for older systems runs a separate
QWK-capable BBS for them; nothing here prevents it.

**Every request is a login.** HTTP Basic authentication names the node's
QWK ID and its password, checked against the Argon2id hash on the
password-hashing pool. There is no anonymous access: a node account must be
enabled, on an enabled network that serves nodes. A source address that
fails `MAX_FAILURES` logins in `FAILURE_WINDOW` is refused for the rest of
the window (429), at most `MAX_CONCURRENT` requests run at once (503 past
it), and a reply packet is bounded by the node's own limit as it arrives.
Basic authentication sends the password readable: the hub's web listener
belongs behind TLS (the SysOp Handbook says how), as it already does for
callers' logins.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import collections
import logging
import time

from netbbs.auth.users import verify_password_off_loop
from netbbs.qwk.rep import RepError
from netbbs.qwknet.exchange import build_node_packet, confirm_node_packet, toss_node_rep
from netbbs.qwknet.networks import QwknetNetwork, QwknetNode, get_network, node_login, record_node_outcome
from netbbs.qwknet.transport import ENDPOINT

_logger = logging.getLogger(__name__)

MAX_FAILURES = 10
FAILURE_WINDOW = 15 * 60.0
MAX_TRACKED_ADDRESSES = 4096
MAX_CONCURRENT = 4
_REALM = 'Basic realm="QWKnet", charset="UTF-8"'


class QwknetHub:
    def __init__(self, lane, *, clock=time.monotonic):
        self._lane = lane
        self._clock = clock
        self._failures: collections.OrderedDict[str, collections.deque] = collections.OrderedDict()
        self._busy = asyncio.Semaphore(MAX_CONCURRENT)
        self._address_of = None

    def add_routes(self, app, *, address_of=None) -> None:
        """Register `/qwk.ssjs` on the web listener's application.
        `address_of(request)` names the caller as the rest of the web
        listener does (trusted proxies included)."""
        self._address_of = address_of
        app.router.add_route("GET", ENDPOINT, self.handle)
        app.router.add_route("POST", ENDPOINT, self.handle)

    # --- login throttle ---

    def _address(self, request) -> str:
        if self._address_of is not None:
            return self._address_of(request) or "?"
        return request.remote or "?"

    def _locked_out(self, address: str) -> bool:
        times = self._failures.get(address)
        if not times:
            return False
        cutoff = self._clock() - FAILURE_WINDOW
        while times and times[0] < cutoff:
            times.popleft()
        return len(times) >= MAX_FAILURES

    def _fail(self, address: str) -> None:
        times = self._failures.pop(address, None) or collections.deque(maxlen=MAX_FAILURES)
        times.append(self._clock())
        self._failures[address] = times
        while len(self._failures) > MAX_TRACKED_ADDRESSES:
            self._failures.popitem(last=False)

    async def _login(self, request, address: str) -> tuple[QwknetNetwork, QwknetNode] | None:
        header = request.headers.get("Authorization", "")
        scheme, _, encoded = header.partition(" ")
        if scheme.lower() != "basic" or not encoded:
            return None
        try:
            user, separator, password = base64.b64decode(encoded.strip(), validate=True).decode("utf-8").partition(":")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            self._fail(address)
            return None
        if not separator or not user or len(user) > 16 or len(password) > 256:
            self._fail(address)
            return None
        found = await self._lane.run(_account, user)
        if found is None:
            self._fail(address)
            return None
        network, node, password_hash = found
        if not await verify_password_off_loop(password, password_hash):
            self._fail(address)
            _logger.warning("QWKnet hub: failed login for %s from %s", node.qwk_id, address)
            return None
        return network, node

    # --- the endpoint ---

    async def handle(self, request):
        from aiohttp import web

        address = self._address(request)
        if self._locked_out(address):
            return web.Response(status=429, text="Too many failed logins; try again later.")
        if self._busy.locked():
            return web.Response(status=503, text="The hub is busy; try again shortly.")
        async with self._busy:
            account = await self._login(request, address)
            if account is None:
                return web.Response(status=401, text="QWKnet login needed.", headers={"WWW-Authenticate": _REALM})
            network, node = account
            if request.method == "GET":
                return await self._get(network, node)
            if "received" in request.query:
                return await self._confirm(node, request.query.get("received", ""))
            return await self._upload(request, network, node)

    async def _get(self, network: QwknetNetwork, node: QwknetNode):
        from aiohttp import web

        data = await self._lane.run(build_node_packet, network, node)
        if data is None:
            await self._lane.run(record_node_outcome, node.id, "fetched: nothing waiting", fetched=True)
            return web.Response(status=204)
        await self._lane.run(record_node_outcome, node.id, f"fetched a packet of {len(data)} bytes", fetched=True)
        return web.Response(body=data, headers={
            "Content-Type": "application/octet-stream",
            "Content-Disposition": f'inline; filename="{network.our_id}.qwk"',
        })

    async def _confirm(self, node: QwknetNode, value: str):
        from aiohttp import web

        if not value.isdigit() or len(value) > 12:
            return web.Response(status=400, text="received must be the packet's size in bytes")
        if await self._lane.run(confirm_node_packet, node.id, int(value)):
            await self._lane.run(record_node_outcome, node.id, "confirmed its packet")
            return web.Response(status=204)
        return web.Response(status=400, text="received size mismatch, or no packet waiting")

    async def _upload(self, request, network: QwknetNetwork, node: QwknetNode):
        from aiohttp import web

        limit = node.rep_kib * 1024
        if request.content_length is not None and request.content_length > limit:
            return web.Response(status=413, text=f"reply packets from {node.qwk_id} are limited to {node.rep_kib} KiB")
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.content.iter_chunked(64 * 1024):
            total += len(chunk)
            if total > limit:
                return web.Response(status=413,
                                    text=f"reply packets from {node.qwk_id} are limited to {node.rep_kib} KiB")
            chunks.append(chunk)
        if not total:
            return web.Response(status=400, text="No post data provided")
        try:
            result = await self._lane.run(toss_node_rep, network, node, b"".join(chunks))
        except RepError as exc:
            await self._lane.run(record_node_outcome, node.id, f"reply packet refused: {exc}", uploaded=True)
            return web.Response(status=400, text=f"reply packet not used: {exc}")
        line = f"uploaded: {result.summary()}"
        await self._lane.run(record_node_outcome, node.id, line, uploaded=True)
        _logger.info("QWKnet hub: %s on %s %s", node.qwk_id, network.name, line)
        return web.Response(status=200, text=line)


def _account(db, user: str) -> tuple[QwknetNetwork, QwknetNode, str] | None:
    """The account `user` names, when it may log in now: enabled, on an
    enabled network that serves nodes."""
    found = node_login(db, user)
    if found is None:
        return None
    node, password_hash = found
    network = get_network(db, node.network_id)
    if network is None or not node.enabled or not network.enabled or not network.serve_nodes:
        return None
    return network, node, password_hash
