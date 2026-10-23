"""SysOp console System menu.

Node name, Link participation, guest access, timestamps, limits, policy,
MRC settings and status, and the Managed DNS status. Split out of
`netbbs.net.admin_flow` (issue #1311).
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable
from zoneinfo import available_timezones

from netbbs.access_map import level_context
from netbbs.auth.users import (
    NEW_ACCOUNT_SENTINEL,
    SYSOP_LEVEL,
    AuthError,
    User,
    get_user_by_id,
    get_user_by_username,
)
from netbbs.managed_dns.state import (
    get_admin_token as get_managed_dns_admin_token,
    get_local_listeners as get_managed_dns_local_listeners,
    get_contact_problem as get_managed_dns_contact_problem,
    get_published as get_managed_dns_published,
    get_recovery_note as get_managed_dns_recovery_note,
    get_service_contact as get_managed_dns_service_contact,
    OptIn as ManagedDnsOptIn,
    RegistrationStatus as ManagedDnsRegistrationStatus,
    get_last_contact_at as get_managed_dns_last_contact_at,
    get_opt_in as get_managed_dns_opt_in,
    get_previous_name as get_managed_dns_previous_name,
    get_registered_name as get_managed_dns_registered_name,
    get_registration_status as get_managed_dns_registration_status,
)
from netbbs.net.managed_dns_flow import (
    administer_service as administer_managed_dns_service,
    standard_ports_lines as managed_dns_standard_ports_lines,
    cancel_registration_rename,
    register_via_prompt,
    release_registration,
    rename_registration,
)
from netbbs.chat.scrollback import (
    SCROLLBACK_LIMIT_CONFIG_KEY,
    MAX_SCROLLBACK_LIMIT,
    get_scrollback_limit,
)
from netbbs.config import (
    EXPIRY_GRACE_PERIOD_CONFIG_KEY,
    INVITATION_EXPIRY_DAYS_CONFIG_KEY,
    MAX_NODE_DISPLAY_NAME_LENGTH,
    MAX_SETTING_DAYS,
    MAX_UPLOAD_BYTES_CONFIG_KEY,
    MAX_UPLOAD_BYTES_LIMIT,
    get_expiry_grace_period_days,
    get_invitation_expiry_days,
    get_max_upload_bytes,
    get_node_display_name,
    get_node_map_min_level,
    MAX_NODE_MAP_MIN_LEVEL,
    NODE_MAP_MIN_LEVEL_CONFIG_KEY,
    get_mail_min_level,
    MAIL_MIN_LEVEL_CONFIG_KEY,
    MAX_MAIL_MIN_LEVEL,
    get_oneliner_min_level,
    ONELINER_MIN_LEVEL_CONFIG_KEY,
    MAX_ONELINER_MIN_LEVEL,
    CROSSPOST_BOARD_LIMIT_CONFIG_KEY,
    MAX_CROSSPOST_BOARD_LIMIT,
    get_crosspost_board_limit,
    MAX_QWK_PACKET_MESSAGES,
    MIN_QWK_PACKET_MESSAGES,
    QWK_BBS_ID_CONFIG_KEY,
    QWK_PACKET_MESSAGES_CONFIG_KEY,
    derived_qwk_bbs_id,
    get_qwk_bbs_id_setting,
    get_qwk_packet_messages,
    is_valid_qwk_bbs_id,
    is_node_display_name_placeholder,
    set_config_without_commit,
    set_node_display_name,
)
from netbbs.link.boards import LinkContext
from netbbs.net.ftn_console import ftn_networks_screen
from netbbs.net.qwknet_console import qwknet_networks_screen
from netbbs.link.node_page import (
    MAX_TAGLINE,
    NODE_PAGE_INDEXED,
    NODE_PAGE_OFF,
    NODE_PAGE_SHOWN,
    NODE_PAGE_STATES,
    NODE_PAGE_URL_PREFIX,
    get_node_page,
    get_node_tagline,
    set_node_page_without_commit,
    set_node_tagline_without_commit,
)
from netbbs.link.node_profiles import known_nodes_named_like
from netbbs.link.onboarding import (
    Participation,
    get_configured_link_enabled,
    get_participation,
    set_participation,
)
from netbbs.link.reliable_nodes import effective_reliable_nodes, reliable_nodes_source
from netbbs.link.trust import list_sole_authorities
from netbbs.moderation.log import record_action, record_action_without_commit
from netbbs.mrc.protocol import display_roster_entry, room_name_error
from netbbs.net.bootstrap_view import load_bootstrap_snapshot
from netbbs.net.nodeconfig import NodeConfig
from netbbs.net.policy_settings import (
    GROUPS as POLICY_GROUPS,
    PolicyValueError,
    PolicyView,
    format_value as format_policy_value,
    load_policy_views,
    parse_text as parse_policy_text,
    save_policy_without_commit,
    validate_value as validate_policy_value,
)
from netbbs.mrc.bridge import MrcBridge, MrcState, MrcStatus
from netbbs.mrc.settings import (
    MrcSettings,
    MrcSettingsError,
    OpenRoomSettings,
    load_open_room_settings,
    save_open_room_settings,
    default_port_for,
    list_mrc_mappings,
    load_mrc_settings,
    save_mrc_settings,
    validate_mrc_settings,
)
from netbbs.mrc.protocol import sanitize_room
from netbbs.net.char_input import HELP_KEY, InputCancelled, reject_unhandled_key
from netbbs.net.help_overlay import show_menu_help
from netbbs.net.location import crumbs, located
from netbbs.net.resource_editor import (
    inline_field,
    write_field_prompt,
    write_field_message,
    FieldSpec,
    bool_field,
    bool_step,
    choice_field,
    text_field,
)
from netbbs.net.session import Session, write_prompt
from netbbs.net.shutdown import NodeControls
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.node_theme import (
    effective_accent_color_256,
    effective_header_color_256,
    node_name_gradient_override,
    set_node_name_gradient_override,
)
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.selfupdate import get_auto_update_check_enabled, get_display_check_summary
from netbbs.permissions.levels import meets_level
from netbbs.rendering import (
    ACCENT_COLOR,
    DATE_COLOR,
    ERROR_COLOR,
    LABEL_COLOR,
    METADATA_COLOR,
    MUTED_COLOR,
    SUCCESS_COLOR,
    VALUE_COLOR,
    WARNING_COLOR,
    MenuEntry,
    action_bar,
    badge,
    colored,
    cut_to_width,
    GRADIENTS,
    gradient_text,
    menu_key,
    sanitize_text,
    screen_title,
    status_badge,
    status_mark,
)
from netbbs.rendering.detail import Field, Note, Section, Table
from netbbs.guest import (
    guest_privileges,
    guest_user,
    pre_login_notice,
    set_guest_user_without_commit,
    set_pre_login_notice_without_commit,
)
from netbbs.oneliners import oneliners_enabled, oneliners_plain, set_oneliners_enabled, set_oneliners_plain
from netbbs.session_history import (
    previous_callers_enabled,
    previous_callers_plain,
    set_previous_callers_enabled,
    set_previous_callers_plain,
)
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import (
    format_for_display,
    resolve_display_preferences,
    set_display_format,
    set_display_timezone,
    utc_now_iso,
)
from netbbs.net.admin_ui import (
    _BACK_ACTION,
    _TrailingOutput,
    _announce_line,
    _announce_styled,
    _choice_prompt,
    _degrade_description_level,
    _detail_title,
    _fitted_menu,
    _format_bytes,
    _load_chrome,
    _menu_row,
    _write_panel,
    _write_sections,
    edit_resource_draft,
    pick_item,
    show_detail,
)
from netbbs.net.admin_fields import (
    _CLEAR_HINT,
    _EDIT_HINT,
    _VERIFIED_AGE_HELP,
    _int_field,
    _min_age_field,
    _min_age_label,
    _optional_int_field,
    _optional_text_field,
    _plain_level_label,
    _read_seeded_line,
    _resolve_redraw_preference,
    _setting_level_label,
)
from netbbs.net.admin_maintenance import _update_settings_screen
from netbbs.net.admin_trust import _trust_menu
from netbbs.net.admin_banners import _banners_and_mastheads_menu, _theme_colors_menu


@located("Settings")
async def _system_menu(
    session: Session,
    lane: DatabaseLane,
    actor: User,
    *,
    node_controls: NodeControls | None,
    link_context: LinkContext | None = None,
) -> None:
    """Durable node settings: mastheads & banners, updates, timestamps, and trust.

    The operational screens (Link status, Outbox, Repair carried posts,
    Diagnostics, Watch log, Full backups) live under Operations. Their old
    keys were accepted here too as unlisted aliases until issue #1158 step 2:
    a hidden key blocks a label's first letter, and a renamed key is a clean
    switch."""

    def _load_settings_stats(db: Database) -> dict[str, Any]:
        # GitHub issue #206: unlike its Users/Content/Operations/Node
        # siblings, this screen had no glance-able status of its own at
        # all -- just the title and menu. Settings is durable
        # configuration, not countable stats, so "current values" (not
        # totals/pending counts) is the shape that actually fits here.
        display_format, display_timezone = resolve_display_preferences(db)
        return {
            "node_name": get_node_display_name(db),
            "auto_update_enabled": get_auto_update_check_enabled(db),
            "update": get_display_check_summary(db),
            "display_timezone": display_timezone,
            "timestamp_example": format_for_display(
                utc_now_iso(), override_format=display_format, override_timezone=display_timezone
            ),
            "previous_callers_enabled": previous_callers_enabled(db),
            "previous_callers_plain": previous_callers_plain(db),
            "oneliners_enabled": oneliners_enabled(db),
            "oneliners_plain": oneliners_plain(db),
            "guest_username": (lambda u: u.username if u is not None else None)(guest_user(db)),
            "trust_exceptions": len(list_sole_authorities(db)),
            "description_level": menu_description_level(db, actor),
            "unicode_style": unicode_style_enabled(db, actor),
            "collapsed": breadcrumb_collapsed_enabled(db, actor),
            "redraw_in_place": redraw_in_place_enabled(db, actor),
            "header_color": effective_header_color_256(db),
        }

    stats = await lane.run(_load_settings_stats)
    await _draw_system_menu(session, node_controls, link_context, stats=stats)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "m":
            await session.write_line("")
            await _banners_and_mastheads_menu(session, lane, actor, node_controls=node_controls)
            stats = await lane.run(_load_settings_stats)
            await _draw_system_menu(session, node_controls, link_context, stats=stats)
        elif choice == "c":
            await session.write_line("")
            await _theme_colors_menu(session, lane, actor)
            stats = await lane.run(_load_settings_stats)
            await _draw_system_menu(session, node_controls, link_context, stats=stats)
        elif choice == "u":
            await session.write_line("")
            await _update_settings_screen(session, lane, actor, node_controls=node_controls)
            stats = await lane.run(_load_settings_stats)
            await _draw_system_menu(session, node_controls, link_context, stats=stats)
        elif choice == "n":
            await session.write_line("")
            await _node_name_screen(session, lane, actor)
            stats = await lane.run(_load_settings_stats)
            await _draw_system_menu(session, node_controls, link_context, stats=stats)
        elif choice == "j":
            await session.write_line("")
            await _link_participation_screen(session, lane, actor)
            stats = await lane.run(_load_settings_stats)
            await _draw_system_menu(session, node_controls, link_context, stats=stats)
        elif choice == "g":
            await session.write_line("")
            await _guest_access_screen(session, lane, actor)
            stats = await lane.run(_load_settings_stats)
            await _draw_system_menu(session, node_controls, link_context, stats=stats)
        elif choice == "t":
            await session.write_line("")
            await _timestamp_settings_screen(session, lane, actor)
            stats = await lane.run(_load_settings_stats)
            await _draw_system_menu(session, node_controls, link_context, stats=stats)
        elif choice == "l":
            await session.write_line("")
            await _limits_settings_screen(session, lane, actor)
            stats = await lane.run(_load_settings_stats)
            await _draw_system_menu(session, node_controls, link_context, stats=stats)
        elif choice == "q":
            await session.write_line("")
            await _qwk_settings_screen(session, lane, actor)
            stats = await lane.run(_load_settings_stats)
            await _draw_system_menu(session, node_controls, link_context, stats=stats)
        elif choice == "o":
            await session.write_line("")
            await _policy_settings_screen(session, lane, actor)
            stats = await lane.run(_load_settings_stats)
            await _draw_system_menu(session, node_controls, link_context, stats=stats)
        elif choice == "r":
            def _toggle_previous_callers(db: Database) -> None:
                # Three steps (issue #841): shown in the default neon style,
                # shown plain, hidden, and round again.
                enabled, plain = previous_callers_enabled(db), previous_callers_plain(db)
                if enabled and not plain:
                    set_previous_callers_plain(db, True)
                    record_action(db, actor=actor, action="set_previous_callers_plain", detail="plain=true")
                    return
                enabled = not enabled
                set_previous_callers_enabled(db, enabled)
                if enabled:
                    set_previous_callers_plain(db, False)
                record_action(
                    db,
                    actor=actor,
                    action="set_previous_callers_enabled",
                    detail=f"enabled={str(enabled).lower()}",
                )

            await lane.run(_toggle_previous_callers)
            stats = await lane.run(_load_settings_stats)
            await _draw_system_menu(session, node_controls, link_context, stats=stats)
        elif choice == "w":
            def _toggle_oneliners(db: Database) -> None:
                # Issue #1391: the wall after login, in the same three steps
                # as Recent callers: neon, plain, hidden. Sharing the frame
                # with the roll, it is drawn in the roll's style.
                enabled, plain = oneliners_enabled(db), oneliners_plain(db)
                if enabled and not plain:
                    set_oneliners_plain(db, True)
                    record_action(db, actor=actor, action="set_oneliners_plain", detail="plain=true")
                    return
                enabled = not enabled
                set_oneliners_enabled(db, enabled)
                if enabled:
                    set_oneliners_plain(db, False)
                record_action(
                    db, actor=actor, action="set_oneliners_enabled", detail=f"enabled={str(enabled).lower()}",
                )

            await lane.run(_toggle_oneliners)
            stats = await lane.run(_load_settings_stats)
            await _draw_system_menu(session, node_controls, link_context, stats=stats)
        elif choice == "i":
            await session.write_line("")
            await _mrc_settings_screen(session, lane, actor, node_controls=node_controls)
            stats = await lane.run(_load_settings_stats)
            await _draw_system_menu(session, node_controls, link_context, stats=stats)
        elif choice == "d":
            await session.write_line("")
            await qwknet_networks_screen(session, lane, actor, node_controls=node_controls)
            stats = await lane.run(_load_settings_stats)
            await _draw_system_menu(session, node_controls, link_context, stats=stats)
        elif choice == "e":
            await session.write_line("")
            await ftn_networks_screen(session, lane, actor, node_controls=node_controls)
            stats = await lane.run(_load_settings_stats)
            await _draw_system_menu(session, node_controls, link_context, stats=stats)
        elif choice == "p":
            await session.write_line("")
            await _trust_menu(session, lane, actor, link_context=link_context)
            stats = await lane.run(_load_settings_stats)
            await _draw_system_menu(session, node_controls, link_context, stats=stats)
        elif choice == HELP_KEY:
            await show_menu_help(
                session, "Settings help", _system_menu_entries(stats),
                about="The node's lasting configuration: how it looks, what callers may do, and how it "
                      "joins other networks. Recent callers and the Wall change at once, without a screen of "
                      "their own.",
                header_color=stats["header_color"], unicode_style=stats["unicode_style"],
            )
            await _draw_system_menu(session, node_controls, link_context, stats=stats)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_system_menu(
    session: Session,
    node_controls: NodeControls | None,
    link_context: LinkContext | None = None,
    *,
    stats: dict[str, Any],
) -> None:
    description_level = stats["description_level"]
    unicode_style = stats["unicode_style"]
    collapsed = stats["collapsed"]
    redraw_in_place = stats["redraw_in_place"]
    header_color = stats["header_color"]

    await session.write_line("\r\n" + screen_title("Settings",
            breadcrumb=crumbs(session, own=True), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color, node_name_gradient=session.node_name_gradient))

    panel: list[str] = []
    if session.terminal_height >= 18:
        # GitHub issue #206: "current values at a glance," not counts --
        # unlike Users/Content (registered accounts, boards/files),
        # Settings is durable configuration with nothing to total or
        # count, so this panel shows what each setting is currently set
        # to instead, one line per submenu that has a single
        # headline-worthy value. Mastheads & banners/Colors are left out
        # -- neither reduces to one meaningful line (banners: several
        # independent on/off toggles; colors: several independent
        # swatches) without misrepresenting the others as unset.
        box_inner_width = min(session.terminal_width, 78) - 4
        # Same "cut plain, then color, only under unicode_style" order
        # `_draw_admin_menu`'s own `_fit` already established -- this
        # codebase never cuts already-SGR-styled text (double_frame's
        # own docstring). Code review follow-up (PR #215): without this,
        # a configured node name or a long update-check outcome message
        # (an HTTP client's own exception text, e.g.) pushes the row --
        # and with it the frame's right border -- past the terminal edge
        # on a narrow (e.g. 40-column) terminal, corrupting the box.
        def _fit(text: str, prefix_len: int) -> str:
            return cut_to_width(text, box_inner_width - prefix_len) if unicode_style else text

        update_checked_at, update_outcome = stats["update"]
        update_state = "auto" if stats["auto_update_enabled"] else "manual"
        # Code review follow-up (PR #215): record_check_outcome persists
        # update_checked_at/update_outcome as two separate committing
        # set_config calls, so a check opened mid-write (or interrupted
        # between the two) can leave checked_at set with outcome still
        # None -- the old `update_outcome if update_checked_at else
        # "never checked"` then passed None straight to sanitize_text
        # below and crashed this screen with TypeError. Handles all
        # three real states instead of assuming they change together.
        if update_outcome:
            update_summary = update_outcome
        elif update_checked_at:
            update_summary = "check in progress"
        else:
            update_summary = "never checked"
        trust_summary = (
            f"{stats['trust_exceptions']} exception(s) active"
            if stats["trust_exceptions"] else "clear"
        )
        # One label column, so the five values start under one another: each
        # label used to be exactly as wide as its own text, which put every
        # value at a different column and made the panel read as five
        # sentences rather than five settings.
        node_name_label, update_label, timestamp_label, callers_label, trust_label = (
            f"{label + ':':<22}" for label in (
                "Node name", "Update checks", "Timestamps shown as", "Recent callers", "Trust policy",
            )
        )
        update_value = f"{update_state} -- {update_summary}"
        # Code review follow-up (PR #215): unlike every other value in
        # this panel, the formatted timestamp example was concatenated
        # without sanitize_text -- is_valid_display_format accepts
        # arbitrary literal characters in a SysOp-configured format
        # string (including C1/bidi controls), and format_for_display
        # preserves them verbatim, letting a crafted format string alter
        # or visually reorder this framed terminal output. Built here as
        # plain text, cut, then sanitized -- same "cut plain, then
        # color/sanitize" order as every other value below.
        timestamp_value = f"{stats['timestamp_example']} ({stats['display_timezone']})"
        panel = [
            colored("CURRENT VALUES", fg_color=LABEL_COLOR, bold=True),
            "  " + colored(node_name_label, fg_color=LABEL_COLOR)
            + colored(sanitize_text(_fit(stats["node_name"], 2 + len(node_name_label))), fg_color=VALUE_COLOR, bold=True),
            "  " + colored(update_label, fg_color=LABEL_COLOR)
            + colored(sanitize_text(_fit(update_value, 2 + len(update_label))), fg_color=VALUE_COLOR),
            "  " + colored(timestamp_label, fg_color=LABEL_COLOR)
            + colored(sanitize_text(_fit(timestamp_value, 2 + len(timestamp_label))), fg_color=VALUE_COLOR),
            "  " + colored(callers_label, fg_color=LABEL_COLOR)
            + colored(
                _fit(
                    ("shown after login, plain" if stats["previous_callers_plain"] else "shown after login, neon")
                    if stats["previous_callers_enabled"]
                    else "hidden",
                    2 + len(callers_label),
                ),
                fg_color=SUCCESS_COLOR if stats["previous_callers_enabled"] else MUTED_COLOR,
            ),
            "  " + colored(trust_label, fg_color=LABEL_COLOR)
            + colored(_fit(trust_summary, 2 + len(trust_label)), fg_color=ERROR_COLOR if stats["trust_exceptions"] else SUCCESS_COLOR),
        ]
        await _write_panel(session, panel, unicode_style=unicode_style, header_color=header_color)

    option_list = _system_menu_entries(stats)
    # The panel above takes rows from the menu, as on Users, Content and
    # Operations: descriptions give way before anything overflows.
    effective_desc_level, available_menu_height, desc_degraded = _degrade_description_level(
        panel=panel, unicode_style=unicode_style, description_level=description_level,
        entry_count=len(option_list), terminal_width=session.terminal_width,
        terminal_height=session.terminal_height,
    )
    await session.write_line(
        _menu_row(
            option_list, effective_desc_level, width=session.terminal_width,
            height=available_menu_height, degraded=desc_degraded,
        )
    )
    await _choice_prompt(session)


def _system_menu_entries(stats: dict[str, Any]) -> list[MenuEntry]:
    """The Settings screen's menu, shared by its draw and its help."""
    option_list = [
        MenuEntry(label=menu_key("M", "astheads & banners"), brief="Banners and mastheads callers see"),
        MenuEntry(label=menu_key("C", "olors"), brief="Accent, header and clock colors"),
        MenuEntry(label=menu_key("N", "ode name"), brief="Name and gradient shown everywhere"),
        MenuEntry(label=menu_key("J", "oin NetBBS Link"), brief="Reliable-node seeds and relays"),
        MenuEntry(label=menu_key("U", "pdate"), brief="Software update settings"),
        MenuEntry(label=menu_key("T", "imestamp format"), brief="Node-wide date/time display"),
        MenuEntry(
            label=menu_key("L", "imits & retention"),
            brief="Uploads, expiry, chat, mail level",
        ),
        MenuEntry(
            label=menu_key("O", "perating limits"),
            brief="Link, login throttle, shutdown",
        ),
        MenuEntry(
            label=menu_key("G", "uest access"),
            brief=(
                # Sanitized here because `menu_grid` does not do it for
                # its callers (Codex review). Account creation rejects a
                # username carrying C1 controls or a bidi override, but
                # deliberately left rows that predate that check valid --
                # so designating one of those could have injected
                # terminal control into the SysOp's own Settings menu.
                f"Guest login as {sanitize_text(stats['guest_username'])}"
                if stats["guest_username"]
                else "Guest login off; pre-login notice"
            ),
        ),
        # A three-way toggle (issue #841) shows its state after a colon
        # (issue #1158): the key and the label's start never change.
        MenuEntry(
            label=menu_key("R", "ecent callers: " + (
                "hidden" if not stats["previous_callers_enabled"]
                else "plain" if stats["previous_callers_plain"] else "neon"
            )),
            brief=(
                "Hidden after login; press to show (neon)"
                if not stats["previous_callers_enabled"]
                else "Plain; press to hide"
                if stats["previous_callers_plain"]
                else "Neon; press for plain"
            ),
        ),
        # Issue #1391: "Oneliners" would take Operating limits' key, so the
        # wall is named by its other word.
        MenuEntry(
            label=menu_key("W", "all of oneliners") + ": " + (
                "hidden" if not stats["oneliners_enabled"]
                else "plain" if stats["oneliners_plain"] else "neon"
            ),
            brief=(
                "Hidden after login; press to show (neon)"
                if not stats["oneliners_enabled"]
                else "Plain; press to hide"
                if stats["oneliners_plain"]
                else "Neon; press for plain"
            ),
        ),
        MenuEntry(label=menu_key("I", "nter-BBS chat (MRC)"), brief="Bridge channels to the MRC network"),
        MenuEntry(label=menu_key("E", "chomail & netmail (FTN)"), brief="FidoNet-style networks"),
        MenuEntry(label=menu_key("P", "olicy trust"), brief="Federation trust policy"),
        MenuEntry(label=menu_key("Q", "WK offline mail"), brief="Packet size and BBS ID"),
        # Issues #1481 and #1482: Q is offline mail's, so QWKnet goes by the
        # network most SysOps join it for.
        MenuEntry(label=menu_key("D", "OVE-Net and QWKnet"), brief="QWK networks: node and hub"),
    ]
    option_list.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to the SysOp console"))
    return option_list


async def _draw_node_name_screen(
    session: Session, lane: DatabaseLane, description_level: str, redraw_in_place: bool,
    unicode_style: bool, collapsed: bool, header_color: int | tuple[int, int, int],
) -> None:
    name = await lane.run(get_node_display_name)
    gradient = await lane.run(node_name_gradient_override)
    await session.write_line(
        "\r\n" + screen_title(
            "Node name",
            breadcrumb=crumbs(session, own=True), width=session.terminal_width, clear=redraw_in_place,
            unicode_style=unicode_style, collapsed=collapsed, header_color=header_color,
            node_name_gradient=session.node_name_gradient,
        )
    )
    safe_name = sanitize_text(name)
    preview = (
        gradient_text(safe_name, gradient, truecolor=False) if gradient is not None
        else colored(safe_name, fg_color=header_color, bold=True)
    )
    panel_rows = await _write_sections(session, [
        Section("Node name", [
            Field("Name", name, bold=True),
            Field("Gradient", gradient if gradient is not None else "solid (no gradient)",
                  color=VALUE_COLOR if gradient is not None else MUTED_COLOR),
            Field("Shown as", preview, styled=True),
        ]),
        Section("Where it appears", [Note(
            f"The name is shown in the upper-left corner of every screen, and to any door as its own "
            f"drop-file 'node_name' field (always plain there, never with gradient codes). Up to "
            f"{MAX_NODE_DISPLAY_NAME_LENGTH} characters."
        )]),
    ], unicode_style=unicode_style)
    await session.write_line(
        "\r\n" + _fitted_menu(list(_NODE_NAME_MENU), description_level, session=session, used_rows=panel_rows + 4)
    )
    await _choice_prompt(session)


_NODE_NAME_MENU = (
    MenuEntry(label=menu_key("N", "ame"), brief="Rename the node"),
    MenuEntry(label=menu_key("G", "radient"), brief="Recolor the node name"),
    MenuEntry(label=menu_key("B", "ack"), brief="Return to Settings"),
)


async def _link_participation_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """Settings home for the reliable-node participation decision
    (design doc §16, issue #219): what the SysOp answered on the first-run
    screen (or never did), what the operator's configuration says about
    `[link] enabled`, which of the two therefore decides whether Link
    runs, and the roster this node would dial -- with a single accept/
    decline action. Reachable whether or not Link is currently running
    (unlike `_link_status_screen`, which needs a live `link_context`),
    since changing this is exactly how a SysOp turns Link on from a
    silent configuration. Takes effect at the next sync pass for the
    roster and at the next startup for enablement itself.

    The outcome of a keypress is carried into the redraw it causes
    (`show_detail`'s `message`): written above it, the redraw's own clear
    wiped it before it could be read."""
    page = 0
    message: str | None = None
    while True:
        chrome = await _load_chrome(lane, actor)
        sections, participation = await _link_participation_sections(lane)
        actions = [_BACK_ACTION]
        if participation is not Participation.DECLINED:
            actions.insert(0, ("d", menu_key("D", "ecline")))
        if participation is not Participation.ACCEPTED:
            actions.insert(0, ("a", menu_key("A", "ccept")))
        choice, page = await show_detail(
            session,
            title=_detail_title(
                session, chrome, "Join NetBBS Link",
                subtitle="Whether this node uses the project's reliable nodes as seeds and relays.",
            ),
            sections=sections, actions=actions, page=page, message=message,
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            help_title="Join NetBBS Link help",
            help_about=(
                "Whether this node uses the project's reliable nodes as seeds and relays. The node's own "
                "configuration file can override the answer."
            ),
        )
        message = None
        if choice == "b":
            return
        if choice == "a":
            if await lane.run(is_node_display_name_placeholder):
                message = colored(
                    "Set a node name first (Settings > Node name) -- a node can't join NetBBS "
                    "Link under the placeholder name.",
                    fg_color=ERROR_COLOR,
                )
                continue

            def _accept(db: Database) -> None:
                set_participation(db, Participation.ACCEPTED)
                record_action(db, actor=actor, action="set_link_participation", detail="accepted")

            await lane.run(_accept)
            message = colored("Reliable-node participation accepted.", fg_color=SUCCESS_COLOR)
        elif choice == "d":
            def _decline(db: Database) -> None:
                set_participation(db, Participation.DECLINED)
                record_action(db, actor=actor, action="set_link_participation", detail="declined")

            await lane.run(_decline)
            message = colored("Reliable-node participation declined.", fg_color=SUCCESS_COLOR)


async def _link_participation_sections(lane: DatabaseLane) -> tuple[list[Section], Participation]:
    def _load(db: Database) -> tuple[Participation, bool | None | str, list, str, bool]:
        return (
            get_participation(db), get_configured_link_enabled(db), effective_reliable_nodes(db),
            reliable_nodes_source(db), is_node_display_name_placeholder(db),
        )

    participation, configured, reliable, source, placeholder = await lane.run(_load)
    if configured is True:
        config_text = "[link] enabled = true -- Link runs regardless of this answer"
    elif configured is False:
        config_text = "[link] enabled = false -- Link stays off regardless of this answer"
    elif configured is None:
        config_text = "[link] enabled not set -- this answer decides (applies at the next startup)"
    else:
        config_text = "unknown until this node has started once on this version"
    decision: list[Field | Note | Table] = [
        Field(
            "Participation",
            badge(participation.value.upper(), tone="success" if participation is Participation.ACCEPTED else "neutral"),
            styled=True,
        ),
        Field("Configuration", config_text, color=METADATA_COLOR),
    ]
    if placeholder:
        decision.append(Note(
            "This node still has the placeholder name -- set one under Node name before accepting.",
            color=ERROR_COLOR,
        ))
    roster: list[Field | Note | Table] = [Field("Reliable nodes", f"{len(reliable)} ({source} list)")]
    if reliable:
        roster.append(Table(
            ("Name", "Address"),
            [[(entry.name, ACCENT_COLOR), (entry.url, METADATA_COLOR)] for entry in reliable],
            flex=1,
        ))
    return [
        Section("This node's answer", decision),
        Section("Who it would dial", roster),
        Section("What accepting means", [Note(
            "Accepting dials these nodes as seeds after your own configured ones and, for a node that "
            "can't be reached from the internet directly, uses them as relays. It hands them no say over "
            "your content and is not a trust decision about anyone."
        )]),
    ], participation


@located("Node name")
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
        elif choice == HELP_KEY:
            await show_menu_help(
                session, "Node name help", _NODE_NAME_MENU,
                about="The name shown in the corner of every screen and handed to doors, and the "
                      "colors it is drawn in.",
                header_color=header_color, unicode_style=unicode_style,
            )
            await _draw_node_name_screen(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


async def _guest_access_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """Guest login and the pre-login notice (issue #531).

    One screen for both because they are one feature in use: the notice
    is how a caller learns the guest credentials exist. See
    `netbbs.guest` for why guest login is only an authentication
    shortcut and never a new kind of account.

    A draft editor rather than a prompt chain, per design doc §3.5 --
    two values, changed independently, nothing written before [S]ave.
    """
    redraw_in_place, redraw_hint = await lane.run(_resolve_redraw_preference, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)

    def _load(db: Database) -> dict:
        current = guest_user(db)
        return {
            "guest_username": current.username if current is not None else "",
            "notice": pre_login_notice(db),
        }

    draft = await lane.run(_load)

    async def save(draft: dict):
        name = (draft["guest_username"] or "").strip()
        account = None
        if name:
            def _check(db: Database) -> tuple[User | None, list[str]]:
                try:
                    found = get_user_by_username(db, name)
                except AuthError:
                    return None, []
                return found, guest_privileges(db, found)

            account, privileges = await lane.run(_check)
            if account is None:
                # Raised, not returned: a bare `return None` closes the
                # editor and discards the notice typed alongside it
                # (issue #282's own lesson, learned the hard way).
                raise AuthError(
                    f"No account named {name!r}. Guest login names an existing account; "
                    "create it first, then designate it here."
                )
            if account.username.strip().lower() == NEW_ACCOUNT_SENTINEL:
                # `new` is how a caller asks to register, and `_login`
                # acts on it before the guest branch is reached -- so an
                # account with this name can be designated, saved, and
                # then never actually sign anybody in (Codex review).
                # `RESERVED_USERNAMES` has refused the name since, so
                # only an account predating that check can be here.
                raise AuthError(
                    f"{name!r} is how a caller asks to create an account, so it cannot be the "
                    "guest account. Rename it first."
                )
            if meets_level(account, SYSOP_LEVEL):
                # The one refusal worth hard-coding. Everything else
                # about what a guest may do is the account's level and
                # per-object grants -- but a passwordless SysOp login is
                # not a policy choice a SysOp should be able to make by
                # typing a name into a field.
                raise AuthError(
                    f"{name!r} is a SysOp account. Guest login skips the password, so it cannot "
                    "be a SysOp."
                )
            if privileges:
                # Issue #1075: the same reasoning for every privilege a
                # SysOp grants a person -- the guest account is everyone.
                raise AuthError(
                    f"{name!r} holds {' and '.join(privileges)}. Guest login skips the password, "
                    "so every guest would hold them too. Remove them first."
                )

        def _apply(db: Database) -> None:
            # Validated and written inside one `BEGIN IMMEDIATE`, the
            # same shape `netbbs.auth.users.delete_user` uses -- and for
            # the same reason, since that is the writer this is racing
            # (Codex review, three rounds on this one paragraph).
            #
            # Resolving the account and designating it as two separate
            # statements is a check-then-act: a delete landing in
            # between runs its own designation-clearing hook *before*
            # the stale designation is written, so nothing clears it
            # afterwards -- and if the freed id and creation timestamp
            # are later handed to a new account, that account has
            # passwordless login. Doing it in one lane callback
            # serializes this process but not the standalone admin CLI
            # or any other connection; a write transaction is what
            # actually excludes them.
            db.connection.execute("BEGIN IMMEDIATE")
            try:
                if account is not None:
                    still_there = get_user_by_id(db, account.id)
                    if still_there is None or still_there.created_at != account.created_at:
                        raise AuthError(
                            f"{name!r} was removed while you were editing. Nothing was changed."
                        )
                set_guest_user_without_commit(db, account)
                set_pre_login_notice_without_commit(db, draft["notice"] or "")
                record_action_without_commit(
                    db, actor=actor, action="set_guest_access",
                    detail=f"guest={name or '(off)'} notice={'set' if draft['notice'] else '(none)'}",
                )
            except BaseException:
                db.connection.rollback()
                raise
            else:
                db.connection.commit()

        await lane.run(_apply)
        return True

    fields = [
        FieldSpec(
            key="guest_username",
            label="Guest account",
            render=lambda d: d.get("guest_username") or "(guest login off)",
            # Two Codex rounds landed on this one field. It could not
            # be cleared at all at first -- the shared `text_field` read
            # a blank entry as "keep", which is right for a name that
            # must always be *something* and wrong for a setting whose
            # off switch is emptiness. A `'none'` sentinel fixed that and
            # introduced a smaller bug of its own: `RESERVED_USERNAMES`
            # holds only `new`, so an account genuinely named "none"
            # could never be designated.
            #
            # Neither is needed now. Issue #529 gave `text_field` the
            # current value as an editable prefill, so erasing it and
            # pressing Enter *is* the clear -- no sentinel, no word a
            # SysOp cannot type.
            prompt=text_field("guest_username"),
            brief="Account that needs no password",
            help=(
                "An existing account callers may sign in as without a password. It stays an "
                "ordinary account: its level and per-object permissions decide what a guest can "
                "reach, and it keeps its own password for normal sign-in. One thing is closed to it "
                "whatever its level: mail, because every guest would share its mailbox. Clear this "
                "field to turn guest login off. A SysOp account cannot be used."
            ),
        ),
        FieldSpec(
            key="notice",
            label="Pre-login notice",
            render=lambda d: d.get("notice") or "(none)",
            prompt=text_field("notice"),
            brief="Shown before the login prompt",
            help=(
                "A short line shown after the welcome banner and before the username prompt -- "
                "where you tell callers the guest account exists. Telnet and web only: an SSH "
                "caller has already proven who they are before there is anything to show."
            ),
        ),
    ]

    result = await edit_resource_draft(
        session, lane,
        title="Guest access",
        fields=fields, draft=draft, save=save, error_type=AuthError,
        save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=redraw_in_place, redraw_hint=redraw_hint,
        unicode_style=unicode_style, collapsed=collapsed,
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if result is not None:
        _announce_line(session, "Guest access settings saved.")


async def _rename_node_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    current = await lane.run(get_node_display_name)
    await write_prompt(session, f"New name [{current}] (blank to leave unchanged): ")
    new_name = (await session.read_line()).strip()
    if not new_name:
        _announce_line(session, "No change.")
        return

    def _apply(db: Database) -> list[str]:
        set_node_display_name(db, new_name)
        record_action(db, actor=actor, action="set_node_display_name", detail=f"{current!r} -> {new_name!r}")
        return [identity.label for identity in known_nodes_named_like(db, get_node_display_name(db))]

    try:
        look_alikes = await lane.run(_apply)
    except ValueError as exc:
        _announce_line(session, colored(str(exc), fg_color=ERROR_COLOR))
        return
    if look_alikes:
        # Issue #900: warned, not refused. Two hobbyists picking one name is
        # harmless, and every other node already flags the newcomer.
        _announce_line(session, colored(
            f"Node name set to {new_name!r}, which reads like "
            + ", ".join(sanitize_text(label) for label in look_alikes)
            + ". A node that knows both may warn its callers about this one, and chat will add "
            "each node's address to the name.",
            fg_color=WARNING_COLOR,
        ))
        return
    _announce_line(session, f"Node name set to {new_name!r}.")


async def _set_node_name_gradient_screen(
    session: Session, lane: DatabaseLane, actor: User, header_color: int | tuple[int, int, int]
) -> None:
    """Preset-only (GitHub issue #175's own deliberately narrow scope --
    `netbbs.rendering.gradient.GRADIENTS`'s own keys, not free-form RGB
    stop entry). The list itself shows every candidate rendered against
    the real current node name, so it *is* the preview issue #162's own
    "preview before confirm" requirement asks for: one keystroke on a
    listed digit applies that gradient (issue #282 -- previously a typed
    index, then a second preview, then a yes/no), `[B]ack` leaves it."""
    def _load(db: Database) -> tuple[str, str | None]:
        return get_node_display_name(db), node_name_gradient_override(db)

    name, current = await lane.run(_load)
    choices: list[str | None] = [None, *sorted(GRADIENTS)]

    async def _draw() -> None:
        await session.write_line(colored(f"\r\nGradient for {name!r}:", fg_color=header_color, bold=True))
        for i, choice in enumerate(choices):
            label = "solid" if choice is None else choice
            preview = gradient_text(name, choice, truecolor=False) if choice is not None else colored(name, fg_color=header_color, bold=True)
            marker = colored(" (current)", fg_color=MUTED_COLOR) if choice == current else ""
            await session.write_line(colored(f"  {i}. {label:<8}", fg_color=LABEL_COLOR) + preview + marker)
        await session.write_line(
            action_bar([menu_key(f"0-{len(choices) - 1}", ""), menu_key("B", "ack")], width=session.terminal_width, height=session.terminal_height)
        )
        await _choice_prompt(session)

    await _draw()
    while True:
        raw = (await session.read_key()).lower()
        if raw == "b":
            await session.write_line("")
            return
        # ASCII digits only: str.isdigit() is also true for characters like
        # "²" or "①", for which int() then raises (Codex review on #291).
        if len(raw) == 1 and raw in "0123456789" and int(raw) < len(choices):
            await session.write_line("")
            chosen = choices[int(raw)]
            break
        if raw == HELP_KEY:
            await show_menu_help(
                session, "Gradient help",
                [MenuEntry(label=menu_key(f"0-{len(choices) - 1}", ""),
                           brief="Apply that gradient (0 is solid)")],
                about="Each row shows the node name in one gradient. Pressing its number applies it "
                      "straight away.",
                header_color=header_color, unicode_style=await lane.run(unicode_style_enabled, actor),
            )
            await _draw()
            continue
        await session.write(reject_unhandled_key(raw))

    if chosen == current:
        _announce_line(session, "Already set to that -- no change.")
        return

    label = "solid" if chosen is None else chosen

    def _apply(db: Database) -> None:
        set_node_name_gradient_override(db, chosen)
        action = "set_node_name_gradient" if chosen is not None else "clear_node_name_gradient"
        record_action(db, actor=actor, action=action, detail=f"{current!r} -> {chosen!r}")

    await lane.run(_apply)
    _announce_line(session, f"Node name gradient set to {label!r}.")


_MANAGED_DNS_ACTIVE_STATUSES = (ManagedDnsRegistrationStatus.PENDING, ManagedDnsRegistrationStatus.MATURED)


# Where the node page's setting is offered (issue #1177): wherever the name
# has a page or will have one. A matured name has one, and keeps it once
# abandoned or released (marked "left"; design doc §8.13), so the SysOp can
# still turn it off; a pending name can be set in advance. A revoked name
# loses its page, and no name has none.
_NODE_PAGE_STATUSES = (
    *_MANAGED_DNS_ACTIVE_STATUSES,
    ManagedDnsRegistrationStatus.ABANDONED,
    ManagedDnsRegistrationStatus.RELEASED,
)


async def _draw_managed_dns_status(
    session: Session, lane: DatabaseLane, actor: User
) -> ManagedDnsRegistrationStatus:
    """Renders this node's managed netbbs.org subdomain registration
    status (design doc §16, issue #201) -- shows whatever `netbbs.net.
    managed_dns_flow`'s register/release flows already set. Returns the
    current status so `_managed_dns_status_screen` knows whether to
    offer `[R]egister` or `[G]ive up name` without re-reading it a second
    time."""
    opt_in = await lane.run(get_managed_dns_opt_in)
    name = await lane.run(get_managed_dns_registered_name)
    previous_name = await lane.run(get_managed_dns_previous_name)
    status = await lane.run(get_managed_dns_registration_status)
    published = await lane.run(get_managed_dns_published)
    last_contact_at = await lane.run(get_managed_dns_last_contact_at)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    header_color = await lane.run(effective_header_color_256)

    await session.write_line(
        "\r\n"
        + screen_title(
            "Managed DNS",
            breadcrumb=crumbs(session, own=True),
            subtitle="This node's netbbs.org subdomain registration, if any.",
            width=session.terminal_width,
            clear=redraw_in_place,
            unicode_style=unicode_style, collapsed=collapsed,
            header_color=header_color,
        node_name_gradient=session.node_name_gradient)
    )

    sections: list[Section] = []
    if opt_in is ManagedDnsOptIn.UNDECIDED:
        sections.append(Section("Registration", [
            Field("Status", status_badge("NOT YET DECIDED", tone="neutral", unicode_style=unicode_style), styled=True),
            Note("This node hasn't been asked (or hasn't answered) the managed-DNS opt-in prompt yet."),
        ]))
    elif name is None:
        sections.append(Section("Registration", [
            Field(
                "Status",
                status_badge(
                    "DECLINED" if opt_in is ManagedDnsOptIn.DECLINED else "NO REGISTRATION",
                    tone="neutral", unicode_style=unicode_style,
                ),
                styled=True,
            ),
            Note("This node has no active managed-DNS registration."),
        ]))
    else:
        # "LIVE" is a claim callers can act on, so it is made only once
        # the service has confirmed a published record (`published`):
        # the service matures a registration *before* its first provider
        # upsert, and a failed upsert leaves it matured with no record
        # (`netbbs.link.node_profiles.own_canonical_dns_name` draws the
        # same line for what the node advertises). The badge used to say
        # LIVE on `matured` alone.
        live = status is ManagedDnsRegistrationStatus.MATURED and published
        tone = "success" if live else ("error" if status is ManagedDnsRegistrationStatus.REVOKED else "neutral")
        badge_text = {
            ManagedDnsRegistrationStatus.PENDING: "PENDING",
            ManagedDnsRegistrationStatus.MATURED: "LIVE" if published else "NOT YET PUBLISHED",
            ManagedDnsRegistrationStatus.RELEASED: "RELEASED",
            ManagedDnsRegistrationStatus.ABANDONED: "ABANDONED",
            ManagedDnsRegistrationStatus.REVOKED: "REVOKED",
            ManagedDnsRegistrationStatus.NONE: "NONE",
        }[status]
        display_format, display_timezone = await lane.run(resolve_display_preferences)
        registration: list[Field | Note] = [
            Field("Status", status_badge(badge_text, tone=tone, unicode_style=unicode_style), styled=True),
            Field("Name" if previous_name is None else "New name", f"{name}.netbbs.org", bold=True),
        ]
        if previous_name is not None:
            registration.append(Field("Current name", f"{previous_name}.netbbs.org", bold=True))
        # Issue #640: only a name the service is waiting to hear from
        # has a contact problem worth stating; the other states explain
        # themselves below.
        contact_problem = (
            await lane.run(get_managed_dns_contact_problem)
            if status in _MANAGED_DNS_ACTIVE_STATUSES else None
        )
        if last_contact_at is not None:
            registration.append(Field(
                "Last contact",
                format_for_display(last_contact_at, override_format=display_format, override_timezone=display_timezone),
                color=DATE_COLOR,
            ))
        elif status in _MANAGED_DNS_ACTIVE_STATUSES:
            # The row used to be left out, which made a registration the
            # service had never heard from look the same as a healthy one.
            registration.append(Field(
                "Last contact", "never", color=WARNING_COLOR if contact_problem is not None else VALUE_COLOR,
            ))
        sections.append(Section("Registration", registration))

        # What the state means for the SysOp and what, if anything, they
        # need to do about it -- every state here used to be a bare badge.
        # A revocation takes both halves of a rename, so it is explained
        # even while `previous_name` is still set.
        meaning: list[Field | Note] = []
        if previous_name is None or status is ManagedDnsRegistrationStatus.REVOKED:
            note = (
                await lane.run(get_managed_dns_recovery_note)
                if status is ManagedDnsRegistrationStatus.ABANDONED else None
            )
            contact = (
                await lane.run(get_managed_dns_service_contact)
                if status is ManagedDnsRegistrationStatus.REVOKED else None
            )
            meaning.extend(Note(line) for line in _managed_dns_state_guidance(
                status, published, recovery_refused=note is not None,
                recovery_final=note is not None and note.final, contact=contact,
                contact_failing=contact_problem is not None,
            ))
            if note is not None:
                when = format_for_display(
                    note.at, override_format=display_format, override_timezone=display_timezone
                )
                meaning.append(Note(
                    f"Last automatic attempt ({when}): {sanitize_text(note.text)[:240]}", color=WARNING_COLOR
                ))
        if previous_name is not None:
            if status is ManagedDnsRegistrationStatus.ABANDONED:
                meaning.append(Note(
                    "The new name is inactive. Cancel the change to restore the current name.",
                    color=WARNING_COLOR,
                ))
            elif status is ManagedDnsRegistrationStatus.REVOKED:
                meaning.append(Note("Both names were revoked together; the change cannot be cancelled."))
            else:
                meaning.append(Note("The new name is reserved and maturing."))
        if contact_problem is not None:
            when = format_for_display(
                contact_problem.at, override_format=display_format, override_timezone=display_timezone
            )
            # The text can quote a service's or a proxy's answer, so it
            # is sanitised and bounded here where it is shown.
            meaning.append(Note(
                f"Last check-in attempt ({when}): {sanitize_text(contact_problem.text)[:400]}.",
                color=WARNING_COLOR,
            ))
        if meaning:
            sections.append(Section("What this means", meaning))
        if status in _MANAGED_DNS_ACTIVE_STATUSES:
            # Design doc §16 Decision 6 (issue #603): the convention
            # that makes the name reach this board, against this node's
            # own listeners. Stated here, where the SysOp looks after
            # registering, not only in the editor they saw once.
            listeners = await lane.run(get_managed_dns_local_listeners)
            sections.append(Section(
                "Reaching this board by name",
                [Note(line) for line in managed_dns_standard_ports_lines(listeners)],
            ))
        if status in _NODE_PAGE_STATUSES:
            sections.append(Section(
                "Web page",
                _node_page_lines(
                    previous_name or name, await lane.run(get_node_page),
                    tagline=await lane.run(get_node_tagline),
                    # During a rename the page shown is the current name's,
                    # still live, whatever has become of the new one.
                    lapsed=status not in _MANAGED_DNS_ACTIVE_STATUSES and previous_name is None,
                ),
            ))
    await _write_sections(session, sections, unicode_style=unicode_style)
    actions = [menu_key("R", "egister")]
    if previous_name is not None and status is not ManagedDnsRegistrationStatus.REVOKED:
        actions.append(menu_key("C", "ancel change"))
    elif status in _MANAGED_DNS_ACTIVE_STATUSES:
        if previous_name is None:
            actions.append(menu_key("G", "ive up name"))
            actions.append(menu_key("N", "ew name"))
    if status in _NODE_PAGE_STATUSES:
        actions.append(menu_key("W", "eb page"))
        actions.append(menu_key("T", "agline"))
    if await lane.run(get_managed_dns_admin_token) is not None:
        # Design doc §16 Decision 4: the one node whose operator also
        # runs the service gets the service's own table here.
        actions.append(menu_key("A", "dminister service"))
    actions.append(menu_key("B", "ack"))
    await session.write_line("\r\n" + action_bar(actions, width=session.terminal_width, height=session.terminal_height))
    await _choice_prompt(session)
    return status


_NODE_PAGE_DESCRIPTIONS = {
    NODE_PAGE_SHOWN: "Shown, not indexed by search engines",
    NODE_PAGE_INDEXED: "Shown and indexed by search engines",
    NODE_PAGE_OFF: "Off",
}


def _node_page_lines(
    name: str, setting: str, *, lapsed: bool = False, tagline: str | None = None,
) -> list[Field | Note]:
    """The "Web page" section of the DNS screen (design doc §8.13, issue
    #1165): where this node's page lives and the SysOp's choice about it.
    The page is built from what Reliable Link knows, so it appears only
    once Reliable Link has met this node, and a change reaches it with
    the next contact. `lapsed` (an abandoned or released name, issue
    #1177) says the page stays, marked as left, until it is turned off."""
    rows: list[Field | Note] = [
        Field("Address", f"{NODE_PAGE_URL_PREFIX}{name}"),
        Field(
            "Setting", _NODE_PAGE_DESCRIPTIONS[setting],
            color=MUTED_COLOR if setting == NODE_PAGE_OFF else VALUE_COLOR,
        ),
        # Issue #1172: one line of the SysOp's own words, shown under the
        # name. It is published only while the page is not off.
        Field("Tagline", tagline or "(none)", color=VALUE_COLOR if tagline else MUTED_COLOR),
        Note(
            "A public page about this node: its name, how callers dial in, and when it joined and was "
            "last heard on NetBBS Link. It appears once Reliable Link has met this node, and a change "
            "here reaches it with the next contact. [W]eb page steps through the settings; [T]agline "
            f"sets one line of up to {MAX_TAGLINE} characters about this board, shown as plain text."
        ),
    ]
    if lapsed:
        rows.append(Note(
            "This name is no longer registered, but its page stays, marked as left. "
            "Set it to off here to take the page down."
        ))
    return rows


async def _edit_node_tagline(session: Session, lane: DatabaseLane, actor: User) -> str:
    """Issue #1172: edit the page's tagline in place -- the #557 field
    convention: the line opens on the current text, Enter saves, blank
    clears, Esc keeps. Returns the outcome line for the redrawn screen.
    The setting and its audit entry commit together."""
    current = await lane.run(get_node_tagline)
    await session.write_line("")
    await write_field_prompt(
        session, colored(f"Tagline, up to {MAX_TAGLINE} characters ({_CLEAR_HINT}):", fg_color=MUTED_COLOR),
        hint=_CLEAR_HINT,
    )
    try:
        raw = await _read_seeded_line(session, initial=current or "")
    except InputCancelled:
        return colored("Tagline unchanged.", fg_color=MUTED_COLOR)

    def _apply(db: Database) -> str | None:
        db.connection.execute("BEGIN IMMEDIATE")
        try:
            stored = set_node_tagline_without_commit(db, raw)
            record_action_without_commit(db, actor=actor, action="set_node_tagline", detail=stored or "(cleared)")
        except BaseException:
            db.connection.rollback()
            raise
        db.connection.commit()
        return stored

    try:
        stored = await lane.run(_apply)
    except ValueError as exc:
        return colored(f"{exc} Tagline unchanged.", fg_color=ERROR_COLOR)
    if stored is None:
        return colored("Tagline cleared.", fg_color=MUTED_COLOR)
    return colored("Tagline saved. It reaches the page with Reliable Link's next contact.", fg_color=MUTED_COLOR)


def _managed_dns_state_guidance(
    status: ManagedDnsRegistrationStatus, published: bool, *, recovery_refused: bool = False,
    recovery_final: bool = False,
    contact: str | None = None,
    contact_failing: bool = False,
) -> list[str]:
    """One or two plain sentences per registration state: what it means
    and what happens next, for a standalone (not mid-rename) name. The
    node knows none of the service's timings for certain (they are
    operator parameters), so the durations are the shipped defaults
    hedged as such. `recovery_refused` is whether an automatic reclaim
    has already been turned down since this abandonment: then the
    service may no longer hold the name for this node, and the screen
    must not claim it does (Codex review of PR #608). `contact_failing`
    is whether the node's latest check-in never reached the service
    (issue #640): then "nothing to do" is false for every active state,
    and the screen used to say it for a week until the name was swept."""
    if contact_failing and status in _MANAGED_DNS_ACTIVE_STATUSES:
        if status is ManagedDnsRegistrationStatus.PENDING:
            consequence = (
                "Reserved, but not on its way into DNS: it goes live only after about a day of "
                "uninterrupted contact, and this node's check-ins are not reaching the service (below)."
            )
        elif published:
            consequence = "Live, but this node's check-ins are not reaching the service (below)."
        else:
            consequence = (
                "Accepted, but not in DNS: the record is published on a check-in, and this node's are "
                "not reaching the service (below)."
            )
        return [
            consequence + " A name the service has not heard from for about a week is taken out of DNS "
            "and held for this node. The node retries every 15 minutes."
        ]
    if status is ManagedDnsRegistrationStatus.PENDING:
        return [
            "Reserved but not yet in DNS: it goes live once this node has stayed in contact with the "
            "service for about a day (the node checks in every 15 minutes by itself). Nothing to do."
        ]
    if status is ManagedDnsRegistrationStatus.MATURED and not published:
        return [
            "The service accepted the name but has not confirmed a published DNS record yet; the next "
            "check-in retries the publication. Nothing to do unless this persists."
        ]
    if status is ManagedDnsRegistrationStatus.ABANDONED and recovery_refused:
        retry = (
            "will not be retried: the service said this node cannot reclaim it"
            if recovery_final else "is retried every 15 minutes"
        )
        return [
            "The service stopped hearing from this node for about a week and took the record out of "
            f"DNS. The node's automatic reclaim was refused (below) and {retry}; "
            "[R]egister registers the name afresh if it is still free."
        ]
    if status is ManagedDnsRegistrationStatus.ABANDONED:
        return [
            "The service stopped hearing from this node for about a week and took the record out of "
            "DNS. The name is held for this node, which reclaims it by itself (retrying every 15 "
            "minutes); [R]egister reclaims it now."
        ]
    if status is ManagedDnsRegistrationStatus.RELEASED:
        # The node records no release time and never asks the service
        # again about a released name, so it cannot know whether the
        # cooldown (about three months by default) has run out; the
        # wording says what it knows (Codex review of PR #608).
        return [
            "Released by this node. The service holds a released name for its former holder for a "
            "while (about three months by default) before anyone else may take it; [R]egister "
            "reclaims it while that lasts, and registers it afresh afterwards if it is still free."
        ]
    if status is ManagedDnsRegistrationStatus.REVOKED:
        # Design doc §16 Decision 4: the registrant is told that, and
        # where to write, not why. The channel is free text the service
        # sent; sanitised and bounded here where it is shown.
        where = (
            f" To dispute it, contact {sanitize_text(contact)[:200]}."
            if contact else " The service named no contact channel."
        )
        return [
            "The service operator revoked this name. Its record is out of DNS, and this node cannot get "
            "it back with the credential it holds; [R]egister with a different name works as usual."
            + where
        ]
    return []


@located("Managed DNS")
async def _managed_dns_status_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """
    Design doc §16, issue #201: status plus, once release/reclaim exist
    to make them meaningful, the actions themselves -- `[R]egister`
    (also how a SysOp who initially declined the opt-in prompt, or wants
    to reclaim a previously-released name, gets in) and `[G]ive up name`,
    mutually exclusive on whether this node currently has an active
    (`pending`/`matured`) registration. Same single-item hotkey-menu-loop
    shape as `_post_action_screen`.
    """
    status = await _draw_managed_dns_status(session, lane, actor)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "r":
            await session.write_line("")
            # register_via_prompt reports every terminal outcome inline,
            # including missing configuration and service rejections; held
            # here and shown on the redrawn status screen, not under a clear.
            flow = _TrailingOutput(session)
            await register_via_prompt(flow, lane, actor=actor)
            flow.announce_rest()
            status = await _draw_managed_dns_status(session, lane, actor)
        elif (
            choice == "g" and status in _MANAGED_DNS_ACTIVE_STATUSES
            and await lane.run(get_managed_dns_previous_name) is None
        ):
            await session.write_line("")
            flow = _TrailingOutput(session)
            await release_registration(flow, lane)
            flow.announce_rest()
            status = await _draw_managed_dns_status(session, lane, actor)
        elif (
            choice == "n" and status in _MANAGED_DNS_ACTIVE_STATUSES
            and await lane.run(get_managed_dns_previous_name) is None
        ):
            await session.write_line("")
            flow = _TrailingOutput(session)
            await rename_registration(flow, lane)
            flow.announce_rest()
            status = await _draw_managed_dns_status(session, lane, actor)
        elif (
            choice == "c" and await lane.run(get_managed_dns_previous_name) is not None
            and status is not ManagedDnsRegistrationStatus.REVOKED
        ):
            await session.write_line("")
            flow = _TrailingOutput(session)
            await cancel_registration_rename(flow, lane)
            flow.announce_rest()
            status = await _draw_managed_dns_status(session, lane, actor)
        elif choice == "w" and status in _NODE_PAGE_STATUSES:
            # Issue #1165: a toggle that steps through the three settings.
            # The setting and its audit entry commit together.
            def _step(db: Database) -> None:
                db.connection.execute("BEGIN IMMEDIATE")
                try:
                    current = get_node_page(db)
                    following = NODE_PAGE_STATES[(NODE_PAGE_STATES.index(current) + 1) % len(NODE_PAGE_STATES)]
                    set_node_page_without_commit(db, following)
                    record_action_without_commit(db, actor=actor, action="set_node_page", detail=following)
                except BaseException:
                    db.connection.rollback()
                    raise
                else:
                    db.connection.commit()

            await lane.run(_step)
            status = await _draw_managed_dns_status(session, lane, actor)
        elif choice == "t" and status in _NODE_PAGE_STATUSES:
            _announce_line(session, await _edit_node_tagline(session, lane, actor))
            status = await _draw_managed_dns_status(session, lane, actor)
        elif choice == "a" and await lane.run(get_managed_dns_admin_token) is not None:
            await session.write_line("")
            await administer_managed_dns_service(session, lane, actor)
            status = await _draw_managed_dns_status(session, lane, actor)
        elif choice == HELP_KEY:
            previous_name, admin_token, header_color, unicode_style = await lane.run(lambda db: (
                get_managed_dns_previous_name(db), get_managed_dns_admin_token(db),
                effective_header_color_256(db), unicode_style_enabled(db, actor),
            ))
            help_entries = [MenuEntry(label=menu_key("R", "egister"),
                                      brief="Ask for a netbbs.org name")]
            if previous_name is not None and status is not ManagedDnsRegistrationStatus.REVOKED:
                help_entries.append(MenuEntry(label=menu_key("C", "ancel change"),
                                              brief="Keep the old name after all"))
            elif status in _MANAGED_DNS_ACTIVE_STATUSES and previous_name is None:
                help_entries.append(MenuEntry(label=menu_key("G", "ive up name"),
                                              brief="Release the name"))
                help_entries.append(MenuEntry(label=menu_key("N", "ew name"),
                                              brief="Move to a different name"))
            if status in _NODE_PAGE_STATUSES:
                help_entries.append(MenuEntry(label=menu_key("W", "eb page"),
                                              brief="Show, index or hide the web page"))
                help_entries.append(MenuEntry(label=menu_key("T", "agline"),
                                              brief="One line about this board"))
            if admin_token is not None:
                help_entries.append(MenuEntry(label=menu_key("A", "dminister service"),
                                              brief="Every name the service holds"))
            await show_menu_help(
                session, "Managed DNS help", help_entries,
                about="A free name under netbbs.org that callers can use to reach this node, "
                      "kept pointing at its current address.",
                header_color=header_color, unicode_style=unicode_style,
            )
            status = await _draw_managed_dns_status(session, lane, actor)
        else:
            await session.write(reject_unhandled_key(choice))


@located("Timestamp display")
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

    # Hoisted here, once, rather than re-fetched inside _timezone_field
    # below -- it needs these same values to open the timezone picker,
    # and they're already computed fresh for edit_resource_draft's own
    # call at the bottom of this function anyway.
    description_level = await lane.run(menu_description_level, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    accent_color = await lane.run(effective_accent_color_256)
    header_color = await lane.run(effective_header_color_256)

    @inline_field
    async def _format_field(session: Session, lane: DatabaseLane, draft: dict) -> None:
        await write_field_prompt(
            session, colored(f"Timestamp format ({_EDIT_HINT}):", fg_color=MUTED_COLOR), hint=_EDIT_HINT,
        )
        try:
            new_fmt = (await _read_seeded_line(session, initial=draft["format"])).strip()
        except InputCancelled:
            return
        if not new_fmt or new_fmt == draft["format"]:
            return

        def _apply(db: Database) -> None:
            set_display_format(db, new_fmt)
            record_action(db, actor=actor, action="set_display_format", detail=new_fmt)

        try:
            await lane.run(_apply)
        except ValueError as exc:
            await write_field_message(session, colored(str(exc), fg_color=MUTED_COLOR))
        else:
            draft["format"] = new_fmt

    async def _timezone_field(session: Session, lane: DatabaseLane, draft: dict) -> None:
        # Dogfood feature request ("Can we get a timezone selector for
        # the SysOp console?"): this used to be a bare free-text prompt
        # -- a SysOp had to already know and correctly type an exact
        # IANA identifier ('Europe/Berlin'), with `is_valid_timezone`
        # only catching a typo after the fact. `pick_item` (the same
        # searchable/paginated browse-and-select component every other
        # "choose one of many named things" screen in this module
        # already uses) makes this an actual picker instead -- substring
        # search and Tab completion come for free. `zoneinfo.available_
        # timezones()` is a flat set of strings with no natural integer
        # id of its own (unlike every other pick_item caller's DB-backed
        # rows), so `_zone_ids` below synthesizes one: a fixed index
        # into a sorted, one-time-computed list, stable for exactly as
        # long as this screen visit lasts, which is all `pick_item`
        # itself needs.
        zones = sorted(available_timezones())
        zone_ids = {name: index for index, name in enumerate(zones)}
        selected = await pick_item(
            session, zones,
            name_of=lambda tz: tz,
            stable_id_of=lambda tz: zone_ids[tz],
            title="Timezone",
            empty_message="No timezones available.",
            redraw_in_place=redraw_in_place,
            unicode_style=unicode_style,
            collapsed=collapsed,
            accent_color=accent_color,
            header_color=header_color,
        )
        if selected is None:
            return

        def _apply(db: Database) -> None:
            set_display_timezone(db, selected)
            record_action(db, actor=actor, action="set_display_timezone", detail=selected)

        try:
            await lane.run(_apply)
        except ValueError as exc:
            _announce_line(session, colored(str(exc), fg_color=MUTED_COLOR))
        else:
            draft["timezone"] = selected

    fields = [
        FieldSpec(
            key="format", label="Format",
            render=lambda d: d["format"], prompt=_format_field,
            help=(
                "A Python strftime pattern controlling the *shape* of every displayed "
                "timestamp node-wide, e.g. '%Y-%m-%d %H:%M' for '2026-08-22 14:05'. "
                "Doesn't affect which instant is shown -- see Timezone for that."
            ),
        ),
        FieldSpec(
            key="timezone", label="Timezone",
            render=lambda d: d["timezone"], prompt=_timezone_field,
            help=(
                "Opens a searchable list of every IANA timezone (e.g. 'Europe/Berlin', "
                "'America/New_York') controlling *which instant* every displayed timestamp "
                "shows, node-wide. Getting this wrong leaves every timestamp reshaped but "
                "still pointing at the wrong wall-clock time."
            ),
        ),
    ]
    await edit_resource_draft(
        session, lane,
        title="Timestamp display", own=True,
        fields=fields,
        draft=draft, save=None,
        back_menu_text=menu_key("B", "ack"),
        description_level=description_level,
        redraw_in_place=redraw_in_place,
        unicode_style=unicode_style,
        collapsed=collapsed,
        accent_color=accent_color,
        header_color=header_color,
    )


_MIB = 1024 * 1024


class _LimitsError(ValueError):
    """A limits-screen save the SysOp must correct; keeps the draft open."""


async def _limits_settings_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """Issue #725: four node-wide settings the node has always read, each
    with a validating setter, that no screen ever called -- the upload
    cap, the grace between expiry and deletion, the default channel
    invitation expiry and the chat scrollback limit. Before this the
    only way to change them was `scripts/set_node_config.py`, a
    development helper that writes the raw string unchecked.

    One draft editor rather than four immediate-mode fields: four values
    on one screen are a draft (design doc §3.5), and none of them is so
    urgent that it has to apply the moment it is typed.
    """

    def _load(db: Database) -> dict:
        return {
            "upload_bytes": get_max_upload_bytes(db),
            "grace_days": get_expiry_grace_period_days(db),
            "invite_days": get_invitation_expiry_days(db),
            "scrollback": get_scrollback_limit(db),
            "map_level": get_node_map_min_level(db),
            "mail_level": get_mail_min_level(db),
            "oneliner_level": get_oneliner_min_level(db),
            "crosspost_boards": get_crosspost_board_limit(db),
        }

    current = await lane.run(_load)
    # The upload cap is edited in whole MiB. A byte count that is not a
    # whole MiB (only reachable through the dev script) is kept exactly
    # until the SysOp edits this field -- tracked as "edited", not by
    # comparing numbers, so re-entering the floored value still applies
    # it (Codex review).
    draft: dict = {**current, "upload_mib": max(1, current["upload_bytes"] // _MIB), "upload_edited": False}
    @inline_field
    async def _upload_field(session: Session, lane: DatabaseLane, draft: dict) -> None:
        # `_int_field`'s shape, except that Esc and Enter differ here:
        # Esc keeps the field untouched, Enter chooses the number shown.
        await write_field_prompt(session, colored(f"Largest upload, in MiB ({_EDIT_HINT}):", fg_color=MUTED_COLOR))
        try:
            raw = (await _read_seeded_line(session, initial=str(draft["upload_mib"]))).strip()
        except InputCancelled:
            await session.write_line("")
            return
        try:
            value = int(raw) if raw else draft["upload_mib"]
        except ValueError:
            await write_field_message(session, colored("Not a number -- cancelled.", fg_color=MUTED_COLOR))
            return
        draft["upload_mib"] = value
        draft["upload_edited"] = True

    def _upload_render(d: dict) -> str:
        if not d["upload_edited"]:
            return _format_bytes(current["upload_bytes"])
        return f"{d['upload_mib']} MiB"

    levels = await lane.run(level_context)
    fields = [
        FieldSpec(
            key="upload_mib", label="Upload cap",
            render=_upload_render, prompt=_upload_field,
            brief="Largest file a caller may upload", section="Files",
            help=(
                "The largest single upload this node accepts, over Zmodem and the browser alike, in MiB "
                f"(1-{MAX_UPLOAD_BYTES_LIMIT // _MIB}). A reverse proxy in front of the web listener has "
                "its own request-body limit: set that at least 1 MiB higher than this (a browser upload "
                "also carries form framing), or uploads near the cap fail at the proxy."
            ),
        ),
        FieldSpec(
            key="grace_days",
            label="Grace before deletion",
            render=lambda d: f"{d['grace_days']} days", prompt=_int_field("grace_days", "Days"),
            brief="Days before expired items go", section="Retention",
            help=(
                "When a post or file passes its board's or area's maximum age it expires, and is deleted "
                f"this many days later (0-{MAX_SETTING_DAYS}). Until then a SysOp can still recover an "
                "expired file from its area's [E]xpired files screen."
            ),
        ),
        FieldSpec(
            key="invite_days",
            label="Channel invitations expire after",
            render=lambda d: "never" if d["invite_days"] is None else f"{d['invite_days']} days",
            prompt=_optional_int_field("invite_days", "Days (blank = never)"),
            brief="Unused chat invitations lapse", section="Chat",
            help=(
                f"A channel invitation not yet accepted lapses after this many days (1-{MAX_SETTING_DAYS}). "
                "Clear the field for invitations that never expire. Applies to invitations sent from now on."
            ),
        ),
        FieldSpec(
            key="scrollback",
            label="Chat scrollback",
            render=lambda d: f"{d['scrollback']} messages per channel",
            prompt=_int_field("scrollback", "Messages kept per channel"),
            brief="History kept in each channel", section="Chat",
            help=(
                f"How many messages and join/leave lines each channel keeps (1-{MAX_SCROLLBACK_LIMIT}). "
                "Carried Link channels count too. Lowering it trims each channel the next time "
                "something is said there."
            ),
        ),
        FieldSpec(
            key="map_level",
            label="Node map level",
            render=lambda d: _setting_level_label(levels, d["map_level"]),
            prompt=_int_field("map_level", "Lowest level"),
            brief="Who may open the node map", section="Directory",
            help=(
                f"The lowest level that may open the node map, \"Nodes known to\" this board, from the "
                f"Directory (0-{MAX_NODE_MAP_MIN_LEVEL}). A guest is an ordinary account: to keep the map "
                "from guests, set this above the guest account's level. Only offered while Link is on."
            ),
        ),
        FieldSpec(
            key="mail_level",
            label="Mail level",
            render=lambda d: _setting_level_label(levels, d["mail_level"]),
            prompt=_int_field("mail_level", "Lowest level"),
            brief="Who may read and send mail", section="Mail",
            help=(
                f"The lowest level that may open E-mail, to read and to send, here and to other BBSes "
                f"(0-{MAX_MAIL_MIN_LEVEL}; 0 is everyone). Mail to an account below it still arrives and "
                "waits until its level is raised. The guest account never has mail, whatever its level: "
                "every guest shares its mailbox."
            ),
        ),
        FieldSpec(
            key="crosspost_boards",
            label="Cross-post boards",
            render=lambda d: "off" if d["crosspost_boards"] == 1 else f"up to {d['crosspost_boards']} boards",
            prompt=_int_field("crosspost_boards", "Boards"),
            brief="Most boards one post may go to", section="Message boards",
            help=(
                "The most message boards a caller may send one new post to with [T]o boards on its review "
                f"screen, the board it was written on included (1-{MAX_CROSSPOST_BOARD_LIMIT}; 1 turns "
                "cross-posting off). Each board gets its own copy, so this bounds what one post writes."
            ),
        ),
        FieldSpec(
            key="oneliner_level",
            label="Oneliner level",
            render=lambda d: _setting_level_label(levels, d["oneliner_level"]),
            prompt=_int_field("oneliner_level", "Lowest level"),
            brief="Who may add to the oneliner wall", section="Oneliners",
            help=(
                f"The lowest level that may add a line to the oneliner wall under Recent callers "
                f"(0-{MAX_ONELINER_MIN_LEVEL}; 0 is everyone). Every caller can read it. A guest is an "
                "ordinary account: set this above the guest account's level to keep guests read-only."
            ),
        ),
    ]

    async def save(draft: dict) -> list[str]:
        upload_bytes = draft["upload_mib"] * _MIB if draft["upload_edited"] else current["upload_bytes"]
        values = {
            "upload_bytes": upload_bytes,
            "grace_days": draft["grace_days"],
            "invite_days": draft["invite_days"],
            "scrollback": draft["scrollback"],
            "map_level": draft["map_level"],
            "mail_level": draft["mail_level"],
            "oneliner_level": draft["oneliner_level"],
            "crosspost_boards": draft["crosspost_boards"],
        }
        # Checked here, before anything is written, so one bad value
        # cannot leave the others half saved; the setters check again.
        if not 0 < upload_bytes <= MAX_UPLOAD_BYTES_LIMIT:
            raise _LimitsError(f"Upload cap must be 1-{MAX_UPLOAD_BYTES_LIMIT // _MIB} MiB.")
        if not 0 <= values["grace_days"] <= MAX_SETTING_DAYS:
            raise _LimitsError(f"Grace before deletion must be 0-{MAX_SETTING_DAYS} days.")
        if values["invite_days"] is not None and not 0 < values["invite_days"] <= MAX_SETTING_DAYS:
            raise _LimitsError(f"Invitation expiry must be 1-{MAX_SETTING_DAYS} days, or blank for never.")
        if not 0 < values["scrollback"] <= MAX_SCROLLBACK_LIMIT:
            raise _LimitsError(f"Chat scrollback must be 1-{MAX_SCROLLBACK_LIMIT} messages.")
        if not 0 <= values["map_level"] <= MAX_NODE_MAP_MIN_LEVEL:
            raise _LimitsError(f"Node map level must be 0-{MAX_NODE_MAP_MIN_LEVEL}.")
        if not 0 <= values["mail_level"] <= MAX_MAIL_MIN_LEVEL:
            raise _LimitsError(f"Mail level must be 0-{MAX_MAIL_MIN_LEVEL}.")
        if not 0 <= values["oneliner_level"] <= MAX_ONELINER_MIN_LEVEL:
            raise _LimitsError(f"Oneliner level must be 0-{MAX_ONELINER_MIN_LEVEL}.")
        if not 1 <= values["crosspost_boards"] <= MAX_CROSSPOST_BOARD_LIMIT:
            raise _LimitsError(f"Cross-post boards must be 1-{MAX_CROSSPOST_BOARD_LIMIT}.")
        changed = [key for key in values if values[key] != current[key]]

        config_keys = {
            "upload_bytes": MAX_UPLOAD_BYTES_CONFIG_KEY,
            "grace_days": EXPIRY_GRACE_PERIOD_CONFIG_KEY,
            "invite_days": INVITATION_EXPIRY_DAYS_CONFIG_KEY,
            "scrollback": SCROLLBACK_LIMIT_CONFIG_KEY,
            "map_level": NODE_MAP_MIN_LEVEL_CONFIG_KEY,
            "mail_level": MAIL_MIN_LEVEL_CONFIG_KEY,
            "oneliner_level": ONELINER_MIN_LEVEL_CONFIG_KEY,
            "crosspost_boards": CROSSPOST_BOARD_LIMIT_CONFIG_KEY,
        }

        def _persist(db: Database) -> None:
            if not changed:
                return
            # The values and the audit entry commit together or not at
            # all (Codex review). The range checks above are the same
            # ones each setter makes; `None` invitation expiry is stored
            # as "", as `set_invitation_expiry_days` does.
            db.connection.execute("BEGIN IMMEDIATE")
            try:
                for key in changed:
                    set_config_without_commit(db, config_keys[key], "" if values[key] is None else str(values[key]))
                record_action_without_commit(
                    db, actor=actor, action="set_limits_and_retention",
                    detail=" ".join(f"{key}={values[key]}" for key in changed),
                )
            except BaseException:
                db.connection.rollback()
                raise
            else:
                db.connection.commit()

        await lane.run(_persist)
        return changed

    changed = await edit_resource_draft(
        session, lane,
        title="Limits & retention",
        fields=fields, draft=draft, save=save, error_type=_LimitsError,
        save_menu_text=menu_key("S", "ave"),
        back_menu_text=menu_key("B", "ack"),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if changed is None:
        return
    _announce_line(session, "Saved. Applies from now on." if changed else "Nothing changed.")


# -- Operating limits (issue #730) -------------------------------------

# One hotkey per group on the overview: its first letter (issue #1158), so
# the group names (`netbbs.net.policy_settings.GROUPS`) start with different
# letters, none of them `n` (Node configuration) or `b` (Back). Checked here,
# once, so a group added later cannot quietly take another's key.
_POLICY_GROUP_KEYS = {group: group[0].lower() for group in POLICY_GROUPS}


_NODE_CONFIGURATION_KEY = "n"
# Issue #1504: the Link capacity preset, beside the group editors.
_CAPACITY_PRESET_KEY = "a"


if len({*_POLICY_GROUP_KEYS.values(), _NODE_CONFIGURATION_KEY, _CAPACITY_PRESET_KEY, "b"}) != len(_POLICY_GROUP_KEYS) + 3:
    raise RuntimeError(f"policy groups need different first letters: {sorted(_POLICY_GROUP_KEYS)}")


_POLICY_CONFIG_NOTE = (
    "Set in the config file or on the command line, which wins over this screen. Change it there, "
    "or remove it there to manage it here."
)


class _PolicyError(ValueError):
    """A limits save the SysOp must correct; keeps the draft open."""


class _QwkSettingsError(ValueError):
    """A QWK settings save the SysOp must correct; keeps the draft open."""


async def _qwk_settings_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """Issue #1389 (design doc §6.10): the two node-wide settings of
    callers' QWK offline mail -- how many messages a packet holds, and the
    BBS ID packets are named by. A draft, saved together."""

    def _load(db: Database) -> dict:
        return {"qwk_messages": get_qwk_packet_messages(db), "qwk_id": get_qwk_bbs_id_setting(db)}

    current = await lane.run(_load)
    qwk_default_id = await lane.run(derived_qwk_bbs_id)
    draft = dict(current)
    fields = [
        FieldSpec(
            key="qwk_messages",
            label="QWK packet size",
            render=lambda d: f"up to {d['qwk_messages']} messages",
            prompt=_int_field("qwk_messages", "Messages"),
            brief="Most messages in one packet",
            help=(
                "The most messages one QWK packet holds "
                f"({MIN_QWK_PACKET_MESSAGES}-{MAX_QWK_PACKET_MESSAGES}). The rest wait for the caller's next "
                "packet. A packet also stops at 16 MiB of text, whatever this says."
            ),
        ),
        FieldSpec(
            key="qwk_id",
            label="QWK BBS ID",
            render=lambda d: d["qwk_id"] or f"{qwk_default_id} (from the node name)",
            prompt=_optional_text_field("qwk_id"),
            brief="Name of every packet, NAME.QWK",
            help=(
                "The BBS ID QWK packets are named by (ID.QWK) and that a reply packet (ID.REP) must carry: "
                "1-8 letters and digits. Empty uses the node's name, cut to fit. Changing it makes readers "
                "treat this node as a new BBS, and reply packets made for the old ID are refused, so set it "
                "once."
            ),
        ),
    ]

    async def save(draft: dict) -> list[str]:
        values = {
            "qwk_messages": draft["qwk_messages"],
            "qwk_id": (draft["qwk_id"] or "").strip().upper(),
        }
        if not MIN_QWK_PACKET_MESSAGES <= values["qwk_messages"] <= MAX_QWK_PACKET_MESSAGES:
            raise _QwkSettingsError(
                f"QWK packet size must be {MIN_QWK_PACKET_MESSAGES}-{MAX_QWK_PACKET_MESSAGES} messages."
            )
        if values["qwk_id"] and not is_valid_qwk_bbs_id(values["qwk_id"]):
            raise _QwkSettingsError("A QWK BBS ID is 1-8 letters and digits, or empty for the node name.")
        changed = [key for key in values if values[key] != current[key]]
        config_keys = {"qwk_messages": QWK_PACKET_MESSAGES_CONFIG_KEY, "qwk_id": QWK_BBS_ID_CONFIG_KEY}

        def _persist(db: Database) -> None:
            if not changed:
                return
            db.connection.execute("BEGIN IMMEDIATE")
            try:
                for key in changed:
                    set_config_without_commit(db, config_keys[key], str(values[key]))
                record_action_without_commit(
                    db, actor=actor, action="set_qwk_settings",
                    detail=" ".join(f"{key}={values[key]}" for key in changed),
                )
            except BaseException:
                db.connection.rollback()
                raise
            else:
                db.connection.commit()

        await lane.run(_persist)
        return changed

    changed = await edit_resource_draft(
        session, lane,
        title="QWK offline mail",
        fields=fields, draft=draft, save=save, error_type=_QwkSettingsError,
        save_menu_text=menu_key("S", "ave"),
        back_menu_text=menu_key("B", "ack"),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if changed is None:
        return
    _announce_line(session, "Saved. Applies from now on." if changed else "Nothing changed.")


def _policy_source(view: PolicyView, draft_value: Any = None, *, in_draft: bool = False) -> tuple[str, str]:
    """`(value, where it comes from)` for one setting as it will be at the
    next start."""
    if view.overridden:
        return format_policy_value(view.setting, view.running), "set in config"
    value = draft_value if in_draft else view.stored
    if value is None:
        return format_policy_value(view.setting, view.default), "default"
    return format_policy_value(view.setting, value), "set here"


def _policy_running_note(view: PolicyView, next_value: Any) -> str | None:
    """Said when the node is running with something other than what the
    next start will use."""
    if view.running is None or view.overridden:
        return None
    if view.setting.kind is list:
        differs = list(view.running) != list(next_value)
    else:
        differs = view.running != next_value
    if not differs:
        return None
    return f"running with {format_policy_value(view.setting, view.running)} until the next start"


@located("Operating limits")
async def _policy_settings_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """Issue #730: operating policy that used to live only in `netbbs.toml`
    -- the Link carry caps, peering and limits, the live relay's bounds, the
    login throttle and the shutdown delays. An overview of the six groups;
    each group opens as a draft editor.

    Everything here applies at the next start (`netbbs.net.policy_settings`
    explains why), and a key the config file or command line sets wins and
    is shown as such rather than offered for editing."""
    chrome = await _load_chrome(lane, actor)
    defaults = NodeConfig()
    page = 0
    while True:
        views, started = await lane.run(load_policy_views, defaults)
        rows: list[Field | Note] = []
        for group in POLICY_GROUPS:
            members = [view for view in views if view.setting.group == group]
            here = sum(1 for view in members if view.stored is not None and not view.overridden)
            config = sum(1 for view in members if view.overridden)
            pending = sum(
                1 for view in members if _policy_running_note(view, view.stored if view.stored is not None
                                                              else view.default) is not None
            )
            parts = [f"{len(members)} settings"]
            if here:
                parts.append(f"{here} set here")
            if config:
                parts.append(f"{config} set in config")
            if pending:
                parts.append(f"{pending} waiting for a restart")
            rows.append(Field(group, ", ".join(parts)))
        notes: list[Field | Note] = [Note("Changes apply the next time the node starts.")]
        if not started:
            notes.append(Note(
                "The node has not started since this version, so which settings the config file "
                "holds is not known yet. A value the config file sets will still win."
            ))
        choice, page = await show_detail(
            session,
            title=_detail_title(session, chrome, "Operating limits", own=True),
            sections=[Section(None, rows), Section(None, notes)],
            actions=[
                *((key, menu_key(group[0], group[1:])) for group, key in _POLICY_GROUP_KEYS.items()),
                (_CAPACITY_PRESET_KEY, menu_key("A", "pply capacity preset")),
                (_NODE_CONFIGURATION_KEY, menu_key("N", "ode configuration")),
                _BACK_ACTION,
            ],
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style, page=page,
            help_title="Operating limits help",
            help_about=(
                "Link limits, the login throttle and shutdown delays. Changes apply at the next start; a "
                "value set in the config file or on the command line wins and is shown as such. A capacity "
                "preset (Small, Default or Hub) sets the Link groups at once, after showing what it changes."
            ),
        )
        if choice == "b":
            return
        if choice == _CAPACITY_PRESET_KEY:
            from netbbs.net.presets import LINK_CAPACITY, run_preset
            await run_preset(session, lane, actor, LINK_CAPACITY)
            continue
        if choice == _NODE_CONFIGURATION_KEY:
            await _node_configuration_screen(session, lane, actor)
            continue
        group = next(group for group, key in _POLICY_GROUP_KEYS.items() if key == choice)
        await _policy_group_editor(session, lane, actor, group, [v for v in views if v.setting.group == group])


_NODE_CONFIGURATION_NOTE = (
    "Read-only. The node reads these once when it starts, from its config file and command line; "
    "change them there and restart. They cannot be changed here: a wrong listener or address set "
    "from inside NetBBS could lock you out of the session you would need to fix it. This is the "
    "configuration the node resolved; a listener whose optional extra is not installed is listed "
    "but was not started (the service output says so)."
)


async def _node_configuration_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """Issue #748: the bootstrap settings -- listeners, Link addresses, paths
    and the managed-DNS service -- as the node resolved them at its last
    start, each with where its value came from. Read-only by design (#733).

    The same record serves the standalone `python -m netbbs.admin`, which
    cannot know which config file a service uses: it shows what the node
    itself last started with, and says so."""
    chrome = await _load_chrome(lane, actor)
    rows = await lane.run(load_bootstrap_snapshot)
    sections: list[Section] = []
    if rows is None:
        sections.append(Section(None, [Note(
            "The node has not started since this version, so there is nothing to show yet. "
            "Start it once; until then, see the config file and the service's command line.")]))
    else:
        # One table per group: the key is what to look for in the config
        # file, so it names the row; the source says whether it is there.
        groups: dict[str, list[list[str | tuple[str, int]]]] = {}
        for row in rows:
            groups.setdefault(row.group, []).append([
                row.key,
                (row.value, VALUE_COLOR if row.source != "default" else MUTED_COLOR),
                (row.source, METADATA_COLOR if row.source != "default" else MUTED_COLOR),
            ])
        sections += [Section(group, [Table(["Setting", "Value", "From"], table_rows, flex=1)])
                     for group, table_rows in groups.items()]
    sections.append(Section(None, [Note(_NODE_CONFIGURATION_NOTE)]))
    page = 0
    while True:
        choice, page = await show_detail(
            session,
            title=_detail_title(session, chrome, "Node configuration",
                                subtitle="As the node resolved it at its last start"),
            sections=sections, actions=[_BACK_ACTION], page=page,
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            help_title="Node configuration help",
            help_about=(
                "The listeners, Link addresses and paths the node read from its config file and command line "
                "when it last started. Read-only: change them there and restart."
            ),
        )
        if choice == "b":
            return


async def _policy_group_editor(
    session: Session, lane: DatabaseLane, actor: User, group: str, views: list[PolicyView]
) -> None:
    by_key = {view.setting.key: view for view in views}
    draft: dict = {view.setting.key: view.stored for view in views}

    def _render(key: str) -> Callable[[dict], str]:
        view = by_key[key]

        def render(d: dict) -> str:
            value, source = _policy_source(view, d[key], in_draft=True)
            next_value = view.default if d[key] is None else d[key]
            note = _policy_running_note(view, next_value)
            return f"{value}  ({source}{'; ' + note if note else ''})"

        return render

    def _prompt(key: str):
        view = by_key[key]
        setting = view.setting

        if setting.kind is bool:
            async def toggle(session: Session, lane: DatabaseLane, d: dict) -> None:
                if view.overridden:
                    await write_field_message(session, colored(_POLICY_CONFIG_NOTE, fg_color=MUTED_COLOR))
                    return
                current = view.default if d[key] is None else d[key]
                # Toggling back to the default forgets the stored value rather
                # than pinning it, so the setting reads "(default)" again.
                d[key] = None if (not current) == view.default else not current

            return toggle

        @inline_field
        async def prompt(session: Session, lane: DatabaseLane, d: dict) -> None:
            if view.overridden:
                await write_field_message(session, colored(_POLICY_CONFIG_NOTE, fg_color=MUTED_COLOR))
                return
            current = view.default if d[key] is None else d[key]
            initial = " ".join(current) if setting.kind is list else format_policy_value(setting, current)
            hint = "blank = default"
            await write_field_prompt(
                session, colored(f"{setting.label} ({_EDIT_HINT}, {hint}):", fg_color=MUTED_COLOR)
            )
            try:
                raw = await _read_seeded_line(session, initial=initial)
            except InputCancelled:
                await session.write_line("")
                return
            if not raw.strip():
                # Blank returns the setting to its default, a list included:
                # an empty-list override would read "(set here)" forever.
                d[key] = None
                return
            try:
                d[key] = validate_policy_value(setting, parse_policy_text(setting, raw))
            except PolicyValueError as exc:
                await write_field_message(session, colored(str(exc), fg_color=MUTED_COLOR))

        return prompt

    fields = []
    for view in views:
        key = view.setting.key
        fields.append(FieldSpec(
            key=key,
            label=view.setting.label,
            render=_render(key), prompt=_prompt(key),
            help=view.setting.help + (" " + _POLICY_CONFIG_NOTE if view.overridden else ""),
        ))

    async def save(d: dict) -> list[str]:
        changed = {key: value for key, value in d.items() if value != by_key[key].stored}
        for key, value in changed.items():
            if value is not None:
                try:
                    validate_policy_value(by_key[key].setting, value)
                except PolicyValueError as exc:
                    raise _PolicyError(str(exc)) from exc
        if not changed:
            return []

        def _persist(db: Database) -> None:
            # The values and the audit entry commit together or not at all.
            db.connection.execute("BEGIN IMMEDIATE")
            try:
                save_policy_without_commit(db, changed)
                record_action_without_commit(
                    db, actor=actor, action="set_node_policy",
                    detail=" ".join(
                        f"{key}={'default' if value is None else format_policy_value(by_key[key].setting, value)}"
                        for key, value in changed.items()
                    ),
                )
            except BaseException:
                db.connection.rollback()
                raise
            else:
                db.connection.commit()

        await lane.run(_persist)
        return list(changed)

    changed = await edit_resource_draft(
        session, lane,
        title=group,
        fields=fields, draft=draft, save=save, error_type=_PolicyError,
        save_menu_text=menu_key("S", "ave"),
        back_menu_text=menu_key("B", "ack"),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if changed is None:
        return
    _announce_line(
        session, "Saved. Applies the next time the node starts." if changed else "Nothing changed."
    )


_MRC_STATE_BADGES: dict[MrcState, tuple[str, str]] = {
    MrcState.CONNECTED: ("CONNECTED", "success"),
    MrcState.CONNECTING: ("CONNECTING", "neutral"),
    MrcState.BACKOFF: ("RECONNECTING", "neutral"),
    MrcState.ERROR: ("ERROR", "neutral"),
    MrcState.DISABLED: ("OFF", "neutral"),
}


def _mrc_state_line(status: MrcStatus, *, unicode_style: bool) -> str:
    text, tone = _MRC_STATE_BADGES[status.state]
    line = status_badge(text, tone=tone, unicode_style=unicode_style)
    if status.state is MrcState.DISABLED and status.enabled:
        line += colored("  (enabled, waiting for the node to bring it up)", fg_color=MUTED_COLOR)
    return line


@located("Inter-BBS chat (MRC)")
async def _mrc_settings_screen(
    session: Session, lane: DatabaseLane, actor: User, *, node_controls: NodeControls | None
) -> None:
    """
    Issue #275 (design doc §16, issue #165): the node-wide half of MRC
    configuration -- hub host/port/TLS, the site name this node presents
    as, and the free-text INFO fields other MRC users see via `/info`.
    DB-backed (`netbbs.mrc.settings`) and applied live: saving asks the
    running node's bridge to reload and reconnect, so no restart is
    ever needed. Which channels are bridged is decided per channel, on
    each channel's own detail screen -- deliberately not here, so that
    enabling MRC can never, by itself, put any channel on the network.
    """
    current = await lane.run(load_mrc_settings)
    open_current = await lane.run(load_open_room_settings)
    draft: dict = {
        "enabled": current.enabled, "host": current.host, "port": current.port, "tls": current.tls,
        "site_name": current.site_name, "info_sysop": current.info_sysop,
        "info_description": current.info_description, "info_telnet": current.info_telnet,
        "info_ssh": current.info_ssh, "info_web": current.info_web,
        "send_caller_ip": current.send_caller_ip, "send_caller_meta": current.send_caller_meta,
        # Issue #300: the open-room half -- whether callers may open any
        # room, the gates a room opened that way starts with, and the
        # lifecycle bounds.
        "open_rooms": open_current.enabled, "open_min_level": open_current.min_level,
        "open_min_age": open_current.min_age, "open_name_requirement": open_current.name_requirement,
        "open_age_requirement": open_current.age_requirement,
        "open_cap": open_current.cap, "open_retention_days": open_current.retention_days,
        "open_blocklist": list(open_current.blocklist),
    }
    mrc_bridge = node_controls.mrc_bridge if node_controls is not None else None
    unicode_style = await lane.run(unicode_style_enabled, actor)

    def _toggle_tls(draft: dict, direction: int = 1) -> None:
        was = bool(draft.get("tls"))
        draft["tls"] = not was
        # The two well-known hub ports differ only by transport; follow
        # the toggle unless the SysOp chose a custom port.
        if draft.get("port") == default_port_for(was):
            draft["port"] = default_port_for(draft["tls"])

    async def _tls_field(session: Session, lane: DatabaseLane, draft: dict) -> None:
        _toggle_tls(draft)

    def _preamble(draft: dict) -> str:
        lines = [
            "Multi Relay Chat is a public, unauthenticated inter-BBS chat network with one central hub. "
            "Enable it here, then bridge individual channels from each channel's own screen.",
        ]
        if mrc_bridge is not None:
            status = mrc_bridge.status()
            lines.append("Link now: " + _mrc_state_line(status, unicode_style=unicode_style))
            if status.last_error:
                lines.append(colored(f"Last error: {sanitize_text(status.last_error)}", fg_color=MUTED_COLOR))
        else:
            lines.append(colored("(Changes apply the next time the node runs.)", fg_color=MUTED_COLOR))
        return "\r\n".join(lines)

    levels = await lane.run(level_context)
    fields = [
        FieldSpec(
            key="enabled", label="Enabled",
            render=lambda d: "yes" if d["enabled"] else "no",
            prompt=bool_field("enabled"), step=bool_step("enabled"),
            brief="Switch the hub link on or off", section="Hub",
            help="Off by default. Even when on, only channels you bridge individually reach the network.",
        ),
        FieldSpec(
            key="host", label="Hub host",
            render=lambda d: d["host"], prompt=text_field("host", required=True),
            brief="The MRC hub to connect to", section="Hub",
            help="The public hub is mrc.bottomlessabyss.net. Change it only for a private hub. The protocol page (rev 1.26) also lists the operator's pool: na-multi, eu-multi and au-multi.relaychat.net (5000 plain, 5001 TLS) and mrcdev.relaychat.net for development.",
        ),
        FieldSpec(
            key="port", label="Hub port",
            render=lambda d: str(d["port"]), prompt=_int_field("port", "Hub port"),
            brief="5001 with TLS, 5000 without", section="Hub",
        ),
        FieldSpec(
            key="tls", label="TLS",
            render=lambda d: "yes" if d["tls"] else "no", prompt=_tls_field, step=_toggle_tls,
            brief="Encrypt the hub connection", section="Hub",
            help="Recommended. The hub's certificate is verified against the system CA store.",
        ),
        FieldSpec(
            key="site_name", label="Site name",
            render=lambda d: d["site_name"], prompt=text_field("site_name", required=True),
            brief="How this BBS is named on MRC", section="Identity",
            help="Shown next to every caller from this node (nick@site). Printable ASCII, up to 30 characters; spaces become underscores on the wire.",
        ),
        FieldSpec(
            key="info_sysop", label="SysOp",
            render=lambda d: d["info_sysop"] or "(none)", prompt=_optional_text_field("info_sysop"),
            brief="Shown to MRC users via /info", section="Identity",
        ),
        FieldSpec(
            key="info_description", label="Description",
            render=lambda d: d["info_description"] or "(none)", prompt=_optional_text_field("info_description"),
            brief="One line about this BBS", section="Identity",
        ),
        FieldSpec(
            key="info_telnet", label="Telnet address",
            render=lambda d: d["info_telnet"] or "(none)", prompt=_optional_text_field("info_telnet"),
            brief="host[:port] MRC users can call", section="Advertised addresses",
        ),
        FieldSpec(
            key="info_ssh", label="SSH address",
            render=lambda d: d["info_ssh"] or "(none)", prompt=_optional_text_field("info_ssh"),
            brief="host[:port] for SSH callers", section="Advertised addresses",
        ),
        FieldSpec(
            key="info_web", label="Web address",
            render=lambda d: d["info_web"] or "(none)", prompt=_optional_text_field("info_web"),
            brief="URL of the web/xterm.js front door", section="Advertised addresses",
        ),
        FieldSpec(
            key="send_caller_ip", label="Send callers' IP addresses (USERIP)",
            render=lambda d: "yes" if d["send_caller_ip"] else "no",
            prompt=_toggle_draft_field("send_caller_ip"),
            brief="Off: caller addresses stay local", section="About callers",
            help=(
                "Off by default. On: each caller announced on the network is followed by their "
                "connecting IP address (USERIP), which the hub uses to tell this board's callers apart "
                "when it bans one for abuse. The hub's documentation warns that a caller without it "
                "may be removed from room traffic routing; with it off, that risk is yours to take."
            ),
        ),
        FieldSpec(
            key="send_caller_meta", label="Send caller level and SysOp name (BBSMETA)",
            render=lambda d: "yes" if d["send_caller_meta"] else "no",
            prompt=_toggle_draft_field("send_caller_meta"),
            brief="Off: hub learns no caller levels", section="About callers",
            help=(
                "Off by default. On: each announced caller is followed by their security level and "
                "this node's SysOp name (BBSMETA), which the hub's operator uses to judge a caller's "
                "standing on their home board. The SysOp name is the INFO field above."
            ),
        ),
        FieldSpec(
            key="open_rooms", label="Callers may open any room",
            render=lambda d: "yes" if d["open_rooms"] else "no",
            prompt=_toggle_draft_field("open_rooms"),
            brief="Callers' rooms become mrc:<room>", section="Open rooms",
            help=(
                "Off by default. On: the chat channel picker gains a Multi Relay Chat section where a "
                "caller can open any room on the network by name; it becomes a channel named mrc:<room> "
                "here, with the gates below, and is retired once idle. Channels you bridge yourself are "
                "unaffected either way."
            ),
        ),
        FieldSpec(
            key="open_min_level",
            label="Minimum level (open rooms)",
            render=lambda d: _plain_level_label(levels, d["open_min_level"]),
            prompt=_int_field("open_min_level", "Minimum level"),
            brief="Level needed to open or enter one", section="Open rooms",
        ),
        FieldSpec(
            key="open_min_age",
            label="Minimum age (open rooms)",
            render=lambda d: _min_age_label(d["open_min_age"], d["open_age_requirement"]),
            prompt=_min_age_field("open_min_age", "open_age_requirement"),
            brief="Age gate on rooms callers open", section="Open rooms",
            help="The minimum age to open a room." + _VERIFIED_AGE_HELP,
        ),
        FieldSpec(
            key="open_name_requirement",
            label="Name requirement (open rooms)",
            render=lambda d: d["open_name_requirement"] or "none",
            prompt=choice_field("open_name_requirement", [None, "verified", "verified_and_displayed"]),
            brief="Name gate on rooms callers open", section="Open rooms",
        ),
        FieldSpec(
            key="open_cap", label="Cap on open rooms",
            render=lambda d: str(d["open_cap"]), prompt=_int_field("open_cap", "Most rooms open at once"),
            brief="Opening refuses past this cap", section="Open rooms",
        ),
        FieldSpec(
            key="open_retention_days", label="Retention (days)",
            render=lambda d: str(d["open_retention_days"]),
            prompt=_int_field("open_retention_days", "Days an open room may sit idle"),
            brief="Idle open rooms retire after this", section="Open rooms",
            help=(
                "A room callers opened is deleted, with its scrollback, once nobody has been in it for "
                "this many days -- unless someone follows it or you adopt it from its channel screen."
            ),
        ),
        FieldSpec(
            key="open_blocklist", label="Blocked rooms",
            render=lambda d: ", ".join(d["open_blocklist"]) if d["open_blocklist"] else "(none)",
            prompt=_blocklist_field, brief="Rooms callers may not open here", section="Open rooms",
        ),
    ]

    async def save(draft: dict) -> MrcSettings:
        candidate = MrcSettings(
            enabled=bool(draft["enabled"]), host=str(draft["host"]), port=int(draft["port"]), tls=bool(draft["tls"]),
            site_name=str(draft["site_name"]), info_sysop=str(draft["info_sysop"]),
            info_description=str(draft["info_description"]), info_telnet=str(draft["info_telnet"]),
            info_ssh=str(draft["info_ssh"]), info_web=str(draft["info_web"]),
            send_caller_ip=bool(draft["send_caller_ip"]), send_caller_meta=bool(draft["send_caller_meta"]),
        )

        open_candidate = OpenRoomSettings(
            enabled=bool(draft["open_rooms"]), min_level=int(draft["open_min_level"]),
            min_age=draft["open_min_age"], name_requirement=draft["open_name_requirement"],
            age_requirement=draft["open_age_requirement"],
            cap=int(draft["open_cap"]), retention_days=int(draft["open_retention_days"]),
            blocklist=tuple(draft["open_blocklist"]),
        )

        def _persist(db: Database) -> MrcSettings:
            # Validate both halves before writing either, so a bad open-room
            # value cannot leave the hub half saved and the rest not.
            validate_mrc_settings(candidate)
            open_saved = save_open_room_settings(db, open_candidate)
            saved = save_mrc_settings(db, candidate)
            record_action(
                db, actor=actor, action="set_mrc_settings",
                detail=(
                    f"enabled={saved.enabled} hub={saved.host}:{saved.port} tls={saved.tls} site={saved.site_name!r} "
                    f"open_rooms={open_saved.enabled} cap={open_saved.cap} retention={open_saved.retention_days}d "
                    f"send_caller_ip={saved.send_caller_ip} send_caller_meta={saved.send_caller_meta}"
                ),
            )
            return saved

        saved = await lane.run(_persist)
        if mrc_bridge is not None:
            await mrc_bridge.reload_settings()
        return saved

    saved = await edit_resource_draft(
        session, lane,
        title="Inter-BBS chat (MRC)", own=True,
        fields=fields, draft=draft, save=save, error_type=MrcSettingsError,
        save_menu_text=menu_key("S", "ave & apply"),
        back_menu_text=menu_key("B", "ack"),
        preamble=_preamble,
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=unicode_style,
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    if saved is None:
        return
    unreachable = await lane.run(_mrc_unreachable_note) if saved.enabled else None
    if mrc_bridge is None:
        _announce_line(session, "Saved. Applies the next time the node runs.")
    else:
        status = mrc_bridge.status()
        _announce_styled(
            session,
            status_mark("success") + "Saved and applied. Link now: "
            + _mrc_state_line(status, unicode_style=unicode_style),
        )
        if status.last_error:
            _announce_line(session, colored(f"Last error: {sanitize_text(status.last_error)}", fg_color=MUTED_COLOR))
    if unreachable is not None:
        _announce_line(session, colored(unreachable, fg_color=WARNING_COLOR))


def _mrc_unreachable_note(db: Database) -> str | None:
    """MRC switched on bridges nothing by itself (design doc section 16,
    Decision 2): with open rooms off and no channel mapped, the hub link
    comes up and no caller can see any of it. Say so at the one moment
    the SysOp is looking, and name both ways out."""
    if load_open_room_settings(db).enabled or any(mapping.active for mapping in list_mrc_mappings(db)):
        return None
    return (
        "No caller can reach MRC yet: turn on \"Callers may open any room\" here so Chat shows the network's rooms, "
        "or bridge a channel to a room from that channel's own screen."
    )


def _toggle_draft_field(key: str) -> Callable[[Session, DatabaseLane, dict], Awaitable[None]]:
    """A toggle toggles (design doc §3.5): flip the draft value in place,
    no yes/no prompt -- `[S]ave & apply` is the one boundary that matters."""

    async def _toggle(session: Session, lane: DatabaseLane, draft: dict) -> None:
        draft[key] = not bool(draft.get(key))

    return _toggle


async def _blocklist_field(session: Session, lane: DatabaseLane, draft: dict) -> None:
    """The open-room blocklist as its own small list screen (issue
    #300): the current entries, `[A]dd` (one prompt), `[R]emove` (pick
    by number), `[B]ack`. Edits the draft only; the settings screen's
    own `[S]ave & apply` persists it."""
    while True:
        entries: list[str] = list(draft["open_blocklist"])
        await session.write_line("")
        if entries:
            await session.write_line("Blocked MRC rooms (callers cannot open these here):")
            for index, room in enumerate(entries, start=1):
                await session.write_line(f"  {index}. #{sanitize_text(room)}")
        else:
            await session.write_line(colored("No MRC rooms are blocked.", fg_color=MUTED_COLOR))
        actions = [menu_key("A", "dd")]
        if entries:
            actions.append(menu_key("R", "emove"))
        actions.append(menu_key("B", "ack"))
        await session.write_line("    ".join(actions))
        choice = (await session.read_key()).lower()
        if choice == "b":
            await session.write_line("")
            return
        if choice == "a":
            await session.write_line("")
            await write_prompt(session, "Room to block (blank = cancel): ")
            raw = (await session.read_line()).strip()
            if not raw:
                continue
            error = room_name_error(raw)
            if error is not None:
                await session.write_line(colored(error, fg_color=ERROR_COLOR))
                continue
            room = sanitize_room(raw)
            if room.lower() not in {entry.lower() for entry in entries}:
                entries.append(room)
            draft["open_blocklist"] = entries
            continue
        if choice == "r" and entries:
            await session.write_line("")
            # The entries themselves are the choices (design doc §3.5):
            # a picker, not a second prompt asking for a number.
            picked = await pick_item(
                session, list(enumerate(entries, start=1)),
                name_of=lambda item: f"#{item[1]}",
                stable_id_of=lambda item: item[0],
                title="Unblock an MRC room",
                empty_message="No MRC rooms are blocked.",
            )
            if picked is not None:
                entries.remove(picked[1])
                draft["open_blocklist"] = entries
            continue
        if choice == HELP_KEY:
            help_entries = [MenuEntry(label=menu_key("A", "dd"), brief="Block one more room by name")]
            if entries:
                help_entries.append(MenuEntry(label=menu_key("R", "emove"), brief="Pick a room to unblock"))
            await show_menu_help(
                session, "Blocked rooms help", help_entries,
                about="MRC rooms callers may not open from this node. Changes are kept only when you "
                      "save the MRC settings.",
            )
            continue
        await session.write(reject_unhandled_key(choice))


async def _mrc_status_sections(
    lane: DatabaseLane, mrc_bridge: MrcBridge, status: MrcStatus, *, unicode_style: bool
) -> list[Section]:
    """The live bridge as grouped facts: the hub connection, what the network
    looks like from here, caller-opened rooms, and one table row per bridged
    channel."""
    sections: list[Section] = []
    if not status.enabled:
        sections.append(Section("Connection", [
            Field("State", status_badge("MRC IS OFF", tone="neutral", unicode_style=unicode_style), styled=True),
            Note("Enable it under Settings > Inter-BBS chat (MRC), then bridge channels from each channel's screen."),
        ]))
    else:
        transport = "TLS" if status.tls else "plain"
        connection: list[Field | Note] = [
            Field("State", _mrc_state_line(status, unicode_style=unicode_style), styled=True),
            Field("Hub", f"{status.host}:{status.port} ({transport})"),
            Field("Site name", status.site_name),
        ]
        if status.connected_since is not None:
            display_format, display_timezone = await lane.run(resolve_display_preferences)
            connection.append(Field(
                "Connected since",
                format_for_display(
                    status.connected_since, override_format=display_format, override_timezone=display_timezone
                ),
                color=DATE_COLOR,
            ))
        if status.last_error:
            connection.append(Field("Last error", status.last_error, color=WARNING_COLOR))
        sections.append(Section("Connection", connection))

        dropped = status.dropped_outbound or status.dropped_inbound
        sections.append(Section("Traffic", [
            Field("Connection attempts", str(status.attempts)),
            Field("Callers announced", str(status.participants)),
            Field(
                "Dropped lines", f"{status.dropped_outbound} outbound, {status.dropped_inbound} inbound",
                color=WARNING_COLOR if dropped else VALUE_COLOR,
            ),
        ]))

        if status.network_summary is not None:
            age = status.network_stats_age_seconds or 0.0
            activity = f", {status.network_activity_label}" if status.network_activity_label else ""
            network = Field(
                "Network size",
                f"{status.network_summary}, {status.network_rooms} rooms{activity} (as of {int(age // 60)} min ago)",
            )
        elif status.network_stats_raw:
            network = Field(
                "Network size", f"unknown -- the hub answered STATS with: {status.network_stats_raw}",
                color=MUTED_COLOR,
            )
        else:
            network = Field("Network size", "unknown yet (asked once a caller is announced)", color=MUTED_COLOR)
        if status.hub_latency_seconds is not None:
            latency_age = int(status.hub_latency_age_seconds or 0.0)
            latency = Field(
                "Hub round trip", f"{status.hub_latency_seconds * 1000:.0f} ms (as of {latency_age} s ago)"
            )
        else:
            latency = Field(
                "Hub round trip", "not measured yet (the hub answers the next keepalive)", color=MUTED_COLOR
            )
        sections.append(Section("Network", [network, latency]))

    if status.enabled or status.open_rooms or status.retired_rooms:
        # Shown whenever there is open-room state to report -- with MRC
        # switched off node-wide too, since the sweeper keeps retiring
        # rooms opened earlier and a SysOp should see that happening.
        open_settings = await lane.run(load_open_room_settings)
        days = f"{open_settings.retention_days} idle day{'s' if open_settings.retention_days != 1 else ''}"
        if status.open_rooms_enabled:
            rooms: list[Field | Note] = [
                Field("Open rooms", f"{status.open_rooms} of {open_settings.cap} open, retired after {days}"),
                Field("Heard of", f"{status.observed_rooms} room{'s' if status.observed_rooms != 1 else ''}"),
            ]
        else:
            rooms = [
                Field(
                    "Open rooms",
                    "MRC is off node-wide" if not status.enabled else "off (callers cannot open rooms)",
                    color=MUTED_COLOR,
                ),
                Field("Opened earlier", f"{status.open_rooms} still age out after {days}"),
            ]
        rooms.append(Field("Retired since start", str(status.retired_rooms)))
        sections.append(Section("Caller-opened rooms", rooms))

    mappings = await lane.run(list_mrc_mappings)
    if not mappings:
        sections.append(Section("Bridged channels", [Note("No channel is bridged to an MRC room yet.")]))
    else:
        rows = []
        for mapping in mappings:
            state = "paused" if mapping.paused else ("open room" if mapping.is_open_room else "bridged")
            roster = mrc_bridge.remote_roster(mapping.channel)
            who = ", ".join(display_roster_entry(name) for name in roster[:12]) + (", ..." if len(roster) > 12 else "")
            rows.append([
                (mapping.channel.name, ACCENT_COLOR),
                f"#{mapping.room}",
                (state, WARNING_COLOR if mapping.paused else SUCCESS_COLOR),
                str(len(roster)),
                (who, METADATA_COLOR),
            ])
        sections.append(Section("Bridged channels", [
            Table(("Channel", "MRC room", "State", "Users", "Who"), rows, flex=4, right_aligned=frozenset({3})),
        ]))
    return sections


@located("MRC status")
async def _mrc_status_screen(session: Session, lane: DatabaseLane, actor: User, node_controls: NodeControls) -> None:
    """Issue #275: the "what is the bridge doing right now" screen §16
    left open -- read-only apart from `[R]econnect now` (a settings
    reload, which drops and redials the hub) and a jump to Settings.

    Grouped and paged (`show_detail`): with a few bridged channels the
    flat form ran past a 24-row terminal, the hub's own state first off
    the top."""
    page = 0
    message = None
    while True:
        chrome = await _load_chrome(lane, actor)
        title = _detail_title(
            session, chrome, "Inter-BBS chat (MRC)", own=True,
            subtitle="This node's link to the Multi Relay Chat network, live.",
        )
        mrc_bridge = node_controls.mrc_bridge
        if mrc_bridge is None:
            await show_detail(
                session, title=title, actions=[_BACK_ACTION],
                sections=[Section(None, [
                    Field("State", status_badge("NOT AVAILABLE HERE", tone="neutral", unicode_style=chrome.unicode_style), styled=True),
                    Note("The MRC bridge lives inside the running node; this console runs outside the node and can't see it."),
                ])],
                redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
                help_title="MRC status help",
                help_about=(
                    "The MRC bridge runs inside the live node, so this console cannot show it. Log in as a "
                    "SysOp on the node to see it."
                ),
            )
            return
        status = mrc_bridge.status()
        actions = [("s", menu_key("S", "ettings")), _BACK_ACTION]
        if status.enabled:
            actions.insert(0, ("r", menu_key("R", "econnect now")))
        choice, page = await show_detail(
            session, title=title, actions=actions, page=page, message=message,
            sections=await _mrc_status_sections(lane, mrc_bridge, status, unicode_style=chrome.unicode_style),
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            help_title="MRC status help",
            help_about=(
                "What the inter-BBS chat bridge is doing right now: its connection to the hub and the "
                "channels it carries."
            ),
        )
        message = None
        if choice == "b":
            return
        if choice == "r":
            await lane.run(record_action, actor=actor, action="reconnect_mrc")
            await mrc_bridge.reload_settings()
            message = colored("Reconnecting...", fg_color=SUCCESS_COLOR)
        elif choice == "s":
            await _mrc_settings_screen(session, lane, actor, node_controls=node_controls)
