"""
Login flow and top-level main menu, tying a Session to the auth,
permissions, boards, chat, and rendering modules.

The main menu itself is intentionally minimal structurally — a plain
lettered loop, not a real menu-dispatch architecture. It exists now,
rather than staying purely linear the way the board-only version of this
file was, because there are genuinely two independent things to route
between (boards, chat) — adding real menu structure now that it's
actually needed is not the same as building it prematurely. Output now
uses the "ANSI half" of the hybrid rendering framework (color, and
reflow to each session's actual detected terminal width) — the "TUI
half" (character-mode input, screen-buffer diffing) remains deferred
until a real heavy screen (the fullscreen editor, a future file browser)
needs it.
"""

from __future__ import annotations

from enum import Enum, auto

from netbbs.auth.users import AuthError, User, authenticate_password
from netbbs.boards import Board, create_post, list_boards, list_posts
from netbbs.boards.categories import Category, list_subcategories, list_top_level_categories
from netbbs.chat import ChatHub
from netbbs.moderation import is_blocked
from netbbs.net.chat_flow import browse_channels
from netbbs.net.file_flow import browse_file_areas
from netbbs.net.picker import pick_item
from netbbs.net.session import Session
from netbbs.permissions import meets_level
from netbbs.rendering import ACCENT_COLOR, HEADER_COLOR, colored, menu_key, reflow
from netbbs.storage.database import Database
from netbbs.timeutil import format_for_display

WELCOME_BANNER = colored(
    "================================================\r\n"
    "  Welcome to NetBBS\r\n"
    "  NetBBS Link -- coming soon\r\n"
    "================================================",
    fg_color=HEADER_COLOR,
    bold=True,
)

# Arbitrary placeholder threshold demonstrating that level-gating works
# end-to-end over a real connection. Not a real SysOp-level constant yet
# — that belongs with the actual permission model once boards/moderators
# exist in Phase 2.
_DEMO_ELEVATED_LEVEL = 100

_MAX_LOGIN_ATTEMPTS = 3


class LoginOutcome(Enum):
    """Terminal outcomes from the interactive login flow."""

    ATTEMPTS_EXHAUSTED = auto()
    BLOCKED = auto()


async def handle_session(session: Session, db: Database, hub: ChatHub) -> None:
    await session.write_line(WELCOME_BANNER)

    login_result = await _login(session, db)
    if login_result is LoginOutcome.ATTEMPTS_EXHAUSTED:
        await session.write_line("Too many failed attempts. Goodbye.")
        return
    if login_result is LoginOutcome.BLOCKED:
        return

    user = login_result
    await session.write_line(f"\r\nWelcome, {user.username}! You are level {user.user_level}.")

    if meets_level(user, _DEMO_ELEVATED_LEVEL):
        await session.write_line("(You have elevated access.)")

    await _main_menu(session, db, hub, user)

    await session.write_line("\r\nGoodbye!")


async def _main_menu(session: Session, db: Database, hub: ChatHub, user: User) -> None:
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
    """
    while True:
        header = colored("Main menu:", fg_color=HEADER_COLOR, bold=True)
        options = "  ".join(
            [
                menu_key("B", "oards"),
                menu_key("C", "hat"),
                menu_key("F", "ile areas"),
                menu_key("Q", "uit"),
            ]
        )
        await session.write_line(f"\r\n{header} {options}")
        await session.write("Choice: ")
        choice = (await session.read_key()).lower()
        await session.write_line("")  # move to a fresh line after the single-key echo

        if choice == "q":
            return
        elif choice == "b":
            await _browse_boards(session, db, user)
        elif choice == "c":
            await browse_channels(session, db, hub, user)
        elif choice == "f":
            await browse_file_areas(session, db, user)
        else:
            await session.write_line("Unknown choice.")


async def _login(
    session: Session, db: Database, max_attempts: int = _MAX_LOGIN_ATTEMPTS
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

    Per-connection attempt limiting only, nothing persistent yet — a real
    lockout/ban mechanism belongs to §13's mute/ban system, which is
    Phase 2. This is just enough to stop a single connection from
    hammering the password check in a tight loop.

    The blocklist check happens *here*, after successful authentication,
    not inside `authenticate_password` itself — authentication ("are
    these credentials correct") and this kind of authorization ("is this
    correctly-authenticated account allowed to proceed") are different
    concerns, kept separate the same way `netbbs.permissions` is kept
    separate from `netbbs.auth`. It also can't happen any earlier: we
    need to know *who* successfully authenticated before we can check
    whether they're blocked.
    """
    for attempt in range(max_attempts):
        await session.write("\r\nUsername: ")
        username = (await session.read_line()).strip()
        if not username:
            continue

        await session.write("Password: ")
        password = await session.read_line(echo=False)
        # No explicit blank-line write needed here anymore — read_line()
        # now writes its own trailing CRLF after Enter unconditionally
        # (part of character-mode input; see netbbs.net.telnet), whereas
        # the original line-mode implementation relied on the client's
        # own local echo to show that newline and needed this line to
        # compensate. Leaving it in would now print an extra blank line.

        try:
            user = authenticate_password(db, username, password)
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
            title="Available boards",
            empty_message="No boards are available to you yet.",
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
        title="Available boards",
        empty_message="No boards are available to you yet.",
    )
    if selected is None:
        return

    if isinstance(selected, Category):
        await _browse_boards_in_category(session, db, user, category_id=selected.id)
    else:
        await _show_board(session, db, selected, user)


async def _show_board(session: Session, db: Database, board: Board, user: User) -> None:
    posts = list_posts(db, board, user)
    if not posts:
        await session.write_line(f"\r\n[{board.name}] has no posts yet.")
    else:
        header = colored(f"[{board.name}]", fg_color=HEADER_COLOR, bold=True)
        await session.write_line(f"\r\n{header}")
        for post in posts:
            when = format_for_display(post.created_at, db)
            post_header = colored(
                f"{post.subject} -- {post.author_label} ({when})", fg_color=ACCENT_COLOR
            )
            await session.write_line(f"\r\n{post_header}")
            # Reflowed to this specific session's actual detected width
            # (NAWS-negotiated, or the 80-column default — see
            # netbbs.net.session.Session.terminal_width), not a fixed
            # assumption, per the design doc's "must degrade gracefully
            # above 40x24 minimum" requirement.
            await session.write_line(reflow(post.body, width=session.terminal_width))

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