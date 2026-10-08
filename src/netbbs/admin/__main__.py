"""
`python -m netbbs.admin [--db PATH] [--as USERNAME]` -- the standalone
local SysOp admin CLI tool (design doc).

Shares `netbbs.net.admin_flow.admin_menu` with the in-BBS [S]ysOp menu
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
handle it's given (design doc/issue #57) -- the
shared `admin_menu` now takes `lane`, not `db`, and this is the
process's only other caller of it besides the in-BBS `[S]ysOp` menu
option. Scoped to this function (opened and closed here, not owned by
`main()`) so tests that call `run_admin_session` directly still only
need to hand it a plain `db`, matching this module's own stated reason
for keeping this function separate from `main()`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import nacl.signing

from netbbs.net.redraw_preference import start_new_account_redrawing_in_place
from netbbs.backup import running_node_pid
from netbbs.link.key_rotation import KeyRotationError, rotate_offline
from netbbs.net.confirm import prompt_yes_no
from netbbs.auth.users import (
    SYSOP_LEVEL,
    AuthError,
    User,
    UserManagementError,
    create_user,
    get_user_by_username,
    hash_password_off_loop,
    list_users,
    set_password_hash,
)
from netbbs.identity.keys import IdentityError, parse_verify_key
from netbbs.moderation.log import record_action
from netbbs.net.admin_flow import admin_menu
from netbbs.net.managed_dns_flow import offer_deferred_registration
from netbbs.net.onboarding_flow import offer_onboarding
from netbbs.net.local_cli import LocalCLISession
from netbbs.net.local_terminal import raw_terminal
from netbbs.net.node_theme import effective_accent_color_256
from netbbs.net.picker import pick_item
from netbbs.net.char_input import reject_unhandled_key
from netbbs.net.session import Session, write_prompt
from netbbs.rendering import action_bar, menu_key
from netbbs.rendering.reflow import print_wrapped, terminal_wrapped
from netbbs.admin.levels_report import LevelsReportError, render_json, run_levels_report
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

_DEFAULT_DB_PATH = Path("netbbs.db")
# The same default `netbbs.net.nodeconfig.NodeConfig.identity_dir` and
# `python -m netbbs.backup` use.
_DEFAULT_IDENTITY_DIR = Path("netbbs_identity")


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
        # Issue #634: a managed-DNS opt-in accepted at bootstrap cannot
        # register until the node has started once. The deployment that
        # bootstrap anchor exists for never signs in over the network, so
        # running this tool again is where it picks the name. A no-op in
        # the bootstrap session itself (the node still has not started)
        # and on every node that owes no such registration.
        await offer_deferred_registration(session, lane)
        await admin_menu(session, lane, actor)
    finally:
        lane.close()


async def run_reset_password(session: Session, db: Database, as_username: str | None, username: str) -> int:
    """
    `python -m netbbs.admin reset-password USERNAME` (issue #611): set a
    new password on `username` from the local shell, without opening the
    admin menu. Returns the process exit status.

    This is the locked-out case -- a SysOp who cannot sign in to reach
    the user detail screen's own `[P]assword` action, most often because
    it is their own account. No current password is asked for and none
    is needed: exactly as for the rest of this tool, local filesystem
    access to the database is the trust boundary (module docstring),
    and the acting SysOp is resolved only so the audit row names
    someone. A SysOp resetting their own password therefore gets a
    self-attributed audit entry, which is the honest record of what
    happened.

    Blank input cancels; a mismatched confirmation cancels; the failure
    `set_password` raises (a blank password) is printed rather than
    traced.
    """
    lane = DatabaseLane(db.path)
    try:
        actor = await _resolve_actor(session, lane, as_username)
        try:
            target = await lane.run(get_user_by_username, username)
        except AuthError:
            await session.write_line(f"No account named {username!r} exists on this node.")
            return 1
        await session.write_line(f"Setting a new password for {target.username!r} (attributed to {actor.username!r}).")
        await write_prompt(session, "New password (blank to cancel): ")
        first = await session.read_line(echo=False)
        if not first:
            await session.write_line("Cancelled -- nothing changed.")
            return 1
        await write_prompt(session, "Confirm new password: ")
        second = await session.read_line(echo=False)
        if first != second:
            await session.write_line("The two entries did not match -- nothing changed.")
            return 1
        # Off-loop hash then a short lane transaction -- the same split
        # the in-BBS screen uses, so there is one shape rather than two.
        new_hash = await hash_password_off_loop(first)
        try:
            await lane.run(set_password_hash, target, new_hash, changed_by=actor)
        except (AuthError, UserManagementError) as exc:
            await session.write_line(str(exc))
            return 1
        await session.write_line(f"Password set for {target.username!r}. It applies to their next sign-in.")
        return 0
    finally:
        lane.close()


async def run_rotate_key(
    session: Session, db: Database, as_username: str | None, *,
    purpose: str, compromised: bool, identity_dir: Path,
) -> int:
    """
    `python -m netbbs.admin rotate-key {signing,transport}` (issue #624):
    replace one of this node's operational keys while the node is stopped.
    Returns the process exit status.

    The running node's console does the same from Link status -> `[K]eys`.
    This is for the SysOp who does not want the node up while responding to
    a leak, or cannot reach its console. It refuses while a node process
    holds the database, since that node would go on signing with -- and
    advertising -- the key replaced here, and the next save of its identity
    would say nothing about it.

    `--compromised` marks the old key compromised: peers stop believing
    anything it signed, and this node's own boards, posts, files and mail
    are signed again here, before the node next starts. Without it the old
    key is retired and what it signed stays valid.
    """
    lane = DatabaseLane(db.path)
    try:
        pid = running_node_pid(db.path)
        if pid is not None:
            await session.write_line(
                f"A node process (PID {pid}) is running on {db.path}. Stop it first, or rotate "
                "from its console: Link status, then [K]eys."
            )
            return 1
        actor = await _resolve_actor(session, lane, as_username)
        if compromised:
            question = (
                f"Replace the {purpose} key in {identity_dir} and tell every peer the old one is "
                "compromised? Peers refuse anything it signed that they have not already "
                "accepted; copies they already hold stay."
            )
        else:
            question = f"Retire the {purpose} key in {identity_dir} and replace it?"
        if not await prompt_yes_no(session, question, default=False):
            await session.write_line("Cancelled -- nothing changed.")
            return 1
        try:
            outcome = await lane.run(
                rotate_offline, identity_dir, purpose=purpose, compromised=compromised, actor=actor,
            )
        except (KeyRotationError, OSError) as exc:
            await session.write_line(f"The {purpose} key was not rotated: {exc}")
            return 1
        await session.write_line(f"The {purpose} key is now {outcome.new_key_fingerprint}.")
        if outcome.resigned:
            await session.write_line(f"Re-signed {outcome.resigned} object(s) under it.")
        await session.write_line("Peers learn the new key when the node next syncs.")
        return 0
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
            raise SystemExit(
                terminal_wrapped(
                    f"--as {as_username!r} is not an active SysOp-level account",
                    stream=sys.stderr,
                )
            )
        return match

    if len(sysops) == 1:
        return sysops[0]

    selected = await pick_item(
        session, sysops,
        name_of=lambda u: u.username,
        stable_id_of=lambda u: u.id,
        title="Attribute this session to which SysOp?",
        empty_message="No SysOp accounts.",
        accent_color=await lane.run(effective_accent_color_256),
    )
    if selected is None:
        raise SystemExit(terminal_wrapped("no SysOp selected -- exiting", stream=sys.stderr))
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
        # Issue #282: choose the credential kind up front instead of being
        # asked for a public key right after a password was accepted
        # ([T]wo is still one keystroke away). Not `B`: `B` is Back on
        # every screen and Esc reads as it (issue #1158); this once-only
        # question has nothing to go back to, so both are refused.
        await session.write_line(
            action_bar(
                [menu_key("P", "assword"), menu_key("K", "ey (ssh-ed25519)"), menu_key("T", "wo (password and key)")],
                width=session.terminal_width,
            )
        )
        await write_prompt(session, "Sign in with: ")
        choice = (await session.read_key()).lower()
        if choice not in ("p", "k", "t"):
            await session.write(reject_unhandled_key(choice))
            continue
        await session.write_line("")
        if choice in ("p", "t"):
            password = await _prompt_password(session)
        if choice in ("k", "t"):
            verify_key = await _prompt_pubkey(session)
        if choice == "t" and (password is None or verify_key is None):
            # [T]wo was an explicit choice: one accepted credential is
            # not enough to create the account with (Codex review on
            # #292) -- start the choice over rather than silently
            # settling for half.
            await session.write_line("Both were selected, but only one was accepted. Try again.")
            password = None
            verify_key = None
            continue
        if password is None and verify_key is None:
            await session.write_line("An account needs a password, a public key, or both. Try again.\r\n")

    # create_user (not create_user_async), same reasoning as
    # netbbs.net.admin_flow._create_user_screen -- lane.run() already
    # dispatches this whole call to a worker thread.
    def _create(db: Database) -> User:
        user = create_user(db, username, password=password, verify_key=verify_key, user_level=SYSOP_LEVEL)
        # As a signed-up account starts (issue #840).
        start_new_account_redrawing_in_place(db, user)
        # Chicken-and-egg: no actor exists yet to attribute this to, so
        # the audit entry self-attributes to the account it just created.
        record_action(
            db, actor=user, action="bootstrap_create_sysop", target_user_id=user.id,
            detail="first SysOp account on this node; created with no prior SysOp to attribute the action to",
        )
        return user

    user = await lane.run(_create)
    await session.write_line(f"\r\nCreated SysOp account {user.username!r}.\r\n")
    # Design doc §16 (issues #219 Decision 7 and #201 Decision 1): the
    # first-run screen -- reliable-node participation and the managed
    # subdomain, one screen, two independent choices -- anchored here
    # (the earliest interactive surface a fresh node has) rather than a
    # literal "first daemon run" prompt -- a supported persistent
    # deployment bootstraps its first SysOp non-interactively via
    # netbbs.admin and then runs headlessly under systemd/rc.d, with no
    # interactive channel left by the time the daemon itself starts.
    await offer_onboarding(session, lane)
    return user


async def _prompt_password(session: Session) -> str | None:
    await session.write("Password: ")
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
    await write_prompt(session, "Public key (base64, or an ssh-ed25519 line): ")
    text = (await session.read_line()).strip()
    if not text:
        return None
    try:
        return parse_verify_key(text)
    except IdentityError as exc:
        await session.write_line(f"Could not parse key: {exc} -- try again.\r\n")
        return None


def run_ftn_import_nodelist(db: Database, network_name: str, path: Path, *, as_username: str | None = None) -> str:
    """`ftn-import-nodelist`: the outcome line, or `ValueError`/`OSError`
    saying why nothing was imported (design doc §6.8). `--as` names the
    active SysOp the audit log attributes it to; without it, nobody."""
    actor = None
    if as_username is not None:
        actor = next((u for u in list_users(db) if u.username == as_username and u.user_level >= SYSOP_LEVEL
                      and u.disabled_at is None), None)
        if actor is None:
            raise ValueError(f"--as {as_username!r} is not an active SysOp-level account")
    from netbbs.ftn.networks import list_networks
    from netbbs.ftn.nodelist import MAX_NODELIST_BYTES, import_nodelist
    from netbbs.moderation.log import record_action

    network = next((n for n in list_networks(db) if n.name.casefold() == network_name.casefold()), None)
    if network is None:
        names = ", ".join(n.name for n in list_networks(db)) or "none"
        raise ValueError(f"no FTN network called {network_name!r} (networks: {names})")
    if path.stat().st_size > MAX_NODELIST_BYTES:
        raise ValueError(f"{path.name} is over {MAX_NODELIST_BYTES // (1024 * 1024)} MiB")
    count = import_nodelist(db, network.id, path.read_bytes().decode("cp437", errors="replace"))
    record_action(db, actor=actor, action="import_ftn_nodelist", detail=f"{network.name}: {count} nodes from {path.name}")
    return f"Imported {count} nodes for {network.name} from {path.name}."


def build_parser() -> argparse.ArgumentParser:
    """The tool's argument parser, separate from `main()` so its shape
    can be tested without a terminal. With no subcommand the tool opens
    the interactive admin menu, as it always has; `reset-password` is
    the first non-interactive-menu command (issue #611), and
    `rotate-key` (issue #624) the second."""
    def _add_common(target: argparse.ArgumentParser, *, defaults: bool) -> None:
        # The same two options before or after the subcommand, so both
        # `--db x.db reset-password bob` and `reset-password bob --db
        # x.db` work. A subparser's own defaults would otherwise
        # overwrite a value given before the subcommand (argparse
        # applies them last), hence SUPPRESS on the subcommand's copy.
        target.add_argument(
            "--db", type=Path, default=_DEFAULT_DB_PATH if defaults else argparse.SUPPRESS,
            help=f"path to the node's database file (default: {_DEFAULT_DB_PATH})",
        )
        target.add_argument(
            "--as", dest="as_username", default=None if defaults else argparse.SUPPRESS,
            help="attribute this session's actions to this SysOp account (skips the picker)",
        )

    parser = argparse.ArgumentParser(
        prog="python -m netbbs.admin", description="Local SysOp administration tool."
    )
    _add_common(parser, defaults=True)
    subcommands = parser.add_subparsers(dest="command")
    reset = subcommands.add_parser(
        "reset-password",
        help="set a new password on an account without opening the admin menu",
        description=(
            "Set a new password on an account. Prompts for the new password twice, without "
            "echo; the old password is neither asked for nor recoverable. Use this when a "
            "SysOp is locked out of their own account -- otherwise the same action is on the "
            "user's detail screen in the admin menu."
        ),
    )
    reset.add_argument("username", help="the account to set a new password on")
    _add_common(reset, defaults=False)
    rotate = subcommands.add_parser(
        "rotate-key",
        help="replace one of this node's Link operational keys while the node is stopped",
        description=(
            "Replace this node's signing or transport key. The node's fingerprint and address "
            "do not change. Refuses while the node is running; the running node does the same "
            "from its console (Link status, then [K]eys). By default the old key is retired and "
            "what it signed stays valid. With --compromised, peers stop trusting anything the "
            "old key signed and this node signs its own content again under the new key."
        ),
    )
    rotate.add_argument("purpose", choices=("signing", "transport"), help="which key to replace")
    rotate.add_argument(
        "--compromised", action="store_true",
        help="the old key may be in someone else's hands: withdraw belief in everything it signed",
    )
    rotate.add_argument(
        "--identity-dir", type=Path, default=_DEFAULT_IDENTITY_DIR,
        help=f"the node's identity directory (default: {_DEFAULT_IDENTITY_DIR})",
    )
    _add_common(rotate, defaults=False)
    # LAST, the Level Admin SysOp Tool. `levels` was its first name and is
    # kept, so scripts written against it go on working.
    levels = subcommands.add_parser(
        "last",
        aliases=["levels"],
        help="LAST: what each level opens, one level in full, or one account's level change",
        description=(
            "LAST, the Level Admin SysOp Tool, from the shell. "
            "Read-only. With no arguments: every level in use, its name, how many accounts hold it "
            "and what it first opens. With a level (a number or a level's name): what that level "
            "opens and what stays closed to it. With --user and --to: what moving that account to "
            "that level would gain, lose and still leave blocked, as the console shows before a "
            "level change. Nothing is changed."
        ),
    )
    levels.add_argument("level", nargs="?", help="a level, as a number or a level's name")
    levels.add_argument("--user", help="the account whose level change to show")
    levels.add_argument("--to", help="the level to show --user's change to")
    levels.add_argument("--json", action="store_true", help="print JSON instead of text")
    _add_common(levels, defaults=False)
    nodelist = subcommands.add_parser(
        "ftn-import-nodelist",
        help="import an FTN network's nodelist, for direct netmail",
        description=(
            "Replace an FTN network's stored nodelist with the given (unpacked) nodelist file. "
            "Netmail to a node the list says answers BinkP then goes to it directly; the rest goes "
            "via the uplink. The console does the same from Node, FTN mail, [N]odelist import. "
            "Safe while the node runs."
        ),
    )
    nodelist.add_argument("network", help="the network's name, as in Settings, FTN networks")
    nodelist.add_argument("path", type=Path, help="the nodelist file, e.g. FSXNET.123")
    _add_common(nodelist, defaults=False)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    if args.command == "rotate-key":
        # Before the database opens, since opening it applies this build's
        # migrations: a newer tool must not migrate a live older node's
        # database only to refuse afterwards.
        pid = running_node_pid(args.db)
        if pid is not None:
            raise SystemExit(terminal_wrapped(
                f"A node process (PID {pid}) is running on {args.db}. Stop it first, or rotate "
                "from its console: Link status, then [K]eys.",
                stream=sys.stderr,
            ))

    try:
        db = Database(args.db)
    except Exception as exc:
        # A clear, actionable message instead of a raw sqlite3.Error/
        # RuntimeError traceback -- the concrete failure this closes:
        # pointing --db at a database file that doesn't match this
        # build (e.g. one a newer or older version last migrated).
        raise SystemExit(
            terminal_wrapped(
                f"could not open the database at {args.db}: {exc} -- this usually means "
                "the database file doesn't match this build of NetBBS (e.g. it was last migrated "
                "by a newer or older version). If you're testing multiple NetBBS versions side by "
                "side, make sure each one is paired with its own separate database file.",
                stream=sys.stderr,
            )
        ) from exc

    if args.command == "ftn-import-nodelist":
        try:
            print_wrapped(run_ftn_import_nodelist(db, args.network, args.path, as_username=args.as_username))
        except (OSError, ValueError) as exc:
            raise SystemExit(terminal_wrapped(f"Not imported: {exc}", stream=sys.stderr)) from exc
        finally:
            db.close()
        return

    if args.command in ("last", "levels"):
        # Read-only and attributed to nobody: no SysOp to pick, no raw terminal.
        try:
            lines, data = run_levels_report(db, level=args.level, user=args.user, to=args.to)
        except LevelsReportError as exc:
            raise SystemExit(terminal_wrapped(str(exc), stream=sys.stderr)) from exc
        finally:
            db.close()
        if args.json:
            print(render_json(data))
        else:
            for line in lines:
                print_wrapped(line)
        return

    try:
        with raw_terminal():
            if args.command == "reset-password":
                status = asyncio.run(run_reset_password(LocalCLISession(), db, args.as_username, args.username))
            elif args.command == "rotate-key":
                status = asyncio.run(run_rotate_key(
                    LocalCLISession(), db, args.as_username, purpose=args.purpose,
                    compromised=args.compromised, identity_dir=args.identity_dir,
                ))
            else:
                asyncio.run(run_admin_session(LocalCLISession(), db, args.as_username))
                status = 0
    finally:
        db.close()
    if status:
        raise SystemExit(status)


if __name__ == "__main__":
    main()
