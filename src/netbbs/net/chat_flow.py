"""
Chat channel browsing and the real-time chat loop.

Kept in its own module rather than growing login_flow.py indefinitely —
matches the project's modular-package approach (design doc §3).

Migrated onto the two-lane database execution model (design doc,
issue #57), following `netbbs.net.
mail_flow`/`netbbs.net.file_flow`'s established recipe: every function
reachable from `browse_channels` takes `lane: DatabaseLane` instead of
`db: Database`. Unlike those two, chat has no functions left
deliberately unmigrated — confirmed with Thiesi: the pinned
status line (`_render_chat_status_line`, repainted after nearly every
message) and the tab-completer (`_build_completer`, rebuilt fresh on
every `read_line()` call) are both hot, read-only, cosmetic paths that
still move fully onto the lane, same as everything else, rather than
staying on a lingering `db` the way `file_flow.has_visible_areas` did —
real added per-message overhead, accepted as consistent with the
"defer benchmarking to #59's harness" stance rather than guessed at
now.

Every module-level helper function that takes `db: Database` as its own
first parameter (`_visible_channels_for`, `_authorize_channel_entry`,
`_meets_live_participation_requirements`, `_chat_author_label`,
`_resolve_message_author`, `_message_author_label`,
`_render_channel_message`, `_render_scrollback_message`,
`_own_channel_privileges`, `_render_chat_status_line`,
`_requires_moderate`, `_requires_manage_members`, `_can_invite`,
`_lookup_user_quietly`, `_channel_names_for_user`,
`_find_live_participants`, `_check_mute`, `_check_ban`) stays exactly
that shape, unchanged — these are leaf/callee functions dispatched
*through* the lane (`lane.run(func, ...)`), never callers of it
themselves, the same "callee, not caller" distinction
`netbbs.net.file_flow._uploader_display_name`'s own docstring already
established. `_channel_names_for_user`/`_find_live_participants` were
reordered to be `db`-first (`hub` used to come first) specifically so
`lane.run`'s db-injection convention applies to them without a wrapper
closure — both are private, single-call-site functions, safe to
reorder.

Three call-site shapes needed real restructuring, not just a
db-to-lane rename, because they mix a synchronous DB read with
something that can't run inside a lane job (async session I/O, or a
synchronous callback contract this module doesn't control):

- `_build_completer` (a `pick_item`-callback-shaped problem, one level
  removed from `netbbs.net.picker`'s own callbacks): its `completer(text)` closure is itself a plain
  synchronous callable (`netbbs.net.char_input.Completer`'s own
  contract), invoked possibly several times per `read_line()` call as
  the user presses Tab — same fix as the picker case, eager pre-fetch
  of everything it might need (visible commands, the user list, current
  membership) in one lane call *before* the closure is built, not a
  live DB read inside it.
- `_resolve_target`/`_write_vcard_detail`: previously took `db` and
  mixed one synchronous lookup/read with `await session.write_line(...)`
  in the same function body — split so the DB read goes through
  `lane.run` and the write stays a plain `await` in the caller's own
  coroutine, never inside a lane job (a lane job can't itself `await`
  anything).
- `_handle_names`/`_handle_who`/`_handle_help`: each had a per-item
  loop making one live DB call per roster entry/command name — bundled
  into one small inner function dispatched through a single `lane.run`
  call, the same "one round trip, not N" shape
  `netbbs.net.file_flow._show_area`'s own bundled `_load` helper
  established.
"""

from __future__ import annotations

import asyncio
import datetime
from dataclasses import dataclass
from typing import Awaitable, Callable, Sequence

from netbbs.activity import record_channel_seen
from netbbs.attestation import (
    format_verified_name_unit,
    get_display_name,
    meets_age,
    meets_name_requirement,
)
from netbbs.auth.users import (
    SYSOP_LEVEL,
    AuthError,
    User,
    account_still_active,
    get_user_by_id,
    get_user_by_username,
    list_users,
)
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
    ParticipantId,
    PresenceRegistry,
    QueueOverflowNotice,
    TopicError,
    accept_invitation,
    add_member,
    ban_user,
    chat_stream_label,
    create_invitation,
    display_label,
    format_with_preference,
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
    set_timestamps_enabled,
    set_topic,
    timestamps_enabled,
    unban_user,
    unmute_user,
)
from netbbs.chat.categories import Category, list_subcategories, list_top_level_categories
from netbbs.communities import get_effective_min_age, get_effective_name_requirement
from netbbs.directory import VCard, get_vcard
from netbbs.moderation import ChannelPermission, has_permission
from netbbs.net.char_input import Completer, InputHistory, LiveInputBuffer
from netbbs.net.char_input import move_cursor as relative_move_cursor
from netbbs.net.picker import pick_item
from netbbs.net.session import Session, SessionClosedError
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.permissions import meets_level
from netbbs.rendering import (
    ACCENT_COLOR,
    CHANNEL_TYPE_COLOR,
    HEADER_COLOR,
    MUTED_COLOR,
    NICK_COLOR,
    PRIVILEGE_COLOR,
    SELF_COLOR,
    STATUS_BAR_BACKGROUND,
    TOPIC_COLOR,
    clear_line,
    clear_screen,
    colored,
    menu_key,
    move_cursor,
    reset_scroll_region,
    restore_cursor,
    sanitize_text,
    save_cursor,
    set_scroll_region,
    truncate,
)
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display, resolve_display_preferences, utc_now_iso


async def browse_channels(
    session: Session,
    lane: DatabaseLane,
    hub: ChatHub,
    presence: PresenceRegistry,
    mailbox: MessageMailbox,
    history: InputHistory,
    user: User,
    *,
    session_registry: ActiveSessionRegistry | None = None,
    community_id: int | None = None,
    community_scoped: bool = False,
    title_prefix: str | None = None,
    initial_channel: Channel | None = None,
) -> None:
    """
    Entry point: browse from the top level, then run the chat loop for
    whatever's picked.

    `initial_channel` (issue #56's `[N]ew scan`), if given, skips the
    first picker entirely and enters `_chat_loop` with that channel
    directly -- the same shape `/join`'s `_SwitchTo` already re-enters
    this loop with, just supplied by the caller instead of a chat
    command. Every later `/leave`/picker re-entry in this call still
    goes through the ordinary picker, unaffected.

    `session_registry` (GitHub issue #27), if given, is what
    `_deliver_private_message` uses to enumerate *every* live session
    belonging to a `/msg` recipient rather than just one -- passed
    straight through to `_chat_loop`. `None` (the default) degrades to
    the old single-target-ish behavior for any caller that bypasses
    `netbbs.net.login_flow.handle_session`'s real node-wide registry
    entirely (mainly tests not exercising this specific feature); every
    real connection always has one.

    A small outer loop, not a single call (design doc §8): `/leave`
    returns here to pick again,
    `/join` jumps straight back into `_chat_loop` with an already-
    validated channel without going through the picker at all, and
    `/quit` (or a kick/dropped connection) exits out to the caller (the
    main menu). Always re-enters the picker at the *top* level on
    `/leave`, never wherever a category-nested pick left off — the same
    "back always lands somewhere consistent" reasoning `/quit` already
    followed, not a new decision.

    `history` (design doc) is the one connection's
    `InputHistory`, constructed once in `netbbs.net.login_flow.
    handle_session` — passed straight through to every `_chat_loop`
    call here so command recall persists across a `/join` channel
    switch rather than resetting.

    Every `channel` this loop is about to enter `_chat_loop` with is
    re-checked through `_authorize_channel_entry` first (GitHub issue
    #28, reopened a third time) -- covers all three ways `channel` can
    change here (the initial pick, a picker repick via `_ToPicker`, and
    a `/join`-driven `_SwitchTo`) with one check at the top of the loop,
    rather than needing it duplicated at each. Redundant, not wrong,
    for the `_SwitchTo` case specifically: `_handle_join` already ran
    the identical check (and already consumed any invitation) before
    ever returning that action, so this just re-confirms "yes, still a
    member" for free.

    `community_id`/`community_scoped`/`title_prefix` (design doc §16)
    narrow every picker re-entry in this loop -- not just the
    initial pick -- to the same Community/Uncategorized/unfiltered
    scope the caller entered with; see
    `netbbs.net.login_flow._browse_boards_in_category`'s docstring for
    the full reasoning, identical here.
    """
    channel = initial_channel or await _pick_channel(
        session, lane, hub, user, category_id=None,
        community_id=community_id, community_scoped=community_scoped, title_prefix=title_prefix,
    )
    while channel is not None:
        allowed, denial_message = await lane.run(_authorize_channel_entry, channel, user)
        if not allowed:
            await session.write_line(colored(denial_message, fg_color=MUTED_COLOR))
            channel = await _pick_channel(
                session, lane, hub, user, category_id=None,
                community_id=community_id, community_scoped=community_scoped, title_prefix=title_prefix,
            )
            continue

        action = await _chat_loop(
            session, lane, hub, presence, mailbox, history, channel, user, session_registry=session_registry
        )
        if isinstance(action, _SwitchTo):
            channel = action.channel
            continue
        if isinstance(action, _ToPicker):
            channel = await _pick_channel(
                session, lane, hub, user, category_id=None,
                community_id=community_id, community_scoped=community_scoped, title_prefix=title_prefix,
            )
            continue
        return  # _Quit


def _visible_channels_for(db: Database, user: User) -> list[Channel]:
    """
    Every channel `user` is allowed to *see* — the shared filter behind
    the picker, `/list`, and `/whois`'s channel-membership display
    (design doc). Consolidates what
    were three separate, slightly-duplicated `meets_level`-only list
    comprehensions into one place, adding the hidden-channel
    condition: level still gates access exactly as before, and a
    `hidden` channel is additionally excluded unless the user is
    already a member, holds a pending invitation, or holds *any*
    moderator grant on it (checked via `has_permission` with every
    `ChannelPermission` bit combined — "does the user hold any of
    these," not one specific bit).

    A `members_only`-but-not-`hidden` channel still appears here —
    "hidden + open is obscurity, not access control": only `hidden`
    controls listing visibility itself; `members_only`
    alone just means you can see it exists but can't actually enter it
    without access, enforced separately by `_authorize_channel_entry`
    (GitHub issue #28, reopened a third time -- both `/join` and
    picking it directly from this list go through that one check now).
    """
    visible = []
    for channel in list_channels(db):
        if not meets_level(user, channel.min_level) or not meets_age(db, user, get_effective_min_age(db, channel)):
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


def has_visible_channels(
    db: Database, user: User, *, community_id: int | None = None, community_scoped: bool = False
) -> bool:
    """Whether `user` can see at least one channel under the given
    Community filter -- public (unlike `_visible_channels_for`)
    specifically so `netbbs.net.login_flow`'s shared resource-type
    sub-menu can use it for the same "only offer what currently
    applies" conditional visibility `_has_visible_boards` provides for
    boards (design doc §16). Deliberately still `db`-based --
    a menu-gating check called from still-unmigrated `login_flow.py`
    code, same category as `netbbs.net.file_flow.has_visible_areas`."""
    channels = _visible_channels_for(db, user)
    if community_scoped:
        channels = [c for c in channels if c.community_id == community_id]
    return bool(channels)


def list_visible_channels_for(db: Database, user: User) -> list[Channel]:
    """Every channel `user` can currently see, unscoped by Community --
    public (unlike `_visible_channels_for`) so issue #56's `[N]ew scan`
    screen (`netbbs.net.login_flow`) can reuse this module's own
    hidden/members_only/permission visibility logic instead of
    duplicating it, the same reasoning `has_visible_channels` already
    applies for its own boolean-only callers."""
    return _visible_channels_for(db, user)


async def _pick_channel(
    session: Session,
    lane: DatabaseLane,
    hub: ChatHub,
    user: User,
    *,
    category_id: int | None,
    community_id: int | None = None,
    community_scoped: bool = False,
    title_prefix: str | None = None,
) -> Channel | None:
    """
    Browse channels within a category (or the top level) and return
    whichever one the user picks, or `None` if they back out — mirrors
    `netbbs.net.login_flow._browse_boards_in_category` exactly — same
    reasoning, same two-level cap, same category/item ID-namespace
    disambiguation trick (negated category IDs), and the same
    `community_id`/`community_scoped`/`title_prefix` Community-filter
    threading (design doc §16). See that function's docstring
    for the full rationale; not repeated here to avoid the two copies
    drifting out of sync in what they claim rather than just in what
    they say.

    The channel/category list-building is one bundled
    `lane.run` call, not several -- `_channel_description` (the picker's
    own `description_of` callback) needs no DB access at all (`hub.
    participant_count` plus the channel's own already-fetched
    `description` field), so, like `netbbs.net.file_flow`'s own category
    picker, nothing about the picker callback itself needed
    eager-pre-fetch restructuring -- only the list construction moved.
    """

    def _load(db: Database) -> tuple[list[Channel], list[Category]]:
        all_channels = _visible_channels_for(db, user)
        if community_scoped:
            all_channels = [c for c in all_channels if c.community_id == community_id]
        # Activity-sort applied before splitting by category, so ordering
        # within each category's channel list is still most-recent-first —
        # same node-wide default as boards (design doc).
        all_channels.sort(key=lambda c: hub.last_activity(c.name) or c.created_at, reverse=True)
        all_channels.sort(key=lambda c: not c.pinned)
        channels_here = [c for c in all_channels if c.category_id == category_id]

        categories_here = (
            list_top_level_categories(db) if category_id is None else list_subcategories(db, category_id)
        )
        if community_scoped:
            used_category_ids = {c.category_id for c in all_channels if c.category_id is not None}
            if category_id is None:
                categories_here = [
                    c for c in categories_here
                    if c.id in used_category_ids
                    or any(sub.id in used_category_ids for sub in list_subcategories(db, c.id))
                ]
            else:
                categories_here = [c for c in categories_here if c.id in used_category_ids]
        return channels_here, categories_here

    channels_here, categories_here = await lane.run(_load)

    title = f"{title_prefix} — chat channels" if title_prefix is not None else "Available channels"

    if not categories_here:
        return await pick_item(
            session,
            channels_here,
            name_of=lambda c: c.name,
            stable_id_of=lambda c: c.id,
            description_of=lambda c: _channel_description(hub, c),
            title=title,
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
        title=title,
        empty_message="No chat channels are available to you yet.",
    )
    if selected is None:
        return None

    if isinstance(selected, Category):
        return await _pick_channel(
            session, lane, hub, user, category_id=selected.id,
            community_id=community_id, community_scoped=community_scoped, title_prefix=title_prefix,
        )
    return selected


def _authorize_channel_entry(db: Database, channel: Channel, user: User) -> tuple[bool, str | None]:
    """
    The one authoritative check for whether `user` may actually enter
    `channel` right now, and the single place that performs it (GitHub
    issue #28, reopened a third time).

    Before this, membership/invitation enforcement existed *only*
    inside `_handle_join` (`/join`'s command handler) -- but `/join`
    was never the only way into `_chat_loop`. `browse_channels`' own
    picker returns any *visible* channel (`_visible_channels_for`
    deliberately still lists a non-hidden `members_only` channel, and a
    `hidden` one the user merely holds a pending invitation for) and
    handed it straight to `_chat_loop`, which only checks bans. Picking
    such a channel from the browse list -- not typing `/join` -- used
    to grant entry with no membership/invitation check at all, and a
    hidden channel's invitation was never actually consumed that way
    either, leaving it perpetually pending while the invitee kept
    re-entering through the picker.

    Returns `(True, None)` if entry is allowed (open channel, existing
    member, or a valid pending invitation -- atomically accepted here,
    creating real persistent membership via `accept_invitation`, exactly
    as `/join` already did), or `(False, message)` otherwise. `message`
    distinguishes "never had any claim on this channel" from "had a
    pending invitation, but it stopped being valid" (expired, revoked,
    or lost a race to a concurrent revoke -- see `accept_invitation`'s
    own docstring) purely for clearer feedback; both are the same
    "not authorized" outcome as far as entry itself is concerned.

    Deliberately synchronous -- every check here (`meets_level`,
    `is_member`, `has_pending_invitation`, `accept_invitation`) already
    is, and there's no `await` point for a race to open up between them
    within a single call. Dispatched as one `lane.run` call by every
    caller -- the whole point of staying synchronous here.

    `meets_age`/`meets_name_requirement` (design doc §18) are
    both checked here, not split across a separate read/write boundary
    the way `netbbs.boards.boards.Board`'s gates are -- chat has no
    meaningful read/write split (this module's own docstring, and
    `netbbs.chat.channels.Channel`'s single `min_level` already reflect
    that), so entry itself is the one point where both age content-
    restriction and name-verification participation-requirement apply.
    """
    if not meets_level(user, channel.min_level) or not meets_age(db, user, get_effective_min_age(db, channel)):
        return False, "You are not authorized to enter that channel."
    if not meets_name_requirement(db, user, get_effective_name_requirement(db, channel)):
        return False, "This channel requires a verified real name to participate."
    if not channel.members_only:
        return True, None
    if is_member(db, channel, user):
        return True, None

    had_invitation = has_pending_invitation(db, channel, user)
    try:
        accepted = accept_invitation(db, channel, user)
    except MembershipError:
        accepted = False
    if accepted:
        return True, None
    if had_invitation:
        return False, "That invitation is no longer valid."
    return False, "You are not authorized to enter that channel."


_NO_LONGER_QUALIFIES_MESSAGE = (
    "You no longer meet this channel's participation requirements. Returning to the channel picker."
)


def _meets_live_participation_requirements(db: Database, channel: Channel, user: User) -> bool:
    """
    Re-checks the age/name-verification gates a long-lived chat
    session's `channel` snapshot can drift out of sync with (GitHub
    issue #64 point 5) — `_authorize_channel_entry` only
    ever runs once, at join time, but a channel's (or its Community's)
    `min_age`/`name_requirement` can change, or a name/age attestation
    can be revoked/replaced (design doc §18), while a session sits in
    `_chat_loop` for arbitrarily long. Called by `send_loop`'s
    ordinary-message branch and `_handle_me`, immediately before either
    accepts a send — the two paths whose broadcast now carries
    verified-name styling (`_chat_author_label`).

    Re-fetches `channel` fresh via `get_channel_by_name` rather than
    trusting the frozen snapshot passed in — acting on a stale snapshot
    here specifically risks letting a since-disqualified speaker keep
    posting under styling that claims a verification they no longer
    hold, not just showing a stale topic/description.

    Only re-checks name/age — level, ban, and members-only access
    already have their own live enforcement (kick/ban immediately
    evicts a live session; nothing in this codebase revokes level or
    membership out from under one). Name/age verification is the one
    gate that can silently stop being satisfied with no corresponding
    live eviction, since an attestation is a mutable row a verifier can
    revoke or replace at any time.
    """
    current = get_channel_by_name(db, channel.name)
    return meets_age(db, user, get_effective_min_age(db, current)) and meets_name_requirement(
        db, user, get_effective_name_requirement(db, current)
    )


def _channel_description(hub: ChatHub, channel: Channel) -> str:
    online = hub.participant_count(channel.name)
    base = channel.description or ""
    return f"{base} ({online} online)".strip()


def _chat_author_label(db: Database, channel: Channel, user: User) -> str:
    """
    `chat_stream_label(db, user)` — the alias-or-username presentation
    form, design doc, deliberately untouched by this function —
    with `channel`'s colored verified-real-name unit appended when its
    *effective* `name_requirement` (`netbbs.communities.
    get_effective_name_requirement`, Community-inheritance-aware) is
    `verified_and_displayed`: the chat-specific composition of the
    anti-forgery display for the live message stream (GitHub issue #64).

    Deliberately its own function here in the net layer, not folded
    into `chat_stream_label` itself or into `netbbs.attestation.
    format_name_for_resource`: `chat_stream_label`'s one job is the
    presentation alias, plain and channel-policy-ignorant —
    making it resource-aware would give the nick module a dependency on
    chat/Communities policy it has no other reason to carry.
    `format_name_for_resource` doesn't transfer directly either, since
    chat's primary name may be a nick, not `display_name` — this reuses
    that function's own extracted primitive, `format_verified_name_unit`,
    rather than either duplicating the coloring or trying
    to parse a fully-composed string back apart.

    Composition confirmed with Thiesi (issue #64): `~nick~ (=Real
    Name=)` when a nick is set, `display-name-or-username (=Real
    Name=)` otherwise — deliberately not a three-name `~nick~
    display-name (=Real Name=)` form, which would put three
    simultaneous names on every line of live chat and reverse the
    plain alias's deliberate clutter reduction; `/whois` still supplies
    canonical/display identity on demand.
    """
    ordinary = chat_stream_label(db, user)
    verified_unit = format_verified_name_unit(
        db, user, name_requirement=get_effective_name_requirement(db, channel)
    )
    if verified_unit is None:
        return ordinary
    if get_nick(db, user) is not None:
        return f"{ordinary} {verified_unit}"
    primary = sanitize_text(get_display_name(db, user) or user.username)
    return f"{primary} {verified_unit}"


def _resolve_message_author(db: Database, author_label: str) -> User | None:
    """Best-effort live-account lookup behind a stored `ChannelMessage.
    author_label` (a denormalized username, design doc) —
    `None` if the account can no longer be found (defensive; no
    account-deletion feature exists yet to actually trigger this)."""
    try:
        return get_user_by_username(db, author_label)
    except AuthError:
        return None


def _message_author_label(db: Database, channel: Channel, message: ChannelMessage) -> str:
    """
    The author label to show for one `ChannelMessage` of kind
    `"message"`/`"action"`/`"join"`/`"leave"` — resolves the *current*
    live account behind `message.author_label` and renders it through
    `_chat_author_label` (current nick/alias, current verified-name
    unit if `channel` currently calls for one) when possible, exactly
    the same whether this is a live event or scrollback replay (GitHub
    issue #64) — there's no per-message alias/attestation
    snapshot, matching the existing "replay shows current presentation"
    behavior `chat_stream_label`'s own docstring already establishes.

    Falls back to the sanitized *stored* label, with no `VERIFIED_COLOR`
    styling at all, if the account can no longer be resolved — a stored
    string, even one that once came from a verified user, must never
    itself be treated as proof.
    """
    author = _resolve_message_author(db, message.author_label)
    if author is None:
        return sanitize_text(message.author_label)
    return _chat_author_label(db, channel, author)


def _colored_around(prefix: str, middle: str, suffix: str, *, fg_color: int, bold: bool = False) -> str:
    """
    Compose `prefix + middle + suffix`, all in `fg_color` — the fix for
    a nesting bug (GitHub issue #64 point 4):
    `middle` (almost always `_message_author_label`'s output) may
    already carry its own embedded `NICK_COLOR`/`VERIFIED_COLOR` segment
    with its own trailing reset. Interpolating it into one larger string
    and passing the whole thing through a single outer `colored()` call
    would let that inner reset clear the outer color early, leaving
    `suffix` rendered in the terminal's default color instead of
    `fg_color`.

    When `middle` carries no embedded ANSI of its own — the common case,
    no nick and no verified-name unit — this returns exactly what a
    single `colored(f"{prefix}{middle}{suffix}", ...)` call would
    (checked directly, not just assumed as an optimization): several
    existing tests assert literal, uninterrupted substrings like
    `"* alice waves"`, true only when no escape codes are injected
    mid-string, and there is no reason to fragment output that doesn't
    need isolating in the first place. Only when `middle` does carry its
    own color+reset are the three pieces wrapped independently instead,
    each a fully self-contained open-content-reset unit immune to
    whatever came before or after it.
    """
    if "\x1b" not in middle:
        return colored(f"{prefix}{middle}{suffix}", fg_color=fg_color, bold=bold)
    return (
        colored(prefix, fg_color=fg_color, bold=bold)
        + colored(middle, fg_color=fg_color, bold=bold)
        + colored(suffix, fg_color=fg_color, bold=bold)
    )


def _render_channel_message(
    db: Database, channel: Channel, viewer: User, message: ChannelMessage, *, self_message: bool = False
) -> str:
    """
    Render one `"message"`/`"action"`/`"join"`/`"leave"` `ChannelMessage`
    for `viewer` — the single renderer shared by the live broadcast path
    (`send_loop`/`receive_loop`, `_handle_me`, channel join/leave) and
    scrollback replay (`_render_scrollback_message`), so the two paths
    can never disagree about whether a currently-verified real name is
    shown (GitHub issue #64). Previously each path rendered
    independently — a live-only fix would have left replay behind, and
    a rule fixed in one path but not the other could have either shown
    a visible inconsistency or reintroduced the historical-instability
    bug this was written to avoid.

    `self_message` selects `SELF_COLOR` over `ACCENT_COLOR` for a
    `"message"` kind's delimiter — the sender's own live-typing
    affordance (design doc -- per-user chat timestamp preference);
    meaningless for `"action"`/`"join"`/`"leave"` (never self-colored,
    matching existing behavior) and always `False` for scrollback replay
    — a past message read back isn't "what I just sent," possibly from a
    different session than whichever one originally sent it.

    See `_colored_around` for how each line safely incorporates
    `_message_author_label`'s output, which may or may not already
    carry its own embedded color.
    """
    author_label = _message_author_label(db, channel, message)
    if message.kind == "join":
        line = _colored_around("*** ", author_label, " has joined the channel.", fg_color=MUTED_COLOR)
    elif message.kind == "leave":
        line = _colored_around("*** ", author_label, " has left the channel.", fg_color=MUTED_COLOR)
    elif message.kind == "action":
        line = _colored_around(
            "* ", author_label, f" {sanitize_text(message.body)}", fg_color=MUTED_COLOR
        )
    else:  # "message"
        color = SELF_COLOR if self_message else ACCENT_COLOR
        label = _colored_around("<", author_label, ">", fg_color=color, bold=self_message)
        line = f"{label} {sanitize_text(message.body)}"
    return format_with_preference(db, viewer, line, message.created_at)


def _render_scrollback_message(db: Database, channel: Channel, user: User, message: ChannelMessage) -> str:
    """
    Render a persisted `ChannelMessage` for replay on join, matching the
    live formatting `_chat_loop` itself uses for the same kind of event —
    a replay should look exactly like the original moment did, just
    delayed.

    `"message"`/`"action"`/`"join"`/`"leave"` kinds delegate entirely to
    `_render_channel_message` (GitHub issue #64) — the exact
    same renderer the live broadcast path uses, so the two can never
    drift apart on the gated-display rule. Moderation kinds
    (`_VERB_BY_KIND`) and `nick` itself deliberately don't go through
    it: moderation/auditing always shows canonical identity only (design
    doc), and a `nick` event's own body text already
    fully describes the change — neither is a candidate for verified-
    name display in the first place.

    `user` is the *replaying* session's own account — every kind gets
    prefixed with `message.created_at` per `user`'s own timestamp
    preference (design doc -- per-user chat timestamp preference),
    uniformly across every kind rather than selectively by original
    live-broadcast coverage: "replayed scrollback" is its own category
    in that scope, not a kind-by-kind carryover of which live
    events happen to be timestamped.
    """
    if message.kind in ("message", "action", "join", "leave"):
        return _render_channel_message(db, channel, user, message, self_message=False)
    if message.kind == "nick":
        line = colored(
            f"*** {sanitize_text(message.author_label)} {sanitize_text(message.body)}",
            fg_color=MUTED_COLOR,
        )
    elif message.kind == "daybreak":
        # No author at all (design doc) -- a standalone system
        # announcement, unlike every other kind here, none of which
        # reference message.author_label.
        line = colored(sanitize_text(message.body), fg_color=MUTED_COLOR)
    else:  # message.kind in _VERB_BY_KIND
        author_label = sanitize_text(message.author_label)
        detail = f" ({sanitize_text(message.body)})" if message.body else ""
        line = colored(
            f"*** {author_label} was {_VERB_BY_KIND[message.kind]}{detail}.", fg_color=MUTED_COLOR
        )
    return format_with_preference(db, user, line, message.created_at)


def _render_all_scrollback(db: Database, channel: Channel, user: User, messages: list[ChannelMessage]) -> list[str]:
    """Bundles rendering the whole scrollback replay into one `lane.run`
    call rather than one per message -- the same "one round
    trip, not N" reasoning as every other bundled loop in this module."""
    return [_render_scrollback_message(db, channel, user, message) for message in messages]


# -- command dispatch (design doc §13) ------------------------


@dataclass(frozen=True)
class ChatCommandContext:
    """Everything a slash-command handler might need, bundled into one
    consistent shape — replaces what used to be a different ad hoc
    positional-argument list per handler (`/mute` etc. each
    took their own subset of `session, db, hub, channel, user`;
    `/finger` omitted `hub` entirely). `lane`, not `db` --
    see this module's own docstring."""

    session: Session
    lane: DatabaseLane
    hub: ChatHub
    presence: PresenceRegistry
    mailbox: MessageMailbox
    channel: Channel
    user: User
    participant_id: ParticipantId
    session_registry: ActiveSessionRegistry | None = None


@dataclass(frozen=True)
class _Quit:
    """Exit chat entirely, back to the main menu — `/quit`'s meaning,
    and also what a kick/ban or a dropped connection resolves to."""


@dataclass(frozen=True)
class _ToPicker:
    """Exit the current channel, back to channel selection — `/leave`'s
    meaning (previously an alias for `/quit`)."""


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
    <user>`'s meaning. Unlike `_ToPicker`/`_SwitchTo`, this
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
# `send_loop` without going any further (`_EnterPrivate`/`_ExitPrivate`
# — see their own docstrings). Originally just `bool`
# (only `/quit` ever returned `True`) — widened, not replaced, each
# time a new command needed to distinguish another outcome from plain
# "keep going": the same "explicit return contract, not exceptions"
# reasoning already established, just with more to say than a
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

    Also doubles as invitation *acceptance* (design doc): there is no
    separate `/accept` command — successfully
    joining a `members_only` channel via a pending invitation marks it
    accepted, reusing this existing "look up, check authorization,
    switch" flow instead of inventing parallel command surface for the
    same action.

    Authorization itself is `_authorize_channel_entry` (GitHub issue
    #28, reopened a third time) -- the same check `browse_channels` now
    runs before *any* path into `_chat_loop`, not duplicated here. The
    "already in this channel" check runs first, ahead of it, purely so
    that case gets its own clearer message rather than being folded
    into a generic authorization failure.
    """
    channel_name = args.strip()
    if not channel_name:
        await _show_usage(ctx.session, "join")
        return None

    try:
        channel = await ctx.lane.run(get_channel_by_name, channel_name)
    except ChannelError:
        await ctx.session.write_line(
            colored(f"No such channel: {sanitize_text(channel_name)!r}", fg_color=MUTED_COLOR)
        )
        return None

    if channel.id == ctx.channel.id:
        await ctx.session.write_line(
            colored(f"You are already in #{sanitize_text(channel.name)}.", fg_color=MUTED_COLOR)
        )
        return None

    allowed, denial_message = await ctx.lane.run(_authorize_channel_entry, channel, ctx.user)
    if not allowed:
        await ctx.session.write_line(colored(denial_message, fg_color=MUTED_COLOR))
        return None

    return _SwitchTo(channel)


async def _handle_topic(ctx: ChatCommandContext, args: str) -> None:
    """
    `/topic <text>` sets the channel topic, gated by
    `ChannelPermission.EDIT` (see `netbbs.chat.channels.set_topic`). A
    bare `/topic` clears it -- same reasoning as `/nick`'s own bare
    invocation (and requiring the same `EDIT` permission `set_topic`
    already gates clearing on, same as setting): the status line
    already shows whatever topic is currently set, so a query-only bare
    invocation would just be redundant, and the only useful thing left
    for it to do is act. Deliberately not persisted into scrollback
    (design doc only asks for moderation-log history,
    unlike `/nick`'s explicit scrollback requirement) — a live
    in-channel notice plus the audit log entry `set_topic` already
    writes is enough.
    """
    try:
        await ctx.lane.run(set_topic, ctx.channel, args or None, set_by=ctx.user)
    except TopicError:
        await ctx.session.write_line(
            colored("You do not have permission to change the topic.", fg_color=MUTED_COLOR)
        )
        return

    if args:
        notice = colored(
            f"*** Topic changed by {sanitize_text(ctx.user.username)}: {sanitize_text(args)}",
            fg_color=MUTED_COLOR,
        )
    else:
        notice = colored(f"*** Topic cleared by {sanitize_text(ctx.user.username)}", fg_color=MUTED_COLOR)
    await ctx.session.write_line(notice)
    await ctx.hub.broadcast(ctx.channel.name, notice, exclude={ctx.participant_id})


def _find_live_participants(db: Database, hub: ChatHub, username: str) -> list[tuple[str, ParticipantId]]:
    """
    Every live session belonging to `username`, across every channel —
    `(channel_name, participant_id)` for each one found, not just the
    first (GitHub issue #27: a recipient with several simultaneous
    chat sessions used to have only the first one located actually
    receive a `/msg`). `ChatHub` has no reverse "which channel is this
    user in" index, the same O(channels) shape `_kick_live_sessions`
    already has for the same reason. Reordered `db`-first
    (was `hub`-first) so `DatabaseLane.run`'s db-injection convention
    applies without a wrapper closure -- private, single call site.
    """
    found = []
    for channel in list_channels(db):
        for pid in hub.participants_for_username(channel.name, username):
            found.append((channel.name, pid))
    return found


async def _deliver_private_message(ctx: ChatCommandContext, target: User, body: str) -> None:
    """
    Delivers a private message to *every* one of `target`'s currently
    active sessions independently (GitHub issue #27's session-
    addressed redesign, replacing the previous account-wide-mailbox
    behavior): instantly via `ChatHub` for whichever are currently live
    in some channel (the same delivery mechanism moderation notices
    already use, just a differently-formatted string — no new
    `receive_loop` branch needed), and queued in the mailbox — keyed by
    that specific `Session` object, not the account — for the rest,
    each to be shown at its own next natural prompt (design doc).

    `ctx.session_registry` (`netbbs.net.session_registry.
    ActiveSessionRegistry`) is what makes "every session," not just
    one, possible at all — without it (only reachable by a caller that
    bypasses the real node-wide registry entirely, see
    `browse_channels`'s docstring), this can only still reach sessions
    currently live in chat; there is no way to address a non-chat
    session's mailbox slot without actually knowing which `Session`
    objects exist for the account.

    Never written to scrollback or the moderation log — these
    "online-only" private messages are intentionally as ephemeral as
    live chat itself.
    """
    sender_label = sanitize_text(await ctx.lane.run(display_label, ctx.user))
    notice = colored(
        f"*** Private message from {sender_label}: {sanitize_text(body)}",
        fg_color=MUTED_COLOR,
        bold=True,
    )
    created_at = utc_now_iso()

    live = await ctx.lane.run(_find_live_participants, ctx.hub, target.username)
    live_session_keys = {pid.session_key for _channel_name, pid in live}
    for channel_name, pid in live:
        await ctx.hub.send_to(channel_name, pid, _TimestampedNotice(notice, created_at))

    if ctx.session_registry is not None:
        for target_session in ctx.session_registry.sessions_for_username(target.username):
            if id(target_session) in live_session_keys:
                continue  # already delivered live, above -- not also queued
            ctx.mailbox.deliver(target_session, notice, created_at)

    await ctx.session.write_line(
        colored(f"(sent to {sanitize_text(target.username)})", fg_color=MUTED_COLOR)
    )


async def _handle_msg(ctx: ChatCommandContext, args: str) -> None:
    """
    `/msg <user> <text>` (design doc): a one-off,
    online-only private message. Scoped as a chat-context command only,
    matching every other chat command — no parallel main-menu entry point.
    """
    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        await _show_usage(ctx.session, "msg")
        return
    target_name, body = parts

    target = await _resolve_target(ctx.session, ctx.lane, target_name)
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
    `/private <user>` (design doc): enters a temporary
    private-conversation mode layered on `/msg` — ordinary (non-slash)
    input is sent privately to `target` until `/close`. The old `/query`
    IRC-compatibility alias for this handler was removed —
    it was the only command with two names and added no value beyond
    what `/private` already provides.
    """
    target_name = args.split(maxsplit=1)[0] if args.split() else ""
    if not target_name:
        await _show_usage(ctx.session, "private")
        return None

    target = await _resolve_target(ctx.session, ctx.lane, target_name)
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
    `/help` (no args, design doc): lists every command visible
    to the caller — reuses the exact same `_COMMAND_VISIBILITY`
    predicate Tab completion already applies, so
    the list matches what `/` + Tab would offer, with syntax and a
    one-line description attached to each command — the actual
    value-add over Tab completion this was built to provide (the
    old version just printed the same bare name list Tab completion
    already surfaces).

    `/help <command>` shows one command's full detail regardless of the
    caller's own visibility for it — consistent with the established
    framing that visibility gating is a suggestion filter,
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

    def _visible_names(db: Database) -> list[str]:
        return sorted(
            name
            for name in _COMMANDS
            if name in _COMMAND_INFO
            and (_COMMAND_VISIBILITY.get(name) is None or _COMMAND_VISIBILITY[name](db, ctx.channel, ctx.user))
        )

    await ctx.session.write_line(colored("Available commands:", fg_color=MUTED_COLOR, bold=True))
    visible_names = await ctx.lane.run(_visible_names)
    for name in visible_names:
        syntax, description = _COMMAND_INFO[name]
        await ctx.session.write_line(f"{colored(syntax, fg_color=MUTED_COLOR, bold=True)} - {description}")


async def _dispatch_command(ctx: ChatCommandContext, line: str) -> ChatAction | None:
    """
    `line` is known to start with `/` (checked by the caller). Any
    such line is now always treated as a command attempt — looked up
    in `_COMMANDS`, and if not found, "Unknown command" is shown and
    nothing is broadcast. Previously, an unrecognized `/x`
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


# -- mute/ban/kick (design doc §13) -----------------------

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


@dataclass(frozen=True)
class _TimestampedNotice:
    """
    Delivered through `ChatHub.broadcast`/`send_to` for any event whose
    display should honor each *recipient's* own timestamp preference
    (design doc): `text` is the already-rendered line, `created_at`
    the raw moment it happened. `receive_loop` is the one place that
    turns this into a final string, via `format_with_preference`,
    using the receiving session's own user -- the same reason a plain
    rendered string can't be broadcast directly here the way it is for
    events outside this scope (e.g. `/topic`/`/nick` notices): a shared
    string can't reflect a
    per-recipient decision.
    """

    text: str
    created_at: str


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

    A deliberate, flagged-as-reconsiderable heuristic (design doc)
    — matches the common `!mute @user 10m
    spamming`-style convention several existing chat moderation tools
    use, not something settled permanently.
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
    lane: DatabaseLane, hub: ChatHub, channel: Channel, *, kind: str, target_label: str, detail: str
) -> None:
    """Records a scrollback event and broadcasts a system notice —
    matches the existing join/leave precedent in `_chat_loop` exactly
    (design doc §13: "all actions logged and echoed in-channel for
    transparency"). Not excluding anyone from the broadcast, unlike
    join/leave: there's no separate direct message a moderation
    action's *target* gets the way a joining/leaving user gets "Joined
    #channel" — they see the same notice as everyone else."""
    await lane.run(record_message, channel, kind=kind, author_label=target_label, body=detail)
    notice = colored(
        f"*** {sanitize_text(target_label)} was {_VERB_BY_KIND[kind]} ({sanitize_text(detail)}).",
        fg_color=MUTED_COLOR,
    )
    await hub.broadcast(channel.name, notice)


async def _show_usage(session: Session, command: str) -> None:
    """Writes the standard `"Usage: /command <args>"` message for
    `command`, generated from `_COMMAND_INFO` (design doc) —
    the single source of truth for command syntax, also used by
    `_handle_help`, instead of each handler carrying its own
    independently-maintained copy of the same text."""
    syntax, _ = _COMMAND_INFO[command]
    await session.write_line(colored(f"Usage: {syntax}", fg_color=MUTED_COLOR))


async def _resolve_target(session: Session, lane: DatabaseLane, username: str) -> User | None:
    """Look up a mute/ban/kick command's target username, writing a
    friendly message and returning `None` if there's no such account —
    `AuthError`'s own message is deliberately generic for login-failure
    enumeration-avoidance (see its docstring), not meant for this
    different, non-login context. `lane`, not `db` -- split
    from a single function body mixing a synchronous lookup with async
    session I/O into a `lane.run` call (the lookup) wrapped by ordinary
    `await`s (the write) in this same coroutine, since a lane job can't
    itself await anything."""
    try:
        return await lane.run(get_user_by_username, username)
    except AuthError:
        await session.write_line(colored(f"No such user: {sanitize_text(username)!r}", fg_color=MUTED_COLOR))
        return None


async def _kick_live_sessions(hub: ChatHub, channel: Channel, target: User, *, reason: str) -> None:
    """Force out every currently-connected session belonging to
    `target` in `channel` — used by `/kick`, `/ban` (a ban that doesn't
    remove an already-present target would be meaningless), and
    `/revokeaccess` on a `members_only` channel (GitHub issue #28). A
    target with no live session in this channel is not an error;
    there's simply nothing to do.

    `priority=True` (GitHub issue #31, reopened): `_KickNotice` is a
    mandatory state transition, not ordinary chat traffic — a target
    with a full, stalled queue must still receive it, evicting whatever
    ordinary message would otherwise have occupied that freed slot,
    rather than the removal itself silently being the thing that gets
    dropped on overflow.
    """
    for participant_id in hub.participants_for_username(channel.name, target.username):
        await hub.send_to(channel.name, participant_id, _KickNotice(reason=reason), priority=True)


async def _handle_mute(ctx: ChatCommandContext, args: str) -> None:
    parts = args.split(maxsplit=1)
    if not parts:
        await _show_usage(ctx.session, "mute")
        return
    target_name, rest = parts[0], (parts[1] if len(parts) > 1 else "")
    duration, reason = _split_duration_and_reason(rest)

    target = await _resolve_target(ctx.session, ctx.lane, target_name)
    if target is None:
        return

    try:
        await ctx.lane.run(mute_user, ctx.channel, target, duration=duration, reason=reason, muted_by=ctx.user)
    except ChatModerationError:
        await ctx.session.write_line(
            colored("You do not have permission to mute in this channel.", fg_color=MUTED_COLOR)
        )
        return

    detail = _moderation_detail(ctx.user.username, duration, reason)
    await _announce_moderation(ctx.lane, ctx.hub, ctx.channel, kind="mute", target_label=target.username, detail=detail)


async def _handle_unmute(ctx: ChatCommandContext, args: str) -> None:
    target_name = args.split(maxsplit=1)[0] if args.split() else ""
    if not target_name:
        await _show_usage(ctx.session, "unmute")
        return

    target = await _resolve_target(ctx.session, ctx.lane, target_name)
    if target is None:
        return

    try:
        await ctx.lane.run(unmute_user, ctx.channel, target, unmuted_by=ctx.user)
    except ChatModerationError:
        await ctx.session.write_line(
            colored("You do not have permission to unmute in this channel.", fg_color=MUTED_COLOR)
        )
        return

    detail = _moderation_detail(ctx.user.username, None, None)
    await _announce_moderation(
        ctx.lane, ctx.hub, ctx.channel, kind="unmute", target_label=target.username, detail=detail
    )


async def _handle_ban(ctx: ChatCommandContext, args: str) -> None:
    parts = args.split(maxsplit=1)
    if not parts:
        await _show_usage(ctx.session, "ban")
        return
    target_name, rest = parts[0], (parts[1] if len(parts) > 1 else "")
    duration, reason = _split_duration_and_reason(rest)

    target = await _resolve_target(ctx.session, ctx.lane, target_name)
    if target is None:
        return

    try:
        await ctx.lane.run(ban_user, ctx.channel, target, duration=duration, reason=reason, banned_by=ctx.user)
    except ChatModerationError:
        await ctx.session.write_line(
            colored("You do not have permission to ban in this channel.", fg_color=MUTED_COLOR)
        )
        return

    detail = _moderation_detail(ctx.user.username, duration, reason)
    await _announce_moderation(ctx.lane, ctx.hub, ctx.channel, kind="ban", target_label=target.username, detail=detail)
    await _kick_live_sessions(ctx.hub, ctx.channel, target, reason="banned")


async def _handle_unban(ctx: ChatCommandContext, args: str) -> None:
    target_name = args.split(maxsplit=1)[0] if args.split() else ""
    if not target_name:
        await _show_usage(ctx.session, "unban")
        return

    target = await _resolve_target(ctx.session, ctx.lane, target_name)
    if target is None:
        return

    try:
        await ctx.lane.run(unban_user, ctx.channel, target, unbanned_by=ctx.user)
    except ChatModerationError:
        await ctx.session.write_line(
            colored("You do not have permission to unban in this channel.", fg_color=MUTED_COLOR)
        )
        return

    detail = _moderation_detail(ctx.user.username, None, None)
    await _announce_moderation(
        ctx.lane, ctx.hub, ctx.channel, kind="unban", target_label=target.username, detail=detail
    )


async def _handle_kick(ctx: ChatCommandContext, args: str) -> None:
    parts = args.split(maxsplit=1)
    if not parts:
        await _show_usage(ctx.session, "kick")
        return
    target_name = parts[0]
    reason = parts[1] if len(parts) > 1 else None

    target = await _resolve_target(ctx.session, ctx.lane, target_name)
    if target is None:
        return

    try:
        await ctx.lane.run(kick_user, ctx.channel, target, reason=reason, kicked_by=ctx.user)
    except ChatModerationError:
        await ctx.session.write_line(
            colored("You do not have permission to kick in this channel.", fg_color=MUTED_COLOR)
        )
        return

    detail = _moderation_detail(ctx.user.username, None, reason)
    await _announce_moderation(ctx.lane, ctx.hub, ctx.channel, kind="kick", target_label=target.username, detail=detail)
    await _kick_live_sessions(ctx.hub, ctx.channel, target, reason="kicked")


async def _write_vcard_detail(session: Session, lane: DatabaseLane, vcard: VCard) -> None:
    """Shared by `/finger` and `/whois` (design doc) — the
    identity/bio block both commands show identically; `/whois`
    appends online/away/channel-membership lines of its own after
    calling this. `lane`, not `db` -- the one DB read
    (`resolve_display_preferences`) happens via `lane.run` before any
    `await session.write_line(...)` call, same split as
    `_resolve_target`."""
    display_format, display_timezone = await lane.run(resolve_display_preferences)
    when = format_for_display(vcard.created_at, override_format=display_format, override_timezone=display_timezone)
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

    target = await _resolve_target(ctx.session, ctx.lane, target_name)
    if target is None:
        return

    vcard = await ctx.lane.run(get_vcard, target, requesting_user=ctx.user)
    await _write_vcard_detail(ctx.session, ctx.lane, vcard)


async def _handle_me(ctx: ChatCommandContext, args: str) -> ChatAction | None:
    """
    `/me <action>` (design doc): a typed action
    event ("* alice waves"), stored and transported as a distinct
    event kind rather than encoded as specially formatted ordinary
    text. Rendered identically for the actor and everyone else —
    unlike a regular chat message, there's no "my own words" distinction
    worth making for a shared narrative-style action.
    """
    if not args:
        await _show_usage(ctx.session, "me")
        return

    # GitHub issue #64: a long-lived session's meets_name_requirement()
    # was only ever checked once, at channel entry -- re-checked here
    # (and in send_loop's plain-message branch) against the *current*
    # policy/attestation state before accepting the send. See
    # _meets_live_participation_requirements's own docstring.
    if not await ctx.lane.run(_meets_live_participation_requirements, ctx.channel, ctx.user):
        await ctx.session.write_line(colored(_NO_LONGER_QUALIFIES_MESSAGE, fg_color=MUTED_COLOR))
        return _ToPicker()

    # GitHub issue #30: /me is a slash command, so it used to reach the
    # dispatcher before send_loop's own is_muted() check (which only
    # guards the plain, non-slash message branch) -- a muted user could
    # still broadcast arbitrary visible text as an action event. Same
    # response text/expiry formatting as the ordinary-message check.
    until = await ctx.lane.run(_check_mute, ctx.channel, ctx.user)
    if until is not None:
        await ctx.session.write_line(
            colored(f"You are muted in this channel ({until}).", fg_color=MUTED_COLOR)
        )
        return

    recorded = await ctx.lane.run(
        record_message,
        ctx.channel,
        kind="action",
        author_label=ctx.user.username,
        author_fingerprint=ctx.user.fingerprint,
        body=args,
    )
    await ctx.session.write_line(await ctx.lane.run(_render_channel_message, ctx.channel, ctx.user, recorded))
    await ctx.hub.broadcast(ctx.channel.name, recorded, exclude={ctx.participant_id})


async def _handle_nick(ctx: ChatCommandContext, args: str) -> None:
    """
    `/nick <name>` sets a transparent display alias (design doc,
    points 7-10). A bare `/nick` clears it -- same reasoning as
    `/timestamps`'s own bare invocation: the status line already shows
    whatever alias is currently active, so a query-only bare invocation
    would just be redundant, and the only useful thing left for it to
    do is act. Deliberately no magic `"off"`/`"clear"`/etc. keyword --
    unlike `/timestamps` (whose `on`/`off` are the only two states that
    exist, so there's nothing real a literal alias named "on" could
    ever mean), a nick is free-form user-chosen text, and reserving any
    one spelling would be a real, if narrow, loss for zero gain now
    that bare `/nick` already covers clearing it. Nickname changes are
    their own typed scrollback event, recorded for the channel this
    command was run in — announced the same way join/leave/action
    events are.
    """
    if not args:
        await ctx.lane.run(set_nick, ctx.user, "")
        await _announce_nick_change(ctx, new_nick=None)
        return

    try:
        await ctx.lane.run(set_nick, ctx.user, args)
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
    await ctx.lane.run(record_message, ctx.channel, kind="nick", author_label=ctx.user.username, body=body)
    await ctx.hub.broadcast(ctx.channel.name, notice, exclude={ctx.participant_id})


async def _handle_away(ctx: ChatCommandContext, args: str) -> None:
    """
    `/away [message]` (design doc): sets a node-wide
    away status shared across every one of the account's active
    sessions; `/away` with no argument clears it. Not written to
    channel scrollback or broadcast — away status is "visible through
    local presence views and private-message feedback" per the design
    doc, neither of which exist yet, so for now this is a private
    confirmation to the user themselves only.
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


async def _handle_timestamps(ctx: ChatCommandContext, args: str) -> None:
    """
    `/timestamps [on|off]`: sets the persistent per-user preference
    controlling whether chat lines show a display timestamp, defaulting
    to off. A bare `/timestamps` toggles the current state rather than
    just reporting it -- querying the state is not useful on its own
    since the user's screen already shows whether timestamps are
    present, so a bare invocation's intent is unambiguous the same way
    `/away` treats one.
    """
    choice = args.strip().lower()
    if not choice:
        new_state = not await ctx.lane.run(timestamps_enabled, ctx.user)
    elif choice == "on":
        new_state = True
    elif choice == "off":
        new_state = False
    else:
        await _show_usage(ctx.session, "timestamps")
        return

    await ctx.lane.run(set_timestamps_enabled, ctx.user, new_state)
    state = "on" if new_state else "off"
    await ctx.session.write_line(colored(f"Chat timestamps are now {state}.", fg_color=MUTED_COLOR))


# -- discovery (design doc) ------------------


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
    sorted case-insensitively."""
    usernames = {pid.username for pid in hub.participant_ids(channel.name)}
    return sorted(usernames, key=str.lower)


async def _handle_names(ctx: ChatCommandContext, args: str) -> None:
    """`/names` (design doc): a compact, one-line roster
    of `ctx.channel`."""
    usernames = _roster_usernames(ctx.hub, ctx.channel)
    if not usernames:
        await ctx.session.write_line(colored("No one is here.", fg_color=MUTED_COLOR))
        return

    def _labels(db: Database) -> list[str]:
        result = []
        for username in usernames:
            user = _lookup_user_quietly(db, username)
            if user is not None:
                result.append(sanitize_text(display_label(db, user)))
        return result

    labels = await ctx.lane.run(_labels)
    await ctx.session.write_line(", ".join(labels))


async def _handle_who(ctx: ChatCommandContext, args: str) -> None:
    """`/who` (design doc): the more detailed presence
    view of `ctx.channel` — one line per person, with an away
    indicator where applicable."""
    usernames = _roster_usernames(ctx.hub, ctx.channel)
    if not usernames:
        await ctx.session.write_line(colored("No one is here.", fg_color=MUTED_COLOR))
        return

    def _labels(db: Database) -> dict[str, str]:
        result = {}
        for username in usernames:
            user = _lookup_user_quietly(db, username)
            if user is not None:
                result[username] = sanitize_text(display_label(db, user))
        return result

    labels_by_username = await ctx.lane.run(_labels)
    for username in usernames:
        label = labels_by_username.get(username)
        if label is None:
            continue
        if ctx.presence.is_away(username):
            message = ctx.presence.get_away_message(username)
            suffix = f" (away: {sanitize_text(message)})" if message else " (away)"
        else:
            suffix = ""
        await ctx.session.write_line(f"{label}{suffix}")


async def _handle_list(ctx: ChatCommandContext, args: str) -> None:
    """`/list` (design doc): every channel `ctx.user`'s
    level allows, "exposes only channels visible to the requesting
    user." Flat and sorted pinned-first-then-alphabetical, matching
    `list_boards`/`_pick_channel`'s existing sort
    precedent — a quick text reference from inside chat, not the
    interactive category-nested picker the main menu's Chat option
    already provides."""
    visible = await ctx.lane.run(_visible_channels_for, ctx.user)
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
    db: Database, hub: ChatHub, requesting_user: User, target_username: str
) -> list[str]:
    """
    Every channel `target_username` currently has a live session in,
    restricted to channels `requesting_user` can themselves see
    (`_visible_channels_for` — the same filter `/list` and the picker
    use). This *is* the "hidden-channel visibility" `/whois` must
    respect (design doc), now actually enforced against real
    hidden channels, not just consistently applied ahead of
    their existence the way it was originally left.

    `ChatHub` has no reverse "which channels is this user in" index —
    only per-channel participant lists — so this checks every visible
    channel's roster in turn. O(channels × participants); fine at this
    project's declared scale (§14). Reordered `db`-first
    (was `hub`-first) -- see `_find_live_participants`'s own docstring
    for why.
    """
    visible = _visible_channels_for(db, requesting_user)
    names = []
    for channel in visible:
        if hub.participants_for_username(channel.name, target_username):
            names.append(channel.name)
    return names


async def _handle_whois(ctx: ChatCommandContext, args: str) -> None:
    """
    `/whois <user>` (design doc): reuses `get_vcard`
    for the identity/bio block (`_write_vcard_detail`,
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

    target = await _resolve_target(ctx.session, ctx.lane, target_name)
    if target is None:
        return

    vcard = await ctx.lane.run(get_vcard, target, requesting_user=ctx.user)
    await _write_vcard_detail(ctx.session, ctx.lane, vcard)

    online = ctx.presence.is_online(target.username)
    await ctx.session.write_line(f"Status: {'online' if online else 'offline'}")
    if ctx.presence.is_away(target.username):
        message = ctx.presence.get_away_message(target.username)
        await ctx.session.write_line(f"Away: {sanitize_text(message)}" if message else "Away")

    channel_names = await ctx.lane.run(_channel_names_for_user, ctx.hub, ctx.user, target.username)
    if channel_names:
        joined = ", ".join(f"#{sanitize_text(name)}" for name in channel_names)
        await ctx.session.write_line(f"Channels: {joined}")


# -- invite-only channels & membership admin (design doc §8) -------------------------------------


async def _handle_invite(ctx: ChatCommandContext, args: str) -> None:
    """
    `/invite <user>` (design doc): allowed if the
    actor holds `ChannelPermission.MANAGE_MEMBERS`, **or** the channel
    has `allow_member_invites` set and the actor is already a member —
    `create_invitation` itself is the one place that authorization
    decision is made (`netbbs.chat.membership`), not duplicated here.

    The durable `channel_invitations` row `create_invitation` writes is
    the actual notification mechanism now (GitHub issue #42) —
    discoverable by the invitee at their own next login/main-menu visit
    (`netbbs.net.login_flow._announce_pending_invitations`/
    `_show_pending_invitations`) regardless of whether they're online
    right now. Live delivery through the mailbox/push mechanism
    (`_deliver_private_message`) remains a *convenience* on top of that
    for a currently-online invitee, gated on `ctx.presence.is_online`
    first -- the same check `_handle_msg` already makes before ever
    attempting delivery. Calling `_deliver_private_message`
    unconditionally, the way this used to, was the actual bug: that
    mailbox is session-addressed and ephemeral (see its own docstring)
    and silently reaches nobody for an offline invitee, yet it always
    printed "(sent to X)" regardless -- misleading the inviter into
    believing a notification went out when none did.
    """
    target_name = args.strip()
    if not target_name:
        await _show_usage(ctx.session, "invite")
        return

    target = await _resolve_target(ctx.session, ctx.lane, target_name)
    if target is None:
        return

    try:
        await ctx.lane.run(create_invitation, ctx.channel, target, invited_by=ctx.user)
    except MembershipError:
        await ctx.session.write_line(
            colored("You do not have permission to invite users to this channel.", fg_color=MUTED_COLOR)
        )
        return

    if ctx.presence.is_online(target.username):
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

    target = await _resolve_target(ctx.session, ctx.lane, target_name)
    if target is None:
        return

    try:
        await ctx.lane.run(revoke_invitation, ctx.channel, target, revoked_by=ctx.user)
    except MembershipError:
        await ctx.session.write_line(
            colored("You do not have permission to do that, or there is no pending invitation.", fg_color=MUTED_COLOR)
        )
        return

    await ctx.session.write_line(
        colored(f"Invitation for {sanitize_text(target.username)} revoked.", fg_color=MUTED_COLOR)
    )


async def _handle_grantaccess(ctx: ChatCommandContext, args: str) -> None:
    """`/grantaccess <user>` (design doc): directly
    adds `target` to `channel_members`, bypassing the invite-then-accept
    flow entirely — a distinct capability from `/invite`, not an
    alternate way to trigger the same thing."""
    target_name = args.strip()
    if not target_name:
        await _show_usage(ctx.session, "grantaccess")
        return

    target = await _resolve_target(ctx.session, ctx.lane, target_name)
    if target is None:
        return

    try:
        await ctx.lane.run(add_member, ctx.channel, target, granted_by=ctx.user)
    except MembershipError:
        await ctx.session.write_line(
            colored("You do not have permission to manage members on this channel.", fg_color=MUTED_COLOR)
        )
        return

    await ctx.session.write_line(
        colored(f"Granted {sanitize_text(target.username)} access to this channel.", fg_color=MUTED_COLOR)
    )


async def _handle_revokeaccess(ctx: ChatCommandContext, args: str) -> None:
    """`/revokeaccess <user>` (design doc): removes a
    `channel_members` grant and, for a `members_only` channel, forces
    out any of the target's currently-live sessions in it (GitHub issue
    #28) -- for an access-*restricted* channel specifically, letting an
    already-connected target keep reading/sending indefinitely until
    they happen to leave or reconnect would make the revocation
    meaningless in the moment that actually matters. An open (not
    `members_only`) channel's own membership grant is a lesser, purely
    persistent-access concept -- revoking it there doesn't eject anyone
    still present, matching `/kick`'s existing role as the separate,
    general-purpose "remove someone right now" action for that case."""
    target_name = args.strip()
    if not target_name:
        await _show_usage(ctx.session, "revokeaccess")
        return

    target = await _resolve_target(ctx.session, ctx.lane, target_name)
    if target is None:
        return

    try:
        await ctx.lane.run(remove_member, ctx.channel, target, removed_by=ctx.user)
    except MembershipError:
        await ctx.session.write_line(
            colored("You do not have permission to manage members on this channel.", fg_color=MUTED_COLOR)
        )
        return

    if ctx.channel.members_only:
        await _kick_live_sessions(ctx.hub, ctx.channel, target, reason="removed")

    await ctx.session.write_line(
        colored(f"Revoked {sanitize_text(target.username)}'s access to this channel.", fg_color=MUTED_COLOR)
    )


async def _handle_members(ctx: ChatCommandContext, args: str) -> None:
    """`/members` (design doc): lists current direct
    members. Viewable by anyone already in the channel (you can only
    run this from inside it) — not further gated, reviewing your own
    channel's roster is different from administering it."""
    members = await ctx.lane.run(list_members, ctx.channel)
    if not members:
        await ctx.session.write_line(colored("No members have been granted access yet.", fg_color=MUTED_COLOR))
        return
    names = ", ".join(sanitize_text(member.username) for member in members)
    await ctx.session.write_line(f"Members: {names}")


# Design doc: the single source of truth for every command's
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
    "topic": ("/topic [text]", "Set the channel topic; a bare /topic clears it (requires edit permission)."),
    "msg": ("/msg <user> <text>", "Send a one-off private message to an online user."),
    "private": ("/private <user>", "Enter a private conversation with an online user."),
    "close": ("/close", "Leave the current private conversation."),
    "help": ("/help [command]", "List available commands, or show detail for one."),
    "me": ("/me <action>", 'Send an action message (e.g. "* alice waves").'),
    "nick": ("/nick [name]", "Set your display alias; a bare /nick clears it."),
    "away": ("/away [message]", "Mark yourself away, or clear away status."),
    "timestamps": ("/timestamps [on|off]", "Toggle chat timestamps, or set them on/off explicitly."),
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
    "?": _handle_help,  # terse alias (design doc) -- a genuinely
                         # distinct trigger for /help, not a second name
                         # for a command that already has one (see the
                         # removal of /query for the contrast)
    "me": _handle_me,
    "nick": _handle_nick,
    "away": _handle_away,
    "timestamps": _handle_timestamps,
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


# -- Tab completion (design doc) --------------------------

# Per-command visibility predicate for completion *suggestions* only --
# deliberately a separate dict rather than widening _COMMANDS' own value
# type, so dispatch (_dispatch_command) and /help's listing need no
# changes at all. A command absent from this dict is always suggested.
# This is purely a suggestion filter, not an authorization check: the
# handlers themselves (mute_user/kick_user/etc., via ChatModerationError/
# MembershipError) remain the sole source of truth for what's actually
# allowed to run.
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
    real authorization exactly (design doc's opt-in) --
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
# accounts too, so the *command* itself accepts any registered username
# typed in full). /invite is its own case just below -- eligible
# candidates are registered users who aren't already members.
#
# Tab-completion *suggestions* for /whois and /finger are narrower than
# what the command itself accepts, though: on a node with hundreds of
# registered users, offering every one of them from a single typed
# character is noise, not help. So completion only searches this
# channel's current roster (`_roster_usernames`, the same live source
# the status line's own online count uses) -- a user not currently in
# the channel still works if typed out in full, just without tab
# assistance for it.
_ONLINE_USER_COMMAND_PREFIXES = ("/msg ", "/private ")
_ANY_USER_COMMAND_PREFIXES = ("/whois ", "/finger ")
_INVITE_COMMAND_PREFIX = "/invite "


async def _build_completer(
    lane: DatabaseLane, hub: ChatHub, presence: PresenceRegistry, channel: Channel, user: User
) -> Completer:
    """
    Builds one Tab-completion closure per `read_line()` call in
    `send_loop`, from the state available there -- cheap (a handful of
    string comparisons), and always reflects the actor's *current*
    permissions rather than a snapshot taken once at channel entry
    (moderator grants can change mid-session, unlike a static completer
    built once and reused).

    All matching is case-insensitive (design doc).

    `async` and `lane`-based, not `db`-based: the returned
    `completer(text)` closure is itself a plain *synchronous* callable
    (`netbbs.net.char_input.Completer`'s own contract) that `session.
    read_line` may call several times per invocation, once per Tab
    press -- it structurally cannot make a `lane.run` call itself, the
    same "callback can't await" problem `netbbs.net.picker.pick_item`'s
    `name_of`/`description_of` callbacks had. Fixed the
    same way: everything the completer might need -- which commands are
    currently visible, the full username list, and current membership --
    is fetched once, eagerly, in one bundled `lane.run` call *before*
    the closure is built, and the closure itself only ever reads that
    already-fetched data. `roster_usernames` is the one exception --
    `_roster_usernames` reads `hub`'s live in-memory participant map,
    not `db`, so it's computed directly here rather than routed through
    `lane.run`, the same reasoning `_repaint_status_line` already
    applies to `presence.is_away`.
    """

    def _gather(db: Database) -> tuple[list[str], list[str], set[str]]:
        visible_commands = sorted(
            f"/{name}"
            for name in _COMMANDS
            if _COMMAND_VISIBILITY.get(name) is None or _COMMAND_VISIBILITY[name](db, channel, user)
        )
        all_users = list_users(db)
        all_usernames = [candidate.username for candidate in all_users]
        member_usernames = {candidate.username for candidate in all_users if is_member(db, channel, candidate)}
        return visible_commands, all_usernames, member_usernames

    visible_commands, all_usernames, member_usernames = await lane.run(_gather)
    roster_usernames = _roster_usernames(hub, channel)

    def completer(text: str) -> list[str]:
        if text.startswith("/") and " " not in text:
            prefix = text[1:].lower()
            return sorted(name for name in visible_commands if name[1:].lower().startswith(prefix))

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
                return sorted(name for name in roster_usernames if name.lower().startswith(word))

        rest = text[len(_INVITE_COMMAND_PREFIX) :]
        if text.lower().startswith(_INVITE_COMMAND_PREFIX) and " " not in rest:
            word = rest.lower()
            return sorted(
                name for name in all_usernames if name.lower().startswith(word) and name not in member_usernames
            )

        return []

    return completer


# -- chat status line + pinned input row (design doc) ---

# The scroll region reserves the terminal's last two rows -- the pinned
# status row, then the input row below it -- for the duration of the
# session (rows 1..height-2 scroll normally; neither reserved row ever
# does). Status above input -- originally the other way around, swapped
# so the status row's own underline sits on the
# boundary between scrollback and the pinned UI, where it actually
# reads as a divider. On the terminal's true last row (the old order)
# there was nothing below the rule to separate, so it was rendered but
# pointless.
# At least 3 rows are needed for that split to mean anything at
# all: >=1 row of actual scrolling content, plus the two reserved ones.
# Below this, both pinned rows are skipped entirely and chat behaves
# exactly as it did before either feature existed: plain, unconfined
# scrolling, no pinned input either. One combined gate, not two
# independent minimums -- there's no sensible state where one pinned
# row exists without the other (design doc). A client can
# report an arbitrarily small height (`netbbs.net.session.
# clamp_terminal_size`'s own floor is 1, not a sane minimum), so this
# has to be a real runtime check, not an assumption.
_PINNED_UI_MIN_HEIGHT = 3

# Design doc: shown before the pinned input row's in-progress
# text, now that a full redraw happens on every update anyway -- makes
# it immediately clear "this row is for typing," distinct from the
# scrolling chat content above it. There was no equivalent before the
# input row was pinned (a bare cursor wherever output last left it was
# enough when input and output shared one stream).
_INPUT_PROMPT = "> "


def _own_channel_privileges(db: Database, channel: Channel, user: User) -> str | None:
    """A compact label for whatever moderator-level access `user` holds
    on `channel` -- `None` if none. A SysOp collapses to `"sysop"`
    rather than enumerating every individual bit (`has_permission`
    always passes a SysOp regardless of any actual grant row, so
    listing them out would be misleading busywork, not information);
    otherwise each `ChannelPermission` bit `user` actually holds
    (including via a local-blanket grant -- `has_permission`, not the
    literal-grant-only `get_grant`, is what correctly folds that in)
    gets its own short label."""
    if meets_level(user, SYSOP_LEVEL):
        return "sysop"
    labels = [
        label
        for permission, label in (
            (ChannelPermission.MODERATE, "mod"),
            (ChannelPermission.EDIT, "edit"),
            (ChannelPermission.MANAGE_MEMBERS, "members"),
        )
        if has_permission(db, user, object_type="channel", object_id=channel.id, permission=permission)
    ]
    return ",".join(labels) if labels else None


def _check_mute(db: Database, channel: Channel, user: User) -> str | None:
    """`None` if `user` isn't currently muted in `channel`, else the
    human-readable "indefinitely"/"until ..." text -- bundles the
    restriction check and its expiry formatting into one function so
    every call site (`_chat_loop`'s send-path checks, `_handle_me`) is
    a single `lane.run` round trip, not two."""
    restriction = is_muted(db, channel, user)
    if restriction is None:
        return None
    return "indefinitely" if restriction.expires_at is None else f"until {format_for_display(restriction.expires_at, db)}"


def _check_ban(db: Database, channel: Channel, user: User) -> str | None:
    """Same shape as `_check_mute`, for `_chat_loop`'s entry-time ban
    check."""
    restriction = is_banned(db, channel, user)
    if restriction is None:
        return None
    return "indefinitely" if restriction.expires_at is None else f"until {format_for_display(restriction.expires_at, db)}"


@dataclass
class _StatusSpan:
    """One colored run of text within a status-line field group -- e.g.
    the identity group splits into a bold `SELF_COLOR` "alice" span
    (bold, not a "you:" label -- `SELF_COLOR` already reads as "this is
    you" on its own), an optional `NICK_COLOR` "(nick)" span, and a
    `MUTED_COLOR` "[mod]" indicator span, concatenated with no gap
    between them so they read as one field while still each getting
    their own color."""

    text: str
    fg_color: int | None = None
    bold: bool = False


# A "group" is one status-line field: a list of `_StatusSpan`s
# concatenated with no separator (see `_StatusSpan`'s own docstring for
# why a field can be more than one span). `_compose_status_line` joins
# *groups* with `_STATUS_SEPARATOR` and can drop a whole group from the
# right if the terminal is too narrow -- never splits one mid-span.
_StatusGroup = list[_StatusSpan]

_STATUS_SEPARATOR = " | "

# Characters of the channel *name* itself shown before truncating --
# not counting the "#" prefix, the "..." marker, or the "[type]" tag
# that follows. An overeager channel name was the actual root cause
# observed in practice behind identity/topic/clock getting squeezed off
# the status line entirely on an ordinary 80-column terminal (Thiesi's
# own "Test!! Hier wird getestet" channel), not just a cosmetic nicety.
_CHANNEL_NAME_DISPLAY_LIMIT = 24


def _channel_name_spans(name: str) -> list[_StatusSpan]:
    """The channel-name portion of the status line's channel group,
    truncating a name over `_CHANNEL_NAME_DISPLAY_LIMIT` and marking the
    cut with a separately `MUTED_COLOR`-ed "..." -- the same "this is
    the display adding something, not part of the real content" color
    every other status-line decoration already uses (the mute/away
    parenthetical text, the online-count's own connective words), so it
    reads unambiguously as a shortening marker rather than three literal
    dots someone typed into the channel's actual name."""
    sanitized = sanitize_text(name)
    if len(sanitized) <= _CHANNEL_NAME_DISPLAY_LIMIT:
        return [_StatusSpan(f"#{sanitized}", fg_color=ACCENT_COLOR, bold=True)]
    return [
        _StatusSpan(f"#{sanitized[:_CHANNEL_NAME_DISPLAY_LIMIT]}", fg_color=ACCENT_COLOR, bold=True),
        _StatusSpan("...", fg_color=MUTED_COLOR),
    ]


def _render_chat_status_line(
    db: Database, hub: ChatHub, presence: PresenceRegistry, channel: Channel, user: User
) -> list[_StatusGroup]:
    """
    The status line's content, as an ordered list of colored field
    groups (composed/truncated/backgrounded by the caller). Each field
    gets its own distinct foreground color rather than sharing one --
    the channel name, its `[pub]`/`[invite]`/`[hidden]` type, the topic,
    and this user's own moderator/SysOp badge are all visually distinct
    categories, not one undifferentiated run of text.

    Groups are ordered most- to least-important on purpose, not
    alphabetically or by "how it's stored": `_compose_status_line` (the
    caller) drops whole groups from the *right* on a too-narrow
    terminal, so whichever group is listed last here is what silently
    disappears first -- the clock, then this user's own identity/
    privileges/mute state, then the topic, while the channel name and
    live counts always survive. Away state is deliberately *not* one of
    these rendered indicator groups -- `_repaint_status_line` gives the
    whole composed row a solid background band by default and drops it
    specifically when the viewer is away (a deliberately quieter,
    background-less look for "I've stepped back"), so there is nothing
    here to drop or truncate for it. Deliberately still *not* included:
    any per-channel "linked vs. local" origin -- that distinction
    doesn't exist anywhere in the schema yet (NetBBS Link is Phase 3,
    still private/experimental federation), so there is nothing real to
    render.

    The clock forces a bare `%H:%M` (`override_format`), not the
    node-configured display format `format_for_display` would otherwise
    use elsewhere (e.g. scrollback timestamps) -- that format normally
    includes the full date, sensible for a timestamp attached to one
    specific past event, but wasted width on a bar that's redrawn
    continuously and only ever shows *right now*. Still honors the
    node's configured timezone, which `override_format` alone doesn't
    affect (see that parameter's own resolution order).

    Dispatched as one bundled `lane.run` call by `_repaint_status_line`
    -- this whole function runs on the lane's worker
    thread, so every read here (`get_nick`, `_own_channel_privileges`,
    `is_muted`, `format_for_display`) stays a plain synchronous call.

    Re-fetches `channel` fresh via `get_channel_by_name` rather than
    trusting the frozen snapshot passed in -- the same reasoning
    `_meets_live_participation_requirements` already applies, and
    exactly the gap its own docstring flags ("not just showing a stale
    topic/description"): `_chat_loop` holds one `channel` snapshot for
    the session's whole lifetime, so without this, a topic changed via
    `/topic` mid-session would never appear here until the channel was
    re-joined, even though `/topic` with no arguments (which already
    re-fetches) shows it correctly right away.
    """
    channel = get_channel_by_name(db, channel.name)
    channel_type = "invite" if channel.members_only else ("hidden" if channel.hidden else "pub")

    roster = _roster_usernames(hub, channel)
    online_count = hub.participant_count(channel.name)
    away_count = sum(1 for username in roster if presence.is_away(username))

    groups: list[_StatusGroup] = [
        [
            *_channel_name_spans(channel.name),
            _StatusSpan(f"[{channel_type}]", fg_color=CHANNEL_TYPE_COLOR, bold=True),
        ],
        [
            _StatusSpan(str(online_count), fg_color=HEADER_COLOR, bold=True),
            _StatusSpan(" online (", fg_color=MUTED_COLOR),
            _StatusSpan(str(away_count), fg_color=HEADER_COLOR, bold=True),
            _StatusSpan(" away)", fg_color=MUTED_COLOR),
        ],
    ]

    if channel.topic:
        groups.append([_StatusSpan(f'"{sanitize_text(channel.topic)}"', fg_color=TOPIC_COLOR, bold=True)])

    nick = get_nick(db, user)
    identity: _StatusGroup = [_StatusSpan(sanitize_text(user.username), fg_color=SELF_COLOR, bold=True)]
    if nick:
        identity.append(_StatusSpan(f"({sanitize_text(nick)})", fg_color=NICK_COLOR))

    privileges = _own_channel_privileges(db, channel, user)
    if privileges is not None:
        identity.append(_StatusSpan(f"[{privileges}]", fg_color=PRIVILEGE_COLOR, bold=True))

    until = _check_mute(db, channel, user)
    if until is not None:
        label = "muted" if until == "indefinitely" else f"muted {until}"
        identity.append(_StatusSpan(f"[{label}]", fg_color=MUTED_COLOR))
    groups.append(identity)

    groups.append(
        [_StatusSpan(format_for_display(utc_now_iso(), db, override_format="%H:%M"), fg_color=MUTED_COLOR)]
    )
    return groups


def _compose_status_line(groups: list[_StatusGroup], width: int, *, active: bool = False) -> str:
    """
    Joins `groups` with a dim `_STATUS_SEPARATOR`, dropping whole groups
    from the right when the terminal is too narrow to fit them all --
    the same "least-important field disappears first" priority order
    `_render_chat_status_line`'s own field ordering establishes, now
    applied per-group instead of a raw character `truncate()`, which
    could land mid-field. A group that doesn't fit in full first falls
    back to just its own first span (a bare channel name, a bare
    username) before being dropped outright -- see the fallback branch
    below for why.

    `active` -- set by `_repaint_status_line` from the viewer's own away
    state, not a field this function decides on its own -- controls the
    row's whole look, not any one field's color (Thiesi's own explicit
    choice: literal per-field foreground colors, one flat background for
    the whole bar, not reverse video swapping fg/bg per span):

    - active (the default look, i.e. not away): every span keeps its own
      `fg_color` and additionally gets `STATUS_BAR_BACKGROUND` as a
      shared `bg_color`, including the separators and the trailing
      padding, so the whole row reads as one continuous colored band —
      no underline, since the background band itself already marks the
      row's boundary.
    - not active (away): no background at all -- literal per-field
      foreground color on the terminal's own default background,
      substituting a continuous underline (again spanning separators and
      padding) as the row's boundary marker instead, since there's no
      background band to serve that purpose. A deliberately quieter look
      standing in for an inline `[away]` tag, so there's nothing further
      to render or drop for it in the field groups themselves.
    """
    background = STATUS_BAR_BACKGROUND if active else None
    underline = not active

    kept: list[_StatusGroup] = []
    running = 0
    for group in groups:
        sep_len = len(_STATUS_SEPARATOR) if kept else 0
        text = "".join(span.text for span in group)
        if running + sep_len + len(text) <= width:
            running += sep_len + len(text)
            kept.append(group)
            continue

        # The full group doesn't fit. For a multi-span group, its first
        # span alone is still meaningful on its own -- a bare channel
        # name without its "[pub]" tag, a bare username without a nick
        # or mod badge -- unlike the group as a whole, which was built
        # to read as one unit. Try that core span before dropping the
        # group entirely: a user's own identity in particular must
        # never vanish from their own status bar just because a long
        # nickname or badge pushed the line over budget -- reported by
        # Thiesi as "setting a nickname made my name disappear entirely".
        if len(group) > 1:
            core_text = group[0].text
            if running + sep_len + len(core_text) <= width:
                running += sep_len + len(core_text)
                kept.append(group[:1])
                continue

        break

    if not kept:
        # Not even the first (most important) group fits -- fall back to
        # a raw character truncation of just that one.
        only = truncate("".join(span.text for span in groups[0]), width) if groups else ""
        return colored(only, bg_color=background, underline=underline)

    rendered: list[str] = []
    for index, group in enumerate(kept):
        if index > 0:
            rendered.append(
                colored(_STATUS_SEPARATOR, fg_color=MUTED_COLOR, bg_color=background, underline=underline)
            )
        for span in group:
            rendered.append(
                colored(span.text, fg_color=span.fg_color, bg_color=background, bold=span.bold, underline=underline)
            )
    pad = width - running
    if pad > 0:
        rendered.append(colored(" " * pad, bg_color=background, underline=underline))
    return "".join(rendered)


async def _repaint_status_line(
    session: Session, lane: DatabaseLane, hub: ChatHub, presence: PresenceRegistry, channel: Channel, user: User
) -> None:
    """
    Redraws the pinned status row in place, leaving the user's own
    in-progress input line untouched (design doc).

    Re-reads `session.terminal_height` and re-issues `set_scroll_region`
    on every call, not just once at entry -- the cheapest way to stay
    correct across a mid-session resize (Telnet NAWS/SSH PTY-resize/web
    `resize` all update `terminal_height` live, but passively, with no
    event this function could otherwise hook) without a dedicated
    resize-notification mechanism, which doesn't exist anywhere in this
    codebase yet. Re-sending an unchanged region is harmless. Falls
    back to doing nothing if the terminal is too short for the pinned
    UI to make sense (`_PINNED_UI_MIN_HEIGHT`) -- matches whatever
    `_chat_loop` itself decided at entry, recomputed fresh here in case
    a resize crossed that threshold mid-session.

    `save_cursor`/`restore_cursor`, not a remembered logical position:
    the user's own terminal already knows exactly where its cursor is
    mid-line-edit (including any client-side echo/cursor movement this
    server-side code has no visibility into) — asking the terminal
    itself to remember and restore it is the only way to guarantee this
    doesn't disturb in-progress typing, regardless of what transport or
    client is on the other end. Every call site (design doc)
    already holds `_chat_loop`'s shared write lock before calling this
    -- this function doesn't acquire it itself, the same way it never
    needed to before that lock existed, since `save_cursor`/
    `restore_cursor` alone was already sufficient in isolation; the
    lock's job is only to stop this call's own sequence of writes from
    interleaving with some *other* concurrent write, not to protect
    this function against itself.

    `lane`, not `db` (per Thiesi's explicit choice to
    migrate this hot, cosmetic, repainted-after-nearly-every-message
    path fully rather than leave it on direct `db` access): one
    `lane.run` call renders the whole line's fields on the worker
    thread (`_render_chat_status_line`), then composing/coloring/
    truncating and the actual terminal write stay plain, synchronous,
    non-`db`-touching code here, same split as `_resolve_target`/
    `_write_vcard_detail`.

    Gives the row its solid background band by default, dropping it when
    `user` is currently away (`presence.is_away`) instead of rendering a
    separate `[away]` tag inline -- read directly off `presence` here, a
    plain in-memory lookup, rather than threading it through the
    `lane.run` call, since it's not a `db` read at all. Tied specifically
    to the viewer's own away state, the same as every other "own state"
    field `_render_chat_status_line` already renders -- other
    participants' away state only ever shows up folded into the online/
    away counts.
    """
    height = session.terminal_height
    if height < _PINNED_UI_MIN_HEIGHT:
        return
    groups = await lane.run(_render_chat_status_line, hub, presence, channel, user)
    line = _compose_status_line(groups, session.terminal_width, active=not presence.is_away(user.username))
    await session.write(
        save_cursor()
        + set_scroll_region(1, height - 2)
        + move_cursor(height - 1, 1)
        + clear_line()
        + line
        + restore_cursor()
    )


async def _repaint_input_row(session: Session, live_buffer: LiveInputBuffer, height: int) -> None:
    """
    Redraws the pinned input row in place from `live_buffer`'s current
    text/cursor (design doc). Unlike `_repaint_status_line`
    (which jumps away and back via save/restore-cursor), this leaves
    the cursor sitting exactly here afterward -- the input row *is*
    its destination, not somewhere to return from, so there's nothing
    to restore. The next keystroke's own relative-movement echo
    (`netbbs.net.char_input`) builds on exactly this resting position.

    Re-issues `set_scroll_region` on every call, matching
    `_repaint_status_line`'s own resize-robustness reasoning. Caller is
    responsible for holding `_chat_loop`'s shared write lock, same as
    every other pinned-row write in this module (see
    `_repaint_status_line`'s own docstring).

    A very long in-progress line is truncated (not horizontally
    scrolled) to fit the terminal width -- an accepted simplification
    for what's expected to be a rare case (design doc); the
    cursor is left at the end of the truncated view rather than at its
    true, possibly-invisible position when that happens.

    A no-op if `height` is below `_PINNED_UI_MIN_HEIGHT` (GitHub issue
    #46) -- a defensive backstop against constructing an impossible
    (`height - 2 <= 0`) scroll region, on top of `_chat_loop`'s own
    `_PinnedUIState` already refusing to reach this call at all once a
    resize crosses that threshold.
    """
    if height < _PINNED_UI_MIN_HEIGHT:
        return
    scroll_bottom = height - 2
    input_row = height
    full_text = _INPUT_PROMPT + live_buffer.text
    displayed = truncate(full_text, session.terminal_width)
    await session.write(
        set_scroll_region(1, scroll_bottom) + move_cursor(input_row, 1) + clear_line() + displayed
    )
    if displayed == full_text:
        trailing = len(live_buffer.text) - live_buffer.cursor
        if trailing > 0:
            await session.write(relative_move_cursor(trailing, forward=False))


async def _print_and_redraw_input(
    session: Session, text: str, live_buffer: LiveInputBuffer, height: int
) -> None:
    """
    Print `text` as a new line into the scrolling content region, then
    redraw the pinned input row immediately below it (design doc).
    Used by every write that happens while a pinned input row
    exists: an incoming broadcast (`receive_loop`'s `deliver` closure),
    and this session's own command/message output (`send_loop`, via
    `_enter_content_region` beforehand, right after each `read_line()`
    call returns). Caller is responsible for holding `_chat_loop`'s
    shared write lock.

    Positioning is unconditional -- always jump to the scroll region's
    bottom row before printing, rather than tracking incrementally how
    "full" the region currently is (no such tracking exists anywhere
    else in this codebase either, and none is needed: DECSTBM's own
    auto-scroll-at-the-bottom-margin behavior handles it). This
    produces newest-content-adjacent-to-the-input-box behavior, which
    is what's actually expected once the input row is a fixed anchor
    (design doc's own note on why this isn't a regression from
    the old grows-from-the-top behavior, just a different, arguably
    more correct one now that there's a fixed anchor to grow from).

    A no-op if `height` is below `_PINNED_UI_MIN_HEIGHT` (GitHub issue
    #46) -- same defensive backstop as `_repaint_input_row`.
    """
    if height < _PINNED_UI_MIN_HEIGHT:
        return
    scroll_bottom = height - 2
    await session.write(set_scroll_region(1, scroll_bottom) + move_cursor(scroll_bottom, 1) + text + "\r\n")
    await _repaint_input_row(session, live_buffer, height)


async def _print_candidates_and_redraw_input(
    session: Session, live_buffer: LiveInputBuffer, height: int,
    candidates: Sequence[str], line_text: str, cursor: int,
) -> None:
    """
    `apply_tab_completion`'s `list_candidates` hook (design doc)
    for chat's pinned input row: prints the Tab-completion candidate
    list through the exact same "new line in the scrolling content
    region, then redraw the pinned input row" shape as everything else
    that prints while the pinned rows are active (`_print_and_redraw_
    input`, reused directly here), instead of `apply_tab_completion`'s
    own default fallback -- an unconditional `"\\r\\n"` that has no idea
    the terminal's cursor sits on the pinned input row, outside the
    scroll region, and would print the candidate list wherever that
    unconstrained newline happens to land instead of scrolling normally
    within the content region above.

    `live_buffer` is updated with the completion's own already-applied
    result *before* `_print_and_redraw_input` reads it to redraw the
    input row -- `_read_line_editable`'s own per-keystroke update to
    `live_buffer` only happens once this whole Tab keypress finishes
    handling, after this hook has already returned, so without this the
    redraw would show the in-progress text from *before* the completion
    ran.
    """
    live_buffer.update(list(line_text), cursor)
    await _print_and_redraw_input(session, "  ".join(candidates), live_buffer, height)


async def _enter_content_region(session: Session, height: int) -> None:
    """
    Repositions the cursor into the scrolling content region's bottom
    row, ready for ordinary `write_line` calls (every existing chat
    command handler's own output) to print and
    auto-scroll correctly (design doc). Needed because the
    cursor is otherwise sitting on the pinned input row -- outside the
    scroll region -- immediately after `read_line()` returns; without
    this, the first line a command handler writes would land in the
    wrong place (potentially right on top of the pinned input or status
    row, since the *cursor's* position, not the scroll region alone,
    is what an ordinary newline-driven write respects). Caller is
    responsible for holding `_chat_loop`'s shared write lock.

    A no-op if `height` is below `_PINNED_UI_MIN_HEIGHT` (GitHub issue
    #46) -- same defensive backstop as `_repaint_input_row`.
    """
    if height < _PINNED_UI_MIN_HEIGHT:
        return
    scroll_bottom = height - 2
    await session.write(set_scroll_region(1, scroll_bottom) + move_cursor(scroll_bottom, 1))


@dataclass
class _PinnedUIState:
    """
    Whether the pinned status/input rows are currently active for one
    chat session (GitHub issue #46).

    `_chat_loop` used to decide this once, at channel entry, and trust
    that decision for the rest of the session -- but Telnet NAWS, SSH
    PTY resize, and the web transport's `resize` event can all update
    `session.terminal_height` at any point afterward, and the pinned-
    row helpers (`_repaint_input_row`/`_print_and_redraw_input`/
    `_enter_content_region`) construct a scroll region from whatever
    height they're handed with no validation of their own -- shrinking
    below `_PINNED_UI_MIN_HEIGHT` mid-session made `height - 2` reach
    zero or negative, and `netbbs.rendering.set_scroll_region` raises
    `ValueError` on an invalid region, terminating the session.

    `sync()` re-derives the current answer from `session.terminal_height`
    every time it's called and performs whichever one-time transition
    is needed the moment the threshold is crossed -- normal-to-too-short
    hands the whole screen back before any helper above would compute
    an impossible region; too-short-to-normal re-establishes the region
    and repaints both pinned rows from scratch, since nothing has kept
    them up to date while inactive. Every call site that used to read a
    fixed `pinned_ui_enabled` local now calls `sync()` instead, always
    under `_chat_loop`'s shared lock -- both because the transition's
    own writes must never interleave with a concurrent one, and because
    `receive_loop`/`send_loop` must always agree on which regime is
    currently active rather than each independently guessing from a
    possibly-stale read of `terminal_height`.
    """

    active: bool

    async def sync(
        self,
        session: Session,
        lane: DatabaseLane,
        hub: ChatHub,
        presence: PresenceRegistry,
        channel: Channel,
        user: User,
        live_buffer: LiveInputBuffer,
    ) -> bool:
        now_active = session.terminal_height >= _PINNED_UI_MIN_HEIGHT
        if now_active != self.active:
            if now_active:
                await session.write(
                    clear_screen() + set_scroll_region(1, session.terminal_height - 2)
                )
                await _repaint_status_line(session, lane, hub, presence, channel, user)
                await _repaint_input_row(session, live_buffer, session.terminal_height)
            else:
                await session.write(reset_scroll_region() + clear_screen())
            self.active = now_active
        return self.active


def _seconds_until_next_minute(current: datetime.datetime) -> float:
    """Seconds remaining until `current`'s wall-clock minute rolls
    over -- `_clock_loop`'s own scheduling math, factored out as a pure
    function the same way `netbbs.net.daybreak._seconds_until_next_
    local_midnight` is, so the arithmetic is directly testable without
    driving the loop itself. No timezone conversion needed here (unlike
    daybreak's midnight math): every real IANA zone's UTC offset is a
    whole number of minutes, so the instant a minute rolls over is the
    same regardless of which zone the status line happens to be
    displaying it in -- only the displayed digits differ, not the
    timing."""
    return 60 - current.second - current.microsecond / 1_000_000


async def _clock_loop(
    session: Session,
    lane: DatabaseLane,
    hub: ChatHub,
    presence: PresenceRegistry,
    channel: Channel,
    user: User,
    lock: asyncio.Lock,
    *,
    now: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(datetime.timezone.utc),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """
    Repaints the pinned status row once a minute, aligned to the real
    wall-clock minute boundary -- without this, the status line's own
    clock (and everything else in it) only ever updates as a side
    effect of some other event happening (a message, a command), so an
    otherwise-idle session could sit showing a minute-stale clock
    indefinitely. Sleeping a flat 60s from whichever moment this task
    happened to start would drift against the real boundary and could
    show a stale minute for up to 59 seconds after it actually ticked
    over; `_seconds_until_next_minute` keeps every wakeup landing right
    on it instead.

    `now`/`sleep` are injectable so a test can drive this without a real
    wait -- the same dependency-injection shape `run_daybreak_announcer`
    already uses, for the identical reason.

    Runs as `_chat_loop`'s own third task (`clock_task`), deliberately
    not folded into `receive_loop`/`send_loop` -- neither of those ever
    runs unconditionally on a timer, and layering a sleep-based tick
    into either would mean it only fires between other work rather than
    on a true schedule. Also deliberately excluded from `_chat_loop`'s
    `asyncio.wait(..., return_when=FIRST_COMPLETED)`: this loop never
    finishes on its own, so it must never be mistaken for "the user
    quit" or "the connection dropped" the way `receive_task`/`send_task`
    completing does -- `_chat_loop` cancels it alongside them on every
    exit path instead, same as any other owned background task.
    """
    while True:
        await sleep(_seconds_until_next_minute(now()))
        async with lock:
            await _repaint_status_line(session, lane, hub, presence, channel, user)


async def _chat_loop(
    session: Session,
    lane: DatabaseLane,
    hub: ChatHub,
    presence: PresenceRegistry,
    mailbox: MessageMailbox,
    history: InputHistory,
    channel: Channel,
    user: User,
    *,
    session_registry: ActiveSessionRegistry | None = None,
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
    under either task. A third task, `clock_loop`, repaints the pinned
    status row once a minute so its clock keeps advancing even in an
    idle session — deliberately not part of that same stop-on-first-
    finish pair, since it never legitimately "finishes" on its own (see
    its own docstring).

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
    — design doc) landed later still, once retyping a
    long `/mute`/`/ban` reason from scratch each time turned out to be
    genuinely painful in practice. An incoming message can still land
    mid-typing and interleave visually with a user's own in-progress
    line, same as classic line-mode chat tools (Unix `talk`, `wall`)
    always had — that's a receive-vs-send-task race (see below), a
    different problem from line editing, and still out of scope.

    Scrollback (design doc) is replayed here, before the
    "Joined" line, using whatever was persisted *before* this join —
    this join's own event is recorded immediately after, so it's part of
    the next person's replay, not this one's.

    Checked once, here, before doing anything else (design doc §13):
    an unexpired ban means the user never enters
    the loop at all. Mute has no equivalent join-time check — a muted
    user can still read, just not send (enforced in `send_loop`).

    A pinned status row, and a pinned input row just below it (design
    doc; see `_repaint_status_line`/
    `_repaint_input_row`), occupy the terminal's last two lines for the
    duration of the session, via a VT100 scroll region — everything
    above them, including the scrollback replay and "Joined" line
    below, scrolls normally within the shrunk region. Setting that
    region moves the real terminal cursor to its home position as an
    unavoidable side effect of the escape sequence itself, so entry
    clears the screen first — a deliberate, visible transition into
    chat, not previously true (chat used to just continue printing
    inline below whatever screen preceded it). Skipped entirely on a
    too-short terminal (`_PINNED_UI_MIN_HEIGHT`), which degrades to
    exactly the old, unconfined scrolling behavior with no pinned input
    row either.

    `live_buffer`/`lock` (design doc) are constructed
    unconditionally, even when the pinned UI itself is disabled for
    this session -- passed to every `read_line()` call regardless, so
    `receive_loop`'s own conditional logic doesn't need a second,
    parallel "were these even created" check. When the pinned UI is
    off, `lock` is simply never contended (`receive_loop` never
    acquires it either in that case), so this costs nothing.

    `lane`, not `db`: both `send_loop`/`receive_loop`
    dispatch concurrently through this same lane -- safe by
    construction, the identical property that already lets two
    different *sessions* share one lane (`DatabaseLane`'s own single
    worker thread plus bounded semaphore backpressure doesn't
    distinguish which coroutine submitted a job).
    """
    until = await lane.run(_check_ban, channel, user)
    if until is not None:
        await session.write_line(
            colored(f"\r\nYou are banned from this channel ({until}).", fg_color=MUTED_COLOR)
        )
        return _Quit()

    participant_id = ParticipantId(username=user.username, session_key=id(session))
    queue = hub.join(channel.name, participant_id)
    # This try starts immediately after hub.join(), not
    # just around the receive/send-task wait below -- scrollback replay
    # and the initial join broadcast/status-line paint between here and
    # task creation cross real lane.run() round trips, so a
    # cancellation landing in that window needs the same finally-block
    # hub.leave() cleanup as one landing in the wait() below (that
    # window is not zero-width: real await points exist there, not just
    # synchronous db calls).
    try:
        pinned_ui_enabled = session.terminal_height >= _PINNED_UI_MIN_HEIGHT
        live_buffer = LiveInputBuffer()
        lock = asyncio.Lock()
        # GitHub issue #46: `pinned_ui` tracks this dynamically for the rest
        # of the session (see `_PinnedUIState`) -- constructed directly from
        # the boolean just computed, not via `sync()`, since entry's own
        # setup immediately below already does the equivalent one-time
        # activation work itself (and repainting the status/input rows here
        # would be premature -- scrollback and the join notice haven't been
        # written yet).
        pinned_ui = _PinnedUIState(active=pinned_ui_enabled)
        if pinned_ui_enabled:
            await session.write(clear_screen() + set_scroll_region(1, session.terminal_height - 2))

        channel_label = colored(f"#{sanitize_text(channel.name)}", fg_color=ACCENT_COLOR, bold=True)
        quit_hint = menu_key("/quit", " to leave")

        scrollback = await lane.run(get_scrollback, channel)
        if scrollback:
            rendered_scrollback = await lane.run(_render_all_scrollback, channel, user, scrollback)
            await session.write_line(colored("--- scrollback ---", fg_color=MUTED_COLOR))
            for line in rendered_scrollback:
                await session.write_line(line)
            # Even bounded persistence is a different
            # promise than pure ephemeral chat — worth surfacing explicitly
            # rather than leaving as an internal implementation detail.
            await session.write_line(
                colored(
                    f"--- end scrollback (last {len(rendered_scrollback)} events retained) ---",
                    fg_color=MUTED_COLOR,
                )
            )
            # Issue #56: viewing this channel's scrollback advances
            # user's read cursor to whatever is now newest on screen --
            # scrollback is oldest-first (get_scrollback's own contract),
            # so the last element is the newest.
            await lane.run(record_channel_seen, user, channel, scrollback[-1])

        await session.write_line(f"\r\nJoined {channel_label}. Type {quit_hint}.")
        # author_label is stored raw here (user.username, not a sanitized/
        # alias-aware label) -- sanitize on output, not on storage, per
        # sanitize_text's docstring; only the rendered copy each recipient's
        # receive_loop produces is actually shown to a terminal (GitHub
        # issue #64) -- the recorded ChannelMessage itself is
        # broadcast, not a pre-rendered string, so live and scrollback
        # replay always agree on how to render it (see
        # _render_channel_message).
        recorded_join = await lane.run(
            record_message, channel, kind="join", author_label=user.username, author_fingerprint=user.fingerprint
        )
        await hub.broadcast(channel.name, recorded_join, exclude={participant_id})
        if pinned_ui_enabled:
            await _repaint_status_line(session, lane, hub, presence, channel, user)
            # Neither task is running yet (both are created below, after
            # this initial setup completes) -- no concurrent writer exists
            # at this exact point, so this first paint needs no lock.
            await _repaint_input_row(session, live_buffer, session.terminal_height)

        async def deliver(text: str, *, repaint_status: bool = False) -> None:
            """
            Routes through the pinned-row-aware print-and-redraw path
            when the pinned UI is active (under `lock`, so this can
            never interleave with `send_loop` mid-keystroke or
            mid-dispatch -- see `_repaint_status_line`'s docstring), or
            falls straight back to a plain `write_line` when it isn't (a
            too-short terminal, exactly the old, unconfined-scrolling
            behavior).

            Always acquires `lock` first, then re-derives whether the
            pinned UI is *currently* active via `pinned_ui.sync()`
            (GitHub issue #46) -- a resize can flip this at any moment
            between two deliveries, and `sync()` itself needs the lock
            held to perform a threshold-crossing transition atomically
            with respect to `send_loop`.

            Used two ways: `receive_loop`'s own writes below (design doc),
            and installed as `session.pinned_notice_hook` so
            an out-of-band system notice (a node-shutdown broadcast,
            `netbbs.net.session_registry.ActiveSessionRegistry.
            broadcast_to_all`) reaches this session through the same
            safe path instead of a raw write that assumes a plain
            scrolling prompt -- see `Session.pinned_notice_hook`'s own
            docstring.
            """
            async with lock:
                if not await pinned_ui.sync(session, lane, hub, presence, channel, user, live_buffer):
                    await session.write_line(text)
                    return
                await _print_and_redraw_input(session, text, live_buffer, session.terminal_height)
                if repaint_status:
                    await _repaint_status_line(session, lane, hub, presence, channel, user)

        session.pinned_notice_hook = deliver

        async def receive_loop() -> None:
            while True:
                message = await queue.get()
                if isinstance(message, _KickNotice):
                    await deliver(
                        colored(f"\r\n*** You have been {message.reason} from this channel.", fg_color=MUTED_COLOR)
                    )
                    return
                if isinstance(message, ChannelMessage):
                    # GitHub issue #64: join/leave/message/action
                    # broadcasts carry the structured, persisted event
                    # itself rather than a pre-rendered string, so this is
                    # the same renderer scrollback replay uses
                    # (_render_channel_message) -- the two paths can no
                    # longer independently drift on whether a currently-
                    # verified real name is shown. repaint_status=True
                    # unconditionally, same as the _TimestampedNotice branch
                    # below does for these same event kinds
                    # (design doc) -- join/leave affect the
                    # participant count every repaint shows regardless.
                    rendered = await lane.run(_render_channel_message, channel, user, message, self_message=False)
                    await deliver(rendered, repaint_status=True)
                    continue
                if isinstance(message, _TimestampedNotice):
                    # The one remaining use of this wrapper now that
                    # join/leave/message/action are carried on ChannelMessage
                    # above: private (`/msg`/`/private`) message delivery via
                    # `send_to` (`_deliver_private_message`), which has no
                    # ChannelMessage to carry -- private conversations are
                    # deliberately not persisted (design doc). repaint_status=True here too since a private
                    # message can arrive while the status line is showing
                    # something now-stale (e.g. an away reminder).
                    rendered = await lane.run(format_with_preference, user, message.text, message.created_at)
                    await deliver(rendered, repaint_status=True)
                    continue
                if isinstance(message, QueueOverflowNotice):
                    # GitHub issue #31: this session's own queue overflowed
                    # (too far behind the channel's message rate) and one
                    # message was dropped to make room for this notice --
                    # an honest signal that something was missed, rather
                    # than silently losing it.
                    await deliver(
                        colored(
                            "\r\n*** You're falling behind -- a message was dropped.",
                            fg_color=MUTED_COLOR,
                        )
                    )
                    continue
                await deliver(message)

        async def send_loop() -> ChatAction | None:
            # Per-session private-conversation state (design doc): set by
            # `/private` (`_EnterPrivate`), cleared by `/close`
            # (`_ExitPrivate`). A plain local, not anything shared/global --
            # only this session's own next lines of ordinary input are
            # affected. While set, slash-commands still dispatch exactly as
            # normal (confirmed with Thiesi, matching the existing "leading /
            # is always a command attempt" rule) -- only *non-slash* lines change
            # meaning, routed to the private conversation instead of posted
            # to the channel.
            private_target: User | None = None

            async def list_candidates(candidates: Sequence[str], line_text: str, cursor: int) -> None:
                await _print_candidates_and_redraw_input(
                    session, live_buffer, session.terminal_height, candidates, line_text, cursor
                )

            while True:
                completer = await _build_completer(lane, hub, presence, channel, user)
                line = (
                    await session.read_line(
                        history=history, completer=completer, live_buffer=live_buffer, lock=lock,
                        list_candidates=list_candidates if pinned_ui.active else None,
                    )
                ).strip()

                # Everything from here to the next read_line() call is one
                # atomic critical section under `lock` (design doc)
                # -- entering the scroll region's content row up front, so
                # every existing write_line() call below (unchanged) prints
                # and auto-scrolls in the right place instead of landing on
                # the pinned input row it was just echoed on. `finally`
                # guarantees the input row gets redrawn (now-empty, ready
                # for the next line) before this iteration ends, regardless
                # of which of the several continue/return paths below fires
                # -- same reasoning as netbbs.net.char_input's own per-
                # keystroke `finally`, one level up: per *submitted line*
                # here, rather than per keystroke.
                #
                # `lock` is now taken unconditionally (GitHub issue #46),
                # not just when the pinned UI was active at channel entry --
                # `pinned_ui.sync()` needs it held to perform a threshold-
                # crossing transition atomically, and a resize can enable or
                # disable the pinned UI at any point during a long-running
                # session, not just decide it once up front.
                try:
                    async with lock:
                        pinned_ui_enabled = await pinned_ui.sync(
                            session, lane, hub, presence, channel, user, live_buffer
                        )
                        if pinned_ui_enabled:
                            await _enter_content_region(session, session.terminal_height)

                        if not await lane.run(account_still_active, user):
                            # GitHub issue #29 (reopened): the same cross-process
                            # revalidation netbbs.net.login_flow._main_menu already
                            # does at its own boundary -- a session that stays in
                            # chat (or any other long-running submenu) never
                            # returns to that menu to pick up a disable/delete made
                            # through a separate `python -m netbbs.admin`
                            # invocation, so this loop needs the identical check at
                            # its own equivalent boundary: every attempted message
                            # or command, before any of it is actually processed.
                            await session.write_line(
                                colored(
                                    "\r\nYour account is no longer active. Disconnecting.",
                                    fg_color=MUTED_COLOR,
                                )
                            )
                            return _Quit()

                        if not line:
                            continue
                        if line.startswith("/"):
                            ctx = ChatCommandContext(
                                session=session,
                                lane=lane,
                                hub=hub,
                                presence=presence,
                                mailbox=mailbox,
                                channel=channel,
                                user=user,
                                participant_id=participant_id,
                                session_registry=session_registry,
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
                            # A dispatched command may have changed status-line-
                            # relevant state of this user's own (/away, /nick) --
                            # repainting after every command, not just those two,
                            # is simpler than enumerating which ones matter and no
                            # more expensive (design doc).
                            if pinned_ui_enabled:
                                await _repaint_status_line(session, lane, hub, presence, channel, user)
                            continue

                        if private_target is not None:
                            ctx = ChatCommandContext(
                                session=session,
                                lane=lane,
                                hub=hub,
                                presence=presence,
                                mailbox=mailbox,
                                channel=channel,
                                user=user,
                                participant_id=participant_id,
                                session_registry=session_registry,
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

                        until = await lane.run(_check_mute, channel, user)
                        if until is not None:
                            await session.write_line(
                                colored(f"You are muted in this channel ({until}).", fg_color=MUTED_COLOR)
                            )
                            # The user may be learning they're muted right now, for
                            # the first time -- there's no live-push notice for a
                            # new mute the way kick/ban get one, so this rejection
                            # is the first opportunity to reflect it.
                            if pinned_ui_enabled:
                                await _repaint_status_line(session, lane, hub, presence, channel, user)
                            continue

                        # GitHub issue #64: re-checked here against
                        # the *current* channel/Community policy and the
                        # user's *current* attestation, not just at channel
                        # entry -- see _meets_live_participation_requirements.
                        if not await lane.run(_meets_live_participation_requirements, channel, user):
                            await session.write_line(colored(_NO_LONGER_QUALIFIES_MESSAGE, fg_color=MUTED_COLOR))
                            return _ToPicker()

                        # The sender gets a direct write with self_message=True
                        # (SELF_COLOR, so their own messages visually stand out
                        # from the rest of the conversation) while everyone else
                        # receives the ACCENT_COLOR-formatted version via the
                        # broadcast (sender excluded, since they already got
                        # their own copy directly) -- _render_channel_message
                        # composes the two independently per recipient rather
                        # than reusing one shared string, since it's genuinely
                        # different text per recipient (GitHub issue #64 point
                        # 4: never nest a second color inside `_chat_author_
                        # label`'s already-colored/reset segments).
                        #
                        # The broadcast payload is the recorded ChannelMessage
                        # itself, not a pre-rendered string -- receive_loop
                        # renders it the same way scrollback replay does
                        # (_render_channel_message), so live and replay can't
                        # independently drift on the gated-display rule.
                        # record_message stores the raw `line`, not a sanitized
                        # copy -- sanitize on output, not on storage.
                        recorded_message = await lane.run(
                            record_message,
                            channel,
                            kind="message",
                            author_label=user.username,
                            author_fingerprint=user.fingerprint,
                            body=line,
                        )
                        rendered_self = await lane.run(
                            _render_channel_message, channel, user, recorded_message, self_message=True
                        )
                        await session.write_line(rendered_self)
                        await hub.broadcast(channel.name, recorded_message, exclude={participant_id})
                        # Design doc: sending a message does not
                        # clear away state -- a user may intentionally remain away
                        # while briefly responding. Reminded, not silently changed.
                        if presence.is_away(user.username):
                            await session.write_line(
                                colored("(You are still marked away.)", fg_color=MUTED_COLOR)
                            )
                        if pinned_ui_enabled:
                            await _repaint_status_line(session, lane, hub, presence, channel, user)
                finally:
                    # Re-synced once more here, not just trusted from the
                    # top of this same iteration (GitHub issue #46) -- a
                    # resize could have crossed the threshold while this
                    # iteration's own body was busy (command dispatch, a
                    # broadcast, etc.), and this is the one place that
                    # decides whether the final per-iteration repaint below
                    # is even valid to attempt.
                    async with lock:
                        if await pinned_ui.sync(session, lane, hub, presence, channel, user, live_buffer):
                            await _repaint_input_row(session, live_buffer, session.terminal_height)

        receive_task = asyncio.create_task(receive_loop())
        send_task = asyncio.create_task(send_loop())
        clock_task = asyncio.create_task(_clock_loop(session, lane, hub, presence, channel, user, lock))

        try:
            done, pending = await asyncio.wait(
                {receive_task, send_task}, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            # This whole session task was itself cancelled from outside
            # (e.g. deliberate node shutdown, design doc's
            # ActiveSessionRegistry.disconnect_all()) -- asyncio.wait()
            # being cancelled does NOT cancel the tasks it was waiting
            # on, so without this, receive_task/send_task would be left
            # orphaned: still scheduled, with nothing left to await
            # their result. One of them then hits SessionClosedError
            # the moment the underlying socket actually closes, and
            # asyncio logs "Task exception was never retrieved" since
            # there's no one left to retrieve it (seen for real on
            # Thiesi's NetBSD box on Ctrl-C with a chat session open,
            # not just reasoned about). clock_task is included here too --
            # it's never part of the wait() set above (see its own
            # docstring for why), so it's just as orphaned by an external
            # cancellation as the other two would be without this.
            for task in (receive_task, send_task, clock_task):
                task.cancel()
            await asyncio.gather(receive_task, send_task, clock_task, return_exceptions=True)
            raise
        for task in pending:
            task.cancel()
        # Properly await cancelled tasks rather than fire-and-forget —
        # otherwise asyncio can warn "Task was destroyed but it is
        # pending" and the cancellation may not actually finish cleanly
        # before this function returns. clock_task is never in `pending`
        # (excluded from the wait() set above) so it needs its own
        # explicit cancel+gather here, on the normal (non-cancelled) exit
        # path, same reasoning as the CancelledError branch above.
        clock_task.cancel()
        await asyncio.gather(*pending, clock_task, return_exceptions=True)
        outcome: ChatAction | None = None
        for task in done:
            value = task.result()  # re-raise, e.g. SessionClosedError from a dropped connection
            if task is send_task:
                outcome = value
        # receive_task finishing (a kick/ban) has no ChatAction of its
        # own -- it always means "exit entirely," same as /quit.
        if receive_task in done and pinned_ui.active:
            # `receive_task` completing normally (not cancelled, and the
            # exception re-raise above already ruled out a dropped
            # connection) only ever happens via `_KickNotice`'s own
            # `return` -- receive_loop has no other path out of its
            # `while True`. The kick/ban notice was just printed above,
            # but the `finally` block below is about to reset the scroll
            # region and clear the screen unconditionally (so the next
            # screen doesn't inherit chat's shrunk region) -- without a
            # pause here, that clear fires before the user has had any
            # real chance to read why they were just removed. A genuine
            # keypress, not a timed sleep, so this doesn't race a slow
            # reader or make a fast one wait on nothing. Unpins the
            # scroll region first (the `finally` block below will
            # harmlessly re-send the same reset) so this prompt lands on
            # its own fresh line below the notice, not appended straight
            # onto the pinned input row's now-empty "> ".
            try:
                await session.write(
                    reset_scroll_region() + "\r\n" + colored("Press any key to continue...", fg_color=MUTED_COLOR)
                )
                await session.read_key()
            except SessionClosedError:
                pass
        return outcome or _Quit()
    finally:
        # Cleared unconditionally, even if `deliver` was never actually
        # installed (an exception before that point above) -- a no-op in
        # that case, but leaving a stale hook on `session` past this
        # chat session's own lifetime would let a later out-of-band
        # notice call back into closures capturing this session's own
        # now-defunct `lock`/`live_buffer`/`channel`/`user`.
        session.pinned_notice_hook = None
        # `pinned_ui.active` (GitHub issue #46), not the entry-time
        # `pinned_ui_enabled` local -- a resize during the session may
        # have changed which regime was actually active by the time
        # things unwound here, and `send_loop`'s own reassignment of
        # `pinned_ui_enabled` is local to that nested function, not
        # visible in this outer scope (`pinned_ui` is the one object
        # both closures actually share and keep in sync).
        if pinned_ui.active:
            # Best-effort: a session that's already gone (the common
            # reason this whole function is unwinding in the first
            # place) makes this write raise SessionClosedError, which
            # must not replace/mask whatever exception is already
            # propagating out of the try block above (design doc) --
            # there's nothing left to reset for a session that's
            # already disconnected anyway. Must happen before this
            # session moves on to any other screen (the main menu, the
            # channel picker) -- left active, every subsequent screen
            # would keep scrolling inside this same shrunk region.
            #
            # clear_screen() is bundled in here too (design doc
            # bugfix): neither the channel picker (`pick_item`,
            # reached via /leave) nor the main menu (`_draw_main_menu`,
            # reached via /quit) ever clear the screen themselves, so
            # without this the last screenful of chat stayed visible
            # until unrelated output happened to overwrite it. Entry
            # clears for the same reason (setting the scroll region
            # moves the cursor home as a side effect, see entry's own
            # comment) -- this is that same clear, mirrored on exit.
            try:
                await session.write(reset_scroll_region() + clear_screen())
            except SessionClosedError:
                pass
        hub.leave(channel.name, participant_id)
        recorded_leave = await lane.run(
            record_message, channel, kind="leave", author_label=user.username, author_fingerprint=user.fingerprint
        )
        await hub.broadcast(channel.name, recorded_leave, exclude={participant_id})
