"""SysOp console banner, masthead and theme-colour screens.

The welcome, main-menu, logoff and new-account banners, the board-list,
file-area and chat-channel-picker mastheads, and the theme colours. Split
out of `netbbs.net.admin_flow` (issue #1311).
"""

from __future__ import annotations

import asyncio
import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from netbbs.rendering.menu import continue_prompt
from netbbs.auth.users import User
from netbbs.boards.boards import list_boards
from netbbs.boards.categories import list_top_level_categories as list_top_level_board_categories
from netbbs.chat.categories import list_top_level_categories as list_top_level_channel_categories
from netbbs.communities import meets_read_gate
from netbbs.files.areas import list_file_areas
from netbbs.files.categories import list_top_level_categories as list_top_level_file_categories
from netbbs.files.entries import count_listed_files
from netbbs.moderation.log import record_action
from netbbs.net.char_input import HELP_KEY, InputCancelled, help_key_label, reject_unhandled_key
from netbbs.net.confirm import prompt_yes_no
from netbbs.sysop_uploads import (
    BANNER as SYSOP_UPLOAD_BANNER,
    SysOpUploadTarget,
    destination_problem,
)
from netbbs.net.help_overlay import show_menu_help
from netbbs.net.location import crumbs, entered, located
from netbbs.net.resource_editor import (
    inline_field,
    write_field_prompt,
    write_field_message,
    FieldSpec,
)
from netbbs.net.session import (
    Session,
    physical_terminal_width,
    write_preformatted_line,
    write_prompt,
)
from netbbs.rendering.charset import ASCII, ellipsis_for
from netbbs.net.shutdown import NodeControls
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.color_depth_preference import effective_truecolor
from netbbs.net.node_theme import (
    accent_color_override,
    clock_color_override,
    effective_accent_color_256,
    effective_header_color_256,
    header_color_override,
    set_accent_color_override,
    set_clock_color_override,
    set_header_color_override,
)
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.net.ansi_editor import edit_ansi_art
from netbbs.net.art_pacing import (
    ART_SPEEDS,
    ART_TIME_LIMITS,
    MAIN_MENU_ART,
    WELCOME_ART,
    art_speed,
    art_time_limit,
    set_art_speed,
    set_art_time_limit,
    write_preview_art,
    write_preview_art_text,
)
from netbbs.net.banner_fields import banner_fields, count_callers_online
from netbbs.net.welcome_banner import (
    MAX_BANNER_SIZE_BYTES,
    banner_path,
    is_welcome_banner_credit_enabled,
    load_welcome_banner,
    pre_login_unicode_style,
    set_welcome_banner_credit_enabled,
    set_welcome_banner_enabled,
    welcome_banner_status,
)
from netbbs.net.banner_presets import (
    BOARD_LIST_MASTHEAD_PRESETS,
    CHAT_CHANNEL_PICKER_MASTHEAD_PRESETS,
    FILE_AREA_MASTHEAD_PRESETS,
    LOGOFF_BANNER_PRESETS,
    MAIN_MENU_BANNER_PRESETS,
    NEW_ACCOUNT_BANNER_AFTER_PRESETS,
    NEW_ACCOUNT_BANNER_BEFORE_PRESETS,
    WELCOME_BANNER_PRESETS,
    load_board_list_masthead_preset,
    load_chat_channel_picker_masthead_preset,
    load_file_area_masthead_preset,
    load_logoff_banner_preset,
    load_main_menu_banner_preset,
    load_new_account_banner_after_preset,
    load_new_account_banner_before_preset,
    load_welcome_banner_preset,
)
from netbbs.net.main_menu_banner import (
    MASTHEAD_MODE,
    MAX_MASTHEAD_SIZE_BYTES,
    SLOTS_MODE,
    is_main_menu_banner_enabled,
    load_main_menu_banner,
    main_menu_art_mode,
    main_menu_banner_path,
    main_menu_banner_status,
    set_main_menu_art_mode,
    set_main_menu_banner_enabled,
)
from netbbs.rendering.art_slots import (
    ListSlotRow,
    SlotArt,
    describe_items,
    banner_slot_notes,
    describe_slots,
    fill_field_slots,
    list_name_width,
    list_slot_fits,
    parse_slot_art,
    render_list_slot_art,
)
from netbbs.rendering.ansi import clear_screen, move_cursor
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
from netbbs.net.list_art import (
    BOARD_LIST,
    CHAT_CHANNEL_PICKER,
    FILE_AREA,
    list_art_mode,
    list_slot_fields,
    read_list_art,
    set_list_art_mode,
)
from netbbs.net.list_art import MASTHEAD_MODE as LIST_MASTHEAD_MODE
from netbbs.net.list_art import SLOTS_MODE as LIST_SLOTS_MODE
from netbbs.net.board_list_banner import (
    MAX_BOARD_LIST_BANNER_SIZE_BYTES,
    is_board_list_banner_enabled,
    board_list_banner_path,
    board_list_banner_status,
    load_board_list_banner,
    set_board_list_banner_enabled,
)
from netbbs.net.file_area_banner import (
    MAX_FILE_AREA_BANNER_SIZE_BYTES,
    is_file_area_banner_enabled,
    file_area_banner_path,
    file_area_banner_status,
    load_file_area_banner,
    set_file_area_banner_enabled,
)
from netbbs.net.chat_channel_picker_banner import (
    MAX_CHAT_CHANNEL_PICKER_BANNER_SIZE_BYTES,
    is_chat_channel_picker_banner_enabled,
    chat_channel_picker_banner_path,
    chat_channel_picker_banner_status,
    load_chat_channel_picker_banner,
    set_chat_channel_picker_banner_enabled,
)
from netbbs.rendering import (
    ACCENT_COLOR,
    AUTHOR_COLOR,
    CLOCK_COLOR,
    EMPHASIS_COLOR,
    ERROR_COLOR,
    HEADER_COLOR,
    LABEL_COLOR,
    METADATA_COLOR,
    MUTED_COLOR,
    RESET,
    SUCCESS_COLOR,
    VALUE_COLOR,
    WARNING_COLOR,
    MenuEntry,
    action_bar,
    colored,
    display_width,
    decode_banner_bytes,
    highlight_hotkeys,
    highlight_report,
    menu_key,
    nearest_256,
    sanitize_text,
    screen_title,
    status_badge,
    status_mark,
    wrap_to_width,
)
from netbbs.rendering.detail import Field, Section
from netbbs.rendering.sauce import Sauce, split_sauce
from netbbs.net import notices as _notices
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.net.admin_ui import (
    _announce,
    _announce_line,
    _choice_prompt,
    _fitted_menu,
    _format_bytes,
    _menu_row,
    _receive_sysop_upload,
    _remove_file_entry,
    _upload_unavailable_reason,
    _write_sections,
    _write_wrapped_subtitle,
    edit_resource_draft,
    pick_item,
)
from netbbs.net.admin_fields import _EDIT_HINT, _read_seeded_line, _resolve_redraw_preference


def _announce_saved(session: Session, lead: str, path: object, tail: str) -> None:
    """An outcome that names the file it saved: the success mark, `lead`
    and `tail` in the normal colours (issue #1109), the path in the value
    colour so it reads as the thing saved, and a key the tail mentions
    highlighted (issue #1083)."""
    _notices.announce_styled(
        session,
        status_mark("success") + lead + colored(str(path), fg_color=VALUE_COLOR) + highlight_hotkeys(tail),
    )


async def _write_counted(session: Session, text: str) -> int:
    """Write `text` and return the rows it leaves on screen, counting each
    line break after the last clear -- for a screen that fits its menu under
    what it has drawn (issue #662). A break before a clear is erased with it;
    counting it pushed the main-menu masthead's described menu, which fits
    80x24 to the row, into the packed one."""
    await session.write_line(text)
    return len(text.rsplit(clear_screen(), 1)[-1].split("\r\n"))


async def _write_banner_menu(
    session: Session, options: list[MenuEntry], description_level: str, *, used_rows: int, help_text: str,
) -> None:
    """A banner or masthead screen's menu and its help line, fitted under the
    `used_rows` already on screen (issue #662). Built with `_menu_row`, these
    screens budgeted the described menu against the whole terminal, so below
    72 columns, where `menu_grid` lays it out one entry per two rows, the
    screen's own title scrolled away before Choice: appeared."""
    help_text = help_text.replace("Ctrl-H", help_key_label(session))  # issue #1119
    help_rows = len(wrap_to_width(help_text, session.terminal_width)) or 1
    await session.write_line(
        "\r\n" + _fitted_menu(options, description_level, session=session, used_rows=used_rows + 1 + help_rows)
    )
    await session.write_line(colored(help_text, fg_color=MUTED_COLOR))


def _banner_status_section(
    status,
    *,
    unicode_style: bool,
    sauce: Sauce | None = None,
    credit_line: bool | None = None,
    too_wide: str = "no art",
    speed: int | None = None,
) -> Section:
    """Whether a banner or masthead is switched on, and the state of the file
    behind it -- the two facts every one of these seven menus leads with. They
    were one run-on line (`disabled -- file: x.ans (missing)`); a missing file
    behind an enabled banner is the case worth seeing, and now reads in red
    on a row of its own.

    A file with a SAUCE record (issue #929) also shows its credit, the width
    it was drawn for, and a warning when it was made for a font other than
    CP437's; the caller reads the record off the event loop
    (`_banner_sauce`). `too_wide` is what a caller whose terminal is
    narrower than the art sees instead. `credit_line` is the welcome
    banner's caller-facing credit setting, shown when given. `speed` is the
    art's playback speed (issue #929), shown when the screen has a `[S]peed`
    entry: its description alone could be packed away by a fitted menu
    (issue #662), and a SysOp who pressed it would not see what it is now.
    The `[T]ime limit` beside it needs no row here: its label, which a
    fitted menu keeps, shows its state."""
    fields = [
        Field(
            "Shown to callers",
            status_badge("ENABLED", tone="success", unicode_style=unicode_style) if status.enabled
            else status_badge("DISABLED", tone="neutral", unicode_style=unicode_style),
            styled=True,
        ),
        Field("File", status.path.name, color=METADATA_COLOR),
        Field("On disk", _format_bytes(status.size_bytes), color=VALUE_COLOR) if status.exists
        else Field("On disk", "missing", color=ERROR_COLOR if status.enabled else MUTED_COLOR),
    ]
    if sauce is not None:
        # Title and artist apart (issue #1083), each a plain field so the
        # panel wraps it: a styled field is never wrapped, and a SAUCE
        # credit can run to 80 columns (review on #1095).
        title, artist = _credit_parts(sauce)
        if title:
            fields.append(Field("Art", title, color=EMPHASIS_COLOR))
        if artist:
            fields.append(Field("By", artist, color=AUTHOR_COLOR))
        if not (title or artist):
            fields.append(Field("Art", "(no credit in its SAUCE record)", color=MUTED_COLOR))
        if sauce.width is not None:
            fields.append(Field("Drawn for", f"{sauce.width} columns; narrower terminals get {too_wide}", color=VALUE_COLOR))
        if not sauce.font_is_cp437:
            font = "".join(ch for ch in sauce.font if ch.isprintable())
            fields.append(Field("Font", f"made for {font}; shown with CP437's characters", color=WARNING_COLOR))
    if speed is not None:
        fields.append(Field("Speed", _art_speed_brief(speed), color=VALUE_COLOR if speed else MUTED_COLOR))
    if credit_line is not None:
        fields.append(Field("Credit line", "shown under the banner" if credit_line else "off", color=VALUE_COLOR if credit_line else MUTED_COLOR))
    return Section("Status", fields)


def _credit_parts(sauce: Sauce) -> tuple[str, str]:
    """A SAUCE record's title, and its author and group as one "artist"
    ("InkWell/Nib and Quill"), each printable and sanitized; either may be
    empty."""
    def clean(text: str) -> str:
        return sanitize_text("".join(ch for ch in text if ch.isprintable()))

    who = "/".join(part for part in (clean(sauce.author), clean(sauce.group)) if part)
    return clean(sauce.title), who


def _toggle_welcome_banner_credit(db: Database, actor: User) -> bool:
    """Flip whether callers see the art's credit under the welcome banner,
    audited; returns the new setting."""
    shown = not is_welcome_banner_credit_enabled(db)
    set_welcome_banner_credit_enabled(db, shown)
    record_action(db, actor=actor, action="set_welcome_banner_credit", detail="on" if shown else "off")
    return shown


def _banner_sauce(status) -> Sauce | None:
    """The SAUCE record of the file behind `status`, or `None` when there is
    no file, it is over the size limit, or it has no record."""
    if not status.exists or (status.size_bytes or 0) > _SAVED_BANNER_READ_LIMIT:
        return None
    try:
        return split_sauce(status.path.read_bytes())[1]
    except OSError:
        return None


@located("Mastheads & banners")
async def _banners_and_mastheads_menu(
    session: Session, lane: DatabaseLane, actor: User, *, node_controls: NodeControls | None = None
) -> None:
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_banners_and_mastheads_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "s":
            await session.write_line("")
            await _banners_menu(session, lane, actor, node_controls=node_controls)
            await _draw_banners_and_mastheads_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "m":
            await session.write_line("")
            await _mastheads_menu(session, lane, actor)
            await _draw_banners_and_mastheads_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == HELP_KEY:
            await session.write_line("")
            await _banner_router_help_screen(
                session, "Mastheads & banners help", _ROUTER_MENU,
                "Every piece of art and text you can add to the caller's screens. Mastheads sit above "
                "the main menu and the board, file-area and channel lists, or replace them; session "
                "banners greet callers, see them off and frame signup.",
                header_color=header_color, unicode_style=unicode_style,
            )
            await _draw_banners_and_mastheads_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_banners_and_mastheads_menu(
    session: Session, lane: DatabaseLane, description_level: str, redraw_in_place: bool,
    unicode_style: bool, collapsed: bool, header_color: int | tuple[int, int, int],
) -> None:
    await session.write_line("\r\n" + screen_title("Mastheads & banners",
            breadcrumb=crumbs(session, own=True), width=session.terminal_width, clear=redraw_in_place,
            unicode_style=unicode_style, collapsed=collapsed, header_color=header_color,
            node_name_gradient=session.node_name_gradient))
    await _write_wrapped_subtitle(
        session, "Every optional SysOp-authored banner and masthead, grouped by kind."
    )
    await session.write_line(
        "\r\n" + _menu_row(
            list(_ROUTER_MENU),
            description_level,
            width=session.terminal_width,
            height=session.terminal_height,
        )
    )
    await session.write_line(colored(f"({help_key_label(session)} for help placing your own .ans files)", fg_color=MUTED_COLOR))
    await _choice_prompt(session)


def _art_speed_brief(speed: int) -> str:
    """The `[S]peed` entry's description (issue #929): the speed now set."""
    return f"Plays at {speed} bps" if speed else "Drawn at once"


async def _cycle_art_speed(lane: DatabaseLane, actor: User, kind: str) -> None:
    """`[S]peed` (issue #929): the next of `ART_SPEEDS` for this art -- off,
    2400, 9600, 38400 bps, then off again. The menu shows the new speed."""
    current = await lane.run(art_speed, kind)
    following = ART_SPEEDS[(ART_SPEEDS.index(current) + 1) % len(ART_SPEEDS)]

    def apply(db: Database) -> None:
        set_art_speed(db, kind, following)
        record_action(db, actor=actor, action="set_art_speed", detail=f"{kind}={following}")

    await lane.run(apply)


def _art_time_limit_entry(limit: int) -> MenuEntry:
    """The `[T]ime limit` toggle on an art screen with a `[S]peed`: it
    names its setting and shows its state, so a fitted menu that packs the
    descriptions away (issue #662) still shows it."""
    state = f"{limit} s" if limit else "off"
    return MenuEntry(label=menu_key("T", f"ime limit: {state}"), brief="Skip the rest after a while")


async def _cycle_art_time_limit(lane: DatabaseLane, actor: User, kind: str) -> None:
    """`[T]ime limit`: the next of `ART_TIME_LIMITS` for this art -- off,
    10, 30, 60 seconds, then off again. Past it, the rest of a paced draw
    goes out at once; off, the art plays to the end."""
    current = await lane.run(art_time_limit, kind)
    following = ART_TIME_LIMITS[(ART_TIME_LIMITS.index(current) + 1) % len(ART_TIME_LIMITS)]

    def apply(db: Database) -> None:
        set_art_time_limit(db, kind, following)
        record_action(db, actor=actor, action="set_art_time_limit", detail=f"{kind}={following}")

    await lane.run(apply)


# Every banner and masthead file has the same 256 KiB limit.
_SAVED_BANNER_READ_LIMIT = MAX_BANNER_SIZE_BYTES


def _saved_banner_art(status) -> str | None:
    """The art in a banner's saved file, whether or not it is switched on,
    or `None` when there is no readable file within the size limit."""
    if not status.exists or (status.size_bytes or 0) > _SAVED_BANNER_READ_LIMIT:
        return None
    try:
        data = status.path.read_bytes()
    except OSError:
        return None
    return decode_banner_bytes(data) + RESET


async def _write_banner_not_live(session: Session, status, *, callers_see: str) -> bool:
    """A preview's answer when callers are not seeing the SysOp's own art
    here (issue #841). A saved file that is switched off is shown anyway,
    because right after saving it the SysOp wants to see their work, not
    what callers get instead; the line under it says it is off and what
    callers see meanwhile. Returns whether saved art was shown.

    This replaces lines like "(no banner -- enabled=False, file
    exists=True)", which read as developer output."""
    art = _saved_banner_art(status)
    if art is not None and not status.enabled:
        await write_preformatted_line(session, art)
        await session.write_line(
            colored(
                f"(Saved, but switched off: callers see {callers_see}. [E]nable turns it on.)",
                fg_color=MUTED_COLOR,
            )
        )
        return True
    if status.exists and art is None:
        message = f"(The saved file is over 256 KiB or can't be read: callers see {callers_see}.)"
    elif status.enabled:
        message = f"(Switched on, but no file is saved: callers see {callers_see}.)"
    else:
        message = f"(Nothing saved yet: callers see {callers_see}.)"
    await session.write_line(colored(message, fg_color=MUTED_COLOR))
    return False


async def _preview_welcome_banner_screen(
    session: Session, lane: DatabaseLane, actor: User, *, node_controls: NodeControls | None = None
) -> None:
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
        # As the connecting caller on this transport gets it (issue #841).
        banner = load_welcome_banner(
            db, truecolor=truecolor, unicode_style=pre_login_unicode_style(session),
            max_width=physical_terminal_width(session),
        )
        fields = banner_fields(db, callers_online=count_callers_online(
            node_controls.session_registry if node_controls is not None else None
        ))
        return welcome_banner_status(db), banner, fields, truecolor

    status, banner_text, fields, _truecolor = await lane.run(_load)
    # As a connecting caller gets it, field slots filled with what is true
    # before sign-in (issue #1057): the raw tokens were never what callers saw.
    raw_text = banner_text
    # Width, ellipsis and fields exactly as `login_flow` draws it (review
    # of #1060): a terminal that wraps at once lays screens out a column
    # narrower than the art it is sent.
    banner_text = fill_field_slots(
        banner_text, fields, ellipsis=ellipsis_for(session), width=physical_terminal_width(session),
    )
    await session.write_line(colored("\r\nPreviewing the welcome banner callers see when they connect:", fg_color=MUTED_COLOR))
    # At the speed callers get it (issue #1083 finding 9).
    speed = await lane.run(art_speed, WELCOME_ART)
    limit = await lane.run(art_time_limit, WELCOME_ART)
    if status.enabled and status.exists and (status.size_bytes or 0) <= MAX_BANNER_SIZE_BYTES:
        await write_preview_art(session, banner_text, speed=speed, limit=limit)
        await session.write_line(colored("(Your banner, as callers see it.)", fg_color=MUTED_COLOR))
        await _write_banner_slot_notes(session, raw_text)
    elif not await _write_banner_not_live(session, status, callers_see="the default NetBBS banner"):
        # Nothing of the SysOp's own to show: show what callers do see.
        await write_preview_art(session, banner_text, speed=speed, limit=limit)
    # Dogfood report: this screen used to fall straight through to the
    # menu's own immediate redraw, which -- with redraw_in_place on
    # (the default for new accounts, issue #160's own follow-up)
    # cleared the just-printed preview off the screen before it could
    # actually be read, sometimes in well under a second. Same
    # present-then-wait shape `netbbs.net.help_overlay.show_help`
    # already uses for the identical reason.
    await session.write_line(continue_prompt())
    await session.read_any_key()


async def _write_banner_slot_notes(session: Session, banner_text: str) -> None:
    """Under a welcome or log-off banner's preview: each token callers see
    blank rather than filled (issue #1057), so a SysOp who drew a
    `{menu}` into a banner learns it here, not from a caller."""
    for note in banner_slot_notes(banner_text, width=physical_terminal_width(session)):
        await session.write_line(colored(f"Note: {note}.", fg_color=WARNING_COLOR))


async def _preview_apply_choice(session: Session, label: str) -> bool:
    """After a gallery/filesystem preview (issue #282): `[A]pply` or
    `[B]ack` to the list, instead of a yes/no whose "no" then also needed
    a "Press any key" to get back -- two keystrokes per declined preset
    while shopping through a gallery. Returns whether to apply."""
    async def _ask() -> None:
        await session.write_line(
            "\r\n" + action_bar([menu_key("A", "pply"), menu_key("B", "ack to the list")], width=session.terminal_width, height=session.terminal_height)
        )
        await write_prompt(session, f"{label}? ")

    await _ask()
    while True:
        choice = (await session.read_key()).lower()
        if choice == HELP_KEY:
            await show_menu_help(
                session, "Preview help",
                [MenuEntry(label=menu_key("A", "pply"), brief="Use what the preview showed"),
                 MenuEntry(label=menu_key("B", "ack to the list"), brief="Leave it unchanged and pick again")],
                about="The preview above is what callers would see. Nothing changes unless you apply it.",
            )
            await _ask()
            continue
        if choice in ("a", "b"):
            # A caller who habitually types "A<Enter>" must not carry that
            # Enter into the success pause or back into the picker (where
            # Enter activates the highlighted entry) -- the same drain the
            # yes/no primitive this replaces performs (Codex review on #291).
            discard_buffered_enter = getattr(session, "discard_buffered_enter", None)
            if discard_buffered_enter is not None:
                await discard_buffered_enter()
            await session.write_line("")
            return choice == "a"
        await session.write(reject_unhandled_key(choice))


async def _remove_banner_file(
    session: Session, lane: DatabaseLane, actor: User, *,
    status_of, set_enabled, label: str, audit_action: str,
) -> None:
    """Delete a banner's or masthead's saved file (issue #1119): until now a
    SysOp could only switch one off, and a file that should be gone stayed
    on disk. One yes/no, then the file goes and the piece is switched off
    with it -- left on, it would read "switched on, but no file is saved".
    Its mode and speed stay: they describe how art is shown, and apply
    again to whatever file comes next."""
    status = await lane.run(status_of)
    if not status.exists:
        _announce(session, f"No file is saved for {label}.", color=MUTED_COLOR)
        return
    if not await prompt_yes_no(session, f"Delete {status.path}? It can't be brought back.", default=False):
        _announce_line(session, "Cancelled. Nothing was deleted.")
        return

    def _apply(db: Database) -> tuple[bool, bool]:
        was_enabled = status_of(db).enabled
        try:
            status.path.unlink()
        except FileNotFoundError:
            return False, was_enabled
        if was_enabled:
            set_enabled(db, False)
        record_action(db, actor=actor, action=audit_action, detail=str(status.path))
        return True, was_enabled

    try:
        removed, was_enabled = await lane.run(_apply)
    except OSError as exc:
        _announce(session, f"Could not delete {status.path}: {exc.strerror or exc}", error=True)
        return
    if not removed:
        _announce(session, f"No file is saved for {label}.", color=MUTED_COLOR)
        return
    switched = " and switched it off" if was_enabled else ""
    _announce_line(session, f"Deleted {status.path}{switched}. Callers see what they would with no file.")


async def _upload_banner_piece(
    session: Session, lane: DatabaseLane, actor: User, *,
    path_of: Callable[[Database], Path], label: str, audit_action: str,
) -> None:
    """[U]pload on a banner or masthead screen (issue #728): one `.ans` file
    from the SysOp's own computer, written to exactly this piece's file.

    Never enables the piece. Replacing an existing file is confirmed first,
    because an enabled piece shows the new file to callers at once."""
    reason = _upload_unavailable_reason(session)
    if reason is not None:
        _announce(session, reason, error=True)
        return
    def _lexical(db: Database) -> Path:
        # The piece helpers return a `.resolve()`d path, which has already
        # followed a symlink at the piece's own name -- so the refusal below
        # could never see one. Rebuild the name without following it: the
        # database's directory, resolved, plus the piece's file name.
        return db.path.parent.resolve() / _BANNER_FILE_SUFFIX_OF[path_of](db.path.stem)

    destination = await lane.run(_lexical)
    problem = destination_problem(destination)
    if problem is not None:
        _announce(session, problem, error=True)
        return
    if destination.exists() and not await prompt_yes_no(
        session,
        f"Replace the current {destination.name}? If {label} is enabled, callers see the new one at once",
        default=False,
    ):
        _announce(session, "Nothing uploaded.")
        return
    target = SysOpUploadTarget(
        kind=SYSOP_UPLOAD_BANNER, destination=destination, max_bytes=MAX_BANNER_SIZE_BYTES,
        label=label, audit_action=audit_action, replaces=destination.exists(),
    )
    await _receive_sysop_upload(
        session, lane, actor, target, then="Use [P]review, then [E]nable if it is not on yet.",
    )


def _browsable_ans_files(directory: Path, *, exclude: Path) -> list[Path]:
    """Every `.ans` file in `directory` except `exclude` (the picker's
    own current target -- loading it onto itself is a pointless option,
    not a real choice). No traversal risk here: `directory.glob` only
    ever returns real entries NetBBS itself enumerated, never a
    caller-typed path, so there's nothing for a `..`-style escape to
    reach in the first place (issue #170's own locked design)."""
    return sorted(p for p in directory.glob("*.ans") if p.is_file() and p != exclude)


#: What a banner's or masthead's keys change, by key, for its help screen.
_SLOT_KEY_HELP = {
    "p": "As callers would get it now, filled in",
    "s": "2400, 9600 or 38400 bps, or at once; a key skips",
    "t": "After 10, 30 or 60 s the rest is drawn at once",
    "m": "Above the screen, or the screen with live slots",
    "c": "Its slots, its problems and whether it fits",
    "e": "Callers get it the next time it is due",
    "d": "Callers stop getting it; the file stays",
    "o": "Edit the file in the ANSI art editor",
    "g": "Preview a sample; Apply overwrites and enables",
    "f": "Other .ans files beside it; Load overwrites",
    "u": "From your computer, replacing the file",
    "r": "Deletes the file for good and turns it off",
}


_ROUTER_MENU = (
    MenuEntry(label=menu_key("M", "astheads"), brief="Art above the menu and pickers"),
    MenuEntry(label=menu_key("S", "ession banners"), brief="Welcome, logoff, new-account text"),
    MenuEntry(label=menu_key("B", "ack"), brief="Return to Settings"),
)
_SESSION_BANNER_MENU = (
    MenuEntry(label=menu_key("W", "elcome banner"), brief="Art shown when a caller connects"),
    MenuEntry(label=menu_key("L", "ogoff banner"), brief="Shown on an intentional Log off"),
    MenuEntry(label=menu_key("P", "re-signup"), brief="Shown once, before Create account"),
    MenuEntry(label=menu_key("A", "fter signup"), brief="Shown once signup succeeds"),
    MenuEntry(label=menu_key("N", "ew-account letter"), brief="Mail every new account finds"),
    MenuEntry(label=menu_key("B", "ack"), brief="Return to Mastheads & banners"),
)
_MASTHEAD_MENU = (
    MenuEntry(label=menu_key("M", "ain menu"), brief="Above the menu, or as the menu"),
    MenuEntry(label=menu_key("D", "iscussion boards"), brief="Above the list, or as the list"),
    MenuEntry(label=menu_key("F", "ile areas"), brief="Above the list, or as the list"),
    MenuEntry(label=menu_key("C", "hat channels"), brief="Above the picker, or as the list"),
    MenuEntry(label=menu_key("B", "ack"), brief="Return to Mastheads & banners"),
)


async def _banner_router_help_screen(
    session: Session, title: str, entries, about: str, *,
    header_color: int | tuple[int, int, int], unicode_style: bool,
) -> None:
    """Ctrl-H on the three navigation screens above the banners and
    mastheads: their keys, and where each piece's own help says its file
    goes, since none of these owns a file itself. Dogfood report:
    a SysOp reasonably tries Ctrl-H at the first screen they land on, not
    only at a specific banner's own menu, and got silently nothing here
    -- worse than no hint at all, since every leaf screen one level down
    does answer Ctrl-H."""
    await show_menu_help(
        session, title, entries,
        about=f"{about} Each has its own .ans file; {help_key_label(session)} on its screen says where it goes.",
        header_color=header_color, unicode_style=unicode_style,
    )


async def _toggle_main_menu_art_mode(session: Session, lane: DatabaseLane, actor: User) -> None:
    """[M]ode (issue #929, step 4): the art above the main menu, or the
    art as the menu itself with live slots."""

    def _apply(db: Database) -> str:
        mode = SLOTS_MODE if main_menu_art_mode(db) == MASTHEAD_MODE else MASTHEAD_MODE
        set_main_menu_art_mode(db, mode)
        record_action(db, actor=actor, action="set_main_menu_art_mode", detail=mode)
        return mode

    mode = await lane.run(_apply)
    if mode == SLOTS_MODE:
        _announce_line(session, "The art is now the main menu itself. Use [C]heck to see whether the menu fits it.")
    else:
        _announce_line(session, "The art is now shown above the main menu.")


def _read_slot_art(db: Database) -> SlotArt | None:
    path = main_menu_banner_path(db)
    try:
        if not path.exists() or path.stat().st_size > MAX_MASTHEAD_SIZE_BYTES:
            return None
        return parse_slot_art(decode_banner_bytes(path.read_bytes()))
    except OSError:
        return None


async def _check_main_menu_slot_art_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """[C]heck (issue #929, steps 4 and 5): the slots and hand-drawn items
    found in the art, what makes it unusable, and whether your own menu and
    a level-0 caller's fit on a terminal the size of yours -- which drawn
    items each of them has blanked, and which of their items go into the
    `{menu}` slot. A menu that doesn't fit isn't cut: those callers get the
    generated menu instead."""
    # Imported here: netbbs.net.main_menu imports this module.
    from netbbs.net.main_menu import MAIN_MENU_KEYS, slot_menu_preview

    art = await lane.run(_read_slot_art)
    enabled, mode = await lane.run(lambda db: (is_main_menu_banner_enabled(db), main_menu_art_mode(db)))
    await session.write_line(colored("\r\nChecking the main-menu art for slots:", fg_color=MUTED_COLOR))
    # Enabled and mode are separate switches: [D]isable leaves the mode
    # alone, so say plainly when callers won't see this art at all.
    if not enabled:
        await session.write_line(highlight_hotkeys(
            "Callers don't see this art yet: it's switched off. Use [E]nable.", color=WARNING_COLOR
        ))
    if mode != SLOTS_MODE:
        await session.write_line(highlight_hotkeys(
            "Callers see this above the menu, not as it: [M]ode changes that.",
            color=WARNING_COLOR,
        ))
    if art is None:
        await session.write_line("No usable art file. Apply a gallery sample, upload one, or draw one first.")
    else:
        await session.write_line(highlight_report(f"Art: {art.width} columns, {art.height} rows.", color=VALUE_COLOR))
        for line in describe_slots(art) or ["No slots found."]:
            await session.write_line(highlight_report(f"  {line}", color=VALUE_COLOR))
        if art.items:
            await session.write_line("Drawn items:")
            for item, line in zip(art.items, describe_items(art)):
                if any(key in MAIN_MENU_KEYS for key in item.keys):
                    await session.write_line(f"  {line}")
                else:
                    await session.write_line(colored(
                        f"  {line}: not a main-menu key, so it stays as drawn for everyone",
                        fg_color=WARNING_COLOR,
                    ))
        for note in art.notes:
            await session.write_line(colored(f"  Note: {note}", fg_color=WARNING_COLOR))
        if art.problems:
            for problem in art.problems:
                await session.write_line(colored(f"  Problem: {problem}", fg_color=ERROR_COLOR))
        else:
            for label, level in (("Your menu", None), ("A level-0 caller's menu", 0)):
                plan = await lane.run(lambda db, level=level: slot_menu_preview(session, db, actor, art, level=level))
                if plan.text is not None:
                    await session.write_line(_fits_line(label, ""))
                    for line in _drawn_item_outcome(plan):
                        await session.write_line(f"    {line}")
                else:
                    await session.write_line(
                        colored(f"  {label}: generated menu instead -- {plan.reason}.", fg_color=WARNING_COLOR)
                    )
    await session.write_line(continue_prompt())
    await session.read_any_key()


def _fits_line(label: str, details: str) -> str:
    """A check's "fits" verdict (issue #1083): the verdict in the success
    colour and the numbers after it emphasised, not one flat green line."""
    return (
        colored(f"  {label}: ", fg_color=VALUE_COLOR) + colored("fits", fg_color=SUCCESS_COLOR, bold=True)
        + highlight_report(f"{details}.", color=VALUE_COLOR)
    )


def _drawn_item_outcome(plan) -> list[str]:
    """What a fitting slot-menu plan does with hand-drawn items (#929, step
    5): which it blanks for that caller, and which of their items go into
    the `{menu}` slot because the art doesn't draw them."""
    lines = []
    if plan.hidden_keys:
        keys = ", ".join(f"[{key.upper() if key.isalpha() else key}]" for key in plan.hidden_keys)
        lines.append(f"Blanked, as this caller can't use them: {keys}")
    if plan.overflow:
        lines.append(f"In the {{menu}} slot, as the art doesn't draw them: {', '.join(plan.overflow)}")
    return lines


async def _write_slot_art_preview(
    session: Session, lane: DatabaseLane, actor: User, art: SlotArt, *, level: int | None
) -> None:
    from netbbs.net.main_menu import slot_menu_preview

    plan = await lane.run(lambda db: slot_menu_preview(session, db, actor, art, level=level))
    if plan.text is None:
        await session.write_line(
            colored(f"Callers like this get the generated menu instead: {plan.reason}.", fg_color=WARNING_COLOR)
        )
        return
    await write_preview_art_text(
        session, plan.text,
        speed=await lane.run(art_speed, MAIN_MENU_ART), limit=await lane.run(art_time_limit, MAIN_MENU_ART),
    )
    await session.write(move_cursor(art.height + 1, 1))


async def _preview_main_menu_slot_art(session: Session, lane: DatabaseLane, actor: User) -> None:
    """[P]review in slots mode: the main menu as you see it, then as a
    level-0 caller sees it."""
    art = await lane.run(_read_slot_art)
    if art is None:
        await session.write_line(colored("\r\nNo usable art file to preview.", fg_color=MUTED_COLOR))
        await session.write_line(continue_prompt())
        await session.read_any_key()
        return
    # [D]isable leaves the mode alone, so a switched-off banner still lands
    # here: say under each draw that callers get the plain menu meanwhile.
    enabled = await lane.run(is_main_menu_banner_enabled)
    for number, (intro, level) in enumerate((("as you see it", None), ("as a level-0 caller sees it", 0)), 1):
        # The art clears the screen, so what is being shown is said below it.
        await _write_slot_art_preview(session, lane, actor, art, level=level)
        if not enabled:
            await session.write_line(highlight_hotkeys(
                "Callers don't see this art yet: it's switched off. Use [E]nable.", color=WARNING_COLOR
            ))
        await session.write_line(colored(f"(the main menu {intro}, {number} of 2)  ", fg_color=MUTED_COLOR) + continue_prompt())
        await session.read_any_key()


@located("Session banners")
async def _banners_menu(
    session: Session, lane: DatabaseLane, actor: User, *, node_controls: NodeControls | None = None
) -> None:
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_banners_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "w":
            await session.write_line("")
            await _slot_menu(session, lane, actor, _WELCOME, node_controls=node_controls)
            await _draw_banners_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "l":
            await session.write_line("")
            await _slot_menu(session, lane, actor, _LOGOFF, node_controls=node_controls)
            await _draw_banners_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "p":
            await session.write_line("")
            await _slot_menu(session, lane, actor, _NEW_ACCOUNT_BEFORE)
            await _draw_banners_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "a":
            await session.write_line("")
            await _slot_menu(session, lane, actor, _NEW_ACCOUNT_AFTER)
            await _draw_banners_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "n":
            from netbbs.net.admin_welcome_letter import welcome_letter_screen

            await session.write_line("")
            await welcome_letter_screen(session, lane, actor)
            await _draw_banners_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == HELP_KEY:
            await session.write_line("")
            await _banner_router_help_screen(
                session, "Session banners help", _SESSION_BANNER_MENU,
                "Banners shown at points in a caller's session, each off until you enable it, and the "
                "letter mailed to every new account.",
                header_color=header_color, unicode_style=unicode_style,
            )
            await _draw_banners_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_banners_menu(
    session: Session, lane: DatabaseLane, description_level: str, redraw_in_place: bool,
    unicode_style: bool, collapsed: bool, header_color: int | tuple[int, int, int],
) -> None:
    await session.write_line("\r\n" + screen_title("Session banners",
            breadcrumb=crumbs(session, own=True), width=session.terminal_width, clear=redraw_in_place,
            unicode_style=unicode_style, collapsed=collapsed, header_color=header_color,
            node_name_gradient=session.node_name_gradient))
    await _write_wrapped_subtitle(
        session,
        "Optional banners shown throughout a caller's session -- first login, signing off, "
        "and starting/finishing self-service signup -- and the letter waiting in a new account's mailbox.",
    )
    await session.write_line(
        "\r\n" + _menu_row(
            list(_SESSION_BANNER_MENU),
            description_level,
            width=session.terminal_width,
            height=session.terminal_height,
        )
    )
    await session.write_line(colored(f"({help_key_label(session)} for help placing your own .ans files)", fg_color=MUTED_COLOR))
    await _choice_prompt(session)


async def _preview_logoff_banner_screen(
    session: Session, lane: DatabaseLane, actor: User | None = None, *, node_controls: NodeControls | None = None
) -> None:
    status, banner_text, fields = await lane.run(lambda db: (
        logoff_banner_status(db), load_logoff_banner(db, max_width=physical_terminal_width(session)),
        banner_fields(db, user=actor, callers_online=count_callers_online(
            node_controls.session_registry if node_controls is not None else None
        )),
    ))
    await session.write_line(colored("\r\nPreviewing logoff banner as shown on Log off:", fg_color=MUTED_COLOR))
    if banner_text:
        # Filled as your own Log off would fill it (issue #1057).
        await write_preformatted_line(session, fill_field_slots(
            banner_text, fields, ellipsis=ellipsis_for(session), width=physical_terminal_width(session),
        ))
        await _write_banner_slot_notes(session, banner_text)
    else:
        await _write_banner_not_live(session, status, callers_see="no banner")
    await session.write_line(continue_prompt())
    await session.read_any_key()


@located("Mastheads")
async def _mastheads_menu(session: Session, lane: DatabaseLane, actor: User) -> None:
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    await _draw_mastheads_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "m":
            await session.write_line("")
            await _slot_menu(session, lane, actor, _MAIN_MENU)
            await _draw_mastheads_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "d":
            await session.write_line("")
            await _slot_menu(session, lane, actor, _BOARD_LIST)
            await _draw_mastheads_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "f":
            await session.write_line("")
            await _slot_menu(session, lane, actor, _FILE_AREA)
            await _draw_mastheads_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "c":
            await session.write_line("")
            await _slot_menu(session, lane, actor, _CHAT_CHANNEL_PICKER)
            await _draw_mastheads_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == HELP_KEY:
            await session.write_line("")
            await _banner_router_help_screen(
                session, "Mastheads help", _MASTHEAD_MENU,
                "Art for the main menu and the three browsing lists, at every level (top, a category, a "
                "Community). Each is drawn above its menu or list, or, with Mode on its screen, is the "
                "menu or list itself with live slots.",
                header_color=header_color, unicode_style=unicode_style,
            )
            await _draw_mastheads_menu(session, lane, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


async def _draw_mastheads_menu(
    session: Session, lane: DatabaseLane, description_level: str, redraw_in_place: bool,
    unicode_style: bool, collapsed: bool, header_color: int | tuple[int, int, int],
) -> None:
    await session.write_line("\r\n" + screen_title("Mastheads",
            breadcrumb=crumbs(session, own=True), width=session.terminal_width, clear=redraw_in_place,
            unicode_style=unicode_style, collapsed=collapsed, header_color=header_color,
            node_name_gradient=session.node_name_gradient))
    await _write_wrapped_subtitle(
        session,
        "Optional mastheads for the main menu, message-board list, file-area list, and "
        "chat channel picker -- at every level of browsing (top level, a category, a "
        "Community) where applicable. Each is shown above the menu or list, or, under its "
        "[M]ode, as the menu or list itself.",
    )
    await session.write_line(
        "\r\n" + _menu_row(
            list(_MASTHEAD_MENU),
            description_level,
            width=session.terminal_width,
            height=session.terminal_height,
        )
    )
    await session.write_line(colored(f"({help_key_label(session)} for help placing your own .ans files)", fg_color=MUTED_COLOR))
    await _choice_prompt(session)


# -- list screens as art (issue #929) --------------------------------------
#
# The Boards, file areas and Chat channels lists can use their masthead art
# as the list itself, with a page drawn into its {list WxH} region
# (`netbbs.net.list_art`, `pick_item`'s `slot_art`). These are the three
# masthead screens' [M]ode, [C]heck and slots-mode [P]review.

_LIST_ART_SCREENS = {
    BOARD_LIST: ("board list", board_list_banner_path, is_board_list_banner_enabled, MAX_BOARD_LIST_BANNER_SIZE_BYTES),
    FILE_AREA: ("file-area list", file_area_banner_path, is_file_area_banner_enabled, MAX_FILE_AREA_BANNER_SIZE_BYTES),
    CHAT_CHANNEL_PICKER: (
        "channel list", chat_channel_picker_banner_path, is_chat_channel_picker_banner_enabled,
        MAX_CHAT_CHANNEL_PICKER_BANNER_SIZE_BYTES,
    ),
}


def _list_art_mode_line(mode: str) -> str:
    if mode == LIST_MASTHEAD_MODE:
        return "Mode: above the list. The art is shown above the list, which stays live underneath it."
    return (
        "Mode: the list itself. A page of entries is drawn into the art's {list} slot, "
        "and its {title}, {page} and {count} slots are filled in."
    )


async def _toggle_list_art_mode(session: Session, lane: DatabaseLane, actor: User, kind: str) -> None:
    what = _LIST_ART_SCREENS[kind][0]

    def _apply(db: Database) -> str:
        mode = LIST_SLOTS_MODE if list_art_mode(db, kind) == LIST_MASTHEAD_MODE else LIST_MASTHEAD_MODE
        set_list_art_mode(db, kind, mode)
        record_action(db, actor=actor, action=f"set_{kind}_art_mode", detail=mode)
        return mode

    mode = await lane.run(_apply)
    if mode == LIST_SLOTS_MODE:
        _announce_line(session, f"The art is now the {what} itself. Use [C]heck to see whether the list fits it.")
    else:
        _announce_line(session, f"The art is now shown above the {what}.")


def _list_art_sample_rows(db: Database, actor: User, kind: str) -> list[tuple[str, str]]:
    """The top level of the list as `actor` sees it, name and compact
    value, for the preview. People online are only known on a running
    node, so channels show none."""
    if kind == BOARD_LIST:
        from netbbs.activity import unread_post_count

        rows = [(f"[{c.name}]", "") for c in list_top_level_board_categories(db)]
        for board in list_boards(db):
            if board.category_id is None and meets_read_gate(db, actor, board):
                count = unread_post_count(db, actor, board)
                value = "not visited yet" if count is None else ("caught up" if count == 0 else f"{count} new")
                rows.append((board.name, value))
        return rows
    if kind == FILE_AREA:
        rows = [(f"[{c.name}]", "") for c in list_top_level_file_categories(db)]
        for area in list_file_areas(db):
            if area.category_id is None and meets_read_gate(db, actor, area):
                count, _ = count_listed_files(db, area)
                rows.append((area.name, f"{count} file{'' if count == 1 else 's'}"))
        return rows
    # The channel list's own filter: hidden channels, level and age gates.
    from netbbs.net.chat_flow import list_visible_channels_for

    rows = [(f"[{c.name}]", "") for c in list_top_level_channel_categories(db)]
    rows += [(channel.name, "") for channel in list_visible_channels_for(db, actor) if channel.category_id is None]
    return rows


def _list_art_fits(session: Session, art: SlotArt, column_width: int) -> str | None:
    """Why a caller with a terminal like yours gets the generated list
    instead, or `None` when the art is used -- the same tests the list
    itself makes (`pick_item`'s slot art)."""
    if getattr(session, "output_charset", None) == ASCII:
        return "your terminal reads plain ASCII"
    physical = physical_terminal_width(session)
    if art.width > physical:
        return f"the art is {art.width} columns, your terminal {physical}"
    if not list_slot_fits(art.list, column_width):
        if art.list.height < 3:
            return f"the {{list}} slot is {art.list.height} rows; it needs at least 3"
        return f"the {{list}} slot leaves names {list_name_width(art.list, column_width)} columns; they need 12"
    # The art, a nav row, a trailer row and the prompt, above the last row.
    if art.height + 3 >= session.terminal_height:
        return f"the art needs {art.height + 4} rows, your terminal has {session.terminal_height}"
    return None


async def _check_list_slot_art_screen(session: Session, lane: DatabaseLane, actor: User, kind: str) -> None:
    """[C]heck for a list screen's art: its slots, what makes it unusable,
    and whether a list fits its `{list}` slot on a terminal the size of
    yours -- with the widest value a row can show, the gate note."""
    from netbbs.net.chat_flow import NAME_GATE_NOTE

    what, path_of, enabled_of, max_bytes = _LIST_ART_SCREENS[kind]
    art, enabled, mode, rows = await lane.run(lambda db: (
        read_list_art(path_of(db), max_bytes), enabled_of(db), list_art_mode(db, kind),
        _list_art_sample_rows(db, actor, kind),
    ))
    await session.write_line(colored(f"\r\nChecking the {what} art for slots:", fg_color=MUTED_COLOR))
    if not enabled:
        await session.write_line(highlight_hotkeys(
            "Callers don't see this art yet: it's switched off. Use [E]nable.", color=WARNING_COLOR
        ))
    if mode != LIST_SLOTS_MODE:
        await session.write_line(highlight_hotkeys(
            "Callers see this above the list, not as it: [M]ode changes that.", color=WARNING_COLOR
        ))
    if art is None:
        await session.write_line("No usable art file. Apply a gallery sample, upload one, or draw one first.")
    else:
        await session.write_line(highlight_report(f"Art: {art.width} columns, {art.height} rows.", color=VALUE_COLOR))
        for line in describe_slots(art) or ["No slots found."]:
            await session.write_line(highlight_report(f"  {line}", color=VALUE_COLOR))
        if art.problems:
            for problem in art.problems:
                await session.write_line(colored(f"  Problem: {problem}", fg_color=ERROR_COLOR))
        else:
            widest = max((display_width(value) for _, value in rows), default=0)
            for label, column_width in (
                ("Your list", widest),
                ("A list with an entry that needs verification", max(widest, display_width(NAME_GATE_NOTE))),
            ):
                reason = _list_art_fits(session, art, column_width)
                if reason is None:
                    await session.write_line(_fits_line(
                        label, f", {art.list.height} entries a page, names up to "
                        f"{list_name_width(art.list, column_width)} columns",
                    ))
                else:
                    await session.write_line(
                        colored(f"  {label}: generated list instead -- {reason}.", fg_color=WARNING_COLOR)
                    )
    await session.write_line(continue_prompt())
    await session.read_any_key()


async def _preview_list_slot_art(session: Session, lane: DatabaseLane, actor: User, kind: str) -> None:
    """[P]review in slots mode: the first page of the list's top level as
    you see it, drawn into the art."""
    what, path_of, _enabled_of, max_bytes = _LIST_ART_SCREENS[kind]
    art, rows, fields = await lane.run(lambda db: (
        read_list_art(path_of(db), max_bytes), _list_art_sample_rows(db, actor, kind),
        list_slot_fields(session, db, actor),
    ))
    if art is None or art.problems or art.list is None:
        await session.write_line(colored(
            f"\r\nThe {what} art can't be used as the list; [C]heck says why. Callers get the generated list.",
            fg_color=WARNING_COLOR,
        ))
    else:
        column_width = max((display_width(value) for _, value in rows), default=0)
        reason = _list_art_fits(session, art, column_width)
        page = rows[:art.list.height]
        pages = max(1, -(-len(rows) // art.list.height))
        fields.update(title=what.capitalize(), page=f"1/{pages}", count=str(len(rows)))
        drawn = None if reason is not None else render_list_slot_art(
            art, fields=fields,
            rows=[ListSlotRow(f"{number:02d}.", name, value) for number, (name, value) in enumerate(page, start=1)],
            column_width=column_width, ellipsis=ellipsis_for(session),
        )
        if drawn is None:
            await session.write_line(colored(
                f"\r\nCallers like you get the generated list instead: {reason or 'the list does not fit'}.",
                fg_color=WARNING_COLOR,
            ))
        else:
            await write_preview_art_text(
                session, drawn, speed=await lane.run(art_speed, kind), limit=await lane.run(art_time_limit, kind),
            )
            await session.write(move_cursor(art.height + 1, 1))
            if kind == CHAT_CHANNEL_PICKER:
                await session.write_line(colored("(people online are filled in on a running node)", fg_color=MUTED_COLOR))
    await session.write_line(continue_prompt())
    await session.read_any_key()


# -- one set of screens for every banner and masthead (issue #1327) ---------
#
# The welcome, main-menu, logoff and new-account banners and the three list
# mastheads each have the same screen: status, [P]review, [E]nable,
# [D]isable, [O]pen editor, [G]allery, [F]rom disk, [U]pload and [R]emove
# file. A `_BannerSlot` says what differs between them; `_slot_menu` and the
# screens below draw every one of them from it.


@dataclass(frozen=True)
class _ArtMode:
    """A slot whose art can be shown above its screen or be the screen
    itself, with live slots (issue #929): the main menu, and the three lists
    (`_LIST_ART_SCREENS`). Adds [M]ode and [C]heck to the slot's screen."""

    read: Callable[[Database], str]
    slots: str  # the value of `read` in slots mode
    line: Callable[[str], str]  # the mode, as the row under the status
    entry_brief: Callable[[str], str]  # [M]ode's description
    check_brief: str
    toggle: Callable[[Session, DatabaseLane, User], Awaitable[None]]
    check: Callable[[Session, DatabaseLane, User], Awaitable[None]]
    preview: Callable[[Session, DatabaseLane, User], Awaitable[None]]  # [P]review in slots mode
    apply_preset: Callable[[Database, object], None]  # a gallery sample's own mode


@dataclass(frozen=True)
class _BannerSlot:
    """One banner or masthead: its file, its switch, its samples, and the
    words its screens use for it."""

    key: str  # the file's suffix (`<db>_<key>.ans`) and the audit actions' stem
    title: str  # the screen's title and its breadcrumb
    noun: str  # "banner" or "masthead"
    piece: str  # how [U]pload and [R]emove name it
    back_to: str  # the screen [B]ack returns to
    path_of: Callable[[Database], Path]
    status_of: Callable[[Database], object]
    set_enabled: Callable[[Database, bool], None]
    max_bytes: int
    presets: tuple
    load_preset: Callable[[object], bytes]
    load: Callable[[Database], str | None] | None = None  # the art callers get, for the plain preview
    preview_heading: str = ""
    preview_note: str = ""  # a muted line under a shown preview
    preview: Callable[..., Awaitable[None]] | None = None  # a preview of the slot's own
    name: str = ""  # the outcome lines' name for it; the title when empty
    picker_name: str = ""  # the gallery and from-disk pickers' name for it; `name` when empty
    disabled_lead: str = ""  # what [D]isable says first; "<name> disabled." when empty
    gallery_empty: str = ""  # "No bundled <noun> samples." when empty
    subtitle: str = ""  # the line under the title
    art_kind: str | None = None  # `netbbs.net.art_pacing` kind: adds [S]peed and [T]ime limit
    mode: _ArtMode | None = None
    credit: bool = False  # the welcome banner's [C]redit line
    too_wide: str = "no art"  # what a terminal narrower than the art gets
    preview_preset: Callable[..., Awaitable[None]] | None = None  # a gallery sample's preview, when not as drawn
    preset_as: Callable[[object], str] | None = None  # "as the ..." for a gallery sample, when it varies

    @property
    def shown_name(self) -> str:
        return self.name or self.title

    @property
    def listed_name(self) -> str:
        return self.picker_name or self.shown_name

    @property
    def as_what(self) -> str:
        return f"the {self.listed_name.lower()}"


async def _slot_menu(
    session: Session, lane: DatabaseLane, actor: User, slot: _BannerSlot, *,
    node_controls: NodeControls | None = None,
) -> None:
    """A banner's or masthead's own screen, and every key on it."""
    with entered(session, slot.title):
        description_level = await lane.run(menu_description_level, actor)
        unicode_style = await lane.run(unicode_style_enabled, actor)
        collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
        redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
        prefs = (description_level, redraw_in_place, unicode_style, collapsed)
        actions = {
            "p": lambda: _preview_slot(session, lane, actor, slot=slot, node_controls=node_controls),
            "e": lambda: _enable_slot_screen(session, lane, actor, slot),
            "d": lambda: _disable_slot_screen(session, lane, actor, slot),
            "o": lambda: _edit_slot_screen(session, lane, actor, slot),
            "g": lambda: _slot_gallery_screen(session, lane, actor, slot, *prefs),
            "f": lambda: _slot_filesystem_screen(session, lane, actor, slot, *prefs),
            "u": lambda: _upload_banner_piece(
                session, lane, actor, path_of=slot.path_of, label=slot.piece, audit_action=f"upload_{slot.key}",
            ),
            "r": lambda: _remove_banner_file(
                session, lane, actor, status_of=slot.status_of, set_enabled=slot.set_enabled,
                label=slot.piece, audit_action=f"remove_{slot.key}",
            ),
            HELP_KEY: lambda: _slot_help_screen(session, lane, slot, unicode_style),
        }
        if slot.art_kind is not None:
            actions["s"] = lambda: _cycle_art_speed(lane, actor, slot.art_kind)
            actions["t"] = lambda: _cycle_art_time_limit(lane, actor, slot.art_kind)
        if slot.mode is not None:
            actions["m"] = lambda: slot.mode.toggle(session, lane, actor)
            actions["c"] = lambda: slot.mode.check(session, lane, actor)
        if slot.credit:
            actions["c"] = lambda: _toggle_credit_screen(session, lane, actor)

        await _draw_slot_menu(session, lane, *prefs, slot=slot)
        while True:
            choice = (await session.read_key()).lower()
            if choice == "b":
                await session.write_line("")
                return
            action = actions.get(choice)
            if action is None:
                await session.write(reject_unhandled_key(choice))
                continue
            await session.write_line("")
            await action()
            await _draw_slot_menu(session, lane, *prefs, slot=slot)


async def _toggle_credit_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    shown = await lane.run(_toggle_welcome_banner_credit, actor)
    _announce(session, "Callers now see the art's credit under your banner." if shown
              else "The art's credit is no longer shown under your banner.")


async def _slot_help_screen(session: Session, lane: DatabaseLane, slot: _BannerSlot, unicode_style: bool) -> None:
    """A banner's or masthead's help: what each of its keys changes, then
    where its file lives."""
    from netbbs.rendering.ansi import strip_ansi

    entries = _slot_entries(
        slot, status=await lane.run(slot.status_of),
        speed=await lane.run(art_speed, slot.art_kind) if slot.art_kind is not None else 0,
        time_limit=await lane.run(art_time_limit, slot.art_kind) if slot.art_kind is not None else 0,
        mode=await lane.run(slot.mode.read) if slot.mode is not None else None,
    )
    help_entries = []
    for entry in entries:
        key = strip_ansi(entry.label)[1:2].lower()
        detail = _SLOT_KEY_HELP.get(key)
        if key == "c" and slot.credit:
            detail = "Show or hide the art's credit under the banner"
        help_entries.append(MenuEntry(label=entry.label, brief=entry.brief, detailed=detail))
    path = await lane.run(slot.path_of)
    await show_menu_help(
        session, f"{slot.title} help", help_entries,
        about=f"Its file: {path}. Upload, or copy an .ans there yourself (SFTP, SCP), then Enable.",
        header_color=await lane.run(effective_header_color_256), unicode_style=unicode_style,
    )


async def _draw_slot_menu(
    session: Session, lane: DatabaseLane, description_level: str, redraw_in_place: bool,
    unicode_style: bool, collapsed: bool, *, slot: _BannerSlot,
) -> None:
    status = await lane.run(slot.status_of)
    rows = await _write_counted(session, "\r\n" + screen_title(slot.title,
            breadcrumb=crumbs(session, own=True), width=session.terminal_width, clear=redraw_in_place,
            unicode_style=unicode_style, collapsed=collapsed,
            header_color=await lane.run(effective_header_color_256), node_name_gradient=session.node_name_gradient))
    if slot.subtitle:
        rows += await _write_wrapped_subtitle(session, slot.subtitle)
    speed = time_limit = None
    if slot.art_kind is not None:
        speed = await lane.run(art_speed, slot.art_kind)
        time_limit = await lane.run(art_time_limit, slot.art_kind)
    rows += await _write_sections(
        session,
        [_banner_status_section(
            status, unicode_style=unicode_style, sauce=await asyncio.to_thread(_banner_sauce, status),
            credit_line=await lane.run(is_welcome_banner_credit_enabled) if slot.credit else None,
            too_wide=slot.too_wide, speed=speed,
        )],
        unicode_style=unicode_style,
    )
    mode = None
    if slot.mode is not None:
        mode = await lane.run(slot.mode.read)
        await session.write_line("")
        rows += 1
        # One row each on the main menu's, so its screen fits 80x24 with [S]peed.
        rows += await _write_wrapped_subtitle(session, slot.mode.line(mode))
    entries = _slot_entries(slot, status=status, speed=speed, time_limit=time_limit, mode=mode)
    await _write_banner_menu(
        session, entries, description_level, used_rows=rows,
        help_text="(Ctrl-H for where to place your own .ans files)",
    )
    await _choice_prompt(session)


def _slot_entries(slot: _BannerSlot, *, status, speed, time_limit, mode) -> list[MenuEntry]:
    """The keys a banner's or masthead's screen offers, as its menu and its
    help both show them."""
    entries = [MenuEntry(
        label=menu_key("P", "review"),
        brief="Show it as callers see it" if slot.mode is not None else f"Show the {slot.noun} as callers see it",
    )]
    if slot.art_kind is not None:
        entries += [MenuEntry(label=menu_key("S", "peed"), brief=_art_speed_brief(speed)), _art_time_limit_entry(time_limit)]
    if slot.mode is not None:
        entries += [
            MenuEntry(label=menu_key("M", "ode"), brief=slot.mode.entry_brief(mode)),
            MenuEntry(label=menu_key("C", "heck"), brief=slot.mode.check_brief),
        ]
    entries += [
        MenuEntry(label=menu_key("E", "nable"), brief=f"Turn the {slot.noun} on"),
        MenuEntry(label=menu_key("D", "isable"), brief=f"Turn the {slot.noun} off"),
        MenuEntry(label=menu_key("O", "pen editor"), brief="Edit the banner text" if slot.noun == "banner" else "Edit the masthead art"),
    ]
    if slot.credit:
        entries.append(MenuEntry(label=menu_key("C", "redit line"), brief="Credit the art under the banner"))
    entries += [
        MenuEntry(label=menu_key("G", "allery"), brief=f"Apply a bundled sample {slot.noun}"),
        MenuEntry(label=menu_key("F", "rom disk"), brief="Load your own .ans from this node"),
        MenuEntry(label=menu_key("U", "pload"), brief="Send an .ans from your computer"),
        *_remove_file_entry(status),
        MenuEntry(label=menu_key("B", "ack"), brief=f"Return to {slot.back_to}"),
    ]
    return entries


async def _preview_slot(
    session: Session, lane: DatabaseLane, actor: User, *, slot: _BannerSlot,
    node_controls: NodeControls | None = None,
) -> None:
    """[P]review: the slot's own preview where it has one (the welcome and
    logoff banners fill field slots as callers get them), the screen drawn
    into the art in slots mode, and otherwise the art exactly as the
    caller-facing loader returns it -- a smoke test of that loading path,
    not a separate rendering."""
    if slot.preview is not None:
        await slot.preview(session, lane, actor, node_controls=node_controls)
        return
    if slot.mode is not None and await lane.run(slot.mode.read) == slot.mode.slots:
        await slot.mode.preview(session, lane, actor)
        return
    status, text = await lane.run(lambda db: (slot.status_of(db), slot.load(db)))
    await session.write_line(colored(f"\r\n{slot.preview_heading}", fg_color=MUTED_COLOR))
    if not text:
        await _write_banner_not_live(session, status, callers_see=f"no {slot.noun}")
    elif slot.art_kind is not None:
        # At the speed callers get it (issue #929).
        await write_preview_art(
            session, text, speed=await lane.run(art_speed, slot.art_kind), limit=await lane.run(art_time_limit, slot.art_kind),
        )
    else:
        await write_preformatted_line(session, text)
    if text and slot.preview_note:
        await session.write_line(colored(slot.preview_note, fg_color=MUTED_COLOR))
    # Without this wait, redraw_in_place clears the preview before it can be
    # read (see _preview_welcome_banner_screen).
    await session.write_line(continue_prompt())
    await session.read_any_key()


async def _enable_slot_screen(session: Session, lane: DatabaseLane, actor: User, slot: _BannerSlot) -> None:
    status = await lane.run(slot.status_of)
    if not status.exists:
        _announce(session, f"No {slot.noun} file found at {status.path}. Place a .ans file there first, then enable.", color=WARNING_COLOR)
        return
    if (status.size_bytes or 0) > slot.max_bytes:
        _announce_line(session,
            colored(
                f"{slot.noun.capitalize()} file at {status.path} is {status.size_bytes} bytes, over the "
                f"{slot.max_bytes} byte limit -- not enabling.",
                fg_color=MUTED_COLOR,
            )
        )
        return

    def _apply(db: Database) -> None:
        slot.set_enabled(db, True)
        record_action(db, actor=actor, action=f"enable_{slot.key}", detail=str(status.path))

    await lane.run(_apply)
    _announce_line(session, f"{slot.shown_name} enabled. Use [P]review to verify it looks right.")


async def _disable_slot_screen(session: Session, lane: DatabaseLane, actor: User, slot: _BannerSlot) -> None:
    def _apply(db: Database):
        status = slot.status_of(db)
        slot.set_enabled(db, False)
        record_action(db, actor=actor, action=f"disable_{slot.key}", detail=str(status.path))
        return status

    status = await lane.run(_apply)
    lead = slot.disabled_lead or f"{slot.shown_name} disabled."
    _announce_line(session, f"{lead} Your file at {status.path} was left in place.")


async def _edit_slot_screen(session: Session, lane: DatabaseLane, actor: User, slot: _BannerSlot) -> None:
    """Opens the WYSIWYG ANSI art editor (design doc) against the slot's
    current file, if any. `edit_ansi_art` itself knows nothing about
    banners -- this screen is responsible for loading the existing file,
    computing the draft path, and writing a real save back to the slot's
    path."""
    path = await lane.run(slot.path_of)
    initial_bytes = path.read_bytes() if path.exists() else None
    draft_path = path.parent / f"{path.name}.draft"

    result = await edit_ansi_art(
        session, initial_bytes=initial_bytes, draft_path=draft_path,
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
    )
    if result is None:
        _announce_line(session, colored("\r\nNo changes saved.", fg_color=MUTED_COLOR))
        return

    path.write_bytes(result)
    await lane.run(record_action, actor=actor, action=f"edit_{slot.key}", detail=str(path))
    _announce_saved(session, "Saved ", path, ". Use [P]review to verify it looks right.")


async def _slot_gallery_screen(
    session: Session, lane: DatabaseLane, actor: User, slot: _BannerSlot, description_level: str,
    redraw_in_place: bool, unicode_style: bool, collapsed: bool,
) -> None:
    """Issue #169: apply one of NetBBS's own bundled samples with zero
    filesystem access to the node -- the samples are real installed package
    data (`netbbs.net.banner_presets`), not files a SysOp has to `cp` into
    place first (the gap this closes: a wheel install has no `examples/`
    directory to copy from at all).

    Dogfood follow-up: declining the apply prompt loops back into this same
    gallery picker instead of exiting to the slot's menu -- browsing several
    samples in a row used to mean re-entering `[G]allery` after every
    single one just to look at the next.

    Second dogfood follow-up: that loop-back used to always reopen the
    picker at page 1 with nothing highlighted, discarding exactly the
    browsing position a decline is supposed to return you to -- `pick_
    item`'s `start_stable_id` now re-highlights whichever sample was just
    declined."""
    last_stable_id: int | None = None
    while True:
        selection = await pick_item(
            session,
            list(enumerate(slot.presets, start=1)),
            name_of=lambda pair: pair[1].name,
            stable_id_of=lambda pair: pair[0],
            description_of=lambda pair: f"{pair[1].depth} -- {pair[1].description}",
            title=f"{slot.listed_name} gallery",
            empty_message=slot.gallery_empty or f"No bundled {slot.noun} samples.",
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
        data = slot.load_preset(preset)
        await session.write_line(colored(f"\r\nPreviewing {preset.name!r}:", fg_color=MUTED_COLOR))
        if slot.preview_preset is not None:
            await slot.preview_preset(session, lane, actor, preset, data)
        else:
            await write_preformatted_line(session, decode_banner_bytes(data) + RESET)

        what = slot.preset_as(preset) if slot.preset_as is not None else f"as {slot.as_what}"
        if not await _preview_apply_choice(session, f"Apply {preset.name!r} {what}"):
            continue

        def _apply(db: Database) -> Path:
            path = slot.path_of(db)
            path.write_bytes(data)
            slot.set_enabled(db, True)
            if slot.mode is not None:
                slot.mode.apply_preset(db, preset)
            record_action(db, actor=actor, action=f"apply_{slot.key}_preset", detail=f"{preset.key} -> {path}")
            return path

        path = await lane.run(_apply)
        _announce_saved(session, "Applied and enabled. Saved to ", path, ". Use [P]review to verify it looks right.")
        return


async def _slot_filesystem_screen(
    session: Session, lane: DatabaseLane, actor: User, slot: _BannerSlot, description_level: str,
    redraw_in_place: bool, unicode_style: bool, collapsed: bool,
) -> None:
    """Issue #170: load a SysOp's own externally-authored `.ans` file --
    drawn in a dedicated tool, or downloaded from elsewhere -- instead of
    having to shell in and place it at the slot's path by hand. Different
    problem from the bundled gallery (issue #169, sibling): nothing here was
    ever going to ship *with* NetBBS, so this enumerates real files on disk
    instead of a fixed preset tuple.

    **Browsable root** (locked design, see the issue's own comments): the
    node's own state directory only -- exactly the slot path's own parent,
    the same directory NetBBS already reads/writes banner state in. No new
    `node_config` setting, no new attack surface beyond what already exists.

    **Load semantics**: copy, not reference-in-place. The selected file's
    bytes are written to the slot's existing well-known path, exactly as if
    the SysOp had `cp`'d it there by hand -- the loaders, the status, backup
    and restore, and the WYSIWYG editor all keep their existing
    one-file-one-path assumption completely untouched.

    **Validation timing**: the same size cap `[E]nable` already enforces
    runs *before* the copy, against the source file -- rejecting a pick
    immediately with a clear reason, not a weaker path into the same target
    than manual placement already has."""

    def _list(db: Database) -> tuple[list[Path], Path]:
        directory = slot.path_of(db).parent
        return _browsable_ans_files(directory, exclude=slot.path_of(db)), directory

    files, directory = await lane.run(_list)
    if not files:
        # Announced, not held behind a keypress: it is on the menu this returns to.
        _announce(
            session,
            f"No other .ans files found in {directory}. Place one there "
            f"(e.g. via SFTP/SCP), or send one with [U]pload, then browse again.",
            color=MUTED_COLOR,
        )
        return

    last_stable_id: int | None = None
    while True:
        selection = await pick_item(
            session,
            list(enumerate(files, start=1)),
            name_of=lambda pair: pair[1].name,
            stable_id_of=lambda pair: pair[0],
            title=f"{slot.listed_name} -- load from disk",
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
        if size > slot.max_bytes:
            await session.write_line(colored(
                f"{path.name} is {size} bytes, over the {slot.max_bytes} byte "
                f"limit -- not loading.", fg_color=MUTED_COLOR,
            ))
            # Otherwise `pick_item`'s own next redraw clears this before it
            # can be read.
            await session.write_line(continue_prompt())
            await session.read_any_key()
            continue

        data = path.read_bytes()
        await session.write_line(colored(f"\r\nPreviewing {path.name!r}:", fg_color=MUTED_COLOR))
        await write_preformatted_line(session, decode_banner_bytes(data) + RESET)

        if not await _preview_apply_choice(session, f"Load {path.name!r} as {slot.as_what}"):
            continue

        def _apply(db: Database) -> Path:
            target = slot.path_of(db)
            target.write_bytes(data)
            slot.set_enabled(db, True)
            record_action(db, actor=actor, action=f"load_{slot.key}_from_file", detail=f"{path} -> {target}")
            return target

        target = await lane.run(_apply)
        _announce_saved(session, "Loaded and enabled. Saved to ", target, ". Use [P]review to verify it looks right.")
        return


async def _preview_main_menu_preset(
    session: Session, lane: DatabaseLane, actor: User, preset, data: bytes,
) -> None:
    """A main-menu gallery sample: a slots sample drawn as your menu."""
    if preset.mode == SLOTS_MODE:
        await _write_slot_art_preview(session, lane, actor, parse_slot_art(decode_banner_bytes(data)), level=None)
    else:
        await write_preformatted_line(session, decode_banner_bytes(data) + RESET)


_MAIN_MENU_MODE = _ArtMode(
    read=main_menu_art_mode,
    slots=SLOTS_MODE,
    line=lambda mode: (
        "Mode: above the menu. The art is shown above the live main menu."
        if mode == MASTHEAD_MODE else
        "Mode: the menu itself. Callers' items and live values fill its slots."
    ),
    entry_brief=lambda mode: "Make the art the menu itself" if mode == MASTHEAD_MODE else "Show the art above the menu",
    check_brief="Slots, problems and menu fit",
    toggle=_toggle_main_menu_art_mode,
    check=_check_main_menu_slot_art_screen,
    preview=_preview_main_menu_slot_art,
    apply_preset=lambda db, preset: set_main_menu_art_mode(db, preset.mode),
)


def _list_mode(kind: str) -> _ArtMode:
    """[M]ode, [C]heck and slots-mode [P]review for a list's masthead."""
    return _ArtMode(
        read=lambda db: list_art_mode(db, kind),
        slots=LIST_SLOTS_MODE,
        line=_list_art_mode_line,
        entry_brief=lambda mode: "Make the art the list itself" if mode == LIST_MASTHEAD_MODE else "Show the art above the list",
        check_brief="Slots, problems and list fit",
        toggle=lambda session, lane, actor: _toggle_list_art_mode(session, lane, actor, kind),
        check=lambda session, lane, actor: _check_list_slot_art_screen(session, lane, actor, kind),
        preview=lambda session, lane, actor: _preview_list_slot_art(session, lane, actor, kind),
        # A list sample is the list itself; any other sample goes above it.
        apply_preset=lambda db, preset: set_list_art_mode(
            db, kind, LIST_SLOTS_MODE if preset.mode == "slots" else LIST_MASTHEAD_MODE,
        ),
    )


_WELCOME = _BannerSlot(
    key="welcome_banner", title="Welcome banner", noun="banner", piece="the welcome banner",
    back_to="Session banners", path_of=banner_path, status_of=welcome_banner_status,
    set_enabled=set_welcome_banner_enabled, max_bytes=MAX_BANNER_SIZE_BYTES,
    presets=WELCOME_BANNER_PRESETS, load_preset=load_welcome_banner_preset,
    preview=_preview_welcome_banner_screen, disabled_lead="Reverted to the default banner.",
    gallery_empty="No bundled welcome-banner samples.", art_kind=WELCOME_ART, credit=True,
    too_wide="the default banner",
)
_LOGOFF = _BannerSlot(
    key="logoff_banner", title="Logoff banner", noun="banner", piece="the log-off banner",
    back_to="Session banners", path_of=logoff_banner_path, status_of=logoff_banner_status,
    set_enabled=set_logoff_banner_enabled, max_bytes=MAX_LOGOFF_BANNER_SIZE_BYTES,
    presets=LOGOFF_BANNER_PRESETS, load_preset=load_logoff_banner_preset,
    preview=_preview_logoff_banner_screen,
    subtitle="Shown above the ordinary Goodbye message on an intentional Log off only -- never on an idle "
    "timeout, kick, or account revocation.",
)
_NEW_ACCOUNT_BEFORE = _BannerSlot(
    key="new_account_banner_before", title="New account banner (before)", noun="banner",
    piece="the before-signup banner", back_to="Session banners", path_of=new_account_banner_before_path,
    status_of=new_account_banner_before_status, set_enabled=set_new_account_banner_before_enabled,
    max_bytes=MAX_NEW_ACCOUNT_BANNER_BEFORE_SIZE_BYTES, presets=NEW_ACCOUNT_BANNER_BEFORE_PRESETS,
    load_preset=load_new_account_banner_before_preset, load=load_new_account_banner_before,
    preview_heading="Previewing new-account (before) banner as shown at signup:",
    name="New-account (before) banner",
    subtitle="Shown once, right when a caller starts self-service signup -- before the Create "
    "account prompts, never repeated on a fixable retry.",
)
_NEW_ACCOUNT_AFTER = _BannerSlot(
    key="new_account_banner_after", title="New account banner (after)", noun="banner",
    piece="the after-signup banner", back_to="Session banners", path_of=new_account_banner_after_path,
    status_of=new_account_banner_after_status, set_enabled=set_new_account_banner_after_enabled,
    max_bytes=MAX_NEW_ACCOUNT_BANNER_AFTER_SIZE_BYTES, presets=NEW_ACCOUNT_BANNER_AFTER_PRESETS,
    load_preset=load_new_account_banner_after_preset, load=load_new_account_banner_after,
    preview_heading="Previewing new-account (after) banner as shown once signup succeeds:",
    name="New-account (after) banner",
    subtitle="Shown once self-service signup succeeds -- covers both an immediate login and a "
    "pending-approval account, alongside the existing message either way.",
)
_MAIN_MENU = _BannerSlot(
    key="main_menu_banner", title="Main-menu masthead", noun="masthead", piece="the main-menu masthead",
    back_to="Mastheads", path_of=main_menu_banner_path, status_of=main_menu_banner_status,
    set_enabled=set_main_menu_banner_enabled, max_bytes=MAX_MASTHEAD_SIZE_BYTES,
    presets=MAIN_MENU_BANNER_PRESETS, load_preset=load_main_menu_banner_preset, load=load_main_menu_banner,
    # No real menu under it: netbbs.net.main_menu imports this module, and
    # the masthead carries nothing truecolor-dependent to showcase.
    preview_heading="Previewing the masthead as shown above the main menu:",
    preview_note="(the main menu itself renders live, unchanged, immediately below this)",
    picker_name="Masthead",
    disabled_lead="Masthead disabled -- the main menu reverts to showing no banner above it.",
    art_kind=MAIN_MENU_ART, mode=_MAIN_MENU_MODE, preview_preset=_preview_main_menu_preset,
    preset_as=lambda preset: "as the main menu" if preset.mode == SLOTS_MODE else "as the masthead",
)
_BOARD_LIST = _BannerSlot(
    key="board_list_banner", title="Board list masthead", noun="masthead", piece="the message-board masthead",
    back_to="Mastheads", path_of=board_list_banner_path, status_of=board_list_banner_status,
    set_enabled=set_board_list_banner_enabled, max_bytes=MAX_BOARD_LIST_BANNER_SIZE_BYTES,
    presets=BOARD_LIST_MASTHEAD_PRESETS, load_preset=load_board_list_masthead_preset, load=load_board_list_banner,
    preview_heading="Previewing board list masthead as shown above the board list:",
    subtitle="Shown above every board-browsing view -- the top level, a category, or a Community.",
    art_kind=BOARD_LIST, mode=_list_mode(BOARD_LIST),
)
_FILE_AREA = _BannerSlot(
    key="file_area_banner", title="File area masthead", noun="masthead", piece="the file-area masthead",
    back_to="Mastheads", path_of=file_area_banner_path, status_of=file_area_banner_status,
    set_enabled=set_file_area_banner_enabled, max_bytes=MAX_FILE_AREA_BANNER_SIZE_BYTES,
    presets=FILE_AREA_MASTHEAD_PRESETS, load_preset=load_file_area_masthead_preset, load=load_file_area_banner,
    preview_heading="Previewing file area masthead as shown above the file-area list:",
    subtitle="Shown above every file-area-browsing view -- the top level, a category, or a Community.",
    art_kind=FILE_AREA, mode=_list_mode(FILE_AREA),
)
_CHAT_CHANNEL_PICKER = _BannerSlot(
    key="chat_channel_picker_banner", title="Chat channel picker masthead", noun="masthead",
    piece="the chat-channel masthead", back_to="Mastheads", path_of=chat_channel_picker_banner_path,
    status_of=chat_channel_picker_banner_status, set_enabled=set_chat_channel_picker_banner_enabled,
    max_bytes=MAX_CHAT_CHANNEL_PICKER_BANNER_SIZE_BYTES, presets=CHAT_CHANNEL_PICKER_MASTHEAD_PRESETS,
    load_preset=load_chat_channel_picker_masthead_preset, load=load_chat_channel_picker_banner,
    preview_heading="Previewing chat channel picker masthead as shown above the channel picker:",
    subtitle="Shown above every channel-picker view -- the top level, a category, or a Community. "
    "Never inside a live channel.",
    art_kind=CHAT_CHANNEL_PICKER, mode=_list_mode(CHAT_CHANNEL_PICKER),
)
_BANNER_SLOTS = (
    _WELCOME, _LOGOFF, _NEW_ACCOUNT_BEFORE, _NEW_ACCOUNT_AFTER, _MAIN_MENU, _BOARD_LIST, _FILE_AREA,
    _CHAT_CHANNEL_PICKER,
)

# Each piece's file name, built from the database's own name without
# resolving it (`_upload_banner_piece`).
_BANNER_FILE_SUFFIX_OF: dict[Callable[[Database], Path], Callable[[str], str]] = {
    slot.path_of: (lambda stem, key=slot.key: f"{stem}_{key}.ans") for slot in _BANNER_SLOTS
}

# The screens tests draw on their own: the welcome banner's menu, and the
# main-menu masthead's [P]review.
_draw_welcome_banner_menu = functools.partial(_draw_slot_menu, slot=_WELCOME)
_preview_main_menu_banner_screen = functools.partial(_preview_slot, slot=_MAIN_MENU)


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


def _parse_rgb(raw: str) -> tuple[int, int, int] | None:
    parts = raw.split(",")
    if len(parts) != 3:
        return None
    try:
        candidate = tuple(int(part.strip()) for part in parts)
    except ValueError:
        return None
    if not all(0 <= value <= 255 for value in candidate):
        return None
    return candidate  # type: ignore[return-value]


def _theme_color_field(slot: str) -> Callable[[Session, DatabaseLane, dict], Awaitable[None]]:
    """One branding slot on the node-colors editor: an R,G,B triple,
    'default' to clear the override, blank to keep. Validation happens
    here, with the draft untouched on a bad entry; nothing is written
    until [S]ave."""
    label = _THEME_SLOT_LABELS[slot]

    @inline_field
    async def prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        current = draft[slot]
        current_text = f"{current[0]},{current[1]},{current[2]}" if current is not None else "default"
        await write_field_prompt(
            session,
            colored(f"{label}: R,G,B (each 0-255) or 'default' ({_EDIT_HINT}):", fg_color=MUTED_COLOR),
            hint="R,G,B or default; Enter saves, Esc cancels",
        )
        try:
            raw = (await _read_seeded_line(session, initial=current_text)).strip()
        except InputCancelled:
            return
        if not raw or raw.lower() == current_text:
            return
        if raw.lower() == "default":
            draft[slot] = None
            return
        rgb = _parse_rgb(raw)
        if rgb is None:
            await write_field_message(
                session, colored("Not a valid R,G,B triple (each 0-255) -- no change.", fg_color=ERROR_COLOR),
            )
            return
        draft[slot] = rgb

    return prompt


def _theme_preview_preamble(draft: dict) -> str:
    """The editor's own live preview (issue #282): every slot's sample
    line at truecolor and 256-color depth for whatever the draft holds
    right now -- what the separate [P]review screen used to show, and
    the "preview before confirm" issue #162 required, without a second
    trip or a yes/no."""
    lines = [
        colored(
            "Branding only -- status colors (error/success/warning/etc.) always stay standard, "
            "so a caller can trust what they mean on any node.",
            fg_color=MUTED_COLOR,
        )
    ]
    for slot in ("accent", "header", "clock"):
        rgb = draft[slot]
        lines.append(colored(f"{_THEME_SLOT_LABELS[slot]}:", fg_color=LABEL_COLOR, bold=True))
        if rgb is None:
            lines.append(
                colored("  Default:   ", fg_color=MUTED_COLOR) + _theme_sample_line(slot, _THEME_SLOT_DEFAULTS[slot])
            )
        else:
            lines.append(colored("  Truecolor: ", fg_color=MUTED_COLOR) + _theme_sample_line(slot, rgb))
            lines.append(colored("  256-color: ", fg_color=MUTED_COLOR) + _theme_sample_line(slot, nearest_256(rgb)))
    return "\r\n".join(lines)


async def _theme_colors_menu(session: Session, lane: DatabaseLane, actor: User) -> None:
    """Settings -> Node colors (issue #162, reshaped by issue #282): one
    draft editor for the three overridable branding slots, previewed
    live above the fields, applied together on [S]ave. Replaces a menu
    whose per-slot screens only showed the current value inside an
    editable prompt and then asked "Apply this ...?" -- three times over
    -- plus a separate preview screen."""
    def _load(db: Database) -> dict:
        return {
            "accent": accent_color_override(db),
            "header": header_color_override(db),
            "clock": clock_color_override(db),
        }

    draft = await lane.run(_load)
    starting = dict(draft)

    def _render(slot: str) -> Callable[[dict], str]:
        return lambda d: f"{d[slot][0]},{d[slot][1]},{d[slot][2]}" if d[slot] is not None else "default"

    fields = [
        FieldSpec(
            key="accent", label="Accent",
            render=_render("accent"), prompt=_theme_color_field("accent"),
            brief="Board/channel/user-name color",
            help=("Board, channel and user names and other things you can pick, for every caller from their "
                  "next screen; a 256- or 16-colour terminal gets the nearest colour. R,G,B (0-255 each) or "
                  "'default'."),
        ),
        FieldSpec(
            key="header", label="Header",
            render=_render("header"), prompt=_theme_color_field("header"),
            brief="Section-header color",
            help="Section titles and frame borders, for every caller from their next screen. R,G,B or 'default'.",
        ),
        FieldSpec(
            key="clock", label="Clock",
            render=_render("clock"), prompt=_theme_color_field("clock"),
            brief="Main-menu clock color",
            help="The time in the main-menu prompt, for every caller. R,G,B or 'default'.",
        ),
    ]

    async def save(draft: dict) -> bool:
        def _apply(db: Database) -> list[str]:
            messages = []
            for slot in ("accent", "header", "clock"):
                if draft[slot] == starting[slot]:
                    continue
                label = _THEME_SLOT_LABELS[slot]
                _THEME_SLOT_SETTERS[slot](db, draft[slot])
                if draft[slot] is None:
                    previous = starting[slot]
                    record_action(
                        db, actor=actor, action=f"clear_{slot}_color_override",
                        detail=f"{previous[0]},{previous[1]},{previous[2]}",
                    )
                    messages.append(f"{label} reverted to the default.")
                else:
                    rgb = draft[slot]
                    record_action(db, actor=actor, action=f"set_{slot}_color_override", detail=f"{rgb[0]},{rgb[1]},{rgb[2]}")
                    messages.append(f"{label} updated.")
            return messages

        messages = await lane.run(_apply)
        for message in messages or ["No changes."]:
            await session.write_line(message)
        return True

    redraw_in_place, redraw_hint = await lane.run(_resolve_redraw_preference, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    await edit_resource_draft(
        session, lane,
        title="Node colors",
        fields=fields, draft=draft, save=save,
        save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=redraw_in_place, redraw_hint=redraw_hint,
        preamble=_theme_preview_preamble,
        unicode_style=unicode_style,
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
