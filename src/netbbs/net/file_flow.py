"""
File area browsing, upload, and download.

Kept in its own module rather than growing login_flow.py indefinitely —
matches the project's modular-package approach (design doc §3), same
reasoning as chat_flow.py.

Upload/download (design doc) go over real ZMODEM
(`netbbs.net.zmodem`), not a NetBBS-specific scheme — the whole point
being that a real Zmodem-capable terminal (SyncTERM, lrzsz) can drive
this without any custom client software. `[U]pload`/`[D]ownload` take
over the session's raw byte stream for the duration of the transfer,
then hand control back to normal character-mode text I/O once it
finishes (or aborts — see `netbbs.net.zmodem`'s module docstring on
error handling: a failed transfer doesn't crash the session, it reports
the error and returns to browsing).

**Second module migrated onto the two-lane database execution model
(design doc, issue #57)**, following `netbbs.net.
mail_flow`'s proof-of-pattern exactly: every function reachable from
`browse_file_areas` takes `lane: DatabaseLane` instead of `db:
Database`. Two exceptions, deliberately unmigrated:

- `visible_areas` stays on `db: Database`, synchronous — it's a
  menu-*gating* check called from `netbbs.net.main_menu`'s still-
  unmigrated menu-drawing code (a Community's page),
  not part of the file-areas feature itself.
- `_uploader_display_name` keeps `db: Database` as its own first
  parameter, unchanged — it's dispatched *through* the lane
  (`lane.run(_uploader_display_name, entry, ...)`) exactly like any
  imported business-logic function, rather than being rewritten to take
  `lane` itself; nothing about it needs to be a *caller* of the lane,
  only a *callee*.

Unlike `mail_flow`, this module's own `pick_item` call
(`_browse_areas_in_category`) needed no eager-pre-fetch restructuring —
its `name_of`/`description_of` callbacks only ever read fields already
present on the `FileArea`/`FileAreaCategory` objects handed to
`pick_item` (`a.description`, etc.), never a fresh DB read, so there was
nothing to move off the callback in the first place. `_render_file_page`
*does* need it, the same shape `mail_flow._show_inbox`/`_show_sent` used:
`netbbs.timeutil.resolve_display_preferences` fetched once via the lane,
reused for every entry's `format_for_display` call — but this one isn't
a `pick_item` callback at all, just an ordinary loop in an `async`
function, so it's really just the general "fetch once per lane call,
not once per item" efficiency `resolve_display_preferences` was built
for, not a structural requirement the way the picker case was.
"""

from __future__ import annotations

import logging
import weakref
from dataclasses import replace
from pathlib import Path
from typing import Callable

from netbbs.activity import follow, is_following, record_file_area_seen, unfollow
from netbbs.attestation import format_name_for_resource, meets_name_requirement
from netbbs.auth.users import User, get_user_by_id
from netbbs.communities import (
    get_community,
    get_effective_name_requirement,
    meets_read_gate,
    meets_resource_age,
    meets_write_gate,
    resource_age_gate,
    resource_age_visible,
    resource_needs_verification,
)
from netbbs.age_requirement import age_verification_refusal
from netbbs.config import get_max_upload_bytes
from netbbs.files import (
    FileArea,
    FileEntry,
    FileEntryError,
    FileEntryPage,
    download_file,
    list_file_areas,
    list_files_page,
    list_pending_files,
    set_file_description,
    set_file_exempt,
    set_file_pinned,
    upload_file_from_temp,
)
from netbbs.files.categories import (
    FileAreaCategory,
    get_category_by_id,
    list_subcategories,
    list_top_level_categories,
)
from netbbs.files.diz import MAX_DESCRIPTION_BYTES, MAX_DESCRIPTION_LINES, read_archive_description
from netbbs.files.entries import count_listed_files, count_pending_files, count_visible_files
from netbbs.files.storage import new_incoming_temp_path
from netbbs.file_refs import file_size_text
from netbbs.net.file_transfer import (
    DEFAULT_GRANT_TTL_SECONDS,
    DOWNLOAD,
    UPLOAD,
    TransferError,
    TransferGrant,
    TransferGrants,
)
from netbbs.link.boards import LinkContext
from netbbs.link.node_profiles import (
    identity_for_peer, latest_identity_observation, present_link_author_label,
)
from netbbs.link.files import (
    RemoteFile,
    has_queued_file_descriptor,
    is_area_linked,
    linked_area_ids,
    list_remote_files,
    queue_file_descriptor_if_linked,
)
from netbbs.link.protocol import LinkProtocolError
from netbbs.net import zmodem
from netbbs.net.char_input import HELP_KEY, REDRAW_KEY, EditorKey, EditorKeyKind, page_step, reject_unhandled_key
from netbbs.net.help_overlay import show_menu_help
from netbbs.net.color_depth_preference import effective_truecolor
from netbbs.net.composition import edit_line_body
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.draft_storage import drafts_directory, save_draft
from netbbs.net.editor_preference import fullscreen_editor_enabled
from netbbs.net.file_area_banner import load_file_area_banner, load_file_area_slot_art
from netbbs.net.art_pacing import art_speed
from netbbs.net.list_art import FILE_AREA, list_slot_fields
from netbbs.net.chat_flow import NAME_GATE_NOTE
from netbbs.net.node_theme import effective_accent_color_256, effective_header_color_256
from netbbs.net.notices import announce, announce_styled, write_notices
from netbbs.net.row_numbers import read_row_number, row_number_label, row_range_label
from netbbs.net.picker import pick_item
from netbbs.net.prose_editor import EditorHeader, edit_prose
from netbbs.gate_summary import gates_line, resource_gates, unmet_gates
from netbbs.net.session import Session, physical_terminal_width
from netbbs.net.shared_account import (
    authored_earlier_by_shared_account,
    earlier_guest_refusal,
    note_created_this_call,
)
from netbbs.net.session_activity import records_activity
from netbbs.net.sort_ui import SORT_MODE_LABELS, prompt_sort_change
from netbbs.moderation import BoardPermission, has_permission
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.rendering.charset import ellipsis_for
from netbbs.rendering import (
    ERROR_COLOR,
    HEADER_COLOR,
    AUTHOR_COLOR,
    DATE_COLOR,
    EMPHASIS_COLOR,
    MENU_KEY_COLOR,
    MUTED_COLOR,
    RULE_COLOR,
    SUCCESS_COLOR,
    VALUE_COLOR,
    MenuEntry,
    action_bar,
    badge,
    colored,
    colored_truncate,
    cut_to_width,
    empty_state,
    menu_grid,
    menu_key,
    sanitize_text,
    strip_ansi,
    screen_title,
    visible_width,
)
from netbbs.sort_preferences import get_effective_sort_mode, set_sort_preference
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display, resolve_display_preferences


_logger = logging.getLogger(__name__)


def _menu_row(entries: list[MenuEntry], *, width: int, height: int, description_level: str) -> str:
    """Compact `action_bar` packing when descriptions are off, `menu_grid`'s
    taller one-entry-per-line layout once the caller has opted into "brief"/
    "detailed" (issue #160's rollout) -- see `netbbs.net.resource_editor.
    edit_resource_draft`'s identical branch for why `menu_grid` alone isn't a
    byte-for-byte substitute for `action_bar`'s packed row at the off level."""
    if description_level == "off":
        return action_bar([e.label for e in entries], width=width)
    return menu_grid([("", entries)], width=width, height=height, description_level=description_level)


async def enter_file_area(
    session: Session,
    lane: DatabaseLane,
    area: FileArea,
    user: User,
    *,
    initial_cursor: tuple[str, str] | None = None,
    link_context: LinkContext | None = None,
    transfers: TransferGrants | None = None,
) -> None:
    """Enter `area` directly, bypassing the category picker entirely --
    public (unlike `_show_area`) so issue #56's `[N]ew scan` screen
    (`netbbs.net.login_flow`) can jump straight into a specific area
    with a starting cursor, the same reasoning `netbbs.net.chat_flow.
    browse_channels`'s own `initial_channel` parameter already has for
    channels.

    `link_context` (design doc, issue #92), if given, is passed straight
    through to `_show_area`, which offers a `[L]ink catalogue` hotkey to
    browse and fetch this area's carried-but-not-yet-fetched remote
    catalogue when it's Linked -- `None` (Link disabled on this node, or a
    direct test/CLI call site) simply hides that key, same degrade-
    gracefully shape every other optional `link_context` parameter
    already has."""
    await _show_area(session, lane, area, user, initial_cursor=initial_cursor, link_context=link_context, transfers=transfers)


@records_activity("Files")
async def browse_file_areas(
    session: Session,
    lane: DatabaseLane,
    user: User,
    *,
    community_id: int | None = None,
    community_scoped: bool = False,
    title_prefix: str | None = None,
    link_context: LinkContext | None = None,
    transfers: TransferGrants | None = None,
) -> None:
    """Entry point: browse from the top level (no category selected yet)."""
    await _browse_areas_in_category(
        session, lane, user, category_id=None,
        community_id=community_id, community_scoped=community_scoped, title_prefix=title_prefix,
        link_context=link_context, transfers=transfers,
    )


def visible_areas(
    db: Database, user: User, *, community_id: int | None = None, community_scoped: bool = False
) -> list[FileArea]:
    """Every file area `user` can see under the given Community filter --
    what a Community's page offers and counts (design doc §16, issue
    #838). Deliberately still `db`-based, not `lane`-based -- see this
    module's own docstring for why."""
    areas = [
        a for a in list_file_areas(db)
        if meets_read_gate(db, user, a) and resource_age_visible(db, user, a)
    ]
    if community_scoped:
        areas = [a for a in areas if a.community_id == community_id]
    return areas


async def _browse_areas_in_category(
    session: Session,
    lane: DatabaseLane,
    user: User,
    *,
    category_id: int | None,
    community_id: int | None = None,
    community_scoped: bool = False,
    title_prefix: str | None = None,
    link_context: LinkContext | None = None,
    transfers: TransferGrants | None = None,
) -> None:
    """
    Browse file areas within a category (or the top level), mirroring
    `netbbs.net.board_flow._browse_boards_in_category` exactly — same
    reasoning, same two-level cap, same category/item ID-namespace
    disambiguation trick (negated category IDs), and the same
    `community_id`/`community_scoped`/`title_prefix` Community-filter
    threading (design doc §16). See that function's docstring
    for the full rationale.

    Sort mode (design doc, dogfood feature request): like
    `netbbs.boards.boards.list_boards`, `list_file_areas` already
    supports every mode directly against real, persisted columns, so a
    mode switch is just re-calling `_load` with a different `order_by`
    -- no in-memory state to separately combine in, unlike
    `netbbs.net.chat_flow._pick_channel`. `get_effective_sort_mode`
    resolves against this call's own `category_id`/Community scope.
    This module is fully `lane`-based (see its own docstring), so
    persistence goes through `lane.run` like every other write here.
    """

    def _load(db: Database, order_by: str) -> tuple[list[FileArea], list[FileAreaCategory], str | None, str | None]:
        # name_requirement deliberately does not gate reading here --
        # same participation-vs-content-restriction split as
        # netbbs.net.board_flow._browse_boards_in_category (design doc
        # §18); see the upload check in _show_area for where it
        # actually applies. Bundled into one function so a single
        # lane.run() call does the filtering on the worker thread,
        # rather than fetching the raw list and filtering back on the
        # event loop.
        all_areas = [
            a for a in list_file_areas(db, order_by=order_by)
            if meets_read_gate(db, user, a)
            and resource_age_visible(db, user, a)
        ]
        if community_scoped:
            all_areas = [a for a in all_areas if a.community_id == community_id]
        areas_here = [a for a in all_areas if a.category_id == category_id]

        categories_here = (
            list_top_level_categories(db) if category_id is None else list_subcategories(db, category_id)
        )
        if community_scoped:
            used_category_ids = {a.category_id for a in all_areas if a.category_id is not None}
            if category_id is None:
                categories_here = [
                    c for c in categories_here
                    if c.id in used_category_ids
                    or any(sub.id in used_category_ids for sub in list_subcategories(db, c.id))
                ]
            else:
                categories_here = [c for c in categories_here if c.id in used_category_ids]

        category_name = get_category_by_id(db, category_id).name if category_id is not None else None
        community = get_community(db, effective_community_id)
        return areas_here, categories_here, category_name, community.name if community is not None else None

    effective_community_id = community_id if community_scoped else None
    current_mode = await lane.run(
        get_effective_sort_mode, user, "file_area", community_id=effective_community_id, category_id=category_id
    )
    _, _, category_name, community_name = await lane.run(_load, current_mode)
    description_level = await lane.run(menu_description_level, user)
    redraw_in_place = await lane.run(redraw_in_place_enabled, user)
    unicode_style = await lane.run(unicode_style_enabled, user)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, user)
    # GitHub issue #176: resolved once, reused for both pick_item calls
    # below (flat and mixed-with-categories) -- shows at every level of
    # file-area browsing this recursive function reaches (top level, a
    # category, a Community's scope), matching
    # `board_flow._browse_boards_in_category`'s own identical wiring.
    area_masthead = await lane.run(load_file_area_banner, max_width=physical_terminal_width(session))
    # The SysOp's art as the list itself (issue #929), or None.
    area_slot_art = await lane.run(load_file_area_slot_art)
    # The list's art plays at its speed on the first visit (issue #929).
    area_art_speed = await lane.run(art_speed, FILE_AREA)
    area_slot_fields = (
        await lane.run(lambda db: list_slot_fields(session, db, user)) if area_slot_art is not None else None
    )
    # Each area's one value in the art, read in one pass on the worker
    # thread: the gate note when the caller can't upload yet (design doc
    # §3.6), else how many files it holds.
    area_slot_values: dict[int, str] = {}

    def _slot_values(db: Database, areas: list[FileArea]) -> dict[int, str]:
        values = {}
        for area in areas:
            if resource_needs_verification(db, user, area):
                values[area.id] = NAME_GATE_NOTE
            else:
                count, _ = count_listed_files(db, area)
                values[area.id] = f"{count} file{'' if count == 1 else 's'}"
        return values

    def _slot_column_of(item: FileAreaCategory | FileArea) -> str:
        return area_slot_values.get(item.id, "") if isinstance(item, FileArea) else ""
    mode_box = {"mode": current_mode}

    async def _persist_sort_choice(mode: str, scope_kwargs: dict) -> None:
        await lane.run(set_sort_preference, user, "file_area", mode, **scope_kwargs)

    async def _run_sort_prompt() -> str | None:
        return await prompt_sort_change(
            session, persist=_persist_sort_choice,
            community_id=effective_community_id, community_name=community_name,
            category_id=category_id, category_name=category_name, sysop_order=True,
        )

    def _sort_label() -> str:
        return SORT_MODE_LABELS[mode_box["mode"]]

    title = "File areas" if title_prefix is not None else "Available file areas"
    picker_breadcrumb = (title_prefix,) if title_prefix is not None else ()

    # Back from an area or a category comes back to this list, on the row
    # left (issue #839), as the board list does.
    reopen_at: int | None = None
    about_separator = " · " if unicode_style else " - "
    while True:
        areas_here, categories_here, _, _ = await lane.run(_load, mode_box["mode"])
        if area_slot_art is not None:
            area_slot_values.update(await lane.run(_slot_values, areas_here))
        # Which areas will ask this caller for a verification they lack --
        # a verified name to upload, or a verified age to enter (issue
        # #1082) -- named in the row as the board list does (design doc
        # §3.6), read once per list on the worker thread.
        needs_verification = await lane.run(
            lambda db: {a.id for a in areas_here if resource_needs_verification(db, user, a)}
        )

        linked_areas = await lane.run(linked_area_ids)

        def _linked(item: FileAreaCategory | FileArea, linked_areas: set[int] = linked_areas) -> bool:
            # Issue #1104: a Linked area's name takes the Linked colour.
            return isinstance(item, FileArea) and item.id in linked_areas

        def _area_about(area: FileArea) -> str | None:
            parts = [NAME_GATE_NOTE] if area.id in needs_verification else []
            if area.description:
                parts.append(area.description)
            return about_separator.join(parts) or None
        if not categories_here:
            async def on_sort_flat() -> list[FileArea] | None:
                new_mode = await _run_sort_prompt()
                if new_mode is None:
                    return None
                mode_box["mode"] = new_mode
                new_areas, _, _, _ = await lane.run(_load, new_mode)
                return new_areas

            area = await pick_item(
                session,
                areas_here,
                name_of=lambda a: a.name,
                stable_id_of=lambda a: a.id,
                description_of=_area_about,
                title=title,
                breadcrumb=picker_breadcrumb,
                empty_message="No file areas are available to you yet.",
                on_sort=on_sort_flat,
                sort_label=_sort_label,
                description_level=description_level,
                redraw_in_place=redraw_in_place,
                unicode_style=unicode_style,
                collapsed=collapsed,
                accent_color=await lane.run(effective_accent_color_256),
                header_color=await lane.run(effective_header_color_256),
                masthead=area_masthead,
                start_stable_id=reopen_at,
                slot_art=area_slot_art,
                slot_column_of=_slot_column_of,
                slot_fields=area_slot_fields,
                art_speed=area_art_speed,
                art_once=FILE_AREA,
                linked_of=_linked,
            )
            if area is None:
                return
            reopen_at = area.id
            await _show_area(session, lane, area, user, link_context=link_context, transfers=transfers)
            continue

        mixed: list[FileAreaCategory | FileArea] = [*categories_here, *areas_here]

        def render_name(item: FileAreaCategory | FileArea) -> str:
            return f"[{item.name}]" if isinstance(item, FileAreaCategory) else item.name

        def render_description(item: FileAreaCategory | FileArea) -> str | None:
            if isinstance(item, FileAreaCategory):
                return item.description or "(category)"
            return _area_about(item)

        def stable_id(item: FileAreaCategory | FileArea) -> int:
            return item.id if isinstance(item, FileArea) else -item.id

        async def on_sort_mixed() -> list[FileAreaCategory | FileArea] | None:
            new_mode = await _run_sort_prompt()
            if new_mode is None:
                return None
            mode_box["mode"] = new_mode
            new_areas, _, _, _ = await lane.run(_load, new_mode)
            return [*categories_here, *new_areas]

        selected = await pick_item(
            session,
            mixed,
            name_of=render_name,
            stable_id_of=stable_id,
            on_sort=on_sort_mixed,
            sort_label=_sort_label,
            description_of=render_description,
            title=title,
            breadcrumb=picker_breadcrumb,
            empty_message="No file areas are available to you yet.",
            description_level=description_level,
            redraw_in_place=redraw_in_place,
            unicode_style=unicode_style,
            collapsed=collapsed,
            accent_color=await lane.run(effective_accent_color_256),
            header_color=await lane.run(effective_header_color_256),
            masthead=area_masthead,
            start_stable_id=reopen_at,
            slot_art=area_slot_art,
            slot_column_of=_slot_column_of,
            slot_fields=area_slot_fields,
            art_speed=area_art_speed,
            art_once=FILE_AREA,
            linked_of=_linked,
        )
        if selected is None:
            return
        reopen_at = stable_id(selected)

        if isinstance(selected, FileAreaCategory):
            await _browse_areas_in_category(
                session, lane, user, category_id=selected.id,
                community_id=community_id, community_scoped=community_scoped, title_prefix=title_prefix,
                link_context=link_context, transfers=transfers,
            )
        else:
            await _show_area(session, lane, selected, user, link_context=link_context, transfers=transfers)


def _format_size(size_bytes: int) -> str:
    """
    Human-readable file size, binary (KiB/MiB/GiB) units — matches what
    most file managers and BBS file listings show, rather than raw byte
    counts once a file is more than a few hundred bytes.
    """
    return file_size_text(size_bytes)


def _file_column_widths(terminal_width: int, *, uploader_need: int | None = None) -> tuple[int, int, int, int, int]:
    """Returns (idx_w, name_w, size_w, date_w, uploader_w) for columnar file listing.

    `uploader_need` is the widest uploader on the page. The uploader column
    takes only that much, up to its usual width, and the filename gets the
    rest (issue #842): it is what a caller reads a file list for, and at 80
    columns it had 18 against the uploader's 28, so
    "copperplate-minuscules-week1.png" lost its week and its extension
    beside an uploader column holding "Copperplate". A verified name in
    `(=...=)` still gets the full width it had."""
    idx_w = 4
    size_w = 9
    date_w = 16
    uploader_max = 16 if terminal_width < 80 else 28 + min(10, (terminal_width - 80) // 4)
    uploader_w = uploader_max if uploader_need is None else max(len("Uploader"), min(uploader_max, uploader_need))
    # The row is the index label (five columns with its cursor marker),
    # the other four cells and four single-space gutters.
    name_w = terminal_width - (5 + size_w + date_w + uploader_w + 4)
    if terminal_width < 80:
        name_w = max(12, name_w - 1)
    return idx_w, name_w, size_w, date_w, uploader_w


def cut_filename(name: str, width: int, *, ellipsis: str = "...") -> str:
    """`name` fitted to `width` columns with its end kept (issue #842).

    A filename cut at the end loses exactly what tells two files apart --
    "week1.png" from "week2.png" -- and the extension that says what the
    file is. So the cut is taken out of the middle, keeping the extension
    and a few characters before it."""
    if visible_width(name) <= width:
        return name
    room = width - visible_width(ellipsis)
    if room < 4:
        return cut_to_width(name, width)
    dot = name.rfind(".")
    extension = name[dot:] if 0 < dot and visible_width(name[dot:]) <= 10 else ""
    tail_room = min(visible_width(extension) + 6, room // 2)
    tail = ""
    for ch in reversed(name):
        if visible_width(ch + tail) > tail_room:
            break
        tail = ch + tail
    return cut_to_width(name, room - visible_width(tail)) + ellipsis + tail


def _gates_note(
    session: Session, gates: tuple[str, ...], *, unicode_style: bool, unmet: frozenset[str] = frozenset()
) -> str | None:
    """The area's gates as one line under its title (issue #1105), or
    `None` when it has none; the ones this caller does not meet, such as
    an upload level, are marked (issue #1115)."""
    return gates_line(
        gates, width=session.terminal_width, unicode_style=unicode_style,
        ellipsis=ellipsis_for(session, unicode_style=unicode_style), unmet=unmet,
    )


async def _render_area_page(
    session: Session,
    lane: DatabaseLane,
    area_name: str,
    page: FileEntryPage,
    *,
    can_write: bool,
    name_requirement: str | None,
    can_describe: bool = False,
    describable_pending: bool = False,
    show_transfer_hint: bool = False,
    show_remote_hint: bool = False,
    can_pin: bool = False,
    can_keep: bool = False,
    following: bool | None = None,
    description_level: str = "off",
    redraw_in_place: bool = False,
    unicode_style: bool = False,
    collapsed: bool = False,
    truecolor: bool = False,
    highlighted: int | None = None,
    queue_count: int = 0,
    gates: tuple[str, ...] = (),
    unmet: frozenset[str] = frozenset(),
) -> None:
    """Renders one page of files plus its navigation options and command
    hints — the unit that should be redrawn on an actual page change
    (initial entry, Older/Newer/Recent), not on every loop iteration
    regardless of whether anything changed."""
    await _render_file_page(
        session, lane, area_name, page, name_requirement=name_requirement, redraw_in_place=redraw_in_place,
        unicode_style=unicode_style, collapsed=collapsed, truecolor=truecolor, highlighted=highlighted,
        gates=gates, unmet=unmet,
    )
    options, hints = _area_page_menus(
        session, page, can_write=can_write, can_describe=can_describe,
        describable_pending=describable_pending, show_transfer_hint=show_transfer_hint,
        show_remote_hint=show_remote_hint, can_pin=can_pin, can_keep=can_keep, following=following,
        queue_count=queue_count,
    )
    await session.write_line(
        f"\r\n{_menu_row(options, width=session.terminal_width, height=session.terminal_height, description_level=description_level)}"
    )
    if hints:
        # An empty page with nothing this caller may do on it has no
        # hints at all, and a blank row is not a menu.
        await session.write_line(
            _menu_row(
                hints, width=session.terminal_width, height=session.terminal_height,
                description_level=description_level,
            )
        )
    await _write_choice_prompt(session)


def _area_page_menus(
    session: Session,
    page: FileEntryPage,
    *,
    can_write: bool,
    can_describe: bool = False,
    describable_pending: bool = False,
    show_transfer_hint: bool = False,
    show_remote_hint: bool = False,
    can_pin: bool = False,
    can_keep: bool = False,
    following: bool | None = None,
    queue_count: int = 0,
) -> tuple[list[MenuEntry], list[MenuEntry]]:
    """The listing's two menu rows, navigation and actions: what
    `_render_area_page` draws and what its help describes."""
    options = []
    if page.has_older:
        options.append(MenuEntry(label=menu_key("<", " Older"), brief="Show older files"))
    if page.has_newer:
        options.append(MenuEntry(label=menu_key(">", " Newer"), brief="Show newer files"))
        options.append(MenuEntry(label=menu_key("R", "ecent"), brief="Jump to the newest page"))
    options.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to the previous menu"))

    # Named for what this caller's transport can actually do (issue
    # #475): telling a browser caller to "receive via Zmodem" describes
    # a transfer their client cannot start, which is how the file area
    # came to look broken to most people in the first place.
    zmodem = supports_zmodem(session)
    _receive_how = "receive via Zmodem" if zmodem else "get a browser download link"
    # Short enough for one menu column (issue #842): at 120 columns the
    # hints split into three, each with about 34 columns for its brief, and
    # "Get a browser download link for the highlighted file" was cut to
    # "...for the hi".
    _receive_one = "Receive this file via Zmodem" if zmodem else "Browser link for this file"
    _send_how = "Send a file via Zmodem" if zmodem else "Send a file from your browser"

    n_files = len(page.entries)
    hints = []
    if n_files > 0:
        hints.append(MenuEntry(label=menu_key(row_range_label(n_files), f" — {_receive_how}")))
        hints.append(MenuEntry(label=menu_key("D", "ownload"), brief=_receive_one))
    if can_write:
        hints.append(MenuEntry(label=menu_key("U", "pload"), brief=_send_how))
    if can_describe:
        # Says what the key can actually reach: with an upload of the
        # caller's own waiting for approval, `[E]`'s picker offers that
        # too, and it is invisible in this approved-only listing -- so
        # "the highlighted file" would be the one description under
        # which they would never think to press it.
        hints.append(
            MenuEntry(
                label=menu_key("E", "dit description"),
                brief="Describe a file or waiting upload" if describable_pending
                else "Describe the highlighted file",
            )
        )
    if show_transfer_hint and zmodem:
        hints.append(
            MenuEntry(label=menu_key("W", "eb transfer"), brief="Browser link instead of Zmodem")
        )
    if show_remote_hint:
        hints.append(
            MenuEntry(
                label=menu_key("L", "ink catalogue"),
                brief="Fetch files other nodes offer",
            )
        )
    if queue_count:
        hints.append(_queue_entry(queue_count))
    if following is not None:
        # Issue #675: a followed area is listed first in [N]ew scan.
        hints.append(
            MenuEntry(label=menu_key("F", "ollow: on"), brief="Stop following this file area") if following
            else MenuEntry(label=menu_key("F", "ollow: off"), brief="List this area first in New scan")
        )
    if can_pin and n_files > 0:
        # `[O]n top`, as a post's is on a board (issue #1158).
        hints.append(MenuEntry(label=menu_key("O", "n top"), brief="Pin or unpin a file at the top"))
        if can_keep:
            hints.append(MenuEntry(label=menu_key("K", "eep"), brief="Keep a file from expiring, or stop"))
    return options, hints


# This screen's one prompt string, written by whoever last put
# something on screen -- never by the key reader.
#
# Dogfood report: `_read_file_choice` used to write this itself, once
# per call, and the loop below calls it once per keystroke. A key the
# screen does not handle bells and changes nothing, so the next
# iteration wrote a second copy with no newline between them, and a
# caller leaning on Enter got
#
#     Choice: Choice: Choice: ...
#
# marching across the line. The rule every other interactive loop in
# the codebase already follows (`netbbs.net.picker`'s docstring states
# it outright) is that a prompt belongs to a *render*: an action that
# changes nothing leaves the screen exactly as it was, and the bell is
# the whole response.
#
# The wrinkle specific to this screen is that not every rejection
# happens with the prompt still intact. `read_editor_key` does not
# echo, so an unhandled key leaves the cursor sitting right after the
# prompt and there is nothing to reprint -- but the keys this screen
# *does* recognize echo themselves with a trailing newline before
# dispatching. Those have scrolled the prompt away by the time they
# turn out to be refusable (`o` on a page with no older files, `e`
# where nothing is describable), so they reprint it deliberately.
_CHOICE_PROMPT = "Choice: "


async def _write_choice_prompt(session: Session) -> None:
    # Whatever the caller's last action announced sits right above the
    # prompt (issue #680): the screen was just redrawn, and a line written
    # before the redraw is gone.
    await write_notices(session)
    await session.write(_CHOICE_PROMPT)


async def _reject_after_echo(session: Session) -> None:
    """Refuse an action whose own keystroke already echoed a newline:
    bell, then put the prompt back, because the one that was on screen
    has scrolled up out of reach."""
    await session.write("\a")
    await _write_choice_prompt(session)


# Paging is `<` `>` on every screen (issue #1158); a hotkey read turns
# PgUp/PgDn and the arrows into the same two characters.
_NAV_KEYS = {"b": "back", "<": "older", ">": "newer", "r": "recent"}


def _queue_entry(count: int) -> MenuEntry:
    """[Q]ueue for a caller who may approve uploads here (issue #678), with
    how many wait."""
    return MenuEntry(label=menu_key("Q", f"ueue ({count})"), brief="Approve or reject held uploads")


def _key_action(
    char: str, page: FileEntryPage, highlighted: int | None
) -> tuple[str, FileEntry | None, int | None] | None:
    """Map one printable keystroke to this screen's action, or `None`
    when the screen does not handle it.

    Shared by both input paths in `_read_file_choice` so a transport
    without editor-key support answers to exactly the same keys instead
    of to a second dialect of this screen.
    """
    lowered = char.lower()
    # A row number is two keys, read by `_numbered_download` before this.
    if lowered in _NAV_KEYS:
        return (_NAV_KEYS[lowered], None, highlighted)
    if lowered == "d":
        # No entry named: `[D]` means "the one I am looking at", and the
        # loop resolves it from the cursor, a single-entry page, or a
        # picker -- the same resolution `[E]` already uses.
        return ("download", None, highlighted)
    if lowered == "e":
        return ("describe", None, highlighted)
    if lowered == "u":
        return ("upload", None, highlighted)
    if lowered == "w":
        return ("weblink", None, highlighted)
    if lowered == "l":
        return ("remote", None, highlighted)
    if lowered == "o":
        return ("pin", None, highlighted)
    if lowered == "k":
        return ("keep", None, highlighted)
    if lowered == "f":
        return ("follow", None, highlighted)
    if lowered == "q":
        return ("queue", None, highlighted)
    return None


def _is_digit(char: str) -> bool:
    # `isascii()` as well as `isdigit()`: `str.isdigit` is true for
    # characters `int()` then refuses -- `'²'.isdigit()` is `True` and
    # `int('²')` raises `ValueError` -- and AltGr+2 on a German keyboard
    # sends exactly that, which would have taken the session down from a
    # keystroke on this screen.
    return len(char) == 1 and char.isascii() and char.isdigit()


async def _numbered_download(
    session: Session, page: FileEntryPage, first: str, highlighted: int | None, *, first_echoed: bool, read,
) -> tuple[str, FileEntry | None, int | None]:
    """A file by its row number: two digits, or one and Enter (issue #1158)."""
    number = await read_row_number(session, first, row_count=len(page.entries), first_echoed=first_echoed, read=read)
    if number is None:
        return ("none", None, highlighted)
    await session.write_line("")
    return ("download", page.entries[number - 1], None)


async def _read_file_choice(
    session: Session,
    page: FileEntryPage,
    highlighted: int | None,
) -> tuple[str, FileEntry | None, int | None]:
    """Read one keystroke: a hotkey, a file-number shortcut, or cursor
    navigation.

    Returns:
      ('back'|'older'|'newer'|'recent', None, highlighted) - navigation
      ('download', entry, _) - download that file (a number, or Enter
          on the cursor)
      ('download', None, highlighted) - `[D]`, target still to resolve
      ('upload', None, highlighted) - start an upload
      ('describe', None, highlighted) - edit a description (issue #463)
      ('weblink', None, highlighted) - the browser-transfer screen
      ('remote', None, highlighted) - the Link catalogue (issue #92)
      ('pin'|'keep', None, highlighted) - a moderator's pin or expiry
          exemption toggle (issue #675), target still to resolve
      ('follow', None, highlighted) - follow the area or stop (issue #675)
      ('queue', None, highlighted) - the area's moderation queue (issue #678)
      ('refresh', None, highlighted) - re-query and redraw (Ctrl-L)
      ('help', None, highlighted) - the screen's help (`?`, F1, Ctrl-H)
      ('highlight', None, new_index) - arrow key highlight change
      ('none', None, highlighted) - no-op / rejected key

    Every action here is a keystroke, like every other menu in NetBBS
    (design doc §3.5). This screen used to read whole lines and carry
    `/download`, `/upload`, `/describe`, `/weblink` and `/remote`
    command forms, a dialect no other screen spoke: it predates its own
    editor-key support (issue #184's numbered download shortcuts), and
    when keys arrived only navigation and download were given them.
    `/download <name>` was the one form that reached a file on another
    page; `[/] Find` reaches it instead, landing in this area with that
    file at the top of its page (as row 1, not as a preselected cursor
    -- this screen always starts with no highlight).

    Deliberately writes no prompt of its own: the prompt belongs to
    whatever last rendered the screen (see `_CHOICE_PROMPT`). Returning
    `('none', ...)` means the screen is unchanged and the bell already
    rung here is the entire response.
    """
    read_editor_key = getattr(session, "read_editor_key", None)
    if read_editor_key is not None:
        try:
            # Ctrl-H, F1 and `?` as help (issue #1158): without the flag,
            # Ctrl-H arrived as Backspace and F1 not at all.
            key = await read_editor_key(distinguish_ctrl_h=True)
            if key.kind == EditorKeyKind.DOWN:
                if not page.entries:
                    await session.write("\a")
                    return ("none", None, highlighted)
                if highlighted is None:
                    return ("highlight", None, 0)
                elif highlighted < len(page.entries) - 1:
                    return ("highlight", None, highlighted + 1)
                else:
                    await session.write("\a")
                    return ("none", None, highlighted)
            elif key.kind == EditorKeyKind.UP:
                if not page.entries:
                    await session.write("\a")
                    return ("none", None, highlighted)
                if highlighted is None:
                    return ("highlight", None, len(page.entries) - 1)
                elif highlighted > 0:
                    return ("highlight", None, highlighted - 1)
                else:
                    await session.write("\a")
                    return ("none", None, highlighted)
            elif key.kind == EditorKeyKind.ENTER:
                if highlighted is not None and 0 <= highlighted < len(page.entries):
                    await session.write_line("")
                    return ("download", page.entries[highlighted], highlighted)
                else:
                    await session.write("\a")
                    return ("none", None, highlighted)
            elif key.kind == EditorKeyKind.ESCAPE:
                if highlighted is not None:
                    return ("highlight", None, None)
                return ("back", None, highlighted)  # Esc is Back (issue #1158)
            elif key.kind != EditorKeyKind.CHAR and page_step(key) is not None:
                action = _key_action("<" if page_step(key) < 0 else ">", page, highlighted)
                if action is None:
                    await session.write("\a")
                    return ("none", None, highlighted)
                await session.write_line("")
                return action
            elif key.kind == EditorKeyKind.CHAR and key.char and _is_digit(key.char):

                async def _read_structured() -> tuple[EditorKey, bool]:
                    return await read_editor_key(distinguish_ctrl_h=True), False

                return await _numbered_download(
                    session, page, key.char, highlighted, first_echoed=False, read=_read_structured,
                )
            elif key.kind == EditorKeyKind.CHAR and key.char:
                action = _key_action(key.char, page, highlighted)
                if action is None:
                    # `read_editor_key` echoes nothing, so the prompt is
                    # still intact and the bell is the whole response.
                    await session.write("\a")
                    return ("none", None, highlighted)
                await session.write_line(key.char)
                return action
            elif key.kind == EditorKeyKind.CTRL and key.char == "l":
                # Ctrl-L redraws with fresh data, which is also what the
                # browser page sends once an upload it is handling
                # finishes (Codex review): without a signal this loop
                # acts on, the file the caller just sent stays invisible
                # until they leave the area and come back.
                return ("refresh", None, highlighted)
            elif key.kind == EditorKeyKind.CTRL and key.char == "h":
                return ("help", None, highlighted)
            else:
                await session.write("\a")
                return ("none", None, highlighted)
        except (NotImplementedError, AttributeError):
            pass

    # A transport with no editor-key support gets the same keys without
    # the cursor, never the typed command line this screen used to fall
    # back to. `read_key` echoes the character itself, so an accepted
    # key owes only the newline, and `reject_unhandled_key` erases what
    # a refused one drew before ringing the bell.
    char = await session.read_key()
    if char == REDRAW_KEY:
        # The same redraw the editor-key path reports for Ctrl-L. It
        # cannot live in `_key_action`: through `read_editor_key` this
        # key arrives as an `EditorKeyKind.CTRL` event rather than as
        # this byte, so the two paths recognize it in their own idiom
        # and agree on the answer. `read_key` returns it unechoed, so
        # there is nothing to erase and no newline to owe.
        return ("refresh", None, highlighted)
    if char == HELP_KEY:
        return ("help", None, highlighted)
    if _is_digit(char):

        async def _read_plain() -> tuple[EditorKey, bool]:
            return EditorKey(EditorKeyKind.CHAR, char=await session.read_key()), True

        return await _numbered_download(session, page, char, highlighted, first_echoed=True, read=_read_plain)
    action = _key_action(char, page, highlighted)
    if action is None:
        await session.write(reject_unhandled_key(char))
        return ("none", None, highlighted)
    await session.write_line("")
    return action


@records_activity(lambda args: args["area"].name)
async def _show_area(
    session: Session,
    lane: DatabaseLane,
    area: FileArea,
    user: User,
    *,
    initial_cursor: tuple[str, str] | None = None,
    link_context: LinkContext | None = None,
    transfers: TransferGrants | None = None,
) -> None:
    """
    Show `area`, one bounded page of files at a time (design doc,
    issue #10's file-area follow-up to the board-post pagination) —
    mirrors `netbbs.net.board_flow._show_board`'s
    pagination *semantics* exactly: same newest-first default, same
    `[<] Older`/`[>] Newer`/`[R]ecent`/`[B]ack` options, same reasoning for
    both (see that function's docstring, not repeated here) — including
    only redrawing the listing on an actual page change, and `b` (not a
    bare Enter, which used to also work here but no longer does) as the
    one consistent way back. `initial_cursor` (issue #56's `[N]ew scan`
    "jump to first unread") works identically to `_show_board`'s own:
    overrides only the very first render, falling back to the newest
    page if nothing is newer than the cursor.

    Like `_show_board`, every option is a keystroke (design doc §3.5).
    It reads them through `_read_file_choice` rather than `read_key()`
    directly because this screen also has a cursor: an arrow key moves
    the highlight and Enter downloads whatever it sits on. Until this
    pass the screen read whole *lines* so that it could also carry
    `/download`, `/upload`, `/describe`, `/weblink` and `/remote`
    command forms; those are gone, and the prompt is the ordinary
    `Choice: ` every other menu writes.

    `[D]ownload` (like `[E]`) acts on the file under the cursor, on the
    only file on the page, or on whichever one `pick_item` returns — a
    number key `1`-`5` names one directly. A file on *another* page is
    reached through `[/] Find` (`netbbs.net.scan_and_find`), which enters
    this area positioned on that file; `/download <filename>`'s
    area-wide by-name lookup is what that replaced.

    `[E]` (issue #463) edits a file's description in the caller's own
    editor. Offered only when this caller could actually use it on
    something on screen — their own upload, or any file if they hold
    `BoardPermission.EDIT` on the area — since a hotkey that is always
    refused is worse than one that isn't there.

    `link_context` (design doc, issue #92), if given *and this specific
    area is actually Linked* (`is_area_linked` — Link being enabled
    node-wide is not enough, the same distinction `netbbs.net.admin_flow`'s
    board admin screen already draws between "Link is on" and "this
    board is Linked"), offers `[L]ink catalogue` — browse this area's
    carried-but-not-yet-fetched remote catalogue and fetch one on demand
    (`_browse_remote_files`). Reachable both from the ordinary
    pagination loop and from the "has no files yet" fallback prompt
    below it, since a Linked area can have remote catalogue entries even
    with zero *local* uploads of its own. No extra per-file access check
    is applied inside that sub-screen — entering `_show_area` at all
    already required passing this area's own effective read/age/name-
    requirement gate (enforced by whichever picker offered it), and a
    remote catalogue entry carries no additional moderation state of its
    own to re-check.
    """
    # A file area that wants a verified age is listed for a caller old
    # enough by their own birthdate, marked "needs verification" (issue
    # #1082), so entering it is where they are told what to do -- on the
    # way in, whichever list, scan or search opened it.
    if await lane.run(resource_age_gate, user, area) == "unverified":
        announce(session, age_verification_refusal("This file area"), tone="error")
        return
    area_name = sanitize_text(area.name)
    # Where a jump's cursor starts, set by `_load` (issue #839).
    jump: dict[str, int | None] = {"highlight": None}

    def _load(
        db: Database,
    ) -> tuple[FileEntryPage, str | None, bool, bool, str, bool, bool, bool, bool, bool, list[FileEntry]]:
        # Bundled into one lane call: the page, the effective
        # name_requirement, the can_write gate, whether this area is
        # actually Linked, and the menu-description preference all come
        # from the same worker-thread pass rather than five round trips.
        page = (
            list_files_page(db, area, user, after=initial_cursor, with_pinned=True) if initial_cursor
            else list_files_page(db, area, user, with_pinned=True)
        )
        if initial_cursor and not page.entries:
            # Nothing newer than the cursor -- caught up, not a
            # genuinely empty area; fall back to the newest page.
            page = list_files_page(db, area, user, with_pinned=True)
        elif initial_cursor:
            # The jump's target on the newest page, when it is there: that
            # page, the one an ordinary visit shows, with the cursor on it,
            # as a board does (issue #839, F095).
            newest = list_files_page(db, area, user, with_pinned=True)
            target = page.entries[0].file_id
            # In the dated rows only, as a board does: a match in the
            # pinned block would record the files between it and the newest
            # rows as seen without ever showing them (review on #869).
            on_newest = next(
                (
                    i for i, listed in enumerate(newest.entries)
                    if i >= newest.pinned_count and listed.file_id == target
                ),
                None,
            )
            jump["highlight"] = 0 if on_newest is None else on_newest
            if on_newest is not None:
                page = newest
        effective_name_requirement = get_effective_name_requirement(db, area)
        can_write = (
            meets_write_gate(db, user, area)
            and meets_resource_age(db, user, area)
            and meets_name_requirement(db, user, effective_name_requirement)
        )
        return (
            page, effective_name_requirement, can_write, is_area_linked(db, area),
            menu_description_level(db, user), redraw_in_place_enabled(db, user),
            unicode_style_enabled(db, user), breadcrumb_collapsed_enabled(db, user),
            effective_truecolor(session, db, user),
            # Whether this caller could edit *any* file's description
            # here (issue #463) -- the per-file "or it's your own
            # upload" half is a pure comparison against the page in
            # hand, so only the permission lookup needs the database.
            has_permission(
                db, user, object_type="file_area", object_id=area.id, permission=BoardPermission.EDIT
            ),
            # What this caller has waiting for approval here. Fetched in
            # the same pass whether or not the listing turns out to be
            # empty, because both screens need it: `list_files_page`
            # carries `'approved'` rows only, so a pending upload is
            # invisible in the listing and `[E]` is the only thing that
            # can reach it. `list_pending_files` shows a caller nothing
            # but their own uploads unless they hold APPROVE.
            list_pending_files(db, area, requesting_user=user),
        )

    (
        page, effective_name_requirement, can_write, area_linked, description_level, redraw_in_place,
        unicode_style, collapsed, truecolor, can_edit_any_file, pending_uploads,
    ) = await lane.run(_load)
    # The gates this area applies, named under its title (issue #1105).
    area_gates = await lane.run(resource_gates, area)
    # The ones this caller does not meet, such as the upload level (#1115).
    area_unmet = await lane.run(unmet_gates, user, area)

    def _may_describe(entry: FileEntry) -> bool:
        """Whether this caller's save would actually be accepted for
        `entry` -- the same question `netbbs.files.entries.
        set_file_description` answers, asked before an editor is
        offered rather than after the caller has typed into one (Codex
        review).

        The moderated-area rule is the subtle half: an uploader owns
        their own file's description until a moderator approves it, and
        from then on only an EDIT holder may change what everyone is
        already reading."""
        if can_edit_any_file:
            return True
        if entry.uploader_user_id != user.id:
            return False
        return not (area.moderated and entry.status == "approved")

    describable_pending = [entry for entry in pending_uploads if _may_describe(entry)]

    def _describe_candidates(current_page: FileEntryPage) -> FileEntryPage:
        """Everything `[E]` could act on: the page's own rows, plus this
        caller's uploads still awaiting approval here.

        The pending half is not decoration. `list_files_page` carries
        `'approved'` rows only, so while `/describe <filename>` existed
        an uploader reached their waiting file by naming it -- the
        "no files yet" screen was given its own `[E]` for exactly that
        case (Codex review), and a moderated area that also holds other
        people's approved files renders *non*-empty, so without this the
        one screen that can describe it would never offer to.

        Appended after the listing's rows so a cursor position stays an
        index into the page it was taken from."""
        return replace(current_page, entries=[*current_page.entries, *describable_pending])

    def _can_describe(current_page: FileEntryPage) -> bool:
        """`[E]dit description` is only offered when this caller could
        actually use it on something this screen can reach."""
        return any(_may_describe(entry) for entry in _describe_candidates(current_page).entries)

    show_remote_hint = link_context is not None and area_linked
    follows = {"on": await lane.run(is_following, user, "file_area", area.id)}
    # Issue #678: a caller who may approve uploads here decides on them
    # here, not only a SysOp in the console.
    can_approve = await lane.run(lambda db: has_permission(
        db, user, object_type="file_area", object_id=area.id, permission=BoardPermission.APPROVE
    ))

    async def _queue_count() -> int:
        return await lane.run(count_pending_files, area) if can_approve else 0

    async def _open_queue() -> None:
        from netbbs.net.admin_flow import _pending_files_screen

        await _pending_files_screen(session, lane, user, area, link_context=link_context, transfers=transfers)

    async def _menu_flags(current_page: FileEntryPage) -> dict:
        """What decides the listing's menus, for its render and its help."""
        return dict(
            can_write=can_write, can_describe=_can_describe(current_page),
            describable_pending=bool(describable_pending),
            show_transfer_hint=transfers is not None,
            show_remote_hint=show_remote_hint, can_pin=can_edit_any_file,
            can_keep=_keep_offered(area, current_page), following=follows["on"],
            queue_count=await _queue_count(),
        )

    async def _render_and_advance_cursor(current_page: FileEntryPage, highlighted: int | None = None) -> None:
        """The one place every render in this loop funnels through
        (issue #56) -- advances `user`'s file-area read cursor to
        whatever is now newest on screen.

        Also where an upload that arrived since the last render shows up
        (issue #964): a Zmodem upload, or one through a browser link that
        stored it while this screen waited for a key. The listing is read
        again, with the cursor on the new file, so the "press [E] on the
        listing" the outcome says is something the caller can do."""
        nonlocal page, describable_pending
        arrived = _take_arrival(session, area)
        if arrived is not None:
            page = await lane.run(list_files_page, area, user, with_pinned=True)
            pending_uploads = await lane.run(lambda db: list_pending_files(db, area, requesting_user=user))
            describable_pending = [entry for entry in pending_uploads if _may_describe(entry)]
            current_page = page
            highlighted = next(
                (index for index, listed in enumerate(page.entries) if listed.file_id == arrived), None
            )
            _set_highlight(highlighted)
        await _render_area_page(
            session, lane, area_name, current_page, name_requirement=effective_name_requirement,
            **await _menu_flags(current_page),
            description_level=description_level, redraw_in_place=redraw_in_place,
            unicode_style=unicode_style, collapsed=collapsed, truecolor=truecolor, highlighted=highlighted,
            gates=area_gates, unmet=area_unmet,
        )
        if current_page.entries:
            await lane.run(record_file_area_seen, user, area, current_page.entries[-1])

    def _set_highlight(value: int | None) -> None:
        nonlocal highlighted
        highlighted = value

    highlighted: int | None = None
    if not page.entries:
        header_color = await lane.run(effective_header_color_256)
        heading = screen_title(
            area_name, breadcrumb=(session.node_display_name, "Files"), width=session.terminal_width, clear=redraw_in_place,
            unicode_style=unicode_style, collapsed=collapsed, header_color=header_color,
        node_name_gradient=session.node_name_gradient)
        await session.write_line(f"\r\n{heading}")
        gates_note = _gates_note(session, area_gates, unicode_style=unicode_style, unmet=area_unmet)
        if gates_note:
            await session.write_line(gates_note)
        state = empty_state(
            "This file area has no files yet",
            detail="Uploads and fetched Link files will appear here.",
            width=session.terminal_width,
            header_color=header_color,
        )
        await session.write_line(f"\r\n{state}")
    else:
        # A [N]ew scan or [/] Find jump starts with the cursor on its target.
        highlighted = jump["highlight"]
        await _render_and_advance_cursor(page, highlighted=highlighted)
        while True:
            kind, target, new_h = await _read_file_choice(session, page, highlighted)

            if kind == "highlight":
                highlighted = new_h
                await _render_and_advance_cursor(page, highlighted=highlighted)
                continue
            elif kind == "none":
                continue
            elif kind == "help":
                options, hints = _area_page_menus(session, page, **await _menu_flags(page))
                await show_menu_help(
                    session, "File area help", [*options, *hints],
                    about=(
                        "The files in this area, newest first. Arrow keys move a highlight; "
                        "Enter or a file's number downloads it."
                    ),
                    header_color=await lane.run(effective_header_color_256), unicode_style=unicode_style,
                )
                await _render_and_advance_cursor(page, highlighted=highlighted)
                continue
            elif kind == "download":
                # A number key or Enter arrives with its entry; `[D]`
                # arrives with none and resolves one the same way `[E]`
                # does -- cursor, only entry, else a picker.
                entry = target if target is not None else await _choose_entry(
                    session, lane, user, page,
                    highlighted=highlighted,
                    title=f"Download a file from {area_name}",
                    empty_message="No files to download.",
                    description_of=_download_choice_description,
                )
                if entry is None:
                    # Backing out of the picker is not a rejection, but
                    # it drew over the listing, so redraw rather than
                    # bell.
                    await _render_and_advance_cursor(page, highlighted=highlighted)
                    continue
                if await send_file_to_caller(session, lane, area, entry, user, transfers=transfers, web_hint=True):
                    # The Zmodem send failed (issue #842): stay on this
                    # list, where [W]eb transfer is the way that works.
                    await _render_and_advance_cursor(page, highlighted=highlighted)
                    continue
                return
            elif kind == "upload":
                if not can_write:
                    await _reject_after_echo(session)
                    continue
                if await _handle_upload(
                    session, lane, area, user, link_context=link_context, transfers=transfers
                ) is None:
                    return
                # Back on the listing whatever happened (issue #964): a
                # finished upload shows up in it, with the cursor on it, so
                # [E] describes it at once; a browser upload is still on its
                # way; a failed one leaves [W]eb transfer here to try.
                await _render_and_advance_cursor(page, highlighted=highlighted)
                continue
            elif kind == "refresh":
                page = await lane.run(list_files_page, area, user, with_pinned=True)
                # An upload that just arrived may be one of this caller's
                # waiting ones (issue #842), which only [E] can reach.
                pending_uploads = await lane.run(lambda db: list_pending_files(db, area, requesting_user=user))
                describable_pending = [entry for entry in pending_uploads if _may_describe(entry)]
                highlighted = None
                await _render_and_advance_cursor(page, highlighted=highlighted)
                continue
            elif kind == "weblink":
                # Gated on the same condition the hint is drawn under
                # (issue #475): on a transport that cannot carry Zmodem
                # at all, `[D]`/`[U]` already hand out browser links
                # themselves, so `[W]` is not offered there -- and §3.5
                # now says a `Choice: ` prompt accepts exactly the keys
                # its action bar shows.
                if transfers is None or not supports_zmodem(session):
                    await _reject_after_echo(session)
                    continue
                await _transfer_link_screen(
                    session, lane, user, area, page,
                    highlighted=highlighted, can_write=can_write, transfers=transfers,
                )
                await _render_and_advance_cursor(page, highlighted=highlighted)
                continue
            elif kind == "describe":
                if not _can_describe(page):
                    await _reject_after_echo(session)
                    continue
                described = await _handle_describe(
                    session, lane, area, user, _describe_candidates(page),
                    highlighted=highlighted, can_edit_any_file=can_edit_any_file,
                    area_linked=area_linked,
                )
                # Only the listing's own rows go back into the loop's
                # page: a pending upload is reachable by `[E]` but is
                # not part of the listing, and rendering it as a row
                # would show everyone's approved-only page a file that
                # is not in it.
                #
                # The pending half is amended in place instead (Claude
                # review). `_describe_candidates` closes over this list,
                # so leaving it alone made a second `[E]` in the same
                # visit offer the pre-edit `FileEntry`: the picker still
                # said "(no description yet)" and the editor reopened on
                # the old text, inviting the caller to overwrite what
                # they had just saved. Neither half is re-queried --
                # this screen's cursor is positional, which is why
                # `_handle_describe` hands the amended row back at all.
                amended = {entry.file_id: entry for entry in described.entries}
                page = replace(page, entries=[amended.get(e.file_id, e) for e in page.entries])
                describable_pending = [amended.get(e.file_id, e) for e in describable_pending]
                await _render_and_advance_cursor(page, highlighted=highlighted)
                continue
            elif kind == "remote":
                if not show_remote_hint:
                    await _reject_after_echo(session)
                    continue
                await _browse_remote_files(session, lane, area, user, link_context)
                return
            elif kind == "back":
                break
            elif kind == "older":
                if not page.has_older or page.oldest_cursor is None:
                    # Every recognized key echoes itself with a newline
                    # before dispatching, so one refused at the edge of
                    # the listing has already scrolled the prompt away.
                    await _reject_after_echo(session)
                    continue
                page = await lane.run(list_files_page, area, user, before=page.oldest_cursor, with_pinned=True)
                highlighted = None
                await _render_and_advance_cursor(page, highlighted=highlighted)
                continue
            elif kind == "newer":
                if not page.has_newer or page.newest_cursor is None:
                    await _reject_after_echo(session)
                    continue
                page = await lane.run(list_files_page, area, user, after=page.newest_cursor, with_pinned=True)
                highlighted = None
                await _render_and_advance_cursor(page, highlighted=highlighted)
                continue
            elif kind == "recent":
                if not page.has_newer:
                    await _reject_after_echo(session)
                    continue
                page = await lane.run(list_files_page, area, user, with_pinned=True)
                highlighted = None
                await _render_and_advance_cursor(page, highlighted=highlighted)
                continue
            elif kind == "queue":
                if not await _queue_count():
                    await _reject_after_echo(session)
                    continue
                listed = (await lane.run(count_visible_files, area))[0]
                await _open_queue()
                # What `[E]` could reach has changed with the decisions
                # (Codex review on #796): a decided upload is no longer one
                # of the caller's waiting ones.
                pending_uploads = await lane.run(lambda db: list_pending_files(db, area, requesting_user=user))
                describable_pending = [entry for entry in pending_uploads if _may_describe(entry)]
                if (await lane.run(count_visible_files, area))[0] != listed:
                    # An approval added to the listing: the newest page
                    # shows it.
                    page = await lane.run(list_files_page, area, user, with_pinned=True)
                    highlighted = None
                # Otherwise -- nothing decided, or only rejections -- the
                # caller is back on the page they left (Codex review on #796).
                await _render_and_advance_cursor(page, highlighted=highlighted)
                continue
            elif kind == "follow":
                if follows["on"]:
                    await lane.run(unfollow, user, "file_area", area.id)
                    announce(session, "No longer following this file area.", tone="muted")
                else:
                    await lane.run(follow, user, "file_area", area.id)
                    announce(session, "Following this file area: New scan lists it first.")
                follows["on"] = not follows["on"]
                await _render_and_advance_cursor(page, highlighted=highlighted)
                continue
            elif kind in ("pin", "keep"):
                if not can_edit_any_file or (kind == "keep" and not _keep_offered(area, page)):
                    await _reject_after_echo(session)
                    continue
                candidates, cursor = page, highlighted
                if kind == "keep" and area.max_file_age_days is None:
                    # Where files no longer expire, [K]eep only undoes a
                    # past exemption: making a new one would quietly keep
                    # a file should expiry be turned back on (Codex review
                    # on #783).
                    kept = [entry for entry in page.entries if entry.exempt_from_expiry]
                    on = page.entries[highlighted] if highlighted is not None and highlighted < len(page.entries) else None
                    cursor = kept.index(on) if on in kept else None
                    candidates = replace(page, entries=kept, pinned_count=0, feed_bounds=None)
                entry = await _choose_entry(
                    session, lane, user, candidates,
                    highlighted=cursor,
                    title=f"{'Pin or unpin' if kind == 'pin' else 'Keep or stop keeping'} a file in {area_name}",
                    empty_message="No files here.",
                    description_of=_download_choice_description,
                )
                changed = (
                    await _toggle_file_flag(session, lane, entry, user, pin=kind == "pin") if entry is not None else None
                )
                if changed is not None:
                    if kind == "pin":
                        # A pin moves the file to the top of the opening
                        # page, an unpin back among the dated files.
                        page = await lane.run(list_files_page, area, user, with_pinned=True)
                        highlighted = None
                    else:
                        # Keeping moves nothing: this page, the row updated
                        # (Codex review on #783).
                        page = replace(
                            page, entries=[changed if e.file_id == changed.file_id else e for e in page.entries]
                        )
                await _render_and_advance_cursor(page, highlighted=highlighted)
                continue
        return

    # This screen has no listing to act on, so [E] resolves its target
    # from what the caller has waiting instead (Codex review): a
    # moderated area holding only their own pending upload renders
    # empty, since `list_files_page` shows nothing unapproved -- and
    # that upload is exactly the one they are most likely to want to
    # describe while it waits. The listing screen above offers the same
    # uploads for the same reason (`_describe_candidates`); this is the
    # case where they are *all* there is.
    #
    # Offered regardless of `can_write` (Codex review): describing your
    # own upload is not writing to the area, and a SysOp who raises the
    # write level after it lands must not strand the file's own
    # uploader with no way to describe it. Already filtered to what this
    # caller's save would actually be accepted for, so a file is never
    # offered here only to be refused after the editor opens -- which
    # also keeps an APPROVE holder from being shown someone else's
    # pending upload as if they could describe it.
    #
    # A caller with none of these actions still gets the screen and a
    # [B]ack bar (issue #680). It used to return straight after drawing
    # the empty state, into the area list's redraw, so the screen flashed
    # and vanished before it could be read.

    hints = []
    if can_write:
        hints.append(
            MenuEntry(
                label=menu_key("U", "pload"),
                brief="Send a file via Zmodem" if supports_zmodem(session) else "Send a file from your browser",
            )
        )
    after_upload = []
    if transfers is not None and supports_zmodem(session):
        # The same key the listing offers (Codex review): an empty area
        # is exactly where a caller whose emulator has no Zmodem needs
        # to put the first file.
        after_upload.append(
            MenuEntry(label=menu_key("W", "eb transfer"), brief="Get a browser upload link")
        )
    if show_remote_hint:
        after_upload.append(
            MenuEntry(
                label=menu_key("L", "ink catalogue"),
                brief="Fetch files other nodes offer",
            )
        )
    back = MenuEntry(label=menu_key("B", "ack"), brief="Return to the previous menu")

    # Its callers see no files, but held uploads may wait (issue #678).
    queued = await _queue_count()

    def _empty_hints() -> list[MenuEntry]:
        # An empty area can be followed too, to be told of its first file
        # (Codex review on #788).
        follow_entry = (
            MenuEntry(label=menu_key("F", "ollow: on"), brief="Stop following this file area")
            if follows["on"]
            else MenuEntry(label=menu_key("F", "ollow: off"), brief="List this area first in New scan")
        )
        # [E] is decided at each draw: an upload arriving while this
        # screen is up can be the caller's own, waiting (issue #842).
        describe = (
            [MenuEntry(label=menu_key("E", "dit description"), brief="Describe your waiting upload")]
            if describable_pending else []
        )
        return [
            *hints, *describe, *after_upload, *([_queue_entry(queued)] if queued else []), follow_entry, back,
        ]

    # Keystrokes and `[B]ack`, like the listing above it and like every
    # other menu (design doc §3.5) -- this used to be a typed
    # "Command (or press Enter to go back): " line carrying the same
    # slash forms the listing did. Each accepted key still acts and
    # leaves; the loop is only so an unrecognized one bells instead of
    # dropping the caller out of the area.
    #
    # Drawn once, outside the loop, and an unrecognized key does not
    # redraw it: `reject_unhandled_key` erases the character the key
    # echoed, so the prompt is left exactly as it was and the bell is
    # the whole response (`_CHOICE_PROMPT` above states the rule).
    # Reprinting per keystroke scrolled "This file area has no files
    # yet" off a short terminal after two stray keys.
    await session.write_line(
        f"\r\n{_menu_row(_empty_hints(), width=session.terminal_width, height=session.terminal_height, description_level=description_level)}"
    )
    await _write_choice_prompt(session)

    async def _still_empty() -> bool:
        """Look at the area again after something that may have given it a
        file (issue #842): Ctrl-L, a browser link, a failed or browser-side
        upload. `False` means it has one now, and the caller opens the
        listing once. Otherwise this screen is drawn again *in this loop*
        (Claude review), never as a fresh `_show_area`: repeating any of
        those keys would stack one screen per keypress until the session
        died of a RecursionError. What an upload announced shows above the
        prompt, and [E] offers an upload of the caller's that now waits."""
        nonlocal describable_pending, queued
        if (await lane.run(count_visible_files, area))[0]:
            return False
        pending_uploads = await lane.run(lambda db: list_pending_files(db, area, requesting_user=user))
        describable_pending = [entry for entry in pending_uploads if _may_describe(entry)]
        queued = await _queue_count()
        await session.write_line(f"\r\n{heading}")
        gates_note = _gates_note(session, area_gates, unicode_style=unicode_style, unmet=area_unmet)
        if gates_note:
            await session.write_line(gates_note)
        await session.write_line(f"\r\n{state}")
        await session.write_line(
            f"\r\n{_menu_row(_empty_hints(), width=session.terminal_width, height=session.terminal_height, description_level=description_level)}"
        )
        await _write_choice_prompt(session)
        return True

    while True:
        # `read_key`, so Enter is discarded with no effect -- the same
        # on this action bar as on every other hotkey menu in NetBBS
        # (`board_flow` included), and §3.5's own rule that `[B]ack`,
        # not a bare Enter, is the one consistent way out. The typed
        # line this replaced did treat Enter as "go back".
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        if choice == REDRAW_KEY:
            # Ctrl-L looks again (issue #842), which is also what the
            # browser page sends once an upload finishes: the area's first
            # file used to arrive while this screen went on saying it had
            # none.
            if await _still_empty():
                continue
            await _show_area(session, lane, area, user, link_context=link_context, transfers=transfers)
            return
        if choice == "u" and can_write:
            await session.write_line("")
            if await _handle_upload(
                session, lane, area, user, link_context=link_context, transfers=transfers
            ) is None:
                return
            # The browser is uploading the area's first file, or a Zmodem
            # upload failed; staying here is the whole point, since this is
            # the screen the file will appear on (Codex review). A finished
            # upload opens the listing, on the new file (issue #964).
            if await _still_empty():
                continue
            await _show_area(session, lane, area, user, link_context=link_context, transfers=transfers)
            return
        if choice == "w" and transfers is not None and supports_zmodem(session):
            await session.write_line("")
            await _transfer_link_screen(
                session, lane, user, area,
                FileEntryPage(entries=[], has_older=False, has_newer=False),
                highlighted=None, can_write=can_write, transfers=transfers,
            )
            # Back to this area, not the list above it (issue #842): an
            # upload link says "press Ctrl-L here to see it", and "here"
            # is where the area's first file will appear.
            if await _still_empty():
                continue
            await _show_area(session, lane, area, user, link_context=link_context, transfers=transfers)
            return
        if choice == "e" and describable_pending:
            await session.write_line("")
            # `[E]` picks from what is waiting, through the same
            # single-entry/picker logic the listing screen uses.
            await _handle_describe(
                session, lane, area, user,
                FileEntryPage(entries=describable_pending, has_older=False, has_newer=False),
                highlighted=None,
                can_edit_any_file=can_edit_any_file, area_linked=area_linked,
            )
            return
        if choice == "l" and show_remote_hint:
            await session.write_line("")
            await _browse_remote_files(session, lane, area, user, link_context)
            return
        if choice == "q" and queued:
            await session.write_line("")
            await _open_queue()
            if (await lane.run(count_visible_files, area))[0]:
                # An approval gave the area its first file: its listing.
                await _show_area(session, lane, area, user, link_context=link_context, transfers=transfers)
                return
            # Still empty: this bar again, in this loop rather than a fresh
            # screen, so backing out of the queue repeatedly does not pile up
            # screens (Codex review on #796).
            queued = await _queue_count()
            await session.write_line(
                _menu_row(
                    _empty_hints(), width=session.terminal_width, height=session.terminal_height,
                    description_level=description_level,
                )
            )
            await _write_choice_prompt(session)
            continue
        if choice == HELP_KEY:
            await show_menu_help(
                session, "File area help", _empty_hints(),
                about="This area has no files yet. Its first upload, or a file fetched over Link, appears here.",
                header_color=header_color, unicode_style=unicode_style,
            )
            # Drawn again as Ctrl-L would: a file may have arrived meanwhile.
            if await _still_empty():
                continue
            await _show_area(session, lane, area, user, link_context=link_context, transfers=transfers)
            return
        if choice == "f":
            await session.write_line("")
            if follows["on"]:
                await lane.run(unfollow, user, "file_area", area.id)
                announce(session, "No longer following this file area.", tone="muted")
            else:
                await lane.run(follow, user, "file_area", area.id)
                announce(session, "Following this file area: New scan lists it first.")
            follows["on"] = not follows["on"]
            # The bar again, so its label says what [F] now does.
            await session.write_line(
                _menu_row(
                    _empty_hints(), width=session.terminal_width, height=session.terminal_height,
                    description_level=description_level,
                )
            )
            await _write_choice_prompt(session)
            continue
        await session.write(reject_unhandled_key(choice))


async def _browse_remote_files(
    session: Session, lane: DatabaseLane, area: FileArea, user: User, link_context: LinkContext
) -> None:
    """
    `[L]ink catalogue` (design doc, issue #92): list every catalogued
    file for `area` -- both fetched and not -- and offer to fetch one
    that isn't local yet. No per-file access check here beyond what already gated
    entering `_show_area` itself (see that function's own docstring) --
    a `RemoteFile` carries no independent moderation state of its own to
    re-check.

    Already-fetched entries are shown, not hidden, so a user can tell
    "this exists in the catalogue and I already have it" from "this
    exists and I don't" at a glance -- the acceptance criterion's own
    "clearly distinguish remote-only content from content already
    fetched/promoted locally."
    """
    remote_files = await lane.run(list_remote_files, area)
    redraw_in_place = await lane.run(redraw_in_place_enabled, user)
    unicode_style = await lane.run(unicode_style_enabled, user)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, user)
    header_color = await lane.run(effective_header_color_256)
    if not remote_files:
        heading = screen_title(
            "Remote catalogue",
            breadcrumb=(session.node_display_name, "Files", sanitize_text(area.name)),
            width=session.terminal_width,
            clear=redraw_in_place,
            unicode_style=unicode_style, collapsed=collapsed, header_color=header_color,
        node_name_gradient=session.node_name_gradient)
        await session.write_line(f"\r\n{heading}")
        state = empty_state(
            "This file area has no remote catalogue entries",
            detail="New Link descriptors will appear here automatically.",
            width=session.terminal_width,
            header_color=header_color,
        )
        await session.write_line(f"\r\n{state}")
        return

    def warned_origins(db: Database) -> set[str]:
        return {
            remote_file.origin_fingerprint
            for remote_file in remote_files
            if (
                (notice := latest_identity_observation(db, remote_file.origin_fingerprint))
                is not None and notice.severity == "security"
            )
        }

    identity_warnings = await lane.run(warned_origins)

    def render_description(remote_file: RemoteFile) -> str:
        status = "[LOCAL] already fetched" if remote_file.fetched_file_id is not None else "[REMOTE] not yet fetched"
        origin = _remote_file_origin_label(link_context, remote_file)
        if remote_file.origin_fingerprint in identity_warnings:
            return (
                f"[IDENTITY CHANGED: {remote_file.origin_fingerprint}] "
                f"{_format_size(remote_file.size_bytes)} — {status} — from {origin}"
            )
        return f"{_format_size(remote_file.size_bytes)} — {status} — from {origin}"

    selected = await pick_item(
        session,
        remote_files,
        name_of=lambda rf: rf.filename,
        stable_id_of=lambda rf: rf.id,
        description_of=render_description,
        title=f"Remote catalogue: {sanitize_text(area.name)}",
        empty_message="No remote catalogue entries.",
        description_level=await lane.run(menu_description_level, user),
        redraw_in_place=redraw_in_place,
        unicode_style=unicode_style,
        collapsed=collapsed,
        accent_color=await lane.run(effective_accent_color_256),
        header_color=header_color,
    )
    if selected is None:
        return

    if selected.fetched_file_id is not None:
        await session.write_line(
            colored(
                f"\r\n{sanitize_text(selected.filename)!r} is already available locally -- press "
                "[D] on it in the file listing to receive it.",
                fg_color=MUTED_COLOR,
            )
        )
        return

    await session.write_line(
        f"\r\n{sanitize_text(selected.filename)!r} ({_format_size(selected.size_bytes)}), not yet fetched."
    )
    identity_notice = await lane.run(
        latest_identity_observation, selected.origin_fingerprint
    )
    if identity_notice is not None and identity_notice.severity == "security":
        await session.write_line(
            colored(
                "Caution: this familiar origin name now has a different cryptographic identity. "
                f"The file origin's technical identity is "
                f"{sanitize_text(selected.origin_fingerprint)}.",
                fg_color=MUTED_COLOR,
                bold=True,
            )
        )
    if not await prompt_yes_no(session, "Fetch it from its origin now?", default=False):
        announce_styled(session, colored("Cancelled.", fg_color=MUTED_COLOR))
        return

    await _fetch_remote_file(
        session, lane, selected, link_context, redraw_in_place=redraw_in_place, unicode_style=unicode_style,
        collapsed=collapsed,
    )


def _remote_file_origin_label(link_context: LinkContext, remote_file: RemoteFile) -> str:
    peer = link_context.link_node.known_identity(remote_file.origin_fingerprint)
    return identity_for_peer(peer).label if peer is not None else remote_file.origin_fingerprint


@records_activity("Downloading")
async def _fetch_remote_file(
    session: Session,
    lane: DatabaseLane,
    remote_file: RemoteFile,
    link_context: LinkContext,
    *,
    redraw_in_place: bool = False,
    unicode_style: bool = False,
    collapsed: bool = False,
) -> None:
    """
    Drives `netbbs.link.transport.fetch_next_file_chunk` in a loop until
    the transfer completes, fails, or its origin turns out to be
    unreachable -- the actual bounded/resumable chunk-transfer path
    (design doc §11.3), not a parallel implementation. Success promotes
    the content into the ordinary local `files` table via that same
    function's own existing verification path; a `files` row is never
    created for content that didn't fully verify (`netbbs.link.file_
    transfer._finalize_transfer`'s own behavior, unchanged by this UI).

    Imports `aiohttp`/`netbbs.link.transport` lazily, inside this
    function -- `netbbs.net.file_flow` is loaded unconditionally by every
    node, including one with `aiohttp` not installed (`pip install
    netbbs[web]`), so nothing at this module's own top level may import
    either; `netbbs.__main__`'s own Link-server startup already
    established this same lazy-import convention for the identical
    reason.
    """
    import aiohttp

    from netbbs.link.file_transfer import FileTransferError
    from netbbs.link.transport import (
        LinkTransportError,
        RemoteFileWithdrawnError,
        dialable_base_urls_for_peer,
        fetch_next_file_chunk,
    )

    if remote_file.origin_fingerprint in link_context.link_node.introduced:
        # Issue #630: the catalogue arrived through a node that carries the
        # area, and its origin is known here only by introduction. "Try again
        # later" would be a promise nothing keeps.
        announce_styled(
            session,
            colored(
                "\r\nThis node has never been in direct contact with this file's origin, and a "
                "file is only ever fetched from its origin directly. It can be listed here, "
                "but not fetched.",
                fg_color=MUTED_COLOR,
            )
        )
        return
    base_urls = dialable_base_urls_for_peer(link_context.link_node, remote_file.origin_fingerprint)
    if not base_urls:
        announce_styled(
            session,
            colored(
                "\r\nThis file's origin is not currently reachable directly (chunk transfer is "
                "never relayed) -- try again later.",
                fg_color=MUTED_COLOR,
            )
        )
        return
    base_url = base_urls[0]

    heading = screen_title(
        "Fetching file",
        breadcrumb=(session.node_display_name, "Files", "Link"),
        subtitle=sanitize_text(remote_file.filename),
        width=session.terminal_width,
        clear=redraw_in_place,
        unicode_style=unicode_style, collapsed=collapsed,
        header_color=await lane.run(effective_header_color_256),
    node_name_gradient=session.node_name_gradient)
    await session.write_line(f"\r\n{heading}")
    transfer = None
    try:
        # trust_env=True: honor HTTP_PROXY/HTTPS_PROXY/NO_PROXY, same as the
        # Link sync session (__main__.py) -- a linked-file fetch is also
        # outbound Link traffic and needs the same forward-proxy path.
        async with aiohttp.ClientSession(trust_env=True) as http_session:
            while True:
                transfer = await fetch_next_file_chunk(
                    link_context.link_node, http_session, base_url, lane, remote_file,
                )
                if transfer.status != "in_progress":
                    break
                await session.write_line(
                    colored(
                        f"  {ellipsis_for(session)} {transfer.bytes_received}/{transfer.total_size} bytes",
                        fg_color=MUTED_COLOR,
                    )
                )
    except RemoteFileWithdrawnError:
        # Design doc §11.2, issue #479: the origin no longer has the file
        # its catalogue entry described, and has said so under its own
        # signature -- `fetch_next_file_chunk` has already dropped the
        # entry. Say which of those two things happened; a generic
        # "transfer failed" invites the caller to keep retrying something
        # that can never work.
        announce_styled(
            session,
            colored(
                f"The origin no longer has {sanitize_text(remote_file.filename)!r} — it was deleted "
                "or expired there. Removed from this area's catalogue.",
                fg_color=ERROR_COLOR,
            )
        )
        return
    except (LinkProtocolError, LinkTransportError, FileTransferError) as exc:
        announce_styled(session, colored(f"Fetch failed: {exc}", fg_color=ERROR_COLOR))
        return

    if transfer.status == "completed":
        announce_styled(
            session,
            colored(
                f"{sanitize_text(remote_file.filename)!r} fetched and verified — it is in this "
                "file area's listing now.",
                fg_color=SUCCESS_COLOR,
            )
        )
    else:
        announce_styled(
            session,
            colored(f"Fetch failed: transfer ended in status {transfer.status!r}.", fg_color=ERROR_COLOR)
        )


def _uploader_display_name(db: Database, entry, *, name_requirement: str | None) -> str:
    """The uploader label to render for one file entry (design doc §18)
    -- mirrors `netbbs.net.board_flow._author_display_name`
    exactly: only looks up the live account when the area actually
    requires `verified_and_displayed` names, otherwise renders the
    plain historical `uploader_label` unchanged, for the identical
    reason (a mutable `display_name` must not retroactively rewrite an
    already-uploaded entry's attribution). Still `db`-first, unchanged
    -- see this module's own docstring for why. A fetched Link file's
    `remote@<origin-fingerprint>` label is presented by the origin
    node's current friendly identity, exactly as a carried post's
    author is."""
    if name_requirement == "verified_and_displayed":
        uploader = get_user_by_id(db, entry.uploader_user_id)
        if uploader is not None:
            return format_name_for_resource(db, uploader, name_requirement=name_requirement)
    return sanitize_text(present_link_author_label(db, entry.uploader_label))


def _uploader_cell(padded: str) -> str:
    """AUTHOR_COLOR over an uploader label that may already carry a
    color of its own.

    A verified real name arrives pre-styled from
    `netbbs.attestation.format_name_for_resource`, and an SGR reset
    restores no outer color -- so wrapping the whole cell would color
    the name, then leave everything after the verified unit uncolored.
    Composed beside it instead of around it, the same way
    `netbbs.net.chat_flow._colored_around` already handles an author
    label that may or may not bring its own styling (issue #298).
    """
    marker = padded.find(chr(27))
    if marker == -1:
        return colored(padded, fg_color=AUTHOR_COLOR)
    return colored(padded[:marker], fg_color=AUTHOR_COLOR) + padded[marker:]


async def _render_file_page(
    session: Session,
    lane: DatabaseLane,
    area_name: str,
    page: FileEntryPage,
    *,
    name_requirement: str | None,
    redraw_in_place: bool = False,
    unicode_style: bool = False,
    collapsed: bool = False,
    truecolor: bool = False,
    highlighted: int | None = None,
    gates: tuple[str, ...] = (),
    unmet: frozenset[str] = frozenset(),
) -> None:
    header_color = await lane.run(effective_header_color_256)
    header = screen_title(
        area_name,
        breadcrumb=(session.node_display_name, "Files"),
        subtitle=f"{len(page.entries)} file{'s' if len(page.entries) != 1 else ''} on this page",
        width=session.terminal_width,
        clear=redraw_in_place,
        unicode_style=unicode_style, collapsed=collapsed,
        header_color=header_color,
        node_name_gradient=session.node_name_gradient,
    )
    await session.write_line(f"\r\n{header}")
    gates_note = _gates_note(session, gates, unicode_style=unicode_style, unmet=unmet)
    if gates_note:
        await session.write_line(gates_note)
    if not page.entries:
        return

    display_format, display_timezone = await lane.run(resolve_display_preferences)
    accent = await lane.run(effective_accent_color_256)
    divider_color = 238 if truecolor else RULE_COLOR
    rule_char = "─" if unicode_style else "-"

    uploaders = [
        await lane.run(_uploader_display_name, entry, name_requirement=name_requirement) for entry in page.entries
    ]
    idx_w, name_w, size_w, date_w, uploader_w = _file_column_widths(
        session.terminal_width, uploader_need=max((visible_width(u) for u in uploaders), default=0),
    )

    header_cols = [
        f"{'#':^4}",
        f"{'Filename':<{name_w}}",
        f"{'Size':>{size_w}}",
        f"{'Date':<{date_w}}",
        f"{'Uploader':<{uploader_w}}",
    ]
    divider_cols = [
        rule_char * 4,
        rule_char * name_w,
        rule_char * size_w,
        rule_char * date_w,
        rule_char * uploader_w,
    ]

    await session.write_line(f"\r\n{colored(' '.join(header_cols), fg_color=header_color, bold=True)}")
    await session.write_line(colored(" ".join(divider_cols), fg_color=divider_color))

    for position, entry in enumerate(page.entries, start=1):
        if page.pinned_count and position == 1:
            # The pinned files under their own labelled rule, as a board's
            # pinned posts are (issue #675).
            label = f"{rule_char * 2} Pinned "
            await session.write_line(colored(
                label + rule_char * max(0, sum(len(c) for c in divider_cols) + 4 - visible_width(label)),
                fg_color=divider_color,
            ))
        if page.pinned_count and position == page.pinned_count + 1:
            await session.write_line(colored(" ".join(divider_cols), fg_color=divider_color))
        is_highlighted = highlighted == (position - 1)
        marker = ">" if is_highlighted else " "
        idx_label = f"{marker}[{row_number_label(position)}]"

        name_clean = sanitize_text(entry.filename)
        if entry.pinned:
            # Pinned files are listed first (issue #675); this says why.
            name_clean = f"pin {name_clean}"
        name_cut = cut_filename(name_clean, name_w, ellipsis=ellipsis_for(session, unicode_style=unicode_style))
        if visible_width(name_cut) < name_w:
            name_padded = name_cut + " " * (name_w - visible_width(name_cut))
        else:
            name_padded = name_cut

        size_str = _format_size(entry.size_bytes)
        size_padded = f"{size_str:>{size_w}}"

        when = format_for_display(entry.created_at, override_format=display_format, override_timezone=display_timezone)
        date_cut = cut_to_width(when, date_w)
        if visible_width(date_cut) < date_w:
            date_padded = date_cut + " " * (date_w - visible_width(date_cut))
        else:
            date_padded = date_cut

        uploader_display = uploaders[position - 1]
        vis_u = visible_width(uploader_display)
        if vis_u <= uploader_w:
            uploader_padded = uploader_display + " " * (uploader_w - vis_u)
        else:
            uploader_padded = colored_truncate([(uploader_display, None)], uploader_w)

        cells = [idx_label, name_padded, size_padded, date_padded, uploader_padded]
        if is_highlighted:
            # One reverse-video bar for the whole row, not per-cell color
            # plus bold (dogfood feedback: "the cursor is small, and the
            # color change highlighting the selected row barely
            # noticeable, not least because it uses the same color as
            # some elements of the line do" -- the highlight was the
            # accent color the filename already had, so the only real
            # signal was the bold). Reverse cannot be composed with the
            # per-cell colors: `colored()` resets at the end of every
            # segment, so a row of colored cells inside one REVERSE would
            # cancel the attribute at the first cell boundary. The cells
            # therefore go out plain inside a single inverted run, which
            # is also what makes the bar solid rather than striped.
            # `strip_ansi`, not the raw cells (Codex review): a verified
            # uploader name brings its own color and its own reset, and
            # that reset would end the reverse run partway along the
            # column -- striping the bar this commit exists to make
            # solid. The `(=...=)` markers survive stripping, and
            # `set_display_name` refuses `=` at write time, so the
            # unforgeability the color also signals is still on screen
            # while the row is under the cursor.
            await session.write_line(colored(strip_ansi(" ".join(cells)), reverse=True))
        else:
            idx_cell = colored(idx_label, fg_color=MENU_KEY_COLOR)
            name_cell = colored(name_padded, fg_color=accent)
            # Three columns that used to be VALUE_COLOR, METADATA_COLOR
            # and no color at all -- two greys and the terminal default,
            # which is how a row of five fields came to read as one run
            # of text. The size is the figure a caller actually compares
            # down the column, so it takes EMPHASIS_COLOR; the date and
            # the uploader are separate facts and now say so.
            size_cell = colored(size_padded, fg_color=EMPHASIS_COLOR)
            date_cell = colored(date_padded, fg_color=DATE_COLOR)
            uploader_cell = _uploader_cell(uploader_padded)
            await session.write_line(" ".join([idx_cell, name_cell, size_cell, date_cell, uploader_cell]))
        # Every line, not just the first (issue #463): a description
        # read out of an archive's FILE_ID.DIZ is up to ten lines of
        # deliberate layout, and collapsing it into one would throw
        # away the thing the convention exists to carry. Capped here as
        # well as at every write path -- a row stored before those
        # existed (a peer's catalogue entry, a dev script) must not be
        # able to decide how tall this page is. A blank line inside a
        # DIZ is written empty rather than as an indent of trailing
        # spaces.
        for line in (entry.description or "").splitlines()[:MAX_DESCRIPTION_LINES]:
            if not line.strip():
                await session.write_line("")
                continue
            await session.write_line(f"      {colored(sanitize_text(line), fg_color=VALUE_COLOR)}")


def _description_draft_path(db: Database, entry: FileEntry, user: User) -> Path:
    """One stable per-(file, caller) draft slot, colocated with every
    other in-progress composition (`netbbs.net.draft_storage`) — the
    file-description counterpart of `netbbs.net.board_flow.
    _post_draft_path`.

    Keyed on the content-addressed `file_id`, not the row id (Codex
    review): drafts outlive the editor that made them, are not deleted
    when their file is, and `files.id` is a plain SQLite rowid a later
    upload can reuse -- which would offer one upload's abandoned draft
    to whoever next describes a different one. Sixteen hex characters
    is plenty to keep those apart and keeps the filename readable."""
    return drafts_directory(db) / f"filedesc_{entry.file_id[:16]}_{user.id}.draft"


async def _compose_description(
    session: Session, lane: DatabaseLane, user: User, entry: FileEntry, *, initial_text: str | None
) -> str | None:
    """
    Enter (or amend) one file's description, in whichever editor this
    caller has opted into — mirrors `netbbs.net.board_flow._compose_
    body`'s dispatch deliberately rather than sharing it: the two have
    genuinely different ceilings (a description is bounded by what a
    listing row and a Link `file_descriptor` can carry, a post body
    isn't), and `netbbs.net.composition`'s own docstring keeps that
    module free of the database lookup the preference needs.

    `None` means "leave the description as it is" — an explicit
    `/cancel`, a fullscreen quit-without-saving, or a
    `/exit`-keeps-the-draft.

    One asymmetry worth knowing rather than hiding: the plain line
    editor refuses to finish on an empty body, so *removing* a
    description outright is only reachable from the fullscreen editor
    (save an empty buffer). Replacing a wrong description with a right
    one works in both, which is the case that actually comes up.
    """
    draft_path = await lane.run(_description_draft_path, entry, user)
    if await lane.run(fullscreen_editor_enabled, user):
        return await edit_prose(
            session, initial_text=initial_text, draft_path=draft_path,
            max_bytes=MAX_DESCRIPTION_BYTES, unicode_style=await lane.run(unicode_style_enabled, user),
            # What is being written, above the text (issue #813).
            header=EditorHeader(
                "File description", (("File", entry.filename),),
                color=await lane.run(effective_header_color_256),
            ),
        )
    return await edit_line_body(
        session, initial_text=initial_text, max_bytes=MAX_DESCRIPTION_BYTES,
        max_lines=MAX_DESCRIPTION_LINES, draft_path=draft_path,
    )


def _describe_choice_description(entry: FileEntry) -> str:
    """One file's row in the `[E]` picker: its current first line, and
    whether it is still waiting for approval.

    The pending marker matters because those rows are the ones *not* in
    the listing behind the picker (`list_files_page` carries `'approved'`
    rows only), so without it a caller cannot tell which of two rows is
    the upload they are waiting on."""
    first_line = (entry.description or "").splitlines()
    current = sanitize_text(first_line[0]) if first_line else "(no description yet)"
    if entry.status == "pending":
        return f"awaiting approval — {current}"
    return current


def _download_choice_description(entry: FileEntry) -> str:
    """One file's row in the `[D]ownload` picker: the size a caller is
    deciding on, plus the first line of its description when it has
    one."""
    size = _format_size(entry.size_bytes)
    first_line = (entry.description or "").splitlines()
    return f"{size} — {sanitize_text(first_line[0])}" if first_line else size


def _keep_offered(area: FileArea, page: FileEntryPage) -> bool:
    """`[K]eep` means something where files expire, or where one was kept
    before the area stopped expiring them."""
    return area.max_file_age_days is not None or any(entry.exempt_from_expiry for entry in page.entries)


async def _toggle_file_flag(
    session: Session, lane: DatabaseLane, entry: FileEntry, user: User, *, pin: bool
) -> FileEntry | None:
    """Flip `entry`'s pin (`pin=True`) or its expiry exemption (issue
    #675), and say what changed above the next screen. Returns the file
    as it now is, or `None` when the change was refused. A pin is this
    node's own presentation, never carried over the Link."""
    name = sanitize_text(entry.filename)
    try:
        if pin:
            changed = await lane.run(set_file_pinned, entry, not entry.pinned, changed_by=user)
            outcome = f"{name} unpinned." if entry.pinned else f"{name} pinned: it is listed first in this area."
        else:
            changed = await lane.run(set_file_exempt, entry, not entry.exempt_from_expiry, changed_by=user)
            outcome = (
                f"{name} no longer kept: it expires with the others." if entry.exempt_from_expiry
                else f"{name} kept: it will not expire."
            )
    except FileEntryError as exc:
        announce(session, f"Not changed: {exc}.", tone="error")
        return None
    announce(session, outcome, tone="success")
    return changed


async def _choose_entry(
    session: Session,
    lane: DatabaseLane,
    user: User,
    page: FileEntryPage,
    *,
    highlighted: int | None,
    title: str,
    empty_message: str,
    description_of: Callable[[FileEntry], str],
) -> FileEntry | None:
    """The file a hotkey acts on: the one under the cursor, the only one
    on the page, else whichever one `pick_item` returns.

    A picker, not a "which one?" prompt in front of the action (design
    doc §3.5, Codex review): `[D]`/`[E]` with nothing under the cursor
    still has to find out which file it means, and the way this codebase
    asks that question is `pick_item` -- backing out of it changes
    nothing, exactly like backing out of the action behind it.

    Shared by `[D]ownload` and `[E]dit description` so one hotkey cannot
    drift into resolving its target differently from the other; it is
    also what `/download <filename>`'s area-wide name lookup was
    replaced with, `[/] Find` being how a file on another page is reached.
    """
    if highlighted is not None and 0 <= highlighted < len(page.entries):
        return page.entries[highlighted]
    if len(page.entries) == 1:
        return page.entries[0]
    return await pick_item(
        session, page.entries,
        name_of=lambda file_entry: sanitize_text(file_entry.filename),
        stable_id_of=lambda file_entry: file_entry.id,
        description_of=description_of,
        title=title,
        empty_message=empty_message,
        redraw_in_place=await lane.run(redraw_in_place_enabled, user),
        unicode_style=await lane.run(unicode_style_enabled, user),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, user),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )


async def _handle_describe(
    session: Session,
    lane: DatabaseLane,
    area: FileArea,
    user: User,
    page: FileEntryPage,
    *,
    highlighted: int | None,
    can_edit_any_file: bool,
    area_linked: bool = False,
) -> FileEntryPage:
    """
    Edit one file's description (issue #463) and hand back the page to
    keep rendering — the same page object with the amended entry
    swapped in, never a re-query: this screen's keyset cursor is
    positional (`netbbs.files.entries.list_files_page`), and re-fetching
    "the current page" after an in-place edit would silently move the
    caller somewhere else.

    Which file: whichever one `_choose_entry` resolves — the
    cursor-highlighted entry, the only entry on the page, else a
    picker. The permission answer comes from the domain
    (`set_file_description`); the check here only decides whether to
    open an editor at all, so nobody types out a description that was
    never going to be saved.

    `area_linked` only changes what is *said* after a successful save
    (issue #464): in a Linked area an approved upload's catalogue entry
    is already signed and pushed by the time anyone edits it, and a
    `file_descriptor` cannot be revised, so the caller is told their
    change is local rather than left to assume otherwise. Whether this
    particular file actually has one is asked of the file itself, since
    an area can be Linked long after some of its files were approved.

    Exactly one editor session per invocation. A save the domain
    rejects leaves the text on disk as a draft and says so, rather than
    looping straight back into an editor that would then ask its own
    recovery question about text typed seconds ago.
    """
    entry = await _choose_entry(
        session, lane, user, page,
        highlighted=highlighted,
        title=f"Describe a file in {sanitize_text(area.name)}",
        empty_message="No files to describe.",
        description_of=_describe_choice_description,
    )
    if entry is None:
        # The picker was backed out of, which changes nothing and has
        # nothing to report.
        return page

    # `[E]` is offered when *any* file on the page is describable, so
    # the one actually chosen may still not be: this is where a caller
    # who picked someone else's file is told so.
    if not can_edit_any_file and entry.uploader_user_id != user.id:
        announce_styled(
            session,
            colored(
                f"\r\n{sanitize_text(entry.filename)!r} was uploaded by someone else — only its "
                "uploader or a moderator of this area can describe it.",
                fg_color=ERROR_COLOR,
            )
        )
        return page
    if entry.uploader_user_id == user.id and authored_earlier_by_shared_account(session, "file", entry.id):
        # Issue #1075: every guest uploads as the one shared account, so a
        # guest describes only what it uploaded during this call.
        announce_styled(session, colored("\r\n" + earlier_guest_refusal("this file"), fg_color=ERROR_COLOR))
        return page
    if not can_edit_any_file and area.moderated and entry.status == "approved":
        # Refused before an editor opens, not after it is filled in
        # (Codex review) -- the domain would reject this save, and the
        # honest place to say so is here.
        #
        # Still reachable, and by a route that did not exist before
        # `[E]` began offering the caller's pending uploads: one waiting
        # upload turns the key on for the whole page, and the cursor may
        # then be sitting on an *approved* file of theirs in the same
        # moderated area. Without this they would type a description and
        # be told afterwards that they lack a permission they never had,
        # which is what `set_file_description` says rather than what
        # actually happened.
        announce_styled(
            session,
            colored(
                f"\r\n{sanitize_text(entry.filename)!r} has already been approved in a moderated "
                "area — ask a moderator to change its description.",
                fg_color=ERROR_COLOR,
            )
        )
        return page

    heading = screen_title(
        "Describe",
        breadcrumb=(session.node_display_name, "Files", sanitize_text(area.name)),
        subtitle=sanitize_text(entry.filename),
        width=session.terminal_width,
        clear=await lane.run(redraw_in_place_enabled, user),
        unicode_style=await lane.run(unicode_style_enabled, user),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, user),
        header_color=await lane.run(effective_header_color_256),
        node_name_gradient=session.node_name_gradient,
    )
    await session.write_line(f"\r\n{heading}")
    await session.write_line(
        colored(
            f"Up to {MAX_DESCRIPTION_LINES} lines, shown under this file in the area listing.",
            fg_color=MUTED_COLOR,
        )
    )

    text = await _compose_description(session, lane, user, entry, initial_text=entry.description)
    if text is None:
        # Both editors return `None` for "cancelled" and for "leaving,
        # keep what I typed", and only the draft file on disk tells
        # them apart (issue #149's own contract) -- so say which one
        # happened rather than reporting a cancel over a draft the
        # caller expects to find again (Codex review).
        kept = await lane.run(_description_draft_path, entry, user)
        if kept.exists():
            announce_styled(
                session,
                colored(
                    "\r\nDraft kept — press [E] on this file again to pick it up.",
                    fg_color=MUTED_COLOR,
                )
            )
        else:
            announce_styled(session, colored("\r\nDescription unchanged.", fg_color=MUTED_COLOR))
        return page

    try:
        updated = await lane.run(set_file_description, entry, text, changed_by=user)
    except FileEntryError as exc:
        # A rejected save must never be a lost draft (design doc §3.5,
        # issue #282's own lesson) -- so the text goes back to disk
        # *before* anything is awaited (Codex review): the editor
        # deleted its own draft on the way out believing the save would
        # take, and until this write it exists only in memory.
        #
        # Reported and returned rather than looped straight back into
        # the editor: reopening over a draft that now exists would make
        # the editor's own recovery prompt ask about text the caller
        # just typed, and a question they did not ask for is exactly
        # what §3.5 is about. One keystroke picks it up again, and they
        # are told which one.
        def _persist(db: Database) -> bool:
            # `save_draft` logs and swallows an unwritable drafts
            # directory, so its return says nothing (Codex review) --
            # and promising a caller their text is safe when it isn't
            # is the one outcome worse than losing it silently.
            #
            # Read back and compared, not merely checked for existence
            # (Codex review again): a full disk or a short write leaves
            # a file that exists and is wrong, and "kept as a draft" has
            # to mean the whole thing.
            path = _description_draft_path(db, entry, user)
            save_draft(path, text)
            try:
                return path.read_text(encoding="utf-8") == text
            except (OSError, UnicodeDecodeError):
                # A short write can leave a truncated multi-byte
                # sequence, and `UnicodeDecodeError` is a `ValueError`
                # (Codex review) -- letting it escape would replace
                # "your text was not kept" with a crash at exactly the
                # moment the caller most needs to be told.
                return False

        kept = await lane.run(_persist)
        announce_styled(session, colored(f"\r\nNot saved: {exc}", fg_color=ERROR_COLOR))
        if kept:
            announce_styled(
                session,
                colored(
                    "Your text is kept as a draft — press [E] on this file again to fix it.",
                    fg_color=MUTED_COLOR,
                )
            )
        else:
            announce_styled(
                session,
                colored(
                    "This node could not keep a draft of it either — copy your text before "
                    "leaving this screen.",
                    fg_color=ERROR_COLOR,
                )
            )
        return page

    announce_styled(
        session,
        colored(f"\r\nDescription saved for {sanitize_text(entry.filename)!r}.", fg_color=SUCCESS_COLOR)
    )
    if area_linked and await lane.run(has_queued_file_descriptor, entry):
        # Said out loud rather than left as a surprise (issues #463 and
        # #464 together): now that an approved upload's catalogue entry
        # is signed and queued the moment it lands, every later edit
        # amends something peers have already been told about, and a
        # `file_descriptor` is immutable.
        #
        # Asked of the file, not of the area (Codex review): one
        # approved before its area was Linked has no descriptor and
        # never will, so telling its describer that peers hold an older
        # wording would simply be false.
        announce_styled(
            session,
            colored(
                "This area is Linked — peers keep the description they were already sent.",
                fg_color=MUTED_COLOR,
            )
        )
    return replace(
        page, entries=[updated if e.file_id == updated.file_id else e for e in page.entries]
    )


def supports_zmodem(session: Session) -> bool:
    """Whether this session's transport can carry a Zmodem transfer at
    all (issue #475).

    Read with `getattr` rather than as an attribute, matching how this
    module already asks about `read_editor_key`: a `Session` here is a
    duck-typed protocol as much as a base class, and a transport (or a
    test double) written before this capability existed means "an
    ordinary byte-carrying terminal", which is the default anyway."""
    return getattr(session, "supports_zmodem", True)


def what_of(direction: str, area: FileArea, entry: FileEntry | None) -> str:
    """How one transfer is described on screen, in both the
    absolute-URL and same-origin paths: what finishes "Open this in a
    browser to ...", so a verb phrase (issue #842 -- it used to read "to
    download of 'x'")."""
    if direction == UPLOAD:
        return f"upload to [{sanitize_text(area.name)}]"
    return f"download {sanitize_text(entry.filename)!r}" if entry is not None else "download"


#: Uploads stored for a session's file area since its listing last drew
#: (issue #964): area id -> the newest arrival's file id. Keyed weakly by
#: session, as notices are, so an upload through a browser link that
#: outlives its caller leaves nothing behind.
_arrivals: "weakref.WeakKeyDictionary[Session, dict[int, int]]" = weakref.WeakKeyDictionary()


def _mark_arrival(session: Session, area: FileArea, entry: FileEntry) -> None:
    """Note that `entry` was stored in `area` for `session`'s caller, so the
    listing reads itself again at its next render (`_take_arrival`)."""
    _arrivals.setdefault(session, {})[area.id] = entry.file_id


def _take_arrival(session: Session, area: FileArea) -> int | None:
    """The file id of an upload to `area` since the listing last drew, if
    any, and forget it."""
    return _arrivals.get(session, {}).pop(area.id, None)


def _upload_outcome(entry: FileEntry, area_name: str, *, accent: int) -> str:
    """The line that says an upload arrived (issue #842), with the area it
    went to in the node's accent color (issue #964): the name a caller who
    uploads to several areas needs to see, not buried in one green line."""
    return (
        colored(
            f"Uploaded {sanitize_text(entry.filename)!r} ({_format_size(entry.size_bytes)}) to ",
            fg_color=SUCCESS_COLOR,
        )
        + colored(sanitize_text(area_name), fg_color=accent, bold=True)
        + colored(".", fg_color=SUCCESS_COLOR)
    )


def _announce_upload(session: Session, area: FileArea, entry: FileEntry, *, accent: int) -> None:
    """Every word about a stored upload, Zmodem's and a browser link's alike:
    where it went, whether it waits for approval, and the next screen's
    listing read again so it shows the file (`_mark_arrival`)."""
    announce_styled(session, _upload_outcome(entry, area.name, accent=accent))
    if entry.status == "pending":
        announce(session, "It waits for approval before other callers can see it.", tone="muted")
    _mark_arrival(session, area, entry)


def _tell_of_upload(session: Session, area: FileArea, *, accent: int) -> Callable[[FileEntry], None]:
    """What an upload link reports back to the terminal that asked for it
    (issue #842). The file arrives over HTTP, somewhere this session never
    sees, so without this the caller got no word that it worked.

    Queued as an outcome for the next screen drawn here, the way every
    other result is. Holds the session weakly: a link outlives a caller
    who hung up, and must not keep their session alive for ten minutes."""
    owner = weakref.ref(session)

    def tell(entry: FileEntry) -> None:
        live = owner()
        if live is None:
            return
        # A guest may describe what it uploaded in this call (issue #1075).
        note_created_this_call(live, "file", entry.id)
        _announce_upload(live, area, entry, accent=accent)

    return tell


async def _offer_transfer_link(
    session: Session,
    lane: DatabaseLane,
    user: User,
    area: FileArea,
    transfers: TransferGrants,
    *,
    direction: str,
    entry: FileEntry | None = None,
) -> None:
    """Mint one single-use transfer link and put it on screen (issue
    #475).

    Printed rather than acted on, because the caller is on a terminal
    and the transfer happens somewhere else -- their browser. What is
    said about it matters as much as the URL: a link that works once,
    for a few minutes, is a promise this node has to keep and the
    caller has to understand, so both facts are stated every time
    rather than documented somewhere they will not look.
    """
    accent = await lane.run(effective_accent_color_256)
    await offer_grant(
        session, transfers,
        mint=lambda: transfers.issue(
            direction=direction, user=user, area=area,
            file_id=entry.file_id if entry is not None else None,
            on_stored=(
                _tell_of_upload(session, area, accent=accent) if direction == UPLOAD else None
            ),
        ),
        direction=direction, what=what_of(direction, area, entry),
        filename=entry.filename if entry is not None else None,
        # Issue #842: the upload lands out of this terminal's sight, and
        # Ctrl-L is how the file list here looks again.
        then="Once it has uploaded, press Ctrl-L here to see it." if direction == UPLOAD else None,
    )


async def offer_grant(
    session: Session,
    transfers: TransferGrants,
    *,
    mint: Callable[[], TransferGrant],
    direction: str,
    what: str,
    filename: str | None = None,
    then: str | None = None,
) -> bool:
    """Mint one grant with `mint` and put its link on screen, or say why
    there is none. Shared by the file area and by the SysOp's own uploads
    (issue #728), which differ only in what the grant is for.
    `what` finishes "Open this in a browser to ...", and `then` follows a
    printed link, saying what to do once the transfer is done. Returns
    whether a link was handed out; every refusal has already been
    announced."""
    # Decided before anything is minted (Codex review): a grant issued
    # on a node that cannot express a URL, to a session with no page to
    # hand a relative one to, is a token nobody can redeem -- and 128 of
    # them fill the table for ten minutes, crowding out transfers that
    # would have worked.
    offers_to_page = getattr(session, "offer_transfer", None) is not None
    if transfers.base_url is None and not offers_to_page:
        announce_styled(
            session,
            colored(
                "\r\nThis node has no public web address configured, so it cannot hand out "
                "transfer links. Ask the SysOp to set the web transport's public URL.",
                fg_color=ERROR_COLOR,
            )
        )
        return False

    try:
        # Called straight, not through the lane (Codex review):
        # `TransferGrants` is event-loop state that touches no database,
        # and running `issue()` on a worker thread while an HTTP request
        # redeems on the loop is two threads mutating the same dict --
        # including while `_sweep` iterates it.
        grant = mint()
    except TransferError as exc:
        announce_styled(session, colored(f"\r\n{exc}", fg_color=ERROR_COLOR))
        return False

    url = transfers.url_for(grant)
    offer_transfer = getattr(session, "offer_transfer", None)
    if url is None and offer_transfer is not None:
        # A caller inside this node's own browser terminal is already at
        # the right origin, so a relative path is all their page needs
        # (Codex review) -- and it works on exactly the default
        # loopback-bound node that cannot name itself absolutely.
        if await offer_transfer(
            direction=direction, url=f"/transfer/{grant.token}", filename=filename,
        ):
            announce_styled(
                session,
                colored(
                    "\r\nYour browser is starting the download."
                    if direction == DOWNLOAD
                    else "\r\nPick a file in your browser to upload it.",
                    fg_color=MUTED_COLOR,
                )
            )
            return True
    if url is None:
        # A node whose SysOp never told it how it is reached cannot
        # print a URL that works. Saying which setting is missing beats
        # printing a loopback address that fails in a browser.
        announce_styled(
            session,
            colored(
                "\r\nThis node has no public web address configured, so it cannot hand out "
                "transfer links. Ask the SysOp to set the web transport's public URL.",
                fg_color=ERROR_COLOR,
            )
        )
        return False

    # A caller who is already in a browser should not have to select a
    # URL off a terminal and open it by hand (issue #475): the page is
    # told about the transfer and opens a file picker or starts the
    # download itself. The URL is still printed when that fails or when
    # the transport has no such notion -- which is every terminal.
    handled = False
    if offer_transfer is not None:
        handled = await offer_transfer(
            direction=direction, url=url, filename=filename,
        )
    if handled:
        # The frame reaching the socket is not the page acting on it
        # (Codex review): an older cached page, or any other client
        # speaking this protocol, may ignore a message type it does not
        # know. So the URL is printed underneath either way -- quieter,
        # and phrased for someone whose browser did nothing.
        announce_styled(
            session,
            colored(
                "\r\nYour browser is starting the download."
                if direction == DOWNLOAD
                else "\r\nPick a file in your browser to upload it.",
                fg_color=MUTED_COLOR,
            )
        )
        announce_styled(session, colored("If nothing happened, open this instead:", fg_color=MUTED_COLOR))
        announce_styled(session, f"  {colored(url, fg_color=VALUE_COLOR)}")
        return True

    announce_styled(session, colored(f"\r\nOpen this in a browser to {what}:", fg_color=MUTED_COLOR))
    announce_styled(session, f"  {colored(url, fg_color=VALUE_COLOR)}")
    announce_styled(
        session,
        colored(
            f"It works once, and stops working in {DEFAULT_GRANT_TTL_SECONDS // 60} minutes.",
            fg_color=MUTED_COLOR,
        )
    )
    if then is not None:
        announce_styled(session, colored(then, fg_color=MUTED_COLOR))
    return True


async def _transfer_link_screen(
    session: Session,
    lane: DatabaseLane,
    user: User,
    area: FileArea,
    page: FileEntryPage,
    *,
    highlighted: int | None,
    can_write: bool,
    transfers: TransferGrants,
) -> None:
    """`[W]eb transfer`: hand this caller a browser link for an upload
    or for one file, without them having to own a Zmodem-capable
    terminal.

    A screen with an action bar rather than a question (design doc
    §3.5): it says what it can do, does whichever the caller picks, and
    `[B]ack` leaves having written nothing.
    """
    target = None
    if highlighted is not None and 0 <= highlighted < len(page.entries):
        target = page.entries[highlighted]
    elif len(page.entries) == 1:
        target = page.entries[0]

    unicode_style = await lane.run(unicode_style_enabled, user)
    header_color = await lane.run(effective_header_color_256)
    heading = screen_title(
        "Browser transfer",
        breadcrumb=(session.node_display_name, "Files", sanitize_text(area.name)),
        subtitle="for a terminal without Zmodem",
        width=session.terminal_width,
        clear=await lane.run(redraw_in_place_enabled, user),
        unicode_style=unicode_style,
        collapsed=await lane.run(breadcrumb_collapsed_enabled, user),
        header_color=header_color,
        node_name_gradient=session.node_name_gradient,
    )
    while True:
        await session.write_line(f"\r\n{heading}")
        await session.write_line(
            colored(
                "Each link works once and expires in "
                f"{DEFAULT_GRANT_TTL_SECONDS // 60} minutes.",
                fg_color=MUTED_COLOR,
            )
        )
        options = []
        if can_write:
            options.append(MenuEntry(label=menu_key("U", "pload link"), brief="Send a file from your browser"))
        if target is not None:
            options.append(
                MenuEntry(
                    label=menu_key("D", "ownload link"),
                    brief=f"Fetch {sanitize_text(target.filename)}",
                )
            )
        options.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to the file list"))
        await session.write_line(
            f"\r\n{_menu_row(options, width=session.terminal_width, height=session.terminal_height, description_level='off')}"
        )
        await write_notices(session)
        await session.write("Choice: ")
        choice = (await session.read_key()).lower()
        await session.write_line("")

        if choice == "b":
            return
        if choice == HELP_KEY:
            await show_menu_help(
                session, "Browser transfer help", options,
                about=(
                    "A link to open in a web browser, for a terminal that cannot send or receive "
                    "files itself. Each link works once."
                ),
                header_color=header_color, unicode_style=unicode_style,
            )
            continue
        if choice == "u" and can_write:
            await _offer_transfer_link(session, lane, user, area, transfers, direction=UPLOAD)
            return
        if choice == "d" and target is not None:
            await _offer_transfer_link(
                session, lane, user, area, transfers, direction=DOWNLOAD, entry=target
            )
            return
        await session.write(reject_unhandled_key(choice))


@records_activity("Uploading")
async def _handle_upload(
    session: Session, lane: DatabaseLane, area: FileArea, user: User, *,
    link_context: LinkContext | None = None,
    transfers: TransferGrants | None = None,
) -> bool:
    """
    `receive_file` (GitHub issue #34, reopened a second time) now
    streams straight to a temp file under `netbbs.files.storage`'s own
    staging directory rather than returning the complete upload as one
    in-memory `bytes` object -- `temp_path` here is that staging file;
    `upload_file_from_temp` moves it into permanent content-addressed
    storage (or discards it, if this exact content is already stored)
    without ever holding the full content in memory in this module
    either.

    Between those two steps the upload is offered to
    `netbbs.files.diz.read_archive_description` (issue #463): if it is
    an archive carrying a `FILE_ID.DIZ`, that text becomes the file's
    description, the BBS convention where the archive's own author
    writes the catalogue entry. Anything else — not an archive, no DIZ,
    no unpacker installed for that format, a corrupt member — is
    reported as "no description yet" and pointed at `[E]`, never as a
    failed upload.

    `link_context` (issue #464) is what lets a finished upload actually
    reach this area's Link peers: `queue_file_descriptor_if_linked`
    builds and signs the catalogue entry `netbbs.link.sync` then pushes.
    This is the file-area counterpart of `netbbs.net.board_flow`'s own
    `queue_board_post_if_linked` call right after `create_post`, and
    exists for the same reason — nothing else in the system ever queues
    one. A pending upload in a moderated area is deliberately not
    queued here at all; `netbbs.net.admin_flow`'s approval screen queues
    it once it is approved, the same split `board_post` already has, so
    a moderation queue never leaks onto the network (design doc
    §9.2/§11.2).
    """
    # Returns whether the session itself carried a transfer (Codex
    # review): True for a Zmodem upload, False for a browser link or a
    # failed transfer. Either way the caller goes back to the listing
    # (issue #964), where a stored file now shows, cursor on it.
    #
    # Call sites test `is None`, not truthiness: only an explicit answer
    # keeps the screen open. A stand-in that returns `None` -- a test
    # double written before this contract, or a future caller that
    # forgets -- closes the screen, rather than looping on one whose input
    # source has nothing left to give.
    if not supports_zmodem(session):
        # This transport could never carry the transfer (issue #475),
        # so it is not started: a browser link is the whole of what
        # this caller can do, and offering it beats a Zmodem handshake
        # that waits for a client which is not there.
        if transfers is not None:
            await _offer_transfer_link(session, lane, user, area, transfers, direction=UPLOAD)
        else:
            announce_styled(
                session,
                colored(
                    "\r\nThis transport cannot carry a Zmodem transfer, and this node has no "
                    "browser transfer configured. Ask the SysOp to enable the web listener.",
                    fg_color=ERROR_COLOR,
                )
            )
        return False

    heading = screen_title(
        "Upload",
        breadcrumb=(session.node_display_name, "Files", sanitize_text(area.name)),
        subtitle="Zmodem transfer",
        width=session.terminal_width,
        clear=await lane.run(redraw_in_place_enabled, user),
        unicode_style=await lane.run(unicode_style_enabled, user),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, user),
        header_color=await lane.run(effective_header_color_256),
    node_name_gradient=session.node_name_gradient)
    await session.write_line(f"\r\n{heading}")
    await session.write_line(
        "Your terminal should offer its Zmodem upload now; if it doesn't, start its Zmodem send (sz). "
        "Ctrl-X five times cancels."
    )
    temp_path = await lane.run(new_incoming_temp_path)
    max_upload_bytes = await lane.run(get_max_upload_bytes)
    try:
        received = await zmodem.receive_file(session, max_bytes=max_upload_bytes, dest_path=temp_path)
        # Read while the content is still at `temp_path` (issue #463):
        # `upload_file_from_temp` moves it into content-addressed
        # storage under a name with no extension, and the extension is
        # what picks an unpacker. Never fails an upload -- see
        # `read_archive_description`.
        #
        # Its own cleanup, though: between a finished transfer and the
        # move into storage, this is the only code that owns the
        # staging file, and a caller who drops the line mid-extraction
        # would otherwise leave it for the next startup sweep to find
        # (Codex review). `receive_file`'s cleanup covers only failures
        # of its own.
        try:
            description = await read_archive_description(temp_path, received.filename)
        except BaseException:
            # Cleanup never masks what actually went wrong (Codex
            # review) -- a failed unlink here (Windows holding the file
            # open behind a cancelled worker, say) must not replace a
            # session cancellation with an OSError about a temp file.
            try:
                temp_path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                _logger.warning("could not remove staging file %s: %s", temp_path, cleanup_error)
            raise
        def _store_and_announce(db: Database) -> FileEntry:
            # One database job, not two (Codex review): a cancellation
            # between them would leave a committed, approved upload that
            # nothing ever announces -- `DatabaseLane` lets a running
            # worker finish, so the row would exist while the queueing
            # call never ran, and no later pass revisits it.
            stored = upload_file_from_temp(
                db, area, user, received.filename,
                temp_path=temp_path, sha256=received.sha256, size_bytes=received.size_bytes,
                description=description,
            )
            if link_context is not None:
                # Best-effort, and deliberately after the file is
                # committed rather than inside its transaction (Codex
                # review of #482): `upload_file_from_temp` has already
                # moved the bytes and written the row, so a signing or
                # database failure here must not turn a stored file into
                # a reported upload failure. The Linked area simply does
                # not announce this one; nothing re-queues it.
                try:
                    queue_file_descriptor_if_linked(
                        db, stored, area, node_identity=link_context.node_identity
                    )
                except Exception:
                    # Rolled back before swallowing, as on the HTTP path
                    # (Codex review of #508): this is the lane's shared
                    # connection, and a failure inside
                    # `queue_file_descriptor_if_linked`'s own commit would
                    # otherwise leave it in a failed transaction for every
                    # later job.
                    try:
                        db.connection.rollback()
                    except Exception:
                        # Rolling back can fail for the same reason the write did
                        # (Codex review of #508): a closed or broken connection
                        # raises here too, and letting that escape would turn a
                        # best-effort announcement into exactly the failed upload
                        # this catch exists to prevent -- the caller told to
                        # re-upload a file that is already stored, and on the
                        # Zmodem path the session dropped.
                        _logger.warning(
                            "files: could not roll back after a failed descriptor queue",
                            exc_info=True,
                        )
                    _logger.warning(
                        "files: stored %r in area %r but could not queue its Link descriptor; "
                        "the file is available locally and will not be announced to peers",
                        stored.filename, area.name, exc_info=True,
                    )
            return stored

        entry = await lane.run(_store_and_announce)
        # A guest may describe what it uploaded in this call (issue #1075).
        note_created_this_call(session, "file", entry.id)
    except (zmodem.ZmodemError, NotImplementedError) as exc:
        # NotImplementedError: some transports (netbbs.net.web) can't
        # carry raw bytes at all -- see WebSession's docstring. Handled
        # the same as any other failed transfer rather than crashing
        # the session. temp_path is already cleaned up by receive_file
        # itself on any failure of its own; a NotImplementedError means
        # receive_file never even opened it.
        announce_styled(session, colored(f"\r\nUpload failed: {exc}", fg_color=ERROR_COLOR))
        _point_at_browser_transfer(session, transfers, direction=UPLOAD)
        # Back to the list (issue #842), where [W]eb transfer is.
        return False
    _announce_upload(session, area, entry, accent=await lane.run(effective_accent_color_256))
    if entry.description:
        announce_styled(session, colored("Description read from FILE_ID.DIZ:", fg_color=MUTED_COLOR))
        for line in entry.description.splitlines():
            announce_styled(session, f"  {colored(sanitize_text(line), fg_color=MUTED_COLOR)}")
    else:
        # Deliberately not "there is no FILE_ID.DIZ in it" (Codex
        # review): the same `None` covers an archive whose format has
        # no unpacker installed here, one this node couldn't read, and
        # a plain file that was never an archive. Say what is true --
        # nothing was read — and offer the way to fix it.
        announce_styled(
            session,
            colored(
                "No description was read from it — press [E] on the listing to write one.",
                fg_color=MUTED_COLOR,
            )
        )
    return True


def _point_at_browser_transfer(session: Session, transfers: TransferGrants | None, *, direction: str) -> None:
    """After a failed Zmodem transfer, name the way that works without it
    (issue #842): most terminals have no Zmodem at all, and the failure
    message alone left the caller guessing."""
    if transfers is None:
        return
    announce_styled(
        session,
        colored(
            f"Press [W] for a browser {'upload' if direction == UPLOAD else 'download'} link instead.",
            fg_color=MUTED_COLOR,
        ),
    )


@records_activity("Downloading")
async def send_file_to_caller(
    session: Session, lane: DatabaseLane, area: FileArea, entry: FileEntry, user: User, *,
    transfers: TransferGrants | None = None,
    web_hint: bool = False,
) -> bool:
    """Returns whether the caller should stay where they were: `True` only
    when a Zmodem send failed (issue #842), so the file list stays up with
    its [W]eb transfer key. `web_hint` says that key is on the screen the
    caller goes back to, so the failure can point at it."""
    # Takes the entry the caller actually chose -- a number key, Enter
    # on the cursor, or `_choose_entry`'s picker -- rather than a
    # filename to look up again. While `/download <filename>` existed
    # this re-read the area by name (a lookup since removed) because the
    # name was all the screen had; a filename is not unique within an
    # area, so that lookup could hand back a different row than the one
    # under the cursor. Every caller now holds the row itself, and one
    # already passed this area's read/age/name gate and
    # `list_files_page`'s own moderation filter to be on screen at all.

    # Returns whether the session itself carried a transfer (Codex
    # review). A Zmodem upload owns the byte stream and ends with the
    # screen gone, so the caller is dropped back to the menu afterwards
    # as it always was; a browser upload happens somewhere else entirely
    # and the caller is still sitting in the file area, which is where
    # the file they are about to send should appear.

    if not supports_zmodem(session):
        # Issue #475: same reasoning as the upload side -- this
        # transport cannot carry the transfer, so the browser link is
        # the whole of what this caller can do.
        if transfers is not None:
            await _offer_transfer_link(
                session, lane, user, area, transfers, direction=DOWNLOAD, entry=entry
            )
        else:
            announce_styled(
                session,
                colored(
                    "\r\nThis transport cannot carry a Zmodem transfer, and this node has no "
                    "browser transfer configured. Ask the SysOp to enable the web listener.",
                    fg_color=ERROR_COLOR,
                )
            )
        return False

    entry_filename = sanitize_text(entry.filename)
    heading = screen_title(
        "Download",
        breadcrumb=(session.node_display_name, "Files", sanitize_text(area.name)),
        subtitle=f"{entry_filename} / {_format_size(entry.size_bytes)}",
        width=session.terminal_width,
        clear=await lane.run(redraw_in_place_enabled, user),
        unicode_style=await lane.run(unicode_style_enabled, user),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, user),
        header_color=await lane.run(effective_header_color_256),
    node_name_gradient=session.node_name_gradient)
    await session.write_line(f"\r\n{heading}")
    await session.write_line(
        f"Starting Zmodem send of {entry_filename!r} — your terminal should start receiving by itself. "
        "Ctrl-X five times cancels."
    )
    try:
        # download_file reads content-addressed storage directly from
        # disk by hash/path -- it never took a `db` parameter, so
        # nothing here changes: real file I/O, not database I/O, is
        # outside the two-lane database execution model's scope
        # regardless of which lane calls it.
        data = download_file(entry)
        await zmodem.send_file(session, entry.filename, data)
    except (zmodem.ZmodemError, NotImplementedError) as exc:
        announce_styled(session, colored(f"\r\nDownload failed: {exc}", fg_color=ERROR_COLOR))
        if web_hint:
            _point_at_browser_transfer(session, transfers, direction=DOWNLOAD)
        return True
    except OSError:
        # The row the caller chose is the row that gets sent now, rather
        # than one re-read by name -- so a file deleted and then
        # collected (`netbbs.files.gc`) while this page was on screen
        # reaches `download_file` with no bytes behind it. Reported the
        # way the by-name lookup used to report a missing file, instead
        # of leaving the screen through `FileNotFoundError`.
        announce_styled(
            session,
            colored(
                f"\r\n{entry_filename!r} is no longer on this node — it was removed while this "
                "listing was on screen.",
                fg_color=ERROR_COLOR,
            )
        )
        return False
    announce_styled(session, colored(f"\r\nSent {entry_filename!r}.", fg_color=SUCCESS_COLOR))
    return False
