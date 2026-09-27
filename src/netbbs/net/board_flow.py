"""
Message-board browsing and posting: `[B]oards` (and every route into it
-- `[C]ommunities`, `[U]ncategorized`, `[J]ump to...`, `[N]ew scan`,
`[F]ind`), one bounded page of posts at a time (design doc, issue #10),
composing/editing/tombstoning a post, and quoted-reply rendering.

Split out of `netbbs.net.login_flow` (that module's own maintenance
split -- see its module docstring): the largest single concern pulled
out of that file, but a genuinely self-contained one -- nothing here
calls back into `login_flow` itself, only outward into shared
preference/rendering/domain modules. `_show_board` is this module's own
main entry point from elsewhere in the split (the main menu, `[N]ew
scan`, `[F]ind` search-hit selection) -- extracted before those other
pieces specifically so they could import it cleanly from here rather
than from `login_flow` (which will, once every other screen group is
also split out, hold only session-entry/auth logic).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from netbbs.activity import (
    ensure_board_baseline,
    mark_board_read,
    record_post_opened,
    unread_post_count,
    unread_post_ids,
)
from netbbs.attestation import format_name_for_resource, meets_age, meets_name_requirement
from netbbs.auth.users import User, get_user_by_id
from netbbs.boards import (
    MAX_BODY_BYTES,
    Board,
    Post,
    PostError,
    PostPage,
    create_post,
    edit_post,
    list_boards,
    list_posts_page,
    tombstone_post,
    visible_post,
)
from netbbs.boards.categories import Category, list_subcategories, list_top_level_categories
from netbbs.boards.categories import get_category_by_id as get_board_category_by_id
from netbbs.communities import (
    get_community,
    get_effective_min_age,
    get_effective_min_read_level,
    get_effective_min_write_level,
    get_effective_name_requirement,
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
from netbbs.net.board_list_banner import load_board_list_banner
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.chat_flow import NAME_GATE_NOTE
from netbbs.net.char_input import HELP_KEY, REDRAW_KEY, EditorKey, EditorKeyKind, reject_unhandled_key
from netbbs.net.color_depth_preference import effective_truecolor
from netbbs.net.composition import ReviewAction, edit_line_body, read_prefilled_field, review_composition
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.detail_view import show_detail
from netbbs.net.draft_storage import delete_draft, drafts_directory, load_draft
from netbbs.net.editor_preference import fullscreen_editor_enabled
from netbbs.net.help_overlay import show_help
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.node_theme import effective_accent_color, effective_header_color, effective_header_color_256
from netbbs.net.notices import announce, pending_notice_rows, take_notices, write_notices
from netbbs.net.picker import ListColumn, pick_item
from netbbs.net.prose_editor import edit_prose
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.session import Session, write_prompt
from netbbs.net.sort_ui import SORT_MODE_LABELS, prompt_sort_change
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.permissions import meets_level
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
from netbbs.rendering.detail import Section, Styled
from netbbs.rendering.reflow import wrap_terminal_text
from netbbs.rendering.width import cut_to_width, display_width, wrap_to_width
from netbbs.signature import append_signature, get_signature
from netbbs.sort_preferences import get_effective_sort_mode, set_sort_preference
from netbbs.storage.database import Database
from netbbs.timeutil import format_for_display

_MAX_PLAIN_POST_LINES = 200


# The board list's table (issue #679): activity is fixed-width so it can be
# scanned down the page; "about" is the flexible last column.
_BOARD_LIST_COLUMNS = [
    ListColumn("activity", len("not visited yet"), MUTED_COLOR),
    ListColumn("about", 30, MUTED_COLOR),
]


async def _browse_boards(
    session: Session,
    db: Database,
    user: User,
    *,
    community_id: int | None = None,
    community_scoped: bool = False,
    title_prefix: str | None = None,
    link_context: LinkContext | None = None,
) -> None:
    """Entry point: browse from the top level (no category selected yet)."""
    await _browse_boards_in_category(
        session, db, user, category_id=None,
        community_id=community_id, community_scoped=community_scoped, title_prefix=title_prefix,
        link_context=link_context,
    )


def _has_visible_boards(db: Database, user: User, *, community_id: int | None, community_scoped: bool) -> bool:
    """Whether `user` can see at least one board under the given
    Community filter -- backs the shared resource-type sub-menu's
    "only offer what currently applies" conditional visibility (design
    doc §16), same convention as `[I]nvitations`."""
    boards = [
        b for b in list_boards(db)
        if meets_level(user, get_effective_min_read_level(db, b)) and meets_age(db, user, get_effective_min_age(db, b))
    ]
    if community_scoped:
        boards = [b for b in boards if b.community_id == community_id]
    return bool(boards)


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
    1) — mixed into one picker call, that would make `goto` ambiguous
    between two different things sharing the same displayed number.
    Disambiguated by negating category IDs for picker purposes only
    (`-item.id`) — boards keep their real, positive ID unchanged, so
    existing board `goto` numbers aren't affected by this at all.

    `community_id`/`community_scoped` (design doc §16) narrow
    browsing to one Community's boards (`community_scoped=True`,
    `community_id=X`), Uncategorized boards (`community_scoped=True`,
    `community_id=None` -- `board.community_id == None` filters
    identically to the real-Community case, no special-casing needed),
    or no filter at all (`community_scoped=False`, the default --
    every existing caller's unchanged behavior, and what `[J]ump to...`
    uses). `title_prefix`, threaded alongside, is `None` for the
    unfiltered/Jump case (keeping today's unchanged "Available message
    boards" title) or a human label ("Uncategorized", a Community's own
    name) that's passed to `pick_item` as an ancestor `breadcrumb`
    segment otherwise, so it renders muted with only "Message boards"
    itself in the current-location color -- not folded into the title
    text as a fake, uniformly-colored breadcrumb (dogfood-reported bug,
    see `pick_item`'s own `breadcrumb` docstring).
    Category leak prevention ("only show/offer categories
    currently used by ≥1 resource in this Community") only applies when
    `community_scoped` -- the unfiltered Jump path shows every category
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
    # category, a Community/Uncategorized scope), not only the very
    # first unfiltered screen, matching this feature's own scoping
    # decision.
    board_masthead = load_board_list_banner(db)

    def _load(order_by: str) -> tuple[list[Board], list[Category]]:
        all_boards = [
            b for b in list_boards(db, order_by=order_by)
            if meets_level(user, get_effective_min_read_level(db, b))
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
    boards_here, categories_here = _load(current_mode)
    category_name = get_board_category_by_id(db, category_id).name if category_id is not None else None
    # Where the caller came from, carried onto the board's own screens
    # (issue #679): the Community (or "Uncategorized") and the category.
    # Continues the path this picker shows: the Community (or
    # "Uncategorized") is above "Message boards", a category below it.
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
            category_id=category_id, category_name=category_name,
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
        )
        if board is not None:
            await _show_board(session, db, board, user, link_context=link_context, breadcrumb=board_breadcrumb)
        return

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
    )
    if selected is None:
        return

    if isinstance(selected, Category):
        await _browse_boards_in_category(
            session, db, user, category_id=selected.id,
            community_id=community_id, community_scoped=community_scoped, title_prefix=title_prefix,
            link_context=link_context,
        )
    else:
        await _show_board(session, db, selected, user, link_context=link_context, breadcrumb=board_breadcrumb)


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
    if not meets_level(user, write_level):
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
    available = width - number_width - len(_NEW_MARKER) - date_width - _ROW_FURNITURE
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
            marker = f"{_NEW_MARKER} " if post.id in new_ids else ""
            # The date goes first when the row is this narrow: subject and
            # author say which post it is, and the reader shows the date.
            plain = cut_to_width(f"{index + 1:>{number_width}} {marker}{subject} -- {author}", width - 1)
            rows.append(
                colored(plain, reverse=True) if index == highlighted
                else colored(plain, fg_color=MUTED_COLOR if post.tombstoned_at else None)
            )
        return rows
    subject_width, author_width, date_width = widths
    marker_width = len(_NEW_MARKER)
    for index, (post, (subject, author, when)) in enumerate(zip(posts, cells)):
        number = f"{index + 1:>{number_width}}"
        subject_cell = _pad(cut_to_width(subject, subject_width), subject_width)
        marker_cell = _pad(_NEW_MARKER if post.id in new_ids else "", marker_width)
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
            + colored(subject_cell, fg_color=MUTED_COLOR if post.tombstoned_at else None)
            + "  "
            + colored(marker_cell, fg_color=SUCCESS_COLOR, bold=True)
            + "  "
            + colored(author_cell, fg_color=METADATA_COLOR)
            + "  "
            + colored(date_cell, fg_color=METADATA_COLOR)
        )
    return rows


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
    marker_width = len(_NEW_MARKER)
    text = (
        f"  {'#':>{number_width}}  {_pad('Subject', subject_width)}  {' ' * marker_width}  "
        f"{_pad('Author', author_width)}  {_pad('Posted', date_width)}"
    )
    return colored(text.rstrip(), fg_color=LABEL_COLOR, bold=True)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def _list_options(
    page: PostPage, *, can_post: bool, has_draft: bool, row_count: int, has_unread: bool
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
    if has_draft:
        options.append(_DRAFT_MENU_ENTRY)
    if has_unread:
        options.append(MenuEntry(label=menu_key("M", "ark all read"), brief="Count every post here as read"))
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
    "D            resume or discard a saved draft",
    "M            count every post on this board as read",
    "Ctrl-L       redraw the list",
    "B            back to the list of boards",
    "",
    "Reading a post: Edit, Remove, Next and Previous post live there,",
    "and PgUp/PgDn page a long post. A post counts as read once you",
    "open it; the list marks the ones you have not opened as new.",
]


_SAVED_DRAFT_NOTICE = "You have a saved post draft for this message board from an earlier session."
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


async def _show_board(
    session: Session,
    db: Database,
    board: Board,
    user: User,
    *,
    link_context: LinkContext | None = None,
    initial_cursor: tuple[str, str] | None = None,
    breadcrumb: tuple[str, ...] = ("Message boards",),
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
        and meets_level(user, get_effective_min_write_level(db, board))
        and meets_age(db, user, get_effective_min_age(db, board))
        and meets_name_requirement(db, user, get_effective_name_requirement(db, board))
    )
    description_level = menu_description_level(db, user)
    redraw_in_place = redraw_in_place_enabled(db, user)
    unicode_style = unicode_style_enabled(db, user)
    collapsed = breadcrumb_collapsed_enabled(db, user)
    truecolor = effective_truecolor(session, db, user)
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

    def _new_ids(current_page: PostPage) -> set[int]:
        return unread_post_ids(db, user, board, current_page.posts)

    def _frame(current_page: PostPage, *, row_count: int | None = None) -> tuple[str, str]:
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
            has_unread=unread["menu"],
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
        above, below = _frame(PostPage(posts=[], has_older=True, has_newer=True), row_count=9)
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
        lines.append(rule)
        lines.extend(_post_list_rows(
            db, current_page.posts, width=width, highlighted=highlighted,
            new_ids=_new_ids(current_page), name_requirement=name_requirement,
            accent=effective_accent_color(session, db),
        ))
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
            return list_posts_page(db, board, user, limit=rows)
        mode, cursor = page_anchor
        return list_posts_page(db, board, user, limit=rows, **{mode: cursor})

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
            byline = _post_byline(
                db, post, name_requirement=name_requirement, is_new=was_new,
                separator=separator, width=width,
            )
            body_rows = _render_quoted_body(sanitize_text(post.body, allow_newlines=True), width).split("\r\n")
            has_previous = index > 0 or page.has_older
            has_next = index < len(page.posts) - 1 or page.has_newer
            actions = []
            if _can_edit_post(db, post, user):
                actions.append(("e", menu_key("E", "dit")))
            if _can_tombstone_post(db, post, user):
                actions.append(("t", menu_key("t", prefix="Remove pos")))
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
            if key in ("e", "t"):
                root = post.root_post_id
                if key == "e":
                    await _edit_existing_post(session, db, board, post, user, link_context=link_context)
                else:
                    await _tombstone_existing_post(session, db, board, post, user, link_context=link_context)
                # The same number of rows as the page the reader is on: an
                # outcome notice now pending takes a row from a fresh budget,
                # and a page one post shorter could drop the post just acted
                # on (Codex review on #719).
                page = _refetch_current_page(limit=len(page.posts))
                if not page.posts:
                    page_anchor = None
                    page = _refetch_current_page()
                    return None
                index = next(
                    (i for i, p in enumerate(page.posts) if p.root_post_id == root),
                    min(index, len(page.posts) - 1),
                )
                continue
            detail_page = 0
            if key == "n":
                if index < len(page.posts) - 1:
                    index += 1
                    continue
                newest = page.posts[-1]
                page_anchor = ("after", (newest.created_at, newest.post_id))
                page = _refetch_current_page()
                index = 0
            else:
                if index > 0:
                    index -= 1
                    continue
                oldest = page.posts[0]
                page_anchor = ("before", (oldest.created_at, oldest.post_id))
                page = _refetch_current_page()
                index = len(page.posts) - 1
            if not page.posts:
                # Emptied from under the caller (a post removed, a trust
                # change): back to the newest page rather than a blank one.
                page_anchor = None
                page = _refetch_current_page()
                return None

    async def _compose_new_post(*, initial_body: str | None = None) -> None:
        # `[P]ost` is a hotkey followed straight by a line prompt: an Enter
        # typed right behind it ("P<Enter>") would otherwise be read as a
        # blank subject and cancel the post. Same guard as mail's compose.
        discard_buffered_enter = getattr(session, "discard_buffered_enter", None)
        if discard_buffered_enter is not None:
            await discard_buffered_enter()
        await session.write("\r\nSubject (or press Enter to cancel): ")
        subject = (await session.read_line()).strip()
        if not subject:
            announce(session, "Post cancelled.", tone="muted")
            return
        draft_path = _post_draft_path(db, kind="new", board=board, user=user)
        body = await _compose_body(session, db, user, initial_text=initial_body, draft_path=draft_path)
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
                announce(
                    session, "Draft saved -- you'll be offered it next time you visit this message board.", tone="muted"
                )
            else:
                announce(session, "Post cancelled.", tone="muted")
            return
        async def _publish(subject: str, body: str) -> bool:
            try:
                post = create_post(db, board, user, subject, body)
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
                # A moderated board holds the post back; the page the
                # caller returns to lists approved posts only, so
                # "Posted" would describe a post they cannot find.
                announce(session, "Submitted. It will appear once a moderator approves it.")
            else:
                announce(session, "Posted.")
            return True

        await _review_and_commit(
            session, db, user, subject=subject, body=body, draft_path=draft_path,
            commit_key="p", commit_label="ost", commit_brief="Publish this post",
            cancelled_notice="Post cancelled.",
            draft_saved_notice="Draft saved -- you'll be offered it next time you visit this message board.",
            commit=_publish,
        )

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
                # Consumed here, before _compose_new_post ever opens an
                # editor against the same draft_path -- otherwise that
                # editor's own crash-recovery check would immediately
                # offer to "resume" the very draft this menu just handed
                # off, a redundant second prompt for the same file.
                delete_draft(draft_path)
                await _compose_new_post(initial_body=saved_text)
                return True
            await session.write(reject_unhandled_key(choice))

    page_anchor: tuple[str, tuple[str, str]] | None = ("after", initial_cursor) if initial_cursor else None
    page = (
        list_posts_page(db, board, user, after=initial_cursor, limit=_page_limit())
        if initial_cursor else list_posts_page(db, board, user, limit=_page_limit())
    )
    if initial_cursor and not page.posts:
        # Nothing newer than the cursor `[N]ew scan` jumped in with --
        # the user is caught up, not looking at a genuinely empty board.
        # Fall back to the ordinary newest-page view rather than the
        # "has no posts yet" path below, which would falsely claim the
        # board is empty and (worse) prompt to compose the first post.
        page_anchor = None
        page = list_posts_page(db, board, user, limit=_page_limit())
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
            if has_draft:
                options.append(_DRAFT_MENU_ENTRY)
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
            if (choice == "p" and can_post) or (choice == "d" and has_draft):
                await session.write_line("")
                await _saved_draft_menu(from_post=choice == "p")
                page = list_posts_page(db, board, user, limit=_page_limit())
                if page.posts:
                    # A post was actually created (not cancelled) --
                    # fall through to the ordinary render+navigation
                    # loop below, same post-then-refresh behavior the
                    # non-empty case's own [P]ost option already has.
                    break
                has_draft = await _draw_empty_board()
                continue
            await session.write(reject_unhandled_key(choice))

    # A [N]ew scan or [F]ind jump opens the list with its target at the top;
    # the cursor starts on it, so Enter reads what the caller came for.
    highlighted: int | None = 0 if page_anchor is not None else None
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
        elif char == "o" and page.has_older:
            await _moved_on()
            oldest = page.posts[0]
            page_anchor = ("before", (oldest.created_at, oldest.post_id))
            page = _refetch_current_page()
            highlighted = None
            await _render_fresh(page)
        elif char == "n" and page.has_newer:
            await _moved_on()
            newest = page.posts[-1]
            page_anchor = ("after", (newest.created_at, newest.post_id))
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

    subject = await read_prefilled_field(session, "Subject", post.subject)

    edit_draft_path = _post_draft_path(
        db, kind="edit", board=board, user=user, root_post_id=post.root_post_id
    )
    draft_saved_notice = "Draft saved -- you'll be offered it next time you edit this post."
    body = await _compose_body(session, db, user, initial_text=post.body, draft_path=edit_draft_path)
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

    async def _save(subject: str, body: str) -> bool:
        if subject == post.subject and body == post.body:
            announce(session, "No changes to save.", tone="muted")
            return True
        try:
            edited = edit_post(db, post, board, subject=subject, body=body, edited_by=user)
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
        session, db, user, subject=subject, body=body, draft_path=edit_draft_path,
        commit_key="s", commit_label="ave", commit_brief="Save this edit",
        cancelled_notice="Edit cancelled.",
        draft_saved_notice=draft_saved_notice,
        commit=_save,
    )


async def _review_and_commit(
    session: Session,
    db: Database,
    user: User,
    *,
    subject: str,
    body: str,
    draft_path: Path,
    commit_key: str,
    commit_label: str,
    commit_brief: str,
    cancelled_notice: str,
    draft_saved_notice: str,
    commit: Callable[[str, str], Awaitable[bool]],
) -> None:
    """The review screen a new post and an edit both pass through
    before anything is stored: the draft is shown whole, its subject and
    body can be revised, and `commit(subject, body)` persists it.

    `commit` returns whether the composition is finished. A `False`
    (the domain refused it -- a subject over the byte cap, a board
    closed meanwhile) keeps the caller in review with the text intact:
    the editor deleted its draft when it handed the body back, so this
    loop is the only copy left."""
    while True:
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
        )
        if action is ReviewAction.CANCEL:
            announce(session, cancelled_notice, tone="muted")
            return
        if action is ReviewAction.EDIT_SUBJECT:
            subject = await read_prefilled_field(session, "Subject", subject)
            continue
        if action is ReviewAction.EDIT_BODY:
            revised = await _compose_body(session, db, user, initial_text=body, draft_path=draft_path)
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
        if await commit(subject, body):
            return


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


async def _compose_body(
    session: Session, db: Database, user: User, *, initial_text: str | None = None, draft_path: Path
) -> str | None:
    """The single place a post body (or an edit of one) is actually
    entered: the fullscreen prose editor if `user` has opted in,
    otherwise the shared logical-line editor. Both paths accept
    `initial_text`, return a complete draft, and return `None` for
    either an explicit cancel (draft deleted) or an explicit save-and-
    leave (draft kept -- issue #149, see `edit_line_body`'s/
    `edit_prose`'s own docstrings) -- `draft_path.exists()` after a
    `None` return tells the two apart. Neither path persists a real
    post itself."""
    if fullscreen_editor_enabled(db, user):
        return await edit_prose(
            session, initial_text=initial_text, draft_path=draft_path, max_bytes=MAX_BODY_BYTES,
            unicode_style=unicode_style_enabled(db, user),
        )
    return await edit_line_body(
        session,
        initial_text=initial_text,
        max_bytes=MAX_BODY_BYTES,
        max_lines=_MAX_PLAIN_POST_LINES,
        draft_path=draft_path,
    )


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


def _render_quoted_body(body: str, width: int) -> str:
    """Reflow `body`, coloring `>`-quoted lines in `MUTED_COLOR` (issue
    #181). Runs `reflow()` per same-kind run of raw lines, not once over
    the whole body: `reflow()` only paragraph-breaks on a *blank* line,
    and otherwise collapses single line breaks and rewraps -- so a quote
    immediately followed by a reply (no blank line between them, the
    common case) would get merged into one rewrapped line, and a multi-
    line quote's own wrapped continuation lines would lose their leading
    `>` and go uncolored. Each quote run has its `>` prefix stripped,
    gets reflowed as its own paragraph, and has `>` reapplied to every
    wrapped line, so multi-line quotes wrap and color correctly too.

    A blank line is its own third run kind, output verbatim, never
    folded into an adjacent quote/text run's own `reflow()` call --
    a blank separator at a quote/text boundary (`"> quoted\\n\\nreply"`)
    would otherwise join a run's raw lines with a single `\\n`, one
    short of the `\\n\\n` `reflow()` needs to even recognize a paragraph
    break, silently dropping the authored blank line."""
    runs: list[tuple[str, list[str]]] = []
    for raw_line in body.split("\n"):
        stripped_line = raw_line.strip()
        kind = "blank" if not stripped_line else "quote" if stripped_line.startswith(">") else "text"
        if runs and runs[-1][0] == kind:
            runs[-1][1].append(raw_line)
        else:
            runs.append((kind, [raw_line]))

    rendered: list[str] = []
    for kind, raw_lines in runs:
        if kind == "blank":
            rendered.extend(raw_lines)
        elif kind == "quote":
            stripped = [line.split(">", 1)[1].lstrip(" ") for line in raw_lines]
            for wrapped_line in reflow("\n".join(stripped), width=max(1, width - 2)).splitlines():
                rendered.append(colored(f"> {wrapped_line}", fg_color=MUTED_COLOR))
        else:
            rendered.extend(reflow("\n".join(raw_lines), width=width).splitlines())
    return "\r\n".join(rendered)


def _post_byline(
    db: Database, post: Post, *, name_requirement: str | None, is_new: bool, separator: str,
    width: int,
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
    if is_new:
        parts.append(badge("new", tone="success"))
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
