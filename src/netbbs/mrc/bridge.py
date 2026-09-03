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

Every task this bridge creates is cancelled and gathered by `close()`
on every exit path.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import random
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
from netbbs.rendering.sanitize import sanitize_text
from netbbs.rendering.theme import MUTED_COLOR
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import utc_now_iso

MRC_LOGGER_NAME = "netbbs.mrc"
_logger = logging.getLogger(MRC_LOGGER_NAME)

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

        self._settings: MrcSettings | None = None
        # room (lower-cased) -> mapping; channel id -> mapping
        self._by_room: dict[str, MrcChannelMapping] = {}
        self._by_channel: dict[int, MrcChannelMapping] = {}
        # channel id -> {username: nick} of callers currently announced
        # to the hub.
        self._announced: dict[int, dict[str, str]] = {}
        # room (lower) -> roster from the hub's last USERLIST reply
        self._rosters: dict[str, tuple[str, ...]] = {}
        self._last_userlist_request: dict[str, float] = {}
        # (sender lower, channel id, username) -> already notified about
        # an undeliverable private message
        self._private_notified: set[tuple[str, int, str]] = set()

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
            if not self._stopping:
                self._start_connector_if_enabled()

    async def refresh_channel_mappings(self) -> None:
        """Re-read only the per-channel mappings (after a SysOp maps,
        unmaps or pauses a channel) and reconcile announced callers
        without dropping the hub connection."""
        async with self._reload_lock:
            self._apply_mappings(await self._lane.run(self._load_mappings))
            if self._state is MrcState.CONNECTED:
                await self._reconcile_announced()
            if self._settings is not None and self._settings.enabled and self._connector_task is None:
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
        self._rosters.clear()
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
            connected_at = self._clock()
            stable = False
            try:
                await self._connect_and_serve(settings)
                stable = self._clock() - connected_at >= self._stable_after
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = _describe_error(exc)
                _logger.warning("MRC hub %s:%d: %s", settings.host, settings.port, self._last_error)
            finally:
                await self._close_connection()
                self._announced.clear()
                self._rosters.clear()
            if self._stopping:
                return
            if self._fatal_error is not None:
                self._state = MrcState.ERROR
                _logger.error("MRC hub rejected this node; not retrying until settings change: %s", self._fatal_error)
                return
            if stable:
                backoff = self._min_backoff
            self._state = MrcState.BACKOFF
            await asyncio.sleep(self._rng.uniform(0, backoff))
            backoff = min(backoff * 2, self._max_backoff)

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
        self._last_error = None
        _logger.info("Connected to MRC hub %s:%d as %r", settings.host, settings.port, settings.site_name)
        self._drain_outbound_queue()
        self._send_site_info(settings)
        await self._reconcile_announced()

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
        Anything else before it (a banner, a version notice) is passed
        through the ordinary inbound path; `OLDVERSION` there is fatal."""
        while True:
            line = await reader.readline()
            if not line:
                raise ConnectionError("hub closed the connection during handshake")
            packet = parse_line(line.decode("ascii", errors="replace"))
            if packet is None:
                continue
            if packet.is_server and packet.body.strip().upper() == "HELLO":
                return
            await self._handle_packet(packet)
            if self._fatal_error is not None:
                raise ConnectionError(self._fatal_error)

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

    async def _handle_raw_line(self, raw: bytes) -> None:
        if len(raw) > protocol.MAX_LINE * 2:
            self._dropped_inbound += 1
            return
        packet = parse_line(raw.decode("ascii", errors="replace"))
        if packet is None:
            if raw.strip():
                self._dropped_inbound += 1
            return
        if not self._inbound_bucket.has_token():
            self._dropped_inbound += 1
            return
        self._inbound_bucket.consume()
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
        this node's own callers, sorted case-insensitively."""
        mapping = self._by_channel.get(channel.id)
        if mapping is None:
            return []
        own = {nick.lower() for nick in self._announced.get(channel.id, {}).values()}
        roster = self._rosters.get(mapping.room.lower(), ())
        return sorted(
            (name for name in roster if name.split("@", 1)[0].lower() not in own),
            key=str.lower,
        )

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
        if message.kind == "action":
            body = f"* {nick} {body}"
        if not body:
            return False, False
        chunks, truncated = protocol.split_body(body)
        bucket = self._user_bucket(username)
        for chunk in chunks:
            if not bucket.has_token():
                self._dropped_outbound += 1
                return False, truncated
            bucket.consume()
            self._enqueue(protocol.chat_message(nick, settings.site_wire_name, mapping.room, chunk))
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
        self._enqueue(protocol.newroom(nick, settings.site_wire_name, "", mapping.room))
        self._request_userlist(mapping, nick)

    async def _reconcile_announced(self) -> None:
        """Make the announced set match "callers currently in an active
        bridged channel" per the `ChatHub`: announce newcomers (a
        mapping added while callers were already inside; every caller
        after a reconnect), LOGOFF anyone whose channel was unmapped or
        paused."""
        settings = self._settings
        if settings is None or self._state is not MrcState.CONNECTED:
            return
        for channel_id, nicks in list(self._announced.items()):
            mapping = self._by_channel.get(channel_id)
            if mapping is not None and mapping.active:
                continue
            room = mapping.room if mapping is not None else ""
            for nick in nicks.values():
                self._enqueue(protocol.logoff(nick, settings.site_wire_name, room))
            self._announced.pop(channel_id, None)
        for mapping in self._by_channel.values():
            if not mapping.active:
                continue
            usernames = {pid.username for pid in self._hub.participant_ids(mapping.channel.name)}
            for username in sorted(usernames):
                self._announce(mapping, username)

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
        if not packet.is_broadcast:
            await self._notify_private_message(packet)
            return
        mapping = self._by_room.get(packet.room.lower())
        if mapping is None or not mapping.active:
            return
        body = protocol.strip_pipe_codes(packet.body).strip()
        if not body:
            return
        if packet.to_user.upper() == protocol.NOTME or protocol.looks_like_presence_chatter(body):
            await self._broadcast_notice(mapping, body)
            self._request_userlist(mapping, self._any_nick(mapping) or "NetBBS")
            return
        author_label = f"{packet.from_user or 'unknown'}@{packet.from_site or 'unknown'} (MRC)"
        recorded = await self._lane.run(
            record_message, mapping.channel, kind="message", author_label=author_label,
            author_fingerprint=None, body=body,
        )
        await self._hub.broadcast(mapping.channel.name, recorded)

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
            self._fatal_error = f"hub requires a newer MRC client version ({params or 'unspecified'})"
            self._last_error = self._fatal_error
            return
        if command == "NEWUPDATE":
            _logger.info("MRC hub reports a newer client protocol version is available: %s", params)
            return
        if command == "GOODBYE":
            raise ConnectionError("hub is closing the connection")
        if command == "USERLIST":
            room_key = self._room_key_for_server_packet(packet)
            if room_key is not None:
                self._rosters[room_key] = tuple(protocol.parse_userlist(params))
            return
        if command == "ROOMTOPIC":
            room, _, topic = params.partition(":")
            mapping = self._by_room.get(room.strip().lower())
            if mapping is not None and mapping.active:
                await self._broadcast_notice(mapping, f"room topic: {protocol.strip_pipe_codes(topic).strip()}")
            return
        if command in ("STATS", "LATENCY", "BANNER", "MOTD", "PROTOCOLVERSION", "PONG"):
            return
        if packet.to_user.upper() not in ("", protocol.CLIENT, protocol.ALL):
            return  # per-user server chatter (MOTD text, banners) -- not for the room
        mapping = self._by_room.get(packet.room.lower())
        if mapping is None or not mapping.active:
            return
        body = protocol.strip_pipe_codes(packet.body).strip()
        if body:
            await self._broadcast_notice(mapping, body)
            if protocol.looks_like_presence_chatter(body):
                self._request_userlist(mapping, self._any_nick(mapping) or "NetBBS")

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
        """Ephemeral, never recorded: the same plain-string path
        `netbbs.net.chat_flow`'s receive loop already renders for
        `/topic`-style notices. Sanitized before styling."""
        await self._hub.broadcast(
            mapping.channel.name, colored(f"[MRC] {sanitize_text(text)}", fg_color=MUTED_COLOR)
        )

    async def _notify_private_message(self, packet: MrcPacket) -> None:
        target = packet.to_user.lower()
        for channel_id, nicks in self._announced.items():
            for username, nick in nicks.items():
                if nick.lower() != target:
                    continue
                key = (packet.from_user.lower(), channel_id, username)
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
