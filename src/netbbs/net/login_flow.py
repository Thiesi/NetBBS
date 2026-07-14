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

from netbbs.auth.users import SYSOP_LEVEL, AuthError, User, authenticate_password_async, list_users
from netbbs.boards import Board, PostPage, create_post, list_boards, list_posts_page
from netbbs.boards.categories import Category, list_subcategories, list_top_level_categories
from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry, format_with_preference
from netbbs.directory import (
    MAX_BIO_LINES,
    BioError,
    get_bio,
    get_vcard,
    is_bio_visible,
    set_bio,
    set_bio_visible,
)
from netbbs.moderation import is_blocked
from netbbs.net.admin_flow import admin_menu
from netbbs.net.char_input import InputHistory
from netbbs.net.chat_flow import browse_channels
from netbbs.net.file_flow import browse_file_areas
from netbbs.net.maintenance import MAINTENANCE_MESSAGE, MaintenanceMode
from netbbs.net.nodeconfig import ThrottleConfig
from netbbs.net.picker import pick_item
from netbbs.net.session import Session
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.net.shutdown import NodeControls
from netbbs.net.throttle import LoginThrottle
from netbbs.net.welcome_banner import load_welcome_banner
from netbbs.permissions import meets_level
from netbbs.rendering import ACCENT_COLOR, HEADER_COLOR, MUTED_COLOR, colored, menu_key, reflow, sanitize_text
from netbbs.storage.database import Database
from netbbs.timeutil import format_for_display

_MAX_LOGIN_ATTEMPTS = 3


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
    """The login-through-logoff body of a connection, wrapped by
    `handle_session`'s maintenance-mode check and session-registry
    bookkeeping (design doc round 51) — split out so those two concerns
    stay a thin, easy-to-read wrapper rather than adding another level
    of nesting to the whole function.

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

    user = login_result
    await session.write_line(
        f"\r\nWelcome, {sanitize_text(user.username)}! You are level {user.user_level}."
    )

    # One InputHistory per connection (design doc round 47/Track 5f),
    # not node-wide like hub/presence/mailbox -- constructed here rather
    # than passed in from netbbs.__main__, so each connected session
    # gets its own recall buffer. Only threaded down into chat's input
    # loop (the actual pain point this was built for); other screens'
    # read_line() calls simply don't pass one and get no recall.
    history = InputHistory()

    presence.enter(user.username)
    if node_controls is not None:
        node_controls.session_registry.mark_authenticated(session, user.username)
    try:
        await _main_menu(session, db, hub, presence, mailbox, history, user, node_controls=node_controls)
    finally:
        presence.leave(user.username)

    await session.write_line("\r\nGoodbye!")


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
    """
    for text, created_at in mailbox.flush(user.username):
        await session.write_line(format_with_preference(db, user, text, created_at))

    header = colored("Main menu:", fg_color=HEADER_COLOR, bold=True)
    option_list = [
        menu_key("M", "essage Boards"),
        menu_key("C", "hat"),
        menu_key("F", "ile areas"),
        menu_key("D", "irectory"),
        menu_key("P", "rofile"),
    ]
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

        if choice == "l":
            await session.write_line("")
            return
        elif choice == "m":
            await session.write_line("")
            await _browse_boards(session, db, user)
            await _draw_main_menu(session, db, mailbox, user)
        elif choice == "c":
            await session.write_line("")
            await browse_channels(session, db, hub, presence, mailbox, history, user)
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
        elif choice == "a" and meets_level(user, SYSOP_LEVEL):
            await session.write_line("")
            await admin_menu(session, db, user, node_controls=node_controls)
            await _draw_main_menu(session, db, mailbox, user)
        else:
            await session.write("\a")


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


async def _render_board_page(session: Session, db: Database, board_name: str, page: PostPage) -> None:
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
    """
    board_name = sanitize_text(board.name)
    page = list_posts_page(db, board, user)
    if not page.posts:
        await session.write_line(f"\r\n[{board_name}] has no posts yet.")
    else:
        await _render_board_page(session, db, board_name, page)
        while True:
            choice = (await session.read_key()).lower()

            if choice == "o" and page.has_older:
                await session.write_line("")
                oldest = page.posts[0]
                page = list_posts_page(db, board, user, before=(oldest.created_at, oldest.post_id))
                await _render_board_page(session, db, board_name, page)
            elif choice == "n" and page.has_newer:
                await session.write_line("")
                newest = page.posts[-1]
                page = list_posts_page(db, board, user, after=(newest.created_at, newest.post_id))
                await _render_board_page(session, db, board_name, page)
            elif choice == "r" and page.has_newer:
                await session.write_line("")
                page = list_posts_page(db, board, user)
                await _render_board_page(session, db, board_name, page)
            elif choice == "b":
                await session.write_line("")
                break
            else:
                await session.write("\a")

    if not meets_level(user, board.min_write_level):
        return

    await session.write("\r\nPost a new message? Subject (or press Enter to skip): ")
    subject = (await session.read_line()).strip()
    if not subject:
        return

    await session.write("Body: ")
    body = (await session.read_line()).strip()
    post = create_post(db, board, user, subject, body)
    await session.write_line(f"Posted (id {post.post_id[:12]}...).")


async def _render_post_page(session: Session, db: Database, board_name: str, page: PostPage) -> None:
    header = colored(f"[{board_name}]", fg_color=HEADER_COLOR, bold=True)
    await session.write_line(f"\r\n{header}")
    for post in page.posts:
        when = format_for_display(post.created_at, db)
        post_header = colored(
            f"{sanitize_text(post.subject)} -- {sanitize_text(post.author_label)} ({when})",
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

    await session.write_line(colored("\r\nYour profile:", fg_color=HEADER_COLOR, bold=True))
    if current_bio:
        await session.write_line(
            reflow(sanitize_text(current_bio, allow_newlines=True), width=session.terminal_width)
        )
    else:
        await session.write_line(colored("(no bio set)", fg_color=MUTED_COLOR))
    await session.write_line(f"Visibility: {'public' if visible else 'private'}")

    options = "  ".join([menu_key("E", "dit bio"), menu_key("V", "isibility"), menu_key("B", "ack")])
    await session.write_line(f"\r\n{options}")
    await session.write("Choice: ")
    return visible


async def _edit_profile(session: Session, db: Database, user: User) -> None:
    """
    Edit your own vCard: the bio and its visibility toggle. Shows the
    current state first, then a small sub-menu — matches this
    codebase's existing "show state, then offer actions" shape (e.g.
    `netbbs.net.file_flow._show_area`) rather than jumping straight
    into an edit prompt.

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
        else:
            await session.write("\a")


async def _edit_bio(session: Session, db: Database, user: User) -> None:
    """
    Collects up to `MAX_BIO_LINES` lines via repeated `read_line`
    calls, ending early on a blank line — there is no multi-line/
    cursor-addressable text entry anywhere in this codebase yet (the
    fullscreen editor is still-unbuilt Phase 2 Track 6), so this is
    the same repeated-single-line-read shape every other multi-step
    prompt here already uses. A blank first line clears the bio
    entirely, rather than leaving it unchanged — choosing not to edit
    at all is what the profile screen's own [B]ack option is for.
    """
    await session.write_line(f"\r\nEnter your bio, up to {MAX_BIO_LINES} lines. Blank line to finish.")
    lines: list[str] = []
    for _ in range(MAX_BIO_LINES):
        line = (await session.read_line()).strip()
        if not line:
            break
        lines.append(line)

    try:
        set_bio(db, user, "\n".join(lines))
    except BioError as exc:
        await session.write_line(colored(f"Could not save bio: {exc}", fg_color=MUTED_COLOR))
        return
    await session.write_line("Bio updated.")


async def _toggle_bio_visibility(
    session: Session, db: Database, user: User, *, currently_visible: bool
) -> None:
    new_value = not currently_visible
    set_bio_visible(db, user, new_value)
    await session.write_line(f"Bio is now {'public' if new_value else 'private'}.")
