"""
Shared SysOp admin menu (design doc -- SysOp foundation round).

The single implementation of every user-management action, reachable
two ways: a gated menu option inside an authenticated BBS session
(`netbbs.net.login_flow`), and the standalone local CLI tool
(`netbbs.admin.__main__`, `python -m netbbs.admin`) -- see that
module's docstring for why the two entry points share this rather than
each carrying their own copy. Every action here is audit-logged
against whichever `User` the caller supplies, regardless of which
entry point that came from.

Follows the submenu shape already established by
`netbbs.net.login_flow._edit_profile`: a redraw-on-real-change-only
draw function, a bell-only-on-invalid-key dispatch loop (design doc
round 52), and `netbbs.net.picker.pick_item` for target selection.
"""

from __future__ import annotations

import asyncio

import nacl.signing

from netbbs.auth.users import (
    AuthError,
    User,
    UserManagementError,
    create_user_async,
    delete_user,
    get_user_by_username,
    list_users,
    set_user_disabled,
    set_user_level,
)
from netbbs.identity.keys import IdentityError, parse_verify_key
from netbbs.moderation.log import list_actions_for_target_user, record_action
from netbbs.net.picker import pick_item
from netbbs.net.session import Session
from netbbs.net.session_registry import SessionSummary
from netbbs.net.shutdown import NodeControls, run_shutdown_sequence
from netbbs.rendering import HEADER_COLOR, MUTED_COLOR, colored, menu_key, sanitize_text
from netbbs.storage.database import Database
from netbbs.timeutil import format_for_display


async def admin_menu(
    session: Session, db: Database, user: User, *, node_controls: NodeControls | None = None
) -> None:
    """
    Top-level SysOp admin menu. Callers are responsible for their own
    level gating before entering this -- it performs no permission
    check of its own, matching `pick_item`'s "presentation and
    selection only" precedent.

    `node_controls` (design doc -- node management round), if given,
    unlocks the `[N]ode` submenu (list/disconnect sessions, trigger
    shutdown) -- present when called from within a live session
    (`netbbs.net.login_flow`), absent (`None`) when called from the
    standalone `python -m netbbs.admin` CLI, which has no access to a
    running node's live in-memory state at all (confirmed design
    decision, not an oversight -- see that module's docstring).
    """
    await _draw_admin_menu(session, node_controls)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "c":
            await session.write_line("")
            await _create_user_screen(session, db, user)
            await _draw_admin_menu(session, node_controls)
        elif choice == "l":
            await session.write_line("")
            await _list_users_screen(session, db)
            await _draw_admin_menu(session, node_controls)
        elif choice == "p":
            await session.write_line("")
            await _change_level_screen(session, db, user)
            await _draw_admin_menu(session, node_controls)
        elif choice == "e":
            await session.write_line("")
            await _disable_enable_screen(session, db, user)
            await _draw_admin_menu(session, node_controls)
        elif choice == "d":
            await session.write_line("")
            await _delete_user_screen(session, db, user)
            await _draw_admin_menu(session, node_controls)
        elif choice == "n" and node_controls is not None:
            await session.write_line("")
            await _node_menu(session, db, user, node_controls)
            await _draw_admin_menu(session, node_controls)
        else:
            await session.write("\a")


async def _draw_admin_menu(session: Session, node_controls: NodeControls | None) -> None:
    header = colored("SysOp admin menu:", fg_color=HEADER_COLOR, bold=True)
    option_list = [
        menu_key("C", "reate user"),
        menu_key("L", "ist users"),
        menu_key("P", "romote/demote"),
        menu_key("E", "nable/disable"),
        menu_key("D", "elete user"),
    ]
    if node_controls is not None:
        option_list.append(menu_key("N", "ode"))
    option_list.append(menu_key("B", "ack"))
    options = "  ".join(option_list)
    await session.write_line(f"\r\n{header} {options}")
    await session.write("Choice: ")


# -- create ------------------------------------------------------------


async def _create_user_screen(session: Session, db: Database, actor: User) -> None:
    await session.write_line(colored("\r\nCreate user", fg_color=HEADER_COLOR, bold=True))
    await session.write("Username: ")
    username = (await session.read_line()).strip()
    if not username:
        await session.write_line(colored("Cancelled: username cannot be blank.", fg_color=MUTED_COLOR))
        return

    password = await _prompt_optional_password(session)
    verify_key = await _prompt_optional_pubkey(session)
    if password is None and verify_key is None:
        await session.write_line(
            colored("Cancelled: an account needs a password, a public key, or both.", fg_color=MUTED_COLOR)
        )
        return

    await session.write("Starting level [0]: ")
    level_raw = (await session.read_line()).strip()
    try:
        level = int(level_raw) if level_raw else 0
    except ValueError:
        await session.write_line(colored("Not a number -- cancelled.", fg_color=MUTED_COLOR))
        return

    try:
        new_user = await create_user_async(
            db, username, password=password, verify_key=verify_key, user_level=level
        )
    except AuthError as exc:
        await session.write_line(colored(f"Could not create account: {exc}", fg_color=MUTED_COLOR))
        return

    record_action(
        db, actor=actor, action="create_user", target_user_id=new_user.id,
        detail=f"created user {new_user.username!r} at level {level}",
    )
    await session.write_line(f"Created {new_user.username!r} at level {level}.")


async def _prompt_optional_password(session: Session) -> str | None:
    await session.write("Set a password? [y/N]: ")
    answer = (await session.read_key()).lower()
    await session.write_line("")
    if answer != "y":
        return None
    await session.write("Password: ")
    first = await session.read_line(echo=False)
    await session.write("Confirm password: ")
    second = await session.read_line(echo=False)
    if not first or first != second:
        await session.write_line(
            colored("Passwords did not match or were blank -- no password set.", fg_color=MUTED_COLOR)
        )
        return None
    return first


async def _prompt_optional_pubkey(session: Session) -> nacl.signing.VerifyKey | None:
    await session.write("Add a public key? [y/N]: ")
    answer = (await session.read_key()).lower()
    await session.write_line("")
    if answer != "y":
        return None
    await session.write("Paste the public key (base64, or an ssh-ed25519 line): ")
    text = (await session.read_line()).strip()
    try:
        return parse_verify_key(text)
    except IdentityError as exc:
        await session.write_line(colored(f"Could not parse key: {exc} -- no key set.", fg_color=MUTED_COLOR))
        return None


# -- list / detail -------------------------------------------------------


async def _list_users_screen(session: Session, db: Database) -> None:
    users = list_users(db)
    selected = await pick_item(
        session, users,
        name_of=lambda u: u.username,
        stable_id_of=lambda u: u.id,
        description_of=_user_description,
        title="Registered users",
        empty_message="No registered users yet.",
    )
    if selected is not None:
        await _show_user_detail(session, db, selected)


def _user_description(user: User) -> str:
    status = "disabled" if user.disabled_at is not None else "active"
    return f"level {user.user_level}, {status}"


async def _show_user_detail(session: Session, db: Database, target: User) -> None:
    header = colored(sanitize_text(target.username), fg_color=HEADER_COLOR, bold=True)
    await session.write_line(f"\r\n{header}")
    await session.write_line(f"Level: {target.user_level}")
    await session.write_line(f"Status: {'disabled' if target.disabled_at is not None else 'active'}")
    await session.write_line(f"Member since: {format_for_display(target.created_at, db)}")

    entries = list_actions_for_target_user(db, target.id)
    if not entries:
        await session.write_line(colored("No recorded admin actions.", fg_color=MUTED_COLOR))
        return
    await session.write_line(colored("Recent admin actions:", fg_color=MUTED_COLOR))
    for entry in entries[-10:]:
        when = format_for_display(entry.created_at, db)
        detail = f" -- {sanitize_text(entry.detail)}" if entry.detail else ""
        await session.write_line(f"  {when}: {sanitize_text(entry.action)}{detail}")


# -- promote/demote, enable/disable ---------------------------------------


async def _pick_target_user(session: Session, db: Database, *, title: str) -> User | None:
    users = list_users(db)
    return await pick_item(
        session, users,
        name_of=lambda u: u.username,
        stable_id_of=lambda u: u.id,
        description_of=_user_description,
        title=title,
        empty_message="No registered users yet.",
    )


async def _change_level_screen(session: Session, db: Database, actor: User) -> None:
    target = await _pick_target_user(session, db, title="Promote/demote which user?")
    if target is None:
        return
    await session.write(f"New level for {target.username!r} [{target.user_level}]: ")
    raw = (await session.read_line()).strip()
    if not raw:
        return
    try:
        new_level = int(raw)
    except ValueError:
        await session.write_line(colored("Not a number -- cancelled.", fg_color=MUTED_COLOR))
        return
    try:
        updated = set_user_level(db, target, new_level, changed_by=actor)
    except UserManagementError as exc:
        await session.write_line(colored(str(exc), fg_color=MUTED_COLOR))
        return
    await session.write_line(f"{updated.username!r} is now level {updated.user_level}.")


async def _disable_enable_screen(session: Session, db: Database, actor: User) -> None:
    target = await _pick_target_user(session, db, title="Enable/disable which user?")
    if target is None:
        return
    currently_disabled = target.disabled_at is not None
    action_word = "Enable" if currently_disabled else "Disable"
    await session.write(f"{action_word} {target.username!r}? [y/N]: ")
    answer = (await session.read_key()).lower()
    await session.write_line("")
    if answer != "y":
        return
    try:
        updated = set_user_disabled(db, target, not currently_disabled, changed_by=actor)
    except UserManagementError as exc:
        await session.write_line(colored(str(exc), fg_color=MUTED_COLOR))
        return
    await session.write_line(
        f"{updated.username!r} is now {'disabled' if updated.disabled_at is not None else 'active'}."
    )


# -- delete ----------------------------------------------------------------


async def _delete_user_screen(session: Session, db: Database, actor: User) -> None:
    target = await _pick_target_user(session, db, title="Delete which user?")
    if target is None:
        return
    await session.write_line(
        colored(
            "\r\nThis permanently deletes the account. Posts and files they created "
            "keep their recorded author name; moderator grants, channel membership/"
            "invitations, preferences, and blocklist entries tied to this account are "
            "removed. This cannot be undone.",
            fg_color=MUTED_COLOR,
        )
    )
    await session.write(f"Type the username {target.username!r} to confirm, or anything else to cancel: ")
    confirmation = (await session.read_line()).strip()
    if confirmation != target.username:
        await session.write_line("Cancelled.")
        return
    try:
        delete_user(db, target, deleted_by=actor)
    except UserManagementError as exc:
        await session.write_line(colored(str(exc), fg_color=MUTED_COLOR))
        return
    await session.write_line(f"{target.username!r} deleted.")


# -- node management (design doc -- node management round) -----------------


async def _node_menu(session: Session, db: Database, actor: User, node_controls: NodeControls) -> None:
    await _draw_node_menu(session)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "w":
            await session.write_line("")
            await _who_screen(session, db, actor, node_controls)
            await _draw_node_menu(session)
        elif choice == "s":
            await session.write_line("")
            await _shutdown_screen(session, db, actor, node_controls)
            await _draw_node_menu(session)
        else:
            await session.write("\a")


async def _draw_node_menu(session: Session) -> None:
    header = colored("Node management:", fg_color=HEADER_COLOR, bold=True)
    options = "  ".join([menu_key("W", "ho"), menu_key("S", "hutdown"), menu_key("B", "ack")])
    await session.write_line(f"\r\n{header} {options}")
    await session.write("Choice: ")


def _session_name(entry: SessionSummary) -> str:
    if entry.username is not None:
        return entry.username
    return f"(unauthenticated) {entry.peer_address or 'unknown address'}"


def _session_description(db: Database, entry: SessionSummary) -> str:
    return f"connected since {format_for_display(entry.connected_at, db)}"


async def _who_screen(session: Session, db: Database, actor: User, node_controls: NodeControls) -> None:
    entries = node_controls.session_registry.list_entries()
    selected = await pick_item(
        session, entries,
        name_of=_session_name,
        stable_id_of=lambda e: id(e.session),
        description_of=lambda e: _session_description(db, e),
        title="Active sessions",
        empty_message="No active sessions.",
    )
    if selected is None:
        return

    if selected.session is session:
        await session.write_line(
            colored("That's your own session -- use Logoff instead.", fg_color=MUTED_COLOR)
        )
        return

    await session.write(f"Disconnect {_session_name(selected)!r}? [y/N]: ")
    answer = (await session.read_key()).lower()
    await session.write_line("")
    if answer != "y":
        return

    target_user_id: int | None = None
    detail = f"peer address {selected.peer_address or 'unknown'}"
    if selected.username is not None:
        try:
            target_user_id = get_user_by_username(db, selected.username).id
        except AuthError:
            pass  # account no longer exists -- log by peer address only

    disconnected = await node_controls.session_registry.disconnect_one(selected.session)
    if not disconnected:
        await session.write_line(colored("That session is already gone.", fg_color=MUTED_COLOR))
        return

    record_action(
        db, actor=actor, action="disconnect_session", target_user_id=target_user_id, detail=detail
    )
    await session.write_line(f"{_session_name(selected)!r} disconnected.")


async def _shutdown_screen(session: Session, db: Database, actor: User, node_controls: NodeControls) -> None:
    await session.write_line(
        colored(
            "\r\nThis warns and disconnects every connected session (including this "
            "one), then locks out new logins. This cannot be undone once confirmed.",
            fg_color=MUTED_COLOR,
        )
    )
    await session.write("Graceful (wait, then disconnect) or immediate? [G/i]: ")
    mode_answer = (await session.read_key()).lower()
    await session.write_line("")
    graceful = mode_answer != "i"

    await session.write("Custom broadcast message (leave blank for the default): ")
    message_raw = (await session.read_line()).strip()
    message = message_raw or None

    mode_label = "graceful" if graceful else "immediate"
    await session.write(f"Confirm {mode_label} shutdown? [y/N]: ")
    confirm = (await session.read_key()).lower()
    await session.write_line("")
    if confirm != "y":
        await session.write_line("Cancelled.")
        return

    # Logged before triggering, not after: the sequence disconnects
    # this very session too (see run_shutdown_sequence's own docstring
    # on why it's fired as a background task rather than awaited
    # inline), so there's no guarantee this session survives long
    # enough afterward to still be able to write an audit row.
    record_action(
        db, actor=actor, action="trigger_shutdown",
        detail=f"graceful={graceful}, message={message!r}",
    )
    asyncio.create_task(
        run_shutdown_sequence(
            graceful=graceful,
            session_registry=node_controls.session_registry,
            maintenance=node_controls.maintenance,
            graceful_delay_seconds=node_controls.graceful_delay_seconds,
            shutdown_event=node_controls.shutdown_event,
            message=message,
        )
    )
    await session.write_line("Shutdown sequence started.")
