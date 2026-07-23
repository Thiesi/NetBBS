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

Fourth and final file migrated onto design doc round 91's two-lane
database execution model (issue #57/round 115) -- every screen/menu
function here now takes `lane: DatabaseLane` instead of `db: Database`,
and every direct domain-function call goes through `await lane.run(...)`.
Almost entirely mechanical, unlike chat_flow.py's round 114 (no
synchronous-callback contracts here the way the Tab completer was) --
`pick_item`'s own `name_of`/`description_of` callbacks in this file only
ever read attributes already present on the objects handed to
`pick_item`, never a fresh DB call, so no eager-pre-fetch restructuring
was needed for any of them except one: `_who_screen`'s
`description_of` used to call `format_for_display(entry.connected_at,
db)` directly inside its lambda, which cannot dispatch through a lane
from inside a synchronous callback -- fixed the same way round 112
fixed this same shape (`resolve_display_preferences`, fetched once via
`lane.run` before the picker, then passed as `override_format`/
`override_timezone` into a plain synchronous `format_for_display`
call). `_community_label` stays `db`-first, dispatched *through* the
lane like `netbbs.net.file_flow`'s `_uploader_display_name` before it
-- it's a callee, never a caller, of the lane.

Both entry points that share this module now need a real
`DatabaseLane`: the in-BBS `[S]ysOp` menu option
(`netbbs.net.login_flow`, same `lane is None` degrade-gracefully guard
as the mail/files/chat branches before it) and the standalone
`python -m netbbs.admin` CLI (`netbbs.admin.__main__`), which
constructs its own `DatabaseLane` around its own `Database` handle --
there is no live-session/CLI distinction for admin functionality
itself once inside `admin_menu` (that's `node_controls`' job, already
established before this round).
"""

from __future__ import annotations

import asyncio

import nacl.signing

from netbbs.auth.users import (
    AuthError,
    User,
    UserManagementError,
    approve_pending_user,
    create_user,
    delete_user,
    get_user_by_username,
    list_users,
    set_can_verify_identity,
    set_user_disabled,
    set_user_level,
)
from netbbs.backup import get_last_backup_summary
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
from netbbs.communities import (
    Community,
    CommunityError,
    create_community,
    delete_community,
    get_community,
    list_communities,
    update_community,
)
from netbbs.config import RegistrationMode, get_registration_mode, set_registration_mode
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
from netbbs.link.boards import (
    LinkBoardsError,
    LinkContext,
    accept_board_origin_transfer,
    board_origin_fingerprint,
    is_board_linked,
    is_board_origin_orphaned,
    link_board,
    offer_board_origin_transfer,
    queue_board_post_if_linked,
)
from netbbs.link.protocol import PeerRecord
from netbbs.link.relay_mailbox import mailbox_sizes
from netbbs.link.reliability import reliability_score
from netbbs.link.seedlist import get_cached_supplementary_seeds
from netbbs.link.store import load_peer_last_contact
from netbbs.moderation.log import list_actions_for_target_user, record_action
from netbbs.moderation.roles import (
    BoardPermission,
    ChannelPermission,
    get_grant,
    grant_permissions,
    list_grants_for_community,
    revoke_permissions,
)
from netbbs.net.confirm import prompt_yes_no, prompt_yes_no_or_keep
from netbbs.net.picker import pick_item
from netbbs.net.session import Session
from netbbs.net.session_registry import SessionSummary
from netbbs.net.shutdown import NodeControls, run_shutdown_sequence
from netbbs.selfupdate import (
    UpdateError,
    check_latest_release,
    get_auto_update_check_enabled,
    get_last_check_summary,
    is_newer,
    record_check_outcome,
    set_auto_update_check_enabled,
)
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
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import (
    format_for_display,
    resolve_display_preferences,
    set_display_format,
    set_display_timezone,
)


async def admin_menu(
    session: Session,
    lane: DatabaseLane,
    user: User,
    *,
    node_controls: NodeControls | None = None,
    link_context: LinkContext | None = None,
) -> None:
    """
    Top-level SysOp admin menu. Callers are responsible for their own
    level gating before entering this -- it performs no permission
    check of its own, matching `pick_item`'s "presentation and
    selection only" precedent.

    `node_controls` (design doc -- node management round), if given,
    unlocks the `[N]ode` command nested inside the `[S]ystem` submenu
    (list/disconnect sessions, trigger shutdown) -- present when called
    from within a live session (`netbbs.net.login_flow`), absent
    (`None`) when called from the standalone `python -m netbbs.admin`
    CLI, which has no access to a running node's live in-memory state
    at all (confirmed design decision, not an oversight -- see that
    module's docstring).

    `link_context` (design doc round 124/128), if given, unlocks the
    `[L]ink this board` command inside the board-management screens --
    same presence/absence reasoning as `node_controls`: absent for the
    standalone CLI and for any node with Link disabled.

    Grouped into three submenus by what they act on (Thiesi's own
    observation that the previous flat 9-option layout mixed user-
    account actions and node-wide settings side by side with no
    grouping at all, "Welcome banner" sitting next to "Create user"):
    `[U]sers` (create/list/registration/promote/enable-disable/delete),
    `[M]anage content` (boards/areas/channels, already its own submenu
    since before this reorganization), and `[S]ystem` (welcome banner,
    update, and -- Thiesi's own call -- `[N]ode` nested one level
    further in, rather than sitting at this top level as its own
    sibling the way it used to).
    """
    await _draw_admin_menu(session)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "u":
            await session.write_line("")
            await _users_menu(session, lane, user, node_controls=node_controls)
            await _draw_admin_menu(session)
        elif choice == "m":
            await session.write_line("")
            await _content_menu(session, lane, user, link_context=link_context)
            await _draw_admin_menu(session)
        elif choice == "s":
            await session.write_line("")
            await _system_menu(session, lane, user, node_controls=node_controls, link_context=link_context)
            await _draw_admin_menu(session)
        else:
            await session.write(reject_keystroke())


async def _draw_admin_menu(session: Session) -> None:
    header = colored("SysOp admin menu:", fg_color=HEADER_COLOR, bold=True)
    options = "  ".join(
        [
            menu_key("U", "sers"),
            menu_key("M", "anage boards/areas/channels"),
            menu_key("S", "ystem"),
            menu_key("B", "ack"),
        ]
    )
    await session.write_line(f"\r\n{header} {options}")
    await session.write("Choice: ")


# -- users submenu ---------------------------------------------------------


async def _users_menu(
    session: Session, lane: DatabaseLane, actor: User, *, node_controls: NodeControls | None
) -> None:
    """Every user-account action, grouped together (design doc -- admin
    menu reorganization round): create, list/detail, registration
    policy, promote/demote, enable/disable, delete. `node_controls` is
    threaded straight through to the screens that need it
    (`_disable_enable_screen`/`_delete_user_screen`, for the live-
    session-revocation guard) -- this submenu itself doesn't use it
    directly."""
    await _draw_users_menu(session)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "c":
            await session.write_line("")
            await _create_user_screen(session, lane, actor)
            await _draw_users_menu(session)
        elif choice == "l":
            await session.write_line("")
            await _list_users_screen(session, lane, actor)
            await _draw_users_menu(session)
        elif choice == "r":
            await session.write_line("")
            await _registration_settings_screen(session, lane, actor)
            await _draw_users_menu(session)
        elif choice == "p":
            await session.write_line("")
            await _change_level_screen(session, lane, actor)
            await _draw_users_menu(session)
        elif choice == "e":
            await session.write_line("")
            await _disable_enable_screen(session, lane, actor, node_controls)
            await _draw_users_menu(session)
        elif choice == "d":
            await session.write_line("")
            await _delete_user_screen(session, lane, actor, node_controls)
            await _draw_users_menu(session)
        else:
            await session.write(reject_keystroke())


async def _draw_users_menu(session: Session) -> None:
    header = colored("\r\nUsers:", fg_color=HEADER_COLOR, bold=True)
    options = "  ".join(
        [
            menu_key("C", "reate user"),
            menu_key("L", "ist users"),
            menu_key("R", "egistration"),
            menu_key("P", "romote/demote"),
            menu_key("E", "nable/disable"),
            menu_key("D", "elete user"),
            menu_key("B", "ack"),
        ]
    )
    await session.write_line(f"{header} {options}")
    await session.write("Choice: ")


# -- system submenu ----------------------------------------------------------


async def _system_menu(
    session: Session,
    lane: DatabaseLane,
    actor: User,
    *,
    node_controls: NodeControls | None,
    link_context: LinkContext | None = None,
) -> None:
    """Node-wide settings, grouped together (design doc -- admin menu
    reorganization round): welcome banner, self-update, and -- Thiesi's
    own call -- `[N]ode` (sessions/shutdown) nested here rather than
    sitting at the top level, since it's a node-wide concern too.

    `link_context` (issue #60), if given, unlocks `[L]ink status` --
    same presence/absence reasoning as `node_controls`: absent for the
    standalone CLI and for any node with Link disabled."""
    await _draw_system_menu(session, node_controls, link_context)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "w":
            await session.write_line("")
            await _welcome_banner_menu(session, lane, actor)
            await _draw_system_menu(session, node_controls, link_context)
        elif choice == "u":
            await session.write_line("")
            await _update_settings_screen(session, lane, actor)
            await _draw_system_menu(session, node_controls, link_context)
        elif choice == "n" and node_controls is not None:
            await session.write_line("")
            await _node_menu(session, lane, actor, node_controls)
            await _draw_system_menu(session, node_controls, link_context)
        elif choice == "t":
            await session.write_line("")
            await _timestamp_settings_screen(session, lane, actor)
            await _draw_system_menu(session, node_controls, link_context)
        elif choice == "l" and link_context is not None:
            await session.write_line("")
            await _link_status_screen(session, lane, actor, link_context=link_context)
            await _draw_system_menu(session, node_controls, link_context)
        elif choice == "k":
            await session.write_line("")
            await _backup_status_screen(session, lane, actor)
            await _draw_system_menu(session, node_controls, link_context)
        else:
            await session.write(reject_keystroke())


async def _draw_system_menu(
    session: Session, node_controls: NodeControls | None, link_context: LinkContext | None = None
) -> None:
    header = colored("\r\nSystem:", fg_color=HEADER_COLOR, bold=True)
    option_list = [
        menu_key("W", "elcome banner"), menu_key("U", "pdate"), menu_key("T", "imestamp format"),
        menu_key("K", "backup status"),
    ]
    if link_context is not None:
        option_list.append(menu_key("L", "ink status"))
    if node_controls is not None:
        option_list.append(menu_key("N", "ode"))
    option_list.append(menu_key("B", "ack"))
    options = "  ".join(option_list)
    await session.write_line(f"{header} {options}")
    await session.write("Choice: ")


# -- create ------------------------------------------------------------


async def _create_user_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
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
        # round 115: create_user, not create_user_async -- the latter's
        # off-loop hashing split existed specifically to keep Argon2
        # hashing off the *raw* event loop; lane.run() already dispatches
        # this whole call to a worker thread, so the plain synchronous
        # create_user (per its own docstring, "for command-line/admin
        # callers") does the hash and the write in one lane dispatch.
        new_user = await lane.run(
            create_user, username, password=password, verify_key=verify_key, user_level=level
        )
    except AuthError as exc:
        await session.write_line(colored(f"Could not create account: {exc}", fg_color=MUTED_COLOR))
        return

    await lane.run(
        record_action, actor=actor, action="create_user", target_user_id=new_user.id,
        detail=f"created user {new_user.username!r} at level {level}",
    )
    await session.write_line(f"Created {new_user.username!r} at level {level}.")


async def _prompt_optional_password(session: Session) -> str | None:
    if not await prompt_yes_no(session, "Set a password?", default=False):
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
    if not await prompt_yes_no(session, "Add a public key?", default=False):
        return None
    await session.write("Paste the public key (base64, or an ssh-ed25519 line): ")
    text = (await session.read_line()).strip()
    try:
        return parse_verify_key(text)
    except IdentityError as exc:
        await session.write_line(colored(f"Could not parse key: {exc} -- no key set.", fg_color=MUTED_COLOR))
        return None


# -- list / detail -------------------------------------------------------


async def _list_users_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    users = await lane.run(list_users)
    selected = await pick_item(
        session, users,
        name_of=lambda u: u.username,
        stable_id_of=lambda u: u.id,
        description_of=_user_description,
        title="Registered users",
        empty_message="No registered users yet.",
    )
    if selected is not None:
        await _show_user_detail(session, lane, actor, selected)


def _status_label(user: User) -> str:
    if user.disabled_at is not None:
        return "disabled"
    if user.pending_approval:
        return "pending approval"
    return "active"


def _user_description(user: User) -> str:
    return f"level {user.user_level}, {_status_label(user)}"


async def _show_user_detail(session: Session, lane: DatabaseLane, actor: User, target: User) -> None:
    header = colored(sanitize_text(target.username), fg_color=HEADER_COLOR, bold=True)
    await session.write_line(f"\r\n{header}")
    await session.write_line(f"Level: {target.user_level}")
    await session.write_line(f"Status: {_status_label(target)}")
    # round 115: display prefs fetched once, reused for the loop below
    # too (round 112's format_for_display-under-a-lane fix).
    display_format, display_timezone = await lane.run(resolve_display_preferences)
    member_since = format_for_display(
        target.created_at, override_format=display_format, override_timezone=display_timezone
    )
    await session.write_line(f"Member since: {member_since}")

    entries = await lane.run(list_actions_for_target_user, target.id)
    if not entries:
        await session.write_line(colored("No recorded admin actions.", fg_color=MUTED_COLOR))
    else:
        await session.write_line(colored("Recent admin actions:", fg_color=MUTED_COLOR))
        for entry in entries[-10:]:
            when = format_for_display(
                entry.created_at, override_format=display_format, override_timezone=display_timezone
            )
            detail = f" -- {sanitize_text(entry.detail)}" if entry.detail else ""
            await session.write_line(f"  {when}: {sanitize_text(entry.action)}{detail}")

    if target.pending_approval:
        if await prompt_yes_no(session, "\r\nApprove this account so it can log in?", default=False):
            updated = await lane.run(approve_pending_user, target, approved_by=actor)
            await session.write_line(f"{updated.username!r} approved.")

    # Design doc §18, round 85 point 6 / round 101: a narrow, SysOp-
    # grantable permission independent of the four moderator scope
    # tiers -- toggled here rather than a dedicated screen, same shape
    # as the pending-approval prompt just above.
    await session.write_line(
        f"\r\nCan verify identity (age/name attestation): "
        f"{'yes' if target.can_verify_identity else 'no'}"
    )
    new_state = "revoke" if target.can_verify_identity else "grant"
    if await prompt_yes_no(session, f"{new_state.capitalize()} identity-verification permission?", default=False):
        updated = await lane.run(set_can_verify_identity, target, not target.can_verify_identity, changed_by=actor)
        await session.write_line(
            f"{updated.username!r} can now verify identity: {'yes' if updated.can_verify_identity else 'no'}."
        )


# -- self-service registration settings (design doc round 76) -----------


_REGISTRATION_MODE_LABELS = {
    RegistrationMode.OPEN: "open (new accounts active immediately)",
    RegistrationMode.APPROVAL_REQUIRED: "approval required (SysOp must approve new accounts)",
    RegistrationMode.CLOSED: "closed (no public registration; SysOp-created accounts only)",
}


async def _registration_settings_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """
    Sets the node's `registration_mode` (design doc round 96) --
    open/approval_required/closed, replacing the earlier plain
    require-approval toggle -- and surfaces how many self-registered
    accounts are currently waiting on approval. Approving/rejecting any
    of them individually still happens via `[L]ist users` -> a pending
    account's own detail screen (`_show_user_detail`'s approve prompt),
    reusing the existing user-management flow rather than building a
    second, parallel pending-accounts queue UI.
    """
    def _load(db: Database) -> tuple[RegistrationMode, int]:
        return get_registration_mode(db), sum(1 for u in list_users(db) if u.pending_approval)

    current, pending_count = await lane.run(_load)

    header = colored("\r\nSelf-service registration:", fg_color=HEADER_COLOR, bold=True)
    await session.write_line(header)
    await session.write_line(f"Current mode: {_REGISTRATION_MODE_LABELS[current]}")
    if pending_count:
        await session.write_line(
            colored(
                f"{pending_count} account(s) awaiting approval -- see [L]ist users.",
                fg_color=MUTED_COLOR,
            )
        )

    await session.write_line(
        "\r\n"
        + menu_key("O", "pen")
        + "  "
        + menu_key("A", "pproval required")
        + "  "
        + menu_key("C", "losed")
        + "  "
        + menu_key("B", "ack (leave unchanged)")
    )
    await session.write("Choice: ")
    choice = (await session.read_key()).lower()
    await session.write_line("")

    new_mode = {"o": RegistrationMode.OPEN, "a": RegistrationMode.APPROVAL_REQUIRED, "c": RegistrationMode.CLOSED}.get(
        choice
    )
    if new_mode is None:
        return
    if new_mode == current:
        await session.write_line(colored("Already set to that mode.", fg_color=MUTED_COLOR))
        return

    def _apply(db: Database) -> None:
        set_registration_mode(db, new_mode)
        record_action(db, actor=actor, action="set_registration_mode", detail=f"mode={new_mode.value}")

    await lane.run(_apply)
    await session.write_line(f"Registration mode is now: {_REGISTRATION_MODE_LABELS[new_mode]}")


# -- self-update (design doc §17, round 82; round 95/96 implementation) --


async def _update_settings_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """
    Check-for-updates and the daily-automatic-check off switch (§17's
    "off switch: ... disables the daily automatic background check").

    Deliberately **check-only** in this screen: it reports whether a
    newer release exists and records the outcome (`netbbs.selfupdate.
    record_check_outcome`), but does not download/apply/restart. The
    graceful-drain-then-restart apply flow (§17) needs to coordinate
    with the live node process's own shutdown/re-exec sequence, which
    isn't wired up yet -- a deliberate scope cut for this
    implementation pass, not an oversight, so this screen doesn't
    promise automation that isn't safely built and tested yet.
    """
    from netbbs import __version__ as current_version

    def _load(db: Database) -> tuple[bool, str | None, str | None]:
        auto_enabled = get_auto_update_check_enabled(db)
        checked_at, outcome = get_last_check_summary(db)
        return auto_enabled, checked_at, outcome

    auto_enabled, checked_at, outcome = await lane.run(_load)

    header = colored("\r\nSelf-update:", fg_color=HEADER_COLOR, bold=True)
    await session.write_line(header)
    await session.write_line(f"Running version: {current_version}")
    await session.write_line(f"Daily automatic check: {'ON' if auto_enabled else 'off'}")
    if checked_at is not None:
        display_format, display_timezone = await lane.run(resolve_display_preferences)
        when = format_for_display(checked_at, override_format=display_format, override_timezone=display_timezone)
        await session.write_line(f"Last check: {when} -- {sanitize_text(outcome or '')}")
    else:
        await session.write_line(colored("No check has been run on this node yet.", fg_color=MUTED_COLOR))

    if await prompt_yes_no(session, "\r\nCheck for a new release now?", default=False):
        try:
            release = await check_latest_release()
        except UpdateError as exc:
            await session.write_line(colored(f"Could not check for updates: {exc}", fg_color=MUTED_COLOR))
        else:
            if is_newer(current_version, release.tag_name):
                await lane.run(record_check_outcome, f"newer release available: {release.tag_name}")
                await session.write_line(
                    f"A newer release is available: {release.tag_name} "
                    f"(published {release.published_at})."
                )
                await session.write_line(
                    colored(
                        "Automatic download/apply is not yet available from this "
                        "screen -- update manually for now.",
                        fg_color=MUTED_COLOR,
                    )
                )
            else:
                await lane.run(record_check_outcome, f"up to date ({current_version})")
                await session.write_line(f"Already up to date ({current_version}).")

    new_state = "off" if auto_enabled else "ON"
    if not await prompt_yes_no(session, f"\r\nTurn daily automatic check {new_state}?", default=False):
        return

    def _apply(db: Database) -> None:
        set_auto_update_check_enabled(db, not auto_enabled)
        record_action(db, actor=actor, action="set_auto_update_check", detail=f"enabled={not auto_enabled}")

    await lane.run(_apply)
    await session.write_line(f"Daily automatic check is now {'ON' if not auto_enabled else 'off'}.")


# -- backup status (design doc §13.4, issue #60's first operational slice) --


async def _backup_status_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """
    Read-only visibility into when this node was last backed up
    (`netbbs.backup.get_last_backup_summary`) -- there is deliberately
    no "back up now"/"restore" action here. Both are `python -m netbbs.
    backup {create,restore}`, a standalone, cron-schedulable CLI, not a
    live-session action (see that module's docstring for why: a backup
    needs to be triggerable by an external scheduler, not only by a
    SysOp who remembers to log in and press a key). Nothing here
    mutates anything, so `actor` is accepted only for signature
    consistency with this submenu's other screens, same as
    `_link_status_screen`.
    """
    checked_at, path = await lane.run(get_last_backup_summary)

    await session.write_line(colored("\r\nBackup status:", fg_color=HEADER_COLOR, bold=True))
    if checked_at is not None:
        display_format, display_timezone = await lane.run(resolve_display_preferences)
        when = format_for_display(checked_at, override_format=display_format, override_timezone=display_timezone)
        await session.write_line(f"Last backup: {when}")
        await session.write_line(f"Location: {sanitize_text(path or '')}")
    else:
        await session.write_line(colored("No backup has been taken on this node yet.", fg_color=MUTED_COLOR))
    await session.write_line(
        colored("Run 'python -m netbbs.backup create --to <path>' to create one.", fg_color=MUTED_COLOR)
    )


# -- node-wide display format/timezone -------------------------------------


async def _timestamp_settings_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """
    Node-wide display format/timezone (`netbbs.timeutil.set_display_
    format`/`set_display_timezone`) -- previously reachable only by
    calling those functions directly, with no UI wired to either one
    anywhere. That gap is exactly why the chat status line's own clock
    could read differently from the host system's: `display_timezone`
    just sat at its hardcoded UTC default forever, with no admin
    surface to change it (Thiesi's own report).

    Two independent settings, each with its own "show current, prompt
    for a new value, blank leaves it unchanged" turn -- format controls
    the *shape* of a displayed timestamp, timezone controls *which
    instant* it shows (see `format_for_display`'s own docstring for why
    getting one right without the other still leaves users looking at
    the wrong wall-clock time, just reshaped) -- so a SysOp who only
    wants to fix one doesn't have to re-enter the other unchanged.
    """
    fmt, tz_name = await lane.run(resolve_display_preferences)

    header = colored("\r\nTimestamp display:", fg_color=HEADER_COLOR, bold=True)
    await session.write_line(header)
    await session.write_line(f"Current format: {fmt!r}")
    await session.write_line(f"Current timezone: {tz_name}")

    await session.write(colored("\r\nNew format (blank to leave unchanged): ", fg_color=MUTED_COLOR))
    new_fmt = (await session.read_line()).strip()
    if new_fmt:
        def _apply_format(db: Database) -> None:
            set_display_format(db, new_fmt)
            record_action(db, actor=actor, action="set_display_format", detail=new_fmt)

        try:
            await lane.run(_apply_format)
        except ValueError as exc:
            await session.write_line(colored(str(exc), fg_color=MUTED_COLOR))
        else:
            await session.write_line(f"Display format is now: {new_fmt!r}")

    await session.write(colored("New timezone (blank to leave unchanged): ", fg_color=MUTED_COLOR))
    new_tz = (await session.read_line()).strip()
    if new_tz:
        def _apply_timezone(db: Database) -> None:
            set_display_timezone(db, new_tz)
            record_action(db, actor=actor, action="set_display_timezone", detail=new_tz)

        try:
            await lane.run(_apply_timezone)
        except ValueError as exc:
            await session.write_line(colored(str(exc), fg_color=MUTED_COLOR))
        else:
            await session.write_line(f"Display timezone is now: {new_tz}")


# -- Link status (issue #60, narrow scope) -----------------------------------


async def _link_status_screen(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext
) -> None:
    """
    Read-only SysOp visibility into this node's live NetBBS Link state
    (issue #60, deliberately narrow: just visibility into what already
    exists -- peers, relay activity, board/event counters -- not the
    backup/quota/retry-queue/dead-letter machinery #60 also calls for,
    which stays a future design task). Nothing here mutates anything,
    so unlike every other screen in this submenu there's no
    `record_action` call; `actor` is accepted only so this screen's
    signature matches its `_system_menu` siblings.

    `link_context.link_node`'s in-memory fields are read directly, no
    lane dispatch -- the same "in-memory, no I/O" shape `_who_screen`
    already uses for `node_controls.session_registry`. Reliability
    scores, per-peer last-contact, cached seed count, and mailbox sizes
    are separate, read-only lane-dispatched queries, since none of that
    is held in memory.
    """
    node = link_context.link_node
    config = link_context.link_config

    await session.write_line(colored("\r\nLink status:", fg_color=HEADER_COLOR, bold=True))
    await session.write_line(f"This node's fingerprint: {sanitize_text(link_context.node_identity.fingerprint)}")

    if config is not None:
        await session.write_line(f"Mode: {'outgoing-only' if config.outgoing_only else 'full peer'}")
        if not config.outgoing_only:
            address = (
                f"{config.advertised_host}:{config.advertised_port}"
                if config.advertised_host else "(not configured)"
            )
            await session.write_line(f"Advertised address: {sanitize_text(address)}")
        await session.write_line(
            f"Relay-serving: {'on' if config.relay_serving_enabled else 'off'} "
            f"({len(node.relaying_for)}/{config.max_relay_clients} slots in use)"
        )
        await session.write_line(f"Sync interval: {config.sync_interval_seconds:.0f}s")
        await session.write_line(f"Configured seeds: {len(config.seeds)}")
    else:
        await session.write_line(f"Relaying for: {len(node.relaying_for)} requester(s)")

    cached_seeds = await lane.run(get_cached_supplementary_seeds)
    await session.write_line(f"Cached supplementary seeds: {len(cached_seeds)}")

    await session.write_line(f"Linked boards: {len(node.boards)}")
    await session.write_line(f"Known events: {len(node.known_event_ids)}")
    await session.write_line(f"Post-edit chains: {len(node.post_edits)}")
    await session.write_line(f"Candidate (unverified) peers: {len(node.candidate_descriptors)}")
    await session.write_line(f"Relays serving this node: {len(node.relays_serving_me)}")
    await session.write_line(
        f"Outstanding relay-consent requests of this node's own: {len(node.pending_own_relay_requests)}"
    )

    mailbox_by_recipient = await lane.run(mailbox_sizes)
    if mailbox_by_recipient:
        held = sum(mailbox_by_recipient.values())
        await session.write_line(
            f"Relay mailbox: {held} envelope(s) held for {len(mailbox_by_recipient)} recipient(s)."
        )
    else:
        await session.write_line(colored("Relay mailbox: empty.", fg_color=MUTED_COLOR))

    if not node.peers:
        await session.write_line(colored("\r\nNo verified peers.", fg_color=MUTED_COLOR))
        return

    await session.write_line(f"\r\nVerified peers: {len(node.peers)}")

    def _load(db: Database) -> tuple[dict[str, float], dict[str, str]]:
        return (
            {fingerprint: reliability_score(db, fingerprint) for fingerprint in node.peers},
            load_peer_last_contact(db),
        )

    scores, last_contact = await lane.run(_load)
    display_format, display_timezone = await lane.run(resolve_display_preferences)

    def _peer_description(peer: PeerRecord) -> str:
        # Kept to a single short word -- this is squeezed onto one line
        # alongside the fingerprint (32+ chars) and pick_item's own
        # "(#<id>)" reference, then truncated to terminal width
        # (netbbs.net.picker.truncate); reliability and last-contact
        # both get their own full-width line in the post-selection
        # detail below instead, where truncation isn't a concern.
        return "outgoing-only" if peer.descriptor.payload.get("outgoing_only") else "full peer"

    selected = await pick_item(
        session, list(node.peers.values()),
        name_of=lambda peer: peer.fingerprint,
        stable_id_of=lambda peer: id(peer),  # in-memory only, same idiom _who_screen uses for sessions
        description_of=_peer_description,
        title="Verified peers",
        empty_message="No verified peers.",
    )
    if selected is None:
        return

    await session.write_line(f"Reliability: {scores.get(selected.fingerprint, 0.5):.2f}")
    when = last_contact.get(selected.fingerprint)
    last = (
        format_for_display(when, override_format=display_format, override_timezone=display_timezone)
        if when else "never"
    )
    await session.write_line(f"Last contact: {last}")

    # selected.descriptor's fields are peer-controlled -- sanitized here
    # since this is a plain session.write_line, outside pick_item's own
    # automatic name_of/description_of sanitization.
    addresses = selected.descriptor.payload.get("addresses") or []
    if addresses:
        rendered = ", ".join(
            sanitize_text(f"{a.get('protocol')}://{a.get('address')}:{a.get('port')}") for a in addresses
        )
        await session.write_line(f"Addresses: {rendered}")
    else:
        await session.write_line(colored("Addresses: none published (outgoing-only).", fg_color=MUTED_COLOR))

    relays = selected.descriptor.payload.get("relays") or []
    if relays:
        await session.write_line(f"Publishes {len(relays)} relay(s) in its own descriptor.")
    await session.write_line(
        f"Currently relaying for this node's requests: "
        f"{'yes' if selected.fingerprint in node.relaying_for else 'no'}"
    )
    await session.write_line(
        f"This node relays for it: {'yes' if selected.fingerprint in node.relays_serving_me else 'no'}"
    )


# -- promote/demote, enable/disable ---------------------------------------


async def _pick_target_user(session: Session, lane: DatabaseLane, *, title: str) -> User | None:
    users = await lane.run(list_users)
    return await pick_item(
        session, users,
        name_of=lambda u: u.username,
        stable_id_of=lambda u: u.id,
        description_of=_user_description,
        title=title,
        empty_message="No registered users yet.",
    )


async def _change_level_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    target = await _pick_target_user(session, lane, title="Promote/demote which user?")
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
        updated = await lane.run(set_user_level, target, new_level, changed_by=actor)
    except UserManagementError as exc:
        await session.write_line(colored(str(exc), fg_color=MUTED_COLOR))
        return
    await session.write_line(f"{updated.username!r} is now level {updated.user_level}.")


async def _disable_enable_screen(
    session: Session, lane: DatabaseLane, actor: User, node_controls: NodeControls | None = None
) -> None:
    target = await _pick_target_user(session, lane, title="Enable/disable which user?")
    if target is None:
        return
    currently_disabled = target.disabled_at is not None
    action_word = "Enable" if currently_disabled else "Disable"
    if not await prompt_yes_no(session, f"{action_word} {target.username!r}?", default=False):
        return
    try:
        updated = await lane.run(set_user_disabled, target, not currently_disabled, changed_by=actor)
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
    session: Session, lane: DatabaseLane, actor: User, node_controls: NodeControls | None = None
) -> None:
    target = await _pick_target_user(session, lane, title="Delete which user?")
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
        await lane.run(delete_user, target, deleted_by=actor)
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


async def _node_menu(session: Session, lane: DatabaseLane, actor: User, node_controls: NodeControls) -> None:
    await _draw_node_menu(session)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "w":
            await session.write_line("")
            await _who_screen(session, lane, actor, node_controls)
            await _draw_node_menu(session)
        elif choice == "s":
            await session.write_line("")
            await _shutdown_screen(session, lane, actor, node_controls)
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


def _session_description(entry: SessionSummary, display_format: str, display_timezone: str) -> str:
    when = format_for_display(entry.connected_at, override_format=display_format, override_timezone=display_timezone)
    return f"connected since {when}"


async def _who_screen(session: Session, lane: DatabaseLane, actor: User, node_controls: NodeControls) -> None:
    entries = node_controls.session_registry.list_entries()
    # round 115: description_of runs synchronously inside pick_item, so
    # the display-preference lookup _session_description needs is
    # resolved once via the lane *before* the picker, same shape round
    # 112 established for format_for_display generally.
    display_format, display_timezone = await lane.run(resolve_display_preferences)
    selected = await pick_item(
        session, entries,
        name_of=_session_name,
        stable_id_of=lambda e: id(e.session),
        description_of=lambda e: _session_description(e, display_format, display_timezone),
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

    if not await prompt_yes_no(session, f"Disconnect {_session_name(selected)!r}?", default=False):
        return

    target_user_id: int | None = None
    detail = f"peer address {selected.peer_address or 'unknown'}"
    if selected.username is not None:
        try:
            target_user_id = (await lane.run(get_user_by_username, selected.username)).id
        except AuthError:
            pass  # account no longer exists -- log by peer address only

    disconnected = await node_controls.session_registry.disconnect_one(selected.session)
    if not disconnected:
        await session.write_line(colored("That session is already gone.", fg_color=MUTED_COLOR))
        return

    await lane.run(
        record_action, actor=actor, action="disconnect_session", target_user_id=target_user_id, detail=detail
    )
    await session.write_line(f"{_session_name(selected)!r} disconnected.")


async def _shutdown_screen(session: Session, lane: DatabaseLane, actor: User, node_controls: NodeControls) -> None:
    await session.write_line(
        colored(
            "\r\nThis warns and disconnects every connected session (including this "
            "one), then locks out new logins. This cannot be undone once confirmed.",
            fg_color=MUTED_COLOR,
        )
    )
    await session.write("Graceful (wait, then disconnect) or immediate? [G/i]: ")
    mode_answer = (await session.read_line()).strip().lower()
    graceful = mode_answer != "i"

    await session.write("Custom broadcast message (leave blank for the default): ")
    message_raw = (await session.read_line()).strip()
    message = message_raw or None

    mode_label = "graceful" if graceful else "immediate"
    if not await prompt_yes_no(session, f"Confirm {mode_label} shutdown?", default=False):
        await session.write_line("Cancelled.")
        return

    # Logged before triggering, not after: the sequence disconnects
    # this very session too (see run_shutdown_sequence's own docstring
    # on why it's fired as a background task rather than awaited
    # inline), so there's no guarantee this session survives long
    # enough afterward to still be able to write an audit row.
    await lane.run(
        record_action, actor=actor, action="trigger_shutdown",
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


async def _welcome_banner_menu(session: Session, lane: DatabaseLane, actor: User) -> None:
    await _draw_welcome_banner_menu(session, lane)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "p":
            await session.write_line("")
            await _preview_welcome_banner_screen(session, lane)
            await _draw_welcome_banner_menu(session, lane)
        elif choice == "e":
            await session.write_line("")
            await _enable_welcome_banner_screen(session, lane, actor)
            await _draw_welcome_banner_menu(session, lane)
        elif choice == "d":
            await session.write_line("")
            await _disable_welcome_banner_screen(session, lane, actor)
            await _draw_welcome_banner_menu(session, lane)
        elif choice == "x":
            await session.write_line("")
            await _edit_welcome_banner_screen(session, lane, actor)
            await _draw_welcome_banner_menu(session, lane)
        else:
            await session.write(reject_keystroke())


async def _draw_welcome_banner_menu(session: Session, lane: DatabaseLane) -> None:
    status = await lane.run(welcome_banner_status)
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


async def _preview_welcome_banner_screen(session: Session, lane: DatabaseLane) -> None:
    """Renders the exact banner `netbbs.net.login_flow` would show at
    login right now -- the same `load_welcome_banner` call, used as a
    smoke test of the loading path itself, not a separate rendering."""

    def _load(db: Database) -> tuple:
        return welcome_banner_status(db), load_welcome_banner(db)

    status, banner_text = await lane.run(_load)
    await session.write_line(colored("\r\nPreviewing welcome banner as shown at login:", fg_color=MUTED_COLOR))
    await session.write_line(banner_text)
    if status.enabled and status.exists and (status.size_bytes or 0) <= MAX_BANNER_SIZE_BYTES:
        await session.write_line(colored("(showing your custom file)", fg_color=MUTED_COLOR))
    else:
        await session.write_line(
            colored(
                f"(showing the DEFAULT banner -- enabled={status.enabled}, file exists={status.exists})",
                fg_color=MUTED_COLOR,
            )
        )


async def _enable_welcome_banner_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    status = await lane.run(welcome_banner_status)
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

    def _apply(db: Database) -> None:
        set_welcome_banner_enabled(db, True)
        record_action(db, actor=actor, action="enable_welcome_banner", detail=str(status.path))

    await lane.run(_apply)
    await session.write_line("Welcome banner enabled. Use [P]review to verify it looks right.")


async def _disable_welcome_banner_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    def _apply(db: Database):
        status = welcome_banner_status(db)
        set_welcome_banner_enabled(db, False)
        record_action(db, actor=actor, action="disable_welcome_banner", detail=str(status.path))
        return status

    status = await lane.run(_apply)
    await session.write_line(
        f"Reverted to the default banner. Your file at {status.path} was left in place."
    )


async def _edit_welcome_banner_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """Opens the WYSIWYG ANSI art editor (design doc -- welcome banner
    round B1) against the current banner file, if any. `edit_ansi_art`
    itself knows nothing about "welcome banner" -- this screen is
    responsible for loading the existing file, computing the draft
    path, and writing a real save back to `banner_path(db)`."""
    path = await lane.run(banner_path)
    initial_bytes = path.read_bytes() if path.exists() else None
    draft_path = path.parent / f"{path.name}.draft"

    result = await edit_ansi_art(session, initial_bytes=initial_bytes, draft_path=draft_path)
    if result is None:
        await session.write_line(colored("\r\nNo changes saved.", fg_color=MUTED_COLOR))
        return

    path.write_bytes(result)
    await lane.run(record_action, actor=actor, action="edit_welcome_banner", detail=str(path))
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


async def _content_menu(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext | None = None
) -> None:
    await _draw_content_menu(session)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "m":
            await session.write_line("")
            await _board_menu(session, lane, actor, link_context=link_context)
            await _draw_content_menu(session)
        elif choice == "f":
            await session.write_line("")
            await _area_menu(session, lane, actor)
            await _draw_content_menu(session)
        elif choice == "n":
            await session.write_line("")
            await _channel_menu(session, lane, actor)
            await _draw_content_menu(session)
        elif choice == "c":
            await session.write_line("")
            await _category_menu(session, lane, actor)
            await _draw_content_menu(session)
        elif choice == "o":
            await session.write_line("")
            await _community_menu(session, lane, actor)
            await _draw_content_menu(session)
        elif choice == "g":
            await session.write_line("")
            await _grant_moderator_screen(session, lane, actor)
            await _draw_content_menu(session)
        elif choice == "r":
            await session.write_line("")
            await _revoke_moderator_screen(session, lane, actor)
            await _draw_content_menu(session)
        else:
            await session.write(reject_keystroke())


async def _draw_content_menu(session: Session) -> None:
    header = colored("Manage boards/areas/channels:", fg_color=HEADER_COLOR, bold=True)
    options = "  ".join(
        [
            menu_key("M", "essage boards"),
            menu_key("F", "ile areas"),
            menu_key("N", "nels", prefix="Cha"),
            menu_key("C", "ategories"),
            menu_key("O", "mmunities", prefix="C"),
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


async def _prompt_optional_int(session: Session, label: str, *, current: int | None) -> tuple[int | None, bool]:
    """Generic nullable-int prompt -- same "blank = keep, 'none' =
    clear" shape as `_prompt_min_age` below, factored out separately
    (rather than having that function delegate here) so its existing
    "no gate" wording -- already asserted on by
    tests/test_admin_flow.py and tests/test_board_pagination_ui.py --
    stays exactly as it is. Used for every nullable-int field
    introduced by design doc §16's Community inheritance model (round
    84): boards'/file areas' own `min_read_level`/`min_write_level`
    (this is the *only* way to ever set one back to `None` -- i.e. opt
    a resource into inheriting its Community's default -- `_read_int`
    has no clearing mechanism at all), plus Community's own
    `default_min_read_level`/`default_min_write_level`. "Clear" is the
    accurate word in both cases, not "no gate" (a level isn't a gate
    the way age/name-requirement are)."""
    shown = current if current is not None else "none"
    await session.write(f"{label} [{shown}] (blank = keep, 'none' = clear): ")
    raw = (await session.read_line()).strip()
    if not raw:
        return current, True
    if raw.lower() == "none":
        return None, True
    try:
        return int(raw), True
    except ValueError:
        await session.write_line(colored("Not a number -- cancelled.", fg_color=MUTED_COLOR))
        return None, False


async def _prompt_min_age(session: Session, *, current: int | None) -> tuple[int | None, bool]:
    """Shared min_age prompt for board/channel/area create+edit screens
    (design doc §18, round 101). Returns `(value, ok)` -- `ok=False`
    means the caller should cancel; blank keeps `current` (which may
    itself already be `None`, meaning no gate), `'none'` clears any
    existing gate, otherwise a plain integer sets it."""
    label = current if current is not None else "none"
    await session.write(f"Minimum age [{label}] (blank = keep, 'none' = no gate): ")
    raw = (await session.read_line()).strip()
    if not raw:
        return current, True
    if raw.lower() == "none":
        return None, True
    try:
        return int(raw), True
    except ValueError:
        await session.write_line(colored("Not a number -- cancelled.", fg_color=MUTED_COLOR))
        return None, False


async def _prompt_name_requirement(session: Session, *, current: str | None) -> tuple[str | None, bool]:
    """Shared name_requirement prompt (design doc §18, round 101) --
    `none` (no gate), `verified` (SysOp can identify but nothing is
    displayed), or `verified_and_displayed` (shown within this
    resource's own rendering, design doc round 99)."""
    label = current or "none"
    await session.write(
        f"Name requirement [{label}] (none/verified/verified_and_displayed, blank = keep): "
    )
    raw = (await session.read_line()).strip().lower()
    if not raw:
        return current, True
    if raw == "none":
        return None, True
    if raw in ("verified", "verified_and_displayed"):
        return raw, True
    await session.write_line(
        colored("Must be none/verified/verified_and_displayed -- cancelled.", fg_color=MUTED_COLOR)
    )
    return None, False


async def _pick_optional_category(
    session: Session,
    lane: DatabaseLane,
    *,
    list_top_level,
    list_subcategories,
    title: str,
    community_id: int | None = None,
    resources: list | None = None,
):
    """Optional category picker shared by board/channel/area create+edit
    screens. Top-level categories are shown first; picking one that has
    sub-categories offers picking one of those instead, matching the
    two-level design (`netbbs.boards.categories`/`netbbs.chat.categories`/
    `netbbs.files.categories`). Returns the chosen category's id, or
    `None` if declined or none exist.

    `community_id`/`resources` (design doc §16, round 84's category
    leak-prevention, admin-side half -- see
    `netbbs.net.login_flow._browse_boards_in_category`'s docstring for
    the browse-side half, which this mirrors) narrow the offered
    categories to those already used by ≥1 same-type resource in this
    Community, but only once a Community was actually just assigned
    (`community_id` not `None`) and the caller supplies its full,
    unfiltered same-type resource list via `resources` (e.g.
    `list_boards(db)`) for this function to filter internally. Left
    completely unfiltered -- today's original behavior -- when no
    Community was assigned, since the leak this guards against is
    specifically cross-Community, not a concern for an Uncategorized
    resource's own category choice.
    """
    if not await prompt_yes_no(session, "Assign a category?", default=False):
        return None

    used_category_ids: set[int] | None = None
    if community_id is not None and resources is not None:
        in_community = [r for r in resources if r.community_id == community_id]
        used_category_ids = {r.category_id for r in in_community if r.category_id is not None}

    def _load_top_level(db: Database) -> list:
        top_level = list_top_level(db)
        if used_category_ids is not None:
            top_level = [
                c for c in top_level
                if c.id in used_category_ids
                or any(sub.id in used_category_ids for sub in list_subcategories(db, c.id))
            ]
        return top_level

    top_level = await lane.run(_load_top_level)

    selected = await pick_item(
        session, top_level,
        name_of=lambda c: c.name,
        stable_id_of=lambda c: c.id,
        title=title,
        empty_message="No categories exist yet.",
    )
    if selected is None:
        return None
    subs = await lane.run(list_subcategories, selected.id)
    if used_category_ids is not None:
        subs = [c for c in subs if c.id in used_category_ids]
    if not subs:
        return selected.id
    if not await prompt_yes_no(session, f"Use a sub-category of {selected.name!r} instead?", default=False):
        return selected.id
    sub_selected = await pick_item(
        session, subs,
        name_of=lambda c: c.name,
        stable_id_of=lambda c: c.id,
        title=f"Sub-category of {selected.name!r}",
        empty_message="No sub-categories.",
    )
    return sub_selected.id if sub_selected is not None else selected.id


async def _pick_optional_community(session: Session, lane: DatabaseLane) -> int | None:
    """Optional Community picker shared by board/channel/area create+
    edit screens (design doc §16, round 84) -- mirrors
    `_pick_optional_category` exactly, but flat (a Community has no
    two-level sub-structure the way categories do). Prompted *before*
    the existing category prompt at every call site -- Community is the
    outer layer, chosen first. Returns the chosen Community's id, or
    `None` if declined or none exist yet."""
    if not await prompt_yes_no(session, "Assign a Community?", default=False):
        return None
    selected = await pick_item(
        session, await lane.run(list_communities),
        name_of=lambda c: c.name,
        stable_id_of=lambda c: c.id,
        title="Community",
        empty_message="No Communities exist yet.",
    )
    return selected.id if selected is not None else None


def _community_label(db: Database, community_id: int | None) -> str:
    """Detail-screen display helper -- `(none)` for `community_id is
    None`, else the Community's own name (sanitized, same as every
    other user-controlled string shown on these detail screens)."""
    community = get_community(db, community_id)
    return sanitize_text(community.name) if community is not None else "(none)"


# -- Communities (design doc §16, rounds 71/83/84/86) ------------------


async def _community_menu(session: Session, lane: DatabaseLane, actor: User) -> None:
    await _draw_community_menu(session)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "c":
            await session.write_line("")
            await _create_community_screen(session, lane, actor)
            await _draw_community_menu(session)
        elif choice == "l":
            await session.write_line("")
            await _list_communities_screen(session, lane, actor)
            await _draw_community_menu(session)
        else:
            await session.write(reject_keystroke())


async def _draw_community_menu(session: Session) -> None:
    header = colored("Communities:", fg_color=HEADER_COLOR, bold=True)
    options = "  ".join([menu_key("C", "reate"), menu_key("L", "ist"), menu_key("B", "ack")])
    await session.write_line(f"\r\n{header} {options}")
    await session.write("Choice: ")


async def _create_community_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """Create stays lean, Edit carries the rest (design doc §16, round
    84) -- same split boards already use. Only name/description are
    prompted here; `hidden` and every `default_*` field start at their
    own defaults (visible, no gate) and are set via `_edit_community_screen`
    afterward if the SysOp wants them."""
    await session.write_line(colored("\r\nCreate Community", fg_color=HEADER_COLOR, bold=True))
    await session.write("Name: ")
    name = (await session.read_line()).strip()
    if not name:
        await session.write_line(colored("Cancelled: name cannot be blank.", fg_color=MUTED_COLOR))
        return
    await session.write("Description (optional): ")
    description = (await session.read_line()).strip() or None

    try:
        community = await lane.run(create_community, name, description=description, creator=actor)
    except CommunityError as exc:
        await session.write_line(colored(f"Could not create Community: {exc}", fg_color=MUTED_COLOR))
        return
    await session.write_line(f"Created Community {community.name!r}.")
    await _community_detail_screen(session, lane, actor, community)


async def _list_communities_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    communities = await lane.run(list_communities)
    selected = await pick_item(
        session, communities,
        name_of=lambda c: c.name,
        stable_id_of=lambda c: c.id,
        description_of=_community_description,
        title="Communities",
        empty_message="No Communities yet.",
    )
    if selected is not None:
        await _community_detail_screen(session, lane, actor, selected)


def _community_description(community: Community) -> str:
    return "hidden" if community.hidden else "listed"


async def _community_detail_screen(session: Session, lane: DatabaseLane, actor: User, community: Community) -> None:
    """No "pending" equivalent here, unlike boards/areas -- a Community
    holds no content of its own (design doc §16, round 84)."""
    await _draw_community_detail(session, community)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "e":
            await session.write_line("")
            updated = await _edit_community_screen(session, lane, actor, community)
            if updated is not None:
                community = updated
            await _draw_community_detail(session, community)
        elif choice == "d":
            await session.write_line("")
            deleted = await _delete_community_screen(session, lane, actor, community)
            if deleted:
                return
            await _draw_community_detail(session, community)
        else:
            await session.write(reject_keystroke())


async def _draw_community_detail(session: Session, community: Community) -> None:
    header = colored(sanitize_text(community.name), fg_color=HEADER_COLOR, bold=True)
    await session.write_line(f"\r\n{header}")
    await session.write_line(
        f"Description: {sanitize_text(community.description) if community.description else '(none)'}"
    )
    await session.write_line(f"Hidden: {'yes' if community.hidden else 'no'}")
    read_default = community.default_min_read_level if community.default_min_read_level is not None else "none"
    write_default = community.default_min_write_level if community.default_min_write_level is not None else "none"
    await session.write_line(f"Default read level: {read_default}  Default write level: {write_default}")
    await session.write_line(
        f"Default minimum age: "
        f"{community.default_min_age if community.default_min_age is not None else 'none'}  "
        f"Default name requirement: {community.default_name_requirement or 'none'}"
    )
    options = "  ".join([menu_key("E", "dit"), menu_key("D", "elete"), menu_key("B", "ack")])
    await session.write_line(f"\r\n{options}")
    await session.write("Choice: ")


async def _edit_community_screen(
    session: Session, lane: DatabaseLane, actor: User, community: Community
) -> Community | None:
    await session.write(f"Name [{community.name}]: ")
    name = (await session.read_line()).strip() or community.name
    await session.write(f"Description [{community.description or '(none)'}]: ")
    description = (await session.read_line()).strip() or community.description
    hidden = await prompt_yes_no_or_keep(session, "Hidden?", current=community.hidden)

    default_min_read_level, ok = await _prompt_optional_int(
        session, "Default minimum read level", current=community.default_min_read_level
    )
    if not ok:
        return None
    default_min_write_level, ok = await _prompt_optional_int(
        session, "Default minimum write level", current=community.default_min_write_level
    )
    if not ok:
        return None
    default_min_age, ok = await _prompt_min_age(session, current=community.default_min_age)
    if not ok:
        return None
    default_name_requirement, ok = await _prompt_name_requirement(
        session, current=community.default_name_requirement
    )
    if not ok:
        return None

    try:
        updated = await lane.run(
            update_community,
            community, name=name, description=description, hidden=hidden,
            default_min_read_level=default_min_read_level, default_min_write_level=default_min_write_level,
            default_min_age=default_min_age, default_name_requirement=default_name_requirement,
            changed_by=actor,
        )
    except CommunityError as exc:
        await session.write_line(colored(f"Could not update Community: {exc}", fg_color=MUTED_COLOR))
        return None
    await session.write_line(f"Updated {updated.name!r}.")
    return updated


async def _delete_community_screen(session: Session, lane: DatabaseLane, actor: User, community: Community) -> bool:
    """Shows the blast radius before committing (design doc §16, round
    84's exact confirmation wording): how many boards/channels/areas
    will revert to Uncategorized, and how many Community-blanket
    moderator grants will be revoked outright."""

    def _counts(db: Database) -> tuple[int, int, int, int]:
        board_count = sum(1 for b in list_boards(db) if b.community_id == community.id)
        channel_count = sum(1 for c in list_channels(db) if c.community_id == community.id)
        area_count = sum(1 for a in list_file_areas(db) if a.community_id == community.id)
        grant_count = len(list_grants_for_community(db, community.id))
        return board_count, channel_count, area_count, grant_count

    board_count, channel_count, area_count, grant_count = await lane.run(_counts)
    await session.write_line(
        colored(
            f"\r\nThis Community has {board_count} board(s), {channel_count} channel(s), "
            f"{area_count} file area(s), and {grant_count} moderator grant(s). Deleting will "
            "un-categorize its resources and revoke those grants. This cannot be undone.",
            fg_color=MUTED_COLOR,
        )
    )
    await session.write(f"Type the Community name {community.name!r} to confirm, or anything else to cancel: ")
    confirmation = (await session.read_line()).strip()
    if confirmation != community.name:
        await session.write_line("Cancelled.")
        return False
    await lane.run(delete_community, community, deleted_by=actor)
    await session.write_line(f"{community.name!r} deleted.")
    return True


# -- message boards ----------------------------------------------------


async def _board_menu(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext | None = None
) -> None:
    await _draw_board_menu(session)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "c":
            await session.write_line("")
            await _create_board_screen(session, lane, actor)
            await _draw_board_menu(session)
        elif choice == "l":
            await session.write_line("")
            await _list_boards_screen(session, lane, actor, link_context=link_context)
            await _draw_board_menu(session)
        else:
            await session.write(reject_keystroke())


async def _draw_board_menu(session: Session) -> None:
    header = colored("Message boards:", fg_color=HEADER_COLOR, bold=True)
    options = "  ".join([menu_key("C", "reate"), menu_key("L", "ist"), menu_key("B", "ack")])
    await session.write_line(f"\r\n{header} {options}")
    await session.write("Choice: ")


async def _create_board_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    await session.write_line(colored("\r\nCreate board", fg_color=HEADER_COLOR, bold=True))
    await session.write("Name: ")
    name = (await session.read_line()).strip()
    if not name:
        await session.write_line(colored("Cancelled: name cannot be blank.", fg_color=MUTED_COLOR))
        return
    await session.write("Description (optional): ")
    description = (await session.read_line()).strip() or None
    min_read_level, ok = await _prompt_optional_int(session, "Minimum read level", current=0)
    if not ok:
        return
    min_write_level, ok = await _prompt_optional_int(session, "Minimum write level", current=0)
    if not ok:
        return
    community_id = await _pick_optional_community(session, lane)
    category_id = await _pick_optional_category(
        session, lane, list_top_level=list_top_level_board_categories,
        list_subcategories=list_board_subcategories, title="Board category",
        community_id=community_id, resources=await lane.run(list_boards),
    )
    pinned = await prompt_yes_no(session, "Pinned?", default=False)
    moderated = await prompt_yes_no(session, "Moderated (posts need approval)?", default=False)
    await session.write("Max post age in days (blank = unlimited): ")
    max_age_raw = (await session.read_line()).strip()
    max_post_age_days = None
    if max_age_raw:
        try:
            max_post_age_days = int(max_age_raw)
        except ValueError:
            await session.write_line(colored("Not a number -- cancelled.", fg_color=MUTED_COLOR))
            return
    min_age, ok = await _prompt_min_age(session, current=None)
    if not ok:
        return
    name_requirement, ok = await _prompt_name_requirement(session, current=None)
    if not ok:
        return

    try:
        board = await lane.run(
            create_board,
            name, description=description, min_read_level=min_read_level,
            min_write_level=min_write_level, category_id=category_id, pinned=pinned,
            moderated=moderated, max_post_age_days=max_post_age_days,
            min_age=min_age, name_requirement=name_requirement,
            community_id=community_id, creator=actor,
        )
    except BoardError as exc:
        await session.write_line(colored(f"Could not create board: {exc}", fg_color=MUTED_COLOR))
        return
    await session.write_line(f"Created board {board.name!r}.")


async def _list_boards_screen(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext | None = None
) -> None:
    boards = await lane.run(list_boards, order_by="alphabetical")
    selected = await pick_item(
        session, boards,
        name_of=lambda b: b.name,
        stable_id_of=lambda b: b.id,
        description_of=_board_description,
        title="Boards",
        empty_message="No boards yet.",
    )
    if selected is not None:
        await _board_detail_screen(session, lane, actor, selected, link_context=link_context)


def _board_description(board: Board) -> str:
    status = "moderated" if board.moderated else "open"
    read_level = board.min_read_level if board.min_read_level is not None else "inherit"
    write_level = board.min_write_level if board.min_write_level is not None else "inherit"
    return f"read {read_level}/write {write_level}, {status}"


async def _board_detail_screen(
    session: Session, lane: DatabaseLane, actor: User, board: Board, *, link_context: LinkContext | None = None
) -> None:
    linked = await lane.run(is_board_linked, board) if link_context is not None else False
    is_origin, has_incoming_offer = await _draw_board_detail(
        session, lane, board, linked=linked, link_context=link_context
    )
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "e":
            await session.write_line("")
            updated = await _edit_board_screen(session, lane, actor, board)
            if updated is not None:
                board = updated
            is_origin, has_incoming_offer = await _draw_board_detail(
                session, lane, board, linked=linked, link_context=link_context
            )
        elif choice == "d":
            await session.write_line("")
            deleted = await _delete_board_screen(session, lane, actor, board)
            if deleted:
                return
            is_origin, has_incoming_offer = await _draw_board_detail(
                session, lane, board, linked=linked, link_context=link_context
            )
        elif choice == "p":
            await session.write_line("")
            await _pending_posts_screen(session, lane, actor, board, link_context=link_context)
            is_origin, has_incoming_offer = await _draw_board_detail(
                session, lane, board, linked=linked, link_context=link_context
            )
        elif choice == "l" and link_context is not None and not linked:
            await session.write_line("")
            await _link_board_screen(session, lane, board, link_context)
            linked = await lane.run(is_board_linked, board)
            is_origin, has_incoming_offer = await _draw_board_detail(
                session, lane, board, linked=linked, link_context=link_context
            )
        elif choice == "t" and link_context is not None and linked and is_origin:
            await session.write_line("")
            await _transfer_board_origin_screen(session, lane, board, link_context)
            is_origin, has_incoming_offer = await _draw_board_detail(
                session, lane, board, linked=linked, link_context=link_context
            )
        elif choice == "a" and has_incoming_offer:
            await session.write_line("")
            await _accept_board_origin_transfer_screen(session, lane, board, link_context)
            is_origin, has_incoming_offer = await _draw_board_detail(
                session, lane, board, linked=linked, link_context=link_context
            )
        else:
            await session.write(reject_keystroke())


async def _link_board_screen(session: Session, lane: DatabaseLane, board: Board, link_context: LinkContext) -> None:
    """
    `[L]ink this board` (design doc round 124/128): puts `board` into
    Link scope via a signed `board_genesis` event referencing its
    existing `board_id` -- never a fresh one (round 124's "promote an
    existing local board" case, the normal one, not a special one).

    The six `default_*` cascading-scalar-default fields (round 124)
    are pre-filled from `board`'s own *current* local settings (the
    obvious starting recommendation, matching `_edit_board_screen`'s
    own prefill-then-edit convention). For the four fields sharing
    `_prompt_optional_int`/`_prompt_min_age`/`_prompt_name_requirement`
    with `_edit_board_screen`, blank keeps that prefilled value as the
    recommendation and typing `none` clears it to send no
    recommendation at all for that field -- their own existing
    "blank = keep, 'none' = clear" convention, reused rather than
    special-cased here. `default_moderated`/`default_max_post_age_days`
    have no such prior art to reuse (`_edit_board_screen` reads them as
    plain required fields, never optional) -- blank means no
    recommendation directly for both.

    Building/signing/persisting the genesis is a plain, synchronous
    `db`-first call (`link_board`), dispatched through `lane` like
    every other board-admin mutation here. Registering the result with
    the *live* `LinkNode` is deliberately done here, directly, on the
    event loop -- never inside the lane-dispatched call itself (see
    `link_board`'s own docstring for why that split matters: `LinkNode`
    mutation and `DatabaseLane` dispatch must never share a thread).
    """
    await session.write_line(colored("\r\nLink this board", fg_color=HEADER_COLOR, bold=True))
    default_min_read_level, ok = await _prompt_optional_int(
        session, "Recommended minimum read level", current=board.min_read_level
    )
    if not ok:
        return
    default_min_write_level, ok = await _prompt_optional_int(
        session, "Recommended minimum write level", current=board.min_write_level
    )
    if not ok:
        return
    await session.write(f"Recommend moderated? [{'y' if board.moderated else 'N'}/blank=no recommendation]: ")
    moderated_answer = (await session.read_line()).strip().lower()
    default_moderated = moderated_answer == "y" if moderated_answer in ("y", "n") else None
    current_age = board.max_post_age_days if board.max_post_age_days is not None else "unlimited"
    await session.write(f"Recommended max post age in days [{current_age}] (blank = no recommendation): ")
    max_age_raw = (await session.read_line()).strip()
    default_max_post_age_days = None
    if max_age_raw:
        try:
            default_max_post_age_days = int(max_age_raw)
        except ValueError:
            await session.write_line(colored("Not a number -- cancelled.", fg_color=MUTED_COLOR))
            return
    default_min_age, ok = await _prompt_min_age(session, current=board.min_age)
    if not ok:
        return
    default_name_requirement, ok = await _prompt_name_requirement(session, current=board.name_requirement)
    if not ok:
        return

    forked_from: str | None = None
    if await prompt_yes_no(session, "Is this a fork of an existing Linked board?", default=False):
        candidates = await lane.run(_linked_boards_excluding, board.id)
        chosen = await pick_item(
            session, candidates,
            name_of=lambda b: b.name, stable_id_of=lambda b: b.id,
            title="Fork of which board?", empty_message="No other Linked boards to fork from.",
        )
        if chosen is not None:
            forked_from = chosen.board_id

    try:
        genesis = await lane.run(
            link_board,
            board,
            node_identity=link_context.node_identity,
            default_min_read_level=default_min_read_level,
            default_min_write_level=default_min_write_level,
            default_moderated=default_moderated,
            default_max_post_age_days=default_max_post_age_days,
            default_min_age=default_min_age,
            default_name_requirement=default_name_requirement,
            forked_from=forked_from,
        )
    except LinkBoardsError as exc:
        await session.write_line(colored(f"Could not Link board: {exc}", fg_color=MUTED_COLOR))
        return

    link_context.link_node.boards[board.board_id] = genesis
    link_context.link_node.known_event_ids.add(genesis.content_id)
    link_context.link_node.events[genesis.content_id] = genesis.to_dict()

    await session.write_line(f"Linked {board.name!r} -- it will be pushed to peers on the next sync pass.")


def _linked_boards_excluding(db: Database, exclude_board_id: int) -> list[Board]:
    """Every currently-Linked board except `exclude_board_id` (design
    doc §13, round 94/issue #53) -- the fork-source candidate list for
    `_link_board_screen`'s own optional `forked_from` prompt. A board
    doesn't fork from itself, and an as-yet-unLinked board has no
    genesis to point at in the first place, so both are excluded by
    construction (`exclude_board_id` covers the former; `is_board_
    linked` alone already covers the latter)."""
    return [
        board for board in list_boards(db, order_by="alphabetical")
        if board.id != exclude_board_id and is_board_linked(db, board)
    ]


async def _transfer_board_origin_screen(
    session: Session, lane: DatabaseLane, board: Board, link_context: LinkContext
) -> None:
    """
    `[T]ransfer origin` (design doc §13, round 94/issue #53): the
    current origin's half of the mutual-consent handoff -- offers a
    different, already-known peer as `board`'s next origin. Alone, this
    changes nothing (see `BoardOriginTransferOffer`'s own docstring) --
    every other node, including the proposed new origin itself, keeps
    trusting *this* node until that peer's own SysOp explicitly accepts
    on their own node (`_accept_board_origin_transfer_screen`, there).

    No picker here, deliberately -- unlike `board`/`Board` rows, a peer
    has no local integer id `pick_item`'s `stable_id_of` could use;
    fingerprints are typed directly, the same way this UI already shows
    them everywhere else a specific peer needs naming (e.g. `Origin:
    <fingerprint>` on this same screen).
    """
    peers = sorted(link_context.link_node.peers.keys())
    await session.write_line(colored("\r\nTransfer board origin", fg_color=HEADER_COLOR, bold=True))
    if not peers:
        await session.write_line(colored("No known peers to transfer this board to.", fg_color=MUTED_COLOR))
        return
    await session.write_line("Known peers:")
    for fingerprint in peers:
        await session.write_line(f"  {fingerprint}")
    await session.write("New origin's fingerprint (blank to cancel): ")
    target = (await session.read_line()).strip()
    if not target:
        return
    if target not in link_context.link_node.peers:
        await session.write_line(colored("Not a known peer -- cancelled.", fg_color=MUTED_COLOR))
        return
    if not await prompt_yes_no(session, f"Offer to hand {board.name!r} off to {target}?", default=False):
        await session.write_line("Cancelled.")
        return

    try:
        offer = await lane.run(
            offer_board_origin_transfer,
            board,
            node_identity=link_context.node_identity,
            new_origin_fingerprint=target,
        )
    except LinkBoardsError as exc:
        await session.write_line(colored(f"Could not offer transfer: {exc}", fg_color=MUTED_COLOR))
        return

    link_context.link_node.pending_origin_transfers[board.board_id] = offer
    link_context.link_node.board_lifecycle_head[board.board_id] = offer.content_id
    link_context.link_node.known_event_ids.add(offer.content_id)
    link_context.link_node.events[offer.content_id] = offer.to_dict()

    await session.write_line("Offer sent -- it will be pushed to peers on the next sync pass.")


async def _accept_board_origin_transfer_screen(
    session: Session, lane: DatabaseLane, board: Board, link_context: LinkContext
) -> None:
    """
    `[A]ccept transfer` (design doc §13, round 94/issue #53): the
    consent-completing half -- accepts the single pending incoming
    origin-transfer offer for `board` that names this node as the
    proposed new origin. Only reachable when `_draw_board_detail`
    already confirmed such an offer exists (`has_incoming_offer`), but
    re-checked here too rather than trusted blindly, the same
    defense-in-depth every other admin mutation in this file already
    applies to a caller-supplied precondition.
    """
    offer = link_context.link_node.pending_origin_transfers.get(board.board_id)
    if offer is None or offer.payload.get("new_origin_fingerprint") != link_context.node_identity.fingerprint:
        await session.write_line(colored("\r\nNo pending incoming offer for this board.", fg_color=MUTED_COLOR))
        return

    old_origin = offer.payload.get("old_origin_fingerprint")
    await session.write_line(colored("\r\nAccept board origin", fg_color=HEADER_COLOR, bold=True))
    if not await prompt_yes_no(session, f"Accept origin of {board.name!r} from {old_origin}?", default=False):
        await session.write_line("Cancelled.")
        return

    try:
        accepted = await lane.run(
            accept_board_origin_transfer,
            board,
            node_identity=link_context.node_identity,
            offer=offer,
        )
    except LinkBoardsError as exc:
        await session.write_line(colored(f"Could not accept transfer: {exc}", fg_color=MUTED_COLOR))
        return

    link_context.link_node.board_origin[board.board_id] = link_context.node_identity.fingerprint
    link_context.link_node.board_lifecycle_head[board.board_id] = accepted.content_id
    del link_context.link_node.pending_origin_transfers[board.board_id]
    link_context.link_node.known_event_ids.add(accepted.content_id)
    link_context.link_node.events[accepted.content_id] = accepted.to_dict()

    await session.write_line(
        f"Accepted -- this node is now {board.name!r}'s origin. Pushed to peers on the next sync pass."
    )


async def _draw_board_detail(
    session: Session,
    lane: DatabaseLane,
    board: Board,
    *,
    linked: bool = False,
    link_context: LinkContext | None = None,
) -> tuple[bool, bool]:
    """
    Returns `(is_origin, has_incoming_offer)` (design doc §13, round
    94/issue #53) -- whether this node is currently `board`'s own
    origin (gates `[T]ransfer origin`) and whether a pending incoming
    origin-transfer offer names this node as the proposed new origin
    (gates `[A]ccept transfer`). `_board_detail_screen`'s own dispatch
    loop needs both every time it redraws, so returning them here
    avoids a second, separately-timed recomputation immediately after.
    """
    header = colored(sanitize_text(board.name), fg_color=HEADER_COLOR, bold=True)
    await session.write_line(f"\r\n{header}")
    await session.write_line(f"Description: {sanitize_text(board.description) if board.description else '(none)'}")
    await session.write_line(f"Community: {await lane.run(_community_label, board.community_id)}")
    read_level = board.min_read_level if board.min_read_level is not None else "inherit"
    write_level = board.min_write_level if board.min_write_level is not None else "inherit"
    await session.write_line(f"Read level: {read_level}  Write level: {write_level}")
    await session.write_line(
        f"Pinned: {'yes' if board.pinned else 'no'}  Moderated: {'yes' if board.moderated else 'no'}"
    )
    age = board.max_post_age_days if board.max_post_age_days is not None else "unlimited"
    await session.write_line(f"Max post age: {age} days")
    await session.write_line(
        f"Minimum age: {board.min_age if board.min_age is not None else 'none'}  "
        f"Name requirement: {board.name_requirement or 'none'}"
    )
    is_origin = False
    has_incoming_offer = False
    if link_context is not None:
        await session.write_line(f"Linked: {'yes' if linked else 'no'}")
        if linked:
            origin_fingerprint = await lane.run(board_origin_fingerprint, board)
            is_origin = origin_fingerprint == link_context.node_identity.fingerprint
            orphan_note = ""
            if not is_origin:
                peer = link_context.link_node.peers.get(origin_fingerprint)
                if peer is not None and is_board_origin_orphaned(peer):
                    orphan_note = colored(
                        " (ORPHANED -- origin's signing key was revoked, no replacement on file)",
                        fg_color=MUTED_COLOR,
                    )
            origin_label = "this node" if is_origin else origin_fingerprint
            await session.write_line(f"Origin: {origin_label}{orphan_note}")

            offer = link_context.link_node.pending_origin_transfers.get(board.board_id)
            if offer is not None:
                if offer.payload.get("new_origin_fingerprint") == link_context.node_identity.fingerprint:
                    has_incoming_offer = True
                    await session.write_line(
                        colored(
                            f"Pending: an incoming origin-transfer offer from "
                            f"{offer.payload.get('old_origin_fingerprint')}",
                            fg_color=MUTED_COLOR,
                        )
                    )
                elif is_origin:
                    await session.write_line(
                        colored(
                            f"Pending: your own outstanding transfer offer to "
                            f"{offer.payload.get('new_origin_fingerprint')}",
                            fg_color=MUTED_COLOR,
                        )
                    )
    options = [menu_key("E", "dit"), menu_key("D", "elete"), menu_key("P", "ending posts")]
    if link_context is not None and not linked:
        options.append(menu_key("L", "ink this board"))
    if link_context is not None and linked and is_origin and board.board_id not in link_context.link_node.pending_origin_transfers:
        options.append(menu_key("T", "ransfer origin"))
    if has_incoming_offer:
        options.append(menu_key("A", "ccept transfer"))
    options.append(menu_key("B", "ack"))
    await session.write_line(f"\r\n{'  '.join(options)}")
    await session.write("Choice: ")
    return is_origin, has_incoming_offer


async def _edit_board_screen(session: Session, lane: DatabaseLane, actor: User, board: Board) -> Board | None:
    await session.write(f"Name [{board.name}]: ")
    name = (await session.read_line()).strip() or board.name
    await session.write(f"Description [{board.description or '(none)'}]: ")
    description = (await session.read_line()).strip() or board.description
    min_read_level, ok = await _prompt_optional_int(session, "Minimum read level", current=board.min_read_level)
    if not ok:
        return None
    min_write_level, ok = await _prompt_optional_int(session, "Minimum write level", current=board.min_write_level)
    if not ok:
        return None
    change_community = await prompt_yes_no(session, "Change Community?", default=False)
    community_id = board.community_id
    if change_community:
        community_id = await _pick_optional_community(session, lane)
    change_category = await prompt_yes_no(session, "Change category?", default=False)
    category_id = board.category_id
    if change_category:
        category_id = await _pick_optional_category(
            session, lane, list_top_level=list_top_level_board_categories,
            list_subcategories=list_board_subcategories, title="Board category",
            community_id=community_id, resources=await lane.run(list_boards),
        )
    pinned = await prompt_yes_no_or_keep(session, "Pinned?", current=board.pinned)
    moderated = await prompt_yes_no_or_keep(session, "Moderated?", current=board.moderated)
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
    min_age, ok = await _prompt_min_age(session, current=board.min_age)
    if not ok:
        return None
    name_requirement, ok = await _prompt_name_requirement(session, current=board.name_requirement)
    if not ok:
        return None

    try:
        updated = await lane.run(
            update_board,
            board, name=name, description=description, min_read_level=min_read_level,
            min_write_level=min_write_level, category_id=category_id, pinned=pinned,
            moderated=moderated, max_post_age_days=max_post_age_days,
            min_age=min_age, name_requirement=name_requirement,
            community_id=community_id, changed_by=actor,
        )
    except BoardError as exc:
        await session.write_line(colored(f"Could not update board: {exc}", fg_color=MUTED_COLOR))
        return None
    await session.write_line(f"Updated {updated.name!r}.")
    return updated


async def _delete_board_screen(session: Session, lane: DatabaseLane, actor: User, board: Board) -> bool:
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
    await lane.run(delete_board, board, deleted_by=actor)
    await session.write_line(f"{board.name!r} deleted.")
    return True


async def _pending_posts_screen(
    session: Session, lane: DatabaseLane, actor: User, board: Board, *, link_context: LinkContext | None = None
) -> None:
    while True:
        posts = await lane.run(list_pending_posts, board, requesting_user=actor)
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
        await _post_action_screen(session, lane, actor, selected, board, link_context=link_context)


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


async def _post_action_screen(
    session: Session,
    lane: DatabaseLane,
    actor: User,
    post: Post,
    board: Board,
    *,
    link_context: LinkContext | None = None,
) -> None:
    await _draw_post_action(session, post)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "a":
            await session.write_line("")
            approved = await lane.run(approve_post, post, approved_by=actor)
            if link_context is not None:
                await lane.run(
                    queue_board_post_if_linked, approved, board, node_identity=link_context.node_identity
                )
            await session.write_line("Approved.")
            return
        elif choice == "r":
            await session.write_line("")
            try:
                await lane.run(delete_post, post, deleted_by=actor)
            except PostError as exc:
                await session.write_line(f"Error: {exc}")
                await _draw_post_action(session, post)
                continue
            await session.write_line("Rejected.")
            return
        elif choice == "p":
            await session.write_line("")
            post = await lane.run(set_post_pinned, post, not post.pinned, changed_by=actor)
            await _draw_post_action(session, post)
        elif choice == "x":
            await session.write_line("")
            post = await lane.run(set_post_exempt, post, not post.exempt_from_expiry, changed_by=actor)
            await _draw_post_action(session, post)
        else:
            await session.write(reject_keystroke())


# -- file areas ----------------------------------------------------------


async def _area_menu(session: Session, lane: DatabaseLane, actor: User) -> None:
    await _draw_area_menu(session)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "c":
            await session.write_line("")
            await _create_area_screen(session, lane, actor)
            await _draw_area_menu(session)
        elif choice == "l":
            await session.write_line("")
            await _list_areas_screen(session, lane, actor)
            await _draw_area_menu(session)
        elif choice == "g":
            await session.write_line("")
            await _gc_screen(session, lane)
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


async def _gc_screen(session: Session, lane: DatabaseLane) -> None:
    """
    Reference-aware blob garbage collection (GitHub issue #35): always
    shows a dry-run report first, then asks separately before actually
    reclaiming anything -- the same "preview, then explicit confirm"
    shape delete confirmations elsewhere in this menu use, appropriate
    here too since this is a one-way filesystem operation the database
    itself can't undo.
    """
    preview = await lane.run(reclaim_orphaned_blobs, dry_run=True)
    await _write_gc_report(session, preview)
    if preview.reclaimable_blobs == 0:
        return
    if not await prompt_yes_no(session, "Reclaim this space now?", default=False):
        return
    result = await lane.run(reclaim_orphaned_blobs, dry_run=False)
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


async def _create_area_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    await session.write_line(colored("\r\nCreate file area", fg_color=HEADER_COLOR, bold=True))
    await session.write("Name: ")
    name = (await session.read_line()).strip()
    if not name:
        await session.write_line(colored("Cancelled: name cannot be blank.", fg_color=MUTED_COLOR))
        return
    await session.write("Description (optional): ")
    description = (await session.read_line()).strip() or None
    min_read_level, ok = await _prompt_optional_int(session, "Minimum read level", current=0)
    if not ok:
        return
    min_write_level, ok = await _prompt_optional_int(session, "Minimum write level", current=0)
    if not ok:
        return
    community_id = await _pick_optional_community(session, lane)
    category_id = await _pick_optional_category(
        session, lane, list_top_level=list_top_level_file_categories,
        list_subcategories=list_file_subcategories, title="File-area category",
        community_id=community_id, resources=await lane.run(list_file_areas),
    )
    pinned = await prompt_yes_no(session, "Pinned?", default=False)
    moderated = await prompt_yes_no(session, "Moderated (uploads need approval)?", default=False)
    await session.write("Max file age in days (blank = unlimited): ")
    max_age_raw = (await session.read_line()).strip()
    max_file_age_days = None
    if max_age_raw:
        try:
            max_file_age_days = int(max_age_raw)
        except ValueError:
            await session.write_line(colored("Not a number -- cancelled.", fg_color=MUTED_COLOR))
            return
    min_age, ok = await _prompt_min_age(session, current=None)
    if not ok:
        return
    name_requirement, ok = await _prompt_name_requirement(session, current=None)
    if not ok:
        return

    try:
        area = await lane.run(
            create_file_area,
            name, description=description, min_read_level=min_read_level,
            min_write_level=min_write_level, category_id=category_id, pinned=pinned,
            moderated=moderated, max_file_age_days=max_file_age_days,
            min_age=min_age, name_requirement=name_requirement,
            community_id=community_id, creator=actor,
        )
    except FileAreaError as exc:
        await session.write_line(colored(f"Could not create file area: {exc}", fg_color=MUTED_COLOR))
        return
    await session.write_line(f"Created file area {area.name!r}.")


async def _list_areas_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    areas = await lane.run(list_file_areas, order_by="alphabetical")
    selected = await pick_item(
        session, areas,
        name_of=lambda a: a.name,
        stable_id_of=lambda a: a.id,
        description_of=_area_description,
        title="File areas",
        empty_message="No file areas yet.",
    )
    if selected is not None:
        await _area_detail_screen(session, lane, actor, selected)


def _area_description(area: FileArea) -> str:
    status = "moderated" if area.moderated else "open"
    read_level = area.min_read_level if area.min_read_level is not None else "inherit"
    write_level = area.min_write_level if area.min_write_level is not None else "inherit"
    return f"read {read_level}/write {write_level}, {status}"


async def _area_detail_screen(session: Session, lane: DatabaseLane, actor: User, area: FileArea) -> None:
    await _draw_area_detail(session, lane, area)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "e":
            await session.write_line("")
            updated = await _edit_area_screen(session, lane, actor, area)
            if updated is not None:
                area = updated
            await _draw_area_detail(session, lane, area)
        elif choice == "d":
            await session.write_line("")
            deleted = await _delete_area_screen(session, lane, actor, area)
            if deleted:
                return
            await _draw_area_detail(session, lane, area)
        elif choice == "p":
            await session.write_line("")
            await _pending_files_screen(session, lane, actor, area)
            await _draw_area_detail(session, lane, area)
        else:
            await session.write(reject_keystroke())


async def _draw_area_detail(session: Session, lane: DatabaseLane, area: FileArea) -> None:
    header = colored(sanitize_text(area.name), fg_color=HEADER_COLOR, bold=True)
    await session.write_line(f"\r\n{header}")
    await session.write_line(f"Description: {sanitize_text(area.description) if area.description else '(none)'}")
    await session.write_line(f"Community: {await lane.run(_community_label, area.community_id)}")
    read_level = area.min_read_level if area.min_read_level is not None else "inherit"
    write_level = area.min_write_level if area.min_write_level is not None else "inherit"
    await session.write_line(f"Read level: {read_level}  Write level: {write_level}")
    await session.write_line(
        f"Pinned: {'yes' if area.pinned else 'no'}  Moderated: {'yes' if area.moderated else 'no'}"
    )
    age = area.max_file_age_days if area.max_file_age_days is not None else "unlimited"
    await session.write_line(f"Max file age: {age} days")
    await session.write_line(
        f"Minimum age: {area.min_age if area.min_age is not None else 'none'}  "
        f"Name requirement: {area.name_requirement or 'none'}"
    )
    options = "  ".join(
        [menu_key("E", "dit"), menu_key("D", "elete"), menu_key("P", "ending files"), menu_key("B", "ack")]
    )
    await session.write_line(f"\r\n{options}")
    await session.write("Choice: ")


async def _edit_area_screen(session: Session, lane: DatabaseLane, actor: User, area: FileArea) -> FileArea | None:
    await session.write(f"Name [{area.name}]: ")
    name = (await session.read_line()).strip() or area.name
    await session.write(f"Description [{area.description or '(none)'}]: ")
    description = (await session.read_line()).strip() or area.description
    min_read_level, ok = await _prompt_optional_int(session, "Minimum read level", current=area.min_read_level)
    if not ok:
        return None
    min_write_level, ok = await _prompt_optional_int(session, "Minimum write level", current=area.min_write_level)
    if not ok:
        return None
    change_community = await prompt_yes_no(session, "Change Community?", default=False)
    community_id = area.community_id
    if change_community:
        community_id = await _pick_optional_community(session, lane)
    change_category = await prompt_yes_no(session, "Change category?", default=False)
    category_id = area.category_id
    if change_category:
        category_id = await _pick_optional_category(
            session, lane, list_top_level=list_top_level_file_categories,
            list_subcategories=list_file_subcategories, title="File-area category",
            community_id=community_id, resources=await lane.run(list_file_areas),
        )
    pinned = await prompt_yes_no_or_keep(session, "Pinned?", current=area.pinned)
    moderated = await prompt_yes_no_or_keep(session, "Moderated?", current=area.moderated)
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
    min_age, ok = await _prompt_min_age(session, current=area.min_age)
    if not ok:
        return None
    name_requirement, ok = await _prompt_name_requirement(session, current=area.name_requirement)
    if not ok:
        return None

    try:
        updated = await lane.run(
            update_file_area,
            area, name=name, description=description, min_read_level=min_read_level,
            min_write_level=min_write_level, category_id=category_id, pinned=pinned,
            moderated=moderated, max_file_age_days=max_file_age_days,
            min_age=min_age, name_requirement=name_requirement,
            community_id=community_id, changed_by=actor,
        )
    except FileAreaError as exc:
        await session.write_line(colored(f"Could not update file area: {exc}", fg_color=MUTED_COLOR))
        return None
    await session.write_line(f"Updated {updated.name!r}.")
    return updated


async def _delete_area_screen(session: Session, lane: DatabaseLane, actor: User, area: FileArea) -> bool:
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
    await lane.run(delete_file_area, area, deleted_by=actor)
    await session.write_line(f"{area.name!r} deleted.")
    return True


async def _pending_files_screen(session: Session, lane: DatabaseLane, actor: User, area: FileArea) -> None:
    while True:
        files = await lane.run(list_pending_files, area, requesting_user=actor)
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
        await _file_action_screen(session, lane, actor, selected)


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


async def _file_action_screen(session: Session, lane: DatabaseLane, actor: User, entry: FileEntry) -> None:
    await _draw_file_action(session, entry)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "a":
            await session.write_line("")
            await lane.run(approve_file, entry, approved_by=actor)
            await session.write_line("Approved.")
            return
        elif choice == "r":
            await session.write_line("")
            await lane.run(delete_file, entry, deleted_by=actor)
            await session.write_line("Rejected.")
            return
        elif choice == "p":
            await session.write_line("")
            entry = await lane.run(set_file_pinned, entry, not entry.pinned, changed_by=actor)
            await _draw_file_action(session, entry)
        elif choice == "x":
            await session.write_line("")
            entry = await lane.run(set_file_exempt, entry, not entry.exempt_from_expiry, changed_by=actor)
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


async def _channel_menu(session: Session, lane: DatabaseLane, actor: User) -> None:
    await _draw_channel_menu(session)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "c":
            await session.write_line("")
            await _create_channel_screen(session, lane, actor)
            await _draw_channel_menu(session)
        elif choice == "l":
            await session.write_line("")
            await _list_channels_screen(session, lane, actor)
            await _draw_channel_menu(session)
        else:
            await session.write(reject_keystroke())


async def _draw_channel_menu(session: Session) -> None:
    header = colored("Channels:", fg_color=HEADER_COLOR, bold=True)
    options = "  ".join([menu_key("C", "reate"), menu_key("L", "ist"), menu_key("B", "ack")])
    await session.write_line(f"\r\n{header} {options}")
    await session.write("Choice: ")


async def _create_channel_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
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
    community_id = await _pick_optional_community(session, lane)
    category_id = await _pick_optional_category(
        session, lane, list_top_level=list_top_level_channel_categories,
        list_subcategories=list_channel_subcategories, title="Channel category",
        community_id=community_id, resources=await lane.run(list_channels),
    )
    pinned = await prompt_yes_no(session, "Pinned?", default=False)
    hidden = await prompt_yes_no(session, "Hidden (omitted from listings)?", default=False)
    members_only = await prompt_yes_no(session, "Members-only (invite-only access)?", default=False)
    allow_member_invites = False
    if members_only:
        allow_member_invites = await prompt_yes_no(session, "Allow members to invite others?", default=False)
    min_age, ok = await _prompt_min_age(session, current=None)
    if not ok:
        return
    name_requirement, ok = await _prompt_name_requirement(session, current=None)
    if not ok:
        return

    try:
        channel = await lane.run(
            create_channel,
            name, description=description, min_level=min_level, category_id=category_id,
            pinned=pinned, hidden=hidden, members_only=members_only,
            allow_member_invites=allow_member_invites,
            min_age=min_age, name_requirement=name_requirement,
            community_id=community_id, creator=actor,
        )
    except ChannelError as exc:
        await session.write_line(colored(f"Could not create channel: {exc}", fg_color=MUTED_COLOR))
        return
    await session.write_line(f"Created channel {channel.name!r}.")


async def _list_channels_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    channels = await lane.run(list_channels)
    selected = await pick_item(
        session, channels,
        name_of=lambda c: c.name,
        stable_id_of=lambda c: c.id,
        description_of=_channel_description,
        title="Channels",
        empty_message="No channels yet.",
    )
    if selected is not None:
        await _channel_detail_screen(session, lane, actor, selected)


def _channel_description(channel: Channel) -> str:
    bits = [f"level {channel.min_level}"]
    if channel.members_only:
        bits.append("members-only")
    if channel.hidden:
        bits.append("hidden")
    return ", ".join(bits)


async def _channel_detail_screen(session: Session, lane: DatabaseLane, actor: User, channel: Channel) -> None:
    await _draw_channel_detail(session, lane, channel)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "e":
            await session.write_line("")
            updated = await _edit_channel_screen(session, lane, actor, channel)
            if updated is not None:
                channel = updated
            await _draw_channel_detail(session, lane, channel)
        elif choice == "d":
            await session.write_line("")
            deleted = await _delete_channel_screen(session, lane, actor, channel)
            if deleted:
                return
            await _draw_channel_detail(session, lane, channel)
        else:
            await session.write(reject_keystroke())


async def _draw_channel_detail(session: Session, lane: DatabaseLane, channel: Channel) -> None:
    header = colored(sanitize_text(channel.name), fg_color=HEADER_COLOR, bold=True)
    await session.write_line(f"\r\n{header}")
    await session.write_line(
        f"Description: {sanitize_text(channel.description) if channel.description else '(none)'}"
    )
    await session.write_line(f"Community: {await lane.run(_community_label, channel.community_id)}")
    await session.write_line(f"Minimum level: {channel.min_level}")
    await session.write_line(
        f"Pinned: {'yes' if channel.pinned else 'no'}  Hidden: {'yes' if channel.hidden else 'no'}"
    )
    await session.write_line(
        f"Members-only: {'yes' if channel.members_only else 'no'}  "
        f"Allow member invites: {'yes' if channel.allow_member_invites else 'no'}"
    )
    await session.write_line(
        f"Minimum age: {channel.min_age if channel.min_age is not None else 'none'}  "
        f"Name requirement: {channel.name_requirement or 'none'}"
    )
    options = "  ".join([menu_key("E", "dit"), menu_key("D", "elete"), menu_key("B", "ack")])
    await session.write_line(f"\r\n{options}")
    await session.write("Choice: ")


async def _edit_channel_screen(session: Session, lane: DatabaseLane, actor: User, channel: Channel) -> Channel | None:
    await session.write(f"Name [{channel.name}]: ")
    name = (await session.read_line()).strip() or channel.name
    await session.write(f"Description [{channel.description or '(none)'}]: ")
    description = (await session.read_line()).strip() or channel.description
    await session.write(f"Minimum level [{channel.min_level}]: ")
    min_level = await _read_int(session, default=channel.min_level)
    if min_level is None:
        return None
    change_community = await prompt_yes_no(session, "Change Community?", default=False)
    community_id = channel.community_id
    if change_community:
        community_id = await _pick_optional_community(session, lane)
    change_category = await prompt_yes_no(session, "Change category?", default=False)
    category_id = channel.category_id
    if change_category:
        category_id = await _pick_optional_category(
            session, lane, list_top_level=list_top_level_channel_categories,
            list_subcategories=list_channel_subcategories, title="Channel category",
            community_id=community_id, resources=await lane.run(list_channels),
        )
    pinned = await prompt_yes_no_or_keep(session, "Pinned?", current=channel.pinned)
    hidden = await prompt_yes_no_or_keep(session, "Hidden?", current=channel.hidden)
    members_only = await prompt_yes_no_or_keep(session, "Members-only?", current=channel.members_only)
    allow_member_invites = await prompt_yes_no_or_keep(
        session, "Allow member invites?", current=channel.allow_member_invites
    )
    min_age, ok = await _prompt_min_age(session, current=channel.min_age)
    if not ok:
        return None
    name_requirement, ok = await _prompt_name_requirement(session, current=channel.name_requirement)
    if not ok:
        return None

    try:
        updated = await lane.run(
            update_channel,
            channel, name=name, description=description, min_level=min_level,
            category_id=category_id, pinned=pinned, hidden=hidden, members_only=members_only,
            allow_member_invites=allow_member_invites,
            min_age=min_age, name_requirement=name_requirement,
            community_id=community_id, changed_by=actor,
        )
    except ChannelError as exc:
        await session.write_line(colored(f"Could not update channel: {exc}", fg_color=MUTED_COLOR))
        return None
    await session.write_line(f"Updated {updated.name!r}.")
    return updated


async def _delete_channel_screen(session: Session, lane: DatabaseLane, actor: User, channel: Channel) -> bool:
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
    await lane.run(delete_channel, channel, deleted_by=actor)
    await session.write_line(f"{channel.name!r} deleted.")
    return True


# -- categories ----------------------------------------------------------


async def _category_menu(session: Session, lane: DatabaseLane, actor: User) -> None:
    await _draw_category_menu(session)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "m":
            await session.write_line("")
            await _generic_category_screen(
                session, lane, actor,
                create=create_board_category, list_top_level=list_top_level_board_categories,
                list_subcategories=list_board_subcategories, delete=delete_board_category,
                error_type=CategoryError, title="Board categories",
            )
            await _draw_category_menu(session)
        elif choice == "f":
            await session.write_line("")
            await _generic_category_screen(
                session, lane, actor,
                create=create_file_category, list_top_level=list_top_level_file_categories,
                list_subcategories=list_file_subcategories, delete=delete_file_category,
                error_type=FileCategoryError, title="File-area categories",
            )
            await _draw_category_menu(session)
        elif choice == "c":
            await session.write_line("")
            await _generic_category_screen(
                session, lane, actor,
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
            menu_key("C", "hannel category"),
            menu_key("B", "ack"),
        ]
    )
    await session.write_line(f"\r\n{header} {options}")
    await session.write("Choice: ")


async def _generic_category_screen(
    session: Session, lane: DatabaseLane, actor: User, *, create, list_top_level, list_subcategories, delete,
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
                session, lane, actor, create=create, list_top_level=list_top_level, error_type=error_type,
            )
            await _draw_generic_category_menu(session, title)
        elif choice == "l":
            await session.write_line("")
            await _list_categories_screen(
                session, lane, actor, list_top_level=list_top_level,
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
    session: Session, lane: DatabaseLane, actor: User, *, create, list_top_level, error_type
) -> None:
    await session.write("Name: ")
    name = (await session.read_line()).strip()
    if not name:
        await session.write_line(colored("Cancelled: name cannot be blank.", fg_color=MUTED_COLOR))
        return
    await session.write("Description (optional): ")
    description = (await session.read_line()).strip() or None
    parent_category_id = None
    if await prompt_yes_no(session, "Make this a sub-category of an existing one?", default=False):
        parent = await pick_item(
            session, await lane.run(list_top_level),
            name_of=lambda c: c.name, stable_id_of=lambda c: c.id,
            title="Parent category", empty_message="No top-level categories exist yet.",
        )
        parent_category_id = parent.id if parent is not None else None
    try:
        category = await lane.run(
            create, name, description=description, parent_category_id=parent_category_id, created_by=actor
        )
    except error_type as exc:
        await session.write_line(colored(f"Could not create category: {exc}", fg_color=MUTED_COLOR))
        return
    await session.write_line(f"Created category {category.name!r}.")


async def _list_categories_screen(
    session: Session, lane: DatabaseLane, actor: User, *, list_top_level, list_subcategories, delete
) -> None:
    def _load(db: Database) -> list:
        top_level = list_top_level(db)
        all_categories = list(top_level)
        for top in top_level:
            all_categories.extend(list_subcategories(db, top.id))
        return all_categories

    all_categories = await lane.run(_load)
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
    await lane.run(delete, selected, deleted_by=actor)
    await session.write_line(f"{selected.name!r} deleted.")


# -- moderator grants -----------------------------------------------------


async def _pick_moderator_scope(session: Session, lane: DatabaseLane) -> tuple[str, int | None, str, int | None] | None:
    """Returns `(object_type, object_id, human label, community_id)`,
    or `None` if cancelled. `object_id=None` means a blanket grant
    (design doc -- board/area management round; channel scope added in
    the channel management round) -- `community_id` further narrows a
    blanket grant to one Community's membership (design doc §16, round
    84's Community-blanket tier) instead of the whole node;
    `community_id` is always `None` for a per-object grant (`[B]oard`/
    `[A]rea`/`Cha[N]nel`), since a specific object's own `community_id`
    already answers that question without needing it duplicated on the
    grant."""
    await session.write(
        "Scope: [B]oard, [A]rea, Cha[N]nel, blanket across all boards [X], "
        "blanket across all areas [Y], blanket across all channels [Z]: "
    )
    scope_key = (await session.read_key()).lower()
    await session.write_line("")
    if scope_key == "b":
        board = await pick_item(
            session, await lane.run(list_boards, order_by="alphabetical"),
            name_of=lambda b: b.name, stable_id_of=lambda b: b.id,
            title="Which board?", empty_message="No boards yet.",
        )
        if board is None:
            return None
        return "board", board.id, f"board {board.name!r}", None
    elif scope_key == "a":
        area = await pick_item(
            session, await lane.run(list_file_areas, order_by="alphabetical"),
            name_of=lambda a: a.name, stable_id_of=lambda a: a.id,
            title="Which file area?", empty_message="No file areas yet.",
        )
        if area is None:
            return None
        return "file_area", area.id, f"file area {area.name!r}", None
    elif scope_key == "n":
        channel = await pick_item(
            session, await lane.run(list_channels),
            name_of=lambda c: c.name, stable_id_of=lambda c: c.id,
            title="Which channel?", empty_message="No channels yet.",
        )
        if channel is None:
            return None
        return "channel", channel.id, f"channel {channel.name!r}", None
    elif scope_key == "x":
        object_type, label = "board", "all boards (blanket)"
    elif scope_key == "y":
        object_type, label = "file_area", "all file areas (blanket)"
    elif scope_key == "z":
        object_type, label = "channel", "all channels (blanket)"
    else:
        await session.write_line(colored("Not a valid scope -- cancelled.", fg_color=MUTED_COLOR))
        return None

    community_id = await _pick_optional_community_blanket_scope(session, lane)
    if community_id is not None:
        community = await lane.run(get_community, community_id)
        label = f"{label} scoped to Community {community.name!r}"
    return object_type, None, label, community_id


async def _pick_optional_community_blanket_scope(session: Session, lane: DatabaseLane) -> int | None:
    """The blanket-grant-scoping follow-up (design doc §16, round 84):
    'Scope this blanket grant to one Community instead of the whole
    node?' -- extends the existing X/Y/Z blanket keys rather than
    adding new ones, per that round's own decision. Returns the chosen
    Community's id, or `None` for an ordinary node-wide (local-)blanket
    grant."""
    if not await prompt_yes_no(
        session, "Scope this blanket grant to one Community instead of the whole node?", default=False
    ):
        return None
    selected = await pick_item(
        session, await lane.run(list_communities),
        name_of=lambda c: c.name,
        stable_id_of=lambda c: c.id,
        title="Community",
        empty_message="No Communities exist yet.",
    )
    return selected.id if selected is not None else None


async def _grant_moderator_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    target = await pick_item(
        session, await lane.run(list_users),
        name_of=lambda u: u.username, stable_id_of=lambda u: u.id,
        title="Grant moderator to which user?", empty_message="No registered users yet.",
    )
    if target is None:
        return
    scope = await _pick_moderator_scope(session, lane)
    if scope is None:
        return
    object_type, object_id, label, community_id = scope

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

    if not await prompt_yes_no(
        session, f"Grant {preset_label!r} on {label} to {target.username!r}?", default=False
    ):
        await session.write_line("Cancelled.")
        return

    await lane.run(
        grant_permissions,
        target, object_type=object_type, object_id=object_id, permissions=permissions,
        granted_by=actor, community_id=community_id,
    )
    await session.write_line(f"Granted {preset_label} on {label} to {target.username!r}.")


async def _revoke_moderator_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    target = await pick_item(
        session, await lane.run(list_users),
        name_of=lambda u: u.username, stable_id_of=lambda u: u.id,
        title="Revoke moderator from which user?", empty_message="No registered users yet.",
    )
    if target is None:
        return
    scope = await _pick_moderator_scope(session, lane)
    if scope is None:
        return
    object_type, object_id, label, community_id = scope

    grant = await lane.run(
        get_grant, target, object_type=object_type, object_id=object_id, community_id=community_id
    )
    if grant is None:
        await session.write_line(colored(f"{target.username!r} has no grant on {label}.", fg_color=MUTED_COLOR))
        return

    if not await prompt_yes_no(session, f"Revoke all permissions for {target.username!r} on {label}?", default=False):
        await session.write_line("Cancelled.")
        return

    permission_enum = ChannelPermission if object_type == "channel" else BoardPermission
    await lane.run(
        revoke_permissions,
        target, object_type=object_type, object_id=object_id,
        permissions=permission_enum(grant.permissions), revoked_by=actor, community_id=community_id,
    )
    await session.write_line(f"Revoked {target.username!r}'s grant on {label}.")
