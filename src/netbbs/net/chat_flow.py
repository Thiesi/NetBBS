"""
Chat channel browsing and the real-time chat loop.

Kept in its own module rather than growing login_flow.py indefinitely —
matches the project's modular-package approach (design doc §3).
"""

from __future__ import annotations

import asyncio

from netbbs.auth.users import User
from netbbs.chat import Channel, ChatHub, list_channels
from netbbs.net.picker import pick_item
from netbbs.net.session import Session
from netbbs.permissions import meets_level
from netbbs.rendering import ACCENT_COLOR, MUTED_COLOR, SELF_COLOR, colored, menu_key
from netbbs.storage.database import Database


async def browse_channels(session: Session, db: Database, hub: ChatHub, user: User) -> None:
    """
    Channel selection via the shared paginated picker
    (`netbbs.net.picker`), matching how board selection already works —
    same reasoning: typing out a full channel name shouldn't be
    required, especially given channel names can be long/descriptive
    (e.g. "channel party all day long on Monday - come and say hi", the
    exact kind of name that prompted building the picker in the first
    place).
    """
    joinable = [c for c in list_channels(db) if meets_level(user, c.min_level)]
    channel = await pick_item(
        session,
        joinable,
        name_of=lambda c: c.name,
        description_of=lambda c: _channel_description(hub, c),
        title="Available channels",
        empty_message="No chat channels are available to you yet.",
    )
    if channel is None:
        return

    await _chat_loop(session, hub, channel, user)


def _channel_description(hub: ChatHub, channel: Channel) -> str:
    online = hub.participant_count(channel.name)
    base = channel.description or ""
    return f"{base} ({online} online)".strip()


async def _chat_loop(session: Session, hub: ChatHub, channel: Channel, user: User) -> None:
    """
    Real-time chat within `channel`, until the user types /quit.

    The core architectural piece this needed, which nothing before it in
    the codebase did: a session has to be able to *receive* a broadcast
    message from another user's session while it's sitting idle waiting
    for its *own* next line of input. Solved by running two concurrent
    asyncio tasks — one reading lines from this user, one draining a
    per-participant queue of incoming broadcasts (see `netbbs.chat.hub.
    ChatHub`) — and stopping, with cleanup, as soon as either one
    finishes: the user typing /quit, or the connection dropping out from
    under either task.

    Both tasks can call `session.write()`/`write_line()` concurrently
    (`send_loop` writes the sender's own self-colored message directly;
    `receive_loop` writes whatever arrives from other participants) —
    this is safe: `TelnetSession.write()` buffers its bytes with a single
    synchronous `self._writer.write()` call before ever `await`ing
    `drain()`, so one logical message can never be interleaved
    mid-write by the other task. The only effect of the two tasks racing
    is which complete message lands on the wire first, which is exactly
    the ordering ambiguity any real-time chat already has (you might see
    someone else's message arrive while still composing your own).

    Character-mode input (server-driven echo, working Backspace, no more
    `^M` instead of a newline) landed in `netbbs.net.telnet` after real
    testing surfaced the problems client-side line editing was causing —
    an earlier version of this docstring described that as still
    deferred; it isn't anymore. The remaining scope limitation is
    narrower: no cursor-addressable line editing (arrow keys, Home/End)
    — Backspace/Delete only remove from the end of what's typed. So an
    incoming message can still land mid-typing and interleave visually
    with a user's own in-progress line, same as classic line-mode chat
    tools (Unix `talk`, `wall`) always had — full mid-line redraw to
    avoid that would need real cursor-addressable TUI machinery, still
    out of scope.
    """
    participant_id = f"{user.username}:{id(session)}"
    queue = hub.join(channel.name, participant_id)

    channel_label = colored(f"#{channel.name}", fg_color=ACCENT_COLOR, bold=True)
    quit_hint = menu_key("/quit", " to leave")
    await session.write_line(f"\r\nJoined {channel_label}. Type {quit_hint}.")
    await hub.broadcast(
        channel.name,
        colored(f"*** {user.username} has joined the channel.", fg_color=MUTED_COLOR),
        exclude={participant_id},
    )

    async def receive_loop() -> None:
        while True:
            message = await queue.get()
            await session.write_line(message)

    async def send_loop() -> None:
        while True:
            line = (await session.read_line()).strip()
            if not line:
                continue
            if line.lower() in ("/quit", "/leave"):
                return
            # Two differently-colored copies of the same message, not
            # one broadcast to everyone: the sender gets a direct write
            # using SELF_COLOR so their own messages visually stand out
            # from the rest of the conversation, while everyone else
            # receives the normal ACCENT_COLOR-formatted version via the
            # broadcast (sender excluded this time, unlike before —
            # they're getting their own copy directly instead). This
            # can't be done as a single shared broadcast string the way
            # join/leave notices are, since it's genuinely different
            # text per recipient.
            self_label = colored(f"<{user.username}>", fg_color=SELF_COLOR, bold=True)
            others_label = colored(f"<{user.username}>", fg_color=ACCENT_COLOR)
            await session.write_line(f"{self_label} {line}")
            await hub.broadcast(
                channel.name, f"{others_label} {line}", exclude={participant_id}
            )

    receive_task = asyncio.create_task(receive_loop())
    send_task = asyncio.create_task(send_loop())

    try:
        done, pending = await asyncio.wait(
            {receive_task, send_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        # Properly await cancelled tasks rather than fire-and-forget —
        # otherwise asyncio can warn "Task was destroyed but it is
        # pending" and the cancellation may not actually finish cleanly
        # before this function returns.
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()  # re-raise, e.g. SessionClosedError from a dropped connection
    finally:
        hub.leave(channel.name, participant_id)
        await hub.broadcast(
            channel.name,
            colored(f"*** {user.username} has left the channel.", fg_color=MUTED_COLOR),
            exclude={participant_id},
        )
