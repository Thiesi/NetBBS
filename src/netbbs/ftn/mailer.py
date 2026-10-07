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

A call sends one packet of up to `MAX_MESSAGES_PER_PACKET` waiting
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
from dataclasses import dataclass

from netbbs import __version__
from netbbs.auth.users import is_usable_sysop, list_users
from netbbs.config import get_node_display_name
from netbbs.ftn import FtnFormatError
from netbbs.ftn.binkp import BinkpError, OutgoingFile, ReceivedFile, SessionResult, SystemInfo, run_session
from netbbs.ftn.bundle import archive_kind, extract_packets, packet_name
from netbbs.ftn.networks import FtnNetwork, list_networks
from netbbs.ftn.packet import PacketHeader, build_packet_from_packed
from netbbs.ftn.queue import (
    count_pending_outbound,
    hold_inbound,
    mark_outbound_sent,
    pending_outbound,
    prune_seen_msgids,
    prune_sent_outbound,
)
from netbbs.ftn.tosser import TossResult, toss_packet

_logger = logging.getLogger(__name__)

CHECK_INTERVAL = 60.0
MIN_CALL_GAP = 300.0
CONNECT_TIMEOUT = 30.0
MAX_MESSAGES_PER_PACKET = 500
MAINTENANCE_INTERVAL = 24 * 3600.0


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
            pending = await self._lane.run(count_pending_outbound, network.id)
            if self._due(network, pending, now):
                await self.poll(network)

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
        messages = await self._lane.run(pending_outbound, network.id, limit=MAX_MESSAGES_PER_PACKET)
        outgoing: list[OutgoingFile] = []
        if messages:
            header = PacketHeader(orig=network.our_address, dest=network.uplink_address,
                                  created=datetime.datetime.now(), password=network.packet_password)
            outgoing.append(OutgoingFile(packet_name(secrets.randbits(32)),
                                         build_packet_from_packed(header, [m.packed for m in messages])))
        system = await self._lane.run(_system_info)
        try:
            result = await self._session(network, system, outgoing)
        except (OSError, asyncio.TimeoutError, BinkpError) as exc:
            status.failures += 1
            status.last_error = str(exc) or type(exc).__name__
            _logger.warning("FTN %s: call to %s:%s failed: %s", network.name, network.uplink_host,
                            network.uplink_port, status.last_error)
            return status
        if result.plaintext_password:
            _logger.warning("FTN %s: the uplink offered no CRAM-MD5, so the session password crossed unhashed",
                            network.name)
        sent = len(messages) if outgoing and outgoing[0].name in result.sent else 0
        if sent:
            await self._lane.run(mark_outbound_sent, [m.id for m in messages])
        tossed = TossResult()
        for received in result.received:
            await self._toss_received(network, received, result, tossed)
        status.failures = 0
        status.last_error = None
        status.last_success = status.last_attempt
        status.last_success_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        status.last_summary = (f"sent {sent}, received {len(result.received)} files: {tossed.posts} posts, "
                               f"{tossed.netmail} netmail, {tossed.duplicates} duplicates, {tossed.held} held")
        _logger.info("FTN %s: call to %s done -- %s", network.name, network.uplink_host, status.last_summary)
        return status

    async def _session(self, network: FtnNetwork, system: SystemInfo, outgoing: list[OutgoingFile]) -> SessionResult:
        reader, writer = await asyncio.wait_for(
            self._connect(network.uplink_host, network.uplink_port), CONNECT_TIMEOUT)
        try:
            return await run_session(
                reader, writer, originating=True, our_addresses=[network.our_address], system=system,
                password=network.session_password, outgoing=outgoing, expected_remote=network.uplink_address,
            )
        finally:
            writer.close()
            with contextlib.suppress(OSError, asyncio.TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 5)

    async def _toss_received(self, network: FtnNetwork, received: ReceivedFile, result: SessionResult,
                             tossed: TossResult) -> None:
        remote = str(result.remote_addresses[0]) if result.remote_addresses else "?"
        kind = archive_kind(received.data)
        if kind == "pkt":
            packets = [(received.name, received.data)]
        elif kind == "zip":
            try:
                packets = await asyncio.to_thread(extract_packets, received.data)
            except FtnFormatError as exc:
                await self._hold(network, remote, received, f"bundle could not be unpacked: {exc}", tossed)
                return
        else:
            await self._hold(network, remote, received, f"not a packet or ZIP bundle ({kind or 'unknown'})", tossed)
            return
        for name, data in packets:
            one = await self._lane.run(toss_packet, network, data, secure=result.secure,
                                       remote_address=remote, file_name=name)
            tossed.posts += one.posts
            tossed.netmail += one.netmail
            tossed.duplicates += one.duplicates
            tossed.held += one.held

    async def _hold(self, network, remote, received: ReceivedFile, reason: str, tossed: TossResult) -> None:
        kept = await self._lane.run(_hold, network.id, remote, received.name, received.data, reason)
        if kept:
            tossed.held += 1
        else:
            _logger.error("FTN %s: %s from %s was lost (held store full): %s", network.name, received.name,
                          remote, reason)


def _hold(db, network_id, remote, name, data, reason) -> bool:
    return hold_inbound(db, network_id=network_id, remote_address=remote, file_name=name, content=data, reason=reason)


def _system_info(db) -> SystemInfo:
    sysops = sorted((user for user in list_users(db) if is_usable_sysop(user)), key=lambda user: user.id)
    return SystemInfo(name=get_node_display_name(db), sysop=sysops[0].username if sysops else "SysOp",
                      version=f"NetBBS/{__version__}")
