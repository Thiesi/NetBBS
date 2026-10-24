"""Node-owned bot processes. No bot I/O runs inline with a chat send."""

from __future__ import annotations

import asyncio
from collections import deque
import json
import logging
import os
from pathlib import Path
import signal
import sys
import time
import uuid

from netbbs.bots import BOT_API_VERSION
from netbbs.bots.registry import (
    Bot, BotError, allowed_channel, bot_for_label, check_block, deliver_private, list_bots, local_caller, publish,
    select_request,
    set_runtime_status, reset_runtime_status,
)
from netbbs.bots.state import read_state, write_state, state_path
from netbbs.chat.bots import BotNotice, BotPrivateAnswer
from netbbs.rendering import sanitize_text
from netbbs.timeutil import utc_now_iso

MAX_LINE_BYTES = 32768
MAX_PENDING = 32
REQUEST_SECONDS = 60
# The bot's answer to the hello. The time counts the interpreter's own start,
# which a loaded host can stretch to many seconds (issue #1454): a bound of 5
# treated a slow start as a broken bot and backed it off for up to a minute.
# A bot that really hangs is still restarted once this runs out.
READY_SECONDS = 30
# Joins waiting for held-line delivery (issue #1375). A join is queued with
# put_nowait from the chat paths, so a slow database never holds a caller's
# input; a full queue drops the join and its lines wait for the next one.
MAX_JOINS = 256
PRUNE_SECONDS = 60
_logger = logging.getLogger(__name__)
HELD_ACTIONS = frozenset({"say.for", "held.list", "held.cancel", "tell", "tell.list", "tell.cancel"})


def _display_timezone(db) -> str:
    from netbbs.timeutil import get_node_timezone
    return get_node_timezone(db).key


class _TokenKept(BotError):
    """A refusal the bot can act on with another answer (issue #1372): the
    request token stays live and the caller is not told, so a bot whose
    private answer cannot reach this caller may still reply publicly."""


class BotRuntime:
    def __init__(self, lane, hub, *, presence=None, poll_seconds: float = 1.0, restart_seconds: float = 1.0,
                 trial=None):
        self.lane, self.hub = lane, hub
        # Issue #1374: only the console's Try passes this (netbbs.bots.trial):
        # it traces the protocol, keeps a bounded stderr excerpt and rehearses
        # model actions. Live it is None, and none of that happens.
        self.trial = trial
        self.presence = presence
        self.started_at = time.monotonic()
        self.poll_seconds, self.restart_seconds = poll_seconds, restart_seconds
        self.workers: dict[int, _Worker] = {}
        self.task: asyncio.Task | None = None
        self.link = None
        self.disclosures = {}
        self.joins: asyncio.Queue = asyncio.Queue(maxsize=MAX_JOINS)
        self.delivery_task: asyncio.Task | None = None
        from netbbs.bots.model import ModelService
        self.model = ModelService(lane)

    @property
    def running_ids(self) -> frozenset[int]:
        return frozenset(i for i, w in list(self.workers.items()) if w.ready)

    def start(self) -> None:
        if self.task is None:
            self.started_at = time.monotonic()
            self.hub.bot_runtime = self
            self.task = asyncio.create_task(self._run(), name="bot-manager")
            self.delivery_task = asyncio.create_task(self._deliveries(), name="bot-held-delivery")
            if self.link is not None:
                self.link.start()

    async def _run(self) -> None:
        from netbbs.bots.model import settings as model_settings
        try:
            await self.lane.run(reset_runtime_status)
            while True:
                try:
                    await self.model.upkeep()
                    desired = {b.id: b for b in await self.lane.run(list_bots) if b.enabled}
                    model_enabled = (await self.lane.run(model_settings)).enabled
                    # Issue #1375: Baud reads absolute reminder times in the
                    # node display clock, which reaches it only in the hello.
                    timezone = await self.lane.run(_display_timezone)
                    if self.trial is not None:
                        # The scratch node has no model settings of its own;
                        # the trial carries the live node's answer.
                        model_enabled = self.trial.model_enabled
                    disclosures = {}
                    for bot in desired.values():
                        for name in bot.reading_channels:
                            disclosures.setdefault(name, []).append(bot.label)
                    for name in self.disclosures.keys() | disclosures.keys():
                        labels = disclosures.get(name, [])
                        if labels != self.disclosures.get(name, []):
                            notice = ", ".join(labels) + " reads this channel." if labels else "No local bot now has whole-channel reading access."
                            await self.hub.broadcast(name, BotNotice(notice))
                    self.disclosures = disclosures
                    for bot_id, worker in list(self.workers.items()):
                        if (desired.get(bot_id) != worker.bot or worker.task.done() or
                                worker.model_enabled != (worker.bot.model_conversation and model_enabled) or
                                worker.timezone != timezone):
                            await worker.stop()
                            del self.workers[bot_id]
                    for bot_id, bot in desired.items():
                        if bot_id not in self.workers:
                            worker = _Worker(self, bot, model_enabled=bot.model_conversation and model_enabled,
                                             timezone=timezone)
                            self.workers[bot_id] = worker
                            worker.task = asyncio.create_task(worker.run(), name=f"bot-{bot.name}")
                except Exception:
                    _logger.exception("Could not reconcile chat bots; retrying")
                await asyncio.sleep(self.poll_seconds)
        finally:
            await asyncio.gather(*(w.stop() for w in list(self.workers.values())), return_exceptions=True)
            self.workers.clear()

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.delivery_task is not None:
            self.delivery_task.cancel()
            await asyncio.gather(self.delivery_task, return_exceptions=True)
            self.delivery_task = None
        if self.link is not None:
            await self.link.close()
        if self.hub.bot_runtime is self:
            self.hub.bot_runtime = None

    async def addressed(self, channel, user, message, participant_id) -> str | None:
        """Called only by the accepted local human send path, never broadcast/replay.

        A refusal is returned for the sender to see. Successful dispatch is a
        bounded put_nowait, so a broken bot cannot hold the chat input lock.
        """
        if message.kind != "message" or message.external_source or message.author_kind != "user":
            return None
        try:
            selected = await self.lane.run(select_request, channel, message.body)
            if selected is None:
                return None
            bot, text = selected
            caller = await self.lane.run(local_caller, channel, user)
            await self.enqueue(bot, channel, caller, text, participant_id)
        except BotError as exc:
            return str(exc)
        return None

    async def ask_privately(self, channel, user, label, text, participant_id) -> str | None:
        """A local caller's private request (issue #1372): `/msg Baud.bot`
        and `/private Baud.bot`. The chat screen calls this instead of the
        public send path, so the request is never recorded, broadcast,
        queued for Link, relayed to MRC or observed. A refusal is returned
        for the caller to see."""
        try:
            bot = await self.lane.run(bot_for_label, label)
            if bot is None:
                raise BotError("No local bot has that name. Use /names to see running bots here.")
            caller = await self.lane.run(local_caller, channel, user)
            await self.enqueue(bot, channel, caller, text, participant_id, asked_privately=True)
        except BotError as exc:
            return str(exc)
        return None

    async def enqueue(self, bot, channel, caller, text, participant_id=None, *, completed=None,
                      asked_privately=False, private_reachable=True):
        """Dispatch one addressed request. A privately asked one is checked
        against the caller's private-answer limit before dispatch, a public
        one against the channel's, since its answer may be public (issue
        #1372 Decision 4). `private_reachable` says whether a private answer
        can reach this caller: false for a Link caller whose home node lacks
        `chat_bot_private_v1`."""
        await self.lane.run(allowed_channel, bot.id, channel.name, check_rate=True,
                            private_caller=str(caller["id"]) if asked_privately else None)
        worker = self.workers.get(bot.id)
        if worker is None or not worker.ready:
            raise BotError(f"{bot.label} is not ready. The SysOp can check its status in Chat bots.")
        if len(worker.pending) >= MAX_PENDING or worker.events.full():
            raise BotError(f"{bot.label} is busy; try again shortly.")
        # Link request text arrives unsanitized; a control kept here would be
        # stored by the bot and refused by publish on every later attempt.
        # Ordinary spacing is the caller's and passes through unchanged.
        text = sanitize_text(text).replace("\t", " ")
        request = uuid.uuid4().hex
        worker.pending[request] = (channel.name, participant_id, time.monotonic() + REQUEST_SECONDS, completed, dict(caller), text)
        worker.privacy[request] = (bool(asked_privately), bool(private_reachable or asked_privately))
        worker.events.put_nowait({"type": "message", "request": request, "channel": channel.name,
                                 "caller": caller, "text": text,
                                 "private": bool(private_reachable or asked_privately),
                                 "asked_privately": bool(asked_privately)})

    def joined(self, channel, caller) -> None:
        """A caller joined `channel` live: deliver the lines held for them
        (issues #1375, #1376). Called from the local and Linked join paths;
        never blocks them."""
        try:
            self.joins.put_nowait((channel, caller))
        except asyncio.QueueFull:
            _logger.warning("Held bot lines: join queue full; lines wait for the caller's next join.")

    async def present(self, channel, caller) -> bool:
        """Whether `caller` (a bot caller id) is in `channel` now, on this node
        or, for a Link caller, in the Linked channel's live roster."""
        if isinstance(caller, int) and not isinstance(caller, bool):
            from netbbs.auth.users import get_user_by_id
            user = await self.lane.run(get_user_by_id, caller)
            return user is not None and bool(self.hub.participants_for_username(channel.name, user.username))
        if isinstance(caller, str) and caller.startswith("link:") and self.link is not None:
            _prefix, node, user_id = caller.split(":", 2)
            return any(n == node and u == user_id
                       for n, u, _label in self.link.bridge.remote_channel_roster(channel.channel_id))
        return False

    async def send_private(self, channel, caller: int, label: str, body: str) -> None:
        """A `(to you)` line to every session of local `caller` in `channel`."""
        from netbbs.auth.users import get_user_by_id
        user = await self.lane.run(get_user_by_id, caller)
        answer = BotPrivateAnswer(label, body, utc_now_iso())
        for pid in self.hub.participants_for_username(channel.name, user.username) if user else ():
            await self.hub.send_to(channel.name, pid, answer)

    async def publish_live(self, channel, message) -> None:
        await self.hub.broadcast(channel.name, message)
        if self.link is not None:
            await self.link.bridge.broadcast_local_message_live(channel, message)

    async def _deliveries(self) -> None:
        from netbbs.bots.held import join_candidates, prune_expired
        pruned = 0.0
        while True:
            try:
                if time.monotonic() - pruned >= PRUNE_SECONDS:
                    pruned = time.monotonic()
                    for bot_id, held_id, kind in await self.lane.run(prune_expired):
                        if kind == "line":
                            self._tell_bot(bot_id, {"type": "held", "held_id": held_id, "outcome": "expired"})
                try:
                    channel, caller = await asyncio.wait_for(self.joins.get(), PRUNE_SECONDS)
                except asyncio.TimeoutError:
                    continue
                try:
                    for held_id, bot_id, kind, private in await self.lane.run(join_candidates, channel, caller):
                        await self._deliver(channel, held_id, bot_id, kind, caller if private else None)
                finally:
                    self.joins.task_done()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("Could not deliver held bot lines; retrying")
                await asyncio.sleep(self.poll_seconds)

    async def _deliver(self, channel, held_id, bot_id, kind, private_caller=None) -> None:
        link = self.link
        try:
            if private_caller is not None:
                from netbbs.bots import held
                published = await self.lane.run(held.deliver_private, held_id, bot_id, channel.name,
                                                private_caller)
            else:
                published = await self.lane.run(publish, bot_id, channel.name, None, held_id=held_id,
                                                node_identity=link.node.identity if link else None)
        except BotError:
            return  # Muted, over its hourly limit, or taken: held until expiry.
        if published is None:
            outcome = {"type": "held", "held_id": held_id, "outcome": "expired"}
        elif private_caller is not None:
            await self.send_private(channel, private_caller, *published)
            outcome = {"type": "held", "held_id": held_id, "outcome": "delivered", "private": True}
        else:
            target, message = published
            await self.publish_live(target, message)
            outcome = {"type": "held", "held_id": held_id, "outcome": "delivered", "message_id": message.id}
        if kind == "line":
            self._tell_bot(bot_id, outcome)

    def _tell_bot(self, bot_id, event) -> None:
        worker = self.workers.get(bot_id)
        if worker is not None and worker.ready and not worker.events.full():
            worker.events.put_nowait(event)

    async def observe(self, channel, message, *, caller=None):
        """Only accepted live human paths call this; no tokens or replay dispatch."""
        from netbbs.bots.registry import observation_targets
        if (message.kind not in {"message", "action", "join", "leave"} or
                message.external_source or message.author_kind != "user"):
            return
        if caller is None:
            caller = {"name": message.author_label}
        # Remote legacy automatic authors have no local author_kind marker.
        if (isinstance(caller.get("id"), str) and
                caller["name"].split("@", 1)[0].lower().endswith((".bot", ".door"))):
            return
        caller = {**caller, "name": sanitize_text(caller["name"])}
        targets = await self.lane.run(observation_targets, channel.name, message.kind)
        for bot_id in targets:
            worker = self.workers.get(bot_id)
            if worker is None or not worker.ready:
                continue
            if worker.events.full():
                await worker.status("running", "Bot event queue full; a live observation was dropped.")
                continue
            worker.events.put_nowait({
                "type": "presence" if message.kind in {"join", "leave"} else "channel",
                "channel": channel.name, "kind": message.kind, "caller": caller,
                "text": sanitize_text(message.body or ""),
            })


class _Worker:
    def __init__(self, owner: BotRuntime, bot: Bot, *, model_enabled: bool = False, timezone: str = "UTC"):
        self.owner, self.bot = owner, bot
        self.model_enabled = model_enabled
        self.timezone = timezone
        self.events: asyncio.Queue = asyncio.Queue(maxsize=64)
        self.pending: dict[str, tuple] = {}
        # request -> (asked privately, a private answer can reach the caller)
        self.privacy: dict[str, tuple[bool, bool]] = {}
        self.announcements = {}
        self.source_cursor = 0
        self.door_cursor = 0
        self.ready = False
        self.proc = None
        self.task = None
        self.actions = deque()
        self.model_tasks = {}

    async def status(self, state: str, error: str = "") -> None:
        await self.owner.lane.run(set_runtime_status, self.bot.id, state, error)

    def request_privacy(self, request: str) -> tuple[bool, bool]:
        """(asked privately, private answer reachable) for a live token."""
        return self.privacy.get(request, (False, True))

    async def notify(self, request: str, text: str, message=None, private: BotPrivateAnswer | None = None) -> None:
        target = self.pending.pop(request, None)
        self.privacy.pop(request, None)
        if target:
            if target[3] is not None:
                if private is not None:
                    await target[3](text, message, private)
                else:
                    await target[3](text, message)
            elif private is not None:
                # Only the requesting session; dropped if it has left, as
                # an online-only /msg is (issue #1372).
                await self.owner.hub.send_to(target[0], target[1], private)
            elif text:
                await self.owner.hub.send_to(target[0], target[1],
                                             BotNotice(f"{self.bot.label}: {sanitize_text(text)}", private=True))

    async def run(self) -> None:
        delay = self.owner.restart_seconds
        try:
            while True:
                started = time.monotonic()
                try:
                    await self.status("starting")
                    await self._session()
                    raise BotError("Bot process exited.")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if asyncio.current_task().cancelling():
                        # A stop is under way and the session's cleanup failed
                        # on its way out. That failure replaced the stop's
                        # CancelledError; restarting here would swallow the
                        # stop, and stop() (Try's /quit, Kill, node shutdown)
                        # would wait forever on a bot that keeps running.
                        # The callers gather and drop the result, so the
                        # failure is logged here.
                        _logger.warning("%s: cleanup failed while stopping (%s); stopped anyway.",
                                        self.bot.label, type(exc).__name__)
                        raise asyncio.CancelledError() from exc
                    # No raw process output or prompts in logs/status.
                    reason = str(exc) if isinstance(exc, BotError) else f"Bot process failed ({type(exc).__name__})."
                    await self.status("restarting", reason)
                    _logger.warning("%s: %s", self.bot.label, reason)
                    for request in list(self.pending):
                        await self.notify(request, "The bot stopped before replying; try again when it restarts.")
                if time.monotonic() - started > 60:
                    delay = self.owner.restart_seconds
                await asyncio.sleep(delay)
                delay = min(60, delay * 2)
        finally:
            self.ready = False
            for request in list(self.pending):
                await self.notify(request, "The bot was stopped before replying.")
            await self.status("stopped")

    async def stop(self) -> None:
        self.ready = False
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def _session(self) -> None:
        lane = self.owner.lane
        from netbbs.bots.sources import door_head, source_head
        # Startup skips history: only what arrives while the bot runs.
        self.source_cursor = await lane.run(source_head)
        self.door_cursor = await lane.run(door_head)
        self.announcements.clear()
        state = await lane.run(read_state, self.bot.storage_name)
        from netbbs.config import get_node_display_name
        node = await lane.run(get_node_display_name)
        path = await lane.run(state_path, self.bot.storage_name)
        command = self.bot.command or (sys.executable, "-u", str(Path(__file__).with_name("baud.py")))
        env = {k: v for k, v in os.environ.items()
               if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE", "TEMP", "TMP", "LANG", "LC_ALL"}}
        env.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        self.events = asyncio.Queue(maxsize=64)
        self.actions.clear()
        tasks = []
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=path.parent, env=env,
            limit=MAX_LINE_BYTES, start_new_session=os.name != "nt",
        ))
        try:
            self.proc = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            # Stopping while the OS creates the child must still adopt/reap it.
            self.proc = await spawn
            await self._reap()
            raise
        try:
            tasks.append(asyncio.create_task(self._drain_stderr()))
            await self._write({"type": "hello", "version": BOT_API_VERSION, "name": self.bot.label,
                               "scope": "addressed", "channels": self.bot.channels,
                               "temperament": self.bot.temperament, "phrases": self.bot.phrase_overrides, "state": state, "queries": self.bot.queries,
                               "reading_channels": self.bot.reading_channels,
                               "event_channels": self.bot.event_channels, "greetings": self.bot.greetings,
                               "board_sources": self.bot.board_sources, "file_sources": self.bot.file_sources,
                               "announcement_channel": self.bot.announcement_channel,
                               "announce_posts": self.bot.announce_posts, "announce_files": self.bot.announce_files,
                               "door_sources": self.bot.door_sources, "announce_doors": self.bot.announce_doors,
                               "model_conversation": self.model_enabled, "timezone": self.timezone,
                               # Issue #1373: this host takes a reply's titled block.
                               "blocks": True,
                               # Issue #1379: Baud's switches; the Linked channels
                               # among `channels`, and the name other BBSes address
                               # this node by (`Baud.bot@<node>`).
                               "quotes": self.bot.quotes, "polls": self.bot.polls,
                               "linked_channels": list(self.bot.linked_channels), "node": node})
            ready = await asyncio.wait_for(self._read(), READY_SECONDS)
            if ready != {"type": "ready", "version": BOT_API_VERSION}:
                raise BotError("Bot did not acknowledge BOT_API_VERSION 1.")
            self.ready = True
            await self.status("running")
            tasks.extend([asyncio.create_task(self._reader()), asyncio.create_task(self._writer()),
                          asyncio.create_task(self._ticks())])
            if self.bot.board_sources or self.bot.file_sources or self.bot.door_sources:
                tasks.append(asyncio.create_task(self._source_events()))
            # A program may close stderr while keeping its protocol pipes open.
            # EOF there is not the end of the bot session.
            done, _ = await asyncio.wait(tasks[1:], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            self.ready = False
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for task in list(self.model_tasks.values()):
                task.cancel()
            await asyncio.gather(*list(self.model_tasks.values()), return_exceptions=True)
            self.model_tasks.clear()
            self.owner.model.clear_bot(self.bot.id)
            await self._reap()

    async def _reap(self) -> None:
        # Bounded, and it raises nothing of its own: it runs in _session's
        # finally, where an exception would replace a stop's CancelledError.
        # On a loaded machine the child's exit can reach the event loop
        # seconds after the kill, so a wait that runs out is logged and left
        # there; asyncio still collects the process when its exit arrives.
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.returncode is None:
                self._signal(proc, kill=False)
                if not await self._exited(proc):
                    self._signal(proc, kill=True)
                    if not await self._exited(proc):
                        _logger.warning("%s: the bot process had not reported its exit 2 seconds after a kill; "
                                        "no longer waiting for it.", self.bot.label)
        finally:
            if proc.stdin:
                proc.stdin.close()
            self.proc = None

    @staticmethod
    def _signal(proc, *, kill: bool) -> None:
        try:
            if os.name == "nt":
                proc.kill() if kill else proc.terminate()
            else:
                os.killpg(proc.pid, signal.SIGKILL if kill else signal.SIGTERM)
        except ProcessLookupError:
            pass  # Already gone; the wait that follows sees its exit.

    @staticmethod
    async def _exited(proc, seconds: float = 2) -> bool:
        try:
            await asyncio.wait_for(proc.wait(), seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def _read(self) -> dict:
        # BotError is a ValueError: keep the specific reasons out of the
        # JSON handler, or every crash would read as "invalid JSON".
        try:
            line = await self.proc.stdout.readline()
        except ValueError as exc:
            raise BotError("Bot exceeded the 32 KiB protocol line limit.") from exc
        if not line:
            raise BotError("Bot closed its output pipe.")
        if len(line) > MAX_LINE_BYTES:
            raise BotError("Bot exceeded the 32 KiB protocol line limit.")
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise BotError("Bot sent invalid JSON.") from exc
        if not isinstance(value, dict):
            raise BotError("Bot protocol requires JSON objects.")
        if self.owner.trial is not None:
            self.owner.trial.trace("bot", value)
        return value

    async def _write(self, value: dict) -> None:
        raw = (json.dumps(value, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
        if len(raw) > MAX_LINE_BYTES:
            raise BotError("Host event exceeds the protocol line limit.")
        if self.owner.trial is not None:
            self.owner.trial.trace("host", value)
        self.proc.stdin.write(raw)
        await asyncio.wait_for(self.proc.stdin.drain(), 5)

    async def _drain_stderr(self) -> None:
        while chunk := await self.proc.stderr.read(4096):
            # Live, stderr is discarded: unbounded logs and prompt text.
            if self.owner.trial is not None:
                self.owner.trial.stderr(chunk)

    async def _writer(self) -> None:
        while True:
            event = await self.events.get()
            if event["type"] == "message":
                try:
                    await self.owner.lane.run(allowed_channel, self.bot.id, event["channel"])
                except BotError as exc:
                    await self.notify(event["request"], str(exc))
                    continue
            elif event["type"] in {"post", "file", "door"}:
                from netbbs.bots.sources import source_event
                reference = self.announcements.get(event["event"])
                if reference is None:
                    continue
                try:
                    current = await self.owner.lane.run(source_event, self.bot.id, reference[0])
                    event = {**current, "event": event["event"]}
                except BotError as exc:
                    self.announcements.pop(event["event"], None)
                    await self.status("running", str(exc))
                    continue
            await self._write(event)

    async def _source_events(self):
        from netbbs.bots.sources import door_batch, source_batch, source_event
        while True:
            rows = []
            if self.bot.board_sources or self.bot.file_sources:
                rows, self.source_cursor, advanced = await self.owner.lane.run(source_batch, self.bot.id, self.source_cursor)
                if advanced:
                    await self.status("running", "Source-event window advanced; earlier notifications may have been skipped.")
            if self.bot.door_sources:
                doors, self.door_cursor, advanced = await self.owner.lane.run(door_batch, self.bot.id, self.door_cursor)
                if advanced:
                    await self.status("running", "Door-event window advanced; earlier notifications may have been skipped.")
                rows = rows + doors
            for reference in rows:
                if len(self.announcements) >= MAX_PENDING or self.events.full():
                    await self.status("running", "Bot source-event queue full; a notification was dropped.")
                    break
                try:
                    event = await self.owner.lane.run(source_event, self.bot.id, reference)
                except BotError as exc:
                    await self.status("running", str(exc))
                    continue
                token = uuid.uuid4().hex
                reference["destination"] = event["channel"]
                self.announcements[token] = (reference, time.monotonic() + REQUEST_SECONDS)
                self.events.put_nowait({**event, "event": token})
            await asyncio.sleep(self.owner.poll_seconds)

    async def _ticks(self) -> None:
        while True:
            await asyncio.sleep(1)
            for request, pending in list(self.pending.items()):
                deadline = pending[2]
                if time.monotonic() >= deadline:
                    await self.notify(request, "The bot did not reply within 60 seconds.")
            for token, (_, deadline) in list(self.announcements.items()):
                if time.monotonic() >= deadline:
                    del self.announcements[token]
            if not self.events.full():
                self.events.put_nowait({"type": "tick", "time": time.time()})

    async def _reader(self) -> None:
        while True:
            value = await self._read()
            now = time.monotonic()
            while self.actions and self.actions[0] < now - 60:
                self.actions.popleft()
            if len(self.actions) >= 240:
                raise BotError("Bot exceeded 240 actions per minute.")
            self.actions.append(now)
            action_id = value.get("id")
            if not isinstance(action_id, str) or len(action_id) > 64:
                raise BotError("Bot actions need a string id of up to 64 characters.")
            result = {"type": "result", "id": action_id, "ok": True}
            try:
                result["value"] = await self._action(value)
            except (BotError, OSError) as exc:
                result.update(ok=False, error=str(exc)[:300])
                if value.get("action") != "tell":
                    # A refused tell can name who blocks whom: the caller
                    # alone is told, not the SysOp's status line.
                    await self.status("running", str(exc))
                request = value.get("request")
                if (isinstance(request, str) and value.get("action") != "query"
                        and not isinstance(exc, _TokenKept)):
                    await self.notify(request, str(exc))
            if self.events.full():
                raise BotError("Bot stopped consuming host events.")
            self.events.put_nowait(result)

    async def _action(self, value: dict):
        action = value.get("action")
        lane = self.owner.lane
        if action in {"model", "model.reset"}:
            request = value.get("request")
            pending = self.pending.get(request) if isinstance(request, str) else None
            if pending is None or pending[2] <= time.monotonic():
                raise BotError("Model actions need this bot's live addressed request token.")
            if request in self.model_tasks:
                raise BotError("This request is already being handled by the model.")
            private = self._answer_privacy(request, value)
            if self.owner.trial is not None:
                # A trial never calls the provider: it says what would have
                # happened, as door outbound rehearsals do (issue #1374).
                if not self.model_enabled:
                    raise BotError("Model conversation is not granted to this bot, or the node's Model settings are off.")
                text = ("Rehearsal: would clear this caller's model context." if action == "model.reset" else
                        "Rehearsal: would ask the model with this caller's text: " + pending[5][:200])
                await self.notify(request, text)
                return {"pending": False, "rehearsal": text}
            task = asyncio.create_task(self._model_reply(request, pending, reset=action == "model.reset",
                                                         private=private), name="bot-model-reply")
            self.model_tasks[request] = task
            def finished(done):
                self.model_tasks.pop(request, None)
                if not done.cancelled() and done.exception() is not None:
                    _logger.warning("%s: model reply delivery failed", self.bot.label)
            task.add_done_callback(finished)
            return {"pending": True}
        if action == "state.get":
            return await lane.run(read_state, self.bot.storage_name)
        if action == "state.set":
            await lane.run(write_state, self.bot.storage_name, value.get("value"))
            return None
        if action == "query":
            from netbbs.bots.queries import run_query
            request = value.get("request")
            pending = self.pending.get(request) if isinstance(request, str) else None
            if pending is None or pending[2] <= time.monotonic():
                raise BotError("A node query needs this bot's live request token.")
            if self.owner.presence is None:
                raise BotError("Node presence is not available for bot queries.")
            return await lane.run(
                run_query, self.bot.id, pending[0], value.get("query"), value.get("target"),
                caller=pending[4], online=self.owner.presence.online_usernames(),
                uptime_seconds=int(time.monotonic() - self.owner.started_at),
            )
        if value.get("block") is not None and action != "reply":
            # Issue #1373: only an answer may be a titled block; reminders,
            # held lines, tells and announcements stay one line.
            raise BotError("Only reply takes a block; say and announce stay one line.")
        if action in HELD_ACTIONS:
            return await self._held_action(action, value)
        if action not in {"say", "reply", "announce"}:
            raise BotError("Unknown action or missing grant. Available: say, say.for, reply, announce, "
                           "state.get, state.set, query, held.list, held.cancel, tell, tell.list, tell.cancel.")
        body = value.get("text")
        request = value.get("request")
        block = value.get("block")
        if block is not None:
            # Issue #1373: an answer may be a titled block; reminders and
            # announcements stay one line.
            if action != "reply":
                raise BotError("Only reply takes a block; say and announce stay one line.")
            if body is not None:
                raise BotError("A reply carries text or a block, not both.")
            block = check_block(block)
        if action == "announce":
            token = value.get("event")
            reference = self.announcements.pop(token, None) if isinstance(token, str) else None
            if reference is None or reference[1] <= time.monotonic():
                raise BotError("Announce needs this bot's live source-event token.")
            body, channel_name = "Source announcement", reference[0]["destination"]
        elif action == "reply":
            if not isinstance(request, str) or request not in self.pending:
                raise BotError("Reply request has expired or does not belong to this bot.")
            channel_name = self.pending[request][0]
            if self._answer_privacy(request, value):
                return await self._private_reply(request, channel_name, body, block)
        else:
            channel_name = value.get("channel")
            if not isinstance(channel_name, str):
                raise BotError("Say needs an allowed channel name.")
        link = self.owner.link
        channel, message = await lane.run(publish, self.bot.id, channel_name, body,
                                          node_identity=link.node.identity if link else None,
                                          source_reference=reference[0] if action == "announce" else None,
                                          block=block)
        await self.owner.publish_live(channel, message)
        if action == "reply":
            await self.notify(request, "", message)
        return {"message_id": message.id}

    def _live_request(self, value: dict, action: str) -> tuple:
        request = value.get("request")
        pending = self.pending.get(request) if isinstance(request, str) else None
        if pending is None or pending[2] <= time.monotonic():
            raise BotError(f"{action} needs this bot's live request token.")
        return pending

    async def _held_action(self, action: str, value: dict):
        """Host-held lines (issues #1375, #1376). `say.for` names its caller;
        the tell actions take the caller from the pending request only."""
        from netbbs.bots import held
        from netbbs.bots.registry import local_channel
        lane = self.owner.lane
        if action == "held.list":
            return await lane.run(held.list_lines, self.bot.id)
        if action == "held.cancel":
            return await lane.run(held.cancel_line, self.bot.id, value.get("held_id"))
        if action == "say.for":
            channel_name, caller = value.get("channel"), value.get("caller")
            if not isinstance(channel_name, str):
                raise BotError("say.for needs an allowed channel name.")
            held.caller_columns(caller)
            private = value.get("private", False)
            if type(private) is not bool:
                raise BotError("An action's private flag must be true or false.")
            if private and not isinstance(caller, int):
                raise BotError("A private held line reaches local callers only.")
            channel = await lane.run(local_channel, channel_name,
                                     allow_linked=channel_name in self.bot.linked_channels)
            if private and await self.owner.present(channel, caller):
                text = value.get("text")
                bot, _channel = await lane.run(deliver_private, self.bot.id, channel_name, text, str(caller))
                await self.owner.send_private(channel, caller, bot.label, text)
                return {"delivered": True, "private": True}
            if await self.owner.present(channel, caller):
                link = self.owner.link
                target, message = await lane.run(publish, self.bot.id, channel_name, value.get("text"),
                                                 node_identity=link.node.identity if link else None)
                await self.owner.publish_live(target, message)
                return {"delivered": True, "message_id": message.id}
            text = value.get("held_text", value.get("text"))
            result = await lane.run(held.hold_line, self.bot.id, channel_name, caller, text, value.get("expires"),
                                    private)
            if await self.owner.present(channel, caller):
                self.owner.joined(channel, caller)  # Arrived meanwhile: deliver now.
            return result
        pending = self._live_request(value, action)
        channel_name, caller = pending[0], pending[4]
        if action == "tell.list":
            return await lane.run(held.list_tells, self.bot.id, channel_name, caller)
        if action == "tell.cancel":
            return await lane.run(held.cancel_tell, self.bot.id, channel_name, caller, value.get("tell_id"))
        present = frozenset(pid.username for pid in self.owner.hub.participant_ids(channel_name))
        return await lane.run(held.hold_tell, self.bot.id, channel_name, caller, value.get("to"),
                              value.get("text"), present)

    def _answer_privacy(self, request, value) -> bool:
        """Whether this answer is private: the action's `private` flag,
        which defaults to how the request was asked. A privately asked
        request takes no public answer, and a private one is refused, never
        made public, when it cannot reach the caller (issue #1372). Both
        refusals keep the token, so the bot can answer the other way."""
        flag = value.get("private")
        if flag is not None and type(flag) is not bool:
            raise BotError("An action's private flag must be true or false.")
        asked, reachable = self.request_privacy(request)
        private = asked if flag is None else flag
        if asked and not private:
            raise _TokenKept("This request was asked privately; answer it privately.")
        if private and not reachable:
            raise _TokenKept("This caller's node cannot take private answers; reply publicly instead.")
        return private

    async def _private_reply(self, request, channel_name, body, block=None):
        pending = self.pending[request]
        if block is not None:
            body = block.fallback()
        bot, _channel = await self.owner.lane.run(deliver_private, self.bot.id, channel_name, body,
                                                  str(pending[4]["id"]))
        await self.notify(request, "", private=BotPrivateAnswer(bot.label, body, utc_now_iso(), block=block))
        return {"private": True}

    async def _model_reply(self, request, pending, *, reset, private=False):
        try:
            presence = self.owner.presence
            # Help desk's node facts include `stats` when granted, through
            # the same run_query and caller check as the query action.
            text, accept = await self.owner.model.respond(
                self.bot.id, pending[0], pending[4], pending[5], reset=reset, private=private,
                online=presence.online_usernames() if presence is not None else None,
                uptime_seconds=int(time.monotonic() - self.owner.started_at))
            if self.pending.get(request) is not pending or pending[2] <= time.monotonic():
                return
            await self._action({"action": "reply", "request": request, "text": text, "private": private})
            accept()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = str(exc) if isinstance(exc, BotError) else "Model request failed; ask the SysOp to check its settings."
            await self.status("running", reason)
            await self.notify(request, reason)
