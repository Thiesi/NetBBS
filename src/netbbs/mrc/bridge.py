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
from netbbs.chat.scrollback import ChannelMessage, record_message
from netbbs.mrc import protocol
from netbbs.mrc.protocol import MrcPacket, parse_line
from netbbs.mrc.settings import MrcChannelMapping, MrcSettings, list_mrc_mappings, load_mrc_settings
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
        load_settings: Callable[[Database], MrcSettings] = load_mrc_settings,
        load_mappings: Callable[[Database], list[MrcChannelMapping]] = list_mrc_mappings,
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
        self._load_settings = load_settings
        self._load_mappings = load_mappings
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

        self._outbound: asyncio.Queue[str] = asyncio.Queue(maxsize=outbound_queue_size)
        self._node_bucket = _TokenBucket(OUTBOUND_BURST, OUTBOUND_RATE_PER_SECOND, clock)
        self._user_buckets: dict[str, _TokenBucket] = {}
        self._inbound_bucket = _TokenBucket(INBOUND_BURST, INBOUND_RATE_PER_SECOND, clock)

        self._state = MrcState.DISABLED
        self._connector_task: asyncio.Task | None = None
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

    async def close(self) -> None:
        self._stopping = True
        await self._stop_connector(graceful=True)

    async def reload_settings(self) -> None:
        """Re-read the hub settings and channel mappings from the
        database and reconnect (or disconnect) accordingly -- the SysOp
        settings screen's "apply" path. A fatal rejection from the
        previous settings is cleared: the SysOp may just have fixed it."""
        async with self._reload_lock:
            await self._stop_connector(graceful=True)
            self._fatal_error = None
            self._attempts = 0
            await self._reload_from_db()
            self._notify_on_connect = True
            if not self._stopping:
                self._start_connector_if_enabled()

    async def refresh_channel_mappings(self) -> None:
        """Re-read only the per-channel mappings (after a SysOp maps,
        unmaps or pauses a channel) and reconcile announced callers
        without dropping the hub connection."""
        async with self._reload_lock:
            self._apply_mappings(await self._lane.run(self._load_mappings))
            if self._state is MrcState.CONNECTED:
                await self._reconcile_announced(notify=True)
            if self._settings is not None and self._settings.enabled and self._connector_task is None:
                self._notify_on_connect = True
                self._start_connector_if_enabled()

    async def _reload_from_db(self) -> None:
        def _load(db: Database) -> tuple[MrcSettings, list[MrcChannelMapping]]:
            return self._load_settings(db), self._load_mappings(db)

        settings, mappings = await self._lane.run(_load)
        self._settings = settings
        self._apply_mappings(mappings)

    def _apply_mappings(self, mappings: list[MrcChannelMapping]) -> None:
        self._by_room = {mapping.room.lower(): mapping for mapping in mappings}
        self._by_channel = {mapping.channel.id: mapping for mapping in mappings}

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
        self._connected_at_monotonic = self._clock()
        self._last_error = None
        _logger.info("Connected to MRC hub %s:%d as %r", settings.host, settings.port, settings.site_name)
        self._drain_outbound_queue()
        self._send_site_info(settings)
        notify, self._notify_on_connect = self._notify_on_connect, False
        await self._reconcile_announced(notify=notify)

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
            return nick.lower() in own and (not site or site.lower() == own_site)

        return sorted((name for name in roster if not _is_own(name)), key=str.lower)

    async def local_join(self, channel: Channel, username: str) -> None:
        """A caller entered `channel`. Announces them to the hub if the
        channel is bridged and they aren't already announced (a second
        session of the same account is still one MRC user)."""
        mapping = self._by_channel.get(channel.id)
        if mapping is None or not mapping.active or self._state is not MrcState.CONNECTED:
            return
        self._announce(mapping, username)

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
        mapping = self._by_channel.get(channel.id)
        settings = self._settings
        if mapping is None or settings is None or self._state is not MrcState.CONNECTED:
            return
        self._enqueue(protocol.logoff(nick, settings.site_wire_name, mapping.room))

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
        if self._state is not MrcState.CONNECTED:
            return False, False
        if message.kind not in ("message", "action") or not message.body:
            return False, False
        username = message.author_label
        nicks = self._announced.setdefault(channel.id, {})
        nick = nicks.get(username)
        if nick is None:
            self._announce(mapping, username)
            nick = nicks[username]
        body = protocol.sanitize_body(message.body)
        if not body:
            return False, False
        # Issue #298: every chunk carries the caller's handle in the
        # network's own body convention, so the words reach other boards
        # with a name attached; the prefix is paid for out of the same
        # 140-character budget as the words.
        template = protocol.format_action_body if message.kind == "action" else protocol.format_room_body
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

    def _announce(self, mapping: MrcChannelMapping, username: str) -> None:
        settings = self._settings
        if settings is None:
            return
        nicks = self._announced.setdefault(mapping.channel.id, {})
        if username in nicks:
            return
        nick = protocol.nick_for_username(username)
        nicks[username] = nick
        self._announced_rooms[mapping.channel.id] = mapping.room
        self._enqueue(protocol.newroom(nick, settings.site_wire_name, "", mapping.room))
        self._request_userlist(mapping, nick)

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
            for username in sorted(usernames):
                self._announce(mapping, username)
            if notify and newcomers:
                await self._notify_bridged(mapping, newcomers)

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
        mapping = self._by_room.get(packet.room.lower())
        if mapping is None or not mapping.active:
            return
        plain = strip_pipe_codes(packet.body).strip()
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

    def _forget_mapping(self, mapping: MrcChannelMapping) -> None:
        self._by_room.pop(mapping.room.lower(), None)
        self._by_channel.pop(mapping.channel.id, None)
        self._announced.pop(mapping.channel.id, None)
        self._announced_rooms.pop(mapping.channel.id, None)

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
                await self._broadcast_notice(mapping, f"room topic: {topic.strip()}")
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
