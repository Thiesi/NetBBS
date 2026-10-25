"""
Profile editing, identity/attestation, and session-history screens:
`[E]dit profile` (bio/signature/display prefs/SSH keys), `[I]dentity
details` (age/name attestation, verification), `[L]ast sessions`, and
the session-lifecycle presentations shown before and after the main menu.

Split out of `netbbs.net.login_flow` (that module's own maintenance
split -- see its module docstring), the last and second-largest of the
extracted screen groups. Most screens are reached from the main menu;
calls nothing else in `login_flow`.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from netbbs.rendering.layout import MenuEntry
from netbbs.rendering.menu import continue_prompt, highlight_hotkeys
from netbbs.attestation import (
    SELF_ATTESTED_NOT_SHARED,
    AttestationChanged,
    AttestationError,
    ProfileFieldError,
    UserAttestation,
    attest_identity,
    birthdate_problem,
    change_birthdate,
    change_display_name,
    compute_age,
    clear_own_profile_field,
    get_attestation,
    get_birthdate,
    get_display_name,
    get_location,
    is_birthdate_visible,
    is_display_name_visible,
    is_location_visible,
    is_verified_badge_visible,
    set_attestation_link_visible,
    revoke_attestation,
    set_birthdate,
    set_birthdate_visible,
    set_display_name,
    set_display_name_visible,
    set_location,
    set_location_visible,
    set_verified_badge_visible,
)
from netbbs.auth.renames import (
    UsernameChangeError,
    open_username_request,
    request_username_change,
    withdraw_username_request,
)
from netbbs.auth.users import (
    SYSOP_LEVEL, User, get_user_by_id, has_password, is_usable_sysop, list_ssh_keys, list_users,
)
from netbbs.boards.categories import get_category_by_id as get_board_category_by_id
from netbbs.chat.categories import get_category_by_id as get_channel_category_by_id
from netbbs.communities import Community, get_community
from netbbs.directory import (
    MAX_BIO_BYTES,
    MAX_BIO_LINES,
    BioError,
    get_bio,
    is_bio_visible,
    set_bio,
    set_bio_visible,
)
from netbbs.files.categories import get_category_by_id as get_file_area_category_by_id
from netbbs.guest import is_guest_account
from netbbs.link.boards import LinkContext
from netbbs.link.attestation_delivery import attestation_delivery_counts
from netbbs.link.remote_attestation import count_attestation_recipients
from netbbs.mail import list_mail_blocks, set_shares_read_receipts, shares_read_receipts
from netbbs.messaging_preferences import accepts_direct_messages, set_accepts_direct_messages
from netbbs.net.oneliner_flow import add_oneliner_prompt, oneliner_wall_screen
from netbbs.oneliners import Oneliner, list_oneliners, may_write_oneliners, oneliners_enabled, oneliners_plain
from netbbs.net.char_input import HELP_KEY, InputCancelled, reject_unhandled_key
from netbbs.net.help_overlay import show_menu_help
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled, set_breadcrumb_collapsed_enabled
from netbbs.net.color_depth_preference import (
    apply_color_depth,
    color_depth_override,
    effective_truecolor,
    set_color_depth_override,
)
from netbbs.net.composition import edit_line_body
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.draft_storage import drafts_directory
from netbbs.net.editor_preference import fullscreen_editor_enabled, set_fullscreen_editor_enabled
from netbbs.net.mail_flow import mail_blocked_notice, mail_open_to, mail_someone
from netbbs.net.menu_description_preference import menu_description_level, set_menu_description_level
from netbbs.net.notices import announce, announce_line, pending_notice_rows, take_notices, write_notices
from netbbs.net.node_theme import (
    effective_accent_color,
    effective_accent_color_256,
    effective_header_color,
    effective_header_color_256,
)
from netbbs.net.mail_flow import blocked_senders_screen
from netbbs.net.location import crumbs, located
from netbbs.net.picker import pick_item
from netbbs.net.prose_editor import EditorHeader, edit_prose
from netbbs.net.animation_preference import animations_enabled, set_animations_enabled
from netbbs.net.redraw_preference import redraw_in_place_enabled, set_redraw_in_place_enabled
from netbbs.net.resource_editor import (
    DetailAction,
    DetailMode,
    DetailState,
    Draft,
    FieldSpec,
    SaveDeclined,
    edit_resource_draft,
    inline_field,
    live_choice_field,
    read_field_line,
    write_field_prompt,
)
from netbbs.net.session import Session, write_prompt
from netbbs.net.shared_account import shared_account_refusal, signed_in_without_credential
from netbbs.net.staff_edit import StaffEditRefused, as_staff, refusable, write_member
from netbbs.digits import is_ascii_number
from netbbs.net.sort_ui import SORT_MODE_LABELS
from netbbs.net.password_screen import manage_password_screen
from netbbs.net.ssh_key_screen import manage_ssh_keys_screen
from netbbs.net.mrc_color_preference import mrc_colors_enabled, set_mrc_colors_enabled
from netbbs.net.post_color_preference import post_colors_enabled, set_post_colors_enabled
from netbbs.net.mrc_name_preference import MRC_NAME_STYLES, mrc_name_style, set_mrc_name_style
from netbbs.chat.bell import BELL_LEVEL_LABELS, BELL_LEVELS, bell_level, set_bell_level
from netbbs.net.mrc_nick_color_preference import mrc_nick_color, set_mrc_nick_color
from netbbs.net.mrc_lastseen_preference import mrc_lastseen_recorded, set_mrc_lastseen_recorded
from netbbs.net.mrc_private_preference import mrc_private_messages_enabled, set_mrc_private_messages_enabled
from netbbs.rendering.pipe_codes import CGA_COLOR_NAMES
from netbbs.rendering.reflow import wrap_terminal_text
from netbbs.net.unicode_style_preference import (
    CHARSET_PREFERENCES,
    QUADRANT_PREFERENCES,
    apply_charset_preference,
    apply_quadrant_preference,
    charset_preference,
    quadrant_preference,
    set_charset_preference,
    set_quadrant_preference,
    unicode_style_enabled,
)
from netbbs.permissions import meets_level
from netbbs.rendering import (
    ERROR_COLOR,
    HEADER_COLOR,
    LABEL_COLOR,
    METADATA_COLOR,
    MUTED_COLOR,
    SUCCESS_COLOR,
    VALUE_COLOR,
    action_bar,
    colored,
    colored_truncate,
    cut_to_width,
    display_width,
    gradient_color,
    gradient_text,
    menu_key,
    reflow,
    sanitize_text,
    screen_title,
    visible_width,
)
from netbbs.doors.events import game_results_announced, set_game_results_announced
from netbbs.session_history import (
    SessionHistoryEntry,
    list_recent_sessions,
    previous_callers_enabled,
    previous_callers_plain,
    session_history_name_visible,
    set_session_history_name_visible,
)
from netbbs.signature import MAX_SIGNATURE_BYTES, MAX_SIGNATURE_LINES, SignatureError, get_signature, set_signature
from netbbs.sort_preferences import SortPreference, clear_sort_preference, list_sort_preferences
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display


# How many of your own calls [H]istory shows -- generous enough to be
# useful, and one screen page at the 80 columns a row fits on. A
# narrow terminal breaks each row in two and the listing scrolls,
# which is the ordinary behaviour of every plain listing here: there
# is no per-entry detail beyond the row itself, so there is nothing a
# pick_item selection would do, and nothing gained by hiding calls the
# caller asked for behind a pager.
_SESSION_HISTORY_DISPLAY_LIMIT = 20
_PREVIOUS_CALLERS_DISPLAY_LIMIT = 10
_PREVIOUS_CALLERS_FIXED_ROWS = 8
# Two rows more than the splash: reached from the main menu, the panel
# sits under a breadcrumbed `screen_title` the splash does not draw.
_PREVIOUS_CALLERS_MENU_FIXED_ROWS = 10
_PREVIOUS_CALLERS_GRADIENT = [
    (0, 245, 255),
    (120, 80, 255),
    (255, 70, 210),
    (255, 205, 70),
    (0, 245, 255),
]
_LOGOFF_SUMMARY_GRADIENT = [
    (0, 255, 210),
    (0, 170, 255),
    (125, 90, 255),
    (255, 75, 190),
    (255, 190, 70),
]


# What a row shows for a caller whose name is hidden -- and so, being
# compared against, what keeps that row from being mailed (issue #821).
_NAME_HIDDEN = "(name hidden)"


#: Issue #1156: what the Profile shows for each MRC name style.
_MRC_NAME_STYLE_LABELS = {
    "combined": "combined: <+Nick+@BBS (TAG)>",
    "both": "both: <Nick@BBS> +Nick+[TAG]",
    "label": "label only: <Nick@BBS>",
}

#: How a self-reported field on Name & details is edited. The same words
#: as `admin_fields._CLEAR_HINT` (issue #557) for a value that can be
#: cleared; kept here because the caller's screens do not import the
#: console's.
_FIELD_HINT = "Enter saves, blank clears, Esc keeps"


def _session_history_display_name(
    db: Database, entry: SessionHistoryEntry, *, viewer_is_sysop: bool
) -> str:
    """The denormalized `username_label` survives account deletion (see
    the migration's own docstring), but showing it is not automatic. A
    SysOp always sees the real name unconditionally (mirrors `netbbs.
    net.admin_flow`'s existing SysOp-sees-everything convention) --
    administrative visibility is the deliberately chosen policy here,
    same as it already was before issue #111.

    For an ordinary caller: while the account still exists, its *current*
    `session_history_name_visible` preference is re-checked live, not
    frozen at connect time -- issue #100's own choice, preserved
    unchanged, so a later opt-out/opt-in takes effect retroactively for
    every one of that account's existing rows. Once the account is
    deleted, there is no longer a live preference to re-check at all --
    falling back to unconditionally showing `username_label` in that case
    (the pre-#111 behavior) silently reversed a user's own prior opt-out
    the moment their account was deleted. `entry.name_visible_fallback`
    (kept in sync with the live preference for as long as the account
    exists -- see `set_session_history_name_visible`'s own docstring) is
    the fallback issue #111 adds specifically for this case: whatever the
    account's preference genuinely was immediately before deletion is
    what a now-deleted account's history keeps showing, permanently,
    since there is no "current" value left to ask."""
    if viewer_is_sysop:
        return entry.username_label
    if entry.user_id is None:
        return entry.username_label if entry.name_visible_fallback else _NAME_HIDDEN
    target = get_user_by_id(db, entry.user_id)
    if target is None or session_history_name_visible(db, target):
        return entry.username_label
    return _NAME_HIDDEN


def _previous_caller_entries(
    db: Database, *, limit: int, exclude_history_id: int | None
) -> list[SessionHistoryEntry]:
    """The node-wide roll, newest first, minus one session by id.

    Over-fetches by one so that excluding the viewer's own in-progress
    session still fills the panel.
    """
    return [
        entry
        for entry in list_recent_sessions(db, limit=limit + 1)
        if entry.id != exclude_history_id
    ][:limit]


def _render_previous_callers_panel(
    session: Session,
    db: Database,
    user: User,
    entries: list[SessionHistoryEntry],
    *,
    frame_width: int,
    oneliners: Sequence[Oneliner] = (),
) -> list[str]:
    """The framed caller roll, as rendered rows, drawn identically for
    both places it appears (issue #592): the post-login splash and the
    main menu's own `[R]ecent callers` entry. One renderer, so the
    name-visibility policy and the column measurement below can never
    drift between the two.

    Issue #1391: `oneliners`, the newest lines on the wall, are a lower
    section of the same frame under a "Oneliners" divider, so the call
    has one pause. With no caller rows the frame is headed by the wall
    alone. Each line is one row, cut to the frame; the full wall wraps.
    """
    # Sharing the frame, the wall follows the roll's style; drawn alone it
    # has its own (issue #1391).
    plain = previous_callers_plain(db) if entries else oneliners_plain(db)
    # The plain style (issue #841) keeps to the node's header colour, so
    # the truecolor gradient is never used for its frame or heading.
    use_truecolor = effective_truecolor(session, db, user) and not plain
    unicode_style = unicode_style_enabled(db, user)
    viewer_is_sysop = meets_level(user, SYSOP_LEVEL)
    header_color = effective_header_color(session, db)
    accent_color = effective_accent_color(session, db)
    body_width = frame_width - 4
    left, right = ("║", "║") if unicode_style else ("|", "|")
    horizontal = "═" if unicode_style else "="
    top_left, top_right = ("╔", "╗") if unicode_style else ("+", "+")
    middle_left, middle_right = ("╠", "╣") if unicode_style else ("+", "+")
    bottom_left, bottom_right = ("╚", "╝") if unicode_style else ("+", "+")
    marker = "◆" if unicode_style else "*"
    dot = " • " if unicode_style else "  /  "

    def _gradient(text: str, *, bold: bool = False) -> str:
        return gradient_text(
            text,
            _PREVIOUS_CALLERS_GRADIENT,
            bold=bold,
            truecolor=use_truecolor,
        )

    def _rule(left_char: str, right_char: str) -> str:
        plain = left_char + horizontal * (frame_width - 2) + right_char
        return _gradient(plain, bold=True) if use_truecolor else colored(
            plain, fg_color=header_color, bold=True
        )

    def _centered(text: str, *, gradient: bool = False, bold: bool = False) -> str:
        text = cut_to_width(text, body_width)
        spare = body_width - display_width(text)
        left_pad = spare // 2
        right_pad = spare - left_pad
        rendered = _gradient(text, bold=bold) if gradient else colored(
            text, fg_color=METADATA_COLOR, bold=bold
        )
        return " " * left_pad + rendered + " " * right_pad

    # The side bars continue the rules' corners -- the gradient's first
    # stop on the left, its last on the right -- rather than taking each
    # row's content color.
    # The rules are solid `header_color` on a 256-color session.
    left_bar_color, right_bar_color = (
        (
            gradient_color(_PREVIOUS_CALLERS_GRADIENT, 0.0),
            gradient_color(_PREVIOUS_CALLERS_GRADIENT, 1.0),
        )
        if use_truecolor
        else (header_color, header_color)
    )

    def _framed(content: str) -> str:
        padding = max(0, body_width - visible_width(content))
        return (
            colored(f"{left} ", fg_color=left_bar_color, bold=True)
            + content
            + " " * padding
            + colored(f" {right}", fg_color=right_bar_color, bold=True)
        )

    if not entries:
        if plain:
            title = "Oneliners"
            subtitle = "What callers left on the wall"
        else:
            title = f"{marker}  O N E L I N E R S  {marker}"
            subtitle = "WHAT THE LAST CALLERS LEFT BEHIND"
    elif plain:
        title = "Recent callers"
        subtitle = "Who has called in lately"
    else:
        title = f"{marker}  R E C E N T   C A L L E R S  {marker}"
        subtitle = "SIGNALS RECENTLY RECEIVED BY THIS NODE"
    rendered: list[str] = [
        _rule(top_left, top_right),
        _framed(_centered(title, gradient=not plain, bold=True)),
        _framed(_centered(subtitle, gradient=use_truecolor)),
        _rule(middle_left, middle_right),
    ]

    # Issue #535: every column after the name used to start wherever
    # that row's name happened to end, so nothing lined up down the
    # panel. The widths are computed once, from the entries actually on
    # screen -- the same per-page (not per-list) measurement
    # `netbbs.net.picker` uses for its own reference column, since
    # alignment only has to hold within one panel.
    #
    # The sibling screen in this file already did this right
    # (`_show_logoff_summary_screen` pads with `f" {label:<12}"`); this
    # one was simply overlooked.
    def _fit(text: str, width: int) -> tuple[str, str]:
        """`(visible, padding)` for one cell, fitted to `width` display
        columns.

        Returned as two pieces rather than one padded string (Codex
        review) because `name_color` is a *callable* on a truecolor
        session -- it hands its whole segment to `gradient_text`. Pad
        before styling and the gradient spreads itself across the
        invisible trailing spaces, so a short name like "bob" gets three
        characters' worth of a five-stop gradient and loses the effect
        entirely. The padding is emitted as its own unstyled segment.

        Tabs are normalized first: `sanitize_text` preserves them,
        `display_width` scores them zero, and the terminal expands them
        -- so a timestamp format containing one (`%B\t%Y`) would be
        measured short and take its column with it. AGENTS.md: "Width
        measurement must normalize tabs."
        """
        text = cut_to_width(text.replace("\t", " "), width)
        return text, " " * max(0, width - display_width(text))

    def _name_of(entry) -> str:
        return sanitize_text(
            _session_history_display_name(db, entry, viewer_is_sysop=viewer_is_sysop)
        )

    def _connected_of(entry) -> str:
        return sanitize_text(format_for_display(entry.connected_at, db)).replace("\t", " ")

    # The privacy placeholder is not a username and must never be cut
    # into something that merely looks like one (Codex review): at the
    # 40-column minimum the name column is narrower than
    # "(name hidden)", and truncating it produced "(name hidde" --
    # malformed, and ambiguous about whether the name is hidden or just
    # long. A deliberately shorter label is substituted instead, so the
    # row still says plainly what it is.
    _HIDDEN = _NAME_HIDDEN
    _HIDDEN_SHORT = "(hidden)"

    # The timestamp is not a fixed width: a 12-hour display format
    # yields both "1:05 PM" and "11:05 AM", so it is measured rather
    # than assumed.
    connected_width = max(
        (display_width(_connected_of(entry)) for entry in entries), default=0
    )
    # "SIGNED OFF" / "SIGNAL LOST" / "ONLINE NOW" are all ten columns
    # already, but deriving it keeps the arithmetic honest if a fourth
    # state is ever added.
    status_width = max(len(s) for s in ("SIGNED OFF", "SIGNAL LOST", "ONLINE NOW"))
    show_status = frame_width >= 62
    fixed = (
        display_width(f" {1:02d} ")
        + display_width(marker)
        + 1
        + display_width(dot)
        + connected_width
        + ((display_width(dot) + status_width) if show_status else 0)
    )
    # A name still has to be a name; below this the panel would be all
    # chrome and no caller, so the name keeps a floor and the row is
    # allowed to run into `colored_truncate` as it always did. The floor
    # is the short privacy label's own width, so that label is never
    # itself truncated.
    name_width = max(len(_HIDDEN_SHORT), body_width - fixed)

    for index, entry in enumerate(entries, start=1):
        name = _name_of(entry)
        if name == _HIDDEN and name_width < display_width(_HIDDEN):
            name = _HIDDEN_SHORT
        connected = _connected_of(entry)
        if entry.disconnected_at is not None:
            status, status_color = "SIGNED OFF", METADATA_COLOR
        elif entry.interrupted_at is not None:
            status, status_color = "SIGNAL LOST", ERROR_COLOR
        else:
            status, status_color = "ONLINE NOW", SUCCESS_COLOR

        fraction = (index - 1) / max(1, len(entries) - 1)
        rail_color = (
            gradient_color(_PREVIOUS_CALLERS_GRADIENT, fraction)
            if use_truecolor
            else header_color
        )
        name_color = (
            (lambda text: _gradient(text, bold=True))
            if use_truecolor and name != _NAME_HIDDEN
            else MUTED_COLOR if name == _NAME_HIDDEN else accent_color
        )
        name_text, name_padding = _fit(name, name_width)
        connected_text, connected_padding = _fit(connected, connected_width)
        segments = [
            (f" {index:02d} ", METADATA_COLOR),
            (marker, rail_color),
            (" ", None),
            # Name and padding are separate segments so the truecolor
            # gradient spans the name only -- see `_fit`.
            (name_text, name_color),
            (name_padding, None),
            (dot, METADATA_COLOR),
            # Padded even in the narrow branch, where it is the last
            # column: a trailing run of spaces costs nothing inside a
            # frame that pads to `body_width` anyway, and leaving it
            # unpadded would mean the two branches disagreed about
            # whether a column is a column.
            (connected_text, METADATA_COLOR),
            (connected_padding, None),
        ]
        if show_status:
            segments.extend([
                (dot, METADATA_COLOR),
                (status, status_color),
            ])
        rendered.append(
            _framed(colored_truncate(segments, body_width))
        )

    if oneliners:
        if entries:
            # The divider names the section inside the rule itself, so the
            # wall costs one row of chrome rather than a heading block.
            label = " Oneliners " if plain else " O N E L I N E R S "
            inner = frame_width - 2
            if display_width(label) + 2 <= inner:
                spare = inner - display_width(label)
                divider = (
                    middle_left + horizontal * (spare // 2) + label
                    + horizontal * (spare - spare // 2) + middle_right
                )
                rendered.append(
                    _gradient(divider, bold=True) if use_truecolor
                    else colored(divider, fg_color=header_color, bold=True)
                )
            else:
                rendered.append(_rule(middle_left, middle_right))
        # The author column is as wide as the widest name shown, up to
        # twelve columns, so the lines start under one another.
        author_width = min(
            12, max(display_width(sanitize_text(line.author_label)) for line in oneliners)
        )
        for line in oneliners:
            author_text, author_padding = _fit(sanitize_text(line.author_label), author_width)
            rendered.append(_framed(colored_truncate(
                [
                    (author_text, (lambda text: _gradient(text, bold=True)) if use_truecolor else accent_color),
                    (author_padding, None),
                    ("  ", None),
                    (sanitize_text(line.text), VALUE_COLOR),
                ],
                body_width,
            )))

    rendered.append(_rule(bottom_left, bottom_right))
    return rendered


def _split_panel_rows(available: int, callers: int, lines: int) -> tuple[int, int]:
    """How many caller rows and wall lines fit `available` content rows
    of the combined panel (issue #1391).

    Each section shows at most `_PREVIOUS_CALLERS_DISPLAY_LIMIT` rows.
    With both present the divider takes a row, and each keeps at least
    one; where even that does not fit, the roll keeps its place and the
    wall is left to its own screen. Otherwise the larger section gives
    way first, the wall on a tie.
    """
    callers = min(callers, _PREVIOUS_CALLERS_DISPLAY_LIMIT)
    lines = min(lines, _PREVIOUS_CALLERS_DISPLAY_LIMIT)
    available = max(0, available)
    if callers and lines:
        room = available - 1
        if room >= 2:
            while callers + lines > room:
                if lines >= callers:
                    lines -= 1
                else:
                    callers -= 1
            return callers, lines
        lines = 0
    return min(callers, available), min(lines, available)


async def _show_previous_callers_screen(
    session: Session,
    db: Database,
    user: User,
    *,
    current_history_id: int,
) -> bool:
    """Show the adaptive post-login caller roll and wait for dismissal.

    The current session has already been recorded so it can be finalized
    reliably on every exit path.  It is excluded by id here, leaving only
    connections which genuinely preceded this one.  An empty history is a
    complete non-event so the first caller on a new node is not paused at an
    empty screen.

    The node setting gates this automatic splash only, which is exactly
    what the SysOp toggle offers ("shown after login" / "hidden after
    login").  The same roll stays reachable on demand from the main
    menu, as the node-wide listing has always been -- before issue #592
    it was what `[H]istory` showed.

    Issue #1391: the oneliner wall is the same frame's lower section, so
    the call still has one pause, and each section follows its own
    toggle and is left out when empty. Where the wall is on and the
    caller meets its level, the pause offers `[A]dd a line` beside
    `[Enter] Continue` -- a hotkey, never a question (design doc §3.5);
    any other key goes on, and the outcome is carried to the main menu.
    """
    show_callers = previous_callers_enabled(db)
    show_wall = oneliners_enabled(db)
    if not (show_callers or show_wall):
        return False

    available = session.terminal_height - _PREVIOUS_CALLERS_FIXED_ROWS
    frame_width = min(session.terminal_width, 78)
    if available <= 0 or frame_width < 4:
        return False

    entries = _previous_caller_entries(
        db,
        limit=_PREVIOUS_CALLERS_DISPLAY_LIMIT,
        exclude_history_id=current_history_id,
    ) if show_callers else []
    lines = list_oneliners(db, limit=_PREVIOUS_CALLERS_DISPLAY_LIMIT) if show_wall else []
    caller_rows, line_rows = _split_panel_rows(available, len(entries), len(lines))
    if not (caller_rows or line_rows):
        return False

    rendered = _render_previous_callers_panel(
        session, db, user, entries[:caller_rows], frame_width=frame_width,
        oneliners=lines[:line_rows],
    )
    await session.write_line("\r\n" + "\r\n".join(rendered))
    offer_add = show_wall and may_write_oneliners(db, user)
    await session.write_line(
        "\r\n" + (
            menu_key("A", "dd a line") + "   " + continue_prompt() if offer_add else continue_prompt()
        )
    )
    key = await session.read_any_key()
    if offer_add and key.lower() == "a":
        await add_oneliner_prompt(session, db, user)
    return True


def _previous_caller_mail_target(
    db: Database, entry: SessionHistoryEntry, viewer: User, *, viewer_is_sysop: bool
) -> User | str:
    """The account to write to from one row of the roll (issue #821), or
    why there is none, in words for the caller.

    A row whose name the roll hides is not mailable: the compose screen
    would show the name in To. That is the same decision
    `_session_history_display_name` makes for the row itself, so the two
    cannot disagree -- a SysOp, who sees every name, may write to any."""
    name = _session_history_display_name(db, entry, viewer_is_sysop=viewer_is_sysop)
    if name == _NAME_HIDDEN:
        return "That caller keeps their name private, so they can't be written to from here."
    target = get_user_by_id(db, entry.user_id) if entry.user_id is not None else None
    if target is None:
        return f"{name}'s account no longer exists."
    if target.id == viewer.id:
        return "That call was yours."
    # Someone who has blocked the viewer is refused on this row, in Who's
    # online's words (issue #953). The key stays: the roll's other callers
    # can still be written to.
    blocked = mail_blocked_notice(db, target, sender=viewer)
    if blocked is not None:
        return blocked
    return target


@located("Recent callers")
async def _previous_callers_screen(
    session: Session,
    db: Database,
    user: User,
    *,
    current_history_id: int | None = None,
    lane: DatabaseLane | None = None,
    link_context: LinkContext | None = None,
) -> None:
    """The same caller roll, reached deliberately from the main menu
    (issue #592).

    Everything that differs from the post-login splash follows from this
    one being asked for rather than offered. It ignores the node
    setting, which only says whether the roll interrupts a login. It
    keeps at least one row on a terminal too short for the splash's own
    budget. And where the splash may silently skip itself -- nobody
    asked for it -- every path here ends in something drawn: an empty
    roll and a terminal too narrow to frame one each say so, because a
    hotkey that draws nothing reads as a broken key (design doc
    section 3.5).

    `current_history_id` is still excluded where the caller knows it:
    the viewer's own live session is already on `[W]ho's online`, and
    listing it here as `ONLINE NOW` would spend one of ten scarce rows
    telling the viewer they are connected.

    Issue #821: `[M]ail a caller` asks for a row's number and opens the
    compose screen addressed to that caller (`netbbs.net.mail_flow.
    mail_someone`), then comes back here with the outcome above the
    prompt. Offered only with a `lane` and while mail is open to the
    viewer; otherwise the roll is dismissed with any key, as before. A
    row whose name is hidden, your own call, a deleted account, and a
    caller who has blocked you (issue #953: "<name> does not accept
    messages or mail from you.") are refused with the reason. The key is
    not hidden for a caller who blocked you, as Who's online's `[E]-mail`
    is: it is the roll's, not one caller's.

    Issue #1391: the oneliner wall is the frame's lower section here as
    at logon, whatever the after-login toggle says, and the bar adds
    `[A]dd a line` (for a caller who meets the wall's level) and
    `[O]neliners`, the whole wall. So the bar is always drawn, and the
    screen is left with `[B]ack`.
    """
    viewer_is_sysop = meets_level(user, SYSOP_LEVEL)
    while True:
        offer_mail = lane is not None and await mail_open_to(session, lane, user)
        offer_add = may_write_oneliners(db, user)
        frame_width = min(session.terminal_width, 78)

        await session.write_line(
            "\r\n" + screen_title(
                "Recent callers",
                breadcrumb=crumbs(session, own=True),
                width=session.terminal_width,
                clear=redraw_in_place_enabled(db, user),
                unicode_style=unicode_style_enabled(db, user),
                collapsed=breadcrumb_collapsed_enabled(db, user),
                header_color=effective_header_color(session, db),
                node_name_gradient=session.node_name_gradient,
            )
        )
        all_entries = _previous_caller_entries(
            db,
            limit=_PREVIOUS_CALLERS_DISPLAY_LIMIT,
            exclude_history_id=current_history_id,
        )
        offer_mail = offer_mail and bool(all_entries)
        # One packed bar, whatever the description level: the roll is what
        # this screen is for, and on a short terminal every row the bar
        # took would be a caller fewer.
        bar = action_bar(
            [
                *([menu_key("M", "ail a caller")] if offer_mail else []),
                *([menu_key("A", "dd a line")] if offer_add else []),
                menu_key("O", "neliners"),
                menu_key("B", "ack"),
            ],
            width=session.terminal_width, height=session.terminal_height,
        )
        # The budget counts the bar's rows beyond its first, and the
        # outcome carried above the prompt.
        extra_rows = pending_notice_rows(session)
        extra_rows += wrap_terminal_text(bar, max(1, session.terminal_width)).count("\r\n")
        available = max(1, session.terminal_height - _PREVIOUS_CALLERS_MENU_FIXED_ROWS - extra_rows)
        lines = list_oneliners(db, limit=_PREVIOUS_CALLERS_DISPLAY_LIMIT)
        caller_rows, line_rows = _split_panel_rows(available, len(all_entries), len(lines))
        entries = all_entries[:caller_rows]
        if frame_width < 4:
            # The splash simply skips itself here; this screen was asked
            # for, so it says why nothing is drawn rather than looking
            # broken. A frame needs two corners and something between them.
            await session.write_line(
                colored("Your terminal is too narrow for this list.", fg_color=MUTED_COLOR)
            )
            await write_notices(session)
            await session.write_line(
                "\r\n" + continue_prompt()
            )
            await session.read_any_key()
            return
        if not entries and not line_rows:
            await session.write_line(
                colored("Nobody else has called this node yet.", fg_color=MUTED_COLOR)
            )
        else:
            rendered = _render_previous_callers_panel(
                session, db, user, entries, frame_width=frame_width,
                oneliners=lines[:line_rows],
            )
            await session.write_line("\r\n".join(rendered))

        await session.write_line("\r\n" + bar)
        await write_notices(session)
        await write_prompt(session, "Choice: ")
        while True:
            action = (await session.read_key()).lower()
            if action == HELP_KEY:
                help_entries = []
                if offer_mail:
                    help_entries.append(MenuEntry(
                        label=menu_key("M", "ail a caller"),
                        brief="Write to someone on the list; asks for their number",
                    ))
                if offer_add:
                    help_entries.append(MenuEntry(
                        label=menu_key("A", "dd a line"),
                        brief="Leave one line on the oneliner wall",
                    ))
                help_entries.append(MenuEntry(
                    label=menu_key("O", "neliners"),
                    brief="The whole wall, to page, find or delete",
                ))
                await show_menu_help(
                    session, "Recent callers help", help_entries,
                    about=(
                        "The most recent calls to this BBS, newest first, and the newest lines "
                        "callers left on the oneliner wall. A caller who hides their name in Profile "
                        "shows as (name hidden)."
                    ),
                    header_color=effective_header_color(session, db),
                    unicode_style=unicode_style_enabled(db, user),
                )
                break  # drawn again
            if action == "b" or (action == "m" and offer_mail) or (action == "a" and offer_add) or action == "o":
                await session.write_line("")
                break
            await session.write(reject_unhandled_key(action))
        if action == HELP_KEY:
            continue
        if action == "b":
            return
        if action == "a":
            await add_oneliner_prompt(session, db, user, lane=lane)
            continue
        if action == "o":
            await oneliner_wall_screen(session, db, user, lane=lane)
            continue

        # A hotkey followed by a line prompt: an Enter typed right behind
        # the "m" is not an answer (same guard as mail's compose).
        discard_buffered_enter = getattr(session, "discard_buffered_enter", None)
        if discard_buffered_enter is not None:
            await discard_buffered_enter()
        await write_prompt(session, f"Mail which caller (1-{len(entries)})? ")
        try:
            typed = (await session.read_line(cancellable=True)).strip()
        except InputCancelled:
            typed = ""
        if not typed:
            continue
        if not is_ascii_number(typed) or not 1 <= int(typed) <= len(entries):
            announce(session, f"There is no caller {typed} on this list.", tone="error")
            continue
        target = _previous_caller_mail_target(db, entries[int(typed) - 1], user, viewer_is_sysop=viewer_is_sysop)
        if isinstance(target, str):
            announce(session, target, tone="error")
            continue
        assert lane is not None  # offer_mail's own condition
        await mail_someone(session, lane, user, recipient=target, link_context=link_context)


def _format_call_duration(connected_at: str, disconnected_at: str) -> str:
    """Human-sized elapsed time derived from the two persisted instants."""
    storage_format = "%Y-%m-%dT%H:%M:%S.%fZ"
    connected = datetime.strptime(connected_at, storage_format)
    disconnected = datetime.strptime(disconnected_at, storage_format)
    total_seconds = max(0, int((disconnected - connected).total_seconds()))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


async def _show_logoff_summary_screen(
    session: Session,
    db: Database,
    user: User,
    entry: SessionHistoryEntry,
) -> None:
    """Render the completed call's persisted timing immediately before exit."""
    if entry.disconnected_at is None:
        return

    use_truecolor = effective_truecolor(session, db, user)
    unicode_style = unicode_style_enabled(db, user)
    terminal_width = max(1, session.terminal_width)
    connected = format_for_display(entry.connected_at, db)
    disconnected = format_for_display(entry.disconnected_at, db)
    duration = _format_call_duration(entry.connected_at, entry.disconnected_at)
    color_depth = "TRUECOLOR" if use_truecolor else "256 COLOR"
    size_separator = " × " if unicode_style else " x "
    field_separator = "  •  " if unicode_style else "  /  "
    terminal = (
        f"{session.terminal_width}{size_separator}{session.terminal_height}"
        f"{field_separator}{color_depth}"
    )
    facts = [
        ("CONNECTED", connected),
        ("SIGNED OFF", disconnected),
        ("TIME ONLINE", duration),
        ("TERMINAL", terminal),
    ]

    # Defensive fallback for synthetic tiny sessions. Real transports negotiate
    # enough columns for the framed composition, but even a test or unusual
    # adapter below that boundary must remain width-safe.
    if terminal_width < 20:
        lines = ["CALL COMPLETE", *(f"{label}: {value}" for label, value in facts)]
        await session.write_line(
            "\r\n" + "\r\n".join(cut_to_width(line, terminal_width) for line in lines)
        )
        return

    frame_width = min(76, terminal_width)
    body_width = frame_width - 4
    accent_color = (
        effective_accent_color(session, db)
        if use_truecolor
        else effective_accent_color_256(db)
    )
    left, right = ("║", "║") if unicode_style else ("|", "|")
    horizontal = "═" if unicode_style else "="
    top_left, top_right = ("╔", "╗") if unicode_style else ("+", "+")
    middle_left, middle_right = ("╠", "╣") if unicode_style else ("+", "+")
    bottom_left, bottom_right = ("╚", "╝") if unicode_style else ("+", "+")
    marker = "◆" if unicode_style else "*"

    def _gradient(text: str, *, bold: bool = False) -> str:
        return gradient_text(
            text, _LOGOFF_SUMMARY_GRADIENT, bold=bold, truecolor=use_truecolor
        )

    def _rule(left_char: str, right_char: str) -> str:
        return _gradient(
            left_char + horizontal * (frame_width - 2) + right_char,
            bold=True,
        )

    # The side bars continue the rules' corners -- the gradient's first
    # stop on the left, its last on the right -- rather than taking each
    # row's content color.
    left_bar_color = gradient_color(
        _LOGOFF_SUMMARY_GRADIENT, 0.0, truecolor=use_truecolor
    )
    right_bar_color = gradient_color(
        _LOGOFF_SUMMARY_GRADIENT, 1.0, truecolor=use_truecolor
    )

    def _framed(content: str) -> str:
        padding = max(0, body_width - visible_width(content))
        return (
            colored(f"{left} ", fg_color=left_bar_color, bold=True)
            + content
            + " " * padding
            + colored(f" {right}", fg_color=right_bar_color, bold=True)
        )

    def _centered(text: str, *, gradient: bool = False) -> str:
        text = cut_to_width(text, body_width)
        spare = body_width - display_width(text)
        left_pad = spare // 2
        rendered = _gradient(text, bold=True) if gradient else colored(
            text, fg_color=METADATA_COLOR, bold=True
        )
        return " " * left_pad + rendered + " " * (spare - left_pad)

    rendered = [
        _rule(top_left, top_right),
        _framed(
            _centered(f"{marker}  C A L L   C O M P L E T E  {marker}", gradient=True),
        ),
        _framed(_centered("FINAL SESSION TELEMETRY")),
        _rule(middle_left, middle_right),
    ]
    for index, (label, value) in enumerate(facts):
        rail_color = gradient_color(
            _LOGOFF_SUMMARY_GRADIENT,
            index / max(1, len(facts) - 1),
            truecolor=use_truecolor,
        )
        content = colored_truncate(
            [
                (f" {label:<12}", rail_color),
                (value, accent_color if label == "TIME ONLINE" else VALUE_COLOR),
            ],
            body_width,
        )
        rendered.append(_framed(content))
    rendered.append(_rule(bottom_left, bottom_right))
    await session.write_line("\r\n" + "\r\n".join(rendered))


async def _last_sessions_screen(session: Session, db: Database, user: User) -> None:
    """
    Issue #592: *your* calls, not the node's. The menu entry has always
    described itself as "Your recent sessions", but the listing behind
    it was the whole node's -- every caller's connect and disconnect
    times, under a heading claiming they were yours. The node-wide roll
    is a real screen and keeps existing; it is the previous-callers
    panel, now reachable on its own main-menu hotkey rather than only
    as a post-login splash. Splitting them lets each say one true
    thing, and leaves the name-visibility policy in the one place it
    was ever about: a listing other callers read.

    No privacy question survives the filter, so none is asked here. The
    rows are the viewer's own, so `session_history_name_visible` (which
    governs whether *other* callers see your name) has nothing to
    decide, and the name itself is dropped from the row -- it is the
    same name on all twenty of them. What the freed width buys is the
    call duration, which the node-wide roll has no room for.

    Distinct from `[W]ho's online` (issue #99), which only ever shows
    who is currently connected.

    Issue #110: `interrupted_at` (reconciled once at startup, before any
    listener could accept a new session -- see `netbbs.session_history.
    reconcile_interrupted_sessions`'s own docstring) means a row can be
    NULL/NULL and *not* still connected: this process crashed, was
    killed, or lost power before it ever reached `record_session_end`.
    Shown as its own distinct third state, never folded into "still
    connected" (which cannot possibly still be true across a restart) or
    silently written as if `interrupted_at` were the real disconnect
    moment (it's only ever "whenever this node next started up," which
    could be long after the connection actually dropped).

    Waits for a keystroke before returning (dogfood report): this used
    to fall straight through to the main menu's own redraw the instant
    the listing finished printing, which -- under redraw-in-place --
    cleared the terminal and wiped the listing before there was any
    chance to actually read it. Every other plain (non-`pick_item`)
    content screen in this codebase already pauses the same way
    (`netbbs.net.help_overlay.show_help`'s own "Press any key to
    continue..." convention).
    """
    entries = list_recent_sessions(
        db, limit=_SESSION_HISTORY_DISPLAY_LIMIT, user_id=user.id
    )
    await session.write_line(
        "\r\n" + screen_title(
            "Your sessions",
            breadcrumb=crumbs(session),
            width=session.terminal_width,
            clear=redraw_in_place_enabled(db, user),
            unicode_style=unicode_style_enabled(db, user),
            collapsed=breadcrumb_collapsed_enabled(db, user),
            header_color=effective_header_color(session, db),
        node_name_gradient=session.node_name_gradient)
    )
    if not entries:
        await session.write_line(
            colored("You have no recorded sessions yet.", fg_color=MUTED_COLOR)
        )
    else:
        accent = effective_accent_color(session, db)
        for entry in entries:
            connected = format_for_display(entry.connected_at, db)
            if entry.disconnected_at is not None:
                status = (
                    f"until {format_for_display(entry.disconnected_at, db)}"
                    f" ({_format_call_duration(entry.connected_at, entry.disconnected_at)})"
                )
                status_color = METADATA_COLOR
            elif entry.interrupted_at is not None:
                status = "connection lost -- session did not end cleanly"
                status_color = ERROR_COLOR
            else:
                status = "still connected"
                status_color = SUCCESS_COLOR
            # Nothing on this row is truncated (Codex review of issue
            # #592). A completed call at the supported 40-column
            # minimum runs past 60 columns, so `colored_truncate` --
            # which this screen used while the row was one line of
            # everyone's names -- would cut away the disconnect time
            # and the duration the screen exists to show. Every
            # segment here is the node's own text, no caller's, so the
            # composed row can go to `write_line` and be wrapped
            # rather than cut (AGENTS.md: retain all text).
            #
            # It is broken at the comma first, into a line per fact,
            # because `wrap_terminal_text` continues at column zero and
            # unindented continuations make twenty rows read as one
            # paragraph. Whatever still does not fit -- the interrupted
            # sentence, narrower than 40 -- `write_line` wraps.
            plain = f"  connected {connected}, {status}"
            if display_width(plain) <= session.terminal_width:
                await session.write_line(
                    "  "
                    + colored("connected ", fg_color=LABEL_COLOR)
                    + colored(connected, fg_color=accent)
                    + colored(", ", fg_color=LABEL_COLOR)
                    + colored(status, fg_color=status_color)
                )
            else:
                await session.write_line(
                    "  "
                    + colored("connected ", fg_color=LABEL_COLOR)
                    + colored(connected, fg_color=accent)
                )
                await session.write_line(
                    "    " + colored(status, fg_color=status_color)
                )
    await session.write_line("\r\n" + continue_prompt())
    await session.read_any_key()


_SORT_PREFERENCE_KIND_LABELS = {
    "channel": "Chat channels", "board": "Message boards", "file_area": "File areas",
}


def _sort_preference_scope_label(db: Database, pref: SortPreference) -> str:
    if pref.category_id is not None:
        if pref.resource_kind == "channel":
            name = get_channel_category_by_id(db, pref.category_id).name
        elif pref.resource_kind == "board":
            name = get_board_category_by_id(db, pref.category_id).name
        else:
            name = get_file_area_category_by_id(db, pref.category_id).name
        return f"Category: {name}"
    if pref.community_id is not None:
        community = get_community(db, pref.community_id)
        name = community.name if community is not None else f"Community #{pref.community_id}"
        return f"Community: {name}"
    return "Global default"


async def _sort_preferences_screen(
    session: Session, lane: DatabaseLane, user: User, *, actor: User | None = None
) -> None:
    """
    Review/clear your saved sort-mode overrides (design doc, dogfood
    feature request) -- the discoverability half of the `[O]rder`
    command's own design conversation: a 3-level cascade is invisible
    complexity right up until someone forgets they set an override
    months ago and can't tell why one list looks "wrong." `netbbs.
    sort_preferences.list_sort_preferences` deliberately returns raw
    `community_id`/`category_id`, leaving name resolution to the
    caller (that module's own docstring) -- this is the one place that
    resolution happens, since no other screen needs to enumerate a
    user's *entire* set of overrides across all three resource kinds
    and every scope at once.

    `pick_item`'s `name_of`/`description_of` must stay plain, synchronous,
    no-I/O lambdas (its own established contract, matched by every other
    caller in this codebase) -- so scope labels are resolved once via
    `lane.run` into a `pref.id`-keyed dict before entering the picker,
    not lazily per item inside the lambda the way the pre-lane version
    could when it held a bare `db` directly.

    `actor` clears a member's from their Profile (design doc §5.6), each
    clear recorded in the account's admin history.
    """
    viewer = actor or user
    none_saved = (
        "You have no saved sort preferences yet." if actor is None
        else f"{sanitize_text(user.username)} has no saved sort preferences."
    )
    while True:
        prefs = await lane.run(list_sort_preferences, user)
        if not prefs:
            await session.write_line(colored(f"\r\n{none_saved}", fg_color=MUTED_COLOR))
            await session.write_line(
                colored(
                    "Set one from any chat channel/message board/file-area picker's [O]rder command.",
                    fg_color=MUTED_COLOR,
                )
            )
            return

        labels: dict[int, str] = {}
        for pref in prefs:
            labels[pref.id] = await lane.run(_sort_preference_scope_label, pref)

        selected = await pick_item(
            session,
            prefs,
            name_of=lambda p: f"{_SORT_PREFERENCE_KIND_LABELS[p.resource_kind]} — {labels[p.id]}",
            stable_id_of=lambda p: p.id,
            description_of=lambda p: SORT_MODE_LABELS[p.sort_mode],
            title="Your sort preferences" if actor is None else f"{_possessive(user)} sort preferences",
            empty_message=none_saved,
            help_about=(
                "Each sort order you saved: the kind of list, where it applies, and the order. "
                "Picking one asks before clearing it; that list then falls back to the next wider order."
            ),
            redraw_in_place=await lane.run(redraw_in_place_enabled, viewer),
            unicode_style=await lane.run(unicode_style_enabled, viewer),
            collapsed=await lane.run(breadcrumb_collapsed_enabled, viewer),
            accent_color=await lane.run(effective_accent_color_256),
            header_color=await lane.run(effective_header_color_256),
        )
        if selected is None:
            return

        clear = await prompt_yes_no(
            session,
            f"Clear this override ({labels[selected.id]}, "
            f"{SORT_MODE_LABELS[selected.sort_mode]})?",
            default=False,
        )
        if clear and actor is not None:
            chosen = selected

            def write(db: Database, target: User) -> tuple[None, str]:
                clear_sort_preference(
                    db, target, chosen.resource_kind,
                    community_id=chosen.community_id, category_id=chosen.category_id,
                )
                return None, f"Sort preference cleared: {labels[chosen.id]}"

            await write_member(lane, actor, user, write)
            announce_line(session, "Cleared.")
        elif clear:
            await lane.run(
                clear_sort_preference,
                user, selected.resource_kind,
                community_id=selected.community_id, category_id=selected.category_id,
            )
            announce_line(
                session, "Cleared for this call." if signed_in_without_credential(session) else "Cleared."
            )


# -- a session that signed in without a credential (issue #1073) -----------
#
# Everything other callers see of the account -- bio, signature, name and
# details, whether it takes messages, who it blocks -- and what a node-wide
# service reads from it (the MRC bridge's per-handle settings) is refused
# (`netbbs.net.shared_account`). Each such entry stays on the screen, as the
# password and key entries do, and says why when pressed. Display settings
# stay open: a guest on a plain-ASCII or 16-colour terminal needs them, and
# what this session writes lasts for the call
# (`netbbs.user_preferences.session_scoped_preferences`, entered at sign-in).


def _refuse_shared_account(session: Session, what: str) -> None:
    """Carry the reason into the next screen drawn, which is the redraw of
    the one the caller pressed the key on."""
    announce(session, shared_account_refusal(what), tone="error")


def _unless_signed_in_without_credential(what: str, prompt):
    """`prompt`, refused with the reason for a session that signed in
    without a credential."""

    async def guarded(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        if signed_in_without_credential(session):
            _refuse_shared_account(session, what)
            return
        await prompt(session, lane, draft)

    return guarded


_STAFF_PROFILE_NOTE = (
    "Changes are recorded in the account's admin history. A member signed in now sees "
    "some display changes only when they next sign in."
)


_GUEST_DEFAULTS_NOTE = (
    "What every guest starts with. A guest can change Display settings, the "
    "fullscreen editor and stylized MRC names for their own call only; the rest is "
    "the shared guest account's, the same for every guest. Changes are recorded in "
    "the account's admin history."
)

# Not on the guest defaults (design doc §5.6): the guest account signs in
# without a credential and has no identity of its own, it has no mailbox, so
# read receipts mean nothing, and sort preferences can only be cleared here
# while a guest's own sort choices last only for their call, so there is
# nothing to set.
_NOT_GUEST_DEFAULTS = frozenset({"identity_details", "read_receipts", "sort_preferences", "ssh_public_key", "password"})


_SHARED_ACCOUNT_NOTE = (
    "You signed in as a guest, on an account every guest shares. What other callers see of it "
    "can't be changed here; display settings you change last for this call only."
)


def _profile_field(label: str, value: str, *, value_color: int = VALUE_COLOR) -> str:
    """Compose one trusted label with a separately sanitized value span."""
    return colored(f"{label}: ", fg_color=LABEL_COLOR) + colored(
        sanitize_text(value), fg_color=value_color
    )


# Bounds the bio preview `_edit_profile`'s own preamble shows -- see
# that function's `_preamble` closure for why an unbounded preview is a
# real, Codex-caught problem now that this screen can paginate.
_MAX_BIO_PREVIEW_LINES = 3


_CHARSET_NAMES = {"utf-8": "Unicode", "cp437": "CP437", "ascii": "ASCII"}


def _charset_label(preference: str, session: Session | None) -> str:
    """`session` is the caller's own, to say what Auto chose for it; `None`
    on a member's Profile, where this session is the SysOp's."""
    if preference == "auto" and session is None:
        return "Auto"
    if preference == "auto":
        return f"Auto (now {_CHARSET_NAMES.get(getattr(session, 'output_charset', 'utf-8'), 'Unicode')})"
    return {"unicode": "Unicode", "cp437": "CP437", "ascii": "ASCII"}[preference]


def _quadrant_label(preference: str, session: Session | None) -> str:
    """The Quarter blocks value (issue #1560), with a sample of them so a
    caller can see whether their font has them. `session` as for
    `_charset_label`."""
    if preference == "auto" and session is not None:
        value = "Auto (now on)" if getattr(session, "quadrant_blocks", True) else "Auto (now off: PuTTY detected)"
    else:
        value = {"auto": "Auto", "on": "On", "off": "Off"}[preference]
    return f"{value}  ▗▟█▙▖"


def _member_persist(
    user: User, actor: User | None, key: str, setter: Callable[[Database, User, Any], None],
    *, describe: Callable[[str], str], apply: Callable[[Any], None] | None = None,
) -> Callable[[DatabaseLane, Any], Awaitable[None]]:
    """`live_choice_field`'s `persist` for one of `user`'s settings.

    On your own Profile, `setter` writes it and `apply` carries it into this
    session (a character set, say). On a member's Profile, opened by `actor`
    from the user editor (design doc §5.6), the write is checked and recorded
    in the account's admin history as `describe(key)` -- the field and its
    new value -- and this session, the SysOp's, is left alone."""

    async def persist(lane: DatabaseLane, value: Any) -> None:
        if actor is None:
            await lane.run(setter, user, value)
            if apply is not None:
                apply(value)
            return
        detail = describe(key)

        def write(db: Database, target: User) -> tuple[None, str]:
            setter(db, target, value)
            return None, detail

        await write_member(lane, actor, user, write)

    return persist


def _field_describer(fields: list[FieldSpec], draft: Draft) -> Callable[[str], str]:
    """`_member_persist`'s `describe`: a field's label and its value as the
    screen shows it, after the toggle has moved it."""

    def describe(key: str) -> str:
        field = next(f for f in fields if f.key == key)
        return f"{field.label}: {field.render(draft)}"

    return describe


def _possessive(user: User) -> str:
    return f"{sanitize_text(user.username)}'s"


@located("Profile")
async def _edit_profile(session: Session, lane: DatabaseLane, user: User, *, actor: User | None = None) -> None:
    """
    Edit your own vCard and caller preferences (design doc) --
    `edit_resource_draft` in immediate mode (issue #160's cursor-nav
    follow-up; see that function's own `save=None` docstring): every
    field here already persists itself the instant it's activated (see
    `live_choice_field`), unlike a resource create/edit screen's own
    draft/Save step, so there is nothing to discard on `[B]ack` and no
    `[S]ave` entry is offered.

    `description_level`/`redraw_in_place` are fetched once, same as
    every other `edit_resource_draft` caller (see that parameter's own
    docstring for why a per-redraw lookup is deliberately avoided).
    One consequence worth calling out because it's new to this specific
    screen: toggling the "Descriptions" field updates *that field's*
    own displayed value immediately, but this same screen's own menu-row
    layout only starts using the new level the next time "Your profile"
    is entered, not mid-visit -- every other `edit_resource_draft`
    caller doesn't expose this preference as one of its own fields, so
    this self-referential case doesn't come up for them.

    `actor` opens a member's Profile from the user editor instead (design
    doc §5.6): the same fields, sections and help, drawn the way `actor`'s
    own screens are, with `user`'s values. Every change is checked and
    recorded in the account's admin history, and nothing reaches `actor`'s
    session: a character set chosen here is the member's, not the SysOp's.

    On the guest account it sets the guest defaults: what every guest starts
    with, under the changes each guest makes for their own call. Only the
    fields that mean something for a shared account without a credential,
    identity or mailbox are offered (`_NOT_GUEST_DEFAULTS`).
    """
    viewer = actor or user
    guest_defaults = actor is not None and await lane.run(is_guest_account, user)
    description_level = await lane.run(menu_description_level, viewer)
    redraw_in_place = await lane.run(redraw_in_place_enabled, viewer)
    unicode_style = await lane.run(unicode_style_enabled, viewer)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, viewer)
    accent_color = await lane.run(effective_accent_color_256)
    header_color = await lane.run(effective_header_color_256)

    draft: Draft = {
        "bio": await lane.run(get_bio, user) or "",
        "bio_visible": await lane.run(is_bio_visible, user),
        "signature": await lane.run(get_signature, user) or "",
        "fullscreen_editor": await lane.run(fullscreen_editor_enabled, user),
        "accepts_dm": await lane.run(accepts_direct_messages, user),
        "blocked_sender_count": len(await lane.run(list_mail_blocks, user)),
        "read_receipts": await lane.run(shares_read_receipts, user),
        "mrc_private": await lane.run(mrc_private_messages_enabled, user),
        "mrc_lastseen": await lane.run(mrc_lastseen_recorded, user),
        "history_name_visible": await lane.run(session_history_name_visible, user),
        "game_results": await lane.run(game_results_announced, user),
        "color_depth": await lane.run(color_depth_override, user) or "auto",
        "description_level": await lane.run(menu_description_level, user),
        "redraw_in_place": await lane.run(redraw_in_place_enabled, user),
        "animations": await lane.run(animations_enabled, user),
        "unicode_style": await lane.run(unicode_style_enabled, user),
        "charset": await lane.run(charset_preference, user),
        "quadrants": await lane.run(quadrant_preference, user),
        "mrc_colors": await lane.run(mrc_colors_enabled, user),
        "post_colors": await lane.run(post_colors_enabled, user),
        "mrc_nick_color": await lane.run(mrc_nick_color, user),
        "mrc_names": await lane.run(mrc_name_style, user),
        "chat_bell": await lane.run(bell_level, user),
        "breadcrumb_collapsed": await lane.run(breadcrumb_collapsed_enabled, user),
        "sort_preference_count": len(await lane.run(list_sort_preferences, user)),
        "ssh_key_count": len(await lane.run(list_ssh_keys, user)),
        "password_set": await lane.run(has_password, user),
        "username_request": getattr(await lane.run(open_username_request, user), "requested_username", None),
    }

    async def _bio_prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        await _edit_bio(session, lane, user, actor=actor)
        draft["bio"] = await lane.run(get_bio, user) or ""

    async def _signature_prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        await _edit_signature(session, lane, user, actor=actor)
        draft["signature"] = await lane.run(get_signature, user) or ""

    async def _identity_details_prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        await _identity_details_screen(session, lane, user, actor=actor)

    async def _blocked_senders_prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        await blocked_senders_screen(session, lane, user, actor=actor)
        draft["blocked_sender_count"] = len(await lane.run(list_mail_blocks, user))

    async def _sort_preferences_prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        await _sort_preferences_screen(session, lane, user, actor=actor)
        draft["sort_preference_count"] = len(await lane.run(list_sort_preferences, user))

    async def _ssh_public_key_prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        """Self-service counterpart to `_draw_user_detail`'s SysOp-only
        `[K]` field (`netbbs.net.admin_flow`) -- both now open the same
        shared `manage_ssh_keys_screen` (`netbbs.net.ssh_key_screen`),
        `changed_by=user` here recording the account acting on its own
        keys, distinct in the moderation log from a SysOp acting on
        someone else's behalf.

        Dogfood report: the old single-key `[K]` field's "add or
        *replace*" semantics silently revoked whichever key was already
        on file the moment a second device's key was added -- a second
        device (a phone, generated fresh because Android's own security
        model won't let an existing private key be copied over) had no
        way to gain SSH login without kicking the first device out. Now
        a real list/add/remove screen backed by `add_ssh_key`/
        `remove_ssh_key`/`list_ssh_keys`, none of which ever revoke a
        different key."""
        if signed_in_without_credential(session):
            # The screen refuses too (issue #531); refused here as well so
            # the reason survives Profile's in-place redraw (issue #1073).
            _refuse_shared_account(session, "this account's keys")
            return
        if actor is not None and not is_usable_sysop(actor):
            # As on the user editor, where `[K]ey` is a SysOp's alone: a key
            # signs in without the password a manager may reset.
            raise StaffEditRefused("only a SysOp can change an account's SSH keys")
        nonlocal user
        user = await manage_ssh_keys_screen(session, lane, user, changed_by=actor or user)
        draft["ssh_key_count"] = len(await lane.run(list_ssh_keys, user))
        draft["password_set"] = await lane.run(has_password, user)

    async def _password_prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        """Issue #611: until this field existed a password was written
        once, at account creation, and could never be changed by anyone.
        Same shared-screen shape as `[K]` above (`netbbs.net.
        password_screen`); `changed_by=user` records the account acting
        on itself, which is also what makes the screen demand the
        current password first."""
        if signed_in_without_credential(session):
            _refuse_shared_account(session, "this account's password")
            return
        nonlocal user
        user = await manage_password_screen(session, lane, user, changed_by=actor or user)
        draft["password_set"] = await lane.run(has_password, user)
        draft["ssh_key_count"] = len(await lane.run(list_ssh_keys, user))

    @inline_field
    async def _username_prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
        """Issue #1306 (design doc §16 #1111 Decision 1): ask the SysOp for a
        new username. The value is the name asked for: typing one asks (or
        replaces the open request), blank withdraws it, Esc keeps it. Checked
        as a signup is, at once; the SysOp approves or refuses it later."""
        if signed_in_without_credential(session):
            _refuse_shared_account(session, "the username")
            return
        current = draft["username_request"] or ""
        await write_field_prompt(
            session, colored(f"New username to ask the SysOp for ({_FIELD_HINT}):", fg_color=MUTED_COLOR),
            hint=_FIELD_HINT,
        )
        try:
            raw = (await read_field_line(session, initial=current)).strip()
        except InputCancelled:
            await session.write_line("")
            return
        if raw == current:
            return
        if not raw:
            withdrawn = await lane.run(withdraw_username_request, user)
            draft["username_request"] = None
            announce_line(session, "Request withdrawn." if withdrawn else "There was no request to withdraw.")
            return
        try:
            await lane.run(request_username_change, user, raw)
        except UsernameChangeError as exc:
            announce_line(session, colored(f"Not asked: {sanitize_text(str(exc))}.", fg_color=MUTED_COLOR))
            return
        draft["username_request"] = raw
        announce_line(
            session,
            f"Asked for {raw!r}. The SysOp decides; until then you keep {user.username!r}.",
        )

    def _color_depth_render(d: Draft) -> str:
        value = d["color_depth"]
        if value == "auto" and actor is not None:
            # What this session detected is the SysOp's terminal, not theirs.
            return "auto"
        if value == "auto":
            if getattr(session, "detected_sixteen_colors", False):
                detected = "16-color"
            else:
                detected = "truecolor" if session.supports_truecolor else "256-color"
            return f"auto (detected: {detected})"
        return f"{value} (forced)"

    def _preamble(d: Draft) -> str:
        lines = [colored("BIO", fg_color=METADATA_COLOR, bold=True)]
        if d["bio"]:
            # Codex review (PR #236): a bio can be up to MAX_BIO_BYTES
            # (2000) with no embedded newlines at all -- well under
            # MAX_BIO_LINES (6), which counts newline-separated lines,
            # not rendered height -- so `reflow` alone could still wrap
            # it to ~25 rows at 80 columns. That's shown on *every*
            # page once this screen paginates (the preamble sits above
            # the paginated field list, not part of it), so it can blow
            # the whole screen's height budget entirely on its own,
            # regardless of how few fields a given page shows -- the one
            # piece of genuinely unbounded-length content on this
            # screen, everything else being short, fixed-shape status
            # strings. Capped here to a fixed preview instead; the full
            # bio is never touched, still fully readable (and editable)
            # via the Bio field.
            bio_lines = reflow(sanitize_text(d["bio"], allow_newlines=True), width=session.terminal_width).split("\n")
            # Codex review (PR #238): resource_editor.py's height budget
            # counts `preamble_text.count("\r\n")` -- entries joined at
            # the *outer* "\r\n".join(lines) below -- not physical
            # display rows. A bare "\n" joining several rows into one
            # `lines` entry (as this used to do for both the preview
            # itself and its truncation marker) is normalized to a real
            # CRLF at the telnet transport boundary (see telnet.py's own
            # write()), so it still *renders* as multiple rows on
            # screen, but the budget math undercounts it as one. Every
            # physical row is therefore its own top-level `lines` entry
            # here, never joined internally with "\n" -- the only way
            # the count stays honest regardless of how many rows a
            # preview or its marker end up taking.
            if len(bio_lines) > _MAX_BIO_PREVIEW_LINES:
                hidden = len(bio_lines) - _MAX_BIO_PREVIEW_LINES
                lines.extend(bio_lines[:_MAX_BIO_PREVIEW_LINES])
                marker_lines = reflow(
                    f"...({hidden} more line{'' if hidden == 1 else 's'} -- open Bio to see the rest)",
                    width=session.terminal_width,
                ).split("\n")
                lines.extend(colored(line, fg_color=MUTED_COLOR) for line in marker_lines)
            else:
                lines.extend(bio_lines)
        else:
            lines.append(colored("(no bio set)", fg_color=MUTED_COLOR))
        lines.append("")
        if signed_in_without_credential(session):
            lines.extend(
                colored(line, fg_color=MUTED_COLOR)
                for line in reflow(_SHARED_ACCOUNT_NOTE, width=session.terminal_width).split("\n")
            )
        if actor is not None:
            note = _GUEST_DEFAULTS_NOTE if guest_defaults else _STAFF_PROFILE_NOTE
            lines.extend(
                colored(line, fg_color=MUTED_COLOR)
                for line in reflow(note, width=session.terminal_width).split("\n")
            )
        if actor is None:
            # About this connection, which on a member's Profile is the SysOp's.
            lines.append(
                _profile_field(
                    "Transport report",
                    getattr(session, "truecolor_diagnostic", "capability report unavailable"),
                    value_color=METADATA_COLOR,
                )
            )
        return "\r\n".join(lines)

    # Dogfood report -- Thiesi's own observation that the main menu's
    # grouped, multi-column layout and this screen's own flat 14-field
    # list read as wildly different levels of polish. Grouped here into
    # four sections via FieldSpec's own new `section` (netbbs.net.
    # resource_editor's own docstring for why this is additive, not a
    # rewrite of the shared component): IDENTITY (who you are, publicly)
    # is what a caller usually opens this screen to change; ACCOUNT is
    # security/data-management, deliberately last, not first, since it's
    # visited far less often than the fields above it. COMMUNICATION and
    # DISPLAY split what remains along "affects how others reach you" vs.
    # "affects how this client renders for you" -- a caller wondering
    # "why does chat behave this way" and one wondering "why does this
    # look wrong" land in different, smaller groups instead of one
    # shared pile. Sections group adjacent fields only (see that same
    # docstring) -- reordered here to match, not just relabeled in
    # place. Also normalizes five different "empty" spellings ("(no bio
    # set)", "(no signature set)", "none saved", "(none set)") down to
    # one, "(none)", consistently used wherever a field has nothing set.
    def describe(key: str) -> str:
        return _field_describer(fields, draft)(key)

    fields = [
        FieldSpec(
            key="bio", label="Bio",
            render=lambda d: f"{len(d['bio'].splitlines())} line(s)" if d["bio"] else "(none)",
            prompt=_unless_signed_in_without_credential("the bio", _bio_prompt),
            brief="Change your public bio text",
            help=(
                "Free-form text, several lines if you like, shown on your Directory card when "
                "Visibility below is public. To remove it, the line editor asks first whether to "
                "clear it; in the fullscreen editor, erase the text and save."
            ),
            section="Identity",
        ),
        FieldSpec(
            key="bio_visible", label="Visibility",
            render=lambda d: "public" if d["bio_visible"] else "private",
            prompt=_unless_signed_in_without_credential("who sees the bio", live_choice_field(
                "bio_visible", [False, True],
                persist=_member_persist(user, actor, "bio_visible", set_bio_visible, describe=describe),
            )),
            brief="Toggle bio public/private",
            help=(
                "Whether your Bio is shown to other callers at all, independent of what the "
                "bio text itself says. Private hides it everywhere except from a SysOp."
            ),
            section="Identity",
        ),
        FieldSpec(
            key="signature", label="Signature",
            render=lambda d: f"{len(d['signature'].splitlines())} line(s)" if d["signature"] else "(none)",
            prompt=_unless_signed_in_without_credential("the signature", _signature_prompt),
            brief="Auto-appended to mail and posts you send",
            help=(
                "Text added to the end of every letter and board post you write from now on; "
                "what you already wrote keeps the signature it had. Blank means no signature."
            ),
            section="Identity",
        ),
        FieldSpec(
            key="identity_details",
            label="Name & details",
            render=lambda d: "(edit)",
            prompt=_unless_signed_in_without_credential("the name and details", _identity_details_prompt),
            brief="Display name, location, age",
            help=(
                "Opens a separate screen for your display name, location, and birthdate -- "
                "each independently shown or hidden to other callers, plus your verified-"
                "badge and Link-attestation-sharing settings."
            ),
            section="Identity",
        ),
        FieldSpec(
            key="fullscreen_editor",
            label="Fullscreen editor (all writing)",
            render=lambda d: "on" if d["fullscreen_editor"] else "off",
            prompt=live_choice_field(
                "fullscreen_editor", [False, True],
                persist=_member_persist(user, actor, "fullscreen_editor", set_fullscreen_editor_enabled, describe=describe),
            ),
            brief="Toggle the fullscreen editor",
            help=(
                "On: writing mail, a post, your bio or signature, or a file description opens "
                "the cursor-addressed fullscreen editor (arrow keys, Ctrl-based commands, like "
                "a simple nano). Off: a plain line-by-line "
                "editor instead -- the safer default for a client that can't reliably position "
                "the cursor."
            ),
            section="Communication",
        ),
        FieldSpec(
            key="accepts_dm",
            label="Direct messages",
            render=lambda d: "accepted" if d["accepts_dm"] else "not accepted",
            prompt=_unless_signed_in_without_credential("whether this account takes messages", live_choice_field(
                "accepts_dm", [False, True],
                persist=_member_persist(user, actor, "accepts_dm", set_accepts_direct_messages, describe=describe),
            )),
            brief="Direct-message preferences",
            help=(
                "Whether other callers can send you live one-to-one messages: /msg, /private, "
                "a message from Who's online, a direct-chat invite (/dm), or a message from "
                "someone on a linked BBS. Doesn't affect chat channels or mail. To stop one "
                "person rather than everyone, block them (Blocked people, below)."
            ),
            section="Communication",
        ),
        FieldSpec(
            key="blocked_senders",
            label="Blocked people",
            render=lambda d: f"{d['blocked_sender_count']} blocked" if d["blocked_sender_count"] else "(none)",
            prompt=_unless_signed_in_without_credential("who this account blocks", _blocked_senders_prompt),
            brief="Refuse mail and messages from someone",
            help=(
                "Lists the people you block, on this BBS and on linked BBSes, and lets you block "
                "someone by name or unblock them. A block refuses their mail and their live "
                "messages: /msg, /private, /dm and Who's online. Someone on this BBS is told you "
                "don't accept mail or messages from them; someone on a linked BBS is told about "
                "mail, while their live messages are dropped without a word. You can also block "
                "someone from a letter they sent you or from Who's online. The system and this "
                "BBS's SysOp can't be blocked. A block doesn't hide what someone says in a chat "
                "channel."
            ),
            section="Communication",
        ),
        FieldSpec(
            key="read_receipts",
            label="Let senders see when I've read their mail",
            render=lambda d: "yes" if d["read_receipts"] else "no",
            prompt=_unless_signed_in_without_credential("read receipts", live_choice_field(
                "read_receipts", [False, True],
                persist=_member_persist(user, actor, "read_receipts", set_shares_read_receipts, describe=describe),
            )),
            brief="Mail read receipts, both ways",
            help=(
                "On (the default): someone who sends you mail on this BBS sees in their Sent "
                "folder when you first opened it, and you see the same for mail you send. It "
                "works both ways: turn it off and no one sees when you read their mail, and you "
                "no longer see when anyone reads yours. Off also hides the receipts already given; "
                "turning it on again shows them again. Mail read while either of you had receipts "
                "off never shows as read, even after you both turn them on. A sender is told that "
                "you don't share receipts, so your unread-looking letter isn't taken for one you "
                "haven't read. Mail to and from other BBSes never has read receipts."
            ),
            section="Communication",
        ),
        FieldSpec(
            key="mrc_private",
            label="Private messages from MRC users",
            render=lambda d: "accepted" if d["mrc_private"] else "not accepted",
            prompt=_unless_signed_in_without_credential("private MRC messages", live_choice_field(
                "mrc_private", [False, True],
                persist=_member_persist(user, actor, "mrc_private", set_mrc_private_messages_enabled, describe=describe),
            )),
            brief="Off by default; also needed to send",
            help=(
                "Whether users on the Multi Relay Chat network can message you privately "
                "(/mrc msg), and whether you can message them. Off by default: a private MRC "
                "message is not private on that network -- the hub and any client can read or "
                "spoof it. Applies the next time you enter an MRC room."
            ),
            section="Communication",
        ),
        FieldSpec(
            key="mrc_lastseen",
            label="MRC may remember when you were last seen",
            render=lambda d: "yes" if d["mrc_lastseen"] else "no",
            prompt=_unless_signed_in_without_credential("what MRC remembers", live_choice_field(
                "mrc_lastseen", [False, True],
                persist=_member_persist(user, actor, "mrc_lastseen", set_mrc_lastseen_recorded, describe=describe),
            )),
            brief="The hub's LASTSEEN record; on is the hub's default",
            help=(
                "Whether the Multi Relay Chat hub may answer other users' LASTSEEN questions about "
                "your handle. On by default, as on the hub itself. Off sends the hub an opt-out each "
                "time you enter an MRC room. Applies the next time you enter one."
            ),
            section="Communication",
        ),
        FieldSpec(
            key="mrc_names",
            label="Stylized MRC names",
            render=lambda d: _MRC_NAME_STYLE_LABELS[d["mrc_names"]],
            prompt=live_choice_field(
                "mrc_names", list(MRC_NAME_STYLES),
                persist=_member_persist(user, actor, "mrc_names", set_mrc_name_style, describe=describe),
            ),
            brief="How a styled MRC name such as +Nick+[TAG] is shown",
            help=(
                "Some MRC users' software puts a styled name in front of their lines, such as "
                "+Nick+[CASTLE BBS]. Combined: the styled name replaces the plain one in "
                "<name@BBS>, and the rest follows in brackets: <+Nick+@Castle_BBS (CASTLE BBS)>. "
                "Both: the plain <Nick@Castle_BBS>, then the styled name as sent. Label only: "
                "just <Nick@Castle_BBS>. The BBS name always comes from the network, never from "
                "the styled name. Lines from before this setting existed look the same in all three."
            ),
            section="Communication",
        ),
        FieldSpec(
            key="history_name_visible",
            label="Name shown to other callers",
            render=lambda d: "yes" if d["history_name_visible"] else "no (hidden)",
            prompt=_unless_signed_in_without_credential("whether the name is shown", live_choice_field(
                "history_name_visible", [False, True],
                persist=_member_persist(user, actor, "history_name_visible", set_session_history_name_visible, describe=describe),
            )),
            brief="Show your name to other callers",
            help=(
                "Whether your username appears in this node's previous-callers roll -- the "
                "list shown after login and from the main menu. Your own [H]istory screen is "
                "unaffected; it only ever shows you your own calls. Hiding it only affects "
                "what ordinary callers see -- a SysOp can always see the real name."
            ),
            section="Communication",
        ),
        FieldSpec(
            key="game_results",
            label="Game results announced in chat",
            render=lambda d: "yes" if d["game_results"] else "no",
            prompt=_unless_signed_in_without_credential("whether game results are announced", live_choice_field(
                "game_results", [False, True],
                persist=_member_persist(user, actor, "game_results", set_game_results_announced, describe=describe),
            )),
            brief="Whether Baud may announce your door game results",
            help=(
                "Whether Baud may announce your ranks, achievements and high scores from door games, "
                "and list you in !top. On by default. Off means none of your game results is announced "
                "and !top leaves you out; the games themselves, and their own Halls of Fame, are "
                "unaffected."
            ),
            section="Communication",
        ),
        FieldSpec(
            key="chat_bell",
            label="Chat bell",
            render=lambda d: BELL_LEVEL_LABELS[d["chat_bell"]],
            prompt=live_choice_field(
                "chat_bell", list(BELL_LEVELS),
                persist=_member_persist(user, actor, "chat_bell", set_bell_level, describe=describe),
            ),
            brief="When chat rings your terminal's bell",
            help=(
                "When chat rings your terminal's bell, so you notice it from another window. "
                "Private (the default): for a private message -- /msg, /private, a message from "
                "someone on a linked BBS or on MRC -- and a /dm invitation. Mentions: those, and "
                "a line in a chat channel that names you, by your username or your alias. Off: "
                "never. Being away doesn't silence it. Your terminal decides whether a bell beeps, "
                "flashes or stays quiet; in the web client it plays a short tone. Also set with "
                "/bell in chat."
            ),
            section="Communication",
        ),
        FieldSpec(
            key="color_depth", label="Color depth",
            render=_color_depth_render,
            prompt=live_choice_field(
                "color_depth", ["auto", "truecolor", "256", "16"],
                persist=_member_persist(
                    user, actor, "color_depth", set_color_depth_override, describe=describe,
                    apply=lambda v: apply_color_depth(session, None if v == "auto" else v),
                ),
            ),
            brief="Force a terminal color depth",
            help=(
                "Overrides NetBBS's automatic terminal-capability detection. 'auto' trusts "
                "what your client reports; force 'truecolor', '256' or '16' only if colors "
                "render wrong under auto. '16' sends only the sixteen classic ANSI colors, "
                "for a classic BBS terminal that shows blinking, reversed or garbled text "
                "where colors should be; auto already picks it for most of them."
            ),
            section="Display",
        ),
        FieldSpec(
            key="description_level",
            label="Menu descriptions",
            render=lambda d: d["description_level"],
            prompt=live_choice_field(
                "description_level", ["off", "brief", "detailed"],
                persist=_member_persist(user, actor, "description_level", set_menu_description_level, describe=describe),
            ),
            brief="Off/brief/detailed menu text",
            help=(
                "Whether menu screens show a short explanation under each option. 'off' is "
                "most compact; 'brief' adds a one-line hint per option; 'detailed' shows the "
                "fullest explanation where a field also defines one, like this help text."
            ),
            section="Display",
        ),
        FieldSpec(
            key="redraw_in_place", label="In-place redraw",
            render=lambda d: "on" if d["redraw_in_place"] else "off",
            prompt=live_choice_field(
                "redraw_in_place", [False, True],
                persist=_member_persist(user, actor, "redraw_in_place", set_redraw_in_place_enabled, describe=describe),
            ),
            brief="Clear screen instead of scrolling",
            help=(
                "On: moving between screens clears the terminal instead of printing below "
                "what's already there -- less scrolling, but anything above the clear (like a "
                "save confirmation) disappears immediately. Off is the safer default -- it "
                "preserves scrollback."
            ),
            section="Display",
        ),
        FieldSpec(
            key="animations",
            label="Banner animations",
            render=lambda d: "animated" if d["animations"] else "quick",
            prompt=live_choice_field(
                "animations", [True, False],
                persist=_member_persist(
                    user, actor, "animations", set_animations_enabled, describe=describe,
                    apply=lambda v: setattr(session, "animations_enabled", v),
                ),
            ),
            brief="Play banners at their modem speed, or draw them at once",
            help=(
                "Animated: a banner the SysOp gave a speed draws at that speed, as a modem of "
                "the day would have, and any key draws the rest at once. Quick: every banner "
                "after you sign in is drawn at once. The welcome banner comes before you sign "
                "in, so it always plays at the SysOp's speed; any key still draws the rest."
            ),
            section="Display",
        ),
        FieldSpec(
            key="charset",
            label="Character set",
            render=lambda d: _charset_label(d["charset"], session if actor is None else None),
            prompt=live_choice_field(
                "charset", list(CHARSET_PREFERENCES),
                persist=_member_persist(
                    user, actor, "charset", set_charset_preference, describe=describe,
                    apply=lambda v: apply_charset_preference(session, v),
                ),
            ),
            brief="What your terminal shows: Auto, Unicode, CP437 or ASCII",
            help=(
                "Which characters NetBBS sends your terminal. Auto follows what your terminal "
                "reported when you connected. Unicode suits modern terminals and the browser; "
                "CP437 suits classic BBS terminals such as SyncTERM and shows ANSI art as drawn; "
                "ASCII is plain text any terminal shows. Takes effect at once."
            ),
            section="Display",
        ),
        FieldSpec(
            key="quadrants",
            label="Quarter blocks",
            render=lambda d: _quadrant_label(d["quadrants"], session if actor is None else None),
            prompt=live_choice_field(
                "quadrants", list(QUADRANT_PREFERENCES),
                persist=_member_persist(
                    user, actor, "quadrants", set_quadrant_preference, describe=describe,
                    apply=lambda v: apply_quadrant_preference(session, v),
                ),
            ),
            brief="Whether your font has the quarter-block pictures games draw with",
            help=(
                "Some games, such as Lunar Lander, draw pictures with quarter blocks. A font "
                "without them shows boxes instead, as PuTTY's default fonts do. Off sends half "
                "blocks in their place: a coarser picture, but every font has them. Auto turns "
                "them off for PuTTY and on everywhere else. If the sample after the value shows "
                "boxes, choose Off; if PuTTY shows them fine with your font, choose On. Only "
                "matters with the Unicode character set. Takes effect at once."
            ),
            section="Display",
        ),
        FieldSpec(
            key="post_colors",
            label="Colors in posts and mail",
            render=lambda d: "on" if d["post_colors"] else "off",
            prompt=live_choice_field(
                "post_colors", [False, True],
                persist=_member_persist(user, actor, "post_colors", set_post_colors_enabled, describe=describe),
            ),
            brief="Show authors' colors in posts and mail",
            help=(
                "On a message board whose SysOp allows color, and in mail, authors can color "
                "what they write with pipe codes (|00-|23). On: those colors are shown. Off: "
                "the same posts and letters appear as plain text. Either way nothing in them "
                "can move the cursor or clear the screen."
            ),
            section="Display",
        ),
        FieldSpec(
            key="mrc_colors",
            label="Inter-BBS chat (MRC) colors",
            render=lambda d: "on" if d["mrc_colors"] else "off",
            prompt=live_choice_field(
                "mrc_colors", [False, True],
                persist=_member_persist(user, actor, "mrc_colors", set_mrc_colors_enabled, describe=describe),
            ),
            brief="Show the colors MRC users put in their lines",
            help=(
                "Users on the Multi Relay Chat network color their lines with Mystic-style "
                "codes. On: those colors are shown in a channel bridged to MRC. Off: the same "
                "lines appear as plain text. Either way the text is sanitized first."
            ),
            section="Display",
        ),
        FieldSpec(
            key="mrc_nick_color",
            label="MRC nick color",
            render=lambda d: f"{CGA_COLOR_NAMES[d['mrc_nick_color']]} (|{d['mrc_nick_color']:02d})",
            prompt=_unless_signed_in_without_credential("the MRC nick color", live_choice_field(
                "mrc_nick_color", list(range(16)),
                persist=_member_persist(user, actor, "mrc_nick_color", set_mrc_nick_color, describe=describe),
            )),
            brief="The color your handle wears on MRC",
            help=(
                "Every line you send to the Multi Relay Chat network carries your handle in front "
                "of it, in this color -- one of the sixteen CGA colors MRC clients understand. "
                "Each press moves to the next color. Applies the next time you enter an MRC room."
            ),
            section="Display",
        ),
        FieldSpec(
            key="breadcrumb_collapsed",
            label="Location style",
            render=lambda d: "always collapsed" if d["breadcrumb_collapsed"] else "auto",
            prompt=live_choice_field(
                "breadcrumb_collapsed", [False, True],
                persist=_member_persist(user, actor, "breadcrumb_collapsed", set_breadcrumb_collapsed_enabled, describe=describe),
            ),
            brief="Always show only the current location, not the full path",
            help=(
                "On: every screen's heading shows only your current location (e.g. 'Trust "
                "policy') instead of the full path ('NetBBS › System › Trust policy'). The "
                "full path already collapses automatically when it doesn't fit your terminal "
                "-- this forces the short form even when there's room to spare."
            ),
            section="Display",
        ),
        FieldSpec(
            key="sort_preferences",
            label="Sort preferences",
            render=lambda d: f"{d['sort_preference_count']} saved" if d["sort_preference_count"] else "(none)",
            prompt=_sort_preferences_prompt,
            brief="Manage saved sort orders",
            help=(
                "Lists the sort preferences you've saved so far (e.g. how boards or file "
                "areas are ordered) and lets you clear them. These are set implicitly "
                "wherever you actually pick a sort order, not edited directly here."
            ),
            section="Account",
        ),
        FieldSpec(
            key="ssh_public_key",
            label="SSH public key(s)",
            render=lambda d: f"{d['ssh_key_count']} key(s)" if d["ssh_key_count"] else "(none)",
            prompt=_ssh_public_key_prompt,
            brief="Manage your SSH login keys",
            help=(
                "Opens a screen listing every SSH public key attached to this account, so you "
                "can log in over SSH with key-based authentication instead of (or alongside) "
                "your password -- from more than one device at once, each with its own key "
                "(useful when a device, e.g. a phone, can't have an existing private key "
                "copied onto it and has to generate its own). [A]dd accepts a key pasted as "
                "base64, or a full 'ssh-ed25519 ...' line, and never revokes any other key "
                "already on the account. Removing your last key is only offered if you have a "
                "password set, since an account needs at least one way to log in."
            ),
            section="Account",
        ),
        FieldSpec(
            key="password",
            label="Password",
            render=lambda d: "set" if d["password_set"] else "(none -- key login only)",
            prompt=_password_prompt,
            brief="Change your sign-in password",
            help=(
                "Opens a screen where you can change the password you sign in with. You are "
                "asked for your current password first, then the new one twice; nothing is "
                "echoed. If your account signs in by SSH key only, this is where you add a "
                "password, and if it has both, this is where you can remove the password to "
                "keep key login as the only way in. If you have forgotten your password, ask "
                "your SysOp to set a new one for you."
            ),
            section="Account",
        ),
        FieldSpec(
            key="username",
            label="Username",
            render=lambda d: (
                f"{user.username} (asked for {d['username_request']})" if d["username_request"] else user.username
            ),
            prompt=_username_prompt,
            brief="Ask the SysOp for a new username",
            help=(
                "The name you log in with, and who you are to other nodes on NetBBS Link. "
                "Type a new one to ask the SysOp for it: it is checked as a new account's "
                "name is, and the SysOp approves or refuses it. A refusal, and why, is shown "
                "when you next log in. Blank withdraws your request. If it is approved, you "
                "are logged out and log in with the new name; mail sent to the old one still "
                "reaches you, and your old posts keep the name they were written under. One "
                "change every 30 days."
            ),
            section="Account",
        ),
    ]
    if actor is not None:
        # The SysOp renames an account from its detail screen ([C]hange
        # username), not by asking on the member's behalf.
        fields = [field for field in fields if field.key != "username"]

    title, subtitle = "Your profile", "Your public identity and caller preferences."
    if actor is not None:
        # Every field, refusable: a permission taken away while this screen
        # is open stops the next change and says so.
        fields = [dataclasses.replace(field, prompt=refusable(field.prompt)) for field in fields]
        title = "Profile"
        # Short enough for one row: a subtitle is cut, not wrapped.
        subtitle = f"{_possessive(user)} profile. Every change is logged."
        if guest_defaults:
            fields = [field for field in fields if field.key not in _NOT_GUEST_DEFAULTS]
            title = "Guest defaults"
            subtitle = "What every guest starts with. Every change is logged."
    await edit_resource_draft(
        session, lane,
        title=title,
        subtitle=subtitle,
        own=True,
        fields=fields,
        draft=draft,
        back_menu_text=menu_key("B", "ack"),
        description_level=description_level,
        redraw_in_place=redraw_in_place,
        preamble=_preamble,
        unicode_style=unicode_style,
        collapsed=collapsed,
        accent_color=accent_color,
        header_color=header_color,
        # What a field reported -- a refusal above all (issue #1073) -- is
        # shown on the redraw instead of above a screen the redraw clears.
        notices=lambda: take_notices(session),
    )


async def _edit_bio(session: Session, lane: DatabaseLane, user: User, *, actor: User | None = None) -> None:
    """
    Edits the bio via the fullscreen prose editor if `user` has opted
    in (`netbbs.net.editor_preference`), otherwise `netbbs.net.
    composition.edit_line_body` -- the same shared plain-line editor
    `netbbs.net.mail_flow` already uses for message bodies.

    Dogfood follow-up: this used to be a bespoke
    `for _ in range(MAX_BIO_LINES): read_line()` loop with one
    `try/except BioError` at the very end -- a byte-cap overrun was
    only ever discovered after every line had already been typed, and
    the *entire* draft was then discarded with no indication which
    line(s) to trim. `edit_line_body` validates each candidate as it's
    submitted and rejects only the addition that broke a limit,
    keeping everything already accepted -- plus `/cancel`, `/done`,
    and (via `draft_path`) the same crash-recovery offer the fullscreen
    path above already has. `set_bio`'s own `BioError` check below is
    still the final word either way -- belt-and-suspenders, not the
    only check anymore.

    One behavior change worth calling out: `edit_line_body` refuses to
    submit a genuinely blank body ("Body cannot be blank.", by design --
    it's shared with mail/post composition, which have no legitimate
    reason to be empty). The bespoke loop it replaces treated an
    immediate blank first line as "clear the bio," a real, if easy to
    trigger by accident, capability -- restored below as an explicit,
    confirmed step instead, only offered when there's an existing bio
    to lose.
    """
    if signed_in_without_credential(session):
        _refuse_shared_account(session, "the bio")
        return
    viewer = actor or user
    whose = "Your" if actor is None else _possessive(user)

    async def _save(text: str) -> None:
        if actor is None:
            await lane.run(set_bio, user, text)
            return

        def write(db: Database, target: User) -> tuple[None, str]:
            set_bio(db, target, text)
            return None, "Bio " + ("cleared" if not text else "changed")

        await write_member(lane, actor, user, write)

    # The editor is the one the person typing chose.
    if await lane.run(fullscreen_editor_enabled, viewer):
        current = await lane.run(get_bio, user) or ""
        result = await edit_prose(
            session, initial_text=current, draft_path=await lane.run(_bio_draft_path, user, actor), max_bytes=MAX_BIO_BYTES,
            unicode_style=await lane.run(unicode_style_enabled, viewer),
            # What is being written, above the text (issue #813).
            header=EditorHeader(f"{whose} bio", color=await lane.run(effective_header_color_256)),
        )
        if result is None:
            return
        text = result
    else:
        current = await lane.run(get_bio, user)
        if current and await prompt_yes_no(
            session, f"Clear {whose.lower() if actor is None else whose} bio instead of editing it?", default=False
        ):
            await _save("")
            announce_line(session, "Bio cleared.")
            return
        result = await edit_line_body(
            session,
            initial_text=current,
            max_bytes=MAX_BIO_BYTES,
            max_lines=MAX_BIO_LINES,
            draft_path=await lane.run(_bio_draft_path, user, actor),
        )
        if result is None:
            return
        text = result

    try:
        await _save(text)
    except BioError as exc:
        announce_line(session, f"Could not save bio: {exc}")
        return
    announce_line(session, "Bio updated.")


def _bio_draft_path(db: Database, user: User, actor: User | None = None) -> Path:
    """A member's own draft, or -- for a SysOp writing it for them -- one of
    the SysOp's, so neither recovers the other's unsaved text."""
    if actor is not None:
        return drafts_directory(db) / f"bio_{user.id}_by_{actor.id}.draft"
    return drafts_directory(db) / f"bio_{user.id}.draft"


async def _edit_signature(session: Session, lane: DatabaseLane, user: User, *, actor: User | None = None) -> None:
    """Edits the signature auto-appended to mail/board posts
    (`netbbs.signature.append_signature`) -- same shape as `_edit_bio`
    immediately above (fullscreen prose editor or `edit_line_body`
    depending on `netbbs.net.editor_preference`, a clear-if-blank
    confirm, crash-recovery draft path), deliberately not deduplicated
    with it: the two edit genuinely different fields with different
    caps (`MAX_SIGNATURE_LINES`/`MAX_SIGNATURE_BYTES` vs. bio's own),
    and `_edit_bio`'s own docstring already explains why this shape
    exists over a bespoke line-at-a-time loop -- that reasoning applies
    here unchanged, not something worth re-deriving via a shared helper
    for two four-line call sites."""
    if signed_in_without_credential(session):
        _refuse_shared_account(session, "the signature")
        return
    viewer = actor or user
    whose = "Your" if actor is None else _possessive(user)

    async def _save(text: str) -> None:
        if actor is None:
            await lane.run(set_signature, user, text)
            return

        def write(db: Database, target: User) -> tuple[None, str]:
            set_signature(db, target, text)
            return None, "Signature " + ("cleared" if not text else "changed")

        await write_member(lane, actor, user, write)

    # The editor is the one the person typing chose.
    if await lane.run(fullscreen_editor_enabled, viewer):
        current = await lane.run(get_signature, user) or ""
        result = await edit_prose(
            session, initial_text=current, draft_path=await lane.run(_signature_draft_path, user, actor),
            max_bytes=MAX_SIGNATURE_BYTES,
            unicode_style=await lane.run(unicode_style_enabled, viewer),
            header=EditorHeader(f"{whose} signature", color=await lane.run(effective_header_color_256)),
        )
        if result is None:
            return
        text = result
    else:
        current = await lane.run(get_signature, user)
        if current and await prompt_yes_no(
            session, f"Clear {whose.lower() if actor is None else whose} signature instead of editing it?", default=False
        ):
            await _save("")
            announce_line(session, "Signature cleared.")
            return
        result = await edit_line_body(
            session,
            initial_text=current,
            max_bytes=MAX_SIGNATURE_BYTES,
            max_lines=MAX_SIGNATURE_LINES,
            draft_path=await lane.run(_signature_draft_path, user, actor),
        )
        if result is None:
            return
        text = result

    try:
        await _save(text)
    except SignatureError as exc:
        announce_line(session, f"Could not save signature: {exc}")
        return
    announce_line(session, "Signature updated.")


def _signature_draft_path(db: Database, user: User, actor: User | None = None) -> Path:
    """A member's own draft, or -- for a SysOp writing it for them -- one of
    the SysOp's, so neither recovers the other's unsaved text."""
    if actor is not None:
        return drafts_directory(db) / f"signature_{user.id}_by_{actor.id}.draft"
    return drafts_directory(db) / f"signature_{user.id}.draft"


# -- identity attestation: self-reported profile fields (design doc §18) --


async def _identity_details_screen(
    session: Session, lane: DatabaseLane, user: User, *, actor: User | None = None
) -> None:
    """
    Self-reported `display_name`/`location`/`birthdate`, each with its
    own visibility toggle, plus the SysOp-verified side: the general
    "verified" badge visibility toggle and the per-attribute Link
    sharing toggles (design doc §18) -- a separate screen from
    `_edit_profile`'s own bio/fullscreen-editor options rather than
    crowding nine more fields onto that one menu.

    Issue #282: the three self-reported fields used to combine "edit
    the value" and "set its visibility" into one two-prompt chain (a
    `read_line` with blank = keep, then an unconditional `prompt_yes_no
    ("Show it publicly?", default=False)` whose answer was always
    written). Pressing Enter twice just to look at a field silently set
    it private. Each value prompt now writes only its own value, and
    visibility is its own instant `live_choice_field` toggle, the same
    shape `_edit_profile`'s `bio`/`bio_visible` pair already uses.
    Likewise, Link sharing used to be a sub-screen that toggled *off*
    silently but asked a yes/no before toggling *on*; both directions
    are now the same one-keystroke toggle, and an attribute no SysOp has
    verified yet simply reports that instead of offering anything.
    Sections group the self-reported and verified halves so the screen
    paginates cleanly on a 24-row terminal.

    `actor` edits a member's details from their Profile (design doc §5.6):
    the display name and birthdate through the staff path the user editor
    uses (`change_display_name`, `change_birthdate`), everything else checked
    and recorded. Whether a verification is shared over Link stays the
    member's choice.
    """
    if signed_in_without_credential(session):
        _refuse_shared_account(session, "the name and details")
        return
    viewer = actor or user
    description_level = await lane.run(menu_description_level, viewer)
    redraw_in_place = await lane.run(redraw_in_place_enabled, viewer)
    unicode_style = await lane.run(unicode_style_enabled, viewer)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, viewer)
    accent = await lane.run(effective_accent_color_256)
    header = await lane.run(effective_header_color_256)

    draft: Draft = {
        "display_name": await lane.run(get_display_name, user),
        "display_name_visible": await lane.run(is_display_name_visible, user),
        "location": await lane.run(get_location, user),
        "location_visible": await lane.run(is_location_visible, user),
        "birthdate": await lane.run(get_birthdate, user),
        "birthdate_visible": await lane.run(is_birthdate_visible, user),
        "verified_badge_visible": await lane.run(is_verified_badge_visible, user),
        "age_attestation": await lane.run(get_attestation, user, "age"),
        "name_attestation": await lane.run(get_attestation, user, "name"),
        # Issue #596: how many nodes a shared attestation can reach right now.
        # The caller is told how many; which ones is the SysOp's screen.
        "recipient_count": await lane.run(count_attestation_recipients),
        # Issue #632: how many of them hold what this node last sent.
        "delivered_age": (await lane.run(attestation_delivery_counts, user.id, "age"))[0],
        "delivered_name": (await lane.run(attestation_delivery_counts, user.id, "name"))[0],
    }

    def _self_reported_prompt(
        key: str, name: str, *, label: str | None = None, as_text: Callable[[Any], str],
        parse: Callable[[str], Any], invalid: str = "", save: Callable[..., Any],
    ) -> Callable[[Session, DatabaseLane, Draft], Awaitable[None]]:
        """One self-reported value, edited the way the SysOp edits it
        (#1110): the value opens in the line, blank clears it, Esc keeps
        it. The prompt used to put "[current] -- new value (blank to keep,
        - to clear):" in front of the cursor, which left two columns to
        type a birthdate into on an 80-column screen. `parse` raises
        ValueError for an entry that is not a value, and `invalid` says so."""

        @inline_field
        async def prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
            current = "" if draft[key] is None else as_text(draft[key])
            await write_field_prompt(
                session, colored(f"{label or name} ({_FIELD_HINT}):", fg_color=MUTED_COLOR), hint=_FIELD_HINT,
            )
            try:
                raw = (await read_field_line(session, initial=current)).strip()
            except InputCancelled:
                await session.write_line("")
                return
            if raw == current:
                return
            if not raw:
                await _store(key, name, None, save)
                draft[key] = None
                announce_line(session, f"{name} cleared.")
                return
            try:
                value = parse(raw)
            except ValueError:
                announce_line(session, colored(f"{invalid} -- unchanged.", fg_color=MUTED_COLOR))
                return
            try:
                await _store(key, name, value, save)
            except ProfileFieldError as exc:
                announce_line(session, colored(f"Could not save {name.lower()}: {exc}", fg_color=MUTED_COLOR))
                return
            draft[key] = value
            announce_line(session, f"{name} updated.")

        return prompt

    async def _store(key: str, name: str, value: Any, save: Callable[..., Any]) -> None:
        """Write (or, with `None`, clear) one self-reported value."""
        if actor is None:
            if value is None:
                await lane.run(clear_own_profile_field, user, key)
            else:
                await lane.run(save, user, value)
            return
        if key == "display_name":
            await as_staff(lane.run(change_display_name, user, value, changed_by=actor))
            return
        if key == "birthdate":
            await as_staff(lane.run(change_birthdate, user, value, changed_by=actor))
            return

        def write(db: Database, target: User) -> tuple[None, str]:
            if value is None:
                clear_own_profile_field(db, target, key)
            else:
                save(db, target, value)
            # What they typed may be private: the record says only that it changed.
            return None, f"{name} {'cleared' if value is None else 'changed'}"

        await write_member(lane, actor, user, write)

    _display_name_prompt = _self_reported_prompt(
        "display_name", "Display name", as_text=sanitize_text, parse=str, save=set_display_name,
    )
    _location_prompt = _self_reported_prompt(
        "location", "Location", as_text=sanitize_text, parse=str, save=set_location,
    )
    _birthdate_prompt = _self_reported_prompt(
        "birthdate", "Birthdate", label="Birthdate (YYYY-MM-DD)", as_text=date.isoformat,
        parse=date.fromisoformat, invalid="Not a valid date (expected YYYY-MM-DD)", save=set_birthdate,
    )

    def _link_share_toggle(attribute: str) -> Callable[[Session, DatabaseLane, Draft], Awaitable[None]]:
        # One keystroke flips `link_visible` either way -- but only the
        # state the caller is actually looking at. The attestation is
        # re-read before acting; if a SysOp re-attested (which clears
        # `link_visible`) or revoked it since the screen was drawn, the
        # keypress refreshes and reports that instead of toggling, so a
        # press meant to switch a displayed "on" off can never enable
        # sharing of a replacement attestation the caller hasn't seen
        # (Codex review, PR #284).
        key = f"{attribute}_attestation"

        def _toggle_if_unchanged(db: Database, shown):
            # Compare and mutate inside one lane call so the record the
            # caller saw is the record that gets toggled -- a re-attest
            # or removal landing between a separate read and write
            # could otherwise enable sharing of an unseen replacement
            # (Codex review, PR #284, second round).
            current = get_attestation(db, user, attribute)
            if current is None or current != shown:
                return False, current
            if current.self_attested and not current.link_visible:
                # Issue #1443: said, not toggled -- a press here is not the
                # "changed since this screen was drawn" race below.
                return False, current
            try:
                return True, set_attestation_link_visible(db, user, attribute, not current.link_visible)
            except AttestationError:
                return False, get_attestation(db, user, attribute)

        async def prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
            if actor is not None:
                # Sharing a verified date of birth or name with other nodes is
                # an opt-in only its owner gives (design doc §18).
                raise StaffEditRefused(f"only {sanitize_text(user.username)} can choose to share a verification over Link")
            shown = draft.get(key)
            toggled, attestation = await lane.run(_toggle_if_unchanged, shown)
            draft[key] = attestation
            # Re-read with the toggle, so the number beside "on" is the one
            # that was true when the caller switched it on.
            draft["recipient_count"] = await lane.run(count_attestation_recipients)
            draft[f"delivered_{attribute}"] = (await lane.run(attestation_delivery_counts, user.id, attribute))[0]
            if toggled:
                return
            if attestation is not None and attestation == shown and attestation.self_attested:
                await session.write_line(colored(f"Not shared: {SELF_ATTESTED_NOT_SHARED}.", fg_color=MUTED_COLOR))
                return
            if attestation is None:
                await session.write_line(
                    colored(
                        f"No {attribute} attestation exists -- nothing to share until a SysOp verifies it."
                        if shown is None
                        else f"The {attribute} attestation was removed since this screen was drawn -- nothing to share now.",
                        fg_color=MUTED_COLOR,
                    )
                )
                return
            await session.write_line(
                colored(
                    f"The {attribute} attestation changed since this screen was drawn -- refreshed, "
                    "not toggled. Press again to change sharing for the current one.",
                    fg_color=MUTED_COLOR,
                )
            )

        return prompt

    def _link_share_render(attribute: str) -> Callable[[Draft], str]:
        key = f"{attribute}_attestation"

        def render(d: Draft) -> str:
            attestation = d[key]
            if attestation is None:
                return "(not verified)"
            if attestation.self_attested:
                return "off (you verified it yourself)"
            if not attestation.link_visible:
                return "off"
            # Said beside the value rather than only in the help, because
            # "on" with nobody to receive it shares nothing, and a caller
            # waiting for a remote age gate to open should be able to see that.
            count = d.get("recipient_count", 0)
            if count == 0:
                return "on (your SysOp shares with no node yet)"
            # Issue #632: what was actually sent, not who could ask. A change
            # the caller just made reads as not delivered until the next sync
            # pass sends it, which is the truth.
            delivered = d.get(f"delivered_{attribute}", 0)
            plural = "s" if count != 1 else ""
            if delivered == 0:
                return "on (not delivered yet)"
            if delivered >= count:
                return f"on (sent to {count} node{plural})"
            return f"on (sent to {delivered} of {count} node{plural})"

        return render

    def _visibility_render(key: str) -> Callable[[Draft], str]:
        return lambda d: "public" if d[key] else "private"

    def _birthdate_render(d: Draft) -> str:
        birthdate = d["birthdate"]
        if birthdate is None:
            return "(not set)"
        return f"{birthdate.isoformat()} (age {compute_age(birthdate)})"

    def _preamble(d: Draft) -> str:
        """What this node verified, with the values (issue #1115): apart
        from the self-reported fields below, so a caller who clears their
        own birthdate sees why an age still counts, and which one."""
        age_attestation, name_attestation = d["age_attestation"], d["name_attestation"]
        if age_attestation is None and name_attestation is None:
            return colored("Verified by this node: (none)", fg_color=MUTED_COLOR)
        parts = []
        if age_attestation is not None:
            parts.append(f"born {age_attestation.attested_value}")
        if name_attestation is not None:
            parts.append(f"real name {sanitize_text(name_attestation.attested_value)}")
        return colored(f"Verified by this node: {', '.join(parts)}", fg_color=accent)

    visibility_help = (
        "Whether other callers can see this value at all. Private hides it everywhere "
        "except from a SysOp. The value itself is kept either way."
    )

    def describe(key: str) -> str:
        return _field_describer(fields, draft)(key)

    fields = [
        FieldSpec(
            key="display_name", label="Display name",
            render=lambda d: sanitize_text(d["display_name"]) if d["display_name"] else "(not set)",
            prompt=_display_name_prompt,
            brief="Set your shown display name",
            help=(
                "An alternate name shown alongside your username when its visibility below "
                "is public. Self-reported and unverified -- distinct from a SysOp-verified "
                "real name (see 'Verified' above, and the Verified badge field below)."
            ),
            section="Self-reported",
        ),
        FieldSpec(
            key="display_name_visible",
            label="Display name visibility",
            render=_visibility_render("display_name_visible"),
            prompt=live_choice_field(
                "display_name_visible", [False, True],
                persist=_member_persist(user, actor, "display_name_visible", set_display_name_visible, describe=describe),
            ),
            brief="Toggle display name public/private",
            help=visibility_help,
            section="Self-reported",
        ),
        FieldSpec(
            key="location", label="Location",
            render=lambda d: sanitize_text(d["location"]) if d["location"] else "(not set)",
            prompt=_location_prompt,
            brief="Set your shown location",
            help=(
                "Free-text location (city, region, whatever you want), shown to other callers "
                "only when its visibility below is public. Not validated or verified -- purely "
                "self-reported."
            ),
            section="Self-reported",
        ),
        FieldSpec(
            key="location_visible",
            label="Location visibility",
            render=_visibility_render("location_visible"),
            prompt=live_choice_field(
                "location_visible", [False, True],
                persist=_member_persist(user, actor, "location_visible", set_location_visible, describe=describe),
            ),
            brief="Toggle location public/private",
            help=visibility_help,
            section="Self-reported",
        ),
        FieldSpec(
            key="birthdate", label="Birthdate",
            render=_birthdate_render,
            prompt=_birthdate_prompt,
            brief="Set your birthdate",
            help=(
                "Used to compute your age, which some boards/areas/channels require a "
                "minimum age to post or join. That age gate is checked against this value "
                "even if you keep it private -- the visibility below only controls whether "
                "*other callers* can see your birthdate/age, not whether age gates apply."
            ),
            section="Self-reported",
        ),
        FieldSpec(
            key="birthdate_visible",
            label="Age visibility",
            render=_visibility_render("birthdate_visible"),
            prompt=live_choice_field(
                "birthdate_visible", [False, True],
                persist=_member_persist(user, actor, "birthdate_visible", set_birthdate_visible, describe=describe),
            ),
            brief="Toggle birthdate/age public/private",
            help=visibility_help,
            section="Self-reported",
        ),
        FieldSpec(
            key="verified_badge_visible",
            label="Verified badge",
            render=_visibility_render("verified_badge_visible"),
            prompt=live_choice_field(
                "verified_badge_visible", [False, True],
                persist=_member_persist(user, actor, "verified_badge_visible", set_verified_badge_visible, describe=describe),
            ),
            brief="Show/hide your verified badge",
            help=(
                "Whether a badge marking your SysOp-verified real name/age is shown to other "
                "callers, once a SysOp has actually verified something. Has no effect until "
                "something is verified -- see 'Verified' at the top of this screen."
            ),
            section="SysOp-verified",
        ),
        FieldSpec(
            key="age_attestation",
            label="Share verified age over Link",
            render=_link_share_render("age"),
            prompt=_link_share_toggle("age"),
            brief="Toggle sharing your verified age",
            help=(
                "Whether your SysOp-verified age is shared over NetBBS Link, so a remote "
                "board with an age gate can let you in. Off by default -- this "
                "node's own verification of you isn't shared elsewhere unless you opt in, "
                "and a fresh verification switches it off again. While it is on, your "
                "verified date of birth is given only to the nodes your SysOp has named to "
                "receive this node's verifications; the number beside 'on' is how many that "
                "is now, and your SysOp may name more later. Switching it back off makes this "
                "node stop giving the date out and delete its signed copy, and tells the "
                "nodes it shares with to forget it. A node running NetBBS does; nothing can "
                "force a node that already copied the date to, and a node your SysOp has "
                "since stopped sharing with keeps its copy until that expires, within 90 days."
            ),
            section="SysOp-verified",
        ),
        FieldSpec(
            key="name_attestation",
            label="Share verified name over Link",
            render=_link_share_render("name"),
            prompt=_link_share_toggle("name"),
            brief="Toggle sharing your verified name",
            help=(
                "Whether your SysOp-verified real name is shared over NetBBS Link, so a "
                "remote board that requires a verified name can let you in. Off "
                "by default -- this node's own verification of you isn't shared elsewhere "
                "unless you opt in, and a fresh verification switches it off again. While "
                "it is on, your verified real name is given only to the nodes your SysOp "
                "has named to receive this node's verifications; the number beside 'on' is "
                "how many that is now, and your SysOp may name more later. Switching it back "
                "off makes this node stop giving the name out and delete its signed copy, "
                "and tells the nodes it shares with to forget it. A node running NetBBS does; "
                "nothing can force a node that already copied the name to, and a node your "
                "SysOp has since stopped sharing with keeps its copy until that expires, "
                "within 90 days."
            ),
            section="SysOp-verified",
        ),
    ]

    if actor is not None:
        fields = [dataclasses.replace(field, prompt=refusable(field.prompt)) for field in fields]
    await edit_resource_draft(
        session, lane,
        title="Name & details",
        fields=fields,
        draft=draft,
        back_menu_text=menu_key("B", "ack"),
        description_level=description_level,
        redraw_in_place=redraw_in_place,
        preamble=_preamble,
        unicode_style=unicode_style,
        collapsed=collapsed,
        accent_color=accent,
        header_color=header,
    )


# -- identity attestation: the [V]erify main-menu screen (design doc §18) --


@located("Verify")
async def _verify_identity_menu(
    session: Session, db: Database, verifier: User, *, lane: DatabaseLane | None = None,
) -> None:
    """
    Conditionally-visible main-menu entry for users with
    `can_verify_identity` (or SysOp level) -- lives at the main menu
    rather than inside the admin menu, since a granted verifier may not
    have admin access otherwise (design doc §18).
    """
    # A SysOp verifies themselves too (maintainer decision 2026-10-23, issue
    # #1443): a real-name echo needs their name. Anyone else vouches for
    # others only, as `attestation.attest_name` enforces.
    may_self_verify = verifier.user_level >= SYSOP_LEVEL
    candidates = [u for u in list_users(db) if may_self_verify or u.id != verifier.id]
    selected = await pick_item(
        session,
        candidates,
        name_of=lambda u: u.username,
        stable_id_of=lambda u: u.id,
        description_of=lambda u: _verification_status_description(db, u),
        title="Verify a user's identity",
        own=True,
        empty_message="No users to verify." if may_self_verify else "No other users to verify.",
        help_about=(
            "Every account you may verify, with what this node has verified for it. Pick one to record "
            "a birthdate or real name you checked, or to revoke one. Boards and areas that require a "
            "verified name or age let them in from then on."
        ),
        redraw_in_place=redraw_in_place_enabled(db, verifier),
        unicode_style=unicode_style_enabled(db, verifier),
        collapsed=breadcrumb_collapsed_enabled(db, verifier),
        accent_color=effective_accent_color(session, db),
        header_color=effective_header_color(session, db),
    )
    if selected is not None:
        await _verify_user(session, db, verifier, selected, lane=lane)


async def _revoke_one(
    session: Session, lane: DatabaseLane, verifier: User, subject: User, *,
    header_color: int | tuple[int, int, int] = HEADER_COLOR, unicode_style: bool = False,
) -> None:
    """Withdraw `subject`'s verified age or real name (issue #1115), asking
    which when both are on record, then confirming."""
    age = await lane.run(get_attestation, subject, "age")
    name = await lane.run(get_attestation, subject, "name")
    if age is not None and name is not None:
        while True:
            await write_prompt(session, highlight_hotkeys("Revoke which: [A]ge, [N]ame, or [B]ack? "))
            which = (await session.read_key()).lower()
            if which != HELP_KEY:
                break
            await show_menu_help(
                session, "Revoke help",
                [
                    MenuEntry(label=menu_key("A", "ge"), brief="Withdraw the verified age"),
                    MenuEntry(label=menu_key("N", "ame"), brief="Withdraw the verified real name"),
                ],
                about=(
                    f"{sanitize_text(subject.username)} has both a verified age and a verified real name. "
                    "Revoking one leaves the other; you are asked to confirm."
                ),
                header_color=header_color,
                unicode_style=unicode_style,
            )
            await session.write_line("")
        await session.write_line("")
        if which not in ("a", "n"):
            announce_line(session, "Cancelled.")
            return
        attribute = "age" if which == "a" else "name"
    elif age is None and name is None:
        announce_line(session, f"No verification on record for {subject.username!r} any more.")
        return
    else:
        attribute = "age" if age is not None else "name"
    label = "age" if attribute == "age" else "real name"
    if not await prompt_yes_no(
        session,
        f"Revoke the verified {label} of {subject.username!r}? Anything that asks for a "
        f"verified {label} will refuse them again.",
        default=False,
    ):
        announce_line(session, "Cancelled.")
        return
    try:
        revoked = await lane.run(revoke_attestation, subject, attribute, actor=verifier)
    except AttestationError as exc:
        announce_line(session, f"Could not revoke: {exc}")
        return
    if revoked:
        announce_line(session, f"Verified {label} revoked.")
    else:
        # Someone else revoked it while this screen waited for an answer.
        announce_line(session, f"No verified {label} on record for {subject.username!r} any more.")


def _verification_status_description(db: Database, user: User) -> str:
    parts = []
    if get_attestation(db, user, "age") is not None:
        parts.append("age verified")
    if get_attestation(db, user, "name") is not None:
        parts.append("name verified")
    return ", ".join(parts) if parts else "not verified"


#: What a Verify field's line editor says on the `Choice:` row: the value goes
#: into the draft, and only `[S]ave` records it.
_VERIFY_FIELD_HINT = "Enter takes it; Esc keeps"
_VERIFY_LABELS = {"age": "age", "name": "real name"}


def _verified_birthdate_text(value: str) -> str:
    """An ISO birthdate with the age it gives today, as the field and the
    confirmation show it: a mistyped year reads as a wrong age."""
    return f"{value} (age {compute_age(date.fromisoformat(value))})"


def _load_verify_state(db: Database, subject: User) -> dict[str, Any]:
    """What the Verify screen shows besides its draft, read in one go: the
    stored verifications, who made them, and what the caller says."""
    shown = {attribute: get_attestation(db, subject, attribute) for attribute in ("age", "name")}
    verifiers: dict[int, str] = {}
    for attestation in shown.values():
        if attestation is not None and attestation.verifier_user_id is not None:
            verifier = get_user_by_id(db, attestation.verifier_user_id)
            verifiers[attestation.verifier_user_id] = verifier.username if verifier is not None else "a former account"
    return {
        "shown": shown,
        "verifiers": verifiers,
        "self_birthdate": get_birthdate(db, subject),
        "self_display_name": get_display_name(db, subject),
    }


async def _verify_user(
    session: Session, db: Database, verifier: User, subject: User, *, lane: DatabaseLane | None = None,
) -> None:
    """
    One user's verification screen (design doc §18, §3.5): a resource's
    own screen in the draft editor, as the console's are (issue #1081).
    What the caller says about themselves and who verified what sit
    above two fields, `01` the verified birthdate and `02` the verified
    real name, each edited where it is shown and opened on the value on
    record. Nothing is stored until `[S]ave`, which says what it records,
    what it replaces and whose Link sharing that switches off, and asks
    once; both values then land together with their history entries, or
    neither does (`attest_identity`). A verification changed by someone
    else since the screen was drawn is not replaced: the save refuses and
    the screen shows what is stored now. Emptying a stored value does not
    withdraw it; `[R]evoke`, offered while nothing is unsaved, does.

    Issue #282 replaced two gating yes/no questions here with an action
    bar; this replaces that bar's `[A]ge`/`[N]ame` prompts, which read a
    value under the bar and stored it on Enter.
    """
    if lane is None:
        own_lane = DatabaseLane(db.path)
        try:
            await _verify_user(session, db, verifier, subject, lane=own_lane)
        finally:
            own_lane.close()
        return

    self_verify = verifier.id == subject.id
    who = "yourself" if self_verify else repr(subject.username)
    title = "Verifying yourself" if self_verify else f"Verifying {sanitize_text(subject.username)}"
    header_color = await lane.run(effective_header_color_256)
    unicode_style = await lane.run(unicode_style_enabled, verifier)
    state: dict[str, Any] = await lane.run(_load_verify_state, subject)

    def _stored(attribute: str) -> str:
        attestation = state["shown"][attribute]
        return attestation.attested_value if attestation is not None else ""

    def _verified_by(attestation: UserAttestation, *, sharing: bool = True) -> str:
        if attestation.verifier_user_id == verifier.id:
            by = "you"
        else:
            by = sanitize_text(state["verifiers"].get(attestation.verifier_user_id, "a former account"))
        text = f"by {by} on {attestation.created_at[:10]}"
        if attestation.self_attested:
            text += " (self)"
        if sharing and attestation.link_visible:
            text += ", shared over Link"
        return text

    def _preamble(_d: Draft) -> str:
        says = []
        if state["self_birthdate"] is not None:
            says.append(f"born {_verified_birthdate_text(state['self_birthdate'].isoformat())}")
        if state["self_display_name"]:
            says.append(f"display name {sanitize_text(state['self_display_name'])}")
        lines = [
            colored(
                ("You say: " if self_verify else "They say: ") + (", ".join(says) if says else "(nothing)"),
                fg_color=MUTED_COLOR,
            )
        ]
        for attribute in ("age", "name"):
            attestation = state["shown"][attribute]
            if attestation is not None:
                lines.append(colored(
                    f"{_VERIFY_LABELS[attribute].capitalize()} verified {_verified_by(attestation)}",
                    fg_color=MUTED_COLOR,
                ))
        if self_verify:
            lines.append(colored(SELF_ATTESTED_NOT_SHARED[0].upper() + SELF_ATTESTED_NOT_SHARED[1:] + ".", fg_color=MUTED_COLOR))
        return "\r\n".join(lines)

    def _verify_prompt(
        attribute: str, label: str, parse: Callable[[str], str],
    ) -> Callable[[Session, DatabaseLane, Draft], Awaitable[None]]:
        """One verified value into the draft: opened on what is there, Esc
        keeps it, and `parse` turns what was typed into the stored text or
        raises ValueError with the reason it was refused."""

        @inline_field
        async def prompt(session: Session, lane: DatabaseLane, draft: Draft) -> None:
            current = draft[attribute]
            await write_field_prompt(
                session, colored(f"{label} ({_VERIFY_FIELD_HINT}):", fg_color=MUTED_COLOR), hint=_VERIFY_FIELD_HINT,
            )
            try:
                raw = (await read_field_line(session, initial=current)).strip()
            except InputCancelled:
                await session.write_line("")
                return
            if raw == current:
                return
            if not raw:
                if _stored(attribute):
                    announce_line(
                        session,
                        f"Can't empty a verified {_VERIFY_LABELS[attribute]}: use [R]evoke to withdraw it. Unchanged.",
                    )
                    return
                draft[attribute] = ""
                return
            try:
                draft[attribute] = parse(raw)
            except ValueError as exc:
                announce_line(session, f"{exc} -- unchanged.")

        return prompt

    def _parse_birthdate(raw: str) -> str:
        try:
            birthdate = date.fromisoformat(raw)
        except ValueError:
            raise ValueError("Not a valid date (expected YYYY-MM-DD)") from None
        problem = birthdate_problem(birthdate)
        if problem is not None:
            raise ValueError(f"Can't verify {birthdate.isoformat()}: {problem}")
        return birthdate.isoformat()

    def _confirmation(changes: dict[str, str]) -> list[str]:
        parts = []
        if "age" in changes:
            parts.append(f"born {_verified_birthdate_text(changes['age'])}")
        if "name" in changes:
            parts.append(f"real name {sanitize_text(changes['name'])}")
        lines = [f"Record for {who}: {' and '.join(parts)}."]
        for attribute in changes:
            old = state["shown"][attribute]
            if old is None:
                continue
            old_value = (
                _verified_birthdate_text(old.attested_value) if attribute == "age" else sanitize_text(old.attested_value)
            )
            line = f"It replaces the verified {_VERIFY_LABELS[attribute]} {old_value}, {_verified_by(old, sharing=False)}"
            if old.link_visible:
                line += (
                    f", and switches off {'your' if self_verify else _possessive(subject)} sharing of it over Link"
                    + ("" if self_verify else " until they share the new one")
                )
            lines.append(line + ".")
        if self_verify:
            lines.append("A verification you make of yourself is not shared over Link.")
        return lines

    async def save(draft: Draft) -> None:
        changes = {attribute: draft[attribute] for attribute in ("age", "name") if draft[attribute] != _stored(attribute)}
        if not changes:
            return None
        for line in _confirmation(changes):
            await session.write_line(line)
        if not await prompt_yes_no(session, "Record it?", default=False):
            announce_line(session, "Cancelled. Nothing recorded; your changes are still here.")
            raise SaveDeclined()
        try:
            await lane.run(
                attest_identity, subject, verifier=verifier,
                birthdate=date.fromisoformat(changes["age"]) if "age" in changes else None,
                real_name=changes.get("name"),
                expected={attribute: state["shown"][attribute] for attribute in changes},
            )
        except AttestationChanged:
            # Show what is stored now above the draft, which stays for another look.
            state.update(await lane.run(_load_verify_state, subject))
            raise
        recorded = " and ".join(f"verified {_VERIFY_LABELS[attribute]}" for attribute in changes)
        announce_line(session, f"Recorded the {recorded}{' (self)' if self_verify else ''}.")
        return None

    async def _revoke(session: Session, lane: DatabaseLane) -> bool:
        await _revoke_one(session, lane, verifier, subject, header_color=header_color, unicode_style=unicode_style)
        return False

    async def refresh() -> DetailState:
        state.update(await lane.run(_load_verify_state, subject))
        actions = []
        # Issue #1115: whoever may verify may also withdraw a verification.
        if any(attestation is not None for attestation in state["shown"].values()):
            actions.append(DetailAction("r", menu_key("R", "evoke"), _revoke, brief="Withdraw a verification"))
        return DetailState(draft={"age": _stored("age"), "name": _stored("name")}, actions=actions)

    record_help = (
        " Nothing is recorded until [S]ave, which says what it replaces and asks once. A new value "
        "switches off the caller's sharing of the old one over Link until they share the new one. "
        "Emptying it here does not withdraw it: [R]evoke does."
    )
    fields = [
        FieldSpec(
            key="age", label="Verified birthdate",
            render=lambda d: _verified_birthdate_text(d["age"]) if d["age"] else "(not verified)",
            prompt=_verify_prompt("age", "Verified birthdate, YYYY-MM-DD", _parse_birthdate),
            brief="The date of birth you checked",
            help=(
                "The date of birth you checked, as YYYY-MM-DD. It decides every age gate on this node, "
                "whatever the caller entered as their own birthdate (shown above)." + record_help
            ),
        ),
        FieldSpec(
            key="name", label="Verified real name",
            render=lambda d: sanitize_text(d["name"]) if d["name"] else "(not verified)",
            prompt=_verify_prompt("name", "Verified real name", str),
            brief="The real name you checked",
            help=(
                "The real name you checked. Boards, areas and channels that require a verified name "
                "let the caller in with it, and some show it beside their posts." + record_help
            ),
        ),
    ]
    await edit_resource_draft(
        session, lane,
        title=title,
        fields=fields,
        draft={},
        save=save,
        error_type=AttestationError,
        save_menu_text=menu_key("S", "ave"),
        back_menu_text=menu_key("B", "ack"),
        description_level=await lane.run(menu_description_level, verifier),
        redraw_in_place=await lane.run(redraw_in_place_enabled, verifier),
        preamble=_preamble,
        unicode_style=unicode_style,
        collapsed=await lane.run(breadcrumb_collapsed_enabled, verifier),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=header_color,
        detail=DetailMode(refresh=refresh),
    )
