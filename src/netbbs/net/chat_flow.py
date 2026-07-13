"""
Chat channel browsing and the real-time chat loop.

Kept in its own module rather than growing login_flow.py indefinitely —
matches the project's modular-package approach (design doc §3).
"""

from __future__ import annotations

import asyncio
import datetime
from dataclasses import dataclass

from netbbs.auth.users import AuthError, User, get_user_by_username
from netbbs.chat import (
    Channel,
    ChannelMessage,
    ChatHub,
    ChatModerationError,
    DurationError,
    ban_user,
    get_scrollback,
    is_banned,
    is_muted,
    kick_user,
    list_channels,
    mute_user,
    parse_duration,
    record_message,
    unban_user,
    unmute_user,
)
from netbbs.chat.categories import Category, list_subcategories, list_top_level_categories
from netbbs.net.picker import pick_item
from netbbs.net.session import Session
from netbbs.permissions import meets_level
from netbbs.rendering import ACCENT_COLOR, MUTED_COLOR, SELF_COLOR, colored, menu_key, sanitize_text
from netbbs.storage.database import Database
from netbbs.timeutil import format_for_display


async def browse_channels(session: Session, db: Database, hub: ChatHub, user: User) -> None:
    """Entry point: browse from the top level (no category selected yet)."""
    await _browse_channels_in_category(session, db, hub, user, category_id=None)


async def _browse_channels_in_category(
    session: Session, db: Database, hub: ChatHub, user: User, *, category_id: int | None
) -> None:
    """
    Browse channels within a category (or the top level), mirroring
    `netbbs.net.login_flow._browse_boards_in_category` exactly — same
    reasoning, same two-level cap, same category/item ID-namespace
    disambiguation trick (negated category IDs). See that function's
    docstring for the full rationale; not repeated here to avoid the two
    copies drifting out of sync in what they claim rather than just in
    what they say.
    """
    all_channels = [c for c in list_channels(db) if meets_level(user, c.min_level)]
    # Activity-sort applied before splitting by category, so ordering
    # within each category's channel list is still most-recent-first —
    # same node-wide default as boards (design doc round 17).
    all_channels.sort(key=lambda c: hub.last_activity(c.name) or c.created_at, reverse=True)
    all_channels.sort(key=lambda c: not c.pinned)
    channels_here = [c for c in all_channels if c.category_id == category_id]

    categories_here = (
        list_top_level_categories(db) if category_id is None else list_subcategories(db, category_id)
    )

    if not categories_here:
        channel = await pick_item(
            session,
            channels_here,
            name_of=lambda c: c.name,
            stable_id_of=lambda c: c.id,
            description_of=lambda c: _channel_description(hub, c),
            title="Available channels",
            empty_message="No chat channels are available to you yet.",
        )
        if channel is not None:
            await _chat_loop(session, db, hub, channel, user)
        return

    mixed: list[Category | Channel] = [*categories_here, *channels_here]

    def render_name(item: Category | Channel) -> str:
        return f"[{item.name}]" if isinstance(item, Category) else item.name

    def render_description(item: Category | Channel) -> str | None:
        if isinstance(item, Category):
            return item.description or "(category)"
        return _channel_description(hub, item)

    def stable_id(item: Category | Channel) -> int:
        return item.id if isinstance(item, Channel) else -item.id

    selected = await pick_item(
        session,
        mixed,
        name_of=render_name,
        stable_id_of=stable_id,
        description_of=render_description,
        title="Available channels",
        empty_message="No chat channels are available to you yet.",
    )
    if selected is None:
        return

    if isinstance(selected, Category):
        await _browse_channels_in_category(session, db, hub, user, category_id=selected.id)
    else:
        await _chat_loop(session, db, hub, selected, user)


def _channel_description(hub: ChatHub, channel: Channel) -> str:
    online = hub.participant_count(channel.name)
    base = channel.description or ""
    return f"{base} ({online} online)".strip()


def _render_scrollback_message(message: ChannelMessage) -> str:
    """
    Render a persisted `ChannelMessage` for replay on join, matching the
    live formatting `_chat_loop` itself uses for the same kind of event —
    a replay should look exactly like the original moment did, just
    delayed. Unlike live messages, no message here is ever "self"-colored
    (`netbbs.rendering.theme.SELF_COLOR`): that's a live-typing affordance
    ("this is what I just sent"), which doesn't carry any meaning when
    reading back history, possibly from a different session than whichever
    one originally sent it.
    """
    author_label = sanitize_text(message.author_label)
    if message.kind == "join":
        return colored(f"*** {author_label} has joined the channel.", fg_color=MUTED_COLOR)
    if message.kind == "leave":
        return colored(f"*** {author_label} has left the channel.", fg_color=MUTED_COLOR)
    if message.kind in _VERB_BY_KIND:
        detail = f" ({sanitize_text(message.body)})" if message.body else ""
        return colored(
            f"*** {author_label} was {_VERB_BY_KIND[message.kind]}{detail}.", fg_color=MUTED_COLOR
        )
    label = colored(f"<{author_label}>", fg_color=ACCENT_COLOR)
    return f"{label} {sanitize_text(message.body)}"


# -- mute/ban/kick (design doc §13, sign-off round 37) -----------------------

_VERB_BY_KIND = {
    "mute": "muted",
    "unmute": "unmuted",
    "ban": "banned",
    "unban": "unbanned",
    "kick": "kicked",
}


@dataclass(frozen=True)
class _KickNotice:
    """
    Delivered through `ChatHub.send_to` to force a specific live
    session out of `_chat_loop` (see that function's `receive_loop`) —
    a distinct object, not a plain string, so it can never be confused
    with real chat text passing through the same queue.
    """

    reason: str  # "kicked" or "banned" -- which notice the target sees


def _humanize_duration(duration: datetime.timedelta) -> str:
    total_seconds = int(duration.total_seconds())
    for unit_seconds, suffix in ((604800, "w"), (86400, "d"), (3600, "h"), (60, "m")):
        if total_seconds >= unit_seconds and total_seconds % unit_seconds == 0:
            return f"{total_seconds // unit_seconds}{suffix}"
    return f"{total_seconds}s"


def _split_duration_and_reason(rest: str) -> tuple[datetime.timedelta | None, str | None]:
    """
    For `/mute`/`/ban`: the first token of `rest` is tried as a
    duration; if it parses, it's consumed and the remainder is the
    reason. If it fails to parse, the duration defaults to indefinite
    and the *entire* `rest` is the reason instead.

    A deliberate, flagged-as-reconsiderable heuristic (design doc
    sign-off round 37) — matches the common `!mute @user 10m
    spamming`-style convention several existing chat moderation tools
    use, not something settled beyond this round.
    """
    if not rest:
        return None, None
    tokens = rest.split(maxsplit=1)
    try:
        duration = parse_duration(tokens[0])
    except DurationError:
        return None, rest
    reason = tokens[1] if len(tokens) > 1 else None
    return duration, reason


def _moderation_detail(actor_label: str, duration: datetime.timedelta | None, reason: str | None) -> str:
    bits = [f"by {actor_label}"]
    if duration is not None:
        bits.append(f"for {_humanize_duration(duration)}")
    if reason:
        bits.append(f"reason: {reason}")
    return ", ".join(bits)


async def _announce_moderation(
    db: Database, hub: ChatHub, channel: Channel, *, kind: str, target_label: str, detail: str
) -> None:
    """Records a scrollback event and broadcasts a system notice —
    matches the existing join/leave precedent in `_chat_loop` exactly
    (design doc §13: "all actions logged and echoed in-channel for
    transparency"). Not excluding anyone from the broadcast, unlike
    join/leave: there's no separate direct message a moderation
    action's *target* gets the way a joining/leaving user gets "Joined
    #channel" — they see the same notice as everyone else."""
    record_message(db, channel, kind=kind, author_label=target_label, body=detail)
    notice = colored(
        f"*** {sanitize_text(target_label)} was {_VERB_BY_KIND[kind]} ({sanitize_text(detail)}).",
        fg_color=MUTED_COLOR,
    )
    await hub.broadcast(channel.name, notice)


async def _resolve_target(session: Session, db: Database, username: str) -> User | None:
    """Look up a mute/ban/kick command's target username, writing a
    friendly message and returning `None` if there's no such account —
    `AuthError`'s own message is deliberately generic for login-failure
    enumeration-avoidance (see its docstring), not meant for this
    different, non-login context."""
    try:
        return get_user_by_username(db, username)
    except AuthError:
        await session.write_line(colored(f"No such user: {sanitize_text(username)!r}", fg_color=MUTED_COLOR))
        return None


async def _kick_live_sessions(hub: ChatHub, channel: Channel, target: User, *, reason: str) -> None:
    """Force out every currently-connected session belonging to
    `target` in `channel` — used by both `/kick` and `/ban` (a ban
    that doesn't remove an already-present target would be
    meaningless). A target with no live session in this channel is not
    an error; there's simply nothing to do."""
    for participant_id in hub.participant_ids(channel.name):
        if participant_id.startswith(f"{target.username}:"):
            await hub.send_to(channel.name, participant_id, _KickNotice(reason=reason))


async def _handle_mute(
    session: Session, db: Database, hub: ChatHub, channel: Channel, user: User, args_str: str
) -> None:
    parts = args_str.split(maxsplit=1)
    if not parts:
        await session.write_line(colored("Usage: /mute <user> [duration] [reason]", fg_color=MUTED_COLOR))
        return
    target_name, rest = parts[0], (parts[1] if len(parts) > 1 else "")
    duration, reason = _split_duration_and_reason(rest)

    target = await _resolve_target(session, db, target_name)
    if target is None:
        return

    try:
        mute_user(db, channel, target, duration=duration, reason=reason, muted_by=user)
    except ChatModerationError:
        await session.write_line(
            colored("You do not have permission to mute in this channel.", fg_color=MUTED_COLOR)
        )
        return

    detail = _moderation_detail(user.username, duration, reason)
    await _announce_moderation(db, hub, channel, kind="mute", target_label=target.username, detail=detail)


async def _handle_unmute(
    session: Session, db: Database, hub: ChatHub, channel: Channel, user: User, args_str: str
) -> None:
    target_name = args_str.split(maxsplit=1)[0] if args_str.split() else ""
    if not target_name:
        await session.write_line(colored("Usage: /unmute <user>", fg_color=MUTED_COLOR))
        return

    target = await _resolve_target(session, db, target_name)
    if target is None:
        return

    try:
        unmute_user(db, channel, target, unmuted_by=user)
    except ChatModerationError:
        await session.write_line(
            colored("You do not have permission to unmute in this channel.", fg_color=MUTED_COLOR)
        )
        return

    detail = _moderation_detail(user.username, None, None)
    await _announce_moderation(db, hub, channel, kind="unmute", target_label=target.username, detail=detail)


async def _handle_ban(
    session: Session, db: Database, hub: ChatHub, channel: Channel, user: User, args_str: str
) -> None:
    parts = args_str.split(maxsplit=1)
    if not parts:
        await session.write_line(colored("Usage: /ban <user> [duration] [reason]", fg_color=MUTED_COLOR))
        return
    target_name, rest = parts[0], (parts[1] if len(parts) > 1 else "")
    duration, reason = _split_duration_and_reason(rest)

    target = await _resolve_target(session, db, target_name)
    if target is None:
        return

    try:
        ban_user(db, channel, target, duration=duration, reason=reason, banned_by=user)
    except ChatModerationError:
        await session.write_line(
            colored("You do not have permission to ban in this channel.", fg_color=MUTED_COLOR)
        )
        return

    detail = _moderation_detail(user.username, duration, reason)
    await _announce_moderation(db, hub, channel, kind="ban", target_label=target.username, detail=detail)
    await _kick_live_sessions(hub, channel, target, reason="banned")


async def _handle_unban(
    session: Session, db: Database, hub: ChatHub, channel: Channel, user: User, args_str: str
) -> None:
    target_name = args_str.split(maxsplit=1)[0] if args_str.split() else ""
    if not target_name:
        await session.write_line(colored("Usage: /unban <user>", fg_color=MUTED_COLOR))
        return

    target = await _resolve_target(session, db, target_name)
    if target is None:
        return

    try:
        unban_user(db, channel, target, unbanned_by=user)
    except ChatModerationError:
        await session.write_line(
            colored("You do not have permission to unban in this channel.", fg_color=MUTED_COLOR)
        )
        return

    detail = _moderation_detail(user.username, None, None)
    await _announce_moderation(db, hub, channel, kind="unban", target_label=target.username, detail=detail)


async def _handle_kick(
    session: Session, db: Database, hub: ChatHub, channel: Channel, user: User, args_str: str
) -> None:
    parts = args_str.split(maxsplit=1)
    if not parts:
        await session.write_line(colored("Usage: /kick <user> [reason]", fg_color=MUTED_COLOR))
        return
    target_name = parts[0]
    reason = parts[1] if len(parts) > 1 else None

    target = await _resolve_target(session, db, target_name)
    if target is None:
        return

    try:
        kick_user(db, channel, target, reason=reason, kicked_by=user)
    except ChatModerationError:
        await session.write_line(
            colored("You do not have permission to kick in this channel.", fg_color=MUTED_COLOR)
        )
        return

    detail = _moderation_detail(user.username, None, reason)
    await _announce_moderation(db, hub, channel, kind="kick", target_label=target.username, detail=detail)
    await _kick_live_sessions(hub, channel, target, reason="kicked")


async def _chat_loop(
    session: Session, db: Database, hub: ChatHub, channel: Channel, user: User
) -> None:
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

    Scrollback (design doc round 19/20) is replayed here, before the
    "Joined" line, using whatever was persisted *before* this join —
    this join's own event is recorded immediately after, so it's part of
    the next person's replay, not this one's.

    Checked once, here, before doing anything else (design doc §13,
    sign-off round 37): an unexpired ban means the user never enters
    the loop at all. Mute has no equivalent join-time check — a muted
    user can still read, just not send (enforced in `send_loop`).
    """
    restriction = is_banned(db, channel, user)
    if restriction is not None:
        until = (
            "indefinitely"
            if restriction.expires_at is None
            else f"until {format_for_display(restriction.expires_at, db)}"
        )
        await session.write_line(
            colored(f"\r\nYou are banned from this channel ({until}).", fg_color=MUTED_COLOR)
        )
        return

    participant_id = f"{user.username}:{id(session)}"
    queue = hub.join(channel.name, participant_id)

    username = sanitize_text(user.username)
    channel_label = colored(f"#{sanitize_text(channel.name)}", fg_color=ACCENT_COLOR, bold=True)
    quit_hint = menu_key("/quit", " to leave")

    scrollback = get_scrollback(db, channel)
    if scrollback:
        await session.write_line(colored("--- scrollback ---", fg_color=MUTED_COLOR))
        for message in scrollback:
            await session.write_line(_render_scrollback_message(message))
        # Round 19, point 5: even bounded persistence is a different
        # promise than pure ephemeral chat — worth surfacing explicitly
        # rather than leaving as an internal implementation detail.
        await session.write_line(
            colored(
                f"--- end scrollback (last {len(scrollback)} events retained) ---",
                fg_color=MUTED_COLOR,
            )
        )

    await session.write_line(f"\r\nJoined {channel_label}. Type {quit_hint}.")
    # author_label is stored raw here (user.username, not the sanitized
    # `username` local) -- sanitize on output, not on storage, per
    # sanitize_text's docstring; only the broadcast text below is
    # actually rendered to a terminal.
    record_message(
        db, channel, kind="join", author_label=user.username, author_fingerprint=user.fingerprint
    )
    await hub.broadcast(
        channel.name,
        colored(f"*** {username} has joined the channel.", fg_color=MUTED_COLOR),
        exclude={participant_id},
    )

    async def receive_loop() -> None:
        while True:
            message = await queue.get()
            if isinstance(message, _KickNotice):
                await session.write_line(
                    colored(f"\r\n*** You have been {message.reason} from this channel.", fg_color=MUTED_COLOR)
                )
                return
            await session.write_line(message)

    async def send_loop() -> None:
        while True:
            line = (await session.read_line()).strip()
            if not line:
                continue
            if line.lower() in ("/quit", "/leave"):
                return
            if line.lower().startswith("/mute "):
                await _handle_mute(session, db, hub, channel, user, line[len("/mute "):])
                continue
            if line.lower().startswith("/unmute "):
                await _handle_unmute(session, db, hub, channel, user, line[len("/unmute "):])
                continue
            if line.lower().startswith("/ban "):
                await _handle_ban(session, db, hub, channel, user, line[len("/ban "):])
                continue
            if line.lower().startswith("/unban "):
                await _handle_unban(session, db, hub, channel, user, line[len("/unban "):])
                continue
            if line.lower().startswith("/kick "):
                await _handle_kick(session, db, hub, channel, user, line[len("/kick "):])
                continue

            restriction = is_muted(db, channel, user)
            if restriction is not None:
                until = (
                    "indefinitely"
                    if restriction.expires_at is None
                    else f"until {format_for_display(restriction.expires_at, db)}"
                )
                await session.write_line(
                    colored(f"You are muted in this channel ({until}).", fg_color=MUTED_COLOR)
                )
                continue

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
            self_label = colored(f"<{username}>", fg_color=SELF_COLOR, bold=True)
            others_label = colored(f"<{username}>", fg_color=ACCENT_COLOR)
            # Sanitized once here, used for both the direct self-write
            # and the broadcast -- receive_loop (above) writes whatever
            # arrives from the hub queue as-is, with no sanitization of
            # its own, so the broadcast payload must already be safe by
            # the time it's queued. record_message below stores the raw
            # `line`, not this sanitized copy -- sanitize on output, not
            # on storage.
            displayed_line = sanitize_text(line)
            await session.write_line(f"{self_label} {displayed_line}")
            record_message(
                db,
                channel,
                kind="message",
                author_label=user.username,
                author_fingerprint=user.fingerprint,
                body=line,
            )
            await hub.broadcast(
                channel.name, f"{others_label} {displayed_line}", exclude={participant_id}
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
        record_message(
            db, channel, kind="leave", author_label=user.username, author_fingerprint=user.fingerprint
        )
        await hub.broadcast(
            channel.name,
            colored(f"*** {username} has left the channel.", fg_color=MUTED_COLOR),
            exclude={participant_id},
        )
