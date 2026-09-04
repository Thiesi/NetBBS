"""
File area browsing, upload, and download.

Kept in its own module rather than growing login_flow.py indefinitely —
matches the project's modular-package approach (design doc §3), same
reasoning as chat_flow.py.

Upload/download (design doc) go over real ZMODEM
(`netbbs.net.zmodem`), not a NetBBS-specific scheme — the whole point
being that a real Zmodem-capable terminal (SyncTERM, lrzsz) can drive
this without any custom client software. `/upload`/`/download` take
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

- `has_visible_areas` stays on `db: Database`, synchronous — it's a
  menu-*gating* check called from `netbbs.net.login_flow`'s still-
  unmigrated menu-drawing code (`_resource_type_menu`'s `show_areas`),
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

from netbbs.activity import record_file_area_seen
from netbbs.attestation import format_name_for_resource, meets_age, meets_name_requirement
from netbbs.auth.users import User, get_user_by_id
from netbbs.communities import (
    get_community,
    get_effective_min_age,
    get_effective_min_read_level,
    get_effective_min_write_level,
    get_effective_name_requirement,
)
from netbbs.config import get_max_upload_bytes
from netbbs.files import (
    FileArea,
    FileEntryPage,
    download_file,
    get_file_by_name,
    list_file_areas,
    list_files_page,
    upload_file_from_temp,
)
from netbbs.files.categories import (
    FileAreaCategory,
    get_category_by_id,
    list_subcategories,
    list_top_level_categories,
)
from netbbs.files.storage import new_incoming_temp_path
from netbbs.link.boards import LinkContext
from netbbs.link.node_profiles import (
    identity_for_peer, latest_identity_observation, present_link_author_label,
)
from netbbs.link.files import RemoteFile, is_area_linked, list_remote_files
from netbbs.link.protocol import LinkProtocolError
from netbbs.net import zmodem
from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.color_depth_preference import effective_truecolor
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.file_area_banner import load_file_area_banner
from netbbs.net.node_theme import effective_accent_color_256, effective_header_color_256
from netbbs.net.picker import pick_item
from netbbs.net.session import Session
from netbbs.net.sort_ui import SORT_MODE_LABELS, prompt_sort_change
from netbbs.permissions import meets_level
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.rendering import (
    ERROR_COLOR,
    HEADER_COLOR,
    MENU_KEY_COLOR,
    METADATA_COLOR,
    MUTED_COLOR,
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
    screen_title,
    visible_width,
)
from netbbs.sort_preferences import get_effective_sort_mode, set_sort_preference
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display, resolve_display_preferences


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
) -> None:
    """Enter `area` directly, bypassing the category picker entirely --
    public (unlike `_show_area`) so issue #56's `[N]ew scan` screen
    (`netbbs.net.login_flow`) can jump straight into a specific area
    with a starting cursor, the same reasoning `netbbs.net.chat_flow.
    browse_channels`'s own `initial_channel` parameter already has for
    channels.

    `link_context` (design doc, issue #92), if given, is passed straight
    through to `_show_area`, which offers a `/remote` command to browse
    and fetch this area's carried-but-not-yet-fetched remote catalogue
    when it's Linked -- `None` (Link disabled on this node, or a direct
    test/CLI call site) simply hides that command, same degrade-
    gracefully shape every other optional `link_context` parameter
    already has."""
    await _show_area(session, lane, area, user, initial_cursor=initial_cursor, link_context=link_context)


async def browse_file_areas(
    session: Session,
    lane: DatabaseLane,
    user: User,
    *,
    community_id: int | None = None,
    community_scoped: bool = False,
    title_prefix: str | None = None,
    link_context: LinkContext | None = None,
) -> None:
    """Entry point: browse from the top level (no category selected yet)."""
    await _browse_areas_in_category(
        session, lane, user, category_id=None,
        community_id=community_id, community_scoped=community_scoped, title_prefix=title_prefix,
        link_context=link_context,
    )


def has_visible_areas(
    db: Database, user: User, *, community_id: int | None = None, community_scoped: bool = False
) -> bool:
    """Whether `user` can see at least one file area under the given
    Community filter -- backs `netbbs.net.login_flow`'s shared
    resource-type sub-menu, same convention as `_has_visible_boards`/
    `netbbs.net.chat_flow.has_visible_channels` (design doc §16).
    Deliberately still `db`-based, not `lane`-based -- see this
    module's own docstring for why."""
    areas = [
        a for a in list_file_areas(db)
        if meets_level(user, get_effective_min_read_level(db, a)) and meets_age(db, user, get_effective_min_age(db, a))
    ]
    if community_scoped:
        areas = [a for a in areas if a.community_id == community_id]
    return bool(areas)


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
            if meets_level(user, get_effective_min_read_level(db, a))
            and meets_age(db, user, get_effective_min_age(db, a))
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
    areas_here, categories_here, category_name, community_name = await lane.run(_load, current_mode)
    description_level = await lane.run(menu_description_level, user)
    redraw_in_place = await lane.run(redraw_in_place_enabled, user)
    unicode_style = await lane.run(unicode_style_enabled, user)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, user)
    # GitHub issue #176: resolved once, reused for both pick_item calls
    # below (flat and mixed-with-categories) -- shows at every level of
    # file-area browsing this recursive function reaches (top level, a
    # category, a Community/Uncategorized scope), matching
    # `board_flow._browse_boards_in_category`'s own identical wiring.
    area_masthead = await lane.run(load_file_area_banner)
    mode_box = {"mode": current_mode}

    async def _persist_sort_choice(mode: str, scope_kwargs: dict) -> None:
        await lane.run(set_sort_preference, user, "file_area", mode, **scope_kwargs)

    async def _run_sort_prompt() -> str | None:
        return await prompt_sort_change(
            session, persist=_persist_sort_choice,
            community_id=effective_community_id, community_name=community_name,
            category_id=category_id, category_name=category_name,
        )

    def _sort_label() -> str:
        return SORT_MODE_LABELS[mode_box["mode"]]

    title = "File areas" if title_prefix is not None else "Available file areas"
    picker_breadcrumb = (title_prefix,) if title_prefix is not None else ()

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
            description_of=lambda a: a.description,
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
        )
        if area is not None:
            await _show_area(session, lane, area, user, link_context=link_context)
        return

    mixed: list[FileAreaCategory | FileArea] = [*categories_here, *areas_here]

    def render_name(item: FileAreaCategory | FileArea) -> str:
        return f"[{item.name}]" if isinstance(item, FileAreaCategory) else item.name

    def render_description(item: FileAreaCategory | FileArea) -> str | None:
        if isinstance(item, FileAreaCategory):
            return item.description or "(category)"
        return item.description

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
    )
    if selected is None:
        return

    if isinstance(selected, FileAreaCategory):
        await _browse_areas_in_category(
            session, lane, user, category_id=selected.id,
            community_id=community_id, community_scoped=community_scoped, title_prefix=title_prefix,
            link_context=link_context,
        )
    else:
        await _show_area(session, lane, selected, user, link_context=link_context)


def _format_size(size_bytes: int) -> str:
    """
    Human-readable file size, binary (KiB/MiB/GiB) units — matches what
    most file managers and BBS file listings show, rather than raw byte
    counts once a file is more than a few hundred bytes.
    """
    if size_bytes < 1024:
        return f"{size_bytes} B"
    size = size_bytes / 1024
    for unit in ("KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024


def _file_column_widths(terminal_width: int) -> tuple[int, int, int, int, int]:
    """Returns (idx_w, name_w, size_w, date_w, uploader_w) for columnar file listing."""
    idx_w = 4
    size_w = 9
    date_w = 16
    if terminal_width < 80:
        uploader_w = 16
        fixed = idx_w + 1 + size_w + 1 + date_w + 1 + uploader_w
        name_w = max(12, terminal_width - fixed - 1)
    else:
        extra = terminal_width - 80
        name_w = 18 + min(10, extra // 2)
        uploader_w = 28 + min(10, extra // 4)
    return idx_w, name_w, size_w, date_w, uploader_w


async def _render_area_page(
    session: Session,
    lane: DatabaseLane,
    area_name: str,
    page: FileEntryPage,
    *,
    can_write: bool,
    name_requirement: str | None,
    show_remote_hint: bool = False,
    description_level: str = "off",
    redraw_in_place: bool = False,
    unicode_style: bool = False,
    collapsed: bool = False,
    truecolor: bool = False,
    highlighted: int | None = None,
) -> None:
    """Renders one page of files plus its navigation options and command
    hints — the unit that should be redrawn on an actual page change
    (initial entry, Older/Newer/Recent), not on every loop iteration
    regardless of whether anything changed."""
    await _render_file_page(
        session, lane, area_name, page, name_requirement=name_requirement, redraw_in_place=redraw_in_place,
        unicode_style=unicode_style, collapsed=collapsed, truecolor=truecolor, highlighted=highlighted,
    )
    options = []
    if page.has_older:
        options.append(MenuEntry(label=menu_key("O", "lder"), brief="Show older files"))
    if page.has_newer:
        options.append(MenuEntry(label=menu_key("N", "ewer"), brief="Show newer files"))
        options.append(MenuEntry(label=menu_key("R", "ecent"), brief="Jump to the newest page"))
    options.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to the previous menu"))
    await session.write_line(
        f"\r\n{_menu_row(options, width=session.terminal_width, height=session.terminal_height, description_level=description_level)}"
    )

    n_files = len(page.entries)
    if n_files > 0:
        num_label = f"1-{n_files}" if n_files > 1 else "1"
        hints = [MenuEntry(label=menu_key(num_label, " or /download <name|#> — receive via Zmodem"))]
    else:
        hints = [MenuEntry(label=menu_key("/download <filename>", " — receive via Zmodem"))]
    if can_write:
        hints.append(MenuEntry(label=menu_key("/upload", " — send via Zmodem")))
    if show_remote_hint:
        hints.append(MenuEntry(label=menu_key("/remote", " — browse/fetch this file area's remote catalogue")))
    await session.write_line(
        _menu_row(hints, width=session.terminal_width, height=session.terminal_height, description_level=description_level)
    )


async def _read_file_choice(
    session: Session,
    page: FileEntryPage,
    highlighted: int | None,
) -> tuple[str, str | None, int | None]:
    """Read a command, file number shortcut, or arrow navigation.

    Returns:
      ('nav', action, None) - navigation command ('b', 'o', 'n', 'r')
      ('download', filename, None) - direct file download
      ('highlight', None, new_index) - arrow key highlight change
      ('command', full_cmd, None) - multi-character command line
      ('none', None, highlighted) - no-op / rejected key
    """
    await session.write("Choice or command: ")

    read_editor_key = getattr(session, "read_editor_key", None)
    if read_editor_key is not None:
        try:
            key = await read_editor_key()
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
                    return ("download", page.entries[highlighted].filename, highlighted)
                else:
                    await session.write("\a")
                    return ("none", None, highlighted)
            elif key.kind == EditorKeyKind.ESCAPE:
                if highlighted is not None:
                    return ("highlight", None, None)
                await session.write("\a")
                return ("none", None, highlighted)
            elif key.kind == EditorKeyKind.CHAR and key.char:
                char = key.char
                if char.isdigit():
                    idx = int(char)
                    if 1 <= idx <= len(page.entries):
                        await session.write_line(char)
                        return ("download", page.entries[idx - 1].filename, None)
                if char.lower() in ("b", "o", "n", "r"):
                    await session.write_line(char)
                    return ("nav", char.lower(), None)
                await session.write(char)
                rest = await session.read_line()
                return ("command", (char + rest).strip(), highlighted)
            else:
                await session.write("\a")
                return ("none", None, highlighted)
        except (NotImplementedError, AttributeError):
            pass

    line = (await session.read_line()).strip()
    return ("command", line, highlighted)


async def _show_area(
    session: Session,
    lane: DatabaseLane,
    area: FileArea,
    user: User,
    *,
    initial_cursor: tuple[str, str] | None = None,
    link_context: LinkContext | None = None,
) -> None:
    """
    Show `area`, one bounded page of files at a time (design doc,
    issue #10's file-area follow-up to the board-post pagination) —
    mirrors `netbbs.net.board_flow._show_board`'s
    pagination *semantics* exactly: same newest-first default, same
    `[O]lder`/`[N]ewer`/`[R]ecent`/`[B]ack` options, same reasoning for
    both (see that function's docstring, not repeated here) — including
    only redrawing the listing on an actual page change, and `b` (not a
    bare Enter, which used to also work here but no longer does) as the
    one consistent way back. `initial_cursor` (issue #56's `[N]ew scan`
    "jump to first unread") works identically to `_show_board`'s own:
    overrides only the very first render, falling back to the newest
    page if nothing is newer than the cursor.

    One deliberate mechanical difference from `_show_board`, not an
    inconsistency: this reads the choice via `read_line()`, not
    `read_key()`. `_show_board`'s options are all single immediate
    keystrokes; this screen also needs to accept free-text multi-
    character commands (`/download <filename>`, `/upload`) in the same
    prompt, which single-keystroke dispatch can't support — `read_key()`
    returns after exactly one character, before "/download " could ever
    be typed.

    `/download <filename>` deliberately looks a file up by name across
    the *whole area* (`get_file_by_name`), not just the currently
    displayed page — pagination bounds what's fetched for browsing, not
    what can be referenced by a name the user already knows (from an
    earlier page, or from outside this session entirely).

    `link_context` (design doc, issue #92), if given *and this specific
    area is actually Linked* (`is_area_linked` — Link being enabled
    node-wide is not enough, the same distinction `netbbs.net.admin_flow`'s
    board admin screen already draws between "Link is on" and "this
    board is Linked"), offers `/remote` — browse this area's carried-
    but-not-yet-fetched remote catalogue and fetch one on demand
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
    area_name = sanitize_text(area.name)

    def _load(db: Database) -> tuple[FileEntryPage, str | None, bool, bool, str, bool, bool, bool, bool]:
        # Bundled into one lane call: the page, the effective
        # name_requirement, the can_write gate, whether this area is
        # actually Linked, and the menu-description preference all come
        # from the same worker-thread pass rather than five round trips.
        page = list_files_page(db, area, user, after=initial_cursor) if initial_cursor else list_files_page(db, area, user)
        if initial_cursor and not page.entries:
            # Nothing newer than the cursor -- caught up, not a
            # genuinely empty area; fall back to the newest page.
            page = list_files_page(db, area, user)
        effective_name_requirement = get_effective_name_requirement(db, area)
        can_write = (
            meets_level(user, get_effective_min_write_level(db, area))
            and meets_age(db, user, get_effective_min_age(db, area))
            and meets_name_requirement(db, user, effective_name_requirement)
        )
        return (
            page, effective_name_requirement, can_write, is_area_linked(db, area),
            menu_description_level(db, user), redraw_in_place_enabled(db, user),
            unicode_style_enabled(db, user), breadcrumb_collapsed_enabled(db, user),
            effective_truecolor(session, db, user),
        )

    page, effective_name_requirement, can_write, area_linked, description_level, redraw_in_place, unicode_style, collapsed, truecolor = (
        await lane.run(_load)
    )

    show_remote_hint = link_context is not None and area_linked

    async def _render_and_advance_cursor(current_page: FileEntryPage, highlighted: int | None = None) -> None:
        """The one place every render in this loop funnels through
        (issue #56) -- advances `user`'s file-area read cursor to
        whatever is now newest on screen."""
        await _render_area_page(
            session, lane, area_name, current_page, can_write=can_write, name_requirement=effective_name_requirement,
            show_remote_hint=show_remote_hint, description_level=description_level, redraw_in_place=redraw_in_place,
            unicode_style=unicode_style, collapsed=collapsed, truecolor=truecolor, highlighted=highlighted,
        )
        if current_page.entries:
            await lane.run(record_file_area_seen, user, area, current_page.entries[-1])

    if not page.entries:
        header_color = await lane.run(effective_header_color_256)
        heading = screen_title(
            area_name, breadcrumb=(session.node_display_name, "Files"), width=session.terminal_width, clear=redraw_in_place,
            unicode_style=unicode_style, collapsed=collapsed, header_color=header_color,
        node_name_gradient=session.node_name_gradient)
        await session.write_line(f"\r\n{heading}")
        state = empty_state(
            "This file area has no files yet",
            detail="Uploads and fetched Link files will appear here.",
            width=session.terminal_width,
            header_color=header_color,
        )
        await session.write_line(f"\r\n{state}")
    else:
        highlighted: int | None = None
        await _render_and_advance_cursor(page, highlighted=highlighted)
        while True:
            kind, target, new_h = await _read_file_choice(session, page, highlighted)

            if kind == "highlight":
                highlighted = new_h
                await _render_and_advance_cursor(page, highlighted=highlighted)
                continue
            elif kind == "none":
                continue
            elif kind == "download":
                if target is not None:
                    await _handle_download(session, lane, area, target, user)
                    return
            elif kind == "nav":
                if target == "b":
                    break
                elif target == "o" and page.has_older:
                    oldest = page.entries[0]
                    page = await lane.run(
                        list_files_page, area, user, before=(oldest.created_at, oldest.file_id)
                    )
                    highlighted = None
                    await _render_and_advance_cursor(page, highlighted=highlighted)
                elif target == "n" and page.has_newer:
                    newest = page.entries[-1]
                    page = await lane.run(
                        list_files_page, area, user, after=(newest.created_at, newest.file_id)
                    )
                    highlighted = None
                    await _render_and_advance_cursor(page, highlighted=highlighted)
                elif target == "r" and page.has_newer:
                    page = await lane.run(list_files_page, area, user)
                    highlighted = None
                    await _render_and_advance_cursor(page, highlighted=highlighted)
                else:
                    await session.write("\a")
                continue
            elif kind == "command":
                choice = target or ""
                if choice.lower() == "b":
                    break
                elif choice.lower() == "o" and page.has_older:
                    oldest = page.entries[0]
                    page = await lane.run(
                        list_files_page, area, user, before=(oldest.created_at, oldest.file_id)
                    )
                    highlighted = None
                    await _render_and_advance_cursor(page, highlighted=highlighted)
                elif choice.lower() == "n" and page.has_newer:
                    newest = page.entries[-1]
                    page = await lane.run(
                        list_files_page, area, user, after=(newest.created_at, newest.file_id)
                    )
                    highlighted = None
                    await _render_and_advance_cursor(page, highlighted=highlighted)
                elif choice.lower() == "r" and page.has_newer:
                    page = await lane.run(list_files_page, area, user)
                    highlighted = None
                    await _render_and_advance_cursor(page, highlighted=highlighted)
                elif choice.lower() == "/upload" and can_write:
                    await _handle_upload(session, lane, area, user)
                    return
                elif choice.lower() == "/remote" and show_remote_hint:
                    await _browse_remote_files(session, lane, area, user, link_context)
                    return
                elif choice.isdigit() and 1 <= int(choice) <= len(page.entries):
                    target_file = page.entries[int(choice) - 1].filename
                    await _handle_download(session, lane, area, target_file, user)
                    return
                elif choice.startswith("#") and choice[1:].isdigit() and 1 <= int(choice[1:]) <= len(page.entries):
                    target_file = page.entries[int(choice[1:]) - 1].filename
                    await _handle_download(session, lane, area, target_file, user)
                    return
                elif choice.lower().startswith("/download ") or choice.lower().startswith("d ") or choice.lower().startswith("dl "):
                    arg = choice.split(maxsplit=1)[1].strip()
                    if arg.isdigit() and 1 <= int(arg) <= len(page.entries):
                        exact = next((e.filename for e in page.entries if e.filename == arg), None)
                        target_file = exact if exact is not None else page.entries[int(arg) - 1].filename
                    else:
                        target_file = arg
                    await _handle_download(session, lane, area, target_file, user)
                    return
                elif choice.lower() in ("/download", "d", "dl"):
                    if highlighted is not None and 0 <= highlighted < len(page.entries):
                        target_file = page.entries[highlighted].filename
                        await _handle_download(session, lane, area, target_file, user)
                        return
                    elif len(page.entries) == 1:
                        target_file = page.entries[0].filename
                        await _handle_download(session, lane, area, target_file, user)
                        return
                    else:
                        await session.write("File number or name to download: ")
                        sub_choice = (await session.read_line()).strip()
                        if not sub_choice:
                            continue
                        if sub_choice.isdigit() and 1 <= int(sub_choice) <= len(page.entries):
                            exact = next((e.filename for e in page.entries if e.filename == sub_choice), None)
                            target_file = exact if exact is not None else page.entries[int(sub_choice) - 1].filename
                        else:
                            target_file = sub_choice
                        await _handle_download(session, lane, area, target_file, user)
                        return
                else:
                    await session.write("\a")
        return

    if not can_write and not show_remote_hint:
        return

    hints = []
    if can_write:
        hints.append(MenuEntry(label=menu_key("/upload", " — send via Zmodem")))
    if show_remote_hint:
        hints.append(MenuEntry(label=menu_key("/remote", " — browse/fetch this file area's remote catalogue")))
    await session.write_line(
        f"\r\n{_menu_row(hints, width=session.terminal_width, height=session.terminal_height, description_level=description_level)}"
    )
    await session.write("Command (or press Enter to go back): ")
    command = (await session.read_line()).strip()

    if not command:
        return
    elif command.lower() == "/upload" and can_write:
        await _handle_upload(session, lane, area, user)
    elif command.lower() == "/remote" and show_remote_hint:
        await _browse_remote_files(session, lane, area, user, link_context)
    else:
        await session.write_line("Unknown command.")


async def _browse_remote_files(
    session: Session, lane: DatabaseLane, area: FileArea, user: User, link_context: LinkContext
) -> None:
    """
    `/remote` (design doc, issue #92): list every catalogued file for
    `area` -- both fetched and not -- and offer to fetch one that isn't
    local yet. No per-file access check here beyond what already gated
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
                f"\r\n{sanitize_text(selected.filename)!r} is already available locally -- use "
                "/download to receive it.",
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
        await session.write_line(colored("Cancelled.", fg_color=MUTED_COLOR))
        return

    await _fetch_remote_file(
        session, lane, selected, link_context, redraw_in_place=redraw_in_place, unicode_style=unicode_style,
        collapsed=collapsed,
    )


def _remote_file_origin_label(link_context: LinkContext, remote_file: RemoteFile) -> str:
    peer = link_context.link_node.peers.get(remote_file.origin_fingerprint)
    return identity_for_peer(peer).label if peer is not None else remote_file.origin_fingerprint


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
    from netbbs.link.transport import LinkTransportError, dialable_base_urls_for_peer, fetch_next_file_chunk

    base_urls = dialable_base_urls_for_peer(link_context.link_node, remote_file.origin_fingerprint)
    if not base_urls:
        await session.write_line(
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
                    colored(f"  … {transfer.bytes_received}/{transfer.total_size} bytes", fg_color=MUTED_COLOR)
                )
    except (LinkProtocolError, LinkTransportError, FileTransferError) as exc:
        await session.write_line(colored(f"Fetch failed: {exc}", fg_color=ERROR_COLOR))
        return

    if transfer.status == "completed":
        await session.write_line(
            colored(
                f"{sanitize_text(remote_file.filename)!r} fetched and verified — available via /download now.",
                fg_color=SUCCESS_COLOR,
            )
        )
    else:
        await session.write_line(
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
    if not page.entries:
        return

    display_format, display_timezone = await lane.run(resolve_display_preferences)
    accent = await lane.run(effective_accent_color_256)
    divider_color = 238 if truecolor else MUTED_COLOR
    rule_char = "─" if unicode_style else "-"

    idx_w, name_w, size_w, date_w, uploader_w = _file_column_widths(session.terminal_width)

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
        is_highlighted = highlighted == (position - 1)
        marker = ">" if is_highlighted else " "
        idx_label = f"{marker}[{position:2d}]"
        if is_highlighted:
            idx_cell = colored(idx_label, fg_color=accent, bold=True)
        else:
            idx_cell = colored(idx_label, fg_color=MENU_KEY_COLOR)

        name_clean = sanitize_text(entry.filename)
        name_cut = cut_to_width(name_clean, name_w)
        if visible_width(name_cut) < name_w:
            name_padded = name_cut + " " * (name_w - visible_width(name_cut))
        else:
            name_padded = name_cut
        name_cell = colored(name_padded, fg_color=accent, bold=is_highlighted)

        size_str = _format_size(entry.size_bytes)
        size_padded = f"{size_str:>{size_w}}"
        if is_highlighted:
            size_cell = colored(size_padded, fg_color=accent, bold=True)
        else:
            size_cell = colored(size_padded, fg_color=VALUE_COLOR)

        when = format_for_display(entry.created_at, override_format=display_format, override_timezone=display_timezone)
        date_cut = cut_to_width(when, date_w)
        if visible_width(date_cut) < date_w:
            date_padded = date_cut + " " * (date_w - visible_width(date_cut))
        else:
            date_padded = date_cut
        date_cell = colored(date_padded, fg_color=METADATA_COLOR)

        uploader_display = await lane.run(_uploader_display_name, entry, name_requirement=name_requirement)
        vis_u = visible_width(uploader_display)
        if vis_u <= uploader_w:
            uploader_cell = uploader_display + " " * (uploader_w - vis_u)
        else:
            uploader_cell = colored_truncate([(uploader_display, None)], uploader_w)

        row_cells = [idx_cell, name_cell, size_cell, date_cell, uploader_cell]
        await session.write_line(" ".join(row_cells))
        if entry.description:
            await session.write_line(f"      {colored(sanitize_text(entry.description), fg_color=MUTED_COLOR)}")


async def _handle_upload(session: Session, lane: DatabaseLane, area: FileArea, user: User) -> None:
    """
    `receive_file` (GitHub issue #34, reopened a second time) now
    streams straight to a temp file under `netbbs.files.storage`'s own
    staging directory rather than returning the complete upload as one
    in-memory `bytes` object -- `temp_path` here is that staging file;
    `upload_file_from_temp` moves it into permanent content-addressed
    storage (or discards it, if this exact content is already stored)
    without ever holding the full content in memory in this module
    either.
    """
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
    await session.write_line("Start your terminal's Zmodem send (sz) now. Waiting for the transfer to begin...")
    temp_path = await lane.run(new_incoming_temp_path)
    max_upload_bytes = await lane.run(get_max_upload_bytes)
    try:
        received = await zmodem.receive_file(session, max_bytes=max_upload_bytes, dest_path=temp_path)
        entry = await lane.run(
            upload_file_from_temp, area, user, received.filename,
            temp_path=temp_path, sha256=received.sha256, size_bytes=received.size_bytes,
        )
    except (zmodem.ZmodemError, NotImplementedError) as exc:
        # NotImplementedError: some transports (netbbs.net.web) can't
        # carry raw bytes at all -- see WebSession's docstring. Handled
        # the same as any other failed transfer rather than crashing
        # the session. temp_path is already cleaned up by receive_file
        # itself on any failure of its own; a NotImplementedError means
        # receive_file never even opened it.
        await session.write_line(colored(f"\r\nUpload failed: {exc}", fg_color=ERROR_COLOR))
        return
    await session.write_line(
        colored(
            f"\r\nUploaded {sanitize_text(entry.filename)!r} ({_format_size(entry.size_bytes)}) "
            f"to [{sanitize_text(area.name)}].",
            fg_color=SUCCESS_COLOR,
        )
    )


async def _handle_download(session: Session, lane: DatabaseLane, area: FileArea, filename: str, user: User) -> None:
    # Looked up by exact name across the whole area (get_file_by_name),
    # not just the currently displayed page -- see _show_area's
    # docstring. Matched against the raw, unsanitized `filename` the
    # user actually typed -- sanitizing before comparison risks a false
    # match/miss against real stored filenames; sanitize_text is only
    # applied below, at the point this gets echoed back to the terminal.
    # requesting_user is passed so a still-pending upload (moderated
    # area, design doc sign-off) isn't downloadable by name
    # before it's been approved, unless this user is its own uploader
    # or holds approve permission on the area.
    entry = await lane.run(get_file_by_name, area, filename, requesting_user=user)
    if entry is None:
        await session.write_line(
            colored(f"\r\nNo file named {sanitize_text(filename)!r} in this file area.", fg_color=ERROR_COLOR)
        )
        return

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
    await session.write_line(f"Starting Zmodem send of {entry_filename!r} — accept the transfer in your terminal.")
    try:
        # download_file reads content-addressed storage directly from
        # disk by hash/path -- it never took a `db` parameter, so
        # nothing here changes: real file I/O, not database I/O, is
        # outside the two-lane database execution model's scope
        # regardless of which lane calls it.
        data = download_file(entry)
        await zmodem.send_file(session, entry.filename, data)
    except (zmodem.ZmodemError, NotImplementedError) as exc:
        await session.write_line(colored(f"\r\nDownload failed: {exc}", fg_color=ERROR_COLOR))
        return
    await session.write_line(colored(f"\r\nSent {entry_filename!r}.", fg_color=SUCCESS_COLOR))
