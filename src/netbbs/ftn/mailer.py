"""
The mailer: this node's calls to its FTN uplinks (design doc §6.8).

One `FtnMailer` per node, started and closed by `netbbs.__main__` like the
MRC bridge. Every `CHECK_INTERVAL` it looks at each enabled network and
calls the uplink when

- the network's poll interval has passed since the last successful call,
  or
- messages or files are waiting and the last call was at least
  `MIN_CALL_GAP` ago, so a new post goes out within minutes without
  hammering the hub (fsxNet blocks a node that calls several times a
  minute); crash mail for the hub itself goes at the next check. Held mail
  never makes it call (issue #1471).

A call hands over what `netbbs.ftn.outbound.outbound_for_node` gives the
node -- one packet of up to `outbound.MAX_MESSAGES_PER_PACKET` waiting
messages, and the files queued for it -- receives whatever the hub holds, and tosses it: bare packets
directly, ZIP bundles unpacked first; any other file is held for the
SysOp. A message is marked sent only when the hub confirms the packet
holding it (`M_GOT`), and a file only when the hub confirms it, so a call cut off part way loses nothing and sends
again next time; the hub's dupe check absorbs a packet it did receive but
could not confirm.

A failed call is retried after 1, 2, 4 ... minutes, never later than the
poll interval. Settings are read on every check, so a SysOp's change takes
effect without a restart. Per-network results are kept in `status` for
the console.

Netmail routed direct (Decision 5, `netbbs.ftn.nodelist`) is delivered by
calling the destination node where the nodelist says it answers, with no
session password; what that node hands over is held, since nothing proved
who it is. After `DIRECT_ATTEMPTS` failed calls normal netmail goes via
the uplink instead; crash netmail never does, and is dropped and reported
to its sender after `outbound.CRASH_ATTEMPTS` (issue #1471).

Once a day it prunes the dupe history and the sent-message record
(`netbbs.ftn.queue`), drops held mail older than `outbound.HOLD_DAYS` and
forgets attached files that never arrived (`netbbs.ftn.attach`).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import time
from dataclasses import dataclass, replace

from netbbs.ftn.address import FtnAddress
from netbbs.ftn.attach import expire_attach_waits
from netbbs.ftn.binkp import BinkpError, OutgoingFile, SessionResult, SystemInfo, run_session
from netbbs.ftn.exchange import summary, system_info, toss_received
from netbbs.ftn.networks import FtnNetwork, list_networks
from netbbs.ftn.nodelist import direct_route
from netbbs.ftn.outbound import (
    SessionOutbound,
    count_callable,
    direct_nodes_waiting,
    expire_held,
    fail_crash_without_route,
    note_failed_crash,
    outbound_for_node,
    reroute_files_to_uplink,
    waiting_for_node,
)
from netbbs.ftn.queue import (
    expire_unpaired,
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
            await self._lane.run(expire_unpaired)  # file-echo halves whose partner never came (issue #1462)
            await self._lane.run(expire_attach_waits)  # attached files that never came (issue #1470)
            await self._lane.run(expire_held)  # held mail whose node never called (issue #1471)
        for network in await self._lane.run(list_networks):
            if not network.enabled or not network.uplink_host:
                continue
            # Held mail never makes this node call (issue #1471).
            pending, crash = await self._lane.run(count_callable, network.id, route="uplink")
            if self._due(network, pending, now, crash=crash):
                await self.poll(network)
            await self.deliver_direct(network)

    async def deliver_direct(self, network: FtnNetwork) -> None:
        """Call each node that direct netmail or files wait for (Decision 5),
        at most once per `MIN_CALL_GAP`; the session also hands over what is
        held for that node. After `DIRECT_ATTEMPTS` failed calls, or once
        the nodelist no longer lists where the node answers, normal mail
        goes via the uplink instead. Crash mail never does: it is dropped
        and its sender told after `outbound.CRASH_ATTEMPTS` failed calls,
        or at once when the node is no longer listed (issue #1471)."""
        now = self._clock()
        for node in await self._lane.run(direct_nodes_waiting, network, include_hold=False):
            target = await self._lane.run(direct_route, network.id, node)
            if target is None:
                await self._lane.run(_unroutable, network, node)
                continue
            _node, host, port = target
            status = self.direct_status.setdefault((network.id, node.four_d), PollStatus())
            if status.last_attempt is not None and now - status.last_attempt < MIN_CALL_GAP:
                continue
            status.last_attempt = now
            outbound = await outbound_for_node(self._lane, network, node, uplink=False)
            try:
                system = await self._lane.run(system_info)
                try:
                    result = await self._call(host, port, network, system, outbound.files, password="",
                                              expected=node)
                except (OSError, asyncio.TimeoutError, BinkpError) as exc:
                    status.failures += 1
                    status.last_error = str(exc) or type(exc).__name__
                    _logger.warning("FTN %s: direct call to %s (%s:%s) failed: %s", network.name, node.four_d,
                                    host, port, status.last_error)
                    await self._lane.run(note_failed_crash, network.id, outbound.message_ids,
                                         [row.id for row in outbound.file_rows.values()])
                    if status.failures >= DIRECT_ATTEMPTS:
                        await self._lane.run(_to_uplink, outbound)
                        status.failures = 0
                    continue
                await outbound.settle(self._lane, result.sent)
            finally:
                outbound.close()
            status.failures = 0
            status.last_error = None
            # What a node hands over on a call this node made unasked is held:
            # nothing proved who it is.
            await toss_received(self._lane, network, replace(result, secure=False))

    def _due(self, network: FtnNetwork, pending: int, now: float, *, crash: int = 0) -> bool:
        status = self.status.get(network.id)
        if status is None or status.last_attempt is None:
            return True
        interval = network.poll_minutes * 60.0
        if status.failures:
            retry = min(60.0 * 2 ** (status.failures - 1), interval)
            return now - status.last_attempt >= retry
        if now - (status.last_success or status.last_attempt) >= interval:
            return True
        # Crash mail for the uplink goes at the next check, not after the gap.
        return (crash > 0 and now - status.last_attempt >= self._check_interval) or (
            pending > 0 and now - status.last_attempt >= MIN_CALL_GAP)

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
        outbound = await outbound_for_node(self._lane, network, network.uplink_address, uplink=True)
        try:
            system = await self._lane.run(system_info)
            try:
                result = await self._call(network.uplink_host, network.uplink_port, network, system, outbound.files,
                                          password=network.session_password, expected=network.uplink_address)
            except (OSError, asyncio.TimeoutError, BinkpError) as exc:
                status.failures += 1
                status.last_error = str(exc) or type(exc).__name__
                _logger.warning("FTN %s: call to %s:%s failed: %s", network.name, network.uplink_host,
                                network.uplink_port, status.last_error)
                # Crash mail for the uplink itself is reported once its calls keep failing (issue #1471).
                await self._lane.run(note_failed_crash, network.id, outbound.message_ids,
                                     [row.id for row in outbound.file_rows.values()])
                return status
            sent, files_sent = await outbound.settle(self._lane, result.sent)
        finally:
            outbound.close()
        if result.plaintext_password:
            _logger.warning("FTN %s: the uplink offered no CRAM-MD5, so the session password crossed unhashed",
                            network.name)
        tossed = await toss_received(self._lane, network, result)
        status.failures = 0
        status.last_error = None
        status.last_success = status.last_attempt
        status.last_success_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        status.last_summary = summary(sent, len(result.received), tossed, files_sent=files_sent)
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


def _unroutable(db, network: FtnNetwork, node: FtnAddress) -> None:
    """Mail for a node the nodelist no longer lists as answering BinkP:
    normal mail goes via the uplink, crash mail is dropped and reported."""
    messages, files = waiting_for_node(db, network, node, uplink=False)
    message_ids, file_ids = [m.id for m in messages], [f.id for f in files]
    reroute_outbound_to_uplink(db, message_ids)
    reroute_files_to_uplink(db, file_ids)
    fail_crash_without_route(db, network.id, message_ids, file_ids)
    _logger.info("FTN %s: %s is not listed as answering BinkP; its normal mail goes via the uplink", network.name,
                 node.four_d)


def _to_uplink(db, outbound: SessionOutbound) -> None:
    """Normal direct mail whose calls kept failing goes via the uplink."""
    reroute_outbound_to_uplink(db, outbound.message_ids)
    reroute_files_to_uplink(db, [row.id for row in outbound.file_rows.values()])
