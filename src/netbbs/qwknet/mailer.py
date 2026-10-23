"""
The QWKnet mailer: this node's calls to its QWKnet hubs (issue #1481,
design doc §6.11).

One `QwknetMailer` per node, started and closed by `netbbs.__main__` like
the FTN mailer. Every `CHECK_INTERVAL` it looks at each enabled network
that has a hub and calls it when

- the network's poll interval has passed since the last call, or
- posts wait to go up and the last call was at least `MIN_CALL_GAP` ago,
  so a new post leaves within the quarter hour without calling a hub
  every minute, or
- the SysOp asked for a call now (`poll_now`).

A call uploads the reply packet of what waits, then fetches and tosses the
hub's packet (`netbbs.qwknet.transport.call_hub`). The upload's cursor
moves only once the hub has taken it, and a fetched packet is tossed before
the hub is told it arrived, so a call cut off part way loses nothing; the
duplicate checks absorb what comes twice. A failed call is retried after 1,
2, 4 ... minutes, never later than the poll interval. What each call came to
is stored on the network for the console. Once a day the stores are pruned
(`netbbs.qwknet.exchange.prune`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from netbbs.qwknet.exchange import build_uplink_rep, confirm_uplink, pending_for_uplink, prune, toss_hub_packet
from netbbs.qwknet.networks import QwknetNetwork, get_network, list_networks, record_poll
from netbbs.qwknet.transport import TransportError, call_hub

_logger = logging.getLogger(__name__)

CHECK_INTERVAL = 60.0
MIN_CALL_GAP = 900.0
MAINTENANCE_INTERVAL = 24 * 3600.0


def retry_after(failures: int, ceiling: float) -> float:
    return min(60.0 * 2 ** min(max(failures - 1, 0), 16), ceiling)


class QwknetMailer:
    def __init__(self, lane, *, clock=time.monotonic, check_interval: float = CHECK_INTERVAL, call=call_hub):
        self._lane = lane
        self._clock = clock
        self._check_interval = check_interval
        self._call = call
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._last_maintenance: float | None = None
        self._last_attempt: dict[int, float] = {}
        self._failures: dict[int, int] = {}
        self._requested: set[int] = set()
        self._calling: set[int] = set()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="qwknet-mailer")

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def poll_now(self, network_id: int) -> None:
        """The console's [P]oll now: call that network's hub at once."""
        self._requested.add(network_id)
        self._wake.set()

    def is_calling(self, network_id: int) -> bool:
        return network_id in self._calling

    async def _run(self) -> None:
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- the loop must outlive one bad pass
                _logger.exception("QWKnet mailer pass failed")
            self._wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self._check_interval)

    async def check_once(self) -> None:
        now = self._clock()
        if self._last_maintenance is None or now - self._last_maintenance >= MAINTENANCE_INTERVAL:
            self._last_maintenance = now
            await self._lane.run(prune)
        for network in await self._lane.run(list_networks):
            if not network.enabled or not network.has_uplink or not network.host:
                self._requested.discard(network.id)
                continue
            if network.id in self._requested or await self._due(network, now):
                self._requested.discard(network.id)
                await self.poll(network)

    async def _due(self, network: QwknetNetwork, now: float) -> bool:
        last = self._last_attempt.get(network.id)
        if last is None:
            return True
        interval = network.poll_minutes * 60.0
        failures = self._failures.get(network.id, 0)
        if failures:
            return now - last >= retry_after(failures, interval)
        if now - last >= interval:
            return True
        if now - last >= MIN_CALL_GAP:
            return await self._lane.run(pending_for_uplink, network) > 0
        return False

    async def poll(self, network: QwknetNetwork) -> str:
        """Call `network`'s hub once; returns the outcome line stored for the
        console."""
        self._last_attempt[network.id] = self._clock()
        self._calling.add(network.id)
        try:
            outgoing = await self._lane.run(build_uplink_rep, network)
            if outgoing.data is None and outgoing.upto > network.uplink_seq:
                await self._lane.run(confirm_uplink, network.id, outgoing.upto)

            async def uploaded() -> None:
                await self._lane.run(confirm_uplink, network.id, outgoing.upto)

            async def toss(data: bytes) -> str:
                current = await self._lane.run(get_network, network.id) or network
                return (await self._lane.run(toss_hub_packet, current, data)).summary()

            try:
                result = await self._call(network, outgoing.data, toss, on_uploaded=uploaded)
            except TransportError as exc:
                self._failures[network.id] = self._failures.get(network.id, 0) + 1
                line = f"failed: {exc}"
                _logger.warning("QWKnet %s: call to %s failed: %s", network.name, network.hub_id, exc)
            else:
                self._failures.pop(network.id, None)
                parts = [f"sent {outgoing.messages}" if result.uploaded else "sent nothing"]
                parts.append(f"received {result.toss_summary}" if result.received else "nothing waiting")
                line = "; ".join(parts)
                _logger.info("QWKnet %s: called %s: %s", network.name, network.hub_id, line)
            await self._lane.run(record_poll, network.id, line)
            return line
        finally:
            self._calling.discard(network.id)
