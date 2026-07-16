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
editor that's the actual reason it's needed (design doc round 26).
"""

from __future__ import annotations

import asyncio
from enum import Enum, auto
from pathlib import Path

from netbbs.auth.users import (
    SYSOP_LEVEL,
    AuthError,
    User,
    account_still_active,
    authenticate_password_async,
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
)
from netbbs.boards.categories import Category, list_subcategories, list_top_level_categories
from netbbs.chat import (
    ChatHub,
    MessageMailbox,
    PresenceRegistry,
    format_with_preference,
    list_pending_invitations_for_user,
)
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
from netbbs.moderation import BoardPermission, has_permission, is_blocked
from netbbs.net.admin_flow import admin_menu
from netbbs.net.char_input import InputHistory
from netbbs.net.chat_flow import browse_channels
from netbbs.net.editor_preference import fullscreen_editor_enabled, set_fullscreen_editor_enabled
from netbbs.net.file_flow import browse_file_areas
from netbbs.net.maintenance import MAINTENANCE_MESSAGE, MaintenanceMode
from netbbs.net.nodeconfig import ThrottleConfig
from netbbs.net.picker import pick_item
from netbbs.net.prose_editor import edit_prose
from netbbs.net.session import Session, SessionClosedError
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.net.shutdown import NodeControls
from netbbs.net.throttle import LoginThrottle
from netbbs.net.welcome_banner import load_welcome_banner
from netbbs.permissions import meets_level
from netbbs.rendering import (
    ACCENT_COLOR,
    HEADER_COLOR,
    MUTED_COLOR,
    colored,
    menu_key,
    reflow,
    reject_keystroke,
    sanitize_text,
)
from netbbs.storage.database import Database
from netbbs.timeutil import format_for_display

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
) -> None:
    """
    Top-level per-connection entry point.

    `shutdown_event`/`graceful_delay_seconds` (design doc -- node
    management round) are bundled with `session_registry`/`maintenance`
    into a `NodeControls`, threaded down through `_run_authenticated_
    session`/`_main_menu` to `netbbs.net.admin_flow.admin_menu` — what
    the in-session `[N]ode` admin command needs to trigger a shutdown
    directly, the same sequence a real OS signal already triggers (see
    `netbbs.net.shutdown`). Both optional/defaulted so every existing
    caller of this function (many tests, none of which exercise node
    management) needs no changes; `netbbs.__main__.run()` is the only
    caller that passes its own real values.

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

    `presence` (design doc round 32, sign-off round 42) is entered
    right before the main menu runs and left in a `finally` around it —
    this is the one place in the codebase that knows "this account now
    has one more/one fewer live connection", which `/away`'s "clears
    only when the account's final session disconnects" behavior
    depends on. Deliberately scoped to the authenticated portion only,
    same reasoning as the login-throttle budget above: an
    unauthenticated connection was never "present" as any account.

    `session_registry`/`maintenance` (design doc round 51) are checked/
    entered before any of that, right at the top — a deliberate node
    shutdown needs to reach and reject connections regardless of
    whether they ever authenticate at all, unlike `presence`, which
    only ever needs to know about accounts.
    """
    if maintenance.is_active():
        await session.write_line(MAINTENANCE_MESSAGE)
        return

    node_controls = NodeControls(
        session_registry=session_registry,
        maintenance=maintenance,
        shutdown_event=shutdown_event if shutdown_event is not None else asyncio.Event(),
        graceful_delay_seconds=graceful_delay_seconds,
    )

    session_registry.enter(session)
    try:
        await _run_authenticated_session(
            session, db, hub, presence, mailbox, throttle, throttle_config, node_controls=node_controls
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
) -> None:
    """The login-through-logoff body of a *Telnet/web* connection,
    wrapped by `handle_session`'s maintenance-mode check and session-
    registry bookkeeping (design doc round 51) — split out so those two
    concerns stay a thin, easy-to-read wrapper rather than adding
    another level of nesting to the whole function.

    Interactive-login-specific (the concurrent-unauthenticated-session
    budget, the username/password prompt loop) -- SSH has already
    proven identity before its own entry point, `handle_ssh_session`,
    is ever called, so it skips straight to `run_authenticated_session`
    below instead of going through this function at all (GitHub issue
    #25).

    `node_controls`, if given, is threaded straight through to
    `_main_menu`/`admin_menu` (design doc -- node management round);
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

    await run_authenticated_session(session, db, hub, presence, mailbox, login_result, node_controls=node_controls)


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
    `_main_menu`/`admin_menu` (design doc -- node management round);
    `None` is what a direct test call site (bypassing both entry
    points above) gets by default, which correctly hides the `[N]ode`
    admin option rather than needing every such test updated. The
    background account-revocation watcher (GitHub issue #29, reopened a
    second time) is gated on it the same way -- it needs
    `node_controls.session_registry` to actually reach this session
    from outside, and a caller bypassing `NodeControls` entirely gets
    no watcher, matching every other node-wide-registry-dependent
    feature's existing degrade-gracefully-in-tests behavior.
    """
    await session.write_line(
        f"\r\nWelcome, {sanitize_text(user.username)}! You are level {user.user_level}."
    )
    await _announce_pending_invitations(session, db, user)

    # One InputHistory per connection (design doc round 47/Track 5f),
    # not node-wide like hub/presence/mailbox -- constructed here rather
    # than passed in from netbbs.__main__, so each connected session
    # gets its own recall buffer. Only threaded down into chat's input
    # loop (the actual pain point this was built for); other screens'
    # read_line() calls simply don't pass one and get no recall.
    history = InputHistory()

    presence.enter(user.username)
    watcher_task: asyncio.Task | None = None
    if node_controls is not None:
        node_controls.session_registry.mark_authenticated(session, user.username)
        watcher_task = asyncio.create_task(
            _watch_for_account_revocation(session, db, user, node_controls.session_registry)
        )
    try:
        await _main_menu(session, db, hub, presence, mailbox, history, user, node_controls=node_controls)
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
        await session.write_line(MAINTENANCE_MESSAGE)
        return

    node_controls = NodeControls(
        session_registry=session_registry,
        maintenance=maintenance,
        shutdown_event=shutdown_event if shutdown_event is not None else asyncio.Event(),
        graceful_delay_seconds=graceful_delay_seconds,
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
            session, db, hub, presence, mailbox, result, node_controls=node_controls
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
    way to accept (design doc round 33's "reuse /join" decision,
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


async def _draw_main_menu(session: Session, db: Database, mailbox: MessageMailbox, user: User) -> None:
    """
    Shows any private messages that arrived while away from this menu,
    then the menu itself.

    This is the one place `/msg`'s mailbox-plus-next-prompt delivery
    (design doc round 32, sign-off round 46/Track 5e) actually flushes:
    every screen (boards, files, directory, profile, chat) returns here
    before its next redraw, so a single flush point here covers all of
    them without needing one sprinkled into each individual screen.

    Each flushed `(text, created_at)` pair is formatted through
    `format_with_preference` (design doc -- per-user chat timestamp
    preference round), honoring `user`'s *current* timestamp preference
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
    """
    for text, created_at in mailbox.flush(session):
        await session.write_line(format_with_preference(db, user, text, created_at))

    header = colored("Main menu:", fg_color=HEADER_COLOR, bold=True)
    option_list = [
        menu_key("M", "essage Boards"),
        menu_key("C", "hat"),
        menu_key("F", "ile areas"),
        menu_key("D", "irectory"),
        menu_key("P", "rofile"),
    ]
    if list_pending_invitations_for_user(db, user):
        option_list.append(menu_key("I", "nvitations"))
    if meets_level(user, SYSOP_LEVEL):
        option_list.append(menu_key("A", "dmin"))
    option_list.append(menu_key("L", "ogoff"))
    options = "  ".join(option_list)
    await session.write_line(f"\r\n{header} {options}")
    await session.write("Choice: ")


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
    unrecognized key (design doc round 52): that just sounds a bell and
    leaves the screen exactly as it was, no reprinted prompt, since
    nothing was actually communicated worth a fresh line for.
    """
    await _draw_main_menu(session, db, mailbox, user)
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
        elif choice == "m":
            await session.write_line("")
            await _browse_boards(session, db, user)
            await _draw_main_menu(session, db, mailbox, user)
        elif choice == "c":
            await session.write_line("")
            session_registry = node_controls.session_registry if node_controls is not None else None
            await browse_channels(
                session, db, hub, presence, mailbox, history, user, session_registry=session_registry
            )
            await _draw_main_menu(session, db, mailbox, user)
        elif choice == "f":
            await session.write_line("")
            await browse_file_areas(session, db, user)
            await _draw_main_menu(session, db, mailbox, user)
        elif choice == "d":
            await session.write_line("")
            await _browse_directory(session, db, user)
            await _draw_main_menu(session, db, mailbox, user)
        elif choice == "p":
            await session.write_line("")
            await _edit_profile(session, db, user)
            await _draw_main_menu(session, db, mailbox, user)
        elif choice == "i" and list_pending_invitations_for_user(db, user):
            await session.write_line("")
            await _show_pending_invitations(session, db, user)
            await _draw_main_menu(session, db, mailbox, user)
        elif choice == "a" and meets_level(user, SYSOP_LEVEL):
            await session.write_line("")
            await admin_menu(session, db, user, node_controls=node_controls)
            await _draw_main_menu(session, db, mailbox, user)
        else:
            await session.write(reject_keystroke())


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
    for attempt in range(max_attempts):
        try:
            await session.write("\r\nUsername: ")
            username = (await asyncio.wait_for(session.read_line(), timeout=idle_timeout)).strip()
        except asyncio.TimeoutError:
            return LoginOutcome.IDLE_TIMEOUT
        if not username:
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


async def _browse_boards(session: Session, db: Database, user: User) -> None:
    """Entry point: browse from the top level (no category selected yet)."""
    await _browse_boards_in_category(session, db, user, category_id=None)


async def _browse_boards_in_category(
    session: Session, db: Database, user: User, *, category_id: int | None
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
    """
    all_boards = [b for b in list_boards(db) if meets_level(user, b.min_read_level)]
    boards_here = [b for b in all_boards if b.category_id == category_id]

    categories_here = (
        list_top_level_categories(db) if category_id is None else list_subcategories(db, category_id)
    )

    if not categories_here:
        board = await pick_item(
            session,
            boards_here,
            name_of=lambda b: b.name,
            stable_id_of=lambda b: b.id,
            description_of=lambda b: b.description,
            title="Available message boards",
            empty_message="No message boards are available to you yet.",
        )
        if board is not None:
            await _show_board(session, db, board, user)
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
        title="Available message boards",
        empty_message="No message boards are available to you yet.",
    )
    if selected is None:
        return

    if isinstance(selected, Category):
        await _browse_boards_in_category(session, db, user, category_id=selected.id)
    else:
        await _show_board(session, db, selected, user)


def _can_edit_post(db: Database, post: Post, user: User) -> bool:
    """The post's own original author, no grant needed, or anyone
    holding `BoardPermission.EDIT` -- the exact same authorization
    `netbbs.boards.posts.edit_post` itself enforces, checked here too
    so `[E]dit` only offers itself when it would actually succeed,
    rather than letting a SysOp compose a whole edit only to be
    rejected at the very end."""
    return post.author_user_id == user.id or has_permission(
        db, user, object_type="board", object_id=post.board_id, permission=BoardPermission.EDIT
    )


async def _render_board_page(
    session: Session, db: Database, board_name: str, page: PostPage, user: User, *, can_post: bool
) -> None:
    """Renders one page of posts plus its navigation options — the unit
    that should be redrawn on an actual page change (initial entry,
    Older/Newer/Recent), not on every loop iteration regardless of
    whether anything changed."""
    await _render_post_page(session, db, board_name, page)
    options = []
    if page.has_older:
        options.append(menu_key("O", "lder"))
    if page.has_newer:
        options.append(menu_key("N", "ewer"))
        options.append(menu_key("R", "ecent"))
    if any(_can_edit_post(db, post, user) for post in page.posts):
        options.append(menu_key("E", "dit"))
    if can_post:
        options.append(menu_key("P", "ost"))
    options.append(menu_key("B", "ack"))
    await session.write_line(f"\r\n{'  '.join(options)}")
    await session.write("Choice: ")


async def _show_board(session: Session, db: Database, board: Board, user: User) -> None:
    """
    Show `board`, one bounded page of posts at a time (design doc round
    30, issue #10) — never the whole board, however large its history.

    Opens on the *newest* page, confirmed with Thiesi over keeping the
    old oldest-first default: an active board's most recent activity is
    what's actually useful to see on arrival, not its oldest history —
    directly answers the original complaint that returning to a board
    re-rendered everything, most of which was already read.

    Composing a new post is a first-class `[P]ost` menu option inside
    the browsing loop (GitHub issue #40), not something a `[B]ack`
    choice used to silently fall through into on its way out (GitHub
    issue #39) -- `[B]ack` now always means back, nothing else.
    """
    board_name = sanitize_text(board.name)
    can_post = meets_level(user, board.min_write_level)

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
        await session.write_line(f"Posted (id {post.post_id[:12]}...).")

    page_anchor: tuple[str, tuple[str, str]] | None = None
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

    await _render_board_page(session, db, board_name, page, user, can_post=can_post)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "o" and page.has_older:
            await session.write_line("")
            oldest = page.posts[0]
            page_anchor = ("before", (oldest.created_at, oldest.post_id))
            page = _refetch_current_page()
            await _render_board_page(session, db, board_name, page, user, can_post=can_post)
        elif choice == "n" and page.has_newer:
            await session.write_line("")
            newest = page.posts[-1]
            page_anchor = ("after", (newest.created_at, newest.post_id))
            page = _refetch_current_page()
            await _render_board_page(session, db, board_name, page, user, can_post=can_post)
        elif choice == "r" and page.has_newer:
            await session.write_line("")
            page_anchor = None
            page = _refetch_current_page()
            await _render_board_page(session, db, board_name, page, user, can_post=can_post)
        elif choice == "e" and any(_can_edit_post(db, post, user) for post in page.posts):
            await session.write_line("")
            await _edit_existing_post(session, db, board, page, user)
            page = _refetch_current_page()
            await _render_board_page(session, db, board_name, page, user, can_post=can_post)
        elif choice == "p" and can_post:
            await session.write_line("")
            await _compose_new_post()
            page_anchor = None  # a freshly-created post always lands on the newest page
            page = _refetch_current_page()
            await _render_board_page(session, db, board_name, page, user, can_post=can_post)
        elif choice == "b":
            await session.write_line("")
            return
        else:
            await session.write(reject_keystroke())


async def _edit_existing_post(
    session: Session, db: Database, board: Board, page: PostPage, user: User
) -> None:
    """
    Edit one of the posts currently on screen -- selected by the
    page-relative `[N]` position `_render_post_page` prints next to
    each one, since a board page is at most 5 posts, too small to
    justify pulling in the real picker (`netbbs.net.picker.pick_item`)
    just to choose one (design doc -- prose editor round B2).

    Authorization is checked *before* prompting for any new content
    (`_can_edit_post`, the same rule `edit_post` itself enforces) so a
    SysOp who picks a post they can't actually edit finds out
    immediately, not after composing a whole revision.
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
        edit_post(db, post, board, subject=subject, body=body, edited_by=user)
    except PostError as exc:
        await session.write_line(colored(f"Could not save edit: {exc}", fg_color=MUTED_COLOR))
        return
    await session.write_line("Post updated.")


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


async def _render_post_page(session: Session, db: Database, board_name: str, page: PostPage) -> None:
    header = colored(f"[{board_name}]", fg_color=HEADER_COLOR, bold=True)
    await session.write_line(f"\r\n{header}")
    for position, post in enumerate(page.posts, start=1):
        when = format_for_display(post.created_at, db)
        edited_marker = " (edited)" if post.is_edited else ""
        # Position numbers are 1-indexed *within this page only* -- not
        # a stable identity across page changes, purely a same-screen
        # selector for [E]dit (design doc -- prose editor round B2:
        # editing an existing post), the same "how do you pick one item
        # currently on screen" role a picker's page-relative numbering
        # already plays elsewhere, just inline here since a board page
        # is at most 5 posts, too small to need a real picker for it.
        post_header = colored(
            f"[{position}] {sanitize_text(post.subject)} -- "
            f"{sanitize_text(post.author_label)} ({when}){edited_marker}",
            fg_color=ACCENT_COLOR,
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


# -- user directory & vCard/finger (design doc §13, sign-off round 38) ------


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
        [menu_key("E", "dit bio"), menu_key("V", "isibility"), menu_key("F", "ullscreen editor"), menu_key("B", "ack")]
    )
    await session.write_line(f"\r\n{options}")
    await session.write("Choice: ")
    return visible


async def _edit_profile(session: Session, db: Database, user: User) -> None:
    """
    Edit your own vCard: the bio, its visibility toggle, and the
    fullscreen-editor composition preference (design doc -- prose
    editor round B2). Shows the current state first, then a small
    sub-menu — matches this codebase's existing "show state, then
    offer actions" shape (e.g. `netbbs.net.file_flow._show_area`)
    rather than jumping straight into an edit prompt.

    Redraws the (possibly just-updated) state only after an edit or
    toggle actually happens, not on every loop iteration — mirrors
    `_show_board`'s `_render_board_page` split. An unrecognized key
    sounds a bell and leaves the screen exactly as it was, no reprinted
    prompt (design doc round 52), same as the main menu and the picker.
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
