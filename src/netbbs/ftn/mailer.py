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

**Polled downlinks** (issue #1479, `netbbs.ftn.links`). A downlink whose
calls are `poll` is called on the host it names, under its own session
password, when mail waits for it -- at most once per `MIN_CALL_GAP`, and
after a failed call again after 1, 2, 4 ... minutes. What it hands over is
tossed as the downlink's. Its mail never goes via the uplink: the uplink
would only route it back here. Points, and downlinks that call in, are
never called. Each call is recorded on the link for the console.

Once a day it prunes the dupe history and the sent-message record
(`netbbs.ftn.queue`), drops held mail older than `outbound.HOLD_DAYS` (and
a point's or downlink's mail it never collected), forgets attached files
that never arrived (`netbbs.ftn.attach`) and a point's echomail whose post
was never approved (`netbbs.ftn.links`).

**Transport** (issues #1467, #1468, #1469; `netbbs.ftn.transport`). A call
dials the host over IPv6 or IPv4, whichever answers first. A network with
TLS on calls its uplink over TLS: pinned, the first call that completes
learns the hub's certificate and a later call to another one is refused;
verified, the system's certificate authorities and the host name decide.
Direct netmail calls stay plain: a nodelist says nothing of TLS. A call that
fails part way still keeps what moved before it failed: files received
whole (each confirmed to the remote, which has let go of it) are tossed,
and messages in a packet the remote confirmed are marked sent.

**Later batches.** `batch_hook(network, context)` is asked for more files
before each batch after the first of a binkp/1.1 multi-batch session
(`netbbs.ftn.binkp.BatchContext`): the node sets it to
`netbbs.ftn.freq.FreqAnswerer`, which answers the file requests received in
the session (issue #1475).
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
from netbbs.ftn.binkp import BatchContext, BinkpError, OutgoingFile, SessionResult, SystemInfo, run_session
from netbbs.ftn.exchange import summary, system_info, toss_received
from netbbs.ftn.links import get_link, prune_point_waiting, record_link_call
from netbbs.ftn.networks import FtnNetwork, list_networks, set_uplink_tls_pin
from netbbs.ftn.nodelist import direct_route
from netbbs.ftn.outbound import (
    SessionOutbound,
    count_callable,
    direct_nodes_waiting,
    expire_held,
    fail_crash_without_route,
    links_with_callable,
    note_failed_crash,
    outbound_for_link,
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
from netbbs.ftn.transport import TLS_OFF, TLS_PINNED, dial, format_host_port, show_fingerprint

_logger = logging.getLogger(__name__)

CHECK_INTERVAL = 60.0
MIN_CALL_GAP = 300.0
CONNECT_TIMEOUT = 30.0
MAINTENANCE_INTERVAL = 24 * 3600.0
DIRECT_ATTEMPTS = 3


def retry_after(failures: int, ceiling: float) -> float:
    """Seconds before the next try after `failures` failed calls in a row:
    1, 2, 4 ... minutes, never more than `ceiling`. The exponent is capped,
    so a node unreachable for weeks never overflows the float."""
    return min(60.0 * 2 ** min(max(failures - 1, 0), 16), ceiling)


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
        self.link_status: dict[int, PollStatus] = {}  # polled downlinks, by link id (issue #1479)

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
            await self._lane.run(prune_point_waiting)  # a point's posts never approved (issue #1478)
        for network in await self._lane.run(list_networks):
            if not network.enabled or not network.uplink_host:
                continue
            # Held mail never makes this node call (issue #1471).
            pending, crash = await self._lane.run(count_callable, network.id, route="uplink")
            if self._due(network, pending, now, crash=crash):
                await self.poll(network)
            await self.deliver_direct(network)
            await self.deliver_links(network)

    async def deliver_links(self, network: FtnNetwork) -> None:
        """Call each polled downlink mail waits for (issue #1479), under its
        own session password; see the module docstring."""
        now = self._clock()
        for link_id in await self._lane.run(links_with_callable, network.id):
            link = await self._lane.run(get_link, link_id)
            if link is None or not link.enabled or not link.polled:
                continue  # it calls in for its mail
            status = self.link_status.setdefault(link.id, PollStatus())
            if status.last_attempt is not None:
                gap = retry_after(status.failures, MIN_CALL_GAP) if status.failures else MIN_CALL_GAP
                if now - status.last_attempt < gap:
                    continue
            await self.call_link(network, link)

    async def call_link(self, network: FtnNetwork, link) -> PollStatus:
        """Call one downlink once; its updated status."""
        status = self.link_status.setdefault(link.id, PollStatus())
        status.last_attempt = self._clock()
        outbound = await outbound_for_link(self._lane, network, link)
        where = format_host_port(link.host, link.port)
        try:
            system = await self._lane.run(system_info)
            try:
                result, _ = await self._call(link.host, link.port, network, system, outbound.files,
                                             password=link.session_password, expected=link.address)
            except (OSError, asyncio.TimeoutError, BinkpError) as exc:
                status.failures += 1
                status.last_error = str(exc) or type(exc).__name__
                partial = getattr(exc, "result", None)
                if partial is not None and (partial.received or partial.sent):
                    moved = await self._settle(network, partial, outbound, link=link)
                    status.last_error += f" (before that: {moved})"
                _logger.warning("FTN %s: call to downlink %s (%s) failed: %s", network.name, link.address.four_d,
                                where, status.last_error)
                await self._lane.run(record_link_call, link.id, f"call failed: {status.last_error}")
                return status
            status.last_summary = await self._settle(network, result, outbound, link=link)
        finally:
            outbound.close()
        status.failures = 0
        status.last_error = None
        status.last_success = status.last_attempt
        status.last_success_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        await self._lane.run(record_link_call, link.id, f"called: {status.last_summary}")
        _logger.info("FTN %s: call to downlink %s (%s) done -- %s", network.name, link.address.four_d, where,
                     status.last_summary)
        return status

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
                    result, _ = await self._call(host, port, network, system, outbound.files, password="",
                                                 expected=node)
                except (OSError, asyncio.TimeoutError, BinkpError) as exc:
                    status.failures += 1
                    status.last_error = str(exc) or type(exc).__name__
                    _logger.warning("FTN %s: direct call to %s (%s) failed: %s", network.name, node.four_d,
                                    format_host_port(host, port), status.last_error)
                    partial = getattr(exc, "result", None)
                    if partial is not None and (partial.received or partial.sent):
                        # What moved before the call broke is kept: confirmed
                        # messages and files are settled, whole files tossed.
                        await self._settle(network, replace(partial, secure=False), outbound)
                        if outbound.packet_name is not None and outbound.packet_name in partial.sent:
                            status.failures = 0  # delivered before the call broke
                            continue
                    await self._lane.run(note_failed_crash, network.id, outbound.message_ids,
                                         [row.id for row in outbound.file_rows.values()])
                    if status.failures >= DIRECT_ATTEMPTS:
                        await self._lane.run(_to_uplink, outbound)
                        status.failures = 0
                    continue
                status.failures = 0
                status.last_error = None
                # What a node hands over on a call this node made unasked is
                # held: nothing proved who it is.
                await self._settle(network, replace(result, secure=False), outbound)
            finally:
                outbound.close()
            status.failures = 0
            status.last_error = None
            # What a node hands over on a call this node made unasked is held:
            # nothing proved who it is.

    def _due(self, network: FtnNetwork, pending: int, now: float, *, crash: int = 0) -> bool:
        status = self.status.get(network.id)
        if status is None or status.last_attempt is None:
            return True
        interval = network.poll_minutes * 60.0
        if status.failures:
            retry = retry_after(status.failures, interval)
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
            where = format_host_port(network.uplink_host, network.uplink_port)
            try:
                result, fingerprint = await self._call(
                    network.uplink_host, network.uplink_port, network, system, outbound.files,
                    password=network.session_password, expected=network.uplink_address,
                    tls=network.uplink_tls, pin=network.uplink_tls_pin)
            except (OSError, asyncio.TimeoutError, BinkpError) as exc:
                status.failures += 1
                status.last_error = str(exc) or type(exc).__name__
                partial = getattr(exc, "result", None)
                if partial is not None and (partial.received or partial.sent):
                    moved = await self._settle(network, partial, outbound)
                    status.last_error += f" (before that: {moved})"
                _logger.warning("FTN %s: call to %s failed: %s", network.name, where, status.last_error)
                # Crash mail for the uplink itself is reported once its calls keep failing (issue #1471).
                await self._lane.run(note_failed_crash, network.id, outbound.message_ids,
                                     [row.id for row in outbound.file_rows.values()])
                return status
            if result.plaintext_password:
                _logger.warning("FTN %s: the uplink offered no CRAM-MD5, so the session password crossed "
                                "unhashed", network.name)
            # --- transport (issue #1468): trust on first use ---
            if fingerprint and network.uplink_tls == TLS_PINNED and not network.uplink_tls_pin:
                if await self._lane.run(set_uplink_tls_pin, network.id, fingerprint):
                    _logger.info("FTN %s: pinned the uplink's TLS certificate %s", network.name,
                                 show_fingerprint(fingerprint))
            # --- end transport ---
            status.last_summary = await self._settle(network, result, outbound)
        finally:
            outbound.close()
        status.failures = 0
        status.last_error = None
        status.last_success = status.last_attempt
        status.last_success_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        _logger.info("FTN %s: call to %s%s done -- %s", network.name, where,
                     " over TLS" if network.uplink_tls != TLS_OFF else "", status.last_summary)
        return status

    async def _settle(self, network: FtnNetwork, result: SessionResult, outbound: SessionOutbound, *,
                      link=None) -> str:
        """Settle what the remote confirmed (`SessionOutbound.settle`: its
        messages marked sent, its files' rows gone) and toss what was
        received, as `link`'s when a downlink was called; the summary line.
        Also run on a failed session's partial result, so nothing confirmed
        is sent twice."""
        sent, files_sent = await outbound.settle(self._lane, result.sent)
        tossed = await toss_received(self._lane, network, result, link=link)
        return summary(sent, len(result.received), tossed, files_sent=files_sent)

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
