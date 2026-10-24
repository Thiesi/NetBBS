"""SysOp console board, Community, category and moderator screens.

Split out of `netbbs.net.admin_flow` (issue #1311).
"""

from __future__ import annotations

from typing import Awaitable, Callable, Sequence

from netbbs.access_map import LevelContext, level_context
from netbbs.auth.users import User, list_users
from netbbs.boards.boards import (
    Board,
    BoardError,
    board_siblings,
    create_board,
    delete_board,
    list_boards,
    move_board,
    update_board,
)
from netbbs.boards.categories import CategoryError
from netbbs.boards.categories import create_category as create_board_category
from netbbs.boards.categories import delete_category as delete_board_category
from netbbs.boards.categories import move_category as move_board_category
from netbbs.boards.categories import update_category as update_board_category
from netbbs.boards.categories import get_category_by_id as get_board_category_by_id
from netbbs.boards.categories import list_subcategories as list_board_subcategories
from netbbs.boards.categories import list_top_level_categories as list_top_level_board_categories
from netbbs.boards.posts import count_listed_posts, count_pending_posts, count_visible_posts
from netbbs.chat.categories import CategoryError as ChannelCategoryError
from netbbs.chat.categories import create_category as create_channel_category
from netbbs.chat.categories import delete_category as delete_channel_category
from netbbs.chat.categories import move_category as move_channel_category
from netbbs.chat.categories import update_category as update_channel_category
from netbbs.chat.categories import list_subcategories as list_channel_subcategories
from netbbs.chat.categories import list_top_level_categories as list_top_level_channel_categories
from netbbs.chat.channels import list_channels
from netbbs.communities import (
    Community,
    CommunityError,
    create_community,
    delete_community,
    move_community,
    get_community,
    list_communities,
    update_community,
)
from netbbs.doors import list_doors
from netbbs.files.areas import list_file_areas
from netbbs.files.categories import FileAreaCategoryError as FileCategoryError
from netbbs.files.categories import create_category as create_file_category
from netbbs.files.categories import delete_category as delete_file_category
from netbbs.files.categories import move_category as move_file_category
from netbbs.files.categories import update_category as update_file_category
from netbbs.files.categories import list_subcategories as list_file_subcategories
from netbbs.files.categories import list_top_level_categories as list_top_level_file_categories
from netbbs.link.events import BOARD_POSTING_MODES
from netbbs.link.boards import (
    linked_board_ids,
    LinkBoardsError,
    LinkContext,
    accept_board_origin_transfer,
    board_origin_fingerprint,
    board_posting_mode,
    close_board_if_linked,
    is_board_closed,
    is_board_linked,
    is_board_origin_orphaned,
    link_board,
    offer_board_origin_transfer,
    set_board_posting,
)
from netbbs.ftn.networks import board_area, list_networks
from netbbs.net.ftn_console import board_echo_action, board_echo_rows, board_origin_action
from netbbs.link.node_profiles import identity_for_peer
from netbbs.link.communities import (
    LinkCommunitiesError,
    close_own_community,
    community_link,
    leave_community,
    linked_community_ids,
    promote_community,
)
from netbbs.link.carry import (
    CarryDecisionError,
    carried_from_elsewhere,
    carried_to_review,
    mark_carried_reviewed,
)
from netbbs.moderation.roles import (
    BoardPermission,
    ChannelPermission,
    ModeratorGrantError,
    grant_everywhere,
    grant_permissions,
    list_grants_for_community,
    list_grants_for_user,
    revoke_permissions,
)
from netbbs.net.char_input import HELP_KEY, help_key_label, reject_unhandled_key
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.help_overlay import show_help, show_menu_help
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
    ERROR_COLOR,
    HEADER_COLOR,
    MUTED_COLOR,
    VALUE_COLOR,
    WARNING_COLOR,
    MenuEntry,
    action_bar,
    colored,
    menu_key,
    sanitize_text,
    screen_title,
)
from netbbs.rendering.detail import Field, Note, Section
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display, resolve_display_preferences
from netbbs.net.admin_ui import (
    _announce,
    _announce_line,
    _choice_prompt,
    _confirmation_refused,
    _menu_row,
    _yes_no,
    edit_resource_draft,
    pick_item,
)
from netbbs.net.admin_fields import (
    _LINK_RECOMMENDATION_VALUES,
    _NAME_REQUIREMENT_HELP,
    _VERIFIED_AGE_HELP,
    _community_default_label,
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
    _community_columns,
    _count_cell,
    _describe_resource,
    _effective_by_id,
    _gate_cell,
    _hide_carried_screen,
    _level_cell,
    _linked_announcement,
    _linked_node_label,
    _listing_status,
    _moderator_lines,
    _peer_reach_rows,
    _place_label,
    _remove_resource,
    _sections_text,
    _pick_new_origin,
    _warn_about_changed_node_identity,
)
from netbbs.net.admin_maintenance import _audit_log_screen
from netbbs.net.admin_moderation import _pending_posts_screen


async def _pick_optional_category(
    session: Session,
    lane: DatabaseLane,
    actor: User,
    *,
    create,
    error_type,
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
    `netbbs.net.board_flow._browse_boards_in_category`'s docstring for
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

    async def _create():
        # Same dead end as the Community field's, and the same fix
        # (issue #530): with no categories defined this picker used to
        # return instantly and the field silently resolved to "none".
        # A newly created category is returned as the selection without
        # going back through `_load_top_level`'s filtering -- the SysOp
        # just made it for this resource, which is answer enough.
        return await _create_category_screen(
            session, lane, actor, create=create,
            # The *filtered* loader, not the raw one (Codex review).
            # `_load_top_level` hides categories used only by another
            # Community; passing `list_top_level` straight through meant
            # [C]reate -> [P]arent listed them again and let the new
            # category be nested under one, walking around the same
            # admin-side leak-prevention invariant
            # `test_admin_category_picker_leak_prevention` exists to
            # hold. The nested editor inherits the scope it was opened
            # from.
            list_top_level=_load_top_level,
            error_type=error_type,
        )

    selected = await pick_item(
        session, top_level,
        name_of=lambda c: c.name,
        stable_id_of=lambda c: c.id,
        title=title,
        empty_message="No categories exist yet.",
        on_create=_create,
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
    actor: User,
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
    `None` if cancelled (the picker's own `[B]ack`)
    -- see `_pick_optional_category`'s own docstring for why this no
    longer gates the picker behind a separate "Assign a Community?"
    yes/no first.

    `[C]reate` (issue #530) makes one from here and returns it
    selected. The dead end it removes: with no Communities defined, this
    picker returned instantly, the field silently resolved to "none",
    and the SysOp had to leave the editor, create a Community from the
    console, and come back. It is offered on a populated list too,
    because the original reasoning -- that this only bites before the
    first Community exists -- was wrong: wanting to file a board under a
    *new* Community is an ordinary thing to want at any point."""

    async def _create() -> Community | None:
        return await _community_screen(session, lane, actor)

    selected = await pick_item(
        session, await lane.run(list_communities),
        name_of=lambda c: c.name,
        stable_id_of=lambda c: c.id,
        title="Community",
        empty_message="No Communities exist yet.",
        on_create=_create,
        redraw_in_place=redraw_in_place,
        unicode_style=unicode_style,
        collapsed=collapsed,
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )
    return selected.id if selected is not None else None


def _community_field(
    key: str = "community_id",
    *,
    actor: User,
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
            session, lane, actor,
            redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
        )
        draft[key] = community_id
        community = await lane.run(get_community, community_id) if community_id is not None else None
        draft[f"{key}_label"] = community.name if community is not None else None

    return prompt


def _category_field(
    *,
    actor: User,
    create,
    error_type,
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
            session, lane, actor,
            create=create, error_type=error_type,
            list_top_level=list_top_level, list_subcategories=list_subcategories,
            title=title, community_id=draft.get("community_id"), resources=await lane.run(list_resources),
            redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
        )
        draft["category_id"] = category_id
        category = await lane.run(get_by_id, category_id) if category_id is not None else None
        draft["category_id_label"] = category.name if category is not None else None

    return prompt


@located("Communities")
async def _community_menu(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext | None = None
) -> None:
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
            await _community_screen(session, lane, actor, link_context=link_context)
            await _draw_community_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "l":
            await session.write_line("")
            await _list_communities_screen(session, lane, actor, link_context=link_context)
            await _draw_community_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == HELP_KEY:
            await show_menu_help(
                session, "Communities help", _COMMUNITY_MENU,
                about="A Community gathers boards, file areas, chat channels and doors around one topic; "
                      "callers find it under Topics on the main menu. Its defaults (levels, age, name rule) "
                      "apply to every resource inside that leaves its own field at 'none', from the next "
                      "time a caller opens one. List opens a Community to edit, reorder or remove it.",
                header_color=header_color, unicode_style=unicode_style,
            )
            await _draw_community_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


_COMMUNITY_MENU = (
    MenuEntry(label=menu_key("C", "reate"), brief="Add a new Community"),
    MenuEntry(label=menu_key("L", "ist"), brief="Browse and edit Communities"),
    MenuEntry(label=menu_key("B", "ack"), brief="Return to the Content menu"),
)


async def _draw_community_menu(
    session: Session, description_level: str, redraw_in_place: bool, unicode_style: bool, collapsed: bool,
    header_color: int | tuple[int, int, int] = HEADER_COLOR,
) -> None:
    await session.write_line("\r\n" + screen_title("Communities",
            breadcrumb=crumbs(session, own=True), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color, node_name_gradient=session.node_name_gradient))
    await session.write_line(
        _menu_row(
            list(_COMMUNITY_MENU),
            description_level,
            width=session.terminal_width,
            height=session.terminal_height,
        )
    )
    await _choice_prompt(session)


def _community_field_specs(
    *, levels: LevelContext | None = None, community_id: int | None = None
) -> list[FieldSpec]:
    """One shared field list drives both create and edit (design doc,
    dogfood feature request) -- see `_community_screen`. Unlike board/
    channel/file-area, a Community has no `community_id`/`category_id`
    of its own (nothing above it) and no `pinned`/`moderated` -- just
    name/description/hidden plus its four `default_*` inheritance
    fields (design doc §16)."""
    return [
        FieldSpec(
            key="name", label="Name",
            render=lambda d: d.get("name") or "(blank)",
            prompt=text_field("name", required=True),
            brief="The community's display name",
            help="The Community's display name, shown wherever it's listed. Must be non-blank.",
        ),
        FieldSpec(
            key="description", label="Description",
            render=lambda d: d.get("description") or "(none)",
            prompt=text_field("description"),
            brief="Shown in the community directory",
            help="A short explanation of what this Community is for, shown in the Communities "
            "directory alongside its name.",
        ),
        FieldSpec(
            key="hidden", label="Hidden",
            render=lambda d: "yes" if d.get("hidden") else "no",
            prompt=bool_field("hidden"), step=bool_step("hidden"),
            brief="Hide from the communities list",
            help=(
                "Takes it off Topics for everyone but SysOps. Its boards, areas and channels still "
                "appear in the main lists and keep working; the help-desk bot leaves them out, and "
                "FTN file requests cannot fetch its files."
            ),
        ),
        FieldSpec(
            key="default_min_read_level",
            label="Default read level",
            render=lambda d: _community_default_label(levels, d, "default_min_read_level", community_id),
            prompt=_optional_int_field("default_min_read_level", "Default minimum read level"),
            brief="Default read level, inherited",
            help=(
                "The read level of every board and file area in this Community whose own Min read "
                "level is 'none'. Chat channels keep their own level. 'none' here: those resources "
                "are open to every level."
            ),
        ),
        FieldSpec(
            key="default_min_write_level",
            label="Default write level",
            render=lambda d: _community_default_label(levels, d, "default_min_write_level", community_id),
            prompt=_optional_int_field("default_min_write_level", "Default minimum write level"),
            brief="Default write level, inherited",
            help=(
                "The level needed to post or upload in every board and file area in this Community "
                "whose own Min write level is 'none'. 'none' here: anyone who can read may write."
            ),
        ),
        FieldSpec(
            key="default_min_age",
            label="Default min age",
            render=lambda d: _min_age_label(d.get("default_min_age"), d.get("default_age_requirement")),
            prompt=_min_age_field("default_min_age", "default_age_requirement"),
            brief="Default min. age, inherited",
            help=(
                "The minimum age for every board, file area and channel in this Community whose own "
                "Min age is 'none'. 'none' here: no age gate."
            ) + _VERIFIED_AGE_HELP,
        ),
        FieldSpec(
            key="default_name_requirement",
            label="Default name requirement",
            render=lambda d: _name_requirement_label(d.get("default_name_requirement")),
            prompt=_name_requirement_field("default_name_requirement"),
            step=_name_requirement_step("default_name_requirement"),
            help=_NAME_REQUIREMENT_HELP + " Applies to every board, file area and channel in this "
            "Community whose own Name requirement is 'none'.",
            brief="Default name rule, inherited",
        ),
    ]


async def _community_screen(
    session: Session, lane: DatabaseLane, actor: User, *, existing: Community | None = None,
    link_context: LinkContext | None = None,
) -> Community | None:
    """A Community's one screen (issue #1081, design doc §3.5), and the
    screen that creates one: its place in the callers' list above the
    fields, the fields chosen by the cursor, and [U]p, [D]own and [R]emove
    while nothing is waiting to be saved. Creating uses the same screen
    with an empty draft and no actions, and returns what it created.

    Creating needs no follow-up trip to configure the rest: every field is
    on this one screen at its own sensible default, as for boards, channels
    and file areas."""
    current: dict[str, Community | None] = {"community": existing}

    def _draft_of(community: Community | None) -> dict:
        if community is None:
            return {
                "name": "", "description": None, "hidden": False,
                "default_min_read_level": None, "default_min_write_level": None,
                "default_min_age": None, "default_name_requirement": None,
                "default_age_requirement": None,
            }
        return {
            "name": community.name, "description": community.description, "hidden": community.hidden,
            "default_min_read_level": community.default_min_read_level,
            "default_min_write_level": community.default_min_write_level,
            "default_min_age": community.default_min_age,
            "default_name_requirement": community.default_name_requirement,
            "default_age_requirement": community.default_age_requirement,
        }

    async def save(draft: dict) -> Community:
        if not draft["name"]:
            raise CommunityError("name cannot be blank")
        community = current["community"]
        if community is None:
            created = await lane.run(
                create_community,
                draft["name"], description=draft["description"], hidden=draft["hidden"],
                default_min_read_level=draft["default_min_read_level"],
                default_min_write_level=draft["default_min_write_level"],
                default_min_age=draft["default_min_age"],
                default_name_requirement=draft["default_name_requirement"],
                default_age_requirement=draft["default_age_requirement"], creator=actor,
            )
            _announce_line(session, f"Created Community {created.name!r}.")
            return created
        updated = await lane.run(
            update_community,
            community, name=draft["name"], description=draft["description"], hidden=draft["hidden"],
            default_min_read_level=draft["default_min_read_level"],
            default_min_write_level=draft["default_min_write_level"],
            default_min_age=draft["default_min_age"],
            default_name_requirement=draft["default_name_requirement"],
            default_age_requirement=draft["default_age_requirement"], changed_by=actor,
        )
        _announce_line(session, f"Updated {updated.name!r}.")
        return updated

    def _move(offset: int) -> Callable[[Session, DatabaseLane], Awaitable[bool]]:
        async def run(session: Session, lane: DatabaseLane) -> bool:
            await lane.run(move_community, current["community"], offset, moved_by=actor)
            return False
        return run

    async def _remove(session: Session, lane: DatabaseLane) -> bool:
        return await _delete_community_screen(session, lane, actor, current["community"], link_context=link_context)

    async def _link(session: Session, lane: DatabaseLane) -> bool:
        await _link_community_screen(session, lane, actor, current["community"], link_context)
        return False

    async def _unlink(session: Session, lane: DatabaseLane) -> bool:
        await _unlink_community_screen(session, lane, actor, current["community"], link_context)
        return False

    async def refresh() -> DetailState:
        if current["community"] is None:
            return DetailState(draft=_draft_of(None))

        def _load(db: Database) -> tuple[Community | None, list[int]]:
            return get_community(db, current["community"].id), [c.id for c in list_communities(db)]

        community, order = await lane.run(_load)
        if community is not None:
            current["community"] = community
        community = current["community"]
        place = order.index(community.id) if community.id in order else 0
        actions = []
        if place > 0:
            actions.append(DetailAction("u", menu_key("U", "p"), _move(-1), brief="Earlier in the callers' list"))
        if place < len(order) - 1:
            actions.append(DetailAction("d", menu_key("D", "own"), _move(1), brief="Later in the callers' list"))
        actions.append(DetailAction("r", menu_key("R", "emove"), _remove, brief="Permanently remove it"))
        header_bits = [f"Place {place + 1} of {len(order)} in the callers' Communities list"]
        after = ""
        own_fingerprint = link_context.node_identity.fingerprint if link_context is not None else None
        link = await lane.run(community_link, community.id, own_fingerprint)
        if link is not None:
            header_bits.append(_community_link_state(link))
        if link_context is not None:
            after = _sections_text(
                [Section("NetBBS Link", _community_link_rows(link, link_context))], width=session.terminal_width,
                unicode_style=await lane.run(unicode_style_enabled, actor),
            )
            if link is None or (link.closed and not link.own):
                actions.append(DetailAction(
                    "l", menu_key("L", "ink this Community"), _link, brief="Share it via NetBBS Link",
                ))
            elif link.own and not link.closed:
                actions.append(DetailAction(
                    "c", menu_key("C", "lose on Link"), _unlink, brief="Ends it on every carrying node",
                ))
        header = colored(" · ".join(header_bits), fg_color=MUTED_COLOR)
        return DetailState(
            draft=_draft_of(community), header=header, actions=actions, title=sanitize_text(community.name),
            after_fields=after,
        )

    redraw_in_place, redraw_hint = await lane.run(_resolve_redraw_preference, actor)
    return await edit_resource_draft(
        session, lane,
        title=sanitize_text(existing.name) if existing is not None else "Create Community",
        fields=_community_field_specs(
            levels=await lane.run(level_context), community_id=existing.id if existing is not None else None,
        ),
        draft={}, save=save, error_type=CommunityError,
        save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=redraw_in_place, redraw_hint=redraw_hint,
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
        detail=DetailMode(refresh=refresh, stay_after_save=existing is not None),
    )


async def _list_communities_screen(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext | None = None
) -> None:
    """The Communities in the order callers see them (issue #838). Picking
    one opens its screen, to edit, move or remove it; leaving that comes
    back here, on it -- the same loop the category list has, so a SysOp
    ordering several Communities sees each move land."""
    reopen_at: int | None = None
    while True:
        communities = await lane.run(list_communities)
        linked = await lane.run(linked_community_ids)
        selected = await pick_item(
            session, communities,
            name_of=lambda c: c.name,
            stable_id_of=lambda c: c.id,
            description_of=_community_description,
            linked_of=lambda c: c.id in linked,
            columns=_COMMUNITY_COLUMNS,
            column_values_of=_community_columns,
            title="Communities", own=True,
            help_about=(
                'The Communities in the order callers see them under Topics, with their default read and '
                'write levels, whether they are listed, and their default gates. Gates: 18+ an age, name '
                'a verified name, name+ one shown. Pick one to edit, move or remove it.'
            ),
            empty_message="No Communities yet.",
            start_stable_id=reopen_at,
            redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
            unicode_style=await lane.run(unicode_style_enabled, actor),
            collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
            accent_color=await lane.run(effective_accent_color_256),
            header_color=await lane.run(effective_header_color_256),
        )
        if selected is None:
            return
        reopen_at = selected.id
        await _community_detail_screen(session, lane, actor, selected, link_context=link_context)


def _community_description(community: Community) -> str:
    """A Community sits at the top of the cascade, so its own
    `default_*` values are already the effective ones -- nothing above
    it to resolve. Its gates still belong in the fallback text, for the
    same reason a leaf's do."""
    gates, _ = _gate_cell(community.default_min_age, community.default_name_requirement, community.default_age_requirement)
    bits = [] if gates == "-" else [f"default {gates}"]  # leads; see `_describe_resource`
    # The level defaults belong here too (Codex review): they are shown
    # in the wide table and they set the floor for every inheriting
    # child, so dropping them made two Communities with entirely
    # different defaults read identically on a narrow terminal.
    bits.append(
        f"read {_level_cell(community.default_min_read_level)}"
        f"/write {_level_cell(community.default_min_write_level)}"
    )
    bits.append("hidden" if community.hidden else "listed")
    return ", ".join(bits)


async def _community_detail_screen(
    session: Session, lane: DatabaseLane, actor: User, community: Community, *,
    link_context: LinkContext | None = None,
) -> None:
    """A Community's screen is its editor (issue #1081): see `_community_screen`."""
    await _community_screen(session, lane, actor, existing=community, link_context=link_context)


_MEMBER_KIND_LABELS = (("boards", "message board"), ("channels", "chat channel"), ("file_areas", "file area"))


def _community_link_state(link) -> str:
    """One phrase for a Community's header: what its Link side is."""
    if link.own:
        return "Linked, origin here"
    if link.closed:
        return "closed at its origin"
    return "Linked"


def _community_link_rows(link, link_context: LinkContext) -> list[Field | Note]:
    """The NetBBS Link section of a Community's screen (issue #1310)."""
    if link is None:
        return [
            Field("Linked", "no"),
            Note(
                "Linking announces it to other nodes with its name, description and defaults, and lists "
                "its message boards, chat channels and file areas that are already Linked from here. It "
                "links nothing else: link each one on its own screen, and it joins the list.",
                color=MUTED_COLOR,
            ),
        ]
    rows: list[Field | Note] = [
        Field("Linked", "no -- closed at its origin" if link.closed else "yes"),
        Field("Origin", "this node" if link.own else _linked_node_label(link_context, link.origin_fingerprint)),
    ]
    counts = [
        f"{len(link.members[kind])} {label}{'' if len(link.members[kind]) == 1 else 's'}"
        for kind, label in _MEMBER_KIND_LABELS
    ]
    rows.append(Field("Listed members", ", ".join(counts)))
    if link.closed:
        rows.append(Note(
            "Its origin closed it. It is a plain local Community here now; its members stay carried, "
            "and you may keep it or remove it.",
            color=MUTED_COLOR,
        ))
    elif not link.own:
        rows.append(Note(
            "Subscribed: members its origin lists are carried and filed here, within the carry caps. A "
            "member you file under another Community stays where you put it. Its defaults came from its "
            "origin; your edits to them are local. Removing it leaves the Community: nothing more is "
            "accepted, and nothing carried is removed.",
            color=MUTED_COLOR,
        ))
    else:
        rows.append(Note(
            "Linking, unlinking or moving one of its members sends a new list on the next sync pass. "
            "Unlink closes it on every node that carries it; they keep its members.",
            color=MUTED_COLOR,
        ))
    return rows


async def _link_community_screen(
    session: Session, lane: DatabaseLane, actor: User, community: Community, link_context: LinkContext,
) -> None:
    """`[L]ink this Community` (issue #1310): promote it. Nothing to gather
    -- the genesis carries the Community's own fields -- so the screen is the
    one yes/no before a network action."""
    if not await prompt_yes_no(session, f"Link {community.name!r}, announcing it to other nodes?", default=False):
        _announce_line(session, "Cancelled.")
        return
    try:
        events = await lane.run(promote_community, community.id, node_identity=link_context.node_identity, actor=actor)
    except LinkCommunitiesError as exc:
        _announce_line(session, colored(f"Could not link {community.name!r}: {exc}", fg_color=ERROR_COLOR))
        return
    _register_community_events(link_context, events)
    _announce_line(session, f"{community.name!r} is Linked; it is offered to peers on the next sync pass.")


async def _unlink_community_screen(
    session: Session, lane: DatabaseLane, actor: User, community: Community, link_context: LinkContext,
) -> None:
    """`Un[l]ink` (issue #1310, Decision 7): sign the Community's terminal
    closure. Carriers keep every member; this node's copy stays, local."""
    await session.write_line(colored(
        "\r\nUnlinking closes it on every node that carries it, for good; they keep its members. It stays "
        "here as a local Community.",
        fg_color=MUTED_COLOR,
    ))
    if not await prompt_yes_no(session, f"Unlink {community.name!r}?", default=False):
        _announce_line(session, "Cancelled.")
        return
    closure = await lane.run(close_own_community, community.id, node_identity=link_context.node_identity, actor=actor)
    if closure is None:
        _announce_line(session, colored(f"{community.name!r} is not Linked from here.", fg_color=ERROR_COLOR))
        return
    _register_community_events(link_context, [closure])
    _announce_line(session, f"{community.name!r} unlinked; the closure reaches peers on the next sync pass.")


def _register_community_events(link_context: LinkContext, events: list[dict]) -> None:
    """The running node knows this node's own community events, on the
    event loop, as a linked board's genesis is registered after `link_board`."""
    from netbbs.link.events import CommunityClosure, CommunityGenesis, event_content_id

    node = link_context.link_node
    for raw in events:
        content_id = event_content_id(raw["envelope"])
        node.known_event_ids.add(content_id)
        node.events[content_id] = raw
        object_type = raw["envelope"]["object_type"]
        if object_type == "community_genesis":
            node.community_events.record_genesis(CommunityGenesis.from_dict(raw))
        elif object_type == "community_closure":
            closure = CommunityClosure.from_dict(raw)
            node.community_events.closures[closure.payload["community_id"]] = closure


async def _delete_community_screen(
    session: Session, lane: DatabaseLane, actor: User, community: Community, *,
    link_context: LinkContext | None = None,
) -> bool:
    """Shows the blast radius before committing (design doc §16's exact
    confirmation wording): how many boards/channels/areas
    will be left without a Community, and how many Community-blanket
    moderator grants will be revoked outright."""

    def _counts(db: Database) -> tuple[int, int, int, int, int]:
        board_count = sum(1 for b in list_boards(db) if b.community_id == community.id)
        channel_count = sum(1 for c in list_channels(db) if c.community_id == community.id)
        area_count = sum(1 for a in list_file_areas(db) if a.community_id == community.id)
        door_count = len(list_doors(db, community_id=community.id))
        grant_count = len(list_grants_for_community(db, community.id))
        return board_count, channel_count, area_count, door_count, grant_count

    board_count, channel_count, area_count, door_count, grant_count = await lane.run(_counts)
    own_fingerprint = link_context.node_identity.fingerprint if link_context is not None else None
    link = await lane.run(community_link, community.id, own_fingerprint)
    linked_here = link is not None and link.own and not link.closed
    subscribed = link is not None and not link.own and not link.closed
    if linked_here and link_context is None:
        # Its closure has to be signed, which needs the running node.
        _announce_line(session, colored(
            f"{community.name!r} is Linked from here; remove it from the running node's console, which "
            "closes it on every node that carries it.",
            fg_color=ERROR_COLOR,
        ))
        return False
    link_note = ""
    if linked_here:
        link_note = " It is Linked: removing it also closes it on every node that carries it; they keep its members."
    elif subscribed:
        link_note = (
            " It is another node's Link Community: removing it leaves it, so nothing more is accepted from it and "
            "it is not offered again until you restore it under Link status. What is carried stays."
        )
    await session.write_line(
        colored(
            f"\r\nThis Community has {board_count} message board(s), {channel_count} chat channel(s), "
            f"{area_count} file area(s), {door_count} door(s), and {grant_count} moderator grant(s). Removing it leaves "
            "those resources without a Community and revokes those grants. This cannot be undone." + link_note,
            fg_color=MUTED_COLOR,
        )
    )
    await write_prompt(
        session,
        f"Type the Community name {community.name!r} to confirm, or anything else to cancel: ",
    )
    confirmation = (await session.read_line()).strip()
    if confirmation != community.name:
        _confirmation_refused(session, confirmation, community.name)
        return False
    if linked_here:
        closure = await lane.run(
            close_own_community, community.id, node_identity=link_context.node_identity, actor=actor,
        )
        if closure is None:
            # Linked from this database but not by this node's identity (a
            # copied database): no closure can be signed, so nothing is deleted.
            _announce_line(session, colored(
                f"{community.name!r} was not Linked by this node's identity, so it cannot be closed here; "
                "nothing was removed.",
                fg_color=ERROR_COLOR,
            ))
            return False
        _register_community_events(link_context, [closure])
    if subscribed:
        def _leave(db: Database) -> None:
            leave_community(db, community.id, actor=actor, delete=lambda: delete_community(db, community, deleted_by=actor))

        await lane.run(_leave)
    else:
        await lane.run(delete_community, community, deleted_by=actor)
    _announce_line(session, f"{community.name!r} deleted; you left that Community." if subscribed else f"{community.name!r} deleted.")
    return True


@located("Message boards")
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
        elif choice == HELP_KEY:
            await show_menu_help(
                session, "Message boards help", _BOARD_MENU,
                about="Message boards on this node, carried Linked boards included. List opens a board "
                      "on its own screen: its fields change who may read and post, moderation and "
                      "retention, saved with Save and in force at once; its actions review pending posts, "
                      "reorder, Link or remove it. Ctrl-H there explains each field.",
                header_color=header_color, unicode_style=unicode_style,
            )
            await _draw_board_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


_BOARD_MENU = (
    MenuEntry(label=menu_key("C", "reate"), brief="Add a new message board"),
    MenuEntry(label=menu_key("L", "ist"), brief="Browse and edit boards"),
    MenuEntry(label=menu_key("B", "ack"), brief="Return to the Content menu"),
)


async def _draw_board_menu(
    session: Session, description_level: str, redraw_in_place: bool, unicode_style: bool, collapsed: bool,
    header_color: int | tuple[int, int, int] = HEADER_COLOR,
) -> None:
    await session.write_line("\r\n" + screen_title("Message boards",
            breadcrumb=crumbs(session, own=True), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color, node_name_gradient=session.node_name_gradient))
    await session.write_line(
        _menu_row(
            list(_BOARD_MENU),
            description_level,
            width=session.terminal_width,
            height=session.terminal_height,
        )
    )
    await _choice_prompt(session)


def _board_field_specs(
    *, actor: User, redraw_in_place: bool = False, unicode_style: bool = False, collapsed: bool = False,
    levels: LevelContext | None = None,
) -> list[FieldSpec]:
    """One shared field list drives both create and edit (design doc,
    dogfood feature request) -- see `_board_screen`. `redraw_in_place`/
    `unicode_style`/`collapsed`: threaded straight through to the
    Community/Category fields -- see `_community_field`'s own
    docstring."""
    return [
        FieldSpec(
            key="name", label="Name",
            render=lambda d: d.get("name") or "(blank)",
            prompt=text_field("name", required=True),
            brief="The board's display name",
            help="The board's display name, shown wherever it's listed. Must be non-blank.",
            section="Identity",
        ),
        FieldSpec(
            key="description", label="Description",
            render=lambda d: d.get("description") or "(none)",
            prompt=text_field("description"),
            brief="Shown when browsing the board",
            help="A short explanation of what this board is for, shown when browsing/selecting it.",
            section="Identity",
        ),
        FieldSpec(
            key="min_read_level", label="Min read level",
            render=lambda d: _resource_level_label(levels, d, "min_read_level"),
            prompt=_optional_int_field("min_read_level", "Minimum read level"),
            brief="Level required to read it",
            help=(
                "The level a caller needs to see and read this board. 'none' uses its Community's "
                "default read level, or no gate outside one. A Read only or Read and post grant "
                "(Grant moderator) lets its holder past it."
            ),
            section="Access",
        ),
        FieldSpec(
            key="min_write_level", label="Min write level",
            render=lambda d: _resource_level_label(levels, d, "min_write_level"),
            prompt=_optional_int_field("min_write_level", "Minimum write level"),
            brief="Level required to post",
            help=(
                "The level a caller needs to post here; below it the board is read only. 'none' "
                "uses its Community's default write level, or no gate outside one. A Read and post "
                "grant (Grant moderator) lets its holder past it."
            ),
            section="Access",
        ),
        FieldSpec(
            key="min_age", label="Min age",
            render=lambda d: _min_age_label(d.get("min_age"), d.get("age_requirement")),
            prompt=_min_age_field("min_age", "age_requirement"),
            brief="Minimum caller age required",
            help=(
                "The minimum age to see, read or post here, checked against a caller's birthdate "
                "even if they keep it private. Too young: the board is not listed for them. 'none' "
                "uses its Community's default, or no gate outside one."
            ) + _VERIFIED_AGE_HELP,
            section="Access",
        ),
        FieldSpec(
            key="name_requirement",
            label="Name requirement",
            render=lambda d: _name_requirement_label(d.get("name_requirement")),
            prompt=_name_requirement_field(),
            step=_name_requirement_step(),
            help=_NAME_REQUIREMENT_HELP + (
                " 'none' uses its Community's rule. On a board carrying an FTN echo, "
                "'verified_and_displayed' also sends that name to the network as the poster's From, and "
                "refuses a post from anyone, you included, without one that fits its 35 bytes."
            ),
            brief="How posters must be identified",
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
                "The Community it is listed under in Topics. Its fields left at 'none' then take that "
                "Community's defaults; moving it to another Community changes them at once. 'none': "
                "outside every Community, still in the main board list."
            ),
            section="Organization",
        ),
        FieldSpec(
            key="category_id", label="Category",
            render=lambda d: d.get("category_id_label") or "(none)",
            prompt=_category_field(
                actor=actor, create=create_board_category, error_type=CategoryError,
                list_top_level=list_top_level_board_categories, list_subcategories=list_board_subcategories,
                title="Message board category", list_resources=list_boards, get_by_id=get_board_category_by_id,
                redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            ),
            brief="Where it's grouped in listings",
            help=(
                "The folder it is listed in when callers browse boards (Content > Categories). Has no "
                "effect on who can read or post. 'none': listed at the top level."
            ),
            section="Organization",
        ),
        FieldSpec(
            key="pinned", label="Pinned",
            render=lambda d: "yes" if d.get("pinned") else "no",
            prompt=bool_field("pinned"), step=bool_step("pinned"),
            brief="Shown at the top of listings",
            help="Shown at the top of board listings, above unpinned boards, regardless of sort order.",
            section="Organization",
        ),
        FieldSpec(
            key="moderated", label="Moderated",
            render=lambda d: "yes" if d.get("moderated") else "no",
            prompt=bool_field("moderated"), step=bool_step("moderated"),
            brief="New posts need approval first",
            help=(
                "New posts wait under Pending posts until a moderator or SysOp approves them; only the "
                "author sees one before then. Turning it off releases nothing: posts already waiting "
                "still need approving."
            ),
            section="Moderation",
        ),
        FieldSpec(
            key="allow_color",
            label="Color in posts",
            render=lambda d: "allowed" if d.get("allow_color") else "not allowed",
            prompt=choice_field("allow_color", [False, True]),
            step=choice_step("allow_color", [False, True]),
            brief="Show authors' colors in posts",
            help=(
                "Allowed: pipe codes (|00-|23) and color escape codes in a post body show as "
                "color, bold, underline and blink -- never anything that moves the cursor or "
                "clears the screen, and never in subjects. Callers can still turn color off "
                "for themselves. Not allowed: posts show as text, codes as typed. Posts "
                "carried from other nodes follow this node's setting."
            ),
            section="Moderation",
        ),
        FieldSpec(
            key="max_post_age_days",
            label="Max post age (days)",
            render=lambda d: _optional_int_label(d.get("max_post_age_days"), none_word="unlimited"),
            prompt=_optional_int_field("max_post_age_days", "Max post age in days"),
            brief="Expire posts after N days",
            help=(
                "Posts older than this expire, posts already here included, the next time the board is "
                "opened: readers no longer see them, and they are deleted for good once the node's grace "
                "period (Settings, Limits & retention) has passed too. Raising it later does not bring "
                "an expired post back. 'unlimited' keeps every post."
            ),
            section="Moderation",
        ),
    ]


def _board_draft(board: Board | None) -> dict:
    if board is None:
        return {
            "name": "", "description": None, "min_read_level": 0, "min_write_level": 0,
            "community_id": None, "category_id": None, "pinned": False, "moderated": False,
            "max_post_age_days": None, "min_age": None, "name_requirement": None,
            "community_id_label": None, "category_id_label": None, "allow_color": False,
            "age_requirement": None,
        }
    return {
        "name": board.name, "description": board.description,
        "min_read_level": board.min_read_level, "min_write_level": board.min_write_level,
        "community_id": board.community_id, "category_id": board.category_id,
        "pinned": board.pinned, "moderated": board.moderated,
        "max_post_age_days": board.max_post_age_days, "min_age": board.min_age,
        "name_requirement": board.name_requirement, "allow_color": board.allow_color,
        "age_requirement": board.age_requirement,
    }


async def _board_link_rows(
    lane: DatabaseLane, board: Board, link_context: LinkContext, *, linked: bool
) -> tuple[list[Field | Note], bool, bool, bool]:
    """The NetBBS Link section of a board's screen, and `(is_origin,
    has_incoming_offer, is_closed)` (design doc §13/§9.5, issues #53/#88):
    whether this node is the board's origin (gates `[T]ransfer origin`,
    `[C]lose` and `[W]ho posts`), whether a pending origin transfer names
    this node (gates `[A]ccept transfer`), and whether the board is closed
    (closure is terminal, so it suppresses the origin actions)."""
    is_origin = has_incoming_offer = is_closed = False
    rows: list[Field | Note] = [Field("Linked", _yes_no(linked))]
    if linked:
        is_closed = await lane.run(is_board_closed, board)
        if is_closed:
            rows.append(Field("Closed", "yes -- no longer accepts new posts", color=WARNING_COLOR))
            # The reason is part of the signed closure; without this the one
            # place a SysOp looks at a closed board never showed it (#680).
            closure = link_context.link_node.board_closures.get(board.board_id)
            closure_reason = closure.payload.get("reason") if closure is not None else None
            if closure_reason:
                rows.append(Field("Closure reason", sanitize_text(closure_reason)))
        origin_fingerprint = await lane.run(board_origin_fingerprint, board)
        is_origin = origin_fingerprint == link_context.node_identity.fingerprint
        rows.append(Field("Origin", "this node" if is_origin else _linked_node_label(link_context, origin_fingerprint)))
        # Issue #993: who may post, as the origin set it.
        rows.append(Field("Who posts", _POSTING_LABELS[await lane.run(board_posting_mode, board)]))
        if not is_origin:
            peer = link_context.link_node.peers.get(origin_fingerprint)
            if peer is not None and is_board_origin_orphaned(peer):
                rows.append(Note(
                    "ORPHANED -- origin's signing key was revoked, no replacement on file", color=WARNING_COLOR,
                ))
        offer = link_context.link_node.pending_origin_transfers.get(board.board_id)
        if offer is not None:
            if offer.payload.get("new_origin_fingerprint") == link_context.node_identity.fingerprint:
                has_incoming_offer = True
                rows.append(Field(
                    "Pending",
                    "an incoming origin-transfer offer from "
                    + _linked_node_label(link_context, offer.payload.get("old_origin_fingerprint")),
                    color=WARNING_COLOR,
                ))
            elif is_origin:
                rows.append(Field(
                    "Pending",
                    "your own outstanding transfer offer to "
                    + _linked_node_label(link_context, offer.payload.get("new_origin_fingerprint")),
                    color=WARNING_COLOR,
                ))
    if linked and is_origin:
        rows.extend(await _peer_reach_rows(lane, link_context, link_context.link_node.boards.get(board.board_id)))
    return rows, is_origin, has_incoming_offer, is_closed


async def _board_screen(
    session: Session, lane: DatabaseLane, actor: User, *, existing: Board | None = None,
    link_context: LinkContext | None = None, initial_name: str = "",
) -> Board | None:
    """A message board's one screen (issue #1081, design doc §3.5), and the
    screen that creates one. Above the fields: its posts and latest
    activity, its place in the callers' list and whether it is Linked.
    After them, for a Linked board, the NetBBS Link section. The fields
    are chosen by the cursor; the actions -- [U]p, [D]own, [R]emove,
    [P]ending posts, [H]istory, and the Link ones -- are offered while
    nothing waits to be saved. Creating uses the same screen with an empty
    draft and no actions, and returns the board it created; nothing is
    written before [S]ave either way. `initial_name` fills in a new board's
    name: a bad echo's [C]reate starts from its tag (issue #1466)."""
    with entered(session, sanitize_text(existing.name) if existing is not None else "Create message board"):
        current: dict[str, Board | None] = {"board": existing}
        own_fingerprint = link_context.node_identity.fingerprint if link_context is not None else None

        async def save(draft: dict) -> Board:
            if not draft["name"]:
                raise BoardError("name cannot be blank")
            board = current["board"]
            if board is None:
                created = await lane.run(
                    create_board,
                    draft["name"], description=draft["description"], min_read_level=draft["min_read_level"],
                    min_write_level=draft["min_write_level"], category_id=draft["category_id"],
                    pinned=draft["pinned"], moderated=draft["moderated"],
                    max_post_age_days=draft["max_post_age_days"], min_age=draft["min_age"],
                    name_requirement=draft["name_requirement"], community_id=draft["community_id"],
                    allow_color=draft["allow_color"], age_requirement=draft["age_requirement"], creator=actor,
                )
                _announce_line(session, f"Created message board {created.name!r}.")
                return created
            updated = await lane.run(
                update_board,
                board, name=draft["name"], description=draft["description"],
                min_read_level=draft["min_read_level"], min_write_level=draft["min_write_level"],
                category_id=draft["category_id"], pinned=draft["pinned"], moderated=draft["moderated"],
                max_post_age_days=draft["max_post_age_days"], min_age=draft["min_age"],
                name_requirement=draft["name_requirement"], community_id=draft["community_id"],
                allow_color=draft["allow_color"], age_requirement=draft["age_requirement"], changed_by=actor,
            )
            _announce_line(session, f"Updated {updated.name!r}.")
            return updated

        def _act(run: Callable[[Board], Awaitable[bool | None]]):
            async def action(session: Session, lane: DatabaseLane) -> bool:
                await session.write_line("")
                return bool(await run(current["board"]))
            return action

        async def _move(board: Board, offset: int) -> bool:
            # The first cannot go up nor the last down; the bar never offers them.
            await lane.run(move_board, board, offset, moved_by=actor)
            return False

        async def _remove(board: Board) -> bool:
            return await _delete_board_screen(session, lane, actor, board, own_fingerprint=own_fingerprint)

        async def _pending(board: Board) -> bool:
            await _pending_posts_screen(session, lane, actor, board, link_context=link_context)
            return False

        async def _history(board: Board) -> bool:
            await _audit_log_screen(
                session, lane, actor, object_type="board", object_id=board.id,
                moderators=await lane.run(_moderator_lines, "board", board.id),
                # A carried board's name is its origin's: sanitized before it
                # reaches a title (Codex review on #797).
                title=f"History of {sanitize_text(board.name)}",
            )
            return False

        async def _link(board: Board) -> bool:
            await _link_board_screen(session, lane, actor, board, link_context)
            return False

        async def _transfer(board: Board) -> bool:
            await _transfer_board_origin_screen(session, lane, board, link_context)
            return False

        async def _close(board: Board) -> bool:
            await _close_board_screen(session, lane, board, link_context)
            return False

        async def _accept(board: Board) -> bool:
            await _accept_board_origin_transfer_screen(session, lane, board, link_context)
            return False

        async def _who_posts(board: Board) -> bool:
            # A toggle steps (design doc §3.5): anyone, then this node starts
            # threads, then only this node posts. Only the latest setting is
            # pushed, at the next sync pass.
            current_mode = await lane.run(board_posting_mode, board)
            following = BOARD_POSTING_MODES[(BOARD_POSTING_MODES.index(current_mode) + 1) % len(BOARD_POSTING_MODES)]
            try:
                posting = await lane.run(set_board_posting, board, following, node_identity=link_context.node_identity)
            except LinkBoardsError as exc:
                _announce_line(session, colored(f"Could not change who posts: {exc}", fg_color=MUTED_COLOR))
            else:
                link_context.link_node.known_event_ids.add(posting.content_id)
                link_context.link_node.events[posting.content_id] = posting.to_dict()
                _announce_line(
                    session, f"Who posts on {board.name!r}: {_POSTING_LABELS[following]}. Sent on the next sync pass.",
                )
            return False

        async def refresh() -> DetailState:
            board = current["board"]
            if board is None:
                return DetailState(draft={**_board_draft(None), "name": initial_name})

            def _load(db: Database):
                fresh = next((b for b in list_boards(db) if b.id == board.id), board)
                count, last_post_at = count_visible_posts(db, fresh)
                order = [b.id for b in board_siblings(db, fresh)]
                community = get_community(db, fresh.community_id) if fresh.community_id is not None else None
                category = get_board_category_by_id(db, fresh.category_id) if fresh.category_id is not None else None
                labels = {
                    "community_id_label": community.name if community is not None else None,
                    "category_id_label": category.name if category is not None else None,
                }
                return fresh, count, last_post_at, order, labels

            board, post_count, last_post_at, order, labels = await lane.run(_load)
            current["board"] = board
            place, total = (order.index(board.id) if board.id in order else 0), len(order)
            if last_post_at is None:
                activity = "no posts yet"
            else:
                display_format, display_timezone = await lane.run(resolve_display_preferences)
                activity = "last post " + format_for_display(
                    last_post_at, override_format=display_format, override_timezone=display_timezone,
                )
            linked = await lane.run(is_board_linked, board) if link_context is not None else False
            header_bits = [f"{post_count} posts ({activity})", f"place {_place_label(place, total, pinned=board.pinned)}"]
            after = ""
            is_origin = has_incoming_offer = is_closed = False
            if link_context is not None:
                rows, is_origin, has_incoming_offer, is_closed = await _board_link_rows(
                    lane, board, link_context, linked=linked,
                )
                header_bits.append(("Linked, origin here" if is_origin else "Linked") if linked else "not Linked")
                after = _sections_text(
                    [Section("NetBBS Link", rows)], width=session.terminal_width,
                    unicode_style=await lane.run(unicode_style_enabled, actor),
                )
            # Design doc §6.8: a board is local, Linked or FTN, never two, so a
            # Linked board shows no FTN section and offers no [E]cho.
            ftn_networks = [] if linked else await lane.run(list_networks)
            carries_echo = bool(ftn_networks) and await lane.run(board_area, board) is not None
            if ftn_networks:
                ftn_rows = await lane.run(board_echo_rows, board)
                after = (after + "\r\n" if after else "") + _sections_text(
                    [Section("FTN", ftn_rows)], width=session.terminal_width,
                    unicode_style=await lane.run(unicode_style_enabled, actor),
                )
            actions = []
            if place > 0:
                actions.append(DetailAction("u", menu_key("U", "p"), _act(lambda b: _move(b, -1)), brief="Earlier in the callers' list"))
            if place < total - 1:
                actions.append(DetailAction("d", menu_key("D", "own"), _act(lambda b: _move(b, 1)), brief="Later in the callers' list"))
            actions += [
                DetailAction("r", menu_key("R", "emove"), _act(_remove), brief="Permanently remove this board"),
                DetailAction("p", menu_key("P", "ending posts"), _act(_pending), brief="Review posts awaiting approval"),
                DetailAction("h", menu_key("H", "istory"), _act(_history), brief="Its moderators and what they did"),
            ]
            if link_context is not None and not linked:
                actions.append(DetailAction("l", menu_key("L", "ink this message board"), _act(_link), brief="Share it via NetBBS Link"))
            if (
                link_context is not None and linked and is_origin and not is_closed
                and board.board_id not in link_context.link_node.pending_origin_transfers
            ):
                actions.append(DetailAction("t", menu_key("T", "ransfer origin"), _act(_transfer), brief="Hand off origin to a peer"))
                actions.append(DetailAction("c", menu_key("C", "lose message board"), _act(_close), brief="Stop accepting new posts"))
            if link_context is not None and linked and is_origin and not is_closed:
                actions.append(DetailAction("w", menu_key("W", "ho posts"), _act(_who_posts), brief="Who may post, network-wide"))
            if has_incoming_offer:
                actions.append(DetailAction("a", menu_key("A", "ccept transfer"), _act(_accept), brief="Accept incoming origin transfer"))
            if ftn_networks:
                actions.append(DetailAction(
                    "e", menu_key("E", "cho (FTN)"), _act(lambda b: board_echo_action(session, lane, actor, b)),
                    brief="Carry an FTN echo area here"))
            if carries_echo:
                # Issue #1474: the board's own Origin line, else the network's.
                actions.append(DetailAction(
                    "o", menu_key("O", "rigin (FTN)"), _act(lambda b: board_origin_action(session, lane, actor, b)),
                    brief="This board's own Origin line"))
            return DetailState(
                draft={**_board_draft(board), **labels},
                title=sanitize_text(board.name),
                header=colored(" · ".join(header_bits), fg_color=MUTED_COLOR),
                after_fields=after, actions=actions,
            )

        redraw_in_place, redraw_hint = await lane.run(_resolve_redraw_preference, actor)
        unicode_style = await lane.run(unicode_style_enabled, actor)
        collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
        return await edit_resource_draft(
            session, lane,
            title=sanitize_text(existing.name) if existing is not None else "Create message board", own=True,
            fields=_board_field_specs(
                actor=actor, redraw_in_place=redraw_in_place,
                unicode_style=unicode_style, collapsed=collapsed,
                levels=await lane.run(level_context),
            ),
            draft={}, save=save, error_type=BoardError,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
            description_level=await lane.run(menu_description_level, actor),
            redraw_in_place=redraw_in_place, redraw_hint=redraw_hint,
            unicode_style=unicode_style,
            collapsed=collapsed,
            accent_color=await lane.run(effective_accent_color_256),
            header_color=await lane.run(effective_header_color_256),
            detail=DetailMode(refresh=refresh, stay_after_save=existing is not None),
        )


async def _list_boards_screen(
    session: Session, lane: DatabaseLane, actor: User, *, link_context: LinkContext | None = None
) -> None:
    def _load_boards(db: Database):
        # The SysOp's order (issue #839), which callers' lists follow within
        # each category and Community; [U]p/[D]own on a detail screen move
        # one among those, and its Place row says where it sits.
        boards = list_boards(db, order_by="sysop")
        counts = {board.id: (count_listed_posts(db, board)[0], count_pending_posts(db, board)) for board in boards}
        return boards, _effective_by_id(db, boards), counts, carried_to_review(db, "boards")

    # A board's [B]ack comes back here, on the board it left, as the detail
    # screen's action bar says (issue #681); the list is reloaded, since the
    # detail screen may have changed or deleted what it showed.
    reopen_at: int | None = None
    while True:
        boards, effective, counts, to_review = await lane.run(_load_boards)
        linked_boards = await lane.run(linked_board_ids)
        if not boards and reopen_at is not None:
            # The detail screen deleted the last board: its outcome is the
            # message, not an empty list's.
            return
        selected = await pick_item(
            session, boards,
            name_of=lambda b: b.name,
            stable_id_of=lambda b: b.id,
            description_of=lambda b: _board_description(b, effective[b.id], b.board_id in to_review),
            columns=_BOARD_COLUMNS,
            column_values_of=lambda b: _board_columns(b, effective[b.id], counts[b.id], b.board_id in to_review),
            title="Message boards", own=True,
            help_about=(
                "Every board in callers' order: the read and write level in force, inherited ones "
                'included; moderated, pinned, or carried and not yet looked at (to review); posts, +N '
                'waiting for approval. Gates: 18+ an age, name a verified name, name+ one shown. Pick one '
                'to open its screen.'
            ),
            empty_message="No message boards yet.",
            start_stable_id=reopen_at,
            linked_of=lambda b, linked=linked_boards: b.id in linked,  # issue #1104
            redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
            unicode_style=await lane.run(unicode_style_enabled, actor),
            collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
            accent_color=await lane.run(effective_accent_color_256),
            header_color=await lane.run(effective_header_color_256),
        )
        if selected is None:
            return
        reopen_at = selected.id
        await _board_detail_screen(session, lane, actor, selected, link_context=link_context)


def _board_description(board: Board, effective: _Effective, to_review: bool = False) -> str:
    """The narrow-terminal form, below the width a table needs.

    Carries the gates too (Codex review). Without them this fallback
    recreated the exact reported bug on every supported narrow
    terminal -- gated and open resources indistinguishable -- while
    design doc §3.6 promises gates appear wherever a resource is
    listed. A promise the docs make is not optional on small screens."""
    description = _describe_resource(
        effective.read, effective.write, _listing_status(board.moderated, board.pinned), effective
    )
    # "to review" leads, so a narrow row keeps it (Codex review on #800).
    return f"to review; {description}" if to_review else description


_BOARD_COLUMNS = [*_LEVEL_COLUMNS, _BOARD_STATUS_COLUMN, ListColumn("posts", 9, VALUE_COLOR), _GATES_COLUMN]


_COMMUNITY_COLUMNS = [*_LEVEL_COLUMNS, ListColumn("listed", 6, VALUE_COLOR), _GATES_COLUMN]


def _board_columns(
    board: Board, effective: _Effective, counts: tuple[int, int] = (0, 0), to_review: bool = False,
) -> list[str | tuple[str, SegmentColor]]:
    return [
        str(effective.read),
        str(effective.write),
        _listing_status(board.moderated, board.pinned, to_review),
        _count_cell(*counts),
        _gate_cell(effective.min_age, effective.name_requirement, effective.age_requirement),
    ]


async def _board_detail_screen(
    session: Session, lane: DatabaseLane, actor: User, board: Board, *, link_context: LinkContext | None = None
) -> None:
    """A board's screen is its editor (issue #1081): see `_board_screen`."""
    # Looked at: a carried board is no longer news (issue #681).
    await lane.run(mark_carried_reviewed, "boards", board.board_id)
    await _board_screen(session, lane, actor, existing=board, link_context=link_context)


def _forked_from_field(
    board: Board, *, redraw_in_place: bool = False, unicode_style: bool = False, collapsed: bool = False,
) -> Callable[[Session, DatabaseLane, dict], Awaitable[None]]:
    """`[L]ink this board`'s own optional fork-source picker, as a
    draft-editor field -- also stashes the chosen board's own name into
    `draft["forked_from_label"]`, same reasoning as `_community_field`'s
    own `_label` companion (`render` must stay a pure, synchronous dict
    read; the name is resolved here, at prompt time, where a `lane`
    round-trip is already in flight)."""

    async def prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        candidates = await lane.run(_linked_boards_excluding, board.id)
        chosen = await pick_item(
            session, candidates,
            name_of=lambda b: b.name, stable_id_of=lambda b: b.id,
            title="Fork of which message board?", empty_message="No other Linked message boards to fork from.",
            redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            accent_color=await lane.run(effective_accent_color_256),
            header_color=await lane.run(effective_header_color_256),
        )
        if chosen is not None:
            draft["forked_from"] = chosen.board_id
            draft["forked_from_label"] = chosen.name

    return prompt


def _link_board_field_specs(
    board: Board, *, redraw_in_place: bool = False, unicode_style: bool = False, collapsed: bool = False,
) -> list[FieldSpec]:
    """Dogfood report: this used to be a fixed linear chain of six
    prompts plus a yes/no fork question, no way back -- a mistyped max
    post age discarded every recommendation already chosen. Now an
    `edit_resource_draft` field list, same shape as `_board_field_
    specs`'s own already-converted fields, just recommendations instead
    of enforced settings."""
    return [
        FieldSpec(
            key="default_min_read_level",
            label="Recommended read level",
            render=lambda d: _optional_int_label(d.get("default_min_read_level")),
            prompt=_optional_int_field("default_min_read_level", "Recommended minimum read level"),
            brief="Sent to peers as a default",
            help=(
                "The read level a node that starts carrying this board gives its copy; its SysOp can "
                "change it there. Signed into the board's Link record on Save, so it cannot be changed "
                "afterwards. 'none': no recommendation, so their copy starts open to every level."
            ),
        ),
        FieldSpec(
            key="default_min_write_level",
            label="Recommended write level",
            render=lambda d: _optional_int_label(d.get("default_min_write_level")),
            prompt=_optional_int_field("default_min_write_level", "Recommended minimum write level"),
            brief="Sent to peers as a default",
            help=(
                "The write level a carrying node's copy starts with; fixed on Save, like every field "
                "here. 'none': no recommendation, open to every level."
            ),
        ),
        FieldSpec(
            key="default_moderated",
            label="Recommended moderated",
            render=lambda d: _link_recommendation_label(d.get("default_moderated")),
            prompt=choice_field("default_moderated", _LINK_RECOMMENDATION_VALUES),
            step=choice_step("default_moderated", _LINK_RECOMMENDATION_VALUES),
            brief="Sent to peers as a default",
            help="Whether a carrying node's copy starts moderated. Fixed on Save. No recommendation: unmoderated.",
        ),
        FieldSpec(
            key="default_max_post_age_days",
            label="Recommended max post age",
            render=lambda d: _optional_int_label(d.get("default_max_post_age_days"), none_word="no recommendation"),
            prompt=_optional_int_field("default_max_post_age_days", "Recommended max post age in days"),
            brief="Sent to peers as a default",
            help=(
                "The max post age a carrying node's copy starts with. Fixed on Save. No recommendation: "
                "their copy keeps every post."
            ),
        ),
        FieldSpec(
            key="default_min_age",
            label="Recommended min age",
            render=lambda d: _optional_int_label(d.get("default_min_age")),
            prompt=_min_age_field("default_min_age"),
            brief="Sent to peers as a default",
            help="The minimum age a carrying node's copy starts with. Fixed on Save. 'none': no age gate.",
        ),
        FieldSpec(
            key="default_name_requirement",
            label="Recommended name requirement",
            render=lambda d: _name_requirement_label(d.get("default_name_requirement")),
            prompt=_name_requirement_field("default_name_requirement"),
            step=_name_requirement_step("default_name_requirement"),
            brief="Sent to peers as a default",
            help="The name requirement a carrying node's copy starts with. Fixed on Save. " + _NAME_REQUIREMENT_HELP,
        ),
        FieldSpec(
            key="forked_from", label="Fork of",
            render=lambda d: d.get("forked_from_label") or "(not a fork)",
            prompt=_forked_from_field(
                board, redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            ),
            brief="Most boards: leave it",
            help=(
                "Only for a board that carries on another node's Linked message board under this "
                "node, for example after that board was closed. Peers are told it continues that "
                "one; each still decides what it carries. A new board of your own is not a fork: "
                "leave it as '(not a fork)'."
            ),
        ),
    ]


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
    own prefill-then-edit convention) -- except `default_moderated`/
    `default_max_post_age_days`, which have no such prior art to reuse
    (`_edit_board_screen` reads them as plain required fields, never
    optional) and start at "no recommendation" instead.

    Building/signing/persisting the genesis is a plain, synchronous
    `db`-first call (`link_board`), dispatched through `lane` like
    every other board-admin mutation here. Registering the result with
    the *live* `LinkNode` is deliberately done here, directly, on the
    event loop -- never inside the lane-dispatched call itself (see
    `link_board`'s own docstring for why that split matters: `LinkNode`
    mutation and `DatabaseLane` dispatch must never share a thread).

    Dogfood report: the six fields plus the fork question below used to
    be a fixed linear chain with no way back -- see `_link_board_field_
    specs`'s own docstring for the bug this fixes. Unlike `[S]hutdown`/
    `[D]rain`'s own draft-editor conversion, there is no extra confirm
    step before `[S]ave` here -- the old linear chain never had one
    either, and this matches every other `edit_resource_draft` caller's
    own shape (the field summary itself is the confirmation).
    """
    draft: dict = {
        "default_min_read_level": board.min_read_level,
        "default_min_write_level": board.min_write_level,
        "default_moderated": None,
        "default_max_post_age_days": None,
        "default_min_age": board.min_age,
        "default_name_requirement": board.name_requirement,
        "forked_from": None,
        "forked_from_label": None,
    }

    async def save(draft: dict) -> bool:
        genesis = await lane.run(
            link_board,
            board,
            node_identity=link_context.node_identity,
            default_min_read_level=draft["default_min_read_level"],
            default_min_write_level=draft["default_min_write_level"],
            default_moderated=draft["default_moderated"],
            default_max_post_age_days=draft["default_max_post_age_days"],
            default_min_age=draft["default_min_age"],
            default_name_requirement=draft["default_name_requirement"],
            default_age_requirement=board.age_requirement,
            forked_from=draft["forked_from"],
        )
        link_context.link_node.boards[board.board_id] = genesis
        link_context.link_node.known_event_ids.add(genesis.content_id)
        link_context.link_node.events[genesis.content_id] = genesis.to_dict()
        _announce_line(session, _linked_announcement(board.name))
        return True

    redraw_in_place, redraw_hint = await lane.run(_resolve_redraw_preference, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    await edit_resource_draft(
        session, lane,
        title="Link this message board",
        fields=_link_board_field_specs(
            board, redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
        ),
        draft=draft, save=save, error_type=LinkBoardsError,
        save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=redraw_in_place, redraw_hint=redraw_hint,
        unicode_style=unicode_style, collapsed=collapsed,
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )


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

    Peers are selected by their authenticated friendly/DNS presentation;
    the fingerprint remains the value placed in the signed offer.
    """
    await session.write_line(
        colored("\r\nTransfer message board origin", fg_color=await lane.run(effective_header_color_256), bold=True)
    )
    if not link_context.link_node.peers:
        _announce_line(session, colored("No known peers to transfer this message board to.", fg_color=MUTED_COLOR))
        return
    picked = await _pick_new_origin(session, lane, link_context, title="New message-board origin")
    if picked is None:
        return
    target, target_label = picked
    await _warn_about_changed_node_identity(
        session, lane, target, role="The proposed new origin's",
    )
    if not await prompt_yes_no(session, f"Offer to hand {board.name!r} off to {target_label}?", default=False):
        _announce_line(session, "Cancelled.")
        return

    try:
        offer = await lane.run(
            offer_board_origin_transfer,
            board,
            node_identity=link_context.node_identity,
            new_origin_fingerprint=target,
        )
    except LinkBoardsError as exc:
        _announce_line(session, colored(f"Could not offer transfer: {exc}", fg_color=MUTED_COLOR))
        return

    link_context.link_node.pending_origin_transfers[board.board_id] = offer
    link_context.link_node.board_lifecycle_head[board.board_id] = offer.content_id
    link_context.link_node.known_event_ids.add(offer.content_id)
    link_context.link_node.events[offer.content_id] = offer.to_dict()

    _announce_line(session, "Offer sent -- it will be pushed to peers on the next sync pass.")


# Issue #993: the posting setting in the board screen's words.
_POSTING_LABELS = {
    "anyone": "anyone",
    "origin_threads": "the origin's callers start threads; anyone replies",
    "origin_only": "the origin's callers only",
}


async def _close_board_screen(session: Session, lane: DatabaseLane, board: Board, link_context: LinkContext) -> None:
    """
    `[C]lose board` (design doc §9.5, issue #88): the current origin's
    terminal board_closure, stopping new posts network-wide. Only
    reachable when `_board_screen` already confirmed this node is
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
    await write_prompt(session, "Optional reason (blank for none): ")
    reason = (await session.read_line()).strip() or None
    if not await prompt_yes_no(session, f"Close {board.name!r}?", default=False):
        _announce_line(session, "Cancelled.")
        return

    try:
        closure = await lane.run(
            close_board_if_linked, board, node_identity=link_context.node_identity, reason=reason,
        )
    except LinkBoardsError as exc:
        _announce_line(session, colored(f"Could not close message board: {exc}", fg_color=MUTED_COLOR))
        return

    link_context.link_node.board_closures[board.board_id] = closure
    link_context.link_node.board_lifecycle_head[board.board_id] = closure.content_id
    link_context.link_node.known_event_ids.add(closure.content_id)
    link_context.link_node.events[closure.content_id] = closure.to_dict()

    _announce_line(session, f"{board.name!r} closed -- it will be pushed to peers on the next sync pass.")


async def _accept_board_origin_transfer_screen(
    session: Session, lane: DatabaseLane, board: Board, link_context: LinkContext
) -> None:
    """
    `[A]ccept transfer` (design doc §13, issue #53): the
    consent-completing half -- accepts the single pending incoming
    origin-transfer offer for `board` that names this node as the
    proposed new origin. Only reachable when `_board_screen`
    already confirmed such an offer exists (`has_incoming_offer`), but
    re-checked here too rather than trusted blindly, the same
    defense-in-depth every other admin mutation in this file already
    applies to a caller-supplied precondition.
    """
    offer = link_context.link_node.pending_origin_transfers.get(board.board_id)
    if offer is None or offer.payload.get("new_origin_fingerprint") != link_context.node_identity.fingerprint:
        _announce_line(session, colored("\r\nNo pending incoming offer for this message board.", fg_color=MUTED_COLOR))
        return

    old_origin = offer.payload.get("old_origin_fingerprint")
    await session.write_line(
        colored("\r\nAccept message board origin", fg_color=await lane.run(effective_header_color_256), bold=True)
    )
    old_peer = link_context.link_node.peers.get(old_origin)
    old_label = sanitize_text(
        identity_for_peer(old_peer).label
        if old_peer is not None
        else old_origin if isinstance(old_origin, str) else "an unknown linked node"
    )
    if isinstance(old_origin, str):
        await _warn_about_changed_node_identity(
            session, lane, old_origin, role="The offering node's",
        )
    if not await prompt_yes_no(session, f"Accept origin of {board.name!r} from {old_label}?", default=False):
        _announce_line(session, "Cancelled.")
        return

    try:
        accepted = await lane.run(
            accept_board_origin_transfer,
            board,
            node_identity=link_context.node_identity,
            offer=offer,
        )
    except LinkBoardsError as exc:
        _announce_line(session, colored(f"Could not accept transfer: {exc}", fg_color=MUTED_COLOR))
        return

    link_context.link_node.board_origin[board.board_id] = link_context.node_identity.fingerprint
    link_context.link_node.board_lifecycle_head[board.board_id] = accepted.content_id
    del link_context.link_node.pending_origin_transfers[board.board_id]
    link_context.link_node.known_event_ids.add(accepted.content_id)
    link_context.link_node.events[accepted.content_id] = accepted.to_dict()

    _announce_line(session,
        f"Accepted -- this node is now {board.name!r}'s origin. Pushed to peers on the next sync pass."
    )


async def _delete_board_screen(
    session: Session, lane: DatabaseLane, actor: User, board: Board, *,
    own_fingerprint: str | None = None,
) -> bool:
    # Issue #683: a carried message board whose origin is another node is hidden,
    # not destroyed -- see `_hide_carried_screen`.
    try:
        carried = await lane.run(carried_from_elsewhere, "boards", board.board_id, own_fingerprint)
    except CarryDecisionError as exc:
        _announce_line(session, colored(f"Cannot delete {board.name!r}: {exc}", fg_color=ERROR_COLOR))
        return False
    if carried:
        return await _hide_carried_screen(session, lane, actor, "boards", board.board_id, board.name, own_fingerprint=own_fingerprint)
    await session.write_line(
        colored(
            "\r\nThis permanently deletes the message board, all of its posts, and any "
            "moderator grants scoped to it. This cannot be undone.",
            fg_color=MUTED_COLOR,
        )
    )
    await write_prompt(
        session,
        f"Type the message board name {board.name!r} to confirm, or anything else to cancel: ",
    )
    confirmation = (await session.read_line()).strip()
    if confirmation != board.name:
        _confirmation_refused(session, confirmation, board.name)
        return False
    # Issues #669/#683: a Linked board keeps its genesis and is recorded as
    # excluded, so it stays declared as not carried and does not come back.
    return await _remove_resource(
        session, lane, actor, "boards", board.board_id, board.name, own_fingerprint=own_fingerprint,
        delete=lambda db: delete_board(db, board, deleted_by=actor),
    )


@located("Categories")
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
        elif choice == HELP_KEY:
            await session.write_line("")
            await _categories_help_screen(session, header_color=header_color, unicode_style=unicode_style)
            await _draw_category_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "m":
            await session.write_line("")
            await _generic_category_screen(
                session, lane, actor,
                create=create_board_category, list_top_level=list_top_level_board_categories,
                list_subcategories=list_board_subcategories, delete=delete_board_category,
                update=update_board_category, move=move_board_category,
                error_type=CategoryError, title="Message board categories",
            )
            await _draw_category_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "f":
            await session.write_line("")
            await _generic_category_screen(
                session, lane, actor,
                create=create_file_category, list_top_level=list_top_level_file_categories,
                list_subcategories=list_file_subcategories, delete=delete_file_category,
                update=update_file_category, move=move_file_category,
                error_type=FileCategoryError, title="File-area categories",
            )
            await _draw_category_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        elif choice == "c":
            await session.write_line("")
            await _generic_category_screen(
                session, lane, actor,
                create=create_channel_category, list_top_level=list_top_level_channel_categories,
                list_subcategories=list_channel_subcategories, delete=delete_channel_category,
                update=update_channel_category, move=move_channel_category,
                error_type=ChannelCategoryError, title="Chat channel categories",
            )
            await _draw_category_menu(session, description_level, redraw_in_place, unicode_style, collapsed, header_color)
        else:
            await session.write(reject_unhandled_key(choice))


async def _categories_help_screen(
    session: Session, *, header_color: int | tuple[int, int, int], unicode_style: bool
) -> None:
    """Ctrl-H on the Categories screen (issue #838): the difference
    between a category and a Community, which nothing else on screen
    explained -- a SysOp setting up a node met both words on one menu."""
    lines = [
        colored("Categories", fg_color=header_color, bold=True),
        "  Group the list of one kind: board categories group boards, file-area",
        "  categories group file areas, chat categories group channels. A caller",
        "  sees them as folders in that list, at most two levels deep.",
        "",
        colored("Communities", fg_color=header_color, bold=True),
        "  A topic that holds every kind at once: its own boards, chat channels,",
        "  file areas and games. Callers reach them under [T]opics on the main",
        "  menu. They are managed under Content, not here.",
        "",
        colored("Using both", fg_color=header_color, bold=True),
        "  A board can have a Community and a category. Inside a Community,",
        "  callers see only the categories its own boards use.",
    ]
    await show_help(session, "Categories help", lines, header_color=header_color, unicode_style=unicode_style)


async def _draw_category_menu(
    session: Session, description_level: str, redraw_in_place: bool, unicode_style: bool, collapsed: bool,
    header_color: int | tuple[int, int, int] = HEADER_COLOR,
) -> None:
    await session.write_line("\r\n" + screen_title("Categories",
            breadcrumb=crumbs(session, own=True), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color, node_name_gradient=session.node_name_gradient))
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
    await session.write_line(colored(f"{help_key_label(session)}: categories vs. Communities", fg_color=MUTED_COLOR))
    await _choice_prompt(session)


async def _generic_category_screen(
    session: Session, lane: DatabaseLane, actor: User, *, create, list_top_level, list_subcategories, delete,
    error_type, title: str, update=None, move=None,
) -> None:
    with entered(session, title):
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
                    update=update, move=move, create=create, error_type=error_type, title=title,
                )
                await _draw_generic_category_menu(session, title, description_level, redraw_in_place, unicode_style, collapsed, header_color)
            elif choice == HELP_KEY:
                await show_menu_help(
                    session, f"{title} help", _GENERIC_CATEGORY_MENU,
                    about="Folders that group this kind in the callers' lists, at most two levels deep; "
                          "put an item in one with its own Category field. List opens a category to "
                          "rename it, change its parent, move it up or down among its siblings, or "
                          "remove it. Changes show in the callers' lists at once.",
                    header_color=header_color, unicode_style=unicode_style,
                )
                await _draw_generic_category_menu(session, title, description_level, redraw_in_place, unicode_style, collapsed, header_color)
            else:
                await session.write(reject_unhandled_key(choice))


_GENERIC_CATEGORY_MENU = (
    MenuEntry(label=menu_key("C", "reate"), brief="Add a new category"),
    MenuEntry(label=menu_key("L", "ist"), brief="Edit, order and remove categories"),
    MenuEntry(label=menu_key("B", "ack"), brief="Return to Categories"),
)


async def _draw_generic_category_menu(
    session: Session, title: str, description_level: str, redraw_in_place: bool,
    unicode_style: bool,
    collapsed: bool,
    header_color: int | tuple[int, int, int] = HEADER_COLOR,
) -> None:
    await session.write_line("\r\n" + screen_title(title,
            breadcrumb=crumbs(session, own=True), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color, node_name_gradient=session.node_name_gradient))
    await session.write_line(
        _menu_row(
            list(_GENERIC_CATEGORY_MENU),
            description_level,
            width=session.terminal_width,
            height=session.terminal_height,
        )
    )
    await _choice_prompt(session)


async def _create_category_screen(
    session: Session, lane: DatabaseLane, actor: User, *, create, list_top_level, error_type,
    existing=None, update=None, detail: DetailMode | None = None, current: dict | None = None,
):
    # Returns the category it created, or `None` if the SysOp backed
    # out -- `edit_resource_draft` already returns whatever `save`
    # returned, this just stopped discarding it (issue #530, so
    # `_pick_optional_category`'s `[C]reate` can hand it straight back
    # as the selection).
    """
    Issue #282: the last create-wizard left in this module (boards,
    areas, channels, doors, and Communities were all already
    `edit_resource_draft` screens) -- name, then description, then
    "Make this a sub-category?", with a blank name aborting outright.
    Now the same draft-editor shape as its siblings: `[N]ame`,
    `[D]escription`, `[P]arent` (a picker whose first entry is "no
    parent"), `[S]ave`; nothing is created before Save, and a domain
    rejection (duplicate name, ...) returns to the draft intact.
    """
    with entered(session, sanitize_text(existing.name) if existing is not None else "Create category"):
        redraw_in_place, redraw_hint = await lane.run(_resolve_redraw_preference, actor)
        unicode_style = await lane.run(unicode_style_enabled, actor)
        collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
        accent_color = await lane.run(effective_accent_color_256)
        header_color = await lane.run(effective_header_color_256)

        # With `existing` and `update`, the same draft edits a category
        # (issue #681): its name, description and parent, prefilled.
        draft: dict = {"name": "", "description": None, "parent": None}
        if existing is not None:
            parents = {category.id: category for category in await lane.run(list_top_level)}
            draft = {
                "name": existing.name, "description": existing.description,
                "parent": parents.get(existing.parent_category_id),
            }

        async def _parent_prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
            # (name, category-or-None) pairs: a "no parent" entry leads the
            # list so clearing a previously chosen parent is a pick like any
            # other, and backing out of the picker keeps the draft as-is.
            choices = [("(none -- a top-level category)", None)]
            editing = current["category"] if current is not None else existing
            choices += [
                (category.name, category) for category in await lane.run(list_top_level)
                if editing is None or category.id != editing.id
            ]
            selected = await pick_item(
                session, choices,
                name_of=lambda item: item[0],
                stable_id_of=lambda item: 0 if item[1] is None else item[1].id,
                title="Parent category",
                empty_message="No top-level categories exist yet.",
                redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
                accent_color=accent_color, header_color=header_color,
            )
            if selected is None:
                return
            draft["parent"] = selected[1]

        fields = [
            FieldSpec(
                key="name", label="Name",
                render=lambda d: sanitize_text(d["name"]) if d["name"] else "(blank)",
                prompt=text_field("name", required=True),
                brief="The category's name",
                help="Shown wherever this category is listed. Required.",
            ),
            FieldSpec(
                key="description", label="Description",
                render=lambda d: sanitize_text(d["description"]) if d["description"] else "(none)",
                prompt=text_field("description"),
                brief="Optional one-line description",
                help="Optional. Shown under the category name where there is room for it.",
            ),
            FieldSpec(
                key="parent", label="Parent",
                render=lambda d: sanitize_text(d["parent"].name) if d["parent"] is not None else "(none -- top-level)",
                prompt=_parent_prompt,
                brief="Nest under an existing category",
                help=(
                    "Pick an existing top-level category to make this one its sub-category, or "
                    "the first entry to keep it top-level."
                ),
            ),
        ]

        async def save(draft: dict) -> object:
            if not draft["name"]:
                # Raised, not returned: a bare `return None` would close the
                # editor and discard the description/parent already entered
                # (Codex review on #289); the caught error keeps the draft up.
                raise error_type("name cannot be blank")
            parent = draft["parent"]
            if existing is not None:
                category = await lane.run(
                    update, current["category"] if current is not None else existing,
                    name=draft["name"], description=draft["description"],
                    parent_category_id=parent.id if parent is not None else None, changed_by=actor,
                )
                _announce_line(session, f"Saved category {category.name!r}.")
                return category
            category = await lane.run(
                create, draft["name"], description=draft["description"],
                parent_category_id=parent.id if parent is not None else None, created_by=actor,
            )
            _announce_line(session, f"Created category {category.name!r}.")
            return category

        if detail is None:
            # Creating uses the same screen as a category's own, with an empty
            # draft and no actions, and returns what it created (issue #1081).
            async def _empty() -> DetailState:
                return DetailState(draft=dict(draft))

            detail = DetailMode(refresh=_empty, stay_after_save=False)
        return await edit_resource_draft(
            session, lane,
            title=sanitize_text(existing.name) if existing is not None else "Create category", own=True,
            fields=fields, draft=draft, save=save, error_type=error_type,
            save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
            description_level=await lane.run(menu_description_level, actor),
            redraw_in_place=redraw_in_place, redraw_hint=redraw_hint,
            unicode_style=unicode_style, collapsed=collapsed,
            accent_color=accent_color, header_color=header_color,
            detail=detail,
        )


async def _list_categories_screen(
    session: Session, lane: DatabaseLane, actor: User, *, list_top_level, list_subcategories, delete,
    update=None, move=None, create=None, error_type=Exception, title: str = "Categories",
) -> None:
    """Every category, each sub-category under its parent, in the SysOp's
    order (issue #681). Picking one opens its screen, to edit, move or
    remove it; leaving that comes back here, on it."""
    def _load(db: Database) -> list:
        tree = []
        for top in list_top_level(db):
            tree.append(top)
            tree.extend(list_subcategories(db, top.id))
        return tree

    reopen_at: int | None = None
    while True:
        tree = await lane.run(_load)
        if not tree and reopen_at is not None:
            # The last category was just removed: that outcome is the message.
            return
        by_id = {category.id: category for category in tree}
        selected = await pick_item(
            session, tree,
            name_of=lambda c: c.name if c.is_top_level else f"  - {c.name}",
            stable_id_of=lambda c: c.id,
            description_of=lambda c: (
                f"in {by_id[c.parent_category_id].name}" if not c.is_top_level and c.parent_category_id in by_id
                else sanitize_text(c.description) if c.description else "top-level"
            ),
            title=title, own=True,
            empty_message="No categories yet.",
            help_about=(
                "This kind's categories, each sub-category under its parent. Pick one to rename it, "
                'change its parent, move it among its siblings or remove it.'
            ),
            start_stable_id=reopen_at,
            redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
            unicode_style=await lane.run(unicode_style_enabled, actor),
            collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
            accent_color=await lane.run(effective_accent_color_256),
            header_color=await lane.run(effective_header_color_256),
        )
        if selected is None:
            return
        reopen_at = selected.id
        await _category_screen(
            session, lane, actor, selected, list_top_level=list_top_level,
            list_subcategories=list_subcategories, delete=delete, update=update, move=move,
            create=create, error_type=error_type,
        )


async def _category_screen(
    session: Session, lane: DatabaseLane, actor: User, category, *, list_top_level, list_subcategories,
    delete, update, move, create, error_type,
) -> None:
    """One category's screen, which is its editor (issue #1081, design doc
    §3.5): its place among its siblings and its sub-categories above the
    fields, the fields chosen by the cursor, and [U]p, [D]own and [R]emove
    while nothing is waiting to be saved (issue #681)."""
    current: dict = {"category": category}

    def _load(db: Database):
        # Found by id among every category, not through the parent it had
        # when the screen opened: a save may have moved it under another
        # (Codex review on #799).
        tops = list_top_level(db)
        for top in tops:
            if top.id == current["category"].id:
                return top, tops, None, list_subcategories(db, top.id)
            subs = list_subcategories(db, top.id)
            for sub in subs:
                if sub.id == current["category"].id:
                    return sub, subs, top, []
        return None, [], None, []

    def _move(offset: int):
        async def run(session: Session, lane: DatabaseLane) -> bool:
            try:
                await lane.run(move, current["category"], offset, moved_by=actor)
            except error_type as exc:
                # Deleted meanwhile: said, and the screen reloads.
                _announce(session, f"Error: {exc}", error=True)
            return False
        return run

    async def _remove(session: Session, lane: DatabaseLane) -> bool:
        target = current["category"]
        await session.write_line(
            colored(
                "\r\nDeleting this category sets any message boards/file areas/chat channels "
                "assigned to it (and any of its own sub-categories) back to uncategorized.",
                fg_color=MUTED_COLOR,
            )
        )
        await write_prompt(
            session,
            f"Type the category name {target.name!r} to confirm deletion, or anything else to cancel: ",
        )
        confirmation = (await session.read_line()).strip()
        if confirmation != target.name:
            _confirmation_refused(session, confirmation, target.name)
            return False
        await lane.run(delete, target, deleted_by=actor)
        _announce_line(session, f"{target.name!r} deleted.")
        return True

    async def refresh() -> DetailState:
        found, siblings, parent, children = await lane.run(_load)
        if found is None:
            return DetailState(
                draft={"name": current["category"].name, "description": current["category"].description,
                       "parent": None},
                header=colored("This category no longer exists.", fg_color=WARNING_COLOR),
            )
        current["category"] = found
        place = [sibling.id for sibling in siblings].index(found.id)
        header = f"Place {place + 1} of {len(siblings)} among its siblings"
        if found.parent_category_id is None:
            header += " · Sub-categories: " + (", ".join(sanitize_text(c.name) for c in children) or "none")
        actions = []
        if move is not None and place > 0:
            actions.append(DetailAction("u", menu_key("U", "p"), _move(-1), brief="Earlier among its siblings"))
        if move is not None and place < len(siblings) - 1:
            actions.append(DetailAction("d", menu_key("D", "own"), _move(1), brief="Later among its siblings"))
        actions.append(DetailAction("r", menu_key("R", "emove"), _remove, brief="Permanently remove it"))
        return DetailState(
            draft={"name": found.name, "description": found.description, "parent": parent},
            header=colored(header, fg_color=MUTED_COLOR), actions=actions, title=sanitize_text(found.name),
        )

    await _create_category_screen(
        session, lane, actor, create=create, list_top_level=list_top_level, error_type=error_type,
        existing=category, update=update, detail=DetailMode(refresh=refresh),
        current=current,
    )


async def _pick_moderator_scope(
    session: Session, lane: DatabaseLane, *, redraw_in_place: bool, unicode_style: bool, collapsed: bool
) -> tuple[str, int | None, str] | None:
    """Returns `(object_type, object_id, human label)`, or `None` if
    backed out. `object_id=None` means a blanket grant (design doc);
    narrowing a blanket grant to one Community (design doc §16's
    Community-blanket tier) is `_grant_moderator_screen`'s own
    `[C]ommunity` field now (issue #282), not a question asked here."""
    # Dogfood-reported regression, same shape as picker.py's own nav-
    # trailer bug: this line used to be one hand-built, unclamped
    # concatenation -- 147 columns wide once all six options were
    # spelled out in full, nearly double a standard 80-column terminal
    # -- so a real client wrapped it wherever it happened to land,
    # mid-word. `action_bar` already wraps whole options deterministically
    # at the terminal edge (the same primitive every other hotkey menu
    # in this codebase uses); routing through it here instead of a raw
    # write() fixes this the same way, not a one-off patch.
    #
    # Issue #1158: every key is its label's first letter, so a blanket grant
    # over one kind is `[A]ll of one kind` and then the kind, rather than
    # three more keys that could not start their own labels.
    scope_options = [label for label, _brief in _MODERATOR_SCOPE_CHOICES] + [menu_key("B", "ack")]
    header_color = await lane.run(effective_header_color_256)
    scope_key = await _ask_moderator_scope_key(
        session, "Scope:", scope_options, _MODERATOR_SCOPE_HELP,
        about="What the new grant covers. One board, area or channel is picked next; a blanket "
              "scope (all of one kind, or everything) also covers ones created later, and can be "
              "narrowed to one Community on the grant screen. Any other key cancels.",
        header_color=header_color, unicode_style=unicode_style,
    )
    if scope_key == "a":
        kind_key = await _ask_moderator_scope_key(
            session, "All of which kind:",
            [label for label, _brief in _MODERATOR_BLANKET_CHOICES] + [menu_key("B", "ack")],
            _MODERATOR_BLANKET_HELP,
            about="A blanket grant: every one of this kind, including those created later. "
                  "Any other key cancels.",
            header_color=header_color, unicode_style=unicode_style,
        )
        if kind_key == "b":
            return None
        scope_key = {"m": "all_boards", "f": "all_areas", "c": "all_channels"}.get(kind_key, "")
    accent_color = await lane.run(effective_accent_color_256)
    if scope_key == "b":
        return None  # Back: nothing chosen, nothing said
    if scope_key == "m":
        board = await pick_item(
            session, await lane.run(list_boards, order_by="alphabetical"),
            name_of=lambda b: b.name, stable_id_of=lambda b: b.id,
            title="Which message board?", empty_message="No message boards yet.",
            redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            accent_color=accent_color, header_color=header_color,
        )
        if board is None:
            return None
        return "board", board.id, f"message board {board.name!r}"
    elif scope_key == "f":
        area = await pick_item(
            session, await lane.run(list_file_areas, order_by="alphabetical"),
            name_of=lambda a: a.name, stable_id_of=lambda a: a.id,
            title="Which file area?", empty_message="No file areas yet.",
            redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            accent_color=accent_color, header_color=header_color,
        )
        if area is None:
            return None
        return "file_area", area.id, f"file area {area.name!r}"
    elif scope_key == "c":
        channel = await pick_item(
            session, await lane.run(list_channels),
            name_of=lambda c: c.name, stable_id_of=lambda c: c.id,
            title="Which chat channel?", empty_message="No chat channels yet.",
            redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            accent_color=accent_color, header_color=header_color,
        )
        if channel is None:
            return None
        return "channel", channel.id, f"chat channel {channel.name!r}"
    elif scope_key == "all_boards":
        object_type, label = "board", "all message boards (blanket)"
    elif scope_key == "all_areas":
        object_type, label = "file_area", "all file areas (blanket)"
    elif scope_key == "all_channels":
        object_type, label = "channel", "all chat channels (blanket)"
    elif scope_key == "e":
        # Issue #836 (F131): boards, areas and channels in one grant.
        object_type, label = _EVERYWHERE, "every board, file area and chat channel (blanket)"
    else:
        _announce_line(session, colored("Not a valid scope.", fg_color=MUTED_COLOR))
        return None
    return object_type, None, label


async def _ask_moderator_scope_key(
    session: Session, question: str, options: list[str], help_entries: Sequence[MenuEntry], *,
    about: str, header_color: int | tuple[int, int, int], unicode_style: bool,
) -> str:
    """One key for `_pick_moderator_scope`'s one-key questions, with their
    help on `?`."""
    while True:
        await session.write_line(question)
        await write_prompt(session, f"{action_bar(options, width=session.terminal_width, height=session.terminal_height)}: ")
        key = (await session.read_key()).lower()
        await session.write_line("")
        if key != HELP_KEY:
            return key
        await show_menu_help(
            session, "Moderator scope help", help_entries, about=about,
            header_color=header_color, unicode_style=unicode_style,
        )


# `(label, brief)` for each scope; `b` is Back (issue #1158).
_MODERATOR_SCOPE_CHOICES = (
    (menu_key("M", "essage board"), "One board you pick next"),
    (menu_key("F", "ile area"), "One file area you pick next"),
    (menu_key("C", "hat channel"), "One chat channel you pick next"),
    (menu_key("A", "ll of one kind"), "Every board, every area or every channel"),
    (menu_key("E", "verything"), "Every board, area and channel"),
)


_MODERATOR_SCOPE_HELP = tuple(MenuEntry(label=label, brief=brief) for label, brief in _MODERATOR_SCOPE_CHOICES)


_MODERATOR_BLANKET_CHOICES = (
    (menu_key("M", "essage boards"), "Every message board"),
    (menu_key("F", "ile areas"), "Every file area"),
    (menu_key("C", "hat channels"), "Every chat channel"),
)


_MODERATOR_BLANKET_HELP = tuple(MenuEntry(label=label, brief=brief) for label, brief in _MODERATOR_BLANKET_CHOICES)


_MODERATOR_PRESETS = ["full", "limited", "post", "read"]


#: Presets that are access, not moderation (issue #836): a read or write
#: grant lets its holder past a board's or file area's level gate. Chat
#: channels have no read/write split.
_ACCESS_PRESETS = {"post", "read"}


#: `_pick_moderator_scope`'s "every kind at once" scope (issue #836) --
#: not an object type; `_grant_moderator_screen` writes it as the three
#: blanket grants through `grant_everywhere`.
_EVERYWHERE = "everywhere"


def _moderator_preset_label(object_type: str | None, preset: str) -> str:
    # The access presets can't cover channels, and the everything scope
    # includes them (review on #868): say so before Save is tried.
    no_access = object_type in ("channel", _EVERYWHERE)
    if preset == "post":
        return "Read and post (boards and areas only)" if no_access else "Read and post (past the level gates)"
    if preset == "read":
        return "Read only (boards and areas only)" if no_access else "Read only (past the read level)"
    if object_type == _EVERYWHERE:
        object_type = None
    if object_type == "channel":
        return "Full moderator (edit+moderate+manage members)" if preset == "full" else "Moderator only"
    if object_type is None:
        return "Full moderator" if preset == "full" else "Limited (approve/moderate only)"
    return "Full moderator (edit+delete+approve)" if preset == "full" else "Approver only"


def _moderator_preset_permissions(object_type: str, preset: str):
    if preset in _ACCESS_PRESETS:
        if object_type == "channel":
            raise ModeratorGrantError("read and post grants are for boards and file areas; channels use their level")
        return BoardPermission.READ | BoardPermission.WRITE if preset == "post" else BoardPermission.READ
    if object_type == "channel":
        if preset == "full":
            return ChannelPermission.EDIT | ChannelPermission.MODERATE | ChannelPermission.MANAGE_MEMBERS
        return ChannelPermission.MODERATE
    if preset == "full":
        return BoardPermission.EDIT | BoardPermission.DELETE | BoardPermission.APPROVE
    return BoardPermission.APPROVE


@located("Grant moderator")
async def _grant_moderator_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """
    Issue #282: this was a five-step chain (user picker, scope key,
    optional "scope to one Community?" yes/no plus picker, preset key,
    confirm) with no way back -- any invalid key cancelled the whole
    thing back to the Content menu. Now one draft-editor screen:
    `[U]ser`, `[O]n` (the scope: one board/area/channel, or a blanket
    across all of one kind), `[C]ommunity` (narrows a blanket grant to
    one Community's membership; not applicable to a per-object grant),
    `[P]reset` (a one-keystroke cycle), and `[S]ave`. Nothing is granted
    before Save, and the summary the editor keeps on screen is the
    confirmation -- a grant is reversible from `[R]evoke`.
    """
    redraw_in_place, redraw_hint = await lane.run(_resolve_redraw_preference, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    accent_color = await lane.run(effective_accent_color_256)
    header_color = await lane.run(effective_header_color_256)

    draft: dict = {
        "user": None, "object_type": None, "object_id": None, "label": None,
        "community": None, "preset": "full",
    }

    async def _user_prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        selected = await pick_item(
            session, await lane.run(list_users),
            name_of=lambda u: u.username, stable_id_of=lambda u: u.id,
            title="Grant moderator to which user?", empty_message="No registered users yet.",
            redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            accent_color=accent_color, header_color=header_color,
        )
        if selected is not None:
            draft["user"] = selected

    async def _scope_prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        scope = await _pick_moderator_scope(
            session, lane, redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed
        )
        if scope is None:
            return
        draft["object_type"], draft["object_id"], draft["label"] = scope
        if draft["object_id"] is not None:
            draft["community"] = None  # a specific object already belongs to its own Community

    async def _community_prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        if draft["object_type"] is None or draft["object_id"] is not None:
            _announce_line(session,
                colored(
                    "Community scoping applies to blanket grants only -- choose a blanket scope under On first.",
                    fg_color=MUTED_COLOR,
                )
            )
            return
        choices = [("(whole node)", None)]
        choices += [(community.name, community) for community in await lane.run(list_communities)]
        selected = await pick_item(
            session, choices,
            name_of=lambda item: item[0],
            stable_id_of=lambda item: 0 if item[1] is None else item[1].id,
            title="Scope the blanket grant to which Community?",
            empty_message="No Communities exist yet.",
            redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            accent_color=accent_color, header_color=header_color,
        )
        if selected is None:
            return
        draft["community"] = selected[1]

    def _community_render(d: dict) -> str:
        if d["object_type"] is None:
            return "(choose a scope first)"
        if d["object_id"] is not None:
            return "(n/a -- a specific object)"
        return sanitize_text(d["community"].name) if d["community"] is not None else "(whole node)"

    fields = [
        FieldSpec(
            key="user", label="User",
            render=lambda d: sanitize_text(d["user"].username) if d["user"] is not None else "(not chosen)",
            prompt=_user_prompt,
            brief="Who receives the grant",
            help="The account that will hold these moderator permissions.",
        ),
        FieldSpec(
            key="label", label="On",
            render=lambda d: sanitize_text(d["label"]) if d["label"] else "(not chosen)",
            prompt=_scope_prompt,
            brief="One resource, or all of one kind",
            help=(
                "One message board, file area or chat channel; or a blanket grant over every board, "
                "every area, every channel, or all three, including ones created later."
            ),
        ),
        FieldSpec(
            key="community", label="Community",
            render=_community_render,
            prompt=_community_prompt,
            brief="Narrow the grant to one Community",
            help=(
                "For a blanket grant only: limit it to what belongs to one Community, now or later, "
                "instead of the whole node."
            ),
        ),
        FieldSpec(
            key="preset", label="Preset",
            render=lambda d: _moderator_preset_label(d["object_type"], d["preset"]),
            prompt=choice_field("preset", _MODERATOR_PRESETS),
            step=choice_step("preset", _MODERATOR_PRESETS),
            brief="Moderator, approver, or access",
            help=(
                "Full moderator can edit, delete/moderate, and (for boards/areas) approve or (for "
                "channels) manage members. The limited preset only approves (boards/areas) or "
                "only moderates (channels). Read and post, and read only, are access rather than "
                "moderation: they let the holder past the minimum level of whatever the scope "
                "covers -- one board or area, or every one under a blanket scope -- such as "
                "posting on an announcements board with write level 255. Age and "
                "verified-name requirements still apply. A grant works at once; moderators, "
                "unlike access holders, are listed under Operators."
            ),
        ),
    ]

    async def save(draft: dict) -> bool | None:
        if draft["user"] is None or draft["object_type"] is None:
            # Raised so the editor redraws with the draft intact (Codex
            # review on #289) rather than closing on a bare `return None`.
            raise ModeratorGrantError("choose a User and a scope under On first")
        community = draft["community"]
        label = draft["label"]
        if community is not None:
            label = f"{label} scoped to Community {community.name!r}"
        # Access presets make nobody a moderator, so nobody joins Operators.
        staff_list_note = "" if draft["preset"] in _ACCESS_PRESETS else " Members see moderators under Operators."
        if draft["object_type"] == _EVERYWHERE:
            preset_label = _moderator_preset_label(_EVERYWHERE, draft["preset"])
            await lane.run(
                grant_everywhere,
                draft["user"],
                board_permissions=_moderator_preset_permissions("board", draft["preset"]),
                channel_permissions=_moderator_preset_permissions("channel", draft["preset"]),
                granted_by=actor, community_id=community.id if community is not None else None,
            )
            _announce_line(
                session, f"Granted {preset_label} on {label} to {draft['user'].username!r}.{staff_list_note}",
            )
            return True
        preset_label = _moderator_preset_label(draft["object_type"], draft["preset"])
        await lane.run(
            grant_permissions,
            draft["user"], object_type=draft["object_type"], object_id=draft["object_id"],
            permissions=_moderator_preset_permissions(draft["object_type"], draft["preset"]),
            granted_by=actor, community_id=community.id if community is not None else None,
        )
        _announce_line(
            session, f"Granted {preset_label} on {label} to {draft['user'].username!r}.{staff_list_note}",
        )
        return True

    await edit_resource_draft(
        session, lane,
        title="Grant moderator", own=True,
        fields=fields, draft=draft, save=save, error_type=ModeratorGrantError,
        save_menu_text=menu_key("S", "ave"), back_menu_text=menu_key("B", "ack"),
        description_level=await lane.run(menu_description_level, actor),
        redraw_in_place=redraw_in_place, redraw_hint=redraw_hint,
        unicode_style=unicode_style, collapsed=collapsed,
        accent_color=accent_color, header_color=header_color,
    )


async def _revoke_moderator_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """
    Issue #282: revoking used to mean re-describing the grant from
    memory -- user picker, scope key, object picker, then being told
    "has no grant on ..." if the guess was wrong -- and nothing anywhere
    listed what a user actually held. Now: pick the user, pick one of
    their real grants (which is also the only place "who moderates
    what" can be read), confirm, done.
    """
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    accent_color = await lane.run(effective_accent_color_256)
    header_color = await lane.run(effective_header_color_256)
    target = await pick_item(
        session, await lane.run(list_users),
        name_of=lambda u: u.username, stable_id_of=lambda u: u.id,
        title="Revoke moderator from which user?", empty_message="No registered users yet.",
        redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
        accent_color=accent_color, header_color=header_color,
    )
    if target is None:
        return

    def _load(db: Database) -> tuple[list, dict[tuple[str, int], str], dict[int, str]]:
        grants = list_grants_for_user(db, target)
        names: dict[tuple[str, int], str] = {}
        names.update({("board", b.id): b.name for b in list_boards(db, order_by="alphabetical")})
        names.update({("file_area", a.id): a.name for a in list_file_areas(db, order_by="alphabetical")})
        names.update({("channel", c.id): c.name for c in list_channels(db)})
        communities = {c.id: c.name for c in list_communities(db)}
        return grants, names, communities

    grants, names, communities = await lane.run(_load)
    if not grants:
        _announce_line(session, colored(f"{target.username!r} has no moderator grants.", fg_color=MUTED_COLOR))
        return

    kind_labels = {"board": "message board", "file_area": "file area", "channel": "chat channel"}
    blanket_labels = {"board": "all message boards", "file_area": "all file areas", "channel": "all chat channels"}

    def _grant_label(grant) -> str:
        if grant.object_id is None:
            label = f"{blanket_labels.get(grant.object_type, grant.object_type)} (blanket)"
            if grant.community_id is not None:
                label += f" scoped to Community {communities.get(grant.community_id, '#' + str(grant.community_id))!r}"
            return label
        name = names.get((grant.object_type, grant.object_id))
        kind = kind_labels.get(grant.object_type, grant.object_type)
        return f"{kind} {name!r}" if name is not None else f"{kind} #{grant.object_id} (deleted)"

    def _grant_description(grant) -> str:
        enum_type = ChannelPermission if grant.object_type == "channel" else BoardPermission
        return ", ".join(flag.name.lower().replace("_", " ") for flag in enum_type if grant.permissions & int(flag))

    selected = await pick_item(
        session, grants,
        name_of=_grant_label, stable_id_of=lambda g: g.id, description_of=_grant_description,
        title=f"{target.username}'s moderator grants", empty_message="No grants.",
        help_about=(
            'Every grant this account holds and what it allows. Picking one asks, then revokes it; '
            'the account loses that access or moderation at once.'
        ),
        redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
        accent_color=accent_color, header_color=header_color,
    )
    if selected is None:
        return
    label = _grant_label(selected)
    if not await prompt_yes_no(session, f"Revoke all permissions for {target.username!r} on {label}?", default=False):
        _announce_line(session, "Cancelled.")
        return

    permission_enum = ChannelPermission if selected.object_type == "channel" else BoardPermission
    await lane.run(
        revoke_permissions,
        target, object_type=selected.object_type, object_id=selected.object_id,
        permissions=permission_enum(selected.permissions), revoked_by=actor, community_id=selected.community_id,
    )
    _announce_line(session, f"Revoked the grant on {label} from {target.username!r}.")
