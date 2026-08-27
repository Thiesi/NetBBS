"""
Shared SysOp admin menu (design doc -- SysOp foundation).

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
draw function, a bell-only-on-invalid-key dispatch loop (design doc),
and `netbbs.net.picker.pick_item` for target selection.

Migrated onto the two-lane database execution model (design doc,
issue #57) -- every screen/menu
function here takes `lane: DatabaseLane` instead of `db: Database`,
and every direct domain-function call goes through `await lane.run(...)`.
Almost entirely mechanical (no
synchronous-callback contracts here the way the Tab completer in
chat_flow.py has) --
`pick_item`'s own `name_of`/`description_of` callbacks in this file only
ever read attributes already present on the objects handed to
`pick_item`, never a fresh DB call, except one: `_who_screen`'s
`description_of` cannot call `format_for_display(entry.connected_at,
db)` directly inside its lambda, since a synchronous callback cannot
dispatch through a lane -- it instead uses the same fix as elsewhere
(`resolve_display_preferences`, fetched once via
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
itself once inside `admin_menu` (that's `node_controls`' job,
established independently).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import shlex
import sys
from pathlib import Path
from typing import Awaitable, Callable, Sequence

import nacl.signing

from netbbs.auth.users import (
    AuthError,
    User,
    UserManagementError,
    approve_pending_user,
    create_user,
    delete_user,
    get_user_by_id,
    get_user_by_username,
    list_users,
    set_can_verify_identity,
    set_user_disabled,
    set_user_level,
    set_verify_key,
)
from netbbs.backup import get_last_backup_summary
from netbbs.boards.boards import Board, BoardError, create_board, delete_board, list_boards, update_board
from netbbs.boards.categories import Category, CategoryError
from netbbs.boards.categories import create_category as create_board_category
from netbbs.boards.categories import delete_category as delete_board_category
from netbbs.boards.categories import get_category_by_id as get_board_category_by_id
from netbbs.boards.categories import list_subcategories as list_board_subcategories
from netbbs.boards.categories import list_top_level_categories as list_top_level_board_categories
from netbbs.boards.posts import (
    Post,
    PostError,
    approve_post,
    count_visible_posts,
    delete_post,
    list_pending_posts,
    set_post_exempt,
    set_post_pinned,
)
from netbbs.chat.moderation import ChannelRestriction, list_active_channel_restrictions, unban_user, unmute_user
from netbbs.chat.categories import CategoryError as ChannelCategoryError
from netbbs.chat.categories import create_category as create_channel_category
from netbbs.chat.categories import delete_category as delete_channel_category
from netbbs.chat.categories import get_category_by_id as get_channel_category_by_id
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
from netbbs.config import (
    MAX_NODE_DISPLAY_NAME_LENGTH,
    RegistrationMode,
    get_node_display_name,
    get_registration_mode,
    set_node_display_name,
    set_registration_mode,
)
from netbbs.doors import (
    Door,
    DoorError,
    create_door,
    custom_doors_dir,
    delete_door,
    get_door_by_name,
    list_doors,
    update_door,
)
from netbbs.doors.bundled import available_bundled_doors
from netbbs.files.areas import FileArea, FileAreaError, create_file_area, delete_file_area, list_file_areas, update_file_area
from netbbs.files.categories import FileAreaCategory
from netbbs.files.categories import FileAreaCategoryError as FileCategoryError
from netbbs.files.categories import create_category as create_file_category
from netbbs.files.categories import delete_category as delete_file_category
from netbbs.files.categories import get_category_by_id as get_file_area_category_by_id
from netbbs.files.categories import list_subcategories as list_file_subcategories
from netbbs.files.categories import list_top_level_categories as list_top_level_file_categories
from netbbs.files.gc import GCReport, reclaim_orphaned_blobs
from netbbs.files.entries import (
    FileEntry,
    approve_file,
    count_visible_files,
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
    carried_board_count,
    close_board_if_linked,
    is_board_closed,
    is_board_linked,
    is_board_origin_orphaned,
    link_board,
    offer_board_origin_transfer,
    queue_board_post_if_linked,
    rebuild_carried_post_materialization,
)
from netbbs.link.channels import LinkChannelsError, is_channel_linked, link_channel
from netbbs.link.diagnostics import (
    DiagnosticLogEntry,
    list_diagnostic_log_entries,
    list_diagnostic_log_entries_since,
)
from netbbs.link.files import LinkFilesError, is_area_linked, link_file_area
from netbbs.link.protocol import PeerRecord
from netbbs.link.relay_mailbox import mailbox_sizes
from netbbs.link.reliability import reliability_score
from netbbs.link.remote_attestation import (
    clear_remote_attestation_override,
    configure_attestation_authority,
    get_remote_attestation_state,
    list_attestation_authorities,
    list_remote_attestation_audit,
    list_remote_attestation_overrides,
    remove_attestation_authority,
    set_remote_attestation_override,
)
from netbbs.link.seedlist import get_cached_supplementary_seeds
from netbbs.link.store import load_peer_last_contact
from netbbs.link.trust import (
    TrustDimension,
    TrustState,
    TrustSubject,
    clear_trust_override,
    configure_sole_authority,
    configure_trust_anchor,
    configure_trust_domain,
    configure_trusted_reporter,
    get_effective_trust_state,
    list_sole_authorities,
    list_trust_anchors,
    list_trust_config_audit,
    list_trust_decision_audit,
    list_trust_domains,
    list_trust_overrides,
    list_trust_subjects,
    list_trusted_reporters,
    remove_sole_authority,
    remove_trust_anchor,
    remove_trusted_reporter,
    set_trust_override,
)
from netbbs.link.mail import unexpire_link_message_delivery
from netbbs.link.work_items import (
    KIND_LINK_MAIL_DELIVERY,
    WorkItem,
    cancel_work_item,
    list_work_items,
    replay_work_item,
)
from netbbs.moderation.blocklist import BlocklistError, block_user, is_blocked, unblock_user
from netbbs.moderation.log import list_actions_for_target_user, list_recent_actions, record_action
from netbbs.moderation.roles import (
    BoardPermission,
    ChannelPermission,
    get_grant,
    grant_permissions,
    list_grants_for_community,
    revoke_permissions,
)
from netbbs.net.char_input import (
    HELP_KEY,
    REDRAW_KEY,
    REFRESH_KEY,
    EditorKey,
    EditorKeyKind,
    reject_unhandled_key,
)
from netbbs.net.confirm import prompt_yes_no, prompt_yes_no_or_keep
from netbbs.net.draft_storage import DraftPruneReport, prune_stale_drafts
from netbbs.net.help_overlay import show_help
from netbbs.net.picker import pick_item
from netbbs.net.resource_editor import (
    FieldSpec,
    bool_field,
    choice_field,
    choice_step,
    edit_resource_draft,
    text_field,
)
from netbbs.net.session import Session
from netbbs.net.session_registry import SessionSummary
from netbbs.net.shutdown import (
    NodeControls,
    format_remaining_seconds,
    run_drain_sequence,
    run_shutdown_sequence,
)
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.redraw_preference import (
    redraw_in_place_enabled,
    redraw_in_place_ever_set,
    set_redraw_in_place_enabled,
)
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.color_depth_preference import effective_truecolor
from netbbs.net.node_theme import (
    accent_color_override,
    clock_color_override,
    effective_accent_color_256,
    effective_header_color_256,
    header_color_override,
    node_name_gradient_override,
    set_accent_color_override,
    set_clock_color_override,
    set_header_color_override,
    set_node_name_gradient_override,
)
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.operational_history import list_operational_run_history
from netbbs.selfupdate import (
    UpdateError,
    check_latest_release,
    clear_github_pat,
    get_auto_update_check_enabled,
    get_github_pat,
    get_last_check_summary,
    is_newer,
    load_release_cache,
    masked_github_pat,
    record_check_outcome,
    save_release_cache,
    set_auto_update_check_enabled,
    set_github_pat,
)
from netbbs.net.ansi_editor import edit_ansi_art
from netbbs.net.welcome_banner import (
    MAX_BANNER_SIZE_BYTES,
    banner_path,
    load_welcome_banner,
    set_welcome_banner_enabled,
    welcome_banner_status,
)
from netbbs.net.banner_presets import (
    MAIN_MENU_BANNER_PRESETS,
    WELCOME_BANNER_PRESETS,
    load_main_menu_banner_preset,
    load_welcome_banner_preset,
)
from netbbs.net.main_menu_banner import (
    MAX_MASTHEAD_SIZE_BYTES,
    load_main_menu_banner,
    main_menu_banner_path,
    main_menu_banner_status,
    set_main_menu_banner_enabled,
)
from netbbs.net.logoff_banner import (
    MAX_LOGOFF_BANNER_SIZE_BYTES,
    load_logoff_banner,
    logoff_banner_path,
    logoff_banner_status,
    set_logoff_banner_enabled,
)
from netbbs.net.new_account_banner_before import (
    MAX_NEW_ACCOUNT_BANNER_BEFORE_SIZE_BYTES,
    load_new_account_banner_before,
    new_account_banner_before_path,
    new_account_banner_before_status,
    set_new_account_banner_before_enabled,
)
from netbbs.net.new_account_banner_after import (
    MAX_NEW_ACCOUNT_BANNER_AFTER_SIZE_BYTES,
    load_new_account_banner_after,
    new_account_banner_after_path,
    new_account_banner_after_status,
    set_new_account_banner_after_enabled,
)
from netbbs.net.board_list_banner import (
    MAX_BOARD_LIST_BANNER_SIZE_BYTES,
    board_list_banner_path,
    board_list_banner_status,
    load_board_list_banner,
    set_board_list_banner_enabled,
)
from netbbs.net.file_area_banner import (
    MAX_FILE_AREA_BANNER_SIZE_BYTES,
    file_area_banner_path,
    file_area_banner_status,
    load_file_area_banner,
    set_file_area_banner_enabled,
)
from netbbs.net.chat_channel_picker_banner import (
    MAX_CHAT_CHANNEL_PICKER_BANNER_SIZE_BYTES,
    chat_channel_picker_banner_path,
    chat_channel_picker_banner_status,
    load_chat_channel_picker_banner,
    set_chat_channel_picker_banner_enabled,
)
from netbbs.rendering import (
    ACCENT_COLOR,
    ALERT_COLOR,
    CLOCK_COLOR,
    ERROR_COLOR,
    HEADER_COLOR,
    LABEL_COLOR,
    MENU_KEY_COLOR,
    METADATA_COLOR,
    MUTED_COLOR,
    RESET,
    SUCCESS_COLOR,
    VALUE_COLOR,
    WARNING_COLOR,
    MenuEntry,
    action_bar,
    badge,
    colored,
    colored_truncate,
    counts_row,
    cut_to_width,
    GRADIENTS,
    decode_ansi_bytes,
    double_frame,
    empty_state,
    field_row,
    gradient_text,
    menu_grid,
    menu_key,
    nearest_256,
    reflow,
    reject_keystroke,
    sanitize_text,
    screen_title,
    status_badge,
    truncate,
)
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import (
    format_for_display,
    resolve_display_preferences,
    set_display_format,
    set_display_timezone,
)

# Design doc -- node management, Thiesi's own request: an operator
# watching this node's own stdout/journal should see a shutdown/drain
# being scheduled (or maintenance mode toggled) as it happens, not only
# via the DB-backed moderation log (`record_action`, queried separately).
_logger = logging.getLogger(__name__)


def _menu_row(entries: list[MenuEntry], description_level: str, *, width: int, height: int) -> str:
    """Shared off/on branch for every hotkey-row menu in this module
    (issue #160's rollout, commit 1b1da33's own established pattern):
    `menu_grid` always renders one entry per line, even with
    descriptions off, so it isn't a byte-for-byte substitute for
    `action_bar`'s packed row at that level -- keep `action_bar` there
    so no screen's height changes for a caller who hasn't opted into
    descriptions, and only switch to `menu_grid` once they have."""
    if description_level == "off":
        return action_bar([e.label for e in entries], width=width)
    return menu_grid([("", entries)], width=width, height=height, description_level=description_level)


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

    `node_controls` (design doc), if given,
    unlocks the `[N]ode` quick action and its entry in `[O]perations`
    (list/disconnect sessions, trigger shutdown) -- present when called
    from within a live session (`netbbs.net.login_flow`), absent
    (`None`) when called from the standalone `python -m netbbs.admin`
    CLI, which has no access to a running node's live in-memory state
    at all (confirmed design decision, not an oversight -- see that
    module's docstring).

    `link_context` (design doc), if given, unlocks the
    `[L]ink this board` command inside the board-management screens --
    same presence/absence reasoning as `node_controls`: absent for the
    standalone CLI and for any node with Link disabled.

    The landing view is an at-a-glance operations dashboard. Navigation
    separates users, content, running-node operations, and durable settings;
    context-sensitive quick actions avoid forcing an operator through that
    hierarchy when responding to a visible condition. Historical `[M]anage`
    and `[S]ystem` operation keys remain accepted as compatibility aliases but
    are no longer advertised in the reorganized console.
    """
    dashboard_state = await _draw_admin_menu(
        session, lane, user, node_controls=node_controls, link_context=link_context
    )
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "r":
            dashboard_state = await _draw_admin_menu(
                session, lane, user, node_controls=node_controls, link_context=link_context
            )
        elif choice == "u":
            await session.write_line("")
            await _users_menu(session, lane, user, node_controls=node_controls)
            dashboard_state = await _draw_admin_menu(
                session, lane, user, node_controls=node_controls, link_context=link_context
            )
        elif choice in {"c", "m"}:
            await session.write_line("")
            await _content_menu(session, lane, user, link_context=link_context)
            dashboard_state = await _draw_admin_menu(
                session, lane, user, node_controls=node_controls, link_context=link_context
            )
        elif choice == "o":
            await session.write_line("")
            await _operations_menu(
                session, lane, user, node_controls=node_controls, link_context=link_context
            )
            await _draw_admin_menu(session, lane, user, node_controls=node_controls,
                                   link_context=link_context, state=dashboard_state)
        elif choice == "s":
            await session.write_line("")
            await _system_menu(session, lane, user, node_controls=node_controls, link_context=link_context)
            await _draw_admin_menu(session, lane, user, node_controls=node_controls,
                                   link_context=link_context, state=dashboard_state)
        elif choice == "n" and node_controls is not None:
            await session.write_line("")
            await _node_menu(session, lane, user, node_controls)
            await _draw_admin_menu(session, lane, user, node_controls=node_controls,
                                   link_context=link_context, state=dashboard_state)
        elif choice == "l" and link_context is not None:
            await session.write_line("")
            await _link_status_screen(session, lane, user, link_context=link_context)
            await _draw_admin_menu(session, lane, user, node_controls=node_controls,
                                   link_context=link_context, state=dashboard_state)
        elif choice == "x" and link_context is not None:
            await session.write_line("")
            await _outbox_screen(session, lane, user)
            dashboard_state = await _draw_admin_menu(
                session, lane, user, node_controls=node_controls, link_context=link_context
            )
        elif choice == "k":
            await session.write_line("")
            await _backup_status_screen(session, lane, user)
            await _draw_admin_menu(session, lane, user, node_controls=node_controls,
                                   link_context=link_context, state=dashboard_state)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_admin_menu(
    session: Session,
    lane: DatabaseLane,
    actor: User,
    *,
    node_controls: NodeControls | None,
    link_context: LinkContext | None,
    state: dict[str, object] | None = None,
) -> dict[str, object]:
    """Render the SysOp landing page as an operations overview, not a link list."""

    def _load(db: Database) -> dict[str, object]:
        all_users = list_users(db)
        all_boards = list_boards(db)
        all_areas = list_file_areas(db)
        pending_users = sum(user.pending_approval for user in all_users)
        pending_posts = sum(len(list_pending_posts(db, board, requesting_user=actor)) for board in all_boards)
        pending_files = sum(
            len(list_pending_files(db, area, requesting_user=actor)) for area in all_areas
        )
        dead_letters = len(list_work_items(db, status="dead_lettered")) if link_context is not None else 0
        recent_diagnostics = list_diagnostic_log_entries(db, limit=20) if link_context is not None else []
        return {
            "pending_users": pending_users,
            "pending_posts": pending_posts,
            "pending_files": pending_files,
            # Dogfood follow-up: the dashboard previously showed only
            # *pending* counts (always 0 on a quiet node) with no sense
            # of overall node scale at all -- a SysOp glancing at the
            # landing screen couldn't tell how many users/boards/posts/
            # files actually exist.
            "total_users": len(all_users),
            "total_boards": len(all_boards),
            "total_posts": sum(count_visible_posts(db, board)[0] for board in all_boards),
            "total_areas": len(all_areas),
            "total_files": sum(count_visible_files(db, area)[0] for area in all_areas),
            "dead_letters": dead_letters,
            "recent_errors": sum(entry.level == "ERROR" for entry in recent_diagnostics),
            "recent_warnings": sum(entry.level == "WARNING" for entry in recent_diagnostics),
            "backup": get_last_backup_summary(db),
            "update": get_last_check_summary(db),
            "description_level": menu_description_level(db, actor),
            "redraw_in_place": redraw_in_place_enabled(db, actor),
            "unicode_style": unicode_style_enabled(db, actor),
            "collapsed": breadcrumb_collapsed_enabled(db, actor),
            "header_color": effective_header_color_256(db),
        }

    if state is None:
        state = await lane.run(_load)
    active_sessions = len(node_controls.session_registry) if node_controls is not None else None
    maintenance = node_controls.maintenance.is_active() if node_controls is not None else False
    lockdown = node_controls.maintenance.is_lockdown_active() if node_controls is not None else False

    unicode_style = state["unicode_style"]
    collapsed = state["collapsed"]
    await session.write_line(
        "\r\n" + screen_title(
            "SysOp operations console",
            breadcrumb=(session.node_display_name,),
            subtitle="Live health, attention queues, and administrative controls.",
            width=session.terminal_width,
            clear=state["redraw_in_place"],
            unicode_style=unicode_style, collapsed=collapsed,
            header_color=state["header_color"],
        node_name_gradient=session.node_name_gradient)
    )

    node_badge = status_badge(
        "LOCKDOWN" if lockdown else "MAINTENANCE" if maintenance else "ONLINE",
        tone="error" if lockdown else "warning" if maintenance else "success",
        unicode_style=unicode_style,
    ) if node_controls is not None else status_badge("LOCAL ADMIN", tone="neutral", unicode_style=unicode_style)
    health: list[str] = [colored("NODE  ", fg_color=LABEL_COLOR, bold=True) + node_badge]
    if active_sessions is not None:
        health.append(counts_row([("  Active sessions", active_sessions)]))
    else:
        health.append(colored("  Live node controls unavailable in standalone mode.", fg_color=MUTED_COLOR))

    if link_context is None:
        health.append(colored("LINK  ", fg_color=LABEL_COLOR, bold=True) + status_badge("DISABLED", tone="neutral", unicode_style=unicode_style))
    else:
        node = link_context.link_node
        link_tone = "warning" if not node.peers or state["dead_letters"] else "success"
        link_label = "ATTENTION" if link_tone == "warning" else "HEALTHY"
        health.append(colored("LINK  ", fg_color=LABEL_COLOR, bold=True) + status_badge(link_label, tone=link_tone, unicode_style=unicode_style))
        health.append(
            "  " + counts_row(
                [("Peers", len(node.peers)), ("Relays", len(node.relays_serving_me)), ("Dead letters", state["dead_letters"])]
            )
        )

    health.append(colored("CONTENT", fg_color=LABEL_COLOR, bold=True))
    health.append(
        "  " + counts_row(
            [
                ("Users", state["total_users"]),
                ("Message boards", state["total_boards"]),
                ("Posts", state["total_posts"]),
                ("File areas", state["total_areas"]),
                ("Files", state["total_files"]),
            ]
        )
    )

    pending_total = state["pending_users"] + state["pending_posts"] + state["pending_files"]
    health.append(colored("ATTENTION", fg_color=LABEL_COLOR, bold=True))
    health.append("  " + counts_row([("Moderation", pending_total)]) + " pending")
    health.append(
        "    " + counts_row(
            [("Users", state["pending_users"]), ("Posts", state["pending_posts"]), ("Files", state["pending_files"])]
        )
    )
    backup_at, _backup_path = state["backup"]
    update_at, update_outcome = state["update"]
    # A real update-check outcome can be an arbitrary-length message (an
    # HTTP client's own exception text, e.g.) -- when boxed, cut it to
    # what the frame can actually hold so it doesn't push the right
    # border past the terminal edge. Only under unicode_style: the flat,
    # unboxed ASCII fallback has no such constraint and must stay
    # byte-for-byte unchanged. `cut_to_width` runs on the plain text
    # before `sanitize_text`/`colored()` wrap it, matching `screen_
    # title`'s own "cut plain, then color" order (this codebase never
    # cuts already-SGR-styled text -- see `double_frame`'s docstring for
    # why).
    box_inner_width = min(session.terminal_width, 78) - 4

    def _fit(text: str, prefix_len: int) -> str:
        return cut_to_width(text, box_inner_width - prefix_len) if unicode_style else text

    health.append(
        "  Backup: "
        + (sanitize_text(_fit(backup_at, 10)) if backup_at else colored("never", fg_color=WARNING_COLOR))
    )
    health.append(
        "  Update check: "
        + (
            sanitize_text(_fit(update_outcome or update_at or "completed", 17))
            if update_at
            else colored("never", fg_color=WARNING_COLOR)
        )
    )
    if link_context is not None:
        health.append(
            "  " + counts_row([("Recent Link errors", state["recent_errors"]), ("warnings", state["recent_warnings"])])
        )

    if unicode_style:
        await session.write_line(
            double_frame(health, width=min(session.terminal_width, 78), header_color=state["header_color"])
        )
    else:
        for line in health:
            await session.write_line(line)

    # Brief descriptions are kept to roughly 34 characters or less --
    # the actual available width once this renders in two columns at
    # the classic 80-column terminal (menu_grid's own column_width
    # minus its description indent). Longer, fuller text belongs in
    # `detailed`, shown only when a caller opts into that verbosity.
    console = [
        MenuEntry(label=menu_key("U", "sers"), brief="Manage user accounts"),
        MenuEntry(
            label=menu_key("C", "ontent"),
            brief="Boards, areas, channels & more",
            detailed="Manage message boards, file areas, chat channels, and Communities -- including GC (storage garbage collection) under file areas.",
        ),
        MenuEntry(
            label=menu_key("O", "perations"),
            brief="Observe the node, fix trouble",
            detailed="Live node observation: sessions, Link status, the audit log, backup status, and draft cleanup.",
        ),
        MenuEntry(label=menu_key("S", "ettings"), brief="Durable node configuration"),
        # Dogfood follow-up: this used to say "Dashboard" (hotkey "d"),
        # which reads as a promise of some separate, deeper stats view
        # -- it's actually a manual redraw of the exact screen already
        # on display (see the "r" dispatch case in `admin_menu`).
        # "Refresh" says what it actually does; "d" wasn't a natural
        # fit for that word, so the hotkey moves to "r" (unused at
        # this menu) rather than forcing a mismatched letter.
        MenuEntry(label=menu_key("R", "efresh"), brief="Redraw with current numbers"),
        MenuEntry(label=menu_key("B", "ack"), brief="Return to the main menu"),
    ]
    quick = [MenuEntry(label=menu_key("K", "up", prefix="Bac"), brief="Last backup status and history")]
    if node_controls is not None:
        quick.insert(
            0,
            MenuEntry(label=menu_key("N", "ode"), brief="Sessions, shutdown, and drain"),
        )
    if link_context is not None:
        quick.extend([
            MenuEntry(label=menu_key("L", "ink status"), brief="NetBBS Link peer/network health"),
            MenuEntry(label=menu_key("X", "outbox"), brief="Pending outgoing Link work items"),
        ])
    await session.write_line(
        "\r\n"
        + menu_grid(
            [("Console", console), ("Quick", quick)],
            width=session.terminal_width,
            height=session.terminal_height,
            description_level=state["description_level"],
        )
    )
    await session.write("Choice: ")
    return state


# -- users submenu ---------------------------------------------------------


async def _users_menu(
    session: Session, lane: DatabaseLane, actor: User, *, node_controls: NodeControls | None
) -> None:
    """Every user-account action, grouped together (design doc): create,
    list/detail, registration policy, promote/demote, enable/disable,
    delete. `[L]ist users`/`[P]romote/demote`/`[E]nable/disable`/
    `[D]elete user` all route through the same `_pick_and_edit_user` ->
    `_user_detail_screen` central editor now (design doc -- node
    management, Thiesi's own dogfood-testing report), differing only in
    the picker's own title text. `node_controls` is threaded straight
    through to that editor -- it needs it for the live-session-
    revocation guard on disable/delete -- this submenu itself doesn't
    use it directly."""
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_users_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "c":
            await session.write_line("")
            await _create_user_screen(session, lane, actor)
            await _draw_users_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "l":
            await session.write_line("")
            await _pick_and_edit_user(session, lane, actor, node_controls, title="Registered users")
            await _draw_users_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "r":
            await session.write_line("")
            await _registration_settings_screen(session, lane, actor)
            await _draw_users_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "p":
            await session.write_line("")
            await _pick_and_edit_user(session, lane, actor, node_controls, title="Promote/demote which user?")
            await _draw_users_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "e":
            await session.write_line("")
            await _pick_and_edit_user(session, lane, actor, node_controls, title="Enable/disable which user?")
            await _draw_users_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "d":
            await session.write_line("")
            await _pick_and_edit_user(session, lane, actor, node_controls, title="Delete which user?")
            await _draw_users_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_users_menu(session: Session, description_level: str, redraw_in_place: bool, unicode_style: bool, collapsed: bool, header_color: int | tuple[int, int, int] = HEADER_COLOR) -> None:
    await session.write_line("\r\n" + screen_title("Users",
            breadcrumb=(session.node_display_name,), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color, node_name_gradient=session.node_name_gradient))
    await session.write_line(
        _menu_row(
            [
                MenuEntry(label=menu_key("C", "reate user"), brief="Add a new user account"),
                MenuEntry(label=menu_key("L", "ist users"), brief="Browse and edit accounts"),
                MenuEntry(label=menu_key("R", "egistration"), brief="Signup policy settings"),
                MenuEntry(label=menu_key("P", "romote/demote"), brief="Change a user's level"),
                MenuEntry(label=menu_key("E", "nable/disable"), brief="Toggle account access"),
                MenuEntry(label=menu_key("D", "elete user"), brief="Permanently remove a user"),
                MenuEntry(label=menu_key("B", "ack"), brief="Return to the SysOp console"),
            ],
            description_level,
            width=session.terminal_width,
            height=session.terminal_height,
        )
    )
    await session.write("Choice: ")


# -- system submenu ----------------------------------------------------------


async def _operations_menu(
    session: Session,
    lane: DatabaseLane,
    actor: User,
    *,
    node_controls: NodeControls | None,
    link_context: LinkContext | None,
) -> None:
    """Operational observation and intervention, separate from durable settings."""
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    while True:
        await session.write_line(
            "\r\n" + screen_title(
                "Operations",
                breadcrumb=(session.node_display_name, "SysOp"),
                subtitle="Observe the running node, investigate trouble, and recover work.",
                width=session.terminal_width,
                clear=redraw_in_place,
                unicode_style=unicode_style, collapsed=collapsed,
                header_color=header_color,
            node_name_gradient=session.node_name_gradient)
        )
        options = [
            MenuEntry(label=menu_key("K", "up status", prefix="Bac"), brief="Last backup status and history"),
            MenuEntry(label=menu_key("P", "rune drafts"), brief="Clean up old unsaved drafts"),
            MenuEntry(label=menu_key("A", "udit log"), brief="Moderation action history"),
        ]
        if node_controls is not None:
            options.insert(0, MenuEntry(label=menu_key("N", "ode and sessions"), brief="Sessions, shutdown, and drain"))
        if link_context is not None:
            options.extend([
                MenuEntry(label=menu_key("L", "ink status"), brief="NetBBS Link peer/network health"),
                MenuEntry(label=menu_key("O", "utbox"), brief="Pending outgoing Link work items"),
                MenuEntry(label=menu_key("D", "iagnostics"), brief="Recent Link diagnostic events"),
                MenuEntry(label=menu_key("F", "ollow log"), brief="Live-tail the diagnostic log"),
                MenuEntry(label=menu_key("R", "epair carried posts"), brief="Fix inconsistent carried posts"),
            ])
        options.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to the SysOp console"))
        await session.write_line(
            _menu_row(options, description_level, width=session.terminal_width, height=session.terminal_height)
        )
        await session.write("Choice: ")
        choice = (await session.read_key()).lower()
        if choice == "b":
            await session.write_line("")
            return
        if choice == "n" and node_controls is not None:
            await _node_menu(session, lane, actor, node_controls)
        elif choice == "l" and link_context is not None:
            await _link_status_screen(session, lane, actor, link_context=link_context)
        elif choice == "o" and link_context is not None:
            await _outbox_screen(session, lane, actor)
        elif choice == "d" and link_context is not None:
            await _diagnostic_log_screen(session, lane, actor)
        elif choice == "f" and link_context is not None:
            await _diagnostic_log_tail_screen(session, lane)
        elif choice == "r" and link_context is not None:
            await _repair_carried_posts_screen(session, lane)
        elif choice == "k":
            await _backup_status_screen(session, lane, actor)
        elif choice == "p":
            await _prune_drafts_screen(session, lane)
        elif choice == "a":
            await _audit_log_screen(session, lane, actor)
        else:
            await session.write(reject_unhandled_key(choice))


async def _system_menu(
    session: Session,
    lane: DatabaseLane,
    actor: User,
    *,
    node_controls: NodeControls | None,
    link_context: LinkContext | None = None,
) -> None:
    """Durable node settings: welcome banner, updates, timestamps, and trust.

    The old operational keys remain accepted here as non-advertised
    compatibility aliases; the visible home for those actions is now the
    operations console."""
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_system_menu(session, node_controls, link_context, description_level, redraw_in_place, unicode_style, header_color=header_color)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "w":
            await session.write_line("")
            await _welcome_banner_menu(session, lane, actor)
            await _draw_system_menu(session, node_controls, link_context, description_level, redraw_in_place, unicode_style, header_color=header_color)
        elif choice == "m":
            await session.write_line("")
            await _main_menu_banner_menu(session, lane, actor)
            await _draw_system_menu(session, node_controls, link_context, description_level, redraw_in_place, unicode_style, header_color=header_color)
        elif choice == "c":
            await session.write_line("")
            await _theme_colors_menu(session, lane, actor)
            await _draw_system_menu(session, node_controls, link_context, description_level, redraw_in_place, unicode_style, header_color=header_color)
        elif choice == "u":
            await session.write_line("")
            await _update_settings_screen(session, lane, actor)
            await _draw_system_menu(session, node_controls, link_context, description_level, redraw_in_place, unicode_style, header_color=header_color)
        elif choice == "a":
            await session.write_line("")
            await _node_name_screen(session, lane, actor)
            await _draw_system_menu(session, node_controls, link_context, description_level, redraw_in_place, unicode_style, header_color=header_color)
        elif choice == "s":
            await session.write_line("")
            await _session_banners_menu(session, lane, actor)
            await _draw_system_menu(session, node_controls, link_context, description_level, redraw_in_place, unicode_style, header_color=header_color)
        elif choice == "e":
            await session.write_line("")
            await _section_mastheads_menu(session, lane, actor)
            await _draw_system_menu(session, node_controls, link_context, description_level, redraw_in_place, unicode_style, header_color=header_color)
        elif choice == "n" and node_controls is not None:
            await session.write_line("")
            await _node_menu(session, lane, actor, node_controls)
            await _draw_system_menu(session, node_controls, link_context, description_level, redraw_in_place, unicode_style, header_color=header_color)
        elif choice == "t":
            await session.write_line("")
            await _timestamp_settings_screen(session, lane, actor)
            await _draw_system_menu(session, node_controls, link_context, description_level, redraw_in_place, unicode_style, header_color=header_color)
        elif choice == "p":
            await session.write_line("")
            await _trust_menu(session, lane, actor)
            await _draw_system_menu(session, node_controls, link_context, description_level, redraw_in_place, unicode_style, header_color=header_color)
        elif choice == "l" and link_context is not None:
            await session.write_line("")
            await _link_status_screen(session, lane, actor, link_context=link_context)
            await _draw_system_menu(session, node_controls, link_context, description_level, redraw_in_place, unicode_style, header_color=header_color)
        elif choice == "o" and link_context is not None:
            await session.write_line("")
            await _outbox_screen(session, lane, actor)
            await _draw_system_menu(session, node_controls, link_context, description_level, redraw_in_place, unicode_style, header_color=header_color)
        elif choice == "r" and link_context is not None:
            await session.write_line("")
            await _repair_carried_posts_screen(session, lane)
            await _draw_system_menu(session, node_controls, link_context, description_level, redraw_in_place, unicode_style, header_color=header_color)
        elif choice == "d" and link_context is not None:
            await session.write_line("")
            await _diagnostic_log_screen(session, lane, actor)
            await _draw_system_menu(session, node_controls, link_context, description_level, redraw_in_place, unicode_style, header_color=header_color)
        elif choice == "f" and link_context is not None:
            await session.write_line("")
            await _diagnostic_log_tail_screen(session, lane)
            await _draw_system_menu(session, node_controls, link_context, description_level, redraw_in_place, unicode_style, header_color=header_color)
        elif choice == "k":
            await session.write_line("")
            await _backup_status_screen(session, lane, actor)
            await _draw_system_menu(session, node_controls, link_context, description_level, redraw_in_place, unicode_style, header_color=header_color)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_system_menu(
    session: Session,
    node_controls: NodeControls | None,
    link_context: LinkContext | None = None,
    description_level: str = "off",
    redraw_in_place: bool = False,
    unicode_style: bool = False,
    collapsed: bool = False,
    header_color: int | tuple[int, int, int] = HEADER_COLOR,
) -> None:
    await session.write_line("\r\n" + screen_title("Settings",
            breadcrumb=(session.node_display_name,), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color, node_name_gradient=session.node_name_gradient))
    option_list = [
        MenuEntry(label=menu_key("W", "elcome banner"), brief="First-login greeting text"),
        MenuEntry(label=menu_key("M", "asthead"), brief="Custom art above the main menu"),
        MenuEntry(label=menu_key("C", "olors"), brief="Node-wide accent/header/clock branding"),
        MenuEntry(label=menu_key("a", "me", prefix="Node N"), brief="The name and gradient shown in every screen's own corner"),
        MenuEntry(label=menu_key("S", "ession banners"), brief="Logoff and new-account banners"),
        MenuEntry(label=menu_key("e", "ction mastheads", prefix="S"), brief="Above the board/file/chat pickers"),
        MenuEntry(label=menu_key("U", "pdate"), brief="Software update settings"),
        MenuEntry(label=menu_key("T", "imestamp format"), brief="Node-wide date/time display"),
        MenuEntry(label=menu_key("P", "olicy trust"), brief="Federation trust policy"),
    ]
    option_list.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to the SysOp console"))
    await session.write_line(
        _menu_row(option_list, description_level, width=session.terminal_width, height=session.terminal_height)
    )
    await session.write("Choice: ")


async def _draw_node_name_screen(
    session: Session, lane: DatabaseLane, description_level: str, redraw_in_place: bool,
    unicode_style: bool, collapsed: bool, header_color: int | tuple[int, int, int],
) -> None:
    name = await lane.run(get_node_display_name)
    gradient = await lane.run(node_name_gradient_override)
    await session.write_line(
        "\r\n" + screen_title(
            "Node name",
            breadcrumb=(session.node_display_name,), width=session.terminal_width, clear=redraw_in_place,
            unicode_style=unicode_style, collapsed=collapsed, header_color=header_color,
            node_name_gradient=session.node_name_gradient,
        )
    )
    await session.write_line(colored(f"Name: {name!r}", fg_color=header_color, bold=True))
    preview = gradient_text(name, gradient, truecolor=False) if gradient is not None else colored(name, fg_color=header_color, bold=True)
    gradient_label = gradient if gradient is not None else "solid (no gradient)"
    await session.write_line(
        colored("Gradient: ", fg_color=LABEL_COLOR) + colored(gradient_label, fg_color=MUTED_COLOR) + "   " + preview
    )
    await session.write_line(
        colored(
            f"The name is shown in the upper-left corner of every screen, and to any door as its own "
            f"drop-file 'node_name' field (always plain there, never with gradient codes). Up to "
            f"{MAX_NODE_DISPLAY_NAME_LENGTH} characters.",
            fg_color=MUTED_COLOR,
        )
    )
    await session.write_line(
        "\r\n" + _menu_row(
            [
                MenuEntry(label=menu_key("N", "ame"), brief="Rename the node"),
                MenuEntry(label=menu_key("G", "radient"), brief="Recolor the node name"),
                MenuEntry(label=menu_key("B", "ack"), brief="Return to Settings"),
            ],
            description_level,
            width=session.terminal_width,
            height=session.terminal_height,
        )
    )
    await session.write("Choice: ")


async def _node_name_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """Settings home for everything about the node's own display name:
    the text itself (`netbbs.config.set_node_display_name`, wired up as
    a dogfood-caught gap -- the getter drove `session.node_display_name`,
    shown in the breadcrumb corner of every post-login screen, since
    that field existed, but a SysOp had no way to actually set it) and,
    as of issue #175, a per-character gradient for it
    (`netbbs.net.node_theme.set_node_name_gradient_override`) -- the
    same flair `netbbs.net.welcome_banner`'s own wordmark already gets.
    Split into a menu, not a single-action screen, once there were two
    genuinely different things to change here."""
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_node_name_screen(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
    while True:
        choice = (await session.read_key()).lower()
        if choice == "b":
            await session.write_line("")
            return
        elif choice == "n":
            await session.write_line("")
            await _rename_node_screen(session, lane, actor)
            await _draw_node_name_screen(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "g":
            await session.write_line("")
            await _set_node_name_gradient_screen(session, lane, actor, header_color)
            await _draw_node_name_screen(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


async def _rename_node_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    current = await lane.run(get_node_display_name)
    await session.write(f"New name [{current}] (blank to leave unchanged): ")
    new_name = (await session.read_line()).strip()
    if not new_name:
        await session.write_line("No change.")
        return

    def _apply(db: Database) -> None:
        set_node_display_name(db, new_name)
        record_action(db, actor=actor, action="set_node_display_name", detail=f"{current!r} -> {new_name!r}")

    try:
        await lane.run(_apply)
    except ValueError as exc:
        await session.write_line(colored(str(exc), fg_color=ERROR_COLOR))
        return
    await session.write_line(f"Node name set to {new_name!r}.")


async def _set_node_name_gradient_screen(
    session: Session, lane: DatabaseLane, actor: User, header_color: int | tuple[int, int, int]
) -> None:
    """Preset-only (GitHub issue #175's own deliberately narrow scope --
    `netbbs.rendering.gradient.GRADIENTS`'s own keys, not free-form RGB
    stop entry) -- previews the candidate against the real current node
    name before asking for confirmation, the same "preview before
    confirm" requirement issue #162's own RGB slots established."""
    def _load(db: Database) -> tuple[str, str | None]:
        return get_node_display_name(db), node_name_gradient_override(db)

    name, current = await lane.run(_load)
    choices: list[str | None] = [None, *sorted(GRADIENTS)]

    await session.write_line(colored(f"\r\nGradient for {name!r}:", fg_color=header_color, bold=True))
    for i, choice in enumerate(choices):
        label = "solid" if choice is None else choice
        preview = gradient_text(name, choice, truecolor=False) if choice is not None else colored(name, fg_color=header_color, bold=True)
        marker = colored(" (current)", fg_color=MUTED_COLOR) if choice == current else ""
        await session.write_line(colored(f"  {i}. {label:<8}", fg_color=LABEL_COLOR) + preview + marker)
    await session.write(f"Choice (0-{len(choices) - 1}, blank to leave unchanged): ")

    raw = (await session.read_line()).strip()
    if not raw:
        await session.write_line("No change.")
        return
    try:
        index = int(raw)
        chosen = choices[index]
    except (ValueError, IndexError):
        await session.write_line(colored("Not a valid choice -- no change.", fg_color=ERROR_COLOR))
        return

    if chosen == current:
        await session.write_line("Already set to that -- no change.")
        return

    label = "solid" if chosen is None else chosen
    preview = gradient_text(name, chosen, truecolor=False) if chosen is not None else colored(name, fg_color=header_color, bold=True)
    await session.write_line(colored("\r\nPreview: ", fg_color=MUTED_COLOR) + preview)
    if not await prompt_yes_no(session, f"Apply {label!r} as the node name gradient?", default=False):
        await session.write_line("Not applied.")
        return

    def _apply(db: Database) -> None:
        set_node_name_gradient_override(db, chosen)
        action = "set_node_name_gradient" if chosen is not None else "clear_node_name_gradient"
        record_action(db, actor=actor, action=action, detail=f"{current!r} -> {chosen!r}")

    await lane.run(_apply)
    await session.write_line(f"Node name gradient set to {label!r}.")


# -- trust policy (Phase 4, issue #129) -------------------------------------


async def _trust_menu(session: Session, lane: DatabaseLane, actor: User) -> None:
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    while True:
        authorities = await lane.run(list_sole_authorities)
        await session.write_line(
            "\r\n"
            + screen_title(
                "Trust policy",
                breadcrumb=(session.node_display_name, "System"),
                subtitle="Inspect policy, explain restrictions, and manage trusted authorities.",
                width=session.terminal_width,
                clear=redraw_in_place,
                unicode_style=unicode_style, collapsed=collapsed,
                header_color=header_color,
            node_name_gradient=session.node_name_gradient)
        )
        options = [
            MenuEntry(label=menu_key("S", "ubjects"), brief="Trusted node/user subjects"),
            MenuEntry(label=menu_key("D", "omains"), brief="Trusted federation domains"),
            MenuEntry(label=menu_key("A", "nchors"), brief="Root trust anchor keys"),
            MenuEntry(label=menu_key("R", "eporters"), brief="Who can report abuse remotely"),
            MenuEntry(label=menu_key("I", "dentity authorities"), brief="Attestation authority list"),
            MenuEntry(label=menu_key("E", "xceptions"), brief="Sole-authority deviations"),
            MenuEntry(label=menu_key("H", "istory"), brief="Trust config change log"),
            MenuEntry(label=menu_key("B", "ack"), brief="Return to Settings"),
        ]
        await session.write_line(
            _menu_row(options, description_level, width=session.terminal_width, height=session.terminal_height)
        )
        if authorities:
            await session.write_line(
                badge("SAFETY DEVIATION", tone="error")
                + " "
                + colored(
                    f"{len(authorities)} sole-authority exception(s) active.",
                    fg_color=ALERT_COLOR,
                    bold=True,
                )
            )
        await session.write("Choice: ")
        choice = (await session.read_key()).lower()
        if choice == "b":
            await session.write_line("")
            return
        if choice == "s":
            await _trust_subjects_screen(session, lane, actor)
        elif choice == "d":
            await _trust_domains_screen(session, lane, actor)
        elif choice == "a":
            await _trust_anchors_screen(session, lane, actor)
        elif choice == "r":
            await _trust_reporters_screen(session, lane, actor)
        elif choice == "i":
            await _attestation_authorities_screen(session, lane, actor)
        elif choice == "e":
            await _trust_exceptions_screen(session, lane, actor)
        elif choice == "h":
            await _trust_config_history_screen(session, lane)
        else:
            await session.write(reject_unhandled_key(choice))


def _trust_subject_name(subject: TrustSubject) -> str:
    if subject.kind == "node":
        return f"node:{subject.node_fingerprint}"
    return f"user:{subject.node_fingerprint}/{subject.opaque_user_id}"


_TRUST_STATE_TONE = {
    TrustState.ESTABLISHED: "success",
    TrustState.PROBATIONARY: "neutral",
    TrustState.QUARANTINED: "warning",
    TrustState.BLOCKED: "error",
}


def _trust_subject_stable_id(subject: TrustSubject) -> int:
    return int(subject.subject_id[:12], 16)


async def _trust_subjects_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    subjects = await lane.run(list_trust_subjects)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    selected = await pick_item(
        session, subjects,
        name_of=_trust_subject_name,
        stable_id_of=_trust_subject_stable_id,
        description_of=lambda subject: subject.kind,
        title="Trust subjects",
        empty_message="No remote trust subjects have been registered.",
        redraw_in_place=redraw_in_place,
        unicode_style=unicode_style,
        collapsed=collapsed,
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if selected is None:
        return
    description_level = await lane.run(menu_description_level, actor)
    while True:
        states = [
            await lane.run(get_effective_trust_state, selected, dimension)
            for dimension in TrustDimension
        ]
        await session.write_line(
            colored(f"\r\n{_trust_subject_name(selected)}", fg_color=await lane.run(effective_header_color_256), bold=True)
        )
        for state in states:
            await session.write_line(
                f"{state.dimension.value}: {status_badge(state.state.value, tone=_TRUST_STATE_TONE[state.state], unicode_style=unicode_style)} ({state.reason_code})"
            )
            await session.write_line(
                colored(
                    "  " + json.dumps(state.explanation, sort_keys=True, ensure_ascii=True),
                    fg_color=METADATA_COLOR,
                )
            )
        if selected.kind == "user":
            for attribute in ("age", "name"):
                attestation_state = await lane.run(
                    get_remote_attestation_state, selected, attribute
                )
                accepted_badge = (
                    status_badge("accepted", tone="success", unicode_style=unicode_style)
                    if attestation_state.accepted
                    else status_badge("not accepted", tone="error", unicode_style=unicode_style)
                )
                await session.write_line(
                    f"remote {attribute} attestation: {accepted_badge} "
                    f"({attestation_state.reason_code})"
                )
                await session.write_line(
                    colored(
                        "  " + json.dumps(
                            attestation_state.explanation,
                            sort_keys=True,
                            ensure_ascii=True,
                        ),
                        fg_color=METADATA_COLOR,
                    )
                )
        await session.write_line(
            _menu_row(
                [
                    MenuEntry(label=menu_key("O", "verride"), brief="Force a trust dimension's state"),
                    MenuEntry(label=menu_key("C", "lear override"), brief="Remove a forced state"),
                    *(
                        [MenuEntry(
                            label=menu_key("I", "dentity attestation override"),
                            brief="Force age/name attestation state",
                        )]
                        if selected.kind == "user" else []
                    ),
                    MenuEntry(label=menu_key("H", "istory"), brief="This subject's change log"),
                    MenuEntry(label=menu_key("B", "ack"), brief="Return to the subject list"),
                ],
                description_level,
                width=session.terminal_width,
                height=session.terminal_height,
            )
        )
        await session.write("Choice: ")
        choice = (await session.read_key()).lower()
        if choice == "b":
            await session.write_line("")
            return
        if choice == "o":
            await _set_trust_override_screen(session, lane, actor, selected)
        elif choice == "c":
            await _clear_trust_override_screen(session, lane, actor, selected)
        elif choice == "h":
            await _trust_decision_history_screen(session, lane, selected)
        elif choice == "i" and selected.kind == "user":
            await _remote_attestation_override_screen(session, lane, actor, selected)
        else:
            await session.write(reject_unhandled_key(choice))


async def _pick_trust_dimension(session: Session) -> TrustDimension | None:
    await session.write_line("Dimension:")
    await session.write_line(
        action_bar(
            [
                menu_key("I", "dentity integrity"),
                menu_key("R", "esource behavior"),
                menu_key("C", "ontent conduct"),
                menu_key("B", "ack"),
            ],
            width=session.terminal_width,
        )
    )
    await session.write("Choice: ")
    choice = (await session.read_key()).lower()
    return {
        "i": TrustDimension.IDENTITY_INTEGRITY,
        "r": TrustDimension.RESOURCE_BEHAVIOR,
        "c": TrustDimension.CONTENT_CONDUCT,
    }.get(choice)


async def _set_trust_override_screen(
    session: Session, lane: DatabaseLane, actor: User, subject: TrustSubject
) -> None:
    dimension = await _pick_trust_dimension(session)
    if dimension is None:
        return
    await session.write_line("State:")
    await session.write_line(
        action_bar(
            [
                menu_key("P", "robationary"),
                menu_key("E", "stablished"),
                menu_key("Q", "uarantined"),
                menu_key("B", "locked"),
            ],
            width=session.terminal_width,
        )
    )
    await session.write("Choice: ")
    state = {
        "p": TrustState.PROBATIONARY, "e": TrustState.ESTABLISHED,
        "q": TrustState.QUARANTINED, "b": TrustState.BLOCKED,
    }.get((await session.read_key()).lower())
    if state is None:
        await session.write_line(colored("Unknown trust state; no change made.", fg_color=ERROR_COLOR))
        return
    await session.write("Mandatory reason: ")
    reason = (await session.read_line()).strip()
    if not reason:
        await session.write_line(colored("A reason is required; no change made.", fg_color=ERROR_COLOR))
        return
    if state == TrustState.ESTABLISHED:
        confirmed = await prompt_yes_no(
            session,
            "This bypasses automatic probation requirements. Apply this audited safety deviation?",
            default=False,
        )
        if not confirmed:
            await session.write_line(colored("No change made.", fg_color=MUTED_COLOR))
            return
    try:
        await lane.run(
            set_trust_override, subject, dimension, state,
            reason=reason, actor_user_id=actor.id,
        )
    except ValueError as exc:
        await session.write_line(colored(f"Trust state changed concurrently: {exc}", fg_color=ERROR_COLOR))
        return
    await session.write_line(colored("Trust override applied and audited.", fg_color=SUCCESS_COLOR))


async def _clear_trust_override_screen(
    session: Session, lane: DatabaseLane, actor: User, subject: TrustSubject
) -> None:
    overrides = await lane.run(list_trust_overrides, subject)
    selected = await pick_item(
        session, overrides,
        name_of=lambda item: f"{item.dimension.value}: {item.state.value}",
        stable_id_of=lambda item: item.override_id,
        description_of=lambda item: item.reason,
        title="Active trust overrides",
        empty_message="No active trust overrides.",
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if selected is None:
        return
    try:
        await lane.run(clear_trust_override, selected.override_id, actor_user_id=actor.id)
    except ValueError as exc:
        await session.write_line(colored(f"Trust state changed concurrently: {exc}", fg_color=ERROR_COLOR))
        return
    await session.write_line(colored("Override cleared; recovery policy was recomputed.", fg_color=SUCCESS_COLOR))


async def _trust_decision_history_screen(
    session: Session, lane: DatabaseLane, subject: TrustSubject
) -> None:
    rows = await lane.run(list_trust_decision_audit, subject)
    await session.write_line(
        colored("\r\nTrust decision history:", fg_color=await lane.run(effective_header_color_256), bold=True)
    )
    for row in rows:
        await session.write_line(
            f"{row.created_at} {row.kind} {row.action} "
            f"{json.dumps(row.details, sort_keys=True, ensure_ascii=True)}"
        )
    if not rows:
        await session.write_line(colored("No decision history.", fg_color=MUTED_COLOR))
    if subject.kind == "user":
        attestation_rows = await lane.run(list_remote_attestation_audit, subject)
        for row in attestation_rows:
            await session.write_line(
                f"{row.created_at} remote-{row.object_kind} {row.action} "
                f"{json.dumps(row.details, sort_keys=True, ensure_ascii=True)}"
            )


async def _trust_domains_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    domains = await lane.run(list_trust_domains)
    await session.write_line(
        colored("\r\nTrust domains:", fg_color=await lane.run(effective_header_color_256), bold=True)
    )
    for domain in domains:
        await session.write_line(f"{domain.domain_id}: {domain.display_name} (weight {domain.weight:.2f})")
    while True:
        await session.write(
            f"{action_bar([menu_key('A', 'dd/update'), menu_key('B', 'ack')], width=session.terminal_width)}: "
        )
        choice = (await session.read_key()).lower()
        if choice == "b":
            return
        if choice == "a":
            break
        await session.write(reject_unhandled_key(choice))
    await session.write_line("")
    await session.write("Domain ID: ")
    domain_id = (await session.read_line()).strip()
    await session.write("Display name: ")
    display_name = (await session.read_line()).strip()
    await session.write("Weight (0.0-1.0): ")
    raw_weight = (await session.read_line()).strip()
    try:
        weight = float(raw_weight)
    except ValueError:
        await session.write_line(colored("Not a number -- cancelled.", fg_color=MUTED_COLOR))
        return
    try:
        await lane.run(
            configure_trust_domain, domain_id, display_name=display_name,
            weight=weight, actor_user_id=actor.id,
        )
    except ValueError as exc:
        await session.write_line(colored(f"Trust domain not changed: {exc}", fg_color=ERROR_COLOR))
        return
    await session.write_line(colored("Trust domain saved and audited.", fg_color=SUCCESS_COLOR))


async def _trust_anchors_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    anchors = await lane.run(list_trust_anchors)
    await session.write_line(
        colored("\r\nTrust anchors:", fg_color=await lane.run(effective_header_color_256), bold=True)
    )
    for anchor in anchors:
        await session.write_line(f"{anchor.fingerprint}: {anchor.reason}")
    while True:
        await session.write(
            f"{action_bar([menu_key('A', 'dd/update'), menu_key('R', 'emove'), menu_key('B', 'ack')], width=session.terminal_width)}: "
        )
        choice = (await session.read_key()).lower()
        if choice == "b":
            return
        if choice in {"a", "r"}:
            break
        await session.write(reject_unhandled_key(choice))
    await session.write_line("")
    await session.write("Fingerprint: ")
    fingerprint = (await session.read_line()).strip()
    try:
        if choice == "a":
            await session.write("Mandatory reason: ")
            reason = (await session.read_line()).strip()
            await lane.run(
                configure_trust_anchor, fingerprint, reason=reason, actor_user_id=actor.id,
            )
        else:
            await lane.run(remove_trust_anchor, fingerprint, actor_user_id=actor.id)
    except ValueError as exc:
        await session.write_line(colored(f"Trust anchor not changed: {exc}", fg_color=ERROR_COLOR))
        return
    await session.write_line(colored("Trust anchor changed and audited.", fg_color=SUCCESS_COLOR))


def _parse_reporter_scopes(value: str) -> list[tuple[TrustDimension, str]]:
    result: list[tuple[TrustDimension, str]] = []
    for item in value.split(","):
        dimension, separator, category = item.strip().partition(":")
        if not separator or not category:
            raise ValueError("scopes must use dimension:category, separated by commas")
        normalized = TrustDimension(dimension)
        result.append((normalized, category))
    if not result:
        raise ValueError("at least one reporter scope is required")
    return result


async def _trust_reporters_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    reporters = await lane.run(list_trusted_reporters)
    await session.write_line(
        colored("\r\nTrusted reporters:", fg_color=await lane.run(effective_header_color_256), bold=True)
    )
    for reporter in reporters:
        scopes = ", ".join(f"{d.value}:{c}" for d, c in reporter.scopes) or "no scopes"
        await session.write_line(
            f"{reporter.fingerprint} domain={reporter.domain_id} scopes={scopes} "
            f"vouch(node={reporter.can_vouch_nodes}, user={reporter.can_vouch_users})"
        )
    while True:
        await session.write(
            f"{action_bar([menu_key('A', 'dd/update'), menu_key('R', 'emove'), menu_key('B', 'ack')], width=session.terminal_width)}: "
        )
        choice = (await session.read_key()).lower()
        if choice == "b":
            return
        if choice in {"a", "r"}:
            break
        await session.write(reject_unhandled_key(choice))
    await session.write_line("")
    await session.write("Reporter fingerprint: ")
    fingerprint = (await session.read_line()).strip()
    try:
        if choice == "r":
            await lane.run(remove_trusted_reporter, fingerprint, actor_user_id=actor.id)
        else:
            await session.write("Trust domain ID: ")
            domain_id = (await session.read_line()).strip()
            await session.write("Scopes (dimension:category, comma separated): ")
            scopes = _parse_reporter_scopes(await session.read_line())
            node_vouch = await prompt_yes_no(session, "May this reporter vouch for nodes?", default=False)
            user_vouch = await prompt_yes_no(session, "May this reporter vouch for users?", default=False)
            await lane.run(
                configure_trusted_reporter, fingerprint, domain_id=domain_id, scopes=scopes,
                can_vouch_nodes=node_vouch, can_vouch_users=user_vouch,
                actor_user_id=actor.id,
            )
    except ValueError as exc:
        await session.write_line(colored(f"Trusted reporter not changed: {exc}", fg_color=ERROR_COLOR))
        return
    await session.write_line(colored("Trusted reporter changed and audited.", fg_color=SUCCESS_COLOR))


async def _attestation_authorities_screen(
    session: Session, lane: DatabaseLane, actor: User
) -> None:
    authorities = await lane.run(list_attestation_authorities)
    await session.write_line(
        colored(
            "\r\nRemote identity-attestation authorities:",
            fg_color=await lane.run(effective_header_color_256), bold=True,
        )
    )
    for authority in authorities:
        await session.write_line(
            f"{authority.fingerprint} scope={','.join(authority.attributes)} -- {authority.reason}"
        )
    if not authorities:
        await session.write_line(
            colored("None. Remote attestations fail closed.", fg_color=SUCCESS_COLOR)
        )
    while True:
        await session.write(
            f"{action_bar([menu_key('A', 'dd/update'), menu_key('R', 'emove'), menu_key('B', 'ack')], width=session.terminal_width)}: "
        )
        choice = (await session.read_key()).lower()
        if choice == "b":
            return
        if choice in {"a", "r"}:
            break
        await session.write(reject_unhandled_key(choice))
    await session.write_line("")
    await session.write("Authority node fingerprint: ")
    fingerprint = (await session.read_line()).strip()
    try:
        if choice == "r":
            await lane.run(
                remove_attestation_authority,
                fingerprint,
                actor_user_id=actor.id,
            )
        else:
            await session.write("Attributes (age,name or both, comma separated): ")
            attributes = [part.strip() for part in (await session.read_line()).split(",")]
            await session.write("Mandatory reason: ")
            reason = (await session.read_line()).strip()
            await lane.run(
                configure_attestation_authority,
                fingerprint,
                attributes=attributes,
                reason=reason,
                actor_user_id=actor.id,
            )
    except ValueError as exc:
        await session.write_line(
            colored(f"Attestation authority not changed: {exc}", fg_color=ERROR_COLOR)
        )
        return
    await session.write_line(
        colored("Attestation authority changed and audited.", fg_color=SUCCESS_COLOR)
    )


async def _remote_attestation_override_screen(
    session: Session,
    lane: DatabaseLane,
    actor: User,
    subject: TrustSubject,
) -> None:
    await session.write_line("Attribute:")
    await session.write_line(
        action_bar(
            [menu_key("A", "ge"), menu_key("N", "ame"), menu_key("C", "lear override"), menu_key("B", "ack")],
            width=session.terminal_width,
        )
    )
    await session.write("Choice: ")
    choice = (await session.read_key()).lower()
    if choice == "c":
        overrides = await lane.run(list_remote_attestation_overrides, subject)
        selected = await pick_item(
            session,
            overrides,
            name_of=lambda item: f"{item.attribute}: {'accept' if item.accepted else 'reject'}",
            stable_id_of=lambda item: item.override_id,
            description_of=lambda item: item.reason,
            title="Remote attestation overrides",
            empty_message="No active remote attestation overrides.",
            redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
            unicode_style=await lane.run(unicode_style_enabled, actor),
            collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
            accent_color=await lane.run(effective_accent_color_256),
            header_color=await lane.run(effective_header_color_256),
        )
        if selected is None:
            return
        try:
            await lane.run(
                clear_remote_attestation_override,
                selected.override_id,
                actor_user_id=actor.id,
            )
        except ValueError as exc:
            await session.write_line(
                colored(f"Attestation state changed concurrently: {exc}", fg_color=ERROR_COLOR)
            )
            return
        await session.write_line(
            colored("Remote attestation override cleared.", fg_color=SUCCESS_COLOR)
        )
        return
    attribute = {"a": "age", "n": "name"}.get(choice)
    if attribute is None:
        return
    await session.write_line("Decision:")
    await session.write_line(
        action_bar(
            [menu_key("A", "ccept current trusted record"), menu_key("R", "eject")],
            width=session.terminal_width,
        )
    )
    await session.write("Choice: ")
    accepted_choice = (await session.read_key()).lower()
    if accepted_choice not in {"a", "r"}:
        return
    accepted = accepted_choice == "a"
    await session.write("Mandatory reason: ")
    reason = (await session.read_line()).strip()
    if accepted:
        confirmed = await prompt_yes_no(
            session,
            "Accept only while a current signed record from a configured authority exists?",
            default=False,
        )
        if not confirmed:
            await session.write_line(colored("No change made.", fg_color=MUTED_COLOR))
            return
    try:
        await lane.run(
            set_remote_attestation_override,
            subject,
            attribute,
            accepted=accepted,
            reason=reason,
            actor_user_id=actor.id,
        )
    except ValueError as exc:
        await session.write_line(
            colored(f"Remote attestation override not changed: {exc}", fg_color=ERROR_COLOR)
        )
        return
    await session.write_line(
        colored("Remote attestation override applied and audited.", fg_color=SUCCESS_COLOR)
    )


async def _trust_exceptions_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    exceptions = await lane.run(list_sole_authorities)
    await session.write_line(colored("\r\nSole-authority safety deviations:", fg_color=ALERT_COLOR, bold=True))
    for item in exceptions:
        await session.write_line(
            f"{item.reporter_fingerprint} {item.dimension.value}:{item.category} -- {item.reason}"
        )
    if not exceptions:
        await session.write_line(colored("None. Two independent domains remain required.", fg_color=SUCCESS_COLOR))
    while True:
        await session.write(
            f"{action_bar([menu_key('A', 'dd/update'), menu_key('R', 'emove'), menu_key('B', 'ack')], width=session.terminal_width)}: "
        )
        choice = (await session.read_key()).lower()
        if choice == "b":
            return
        if choice in {"a", "r"}:
            break
        await session.write(reject_unhandled_key(choice))
    await session.write_line("")
    await session.write("Reporter fingerprint: ")
    fingerprint = (await session.read_line()).strip()
    dimension = await _pick_trust_dimension(session)
    if dimension is None:
        return
    await session.write("Category: ")
    category = (await session.read_line()).strip()
    try:
        if choice == "r":
            await lane.run(
                remove_sole_authority, fingerprint, dimension, category,
                actor_user_id=actor.id,
            )
        else:
            await session.write("Mandatory justification: ")
            reason = (await session.read_line()).strip()
            confirmed = await prompt_yes_no(
                session,
                "DANGER: one reporter will bypass the two-domain rule for this category. Continue?",
                default=False,
            )
            if not confirmed:
                await session.write_line(colored("No change made.", fg_color=MUTED_COLOR))
                return
            await lane.run(
                configure_sole_authority, fingerprint, dimension, category,
                reason=reason, actor_user_id=actor.id,
            )
    except ValueError as exc:
        await session.write_line(colored(f"Safety deviation not changed: {exc}", fg_color=ERROR_COLOR))
        return
    await session.write_line(colored("Safety deviation changed and audited.", fg_color=SUCCESS_COLOR))


async def _trust_config_history_screen(session: Session, lane: DatabaseLane) -> None:
    rows = await lane.run(list_trust_config_audit)
    await session.write_line(
        colored("\r\nTrust configuration history:", fg_color=await lane.run(effective_header_color_256), bold=True)
    )
    for row in rows:
        await session.write_line(
            f"{row.created_at} {row.kind} {row.action} "
            f"{json.dumps(row.details, sort_keys=True, ensure_ascii=True)}"
        )
    if not rows:
        await session.write_line(colored("No configuration history.", fg_color=MUTED_COLOR))
    attestation_rows = await lane.run(list_remote_attestation_audit)
    for row in attestation_rows:
        if row.subject_id is None:
            await session.write_line(
                f"{row.created_at} remote-{row.object_kind}:{row.object_id} {row.action} "
                f"{json.dumps(row.details, sort_keys=True, ensure_ascii=True)}"
            )


# -- create ------------------------------------------------------------


def _create_user_password_field() -> Callable[[Session, DatabaseLane, dict], Awaitable[None]]:
    """Reuses `_prompt_optional_password` as-is on every activation --
    "no" always (re)clears `draft["password"]` to `None`, "yes" always
    prompts a fresh password and replaces whatever was there, so the
    field's own y/n gate doubles as its only clear/replace mechanism.
    Deliberately not a richer set/replace/clear menu of its own: this
    screen's draft is never seeded from an existing account (create-only,
    unlike `edit_resource_draft`'s other callers), so there's no "current
    real password" a clear/keep distinction would need to protect."""

    async def prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        draft["password"] = await _prompt_optional_password(session)

    return prompt


def _create_user_pubkey_field() -> Callable[[Session, DatabaseLane, dict], Awaitable[None]]:
    """`_create_user_password_field`'s counterpart for the public key,
    same reused-prompt/replace-or-clear shape."""

    async def prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        draft["verify_key"] = await _prompt_optional_pubkey(session)

    return prompt


def _create_user_field_specs() -> list[FieldSpec]:
    """Dogfood feature request, issue #160's cursor-navigation follow-up
    (item 4 of the prioritized list): the linear username -> password? ->
    key? -> level wizard forced every field in sequence with no way to
    revisit or skip ahead. Same draft-editor shape as every other
    create/edit screen -- see `netbbs.net.resource_editor`'s own module
    docstring -- except this one has no `existing` counterpart to edit:
    account creation is the only thing this screen does."""
    return [
        FieldSpec(
            key="username", hotkey="u", menu_text=menu_key("U", "sername"), label="Username",
            render=lambda d: d.get("username") or "",
            prompt=text_field("username", required=True),
            help="Letters, digits, '_', '-', and '.' only. Case-insensitive uniqueness.",
        ),
        FieldSpec(
            key="password", hotkey="p", menu_text=menu_key("P", "assword"), label="Password",
            render=lambda d: "set" if d.get("password") else "(not set)",
            prompt=_create_user_password_field(),
            help=(
                "An account needs a password, a public key, or both. Answering 'no' here "
                "clears any password already entered on this draft; answering 'yes' always "
                "prompts for and replaces it with a fresh one."
            ),
        ),
        FieldSpec(
            key="verify_key", hotkey="k", menu_text=menu_key("K", "ey"), label="Public key",
            render=lambda d: "set" if d.get("verify_key") else "(not set)",
            prompt=_create_user_pubkey_field(),
            help="For SSH key-based login. Paste as base64, or a full 'ssh-ed25519 ...' line.",
        ),
        FieldSpec(
            key="level", hotkey="l", menu_text=menu_key("L", "evel", prefix="Starting "), label="Starting level",
            render=lambda d: str(d.get("level", 0)),
            prompt=_int_field("level", "Starting level"),
            help=(
                "The account's initial permission level. 0 is an ordinary caller; higher "
                "numbers unlock moderator/SysOp capability (see the Level field on an "
                "existing account's own detail screen for the specific thresholds)."
            ),
        ),
    ]


async def _create_user_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    draft: dict = {"username": "", "password": None, "verify_key": None, "level": 0}

    async def save(draft: dict) -> User:
        # create_user, not create_user_async -- the latter's off-loop
        # hashing split existed specifically to keep Argon2 hashing off
        # the *raw* event loop; lane.run() already dispatches this whole
        # call to a worker thread, so the plain synchronous create_user
        # (per its own docstring, "for command-line/admin callers") does
        # the hash and the write in one lane dispatch. Blank username and
        # "neither password nor key" are both rejected by create_user
        # itself (AuthError), so this closure has no validation of its
        # own to duplicate.
        new_user = await lane.run(
            create_user, draft["username"], password=draft["password"],
            verify_key=draft["verify_key"], user_level=draft["level"],
        )
        await lane.run(
            record_action, actor=actor, action="create_user", target_user_id=new_user.id,
            detail=f"created user {new_user.username!r} at level {draft['level']}",
        )
        # Dogfood report: three testers on modern (ANSI-capable) clients
        # never discovered in-place redraw existed, so never turned it
        # on. New accounts (self-registered or SysOp-created) now start
        # with it already on -- see the matching self-registration
        # change in login_flow._register_new_account for the full
        # rationale.
        await lane.run(set_redraw_in_place_enabled, new_user, True)
        return new_user

    redraw_in_place, redraw_hint = await lane.run(_resolve_redraw_preference, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    new_user = await edit_resource_draft(
        session, lane,
        title="Create user",
        fields=_create_user_field_specs(),
        draft=draft, save=save, error_type=AuthError,
        save_menu_text=menu_key("C", "reate"), save_hotkey="c", back_menu_text=menu_key("B", "ack"),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=redraw_in_place, redraw_hint=redraw_hint,
        unicode_style=unicode_style,
        collapsed=collapsed,
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if new_user is not None:
        await session.write_line(f"Created {new_user.username!r} at level {new_user.user_level}.")
        await session.write_line(
            colored(
                "In-place redraw is on by default for this account -- they can turn it off in Your profile.",
                fg_color=MUTED_COLOR,
            )
        )


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


# Mirrors netbbs.net.picker's own reserved-lines/max-page-size budget --
# duplicated rather than imported (that module's own private helpers
# aren't reached into from other modules, the same "duplicate rather
# than reach into another module's private helper" convention
# netbbs.link.files._file_area_from_row's own docstring already states
# for the identical reasoning).
_USER_PICKER_RESERVED_LINES = 7
_USER_PICKER_MAX_PAGE_SIZE = 99

# One key per sort dimension, each a live toggle between its ascending
# and descending `netbbs.auth.users.list_users` `order_by` value.
# Design doc -- Thiesi's own follow-up dogfood-testing request: pressing
# the currently-active key flips direction; pressing a different key
# switches to it, always starting ascending. Deliberately a *bespoke*
# screen, not a generic extension to netbbs.net.picker.pick_item -- that
# component is shared by boards/channels/file areas too, and this
# project's own convention (worklog) is to design a shared abstraction
# against a second real consumer, not build one on spec for a single
# caller.
_USER_SORT_MODES = {
    "a": ("Alphabetical", "alphabetical", "alphabetical_desc"),
    "r": ("Registration date", "registered", "registered_desc"),
    "l": ("Level", "level_asc", "level_desc"),
}

# A second, independent live toggle -- requested by the SysOp of the
# biggest node once real dogfood use with ~50 registered users made
# scrolling past disabled accounts to find an active one (or vice versa)
# a real friction point pagination/search alone didn't solve. A 3-state
# cycle, not a plain on/off filter, since both "I only care about active
# accounts" and "I only care about disabled ones" (e.g. reviewing who to
# actually delete) are real, equally common tasks -- one boolean can't
# express both. `[V]` always advances one step forward through the same
# fixed order; there's no direct "jump to state N" key, matching how
# `[A]`/`[R]`/`[L]` already only offer "toggle the active one" rather
# than a menu of every possible value.
_USER_VISIBILITY_MODES = ("all", "active_only", "disabled_only")
_USER_VISIBILITY_LABELS = {
    "all": "All users",
    "active_only": "Active users only (disabled hidden)",
    "disabled_only": "Disabled users only",
}


def _user_picker_nav(session: Session) -> str:
    # Deliberately NOT run through `_menu_row`/`menu_grid` -- issue
    # #160's rollout, but a considered exception: this nav has 9 entries
    # (4 sort/filter toggles plus the usual 5 paging keys), and
    # `menu_grid` renders one entry per line even at "brief" (the real
    # default -- see `menu_description_preference`'s own docstring).
    # That's 18 lines of nav alone on top of this screen's other
    # reserved lines, which at a standard 24-row terminal leaves room
    # for exactly 1 user per page -- defeating the point of a screen
    # whose whole purpose is browsing a list of many users. Every other
    # converted site in this rollout has at most ~6 entries, where the
    # same trade-off is still tolerable; this one isn't. Always compact,
    # regardless of the caller's description-level preference.
    options = [
        menu_key("A", "lphabetical"), menu_key("R", "egistration"), menu_key("L", "evel"),
        menu_key("V", "isibility"), menu_key("N", "ext"), menu_key("P", "rev"),
        menu_key("S", "earch"), menu_key("G", "oto #"), menu_key("B", "ack"),
    ]
    return action_bar(options, width=session.terminal_width)


def _user_picker_page_size(session: Session) -> int:
    available = session.terminal_height - _USER_PICKER_RESERVED_LINES
    return max(1, min(_USER_PICKER_MAX_PAGE_SIZE, available))


def _user_search_completer(candidates: Sequence[str]) -> Callable[[str], list[str]]:
    """Tab completion for the user picker's own `"Search: "` prompt --
    mirrors `netbbs.net.picker._search_completer`'s exact behavior
    (prefix match, no candidates once the query contains a space),
    duplicated rather than imported for the same reason
    `_user_picker_page_size` above is."""

    def completer(text: str) -> list[str]:
        if " " in text:
            return []
        lower = text.lower()
        return sorted(name for name in candidates if name.lower().startswith(lower))

    return completer


async def _pick_target_user(session: Session, lane: DatabaseLane, actor: User, *, title: str) -> User | None:
    """
    The single screen every `[U]sers` submenu entry now reaches a target
    account through (design doc -- Thiesi's own dogfood-testing report).
    Mirrors `pick_item`'s own pagination/search/goto/select shape
    closely, adding three live sort-toggle keys (`[A]lphabetical`/
    `[R]egistration date`/`[L]evel`) that re-sort and redraw the same
    screen in place, each shown with its own current direction arrow so
    the active mode is never ambiguous, plus a fourth, independent
    `[V]isibility` toggle (a real ~50-user node's own SysOp, dogfooding
    the sort toggles) cycling all -> active-only -> disabled-only -> all,
    always shown as a `Showing: ...` line the same "current state is
    never ambiguous" way the sort line already is. The visibility filter
    applies everywhere `_load` is called -- search and goto both scope to
    the currently visible subset, not the full roster -- since the whole
    point of hiding a class of accounts is to stop having to look at or
    reach them until the SysOp explicitly widens the filter again.
    """
    mode = "a"
    descending = False
    visibility = "all"
    query: str | None = None
    page_index = 0

    async def _load(*, apply_search: bool = True) -> list[User]:
        _, ascending_order, descending_order = _USER_SORT_MODES[mode]
        users = await lane.run(list_users, order_by=descending_order if descending else ascending_order)
        if visibility == "active_only":
            users = [u for u in users if u.disabled_at is None]
        elif visibility == "disabled_only":
            users = [u for u in users if u.disabled_at is not None]
        if apply_search and query:
            return [u for u in users if query.lower() in u.username.lower()]
        return users

    working_set = await _load()
    if not working_set:
        await session.write_line("\r\nNo registered users yet.")
        return None
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    accent = await lane.run(effective_accent_color_256)
    header_color = await lane.run(effective_header_color_256)

    def _total_pages() -> int:
        return max(1, math.ceil(len(working_set) / _user_picker_page_size(session)))

    async def _render() -> list[User]:
        nonlocal page_index
        page_size = _user_picker_page_size(session)
        total_pages = _total_pages()
        page_index = max(0, min(page_index, total_pages - 1))
        start = page_index * page_size
        page_users = working_set[start : start + page_size]

        label, _, _ = _USER_SORT_MODES[mode]
        if unicode_style:
            arrow = "↓" if descending else "↑"
        else:
            arrow = "desc" if descending else "asc"
        await session.write_line(
            "\r\n" + screen_title(
                title,
            breadcrumb=(session.node_display_name,),
                subtitle=f"page {page_index + 1}/{total_pages}, {len(working_set)} total",
                width=session.terminal_width,
                clear=redraw_in_place,
                unicode_style=unicode_style, collapsed=collapsed, header_color=header_color, node_name_gradient=session.node_name_gradient)
        )
        await session.write_line(colored(f"Sorted by: {label} {arrow}", fg_color=MUTED_COLOR))
        await session.write_line(
            colored(f"Showing: {_USER_VISIBILITY_LABELS[visibility]}", fg_color=MUTED_COLOR)
        )
        for position, user in enumerate(page_users, start=1):
            segments: list[tuple[str, int | None]] = [
                (f"  {position:02d}. ", MENU_KEY_COLOR),
                (f"(#{user.id}) ", MUTED_COLOR),
                (sanitize_text(user.username), accent),
                (f" - {_user_description(user)}", MUTED_COLOR),
            ]
            await session.write_line(colored_truncate(segments, session.terminal_width))

        nav = _user_picker_nav(session)
        await session.write_line(f"\r\n{nav} — or type a 2-digit number to select")
        await session.write("Choice: ")
        return page_users

    page_users = await _render()
    while True:
        key = await session.read_key()
        key_lower = key.lower()

        if key_lower == "b":
            await session.write_line("")
            return None

        if key_lower in _USER_SORT_MODES:
            if mode == key_lower:
                descending = not descending
            else:
                mode = key_lower
                descending = False
            await session.write_line("")
            working_set = await _load()
            page_index = 0
            page_users = await _render()
            continue

        if key_lower == "v":
            next_index = (_USER_VISIBILITY_MODES.index(visibility) + 1) % len(_USER_VISIBILITY_MODES)
            visibility = _USER_VISIBILITY_MODES[next_index]
            await session.write_line("")
            working_set = await _load()
            page_index = 0
            page_users = await _render()
            continue

        if key_lower == "n":
            if page_index < _total_pages() - 1:
                await session.write_line("")
                page_index += 1
                page_users = await _render()
            else:
                await session.write(reject_unhandled_key(key))
            continue

        if key_lower == "p":
            if page_index > 0:
                await session.write_line("")
                page_index -= 1
                page_users = await _render()
            else:
                await session.write(reject_unhandled_key(key))
            continue

        if key_lower == "s":
            await session.write_line("")
            await session.write("Search: ")
            all_users = await _load(apply_search=False)
            completer = _user_search_completer([u.username for u in all_users])
            typed = (await session.read_line(completer=completer)).strip()
            if not typed:
                # Empty search clears back to the full, unfiltered list --
                # a no-op if nothing was filtered yet, "clear filter"
                # otherwise, same dual role pick_item's own search
                # command already establishes.
                query = None
                working_set = await _load()
                page_index = 0
                page_users = await _render()
                continue
            matches = [u for u in all_users if typed.lower() in u.username.lower()]
            if not matches:
                await session.write_line("No matches.")
                await session.write("Choice: ")
                continue
            if len(matches) == 1:
                return matches[0]
            query = typed
            working_set = matches
            page_index = 0
            page_users = await _render()
            continue

        if key_lower == "g":
            await session.write_line("")
            await session.write("Go to #: ")
            raw = (await session.read_line()).strip()
            try:
                target_id = int(raw)
            except ValueError:
                await session.write_line("Not a number.")
                await session.write("Choice: ")
                continue
            # Always searches the full, unfiltered list at the current
            # sort -- a goto number means the same account regardless of
            # any active search filter, matching the "(#N)" shown next
            # to every displayed row (pick_item's own goto establishes
            # this same "ignore the search filter" rule).
            for user in await _load(apply_search=False):
                if user.id == target_id:
                    return user
            await session.write_line("Out of range.")
            await session.write("Choice: ")
            continue

        if key.isdigit():
            second = await session.read_key()
            if not second.isdigit():
                # Only `key` (the first digit) was actually echoed --
                # `second` here is either an ordinary unrecognized
                # character (also echoed, erase both) or REDRAW_KEY/
                # REFRESH_KEY (never echoed, erase just the one real
                # character on screen, same reasoning as
                # reject_unhandled_key itself).
                erase_count = 1 if second in (REDRAW_KEY, REFRESH_KEY) else 2
                await session.write(reject_keystroke(erase_count))
                continue
            number = int(key + second)
            if 1 <= number <= len(page_users):
                await session.write_line("")
                return page_users[number - 1]
            await session.write(reject_keystroke(2))
            continue

        await session.write(reject_unhandled_key(key))


async def _pick_and_edit_user(
    session: Session, lane: DatabaseLane, actor: User, node_controls: NodeControls | None, *, title: str
) -> None:
    """
    Every per-user action funnels through here now (design doc -- node
    management, Thiesi's own dogfood-testing report: SysOps wanted one
    central editor rather than picking the same user again through
    three separate single-purpose screens to promote them, then disable
    them, then...). `title` is the only thing that still varies by
    which top-level `[U]sers` menu entry got here -- `[L]ist users`/
    `[P]romote/demote`/`[E]nable/disable`/`[D]elete user` all land on
    the exact same full editor once a user is actually selected, so a
    SysOp who only meant to promote someone can still also disable them
    right there without leaving and re-picking them a second time.
    """
    target = await _pick_target_user(session, lane, actor, title=title)
    if target is not None:
        await _user_detail_screen(session, lane, actor, target, node_controls)


def _status_label(user: User) -> str:
    if user.disabled_at is not None:
        return "disabled"
    if user.pending_approval:
        return "pending approval"
    return "active"


def _user_description(user: User) -> str:
    return f"level {user.user_level}, {_status_label(user)}"


def _user_detail_field_line(hotkey: str, label: str, value: str, *, selected: str | None, accent: int) -> str:
    """Dogfood feature request, issue #160's cursor-navigation follow-up
    (item 1 of the prioritized list): the same `>`-cursor/accent-color
    highlight convention `netbbs.net.resource_editor.edit_resource_draft`
    already renders its own fields with -- duplicated rather than
    imported, since this screen is a bespoke dispatch loop (below), not
    a draft-editor call. One `colored()` call per side, same reasoning
    as that module's own field-row rendering: a marker/label split
    across two `colored()` calls would insert an SGR reset between them,
    breaking what should read as one contiguous highlighted run."""
    prefix = (
        colored(f"> {label}", fg_color=accent, bold=True)
        if selected == hotkey
        else colored(f"  {label}", fg_color=LABEL_COLOR)
    )
    return f"{prefix}: {colored(value, fg_color=MUTED_COLOR)}"


async def _draw_user_detail(
    session: Session, lane: DatabaseLane, target: User, description_level: str, redraw_in_place: bool,
    unicode_style: bool,
    collapsed: bool,
    *,
    selected: str | None = None,
) -> bool:
    """Returns whether `target` is currently on the local blocklist --
    unlike `disabled_at`, blocked status isn't a field on `User` itself,
    so `_user_detail_screen`'s dispatch loop needs it back to know
    which of `[R]estrict`'s two directions a confirmation should offer,
    same shape `_render_profile`/`_draw_channel_detail` already use to
    hand a caller-needed piece of drawn state back to their own dispatch
    loops.

    `selected` (issue #160's cursor-navigation follow-up), if given one
    of this screen's five field hotkeys (`l`/`t`/`i`/`k`/`r`), highlights
    that line's own `>` cursor -- `None` (nothing arrow-highlighted yet,
    or a `[A]pprove`/`[D]elete`/`[B]ack` action, none of which are
    arrow-selectable fields, matching `edit_resource_draft`'s own
    Save/Back convention) renders identically to before this feature."""
    await session.write_line(
        "\r\n" + screen_title(sanitize_text(target.username),
            breadcrumb=(session.node_display_name,), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            header_color=await lane.run(effective_header_color_256), node_name_gradient=session.node_name_gradient)
    )
    accent = await lane.run(effective_accent_color_256)
    await session.write_line(_user_detail_field_line("l", "Level", str(target.user_level), selected=selected, accent=accent))
    await session.write_line(_user_detail_field_line("t", "Status", _status_label(target), selected=selected, accent=accent))
    display_format, display_timezone = await lane.run(resolve_display_preferences)
    member_since = format_for_display(
        target.created_at, override_format=display_format, override_timezone=display_timezone
    )
    await session.write_line(f"Member since: {member_since}")
    # Design doc §18: a narrow, SysOp-grantable permission independent
    # of the four moderator scope tiers.
    await session.write_line(
        _user_detail_field_line(
            "i", "Can verify identity (age/name attestation)",
            "yes" if target.can_verify_identity else "no", selected=selected, accent=accent,
        )
    )
    await session.write_line(
        _user_detail_field_line(
            "k", "Public key (SSH/Link login)",
            target.fingerprint if target.fingerprint else "(none)", selected=selected, accent=accent,
        )
    )
    # Dogfood follow-up (`netbbs.moderation.blocklist`): the local
    # blocklist enforcement path was real and already wired into login
    # (`netbbs.net.login_flow`'s own distinct "Your access to this
    # system has been revoked." message), but nothing in the
    # interactive product could ever create an entry -- only a
    # dev/admin script (`scripts/block_user.py`) could. `[T]oggle
    # enable/disabled` already covers "stop this local account from
    # logging in" for most purposes; this is additionally fingerprint-
    # based when the account has a keypair, the form the same
    # mechanism is designed to extend to remote nodes/traffic later
    # (module docstring) -- a real, separate capability, not just a
    # second button for the same thing.
    blocked = await lane.run(is_blocked, target)
    await session.write_line(
        _user_detail_field_line("r", "Blocked (local blocklist)", "yes" if blocked else "no", selected=selected, accent=accent)
    )

    entries = await lane.run(list_actions_for_target_user, target.id)
    if not entries:
        await session.write_line(colored("No recorded admin actions.", fg_color=MUTED_COLOR))
    else:
        # Dogfood follow-up: this list used to show *what* happened but
        # never *who* did it, even though `actor_user_id` is stored for
        # exactly this (`netbbs.moderation.log.ModerationLogEntry`'s own
        # docstring: nullable specifically so a deleted actor's audit
        # trail survives, implying display was always the intent) --
        # the in-channel notice for the same action already says "by
        # bob," this screen just never repeated it. Resolved once for
        # the shown slice, not once per entry.
        shown = entries[-10:]
        actor_ids = {entry.actor_user_id for entry in shown if entry.actor_user_id is not None}
        actor_usernames: dict[int, str] = {}
        for actor_id in actor_ids:
            actor = await lane.run(get_user_by_id, actor_id)
            actor_usernames[actor_id] = actor.username if actor is not None else "(deleted account)"

        await session.write_line(colored("Recent admin actions:", fg_color=MUTED_COLOR))
        for entry in shown:
            when = format_for_display(
                entry.created_at, override_format=display_format, override_timezone=display_timezone
            )
            detail = f" -- {sanitize_text(entry.detail)}" if entry.detail else ""
            by = actor_usernames.get(entry.actor_user_id, "(unknown)") if entry.actor_user_id is not None else "(system)"
            await session.write_line(f"  {when}: {sanitize_text(entry.action)} (by {sanitize_text(by)}){detail}")

    options = []
    if target.pending_approval:
        options.append(MenuEntry(label=menu_key("A", "pprove"), brief="Approve this pending signup"))
    options.append(MenuEntry(label=menu_key("L", "evel"), brief="Change this user's access level"))
    options.append(MenuEntry(label=menu_key("T", "oggle enable/disabled"), brief="Enable or disable this account"))
    options.append(MenuEntry(label=menu_key("I", "dentity verification"), brief="Grant/revoke attestation rights"))
    options.append(MenuEntry(label=menu_key("K", "ey"), brief="View/replace this user's SSH key"))
    options.append(MenuEntry(label=menu_key("R", "estrict login"), brief="Block or unblock this account"))
    options.append(MenuEntry(label=menu_key("D", "elete"), brief="Permanently remove this user"))
    options.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to the picker"))
    await session.write_line(
        "\r\n"
        + _menu_row(options, description_level, width=session.terminal_width, height=session.terminal_height)
    )
    await session.write_line(colored("(Ctrl-H for help on these fields)", fg_color=MUTED_COLOR))
    await session.write("Choice: ")
    return blocked


_USER_DETAIL_FIELD_ORDER = ("l", "t", "i", "k", "r")


async def _read_user_detail_key(session: Session) -> EditorKey:
    """`netbbs.net.resource_editor._read_navigable_key`'s own fallback
    shape, duplicated here rather than imported -- this project's own
    "duplicate rather than reach into another module's private helper"
    convention (see `netbbs.link.files._file_area_from_row`'s own
    docstring for the identical reasoning), since this screen is a
    bespoke dispatch loop (below), not an `edit_resource_draft` caller:
    `[A]pprove` (conditional) and `[D]elete` (which ends the whole
    screen mid-field, not just mutates one) don't fit that shared
    driver's fields-plus-save-plus-back shape, so this screen keeps its
    own hand-rolled loop and borrows only the cursor-navigation
    mechanics, matching this same file's own `_USER_SORT_MODES`
    precedent for when a bespoke screen beats a generic extension.

    `distinguish_ctrl_h=True` (dogfood feature request: this screen had
    no on-demand help at all until now) -- without it, real byte 0x08
    collapses into `BACKSPACE`, unreachable as help the same way
    `edit_resource_draft`'s own Ctrl-H was before that bug was found and
    fixed (see that module's `_read_navigable_key` docstring). This
    screen never needs a real Backspace at its own top level either."""
    read_editor_key = getattr(session, "read_editor_key", None)
    if read_editor_key is not None:
        try:
            return await read_editor_key(distinguish_ctrl_h=True)
        except NotImplementedError:
            pass
    raw = await session.read_key()
    return EditorKey(EditorKeyKind.CHAR, char=raw)


# Ctrl-H's own content for the five arrow-selectable fields
# (`_USER_DETAIL_FIELD_ORDER`) -- dogfood feature request, this screen
# had no on-demand help at all until now. Keyed the same as
# `_USER_DETAIL_FIELD_ORDER`, not a `FieldSpec` list, since this
# screen's status lines are drawn by `_draw_user_detail` directly, not
# through `netbbs.net.resource_editor`.
_USER_DETAIL_HELP: dict[str, tuple[str, str]] = {
    "l": (
        "Level",
        "The account's permission level. 0 is an ordinary caller; higher numbers unlock "
        "moderator/SysOp capability.",
    ),
    "t": (
        "Status",
        "Enable/disable this account. A disabled account can't log in; existing posts/"
        "files/messages they created are untouched.",
    ),
    "i": (
        "Can verify identity",
        "A narrow, SysOp-grantable permission (design doc §18) letting this account "
        "perform age/name attestation for other callers -- independent of the four "
        "moderator scope tiers.",
    ),
    "k": (
        "Public key",
        "This account's SSH/Link public key. Setting or replacing it here lets them log "
        "in over SSH with key-based authentication.",
    ),
    "r": (
        "Blocked",
        "The local blocklist -- a separate, fingerprint-based mechanism from Status/"
        "disable, designed to extend to remote nodes/traffic later. Unlike disabling, a "
        "block also survives the account being re-enabled.",
    ),
}


async def _show_user_detail_help(
    session: Session, lane: DatabaseLane, *, selected: str | None, unicode_style: bool = False
) -> None:
    """Same "narrow to the highlighted field if one is selected, else
    list everything" shape `netbbs.net.resource_editor._show_field_help`
    already establishes for `edit_resource_draft`'s own Ctrl-H."""
    header_color = await lane.run(effective_header_color_256)
    if selected is not None:
        label, help_text = _USER_DETAIL_HELP[selected]
        await show_help(
            session, "Field help", [colored(label, fg_color=header_color, bold=True), f"  {help_text}"],
            header_color=header_color, unicode_style=unicode_style,
        )
        return
    lines: list[str] = []
    for label, help_text in _USER_DETAIL_HELP.values():
        lines.append(colored(label, fg_color=header_color, bold=True))
        lines.append(f"  {help_text}")
        lines.append("")
    await show_help(session, "Field help", lines[:-1], header_color=header_color, unicode_style=unicode_style)


async def _user_detail_screen(
    session: Session, lane: DatabaseLane, actor: User, target: User, node_controls: NodeControls | None
) -> None:
    """
    The single per-user action screen every `[U]sers` submenu entry
    lands on now (design doc -- node management, Thiesi's own dogfood-
    testing report), mirroring the board/channel/file-area admin
    screens' own established "draw status, dispatch a lettered action,
    redraw" shape rather than the linear one-pass-of-prompts this used
    to be. A SysOp can now promote, then disable, then delete the exact
    same already-selected account without leaving this screen or
    re-picking them through three separate single-purpose flows.

    Dogfood feature request, issue #160's cursor-navigation follow-up
    (item 1 of the prioritized list): the five status lines
    (`_USER_DETAIL_FIELD_ORDER`) are also reachable by moving a `>`
    cursor with Up/Down and activating the highlighted one with Space
    or Enter -- purely additive, every hotkey letter keeps working
    exactly as before. `[A]pprove`/`[D]elete`/`[B]ack` are never
    arrow-selectable, the same "always hotkey-only" treatment
    `edit_resource_draft` already gives its own Save/Back.
    """
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    selected: str | None = None
    blocked = await _draw_user_detail(
        session, lane, target, description_level, redraw_in_place, unicode_style, collapsed, selected=selected
    )
    while True:
        key = await _read_user_detail_key(session)

        if key.kind == EditorKeyKind.UP:
            index = _USER_DETAIL_FIELD_ORDER.index(selected) if selected in _USER_DETAIL_FIELD_ORDER else 0
            selected = _USER_DETAIL_FIELD_ORDER[(index - 1) % len(_USER_DETAIL_FIELD_ORDER)]
            blocked = await _draw_user_detail(
                session, lane, target, description_level, redraw_in_place, unicode_style, collapsed, selected=selected
            )
            continue
        if key.kind == EditorKeyKind.DOWN:
            index = _USER_DETAIL_FIELD_ORDER.index(selected) if selected in _USER_DETAIL_FIELD_ORDER else -1
            selected = _USER_DETAIL_FIELD_ORDER[(index + 1) % len(_USER_DETAIL_FIELD_ORDER)]
            blocked = await _draw_user_detail(
                session, lane, target, description_level, redraw_in_place, unicode_style, collapsed, selected=selected
            )
            continue
        if key.kind == EditorKeyKind.ESCAPE:
            if selected is not None:
                selected = None
                blocked = await _draw_user_detail(
                    session, lane, target, description_level, redraw_in_place, unicode_style, collapsed,
                    selected=selected,
                )
                continue
            await session.write("\a")
            continue
        if key.kind == EditorKeyKind.CTRL and key.char == "h":
            await _show_user_detail_help(session, lane, selected=selected, unicode_style=unicode_style)
            blocked = await _draw_user_detail(
                session, lane, target, description_level, redraw_in_place, unicode_style, collapsed, selected=selected
            )
            continue
        if key.kind == EditorKeyKind.ENTER or (key.kind == EditorKeyKind.CHAR and key.char == " "):
            if selected is None:
                await session.write("\a")
                continue
            choice = selected
        elif key.kind == EditorKeyKind.CHAR and key.char is not None:
            choice = key.char.lower()
            if choice == HELP_KEY:
                # A session with no real `read_editor_key` (falls back
                # to plain `read_key()`) delivers Ctrl-H as an ordinary
                # character, never as `EditorKeyKind.CTRL` -- same dual
                # path `edit_resource_draft` itself handles.
                await _show_user_detail_help(session, lane, selected=selected, unicode_style=unicode_style)
                blocked = await _draw_user_detail(
                    session, lane, target, description_level, redraw_in_place, unicode_style, collapsed,
                    selected=selected,
                )
                continue
            if choice in _USER_DETAIL_FIELD_ORDER:
                selected = choice
        else:
            # Left/Right/Backspace/Tab/Home/End/Page Up/Page Down --
            # nothing on this screen defines a step, same silent no-op
            # `edit_resource_draft` gives Left/Right on a step-less field.
            continue

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "a" and target.pending_approval:
            await session.write_line("")
            if await prompt_yes_no(session, "Approve this account so it can log in?", default=False):
                target = await lane.run(approve_pending_user, target, approved_by=actor)
                await session.write_line(f"{target.username!r} approved.")
            blocked = await _draw_user_detail(
                session, lane, target, description_level, redraw_in_place, unicode_style, collapsed, selected=selected,
            )
        elif choice == "l":
            await session.write_line("")
            await session.write(f"New level for {target.username!r} [{target.user_level}]: ")
            raw = (await session.read_line()).strip()
            if raw:
                try:
                    new_level = int(raw)
                except ValueError:
                    await session.write_line(colored("Not a number -- cancelled.", fg_color=MUTED_COLOR))
                else:
                    try:
                        target = await lane.run(set_user_level, target, new_level, changed_by=actor)
                    except UserManagementError as exc:
                        await session.write_line(colored(str(exc), fg_color=MUTED_COLOR))
                    else:
                        await session.write_line(f"{target.username!r} is now level {target.user_level}.")
            blocked = await _draw_user_detail(
                session, lane, target, description_level, redraw_in_place, unicode_style, collapsed, selected=selected,
            )
        elif choice == "t":
            await session.write_line("")
            currently_disabled = target.disabled_at is not None
            action_word = "Enable" if currently_disabled else "Disable"
            if await prompt_yes_no(session, f"{action_word} {target.username!r}?", default=False):
                try:
                    target = await lane.run(set_user_disabled, target, not currently_disabled, changed_by=actor)
                except UserManagementError as exc:
                    await session.write_line(colored(str(exc), fg_color=MUTED_COLOR))
                else:
                    await session.write_line(
                        f"{target.username!r} is now {'disabled' if target.disabled_at is not None else 'active'}."
                    )
                    if target.disabled_at is not None:
                        await _revoke_live_sessions(session, node_controls, target, actor)
            blocked = await _draw_user_detail(
                session, lane, target, description_level, redraw_in_place, unicode_style, collapsed, selected=selected,
            )
        elif choice == "i":
            await session.write_line("")
            new_state = "revoke" if target.can_verify_identity else "grant"
            if await prompt_yes_no(
                session, f"{new_state.capitalize()} identity-verification permission?", default=False
            ):
                target = await lane.run(
                    set_can_verify_identity, target, not target.can_verify_identity, changed_by=actor
                )
                await session.write_line(
                    f"{target.username!r} can now verify identity: "
                    f"{'yes' if target.can_verify_identity else 'no'}."
                )
            blocked = await _draw_user_detail(
                session, lane, target, description_level, redraw_in_place, unicode_style, collapsed, selected=selected,
            )
        elif choice == "k":
            await session.write_line("")
            verb = "Replace" if target.fingerprint else "Add"
            await session.write(f"{verb} public key (base64, or an ssh-ed25519 line, blank to cancel): ")
            text = (await session.read_line()).strip()
            if text:
                try:
                    verify_key = parse_verify_key(text)
                except IdentityError as exc:
                    await session.write_line(colored(f"Could not parse key: {exc}", fg_color=MUTED_COLOR))
                else:
                    try:
                        target = await lane.run(set_verify_key, target, verify_key, changed_by=actor)
                    except AuthError as exc:
                        await session.write_line(colored(str(exc), fg_color=MUTED_COLOR))
                    else:
                        await session.write_line(f"Public key set for {target.username!r}.")
            blocked = await _draw_user_detail(
                session, lane, target, description_level, redraw_in_place, unicode_style, collapsed, selected=selected,
            )
        elif choice == "r":
            await session.write_line("")
            action_word = "Unrestrict" if blocked else "Restrict"
            prompt = (
                f"{action_word} {target.username!r} from logging in?"
                if not blocked
                else f"{action_word} {target.username!r} (allow login again)?"
            )
            if await prompt_yes_no(session, prompt, default=False):
                if blocked:
                    await lane.run(unblock_user, target)
                    await session.write_line(f"{target.username!r} can log in again.")
                else:
                    try:
                        await lane.run(block_user, target, blocked_by=actor)
                    except BlocklistError as exc:
                        await session.write_line(colored(str(exc), fg_color=MUTED_COLOR))
                    else:
                        await session.write_line(f"{target.username!r} is now blocked from logging in.")
                        await _revoke_live_sessions(session, node_controls, target, actor)
            blocked = await _draw_user_detail(
                session, lane, target, description_level, redraw_in_place, unicode_style, collapsed, selected=selected,
            )
        elif choice == "d":
            await session.write_line("")
            deleted = await _delete_user_confirm(session, lane, actor, target, node_controls)
            if deleted:
                return
            blocked = await _draw_user_detail(
                session, lane, target, description_level, redraw_in_place, unicode_style, collapsed, selected=selected,
            )
        else:
            await session.write(reject_unhandled_key(choice))


async def _delete_user_confirm(
    session: Session, lane: DatabaseLane, actor: User, target: User, node_controls: NodeControls | None
) -> bool:
    """Returns whether the account was actually deleted -- the caller
    (`_user_detail_screen`) uses this to know whether to return
    entirely (nothing left worth redrawing) or keep showing the same,
    unchanged detail screen (a declined confirmation)."""
    await session.write_line(
        colored(
            "\r\nThis permanently deletes the account. Posts and files they created "
            "keep their recorded author name; their entries in Last sessions also "
            "survive, keeping whatever name-visibility choice was in effect at the "
            "time (a prior opt-out stays hidden -- SysOps still see the real name "
            "regardless); moderator grants, chat channel membership/invitations, "
            "preferences, and blocklist entries tied to this account are removed. "
            "This cannot be undone.",
            fg_color=MUTED_COLOR,
        )
    )
    await session.write(f"Type the username {target.username!r} to confirm, or anything else to cancel: ")
    confirmation = (await session.read_line()).strip()
    if confirmation != target.username:
        await session.write_line("Cancelled.")
        return False
    try:
        await lane.run(delete_user, target, deleted_by=actor)
    except UserManagementError as exc:
        await session.write_line(colored(str(exc), fg_color=MUTED_COLOR))
        return False
    await session.write_line(f"{target.username!r} deleted.")
    await _revoke_live_sessions(session, node_controls, target, actor)
    return True


# -- self-service registration settings (design doc) -----------


_REGISTRATION_MODE_LABELS = {
    RegistrationMode.OPEN: "open (new accounts active immediately)",
    RegistrationMode.APPROVAL_REQUIRED: "approval required (SysOp must approve new accounts)",
    RegistrationMode.CLOSED: "closed (no public registration; SysOp-created accounts only)",
}


async def _registration_settings_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """
    Sets the node's `registration_mode` (design doc) --
    open/approval_required/closed, replacing the earlier plain
    require-approval toggle -- and surfaces how many self-registered
    accounts are currently waiting on approval. Approving/rejecting any
    of them individually still happens via `[L]ist users` -> a pending
    account's own detail screen (`_user_detail_screen`'s `[A]pprove` action),
    reusing the existing user-management flow rather than building a
    second, parallel pending-accounts queue UI.
    """
    def _load(db: Database) -> tuple[RegistrationMode, int]:
        return get_registration_mode(db), sum(1 for u in list_users(db) if u.pending_approval)

    current, pending_count = await lane.run(_load)

    header = colored("\r\nSelf-service registration:", fg_color=await lane.run(effective_header_color_256), bold=True)
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
        + action_bar(
            [
                menu_key("O", "pen"),
                menu_key("A", "pproval required"),
                menu_key("C", "losed"),
                menu_key("B", "ack (leave unchanged)"),
            ],
            width=session.terminal_width,
        )
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


# -- self-update (design doc §17) --


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

    A failed check (network/API error) records `"check failed: ..."`
    too, not just the two success outcomes -- a real gap traced from a
    SysOp's own console report of a transient TLS error: before this,
    `record_check_outcome` was only ever called on success, so a run of
    consecutive failing days (scheduled or manual) left this screen's
    own "Last check: ..." line silently showing a stale success from
    however long ago, with nothing in the product itself distinguishing
    "quiet because it's fine" from "quiet because it's been failing" --
    the console's own warning line was the only place that ever showed,
    which nobody reliably watches.

    The optional GitHub token (`netbbs.selfupdate.get_github_pat`/
    `set_github_pat`) is the real fix for a node whose release checks
    keep hitting GitHub's unauthenticated 60/hour-per-source-IP limit
    (an ordinary dev-loop restart pattern, or a genuine crash-restart
    loop): authenticated requests get 5000/hour instead. Read masked
    (last 4 characters only, `masked_github_pat`) and never re-displayed
    in full once set -- entered via `read_line(echo=False)`, the same
    masked-input primitive password prompts use, and stored in a plain,
    owner-only file next to the database (`github_pat_path`), never in
    the plaintext `node_config` table -- see that function's own
    docstring for why. The prompt copy names the exact minimal scope
    needed ("Public Repositories, read-only") since this screen has no
    way to enforce what scope a pasted token actually carries.
    """
    from netbbs import __version__ as current_version

    def _load(db: Database) -> tuple[bool, str | None, str | None, str | None]:
        auto_enabled = get_auto_update_check_enabled(db)
        checked_at, outcome = get_last_check_summary(db)
        return auto_enabled, checked_at, outcome, masked_github_pat(db)

    auto_enabled, checked_at, outcome, masked_token = await lane.run(_load)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    header_color = await lane.run(effective_header_color_256)

    await session.write_line(
        "\r\n"
        + screen_title(
            "Self-update",
            breadcrumb=(session.node_display_name, "System"),
            subtitle="Release checks only; applying an update remains an operator action.",
            width=session.terminal_width,
            clear=redraw_in_place,
            unicode_style=unicode_style, collapsed=collapsed,
            header_color=header_color,
        node_name_gradient=session.node_name_gradient)
    )
    await session.write_line(
        colored("Running version: ", fg_color=LABEL_COLOR)
        + colored(current_version, fg_color=METADATA_COLOR)
    )
    auto_badge = (
        status_badge("ON", tone="success", unicode_style=unicode_style)
        if auto_enabled
        else status_badge("OFF", tone="warning", unicode_style=unicode_style)
    )
    await session.write_line(
        colored("Daily automatic check: ", fg_color=LABEL_COLOR) + auto_badge
    )
    await session.write_line(
        colored("GitHub token: ", fg_color=LABEL_COLOR)
        + (
            status_badge(f"set ({masked_token})", tone="success", unicode_style=unicode_style)
            if masked_token is not None
            else status_badge("not set", tone="neutral", unicode_style=unicode_style)
        )
    )
    if checked_at is not None:
        display_format, display_timezone = await lane.run(resolve_display_preferences)
        when = format_for_display(checked_at, override_format=display_format, override_timezone=display_timezone)
        await session.write_line(
            colored("Last check: ", fg_color=LABEL_COLOR)
            + colored(f"{when} -- {sanitize_text(outcome or '')}", fg_color=METADATA_COLOR)
        )
        # Dogfood follow-up: "Last check" alone couldn't distinguish
        # "runs on a healthy schedule" from "happened to succeed once"
        # -- a few of the most recent runs make a gap or a run of
        # consecutive failures visible at a glance.
        history = await lane.run(list_operational_run_history, "update_check", limit=5)
        if len(history) > 1:
            await session.write_line(colored("Recent checks:", fg_color=MUTED_COLOR))
            for run in history:
                run_when = format_for_display(
                    run.created_at, override_format=display_format, override_timezone=display_timezone
                )
                await session.write_line(f"  {run_when}: {sanitize_text(run.outcome)}")
    else:
        await session.write_line(colored("No check has been run on this node yet.", fg_color=MUTED_COLOR))

    if await prompt_yes_no(session, "\r\nCheck for a new release now?", default=False):
        known_etag, known_release = await lane.run(load_release_cache)
        token = await lane.run(get_github_pat)
        try:
            release, new_etag = await check_latest_release(
                known_etag=known_etag, known_release=known_release, token=token
            )
        except UpdateError as exc:
            await lane.run(record_check_outcome, f"check failed: {exc}")
            await session.write_line(colored(f"Could not check for updates: {exc}", fg_color=ERROR_COLOR))
        else:
            await lane.run(save_release_cache, new_etag, release)
            if is_newer(current_version, release.tag_name):
                await lane.run(record_check_outcome, f"newer release available: {release.tag_name}")
                await session.write_line(
                    status_badge("UPDATE AVAILABLE", tone="warning", unicode_style=unicode_style)
                    + " "
                    + colored(
                        f"{release.tag_name} (published {release.published_at}).",
                        fg_color=WARNING_COLOR,
                    )
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
                await session.write_line(
                    status_badge("UP TO DATE", tone="success", unicode_style=unicode_style)
                    + " "
                    + colored(current_version, fg_color=SUCCESS_COLOR)
                )

    token_prompt = (
        "\r\nReplace or clear the stored GitHub token?" if masked_token is not None
        else "\r\nSet a GitHub token to raise the update-check rate limit (60/hour -> 5000/hour)?"
    )
    if await prompt_yes_no(session, token_prompt, default=False):
        await session.write_line(
            colored(
                "Paste a fine-grained personal access token scoped to "
                "'Public Repositories (read-only)' -- no broader access is needed. "
                + ("Blank clears the existing token." if masked_token is not None else "Blank cancels."),
                fg_color=MUTED_COLOR,
            )
        )
        await session.write(colored("Token: ", fg_color=LABEL_COLOR, bold=True))
        token_input = (await session.read_line(echo=False)).strip()
        if token_input:
            def _apply_token(db: Database) -> None:
                set_github_pat(db, token_input)
                record_action(db, actor=actor, action="set_github_pat", detail=f"token ending {token_input[-4:]}")

            await lane.run(_apply_token)
            await session.write_line("GitHub token saved.")
        elif masked_token is not None:
            def _clear_token(db: Database) -> None:
                clear_github_pat(db)
                record_action(db, actor=actor, action="clear_github_pat")

            await lane.run(_clear_token)
            await session.write_line("GitHub token cleared.")
        else:
            await session.write_line(colored("Cancelled -- no change.", fg_color=MUTED_COLOR))

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
    history = await lane.run(list_operational_run_history, "backup", limit=5)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    header_color = await lane.run(effective_header_color_256)

    await session.write_line(
        "\r\n"
        + screen_title(
            "Backup status",
            breadcrumb=(session.node_display_name, "System"),
            subtitle="Last recorded operator backup for this node.",
            width=session.terminal_width,
            clear=redraw_in_place,
            unicode_style=unicode_style, collapsed=collapsed,
            header_color=header_color,
        node_name_gradient=session.node_name_gradient)
    )
    if checked_at is not None:
        display_format, display_timezone = await lane.run(resolve_display_preferences)
        when = format_for_display(checked_at, override_format=display_format, override_timezone=display_timezone)
        await session.write_line(status_badge("BACKED UP", tone="success", unicode_style=unicode_style))
        await session.write_line(
            colored("Last backup: ", fg_color=LABEL_COLOR) + colored(when, fg_color=METADATA_COLOR)
        )
        await session.write_line(
            colored("Location: ", fg_color=LABEL_COLOR)
            + colored(sanitize_text(path or ""), fg_color=METADATA_COLOR)
        )
    else:
        await session.write_line(
            empty_state(
                "No backup recorded",
                detail="No backup has been taken on this node yet.",
                width=session.terminal_width,
                header_color=header_color,
            )
        )
    await session.write_line(
        colored("Run 'python -m netbbs.backup create --to <path>' to create one.", fg_color=MUTED_COLOR)
    )
    # Dogfood follow-up: the single "Last backup" line above couldn't
    # distinguish "runs on a healthy schedule" from "happened to
    # succeed once" -- a few of the most recent runs make a gap or an
    # irregular cadence visible at a glance.
    if len(history) > 1:
        display_format, display_timezone = await lane.run(resolve_display_preferences)
        await session.write_line(colored("\r\nRecent backups:", fg_color=MUTED_COLOR))
        for run in history:
            when = format_for_display(
                run.created_at, override_format=display_format, override_timezone=display_timezone
            )
            await session.write_line(f"  {when}: {sanitize_text(run.outcome)}")


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

    Dogfood feature request, issue #160's cursor-navigation follow-up
    (item 3 of the prioritized list): the two independent settings --
    format controls the *shape* of a displayed timestamp, timezone
    controls *which instant* it shows (see `format_for_display`'s own
    docstring for why getting one right without the other still leaves
    users looking at the wrong wall-clock time, just reshaped) -- used
    to always be asked back-to-back in one linear pass. Rebuilt as an
    immediate-mode `edit_resource_draft` screen (`save=None`, the same
    shape `netbbs.net.login_flow`'s own profile screen uses): each
    field persists itself the instant it's edited, so a SysOp who only
    wants to fix one genuinely never has to visit the other at all.
    """
    fmt, tz_name = await lane.run(resolve_display_preferences)
    draft: dict = {"format": fmt, "timezone": tz_name}

    async def _format_field(session: Session, lane: DatabaseLane, draft: dict) -> None:
        await session.write(f"New format [{draft['format']!r}] (blank to leave unchanged): ")
        new_fmt = (await session.read_line()).strip()
        if not new_fmt:
            return

        def _apply(db: Database) -> None:
            set_display_format(db, new_fmt)
            record_action(db, actor=actor, action="set_display_format", detail=new_fmt)

        try:
            await lane.run(_apply)
        except ValueError as exc:
            await session.write_line(colored(str(exc), fg_color=MUTED_COLOR))
        else:
            draft["format"] = new_fmt

    async def _timezone_field(session: Session, lane: DatabaseLane, draft: dict) -> None:
        await session.write(f"New timezone [{draft['timezone']}] (blank to leave unchanged): ")
        new_tz = (await session.read_line()).strip()
        if not new_tz:
            return

        def _apply(db: Database) -> None:
            set_display_timezone(db, new_tz)
            record_action(db, actor=actor, action="set_display_timezone", detail=new_tz)

        try:
            await lane.run(_apply)
        except ValueError as exc:
            await session.write_line(colored(str(exc), fg_color=MUTED_COLOR))
        else:
            draft["timezone"] = new_tz

    fields = [
        FieldSpec(
            key="format", hotkey="f", menu_text=menu_key("F", "ormat"), label="Format",
            render=lambda d: d["format"], prompt=_format_field,
            help=(
                "A Python strftime pattern controlling the *shape* of every displayed "
                "timestamp node-wide, e.g. '%Y-%m-%d %H:%M' for '2026-08-22 14:05'. "
                "Doesn't affect which instant is shown -- see Timezone for that."
            ),
        ),
        FieldSpec(
            key="timezone", hotkey="z", menu_text=menu_key("z", "one", prefix="Time"), label="Timezone",
            render=lambda d: d["timezone"], prompt=_timezone_field,
            help=(
                "An IANA timezone name (e.g. 'Europe/Berlin', 'America/New_York') "
                "controlling *which instant* every displayed timestamp shows, node-wide. "
                "Getting this wrong leaves every timestamp reshaped but still pointing at "
                "the wrong wall-clock time."
            ),
        ),
    ]
    await edit_resource_draft(
        session, lane,
        title="Timestamp display",
        fields=fields,
        draft=draft, save=None,
        back_menu_text=menu_key("B", "ack"),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )


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
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    header_color = await lane.run(effective_header_color_256)

    await session.write_line(
        "\r\n"
        + screen_title(
            "Link status",
            breadcrumb=(session.node_display_name, "System"),
            subtitle="Identity, capacity, relay activity, and verified peers.",
            width=session.terminal_width,
            clear=redraw_in_place,
            unicode_style=unicode_style, collapsed=collapsed,
            header_color=header_color,
        node_name_gradient=session.node_name_gradient)
    )
    await session.write_line(
        colored("Node fingerprint: ", fg_color=LABEL_COLOR)
        + colored(
            sanitize_text(link_context.node_identity.fingerprint),
            fg_color=METADATA_COLOR,
        )
    )

    if config is not None:
        await session.write_line(
            colored("Mode: ", fg_color=LABEL_COLOR)
            + badge("OUTGOING ONLY" if config.outgoing_only else "FULL PEER", tone="neutral")
        )
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
    if config is not None:
        carried = await lane.run(carried_board_count, link_context.node_identity.fingerprint)
        await session.write_line(f"Carried boards: {carried}/{config.max_carried_boards}")
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
        no_peers_message = "No verified peers." if config is None else f"No verified peers. (max {config.max_peers})"
        await session.write_line(colored(f"\r\n{no_peers_message}", fg_color=MUTED_COLOR))
        return

    peers_line = (
        f"Verified peers: {len(node.peers)}" if config is None else f"Verified peers: {len(node.peers)}/{config.max_peers}"
    )
    await session.write_line(f"\r\n{peers_line}")

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
        stable_id_of=lambda peer: id(peer),  # in-memory only, no persisted/NetBBS-owned identifier exists here
        description_of=_peer_description,
        title="Verified peers",
        empty_message="No verified peers.",
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
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


# -- outbox: work-item inspection/replay/cancel (design doc §13.7, ----------
# -- issue #60's second operational slice) ----------------------------------


async def _outbox_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """
    SysOp inspection, replay, and cancellation for outbound Link work
    items -- scoped to exactly what `netbbs.link.work_items` itself
    covers (Link mail delivery and acknowledgement delivery), never
    gossip or relay maintenance, which don't fit this model (see that
    module's own docstring for why not).

    The picker only ever offers `retrying`/`dead_lettered` items --
    `pending` will be attempted on its own within one sync pass,
    `pushed`/`cancelled` are already resolved, so there's nothing a
    SysOp would act on for either. Replaying a dead-lettered
    `link_mail_delivery` item also undoes its `mail_messages.
    link_delivery_status = 'expired'` side effect back to `'pending'`
    -- the one place that undo happens, symmetric with `netbbs.link.
    sync`'s own dead-letter side effect.
    """
    def _load(db: Database) -> list[WorkItem]:
        return list_work_items(db)

    items = await lane.run(_load)

    await session.write_line(colored("\r\nOutbox:", fg_color=await lane.run(effective_header_color_256), bold=True))
    if not items:
        await session.write_line(colored("No outbound work items recorded yet.", fg_color=MUTED_COLOR))
        return

    counts: dict[str, int] = {}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    await session.write_line(", ".join(f"{status}: {count}" for status, count in sorted(counts.items())))

    actionable = [item for item in items if item.status in ("retrying", "dead_lettered")]
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    arrow = " → " if unicode_style else " -> "
    selected = await pick_item(
        session, actionable,
        name_of=lambda item: f"{item.kind}{arrow}{item.target_fingerprint}",
        stable_id_of=lambda item: item.id,
        description_of=lambda item: f"{item.status}, {item.attempts} attempt(s)",
        title="Retrying/dead-lettered work items",
        empty_message="Nothing currently retrying or dead-lettered.",
        redraw_in_place=redraw_in_place,
        unicode_style=unicode_style,
        collapsed=collapsed,
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if selected is None:
        return

    await session.write_line(f"\r\nKind: {sanitize_text(selected.kind)}")
    await session.write_line(f"Target: {sanitize_text(selected.target_fingerprint)}")
    await session.write_line(f"Status: {selected.status}, {selected.attempts} attempt(s)")
    if selected.last_error:
        await session.write_line(f"Last error: {sanitize_text(selected.last_error)}")

    if selected.status == "dead_lettered":
        if await prompt_yes_no(session, "\r\nReplay this work item now?", default=False):
            def _replay(db: Database) -> WorkItem:
                replayed = replay_work_item(db, selected.id, replayed_by=actor)
                if replayed.kind == KIND_LINK_MAIL_DELIVERY:
                    unexpire_link_message_delivery(db, replayed.reference_id)
                return replayed

            replayed = await lane.run(_replay)
            await session.write_line(f"Replayed -- status is now {replayed.status!r}.")
    else:
        if await prompt_yes_no(session, "\r\nCancel this work item (stop retrying)?", default=False):
            def _cancel(db: Database) -> WorkItem:
                return cancel_work_item(db, selected.id, cancelled_by=actor)

            cancelled = await lane.run(_cancel)
            await session.write_line(f"Cancelled -- status is now {cancelled.status!r}.")


def _diagnostic_level_color(level: str) -> int:
    """WARNING reads as merely notable; ERROR/CRITICAL (the only other
    levels `LinkDiagnosticLogHandler` ever forwards -- it's attached at
    `WARNING` and above) read as more urgent, via the same `ALERT_COLOR`
    a live drain/shutdown countdown already uses for "something
    time-sensitive, act on it"."""
    return ALERT_COLOR if level in ("ERROR", "CRITICAL") else WARNING_COLOR


async def _diagnostic_log_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """
    Read-only SysOp inspection of the bounded Link diagnostic log
    (design doc §13.11, issue #60) -- `netbbs.link.diagnostics.
    LinkDiagnosticLogHandler` populates this from every existing
    `netbbs.link` `_logger.warning`/`.error` call (dial failures, sync
    failures, materialization refusals, and similar), pruned against
    operator-configured age/row bounds on every write. No action to
    take on an entry here, unlike `[O]utbox` -- purely "what has this
    node's own Link activity been complaining about lately."

    Issue #101: order is a simple per-visit toggle (asked once, up
    front, via the same `prompt_yes_no` convention used throughout this
    module for a binary choice, and only once there's actually something
    to reorder) rather than a live in-list hotkey -- `pick_item`'s own
    key dispatch (N/P/S/G/B/digits) has no room for a caller-defined
    extra command, and a full custom picker (the shape
    `_pick_target_user`'s multi-mode sort/visibility toggle needed) would
    be disproportionate for what is, here, a single boolean.
    """
    await session.write_line(
        colored("\r\nDiagnostic log:", fg_color=await lane.run(effective_header_color_256), bold=True)
    )
    entries = await lane.run(list_diagnostic_log_entries)
    if not entries:
        await session.write_line(colored("Nothing logged yet.", fg_color=MUTED_COLOR))
        return

    ascending = not await prompt_yes_no(session, "Show newest first?", default=True)
    order_label = "oldest first" if ascending else "most recent first"
    if ascending:
        entries = list(reversed(entries))
    selected = await pick_item(
        session, entries,
        name_of=lambda entry: f"{entry.created_at}  {entry.level}",
        stable_id_of=lambda entry: entry.id,
        description_of=lambda entry: sanitize_text(entry.message),
        title=f"Diagnostic log ({order_label})",
        empty_message="Nothing logged yet.",
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if selected is None:
        return

    level_color = _diagnostic_level_color(selected.level)
    await session.write_line(colored(f"\r\nWhen: {selected.created_at}", fg_color=MUTED_COLOR))
    await session.write_line(f"Level: {colored(selected.level, fg_color=level_color, bold=True)}")
    await session.write_line(colored(f"Logger: {sanitize_text(selected.logger_name)}", fg_color=MUTED_COLOR))
    await session.write_line(f"Message: {sanitize_text(selected.message)}")


async def _audit_log_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """
    Read-only, node-wide moderation/admin audit trail (dogfood follow-
    up: `list_actions_for_object`/`list_actions_for_target_user` only
    ever answer "what happened to this specific user/board/channel" --
    a SysOp investigating "did anything bad happen on this node
    recently" had no way to ask that without already knowing who or
    what to check). Mirrors `_diagnostic_log_screen`'s own shape
    (bounded list, newest-first toggle, pick an entry for its full
    detail) since both are "here's what's been logged lately" screens.
    """
    await session.write_line(
        colored("\r\nAudit log:", fg_color=await lane.run(effective_header_color_256), bold=True)
    )
    entries = await lane.run(list_recent_actions)
    if not entries:
        await session.write_line(colored("Nothing logged yet.", fg_color=MUTED_COLOR))
        return

    ascending = not await prompt_yes_no(session, "Show newest first?", default=True)
    order_label = "oldest first" if ascending else "most recent first"
    if ascending:
        entries = list(reversed(entries))

    def _resolve_names(db: Database) -> dict[int, str]:
        ids = {entry.actor_user_id for entry in entries if entry.actor_user_id is not None}
        ids |= {entry.target_user_id for entry in entries if entry.target_user_id is not None}
        names: dict[int, str] = {}
        for user_id in ids:
            user = get_user_by_id(db, user_id)
            names[user_id] = user.username if user is not None else "(deleted account)"
        return names

    usernames = await lane.run(_resolve_names)

    def _actor_name(entry) -> str:
        if entry.actor_user_id is None:
            return "(system)"
        return usernames.get(entry.actor_user_id, "(deleted account)")

    def _row_label(entry) -> str:
        return f"{entry.created_at}  {entry.action}  (by {_actor_name(entry)})"

    def _row_description(entry) -> str:
        parts = []
        if entry.object_type is not None:
            parts.append(f"{entry.object_type} #{entry.object_id}")
        if entry.target_user_id is not None:
            parts.append(f"target: {usernames.get(entry.target_user_id, '(deleted account)')}")
        if entry.detail:
            parts.append(sanitize_text(entry.detail))
        return "  ".join(parts) if parts else "(no detail)"

    selected = await pick_item(
        session, entries,
        name_of=_row_label,
        stable_id_of=lambda entry: entry.id,
        description_of=_row_description,
        title=f"Audit log ({order_label})",
        empty_message="Nothing logged yet.",
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if selected is None:
        return

    await session.write_line(colored(f"\r\nWhen: {selected.created_at}", fg_color=MUTED_COLOR))
    await session.write_line(f"Action: {sanitize_text(selected.action)}")
    await session.write_line(f"By: {sanitize_text(_actor_name(selected))}")
    if selected.object_type is not None:
        await session.write_line(f"Object: {sanitize_text(selected.object_type)} #{selected.object_id}")
    if selected.target_user_id is not None:
        target_name = usernames.get(selected.target_user_id, "(deleted account)")
        await session.write_line(f"Target: {sanitize_text(target_name)}")
    if selected.detail:
        await session.write_line(f"Detail: {sanitize_text(selected.detail)}")


# How often `_diagnostic_log_tail_screen` polls for new rows -- the log
# is DB-backed (LinkDiagnosticLogHandler's own connection), not an
# in-memory ring buffer, so "live" here really is short-interval
# polling, not a push subscription. 2s keeps the table read cheap
# (an indexed "id > ?" scan on an already row/age-bounded table) while
# still feeling immediate to a SysOp actively watching.
_DIAGNOSTIC_TAIL_POLL_INTERVAL_SECONDS = 2.0

# How many of the most recent entries to seed the view with on entry --
# enough recent context to be useful without dumping the entire
# (already up-to-200-row) log before anything new has even happened.
_DIAGNOSTIC_TAIL_SEED_COUNT = 20


def _diagnostic_entry_line(entry: DiagnosticLogEntry, width: int) -> str:
    level_color = _diagnostic_level_color(entry.level)
    segments: list[tuple[str, int | None]] = [
        (f"{entry.created_at}  ", MUTED_COLOR),
        (f"[{entry.level}] ", level_color),
        (f"{sanitize_text(entry.logger_name)}: ", MUTED_COLOR),
        (sanitize_text(entry.message), None),
    ]
    return colored_truncate(segments, width)


async def _diagnostic_log_tail_screen(session: Session, lane: DatabaseLane) -> None:
    """
    Issue #101b: a live "follow" view of the diagnostic log, appending
    new entries as they're written rather than requiring the SysOp to
    back out of `_diagnostic_log_screen` and reopen it to see what's new
    since.

    Races a single `read_key()` call (any key stops the tail) against a
    polling timer via `asyncio.wait` -- own async tasks (CLAUDE.md): the
    `finally` always cancels and gathers the read task, on every exit
    path, whether that's the SysOp actually pressing a key or an
    exception unwinding out of the poll loop, so a stray uncompleted
    `read_key()` never leaks past this function's return.
    """
    await session.write_line(
        colored(
            "\r\nDiagnostic log (live) -- press any key to stop.",
            fg_color=await lane.run(effective_header_color_256), bold=True,
        )
    )
    seed = await lane.run(list_diagnostic_log_entries, limit=_DIAGNOSTIC_TAIL_SEED_COUNT)
    last_id = 0
    for entry in reversed(seed):  # oldest of the seeded batch first, matching tail's own reading order
        await session.write_line(_diagnostic_entry_line(entry, session.terminal_width))
        last_id = max(last_id, entry.id)
    if not seed:
        await session.write_line(colored("Nothing logged yet -- watching for new entries.", fg_color=MUTED_COLOR))

    key_task = asyncio.create_task(session.read_key())
    try:
        while True:
            done, _pending = await asyncio.wait(
                {key_task}, timeout=_DIAGNOSTIC_TAIL_POLL_INTERVAL_SECONDS
            )
            if key_task in done:
                break
            new_entries = await lane.run(list_diagnostic_log_entries_since, last_id)
            for entry in new_entries:
                await session.write_line(_diagnostic_entry_line(entry, session.terminal_width))
                last_id = entry.id
    finally:
        if not key_task.done():
            key_task.cancel()
            await asyncio.gather(key_task, return_exceptions=True)
    await session.write_line("")


async def _repair_carried_posts_screen(session: Session, lane: DatabaseLane) -> None:
    """
    Design doc §9.3/issue #73's own "supported rebuild path" acceptance
    criterion, exposed the same way `_gc_screen` exposes reference-aware
    blob reclaim: an explicit, SysOp-triggered maintenance action, no
    background scheduler (this codebase's established convention).
    Unlike GC, this is purely additive -- it can only fill in a missing
    `posts` row from an already-accepted, already-verified signed event,
    never delete or rewrite anything -- so there's no dry-run/confirm
    step needed, just run it and report what happened.

    Only ever finds work for a node that carried boards *before* this
    feature shipped (`rebuild_carried_post_materialization`'s own
    docstring) -- persistence and projection are atomic for every new
    event going forward, so a freshly upgraded node reporting 0 here is
    the expected steady state, not a sign anything is broken.
    """
    rebuilt = await lane.run(rebuild_carried_post_materialization)
    if rebuilt == 0:
        await session.write_line(
            colored(
                "\r\nRepair carried posts: nothing to do -- every accepted board_post/"
                "board_post_edit already has a local posts row.",
                fg_color=MUTED_COLOR,
            )
        )
    else:
        await session.write_line(f"\r\nRepair carried posts: materialized {rebuilt} missing row(s).")


async def _prune_drafts_screen(session: Session, lane: DatabaseLane) -> None:
    """
    Bounds stale post/bio draft files (GitHub issue #158, split from
    #149): always shows a dry-run report first, then asks separately
    before actually deleting anything -- the same "preview, then
    explicit confirm" shape `_gc_screen` already uses, appropriate here
    too since this is a one-way filesystem operation the database
    itself can't undo.

    `new`-kind post drafts are already naturally bounded (one file per
    (user, board), always overwritten) and never need this; `edit`-kind
    post drafts and bio drafts are not (one file per abandoned edit/bio
    session, with nothing else to clean them up) -- see
    `netbbs.net.draft_storage.prune_stale_drafts`'s own docstring for
    why every draft is safe to prune the same way once stale, regardless
    of which caller wrote it.
    """
    preview = await lane.run(prune_stale_drafts, dry_run=True)
    await _write_draft_prune_report(session, preview)
    if preview.stale_files == 0:
        return
    if not await prompt_yes_no(session, "Delete these stale drafts now?", default=False):
        return
    result = await lane.run(prune_stale_drafts, dry_run=False)
    await _write_draft_prune_report(session, result)


async def _write_draft_prune_report(session: Session, report: DraftPruneReport) -> None:
    verb = "Would delete" if report.dry_run else "Deleted"
    await session.write_line(
        f"\r\n{verb} {report.stale_files} stale draft(s), {_format_bytes(report.stale_bytes)}."
    )
    if report.skipped_recent:
        await session.write_line(
            colored(
                f"{report.skipped_recent} draft(s) still within the retention window skipped "
                "this pass.",
                fg_color=MUTED_COLOR,
            )
        )
    for error in report.errors:
        await session.write_line(colored(f"Error: {error}", fg_color=MUTED_COLOR))


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


# -- node management (design doc) -------------------------------------------


async def _node_menu(session: Session, lane: DatabaseLane, actor: User, node_controls: NodeControls) -> None:
    # Fetched once, here, and threaded through as a plain parameter --
    # NOT re-queried inside `_draw_node_menu` on every redraw. An
    # earlier version of this rollout learned the hard way that an
    # extra `lane.run` await point on a screen like this one (drain/
    # shutdown scheduling) can perturb tests relying on precise async
    # cancellation timing.
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_node_menu(session, node_controls, description_level, redraw_in_place, unicode_style, collapsed, header_color)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "w":
            await session.write_line("")
            await _who_screen(session, lane, actor, node_controls)
            await _draw_node_menu(session, node_controls, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "s":
            await session.write_line("")
            await _shutdown_screen(session, lane, actor, node_controls)
            await _draw_node_menu(session, node_controls, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "m":
            await session.write_line("")
            await _maintenance_mode_screen(session, lane, actor, node_controls)
            await _draw_node_menu(session, node_controls, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "d":
            await session.write_line("")
            await _drain_screen(session, lane, actor, node_controls)
            await _draw_node_menu(session, node_controls, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "l":
            await session.write_line("")
            await _lock_and_drain_screen(session, lane, actor, node_controls)
            await _draw_node_menu(session, node_controls, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


def _shutdown_source_label(source: str | None) -> str:
    """Human-readable provenance for `SequenceScheduler.source()` (issue
    #108) -- `"sysop"` never reaches display (a SysOp-created shutdown's
    own status lines don't need to say so), so this only ever needs to
    spell out the two signal names."""
    return {"sigterm": "SIGTERM", "sigint": "SIGINT"}.get(source or "", source or "signal")


async def _draw_node_menu(
    session: Session, node_controls: NodeControls, description_level: str, redraw_in_place: bool,
    unicode_style: bool,
    collapsed: bool,
    header_color: int | tuple[int, int, int] = HEADER_COLOR,
) -> None:
    """
    Design doc -- node management, Thiesi's own dogfood-testing report:
    a SysOp who toggled `[M]aintenance mode` and then moved on to
    something else has no way to notice it's still on except this
    screen (and the live main-menu prompt's own tag, `netbbs.net.
    login_flow._draw_main_menu`) -- the actual incident that prompted
    this: a SysOp left it on and only found out when a user reported
    being unable to log in. Shown unconditionally here (this is already
    the node-management screen, not a place that needs restraint about
    operational detail) rather than only when something's active.
    """
    await session.write_line("\r\n" + screen_title("Node management",
            breadcrumb=(session.node_display_name,), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color, node_name_gradient=session.node_name_gradient))
    await session.write_line(
        _menu_row(
            [
                MenuEntry(label=menu_key("W", "ho"), brief="See who's currently connected"),
                MenuEntry(label=menu_key("M", "aintenance mode"), brief="Block new non-SysOp logins"),
                MenuEntry(label=menu_key("D", "rain"), brief="Disconnect non-SysOps soon"),
                MenuEntry(label=menu_key("L", "ock & drain"), brief="Maintenance mode, then drain"),
                MenuEntry(label=menu_key("S", "hutdown"), brief="Schedule a node shutdown"),
                MenuEntry(label=menu_key("B", "ack"), brief="Return to Operations"),
            ],
            description_level,
            width=session.terminal_width,
            height=session.terminal_height,
        )
    )
    status_lines = [
        f"Maintenance mode: {'ON' if node_controls.maintenance.is_lockdown_active() else 'off'}"
    ]
    if node_controls.drain_scheduler.is_scheduled():
        remaining = node_controls.drain_scheduler.remaining_seconds()
        status_lines.append(f"Drain scheduled -- disconnecting non-SysOps in {format_remaining_seconds(remaining)}")
    if node_controls.shutdown_scheduler.is_scheduled():
        remaining = node_controls.shutdown_scheduler.remaining_seconds()
        line = f"Shutdown scheduled -- going down in {format_remaining_seconds(remaining)}"
        if not node_controls.shutdown_scheduler.is_cancellable():
            line += f" (triggered by {_shutdown_source_label(node_controls.shutdown_scheduler.source())}, cannot be cancelled)"
        status_lines.append(line)
    await session.write_line(colored("  ".join(status_lines), fg_color=ALERT_COLOR, bold=True))
    await session.write("Choice: ")


def _session_name(entry: SessionSummary) -> str:
    if entry.username is not None:
        return entry.username
    return f"(unauthenticated) {entry.peer_address or 'unknown address'}"


def _session_description(entry: SessionSummary, display_format: str, display_timezone: str) -> str:
    when = format_for_display(entry.connected_at, override_format=display_format, override_timezone=display_timezone)
    return f"connected since {when}"


async def _who_screen(session: Session, lane: DatabaseLane, actor: User, node_controls: NodeControls) -> None:
    """
    Design doc -- node management, Thiesi's own dogfood-testing report:
    this screen's only action is disconnecting whoever gets selected --
    previously undocumented anywhere on screen, so a SysOp only found
    out by actually selecting someone. Said explicitly now, before the
    picker, rather than left implicit.

    The optional custom message (also Thiesi's own request) is delivered
    to the target's own session, via `ActiveSessionRegistry.notify_one`,
    *before* `disconnect_one` ends its connection -- so the about-to-be-
    disconnected user actually gets a chance to read it, not just a
    silently dropped connection.
    """
    entries = node_controls.session_registry.list_entries()
    # description_of runs synchronously inside pick_item, so
    # the display-preference lookup _session_description needs is
    # resolved once via the lane *before* the picker, same shape
    # established for format_for_display generally.
    display_format, display_timezone = await lane.run(resolve_display_preferences)
    await session.write_line(
        colored("\r\nSelect a session below to disconnect it.", fg_color=MUTED_COLOR)
    )
    selected = await pick_item(
        session, entries,
        name_of=_session_name,
        stable_id_of=lambda e: e.session_id,
        description_of=lambda e: _session_description(e, display_format, display_timezone),
        title="Active sessions",
        empty_message="No active sessions.",
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
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

    await session.write("Message to show them before disconnecting (optional): ")
    message_raw = (await session.read_line()).strip()
    message = message_raw or None

    target_user_id: int | None = None
    detail = f"peer address {selected.peer_address or 'unknown'}"
    if selected.username is not None:
        try:
            target_user_id = (await lane.run(get_user_by_username, selected.username)).id
        except AuthError:
            pass  # account no longer exists -- log by peer address only

    if message is not None:
        await node_controls.session_registry.notify_one(
            selected.session, colored(f"\r\n*** {sanitize_text(message)} ***", fg_color=ALERT_COLOR, bold=True)
        )

    disconnected = await node_controls.session_registry.disconnect_one(selected.session)
    if not disconnected:
        await session.write_line(colored("That session is already gone.", fg_color=ERROR_COLOR))
        return

    await lane.run(
        record_action, actor=actor, action="disconnect_session",
        target_user_id=target_user_id, detail=f"{detail}, message={message!r}",
    )
    await session.write_line(
        colored(f"{_session_name(selected)!r} disconnected.", fg_color=SUCCESS_COLOR)
    )


async def _shutdown_screen(session: Session, lane: DatabaseLane, actor: User, node_controls: NodeControls) -> None:
    """
    Design doc -- node management, Thiesi's own request: now behaves
    exactly like `[D]rain` rather than a differently-shaped sibling
    command -- an operator-chosen delay (prefilled from `config.
    shutdown.graceful_delay_seconds`, not a hard-coded mandate) instead
    of a fixed config value with no override, and the same "already
    scheduled? offer to cancel" check `_drain_screen` has, backed by the
    same `SequenceScheduler` mechanism.

    Issue #108: an externally triggered shutdown (SIGTERM/SIGINT,
    `node_controls.shutdown_scheduler.is_cancellable()` is `False`) gets
    a status-only message and returns immediately here -- no "Cancel
    it?" prompt, and critically no fall-through to the "schedule a new
    one" flow below either. Letting a SysOp schedule a *replacement*
    while one is already in flight would silently achieve the same
    authority-overriding effect `SequenceScheduler.cancel()` itself
    already refuses (`schedule()` unconditionally cancels-and-replaces
    whatever was there) -- so this function, not the scheduler, is
    responsible for never reaching its own `schedule()` call in that
    case. A SysOp-created shutdown remains fully cancellable/replaceable
    exactly as before.
    """
    if node_controls.shutdown_scheduler.is_scheduled():
        remaining = node_controls.shutdown_scheduler.remaining_seconds()
        if not node_controls.shutdown_scheduler.is_cancellable():
            source_label = _shutdown_source_label(node_controls.shutdown_scheduler.source())
            await session.write_line(
                colored(
                    f"\r\nA shutdown was triggered externally ({source_label}) and is already "
                    f"in progress -- going down in {format_remaining_seconds(remaining)}. It "
                    "cannot be cancelled or replaced from here.",
                    fg_color=ALERT_COLOR, bold=True,
                )
            )
            return
        await session.write_line(
            colored(
                f"\r\nA shutdown is already scheduled -- going down in "
                f"{format_remaining_seconds(remaining)}.",
                fg_color=ALERT_COLOR, bold=True,
            )
        )
        if await prompt_yes_no(session, "Cancel it?", default=False):
            node_controls.shutdown_scheduler.cancel()
            node_controls.maintenance.deactivate()
            await lane.run(record_action, actor=actor, action="cancel_shutdown")
            _logger.info("scheduled shutdown cancelled by %s", actor.username)
            await session.write_line("Scheduled shutdown cancelled.")
            return
        await session.write_line(
            colored("Continuing -- scheduling a new shutdown will replace it.", fg_color=MUTED_COLOR)
        )

    await session.write_line(
        colored(
            "\r\nThis locks out new logins and warns every connected session "
            "(including this one) immediately. Everyone is then disconnected -- "
            "either right away, or after a grace period, depending on your choice "
            "below. This cannot be undone once the grace period actually elapses.",
            fg_color=MUTED_COLOR,
        )
    )
    await session.write("Graceful (wait, then disconnect) or immediate? [G/i]: ")
    mode_answer = (await session.read_line()).strip().lower()
    graceful = mode_answer != "i"

    delay_seconds = node_controls.graceful_delay_seconds
    if graceful:
        await session.write(f"Delay in seconds before disconnecting [{int(node_controls.graceful_delay_seconds)}]: ")
        delay_raw = (await session.read_line()).strip()
        try:
            delay_seconds = float(delay_raw) if delay_raw else node_controls.graceful_delay_seconds
        except ValueError:
            await session.write_line("Not a number -- cancelled.")
            return
        if delay_seconds < 0:
            await session.write_line("Delay cannot be negative -- cancelled.")
            return

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
        detail=f"graceful={graceful}, delay_seconds={delay_seconds}, message={message!r}",
    )
    _logger.info(
        "shutdown scheduled by %s (graceful=%s, delay_seconds=%s)", actor.username, graceful, delay_seconds
    )
    task = asyncio.create_task(
        run_shutdown_sequence(
            graceful=graceful,
            session_registry=node_controls.session_registry,
            maintenance=node_controls.maintenance,
            delay_seconds=delay_seconds,
            shutdown_event=node_controls.shutdown_event,
            message=message,
        )
    )
    loop = asyncio.get_running_loop()
    node_controls.shutdown_scheduler.schedule(
        task, deadline=loop.time() + (delay_seconds if graceful else 0.0), message=message
    )
    await session.write_line("Shutdown sequence started.")


# -- maintenance mode and drain (design doc §13.8) --------------------------


async def _maintenance_mode_screen(session: Session, lane: DatabaseLane, actor: User, node_controls: NodeControls) -> None:
    """
    Toggles `node_controls.maintenance`'s lockdown flag -- while active,
    a non-SysOp account can no longer log in (`netbbs.net.login_flow.
    run_authenticated_session`'s own post-authentication check), but
    every already-connected session (SysOp or not) is left completely
    untouched. Reversible, unlike `[S]hutdown`'s one-way lockout --
    turning this back off is exactly the same action, run again.

    Deliberately does nothing to currently-connected non-SysOp sessions
    -- `[D]rain` is the separate, explicit action for that (design doc
    §13.8's own two-step workflow), not an implied side effect of
    toggling this on.
    """
    currently_on = node_controls.maintenance.is_lockdown_active()
    await session.write_line(
        colored("\r\nMaintenance mode:", fg_color=await lane.run(effective_header_color_256), bold=True)
    )
    await session.write_line(
        f"Currently: {'ON' if currently_on else 'off'} -- new non-SysOp logins are "
        f"{'blocked' if currently_on else 'allowed'}. Already-connected sessions are unaffected either way."
    )

    new_state = "off" if currently_on else "ON"
    if not await prompt_yes_no(session, f"\r\nTurn maintenance mode {new_state}?", default=False):
        return

    if currently_on:
        node_controls.maintenance.disable_lockdown()
    else:
        node_controls.maintenance.enable_lockdown()
    await lane.run(record_action, actor=actor, action="set_maintenance_mode", detail=f"enabled={not currently_on}")
    _logger.info("maintenance mode set to %s by %s", "ON" if not currently_on else "off", actor.username)
    await session.write_line(f"Maintenance mode is now {'ON' if not currently_on else 'off'}.")


async def _drain_screen(session: Session, lane: DatabaseLane, actor: User, node_controls: NodeControls) -> None:
    """
    Warns every currently-connected non-SysOp session, then disconnects
    them after an operator-chosen delay -- never touches SysOp sessions
    (including this one), and never shuts the node down or changes
    `[M]aintenance mode`'s own lockdown flag (design doc §13.8: the two
    are meant to be composed deliberately, not implied by each other).

    `node_controls.drain_scheduler` (design doc -- node management,
    Thiesi's own dogfood-testing report) is what fixes the stacking bug
    a plain `asyncio.create_task(run_drain_sequence(...))` call had: a
    SysOp running this command again while a drain is already scheduled
    is now explicitly offered a chance to cancel it first, rather than
    silently launching a second, uncoordinated countdown racing the
    first one -- see `SequenceScheduler`'s own docstring.
    """
    if node_controls.drain_scheduler.is_scheduled():
        remaining = node_controls.drain_scheduler.remaining_seconds()
        await session.write_line(
            colored(
                f"\r\nA drain is already scheduled -- non-SysOps will be disconnected in "
                f"{format_remaining_seconds(remaining)}.",
                fg_color=ALERT_COLOR, bold=True,
            )
        )
        if await prompt_yes_no(session, "Cancel it?", default=False):
            node_controls.drain_scheduler.cancel()
            await lane.run(record_action, actor=actor, action="cancel_drain")
            _logger.info("scheduled drain cancelled by %s", actor.username)
            await session.write_line("Scheduled drain cancelled.")
            return
        await session.write_line(
            colored("Continuing -- scheduling a new drain will replace it.", fg_color=MUTED_COLOR)
        )

    await session.write_line(
        colored(
            "\r\nThis warns every connected non-SysOp session, waits, then disconnects "
            "them. SysOp sessions (including this one) are never warned or "
            "disconnected by this. The node itself is not shut down, and new non-SysOp "
            "logins are not blocked either -- turn on [M]aintenance mode too if you want "
            "the node to actually stay empty afterward.",
            fg_color=MUTED_COLOR,
        )
    )
    await session.write("Delay in seconds before disconnecting [60]: ")
    delay_raw = (await session.read_line()).strip()
    try:
        delay_seconds = float(delay_raw) if delay_raw else 60.0
    except ValueError:
        await session.write_line("Not a number -- cancelled.")
        return
    if delay_seconds < 0:
        await session.write_line("Delay cannot be negative -- cancelled.")
        return

    await session.write("Custom broadcast message (leave blank for the default): ")
    message_raw = (await session.read_line()).strip()
    message = message_raw or None

    if not await prompt_yes_no(session, f"\r\nConfirm drain (disconnect non-SysOps after {int(delay_seconds)}s)?", default=False):
        await session.write_line("Cancelled.")
        return

    await lane.run(
        record_action, actor=actor, action="trigger_drain",
        detail=f"delay_seconds={delay_seconds}, message={message!r}",
    )
    _logger.info("drain scheduled by %s (delay_seconds=%s)", actor.username, delay_seconds)
    task = asyncio.create_task(
        run_drain_sequence(
            session_registry=node_controls.session_registry, delay_seconds=delay_seconds, message=message,
        )
    )
    loop = asyncio.get_running_loop()
    node_controls.drain_scheduler.schedule(task, deadline=loop.time() + delay_seconds, message=message)
    await session.write_line(f"Drain started -- non-SysOp sessions will be disconnected in {int(delay_seconds)}s.")


async def _lock_and_drain_screen(session: Session, lane: DatabaseLane, actor: User, node_controls: NodeControls) -> None:
    """
    Design doc §13.8, Thiesi's own dogfood-testing report: `[M]aintenance
    mode` and `[D]rain` are deliberately separate, composable commands
    (see both their own docstrings), but in practice a SysOp almost
    always wants both together -- lock out new non-SysOp logins *and*
    clear out whoever's already connected, in one action. This composes
    the two existing primitives; it adds no new mechanism of its own.

    **Issue #109: ownership, not just current state, decides what this
    screen reports and what a second press may undo.** The original
    version keyed everything off `maintenance.is_lockdown_active()`
    alone -- so a SysOp who'd already turned on plain `[M]aintenance
    mode` independently, then pressed `[L]ock & drain` intending to
    clear current non-SysOps, was told the composite was "already
    active" and got no drain at all; a second press could also silently
    claim ownership of (and later disable) lockdown/drain state this
    command never created. `lockdown_owned`/`drain_owned` below check
    not just whether each half is active, but whether *this* command is
    the one that put it there (`MaintenanceMode.lockdown_source()`,
    `SequenceScheduler.source()` -- both `"lock_and_drain"` only when set
    by this function). A known, accepted narrow gap: if this command's
    own lockdown stays on after its own drain finishes, and a *different*,
    independent drain then gets scheduled by plain `[D]rain` while that
    lockdown is still up, this screen's status line won't mention that
    unrelated drain -- composing arbitrary interleavings of every
    independent command was never this issue's own scope.
    """
    lockdown_owned = (
        node_controls.maintenance.is_lockdown_active()
        and node_controls.maintenance.lockdown_source() == "lock_and_drain"
    )
    drain_owned = (
        node_controls.drain_scheduler.is_scheduled()
        and node_controls.drain_scheduler.source() == "lock_and_drain"
    )

    if lockdown_owned:
        if drain_owned:
            remaining = node_controls.drain_scheduler.remaining_seconds()
            status = f"non-SysOps will be disconnected in {format_remaining_seconds(remaining)}"
        else:
            status = "the drain has already finished (or none was scheduled) -- new non-SysOp logins are still blocked"
        await session.write_line(
            colored(f"\r\nLock & drain is active -- {status}.", fg_color=ALERT_COLOR, bold=True)
        )
        if await prompt_yes_no(session, "Unlock and cancel the drain (if still running)?", default=False):
            if drain_owned:
                node_controls.drain_scheduler.cancel()
            node_controls.maintenance.disable_lockdown()
            await lane.run(record_action, actor=actor, action="cancel_lock_and_drain")
            _logger.info("lock & drain cancelled by %s", actor.username)
            await session.write_line("Lock & drain cancelled -- maintenance mode is off again.")
            return
        await session.write_line(colored("Leaving lock & drain active.", fg_color=MUTED_COLOR))
        return

    # Lockdown may still be on here -- just not because this command put
    # it there. Never silently claim, re-tag, or later offer to undo
    # state this command didn't create; only add the drain on top.
    lockdown_already_independent = node_controls.maintenance.is_lockdown_active()
    if lockdown_already_independent:
        await session.write_line(
            colored(
                "\r\nMaintenance mode is already on (enabled independently of lock & drain) "
                "-- this will only add a drain, leaving the existing lock untouched.",
                fg_color=MUTED_COLOR,
            )
        )

    if node_controls.drain_scheduler.is_scheduled():
        remaining = node_controls.drain_scheduler.remaining_seconds()
        await session.write_line(
            colored(
                f"\r\nA drain is already scheduled -- non-SysOps will be disconnected in "
                f"{format_remaining_seconds(remaining)}.",
                fg_color=ALERT_COLOR, bold=True,
            )
        )
        if await prompt_yes_no(session, "Cancel it?", default=False):
            node_controls.drain_scheduler.cancel()
            await lane.run(record_action, actor=actor, action="cancel_drain")
            _logger.info("scheduled drain cancelled by %s", actor.username)
            await session.write_line("Scheduled drain cancelled.")
            return
        await session.write_line(
            colored("Continuing -- scheduling a new drain will replace it.", fg_color=MUTED_COLOR)
        )

    await session.write_line(
        colored(
            "\r\nThis immediately locks out new non-SysOp logins, then warns every "
            "connected non-SysOp session, waits, and disconnects them. SysOp sessions "
            "(including this one) are never warned or disconnected. Maintenance mode "
            "stays on afterward until you run this again to unlock.",
            fg_color=MUTED_COLOR,
        )
    )
    await session.write("Delay in seconds before disconnecting [60]: ")
    delay_raw = (await session.read_line()).strip()
    try:
        delay_seconds = float(delay_raw) if delay_raw else 60.0
    except ValueError:
        await session.write_line("Not a number -- cancelled.")
        return
    if delay_seconds < 0:
        await session.write_line("Delay cannot be negative -- cancelled.")
        return

    await session.write("Custom broadcast message (leave blank for the default): ")
    message_raw = (await session.read_line()).strip()
    message = message_raw or None

    if not await prompt_yes_no(
        session, f"\r\nConfirm lock & drain (lock now, disconnect non-SysOps after {int(delay_seconds)}s)?", default=False
    ):
        await session.write_line("Cancelled.")
        return

    await lane.run(
        record_action, actor=actor, action="trigger_lock_and_drain",
        detail=(
            f"delay_seconds={delay_seconds}, message={message!r}, "
            f"lockdown_pre_existing={lockdown_already_independent}"
        ),
    )
    _logger.info("lock & drain triggered by %s (delay_seconds=%s)", actor.username, delay_seconds)
    if not lockdown_already_independent:
        node_controls.maintenance.enable_lockdown(source="lock_and_drain")
    task = asyncio.create_task(
        run_drain_sequence(
            session_registry=node_controls.session_registry, delay_seconds=delay_seconds, message=message,
        )
    )
    loop = asyncio.get_running_loop()
    node_controls.drain_scheduler.schedule(
        task, deadline=loop.time() + delay_seconds, message=message, source="lock_and_drain"
    )
    if lockdown_already_independent:
        await session.write_line(
            f"Drain started -- non-SysOp sessions will be disconnected in {int(delay_seconds)}s. "
            "The existing maintenance lock (enabled independently) was left as-is."
        )
    else:
        await session.write_line(
            f"Locked -- new non-SysOp logins are blocked, and non-SysOp sessions will be "
            f"disconnected in {int(delay_seconds)}s."
        )


# -- welcome banner (design doc -- part one of a three-part skinning
# initiative) ----------------------------------------------------------


async def _welcome_banner_menu(session: Session, lane: DatabaseLane, actor: User) -> None:
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    await _draw_welcome_banner_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "p":
            await session.write_line("")
            await _preview_welcome_banner_screen(session, lane, actor)
            await _draw_welcome_banner_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed)
        elif choice == "e":
            await session.write_line("")
            await _enable_welcome_banner_screen(session, lane, actor)
            await _draw_welcome_banner_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed)
        elif choice == "d":
            await session.write_line("")
            await _disable_welcome_banner_screen(session, lane, actor)
            await _draw_welcome_banner_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed)
        elif choice == "i":
            await session.write_line("")
            await _edit_welcome_banner_screen(session, lane, actor)
            await _draw_welcome_banner_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed)
        elif choice == "g":
            await session.write_line("")
            await _welcome_banner_gallery_screen(session, lane, actor, description_level, redraw_in_place, unicode_style, collapsed)
            await _draw_welcome_banner_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed)
        elif choice == "f":
            await session.write_line("")
            await _welcome_banner_filesystem_screen(session, lane, actor, description_level, redraw_in_place, unicode_style, collapsed)
            await _draw_welcome_banner_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_welcome_banner_menu(
    session: Session, lane: DatabaseLane, description_level: str, redraw_in_place: bool,
    unicode_style: bool,
    collapsed: bool,
) -> None:
    status = await lane.run(welcome_banner_status)
    state = "ENABLED" if status.enabled else "disabled"
    if status.exists:
        file_state = f"{status.size_bytes} bytes"
    else:
        file_state = "missing"
    state_color = SUCCESS_COLOR if status.enabled else MUTED_COLOR
    file_color = METADATA_COLOR if status.exists else ERROR_COLOR
    detail = (
        colored(state, fg_color=state_color, bold=status.enabled)
        + colored(" -- file: ", fg_color=LABEL_COLOR)
        + colored(str(status.path), fg_color=METADATA_COLOR)
        + colored(f" ({file_state})", fg_color=file_color)
    )
    await session.write_line("\r\n" + screen_title("Welcome banner",
            breadcrumb=(session.node_display_name,), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            header_color=await lane.run(effective_header_color_256), node_name_gradient=session.node_name_gradient))
    await session.write_line(detail)
    await session.write_line(
        "\r\n" + _menu_row(
            [
                MenuEntry(label=menu_key("P", "review"), brief="Show the banner as callers see it"),
                MenuEntry(label=menu_key("E", "nable"), brief="Turn the banner on"),
                MenuEntry(label=menu_key("D", "isable"), brief="Turn the banner off"),
                MenuEntry(label=menu_key("i", "t", prefix="Ed"), brief="Edit the banner text"),
                MenuEntry(label=menu_key("G", "allery"), brief="Apply a bundled sample banner"),
                MenuEntry(label=menu_key("F", "rom disk"), brief="Load your own .ans file from this node"),
                MenuEntry(label=menu_key("B", "ack"), brief="Return to Settings"),
            ],
            description_level,
            width=session.terminal_width,
            height=session.terminal_height,
        )
    )
    await session.write("Choice: ")


async def _preview_welcome_banner_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """Renders the exact banner `netbbs.net.login_flow` would show at
    login right now -- the same `load_welcome_banner` call, used as a
    smoke test of the loading path itself, not a separate rendering.

    Dogfood follow-up: this used to read `session.supports_truecolor`
    directly, ignoring the previewing SysOp's own `[C]olor depth`
    override (`netbbs.net.color_depth_preference`) -- the one screen
    that override's own help text ("force a terminal color depth")
    promised control over silently didn't honor it.
    `effective_truecolor` is the shared "override wins over negotiated"
    resolution every post-login truecolor decision should go through
    (its own docstring); this is that function's first real caller."""

    def _load(db: Database) -> tuple:
        truecolor = effective_truecolor(session, db, actor)
        return welcome_banner_status(db), load_welcome_banner(db, truecolor=truecolor), truecolor

    status, banner_text, truecolor = await lane.run(_load)
    await session.write_line(colored("\r\nPreviewing welcome banner as shown at login:", fg_color=MUTED_COLOR))
    await session.write_line(
        colored("Capability: ", fg_color=LABEL_COLOR)
        + colored(
            getattr(session, "truecolor_diagnostic", "capability report unavailable"),
            fg_color=METADATA_COLOR,
        )
    )
    await session.write_line(banner_text)
    if status.enabled and status.exists and (status.size_bytes or 0) <= MAX_BANNER_SIZE_BYTES:
        await session.write_line(
            colored(
                "(showing your custom file) -- generated truecolor/256-color showcase is intentionally bypassed",
                fg_color=MUTED_COLOR,
            )
        )
    else:
        depth = "truecolor gradient" if truecolor else "256-color fallback"
        await session.write_line(
            colored(
                f"(showing the DEFAULT banner -- rendering: {depth}; enabled={status.enabled}, file exists={status.exists})",
                fg_color=MUTED_COLOR,
            )
        )
    # Dogfood report: this screen used to fall straight through to the
    # menu's own immediate redraw, which -- with redraw_in_place on
    # (the default for new accounts, issue #160's own follow-up)
    # cleared the just-printed preview off the screen before it could
    # actually be read, sometimes in well under a second. Same
    # present-then-wait shape `netbbs.net.help_overlay.show_help`
    # already uses for the identical reason.
    await session.write_line(colored("Press any key to continue...", fg_color=MUTED_COLOR))
    await session.read_key()


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
    """Opens the WYSIWYG ANSI art editor (design doc) against the
    current banner file, if any. `edit_ansi_art`
    itself knows nothing about "welcome banner" -- this screen is
    responsible for loading the existing file, computing the draft
    path, and writing a real save back to `banner_path(db)`."""
    path = await lane.run(banner_path)
    initial_bytes = path.read_bytes() if path.exists() else None
    draft_path = path.parent / f"{path.name}.draft"

    result = await edit_ansi_art(
        session, initial_bytes=initial_bytes, draft_path=draft_path,
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
    )
    if result is None:
        await session.write_line(colored("\r\nNo changes saved.", fg_color=MUTED_COLOR))
        return

    path.write_bytes(result)
    await lane.run(record_action, actor=actor, action="edit_welcome_banner", detail=str(path))
    await session.write_line(f"\r\nSaved {path}. Use [P]review to verify it looks right.")


async def _welcome_banner_gallery_screen(
    session: Session, lane: DatabaseLane, actor: User, description_level: str,
    redraw_in_place: bool, unicode_style: bool, collapsed: bool,
) -> None:
    """Issue #169: apply one of NetBBS's own bundled sample banners with
    zero filesystem access to the node -- the samples are real installed
    package data (`netbbs.net.banner_presets`), not files a SysOp has to
    `cp` into place first (the gap this closes: a wheel install has no
    `examples/` directory to copy from at all).

    Dogfood follow-up: declining the apply prompt loops back into this
    same gallery picker instead of exiting to the welcome-banner menu --
    browsing several samples in a row used to mean re-entering
    `[G]allery` after every single one just to look at the next.

    Second dogfood follow-up: that loop-back used to always reopen the
    picker at page 1 with nothing highlighted, discarding exactly the
    browsing position a decline is supposed to return you to -- `pick_
    item`'s `start_stable_id` now re-highlights whichever sample was
    just declined."""
    last_stable_id: int | None = None
    while True:
        selection = await pick_item(
            session,
            list(enumerate(WELCOME_BANNER_PRESETS, start=1)),
            name_of=lambda pair: pair[1].name,
            stable_id_of=lambda pair: pair[0],
            description_of=lambda pair: f"{pair[1].depth} -- {pair[1].description}",
            title="Welcome banner gallery",
            empty_message="No bundled welcome-banner samples.",
            description_level=description_level,
            redraw_in_place=redraw_in_place,
            unicode_style=unicode_style,
            collapsed=collapsed,
            start_stable_id=last_stable_id,
        )
        if selection is None:
            return
        last_stable_id = selection[0]
        preset = selection[1]
        data = load_welcome_banner_preset(preset)
        await session.write_line(colored(f"\r\nPreviewing {preset.name!r}:", fg_color=MUTED_COLOR))
        await session.write_line(decode_ansi_bytes(data) + RESET)

        if not await prompt_yes_no(session, f"\r\nApply {preset.name!r} as the welcome banner now?", default=False):
            await session.write_line(colored("Not applied.", fg_color=MUTED_COLOR))
            continue

        def _apply(db: Database) -> Path:
            path = banner_path(db)
            path.write_bytes(data)
            set_welcome_banner_enabled(db, True)
            record_action(db, actor=actor, action="apply_welcome_banner_preset", detail=f"{preset.key} -> {path}")
            return path

        path = await lane.run(_apply)
        await session.write_line(f"Applied and enabled. Saved to {path}. Use [P]review to verify it looks right.")
        return


def _browsable_ans_files(directory: Path, *, exclude: Path) -> list[Path]:
    """Every `.ans` file in `directory` except `exclude` (the picker's
    own current target -- loading it onto itself is a pointless option,
    not a real choice). No traversal risk here: `directory.glob` only
    ever returns real entries NetBBS itself enumerated, never a
    caller-typed path, so there's nothing for a `..`-style escape to
    reach in the first place (issue #170's own locked design)."""
    return sorted(p for p in directory.glob("*.ans") if p.is_file() and p != exclude)


async def _welcome_banner_filesystem_screen(
    session: Session, lane: DatabaseLane, actor: User, description_level: str,
    redraw_in_place: bool, unicode_style: bool, collapsed: bool,
) -> None:
    """Issue #170: load a SysOp's own externally-authored `.ans` file --
    drawn in a dedicated tool, or downloaded from elsewhere -- instead
    of having to shell in and place it at `banner_path(db)` by hand.
    Different problem from the bundled gallery just above (issue #169,
    sibling): nothing here was ever going to ship *with* NetBBS, so this
    enumerates real files on disk instead of a fixed preset tuple.

    **Browsable root** (locked design, see the issue's own comments):
    the node's own state directory only -- exactly `banner_path(db)`'s
    own parent, the same directory NetBBS already reads/writes banner
    state in. No new `node_config` setting, no new attack surface
    beyond what already exists.

    **Load semantics**: copy, not reference-in-place. The selected
    file's bytes are written to the existing well-known `banner_path
    (db)`, exactly as if the SysOp had `cp`'d it there by hand --
    `load_welcome_banner`/`welcome_banner_status`/backup-restore/the
    WYSIWYG editor all keep their existing one-file-one-path assumption
    completely untouched.

    **Validation timing**: the same size cap `[E]nable` already enforces
    runs *before* the copy, against the source file -- rejecting a pick
    immediately with a clear reason, not a weaker path into the same
    target than manual placement already has."""

    def _list(db: Database) -> tuple[list[Path], Path]:
        directory = banner_path(db).parent
        return _browsable_ans_files(directory, exclude=banner_path(db)), directory

    files, directory = await lane.run(_list)
    if not files:
        await session.write_line(colored(
            f"\r\nNo other .ans files found in {directory}. Place one there "
            f"(e.g. via SFTP/SCP), then browse again.", fg_color=MUTED_COLOR,
        ))
        return

    last_stable_id: int | None = None
    while True:
        selection = await pick_item(
            session,
            list(enumerate(files, start=1)),
            name_of=lambda pair: pair[1].name,
            stable_id_of=lambda pair: pair[0],
            title="Welcome banner -- load from disk",
            empty_message="No other .ans files found.",
            description_level=description_level,
            redraw_in_place=redraw_in_place,
            unicode_style=unicode_style,
            collapsed=collapsed,
            start_stable_id=last_stable_id,
        )
        if selection is None:
            return
        last_stable_id = selection[0]
        path = selection[1]

        size = path.stat().st_size
        if size > MAX_BANNER_SIZE_BYTES:
            await session.write_line(colored(
                f"{path.name} is {size} bytes, over the {MAX_BANNER_SIZE_BYTES} byte "
                f"limit -- not loading.", fg_color=MUTED_COLOR,
            ))
            continue

        data = path.read_bytes()
        await session.write_line(colored(f"\r\nPreviewing {path.name!r}:", fg_color=MUTED_COLOR))
        await session.write_line(decode_ansi_bytes(data) + RESET)

        if not await prompt_yes_no(session, f"\r\nLoad {path.name!r} as the welcome banner now?", default=False):
            await session.write_line(colored("Not loaded.", fg_color=MUTED_COLOR))
            continue

        def _apply(db: Database) -> Path:
            target = banner_path(db)
            target.write_bytes(data)
            set_welcome_banner_enabled(db, True)
            record_action(db, actor=actor, action="load_welcome_banner_from_file", detail=f"{path} -> {target}")
            return target

        target = await lane.run(_apply)
        await session.write_line(f"Loaded and enabled. Saved to {target}. Use [P]review to verify it looks right.")
        return


# -- main-menu masthead (issue #161 -- part two of the three-part
# skinning initiative the welcome banner above calls itself part one
# of) ---------------------------------------------------------------


async def _main_menu_banner_menu(session: Session, lane: DatabaseLane, actor: User) -> None:
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    await _draw_main_menu_banner_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "p":
            await session.write_line("")
            await _preview_main_menu_banner_screen(session, lane, actor)
            await _draw_main_menu_banner_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed)
        elif choice == "e":
            await session.write_line("")
            await _enable_main_menu_banner_screen(session, lane, actor)
            await _draw_main_menu_banner_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed)
        elif choice == "d":
            await session.write_line("")
            await _disable_main_menu_banner_screen(session, lane, actor)
            await _draw_main_menu_banner_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed)
        elif choice == "i":
            await session.write_line("")
            await _edit_main_menu_banner_screen(session, lane, actor)
            await _draw_main_menu_banner_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed)
        elif choice == "g":
            await session.write_line("")
            await _main_menu_banner_gallery_screen(session, lane, actor, description_level, redraw_in_place, unicode_style, collapsed)
            await _draw_main_menu_banner_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed)
        elif choice == "f":
            await session.write_line("")
            await _main_menu_banner_filesystem_screen(session, lane, actor, description_level, redraw_in_place, unicode_style, collapsed)
            await _draw_main_menu_banner_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_main_menu_banner_menu(
    session: Session, lane: DatabaseLane, description_level: str, redraw_in_place: bool,
    unicode_style: bool,
    collapsed: bool,
) -> None:
    status = await lane.run(main_menu_banner_status)
    state = "ENABLED" if status.enabled else "disabled"
    if status.exists:
        file_state = f"{status.size_bytes} bytes"
    else:
        file_state = "missing"
    state_color = SUCCESS_COLOR if status.enabled else MUTED_COLOR
    file_color = METADATA_COLOR if status.exists else ERROR_COLOR
    detail = (
        colored(state, fg_color=state_color, bold=status.enabled)
        + colored(" -- file: ", fg_color=LABEL_COLOR)
        + colored(str(status.path), fg_color=METADATA_COLOR)
        + colored(f" ({file_state})", fg_color=file_color)
    )
    await session.write_line("\r\n" + screen_title("Main-menu masthead",
            breadcrumb=(session.node_display_name, "System"), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            header_color=await lane.run(effective_header_color_256), node_name_gradient=session.node_name_gradient))
    await session.write_line(detail)
    await session.write_line(
        "\r\n" + colored(
            "Shown above the main menu, which stays fully live/dynamic underneath "
            "it -- disabled by default, no effect on any existing node.",
            fg_color=MUTED_COLOR,
        )
    )
    await session.write_line(
        "\r\n" + _menu_row(
            [
                MenuEntry(label=menu_key("P", "review"), brief="Show the masthead as callers see it"),
                MenuEntry(label=menu_key("E", "nable"), brief="Turn the masthead on"),
                MenuEntry(label=menu_key("D", "isable"), brief="Turn the masthead off"),
                MenuEntry(label=menu_key("i", "t", prefix="Ed"), brief="Edit the masthead art"),
                MenuEntry(label=menu_key("G", "allery"), brief="Apply a bundled sample masthead"),
                MenuEntry(label=menu_key("F", "rom disk"), brief="Load your own .ans file from this node"),
                MenuEntry(label=menu_key("B", "ack"), brief="Return to Settings"),
            ],
            description_level,
            width=session.terminal_width,
            height=session.terminal_height,
        )
    )
    await session.write("Choice: ")


async def _preview_main_menu_banner_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """Renders the exact masthead `netbbs.net.login_flow._draw_main_menu`
    would prepend above the main menu right now -- the same
    `load_main_menu_banner` call, a smoke test of the loading path
    itself. Unlike the welcome-banner preview, this never calls into
    `netbbs.net.login_flow` to reproduce the real menu underneath it --
    that module already imports `admin_menu` from this one, so the
    reverse import would be circular, and the masthead itself carries no
    truecolor-dependent content to showcase (shown exactly as authored,
    the same "trusted, already-composed art" tier as the welcome
    banner's own custom-file path)."""

    def _load(db: Database) -> tuple:
        return main_menu_banner_status(db), load_main_menu_banner(db)

    status, masthead = await lane.run(_load)
    await session.write_line(colored("\r\nPreviewing the masthead as shown above the main menu:", fg_color=MUTED_COLOR))
    if not masthead:
        await session.write_line(
            colored(
                f"(no masthead would be shown -- enabled={status.enabled}, file exists={status.exists})",
                fg_color=MUTED_COLOR,
            )
        )
    else:
        await session.write_line(masthead)
        await session.write_line(
            colored("(the main menu itself renders live, unchanged, immediately below this)", fg_color=MUTED_COLOR)
        )
    # See _preview_welcome_banner_screen's identical fix for why this
    # wait exists -- without it, redraw_in_place clears this preview
    # before it can actually be read.
    await session.write_line(colored("Press any key to continue...", fg_color=MUTED_COLOR))
    await session.read_key()


async def _enable_main_menu_banner_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    status = await lane.run(main_menu_banner_status)
    if not status.exists:
        await session.write_line(
            colored(
                f"No masthead file found at {status.path}. Place a .ans file there first, then enable.",
                fg_color=MUTED_COLOR,
            )
        )
        return
    if (status.size_bytes or 0) > MAX_MASTHEAD_SIZE_BYTES:
        await session.write_line(
            colored(
                f"Masthead file at {status.path} is {status.size_bytes} bytes, over the "
                f"{MAX_MASTHEAD_SIZE_BYTES} byte limit -- not enabling.",
                fg_color=MUTED_COLOR,
            )
        )
        return

    def _apply(db: Database) -> None:
        set_main_menu_banner_enabled(db, True)
        record_action(db, actor=actor, action="enable_main_menu_banner", detail=str(status.path))

    await lane.run(_apply)
    await session.write_line("Main-menu masthead enabled. Use [P]review to verify it looks right.")


async def _disable_main_menu_banner_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    def _apply(db: Database):
        status = main_menu_banner_status(db)
        set_main_menu_banner_enabled(db, False)
        record_action(db, actor=actor, action="disable_main_menu_banner", detail=str(status.path))
        return status

    status = await lane.run(_apply)
    await session.write_line(
        f"Masthead disabled -- the main menu reverts to showing no banner above it. "
        f"Your file at {status.path} was left in place."
    )


async def _edit_main_menu_banner_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """Same WYSIWYG ANSI art editor as `_edit_welcome_banner_screen`,
    against `main_menu_banner_path` instead."""
    path = await lane.run(main_menu_banner_path)
    initial_bytes = path.read_bytes() if path.exists() else None
    draft_path = path.parent / f"{path.name}.draft"

    result = await edit_ansi_art(
        session, initial_bytes=initial_bytes, draft_path=draft_path,
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
    )
    if result is None:
        await session.write_line(colored("\r\nNo changes saved.", fg_color=MUTED_COLOR))
        return

    path.write_bytes(result)
    await lane.run(record_action, actor=actor, action="edit_main_menu_banner", detail=str(path))
    await session.write_line(f"\r\nSaved {path}. Use [P]review to verify it looks right.")


async def _main_menu_banner_gallery_screen(
    session: Session, lane: DatabaseLane, actor: User, description_level: str,
    redraw_in_place: bool, unicode_style: bool, collapsed: bool,
) -> None:
    """Same bundled-sample gallery as `_welcome_banner_gallery_screen`
    (issue #169), against `main_menu_banner_path` instead -- including
    its identical "decline loops back into the gallery, re-highlighted"
    dogfood fixes."""
    last_stable_id: int | None = None
    while True:
        selection = await pick_item(
            session,
            list(enumerate(MAIN_MENU_BANNER_PRESETS, start=1)),
            name_of=lambda pair: pair[1].name,
            stable_id_of=lambda pair: pair[0],
            description_of=lambda pair: f"{pair[1].depth} -- {pair[1].description}",
            title="Masthead gallery",
            empty_message="No bundled masthead samples.",
            description_level=description_level,
            redraw_in_place=redraw_in_place,
            unicode_style=unicode_style,
            collapsed=collapsed,
            start_stable_id=last_stable_id,
        )
        if selection is None:
            return
        last_stable_id = selection[0]
        preset = selection[1]
        data = load_main_menu_banner_preset(preset)
        await session.write_line(colored(f"\r\nPreviewing {preset.name!r}:", fg_color=MUTED_COLOR))
        await session.write_line(decode_ansi_bytes(data) + RESET)

        if not await prompt_yes_no(session, f"\r\nApply {preset.name!r} as the masthead now?", default=False):
            await session.write_line(colored("Not applied.", fg_color=MUTED_COLOR))
            continue

        def _apply(db: Database) -> Path:
            path = main_menu_banner_path(db)
            path.write_bytes(data)
            set_main_menu_banner_enabled(db, True)
            record_action(db, actor=actor, action="apply_main_menu_banner_preset", detail=f"{preset.key} -> {path}")
            return path

        path = await lane.run(_apply)
        await session.write_line(f"Applied and enabled. Saved to {path}. Use [P]review to verify it looks right.")
        return


async def _main_menu_banner_filesystem_screen(
    session: Session, lane: DatabaseLane, actor: User, description_level: str,
    redraw_in_place: bool, unicode_style: bool, collapsed: bool,
) -> None:
    """Same load-from-disk screen as `_welcome_banner_filesystem_screen`
    (issue #170), against `main_menu_banner_path` instead -- see that
    function's own docstring for the locked design (browsable root,
    copy-not-reference load semantics, validate-before-copy)."""

    def _list(db: Database) -> tuple[list[Path], Path]:
        directory = main_menu_banner_path(db).parent
        return _browsable_ans_files(directory, exclude=main_menu_banner_path(db)), directory

    files, directory = await lane.run(_list)
    if not files:
        await session.write_line(colored(
            f"\r\nNo other .ans files found in {directory}. Place one there "
            f"(e.g. via SFTP/SCP), then browse again.", fg_color=MUTED_COLOR,
        ))
        return

    last_stable_id: int | None = None
    while True:
        selection = await pick_item(
            session,
            list(enumerate(files, start=1)),
            name_of=lambda pair: pair[1].name,
            stable_id_of=lambda pair: pair[0],
            title="Masthead -- load from disk",
            empty_message="No other .ans files found.",
            description_level=description_level,
            redraw_in_place=redraw_in_place,
            unicode_style=unicode_style,
            collapsed=collapsed,
            start_stable_id=last_stable_id,
        )
        if selection is None:
            return
        last_stable_id = selection[0]
        path = selection[1]

        size = path.stat().st_size
        if size > MAX_MASTHEAD_SIZE_BYTES:
            await session.write_line(colored(
                f"{path.name} is {size} bytes, over the {MAX_MASTHEAD_SIZE_BYTES} byte "
                f"limit -- not loading.", fg_color=MUTED_COLOR,
            ))
            continue

        data = path.read_bytes()
        await session.write_line(colored(f"\r\nPreviewing {path.name!r}:", fg_color=MUTED_COLOR))
        await session.write_line(decode_ansi_bytes(data) + RESET)

        if not await prompt_yes_no(session, f"\r\nLoad {path.name!r} as the masthead now?", default=False):
            await session.write_line(colored("Not loaded.", fg_color=MUTED_COLOR))
            continue

        def _apply(db: Database) -> Path:
            target = main_menu_banner_path(db)
            target.write_bytes(data)
            set_main_menu_banner_enabled(db, True)
            record_action(db, actor=actor, action="load_main_menu_banner_from_file", detail=f"{path} -> {target}")
            return target

        target = await lane.run(_apply)
        await session.write_line(f"Loaded and enabled. Saved to {target}. Use [P]review to verify it looks right.")
        return


# -- session banners (issue #177) ---------------------------------------
#
# Three more optional SysOp-authored banners, each its own independent
# singleton exactly like the welcome banner/main-menu masthead above
# (colocated .ans file, node_config enabled flag, size cap, silent
# fallback) -- but grouped under one new "Session banners" Settings entry
# rather than three more flat top-level rows, since there was no good
# non-colliding mnemonic left for three more entries in an already-dense
# Settings menu. No [G]allery/[F]rom disk here, unlike the welcome
# banner/masthead above -- those depend on a curated preset library
# (issue #169's bundled samples) that doesn't exist for these three yet;
# preview/enable/disable/edit alone already covers issue #177's own
# acceptance criteria. Where these ultimately live in the admin UI is
# expected to change again once issue #178's own broader reorg (one
# "Banners & Mastheads" entry covering every banner/masthead, existing
# and new) lands -- this is a reasonable interim grouping, not a
# commitment to keep this exact shape.


async def _session_banners_menu(session: Session, lane: DatabaseLane, actor: User) -> None:
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_session_banners_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "l":
            await session.write_line("")
            await _logoff_banner_menu(session, lane, actor)
            await _draw_session_banners_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "e":
            await session.write_line("")
            await _new_account_banner_before_menu(session, lane, actor)
            await _draw_session_banners_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "f":
            await session.write_line("")
            await _new_account_banner_after_menu(session, lane, actor)
            await _draw_session_banners_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_session_banners_menu(
    session: Session, lane: DatabaseLane, description_level: str, redraw_in_place: bool,
    unicode_style: bool, collapsed: bool, header_color: int | tuple[int, int, int],
) -> None:
    await session.write_line("\r\n" + screen_title("Session banners",
            breadcrumb=(session.node_display_name,), width=session.terminal_width, clear=redraw_in_place,
            unicode_style=unicode_style, collapsed=collapsed, header_color=header_color,
            node_name_gradient=session.node_name_gradient))
    await session.write_line(
        colored(
            "Optional banners shown at specific points in a caller's session -- signing off, "
            "and starting/finishing self-service signup.",
            fg_color=MUTED_COLOR,
        )
    )
    await session.write_line(
        "\r\n" + _menu_row(
            [
                MenuEntry(label=menu_key("L", "ogoff banner"), brief="Shown on an intentional Log off"),
                MenuEntry(label=menu_key("e", "fore signup", prefix="B"), brief="Shown once, before Create account"),
                MenuEntry(label=menu_key("f", "ter signup", prefix="A"), brief="Shown once signup succeeds"),
                MenuEntry(label=menu_key("B", "ack"), brief="Return to Settings"),
            ],
            description_level,
            width=session.terminal_width,
            height=session.terminal_height,
        )
    )
    await session.write("Choice: ")


# -- logoff banner --------------------------------------------------------


async def _logoff_banner_menu(session: Session, lane: DatabaseLane, actor: User) -> None:
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_logoff_banner_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "p":
            await session.write_line("")
            await _preview_logoff_banner_screen(session, lane)
            await _draw_logoff_banner_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "e":
            await session.write_line("")
            await _enable_logoff_banner_screen(session, lane, actor)
            await _draw_logoff_banner_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "d":
            await session.write_line("")
            await _disable_logoff_banner_screen(session, lane, actor)
            await _draw_logoff_banner_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "i":
            await session.write_line("")
            await _edit_logoff_banner_screen(session, lane, actor)
            await _draw_logoff_banner_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_logoff_banner_menu(
    session: Session, lane: DatabaseLane, description_level: str, redraw_in_place: bool,
    unicode_style: bool, collapsed: bool, header_color: int | tuple[int, int, int],
) -> None:
    status = await lane.run(logoff_banner_status)
    state = "ENABLED" if status.enabled else "disabled"
    file_state = f"{status.size_bytes} bytes" if status.exists else "missing"
    state_color = SUCCESS_COLOR if status.enabled else MUTED_COLOR
    file_color = METADATA_COLOR if status.exists else ERROR_COLOR
    detail = (
        colored(state, fg_color=state_color, bold=status.enabled)
        + colored(" -- file: ", fg_color=LABEL_COLOR)
        + colored(str(status.path), fg_color=METADATA_COLOR)
        + colored(f" ({file_state})", fg_color=file_color)
    )
    await session.write_line("\r\n" + screen_title("Logoff banner",
            breadcrumb=(session.node_display_name, "System", "Session banners"), width=session.terminal_width,
            clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color,
            node_name_gradient=session.node_name_gradient))
    await session.write_line(
        colored("Shown above the ordinary Goodbye message on an intentional Log off only -- never on an idle "
                "timeout, kick, or account revocation.", fg_color=MUTED_COLOR)
    )
    await session.write_line(detail)
    await session.write_line(
        "\r\n" + _menu_row(
            [
                MenuEntry(label=menu_key("P", "review"), brief="Show the banner as callers see it"),
                MenuEntry(label=menu_key("E", "nable"), brief="Turn the banner on"),
                MenuEntry(label=menu_key("D", "isable"), brief="Turn the banner off"),
                MenuEntry(label=menu_key("i", "t", prefix="Ed"), brief="Edit the banner text"),
                MenuEntry(label=menu_key("B", "ack"), brief="Return to Session banners"),
            ],
            description_level,
            width=session.terminal_width,
            height=session.terminal_height,
        )
    )
    await session.write("Choice: ")


async def _preview_logoff_banner_screen(session: Session, lane: DatabaseLane) -> None:
    status, banner_text = await lane.run(lambda db: (logoff_banner_status(db), load_logoff_banner(db)))
    await session.write_line(colored("\r\nPreviewing logoff banner as shown on Log off:", fg_color=MUTED_COLOR))
    if banner_text:
        await session.write_line(banner_text)
    else:
        await session.write_line(
            colored(
                f"(no banner -- enabled={status.enabled}, file exists={status.exists})", fg_color=MUTED_COLOR
            )
        )
    await session.write_line(colored("Press any key to continue...", fg_color=MUTED_COLOR))
    await session.read_key()


async def _enable_logoff_banner_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    status = await lane.run(logoff_banner_status)
    if not status.exists:
        await session.write_line(
            colored(
                f"No banner file found at {status.path}. Place a .ans file there first, then enable.",
                fg_color=MUTED_COLOR,
            )
        )
        return
    if (status.size_bytes or 0) > MAX_LOGOFF_BANNER_SIZE_BYTES:
        await session.write_line(
            colored(
                f"Banner file at {status.path} is {status.size_bytes} bytes, over the "
                f"{MAX_LOGOFF_BANNER_SIZE_BYTES} byte limit -- not enabling.",
                fg_color=MUTED_COLOR,
            )
        )
        return

    def _apply(db: Database) -> None:
        set_logoff_banner_enabled(db, True)
        record_action(db, actor=actor, action="enable_logoff_banner", detail=str(status.path))

    await lane.run(_apply)
    await session.write_line("Logoff banner enabled. Use [P]review to verify it looks right.")


async def _disable_logoff_banner_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    def _apply(db: Database):
        status = logoff_banner_status(db)
        set_logoff_banner_enabled(db, False)
        record_action(db, actor=actor, action="disable_logoff_banner", detail=str(status.path))
        return status

    status = await lane.run(_apply)
    await session.write_line(f"Logoff banner disabled. Your file at {status.path} was left in place.")


async def _edit_logoff_banner_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    path = await lane.run(logoff_banner_path)
    initial_bytes = path.read_bytes() if path.exists() else None
    draft_path = path.parent / f"{path.name}.draft"

    result = await edit_ansi_art(
        session, initial_bytes=initial_bytes, draft_path=draft_path,
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
    )
    if result is None:
        await session.write_line(colored("\r\nNo changes saved.", fg_color=MUTED_COLOR))
        return

    path.write_bytes(result)
    await lane.run(record_action, actor=actor, action="edit_logoff_banner", detail=str(path))
    await session.write_line(f"\r\nSaved {path}. Use [P]review to verify it looks right.")


# -- new-account banner (before signup) ------------------------------------


async def _new_account_banner_before_menu(session: Session, lane: DatabaseLane, actor: User) -> None:
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_new_account_banner_before_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "p":
            await session.write_line("")
            await _preview_new_account_banner_before_screen(session, lane)
            await _draw_new_account_banner_before_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "e":
            await session.write_line("")
            await _enable_new_account_banner_before_screen(session, lane, actor)
            await _draw_new_account_banner_before_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "d":
            await session.write_line("")
            await _disable_new_account_banner_before_screen(session, lane, actor)
            await _draw_new_account_banner_before_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "i":
            await session.write_line("")
            await _edit_new_account_banner_before_screen(session, lane, actor)
            await _draw_new_account_banner_before_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_new_account_banner_before_menu(
    session: Session, lane: DatabaseLane, description_level: str, redraw_in_place: bool,
    unicode_style: bool, collapsed: bool, header_color: int | tuple[int, int, int],
) -> None:
    status = await lane.run(new_account_banner_before_status)
    state = "ENABLED" if status.enabled else "disabled"
    file_state = f"{status.size_bytes} bytes" if status.exists else "missing"
    state_color = SUCCESS_COLOR if status.enabled else MUTED_COLOR
    file_color = METADATA_COLOR if status.exists else ERROR_COLOR
    detail = (
        colored(state, fg_color=state_color, bold=status.enabled)
        + colored(" -- file: ", fg_color=LABEL_COLOR)
        + colored(str(status.path), fg_color=METADATA_COLOR)
        + colored(f" ({file_state})", fg_color=file_color)
    )
    await session.write_line("\r\n" + screen_title("New account banner (before)",
            breadcrumb=(session.node_display_name, "System", "Session banners"), width=session.terminal_width,
            clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color,
            node_name_gradient=session.node_name_gradient))
    await session.write_line(
        colored(
            "Shown once, right when a caller starts self-service signup -- before the Create "
            "account prompts, never repeated on a fixable retry.",
            fg_color=MUTED_COLOR,
        )
    )
    await session.write_line(detail)
    await session.write_line(
        "\r\n" + _menu_row(
            [
                MenuEntry(label=menu_key("P", "review"), brief="Show the banner as callers see it"),
                MenuEntry(label=menu_key("E", "nable"), brief="Turn the banner on"),
                MenuEntry(label=menu_key("D", "isable"), brief="Turn the banner off"),
                MenuEntry(label=menu_key("i", "t", prefix="Ed"), brief="Edit the banner text"),
                MenuEntry(label=menu_key("B", "ack"), brief="Return to Session banners"),
            ],
            description_level,
            width=session.terminal_width,
            height=session.terminal_height,
        )
    )
    await session.write("Choice: ")


async def _preview_new_account_banner_before_screen(session: Session, lane: DatabaseLane) -> None:
    status, banner_text = await lane.run(
        lambda db: (new_account_banner_before_status(db), load_new_account_banner_before(db))
    )
    await session.write_line(
        colored("\r\nPreviewing new-account (before) banner as shown at signup:", fg_color=MUTED_COLOR)
    )
    if banner_text:
        await session.write_line(banner_text)
    else:
        await session.write_line(
            colored(
                f"(no banner -- enabled={status.enabled}, file exists={status.exists})", fg_color=MUTED_COLOR
            )
        )
    await session.write_line(colored("Press any key to continue...", fg_color=MUTED_COLOR))
    await session.read_key()


async def _enable_new_account_banner_before_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    status = await lane.run(new_account_banner_before_status)
    if not status.exists:
        await session.write_line(
            colored(
                f"No banner file found at {status.path}. Place a .ans file there first, then enable.",
                fg_color=MUTED_COLOR,
            )
        )
        return
    if (status.size_bytes or 0) > MAX_NEW_ACCOUNT_BANNER_BEFORE_SIZE_BYTES:
        await session.write_line(
            colored(
                f"Banner file at {status.path} is {status.size_bytes} bytes, over the "
                f"{MAX_NEW_ACCOUNT_BANNER_BEFORE_SIZE_BYTES} byte limit -- not enabling.",
                fg_color=MUTED_COLOR,
            )
        )
        return

    def _apply(db: Database) -> None:
        set_new_account_banner_before_enabled(db, True)
        record_action(db, actor=actor, action="enable_new_account_banner_before", detail=str(status.path))

    await lane.run(_apply)
    await session.write_line("New-account (before) banner enabled. Use [P]review to verify it looks right.")


async def _disable_new_account_banner_before_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    def _apply(db: Database):
        status = new_account_banner_before_status(db)
        set_new_account_banner_before_enabled(db, False)
        record_action(db, actor=actor, action="disable_new_account_banner_before", detail=str(status.path))
        return status

    status = await lane.run(_apply)
    await session.write_line(f"New-account (before) banner disabled. Your file at {status.path} was left in place.")


async def _edit_new_account_banner_before_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    path = await lane.run(new_account_banner_before_path)
    initial_bytes = path.read_bytes() if path.exists() else None
    draft_path = path.parent / f"{path.name}.draft"

    result = await edit_ansi_art(
        session, initial_bytes=initial_bytes, draft_path=draft_path,
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
    )
    if result is None:
        await session.write_line(colored("\r\nNo changes saved.", fg_color=MUTED_COLOR))
        return

    path.write_bytes(result)
    await lane.run(record_action, actor=actor, action="edit_new_account_banner_before", detail=str(path))
    await session.write_line(f"\r\nSaved {path}. Use [P]review to verify it looks right.")


# -- new-account banner (after signup) -------------------------------------


async def _new_account_banner_after_menu(session: Session, lane: DatabaseLane, actor: User) -> None:
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_new_account_banner_after_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "p":
            await session.write_line("")
            await _preview_new_account_banner_after_screen(session, lane)
            await _draw_new_account_banner_after_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "e":
            await session.write_line("")
            await _enable_new_account_banner_after_screen(session, lane, actor)
            await _draw_new_account_banner_after_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "d":
            await session.write_line("")
            await _disable_new_account_banner_after_screen(session, lane, actor)
            await _draw_new_account_banner_after_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "i":
            await session.write_line("")
            await _edit_new_account_banner_after_screen(session, lane, actor)
            await _draw_new_account_banner_after_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_new_account_banner_after_menu(
    session: Session, lane: DatabaseLane, description_level: str, redraw_in_place: bool,
    unicode_style: bool, collapsed: bool, header_color: int | tuple[int, int, int],
) -> None:
    status = await lane.run(new_account_banner_after_status)
    state = "ENABLED" if status.enabled else "disabled"
    file_state = f"{status.size_bytes} bytes" if status.exists else "missing"
    state_color = SUCCESS_COLOR if status.enabled else MUTED_COLOR
    file_color = METADATA_COLOR if status.exists else ERROR_COLOR
    detail = (
        colored(state, fg_color=state_color, bold=status.enabled)
        + colored(" -- file: ", fg_color=LABEL_COLOR)
        + colored(str(status.path), fg_color=METADATA_COLOR)
        + colored(f" ({file_state})", fg_color=file_color)
    )
    await session.write_line("\r\n" + screen_title("New account banner (after)",
            breadcrumb=(session.node_display_name, "System", "Session banners"), width=session.terminal_width,
            clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color,
            node_name_gradient=session.node_name_gradient))
    await session.write_line(
        colored(
            "Shown once self-service signup succeeds -- covers both an immediate login and a "
            "pending-approval account, alongside the existing message either way.",
            fg_color=MUTED_COLOR,
        )
    )
    await session.write_line(detail)
    await session.write_line(
        "\r\n" + _menu_row(
            [
                MenuEntry(label=menu_key("P", "review"), brief="Show the banner as callers see it"),
                MenuEntry(label=menu_key("E", "nable"), brief="Turn the banner on"),
                MenuEntry(label=menu_key("D", "isable"), brief="Turn the banner off"),
                MenuEntry(label=menu_key("i", "t", prefix="Ed"), brief="Edit the banner text"),
                MenuEntry(label=menu_key("B", "ack"), brief="Return to Session banners"),
            ],
            description_level,
            width=session.terminal_width,
            height=session.terminal_height,
        )
    )
    await session.write("Choice: ")


async def _preview_new_account_banner_after_screen(session: Session, lane: DatabaseLane) -> None:
    status, banner_text = await lane.run(
        lambda db: (new_account_banner_after_status(db), load_new_account_banner_after(db))
    )
    await session.write_line(
        colored("\r\nPreviewing new-account (after) banner as shown once signup succeeds:", fg_color=MUTED_COLOR)
    )
    if banner_text:
        await session.write_line(banner_text)
    else:
        await session.write_line(
            colored(
                f"(no banner -- enabled={status.enabled}, file exists={status.exists})", fg_color=MUTED_COLOR
            )
        )
    await session.write_line(colored("Press any key to continue...", fg_color=MUTED_COLOR))
    await session.read_key()


async def _enable_new_account_banner_after_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    status = await lane.run(new_account_banner_after_status)
    if not status.exists:
        await session.write_line(
            colored(
                f"No banner file found at {status.path}. Place a .ans file there first, then enable.",
                fg_color=MUTED_COLOR,
            )
        )
        return
    if (status.size_bytes or 0) > MAX_NEW_ACCOUNT_BANNER_AFTER_SIZE_BYTES:
        await session.write_line(
            colored(
                f"Banner file at {status.path} is {status.size_bytes} bytes, over the "
                f"{MAX_NEW_ACCOUNT_BANNER_AFTER_SIZE_BYTES} byte limit -- not enabling.",
                fg_color=MUTED_COLOR,
            )
        )
        return

    def _apply(db: Database) -> None:
        set_new_account_banner_after_enabled(db, True)
        record_action(db, actor=actor, action="enable_new_account_banner_after", detail=str(status.path))

    await lane.run(_apply)
    await session.write_line("New-account (after) banner enabled. Use [P]review to verify it looks right.")


async def _disable_new_account_banner_after_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    def _apply(db: Database):
        status = new_account_banner_after_status(db)
        set_new_account_banner_after_enabled(db, False)
        record_action(db, actor=actor, action="disable_new_account_banner_after", detail=str(status.path))
        return status

    status = await lane.run(_apply)
    await session.write_line(f"New-account (after) banner disabled. Your file at {status.path} was left in place.")


async def _edit_new_account_banner_after_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    path = await lane.run(new_account_banner_after_path)
    initial_bytes = path.read_bytes() if path.exists() else None
    draft_path = path.parent / f"{path.name}.draft"

    result = await edit_ansi_art(
        session, initial_bytes=initial_bytes, draft_path=draft_path,
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
    )
    if result is None:
        await session.write_line(colored("\r\nNo changes saved.", fg_color=MUTED_COLOR))
        return

    path.write_bytes(result)
    await lane.run(record_action, actor=actor, action="edit_new_account_banner_after", detail=str(path))
    await session.write_line(f"\r\nSaved {path}. Use [P]review to verify it looks right.")


# -- section mastheads (issue #176) --------------------------------------
#
# Extends the main-menu masthead (issue #161) to the three top-level
# index/listing screens -- board list, file areas, chat channel picker --
# via netbbs.net.picker.pick_item's own new `masthead` parameter (issue
# #176), each an independent singleton on the same mechanism as every
# banner/masthead above. Grouped under one new "Section mastheads"
# Settings entry for the same reason issue #177's own "Session banners"
# entry exists -- no good non-colliding mnemonic was left for three more
# flat top-level rows -- and, like that entry, this is a reasonable
# interim grouping, not a commitment to keep this exact shape once issue
# #178's own broader "Banners & Mastheads" reorg lands.


async def _section_mastheads_menu(session: Session, lane: DatabaseLane, actor: User) -> None:
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_section_mastheads_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "o":
            await session.write_line("")
            await _board_list_masthead_menu(session, lane, actor)
            await _draw_section_mastheads_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "f":
            await session.write_line("")
            await _file_area_masthead_menu(session, lane, actor)
            await _draw_section_mastheads_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "c":
            await session.write_line("")
            await _chat_channel_picker_masthead_menu(session, lane, actor)
            await _draw_section_mastheads_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_section_mastheads_menu(
    session: Session, lane: DatabaseLane, description_level: str, redraw_in_place: bool,
    unicode_style: bool, collapsed: bool, header_color: int | tuple[int, int, int],
) -> None:
    await session.write_line("\r\n" + screen_title("Section mastheads",
            breadcrumb=(session.node_display_name,), width=session.terminal_width, clear=redraw_in_place,
            unicode_style=unicode_style, collapsed=collapsed, header_color=header_color,
            node_name_gradient=session.node_name_gradient))
    await session.write_line(
        colored(
            "Optional mastheads shown above the board list, file-area list, and chat channel "
            "picker -- at every level of browsing (top level, a category, a Community), the "
            "same way the main-menu masthead marks the main menu.",
            fg_color=MUTED_COLOR,
        )
    )
    await session.write_line(
        "\r\n" + _menu_row(
            [
                MenuEntry(label=menu_key("o", "ard list", prefix="B"), brief="Above every board-list view"),
                MenuEntry(label=menu_key("F", "ile areas"), brief="Above every file-area-list view"),
                MenuEntry(label=menu_key("C", "hat channels"), brief="Above the channel picker"),
                MenuEntry(label=menu_key("B", "ack"), brief="Return to Settings"),
            ],
            description_level,
            width=session.terminal_width,
            height=session.terminal_height,
        )
    )
    await session.write("Choice: ")


# -- board list masthead --------------------------------------------------


async def _board_list_masthead_menu(session: Session, lane: DatabaseLane, actor: User) -> None:
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_board_list_masthead_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "p":
            await session.write_line("")
            await _preview_board_list_masthead_screen(session, lane)
            await _draw_board_list_masthead_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "e":
            await session.write_line("")
            await _enable_board_list_masthead_screen(session, lane, actor)
            await _draw_board_list_masthead_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "d":
            await session.write_line("")
            await _disable_board_list_masthead_screen(session, lane, actor)
            await _draw_board_list_masthead_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "i":
            await session.write_line("")
            await _edit_board_list_masthead_screen(session, lane, actor)
            await _draw_board_list_masthead_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_board_list_masthead_menu(
    session: Session, lane: DatabaseLane, description_level: str, redraw_in_place: bool,
    unicode_style: bool, collapsed: bool, header_color: int | tuple[int, int, int],
) -> None:
    status = await lane.run(board_list_banner_status)
    state = "ENABLED" if status.enabled else "disabled"
    file_state = f"{status.size_bytes} bytes" if status.exists else "missing"
    state_color = SUCCESS_COLOR if status.enabled else MUTED_COLOR
    file_color = METADATA_COLOR if status.exists else ERROR_COLOR
    detail = (
        colored(state, fg_color=state_color, bold=status.enabled)
        + colored(" -- file: ", fg_color=LABEL_COLOR)
        + colored(str(status.path), fg_color=METADATA_COLOR)
        + colored(f" ({file_state})", fg_color=file_color)
    )
    await session.write_line("\r\n" + screen_title("Board list masthead",
            breadcrumb=(session.node_display_name, "System", "Section mastheads"), width=session.terminal_width,
            clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color,
            node_name_gradient=session.node_name_gradient))
    await session.write_line(
        colored("Shown above every board-browsing view -- the top level, a category, or a "
                "Community/Uncategorized scope.", fg_color=MUTED_COLOR)
    )
    await session.write_line(detail)
    await session.write_line(
        "\r\n" + _menu_row(
            [
                MenuEntry(label=menu_key("P", "review"), brief="Show the masthead as callers see it"),
                MenuEntry(label=menu_key("E", "nable"), brief="Turn the masthead on"),
                MenuEntry(label=menu_key("D", "isable"), brief="Turn the masthead off"),
                MenuEntry(label=menu_key("i", "t", prefix="Ed"), brief="Edit the masthead art"),
                MenuEntry(label=menu_key("B", "ack"), brief="Return to Section mastheads"),
            ],
            description_level,
            width=session.terminal_width,
            height=session.terminal_height,
        )
    )
    await session.write("Choice: ")


async def _preview_board_list_masthead_screen(session: Session, lane: DatabaseLane) -> None:
    status, masthead_text = await lane.run(lambda db: (board_list_banner_status(db), load_board_list_banner(db)))
    await session.write_line(colored("\r\nPreviewing board list masthead as shown above the board list:", fg_color=MUTED_COLOR))
    if masthead_text:
        await session.write_line(masthead_text)
    else:
        await session.write_line(
            colored(f"(no masthead -- enabled={status.enabled}, file exists={status.exists})", fg_color=MUTED_COLOR)
        )
    await session.write_line(colored("Press any key to continue...", fg_color=MUTED_COLOR))
    await session.read_key()


async def _enable_board_list_masthead_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    status = await lane.run(board_list_banner_status)
    if not status.exists:
        await session.write_line(
            colored(
                f"No masthead file found at {status.path}. Place a .ans file there first, then enable.",
                fg_color=MUTED_COLOR,
            )
        )
        return
    if (status.size_bytes or 0) > MAX_BOARD_LIST_BANNER_SIZE_BYTES:
        await session.write_line(
            colored(
                f"Masthead file at {status.path} is {status.size_bytes} bytes, over the "
                f"{MAX_BOARD_LIST_BANNER_SIZE_BYTES} byte limit -- not enabling.",
                fg_color=MUTED_COLOR,
            )
        )
        return

    def _apply(db: Database) -> None:
        set_board_list_banner_enabled(db, True)
        record_action(db, actor=actor, action="enable_board_list_banner", detail=str(status.path))

    await lane.run(_apply)
    await session.write_line("Board list masthead enabled. Use [P]review to verify it looks right.")


async def _disable_board_list_masthead_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    def _apply(db: Database):
        status = board_list_banner_status(db)
        set_board_list_banner_enabled(db, False)
        record_action(db, actor=actor, action="disable_board_list_banner", detail=str(status.path))
        return status

    status = await lane.run(_apply)
    await session.write_line(f"Board list masthead disabled. Your file at {status.path} was left in place.")


async def _edit_board_list_masthead_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    path = await lane.run(board_list_banner_path)
    initial_bytes = path.read_bytes() if path.exists() else None
    draft_path = path.parent / f"{path.name}.draft"

    result = await edit_ansi_art(
        session, initial_bytes=initial_bytes, draft_path=draft_path,
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
    )
    if result is None:
        await session.write_line(colored("\r\nNo changes saved.", fg_color=MUTED_COLOR))
        return

    path.write_bytes(result)
    await lane.run(record_action, actor=actor, action="edit_board_list_banner", detail=str(path))
    await session.write_line(f"\r\nSaved {path}. Use [P]review to verify it looks right.")


# -- file area masthead ----------------------------------------------------


async def _file_area_masthead_menu(session: Session, lane: DatabaseLane, actor: User) -> None:
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_file_area_masthead_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "p":
            await session.write_line("")
            await _preview_file_area_masthead_screen(session, lane)
            await _draw_file_area_masthead_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "e":
            await session.write_line("")
            await _enable_file_area_masthead_screen(session, lane, actor)
            await _draw_file_area_masthead_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "d":
            await session.write_line("")
            await _disable_file_area_masthead_screen(session, lane, actor)
            await _draw_file_area_masthead_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "i":
            await session.write_line("")
            await _edit_file_area_masthead_screen(session, lane, actor)
            await _draw_file_area_masthead_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_file_area_masthead_menu(
    session: Session, lane: DatabaseLane, description_level: str, redraw_in_place: bool,
    unicode_style: bool, collapsed: bool, header_color: int | tuple[int, int, int],
) -> None:
    status = await lane.run(file_area_banner_status)
    state = "ENABLED" if status.enabled else "disabled"
    file_state = f"{status.size_bytes} bytes" if status.exists else "missing"
    state_color = SUCCESS_COLOR if status.enabled else MUTED_COLOR
    file_color = METADATA_COLOR if status.exists else ERROR_COLOR
    detail = (
        colored(state, fg_color=state_color, bold=status.enabled)
        + colored(" -- file: ", fg_color=LABEL_COLOR)
        + colored(str(status.path), fg_color=METADATA_COLOR)
        + colored(f" ({file_state})", fg_color=file_color)
    )
    await session.write_line("\r\n" + screen_title("File area masthead",
            breadcrumb=(session.node_display_name, "System", "Section mastheads"), width=session.terminal_width,
            clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color,
            node_name_gradient=session.node_name_gradient))
    await session.write_line(
        colored("Shown above every file-area-browsing view -- the top level, a category, or a "
                "Community/Uncategorized scope.", fg_color=MUTED_COLOR)
    )
    await session.write_line(detail)
    await session.write_line(
        "\r\n" + _menu_row(
            [
                MenuEntry(label=menu_key("P", "review"), brief="Show the masthead as callers see it"),
                MenuEntry(label=menu_key("E", "nable"), brief="Turn the masthead on"),
                MenuEntry(label=menu_key("D", "isable"), brief="Turn the masthead off"),
                MenuEntry(label=menu_key("i", "t", prefix="Ed"), brief="Edit the masthead art"),
                MenuEntry(label=menu_key("B", "ack"), brief="Return to Section mastheads"),
            ],
            description_level,
            width=session.terminal_width,
            height=session.terminal_height,
        )
    )
    await session.write("Choice: ")


async def _preview_file_area_masthead_screen(session: Session, lane: DatabaseLane) -> None:
    status, masthead_text = await lane.run(lambda db: (file_area_banner_status(db), load_file_area_banner(db)))
    await session.write_line(colored("\r\nPreviewing file area masthead as shown above the file-area list:", fg_color=MUTED_COLOR))
    if masthead_text:
        await session.write_line(masthead_text)
    else:
        await session.write_line(
            colored(f"(no masthead -- enabled={status.enabled}, file exists={status.exists})", fg_color=MUTED_COLOR)
        )
    await session.write_line(colored("Press any key to continue...", fg_color=MUTED_COLOR))
    await session.read_key()


async def _enable_file_area_masthead_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    status = await lane.run(file_area_banner_status)
    if not status.exists:
        await session.write_line(
            colored(
                f"No masthead file found at {status.path}. Place a .ans file there first, then enable.",
                fg_color=MUTED_COLOR,
            )
        )
        return
    if (status.size_bytes or 0) > MAX_FILE_AREA_BANNER_SIZE_BYTES:
        await session.write_line(
            colored(
                f"Masthead file at {status.path} is {status.size_bytes} bytes, over the "
                f"{MAX_FILE_AREA_BANNER_SIZE_BYTES} byte limit -- not enabling.",
                fg_color=MUTED_COLOR,
            )
        )
        return

    def _apply(db: Database) -> None:
        set_file_area_banner_enabled(db, True)
        record_action(db, actor=actor, action="enable_file_area_banner", detail=str(status.path))

    await lane.run(_apply)
    await session.write_line("File area masthead enabled. Use [P]review to verify it looks right.")


async def _disable_file_area_masthead_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    def _apply(db: Database):
        status = file_area_banner_status(db)
        set_file_area_banner_enabled(db, False)
        record_action(db, actor=actor, action="disable_file_area_banner", detail=str(status.path))
        return status

    status = await lane.run(_apply)
    await session.write_line(f"File area masthead disabled. Your file at {status.path} was left in place.")


async def _edit_file_area_masthead_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    path = await lane.run(file_area_banner_path)
    initial_bytes = path.read_bytes() if path.exists() else None
    draft_path = path.parent / f"{path.name}.draft"

    result = await edit_ansi_art(
        session, initial_bytes=initial_bytes, draft_path=draft_path,
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
    )
    if result is None:
        await session.write_line(colored("\r\nNo changes saved.", fg_color=MUTED_COLOR))
        return

    path.write_bytes(result)
    await lane.run(record_action, actor=actor, action="edit_file_area_banner", detail=str(path))
    await session.write_line(f"\r\nSaved {path}. Use [P]review to verify it looks right.")


# -- chat channel picker masthead -------------------------------------------


async def _chat_channel_picker_masthead_menu(session: Session, lane: DatabaseLane, actor: User) -> None:
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_chat_channel_picker_masthead_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "p":
            await session.write_line("")
            await _preview_chat_channel_picker_masthead_screen(session, lane)
            await _draw_chat_channel_picker_masthead_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "e":
            await session.write_line("")
            await _enable_chat_channel_picker_masthead_screen(session, lane, actor)
            await _draw_chat_channel_picker_masthead_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "d":
            await session.write_line("")
            await _disable_chat_channel_picker_masthead_screen(session, lane, actor)
            await _draw_chat_channel_picker_masthead_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "i":
            await session.write_line("")
            await _edit_chat_channel_picker_masthead_screen(session, lane, actor)
            await _draw_chat_channel_picker_masthead_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_chat_channel_picker_masthead_menu(
    session: Session, lane: DatabaseLane, description_level: str, redraw_in_place: bool,
    unicode_style: bool, collapsed: bool, header_color: int | tuple[int, int, int],
) -> None:
    status = await lane.run(chat_channel_picker_banner_status)
    state = "ENABLED" if status.enabled else "disabled"
    file_state = f"{status.size_bytes} bytes" if status.exists else "missing"
    state_color = SUCCESS_COLOR if status.enabled else MUTED_COLOR
    file_color = METADATA_COLOR if status.exists else ERROR_COLOR
    detail = (
        colored(state, fg_color=state_color, bold=status.enabled)
        + colored(" -- file: ", fg_color=LABEL_COLOR)
        + colored(str(status.path), fg_color=METADATA_COLOR)
        + colored(f" ({file_state})", fg_color=file_color)
    )
    await session.write_line("\r\n" + screen_title("Chat channel picker masthead",
            breadcrumb=(session.node_display_name, "System", "Section mastheads"), width=session.terminal_width,
            clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color,
            node_name_gradient=session.node_name_gradient))
    await session.write_line(
        colored("Shown above every channel-picker view -- the top level, a category, or a "
                "Community/Uncategorized scope. Never inside a live channel.", fg_color=MUTED_COLOR)
    )
    await session.write_line(detail)
    await session.write_line(
        "\r\n" + _menu_row(
            [
                MenuEntry(label=menu_key("P", "review"), brief="Show the masthead as callers see it"),
                MenuEntry(label=menu_key("E", "nable"), brief="Turn the masthead on"),
                MenuEntry(label=menu_key("D", "isable"), brief="Turn the masthead off"),
                MenuEntry(label=menu_key("i", "t", prefix="Ed"), brief="Edit the masthead art"),
                MenuEntry(label=menu_key("B", "ack"), brief="Return to Section mastheads"),
            ],
            description_level,
            width=session.terminal_width,
            height=session.terminal_height,
        )
    )
    await session.write("Choice: ")


async def _preview_chat_channel_picker_masthead_screen(session: Session, lane: DatabaseLane) -> None:
    status, masthead_text = await lane.run(
        lambda db: (chat_channel_picker_banner_status(db), load_chat_channel_picker_banner(db))
    )
    await session.write_line(
        colored("\r\nPreviewing chat channel picker masthead as shown above the channel picker:", fg_color=MUTED_COLOR)
    )
    if masthead_text:
        await session.write_line(masthead_text)
    else:
        await session.write_line(
            colored(f"(no masthead -- enabled={status.enabled}, file exists={status.exists})", fg_color=MUTED_COLOR)
        )
    await session.write_line(colored("Press any key to continue...", fg_color=MUTED_COLOR))
    await session.read_key()


async def _enable_chat_channel_picker_masthead_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    status = await lane.run(chat_channel_picker_banner_status)
    if not status.exists:
        await session.write_line(
            colored(
                f"No masthead file found at {status.path}. Place a .ans file there first, then enable.",
                fg_color=MUTED_COLOR,
            )
        )
        return
    if (status.size_bytes or 0) > MAX_CHAT_CHANNEL_PICKER_BANNER_SIZE_BYTES:
        await session.write_line(
            colored(
                f"Masthead file at {status.path} is {status.size_bytes} bytes, over the "
                f"{MAX_CHAT_CHANNEL_PICKER_BANNER_SIZE_BYTES} byte limit -- not enabling.",
                fg_color=MUTED_COLOR,
            )
        )
        return

    def _apply(db: Database) -> None:
        set_chat_channel_picker_banner_enabled(db, True)
        record_action(db, actor=actor, action="enable_chat_channel_picker_banner", detail=str(status.path))

    await lane.run(_apply)
    await session.write_line("Chat channel picker masthead enabled. Use [P]review to verify it looks right.")


async def _disable_chat_channel_picker_masthead_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    def _apply(db: Database):
        status = chat_channel_picker_banner_status(db)
        set_chat_channel_picker_banner_enabled(db, False)
        record_action(db, actor=actor, action="disable_chat_channel_picker_banner", detail=str(status.path))
        return status

    status = await lane.run(_apply)
    await session.write_line(f"Chat channel picker masthead disabled. Your file at {status.path} was left in place.")


async def _edit_chat_channel_picker_masthead_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    path = await lane.run(chat_channel_picker_banner_path)
    initial_bytes = path.read_bytes() if path.exists() else None
    draft_path = path.parent / f"{path.name}.draft"

    result = await edit_ansi_art(
        session, initial_bytes=initial_bytes, draft_path=draft_path,
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
    )
    if result is None:
        await session.write_line(colored("\r\nNo changes saved.", fg_color=MUTED_COLOR))
        return

    path.write_bytes(result)
    await lane.run(record_action, actor=actor, action="edit_chat_channel_picker_banner", detail=str(path))
    await session.write_line(f"\r\nSaved {path}. Use [P]review to verify it looks right.")


# -- node colors (issue #162) ------------------------------------------


_THEME_SLOT_LABELS = {"accent": "Accent color", "header": "Header color", "clock": "Clock color"}
_THEME_SLOT_GETTERS = {
    "accent": accent_color_override, "header": header_color_override, "clock": clock_color_override,
}
_THEME_SLOT_SETTERS = {
    "accent": set_accent_color_override, "header": set_header_color_override, "clock": set_clock_color_override,
}
_THEME_SLOT_DEFAULTS = {"accent": ACCENT_COLOR, "header": HEADER_COLOR, "clock": CLOCK_COLOR}


def _theme_sample_text(slot: str) -> str:
    """One real rendered fragment per slot -- board name for accent,
    a section-header banner for header, a main-menu clock for clock --
    rather than an abstract swatch, so a SysOp judges the color against
    the same shape of text it will actually appear on."""
    if slot == "accent":
        return "  MetroBBS Underground"
    if slot == "header":
        return "== Node management =="
    return "14:32:07"


def _theme_sample_line(slot: str, color: int | tuple[int, int, int]) -> str:
    return colored(_theme_sample_text(slot), fg_color=color, bold=True)


def _theme_color_status_line(label: str, rgb: tuple[int, int, int] | None) -> str:
    if rgb is None:
        return colored(f"{label}: ", fg_color=LABEL_COLOR) + colored("default", fg_color=MUTED_COLOR)
    value = f"{rgb[0]},{rgb[1]},{rgb[2]}"
    swatch = colored("****", fg_color=rgb, bold=True)
    return colored(f"{label}: ", fg_color=LABEL_COLOR) + swatch + colored(f"  {value}", fg_color=METADATA_COLOR)


async def _draw_theme_colors_menu(
    session: Session, lane: DatabaseLane, description_level: str, redraw_in_place: bool,
    unicode_style: bool, collapsed: bool,
) -> None:
    def _load(db: Database):
        return accent_color_override(db), header_color_override(db), clock_color_override(db), effective_header_color_256(db)

    accent_rgb, header_rgb, clock_rgb, header_color = await lane.run(_load)
    await session.write_line("\r\n" + screen_title("Node colors",
            breadcrumb=(session.node_display_name,), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            header_color=header_color, node_name_gradient=session.node_name_gradient))
    await session.write_line(
        colored(
            "Branding only -- status colors (error/success/warning/etc.) always stay standard, "
            "so a caller can trust what they mean on any node.",
            fg_color=MUTED_COLOR,
        )
    )
    await session.write_line(_theme_color_status_line("Accent", accent_rgb))
    await session.write_line(_theme_color_status_line("Header", header_rgb))
    await session.write_line(_theme_color_status_line("Clock ", clock_rgb))
    await session.write_line(
        "\r\n" + _menu_row(
            [
                MenuEntry(label=menu_key("A", "ccent"), brief="Board/channel/user-name color"),
                MenuEntry(label=menu_key("H", "eader"), brief="Section-header color"),
                MenuEntry(label=menu_key("C", "lock"), brief="Main-menu clock color"),
                MenuEntry(label=menu_key("P", "review"), brief="Sample text, truecolor vs. 256-color"),
                MenuEntry(label=menu_key("B", "ack"), brief="Return to Settings"),
            ],
            description_level,
            width=session.terminal_width,
            height=session.terminal_height,
        )
    )
    await session.write("Choice: ")


async def _set_theme_color_screen(session: Session, lane: DatabaseLane, actor: User, *, slot: str) -> None:
    """Set or clear one of the three overridable branding colors.

    Previews the candidate RGB against real sample text at both
    truecolor and 256-color depth *before* asking for confirmation --
    issue #162's own explicit requirement -- rather than applying first
    and letting a SysOp discover a bad choice only on the next real
    screen that happens to use it."""
    label = _THEME_SLOT_LABELS[slot]
    getter = _THEME_SLOT_GETTERS[slot]
    setter = _THEME_SLOT_SETTERS[slot]
    current = await lane.run(getter)
    current_text = f"{current[0]},{current[1]},{current[2]}" if current is not None else "default"

    await session.write_line(colored(f"\r\n{label}:", fg_color=await lane.run(effective_header_color_256), bold=True))
    await session.write_line(f"Currently: {current_text}")
    await session.write_line(
        colored(
            "Enter a color as R,G,B (each 0-255), 'default' to clear the override, "
            "or leave blank to make no change.",
            fg_color=MUTED_COLOR,
        )
    )
    await session.write(f"{label} [{current_text}]: ")
    raw = (await session.read_line()).strip()
    if not raw:
        await session.write_line("No change.")
        return

    if raw.lower() == "default":
        if current is None:
            await session.write_line("Already using the default -- no change.")
            return
        if not await prompt_yes_no(session, f"Clear the {label.lower()} override and revert to the default?", default=False):
            await session.write_line("Cancelled.")
            return
        await lane.run(setter, None)
        await lane.run(record_action, actor=actor, action=f"clear_{slot}_color_override", detail=current_text)
        await session.write_line(f"{label} reverted to the default.")
        return

    parts = raw.split(",")
    rgb: tuple[int, int, int] | None = None
    if len(parts) == 3:
        try:
            candidate = tuple(int(part.strip()) for part in parts)
        except ValueError:
            candidate = None
        if candidate is not None and all(0 <= value <= 255 for value in candidate):
            rgb = candidate  # type: ignore[assignment]
    if rgb is None:
        await session.write_line(colored("Not a valid R,G,B triple (each 0-255) -- no change.", fg_color=ERROR_COLOR))
        return

    await session.write_line(colored("\r\nPreview:", fg_color=MUTED_COLOR))
    await session.write_line(colored("  Truecolor: ", fg_color=LABEL_COLOR) + _theme_sample_line(slot, rgb))
    await session.write_line(colored("  256-color: ", fg_color=LABEL_COLOR) + _theme_sample_line(slot, nearest_256(rgb)))

    if not await prompt_yes_no(session, f"Apply this {label.lower()}?", default=False):
        await session.write_line("Not applied.")
        return

    await lane.run(setter, rgb)
    await lane.run(record_action, actor=actor, action=f"set_{slot}_color_override", detail=f"{rgb[0]},{rgb[1]},{rgb[2]}")
    await session.write_line(f"{label} updated.")


async def _preview_theme_colors_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    def _load(db: Database):
        return accent_color_override(db), header_color_override(db), clock_color_override(db)

    overrides = await lane.run(_load)
    await session.write_line(colored("\r\nNode color preview:", fg_color=MUTED_COLOR))
    for slot, rgb in zip(("accent", "header", "clock"), overrides):
        label = _THEME_SLOT_LABELS[slot]
        await session.write_line(colored(f"\r\n{label}:", fg_color=LABEL_COLOR, bold=True))
        if rgb is None:
            await session.write_line(
                colored("  Default:   ", fg_color=MUTED_COLOR) + _theme_sample_line(slot, _THEME_SLOT_DEFAULTS[slot])
            )
        else:
            await session.write_line(colored("  Truecolor: ", fg_color=MUTED_COLOR) + _theme_sample_line(slot, rgb))
            await session.write_line(
                colored("  256-color: ", fg_color=MUTED_COLOR) + _theme_sample_line(slot, nearest_256(rgb))
            )
    await session.write_line(colored("\r\nPress any key to continue...", fg_color=MUTED_COLOR))
    await session.read_key()


async def _theme_colors_menu(session: Session, lane: DatabaseLane, actor: User) -> None:
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    await _draw_theme_colors_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed)
    while True:
        choice = (await session.read_key()).lower()
        if choice == "b":
            await session.write_line("")
            return
        elif choice == "a":
            await session.write_line("")
            await _set_theme_color_screen(session, lane, actor, slot="accent")
            await _draw_theme_colors_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed)
        elif choice == "h":
            await session.write_line("")
            await _set_theme_color_screen(session, lane, actor, slot="header")
            await _draw_theme_colors_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed)
        elif choice == "c":
            await session.write_line("")
            await _set_theme_color_screen(session, lane, actor, slot="clock")
            await _draw_theme_colors_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed)
        elif choice == "p":
            await session.write_line("")
            await _preview_theme_colors_screen(session, lane, actor)
            await _draw_theme_colors_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed)
        else:
            await session.write(reject_unhandled_key(choice))


# -- boards & areas (design doc) ---------------------------------------
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
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_content_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "m":
            await session.write_line("")
            await _board_menu(session, lane, actor, link_context=link_context)
            await _draw_content_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "f":
            await session.write_line("")
            await _area_menu(session, lane, actor, link_context=link_context)
            await _draw_content_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "d":
            await session.write_line("")
            await _door_menu(session, lane, actor)
            await _draw_content_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "n":
            await session.write_line("")
            await _channel_menu(session, lane, actor, link_context=link_context)
            await _draw_content_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "c":
            await session.write_line("")
            await _category_menu(session, lane, actor)
            await _draw_content_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "o":
            await session.write_line("")
            await _community_menu(session, lane, actor)
            await _draw_content_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "g":
            await session.write_line("")
            await _grant_moderator_screen(session, lane, actor)
            await _draw_content_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "r":
            await session.write_line("")
            await _revoke_moderator_screen(session, lane, actor)
            await _draw_content_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_content_menu(session: Session, description_level: str, redraw_in_place: bool, unicode_style: bool, collapsed: bool, header_color: int | tuple[int, int, int] = HEADER_COLOR) -> None:
    await session.write_line(
        "\r\n" + screen_title("Manage message boards/file areas/chat channels",
            breadcrumb=(session.node_display_name,), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color, node_name_gradient=session.node_name_gradient)
    )
    await session.write_line(
        _menu_row(
            [
                MenuEntry(label=menu_key("M", "essage boards"), brief="Create/edit message boards"),
                MenuEntry(label=menu_key("F", "ile areas"), brief="Create/edit file areas"),
                MenuEntry(label=menu_key("D", "oors"), brief="Register/edit door games"),
                MenuEntry(label=menu_key("n", "nels", prefix="Chat cha"), brief="Create/edit chat channels"),
                MenuEntry(label=menu_key("C", "ategories"), brief="Organize boards/areas/channels"),
                MenuEntry(label=menu_key("O", "mmunities", prefix="C"), brief="Manage Communities"),
                MenuEntry(label=menu_key("G", "rant moderator"), brief="Grant a moderation scope"),
                MenuEntry(label=menu_key("R", "evoke moderator"), brief="Revoke a moderation scope"),
                MenuEntry(label=menu_key("B", "ack"), brief="Return to the SysOp console"),
            ],
            description_level,
            width=session.terminal_width,
            height=session.terminal_height,
        )
    )
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
    introduced by design doc §16's Community inheritance model:
    boards'/file areas' own `min_read_level`/`min_write_level`
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
    (design doc §18). Returns `(value, ok)` -- `ok=False`
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
    """Shared name_requirement prompt (design doc §18) --
    `none` (no gate), `verified` (SysOp can identify but nothing is
    displayed), or `verified_and_displayed` (shown within this
    resource's own rendering, design doc)."""
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
    redraw_in_place: bool = False,
    unicode_style: bool = False,
    collapsed: bool = False,
):
    """Optional category picker shared by board/channel/area create+edit
    screens. Top-level categories are shown first; picking one that has
    sub-categories offers picking one of those instead, matching the
    two-level design (`netbbs.boards.categories`/`netbbs.chat.categories`/
    `netbbs.files.categories`). Returns the chosen category's id, or
    `None` if cancelled (the picker's own `[B]ack`) or none exist.

    `community_id`/`resources` (design doc §16's category
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

    Dogfood-reported bug: this used to gate the whole picker behind an
    "Assign a category?" yes/no first -- a holdover from before this
    was a directly addressable field (`_category_field`) on the
    cursor-nav field list; a caller who presses this field's own hotkey
    has already made the "yes, I want to interact with this field"
    choice once, and the picker's own `[B]ack` is the actual "decline"
    affordance. `pick_item` itself already reports "No categories exist
    yet." and returns immediately when there's nothing to offer -- that
    part stays, since it's useful feedback in its own right; only the
    redundant upfront yes/no is gone.
    """
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
    accent_color = await lane.run(effective_accent_color_256)
    header_color = await lane.run(effective_header_color_256)

    selected = await pick_item(
        session, top_level,
        name_of=lambda c: c.name,
        stable_id_of=lambda c: c.id,
        title=title,
        empty_message="No categories exist yet.",
        redraw_in_place=redraw_in_place,
        unicode_style=unicode_style,
        collapsed=collapsed,
        accent_color=accent_color,
        header_color=header_color,
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
        redraw_in_place=redraw_in_place,
        unicode_style=unicode_style,
        collapsed=collapsed,
        accent_color=accent_color,
        header_color=header_color,
    )
    return sub_selected.id if sub_selected is not None else selected.id


async def _pick_optional_community(
    session: Session,
    lane: DatabaseLane,
    *,
    redraw_in_place: bool = False,
    unicode_style: bool = False,
    collapsed: bool = False,
) -> int | None:
    """Optional Community picker shared by board/channel/area create+
    edit screens (design doc §16) -- mirrors
    `_pick_optional_category` exactly, but flat (a Community has no
    two-level sub-structure the way categories do). Prompted *before*
    the existing category prompt at every call site -- Community is the
    outer layer, chosen first. Returns the chosen Community's id, or
    `None` if cancelled (the picker's own `[B]ack`) or none exist yet
    -- see `_pick_optional_category`'s own docstring for why this no
    longer gates the picker behind a separate "Assign a Community?"
    yes/no first."""
    selected = await pick_item(
        session, await lane.run(list_communities),
        name_of=lambda c: c.name,
        stable_id_of=lambda c: c.id,
        title="Community",
        empty_message="No Communities exist yet.",
        redraw_in_place=redraw_in_place,
        unicode_style=unicode_style,
        collapsed=collapsed,
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    return selected.id if selected is not None else None


def _optional_int_label(value: int | None, *, none_word: str = "none") -> str:
    return str(value) if value is not None else none_word


# -- FieldSpec adapters (design doc, dogfood feature request) --------
#
# Thin wrappers turning this module's own existing per-type prompt
# helpers (above) into the `netbbs.net.resource_editor.FieldPrompt`
# shape a draft-based create/edit screen needs: read/parse, then
# mutate `draft[key]` in place on success, leave it untouched
# (draft-preserving, not screen-aborting) on a rejected entry -- unlike
# every one of these helpers' own original callers, which used to
# treat a `False` `ok` as "abandon the entire in-progress wizard."


def _optional_int_field(key: str, label: str) -> Callable[[Session, DatabaseLane, dict], Awaitable[None]]:
    async def prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        value, ok = await _prompt_optional_int(session, label, current=draft.get(key))
        if ok:
            draft[key] = value

    return prompt


def _int_field(key: str, label: str) -> Callable[[Session, DatabaseLane, dict], Awaitable[None]]:
    """A plain, non-nullable int field (channel's own `min_level` has
    no Community-inheritance concept the way boards'/areas' `min_read_
    level`/`min_write_level` do, so `_read_int`, not `_prompt_optional_
    int`, is the right underlying primitive here)."""

    async def prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        await session.write(f"{label} [{draft.get(key)}]: ")
        value = await _read_int(session, default=draft.get(key))
        if value is not None:
            draft[key] = value

    return prompt


def _min_age_field(key: str = "min_age") -> Callable[[Session, DatabaseLane, dict], Awaitable[None]]:
    async def prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        value, ok = await _prompt_min_age(session, current=draft.get(key))
        if ok:
            draft[key] = value

    return prompt


def _name_requirement_label(value: str | None) -> str:
    """Dogfood follow-up: since issue #153 turned this from typed text
    into a cycling toggle, showing the raw stored value verbatim
    ('verified_and_displayed') means every render still displays the
    field's internal string constant, underscores and all, rather than
    words a SysOp would actually write. 'none'/'verified' already read
    fine as-is; only the compound value needs it."""
    return (value or "none").replace("_", " ")


_NAME_REQUIREMENT_VALUES = [None, "verified", "verified_and_displayed"]


def _name_requirement_field(key: str = "name_requirement") -> Callable[[Session, DatabaseLane, dict], Awaitable[None]]:
    """Cycles none -> verified -> verified_and_displayed -> none on
    each hotkey press (dogfood feature request, issue #153) instead of
    requiring the SysOp to type the literal string
    `verified_and_displayed`. Only this draft-editor field changes --
    `_prompt_name_requirement` itself stays typed-text for its other
    (non-draft-editor, "recommend a value for Link" wizard) callers,
    which weren't part of the reported complaint."""
    return choice_field(key, _NAME_REQUIREMENT_VALUES)


def _name_requirement_step(key: str = "name_requirement") -> Callable[[dict, int], None]:
    """`FieldSpec.step` counterpart to `_name_requirement_field`
    (dogfood feature request, issue #160's cursor-navigation
    follow-up) -- Left/Right cycle the same three values Space/Enter/
    the hotkey letter already do, just in either direction."""
    return choice_step(key, _NAME_REQUIREMENT_VALUES)


def _resolve_redraw_preference(db: Database, actor: User) -> tuple[bool, bool]:
    """`(redraw_in_place, show_hint)` for `edit_resource_draft` -- one
    `lane.run` round trip instead of two, resolved once by the caller
    before entering the screen, the same "caller resolves, function
    just trusts it" shape `description_level` already uses. The hint
    is only ever worth showing when the preference is both off and
    never explicitly touched -- an account that already turned it on,
    or already turned it down, doesn't need to be told about it again."""
    enabled = redraw_in_place_enabled(db, actor)
    show_hint = not enabled and not redraw_in_place_ever_set(db, actor)
    return enabled, show_hint


# Dogfood feature request, issue #150's own concrete example ("new
# SysOps have absolutely no clue what name requirements do"): Ctrl-H
# help text for every FieldSpec below that exposes this field. One
# shared string so the four call sites (board/area/channel plus
# Community's own cascading default) can never drift apart.
_NAME_REQUIREMENT_HELP = (
    "Gates posting/joining on identity: 'none' has no gate. 'verified' requires "
    "attestation but shows nothing about it. 'verified_and_displayed' also shows the "
    "caller's attested real name alongside their posts here."
)


def _community_field(
    key: str = "community_id",
    *,
    redraw_in_place: bool = False,
    unicode_style: bool = False,
    collapsed: bool = False,
) -> Callable[[Session, DatabaseLane, dict], Awaitable[None]]:
    """Also stashes the chosen Community's own name into
    `draft[f"{key}_label"]` -- the field's `render` callback must stay
    a pure, synchronous dict read (`edit_resource_draft`'s own
    contract), so the name is resolved here, at prompt time, where a
    `lane` round-trip is already in flight, rather than re-fetched on
    every redraw.

    `redraw_in_place`/`unicode_style`/`collapsed`: this factory is
    called once, at field-list construction time, by a caller that has
    already resolved these same three preferences for its own
    `edit_resource_draft` call a few lines later -- threaded straight
    through and closed over here rather than widening `FieldSpec.
    prompt`'s own fixed `(session, lane, draft)` contract, which every
    other field on this screen (and every other draft-editor screen)
    also implements and would otherwise all need to change."""

    async def prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        community_id = await _pick_optional_community(
            session, lane, redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed
        )
        draft[key] = community_id
        community = await lane.run(get_community, community_id) if community_id is not None else None
        draft[f"{key}_label"] = community.name if community is not None else None

    return prompt


def _category_field(
    *,
    list_top_level,
    list_subcategories,
    title: str,
    list_resources,
    get_by_id,
    redraw_in_place: bool = False,
    unicode_style: bool = False,
    collapsed: bool = False,
) -> Callable[[Session, DatabaseLane, dict], Awaitable[None]]:
    """`list_resources` is the plain `netbbs.<kind>.list_*` domain
    function (e.g. `list_boards`) -- dispatched through `lane` here,
    same as `_pick_optional_category`'s own existing call sites already
    did, rather than asking every field-spec builder to pre-fetch it.
    `get_by_id` resolves the chosen category's own name into
    `draft["category_id_label"]`, same reasoning as `_community_field`'s
    own `_label` companion above. `redraw_in_place`/`unicode_style`/
    `collapsed`: see `_community_field`'s own docstring -- identical
    reasoning, applied here too."""

    async def prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        category_id = await _pick_optional_category(
            session, lane, list_top_level=list_top_level, list_subcategories=list_subcategories,
            title=title, community_id=draft.get("community_id"), resources=await lane.run(list_resources),
            redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
        )
        draft["category_id"] = category_id
        category = await lane.run(get_by_id, category_id) if category_id is not None else None
        draft["category_id_label"] = category.name if category is not None else None

    return prompt


def _community_label(db: Database, community_id: int | None) -> str:
    """Detail-screen display helper -- `(none)` for `community_id is
    None`, else the Community's own name (sanitized, same as every
    other user-controlled string shown on these detail screens)."""
    community = get_community(db, community_id)
    return sanitize_text(community.name) if community is not None else "(none)"


# -- Communities (design doc §16) ------------------


async def _community_menu(session: Session, lane: DatabaseLane, actor: User) -> None:
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_community_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "c":
            await session.write_line("")
            await _community_screen(session, lane, actor)
            await _draw_community_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "l":
            await session.write_line("")
            await _list_communities_screen(session, lane, actor)
            await _draw_community_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_community_menu(session: Session, description_level: str, redraw_in_place: bool, unicode_style: bool, collapsed: bool, header_color: int | tuple[int, int, int] = HEADER_COLOR) -> None:
    await session.write_line("\r\n" + screen_title("Communities",
            breadcrumb=(session.node_display_name,), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color, node_name_gradient=session.node_name_gradient))
    await session.write_line(
        _menu_row(
            [
                MenuEntry(label=menu_key("C", "reate"), brief="Add a new Community"),
                MenuEntry(label=menu_key("L", "ist"), brief="Browse and edit Communities"),
                MenuEntry(label=menu_key("B", "ack"), brief="Return to the Content menu"),
            ],
            description_level,
            width=session.terminal_width,
            height=session.terminal_height,
        )
    )
    await session.write("Choice: ")


def _community_field_specs() -> list[FieldSpec]:
    """One shared field list drives both create and edit (design doc,
    dogfood feature request) -- see `_community_screen`. Unlike board/
    channel/file-area, a Community has no `community_id`/`category_id`
    of its own (nothing above it) and no `pinned`/`moderated` -- just
    name/description/hidden plus its four `default_*` inheritance
    fields (design doc §16)."""
    return [
        FieldSpec(
            key="name", hotkey="n", menu_text=menu_key("N", "ame"), label="Name",
            render=lambda d: d.get("name") or "(blank)",
            prompt=text_field("name", required=True),
            brief="The community's display name",
            help="The Community's display name, shown wherever it's listed. Must be non-blank.",
        ),
        FieldSpec(
            key="description", hotkey="d", menu_text=menu_key("D", "escription"), label="Description",
            render=lambda d: d.get("description") or "(none)",
            prompt=text_field("description"),
            brief="Shown in the community directory",
            help="A short explanation of what this Community is for, shown in the Communities "
            "directory alongside its name.",
        ),
        FieldSpec(
            key="hidden", hotkey="h", menu_text=menu_key("H", "idden"), label="Hidden",
            render=lambda d: "yes" if d.get("hidden") else "no",
            prompt=bool_field("hidden", "Hidden?"),
            brief="Hide from the communities list",
            help=(
                "Delists this Community from ordinary browsing without deleting it. A SysOp "
                "still sees it everywhere; boards/areas/channels inside keep working normally "
                "for anyone who already knows to look for them."
            ),
        ),
        FieldSpec(
            key="default_min_read_level", hotkey="r", menu_text=menu_key("R", "ead level"),
            label="Default read level",
            render=lambda d: _optional_int_label(d.get("default_min_read_level")),
            prompt=_optional_int_field("default_min_read_level", "Default minimum read level"),
            brief="Default read level, inherited",
            help=(
                "The read-level floor every board/area/channel in this Community inherits "
                "unless it sets its own. 'none' means no inherited floor -- each resource's "
                "own level (or lack of one) applies instead."
            ),
        ),
        FieldSpec(
            key="default_min_write_level", hotkey="w", menu_text=menu_key("W", "rite level"),
            label="Default write level",
            render=lambda d: _optional_int_label(d.get("default_min_write_level")),
            prompt=_optional_int_field("default_min_write_level", "Default minimum write level"),
            brief="Default write level, inherited",
            help=(
                "The write-level floor every board/area/channel in this Community inherits "
                "unless it sets its own. 'none' means no inherited floor."
            ),
        ),
        FieldSpec(
            key="default_min_age", hotkey="g", menu_text=menu_key("G", "e", prefix="Min a"),
            label="Default min age",
            render=lambda d: _optional_int_label(d.get("default_min_age")),
            prompt=_min_age_field("default_min_age"),
            brief="Default min. age, inherited",
            help=(
                "The minimum-age gate every board/area/channel in this Community inherits "
                "unless it sets its own. 'none' means no inherited gate."
            ),
        ),
        FieldSpec(
            key="default_name_requirement", hotkey="q", menu_text=menu_key("q", "uirement", prefix="Name re"),
            label="Default name requirement",
            render=lambda d: _name_requirement_label(d.get("default_name_requirement")),
            prompt=_name_requirement_field("default_name_requirement"),
            step=_name_requirement_step("default_name_requirement"),
            help=_NAME_REQUIREMENT_HELP + " Message boards/file areas/chat channels in this "
            "Community inherit this unless they set their own.",
            brief="Default name rule, inherited",
        ),
    ]


async def _community_screen(
    session: Session, lane: DatabaseLane, actor: User, *, existing: Community | None = None
) -> Community | None:
    """Unified create/edit screen -- see `_board_screen`'s own
    docstring for the general shape and reasoning, identical here.

    Unlike the old separate `_create_community_screen`/
    `_edit_community_screen` pair, creating no longer needs to stay
    "lean" (name/description only, with a forced follow-up trip into
    `_community_detail_screen` to configure the rest) -- every field is
    available immediately, at its own sensible default, on this one
    screen, the same as board/channel/file-area creation already
    works. No longer auto-enters the detail screen after a successful
    create; the caller's own menu redraw is enough, matching every
    other resource kind's own create flow.
    """
    if existing is not None:
        draft = {
            "name": existing.name, "description": existing.description, "hidden": existing.hidden,
            "default_min_read_level": existing.default_min_read_level,
            "default_min_write_level": existing.default_min_write_level,
            "default_min_age": existing.default_min_age,
            "default_name_requirement": existing.default_name_requirement,
        }
    else:
        draft = {
            "name": "", "description": None, "hidden": False,
            "default_min_read_level": None, "default_min_write_level": None,
            "default_min_age": None, "default_name_requirement": None,
        }

    async def save(draft: dict) -> Community:
        if not draft["name"]:
            raise CommunityError("name cannot be blank")
        if existing is None:
            return await lane.run(
                create_community,
                draft["name"], description=draft["description"], hidden=draft["hidden"],
                default_min_read_level=draft["default_min_read_level"],
                default_min_write_level=draft["default_min_write_level"],
                default_min_age=draft["default_min_age"],
                default_name_requirement=draft["default_name_requirement"], creator=actor,
            )
        return await lane.run(
            update_community,
            existing, name=draft["name"], description=draft["description"], hidden=draft["hidden"],
            default_min_read_level=draft["default_min_read_level"],
            default_min_write_level=draft["default_min_write_level"],
            default_min_age=draft["default_min_age"],
            default_name_requirement=draft["default_name_requirement"], changed_by=actor,
        )

    redraw_in_place, redraw_hint = await lane.run(_resolve_redraw_preference, actor)
    community = await edit_resource_draft(
        session, lane,
        title="Edit Community" if existing is not None else "Create Community",
        fields=_community_field_specs(), draft=draft, save=save, error_type=CommunityError,
        save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=redraw_in_place, redraw_hint=redraw_hint,
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if community is not None:
        verb = "Updated" if existing is not None else "Created Community"
        await session.write_line(f"{verb} {community.name!r}.")
    return community


async def _list_communities_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    communities = await lane.run(list_communities)
    selected = await pick_item(
        session, communities,
        name_of=lambda c: c.name,
        stable_id_of=lambda c: c.id,
        description_of=_community_description,
        title="Communities",
        empty_message="No Communities yet.",
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if selected is not None:
        await _community_detail_screen(session, lane, actor, selected)


def _community_description(community: Community) -> str:
    return "hidden" if community.hidden else "listed"


async def _community_detail_screen(session: Session, lane: DatabaseLane, actor: User, community: Community) -> None:
    """No "pending" equivalent here, unlike boards/areas -- a Community
    holds no content of its own (design doc §16)."""
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_community_detail(session, community, description_level, redraw_in_place, unicode_style, collapsed, header_color)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "e":
            await session.write_line("")
            updated = await _community_screen(session, lane, actor, existing=community)
            if updated is not None:
                community = updated
            await _draw_community_detail(session, community, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "d":
            await session.write_line("")
            deleted = await _delete_community_screen(session, lane, actor, community)
            if deleted:
                return
            await _draw_community_detail(session, community, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_community_detail(
    session: Session, community: Community, description_level: str, redraw_in_place: bool,
    unicode_style: bool,
    collapsed: bool,
    header_color: int | tuple[int, int, int] = HEADER_COLOR,
) -> None:
    await session.write_line(
        "\r\n" + screen_title(sanitize_text(community.name),
            breadcrumb=(session.node_display_name,), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            header_color=header_color, node_name_gradient=session.node_name_gradient)
    )
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
    options = _menu_row(
        [
            MenuEntry(label=menu_key("E", "dit"), brief="Change this Community's settings"),
            MenuEntry(label=menu_key("D", "elete"), brief="Permanently remove it"),
            MenuEntry(label=menu_key("B", "ack"), brief="Return to the list"),
        ],
        description_level,
        width=session.terminal_width,
        height=session.terminal_height,
    )
    await session.write_line(f"\r\n{options}")
    await session.write("Choice: ")


async def _delete_community_screen(session: Session, lane: DatabaseLane, actor: User, community: Community) -> bool:
    """Shows the blast radius before committing (design doc §16's exact
    confirmation wording): how many boards/channels/areas
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
            f"\r\nThis Community has {board_count} message board(s), {channel_count} chat channel(s), "
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
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_board_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "c":
            await session.write_line("")
            await _board_screen(session, lane, actor)
            await _draw_board_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "l":
            await session.write_line("")
            await _list_boards_screen(session, lane, actor, link_context=link_context)
            await _draw_board_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_board_menu(session: Session, description_level: str, redraw_in_place: bool, unicode_style: bool, collapsed: bool, header_color: int | tuple[int, int, int] = HEADER_COLOR) -> None:
    await session.write_line("\r\n" + screen_title("Message boards",
            breadcrumb=(session.node_display_name,), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color, node_name_gradient=session.node_name_gradient))
    await session.write_line(
        _menu_row(
            [
                MenuEntry(label=menu_key("C", "reate"), brief="Add a new message board"),
                MenuEntry(label=menu_key("L", "ist"), brief="Browse and edit boards"),
                MenuEntry(label=menu_key("B", "ack"), brief="Return to the Content menu"),
            ],
            description_level,
            width=session.terminal_width,
            height=session.terminal_height,
        )
    )
    await session.write("Choice: ")


def _board_field_specs(
    *, redraw_in_place: bool = False, unicode_style: bool = False, collapsed: bool = False
) -> list[FieldSpec]:
    """One shared field list drives both create and edit (design doc,
    dogfood feature request) -- see `_board_screen`. `redraw_in_place`/
    `unicode_style`/`collapsed`: threaded straight through to the
    Community/Category fields -- see `_community_field`'s own
    docstring."""
    return [
        FieldSpec(
            key="name", hotkey="n", menu_text=menu_key("N", "ame"), label="Name",
            render=lambda d: d.get("name") or "(blank)",
            prompt=text_field("name", required=True),
            brief="The board's display name",
            help="The board's display name, shown wherever it's listed. Must be non-blank.",
        ),
        FieldSpec(
            key="description", hotkey="d", menu_text=menu_key("D", "escription"), label="Description",
            render=lambda d: d.get("description") or "(none)",
            prompt=text_field("description"),
            brief="Shown when browsing the board",
            help="A short explanation of what this board is for, shown when browsing/selecting it.",
        ),
        FieldSpec(
            key="min_read_level", hotkey="r", menu_text=menu_key("R", "ead level"), label="Min read level",
            render=lambda d: _optional_int_label(d.get("min_read_level")),
            prompt=_optional_int_field("min_read_level", "Minimum read level"),
            brief="Level required to read it",
            help=(
                "The permission level a caller needs to read this board. 'none' inherits "
                "the parent Community's own default read level if it has one set, or falls "
                "back to no gate."
            ),
        ),
        FieldSpec(
            key="min_write_level", hotkey="w", menu_text=menu_key("W", "rite level"), label="Min write level",
            render=lambda d: _optional_int_label(d.get("min_write_level")),
            prompt=_optional_int_field("min_write_level", "Minimum write level"),
            brief="Level required to post",
            help=(
                "The permission level a caller needs to post here. 'none' inherits the "
                "parent Community's own default write level if it has one set, or falls "
                "back to no gate."
            ),
        ),
        FieldSpec(
            key="community_id", hotkey="u", menu_text=menu_key("U", "nity", prefix="Comm"), label="Community",
            render=lambda d: d.get("community_id_label") or "(none)",
            prompt=_community_field(
                redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed
            ),
            brief="Parent community, if any",
            help=(
                "The Community this board belongs to, if any -- a board inside a Community "
                "can inherit its default read/write/age/name-requirement settings instead of "
                "each needing its own. 'none' keeps this board outside every Community."
            ),
        ),
        FieldSpec(
            key="category_id", hotkey="c", menu_text=menu_key("C", "ategory"), label="Category",
            render=lambda d: d.get("category_id_label") or "(none)",
            prompt=_category_field(
                list_top_level=list_top_level_board_categories, list_subcategories=list_board_subcategories,
                title="Message board category", list_resources=list_boards, get_by_id=get_board_category_by_id,
                redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            ),
            brief="Where it's grouped in listings",
            help=(
                "Where this board is grouped when browsing listings -- purely organizational, "
                "has no effect on who can read or post."
            ),
        ),
        FieldSpec(
            key="pinned", hotkey="p", menu_text=menu_key("P", "inned"), label="Pinned",
            render=lambda d: "yes" if d.get("pinned") else "no",
            prompt=bool_field("pinned", "Pinned?"),
            brief="Shown at the top of listings",
            help="Shown at the top of board listings, above unpinned boards, regardless of sort order.",
        ),
        FieldSpec(
            key="moderated", hotkey="m", menu_text=menu_key("M", "oderated"), label="Moderated",
            render=lambda d: "yes" if d.get("moderated") else "no",
            prompt=bool_field("moderated", "Moderated (posts need approval)?"),
            brief="New posts need approval first",
            help="New posts need a moderator or SysOp to approve them before anyone else can see them.",
        ),
        FieldSpec(
            key="max_post_age_days", hotkey="x", menu_text=menu_key("X", " post age", prefix="Ma"),
            label="Max post age (days)",
            render=lambda d: _optional_int_label(d.get("max_post_age_days"), none_word="unlimited"),
            prompt=_optional_int_field("max_post_age_days", "Max post age in days"),
            brief="Auto-purge posts after N days",
            help="Posts older than this are automatically purged. 'unlimited' keeps every post indefinitely.",
        ),
        FieldSpec(
            key="min_age", hotkey="g", menu_text=menu_key("G", "e", prefix="Min a"), label="Min age",
            render=lambda d: _optional_int_label(d.get("min_age")),
            prompt=_min_age_field(),
            brief="Minimum caller age required",
            help=(
                "The minimum caller age required to read or post here, checked against a "
                "caller's own birthdate (Your profile › Name & details) even if they've "
                "chosen not to show it publicly. 'none' means no age gate."
            ),
        ),
        FieldSpec(
            key="name_requirement", hotkey="q", menu_text=menu_key("q", "uirement", prefix="Name re"),
            label="Name requirement",
            render=lambda d: _name_requirement_label(d.get("name_requirement")),
            prompt=_name_requirement_field(),
            step=_name_requirement_step(),
            help=_NAME_REQUIREMENT_HELP,
            brief="How posters must be identified",
        ),
    ]


async def _board_screen(
    session: Session, lane: DatabaseLane, actor: User, *, existing: Board | None = None
) -> Board | None:
    """Unified create/edit screen (design doc, dogfood feature request):
    creating a board is editing a fresh draft of defaults, then [S]ave
    inserts instead of updates -- every field addressable independently,
    in any order, and [B]ack discards the whole draft with nothing ever
    written to the database, directly answering the "no way to cancel
    mid-creation" complaint. Editing an existing board no longer walks
    the same linear step-by-step wizard creating one does -- both are
    now this one screen. See `netbbs.net.resource_editor`'s own module
    docstring for the general shape."""
    if existing is not None:
        draft = {
            "name": existing.name, "description": existing.description,
            "min_read_level": existing.min_read_level, "min_write_level": existing.min_write_level,
            "community_id": existing.community_id, "category_id": existing.category_id,
            "pinned": existing.pinned, "moderated": existing.moderated,
            "max_post_age_days": existing.max_post_age_days, "min_age": existing.min_age,
            "name_requirement": existing.name_requirement,
        }
        draft["community_id_label"] = (
            (await lane.run(get_community, existing.community_id)).name
            if existing.community_id is not None else None
        )
        draft["category_id_label"] = (
            (await lane.run(get_board_category_by_id, existing.category_id)).name
            if existing.category_id is not None else None
        )
    else:
        draft = {
            "name": "", "description": None, "min_read_level": 0, "min_write_level": 0,
            "community_id": None, "category_id": None, "pinned": False, "moderated": False,
            "max_post_age_days": None, "min_age": None, "name_requirement": None,
            "community_id_label": None, "category_id_label": None,
        }

    async def save(draft: dict) -> Board:
        if not draft["name"]:
            raise BoardError("name cannot be blank")
        if existing is None:
            return await lane.run(
                create_board,
                draft["name"], description=draft["description"], min_read_level=draft["min_read_level"],
                min_write_level=draft["min_write_level"], category_id=draft["category_id"],
                pinned=draft["pinned"], moderated=draft["moderated"],
                max_post_age_days=draft["max_post_age_days"], min_age=draft["min_age"],
                name_requirement=draft["name_requirement"], community_id=draft["community_id"], creator=actor,
            )
        return await lane.run(
            update_board,
            existing, name=draft["name"], description=draft["description"],
            min_read_level=draft["min_read_level"], min_write_level=draft["min_write_level"],
            category_id=draft["category_id"], pinned=draft["pinned"], moderated=draft["moderated"],
            max_post_age_days=draft["max_post_age_days"], min_age=draft["min_age"],
            name_requirement=draft["name_requirement"], community_id=draft["community_id"], changed_by=actor,
        )

    redraw_in_place, redraw_hint = await lane.run(_resolve_redraw_preference, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    board = await edit_resource_draft(
        session, lane,
        title="Edit message board" if existing is not None else "Create message board",
        fields=_board_field_specs(redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed),
        draft=draft, save=save, error_type=BoardError,
        save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=redraw_in_place, redraw_hint=redraw_hint,
        unicode_style=unicode_style,
        collapsed=collapsed,
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if board is not None:
        verb = "Updated" if existing is not None else "Created message board"
        await session.write_line(f"{verb} {board.name!r}.")
    return board


async def _list_boards_screen(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext | None = None
) -> None:
    boards = await lane.run(list_boards, order_by="alphabetical")
    selected = await pick_item(
        session, boards,
        name_of=lambda b: b.name,
        stable_id_of=lambda b: b.id,
        description_of=_board_description,
        title="Message boards",
        empty_message="No message boards yet.",
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
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
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    is_origin, has_incoming_offer, is_closed = await _draw_board_detail(
        session, lane, board, linked=linked, link_context=link_context, description_level=description_level,
        redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
    )
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "e":
            await session.write_line("")
            updated = await _board_screen(session, lane, actor, existing=board)
            if updated is not None:
                board = updated
            is_origin, has_incoming_offer, is_closed = await _draw_board_detail(
                session, lane, board, linked=linked, link_context=link_context,
                description_level=description_level, redraw_in_place=redraw_in_place,
                unicode_style=unicode_style, collapsed=collapsed,
            )
        elif choice == "d":
            await session.write_line("")
            deleted = await _delete_board_screen(session, lane, actor, board)
            if deleted:
                return
            is_origin, has_incoming_offer, is_closed = await _draw_board_detail(
                session, lane, board, linked=linked, link_context=link_context,
                description_level=description_level, redraw_in_place=redraw_in_place,
                unicode_style=unicode_style, collapsed=collapsed,
            )
        elif choice == "p":
            await session.write_line("")
            await _pending_posts_screen(session, lane, actor, board, link_context=link_context)
            is_origin, has_incoming_offer, is_closed = await _draw_board_detail(
                session, lane, board, linked=linked, link_context=link_context,
                description_level=description_level, redraw_in_place=redraw_in_place,
                unicode_style=unicode_style, collapsed=collapsed,
            )
        elif choice == "l" and link_context is not None and not linked:
            await session.write_line("")
            await _link_board_screen(session, lane, actor, board, link_context)
            linked = await lane.run(is_board_linked, board)
            is_origin, has_incoming_offer, is_closed = await _draw_board_detail(
                session, lane, board, linked=linked, link_context=link_context,
                description_level=description_level, redraw_in_place=redraw_in_place,
                unicode_style=unicode_style, collapsed=collapsed,
            )
        elif choice == "t" and link_context is not None and linked and is_origin and not is_closed:
            await session.write_line("")
            await _transfer_board_origin_screen(session, lane, board, link_context)
            is_origin, has_incoming_offer, is_closed = await _draw_board_detail(
                session, lane, board, linked=linked, link_context=link_context,
                description_level=description_level, redraw_in_place=redraw_in_place,
                unicode_style=unicode_style, collapsed=collapsed,
            )
        elif choice == "c" and link_context is not None and linked and is_origin and not is_closed:
            await session.write_line("")
            await _close_board_screen(session, lane, board, link_context)
            is_origin, has_incoming_offer, is_closed = await _draw_board_detail(
                session, lane, board, linked=linked, link_context=link_context,
                description_level=description_level, redraw_in_place=redraw_in_place,
                unicode_style=unicode_style, collapsed=collapsed,
            )
        elif choice == "a" and has_incoming_offer:
            await session.write_line("")
            await _accept_board_origin_transfer_screen(session, lane, board, link_context)
            is_origin, has_incoming_offer, is_closed = await _draw_board_detail(
                session, lane, board, linked=linked, link_context=link_context,
                description_level=description_level, redraw_in_place=redraw_in_place,
                unicode_style=unicode_style, collapsed=collapsed,
            )
        else:
            await session.write(reject_unhandled_key(choice))


async def _link_board_screen(
    session: Session, lane: DatabaseLane, actor: User, board: Board, link_context: LinkContext
) -> None:
    """
    `[L]ink this board` (design doc): puts `board` into
    Link scope via a signed `board_genesis` event referencing its
    existing `board_id` -- never a fresh one (the "promote an
    existing local board" case, the normal one, not a special one).

    The six `default_*` cascading-scalar-default fields
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
    await session.write_line(
        colored("\r\nLink this message board", fg_color=await lane.run(effective_header_color_256), bold=True)
    )
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
    if await prompt_yes_no(session, "Is this a fork of an existing Linked message board?", default=False):
        candidates = await lane.run(_linked_boards_excluding, board.id)
        chosen = await pick_item(
            session, candidates,
            name_of=lambda b: b.name, stable_id_of=lambda b: b.id,
            title="Fork of which message board?", empty_message="No other Linked message boards to fork from.",
            redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
            unicode_style=await lane.run(unicode_style_enabled, actor),
            collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
            accent_color=await lane.run(effective_accent_color_256),
            header_color=await lane.run(effective_header_color_256),
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
        await session.write_line(colored(f"Could not Link message board: {exc}", fg_color=MUTED_COLOR))
        return

    link_context.link_node.boards[board.board_id] = genesis
    link_context.link_node.known_event_ids.add(genesis.content_id)
    link_context.link_node.events[genesis.content_id] = genesis.to_dict()

    await session.write_line(f"Linked {board.name!r} -- it will be pushed to peers on the next sync pass.")


def _linked_boards_excluding(db: Database, exclude_board_id: int) -> list[Board]:
    """Every currently-Linked board except `exclude_board_id` (design
    doc §13, issue #53) -- the fork-source candidate list for
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
    `[T]ransfer origin` (design doc §13, issue #53): the
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
    await session.write_line(
        colored("\r\nTransfer message board origin", fg_color=await lane.run(effective_header_color_256), bold=True)
    )
    if not peers:
        await session.write_line(colored("No known peers to transfer this message board to.", fg_color=MUTED_COLOR))
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


async def _close_board_screen(session: Session, lane: DatabaseLane, board: Board, link_context: LinkContext) -> None:
    """
    `[C]lose board` (design doc §9.5, issue #88): the current origin's
    terminal board_closure, stopping new posts network-wide. Only
    reachable when `_draw_board_detail` already confirmed this node is
    the current origin and `board` isn't already closed, re-checked here
    too like every other admin mutation in this file, since closure
    can't be undone in this slice -- confirmed explicitly before acting.
    """
    await session.write_line(
        colored(
            "\r\nClosing a message board is permanent in this slice -- no new posts, network-wide, "
            "and this cannot be reversed here.",
            fg_color=MUTED_COLOR,
        )
    )
    await session.write("Optional reason (blank for none): ")
    reason = (await session.read_line()).strip() or None
    if not await prompt_yes_no(session, f"Close {board.name!r}?", default=False):
        await session.write_line("Cancelled.")
        return

    try:
        closure = await lane.run(
            close_board_if_linked, board, node_identity=link_context.node_identity, reason=reason,
        )
    except LinkBoardsError as exc:
        await session.write_line(colored(f"Could not close message board: {exc}", fg_color=MUTED_COLOR))
        return

    link_context.link_node.board_closures[board.board_id] = closure
    link_context.link_node.board_lifecycle_head[board.board_id] = closure.content_id
    link_context.link_node.known_event_ids.add(closure.content_id)
    link_context.link_node.events[closure.content_id] = closure.to_dict()

    await session.write_line(f"{board.name!r} closed -- it will be pushed to peers on the next sync pass.")


async def _accept_board_origin_transfer_screen(
    session: Session, lane: DatabaseLane, board: Board, link_context: LinkContext
) -> None:
    """
    `[A]ccept transfer` (design doc §13, issue #53): the
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
        await session.write_line(colored("\r\nNo pending incoming offer for this message board.", fg_color=MUTED_COLOR))
        return

    old_origin = offer.payload.get("old_origin_fingerprint")
    await session.write_line(
        colored("\r\nAccept message board origin", fg_color=await lane.run(effective_header_color_256), bold=True)
    )
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
    description_level: str = "off",
    redraw_in_place: bool = False,
    unicode_style: bool = False,
    collapsed: bool = False,
) -> tuple[bool, bool, bool]:
    """
    Returns `(is_origin, has_incoming_offer, is_closed)` (design doc
    §13/§9.5, issues #53/#88) -- whether this node is currently
    `board`'s own origin (gates `[T]ransfer origin`/`[C]lose`), whether a
    pending incoming origin-transfer offer names this node as the
    proposed new origin (gates `[A]ccept transfer`), and whether `board`
    has already been closed (suppresses both `[T]ransfer origin` and
    `[C]lose` -- closure is terminal, design doc §9.5).
    `_board_detail_screen`'s own dispatch loop needs all three every time
    it redraws, so returning them here avoids a second, separately-timed
    recomputation immediately after.
    """
    await session.write_line(
        "\r\n" + screen_title(sanitize_text(board.name),
            breadcrumb=(session.node_display_name,), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            header_color=await lane.run(effective_header_color_256), node_name_gradient=session.node_name_gradient)
    )
    await session.write_line(f"Description: {sanitize_text(board.description) if board.description else '(none)'}")
    # Dogfood follow-up: nothing on this screen (or the board-list picker)
    # ever showed how many posts actually exist or when the last one was
    # made -- a SysOp trying to spot a dead board versus an active one had
    # no way to tell without leaving admin and browsing it as an ordinary
    # reader.
    post_count, last_post_at = await lane.run(count_visible_posts, board)
    if last_post_at is None:
        activity = "no posts yet"
    else:
        display_format, display_timezone = await lane.run(resolve_display_preferences)
        activity = f"last post {format_for_display(last_post_at, override_format=display_format, override_timezone=display_timezone)}"
    await session.write_line(f"Posts: {post_count} ({activity})")
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
    is_closed = False
    if link_context is not None:
        await session.write_line(f"Linked: {'yes' if linked else 'no'}")
        if linked:
            is_closed = await lane.run(is_board_closed, board)
            if is_closed:
                await session.write_line(colored("Closed: yes -- no longer accepts new posts", fg_color=MUTED_COLOR))
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
    options = [
        MenuEntry(label=menu_key("E", "dit"), brief="Change this board's settings"),
        MenuEntry(label=menu_key("D", "elete"), brief="Permanently remove this board"),
        MenuEntry(label=menu_key("P", "ending posts"), brief="Review posts awaiting approval"),
    ]
    if link_context is not None and not linked:
        options.append(MenuEntry(label=menu_key("L", "ink this message board"), brief="Share it via NetBBS Link"))
    if (
        link_context is not None and linked and is_origin and not is_closed
        and board.board_id not in link_context.link_node.pending_origin_transfers
    ):
        options.append(MenuEntry(label=menu_key("T", "ransfer origin"), brief="Hand off origin to a peer"))
        options.append(MenuEntry(label=menu_key("C", "lose message board"), brief="Stop accepting new posts"))
    if has_incoming_offer:
        options.append(MenuEntry(label=menu_key("A", "ccept transfer"), brief="Accept incoming origin transfer"))
    options.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to the list"))
    await session.write_line(
        "\r\n"
        + _menu_row(options, description_level, width=session.terminal_width, height=session.terminal_height)
    )
    await session.write("Choice: ")
    return is_origin, has_incoming_offer, is_closed


async def _delete_board_screen(session: Session, lane: DatabaseLane, actor: User, board: Board) -> bool:
    await session.write_line(
        colored(
            "\r\nThis permanently deletes the message board, all of its posts, and any "
            "moderator grants scoped to it. This cannot be undone.",
            fg_color=MUTED_COLOR,
        )
    )
    await session.write(f"Type the message board name {board.name!r} to confirm, or anything else to cancel: ")
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
            redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
            unicode_style=await lane.run(unicode_style_enabled, actor),
            collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
            accent_color=await lane.run(effective_accent_color_256),
            header_color=await lane.run(effective_header_color_256),
        )
        if selected is None:
            return
        await _post_action_screen(session, lane, actor, selected, board, link_context=link_context)


async def _draw_post_action(
    session: Session, post: Post, description_level: str, redraw_in_place: bool,
    unicode_style: bool,
    collapsed: bool,
    header_color: int | tuple[int, int, int] = HEADER_COLOR,
) -> None:
    await session.write_line(
        "\r\n" + screen_title(sanitize_text(post.subject),
            breadcrumb=(session.node_display_name,), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            header_color=header_color, node_name_gradient=session.node_name_gradient)
    )
    await session.write_line(f"By: {sanitize_text(post.author_label)}")
    await session.write_line(reflow(sanitize_text(post.body, allow_newlines=True), width=session.terminal_width))
    options = _menu_row(
        [
            MenuEntry(label=menu_key("A", "pprove"), brief="Publish this pending post"),
            MenuEntry(label=menu_key("R", "eject"), brief="Delete this pending post"),
            MenuEntry(label=menu_key("P", "in toggle"), brief="Toggle showing at the top"),
            MenuEntry(label=menu_key("X", "empt toggle"), brief="Toggle exempt from auto-purge"),
            MenuEntry(label=menu_key("B", "ack"), brief="Return to the pending list"),
        ],
        description_level,
        width=session.terminal_width,
        height=session.terminal_height,
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
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_post_action(session, post, description_level, redraw_in_place, unicode_style, collapsed, header_color)
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
                await _draw_post_action(session, post, description_level, redraw_in_place, unicode_style, collapsed, header_color)
                continue
            await session.write_line("Rejected.")
            return
        elif choice == "p":
            await session.write_line("")
            post = await lane.run(set_post_pinned, post, not post.pinned, changed_by=actor)
            await _draw_post_action(session, post, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "x":
            await session.write_line("")
            post = await lane.run(set_post_exempt, post, not post.exempt_from_expiry, changed_by=actor)
            await _draw_post_action(session, post, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


# -- file areas ----------------------------------------------------------


async def _area_menu(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext | None = None
) -> None:
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_area_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "c":
            await session.write_line("")
            await _area_screen(session, lane, actor)
            await _draw_area_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "l":
            await session.write_line("")
            await _list_areas_screen(session, lane, actor, link_context=link_context)
            await _draw_area_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "g":
            await session.write_line("")
            await _gc_screen(session, lane)
            await _draw_area_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_area_menu(session: Session, description_level: str, redraw_in_place: bool, unicode_style: bool, collapsed: bool, header_color: int | tuple[int, int, int] = HEADER_COLOR) -> None:
    await session.write_line("\r\n" + screen_title("File areas",
            breadcrumb=(session.node_display_name,), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color, node_name_gradient=session.node_name_gradient))
    await session.write_line(
        menu_grid(
            [(
                "",
                [
                    MenuEntry(label=menu_key("C", "reate"), brief="Add a new file area"),
                    MenuEntry(label=menu_key("L", "ist"), brief="Browse and edit file areas"),
                    MenuEntry(
                        label=menu_key("G", "C storage"),
                        brief="Reclaim space from orphaned files",
                        detailed=(
                            "Garbage-collect (GC) uploaded file storage: reclaim disk space still "
                            "held by blobs no file entry references anymore, e.g. after a delete."
                        ),
                    ),
                    MenuEntry(label=menu_key("B", "ack"), brief="Return to the Content menu"),
                ],
            )],
            width=session.terminal_width,
            height=session.terminal_height,
            description_level=description_level,
        )
    )
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


def _area_field_specs(
    *, redraw_in_place: bool = False, unicode_style: bool = False, collapsed: bool = False
) -> list[FieldSpec]:
    """One shared field list drives both create and edit (design doc,
    dogfood feature request) -- see `_area_screen`. Identical shape to
    `_board_field_specs`, just "file" in place of "post" throughout."""
    return [
        FieldSpec(
            key="name", hotkey="n", menu_text=menu_key("N", "ame"), label="Name",
            render=lambda d: d.get("name") or "(blank)",
            prompt=text_field("name", required=True),
            brief="The area's display name",
            help="The file area's display name, shown wherever it's listed. Must be non-blank.",
        ),
        FieldSpec(
            key="description", hotkey="d", menu_text=menu_key("D", "escription"), label="Description",
            render=lambda d: d.get("description") or "(none)",
            prompt=text_field("description"),
            brief="Shown when browsing the area",
            help="A short explanation of what this file area is for, shown when browsing/selecting it.",
        ),
        FieldSpec(
            key="min_read_level", hotkey="r", menu_text=menu_key("R", "ead level"), label="Min read level",
            render=lambda d: _optional_int_label(d.get("min_read_level")),
            prompt=_optional_int_field("min_read_level", "Minimum read level"),
            brief="Level required to browse it",
            help=(
                "The permission level a caller needs to browse/download from this area. "
                "'none' inherits the parent Community's own default read level if it has "
                "one set, or falls back to no gate."
            ),
        ),
        FieldSpec(
            key="min_write_level", hotkey="w", menu_text=menu_key("W", "rite level"), label="Min write level",
            render=lambda d: _optional_int_label(d.get("min_write_level")),
            prompt=_optional_int_field("min_write_level", "Minimum write level"),
            brief="Level required to upload",
            help=(
                "The permission level a caller needs to upload here. 'none' inherits the "
                "parent Community's own default write level if it has one set, or falls "
                "back to no gate."
            ),
        ),
        FieldSpec(
            key="community_id", hotkey="u", menu_text=menu_key("U", "nity", prefix="Comm"), label="Community",
            render=lambda d: d.get("community_id_label") or "(none)",
            prompt=_community_field(
                redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed
            ),
            brief="Parent community, if any",
            help=(
                "The Community this file area belongs to, if any -- an area inside a "
                "Community can inherit its default read/write/age/name-requirement settings "
                "instead of each needing its own. 'none' keeps it outside every Community."
            ),
        ),
        FieldSpec(
            key="category_id", hotkey="c", menu_text=menu_key("C", "ategory"), label="Category",
            render=lambda d: d.get("category_id_label") or "(none)",
            prompt=_category_field(
                list_top_level=list_top_level_file_categories, list_subcategories=list_file_subcategories,
                title="File-area category", list_resources=list_file_areas, get_by_id=get_file_area_category_by_id,
                redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            ),
            brief="Where it's grouped in listings",
            help=(
                "Where this file area is grouped when browsing listings -- purely "
                "organizational, has no effect on who can browse or upload."
            ),
        ),
        FieldSpec(
            key="pinned", hotkey="p", menu_text=menu_key("P", "inned"), label="Pinned",
            render=lambda d: "yes" if d.get("pinned") else "no",
            prompt=bool_field("pinned", "Pinned?"),
            brief="Shown at the top of listings",
            help="Shown at the top of file-area listings, above unpinned areas, regardless of sort order.",
        ),
        FieldSpec(
            key="moderated", hotkey="m", menu_text=menu_key("M", "oderated"), label="Moderated",
            render=lambda d: "yes" if d.get("moderated") else "no",
            prompt=bool_field("moderated", "Moderated (uploads need approval)?"),
            brief="New uploads need approval first",
            help="New uploads need a moderator or SysOp to approve them before anyone else can download them.",
        ),
        FieldSpec(
            key="max_file_age_days", hotkey="x", menu_text=menu_key("X", " file age", prefix="Ma"),
            label="Max file age (days)",
            render=lambda d: _optional_int_label(d.get("max_file_age_days"), none_word="unlimited"),
            prompt=_optional_int_field("max_file_age_days", "Max file age in days"),
            brief="Auto-purge files after N days",
            help="Files older than this are automatically purged. 'unlimited' keeps every file indefinitely.",
        ),
        FieldSpec(
            key="min_age", hotkey="g", menu_text=menu_key("G", "e", prefix="Min a"), label="Min age",
            render=lambda d: _optional_int_label(d.get("min_age")),
            prompt=_min_age_field(),
            brief="Minimum caller age required",
            help=(
                "The minimum caller age required to browse or upload here, checked against "
                "a caller's own birthdate (Your profile › Name & details) even if they've "
                "chosen not to show it publicly. 'none' means no age gate."
            ),
        ),
        FieldSpec(
            key="name_requirement", hotkey="q", menu_text=menu_key("q", "uirement", prefix="Name re"),
            label="Name requirement",
            render=lambda d: _name_requirement_label(d.get("name_requirement")),
            prompt=_name_requirement_field(),
            step=_name_requirement_step(),
            help=_NAME_REQUIREMENT_HELP,
            brief="How uploaders must be identified",
        ),
    ]


async def _area_screen(
    session: Session, lane: DatabaseLane, actor: User, *, existing: FileArea | None = None
) -> FileArea | None:
    """Unified create/edit screen -- see `_board_screen`'s own
    docstring for the general shape and reasoning, identical here."""
    if existing is not None:
        draft = {
            "name": existing.name, "description": existing.description,
            "min_read_level": existing.min_read_level, "min_write_level": existing.min_write_level,
            "community_id": existing.community_id, "category_id": existing.category_id,
            "pinned": existing.pinned, "moderated": existing.moderated,
            "max_file_age_days": existing.max_file_age_days, "min_age": existing.min_age,
            "name_requirement": existing.name_requirement,
        }
        draft["community_id_label"] = (
            (await lane.run(get_community, existing.community_id)).name
            if existing.community_id is not None else None
        )
        draft["category_id_label"] = (
            (await lane.run(get_file_area_category_by_id, existing.category_id)).name
            if existing.category_id is not None else None
        )
    else:
        draft = {
            "name": "", "description": None, "min_read_level": 0, "min_write_level": 0,
            "community_id": None, "category_id": None, "pinned": False, "moderated": False,
            "max_file_age_days": None, "min_age": None, "name_requirement": None,
            "community_id_label": None, "category_id_label": None,
        }

    async def save(draft: dict) -> FileArea:
        if not draft["name"]:
            raise FileAreaError("name cannot be blank")
        if existing is None:
            return await lane.run(
                create_file_area,
                draft["name"], description=draft["description"], min_read_level=draft["min_read_level"],
                min_write_level=draft["min_write_level"], category_id=draft["category_id"],
                pinned=draft["pinned"], moderated=draft["moderated"],
                max_file_age_days=draft["max_file_age_days"], min_age=draft["min_age"],
                name_requirement=draft["name_requirement"], community_id=draft["community_id"], creator=actor,
            )
        return await lane.run(
            update_file_area,
            existing, name=draft["name"], description=draft["description"],
            min_read_level=draft["min_read_level"], min_write_level=draft["min_write_level"],
            category_id=draft["category_id"], pinned=draft["pinned"], moderated=draft["moderated"],
            max_file_age_days=draft["max_file_age_days"], min_age=draft["min_age"],
            name_requirement=draft["name_requirement"], community_id=draft["community_id"], changed_by=actor,
        )

    redraw_in_place, redraw_hint = await lane.run(_resolve_redraw_preference, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    area = await edit_resource_draft(
        session, lane,
        title="Edit file area" if existing is not None else "Create file area",
        fields=_area_field_specs(redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed),
        draft=draft, save=save, error_type=FileAreaError,
        save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=redraw_in_place, redraw_hint=redraw_hint,
        unicode_style=unicode_style,
        collapsed=collapsed,
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if area is not None:
        verb = "Updated" if existing is not None else "Created file area"
        await session.write_line(f"{verb} {area.name!r}.")
    return area


async def _list_areas_screen(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext | None = None
) -> None:
    areas = await lane.run(list_file_areas, order_by="alphabetical")
    selected = await pick_item(
        session, areas,
        name_of=lambda a: a.name,
        stable_id_of=lambda a: a.id,
        description_of=_area_description,
        title="File areas",
        empty_message="No file areas yet.",
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if selected is not None:
        await _area_detail_screen(session, lane, actor, selected, link_context=link_context)


def _area_description(area: FileArea) -> str:
    status = "moderated" if area.moderated else "open"
    read_level = area.min_read_level if area.min_read_level is not None else "inherit"
    write_level = area.min_write_level if area.min_write_level is not None else "inherit"
    return f"read {read_level}/write {write_level}, {status}"


async def _area_detail_screen(
    session: Session, lane: DatabaseLane, actor: User, area: FileArea, *, link_context: LinkContext | None = None
) -> None:
    linked = await lane.run(is_area_linked, area) if link_context is not None else False
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    await _draw_area_detail(session, lane, area, linked=linked, link_context=link_context, description_level=description_level, redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "e":
            await session.write_line("")
            updated = await _area_screen(session, lane, actor, existing=area)
            if updated is not None:
                area = updated
            await _draw_area_detail(session, lane, area, linked=linked, link_context=link_context, description_level=description_level, redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed)
        elif choice == "d":
            await session.write_line("")
            deleted = await _delete_area_screen(session, lane, actor, area)
            if deleted:
                return
            await _draw_area_detail(session, lane, area, linked=linked, link_context=link_context, description_level=description_level, redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed)
        elif choice == "p":
            await session.write_line("")
            await _pending_files_screen(session, lane, actor, area)
            await _draw_area_detail(session, lane, area, linked=linked, link_context=link_context, description_level=description_level, redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed)
        elif choice == "l" and link_context is not None and not linked:
            await session.write_line("")
            await _link_area_screen(session, lane, area, link_context)
            linked = await lane.run(is_area_linked, area)
            await _draw_area_detail(session, lane, area, linked=linked, link_context=link_context, description_level=description_level, redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_area_detail(
    session: Session,
    lane: DatabaseLane,
    area: FileArea,
    *,
    linked: bool = False,
    link_context: LinkContext | None = None,
    description_level: str = "off",
    redraw_in_place: bool = False,
    unicode_style: bool = False,
    collapsed: bool = False,
) -> None:
    await session.write_line(
        "\r\n" + screen_title(sanitize_text(area.name),
            breadcrumb=(session.node_display_name,), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            header_color=await lane.run(effective_header_color_256), node_name_gradient=session.node_name_gradient)
    )
    await session.write_line(f"Description: {sanitize_text(area.description) if area.description else '(none)'}")
    file_count, last_file_at = await lane.run(count_visible_files, area)
    if last_file_at is None:
        activity = "no files yet"
    else:
        display_format, display_timezone = await lane.run(resolve_display_preferences)
        activity = f"last upload {format_for_display(last_file_at, override_format=display_format, override_timezone=display_timezone)}"
    await session.write_line(f"Files: {file_count} ({activity})")
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
    if link_context is not None:
        await session.write_line(f"Linked: {'yes' if linked else 'no'}")
    options = [
        MenuEntry(label=menu_key("E", "dit"), brief="Change this area's settings"),
        MenuEntry(label=menu_key("D", "elete"), brief="Permanently remove this area"),
        MenuEntry(label=menu_key("P", "ending files"), brief="Review uploads awaiting approval"),
    ]
    if link_context is not None and not linked:
        options.append(MenuEntry(label=menu_key("L", "ink this file area"), brief="Share it via NetBBS Link"))
    options.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to the list"))
    await session.write_line(
        "\r\n"
        + _menu_row(options, description_level, width=session.terminal_width, height=session.terminal_height)
    )
    await session.write("Choice: ")


async def _link_area_screen(session: Session, lane: DatabaseLane, area: FileArea, link_context: LinkContext) -> None:
    """
    `[L]ink this file area` (design doc §11, issue #89 -- previously
    defined by `netbbs.link.files.link_file_area` but, unlike `link_
    board`, never actually reachable from any live UI action; this is
    that missing call site). Mirrors `_link_board_screen` exactly, minus
    the fields `FileArea` has no equivalent of (no `forked_from` --
    file-area origin succession is not built, design doc §11) and with
    `max_file_age_days` in place of `max_post_age_days`.
    """
    await session.write_line(
        colored("\r\nLink this file area", fg_color=await lane.run(effective_header_color_256), bold=True)
    )
    default_min_read_level, ok = await _prompt_optional_int(
        session, "Recommended minimum read level", current=area.min_read_level
    )
    if not ok:
        return
    default_min_write_level, ok = await _prompt_optional_int(
        session, "Recommended minimum write level", current=area.min_write_level
    )
    if not ok:
        return
    await session.write(f"Recommend moderated? [{'y' if area.moderated else 'N'}/blank=no recommendation]: ")
    moderated_answer = (await session.read_line()).strip().lower()
    default_moderated = moderated_answer == "y" if moderated_answer in ("y", "n") else None
    current_age = area.max_file_age_days if area.max_file_age_days is not None else "unlimited"
    await session.write(f"Recommended max file age in days [{current_age}] (blank = no recommendation): ")
    max_age_raw = (await session.read_line()).strip()
    default_max_file_age_days = None
    if max_age_raw:
        try:
            default_max_file_age_days = int(max_age_raw)
        except ValueError:
            await session.write_line(colored("Not a number -- cancelled.", fg_color=MUTED_COLOR))
            return
    default_min_age, ok = await _prompt_min_age(session, current=area.min_age)
    if not ok:
        return
    default_name_requirement, ok = await _prompt_name_requirement(session, current=area.name_requirement)
    if not ok:
        return

    try:
        genesis = await lane.run(
            link_file_area,
            area,
            node_identity=link_context.node_identity,
            default_min_read_level=default_min_read_level,
            default_min_write_level=default_min_write_level,
            default_moderated=default_moderated,
            default_max_file_age_days=default_max_file_age_days,
            default_min_age=default_min_age,
            default_name_requirement=default_name_requirement,
        )
    except LinkFilesError as exc:
        await session.write_line(colored(f"Could not Link file area: {exc}", fg_color=MUTED_COLOR))
        return

    link_context.link_node.file_areas[area.area_id] = genesis
    link_context.link_node.known_event_ids.add(genesis.content_id)
    link_context.link_node.events[genesis.content_id] = genesis.to_dict()

    await session.write_line(f"Linked {area.name!r} -- it will be pushed to peers on the next sync pass.")




async def _delete_area_screen(session: Session, lane: DatabaseLane, actor: User, area: FileArea) -> bool:
    await session.write_line(
        colored(
            "\r\nThis permanently deletes the file area, all of its files, and any "
            "moderator grants scoped to it. This cannot be undone.",
            fg_color=MUTED_COLOR,
        )
    )
    await session.write(f"Type the file area name {area.name!r} to confirm, or anything else to cancel: ")
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
            redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
            unicode_style=await lane.run(unicode_style_enabled, actor),
            collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
            accent_color=await lane.run(effective_accent_color_256),
            header_color=await lane.run(effective_header_color_256),
        )
        if selected is None:
            return
        await _file_action_screen(session, lane, actor, selected)


async def _draw_file_action(
    session: Session, entry: FileEntry, description_level: str, redraw_in_place: bool,
    unicode_style: bool,
    collapsed: bool,
    header_color: int | tuple[int, int, int] = HEADER_COLOR,
) -> None:
    await session.write_line(
        "\r\n" + screen_title(sanitize_text(entry.filename),
            breadcrumb=(session.node_display_name,), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            header_color=header_color, node_name_gradient=session.node_name_gradient)
    )
    await session.write_line(f"By: {sanitize_text(entry.uploader_label)}")
    if entry.description:
        await session.write_line(sanitize_text(entry.description))
    await session.write_line(f"Size: {entry.size_bytes} bytes")
    options = _menu_row(
        [
            MenuEntry(label=menu_key("A", "pprove"), brief="Publish this pending file"),
            MenuEntry(label=menu_key("R", "eject"), brief="Delete this pending file"),
            MenuEntry(label=menu_key("P", "in toggle"), brief="Toggle showing at the top"),
            MenuEntry(label=menu_key("X", "empt toggle"), brief="Toggle exempt from auto-purge"),
            MenuEntry(label=menu_key("B", "ack"), brief="Return to the pending list"),
        ],
        description_level,
        width=session.terminal_width,
        height=session.terminal_height,
    )
    await session.write_line(f"\r\n{options}")
    await session.write("Choice: ")


async def _file_action_screen(session: Session, lane: DatabaseLane, actor: User, entry: FileEntry) -> None:
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_file_action(session, entry, description_level, redraw_in_place, unicode_style, collapsed, header_color)
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
            await _draw_file_action(session, entry, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "x":
            await session.write_line("")
            entry = await lane.run(set_file_exempt, entry, not entry.exempt_from_expiry, changed_by=actor)
            await _draw_file_action(session, entry, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


# -- doors (issue #172) ------------------------------------------------------
#
# Deliberately flat -- no categories, no age/name-requirement gates, no
# Community-inheritance for its one level field (see netbbs.doors.
# registry's own docstring for why: doors have neither a stated Link
# future nor a read/write split, so this section is noticeably shorter
# than boards'/areas' own.


async def _door_menu(session: Session, lane: DatabaseLane, actor: User) -> None:
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_door_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "c":
            await session.write_line("")
            await _door_screen(session, lane, actor)
            await _draw_door_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "g":
            await session.write_line("")
            await _door_gallery_screen(session, lane, actor, description_level, redraw_in_place, unicode_style, collapsed)
            await _draw_door_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "f":
            await session.write_line("")
            await _door_filesystem_screen(session, lane, actor, description_level, redraw_in_place, unicode_style, collapsed)
            await _draw_door_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "l":
            await session.write_line("")
            await _list_doors_screen(session, lane, actor)
            await _draw_door_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_door_menu(session: Session, description_level: str, redraw_in_place: bool, unicode_style: bool, collapsed: bool, header_color: int | tuple[int, int, int] = HEADER_COLOR) -> None:
    await session.write_line("\r\n" + screen_title("Doors",
            breadcrumb=(session.node_display_name,), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color, node_name_gradient=session.node_name_gradient))
    await session.write_line(
        menu_grid(
            [(
                "",
                [
                    MenuEntry(label=menu_key("C", "reate"), brief="Register a new door"),
                    MenuEntry(label=menu_key("G", "allery"), brief="Register one of NetBBS's own doors"),
                    MenuEntry(label=menu_key("F", "rom disk"), brief="Register your own script from this node"),
                    MenuEntry(label=menu_key("L", "ist"), brief="Browse and edit doors"),
                    MenuEntry(label=menu_key("B", "ack"), brief="Return to the Content menu"),
                ],
            )],
            width=session.terminal_width,
            height=session.terminal_height,
            description_level=description_level,
        )
    )
    await session.write("Choice: ")


def _door_field_specs() -> list[FieldSpec]:
    """One shared field list drives both create and edit, same "single
    source of truth" precedent as `_area_field_specs`/`_board_field_
    specs`. `args` is edited as one space-separated line and split with
    `shlex.split` -- still always launched via `create_subprocess_exec`'s
    argv-list form (see `netbbs.doors.runtime`), never a shell; this is
    just a friendlier way for a SysOp to type several arguments on one
    line than re-entering the field once per argument."""
    return [
        FieldSpec(
            key="name", hotkey="n", menu_text=menu_key("N", "ame"), label="Name",
            render=lambda d: d.get("name") or "(blank)",
            prompt=text_field("name", required=True),
            brief="The door's display name",
            help="The door's display name, shown wherever it's listed. Must be non-blank.",
        ),
        FieldSpec(
            key="description", hotkey="d", menu_text=menu_key("D", "escription"), label="Description",
            render=lambda d: d.get("description") or "(none)",
            prompt=text_field("description"),
            brief="Shown when browsing doors",
            help="A short explanation of what this door is, shown when browsing/selecting it.",
        ),
        FieldSpec(
            key="executable_path", hotkey="e", menu_text=menu_key("E", "xecutable path"), label="Executable path",
            render=lambda d: d.get("executable_path") or "(blank)",
            prompt=text_field("executable_path", required=True),
            brief="Path to the program to launch",
            help=(
                "The full filesystem path to the door's own executable. Runs as the same OS "
                "user as NetBBS itself, with CPU/memory/process-count limits enforced "
                "regardless of what the door does -- see the operator guide for the full "
                "sandbox model."
            ),
        ),
        FieldSpec(
            key="args_line", hotkey="a", menu_text=menu_key("A", "rgs"), label="Arguments",
            render=lambda d: d.get("args_line") or "(none)",
            prompt=text_field("args_line"),
            brief="Fixed command-line arguments",
            help="Fixed arguments always passed to the door, space-separated on one line. Leave blank for none.",
        ),
        FieldSpec(
            key="min_play_level", hotkey="p", menu_text=menu_key("P", "lay level"), label="Min play level",
            render=lambda d: str(d.get("min_play_level")),
            prompt=_int_field("min_play_level", "Minimum play level"),
            brief="Level required to launch it",
            help="The permission level a caller needs to launch/play this door.",
        ),
        FieldSpec(
            key="community_id", hotkey="u", menu_text=menu_key("U", "nity", prefix="Comm"), label="Community",
            render=lambda d: d.get("community_id_label") or "(none)",
            prompt=_community_field(),
            brief="Where it's offered from",
            help="The Community this door is offered from, if any. 'none' keeps it outside every Community.",
        ),
        FieldSpec(
            key="pinned", hotkey="i", menu_text=menu_key("i", "nned", prefix="P"), label="Pinned",
            render=lambda d: "yes" if d.get("pinned") else "no",
            prompt=bool_field("pinned", "Pinned?"),
            brief="Shown at the top of listings",
            help="Shown at the top of door listings, above unpinned doors, regardless of sort order.",
        ),
    ]


async def _door_screen(
    session: Session, lane: DatabaseLane, actor: User, *,
    existing: Door | None = None, prefill: dict | None = None,
) -> Door | None:
    """Unified create/edit screen -- see `_area_screen`'s own docstring
    for the general shape and reasoning, identical here.

    `prefill` (issue #172's door-gallery follow-up): starting field
    values for a brand-new door, merged over the ordinary blank draft --
    still the exact same editor, save validation, and `create_door` call
    as manual `[C]reate`, just not starting from an all-blank form. Only
    meaningful when `existing` is `None`; silently ignored otherwise --
    editing an existing door already has real values to start from, and
    a caller select-then-edit flow (`_door_gallery_screen`) only ever
    reaches this with `existing=None` in the first place."""
    if existing is not None:
        draft = {
            "name": existing.name, "description": existing.description,
            "executable_path": existing.executable_path, "args_line": " ".join(existing.args),
            "min_play_level": existing.min_play_level, "community_id": existing.community_id,
            "pinned": existing.pinned,
        }
        draft["community_id_label"] = (
            (await lane.run(get_community, existing.community_id)).name
            if existing.community_id is not None else None
        )
    else:
        draft = {
            "name": "", "description": None, "executable_path": "", "args_line": "",
            "min_play_level": 0, "community_id": None, "pinned": False, "community_id_label": None,
        }
        if prefill is not None:
            draft.update(prefill)

    async def save(draft: dict) -> Door:
        if not draft["name"]:
            raise DoorError("name cannot be blank")
        if not draft["executable_path"]:
            raise DoorError("executable path cannot be blank")
        try:
            args = tuple(shlex.split(draft["args_line"]))
        except ValueError as exc:
            raise DoorError(f"could not parse arguments -- {exc}") from exc
        if existing is None:
            return await lane.run(
                create_door,
                draft["name"], draft["executable_path"], description=draft["description"], args=args,
                min_play_level=draft["min_play_level"], pinned=draft["pinned"],
                community_id=draft["community_id"], creator=actor,
            )
        return await lane.run(
            update_door,
            existing, name=draft["name"], description=draft["description"],
            executable_path=draft["executable_path"], args=args, min_play_level=draft["min_play_level"],
            pinned=draft["pinned"], community_id=draft["community_id"], changed_by=actor,
        )

    redraw_in_place, redraw_hint = await lane.run(_resolve_redraw_preference, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    door = await edit_resource_draft(
        session, lane,
        title="Edit door" if existing is not None else "Register door",
        fields=_door_field_specs(),
        draft=draft, save=save, error_type=DoorError,
        save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=redraw_in_place, redraw_hint=redraw_hint,
        unicode_style=unicode_style,
        collapsed=collapsed,
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if door is not None:
        verb = "Updated" if existing is not None else "Registered door"
        await session.write_line(f"{verb} {door.name!r}.")
    return door


def _find_door_by_name(db: Database, name: str) -> Door | None:
    try:
        return get_door_by_name(db, name)
    except DoorError:
        return None


async def _resolve_door_name_collision(
    session: Session, lane: DatabaseLane, actor: User, default_name: str,
) -> str | None:
    """Shared by every screen that prefills a *new* door registration
    from a default name it didn't get to choose freely (the bundled-door
    gallery, the SysOp's-own-scripts filesystem picker) -- `name` has a
    real `UNIQUE` constraint (registry.py), so silently handing back a
    name that's already taken would only ever surface as a save-time
    "name already in use" error, or worse, get past that by the SysOp
    incidentally renaming a field they were editing for an unrelated
    reason. Asks explicitly instead: a new instance under a different
    name, editing the existing registration directly, or cancelling.

    Returns the name to prefill a fresh registration with (`default_name`
    unchanged if there was no collision, or whatever the SysOp typed for
    a new instance), or `None` if the caller has nothing left to do --
    cancelled, or the existing door's own detail screen was opened
    directly instead."""
    existing = await lane.run(_find_door_by_name, default_name)
    if existing is None:
        return default_name
    await session.write_line(
        colored(f"\r\n{default_name!r} is already registered as a door.", fg_color=MUTED_COLOR)
    )
    await session.write(
        f"{menu_key('N', 'ew instance under a different name')}  "
        f"{menu_key('E', 'dit the existing one')}  {menu_key('C', 'ancel')}: "
    )
    choice = (await session.read_key()).lower()
    await session.write_line(choice.upper())
    if choice == "e":
        await _door_detail_screen(session, lane, actor, existing)
        return None
    if choice != "n":
        return None
    await session.write("New name for this instance (blank to cancel): ")
    new_name = sanitize_text((await session.read_line()).strip())
    return new_name or None


async def _door_gallery_screen(
    session: Session, lane: DatabaseLane, actor: User, description_level: str,
    redraw_in_place: bool, unicode_style: bool, collapsed: bool,
) -> None:
    """Browse NetBBS's own first-party doors (issue #172) and register
    one with sensible defaults pre-filled, instead of starting
    `[C]reate`'s editor from an all-blank form. These ship as real
    installed package data (`netbbs.doors.bundled`, the same mechanism
    `netbbs.net.banner_presets` already uses for bundled welcome-banner/
    masthead samples) -- not sample code to point a door at, first-class
    product content NetBBS ships with (Voidrunner alone is a genuinely
    complete persistent game, not a toy). A SysOp's own, separately-
    authored door is untouched by any of this -- still registered by
    hand via `[C]reate`, still a filesystem path the SysOp supplies.

    Unlike the welcome-banner/masthead galleries this otherwise mirrors
    the shape of, there's nothing here to actually preview -- a door is
    a program, not a rendered banner -- so selecting an entry shows its
    details (what it is, and what defaults would be used) rather than
    what it looks like.

    Dogfood follow-up: selecting an entry goes *straight* to the
    prefilled editor now, no confirmation step first. There used to be
    one, copied from the banner galleries' own "preview -> confirm ->
    apply" shape without noticing it doesn't actually fit here:
    confirming there directly writes bytes to disk and enables the
    banner, so a confirmation before that makes sense; confirming here
    only ever opened the very same editor a confirmation-free `[C]reate`
    already lets a SysOp enter freely, which still requires its own
    explicit `[S]ave` and lets `[B]ack` discard the draft with nothing
    persisted -- identical in risk to opening an *existing* door's own
    detail/edit screen from the list, which already has no confirmation
    gate either. The interpreter default is `sys.executable` -- whatever
    Python is currently running NetBBS itself, almost always correct for
    a stdlib-only door -- rather than a new stored "default interpreter"
    setting.

    Dogfood follow-up: if a door is already registered under this
    entry's exact default name, selecting it again no longer silently
    hands back that same name (which `create_door`'s own `UNIQUE`
    constraint on `name` would then reject on save unless the SysOp
    happened to also rename it while editing something unrelated --
    an accidental path to "I meant to update the existing one, not
    create a second" or vice versa). Registering the *same* underlying
    script more than once is a real, legitimate thing to want (the same
    game bound to a different Community, a different tick rate, a
    different universe) -- so this surfaces the collision explicitly and
    asks which was actually meant, rather than leaving it to chance.

    Declining, or backing out of the editor without saving, loops back
    into this same gallery (`pick_item`'s `start_stable_id` re-highlights
    the entry just looked at -- the same dogfood fix applied to the
    banner galleries) rather than exiting, so registering more than one
    bundled door in one visit doesn't mean re-entering `[G]allery` each
    time."""
    available = available_bundled_doors()
    if not available:
        await session.write_line(
            colored(
                "\r\nNo bundled doors found on this filesystem -- NetBBS's own doors ship as "
                "real installed package data, so this suggests an incomplete install rather "
                "than the normal case.",
                fg_color=MUTED_COLOR,
            )
        )
        return

    last_stable_id: int | None = None
    while True:
        selection = await pick_item(
            session,
            list(enumerate(available, start=1)),
            name_of=lambda pair: pair[1][0].name,
            stable_id_of=lambda pair: pair[0],
            description_of=lambda pair: pair[1][0].description,
            title="Door gallery",
            empty_message="No bundled doors found.",
            description_level=description_level,
            redraw_in_place=redraw_in_place,
            unicode_style=unicode_style,
            collapsed=collapsed,
            start_stable_id=last_stable_id,
        )
        if selection is None:
            return
        last_stable_id = selection[0]
        entry, path = selection[1]

        await session.write_line(colored(f"\r\n{entry.name}", fg_color=MUTED_COLOR, bold=True))
        await session.write_line(colored(
            reflow(sanitize_text(entry.description), width=session.terminal_width), fg_color=MUTED_COLOR,
        ))
        await session.write_line(colored(f"  Suggested min level: {entry.suggested_min_play_level}", fg_color=MUTED_COLOR))
        await session.write_line(colored(f"  Interpreter (default, editable next): {sys.executable}", fg_color=MUTED_COLOR))
        await session.write_line(colored(f"  Script: {path}", fg_color=MUTED_COLOR))

        prefill_name = await _resolve_door_name_collision(session, lane, actor, entry.name)
        if prefill_name is None:
            continue

        prefill = {
            "name": prefill_name, "description": entry.description,
            # `.as_posix()`, not `str(path)`: the args field is parsed
            # with `shlex.split` in POSIX mode (see `_door_field_specs`'
            # own docstring -- deliberately, for the same never-a-shell
            # safety reasoning as everywhere else args reach a
            # subprocess argv list), which treats a bare backslash as an
            # escape character and silently eats it -- a raw Windows
            # path (`C:\Users\...`) round-trips through that as mangled
            # garbage (`C:Users...`). Forward slashes are unaffected by
            # that escaping and are accepted as path separators on every
            # platform NetBBS runs on, Windows included.
            "executable_path": sys.executable, "args_line": path.as_posix(),
            "min_play_level": entry.suggested_min_play_level,
        }
        await _door_screen(session, lane, actor, prefill=prefill)


async def _door_filesystem_screen(
    session: Session, lane: DatabaseLane, actor: User, description_level: str,
    redraw_in_place: bool, unicode_style: bool, collapsed: bool,
) -> None:
    """A SysOp's *own* door scripts, not NetBBS's -- the direct
    counterpart to `[G]allery` for something a SysOp wrote or downloaded
    themselves rather than one of NetBBS's own bundled doors. Same
    locked design as issue #170's welcome-banner/masthead filesystem
    picker, applied to a different directory: the browsable root is
    `netbbs.doors.custom_doors_dir(db)`, a real, narrow, conventional
    location under the node's own state directory -- not open-ended
    traversal from `/` -- that this screen never creates on its own; it
    simply reports nothing found until a SysOp places something there
    (e.g. via SFTP/SCP). Unfiltered by extension, unlike the `.ans`-only
    banner picker: a door can legitimately be any executable, not one
    well-known format.

    There is no metadata to show here the way a bundled door's own
    catalog entry has (no description, no suggested play level) -- a
    SysOp's own script is just a filename NetBBS knows nothing about.
    The prefilled name defaults to the file's own stem, the interpreter
    defaults to `sys.executable` the same guessed-default way the
    gallery's own prefill does, and both remain fully editable in the
    real create-door editor this opens next -- never auto-registered on
    selection alone. Same duplicate-name handling as the gallery
    (`_resolve_door_name_collision`) and the same cursor-preserving
    loop-back on decline/cancel/save (`pick_item`'s `start_stable_id`)."""

    def _list(db: Database) -> tuple[list[Path], Path]:
        directory = custom_doors_dir(db)
        if not directory.is_dir():
            return [], directory
        return sorted(p for p in directory.iterdir() if p.is_file()), directory

    files, directory = await lane.run(_list)
    if not files:
        await session.write_line(colored(
            f"\r\nNo files found in {directory}. Place your own door script there "
            f"(e.g. via SFTP/SCP), then browse again.", fg_color=MUTED_COLOR,
        ))
        return

    last_stable_id: int | None = None
    while True:
        selection = await pick_item(
            session,
            list(enumerate(files, start=1)),
            name_of=lambda pair: pair[1].name,
            stable_id_of=lambda pair: pair[0],
            title="Doors -- your own scripts",
            empty_message="No files found.",
            description_level=description_level,
            redraw_in_place=redraw_in_place,
            unicode_style=unicode_style,
            collapsed=collapsed,
            start_stable_id=last_stable_id,
        )
        if selection is None:
            return
        last_stable_id = selection[0]
        path = selection[1]

        await session.write_line(colored(f"\r\n{path.name}", fg_color=MUTED_COLOR, bold=True))
        await session.write_line(colored(f"  Interpreter (default, editable next): {sys.executable}", fg_color=MUTED_COLOR))
        await session.write_line(colored(f"  Script: {path}", fg_color=MUTED_COLOR))

        prefill_name = await _resolve_door_name_collision(session, lane, actor, path.stem)
        if prefill_name is None:
            continue

        prefill = {
            "name": prefill_name, "description": None,
            # `.as_posix()`, not `str(path)` -- see the gallery's own
            # identical comment above for why (shlex-mangled Windows
            # backslashes).
            "executable_path": sys.executable, "args_line": path.as_posix(),
            "min_play_level": 0,
        }
        await _door_screen(session, lane, actor, prefill=prefill)


async def _list_doors_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    doors = await lane.run(list_doors)
    selected = await pick_item(
        session, doors,
        name_of=lambda d: d.name,
        stable_id_of=lambda d: d.id,
        description_of=lambda d: d.description,
        title="Doors",
        empty_message="No doors yet.",
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if selected is not None:
        await _door_detail_screen(session, lane, actor, selected)


async def _door_detail_screen(session: Session, lane: DatabaseLane, actor: User, door: Door) -> None:
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    await _draw_door_detail(session, lane, door, description_level=description_level, redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "e":
            await session.write_line("")
            updated = await _door_screen(session, lane, actor, existing=door)
            if updated is not None:
                door = updated
            await _draw_door_detail(session, lane, door, description_level=description_level, redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed)
        elif choice == "d":
            await session.write_line("")
            deleted = await _delete_door_screen(session, lane, actor, door)
            if deleted:
                return
            await _draw_door_detail(session, lane, door, description_level=description_level, redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_door_detail(
    session: Session, lane: DatabaseLane, door: Door, *,
    description_level: str = "off", redraw_in_place: bool = False, unicode_style: bool = False, collapsed: bool = False,
) -> None:
    await session.write_line(
        "\r\n" + screen_title(sanitize_text(door.name),
            breadcrumb=(session.node_display_name,), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            header_color=await lane.run(effective_header_color_256), node_name_gradient=session.node_name_gradient)
    )
    description_text = sanitize_text(door.description) if door.description else "(none)"
    await session.write_line("Description:")
    await session.write_line(reflow(description_text, width=session.terminal_width))
    await session.write_line(f"Executable: {sanitize_text(door.executable_path)}")
    await session.write_line(f"Arguments: {' '.join(door.args) if door.args else '(none)'}")
    await session.write_line(f"Community: {await lane.run(_community_label, door.community_id)}")
    await session.write_line(f"Play level: {door.min_play_level}  Pinned: {'yes' if door.pinned else 'no'}")
    options = [
        MenuEntry(label=menu_key("E", "dit"), brief="Change this door's settings"),
        MenuEntry(label=menu_key("D", "elete"), brief="Permanently remove this door"),
        MenuEntry(label=menu_key("B", "ack"), brief="Return to the list"),
    ]
    await session.write_line(
        "\r\n" + _menu_row(options, description_level, width=session.terminal_width, height=session.terminal_height)
    )
    await session.write("Choice: ")


async def _delete_door_screen(session: Session, lane: DatabaseLane, actor: User, door: Door) -> bool:
    await session.write_line(
        colored("\r\nThis permanently removes the door from the catalogue. This cannot be undone.", fg_color=MUTED_COLOR)
    )
    await session.write(f"Type the door name {door.name!r} to confirm, or anything else to cancel: ")
    confirmation = (await session.read_line()).strip()
    if confirmation != door.name:
        await session.write_line("Cancelled.")
        return False
    await lane.run(delete_door, door, deleted_by=actor)
    await session.write_line(f"{door.name!r} deleted.")
    return True


# -- channels (design doc) --------------------------------------------------
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


async def _channel_menu(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext | None = None
) -> None:
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_channel_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "c":
            await session.write_line("")
            await _channel_screen(session, lane, actor)
            await _draw_channel_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "l":
            await session.write_line("")
            await _list_channels_screen(session, lane, actor, link_context=link_context)
            await _draw_channel_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_channel_menu(session: Session, description_level: str, redraw_in_place: bool, unicode_style: bool, collapsed: bool, header_color: int | tuple[int, int, int] = HEADER_COLOR) -> None:
    await session.write_line("\r\n" + screen_title("Chat channels",
            breadcrumb=(session.node_display_name,), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color, node_name_gradient=session.node_name_gradient))
    await session.write_line(
        _menu_row(
            [
                MenuEntry(label=menu_key("C", "reate"), brief="Add a new chat channel"),
                MenuEntry(label=menu_key("L", "ist"), brief="Browse and edit channels"),
                MenuEntry(label=menu_key("B", "ack"), brief="Return to the Content menu"),
            ],
            description_level,
            width=session.terminal_width,
            height=session.terminal_height,
        )
    )
    await session.write("Choice: ")


def _channel_field_specs(
    *, redraw_in_place: bool = False, unicode_style: bool = False, collapsed: bool = False
) -> list[FieldSpec]:
    """One shared field list drives both create and edit (design doc,
    dogfood feature request) -- see `_channel_screen`."""
    return [
        FieldSpec(
            key="name", hotkey="n", menu_text=menu_key("N", "ame"), label="Name",
            render=lambda d: d.get("name") or "(blank)",
            prompt=text_field("name", required=True),
            brief="The channel's display name",
            help="The channel's display name, shown wherever it's listed. Must be non-blank.",
        ),
        FieldSpec(
            key="description", hotkey="d", menu_text=menu_key("D", "escription"), label="Description",
            render=lambda d: d.get("description") or "(none)",
            prompt=text_field("description"),
            brief="Shown when browsing channels",
            help="A short explanation of what this channel is for, shown when browsing/selecting it.",
        ),
        FieldSpec(
            key="min_level", hotkey="l", menu_text=menu_key("L", "evel"), label="Min level",
            render=lambda d: str(d.get("min_level")),
            prompt=_int_field("min_level", "Minimum level"),
            brief="Level required to join",
            help=(
                "The permission level a caller needs to join this channel. Unlike boards/"
                "file areas, a channel's own level never inherits from its Community -- "
                "always a plain number, 0 meaning no gate."
            ),
        ),
        FieldSpec(
            key="community_id", hotkey="u", menu_text=menu_key("U", "nity", prefix="Comm"), label="Community",
            render=lambda d: d.get("community_id_label") or "(none)",
            prompt=_community_field(
                redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed
            ),
            brief="Parent community, if any",
            help=(
                "The Community this channel belongs to, if any -- a channel inside a "
                "Community can inherit its default age/name-requirement settings instead of "
                "each needing its own. 'none' keeps it outside every Community."
            ),
        ),
        FieldSpec(
            key="category_id", hotkey="c", menu_text=menu_key("C", "ategory"), label="Category",
            render=lambda d: d.get("category_id_label") or "(none)",
            prompt=_category_field(
                list_top_level=list_top_level_channel_categories, list_subcategories=list_channel_subcategories,
                title="Chat channel category", list_resources=list_channels, get_by_id=get_channel_category_by_id,
                redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            ),
            brief="Where it's grouped in listings",
            help=(
                "Where this channel is grouped when browsing listings -- purely "
                "organizational, has no effect on who can join."
            ),
        ),
        FieldSpec(
            key="pinned", hotkey="p", menu_text=menu_key("P", "inned"), label="Pinned",
            render=lambda d: "yes" if d.get("pinned") else "no",
            prompt=bool_field("pinned", "Pinned?"),
            brief="Shown at the top of listings",
            help="Shown at the top of channel listings, above unpinned channels, regardless of sort order.",
        ),
        FieldSpec(
            key="hidden", hotkey="h", menu_text=menu_key("H", "idden"), label="Hidden",
            render=lambda d: "yes" if d.get("hidden") else "no",
            prompt=bool_field("hidden", "Hidden (omitted from listings)?"),
            brief="Omitted from channel listings",
            help=(
                "Delists this channel from ordinary browsing without deleting it. Members "
                "who already know about it (or are invited) can still join and use it "
                "normally -- this only affects whether it shows up when browsing."
            ),
        ),
        FieldSpec(
            key="members_only", hotkey="m", menu_text=menu_key("M", "embers-only"), label="Members-only",
            render=lambda d: "yes" if d.get("members_only") else "no",
            prompt=bool_field("members_only", "Members-only (invite-only access)?"),
            brief="Only invited members may join",
            help="When on, a caller can only join via an invite from an existing member -- browsing to it isn't enough.",
        ),
        FieldSpec(
            key="allow_member_invites", hotkey="i", menu_text=menu_key("I", "nvites"),
            label="Allow member invites",
            render=lambda d: "yes" if d.get("allow_member_invites") else "no",
            prompt=bool_field("allow_member_invites", "Allow members to invite others?"),
            brief="Members can invite others too",
            help=(
                "When on, any regular member can invite someone else, not just a moderator/"
                "SysOp. Most relevant when Members-only is also on, since that's the only "
                "way new members can join at all."
            ),
        ),
        FieldSpec(
            key="min_age", hotkey="g", menu_text=menu_key("G", "e", prefix="Min a"), label="Min age",
            render=lambda d: _optional_int_label(d.get("min_age")),
            prompt=_min_age_field(),
            brief="Minimum caller age required",
            help=(
                "The minimum caller age required to join this channel, checked against a "
                "caller's own birthdate (Your profile › Name & details) even if they've "
                "chosen not to show it publicly. 'none' means no age gate."
            ),
        ),
        FieldSpec(
            key="name_requirement", hotkey="q", menu_text=menu_key("q", "uirement", prefix="Name re"),
            label="Name requirement",
            render=lambda d: _name_requirement_label(d.get("name_requirement")),
            prompt=_name_requirement_field(),
            step=_name_requirement_step(),
            help=_NAME_REQUIREMENT_HELP,
            brief="How chatters must be identified",
        ),
    ]


async def _channel_screen(
    session: Session, lane: DatabaseLane, actor: User, *, existing: Channel | None = None
) -> Channel | None:
    """Unified create/edit screen -- see `_board_screen`'s own
    docstring for the general shape and reasoning, identical here."""
    if existing is not None:
        draft = {
            "name": existing.name, "description": existing.description, "min_level": existing.min_level,
            "community_id": existing.community_id, "category_id": existing.category_id,
            "pinned": existing.pinned, "hidden": existing.hidden, "members_only": existing.members_only,
            "allow_member_invites": existing.allow_member_invites,
            "min_age": existing.min_age, "name_requirement": existing.name_requirement,
        }
        draft["community_id_label"] = (
            (await lane.run(get_community, existing.community_id)).name
            if existing.community_id is not None else None
        )
        draft["category_id_label"] = (
            (await lane.run(get_channel_category_by_id, existing.category_id)).name
            if existing.category_id is not None else None
        )
    else:
        draft = {
            "name": "", "description": None, "min_level": 0,
            "community_id": None, "category_id": None, "pinned": False, "hidden": False,
            "members_only": False, "allow_member_invites": False,
            "min_age": None, "name_requirement": None,
            "community_id_label": None, "category_id_label": None,
        }

    async def save(draft: dict) -> Channel:
        if not draft["name"]:
            raise ChannelError("name cannot be blank")
        if existing is None:
            return await lane.run(
                create_channel,
                draft["name"], description=draft["description"], min_level=draft["min_level"],
                category_id=draft["category_id"], pinned=draft["pinned"], hidden=draft["hidden"],
                members_only=draft["members_only"], allow_member_invites=draft["allow_member_invites"],
                min_age=draft["min_age"], name_requirement=draft["name_requirement"],
                community_id=draft["community_id"], creator=actor,
            )
        return await lane.run(
            update_channel,
            existing, name=draft["name"], description=draft["description"], min_level=draft["min_level"],
            category_id=draft["category_id"], pinned=draft["pinned"], hidden=draft["hidden"],
            members_only=draft["members_only"], allow_member_invites=draft["allow_member_invites"],
            min_age=draft["min_age"], name_requirement=draft["name_requirement"],
            community_id=draft["community_id"], changed_by=actor,
        )

    redraw_in_place, redraw_hint = await lane.run(_resolve_redraw_preference, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    channel = await edit_resource_draft(
        session, lane,
        title="Edit chat channel" if existing is not None else "Create chat channel",
        fields=_channel_field_specs(
            redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed
        ),
        draft=draft, save=save, error_type=ChannelError,
        save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=redraw_in_place, redraw_hint=redraw_hint,
        unicode_style=unicode_style,
        collapsed=collapsed,
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if channel is not None:
        verb = "Updated" if existing is not None else "Created chat channel"
        await session.write_line(f"{verb} {channel.name!r}.")
    return channel


async def _list_channels_screen(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext | None = None
) -> None:
    channels = await lane.run(list_channels)
    selected = await pick_item(
        session, channels,
        name_of=lambda c: c.name,
        stable_id_of=lambda c: c.id,
        description_of=_channel_description,
        title="Chat channels",
        empty_message="No chat channels yet.",
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if selected is not None:
        await _channel_detail_screen(session, lane, actor, selected, link_context=link_context)


def _channel_description(channel: Channel) -> str:
    bits = [f"level {channel.min_level}"]
    if channel.members_only:
        bits.append("members-only")
    if channel.hidden:
        bits.append("hidden")
    return ", ".join(bits)


async def _channel_detail_screen(
    session: Session, lane: DatabaseLane, actor: User, channel: Channel, *, link_context: LinkContext | None = None
) -> None:
    linked = await lane.run(is_channel_linked, channel) if link_context is not None else False
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    await _draw_channel_detail(session, lane, channel, linked=linked, link_context=link_context, description_level=description_level, redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "e":
            await session.write_line("")
            updated = await _channel_screen(session, lane, actor, existing=channel)
            if updated is not None:
                channel = updated
            await _draw_channel_detail(session, lane, channel, linked=linked, link_context=link_context, description_level=description_level, redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed)
        elif choice == "d":
            await session.write_line("")
            deleted = await _delete_channel_screen(session, lane, actor, channel)
            if deleted:
                return
            await _draw_channel_detail(session, lane, channel, linked=linked, link_context=link_context, description_level=description_level, redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed)
        elif choice == "r":
            await session.write_line("")
            await _channel_restrictions_screen(session, lane, actor, channel)
            await _draw_channel_detail(session, lane, channel, linked=linked, link_context=link_context, description_level=description_level, redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed)
        elif choice == "l" and link_context is not None and not linked:
            await session.write_line("")
            await _link_channel_screen(session, lane, channel, link_context)
            linked = await lane.run(is_channel_linked, channel)
            await _draw_channel_detail(session, lane, channel, linked=linked, link_context=link_context, description_level=description_level, redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_channel_detail(
    session: Session,
    lane: DatabaseLane,
    channel: Channel,
    *,
    linked: bool = False,
    link_context: LinkContext | None = None,
    description_level: str = "off",
    redraw_in_place: bool = False,
    unicode_style: bool = False,
    collapsed: bool = False,
) -> None:
    await session.write_line(
        "\r\n" + screen_title(sanitize_text(channel.name),
            breadcrumb=(session.node_display_name,), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            header_color=await lane.run(effective_header_color_256), node_name_gradient=session.node_name_gradient)
    )
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
    if link_context is not None:
        await session.write_line(f"Linked: {'yes' if linked else 'no'}")
    options = [
        MenuEntry(label=menu_key("E", "dit"), brief="Change this channel's settings"),
        MenuEntry(label=menu_key("D", "elete"), brief="Permanently remove this channel"),
        MenuEntry(label=menu_key("R", "estrictions"), brief="Active mutes and bans"),
    ]
    if link_context is not None and not linked:
        options.append(MenuEntry(label=menu_key("L", "ink this chat channel"), brief="Share it via NetBBS Link"))
    options.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to the list"))
    await session.write_line(
        "\r\n"
        + _menu_row(options, description_level, width=session.terminal_width, height=session.terminal_height)
    )
    await session.write("Choice: ")


async def _channel_restrictions_screen(session: Session, lane: DatabaseLane, actor: User, channel: Channel) -> None:
    """
    Lists every currently-active mute/ban on `channel` and lets a
    SysOp lift one -- the ordinary door alongside `_check_ban`'s own
    emergency SysOp bypass (`netbbs.net.chat_flow`, dogfood follow-up).
    Before this, a self-ban or a ban placed by any channel moderator
    had no interactive recovery path whatsoever: `/unban` can only be
    run from *inside* the channel by someone holding MODERATE, which
    is exactly what being banned prevents, and no admin screen anywhere
    even listed `channel_restrictions` rows -- the only fix was direct
    database surgery.

    Usernames/timestamps are resolved once per redraw via `lane.run`,
    then read synchronously by `pick_item`'s own `name_of`/
    `description_of` callbacks -- same "pre-fetch, don't call the lane
    from inside a sync callback" shape `_who_screen` already
    established (see this module's own docstring) for the identical
    reason.
    """
    def _load(db: Database) -> tuple[list[ChannelRestriction], dict[int, str], str | None, str | None]:
        restrictions = list_active_channel_restrictions(db, channel)
        user_ids = {r.user_id for r in restrictions} | {r.imposed_by_user_id for r in restrictions}
        usernames: dict[int, str] = {}
        for user_id in user_ids:
            resolved = get_user_by_id(db, user_id)
            usernames[user_id] = resolved.username if resolved is not None else "(deleted account)"
        display_format, display_timezone = resolve_display_preferences(db)
        return restrictions, usernames, display_format, display_timezone

    def _name_of(r: ChannelRestriction, usernames: dict[int, str]) -> str:
        return f"{r.kind} -- {usernames[r.user_id]}"

    def _description_of(
        r: ChannelRestriction, usernames: dict[int, str], display_format: str | None, display_timezone: str | None
    ) -> str:
        if r.expires_at is None:
            expiry = "indefinite"
        else:
            expiry = f"until {format_for_display(r.expires_at, override_format=display_format, override_timezone=display_timezone)}"
        by = f"by {usernames[r.imposed_by_user_id]}"
        reason = f" -- {sanitize_text(r.reason)}" if r.reason else ""
        return f"{expiry} ({by}){reason}"

    last_stable_id: int | None = None
    while True:
        restrictions, usernames, display_format, display_timezone = await lane.run(_load)

        selected = await pick_item(
            session, restrictions,
            name_of=lambda r: _name_of(r, usernames),
            stable_id_of=lambda r: r.id,
            description_of=lambda r: _description_of(r, usernames, display_format, display_timezone),
            title=f"Restrictions on {channel.name!r}",
            empty_message="No active mute/ban restrictions on this chat channel.",
            redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
            unicode_style=await lane.run(unicode_style_enabled, actor),
            collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
            accent_color=await lane.run(effective_accent_color_256),
            header_color=await lane.run(effective_header_color_256),
            start_stable_id=last_stable_id,
        )
        if selected is None:
            return
        last_stable_id = selected.id

        target_label = usernames[selected.user_id]
        if not await prompt_yes_no(session, f"Lift this {selected.kind} on {target_label!r}?", default=False):
            await session.write_line("Cancelled.")
            continue

        target = await lane.run(get_user_by_id, selected.user_id)
        if target is None:
            # The account was deleted after this restriction was
            # imposed -- nothing left to look up an unmute/unban
            # against by object, and the restriction row itself is
            # already unreachable through any ordinary path once its
            # subject is gone. Same "nothing to do" shape lifting an
            # already-lifted restriction gets, just for a different
            # reason.
            await session.write_line(colored("That account no longer exists.", fg_color=MUTED_COLOR))
            continue

        if selected.kind == "mute":
            await lane.run(unmute_user, channel, target, unmuted_by=actor)
        else:
            await lane.run(unban_user, channel, target, unbanned_by=actor)
        await session.write_line(f"Lifted the {selected.kind} on {target_label!r}.")


async def _link_channel_screen(session: Session, lane: DatabaseLane, channel: Channel, link_context: LinkContext) -> None:
    """
    `[L]ink this channel` (design doc §9.6, issue #87 -- previously
    defined by `netbbs.link.channels.link_channel` but, unlike `link_
    board`, never actually reachable from any live UI action; this is
    that missing call site). Mirrors `_link_board_screen`, minus every
    field `Channel` has no equivalent setting for (no `default_min_
    write_level`/`_moderated`/`_max_post_age_days`, no `forked_from` --
    channel origin succession is reused by reference only, not built,
    design doc §9.6).
    """
    await session.write_line(
        colored("\r\nLink this chat channel", fg_color=await lane.run(effective_header_color_256), bold=True)
    )
    default_min_level, ok = await _prompt_optional_int(
        session, "Recommended minimum level", current=channel.min_level
    )
    if not ok:
        return
    default_min_age, ok = await _prompt_min_age(session, current=channel.min_age)
    if not ok:
        return
    default_name_requirement, ok = await _prompt_name_requirement(session, current=channel.name_requirement)
    if not ok:
        return

    try:
        genesis = await lane.run(
            link_channel,
            channel,
            node_identity=link_context.node_identity,
            default_min_level=default_min_level,
            default_min_age=default_min_age,
            default_name_requirement=default_name_requirement,
        )
    except LinkChannelsError as exc:
        await session.write_line(colored(f"Could not Link chat channel: {exc}", fg_color=MUTED_COLOR))
        return

    link_context.link_node.channels[channel.channel_id] = genesis
    link_context.link_node.known_event_ids.add(genesis.content_id)
    link_context.link_node.events[genesis.content_id] = genesis.to_dict()

    await session.write_line(f"Linked {channel.name!r} -- it will be pushed to peers on the next sync pass.")




async def _delete_channel_screen(session: Session, lane: DatabaseLane, actor: User, channel: Channel) -> bool:
    await session.write_line(
        colored(
            "\r\nThis permanently deletes the chat channel, its scrollback, mute/ban "
            "restrictions, membership/invitations, and any moderator grants "
            "scoped to it. This cannot be undone.",
            fg_color=MUTED_COLOR,
        )
    )
    await session.write(f"Type the chat channel name {channel.name!r} to confirm, or anything else to cancel: ")
    confirmation = (await session.read_line()).strip()
    if confirmation != channel.name:
        await session.write_line("Cancelled.")
        return False
    await lane.run(delete_channel, channel, deleted_by=actor)
    await session.write_line(f"{channel.name!r} deleted.")
    return True


# -- categories ----------------------------------------------------------


async def _category_menu(session: Session, lane: DatabaseLane, actor: User) -> None:
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_category_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
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
                error_type=CategoryError, title="Message board categories",
            )
            await _draw_category_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "f":
            await session.write_line("")
            await _generic_category_screen(
                session, lane, actor,
                create=create_file_category, list_top_level=list_top_level_file_categories,
                list_subcategories=list_file_subcategories, delete=delete_file_category,
                error_type=FileCategoryError, title="File-area categories",
            )
            await _draw_category_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "c":
            await session.write_line("")
            await _generic_category_screen(
                session, lane, actor,
                create=create_channel_category, list_top_level=list_top_level_channel_categories,
                list_subcategories=list_channel_subcategories, delete=delete_channel_category,
                error_type=ChannelCategoryError, title="Chat channel categories",
            )
            await _draw_category_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_category_menu(session: Session, description_level: str, redraw_in_place: bool, unicode_style: bool, collapsed: bool, header_color: int | tuple[int, int, int] = HEADER_COLOR) -> None:
    await session.write_line("\r\n" + screen_title("Categories",
            breadcrumb=(session.node_display_name,), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color, node_name_gradient=session.node_name_gradient))
    await session.write_line(
        _menu_row(
            [
                MenuEntry(label=menu_key("M", "essage board category"), brief="Organize message boards"),
                MenuEntry(label=menu_key("F", "ile-area category"), brief="Organize file areas"),
                MenuEntry(label=menu_key("C", "hat channel category"), brief="Organize chat channels"),
                MenuEntry(label=menu_key("B", "ack"), brief="Return to the Content menu"),
            ],
            description_level,
            width=session.terminal_width,
            height=session.terminal_height,
        )
    )
    await session.write("Choice: ")


async def _generic_category_screen(
    session: Session, lane: DatabaseLane, actor: User, *, create, list_top_level, list_subcategories, delete,
    error_type, title: str,
) -> None:
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_generic_category_menu(session, title, description_level, redraw_in_place, unicode_style, collapsed, header_color)
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
            await _draw_generic_category_menu(session, title, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "l":
            await session.write_line("")
            await _list_categories_screen(
                session, lane, actor, list_top_level=list_top_level,
                list_subcategories=list_subcategories, delete=delete,
            )
            await _draw_generic_category_menu(session, title, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_generic_category_menu(
    session: Session, title: str, description_level: str, redraw_in_place: bool,
    unicode_style: bool,
    collapsed: bool,
    header_color: int | tuple[int, int, int] = HEADER_COLOR,
) -> None:
    await session.write_line("\r\n" + screen_title(title,
            breadcrumb=(session.node_display_name,), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color, node_name_gradient=session.node_name_gradient))
    await session.write_line(
        _menu_row(
            [
                MenuEntry(label=menu_key("C", "reate"), brief="Add a new category"),
                MenuEntry(label=menu_key("L", "ist/delete"), brief="Browse and remove categories"),
                MenuEntry(label=menu_key("B", "ack"), brief="Return to Categories"),
            ],
            description_level,
            width=session.terminal_width,
            height=session.terminal_height,
        )
    )
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
            redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
            unicode_style=await lane.run(unicode_style_enabled, actor),
            collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
            accent_color=await lane.run(effective_accent_color_256),
            header_color=await lane.run(effective_header_color_256),
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
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if selected is None:
        return
    await session.write_line(
        colored(
            "\r\nDeleting this category sets any message boards/file areas/chat channels "
            "assigned to it (and any of its own sub-categories) back to uncategorized.",
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


async def _pick_moderator_scope(
    session: Session, lane: DatabaseLane, *, redraw_in_place: bool, unicode_style: bool, collapsed: bool
) -> tuple[str, int | None, str, int | None] | None:
    """Returns `(object_type, object_id, human label, community_id)`,
    or `None` if cancelled. `object_id=None` means a blanket grant
    (design doc) -- `community_id` further narrows a blanket grant to
    one Community's membership (design doc §16's Community-blanket
    tier) instead of the whole node;
    `community_id` is always `None` for a per-object grant (board/file
    area/chat channel), since a specific object's own `community_id`
    already answers that question without needing it duplicated on the
    grant."""
    # Dogfood-reported regression, same shape as picker.py's own nav-
    # trailer bug: this line used to be one hand-built, unclamped
    # concatenation -- 147 columns wide once all six options were
    # spelled out in full, nearly double a standard 80-column terminal
    # -- so a real client wrapped it wherever it happened to land,
    # mid-word. `action_bar` already wraps whole options deterministically
    # at the terminal edge (the same primitive every other hotkey menu
    # in this codebase uses); routing through it here instead of a raw
    # write() fixes this the same way, not a one-off patch.
    scope_options = [
        menu_key("b", "oard", prefix="message "),
        menu_key("a", "rea", prefix="file "),
        menu_key("n", "nel", prefix="chat cha"),
        menu_key("x", "", prefix="blanket across all boards "),
        menu_key("y", "", prefix="blanket across all areas "),
        menu_key("z", "", prefix="blanket across all channels "),
    ]
    await session.write_line("Scope:")
    await session.write(f"{action_bar(scope_options, width=session.terminal_width)}: ")
    scope_key = (await session.read_key()).lower()
    await session.write_line("")
    accent_color = await lane.run(effective_accent_color_256)
    header_color = await lane.run(effective_header_color_256)
    if scope_key == "b":
        board = await pick_item(
            session, await lane.run(list_boards, order_by="alphabetical"),
            name_of=lambda b: b.name, stable_id_of=lambda b: b.id,
            title="Which message board?", empty_message="No message boards yet.",
            redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            accent_color=accent_color, header_color=header_color,
        )
        if board is None:
            return None
        return "board", board.id, f"message board {board.name!r}", None
    elif scope_key == "a":
        area = await pick_item(
            session, await lane.run(list_file_areas, order_by="alphabetical"),
            name_of=lambda a: a.name, stable_id_of=lambda a: a.id,
            title="Which file area?", empty_message="No file areas yet.",
            redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            accent_color=accent_color, header_color=header_color,
        )
        if area is None:
            return None
        return "file_area", area.id, f"file area {area.name!r}", None
    elif scope_key == "n":
        channel = await pick_item(
            session, await lane.run(list_channels),
            name_of=lambda c: c.name, stable_id_of=lambda c: c.id,
            title="Which chat channel?", empty_message="No chat channels yet.",
            redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            accent_color=accent_color, header_color=header_color,
        )
        if channel is None:
            return None
        return "channel", channel.id, f"chat channel {channel.name!r}", None
    elif scope_key == "x":
        object_type, label = "board", "all message boards (blanket)"
    elif scope_key == "y":
        object_type, label = "file_area", "all file areas (blanket)"
    elif scope_key == "z":
        object_type, label = "channel", "all chat channels (blanket)"
    else:
        await session.write_line(colored("Not a valid scope -- cancelled.", fg_color=MUTED_COLOR))
        return None

    community_id = await _pick_optional_community_blanket_scope(
        session, lane, redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed
    )
    if community_id is not None:
        community = await lane.run(get_community, community_id)
        label = f"{label} scoped to Community {community.name!r}"
    return object_type, None, label, community_id


async def _pick_optional_community_blanket_scope(
    session: Session, lane: DatabaseLane, *, redraw_in_place: bool, unicode_style: bool, collapsed: bool
) -> int | None:
    """The blanket-grant-scoping follow-up (design doc §16):
    'Scope this blanket grant to one Community instead of the whole
    node?' -- extends the existing X/Y/Z blanket keys rather than
    adding new ones. Returns the chosen
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
        redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    return selected.id if selected is not None else None


async def _grant_moderator_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    target = await pick_item(
        session, await lane.run(list_users),
        name_of=lambda u: u.username, stable_id_of=lambda u: u.id,
        title="Grant moderator to which user?", empty_message="No registered users yet.",
        redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if target is None:
        return
    scope = await _pick_moderator_scope(
        session, lane, redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed
    )
    if scope is None:
        return
    object_type, object_id, label, community_id = scope

    if object_type == "channel":
        await session.write_line("Preset:")
        await session.write(
            f"{action_bar([menu_key('F', 'ull moderator (edit+moderate+manage members)'), menu_key('M', 'oderator only')], width=session.terminal_width)}: "
        )
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
        await session.write_line("Preset:")
        await session.write(
            f"{action_bar([menu_key('F', 'ull moderator (edit+delete+approve)'), menu_key('A', 'pprover only')], width=session.terminal_width)}: "
        )
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
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    target = await pick_item(
        session, await lane.run(list_users),
        name_of=lambda u: u.username, stable_id_of=lambda u: u.id,
        title="Revoke moderator from which user?", empty_message="No registered users yet.",
        redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if target is None:
        return
    scope = await _pick_moderator_scope(
        session, lane, redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed
    )
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
