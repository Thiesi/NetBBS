"""SysOp console file-area screens.

File areas, their garbage collection and expired files. Split out of
`netbbs.net.admin_flow` (issue #1311).
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from netbbs.access_map import LevelContext, level_context
from netbbs.auth.users import User
from netbbs.communities import get_community
from netbbs.files.areas import (
    FileArea,
    FileAreaError,
    create_file_area,
    delete_file_area,
    file_area_siblings,
    list_file_areas,
    move_file_area,
    update_file_area,
)
from netbbs.files.categories import FileAreaCategoryError as FileCategoryError
from netbbs.files.categories import create_category as create_file_category
from netbbs.files.categories import get_category_by_id as get_file_area_category_by_id
from netbbs.files.categories import list_subcategories as list_file_subcategories
from netbbs.files.categories import list_top_level_categories as list_top_level_file_categories
from netbbs.files.gc import GCReport, reclaim_orphaned_blobs
from netbbs.files.entries import (
    FileEntry,
    count_listed_files,
    count_pending_files,
    count_visible_files,
    expired_file_purge_at,
    list_expired_files,
)
from netbbs.ftn.freq import is_area_offered
from netbbs.ftn.networks import list_networks
from netbbs.link.boards import LinkContext
from netbbs.link.files import linked_area_ids, LinkFilesError, is_area_linked, link_file_area
from netbbs.link.carry import (
    CarryDecisionError,
    carried_from_elsewhere,
    carried_to_review,
    mark_carried_reviewed,
)
from netbbs.net.char_input import HELP_KEY, reject_unhandled_key
from netbbs.net.ftn_console import file_area_echo_action, file_area_echo_rows
from netbbs.net.ftn_console import auto_hatch_action, file_area_hatch_rows  # issue #1476
from netbbs.net.ftn_freq_console import area_offer_label, area_offer_rows, toggle_area_offer
from netbbs.net.help_overlay import show_menu_help
from netbbs.net.location import crumbs, entered, located
from netbbs.net.picker import ListColumn
from netbbs.net.resource_editor import (
    FieldSpec,
    bool_field,
    bool_step,
    choice_field,
    choice_step,
    DetailAction,
    DetailMode,
    DetailState,
    text_field,
)
from netbbs.net.session import Session, write_prompt
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.node_theme import effective_accent_color_256, effective_header_color_256
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.rendering import (
    AUTHOR_COLOR,
    DATE_COLOR,
    ERROR_COLOR,
    HEADER_COLOR,
    METADATA_COLOR,
    MUTED_COLOR,
    VALUE_COLOR,
    MenuEntry,
    colored,
    menu_grid,
    menu_key,
    sanitize_text,
    screen_title,
)
from netbbs.rendering.detail import Field, Note, Section
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display, resolve_display_preferences
from netbbs.net.admin_ui import (
    _BACK_ACTION,
    _announce_line,
    _choice_prompt,
    _confirmation_refused,
    _detail_title,
    _fitted_menu,
    _format_bytes,
    _load_chrome,
    _yes_no,
    edit_resource_draft,
    pick_item,
    show_detail,
)
from netbbs.net.admin_fields import (
    _LINK_RECOMMENDATION_VALUES,
    _NAME_REQUIREMENT_HELP,
    _VERIFIED_AGE_HELP,
    _link_recommendation_label,
    _min_age_field,
    _min_age_label,
    _name_requirement_field,
    _name_requirement_label,
    _name_requirement_step,
    _optional_int_field,
    _optional_int_label,
    _resolve_redraw_preference,
    _resource_level_label,
)
from netbbs.net.admin_resources import (
    _BOARD_STATUS_COLUMN,
    _Effective,
    _GATES_COLUMN,
    _LEVEL_COLUMNS,
    _area_columns,
    _describe_resource,
    _effective_by_id,
    _hide_carried_screen,
    _linked_announcement,
    _listing_status,
    _moderator_lines,
    _peer_reach_rows,
    _place_label,
    _remove_resource,
    _sections_text,
)
from netbbs.net.admin_maintenance import _audit_log_screen
from netbbs.net.admin_moderation import _pending_files_screen, _write_file_record
from netbbs.net.admin_boards import _category_field, _community_field


_AREA_COLUMNS = [*_LEVEL_COLUMNS, _BOARD_STATUS_COLUMN, ListColumn("files", 9, VALUE_COLOR), _GATES_COLUMN]


@located("File areas")
async def _area_menu(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext | None = None,
    transfers: Any = None,
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
            await _list_areas_screen(session, lane, actor, link_context=link_context, transfers=transfers)
            await _draw_area_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "g":
            await session.write_line("")
            await _gc_screen(session, lane, actor)
            await _draw_area_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == HELP_KEY:
            await show_menu_help(
                session, "File areas help", _AREA_MENU,
                about="Add file areas and change or remove existing ones. GC storage frees disk space "
                      "that deleted files still hold.",
                header_color=header_color, unicode_style=unicode_style,
            )
            await _draw_area_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


_AREA_MENU = (
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
)


async def _draw_area_menu(
    session: Session, description_level: str, redraw_in_place: bool, unicode_style: bool, collapsed: bool,
    header_color: int | tuple[int, int, int] = HEADER_COLOR,
) -> None:
    await session.write_line("\r\n" + screen_title("File areas",
            breadcrumb=crumbs(session, own=True), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color, node_name_gradient=session.node_name_gradient))
    await session.write_line(
        menu_grid(
            [(
                "",
                list(_AREA_MENU),
            )],
            width=session.terminal_width,
            height=session.terminal_height,
            description_level=description_level,
        )
    )
    await _choice_prompt(session)


async def _gc_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """
    Reference-aware blob garbage collection (GitHub issue #35): always
    shows a dry-run report first, then offers the actual reclaim as an
    explicit hotkey -- the "preview, then explicit confirm" shape is
    kept because this is a one-way filesystem operation the database
    itself can't undo. Issue #282: the confirm used to be a yes/no that
    gated leaving the screen; `[B]ack` now leaves without a question,
    and a report with nothing to reclaim never offers anything.

    Both reports are held on screen until left: a dry run with nothing
    to reclaim, and the result of a real reclaim, each used to print and
    return, and the File areas menu's redraw wiped them unread.
    """
    chrome = await _load_chrome(lane, actor)
    report = await lane.run(reclaim_orphaned_blobs, dry_run=True)
    while True:
        actions = [_BACK_ACTION]
        if report.dry_run and report.reclaimable_blobs:
            actions.insert(0, ("r", menu_key("R", "eclaim now")))
        choice, _page = await show_detail(
            session,
            title=_detail_title(
                session, chrome, "GC storage",
                subtitle="File blobs on disk that no file entry refers to any more.",
            ),
            sections=[_gc_report_section(report)], actions=actions,
            redraw_in_place=chrome.redraw_in_place, unicode_style=chrome.unicode_style,
            help_title="GC storage help",
            help_about=(
                "Files on disk that no file entry refers to any more. Reclaiming deletes them to free the "
                "space."
            ),
        )
        if choice == "b":
            return
        report = await lane.run(reclaim_orphaned_blobs, dry_run=False)


def _gc_report_section(report: GCReport) -> Section:
    rows: list[Field | Note] = [
        Field(
            "Would reclaim" if report.dry_run else "Reclaimed",
            f"{report.reclaimable_blobs} orphaned blob(s), {_format_bytes(report.reclaimable_bytes)}",
            color=VALUE_COLOR if report.reclaimable_blobs else MUTED_COLOR, bold=bool(report.reclaimable_blobs),
        ),
    ]
    if report.skipped_recent:
        rows.append(Field(
            "Skipped",
            f"{report.skipped_recent} recently-written orphan(s) (safety age not yet reached)",
            color=MUTED_COLOR,
        ))
    rows.extend(Field("Error", error, color=ERROR_COLOR) for error in report.errors)
    return Section("Dry run" if report.dry_run else "Result", rows)


def _area_field_specs(
    *, actor: User, redraw_in_place: bool = False, unicode_style: bool = False, collapsed: bool = False,
    levels: LevelContext | None = None,
) -> list[FieldSpec]:
    """One shared field list drives both create and edit (design doc,
    dogfood feature request) -- see `_area_screen`. Identical shape to
    `_board_field_specs`, just "file" in place of "post" throughout."""
    return [
        FieldSpec(
            key="name", label="Name",
            render=lambda d: d.get("name") or "(blank)",
            prompt=text_field("name", required=True),
            brief="The area's display name",
            help="The file area's display name, shown wherever it's listed. Must be non-blank.",
            section="Identity",
        ),
        FieldSpec(
            key="description", label="Description",
            render=lambda d: d.get("description") or "(none)",
            prompt=text_field("description"),
            brief="Shown when browsing the area",
            help="A short explanation of what this file area is for, shown when browsing/selecting it.",
            section="Identity",
        ),
        FieldSpec(
            key="min_read_level", label="Min read level",
            render=lambda d: _resource_level_label(levels, d, "min_read_level"),
            prompt=_optional_int_field("min_read_level", "Minimum read level"),
            brief="Level required to browse it",
            help=(
                "The permission level a caller needs to browse/download from this area. "
                "'none' inherits the parent Community's own default read level if it has "
                "one set, or falls back to no gate."
            ),
            section="Access",
        ),
        FieldSpec(
            key="min_write_level", label="Min write level",
            render=lambda d: _resource_level_label(levels, d, "min_write_level"),
            prompt=_optional_int_field("min_write_level", "Minimum write level"),
            brief="Level required to upload",
            help=(
                "The permission level a caller needs to upload here. 'none' inherits the "
                "parent Community's own default write level if it has one set, or falls "
                "back to no gate."
            ),
            section="Access",
        ),
        FieldSpec(
            key="min_age", label="Min age",
            render=lambda d: _min_age_label(d.get("min_age"), d.get("age_requirement")),
            prompt=_min_age_field("min_age", "age_requirement"),
            brief="Minimum caller age required",
            help=(
                "The minimum caller age required to browse or upload here, checked against "
                "a caller's own birthdate (Your profile › Name & details) even if they've "
                "chosen not to show it publicly. 'none' means no age gate."
            ) + _VERIFIED_AGE_HELP,
            section="Access",
        ),
        FieldSpec(
            key="name_requirement",
            label="Name requirement",
            render=lambda d: _name_requirement_label(d.get("name_requirement")),
            prompt=_name_requirement_field(),
            step=_name_requirement_step(),
            help=_NAME_REQUIREMENT_HELP,
            brief="How uploaders must be identified",
            section="Access",
        ),
        FieldSpec(
            key="community_id", label="Community",
            render=lambda d: d.get("community_id_label") or "(none)",
            prompt=_community_field(
                actor=actor,
                redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            ),
            brief="Parent community, if any",
            help=(
                "The Community this file area belongs to, if any -- an area inside a "
                "Community can inherit its default read/write/age/name-requirement settings "
                "instead of each needing its own. 'none' keeps it outside every Community."
            ),
            section="Organization",
        ),
        FieldSpec(
            key="category_id", label="Category",
            render=lambda d: d.get("category_id_label") or "(none)",
            prompt=_category_field(
                actor=actor, create=create_file_category, error_type=FileCategoryError,
                list_top_level=list_top_level_file_categories, list_subcategories=list_file_subcategories,
                title="File-area category", list_resources=list_file_areas, get_by_id=get_file_area_category_by_id,
                redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            ),
            brief="Where it's grouped in listings",
            help=(
                "Where this file area is grouped when browsing listings -- purely "
                "organizational, has no effect on who can browse or upload."
            ),
            section="Organization",
        ),
        FieldSpec(
            key="pinned", label="Pinned",
            render=lambda d: "yes" if d.get("pinned") else "no",
            prompt=bool_field("pinned"), step=bool_step("pinned"),
            brief="Shown at the top of listings",
            help="Shown at the top of file-area listings, above unpinned areas, regardless of sort order.",
            section="Organization",
        ),
        FieldSpec(
            key="moderated", label="Moderated",
            render=lambda d: "yes" if d.get("moderated") else "no",
            prompt=bool_field("moderated"), step=bool_step("moderated"),
            brief="New uploads need approval first",
            help="New uploads need a moderator or SysOp to approve them before anyone else can download them.",
            section="Moderation",
        ),
        FieldSpec(
            key="max_file_age_days",
            label="Max file age (days)",
            render=lambda d: _optional_int_label(d.get("max_file_age_days"), none_word="unlimited"),
            prompt=_optional_int_field("max_file_age_days", "Max file age in days"),
            brief="Expire files after N days",
            help=(
                "Files older than this expire: callers no longer see them, and they are deleted once "
                "the node's grace period (Settings, Limits & retention) has passed too. 'unlimited' "
                "keeps every file indefinitely."
            ),
            section="Moderation",
        ),
    ]


def _area_draft(area: FileArea | None) -> dict:
    if area is None:
        return {
            "name": "", "description": None, "min_read_level": 0, "min_write_level": 0,
            "community_id": None, "category_id": None, "pinned": False, "moderated": False,
            "max_file_age_days": None, "min_age": None, "name_requirement": None,
            "community_id_label": None, "category_id_label": None, "age_requirement": None,
        }
    return {
        "name": area.name, "description": area.description,
        "min_read_level": area.min_read_level, "min_write_level": area.min_write_level,
        "community_id": area.community_id, "category_id": area.category_id,
        "pinned": area.pinned, "moderated": area.moderated,
        "max_file_age_days": area.max_file_age_days, "min_age": area.min_age,
        "name_requirement": area.name_requirement, "age_requirement": area.age_requirement,
    }


async def _area_screen(
    session: Session, lane: DatabaseLane, actor: User, *, existing: FileArea | None = None,
    link_context: LinkContext | None = None, transfers: Any = None, initial_name: str = "",
) -> FileArea | None:
    """A file area's one screen (issue #1081, design doc §3.5), and the
    screen that creates one -- the same shape as `_board_screen`: its
    files and latest upload, its place and whether it is Linked above the
    fields; the NetBBS Link section after them; [U]p, [D]own, [R]emove,
    [P]ending files, [E]xpired files, [H]istory, [L]ink and [F]ile echo while
    nothing waits to be saved. `initial_name` fills in a new area's name: a
    bad file echo's [C]reate starts from its tag (issue #1466)."""
    with entered(session, sanitize_text(existing.name) if existing is not None else "Create file area"):
        current: dict[str, FileArea | None] = {"area": existing}
        own_fingerprint = link_context.node_identity.fingerprint if link_context is not None else None

        async def save(draft: dict) -> FileArea:
            if not draft["name"]:
                raise FileAreaError("name cannot be blank")
            area = current["area"]
            if area is None:
                created = await lane.run(
                    create_file_area,
                    draft["name"], description=draft["description"], min_read_level=draft["min_read_level"],
                    min_write_level=draft["min_write_level"], category_id=draft["category_id"],
                    pinned=draft["pinned"], moderated=draft["moderated"],
                    max_file_age_days=draft["max_file_age_days"], min_age=draft["min_age"],
                    name_requirement=draft["name_requirement"], community_id=draft["community_id"],
                    age_requirement=draft["age_requirement"], creator=actor,
                )
                _announce_line(session, f"Created file area {created.name!r}.")
                return created
            updated = await lane.run(
                update_file_area,
                area, name=draft["name"], description=draft["description"],
                min_read_level=draft["min_read_level"], min_write_level=draft["min_write_level"],
                category_id=draft["category_id"], pinned=draft["pinned"], moderated=draft["moderated"],
                max_file_age_days=draft["max_file_age_days"], min_age=draft["min_age"],
                name_requirement=draft["name_requirement"], community_id=draft["community_id"],
                age_requirement=draft["age_requirement"], changed_by=actor,
            )
            _announce_line(session, f"Updated {updated.name!r}.")
            return updated

        def _act(run: Callable[[FileArea], Awaitable[bool | None]]):
            async def action(session: Session, lane: DatabaseLane) -> bool:
                await session.write_line("")
                return bool(await run(current["area"]))
            return action

        async def _move(area: FileArea, offset: int) -> bool:
            # Moved within its category (issue #839), as a board moves.
            await lane.run(move_file_area, area, offset, moved_by=actor)
            return False

        async def _remove(area: FileArea) -> bool:
            return await _delete_area_screen(session, lane, actor, area, own_fingerprint=own_fingerprint)

        async def _pending(area: FileArea) -> bool:
            await _pending_files_screen(session, lane, actor, area, link_context=link_context, transfers=transfers)
            return False

        async def _expired(area: FileArea) -> bool:
            await _expired_files_screen(session, lane, actor, area, transfers=transfers)
            return False

        async def _history(area: FileArea) -> bool:
            await _audit_log_screen(
                session, lane, actor, object_type="file_area", object_id=area.id,
                moderators=await lane.run(_moderator_lines, "file_area", area.id),
                title=f"History of {sanitize_text(area.name)}",
            )
            return False

        async def _auto_hatch(area: FileArea) -> bool:  # issue #1476: a toggle, so the screen stays
            await auto_hatch_action(session, lane, actor, area)
            return False

        async def _link(area: FileArea) -> bool:
            await _link_area_screen(session, lane, actor, area, link_context)
            return False

        async def refresh() -> DetailState:
            area = current["area"]
            if area is None:
                return DetailState(draft={**_area_draft(None), "name": initial_name})

            def _load(db: Database):
                fresh = next((a for a in list_file_areas(db) if a.id == area.id), area)
                count, last_file_at = count_visible_files(db, fresh)
                order = [a.id for a in file_area_siblings(db, fresh)]
                community = get_community(db, fresh.community_id) if fresh.community_id is not None else None
                category = (
                    get_file_area_category_by_id(db, fresh.category_id) if fresh.category_id is not None else None
                )
                labels = {
                    "community_id_label": community.name if community is not None else None,
                    "category_id_label": category.name if category is not None else None,
                }
                return fresh, count, last_file_at, order, labels

            area, file_count, last_file_at, order, labels = await lane.run(_load)
            current["area"] = area
            place, total = (order.index(area.id) if area.id in order else 0), len(order)
            if last_file_at is None:
                activity = "no files yet"
            else:
                display_format, display_timezone = await lane.run(resolve_display_preferences)
                activity = "last upload " + format_for_display(
                    last_file_at, override_format=display_format, override_timezone=display_timezone,
                )
            linked = await lane.run(is_area_linked, area) if link_context is not None else False
            header_bits = [f"{file_count} files ({activity})", f"place {_place_label(place, total, pinned=area.pinned)}"]
            after = ""
            if link_context is not None:
                header_bits.append("Linked" if linked else "not Linked")
                rows: list[Field | Note] = [Field("Linked", _yes_no(linked))]
                if linked:
                    rows.extend(await _peer_reach_rows(lane, link_context, link_context.link_node.file_areas.get(area.area_id)))
                after = _sections_text(
                    [Section("NetBBS Link", rows)], width=session.terminal_width,
                    unicode_style=await lane.run(unicode_style_enabled, actor),
                )
            # --- FTN file echo (issue #1462): an area is local, Linked or FTN,
            # never two, so a Linked area shows no FTN section and offers no
            # [F]ile echo. ---
            ftn_networks = [] if linked else await lane.run(list_networks)
            hatch_rows: list = []  # issue #1476
            if ftn_networks:
                ftn_rows = await lane.run(file_area_echo_rows, area)
                # --- hatching (issue #1476) ---
                hatch_rows = await lane.run(file_area_hatch_rows, area)
                ftn_rows = [*ftn_rows, *hatch_rows]
                # --- end hatching ---
                ftn_rows += await lane.run(area_offer_rows, area)  # issue #1475
                after = (after + "\r\n" if after else "") + _sections_text(
                    [Section("FTN", ftn_rows)], width=session.terminal_width,
                    unicode_style=await lane.run(unicode_style_enabled, actor),
                )
            # --- end FTN file echo ---
            actions = []
            if place > 0:
                actions.append(DetailAction("u", menu_key("U", "p"), _act(lambda a: _move(a, -1)), brief="Earlier in the callers' list"))
            if place < total - 1:
                actions.append(DetailAction("d", menu_key("D", "own"), _act(lambda a: _move(a, 1)), brief="Later in the callers' list"))
            actions += [
                DetailAction("r", menu_key("R", "emove"), _act(_remove), brief="Permanently remove this area"),
                DetailAction("p", menu_key("P", "ending files"), _act(_pending), brief="Review uploads awaiting approval"),
                DetailAction("e", menu_key("E", "xpired files"), _act(_expired), brief="Recover before they are purged"),
                DetailAction("h", menu_key("H", "istory"), _act(_history), brief="Its moderators and what they did"),
            ]
            if link_context is not None and not linked:
                actions.append(DetailAction("l", menu_key("L", "ink this file area"), _act(_link), brief="Share it via NetBBS Link"))
            if ftn_networks:  # issue #1462
                actions.append(DetailAction(
                    "f", menu_key("F", "ile echo (FTN)"),
                    _act(lambda a: file_area_echo_action(session, lane, actor, a)),
                    brief="Carry an FTN file echo here"))
                # --- hatching (issue #1476) ---
                if hatch_rows:
                    actions.append(DetailAction(
                        "a", menu_key("A", "uto-hatch"),
                        _act(_auto_hatch), brief="Hatch approved uploads"))
                # --- end hatching ---
                # --- file requests (issue #1475) ---
                offered = await lane.run(is_area_offered, area)
                actions.append(DetailAction(
                    "o", area_offer_label(offered),
                    _act(lambda a: toggle_area_offer(session, lane, actor, a)),
                    brief="Let FTN nodes request its files"))
                # --- end file requests ---
            return DetailState(
                draft={**_area_draft(area), **labels},
                title=sanitize_text(area.name),
                header=colored(" · ".join(header_bits), fg_color=MUTED_COLOR),
                after_fields=after, actions=actions,
            )

        redraw_in_place, redraw_hint = await lane.run(_resolve_redraw_preference, actor)
        unicode_style = await lane.run(unicode_style_enabled, actor)
        collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
        return await edit_resource_draft(
            session, lane,
            title=sanitize_text(existing.name) if existing is not None else "Create file area", own=True,
            fields=_area_field_specs(
                actor=actor, redraw_in_place=redraw_in_place,
                unicode_style=unicode_style, collapsed=collapsed,
                levels=await lane.run(level_context),
            ),
            draft={}, save=save, error_type=FileAreaError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
            description_level=await lane.run(menu_description_level, actor),
            redraw_in_place=redraw_in_place, redraw_hint=redraw_hint,
            unicode_style=unicode_style,
            collapsed=collapsed,
            accent_color=await lane.run(effective_accent_color_256),
            header_color=await lane.run(effective_header_color_256),
            detail=DetailMode(refresh=refresh, stay_after_save=existing is not None),
        )


async def _list_areas_screen(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext | None = None,
    transfers: Any = None,
) -> None:
    def _load_areas(db: Database):
        # The SysOp's order (issue #839), which callers' lists follow within
        # each category and Community; [U]p/[D]own on a detail screen move
        # one among those, and its Place row says where it sits.
        areas = list_file_areas(db, order_by="sysop")
        counts = {area.id: (count_listed_files(db, area)[0], count_pending_files(db, area)) for area in areas}
        return areas, _effective_by_id(db, areas), counts, carried_to_review(db, "file_areas")

    # As the board list: an area's [B]ack comes back here (issue #681).
    reopen_at: int | None = None
    while True:
        areas, effective, counts, to_review = await lane.run(_load_areas)
        linked_areas = await lane.run(linked_area_ids)
        if not areas and reopen_at is not None:
            return
        selected = await pick_item(
            session, areas,
            name_of=lambda a: a.name,
            stable_id_of=lambda a: a.id,
            description_of=lambda a: _area_description(a, effective[a.id], a.area_id in to_review),
            columns=_AREA_COLUMNS,
            column_values_of=lambda a: _area_columns(a, effective[a.id], counts[a.id], a.area_id in to_review),
            title="File areas", own=True,
            empty_message="No file areas yet.",
            start_stable_id=reopen_at,
            linked_of=lambda a, linked=linked_areas: a.id in linked,  # issue #1104
            redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
            unicode_style=await lane.run(unicode_style_enabled, actor),
            collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
            accent_color=await lane.run(effective_accent_color_256),
            header_color=await lane.run(effective_header_color_256),
        )
        if selected is None:
            return
        reopen_at = selected.id
        await _area_detail_screen(
            session, lane, actor, selected, link_context=link_context, transfers=transfers,
        )


def _area_description(area: FileArea, effective: _Effective, to_review: bool = False) -> str:
    """Narrow-terminal fallback; see `_board_description`."""
    description = _describe_resource(
        effective.read, effective.write, _listing_status(area.moderated, area.pinned), effective
    )
    return f"to review; {description}" if to_review else description


async def _area_detail_screen(
    session: Session, lane: DatabaseLane, actor: User, area: FileArea, *,
    link_context: LinkContext | None = None, transfers: Any = None,
) -> None:
    """A file area's screen is its editor (issue #1081): see `_area_screen`."""
    await lane.run(mark_carried_reviewed, "file_areas", area.area_id)
    await _area_screen(session, lane, actor, existing=area, link_context=link_context, transfers=transfers)


def _link_area_field_specs() -> list[FieldSpec]:
    """`_link_board_field_specs`'s own subset -- no `forked_from` (file-
    area origin succession is not built, design doc §11), and
    `max_file_age_days` in place of `max_post_age_days`."""
    return [
        FieldSpec(
            key="default_min_read_level",
            label="Recommended read level",
            render=lambda d: _optional_int_label(d.get("default_min_read_level")),
            prompt=_optional_int_field("default_min_read_level", "Recommended minimum read level"),
            brief="Sent to peers as a default",
            help="Recommends a minimum read level to peers that don't set their own. 'none' sends no recommendation.",
        ),
        FieldSpec(
            key="default_min_write_level",
            label="Recommended write level",
            render=lambda d: _optional_int_label(d.get("default_min_write_level")),
            prompt=_optional_int_field("default_min_write_level", "Recommended minimum write level"),
            brief="Sent to peers as a default",
            help="Recommends a minimum write level to peers that don't set their own. 'none' sends no recommendation.",
        ),
        FieldSpec(
            key="default_moderated",
            label="Recommended moderated",
            render=lambda d: _link_recommendation_label(d.get("default_moderated")),
            prompt=choice_field("default_moderated", _LINK_RECOMMENDATION_VALUES),
            step=choice_step("default_moderated", _LINK_RECOMMENDATION_VALUES),
            brief="Sent to peers as a default",
            help="Recommends whether uploads should need approval, to peers that don't set their own.",
        ),
        FieldSpec(
            key="default_max_file_age_days",
            label="Recommended max file age",
            render=lambda d: _optional_int_label(d.get("default_max_file_age_days"), none_word="no recommendation"),
            prompt=_optional_int_field("default_max_file_age_days", "Recommended max file age in days"),
            brief="Sent to peers as a default",
            help="Recommends auto-purging files older than N days, to peers that don't set their own.",
        ),
        FieldSpec(
            key="default_min_age",
            label="Recommended min age",
            render=lambda d: _optional_int_label(d.get("default_min_age")),
            prompt=_min_age_field("default_min_age"),
            brief="Sent to peers as a default",
            help="Recommends a minimum caller age to peers that don't set their own.",
        ),
        FieldSpec(
            key="default_name_requirement",
            label="Recommended name requirement",
            render=lambda d: _name_requirement_label(d.get("default_name_requirement")),
            prompt=_name_requirement_field("default_name_requirement"),
            step=_name_requirement_step("default_name_requirement"),
            brief="Sent to peers as a default",
            help=_NAME_REQUIREMENT_HELP + " Recommended to peers that don't set their own.",
        ),
    ]


async def _link_area_screen(
    session: Session, lane: DatabaseLane, actor: User, area: FileArea, link_context: LinkContext
) -> None:
    """
    `[L]ink this file area` (design doc §11, issue #89 -- previously
    defined by `netbbs.link.files.link_file_area` but, unlike `link_
    board`, never actually reachable from any live UI action; this is
    that missing call site). Mirrors `_link_board_screen`'s own field
    list, minus `forked_from`.

    Dogfood report: same fixed-linear-chain bug `_link_board_screen`
    had, same `edit_resource_draft` fix -- see `_link_board_field_
    specs`'s own docstring. `actor` is newly required here (it wasn't
    before): the draft-editor screen needs it to resolve display
    preferences the old plain-prompt chain never touched.
    """
    draft: dict = {
        "default_min_read_level": area.min_read_level,
        "default_min_write_level": area.min_write_level,
        "default_moderated": None,
        "default_max_file_age_days": None,
        "default_min_age": area.min_age,
        "default_name_requirement": area.name_requirement,
    }

    async def save(draft: dict) -> bool:
        genesis = await lane.run(
            link_file_area,
            area,
            node_identity=link_context.node_identity,
            default_min_read_level=draft["default_min_read_level"],
            default_min_write_level=draft["default_min_write_level"],
            default_moderated=draft["default_moderated"],
            default_max_file_age_days=draft["default_max_file_age_days"],
            default_min_age=draft["default_min_age"],
            default_name_requirement=draft["default_name_requirement"],
            default_age_requirement=area.age_requirement,
        )
        link_context.link_node.file_areas[area.area_id] = genesis
        link_context.link_node.known_event_ids.add(genesis.content_id)
        link_context.link_node.events[genesis.content_id] = genesis.to_dict()
        _announce_line(session, _linked_announcement(area.name))
        return True

    redraw_in_place, redraw_hint = await lane.run(_resolve_redraw_preference, actor)
    await edit_resource_draft(
        session, lane,
        title="Link this file area",
        fields=_link_area_field_specs(), draft=draft, save=save, error_type=LinkFilesError,
        save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=redraw_in_place, redraw_hint=redraw_hint,
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )


async def _delete_area_screen(
    session: Session, lane: DatabaseLane, actor: User, area: FileArea, *,
    own_fingerprint: str | None = None,
) -> bool:
    # Issue #683: a carried file area whose origin is another node is hidden,
    # not destroyed -- see `_hide_carried_screen`.
    try:
        carried = await lane.run(carried_from_elsewhere, "file_areas", area.area_id, own_fingerprint)
    except CarryDecisionError as exc:
        _announce_line(session, colored(f"Cannot delete {area.name!r}: {exc}", fg_color=ERROR_COLOR))
        return False
    if carried:
        return await _hide_carried_screen(session, lane, actor, "file_areas", area.area_id, area.name, own_fingerprint=own_fingerprint)
    await session.write_line(
        colored(
            "\r\nThis permanently deletes the file area, all of its files, and any "
            "moderator grants scoped to it. This cannot be undone.",
            fg_color=MUTED_COLOR,
        )
    )
    await write_prompt(
        session, f"Type the file area name {area.name!r} to confirm, or anything else to cancel: "
    )
    confirmation = (await session.read_line()).strip()
    if confirmation != area.name:
        _confirmation_refused(session, confirmation, area.name)
        return False
    # Issues #669/#683: a Linked file area keeps its genesis and is recorded as
    # excluded, so it stays declared as not carried and does not come back.
    return await _remove_resource(
        session, lane, actor, "file_areas", area.area_id, area.name, own_fingerprint=own_fingerprint,
        delete=lambda db: delete_file_area(db, area, deleted_by=actor),
    )


@located("Expired files")
async def _expired_files_screen(
    session: Session, lane: DatabaseLane, actor: User, area: FileArea, *, transfers: Any = None,
) -> None:
    """`[E]xpired files` (design doc §5.3, issue #639): SysOp recovery.
    Expiry ends a caller's reach, but an expired file's bytes stay in
    storage until the grace period ends, and this is where they are
    fetched back without shell access. Listed oldest first, which is
    also the order they will be purged in."""
    while True:
        files = await lane.run(list_expired_files, area, requesting_user=actor)
        selected = await pick_item(
            session, files,
            name_of=lambda f: f.filename,
            stable_id_of=lambda f: f.id,
            description_of=lambda f: f"by {f.uploader_label}",
            title=f"Expired files in {area.name!r}", own=True,
            empty_message="No expired files.",
            redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
            unicode_style=await lane.run(unicode_style_enabled, actor),
            collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
            accent_color=await lane.run(effective_accent_color_256),
            header_color=await lane.run(effective_header_color_256),
        )
        if selected is None:
            return
        await _expired_file_screen(session, lane, actor, selected, area, transfers=transfers)


async def _expired_file_screen(
    session: Session, lane: DatabaseLane, actor: User, entry: FileEntry, area: FileArea, *,
    transfers: Any = None,
) -> None:
    """One expired file, with `[D]ownload` under the same transport rule
    as the pending review (issue #475). Recovery is the whole of it:
    putting the file back in the listing is not an action here, since
    expired means gone to callers and a re-upload is how a SysOp who
    wants it back says so."""
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    display_format, display_timezone = await lane.run(resolve_display_preferences)
    when = format_for_display(entry.created_at, override_format=display_format, override_timezone=display_timezone)
    purge_at = await lane.run(expired_file_purge_at, area, entry)
    purged = (
        format_for_display(purge_at, override_format=display_format, override_timezone=display_timezone)
        if purge_at is not None else "not scheduled"
    )
    from netbbs.net.file_flow import send_file_to_caller, supports_zmodem

    can_download = supports_zmodem(session) or transfers is not None

    async def _draw() -> None:
        used_rows = await _write_file_record(
            session, entry, heading="Expired file", fields=[
                Field("By", entry.uploader_label, color=AUTHOR_COLOR),
                Field("Uploaded", when, color=DATE_COLOR),
                Field("Purge date", purged, color=DATE_COLOR),
                Field("Size", f"{_format_bytes(entry.size_bytes)} ({entry.size_bytes} bytes)"),
                Field("SHA-256", entry.sha256, color=METADATA_COLOR),
            ],
            redraw_in_place=redraw_in_place, unicode_style=unicode_style,
            collapsed=collapsed, header_color=header_color,
        )
        entries = []
        if can_download:
            entries.append(MenuEntry(label=menu_key("D", "ownload"), brief="Recover it before it is purged"))
        entries.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to the expired list"))
        options = _fitted_menu(entries, description_level, session=session, used_rows=used_rows)
        await session.write_line(f"\r\n{options}")
        await _choice_prompt(session)

    await _draw()
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "d" and can_download:
            await session.write_line("")
            # As on the pending review: `send_file_to_caller` announces its
            # outcome, and the redraw below shows it.
            await send_file_to_caller(session, lane, area, entry, actor, transfers=transfers)
            await _draw()
        elif choice == HELP_KEY:
            await show_menu_help(
                session, "Expired file help",
                [MenuEntry(label=menu_key("D", "ownload"), brief="Recover it before it is purged")]
                if can_download else [],
                about="A file that expired: callers can no longer reach it, and its bytes are deleted "
                      "on the purge date. To put it back in the area, upload it again.",
                header_color=header_color, unicode_style=unicode_style,
            )
            await _draw()
        else:
            await session.write(reject_unhandled_key(choice))
