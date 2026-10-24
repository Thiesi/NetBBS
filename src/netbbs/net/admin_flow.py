"""
The SysOp console (design doc -- SysOp foundation): its entry points and menus.

`admin_menu` is the single implementation of the SysOp console, reachable
two ways: a gated menu option inside an authenticated BBS session
(`netbbs.net.login_flow`), and the standalone local CLI tool
(`netbbs.admin.__main__`, `python -m netbbs.admin`) -- see that
module's docstring for why the two entry points share this rather than
each carrying their own copy. Every action is audit-logged against
whichever `User` the caller supplies, regardless of which entry point that
came from. `staff_menu` and `staff_list_screen` are the staff side of the
same console.

This module holds the entry points, the admin, Operations and Content
menus and the dashboard panel. The screens they open live in one module
per area (issue #1311 split them out of this file):

- `admin_ui`: the shared screen machinery -- announcements, choice
  prompts, pickers, draft editors, detail views, panel writers, uploads;
- `admin_fields`: draft-editor fields the area editors share;
- `admin_resources`: listing and detail helpers shared by boards, file
  areas, chat channels and Communities;
- `admin_users`, `admin_trust`, `admin_link`, `admin_system`,
  `admin_sessions`, `admin_maintenance`, `admin_banners`,
  `admin_moderation`, `admin_boards`, `admin_files`, `admin_chat`,
  `admin_doors`: the screens of each area.

The area modules import from `admin_ui`, `admin_fields`,
`admin_resources` and from each other without a cycle (a detail screen
opening another area's screen), and never from this module, so this
module can import all of them. A test that patches a name must
patch the module whose code calls it: the menus here look the screens up
in this module's globals, the screens look their helpers up in their own
(`tests/test_admin_patch_targets.py` holds every patch to that).

Every screen and menu takes `lane: DatabaseLane` instead of
`db: Database` (design doc, issue #57), and every direct domain-function
call goes through `await lane.run(...)`. Synchronous callbacks such as
`pick_item`'s `name_of`/`description_of` only read attributes already on
the objects they are handed, never the database. There is no
live-session/CLI distinction for admin functionality once inside
`admin_menu` (that is `node_controls`' job).
"""

from __future__ import annotations

import datetime
import json
import logging
import math
from pathlib import Path
from typing import Any, Sequence

import nacl.signing

from netbbs.access_map import LevelSource
from netbbs.auth.users import (
    SYSOP_LEVEL,
    StaffPermission,
    User,
    UserManagementError,
    current_account,
    list_users,
    describe_staff_permissions,
    is_usable_sysop,
)
from netbbs.backup import running_node_pid, get_last_backup_summary
from netbbs.backup_schedule import schedule_status
from netbbs.boards.boards import list_boards
from netbbs.boards.categories import Category
from netbbs.boards.posts import count_listed_posts, count_pending_posts
from netbbs.chat.channels import list_channels
from netbbs.chat.hub import ChatHub
from netbbs.communities import get_community, list_communities
from netbbs.doors import list_doors
from netbbs.files.areas import list_file_areas
from netbbs.files.categories import FileAreaCategory
from netbbs.files.entries import count_listed_files, count_pending_files
from netbbs.link.boards import LinkContext
from netbbs.link.diagnostics import list_diagnostic_log_entries
from netbbs.link.enforcement import node_transport_state
from netbbs.link.carry import (
    OFFERED,
    count_carried_to_review,
    count_carry_decisions,
    hide_carried_resource,
)
from netbbs.link.trust import TrustState
from netbbs.link.work_items import list_work_items
from netbbs.net.policy_settings import SETTINGS as POLICY_SETTINGS
from netbbs.mrc.bridge import MrcBridge
from netbbs.staff import (
    MAX_AWAY_MESSAGE_CHARS,
    away_notice,
    away_problem,
    count_moderation_items,
    count_pending_accounts,
    describe_away,
    end_away,
    has_moderation_scope,
    list_staff,
    node_today,
    set_away,
)
from netbbs.moderation.roles import describe_grant, get_grant
from netbbs.net.char_input import HELP_KEY, REDRAW_KEY, REFRESH_KEY, reject_unhandled_key
from netbbs.net.help_overlay import show_menu_help
from netbbs.net.location import crumbs, located
from netbbs.net.session import Session, write_prompt
from netbbs.net.shutdown import NodeControls
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.node_theme import effective_header_color_256
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.selfupdate import get_display_check_summary
from netbbs.rendering import (
    ALERT_COLOR,
    AUTHOR_COLOR,
    DATE_COLOR,
    LABEL_COLOR,
    MENU_KEY_COLOR,
    MUTED_COLOR,
    SUCCESS_COLOR,
    VALUE_COLOR,
    WARNING_COLOR,
    MenuEntry,
    action_bar,
    colored,
    counts_row,
    cut_to_width,
    highlight_result,
    menu_grid,
    menu_key,
    reject_keystroke,
    sanitize_text,
    screen_title,
    status_badge,
    telemetry_gauge,
    truncate,
    visible_width,
)
from netbbs.rendering.detail import Field, Note, Section, Table
from netbbs.rendering.reflow import wrap_terminal_text
from netbbs.guest import set_guest_user, set_pre_login_notice
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display, resolve_display_preferences
from netbbs.net.admin_ui import (
    _announce_line,
    _choice_prompt,
    _console_transfers,
    _degrade_description_level,
    _fitted_menu,
    _menu_row,
    _show_report,
    _wrap_counts_panel,
    _wrap_panel_sentence,
    _write_panel,
    _write_sections,
)
from netbbs.net.admin_maintenance import (
    _audit_log_screen,
    _backup_status_screen,
    _diagnostic_log_screen,
    _diagnostic_log_tail_screen,
    _node_log_screen,
    _prune_drafts_screen,
    _repair_carried_posts_screen,
    _search_indexes_screen,
)
from netbbs.net.admin_link import _link_status_screen, _mail_tools_screen, _outbox_screen
from netbbs.net.admin_moderation import _pending_review_screen
from netbbs.net.admin_system import _managed_dns_status_screen, _system_menu
from netbbs.net.admin_sessions import _node_menu
from netbbs.net.admin_boards import (
    _board_menu,
    _category_menu,
    _community_menu,
    _grant_moderator_screen,
    _revoke_moderator_screen,
)
from netbbs.net.admin_files import _area_menu
from netbbs.net.admin_chat import _channel_menu
from netbbs.net.admin_doors import _door_menu
from netbbs.net.admin_users import _pick_and_edit_user, _users_menu
from netbbs.net.admin_pages import pages_screen
from netbbs.staff_pages import count_open_pages
from netbbs.auth.renames import count_open_username_requests

# Design doc -- node management, Thiesi's own request: an operator
# watching this node's own stdout/journal should see a shutdown/drain
# being scheduled (or maintenance mode toggled) as it happens, not only
# via the DB-backed moderation log (`record_action`, queried separately).
_logger = logging.getLogger(__name__)


def _link_health_snapshot(db: Database, link_context: LinkContext | None) -> dict[str, int]:
    """The dead-letter count and recent-diagnostic error/warning tally
    the SysOp landing page and the Operations sub-console both need --
    factored out so the two screens can't silently drift into
    disagreeing about what "recent" means by each re-deriving it."""
    dead_letters = len(list_work_items(db, status="dead_lettered")) if link_context is not None else 0
    recent_diagnostics = list_diagnostic_log_entries(db, limit=20) if link_context is not None else []
    return {
        "dead_letters": dead_letters,
        "recent_errors": sum(entry.level == "ERROR" for entry in recent_diagnostics),
        "recent_warnings": sum(entry.level == "WARNING" for entry in recent_diagnostics),
        # Issue #681: what Link carried in on its own and the SysOp has not
        # looked at, and what waits at the carry cap for their decision.
        # Read from the database, so shown whether or not Link runs now
        # (Codex review on #800): a node that stopped Link still has them.
        "carried_to_review": count_carried_to_review(db),
        "carry_offers": count_carry_decisions(db, OFFERED),
        # Issue #844: a peer on probation here exchanges nothing, which the
        # peer count alone made look healthy.
        "peers_on_probation": _count_peers_on_probation(db) if link_context is not None else 0,
    }


def _count_peers_on_probation(db: Database) -> int:
    """Verified peers on probation here, which exchange nothing with this
    node yet (issue #844). A quarantine or block is the SysOp's own doing
    and shows on Link status as such."""
    fingerprints = [row["fingerprint"] for row in db.connection.execute("SELECT fingerprint FROM link_peers")]
    return sum(node_transport_state(db, fingerprint) == TrustState.PROBATIONARY for fingerprint in fingerprints)


def _link_count_pairs(node, state: dict) -> list[tuple[str, int]]:
    """The Link counts every dashboard shows, with the peers nothing is
    exchanged with yet when there are any (issue #844)."""
    pairs = [("Peers", len(node.peers))]
    if state.get("peers_on_probation"):
        pairs.append(("On probation", state["peers_on_probation"]))
    return [*pairs, ("Relays", len(node.relays_serving_me)), ("Dead letters", state["dead_letters"])]


async def _with_fresh_carry_counts(lane: DatabaseLane, state: dict[str, object]) -> dict[str, object]:
    """`state` with its carry attention counts read again: a sub-console
    that leads to Link status may have decided offers there (Codex review
    on #800). Two counts, not the whole dashboard reload."""
    fresh = await lane.run(lambda db: {
        "carried_to_review": count_carried_to_review(db),
        "carry_offers": count_carry_decisions(db, OFFERED),
    })
    return {**state, **fresh}


def _carry_attention_pairs(state: dict[str, object]) -> list[tuple[str, int]]:
    """The dashboard's Link attention counts (issue #681), only when there
    is something to attend to: a node with nothing new draws as before."""
    pairs = []
    # Issue #1306: callers asking for a new username, under Users.
    if state.get("name_requests"):
        pairs.append(("Name requests", state["name_requests"]))
    if state.get("carried_to_review"):
        pairs.append(("Newly carried", state["carried_to_review"]))
    if state.get("carry_offers"):
        pairs.append(("Offered", state["carry_offers"]))
    return pairs


def _offline_console_note(node_running: bool) -> str:
    """What the health panel says when this console has no live node behind
    it -- `python -m netbbs.admin`, run on the node's machine (issue #834).
    "Standalone mode" read as "because I declined Link"; the reason is that
    this process is not the node, and whether the node is up at all."""
    if node_running:
        return (
            "This console runs outside the node. For live controls, log in to "
            "the node and open the SysOp menu there."
        )
    return "The node isn't running, so there are no live controls. Start the node to get them."


def _get_display_backup_summary(db: Database) -> tuple[str | None, str | None]:
    """Return backup state with its timestamp ready for human display."""
    backup_at, backup_path = get_last_backup_summary(db)
    return (
        format_for_display(backup_at, db) if backup_at else None,
        backup_path,
    )


def _get_display_next_backup(db: Database) -> str | None:
    """When the backup schedule runs next, for display; `None` while it is off
    (issue #727)."""
    status = schedule_status(db)
    if not status.schedule.enabled:
        return None
    if status.overdue:
        return "due now"
    return format_for_display(status.next_run.isoformat(), db) if status.next_run else None


# -- outcomes carried into the next redraw ------------------------------------
#
# The mechanism lives in `netbbs.net.notices` (issue #680 shared it with the
# caller-facing screens); these names are the console's own spelling of it.
# An action does not *write* its outcome; it `_announce`s it, and whichever
# console screen is drawn next shows it directly above its prompt --
# `_choice_prompt` for a screen that draws itself, `show_detail` for a paged
# one.


@located("SysOp")
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

    `node_controls` (design doc), if given, unlocks the `[N]ode` quick
    action and its entry in `[O]perations` (list/disconnect sessions,
    trigger shutdown), and lets `[F]ull backups` use the running node's
    configured identity path -- present when called
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
    transfers = node_controls.transfers if node_controls is not None else None
    owner = getattr(session, "notice_session", session)
    if transfers is not None:
        _console_transfers[owner] = transfers
    else:
        _console_transfers.pop(owner, None)
    dashboard_state = await _draw_admin_menu(
        session, lane, user, node_controls=node_controls, link_context=link_context
    )
    while True:
        choice = (await session.read_key()).lower()

        if await _operator_lost_sysop(session, lane, user, node_controls):
            await session.write_line(
                colored("\r\nYour account no longer has SysOp access.", fg_color=ALERT_COLOR)
            )
            return

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "r":
            dashboard_state = await _draw_admin_menu(
                session, lane, user, node_controls=node_controls, link_context=link_context
            )
        elif choice == "u":
            await session.write_line("")
            await _users_menu(session, lane, user, node_controls=node_controls, link_context=link_context)
            dashboard_state = await _draw_admin_menu(
                session, lane, user, node_controls=node_controls, link_context=link_context
            )
        elif choice in {"c", "m"}:
            await session.write_line("")
            await _content_menu(
                session, lane, user, link_context=link_context,
                mrc_bridge=node_controls.mrc_bridge if node_controls is not None else None,
                chat_hub=node_controls.chat_hub if node_controls is not None else None,
                door_services=node_controls.door_services if node_controls is not None else None,
                transfers=node_controls.transfers if node_controls is not None else None,
                backup_identity_dir=node_controls.backup_identity_dir if node_controls is not None else None,
            )
            dashboard_state = await _draw_admin_menu(
                session, lane, user, node_controls=node_controls, link_context=link_context
            )
        elif choice == "o":
            await session.write_line("")
            await _operations_menu(
                session, lane, user, node_controls=node_controls, link_context=link_context
            )
            # Link status is reachable from here too: its offer decisions
            # show, without reloading the rest (Codex review on #800).
            dashboard_state = await _with_fresh_carry_counts(lane, dashboard_state)
            await _draw_admin_menu(session, lane, user, node_controls=node_controls,
                                   link_context=link_context, state=dashboard_state)
        elif choice == "s":
            await session.write_line("")
            await _system_menu(session, lane, user, node_controls=node_controls, link_context=link_context)
            dashboard_state = await _with_fresh_carry_counts(lane, dashboard_state)
            await _draw_admin_menu(session, lane, user, node_controls=node_controls,
                                   link_context=link_context, state=dashboard_state)
        elif choice == "n" and node_controls is not None:
            await session.write_line("")
            await _node_menu(session, lane, user, node_controls)
            await _draw_admin_menu(session, lane, user, node_controls=node_controls,
                                   link_context=link_context, state=dashboard_state)
        elif choice == "l" and link_context is not None:
            await session.write_line("")
            await _link_status_screen(session, lane, user, link_context=link_context, node_controls=node_controls)
            # Reloaded: Link status is where offers are accepted or
            # excluded (Codex review on #800).
            dashboard_state = await _draw_admin_menu(
                session, lane, user, node_controls=node_controls, link_context=link_context
            )
        elif choice == "q" and link_context is not None:
            await session.write_line("")
            await _outbox_screen(session, lane, user)
            dashboard_state = await _draw_admin_menu(
                session, lane, user, node_controls=node_controls, link_context=link_context
            )
        elif choice == "f":
            await session.write_line("")
            await _backup_status_screen(session, lane, user, node_controls=node_controls)
            dashboard_state = await _draw_admin_menu(
                session, lane, user, node_controls=node_controls, link_context=link_context
            )
        elif choice == "d":
            await session.write_line("")
            await _managed_dns_status_screen(session, lane, user)
            await _draw_admin_menu(session, lane, user, node_controls=node_controls,
                                   link_context=link_context, state=dashboard_state)
        elif choice == "t":
            await session.write_line("")
            await _away_screen(session, lane, user)
            dashboard_state = await _draw_admin_menu(
                session, lane, user, node_controls=node_controls, link_context=link_context
            )
        elif choice == "p":
            await session.write_line("")
            await pages_screen(session, lane, user, node_controls)
            dashboard_state = await _draw_admin_menu(
                session, lane, user, node_controls=node_controls, link_context=link_context
            )
        elif choice == HELP_KEY:
            console, quick = _admin_menu_entries(node_controls, link_context)
            await show_menu_help(
                session, "SysOp console help", [*console, *quick],
                about="Above: the node's health and what waits for you. Below: every area of the "
                      "console, then quick keys for screens that also sit deeper in the menus.",
                header_color=dashboard_state["header_color"], unicode_style=dashboard_state["unicode_style"],
            )
            await _draw_admin_menu(session, lane, user, node_controls=node_controls,
                                   link_context=link_context, state=dashboard_state)
        else:
            await session.write(reject_unhandled_key(choice))


@located("Staff")
async def staff_menu(
    session: Session,
    lane: DatabaseLane,
    user: User,
    *,
    node_controls: NodeControls | None = None,
    link_context: LinkContext | None = None,
) -> None:
    """
    The Staff console (design doc §5.6, issue #836): the reduced console a
    staff member reaches from `[S]taff` on the main menu. Its landing view
    counts what waits for them, and it offers only the screens their
    permissions reach -- the accounts waiting for approval, the account
    list, the moderation queue -- which are the SysOp console's own screens
    (`_pick_and_edit_user`, `_pending_review_screen`), narrowed by the
    actor rather than copied. Nothing here reaches Settings, Link, Node,
    DNS or backups.

    Callers gate entry on `netbbs.staff.is_staff`. The console re-checks at
    every key, like the SysOp's (issue #659): once the operator holds no
    staff permission, it closes.
    """
    await _draw_staff_menu(session, lane, user)
    while True:
        choice = (await session.read_key()).lower()
        fresh = await lane.run(current_account, user)
        if fresh is None or not fresh.staff_permissions or fresh.user_level >= SYSOP_LEVEL:
            await session.write_line(colored("\r\nYour account no longer has staff access.", fg_color=ALERT_COLOR))
            return
        user = fresh
        if choice == "b":
            await session.write_line("")
            return
        if choice == "r":
            pass
        elif choice == "a" and user.has_staff(StaffPermission.APPROVE_ACCOUNTS):
            await session.write_line("")
            await _pick_and_edit_user(
                session, lane, user, node_controls, title="Waiting for approval", pending_only=True
            )
        elif choice == "u" and user.has_staff(StaffPermission.MANAGE_ACCOUNTS):
            await session.write_line("")
            await _pick_and_edit_user(session, lane, user, node_controls, title="Accounts")
        elif choice == "t":
            await session.write_line("")
            await _away_screen(session, lane, user)
        elif choice == "p":
            await session.write_line("")
            await pages_screen(session, lane, user, node_controls)
        elif choice == "m" and await lane.run(has_moderation_scope, user):
            await session.write_line("")
            await _pending_review_screen(
                session, lane, user, link_context=link_context,
                transfers=node_controls.transfers if node_controls is not None else None,
            )
        elif choice == HELP_KEY:
            moderates, header_color, unicode_style = await lane.run(lambda db: (
                has_moderation_scope(db, user), effective_header_color_256(db), unicode_style_enabled(db, user),
            ))
            await show_menu_help(
                session, "Staff console help", _staff_menu_entries(user, moderates=moderates),
                about="Your staff work on this node: only the screens your staff permissions reach "
                      "are listed. The counts above show what is waiting for you.",
                header_color=header_color, unicode_style=unicode_style,
            )
        else:
            await session.write(reject_unhandled_key(choice))
            continue
        await _draw_staff_menu(session, lane, user)


async def _draw_staff_menu(session: Session, lane: DatabaseLane, user: User) -> None:
    def _load(db: Database) -> dict[str, object]:
        return {
            "pending_accounts": count_pending_accounts(db),
            "moderates": has_moderation_scope(db, user),
            "held": count_moderation_items(db, user),
            "open_pages": count_open_pages(db),
            "description_level": menu_description_level(db, user),
            "redraw_in_place": redraw_in_place_enabled(db, user),
            "unicode_style": unicode_style_enabled(db, user),
            "collapsed": breadcrumb_collapsed_enabled(db, user),
            "header_color": effective_header_color_256(db),
            **_away_state(db, user),
        }

    state = await lane.run(_load)
    unicode_style = state["unicode_style"]
    await session.write_line(
        "\r\n" + screen_title(
            "Staff console",
            breadcrumb=crumbs(session, own=True),
            subtitle=f"Your staff permissions: {describe_staff_permissions(user.staff_permissions)}.",
            width=session.terminal_width,
            clear=state["redraw_in_place"],
            unicode_style=unicode_style, collapsed=state["collapsed"],
            header_color=state["header_color"], node_name_gradient=session.node_name_gradient,
        )
    )
    counts: list[Field] = []
    if user.has_staff(StaffPermission.APPROVE_ACCOUNTS):
        waiting = state["pending_accounts"]
        counts.append(Field(
            "Waiting for approval", str(waiting), color=WARNING_COLOR if waiting else MUTED_COLOR,
        ))
    if state["moderates"]:
        held = state["held"]
        counts.append(Field("Held for moderation", str(held), color=WARNING_COLOR if held else MUTED_COLOR))
    pages = state["open_pages"]
    counts.append(Field("Pages waiting", str(pages), color=WARNING_COLOR if pages else MUTED_COLOR))
    panel_rows = await _write_sections(session, [Section(None, counts)], unicode_style=unicode_style) if counts else 0
    for line in _away_panel_lines(state, width=min(session.terminal_width, 78) - 4, unicode_style=unicode_style):
        await session.write_line(line)
        panel_rows += 1
    options = _staff_menu_entries(user, moderates=bool(state["moderates"]))
    await session.write_line(
        "\r\n" + _fitted_menu(options, state["description_level"], session=session, used_rows=panel_rows + 5)
    )
    await _choice_prompt(session)


def _staff_menu_entries(user: User, *, moderates: bool) -> list[MenuEntry]:
    """The Staff console's menu, shared by its draw and its help."""
    options: list[MenuEntry] = []
    if user.has_staff(StaffPermission.APPROVE_ACCOUNTS):
        options.append(MenuEntry(label=menu_key("A", "ccounts waiting"), brief="Approve or decline signups"))
    if user.has_staff(StaffPermission.MANAGE_ACCOUNTS):
        options.append(MenuEntry(label=menu_key("U", "sers"), brief="Levels, disabling, passwords"))
    if moderates:
        options.append(MenuEntry(label=menu_key("M", "oderation"), brief="Held posts and uploads"))
    options.append(MenuEntry(label=menu_key("P", "ages"), brief="Callers asking for the SysOp"))
    options.append(MenuEntry(label=menu_key("T", "ime away"), brief="Tell members you're away"))
    options.append(MenuEntry(label=menu_key("R", "efresh"), brief="Redraw with current numbers"))
    options.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to the main menu"))
    return options


def _date_of(db: Database, stamp: str | None) -> str | None:
    """A stored timestamp as the node-local date alone (design doc §5.6:
    Operators shows the day of a last session, not the time)."""
    if not stamp:
        return None
    _fmt, tz_name = resolve_display_preferences(db)
    try:
        return format_for_display(stamp, override_format="%Y-%m-%d", override_timezone=tz_name)
    except ValueError:
        return None


def _away_state(db: Database, user: User) -> dict[str, object]:
    notice = away_notice(db, user)
    return {"away": notice, "away_since": _date_of(db, notice.since) if notice is not None else None}


def _away_panel_lines(state: dict[str, object], *, width: int, unicode_style: bool) -> list[str]:
    notice = state.get("away")
    if notice is None:
        return []
    text = f"You are marked {describe_away(notice, state['away_since'])}. [T]ime away ends it."
    return [
        colored(f"  {line}", fg_color=WARNING_COLOR)
        for line in _wrap_panel_sentence(sanitize_text(text), prefix="  ", width=width, unicode_style=unicode_style)
    ]


def _parse_return_date(raw: str) -> datetime.date | None:
    """`YYYY-MM-DD`, the one spelling that reads the same everywhere."""
    return datetime.date.fromisoformat(raw)


async def _away_screen(session: Session, lane: DatabaseLane, user: User) -> None:
    """
    Mark yourself away, or end it (design doc §5.6, issue #836): one short
    line of plain text and an optional return date. It shows on the Staff
    list beside your name and, when every approver is away, to pending
    callers. Being away changes nobody's permissions.
    """
    while True:
        state = await lane.run(lambda db: _away_state(db, user))
        notice = state["away"]
        await session.write_line("")
        await session.write_line(colored("Away notice", fg_color=LABEL_COLOR, bold=True))
        if notice is None:
            await session.write_line(colored("  You are not marked away.", fg_color=MUTED_COLOR))
        else:
            await session.write_line("  " + sanitize_text(describe_away(notice, state["away_since"])))
        options = [menu_key("S", "et" if notice is None else "et a new notice")]
        if notice is not None:
            options.append(menu_key("E", "nd it"))
        options.append(menu_key("B", "ack"))
        await write_prompt(session, f"\r\n{action_bar(options, width=session.terminal_width, height=session.terminal_height)}: ")
        choice = (await session.read_key()).lower()
        await session.write_line("")
        if choice == "b":
            return
        if choice == "e" and notice is not None:
            await lane.run(end_away, user)
            _announce_line(session, "You are no longer marked away.")
            return
        if choice == HELP_KEY:
            help_entries = [MenuEntry(label=menu_key("S", "et"),
                                      brief="Line and optional return date")]
            if notice is not None:
                help_entries.append(MenuEntry(label=menu_key("E", "nd it"), brief="Stop being marked away"))
            header_color, unicode_style = await lane.run(
                lambda db: (effective_header_color_256(db), unicode_style_enabled(db, user))
            )
            await show_menu_help(
                session, "Away notice help", help_entries,
                about="Tell members you are away. The notice shows beside your name under Operators, and "
                      "when everyone who can approve accounts is away, new callers waiting for approval "
                      "are told who is back first. A return date ends it by itself the day after. It "
                      "changes none of your permissions.",
                header_color=header_color, unicode_style=unicode_style,
            )
            continue
        if choice != "s":
            await session.write(reject_unhandled_key(choice))
            continue
        await write_prompt(session, f"Message (one line, up to {MAX_AWAY_MESSAGE_CHARS} characters): ")
        message = (await session.read_line()).strip()
        if not message:
            _announce_line(session, colored("Cancelled.", fg_color=MUTED_COLOR))
            continue
        await write_prompt(session, "Back on (YYYY-MM-DD, blank if you don't know): ")
        raw_date = (await session.read_line()).strip()
        try:
            until = _parse_return_date(raw_date) if raw_date else None
        except ValueError:
            _announce_line(session, colored(f"{raw_date!r} is not a date like 2026-10-12.", fg_color=MUTED_COLOR))
            continue
        problem = away_problem(message, until, await lane.run(node_today))
        if problem is not None:
            _announce_line(session, colored(f"{problem[0].upper()}{problem[1:]}.", fg_color=MUTED_COLOR))
            continue
        try:
            await lane.run(set_away, user, message, until)
        except UserManagementError as exc:
            _announce_line(session, colored(str(exc), fg_color=MUTED_COLOR))
            continue
        _announce_line(session, "You are marked away. Members see it under Operators.")
        return


def _grants_seen_by(db: Database, viewer: User, grants: Sequence) -> str:
    """A moderator's grants in words, leaving out any that would name
    something `viewer` can't see -- a hidden Community, a board, area or
    channel their level or age keeps from them (review on #870). A grant
    over every board or area names nothing, so it always shows."""
    from netbbs.net.board_flow import visible_boards
    from netbbs.net.chat_flow import list_visible_channels_for
    from netbbs.net.file_flow import visible_areas

    if is_usable_sysop(viewer):
        shown = list(grants)
    else:
        seen = {
            "board": {b.id for b in visible_boards(db, viewer, community_id=None, community_scoped=False)},
            "file_area": {a.id for a in visible_areas(db, viewer)},
            "channel": {c.id for c in list_visible_channels_for(db, viewer)},
        }
        shown = []
        for grant in grants:
            if grant.object_id is not None:
                visible = grant.object_id in seen[grant.object_type]
            elif grant.community_id is not None:
                community = get_community(db, grant.community_id)
                visible = community is not None and not community.hidden
            else:
                visible = True
            if visible:
                shown.append(grant)
    if not shown:
        return "moderates content you can't open"
    return "; ".join(describe_grant(db, grant) for grant in shown)


async def staff_list_screen(session: Session, lane: DatabaseLane, user: User) -> None:
    """
    Who runs the node (design doc §5.6, issue #836): every usable SysOp,
    staff member and moderator, what they look after, the day of their
    last session, and any away notice. For members; the main menu offers
    it to everyone but guests.
    """
    def _load(db: Database) -> tuple[list, dict[int, str | None], dict[int, str | None], dict[int, str]]:
        entries = list_staff(db)
        last_on = {entry.user.id: _date_of(db, entry.user.last_login_at) for entry in entries}
        away_since = {entry.user.id: _date_of(db, entry.away.since) if entry.away else None for entry in entries}
        looks_after = {
            entry.user.id: entry.looks_after or _grants_seen_by(db, user, entry.grants) for entry in entries
        }
        return entries, last_on, away_since, looks_after

    entries, last_on, away_since, looks_after = await lane.run(_load)
    rows = [
        [
            (sanitize_text(entry.user.username), AUTHOR_COLOR),
            entry.role,
            sanitize_text(looks_after[entry.user.id]),
            (last_on[entry.user.id] or "never", DATE_COLOR),
        ]
        for entry in entries
    ]
    body: list[Field | Note | Table] = (
        [Table(("Name", "Role", "Looks after", "Last on"), rows, flex=2)] if rows
        else [Note("Nobody is listed yet.")]
    )
    for entry in entries:
        if entry.away is not None:
            body.append(Note(
                sanitize_text(f"{entry.user.username}: {describe_away(entry.away, away_since[entry.user.id])}"),
                color=WARNING_COLOR,
            ))
    await _show_report(
        session, lane, user, "Staff",
        subtitle="Who runs this node, and when they were last on.", sections=[Section(None, body)],
        help_title="Staff help",
        help_about=(
            "Everyone who runs this node: SysOps, staff and moderators, what each looks after, and any away "
            "notice."
        ),
    )


async def _operator_lost_sysop(
    session: Session, lane: DatabaseLane, user: User, node_controls: NodeControls | None
) -> bool:
    """
    Whether the console's operator has been demoted, disabled or deleted
    since it opened (issue #659) -- by another SysOp, by themselves, or
    from another process.

    In a live session the account watcher unwinds the console anyway, and
    keeps the registry's baseline current, so this reads that baseline
    rather than adding a database round trip to every key -- an await
    here shifts the timing of the drain/shutdown screens' cancellation.
    The standalone CLI has no watcher and asks the database.
    """
    if user.user_level < SYSOP_LEVEL:
        return False
    if node_controls is not None:
        baseline = node_controls.session_registry.account_baseline(session)
        return baseline is not None and baseline[0] < SYSOP_LEVEL
    return not await lane.run(_still_sysop, user)


def _still_sysop(db: Database, user: User) -> bool:
    current = current_account(db, user)
    return current is not None and current.user_level >= SYSOP_LEVEL


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
        # Counted, not listed, and without sweeping expiry: this runs on every
        # draw of the dashboard (issue #681).
        pending_posts = sum(count_pending_posts(db, board) for board in all_boards)
        pending_files = sum(count_pending_files(db, area) for area in all_areas)
        return {
            "pending_users": pending_users,
            "pending_posts": pending_posts,
            "pending_files": pending_files,
            "name_requests": count_open_username_requests(db),
            "open_pages": count_open_pages(db),
            # Dogfood follow-up: the dashboard previously showed only
            # *pending* counts (always 0 on a quiet node) with no sense
            # of overall node scale at all -- a SysOp glancing at the
            # landing screen couldn't tell how many users/boards/posts/
            # files actually exist.
            "total_users": len(all_users),
            "total_boards": len(all_boards),
            "total_posts": sum(count_listed_posts(db, board)[0] for board in all_boards),
            "total_areas": len(all_areas),
            "total_files": sum(count_listed_files(db, area)[0] for area in all_areas),
            **_link_health_snapshot(db, link_context),
            "backup": _get_display_backup_summary(db),
            "next_backup": _get_display_next_backup(db),
            "update": get_display_check_summary(db),
            "description_level": menu_description_level(db, actor),
            "redraw_in_place": redraw_in_place_enabled(db, actor),
            "unicode_style": unicode_style_enabled(db, actor),
            "collapsed": breadcrumb_collapsed_enabled(db, actor),
            "header_color": effective_header_color_256(db),
            "node_running": node_controls is None and running_node_pid(db.path) is not None,
            **_away_state(db, actor),
        }

    if state is None:
        state = await lane.run(_load)
    active_sessions = len(node_controls.session_registry) if node_controls is not None else None
    maintenance = node_controls.maintenance.is_active() if node_controls is not None else False
    lockdown = node_controls.maintenance.is_lockdown_active() if node_controls is not None else False

    unicode_style = state["unicode_style"]
    collapsed = state["collapsed"]
    box_inner_width = min(session.terminal_width, 78) - 4
    await session.write_line(
        "\r\n" + screen_title(
            "SysOp operations console",
            breadcrumb=crumbs(session, own=True),
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
        # No configured session-capacity limit exists anywhere in this
        # codebase to give a gauge a real denominator -- a `max(10,
        # active_sessions)` placeholder made the bar permanently 100%-
        # full (and red) for any count above 10, an identical "at
        # capacity" alarm for 11 sessions and 10,000. An unbounded count
        # has no natural gauge; show the number alone.
        health.append(f"  {counts_row([('Active sessions', active_sessions)])}")
    else:
        standalone_lines = _wrap_panel_sentence(
            _offline_console_note(bool(state.get("node_running"))),
            prefix="  ", width=box_inner_width, unicode_style=unicode_style,
        )
        health.extend(colored(f"  {line}", fg_color=MUTED_COLOR) for line in standalone_lines)

    if link_context is None:
        # `node_controls is None` is this module's own established signal
        # for "running through the standalone admin CLI, which opens its
        # own DB handle and never observes the live node process" (see
        # the identical check just above, for `active_sessions`) -- a
        # standalone session genuinely cannot tell whether the live node
        # actually has Link configured or not, so labeling it "DISABLED"
        # the same as a real, observed disabled config would mislead a
        # SysOp into diagnosing a configuration problem that may not
        # exist (Codex follow-up).
        link_badge_text = "UNAVAILABLE" if node_controls is None else "DISABLED"
        health.append(colored("LINK  ", fg_color=LABEL_COLOR, bold=True) + status_badge(link_badge_text, tone="neutral", unicode_style=unicode_style))
    else:
        node = link_context.link_node
        link_tone = "warning" if not node.peers or state["dead_letters"] else "success"
        link_label = "ATTENTION" if link_tone == "warning" else "HEALTHY"
        health.append(colored("LINK  ", fg_color=LABEL_COLOR, bold=True) + status_badge(link_label, tone=link_tone, unicode_style=unicode_style))
        health.extend(
            _wrap_counts_panel(
                "  ",
                _link_count_pairs(node, state),
                width=box_inner_width,
            )
        )

    health.append(colored("CONTENT", fg_color=LABEL_COLOR, bold=True))
    health.extend(
        _wrap_counts_panel(
            "  ",
            [
                ("Users", state["total_users"]),
                ("Message boards", state["total_boards"]),
                ("Posts", state["total_posts"]),
                ("File areas", state["total_areas"]),
                ("Files", state["total_files"]),
            ],
            width=box_inner_width,
        )
    )

    pending_total = state["pending_users"] + state["pending_posts"] + state["pending_files"]
    health.append(colored("ATTENTION", fg_color=LABEL_COLOR, bold=True))
    # No gauge here: there's no real moderation-queue capacity to measure
    # against (same reasoning as the active-session count above) -- a
    # `max(10, pending_total)` denominator would just make the bar
    # permanently "full" past 10 pending items, same bug as the session
    # gauge this file already dropped for.
    health.append("  " + counts_row([("Moderation", pending_total)]) + " pending")
    health.extend(
        _wrap_counts_panel(
            "    ",
            [("Users", state["pending_users"]), ("Posts", state["pending_posts"]), ("Files", state["pending_files"]),
             *_carry_attention_pairs(state)],
            width=box_inner_width,
        )
    )
    # Decision 1377: callers' /sysop pages, worked under [P]ages.
    health.append("  " + counts_row([("Pages", state.get("open_pages", 0))]) + " open")
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
    def _fit(text: str, prefix_len: int) -> str:
        return cut_to_width(text, box_inner_width - prefix_len) if unicode_style else text

    health.append(
        "  Backup: "
        + (sanitize_text(_fit(backup_at, 10)) if backup_at else colored("never", fg_color=WARNING_COLOR))
    )
    if state.get("next_backup") is not None:
        health.append("  Next backup: " + sanitize_text(_fit(state["next_backup"], 15)))
    health.append(
        "  Update check: "
        + (
            sanitize_text(_fit(update_outcome or update_at or "completed", 17))
            if update_at
            else colored("never", fg_color=WARNING_COLOR)
        )
    )
    if link_context is not None:
        health.extend(
            _wrap_counts_panel(
                "  ",
                [("Recent Link errors", state["recent_errors"]), ("warnings", state["recent_warnings"])],
                width=box_inner_width,
            )
        )

    console, quick = _admin_menu_entries(node_controls, link_context)
    # The landing page is the one console screen that had no answer to a short
    # terminal: a twelve-row health panel over a described two-section menu is
    # 32 rows, so on the classic 24 its own title and the top of the panel had
    # scrolled away before `Choice:` appeared. The sub-consoles already compact
    # their panels and drop descriptions (`_degrade_description_level`); here
    # the same two steps are *measured* rather than estimated -- the full panel
    # with the menu as asked for, then the compact panel, then the compact
    # panel over an undescribed menu -- and the first that fits is drawn.
    def _menu(level: str) -> str:
        return menu_grid(
            [("Console", console), ("Quick", quick)],
            width=session.terminal_width, height=session.terminal_height, description_level=level,
        )

    framed = unicode_style and min(session.terminal_width, 78) >= 4
    title_rows = 3 + (0 if state["redraw_in_place"] else 1)

    def _rows(panel: list[str], menu: str) -> int:
        return title_rows + len(panel) + (2 if framed else 0) + 1 + menu.count("\r\n") + 1 + 1

    compact = _compact_dashboard_panel(
        state, node_badge=node_badge, active_sessions=active_sessions, link_context=link_context,
        node_controls=node_controls, unicode_style=unicode_style, width=box_inner_width,
    )
    # Design doc §5.6: your own away notice, on every landing -- the reminder
    # that ends a notice nobody dated.
    away_lines = _away_panel_lines(state, width=box_inner_width, unicode_style=unicode_style)
    health.extend(away_lines)
    compact.extend(away_lines)
    level = state["description_level"]
    # Before hiding the descriptions, each on its entry's own line (issue #840).
    fallbacks = ((health, level), (compact, level), (compact, "inline"), (compact, "off"))
    for panel, menu_level in fallbacks if level != "off" else ((health, level), (compact, level)):
        menu = _menu(menu_level)
        if _rows(panel, menu) <= session.terminal_height:
            break
    await _write_panel(session, panel, unicode_style=unicode_style, header_color=state["header_color"])
    await session.write_line("\r\n" + menu)
    if menu_level == "off" and level != "off" and "Descriptions hidden" not in menu:
        await session.write_line(
            colored("Descriptions hidden -- terminal too short to show them.", fg_color=MUTED_COLOR)
        )
    await _choice_prompt(session)
    return state


def _admin_menu_entries(
    node_controls: NodeControls | None, link_context: LinkContext | None
) -> tuple[list[MenuEntry], list[MenuEntry]]:
    """The landing page's two menu sections, shared by its draw and its help."""
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
    quick = [
        MenuEntry(label=menu_key("F", "ull backups"), brief="Create and review complete backups"),
        MenuEntry(label=menu_key("D", "NS"), brief="Managed netbbs.org name status"),
        MenuEntry(label=menu_key("T", "ime away"), brief="Tell members you're away"),
        MenuEntry(label=menu_key("P", "ages"), brief="Callers asking for the SysOp"),
    ]
    if node_controls is not None:
        quick.insert(
            0,
            MenuEntry(label=menu_key("N", "ode"), brief="Sessions, shutdown, and drain"),
        )
    if link_context is not None:
        quick.extend([
            MenuEntry(label=menu_key("L", "ink status"), brief="NetBBS Link peer/network health"),
            MenuEntry(label=menu_key("Q", "ueue (outbox)"), brief="Pending outgoing Link work items"),
        ])
    return console, quick


def _compact_dashboard_panel(
    state: dict[str, object], *, node_badge: str, active_sessions: int | None,
    link_context: LinkContext | None, node_controls: NodeControls | None, unicode_style: bool, width: int,
) -> list[str]:
    """The landing page's health panel with each group on one row -- the same
    facts, in the same colours, in six rows instead of twelve. Every row is
    wrapped to the frame by `_wrap_counts_panel`, as the sub-consoles' own
    compact panels are."""
    def _label(text: str) -> str:
        return colored(f"{text:<11}", fg_color=LABEL_COLOR, bold=True)

    panel: list[str] = []
    if active_sessions is not None:
        panel.extend(_wrap_counts_panel(
            _label("NODE") + node_badge + "  ", [("Active sessions", active_sessions)], width=width,
        ))
    else:
        panel.append(_label("NODE") + node_badge)
        panel.extend(
            colored(f"  {line}", fg_color=MUTED_COLOR)
            for line in _wrap_panel_sentence(
                _offline_console_note(bool(state.get("node_running"))),
                prefix="  ", width=width, unicode_style=unicode_style,
            )
        )
    if link_context is None:
        # See `_draw_admin_menu`'s own check for why `node_controls is None`,
        # not `link_context` alone, decides between these two words.
        panel.append(_label("LINK") + status_badge(
            "UNAVAILABLE" if node_controls is None else "DISABLED", tone="neutral", unicode_style=unicode_style,
        ))
    else:
        node = link_context.link_node
        link_tone = "warning" if not node.peers or state["dead_letters"] else "success"
        panel.extend(_wrap_counts_panel(
            _label("LINK")
            + status_badge("ATTENTION" if link_tone == "warning" else "HEALTHY", tone=link_tone, unicode_style=unicode_style)
            + "  ",
            _link_count_pairs(node, state),
            width=width,
        ))
    panel.extend(_wrap_counts_panel(
        _label("CONTENT"),
        [
            ("Users", state["total_users"]), ("Message boards", state["total_boards"]),
            ("Posts", state["total_posts"]), ("File areas", state["total_areas"]), ("Files", state["total_files"]),
        ],
        width=width,
    ))
    pending_total = state["pending_users"] + state["pending_posts"] + state["pending_files"]
    panel.extend(_wrap_counts_panel(
        _label("ATTENTION") + counts_row([("Moderation", pending_total)]) + " pending  ",
        [("Users", state["pending_users"]), ("Posts", state["pending_posts"]), ("Files", state["pending_files"]),
         *_carry_attention_pairs(state), ("Pages", state.get("open_pages", 0))],
        width=width,
    ))
    backup_at, _backup_path = state["backup"]
    update_at, update_outcome = state["update"]
    label_width = 11

    def _fit(text: str, used: int) -> str:
        return cut_to_width(text, max(1, width - used)) if unicode_style else text

    # Issue #727: with a schedule on, the same row says when the next one
    # runs, rather than a row of its own on the screen with least room.
    next_backup = state.get("next_backup")
    if next_backup is not None:
        backup_value = f"{backup_at or 'never'}  next {next_backup}"
        panel.append(
            _label("BACKUP")
            + colored(sanitize_text(_fit(backup_value, label_width)), fg_color=VALUE_COLOR if backup_at else WARNING_COLOR)
        )
    else:
        panel.append(
            _label("BACKUP")
            + (colored(sanitize_text(_fit(backup_at, label_width)), fg_color=VALUE_COLOR) if backup_at
               else colored("never", fg_color=WARNING_COLOR))
        )
    panel.append(
        _label("UPDATES")
        + (colored(sanitize_text(_fit(update_outcome or update_at or "completed", label_width)), fg_color=VALUE_COLOR)
           if update_at else colored("never checked", fg_color=WARNING_COLOR))
    )
    if link_context is not None:
        panel.extend(_wrap_counts_panel(
            _label("LINK LOG"),
            [("Recent errors", state["recent_errors"]), ("warnings", state["recent_warnings"])],
            width=width,
        ))
    return panel


# -- users submenu ---------------------------------------------------------


# -- system submenu ----------------------------------------------------------


def _diagnostics_available(node_controls: NodeControls | None, link_context: LinkContext | None) -> bool:
    """Whether the bounded diagnostic log is worth offering.

    It is written by Link *and* by the MRC bridge (issue #275), and MRC can
    be switched on live without Link -- so on any running node the log is
    worth reading. Operations and Settings' hidden aliases share this one
    gate (issue #732: Settings once required Link alone)."""
    return link_context is not None or node_controls is not None


@located("Operations")
async def _operations_menu(
    session: Session,
    lane: DatabaseLane,
    actor: User,
    *,
    node_controls: NodeControls | None,
    link_context: LinkContext | None,
) -> None:
    """Operational observation and intervention, separate from durable settings."""
    def _load_ops(db: Database) -> dict[str, Any]:
        return {
            "node_running": node_controls is None and running_node_pid(db.path) is not None,
            **_link_health_snapshot(db, link_context),
            "backup": _get_display_backup_summary(db),
            "description_level": menu_description_level(db, actor),
            "unicode_style": unicode_style_enabled(db, actor),
            "collapsed": breadcrumb_collapsed_enabled(db, actor),
            "redraw_in_place": redraw_in_place_enabled(db, actor),
            "header_color": effective_header_color_256(db),
        }

    # A durable snapshot, reloaded only after an action that can actually
    # change one of `_load_ops`'s own DB-backed fields (dead_letters,
    # diagnostics, backup) -- mirrors `_draw_admin_menu`'s own snapshot-
    # reuse discipline. Reloading unconditionally on every redraw (the
    # previous shape here) meant returning from `[N]ode` after scheduling
    # an immediate shutdown/drain re-ran this full DB-lane query before
    # the screen could unwind, reintroducing a cancellable wait into the
    # shutdown path. `active_sessions`/`maintenance`/`lockdown` are never
    # part of this snapshot -- they read `node_controls` directly, in
    # memory, so recomputing them every loop iteration is free.
    state = await lane.run(_load_ops)
    diagnostics_available = _diagnostics_available(node_controls, link_context)
    while True:
        unicode_style = state["unicode_style"]
        collapsed = state["collapsed"]
        redraw_in_place = state["redraw_in_place"]
        header_color = state["header_color"]
        description_level = state["description_level"]

        active_sessions = len(node_controls.session_registry) if node_controls is not None else None
        maintenance = node_controls.maintenance.is_active() if node_controls is not None else False
        lockdown = node_controls.maintenance.is_lockdown_active() if node_controls is not None else False

        await session.write_line(
            "\r\n" + screen_title(
                "Operations",
                breadcrumb=crumbs(session, own=True),
                subtitle="Observe the running node, investigate trouble, and recover work.",
                width=session.terminal_width,
                clear=redraw_in_place,
                unicode_style=unicode_style, collapsed=collapsed,
                header_color=header_color,
                node_name_gradient=session.node_name_gradient,
            )
        )

        node_badge = status_badge(
            "LOCKDOWN" if lockdown else "MAINTENANCE" if maintenance else "ONLINE",
            tone="error" if lockdown else "warning" if maintenance else "success",
            unicode_style=unicode_style,
        ) if node_controls is not None else status_badge("LOCAL ADMIN", tone="neutral", unicode_style=unicode_style)

        panel: list[str] = []
        if session.terminal_height >= 18:
            compact = session.terminal_height < 28 or session.terminal_width < 72
            box_inner_width = min(session.terminal_width, 78) - 4
            backup_at, _backup_path = state["backup"]
            backup_str = sanitize_text(backup_at) if backup_at else colored("never", fg_color=WARNING_COLOR)

            if compact:
                if active_sessions is not None:
                    panel.extend(
                        _wrap_counts_panel(
                            colored("NODE HEALTH: ", fg_color=LABEL_COLOR, bold=True) + node_badge + "  ",
                            [("Active sessions", active_sessions)],
                            width=box_inner_width,
                        )
                    )
                else:
                    panel.append(colored("NODE HEALTH: ", fg_color=LABEL_COLOR, bold=True) + node_badge)
                    standalone_lines = _wrap_panel_sentence(
                        _offline_console_note(bool(state.get("node_running"))),
                        prefix="  ", width=box_inner_width, unicode_style=unicode_style,
                    )
                    panel.extend(colored(f"  {line}", fg_color=MUTED_COLOR) for line in standalone_lines)

                if link_context is None:
                    # See `_draw_admin_menu`'s identical check for why
                    # `node_controls is None` -- not `link_context` alone
                    # -- decides the label here (Codex follow-up).
                    link_badge_text = "UNAVAILABLE" if node_controls is None else "DISABLED"
                    panel.append(colored("LINK OPERATIONS: ", fg_color=LABEL_COLOR, bold=True) + status_badge(link_badge_text, tone="neutral", unicode_style=unicode_style))
                else:
                    node = link_context.link_node
                    link_tone = "warning" if not node.peers or state["dead_letters"] else "success"
                    link_label = "ATTENTION" if link_tone == "warning" else "HEALTHY"
                    panel.extend(
                        _wrap_counts_panel(
                            colored("LINK OPERATIONS: ", fg_color=LABEL_COLOR, bold=True)
                            + status_badge(link_label, tone=link_tone, unicode_style=unicode_style)
                            + "  ",
                            _link_count_pairs(node, state),
                            width=box_inner_width,
                        )
                    )
                backup_pairs = []
                if link_context is not None:
                    # `_load_ops` deliberately skips the diagnostics query
                    # without Link (see its own body) -- reporting these
                    # as a bare "0" here would claim a check that never
                    # happened, exactly like the non-compact branch below
                    # already avoids by omitting the counts entirely.
                    backup_pairs = [("Recent errors", state["recent_errors"]), ("warnings", state["recent_warnings"])]
                # `_wrap_counts_panel` only ever wraps/omits its `pairs`,
                # never its own `label` -- fine for every other label
                # here (a short, fixed heading), but `backup_at` is a
                # full ISO timestamp (~27 columns) once a backup has
                # actually run, wide enough on its own to overflow this
                # frame regardless of `backup_pairs` (worse still with
                # none, the `link_context is None` case, where
                # `_wrap_counts_panel` has nothing to test the label's
                # width against at all and returns it completely as-is).
                # Cut the plain timestamp first, then color -- same
                # order `_fit` above already established for this exact
                # value on the landing page.
                backup_label_prefix, backup_label_suffix = "  Backup: ", "  "
                if backup_at and unicode_style:
                    backup_budget = box_inner_width - visible_width(backup_label_prefix) - visible_width(backup_label_suffix)
                    compact_backup_str = sanitize_text(cut_to_width(backup_at, backup_budget))
                else:
                    compact_backup_str = backup_str
                panel.extend(
                    _wrap_counts_panel(
                        backup_label_prefix + compact_backup_str + backup_label_suffix,
                        backup_pairs, width=box_inner_width,
                    )
                )
            else:
                panel = [colored("NODE HEALTH  ", fg_color=LABEL_COLOR, bold=True) + node_badge]
                if active_sessions is not None:
                    panel.append(f"  {counts_row([('Active sessions', active_sessions)])}")
                else:
                    panel.extend(
                        colored(f"  {line}", fg_color=MUTED_COLOR)
                        for line in _wrap_panel_sentence(
                            _offline_console_note(bool(state.get("node_running"))),
                            prefix="  ", width=box_inner_width, unicode_style=unicode_style,
                        )
                    )

                if link_context is None:
                    link_badge_text = "UNAVAILABLE" if node_controls is None else "DISABLED"
                    panel.append(colored("LINK OPERATIONS  ", fg_color=LABEL_COLOR, bold=True) + status_badge(link_badge_text, tone="neutral", unicode_style=unicode_style))
                else:
                    node = link_context.link_node
                    link_tone = "warning" if not node.peers or state["dead_letters"] else "success"
                    link_label = "ATTENTION" if link_tone == "warning" else "HEALTHY"
                    panel.append(colored("LINK OPERATIONS  ", fg_color=LABEL_COLOR, bold=True) + status_badge(link_label, tone=link_tone, unicode_style=unicode_style))
                    panel.append(
                        "  " + counts_row(
                            _link_count_pairs(node, state)
                        )
                    )
                    panel.append(
                        "  " + counts_row([("Recent Link errors", state["recent_errors"]), ("warnings", state["recent_warnings"])])
                    )
                panel.append("  Backup: " + backup_str)

            await _write_panel(session, panel, unicode_style=unicode_style, header_color=header_color)

        options = _operations_menu_entries(node_controls, link_context, diagnostics_available)

        effective_desc_level, available_menu_height, desc_degraded = _degrade_description_level(
            panel=panel, unicode_style=unicode_style, description_level=description_level,
            entry_count=len(options), terminal_width=session.terminal_width, terminal_height=session.terminal_height,
        )

        await session.write_line(
            _menu_row(
                options, effective_desc_level, width=session.terminal_width,
                height=available_menu_height, degraded=desc_degraded,
            )
        )
        await _choice_prompt(session)
        choice = (await session.read_key()).lower()
        if choice == "b":
            await session.write_line("")
            return
        # Every dispatch branch below except `[N]ode` reloads the
        # snapshot on return: Outbox can change `dead_letters`, Follow
        # Log/Diagnostics can leave newer entries behind after however
        # long the SysOp spent watching them, and a fresh backup can be
        # triggered from Backup status -- none of them share Node
        # control's own reason to skip the reload (a real DB-lane wait
        # sitting in the shutdown/drain path itself), so unlike that one
        # branch there's no cost to staying accurate here (Codex
        # follow-up: the first cut of this fix over-corrected to
        # "reload only after Outbox", leaving every other action's
        # numbers stale until the SysOp happened to also visit Outbox
        # or re-enter Operations from scratch).
        if choice == "n" and node_controls is not None:
            await _node_menu(session, lane, actor, node_controls)
        elif choice == "l" and link_context is not None:
            await _link_status_screen(session, lane, actor, link_context=link_context, node_controls=node_controls)
            state = await lane.run(_load_ops)
        elif choice == "o" and link_context is not None:
            await _outbox_screen(session, lane, actor)
            state = await lane.run(_load_ops)
        elif choice == "d" and diagnostics_available:
            await _diagnostic_log_screen(session, lane, actor)
            state = await lane.run(_load_ops)
        elif choice == "w" and diagnostics_available:
            await _diagnostic_log_tail_screen(session, lane)
            state = await lane.run(_load_ops)
        elif choice == "r" and link_context is not None:
            await _repair_carried_posts_screen(session, lane, actor)
            state = await lane.run(_load_ops)
        elif choice == "f":
            await _backup_status_screen(session, lane, actor, node_controls=node_controls)
            state = await lane.run(_load_ops)
        elif choice == "p":
            await _prune_drafts_screen(session, lane, actor)
            state = await lane.run(_load_ops)
        elif choice == "s":
            await _search_indexes_screen(session, lane, actor)
            state = await lane.run(_load_ops)
        elif choice == "a":
            await _audit_log_screen(session, lane, actor)
            state = await lane.run(_load_ops)
        elif choice == "e":
            await _node_log_screen(session, lane, actor)
            state = await lane.run(_load_ops)
        elif choice == "m":
            await _mail_tools_screen(session, lane, actor, link_context=link_context)
            state = await lane.run(_load_ops)
        elif choice == HELP_KEY:
            await show_menu_help(
                session, "Operations help",
                _operations_menu_entries(node_controls, link_context, diagnostics_available),
                about="Watch the running node and fix trouble: sessions, backups, logs, mail and Link. "
                      "Durable settings live under Settings instead.",
                header_color=header_color, unicode_style=unicode_style,
            )
        else:
            await session.write(reject_unhandled_key(choice))


def _operations_menu_entries(
    node_controls: NodeControls | None, link_context: LinkContext | None, diagnostics_available: bool
) -> list[MenuEntry]:
    """The Operations screen's menu, shared by its draw and its help."""
    options = [
        MenuEntry(label=menu_key("F", "ull backups"), brief="Create and review complete backups"),
        MenuEntry(label=menu_key("P", "rune drafts"), brief="Clean up old unsaved drafts"),
        MenuEntry(label=menu_key("S", "earch indexes"), brief="Check and rebuild Find's indexes"),
        MenuEntry(label=menu_key("A", "udit log"), brief="Moderation and admin actions"),
        MenuEntry(label=menu_key("E", "rror log"), brief="Warnings and errors in netbbs.log"),
        MenuEntry(label=menu_key("M", "ail"), brief="Mailbox sizes, refused Link mail"),
    ]
    if node_controls is not None:
        options.insert(0, MenuEntry(label=menu_key("N", "ode and sessions"), brief="Sessions, shutdown, and drain"))
    if link_context is not None:
        options.extend([
            MenuEntry(label=menu_key("L", "ink status"), brief="NetBBS Link peer/network health"),
            MenuEntry(label=menu_key("O", "utbox"), brief="Pending outgoing Link work items"),
        ])
    if diagnostics_available:
        options.extend([
            MenuEntry(label=menu_key("D", "iagnostics"), brief="Recent Link and MRC diagnostics"),
            MenuEntry(label=menu_key("W", "atch log"), brief="Live-tail the diagnostic log"),
        ])
    if link_context is not None:
        options.append(
            MenuEntry(label=menu_key("R", "epair carried posts"), brief="Fix inconsistent carried posts")
        )
    options.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to the SysOp console"))
    return options


# -- trust policy (Phase 4, issue #129) -------------------------------------


# -- create ------------------------------------------------------------


# -- list / detail -------------------------------------------------------


# -- self-service registration settings (design doc) -----------


# -- self-update (design doc §17) --


# -- backup status (design doc §13.4, issue #60's first operational slice) --


# -- node-wide display format/timezone -------------------------------------


# -- Link status (issue #60, narrow scope) -----------------------------------


# -- carry decisions: offered and excluded resources (design doc §9.3, --------
# -- issue #683) ---------------------------------------------------------------


# -- node keys: guided operational-key rotation (design doc §4.5, ------------
# -- issue #624) -------------------------------------------------------------


# -- outbox: work-item inspection/replay/cancel (design doc §13.7, ----------
# -- issue #60's second operational slice) ----------------------------------


# -- node management (design doc) -------------------------------------------


# -- maintenance mode and drain (design doc §13.8) --------------------------


# -- banners & mastheads umbrella (issue #178) -----------------------------
#
# Every banner/masthead singleton -- welcome, logoff, new-account
# before/after, main-menu, board list, file areas, chat channel picker --
# reachable through this one Settings entry, grouped into two
# subsections (Banners, Mastheads) that mirror this file's own
# menu-within-menu convention (`_theme_colors_menu` inside `_system_menu`).
# Replaces the flat `[W]elcome banner`/`[M]asthead` top-level rows and
# folds in the "Session banners" (issue #177) and "Section mastheads"
# (issue #176) interim groupings, which named this exact reorg as their
# own eventual destination. Each leaf screen's own enable/disable/
# preview/edit shape is unchanged -- only the navigation path to reach
# it moved.


# -- welcome banner (design doc -- part one of a three-part skinning
# initiative) ----------------------------------------------------------


# -- main-menu masthead (issue #161 -- part two of the three-part
# skinning initiative the welcome banner above calls itself part one
# of) ---------------------------------------------------------------


# -- banners (issue #178) ------------------------------------------------
#
# Welcome banner plus the three session-point banners added by issue
# #177 (logoff, new-account before/after), all four independent
# singletons on the same mechanism (colocated .ans file, node_config
# enabled flag, size cap, silent fallback). The three #177 banners
# originally shipped with no [G]allery/[F]rom disk -- issue #169's own
# curated preset library didn't extend to them yet, so preview/enable/
# disable/edit alone covered their acceptance criteria. Dogfood follow-
# up: each surface now has its own curated preset collection. Their
# timing and purpose are different enough that generic main-menu strips
# made misleading and visually repetitive lifecycle banners.


# -- logoff banner --------------------------------------------------------


# -- new-account banner (before signup) ------------------------------------


# -- new-account banner (after signup) -------------------------------------


# -- mastheads (issue #178) -----------------------------------------------
#
# Main-menu masthead (issue #161) plus the three section mastheads added
# by issue #176 (board list, file areas, chat channel picker, via
# netbbs.net.picker.pick_item's own `masthead` parameter), all four
# independent singletons on the same mechanism.


# -- board list masthead --------------------------------------------------


# -- file area masthead ----------------------------------------------------


# -- chat channel picker masthead -------------------------------------------


# -- node colors (issue #162) ------------------------------------------


# -- boards & areas (design doc) ---------------------------------------
#
# Boards and file areas share an identical schema shape and permission
# model (BoardPermission is reused for both object_type='board' and
# 'file_area', see netbbs.moderation.roles) but diverge in terminology
# ("post" vs "file", max_post_age_days vs max_file_age_days) -- written
# as two structurally-parallel but separately-coded sections here,
# matching this file's existing style for user management rather than
# building a shared abstraction for just two call sites.


@located("Content")
async def _content_menu(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext | None = None,
    mrc_bridge: MrcBridge | None = None,
    chat_hub: ChatHub | None = None,
    door_services: Any = None,
    transfers: Any = None,
    backup_identity_dir: Path | None = None,
) -> None:
    def _load_stats(db: Database) -> dict[str, Any]:
        all_boards = list_boards(db)
        all_areas = list_file_areas(db)
        channels = list_channels(db)
        doors = list_doors(db)
        communities = list_communities(db)
        # Read only, as on the dashboard (issue #681).
        pending_posts = sum(count_pending_posts(db, b) for b in all_boards)
        pending_files = sum(count_pending_files(db, a) for a in all_areas)
        total_posts = sum(count_listed_posts(db, b)[0] for b in all_boards)
        total_files = sum(count_listed_files(db, a)[0] for a in all_areas)
        return {
            "total_boards": len(all_boards),
            "total_posts": total_posts,
            "total_areas": len(all_areas),
            "total_files": total_files,
            "total_channels": len(channels),
            "total_doors": len(doors),
            "total_communities": len(communities),
            "pending_posts": pending_posts,
            "pending_files": pending_files,
            "description_level": menu_description_level(db, actor),
            "unicode_style": unicode_style_enabled(db, actor),
            "collapsed": breadcrumb_collapsed_enabled(db, actor),
            "redraw_in_place": redraw_in_place_enabled(db, actor),
            "header_color": effective_header_color_256(db),
        }

    stats = await lane.run(_load_stats)
    await _draw_content_menu(session, stats=stats)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "m":
            await session.write_line("")
            await _board_menu(session, lane, actor, link_context=link_context)
            stats = await lane.run(_load_stats)
            await _draw_content_menu(session, stats=stats)
        elif choice == "f":
            await session.write_line("")
            await _area_menu(session, lane, actor, link_context=link_context, transfers=transfers)
            stats = await lane.run(_load_stats)
            await _draw_content_menu(session, stats=stats)
        elif choice == "d":
            await session.write_line("")
            await _door_menu(session, lane, actor, door_services=door_services,
                             backup_identity_dir=backup_identity_dir)
            stats = await lane.run(_load_stats)
            await _draw_content_menu(session, stats=stats)
        elif choice == "l":
            await session.write_line("")
            await _channel_menu(session, lane, actor, link_context=link_context, mrc_bridge=mrc_bridge, chat_hub=chat_hub)
            stats = await lane.run(_load_stats)
            await _draw_content_menu(session, stats=stats)
        elif choice == "o":
            from netbbs.net.bot_flow import bot_menu
            await bot_menu(session, lane, actor)
            stats = await lane.run(_load_stats)
            await _draw_content_menu(session, stats=stats)
        elif choice == "c":
            await session.write_line("")
            await _category_menu(session, lane, actor)
            stats = await lane.run(_load_stats)
            await _draw_content_menu(session, stats=stats)
        elif choice == "t":
            await session.write_line("")
            await _community_menu(session, lane, actor, link_context=link_context)
            stats = await lane.run(_load_stats)
            await _draw_content_menu(session, stats=stats)
        elif choice == "g":
            await session.write_line("")
            await _grant_moderator_screen(session, lane, actor)
            stats = await lane.run(_load_stats)
            await _draw_content_menu(session, stats=stats)
        elif choice == "r":
            await session.write_line("")
            await _revoke_moderator_screen(session, lane, actor)
            stats = await lane.run(_load_stats)
            await _draw_content_menu(session, stats=stats)
        elif choice == "p":
            await session.write_line("")
            await _pending_review_screen(session, lane, actor, link_context=link_context, transfers=transfers)
            stats = await lane.run(_load_stats)
            await _draw_content_menu(session, stats=stats)
        elif choice == HELP_KEY:
            await show_menu_help(
                session, "Content help", _CONTENT_MENU,
                about="Everything callers read, download, play and chat in, who moderates it, and "
                      "what waits for approval.",
                header_color=stats["header_color"], unicode_style=stats["unicode_style"],
            )
            await _draw_content_menu(session, stats=stats)
        else:
            await session.write(reject_unhandled_key(choice))


_CONTENT_MENU = (
    MenuEntry(label=menu_key("M", "essage boards"), brief="Create/edit message boards"),
    MenuEntry(label=menu_key("F", "ile areas"), brief="Create/edit file areas"),
    MenuEntry(label=menu_key("D", "oors"), brief="Register/edit door games"),
    MenuEntry(label=menu_key("L", "ive chat"), brief="Create/edit chat channels"),
    MenuEntry(label=menu_key("O", "pen chat bots"), brief="Bots, status and kill switches"),
    MenuEntry(label=menu_key("C", "ategories"), brief="Group lists of one kind"),
    MenuEntry(label=menu_key("T", "opics"), brief="Communities holding every kind"),
    MenuEntry(label=menu_key("G", "rant moderator"), brief="Grant a moderation scope"),
    MenuEntry(label=menu_key("R", "evoke moderator"), brief="Revoke a moderation scope"),
    MenuEntry(label=menu_key("P", "ending review"), brief="Posts and files awaiting approval"),
    MenuEntry(label=menu_key("B", "ack"), brief="Return to the SysOp console"),
)


async def _draw_content_menu(session: Session, *, stats: dict[str, Any]) -> None:
    unicode_style = stats["unicode_style"]
    collapsed = stats["collapsed"]
    redraw_in_place = stats["redraw_in_place"]
    header_color = stats["header_color"]
    description_level = stats["description_level"]

    await session.write_line(
        "\r\n"
        + screen_title(
            "Manage message boards/file areas/chat channels",
            breadcrumb=crumbs(session, own=True),
            subtitle="Content repositories, categories, and moderation permissions.",
            width=session.terminal_width,
            clear=redraw_in_place,
            unicode_style=unicode_style,
            collapsed=collapsed,
            header_color=header_color,
            node_name_gradient=session.node_name_gradient,
        )
    )

    panel: list[str] = []
    if session.terminal_height >= 18:
        compact = session.terminal_height < 28 or session.terminal_width < 72
        box_inner_width = min(session.terminal_width, 78) - 4
        pending_total = stats["pending_posts"] + stats["pending_files"]
        if compact:
            panel.extend(
                _wrap_counts_panel(
                    colored("CONTENT: ", fg_color=LABEL_COLOR, bold=True),
                    [
                        ("Message boards", stats["total_boards"]),
                        ("Posts", stats["total_posts"]),
                        ("File areas", stats["total_areas"]),
                        ("Files", stats["total_files"]),
                        ("Channels", stats["total_channels"]),
                    ],
                    width=box_inner_width,
                )
            )
            if pending_total > 0:
                # No gauge here: there's no real moderation-queue capacity
                # to measure against -- a `max(10, pending_total)`
                # denominator would just make the bar permanently "full"
                # past 10 pending items, the same fake-capacity bug this
                # file already dropped for the active-session gauge.
                # `pending_total` is caller-driven (a real backlog can
                # reach 3+ digits), so route it through the same
                # width-aware wrapping the rest of this panel already
                # uses rather than a bare `panel.append` (Codex
                # follow-up, PR #197 review).
                panel.extend(
                    _wrap_counts_panel(
                        colored("MODERATION QUEUE: ", fg_color=LABEL_COLOR, bold=True),
                        [("Pending review", pending_total)],
                        width=box_inner_width,
                    )
                )
            else:
                gauge = telemetry_gauge(0, 10, unicode_style=unicode_style, tone="capacity")
                panel.append(
                    colored("MODERATION QUEUE: ", fg_color=LABEL_COLOR, bold=True)
                    + counts_row([("Pending review", 0)])
                )
                panel.append(
                    f"  {gauge}  "
                    + colored("All clear", fg_color=SUCCESS_COLOR)
                )
        else:
            panel = [
                colored("CONTENT REPOSITORY", fg_color=LABEL_COLOR, bold=True),
                "  "
                + counts_row([("Message boards", stats["total_boards"]), ("Posts", stats["total_posts"])]),
                "  "
                + counts_row([("File areas", stats["total_areas"]), ("Files", stats["total_files"])]),
                "  "
                + counts_row([("Chat channels", stats["total_channels"]), ("Doors", stats["total_doors"]), ("Communities", stats["total_communities"])]),
                colored("MODERATION QUEUE", fg_color=LABEL_COLOR, bold=True),
            ]
            if pending_total > 0:
                # No gauge here -- see the compact branch above for why.
                panel.append("  " + counts_row([("Pending review", pending_total)]))
                panel.append("    " + counts_row([("Posts", stats["pending_posts"]), ("Files", stats["pending_files"])]))
            else:
                gauge = telemetry_gauge(0, 10, unicode_style=unicode_style, tone="capacity")
                panel.append(f"  {counts_row([('Pending review', 0)])}  {gauge}  " + colored("All clear", fg_color=SUCCESS_COLOR))

        await _write_panel(session, panel, unicode_style=unicode_style, header_color=header_color)

    entries = list(_CONTENT_MENU)
    effective_desc_level, available_menu_height, desc_degraded = _degrade_description_level(
        panel=panel, unicode_style=unicode_style, description_level=description_level,
        entry_count=len(entries), terminal_width=session.terminal_width, terminal_height=session.terminal_height,
    )

    await session.write_line(
        _menu_row(
            entries,
            effective_desc_level,
            width=session.terminal_width,
            height=available_menu_height,
            degraded=desc_degraded,
        )
    )
    await _choice_prompt(session)


# -- FieldSpec adapters (design doc, dogfood feature request) --------
#
# Thin wrappers turning this module's own existing per-type prompt
# helpers (above) into the `netbbs.net.resource_editor.FieldPrompt`
# shape a draft-based create/edit screen needs: read/parse, then
# mutate `draft[key]` in place on success, leave it untouched
# (draft-preserving, not screen-aborting) on a rejected entry -- unlike
# every one of these helpers' own original callers, which used to
# treat a `False` `ok` as "abandon the entire in-progress wizard."


# -- Communities (design doc §16) ------------------


# -- message boards ----------------------------------------------------


# -- What actually applies here (issue #528, Codex review) ------------
#
# A board, area or channel that leaves a gate unset inherits its
# Community's, and enforcement reads the inherited value
# (`get_effective_min_age` / `get_effective_name_requirement`, the same
# functions `netbbs.net.file_flow` already gates entry on). A list that
# rendered the resource's own raw `None` therefore printed "-" against a
# resource callers genuinely cannot enter -- the very failure this issue
# was filed about, reintroduced one level up the cascade.
#
# So every list row is built from *effective* values throughout. A row
# answers "who can get in here"; the editor behind `[E]` is where a
# SysOp sees which of those values this resource sets for itself. Levels
# follow the same rule rather than still saying "inherit", since one
# column saying "inherit" beside another silently resolving its
# inherited value would be the more confusing half-measure.


# -- Columnar resource lists (issue #528) -----------------------------
#
# `_board_description` above, and its file-area/channel/Community
# siblings, are kept as-is and still used: `pick_item` falls back to
# them on a terminal too narrow for a table. The column definitions
# below are the wide-terminal form of the same information, plus the
# gates that never fitted into a one-line sentence at all.


# -- file areas ----------------------------------------------------------


# -- doors (issue #172) ------------------------------------------------------
#
# Deliberately flat -- no categories, no age/name-requirement gates, no
# Community-inheritance for its one level field (see netbbs.doors.
# registry's own docstring for why: doors have neither a stated Link
# future nor a read/write split, so this section is noticeably shorter
# than boards'/areas' own.


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


# -- categories ----------------------------------------------------------


# -- moderator grants -----------------------------------------------------
