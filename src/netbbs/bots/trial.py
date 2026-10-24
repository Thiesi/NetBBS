"""Try a bot against a scratch node (issue #1374, decision 4).

The real `BotRuntime` runs the bot against a scratch `Database`,
`DatabaseLane`, `ChatHub` and `PresenceRegistry` in a temporary directory,
so every host check (addressing, grants, the line and state limits, rate
limits, tokens) applies as it will live. Nothing touches the node's
database, its `.bots` state, its rate history or its chat, and live callers
never see a trial. Model actions are rehearsed, never sent.

The runtime's opt-in `trial` hook (`TrialLog`) records each host event and
bot action, the scratch channel's chat lines and a bounded stderr excerpt.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import secrets
import tempfile
import time
from pathlib import Path

import nacl.signing

from netbbs.bots.registry import BotError, HOUSE_BOT_NAME

STDERR_LIMIT = 8192
LOG_LIMIT = 400
SCRATCH_CHANNEL = "lobby"


def _key():
    # A keypair account: no password hash to compute, and none to guess.
    return nacl.signing.SigningKey.generate().verify_key


@dataclass
class TrialSpec:
    """What to try: a saved instance or an editor draft, as `save_bot` takes it."""

    name: str = HOUSE_BOT_NAME
    command: list[str] = field(default_factory=list)
    temperament: str = "friendly"
    phrases: dict[str, str] = field(default_factory=dict)
    channels: list[str] = field(default_factory=list)
    defaults: list[str] = field(default_factory=list)
    lines_per_hour: int = 60
    queries: list[str] = field(default_factory=list)
    reading_channels: list[str] = field(default_factory=list)
    event_channels: list[str] = field(default_factory=list)
    greetings: bool = False
    board_sources: list[str] = field(default_factory=list)
    file_sources: list[str] = field(default_factory=list)
    announcement_channel: str | None = None
    announce_posts: bool = False
    announce_files: bool = False
    model_conversation: bool = False
    quotes: bool = False
    polls: bool = False
    door_sources: list[str] = field(default_factory=list)
    announce_doors: bool = False

    @classmethod
    def of_bot(cls, bot) -> "TrialSpec":
        return cls(
            name=bot.name, command=list(bot.command), temperament=bot.temperament,
            phrases=dict(bot.phrase_overrides), channels=list(bot.channels), defaults=list(bot.defaults),
            lines_per_hour=bot.lines_per_hour, queries=list(bot.queries),
            reading_channels=list(bot.reading_channels), event_channels=list(bot.event_channels),
            greetings=bot.greetings, board_sources=list(bot.board_sources), file_sources=list(bot.file_sources),
            announcement_channel=bot.announcement_channel, announce_posts=bot.announce_posts,
            announce_files=bot.announce_files, model_conversation=bot.model_conversation,
            quotes=bot.quotes, polls=bot.polls,
            door_sources=list(bot.door_sources), announce_doors=bot.announce_doors,
        )

    def grants(self) -> list[str]:
        """The grants a caller-facing summary names, in the editor's order."""
        names = ["addressed"]
        if self.reading_channels:
            names.append("read whole channel")
        if self.event_channels:
            names.append("presence events")
        if self.board_sources or self.file_sources or self.door_sources:
            names.append("source events")
        names.extend(self.queries)
        if self.model_conversation:
            names.append("model (rehearsed)")
        return names


@dataclass
class LogEntry:
    kind: str  # "host", "bot", "chat" or "status"
    value: object
    at: float = field(default_factory=time.time)


class TrialLog:
    """The runtime's trial hook. Live, `BotRuntime.trial` is None."""

    def __init__(self, *, model_enabled: bool):
        self.model_enabled = model_enabled
        self.entries: deque[LogEntry] = deque(maxlen=LOG_LIMIT)
        self.count = 0
        self.ticks = 0
        self._stderr = bytearray()
        self.stderr_total = 0
        self.changed = asyncio.Event()

    def add(self, kind, value):
        self.entries.append(LogEntry(kind, value))
        self.count += 1
        self.changed.set()

    def trace(self, direction, value):
        # One tick a second would bury everything else; they are counted.
        if direction == "host" and value.get("type") == "tick":
            self.ticks += 1
            return
        self.add(direction, value)

    def stderr(self, chunk: bytes) -> None:
        self._stderr += chunk
        del self._stderr[:-STDERR_LIMIT]
        self.stderr_total += len(chunk)
        self.changed.set()

    def stderr_since(self, shown: int) -> str:
        """Stderr written after `shown` bytes, as far as the excerpt keeps it."""
        fresh = min(len(self._stderr), self.stderr_total - shown)
        return bytes(self._stderr[len(self._stderr) - fresh:]).decode("utf-8", "replace") if fresh > 0 else ""

    def since(self, count: int) -> list[LogEntry]:
        fresh = min(len(self.entries), self.count - count)
        return list(self.entries)[len(self.entries) - fresh:] if fresh > 0 else []


class Trial:
    """One trial: start(), then line()/join()/leave()/post()/file(), then close()."""

    def __init__(self, spec: TrialSpec, *, caller: str, model_enabled: bool = False):
        self.spec = spec
        self.caller_name = caller
        self.log = TrialLog(model_enabled=model_enabled and spec.model_conversation)
        self.directory = None
        self.db_path = self.lane = self.hub = self.presence = self.runtime = None
        self.bot = self.channel = self.user = None
        self.participant = None
        self._listen = None
        self._status = None

    @property
    def label(self) -> str:
        return f"{self.spec.name}.bot"

    async def start(self) -> None:
        from netbbs.bots.runtime import BotRuntime
        from netbbs.chat.hub import ChatHub, ParticipantId
        from netbbs.chat.presence import PresenceRegistry
        from netbbs.storage.execution import DatabaseLane

        # Every exit path, a cancellation included, takes down whatever was
        # already made: the listener task, the lane's thread, the directory.
        try:
            self.directory = tempfile.TemporaryDirectory(prefix="netbbs-bot-trial-", ignore_cleanup_errors=True)
            await asyncio.to_thread(self._build, Path(self.directory.name))
            self.lane = DatabaseLane(self.db_path)
            self.hub = ChatHub()
            self.presence = PresenceRegistry()
            self.participant = ParticipantId(self.user.username, 1)
            queue = self.hub.join(self.channel.name, self.participant)
            self._listen = asyncio.create_task(self._chat(queue), name="bot-trial-chat")
            self.runtime = BotRuntime(self.lane, self.hub, presence=self.presence, poll_seconds=.2,
                                      restart_seconds=1, trial=self.log)
            self.runtime.start()
        except BaseException:
            await self.close()
            raise

    def _build(self, root: Path) -> None:
        from netbbs.storage.database import Database

        # Built, and closed again, on this worker thread; the trial then uses
        # the scratch database only through its own lane.
        self.db_path = root / "trial.db"
        db = Database(self.db_path)
        try:
            self._populate(db)
        finally:
            db.close()

    def _populate(self, db) -> None:
        from netbbs.auth.users import create_user
        from netbbs.boards.boards import create_board
        from netbbs.bots.registry import save_bot
        from netbbs.chat.channels import create_channel
        from netbbs.files.areas import create_file_area

        spec = self.spec
        self.user = create_user(db, self.caller_name, verify_key=_key(), user_level=10)
        actor = create_user(db, "Trial" + secrets.token_hex(4), verify_key=_key(), user_level=255)
        channels = spec.channels or [SCRATCH_CHANNEL]
        for name in channels:
            create_channel(db, name, creator=actor)
        for name in spec.board_sources:
            create_board(db, name, creator=actor)
        for name in spec.file_sources:
            create_file_area(db, name, creator=actor)
        if spec.door_sources:
            import sys
            from netbbs.doors import create_door
            for name in spec.door_sources:
                create_door(db, name, sys.executable, creator=actor)
        # Linked approvals do not apply: no scratch channel is Linked.
        self.bot = save_bot(
            db, actor=actor, name=spec.name, enabled=True, command=list(spec.command),
            temperament=spec.temperament, channels=channels,
            defaults=spec.defaults if spec.channels else [SCRATCH_CHANNEL],
            lines_per_hour=spec.lines_per_hour, queries=list(spec.queries),
            reading_channels=spec.reading_channels, event_channels=spec.event_channels,
            greetings=spec.greetings, board_sources=spec.board_sources, file_sources=spec.file_sources,
            announcement_channel=spec.announcement_channel, announce_posts=spec.announce_posts,
            announce_files=spec.announce_files, model_conversation=spec.model_conversation,
            phrases=spec.phrases, quotes=spec.quotes, polls=spec.polls,
            door_sources=spec.door_sources, announce_doors=spec.announce_doors,
        )
        # Callers type where the bot answers "!" lines, if it does anywhere.
        from netbbs.chat.channels import get_channel_by_name
        typed = (spec.defaults or channels)[0] if spec.channels else SCRATCH_CHANNEL
        self.channel = get_channel_by_name(db, typed)

    async def _chat(self, queue) -> None:
        while True:
            self.log.add("chat", await queue.get())

    async def status(self) -> tuple[str, str]:
        from netbbs.bots.registry import runtime_status
        return (await self.lane.run(runtime_status)).get(self.bot.id, ("starting", ""))

    async def settle(self, *, quiet: float = .4, limit: float = 3.0) -> None:
        """Wait until the bot has been quiet for `quiet` seconds, at most `limit`."""
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            self.log.changed.clear()
            try:
                await asyncio.wait_for(self.log.changed.wait(), min(quiet, max(0.01, deadline - time.monotonic())))
            except TimeoutError:
                return

    async def wait_ready(self, limit: float | None = None) -> None:
        """Until the bot runs or has failed to start. By default as long as
        the runtime gives it to answer its hello, and a little more: the bot
        is "restarting" by then if it never answered."""
        from netbbs.bots.runtime import READY_SECONDS
        deadline = time.monotonic() + (READY_SECONDS + 5 if limit is None else limit)
        while time.monotonic() < deadline:
            if self.bot.id in self.runtime.running_ids or (await self.status())[0] == "restarting":
                return
            await asyncio.sleep(.05)

    async def note_status(self) -> None:
        current = await self.status()
        if current != self._status:
            self._status = current
            self.log.add("status", current)

    # -- what the SysOp types

    async def line(self, text: str) -> None:
        """One line typed as the caller, sent exactly as chat sends it."""
        from netbbs.chat.scrollback import record_message
        message = await self.lane.run(record_message, self.channel, kind="message",
                                      author_label=self.user.username, body=text,
                                      author_user_id=self.user.id)
        await self.hub.broadcast(self.channel.name, message, exclude={self.participant})
        await self.runtime.observe(self.channel, message, caller={"id": self.user.id, "name": self.user.username})
        problem = await self.runtime.addressed(self.channel, self.user, message, self.participant)
        if problem:
            from netbbs.chat.bots import BotNotice
            self.log.add("chat", BotNotice(problem, private=True))

    async def presence_event(self, kind: str, name: str) -> None:
        """Another caller joins or leaves the scratch channel."""
        from netbbs.chat.scrollback import record_message
        user = await self.lane.run(self._account, name)
        message = await self.lane.run(record_message, self.channel, kind=kind, author_label=user.username,
                                      author_user_id=user.id)
        await self.hub.broadcast(self.channel.name, message)
        await self.runtime.observe(self.channel, message, caller={"id": user.id, "name": user.username})

    def _account(self, db, name):
        from netbbs.auth.users import AuthError, create_user, get_user_by_username
        try:
            return get_user_by_username(db, name)
        except AuthError:
            pass
        try:
            return create_user(db, name, verify_key=_key(), user_level=10)
        except (AuthError, ValueError) as exc:
            raise BotError(f"{name!r} cannot be a caller's name: {exc}") from exc

    async def post(self, title: str) -> None:
        if not self.spec.board_sources:
            raise BotError("This bot has no board-event sources, so a new post reaches it nowhere.")
        from netbbs.boards.boards import get_board_by_name
        from netbbs.boards.posts import create_post

        def write(db):
            board = get_board_by_name(db, self.spec.board_sources[0])
            create_post(db, board, self.user, title, "A post written by Try.")
        await self.lane.run(write)

    async def file(self, name: str) -> None:
        if not self.spec.file_sources:
            raise BotError("This bot has no file-event sources, so a new file reaches it nowhere.")
        from netbbs.files.areas import get_file_area_by_name
        from netbbs.files.entries import upload_file

        def write(db):
            area = get_file_area_by_name(db, self.spec.file_sources[0])
            upload_file(db, area, self.user, name, b"A file uploaded by Try.\n")
        await self.lane.run(write)

    async def close(self) -> None:
        if self.runtime is not None:
            await self.runtime.close()
        if self._listen is not None:
            self._listen.cancel()
            await asyncio.gather(self._listen, return_exceptions=True)
        if self.lane is not None:
            self.lane.close()
        if self.directory is not None:
            self.directory.cleanup()
        self.runtime = self._listen = self.lane = self.directory = None
