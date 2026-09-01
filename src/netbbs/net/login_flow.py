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
from enum import Enum, auto

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
)
from netbbs.chat import (
    ChatHub,
    DirectChatInvites,
    MessageMailbox,
    PresenceRegistry,
    format_with_preference,
    list_pending_invitations_for_user,
)
from netbbs.communities import Community, list_communities
from netbbs.config import RegistrationMode, get_node_display_name, get_registration_mode
from netbbs.link.boards import LinkContext
from netbbs.mail import unread_count as unread_mail_count
from netbbs.moderation import is_blocked
from netbbs.net.admin_flow import admin_menu
from netbbs.net.board_flow import _browse_boards, _has_visible_boards
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.char_input import REDRAW_KEY, InputHistory, reject_unhandled_key
from netbbs.net.chat_flow import browse_channels, has_visible_channels, run_direct_chat_loop
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.directory_flow import _browse_directory, _caller_who_screen
from netbbs.net.door_flow import browse_doors, has_visible_doors
from netbbs.net.file_flow import browse_file_areas, has_visible_areas
from netbbs.net.logoff_banner import load_logoff_banner
from netbbs.net.mail_flow import browse_mail
from netbbs.net.main_menu_banner import load_main_menu_banner
from netbbs.net.maintenance import LOCKDOWN_MESSAGE, LOCKDOWN_NOTICE, MAINTENANCE_MESSAGE, MaintenanceMode
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.new_account_banner_after import load_new_account_banner_after
from netbbs.net.new_account_banner_before import load_new_account_banner_before
from netbbs.net.node_theme import (
    effective_accent_color,
    effective_accent_color_256,
    effective_clock_color_256,
    effective_header_color,
    effective_header_color_256,
    effective_node_name_gradient,
)
from netbbs.net.nodeconfig import ThrottleConfig
from netbbs.net.picker import pick_item
from netbbs.net.profile_flow import _edit_profile, _last_sessions_screen, _verify_identity_menu
from netbbs.net.redraw_preference import redraw_in_place_enabled, set_redraw_in_place_enabled
from netbbs.net.scan_and_find import _find_screen, _new_scan_screen
from netbbs.net.session import Session, SessionClosedError
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.net.shutdown import NodeControls, SequenceScheduler, format_remaining_seconds
from netbbs.net.throttle import LoginThrottle
from netbbs.net.unicode_style_preference import (
    set_unicode_style_enabled,
    unicode_style_enabled,
    unicode_style_ever_set,
)
from netbbs.net.welcome_banner import load_welcome_banner
from netbbs.permissions import meets_level
from netbbs.rendering import (
    ALERT_COLOR,
    ERROR_COLOR,
    HEADER_COLOR,
    LABEL_COLOR,
    METADATA_COLOR,
    MUTED_COLOR,
    SUCCESS_COLOR,
    VALUE_COLOR,
    WARNING_COLOR,
    MenuEntry,
    clear_screen,
    colored,
    field_row,
    menu_grid,
    menu_key,
    menu_row,
    reflow,
    sanitize_text,
    screen_title,
    status_badge,
)
from netbbs.session_history import record_session_end, record_session_start
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display, utc_now_iso

_MAX_LOGIN_ATTEMPTS = 3

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
                # Code review follow-up (PR #213): the profile screen's
                # own draft only ever received the updated fingerprint
                # for its own "Add"/"Replace"/"Clear" verb -- this loop's
                # own `user` was never refreshed, so every later branch
                # this session reaches (posting, uploading, chatting)
                # kept attributing to the pre-edit key even after it was
                # replaced or removed. `User` is frozen -- re-fetch
                # rather than mutate. Falls back to the pre-edit `user`
                # in the extreme, unlikely case a concurrent session
                # deleted this same account mid-edit -- `_main_menu`'s
                # own loop has no other path for "the account I'm
                # logged in as no longer exists" to unwind through here.
                #
                # Code review follow-up (PR #221): this ran get_user_by_id
                # directly against `db` on the interactive event-loop
                # coroutine instead of through `lane`, like every other
                # SQLite access this async UI flow performs -- under
                # contention or slow storage that blocks every other
                # Telnet/SSH/web session sharing this node's one
                # connection, not just this one.
                user = await lane.run(get_user_by_id, user.id) or user
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
    board, channel, file area, or door -- gates the main menu's
    `[U]ncategorized` entry the same "only offer what currently applies"
    way `[I]nvitations` already does. `community_id=None,
    community_scoped=True` filters each resource type to exactly its
    Uncategorized members -- see `_browse_boards_in_category`'s docstring
    for why `None` needs no special-casing here. Must stay in sync with
    `_resource_type_menu`'s own `show_doors` check below (dogfood-reported
    bug, GitHub issue #204: a lone uncategorized door game didn't surface
    this entry at all, even though the door was reachable once inside)."""
    return (
        _has_visible_boards(db, user, community_id=None, community_scoped=True)
        or has_visible_channels(db, user, community_id=None, community_scoped=True)
        or has_visible_areas(db, user, community_id=None, community_scoped=True)
        or has_visible_doors(db, user, community_id=None, community_scoped=True)
    )


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
            f"\r\n{menu_row(option_list, width=session.terminal_width, height=session.terminal_height, description_level=description_level)}"
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
