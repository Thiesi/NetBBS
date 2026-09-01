"""
Session entry: turns a raw connection into an authenticated interactive
session -- `handle_session`/`handle_ssh_session`/`run_authenticated_
session` (the Telnet/SSH/web transports' own shared entry points),
login and new-account registration, background account-revocation
watching, and every connection-lifecycle notice (maintenance lockout,
throttling, idle timeout) shown along the way. Once a session is
authenticated, control passes to `netbbs.net.main_menu._main_menu` and
never returns here except to tear the session back down.

This is what remains of what used to be one large module covering the
entire interactive experience -- login, the main menu, and every screen
reachable from it. Split apart for maintainability (each extraction is
its own commit in the project history) into `netbbs.net.board_flow`
(message-board browsing/posting), `netbbs.net.scan_and_find` (`[N]ew
scan`/`[F]ind`), `netbbs.net.directory_flow` (the user directory and
`[W]ho's online`), `netbbs.net.profile_flow` (profile/identity editing,
session history), and `netbbs.net.main_menu` (the menu loop itself and
the shared Communities/Uncategorized/Jump-to resource-type sub-menu) --
this module now owns only session entry and authentication, the one
piece every other screen module is ultimately reached through.
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
    get_user_by_username,
)
from netbbs.chat import ChatHub, DirectChatInvites, MessageMailbox, PresenceRegistry, list_pending_invitations_for_user
from netbbs.config import RegistrationMode, get_node_display_name, get_registration_mode
from netbbs.link.boards import LinkContext
from netbbs.moderation import is_blocked
from netbbs.net.char_input import InputHistory
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.logoff_banner import load_logoff_banner
from netbbs.net.main_menu import _main_menu
from netbbs.net.maintenance import LOCKDOWN_MESSAGE, LOCKDOWN_NOTICE, MAINTENANCE_MESSAGE, MaintenanceMode
from netbbs.net.new_account_banner_after import load_new_account_banner_after
from netbbs.net.new_account_banner_before import load_new_account_banner_before
from netbbs.net.node_theme import effective_accent_color, effective_header_color_256, effective_node_name_gradient
from netbbs.net.nodeconfig import ThrottleConfig
from netbbs.net.redraw_preference import set_redraw_in_place_enabled
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
    colored,
    reflow,
    sanitize_text,
    screen_title,
    status_badge,
)
from netbbs.session_history import record_session_end, record_session_start
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

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
    `netbbs.net.main_menu._draw_main_menu`/`_show_pending_invitations`) shows full detail
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
