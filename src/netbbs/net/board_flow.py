"""
Message-board browsing and posting: `[M]essage boards` (and every route
into it -- a Community's page, `[N]ew scan`,
`[/] Find`), one bounded page of posts at a time (design doc, issue #10),
composing/editing/tombstoning a post, and quoted-reply rendering.

Split out of `netbbs.net.login_flow` (that module's own maintenance
split -- see its module docstring): the largest single concern pulled
out of that file, but a genuinely self-contained one -- nothing here
calls back into `login_flow` itself, only outward into shared
preference/rendering/domain modules. `_show_board` is this module's own
main entry point from elsewhere in the split (the main menu, `[N]ew
scan`, `[/] Find` search-hit selection) -- extracted before those other
pieces specifically so they could import it cleanly from here rather
than from `login_flow` (which will, once every other screen group is
also split out, hold only session-entry/auth logic).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path

from netbbs.activity import (
    ensure_board_baseline,
    follow,
    is_following,
    mark_board_read,
    record_post_opened,
    unread_post_count,
    unread_post_ids,
    unfollow,
)
from netbbs.attestation import format_name_for_resource, meets_age, meets_name_requirement
from netbbs.auth.users import User, get_user_by_id
from netbbs.boards import (
    MAX_BODY_BYTES,
    MAX_SUBJECT_BYTES,
    Board,
    Post,
    PostError,
    PostPage,
    create_post,
    edit_post,
    list_boards,
    list_posts_page,
    set_post_exempt,
    set_post_pinned,
    shown_post_refs,
    WITHDRAWN_PLACEHOLDER,
    list_post_revisions,
    tombstone_post,
    visible_post,
    withdraw_post,
)
from netbbs.boards.categories import Category, list_subcategories, list_top_level_categories
from netbbs.boards.categories import get_category_by_id as get_board_category_by_id
from netbbs.boards.posts import count_pending_posts, sweep_expired_posts
from netbbs.communities import (
    get_community,
    get_effective_min_age,
    get_effective_min_write_level,
    get_effective_name_requirement,
    meets_read_gate,
    meets_write_gate,
)
from netbbs.link.node_profiles import identity_for_fingerprint, present_link_author_label
from netbbs.link.remote_attestation import format_remote_name_for_resource
from netbbs.link.trust import TrustSubject
from netbbs.link.boards import (
    LinkContext,
    board_origin_fingerprint,
    is_board_closed,
    is_board_linked,
    queue_board_post_edit_if_linked,
    queue_board_post_if_linked,
    queue_board_post_moderator_edit_if_linked,
    queue_board_post_tombstone_if_linked,
)
from netbbs.moderation import BoardPermission, has_permission
from netbbs.mail import MAX_MAIL_SUBJECT_BYTES
from netbbs.file_refs import FileRef, body_with_link_text, open_ref, refs_some_readers_cannot_open
from netbbs.net.board_list_banner import load_board_list_banner
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.chat_flow import NAME_GATE_NOTE
from netbbs.net.char_input import HELP_KEY, REDRAW_KEY, EditorKey, EditorKeyKind, reject_unhandled_key
from netbbs.net.color_depth_preference import effective_truecolor
from netbbs.net.composition import (
    ReviewAction,
    characters_over,
    edit_line_body,
    read_subject,
    review_composition,
    show_compose_screen,
    too_long_message,
)
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.detail_view import show_detail
from netbbs.net.draft_storage import delete_draft, drafts_directory, load_draft
from netbbs.net.editor_preference import fullscreen_editor_enabled
from netbbs.net.file_ref_view import (
    attached_rows,
    change_attached_files,
    file_actions,
    get_referenced_file,
    ref_rows,
)
from netbbs.net.file_transfer import TransferGrants
from netbbs.net.help_overlay import show_help
from netbbs.net.mail_flow import (
    caller_mail_refusal, mail_blocked_notice, mail_someone, post_reply_key, split_link_address,
)
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.node_theme import (
    effective_accent_color,
    effective_accent_color_256,
    effective_header_color,
    effective_header_color_256,
)
from netbbs.net.notices import announce, pending_notice_rows, take_notices, write_notices
from netbbs.net.picker import ListColumn, pick_item
from netbbs.net.prose_editor import EditorHeader, edit_prose
from netbbs.net.ansi_editor import edit_ansi_art
from netbbs.net.post_color_preference import post_colors_enabled
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.session import Session, physical_terminal_width, post_body_width, write_prompt
from netbbs.net.session_activity import records_activity
from netbbs.net.sort_ui import SORT_MODE_LABELS, prompt_sort_change
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.rendering import (
    LABEL_COLOR,
    METADATA_COLOR,
    MUTED_COLOR,
    RULE_COLOR,
    SUCCESS_COLOR,
    MenuEntry,
    badge,
    colored,
    empty_state,
    menu_key,
    menu_row,
    reflow,
    sanitize_text,
    screen_title,
)
from netbbs.rendering.ansi import strip_ansi
from netbbs.rendering.charset import art_glyphs_to_cp437_controls
from netbbs.rendering.detail import Section, Styled
from netbbs.rendering.post_body import (
    art_body_from_editor,
    art_styles_editable,
    plain_post_body,
    post_body_mode,
    post_body_rows,
    post_body_text,
    split_signature,
    styled_post_body,
)
from netbbs.rendering.reflow import wrap_terminal_text
from netbbs.rendering.width import cut_to_width, display_width, wrap_to_width
from netbbs.quoting import quote_body, reply_subject
from netbbs.signature import append_signature, get_signature
from netbbs.sort_preferences import get_effective_sort_mode, set_sort_preference
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display

_MAX_PLAIN_POST_LINES = 200


# The board list's table (issue #679): activity is fixed-width so it can be
# scanned down the page; "about" is the flexible last column.
_BOARD_LIST_COLUMNS = [
    ListColumn("activity", len("not visited yet"), MUTED_COLOR),
    ListColumn("about", 30, MUTED_COLOR),
]


@records_activity("Boards")
async def _browse_boards(
    session: Session,
    db: Database,
    user: User,
    *,
    community_id: int | None = None,
    community_scoped: bool = False,
    title_prefix: str | None = None,
    link_context: LinkContext | None = None,
    transfers: TransferGrants | None = None,
) -> None:
    """Entry point: browse from the top level (no category selected yet).
    `transfers` is the node's browser-transfer grants, for downloading a
    file a post points at (issue #842)."""
    await _browse_boards_in_category(
        session, db, user, category_id=None,
        community_id=community_id, community_scoped=community_scoped, title_prefix=title_prefix,
        link_context=link_context, transfers=transfers,
    )


def visible_boards(db: Database, user: User, *, community_id: int | None, community_scoped: bool) -> list[Board]:
    """Every board `user` can see under the given Community filter --
    what a Community's page offers and counts (design doc §16, issue
    #838)."""
    boards = [
        b for b in list_boards(db)
        if meets_read_gate(db, user, b) and meets_age(db, user, get_effective_min_age(db, b))
    ]
    if community_scoped:
        boards = [b for b in boards if b.community_id == community_id]
    return boards


async def _browse_boards_in_category(
    session: Session,
    db: Database,
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
    Browse boards within a category (or the top level, if `category_id`
    is `None`), picking via the shared picker (`netbbs.net.picker`)
    instead of typing exact names — see design doc phasing sign-off notes
    for why. Directly answers a real usability problem: a flat list mixes
    unrelated topics together (e.g. one politics board sitting in the
    middle of a dozen vintage-computing boards under any sort order),
    which categories are meant to fix.

    Categories and boards are shown together in one mixed list — pick a
    category to drill in (recursing into this same function, naturally
    capped at two levels since a sub-category has no further
    sub-categories to recurse into), or pick a board directly to open it.
    Falls back to a flat board-only list at any level with no categories,
    identical to the pre-category browsing experience.

    One correctness detail: `Category` and `Board` rows come from
    different tables, so their database IDs can collide (both start at
    1) — mixed into one picker call, two different rows would share one
    identity (the picker reopens a list on a row by it). Disambiguated
    by negating category IDs for picker purposes only (`-item.id`) —
    boards keep their real, positive ID unchanged.

    `community_id`/`community_scoped` (design doc §16) narrow
    browsing to one Community's boards (`community_scoped=True`,
    `community_id=X`), boards outside every Community
    (`community_scoped=True`, `community_id=None` --
    `board.community_id == None` filters identically to the
    real-Community case, no special-casing needed), or no filter at all
    (`community_scoped=False`, the default, and what the main menu's
    `[M]essage boards` uses -- issue #838). `title_prefix`, threaded
    alongside, is `None` for the unfiltered case (keeping the "Available
    message boards" title) or a Community's own
    name that's passed to `pick_item` as an ancestor `breadcrumb`
    segment otherwise, so it renders muted with only "Message boards"
    itself in the current-location color -- not folded into the title
    text as a fake, uniformly-colored breadcrumb (dogfood-reported bug,
    see `pick_item`'s own `breadcrumb` docstring).
    Category leak prevention ("only show/offer categories
    currently used by ≥1 resource in this Community") only applies when
    `community_scoped` -- the unfiltered path shows every category
    exactly as it always has.

    Sort mode (design doc, dogfood feature request): unlike
    `netbbs.net.chat_flow._pick_channel`, `list_boards` already
    supports every mode (`"activity"`/`"alphabetical"`/`"recent"`/
    `"volume"`) directly against real, persisted columns -- no
    in-memory hub state to separately combine in, so a mode switch is
    just re-calling `_load` with a different `order_by`.
    `get_effective_sort_mode` resolves against this call's own
    `category_id`/Community scope, exactly the scope the `[O]rder`
    command's own save-scope prompt offers. This module has no
    `DatabaseLane` (unlike chat's long-running loop, board browsing
    here just calls `Database` directly), so persistence is a plain
    synchronous `set_sort_preference` call wrapped in an async closure
    for `netbbs.net.sort_ui.prompt_sort_change`'s own `persist` seam.
    """
    # name_requirement deliberately does not gate reading here -- it's a
    # participation/accountability requirement (design doc §18 point 7:
    # "mutual visible accountability" among people posting), not a
    # content-restriction the way min_age is; see can_post's own check,
    # below, for where it actually applies.
    effective_community_id = community_id if community_scoped else None
    # GitHub issue #176: resolved once, reused for both pick_item calls
    # below (flat and mixed-with-categories) -- shows at every level of
    # board browsing this recursive function reaches (top level, a
    # category, a Community's scope), not only the very
    # first unfiltered screen, matching this feature's own scoping
    # decision.
    board_masthead = load_board_list_banner(db, max_width=physical_terminal_width(session))

    def _load(order_by: str) -> tuple[list[Board], list[Category]]:
        all_boards = [
            b for b in list_boards(db, order_by=order_by)
            if meets_read_gate(db, user, b)
            and meets_age(db, user, get_effective_min_age(db, b))
        ]
        if community_scoped:
            all_boards = [b for b in all_boards if b.community_id == community_id]
        boards_here = [b for b in all_boards if b.category_id == category_id]

        categories_here = (
            list_top_level_categories(db) if category_id is None else list_subcategories(db, category_id)
        )
        if community_scoped:
            used_category_ids = {b.category_id for b in all_boards if b.category_id is not None}
            if category_id is None:
                categories_here = [
                    c for c in categories_here
                    if c.id in used_category_ids
                    or any(sub.id in used_category_ids for sub in list_subcategories(db, c.id))
                ]
            else:
                categories_here = [c for c in categories_here if c.id in used_category_ids]
        return boards_here, categories_here

    current_mode = get_effective_sort_mode(
        db, user, "board", community_id=effective_community_id, category_id=category_id
    )
    category_name = get_board_category_by_id(db, category_id).name if category_id is not None else None
    # Where the caller came from, carried onto the board's own screens
    # (issue #679): the Community, if any, and the category.
    # Continues the path this picker shows: the Community (or
    # none) is above "Message boards", a category below it.
    board_breadcrumb = (
        *((sanitize_text(title_prefix),) if title_prefix else ()),
        "Message boards",
        *((sanitize_text(category_name),) if category_name else ()),
    )
    community = get_community(db, effective_community_id)
    community_name = community.name if community is not None else None
    mode_box = {"mode": current_mode}

    async def _persist_sort_choice(mode: str, scope_kwargs: dict) -> None:
        set_sort_preference(db, user, "board", mode, **scope_kwargs)

    async def _run_sort_prompt() -> str | None:
        return await prompt_sort_change(
            session, persist=_persist_sort_choice,
            community_id=effective_community_id, community_name=community_name,
            category_id=category_id, category_name=category_name, sysop_order=True,
        )

    def _sort_label() -> str:
        return SORT_MODE_LABELS[mode_box["mode"]]

    unicode_style = unicode_style_enabled(db, user)
    collapsed = breadcrumb_collapsed_enabled(db, user)
    redraw_in_place = redraw_in_place_enabled(db, user)
    accent_color = effective_accent_color(session, db)
    header_color = effective_header_color(session, db)
    title = "Message boards" if title_prefix is not None else "Available message boards"
    picker_breadcrumb = (title_prefix,) if title_prefix is not None else ()
    description_level = menu_description_level(db, user)
    about_separator = " · " if unicode_style else " - "

    # What `[N]ew scan` already knows, on the list a caller picks from
    # (issue #679): whether a board has anything new, and whether it is
    # Linked or asks for a verified name before posting (design doc §3.6
    # puts the gate note ahead of the free-form description).
    def _activity(item: Category | Board) -> tuple[str, int]:
        if isinstance(item, Category):
            return "", MUTED_COLOR
        count = unread_post_count(db, user, item)
        if count is None:
            return "not visited yet", MUTED_COLOR
        if count == 0:
            return "caught up", MUTED_COLOR
        return f"{count} new", SUCCESS_COLOR

    def _about(item: Category | Board) -> str:
        if isinstance(item, Category):
            return item.description or "(category)"
        parts = []
        if is_board_linked(db, item):
            parts.append("[LINK]")
        if not meets_name_requirement(db, user, get_effective_name_requirement(db, item)):
            parts.append(NAME_GATE_NOTE)
        if item.description:
            parts.append(item.description)
        return about_separator.join(parts)

    def _columns_of(item: Category | Board) -> list[str | tuple[str, int]]:
        return [_activity(item), _about(item)]

    def _prose_of(item: Category | Board) -> str | None:
        """The same facts as one line, for a terminal too narrow for the
        table -- activity first, since that is what a caller scans for."""
        activity, _ = _activity(item)
        return about_separator.join(part for part in (activity, _about(item)) if part) or None

    # Back from a board or a category comes back to this list, on the row
    # left (issue #839): it used to return past it, to the menu the list
    # was opened from. Reloaded each time, since reading changes what is new.
    reopen_at: int | None = None
    while True:
        boards_here, categories_here = _load(mode_box["mode"])
        if not categories_here:
            async def on_sort_flat() -> list[Board] | None:
                new_mode = await _run_sort_prompt()
                if new_mode is None:
                    return None
                mode_box["mode"] = new_mode
                new_boards, _ = _load(new_mode)
                return new_boards

            board = await pick_item(
                session,
                boards_here,
                name_of=lambda b: b.name,
                stable_id_of=lambda b: b.id,
                description_of=_prose_of,
                columns=_BOARD_LIST_COLUMNS,
                column_values_of=_columns_of,
                description_level=description_level,
                title=title,
                breadcrumb=picker_breadcrumb,
                empty_message="No message boards are available to you yet.",
                on_sort=on_sort_flat,
                sort_label=_sort_label,
                redraw_in_place=redraw_in_place,
                unicode_style=unicode_style,
                collapsed=collapsed,
                accent_color=accent_color,
                header_color=header_color,
                masthead=board_masthead,
                start_stable_id=reopen_at,
            )
            if board is None:
                return
            reopen_at = board.id
            await _show_board(
                session, db, board, user, link_context=link_context, breadcrumb=board_breadcrumb,
                transfers=transfers,
            )
            continue

        mixed: list[Category | Board] = [*categories_here, *boards_here]

        def render_name(item: Category | Board) -> str:
            return f"[{item.name}]" if isinstance(item, Category) else item.name

        def stable_id(item: Category | Board) -> int:
            return item.id if isinstance(item, Board) else -item.id

        async def on_sort_mixed() -> list[Category | Board] | None:
            new_mode = await _run_sort_prompt()
            if new_mode is None:
                return None
            mode_box["mode"] = new_mode
            new_boards, _ = _load(new_mode)
            return [*categories_here, *new_boards]

        selected = await pick_item(
            session,
            mixed,
            name_of=render_name,
            stable_id_of=stable_id,
            on_sort=on_sort_mixed,
            sort_label=_sort_label,
            description_of=_prose_of,
            columns=_BOARD_LIST_COLUMNS,
            column_values_of=_columns_of,
            description_level=description_level,
            title=title,
            breadcrumb=picker_breadcrumb,
            empty_message="No message boards are available to you yet.",
            redraw_in_place=redraw_in_place,
            unicode_style=unicode_style,
            collapsed=collapsed,
            accent_color=accent_color,
            header_color=header_color,
            masthead=board_masthead,
            start_stable_id=reopen_at,
        )
        if selected is None:
            return
        reopen_at = stable_id(selected)

        if isinstance(selected, Category):
            await _browse_boards_in_category(
                session, db, user, category_id=selected.id,
                community_id=community_id, community_scoped=community_scoped, title_prefix=title_prefix,
                link_context=link_context, transfers=transfers,
            )
        else:
            await _show_board(
                session, db, selected, user, link_context=link_context, breadcrumb=board_breadcrumb,
                transfers=transfers,
            )


def _can_edit_post(db: Database, post: Post, user: User) -> bool:
    """The post's own original author, no grant needed, or anyone
    holding `BoardPermission.EDIT` -- the exact same authorization
    `netbbs.boards.posts.edit_post` itself enforces, checked here too
    so `[E]dit` only offers itself when it would actually succeed,
    rather than letting a SysOp compose a whole edit only to be
    rejected at the very end. `False` for an already-tombstoned post
    (design doc §9.5, issue #88) -- `edit_post` itself refuses those too."""
    if post.tombstoned_at is not None:
        return False
    return post.author_user_id == user.id or has_permission(
        db, user, object_type="board", object_id=post.board_id, permission=BoardPermission.EDIT
    )


def _can_tombstone_post(db: Database, post: Post, user: User) -> bool:
    """`BoardPermission.DELETE`, no author bypass (design doc §9.5,
    issue #88) -- the exact same authorization `netbbs.boards.posts.
    tombstone_post` itself enforces, checked here so `[T]ombstone` only
    offers itself when it would actually succeed. `False` for an
    already-tombstoned post."""
    if post.tombstoned_at is not None:
        return False
    return has_permission(db, user, object_type="board", object_id=post.board_id, permission=BoardPermission.DELETE)


def _can_pin_post(db: Database, post: Post, user: User) -> bool:
    """`BoardPermission.EDIT`, no author bypass -- what `set_post_pinned`
    and `set_post_exempt` enforce (design doc §5.3). A pin is this
    node's own presentation and is never carried over the Link, so a
    carried board's moderator pins for this node's callers only."""
    if post.tombstoned_at is not None:
        return False
    if post.withdrawn and not (post.pinned or post.exempt_from_expiry):
        # A withdrawn post may be unpinned or un-kept, never pinned or kept.
        return False
    return has_permission(db, user, object_type="board", object_id=post.board_id, permission=BoardPermission.EDIT)


def _toggle_post_flag(session: Session, db: Database, post: Post, user: User, *, pin: bool) -> None:
    """Flip `post`'s pin (`pin=True`) or its expiry exemption, and say
    what changed as an outcome above the next screen."""
    try:
        if pin:
            set_post_pinned(db, post, not post.pinned, changed_by=user)
            outcome = (
                "Post unpinned: it is back among the dated posts." if post.pinned
                else "Post pinned: it is listed at the top of this board."
            )
        else:
            set_post_exempt(db, post, not post.exempt_from_expiry, changed_by=user)
            outcome = (
                "Post no longer kept: it expires with the others." if post.exempt_from_expiry
                else "Post kept: it will not expire."
            )
    except PostError as exc:
        announce(session, f"Not changed: {exc}.", tone="error")
        return
    announce(session, outcome, tone="success")


# -- the post list (issue #679) ------------------------------------------------
#
# A board page is a list: one row per post, as many rows as the terminal
# holds, with a cursor. A post is read one at a time in `_read_post`, where its
# actions live. This replaced a page of five posts with their full bodies
# inline, which scrolled its own header away on any post longer than a few
# lines and left actions to pick their post by a typed digit.

# Fewer rows than this are not a list worth paging; more than this is a wall.
_MIN_LIST_ROWS = 3
_MAX_LIST_ROWS = 30
# Below this width the row is prose ("subject -- author, date") instead of
# columns (design doc §3.6: a table that does not fit becomes prose again).
_TABLE_MIN_WIDTH = 60
_NEW_MARKER = "new"
_PIN_MARKER = "pin"
# The caller's own post or edit awaiting a moderator (issue #678).
_HELD_MARKER = "held"
_MARKER_WIDTH = max(len(_NEW_MARKER), len(_PIN_MARKER), len(_HELD_MARKER))
# The row a pinned block adds to the list: the plain rule parting it from
# the dated posts (its labelled "Pinned" rule replaces the top rule).
_PINNED_BLOCK_ROWS = 1
# The two-column lead ("> " or "  ") and the four two-space gaps between
# the five columns, plus one column kept free: a row that reaches the
# last column makes many terminals wrap the cursor onto the next row.
_ROW_FURNITURE = 2 + 4 * 2 + 1
_AUTHOR_MAX_WIDTH = 24
_SUBJECT_MIN_WIDTH = 8
_HINT_MIN_HEIGHT = 20
_DESCRIPTION_ROWS = 2
# A list with fewer rows than this is worth trading the action bar's
# descriptions for.
_COMFORTABLE_LIST_ROWS = 6
# Rows around the list that are neither header nor action bar: the blank
# row, the column heading, the two rules, the blank row after, the hint
# and the prompt.
_LIST_FURNITURE_ROWS = 7


def _read_only_reason(db: Database, user: User, board: Board, *, closed: bool) -> str | None:
    """Why `[P]ost` is not offered, when the caller can read but not post
    (design doc §3.6: tell a caller why something present will refuse them).
    `None` when they can post. A closed board says so in its own notice."""
    if closed:
        return None
    write_level = get_effective_min_write_level(db, board)
    if not meets_write_gate(db, user, board):
        return f"Read only: posting needs level {write_level}."
    if not meets_name_requirement(db, user, get_effective_name_requirement(db, board)):
        return f"Read only: posting {NAME_GATE_NOTE}."
    if not meets_age(db, user, get_effective_min_age(db, board)):
        return "Read only: posting has an age requirement you do not meet."
    return None


def _linked_note(db: Database, board: Board, link_context: LinkContext | None) -> str | None:
    """"linked from X" for a board this node carries, "Linked" for one it
    originated -- the marker remote files already carry and boards lacked."""
    if not is_board_linked(db, board):
        return None
    origin = board_origin_fingerprint(db, board)
    if link_context is not None and origin == link_context.node_identity.fingerprint:
        return "Linked"
    return f"linked from {identity_for_fingerprint(db, origin).label}"


def _post_row_cells(db: Database, post: Post, *, name_requirement: str | None) -> tuple[str, str, str]:
    """Subject, author and date as plain cells. Tabs become spaces before
    anything is measured: `sanitize_text` keeps a tab, the width helpers
    count it as no column, and the transport writes it as one -- a subject
    full of tabs would otherwise overrun its column and the page budget."""
    subject = sanitize_text(post.subject).replace("\t", " ")
    author = strip_ansi(_author_display_name(db, post, name_requirement=name_requirement)).replace("\t", " ")
    when = format_for_display(post.created_at, db)
    return subject, author, when


def _column_widths(
    cells: list[tuple[str, str, str]], *, width: int, number_width: int
) -> tuple[int, int, int] | None:
    """(subject, author, date) widths that fit `width`, or `None` when no
    readable table fits and the rows should be prose instead. The author
    column gives way before the subject does: which post it is matters
    more than who wrote it, and the reader shows the author in full."""
    if width < _TABLE_MIN_WIDTH:
        return None
    date_width = max([display_width(w) for _, _, w in cells] + [len("Posted")])
    available = width - number_width - _MARKER_WIDTH - date_width - _ROW_FURNITURE
    author_width = min(
        _AUTHOR_MAX_WIDTH,
        max([display_width(a) for _, a, _ in cells] + [len("Author")]),
        max(len("Author"), available // 3),
    )
    subject_width = available - author_width
    if subject_width < _SUBJECT_MIN_WIDTH:
        return None
    return subject_width, author_width, date_width


def _post_list_rows(
    db: Database,
    posts: list[Post],
    *,
    width: int,
    highlighted: int | None,
    new_ids: set[int],
    name_requirement: str | None,
    accent: int | tuple[int, int, int],
    held_edits: frozenset[str] = frozenset(),
) -> list[str]:
    """One row per post, fitted to `width` in display columns. The number is
    what a digit key opens; the highlighted row is drawn in reverse video,
    the way the file area draws its cursor."""
    cells = [_post_row_cells(db, post, name_requirement=name_requirement) for post in posts]
    number_width = len(str(len(posts)))
    rows: list[str] = []
    widths = _column_widths(cells, width=width, number_width=number_width)
    if widths is None:
        for index, (post, (subject, author, when)) in enumerate(zip(posts, cells)):
            marker = _row_marker(post, new_ids, held_edits)
            marker = f"{marker} " if marker else ""
            # The date goes first when the row is this narrow: subject and
            # author say which post it is, and the reader shows the date.
            plain = cut_to_width(f"{index + 1:>{number_width}} {marker}{subject} -- {author}", width - 1)
            rows.append(
                colored(plain, reverse=True) if index == highlighted
                else colored(plain, fg_color=MUTED_COLOR if _dimmed(post) else None)
            )
        return rows
    subject_width, author_width, date_width = widths
    marker_width = _MARKER_WIDTH
    for index, (post, (subject, author, when)) in enumerate(zip(posts, cells)):
        number = f"{index + 1:>{number_width}}"
        subject_cell = _pad(cut_to_width(subject, subject_width), subject_width)
        marker = _row_marker(post, new_ids, held_edits)
        marker_cell = _pad(marker, marker_width)
        author_cell = _pad(cut_to_width(author, author_width), author_width)
        date_cell = _pad(cut_to_width(when, date_width), date_width)
        if index == highlighted:
            rows.append(colored(
                f"> {number}  {subject_cell}  {marker_cell}  {author_cell}  {date_cell}", reverse=True
            ))
            continue
        rows.append(
            "  "
            + colored(number, fg_color=accent)
            + "  "
            + colored(subject_cell, fg_color=MUTED_COLOR if _dimmed(post) else None)
            + "  "
            + (colored(marker_cell, fg_color=SUCCESS_COLOR, bold=True) if marker == _NEW_MARKER
               else colored(marker_cell, fg_color=accent))
            + "  "
            + colored(author_cell, fg_color=METADATA_COLOR)
            + "  "
            + colored(date_cell, fg_color=METADATA_COLOR)
        )
    return rows


def _labelled_rule(label: str, *, width: int, unicode_style: bool, color) -> str:
    """A rule with `label` set into it: "-- Pinned ------"."""
    char = "─" if unicode_style else "-"
    lead = char * 2
    text = f"{lead} {label} "
    return colored(text + char * max(0, width - display_width(text)), fg_color=color)


def _row_marker(post: Post, new_ids: set[int], held_edits: frozenset[str] = frozenset()) -> str:
    """"held" for the caller's own post awaiting a moderator (issue #678) --
    never news to its author, and listed to nobody else -- else "new"
    for a post this caller has not opened, else "held" for a post with an
    edit of theirs awaiting a moderator, else "pin" for a pinned one (issue
    #675) -- a pinned post is also listed first, so "new" is the one worth
    the column when it is both."""
    if post.status == "pending":
        return _HELD_MARKER
    if post.id in new_ids:
        return _NEW_MARKER
    if post.root_post_id in held_edits:
        return _HELD_MARKER
    return _PIN_MARKER if post.pinned else ""


def _dimmed(post: Post) -> bool:
    """A removed post, or the caller's own post nobody else sees yet."""
    return post.tombstoned_at is not None or post.status == "pending"


def _post_list_heading(posts: list[Post], *, width: int, db: Database, name_requirement: str | None) -> str | None:
    """The column heading row, or `None` below the table width."""
    if not posts:
        return None
    cells = [_post_row_cells(db, post, name_requirement=name_requirement) for post in posts]
    number_width = len(str(len(posts)))
    widths = _column_widths(cells, width=width, number_width=number_width)
    if widths is None:
        return None
    subject_width, author_width, date_width = widths
    marker_width = _MARKER_WIDTH
    text = (
        f"  {'#':>{number_width}}  {_pad('Subject', subject_width)}  {' ' * marker_width}  "
        f"{_pad('Author', author_width)}  {_pad('Posted', date_width)}"
    )
    return colored(text.rstrip(), fg_color=LABEL_COLOR, bold=True)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def _queue_entry(count: int) -> MenuEntry:
    """[Q]ueue for a caller who may approve posts here (issue #678), with
    how many wait."""
    return MenuEntry(label=menu_key("Q", f"ueue ({count})"), brief="Approve or reject held posts")


def _list_options(
    page: PostPage, *, can_post: bool, has_draft: bool, row_count: int, has_unread: bool,
    can_draw: bool = False, following: bool = False, queue_count: int = 0,
) -> list[MenuEntry]:
    options = []
    if row_count:
        keys = "1" if row_count == 1 else f"1-{min(row_count, 9)}"
        options.append(MenuEntry(label=menu_key(keys, "/Enter read"), brief="Read a post"))
    if page.has_older:
        options.append(MenuEntry(label=menu_key("O", "lder"), brief="Show older posts"))
    if page.has_newer:
        options.append(MenuEntry(label=menu_key("N", "ewer"), brief="Show newer posts"))
        options.append(MenuEntry(label=menu_key("R", "ecent"), brief="Jump to the newest page"))
    if can_post:
        options.append(MenuEntry(label=menu_key("P", "ost"), brief="Write a new post"))
    if can_draw:
        options.append(MenuEntry(label=menu_key("A", "rt post"), brief="Draw a post in the ANSI art editor"))
    if has_draft:
        options.append(_DRAFT_MENU_ENTRY)
    if has_unread:
        options.append(MenuEntry(label=menu_key("M", "ark all read"), brief="Count every post here as read"))
    if queue_count:
        options.append(_queue_entry(queue_count))
    # Issue #675: a followed board is listed first in [N]ew scan.
    options.append(
        MenuEntry(label=menu_key("f", "ollow", prefix="Un"), brief="Stop following this board") if following
        else MenuEntry(label=menu_key("F", "ollow"), brief="List this board first in New scan")
    )
    options.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to the previous menu"))
    return options


def _bounded_rows(text: str, width: int, rows: int) -> list[str]:
    """`text` wrapped to `width` (one column kept free) and cut to `rows`
    rows, the last ending in "..." when anything was cut."""
    wrapped = wrap_to_width(" ".join(text.split()), max(1, width - 1)) or [""]
    if len(wrapped) <= rows:
        return wrapped
    kept = wrapped[:rows]
    kept[-1] = cut_to_width(kept[-1], max(1, width - 4)) + "..."
    return kept


def _count_rows(text: str, width: int) -> int:
    return wrap_terminal_text(text, max(1, width)).count("\r\n") + 1 if text else 0


async def _read_list_key(session: Session) -> tuple[EditorKey, bool]:
    """A structured key, so Up/Down/Enter arrive as keys, with the plain
    `read_key` fallback lightweight sessions need. The flag says whether the
    key was echoed (only a `read_key` echoes), so a rejection erases only
    what was drawn -- the same contract `detail_view` keeps."""
    read_editor_key = getattr(session, "read_editor_key", None)
    if read_editor_key is not None:
        try:
            return await read_editor_key(distinguish_ctrl_h=True), False
        except NotImplementedError:
            pass
    return EditorKey(EditorKeyKind.CHAR, char=await session.read_key()), True


_LIST_HELP = [
    "Up/Down      move the highlight",
    "Enter, 1-9   read the highlighted post, or post number N",
    "O / N / R    older posts, newer posts, the newest page",
    "P            write a new post (when you may post here)",
    "A            draw a post in the ANSI art editor (boards with color)",
    "D            resume or discard a saved draft",
    "M            count every post on this board as read",
    "F            follow this board: New scan lists it first",
    "Ctrl-L       redraw the list",
    "B            back to the list of boards",
    "",
    "Pinned posts are listed first, marked \"pin\". A moderator pins",
    "and unpins a post, and keeps it from expiring, while reading it.",
    "",
    "Reading a post: Reply, Mail author (a private reply), Edit,",
    "Withdraw, Remove, Next and Previous post live there,",
    "and PgUp/PgDn page a long post. A post counts as read once you",
    "open it; the list marks the ones you have not opened as new.",
]


_SAVED_DRAFT_NOTICE = "You have a saved post draft for this message board from an earlier session."
_SAVED_DRAFT_KEPT_NOTICE = "Your draft is still saved -- [D]raft on this board resumes it."
_CLOSED_BOARD_NOTICE = "This message board is closed. It can be read, but it takes no new posts."
_STAYS_LOCAL_NOTICE = "Other nodes carrying this board keep the original: only its origin can change it for them."


def _moderation_stays_local(db: Database, board: Board, link_context: LinkContext | None) -> bool:
    """Whether a moderator's edit or removal on `board` changes this node's
    copy only. On a Linked board only the origin's moderation is signed
    and sent (`queue_board_post_moderator_edit_if_linked`,
    `queue_board_post_tombstone_if_linked`, design doc §9.5); everywhere
    else it is local, and the moderator is told so (issue #677)."""
    if not is_board_linked(db, board):
        return False
    return link_context is None or board_origin_fingerprint(db, board) != link_context.node_identity.fingerprint
_DRAFT_MENU_ENTRY = MenuEntry(label=menu_key("D", "raft"), brief="Resume or discard your saved draft")


@records_activity(lambda args: args["board"].name)
async def _show_board(
    session: Session,
    db: Database,
    board: Board,
    user: User,
    *,
    link_context: LinkContext | None = None,
    initial_cursor: tuple[str, str] | None = None,
    breadcrumb: tuple[str, ...] = ("Message boards",),
    transfers: TransferGrants | None = None,
) -> None:
    """
    Show `board`, one bounded page of posts at a time (design doc,
    issue #10) — never the whole board, however large its history.

    The page is a list, one row per post, as many rows as the terminal
    holds (issue #679); a post is read one at a time in `_read_post`,
    which is also where the post's own actions are. `breadcrumb` is the
    path between the node's name and the board's: the Community and
    category the caller came through, around "Message boards".

    Opens on the *newest* page, confirmed with Thiesi over keeping the
    old oldest-first default: an active board's most recent activity is
    what's actually useful to see on arrival, not its oldest history —
    directly answers the original complaint that returning to a board
    re-rendered everything, most of which was already read. `initial_
    cursor` (issue #56's `[N]ew scan` "jump to first unread"), if given,
    overrides this just for the very first render -- opens on the page
    immediately *after* that cursor instead of the newest page; every
    later Older/Newer/Recent navigation in this same call is unaffected.

    Composing a new post is a first-class `[P]ost` menu option inside
    the browsing loop (GitHub issue #40), not something a `[B]ack`
    choice used to silently fall through into on its way out (GitHub
    issue #39) -- `[B]ack` now always means back, nothing else.

    `link_context` (design doc), if given, is used by
    `_compose_new_post` to queue a `board_post` event when `board` is
    Linked -- `None` (Link disabled on this node, or a direct test call
    site) simply means a new post here never propagates over Link,
    same degrade-gracefully shape every other optional context uses.
    """
    board_name = sanitize_text(board.name)
    # A closed Linked board refuses every new post (`create_post`, design
    # doc §9.5), so [P]ost is not offered on one: the caller would write a
    # whole post before learning that (issue #677).
    closed = is_board_closed(db, board)
    can_post = (
        not closed
        and meets_write_gate(db, user, board)
        and meets_age(db, user, get_effective_min_age(db, board))
        and meets_name_requirement(db, user, get_effective_name_requirement(db, board))
    )
    description_level = menu_description_level(db, user)
    redraw_in_place = redraw_in_place_enabled(db, user)
    unicode_style = unicode_style_enabled(db, user)
    collapsed = breadcrumb_collapsed_enabled(db, user)
    truecolor = effective_truecolor(session, db, user)
    body_mode = post_body_mode(
        board_allows_color=board.allow_color, reader_wants_color=post_colors_enabled(db, user)
    )
    # The ANSI art editor is a third way to write a post, on a board that
    # shows color (issue #711).
    can_draw = can_post and board.allow_color
    name_requirement = get_effective_name_requirement(db, board)
    read_only_reason = None if can_post else _read_only_reason(db, user, board, closed=closed)
    linked_note = _linked_note(db, board, link_context)
    # A first visit counts what is already here as read; from then on a
    # post is read once it is opened, and only then (issue #710).
    ensure_board_baseline(db, user, board)
    unread = {"count": unread_post_count(db, user, board) or 0}
    # Whether the action bar offers [M]ark all read. Decided when a page is
    # fetched, with the page's row budget, and only ever withdrawn between
    # fetches: a bar that grew after the budget was set would push the list
    # off the screen.
    unread["menu"] = bool(unread["count"])
    separator = " · " if unicode_style else " - "
    follows = {"on": is_following(db, user, "board", board.id)}
    # Issue #678: a caller who may approve posts here decides on them here,
    # not only a SysOp in the console.
    can_approve = has_permission(
        db, user, object_type="board", object_id=board.id, permission=BoardPermission.APPROVE
    )

    def _queue_count() -> int:
        return count_pending_posts(db, board) if can_approve else 0

    async def _open_queue() -> None:
        """The board's moderation queue, on the console's own screens. The
        board page reads through `db`; those screens run on a lane, opened
        for as long as the queue is."""
        from netbbs.net.admin_flow import _pending_posts_screen

        lane = DatabaseLane(db.path)
        try:
            await _pending_posts_screen(session, lane, user, board, link_context=link_context)
        finally:
            lane.close()

    def _toggle_follow() -> None:
        """[F]ollow (issue #675): a followed board is listed first in
        [N]ew scan. Offered on the post list and on an empty board alike."""
        if follows["on"]:
            unfollow(db, user, "board", board.id)
            announce(session, "No longer following this board.", tone="muted")
        else:
            follow(db, user, "board", board.id)
            announce(session, "Following this board: New scan lists it first.")
        follows["on"] = not follows["on"]

    def _new_ids(current_page: PostPage) -> set[int]:
        return unread_post_ids(db, user, board, current_page.posts)

    def _frame(current_page: PostPage, *, row_count: int | None = None, measuring: bool = False) -> tuple[str, str]:
        """Everything above the list's rows and everything below them."""
        subtitle = ["Older posts" if current_page.has_newer else "Newest posts"]
        if unread["count"]:
            subtitle.append(f"{unread['count']} new")
        if linked_note:
            subtitle.append(linked_note)
        header = screen_title(
            board_name,
            breadcrumb=(session.node_display_name, *breadcrumb),
            subtitle=separator.join(subtitle),
            width=session.terminal_width,
            clear=redraw_in_place,
            unicode_style=unicode_style, collapsed=collapsed,
            header_color=effective_header_color(session, db),
            node_name_gradient=session.node_name_gradient,
        )
        notes = []
        if board.description:
            # At most two rows: a description has no length limit and can
            # arrive over Link, and the list is what the caller came for
            # (Codex review on #719).
            notes.extend(
                colored(row, fg_color=MUTED_COLOR)
                for row in _bounded_rows(sanitize_text(board.description), session.terminal_width, _DESCRIPTION_ROWS)
            )
        if closed:
            notes.append(colored(_CLOSED_BOARD_NOTICE, fg_color=MUTED_COLOR))
        elif read_only_reason:
            notes.append(colored(read_only_reason, fg_color=MUTED_COLOR))
        has_draft = _has_saved_draft()
        if has_draft:
            notes.append(colored(_SAVED_DRAFT_NOTICE, fg_color=MUTED_COLOR))
        above = "\r\n".join(["", header, *notes])
        options = _list_options(
            current_page, can_post=can_post, has_draft=has_draft,
            row_count=len(current_page.posts) if row_count is None else row_count,
            # The page budget measures the longer Un[f]ollow, so following
            # never changes how many posts fit (Codex review on #788).
            has_unread=unread["menu"], can_draw=can_draw, following=follows["on"] or measuring,
            queue_count=_queue_count(),
        )
        # Descriptions double the action bar. Where they would leave the
        # list fewer rows than a page worth having, the bar goes compact:
        # the posts are what the caller came for (Codex review on #719).
        # Decided against the busiest bar this board can draw, the one the
        # page budget measures, so the budget and the drawn frame agree.
        busiest = menu_row(
            _list_options(
                PostPage(posts=[], has_older=True, has_newer=True),
                can_post=can_post, has_draft=has_draft, row_count=9, has_unread=unread["menu"],
                can_draw=can_draw, following=True,  # the longer label
                queue_count=999 if can_approve else 0,
            ),
            width=session.terminal_width, height=session.terminal_height,
            description_level=description_level,
        )
        room = (
            session.terminal_height - _count_rows(above, session.terminal_width)
            - _count_rows(busiest, session.terminal_width) - _LIST_FURNITURE_ROWS
        )
        compact = description_level != "off" and room < _COMFORTABLE_LIST_ROWS
        menu = menu_row(
            options, width=session.terminal_width, height=session.terminal_height,
            description_level="off" if compact else description_level,
        )
        below_rows = [menu]
        # A hint is the first thing a short terminal can spare: the list's
        # rows are what the caller came for.
        if session.terminal_height >= _HINT_MIN_HEIGHT:
            below_rows.append(colored("(Up/Down to move, Ctrl-H for help)", fg_color=MUTED_COLOR))
        below = "\r\n".join(below_rows)
        return above, below

    def _page_limit() -> int:
        """As many rows as fit under the frame, measured against the
        busiest frame this board can draw -- a page does not change size
        because [N]ewer appeared on it."""
        width = session.terminal_width
        unread["menu"] = bool(unread["count"])
        # Nine rows, so the read entry is in the action bar exactly as a
        # populated page draws it (Codex review on #719).
        above, below = _frame(PostPage(posts=[], has_older=True, has_newer=True), row_count=9, measuring=True)
        fixed = (
            _count_rows(above, width) + 1 + (1 if width >= _TABLE_MIN_WIDTH else 0) + 2 + 1
            + _count_rows(below, width) + pending_notice_rows(session) + 1
        )
        return max(_MIN_LIST_ROWS, min(_MAX_LIST_ROWS, session.terminal_height - fixed))

    async def _render(current_page: PostPage, highlighted: int | None) -> None:
        width = session.terminal_width
        above, below = _frame(current_page)
        rule = colored(
            ("─" if unicode_style else "-") * min(width, 78),
            fg_color=238 if truecolor else RULE_COLOR,
        )
        lines = [above, ""]
        heading = _post_list_heading(current_page.posts, width=width, db=db, name_requirement=name_requirement)
        if heading is not None:
            lines.append(heading)
        rows = _post_list_rows(
            db, current_page.posts, width=width, highlighted=highlighted,
            new_ids=_new_ids(current_page), name_requirement=name_requirement,
            accent=effective_accent_color(session, db), held_edits=current_page.held_edits,
        )
        pinned = current_page.pinned_count
        if pinned:
            # The pinned block under its own labelled rule, parted from the
            # dated posts by a plain one (issue #675). The labelled rule
            # takes the place of the plain top rule, so the block costs one
            # row -- `_PINNED_BLOCK_ROWS`, which the page budget reserves.
            lines.append(_labelled_rule("Pinned", width=min(width, 78), unicode_style=unicode_style,
                                        color=238 if truecolor else RULE_COLOR))
            lines.extend(rows[:pinned])
            if rows[pinned:]:
                lines.append(rule)
            lines.extend(rows[pinned:])
        else:
            lines.append(rule)
            lines.extend(rows)
        lines.extend([rule, "", below])
        for line in lines:
            await session.write_line(line)
        await write_notices(session)
        await session.write("Choice: ")

    def _refetch_current_page(*, limit: int | None = None) -> PostPage:
        """Re-fetches whichever page is currently on screen, using the
        exact cursor that produced it -- not always the newest page.
        Needed after an in-place edit (which never moves a post's feed
        position, see netbbs.boards.posts._resolve_current_version)
        so [E]diting a post doesn't also silently jump the SysOp back
        to page one as an unrelated side effect."""
        rows = limit if limit is not None else _page_limit()
        if page_anchor is None:
            return list_posts_page(
                db, board, user, limit=rows, with_pinned=True, pinned_block_rows=_PINNED_BLOCK_ROWS
            )
        mode, cursor = page_anchor
        return list_posts_page(
            db, board, user, limit=rows, with_pinned=True, pinned_block_rows=_PINNED_BLOCK_ROWS, **{mode: cursor}
        )

    def _refetch_keeping(current_page: PostPage, highlighted: int | None) -> tuple[PostPage, int | None]:
        """The page on screen, refetched at the budget of the moment -- which
        counts an outcome notice just queued, and a resized terminal -- with
        the highlight on the post it was on, not on its row number (Codex
        review on #719 and #723). Should the smaller page lose that post off
        its end, the page keeps its size instead: a row too many is better
        than Enter opening a different post."""
        was_on = (
            current_page.posts[highlighted].post_id
            if highlighted is not None and highlighted < len(current_page.posts) else None
        )
        fresh = _refetch_current_page()
        if was_on is None or not fresh.posts:
            return fresh, None
        for candidate in (fresh, _refetch_current_page(limit=len(current_page.posts))):
            index = next((i for i, listed in enumerate(candidate.posts) if listed.post_id == was_on), None)
            if index is not None:
                return candidate, index
        # The post itself is gone (removed, expired, hidden).
        return fresh, min(highlighted, len(fresh.posts) - 1)

    async def _render_fresh(current_page: PostPage, highlighted: int | None = None) -> None:
        """Render after anything that can change what is unread -- a post
        read, written, removed, or the page refetched. Showing the list
        marks nothing read (issue #710): only opening a post does.

        The unread count is counted once on arrival and kept from there --
        one fewer for each unread post opened, none after [M]ark all read --
        rather than recounted here: a carried board's count checks trust
        post by post (Codex review on #723)."""
        if not unread["count"]:
            unread["menu"] = False
        await _render(current_page, highlighted)

    async def _read_post(index: int) -> int | None:
        """Read `page.posts[index]`, one post to a screen, and step to the
        post before or after it -- across page boundaries -- until
        `[B]ack`. Returns the index, on the page now current, of the post
        last read, so the list comes back with the cursor on it -- or
        `None` when the page emptied while the caller read (a removal, a
        trust change, the expiry sweep) and there is no post to put it on.

        Built on `show_detail`, which keeps the title, the byline and the
        action bar on screen while a long post pages under them."""
        nonlocal page, page_anchor
        detail_page = 0
        while True:
            post = page.posts[index]
            # Opening a post is what makes it read (issue #710), recorded as
            # it is shown, so a dropped connection loses nothing already read.
            # Whether it was new is taken first: the byline says so on the
            # screen that opens it (Codex review on #723).
            was_new = bool(unread_post_ids(db, user, board, [post]))
            if was_new:
                unread["count"] = max(0, unread["count"] - 1)
            if record_post_opened(db, user, board, post):
                # The opened-set cap gave other unread posts up as read.
                unread["count"] = unread_post_count(db, user, board) or 0
            width = session.terminal_width
            title = screen_title(
                sanitize_text(post.subject),
                breadcrumb=(session.node_display_name, *breadcrumb, board_name),
                width=width,
                clear=False,
                unicode_style=unicode_style, collapsed=collapsed,
                header_color=effective_header_color(session, db),
                node_name_gradient=session.node_name_gradient,
            )
            # The caller's own post awaiting a moderator (issue #678): nothing
            # can be done with it until it is approved or rejected.
            held = post.status == "pending"
            byline = _post_byline(
                db, post, name_requirement=name_requirement, is_new=was_new,
                separator=separator, width=width,
                held="post" if held else "edit" if post.root_post_id in page.held_edits else None,
            )
            body_rows = post_body_rows(
                post.body, post_body_width(session, post.layout), body_mode, truecolor=truecolor,
                layout=post.layout,
            )
            # Files the post points at (issue #842), as this reader finds
            # them: a file in an area closed to them is not named.
            refs = shown_post_refs(db, post)
            byline = [*byline, *ref_rows(
                [open_ref(db, user, ref) for ref in refs], accent=effective_accent_color(session, db),
            )]
            has_previous = index > 0 or (page.has_older and page.oldest_cursor is not None)
            has_next = index < len(page.posts) - 1 or (page.has_newer and page.newest_cursor is not None)
            actions = []
            # Anyone who may post here may answer a post that is still there
            # (issue #675).
            can_reply = can_post and post.tombstoned_at is None and not held
            if can_reply:
                actions.append(("r", menu_key("R", "eply")))
            # A private reply to the author (issue #821), to anyone who may
            # read the post, whether or not they may post here.
            mail_target = (
                _post_author_mail_target(db, post, user, link_context=link_context)
                if post.tombstoned_at is None and not held and caller_mail_refusal(session, db, user) is None
                else None
            )
            if mail_target is not None:
                actions.append(("m", menu_key("M", "ail author")))
            if refs and post.tombstoned_at is None:
                actions.append(("g", menu_key("G", "et file")))
            # An edited or removed post's versions, for moderators only
            # (issue #675, decided with the maintainer).
            can_see_history = not held and (post.is_edited or post.tombstoned_at is not None) and has_permission(
                db, user, object_type="board", object_id=post.board_id, permission=BoardPermission.EDIT
            )
            if can_see_history:
                actions.append(("h", menu_key("H", "istory")))
            # The author takes their own post back (issue #675).
            can_withdraw = (
                post.author_user_id is not None and post.author_user_id == user.id
                and post.tombstoned_at is None and not post.withdrawn and not held
            )
            if can_withdraw:
                actions.append(("w", menu_key("W", "ithdraw")))
            can_edit = not held and _can_edit_post(db, post, user)
            if can_edit:
                actions.append(("e", menu_key("E", "dit")))
            can_tombstone = not held and _can_tombstone_post(db, post, user)
            if can_tombstone:
                actions.append(("t", menu_key("t", prefix="Remove pos")))
            can_pin = not held and _can_pin_post(db, post, user)
            if can_pin:
                actions.append(("i", menu_key("i", "n", prefix="Unp" if post.pinned else "P")))
                # Keeping a post only means something where posts expire,
                # or where one was kept before the board stopped expiring.
                if board.max_post_age_days is not None or post.exempt_from_expiry:
                    actions.append(("k", menu_key("k", "eep", prefix="Un") if post.exempt_from_expiry
                                    else menu_key("K", "eep")))
            if has_next:
                actions.append(("n", menu_key("N", "ext post")))
            if has_previous:
                actions.append(("p", menu_key("P", "revious post")))
            actions.append(("b", menu_key("B", "ack")))
            key, detail_page = await show_detail(
                session,
                title=title,
                sections=[Section(None, [Styled(body_rows)])],
                actions=actions,
                redraw_in_place=redraw_in_place,
                unicode_style=unicode_style,
                page=detail_page,
                preamble=byline,
                message="\r\n".join(take_notices(session)) or None,
            )
            if key == "b":
                return index
            if key == "g" and refs and post.tombstoned_at is None:
                # Downloads run on a lane, as mail's do; the board page reads
                # through `db`, so one is opened for the download.
                file_lane = DatabaseLane(db.path)
                try:
                    await get_referenced_file(
                        session, file_lane, user, refs, noun="post",
                        breadcrumb=(*breadcrumb, board_name), style=_picker_style(session, db, user),
                        transfers=transfers,
                    )
                finally:
                    file_lane.close()
                continue
            if key == "h" and can_see_history:
                await _show_history(
                    session, db, board, post, user, breadcrumb=(session.node_display_name, *breadcrumb, board_name),
                    name_requirement=name_requirement, body_mode=body_mode, truecolor=truecolor,
                    redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
                    separator=separator,
                )
                continue
            if key == "w" and can_withdraw:
                if await _withdraw_existing_post(session, db, board, post, user, link_context=link_context):
                    root = post.root_post_id
                    page = _refetch_current_page(limit=len(page.posts))
                    found = next((i for i, p in enumerate(page.posts) if p.root_post_id == root), None)
                    if found is None:
                        page_anchor = None
                        page = _refetch_current_page()
                        return None
                    index = found
                continue
            if key == "m" and mail_target is not None:
                # Mail runs on a lane; the board page reads through `db`, so
                # one is opened for as long as the letter is (as the
                # moderation queue does).
                account, address = mail_target
                mail_lane = DatabaseLane(db.path)
                try:
                    await mail_someone(
                        session, mail_lane, user, recipient=account, link_address=address,
                        subject=reply_subject(post.subject, max_bytes=MAX_MAIL_SUBJECT_BYTES),
                        quote=_reply_quote(db, post, board, name_requirement=name_requirement),
                        reply_key=post_reply_key(post.root_post_id), link_context=link_context,
                    )
                finally:
                    mail_lane.close()
                continue
            if key == "r" and can_reply:
                if _reply_target(db, post, board) is None:
                    # Expired or removed while this screen was open (Codex
                    # review on #786): back to the list, which no longer
                    # shows it.
                    announce(session, "That post is no longer available to reply to.", tone="error")
                    page_anchor = None
                    page = _refetch_current_page()
                    return None
                if await _compose_new_post(reply_to=post):
                    # A reply lands on the newest page, like any new post.
                    page_anchor = None
                    page = _refetch_current_page()
                    return None
                continue
            if (key == "e" and can_edit) or (key == "t" and can_tombstone) or (key in ("i", "k") and can_pin):
                root = post.root_post_id
                if key == "e":
                    await _edit_existing_post(
                        session, db, board, post, user, link_context=link_context,
                        breadcrumb=(*breadcrumb, board_name),
                    )
                elif key == "t":
                    await _tombstone_existing_post(session, db, board, post, user, link_context=link_context)
                else:
                    _toggle_post_flag(session, db, post, user, pin=key == "i")
                # The same number of rows as the page the reader is on: an
                # outcome notice now pending takes a row from a fresh budget,
                # and a page one post shorter could drop the post just acted
                # on (Codex review on #719).
                page = _refetch_current_page(limit=len(page.posts))
                if not page.posts:
                    page_anchor = None
                    page = _refetch_current_page()
                    return None
                found = next((i for i, p in enumerate(page.posts) if p.root_post_id == root), None)
                if found is None and key in ("i", "k"):
                    # Unpinned off this page: its dated place is on an
                    # older one. Back to the list, rather than showing some
                    # other post as if it were this one (Codex review on
                    # #783).
                    return None
                index = found if found is not None else min(index, len(page.posts) - 1)
                continue
            detail_page = 0
            if key == "n":
                if index < len(page.posts) - 1:
                    index += 1
                    continue
                page_anchor = ("after", page.newest_cursor)
                page = _refetch_current_page()
                index = 0
            else:
                if index > 0:
                    index -= 1
                    continue
                page_anchor = ("before", page.oldest_cursor)
                page = _refetch_current_page()
                index = len(page.posts) - 1
            if not page.posts:
                # Emptied from under the caller (a post removed, a trust
                # change): back to the newest page rather than a blank one.
                page_anchor = None
                page = _refetch_current_page()
                return None

    async def _compose_screen(title: str) -> None:
        await show_compose_screen(
            session, title=title, breadcrumb=(*breadcrumb, board_name),
            redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            header_color=effective_header_color(session, db), accent_color=effective_accent_color(session, db),
        )

    async def _compose_new_post(
        *, initial_body: str | None = None, reply_to: Post | None = None, resumed: bool = False
    ) -> bool:
        """[P]ost, or with `reply_to` a reply to that post (issue #675): the
        subject starts as "Re: ...", the body as the post quoted, with the
        cursor under the quote. Returns whether a post was published.

        `resumed` (issue #814): `initial_body` is the saved new-post draft,
        which stays on disk until the editor replaces it -- a subject left
        empty, or a connection dropped before the first keystroke, no
        longer loses it."""
        # `[P]ost` is a hotkey followed straight by a line prompt: an Enter
        # typed right behind it ("P<Enter>") would otherwise be read as a
        # blank subject and cancel the post. Same guard as mail's compose.
        discard_buffered_enter = getattr(session, "discard_buffered_enter", None)
        if discard_buffered_enter is not None:
            await discard_buffered_enter()
        compose_title = "Reply" if reply_to is not None else "New post"
        # Its own screen, not a prompt under the post list (issue #813).
        await _compose_screen(compose_title)
        # Checked as it is typed, not at Publish (issue #812).
        subject = await read_subject(
            session, max_bytes=MAX_SUBJECT_BYTES, blank_cancels=True,
            current=reply_subject(reply_to.subject, max_bytes=MAX_SUBJECT_BYTES) if reply_to is not None else None,
        )
        if not subject:
            if resumed:
                announce(session, _SAVED_DRAFT_KEPT_NOTICE, tone="muted")
                return False
            announce(session, "Reply cancelled." if reply_to else "Post cancelled.", tone="muted")
            return False
        if reply_to is None:
            draft_path = _post_draft_path(db, kind="new", board=board, user=user)
            draft_saved_notice = "Draft saved -- you'll be offered it next time you visit this message board."
            cancelled_notice = "Post cancelled."
        else:
            draft_path = _post_draft_path(db, kind="reply", board=board, user=user, root_post_id=reply_to.root_post_id)
            draft_saved_notice = "Draft saved -- you'll be offered it when you reply to this post again."
            cancelled_notice = "Reply cancelled."
            if initial_body is None:
                initial_body = _reply_quote(db, reply_to, board, name_requirement=name_requirement) or None
        published = {"done": False}

        async def _commit(commit_subject: str, commit_body: str, commit_files: list[FileRef]) -> bool:
            if reply_to is not None and _reply_target(db, reply_to, board) is None:
                # Gone while the reply was written: the text stays in review
                # to be copied or cancelled, not published under a post no
                # one can reach.
                announce(session, "The post you are replying to is no longer available.", tone="error")
                return False
            done = await _publish(
                commit_subject, commit_body, parent_post_id=reply_to.root_post_id if reply_to else None,
                files=commit_files,
            )
            published["done"] = published["done"] or done
            return done

        body = await _compose_body(
            session, db, user, initial_text=initial_body, draft_path=draft_path,
            keep_pasted_color=board.allow_color, cursor_at_end=reply_to is not None,
            header=_editor_header(session, db, board, compose_title, subject), offer_recovery=not resumed,
        )
        if body is not None:
            # `append_signature` is idempotent (its own docstring): a
            # resumed draft (`initial_body`) may or may not already
            # carry the signature depending on exactly when it was
            # saved, and this can't cheaply tell which without that
            # idempotency -- so it's always attempted here, safely,
            # rather than only on a "first, fresh compose" heuristic
            # that missed the /exit-then-resume case entirely.
            signature = get_signature(db, user)
            if signature:
                body = append_signature(body, signature)
        if body is None:
            # Issue #149: /exit or /quit (either editor) leaves the
            # draft on disk instead of deleting it -- that's the one
            # thing distinguishing this from an explicit /cancel here,
            # since both return `None` the same way.
            if draft_path.exists():
                announce(session, draft_saved_notice, tone="muted")
            else:
                announce(session, cancelled_notice, tone="muted")
            return False
        await _review_and_commit(
            session, db, user, board, subject=subject, body=body, draft_path=draft_path, files=[],
            commit_key="p", commit_label="ost", commit_brief="Publish this reply" if reply_to else "Publish this post",
            title=compose_title, breadcrumb=(*breadcrumb, board_name),
            cancelled_notice=cancelled_notice,
            draft_saved_notice=draft_saved_notice,
            commit=_commit,
        )
        return published["done"]

    async def _compose_art_post() -> bool:
        """[A]rt post (issue #711): a subject, then the ANSI art editor,
        then the same review and publishing a written post gets. The post
        keeps its lines. Returns whether the editor was opened, so the
        caller moves to the newest page only when a post may exist."""
        discard_buffered_enter = getattr(session, "discard_buffered_enter", None)
        if discard_buffered_enter is not None:
            await discard_buffered_enter()
        draft_path = _post_draft_path(db, kind="art", board=board, user=user)
        choice = await _art_draft_choice(session, db, user, draft_path)
        if choice == "back":
            return False
        resumed = _recovered_drawing(draft_path) if choice == "resume" else None
        # Before the subject: a terminal the editor cannot open on, or one
        # too small for the resumed drawing, is said so before anything is
        # typed for nothing (Codex review on #753). The draft stays.
        if _art_canvas(session, resumed) is None:
            return False
        await _compose_screen("New art post")
        subject = await read_subject(session, max_bytes=MAX_SUBJECT_BYTES, blank_cancels=True)
        if not subject:
            announce(session, "Post cancelled.", tone="muted")
            return False
        body = await _draw_body(session, db, user, initial_text=resumed, draft_path=draft_path)
        if body is None:
            announce(session, "Post cancelled.", tone="muted")
            return True
        # The signature every composed post gets, as lines under the
        # drawing (Codex review on #753).
        signature = get_signature(db, user)
        if signature:
            # Reset first: a drawing whose last row fills its last column
            # ends without one, and the signature would take its color.
            body = append_signature(body + "\x1b[0m", signature)
        await _review_and_commit(
            session, db, user, board, subject=subject, body=body, draft_path=draft_path, files=[],
            commit_key="p", commit_label="ost", commit_brief="Publish this post",
            title="New art post", breadcrumb=(*breadcrumb, board_name),
            cancelled_notice="Post cancelled.",
            draft_saved_notice="Draft saved -- the art editor offers it the next time you draw here.",
            commit=lambda subject, body, files: _publish(subject, body, layout="art", files=files),
            layout="art",
        )
        return True

    async def _publish(
        subject: str, body: str, *, layout: str = "prose", parent_post_id: str | None = None,
        files: list[FileRef] | None = None,
    ) -> bool:
        try:
            post = create_post(
                db, board, user, subject, body, layout=layout, parent_post_id=parent_post_id, files=files,
            )
        except PostError as exc:
            announce(session, f"Could not create post: {exc}", tone="muted")
            return False
        # A caller's own post is not news to them (issue #710). A held
        # one is recorded too, so it is not new when it is approved.
        if record_post_opened(db, user, board, post):
            # The opened-set cap gave other unread posts up as read.
            unread["count"] = unread_post_count(db, user, board) or 0
        if link_context is not None:
            queue_board_post_if_linked(db, post, board, node_identity=link_context.node_identity)
        if post.status == "pending":
            # A moderated board holds the post back: only its author sees
            # it, marked "held" (issue #678), until a moderator approves it.
            announce(session, "Submitted. Others will see it once a moderator approves it.")
        else:
            announce(session, "Posted.")
        return True

    def _has_saved_draft() -> bool:
        # Gated on `can_post` the same way [P]ost itself already is: no
        # point surfacing a draft the caller couldn't act on to post if
        # they resumed it.
        return can_post and _post_draft_path(db, kind="new", board=board, user=user).exists()

    async def _saved_draft_menu(*, from_post: bool = False) -> bool:
        """Issue #149's other half, reshaped by issue #282: the saved
        new-post draft for this exact (user, board) is announced on the
        board page itself and handled behind its own `[D]raft` entry,
        rather than as a modal question fired before the first post was
        rendered on every entry until dealt with. Scoped to `kind="new"`
        only -- an in-progress *edit* of a specific existing post has no
        equally natural board-level moment, so it stays exclusively
        behind the existing recovery-on-reopen path inside
        `_compose_body`.

        `from_post` (Codex review on the same change): `[P]ost` while a
        draft exists comes through here too, because there is exactly
        one autosave slot per (user, board) and composing straight into
        it would let the editor's own crash-recovery prompt -- or the
        fullscreen editor's autosave -- consume the saved draft without
        an explicit choice. In that mode `[D]iscard` deletes the draft
        and then opens a fresh editor. Returns whether an editor was
        actually opened, so the caller only resets its page position
        when a post may have been created.
        """
        draft_path = _post_draft_path(db, kind="new", board=board, user=user)
        if not draft_path.exists():
            if from_post:
                await _compose_new_post()
                return True
            return False
        await session.write_line(colored(f"\r\n{_SAVED_DRAFT_NOTICE}", fg_color=MUTED_COLOR))
        await session.write_line(
            menu_row(
                [
                    MenuEntry(label=menu_key("R", "esume"), brief="Open it in the editor"),
                    MenuEntry(
                        label=menu_key("D", "iscard"),
                        brief="Delete it, then start a new post" if from_post else "Delete the draft",
                    ),
                    MenuEntry(label=menu_key("B", "ack"), brief="Leave it for later"),
                ],
                width=session.terminal_width, height=session.terminal_height,
                description_level=description_level,
            )
        )
        await write_prompt(session, "Choice: ")
        while True:
            choice = (await session.read_key()).lower()
            if choice == "b":
                await session.write_line("")
                return False
            if choice == "d":
                delete_draft(draft_path)
                announce(session, "Draft deleted.", tone="muted")
                if from_post:
                    await _compose_new_post()
                    return True
                return False
            if choice == "r":
                await session.write_line("")
                saved_text = load_draft(draft_path)
                # Handed to the editor with `resumed`, which keeps it from
                # asking about the very draft this menu just handed off --
                # and leaves it on disk until the editor replaces it, so a
                # cancelled subject or a dropped connection does not lose
                # it (issue #814).
                await _compose_new_post(initial_body=saved_text, resumed=True)
                return True
            await session.write(reject_unhandled_key(choice))

    page_anchor: tuple[str, tuple[str, str]] | None = ("after", initial_cursor) if initial_cursor else None
    page = (
        list_posts_page(db, board, user, after=initial_cursor, limit=_page_limit(), with_pinned=True)
        if initial_cursor else list_posts_page(db, board, user, limit=_page_limit(), with_pinned=True, pinned_block_rows=_PINNED_BLOCK_ROWS)
    )
    if initial_cursor and not page.posts:
        # Nothing newer than the cursor `[N]ew scan` jumped in with --
        # the user is caught up, not looking at a genuinely empty board.
        # Fall back to the ordinary newest-page view rather than the
        # "has no posts yet" path below, which would falsely claim the
        # board is empty and (worse) prompt to compose the first post.
        page_anchor = None
        page = list_posts_page(db, board, user, limit=_page_limit(), with_pinned=True, pinned_block_rows=_PINNED_BLOCK_ROWS)
    # A [N]ew scan or [/] Find jump puts the cursor on the post it came for.
    # When that post is on the newest page, the jump opens that page, the
    # one an ordinary visit shows, rather than a page starting at the post:
    # that page left out every read post and numbered the rest from 01, so
    # the number a caller remembered picked nothing (issue #839, F095).
    jump_highlight: int | None = None
    if page_anchor is not None and page.posts:
        target = page.posts[0].root_post_id
        newest = list_posts_page(
            db, board, user, limit=_page_limit(), with_pinned=True, pinned_block_rows=_PINNED_BLOCK_ROWS
        )
        # In the dated rows only: a pinned target is also listed in the
        # pinned block, and taking that match could skip the unread posts
        # between it and the newest page's rows (review on #869).
        on_newest = next(
            (
                i for i, listed in enumerate(newest.posts)
                if i >= newest.pinned_count and listed.root_post_id == target
            ),
            None,
        )
        if on_newest is not None:
            page, page_anchor, jump_highlight = newest, None, on_newest
        else:
            jump_highlight = 0
    if not page.posts:
        # Dogfood report: this used to skip straight to composing the
        # first post whenever the caller could write, with no [P]ost/
        # [B]ack choice first -- the exact same "walked into it" problem
        # issue #39/#40 already fixed for the non-empty case, just never
        # extended to this one. Still skips the full Older/Newer/Edit
        # navigation loop (nothing to browse either way), but offers the
        # same explicit choice before composing anything.
        #
        # Issue #680: drawn whole on every pass, and always with a [B]ack
        # bar. A caller who cannot post used to get the empty state and an
        # immediate return, straight into the board list's redraw -- the
        # screen flashed and vanished before it could be read, a closed
        # board's notice with it. And a pass after composing drew its
        # choices under whatever the review screen had left.
        header_color = effective_header_color_256(db)

        async def _draw_empty_board() -> bool:
            has_draft = _has_saved_draft()
            await session.write_line(
                f"\r\n{screen_title(board_name, breadcrumb=(session.node_display_name, *breadcrumb), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color, node_name_gradient=session.node_name_gradient)}"
            )
            await session.write_line(
                f"\r\n{empty_state('This message board has no posts yet', detail='It is ready for its first conversation.', width=session.terminal_width, header_color=header_color)}"
            )
            if closed:
                await session.write_line(colored(f"\r\n{_CLOSED_BOARD_NOTICE}", fg_color=MUTED_COLOR))
            elif read_only_reason:
                await session.write_line(colored(f"\r\n{read_only_reason}", fg_color=MUTED_COLOR))
            if has_draft:
                await session.write_line(colored(f"\r\n{_SAVED_DRAFT_NOTICE}", fg_color=MUTED_COLOR))
            options = []
            if can_post:
                options.append(MenuEntry(label=menu_key("P", "ost"), brief="Write the first post"))
            if can_draw:
                options.append(MenuEntry(label=menu_key("A", "rt post"), brief="Draw the first post"))
            if has_draft:
                options.append(_DRAFT_MENU_ENTRY)
            # Its readers see no posts, but held ones may wait (issue #678).
            if _queue_count():
                options.append(_queue_entry(_queue_count()))
            # An empty board can be followed too, to be told of its first
            # post (Codex review on #788).
            options.append(
                MenuEntry(label=menu_key("f", "ollow", prefix="Un"), brief="Stop following this board")
                if follows["on"]
                else MenuEntry(label=menu_key("F", "ollow"), brief="List this board first in New scan")
            )
            options.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to the previous menu"))
            await session.write_line(
                "\r\n" + menu_row(
                    options, width=session.terminal_width, height=session.terminal_height,
                    description_level=description_level,
                )
            )
            await write_notices(session)
            await session.write("Choice: ")
            return has_draft

        # Redrawn after composing (the review screen replaced it), never
        # after a stray key: `reject_unhandled_key` leaves the prompt as it
        # was, and reprinting the screen per keystroke would stack copies
        # of it without redraw-in-place (Claude review on #701), the same
        # rule the empty file area and the populated board page follow.
        has_draft = await _draw_empty_board()
        while True:
            choice = (await session.read_key()).lower()
            if choice == "b":
                await session.write_line("")
                return
            if choice == "f":
                await session.write_line("")
                _toggle_follow()
                has_draft = await _draw_empty_board()
                continue
            if choice == "q" and _queue_count():
                await session.write_line("")
                await _open_queue()
                # As on a populated page: an approved post may be new here.
                unread["count"] = unread_post_count(db, user, board) or 0
                page = list_posts_page(db, board, user, limit=_page_limit(), with_pinned=True, pinned_block_rows=_PINNED_BLOCK_ROWS)
                if page.posts:
                    # An approval gave the board its first post.
                    break
                has_draft = await _draw_empty_board()
                continue
            if (choice == "p" and can_post) or (choice == "d" and has_draft) or (choice == "a" and can_draw):
                await session.write_line("")
                if choice == "a":
                    await _compose_art_post()
                else:
                    await _saved_draft_menu(from_post=choice == "p")
                page = list_posts_page(db, board, user, limit=_page_limit(), with_pinned=True, pinned_block_rows=_PINNED_BLOCK_ROWS)
                if page.posts:
                    # A post was actually created (not cancelled) --
                    # fall through to the ordinary render+navigation
                    # loop below, same post-then-refresh behavior the
                    # non-empty case's own [P]ost option already has.
                    break
                has_draft = await _draw_empty_board()
                continue
            await session.write(reject_unhandled_key(choice))

    # A [N]ew scan or [/] Find jump starts with the cursor on its target, so
    # Enter reads what the caller came for.
    highlighted: int | None = jump_highlight
    await _render_fresh(page, highlighted)
    while True:
        key, echoed = await _read_list_key(session)
        char = key.char.lower() if key.kind == EditorKeyKind.CHAR and key.char else ""

        async def _moved_on() -> None:
            # A key `read_key` echoed leaves the cursor after it; start the
            # next output on a fresh line, as every hotkey here always has.
            if echoed:
                await session.write_line("")

        if key.kind in (EditorKeyKind.UP, EditorKeyKind.DOWN) and page.posts:
            step = -1 if key.kind == EditorKeyKind.UP else 1
            if highlighted is None:
                highlighted = 0 if step == 1 else len(page.posts) - 1
            else:
                highlighted = (highlighted + step) % len(page.posts)
            await _render(page, highlighted)
        elif (
            (key.kind == EditorKeyKind.ENTER or char in ("\r", "\n"))
            and highlighted is not None and highlighted < len(page.posts)
        ):
            await _moved_on()
            highlighted = await _read_post(highlighted)
            await _render_fresh(page, highlighted)
        elif len(char) == 1 and char in "123456789" and int(char) <= min(9, len(page.posts)):
            await _moved_on()
            highlighted = await _read_post(int(char) - 1)
            await _render_fresh(page, highlighted)
        elif (key.kind == EditorKeyKind.CTRL and key.char == "l") or char == REDRAW_KEY:
            page, highlighted = _refetch_keeping(page, highlighted)
            await _render_fresh(page, highlighted)
        elif (key.kind == EditorKeyKind.CTRL and key.char == "h") or char == HELP_KEY:
            await show_help(
                session, "Message board keys", _LIST_HELP,
                header_color=effective_header_color_256(db), unicode_style=unicode_style,
            )
            await _render(page, highlighted)
        elif char == "o" and page.has_older and page.oldest_cursor is not None:
            await _moved_on()
            page_anchor = ("before", page.oldest_cursor)
            page = _refetch_current_page()
            highlighted = None
            await _render_fresh(page)
        elif char == "n" and page.has_newer and page.newest_cursor is not None:
            await _moved_on()
            page_anchor = ("after", page.newest_cursor)
            page = _refetch_current_page()
            highlighted = None
            await _render_fresh(page)
        elif char == "r" and page.has_newer:
            await _moved_on()
            page_anchor = None
            page = _refetch_current_page()
            highlighted = None
            await _render_fresh(page)
        elif char == "p" and can_post:
            await _moved_on()
            if await _saved_draft_menu(from_post=True):
                page_anchor = None  # a freshly-created post always lands on the newest page
                highlighted = None
                page = _refetch_current_page()
            else:
                # Nothing was posted: the same page, the highlight kept.
                page, highlighted = _refetch_keeping(page, highlighted)
            await _render_fresh(page, highlighted)
        elif char == "a" and can_draw:
            await _moved_on()
            if await _compose_art_post():
                page_anchor = None  # a new post lands on the newest page
                highlighted = None
                page = _refetch_current_page()
            else:
                page, highlighted = _refetch_keeping(page, highlighted)
            await _render_fresh(page, highlighted)
        elif char == "d" and _has_saved_draft():
            await _moved_on()
            if await _saved_draft_menu():
                page_anchor = None  # a resumed-and-posted draft lands on the newest page too
                highlighted = None
                page = _refetch_current_page()
            else:
                # Discarded or left: the same page, the highlight kept --
                # "Draft deleted." must not cost the highlighted row (Codex
                # review on #719).
                page, highlighted = _refetch_keeping(page, highlighted)
            await _render_fresh(page, highlighted)
        elif char == "f":
            await _moved_on()
            _toggle_follow()
            # Refetched: the notice takes a row the page was not sized for.
            page, highlighted = _refetch_keeping(page, highlighted)
            await _render_fresh(page, highlighted)
        elif char == "q" and _queue_count():
            await _moved_on()
            await _open_queue()
            # An approved post may be new to the caller, and the page's
            # count and [M]ark all read must say so (Codex review on #796).
            unread["count"] = unread_post_count(db, user, board) or 0
            # Back on the page the caller was reading (Codex review on
            # #796): an approved post joins the list in its dated place.
            page, highlighted = _refetch_keeping(page, highlighted)
            await _render_fresh(page, highlighted)
        elif char == "m" and unread["menu"]:
            await _moved_on()
            mark_board_read(db, user, board)
            unread["count"] = 0
            announce(session, "Every post on this board is marked read.", tone="muted")
            # Refetched: the notice takes a row the page was not sized for.
            page, highlighted = _refetch_keeping(page, highlighted)
            await _render_fresh(page, highlighted)
        elif char == "b":
            await _moved_on()
            return
        else:
            await session.write(reject_unhandled_key(key.char) if echoed and key.char else "\a")


async def _edit_existing_post(
    session: Session,
    db: Database,
    board: Board,
    post: Post,
    user: User,
    *,
    link_context: LinkContext | None = None,
    breadcrumb: tuple[str, ...] = ("Message boards",),
) -> None:
    """
    Edit `post`, the one the reader is showing (issue #679: actions live
    where the post is, rather than asking for a page-relative number).

    Authorization is checked *before* prompting for any new content
    (`_can_edit_post`, the same rule `edit_post` itself enforces) so a
    SysOp who picks a post they can't actually edit finds out
    immediately, not after composing a whole revision.

    `link_context` (design doc), if given, queues a `board_post_edit`
    for a Linked board right after a successful `edit_post` when `user`
    is the post's own original author, or a `board_post_moderator_edit`
    (design doc §9.5, issue #88) when `user` is instead a moderator
    editing someone else's post *and* this node is the board's own
    current origin -- a carrying (non-origin) node's own local moderator
    edit stays purely local, not propagated (see `queue_board_post_
    moderator_edit_if_linked`'s own docstring for why).
    """
    if not _can_edit_post(db, post, user):
        announce(session, "You can't edit that post.", tone="muted")
        return
    # An art post is revised in the editor that drew it (issue #711), with
    # a draft slot of its own: the prose editors' recovery must never be
    # handed a canvas. Its saved draft is offered first; then the drawing
    # chosen -- the draft, or the post -- must fit this terminal, before
    # anything is asked (Codex review on #753).
    art = post.layout == "art"
    initial_body = post.body
    if art:
        art_draft = _post_draft_path(db, kind="art_edit", board=board, user=user, root_post_id=post.root_post_id)
        choice = await _art_draft_choice(session, db, user, art_draft)
        if choice == "back":
            return
        if choice == "resume":
            # The drawing from the draft, under this post's own signature.
            signature_block = split_signature(post.body)[1]
            initial_body = _recovered_drawing(art_draft) + (f"\x1b[0m{signature_block}" if signature_block else "")
        if _art_canvas(session, split_signature(initial_body)[0]) is None:
            return  # said why; the draft stays

    # Its own screen (issue #813); `breadcrumb` ends at the board.
    await show_compose_screen(
        session, title="Edit post", breadcrumb=breadcrumb,
        redraw_in_place=redraw_in_place_enabled(db, user), unicode_style=unicode_style_enabled(db, user),
        collapsed=breadcrumb_collapsed_enabled(db, user),
        header_color=effective_header_color(session, db), accent_color=effective_accent_color(session, db),
    )
    subject = await read_subject(session, max_bytes=MAX_SUBJECT_BYTES, current=post.subject)

    edit_draft_path = _post_draft_path(
        db, kind="art_edit" if art else "edit", board=board, user=user, root_post_id=post.root_post_id
    )
    draft_saved_notice = "Draft saved -- you'll be offered it next time you edit this post."
    editor = _draw_body if art else partial(
        _compose_body, keep_pasted_color=board.allow_color,
        header=_editor_header(session, db, board, "Edit post", subject),
    )
    body = await editor(session, db, user, initial_text=initial_body, draft_path=edit_draft_path)
    if body is None:
        # Issue #149: /exit or /quit leaves this revision's draft on
        # disk instead of deleting it -- same distinguishing check as
        # _compose_new_post's own. There's no board-entry prompt for an
        # in-progress *edit* (see _offer_saved_draft_if_any's own
        # docstring for why), so it's only ever resurfaced by picking
        # [E]dit on this same post again.
        if edit_draft_path.exists():
            announce(session, draft_saved_notice, tone="muted")
        else:
            announce(session, "Edit cancelled.", tone="muted")
        return

    # Only the author changes what their post points at (review on #913): a
    # moderator's edit keeps the files as they are, and offers no file keys,
    # so nothing is attached to someone else's post -- or to a carried one --
    # checked only against the moderator's own access.
    own_post = post.author_user_id is not None and post.author_user_id == user.id
    current_files = shown_post_refs(db, post) if own_post else None

    async def _save(subject: str, body: str, files: list[FileRef] | None) -> bool:
        if subject == post.subject and body == post.body and files == current_files:
            announce(session, "No changes to save.", tone="muted")
            return True
        try:
            edited = edit_post(db, post, board, subject=subject, body=body, edited_by=user, files=files)
        except PostError as exc:
            # Back to review with the revision intact: the editor already
            # deleted its draft on /done, so returning here would lose it.
            announce(session, f"Could not save edit: {exc}", tone="muted")
            return False
        if link_context is not None:
            queue_board_post_edit_if_linked(
                db, edited, board, node_identity=link_context.node_identity, edited_by=user
            )
            queue_board_post_moderator_edit_if_linked(
                db, edited, board, node_identity=link_context.node_identity, edited_by=user
            )
        if edited.status == "pending":
            announce(session, "Edit submitted. The post keeps its current text until a moderator approves it.")
        else:
            announce(session, "Post updated.")
        if post.author_user_id != user.id and _moderation_stays_local(db, board, link_context):
            announce(session, _STAYS_LOCAL_NOTICE, tone="muted")
        return True

    await _review_and_commit(
        session, db, user, board, subject=subject, body=body, draft_path=edit_draft_path, layout=post.layout,
        files=current_files,
        commit_key="s", commit_label="ave", commit_brief="Save this edit",
        title="Edit post", breadcrumb=breadcrumb,
        cancelled_notice="Edit cancelled.",
        draft_saved_notice=draft_saved_notice,
        commit=_save,
    )


async def _review_and_commit(
    session: Session,
    db: Database,
    user: User,
    board: Board,
    *,
    subject: str,
    body: str,
    draft_path: Path,
    commit_key: str,
    commit_label: str,
    commit_brief: str,
    cancelled_notice: str,
    draft_saved_notice: str,
    commit: Callable[[str, str, list[FileRef] | None], Awaitable[bool]],
    layout: str = "prose",
    title: str = "New post",
    breadcrumb: tuple[str, ...] = ("Message boards",),
    files: list[FileRef] | None = None,
) -> None:
    """The review screen a new post and an edit both pass through
    before anything is stored: the draft is shown whole, its subject and
    body can be revised, and `commit(subject, body)` persists it.

    `commit` returns whether the composition is finished. A `False`
    (the domain refused it -- a subject over the byte cap, a board
    closed meanwhile) keeps the caller in review with the text intact:
    the editor deleted its draft when it handed the body back, so this
    loop is the only copy left.

    The draft is previewed as `board`'s readers will see it: in color
    where the board allows it and the caller wants it (issue #711).

    `title` names the composition ("New post", "Reply", "Edit post") and
    `breadcrumb` is the path to the board: the review screen is under
    both, and the fullscreen editor's header shows the title (issue #813).

    `files` are the files the post points at so far (issue #842), changed
    here with `[A]ttach file` and `[R]emove file` and handed to `commit`
    with the subject and body. `None` -- a moderator editing someone else's
    post -- offers no file keys and hands `commit` `None`: the files stay."""
    body_mode = post_body_mode(
        board_allows_color=board.allow_color, reader_wants_color=post_colors_enabled(db, user)
    )
    files = list(files) if files is not None else None
    linked = is_board_linked(db, board)
    while True:
        # Said on arrival, in characters (issue #812), rather than by the
        # domain's byte-counting refusal at Publish: the editors stop a
        # body at the limit, but a signature is added after them.
        too_long = _too_long_to_post(subject, body)
        if too_long is None and files and linked:
            # Other nodes get a line naming each file at the end of the
            # post (issue #842), which counts toward its length there.
            too_long = _file_lines_too_long(body_with_link_text(db, body, files))
        if too_long is not None:
            announce(session, too_long, tone="error")
        action = await review_composition(
            session,
            recipient=None,
            subject=subject,
            body=body,
            commit_key=commit_key,
            commit_label=commit_label,
            commit_brief=commit_brief,
            description_level=menu_description_level(db, user),
            redraw_in_place=redraw_in_place_enabled(db, user),
            unicode_style=unicode_style_enabled(db, user),
            collapsed=breadcrumb_collapsed_enabled(db, user),
            accent_color=effective_accent_color(session, db),
            header_color=effective_header_color(session, db),
            truecolor=effective_truecolor(session, db, user),
            body_mode=body_mode,
            body_layout=layout,
            breadcrumb=(*breadcrumb, title),
            extra_rows=(
                _file_rows(db, board, files, accent=effective_accent_color(session, db), linked=linked)
                if files is not None else ()
            ),
            extra_actions=file_actions(files, noun="post") if files is not None else (),
        )
        if isinstance(action, str) and files is not None:
            # `[A]ttach file` or `[R]emove file`: the pickers run on a lane,
            # opened for as long as they are.
            file_lane = DatabaseLane(db.path)
            try:
                files = await change_attached_files(
                    session, file_lane, user, files, action, noun="post",
                    breadcrumb=(*breadcrumb, title), style=_picker_style(session, db, user),
                )
            finally:
                file_lane.close()
            continue
        if action is ReviewAction.CANCEL:
            announce(session, cancelled_notice, tone="muted")
            return
        if action is ReviewAction.EDIT_SUBJECT:
            subject = await read_subject(session, max_bytes=MAX_SUBJECT_BYTES, current=subject)
            continue
        if action is ReviewAction.EDIT_BODY:
            editor = _draw_body if layout == "art" else partial(
                _compose_body, keep_pasted_color=board.allow_color,
                header=_editor_header(session, db, board, title, subject),
            )
            revised = await editor(session, db, user, initial_text=body, draft_path=draft_path)
            if revised is not None:
                body = revised
            elif draft_path.exists():
                # /exit or /quit while revising -- issue #149: this
                # leaves the whole in-progress composition as a saved
                # draft, not just "keep the previous body and stay in
                # review."
                announce(session, draft_saved_notice, tone="muted")
                return
            else:
                announce(session, "Body unchanged.", tone="muted")
            continue
        if too_long is not None:
            continue
        if await commit(subject, body, files):
            return


def _file_rows(db: Database, board: Board, files: list[FileRef], *, accent, linked: bool) -> list[str]:
    """The review screen's rows for the files a post points at (issue #842):
    each file, then what not every reader will get."""
    rows = attached_rows(files, accent=accent)
    if not files:
        return rows
    narrower = refs_some_readers_cannot_open(db, files, board)
    if narrower:
        names = ", ".join(sanitize_text(ref.filename) for ref in narrower)
        rows.append(colored(
            f"Not everyone who can read this board can open the file area of {names}; "
            "they see only that a file is there.",
            fg_color=MUTED_COLOR,
        ))
    if linked:
        rows.append(colored(
            "Readers on other BBSes get each file's name, size and file area as text at the end of the "
            "post, not a download.",
            fg_color=MUTED_COLOR,
        ))
    return rows


def _file_lines_too_long(link_body: str) -> str | None:
    """Why a post whose Link copy is `link_body` -- the body with its file
    lines (issue #842) -- is too long to publish, or `None`."""
    over = characters_over(link_body, MAX_BODY_BYTES)
    if over:
        return (
            f"{too_long_message('With the file lines added for other BBSes, the post is', over)}"
            " -- shorten it with [B]ody or [R]emove a file."
        )
    return None


def _picker_style(session: Session, db: Database, user: User) -> dict:
    """`pick_item`'s presentation arguments for the file pickers
    (issue #842)."""
    return {
        "description_level": menu_description_level(db, user),
        "redraw_in_place": redraw_in_place_enabled(db, user),
        "unicode_style": unicode_style_enabled(db, user),
        "collapsed": breadcrumb_collapsed_enabled(db, user),
        "accent_color": effective_accent_color_256(db),
        "header_color": effective_header_color_256(db),
    }


def _too_long_to_post(subject: str, body: str) -> str | None:
    """Why this post cannot be published as it stands, in characters --
    or `None`. The limits `netbbs.boards.posts` enforces in bytes."""
    over = characters_over(subject, MAX_SUBJECT_BYTES)
    if over:
        return f"{too_long_message('The subject is', over)} -- shorten it with [U]pdate subject."
    over = characters_over(body, MAX_BODY_BYTES)
    if over:
        return f"{too_long_message('The post is', over)} -- shorten it with [B]ody."
    return None


async def _tombstone_existing_post(
    session: Session,
    db: Database,
    board: Board,
    post: Post,
    user: User,
    *,
    link_context: LinkContext | None = None,
) -> None:
    """
    Remove `post`, the one the reader is showing (design doc §9.5, issue
    #88; issue #679 moved the action into the reader). Redacts the post
    to a placeholder revision
    (`netbbs.boards.posts.tombstone_post`) rather than deleting it
    outright, so the edit chain and any reply's `parent_post_id` stay
    intact -- there was no existing live UI action to redact an
    already-published post at all before this issue (the only existing
    `delete_post` call site handles pending-post rejection, a different
    case that never reaches an approved post).

    `link_context`, if given, queues a `board_post_tombstone` right
    after a successful `tombstone_post`, but only when this node is the
    board's own current origin -- same origin-only reasoning as
    `queue_board_post_moderator_edit_if_linked` (see that function's own
    docstring).
    """
    if not _can_tombstone_post(db, post, user):
        announce(session, "You can't remove that post.", tone="muted")
        return

    stays_local = _moderation_stays_local(db, board, link_context)
    subject = sanitize_text(post.subject)
    question = (
        f"Remove \"{subject}\" on this node only?"
        if stays_local
        else f"Remove \"{subject}\"? This cannot be undone."
    )
    if not await prompt_yes_no(session, question, default=False):
        announce(session, "Cancelled.", tone="muted")
        return

    try:
        tombstoned = tombstone_post(db, post, board, tombstoned_by=user)
    except PostError as exc:
        announce(session, f"Could not remove the post: {exc}", tone="error")
        return
    if link_context is not None:
        queue_board_post_tombstone_if_linked(db, tombstoned, board, node_identity=link_context.node_identity)
    if stays_local:
        announce(session, "Post removed on this node.")
        announce(session, _STAYS_LOCAL_NOTICE, tone="muted")
    else:
        announce(session, "Post removed.")


def _post_draft_path(db: Database, *, kind: str, board: Board, user: User, root_post_id: str = "") -> Path:
    """A stable per-(user, board, [post]) draft location, colocated
    with the node's database the same way `netbbs.net.welcome_banner.
    banner_path` already colocates its own single global draft --
    there just needs to be more than one slot here, one per in-progress
    composition/edit, so this lives in its own subdirectory rather than
    a single flat sibling file.

    Shared by both editors (`netbbs.net.prose_editor.edit_prose`'s
    crash-recovery autosave, `netbbs.net.composition.edit_line_body`'s
    `/exit`/`/quit`) and by two different recovery UIs at two different
    moments (issue #149): `_offer_saved_draft_if_any`, proactively, at
    board entry for `kind="new"`; each editor's own on-entry
    `draft_path.exists()` check otherwise, for `kind="edit"` or for a
    `kind="new"` draft the board-entry prompt didn't consume."""
    suffix = f"_{root_post_id}" if root_post_id else ""
    return drafts_directory(db) / f"{kind}_{board.id}_{user.id}{suffix}.draft"


# The smallest canvas the art editor opens on: the project's 40x12
# terminal floor, less the editor's status rows (Codex review on #753).
_ART_MIN_WIDTH = 40
_ART_MIN_HEIGHT = 9


def _art_canvas(session: Session, drawing: str | None) -> tuple[int, int] | None:
    """The art editor's canvas on this terminal -- as wide as it allows,
    up to 80 columns, and as tall -- or `None`, with the reason announced,
    when the terminal is below the editor's minimum or too small for
    `drawing`, which the canvas would cut (Codex review on #753)."""
    width = min(80, session.terminal_width)
    height = session.terminal_height - 3  # the editor's status line below the canvas
    if width < _ART_MIN_WIDTH or height < _ART_MIN_HEIGHT:
        announce(
            session,
            f"The art editor needs a terminal at least {_ART_MIN_WIDTH} columns wide "
            f"and {_ART_MIN_HEIGHT + 3} rows tall.",
            tone="muted",
        )
        return None
    if drawing:
        try:
            # The canvas holds CP437; anything else would save as "?".
            # Pictographs (☺ ♥ ►) are CP437 too, as their control bytes.
            art_glyphs_to_cp437_controls(strip_ansi(drawing)).encode("cp437")
        except UnicodeEncodeError:
            announce(
                session,
                "This drawing has characters the art editor cannot keep, so it is not opened for editing.",
                tone="muted",
            )
            return None
        if not art_styles_editable(drawing):
            announce(
                session,
                "This drawing uses underline or blink, which the art editor cannot keep, "
                "so it is not opened for editing.",
                tone="muted",
            )
            return None
        lines = drawing.replace("\t", " ").split("\n")
        drawn_width = max(display_width(strip_ansi(line)) for line in lines)
        if drawn_width > width or len(lines) > height:
            announce(
                session,
                f"This drawing is {drawn_width}x{len(lines)}: editing it needs a terminal at least "
                f"{drawn_width} columns wide and {len(lines) + 3} rows tall, or part of it would be cut.",
                tone="muted",
            )
            return None
    return width, height


async def _art_draft_choice(session: Session, db: Database, user: User, draft_path: Path) -> str:
    """An art draft left by an earlier session, offered before anything
    else is asked: ``"resume"``, ``"discard"`` (deleted, and said so), or
    ``"back"``; ``"none"`` when there is no draft. A drawing is never lost
    to an unlabeled default (Codex review on #753)."""
    if not draft_path.exists():
        return "none"
    await session.write_line(colored(
        "\r\nYou have a saved drawing from an earlier session.", fg_color=MUTED_COLOR
    ))
    await session.write_line(menu_row(
        [
            MenuEntry(label=menu_key("R", "esume"), brief="Open it in the art editor"),
            MenuEntry(label=menu_key("D", "iscard"), brief="Delete it and start over"),
            MenuEntry(label=menu_key("B", "ack"), brief="Leave it for later"),
        ],
        width=session.terminal_width, height=session.terminal_height,
        description_level=menu_description_level(db, user),
    ))
    await write_prompt(session, "Choice: ")
    while True:
        choice = (await session.read_key()).lower()
        if choice == "r":
            await session.write_line("")
            return "resume"
        if choice == "d":
            await session.write_line("")
            delete_draft(draft_path)
            announce(session, "Drawing deleted.", tone="muted")
            return "discard"
        if choice == "b":
            await session.write_line("")
            return "back"
        await session.write(reject_unhandled_key(choice))


def _recovered_drawing(draft_path: Path) -> str:
    """The drawing an art draft holds, as a body."""
    return art_body_from_editor(draft_path.read_bytes())


async def _draw_body(
    session: Session, db: Database, user: User, *, initial_text: str | None, draft_path: Path
) -> str | None:
    """An art post's body, drawn (or redrawn) in the ANSI art editor
    (issue #711): a canvas as wide as the terminal allows, up to 80
    columns, and as tall as it allows. `None` when the caller quits
    without saving or saves an empty canvas -- or when the editor cannot
    open: a terminal below the editor's minimum, or one too small for the
    drawing being revised, which the canvas would cut (Codex review on
    #753)."""
    # The signature block under a drawing is not part of the canvas: it is
    # set aside and put back as it was, so it neither takes canvas rows nor
    # goes through the canvas's trimming (Codex review on #753).
    signature_block = ""
    if initial_text:
        # The editor's canvas holds text and the styles it can keep:
        # untrusted controls in a carried body are filtered as for a reader,
        # tabs are one column as a reader sees them (Codex review on #753).
        initial_text, signature_block = split_signature(initial_text)
        initial_text = styled_post_body(initial_text, pipe_codes=False).replace("\t", " ")
    # A saved draft is the caller's to offer (`_art_draft_choice`), so the
    # editor never asks -- or discards one on a bare Enter (Codex review on
    # #753). A resumed draft arrives as `initial_text`, checked like any.
    canvas = _art_canvas(session, initial_text)
    if canvas is None:
        return None
    width, height = canvas
    initial_bytes = initial_text.replace("\n", "\r\n").encode("utf-8") if initial_text else None
    data = await edit_ansi_art(
        session,
        initial_bytes=initial_bytes,
        draft_path=draft_path,
        width=width,
        height=height,
        redraw_in_place=redraw_in_place_enabled(db, user),
        unicode_style=unicode_style_enabled(db, user),
        collapsed=breadcrumb_collapsed_enabled(db, user),
        offer_recovery=False,
    )
    if data is None:
        return None
    drawn = art_body_from_editor(data)
    if not drawn:
        return None
    return f"{drawn}\x1b[0m{signature_block}" if signature_block else drawn


def _editor_header(session: Session, db: Database, board: Board, title: str, subject: str) -> EditorHeader:
    """What the fullscreen editor shows above a post (issue #813): the
    composition, the board it goes to and its subject."""
    return EditorHeader(
        title, (("Board", board.name), ("Subject", subject)), color=effective_header_color(session, db),
    )


async def _compose_body(
    session: Session,
    db: Database,
    user: User,
    *,
    initial_text: str | None = None,
    draft_path: Path,
    keep_pasted_color: bool = False,
    cursor_at_end: bool = False,
    header: EditorHeader | None = None,
    offer_recovery: bool = True,
) -> str | None:
    """The single place a post body (or an edit of one) is actually
    entered: the fullscreen prose editor if `user` has opted in,
    otherwise the shared logical-line editor. Both paths accept
    `initial_text`, return a complete draft, and return `None` for
    either an explicit cancel (draft deleted) or an explicit save-and-
    leave (draft kept -- issue #149, see `edit_line_body`'s/
    `edit_prose`'s own docstrings) -- `draft_path.exists()` after a
    `None` return tells the two apart. Neither path persists a real
    post itself.

    `keep_pasted_color` is the board's "Color in posts" setting (issue
    #754): where pipe codes are color, pasted color is typed in as them;
    where they are text, it is dropped rather than left as ``|04``.

    `offer_recovery` False: `initial_text` already is the saved draft
    (issue #814), so neither editor asks about it again."""
    if fullscreen_editor_enabled(db, user):
        return await edit_prose(
            session, initial_text=initial_text, draft_path=draft_path, max_bytes=MAX_BODY_BYTES,
            unicode_style=unicode_style_enabled(db, user), keep_pasted_color=keep_pasted_color,
            cursor_at_end=cursor_at_end, header=header, offer_recovery=offer_recovery,
        )
    return await edit_line_body(
        session,
        initial_text=initial_text,
        max_bytes=MAX_BODY_BYTES,
        max_lines=_MAX_PLAIN_POST_LINES,
        draft_path=draft_path,
        keep_pasted_color=keep_pasted_color,
        offer_recovery=offer_recovery,
    )


async def _withdraw_existing_post(
    session: Session, db: Database, board: Board, post: Post, user: User, *, link_context: LinkContext | None
) -> bool:
    """[W]ithdraw (issue #675): the author replaces their post's text with
    `WITHDRAWN_PLACEHOLDER`, after saying yes. An ordinary author edit, so
    it is carried like one and can be edited again. Returns whether the
    post was withdrawn."""
    # Says what withdrawing does and does not do (issue #675): the text is
    # hidden, not deleted -- other nodes keep the signed original, and a
    # moderator can still read it in the post's history.
    await session.write_line(colored(
        f"Its text will read {WITHDRAWN_PLACEHOLDER} here and on every node that carries the board. "
        "This hides it but does not delete it: a moderator can still read it, and you can edit the "
        "post again later.",
        fg_color=MUTED_COLOR,
    ))
    if not await prompt_yes_no(session, "Withdraw this post?", default=False):
        announce(session, "Post not withdrawn.", tone="muted")
        return False
    try:
        withdrawn = withdraw_post(db, post, board, withdrawn_by=user)
    except PostError as exc:
        announce(session, f"Could not withdraw: {exc}.", tone="error")
        return False
    sent = (
        queue_board_post_edit_if_linked(db, withdrawn, board, node_identity=link_context.node_identity, edited_by=user)
        if link_context is not None else None
    )
    if is_board_linked(db, board) and sent is None:
        # The post's local chain has a gap the Link cannot extend (an edit
        # made while Link was off), so other nodes keep the old text. Said,
        # not hidden behind "withdrawn" (Codex review on #789).
        announce(
            session,
            "Post withdrawn here, but it could not be sent to other nodes: they keep showing the old text.",
            tone="error",
        )
        return True
    announce(session, "Post withdrawn.")
    return True


async def _show_history(
    session: Session,
    db: Database,
    board: Board,
    post: Post,
    user: User,
    *,
    breadcrumb: tuple[str, ...],
    name_requirement: str | None,
    body_mode: str,
    truecolor: bool,
    redraw_in_place: bool,
    unicode_style: bool,
    collapsed: bool,
    separator: str,
) -> None:
    """[H]istory (issue #675): the versions of `post`, newest first, each
    opened read-only on the same reader a post is read on. For moderators
    only (`list_post_revisions`)."""
    try:
        revisions = list_post_revisions(db, post, board, requesting_user=user)
    except PostError as exc:
        announce(session, f"Not available: {exc}.", tone="error")
        return
    removed = post.tombstoned_at is not None
    # A removed post's one version is what it said before it was removed,
    # which is what a moderator opens it for (claude review on #789).
    if len(revisions) < (1 if removed else 2):
        announce(session, "There are no earlier versions of this post to show.", tone="muted")
        return
    newest_first = list(reversed(revisions))
    labels = {
        id(revision): _revision_label(revision, index, len(revisions), removed=removed)
        for index, revision in enumerate(revisions)
    }

    def _when(revision) -> str:
        return format_for_display(revision.post.created_at, db)

    while True:
        chosen = await pick_item(
            session, newest_first,
            name_of=_when,
            stable_id_of=lambda revision: newest_first.index(revision) + 1,
            description_of=lambda revision: labels[id(revision)],
            title=f"Versions of {sanitize_text(post.subject)}",
            empty_message="No versions to show.",
            redraw_in_place=redraw_in_place,
            unicode_style=unicode_style,
            collapsed=collapsed,
            accent_color=effective_accent_color(session, db),
            header_color=effective_header_color(session, db),
        )
        if chosen is None:
            return
        version = chosen.post
        byline = [
            colored(separator, fg_color=METADATA_COLOR).join([
                _author_display_name(db, post, name_requirement=name_requirement),
                colored(_when(chosen), fg_color=METADATA_COLOR),
                badge(labels[id(chosen)]),
            ])
        ]
        title = screen_title(
            sanitize_text(version.subject),
            breadcrumb=(*breadcrumb, "Versions"),
            width=session.terminal_width,
            clear=False,
            unicode_style=unicode_style, collapsed=collapsed,
            header_color=effective_header_color(session, db),
            node_name_gradient=session.node_name_gradient,
        )
        page = 0
        while True:
            key, page = await show_detail(
                session,
                title=title,
                sections=[Section(None, [Styled(post_body_rows(
                    version.body, post_body_width(session, post.layout), body_mode, truecolor=truecolor,
                    layout=post.layout,
                ))])],
                actions=[("b", menu_key("B", "ack"))],
                redraw_in_place=redraw_in_place,
                unicode_style=unicode_style,
                page=page,
                preamble=byline,
            )
            if key == "b":
                break


def _revision_label(revision, index: int, count: int, *, removed: bool = False) -> str:
    """What one version is: the one shown now, the one first posted, or
    an edit -- and whether a moderator made it. A removed post has no
    current version: what is shown now is the placeholder."""
    if index == count - 1 and not removed:
        kind = "current, withdrawn" if revision.post.withdrawn else "current"
    elif revision.post.withdrawn:
        kind = "withdrawn"
    elif revision.post.post_id == revision.post.root_post_id:
        kind = "original"
    else:
        kind = "edit"
    return f"{kind}, by a moderator" if revision.by_moderator else kind


def _reply_target(db: Database, post: Post, board: Board) -> Post | None:
    """`post` as a reader would find it now, or `None` when a reply to it
    must not be written: expired, pending, hidden by trust, or removed.

    Expiry is swept first: it is applied lazily, by the reads that show a
    board, and a post can pass its age while its reader is open (Codex
    review on #786)."""
    sweep_expired_posts(db, board)
    current = visible_post(db, post.root_post_id)
    if current is None or current.tombstoned_at is not None:
        return None
    return current


def _post_author_mail_target(
    db: Database, post: Post, user: User, *, link_context: LinkContext | None,
) -> tuple[User | None, str | None] | None:
    """Whom `[M]ail author` writes to (issue #821): `(account, None)` for
    a post written here, `(None, "user@<fingerprint>")` for one carried
    from another BBS -- the author's stable Link address, checked when the
    key is pressed -- or `None` when there is nobody to write to: the
    caller's own post, a deleted account, an author here who has blocked
    the caller (issue #953, the letter would be refused; the reader has no
    line to say so, so the key is simply not offered), and a carried post
    while this node has Link off. A carried post's author is offered even
    so: their node's block list is not visible here."""
    if post.author_user_id is not None:
        account = get_user_by_id(db, post.author_user_id)
        if account is None or account.id == user.id:
            return None
        if mail_blocked_notice(db, account, sender=user) is not None:
            return None
        return account, None
    if link_context is None:
        return None
    if split_link_address(post.author_label) is None:
        return None
    return None, post.author_label


def _reply_quote(db: Database, post: Post, board: Board, *, name_requirement: str | None) -> str:
    """`post` quoted for a reply (issue #675): its text as a reader with
    color off sees it -- where pipe codes are color they are dropped, where
    they are text they stay -- under "<author> wrote:". An art post is a
    drawing, and a quote of it is not text anyone can answer, so it quotes
    nothing."""
    if post.layout == "art":
        return ""
    text = plain_post_body(post.body) if board.allow_color else post_body_text(post.body)
    author = strip_ansi(_author_display_name(db, post, name_requirement=name_requirement))
    return quote_body(text, author=author)


def _author_display_name(db: Database, post: Post, *, name_requirement: str | None) -> str:
    """
    The author label to render for one post (design doc §18). Only
    looks up the live account behind `post.author_label`
    when this board actually requires `verified_and_displayed` names --
    that's the one case where showing the *current* attested real name
    is intentional (an attestation, like an age gate, is a living fact
    re-evaluated at read time, not frozen at post time). Every other
    case renders the plain, already-sanitized `author_label` exactly as
    it always has: `author_label` is deliberately denormalized so a
    post's history still reads correctly even if the account is later
    renamed or removed (design doc) -- substituting a user's
    *current* `display_name` there for the ordinary case would quietly
    break that property, since `display_name` (unlike `username`) is
    actually mutable.

    The one resolution that *is* applied: a Link-carried post's
    `user@<home-node-fingerprint>` label is presented by the home node's
    current friendly identity (`present_link_author_label`) -- the
    fingerprint stays in persistence, the presentation follows renames.
    """
    if name_requirement == "verified_and_displayed":
        author = get_user_by_id(db, post.author_user_id)
        if author is not None:
            return format_name_for_resource(db, author, name_requirement=name_requirement)
        # A Link-carried post has no local account, so the attested name has
        # to come from the attestation its home node signed and this node
        # accepted (design doc §5.5, issue #584). Same `(=...=)` unit, same
        # resource scoping -- the value appears only inside a board that
        # requires it, never on an unrelated screen.
        subject = _remote_author_subject(post.author_label)
        if subject is not None:
            return format_remote_name_for_resource(
                db, subject,
                sanitize_text(present_link_author_label(db, post.author_label)),
                name_requirement=name_requirement,
            )
    return sanitize_text(present_link_author_label(db, post.author_label))


def _remote_author_subject(author_label: str) -> TrustSubject | None:
    """The Link identity behind a carried post's `user@fingerprint` label.

    `None` for a local label, which has no `@` -- and for a malformed one,
    since a label this node cannot resolve to a stable Link identity is
    exactly a label whose attested name it must not go looking for.
    """
    local_user_id, separator, home = author_label.rpartition("@")
    if not separator or not local_user_id or not home:
        return None
    return TrustSubject.user(home, local_user_id)


def _post_byline(
    db: Database, post: Post, *, name_requirement: str | None, is_new: bool, separator: str,
    width: int, held: str | None = None,
) -> list[str]:
    """The reader's lines under the subject: who, when, and what state the
    post is in -- edited, new to this caller -- and then which post it
    answers, on a row of its own cut to `width`: a parent's subject runs to
    `MAX_SUBJECT_BYTES`, and wrapped it would take the rows the body is
    paged into (Codex review on #719)."""
    parts = [
        _author_display_name(db, post, name_requirement=name_requirement),
        colored(format_for_display(post.created_at, db), fg_color=METADATA_COLOR),
    ]
    if post.is_edited:
        parts.append(badge("edited"))
    if post.pinned:
        parts.append(badge("pinned"))
    if post.exempt_from_expiry:
        parts.append(badge("kept"))
    if is_new:
        parts.append(badge("new", tone="success"))
    # The caller's own post, or an edit of theirs, awaiting a moderator
    # (issue #678).
    if held == "post":
        parts.append(badge("awaiting approval", tone="warning"))
    elif held == "edit":
        parts.append(badge("your edit awaits approval", tone="warning"))
    if post.parent_post_id is not None:
        # As the feed shows the parent now: its current subject, and nothing
        # at all for a parent that is expired, pending or trust-hidden.
        parent = visible_post(db, post.parent_post_id)
        if parent is not None:
            reply = f'reply to "{sanitize_text(parent.subject)}"'
            if display_width(reply) > width - 1:
                reply = cut_to_width(reply, max(1, width - 4)) + "..."
            return [
                colored(separator, fg_color=METADATA_COLOR).join(parts),
                colored(reply, fg_color=METADATA_COLOR),
            ]
    return [colored(separator, fg_color=METADATA_COLOR).join(parts)]
