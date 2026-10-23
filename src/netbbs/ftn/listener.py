"""
Answering FTN calls (design doc §6.8).

One `FtnListener` per node. It listens while at least one enabled network
answers calls, on the node-wide port in `node_config` (`ftn_listen_port`,
24554 by default), and stops when none does. It listens on every address,
IPv6 and IPv4, unless `ftn_listen_host` names the addresses to bind (one
or more IP addresses, separated by spaces or commas; issue #1467).
Settings are checked every `CHECK_INTERVAL`, so the SysOp's switch takes
effect without a restart.

**TLS** (issue #1468). With `ftn_tls_port` set, a second listener answers
BinkP over TLS on that port (24553 is the usual binkps port), on the same
addresses, with the certificate and key files in `ftn_tls_cert` and
`ftn_tls_key`. A caller's certificate is never asked for: it proves itself
with its session password, as on the plain port. When the files can't be
loaded, the plain port goes on answering and `last_error` says why.

A caller presenting a configured uplink's address must prove that
network's session password (CRAM-MD5 offered, plain accepted); a wrong one
ends the call. A proven caller gets the network's waiting mail and its
packets are tossed. Any other caller gets a non-secure session: it may
deliver, but what it brings is held for the SysOp (`netbbs.ftn.tosser`),
and it is given nothing -- unless its network sets **Give held mail to
callers without a password** (`FtnNetwork.give_held_unproven`, off by
default): then a caller presenting a node that held mail waits for is given
that node's held mail and files, and nothing else (issue #1471). A call
that fails part way keeps what moved before it failed, as a call out does
(`netbbs.ftn.mailer`): what the remote confirmed is settled, so it is not
sent twice.

At most `MAX_SESSIONS` calls run at once, on both ports together, one per
caller address; another caller is told the node is busy (`M_BSY`) and
disconnected.

`batch_hook(network, context)`, as on the mailer, is asked for more files
before each batch after the first of a binkp/1.1 multi-batch session.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
from collections import deque
from dataclasses import dataclass

from netbbs.config import get_config
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.binkp import (
    DEFAULT_PORT,
    M_BSY,
    BatchContext,
    BinkpError,
    OutgoingFile,
    SessionResult,
    encode_frame,
    run_session,
)
from netbbs.ftn.exchange import summary, system_info, toss_received
from netbbs.ftn.networks import FtnNetwork, list_networks
from netbbs.ftn.outbound import direct_nodes_waiting, outbound_for_node
from netbbs.ftn.transport import TLS_HANDSHAKE_SECONDS, parse_listen_hosts, server_context
from netbbs.timeutil import utc_now_iso

_logger = logging.getLogger(__name__)

PORT_CONFIG_KEY = "ftn_listen_port"
HOST_CONFIG_KEY = "ftn_listen_host"
# --- TLS (issue #1468) ---
TLS_PORT_CONFIG_KEY = "ftn_tls_port"
TLS_CERT_CONFIG_KEY = "ftn_tls_cert"
TLS_KEY_CONFIG_KEY = "ftn_tls_key"
# --- end TLS ---
CHECK_INTERVAL = 60.0
MAX_SESSIONS = 4
RECENT_SESSIONS = 20
# The most nodes with held mail one answered call prepares for (issue #1471).
MAX_HELD_NODES = 50


@dataclass(frozen=True)
class AnsweredCall:
    at: str
    peer: str  # the caller's IP address
    addresses: str
    secure: bool
    outcome: str
    tls: bool = False


@dataclass(frozen=True)
class ListenSettings:
    hosts: tuple[str, ...]  # empty: every address
    port: int
    tls_port: int | None = None
    tls_cert: str = ""
    tls_key: str = ""

    @property
    def host(self) -> str | None:
        """The first address bound, None for every address."""
        return self.hosts[0] if self.hosts else None


def _port(text: str | None) -> int | None:
    return int(text) if text and text.isdigit() and 0 < int(text) < 65536 else None


def listen_settings(db) -> ListenSettings:
    """What the listener binds. A host setting that isn't a list of IP
    addresses (only a hand-edited row can hold one) binds every address."""
    try:
        hosts = parse_listen_hosts(get_config(db, HOST_CONFIG_KEY) or "")
    except ValueError:
        _logger.warning("FTN: ftn_listen_host is not a list of IP addresses; listening on every address")
        hosts = ()
    return ListenSettings(
        hosts=hosts, port=_port(get_config(db, PORT_CONFIG_KEY)) or DEFAULT_PORT,
        tls_port=_port(get_config(db, TLS_PORT_CONFIG_KEY)),
        tls_cert=get_config(db, TLS_CERT_CONFIG_KEY) or "", tls_key=get_config(db, TLS_KEY_CONFIG_KEY) or "",
    )


class FtnListener:
    def __init__(self, lane, *, check_interval: float = CHECK_INTERVAL, batch_hook=None):
        self._lane = lane
        self._check_interval = check_interval
        self._task: asyncio.Task | None = None
        self._server: asyncio.base_events.Server | None = None
        self._tls_server: asyncio.base_events.Server | None = None
        self._bound: ListenSettings | None = None
        self._sessions: set[asyncio.Task] = set()
        self._peers: set[str] = set()
        self.recent: deque[AnsweredCall] = deque(maxlen=RECENT_SESSIONS)
        self.last_error: str | None = None
        self.tls_error: str | None = None
        # (network, BatchContext) -> files for a later batch (issue #1469).
        self.batch_hook = batch_hook

    @property
    def listening_on(self) -> tuple[str | None, int] | None:
        """`(first address or None for all, port)` of the plain listener."""
        return (self._bound.host, self._bound.port) if self._server is not None and self._bound else None

    @property
    def tls_listening_on(self) -> int | None:
        """The TLS listener's port, when it runs."""
        return self._bound.tls_port if self._tls_server is not None and self._bound else None

    @property
    def addresses(self) -> tuple[str, ...]:
        """The addresses bound, empty for every address."""
        return self._bound.hosts if self._bound else ()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="ftn-listener")

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # Sessions first: since Python 3.12 the server's wait_closed() waits
        # for every handler, so a live session would hold the shutdown.
        sessions = list(self._sessions)
        for session in sessions:
            session.cancel()
        await asyncio.gather(*sessions, return_exceptions=True)
        await self._stop_server()

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
        addresses and ports; stop otherwise."""
        networks = await self._lane.run(list_networks)
        wanted = await self._lane.run(listen_settings) if any(n.enabled and n.answers_calls for n in networks) else None
        if wanted == self._bound and (wanted is None or self._server is not None):
            if wanted is not None and wanted.tls_port is not None and self._tls_server is None:
                await self._start_tls(wanted, list(wanted.hosts) or None)  # the files may be fixed by now
            return
        await self._stop_server()
        if wanted is None:
            return
        hosts = list(wanted.hosts) or None
        try:
            self._server = await asyncio.start_server(self._accept_plain, hosts, wanted.port)
        except OSError as exc:
            self.last_error = f"cannot listen on port {wanted.port}: {exc}"
            _logger.error("FTN: %s", self.last_error)
            self._bound = None
            return
        self._bound = wanted
        self.last_error = None
        _logger.info("FTN: answering calls on port %s (%s)", wanted.port,
                     ", ".join(wanted.hosts) if wanted.hosts else "every address")
        await self._start_tls(wanted, hosts)

    async def _start_tls(self, wanted: ListenSettings, hosts: list[str] | None) -> None:
        previous, self.tls_error = self.tls_error, None
        if wanted.tls_port is None:
            return
        if not wanted.tls_cert:
            self.tls_error = "the TLS port needs a certificate file"
        elif wanted.tls_port == wanted.port:
            self.tls_error = "the TLS port must differ from the answer port"
        else:
            try:
                context = server_context(wanted.tls_cert, wanted.tls_key)
                self._tls_server = await asyncio.start_server(
                    self._accept_tls, hosts, wanted.tls_port, ssl=context,
                    ssl_handshake_timeout=TLS_HANDSHAKE_SECONDS)
            except (OSError, ssl.SSLError) as exc:
                self.tls_error = f"cannot answer TLS on port {wanted.tls_port}: {exc}"
        if self.tls_error:
            if self.tls_error != previous:  # retried every check; said once
                _logger.error("FTN: %s", self.tls_error)
        else:
            _logger.info("FTN: answering calls over TLS on port %s", wanted.tls_port)

    async def _stop_server(self) -> None:
        servers = [self._server, self._tls_server]
        self._server = self._tls_server = None
        self._bound = None
        for server in servers:
            if server is not None:
                server.close()
                with contextlib.suppress(OSError, asyncio.TimeoutError):
                    await asyncio.wait_for(server.wait_closed(), 5)

    async def _accept_plain(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await self._accept(reader, writer, tls=False)

    async def _accept_tls(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await self._accept(reader, writer, tls=True)

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, tls: bool) -> None:
        peer = writer.get_extra_info("peername")
        peer_ip = peer[0] if isinstance(peer, tuple) else "?"
        # One call per address at a time, so one caller can't take every slot.
        if len(self._sessions) >= MAX_SESSIONS or peer_ip in self._peers:
            with contextlib.suppress(OSError):
                writer.write(encode_frame(M_BSY, b"Too many sessions, call again later"))
                await writer.drain()
            writer.close()
            return
        task = asyncio.current_task()
        self._sessions.add(task)
        self._peers.add(peer_ip)
        try:
            await self._answer(reader, writer, peer_ip, tls)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- nothing else retrieves a session's failure
            self.last_error = f"answering {peer_ip} failed: {exc}"
            self._record(peer_ip, "", False, f"failed: {exc}", tls)
            _logger.exception("FTN: answering %s failed", peer_ip)
        finally:
            self._sessions.discard(task)
            self._peers.discard(peer_ip)
            writer.close()
            with contextlib.suppress(OSError, asyncio.TimeoutError, ssl.SSLError):
                await asyncio.wait_for(writer.wait_closed(), 5)

    async def _answer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, peer_ip: str,
                      tls: bool) -> None:
        networks = [n for n in await self._lane.run(list_networks) if n.enabled and n.answers_calls]
        if not networks:
            return
        system = await self._lane.run(system_info)
        # Prepared before the session, so the callbacks inside it need no
        # database (`netbbs.ftn.outbound.outbound_for_node`; a file is read
        # only as it is sent). Only a caller proving it is a network's uplink
        # gets what waits for the uplink. A hub this node calls at the same
        # moment may get the same messages twice; its dupe check absorbs that.
        # A caller that only presents a node's address gets that node's held
        # mail, and only that, on a network that allows it (issue #1471):
        # anyone can present an address.
        prepared: list = []
        uplinks = {}
        held: dict[tuple[int, str], object] = {}
        try:
            for network in networks:
                uplinks[network.id] = await outbound_for_node(self._lane, network, network.uplink_address,
                                                              uplink=True)
                prepared.append(uplinks[network.id])
                if not network.give_held_unproven:
                    continue
                for node in (await self._lane.run(direct_nodes_waiting, network, hold_only=True))[:MAX_HELD_NODES]:
                    held[(network.id, node.four_d)] = await outbound_for_node(self._lane, network, node,
                                                                              uplink=False, hold_only=True)
                    prepared.append(held[(network.id, node.four_d)])
            state: dict[str, object] = {}

            def password_for(addresses: list[FtnAddress]) -> str | None:
                network = _uplink_network(networks, addresses)
                state["network"] = network
                return network.session_password if network is not None else None

            def outgoing_for(addresses: list[FtnAddress], secure: bool) -> list[OutgoingFile]:
                network = state.get("network")
                if secure and network is not None:
                    state["outbound"] = uplinks[network.id]
                    return list(uplinks[network.id].files)
                if secure:
                    return []
                for address in addresses:
                    boss = f"{address.zone}:{address.net}/{address.node}"
                    for candidate in networks:
                        if (candidate.id, boss) in held:
                            state["outbound"] = held[(candidate.id, boss)]
                            return list(held[(candidate.id, boss)].files)
                return []

            hook = self.batch_hook

            def next_batch(context: BatchContext):
                if hook is None:
                    return []
                return hook(_session_network(networks, state, context.result), context)

            outcome_prefix = ""
            try:
                result = await run_session(
                    reader, writer, originating=False, our_addresses=[n.our_address for n in networks],
                    system=system, password_for=password_for, outgoing_for=outgoing_for, next_batch=next_batch,
                )
            except (OSError, asyncio.TimeoutError, BinkpError) as exc:
                partial = getattr(exc, "result", None)
                if partial is None or not (partial.received or partial.sent):
                    self._record(peer_ip, "", False, f"failed: {exc}", tls)
                    _logger.warning("FTN: call from %s failed: %s", peer_ip, exc)
                    return
                # Established and something moved: keep it, then say it failed.
                result = partial
                outcome_prefix = f"failed: {exc}; before that: "
                _logger.warning("FTN: call from %s failed part way: %s", peer_ip, exc)
            network = _session_network(networks, state, result)
            outbound = state.get("outbound")
            sent, files_sent = await outbound.settle(self._lane, result.sent) if outbound is not None else (0, 0)
        finally:
            for item in prepared:
                item.close()
        tossed = await toss_received(self._lane, network, result)
        outcome = outcome_prefix + summary(sent, len(result.received), tossed, files_sent=files_sent)
        addresses = " ".join(str(a) for a in result.remote_addresses)
        self._record(peer_ip, addresses, result.secure, outcome, tls)
        _logger.info("FTN %s: answered %s (%s, %s%s) -- %s", network.name, addresses, peer_ip,
                     "secure" if result.secure else "non-secure", ", TLS" if tls else "", outcome)

    def _record(self, peer: str, addresses: str, secure: bool, outcome: str, tls: bool = False) -> None:
        self.recent.append(AnsweredCall(utc_now_iso(), peer, addresses, secure, outcome, tls))


def _session_network(networks: list[FtnNetwork], state: dict, result: SessionResult) -> FtnNetwork:
    """The network a session's files belong to: the proven uplink's, else
    the one an unproven caller is filed under."""
    network = state.get("network")
    if result.secure and network is not None:
        return network
    return _network_for_caller(networks, result.remote_addresses)


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
