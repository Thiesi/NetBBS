"""
Chat channel browsing and the real-time chat loop.

Kept in its own module rather than growing login_flow.py indefinitely —
matches the project's modular-package approach (design doc §3).
"""

from __future__ import annotations

import asyncio
import datetime
from dataclasses import dataclass
from typing import Awaitable, Callable

from netbbs.auth.users import AuthError, User, get_user_by_username, list_users
from netbbs.chat import (
    Channel,
    ChannelError,
    ChannelMessage,
    ChatHub,
    ChatModerationError,
    DurationError,
    MembershipError,
    MessageMailbox,
    NickError,
    PresenceRegistry,
    TopicError,
    accept_invitation,
    add_member,
    ban_user,
    chat_stream_label,
    create_invitation,
    display_label,
    get_channel_by_name,
    get_nick,
    get_scrollback,
    has_pending_invitation,
    is_banned,
    is_member,
    is_muted,
    kick_user,
    list_channels,
    list_members,
    mute_user,
    parse_duration,
    record_message,
    remove_member,
    revoke_invitation,
    set_nick,
    set_topic,
    unban_user,
    unmute_user,
)
from netbbs.chat.categories import Category, list_subcategories, list_top_level_categories
from netbbs.directory import VCard, get_vcard
from netbbs.moderation import ChannelPermission, has_permission
from netbbs.net.char_input import Completer, InputHistory
from netbbs.net.picker import pick_item
from netbbs.net.session import Session
from netbbs.permissions import meets_level
from netbbs.rendering import ACCENT_COLOR, MUTED_COLOR, SELF_COLOR, colored, menu_key, sanitize_text
from netbbs.storage.database import Database
from netbbs.timeutil import format_for_display


async def browse_channels(
    session: Session,
    db: Database,
    hub: ChatHub,
    presence: PresenceRegistry,
    mailbox: MessageMailbox,
    history: InputHistory,
    user: User,
) -> None:
    """
    Entry point: browse from the top level, then run the chat loop for
    whatever's picked.

    A small outer loop, not a single call (design doc §8/round 33,
    sign-off round 44 — Track 5d): `/leave` returns here to pick again,
    `/join` jumps straight back into `_chat_loop` with an already-
    validated channel without going through the picker at all, and
    `/quit` (or a kick/dropped connection) exits out to the caller (the
    main menu). Always re-enters the picker at the *top* level on
    `/leave`, never wherever a category-nested pick left off — the same
    "back always lands somewhere consistent" reasoning `/quit` already
    followed, not a new decision.

    `history` (design doc round 47/Track 5f) is the one connection's
    `InputHistory`, constructed once in `netbbs.net.login_flow.
    handle_session` — passed straight through to every `_chat_loop`
    call here so command recall persists across a `/join` channel
    switch rather than resetting.
    """
    channel = await _pick_channel(session, db, hub, user, category_id=None)
    while channel is not None:
        action = await _chat_loop(session, db, hub, presence, mailbox, history, channel, user)
        if isinstance(action, _SwitchTo):
            channel = action.channel
            continue
        if isinstance(action, _ToPicker):
            channel = await _pick_channel(session, db, hub, user, category_id=None)
            continue
        return  # _Quit


def _visible_channels_for(db: Database, user: User) -> list[Channel]:
    """
    Every channel `user` is allowed to *see* — the shared filter behind
    the picker, `/list`, and `/whois`'s channel-membership display
    (design doc round 33 point 9, Phase 2 Track 5h). Consolidates what
    were three separate, slightly-duplicated `meets_level`-only list
    comprehensions (round 43) into one place, adding the hidden-channel
    condition: level still gates access exactly as before, and a
    `hidden` channel is additionally excluded unless the user is
    already a member, holds a pending invitation, or holds *any*
    moderator grant on it (checked via `has_permission` with every
    `ChannelPermission` bit combined — "does the user hold any of
    these," not one specific bit).

    A `members_only`-but-not-`hidden` channel still appears here —
    "hidden + open is obscurity, not access control" (round 33 point
    9): only `hidden` controls listing visibility itself; `members_only`
    alone just means you can see it exists but can't `/join` it without
    access (enforced separately, in `_handle_join`).
    """
    visible = []
    for channel in list_channels(db):
        if not meets_level(user, channel.min_level):
            continue
        if channel.hidden and not (
            is_member(db, channel, user)
            or has_pending_invitation(db, channel, user)
            or has_permission(
                db, user, object_type="channel", object_id=channel.id,
                permission=ChannelPermission.EDIT | ChannelPermission.MODERATE | ChannelPermission.MANAGE_MEMBERS,
            )
        ):
            continue
        visible.append(channel)
    return visible


async def _pick_channel(
    session: Session,
    db: Database,
    hub: ChatHub,
    user: User,
    *,
    category_id: int | None,
) -> Channel | None:
    """
    Browse channels within a category (or the top level) and return
    whichever one the user picks, or `None` if they back out — mirrors
    `netbbs.net.login_flow._browse_boards_in_category` exactly — same
    reasoning, same two-level cap, same category/item ID-namespace
    disambiguation trick (negated category IDs). See that function's
    docstring for the full rationale; not repeated here to avoid the two
    copies drifting out of sync in what they claim rather than just in
    what they say.
    """
    all_channels = _visible_channels_for(db, user)
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
        return await pick_item(
            session,
            channels_here,
            name_of=lambda c: c.name,
            stable_id_of=lambda c: c.id,
            description_of=lambda c: _channel_description(hub, c),
            title="Available channels",
            empty_message="No chat channels are available to you yet.",
        )

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
        return None

    if isinstance(selected, Category):
        return await _pick_channel(session, db, hub, user, category_id=selected.id)
    return selected


def _channel_description(hub: ChatHub, channel: Channel) -> str:
    online = hub.participant_count(channel.name)
    base = channel.description or ""
    return f"{base} ({online} online)".strip()


def _resolve_chat_stream_label(db: Database, author_label: str) -> str:
    """
    Best-effort live alias lookup for scrollback replay (design doc
    round 32/41, switched to the marked nick-only form in round 53):
    there's no per-message nick snapshot — an alias is presentation
    metadata looked up live, not stored history — so replay shows the
    author's *current* alias, not whatever was set at the original
    moment. Falls back to the stored canonical label if the account can
    no longer be found (defensive; no account-deletion feature exists
    yet to actually trigger this).

    `chat_stream_label` already sanitizes internally — no second
    `sanitize_text` wrap here, which would risk stripping its own
    legitimate color codes rather than just hostile content.
    """
    try:
        author = get_user_by_username(db, author_label)
    except AuthError:
        return sanitize_text(author_label)
    return chat_stream_label(db, author)


def _render_scrollback_message(db: Database, message: ChannelMessage) -> str:
    """
    Render a persisted `ChannelMessage` for replay on join, matching the
    live formatting `_chat_loop` itself uses for the same kind of event —
    a replay should look exactly like the original moment did, just
    delayed. Unlike live messages, no message here is ever "self"-colored
    (`netbbs.rendering.theme.SELF_COLOR`): that's a live-typing affordance
    ("this is what I just sent"), which doesn't carry any meaning when
    reading back history, possibly from a different session than whichever
    one originally sent it.

    `join`/`leave`/`action`/plain-message kinds show the author's
    *current* alias, nick-only-plus-marker if one is set
    (`_resolve_chat_stream_label`, design doc round 53) — moderation
    kinds (`_VERB_BY_KIND`) and `nick` itself deliberately don't:
    moderation/auditing always shows canonical identity only (design
    doc round 32, point 7), and a `nick` event's own body text already
    fully describes the change.
    """
    if message.kind == "join":
        return colored(
            f"*** {_resolve_chat_stream_label(db, message.author_label)} has joined the channel.",
            fg_color=MUTED_COLOR,
        )
    if message.kind == "leave":
        return colored(
            f"*** {_resolve_chat_stream_label(db, message.author_label)} has left the channel.",
            fg_color=MUTED_COLOR,
        )
    if message.kind == "action":
        return colored(
            f"* {_resolve_chat_stream_label(db, message.author_label)} {sanitize_text(message.body)}",
            fg_color=MUTED_COLOR,
        )
    if message.kind == "nick":
        return colored(
            f"*** {sanitize_text(message.author_label)} {sanitize_text(message.body)}",
            fg_color=MUTED_COLOR,
        )
    if message.kind in _VERB_BY_KIND:
        author_label = sanitize_text(message.author_label)
        detail = f" ({sanitize_text(message.body)})" if message.body else ""
        return colored(
            f"*** {author_label} was {_VERB_BY_KIND[message.kind]}{detail}.", fg_color=MUTED_COLOR
        )
    label = colored(f"<{_resolve_chat_stream_label(db, message.author_label)}>", fg_color=ACCENT_COLOR)
    return f"{label} {sanitize_text(message.body)}"


# -- command dispatch (design doc §13, sign-off round 39; ChatAction result
# type widened from a bare bool in round 44/Track 5d) ------------------------


@dataclass(frozen=True)
class ChatCommandContext:
    """Everything a slash-command handler might need, bundled into one
    consistent shape — replaces what used to be a different ad hoc
    positional-argument list per handler (Track 3/4's `/mute` etc. each
    took their own subset of `session, db, hub, channel, user`;
    `/finger` omitted `hub` entirely)."""

    session: Session
    db: Database
    hub: ChatHub
    presence: PresenceRegistry
    mailbox: MessageMailbox
    channel: Channel
    user: User
    participant_id: str


@dataclass(frozen=True)
class _Quit:
    """Exit chat entirely, back to the main menu — `/quit`'s meaning,
    and also what a kick/ban or a dropped connection resolves to."""


@dataclass(frozen=True)
class _ToPicker:
    """Exit the current channel, back to channel selection — `/leave`'s
    meaning (Track 5d; previously an alias for `/quit`)."""


@dataclass(frozen=True)
class _SwitchTo:
    """Jump directly to an already-validated channel — `/join
    <channel>`'s meaning. Carries the resolved `Channel`, not just a
    name: resolution/authorization already happened in the handler, so
    the outer loop (`browse_channels`) doesn't need to repeat it."""

    channel: Channel


@dataclass(frozen=True)
class _EnterPrivate:
    """Enter private-conversation mode targeting `target` — `/private
    <user>`'s meaning (Track 5e). Unlike `_ToPicker`/`_SwitchTo`, this
    never propagates past `send_loop` — it's consumed entirely there,
    updating its own local `private_target` variable, since entering
    private mode doesn't change anything about *which channel* the loop
    is running in."""

    target: User


@dataclass(frozen=True)
class _ExitPrivate:
    """Leave private-conversation mode, back to ordinary channel input —
    `/close`'s meaning. Same "consumed entirely inside `send_loop`"
    scope as `_EnterPrivate`."""


# What a command handler returns after running: `None` means "continue
# the chat loop as normal." A `ChatAction` means "something about the
# loop itself needs to change" — propagated all the way up through
# `_dispatch_command`/`send_loop`/`_chat_loop` to `browse_channels`
# (`_Quit`/`_ToPicker`/`_SwitchTo`), or consumed directly inside
# `send_loop` without going any further (`_EnterPrivate`/`_ExitPrivate`,
# Track 5e — see their own docstrings). Originally just `bool` (round
# 39; only `/quit` ever returned `True`) — widened, not replaced, each
# time a new command needed to distinguish another outcome from plain
# "keep going": the same "explicit return contract, not exceptions"
# reasoning round 39 already established, just with more to say than a
# single bit could carry.
ChatAction = _Quit | _ToPicker | _SwitchTo | _EnterPrivate | _ExitPrivate
CommandHandler = Callable[[ChatCommandContext, str], Awaitable[ChatAction | None]]


async def _handle_quit(ctx: ChatCommandContext, args: str) -> ChatAction:
    return _Quit()


async def _handle_leave(ctx: ChatCommandContext, args: str) -> ChatAction:
    return _ToPicker()


async def _handle_join(ctx: ChatCommandContext, args: str) -> ChatAction | None:
    """
    `/join <channel>` -- see `_handle_leave`/`ChatAction` for the outer
    loop's side of channel switching.

    Also doubles as invitation *acceptance* (design doc round 33, Phase
    2 Track 5h): there is no separate `/accept` command — successfully
    joining a `members_only` channel via a pending invitation marks it
    accepted, reusing this existing "look up, check authorization,
    switch" flow instead of inventing parallel command surface for the
    same action.
    """
    channel_name = args.strip()
    if not channel_name:
        await _show_usage(ctx.session, "join")
        return None

    try:
        channel = get_channel_by_name(ctx.db, channel_name)
    except ChannelError:
        await ctx.session.write_line(
            colored(f"No such channel: {sanitize_text(channel_name)!r}", fg_color=MUTED_COLOR)
        )
        return None

    if not meets_level(ctx.user, channel.min_level):
        await ctx.session.write_line(
            colored("You are not authorized to join that channel.", fg_color=MUTED_COLOR)
        )
        return None

    already_member = is_member(ctx.db, channel, ctx.user)
    if channel.members_only and not already_member and not has_pending_invitation(ctx.db, channel, ctx.user):
        await ctx.session.write_line(
            colored("You are not authorized to join that channel.", fg_color=MUTED_COLOR)
        )
        return None

    if channel.id == ctx.channel.id:
        await ctx.session.write_line(
            colored(f"You are already in #{sanitize_text(channel.name)}.", fg_color=MUTED_COLOR)
        )
        return None

    if channel.members_only and not already_member:
        accept_invitation(ctx.db, channel, ctx.user)

    return _SwitchTo(channel)


async def _handle_topic(ctx: ChatCommandContext, args: str) -> None:
    """
    `/topic` with no arguments shows the current topic — viewable by
    anyone already in the channel, no separate permission check, since
    being here at all already implies visibility. `/topic <text>`
    attempts to change it, gated by `ChannelPermission.EDIT` (see
    `netbbs.chat.channels.set_topic`). Deliberately not persisted into
    scrollback (design doc round 33 point 5 only asks for moderation-log
    history, unlike `/nick`'s explicit scrollback requirement) — a live
    in-channel notice plus the audit log entry `set_topic` already
    writes is enough.

    Viewing re-fetches the channel fresh from the database rather than
    trusting `ctx.channel.topic` — `ctx.channel` is a snapshot taken once
    per `_chat_loop` invocation (a frozen dataclass, never mutated in
    place), so it would otherwise still show the *old* topic for the
    rest of the session after a successful change, the same "look it up
    fresh, don't cache" reasoning `display_label` already follows for
    `/nick`.
    """
    if not args:
        current = get_channel_by_name(ctx.db, ctx.channel.name)
        if current.topic:
            await ctx.session.write_line(f"Topic: {sanitize_text(current.topic)}")
        else:
            await ctx.session.write_line(colored("No topic set.", fg_color=MUTED_COLOR))
        return

    try:
        set_topic(ctx.db, ctx.channel, args, set_by=ctx.user)
    except TopicError:
        await ctx.session.write_line(
            colored("You do not have permission to change the topic.", fg_color=MUTED_COLOR)
        )
        return

    notice = colored(
        f"*** Topic changed by {sanitize_text(ctx.user.username)}: {sanitize_text(args)}",
        fg_color=MUTED_COLOR,
    )
    await ctx.session.write_line(notice)
    await ctx.hub.broadcast(ctx.channel.name, notice, exclude={ctx.participant_id})


def _find_live_participant(hub: ChatHub, db: Database, username: str) -> tuple[str, str] | None:
    """
    Every channel's roster is checked in turn for a live session
    belonging to `username` — `ChatHub` has no reverse "which channel is
    this user in" index, the same O(channels) shape as
    `_channel_names_for_user`/`_kick_live_sessions`, which already parse
    the same `"username:id(session)"` convention for the same reason.
    Returns `(channel_name, participant_id)` for the first live session
    found, or `None` if `username` has no live session in any channel
    right now (e.g. online but browsing boards, or between channels) —
    exactly the case `netbbs.chat.mailbox.MessageMailbox` exists for.
    """
    for channel in list_channels(db):
        for participant_id in hub.participant_ids(channel.name):
            if participant_id.startswith(f"{username}:"):
                return channel.name, participant_id
    return None


async def _deliver_private_message(ctx: ChatCommandContext, target: User, body: str) -> None:
    """
    Delivers a private message to `target`: instantly, via the existing
    `ChatHub`, if they currently have a live session in some channel
    (the same delivery mechanism moderation notices already use, just a
    differently-formatted string — no new `receive_loop` branch needed);
    otherwise queued in the mailbox for their next natural prompt
    (design doc round 32, sign-off round 46/Track 5e — mailbox +
    next-prompt delivery, confirmed with Thiesi over full session-wide
    live interrupt delivery, which nothing outside `_chat_loop` has any
    mechanism for today — see `netbbs.chat.mailbox`'s module docstring).

    Never written to scrollback or the moderation log — round 32 point
    1's "online-only" private messages are intentionally as ephemeral as
    live chat itself.
    """
    sender_label = sanitize_text(display_label(ctx.db, ctx.user))
    notice = colored(
        f"*** Private message from {sender_label}: {sanitize_text(body)}",
        fg_color=MUTED_COLOR,
        bold=True,
    )

    live = _find_live_participant(ctx.hub, ctx.db, target.username)
    if live is not None:
        channel_name, participant_id = live
        await ctx.hub.send_to(channel_name, participant_id, notice)
    else:
        ctx.mailbox.deliver(target.username, notice)

    await ctx.session.write_line(
        colored(f"(sent to {sanitize_text(target.username)})", fg_color=MUTED_COLOR)
    )


async def _handle_msg(ctx: ChatCommandContext, args: str) -> None:
    """
    `/msg <user> <text>` (design doc round 32 point 1): a one-off,
    online-only private message. Scoped as a chat-context command only,
    matching every command Tracks 3-5d already built — no parallel
    main-menu entry point.
    """
    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        await _show_usage(ctx.session, "msg")
        return
    target_name, body = parts

    target = await _resolve_target(ctx.session, ctx.db, target_name)
    if target is None:
        return

    if not ctx.presence.is_online(target.username):
        await ctx.session.write_line(
            colored(f"{sanitize_text(target.username)} is not currently online.", fg_color=MUTED_COLOR)
        )
        return

    await _deliver_private_message(ctx, target, body)


async def _handle_private(ctx: ChatCommandContext, args: str) -> ChatAction | None:
    """
    `/private <user>` (design doc round 33 point 1): enters a temporary
    private-conversation mode layered on `/msg` — ordinary (non-slash)
    input is sent privately to `target` until `/close`. The old `/query`
    IRC-compatibility alias for this handler was removed in round 54 —
    it was the only command with two names and added no value beyond
    what `/private` already provides.
    """
    target_name = args.split(maxsplit=1)[0] if args.split() else ""
    if not target_name:
        await _show_usage(ctx.session, "private")
        return None

    target = await _resolve_target(ctx.session, ctx.db, target_name)
    if target is None:
        return None

    if not ctx.presence.is_online(target.username):
        await ctx.session.write_line(
            colored(f"{sanitize_text(target.username)} is not currently online.", fg_color=MUTED_COLOR)
        )
        return None

    close_hint = menu_key("/close", "")
    await ctx.session.write_line(
        colored(
            f"Entering private conversation with {sanitize_text(target.username)}. "
            f"Type {close_hint} to return.",
            fg_color=MUTED_COLOR,
        )
    )
    return _EnterPrivate(target)


async def _handle_close(ctx: ChatCommandContext, args: str) -> ChatAction:
    return _ExitPrivate()


async def _handle_help(ctx: ChatCommandContext, args: str) -> None:
    """
    `/help` (no args, design doc round 55): lists every command visible
    to the caller — reuses the exact same `_COMMAND_VISIBILITY`
    predicate Tab completion (round 49/Track 5g) already applies, so
    the list matches what `/` + Tab would offer, with syntax and a
    one-line description attached to each command — the actual
    value-add over Tab completion this round was built to provide (the
    old version just printed the same bare name list Tab completion
    already surfaces).

    `/help <command>` shows one command's full detail regardless of the
    caller's own visibility for it — consistent with Track 5g's
    established framing that visibility gating is a suggestion filter,
    not an authorization check: asking about a command by name is a
    deliberate, explicit request, not a passive listing a non-moderator
    shouldn't be nudged toward.
    """
    target = args.strip().lstrip("/").lower()
    if target:
        info = _COMMAND_INFO.get(target)
        if info is None:
            await ctx.session.write_line(
                colored(f"Unknown command: /{sanitize_text(target)}", fg_color=MUTED_COLOR)
            )
            return
        syntax, description = info
        await ctx.session.write_line(colored(syntax, fg_color=MUTED_COLOR, bold=True))
        await ctx.session.write_line(description)
        return

    await ctx.session.write_line(colored("Available commands:", fg_color=MUTED_COLOR, bold=True))
    visible_names = sorted(
        name
        for name in _COMMANDS
        if name in _COMMAND_INFO
        and (_COMMAND_VISIBILITY.get(name) is None or _COMMAND_VISIBILITY[name](ctx.db, ctx.channel, ctx.user))
    )
    for name in visible_names:
        syntax, description = _COMMAND_INFO[name]
        await ctx.session.write_line(f"{colored(syntax, fg_color=MUTED_COLOR, bold=True)} - {description}")


async def _dispatch_command(ctx: ChatCommandContext, line: str) -> ChatAction | None:
    """
    `line` is known to start with `/` (checked by the caller). Any
    such line is now always treated as a command attempt — looked up
    in `_COMMANDS`, and if not found, "Unknown command" is shown and
    nothing is broadcast. Previously (Track 3/4), an unrecognized `/x`
    line fell all the way through to being sent as an ordinary chat
    message, since the old ad hoc `if` chain only checked for the
    specific commands it knew about — a typo'd command silently became
    public chat text. Standard behavior for slash-command chat systems
    (IRC/Discord/Slack all reserve leading `/` the same way).
    """
    command_word, _, rest = line[1:].partition(" ")
    handler = _COMMANDS.get(command_word.lower())
    if handler is None:
        await ctx.session.write_line(
            colored(f"Unknown command: /{command_word}", fg_color=MUTED_COLOR)
        )
        return None
    return await handler(ctx, rest)


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


async def _show_usage(session: Session, command: str) -> None:
    """Writes the standard `"Usage: /command <args>"` message for
    `command`, generated from `_COMMAND_INFO` (design doc round 55) —
    the single source of truth for command syntax, also used by
    `_handle_help`, instead of each handler carrying its own
    independently-maintained copy of the same text."""
    syntax, _ = _COMMAND_INFO[command]
    await session.write_line(colored(f"Usage: {syntax}", fg_color=MUTED_COLOR))


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


async def _handle_mute(ctx: ChatCommandContext, args: str) -> None:
    parts = args.split(maxsplit=1)
    if not parts:
        await _show_usage(ctx.session, "mute")
        return
    target_name, rest = parts[0], (parts[1] if len(parts) > 1 else "")
    duration, reason = _split_duration_and_reason(rest)

    target = await _resolve_target(ctx.session, ctx.db, target_name)
    if target is None:
        return

    try:
        mute_user(ctx.db, ctx.channel, target, duration=duration, reason=reason, muted_by=ctx.user)
    except ChatModerationError:
        await ctx.session.write_line(
            colored("You do not have permission to mute in this channel.", fg_color=MUTED_COLOR)
        )
        return

    detail = _moderation_detail(ctx.user.username, duration, reason)
    await _announce_moderation(ctx.db, ctx.hub, ctx.channel, kind="mute", target_label=target.username, detail=detail)


async def _handle_unmute(ctx: ChatCommandContext, args: str) -> None:
    target_name = args.split(maxsplit=1)[0] if args.split() else ""
    if not target_name:
        await _show_usage(ctx.session, "unmute")
        return

    target = await _resolve_target(ctx.session, ctx.db, target_name)
    if target is None:
        return

    try:
        unmute_user(ctx.db, ctx.channel, target, unmuted_by=ctx.user)
    except ChatModerationError:
        await ctx.session.write_line(
            colored("You do not have permission to unmute in this channel.", fg_color=MUTED_COLOR)
        )
        return

    detail = _moderation_detail(ctx.user.username, None, None)
    await _announce_moderation(
        ctx.db, ctx.hub, ctx.channel, kind="unmute", target_label=target.username, detail=detail
    )


async def _handle_ban(ctx: ChatCommandContext, args: str) -> None:
    parts = args.split(maxsplit=1)
    if not parts:
        await _show_usage(ctx.session, "ban")
        return
    target_name, rest = parts[0], (parts[1] if len(parts) > 1 else "")
    duration, reason = _split_duration_and_reason(rest)

    target = await _resolve_target(ctx.session, ctx.db, target_name)
    if target is None:
        return

    try:
        ban_user(ctx.db, ctx.channel, target, duration=duration, reason=reason, banned_by=ctx.user)
    except ChatModerationError:
        await ctx.session.write_line(
            colored("You do not have permission to ban in this channel.", fg_color=MUTED_COLOR)
        )
        return

    detail = _moderation_detail(ctx.user.username, duration, reason)
    await _announce_moderation(ctx.db, ctx.hub, ctx.channel, kind="ban", target_label=target.username, detail=detail)
    await _kick_live_sessions(ctx.hub, ctx.channel, target, reason="banned")


async def _handle_unban(ctx: ChatCommandContext, args: str) -> None:
    target_name = args.split(maxsplit=1)[0] if args.split() else ""
    if not target_name:
        await _show_usage(ctx.session, "unban")
        return

    target = await _resolve_target(ctx.session, ctx.db, target_name)
    if target is None:
        return

    try:
        unban_user(ctx.db, ctx.channel, target, unbanned_by=ctx.user)
    except ChatModerationError:
        await ctx.session.write_line(
            colored("You do not have permission to unban in this channel.", fg_color=MUTED_COLOR)
        )
        return

    detail = _moderation_detail(ctx.user.username, None, None)
    await _announce_moderation(
        ctx.db, ctx.hub, ctx.channel, kind="unban", target_label=target.username, detail=detail
    )


async def _handle_kick(ctx: ChatCommandContext, args: str) -> None:
    parts = args.split(maxsplit=1)
    if not parts:
        await _show_usage(ctx.session, "kick")
        return
    target_name = parts[0]
    reason = parts[1] if len(parts) > 1 else None

    target = await _resolve_target(ctx.session, ctx.db, target_name)
    if target is None:
        return

    try:
        kick_user(ctx.db, ctx.channel, target, reason=reason, kicked_by=ctx.user)
    except ChatModerationError:
        await ctx.session.write_line(
            colored("You do not have permission to kick in this channel.", fg_color=MUTED_COLOR)
        )
        return

    detail = _moderation_detail(ctx.user.username, None, reason)
    await _announce_moderation(ctx.db, ctx.hub, ctx.channel, kind="kick", target_label=target.username, detail=detail)
    await _kick_live_sessions(ctx.hub, ctx.channel, target, reason="kicked")


async def _write_vcard_detail(session: Session, db: Database, vcard: VCard) -> None:
    """Shared by `/finger` and `/whois` (design doc round 32/43) — the
    identity/bio block both commands show identically; `/whois`
    appends online/away/channel-membership lines of its own after
    calling this."""
    when = format_for_display(vcard.created_at, db)
    await session.write_line(colored(f"\r\n{sanitize_text(vcard.username)}", fg_color=ACCENT_COLOR, bold=True))
    await session.write_line(f"Member since: {when}")
    if vcard.bio is not None:
        await session.write_line(sanitize_text(vcard.bio, allow_newlines=True))
    else:
        await session.write_line(colored("(no public bio)", fg_color=MUTED_COLOR))


async def _handle_finger(ctx: ChatCommandContext, args: str) -> None:
    """
    `/finger <user>` (design doc §13: "accessible from the directory,
    main menu, and chat" — this is the chat entry point). Shown only
    to the requester, not broadcast — a lookup, not a channel event.
    """
    target_name = args.split(maxsplit=1)[0] if args.split() else ""
    if not target_name:
        await _show_usage(ctx.session, "finger")
        return

    target = await _resolve_target(ctx.session, ctx.db, target_name)
    if target is None:
        return

    vcard = get_vcard(ctx.db, target, requesting_user=ctx.user)
    await _write_vcard_detail(ctx.session, ctx.db, vcard)


async def _handle_me(ctx: ChatCommandContext, args: str) -> None:
    """
    `/me <action>` (design doc round 32, point 4): a typed action
    event ("* alice waves"), stored and transported as a distinct
    event kind rather than encoded as specially formatted ordinary
    text. Rendered identically for the actor and everyone else —
    unlike a regular chat message, there's no "my own words" distinction
    worth making for a shared narrative-style action.
    """
    if not args:
        await _show_usage(ctx.session, "me")
        return

    label = chat_stream_label(ctx.db, ctx.user)
    notice = colored(f"* {label} {sanitize_text(args)}", fg_color=MUTED_COLOR)

    await ctx.session.write_line(notice)
    record_message(
        ctx.db,
        ctx.channel,
        kind="action",
        author_label=ctx.user.username,
        author_fingerprint=ctx.user.fingerprint,
        body=args,
    )
    await ctx.hub.broadcast(ctx.channel.name, notice, exclude={ctx.participant_id})


async def _handle_nick(ctx: ChatCommandContext, args: str) -> None:
    """
    `/nick <name>` sets a transparent display alias (design doc round
    32, points 7-10); `/nick off` clears it; `/nick` with no argument
    shows the current one. Nickname changes are their own typed
    scrollback event, recorded for the channel this command was run
    in — announced the same way join/leave/action events are.
    """
    if not args:
        current = get_nick(ctx.db, ctx.user)
        if current:
            await ctx.session.write_line(f"Your current alias: {sanitize_text(current)}")
        else:
            await _show_usage(ctx.session, "nick")
        return

    if args.lower() == "off":
        set_nick(ctx.db, ctx.user, "")
        await _announce_nick_change(ctx, new_nick=None)
        return

    try:
        set_nick(ctx.db, ctx.user, args)
    except NickError as exc:
        await ctx.session.write_line(colored(f"Could not set alias: {exc}", fg_color=MUTED_COLOR))
        return

    await _announce_nick_change(ctx, new_nick=args)


async def _announce_nick_change(ctx: ChatCommandContext, *, new_nick: str | None) -> None:
    username = sanitize_text(ctx.user.username)
    if new_nick is not None:
        body = f"is now known as {sanitize_text(new_nick)}|{username}"
    else:
        body = "is no longer using an alias"
    notice = colored(f"*** {username} {body}", fg_color=MUTED_COLOR)

    await ctx.session.write_line(notice)
    record_message(ctx.db, ctx.channel, kind="nick", author_label=ctx.user.username, body=body)
    await ctx.hub.broadcast(ctx.channel.name, notice, exclude={ctx.participant_id})


async def _handle_away(ctx: ChatCommandContext, args: str) -> None:
    """
    `/away [message]` (design doc round 32, point 5): sets a node-wide
    away status shared across every one of the account's active
    sessions; `/away` with no argument clears it. Not written to
    channel scrollback or broadcast — away status is "visible through
    local presence views and private-message feedback" per the design
    doc, neither of which exist yet (Track 5c/5e), so for now this is
    a private confirmation to the user themselves only.
    """
    if not args:
        if ctx.presence.is_away(ctx.user.username):
            ctx.presence.clear_away(ctx.user.username)
            await ctx.session.write_line(colored("You are no longer marked away.", fg_color=MUTED_COLOR))
        else:
            await ctx.session.write_line(colored("You are not currently marked away.", fg_color=MUTED_COLOR))
        return

    ctx.presence.set_away(ctx.user.username, args)
    await ctx.session.write_line(
        colored(f"You are now marked away: {sanitize_text(args)}", fg_color=MUTED_COLOR)
    )


# -- discovery (design doc rounds 32/33, sign-off round 43) ------------------


def _lookup_user_quietly(db: Database, username: str) -> User | None:
    """Like `get_user_by_username`, but returns `None` on a miss
    instead of raising/writing a message — for internal roster
    iteration (`/names`/`/who`), where `username` came from a live
    `participant_id`, not user-typed input, so a miss would be a bug
    to shrug off silently, not something to report back to the user."""
    try:
        return get_user_by_username(db, username)
    except AuthError:
        return None


def _roster_usernames(hub: ChatHub, channel: Channel) -> list[str]:
    """Every canonical username currently present in `channel`,
    deduplicated (a user connected via two sessions appears once) and
    sorted case-insensitively. `ChatHub` only exposes opaque
    `participant_id` strings; this is the one place `chat_flow.py`'s
    own `"username:id(session)"` convention gets parsed back out for
    discovery purposes."""
    usernames = {pid.split(":", 1)[0] for pid in hub.participant_ids(channel.name)}
    return sorted(usernames, key=str.lower)


async def _handle_names(ctx: ChatCommandContext, args: str) -> None:
    """`/names` (design doc round 32/33): a compact, one-line roster
    of `ctx.channel`."""
    usernames = _roster_usernames(ctx.hub, ctx.channel)
    if not usernames:
        await ctx.session.write_line(colored("No one is here.", fg_color=MUTED_COLOR))
        return
    labels = []
    for username in usernames:
        user = _lookup_user_quietly(ctx.db, username)
        if user is not None:
            labels.append(sanitize_text(display_label(ctx.db, user)))
    await ctx.session.write_line(", ".join(labels))


async def _handle_who(ctx: ChatCommandContext, args: str) -> None:
    """`/who` (design doc round 32/33): the more detailed presence
    view of `ctx.channel` — one line per person, with an away
    indicator where applicable."""
    usernames = _roster_usernames(ctx.hub, ctx.channel)
    if not usernames:
        await ctx.session.write_line(colored("No one is here.", fg_color=MUTED_COLOR))
        return
    for username in usernames:
        user = _lookup_user_quietly(ctx.db, username)
        if user is None:
            continue
        label = sanitize_text(display_label(ctx.db, user))
        if ctx.presence.is_away(username):
            message = ctx.presence.get_away_message(username)
            suffix = f" (away: {sanitize_text(message)})" if message else " (away)"
        else:
            suffix = ""
        await ctx.session.write_line(f"{label}{suffix}")


async def _handle_list(ctx: ChatCommandContext, args: str) -> None:
    """`/list` (design doc round 32/33): every channel `ctx.user`'s
    level allows, "exposes only channels visible to the requesting
    user." Flat and sorted pinned-first-then-alphabetical, matching
    `list_boards`/`_pick_channel`'s existing sort
    precedent — a quick text reference from inside chat, not the
    interactive category-nested picker the main menu's Chat option
    already provides."""
    visible = _visible_channels_for(ctx.db, ctx.user)
    if not visible:
        await ctx.session.write_line(colored("No channels are available to you.", fg_color=MUTED_COLOR))
        return
    visible.sort(key=lambda c: (not c.pinned, c.name.lower()))
    for channel in visible:
        online = ctx.hub.participant_count(channel.name)
        description = f" - {sanitize_text(channel.description)}" if channel.description else ""
        await ctx.session.write_line(
            f"#{sanitize_text(channel.name)} ({online} online){description}"
        )


def _channel_names_for_user(
    hub: ChatHub, db: Database, requesting_user: User, target_username: str
) -> list[str]:
    """
    Every channel `target_username` currently has a live session in,
    restricted to channels `requesting_user` can themselves see
    (`_visible_channels_for` — the same filter `/list` and the picker
    use). This *is* the "hidden-channel visibility" `/whois` must
    respect (design doc round 32/33), now actually enforced against real
    hidden channels (Track 5h), not just consistently applied ahead of
    their existence the way round 43 originally left it.

    `ChatHub` has no reverse "which channels is this user in" index —
    only per-channel participant lists — so this checks every visible
    channel's roster in turn. O(channels × participants); fine at this
    project's declared scale (§14).
    """
    visible = _visible_channels_for(db, requesting_user)
    names = []
    for channel in visible:
        if any(pid.startswith(f"{target_username}:") for pid in hub.participant_ids(channel.name)):
            names.append(channel.name)
    return names


async def _handle_whois(ctx: ChatCommandContext, args: str) -> None:
    """
    `/whois <user>` (design doc round 32/33): reuses `get_vcard`
    (Track 4) for the identity/bio block (`_write_vcard_detail`,
    shared with `/finger`), then adds presence info `/finger` doesn't
    have — online/offline, away status, and which currently-visible
    channels the target is in. Works for offline/never-online
    accounts too, same as `/finger` — a directory lookup, not an
    online-only one.
    """
    target_name = args.split(maxsplit=1)[0] if args.split() else ""
    if not target_name:
        await _show_usage(ctx.session, "whois")
        return

    target = await _resolve_target(ctx.session, ctx.db, target_name)
    if target is None:
        return

    vcard = get_vcard(ctx.db, target, requesting_user=ctx.user)
    await _write_vcard_detail(ctx.session, ctx.db, vcard)

    online = ctx.presence.is_online(target.username)
    await ctx.session.write_line(f"Status: {'online' if online else 'offline'}")
    if ctx.presence.is_away(target.username):
        message = ctx.presence.get_away_message(target.username)
        await ctx.session.write_line(f"Away: {sanitize_text(message)}" if message else "Away")

    channel_names = _channel_names_for_user(ctx.hub, ctx.db, ctx.user, target.username)
    if channel_names:
        joined = ", ".join(f"#{sanitize_text(name)}" for name in channel_names)
        await ctx.session.write_line(f"Channels: {joined}")


# -- invite-only channels & membership admin (design doc §8/round 33 -------
# points 8/9/11, Phase 2 Track 5h) -------------------------------------


async def _handle_invite(ctx: ChatCommandContext, args: str) -> None:
    """
    `/invite <user>` (design doc round 33 point 11): allowed if the
    actor holds `ChannelPermission.MANAGE_MEMBERS`, **or** the channel
    has `allow_member_invites` set and the actor is already a member —
    `create_invitation` itself is the one place that authorization
    decision is made (`netbbs.chat.membership`), not duplicated here.

    Notifies the invitee through Track 5e's mailbox/live-push mechanism
    directly (`_deliver_private_message`) — no second delivery mechanism
    invented for this, and it already works for a currently-offline
    invitee (mailbox delivery doesn't require the recipient to be online
    right now, only `/msg`'s own send-time check does).
    """
    target_name = args.strip()
    if not target_name:
        await _show_usage(ctx.session, "invite")
        return

    target = await _resolve_target(ctx.session, ctx.db, target_name)
    if target is None:
        return

    try:
        create_invitation(ctx.db, ctx.channel, target, invited_by=ctx.user)
    except MembershipError:
        await ctx.session.write_line(
            colored("You do not have permission to invite users to this channel.", fg_color=MUTED_COLOR)
        )
        return

    await _deliver_private_message(
        ctx, target, f"You've been invited to #{sanitize_text(ctx.channel.name)}. Use /join to accept."
    )
    await ctx.session.write_line(
        colored(f"Invited {sanitize_text(target.username)}.", fg_color=MUTED_COLOR)
    )


async def _handle_uninvite(ctx: ChatCommandContext, args: str) -> None:
    target_name = args.strip()
    if not target_name:
        await _show_usage(ctx.session, "uninvite")
        return

    target = await _resolve_target(ctx.session, ctx.db, target_name)
    if target is None:
        return

    try:
        revoke_invitation(ctx.db, ctx.channel, target, revoked_by=ctx.user)
    except MembershipError:
        await ctx.session.write_line(
            colored("You do not have permission to do that, or there is no pending invitation.", fg_color=MUTED_COLOR)
        )
        return

    await ctx.session.write_line(
        colored(f"Invitation for {sanitize_text(target.username)} revoked.", fg_color=MUTED_COLOR)
    )


async def _handle_grantaccess(ctx: ChatCommandContext, args: str) -> None:
    """`/grantaccess <user>` (design doc round 33 point 8): directly
    adds `target` to `channel_members`, bypassing the invite-then-accept
    flow entirely — a distinct capability from `/invite`, not an
    alternate way to trigger the same thing."""
    target_name = args.strip()
    if not target_name:
        await _show_usage(ctx.session, "grantaccess")
        return

    target = await _resolve_target(ctx.session, ctx.db, target_name)
    if target is None:
        return

    try:
        add_member(ctx.db, ctx.channel, target, granted_by=ctx.user)
    except MembershipError:
        await ctx.session.write_line(
            colored("You do not have permission to manage members on this channel.", fg_color=MUTED_COLOR)
        )
        return

    await ctx.session.write_line(
        colored(f"Granted {sanitize_text(target.username)} access to this channel.", fg_color=MUTED_COLOR)
    )


async def _handle_revokeaccess(ctx: ChatCommandContext, args: str) -> None:
    """`/revokeaccess <user>` (design doc round 33 point 8): removes a
    `channel_members` grant. Deliberately doesn't force out a currently-
    live session the way `/kick` does — a distinct, narrower action
    (revoking future access, not ending a present one); a target already
    connected stays connected until they leave or reconnect."""
    target_name = args.strip()
    if not target_name:
        await _show_usage(ctx.session, "revokeaccess")
        return

    target = await _resolve_target(ctx.session, ctx.db, target_name)
    if target is None:
        return

    try:
        remove_member(ctx.db, ctx.channel, target, removed_by=ctx.user)
    except MembershipError:
        await ctx.session.write_line(
            colored("You do not have permission to manage members on this channel.", fg_color=MUTED_COLOR)
        )
        return

    await ctx.session.write_line(
        colored(f"Revoked {sanitize_text(target.username)}'s access to this channel.", fg_color=MUTED_COLOR)
    )


async def _handle_members(ctx: ChatCommandContext, args: str) -> None:
    """`/members` (design doc round 33 point 8): lists current direct
    members. Viewable by anyone already in the channel (you can only
    run this from inside it) — not further gated, reviewing your own
    channel's roster is different from administering it."""
    members = list_members(ctx.db, ctx.channel)
    if not members:
        await ctx.session.write_line(colored("No members have been granted access yet.", fg_color=MUTED_COLOR))
        return
    names = ", ".join(sanitize_text(member.username) for member in members)
    await ctx.session.write_line(f"Members: {names}")


# Design doc round 55: the single source of truth for every command's
# syntax and a one-line description -- `_show_usage` generates each
# handler's own "Usage: ..." error message from this same table
# (replacing what used to be ~16 independently-maintained copies of the
# same text), and `_handle_help` is built entirely from it. Deliberately
# excludes "?" (registered only in `_COMMANDS`, as a bare alias for
# "help") -- an alias needs no documentation entry of its own, and
# `_handle_help`'s bare listing skips any `_COMMANDS` name absent here.
_COMMAND_INFO: dict[str, tuple[str, str]] = {
    "quit": ("/quit", "Leave chat and return to the main menu."),
    "leave": ("/leave", "Leave this channel and return to the channel picker."),
    "join": ("/join <channel>", "Switch to another channel."),
    "topic": ("/topic [text]", "View the channel topic, or change it (requires edit permission)."),
    "msg": ("/msg <user> <text>", "Send a one-off private message to an online user."),
    "private": ("/private <user>", "Enter a private conversation with an online user."),
    "close": ("/close", "Leave the current private conversation."),
    "help": ("/help [command]", "List available commands, or show detail for one."),
    "me": ("/me <action>", 'Send an action message (e.g. "* alice waves").'),
    "nick": ("/nick [name|off]", "Set, view, or clear your display alias."),
    "away": ("/away [message]", "Mark yourself away, or clear away status."),
    "mute": ("/mute <user> [duration] [reason]", "Silence a user's messages in this channel."),
    "unmute": ("/unmute <user>", "Lift a mute."),
    "ban": ("/ban <user> [duration] [reason]", "Bar a user from this channel."),
    "unban": ("/unban <user>", "Lift a ban."),
    "kick": ("/kick <user> [reason]", "Force a user out of this channel right now."),
    "finger": ("/finger <user>", "Show a user's public profile."),
    "names": ("/names", "List everyone currently in this channel."),
    "who": ("/who", "List everyone in this channel, with away status."),
    "list": ("/list", "List every channel you can see."),
    "whois": ("/whois <user>", "Show a user's profile plus online/away/channel status."),
    "invite": ("/invite <user>", "Invite a user to a members-only channel."),
    "uninvite": ("/uninvite <user>", "Revoke a pending invitation."),
    "grantaccess": ("/grantaccess <user>", "Directly grant a user access to this channel."),
    "revokeaccess": ("/revokeaccess <user>", "Revoke a user's access to this channel."),
    "members": ("/members", "List users with direct access to this channel."),
}

_COMMANDS: dict[str, CommandHandler] = {
    "quit": _handle_quit,
    "leave": _handle_leave,
    "join": _handle_join,
    "topic": _handle_topic,
    "msg": _handle_msg,
    "private": _handle_private,
    "close": _handle_close,
    "help": _handle_help,
    "?": _handle_help,  # terse alias (design doc round 55) -- a genuinely
                         # distinct trigger for /help, not a second name
                         # for a command that already has one (see round
                         # 54's removal of /query for the contrast)
    "me": _handle_me,
    "nick": _handle_nick,
    "away": _handle_away,
    "mute": _handle_mute,
    "unmute": _handle_unmute,
    "ban": _handle_ban,
    "unban": _handle_unban,
    "kick": _handle_kick,
    "finger": _handle_finger,
    "names": _handle_names,
    "who": _handle_who,
    "list": _handle_list,
    "whois": _handle_whois,
    "invite": _handle_invite,
    "uninvite": _handle_uninvite,
    "grantaccess": _handle_grantaccess,
    "revokeaccess": _handle_revokeaccess,
    "members": _handle_members,
}


# -- Tab completion (design doc round 49/Track 5g) --------------------------

# Per-command visibility predicate for completion *suggestions* only --
# deliberately a separate dict rather than widening _COMMANDS' own value
# type, so dispatch (_dispatch_command) and /help's listing need no
# changes at all. A command absent from this dict is always suggested.
# This is purely a suggestion filter, not an authorization check: the
# handlers themselves (mute_user/kick_user/etc., via ChatModerationError/
# MembershipError) remain the sole source of truth for what's actually
# allowed to run -- unchanged by this track.
def _requires_moderate(db: Database, channel: Channel, user: User) -> bool:
    return has_permission(
        db, user, object_type="channel", object_id=channel.id, permission=ChannelPermission.MODERATE
    )


def _requires_manage_members(db: Database, channel: Channel, user: User) -> bool:
    return has_permission(
        db, user, object_type="channel", object_id=channel.id, permission=ChannelPermission.MANAGE_MEMBERS
    )


def _can_invite(db: Database, channel: Channel, user: User) -> bool:
    """`/invite`'s own visibility predicate is more permissive than the
    other three membership-admin commands, matching `create_invitation`'s
    real authorization exactly (design doc round 33 point 11's opt-in) --
    unlike them, it's not gated on MANAGE_MEMBERS alone."""
    if _requires_manage_members(db, channel, user):
        return True
    return channel.allow_member_invites and is_member(db, channel, user)


_COMMAND_VISIBILITY: dict[str, Callable[[Database, Channel, User], bool]] = {
    "mute": _requires_moderate,
    "unmute": _requires_moderate,
    "ban": _requires_moderate,
    "unban": _requires_moderate,
    "kick": _requires_moderate,
    "invite": _can_invite,
    "uninvite": _requires_manage_members,
    "grantaccess": _requires_manage_members,
    "revokeaccess": _requires_manage_members,
}

# The commands whose first argument is a username -- distinguished by
# whether it must currently be *online* (/msg, /private -- a live
# conversation can't reach an offline account, matching those commands'
# own online_usernames() and the online refusal on send) versus any
# registered account at all (/whois, /finger -- both work for offline
# accounts too). /invite is its own case just below -- eligible
# candidates are registered users who aren't already members.
_ONLINE_USER_COMMAND_PREFIXES = ("/msg ", "/private ")
_ANY_USER_COMMAND_PREFIXES = ("/whois ", "/finger ")
_INVITE_COMMAND_PREFIX = "/invite "


def _build_completer(db: Database, presence: PresenceRegistry, channel: Channel, user: User) -> Completer:
    """
    Builds one Tab-completion closure per `read_line()` call in
    `send_loop`, from the state available there -- cheap (a handful of
    string comparisons plus, at most, one permission lookup), and always
    reflects the actor's *current* permissions rather than a snapshot
    taken once at channel entry (moderator grants can change
    mid-session, unlike a static completer built once and reused).

    All matching is case-insensitive (design doc round 33 point 6).
    """

    def completer(text: str) -> list[str]:
        if text.startswith("/") and " " not in text:
            prefix = text[1:].lower()
            return sorted(
                f"/{name}"
                for name in _COMMANDS
                if name.lower().startswith(prefix)
                and (_COMMAND_VISIBILITY.get(name) is None or _COMMAND_VISIBILITY[name](db, channel, user))
            )

        for command_prefix in _ONLINE_USER_COMMAND_PREFIXES:
            rest = text[len(command_prefix) :]
            if text.lower().startswith(command_prefix) and " " not in rest:
                word = rest.lower()
                return sorted(
                    name for name in presence.online_usernames() if name.lower().startswith(word)
                )

        for command_prefix in _ANY_USER_COMMAND_PREFIXES:
            rest = text[len(command_prefix) :]
            if text.lower().startswith(command_prefix) and " " not in rest:
                word = rest.lower()
                return sorted(
                    candidate.username for candidate in list_users(db) if candidate.username.lower().startswith(word)
                )

        rest = text[len(_INVITE_COMMAND_PREFIX) :]
        if text.lower().startswith(_INVITE_COMMAND_PREFIX) and " " not in rest:
            word = rest.lower()
            return sorted(
                candidate.username
                for candidate in list_users(db)
                if candidate.username.lower().startswith(word) and not is_member(db, channel, candidate)
            )

        return []

    return completer


async def _chat_loop(
    session: Session,
    db: Database,
    hub: ChatHub,
    presence: PresenceRegistry,
    mailbox: MessageMailbox,
    history: InputHistory,
    channel: Channel,
    user: User,
) -> ChatAction:
    """
    Real-time chat within `channel`, until the user types /quit, /leave,
    or /join — returns a `ChatAction` telling `browse_channels` what to
    do next (exit to the main menu, return to the channel picker, or
    jump straight into another channel) rather than just ending. A kick/
    ban or a dropped connection (`receive_task` finishing instead of
    `send_task`) always resolves to `_Quit()`.

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
    deferred; it isn't anymore. Cursor-addressable editing (Left/Right/
    Home/End/Delete/Insert) and Up/Down command-history recall (`history`
    — design doc round 47/Track 5f) landed later still, once retyping a
    long `/mute`/`/ban` reason from scratch each time turned out to be
    genuinely painful in practice. An incoming message can still land
    mid-typing and interleave visually with a user's own in-progress
    line, same as classic line-mode chat tools (Unix `talk`, `wall`)
    always had — that's a receive-vs-send-task race (see below), a
    different problem from line editing, and still out of scope.

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
        return _Quit()

    participant_id = f"{user.username}:{id(session)}"
    queue = hub.join(channel.name, participant_id)

    channel_label = colored(f"#{sanitize_text(channel.name)}", fg_color=ACCENT_COLOR, bold=True)
    quit_hint = menu_key("/quit", " to leave")

    scrollback = get_scrollback(db, channel)
    if scrollback:
        await session.write_line(colored("--- scrollback ---", fg_color=MUTED_COLOR))
        for message in scrollback:
            await session.write_line(_render_scrollback_message(db, message))
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
    # author_label is stored raw here (user.username, not a sanitized/
    # alias-aware label) -- sanitize on output, not on storage, per
    # sanitize_text's docstring; only the broadcast text below is
    # actually rendered to a terminal. chat_stream_label (design doc
    # round 53) looked up fresh here (not cached) since it can change
    # mid-session via /nick.
    record_message(
        db, channel, kind="join", author_label=user.username, author_fingerprint=user.fingerprint
    )
    await hub.broadcast(
        channel.name,
        colored(f"*** {chat_stream_label(db, user)} has joined the channel.", fg_color=MUTED_COLOR),
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

    async def send_loop() -> ChatAction | None:
        # Per-session private-conversation state (design doc round 33
        # point 1, sign-off round 46/Track 5e): set by `/private`
        # (`_EnterPrivate`), cleared by `/close` (`_ExitPrivate`). A
        # plain local, not anything shared/global -- only this session's
        # own next lines of ordinary input are affected. While set,
        # slash-commands still dispatch exactly as normal (confirmed
        # with Thiesi, matching round 39's existing "leading / is always
        # a command attempt" rule) -- only *non-slash* lines change
        # meaning, routed to the private conversation instead of posted
        # to the channel.
        private_target: User | None = None

        while True:
            completer = _build_completer(db, presence, channel, user)
            line = (await session.read_line(history=history, completer=completer)).strip()
            if not line:
                continue
            if line.startswith("/"):
                ctx = ChatCommandContext(
                    session=session,
                    db=db,
                    hub=hub,
                    presence=presence,
                    mailbox=mailbox,
                    channel=channel,
                    user=user,
                    participant_id=participant_id,
                )
                action = await _dispatch_command(ctx, line)
                if isinstance(action, _EnterPrivate):
                    private_target = action.target
                    continue
                if isinstance(action, _ExitPrivate):
                    if private_target is None:
                        await session.write_line(
                            colored("You are not in a private conversation.", fg_color=MUTED_COLOR)
                        )
                    else:
                        private_target = None
                        await session.write_line(
                            colored(
                                f"Returned to #{sanitize_text(channel.name)}.", fg_color=MUTED_COLOR
                            )
                        )
                    continue
                if action is not None:
                    return action
                continue

            if private_target is not None:
                ctx = ChatCommandContext(
                    session=session,
                    db=db,
                    hub=hub,
                    presence=presence,
                    mailbox=mailbox,
                    channel=channel,
                    user=user,
                    participant_id=participant_id,
                )
                if not presence.is_online(private_target.username):
                    await session.write_line(
                        colored(
                            f"{sanitize_text(private_target.username)} is no longer online.",
                            fg_color=MUTED_COLOR,
                        )
                    )
                    private_target = None
                    continue
                await _deliver_private_message(ctx, private_target, line)
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
            # Looked up fresh on every message, not cached from join
            # time -- an alias set via /nick mid-session must show up
            # immediately, not just after the next rejoin. Nick-only-
            # plus-marker, not both forms (design doc round 53) --
            # chat_stream_label already sanitizes internally, unlike
            # display_label, so no separate sanitize_text wrap here.
            current_label = chat_stream_label(db, user)
            self_label = colored(f"<{current_label}>", fg_color=SELF_COLOR, bold=True)
            others_label = colored(f"<{current_label}>", fg_color=ACCENT_COLOR)
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
            # Design doc round 32, point 6: sending a message does not
            # clear away state -- a user may intentionally remain away
            # while briefly responding. Reminded, not silently changed.
            if presence.is_away(user.username):
                await session.write_line(
                    colored("(You are still marked away.)", fg_color=MUTED_COLOR)
                )

    receive_task = asyncio.create_task(receive_loop())
    send_task = asyncio.create_task(send_loop())

    try:
        try:
            done, pending = await asyncio.wait(
                {receive_task, send_task}, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            # This whole session task was itself cancelled from outside
            # (e.g. deliberate node shutdown, design doc round 51's
            # ActiveSessionRegistry.disconnect_all()) -- asyncio.wait()
            # being cancelled does NOT cancel the tasks it was waiting
            # on, so without this, receive_task/send_task would be left
            # orphaned: still scheduled, with nothing left to await
            # their result. One of them then hits SessionClosedError
            # the moment the underlying socket actually closes, and
            # asyncio logs "Task exception was never retrieved" since
            # there's no one left to retrieve it (seen for real on
            # Thiesi's NetBSD box on Ctrl-C with a chat session open,
            # not just reasoned about).
            for task in (receive_task, send_task):
                task.cancel()
            await asyncio.gather(receive_task, send_task, return_exceptions=True)
            raise
        for task in pending:
            task.cancel()
        # Properly await cancelled tasks rather than fire-and-forget —
        # otherwise asyncio can warn "Task was destroyed but it is
        # pending" and the cancellation may not actually finish cleanly
        # before this function returns.
        await asyncio.gather(*pending, return_exceptions=True)
        outcome: ChatAction | None = None
        for task in done:
            value = task.result()  # re-raise, e.g. SessionClosedError from a dropped connection
            if task is send_task:
                outcome = value
        # receive_task finishing (a kick/ban) has no ChatAction of its
        # own -- it always means "exit entirely," same as /quit.
        return outcome or _Quit()
    finally:
        hub.leave(channel.name, participant_id)
        record_message(
            db, channel, kind="leave", author_label=user.username, author_fingerprint=user.fingerprint
        )
        await hub.broadcast(
            channel.name,
            colored(f"*** {chat_stream_label(db, user)} has left the channel.", fg_color=MUTED_COLOR),
            exclude={participant_id},
        )
