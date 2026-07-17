"""
`python -m netbbs.admin [--db PATH] [--as USERNAME]` -- the standalone
local SysOp admin CLI tool (design doc -- SysOp foundation round).

Shares `netbbs.net.admin_flow.admin_menu` with the in-BBS [A]dmin menu
option (`netbbs.net.login_flow`) rather than duplicating any command
logic -- the only thing genuinely new here is *how* a `Session` and an
acting `User` get constructed for a bare local terminal instead of a
network connection.

No credential-based authentication happens here: local shell/
filesystem access to the database file is already the real trust
boundary (whoever can run this tool on the server already has direct
access to the same SQLite file), and a password prompt would
permanently lock out a pubkey-only SysOp who has no local way to prove
key possession without a network transport (SSH's own handshake
already does that proof; there's no local equivalent -- see
`netbbs.auth.users.authorize_public_key`'s docstring). Instead,
`_resolve_actor` below only figures out *which* SysOp to attribute
actions to, for the audit log.

Opens its own `Database` handle on the same file the running node
uses, if any -- an already-supported, designed-for scenario (WAL mode
+ busy_timeout specifically so a second process can do this
concurrently, see `netbbs.storage.database.Database`'s own docstring).

`run_admin_session` opens its own `DatabaseLane` around the `Database`
handle it's given (design doc round 91/issue #57, round 115) -- the
shared `admin_menu` now takes `lane`, not `db`, and this is the
process's only other caller of it besides the in-BBS `[A]dmin` menu
option. Scoped to this function (opened and closed here, not owned by
`main()`) so tests that call `run_admin_session` directly still only
need to hand it a plain `db`, matching this module's own stated reason
for keeping this function separate from `main()`.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import nacl.signing

from netbbs.auth.users import SYSOP_LEVEL, User, create_user, list_users
from netbbs.identity.keys import IdentityError, parse_verify_key
from netbbs.moderation.log import record_action
from netbbs.net.admin_flow import admin_menu
from netbbs.net.local_cli import LocalCLISession
from netbbs.net.local_terminal import raw_terminal
from netbbs.net.picker import pick_item
from netbbs.net.session import Session
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

_DEFAULT_DB_PATH = Path("netbbs.db")


async def run_admin_session(session: Session, db: Database, as_username: str | None) -> None:
    """Resolve which SysOp this session acts as (bootstrapping the
    first one if none exist yet), then hand off to the shared admin
    menu. Kept separate from `main()` so tests can drive it directly
    with a scripted `Session` and a real `tmp_path` `Database`,
    mirroring how `netbbs.__main__.run()` is already tested."""
    lane = DatabaseLane(db.path)
    try:
        actor = await _resolve_actor(session, lane, as_username)
        await session.write_line(f"Attributed to {actor.username!r} for this session's audit log.")
        await admin_menu(session, lane, actor)
    finally:
        lane.close()


async def _resolve_actor(session: Session, lane: DatabaseLane, as_username: str | None) -> User:
    """Only *active* SysOps are eligible -- a disabled account can't
    log in over the network either, so it shouldn't be selectable to
    act as here (same "active" definition `count_sysops` already
    uses)."""
    sysops = [u for u in await lane.run(list_users) if u.user_level >= SYSOP_LEVEL and u.disabled_at is None]

    if not sysops:
        return await _bootstrap_first_sysop(session, lane)

    if as_username is not None:
        match = next((u for u in sysops if u.username == as_username), None)
        if match is None:
            raise SystemExit(f"--as {as_username!r} is not an active SysOp-level account")
        return match

    if len(sysops) == 1:
        return sysops[0]

    selected = await pick_item(
        session, sysops,
        name_of=lambda u: u.username,
        stable_id_of=lambda u: u.id,
        title="Attribute this session to which SysOp?",
        empty_message="No SysOp accounts.",
    )
    if selected is None:
        raise SystemExit("no SysOp selected -- exiting")
    return selected


async def _bootstrap_first_sysop(session: Session, lane: DatabaseLane) -> User:
    """No SysOp account exists yet on this node -- create the first
    one. Skips `_resolve_actor`'s normal --as/auto-select/picker logic
    entirely, since there's nothing yet to pick from."""
    await session.write_line("No SysOp account exists yet on this node. Let's create the first one.\r\n")
    await session.write("Username: ")
    username = (await session.read_line()).strip()
    while not username:
        await session.write("Username cannot be blank. Username: ")
        username = (await session.read_line()).strip()

    password: str | None = None
    verify_key: nacl.signing.VerifyKey | None = None
    while password is None and verify_key is None:
        password = await _prompt_password(session)
        verify_key = await _prompt_pubkey(session)
        if password is None and verify_key is None:
            await session.write_line("An account needs a password, a public key, or both. Try again.\r\n")

    # round 115: create_user (not create_user_async), same reasoning as
    # netbbs.net.admin_flow._create_user_screen -- lane.run() already
    # dispatches this whole call to a worker thread.
    def _create(db: Database) -> User:
        user = create_user(db, username, password=password, verify_key=verify_key, user_level=SYSOP_LEVEL)
        # Chicken-and-egg: no actor exists yet to attribute this to, so
        # the audit entry self-attributes to the account it just created.
        record_action(
            db, actor=user, action="bootstrap_create_sysop", target_user_id=user.id,
            detail="first SysOp account on this node; created with no prior SysOp to attribute the action to",
        )
        return user

    user = await lane.run(_create)
    await session.write_line(f"\r\nCreated SysOp account {user.username!r}.\r\n")
    return user


async def _prompt_password(session: Session) -> str | None:
    await session.write("Password (leave blank to skip): ")
    first = await session.read_line(echo=False)
    if not first:
        return None
    await session.write("Confirm password: ")
    second = await session.read_line(echo=False)
    if first != second:
        await session.write_line("Passwords did not match -- try again.\r\n")
        return None
    return first


async def _prompt_pubkey(session: Session) -> nacl.signing.VerifyKey | None:
    await session.write("Public key, base64 or ssh-ed25519 line (leave blank to skip): ")
    text = (await session.read_line()).strip()
    if not text:
        return None
    try:
        return parse_verify_key(text)
    except IdentityError as exc:
        await session.write_line(f"Could not parse key: {exc} -- try again.\r\n")
        return None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m netbbs.admin", description="Local SysOp administration tool."
    )
    parser.add_argument(
        "--db", type=Path, default=_DEFAULT_DB_PATH,
        help=f"path to the node's database file (default: {_DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--as", dest="as_username", default=None,
        help="attribute this session's actions to this SysOp account (skips the picker)",
    )
    args = parser.parse_args(argv)

    try:
        db = Database(args.db)
    except Exception as exc:
        # A clear, actionable message instead of a raw sqlite3.Error/
        # RuntimeError traceback -- the concrete failure this closes:
        # pointing --db at a database file that doesn't match this
        # build (e.g. one a newer or older version last migrated).
        raise SystemExit(
            f"could not open the database at {args.db}: {exc} -- this usually means "
            "the database file doesn't match this build of NetBBS (e.g. it was last migrated "
            "by a newer or older version). If you're testing multiple NetBBS versions side by "
            "side, make sure each one is paired with its own separate database file."
        ) from exc

    try:
        with raw_terminal():
            asyncio.run(run_admin_session(LocalCLISession(), db, args.as_username))
    finally:
        db.close()


if __name__ == "__main__":
    main()
