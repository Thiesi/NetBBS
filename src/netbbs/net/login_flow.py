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
from netbbs.chat import (
    ChatHub,
    MessageMailbox,
    PresenceRegistry,
    format_with_preference,
    list_pending_invitations_for_user,
)
from netbbs.chat.channels import Channel
from netbbs.communities import (
    Community,
    get_effective_min_age,
    get_effective_min_read_level,
    get_effective_min_write_level,
    get_effective_name_requirement,
    list_communities,
)
from netbbs.config import RegistrationMode, get_registration_mode
from netbbs.directory import (
    MAX_BIO_BYTES,
    MAX_BIO_LINES,
    BioError,
    get_bio,
    get_vcard,
    is_bio_visible,
    set_bio,
    set_bio_visible,
)
from netbbs.files.areas import FileArea, list_file_areas
from netbbs.link.boards import (
    LinkContext,
    queue_board_post_edit_if_linked,
    queue_board_post_if_linked,
    queue_board_post_moderator_edit_if_linked,
    queue_board_post_tombstone_if_linked,
)
from netbbs.mail import unread_count as unread_mail_count
from netbbs.moderation import BoardPermission, has_permission, is_blocked
from netbbs.net.admin_flow import admin_menu
from netbbs.net.char_input import InputHistory
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.chat_flow import browse_channels, has_visible_channels, list_visible_channels_for
from netbbs.net.editor_preference import fullscreen_editor_enabled, set_fullscreen_editor_enabled
from netbbs.net.file_flow import browse_file_areas, enter_file_area, has_visible_areas
from netbbs.net.mail_flow import browse_mail
from netbbs.net.maintenance import LOCKDOWN_MESSAGE, LOCKDOWN_NOTICE, MAINTENANCE_MESSAGE, MaintenanceMode
from netbbs.net.nodeconfig import ThrottleConfig
from netbbs.net.picker import pick_item
from netbbs.net.prose_editor import edit_prose
from netbbs.net.session import Session, SessionClosedError
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.net.shutdown import NodeControls, SequenceScheduler, format_remaining_seconds
from netbbs.net.throttle import LoginThrottle
from netbbs.net.welcome_banner import load_welcome_banner
from netbbs.permissions import meets_level
from netbbs.rendering import (
    ACCENT_COLOR,
    ALERT_COLOR,
    HEADER_COLOR,
    MUTED_COLOR,
    colored,
    menu_key,
    reflow,
    reject_keystroke,
    sanitize_text,
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
        if shutdown_scheduler is not None and shutdown_scheduler.is_scheduled():
            remaining = shutdown_scheduler.remaining_seconds()
            await session.write_line(f"{MAINTENANCE_MESSAGE} (going down in {format_remaining_seconds(remaining)})")
        else:
            await session.write_line(MAINTENANCE_MESSAGE)
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
            node_controls=node_controls, lane=lane, link_context=link_context,
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
        await session.write_line(load_welcome_banner(db))
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
            await session.write_line("\r\nLogin timed out. Goodbye.")
            return
    finally:
        throttle.leave_unauthenticated()

    if login_result is LoginOutcome.ATTEMPTS_EXHAUSTED:
        await session.write_line("Too many failed attempts. Goodbye.")
        return
    if login_result is LoginOutcome.IDLE_TIMEOUT:
        await session.write_line("\r\nTimed out waiting for input. Goodbye.")
        return
    if login_result is LoginOutcome.THROTTLED:
        await session.write_line(
            "\r\nToo many login attempts. Please try again later."
        )
        return
    if login_result is LoginOutcome.BLOCKED:
        return

    await run_authenticated_session(
        session, db, hub, presence, mailbox, login_result,
        node_controls=node_controls, lane=lane, link_context=link_context,
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
    if (
        node_controls is not None
        and node_controls.maintenance.is_lockdown_active()
        and not meets_level(user, SYSOP_LEVEL)
    ):
        await session.write_line(f"\r\n{LOCKDOWN_MESSAGE}")
        return

    welcome = f"\r\nWelcome, {sanitize_text(user.username)}! You are level {user.user_level}."
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

    presence.enter(user.username)
    watcher_task: asyncio.Task | None = None
    if node_controls is not None:
        node_controls.session_registry.mark_authenticated(
            session, user.username, is_sysop=meets_level(user, SYSOP_LEVEL)
        )
        watcher_task = asyncio.create_task(
            _watch_for_account_revocation(session, db, user, node_controls.session_registry)
        )
    try:
        await _main_menu(
            session, db, hub, presence, mailbox, history, user,
            node_controls=node_controls, lane=lane, link_context=link_context,
        )
    finally:
        presence.leave(user.username)
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

    await session.write_line("\r\nGoodbye!")


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
        await session.write_line("\r\nYour account is no longer available. Goodbye.")
        return LoginOutcome.BLOCKED
    if user.disabled_at is not None:
        await session.write_line("\r\nYour account is no longer available. Goodbye.")
        return LoginOutcome.BLOCKED
    if is_blocked(db, user):
        await session.write_line("\r\nYour access to this system has been revoked.")
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
        if shutdown_scheduler is not None and shutdown_scheduler.is_scheduled():
            remaining = shutdown_scheduler.remaining_seconds()
            await session.write_line(f"{MAINTENANCE_MESSAGE} (going down in {format_remaining_seconds(remaining)})")
        else:
            await session.write_line(MAINTENANCE_MESSAGE)
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
            await session.write_line("\r\nSSH authentication did not complete. Goodbye.")
            return

        result = await _authorize_ssh_authenticated_user(session, db, username)
        if isinstance(result, LoginOutcome):
            return
        await run_authenticated_session(
            session, db, hub, presence, mailbox, result,
            node_controls=node_controls, lane=lane, link_context=link_context,
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
            f"\r\n*** You have {len(pending)} pending channel invitation{plural}. "
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
    header = colored("Pending invitations:", fg_color=HEADER_COLOR, bold=True)
    await session.write_line(f"\r\n{header}")
    if not pending:
        await session.write_line("You have no pending channel invitations.")
        return
    for invitation in pending:
        when = format_for_display(invitation.created_at, db)
        await session.write_line(
            f"  #{sanitize_text(invitation.channel_name)} "
            f"-- invited by {sanitize_text(invitation.invited_by_username)} ({when})"
        )
    await session.write_line(
        colored(
            "Use [C]hat, then /join <channel> from the channel picker to accept one.",
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
    actually asked for) and, at most one at a time (most urgent first: a
    scheduled shutdown, then a scheduled drain, then -- SysOps only,
    since a non-SysOp who reached the menu at all already implies
    lockdown isn't blocking them -- maintenance mode being on), a visual
    alert tag. `None` (a direct test call site bypassing `handle_
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
    """
    for text, created_at in mailbox.flush(session):
        await session.write_line(format_with_preference(db, user, text, created_at))

    header = colored("Main menu:", fg_color=HEADER_COLOR, bold=True)
    unread = unread_mail_count(db, user)
    mail_label = f"-mail ({unread} unread)" if unread else "-mail"
    option_list = []
    if _has_visible_communities(db, user):
        option_list.append(menu_key("C", "ommunities"))
    if _has_uncategorized_resources(db, user):
        option_list.append(menu_key("U", "ncategorized"))
    option_list.append(menu_key("J", "ump to..."))
    option_list.append(menu_key("N", "ew scan"))
    option_list.append(menu_key("F", "ind"))
    option_list.extend(
        [
            menu_key("D", "irectory"),
            menu_key("P", "rofile"),
            menu_key("E", mail_label),
        ]
    )
    if list_pending_invitations_for_user(db, user):
        option_list.append(menu_key("I", "nvitations"))
    if user.can_verify_identity or meets_level(user, SYSOP_LEVEL):
        option_list.append(menu_key("V", "erify"))
    if meets_level(user, SYSOP_LEVEL):
        option_list.append(menu_key("S", "ysOp"))
    option_list.append(menu_key("L", "ogoff"))
    options = "  ".join(option_list)
    await session.write_line(f"\r\n{header} {options}")
    await session.write(_main_menu_prompt(db, user, node_controls))


def _main_menu_prompt(db: Database, user: User, node_controls: NodeControls | None) -> str:
    """`Choice: `, optionally prefixed with the current BBS time and a
    node-status alert tag -- see `_draw_main_menu`'s own docstring for
    why `node_controls is None` leaves this completely unchanged."""
    if node_controls is None:
        return "Choice: "

    time_str = format_for_display(utc_now_iso(), db)
    tag = ""
    if node_controls.shutdown_scheduler.is_scheduled():
        remaining = node_controls.shutdown_scheduler.remaining_seconds()
        tag = colored(f"[SHUTDOWN {format_remaining_seconds(remaining)}] ", fg_color=ALERT_COLOR, bold=True)
    elif node_controls.drain_scheduler.is_scheduled():
        remaining = node_controls.drain_scheduler.remaining_seconds()
        tag = colored(f"[DRAINING {format_remaining_seconds(remaining)}] ", fg_color=ALERT_COLOR, bold=True)
    elif node_controls.maintenance.is_lockdown_active():
        # Only ever reached by a SysOp -- a non-SysOp who made it to the
        # main menu at all already implies lockdown wasn't blocking them.
        tag = colored("[MAINT MODE] ", fg_color=ALERT_COLOR, bold=True)
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
    """
    await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
    while True:
        choice = (await session.read_key()).lower()

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

        if choice == "l":
            await session.write_line("")
            return
        elif choice == "c" and _has_visible_communities(db, user):
            await session.write_line("")
            await _enter_communities(
                session, db, hub, presence, mailbox, history, user,
                node_controls=node_controls, lane=lane, link_context=link_context,
            )
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
        elif choice == "u" and _has_uncategorized_resources(db, user):
            await session.write_line("")
            await _enter_uncategorized(
                session, db, hub, presence, mailbox, history, user,
                node_controls=node_controls, lane=lane, link_context=link_context,
            )
            await _draw_main_menu(session, db, mailbox, user, node_controls=node_controls)
        elif choice == "j":
            await session.write_line("")
            await _jump_to(
                session, db, hub, presence, mailbox, history, user,
                node_controls=node_controls, lane=lane, link_context=link_context,
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
            await _edit_profile(session, db, user)
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
            await session.write(reject_keystroke())


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

    await session.write_line(colored("\r\nNew scan:", fg_color=HEADER_COLOR, bold=True))
    if replies:
        await session.write_line(f"Replies to you: {len(replies)}")
        for reply in replies[:10]:
            reply_board = boards_by_id.get(reply.board_id)
            board_label = sanitize_text(reply_board.name) if reply_board is not None else "unknown board"
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
    query, never persisted -- same `stable_id_of=lambda item: id(item)`
    idiom as `_ScanItem`."""

    kind: str  # "post" | "file" | "channel_message"
    name: str
    description: str
    post: PostSearchHit | None = None
    file: FileSearchHit | None = None
    message: ChannelMessageSearchHit | None = None


# A channel message's whole body would otherwise stand in as its list
# "name" -- trimmed to a scannable snippet length, same spirit as
# _ScanItem's "replies to you" list capping at 10 (a display shaping
# choice, unrelated to and separate from pick_item's own sanitize_text
# call, which still runs on whatever (possibly still-long) string this
# produces).
_MESSAGE_SNIPPET_LENGTH = 80


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
    await session.write("\r\nSearch (or press Enter to cancel): ")
    query = (await session.read_line()).strip()
    if not query:
        await session.write_line(colored("Search cancelled.", fg_color=MUTED_COLOR))
        return

    def _load(db: Database) -> list[_SearchResultItem]:
        items: list[_SearchResultItem] = []
        for hit in search_posts(db, user, query):
            items.append(
                _SearchResultItem(
                    kind="post", name=hit.subject, description=f"post, {hit.board.name}", post=hit,
                )
            )
        for hit in search_files(db, user, query):
            items.append(
                _SearchResultItem(
                    kind="file", name=hit.filename, description=f"file, {hit.area.name}", file=hit,
                )
            )
        visible_channels = list_visible_channels_for(db, user)
        for hit in search_channel_messages(db, user, query, visible_channels=visible_channels):
            snippet = hit.body[:_MESSAGE_SNIPPET_LENGTH]
            if len(hit.body) > _MESSAGE_SNIPPET_LENGTH:
                snippet += "..."
            items.append(
                _SearchResultItem(
                    kind="channel_message", name=snippet,
                    description=f"chat, #{hit.channel.name} ({hit.author_label})", message=hit,
                )
            )
        return items

    items = await lane.run(_load)

    selected = await pick_item(
        session, items,
        name_of=lambda item: item.name,
        stable_id_of=lambda item: id(item),
        description_of=lambda item: item.description,
        title=f"Search results for {query!r}",
        empty_message="No matches.",
    )
    if selected is None:
        return

    if selected.kind == "post":
        cursor = await lane.run(post_jump_cursor, selected.post.board.id, selected.post.root_post_id)
        await _show_board(session, db, selected.post.board, user, link_context=link_context, initial_cursor=cursor)
    elif selected.kind == "file":
        cursor = await lane.run(file_jump_cursor, selected.file.area.id, selected.file.file_id)
        await enter_file_area(session, lane, selected.file.area, user, initial_cursor=cursor, link_context=link_context)
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
    prompt = (
        "\r\nUsername: "
        if registration_mode == RegistrationMode.CLOSED
        else "\r\nNew here? Type 'new' to create an account.\r\nUsername: "
    )

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
                    "This system does not accept public registrations. Contact the SysOp for an account."
                )
                continue
            new_user = await _register_new_account(
                session, db, throttle, idle_timeout=idle_timeout, registration_mode=registration_mode
            )
            if new_user is not None:
                return new_user
            continue

        try:
            await session.write("Password: ")
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
                await session.write_line(f"Login failed. {remaining} attempt(s) remaining.")
            else:
                await session.write_line("Login failed.")
            continue

        if is_blocked(db, user):
            # A distinct message from the generic "Login failed" above is
            # deliberate, not an information leak: this user has already
            # proven who they are via successful authentication, unlike
            # an anonymous prober still guessing passwords, so there's no
            # username-enumeration concern in telling them specifically
            # why they can't proceed.
            await session.write_line("Your access to this system has been revoked.")
            return LoginOutcome.BLOCKED

        return user

    return LoginOutcome.ATTEMPTS_EXHAUSTED


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
    """
    await session.write_line(
        colored(
            "\r\nCreating a new account. Press Enter with a blank username to cancel.",
            fg_color=MUTED_COLOR,
        )
    )
    try:
        await session.write("Desired username: ")
        username = (await asyncio.wait_for(session.read_line(), timeout=idle_timeout)).strip()
        if not username:
            return None

        await session.write(f"Password (min {MIN_REGISTRATION_PASSWORD_LENGTH} characters): ")
        password = await asyncio.wait_for(session.read_line(echo=False), timeout=idle_timeout)
        await session.write("Confirm password: ")
        confirm = await asyncio.wait_for(session.read_line(echo=False), timeout=idle_timeout)
    except asyncio.TimeoutError:
        return None

    if len(password) < MIN_REGISTRATION_PASSWORD_LENGTH:
        await session.write_line(
            colored(
                f"Password must be at least {MIN_REGISTRATION_PASSWORD_LENGTH} characters.",
                fg_color=MUTED_COLOR,
            )
        )
        return None
    if password != confirm:
        await session.write_line(colored("Passwords did not match.", fg_color=MUTED_COLOR))
        return None

    # Same node-wide budget _login's own password attempts consume
    # (issue #3) -- keyed by the *desired* username rather than an
    # authenticating one, but the same per-source/per-username/global
    # token buckets, checked before the expensive Argon2 hash below runs
    # (create_user_async), for the identical reason _login checks it
    # before authenticate_password_async.
    if not throttle.allow_attempt(source=session.peer_address, username=username):
        await session.write_line(
            colored("Too many registration attempts. Please try again later.", fg_color=MUTED_COLOR)
        )
        return None

    require_approval = registration_mode == RegistrationMode.APPROVAL_REQUIRED
    try:
        new_user = await create_user_async(db, username, password=password, pending_approval=require_approval)
    except AuthError as exc:
        await session.write_line(colored(f"Could not create account: {exc}", fg_color=MUTED_COLOR))
        return None

    if require_approval:
        await session.write_line(
            f"Account {new_user.username!r} created. A SysOp must approve it before you can log in."
        )
        return None

    await session.write_line(f"Account {new_user.username!r} created.")
    return new_user


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

        header = colored(f"\r\n{menu_header}:", fg_color=HEADER_COLOR, bold=True)
        option_list = []
        if show_boards:
            option_list.append(menu_key("M", "essage Boards"))
        if show_channels:
            option_list.append(menu_key("C", "hat"))
        if show_areas:
            option_list.append(menu_key("F", "ile areas"))
        option_list.append(menu_key("B", "ack"))
        await session.write_line(f"{header} {'  '.join(option_list)}")
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
                    link_context=link_context,
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
        else:
            await session.write(reject_keystroke())


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
    )
    if selected is None:
        return
    await _resource_type_menu(
        session, db, hub, presence, mailbox, history, user, node_controls=node_controls,
        community_id=selected.id, community_scoped=True,
        menu_header=selected.name, title_prefix=selected.name, lane=lane, link_context=link_context,
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
) -> None:
    """`[U]ncategorized` entry point -- straight into the shared
    resource-type sub-menu, no picker needed (there's only one
    Uncategorized "bucket")."""
    await _resource_type_menu(
        session, db, hub, presence, mailbox, history, user, node_controls=node_controls,
        community_id=None, community_scoped=True,
        menu_header="Uncategorized", title_prefix="Uncategorized", lane=lane, link_context=link_context,
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
    name) that becomes "{title_prefix} — message boards" otherwise.
    Category leak prevention ("only show/offer categories
    currently used by ≥1 resource in this Community") only applies when
    `community_scoped` -- the unfiltered Jump path shows every category
    exactly as it always has.
    """
    # name_requirement deliberately does not gate reading here -- it's a
    # participation/accountability requirement (design doc §18 point 7:
    # "mutual visible accountability" among people posting), not a
    # content-restriction the way min_age is; see can_post's own check,
    # below, for where it actually applies.
    all_boards = [
        b for b in list_boards(db)
        if meets_level(user, get_effective_min_read_level(db, b)) and meets_age(db, user, get_effective_min_age(db, b))
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

    title = f"{title_prefix} — message boards" if title_prefix is not None else "Available message boards"

    if not categories_here:
        board = await pick_item(
            session,
            boards_here,
            name_of=lambda b: b.name,
            stable_id_of=lambda b: b.id,
            description_of=lambda b: b.description,
            title=title,
            empty_message="No message boards are available to you yet.",
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

    selected = await pick_item(
        session,
        mixed,
        name_of=render_name,
        stable_id_of=stable_id,
        description_of=render_description,
        title=title,
        empty_message="No message boards are available to you yet.",
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
) -> None:
    """Renders one page of posts plus its navigation options — the unit
    that should be redrawn on an actual page change (initial entry,
    Older/Newer/Recent), not on every loop iteration regardless of
    whether anything changed."""
    await _render_post_page(session, db, board_name, page, name_requirement=name_requirement)
    options = []
    if page.has_older:
        options.append(menu_key("O", "lder"))
    if page.has_newer:
        options.append(menu_key("N", "ewer"))
        options.append(menu_key("R", "ecent"))
    if any(_can_edit_post(db, post, user) for post in page.posts):
        options.append(menu_key("E", "dit"))
    if any(_can_tombstone_post(db, post, user) for post in page.posts):
        options.append(menu_key("T", "ombstone"))
    if can_post:
        options.append(menu_key("P", "ost"))
    options.append(menu_key("B", "ack"))
    await session.write_line(f"\r\n{'  '.join(options)}")
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
        )
        if current_page.posts:
            record_board_seen(db, user, board, current_page.posts[-1])

    async def _compose_new_post() -> None:
        await session.write("\r\nSubject (or press Enter to cancel): ")
        subject = (await session.read_line()).strip()
        if not subject:
            await session.write_line(colored("Post cancelled.", fg_color=MUTED_COLOR))
            return
        body = await _compose_body(
            session, db, user, draft_path=_post_draft_path(db, kind="new", board=board, user=user)
        )
        if body is None:
            await session.write_line(colored("Post cancelled.", fg_color=MUTED_COLOR))
            return
        try:
            post = create_post(db, board, user, subject, body)
        except PostError as exc:
            await session.write_line(colored(f"Could not create post: {exc}", fg_color=MUTED_COLOR))
            return
        if link_context is not None:
            queue_board_post_if_linked(db, post, board, node_identity=link_context.node_identity)
        await session.write_line(f"Posted (id {post.post_id[:12]}...).")

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
        # Deliberately still skips the navigation loop entirely --
        # nothing to browse, so there's nothing for [O]lder/[N]ewer/
        # [E]dit/[B]ack to do here anyway (see
        # test_empty_board_never_enters_the_navigation_loop). Goes
        # straight to composing the first post when the user's allowed
        # to; unrelated to issues #39/#40, which were about the
        # non-empty case's [B]ack silently triggering a post prompt.
        await session.write_line(f"\r\n[{board_name}] has no posts yet.")
        if can_post:
            await _compose_new_post()
        return

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
            await session.write(reject_keystroke())


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

    body = await _compose_body(
        session,
        db,
        user,
        initial_text=post.body,
        draft_path=_post_draft_path(db, kind="edit", board=board, user=user, root_post_id=post.root_post_id),
    )
    if body is None:
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
    """A stable per-(user, board, [post]) autosave draft location for
    `netbbs.net.prose_editor.edit_prose`, colocated with the node's
    database the same way `netbbs.net.welcome_banner.banner_path`
    already colocates its own single global draft -- there just needs
    to be more than one slot here, one per in-progress
    composition/edit, so this lives in its own subdirectory rather than
    a single flat sibling file."""
    directory = db.path.parent / f"{db.path.name}_drafts"
    directory.mkdir(parents=True, exist_ok=True)
    suffix = f"_{root_post_id}" if root_post_id else ""
    return directory / f"{kind}_{board.id}_{user.id}{suffix}.draft"


async def _compose_body(
    session: Session, db: Database, user: User, *, initial_text: str | None = None, draft_path: Path
) -> str | None:
    """The single place a post body (or an edit of one) is actually
    entered: the fullscreen prose editor if `user` has opted in
    (`netbbs.net.editor_preference`), otherwise the original plain
    single-line prompt every account still sees by default. Returns
    `None` only for the fullscreen path's genuine cancel (quit without
    saving) -- the plain-line fallback has no equivalent "cancel"
    concept and always returns a string, matching its unchanged
    existing behavior for every account that hasn't opted in. The
    plain path has no way to *pre-fill* a line-based prompt with
    `initial_text` (unlike the subject field's own "[current] (Enter
    to keep)" pattern, a whole body is too long to inline into a
    prompt that way) -- shown as read-only context above the prompt
    instead, on an edit, so the current content isn't simply invisible
    to anyone who hasn't opted into the fullscreen editor."""
    if fullscreen_editor_enabled(db, user):
        return await edit_prose(
            session, initial_text=initial_text, draft_path=draft_path, max_bytes=MAX_BODY_BYTES
        )
    if initial_text:
        await session.write_line(colored("Current body:", fg_color=MUTED_COLOR))
        await session.write_line(reflow(sanitize_text(initial_text, allow_newlines=True), width=session.terminal_width))
        await session.write("New body (Enter to keep unchanged): ")
        return (await session.read_line()).strip() or initial_text
    await session.write("Body: ")
    return (await session.read_line()).strip()


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


async def _render_post_page(
    session: Session, db: Database, board_name: str, page: PostPage, *, name_requirement: str | None
) -> None:
    header = colored(f"[{board_name}]", fg_color=HEADER_COLOR, bold=True)
    await session.write_line(f"\r\n{header}")
    for position, post in enumerate(page.posts, start=1):
        when = format_for_display(post.created_at, db)
        edited_marker = " (edited)" if post.is_edited else ""
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
            colored(f"[{position}] {sanitize_text(post.subject)} -- ", fg_color=ACCENT_COLOR)
            + author_display
            + colored(f" ({when}){edited_marker}", fg_color=ACCENT_COLOR)
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
        await session.write_line(reflow(body, width=session.terminal_width))


# -- user directory & vCard/finger (design doc §13) ------


async def _browse_directory(session: Session, db: Database, user: User) -> None:
    """
    The user directory: a table-style listing of every registered
    account (`netbbs.auth.users.list_users`). Selecting an entry shows
    their full finger/vCard detail (`_show_vcard`) — bio visibility is
    per-target, not a directory-wide filter, so everyone appears in
    the listing regardless of whether their bio itself is public.
    """
    users = list_users(db)
    selected = await pick_item(
        session,
        users,
        name_of=lambda u: u.username,
        stable_id_of=lambda u: u.id,
        description_of=lambda u: _directory_description(db, u),
        title="User directory",
        empty_message="No registered users yet.",
    )
    if selected is not None:
        await _show_vcard(session, db, selected, user)


def _directory_description(db: Database, target: User) -> str:
    when = format_for_display(target.created_at, db)
    bio_state = "public" if is_bio_visible(db, target) else "private"
    return f"member since {when}, bio: {bio_state}"


async def _show_vcard(session: Session, db: Database, target: User, requesting_user: User) -> None:
    """finger-style detail view — `get_vcard` already resolves
    visibility (always visible to yourself, otherwise only if the
    target has opted in)."""
    vcard = get_vcard(db, target, requesting_user=requesting_user)
    when = format_for_display(vcard.created_at, db)
    header = colored(sanitize_text(vcard.username), fg_color=HEADER_COLOR, bold=True)
    await session.write_line(f"\r\n{header}")
    await session.write_line(f"Member since: {when}")
    if vcard.bio is not None:
        await session.write_line(
            reflow(sanitize_text(vcard.bio, allow_newlines=True), width=session.terminal_width)
        )
    else:
        await session.write_line(colored("(no public bio)", fg_color=MUTED_COLOR))


async def _render_profile(session: Session, db: Database, user: User) -> bool:
    """Renders the profile state plus its option line — the unit that
    should be redrawn on an actual state change (initial entry, an edit
    or a visibility toggle), not on every loop iteration regardless of
    whether anything changed. Returns the current visibility, needed by
    the caller to pass into `_toggle_bio_visibility`."""
    current_bio = get_bio(db, user)
    visible = is_bio_visible(db, user)
    editor_on = fullscreen_editor_enabled(db, user)

    await session.write_line(colored("\r\nYour profile:", fg_color=HEADER_COLOR, bold=True))
    if current_bio:
        await session.write_line(
            reflow(sanitize_text(current_bio, allow_newlines=True), width=session.terminal_width)
        )
    else:
        await session.write_line(colored("(no bio set)", fg_color=MUTED_COLOR))
    await session.write_line(f"Visibility: {'public' if visible else 'private'}")
    await session.write_line(f"Fullscreen editor for posts/bio: {'on' if editor_on else 'off'}")

    options = "  ".join(
        [
            menu_key("E", "dit bio"),
            menu_key("V", "isibility"),
            menu_key("F", "ullscreen editor"),
            menu_key("N", "ame & details"),
            menu_key("B", "ack"),
        ]
    )
    await session.write_line(f"\r\n{options}")
    await session.write("Choice: ")
    return visible


async def _edit_profile(session: Session, db: Database, user: User) -> None:
    """
    Edit your own vCard: the bio, its visibility toggle, and the
    fullscreen-editor composition preference (design doc). Shows the
    current state first, then a small
    sub-menu — matches this codebase's existing "show state, then
    offer actions" shape (e.g. `netbbs.net.file_flow._show_area`)
    rather than jumping straight into an edit prompt.

    Redraws the (possibly just-updated) state only after an edit or
    toggle actually happens, not on every loop iteration — mirrors
    `_show_board`'s `_render_board_page` split. An unrecognized key
    sounds a bell and leaves the screen exactly as it was, no reprinted
    prompt (design doc), same as the main menu and the picker.
    """
    visible = await _render_profile(session, db, user)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "e":
            await session.write_line("")
            await _edit_bio(session, db, user)
            visible = await _render_profile(session, db, user)
        elif choice == "v":
            await session.write_line("")
            await _toggle_bio_visibility(session, db, user, currently_visible=visible)
            visible = await _render_profile(session, db, user)
        elif choice == "f":
            await session.write_line("")
            set_fullscreen_editor_enabled(db, user, not fullscreen_editor_enabled(db, user))
            await session.write_line(
                f"Fullscreen editor is now {'on' if fullscreen_editor_enabled(db, user) else 'off'}."
            )
            visible = await _render_profile(session, db, user)
        elif choice == "n":
            await session.write_line("")
            await _identity_details_screen(session, db, user)
            visible = await _render_profile(session, db, user)
        else:
            await session.write(reject_keystroke())


async def _edit_bio(session: Session, db: Database, user: User) -> None:
    """
    Edits the bio via the fullscreen prose editor if `user` has opted
    in (`netbbs.net.editor_preference`), otherwise the original
    repeated-`read_line`-until-blank-line flow every account still sees
    by default. Either way, `set_bio`'s own `MAX_BIO_LINES` validation
    is the single place the line cap is actually enforced — the
    fullscreen editor doesn't duplicate that check, it just gets the
    same rejection message back if exceeded.
    """
    if fullscreen_editor_enabled(db, user):
        current = get_bio(db, user) or ""
        result = await edit_prose(
            session, initial_text=current, draft_path=_bio_draft_path(db, user), max_bytes=MAX_BIO_BYTES
        )
        if result is None:
            return
        text = result
    else:
        await session.write_line(
            f"\r\nEnter your bio, up to {MAX_BIO_LINES} lines. Blank line to finish."
        )
        lines: list[str] = []
        for _ in range(MAX_BIO_LINES):
            line = (await session.read_line()).strip()
            if not line:
                break
            lines.append(line)
        text = "\n".join(lines)

    try:
        set_bio(db, user, text)
    except BioError as exc:
        await session.write_line(colored(f"Could not save bio: {exc}", fg_color=MUTED_COLOR))
        return
    await session.write_line("Bio updated.")


def _bio_draft_path(db: Database, user: User) -> Path:
    directory = db.path.parent / f"{db.path.name}_drafts"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"bio_{user.id}.draft"


async def _toggle_bio_visibility(
    session: Session, db: Database, user: User, *, currently_visible: bool
) -> None:
    new_value = not currently_visible
    set_bio_visible(db, user, new_value)
    await session.write_line(f"Bio is now {'public' if new_value else 'private'}.")


# -- identity attestation: self-reported profile fields (design doc §18) --


async def _identity_details_screen(session: Session, db: Database, user: User) -> None:
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
    await _render_identity_details(session, db, user)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "d":
            await session.write_line("")
            await _edit_display_name(session, db, user)
            await _render_identity_details(session, db, user)
        elif choice == "l":
            await session.write_line("")
            await _edit_location(session, db, user)
            await _render_identity_details(session, db, user)
        elif choice == "a":
            await session.write_line("")
            await _edit_birthdate(session, db, user)
            await _render_identity_details(session, db, user)
        elif choice == "v":
            await session.write_line("")
            new_value = not is_verified_badge_visible(db, user)
            set_verified_badge_visible(db, user, new_value)
            await session.write_line(f"Verified badge is now {'public' if new_value else 'private'}.")
            await _render_identity_details(session, db, user)
        else:
            await session.write(reject_keystroke())


async def _render_identity_details(session: Session, db: Database, user: User) -> None:
    display_name = get_display_name(db, user)
    location = get_location(db, user)
    birthdate = get_birthdate(db, user)
    age_attestation = get_attestation(db, user, "age")
    name_attestation = get_attestation(db, user, "name")

    await session.write_line(colored("\r\nName & details:", fg_color=HEADER_COLOR, bold=True))
    await session.write_line(
        f"Display name: {sanitize_text(display_name) if display_name else '(not set)'} "
        f"({'public' if is_display_name_visible(db, user) else 'private'})"
    )
    await session.write_line(
        f"Location: {sanitize_text(location) if location else '(not set)'} "
        f"({'public' if is_location_visible(db, user) else 'private'})"
    )
    if birthdate is not None:
        await session.write_line(
            f"Birthdate: {birthdate.isoformat()} (age {compute_age(birthdate)}) "
            f"({'public' if is_birthdate_visible(db, user) else 'private'})"
        )
    else:
        await session.write_line(
            f"Birthdate: (not set) ({'public' if is_birthdate_visible(db, user) else 'private'})"
        )
    if age_attestation is not None or name_attestation is not None:
        verified_parts = []
        if age_attestation is not None:
            verified_parts.append("age")
        if name_attestation is not None:
            verified_parts.append("name")
        await session.write_line(
            colored(
                f"Verified: {', '.join(verified_parts)} "
                f"({'public' if is_verified_badge_visible(db, user) else 'private'})",
                fg_color=ACCENT_COLOR,
            )
        )
    else:
        await session.write_line(colored("Verified: (none)", fg_color=MUTED_COLOR))

    options = "  ".join(
        [
            menu_key("D", "isplay name"),
            menu_key("L", "ocation"),
            menu_key("A", "ge/birthdate"),
            menu_key("V", "erified badge visibility"),
            menu_key("B", "ack"),
        ]
    )
    await session.write_line(f"\r\n{options}")
    await session.write("Choice: ")


async def _edit_display_name(session: Session, db: Database, user: User) -> None:
    current = get_display_name(db, user)
    await session.write(f"\r\nDisplay name [{current or '(not set)'}] -- new value (blank to keep): ")
    new_value = (await session.read_line()).strip()
    if new_value:
        try:
            set_display_name(db, user, new_value)
        except ProfileFieldError as exc:
            await session.write_line(colored(f"Could not save display name: {exc}", fg_color=MUTED_COLOR))
            return
        await session.write_line("Display name updated.")
    set_display_name_visible(db, user, await prompt_yes_no(session, "Show it publicly?", default=False))


async def _edit_location(session: Session, db: Database, user: User) -> None:
    current = get_location(db, user)
    await session.write(f"\r\nLocation [{current or '(not set)'}] -- new value (blank to keep): ")
    new_value = (await session.read_line()).strip()
    if new_value:
        try:
            set_location(db, user, new_value)
        except ProfileFieldError as exc:
            await session.write_line(colored(f"Could not save location: {exc}", fg_color=MUTED_COLOR))
            return
        await session.write_line("Location updated.")
    set_location_visible(db, user, await prompt_yes_no(session, "Show it publicly?", default=False))


async def _edit_birthdate(session: Session, db: Database, user: User) -> None:
    current = get_birthdate(db, user)
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
            set_birthdate(db, user, new_birthdate)
        except ProfileFieldError as exc:
            await session.write_line(colored(f"Could not save birthdate: {exc}", fg_color=MUTED_COLOR))
            return
        await session.write_line("Birthdate updated.")
    set_birthdate_visible(db, user, await prompt_yes_no(session, "Show it publicly?", default=False))


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
        colored(f"\r\nVerifying {sanitize_text(subject.username)!r}:", fg_color=HEADER_COLOR, bold=True)
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
