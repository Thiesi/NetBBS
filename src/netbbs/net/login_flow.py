"""
Minimal login flow, tying a Session to the auth and permissions modules.

This is deliberately not a real menu system yet — boards, chat, and file
areas don't exist as Phase 1 features to route to. It exists to prove the
whole path end-to-end (connect -> authenticate -> permission-gated
action) actually works together, which is exactly the kind of
integration check worth having at this point rather than only unit tests
per module (see the earlier discussion on iterative-vs-end testing).
"""

from __future__ import annotations

from netbbs.auth.users import AuthError, User, authenticate_password
from netbbs.boards import Board, BoardError, create_post, get_board_by_name, list_boards, list_posts
from netbbs.net.session import Session
from netbbs.permissions import meets_level
from netbbs.storage.database import Database
from netbbs.timeutil import format_for_display

WELCOME_BANNER = """\
================================================
  Welcome to NetBBS
  NetBBS Link -- coming soon
================================================"""

# Arbitrary placeholder threshold demonstrating that level-gating works
# end-to-end over a real connection. Not a real SysOp-level constant yet
# — that belongs with the actual permission model once boards/moderators
# exist in Phase 2.
_DEMO_ELEVATED_LEVEL = 100

_MAX_LOGIN_ATTEMPTS = 3


async def handle_session(session: Session, db: Database) -> None:
    await session.write_line(WELCOME_BANNER)

    user = await _login(session, db)
    if user is None:
        await session.write_line("Too many failed attempts. Goodbye.")
        return

    await session.write_line(f"\r\nWelcome, {user.username}! You are level {user.user_level}.")

    if meets_level(user, _DEMO_ELEVATED_LEVEL):
        await session.write_line("(You have elevated access.)")

    await _browse_boards(session, db, user)

    await session.write_line("\r\nGoodbye!")


async def _login(session: Session, db: Database, max_attempts: int = _MAX_LOGIN_ATTEMPTS) -> User | None:
    """
    Prompt for username/password up to `max_attempts` times.

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
    """
    for attempt in range(max_attempts):
        await session.write("\r\nUsername: ")
        username = (await session.read_line()).strip()
        if not username:
            continue

        await session.write("Password: ")
        password = await session.read_line(echo=False)
        await session.write_line("")  # move to a fresh line after the hidden input

        try:
            return authenticate_password(db, username, password)
        except AuthError:
            remaining = max_attempts - attempt - 1
            if remaining > 0:
                await session.write_line(f"Login failed. {remaining} attempt(s) remaining.")
            else:
                await session.write_line("Login failed.")

    return None


async def _browse_boards(session: Session, db: Database, user: User) -> None:
    """
    Minimal linear board-browsing flow: list boards the user can read,
    let them pick one, show its posts, offer to post if they have write
    access.

    Deliberately not a real menu system — there's no menu/command
    dispatch architecture designed yet, and inventing one as a side
    effect of testing boards would be scope creep. This exists purely to
    prove boards work end-to-end over a real connection, the same way
    `_login` proves auth does, per the project's iterative-testing
    approach.
    """
    readable_boards = [b for b in list_boards(db) if meets_level(user, b.min_read_level)]
    if not readable_boards:
        await session.write_line("\r\nNo boards are available to you yet.")
        return

    await session.write_line("\r\nAvailable boards:")
    for board in readable_boards:
        await session.write_line(f"  {board.name} - {board.description or ''}")

    await session.write("\r\nEnter a board name to view (or press Enter to skip): ")
    choice = (await session.read_line()).strip()
    if not choice:
        return

    try:
        board = get_board_by_name(db, choice)
    except BoardError:
        await session.write_line("No such board.")
        return

    if not meets_level(user, board.min_read_level):
        await session.write_line("You don't have access to that board.")
        return

    await _show_board(session, db, board, user)


async def _show_board(session: Session, db: Database, board: Board, user: User) -> None:
    posts = list_posts(db, board, user)
    if not posts:
        await session.write_line(f"\r\n[{board.name}] has no posts yet.")
    else:
        await session.write_line(f"\r\n[{board.name}]")
        for post in posts:
            when = format_for_display(post.created_at, db)
            await session.write_line(f"\r\n{post.subject} -- {post.author_label} ({when})")
            await session.write_line(post.body)

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
