"""SysOp console user screens.

The users menu, levels, gates and promotion rules, creating, listing and
editing users, staff permissions and registration settings. Split out of
`netbbs.net.admin_flow` (issue #1311).
"""

from __future__ import annotations

import datetime
import dataclasses
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable

from netbbs.access_map import (
    AccountChange,
    Gate,
    GateKind,
    GATE_WORDS,
    LadderStep,
    account_level_change,
    gate_source,
    ladder_summary,
    level_change,
    level_ladder,
    list_gates,
)
from netbbs.attestation import (
    AttestationError,
    ProfileFieldError,
    change_birthdate,
    change_display_name,
    get_attestation,
    get_birthdate,
    get_display_name,
    revoke_attestation,
)
from netbbs.auth.users import (
    CO_SYSOP_PRESET,
    STAFF_PERMISSION_LABELS,
    SYSOP_LEVEL,
    AuthError,
    StaffPermission,
    User,
    UserManagementError,
    UsernameRetiredError,
    approve_pending_user,
    decline_pending_user,
    count_sysops,
    create_user,
    delete_user,
    deletion_retires_username,
    get_user_by_id,
    has_password,
    list_users,
    describe_staff_permissions,
    check_user_level_change,
    is_usable_sysop,
    set_automatic_promotion,
    set_can_verify_identity,
    set_staff_permissions,
    set_user_disabled,
    set_user_level,
)
from netbbs.boards.boards import list_boards
from netbbs.chat.channels import list_channels
from netbbs.config import RegistrationMode, get_registration_mode, set_registration_mode
from netbbs.level_names import (
    LevelNameError,
    get_level_names,
    level_label,
    parse_level,
    set_level_name,
)
from netbbs.promotion import (
    PromotionRule,
    PromotionRuleError,
    count_qualifying,
    get_promotion_rules,
    kept_from_rules,
    save_promotion_rules,
)
from netbbs.doors import list_doors
from netbbs.files.areas import list_file_areas
from netbbs.identity.keys import IdentityError, parse_verify_key
from netbbs.link.boards import LinkContext
from netbbs.moderation.blocklist import BlocklistError, block_user, is_blocked, unblock_user
from netbbs.moderation.log import list_actions_for_target_user, record_action
from netbbs.moderation.roles import describe_grant, list_grants_for_user
from netbbs.net.char_input import (
    HELP_KEY,
    InputCancelled,
    EditorKey,
    EditorKeyKind,
    help_key_label,
    reject_unhandled_key,
)
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.help_overlay import show_help, show_menu_help
from netbbs.net.location import crumbs, entered, located
from netbbs.net.picker import ListColumn, pick_item as _pick_item
from netbbs.net.resource_editor import (
    editing_in_place,
    inline_field,
    write_field_prompt,
    write_field_message,
    FieldSpec,
    text_field,
)
from netbbs.net.session import Session, write_prompt
from netbbs.net.shutdown import NodeControls
from netbbs.net.password_screen import manage_password_screen
from netbbs.net.profile_flow import _edit_profile
from netbbs.net.ssh_key_screen import manage_ssh_keys_screen
from netbbs.auth.signup_answers import (
    MAX_REGISTRATION_QUESTION_LENGTH,
    get_registration_question,
    load_signup_answer,
    set_registration_question,
)
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.redraw_preference import (
    start_new_account_redrawing_in_place,
    redraw_in_place_enabled,
)
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.node_theme import effective_accent_color_256, effective_header_color_256
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.permissions.levels import meets_level
from netbbs.rendering import (
    ALERT_COLOR,
    AUTHOR_COLOR,
    DATE_COLOR,
    ERROR_COLOR,
    LABEL_COLOR,
    METADATA_COLOR,
    MUTED_COLOR,
    SUCCESS_COLOR,
    VALUE_COLOR,
    WARNING_COLOR,
    MenuEntry,
    colored,
    counts_row,
    highlight_hotkeys,
    menu_key,
    sanitize_text,
    screen_title,
    telemetry_gauge,
)
from netbbs.rendering.detail import Field, Note, Section, Table
from netbbs.guest import is_guest_account
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display, resolve_display_preferences
from netbbs.net.admin_ui import (
    _BACK_ACTION,
    _announce,
    _announce_line,
    _choice_prompt,
    _confirmation_refused,
    _degrade_description_level,
    _detail_title,
    _fitted_menu,
    _load_chrome,
    _menu_row,
    _pending_notice_rows,
    _show_report,
    _wrap_counts_panel,
    _wrap_panel_sentence,
    _write_panel,
    _write_sections,
    _yes_no,
    edit_resource_draft,
    pick_item,
    show_detail,
)
from netbbs.net.admin_fields import (
    _EDIT_HINT,
    _int_field,
    _read_seeded_line,
    _resolve_redraw_preference,
)
from netbbs.net.admin_trust import _help_entries, _retired_usernames_screen
from netbbs.net.admin_renames import rename_account_directly, username_requests_screen
from netbbs.auth.renames import count_open_username_requests, open_username_request, username_history
from netbbs.net.admin_sessions import _request_live_access_recheck, _revoke_live_sessions
from netbbs.net.admin_boards import _board_detail_screen
from netbbs.net.admin_files import _area_detail_screen
from netbbs.net.admin_chat import _channel_detail_screen
from netbbs.net.admin_doors import _door_detail_screen
from netbbs.welcome_letter import send_welcome_letter


def _can_verify_label(target: User) -> str:
    """The account's "Can verify identity" row. A SysOp verifies by level
    (issue #1103), so the row says so rather than a bare "no" that reads as
    if they could not."""
    if meets_level(target, SYSOP_LEVEL):
        return "yes, as SysOp (age/name attestation)"
    return f"{_yes_no(target.can_verify_identity)} (age/name attestation)"


def _co_sysop_question(username: str) -> str:
    """The Co-SysOp preset's yes/no question. Verifying identity is not part
    of the preset (maintainer decision, issue #1103): vouching for a
    caller's age or name travels to other nodes and opens adult areas, so a
    SysOp grants it to a helper on its own -- with `[V]erify identity` on
    the staff screen this question is asked from (issue #1115), not with a
    key on the account screen the SysOp is not looking at. Not a second
    question after this one: AGENTS.md allows no dialog chains."""
    return (
        f"Make {username!r} a Co-SysOp -- approve accounts, manage accounts (disable, "
        "password reset, display names and birthdates, levels up to 254) and moderate "
        "everything? They can't act on "
        "SysOps or other staff, or reach Settings, Link, Node, DNS or backups, and members "
        "see them under Operators. Verifying identity is separate: [V]erify identity "
        "on this screen."
    )


def _verify_identity_question(username: str, giving: bool) -> str:
    """`[V]erify identity`'s yes/no on the staff screen (issue #1115)."""
    if giving:
        return (
            f"Let {username!r} verify identity? They could then vouch for callers' "
            "ages and real names, which other nodes may trust."
        )
    return f"Stop {username!r} from verifying identity?"


@located("Users")
async def _users_menu(
    session: Session, lane: DatabaseLane, actor: User, *, node_controls: NodeControls | None,
    link_context: LinkContext | None = None,
) -> None:
    """Every user-account action, grouped together (design doc): create,
    list/detail, registration policy, promote/demote, enable/disable,
    delete. `[U]ser list`/`[P]romote/demote`/`[E]nable/disable`/
    `[D]elete user` all route through the same `_pick_and_edit_user` ->
    `_user_detail_screen` central editor now (design doc -- node
    management, Thiesi's own dogfood-testing report), differing only in
    the picker's own title text. `node_controls` is threaded straight
    through to that editor -- it needs it for the live-session-
    revocation guard on disable/delete -- this submenu itself doesn't
    use it directly."""
    def _load_stats(db: Database) -> dict[str, Any]:
        all_users = list_users(db)
        total = len(all_users)
        pending = sum(u.pending_approval for u in all_users)
        disabled = sum(u.disabled_at is not None for u in all_users)
        sysops = count_sysops(db)
        active = sum(not u.pending_approval and u.disabled_at is None for u in all_users)
        return {
            "total": total,
            "active": active,
            "pending": pending,
            "name_requests": count_open_username_requests(db),
            "disabled": disabled,
            "sysops": sysops,
            "description_level": menu_description_level(db, actor),
            "unicode_style": unicode_style_enabled(db, actor),
            "collapsed": breadcrumb_collapsed_enabled(db, actor),
            "redraw_in_place": redraw_in_place_enabled(db, actor),
            "header_color": effective_header_color_256(db),
        }

    stats = await lane.run(_load_stats)
    await _draw_users_menu(session, stats=stats)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "c":
            await session.write_line("")
            await _create_user_screen(session, lane, actor)
            stats = await lane.run(_load_stats)
            await _draw_users_menu(session, stats=stats)
        elif choice == "u":
            await session.write_line("")
            await _pick_and_edit_user(session, lane, actor, node_controls, title="Registered users")
            stats = await lane.run(_load_stats)
            await _draw_users_menu(session, stats=stats)
        elif choice == "r":
            await session.write_line("")
            await _registration_settings_screen(session, lane, actor)
            stats = await lane.run(_load_stats)
            await _draw_users_menu(session, stats=stats)
        elif choice == "p":
            await session.write_line("")
            await _pick_and_edit_user(session, lane, actor, node_controls, title="Promote/demote which user?")
            stats = await lane.run(_load_stats)
            await _draw_users_menu(session, stats=stats)
        elif choice == "e":
            await session.write_line("")
            await _pick_and_edit_user(session, lane, actor, node_controls, title="Enable/disable which user?")
            stats = await lane.run(_load_stats)
            await _draw_users_menu(session, stats=stats)
        elif choice == "d":
            await session.write_line("")
            await _pick_and_edit_user(session, lane, actor, node_controls, title="Delete which user?")
            stats = await lane.run(_load_stats)
            await _draw_users_menu(session, stats=stats)
        elif choice == "a":
            await session.write_line("")
            await _promotion_rules_screen(session, lane, actor)
            stats = await lane.run(_load_stats)
            await _draw_users_menu(session, stats=stats)
        elif choice == "l":
            await session.write_line("")
            await _levels_screen(session, lane, actor, node_controls=node_controls, link_context=link_context)
            stats = await lane.run(_load_stats)
            await _draw_users_menu(session, stats=stats)
        elif choice == "h":
            await session.write_line("")
            await _retired_usernames_screen(session, lane, actor)
            stats = await lane.run(_load_stats)
            await _draw_users_menu(session, stats=stats)
        elif choice == "n":
            # Issue #1306: callers' requests for a new username, beside the
            # pending registrations.
            await session.write_line("")
            await username_requests_screen(session, lane, actor, node_controls)
            stats = await lane.run(_load_stats)
            await _draw_users_menu(session, stats=stats)
        elif choice == HELP_KEY:
            await show_menu_help(
                session, "Users help", _users_menu_entries(),
                about="Counts: every account, those that can log in (Active), signups waiting for "
                      "approval (Pending), disabled accounts and SysOps. User list, Promote/demote, "
                      "Enable/disable and Delete all open the account list; each change is made on the "
                      "account's own screen.",
                header_color=stats["header_color"], unicode_style=stats["unicode_style"],
            )
            await _draw_users_menu(session, stats=stats)
        else:
            await session.write(reject_unhandled_key(choice))


def _users_menu_entries() -> list[MenuEntry]:
    """The Users screen's menu, shared by its draw and its help."""
    return [
        MenuEntry(label=menu_key("C", "reate user"), brief="Add a new user account"),
        MenuEntry(label=menu_key("U", "ser list"), brief="Browse and edit accounts"),
        MenuEntry(label=menu_key("R", "egistration"), brief="Signup policy settings"),
        MenuEntry(label=menu_key("P", "romote/demote"), brief="Change a user's level"),
        MenuEntry(label=menu_key("L", "evels (LAST)"), brief="What each level opens"),
        MenuEntry(label=menu_key("A", "uto-promotion rules"), brief="Raise new accounts automatically"),
        MenuEntry(label=menu_key("E", "nable/disable"), brief="Toggle account access"),
        MenuEntry(label=menu_key("D", "elete user"), brief="Permanently remove a user"),
        MenuEntry(label=menu_key("N", "ame requests"), brief="Callers asking for a new username"),
        MenuEntry(label=menu_key("H", "eld names"), brief="Names held after delete or rename"),
        MenuEntry(label=menu_key("B", "ack"), brief="Return to the SysOp console"),
    ]


_LADDER_COLUMNS = [
    ListColumn("users", 5, VALUE_COLOR, align_right=True),
    ListColumn("opens here", 46, VALUE_COLOR),
]


_LEVEL_VIEWS = {
    "new": "New at this level",
    "open": "Everything open",
    "closed": "Still closed",
    "conditions": "Open, with other gates",
}


_LEVEL_VIEW_ORDER = tuple(_LEVEL_VIEWS)


# What kind of thing each row is, in a word that fits its column (dogfood:
# the access word alone left a SysOp guessing whether "read" meant a board).
_GATE_TYPE_WORDS = {
    "board": "board", "file area": "area", "channel": "channel", "door": "door", "node-wide": "node",
}


# Kept inside 80 columns with a name still worth reading; ALSO gives way
# first, and the narrow-terminal line (`_gate_description`) keeps it whole.
_GATE_COLUMNS = [
    ListColumn("type", 7, MUTED_COLOR),
    ListColumn("access", 8, VALUE_COLOR),
    ListColumn("level", 5, VALUE_COLOR, align_right=True),
    ListColumn("from", 16, MUTED_COLOR),
    ListColumn("also", 12, MUTED_COLOR),
]


def _gate_stable_id(gate: Gate) -> int:
    return list(GateKind).index(gate.kind) * 10_000_000 + (gate.object_id or 0)


def _gate_columns(gate: Gate) -> list[str]:
    action, what = GATE_WORDS[gate.kind]
    also = [f"off: {gate.off}"] if gate.off else []
    also += list(gate.conditions)
    if gate.note:
        also.append(gate.note)
    return [_GATE_TYPE_WORDS[what], action, str(gate.opens_at), gate_source(gate), ", ".join(also)]


def _gate_description(gate: Gate) -> str:
    """The table's columns as one line, for a terminal too narrow for the
    table: `board: post at 10, Community Market; age 18+`."""
    kind, action, level, source, also = _gate_columns(gate)
    return f"{kind}: {action} at {level}, {source}" + (f"; {also}" if also else "")


def _gates_in_view(gates: list[Gate], level: int, view: str) -> list[Gate]:
    if view == "new":
        return [g for g in gates if g.off is None and g.opens_at == level]
    if view == "open":
        return [g for g in gates if g.opens_for(level)]
    if view == "closed":
        return [g for g in gates if not g.opens_for(level)]
    return [g for g in gates if g.opens_for(level) and g.conditions]


async def _open_gate_resource(
    session: Session, lane: DatabaseLane, actor: User, gate: Gate, *,
    node_controls: NodeControls | None, link_context: LinkContext | None,
) -> None:
    """The board's, file area's, channel's or door's own detail screen, the
    one the Content menu opens, so its levels can be changed from here. It
    gets the same live services the Content menu hands it: the chat hub
    that refuses renaming an occupied channel and moves callers out of a
    deleted one, the door supervisor, the transfer grants."""
    from netbbs.doors.registry import list_doors

    what = GATE_WORDS[gate.kind][1]
    listing = {"board": list_boards, "file area": list_file_areas, "channel": list_channels, "door": list_doors}[what]
    resource = next((r for r in await lane.run(listing) if r.id == gate.object_id), None)
    if resource is None:
        _announce_line(session, colored("That no longer exists.", fg_color=MUTED_COLOR))
        return
    controls = node_controls
    if what == "board":
        await _board_detail_screen(session, lane, actor, resource, link_context=link_context)
    elif what == "file area":
        await _area_detail_screen(
            session, lane, actor, resource, link_context=link_context,
            transfers=controls.transfers if controls is not None else None,
        )
    elif what == "channel":
        await _channel_detail_screen(
            session, lane, actor, resource, link_context=link_context,
            mrc_bridge=controls.mrc_bridge if controls is not None else None,
            chat_hub=controls.chat_hub if controls is not None else None,
        )
    else:
        await _door_detail_screen(
            session, lane, actor, resource,
            door_services=controls.door_services if controls is not None else None,
            backup_identity_dir=controls.backup_identity_dir if controls is not None else None,
        )


async def _level_detail_screen(
    session: Session, lane: DatabaseLane, actor: User, level: int, *,
    node_controls: NodeControls | None, link_context: LinkContext | None,
) -> None:
    """Design doc §5.7, issue #1007: what one level opens, gate by gate, with
    where each level comes from. `[V]iew` steps through what is new at this
    level, everything open, what stays closed, and what opens with another
    gate still in the way; picking a board, file area, channel or door opens
    its own screen."""
    view = "new"
    start: int | None = None

    async def _load() -> list[Gate]:
        return _gates_in_view(await lane.run(list_gates), level, view)

    async def _next_view() -> list[Gate]:
        nonlocal view
        view = _LEVEL_VIEW_ORDER[(_LEVEL_VIEW_ORDER.index(view) + 1) % len(_LEVEL_VIEW_ORDER)]
        return await _load()

    chrome = await _load_chrome(lane, actor)
    names = await lane.run(get_level_names)
    with entered(session, f"Level {level_label(level, names)}"):
        users = await lane.run(
            lambda db: db.connection.execute(
                "SELECT COUNT(*) FROM users WHERE user_level = ? AND disabled_at IS NULL AND pending_approval = 0",
                (level,),
            ).fetchone()[0]
        )
        while True:
            picked = await _pick_item(
                session, await _load(),
                name_of=lambda gate: gate.name + (" (hidden)" if gate.hidden else ""),
                stable_id_of=_gate_stable_id,
                columns=_GATE_COLUMNS,
                column_values_of=_gate_columns,
                description_of=_gate_description,
                title=f"Level {level_label(level, names)}", own=True,
                help_about=(
                    "What this level opens, gate by gate: the type, the access, the level it opens at, where "
                    "that level comes from, and other gates still in the way. [V]iew steps through four "
                    "views; picking a board, area, channel or door opens its own screen to change its levels."
                ),
                empty_message=f"Nothing in this view of level {level}.",
                refresh=_load,
                live_keys={"v": _next_view},
                live_nav=[MenuEntry(label=menu_key("V", "iew"), brief="New, open, closed, other gates")],
                live_label=lambda: f"Showing: {_LEVEL_VIEWS[view]}, {users} account{'s' if users != 1 else ''} at {level}",
                selectable_of=lambda gate: gate.object_id is not None,
                start_stable_id=start,
                description_level=await lane.run(menu_description_level, actor),
                redraw_in_place=chrome.redraw_in_place,
                unicode_style=chrome.unicode_style,
                collapsed=chrome.collapsed,
                accent_color=chrome.accent_color,
                header_color=chrome.header_color,
            )
            if picked is None:
                return
            start = _gate_stable_id(picked)
            await _open_gate_resource(
                session, lane, actor, picked, node_controls=node_controls, link_context=link_context
            )


@located("Levels")
async def _levels_screen(
    session: Session, lane: DatabaseLane, actor: User, *,
    node_controls: NodeControls | None, link_context: LinkContext | None,
) -> None:
    """Design doc §5.7, issue #1007: the level ladder -- every level that
    matters on this node, how many accounts hold it, and what it first
    opens. Picking a level shows what it opens in full; `[G]o to level`
    asks for any level, in use or not."""
    start: int | None = None
    names: dict[int, str] = {}
    status: str | None = None

    async def _load() -> list[LadderStep]:
        nonlocal names
        ladder, names = await lane.run(lambda db: (level_ladder(db), get_level_names(db)))
        return ladder

    async def _name_level(step: LadderStep) -> list[LadderStep] | None:
        """Issue #1009: name, rename or clear a level's name. The name is a
        label; nobody's access changes."""
        nonlocal status
        if step.level >= SYSOP_LEVEL:
            status = f"{SYSOP_LEVEL} is always called SysOp."
            return None
        await write_field_prompt(
            session, colored(f"Name for level {step.level} (blank clears it):", fg_color=MUTED_COLOR),
            hint=_EDIT_HINT,
        )
        try:
            raw = await _read_seeded_line(session, initial=names.get(step.level, ""))
        except InputCancelled:
            return None
        try:
            updated = await lane.run(set_level_name, step.level, raw, changed_by=actor)
        except LevelNameError as exc:
            status = str(exc)
            return None
        name = updated.get(step.level)
        status = f"Level {step.level} is now called {name}." if name else f"Level {step.level} has no name now."
        return await _load()

    def _status_line() -> str:
        return status or "Each row: what that level adds to the ones below it"

    async def _ladder_preset() -> list[LadderStep]:
        # Issue #1504: names, a rule and two gates in one previewed step.
        from netbbs.net.presets import LEVELS_LADDER, run_preset
        await run_preset(session, lane, actor, LEVELS_LADDER)
        return await _load()

    async def _other_level() -> list[LadderStep] | None:
        await write_field_prompt(
            session, colored(f"Which level? (0-{SYSOP_LEVEL}, or a level's name)", fg_color=MUTED_COLOR),
            hint=_EDIT_HINT,
        )
        try:
            raw = (await _read_seeded_line(session, initial="")).strip()
        except InputCancelled:
            return None
        chosen = parse_level(raw, names)
        if chosen is None or not 0 <= chosen <= SYSOP_LEVEL:
            await session.write("\a")
            return None
        await _level_detail_screen(
            session, lane, actor, chosen, node_controls=node_controls, link_context=link_context
        )
        # A level may have been changed on the way: count again.
        return await _load()

    chrome = await _load_chrome(lane, actor)
    while True:
        picked = await _pick_item(
            session, await _load(),
            name_of=lambda step: level_label(step.level, names),
            stable_id_of=lambda step: step.level,
            columns=_LADDER_COLUMNS,
            column_values_of=lambda step: [str(step.users), ladder_summary(step)],
            description_of=lambda step: (
                f"{step.users} user{'s' if step.users != 1 else ''}; opens {ladder_summary(step)}"
            ),
            title="Level Admin SysOp Tool (LAST)", own=True,
            help_about=(
                "Each row is a level in use: how many accounts hold it and what it adds to the levels "
                "below. Picking one shows everything it opens. A level's name is only a label; renaming "
                "it changes nobody's access."
            ),
            empty_message="No levels in use.",
            refresh=_load,
            live_keys={"g": _other_level, "a": _ladder_preset},
            item_keys={"n": _name_level},
            live_nav=[
                MenuEntry(label=menu_key("G", "o to level"), brief="What any level opens"),
                MenuEntry(label=menu_key("N", "ame"), brief="Name or rename a level"),
                MenuEntry(label=menu_key("A", "pply levels ladder"), brief="New, Member and a rule to 10"),
            ],
            live_label=_status_line,
            start_stable_id=start,
            description_level=await lane.run(menu_description_level, actor),
            redraw_in_place=chrome.redraw_in_place,
            unicode_style=chrome.unicode_style,
            collapsed=chrome.collapsed,
            accent_color=chrome.accent_color,
            header_color=chrome.header_color,
        )
        if picked is None:
            return
        start = picked.level
        await _level_detail_screen(
            session, lane, actor, picked.level, node_controls=node_controls, link_context=link_context
        )


@dataclass(frozen=True)
class _RuleRow:
    """One row of the Promotion rules screen: the rule, how many accounts it
    would promote at their next login, and what its step opens."""

    rule: PromotionRule
    qualifying: int
    opens: str


_RULE_COLUMNS = [
    ListColumn("needs", 22, VALUE_COLOR),
    ListColumn("ready", 5, VALUE_COLOR, align_right=True),
    ListColumn("opens", 24, MUTED_COLOR),
]


def _load_rule_rows(db: Database) -> tuple[list[_RuleRow], dict[int, str]]:
    gates = list_gates(db)
    rows = []
    for rule in get_promotion_rules(db):
        gained = level_change(gates, rule.from_level, rule.to_level).gained
        opens = ladder_summary(LadderStep(rule.to_level, 0, gained)) if gained else "nothing"
        rows.append(_RuleRow(rule, count_qualifying(db, rule), opens))
    return rows, get_level_names(db)


class _RuleDraftError(PromotionRuleError):
    pass


async def _edit_promotion_rule(
    session: Session, lane: DatabaseLane, actor: User, existing: PromotionRule | None,
) -> PromotionRule | None:
    """Create or edit one rule (issue #992). Five values are a draft (§3.5)."""
    draft: dict = dict(asdict(existing)) if existing is not None else {
        "from_level": 0, "to_level": 10, "min_age_hours": 24, "min_logins": 2, "min_posts": 0,
    }
    names = await lane.run(get_level_names)
    fields = [
        FieldSpec(
            key="from_level", label="From level",
            render=lambda d: level_label(d["from_level"], names),
            prompt=_int_field("from_level", "Level the account is at"), brief="Accounts at this level",
            help="The rule looks at accounts at exactly this level. One rule per level.", section="Step",
        ),
        FieldSpec(
            key="to_level", label="To level",
            render=lambda d: level_label(d["to_level"], names),
            prompt=_int_field("to_level", "Level to raise it to"), brief="Raised to this level",
            help=f"The level the account is raised to; higher than From, at most {SYSOP_LEVEL - 1}.",
            section="Step",
        ),
        FieldSpec(
            key="min_age_hours", label="Account age",
            render=lambda d: f"{d['min_age_hours']} hours",
            prompt=_int_field("min_age_hours", "Hours since the account was created"),
            brief="Hours since signup", help="How long the account must have existed.", section="Needs",
        ),
        FieldSpec(
            key="min_logins", label="Logins",
            render=lambda d: str(d["min_logins"]),
            prompt=_int_field("min_logins", "Logins, this one included"),
            brief="Logins, this one included",
            help="How many times the account must have logged in, counting the login being checked.",
            section="Needs",
        ),
        FieldSpec(
            key="min_posts", label="Posts",
            render=lambda d: str(d["min_posts"]),
            prompt=_int_field("min_posts", "Approved posts"), brief="Approved posts on this node",
            help="Approved posts the account wrote on this node, each counted once however often edited. "
                 "0 asks for none.",
            section="Needs",
        ),
    ]

    async def save(values: dict) -> PromotionRule:
        rule = PromotionRule(**{key: values[key] for key in (
            "from_level", "to_level", "min_age_hours", "min_logins", "min_posts")})

        def _persist(db: Database) -> PromotionRule:
            others = [r for r in get_promotion_rules(db) if existing is None or r.from_level != existing.from_level]
            try:
                save_promotion_rules(db, [*others, rule], changed_by=actor)
            except PromotionRuleError as exc:
                raise _RuleDraftError(str(exc)) from exc
            return rule

        return await lane.run(_persist)

    rule = await edit_resource_draft(
        session, lane,
        title="Edit promotion rule" if existing is not None else "New promotion rule",
        fields=fields, draft=draft, save=save, error_type=_RuleDraftError,
        save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if rule is not None:
        _announce_line(session, f"Saved: {rule.from_level} -> {rule.to_level} ({rule.describe()}).")
    return rule


@located("Promotion rules")
async def _promotion_rules_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """Design doc §4.3, issue #992: the node's promotion rules. Each row says
    what an account needs, how many accounts would be promoted at their next
    login, and what the step opens. `[C]reate` adds a rule, picking one
    edits it, `[D]elete` removes the highlighted one."""
    names: dict[int, str] = {}
    status: str | None = None
    start: int | None = None
    # The picker returns what `[C]reate` made as the pick; that rule was
    # just edited, so it is not opened again.
    created: int | None = None

    async def _load() -> list[_RuleRow]:
        nonlocal names
        rows, names = await lane.run(_load_rule_rows)
        return rows

    async def _create() -> _RuleRow | None:
        nonlocal created
        rule = await _edit_promotion_rule(session, lane, actor, None)
        if rule is None:
            return None
        created = rule.from_level
        return next((row for row in await _load() if row.rule == rule), None)

    async def _delete(row: _RuleRow) -> list[_RuleRow] | None:
        nonlocal status
        await session.write_line("")
        if not await prompt_yes_no(session, f"Delete the rule {row.rule.from_level} -> {row.rule.to_level}?",
                                   default=False):
            return None

        def _persist(db: Database) -> None:
            rules = [r for r in get_promotion_rules(db) if r.from_level != row.rule.from_level]
            save_promotion_rules(db, rules, changed_by=actor)

        await lane.run(_persist)
        status = f"Deleted the rule {row.rule.from_level} -> {row.rule.to_level}."
        return await _load()

    chrome = await _load_chrome(lane, actor)
    while True:
        picked = await _pick_item(
            session, await _load(),
            name_of=lambda row: f"{row.rule.from_level} → {level_label(row.rule.to_level, names)}",
            stable_id_of=lambda row: row.rule.from_level,
            columns=_RULE_COLUMNS,
            column_values_of=lambda row: [row.rule.describe(), str(row.qualifying), row.opens],
            description_of=lambda row: (
                f"needs {row.rule.describe()}; {row.qualifying} ready; opens {row.opens}"
            ),
            title="Promotion rules", own=True,
            help_about=(
                "Each rule raises an account from its From level to its To level once it meets every "
                "need, checked at each login, one step per login. Ready counts who it would raise at "
                "their next login. Picking a rule edits it. Staff, SysOps and levels set by hand are "
                "never raised."
            ),
            empty_message="No promotion rules. [C]reate one: levels then change by hand only.",
            refresh=_load,
            on_create=_create,
            item_keys={"d": _delete},
            live_nav=[MenuEntry(label=menu_key("D", "elete"), brief="Remove a rule")],
            live_label=lambda: status or "Checked at each login, one step per login. Ready: promoted at next login",
            start_stable_id=start,
            description_level=await lane.run(menu_description_level, actor),
            redraw_in_place=chrome.redraw_in_place,
            unicode_style=chrome.unicode_style,
            collapsed=chrome.collapsed,
            accent_color=chrome.accent_color,
            header_color=chrome.header_color,
        )
        if picked is None:
            return
        start = picked.rule.from_level
        if created == picked.rule.from_level:
            created = None
            continue
        rule = await _edit_promotion_rule(session, lane, actor, picked.rule)
        if rule is not None:
            start = rule.from_level


def _name_request_sentence(count: int, unicode_style: bool) -> str:
    """The Users panel's line for callers waiting on a new username (issue
    #1306), beside the registrations awaiting review."""
    prefix = "⚠ " if unicode_style else "! "
    return f"{prefix}{count} name request{'s' if count != 1 else ''} awaiting review ([N]ame requests)"


async def _draw_users_menu(session: Session, *, stats: dict[str, Any]) -> None:
    unicode_style = stats["unicode_style"]
    collapsed = stats["collapsed"]
    redraw_in_place = stats["redraw_in_place"]
    header_color = stats["header_color"]
    description_level = stats["description_level"]

    await session.write_line(
        "\r\n"
        + screen_title(
            "Users",
            breadcrumb=crumbs(session, own=True),
            subtitle="Account administration, registration policy, and access levels.",
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
        if compact:
            panel.extend(
                _wrap_counts_panel(
                    colored("ACCOUNTS: ", fg_color=LABEL_COLOR, bold=True),
                    [
                        ("Total users", stats["total"]),
                        ("Active", stats["active"]),
                        ("Pending", stats["pending"]),
                        ("Disabled", stats["disabled"]),
                        ("SysOps", stats["sysops"]),
                    ],
                    width=box_inner_width,
                )
            )
            # `telemetry_gauge`'s own ` current/total` suffix grows with
            # this node's real account counts (unbounded, unlike the
            # fixed-width bar itself) -- a fixed 10-cell bar plus a
            # 4-digit-vs-4-digit ratio ("1000/1000") already overflows
            # this 36-column compact frame once the "Active ratio: "
            # label is added. A narrower 6-cell bar keeps the same
            # meter legible while leaving enough budget for a
            # established node's real account counts (Codex follow-up,
            # PR #197 review).
            panel.append(
                "  "
                + colored("Active ratio: ", fg_color=METADATA_COLOR)
                + telemetry_gauge(stats["active"], max(1, stats["total"]), width=6, unicode_style=unicode_style, tone="health")
            )
            if stats["pending"] > 0:
                # The non-compact branch below already warns on pending
                # registrations -- compact mode silently dropped the same
                # signal entirely (PR #197 review), not just the glyph
                # (that was finding #9, fixed separately).
                warning_prefix = "⚠ " if unicode_style else "! "
                warning_lines = _wrap_panel_sentence(
                    f"{warning_prefix}{stats['pending']} registration{'s' if stats['pending'] != 1 else ''} awaiting review",
                    prefix="  ", width=box_inner_width, unicode_style=unicode_style,
                )
                panel.extend("  " + colored(line, fg_color=WARNING_COLOR, bold=True) for line in warning_lines)
            if stats.get("name_requests"):
                panel.extend(
                    "  " + colored(line, fg_color=WARNING_COLOR, bold=True)
                    for line in _wrap_panel_sentence(
                        _name_request_sentence(stats["name_requests"], unicode_style),
                        prefix="  ", width=box_inner_width, unicode_style=unicode_style,
                    )
                )
        else:
            panel = [
                colored("ACCOUNTS", fg_color=LABEL_COLOR, bold=True),
                "  "
                + counts_row(
                    [
                        ("Total users", stats["total"]),
                        ("Active", stats["active"]),
                        ("Pending", stats["pending"]),
                        ("Disabled", stats["disabled"]),
                    ]
                ),
                "  "
                + counts_row([("SysOps", stats["sysops"])])
                + "    "
                + colored("Active ratio: ", fg_color=METADATA_COLOR)
                + telemetry_gauge(stats["active"], max(1, stats["total"]), unicode_style=unicode_style, tone="health"),
            ]
            if stats["pending"] > 0:
                warning_prefix = "⚠ " if unicode_style else "! "
                panel.append(
                    "  "
                    + colored(
                        f"{warning_prefix}{stats['pending']} registration{'s' if stats['pending'] != 1 else ''} awaiting review",
                        fg_color=WARNING_COLOR,
                        bold=True,
                    )
                )
            if stats.get("name_requests"):
                panel.append(
                    "  " + colored(
                        _name_request_sentence(stats["name_requests"], unicode_style), fg_color=WARNING_COLOR, bold=True,
                    )
                )

        await _write_panel(session, panel, unicode_style=unicode_style, header_color=header_color)

    entries = _users_menu_entries()
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


def _create_user_password_field() -> Callable[[Session, DatabaseLane, dict], Awaitable[None]]:
    """The new account's password, typed unseen into the field and then
    once more to confirm it. An empty first answer clears it; a mismatch
    keeps what the draft had and says so. This screen's draft is never
    seeded from an existing account, so there is no stored password a
    clear/keep distinction would need to protect."""

    @inline_field
    async def prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        await write_field_prompt(
            session, colored("Password (typed unseen; Enter on an empty line clears it):", fg_color=MUTED_COLOR),
            hint="Password, typed unseen; empty clears it",
        )
        try:
            first = await _read_seeded_line(session, initial="", echo=False)
            if not first:
                draft["password"] = None
                return
            await write_field_prompt(
                session, colored("Confirm password:", fg_color=MUTED_COLOR), hint="Type it again to confirm",
            )
            second = await _read_seeded_line(session, initial="", echo=False)
        except InputCancelled:
            return
        if first != second:
            await write_field_message(
                session, colored("Passwords did not match -- password unchanged.", fg_color=MUTED_COLOR),
            )
            return
        draft["password"] = first

    return prompt


def _create_user_pubkey_field() -> Callable[[Session, DatabaseLane, dict], Awaitable[None]]:
    """`_create_user_password_field`'s counterpart for the public key: pasted
    into the field, an empty line clears it, a key that does not parse
    keeps what the draft had and says why."""

    @inline_field
    async def prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        await write_field_prompt(
            session, colored("Public key (base64, or an ssh-ed25519 line; empty clears it):", fg_color=MUTED_COLOR),
            hint="Paste the key; empty clears it",
        )
        try:
            text = (await _read_seeded_line(session, initial="")).strip()
        except InputCancelled:
            return
        if not text:
            draft["verify_key"] = None
            return
        try:
            draft["verify_key"] = parse_verify_key(text)
        except IdentityError as exc:
            await write_field_message(
                session, colored(f"Could not parse key: {exc} -- key unchanged.", fg_color=MUTED_COLOR),
            )

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
            key="username", label="Username",
            render=lambda d: d.get("username") or "",
            prompt=text_field("username", required=True),
            help="Up to 32 letters, digits, '_', '-' and '.'. Unique ignoring case; a name "
                 "held after a delete or rename (Users > Held names) is refused.",
        ),
        FieldSpec(
            key="password", label="Password",
            render=lambda d: "set" if d.get("password") else "(not set)",
            prompt=_create_user_password_field(),
            help=(
                "An account needs a password, a public key, or both. Type it, unseen, then "
                "again to confirm; an empty answer clears the password from this draft."
            ),
        ),
        FieldSpec(
            key="verify_key", label="Public key",
            render=lambda d: "set" if d.get("verify_key") else "(not set)",
            prompt=_create_user_pubkey_field(),
            help="Lets the account sign in over SSH without a password. Paste an Ed25519 key "
                 "as base64 or a full 'ssh-ed25519 ...' line; blank clears it from this draft.",
        ),
        FieldSpec(
            key="level", label="Starting level",
            render=lambda d: str(d.get("level", 0)),
            prompt=_int_field("level", "Starting level"),
            help=(
                "The level the account starts at, 0 to 255. What each level opens is on "
                "Users > Levels; 255 makes it a SysOp. Promotion rules may raise it later. "
                "An account made here needs no approval and gets the welcome letter."
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
        try:
            new_user = await lane.run(
                create_user, draft["username"], password=draft["password"],
                verify_key=draft["verify_key"], user_level=draft["level"],
            )
        except UsernameRetiredError as exc:
            # Issue #594. The exception's own text is what a remote caller is
            # shown, and deliberately reads as "taken". A SysOp is owed the
            # reason and the way out, and raising the editor's `error_type`
            # keeps the draft open so nothing typed is lost.
            raise AuthError(exc.sysop_detail) from exc
        await lane.run(
            record_action, actor=actor, action="create_user", target_user_id=new_user.id,
            detail=f"created user {new_user.username!r} at level {draft['level']}",
        )
        # Dogfood report: three testers on modern (ANSI-capable) clients
        # never discovered in-place redraw existed, so never turned it
        # on. Every new account starts with it on, however it was made
        # (issue #840).
        await lane.run(start_new_account_redrawing_in_place, new_user)
        # Issue #1352: an account made here gets the welcome letter too.
        await lane.run(send_welcome_letter, new_user)
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
        _announce_line(session, f"Created {new_user.username!r} at level {new_user.user_level}.")
        _announce_line(session,
            colored(
                "In-place redraw is on by default for this account -- they can turn it off in Your profile.",
                fg_color=MUTED_COLOR,
            )
        )


# Mirrors netbbs.net.picker's own reserved-lines/max-page-size budget --
# duplicated rather than imported (that module's own private helpers
# aren't reached into from other modules, the same "duplicate rather
# than reach into another module's private helper" convention
# netbbs.link.files._file_area_from_row's own docstring already states
# for the identical reasoning).
#
# Code review follow-up (PR #216): the condensed status line (issue
# #206) added one more line to this screen's own render below --
# title/subtitle, sorted-by, showing-filter, the status line, nav, and
# the trailing "or type a 2-digit number" prompt -- without a matching
# bump here, a full page pushed the nav/choice prompt past the viewport
# on exactly the terminal heights this budget exists to protect.
_USER_PICKER_RESERVED_LINES = 8


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


_USER_COLUMNS = [
    ListColumn("level", 5, VALUE_COLOR, align_right=True),
    # Wide enough for "pending approval", the longest `_status_label`
    # returns (Codex review). At 10 it truncated to "pending a..." on
    # the one list whose job is scanning exactly this field -- and the
    # prose row it replaced showed the whole thing. A width too narrow
    # for its own longest value is not a column, it is a riddle.
    ListColumn("status", 16, VALUE_COLOR),
]


def _user_columns(user: User) -> list[str | tuple[str, SegmentColor]]:
    """Level and status as their own columns (issue #537, design doc
    §3.6).

    `_user_description` stapled them into one grey string -- and the
    account list is the single place a SysOp most often scans for
    exactly these two fields, so it is the worst place to have left
    them as prose.
    """
    status = _status_label(user)
    return [
        str(user.user_level),
        (status, ALERT_COLOR if status == "disabled" else VALUE_COLOR),
    ]


@dataclass
class _UserListView:
    """How the account list is sorted and filtered, kept while a SysOp
    goes into an account and back out (issue #1109), so Back lands on the
    list as it was left rather than on a freshly sorted one."""

    mode: str = "a"
    descending: bool = False
    visibility: str = "all"
    # The accounts the list showed last, in order: where Back lands when
    # the account just viewed is gone (deleted from its own screen).
    shown_ids: list[int] = dataclasses.field(default_factory=list)


async def _pick_target_user(
    session: Session, lane: DatabaseLane, actor: User, *, title: str, pending_only: bool = False,
    view: _UserListView | None = None, start_stable_id: int | None = None,
) -> User | None:
    """
    The single screen every `[U]sers` submenu entry reaches a target
    account through (design doc -- Thiesi's own dogfood-testing report).

    A `pick_item` call since issue #537. It used to be a hand-rolled
    copy of one: justified when it was written, because it needed live
    sort toggles and a visibility filter and the shared picker could do
    neither -- and then quietly wrong for every month after, because the
    shared picker kept gaining things this screen could not inherit.
    Cursor navigation, `Ctrl-L`, `Ctrl-R`, `Ctrl-H`, reopening on the
    page you left, and columnar rows were all missing here and nowhere
    else. The dogfood question that started it -- "why does the user
    list have no arrow keys?" -- had that one-word answer: because it is
    not the picker.

    The capabilities that forced the fork are in `pick_item` now, and
    the four keys are still four keys. `live_keys` maps a character to a
    callback that returns a new working set, `live_nav` puts the entries
    in the nav row, and `live_label` puts the standing state in the
    trailer beside `Sort:` -- so [A]/[R]/[L] still re-sort in one press
    (the active one again flipping direction) and [V] still cycles
    all -> active-only -> disabled-only.

    Expressing them as `[O]rder`-style prompts was the obvious way to
    fit them into what `pick_item` already had, and it was the wrong
    trade: a SysOp asked for single keystrokes on a fifty-account roster
    by name, and paying for this screen's features with the thing they
    asked for is not a refactor, it is a regression with a tidy diff.

    The filter scopes search too, as it did before -- the whole
    point of hiding a class of accounts is to stop having to reach them
    until the SysOp widens the filter again.
    """
    view = view if view is not None else _UserListView()

    def _load(db: Database) -> list[User]:
        _, ascending_order, descending_order = _USER_SORT_MODES[view.mode]
        users = list_users(db, order_by=descending_order if view.descending else ascending_order)
        if pending_only:
            # The Staff console's accounts waiting for approval (issue #836).
            users = [u for u in users if u.pending_approval]
        if view.visibility == "active_only":
            users = [u for u in users if u.disabled_at is None]
        elif view.visibility == "disabled_only":
            users = [u for u in users if u.disabled_at is not None]
        view.shown_ids = [u.id for u in users]
        return users

    unicode_style = await lane.run(unicode_style_enabled, actor)
    users = await lane.run(_load)
    # Nobody to list at all ends here. A filter kept from the last visit
    # that now matches no one (the one disabled account was just
    # re-enabled) stays on the list instead, which says "No users match
    # that view." and lets [V] widen it again (#1109 review).
    if not users and view.visibility == "all":
        _announce_line(session, "\r\nNo accounts are waiting for approval." if pending_only else "\r\nNo registered users yet.")
        return None

    def _standing_label() -> str:
        """The two lines the old screen kept above its list, on one line
        in the picker's trailer -- where `sort_label` already puts
        exactly this kind of standing state, and where truncation takes
        the boilerplate before it."""
        label, _, _ = _USER_SORT_MODES[view.mode]
        # The arrow, not the word: the direction is the thing a SysOp
        # is checking at a glance, and an arrow reads at a glance.
        # ASCII gets the word, as everywhere else this screen chooses
        # between the two.
        if unicode_style:
            arrow = "↓" if view.descending else "↑"
        else:
            arrow = "desc" if view.descending else "asc"
        return (
            f"Sorted by: {label} {arrow}"
            f", Showing: {_USER_VISIBILITY_LABELS[view.visibility]}"
        )

    def _sort_key(chosen: str):
        async def toggle() -> list[User]:
            # Exactly what the top-level keys did before: the active
            # dimension pressed again reverses it, a different one
            # switches to it and starts ascending.
            if chosen == view.mode:
                view.descending = not view.descending
            else:
                view.mode, view.descending = chosen, False
            return await lane.run(_load)

        return toggle

    async def _cycle_visibility() -> list[User]:
        # One key, forward through a fixed cycle -- not a menu of every
        # value, matching how the sort keys only ever offer "toggle this
        # one" (the original screen's own reasoning, kept).
        position = _USER_VISIBILITY_MODES.index(view.visibility)
        view.visibility = _USER_VISIBILITY_MODES[(position + 1) % len(_USER_VISIBILITY_MODES)]
        return await lane.run(_load)

    async def _reload() -> list[User]:
        """Ctrl-R. The screen claims to inherit it, so it has to have it
        (Codex review) -- `pick_item` rings the bell and keeps the stale
        list when `refresh` is None, and an account list is exactly the
        thing another session changes underneath you."""
        return await lane.run(_load)

    live_keys = {key: _sort_key(key) for key in _USER_SORT_MODES}
    live_keys["v"] = _cycle_visibility
    live_nav = [
        MenuEntry(label=menu_key("A", "lphabetical"), brief="Sort by name"),
        MenuEntry(label=menu_key("R", "egistration"), brief="Sort by join date"),
        MenuEntry(label=menu_key("L", "evel"), brief="Sort by level"),
        MenuEntry(label=menu_key("V", "isibility"), brief="All/active/disabled"),
    ]

    return await pick_item(
        session, users,
        name_of=lambda user: user.username,
        stable_id_of=lambda user: user.id,
        start_stable_id=start_stable_id,
        description_of=_user_description,
        columns=_USER_COLUMNS,
        column_values_of=_user_columns,
        title=title,
        empty_message="No users match that view.",
        refresh=_reload,
        live_keys=live_keys,
        live_nav=live_nav,
        live_label=_standing_label,
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=unicode_style,
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )


def _neighbour_id(ids: list[int], gone: int) -> int | None:
    """The account after `gone` in `ids`, or the one before it when it was
    the last; `None` when it was the only one."""
    if gone not in ids:
        return None
    position = ids.index(gone)
    rest = ids[:position] + ids[position + 1:]
    if not rest:
        return None
    return rest[min(position, len(rest) - 1)]


async def _pick_and_edit_user(
    session: Session, lane: DatabaseLane, actor: User, node_controls: NodeControls | None, *, title: str,
    pending_only: bool = False,
) -> None:
    """
    Every per-user action funnels through here now (design doc -- node
    management, Thiesi's own dogfood-testing report: SysOps wanted one
    central editor rather than picking the same user again through
    three separate single-purpose screens to promote them, then disable
    them, then...). `title` is the only thing that still varies by
    which top-level `[U]sers` menu entry got here -- `[U]ser list`/
    `[P]romote/demote`/`[E]nable/disable`/`[D]elete user` all land on
    the exact same full editor once a user is actually selected, so a
    SysOp who only meant to promote someone can still also disable them
    right there without leaving and re-picking them a second time.
    """
    # Back from an account returns to the list it was picked from, on the
    # same row, sorted and filtered as it was left (issue #1109) -- not to
    # the menu above the list, which made a SysOp working down the roster
    # pick each account's list again from scratch.
    view = _UserListView()
    start_id: int | None = None
    while True:
        target = await _pick_target_user(
            session, lane, actor, title=title, pending_only=pending_only, view=view, start_stable_id=start_id,
        )
        if target is None:
            return
        shown = list(view.shown_ids)
        await _user_detail_screen(session, lane, actor, target, node_controls)
        start_id = target.id
        if await lane.run(get_user_by_id, target.id) is None:
            # Deleted from its own screen: land on the account that took
            # its place in the list, not back at the top (#1109 review).
            start_id = _neighbour_id(shown, target.id)


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
    allowed: frozenset[str] | None = None,
    layout: dict[str, Any] | None = None,
) -> bool:
    """Returns whether `target` is currently on the local blocklist --
    unlike `disabled_at`, blocked status isn't a field on `User` itself,
    so `_user_detail_screen`'s dispatch loop needs it back to know
    which of `[R]estrict`'s two directions a confirmation should offer,
    same shape `_render_profile` already uses to
    hand a caller-needed piece of drawn state back to its own dispatch
    loop.

    `selected` (issue #160's cursor-navigation follow-up), if given one
    of this screen's five field hotkeys (`l`/`t`/`i`/`k`/`r`), highlights
    that line's own `>` cursor -- `None` (nothing arrow-highlighted yet,
    or a `[A]pprove`/`[D]elete`/`[B]ack` action, none of which are
    arrow-selectable fields, matching `edit_resource_draft`'s own
    Save/Back convention) renders identically to before this feature.

    `allowed` (issue #836) narrows the menu to what a staff member may do
    (`_user_detail_keys`); `None` is the SysOp's full screen.

    `layout`, if given, receives where the screen put what an in-place edit
    needs: `fields` maps each editable field's key to its value's 1-based
    screen row, 0-based column and rows, `prompt_row` is the `Choice:` row,
    and `fits` says whether the screen fitted the terminal, without which
    rows counted from the top are not where anything is."""
    title = screen_title(sanitize_text(target.username),
        breadcrumb=crumbs(session, own=True), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
        header_color=await lane.run(effective_header_color_256), node_name_gradient=session.node_name_gradient)
    await session.write_line("\r\n" + title)
    accent = await lane.run(effective_accent_color_256)
    editable: dict[str, Field] = {}

    def _editable(hotkey: str, label: str, value: str, *, color: int = VALUE_COLOR) -> Field:
        editable[hotkey] = Field(label, value, color=color, selected=selected == hotkey, accent=accent)
        return editable[hotkey]

    display_format, display_timezone = await lane.run(resolve_display_preferences)
    member_since = format_for_display(
        target.created_at, override_format=display_format, override_timezone=display_timezone
    )
    status = _status_label(target)
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
    entries = await lane.run(list_actions_for_target_user, target.id)
    age_verified = await lane.run(get_attestation, target, "age") is not None
    name_verified = await lane.run(get_attestation, target, "name") is not None
    # Issue #1119: every field the cursor walks is in one block, top to
    # bottom in `_USER_DETAIL_FIELD_ORDER`, and what can't be changed here
    # follows it. Pairing the account's fields side by side made the cursor
    # zigzag between columns, stepping over read-only rows. One section, not
    # three: their headings cost the rows that let the screen still fit 24
    # now that the fields are one to a row. The account's record continues
    # the block, in its own muted colors, with no heading or blank row of its
    # own: those two rows are what let the key bar line its hotkeys up at
    # 80x24 (maintainer request, 2026-10-09).
    sections = [
        Section("Account", [
            _editable("l", "Level", level_label(target.user_level, await lane.run(get_level_names))),
            _editable("t", "Status", status, color=SUCCESS_COLOR if status == "active" else WARNING_COLOR),
            _editable(
                "r", "Blocked", "yes (local blocklist)" if blocked else "no",
                color=ERROR_COLOR if blocked else VALUE_COLOR,
            ),
            # Issue #1110: the caller's own name and birthdate, which a SysOp
            # or account manager can correct. Shown only to whoever may edit
            # them (review on #1112): a birthdate is private, and an approver
            # reviewing a signup has no need for it.
            *(_detail_fields(
                await lane.run(get_display_name, target),
                await lane.run(get_birthdate, target),
                age_verified=age_verified,
                name_verified=name_verified,
                field=_editable,
            ) if allowed is None or "w" in allowed else ()),
            # Issue #611: whether a password exists, never the password. Beside
            # the key line because the two together are "how this account gets
            # in", and a SysOp resetting one wants to see the other.
            _editable(
                "k", "Public key", target.fingerprint if target.fingerprint else "(none)",
                color=VALUE_COLOR if target.fingerprint else MUTED_COLOR,
            ),
            _editable(
                "p", "Password",
                "set" if await lane.run(has_password, target) else "(none -- key login only)",
            ),
            # Design doc §5.6 (issue #836, F132): what this account may do
            # beyond its level -- the staff permissions, and the
            # verify-identity permission (§5.5) beside them but separate.
            _editable(
                "s", "Staff", describe_staff_permissions(target.staff_permissions),
                color=VALUE_COLOR if target.staff_permissions else MUTED_COLOR,
            ),
            _editable("i", "Can verify identity", _can_verify_label(target)),
            # Issue #992: last, so the arrow order of the fields above holds.
            _editable("u", "Auto promotion", _auto_promotion_label(target, await lane.run(kept_from_rules, target))),
        ]),
    ]
    # The account's record: nothing here is changed on this screen. The
    # admin-action list itself is a screen of its own (`[H]istory`); ten
    # rows of it here pushed the account's fields off a 24-row terminal.
    sections.append(Section(None, [
        Field("Member since", member_since, color=METADATA_COLOR),
        Field(
            "Admin actions", f"{len(entries)} recorded" if entries else "none recorded",
            color=VALUE_COLOR if entries else MUTED_COLOR,
        ),
        _grants_field(await lane.run(_grant_summaries, target)),
    ], paired=True))
    # Issue #1306: the names this account went by, and the one it asks for.
    # Only when there is something to say, so the usual screen keeps its rows.
    former = await lane.run(username_history, target)
    asking = await lane.run(open_username_request, target)
    if former or asking is not None:
        renames: list[Field] = []
        if former:
            renames.append(Field(
                "Formerly", ", ".join(sanitize_text(change.old_username) for change in former),
                color=METADATA_COLOR,
            ))
        if asking is not None:
            renames.append(Field(
                "Asks to be renamed", f"to {sanitize_text(asking.requested_username)} (Users > Name requests)",
                color=WARNING_COLOR,
            ))
        sections.append(Section(None, renames, paired=True))
    # Issue #835 (F072): what the caller said when signing up, for whoever
    # decides on the account. Typed by an unauthenticated caller, so
    # sanitized like any other remote text. Read-only, so below the fields.
    signup_answer = await lane.run(load_signup_answer, target.id) if target.pending_approval else None
    if signup_answer is not None:
        sections.append(Section("Signup answer", [
            Note(f"Asked: {sanitize_text(signup_answer.question)}"),
            Note(f"Answer: {sanitize_text(signup_answer.answer)}"),
        ]))
    places: dict[int, tuple[int, int, int]] = {}
    panel_rows = await _write_sections(session, sections, unicode_style=unicode_style, joined=1, places=places)
    offered = allowed if allowed is not None else _ALL_USER_DETAIL_KEYS
    options = []
    if target.pending_approval and "a" in offered:
        options.append(MenuEntry(label=menu_key("A", "pprove"), brief="Approve this pending signup"))
    if "l" in offered:
        options.append(MenuEntry(label=menu_key("L", "evel"), brief="Change this user's access level"))
    if "u" in offered:
        options.append(MenuEntry(label=menu_key("U", "se promotion rules"), brief="Promotion rules on or off"))
    if "t" in offered:
        options.append(MenuEntry(label=menu_key("T", "oggle enable/disabled"), brief="Enable or disable this account"))
    if "n" in offered:
        options.append(MenuEntry(label=menu_key("N", "ame"), brief="Correct or clear the display name"))
    if "c" in offered:
        options.append(MenuEntry(label=menu_key("C", "hange username"), brief="Rename this account"))
    if "w" in offered:
        options.append(MenuEntry(label=menu_key("W", "hen born"), brief="Correct or clear the birthdate"))
    if "e" in offered:
        options.append(MenuEntry(label=menu_key("E", "dit profile"), brief="Their bio, blocks and settings"))
    if "s" in offered:
        options.append(MenuEntry(label=menu_key("S", "taff"), brief="Staff permissions, Co-SysOp preset"))
    if "i" in offered:
        options.append(MenuEntry(label=menu_key("I", "dentity verification"), brief="Grant/revoke attestation rights"))
    # Issue #1115: only while there is a verification to withdraw.
    if "v" in offered and (age_verified or name_verified):
        options.append(MenuEntry(label=menu_key("V", "erification: revoke"), brief="Withdraw a verified age or name"))
    if "k" in offered:
        options.append(MenuEntry(label=menu_key("K", "ey"), brief="View/replace this user's SSH key"))
    if "p" in offered:
        options.append(MenuEntry(label=menu_key("P", "assword"), brief="Set or clear this user's password"))
    if "r" in offered:
        options.append(MenuEntry(label=menu_key("R", "estrict login"), brief="Block or unblock this account"))
    options.append(MenuEntry(label=menu_key("H", "istory"), brief="Admin actions on this account"))
    if target.pending_approval and "d" in offered:
        # Issue #835: turning down a signup is routine, and used to need
        # the full permanent-delete warning and typed-name confirmation.
        options.append(MenuEntry(label=menu_key("D", "ecline"), brief="Turn down this signup"))
    elif "d" in offered:
        options.append(MenuEntry(label=menu_key("D", "elete"), brief="Permanently remove this user"))
    options.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to the picker"))
    # Above the panel: the title and its rule, and -- unless the screen was
    # cleared, which takes it -- the blank row the title is written after.
    # Below it: the blank row before the menu and the help hint.
    chrome_rows = 4 if redraw_in_place else 5
    menu = _fitted_menu(options, description_level, session=session, used_rows=panel_rows + chrome_rows)
    await session.write_line("\r\n" + menu)
    await session.write_line(colored(f"({help_key_label(session)} for help on these fields)", fg_color=MUTED_COLOR))
    if layout is not None:
        # Counted from the top of a cleared screen: the title, the panel, the
        # blank row and the menu, the help hint, any carried outcome, then
        # `Choice:`. Every row is drawn to fit the width, so none wraps.
        title_rows = title.count("\r\n") + 1
        prompt_row = title_rows + panel_rows + 1 + menu.count("\r\n") + 1 + 1 + _pending_notice_rows(session) + 1
        layout["fields"] = {
            key: (title_rows + places[id(field)][0] + 1, places[id(field)][1], places[id(field)][2])
            for key, field in editable.items() if id(field) in places
        }
        layout["prompt_row"] = prompt_row
        layout["fits"] = redraw_in_place and prompt_row <= session.terminal_height
    await _choice_prompt(session)
    return blocked


_USER_DETAIL_FIELD_ORDER = ("l", "t", "r", "n", "w", "k", "p", "s", "i", "u")


#: Every action key on the account detail -- what a SysOp gets. `c`,
#: renaming the account (issue #1306), is a SysOp's alone, as deleting is:
#: both retire a name.
_ALL_USER_DETAIL_KEYS = frozenset("alutrnwkpsihdvec")


def _auto_promotion_label(target: User, kept: str | None) -> str:
    """Whether the promotion rules (issue #992) may raise this account. The
    on/off is the switch `[U]se promotion rules` flips (a level set by hand turns
    it off); anything else keeping the rules away is said beside it, since
    the switch does not change it."""
    if target.level_set_by_hand:
        return "off (level set by hand)"
    return "on" if kept is None else f"on, but skipped ({kept})"


def _user_detail_keys(actor: User, target: User, *, guest: bool = False) -> frozenset[str]:
    """
    The account-detail actions `actor` is offered on `target` (design doc
    §5.6). A SysOp gets all of them. A staff member gets `[H]istory`, and
    -- only on an account below 255 holding no staff permission -- level,
    enable/disable, password, display name, birthdate (issue #1110) and the
    account's Profile with manage accounts, approve and decline
    with approve accounts. Never keys, the blocklist, staff, identity
    verification, or deleting an account.

    `[E]dit profile` is never offered on your own account, whose Profile is
    on the main menu. On the guest account (`guest`) it sets what every guest
    starts with, which is the whole node's, so only a SysOp is offered it.

    Presentation only: the mutators check again against the database, so
    a permission revoked while this screen is open refuses the action.
    """
    profile = set() if actor.id == target.id else {"e"}
    if is_usable_sysop(actor):
        return _ALL_USER_DETAIL_KEYS - ({"e"} - profile)
    keys = {"h"}
    within_reach = target.user_level < SYSOP_LEVEL and not target.staff_permissions
    if within_reach and actor.has_staff(StaffPermission.MANAGE_ACCOUNTS):
        keys |= {"l", "u", "t", "p", "n", "w"} | (set() if guest else profile)
    if within_reach and target.pending_approval and actor.has_staff(StaffPermission.APPROVE_ACCOUNTS):
        keys |= {"a", "d"}
    return frozenset(keys)


def _detail_fields(
    display_name: str | None,
    birthdate: datetime.date | None,
    *,
    age_verified: bool,
    name_verified: bool,
    field: Callable[..., Field],
) -> list[Field]:
    """The account detail's display name and birthdate (issue #1110), the
    caller's own fields. A verification on record is said in the value,
    since an edit here never changes it and it still decides the gates.
    The display name can come from the caller, so it is sanitized like any
    other text they typed."""
    name = sanitize_text(display_name) if display_name else "(not set)"
    born = birthdate.isoformat() if birthdate else "(not set)"
    return [
        field(
            "n", "Display name", name + ("; real name verified" if name_verified else ""),
            color=VALUE_COLOR if display_name else MUTED_COLOR,
        ),
        field(
            "w", "Birthdate", born + ("; age verified" if age_verified else ""),
            color=VALUE_COLOR if birthdate else MUTED_COLOR,
        ),
    ]


def _grant_summaries(db: Database, target: User) -> list[str]:
    """`target`'s moderator grants in words (`describe_grant`)."""
    return [describe_grant(db, grant) for grant in list_grants_for_user(db, target)]


def _grants_field(summaries: list[str]) -> Field:
    """The account detail's moderator-grant line: how many, and each one
    beneath it. Board and Community names can come from other nodes over
    Link, so they are sanitized like any remote text."""
    if not summaries:
        return Field("Moderator grants", "none", color=MUTED_COLOR)
    return Field(
        "Moderator grants", str(len(summaries)),
        note="; ".join(sanitize_text(summary) for summary in summaries),
    )


async def _user_history_screen(session: Session, lane: DatabaseLane, actor: User, target: User) -> None:
    """Every recorded admin action against `target`, newest first, paged.

    Dogfood follow-up: this list used to show *what* happened but never
    *who* did it, even though `actor_user_id` is stored for exactly this
    (`netbbs.moderation.log.ModerationLogEntry`'s own docstring: nullable
    specifically so a deleted actor's audit trail survives, implying
    display was always the intent) -- the in-channel notice for the same
    action already says "by bob," this screen just never repeated it.

    It was the bottom ten rows of the user detail screen, under the
    account's own fields, and on a 24-row terminal pushed them off the
    top. A screen of its own, it shows all of them."""
    entries = list(reversed(await lane.run(list_actions_for_target_user, target.id)))
    display_format, display_timezone = await lane.run(resolve_display_preferences)
    actor_ids = {entry.actor_user_id for entry in entries if entry.actor_user_id is not None}
    actor_usernames: dict[int, str] = {}
    for actor_id in actor_ids:
        found = await lane.run(get_user_by_id, actor_id)
        actor_usernames[actor_id] = found.username if found is not None else "(deleted account)"

    rows = []
    for entry in entries:
        when = format_for_display(
            entry.created_at, override_format=display_format, override_timezone=display_timezone
        )
        by = actor_usernames.get(entry.actor_user_id, "(unknown)") if entry.actor_user_id is not None else "(system)"
        rows.append([(when, DATE_COLOR), entry.action, (by, AUTHOR_COLOR), (entry.detail or "", METADATA_COLOR)])
    body: list[Field | Note | Table] = (
        [Table(("When", "Action", "By", "Detail"), rows, flex=3)] if rows
        else [Note("No recorded admin actions.")]
    )
    await _show_report(
        session, lane, actor, "Admin actions",
        sections=[Section(None, body)],
        help_title="Admin actions help",
        help_about="Every administrative action taken on this account, newest first.",
    )


async def _read_user_detail_key(session: Session) -> tuple[EditorKey, bool]:
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
    screen never needs a real Backspace at its own top level either.

    The flag says whether the key was echoed (only `read_key` echoes), so
    a rejected key erases only a character that is really on screen."""
    read_editor_key = getattr(session, "read_editor_key", None)
    if read_editor_key is not None:
        try:
            return await read_editor_key(distinguish_ctrl_h=True), False
        except NotImplementedError:
            pass
    raw = await session.read_key()
    return EditorKey(EditorKeyKind.CHAR, char=raw), True


# Ctrl-H's own content for the arrow-selectable fields
# (`_USER_DETAIL_FIELD_ORDER`), shown when one is highlighted. Keyed the
# same as `_USER_DETAIL_FIELD_ORDER`, not a `FieldSpec` list, since this
# screen's status lines are drawn by `_draw_user_detail` directly, not
# through `netbbs.net.resource_editor`.
_USER_DETAIL_HELP: dict[str, tuple[str, str]] = {
    "n": (
        "Display name",
        "The name this caller chose for themselves (Your profile > Name & details). "
        "Correct or clear it here, for a typo or a name that has to go; blank clears it. "
        "The same rules as the caller's own apply, and the change is recorded in the "
        "account's history. A verified real name is a separate record and is not changed.",
    ),
    "w": (
        "Birthdate",
        "The birthdate this caller entered themselves, which age gates use when there is "
        "no verified age. Correct or clear it here; blank clears it. The history records "
        "that it changed, never the date. A verified age is a separate record, is not "
        "changed here, and still decides every age gate.",
    ),
    "u": (
        "Auto promotion",
        "Whether the node's promotion rules (Users > Auto-promotion rules) may raise this "
        "account at its next login. Setting its level by hand turns them off, so a "
        "demotion is not undone; this turns them back on, or off. They never apply to "
        "the guest account, staff, SysOps, or pending and disabled accounts.",
    ),
    "l": (
        "Level",
        "Decides which boards, file areas, channels and doors the account reaches "
        "(Users > Levels shows what each level opens). A change shows what the account "
        "gains and loses before you apply it, and its live sessions follow at once. "
        "Setting it here turns promotion rules off for the account. 255 is SysOp and "
        "only a SysOp gives it; staff can set up to 254.",
    ),
    "t": (
        "Status",
        "Active, pending approval, or disabled. Disabling refuses every login and ends "
        "the account's live sessions; its posts, files and mail stay. Enabling lets it "
        "log in again, unless it is also blocked.",
    ),
    "s": (
        "Staff",
        "What the account may do beyond its level: approve accounts, manage accounts "
        "(level up to 254, disable, password, display name, birthdate, Profile) and "
        "moderate every board, file area and channel. Co-SysOp sets all three. Staff "
        "never act on a SysOp or other staff, and can't grant anything.",
    ),
    "i": (
        "Can verify identity",
        "Lets this account record a caller's verified age or real name. Verified age "
        "opens age-gated areas, and other Link nodes that trust this node may accept it "
        "too. Separate from staff permissions and moderator grants; a SysOp always can.",
    ),
    "k": (
        "Public key",
        "The account's SSH public keys. Opens a screen to add or remove them; a key "
        "lets the account sign in over SSH without a password.",
    ),
    "p": (
        "Password",
        "Whether this account has a password. Opens a screen to set a new one (the old "
        "one is never shown or recovered) or, if the account also has an SSH key, to "
        "remove the password so it signs in by key only. Every change is audit-logged "
        "with your name.",
    ),
    "r": (
        "Blocked",
        "The local blocklist, kept apart from Status: a blocked account is refused at "
        "every login even while it is enabled, and its live sessions end when it is "
        "blocked. Only a SysOp blocks or unblocks; enabling the account does not lift it.",
    ),
    "e": (
        "Edit profile",
        "Opens this member's own Profile screen, acting on their account: bio, "
        "signature, location, who they block, and their display, mail and chat "
        "settings. Each change is recorded in the account's history; private text such "
        "as the bio, and who they block, is recorded as changed, never quoted. Whether a "
        "verification is shared over Link stays their own choice. On the guest account "
        "it sets the guest defaults, what every guest starts with; only a SysOp can.",
    ),
}


# One line per key for the whole-screen help, in the menu's order: what
# pressing it does. A highlighted field's Ctrl-H gives its full text above.
_USER_DETAIL_KEY_HELP: tuple[tuple[str, str, str], ...] = (
    ("a", "[A]pprove", "Pending signup can log in; welcome letter goes"),
    ("l", "[L]evel", "Previews gains and losses, then applies"),
    ("u", "[U]se promotion rules", "Rules may raise this account again, or not"),
    ("t", "[T]oggle enable/disabled", "Disabling ends live sessions; posts stay"),
    ("n", "[N]ame", "Correct or clear their display name"),
    ("c", "[C]hange username", "Rename it; shows what changes before applying"),
    ("w", "[W]hen born", "Correct or clear the birthdate age gates use"),
    ("e", "[E]dit profile", "Their Profile, acting on their account"),
    ("s", "[S]taff", "Staff permissions and the Co-SysOp preset"),
    ("i", "[I]dentity verification", "May record callers' verified age or name"),
    ("v", "[V]erification: revoke", "Withdraw a verified age or real name"),
    ("k", "[K]ey", "Add or remove their SSH public keys"),
    ("p", "[P]assword", "Set a new one, or remove it (key login only)"),
    ("r", "[R]estrict login", "Blocklist: refused at login even if enabled"),
    ("h", "[H]istory", "Every admin action on this account"),
    ("d", "[D]ecline / [D]elete", "Turn down a signup; delete is permanent"),
)


async def _edit_account_detail(
    session: Session, lane: DatabaseLane, actor: User, target: User, choice: str,
    *, run_read: Callable[[Callable[[], Awaitable[str]]], Awaitable[str]] | None = None,
) -> None:
    """Issue #1110: correct or clear `target`'s display name (`n`) or
    birthdate (`w`). The value opens in the line, as the level does; blank
    clears it, Esc keeps it.

    `run_read` runs the prompt and read: the account detail's own puts them
    on the value where it is drawn (`_user_detail_screen`). Without one they
    go below the screen's prompt."""
    if choice == "n":
        current = await lane.run(get_display_name, target) or ""
        label = f"Display name for {target.username!r} (blank clears it, {_EDIT_HINT}):"
    else:
        stored = await lane.run(get_birthdate, target)
        current = stored.isoformat() if stored else ""
        label = f"Birthdate for {target.username!r} as YYYY-MM-DD (blank clears it, {_EDIT_HINT}):"
    async def read() -> str:
        await write_field_prompt(session, colored(label, fg_color=MUTED_COLOR), hint=_EDIT_HINT)
        return (await _read_seeded_line(session, initial=current)).strip()

    try:
        raw = await (run_read or _read_below_prompt(session))(read)
    except InputCancelled:
        return
    if raw == current:
        return
    try:
        if choice == "n":
            changed = await lane.run(change_display_name, target, raw or None, changed_by=actor)
            what = f"Display name for {target.username!r} " + (f"is now {raw!r}." if raw else "cleared.")
        else:
            try:
                new_birthdate = datetime.date.fromisoformat(raw) if raw else None
            except ValueError:
                _announce_line(session, colored("Not a valid date (expected YYYY-MM-DD) -- unchanged.", fg_color=MUTED_COLOR))
                return
            changed = await lane.run(change_birthdate, target, new_birthdate, changed_by=actor)
            what = f"Birthdate for {target.username!r} " + ("updated." if raw else "cleared.")
    except (ProfileFieldError, UserManagementError) as exc:
        _announce_line(session, colored(f"Not changed: {exc}", fg_color=MUTED_COLOR))
        return
    if changed:
        _announce_line(session, what)


def _read_below_prompt(session: Session) -> Callable[[Callable[[], Awaitable[str]]], Awaitable[str]]:
    """A field's prompt and read on the rows below the screen's `Choice:`."""

    async def run(read: Callable[[], Awaitable[str]]) -> str:
        await session.write_line("")
        return await read()

    return run


async def _revoke_verification(session: Session, lane: DatabaseLane, actor: User, target: User) -> None:
    """Issue #1115: withdraw `target`'s verified age or real name. Clearing
    the self-entered birthdate or display name (`n`, `w`) never does this,
    so it is an action of its own, confirmed and recorded. With both on
    record, the SysOp picks one first."""
    age = await lane.run(get_attestation, target, "age")
    name = await lane.run(get_attestation, target, "name")
    if age is None and name is None:
        _announce_line(session, f"No verification on record for {target.username!r}.")
        return
    if age is not None and name is not None:
        while True:
            await write_prompt(
                session, highlight_hotkeys("Revoke which: verified [A]ge, verified real [N]ame, or [B]ack? ")
            )
            which = (await session.read_key()).lower()
            await session.write_line("")
            if which != HELP_KEY:
                break
            await show_menu_help(
                session, "Revoke verification help",
                [MenuEntry(label=menu_key("A", "ge"), brief="Withdraw the verified age"),
                 MenuEntry(label=menu_key("N", "ame"), brief="Withdraw the verified real name")],
                about="Both an age and a real name are verified for this account; choose which to "
                      "withdraw. You are asked to confirm before anything changes.",
                header_color=await lane.run(effective_header_color_256),
                unicode_style=await lane.run(unicode_style_enabled, actor),
            )
        if which not in ("a", "n"):
            return
        attribute = "age" if which == "a" else "name"
    else:
        attribute = "age" if age is not None else "name"
    label = "age" if attribute == "age" else "real name"
    shared = (age if attribute == "age" else name).link_visible
    if not await prompt_yes_no(
        session,
        f"Revoke the verified {label} of {target.username!r}? Anything that asks for a "
        f"verified {label} will refuse them again.",
        default=False,
    ):
        return
    try:
        revoked = await lane.run(revoke_attestation, target, attribute, actor=actor)
    except AttestationError as exc:
        _announce(session, f"Not revoked: {exc}", error=True)
        return
    if revoked:
        extra = " The nodes it was shared with are told at the next sync." if shared else ""
        _announce_line(session, f"Revoked the verified {label} of {target.username!r}.{extra}")
    else:
        # Someone else revoked it while this screen waited for an answer.
        _announce_line(session, f"No verified {label} on record for {target.username!r} any more.")


def _user_detail_help_lines(
    target: User, allowed: frozenset[str], *, verified: bool, header_color: int | tuple[int, int, int],
) -> list[str]:
    """The whole-screen help: what the screen shows, then one row per key
    this screen offers on `target` right now. One row each, so it fits a
    24-row terminal; a highlighted field's Ctrl-H gives the full text."""
    shown = {
        key for key in allowed
        if (key != "a" or target.pending_approval) and (key != "v" or verified)
    }
    rows = []
    for key, label, text in _USER_DETAIL_KEY_HELP:
        if key not in shown:
            continue
        if key == "d":
            label, text = (
                ("[D]ecline", "Turn down this signup and remove it") if target.pending_approval
                else ("[D]elete", "Remove the account permanently; asks its name")
            )
        rows.append((label, text))
    width = max((len(label) for label, _ in rows), default=0)
    lines = [
        "The account's fields, then its record: when it joined, how many admin "
        "actions name it, and its moderator grants.",
        "",
    ]
    lines.extend(f"  {label}{' ' * (width - len(label) + 2)}{text}" for label, text in rows)
    lines.append(colored(
        "Up/Down highlights a field; Ctrl-H then explains it in full.", fg_color=header_color,
    ))
    return lines


async def _show_user_detail_help(
    session: Session, lane: DatabaseLane, target: User, *, selected: str | None,
    allowed: frozenset[str], unicode_style: bool = False,
) -> None:
    """Ctrl-H narrows to the highlighted field if one is selected, as
    `netbbs.net.resource_editor._show_field_help` does; with none, it
    lists this screen's keys one row each."""
    header_color = await lane.run(effective_header_color_256)
    if selected is not None:
        label, help_text = _USER_DETAIL_HELP[selected]
        await show_help(
            session, "Field help", [colored(label, fg_color=header_color, bold=True), f"  {help_text}"],
            header_color=header_color, unicode_style=unicode_style,
        )
        return
    verified = (
        await lane.run(get_attestation, target, "age") is not None
        or await lane.run(get_attestation, target, "name") is not None
    )
    await show_help(
        session, f"{sanitize_text(target.username)} help",
        _user_detail_help_lines(target, allowed, verified=verified, header_color=header_color),
        header_color=header_color, unicode_style=unicode_style,
    )


def _gate_row(gate: Gate, extra: str | None = None) -> tuple[str, str, str]:
    """`(action, name, what)` cells for one gate in a level-change preview."""
    action, what = GATE_WORDS[gate.kind]
    name = gate.name + (" (hidden)" if gate.hidden else "")
    remarks = [text for text in (extra, gate.note) if text]
    return action, name, "; ".join([what, *remarks])


def _level_change_sections(change: AccountChange) -> list[Section]:
    sections = []
    for title, gates in (("Gains", change.gained), ("Loses", change.lost)):
        rows = [_gate_row(gate) for gate in gates]
        sections.append(Section(title, [Table(("", "Name", "What"), rows, flex=1)] if rows else [Note("Nothing.")]))
    if change.blocked:
        rows = [_gate_row(gate, "needs " + ", ".join(fails)) for gate, fails in change.blocked]
        sections.append(Section("Still blocked", [
            Note("The new level opens these, but another gate keeps this account out."),
            Table(("", "Name", "What"), rows, flex=1),
        ]))
    return sections


async def _preview_level_change(
    session: Session, lane: DatabaseLane, actor: User, target: User, change: AccountChange
) -> bool:
    """Design doc §5.7, issue #1006: what a level change opens and closes for
    this account, shown before it is made. Returns whether the SysOp applied
    it; `[B]ack` leaves the level as it was."""
    chrome = await _load_chrome(lane, actor)
    names = await lane.run(get_level_names)
    choice, _page = await show_detail(
        session,
        title=_detail_title(
            session, chrome,
            f"Level {level_label(change.old_level, names)} → {level_label(change.new_level, names)}",
            subtitle=f"What changes for {target.username} by level. Grants and other gates are counted.",
        ),
        sections=_level_change_sections(change),
        actions=[("a", menu_key("A", "pply")), _BACK_ACTION],
        redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
        help_title="Level change help",
        help_about=(
            "What this account gains and loses by moving to the new level, counting its moderator "
            "grants. Still blocked lists what the new level opens but another gate, such as an age "
            "check, keeps closed."
        ),
        help_entries=_help_entries([("a", menu_key("A", "pply"))], {
            "a": "Set the level; promotion rules stop for it",
        }),
    )
    return choice == "a"


async def _change_user_level(
    session: Session, lane: DatabaseLane, actor: User, target: User, new_level: int,
    node_controls: NodeControls | None,
) -> User:
    """Preview, then apply, a level change from the user detail screen.
    A change that opens and closes nothing for the account is applied
    without the preview; the outcome line says so. Returns the account as
    it now stands."""
    try:
        # Refused before it is previewed, not after the SysOp read the preview.
        await lane.run(check_user_level_change, target, new_level, changed_by=actor)
    except UserManagementError as exc:
        _announce_line(session, colored(str(exc), fg_color=MUTED_COLOR))
        return target
    change = await lane.run(account_level_change, target, new_level)
    if not change.changes_nothing and not await _preview_level_change(session, lane, actor, target, change):
        _announce_line(session, colored(f"{target.username!r} stays at level {target.user_level}.", fg_color=MUTED_COLOR))
        return target
    try:
        updated = await lane.run(set_user_level, target, new_level, changed_by=actor)
    except UserManagementError as exc:
        _announce_line(session, colored(str(exc), fg_color=MUTED_COLOR))
        return target
    outcome = f"{updated.username!r} is now level {updated.user_level}."
    if change.changes_nothing:
        outcome += " That opens and closes nothing for them."
    _announce_line(session, outcome)
    _request_live_access_recheck(node_controls, updated)
    return updated


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
    with entered(session, sanitize_text(target.username)):
        description_level = await lane.run(menu_description_level, actor)
        unicode_style = await lane.run(unicode_style_enabled, actor)
        collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
        redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
        selected: str | None = None
        target_is_guest = await lane.run(is_guest_account, target)

        async def _redraw(layout: dict[str, Any] | None = None) -> bool:
            # Recomputed on every draw: an approval or a level change can move
            # the account in or out of a staff member's reach.
            nonlocal allowed, field_order
            allowed = _user_detail_keys(actor, target, guest=target_is_guest)
            field_order = tuple(key for key in _USER_DETAIL_FIELD_ORDER if key in allowed)
            return await _draw_user_detail(
                session, lane, target, description_level, redraw_in_place, unicode_style, collapsed,
                selected=selected, allowed=allowed if allowed != _ALL_USER_DETAIL_KEYS else None, layout=layout,
            )

        def _edit_in_place(hotkey: str) -> Callable[[Callable[[], Awaitable[str]]], Awaitable[str]]:
            """A typed field's prompt and read, on its value where the screen draws
            it: redrawn with the field highlighted, the hint on the `Choice:` row
            and the cursor in the value, as on every Create/Edit screen. A screen
            that scrolls, or does not fit, reads below its prompt as before."""

            async def run(read: Callable[[], Awaitable[str]]) -> str:
                nonlocal blocked, selected
                if redraw_in_place:
                    if hotkey in field_order:
                        selected = hotkey
                    layout: dict[str, Any] = {}
                    blocked = await _redraw(layout)
                    place = layout["fields"].get(hotkey) if layout.get("fits") else None
                    if place is not None:
                        row, column, rows = place
                        with editing_in_place(
                            session, row=row, column=column, rows=rows, prompt_row=layout["prompt_row"],
                        ) as position:
                            try:
                                return await read()
                            finally:
                                # A resize cancels the edit and says so on the next draw.
                                if position.message:
                                    _announce_line(session, colored(position.message, fg_color=MUTED_COLOR))
                await session.write_line("")
                return await read()

            return run

        allowed: frozenset[str] = _ALL_USER_DETAIL_KEYS
        field_order: tuple[str, ...] = _USER_DETAIL_FIELD_ORDER
        blocked = await _redraw()
        while True:
            key, echoed = await _read_user_detail_key(session)

            if key.kind == EditorKeyKind.UP:
                if not field_order:
                    continue
                index = field_order.index(selected) if selected in field_order else 0
                selected = field_order[(index - 1) % len(field_order)]
                blocked = await _redraw()
                continue
            if key.kind == EditorKeyKind.DOWN:
                if not field_order:
                    continue
                index = field_order.index(selected) if selected in field_order else -1
                selected = field_order[(index + 1) % len(field_order)]
                blocked = await _redraw()
                continue
            if key.kind == EditorKeyKind.ESCAPE:
                if selected is not None:
                    selected = None
                    blocked = await _redraw()
                    continue
                await session.write("\a")
                continue
            if key.kind == EditorKeyKind.CTRL and key.char == "h":
                await _show_user_detail_help(
                    session, lane, target, selected=selected, allowed=allowed, unicode_style=unicode_style,
                )
                blocked = await _redraw()
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
                    await _show_user_detail_help(
                        session, lane, target, selected=selected, allowed=allowed, unicode_style=unicode_style,
                    )
                    blocked = await _redraw()
                    continue
                if choice in field_order:
                    selected = choice
            else:
                # Left/Right/Backspace/Tab/Home/End/Page Up/Page Down --
                # nothing on this screen defines a step, same silent no-op
                # `edit_resource_draft` gives Left/Right on a step-less field.
                continue

            if choice == "b":
                await session.write_line("")
                return
            elif choice not in allowed:
                # Design doc §5.6: a staff member sees the account, but only the
                # actions their permissions cover, and none on a SysOp or
                # another staff member.
                await session.write(reject_unhandled_key(choice) if echoed else "\a")
            elif choice == "a" and target.pending_approval:
                await session.write_line("")
                if await prompt_yes_no(session, "Approve this account so it can log in?", default=False):
                    try:
                        target = await lane.run(approve_pending_user, target, approved_by=actor)
                    except UserManagementError as exc:
                        _announce_line(session, colored(str(exc), fg_color=MUTED_COLOR))
                    else:
                        # Issue #1352: approved is when the account can sign
                        # in, so it is when the welcome letter goes.
                        sent = await lane.run(send_welcome_letter, target)
                        _announce_line(session, f"{target.username!r} approved." + (
                            " The welcome letter is in their mailbox." if sent else ""
                        ))
                blocked = await _redraw()
            elif choice == "l":
                # One style for a number (issue #845, F136): the value opens in
                # the line to edit, as on every Create/Edit screen, instead of a
                # `[current]` default beside an empty line -- and, with the screen
                # redrawn in place, on the level itself.
                async def _read_level() -> str:
                    await write_field_prompt(
                        session,
                        colored(f"New level for {target.username!r} ({_EDIT_HINT}):", fg_color=MUTED_COLOR),
                        hint=_EDIT_HINT,
                    )
                    return (await _read_seeded_line(session, initial=str(target.user_level))).strip()

                try:
                    raw = await _edit_in_place("l")(_read_level)
                except InputCancelled:
                    raw = ""
                if raw and raw != str(target.user_level):
                    # A level's name works as well as its number (issue #1009).
                    new_level = parse_level(raw, await lane.run(get_level_names))
                    if new_level is None:
                        _announce_line(session, colored("Not a level or a level's name -- cancelled.", fg_color=MUTED_COLOR))
                    elif new_level != target.user_level:
                        target = await _change_user_level(session, lane, actor, target, new_level, node_controls)
                blocked = await _redraw()
            elif choice == "u":
                # A toggle toggles (design doc §3.5): nothing to confirm.
                try:
                    target = await lane.run(
                        set_automatic_promotion, target, target.level_set_by_hand, changed_by=actor
                    )
                except UserManagementError as exc:
                    _announce_line(session, colored(str(exc), fg_color=MUTED_COLOR))
                else:
                    _announce_line(
                        session,
                        f"Promotion rules {'no longer apply' if target.level_set_by_hand else 'apply again'} "
                        f"to {target.username!r}.",
                    )
                blocked = await _redraw()
            elif choice == "t":
                await session.write_line("")
                currently_disabled = target.disabled_at is not None
                action_word = "Enable" if currently_disabled else "Disable"
                if await prompt_yes_no(session, f"{action_word} {target.username!r}?", default=False):
                    try:
                        target = await lane.run(set_user_disabled, target, not currently_disabled, changed_by=actor)
                    except UserManagementError as exc:
                        _announce_line(session, colored(str(exc), fg_color=MUTED_COLOR))
                    else:
                        _announce_line(
                            session,
                            f"{target.username!r} is now {'disabled' if target.disabled_at is not None else 'active'}.",
                        )
                        if target.disabled_at is not None:
                            await _revoke_live_sessions(session, node_controls, target, actor)
                blocked = await _redraw()
            elif choice in ("n", "w"):
                await _edit_account_detail(session, lane, actor, target, choice, run_read=_edit_in_place(choice))
                blocked = await _redraw()
            elif choice == "c":
                # Issue #1306 (design doc §16 #1111 Decision 1): for what does
                # not start with the caller -- a name chosen to offend, a
                # support request by mail.
                await session.write_line("")
                target = await rename_account_directly(session, lane, actor, target, node_controls)
                blocked = await _redraw()
            elif choice == "v":
                await session.write_line("")
                await _revoke_verification(session, lane, actor, target)
                blocked = await _redraw()
            elif choice == "i":
                await session.write_line("")
                new_state = "revoke" if target.can_verify_identity else "grant"
                if await prompt_yes_no(
                    session, f"{new_state.capitalize()} identity-verification permission?", default=False
                ):
                    try:
                        target = await lane.run(
                            set_can_verify_identity, target, not target.can_verify_identity, changed_by=actor
                        )
                    except UserManagementError as exc:
                        _announce_line(session, colored(str(exc), fg_color=MUTED_COLOR))
                    else:
                        _announce_line(session,
                            f"{target.username!r} can now verify identity: "
                            f"{'yes' if target.can_verify_identity else 'no'}."
                        )
                        _request_live_access_recheck(node_controls, target)
                blocked = await _redraw()
            elif choice == "e":
                # The member's own Profile, acting on their account (design doc
                # §5.6): its writes check this actor again and are recorded.
                await _edit_profile(session, lane, target, actor=actor)
                target = await lane.run(get_user_by_id, target.id) or target
                blocked = await _redraw()
            elif choice == "s":
                target = await _staff_permissions_screen(
                    session, lane, actor, target, node_controls, description_level=description_level,
                    redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
                )
                blocked = await _redraw()
            elif choice == "k":
                # SysOp-assisted counterpart to the self-service Profile
                # `[K]` field (`netbbs.net.login_flow`'s own `_edit_profile`)
                # -- both now open the same shared `manage_ssh_keys_screen`.
                target = await manage_ssh_keys_screen(session, lane, target, changed_by=actor)
                blocked = await _redraw()
            elif choice == "p":
                # Issue #611: the SysOp-assisted counterpart to the Profile
                # `[A]ccount password` field -- the same shared
                # `manage_password_screen`. `changed_by=actor` is what makes
                # it skip the current-password proof for someone else's
                # account, and what the audit row names.
                target = await manage_password_screen(session, lane, target, changed_by=actor)
                blocked = await _redraw()
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
                        _announce_line(session, f"{target.username!r} can log in again.")
                    else:
                        try:
                            await lane.run(block_user, target, blocked_by=actor)
                        except BlocklistError as exc:
                            _announce_line(session, colored(str(exc), fg_color=MUTED_COLOR))
                        else:
                            _announce_line(session, f"{target.username!r} is now blocked from logging in.")
                            await _revoke_live_sessions(session, node_controls, target, actor)
                blocked = await _redraw()
            elif choice == "h":
                await _user_history_screen(session, lane, actor, target)
                blocked = await _redraw()
            elif choice == "d" and target.pending_approval:
                await session.write_line("")
                if await prompt_yes_no(
                    session, f"Decline {target.username!r}'s signup and remove the account?", default=False
                ):
                    try:
                        await lane.run(decline_pending_user, target, declined_by=actor)
                    except UserManagementError as exc:
                        _announce_line(session, colored(str(exc), fg_color=MUTED_COLOR))
                        target = await lane.run(get_user_by_id, target.id) or target
                    else:
                        _announce_line(session, f"{target.username!r}'s signup declined.")
                        return
                blocked = await _redraw()
            elif choice == "d":
                await session.write_line("")
                deleted = await _delete_user_confirm(session, lane, actor, target, node_controls)
                if deleted:
                    return
                blocked = await _redraw()
            else:
                await session.write(reject_unhandled_key(choice) if echoed else "\a")


_STAFF_TOGGLE_KEYS: dict[str, StaffPermission] = {
    "a": StaffPermission.APPROVE_ACCOUNTS,
    "m": StaffPermission.MANAGE_ACCOUNTS,
    "g": StaffPermission.MODERATE_ALL,
}


async def _staff_permissions_screen(
    session: Session, lane: DatabaseLane, actor: User, target: User, node_controls: NodeControls | None,
    *, description_level: str, redraw_in_place: bool = False, unicode_style: bool = True, collapsed: bool = False,
) -> User:
    """
    Give or take `target`'s staff permissions (design doc §5.6, issue
    #836): one toggle each, the Co-SysOp preset that sets all three in one
    confirmed step, and one that removes them all. Every change is
    confirmed, audited by `set_staff_permissions`, and carried into the
    account's live sessions the way a level change is. Returns the account
    as it now stands.

    A screen of its own (issue #1115): with redraw-in-place on it is drawn
    from the top on every pass instead of scrolling a new copy under the
    old one, and each outcome is carried into that redraw above the prompt.
    """
    header_color = await lane.run(effective_header_color_256)
    while True:
        await session.write_line(
            ("" if redraw_in_place else "\r\n")
            + screen_title(
                "Staff permissions",
                breadcrumb=crumbs(session),
                width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style,
                collapsed=collapsed, header_color=header_color, node_name_gradient=session.node_name_gradient,
            )
        )
        options = [
            MenuEntry(label=menu_key("A", "pprove accounts"),
                      brief=_yes_no(target.has_staff(StaffPermission.APPROVE_ACCOUNTS))),
            MenuEntry(label=menu_key("M", "anage accounts"),
                      brief=_yes_no(target.has_staff(StaffPermission.MANAGE_ACCOUNTS))),
            MenuEntry(label=menu_key("G", "lobal moderation"),
                      brief=_yes_no(target.has_staff(StaffPermission.MODERATE_ALL))),
        ]
        # Verifying identity is not a staff permission (§5.5) but a SysOp
        # gives it to the same helpers, so it sits here too (issue #1115):
        # the Co-SysOp preset's outcome points at this key on this screen.
        options.append(MenuEntry(label=menu_key("V", "erify identity"), brief=_yes_no(target.can_verify_identity)))
        options.append(MenuEntry(label=menu_key("C", "o-SysOp preset"), brief="All three at once"))
        options.append(MenuEntry(label=menu_key("N", "one"), brief="Remove every staff permission"))
        options.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to the account"))
        await session.write_line(_fitted_menu(options, description_level, session=session, used_rows=4))
        await _choice_prompt(session)
        choice = (await session.read_key()).lower()
        await session.write_line("")
        if choice == "b":
            return target
        if choice == "v":
            target = await _toggle_verify_identity(session, lane, actor, target, node_controls)
            continue
        if choice == HELP_KEY:
            await show_menu_help(
                session, "Staff permissions help", _STAFF_PERMISSIONS_HELP,
                about="What this account may do beyond its level, yes or no each. Every change "
                      "asks first, is recorded, and reaches the account's live sessions at once. "
                      "Staff never act on a SysOp or other staff and can't grant anything; members "
                      "see staff listed under Operators.",
                header_color=header_color, unicode_style=unicode_style,
            )
            continue
        if choice in _STAFF_TOGGLE_KEYS:
            flag = _STAFF_TOGGLE_KEYS[choice]
            giving = not target.has_staff(flag)
            new_mask = target.staff_permissions | int(flag) if giving else target.staff_permissions & ~int(flag)
            question = (
                f"{'Give' if giving else 'Remove'} {STAFF_PERMISSION_LABELS[flag]} "
                f"{'to' if giving else 'from'} {target.username!r}?"
                + (" Staff are shown to members under Operators." if giving and not target.staff_permissions else "")
            )
        elif choice == "c":
            new_mask = int(CO_SYSOP_PRESET)
            question = _co_sysop_question(target.username)
        elif choice == "n":
            new_mask = 0
            question = f"Remove every staff permission from {target.username!r}?"
        else:
            await session.write(reject_unhandled_key(choice))
            continue
        if new_mask == target.staff_permissions:
            _announce_line(session, colored("Nothing to change.", fg_color=MUTED_COLOR))
            continue
        if not await prompt_yes_no(session, question, default=False):
            continue
        try:
            target = await lane.run(set_staff_permissions, target, new_mask, changed_by=actor)
        except UserManagementError as exc:
            _announce_line(session, colored(str(exc), fg_color=MUTED_COLOR))
            continue
        _announce_line(
            session,
            f"{target.username!r} staff permissions: {describe_staff_permissions(target.staff_permissions)}.",
        )
        if choice == "c" and not target.can_verify_identity:
            # Verifying is not part of the preset (issue #1103); the outcome
            # names the key that grants it on the screen it is shown on
            # (issue #1115).
            _announce_line(session, "To let them verify identity too, use [V]erify identity.")
        _request_live_access_recheck(node_controls, target)


# The staff screen's own briefs are each permission's on/off state; its help
# says what each one lets the account do instead.
_STAFF_PERMISSIONS_HELP = (
    MenuEntry(label=menu_key("A", "pprove accounts"), brief="Approve or decline pending signups"),
    MenuEntry(label=menu_key("M", "anage accounts"), brief="Level, disable, password, name", detailed="Level to 254, disable, password, name, birthdate"),
    MenuEntry(label=menu_key("G", "lobal moderation"), brief="Every board, file area, channel", detailed="Moderate every board, file area and channel"),
    MenuEntry(label=menu_key("V", "erify identity"), brief="Record callers' age or real name", detailed="Record callers' verified age or real name"),
    MenuEntry(label=menu_key("C", "o-SysOp preset"), brief="All three permissions at once"),
    MenuEntry(label=menu_key("N", "one"), brief="Remove every staff permission"),
)


async def _toggle_verify_identity(
    session: Session, lane: DatabaseLane, actor: User, target: User, node_controls: NodeControls | None
) -> User:
    """`[V]erify identity` on the staff screen: the same audited
    `set_can_verify_identity` as `[i]` on the account, one confirmation
    behind the key (§3.5). Returns the account as it now stands."""
    giving = not target.can_verify_identity
    if not await prompt_yes_no(session, _verify_identity_question(target.username, giving), default=False):
        return target
    try:
        target = await lane.run(set_can_verify_identity, target, giving, changed_by=actor)
    except UserManagementError as exc:
        _announce_line(session, colored(str(exc), fg_color=MUTED_COLOR))
        return target
    _announce_line(
        session, f"{target.username!r} can now verify identity: {'yes' if target.can_verify_identity else 'no'}."
    )
    _request_live_access_recheck(node_controls, target)
    return target


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
            "keep their recorded author name; their entries in the node's "
            "previous-callers history also "
            "survive, keeping whatever name-visibility choice was in effect at the "
            "time (a prior opt-out stays hidden -- SysOps still see the real name "
            "regardless); moderator grants, chat channel membership/invitations, "
            "preferences, and blocklist entries tied to this account are removed. "
            "This cannot be undone.",
            fg_color=MUTED_COLOR,
        )
    )
    if await lane.run(deletion_retires_username, target):
        await session.write_line(
            colored(
                f"This node has run NetBBS Link, so the username {target.username!r} stays "
                "retired afterwards: nobody can register it and inherit this account's Link "
                "mail address, authorship and reputation. Users -> Held names releases it.",
                fg_color=MUTED_COLOR,
            )
        )
    await write_prompt(
        session, f"Type the username {target.username!r} to confirm, or anything else to cancel: "
    )
    confirmation = (await session.read_line()).strip()
    if confirmation != target.username:
        _confirmation_refused(session, confirmation, target.username)
        return False
    try:
        await lane.run(delete_user, target, deleted_by=actor)
    except UserManagementError as exc:
        _announce_line(session, colored(str(exc), fg_color=MUTED_COLOR))
        return False
    _announce_line(session, f"{target.username!r} deleted.")
    await _revoke_live_sessions(session, node_controls, target, actor)
    return True


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
    of them individually still happens via `[U]ser list` -> a pending
    account's own detail screen (`_user_detail_screen`'s `[A]pprove` action),
    reusing the existing user-management flow rather than building a
    second, parallel pending-accounts queue UI.

    The screen stays up after a change and shows the mode it now has:
    it used to print the outcome and return, and the Users menu's
    redraw wiped the line before it could be read.
    """
    def _load(db: Database) -> tuple[RegistrationMode, int, str | None]:
        return (
            get_registration_mode(db),
            sum(1 for u in list_users(db) if u.pending_approval),
            get_registration_question(db),
        )

    modes = {"o": RegistrationMode.OPEN, "a": RegistrationMode.APPROVAL_REQUIRED, "c": RegistrationMode.CLOSED}
    chrome = await _load_chrome(lane, actor)
    message: str | None = None
    while True:
        current, pending_count, question = await lane.run(_load)
        rows: list[Field | Note] = [Field("Current mode", _REGISTRATION_MODE_LABELS[current], bold=True)]
        # Issue #835 (F072). Shown in every mode, since a SysOp may set it
        # before switching to approval, but asked only in approval mode.
        rows.append(Field(
            "Signup question",
            sanitize_text(question) if question else "(none)",
            color=VALUE_COLOR if question else MUTED_COLOR,
            note=None if current == RegistrationMode.APPROVAL_REQUIRED or not question
            else "asked only while approval is required",
        ))
        if pending_count:
            rows.append(Field(
                "Awaiting approval", f"{pending_count} account(s) -- see [U]ser list", color=WARNING_COLOR
            ))
        if current == RegistrationMode.APPROVAL_REQUIRED:
            # Issue #835: a SysOp's banner promised newcomers they could
            # "look around a bit" while they waited, and they can't.
            rows.append(Note(
                "A new account can't log in at all until you approve it -- not even to look around. "
                "The caller is told it is waiting for your approval."
            ))
        choice, _page = await show_detail(
            session,
            title=_detail_title(
                session, chrome, "Registration",
                subtitle="Who may create an account on this node by themselves.",
            ),
            sections=[Section("Self-service registration", rows)],
            actions=[
                ("o", menu_key("O", "pen")),
                ("a", menu_key("A", "pproval required")),
                ("c", menu_key("C", "losed")),
                ("q", menu_key("Q", "uestion")),
                _BACK_ACTION,
            ],
            message=message,
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            help_title="Registration help",
            help_about=(
                "Who may sign up by themselves, over telnet and SSH alike. A change applies to the next "
                "signup; accounts already pending stay pending."
            ),
            help_entries=_help_entries([("o", menu_key("O", "pen")), ("a", menu_key("A", "pproval required")), ("c", menu_key("C", "losed")), ("q", menu_key("Q", "uestion"))], {
                "o": "New accounts can log in at once",
                "a": "No login until approved on their account",
                "c": "No signups; you create accounts here",
                "q": "Asked only while approval is required",
            }),
        )
        if choice == "b":
            return
        if choice == "q":
            await session.write_line("")
            await write_prompt(
                session,
                f"Question to ask new callers (up to {MAX_REGISTRATION_QUESTION_LENGTH} characters, "
                "blank for none): ",
            )
            text = (await session.read_line()).strip()
            if len(text) > MAX_REGISTRATION_QUESTION_LENGTH:
                message = colored(
                    f"That is {len(text)} characters; the question can be at most "
                    f"{MAX_REGISTRATION_QUESTION_LENGTH}. Not changed.",
                    fg_color=ERROR_COLOR,
                )
                continue

            def _apply_question(db: Database) -> None:
                set_registration_question(db, text or None)
                record_action(
                    db, actor=actor, action="set_registration_question",
                    detail="cleared" if not text else f"question={text!r}",
                )

            await lane.run(_apply_question)
            message = colored(
                "Signup question set." if text else "Signup question removed.", fg_color=SUCCESS_COLOR
            )
            continue
        new_mode = modes[choice]
        if new_mode == current:
            message = colored("Already set to that mode.", fg_color=MUTED_COLOR)
            continue

        def _apply(db: Database) -> None:
            set_registration_mode(db, new_mode)
            record_action(db, actor=actor, action="set_registration_mode", detail=f"mode={new_mode.value}")

        await lane.run(_apply)
        message = colored(
            f"Registration mode is now: {_REGISTRATION_MODE_LABELS[new_mode]}", fg_color=SUCCESS_COLOR
        )
