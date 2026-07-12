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
from netbbs.net.session import Session
from netbbs.permissions import meets_level
from netbbs.storage.database import Database

WELCOME_BANNER = """\
================================================
  Welcome to NetBBS
  "the Link" -- coming soon
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

    await session.write_line(
        "\r\nThere's nothing else here yet -- boards, chat, and file areas "
        "are still being built."
    )
    await session.write_line("Goodbye!")


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
