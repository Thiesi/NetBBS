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

**Transport** (issues #1467, #1468, #1469; `netbbs.ftn.transport`). A call
dials the host over IPv6 or IPv4, whichever answers first. A network with
TLS on calls its uplink over TLS: pinned, the first call that completes
learns the hub's certificate and a later call to another one is refused;
verified, the system's certificate authorities and the host name decide.
Direct netmail calls stay plain: a nodelist says nothing of TLS. A call that
fails part way still keeps what moved before it failed: files received
whole (each confirmed to the remote, which has let go of it) are tossed,
and messages in a packet the remote confirmed are marked sent.

**Later batches.** `batch_hook(network, context)` -- None until a later
stage sets one -- is asked for more files before each batch after the
first of a binkp/1.1 multi-batch session (`netbbs.ftn.binkp.BatchContext`):
the place where file requests received in the session get their answers.
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
from netbbs.ftn.binkp import BatchContext, BinkpError, OutgoingFile, SessionResult, SystemInfo, run_session
from netbbs.ftn.bundle import packet_name
from netbbs.ftn.exchange import MAX_MESSAGES_PER_PACKET, outbound_packet, summary, system_info, toss_received
from netbbs.ftn.networks import FtnNetwork, list_networks, set_uplink_tls_pin
from netbbs.ftn.nodelist import direct_route
from netbbs.ftn.packet import PacketHeader, build_packet_from_packed
from netbbs.ftn.queue import (
    count_pending_outbound,
    expire_unpaired,
    mark_outbound_sent,
    pending_outbound,
    prune_seen_msgids,
    prune_sent_outbound,
    reroute_outbound_to_uplink,
)
from netbbs.ftn.transport import TLS_OFF, TLS_PINNED, dial, format_host_port, show_fingerprint

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
    def __init__(self, lane, *, connect=None, clock=time.monotonic, check_interval: float = CHECK_INTERVAL,
                 batch_hook=None):
        self._lane = lane
        self._connect = connect  # None: asyncio.open_connection
        # (network, BatchContext) -> files for a later batch (issue #1469).
        self.batch_hook = batch_hook
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
            await self._lane.run(expire_unpaired)  # file-echo halves whose partner never came (issue #1462)
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
        # The same bound per call as the uplink's packet; the rest goes next time.
        messages = await self._lane.run(pending_outbound, network.id, route="direct", limit=MAX_MESSAGES_PER_PACKET)
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
            ids = [m.id for m in waiting]
            try:
                result, _ = await self._call(host, port, network, system, [packet], password="", expected=node)
            except (OSError, asyncio.TimeoutError, BinkpError) as exc:
                status.failures += 1
                status.last_error = str(exc) or type(exc).__name__
                _logger.warning("FTN %s: direct call to %s (%s) failed: %s", network.name, node.four_d,
                                format_host_port(host, port), status.last_error)
                partial = getattr(exc, "result", None)
                if partial is not None and (partial.received or partial.sent):
                    await self._settle(network, replace(partial, secure=False), packet, ids)
                    if packet.name in partial.sent:
                        status.failures = 0  # delivered before the call broke
                        continue
                if status.failures >= DIRECT_ATTEMPTS:
                    to_uplink.extend(ids)
                    status.failures = 0
                continue
            status.failures = 0
            status.last_error = None
            # What a node hands over on a call this node made unasked is held:
            # nothing proved who it is.
            await self._settle(network, replace(result, secure=False), packet, ids)
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
        where = format_host_port(network.uplink_host, network.uplink_port)
        try:
            result, fingerprint = await self._call(
                network.uplink_host, network.uplink_port, network, system, outgoing,
                password=network.session_password, expected=network.uplink_address,
                tls=network.uplink_tls, pin=network.uplink_tls_pin)
        except (OSError, asyncio.TimeoutError, BinkpError) as exc:
            status.failures += 1
            status.last_error = str(exc) or type(exc).__name__
            partial = getattr(exc, "result", None)
            if partial is not None and (partial.received or partial.sent):
                moved = await self._settle(network, partial, packet, message_ids)
                status.last_error += f" (before that: {moved})"
            _logger.warning("FTN %s: call to %s failed: %s", network.name, where, status.last_error)
            return status
        if result.plaintext_password:
            _logger.warning("FTN %s: the uplink offered no CRAM-MD5, so the session password crossed unhashed",
                            network.name)
        # --- transport (issue #1468): trust on first use ---
        if fingerprint and network.uplink_tls == TLS_PINNED and not network.uplink_tls_pin:
            if await self._lane.run(set_uplink_tls_pin, network.id, fingerprint):
                _logger.info("FTN %s: pinned the uplink's TLS certificate %s", network.name,
                             show_fingerprint(fingerprint))
        # --- end transport ---
        status.last_summary = await self._settle(network, result, packet, message_ids)
        status.failures = 0
        status.last_error = None
        status.last_success = status.last_attempt
        status.last_success_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        _logger.info("FTN %s: call to %s%s done -- %s", network.name, where,
                     " over TLS" if network.uplink_tls != TLS_OFF else "", status.last_summary)
        return status

    async def _settle(self, network: FtnNetwork, result: SessionResult, packet: OutgoingFile | None,
                      message_ids: list[int]) -> str:
        """Mark the packet's messages sent if the remote confirmed it, toss
        what was received; the summary line."""
        sent = len(message_ids) if packet is not None and packet.name in result.sent else 0
        if sent:
            await self._lane.run(mark_outbound_sent, message_ids)
        tossed = await toss_received(self._lane, network, result)
        return summary(sent, len(result.received), tossed)

    async def _call(self, host: str, port: int, network: FtnNetwork, system: SystemInfo,
                    outgoing: list[OutgoingFile], *, password: str, expected: FtnAddress, tls: str = TLS_OFF,
                    pin: str = "") -> tuple[SessionResult, str | None]:
        """One session; `(result, the hub's certificate fingerprint under TLS)`."""
        reader, writer, fingerprint = await dial(host, port, tls=tls, pin=pin, connect=self._connect,
                                                 timeout=CONNECT_TIMEOUT)
        hook = self.batch_hook

        def next_batch(context: BatchContext):
            return hook(network, context) if hook is not None else []

        try:
            return await run_session(
                reader, writer, originating=True, our_addresses=[network.our_address], system=system,
                password=password, outgoing=outgoing, next_batch=next_batch, expected_remote=expected,
            ), fingerprint
        finally:
            writer.close()
            with contextlib.suppress(OSError, asyncio.TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 5)
