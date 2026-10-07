"""
The mailer: this node's calls to its FTN uplinks (design doc §6.8).

One `FtnMailer` per node, started and closed by `netbbs.__main__` like the
MRC bridge. Every `CHECK_INTERVAL` it looks at each enabled network and
calls the uplink when

- the network's poll interval has passed since the last successful call,
  or
- messages are waiting and the last call was at least `MIN_CALL_GAP` ago,
  so a new post goes out within minutes without hammering the hub (fsxNet
  blocks a node that calls several times a minute).

A call sends one packet of up to `exchange.MAX_MESSAGES_PER_PACKET` waiting
messages, receives whatever the hub holds, and tosses it: bare packets
directly, ZIP bundles unpacked first; any other file is held for the
SysOp. A message is marked sent only when the hub confirms the packet
holding it (`M_GOT`), so a call cut off part way loses nothing and sends
again next time; the hub's dupe check absorbs a packet it did receive but
could not confirm.

A failed call is retried after 1, 2, 4 ... minutes, never later than the
poll interval. Settings are read on every check, so a SysOp's change takes
effect without a restart. Per-network results are kept in `status` for
the console.

Netmail routed direct (Decision 5, `netbbs.ftn.nodelist`) is delivered by
calling the destination node where the nodelist says it answers, with no
session password; what that node hands over is held, since nothing proved
who it is. After `DIRECT_ATTEMPTS` failed calls the netmail goes via the
uplink instead.

Once a day it prunes the dupe history and the sent-message record
(`netbbs.ftn.queue`).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import secrets
import time
from dataclasses import dataclass, replace

from netbbs.ftn.address import FtnAddress, parse_address
from netbbs.ftn.binkp import BinkpError, OutgoingFile, SessionResult, SystemInfo, run_session
from netbbs.ftn.bundle import packet_name
from netbbs.ftn.exchange import outbound_packet, summary, system_info, toss_received
from netbbs.ftn.networks import FtnNetwork, list_networks
from netbbs.ftn.nodelist import direct_route
from netbbs.ftn.packet import PacketHeader, build_packet_from_packed
from netbbs.ftn.queue import (
    count_pending_outbound,
    mark_outbound_sent,
    pending_outbound,
    prune_seen_msgids,
    prune_sent_outbound,
    reroute_outbound_to_uplink,
)

_logger = logging.getLogger(__name__)

CHECK_INTERVAL = 60.0
MIN_CALL_GAP = 300.0
CONNECT_TIMEOUT = 30.0
MAINTENANCE_INTERVAL = 24 * 3600.0
DIRECT_ATTEMPTS = 3


@dataclass
class PollStatus:
    last_attempt: float | None = None  # monotonic seconds
    last_success: float | None = None
    last_success_at: str | None = None  # wall clock, for display
    last_error: str | None = None
    failures: int = 0
    last_summary: str = ""


class FtnMailer:
    def __init__(self, lane, *, connect=None, clock=time.monotonic, check_interval: float = CHECK_INTERVAL):
        self._lane = lane
        self._connect = connect or asyncio.open_connection
        self._clock = clock
        self._check_interval = check_interval
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._last_maintenance: float | None = None
        self.status: dict[int, PollStatus] = {}
        # By network and the called node's 4D address: numbering is per network.
        self.direct_status: dict[tuple[int, str], PollStatus] = {}

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="ftn-mailer")

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def kick(self) -> None:
        """Check now rather than at the next interval (the console's
        "poll now")."""
        self._wake.set()

    async def _run(self) -> None:
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- the loop must outlive one bad pass
                _logger.exception("FTN mailer pass failed")
            self._wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self._check_interval)

    async def check_once(self) -> None:
        now = self._clock()
        if self._last_maintenance is None or now - self._last_maintenance >= MAINTENANCE_INTERVAL:
            self._last_maintenance = now
            await self._lane.run(prune_seen_msgids)
            await self._lane.run(prune_sent_outbound)
        for network in await self._lane.run(list_networks):
            if not network.enabled or not network.uplink_host:
                continue
            pending = await self._lane.run(count_pending_outbound, network.id, route="uplink")
            if self._due(network, pending, now):
                await self.poll(network)
            await self.deliver_direct(network)

    async def deliver_direct(self, network: FtnNetwork) -> None:
        """Call each node that direct netmail waits for (Decision 5), at most
        once per `MIN_CALL_GAP`. After `DIRECT_ATTEMPTS` failed calls, or
        once the nodelist no longer lists where the node answers, its
        netmail goes via the uplink instead."""
        messages = await self._lane.run(pending_outbound, network.id, route="direct")
        groups: dict[tuple[FtnAddress, str, int], list] = {}
        to_uplink = []
        for message in messages:
            target = await self._lane.run(direct_route, network.id, parse_address(message.destination))
            if target is None:
                to_uplink.append(message.id)
            else:
                groups.setdefault(target, []).append(message)
        now = self._clock()
        for (node, host, port), waiting in groups.items():
            status = self.direct_status.setdefault((network.id, node.four_d), PollStatus())
            if status.last_attempt is not None and now - status.last_attempt < MIN_CALL_GAP:
                continue
            status.last_attempt = now
            header = PacketHeader(orig=network.our_address, dest=node, created=datetime.datetime.now())
            packet = OutgoingFile(packet_name(secrets.randbits(32)),
                                  build_packet_from_packed(header, [m.packed for m in waiting]))
            system = await self._lane.run(system_info)
            try:
                result = await self._call(host, port, network, system, [packet], password="", expected=node)
            except (OSError, asyncio.TimeoutError, BinkpError) as exc:
                status.failures += 1
                status.last_error = str(exc) or type(exc).__name__
                _logger.warning("FTN %s: direct call to %s (%s:%s) failed: %s", network.name, node.four_d, host,
                                port, status.last_error)
                if status.failures >= DIRECT_ATTEMPTS:
                    to_uplink.extend(m.id for m in waiting)
                    status.failures = 0
                continue
            if packet.name in result.sent:
                await self._lane.run(mark_outbound_sent, [m.id for m in waiting])
            status.failures = 0
            status.last_error = None
            # What a node hands over on a call this node made unasked is held:
            # nothing proved who it is.
            await toss_received(self._lane, network, replace(result, secure=False))
        if to_uplink:
            await self._lane.run(reroute_outbound_to_uplink, to_uplink)
            _logger.info("FTN %s: %d netmail could not go direct and goes via the uplink", network.name,
                         len(to_uplink))

    def _due(self, network: FtnNetwork, pending: int, now: float) -> bool:
        status = self.status.get(network.id)
        if status is None or status.last_attempt is None:
            return True
        interval = network.poll_minutes * 60.0
        if status.failures:
            retry = min(60.0 * 2 ** (status.failures - 1), interval)
            return now - status.last_attempt >= retry
        if now - (status.last_success or status.last_attempt) >= interval:
            return True
        return pending > 0 and now - status.last_attempt >= MIN_CALL_GAP

    async def poll(self, network: FtnNetwork) -> PollStatus:
        """Call the uplink once; returns the network's updated status. Any
        failure, expected or not, is a failed call in the status the console
        shows, never a stale success."""
        status = self.status.setdefault(network.id, PollStatus())
        try:
            return await self._poll(network, status)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status.failures += 1
            status.last_error = f"internal error: {exc}"
            _logger.exception("FTN %s: call to the uplink failed unexpectedly", network.name)
            return status

    async def _poll(self, network: FtnNetwork, status: PollStatus) -> PollStatus:
        status.last_attempt = self._clock()
        packet, message_ids = await outbound_packet(self._lane, network)
        outgoing = [packet] if packet is not None else []
        system = await self._lane.run(system_info)
        try:
            result = await self._call(network.uplink_host, network.uplink_port, network, system, outgoing,
                                      password=network.session_password, expected=network.uplink_address)
        except (OSError, asyncio.TimeoutError, BinkpError) as exc:
            status.failures += 1
            status.last_error = str(exc) or type(exc).__name__
            _logger.warning("FTN %s: call to %s:%s failed: %s", network.name, network.uplink_host,
                            network.uplink_port, status.last_error)
            return status
        if result.plaintext_password:
            _logger.warning("FTN %s: the uplink offered no CRAM-MD5, so the session password crossed unhashed",
                            network.name)
        sent = len(message_ids) if packet is not None and packet.name in result.sent else 0
        if sent:
            await self._lane.run(mark_outbound_sent, message_ids)
        tossed = await toss_received(self._lane, network, result)
        status.failures = 0
        status.last_error = None
        status.last_success = status.last_attempt
        status.last_success_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        status.last_summary = summary(sent, len(result.received), tossed)
        _logger.info("FTN %s: call to %s done -- %s", network.name, network.uplink_host, status.last_summary)
        return status

    async def _call(self, host: str, port: int, network: FtnNetwork, system: SystemInfo,
                    outgoing: list[OutgoingFile], *, password: str, expected: FtnAddress) -> SessionResult:
        reader, writer = await asyncio.wait_for(self._connect(host, port), CONNECT_TIMEOUT)
        try:
            return await run_session(
                reader, writer, originating=True, our_addresses=[network.our_address], system=system,
                password=password, outgoing=outgoing, expected_remote=expected,
            )
        finally:
            writer.close()
            with contextlib.suppress(OSError, asyncio.TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 5)
