"""
`MrcBridge`: one in-process MRC hub connection per node (design doc
§16, issue #165 Decision 1; implemented under issue #275).

Architecturally a sibling of `netbbs.link.realtime_channels.
LiveChannelBridge`: it owns no chat storage of its own, delivers
inbound traffic through the same `ChatHub.broadcast()` a purely local
message goes through, and is attached by `netbbs.net.chat_flow` at the
identical join/send/leave call sites. It differs in one deliberate
way: `LiveChannelBridge` never dials on its own, whereas this bridge
*must* run a background connector loop -- the hub is a single fixed
endpoint, and a bridged channel is useless unless the node keeps that
one socket alive across hub restarts.

Identity model (decided for #275, matching every reference client):
each local caller present in a bridged channel is announced to the hub
as their own MRC user -- `nick_for_username(username)` at this node's
site -- with `NEWROOM` on entry, `IAMHERE` every minute and `LOGOFF`
on leave. The set of announced callers is derived from the `ChatHub`'s
own participant roster, so a mapping added while callers are already
inside the channel, or a reconnect after a hub outage, re-announces
exactly who is there rather than relying on a separately-maintained
count.

Trust boundary (§16 Decision 4): inbound content is recorded with
`author_fingerprint=None` and an `author_label` of `user@site (MRC)`,
never enters Phase 4 trust evaluation, and is stripped of every
control/escape sequence by `netbbs.mrc.protocol.parse_line` before it
reaches this module. Private messages (`to_user` naming one caller)
are never delivered (§16 Decision 3) -- the caller gets one muted
notice per sender so the silence isn't mysterious.

Bounds (§16 Decision 5, "bound remotely influenced resources"):

- reconnect with jittered exponential backoff, reset only after a
  connection stays up `stable_after_seconds` (copied from
  `netbbs.link.transport.LinkRealtimeConnector`);
- a fixed-size outbound queue (oldest dropped on overflow, counted), a
  node-wide outbound token bucket and a per-caller bucket, so a stalled
  hub or a pasting caller degrades to *this bridge* going quiet, never
  to blocking local chat delivery;
- an inbound line-length cap and token bucket ahead of any database
  write, so a hostile hub or client can't turn the bridge into a
  scrollback-writing amplifier;
- a fatal hub rejection (`OLDVERSION`) stops retrying until a SysOp
  changes settings -- every retry would start a brand-new rejected
  session.

Body convention and per-caller traffic (issue #298): every MRC client
embeds the sender's own coloured handle in the body and shows an inbound
body verbatim, so outbound chunks wear `protocol.format_room_body`'s
house style and an inbound prefix naming `from_user` is peeled off
before recording; colour codes stay in the stored body for the renderer.
A `SERVER` packet addressed to an announced nick is that caller's reply
(`LIST`, `MOTD`, `INFO`, ...), delivered to their sessions alone as an
`MrcNotice` under a per-caller allowance; `USERROOM`/`USERNICK` keep the
announced state honest and `TERMINATE` is fatal like `OLDVERSION`. CTCP
requests for an announced nick are answered here, bounded per remote
sender. Broadcasts (empty `to_room`) reach every active bridged channel.

Every task this bridge creates is cancelled and gathered by `close()`
on every exit path.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import random
import re
import sqlite3
import ssl
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from netbbs.chat.channels import Channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.presence import PresenceRegistry
from netbbs.chat.scrollback import ChannelMessage, record_message
from netbbs.mrc import protocol
from netbbs.mrc.protocol import MrcPacket, parse_line
from netbbs.mrc.settings import (
    MrcChannelMapping,
    MrcSettings,
    MrcSettingsError,
    OpenRoomSettings,
    list_mrc_mappings,
    load_mrc_settings,
    load_open_room_settings,
    materialize_open_room,
    set_open_room_topic,
    sweep_open_rooms,
    touch_open_room,
)
from netbbs.net.mrc_nick_color_preference import mrc_nick_color_for_username
from netbbs.net.throttle import _TokenBucket
from netbbs.rendering import colored
from netbbs.rendering.pipe_codes import strip_pipe_codes
from netbbs.rendering.sanitize import sanitize_text
from netbbs.rendering.theme import MUTED_COLOR
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import utc_now_iso

MRC_LOGGER_NAME = "netbbs.mrc"
_logger = logging.getLogger(MRC_LOGGER_NAME)


@dataclass(frozen=True)
class MrcNotice:
    """An ephemeral MRC line for a caller's screen (issue #298): a hub
    notice or room chatter (`kind="notice"`), a network-wide broadcast
    (`"broadcast"`), or the hub's reply to something the caller asked
    (`"reply"`). `text` is sanitized and keeps its `|NN` colour codes;
    `netbbs.net.chat_flow` renders it per viewer (colours on or off,
    timestamp preference), the same way `_TimestampedNotice` defers
    rendering there. Never recorded."""

    text: str
    created_at: str
    kind: str = "notice"


# How many reply lines the hub may push at one caller in a burst (a
# `LIST` or `HELP` reply is dozens of lines) and how fast the allowance
# refills; past it, lines are counted and dropped and the caller told
# once per burst.
REPLY_BURST = 60
REPLY_RATE_PER_SECOND = 10.0
# CTCP: every request costs one reply, so a remote sender is bounded on
# its own -- three quick ones, then one every two seconds.
CTCP_BURST = 3
CTCP_RATE_PER_SECOND = 0.5
MAX_TRACKED_CTCP_SENDERS = 200
# Issue #300: rooms this node has seen named (opened here, a USERROOM
# target, join/leave chatter) -- remotely named, so bounded; and how
# often an open room's activity stamp is written per channel.
MAX_OBSERVED_ROOMS = 200
OPEN_ROOM_TOUCH_INTERVAL_SECONDS = 60.0
# `*** Joining lobby: nick@site` / `*** Leaving lobby: nick@site`
_ROOM_CHATTER_RE = re.compile(r"^\*\*\*\s+(?:Joining|Leaving)\s+(?P<room>\S+):\s+\S", re.IGNORECASE)

RECONNECT_MIN_BACKOFF_SECONDS = 1.0
RECONNECT_MAX_BACKOFF_SECONDS = 60.0
RECONNECT_STABLE_AFTER_SECONDS = 30.0
CONNECT_TIMEOUT_SECONDS = 10.0
KEEPALIVE_INTERVAL_SECONDS = 60.0
USERLIST_REFRESH_INTERVAL_SECONDS = 300.0
USERLIST_MIN_INTERVAL_SECONDS = 5.0
OUTBOUND_QUEUE_SIZE = 200
OUTBOUND_RATE_PER_SECOND = 5.0
OUTBOUND_BURST = 10
PER_USER_RATE_PER_SECOND = 1.0
PER_USER_BURST = 3
INBOUND_RATE_PER_SECOND = 20.0
INBOUND_BURST = 40
INBOUND_BUFFER_LIMIT = 4096
MAX_TRACKED_PRIVATE_SENDERS = 50


class MrcState(str, Enum):
    DISABLED = "disabled"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    BACKOFF = "backoff"
    ERROR = "error"


@dataclass(frozen=True)
class MrcStatus:
    """A point-in-time snapshot for the SysOp status screen and the
    chat status line -- plain values, safe to render without touching
    the bridge again."""

    state: MrcState
    enabled: bool
    host: str
    port: int
    tls: bool
    site_name: str
    connected_since: str | None
    last_error: str | None
    attempts: int
    bridged_channels: int
    participants: int
    dropped_outbound: int
    dropped_inbound: int
    rooms: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Issue #300: open rooms (rooms callers opened) against the SysOp's
    # cap, rooms this node has heard of but nobody here is in, and how
    # many idle open rooms the sweeper has retired since start.
    open_rooms_enabled: bool = False
    open_rooms: int = 0
    open_room_cap: int = 0
    observed_rooms: int = 0
    retired_rooms: int = 0
    # Issue #304: the network's size from the hub's last `STATS` reply
    # (BBSes, rooms, users), how old that reading is, and the raw line
    # when it did not parse.
    network_bbses: int | None = None
    network_rooms: int | None = None
    network_users: int | None = None
    network_stats_age_seconds: float | None = None
    network_stats_raw: str | None = None

    @property
    def network_summary(self) -> str | None:
        """"41 users on 12 boards", or `None` while nothing is known."""
        if self.network_users is None or self.network_bbses is None:
            return None
        users = f"{self.network_users} user{'s' if self.network_users != 1 else ''}"
        boards = f"{self.network_bbses} board{'s' if self.network_bbses != 1 else ''}"
        return f"{users} on {boards}"

    @property
    def connected(self) -> bool:
        return self.state is MrcState.CONNECTED


@dataclass
class _Connection:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    tasks: list[asyncio.Task] = field(default_factory=list)


OpenConnection = Callable[..., Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


def _platform_label() -> str:
    machine = platform.machine() or "unknown"
    return f"{sys.platform}.{machine}"


class MrcBridge:
    def __init__(
        self,
        *,
        hub: ChatHub,
        lane: DatabaseLane,
        version: str,
        presence: PresenceRegistry | None = None,
        load_settings: Callable[[Database], MrcSettings] = load_mrc_settings,
        load_mappings: Callable[[Database], list[MrcChannelMapping]] = list_mrc_mappings,
        load_open_settings: Callable[[Database], OpenRoomSettings] = load_open_room_settings,
        load_nick_color: Callable[[Database, str], int] = mrc_nick_color_for_username,
        open_connection: OpenConnection = asyncio.open_connection,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
        min_backoff_seconds: float = RECONNECT_MIN_BACKOFF_SECONDS,
        max_backoff_seconds: float = RECONNECT_MAX_BACKOFF_SECONDS,
        stable_after_seconds: float = RECONNECT_STABLE_AFTER_SECONDS,
        connect_timeout_seconds: float = CONNECT_TIMEOUT_SECONDS,
        keepalive_interval_seconds: float = KEEPALIVE_INTERVAL_SECONDS,
        userlist_refresh_seconds: float = USERLIST_REFRESH_INTERVAL_SECONDS,
        outbound_queue_size: int = OUTBOUND_QUEUE_SIZE,
        reply_burst: int = REPLY_BURST,
    ) -> None:
        self._hub = hub
        self._lane = lane
        self._version = version
        # Issue #304: the one away state is the presence registry's; the
        # bridge reads it when it announces a caller rather than keeping
        # a copy that could outlive the account's last session.
        self._presence = presence
        self._load_settings = load_settings
        self._load_mappings = load_mappings
        self._load_open_settings = load_open_settings
        self._load_nick_color = load_nick_color
        self._open_connection = open_connection
        self._rng = rng if rng is not None else random.Random()
        self._clock = clock
        self._min_backoff = min_backoff_seconds
        self._max_backoff = max_backoff_seconds
        self._stable_after = stable_after_seconds
        self._connect_timeout = connect_timeout_seconds
        self._keepalive_interval = keepalive_interval_seconds
        self._userlist_refresh = userlist_refresh_seconds
        self._reply_burst = reply_burst

        self._settings: MrcSettings | None = None
        # room (lower-cased) -> mapping; channel id -> mapping
        self._by_room: dict[str, MrcChannelMapping] = {}
        self._by_channel: dict[int, MrcChannelMapping] = {}
        # channel id -> {username: nick} of callers currently announced
        # to the hub, and channel id -> the room they were announced in
        # (so a SysOp remapping an occupied channel moves them).
        self._announced: dict[int, dict[str, str]] = {}
        self._announced_rooms: dict[int, str] = {}
        # room (lower) -> roster from the hub's last USERLIST reply
        self._rosters: dict[str, tuple[str, ...]] = {}
        self._last_userlist_request: dict[str, float] = {}
        # (sender lower, channel id, username) -> already notified about
        # an undeliverable private message
        self._private_notified: set[tuple[str, int, str]] = set()
        # Issue #298: per-caller allowance for hub reply lines, whether
        # that caller has already been told a burst was cut short, the
        # per-remote-sender CTCP allowance, and how often the hub moved
        # each caller out of their mapped room this keepalive tick
        # (re-announced on the first move and told on the first two, so a
        # room that keeps bouncing them is neither a NEWROOM loop nor a
        # stream of priority notices).
        self._reply_buckets: dict[str, _TokenBucket] = {}
        self._reply_truncated: set[str] = set()
        self._ctcp_buckets: dict[tuple[str, str], _TokenBucket] = {}
        self._rehomed: dict[tuple[int, str], int] = {}
        # Issue #300: the open-room settings, rooms heard of (lower-cased
        # name -> (display name, last seen)), the last activity stamp
        # written per open room, and the sweeper's tally.
        self._open_settings: OpenRoomSettings | None = None
        self._observed_rooms: dict[str, tuple[str, float]] = {}
        self._last_touch: dict[int, float] = {}
        self._retired_rooms = 0
        # channel id -> when a session last passed the pre-join check for
        # it: the sweeper treats a room being entered as occupied for a
        # minute, so the lane hop between its occupancy snapshot and its
        # delete cannot race a caller who is one await away from joining.
        self._entering: dict[int, float] = {}
        # (channel id, username) already told that their identity is held
        # elsewhere -- said once per conflict, not once per keepalive tick.
        self._identity_notified: set[tuple[int, str]] = set()
        # Issue #304: per-caller nick colour (read once per announcement),
        # away state to mirror (username -> message, present = away),
        # the hub's banner lines, the last STATS reading, and who asked
        # for STATS themselves (their reply is shown, the bridge's own
        # periodic ask is only parsed).
        self._nick_colors: dict[str, int] = {}
        self._banner: list[str] = []
        self._network_stats: tuple[int, int, int] | None = None
        self._network_stats_at: float | None = None
        self._network_stats_raw: str | None = None
        self._stats_requested: set[str] = set()

        self._outbound: asyncio.Queue[str] = asyncio.Queue(maxsize=outbound_queue_size)
        self._node_bucket = _TokenBucket(OUTBOUND_BURST, OUTBOUND_RATE_PER_SECOND, clock)
        self._user_buckets: dict[str, _TokenBucket] = {}
        self._inbound_bucket = _TokenBucket(INBOUND_BURST, INBOUND_RATE_PER_SECOND, clock)

        self._state = MrcState.DISABLED
        self._connector_task: asyncio.Task | None = None
        # Issue #300: the open-room sweeper runs on its own task, on the
        # keepalive cadence but independent of the hub connection -- a
        # room must age out while the hub is unreachable or MRC is off.
        self._sweeper_task: asyncio.Task | None = None
        self._connection: _Connection | None = None
        self._stopping = False
        self._fatal_error: str | None = None
        self._last_error: str | None = None
        self._connected_since: str | None = None
        self._connected_at_monotonic: float | None = None
        # Set when a SysOp (re)enables MRC: the first connection after
        # that must give occupants of already-mapped channels the same
        # disclosure a live mapping change gives (they never saw the
        # join-time notice, since the channel was not bridged then).
        self._notify_on_connect = False
        self._attempts = 0
        self._dropped_outbound = 0
        self._dropped_inbound = 0
        self._reload_lock = asyncio.Lock()

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Load settings and mappings, and start the connector if MRC is
        enabled. Idempotent; safe to call when already running."""
        async with self._reload_lock:
            await self._reload_from_db()
            self._start_connector_if_enabled()
            self._start_sweeper()

    async def close(self) -> None:
        self._stopping = True
        await self._stop_sweeper()
        await self._stop_connector(graceful=True)

    def _start_sweeper(self) -> None:
        if self._stopping or (self._sweeper_task is not None and not self._sweeper_task.done()):
            return
        self._sweeper_task = asyncio.create_task(self._sweeper_loop(), name="mrc-open-room-sweeper")

    async def _stop_sweeper(self) -> None:
        task, self._sweeper_task = self._sweeper_task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _sweeper_loop(self) -> None:
        """Retire idle open rooms (`_sweep_open_rooms`) once per keepalive
        interval, whatever the hub link is doing. Re-reads the mappings
        and open-room settings first so a retention or blocklist edit
        made from the standalone admin CLI is honoured within a tick."""
        while not self._stopping:
            await asyncio.sleep(self._keepalive_interval)
            if self._stopping:
                return
            try:
                await self.refresh_channel_mappings()
                await self._sweep_open_rooms()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _logger.warning("MRC open-room sweeper: %s", exc)

    async def reload_settings(self) -> None:
        """Re-read the hub settings and channel mappings from the
        database and reconnect (or disconnect) accordingly -- the SysOp
        settings screen's "apply" path. A fatal rejection from the
        previous settings is cleared: the SysOp may just have fixed it."""
        async with self._reload_lock:
            await self._stop_connector(graceful=True)
            self._fatal_error = None
            self._attempts = 0
            # A reading from the previous hub must not describe the new one.
            self._network_stats = None
            self._network_stats_at = None
            self._network_stats_raw = None
            self._banner.clear()
            await self._reload_from_db()
            self._notify_on_connect = True
            if not self._stopping:
                self._start_connector_if_enabled()

    async def refresh_channel_mappings(self) -> None:
        """Re-read only the per-channel mappings (after a SysOp maps,
        unmaps or pauses a channel) and reconcile announced callers
        without dropping the hub connection."""
        async with self._reload_lock:
            def _load(db: Database) -> tuple[list[MrcChannelMapping], OpenRoomSettings]:
                return self._load_mappings(db), self._load_open_settings(db)

            mappings, self._open_settings = await self._lane.run(_load)
            self._apply_mappings(mappings)
            if self._state is MrcState.CONNECTED:
                await self._reconcile_announced(notify=True)
            if self._settings is not None and self._settings.enabled and self._connector_task is None:
                self._notify_on_connect = True
                self._start_connector_if_enabled()

    async def _reload_from_db(self) -> None:
        def _load(db: Database) -> tuple[MrcSettings, list[MrcChannelMapping], OpenRoomSettings]:
            return self._load_settings(db), self._load_mappings(db), self._load_open_settings(db)

        settings, mappings, open_settings = await self._lane.run(_load)
        self._settings = settings
        self._open_settings = open_settings
        self._apply_mappings(mappings)

    def _apply_mappings(self, mappings: list[MrcChannelMapping]) -> None:
        self._by_room = {mapping.room.lower(): mapping for mapping in mappings}
        self._by_channel = {mapping.channel.id: mapping for mapping in mappings}
        for mapping in mappings:
            self._observe_room(mapping.room)

    def _start_connector_if_enabled(self) -> None:
        if self._stopping or self._settings is None or not self._settings.enabled:
            self._state = MrcState.DISABLED
            return
        if self._connector_task is not None and not self._connector_task.done():
            return
        self._state = MrcState.CONNECTING
        self._connector_task = asyncio.create_task(self._run(), name="mrc-connector")
        self._connector_task.add_done_callback(self._on_connector_done)

    def _on_connector_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._last_error = f"connector crashed: {exc!r}"
            self._state = MrcState.ERROR
            _logger.error("MRC connector task failed: %r", exc)

    async def _stop_connector(self, *, graceful: bool) -> None:
        task = self._connector_task
        self._connector_task = None
        if graceful and self._connection is not None and self._state is MrcState.CONNECTED:
            await self._send_farewell()
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self._close_connection()
        self._announced.clear()
        self._announced_rooms.clear()
        self._rosters.clear()
        self._last_userlist_request.clear()
        self._rehomed.clear()
        self._reply_truncated.clear()
        if self._state is not MrcState.ERROR:
            self._state = MrcState.DISABLED

    async def _send_farewell(self) -> None:
        """Best-effort LOGOFF for every announced caller plus SHUTDOWN,
        written directly (the writer task may already be gone), bounded
        by a short timeout so shutdown never hangs on a dead hub."""
        connection = self._connection
        settings = self._settings
        if connection is None or settings is None:
            return
        lines = []
        for channel_id, nicks in self._announced.items():
            mapping = self._by_channel.get(channel_id)
            room = mapping.room if mapping is not None else ""
            for nick in nicks.values():
                lines.append(protocol.build_line(protocol.logoff(nick, settings.site_wire_name, room)))
        lines.append(protocol.build_line(protocol.shutdown(settings.site_wire_name)))
        try:
            connection.writer.write("".join(lines).encode("ascii", errors="replace"))
            await asyncio.wait_for(connection.writer.drain(), timeout=2.0)
        except Exception:
            pass

    async def _close_connection(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        for task in connection.tasks:
            task.cancel()
        if connection.tasks:
            await asyncio.gather(*connection.tasks, return_exceptions=True)
        try:
            connection.writer.close()
            await asyncio.wait_for(connection.writer.wait_closed(), timeout=2.0)
        except Exception:
            pass
        self._connected_since = None

    # --- connector loop ----------------------------------------------------

    async def _run(self) -> None:
        backoff = self._min_backoff
        while not self._stopping:
            settings = self._settings
            if settings is None or not settings.enabled:
                self._state = MrcState.DISABLED
                return
            self._attempts += 1
            self._state = MrcState.CONNECTING
            self._connected_at_monotonic = None
            try:
                await self._connect_and_serve(settings)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = _describe_error(exc)
                _logger.warning("MRC hub %s:%d: %s", settings.host, settings.port, self._last_error)
            finally:
                await self._close_connection()
                self._announced.clear()
                self._announced_rooms.clear()
                self._rosters.clear()
                self._last_userlist_request.clear()
                self._rehomed.clear()
                self._reply_truncated.clear()
            # `_connect_and_serve` never returns normally -- a finished
            # session always raises -- so "stable" is judged from how long
            # the session was actually up, not from a normal return.
            stable = (
                self._connected_at_monotonic is not None
                and self._clock() - self._connected_at_monotonic >= self._stable_after
            )
            if self._stopping:
                return
            if self._fatal_error is not None:
                self._state = MrcState.ERROR
                _logger.error("MRC hub rejected this node; not retrying until settings change: %s", self._fatal_error)
                return
            if stable:
                backoff = self._min_backoff
            self._state = MrcState.BACKOFF
            await self._backoff_sleep(backoff)
            backoff = min(backoff * 2, self._max_backoff)

    async def _backoff_sleep(self, backoff: float) -> None:
        """The jittered wait before the next attempt -- a seam tests can
        observe instead of timing real sleeps."""
        await asyncio.sleep(self._rng.uniform(0, backoff))

    async def _connect_and_serve(self, settings: MrcSettings) -> None:
        ssl_context = ssl.create_default_context() if settings.tls else None
        kwargs: dict[str, Any] = {"limit": INBOUND_BUFFER_LIMIT}
        if ssl_context is not None:
            kwargs["ssl"] = ssl_context
            kwargs["server_hostname"] = settings.host
        reader, writer = await asyncio.wait_for(
            self._open_connection(settings.host, settings.port, **kwargs), timeout=self._connect_timeout
        )
        connection = _Connection(reader=reader, writer=writer)
        self._connection = connection
        handshake = protocol.build_handshake(
            settings.site_name, software=f"NetBBS_{self._version}", platform=_platform_label()
        )
        writer.write(handshake.encode("ascii", errors="replace"))
        await writer.drain()
        await asyncio.wait_for(self._await_hello(reader), timeout=self._connect_timeout)

        self._state = MrcState.CONNECTED
        self._connected_since = utc_now_iso()
        self._banner.clear()
        self._nick_colors.clear()
        self._connected_at_monotonic = self._clock()
        self._last_error = None
        _logger.info("Connected to MRC hub %s:%d as %r", settings.host, settings.port, settings.site_name)
        self._drain_outbound_queue()
        self._send_site_info(settings)
        notify, self._notify_on_connect = self._notify_on_connect, False
        await self._reconcile_announced(notify=notify)
        for channel_id, nicks in self._announced.items():
            mapping = self._by_channel.get(channel_id)
            if mapping is not None and nicks:
                self._request_stats(next(iter(nicks.values())), mapping.room)
                break

        connection.tasks = [
            asyncio.create_task(self._reader_loop(reader), name="mrc-reader"),
            asyncio.create_task(self._writer_loop(writer), name="mrc-writer"),
            asyncio.create_task(self._keepalive_loop(), name="mrc-keepalive"),
        ]
        done, _pending = await asyncio.wait(connection.tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exc = task.exception() if not task.cancelled() else None
            if exc is not None:
                raise exc
        raise ConnectionError("hub connection closed")

    async def _await_hello(self, reader: asyncio.StreamReader) -> None:
        """The hub answers the handshake with `SERVER~~~CLIENT~~~HELLO~`.
        Anything else before it (a banner, a version notice) goes through
        the same bounded inbound path as post-HELLO traffic -- size cap
        and token bucket ahead of any database write -- so a hub that
        floods room packets during every handshake window is throttled
        exactly like one that floods afterwards; `OLDVERSION` there is
        fatal."""
        while True:
            line = await reader.readline()
            if not line:
                raise ConnectionError("hub closed the connection during handshake")
            packet = self._parse_raw_line(line)
            if packet is None:
                continue
            # HELLO is the connection gate itself: recognised before the
            # bucket, or a flood ahead of it would leave the bridge unable
            # to ever finish the handshake.
            if packet.is_server and packet.body.strip().upper() == "HELLO":
                return
            if self._admit_packet():
                await self._handle_packet(packet)

    def _send_site_info(self, settings: MrcSettings) -> None:
        site = settings.site_wire_name
        infos = (
            ("WEB", settings.info_web), ("TEL", settings.info_telnet), ("SSH", settings.info_ssh),
            ("SYS", settings.info_sysop), ("DSC", settings.info_description),
        )
        for key, value in infos:
            if value:
                self._enqueue(protocol.info(site, key, value))
        self._enqueue(protocol.imalive(site, settings.site_name))
        self._enqueue(protocol.capabilities(site, ["MCI"] + (["SSL"] if settings.tls else [])))

    async def _reader_loop(self, reader: asyncio.StreamReader) -> None:
        buffer = b""
        while True:
            chunk = await reader.read(INBOUND_BUFFER_LIMIT)
            if not chunk:
                return
            buffer += chunk
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                await self._handle_raw_line(raw)
            if len(buffer) > INBOUND_BUFFER_LIMIT:
                # No newline within the cap: not a packet, whatever it is.
                self._dropped_inbound += 1
                buffer = b""

    def _parse_raw_line(self, raw: bytes) -> MrcPacket | None:
        """Size cap and parse -- the first half of the inbound gate every
        line passes, before or after HELLO. `None` means dropped (and
        counted)."""
        if len(raw) > protocol.MAX_LINE * 2:
            self._dropped_inbound += 1
            return None
        packet = parse_line(raw.decode("ascii", errors="replace"))
        if packet is None and raw.strip():
            self._dropped_inbound += 1
        return packet

    def _admit_packet(self) -> bool:
        """The second half: the inbound token bucket ahead of any
        database write. `False` means dropped (and counted)."""
        if not self._inbound_bucket.has_token():
            self._dropped_inbound += 1
            return False
        self._inbound_bucket.consume()
        return True

    async def _handle_raw_line(self, raw: bytes) -> None:
        # Every non-empty line pays, well-formed or not: a hub streaming
        # garbage is bounded by the same bucket as one streaming packets.
        if not raw.strip():
            return
        if not self._admit_packet():
            return
        packet = self._parse_raw_line(raw)
        if packet is not None:
            await self._handle_packet(packet)

    async def _writer_loop(self, writer: asyncio.StreamWriter) -> None:
        while True:
            line = await self._outbound.get()
            while not self._node_bucket.has_token():
                await asyncio.sleep(1.0 / OUTBOUND_RATE_PER_SECOND)
            self._node_bucket.consume()
            writer.write(line.encode("ascii", errors="replace"))
            await writer.drain()

    async def _keepalive_loop(self) -> None:
        last_userlist = self._clock()
        while True:
            await asyncio.sleep(self._keepalive_interval)
            settings = self._settings
            if settings is None:
                continue
            # The standalone admin CLI edits mappings in SQLite without a
            # way to tell the running node; re-reading them once per tick
            # bounds how long a pause, unmap, delete or rename made there
            # keeps relaying (and tells any newly-bridged occupants).
            await self.refresh_channel_mappings()
            self._rehomed.clear()
            refresh_rosters = self._clock() - last_userlist >= self._userlist_refresh
            for channel_id, nicks in list(self._announced.items()):
                mapping = self._by_channel.get(channel_id)
                if mapping is None:
                    continue
                for nick in nicks.values():
                    self._enqueue(protocol.iamhere(nick, settings.site_wire_name, mapping.room))
                if refresh_rosters and nicks:
                    self._request_userlist(mapping, next(iter(nicks.values())), force=True)
            if refresh_rosters:
                last_userlist = self._clock()
                # One STATS ask per refresh, as any announced nick: the
                # network's size for Who's online and the picker.
                for channel_id, nicks in self._announced.items():
                    mapping = self._by_channel.get(channel_id)
                    if mapping is not None and nicks:
                        self._request_stats(next(iter(nicks.values())), mapping.room)
                        break

    # --- outbound ----------------------------------------------------------

    def _enqueue(self, packet: MrcPacket) -> None:
        try:
            line = protocol.build_line(packet)
        except protocol.MrcProtocolError as exc:
            self._dropped_outbound += 1
            _logger.warning("Dropped outbound MRC packet: %s", exc)
            return
        if self._outbound.full():
            try:
                self._outbound.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._dropped_outbound += 1
        self._outbound.put_nowait(line)

    def _drain_outbound_queue(self) -> None:
        """Anything queued while disconnected refers to a session the
        hub no longer knows about (LOGOFFs for callers who left, chat
        that would arrive out of context) -- start each connection
        clean rather than replaying it."""
        while True:
            try:
                self._outbound.get_nowait()
            except asyncio.QueueEmpty:
                return

    def _user_bucket(self, username: str) -> _TokenBucket:
        bucket = self._user_buckets.get(username)
        if bucket is None:
            if len(self._user_buckets) >= 500:
                self._user_buckets.clear()
            bucket = _TokenBucket(PER_USER_BURST, PER_USER_RATE_PER_SECOND, self._clock)
            self._user_buckets[username] = bucket
        return bucket

    def _request_userlist(self, mapping: MrcChannelMapping, nick: str, *, force: bool = False) -> None:
        settings = self._settings
        if settings is None:
            return
        key = mapping.room.lower()
        now = self._clock()
        if not force and now - self._last_userlist_request.get(key, -1e9) < USERLIST_MIN_INTERVAL_SECONDS:
            return
        self._last_userlist_request[key] = now
        self._enqueue(protocol.userlist(nick, settings.site_wire_name, mapping.room))

    # --- local participants --------------------------------------------------

    def mapping_for(self, channel: Channel) -> MrcChannelMapping | None:
        """The channel's room mapping (paused or not), or `None` if the
        channel isn't bridged at all."""
        return self._by_channel.get(channel.id)

    def is_bridged(self, channel: Channel) -> bool:
        """Whether local traffic in `channel` currently reaches MRC:
        mapped, not paused, and MRC enabled node-wide."""
        mapping = self._by_channel.get(channel.id)
        return mapping is not None and mapping.active and self._settings is not None and self._settings.enabled

    def nick_for(self, username: str) -> str:
        return protocol.nick_for_username(username)

    def remote_roster(self, channel: Channel) -> list[str]:
        """MRC users the hub last reported in the channel's room, minus
        this node's own callers, sorted case-insensitively. An MRC
        identity is nick *and* site: a remote `alice@OtherBoard` is not
        this node's `alice` and stays listed."""
        mapping = self._by_channel.get(channel.id)
        if mapping is None:
            return []
        own = {nick.lower() for nick in self._announced.get(channel.id, {}).values()}
        own_site = self._settings.site_wire_name.lower() if self._settings is not None else ""
        roster = self._rosters.get(mapping.room.lower(), ())

        def _is_own(entry: str) -> bool:
            nick, _, site = entry.partition("@")
            if site:
                # Anyone the hub lists at this node's own site is this
                # node's caller, announced now or a moment ago (issue
                # #300: a roster fetched before a caller left still
                # names them) -- never "on MRC" from here.
                return site.lower() == own_site
            return nick.lower() in own

        return sorted((name for name in roster if not _is_own(name)), key=str.lower)

    async def local_join(self, channel: Channel, username: str) -> None:
        """A caller entered `channel`. Announces them to the hub if the
        channel is bridged and they aren't already announced (a second
        session of the same account is still one MRC user)."""
        mapping = self._by_channel.get(channel.id)
        if mapping is None or not mapping.active:
            return
        # Stamped before the connectivity check: a room in use during a
        # hub outage is not idle, whatever the link is doing.
        await self._touch(mapping)
        if self._state is not MrcState.CONNECTED:
            return
        await self._ensure_nick_color(username)
        if not self._announce(mapping, username):
            await self._notify_identity_held(mapping, username)

    async def local_leave(self, channel: Channel, username: str) -> None:
        """A caller left `channel`. Sends LOGOFF only once the account
        has no session left in the channel (`ChatHub` is the source of
        truth, so this must be called after `hub.leave`)."""
        nicks = self._announced.get(channel.id)
        if nicks is None or username not in nicks:
            return
        if self._hub.participants_for_username(channel.name, username):
            return
        nick = nicks.pop(username)
        if not nicks:
            self._announced.pop(channel.id, None)
            self._announced_rooms.pop(channel.id, None)
        if not any(username in others for others in self._announced.values()):
            # Their next announcement re-reads the Profile: "applies the
            # next time you enter an MRC room" means exactly that.
            self._nick_colors.pop(username, None)
        mapping = self._by_channel.get(channel.id)
        settings = self._settings
        if mapping is None or settings is None or self._state is not MrcState.CONNECTED:
            return
        self._enqueue(protocol.logoff(nick, settings.site_wire_name, mapping.room))
        await self._promote_waiting(username)

    async def _promote_waiting(self, username: str) -> None:
        """The identity just left its room: announce `username` in the
        first other active bridged channel they still occupy (the one
        held back by `_announce`'s one-room rule), now rather than on the
        next keepalive tick, and let its notice be sent again if the
        conflict ever recurs."""
        for mapping in self._by_channel.values():
            if not mapping.active or username in self._announced.get(mapping.channel.id, {}):
                continue
            if not self._hub.participants_for_username(mapping.channel.name, username):
                continue
            if self._announce(mapping, username):
                self._identity_notified.discard((mapping.channel.id, username))
                await self._notify_bridged(mapping, [username])
                return

    async def local_message(self, channel: Channel, message: ChannelMessage) -> tuple[bool, bool]:
        """Relay a locally-recorded `message`/`action` to the room.
        Returns `(relayed, truncated)`: `relayed` is `False` when the
        channel isn't bridged, the bridge is offline, or the caller's
        own rate bucket is empty (the caller sees a notice in each of
        the latter two cases); `truncated` when the line exceeded
        `MAX_CHUNKS` wire chunks."""
        mapping = self._by_channel.get(channel.id)
        settings = self._settings
        if mapping is None or not mapping.active or settings is None:
            return False, False
        if message.kind in ("message", "action"):
            await self._touch(mapping)  # in use, relayed or not
        if self._state is not MrcState.CONNECTED:
            return False, False
        if message.kind not in ("message", "action") or not message.body:
            return False, False
        username = message.author_label
        nicks = self._announced.setdefault(channel.id, {})
        nick = nicks.get(username)
        if nick is None:
            await self._ensure_nick_color(username)
            if not self._announce(mapping, username):
                return False, False
            nick = nicks[username]
        body = protocol.sanitize_body(message.body)
        if not body:
            return False, False
        # Issue #298: every chunk carries the caller's handle in the
        # network's own body convention, so the words reach other boards
        # with a name attached; the prefix is paid for out of the same
        # 140-character budget as the words.
        color = self._nick_colors.get(username, protocol.DEFAULT_NICK_COLOR)
        if message.kind == "action":
            template = protocol.format_action_body
        else:
            def template(nick_: str, text_: str, _color: int = color) -> str:
                return protocol.format_room_body(nick_, text_, nick_color=_color)
        chunks, truncated = protocol.split_body(body, reserve=len(template(nick, "")))
        bucket = self._user_bucket(username)
        # All chunks or none: a prefix of a long line reaching MRC while
        # the caller is told it was *not* relayed is worse than either.
        if not bucket.has_tokens(len(chunks)):
            self._dropped_outbound += 1
            return False, truncated
        for chunk in chunks:
            bucket.consume()
            self._enqueue(protocol.chat_message(nick, settings.site_wire_name, mapping.room, template(nick, chunk)))
        return True, truncated

    def _announce(self, mapping: MrcChannelMapping, username: str) -> bool:
        """Announce `username` in `mapping`'s room. `False` when the
        account's one MRC identity is already announced in another room
        (issue #300 review: two occupied local channels mapped or
        unpaused by the SysOp would otherwise both announce the same
        nick); the caller is told through `_notify_identity_held`."""
        settings = self._settings
        if settings is None:
            return False
        nicks = self._announced.setdefault(mapping.channel.id, {})
        if username in nicks:
            return True
        for other_id, other_nicks in self._announced.items():
            if other_id != mapping.channel.id and username in other_nicks:
                return False
        nick = protocol.nick_for_username(username)
        nicks[username] = nick
        self._announced_rooms[mapping.channel.id] = mapping.room
        self._enqueue(protocol.newroom(nick, settings.site_wire_name, "", mapping.room))
        self._request_userlist(mapping, nick)
        away = self._away_message(username)
        if away is not None:
            # The hub is never behind on a caller's away state: told on
            # every announcement, reconnects included.
            self._enqueue(protocol.afk(nick, settings.site_wire_name, mapping.room, away))
        return True

    def _away_message(self, username: str) -> str | None:
        """The caller's current away message per the presence registry
        (bounded and sanitized for the wire), or `None` when not away or
        when no registry was given."""
        if self._presence is None or not self._presence.is_away(username):
            return None
        return self._wire_away_text(self._presence.get_away_message(username) or "")[0]

    @staticmethod
    def _wire_away_text(message: str) -> tuple[str, bool]:
        """An away message as it may travel: sanitized like any body and
        cut to what fits after `AFK ` in one packet. Returns the text and
        whether it was cut."""
        text = protocol.sanitize_body(message)
        limit = protocol.MAX_BODY - len("AFK ")
        if len(text) > limit:
            return text[:limit].rstrip(), True
        return text, False

    async def _ensure_nick_color(self, username: str) -> None:
        if username in self._nick_colors:
            return
        try:
            color = await self._lane.run(self._load_nick_color, username)
        except Exception:
            color = protocol.DEFAULT_NICK_COLOR
        self._nick_colors[username] = color
        if len(self._nick_colors) > 500:
            self._nick_colors.clear()

    # --- presence, welcome, size, topics (issue #304) ------------------------

    async def local_away(self, username: str, message: str | None) -> bool:
        """Mirror the caller's away state to the hub now: `message` marks
        them away (`AFK <message>`), `None` brings them back. Sent from
        every room they are announced in; the presence registry keeps
        the state, so a reconnect or a later announcement repeats it
        (`_announce`). Returns whether the message was cut to fit."""
        settings = self._settings
        text, truncated = ("", False) if message is None else self._wire_away_text(message)
        if settings is None or self._state is not MrcState.CONNECTED:
            return truncated
        for channel_id, nicks in self._announced.items():
            mapping = self._by_channel.get(channel_id)
            nick = nicks.get(username)
            if mapping is None or nick is None:
                continue
            self._enqueue(protocol.afk(nick, settings.site_wire_name, mapping.room, None if message is None else text))
        return truncated

    def banner_lines(self) -> list[str]:
        """What the hub said in its `BANNER:` lines on connect, sanitized
        with colour codes kept -- the welcome a caller sees once per
        session on their first MRC room (issue #304)."""
        return list(self._banner)

    def send_topic(self, channel: Channel, username: str, text: str) -> str | None:
        """Ask the hub to set an open room's topic (`NEWTOPIC`); the hub
        decides (MRC Trust is required there) and its reply reaches the
        caller. Returns `None` when queued, else the reason it was not."""
        mapping = self._by_channel.get(channel.id)
        settings = self._settings
        if mapping is None or not mapping.active or settings is None or not settings.enabled:
            return "this channel isn't bridged to MRC"
        if self._state is not MrcState.CONNECTED:
            return "the MRC link is offline"
        nick = self._announced.get(channel.id, {}).get(username)
        if nick is None:
            return "you aren't announced to the hub yet"
        body = protocol.sanitize_body(text)
        if not body:
            return "nothing to send"
        if len(f"NEWTOPIC:{mapping.room}:{body}") > protocol.MAX_BODY:
            return f"that topic is longer than MRC allows ({protocol.MAX_BODY} characters with the room name)"
        bucket = self._user_bucket(username)
        if not bucket.has_token():
            self._dropped_outbound += 1
            return "you're sending faster than MRC allows"
        bucket.consume()
        self._enqueue(protocol.newtopic(nick, settings.site_wire_name, mapping.room, body))
        return None

    def _request_stats(self, nick: str, room: str) -> None:
        settings = self._settings
        if settings is None:
            return
        self._enqueue(protocol.stats(nick, settings.site_wire_name, room))

    def _record_stats(self, params: str) -> None:
        parsed = protocol.parse_stats(params)
        self._network_stats_at = self._clock()
        if parsed is None:
            self._network_stats = None
            self._network_stats_raw = strip_pipe_codes(params).strip()[:protocol.MAX_LINE]
        else:
            self._network_stats = parsed
            self._network_stats_raw = None

    async def _notify_identity_held(self, mapping: MrcChannelMapping, username: str) -> None:
        held = self.identity_room_elsewhere(mapping.channel, username)
        if held is None:
            return
        key = (mapping.channel.id, username)
        if key in self._identity_notified:
            return
        self._identity_notified.add(key)
        notice = colored(
            f"Your MRC identity is already in #{sanitize_text(held)} from another session; MRC allows one "
            f"room per user, so what you say here is not relayed to #{sanitize_text(mapping.room)} until you leave it there.",
            fg_color=MUTED_COLOR,
        )
        for participant in self._hub.participants_for_username(mapping.channel.name, username):
            await self._hub.send_to(mapping.channel.name, participant, notice, priority=True)

    async def _reconcile_announced(self, *, notify: bool = False) -> None:
        """Make the announced set match "callers currently in an active
        bridged channel" per the `ChatHub`: announce newcomers (a
        mapping added while callers were already inside; every caller
        after a reconnect), move everyone whose channel now maps to a
        different room, LOGOFF anyone whose channel was unmapped or
        paused.

        `notify` (the SysOp-mapping path, not a reconnect): a caller who
        is already inside a channel when it goes on the network never saw
        the join-time disclosure, so they are told here, before anything
        they say leaves the node -- the NEWROOM announcing them is queued
        in the same turn, ahead of any message they type next."""
        settings = self._settings
        if settings is None or self._state is not MrcState.CONNECTED:
            return
        for channel_id, nicks in list(self._announced.items()):
            mapping = self._by_channel.get(channel_id)
            if mapping is not None and mapping.active:
                previous_room = self._announced_rooms.get(channel_id, mapping.room)
                if previous_room.lower() != mapping.room.lower():
                    for nick in nicks.values():
                        self._enqueue(protocol.newroom(nick, settings.site_wire_name, previous_room, mapping.room))
                    self._announced_rooms[channel_id] = mapping.room
                    self._rosters.pop(previous_room.lower(), None)
                    self._request_userlist(mapping, next(iter(nicks.values())), force=True)
                    if notify:
                        await self._notify_bridged(mapping, list(nicks), moved_from=previous_room)
                continue
            room = mapping.room if mapping is not None else self._announced_rooms.get(channel_id, "")
            for nick in nicks.values():
                self._enqueue(protocol.logoff(nick, settings.site_wire_name, room))
            self._announced.pop(channel_id, None)
            self._announced_rooms.pop(channel_id, None)
        for mapping in self._by_channel.values():
            if not mapping.active:
                continue
            usernames = {pid.username for pid in self._hub.participant_ids(mapping.channel.name)}
            already = self._announced.get(mapping.channel.id, {})
            newcomers = sorted(username for username in usernames if username not in already)
            announced: list[str] = []
            for username in sorted(usernames):
                await self._ensure_nick_color(username)
                if self._announce(mapping, username):
                    announced.append(username)
                    self._identity_notified.discard((mapping.channel.id, username))
                else:
                    await self._notify_identity_held(mapping, username)
            if notify and newcomers:
                await self._notify_bridged(mapping, [name for name in newcomers if name in announced])

    async def _notify_bridged(
        self, mapping: MrcChannelMapping, usernames: list[str], *, moved_from: str | None = None,
    ) -> None:
        """The same disclosure `netbbs.net.chat_flow` gives on joining a
        bridged channel, delivered to callers who were already inside
        when the SysOp mapped (or remapped) it."""
        settings = self._settings
        if settings is None:
            return
        for username in usernames:
            nick = protocol.nick_for_username(username)
            if moved_from is None:
                text = (
                    f"This channel is now bridged to MRC room #{sanitize_text(mapping.room)} on "
                    f"{sanitize_text(settings.host)} -- your handle {nick!r} is visible to everyone on "
                    "that network, and what you say here from now on is relayed there."
                )
            else:
                text = (
                    f"This channel's MRC room changed from #{sanitize_text(moved_from)} to "
                    f"#{sanitize_text(mapping.room)}; your handle {nick!r} now appears there."
                )
            notice = colored(text, fg_color=MUTED_COLOR)
            for participant in self._hub.participants_for_username(mapping.channel.name, username):
                # Mandatory: a full queue must evict chat, not this.
                await self._hub.send_to(mapping.channel.name, participant, notice, priority=True)

    # --- inbound -----------------------------------------------------------

    async def _handle_packet(self, packet: MrcPacket) -> None:
        settings = self._settings
        if settings is None:
            return
        if packet.is_server:
            await self._handle_server_packet(packet)
            return
        if packet.from_site.lower() == settings.site_wire_name.lower():
            return  # the hub echoing this node's own traffic
        if protocol.is_ctcp_packet(packet):
            await self._handle_ctcp(packet)
            return
        if not packet.is_broadcast:
            await self._notify_private_message(packet)
            return
        if not packet.to_room:
            # No room at all: a network-wide broadcast (ENiGMA½'s own
            # reading of an empty `to_room`), shown wherever this node
            # listens rather than filed under the sender's room.
            await self._handle_broadcast(packet)
            return
        plain = strip_pipe_codes(packet.body).strip()
        self._observe_chatter(plain)
        mapping = self._by_room.get(packet.room.lower())
        if mapping is None or not mapping.active:
            return
        if not plain:
            return
        if packet.to_user.upper() == protocol.NOTME or protocol.looks_like_presence_chatter(plain):
            await self._broadcast_notice(mapping, packet.body.strip())
            self._request_userlist(mapping, self._any_nick(mapping) or "NetBBS")
            return
        # Issue #298: the body carries the sender's own coloured handle
        # in every reference client's convention; peel it so the caller
        # sees one name, and keep the colour codes for the renderer.
        kind, text = protocol.split_sender_prefix(packet.body, packet.from_user)
        text = text.strip()
        plain_text = strip_pipe_codes(text).strip()
        if not plain_text:
            return
        author_label = f"{packet.from_user or 'unknown'}@{packet.from_site or 'unknown'} (MRC)"
        try:
            recorded = await self._lane.run(
                record_message, mapping.channel, kind=kind, author_label=author_label,
                author_fingerprint=None, body=text, external_source="mrc", index_body=plain_text,
            )
        except sqlite3.DatabaseError as exc:
            # The channel was deleted (or its row otherwise vanished)
            # underneath a mapping this bridge still cached -- e.g. from
            # the standalone admin CLI, which cannot ask the running node
            # to refresh. Forget the mapping instead of letting one
            # inbound line kill the connection and every reconnect after.
            self._dropped_inbound += 1
            self._forget_mapping(mapping)
            _logger.warning(
                "MRC room %r: dropping its mapping to channel %r after a storage error: %s",
                mapping.room, mapping.channel.name, exc,
            )
            return
        await self._hub.broadcast(mapping.channel.name, recorded)
        await self._touch(mapping)

    def _forget_mapping(self, mapping: MrcChannelMapping) -> None:
        """Drop every trace of a mapping: the lookups, the announced
        callers, and the per-room caches (roster, USERLIST timestamp,
        activity stamp) -- open rooms come and go at callers' pace, so a
        retired room must not leave a roster behind (issue #300)."""
        self._by_room.pop(mapping.room.lower(), None)
        self._by_channel.pop(mapping.channel.id, None)
        self._announced.pop(mapping.channel.id, None)
        self._announced_rooms.pop(mapping.channel.id, None)
        self._rosters.pop(mapping.room.lower(), None)
        self._last_userlist_request.pop(mapping.room.lower(), None)
        self._last_touch.pop(mapping.channel.id, None)

    async def _handle_server_packet(self, packet: MrcPacket) -> None:
        settings = self._settings
        if settings is None:
            return
        command, params = protocol.parse_server_command(packet.body)
        if command == "PING":
            self._enqueue(protocol.imalive(settings.site_wire_name, settings.site_name))
            return
        if command == "HELLO":
            self._send_site_info(settings)
            return
        if command == "OLDVERSION":
            # Fatal whenever it arrives -- during the handshake or on a
            # live session the hub left open: stop relaying now rather
            # than after the socket happens to close.
            self._fatal_error = f"hub requires a newer MRC client version ({params or 'unspecified'})"
            self._last_error = self._fatal_error
            raise ConnectionError(self._fatal_error)
        if command == "NEWUPDATE":
            _logger.info("MRC hub reports a newer client protocol version is available: %s", params)
            return
        if command == "GOODBYE":
            raise ConnectionError("hub is closing the connection")
        if command == "TERMINATE":
            # The hub ended this site's session on purpose (Mystic's
            # client treats it as final); retrying would only repeat it.
            self._fatal_error = f"hub terminated the session: {strip_pipe_codes(params) or 'no reason given'}"
            self._last_error = self._fatal_error
            raise ConnectionError(self._fatal_error)
        if command == "USERLIST":
            room_key = self._room_key_for_server_packet(packet)
            # Only rooms this node has mapped: the hub names the room, and
            # an unbounded remotely-named dictionary is exactly what the
            # inbound rate limit cannot bound on its own.
            if room_key is not None and room_key in self._by_room:
                self._rosters[room_key] = tuple(protocol.parse_userlist(params))
            return
        if command == "ROOMTOPIC":
            room, _, topic = params.partition(":")
            mapping = self._by_room.get(strip_pipe_codes(room).strip().lower())
            if mapping is not None and mapping.active:
                if mapping.is_open_room:
                    # Issue #304: an open room's topic is the hub's; keep it
                    # on the row so the status line shows it.
                    plain = strip_pipe_codes(topic).strip() or None
                    try:
                        await self._lane.run(set_open_room_topic, mapping.channel, plain)
                    except sqlite3.DatabaseError as exc:
                        _logger.warning("MRC room %r: could not store its topic: %s", mapping.room, exc)
                await self._broadcast_notice(mapping, f"room topic: {topic.strip()}")
            return
        if command == "BANNER" and packet.to_user.upper() in ("", protocol.CLIENT, protocol.ALL):
            text = params.strip()
            if text:
                self._banner.append(text)
                del self._banner[:-10]
            return
        if command in ("PROTOCOLVERSION", "PONG"):
            return
        addressed = self._caller_for_nick(packet.to_user)
        if addressed is not None:
            channel_id, username = addressed
            if command == "USERROOM":
                await self._handle_userroom(channel_id, username, strip_pipe_codes(params).strip())
                return
            if command == "USERNICK":
                await self._handle_usernick(channel_id, username, strip_pipe_codes(params).strip())
                return
            if command == "STATS":
                # Parsed for everyone; shown only to a caller who asked.
                self._record_stats(params)
                if username not in self._stats_requested:
                    return
                self._stats_requested.discard(username)
            # Issue #298: everything else the hub says *to one caller* is
            # the reply to something they asked (LIST, CHATTERS, INFO,
            # MOTD, STATS, HELP ...) -- plain text lines, shown to them
            # alone and bounded per caller.
            await self._deliver_reply(username, packet.body.strip())
            return
        if packet.to_user.upper() not in ("", protocol.CLIENT, protocol.ALL):
            return  # addressed to a nick this node never announced
        if command in ("STATS", "LATENCY", "BANNER", "MOTD"):
            return  # site-wide chatter with no room to show it in
        self._observe_chatter(strip_pipe_codes(packet.body))
        mapping = self._by_room.get(packet.room.lower())
        if mapping is None or not mapping.active:
            return
        body = packet.body.strip()
        if strip_pipe_codes(body).strip():
            await self._broadcast_notice(mapping, body)
            if protocol.looks_like_presence_chatter(strip_pipe_codes(body)):
                self._request_userlist(mapping, self._any_nick(mapping) or "NetBBS")

    async def _handle_userroom(self, channel_id: int, username: str, room: str) -> None:
        """`USERROOM:<room>` to one of this node's nicks: the hub moved
        the caller (bounced from a password room, or a hub-side merge).
        The channel stays bridged to its mapped room, so the caller is
        told and re-announced there. Per keepalive tick: one re-announce,
        two notices, then silence -- a room that keeps bouncing them is
        neither a NEWROOM loop nor a stream of priority notices."""
        mapping = self._by_channel.get(channel_id)
        settings = self._settings
        if mapping is None or settings is None or not room or room.lower() == mapping.room.lower():
            return
        self._observe_room(room)
        nick = self._announced.get(channel_id, {}).get(username)
        if nick is None:
            return
        key = (channel_id, username)
        moves = self._rehomed.get(key, 0)
        self._rehomed[key] = moves + 1
        if moves == 0:
            self._enqueue(protocol.newroom(nick, settings.site_wire_name, room, mapping.room))
            text = (
                f"The MRC hub moved you to #{room}; this channel is bridged to #{mapping.room}, "
                "so you have been announced there again."
            )
        elif moves == 1:
            text = (
                f"The MRC hub keeps moving you to #{room}. Until it lets you stay in #{mapping.room}, "
                "what you say here will not reach MRC."
            )
        else:
            # Two priority notices per keepalive tick is the whole story;
            # a hub repeating itself past that must not keep evicting chat.
            return
        await self._deliver_to_caller(username, MrcNotice(text, utc_now_iso()), priority=True)

    async def _handle_usernick(self, channel_id: int, username: str, new_nick: str) -> None:
        """`USERNICK:<nick>` to one of this node's nicks: the hub renamed
        the caller (a registered handle, a collision). Track the new
        name so replies and CTCP still find them, and say so."""
        new_nick = protocol.sanitize_name(new_nick)
        nicks = self._announced.get(channel_id)
        if not new_nick or nicks is None or username not in nicks or nicks[username].lower() == new_nick.lower():
            return
        nicks[username] = new_nick
        await self._deliver_to_caller(
            username, MrcNotice(f"The MRC hub now knows you as {new_nick!r}.", utc_now_iso()), priority=True,
        )

    async def _handle_broadcast(self, packet: MrcPacket) -> None:
        plain = strip_pipe_codes(packet.body).strip()
        if not plain:
            return
        kind, text = protocol.split_sender_prefix(packet.body.strip(), packet.from_user)
        if not strip_pipe_codes(text).strip():
            return
        sender = f"{packet.from_user or 'unknown'}@{packet.from_site or 'unknown'}"
        notice = MrcNotice(f"{sender}: {text.strip()}", utc_now_iso(), kind="broadcast")
        for mapping in self._by_channel.values():
            if mapping.active:
                await self._hub.broadcast(mapping.channel.name, notice)

    async def _handle_ctcp(self, packet: MrcPacket) -> None:
        """Issue #298: answer a `[CTCP] requester target COMMAND` aimed at
        one of this node's nicks, and show a `[CTCP-REPLY]` to the caller
        who asked for it. Bounded per remote sender: every request costs
        this node a reply."""
        settings = self._settings
        if settings is None:
            return
        request = protocol.parse_ctcp_request(packet.body)
        if request is not None:
            addressed = self._caller_for_nick(request.target) or self._caller_for_nick(packet.to_user)
            if addressed is None or request.command not in protocol.CTCP_COMMANDS:
                return
            key = (packet.from_site.lower(), packet.from_user.lower())
            bucket = self._ctcp_buckets.get(key)
            if bucket is None:
                if len(self._ctcp_buckets) >= MAX_TRACKED_CTCP_SENDERS:
                    self._ctcp_buckets.clear()
                bucket = _TokenBucket(CTCP_BURST, CTCP_RATE_PER_SECOND, self._clock)
                self._ctcp_buckets[key] = bucket
            if not bucket.has_token():
                self._dropped_inbound += 1
                return
            bucket.consume()
            channel_id, username = addressed
            nick = self._announced.get(channel_id, {}).get(username) or request.target
            if request.command == "VERSION":
                text = f"NetBBS {self._version}"
            elif request.command == "TIME":
                text = utc_now_iso()
            elif request.command == "PING":
                text = request.params
            else:  # CLIENTINFO
                text = " ".join(protocol.CTCP_COMMANDS)
            self._enqueue(protocol.ctcp_reply(nick, settings.site_wire_name, packet.from_user, request.command, text))
            return
        reply = protocol.parse_ctcp_reply(packet.body)
        if reply is None:
            return
        addressed = self._caller_for_nick(packet.to_user)
        if addressed is None:
            return
        command, text = reply
        await self._deliver_reply(
            addressed[1], f"CTCP {command} reply from {packet.from_user}@{packet.from_site}: {text}".rstrip(": "),
        )

    def _caller_for_nick(self, nick: str) -> tuple[int, str] | None:
        """The announced caller behind `nick` as `(channel id, username)`,
        or `None` -- the hub addresses replies, moves, renames and CTCP
        to the nick, never to the account."""
        target = nick.lower()
        if not target or target in {name.lower() for name in protocol.RESERVED_NAMES}:
            return None
        for channel_id, nicks in self._announced.items():
            for username, announced_nick in nicks.items():
                if announced_nick.lower() == target:
                    return channel_id, username
        return None

    async def _deliver_to_caller(self, username: str, notice: MrcNotice, *, priority: bool = False) -> None:
        for channel_id in list(self._announced):
            if username not in self._announced.get(channel_id, {}):
                continue
            mapping = self._by_channel.get(channel_id)
            if mapping is None:
                continue
            for participant in self._hub.participants_for_username(mapping.channel.name, username):
                await self._hub.send_to(mapping.channel.name, participant, notice, priority=priority)

    async def _deliver_reply(self, username: str, text: str) -> None:
        """One line of the hub's reply to `username`, under that caller's
        own reply allowance; the first line dropped in a burst is
        replaced by a single "cut short" notice."""
        bucket = self._reply_buckets.get(username)
        if bucket is None:
            if len(self._reply_buckets) >= 500:
                self._reply_buckets.clear()
            bucket = _TokenBucket(self._reply_burst, REPLY_RATE_PER_SECOND, self._clock)
            self._reply_buckets[username] = bucket
        if not bucket.has_token():
            self._dropped_inbound += 1
            if username not in self._reply_truncated:
                self._reply_truncated.add(username)
                await self._deliver_to_caller(
                    username,
                    MrcNotice("(the hub's reply was cut short -- it sent more lines than are shown at once)", utc_now_iso(), kind="reply"),
                    priority=True,
                )
            return
        # The latch lifts only once the allowance has genuinely recovered
        # (half the burst back), not on the first trickle-admitted line:
        # a hub streaming just above the refill rate would otherwise earn
        # a fresh priority notice at every refill.
        if bucket.has_tokens(self._reply_burst / 2):
            self._reply_truncated.discard(username)
        bucket.consume()
        await self._deliver_to_caller(username, MrcNotice(text, utc_now_iso(), kind="reply"))

    # --- open rooms (issue #300) ---------------------------------------------

    @property
    def open_rooms_enabled(self) -> bool:
        """Whether callers may open MRC rooms here: MRC on node-wide
        *and* the open-room switch on."""
        return (
            self._settings is not None and self._settings.enabled
            and self._open_settings is not None and self._open_settings.enabled
        )

    @property
    def open_room_settings(self) -> OpenRoomSettings | None:
        return self._open_settings

    async def open_room(self, room: str, username: str) -> MrcChannelMapping:
        """Open MRC `room` for `username`: materialize (or find) its
        channel and make the bridge treat it as an active mapping now,
        so the caller's `NEWROOM` goes out through the ordinary announce
        path the moment they enter. Raises `MrcSettingsError` with the
        reason the caller sees (switched off, blocked, at the cap)."""
        if not self.open_rooms_enabled or self._open_settings is None:
            raise MrcSettingsError("Opening MRC rooms is switched off on this node.")
        open_settings = self._open_settings
        mapping = await self._lane.run(materialize_open_room, room, open_settings=open_settings)
        self._observe_room(mapping.room)
        await self.refresh_channel_mappings()
        return mapping

    def mapping_for_room(self, room: str) -> MrcChannelMapping | None:
        """The channel already carrying MRC room `room` here -- a SysOp-
        mapped channel or an open room -- or `None`. Resolved before any
        open-room gate is applied: entering an existing channel is that
        channel's own decision, the node-wide defaults only govern
        materializing a new row."""
        return self._by_room.get(protocol.sanitize_room(room).lower())

    def note_entry(self, channel: Channel) -> None:
        """A session has passed the pre-join checks for `channel` and is
        about to join it: hold the room against the sweeper (see
        `_entering`)."""
        now = self._clock()
        self._entering = {
            channel_id: stamp for channel_id, stamp in self._entering.items()
            if now - stamp < OPEN_ROOM_TOUCH_INTERVAL_SECONDS
        }
        self._entering[channel.id] = now

    def identity_room_held(self, username: str, *, leaving: Channel | None = None) -> str | None:
        """The MRC room `username` currently occupies through any active
        bridged channel, or `None` -- the target-agnostic form of
        `identity_room_elsewhere`, for the moment *before* a room exists
        (opening a new one must not spend the cap on a caller whose
        identity is already elsewhere)."""
        return self.identity_room_elsewhere(None, username, leaving=leaving)

    def identity_room_elsewhere(
        self, channel: Channel | None, username: str, *, leaving: Channel | None = None,
    ) -> str | None:
        """The MRC room `username` is already announced in through a
        channel other than `channel`, or `None`. An MRC identity is one
        nick at one site in one room: a second session of the same
        account cannot be in two rooms at once (decision 6, issue #300),
        so the caller is refused with this room's name.

        `leaving` is the channel the asking session is about to leave
        (`/join` from inside a room): it does not count unless another
        session of the same account is still in it, since that session
        keeps the identity there after this one has gone.

        Decided from the `ChatHub`'s occupancy of every active bridged
        channel, not from what has been announced to the hub: the
        announced set is empty while the link is down and only catches
        up on reconnect, and a reservation that lapsed during backoff
        would let two sessions settle in two rooms and then fight over
        one nick when the link returns."""
        for mapping in self._by_channel.values():
            if (channel is not None and mapping.channel.id == channel.id) or not mapping.active:
                continue
            sessions = self._hub.participants_for_username(mapping.channel.name, username)
            if not sessions:
                continue
            if leaving is not None and mapping.channel.id == leaving.id and len(sessions) <= 1:
                continue
            return mapping.room
        return None

    def room_blocked(self, room: str) -> bool:
        """Whether the SysOp's current blocklist names `room` -- checked
        on every way into an open room, not only when it is first
        opened, so a room blocked after callers opened it stops
        admitting them until the sweeper retires it."""
        return self._open_settings is not None and self._open_settings.blocks(room)

    def observed_rooms(self) -> list[str]:
        """Rooms this node has heard of, most recently seen first: rooms
        callers opened, `USERROOM` targets, join/leave chatter naming a
        room. Advisory -- joining by name is the mechanism; this is the
        list beside it."""
        ordered = sorted(self._observed_rooms.values(), key=lambda entry: entry[1], reverse=True)
        return [display for display, _seen in ordered]

    def _observe_room(self, room: str) -> None:
        room = protocol.sanitize_room(room)
        if not room:
            return
        key = room.lower()
        if key not in self._observed_rooms and len(self._observed_rooms) >= MAX_OBSERVED_ROOMS:
            oldest = min(self._observed_rooms.items(), key=lambda item: item[1][1])[0]
            del self._observed_rooms[oldest]
        self._observed_rooms[key] = (room, self._clock())

    def _observe_chatter(self, plain: str) -> None:
        match = _ROOM_CHATTER_RE.match(plain.strip())
        if match is not None:
            self._observe_room(match.group("room"))

    async def _touch(self, mapping: MrcChannelMapping) -> None:
        """Stamp activity on an open room for the sweeper, at most once
        per `OPEN_ROOM_TOUCH_INTERVAL_SECONDS` per channel -- one lane
        write per minute, never one per line."""
        if not mapping.is_open_room:
            return
        now = self._clock()
        if now - self._last_touch.get(mapping.channel.id, -1e9) < OPEN_ROOM_TOUCH_INTERVAL_SECONDS:
            return
        self._last_touch[mapping.channel.id] = now
        try:
            await self._lane.run(touch_open_room, mapping.channel)
        except sqlite3.DatabaseError as exc:
            _logger.warning("MRC open room %r: could not stamp activity: %s", mapping.room, exc)

    async def _sweep_open_rooms(self) -> None:
        """Retire idle open rooms (see `sweep_open_rooms`): nobody in
        them per the `ChatHub`, idle past the retention, followed by no
        one. Runs from `_sweeper_loop` whether or not the switch is on
        and whether or not the hub is reachable -- rooms opened earlier
        still age out, and a stranded cap never needs a hand."""
        open_settings = self._open_settings
        if open_settings is None:
            return
        now = self._clock()
        occupied = {
            mapping.channel.id for mapping in self._by_channel.values()
            if self._hub.participant_count(mapping.channel.name) > 0
        } | {
            channel_id for channel_id, stamp in self._entering.items()
            if now - stamp < OPEN_ROOM_TOUCH_INTERVAL_SECONDS
        }
        try:
            retired = await self._lane.run(
                sweep_open_rooms, retention_days=open_settings.retention_days, occupied_channel_ids=occupied,
            )
        except sqlite3.DatabaseError as exc:
            _logger.warning("MRC open-room sweep failed: %s", exc)
            return
        if not retired:
            return
        self._retired_rooms += len(retired)
        for mapping in retired:
            self._forget_mapping(mapping)
            self._last_touch.pop(mapping.channel.id, None)
            _logger.info(
                "MRC open room #%s retired after %d idle day(s) (channel %r and its scrollback removed)",
                mapping.room, open_settings.retention_days, mapping.channel.name,
            )
        await self.refresh_channel_mappings()

    # --- caller-initiated hub commands (issue #298) ------------------------

    def send_hub_command(self, channel: Channel, username: str, command: str) -> str | None:
        """Send `command` (`LIST`, `CHATTERS`, `MOTD`, ...) to the hub as
        the caller's own announced nick; the reply comes back through
        `_deliver_reply`. Returns `None` when queued, else the reason it
        was not (offline, not bridged, or the caller's own allowance
        spent), for the caller's screen."""
        mapping = self._by_channel.get(channel.id)
        settings = self._settings
        if mapping is None or not mapping.active or settings is None or not settings.enabled:
            return "this channel isn't bridged to MRC"
        if self._state is not MrcState.CONNECTED:
            return "the MRC link is offline"
        nick = self._announced.get(channel.id, {}).get(username)
        if nick is None:
            return "you aren't announced to the hub yet"
        body = protocol.sanitize_body(command)
        if not body:
            return "nothing to send"
        if len(body) > protocol.MAX_BODY:
            return f"that command is longer than MRC allows ({protocol.MAX_BODY} characters)"
        bucket = self._user_bucket(username)
        if not bucket.has_token():
            self._dropped_outbound += 1
            return "you're sending faster than MRC allows"
        bucket.consume()
        if body.split(" ", 1)[0].upper() == "STATS":
            self._stats_requested.add(username)
        self._enqueue(protocol.user_command(nick, settings.site_wire_name, mapping.room, body))
        return None

    def send_secret_command(self, channel: Channel, username: str, command: str, secret: str) -> str | None:
        """`IDENTIFY <secret>` and friends (issue #304): the secret is
        validated against what the wire can carry -- printable ASCII
        33-125, no tilde, no space, one packet -- and sent verbatim; it is
        never passed through the chat sanitizer, which would silently
        rewrite a pipe-code-shaped substring and change the credential."""
        mapping = self._by_channel.get(channel.id)
        settings = self._settings
        if mapping is None or not mapping.active or settings is None or not settings.enabled:
            return "this channel isn't bridged to MRC"
        if self._state is not MrcState.CONNECTED:
            return "the MRC link is offline"
        nick = self._announced.get(channel.id, {}).get(username)
        if nick is None:
            return "you aren't announced to the hub yet"
        if not secret or any(ch in "~ " or not 33 <= ord(ch) <= 125 for ch in secret):
            return "MRC passwords are printable ASCII without spaces or tildes; that one cannot be sent as typed"
        body = f"{command} {secret}"
        if len(body) > protocol.MAX_BODY:
            return f"that password is longer than MRC allows ({protocol.MAX_BODY - len(command) - 1} characters)"
        bucket = self._user_bucket(username)
        if not bucket.has_token():
            self._dropped_outbound += 1
            return "you're sending faster than MRC allows"
        bucket.consume()
        self._enqueue(protocol.user_command(nick, settings.site_wire_name, mapping.room, body))
        return None

    def send_ctcp(self, channel: Channel, username: str, target: str, command: str) -> str | None:
        """Ask another MRC user's client something (`VERSION`, `TIME`,
        `PING`, `CLIENTINFO`); the reply is shown to the caller."""
        mapping = self._by_channel.get(channel.id)
        settings = self._settings
        if mapping is None or not mapping.active or settings is None or not settings.enabled:
            return "this channel isn't bridged to MRC"
        if self._state is not MrcState.CONNECTED:
            return "the MRC link is offline"
        nick = self._announced.get(channel.id, {}).get(username)
        if nick is None:
            return "you aren't announced to the hub yet"
        target = protocol.sanitize_name(target)
        command = command.strip().upper()
        if not target or command not in protocol.CTCP_COMMANDS:
            return "usage: /mrc ctcp <nick> " + "|".join(protocol.CTCP_COMMANDS)
        bucket = self._user_bucket(username)
        if not bucket.has_token():
            self._dropped_outbound += 1
            return "you're sending faster than MRC allows"
        bucket.consume()
        params = utc_now_iso() if command == "PING" else ""
        self._enqueue(protocol.ctcp_request(nick, settings.site_wire_name, target, command, params))
        return None

    def _room_key_for_server_packet(self, packet: MrcPacket) -> str | None:
        if packet.room:
            return packet.room.lower()
        target = packet.to_user.lower()
        for channel_id, nicks in self._announced.items():
            if target in (nick.lower() for nick in nicks.values()):
                mapping = self._by_channel.get(channel_id)
                if mapping is not None:
                    return mapping.room.lower()
        return None

    def _any_nick(self, mapping: MrcChannelMapping) -> str | None:
        nicks = self._announced.get(mapping.channel.id)
        if not nicks:
            return None
        return next(iter(nicks.values()))

    async def _broadcast_notice(self, mapping: MrcChannelMapping, text: str) -> None:
        """Ephemeral, never recorded: an `MrcNotice` the receive loop in
        `netbbs.net.chat_flow` renders per viewer. `text` is already
        sanitized by `parse_line` and keeps its colour codes."""
        await self._hub.broadcast(mapping.channel.name, MrcNotice(text, utc_now_iso()))

    async def _notify_private_message(self, packet: MrcPacket) -> None:
        target = packet.to_user.lower()
        for channel_id, nicks in self._announced.items():
            for username, nick in nicks.items():
                if nick.lower() != target:
                    continue
                # An MRC identity is nick *and* site, exactly as the notice
                # below names it.
                key = (packet.from_user.lower(), packet.from_site.lower(), channel_id, username)
                if key in self._private_notified:
                    return
                if len(self._private_notified) >= MAX_TRACKED_PRIVATE_SENDERS:
                    self._private_notified.clear()
                self._private_notified.add(key)
                mapping = self._by_channel.get(channel_id)
                if mapping is None:
                    return
                notice = colored(
                    f"[MRC] {sanitize_text(packet.from_user)}@{sanitize_text(packet.from_site)} tried to message "
                    "you privately. Private MRC chat isn't bridged; only room traffic is.",
                    fg_color=MUTED_COLOR,
                )
                for participant in self._hub.participants_for_username(mapping.channel.name, username):
                    await self._hub.send_to(mapping.channel.name, participant, notice)
                return

    # --- status --------------------------------------------------------------

    def status(self) -> MrcStatus:
        settings = self._settings
        rooms: dict[str, tuple[str, ...]] = {}
        for mapping in self._by_channel.values():
            rooms[mapping.room] = self._rosters.get(mapping.room.lower(), ())
        return MrcStatus(
            state=self._state,
            enabled=bool(settings and settings.enabled),
            host=settings.host if settings else "",
            port=settings.port if settings else 0,
            tls=settings.tls if settings else True,
            site_name=settings.site_name if settings else "",
            connected_since=self._connected_since,
            last_error=self._last_error,
            attempts=self._attempts,
            bridged_channels=len(self._by_channel),
            participants=sum(len(nicks) for nicks in self._announced.values()),
            dropped_outbound=self._dropped_outbound,
            dropped_inbound=self._dropped_inbound,
            rooms=rooms,
            open_rooms_enabled=self.open_rooms_enabled,
            open_rooms=sum(1 for mapping in self._by_channel.values() if mapping.is_open_room),
            open_room_cap=self._open_settings.cap if self._open_settings is not None else 0,
            observed_rooms=len(self._observed_rooms),
            retired_rooms=self._retired_rooms,
            network_bbses=self._network_stats[0] if self._network_stats else None,
            network_rooms=self._network_stats[1] if self._network_stats else None,
            network_users=self._network_stats[2] if self._network_stats else None,
            network_stats_age_seconds=(
                self._clock() - self._network_stats_at if self._network_stats_at is not None else None
            ),
            network_stats_raw=self._network_stats_raw,
        )

    @property
    def state(self) -> MrcState:
        return self._state


def _describe_error(exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return "timed out connecting to the hub"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return f"TLS certificate verification failed: {exc.verify_message or exc}"
    if isinstance(exc, ssl.SSLError):
        return f"TLS handshake failed: {exc.reason or exc}"
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    text = str(exc)
    return text or exc.__class__.__name__
