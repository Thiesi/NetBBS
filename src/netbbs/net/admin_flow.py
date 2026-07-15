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
from netbbs.boards.boards import Board, BoardError, create_board, delete_board, list_boards, update_board
from netbbs.boards.categories import Category, CategoryError
from netbbs.boards.categories import create_category as create_board_category
from netbbs.boards.categories import delete_category as delete_board_category
from netbbs.boards.categories import list_subcategories as list_board_subcategories
from netbbs.boards.categories import list_top_level_categories as list_top_level_board_categories
from netbbs.boards.posts import (
    Post,
    PostError,
    approve_post,
    delete_post,
    list_pending_posts,
    set_post_exempt,
    set_post_pinned,
)
from netbbs.chat.categories import CategoryError as ChannelCategoryError
from netbbs.chat.categories import create_category as create_channel_category
from netbbs.chat.categories import delete_category as delete_channel_category
from netbbs.chat.categories import list_subcategories as list_channel_subcategories
from netbbs.chat.categories import list_top_level_categories as list_top_level_channel_categories
from netbbs.chat.channels import Channel, ChannelError, create_channel, delete_channel, list_channels, update_channel
from netbbs.files.areas import FileArea, FileAreaError, create_file_area, delete_file_area, list_file_areas, update_file_area
from netbbs.files.categories import FileAreaCategory
from netbbs.files.categories import FileAreaCategoryError as FileCategoryError
from netbbs.files.categories import create_category as create_file_category
from netbbs.files.categories import delete_category as delete_file_category
from netbbs.files.categories import list_subcategories as list_file_subcategories
from netbbs.files.categories import list_top_level_categories as list_top_level_file_categories
from netbbs.files.gc import GCReport, reclaim_orphaned_blobs
from netbbs.files.entries import (
    FileEntry,
    approve_file,
    delete_file,
    list_pending_files,
    set_file_exempt,
    set_file_pinned,
)
from netbbs.identity.keys import IdentityError, parse_verify_key
from netbbs.moderation.log import list_actions_for_target_user, record_action
from netbbs.moderation.roles import (
    BoardPermission,
    ChannelPermission,
    get_grant,
    grant_permissions,
    revoke_permissions,
)
from netbbs.net.picker import pick_item
from netbbs.net.session import Session
from netbbs.net.session_registry import SessionSummary
from netbbs.net.shutdown import NodeControls, run_shutdown_sequence
from netbbs.net.ansi_editor import edit_ansi_art
from netbbs.net.welcome_banner import (
    MAX_BANNER_SIZE_BYTES,
    banner_path,
    load_welcome_banner,
    set_welcome_banner_enabled,
    welcome_banner_status,
)
from netbbs.rendering import HEADER_COLOR, MUTED_COLOR, colored, menu_key, reflow, reject_keystroke, sanitize_text
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
            await _disable_enable_screen(session, db, user, node_controls)
            await _draw_admin_menu(session, node_controls)
        elif choice == "d":
            await session.write_line("")
            await _delete_user_screen(session, db, user, node_controls)
            await _draw_admin_menu(session, node_controls)
        elif choice == "n" and node_controls is not None:
            await session.write_line("")
            await _node_menu(session, db, user, node_controls)
            await _draw_admin_menu(session, node_controls)
        elif choice == "w":
            await session.write_line("")
            await _welcome_banner_menu(session, db, user)
            await _draw_admin_menu(session, node_controls)
        elif choice == "m":
            await session.write_line("")
            await _content_menu(session, db, user)
            await _draw_admin_menu(session, node_controls)
        else:
            await session.write(reject_keystroke())


async def _draw_admin_menu(session: Session, node_controls: NodeControls | None) -> None:
    header = colored("SysOp admin menu:", fg_color=HEADER_COLOR, bold=True)
    option_list = [
        menu_key("C", "reate user"),
        menu_key("L", "ist users"),
        menu_key("P", "romote/demote"),
        menu_key("E", "nable/disable"),
        menu_key("D", "elete user"),
        menu_key("M", "anage boards/areas/channels"),
        menu_key("W", "elcome banner"),
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


async def _disable_enable_screen(
    session: Session, db: Database, actor: User, node_controls: NodeControls | None = None
) -> None:
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
    if updated.disabled_at is not None:
        await _revoke_live_sessions(session, node_controls, updated, actor)


# -- delete ----------------------------------------------------------------


async def _delete_user_screen(
    session: Session, db: Database, actor: User, node_controls: NodeControls | None = None
) -> None:
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
    await _revoke_live_sessions(session, node_controls, target, actor)


async def _revoke_live_sessions(
    session: Session, node_controls: NodeControls | None, target: User, actor: User
) -> None:
    """
    The immediate, in-process half of revoking access (GitHub issue
    #29): forcibly disconnect every currently registered session
    authenticated as `target.username`, right after a successful
    disable or delete. A no-op when `node_controls` is `None` (the
    standalone `python -m netbbs.admin` CLI has no live node state to
    act on at all -- see `admin_menu`'s own docstring on that).

    The acting SysOp's own *current* session is deliberately excluded
    (self-targeting: disabling/deleting your own account while it's
    the one running this code) -- `ActiveSessionRegistry.
    disconnect_username`'s docstring explains why that specific session
    can't safely be cancelled-and-awaited from within itself. Any of
    the acting SysOp's *other* live sessions still get disconnected
    normally; the current one is caught instead by the cross-process
    revalidation boundary in `netbbs.net.login_flow._main_menu` at its
    next safe checkpoint.
    """
    if node_controls is None:
        return
    exclude = session if target.id == actor.id else None
    disconnected = await node_controls.session_registry.disconnect_username(
        target.username, exclude_session=exclude
    )
    if disconnected:
        plural = "session" if disconnected == 1 else "sessions"
        await session.write_line(
            colored(f"Disconnected {disconnected} live {plural}.", fg_color=MUTED_COLOR)
        )


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
            await session.write(reject_keystroke())


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


# -- welcome banner (design doc -- welcome banner round, Round A of a
# three-part skinning initiative) -------------------------------------


async def _welcome_banner_menu(session: Session, db: Database, actor: User) -> None:
    await _draw_welcome_banner_menu(session, db)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "p":
            await session.write_line("")
            await _preview_welcome_banner_screen(session, db)
            await _draw_welcome_banner_menu(session, db)
        elif choice == "e":
            await session.write_line("")
            await _enable_welcome_banner_screen(session, db, actor)
            await _draw_welcome_banner_menu(session, db)
        elif choice == "d":
            await session.write_line("")
            await _disable_welcome_banner_screen(session, db, actor)
            await _draw_welcome_banner_menu(session, db)
        elif choice == "x":
            await session.write_line("")
            await _edit_welcome_banner_screen(session, db, actor)
            await _draw_welcome_banner_menu(session, db)
        else:
            await session.write(reject_keystroke())


async def _draw_welcome_banner_menu(session: Session, db: Database) -> None:
    status = welcome_banner_status(db)
    state = "ENABLED" if status.enabled else "disabled"
    if status.exists:
        file_state = f"{status.size_bytes} bytes"
    else:
        file_state = "missing"
    header = colored("Welcome banner:", fg_color=HEADER_COLOR, bold=True)
    detail = f"{state} -- file: {status.path} ({file_state})"
    options = "  ".join(
        [
            menu_key("P", "review"),
            menu_key("E", "nable"),
            menu_key("D", "isable"),
            menu_key("X", " edit"),
            menu_key("B", "ack"),
        ]
    )
    await session.write_line(f"\r\n{header} {detail}\r\n{options}")
    await session.write("Choice: ")


async def _preview_welcome_banner_screen(session: Session, db: Database) -> None:
    """Renders the exact banner `netbbs.net.login_flow` would show at
    login right now -- the same `load_welcome_banner` call, used as a
    smoke test of the loading path itself, not a separate rendering."""
    status = welcome_banner_status(db)
    await session.write_line(colored("\r\nPreviewing welcome banner as shown at login:", fg_color=MUTED_COLOR))
    await session.write_line(load_welcome_banner(db))
    if status.enabled and status.exists and (status.size_bytes or 0) <= MAX_BANNER_SIZE_BYTES:
        await session.write_line(colored("(showing your custom file)", fg_color=MUTED_COLOR))
    else:
        await session.write_line(
            colored(
                f"(showing the DEFAULT banner -- enabled={status.enabled}, file exists={status.exists})",
                fg_color=MUTED_COLOR,
            )
        )


async def _enable_welcome_banner_screen(session: Session, db: Database, actor: User) -> None:
    status = welcome_banner_status(db)
    if not status.exists:
        await session.write_line(
            colored(
                f"No banner file found at {status.path}. Place a .ans file there first, then enable.",
                fg_color=MUTED_COLOR,
            )
        )
        return
    if (status.size_bytes or 0) > MAX_BANNER_SIZE_BYTES:
        await session.write_line(
            colored(
                f"Banner file at {status.path} is {status.size_bytes} bytes, over the "
                f"{MAX_BANNER_SIZE_BYTES} byte limit -- not enabling.",
                fg_color=MUTED_COLOR,
            )
        )
        return

    set_welcome_banner_enabled(db, True)
    record_action(db, actor=actor, action="enable_welcome_banner", detail=str(status.path))
    await session.write_line("Welcome banner enabled. Use [P]review to verify it looks right.")


async def _disable_welcome_banner_screen(session: Session, db: Database, actor: User) -> None:
    status = welcome_banner_status(db)
    set_welcome_banner_enabled(db, False)
    record_action(db, actor=actor, action="disable_welcome_banner", detail=str(status.path))
    await session.write_line(
        f"Reverted to the default banner. Your file at {status.path} was left in place."
    )


async def _edit_welcome_banner_screen(session: Session, db: Database, actor: User) -> None:
    """Opens the WYSIWYG ANSI art editor (design doc -- welcome banner
    round B1) against the current banner file, if any. `edit_ansi_art`
    itself knows nothing about "welcome banner" -- this screen is
    responsible for loading the existing file, computing the draft
    path, and writing a real save back to `banner_path(db)`."""
    path = banner_path(db)
    initial_bytes = path.read_bytes() if path.exists() else None
    draft_path = path.parent / f"{path.name}.draft"

    result = await edit_ansi_art(session, initial_bytes=initial_bytes, draft_path=draft_path)
    if result is None:
        await session.write_line(colored("\r\nNo changes saved.", fg_color=MUTED_COLOR))
        return

    path.write_bytes(result)
    record_action(db, actor=actor, action="edit_welcome_banner", detail=str(path))
    await session.write_line(f"\r\nSaved {path}. Use [P]review to verify it looks right.")


# -- boards & areas (design doc -- board/area management round) -----------
#
# Boards and file areas share an identical schema shape and permission
# model (BoardPermission is reused for both object_type='board' and
# 'file_area', see netbbs.moderation.roles) but diverge in terminology
# ("post" vs "file", max_post_age_days vs max_file_age_days) -- written
# as two structurally-parallel but separately-coded sections here,
# matching this file's existing style for user management rather than
# building a shared abstraction for just two call sites.


async def _content_menu(session: Session, db: Database, actor: User) -> None:
    await _draw_content_menu(session)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "m":
            await session.write_line("")
            await _board_menu(session, db, actor)
            await _draw_content_menu(session)
        elif choice == "a":
            await session.write_line("")
            await _area_menu(session, db, actor)
            await _draw_content_menu(session)
        elif choice == "h":
            await session.write_line("")
            await _channel_menu(session, db, actor)
            await _draw_content_menu(session)
        elif choice == "c":
            await session.write_line("")
            await _category_menu(session, db, actor)
            await _draw_content_menu(session)
        elif choice == "g":
            await session.write_line("")
            await _grant_moderator_screen(session, db, actor)
            await _draw_content_menu(session)
        elif choice == "r":
            await session.write_line("")
            await _revoke_moderator_screen(session, db, actor)
            await _draw_content_menu(session)
        else:
            await session.write(reject_keystroke())


async def _draw_content_menu(session: Session) -> None:
    header = colored("Manage boards/areas/channels:", fg_color=HEADER_COLOR, bold=True)
    options = "  ".join(
        [
            menu_key("M", "essage boards"),
            menu_key("A", "reas"),
            menu_key("H", "annels"),
            menu_key("C", "ategories"),
            menu_key("G", "rant moderator"),
            menu_key("R", "evoke moderator"),
            menu_key("B", "ack"),
        ]
    )
    await session.write_line(f"\r\n{header} {options}")
    await session.write("Choice: ")


async def _read_int(session: Session, *, default: int) -> int | None:
    """Reads a line: blank keeps `default`, a valid integer replaces
    it, anything else shows a cancellation message and returns `None`
    -- callers should treat `None` as "abort the current screen"."""
    raw = (await session.read_line()).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        await session.write_line(colored("Not a number -- cancelled.", fg_color=MUTED_COLOR))
        return None


async def _pick_optional_category(session: Session, db: Database, *, list_top_level, list_subcategories, title: str):
    """Optional category picker shared by board/area create+edit
    screens. Top-level categories are shown first; picking one that has
    sub-categories offers picking one of those instead, matching the
    two-level design (`netbbs.boards.categories`/`netbbs.files.
    categories`). Returns the chosen category's id, or `None` if
    declined or none exist."""
    await session.write("Assign a category? [y/N]: ")
    answer = (await session.read_key()).lower()
    await session.write_line("")
    if answer != "y":
        return None
    top_level = list_top_level(db)
    selected = await pick_item(
        session, top_level,
        name_of=lambda c: c.name,
        stable_id_of=lambda c: c.id,
        title=title,
        empty_message="No categories exist yet.",
    )
    if selected is None:
        return None
    subs = list_subcategories(db, selected.id)
    if not subs:
        return selected.id
    await session.write(f"Use a sub-category of {selected.name!r} instead? [y/N]: ")
    answer = (await session.read_key()).lower()
    await session.write_line("")
    if answer != "y":
        return selected.id
    sub_selected = await pick_item(
        session, subs,
        name_of=lambda c: c.name,
        stable_id_of=lambda c: c.id,
        title=f"Sub-category of {selected.name!r}",
        empty_message="No sub-categories.",
    )
    return sub_selected.id if sub_selected is not None else selected.id


# -- message boards ----------------------------------------------------


async def _board_menu(session: Session, db: Database, actor: User) -> None:
    await _draw_board_menu(session)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "c":
            await session.write_line("")
            await _create_board_screen(session, db, actor)
            await _draw_board_menu(session)
        elif choice == "l":
            await session.write_line("")
            await _list_boards_screen(session, db, actor)
            await _draw_board_menu(session)
        else:
            await session.write(reject_keystroke())


async def _draw_board_menu(session: Session) -> None:
    header = colored("Message boards:", fg_color=HEADER_COLOR, bold=True)
    options = "  ".join([menu_key("C", "reate"), menu_key("L", "ist"), menu_key("B", "ack")])
    await session.write_line(f"\r\n{header} {options}")
    await session.write("Choice: ")


async def _create_board_screen(session: Session, db: Database, actor: User) -> None:
    await session.write_line(colored("\r\nCreate board", fg_color=HEADER_COLOR, bold=True))
    await session.write("Name: ")
    name = (await session.read_line()).strip()
    if not name:
        await session.write_line(colored("Cancelled: name cannot be blank.", fg_color=MUTED_COLOR))
        return
    await session.write("Description (optional): ")
    description = (await session.read_line()).strip() or None
    await session.write("Minimum read level [0]: ")
    min_read_level = await _read_int(session, default=0)
    if min_read_level is None:
        return
    await session.write("Minimum write level [0]: ")
    min_write_level = await _read_int(session, default=0)
    if min_write_level is None:
        return
    category_id = await _pick_optional_category(
        session, db, list_top_level=list_top_level_board_categories,
        list_subcategories=list_board_subcategories, title="Board category",
    )
    await session.write("Pinned? [y/N]: ")
    pinned = (await session.read_key()).lower() == "y"
    await session.write_line("")
    await session.write("Moderated (posts need approval)? [y/N]: ")
    moderated = (await session.read_key()).lower() == "y"
    await session.write_line("")
    await session.write("Max post age in days (blank = unlimited): ")
    max_age_raw = (await session.read_line()).strip()
    max_post_age_days = None
    if max_age_raw:
        try:
            max_post_age_days = int(max_age_raw)
        except ValueError:
            await session.write_line(colored("Not a number -- cancelled.", fg_color=MUTED_COLOR))
            return

    try:
        board = create_board(
            db, name, description=description, min_read_level=min_read_level,
            min_write_level=min_write_level, category_id=category_id, pinned=pinned,
            moderated=moderated, max_post_age_days=max_post_age_days, creator=actor,
        )
    except BoardError as exc:
        await session.write_line(colored(f"Could not create board: {exc}", fg_color=MUTED_COLOR))
        return
    await session.write_line(f"Created board {board.name!r}.")


async def _list_boards_screen(session: Session, db: Database, actor: User) -> None:
    boards = list_boards(db, order_by="alphabetical")
    selected = await pick_item(
        session, boards,
        name_of=lambda b: b.name,
        stable_id_of=lambda b: b.id,
        description_of=_board_description,
        title="Boards",
        empty_message="No boards yet.",
    )
    if selected is not None:
        await _board_detail_screen(session, db, actor, selected)


def _board_description(board: Board) -> str:
    status = "moderated" if board.moderated else "open"
    return f"read {board.min_read_level}/write {board.min_write_level}, {status}"


async def _board_detail_screen(session: Session, db: Database, actor: User, board: Board) -> None:
    await _draw_board_detail(session, board)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "e":
            await session.write_line("")
            updated = await _edit_board_screen(session, db, actor, board)
            if updated is not None:
                board = updated
            await _draw_board_detail(session, board)
        elif choice == "d":
            await session.write_line("")
            deleted = await _delete_board_screen(session, db, actor, board)
            if deleted:
                return
            await _draw_board_detail(session, board)
        elif choice == "p":
            await session.write_line("")
            await _pending_posts_screen(session, db, actor, board)
            await _draw_board_detail(session, board)
        else:
            await session.write(reject_keystroke())


async def _draw_board_detail(session: Session, board: Board) -> None:
    header = colored(sanitize_text(board.name), fg_color=HEADER_COLOR, bold=True)
    await session.write_line(f"\r\n{header}")
    await session.write_line(f"Description: {sanitize_text(board.description) if board.description else '(none)'}")
    await session.write_line(f"Read level: {board.min_read_level}  Write level: {board.min_write_level}")
    await session.write_line(
        f"Pinned: {'yes' if board.pinned else 'no'}  Moderated: {'yes' if board.moderated else 'no'}"
    )
    age = board.max_post_age_days if board.max_post_age_days is not None else "unlimited"
    await session.write_line(f"Max post age: {age} days")
    options = "  ".join(
        [menu_key("E", "dit"), menu_key("D", "elete"), menu_key("P", "ending posts"), menu_key("B", "ack")]
    )
    await session.write_line(f"\r\n{options}")
    await session.write("Choice: ")


async def _edit_board_screen(session: Session, db: Database, actor: User, board: Board) -> Board | None:
    await session.write(f"Name [{board.name}]: ")
    name = (await session.read_line()).strip() or board.name
    await session.write(f"Description [{board.description or '(none)'}]: ")
    description = (await session.read_line()).strip() or board.description
    await session.write(f"Minimum read level [{board.min_read_level}]: ")
    min_read_level = await _read_int(session, default=board.min_read_level)
    if min_read_level is None:
        return None
    await session.write(f"Minimum write level [{board.min_write_level}]: ")
    min_write_level = await _read_int(session, default=board.min_write_level)
    if min_write_level is None:
        return None
    await session.write("Change category? [y/N]: ")
    change_category = (await session.read_key()).lower() == "y"
    await session.write_line("")
    category_id = board.category_id
    if change_category:
        category_id = await _pick_optional_category(
            session, db, list_top_level=list_top_level_board_categories,
            list_subcategories=list_board_subcategories, title="Board category",
        )
    await session.write(f"Pinned? [{'y' if board.pinned else 'N'}]: ")
    pinned_answer = (await session.read_key()).lower()
    await session.write_line("")
    pinned = pinned_answer == "y" if pinned_answer in ("y", "n") else board.pinned
    await session.write(f"Moderated? [{'y' if board.moderated else 'N'}]: ")
    moderated_answer = (await session.read_key()).lower()
    await session.write_line("")
    moderated = moderated_answer == "y" if moderated_answer in ("y", "n") else board.moderated
    current_age = board.max_post_age_days if board.max_post_age_days is not None else "unlimited"
    await session.write(f"Max post age in days [{current_age}] (blank = keep, 'none' = unlimited): ")
    max_age_raw = (await session.read_line()).strip()
    max_post_age_days = board.max_post_age_days
    if max_age_raw.lower() == "none":
        max_post_age_days = None
    elif max_age_raw:
        try:
            max_post_age_days = int(max_age_raw)
        except ValueError:
            await session.write_line(colored("Not a number -- cancelled.", fg_color=MUTED_COLOR))
            return None

    try:
        updated = update_board(
            db, board, name=name, description=description, min_read_level=min_read_level,
            min_write_level=min_write_level, category_id=category_id, pinned=pinned,
            moderated=moderated, max_post_age_days=max_post_age_days, changed_by=actor,
        )
    except BoardError as exc:
        await session.write_line(colored(f"Could not update board: {exc}", fg_color=MUTED_COLOR))
        return None
    await session.write_line(f"Updated {updated.name!r}.")
    return updated


async def _delete_board_screen(session: Session, db: Database, actor: User, board: Board) -> bool:
    await session.write_line(
        colored(
            "\r\nThis permanently deletes the board, all of its posts, and any "
            "moderator grants scoped to it. This cannot be undone.",
            fg_color=MUTED_COLOR,
        )
    )
    await session.write(f"Type the board name {board.name!r} to confirm, or anything else to cancel: ")
    confirmation = (await session.read_line()).strip()
    if confirmation != board.name:
        await session.write_line("Cancelled.")
        return False
    delete_board(db, board, deleted_by=actor)
    await session.write_line(f"{board.name!r} deleted.")
    return True


async def _pending_posts_screen(session: Session, db: Database, actor: User, board: Board) -> None:
    while True:
        posts = list_pending_posts(db, board, requesting_user=actor)
        selected = await pick_item(
            session, posts,
            name_of=lambda p: p.subject,
            stable_id_of=lambda p: p.id,
            description_of=lambda p: f"by {p.author_label}",
            title=f"Pending posts in {board.name!r}",
            empty_message="No pending posts.",
        )
        if selected is None:
            return
        await _post_action_screen(session, db, actor, selected)


async def _draw_post_action(session: Session, post: Post) -> None:
    header = colored(sanitize_text(post.subject), fg_color=HEADER_COLOR, bold=True)
    await session.write_line(f"\r\n{header}")
    await session.write_line(f"By: {sanitize_text(post.author_label)}")
    await session.write_line(reflow(sanitize_text(post.body, allow_newlines=True), width=session.terminal_width))
    options = "  ".join(
        [
            menu_key("A", "pprove"),
            menu_key("R", "eject"),
            menu_key("P", "in toggle"),
            menu_key("X", "empt toggle"),
            menu_key("B", "ack"),
        ]
    )
    await session.write_line(f"\r\n{options}")
    await session.write("Choice: ")


async def _post_action_screen(session: Session, db: Database, actor: User, post: Post) -> None:
    await _draw_post_action(session, post)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "a":
            await session.write_line("")
            approve_post(db, post, approved_by=actor)
            await session.write_line("Approved.")
            return
        elif choice == "r":
            await session.write_line("")
            try:
                delete_post(db, post, deleted_by=actor)
            except PostError as exc:
                await session.write_line(f"Error: {exc}")
                await _draw_post_action(session, post)
                continue
            await session.write_line("Rejected.")
            return
        elif choice == "p":
            await session.write_line("")
            post = set_post_pinned(db, post, not post.pinned, changed_by=actor)
            await _draw_post_action(session, post)
        elif choice == "x":
            await session.write_line("")
            post = set_post_exempt(db, post, not post.exempt_from_expiry, changed_by=actor)
            await _draw_post_action(session, post)
        else:
            await session.write(reject_keystroke())


# -- file areas ----------------------------------------------------------


async def _area_menu(session: Session, db: Database, actor: User) -> None:
    await _draw_area_menu(session)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "c":
            await session.write_line("")
            await _create_area_screen(session, db, actor)
            await _draw_area_menu(session)
        elif choice == "l":
            await session.write_line("")
            await _list_areas_screen(session, db, actor)
            await _draw_area_menu(session)
        elif choice == "g":
            await session.write_line("")
            await _gc_screen(session, db)
            await _draw_area_menu(session)
        else:
            await session.write(reject_keystroke())


async def _draw_area_menu(session: Session) -> None:
    header = colored("File areas:", fg_color=HEADER_COLOR, bold=True)
    options = "  ".join(
        [menu_key("C", "reate"), menu_key("L", "ist"), menu_key("G", "C storage"), menu_key("B", "ack")]
    )
    await session.write_line(f"\r\n{header} {options}")
    await session.write("Choice: ")


async def _gc_screen(session: Session, db: Database) -> None:
    """
    Reference-aware blob garbage collection (GitHub issue #35): always
    shows a dry-run report first, then asks separately before actually
    reclaiming anything -- the same "preview, then explicit confirm"
    shape delete confirmations elsewhere in this menu use, appropriate
    here too since this is a one-way filesystem operation the database
    itself can't undo.
    """
    preview = reclaim_orphaned_blobs(db, dry_run=True)
    await _write_gc_report(session, preview)
    if preview.reclaimable_blobs == 0:
        return
    await session.write("Reclaim this space now? [y/N]: ")
    answer = (await session.read_key()).lower()
    await session.write_line("")
    if answer != "y":
        return
    result = reclaim_orphaned_blobs(db, dry_run=False)
    await _write_gc_report(session, result)


def _format_bytes(size_bytes: int) -> str:
    """Human-readable byte count, binary (KiB/MiB/GiB) units -- a small
    local formatter rather than reaching into
    `netbbs.net.file_flow`'s own private `_format_size`, which exists
    for that module's file-listing display specifically."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    size = float(size_bytes) / 1024
    for unit in ("KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"  # unreachable, satisfies type checkers


async def _write_gc_report(session: Session, report: GCReport) -> None:
    verb = "Would reclaim" if report.dry_run else "Reclaimed"
    await session.write_line(
        f"\r\n{verb} {report.reclaimable_blobs} orphaned blob(s), "
        f"{_format_bytes(report.reclaimable_bytes)}."
    )
    if report.skipped_recent:
        await session.write_line(
            colored(
                f"{report.skipped_recent} recently-written orphan(s) skipped this pass "
                "(safety age not yet reached).",
                fg_color=MUTED_COLOR,
            )
        )
    for error in report.errors:
        await session.write_line(colored(f"Error: {error}", fg_color=MUTED_COLOR))


async def _create_area_screen(session: Session, db: Database, actor: User) -> None:
    await session.write_line(colored("\r\nCreate file area", fg_color=HEADER_COLOR, bold=True))
    await session.write("Name: ")
    name = (await session.read_line()).strip()
    if not name:
        await session.write_line(colored("Cancelled: name cannot be blank.", fg_color=MUTED_COLOR))
        return
    await session.write("Description (optional): ")
    description = (await session.read_line()).strip() or None
    await session.write("Minimum read level [0]: ")
    min_read_level = await _read_int(session, default=0)
    if min_read_level is None:
        return
    await session.write("Minimum write level [0]: ")
    min_write_level = await _read_int(session, default=0)
    if min_write_level is None:
        return
    category_id = await _pick_optional_category(
        session, db, list_top_level=list_top_level_file_categories,
        list_subcategories=list_file_subcategories, title="File-area category",
    )
    await session.write("Pinned? [y/N]: ")
    pinned = (await session.read_key()).lower() == "y"
    await session.write_line("")
    await session.write("Moderated (uploads need approval)? [y/N]: ")
    moderated = (await session.read_key()).lower() == "y"
    await session.write_line("")
    await session.write("Max file age in days (blank = unlimited): ")
    max_age_raw = (await session.read_line()).strip()
    max_file_age_days = None
    if max_age_raw:
        try:
            max_file_age_days = int(max_age_raw)
        except ValueError:
            await session.write_line(colored("Not a number -- cancelled.", fg_color=MUTED_COLOR))
            return

    try:
        area = create_file_area(
            db, name, description=description, min_read_level=min_read_level,
            min_write_level=min_write_level, category_id=category_id, pinned=pinned,
            moderated=moderated, max_file_age_days=max_file_age_days, creator=actor,
        )
    except FileAreaError as exc:
        await session.write_line(colored(f"Could not create file area: {exc}", fg_color=MUTED_COLOR))
        return
    await session.write_line(f"Created file area {area.name!r}.")


async def _list_areas_screen(session: Session, db: Database, actor: User) -> None:
    areas = list_file_areas(db, order_by="alphabetical")
    selected = await pick_item(
        session, areas,
        name_of=lambda a: a.name,
        stable_id_of=lambda a: a.id,
        description_of=_area_description,
        title="File areas",
        empty_message="No file areas yet.",
    )
    if selected is not None:
        await _area_detail_screen(session, db, actor, selected)


def _area_description(area: FileArea) -> str:
    status = "moderated" if area.moderated else "open"
    return f"read {area.min_read_level}/write {area.min_write_level}, {status}"


async def _area_detail_screen(session: Session, db: Database, actor: User, area: FileArea) -> None:
    await _draw_area_detail(session, area)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "e":
            await session.write_line("")
            updated = await _edit_area_screen(session, db, actor, area)
            if updated is not None:
                area = updated
            await _draw_area_detail(session, area)
        elif choice == "d":
            await session.write_line("")
            deleted = await _delete_area_screen(session, db, actor, area)
            if deleted:
                return
            await _draw_area_detail(session, area)
        elif choice == "p":
            await session.write_line("")
            await _pending_files_screen(session, db, actor, area)
            await _draw_area_detail(session, area)
        else:
            await session.write(reject_keystroke())


async def _draw_area_detail(session: Session, area: FileArea) -> None:
    header = colored(sanitize_text(area.name), fg_color=HEADER_COLOR, bold=True)
    await session.write_line(f"\r\n{header}")
    await session.write_line(f"Description: {sanitize_text(area.description) if area.description else '(none)'}")
    await session.write_line(f"Read level: {area.min_read_level}  Write level: {area.min_write_level}")
    await session.write_line(
        f"Pinned: {'yes' if area.pinned else 'no'}  Moderated: {'yes' if area.moderated else 'no'}"
    )
    age = area.max_file_age_days if area.max_file_age_days is not None else "unlimited"
    await session.write_line(f"Max file age: {age} days")
    options = "  ".join(
        [menu_key("E", "dit"), menu_key("D", "elete"), menu_key("P", "ending files"), menu_key("B", "ack")]
    )
    await session.write_line(f"\r\n{options}")
    await session.write("Choice: ")


async def _edit_area_screen(session: Session, db: Database, actor: User, area: FileArea) -> FileArea | None:
    await session.write(f"Name [{area.name}]: ")
    name = (await session.read_line()).strip() or area.name
    await session.write(f"Description [{area.description or '(none)'}]: ")
    description = (await session.read_line()).strip() or area.description
    await session.write(f"Minimum read level [{area.min_read_level}]: ")
    min_read_level = await _read_int(session, default=area.min_read_level)
    if min_read_level is None:
        return None
    await session.write(f"Minimum write level [{area.min_write_level}]: ")
    min_write_level = await _read_int(session, default=area.min_write_level)
    if min_write_level is None:
        return None
    await session.write("Change category? [y/N]: ")
    change_category = (await session.read_key()).lower() == "y"
    await session.write_line("")
    category_id = area.category_id
    if change_category:
        category_id = await _pick_optional_category(
            session, db, list_top_level=list_top_level_file_categories,
            list_subcategories=list_file_subcategories, title="File-area category",
        )
    await session.write(f"Pinned? [{'y' if area.pinned else 'N'}]: ")
    pinned_answer = (await session.read_key()).lower()
    await session.write_line("")
    pinned = pinned_answer == "y" if pinned_answer in ("y", "n") else area.pinned
    await session.write(f"Moderated? [{'y' if area.moderated else 'N'}]: ")
    moderated_answer = (await session.read_key()).lower()
    await session.write_line("")
    moderated = moderated_answer == "y" if moderated_answer in ("y", "n") else area.moderated
    current_age = area.max_file_age_days if area.max_file_age_days is not None else "unlimited"
    await session.write(f"Max file age in days [{current_age}] (blank = keep, 'none' = unlimited): ")
    max_age_raw = (await session.read_line()).strip()
    max_file_age_days = area.max_file_age_days
    if max_age_raw.lower() == "none":
        max_file_age_days = None
    elif max_age_raw:
        try:
            max_file_age_days = int(max_age_raw)
        except ValueError:
            await session.write_line(colored("Not a number -- cancelled.", fg_color=MUTED_COLOR))
            return None

    try:
        updated = update_file_area(
            db, area, name=name, description=description, min_read_level=min_read_level,
            min_write_level=min_write_level, category_id=category_id, pinned=pinned,
            moderated=moderated, max_file_age_days=max_file_age_days, changed_by=actor,
        )
    except FileAreaError as exc:
        await session.write_line(colored(f"Could not update file area: {exc}", fg_color=MUTED_COLOR))
        return None
    await session.write_line(f"Updated {updated.name!r}.")
    return updated


async def _delete_area_screen(session: Session, db: Database, actor: User, area: FileArea) -> bool:
    await session.write_line(
        colored(
            "\r\nThis permanently deletes the file area, all of its files, and any "
            "moderator grants scoped to it. This cannot be undone.",
            fg_color=MUTED_COLOR,
        )
    )
    await session.write(f"Type the area name {area.name!r} to confirm, or anything else to cancel: ")
    confirmation = (await session.read_line()).strip()
    if confirmation != area.name:
        await session.write_line("Cancelled.")
        return False
    delete_file_area(db, area, deleted_by=actor)
    await session.write_line(f"{area.name!r} deleted.")
    return True


async def _pending_files_screen(session: Session, db: Database, actor: User, area: FileArea) -> None:
    while True:
        files = list_pending_files(db, area, requesting_user=actor)
        selected = await pick_item(
            session, files,
            name_of=lambda f: f.filename,
            stable_id_of=lambda f: f.id,
            description_of=lambda f: f"by {f.uploader_label}",
            title=f"Pending files in {area.name!r}",
            empty_message="No pending files.",
        )
        if selected is None:
            return
        await _file_action_screen(session, db, actor, selected)


async def _draw_file_action(session: Session, entry: FileEntry) -> None:
    header = colored(sanitize_text(entry.filename), fg_color=HEADER_COLOR, bold=True)
    await session.write_line(f"\r\n{header}")
    await session.write_line(f"By: {sanitize_text(entry.uploader_label)}")
    if entry.description:
        await session.write_line(sanitize_text(entry.description))
    await session.write_line(f"Size: {entry.size_bytes} bytes")
    options = "  ".join(
        [
            menu_key("A", "pprove"),
            menu_key("R", "eject"),
            menu_key("P", "in toggle"),
            menu_key("X", "empt toggle"),
            menu_key("B", "ack"),
        ]
    )
    await session.write_line(f"\r\n{options}")
    await session.write("Choice: ")


async def _file_action_screen(session: Session, db: Database, actor: User, entry: FileEntry) -> None:
    await _draw_file_action(session, entry)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "a":
            await session.write_line("")
            approve_file(db, entry, approved_by=actor)
            await session.write_line("Approved.")
            return
        elif choice == "r":
            await session.write_line("")
            delete_file(db, entry, deleted_by=actor)
            await session.write_line("Rejected.")
            return
        elif choice == "p":
            await session.write_line("")
            entry = set_file_pinned(db, entry, not entry.pinned, changed_by=actor)
            await _draw_file_action(session, entry)
        elif choice == "x":
            await session.write_line("")
            entry = set_file_exempt(db, entry, not entry.exempt_from_expiry, changed_by=actor)
            await _draw_file_action(session, entry)
        else:
            await session.write(reject_keystroke())


# -- channels (design doc -- channel management round) --------------------
#
# Mirrors the board/area sections above, structurally, but with no
# pending-queue equivalent: channels have no moderated-content/approval
# workflow the way boards/file areas do (see netbbs.chat.channels'
# module docstring — chat messages aren't even persisted beyond bounded
# scrollback). Membership admin (invite/kick/mute/ban) is also
# deliberately not duplicated here — it's already fully reachable via
# in-chat commands (/invite, /kick, /mute, /ban, /members) for anyone
# holding the relevant ChannelPermission grant, so there's no existing-
# but-UI-less gap to close for it the way there was for post/file
# approval.


async def _channel_menu(session: Session, db: Database, actor: User) -> None:
    await _draw_channel_menu(session)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "c":
            await session.write_line("")
            await _create_channel_screen(session, db, actor)
            await _draw_channel_menu(session)
        elif choice == "l":
            await session.write_line("")
            await _list_channels_screen(session, db, actor)
            await _draw_channel_menu(session)
        else:
            await session.write(reject_keystroke())


async def _draw_channel_menu(session: Session) -> None:
    header = colored("Channels:", fg_color=HEADER_COLOR, bold=True)
    options = "  ".join([menu_key("C", "reate"), menu_key("L", "ist"), menu_key("B", "ack")])
    await session.write_line(f"\r\n{header} {options}")
    await session.write("Choice: ")


async def _create_channel_screen(session: Session, db: Database, actor: User) -> None:
    await session.write_line(colored("\r\nCreate channel", fg_color=HEADER_COLOR, bold=True))
    await session.write("Name: ")
    name = (await session.read_line()).strip()
    if not name:
        await session.write_line(colored("Cancelled: name cannot be blank.", fg_color=MUTED_COLOR))
        return
    await session.write("Description (optional): ")
    description = (await session.read_line()).strip() or None
    await session.write("Minimum level [0]: ")
    min_level = await _read_int(session, default=0)
    if min_level is None:
        return
    category_id = await _pick_optional_category(
        session, db, list_top_level=list_top_level_channel_categories,
        list_subcategories=list_channel_subcategories, title="Channel category",
    )
    await session.write("Pinned? [y/N]: ")
    pinned = (await session.read_key()).lower() == "y"
    await session.write_line("")
    await session.write("Hidden (omitted from listings)? [y/N]: ")
    hidden = (await session.read_key()).lower() == "y"
    await session.write_line("")
    await session.write("Members-only (invite-only access)? [y/N]: ")
    members_only = (await session.read_key()).lower() == "y"
    await session.write_line("")
    allow_member_invites = False
    if members_only:
        await session.write("Allow members to invite others? [y/N]: ")
        allow_member_invites = (await session.read_key()).lower() == "y"
        await session.write_line("")

    try:
        channel = create_channel(
            db, name, description=description, min_level=min_level, category_id=category_id,
            pinned=pinned, hidden=hidden, members_only=members_only,
            allow_member_invites=allow_member_invites, creator=actor,
        )
    except ChannelError as exc:
        await session.write_line(colored(f"Could not create channel: {exc}", fg_color=MUTED_COLOR))
        return
    await session.write_line(f"Created channel {channel.name!r}.")


async def _list_channels_screen(session: Session, db: Database, actor: User) -> None:
    channels = list_channels(db)
    selected = await pick_item(
        session, channels,
        name_of=lambda c: c.name,
        stable_id_of=lambda c: c.id,
        description_of=_channel_description,
        title="Channels",
        empty_message="No channels yet.",
    )
    if selected is not None:
        await _channel_detail_screen(session, db, actor, selected)


def _channel_description(channel: Channel) -> str:
    bits = [f"level {channel.min_level}"]
    if channel.members_only:
        bits.append("members-only")
    if channel.hidden:
        bits.append("hidden")
    return ", ".join(bits)


async def _channel_detail_screen(session: Session, db: Database, actor: User, channel: Channel) -> None:
    await _draw_channel_detail(session, channel)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "e":
            await session.write_line("")
            updated = await _edit_channel_screen(session, db, actor, channel)
            if updated is not None:
                channel = updated
            await _draw_channel_detail(session, channel)
        elif choice == "d":
            await session.write_line("")
            deleted = await _delete_channel_screen(session, db, actor, channel)
            if deleted:
                return
            await _draw_channel_detail(session, channel)
        else:
            await session.write(reject_keystroke())


async def _draw_channel_detail(session: Session, channel: Channel) -> None:
    header = colored(sanitize_text(channel.name), fg_color=HEADER_COLOR, bold=True)
    await session.write_line(f"\r\n{header}")
    await session.write_line(
        f"Description: {sanitize_text(channel.description) if channel.description else '(none)'}"
    )
    await session.write_line(f"Minimum level: {channel.min_level}")
    await session.write_line(
        f"Pinned: {'yes' if channel.pinned else 'no'}  Hidden: {'yes' if channel.hidden else 'no'}"
    )
    await session.write_line(
        f"Members-only: {'yes' if channel.members_only else 'no'}  "
        f"Allow member invites: {'yes' if channel.allow_member_invites else 'no'}"
    )
    options = "  ".join([menu_key("E", "dit"), menu_key("D", "elete"), menu_key("B", "ack")])
    await session.write_line(f"\r\n{options}")
    await session.write("Choice: ")


async def _edit_channel_screen(session: Session, db: Database, actor: User, channel: Channel) -> Channel | None:
    await session.write(f"Name [{channel.name}]: ")
    name = (await session.read_line()).strip() or channel.name
    await session.write(f"Description [{channel.description or '(none)'}]: ")
    description = (await session.read_line()).strip() or channel.description
    await session.write(f"Minimum level [{channel.min_level}]: ")
    min_level = await _read_int(session, default=channel.min_level)
    if min_level is None:
        return None
    await session.write("Change category? [y/N]: ")
    change_category = (await session.read_key()).lower() == "y"
    await session.write_line("")
    category_id = channel.category_id
    if change_category:
        category_id = await _pick_optional_category(
            session, db, list_top_level=list_top_level_channel_categories,
            list_subcategories=list_channel_subcategories, title="Channel category",
        )
    await session.write(f"Pinned? [{'y' if channel.pinned else 'N'}]: ")
    pinned_answer = (await session.read_key()).lower()
    await session.write_line("")
    pinned = pinned_answer == "y" if pinned_answer in ("y", "n") else channel.pinned
    await session.write(f"Hidden? [{'y' if channel.hidden else 'N'}]: ")
    hidden_answer = (await session.read_key()).lower()
    await session.write_line("")
    hidden = hidden_answer == "y" if hidden_answer in ("y", "n") else channel.hidden
    await session.write(f"Members-only? [{'y' if channel.members_only else 'N'}]: ")
    members_answer = (await session.read_key()).lower()
    await session.write_line("")
    members_only = members_answer == "y" if members_answer in ("y", "n") else channel.members_only
    await session.write(f"Allow member invites? [{'y' if channel.allow_member_invites else 'N'}]: ")
    invites_answer = (await session.read_key()).lower()
    await session.write_line("")
    allow_member_invites = (
        invites_answer == "y" if invites_answer in ("y", "n") else channel.allow_member_invites
    )

    try:
        updated = update_channel(
            db, channel, name=name, description=description, min_level=min_level,
            category_id=category_id, pinned=pinned, hidden=hidden, members_only=members_only,
            allow_member_invites=allow_member_invites, changed_by=actor,
        )
    except ChannelError as exc:
        await session.write_line(colored(f"Could not update channel: {exc}", fg_color=MUTED_COLOR))
        return None
    await session.write_line(f"Updated {updated.name!r}.")
    return updated


async def _delete_channel_screen(session: Session, db: Database, actor: User, channel: Channel) -> bool:
    await session.write_line(
        colored(
            "\r\nThis permanently deletes the channel, its scrollback, mute/ban "
            "restrictions, membership/invitations, and any moderator grants "
            "scoped to it. This cannot be undone.",
            fg_color=MUTED_COLOR,
        )
    )
    await session.write(f"Type the channel name {channel.name!r} to confirm, or anything else to cancel: ")
    confirmation = (await session.read_line()).strip()
    if confirmation != channel.name:
        await session.write_line("Cancelled.")
        return False
    delete_channel(db, channel, deleted_by=actor)
    await session.write_line(f"{channel.name!r} deleted.")
    return True


# -- categories ----------------------------------------------------------


async def _category_menu(session: Session, db: Database, actor: User) -> None:
    await _draw_category_menu(session)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "m":
            await session.write_line("")
            await _generic_category_screen(
                session, db, actor,
                create=create_board_category, list_top_level=list_top_level_board_categories,
                list_subcategories=list_board_subcategories, delete=delete_board_category,
                error_type=CategoryError, title="Board categories",
            )
            await _draw_category_menu(session)
        elif choice == "f":
            await session.write_line("")
            await _generic_category_screen(
                session, db, actor,
                create=create_file_category, list_top_level=list_top_level_file_categories,
                list_subcategories=list_file_subcategories, delete=delete_file_category,
                error_type=FileCategoryError, title="File-area categories",
            )
            await _draw_category_menu(session)
        elif choice == "h":
            await session.write_line("")
            await _generic_category_screen(
                session, db, actor,
                create=create_channel_category, list_top_level=list_top_level_channel_categories,
                list_subcategories=list_channel_subcategories, delete=delete_channel_category,
                error_type=ChannelCategoryError, title="Channel categories",
            )
            await _draw_category_menu(session)
        else:
            await session.write(reject_keystroke())


async def _draw_category_menu(session: Session) -> None:
    header = colored("Categories:", fg_color=HEADER_COLOR, bold=True)
    options = "  ".join(
        [
            menu_key("M", "essage board category"),
            menu_key("F", "ile-area category"),
            menu_key("H", "annel category"),
            menu_key("B", "ack"),
        ]
    )
    await session.write_line(f"\r\n{header} {options}")
    await session.write("Choice: ")


async def _generic_category_screen(
    session: Session, db: Database, actor: User, *, create, list_top_level, list_subcategories, delete,
    error_type, title: str,
) -> None:
    await _draw_generic_category_menu(session, title)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "c":
            await session.write_line("")
            await _create_category_screen(
                session, db, actor, create=create, list_top_level=list_top_level, error_type=error_type,
            )
            await _draw_generic_category_menu(session, title)
        elif choice == "l":
            await session.write_line("")
            await _list_categories_screen(
                session, db, actor, list_top_level=list_top_level,
                list_subcategories=list_subcategories, delete=delete,
            )
            await _draw_generic_category_menu(session, title)
        else:
            await session.write(reject_keystroke())


async def _draw_generic_category_menu(session: Session, title: str) -> None:
    header = colored(f"{title}:", fg_color=HEADER_COLOR, bold=True)
    options = "  ".join([menu_key("C", "reate"), menu_key("L", "ist/delete"), menu_key("B", "ack")])
    await session.write_line(f"\r\n{header} {options}")
    await session.write("Choice: ")


async def _create_category_screen(
    session: Session, db: Database, actor: User, *, create, list_top_level, error_type
) -> None:
    await session.write("Name: ")
    name = (await session.read_line()).strip()
    if not name:
        await session.write_line(colored("Cancelled: name cannot be blank.", fg_color=MUTED_COLOR))
        return
    await session.write("Description (optional): ")
    description = (await session.read_line()).strip() or None
    await session.write("Make this a sub-category of an existing one? [y/N]: ")
    answer = (await session.read_key()).lower()
    await session.write_line("")
    parent_category_id = None
    if answer == "y":
        parent = await pick_item(
            session, list_top_level(db),
            name_of=lambda c: c.name, stable_id_of=lambda c: c.id,
            title="Parent category", empty_message="No top-level categories exist yet.",
        )
        parent_category_id = parent.id if parent is not None else None
    try:
        category = create(
            db, name, description=description, parent_category_id=parent_category_id, created_by=actor
        )
    except error_type as exc:
        await session.write_line(colored(f"Could not create category: {exc}", fg_color=MUTED_COLOR))
        return
    await session.write_line(f"Created category {category.name!r}.")


async def _list_categories_screen(
    session: Session, db: Database, actor: User, *, list_top_level, list_subcategories, delete
) -> None:
    top_level = list_top_level(db)
    all_categories = list(top_level)
    for top in top_level:
        all_categories.extend(list_subcategories(db, top.id))
    selected = await pick_item(
        session, all_categories,
        name_of=lambda c: c.name,
        stable_id_of=lambda c: c.id,
        description_of=lambda c: "top-level" if c.is_top_level else "sub-category",
        title="Categories",
        empty_message="No categories yet.",
    )
    if selected is None:
        return
    await session.write_line(
        colored(
            "\r\nDeleting this category sets any boards/areas/channels assigned "
            "to it (and any of its own sub-categories) back to uncategorized.",
            fg_color=MUTED_COLOR,
        )
    )
    await session.write(f"Type the category name {selected.name!r} to confirm deletion, or anything else to cancel: ")
    confirmation = (await session.read_line()).strip()
    if confirmation != selected.name:
        await session.write_line("Cancelled.")
        return
    delete(db, selected, deleted_by=actor)
    await session.write_line(f"{selected.name!r} deleted.")


# -- moderator grants -----------------------------------------------------


async def _pick_moderator_scope(session: Session, db: Database) -> tuple[str, int | None, str] | None:
    """Returns `(object_type, object_id, human label)`, or `None` if
    cancelled. `object_id=None` means a local-blanket grant (design doc
    -- board/area management round; channel scope added in the channel
    management round)."""
    await session.write(
        "Scope: [B]oard, [A]rea, [H]annel, blanket across all boards [X], "
        "blanket across all areas [Y], blanket across all channels [Z]: "
    )
    scope_key = (await session.read_key()).lower()
    await session.write_line("")
    if scope_key == "b":
        board = await pick_item(
            session, list_boards(db, order_by="alphabetical"),
            name_of=lambda b: b.name, stable_id_of=lambda b: b.id,
            title="Which board?", empty_message="No boards yet.",
        )
        if board is None:
            return None
        return "board", board.id, f"board {board.name!r}"
    elif scope_key == "a":
        area = await pick_item(
            session, list_file_areas(db, order_by="alphabetical"),
            name_of=lambda a: a.name, stable_id_of=lambda a: a.id,
            title="Which file area?", empty_message="No file areas yet.",
        )
        if area is None:
            return None
        return "file_area", area.id, f"file area {area.name!r}"
    elif scope_key == "h":
        channel = await pick_item(
            session, list_channels(db),
            name_of=lambda c: c.name, stable_id_of=lambda c: c.id,
            title="Which channel?", empty_message="No channels yet.",
        )
        if channel is None:
            return None
        return "channel", channel.id, f"channel {channel.name!r}"
    elif scope_key == "x":
        return "board", None, "all boards (blanket)"
    elif scope_key == "y":
        return "file_area", None, "all file areas (blanket)"
    elif scope_key == "z":
        return "channel", None, "all channels (blanket)"
    else:
        await session.write_line(colored("Not a valid scope -- cancelled.", fg_color=MUTED_COLOR))
        return None


async def _grant_moderator_screen(session: Session, db: Database, actor: User) -> None:
    target = await pick_item(
        session, list_users(db),
        name_of=lambda u: u.username, stable_id_of=lambda u: u.id,
        title="Grant moderator to which user?", empty_message="No registered users yet.",
    )
    if target is None:
        return
    scope = await _pick_moderator_scope(session, db)
    if scope is None:
        return
    object_type, object_id, label = scope

    if object_type == "channel":
        await session.write("Preset: [F]ull moderator (edit+moderate+manage members), [M]oderator only: ")
        preset_key = (await session.read_key()).lower()
        await session.write_line("")
        if preset_key == "f":
            permissions = ChannelPermission.EDIT | ChannelPermission.MODERATE | ChannelPermission.MANAGE_MEMBERS
            preset_label = "Full moderator"
        elif preset_key == "m":
            permissions = ChannelPermission.MODERATE
            preset_label = "Moderator only"
        else:
            await session.write_line(colored("Not a valid preset -- cancelled.", fg_color=MUTED_COLOR))
            return
    else:
        await session.write("Preset: [F]ull moderator (edit+delete+approve), [A]pprover only: ")
        preset_key = (await session.read_key()).lower()
        await session.write_line("")
        if preset_key == "f":
            permissions = BoardPermission.EDIT | BoardPermission.DELETE | BoardPermission.APPROVE
            preset_label = "Full moderator"
        elif preset_key == "a":
            permissions = BoardPermission.APPROVE
            preset_label = "Approver only"
        else:
            await session.write_line(colored("Not a valid preset -- cancelled.", fg_color=MUTED_COLOR))
            return

    await session.write(f"Grant {preset_label!r} on {label} to {target.username!r}? [y/N]: ")
    confirm = (await session.read_key()).lower()
    await session.write_line("")
    if confirm != "y":
        await session.write_line("Cancelled.")
        return

    grant_permissions(
        db, target, object_type=object_type, object_id=object_id, permissions=permissions, granted_by=actor
    )
    await session.write_line(f"Granted {preset_label} on {label} to {target.username!r}.")


async def _revoke_moderator_screen(session: Session, db: Database, actor: User) -> None:
    target = await pick_item(
        session, list_users(db),
        name_of=lambda u: u.username, stable_id_of=lambda u: u.id,
        title="Revoke moderator from which user?", empty_message="No registered users yet.",
    )
    if target is None:
        return
    scope = await _pick_moderator_scope(session, db)
    if scope is None:
        return
    object_type, object_id, label = scope

    grant = get_grant(db, target, object_type=object_type, object_id=object_id)
    if grant is None:
        await session.write_line(colored(f"{target.username!r} has no grant on {label}.", fg_color=MUTED_COLOR))
        return

    await session.write(f"Revoke all permissions for {target.username!r} on {label}? [y/N]: ")
    confirm = (await session.read_key()).lower()
    await session.write_line("")
    if confirm != "y":
        await session.write_line("Cancelled.")
        return

    permission_enum = ChannelPermission if object_type == "channel" else BoardPermission
    revoke_permissions(
        db, target, object_type=object_type, object_id=object_id,
        permissions=permission_enum(grant.permissions), revoked_by=actor,
    )
    await session.write_line(f"Revoked {target.username!r}'s grant on {label}.")
