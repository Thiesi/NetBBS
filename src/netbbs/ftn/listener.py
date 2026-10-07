"""
Answering FTN calls (design doc §6.8).

One `FtnListener` per node. It listens while at least one enabled network
answers calls, on the node-wide port in `node_config` (`ftn_listen_port`,
24554 by default; `ftn_listen_host` to bind one address), and stops when
none does. Settings are checked every `CHECK_INTERVAL`, so the SysOp's
switch takes effect without a restart.

A caller presenting a configured uplink's address must prove that
network's session password (CRAM-MD5 offered, plain accepted); a wrong one
ends the call. A proven caller gets the network's waiting mail and its
packets are tossed. Any other caller gets a non-secure session: it may
deliver, but what it brings is held for the SysOp (`netbbs.ftn.tosser`),
and it is given nothing.

At most `MAX_SESSIONS` calls run at once; another caller is told the node
is busy (`M_BSY`) and disconnected.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import dataclass

from netbbs.config import get_config
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.binkp import (
    DEFAULT_PORT,
    M_BSY,
    BinkpError,
    OutgoingFile,
    encode_frame,
    run_session,
)
from netbbs.ftn.exchange import outbound_packet, summary, system_info, toss_received
from netbbs.ftn.networks import FtnNetwork, list_networks
from netbbs.ftn.queue import mark_outbound_sent
from netbbs.timeutil import utc_now_iso

_logger = logging.getLogger(__name__)

PORT_CONFIG_KEY = "ftn_listen_port"
HOST_CONFIG_KEY = "ftn_listen_host"
CHECK_INTERVAL = 60.0
MAX_SESSIONS = 4
RECENT_SESSIONS = 20


@dataclass(frozen=True)
class AnsweredCall:
    at: str
    peer: str  # the caller's IP address
    addresses: str
    secure: bool
    outcome: str


def listen_settings(db) -> tuple[str | None, int]:
    """`(host, port)`: host None means every address."""
    port_text = get_config(db, PORT_CONFIG_KEY)
    port = int(port_text) if port_text and port_text.isdigit() and 0 < int(port_text) < 65536 else DEFAULT_PORT
    return get_config(db, HOST_CONFIG_KEY) or None, port


class FtnListener:
    def __init__(self, lane, *, check_interval: float = CHECK_INTERVAL):
        self._lane = lane
        self._check_interval = check_interval
        self._task: asyncio.Task | None = None
        self._server: asyncio.base_events.Server | None = None
        self._bound: tuple[str | None, int] | None = None
        self._sessions: set[asyncio.Task] = set()
        self.recent: deque[AnsweredCall] = deque(maxlen=RECENT_SESSIONS)
        self.last_error: str | None = None

    @property
    def listening_on(self) -> tuple[str | None, int] | None:
        return self._bound if self._server is not None else None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="ftn-listener")

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._stop_server()
        sessions = list(self._sessions)
        for session in sessions:
            session.cancel()
        await asyncio.gather(*sessions, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- the loop must outlive one bad pass
                _logger.exception("FTN listener check failed")
            await asyncio.sleep(self._check_interval)

    async def reconcile(self) -> None:
        """Listen if an enabled network answers calls, on the configured
        address; stop otherwise."""
        networks = await self._lane.run(list_networks)
        wanted = await self._lane.run(listen_settings) if any(n.enabled and n.answers_calls for n in networks) else None
        if wanted == self._bound and (wanted is None or self._server is not None):
            return
        await self._stop_server()
        if wanted is None:
            return
        try:
            self._server = await asyncio.start_server(self._accept, wanted[0], wanted[1])
        except OSError as exc:
            self.last_error = f"cannot listen on port {wanted[1]}: {exc}"
            _logger.error("FTN: %s", self.last_error)
            self._bound = None
            return
        self._bound = wanted
        self.last_error = None
        _logger.info("FTN: answering calls on port %s", wanted[1])

    async def _stop_server(self) -> None:
        server, self._server = self._server, None
        self._bound = None
        if server is not None:
            server.close()
            with contextlib.suppress(OSError, asyncio.TimeoutError):
                await asyncio.wait_for(server.wait_closed(), 5)

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if len(self._sessions) >= MAX_SESSIONS:
            with contextlib.suppress(OSError):
                writer.write(encode_frame(M_BSY, b"Too many sessions, call again later"))
                await writer.drain()
            writer.close()
            return
        task = asyncio.current_task()
        self._sessions.add(task)
        try:
            await self._answer(reader, writer)
        finally:
            self._sessions.discard(task)
            writer.close()
            with contextlib.suppress(OSError, asyncio.TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 5)

    async def _answer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        peer_ip = peer[0] if isinstance(peer, tuple) else "?"
        networks = [n for n in await self._lane.run(list_networks) if n.enabled and n.answers_calls]
        if not networks:
            return
        system = await self._lane.run(system_info)
        # Packed before the session, so the callbacks inside it need no
        # database. Only a caller proving it is a network's uplink gets that
        # network's packet. A hub this node calls at the same moment may get
        # the same messages twice; its dupe check absorbs that.
        packets = {network.id: await outbound_packet(self._lane, network) for network in networks}
        state: dict[str, object] = {}

        def password_for(addresses: list[FtnAddress]) -> str | None:
            network = _uplink_network(networks, addresses)
            state["network"] = network
            return network.session_password if network is not None else None

        def outgoing_for(addresses: list[FtnAddress], secure: bool) -> list[OutgoingFile]:
            network = state.get("network")
            if not secure or network is None:
                return []
            state["packet"], state["ids"] = packets[network.id]
            return [state["packet"]] if state["packet"] is not None else []

        try:
            result = await run_session(
                reader, writer, originating=False, our_addresses=[n.our_address for n in networks],
                system=system, password_for=password_for, outgoing_for=outgoing_for,
            )
        except (OSError, asyncio.TimeoutError, BinkpError) as exc:
            self._record(peer_ip, "", False, f"failed: {exc}")
            _logger.warning("FTN: call from %s failed: %s", peer_ip, exc)
            return
        network = state["network"] if result.secure else _network_for_caller(networks, result.remote_addresses)
        packet, ids = state.get("packet"), state.get("ids") or []
        sent = len(ids) if packet is not None and packet.name in result.sent else 0
        if sent:
            await self._lane.run(mark_outbound_sent, ids)
        tossed = await toss_received(self._lane, network, result)
        outcome = summary(sent, len(result.received), tossed)
        addresses = " ".join(str(a) for a in result.remote_addresses)
        self._record(peer_ip, addresses, result.secure, outcome)
        _logger.info("FTN %s: answered %s (%s, %s) -- %s", network.name, addresses, peer_ip,
                     "secure" if result.secure else "non-secure", outcome)

    def _record(self, peer: str, addresses: str, secure: bool, outcome: str) -> None:
        self.recent.append(AnsweredCall(utc_now_iso(), peer, addresses, secure, outcome))


def _uplink_network(networks: list[FtnNetwork], addresses: list[FtnAddress]) -> FtnNetwork | None:
    """The network whose uplink the caller presents itself as."""
    for network in networks:
        if any(address.same_node(network.uplink_address) for address in addresses):
            return network
    return None


def _network_for_caller(networks: list[FtnNetwork], addresses: list[FtnAddress]) -> FtnNetwork:
    """Which network an unproven caller's files belong to: its uplink's, else
    the first in the caller's zone, else the first answering network."""
    network = _uplink_network(networks, addresses)
    if network is not None:
        return network
    zones = {address.zone for address in addresses}
    return next((n for n in networks if n.our_address.zone in zones), networks[0])
