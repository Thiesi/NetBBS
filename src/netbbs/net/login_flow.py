"""
Login flow and top-level main menu, tying a Session to the auth,
permissions, boards, chat, and rendering modules.

The main menu itself is intentionally minimal structurally — a plain
lettered loop, not a real menu-dispatch architecture. It exists now,
rather than staying purely linear the way the board-only version of this
file was, because there are genuinely two independent things to route
between (boards, chat) — adding real menu structure now that it's
actually needed is not the same as building it prematurely. Output now
uses the ANSI rendering framework (color, and reflow to each session's
actual detected terminal width) plus transport-independent character-
mode input; a future screen-buffer/diff ("TUI") abstraction for heavy
cursor-addressable screens is Phase 2 scope, alongside the fullscreen
editor that's the actual reason it's needed (design doc).
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import date
from enum import Enum, auto
from pathlib import Path

from netbbs.activity import (
    board_read_cursor,
    file_area_read_cursor,
    is_following,
    record_board_seen,
    unread_channel_count,
    unread_file_count,
    unread_post_count,
    unread_replies_to,
)
from netbbs.attestation import (
    AttestationError,
    ProfileFieldError,
    attest_age,
    attest_name,
    compute_age,
    format_name_for_resource,
    get_attestation,
    get_birthdate,
    get_display_name,
    get_location,
    is_birthdate_visible,
    is_display_name_visible,
    is_location_visible,
    is_verified_badge_visible,
    meets_age,
    meets_name_requirement,
    set_birthdate,
    set_birthdate_visible,
    set_display_name,
    set_display_name_visible,
    set_location,
    set_location_visible,
    set_attestation_link_visible,
    set_verified_badge_visible,
)
from netbbs.auth.users import (
    MIN_REGISTRATION_PASSWORD_LENGTH,
    NEW_ACCOUNT_SENTINEL,
    SYSOP_LEVEL,
    AuthError,
    User,
    account_still_active,
    authenticate_password_async,
    create_user_async,
    get_user_by_id,
    get_user_by_username,
    list_users,
    set_verify_key,
)
from netbbs.boards import (
    MAX_BODY_BYTES,
    Board,
    Post,
    PostError,
    PostPage,
    create_post,
    edit_post,
    list_boards,
    list_posts_page,
    tombstone_post,
)
from netbbs.boards.categories import Category, list_subcategories, list_top_level_categories
from netbbs.boards.categories import get_category_by_id as get_board_category_by_id
from netbbs.chat import (
    ChatHub,
    DirectChatInvites,
    MessageMailbox,
    PresenceRegistry,
    format_with_preference,
    list_pending_invitations_for_user,
)
from netbbs.chat.categories import get_category_by_id as get_channel_category_by_id
from netbbs.chat.channels import Channel
from netbbs.communities import (
    Community,
    get_community,
    get_effective_min_age,
    get_effective_min_read_level,
    get_effective_min_write_level,
    get_effective_name_requirement,
    list_communities,
)
from netbbs.config import RegistrationMode, get_node_display_name, get_registration_mode
from netbbs.directory import (
    MAX_BIO_BYTES,
    MAX_BIO_LINES,
    BioError,
    get_bio,
    get_vcard,
    has_bio,
    is_bio_visible,
    set_bio,
    set_bio_visible,
)
from netbbs.files.areas import FileArea, list_file_areas
from netbbs.files.categories import get_category_by_id as get_file_area_category_by_id
from netbbs.identity.keys import IdentityError, parse_verify_key
from netbbs.link.boards import (
    LinkContext,
    queue_board_post_edit_if_linked,
    queue_board_post_if_linked,
    queue_board_post_moderator_edit_if_linked,
    queue_board_post_tombstone_if_linked,
)
from netbbs.mail import unread_count as unread_mail_count
from netbbs.messaging_preferences import (
    accepts_direct_messages,
    set_accepts_direct_messages,
)
from netbbs.moderation import BoardPermission, has_permission, is_blocked
from netbbs.signature import (
    MAX_SIGNATURE_BYTES,
    MAX_SIGNATURE_LINES,
    SignatureError,
    append_signature,
    get_signature,
    set_signature,
)
from netbbs.net.admin_flow import admin_menu
from netbbs.net.char_input import REDRAW_KEY, InputHistory, reject_unhandled_key
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.chat_flow import (
    browse_channels,
    has_visible_channels,
    list_visible_channels_for,
    run_direct_chat_invite_flow,
    run_direct_chat_loop,
)
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled, set_breadcrumb_collapsed_enabled
from netbbs.net.color_depth_preference import color_depth_override, effective_truecolor, set_color_depth_override
from netbbs.net.node_theme import (
    effective_accent_color,
    effective_accent_color_256,
    effective_clock_color_256,
    effective_header_color,
    effective_header_color_256,
    effective_node_name_gradient,
)
from netbbs.net.menu_description_preference import menu_description_level, set_menu_description_level
from netbbs.net.redraw_preference import redraw_in_place_enabled, set_redraw_in_place_enabled
from netbbs.net.unicode_style_preference import (
    set_unicode_style_enabled,
    unicode_style_enabled,
    unicode_style_ever_set,
)
from netbbs.net.composition import ReviewAction, edit_line_body, review_composition
from netbbs.net.draft_storage import delete_draft, drafts_directory, load_draft
from netbbs.net.editor_preference import fullscreen_editor_enabled, set_fullscreen_editor_enabled
from netbbs.net.door_flow import browse_doors, has_visible_doors
from netbbs.net.board_list_banner import load_board_list_banner
from netbbs.net.file_flow import browse_file_areas, enter_file_area, has_visible_areas
from netbbs.net.logoff_banner import load_logoff_banner
from netbbs.net.mail_flow import browse_mail
from netbbs.net.main_menu_banner import load_main_menu_banner
from netbbs.net.maintenance import LOCKDOWN_MESSAGE, LOCKDOWN_NOTICE, MAINTENANCE_MESSAGE, MaintenanceMode
from netbbs.net.new_account_banner_after import load_new_account_banner_after
from netbbs.net.new_account_banner_before import load_new_account_banner_before
from netbbs.net.nodeconfig import ThrottleConfig
from netbbs.net.picker import pick_item
from netbbs.net.prose_editor import edit_prose
from netbbs.net.resource_editor import Draft, FieldSpec, edit_resource_draft, live_choice_field
from netbbs.net.session import Session, SessionClosedError
from netbbs.net.session_registry import ActiveSessionRegistry, SessionSummary
from netbbs.net.shutdown import NodeControls, SequenceScheduler, format_remaining_seconds
from netbbs.net.sort_ui import SORT_MODE_LABELS, prompt_sort_change
from netbbs.net.throttle import LoginThrottle
from netbbs.net.welcome_banner import load_welcome_banner
from netbbs.permissions import meets_level
from netbbs.rendering import (
    ALERT_COLOR,
    CLOCK_COLOR,
    ERROR_COLOR,
    HEADER_COLOR,
    LABEL_COLOR,
    METADATA_COLOR,
    MUTED_COLOR,
    SUCCESS_COLOR,
    VALUE_COLOR,
    WARNING_COLOR,
    MenuEntry,
    action_bar,
    badge,
    clear_screen,
    colored,
    colored_truncate,
    counts_row,
    double_frame,
    empty_state,
    field_row,
    menu_grid,
    menu_key,
    reflow,
    sanitize_text,
    screen_title,
    status_badge,
    truncate,
)
from netbbs.search import (
    ChannelMessageSearchHit,
    FileSearchHit,
    PostSearchHit,
    file_jump_cursor,
    post_jump_cursor,
    search_channel_messages,
    search_files,
    search_posts,
)
from netbbs.session_history import (
    SessionHistoryEntry,
    list_recent_sessions,
    record_session_end,
    record_session_start,
    session_history_name_visible,
    set_session_history_name_visible,
)
from netbbs.sort_preferences import (
    SortPreference,
    clear_sort_preference,
    get_effective_sort_mode,
    list_sort_preferences,
    set_sort_preference,
)
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display, utc_now_iso

_MAX_LOGIN_ATTEMPTS = 3
_MAX_PLAIN_POST_LINES = 200

# How often the background account-revocation watcher re-checks a live
# session's account (GitHub issue #29, reopened a second time). A fixed
# module constant, not node-configurable -- an internal responsiveness/
# DB-query-overhead tradeoff, not a policy an operator needs control
# over the way e.g. invitation expiry is. Short enough to feel prompt
# for a real disable/delete, cheap enough that one extra SELECT per
# live session per interval is a non-issue at this project's declared
# scale (§14, dozens to low hundreds of concurrent sessions).
_REVOCATION_CHECK_INTERVAL_SECONDS = 5.0

# Bounds the watcher's own "you're disconnected" notice (GitHub issue
# #29, reopened a third time) -- see _watch_for_account_revocation's
# docstring for why this can't be allowed to block indefinitely.
_REVOCATION_NOTICE_TIMEOUT_SECONDS = 1.0


class LoginOutcome(Enum):
    """Terminal outcomes from the interactive login flow."""

    ATTEMPTS_EXHAUSTED = auto()
    BLOCKED = auto()
    THROTTLED = auto()
    IDLE_TIMEOUT = auto()


async def _write_connection_notice(
    session: Session,
    db: Database | None,
    title: str,
    detail: str,
    *,
    tone: str = "warning",
) -> None:
    """Render a terminal connection state without changing its outcome.

    Unconditionally `unicode_style=True` (no `clear=`/ASCII-fallback
    split like most other screens): this fires pre-authentication, with
    no account/preference to look up yet, and NetBBS's Telnet transport
    already sends every screen as UTF-8 regardless of any preference
    (see `unicode_style_preference`'s own docstring) -- the same
    reasoning `welcome_banner`'s default banner already uses. `db` is
    read directly and synchronously here rather than via `lane.run`
    (issue #162's header-color sweep) -- every call site is a
    connection-lifecycle notice that fires before or around login, the
    same pre-auth timing `welcome_banner._default_welcome_banner`
    already reads `db` synchronously for.

    `db` is `Database | None`, not required, specifically for
    `handle_session`'s own maintenance-mode rejection: that check fires
    before anything else in the function and is deliberately tested
    (`test_shutdown.py`) to never dereference `db` at all, so a
    maintenance rejection still works cleanly even if the db connection
    itself is the thing in a bad state. `None` falls back to the bare
    `theme.HEADER_COLOR` constant instead of resolving an override.

    No `node_name_gradient=` either: `breadcrumb=()` means `screen_title`
    has no separate node-name segment to color here regardless (its own
    `len(segments) > 1` guard), so passing one through would be dead
    weight -- and doing so unconditionally would mean an unguarded
    `session.node_name_gradient` attribute access, unlike `width` just
    above, which already tolerates a minimal test double via `getattr`
    rather than assuming every field every real `Session` subclass
    carries. Some of this function's own real pre-login/minimal test
    callers (`test_shutdown.py`'s connection-notice fakes in particular)
    predate this field and aren't guaranteed to carry it."""
    width = getattr(session, "terminal_width", 80)
    header_color = effective_header_color_256(db) if db is not None else HEADER_COLOR
    await session.write_line(
        "\r\n"
        + screen_title(
            title,
            breadcrumb=(),
            width=width,
            unicode_style=True,
            header_color=header_color,
        )
    )
    await session.write_line(status_badge(title.upper(), tone=tone, unicode_style=True))
    await session.write_line(
        colored(reflow(detail, width=width), fg_color=METADATA_COLOR)
    )


async def handle_session(
    session: Session,
    db: Database,
    hub: ChatHub,
    presence: PresenceRegistry,
    mailbox: MessageMailbox,
    throttle: LoginThrottle,
    throttle_config: ThrottleConfig,
    session_registry: ActiveSessionRegistry,
    maintenance: MaintenanceMode,
    *,
    shutdown_event: asyncio.Event | None = None,
    graceful_delay_seconds: float = 60.0,
    drain_scheduler: SequenceScheduler | None = None,
    shutdown_scheduler: SequenceScheduler | None = None,
    lane: DatabaseLane | None = None,
    link_context: LinkContext | None = None,
    direct_invites: DirectChatInvites | None = None,
) -> None:
    """
    Top-level per-connection entry point.

    `lane` (design doc, issue #57, Phase 3's database execution
    model): the foreground `DatabaseLane`, threaded straight through to
    `_main_menu`'s mail branch (`netbbs.net.mail_flow`, the first —
    proof-of-pattern — module actually migrated onto the lane model).
    Optional and defaulted to `None`, same reasoning as `node_controls`
    below: every existing test calling this function directly, none of
    which exercise mail, needs no changes; `netbbs.__main__.run()` is
    the only caller that passes a real one. `db` above remains the
    synchronous connection every *other* feature in this module still
    uses unmigrated — the two coexist deliberately during this
    transition, not a contradiction.

    `shutdown_event`/`graceful_delay_seconds`/`drain_scheduler`/
    `shutdown_scheduler` (design doc -- node management) are bundled
    with `session_registry`/`maintenance` into a `NodeControls`,
    threaded down through `_run_authenticated_session`/`_main_menu` to
    `netbbs.net.admin_flow.admin_menu` — what the in-session `[N]ode`
    admin command needs to trigger a shutdown/drain directly, the same
    sequence a real OS signal already triggers for shutdown (see
    `netbbs.net.shutdown`). All optional/defaulted so every existing
    caller of this function (many tests, none of which exercise node
    management) needs no changes; `netbbs.__main__.run()` is the only
    caller that passes its own real values. `shutdown_scheduler` is also
    read directly below, before `node_controls` even exists yet, to
    tell a rejected connection how much longer a *scheduled* graceful
    shutdown's countdown has left.

    `throttle`/`throttle_config` implement issue #3's cross-connection
    login throttling: `throttle` is node-lifetime shared state (one
    instance for the whole node, constructed in `netbbs.__main__`
    alongside `hub` — see `netbbs.net.throttle.LoginThrottle`),
    `throttle_config` is the (also node-wide, but stateless) policy
    numbers driving it. Kept as two separate parameters rather than
    folding the config into the stateful object: `LoginThrottle` only
    needs the numbers once, at construction, to build its token
    buckets — `throttle_config` is consulted here directly for the
    per-connection attempt count, idle timeout, and login deadline,
    which aren't `LoginThrottle`'s concern at all.

    The concurrent-unauthenticated-session budget is acquired for the
    *entire* login phase (from here until `_login` returns one way or
    another) and released before the main menu ever runs — a session
    that's successfully authenticated no longer counts against this
    budget, precisely because the risk this budget guards against
    (an attacker holding open many never-completing connections) no
    longer applies to it.

    `presence` (design doc) is entered
    right before the main menu runs and left in a `finally` around it —
    this is the one place in the codebase that knows "this account now
    has one more/one fewer live connection", which `/away`'s "clears
    only when the account's final session disconnects" behavior
    depends on. Deliberately scoped to the authenticated portion only,
    same reasoning as the login-throttle budget above: an
    unauthenticated connection was never "present" as any account.

    `session_registry`/`maintenance` (design doc) are checked/
    entered before any of that, right at the top — a deliberate node
    shutdown needs to reach and reject connections regardless of
    whether they ever authenticate at all, unlike `presence`, which
    only ever needs to know about accounts.

    `link_context` (design doc), if given, is threaded
    straight through to both the ordinary board-browsing path (so
    composing a new post on a Linked board can queue its `board_post`
    event) and to `admin_menu` (the `[L]ink this board` command) — same
    optional/defaulted-to-`None` shape as `node_controls`: every
    existing caller of this function needs no changes, and `netbbs.
    __main__.run()` is the only caller that passes a real one, only
    when `config.link.enabled`.
    """
    if maintenance.is_active():
        detail = MAINTENANCE_MESSAGE
        if shutdown_scheduler is not None and shutdown_scheduler.is_scheduled():
            remaining = shutdown_scheduler.remaining_seconds()
            detail = f"{detail} (going down in {format_remaining_seconds(remaining)})"
        # `db=None`, not the real `db` -- this is the very first thing
        # this function does, deliberately never dereferencing `db` so
        # a maintenance rejection still works even if the db connection
        # itself is the thing in a bad state (test_shutdown.py exercises
        # this with a non-`Database` sentinel in `db`'s place).
        await _write_connection_notice(session, None, "Maintenance", detail)
        return

    node_controls = NodeControls(
        session_registry=session_registry,
        maintenance=maintenance,
        shutdown_event=shutdown_event if shutdown_event is not None else asyncio.Event(),
        graceful_delay_seconds=graceful_delay_seconds,
        drain_scheduler=drain_scheduler if drain_scheduler is not None else SequenceScheduler(),
        shutdown_scheduler=shutdown_scheduler if shutdown_scheduler is not None else SequenceScheduler(),
    )

    session_registry.enter(session)
    try:
        await _run_authenticated_session(
            session, db, hub, presence, mailbox, throttle, throttle_config,
            node_controls=node_controls, lane=lane, link_context=link_context, direct_invites=direct_invites,
        )
    finally:
        session_registry.leave(session)
        # GitHub issue #27: an online-only /msg queued for this specific
        # session must not survive to be shown after a later, distinct
        # reconnect -- discard whatever's still pending for it now that
        # it's gone, regardless of whether the same account remains
        # online via another session.
        mailbox.discard(session)


async def _run_authenticated_session(
    session: Session,
    db: Database,
    hub: ChatHub,
    presence: PresenceRegistry,
    mailbox: MessageMailbox,
    throttle: LoginThrottle,
    throttle_config: ThrottleConfig,
    *,
    node_controls: NodeControls | None = None,
    lane: DatabaseLane | None = None,
    link_context: LinkContext | None = None,
    direct_invites: DirectChatInvites | None = None,
) -> None:
    """The login-through-logoff body of a *Telnet/web* connection,
    wrapped by `handle_session`'s maintenance-mode check and session-
    registry bookkeeping (design doc) — split out so those two
    concerns stay a thin, easy-to-read wrapper rather than adding
    another level of nesting to the whole function.

    Interactive-login-specific (the concurrent-unauthenticated-session
    budget, the username/password prompt loop) -- SSH has already
    proven identity before its own entry point, `handle_ssh_session`,
    is ever called, so it skips straight to `run_authenticated_session`
    below instead of going through this function at all (GitHub issue
    #25).

    `node_controls`, if given, is threaded straight through to
    `_main_menu`/`admin_menu` (design doc);
    `None` is what a direct test call site (bypassing `handle_session`)
    gets by default, which correctly hides the `[N]ode` admin option
    rather than needing every such test updated."""
    if not throttle.try_enter_unauthenticated():
        await session.write_line(
            "This server has too many pending logins right now. Please try again shortly."
        )
        return

    try:
        await session.write_line(load_welcome_banner(db, truecolor=session.supports_truecolor))
        # Design doc -- node management, Thiesi's own request: shown to
        # *every* connecting client, SysOp-to-be or not -- account level
        # isn't known until credentials verify below, so this can't be
        # targeted any more narrowly than that. Purely informational: it
        # never blocks anyone by itself, unlike the post-authentication
        # `[M]aintenance mode` rejection a non-SysOp still gets further
        # down for actually trying to log in while this is on.
        if node_controls is not None and node_controls.maintenance.is_lockdown_active():
            await session.write_line(colored(f"\r\n{LOCKDOWN_NOTICE}", fg_color=ALERT_COLOR, bold=True))
        try:
            login_result = await asyncio.wait_for(
                _login(
                    session,
                    db,
                    throttle,
                    max_attempts=throttle_config.max_attempts_per_connection,
                    idle_timeout=throttle_config.unauthenticated_idle_timeout_seconds,
                ),
                timeout=throttle_config.login_deadline_seconds,
            )
        except asyncio.TimeoutError:
            await _write_connection_notice(session, db, "Session ended", "Login timed out. Goodbye.")
            return
    finally:
        throttle.leave_unauthenticated()

    if login_result is LoginOutcome.ATTEMPTS_EXHAUSTED:
        await _write_connection_notice(
            session,
            db,
            "Sign-in failed",
            "Too many failed attempts. Goodbye.",
            tone="error",
        )
        return
    if login_result is LoginOutcome.IDLE_TIMEOUT:
        await _write_connection_notice(session, db, "Session ended", "Timed out waiting for input. Goodbye.")
        return
    if login_result is LoginOutcome.THROTTLED:
        await _write_connection_notice(
            session,
            db,
            "Please wait",
            "Too many login attempts. Please try again later.",
            tone="error",
        )
        return
    if login_result is LoginOutcome.BLOCKED:
        return

    await run_authenticated_session(
        session, db, hub, presence, mailbox, login_result,
        node_controls=node_controls, lane=lane, link_context=link_context, direct_invites=direct_invites,
    )


async def _watch_for_account_revocation(
    session: Session, db: Database, user: User, session_registry: ActiveSessionRegistry
) -> None:
    """
    Runs for the lifetime of one authenticated session (started in
    `run_authenticated_session`, cancelled in its own `finally`
    alongside `presence.leave`): periodically re-checks
    `account_still_active()` and forcibly disconnects this session the
    moment it comes back `False`, regardless of which screen the
    session is currently blocked inside — including one genuinely idle,
    waiting on input that never comes (GitHub issue #29, reopened a
    second time).

    The in-loop `account_still_active()` checks already in `_main_menu`
    and `netbbs.net.chat_flow`'s send loop only ever fire on that
    loop's *next* keystroke/message — a session sitting inside board
    browsing, a file area, the profile screen, or (most significantly)
    the admin menu tree could otherwise keep operating indefinitely
    after a cross-process disable/delete, exactly as reported. This
    watcher is the comprehensive backstop for every one of those loops
    at once, present or future, without needing a copy of the same
    check bolted onto each — not a replacement for the in-loop checks,
    which still give an *actively* typing session zero-latency
    revalidation on its very next input rather than waiting for the
    next poll tick.

    Calls `session_registry.cancel_one`, not `disconnect_one` — see
    `cancel_one`'s own docstring for exactly why awaiting the fuller
    `disconnect_one` from inside this watcher task would deadlock
    against this same session's own cleanup trying to cancel *this*
    watcher task in turn.

    The "you're disconnected" notice is best-effort and *bounded*
    (GitHub issue #29, reopened a third time) — `session.write_line`
    is an unbounded transport operation; a real Telnet/SSH write
    ultimately awaits the socket/channel drain, and a peer that has
    simply stopped reading (TCP backpressure, not a closed connection —
    `SessionClosedError` only covers the latter) can stall it
    indefinitely. Cancellation is the actual security invariant here
    and must not depend on this presentation detail succeeding, so
    `cancel_one` runs from a `finally`, guaranteed to fire whether the
    write finishes, fails, or times out.
    """
    while True:
        await asyncio.sleep(_REVOCATION_CHECK_INTERVAL_SECONDS)
        if not account_still_active(db, user):
            try:
                await asyncio.wait_for(
                    session.write_line(
                        colored(
                            "\r\nYour account is no longer active. Disconnecting.", fg_color=MUTED_COLOR
                        )
                    ),
                    timeout=_REVOCATION_NOTICE_TIMEOUT_SECONDS,
                )
            except (asyncio.TimeoutError, SessionClosedError):
                pass
            finally:
                session_registry.cancel_one(session)
            return


async def _confirm_unicode_style(session: Session, db: Database, user: User) -> None:
    """One-time post-login check (dogfood feature request): shows a
    live sample of the Unicode breadcrumb style and asks whether it
    rendered cleanly, since -- unlike `netbbs.net.color_depth_
    preference`'s own COLORTERM signal -- there's no reliable way to
    detect real UTF-8 terminal support ahead of time. The sample is
    built with the actual `screen_title(..., unicode_style=True)`, not
    a hand-typed copy, so what's shown always matches what real screens
    will actually look like.

    Fires exactly once per account, gated on `unicode_style_ever_set`.
    Answering either way -- including keeping it on -- writes the
    preference, which itself counts as "touched" and prevents asking
    again (`netbbs.net.unicode_style_preference`'s own established
    contract, shared with `redraw_preference`)."""
    if unicode_style_ever_set(db, user):
        return
    await session.write_line(
        colored("\r\nNetBBS can use a few Unicode characters for a cleaner look, like this:", fg_color=METADATA_COLOR)
    )
    await session.write_line(
        screen_title(
            "Example", breadcrumb=(session.node_display_name, "System"), width=session.terminal_width,
            unicode_style=True, header_color=effective_header_color_256(db),
        node_name_gradient=session.node_name_gradient).split("\r\n")[0]
    )
    switch_off = await prompt_yes_no(
        session, "Does that look garbled or wrong? Switch to plain ASCII instead?", default=False
    )
    set_unicode_style_enabled(db, user, not switch_off)
    if switch_off:
        await session.write_line(
            colored(
                "Switched to plain ASCII style. You can change this later in Your profile.",
                fg_color=MUTED_COLOR,
            )
        )


async def run_authenticated_session(
    session: Session,
    db: Database,
    hub: ChatHub,
    presence: PresenceRegistry,
    mailbox: MessageMailbox,
    user: User,
    *,
    node_controls: NodeControls | None = None,
    lane: DatabaseLane | None = None,
    link_context: LinkContext | None = None,
    direct_invites: DirectChatInvites | None = None,
) -> None:
    """
    The authenticated-through-logoff body of a connection (GitHub issue
    #25's two-stage split): everything that happens once a `User` is
    already known-good, regardless of *how* that was established --
    Telnet/web's interactive `_login()` prompt (`_run_authenticated_
    session`, above), or SSH's own protocol-level password/public-key
    exchange (`handle_ssh_session`, below). Neither transport-specific
    entry point duplicates any of this; there is exactly one "what a
    session actually does" implementation.

    `node_controls`, if given, is threaded straight through to
    `_main_menu`/`admin_menu` (design doc);
    `None` is what a direct test call site (bypassing both entry
    points above) gets by default, which correctly hides the `[N]ode`
    admin option rather than needing every such test updated. The
    background account-revocation watcher (GitHub issue #29, reopened a
    second time) is gated on it the same way -- it needs
    `node_controls.session_registry` to actually reach this session
    from outside, and a caller bypassing `NodeControls` entirely gets
    no watcher, matching every other node-wide-registry-dependent
    feature's existing degrade-gracefully-in-tests behavior.

    `[M]aintenance mode` (design doc §13.8) is checked here, after
    credentials already verified -- unlike `handle_session`'s
    `maintenance.is_active()` gate (shutdown's unconditional, pre-login,
    no-bypass lockout), this one only blocks a *non-SysOp* account, so a
    SysOp can still log in to manage the node (including turning
    lockdown back off) while it's active. A caller bypassing
    `node_controls` entirely (direct test call sites) gets no lockdown
    check at all, matching this function's own established
    degrade-gracefully convention.
    """
    # Resolved once, right here, rather than looked up fresh by every
    # screen_title() call site below (netbbs.net.session.Session.
    # node_display_name's own docstring): the same value for every
    # caller on the node, so a session-lifetime cache is enough -- a
    # SysOp renaming the node takes effect for new connections, not
    # this one already in progress. `node_name_gradient` (issue #175)
    # shares this exact same lifecycle -- resolved alongside the name
    # it recolors, not looked up fresh per screen the way the node's
    # other branding colors are (see `netbbs.net.node_theme`'s own
    # node-name-gradient section docstring for why).
    session.node_display_name = get_node_display_name(db)
    session.node_name_gradient = effective_node_name_gradient(db)

    if (
        node_controls is not None
        and node_controls.maintenance.is_lockdown_active()
        and not meets_level(user, SYSOP_LEVEL)
    ):
        await session.write_line(f"\r\n{LOCKDOWN_MESSAGE}")
        return

    # The Ctrl-L mention (issue #102) lives here, once per session,
    # rather than repeated on every single menu redraw the way list
    # screens' own Ctrl-L/Ctrl-R hint is (netbbs.net.picker) -- the main
    # menu's own options line is already dense enough without adding a
    # permanent trailer to it too. Separator matches the main menu's own
    # subtitle line just below it (style spec, round following the
    # pre-5.0.0 "beautify" audit) instead of the flat, always-ASCII "/"
    # this used to hardcode -- built by hand rather than via field_row
    # since the username segment keeps its own pre-existing `bold=True`,
    # which that shared helper doesn't (and doesn't need to, for its
    # other callers) support per-field.
    welcome_separator = (
        colored(" › ", fg_color=METADATA_COLOR) if unicode_style_enabled(db, user) else "  /  "
    )
    welcome = (
        "\r\n"
        + colored(
            f"Welcome, {sanitize_text(user.username)}",
            fg_color=effective_accent_color(session, db),
            bold=True,
        )
        + welcome_separator
        + colored(f"level {user.user_level}", fg_color=VALUE_COLOR)
        + welcome_separator
        + colored("Ctrl-L redraws", fg_color=METADATA_COLOR)
    )
    if (
        node_controls is not None
        and node_controls.maintenance.is_lockdown_active()
        and meets_level(user, SYSOP_LEVEL)
    ):
        welcome += " (Maintenance mode is ON.)"
    await session.write_line(welcome)
    await _announce_pending_invitations(session, db, user)
    # Design doc -- node management, Thiesi's own report: drain never
    # persisted any state before, so a user who wasn't connected when it
    # was scheduled -- or who reconnects after being disconnected by an
    # earlier drain pass -- had no way to know one was still in
    # progress until it disconnected them again with no warning at all.
    # Non-SysOp only (reaching this point at all already implies
    # lockdown isn't active for this account, see the rejection branch
    # above) -- a SysOp is exempt from drain by design and would never
    # actually be disconnected by it.
    if (
        node_controls is not None
        and not meets_level(user, SYSOP_LEVEL)
        and node_controls.drain_scheduler.is_scheduled()
    ):
        remaining = node_controls.drain_scheduler.remaining_seconds()
        await session.write_line(
            colored(
                f"\r\nNote: this node is currently being drained for maintenance -- "
                f"you will be disconnected in about {format_remaining_seconds(remaining)}.",
                fg_color=ALERT_COLOR, bold=True,
            )
        )

    # One InputHistory per connection (design doc),
    # not node-wide like hub/presence/mailbox -- constructed here rather
    # than passed in from netbbs.__main__, so each connected session
    # gets its own recall buffer. Only threaded down into chat's input
    # loop (the actual pain point this was built for); other screens'
    # read_line() calls simply don't pass one and get no recall.
    history = InputHistory()

    # Issue #164: node-wide presence -- broadcast a join/leave to every
    # currently-connected Link peer only on this account's *first*
    # concurrent session / *last* remaining one, checked against
    # PresenceRegistry's own is_online before/after enter()/leave() --
    # PresenceRegistry already supports multiple simultaneous sessions
    # per account (design doc), and a second session logging in (or a
    # non-final one logging out) must not tell peers the account's
    # online status changed when it hasn't. Best-effort by construction
    # (every send inside broadcast_node_presence_live already swallows a
    # dead peer's LinkTransportError), so this never blocks or fails a
    # login/logout either way.
    already_online = presence.is_online(user.username)
    presence.enter(user.username)
    if not already_online and link_context is not None and link_context.realtime_bridge is not None:
        await link_context.realtime_bridge.broadcast_node_presence_live(change="join", username=user.username)
    history_id = record_session_start(db, user)
    watcher_task: asyncio.Task | None = None
    if node_controls is not None:
        node_controls.session_registry.mark_authenticated(
            session, user.username, is_sysop=meets_level(user, SYSOP_LEVEL)
        )
        watcher_task = asyncio.create_task(
            _watch_for_account_revocation(session, db, user, node_controls.session_registry)
        )
    try:
        # Deliberately after the watcher task above, not alongside the
        # other post-login notices earlier in this function: unlike
        # those (which only ever print, never read), this is genuinely
        # interactive and could block indefinitely on a session that
        # never answers -- placing it before the watcher existed would
        # leave a revoked account's session completely unprotected for
        # as long as it sat here (GitHub issue #29's whole point).
        await _confirm_unicode_style(session, db, user)
        await _main_menu(
            session, db, hub, presence, mailbox, history, user,
            node_controls=node_controls, lane=lane, link_context=link_context, direct_invites=direct_invites,
        )
    finally:
        presence.leave(user.username)
        if (
            not presence.is_online(user.username)
            and link_context is not None
            and link_context.realtime_bridge is not None
        ):
            await link_context.realtime_bridge.broadcast_node_presence_live(change="leave", username=user.username)
        record_session_end(db, history_id)
        if watcher_task is not None:
            # Same cancel-then-await-swallowing-CancelledError shape
            # editor autosave tasks already use (GitHub issue #43) --
            # a no-op if the watcher itself is what triggered this
            # unwind (it's already finished by the time control reaches
            # here), and a clean, awaited cancellation otherwise (the
            # session ended some other way while the watcher was still
            # mid-sleep).
            watcher_task.cancel()
            try:
                await watcher_task
            except asyncio.CancelledError:
                pass

    # GitHub issue #177: only reached when `_main_menu` returns normally,
    # not when it (or anything nested under it) raises -- which covers
    # the deliberate "Log off?" confirm this pre-existing "Signed out"/
    # "Goodbye!" notice was already written for. `_watch_for_account_
    # revocation`'s own disconnect (a task cancellation) bypasses this
    # point entirely, straight out of the function, as does any
    # `ActiveSessionRegistry.disconnect_all`-driven kick/drain -- neither
    # ever sees this banner. One pre-existing wrinkle worth knowing about
    # rather than silently inheriting: `_main_menu`'s own in-loop
    # `account_still_active()` recheck (a revoked account caught on its
    # *next* keystroke, before the watcher's periodic poll gets to it --
    # see that watcher's own docstring) also returns normally rather than
    # raising, so it already fell through to this same friendly "Signed
    # out"/"Goodbye!" message before this banner existed, and will now
    # show this banner too. Not a new gap this change introduces, and not
    # fixed here -- out of scope for adding a banner to an existing,
    # unrelated call site.
    logoff_banner = load_logoff_banner(db)
    if logoff_banner:
        await session.write_line(logoff_banner)
    await _write_connection_notice(session, db, "Signed out", "Goodbye!", tone="success")


async def _authorize_ssh_authenticated_user(
    session: Session, db: Database, username: str
) -> User | LoginOutcome:
    """
    Re-resolves and authorizes `username` fresh, immediately before an
    SSH session actually begins (GitHub issue #25).

    SSH proves identity during its own protocol-level handshake
    (`netbbs.net.ssh._NetBBSSSHServer.validate_password`/
    `validate_public_key`) — genuinely earlier than the process/session
    this runs in ever starts, unlike Telnet/web's interactive
    `_login()`, where the credential check and everything after it
    happen essentially atomically in one function call. Re-fetching
    here closes that gap: a SysOp disabling or deleting the account in
    the meantime (however narrow a window in practice) would otherwise
    go unnoticed. `authenticate_password_async`/`authorize_public_key`
    already checked `disabled_at` at their own, earlier point in time;
    this repeats that check now, plus the blocklist check `_login`'s
    own docstring explains is a distinct authentication-vs-
    authorization concern Telnet/web's inline check (below) already
    makes for its own path.
    """
    try:
        user = get_user_by_username(db, username)
    except AuthError:
        await _write_connection_notice(
            session,
            db,
            "Access unavailable",
            "Your account is no longer available. Goodbye.",
            tone="error",
        )
        return LoginOutcome.BLOCKED
    if user.disabled_at is not None:
        await _write_connection_notice(
            session,
            db,
            "Access unavailable",
            "Your account is no longer available. Goodbye.",
            tone="error",
        )
        return LoginOutcome.BLOCKED
    if is_blocked(db, user):
        await _write_connection_notice(
            session,
            db,
            "Access revoked",
            "Your access to this system has been revoked.",
            tone="error",
        )
        return LoginOutcome.BLOCKED
    return user


async def handle_ssh_session(
    session: Session,
    db: Database,
    hub: ChatHub,
    presence: PresenceRegistry,
    mailbox: MessageMailbox,
    session_registry: ActiveSessionRegistry,
    maintenance: MaintenanceMode,
    *,
    shutdown_event: asyncio.Event | None = None,
    graceful_delay_seconds: float = 60.0,
    drain_scheduler: SequenceScheduler | None = None,
    shutdown_scheduler: SequenceScheduler | None = None,
    lane: DatabaseLane | None = None,
    link_context: LinkContext | None = None,
    direct_invites: DirectChatInvites | None = None,
) -> None:
    """
    SSH-specific top-level entry point (GitHub issue #25) — the
    `session_handler` `netbbs.__main__.run` gives to
    `netbbs.net.ssh.SSHServer`, distinct from `handle_session` (which
    stays exactly what Telnet/web use). SSH has already proven identity
    via its own protocol-level handshake by the time this is ever
    called (see `netbbs.net.ssh.SSHSession.authenticated_username`),
    so this never calls `_login()` or prompts for a username/password a
    second time — the actual bug this closes: previously every
    transport funneled through the same `handle_session`, which had no
    idea an SSH connection had already authenticated and always asked
    again, defeating public-key-only accounts entirely (no password to
    give the second prompt) and needlessly re-prompting password
    accounts.

    Deliberately does *not* acquire `throttle`'s concurrent-
    unauthenticated-session budget the way `handle_session` does for
    Telnet/web: that budget exists to bound how many connections can
    sit *unauthenticated* at once, and by the time this function is
    called, SSH has already fully authenticated the connection through
    its own handshake (with its own `login_timeout` -- see
    `netbbs.net.ssh.SSHServer`'s docstring on why that's a separate,
    already-sufficient mechanism). Counting it against the same budget
    as a genuinely unauthenticated Telnet/web connection would be
    double-charging a connection that was never actually in the state
    that budget protects against.

    Otherwise mirrors `handle_session`'s maintenance-mode check and
    session-registry bookkeeping exactly — see that function's
    docstring for the reasoning, not repeated here.
    """
    if maintenance.is_active():
        detail = MAINTENANCE_MESSAGE
        if shutdown_scheduler is not None and shutdown_scheduler.is_scheduled():
            remaining = shutdown_scheduler.remaining_seconds()
            detail = f"{detail} (going down in {format_remaining_seconds(remaining)})"
        # `db=None`, not the real `db` -- this is the very first thing
        # this function does, deliberately never dereferencing `db` so
        # a maintenance rejection still works even if the db connection
        # itself is the thing in a bad state (test_shutdown.py exercises
        # this with a non-`Database` sentinel in `db`'s place).
        await _write_connection_notice(session, None, "Maintenance", detail)
        return

    node_controls = NodeControls(
        session_registry=session_registry,
        maintenance=maintenance,
        shutdown_event=shutdown_event if shutdown_event is not None else asyncio.Event(),
        graceful_delay_seconds=graceful_delay_seconds,
        drain_scheduler=drain_scheduler if drain_scheduler is not None else SequenceScheduler(),
        shutdown_scheduler=shutdown_scheduler if shutdown_scheduler is not None else SequenceScheduler(),
    )

    session_registry.enter(session)
    try:
        username = getattr(session, "authenticated_username", None)
        if not username:
            # Unreachable in practice -- asyncssh never opens a
            # process/session without a prior successful
            # validate_password/validate_public_key -- but refusing
            # cleanly here is cheaper than trusting that invariant
            # blindly.
            await _write_connection_notice(
                session,
                db,
                "SSH sign-in failed",
                "SSH authentication did not complete. Goodbye.",
                tone="error",
            )
            return

        result = await _authorize_ssh_authenticated_user(session, db, username)
        if isinstance(result, LoginOutcome):
            return
        await run_authenticated_session(
            session, db, hub, presence, mailbox, result,
            node_controls=node_controls, lane=lane, link_context=link_context, direct_invites=direct_invites,
        )
    finally:
        session_registry.leave(session)
        mailbox.discard(session)


async def _announce_pending_invitations(session: Session, db: Database, user: User) -> None:
    """
    A one-time-per-login notice (GitHub issue #42) if `user` has any
    pending channel invitations — the actual discoverability fix: an
    offline invitee previously had no notification mechanism at all
    (`_deliver_private_message`'s mailbox is session-addressed and
    ephemeral, see its own docstring, so it silently reached nobody
    with no active session at `/invite` time), even though the durable
    `channel_invitations` row was always created regardless.

    Deliberately brief (a count, not the full list with channel names/
    inviters) -- `[I]nvitations` on the main menu (see
    `_draw_main_menu`/`_show_pending_invitations`) shows full detail
    and reappears on every redraw for as long as anything's still
    pending, so this only needs to point there, not duplicate it.
    Called once, right after login (`run_authenticated_session`), not
    from `_draw_main_menu` itself -- that function redraws on every
    return from a submenu, which would repeat this same notice far more
    often than the one genuinely new moment it's meant to mark.
    """
    pending = list_pending_invitations_for_user(db, user)
    if not pending:
        return
    plural = "s" if len(pending) != 1 else ""
    await session.write_line(
        colored(
            f"\r\n*** You have {len(pending)} pending chat channel invitation{plural}. "
            "See [I]nvitations on the main menu. ***",
            fg_color=MUTED_COLOR,
        )
    )


async def _show_pending_invitations(session: Session, db: Database, user: User) -> None:
    """The on-demand full-detail view `_announce_pending_invitations`'s
    brief notice points to -- channel name, inviter, and when, for
    every currently pending invitation. No accept/reject action lives
    here: `/join <channel>` from the channel picker remains the one
    way to accept (design doc's "reuse /join" decision,
    unchanged by this issue), so this is purely informational, telling
    the invitee what to type and where."""
    pending = list_pending_invitations_for_user(db, user)
    header = colored("Pending invitations:", fg_color=effective_header_color(session, db), bold=True)
    await session.write_line(f"\r\n{header}")
    if not pending:
        await session.write_line("You have no pending chat channel invitations.")
        return
    for invitation in pending:
        when = format_for_display(invitation.created_at, db)
        await session.write_line(
            f"  #{sanitize_text(invitation.channel_name)} "
            f"-- invited by {sanitize_text(invitation.invited_by_username)} ({when})"
        )
    await session.write_line(
        colored(
            "Use [C]hat, then /join <channel> from the chat channel picker to accept one.",
            fg_color=MUTED_COLOR,
        )
    )


async def _draw_main_menu(
    session: Session, db: Database, mailbox: MessageMailbox, user: User,
    *, node_controls: NodeControls | None = None,
) -> None:
    """
    Shows any private messages that arrived while away from this menu,
    then the menu itself.

    `node_controls` (design doc -- node management, Thiesi's own
    request), if given, prefixes the `Choice: ` prompt with the current
    BBS time (a snapshot at draw time, not a ticking live clock -- this
    codebase has no per-session background refresh mechanism, and
    building one just for a clock would be disproportionate to what was
    actually asked for) and a visual alert tag for every currently-
    applicable status (a scheduled shutdown, a scheduled drain, and --
    SysOps only, since a non-SysOp who reached the menu at all already
    implies lockdown isn't blocking them -- maintenance mode being on),
    concatenated in that order rather than showing only the single most
    urgent one: `[L]ock & drain` (design doc §13.8) makes "drain and
    maintenance mode both active at once" a common case, not a rare
    edge case, so dropping one silently would recreate the exact blind
    spot `_draw_node_menu`'s own docstring already describes for the
    separate-toggle case. `None` (a direct test call site bypassing `handle_
    session`) leaves the prompt exactly as it always was -- bare
    `Choice: `, no time, no tag -- the same degrade-gracefully
    convention every other optional `node_controls` parameter in this
    module already follows, and deliberately conservative about not
    changing output text for the many existing tests that call this
    function directly without one.

    This is the one place `/msg`'s mailbox-plus-next-prompt delivery
    (design doc) actually flushes:
    every screen (boards, files, directory, profile, chat) returns here
    before its next redraw, so a single flush point here covers all of
    them without needing one sprinkled into each individual screen.

    Each flushed `(text, created_at)` pair is formatted through
    `format_with_preference` (design doc -- per-user chat timestamp
    preference), honoring `user`'s *current* timestamp preference
    at display time -- the recipient here is always `user` themselves,
    so unlike live chat's per-recipient broadcast problem, no envelope
    threading through a shared queue is needed, just the same formatting
    call `netbbs.net.chat_flow` uses for its own timestamped lines.

    Flushed by `session` (GitHub issue #27's session-addressed
    redesign), not by `user.username` -- an account with several active
    sessions each has its own independent pending queue now, so this
    only ever drains what was actually queued for *this* connection,
    never stealing a sibling session's still-pending messages.

    `[I]nvitations` (GitHub issue #42) is shown only while `user` has
    at least one currently pending invitation -- same "only offer what
    currently applies" convention `_render_board_page`'s `[O]lder`/
    `[N]ewer` already follow, and it naturally disappears again once
    every pending invitation is accepted/revoked/expired, with no
    separate "mark as seen" bookkeeping needed: this just re-queries
    current truth on every redraw.

    `[E]-mail` (design doc, `netbbs.mail`/
    `netbbs.net.mail_flow`) is always shown, unlike `[I]nvitations` --
    it's a core always-available feature, not a transient notification --
    but grows an "(N unread)" suffix the same "re-query on every redraw,
    no separate seen-tracking" way. Deliberately a different letter and a
    different persistence model from `/msg`: `E` (for "E-mail") is the
    closest thing to a ready-made convention BBS users already have
    muscle memory for.

    `[C]ommunities`/`[U]ncategorized`/`[J]ump to...` (design doc §16)
    replace the old flat `[M]essage Boards`/`[C]hat`/
    `[F]ile areas` split -- `[C]` is reused here specifically because
    Chat moving one level into the shared resource-type sub-menu frees
    it back up (confirmed directly with Thiesi: the design's original
    spec assumed `[E]nter a Community`, but mail later claimed
    `E`). `[C]ommunities`/`[U]ncategorized` are conditionally
    visible -- hidden when there are zero (visible) Communities, or
    zero visible Uncategorized resources, respectively -- same "only
    offer what currently applies" convention as `[I]nvitations`;
    `[J]ump to...` is always shown, matching the old flat menu's own
    unconditional `[M]/[C]/[F]` behavior exactly. On a freshly upgraded
    node with no Communities created yet, this reduces the menu to
    `[U]ncategorized  [J]ump to...` (assuming at least one board/
    channel/area already exists), functionally identical to today's
    flat menu -- migration is a non-event.

    `[N]ew scan` (issue #56) is always shown too, right next to `[J]ump
    to...` -- an activity summary across every accessible board/channel/
    file area, not gated on anything currently existing (a brand-new
    account with nothing yet visited still gets a useful "not yet
    visited" summary, matching classic BBS new-scan semantics).

    `[F]ind` (issue #56's local search) is always shown alongside it --
    unlike `[N]ew scan`, this doesn't summarize *everything* accessible;
    it only runs once a query is actually typed, so there's no "brand-new
    account" empty-list concern to gate on either.

    `netbbs.net.main_menu_banner.load_main_menu_banner` (issue #161,
    skinning part two) optionally prepends a SysOp-authored masthead
    above everything below -- `""` (no masthead, the default) reproduces
    this function's output byte-for-byte as it was before that module
    existed.
    """
    for text, created_at in mailbox.flush(session):
        await session.write_line(format_with_preference(db, user, text, created_at))

    unread = unread_mail_count(db, user)
    mail_label = f"-mail ({unread} unread)" if unread else "-mail"
    # Brief descriptions are kept to roughly 34 characters or less --
    # the actual available width once this renders in two columns at
    # the classic 80-column terminal (menu_grid's own column_width
    # minus its description indent). Longer, fuller text belongs in
    # `detailed`, shown only when a caller opts into that verbosity.
    explore_options = []
    if _has_visible_communities(db, user):
        explore_options.append(MenuEntry(
            label=menu_key("C", "ommunities"),
            brief="Spaces shared by other callers",
            detailed="Browse Communities -- groups of message boards/chat channels/file areas organized by topic.",
        ))
    if _has_uncategorized_resources(db, user):
        explore_options.append(MenuEntry(
            label=menu_key("U", "ncategorized"),
            brief="Boards/areas outside a Community",
        ))
    explore_options.extend(
        [
            MenuEntry(label=menu_key("J", "ump to..."), brief="Go straight to a name you know"),
            MenuEntry(
                label=menu_key("N", "ew scan"),
                brief="Activity since your last visit",
                detailed="Scan every accessible message board/chat channel/file area for activity since your last visit.",
            ),
            MenuEntry(label=menu_key("F", "ind"), brief="Search boards, files, and mail"),
        ]
    )
    personal_options = [
            MenuEntry(label=menu_key("D", "irectory"), brief="Look up other callers"),
            MenuEntry(
                label=menu_key("P", "rofile"),
                brief="Your bio and preferences",
                detailed="Edit your bio, visibility, and preferences -- including these menu descriptions.",
            ),
            MenuEntry(label=menu_key("E", mail_label), brief="Read and send private mail"),
            MenuEntry(label=menu_key("H", "istory"), brief="Your recent sessions"),
    ]
    if node_controls is not None:
        personal_options.append(
            MenuEntry(label=menu_key("W", "ho's online"), brief="See who's connected now")
        )
    if list_pending_invitations_for_user(db, user):
        personal_options.append(
            MenuEntry(label=menu_key("I", "nvitations"), brief="Pending invitations for you")
        )
    if user.can_verify_identity or meets_level(user, SYSOP_LEVEL):
        personal_options.append(
            MenuEntry(label=menu_key("V", "erify"), brief="Verify a caller's identity")
        )
    system_options = []
    if meets_level(user, SYSOP_LEVEL):
        system_options.append(
            MenuEntry(label=menu_key("S", "ysOp"), brief="Node administration console")
        )
    system_options.append(MenuEntry(label=menu_key("L", "ogoff"), brief="Disconnect from this node"))

    unicode_style = unicode_style_enabled(db, user)
    collapsed = breadcrumb_collapsed_enabled(db, user)
    # "mail" pluralized is "mails," which reads oddly -- the Mail submenu's
    # own header (`_render_mail_menu`) already settled this exact wording as
    # "message(s)"; matching it here fixes both the missing pluralization
    # and a term the app wasn't even using consistently with itself.
    mail_status = (
        (f"{unread} unread message{'' if unread == 1 else 's'}", WARNING_COLOR)
        if unread
        else ("mail caught up", SUCCESS_COLOR)
    )
    masthead = load_main_menu_banner(db)
    redraw = redraw_in_place_enabled(db, user)
    title = screen_title(
        "Main menu",
        breadcrumb=(session.node_display_name,),
        subtitle=field_row(
            [
                (sanitize_text(user.username), effective_accent_color(session, db)),
                (f"level {user.user_level}", VALUE_COLOR),
                mail_status,
            ],
            unicode_style=unicode_style,
        ),
        width=session.terminal_width,
        # `clear` stays False here whenever a masthead is shown -- it
        # must land *after* any clear-screen sequence but *before* this
        # title/breadcrumb, and `screen_title` only ever prepends its own
        # clear_screen() to its own returned text, so the redraw-in-place
        # clear is issued by hand below instead in that case (issue #161).
        clear=False if masthead else redraw,
        unicode_style=unicode_style, collapsed=collapsed,
        header_color=effective_header_color(session, db), node_name_gradient=session.node_name_gradient)
    options = menu_grid(
        [("Explore", explore_options), ("You", personal_options), ("System", system_options)],
        width=session.terminal_width,
        height=session.terminal_height,
        description_level=menu_description_level(db, user),
    )
    if masthead:
        prefix = clear_screen() if redraw else ""
        await session.write_line(f"{prefix}{masthead}\r\n{title}\r\n{options}\r\n")
    else:
        # Masthead disabled (the default): identical bytes to before
        # issue #161, unconditionally -- no existing node's output
        # changes just because this module now exists.
        await session.write_line(f"\r\n{title}\r\n{options}\r\n")
    await session.write(_main_menu_prompt(db, user, node_controls))


def _main_menu_prompt(db: Database, user: User, node_controls: NodeControls | None) -> str:
    """`Choice: `, optionally prefixed with the current BBS time and a
    node-status alert tag -- see `_draw_main_menu`'s own docstring for
    why `node_controls is None` leaves this completely unchanged.

    Time-only (`override_format="%H:%M:%S"`), not the node's full
    configured display format (which includes the date) -- the same
    "date is static clutter, not information" reasoning already applied
    to the chat status line's own clock and per-message timestamps (see
    `netbbs.chat.timestamps.format_with_preference`'s docstring): a
    snapshot taken once per menu redraw shows the same date for an
    entire session for the overwhelming majority of users, so printing
    it here added width without adding information. Seconds are kept
    (unlike those other two clocks) since Thiesi specifically asked for
    them here; still just a snapshot at draw time, not a ticking live
    clock (see `_draw_main_menu`'s own docstring for why).

    Rendered as alternating colors (`HH`/`MM`/`SS` in `CLOCK_COLOR`, the
    `:` separators in `MUTED_COLOR`) rather than one flat color --
    Thiesi's own explicit request for a two-tone "digital clock" look,
    distinguishing the digit groups from the separators at a glance.
    `CLOCK_COLOR` (not `HEADER_COLOR`, used one line above by the "Main
    menu:" label itself) is a deliberate follow-up fix: sharing
    `HEADER_COLOR` made the clock read as part of that header rather
    than a separate, unrelated element of the prompt.
    """
    if node_controls is None:
        return "Choice: "

    time_only = format_for_display(utc_now_iso(), db, override_format="%H:%M:%S")
    hours, minutes, seconds = time_only.split(":")
    separator = colored(":", fg_color=MUTED_COLOR)
    clock_color = effective_clock_color_256(db)
    time_str = separator.join(
        colored(part, fg_color=clock_color) for part in (hours, minutes, seconds)
    )
    tags: list[str] = []
    if node_controls.shutdown_scheduler.is_scheduled():
        remaining = node_controls.shutdown_scheduler.remaining_seconds()
        tags.append(colored(f"[SHUTDOWN {format_remaining_seconds(remaining)}]", fg_color=ALERT_COLOR, bold=True))
    if node_controls.drain_scheduler.is_scheduled():
        remaining = node_controls.drain_scheduler.remaining_seconds()
        tags.append(colored(f"[DRAINING {format_remaining_seconds(remaining)}]", fg_color=ALERT_COLOR, bold=True))
    if node_controls.maintenance.is_lockdown_active():
        # Only ever reached by a SysOp -- a non-SysOp who made it to the
        # main menu at all already implies lockdown wasn't blocking them.
        tags.append(colored("[MAINT MODE]", fg_color=ALERT_COLOR, bold=True))
    tag = "".join(t + " " for t in tags)
    return f"{time_str} {tag}Choice: "


async def _main_menu(
    session: Session,
    db: Database,
    hub: ChatHub,
    presence: PresenceRegistry,
    mailbox: MessageMailbox,
    history: InputHistory,
    user: User,
    *,
    node_controls: NodeControls | None = None,
    lane: DatabaseLane | None = None,
    link_context: LinkContext | None = None,
    direct_invites: DirectChatInvites | None = None,
) -> None:
    """
    The main menu, now dispatching immediately on a single keystroke
    (`read_key`) rather than waiting for a full line + Enter — a direct
    benefit of character-mode input landing in `netbbs.net.telnet`.

    Real behavior change worth being explicit about: the old
    line-based version accepted either the letter or the full word
    ("b" or "boards") as valid input. Immediate single-key dispatch can't
    keep that — the whole point is acting on the very first keystroke,
    with no way to know whether more characters are about to follow.
    Only the single letter works now.

    The menu, and its `Choice: ` prompt, are drawn once on entry and
    again after returning from a submenu (a real context change worth
    re-showing) — not on every loop iteration, and not at all on an
    unrecognized key (design doc): that just sounds a bell and
    leaves the screen exactly as it was, no reprinted prompt, since
    nothing was actually communicated worth a fresh line for.

    `direct_invites` (design doc §6.3): every loop iteration races the
    ordinary `read_key()` against `direct_invites.pending_for(session).
    arrived_event` (when something is actually pending) via `asyncio.
    wait(..., return_when=FIRST_COMPLETED)` -- the same cancel-a-live-
    pending-read pattern `netbbs.net.chat_flow._chat_loop` already uses
    for a kick, applied here to let an invite interrupt this specific
    idle read the moment it arrives. One event that stays set until
    consumed is what makes this cover both agreed behaviors for free:
    idle right now -> the race resolves on the invite side immediately;
    busy elsewhere when it arrived -> the event is already set by the
    time this loop's next iteration starts racing again after returning
    here, so that iteration's own race resolves just as instantly. No
    separate queued-notice mechanism exists (or is needed) for this --
    see `netbbs.chat.direct_invites.DirectChatInvite`'s own docstring.
    Deliberately scoped to only this one loop, not any other hotkey read
    elsewhere (the Who screen's own picker, admin screens, etc.) -- every
    other screen simply falls under "shown once back here."
    """
    await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
    while True:
        key_task = asyncio.create_task(session.read_key())
        if direct_invites is not None:
            # Always races, every iteration -- not only when something
            # already happens to be pending. `arrival_event` is a
            # persistent per-session event a waiter can start waiting on
            # before any invite has ever arrived at all; without that,
            # an invite landing while this exact await is already in
            # flight (idle, nothing racing it yet) would only be noticed
            # on the *next* keystroke instead of interrupting immediately
            # -- see that method's own docstring.
            invite_task = asyncio.create_task(direct_invites.arrival_event(session).wait())
            try:
                done, _pending = await asyncio.wait({key_task, invite_task}, return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                # This session's own task was cancelled from outside
                # (deliberate node shutdown/drain, an abrupt client
                # disconnect noticed elsewhere -- design doc's
                # ActiveSessionRegistry.disconnect_all()) while racing
                # key_task against invite_task -- same gap
                # netbbs.net.chat_flow's _chat_loop/_direct_chat_loop
                # already hit and fixed: asyncio.wait() being cancelled
                # does NOT cancel the tasks it was waiting on, so
                # without this, key_task/invite_task are left orphaned
                # and whichever one later finishes with an exception
                # (e.g. SessionClosedError once the socket actually
                # closes) has no one left to retrieve it, and asyncio
                # logs "Task exception was never retrieved."
                key_task.cancel()
                invite_task.cancel()
                await asyncio.gather(key_task, invite_task, return_exceptions=True)
                raise
            if invite_task in done:
                key_task.cancel()
                await asyncio.gather(key_task, return_exceptions=True)
                direct_invites.clear_arrival(session)
                await _handle_incoming_invite(session, db, direct_invites, hub, presence, user)
                continue
            invite_task.cancel()
            await asyncio.gather(invite_task, return_exceptions=True)
        choice = (await key_task).lower()

        if not account_still_active(db, user):
            # GitHub issue #29: the cross-process revalidation
            # boundary. In-process disable/delete already disconnects
            # a live session directly (see
            # netbbs.net.admin_flow._revoke_live_sessions), but the
            # standalone `python -m netbbs.admin` CLI can also change
            # `disabled_at`/delete the row from a completely separate
            # process with no in-memory notification path at all --
            # this re-check, at one natural choke point every
            # main-menu action passes through, is an authoritative
            # fallback regardless of which process made the change.
            # `netbbs.net.chat_flow`'s send loop has the identical
            # check at its own equivalent boundary (GitHub issue #29,
            # reopened) -- a session that never returns to this menu
            # (e.g. staying in chat) still gets revalidated there.
            await session.write_line(
                colored("\r\nYour account is no longer active. Disconnecting.", fg_color=MUTED_COLOR)
            )
            return

        if choice == REDRAW_KEY:
            # Issue #102: redraws in place, no state change -- the same
            # "not a real action" shape an unrecognized key already has
            # (design doc), just without the bell, since Ctrl-L is a
            # deliberate request, not a mistyped one.
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
            continue

        if choice == "l":
            await session.write_line("")
            if not await prompt_yes_no(session, "Log off?", default=False):
                await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
                continue
            return
        elif choice == "c" and _has_visible_communities(db, user):
            await session.write_line("")
            await _enter_communities(
                session, db, hub, presence, mailbox, history, user,
                node_controls=node_controls, lane=lane, link_context=link_context,
                direct_invites=direct_invites,
            )
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
        elif choice == "u" and _has_uncategorized_resources(db, user):
            await session.write_line("")
            await _enter_uncategorized(
                session, db, hub, presence, mailbox, history, user,
                node_controls=node_controls, lane=lane, link_context=link_context,
                direct_invites=direct_invites,
            )
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
        elif choice == "j":
            await session.write_line("")
            await _jump_to(
                session, db, hub, presence, mailbox, history, user,
                node_controls=node_controls, lane=lane, link_context=link_context,
                direct_invites=direct_invites,
            )
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
        elif choice == "n":
            await session.write_line("")
            # Issue #56: same lane-is-None degrade-gracefully reasoning
            # as "e"/"s" above -- a direct test call site without a real
            # lane simply can't reach the new-scan screen's own
            # unread-count queries.
            if lane is not None:
                await _new_scan_screen(
                    session, db, lane, hub, presence, mailbox, history, user, link_context=link_context
                )
            else:
                await session.write_line(
                    colored("New scan is not available in this context.", fg_color=MUTED_COLOR)
                )
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
        elif choice == "f":
            await session.write_line("")
            if lane is not None:
                await _find_screen(
                    session, db, lane, hub, presence, mailbox, history, user, link_context=link_context
                )
            else:
                await session.write_line(
                    colored("Find is not available in this context.", fg_color=MUTED_COLOR)
                )
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
        elif choice == "d":
            await session.write_line("")
            await _browse_directory(session, db, user)
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
        elif choice == "p":
            await session.write_line("")
            # Issue #160's cursor-nav follow-up: the profile screen is
            # now built on edit_resource_draft, which needs a real
            # DatabaseLane -- see the "e" (mail) branch above for the
            # identical lane-is-None degrade-gracefully reasoning.
            if lane is not None:
                await _edit_profile(session, lane, user)
            else:
                await session.write_line(
                    colored("Your profile is not available in this context.", fg_color=MUTED_COLOR)
                )
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
        elif choice == "e":
            await session.write_line("")
            # design doc, issue #57: mail is one of the features
            # migrated onto the two-lane database execution model --
            # `lane` is None only for a direct test call site that
            # doesn't supply one (same degrade-gracefully-in-tests
            # shape `node_controls` already uses above), never for a
            # real connection, since netbbs.__main__.run() always
            # passes a real foreground lane.
            if lane is not None:
                await browse_mail(session, lane, user, link_context=link_context)
            else:
                await session.write_line(
                    colored("Mail is not available in this context.", fg_color=MUTED_COLOR)
                )
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
        elif choice == "h":
            await session.write_line("")
            await _last_sessions_screen(session, db, user)
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
        elif choice == "w" and node_controls is not None:
            await session.write_line("")
            await _caller_who_screen(
                session, db, node_controls, user, hub, presence, direct_invites, lane, link_context=link_context
            )
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
        elif choice == "i" and list_pending_invitations_for_user(db, user):
            await session.write_line("")
            await _show_pending_invitations(session, db, user)
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
        elif choice == "v" and (user.can_verify_identity or meets_level(user, SYSOP_LEVEL)):
            await session.write_line("")
            await _verify_identity_menu(session, db, user)
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
        elif choice == "s" and meets_level(user, SYSOP_LEVEL):
            await session.write_line("")
            # design doc: admin is one of the features
            # migrated onto the two-lane database execution model -- see
            # the "e" (mail) branch above for the identical lane-is-None
            # degrade-gracefully reasoning. Keystroke is "s" (BBS
            # convention: the "SysOp" menu), not "a" -- Thiesi's own
            # explicit request, more in line with traditional BBS lingo
            # than a generic "Admin" label/letter.
            if lane is not None:
                await admin_menu(session, lane, user, node_controls=node_controls, link_context=link_context)
            else:
                await session.write_line(
                    colored("SysOp menu is not available in this context.", fg_color=MUTED_COLOR)
                )
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
        else:
            await session.write(reject_unhandled_key(choice))


async def _handle_incoming_invite(
    session: Session,
    db: Database,
    direct_invites: DirectChatInvites,
    hub: ChatHub,
    presence: PresenceRegistry,
    user: User,
) -> None:
    """
    Runs once `_main_menu`'s own read/invite race (design doc §6.3,
    that function's own docstring) resolves in favor of a pending
    direct-chat invite -- shows the accept/decline prompt, records the
    answer, and on acceptance runs `netbbs.net.chat_flow._direct_chat_
    loop` directly (the same "blocking screen call, returns when done"
    shape entering any other screen from `_main_menu` already has).

    `direct_invites.pending_for(session)` can legitimately return `None`
    here -- the arrival event fired, but the invite it signaled has
    since expired (the inviter's own 60s wait timed out) before this
    function got a chance to run, e.g. because this session was busy
    elsewhere the whole time and only just returned to the main menu.
    That's a safe no-op, not an error: there is nothing left to show.
    """
    invite = direct_invites.pending_for(session)
    if invite is None:
        return

    await session.write_line(
        colored(
            f"\r\n*** {sanitize_text(invite.inviter.username)} wants to start a direct chat. ***",
            fg_color=ALERT_COLOR, bold=True,
        )
    )
    accepted = await prompt_yes_no(session, "Accept?", default=False)
    if not direct_invites.respond(session, accepted=accepted):
        # Expired/cancelled between the prompt being shown and this
        # answer -- same "no longer valid" tolerance as everywhere else
        # in this feature (netbbs.chat.direct_invites's own docstrings).
        await session.write_line(colored("That invitation is no longer valid.", fg_color=MUTED_COLOR))
        return
    if accepted:
        await run_direct_chat_loop(
            session, hub, presence, user, invite.inviter, invite.room_token,
            redraw_in_place=redraw_in_place_enabled(db, user),
            unicode_style=unicode_style_enabled(db, user),
            collapsed=breadcrumb_collapsed_enabled(db, user),
            accent_color=effective_accent_color_256(db),
            header_color=effective_header_color_256(db),
        )
    else:
        await session.write_line(colored("Declined.", fg_color=MUTED_COLOR))


@dataclass(frozen=True)
class _ScanItem:
    """One row in issue #56's `[N]ew scan` picker -- a board, channel,
    or file area `user` can currently access, with its computed unread
    state and follow status. Built fresh on every screen entry, never
    persisted -- see `_new_scan_screen`'s own docstring for why
    `stable_id_of=lambda item: id(item)` is the correct idiom here."""

    kind: str  # "board" | "channel" | "file_area"
    name: str
    unread: int | None  # None = never visited, 0 = caught up, >0 = unread count
    followed: bool
    board: Board | None = None
    channel: Channel | None = None
    file_area: FileArea | None = None


async def _new_scan_screen(
    session: Session,
    db: Database,
    lane: DatabaseLane,
    hub: ChatHub,
    presence: PresenceRegistry,
    mailbox: MessageMailbox,
    history: InputHistory,
    user: User,
    *,
    link_context: LinkContext | None = None,
) -> None:
    """
    Issue #56's activity summary: every board/channel/file area `user`
    can currently access, each showing whether it's never been visited,
    fully caught up, or has unread activity -- plus a distinct "replies
    to you" section, always shown regardless of follow state (a reply
    is always worth surfacing). Followed items are listed first, but
    new scan itself always covers everything accessible, not only
    followed items -- matches the traditional meaning of a BBS
    "new scan" and avoids a brand-new account with nothing followed
    yet seeing an empty screen.

    Built fresh every time this screen is entered -- a plain Python
    list, never persisted -- so `stable_id_of=lambda item: id(item)`
    is the correct idiom (same as `_who_screen`'s sessions, or the
    Link status screen's in-memory peers), not a database id.

    Selecting a board/file area jumps straight to its first unread post/
    file via `initial_cursor`; selecting a channel enters it directly
    via `initial_channel`. Channels have no page concept to jump within
    (`get_scrollback` always replays the same bounded buffer), so
    entering one from here is just the ordinary join.
    """

    def _load(db: Database) -> tuple[list[_ScanItem], list[Post], dict[int, Board]]:
        items: list[_ScanItem] = []
        boards_by_id: dict[int, Board] = {}

        for board in list_boards(db):
            boards_by_id[board.id] = board
            if not (
                meets_level(user, get_effective_min_read_level(db, board))
                and meets_age(db, user, get_effective_min_age(db, board))
            ):
                continue
            items.append(
                _ScanItem(
                    kind="board", name=board.name, unread=unread_post_count(db, user, board),
                    followed=is_following(db, user, "board", board.id), board=board,
                )
            )

        for channel in list_visible_channels_for(db, user):
            items.append(
                _ScanItem(
                    kind="channel", name=channel.name, unread=unread_channel_count(db, user, channel),
                    followed=is_following(db, user, "channel", channel.id), channel=channel,
                )
            )

        for area in list_file_areas(db):
            if not (
                meets_level(user, get_effective_min_read_level(db, area))
                and meets_age(db, user, get_effective_min_age(db, area))
            ):
                continue
            items.append(
                _ScanItem(
                    kind="file_area", name=area.name, unread=unread_file_count(db, user, area),
                    followed=is_following(db, user, "file_area", area.id), file_area=area,
                )
            )

        # Followed items first; a stable sort preserves each source
        # list's own activity-based order within both groups.
        items.sort(key=lambda item: not item.followed)
        replies = unread_replies_to(db, user)
        return items, replies, boards_by_id

    items, replies, boards_by_id = await lane.run(_load)

    await session.write_line(colored("\r\nNew scan:", fg_color=effective_header_color(session, db), bold=True))
    if replies:
        await session.write_line(f"Replies to you: {len(replies)}")
        for reply in replies[:10]:
            reply_board = boards_by_id.get(reply.board_id)
            board_label = sanitize_text(reply_board.name) if reply_board is not None else "unknown message board"
            await session.write_line(f"  {sanitize_text(reply.subject)} ({board_label})")
        if len(replies) > 10:
            await session.write_line(f"  ...and {len(replies) - 10} more.")
    else:
        await session.write_line(colored("Replies to you: none.", fg_color=MUTED_COLOR))

    def _description(item: _ScanItem) -> str:
        prefix = "* " if item.followed else ""
        if item.unread is None:
            status = "not yet visited"
        elif item.unread == 0:
            status = "caught up"
        else:
            status = f"{item.unread} unread"
        return f"{prefix}{item.kind.replace('_', ' ')}, {status}"

    selected = await pick_item(
        session, items,
        name_of=lambda item: item.name,
        stable_id_of=lambda item: id(item),
        description_of=_description,
        title="New scan",
        empty_message="Nothing accessible yet.",
        redraw_in_place=redraw_in_place_enabled(db, user),
        unicode_style=unicode_style_enabled(db, user),
        collapsed=breadcrumb_collapsed_enabled(db, user),
        accent_color=effective_accent_color(session, db),
        header_color=effective_header_color(session, db),
    )
    if selected is None:
        return

    if selected.kind == "board":
        cursor = await lane.run(board_read_cursor, user, selected.board)
        await _show_board(session, db, selected.board, user, link_context=link_context, initial_cursor=cursor)
    elif selected.kind == "channel":
        await browse_channels(
            session, lane, hub, presence, mailbox, history, user,
            initial_channel=selected.channel, link_context=link_context,
        )
    else:
        cursor = await lane.run(file_area_read_cursor, user, selected.file_area)
        await enter_file_area(session, lane, selected.file_area, user, initial_cursor=cursor, link_context=link_context)


@dataclass(frozen=True)
class _SearchResultItem:
    """One row in issue #56's `[F]ind` results picker -- a matched post,
    file, or retained channel message, already filtered to what `user`
    can currently access (`search_posts`/`search_files`/
    `search_channel_messages`'s own authorization). Built fresh per
    query, never persisted.

    `result_index` (dogfood follow-up), not `id(item)`, is this item's
    `stable_id_of` -- a plain 1-based position in this one query's own
    result list. `root_post_id`/`file_id` are long content-addressed
    hash strings, not small integers a caller could ever type back into
    `pick_item`'s `[G]oto #` prompt (which is exactly why `_ScanItem`
    elsewhere in this module still uses the unreachable `id(item)`
    idiom -- there's no natural typeable id for those items either).
    Search results are a fixed, never-reordered, never-re-paginated
    list for the lifetime of one query, unlike a board/category
    listing `goto` is designed to keep working across -- so a plain
    per-query sequential number is a real, honest identifier here, not
    a leaky abstraction, and matches the `(#N)` reference already
    printed next to every row."""

    kind: str  # "post" | "file" | "channel_message"
    name: str
    description: str
    result_index: int
    post: PostSearchHit | None = None
    file: FileSearchHit | None = None
    message: ChannelMessageSearchHit | None = None


# A search result row renders as "  NN. (#N) name - description",
# colored_truncate()d to terminal_width -- front-to-back, so anything
# past the cutoff is dropped wholesale, not shortened (`netbbs.net.
# picker.pick_item`). A channel message's whole body -- and, since the
# same dogfood follow-up that added post/file snippets below, a
# post's/file's own matched body/description text -- would otherwise
# stand in as (or bloat) the name/description field with no budget left
# for the row prefix, the `(#N)` goto reference, and each other on an
# ordinary 80-column terminal. Trimmed to a scannable length that
# leaves real room for the rest of the row in the common case, same
# spirit as _ScanItem's "replies to you" list capping at 10 (a display
# shaping choice, unrelated to and separate from pick_item's own
# sanitize_text call, which still runs on whatever this produces).
_SEARCH_RESULT_SNIPPET_LENGTH = 20

# Mirrors netbbs.search.search_posts/search_files/search_channel_
# messages' own default `limit` -- passed explicitly (rather than
# relying on that default) so `_load` below can request one extra hit
# per category purely to detect truncation (dogfood follow-up), without
# the two ever silently drifting apart.
_SEARCH_RESULT_LIMIT = 20


def _search_snippet(text: str) -> str:
    text = text.strip()
    if len(text) <= _SEARCH_RESULT_SNIPPET_LENGTH:
        return text
    return text[:_SEARCH_RESULT_SNIPPET_LENGTH] + "..."


# Dogfood follow-up: `netbbs.search._match_expression`'s own docstring
# is explicit that "OR"/"AND"/"NOT" are deliberately never interpreted
# as FTS5 boolean operators -- every typed word is required and matched
# literally instead (so oddly formatted input can never raise a syntax
# error deep inside a MATCH clause). That's correct, intentional
# design, not a bug -- but a caller who tries `cats OR dogs` expecting
# an alternation gets an AND-of-three-literal-words query instead,
# which will essentially never match anything, and the plain "No
# matches" message gives no hint why. Investigated and confirmed live:
# quoting the term changes nothing here either (`"cats" OR dogs` fails
# identically to `cats OR dogs`) -- the standalone word is what matters,
# not any surrounding punctuation.
_BOOLEAN_LOOKING_WORDS = frozenset({"or", "and", "not"})


def _looks_like_attempted_boolean_syntax(query: str) -> bool:
    return any(token.lower() in _BOOLEAN_LOOKING_WORDS for token in query.split())


async def _find_screen(
    session: Session,
    db: Database,
    lane: DatabaseLane,
    hub: ChatHub,
    presence: PresenceRegistry,
    mailbox: MessageMailbox,
    history: InputHistory,
    user: User,
    *,
    link_context: LinkContext | None = None,
) -> None:
    """
    Issue #56's local search: prompts for one free-text query, then
    matches it against approved board posts (subject/body), approved
    files (filename/description), and retained channel scrollback
    (message body) -- `netbbs.search`'s three FTS5-backed queries, each
    already filtered to exactly what `user` can currently access (level/
    age/Community gates for boards and file areas, `netbbs.net.
    chat_flow.list_visible_channels_for` for channels -- the identical
    gates `_new_scan_screen` applies). Never touches Link: search only
    ever queries this node's own locally carried content, and the query
    text itself is never transmitted anywhere (see `netbbs.search`'s own
    module docstring).

    Selecting a hit jumps straight to it: a post/file lands on the exact
    matched item (`netbbs.search.post_jump_cursor`/`file_jump_cursor`,
    the immediately preceding item's own cursor, so the hit becomes the
    first thing shown) rather than just opening its board/area at the
    default newest page. A channel message instead just enters its
    channel -- channels have no "jump to one message" concept (unlike
    boards/files, scrollback is a bounded, revision-less ring buffer),
    the same limitation `_new_scan_screen`'s own channel dispatch
    already accepts.
    """
    await session.write_line(
        "\r\n" + screen_title(
            "Search",
            breadcrumb=(session.node_display_name,),
            subtitle="Find posts, files, and retained chat on this node.",
            width=session.terminal_width,
            clear=redraw_in_place_enabled(db, user),
            unicode_style=unicode_style_enabled(db, user), collapsed=breadcrumb_collapsed_enabled(db, user),
            header_color=effective_header_color_256(db), node_name_gradient=session.node_name_gradient)
    )
    await session.write("Search terms (Enter cancels): ")
    query = (await session.read_line()).strip()
    if not query:
        await session.write_line(colored("Search cancelled.", fg_color=MUTED_COLOR))
        return

    def _load(db: Database) -> tuple[list[_SearchResultItem], bool]:
        # Fetched one past the actual display cap, purely to detect
        # truncation (dogfood follow-up) -- a broad query used to
        # silently drop everything past the top `_SEARCH_RESULT_LIMIT`
        # per category with no indication anything was cut, distinct
        # from a genuine "no matches" empty state.
        next_index = 1
        items: list[_SearchResultItem] = []
        truncated = False

        post_hits = search_posts(db, user, query, limit=_SEARCH_RESULT_LIMIT + 1)
        truncated = truncated or len(post_hits) > _SEARCH_RESULT_LIMIT
        for hit in post_hits[:_SEARCH_RESULT_LIMIT]:
            items.append(
                _SearchResultItem(
                    kind="post", name=hit.subject,
                    description=f"[POST] {hit.board.name}: {_search_snippet(hit.body)}",
                    result_index=next_index, post=hit,
                )
            )
            next_index += 1

        file_hits = search_files(db, user, query, limit=_SEARCH_RESULT_LIMIT + 1)
        truncated = truncated or len(file_hits) > _SEARCH_RESULT_LIMIT
        for hit in file_hits[:_SEARCH_RESULT_LIMIT]:
            description = f"[FILE] {hit.area.name}"
            if hit.description:
                description += f": {_search_snippet(hit.description)}"
            items.append(
                _SearchResultItem(
                    kind="file", name=hit.filename, description=description,
                    result_index=next_index, file=hit,
                )
            )
            next_index += 1

        visible_channels = list_visible_channels_for(db, user)
        message_hits = search_channel_messages(
            db, user, query, visible_channels=visible_channels, limit=_SEARCH_RESULT_LIMIT + 1
        )
        truncated = truncated or len(message_hits) > _SEARCH_RESULT_LIMIT
        for hit in message_hits[:_SEARCH_RESULT_LIMIT]:
            items.append(
                _SearchResultItem(
                    kind="channel_message", name=_search_snippet(hit.body),
                    description=f"[CHAT] #{hit.channel.name} by {hit.author_label}",
                    result_index=next_index, message=hit,
                )
            )
            next_index += 1
        return items, truncated

    items, truncated = await lane.run(_load)
    if truncated:
        await session.write_line(
            colored(
                f"Showing the top {_SEARCH_RESULT_LIMIT} matches per category -- "
                "narrow your search terms for a complete list.",
                fg_color=MUTED_COLOR,
            )
        )

    # Loops back to the results list after viewing a hit (dogfood
    # follow-up), same "pick, view, pick again" shape `_browse_
    # directory`/mail's inbox/sent already use -- checking hit #2 of #5
    # is the whole point of search results specifically, more so than
    # this screen's own one-shot sibling `_new_scan_screen` (one pick
    # per resource *category*, not per hit within one query). Re-uses
    # the same already-fetched `items` rather than re-querying on every
    # loop -- the query text can't change mid-loop (there's no `[S]earch`
    # re-prompt wired to a new query here), so nothing to refresh.
    empty_message = "No matches. Try fewer or broader search terms."
    if _looks_like_attempted_boolean_syntax(query):
        empty_message = (
            'No matches. "OR"/"AND"/"NOT" are not search operators here -- '
            "every word you type is required and matched literally, so "
            "combining one of these with other terms can make a query "
            "impossible to satisfy. Try searching without them."
        )

    while True:
        selected = await pick_item(
            session, items,
            name_of=lambda item: item.name,
            stable_id_of=lambda item: item.result_index,
            description_of=lambda item: item.description,
            title=f"Search results for {query!r}",
            empty_message=empty_message,
            redraw_in_place=redraw_in_place_enabled(db, user),
            unicode_style=unicode_style_enabled(db, user),
            collapsed=breadcrumb_collapsed_enabled(db, user),
            accent_color=effective_accent_color(session, db),
            header_color=effective_header_color(session, db),
        )
        if selected is None:
            return

        if selected.kind == "post":
            cursor = await lane.run(post_jump_cursor, selected.post.board.id, selected.post.root_post_id)
            await _show_board(
                session, db, selected.post.board, user, link_context=link_context, initial_cursor=cursor
            )
        elif selected.kind == "file":
            cursor = await lane.run(file_jump_cursor, selected.file.area.id, selected.file.file_id)
            await enter_file_area(
                session, lane, selected.file.area, user, initial_cursor=cursor, link_context=link_context
            )
        else:
            await browse_channels(
                session, lane, hub, presence, mailbox, history, user,
                initial_channel=selected.message.channel, link_context=link_context,
            )


async def _login(
    session: Session,
    db: Database,
    throttle: LoginThrottle,
    *,
    max_attempts: int = _MAX_LOGIN_ATTEMPTS,
    idle_timeout: float,
) -> User | LoginOutcome:
    """
    Prompt for username/password up to `max_attempts` times.

    Returns the authenticated `User` on success, otherwise a named
    `LoginOutcome` so the caller can distinguish exhausted attempts from
    a successfully-authenticated account which is blocked.

    Password-only for now: keypair (challenge-response) login is fully
    implemented in the auth module already, but a plain Telnet client has
    no way to sign a challenge with a local private key — that path needs
    a NetBBS-aware client or a future API entry point, not this one.
    Flagging explicitly rather than silently only ever exercising half of
    what `netbbs.auth` supports.

    `max_attempts` is still per-connection only — reconnecting resets
    *this* counter, same as before issue #3. What's new is that it's no
    longer the only limit: `throttle.allow_attempt` below is
    cross-connection, node-lifetime state that reconnecting does not
    reset (see `netbbs.net.throttle.LoginThrottle`), which is what
    actually stops an attacker from working around the per-connection
    limit by reconnecting. A real persistent lockout/ban mechanism still
    belongs to §13's mute/ban system (Phase 2) — this is throttling, not
    that.

    Each prompt read is individually bounded by `idle_timeout`
    (`asyncio.wait_for` around one `read_line` call) — a client that
    stops sending mid-prompt doesn't hold a connection (and an
    unauthenticated-session budget slot, see `handle_session`) open
    forever. This is a *per-read* inactivity timeout, distinct from
    `handle_session`'s overall `login_deadline_seconds`, which bounds
    the whole login process even against a client that stays active but
    never actually finishes (see that function's docstring).

    The blocklist check happens *here*, after successful authentication,
    not inside `authenticate_password_async` itself — authentication ("are
    these credentials correct") and this kind of authorization ("is this
    correctly-authenticated account allowed to proceed") are different
    concerns, kept separate the same way `netbbs.permissions` is kept
    separate from `netbbs.auth`. It also can't happen any earlier: we
    need to know *who* successfully authenticated before we can check
    whether they're blocked.
    """
    registration_mode = get_registration_mode(db)
    await session.write_line(
        "\r\n"
        + screen_title(
            "Sign in",
            breadcrumb=(),
            subtitle=(
                None
                if registration_mode == RegistrationMode.CLOSED
                else "New here? Type 'new' to create an account."
            ),
            width=session.terminal_width,
            # Pre-authentication -- no account/preference to look up yet,
            # and every screen is already sent as UTF-8 regardless (see
            # `unicode_style_preference`'s own docstring), same reasoning
            # `_write_connection_notice` and the welcome banner both use.
            unicode_style=True,
            header_color=effective_header_color_256(db),
        )
    )
    prompt = colored("Username: ", fg_color=LABEL_COLOR, bold=True)

    for attempt in range(max_attempts):
        try:
            await session.write(prompt)
            username = (await asyncio.wait_for(session.read_line(), timeout=idle_timeout)).strip()
        except asyncio.TimeoutError:
            return LoginOutcome.IDLE_TIMEOUT
        if not username:
            continue

        if username.lower() == NEW_ACCOUNT_SENTINEL:
            # `closed` mode hides the registration option from
            # the prompt above, but 'new' is a documented, memorable
            # convention -- someone who already knows it and types it
            # anyway gets a clear, honest rejection rather than the
            # sentinel silently falling through to an ordinary (and
            # therefore always-failing) username lookup.
            if registration_mode == RegistrationMode.CLOSED:
                await session.write_line(
                    colored(
                        "This system does not accept public registrations. Contact the SysOp for an account.",
                        fg_color=WARNING_COLOR,
                    )
                )
                continue
            new_user = await _register_new_account(
                session, db, throttle, idle_timeout=idle_timeout, registration_mode=registration_mode
            )
            if new_user is not None:
                return new_user
            continue

        try:
            await session.write(colored("Password: ", fg_color=LABEL_COLOR, bold=True))
            password = await asyncio.wait_for(session.read_line(echo=False), timeout=idle_timeout)
        except asyncio.TimeoutError:
            return LoginOutcome.IDLE_TIMEOUT
        # No explicit blank-line write needed here anymore — read_line()
        # now writes its own trailing CRLF after Enter unconditionally
        # (part of character-mode input; see netbbs.net.telnet), whereas
        # the original line-mode implementation relied on the client's
        # own local echo to show that newline and needed this line to
        # compensate. Leaving it in would now print an extra blank line.

        if not throttle.allow_attempt(source=session.peer_address, username=username):
            # Rejected before the expensive Argon2 work runs at all — see
            # LoginThrottle.allow_attempt's docstring for why the check
            # happens before, not after, authenticate_password_async.
            return LoginOutcome.THROTTLED

        try:
            user = await authenticate_password_async(db, username, password)
        except AuthError:
            remaining = max_attempts - attempt - 1
            if remaining > 0:
                await session.write_line(
                    colored(
                        f"Login failed. {remaining} attempt(s) remaining.",
                        fg_color=ERROR_COLOR,
                    )
                )
            else:
                await session.write_line(colored("Login failed.", fg_color=ERROR_COLOR))
            continue

        if is_blocked(db, user):
            # A distinct message from the generic "Login failed" above is
            # deliberate, not an information leak: this user has already
            # proven who they are via successful authentication, unlike
            # an anonymous prober still guessing passwords, so there's no
            # username-enumeration concern in telling them specifically
            # why they can't proceed.
            await session.write_line(
                colored("Your access to this system has been revoked.", fg_color=ERROR_COLOR)
            )
            return LoginOutcome.BLOCKED

        return user

    return LoginOutcome.ATTEMPTS_EXHAUSTED


# A caller reaches signup only by deliberately typing the three-character
# `new` sentinel -- see `_register_new_account`'s own docstring -- so a
# fixable validation mistake gets this many tries in place before falling
# back to the ordinary login prompt.
_REGISTRATION_MAX_ATTEMPTS = 3


async def _offer_signup_retry(session: Session, attempt: int, max_attempts: int) -> bool:
    """After a fixable signup validation failure, either announce another
    attempt is starting (returns `True`, caller should loop) or, once
    `max_attempts` is reached, send the caller back to the ordinary login
    prompt with the same explicit "how to get back here" wording issue
    #156 established (returns `False`, caller should stop)."""
    if attempt < max_attempts:
        await session.write_line(colored("Let's try again.", fg_color=MUTED_COLOR))
        return True
    await session.write_line(
        colored(
            "Returning you to the login prompt -- type 'new' again to retry signing up.",
            fg_color=MUTED_COLOR,
        )
    )
    return False


async def _register_new_account(
    session: Session,
    db: Database,
    throttle: LoginThrottle,
    *,
    idle_timeout: float,
    registration_mode: RegistrationMode,
) -> User | None:
    """
    Self-service account registration (design doc), entered by
    typing the reserved username `new` (`netbbs.auth.users.
    NEW_ACCOUNT_SENTINEL`) at `_login`'s ordinary username prompt -- the
    same sentinel SSH's keyboard-interactive registration
    (`netbbs.net.ssh._NetBBSSSHServer`) triggers, so every transport
    shares one discoverable "how do I sign up" answer. `_login` never
    calls this at all when `registration_mode` is `CLOSED` --
    this function only ever runs for `OPEN`/`APPROVAL_REQUIRED`.

    Returns the freshly created `User` only when the account can log in
    immediately (`registration_mode` is `OPEN`); `None` for every other
    outcome -- cancelled, a validation
    failure, throttled, or created-but-pending-approval. `_login`
    treats `None` as "go back to the username prompt", consuming one of
    the connection's `max_attempts` the same way a failed login would.
    That's a deliberate simplification rather than plumbing a separate
    registration-attempt budget through `_login`'s return type -- a
    failed/cancelled registration attempt is throttled the same way a
    failed login attempt already is (both per-connection here, and
    cross-connection via `throttle` below).

    Password-only, like `_login` itself -- a plain Telnet/web client has
    the same "no way to sign a keypair challenge" limitation `_login`'s
    own docstring already explains, so self-registration never offers a
    keypair option here (an account can still gain one later via the
    admin screen, if a SysOp adds it by hand).

    Retries in place, up to `_REGISTRATION_MAX_ATTEMPTS` times, on any of
    the three *fixable* validation failures (password too short,
    passwords didn't match, or the desired username is already taken) --
    dogfood follow-up to issue #156, which only fixed the message wording
    for a failure that used to always drop straight back to the login
    prompt. Typing the `new` sentinel at the login prompt is a
    deliberate, three-keystroke choice, not something a caller falls
    into by accident -- a mistyped password or a username someone else
    already has doesn't change that intent, so restarting this whole
    username/password/confirm mini-workflow in place is the right
    response, not ejecting them back to Username: on the first typo. A
    blank username (an explicit cancel) or an idle timeout (the caller
    left) still exit immediately regardless of which attempt this is --
    neither is a "fixable mistake" to retry.

    Shows `new_account_banner_before`'s own optional SysOp banner exactly
    once, right here -- before the very first attempt, not repeated on
    every retry (GitHub issue #177's own scoping decision): a fixable
    typo shouldn't replay decorative art on every loop iteration. Shows
    `new_account_banner_after`'s own banner once account creation
    actually succeeds, covering *both* successful outcomes (immediate
    login and pending-approval) -- see that call site below.
    """
    before_banner = load_new_account_banner_before(db)
    if before_banner:
        await session.write_line(before_banner)
    for attempt in range(1, _REGISTRATION_MAX_ATTEMPTS + 1):
        await session.write_line(
            "\r\n"
            + screen_title(
                "Create account",
                breadcrumb=(),
                subtitle=(
                    "Choose your credentials. A blank username cancels. "
                    f"(Attempt {attempt} of {_REGISTRATION_MAX_ATTEMPTS})"
                ),
                width=session.terminal_width,
                unicode_style=True,  # pre-authentication -- see _write_connection_notice
                header_color=effective_header_color_256(db),
            )
        )
        try:
            await session.write(colored("Desired username: ", fg_color=LABEL_COLOR, bold=True))
            username = (await asyncio.wait_for(session.read_line(), timeout=idle_timeout)).strip()
            if not username:
                return None

            await session.write(
                colored(
                    f"Password (min {MIN_REGISTRATION_PASSWORD_LENGTH} characters): ",
                    fg_color=LABEL_COLOR,
                    bold=True,
                )
            )
            password = await asyncio.wait_for(session.read_line(echo=False), timeout=idle_timeout)
            await session.write(colored("Confirm password: ", fg_color=LABEL_COLOR, bold=True))
            confirm = await asyncio.wait_for(session.read_line(echo=False), timeout=idle_timeout)
        except asyncio.TimeoutError:
            return None

        if len(password) < MIN_REGISTRATION_PASSWORD_LENGTH:
            await session.write_line(
                colored(
                    f"Password must be at least {MIN_REGISTRATION_PASSWORD_LENGTH} characters.",
                    fg_color=ERROR_COLOR,
                )
            )
            if not await _offer_signup_retry(session, attempt, _REGISTRATION_MAX_ATTEMPTS):
                return None
            continue
        if password != confirm:
            await session.write_line(colored("Passwords did not match.", fg_color=ERROR_COLOR))
            if not await _offer_signup_retry(session, attempt, _REGISTRATION_MAX_ATTEMPTS):
                return None
            continue

        # Same node-wide budget _login's own password attempts consume
        # (issue #3) -- keyed by the *desired* username rather than an
        # authenticating one, but the same per-source/per-username/global
        # token buckets, checked before the expensive Argon2 hash below runs
        # (create_user_async), for the identical reason _login checks it
        # before authenticate_password_async. Not one of the fixable
        # checks above: retrying *immediately* against a throttle that
        # just rejected this source/username wouldn't help, so this
        # drops straight back to login rather than consuming another of
        # this signup's own three attempts.
        if not throttle.allow_attempt(source=session.peer_address, username=username):
            await session.write_line(
                colored("Too many registration attempts. Please try again later.", fg_color=ERROR_COLOR)
            )
            return None

        require_approval = registration_mode == RegistrationMode.APPROVAL_REQUIRED
        try:
            new_user = await create_user_async(db, username, password=password, pending_approval=require_approval)
        except AuthError as exc:
            await session.write_line(colored(f"Could not create account: {exc}", fg_color=ERROR_COLOR))
            if not await _offer_signup_retry(session, attempt, _REGISTRATION_MAX_ATTEMPTS):
                return None
            continue

        # Dogfood report: three testers on modern (ANSI-capable) clients
        # never discovered in-place redraw existed, so never turned it
        # on. New accounts now start with it already on -- rather than
        # flipping `redraw_in_place_enabled`'s own resolve default,
        # which would silently change behavior for every existing
        # account with an unset preference too, not just new ones.
        set_redraw_in_place_enabled(db, new_user, True)
        redraw_notice = colored(
            "In-place redraw is on by default -- turn it off anytime in Your profile if you'd rather scroll.",
            fg_color=MUTED_COLOR,
        )

        # GitHub issue #177: covers both successful outcomes below (an
        # account created and immediately usable, or created but pending
        # SysOp approval) -- both are "signup completed successfully"
        # from the caller's own perspective, just with different next
        # steps, which the existing distinct messages right after this
        # still convey. Never shown for a validation failure/cancel --
        # those `continue`/`return None` above this point, never reaching
        # here.
        after_banner = load_new_account_banner_after(db)
        if after_banner:
            await session.write_line(after_banner)

        if require_approval:
            await session.write_line(
                colored(
                    f"Account {new_user.username!r} created. A SysOp must approve it before you can log in.",
                    fg_color=WARNING_COLOR,
                    bold=True,
                )
            )
            await session.write_line(redraw_notice)
            return None

        await session.write_line(
            colored(f"Account {new_user.username!r} created.", fg_color=SUCCESS_COLOR, bold=True)
        )
        await session.write_line(redraw_notice)
        return new_user
    return None  # unreachable: the loop's last iteration always returns via _offer_signup_retry


# -- Communities navigation (design doc §16) ------------


def _visible_communities_for(db: Database, user: User) -> list[Community]:
    """Every Community `user` is allowed to see. A `hidden` Community is
    delisted from ordinary browsing -- same "listed/hidden" visibility
    language the design doc's own text reuses -- except for a
    SysOp, who still sees everything here, matching every other admin-
    visibility bypass already established in this codebase (e.g.
    `netbbs.moderation.roles.has_permission`'s own SysOp bypass)."""
    communities = list_communities(db)
    if meets_level(user, SYSOP_LEVEL):
        return communities
    return [c for c in communities if not c.hidden]


def _has_visible_communities(db: Database, user: User) -> bool:
    return bool(_visible_communities_for(db, user))


def _has_uncategorized_resources(db: Database, user: User) -> bool:
    """Whether `user` can currently see at least one Uncategorized
    board, channel, or file area -- gates the main menu's `[U]ncategorized`
    entry the same "only offer what currently applies" way `[I]nvitations`
    already does. `community_id=None, community_scoped=True` filters
    each resource type to exactly its Uncategorized members -- see
    `_browse_boards_in_category`'s docstring for why `None` needs no
    special-casing here."""
    return (
        _has_visible_boards(db, user, community_id=None, community_scoped=True)
        or has_visible_channels(db, user, community_id=None, community_scoped=True)
        or has_visible_areas(db, user, community_id=None, community_scoped=True)
    )


def _menu_row(entries: list[MenuEntry], *, width: int, height: int, description_level: str) -> str:
    """Compact `action_bar` packing when descriptions are off, `menu_grid`'s
    taller one-entry-per-line layout once the caller has opted into "brief"/
    "detailed" (issue #160's rollout) -- see `netbbs.net.resource_editor.
    edit_resource_draft`'s identical branch for why `menu_grid` alone isn't a
    byte-for-byte substitute for `action_bar`'s packed row at the off level."""
    if description_level == "off":
        return action_bar([e.label for e in entries], width=width)
    return menu_grid([("", entries)], width=width, height=height, description_level=description_level)


async def _resource_type_menu(
    session: Session,
    db: Database,
    hub: ChatHub,
    presence: PresenceRegistry,
    mailbox: MessageMailbox,
    history: InputHistory,
    user: User,
    *,
    node_controls: NodeControls | None,
    community_id: int | None,
    community_scoped: bool,
    menu_header: str,
    title_prefix: str | None,
    lane: DatabaseLane | None = None,
    link_context: LinkContext | None = None,
    direct_invites: DirectChatInvites | None = None,
) -> None:
    """
    Shared sub-menu for `[C]ommunities`/`[U]ncategorized`/`[J]ump to...`
    (design doc §16) -- all three main-menu entry points lead
    here, differing only in what Community filter they apply. Reuses the
    *original* `[M]/[C]/[F]` letters one level in rather than inventing
    new ones -- caught during design: `[B]oards` collides with `[B]ack`
    -- so existing muscle memory is relocated one screen deeper, not
    lost.

    Offers only resource types with at least one currently-visible
    match when `community_scoped` (same "only offer what currently
    applies" convention as `[I]nvitations`); the unfiltered Jump case
    (`community_scoped=False`) always offers all three, matching the
    flat main menu's own former unconditional `[M]/[C]/[F]` behavior
    exactly -- Jump is meant to feel identical to how browsing used to
    work before Communities existed.

    Loops rather than a one-shot dispatch, same shape as `_main_menu`
    itself -- staying within one Community's (or Uncategorized's, or
    Jump's) context across several resource-type visits without
    re-entering the Community picker each time.
    """
    description_level = menu_description_level(db, user)
    redraw_in_place = redraw_in_place_enabled(db, user)
    unicode_style = unicode_style_enabled(db, user)
    collapsed = breadcrumb_collapsed_enabled(db, user)
    while True:
        show_boards = not community_scoped or _has_visible_boards(
            db, user, community_id=community_id, community_scoped=community_scoped
        )
        show_channels = not community_scoped or has_visible_channels(
            db, user, community_id=community_id, community_scoped=community_scoped
        )
        show_areas = not community_scoped or has_visible_areas(
            db, user, community_id=community_id, community_scoped=community_scoped
        )
        show_doors = not community_scoped or has_visible_doors(
            db, user, community_id=community_id, community_scoped=community_scoped
        )

        option_list = []
        if show_boards:
            option_list.append(MenuEntry(label=menu_key("M", "essage Boards"), brief="Browse message boards"))
        if show_channels:
            option_list.append(MenuEntry(label=menu_key("C", "hat"), brief="Browse chat channels"))
        if show_areas:
            option_list.append(MenuEntry(label=menu_key("F", "ile areas"), brief="Browse file areas"))
        if show_doors:
            option_list.append(MenuEntry(label=menu_key("G", "ames"), brief="Play a door game"))
        option_list.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to the previous menu"))
        heading = screen_title(
            menu_header,
            breadcrumb=(session.node_display_name, "Communities") if community_scoped else ("NetBBS",),
            subtitle="Choose a space to explore",
            width=session.terminal_width,
            clear=redraw_in_place,
            unicode_style=unicode_style, collapsed=collapsed,
            header_color=effective_header_color_256(db),
        node_name_gradient=session.node_name_gradient)
        await session.write_line(f"\r\n{heading}")
        await session.write_line(
            f"\r\n{_menu_row(option_list, width=session.terminal_width, height=session.terminal_height, description_level=description_level)}"
        )
        await session.write("Choice: ")

        choice = (await session.read_key()).lower()
        if choice == "b":
            await session.write_line("")
            return
        elif choice == "m" and show_boards:
            await session.write_line("")
            await _browse_boards(
                session, db, user,
                community_id=community_id, community_scoped=community_scoped, title_prefix=title_prefix,
                link_context=link_context,
            )
        elif choice == "c" and show_channels:
            await session.write_line("")
            # design doc: chat is one of the features migrated
            # onto the two-lane database execution model -- see the "e"
            # (mail) branch above for the identical lane-is-None
            # degrade-gracefully reasoning.
            if lane is not None:
                session_registry = node_controls.session_registry if node_controls is not None else None
                await browse_channels(
                    session, lane, hub, presence, mailbox, history, user, session_registry=session_registry,
                    community_id=community_id, community_scoped=community_scoped, title_prefix=title_prefix,
                    link_context=link_context, direct_invites=direct_invites,
                )
            else:
                await session.write_line(
                    colored("Chat is not available in this context.", fg_color=MUTED_COLOR)
                )
        elif choice == "f" and show_areas:
            await session.write_line("")
            # design doc: file areas are one of the features
            # migrated onto the two-lane database execution model -- see
            # the "e" (mail) branch above for the identical lane-is-None
            # degrade-gracefully reasoning.
            if lane is not None:
                await browse_file_areas(
                    session, lane, user,
                    community_id=community_id, community_scoped=community_scoped, title_prefix=title_prefix,
                    link_context=link_context,
                )
            else:
                await session.write_line(
                    colored("File areas are not available in this context.", fg_color=MUTED_COLOR)
                )
        elif choice == "g" and show_doors:
            await session.write_line("")
            # design doc: doors are one of the features migrated onto
            # the two-lane database execution model from the start (see
            # netbbs.net.door_flow's own docstring) -- see the "e" (mail)
            # branch above for the identical lane-is-None
            # degrade-gracefully reasoning.
            if lane is not None:
                await browse_doors(
                    session, lane, user,
                    community_id=community_id, community_scoped=community_scoped, title_prefix=title_prefix,
                )
            else:
                await session.write_line(
                    colored("Doors are not available in this context.", fg_color=MUTED_COLOR)
                )
        else:
            await session.write(reject_unhandled_key(choice))


async def _enter_communities(
    session: Session,
    db: Database,
    hub: ChatHub,
    presence: PresenceRegistry,
    mailbox: MessageMailbox,
    history: InputHistory,
    user: User,
    *,
    node_controls: NodeControls | None,
    lane: DatabaseLane | None = None,
    link_context: LinkContext | None = None,
    direct_invites: DirectChatInvites | None = None,
) -> None:
    """`[C]ommunities` entry point -- pick one via the shared picker,
    then the shared resource-type sub-menu scoped to it."""
    communities = _visible_communities_for(db, user)
    selected = await pick_item(
        session, communities,
        name_of=lambda c: c.name,
        stable_id_of=lambda c: c.id,
        description_of=lambda c: c.description,
        title="Communities",
        empty_message="No Communities exist yet.",
        redraw_in_place=redraw_in_place_enabled(db, user),
        unicode_style=unicode_style_enabled(db, user),
        collapsed=breadcrumb_collapsed_enabled(db, user),
        accent_color=effective_accent_color(session, db),
        header_color=effective_header_color(session, db),
    )
    if selected is None:
        return
    await _resource_type_menu(
        session, db, hub, presence, mailbox, history, user, node_controls=node_controls,
        community_id=selected.id, community_scoped=True,
        menu_header=selected.name, title_prefix=selected.name, lane=lane, link_context=link_context,
        direct_invites=direct_invites,
    )


async def _enter_uncategorized(
    session: Session,
    db: Database,
    hub: ChatHub,
    presence: PresenceRegistry,
    mailbox: MessageMailbox,
    history: InputHistory,
    user: User,
    *,
    node_controls: NodeControls | None,
    lane: DatabaseLane | None = None,
    link_context: LinkContext | None = None,
    direct_invites: DirectChatInvites | None = None,
) -> None:
    """`[U]ncategorized` entry point -- straight into the shared
    resource-type sub-menu, no picker needed (there's only one
    Uncategorized "bucket")."""
    await _resource_type_menu(
        session, db, hub, presence, mailbox, history, user, node_controls=node_controls,
        community_id=None, community_scoped=True,
        menu_header="Uncategorized", title_prefix="Uncategorized", lane=lane, link_context=link_context,
        direct_invites=direct_invites,
    )


async def _jump_to(
    session: Session,
    db: Database,
    hub: ChatHub,
    presence: PresenceRegistry,
    mailbox: MessageMailbox,
    history: InputHistory,
    user: User,
    *,
    node_controls: NodeControls | None,
    lane: DatabaseLane | None = None,
    link_context: LinkContext | None = None,
    direct_invites: DirectChatInvites | None = None,
) -> None:
    """`[J]ump to...` entry point -- the shared resource-type sub-menu
    with no Community filter at all (`community_scoped=False`), reusing
    the existing search/goto commands against the full,
    unfiltered list exactly as browsing worked before Communities
    existed (design doc §16). `title_prefix=None` keeps every
    browse function's title exactly as it always was ("Available
    message boards", etc.) rather than prefixing it."""
    await _resource_type_menu(
        session, db, hub, presence, mailbox, history, user, node_controls=node_controls,
        community_id=None, community_scoped=False,
        menu_header="Jump to...", title_prefix=None, lane=lane, link_context=link_context,
        direct_invites=direct_invites,
    )


async def _browse_boards(
    session: Session,
    db: Database,
    user: User,
    *,
    community_id: int | None = None,
    community_scoped: bool = False,
    title_prefix: str | None = None,
    link_context: LinkContext | None = None,
) -> None:
    """Entry point: browse from the top level (no category selected yet)."""
    await _browse_boards_in_category(
        session, db, user, category_id=None,
        community_id=community_id, community_scoped=community_scoped, title_prefix=title_prefix,
        link_context=link_context,
    )


def _has_visible_boards(db: Database, user: User, *, community_id: int | None, community_scoped: bool) -> bool:
    """Whether `user` can see at least one board under the given
    Community filter -- backs the shared resource-type sub-menu's
    "only offer what currently applies" conditional visibility (design
    doc §16), same convention as `[I]nvitations`."""
    boards = [
        b for b in list_boards(db)
        if meets_level(user, get_effective_min_read_level(db, b)) and meets_age(db, user, get_effective_min_age(db, b))
    ]
    if community_scoped:
        boards = [b for b in boards if b.community_id == community_id]
    return bool(boards)


async def _browse_boards_in_category(
    session: Session,
    db: Database,
    user: User,
    *,
    category_id: int | None,
    community_id: int | None = None,
    community_scoped: bool = False,
    title_prefix: str | None = None,
    link_context: LinkContext | None = None,
) -> None:
    """
    Browse boards within a category (or the top level, if `category_id`
    is `None`), picking via the shared picker (`netbbs.net.picker`)
    instead of typing exact names — see design doc phasing sign-off notes
    for why. Directly answers a real usability problem: a flat list mixes
    unrelated topics together (e.g. one politics board sitting in the
    middle of a dozen vintage-computing boards under any sort order),
    which categories are meant to fix.

    Categories and boards are shown together in one mixed list — pick a
    category to drill in (recursing into this same function, naturally
    capped at two levels since a sub-category has no further
    sub-categories to recurse into), or pick a board directly to open it.
    Falls back to a flat board-only list at any level with no categories,
    identical to the pre-category browsing experience.

    One correctness detail: `Category` and `Board` rows come from
    different tables, so their database IDs can collide (both start at
    1) — mixed into one picker call, that would make `goto` ambiguous
    between two different things sharing the same displayed number.
    Disambiguated by negating category IDs for picker purposes only
    (`-item.id`) — boards keep their real, positive ID unchanged, so
    existing board `goto` numbers aren't affected by this at all.

    `community_id`/`community_scoped` (design doc §16) narrow
    browsing to one Community's boards (`community_scoped=True`,
    `community_id=X`), Uncategorized boards (`community_scoped=True`,
    `community_id=None` -- `board.community_id == None` filters
    identically to the real-Community case, no special-casing needed),
    or no filter at all (`community_scoped=False`, the default --
    every existing caller's unchanged behavior, and what `[J]ump to...`
    uses). `title_prefix`, threaded alongside, is `None` for the
    unfiltered/Jump case (keeping today's unchanged "Available message
    boards" title) or a human label ("Uncategorized", a Community's own
    name) that's passed to `pick_item` as an ancestor `breadcrumb`
    segment otherwise, so it renders muted with only "Message boards"
    itself in the current-location color -- not folded into the title
    text as a fake, uniformly-colored breadcrumb (dogfood-reported bug,
    see `pick_item`'s own `breadcrumb` docstring).
    Category leak prevention ("only show/offer categories
    currently used by ≥1 resource in this Community") only applies when
    `community_scoped` -- the unfiltered Jump path shows every category
    exactly as it always has.

    Sort mode (design doc, dogfood feature request): unlike
    `netbbs.net.chat_flow._pick_channel`, `list_boards` already
    supports every mode (`"activity"`/`"alphabetical"`/`"recent"`/
    `"volume"`) directly against real, persisted columns -- no
    in-memory hub state to separately combine in, so a mode switch is
    just re-calling `_load` with a different `order_by`.
    `get_effective_sort_mode` resolves against this call's own
    `category_id`/Community scope, exactly the scope the `[O]rder`
    command's own save-scope prompt offers. This module has no
    `DatabaseLane` (unlike chat's long-running loop, board browsing
    here just calls `Database` directly), so persistence is a plain
    synchronous `set_sort_preference` call wrapped in an async closure
    for `netbbs.net.sort_ui.prompt_sort_change`'s own `persist` seam.
    """
    # name_requirement deliberately does not gate reading here -- it's a
    # participation/accountability requirement (design doc §18 point 7:
    # "mutual visible accountability" among people posting), not a
    # content-restriction the way min_age is; see can_post's own check,
    # below, for where it actually applies.
    effective_community_id = community_id if community_scoped else None
    # GitHub issue #176: resolved once, reused for both pick_item calls
    # below (flat and mixed-with-categories) -- shows at every level of
    # board browsing this recursive function reaches (top level, a
    # category, a Community/Uncategorized scope), not only the very
    # first unfiltered screen, matching this feature's own scoping
    # decision.
    board_masthead = load_board_list_banner(db)

    def _load(order_by: str) -> tuple[list[Board], list[Category]]:
        all_boards = [
            b for b in list_boards(db, order_by=order_by)
            if meets_level(user, get_effective_min_read_level(db, b))
            and meets_age(db, user, get_effective_min_age(db, b))
        ]
        if community_scoped:
            all_boards = [b for b in all_boards if b.community_id == community_id]
        boards_here = [b for b in all_boards if b.category_id == category_id]

        categories_here = (
            list_top_level_categories(db) if category_id is None else list_subcategories(db, category_id)
        )
        if community_scoped:
            used_category_ids = {b.category_id for b in all_boards if b.category_id is not None}
            if category_id is None:
                categories_here = [
                    c for c in categories_here
                    if c.id in used_category_ids
                    or any(sub.id in used_category_ids for sub in list_subcategories(db, c.id))
                ]
            else:
                categories_here = [c for c in categories_here if c.id in used_category_ids]
        return boards_here, categories_here

    current_mode = get_effective_sort_mode(
        db, user, "board", community_id=effective_community_id, category_id=category_id
    )
    boards_here, categories_here = _load(current_mode)
    category_name = get_board_category_by_id(db, category_id).name if category_id is not None else None
    community = get_community(db, effective_community_id)
    community_name = community.name if community is not None else None
    mode_box = {"mode": current_mode}

    async def _persist_sort_choice(mode: str, scope_kwargs: dict) -> None:
        set_sort_preference(db, user, "board", mode, **scope_kwargs)

    async def _run_sort_prompt() -> str | None:
        return await prompt_sort_change(
            session, persist=_persist_sort_choice,
            community_id=effective_community_id, community_name=community_name,
            category_id=category_id, category_name=category_name,
        )

    def _sort_label() -> str:
        return SORT_MODE_LABELS[mode_box["mode"]]

    unicode_style = unicode_style_enabled(db, user)
    collapsed = breadcrumb_collapsed_enabled(db, user)
    redraw_in_place = redraw_in_place_enabled(db, user)
    accent_color = effective_accent_color(session, db)
    header_color = effective_header_color(session, db)
    title = "Message boards" if title_prefix is not None else "Available message boards"
    picker_breadcrumb = (title_prefix,) if title_prefix is not None else ()

    if not categories_here:
        async def on_sort_flat() -> list[Board] | None:
            new_mode = await _run_sort_prompt()
            if new_mode is None:
                return None
            mode_box["mode"] = new_mode
            new_boards, _ = _load(new_mode)
            return new_boards

        board = await pick_item(
            session,
            boards_here,
            name_of=lambda b: b.name,
            stable_id_of=lambda b: b.id,
            description_of=lambda b: b.description,
            title=title,
            breadcrumb=picker_breadcrumb,
            empty_message="No message boards are available to you yet.",
            on_sort=on_sort_flat,
            sort_label=_sort_label,
            redraw_in_place=redraw_in_place,
            unicode_style=unicode_style,
            collapsed=collapsed,
            accent_color=accent_color,
            header_color=header_color,
            masthead=board_masthead,
        )
        if board is not None:
            await _show_board(session, db, board, user, link_context=link_context)
        return

    mixed: list[Category | Board] = [*categories_here, *boards_here]

    def render_name(item: Category | Board) -> str:
        return f"[{item.name}]" if isinstance(item, Category) else item.name

    def render_description(item: Category | Board) -> str | None:
        if isinstance(item, Category):
            return item.description or "(category)"
        return item.description

    def stable_id(item: Category | Board) -> int:
        return item.id if isinstance(item, Board) else -item.id

    async def on_sort_mixed() -> list[Category | Board] | None:
        new_mode = await _run_sort_prompt()
        if new_mode is None:
            return None
        mode_box["mode"] = new_mode
        new_boards, _ = _load(new_mode)
        return [*categories_here, *new_boards]

    selected = await pick_item(
        session,
        mixed,
        name_of=render_name,
        stable_id_of=stable_id,
        on_sort=on_sort_mixed,
        sort_label=_sort_label,
        description_of=render_description,
        title=title,
        breadcrumb=picker_breadcrumb,
        empty_message="No message boards are available to you yet.",
        redraw_in_place=redraw_in_place,
        unicode_style=unicode_style,
        collapsed=collapsed,
        accent_color=accent_color,
        header_color=header_color,
        masthead=board_masthead,
    )
    if selected is None:
        return

    if isinstance(selected, Category):
        await _browse_boards_in_category(
            session, db, user, category_id=selected.id,
            community_id=community_id, community_scoped=community_scoped, title_prefix=title_prefix,
            link_context=link_context,
        )
    else:
        await _show_board(session, db, selected, user, link_context=link_context)


def _can_edit_post(db: Database, post: Post, user: User) -> bool:
    """The post's own original author, no grant needed, or anyone
    holding `BoardPermission.EDIT` -- the exact same authorization
    `netbbs.boards.posts.edit_post` itself enforces, checked here too
    so `[E]dit` only offers itself when it would actually succeed,
    rather than letting a SysOp compose a whole edit only to be
    rejected at the very end. `False` for an already-tombstoned post
    (design doc §9.5, issue #88) -- `edit_post` itself refuses those too."""
    if post.tombstoned_at is not None:
        return False
    return post.author_user_id == user.id or has_permission(
        db, user, object_type="board", object_id=post.board_id, permission=BoardPermission.EDIT
    )


def _can_tombstone_post(db: Database, post: Post, user: User) -> bool:
    """`BoardPermission.DELETE`, no author bypass (design doc §9.5,
    issue #88) -- the exact same authorization `netbbs.boards.posts.
    tombstone_post` itself enforces, checked here so `[T]ombstone` only
    offers itself when it would actually succeed. `False` for an
    already-tombstoned post."""
    if post.tombstoned_at is not None:
        return False
    return has_permission(db, user, object_type="board", object_id=post.board_id, permission=BoardPermission.DELETE)


async def _render_board_page(
    session: Session,
    db: Database,
    board_name: str,
    page: PostPage,
    user: User,
    *,
    can_post: bool,
    name_requirement: str | None,
    description_level: str,
    redraw_in_place: bool,
    unicode_style: bool = False,
    collapsed: bool = False,
) -> None:
    """Renders one page of posts plus its navigation options — the unit
    that should be redrawn on an actual page change (initial entry,
    Older/Newer/Recent), not on every loop iteration regardless of
    whether anything changed."""
    await _render_post_page(
        session, db, board_name, page, user, name_requirement=name_requirement, redraw_in_place=redraw_in_place,
        unicode_style=unicode_style, collapsed=collapsed,
    )
    options = []
    if page.has_older:
        options.append(MenuEntry(label=menu_key("O", "lder"), brief="Show older posts"))
    if page.has_newer:
        options.append(MenuEntry(label=menu_key("N", "ewer"), brief="Show newer posts"))
        options.append(MenuEntry(label=menu_key("R", "ecent"), brief="Jump to the newest page"))
    if any(_can_edit_post(db, post, user) for post in page.posts):
        options.append(MenuEntry(label=menu_key("E", "dit"), brief="Edit one of your posts"))
    if any(_can_tombstone_post(db, post, user) for post in page.posts):
        options.append(MenuEntry(label=menu_key("T", "ombstone"), brief="Remove a post, leave a marker"))
    if can_post:
        options.append(MenuEntry(label=menu_key("P", "ost"), brief="Write a new post"))
    options.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to the previous menu"))
    await session.write_line(
        f"\r\n{_menu_row(options, width=session.terminal_width, height=session.terminal_height, description_level=description_level)}"
    )
    await session.write("Choice: ")


async def _show_board(
    session: Session,
    db: Database,
    board: Board,
    user: User,
    *,
    link_context: LinkContext | None = None,
    initial_cursor: tuple[str, str] | None = None,
) -> None:
    """
    Show `board`, one bounded page of posts at a time (design doc,
    issue #10) — never the whole board, however large its history.

    Opens on the *newest* page, confirmed with Thiesi over keeping the
    old oldest-first default: an active board's most recent activity is
    what's actually useful to see on arrival, not its oldest history —
    directly answers the original complaint that returning to a board
    re-rendered everything, most of which was already read. `initial_
    cursor` (issue #56's `[N]ew scan` "jump to first unread"), if given,
    overrides this just for the very first render -- opens on the page
    immediately *after* that cursor instead of the newest page; every
    later Older/Newer/Recent navigation in this same call is unaffected.

    Composing a new post is a first-class `[P]ost` menu option inside
    the browsing loop (GitHub issue #40), not something a `[B]ack`
    choice used to silently fall through into on its way out (GitHub
    issue #39) -- `[B]ack` now always means back, nothing else.

    `link_context` (design doc), if given, is used by
    `_compose_new_post` to queue a `board_post` event when `board` is
    Linked -- `None` (Link disabled on this node, or a direct test call
    site) simply means a new post here never propagates over Link,
    same degrade-gracefully shape every other optional context uses.
    """
    board_name = sanitize_text(board.name)
    can_post = (
        meets_level(user, get_effective_min_write_level(db, board))
        and meets_age(db, user, get_effective_min_age(db, board))
        and meets_name_requirement(db, user, get_effective_name_requirement(db, board))
    )
    description_level = menu_description_level(db, user)
    redraw_in_place = redraw_in_place_enabled(db, user)
    unicode_style = unicode_style_enabled(db, user)
    collapsed = breadcrumb_collapsed_enabled(db, user)
    accent_color = effective_accent_color(session, db)
    header_color = effective_header_color(session, db)

    def _refetch_current_page() -> PostPage:
        """Re-fetches whichever page is currently on screen, using the
        exact cursor that produced it -- not always the newest page.
        Needed after an in-place edit (which never moves a post's feed
        position, see netbbs.boards.posts._resolve_current_version)
        so [E]diting a post doesn't also silently jump the SysOp back
        to page one as an unrelated side effect."""
        if page_anchor is None:
            return list_posts_page(db, board, user)
        mode, cursor = page_anchor
        return list_posts_page(db, board, user, **{mode: cursor})

    async def _render_and_advance_cursor(current_page: PostPage) -> None:
        """The one place every render in this loop funnels through
        (issue #56) -- advances `user`'s board read cursor to whatever
        is now newest on screen. A no-op when the page is empty (the
        empty-board early return above never reaches here at all, but
        an Older/Newer navigation could in principle land on an empty
        result if a page emptied out from under a live session)."""
        await _render_board_page(
            session, db, board_name, current_page, user, can_post=can_post,
            name_requirement=get_effective_name_requirement(db, board),
            description_level=description_level,
            redraw_in_place=redraw_in_place,
            unicode_style=unicode_style,
            collapsed=collapsed,
        )
        if current_page.posts:
            record_board_seen(db, user, board, current_page.posts[-1])

    async def _compose_new_post(*, initial_body: str | None = None) -> None:
        await session.write("\r\nSubject (or press Enter to cancel): ")
        subject = (await session.read_line()).strip()
        if not subject:
            await session.write_line(colored("Post cancelled.", fg_color=MUTED_COLOR))
            return
        draft_path = _post_draft_path(db, kind="new", board=board, user=user)
        body = await _compose_body(session, db, user, initial_text=initial_body, draft_path=draft_path)
        if body is not None:
            # `append_signature` is idempotent (its own docstring): a
            # resumed draft (`initial_body`) may or may not already
            # carry the signature depending on exactly when it was
            # saved, and this can't cheaply tell which without that
            # idempotency -- so it's always attempted here, safely,
            # rather than only on a "first, fresh compose" heuristic
            # that missed the /exit-then-resume case entirely.
            signature = get_signature(db, user)
            if signature:
                body = append_signature(body, signature)
        if body is None:
            # Issue #149: /exit or /quit (either editor) leaves the
            # draft on disk instead of deleting it -- that's the one
            # thing distinguishing this from an explicit /cancel here,
            # since both return `None` the same way.
            if draft_path.exists():
                await session.write_line(
                    colored("Draft saved -- you'll be offered it next time you visit this message board.", fg_color=MUTED_COLOR)
                )
            else:
                await session.write_line(colored("Post cancelled.", fg_color=MUTED_COLOR))
            return
        while True:
            action = await review_composition(
                session,
                recipient=None,
                subject=subject,
                body=body,
                commit_key="p",
                commit_label="ost",
                commit_brief="Publish this post",
                description_level=description_level,
                redraw_in_place=redraw_in_place,
                unicode_style=unicode_style,
                collapsed=collapsed,
                accent_color=accent_color,
                header_color=header_color,
                truecolor=effective_truecolor(session, db, user),
            )
            if action is ReviewAction.CANCEL:
                await session.write_line(colored("Post cancelled.", fg_color=MUTED_COLOR))
                return
            if action is ReviewAction.EDIT_SUBJECT:
                await session.write(f"Subject [{sanitize_text(subject)}] (Enter to keep): ")
                subject = (await session.read_line()).strip() or subject
                continue
            if action is ReviewAction.EDIT_BODY:
                revised = await _compose_body(session, db, user, initial_text=body, draft_path=draft_path)
                if revised is not None:
                    body = revised
                elif draft_path.exists():
                    # /exit or /quit while revising -- issue #149: this
                    # leaves the whole in-progress post as a saved
                    # draft, not just "keep the previous body and stay
                    # in review."
                    await session.write_line(
                        colored(
                            "Draft saved -- you'll be offered it next time you visit this message board.",
                            fg_color=MUTED_COLOR,
                        )
                    )
                    return
                else:
                    await session.write_line(colored("Body unchanged.", fg_color=MUTED_COLOR))
                continue
            try:
                post = create_post(db, board, user, subject, body)
            except PostError as exc:
                await session.write_line(colored(f"Could not create post: {exc}", fg_color=MUTED_COLOR))
                continue
            if link_context is not None:
                queue_board_post_if_linked(db, post, board, node_identity=link_context.node_identity)
            await session.write_line(f"Posted (id {post.post_id[:12]}...).")
            return

    async def _offer_saved_draft_if_any() -> None:
        """Issue #149's other half: proactively surfaces a saved new-
        post draft for this exact (user, board) the moment the board is
        entered, rather than only ever resurfacing it if/when the
        caller happens to pick [P]ost again on their own (the existing
        crash-recovery prompt inside `_compose_body` still does that,
        unchanged, for whichever draft this one doesn't consume).
        Scoped to `kind="new"` only -- an in-progress *edit* of a
        specific existing post has no equally natural "board entry"
        moment to announce itself at, so it stays exclusively behind
        the existing recovery-on-reopen path."""
        draft_path = _post_draft_path(db, kind="new", board=board, user=user)
        if not draft_path.exists():
            return
        await session.write_line(
            colored(
                "\r\nYou have a saved post draft for this message board from an earlier session.",
                fg_color=MUTED_COLOR,
            )
        )
        await session.write("[E]dit it, [D]elete it, or [I]gnore for now? ")
        choice = (await session.read_key()).lower()
        if choice == "d":
            delete_draft(draft_path)
            await session.write_line(colored("\r\nDraft deleted.", fg_color=MUTED_COLOR))
            return
        if choice == "e":
            await session.write_line("")
            saved_text = load_draft(draft_path)
            # Consumed here, before _compose_new_post ever opens an
            # editor against the same draft_path -- otherwise that
            # editor's own crash-recovery check would immediately offer
            # to "resume" the very draft this prompt just handed off,
            # a redundant second prompt for the same file.
            delete_draft(draft_path)
            await _compose_new_post(initial_body=saved_text)
            return
        # Anything else (including "i") leaves the draft in place,
        # unread -- same permissive "everything but the named choices
        # is a no-op" convention netbbs.net.prose_editor._confirm_quit
        # already uses.

    if can_post:
        # Runs once, before any post list is shown -- issue #149's own
        # "prompt before normal board interaction proceeds" acceptance
        # criterion. Gated on `can_post` the same way [P]ost itself
        # already is: no point resurfacing a draft the caller couldn't
        # act on to post if they resumed it.
        await _offer_saved_draft_if_any()

    page_anchor: tuple[str, tuple[str, str]] | None = ("after", initial_cursor) if initial_cursor else None
    page = list_posts_page(db, board, user, after=initial_cursor) if initial_cursor else list_posts_page(db, board, user)
    if initial_cursor and not page.posts:
        # Nothing newer than the cursor `[N]ew scan` jumped in with --
        # the user is caught up, not looking at a genuinely empty board.
        # Fall back to the ordinary newest-page view rather than the
        # "has no posts yet" path below, which would falsely claim the
        # board is empty and (worse) prompt to compose the first post.
        page_anchor = None
        page = list_posts_page(db, board, user)
    if not page.posts:
        # Dogfood report: this used to skip straight to composing the
        # first post whenever the caller could write, with no [P]ost/
        # [B]ack choice first -- the exact same "walked into it" problem
        # issue #39/#40 already fixed for the non-empty case, just never
        # extended to this one. Still skips the full Older/Newer/Edit
        # navigation loop (nothing to browse either way), but offers the
        # same explicit choice before composing anything.
        header_color = effective_header_color_256(db)
        await session.write_line(
            f"\r\n{screen_title(board_name, breadcrumb=(session.node_display_name, 'Message boards'), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color, node_name_gradient=session.node_name_gradient)}"
        )
        await session.write_line(
            f"\r\n{empty_state('This message board has no posts yet', detail='It is ready for its first conversation.', width=session.terminal_width, header_color=header_color)}"
        )
        if not can_post:
            return
        while True:
            options = [
                MenuEntry(label=menu_key("P", "ost"), brief="Write the first post"),
                MenuEntry(label=menu_key("B", "ack"), brief="Return to the previous menu"),
            ]
            await session.write_line(
                "\r\n" + _menu_row(
                    options, width=session.terminal_width, height=session.terminal_height,
                    description_level=description_level,
                )
            )
            await session.write("Choice: ")
            choice = (await session.read_key()).lower()
            if choice == "b":
                await session.write_line("")
                return
            if choice == "p":
                await session.write_line("")
                await _compose_new_post()
                page = list_posts_page(db, board, user)
                if page.posts:
                    # A post was actually created (not cancelled) --
                    # fall through to the ordinary render+navigation
                    # loop below, same post-then-refresh behavior the
                    # non-empty case's own [P]ost option already has.
                    break
                continue
            await session.write(reject_unhandled_key(choice))

    await _render_and_advance_cursor(page)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "o" and page.has_older:
            await session.write_line("")
            oldest = page.posts[0]
            page_anchor = ("before", (oldest.created_at, oldest.post_id))
            page = _refetch_current_page()
            await _render_and_advance_cursor(page)
        elif choice == "n" and page.has_newer:
            await session.write_line("")
            newest = page.posts[-1]
            page_anchor = ("after", (newest.created_at, newest.post_id))
            page = _refetch_current_page()
            await _render_and_advance_cursor(page)
        elif choice == "r" and page.has_newer:
            await session.write_line("")
            page_anchor = None
            page = _refetch_current_page()
            await _render_and_advance_cursor(page)
        elif choice == "e" and any(_can_edit_post(db, post, user) for post in page.posts):
            await session.write_line("")
            await _edit_existing_post(session, db, board, page, user, link_context=link_context)
            page = _refetch_current_page()
            await _render_and_advance_cursor(page)
        elif choice == "t" and any(_can_tombstone_post(db, post, user) for post in page.posts):
            await session.write_line("")
            await _tombstone_existing_post(session, db, board, page, user, link_context=link_context)
            page = _refetch_current_page()
            await _render_and_advance_cursor(page)
        elif choice == "p" and can_post:
            await session.write_line("")
            await _compose_new_post()
            page_anchor = None  # a freshly-created post always lands on the newest page
            page = _refetch_current_page()
            await _render_and_advance_cursor(page)
        elif choice == "b":
            await session.write_line("")
            return
        else:
            await session.write(reject_unhandled_key(choice))


async def _edit_existing_post(
    session: Session,
    db: Database,
    board: Board,
    page: PostPage,
    user: User,
    *,
    link_context: LinkContext | None = None,
) -> None:
    """
    Edit one of the posts currently on screen -- selected by the
    page-relative `[N]` position `_render_post_page` prints next to
    each one, since a board page is at most 5 posts, too small to
    justify pulling in the real picker (`netbbs.net.picker.pick_item`)
    just to choose one (design doc).

    Authorization is checked *before* prompting for any new content
    (`_can_edit_post`, the same rule `edit_post` itself enforces) so a
    SysOp who picks a post they can't actually edit finds out
    immediately, not after composing a whole revision.

    `link_context` (design doc), if given, queues a `board_post_edit`
    for a Linked board right after a successful `edit_post` when `user`
    is the post's own original author, or a `board_post_moderator_edit`
    (design doc §9.5, issue #88) when `user` is instead a moderator
    editing someone else's post *and* this node is the board's own
    current origin -- a carrying (non-origin) node's own local moderator
    edit stays purely local, not propagated (see `queue_board_post_
    moderator_edit_if_linked`'s own docstring for why).
    """
    await session.write(f"Edit which post number [1-{len(page.posts)}]? ")
    choice = (await session.read_key()).strip()
    if not choice.isdigit() or not (1 <= int(choice) <= len(page.posts)):
        await session.write_line(colored("\r\nNot a valid post number.", fg_color=MUTED_COLOR))
        return
    post = page.posts[int(choice) - 1]
    await session.write_line("")

    if not _can_edit_post(db, post, user):
        await session.write_line(colored("You can't edit that post.", fg_color=MUTED_COLOR))
        return

    await session.write(f"Subject [{post.subject}] (Enter to keep): ")
    subject = (await session.read_line()).strip() or post.subject

    edit_draft_path = _post_draft_path(
        db, kind="edit", board=board, user=user, root_post_id=post.root_post_id
    )
    body = await _compose_body(session, db, user, initial_text=post.body, draft_path=edit_draft_path)
    if body is None:
        # Issue #149: /exit or /quit leaves this revision's draft on
        # disk instead of deleting it -- same distinguishing check as
        # _compose_new_post's own. There's no board-entry prompt for an
        # in-progress *edit* (see _offer_saved_draft_if_any's own
        # docstring for why), so it's only ever resurfaced by picking
        # [E]dit on this same post again.
        if edit_draft_path.exists():
            await session.write_line(
                colored(
                    "Draft saved -- you'll be offered it next time you edit this post.", fg_color=MUTED_COLOR
                )
            )
        else:
            await session.write_line(colored("Edit cancelled.", fg_color=MUTED_COLOR))
        return

    try:
        edited = edit_post(db, post, board, subject=subject, body=body, edited_by=user)
    except PostError as exc:
        await session.write_line(colored(f"Could not save edit: {exc}", fg_color=MUTED_COLOR))
        return
    if link_context is not None:
        queue_board_post_edit_if_linked(db, edited, board, node_identity=link_context.node_identity, edited_by=user)
        queue_board_post_moderator_edit_if_linked(
            db, edited, board, node_identity=link_context.node_identity, edited_by=user
        )
    await session.write_line("Post updated.")


async def _tombstone_existing_post(
    session: Session,
    db: Database,
    board: Board,
    page: PostPage,
    user: User,
    *,
    link_context: LinkContext | None = None,
) -> None:
    """
    `[T]ombstone` one of the posts currently on screen (design doc §9.5,
    issue #88) -- selected the same page-relative way `_edit_existing_
    post` already is. Redacts the post to a placeholder revision
    (`netbbs.boards.posts.tombstone_post`) rather than deleting it
    outright, so the edit chain and any reply's `parent_post_id` stay
    intact -- there was no existing live UI action to redact an
    already-published post at all before this issue (the only existing
    `delete_post` call site handles pending-post rejection, a different
    case that never reaches an approved post).

    `link_context`, if given, queues a `board_post_tombstone` right
    after a successful `tombstone_post`, but only when this node is the
    board's own current origin -- same origin-only reasoning as
    `queue_board_post_moderator_edit_if_linked` (see that function's own
    docstring).
    """
    await session.write(f"Tombstone which post number [1-{len(page.posts)}]? ")
    choice = (await session.read_key()).strip()
    if not choice.isdigit() or not (1 <= int(choice) <= len(page.posts)):
        await session.write_line(colored("\r\nNot a valid post number.", fg_color=MUTED_COLOR))
        return
    post = page.posts[int(choice) - 1]
    await session.write_line("")

    if not _can_tombstone_post(db, post, user):
        await session.write_line(colored("You can't tombstone that post.", fg_color=MUTED_COLOR))
        return

    if not await prompt_yes_no(session, "Redact this post? This cannot be undone.", default=False):
        await session.write_line(colored("Cancelled.", fg_color=MUTED_COLOR))
        return

    try:
        tombstoned = tombstone_post(db, post, board, tombstoned_by=user)
    except PostError as exc:
        await session.write_line(colored(f"Could not tombstone: {exc}", fg_color=MUTED_COLOR))
        return
    if link_context is not None:
        queue_board_post_tombstone_if_linked(db, tombstoned, board, node_identity=link_context.node_identity)
    await session.write_line("Post tombstoned.")


def _post_draft_path(db: Database, *, kind: str, board: Board, user: User, root_post_id: str = "") -> Path:
    """A stable per-(user, board, [post]) draft location, colocated
    with the node's database the same way `netbbs.net.welcome_banner.
    banner_path` already colocates its own single global draft --
    there just needs to be more than one slot here, one per in-progress
    composition/edit, so this lives in its own subdirectory rather than
    a single flat sibling file.

    Shared by both editors (`netbbs.net.prose_editor.edit_prose`'s
    crash-recovery autosave, `netbbs.net.composition.edit_line_body`'s
    `/exit`/`/quit`) and by two different recovery UIs at two different
    moments (issue #149): `_offer_saved_draft_if_any`, proactively, at
    board entry for `kind="new"`; each editor's own on-entry
    `draft_path.exists()` check otherwise, for `kind="edit"` or for a
    `kind="new"` draft the board-entry prompt didn't consume."""
    suffix = f"_{root_post_id}" if root_post_id else ""
    return drafts_directory(db) / f"{kind}_{board.id}_{user.id}{suffix}.draft"


async def _compose_body(
    session: Session, db: Database, user: User, *, initial_text: str | None = None, draft_path: Path
) -> str | None:
    """The single place a post body (or an edit of one) is actually
    entered: the fullscreen prose editor if `user` has opted in,
    otherwise the shared logical-line editor. Both paths accept
    `initial_text`, return a complete draft, and return `None` for
    either an explicit cancel (draft deleted) or an explicit save-and-
    leave (draft kept -- issue #149, see `edit_line_body`'s/
    `edit_prose`'s own docstrings) -- `draft_path.exists()` after a
    `None` return tells the two apart. Neither path persists a real
    post itself."""
    if fullscreen_editor_enabled(db, user):
        return await edit_prose(
            session, initial_text=initial_text, draft_path=draft_path, max_bytes=MAX_BODY_BYTES,
            unicode_style=unicode_style_enabled(db, user),
        )
    return await edit_line_body(
        session,
        initial_text=initial_text,
        max_bytes=MAX_BODY_BYTES,
        max_lines=_MAX_PLAIN_POST_LINES,
        draft_path=draft_path,
    )


def _author_display_name(db: Database, post: Post, *, name_requirement: str | None) -> str:
    """
    The author label to render for one post (design doc §18). Only
    looks up the live account behind `post.author_label`
    when this board actually requires `verified_and_displayed` names --
    that's the one case where showing the *current* attested real name
    is intentional (an attestation, like an age gate, is a living fact
    re-evaluated at read time, not frozen at post time). Every other
    case renders the plain, already-sanitized `author_label` exactly as
    it always has: `author_label` is deliberately denormalized so a
    post's history still reads correctly even if the account is later
    renamed or removed (design doc) -- substituting a user's
    *current* `display_name` there for the ordinary case would quietly
    break that property, since `display_name` (unlike `username`) is
    actually mutable.
    """
    if name_requirement == "verified_and_displayed":
        author = get_user_by_id(db, post.author_user_id)
        if author is not None:
            return format_name_for_resource(db, author, name_requirement=name_requirement)
    return sanitize_text(post.author_label)


def _render_quoted_body(body: str, width: int) -> str:
    """Reflow `body`, coloring `>`-quoted lines in `MUTED_COLOR` (issue
    #181). Runs `reflow()` per same-kind run of raw lines, not once over
    the whole body: `reflow()` only paragraph-breaks on a *blank* line,
    and otherwise collapses single line breaks and rewraps -- so a quote
    immediately followed by a reply (no blank line between them, the
    common case) would get merged into one rewrapped line, and a multi-
    line quote's own wrapped continuation lines would lose their leading
    `>` and go uncolored. Each quote run has its `>` prefix stripped,
    gets reflowed as its own paragraph, and has `>` reapplied to every
    wrapped line, so multi-line quotes wrap and color correctly too.

    A blank line is its own third run kind, output verbatim, never
    folded into an adjacent quote/text run's own `reflow()` call --
    a blank separator at a quote/text boundary (`"> quoted\\n\\nreply"`)
    would otherwise join a run's raw lines with a single `\\n`, one
    short of the `\\n\\n` `reflow()` needs to even recognize a paragraph
    break, silently dropping the authored blank line."""
    runs: list[tuple[str, list[str]]] = []
    for raw_line in body.split("\n"):
        stripped_line = raw_line.strip()
        kind = "blank" if not stripped_line else "quote" if stripped_line.startswith(">") else "text"
        if runs and runs[-1][0] == kind:
            runs[-1][1].append(raw_line)
        else:
            runs.append((kind, [raw_line]))

    rendered: list[str] = []
    for kind, raw_lines in runs:
        if kind == "blank":
            rendered.extend(raw_lines)
        elif kind == "quote":
            stripped = [line.split(">", 1)[1].lstrip(" ") for line in raw_lines]
            for wrapped_line in reflow("\n".join(stripped), width=max(1, width - 2)).splitlines():
                rendered.append(colored(f"> {wrapped_line}", fg_color=MUTED_COLOR))
        else:
            rendered.extend(reflow("\n".join(raw_lines), width=width).splitlines())
    return "\r\n".join(rendered)


async def _render_post_page(
    session: Session,
    db: Database,
    board_name: str,
    page: PostPage,
    user: User,
    *,
    name_requirement: str | None,
    redraw_in_place: bool = False,
    unicode_style: bool = False,
    collapsed: bool = False,
) -> None:
    header = screen_title(
        board_name,
        breadcrumb=(session.node_display_name, "Message boards"),
        subtitle=f"{len(page.posts)} post{'s' if len(page.posts) != 1 else ''} on this page",
        width=session.terminal_width,
        clear=redraw_in_place,
        unicode_style=unicode_style, collapsed=collapsed,
        header_color=effective_header_color(session, db),
    node_name_gradient=session.node_name_gradient)
    await session.write_line(f"\r\n{header}")
    accent = effective_accent_color(session, db)
    for position, post in enumerate(page.posts, start=1):
        if position > 1:
            rule_char = "─" if unicode_style else "-"
            divider_color = 238 if effective_truecolor(session, db, user) else MUTED_COLOR
            await session.write_line(colored(rule_char * min(session.terminal_width, 78), fg_color=divider_color))
        when = format_for_display(post.created_at, db)
        edited_marker = f" {badge('edited')}" if post.is_edited else ""
        author_display = _author_display_name(db, post, name_requirement=name_requirement)
        # Position numbers are 1-indexed *within this page only* -- not
        # a stable identity across page changes, purely a same-screen
        # selector for [E]dit (design doc -- prose editor:
        # editing an existing post), the same "how do you pick one item
        # currently on screen" role a picker's page-relative numbering
        # already plays elsewhere, just inline here since a board page
        # is at most 5 posts, too small to need a real picker for it.
        #
        # Built from three separately-colored segments, not one
        # colored() call wrapping the whole line -- author_display may
        # already contain its own colored+reset unit (the
        # verified-name formatting), and nesting that inside a single
        # outer colored() would have the inner segment's own reset code
        # clear the outer ACCENT_COLOR early, leaving the trailing
        # "(timestamp)" text in the terminal's default color instead.
        post_header = (
            colored(f"[{position}] {sanitize_text(post.subject)} -- ", fg_color=accent)
            + author_display
            + colored(f" ({when})", fg_color=METADATA_COLOR)
            + edited_marker
        )
        await session.write_line(f"\r\n{post_header}")
        # Reflowed to this specific session's actual detected width
        # (NAWS-negotiated, or the 80-column default — see
        # netbbs.net.session.Session.terminal_width), not a fixed
        # assumption, per the design doc's "must degrade gracefully
        # above 40x24 minimum" requirement. Sanitized *before* reflow,
        # not after — textwrap's width math counts raw characters, so a
        # stray control byte would also throw off wrapping, not just be
        # a display-safety concern. allow_newlines=True: a post body is
        # genuinely multi-line content (paragraph breaks), unlike the
        # single-line fields above -- see sanitize_text's docstring.
        body = sanitize_text(post.body, allow_newlines=True)
        await session.write_line(_render_quoted_body(body, session.terminal_width))


# -- user directory & vCard/finger (design doc) ------


async def _browse_directory(session: Session, db: Database, user: User) -> None:
    """
    The user directory: a table-style listing of every registered
    account (`netbbs.auth.users.list_users`). Selecting an entry shows
    their full finger/vCard detail (`_show_vcard`) — bio visibility is
    per-target, not a directory-wide filter, so everyone appears in
    the listing regardless of whether their bio itself is public.

    Loops back to the listing after each lookup, same "pick, view, pick
    again" shape `_show_inbox`/`_show_sent` (`netbbs.net.mail_flow`)
    already use -- a directory's whole purpose is looking people up,
    which a one-shot "view one, then dumped back to the main menu"
    flow made needlessly costly to do for more than one person in a
    row (dogfood follow-up).
    """
    while True:
        users = list_users(db)
        selected = await pick_item(
            session,
            users,
            name_of=lambda u: u.username,
            stable_id_of=lambda u: u.id,
            description_of=lambda u: _directory_description(db, u),
            title="User directory",
            empty_message="No registered users yet.",
            redraw_in_place=redraw_in_place_enabled(db, user),
            unicode_style=unicode_style_enabled(db, user),
            collapsed=breadcrumb_collapsed_enabled(db, user),
            accent_color=effective_accent_color(session, db),
            header_color=effective_header_color(session, db),
        )
        if selected is None:
            return
        await _show_vcard(session, db, selected, user)


def _directory_description(db: Database, target: User) -> str:
    when = format_for_display(target.created_at, db)
    if not has_bio(db, target):
        # Dogfood follow-up: this used to derive the badge purely from
        # visibility, defaulting every account that has never written a
        # bio at all to "PRIVATE BIO" -- identical to an account that
        # deliberately wrote one and hid it. On a directory full of
        # members who just haven't gotten around to writing a bio yet,
        # that reads as "everyone's guarding a secret," which isn't
        # true and isn't what the flag means.
        bio_state = "NO BIO"
    elif is_bio_visible(db, target):
        bio_state = "PUBLIC BIO"
    else:
        bio_state = "PRIVATE BIO"
    return f"[{bio_state}] member since {when}"


async def _show_vcard(session: Session, db: Database, target: User, requesting_user: User) -> None:
    """finger-style detail view — `get_vcard` already resolves
    visibility (always visible to yourself, otherwise only if the
    target has opted in)."""
    vcard = get_vcard(db, target, requesting_user=requesting_user)
    when = format_for_display(vcard.created_at, db)
    username = sanitize_text(vcard.username)
    await session.write_line(
        "\r\n" + screen_title(
            username,
            breadcrumb=(session.node_display_name, "Directory"),
            subtitle="Member profile",
            width=session.terminal_width,
            clear=redraw_in_place_enabled(db, requesting_user),
            unicode_style=unicode_style_enabled(db, requesting_user),
            collapsed=breadcrumb_collapsed_enabled(db, requesting_user),
            header_color=effective_header_color(session, db),
        node_name_gradient=session.node_name_gradient)
    )
    await session.write_line(
        colored("Member since: ", fg_color=LABEL_COLOR)
        + colored(when, fg_color=METADATA_COLOR)
    )
    await session.write_line(colored("Bio", fg_color=effective_header_color(session, db), bold=True))
    if vcard.bio is not None:
        bio = reflow(sanitize_text(vcard.bio, allow_newlines=True), width=session.terminal_width)
        await session.write_line(
            colored(bio, fg_color=VALUE_COLOR)
        )
    else:
        await session.write_line(
            empty_state(
                "No public bio",
                detail="This member has not shared a bio.",
                width=session.terminal_width,
            )
        )


@dataclass(frozen=True)
class _RemoteWhoEntry:
    """One user currently online on a *linked* node (issue #164) --
    `_caller_who_screen`'s picker mixes these in alongside local
    `SessionSummary` entries so "who's online" genuinely means the whole
    reachable mesh, not just this node, the same "no wrong-node
    friction" bar the rest of this initiative is held to."""

    node_fingerprint: str
    username: str

    @property
    def stable_id(self) -> int:
        # A local SessionSummary.session_id is a small, node-lifetime
        # sequential integer (that type's own docstring) -- this stays
        # well outside that range without needing to coordinate with it,
        # since collision would only ever affect picker's cosmetic
        # "goto #" convenience, never selection correctness itself.
        digest = hashlib.sha256(f"{self.node_fingerprint}:{self.username}".encode("utf-8")).hexdigest()
        return int(digest[:8], 16)


_WhoEntry = SessionSummary | _RemoteWhoEntry


def _who_entry_name(entry: _WhoEntry) -> str:
    if isinstance(entry, _RemoteWhoEntry):
        return entry.username
    return entry.username or "(unauthenticated)"


def _who_entry_description(db: Database, entry: _WhoEntry) -> str:
    if isinstance(entry, _RemoteWhoEntry):
        return f"on linked node {entry.node_fingerprint[:12]}…"
    when = format_for_display(entry.connected_at, db)
    return f"connected since {when}"


def _remote_who_entries(link_context: LinkContext | None) -> list[_RemoteWhoEntry]:
    if link_context is None or link_context.realtime_bridge is None:
        return []
    return [
        _RemoteWhoEntry(node_fingerprint=fingerprint, username=username)
        for fingerprint, online in link_context.realtime_bridge.remote_node_presence().items()
        for username in online
    ]


async def _caller_who_screen(
    session: Session,
    db: Database,
    node_controls: NodeControls,
    user: User,
    hub: ChatHub,
    presence: PresenceRegistry,
    direct_invites: DirectChatInvites | None,
    lane: DatabaseLane | None,
    link_context: LinkContext | None = None,
) -> None:
    """
    Issue #99: the caller-facing counterpart to the SysOp `[N]ode`
    menu's own `[W]ho` screen (`netbbs.net.admin_flow._who_screen`) --
    same underlying `ActiveSessionRegistry`, but scoped down to what an
    ordinary caller should actually see and do: no peer addresses (the
    SysOp version's unauthenticated-session fallback shows one; this
    never does), no disconnect action, just "who else is here" plus an
    optional one-off message or (design doc §6.3) a direct-chat invite.

    Unauthenticated sessions (still at the login prompt) are excluded
    entirely -- there's no account to message, and `_who_entry_name`'s
    `"(unauthenticated)"` fallback exists only so `SessionSummary`'s
    general shape doesn't need a second, caller-specific variant.

    Issue #164: every user currently online on a *linked* node
    (`link_context.realtime_bridge.remote_node_presence()`) is mixed
    into the same list, not shown as a separate section -- "who's
    online" should mean the whole reachable mesh, the "no wrong-node
    friction" bar this initiative is held to. Messaging/inviting a
    remote entry isn't offered, though: Link-wide live private chat
    isn't built yet (issue #168) -- selecting one says so plainly
    instead of silently doing nothing or pretending the action exists.

    A target who has opted out (`netbbs.messaging_preferences.
    accepts_direct_messages`, default `True`) still appears in the list
    -- this screen answers "who's online", not "who's reachable" -- but
    acting on them at all is refused up front, before offering either
    action, with a plain explanation rather than a silently swallowed
    attempt. Choosing not to receive unsolicited direct messages
    reasonably also means not receiving direct-chat invites -- one
    check gates both, not two independent ones.

    `[I]nvite to chat` is only offered when both `direct_invites` and
    `lane` are given (`run_direct_chat_invite_flow` needs both) -- same
    degrade-gracefully-in-tests shape every other optional feature on
    this menu already has; the existing `[M]essage` action needs
    neither and is always available.
    """
    async def _load_entries() -> list[_WhoEntry]:
        local: list[_WhoEntry] = [
            entry
            for entry in node_controls.session_registry.list_entries()
            if entry.username is not None and entry.session is not session
        ]
        return local + _remote_who_entries(link_context)

    selected = await pick_item(
        session, await _load_entries(),
        name_of=_who_entry_name,
        stable_id_of=lambda e: e.session_id if isinstance(e, SessionSummary) else e.stable_id,
        description_of=lambda e: _who_entry_description(db, e),
        title="Who's online",
        empty_message="No one else is online right now.",
        # Issue #102: this is exactly the "list that goes stale while
        # you're looking at it" case Ctrl-R exists for -- who's
        # connected changes independently of anything this screen does.
        refresh=_load_entries,
        description_level=menu_description_level(db, user),
        redraw_in_place=redraw_in_place_enabled(db, user),
        unicode_style=unicode_style_enabled(db, user),
        collapsed=breadcrumb_collapsed_enabled(db, user),
        accent_color=effective_accent_color(session, db),
        header_color=effective_header_color(session, db),
    )
    if selected is None:
        return

    if isinstance(selected, _RemoteWhoEntry):
        await session.write_line(
            colored(
                f"{selected.username} is connected to a different linked node -- direct messaging and "
                "chat invites across nodes aren't available yet.",
                fg_color=MUTED_COLOR,
            )
        )
        return

    assert selected.username is not None  # filtered above
    try:
        target = get_user_by_username(db, selected.username)
    except AuthError:
        await session.write_line(colored("That account no longer exists.", fg_color=ERROR_COLOR))
        return

    if not accepts_direct_messages(db, target):
        await session.write_line(
            colored(f"{target.username} has opted out of receiving direct messages.", fg_color=MUTED_COLOR)
        )
        return

    offer_invite = direct_invites is not None and lane is not None
    await session.write_line(
        "\r\n" + screen_title(
            target.username,
            breadcrumb=(session.node_display_name, "Who's online"),
            subtitle="Choose how you would like to connect.",
            width=session.terminal_width,
            clear=redraw_in_place_enabled(db, user),
            unicode_style=unicode_style_enabled(db, user),
            collapsed=breadcrumb_collapsed_enabled(db, user),
            header_color=effective_header_color(session, db),
        node_name_gradient=session.node_name_gradient)
    )
    options = [MenuEntry(label=menu_key("M", "essage"), brief="Send a one-off message")]
    if offer_invite:
        options.append(MenuEntry(label=menu_key("I", "nvite to chat"), brief="Invite them to a direct chat"))
    options.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to Who's online"))
    await session.write_line(
        _menu_row(
            options, width=session.terminal_width, height=session.terminal_height,
            description_level=menu_description_level(db, user),
        )
    )
    await session.write("Choice: ")
    action = (await session.read_key()).lower()
    await session.write_line("")

    if action == "b":
        return
    if offer_invite and action == "i":
        assert direct_invites is not None and lane is not None  # offer_invite's own condition
        await run_direct_chat_invite_flow(
            session, lane, hub, presence, direct_invites, node_controls.session_registry, user, target,
        )
        return
    if action != "m":
        await session.write(reject_unhandled_key(action))
        return

    await session.write(f"Message to {selected.username}: ")
    message = (await session.read_line()).strip()
    if not message:
        await session.write_line(colored("Cancelled: message cannot be blank.", fg_color=MUTED_COLOR))
        return

    delivered = await node_controls.session_registry.notify_one(
        selected.session,
        colored(f"\r\n*** Message from {user.username}: {sanitize_text(message)} ***", fg_color=ALERT_COLOR, bold=True),
    )
    if delivered:
        await session.write_line(colored("Message sent.", fg_color=SUCCESS_COLOR))
    else:
        await session.write_line(colored(f"{selected.username} is no longer online.", fg_color=ERROR_COLOR))


# How many recent sessions [L]ast sessions shows -- generous enough to
# be useful, small enough to fit on one screen page without its own
# pagination affordance (unlike pick_item-backed screens, this is a
# plain listing: there's no per-entry detail beyond what's already on
# its one line, so there's nothing a selection would actually do).
_SESSION_HISTORY_DISPLAY_LIMIT = 20


def _session_history_display_name(
    db: Database, entry: SessionHistoryEntry, *, viewer_is_sysop: bool
) -> str:
    """The denormalized `username_label` survives account deletion (see
    the migration's own docstring), but showing it is not automatic. A
    SysOp always sees the real name unconditionally (mirrors `netbbs.
    net.admin_flow`'s existing SysOp-sees-everything convention) --
    administrative visibility is the deliberately chosen policy here,
    same as it already was before issue #111.

    For an ordinary caller: while the account still exists, its *current*
    `session_history_name_visible` preference is re-checked live, not
    frozen at connect time -- issue #100's own choice, preserved
    unchanged, so a later opt-out/opt-in takes effect retroactively for
    every one of that account's existing rows. Once the account is
    deleted, there is no longer a live preference to re-check at all --
    falling back to unconditionally showing `username_label` in that case
    (the pre-#111 behavior) silently reversed a user's own prior opt-out
    the moment their account was deleted. `entry.name_visible_fallback`
    (kept in sync with the live preference for as long as the account
    exists -- see `set_session_history_name_visible`'s own docstring) is
    the fallback issue #111 adds specifically for this case: whatever the
    account's preference genuinely was immediately before deletion is
    what a now-deleted account's history keeps showing, permanently,
    since there is no "current" value left to ask."""
    if viewer_is_sysop:
        return entry.username_label
    if entry.user_id is None:
        return entry.username_label if entry.name_visible_fallback else "(name hidden)"
    target = get_user_by_id(db, entry.user_id)
    if target is None or session_history_name_visible(db, target):
        return entry.username_label
    return "(name hidden)"


async def _last_sessions_screen(session: Session, db: Database, user: User) -> None:
    """
    Issue #100: a caller-facing "who recently visited" list, backed by
    the persisted `netbbs.session_history` table -- distinct from
    `[W]ho's online` (issue #99), which only ever shows who's currently
    connected. A session whose account has opted out of being shown by
    name still appears -- the session itself is never hidden, only the
    name, same "still listed, not suppressed" shape issue #99's opt-out
    already established.

    Issue #110: `interrupted_at` (reconciled once at startup, before any
    listener could accept a new session -- see `netbbs.session_history.
    reconcile_interrupted_sessions`'s own docstring) means a row can be
    NULL/NULL and *not* still connected: this process crashed, was
    killed, or lost power before it ever reached `record_session_end`.
    Shown as its own distinct third state, never folded into "still
    connected" (which cannot possibly still be true across a restart) or
    silently written as if `interrupted_at` were the real disconnect
    moment (it's only ever "whenever this node next started up," which
    could be long after the connection actually dropped).

    Waits for a keystroke before returning (dogfood report): this used
    to fall straight through to the main menu's own redraw the instant
    the listing finished printing, which -- under redraw-in-place --
    cleared the terminal and wiped the listing before there was any
    chance to actually read it. Every other plain (non-`pick_item`)
    content screen in this codebase already pauses the same way
    (`netbbs.net.help_overlay.show_help`'s own "Press any key to
    continue..." convention).
    """
    entries = list_recent_sessions(db, limit=_SESSION_HISTORY_DISPLAY_LIMIT)
    await session.write_line(
        "\r\n" + screen_title(
            "Last sessions",
            breadcrumb=(session.node_display_name,),
            width=session.terminal_width,
            clear=redraw_in_place_enabled(db, user),
            unicode_style=unicode_style_enabled(db, user),
            collapsed=breadcrumb_collapsed_enabled(db, user),
            header_color=effective_header_color(session, db),
        node_name_gradient=session.node_name_gradient)
    )
    if not entries:
        await session.write_line(colored("No session history yet.", fg_color=MUTED_COLOR))
    else:
        viewer_is_sysop = meets_level(user, SYSOP_LEVEL)
        accent = effective_accent_color(session, db)
        for entry in entries:
            name = _session_history_display_name(db, entry, viewer_is_sysop=viewer_is_sysop)
            connected = format_for_display(entry.connected_at, db)
            if entry.disconnected_at is not None:
                status = f"until {format_for_display(entry.disconnected_at, db)}"
                status_color = METADATA_COLOR
            elif entry.interrupted_at is not None:
                status = "connection lost -- session did not end cleanly"
                status_color = ERROR_COLOR
            else:
                status = "still connected"
                status_color = SUCCESS_COLOR
            name_color = MUTED_COLOR if name == "(name hidden)" else accent
            await session.write_line(
                colored_truncate(
                    [
                        ("  ", None),
                        (sanitize_text(name), name_color),
                        (" -- connected ", LABEL_COLOR),
                        (connected, METADATA_COLOR),
                        (", ", LABEL_COLOR),
                        (status, status_color),
                    ],
                    session.terminal_width,
                )
            )
    await session.write_line(colored("\r\nPress any key to continue...", fg_color=MUTED_COLOR))
    await session.read_key()


_SORT_PREFERENCE_KIND_LABELS = {
    "channel": "Chat channels", "board": "Message boards", "file_area": "File areas",
}


def _sort_preference_scope_label(db: Database, pref: SortPreference) -> str:
    if pref.category_id is not None:
        if pref.resource_kind == "channel":
            name = get_channel_category_by_id(db, pref.category_id).name
        elif pref.resource_kind == "board":
            name = get_board_category_by_id(db, pref.category_id).name
        else:
            name = get_file_area_category_by_id(db, pref.category_id).name
        return f"Category: {name}"
    if pref.community_id is not None:
        community = get_community(db, pref.community_id)
        name = community.name if community is not None else f"Community #{pref.community_id}"
        return f"Community: {name}"
    return "Global default"


async def _sort_preferences_screen(session: Session, lane: DatabaseLane, user: User) -> None:
    """
    Review/clear your saved sort-mode overrides (design doc, dogfood
    feature request) -- the discoverability half of the `[O]rder`
    command's own design conversation: a 3-level cascade is invisible
    complexity right up until someone forgets they set an override
    months ago and can't tell why one list looks "wrong." `netbbs.
    sort_preferences.list_sort_preferences` deliberately returns raw
    `community_id`/`category_id`, leaving name resolution to the
    caller (that module's own docstring) -- this is the one place that
    resolution happens, since no other screen needs to enumerate a
    user's *entire* set of overrides across all three resource kinds
    and every scope at once.

    `pick_item`'s `name_of`/`description_of` must stay plain, synchronous,
    no-I/O lambdas (its own established contract, matched by every other
    caller in this codebase) -- so scope labels are resolved once via
    `lane.run` into a `pref.id`-keyed dict before entering the picker,
    not lazily per item inside the lambda the way the pre-lane version
    could when it held a bare `db` directly.
    """
    while True:
        prefs = await lane.run(list_sort_preferences, user)
        if not prefs:
            await session.write_line(
                colored("\r\nYou have no saved sort preferences yet.", fg_color=MUTED_COLOR)
            )
            await session.write_line(
                colored(
                    "Set one from any chat channel/message board/file-area picker's [O]rder command.",
                    fg_color=MUTED_COLOR,
                )
            )
            return

        labels: dict[int, str] = {}
        for pref in prefs:
            labels[pref.id] = await lane.run(_sort_preference_scope_label, pref)

        selected = await pick_item(
            session,
            prefs,
            name_of=lambda p: f"{_SORT_PREFERENCE_KIND_LABELS[p.resource_kind]} — {labels[p.id]}",
            stable_id_of=lambda p: p.id,
            description_of=lambda p: SORT_MODE_LABELS[p.sort_mode],
            title="Your sort preferences",
            empty_message="You have no saved sort preferences yet.",
            redraw_in_place=await lane.run(redraw_in_place_enabled, user),
            unicode_style=await lane.run(unicode_style_enabled, user),
            collapsed=await lane.run(breadcrumb_collapsed_enabled, user),
            accent_color=await lane.run(effective_accent_color_256),
            header_color=await lane.run(effective_header_color_256),
        )
        if selected is None:
            return

        clear = await prompt_yes_no(
            session,
            f"Clear this override ({labels[selected.id]}, "
            f"{SORT_MODE_LABELS[selected.sort_mode]})?",
            default=False,
        )
        if clear:
            await lane.run(
                clear_sort_preference,
                user, selected.resource_kind,
                community_id=selected.community_id, category_id=selected.category_id,
            )
            await session.write_line(colored("Cleared.", fg_color=MUTED_COLOR))


def _profile_field(label: str, value: str, *, value_color: int = VALUE_COLOR) -> str:
    """Compose one trusted label with a separately sanitized value span."""
    return colored(f"{label}: ", fg_color=LABEL_COLOR) + colored(
        sanitize_text(value), fg_color=value_color
    )


async def _edit_profile(session: Session, lane: DatabaseLane, user: User) -> None:
    """
    Edit your own vCard and caller preferences (design doc) --
    `edit_resource_draft` in immediate mode (issue #160's cursor-nav
    follow-up; see that function's own `save=None` docstring): every
    field here already persists itself the instant it's activated (see
    `live_choice_field`), unlike a resource create/edit screen's own
    draft/Save step, so there is nothing to discard on `[B]ack` and no
    `[S]ave` entry is offered.

    `description_level`/`redraw_in_place` are fetched once, same as
    every other `edit_resource_draft` caller (see that parameter's own
    docstring for why a per-redraw lookup is deliberately avoided).
    One consequence worth calling out because it's new to this specific
    screen: toggling the "Descriptions" field updates *that field's*
    own displayed value immediately, but this same screen's own menu-row
    layout only starts using the new level the next time "Your profile"
    is entered, not mid-visit -- every other `edit_resource_draft`
    caller doesn't expose this preference as one of its own fields, so
    this self-referential case doesn't come up for them.
    """
    description_level = await lane.run(menu_description_level, user)
    redraw_in_place = await lane.run(redraw_in_place_enabled, user)
    unicode_style = await lane.run(unicode_style_enabled, user)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, user)
    accent_color = await lane.run(effective_accent_color_256)
    header_color = await lane.run(effective_header_color_256)

    draft: Draft = {
        "bio": await lane.run(get_bio, user) or "",
        "bio_visible": await lane.run(is_bio_visible, user),
        "signature": await lane.run(get_signature, user) or "",
        "fullscreen_editor": await lane.run(fullscreen_editor_enabled, user),
        "accepts_dm": await lane.run(accepts_direct_messages, user),
        "history_name_visible": await lane.run(session_history_name_visible, user),
        "color_depth": await lane.run(color_depth_override, user) or "auto",
        "description_level": description_level,
        "redraw_in_place": redraw_in_place,
        "unicode_style": unicode_style,
        "breadcrumb_collapsed": collapsed,
        "sort_preference_count": len(await lane.run(list_sort_preferences, user)),
        "ssh_fingerprint": user.fingerprint,
    }

    async def _bio_prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        await _edit_bio(session, lane, user)
        draft["bio"] = await lane.run(get_bio, user) or ""

    async def _signature_prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        await _edit_signature(session, lane, user)
        draft["signature"] = await lane.run(get_signature, user) or ""

    async def _identity_details_prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        await _identity_details_screen(session, lane, user)

    async def _sort_preferences_prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        await _sort_preferences_screen(session, lane, user)
        draft["sort_preference_count"] = len(await lane.run(list_sort_preferences, user))

    async def _ssh_public_key_prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        """Self-service counterpart to `_draw_user_detail`'s SysOp-only
        `[K]` field (`netbbs.net.admin_flow`) -- lets an account that
        registered with a password alone later add or replace the
        public key that enables SSH key-based login, without needing a
        SysOp to do it for them. Reuses the same `set_verify_key`
        domain function; `changed_by=user` records the account acting
        on its own key, distinct in the moderation log from a SysOp
        doing it on someone else's behalf."""
        await session.write_line("")
        verb = "Replace" if draft["ssh_fingerprint"] else "Add"
        await session.write(
            f"{verb} your SSH public key (base64, or an ssh-ed25519 line, blank to cancel): "
        )
        text = (await session.read_line()).strip()
        if not text:
            return
        try:
            verify_key = parse_verify_key(text)
        except IdentityError as exc:
            await session.write_line(colored(f"Could not parse key: {exc}", fg_color=MUTED_COLOR))
            return
        try:
            updated = await lane.run(set_verify_key, user, verify_key, changed_by=user)
        except AuthError as exc:
            await session.write_line(colored(str(exc), fg_color=MUTED_COLOR))
            return
        draft["ssh_fingerprint"] = updated.fingerprint
        await session.write_line(colored("SSH public key set.", fg_color=MUTED_COLOR))

    def _color_depth_render(d: Draft) -> str:
        value = d["color_depth"]
        if value == "auto":
            detected = "truecolor" if session.supports_truecolor else "256-color"
            return f"auto (detected: {detected})"
        return f"{value} (forced)"

    def _preamble(d: Draft) -> str:
        lines = [colored("BIO", fg_color=METADATA_COLOR, bold=True)]
        if d["bio"]:
            lines.append(reflow(sanitize_text(d["bio"], allow_newlines=True), width=session.terminal_width))
        else:
            lines.append(colored("(no bio set)", fg_color=MUTED_COLOR))
        lines.append("")
        lines.append(
            _profile_field(
                "Transport report",
                getattr(session, "truecolor_diagnostic", "capability report unavailable"),
                value_color=METADATA_COLOR,
            )
        )
        return "\r\n".join(lines)

    fields = [
        FieldSpec(
            key="bio", hotkey="e", menu_text=menu_key("E", "dit bio"), label="Bio",
            render=lambda d: f"{len(d['bio'].splitlines())} line(s)" if d["bio"] else "(no bio set)",
            prompt=_bio_prompt,
            brief="Change your public bio text",
            help=(
                "Free-form text shown on your public profile (Directory, Who's online, etc.) "
                "when Visibility below is public. Supports multiple lines. Blank clears it."
            ),
        ),
        FieldSpec(
            key="bio_visible", hotkey="v", menu_text=menu_key("V", "isibility"), label="Visibility",
            render=lambda d: "public" if d["bio_visible"] else "private",
            prompt=live_choice_field(
                "bio_visible", [False, True], persist=lambda lane, v: lane.run(set_bio_visible, user, v)
            ),
            brief="Toggle bio public/private",
            help=(
                "Whether your Bio is shown to other callers at all, independent of what the "
                "bio text itself says. Private hides it everywhere except from a SysOp."
            ),
        ),
        FieldSpec(
            key="signature", hotkey="g", menu_text=menu_key("g", "nature", prefix="Si"), label="Signature",
            render=lambda d: f"{len(d['signature'].splitlines())} line(s)" if d["signature"] else "(no signature set)",
            prompt=_signature_prompt,
            brief="Auto-appended to mail and posts you send",
            help=(
                "Text automatically appended to every message you send from this account -- "
                "mail, board posts, and channel posts alike. Blank means no signature."
            ),
        ),
        FieldSpec(
            key="fullscreen_editor", hotkey="f", menu_text=menu_key("F", "ullscreen editor"),
            label="Fullscreen editor for posts/bio",
            render=lambda d: "on" if d["fullscreen_editor"] else "off",
            prompt=live_choice_field(
                "fullscreen_editor", [False, True],
                persist=lambda lane, v: lane.run(set_fullscreen_editor_enabled, user, v),
            ),
            brief="Toggle the fullscreen editor",
            help=(
                "On: composing a post/bio opens the cursor-addressed fullscreen editor (arrow "
                "keys, Ctrl-based commands, like a simple nano). Off: a plain line-by-line "
                "editor instead -- the safer default for a client that can't reliably position "
                "the cursor."
            ),
        ),
        FieldSpec(
            key="accepts_dm", hotkey="m", menu_text=menu_key("M", "essages"),
            label="Direct messages (Who's online)",
            render=lambda d: "accepted" if d["accepts_dm"] else "not accepted",
            prompt=live_choice_field(
                "accepts_dm", [False, True],
                persist=lambda lane, v: lane.run(set_accepts_direct_messages, user, v),
            ),
            brief="Direct-message preferences",
            help=(
                "Whether other callers can send you a direct/private chat message from the "
                "Who's online screen. Doesn't affect linked-channel chat -- only direct, "
                "one-to-one messages."
            ),
        ),
        FieldSpec(
            key="history_name_visible", hotkey="h", menu_text=menu_key("H", "istory visibility"),
            label="Name shown in Last sessions",
            render=lambda d: "yes" if d["history_name_visible"] else "no (hidden)",
            prompt=live_choice_field(
                "history_name_visible", [False, True],
                persist=lambda lane, v: lane.run(set_session_history_name_visible, user, v),
            ),
            brief="Show your name in Last sessions",
            help=(
                "Whether your username appears in the node's public 'Last sessions' history. "
                "Hiding it only affects what ordinary callers see -- a SysOp can always see "
                "the real name."
            ),
        ),
        FieldSpec(
            key="color_depth", hotkey="c", menu_text=menu_key("C", "olor depth"), label="Color depth",
            render=_color_depth_render,
            prompt=live_choice_field(
                "color_depth", ["auto", "truecolor", "256"],
                persist=lambda lane, v: lane.run(set_color_depth_override, user, v),
            ),
            brief="Force a terminal color depth",
            help=(
                "Overrides NetBBS's automatic terminal-capability detection. 'auto' trusts "
                "what your client reports; force 'truecolor' or '256' only if colors render "
                "wrong -- garbled, or not showing at all -- under auto."
            ),
        ),
        FieldSpec(
            key="description_level", hotkey="d", menu_text=menu_key("D", "escriptions"),
            label="Menu descriptions",
            render=lambda d: d["description_level"],
            prompt=live_choice_field(
                "description_level", ["off", "brief", "detailed"],
                persist=lambda lane, v: lane.run(set_menu_description_level, user, v),
            ),
            brief="Off/brief/detailed menu text",
            help=(
                "Whether menu screens show a short explanation under each option. 'off' is "
                "most compact; 'brief' adds a one-line hint per option; 'detailed' shows the "
                "fullest explanation where a field also defines one, like this Ctrl-H text."
            ),
        ),
        FieldSpec(
            key="redraw_in_place", hotkey="r", menu_text=menu_key("R", "edraw style"), label="In-place redraw",
            render=lambda d: "on" if d["redraw_in_place"] else "off",
            prompt=live_choice_field(
                "redraw_in_place", [False, True],
                persist=lambda lane, v: lane.run(set_redraw_in_place_enabled, user, v),
            ),
            brief="Clear screen instead of scrolling",
            help=(
                "On: moving between screens clears the terminal instead of printing below "
                "what's already there -- less scrolling, but anything above the clear (like a "
                "save confirmation) disappears immediately. Off is the safer default -- it "
                "preserves scrollback."
            ),
        ),
        FieldSpec(
            key="identity_details", hotkey="n", menu_text=menu_key("N", "ame & details"),
            label="Name & details",
            render=lambda d: "(edit)",
            prompt=_identity_details_prompt,
            brief="Display name, location, age",
            help=(
                "Opens a separate screen for your display name, location, and birthdate -- "
                "each independently shown or hidden to other callers, plus your verified-"
                "badge and Link-attestation-sharing settings."
            ),
        ),
        FieldSpec(
            key="sort_preferences", hotkey="s", menu_text=menu_key("S", "ort preferences"),
            label="Sort preferences",
            render=lambda d: f"{d['sort_preference_count']} saved" if d["sort_preference_count"] else "none saved",
            prompt=_sort_preferences_prompt,
            brief="Manage saved sort orders",
            help=(
                "Lists the sort preferences you've saved so far (e.g. how boards or file "
                "areas are ordered) and lets you clear them. These are set implicitly "
                "wherever you actually pick a sort order, not edited directly here."
            ),
        ),
        FieldSpec(
            key="unicode_style", hotkey="u", menu_text=menu_key("U", "nicode style"),
            label="Unicode decorative style",
            render=lambda d: "on" if d["unicode_style"] else "off",
            prompt=live_choice_field(
                "unicode_style", [False, True],
                persist=lambda lane, v: lane.run(set_unicode_style_enabled, user, v),
            ),
            brief="Unicode arrows/bullets vs. plain ASCII",
            help=(
                "Whether menus/breadcrumbs use Unicode characters (›, ●, etc.) for a "
                "cleaner look, or fall back to plain ASCII ('/', '[X]', etc.) for a terminal "
                "that renders Unicode incorrectly."
            ),
        ),
        FieldSpec(
            key="breadcrumb_collapsed", hotkey="l", menu_text=menu_key("L", "ocation style"),
            label="Location style",
            render=lambda d: "always collapsed" if d["breadcrumb_collapsed"] else "auto",
            prompt=live_choice_field(
                "breadcrumb_collapsed", [False, True],
                persist=lambda lane, v: lane.run(set_breadcrumb_collapsed_enabled, user, v),
            ),
            brief="Always show only the current location, not the full path",
            help=(
                "On: every screen's heading shows only your current location (e.g. 'Trust "
                "policy') instead of the full path ('NetBBS › System › Trust policy'). The "
                "full path already collapses automatically when it doesn't fit your terminal "
                "-- this forces the short form even when there's room to spare."
            ),
        ),
        FieldSpec(
            key="ssh_public_key", hotkey="k", menu_text=menu_key("k", "ey", prefix="SSH public "),
            label="SSH public key",
            render=lambda d: d["ssh_fingerprint"] or "(none set)",
            prompt=_ssh_public_key_prompt,
            brief="Add/replace your SSH login key",
            help=(
                "Attaches an SSH public key to this account so you can log in over SSH with "
                "key-based authentication instead of (or alongside) your password. Paste it "
                "as base64, or a full 'ssh-ed25519 ...' line."
            ),
        ),
    ]

    await edit_resource_draft(
        session, lane,
        title="Your profile",
        subtitle="Your public identity and caller preferences.",
        fields=fields,
        draft=draft,
        back_menu_text=menu_key("B", "ack"),
        description_level=description_level,
        redraw_in_place=redraw_in_place,
        preamble=_preamble,
        unicode_style=unicode_style,
        collapsed=collapsed,
        accent_color=accent_color,
        header_color=header_color,
    )


async def _edit_bio(session: Session, lane: DatabaseLane, user: User) -> None:
    """
    Edits the bio via the fullscreen prose editor if `user` has opted
    in (`netbbs.net.editor_preference`), otherwise `netbbs.net.
    composition.edit_line_body` -- the same shared plain-line editor
    `netbbs.net.mail_flow` already uses for message bodies.

    Dogfood follow-up: this used to be a bespoke
    `for _ in range(MAX_BIO_LINES): read_line()` loop with one
    `try/except BioError` at the very end -- a byte-cap overrun was
    only ever discovered after every line had already been typed, and
    the *entire* draft was then discarded with no indication which
    line(s) to trim. `edit_line_body` validates each candidate as it's
    submitted and rejects only the addition that broke a limit,
    keeping everything already accepted -- plus `/cancel`, `/done`,
    and (via `draft_path`) the same crash-recovery offer the fullscreen
    path above already has. `set_bio`'s own `BioError` check below is
    still the final word either way -- belt-and-suspenders, not the
    only check anymore.

    One behavior change worth calling out: `edit_line_body` refuses to
    submit a genuinely blank body ("Body cannot be blank.", by design --
    it's shared with mail/post composition, which have no legitimate
    reason to be empty). The bespoke loop it replaces treated an
    immediate blank first line as "clear the bio," a real, if easy to
    trigger by accident, capability -- restored below as an explicit,
    confirmed step instead, only offered when there's an existing bio
    to lose.
    """
    if await lane.run(fullscreen_editor_enabled, user):
        current = await lane.run(get_bio, user) or ""
        result = await edit_prose(
            session, initial_text=current, draft_path=await lane.run(_bio_draft_path, user), max_bytes=MAX_BIO_BYTES,
            unicode_style=await lane.run(unicode_style_enabled, user),
        )
        if result is None:
            return
        text = result
    else:
        current = await lane.run(get_bio, user)
        if current and await prompt_yes_no(session, "Clear your bio instead of editing it?", default=False):
            await lane.run(set_bio, user, "")
            await session.write_line("Bio cleared.")
            return
        result = await edit_line_body(
            session,
            initial_text=current,
            max_bytes=MAX_BIO_BYTES,
            max_lines=MAX_BIO_LINES,
            draft_path=await lane.run(_bio_draft_path, user),
        )
        if result is None:
            return
        text = result

    try:
        await lane.run(set_bio, user, text)
    except BioError as exc:
        await session.write_line(colored(f"Could not save bio: {exc}", fg_color=MUTED_COLOR))
        return
    await session.write_line("Bio updated.")


def _bio_draft_path(db: Database, user: User) -> Path:
    return drafts_directory(db) / f"bio_{user.id}.draft"


async def _edit_signature(session: Session, lane: DatabaseLane, user: User) -> None:
    """Edits the signature auto-appended to mail/board posts
    (`netbbs.signature.append_signature`) -- same shape as `_edit_bio`
    immediately above (fullscreen prose editor or `edit_line_body`
    depending on `netbbs.net.editor_preference`, a clear-if-blank
    confirm, crash-recovery draft path), deliberately not deduplicated
    with it: the two edit genuinely different fields with different
    caps (`MAX_SIGNATURE_LINES`/`MAX_SIGNATURE_BYTES` vs. bio's own),
    and `_edit_bio`'s own docstring already explains why this shape
    exists over a bespoke line-at-a-time loop -- that reasoning applies
    here unchanged, not something worth re-deriving via a shared helper
    for two four-line call sites."""
    if await lane.run(fullscreen_editor_enabled, user):
        current = await lane.run(get_signature, user) or ""
        result = await edit_prose(
            session, initial_text=current, draft_path=await lane.run(_signature_draft_path, user),
            max_bytes=MAX_SIGNATURE_BYTES,
            unicode_style=await lane.run(unicode_style_enabled, user),
        )
        if result is None:
            return
        text = result
    else:
        current = await lane.run(get_signature, user)
        if current and await prompt_yes_no(session, "Clear your signature instead of editing it?", default=False):
            await lane.run(set_signature, user, "")
            await session.write_line("Signature cleared.")
            return
        result = await edit_line_body(
            session,
            initial_text=current,
            max_bytes=MAX_SIGNATURE_BYTES,
            max_lines=MAX_SIGNATURE_LINES,
            draft_path=await lane.run(_signature_draft_path, user),
        )
        if result is None:
            return
        text = result

    try:
        await lane.run(set_signature, user, text)
    except SignatureError as exc:
        await session.write_line(colored(f"Could not save signature: {exc}", fg_color=MUTED_COLOR))
        return
    await session.write_line("Signature updated.")


def _signature_draft_path(db: Database, user: User) -> Path:
    return drafts_directory(db) / f"signature_{user.id}.draft"


# -- identity attestation: self-reported profile fields (design doc §18) --


async def _identity_details_screen(session: Session, lane: DatabaseLane, user: User) -> None:
    """
    Self-reported `display_name`/`location`/`birthdate` plus the general
    "verified" badge visibility toggle (design doc §18) -- a separate
    screen from `_edit_profile`'s own bio/fullscreen-editor options
    rather than crowding four more fields onto that one menu. Each of
    `[D]isplay name`/`[L]ocation`/`[A]ge/birthdate` combines editing the
    value and setting its visibility into one action (unlike bio's
    separate edit/visibility actions) specifically to avoid needing
    eight top-level options for three fields.
    """
    description_level = await lane.run(menu_description_level, user)
    redraw_in_place = await lane.run(redraw_in_place_enabled, user)
    unicode_style = await lane.run(unicode_style_enabled, user)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, user)
    accent = await lane.run(effective_accent_color_256)
    header = await lane.run(effective_header_color_256)

    draft: Draft = {
        "display_name": await lane.run(get_display_name, user),
        "display_name_visible": await lane.run(is_display_name_visible, user),
        "location": await lane.run(get_location, user),
        "location_visible": await lane.run(is_location_visible, user),
        "birthdate": await lane.run(get_birthdate, user),
        "birthdate_visible": await lane.run(is_birthdate_visible, user),
        "verified_badge_visible": await lane.run(is_verified_badge_visible, user),
        "age_attestation": await lane.run(get_attestation, user, "age"),
        "name_attestation": await lane.run(get_attestation, user, "name"),
    }

    async def _display_name_prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        current = draft["display_name"]
        await session.write(f"\r\nDisplay name [{current or '(not set)'}] -- new value (blank to keep): ")
        new_value = (await session.read_line()).strip()
        if new_value:
            try:
                await lane.run(set_display_name, user, new_value)
            except ProfileFieldError as exc:
                await session.write_line(colored(f"Could not save display name: {exc}", fg_color=MUTED_COLOR))
                return
            draft["display_name"] = new_value
            await session.write_line("Display name updated.")
        visible = await prompt_yes_no(session, "Show it publicly?", default=False)
        await lane.run(set_display_name_visible, user, visible)
        draft["display_name_visible"] = visible

    async def _location_prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        current = draft["location"]
        await session.write(f"\r\nLocation [{current or '(not set)'}] -- new value (blank to keep): ")
        new_value = (await session.read_line()).strip()
        if new_value:
            try:
                await lane.run(set_location, user, new_value)
            except ProfileFieldError as exc:
                await session.write_line(colored(f"Could not save location: {exc}", fg_color=MUTED_COLOR))
                return
            draft["location"] = new_value
            await session.write_line("Location updated.")
        visible = await prompt_yes_no(session, "Show it publicly?", default=False)
        await lane.run(set_location_visible, user, visible)
        draft["location_visible"] = visible

    async def _birthdate_prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        current = draft["birthdate"]
        await session.write(
            f"\r\nBirthdate [{current.isoformat() if current else '(not set)'}] "
            "-- new value as YYYY-MM-DD (blank to keep): "
        )
        raw = (await session.read_line()).strip()
        if raw:
            try:
                new_birthdate = date.fromisoformat(raw)
            except ValueError:
                await session.write_line(colored("Not a valid date (expected YYYY-MM-DD).", fg_color=MUTED_COLOR))
                return
            try:
                await lane.run(set_birthdate, user, new_birthdate)
            except ProfileFieldError as exc:
                await session.write_line(colored(f"Could not save birthdate: {exc}", fg_color=MUTED_COLOR))
                return
            draft["birthdate"] = new_birthdate
            await session.write_line("Birthdate updated.")
        visible = await prompt_yes_no(session, "Show it publicly?", default=False)
        await lane.run(set_birthdate_visible, user, visible)
        draft["birthdate_visible"] = visible

    async def _remote_attestation_prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        await _remote_attestation_visibility_screen(session, lane, user)
        draft["age_attestation"] = await lane.run(get_attestation, user, "age")
        draft["name_attestation"] = await lane.run(get_attestation, user, "name")

    def _birthdate_render(d: Draft) -> str:
        birthdate = d["birthdate"]
        visibility = "public" if d["birthdate_visible"] else "private"
        if birthdate is None:
            return f"(not set) ({visibility})"
        return f"{birthdate.isoformat()} (age {compute_age(birthdate)}) ({visibility})"

    def _shared_render(d: Draft) -> str:
        shared = [
            attribute for attribute, attestation in
            (("age", d["age_attestation"]), ("name", d["name_attestation"]))
            if attestation is not None and attestation.link_visible
        ]
        return ", ".join(shared) if shared else "off"

    def _preamble(d: Draft) -> str:
        age_attestation, name_attestation = d["age_attestation"], d["name_attestation"]
        if age_attestation is None and name_attestation is None:
            return colored("Verified: (none)", fg_color=MUTED_COLOR)
        parts = [attr for attr, att in (("age", age_attestation), ("name", name_attestation)) if att is not None]
        return colored(f"Verified: {', '.join(parts)}", fg_color=accent)

    fields = [
        FieldSpec(
            key="display_name", hotkey="d", menu_text=menu_key("D", "isplay name"), label="Display name",
            render=lambda d: (
                f"{sanitize_text(d['display_name']) if d['display_name'] else '(not set)'} "
                f"({'public' if d['display_name_visible'] else 'private'})"
            ),
            prompt=_display_name_prompt,
            brief="Set your shown display name",
            help=(
                "An alternate name shown alongside your username, only if you answer 'Show "
                "it publicly?' yes when you set it. Self-reported and unverified -- distinct "
                "from a SysOp-verified real name (see 'Verified' above, and the Verified "
                "badge field below)."
            ),
        ),
        FieldSpec(
            key="location", hotkey="l", menu_text=menu_key("L", "ocation"), label="Location",
            render=lambda d: (
                f"{sanitize_text(d['location']) if d['location'] else '(not set)'} "
                f"({'public' if d['location_visible'] else 'private'})"
            ),
            prompt=_location_prompt,
            brief="Set your shown location",
            help=(
                "Free-text location (city, region, whatever you want), shown publicly only "
                "if you answer 'Show it publicly?' yes when you set it. Not validated or "
                "verified -- purely self-reported."
            ),
        ),
        FieldSpec(
            key="birthdate", hotkey="a", menu_text=menu_key("A", "ge/birthdate"), label="Birthdate",
            render=_birthdate_render,
            prompt=_birthdate_prompt,
            brief="Set your birthdate",
            help=(
                "Used to compute your age, which some boards/areas/channels require a "
                "minimum age to post or join. That age gate is checked against this value "
                "even if you keep it private -- 'Show it publicly?' only controls whether "
                "*other callers* can see your birthdate/age, not whether age gates apply."
            ),
        ),
        FieldSpec(
            key="verified_badge_visible", hotkey="v", menu_text=menu_key("V", "erified badge visibility"),
            label="Verified badge",
            render=lambda d: "public" if d["verified_badge_visible"] else "private",
            prompt=live_choice_field(
                "verified_badge_visible", [False, True],
                persist=lambda lane, v: lane.run(set_verified_badge_visible, user, v),
            ),
            brief="Show/hide your verified badge",
            help=(
                "Whether a badge marking your SysOp-verified real name/age is shown to other "
                "callers, once a SysOp has actually verified something. Has no effect until "
                "something is verified -- see 'Verified' at the top of this screen."
            ),
        ),
        FieldSpec(
            key="link_sharing", hotkey="r", menu_text=menu_key("R", "emote Link sharing"),
            label="Link attestation sharing",
            render=_shared_render,
            prompt=_remote_attestation_prompt,
            brief="Share attestations over Link",
            help=(
                "Whether your SysOp-verified age/name attestations are shared with linked "
                "nodes over NetBBS Link, so a remote node's trust/vouch policy can see them "
                "too. Off by default -- this node's own verification of you isn't shared "
                "elsewhere unless you opt in."
            ),
        ),
    ]

    await edit_resource_draft(
        session, lane,
        title="Name & details",
        fields=fields,
        draft=draft,
        back_menu_text=menu_key("B", "ack"),
        description_level=description_level,
        redraw_in_place=redraw_in_place,
        preamble=_preamble,
        unicode_style=unicode_style,
        collapsed=collapsed,
        accent_color=accent,
        header_color=header,
    )


async def _remote_attestation_visibility_screen(
    session: Session, lane: DatabaseLane, user: User
) -> None:
    await session.write_line("Share which attestation with explicitly trusted remote nodes?")
    await session.write_line(
        action_bar([menu_key("A", "ge"), menu_key("N", "ame"), menu_key("B", "ack")], width=session.terminal_width)
    )
    await session.write("Choice: ")
    attribute = {"a": "age", "n": "name"}.get((await session.read_key()).lower())
    if attribute is None:
        return
    attestation = await lane.run(get_attestation, user, attribute)
    if attestation is None:
        await session.write_line(colored(f"No {attribute} attestation exists.", fg_color=ERROR_COLOR))
        return
    if attestation.link_visible:
        await lane.run(set_attestation_link_visible, user, attribute, False)
        await session.write_line(colored(f"{attribute.title()} attestation Link sharing disabled.", fg_color=SUCCESS_COLOR))
        return
    if await prompt_yes_no(
        session,
        f"Allow this verified {attribute} value to be sent over NetBBS Link?",
        default=False,
    ):
        await lane.run(set_attestation_link_visible, user, attribute, True)
        await session.write_line(colored(f"{attribute.title()} attestation Link sharing enabled.", fg_color=SUCCESS_COLOR))
    else:
        await session.write_line(colored("Sharing remains disabled.", fg_color=MUTED_COLOR))


# -- identity attestation: the [V]erify main-menu screen (design doc §18) --


async def _verify_identity_menu(session: Session, db: Database, verifier: User) -> None:
    """
    Conditionally-visible main-menu entry for users with
    `can_verify_identity` (or SysOp level) -- lives at the main menu
    rather than inside the admin menu, since a granted verifier may not
    have admin access otherwise (design doc §18).
    """
    candidates = [u for u in list_users(db) if u.id != verifier.id]
    selected = await pick_item(
        session,
        candidates,
        name_of=lambda u: u.username,
        stable_id_of=lambda u: u.id,
        description_of=lambda u: _verification_status_description(db, u),
        title="Verify a user's identity",
        empty_message="No other users to verify.",
        redraw_in_place=redraw_in_place_enabled(db, verifier),
        unicode_style=unicode_style_enabled(db, verifier),
        collapsed=breadcrumb_collapsed_enabled(db, verifier),
        accent_color=effective_accent_color(session, db),
        header_color=effective_header_color(session, db),
    )
    if selected is not None:
        await _verify_user(session, db, verifier, selected)


def _verification_status_description(db: Database, user: User) -> str:
    parts = []
    if get_attestation(db, user, "age") is not None:
        parts.append("age verified")
    if get_attestation(db, user, "name") is not None:
        parts.append("name verified")
    return ", ".join(parts) if parts else "not verified"


async def _verify_user(session: Session, db: Database, verifier: User, subject: User) -> None:
    await session.write_line(
        colored(f"\r\nVerifying {sanitize_text(subject.username)!r}:", fg_color=effective_header_color(session, db), bold=True)
    )

    self_birthdate = get_birthdate(db, subject)
    self_display_name = get_display_name(db, subject)
    await session.write_line(
        f"Self-reported birthdate: {self_birthdate.isoformat() if self_birthdate else '(not set)'}"
    )
    await session.write_line(
        f"Self-reported display name: {sanitize_text(self_display_name) if self_display_name else '(not set)'}"
    )

    existing_age = get_attestation(db, subject, "age")
    if existing_age is not None:
        await session.write_line(f"Currently attested birthdate: {existing_age.attested_value}")
    existing_name = get_attestation(db, subject, "name")
    if existing_name is not None:
        await session.write_line(f"Currently attested real name: {sanitize_text(existing_name.attested_value)}")

    if await prompt_yes_no(session, "\r\nAttest a birthdate?", default=False):
        await session.write("Attested birthdate (YYYY-MM-DD): ")
        raw = (await session.read_line()).strip()
        try:
            birthdate = date.fromisoformat(raw)
            attest_age(db, subject, birthdate, verifier=verifier)
        except (ValueError, AttestationError) as exc:
            await session.write_line(colored(f"Could not attest age: {exc}", fg_color=MUTED_COLOR))
        else:
            await session.write_line("Age attested.")

    if await prompt_yes_no(session, "Attest a real name?", default=False):
        await session.write("Attested real name: ")
        raw = (await session.read_line()).strip()
        try:
            attest_name(db, subject, raw, verifier=verifier)
        except AttestationError as exc:
            await session.write_line(colored(f"Could not attest name: {exc}", fg_color=MUTED_COLOR))
        else:
            await session.write_line("Real name attested.")
    else:
        await session.write_line("")
