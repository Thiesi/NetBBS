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
    Bot, BotError, allowed_channel, list_bots, publish, select_request, set_runtime_status,
)
from netbbs.bots.state import read_state, write_state, state_path
from netbbs.rendering import sanitize_text

MAX_LINE_BYTES = 32768
MAX_PENDING = 32
REQUEST_SECONDS = 60
_logger = logging.getLogger(__name__)


class BotRuntime:
    def __init__(self, lane, hub, *, poll_seconds: float = 1.0, restart_seconds: float = 1.0):
        self.lane, self.hub = lane, hub
        self.poll_seconds, self.restart_seconds = poll_seconds, restart_seconds
        self.workers: dict[int, _Worker] = {}
        self.task: asyncio.Task | None = None
        self.link = None

    @property
    def running_ids(self) -> frozenset[int]:
        return frozenset(i for i, w in list(self.workers.items()) if w.ready)

    def start(self) -> None:
        if self.task is None:
            self.hub.bot_runtime = self
            self.task = asyncio.create_task(self._run(), name="bot-manager")
            if self.link is not None:
                self.link.start()

    async def _run(self) -> None:
        try:
            while True:
                try:
                    desired = {b.id: b for b in await self.lane.run(list_bots) if b.enabled}
                    for bot_id, worker in list(self.workers.items()):
                        if desired.get(bot_id) != worker.bot or worker.task.done():
                            await worker.stop()
                            del self.workers[bot_id]
                    for bot_id, bot in desired.items():
                        if bot_id not in self.workers:
                            worker = _Worker(self, bot)
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
            await self.enqueue(bot, channel, {"id": user.id, "name": user.username}, text, participant_id)
        except BotError as exc:
            return str(exc)
        return None

    async def enqueue(self, bot, channel, caller, text, participant_id=None, *, completed=None):
        await self.lane.run(allowed_channel, bot.id, channel.name, check_rate=True)
        worker = self.workers.get(bot.id)
        if worker is None or not worker.ready:
            raise BotError(f"{bot.label} is not ready. The SysOp can check its status in Chat bots.")
        if len(worker.pending) >= MAX_PENDING or worker.events.full():
            raise BotError(f"{bot.label} is busy; try again shortly.")
        request = uuid.uuid4().hex
        worker.pending[request] = (channel.name, participant_id, time.monotonic() + REQUEST_SECONDS, completed)
        worker.events.put_nowait({"type": "message", "request": request, "channel": channel.name,
                                 "caller": caller, "text": text})


class _Worker:
    def __init__(self, owner: BotRuntime, bot: Bot):
        self.owner, self.bot = owner, bot
        self.events: asyncio.Queue = asyncio.Queue(maxsize=64)
        self.pending: dict[str, tuple] = {}
        self.ready = False
        self.proc = None
        self.task = None
        self.actions = deque()

    async def status(self, state: str, error: str = "") -> None:
        await self.owner.lane.run(set_runtime_status, self.bot.id, state, error)

    async def notify(self, request: str, text: str, message=None) -> None:
        target = self.pending.pop(request, None)
        if target:
            if target[3] is not None:
                await target[3](text, message)
            elif text:
                await self.owner.hub.send_to(target[0], target[1],
                                             f"{self.bot.label}: {sanitize_text(text)}")

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
        state = await lane.run(read_state, self.bot.storage_name)
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
                               "temperament": self.bot.temperament, "state": state})
            ready = await asyncio.wait_for(self._read(), 5)
            if ready != {"type": "ready", "version": BOT_API_VERSION}:
                raise BotError("Bot did not acknowledge BOT_API_VERSION 1.")
            self.ready = True
            await self.status("running")
            tasks.extend([asyncio.create_task(self._reader()), asyncio.create_task(self._writer()),
                          asyncio.create_task(self._ticks())])
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
            await self._reap()

    async def _reap(self) -> None:
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.returncode is None:
                if os.name == "nt":
                    proc.terminate()
                else:
                    os.killpg(proc.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), 2)
                except asyncio.TimeoutError:
                    if os.name == "nt":
                        proc.kill()
                    else:
                        os.killpg(proc.pid, signal.SIGKILL)
                    await asyncio.wait_for(proc.wait(), 2)
        except ProcessLookupError:
            await proc.wait()
        finally:
            if proc.stdin:
                proc.stdin.close()
            self.proc = None

    async def _read(self) -> dict:
        try:
            line = await self.proc.stdout.readline()
            if not line:
                raise BotError("Bot closed its output pipe.")
            if len(line) > MAX_LINE_BYTES:
                raise BotError("Bot exceeded the 32 KiB protocol line limit.")
            value = json.loads(line)
            if not isinstance(value, dict):
                raise BotError("Bot protocol requires JSON objects.")
            return value
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise BotError("Bot sent invalid or oversized JSON.") from exc

    async def _write(self, value: dict) -> None:
        raw = (json.dumps(value, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
        if len(raw) > MAX_LINE_BYTES:
            raise BotError("Host event exceeds the protocol line limit.")
        self.proc.stdin.write(raw)
        await asyncio.wait_for(self.proc.stdin.drain(), 5)

    async def _drain_stderr(self) -> None:
        while await self.proc.stderr.read(4096):
            pass

    async def _writer(self) -> None:
        while True:
            event = await self.events.get()
            if event["type"] == "message":
                try:
                    await self.owner.lane.run(allowed_channel, self.bot.id, event["channel"])
                except BotError as exc:
                    await self.notify(event["request"], str(exc))
                    continue
            await self._write(event)

    async def _ticks(self) -> None:
        while True:
            await asyncio.sleep(1)
            for request, (_, _, deadline, _) in list(self.pending.items()):
                if time.monotonic() >= deadline:
                    await self.notify(request, "The bot did not reply within 60 seconds.")
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
                await self.status("running", str(exc))
                request = value.get("request")
                if isinstance(request, str):
                    await self.notify(request, str(exc))
            if self.events.full():
                raise BotError("Bot stopped consuming host events.")
            self.events.put_nowait(result)

    async def _action(self, value: dict):
        action = value.get("action")
        lane = self.owner.lane
        if action == "state.get":
            return await lane.run(read_state, self.bot.storage_name)
        if action == "state.set":
            await lane.run(write_state, self.bot.storage_name, value.get("value"))
            return None
        if action not in {"say", "reply"}:
            raise BotError("Unknown action or missing grant. Available: say, reply, state.get, state.set.")
        body = value.get("text")
        request = value.get("request")
        if action == "reply":
            if not isinstance(request, str) or request not in self.pending:
                raise BotError("Reply request has expired or does not belong to this bot.")
            channel_name = self.pending[request][0]
        else:
            channel_name = value.get("channel")
            if not isinstance(channel_name, str):
                raise BotError("Say needs an allowed channel name.")
        link = self.owner.link
        channel, message = await lane.run(publish, self.bot.id, channel_name, body,
                                          node_identity=link.node.identity if link else None)
        await self.owner.hub.broadcast(channel.name, message)
        if link is not None:
            await link.bridge.broadcast_local_message_live(channel, message)
        if action == "reply":
            await self.notify(request, "", message)
        return {"message_id": message.id}
