"""
Chat channel browsing and the real-time chat loop.

Kept in its own module rather than growing login_flow.py indefinitely —
matches the project's modular-package approach (design doc §3).
"""

from __future__ import annotations

import asyncio

from netbbs.auth.users import User
from netbbs.chat import Channel, ChannelError, ChatHub, get_channel_by_name, list_channels
from netbbs.net.session import Session
from netbbs.permissions import meets_level
from netbbs.rendering import ACCENT_COLOR, HEADER_COLOR, MUTED_COLOR, colored, menu_key
from netbbs.storage.database import Database


async def browse_channels(session: Session, db: Database, hub: ChatHub, user: User) -> None:
    joinable = [c for c in list_channels(db) if meets_level(user, c.min_level)]
    if not joinable:
        await session.write_line("\r\nNo chat channels are available to you yet.")
        return

    header = colored("Available channels:", fg_color=HEADER_COLOR, bold=True)
    await session.write_line(f"\r\n{header}")
    for channel in joinable:
        online = hub.participant_count(channel.name)
        name = colored(f"#{channel.name}", fg_color=ACCENT_COLOR)
        await session.write_line(f"  {name} - {channel.description or ''} ({online} online)")

    await session.write("\r\nJoin which channel? (or press Enter to skip): ")
    choice = (await session.read_line()).strip()
    if not choice:
        return

    try:
        channel = get_channel_by_name(db, choice)
    except ChannelError:
        await session.write_line("No such channel.")
        return

    if not meets_level(user, channel.min_level):
        await session.write_line("You don't have access to that channel.")
        return

    await _chat_loop(session, hub, channel, user)


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

    Known limitation as of this writing: because we still stay in the
    client's default line-editing mode (see `netbbs.net.telnet`), an
    incoming message can land on a user's screen while they're mid-typing
    their own line, interleaving with it — the same behavior classic
    line-mode chat tools (Unix `talk`, `wall`) have always had.
    Character-at-a-time server-side input handling, which would fix this
    properly, was originally deferred but has since been pulled forward
    (see design doc phasing sign-off notes) after real testing showed
    client-side line editing behaving inconsistently (backspace not
    working, `^M` displayed literally) — this docstring will need
    updating once that work actually lands here.
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
            # Broadcast to everyone including the sender (not excluded).
            # The alternative — excluding the sender since they already
            # saw their own characters echoed back while typing (now via
            # our own character-mode echo in netbbs.net.telnet, not the
            # client's local echo the way it worked before that existed)
            # — would mean only the sender's own messages look different
            # (unformatted) from everyone else's on their own screen.
            # Seeing your own line echoed back in the same "<user> text"
            # format everyone else sees is a minor, accepted redundancy,
            # not a bug.
            username_label = colored(f"<{user.username}>", fg_color=ACCENT_COLOR)
            await hub.broadcast(channel.name, f"{username_label} {line}")

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
