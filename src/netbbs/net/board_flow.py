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

from pathlib import Path

from netbbs.activity import record_board_seen
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
from netbbs.link.node_profiles import present_link_author_label
from netbbs.link.boards import (
    LinkContext,
    queue_board_post_edit_if_linked,
    queue_board_post_if_linked,
    queue_board_post_moderator_edit_if_linked,
    queue_board_post_tombstone_if_linked,
)
from netbbs.moderation import BoardPermission, has_permission
from netbbs.net.board_list_banner import load_board_list_banner
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.char_input import reject_unhandled_key
from netbbs.net.color_depth_preference import effective_truecolor
from netbbs.net.composition import ReviewAction, edit_line_body, review_composition
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.draft_storage import delete_draft, drafts_directory, load_draft
from netbbs.net.editor_preference import fullscreen_editor_enabled
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.node_theme import effective_accent_color, effective_header_color, effective_header_color_256
from netbbs.net.picker import pick_item
from netbbs.net.prose_editor import edit_prose
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.session import Session, write_prompt
from netbbs.net.sort_ui import SORT_MODE_LABELS, prompt_sort_change
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.permissions import meets_level
from netbbs.rendering import (
    METADATA_COLOR,
    MUTED_COLOR,
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
from netbbs.signature import append_signature, get_signature
from netbbs.sort_preferences import get_effective_sort_mode, set_sort_preference
from netbbs.storage.database import Database
from netbbs.timeutil import format_for_display

_MAX_PLAIN_POST_LINES = 200


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
            description_of=lambda b: b.description,
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
            await _show_board(session, db, board, user, link_context=link_context)
        return

    mixed: list[Category | Board] = [*categories_here, *boards_here]

    def render_name(item: Category | Board) -> str:
        return f"[{item.name}]" if isinstance(item, Category) else item.name

    def render_description(item: Category | Board) -> str | None:
        if isinstance(item, Category):
            return item.description or "(category)"
        return item.description

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
        description_of=render_description,
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
        await _show_board(session, db, selected, user, link_context=link_context)


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


async def _render_board_page(
    session: Session,
    db: Database,
    board_name: str,
    page: PostPage,
    user: User,
    *,
    can_post: bool,
    name_requirement: str | None,
    description_level: str,
    redraw_in_place: bool,
    unicode_style: bool = False,
    collapsed: bool = False,
    has_draft: bool = False,
) -> None:
    """Renders one page of posts plus its navigation options — the unit
    that should be redrawn on an actual page change (initial entry,
    Older/Newer/Recent), not on every loop iteration regardless of
    whether anything changed. `has_draft` (issue #282) adds a notice
    line and a `[D]raft` entry for a saved new-post draft, in place of
    the modal "[E]dit it, [D]elete it, or [I]gnore" question that used
    to interrupt every entry to the board before its first post was
    even shown."""
    await _render_post_page(
        session, db, board_name, page, user, name_requirement=name_requirement, redraw_in_place=redraw_in_place,
        unicode_style=unicode_style, collapsed=collapsed,
    )
    if has_draft:
        await session.write_line(colored(f"\r\n{_SAVED_DRAFT_NOTICE}", fg_color=MUTED_COLOR))
    options = []
    if page.has_older:
        options.append(MenuEntry(label=menu_key("O", "lder"), brief="Show older posts"))
    if page.has_newer:
        options.append(MenuEntry(label=menu_key("N", "ewer"), brief="Show newer posts"))
        options.append(MenuEntry(label=menu_key("R", "ecent"), brief="Jump to the newest page"))
    if any(_can_edit_post(db, post, user) for post in page.posts):
        options.append(MenuEntry(label=menu_key("E", "dit"), brief="Edit one of your posts"))
    if any(_can_tombstone_post(db, post, user) for post in page.posts):
        options.append(MenuEntry(label=menu_key("T", "ombstone"), brief="Remove a post, leave a marker"))
    if can_post:
        options.append(MenuEntry(label=menu_key("P", "ost"), brief="Write a new post"))
    if has_draft:
        options.append(_DRAFT_MENU_ENTRY)
    options.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to the previous menu"))
    await session.write_line(
        f"\r\n{menu_row(options, width=session.terminal_width, height=session.terminal_height, description_level=description_level)}"
    )
    await session.write("Choice: ")


_SAVED_DRAFT_NOTICE = "You have a saved post draft for this message board from an earlier session."
_DRAFT_MENU_ENTRY = MenuEntry(label=menu_key("D", "raft"), brief="Resume or discard your saved draft")


async def _show_board(
    session: Session,
    db: Database,
    board: Board,
    user: User,
    *,
    link_context: LinkContext | None = None,
    initial_cursor: tuple[str, str] | None = None,
) -> None:
    """
    Show `board`, one bounded page of posts at a time (design doc,
    issue #10) — never the whole board, however large its history.

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
    can_post = (
        meets_level(user, get_effective_min_write_level(db, board))
        and meets_age(db, user, get_effective_min_age(db, board))
        and meets_name_requirement(db, user, get_effective_name_requirement(db, board))
    )
    description_level = menu_description_level(db, user)
    redraw_in_place = redraw_in_place_enabled(db, user)
    unicode_style = unicode_style_enabled(db, user)
    collapsed = breadcrumb_collapsed_enabled(db, user)
    accent_color = effective_accent_color(session, db)
    header_color = effective_header_color(session, db)

    def _refetch_current_page() -> PostPage:
        """Re-fetches whichever page is currently on screen, using the
        exact cursor that produced it -- not always the newest page.
        Needed after an in-place edit (which never moves a post's feed
        position, see netbbs.boards.posts._resolve_current_version)
        so [E]diting a post doesn't also silently jump the SysOp back
        to page one as an unrelated side effect."""
        if page_anchor is None:
            return list_posts_page(db, board, user)
        mode, cursor = page_anchor
        return list_posts_page(db, board, user, **{mode: cursor})

    async def _render_and_advance_cursor(current_page: PostPage) -> None:
        """The one place every render in this loop funnels through
        (issue #56) -- advances `user`'s board read cursor to whatever
        is now newest on screen. A no-op when the page is empty (the
        empty-board early return above never reaches here at all, but
        an Older/Newer navigation could in principle land on an empty
        result if a page emptied out from under a live session)."""
        await _render_board_page(
            session, db, board_name, current_page, user, can_post=can_post,
            name_requirement=get_effective_name_requirement(db, board),
            description_level=description_level,
            redraw_in_place=redraw_in_place,
            unicode_style=unicode_style,
            collapsed=collapsed,
            has_draft=_has_saved_draft(),
        )
        if current_page.posts:
            record_board_seen(db, user, board, current_page.posts[-1])

    async def _compose_new_post(*, initial_body: str | None = None) -> None:
        await session.write("\r\nSubject (or press Enter to cancel): ")
        subject = (await session.read_line()).strip()
        if not subject:
            await session.write_line(colored("Post cancelled.", fg_color=MUTED_COLOR))
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
                await session.write_line(
                    colored("Draft saved -- you'll be offered it next time you visit this message board.", fg_color=MUTED_COLOR)
                )
            else:
                await session.write_line(colored("Post cancelled.", fg_color=MUTED_COLOR))
            return
        while True:
            action = await review_composition(
                session,
                recipient=None,
                subject=subject,
                body=body,
                commit_key="p",
                commit_label="ost",
                commit_brief="Publish this post",
                description_level=description_level,
                redraw_in_place=redraw_in_place,
                unicode_style=unicode_style,
                collapsed=collapsed,
                accent_color=accent_color,
                header_color=header_color,
                truecolor=effective_truecolor(session, db, user),
            )
            if action is ReviewAction.CANCEL:
                await session.write_line(colored("Post cancelled.", fg_color=MUTED_COLOR))
                return
            if action is ReviewAction.EDIT_SUBJECT:
                await write_prompt(session, f"Subject [{sanitize_text(subject)}] (Enter to keep): ")
                subject = (await session.read_line()).strip() or subject
                continue
            if action is ReviewAction.EDIT_BODY:
                revised = await _compose_body(session, db, user, initial_text=body, draft_path=draft_path)
                if revised is not None:
                    body = revised
                elif draft_path.exists():
                    # /exit or /quit while revising -- issue #149: this
                    # leaves the whole in-progress post as a saved
                    # draft, not just "keep the previous body and stay
                    # in review."
                    await session.write_line(
                        colored(
                            "Draft saved -- you'll be offered it next time you visit this message board.",
                            fg_color=MUTED_COLOR,
                        )
                    )
                    return
                else:
                    await session.write_line(colored("Body unchanged.", fg_color=MUTED_COLOR))
                continue
            try:
                post = create_post(db, board, user, subject, body)
            except PostError as exc:
                await session.write_line(colored(f"Could not create post: {exc}", fg_color=MUTED_COLOR))
                continue
            if link_context is not None:
                queue_board_post_if_linked(db, post, board, node_identity=link_context.node_identity)
            await session.write_line(f"Posted (id {post.post_id[:12]}...).")
            return

    def _has_saved_draft() -> bool:
        # Gated on `can_post` the same way [P]ost itself already is: no
        # point surfacing a draft the caller couldn't act on to post if
        # they resumed it.
        return can_post and _post_draft_path(db, kind="new", board=board, user=user).exists()

    async def _saved_draft_menu() -> None:
        """Issue #149's other half, reshaped by issue #282: the saved
        new-post draft for this exact (user, board) is announced on the
        board page itself and handled behind its own `[D]raft` entry,
        rather than as a modal question fired before the first post was
        rendered on every entry until dealt with. Scoped to `kind="new"`
        only -- an in-progress *edit* of a specific existing post has no
        equally natural board-level moment, so it stays exclusively
        behind the existing recovery-on-reopen path inside
        `_compose_body`."""
        draft_path = _post_draft_path(db, kind="new", board=board, user=user)
        if not draft_path.exists():
            return
        await session.write_line(colored(f"\r\n{_SAVED_DRAFT_NOTICE}", fg_color=MUTED_COLOR))
        await session.write_line(
            menu_row(
                [
                    MenuEntry(label=menu_key("R", "esume"), brief="Open it in the editor"),
                    MenuEntry(label=menu_key("D", "iscard"), brief="Delete the draft"),
                    MenuEntry(label=menu_key("B", "ack"), brief="Leave it for later"),
                ],
                width=session.terminal_width, height=session.terminal_height,
                description_level=description_level,
            )
        )
        await session.write("Choice: ")
        while True:
            choice = (await session.read_key()).lower()
            if choice == "b":
                await session.write_line("")
                return
            if choice == "d":
                delete_draft(draft_path)
                await session.write_line(colored("\r\nDraft deleted.", fg_color=MUTED_COLOR))
                return
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
                return
            await session.write(reject_unhandled_key(choice))

    page_anchor: tuple[str, tuple[str, str]] | None = ("after", initial_cursor) if initial_cursor else None
    page = list_posts_page(db, board, user, after=initial_cursor) if initial_cursor else list_posts_page(db, board, user)
    if initial_cursor and not page.posts:
        # Nothing newer than the cursor `[N]ew scan` jumped in with --
        # the user is caught up, not looking at a genuinely empty board.
        # Fall back to the ordinary newest-page view rather than the
        # "has no posts yet" path below, which would falsely claim the
        # board is empty and (worse) prompt to compose the first post.
        page_anchor = None
        page = list_posts_page(db, board, user)
    if not page.posts:
        # Dogfood report: this used to skip straight to composing the
        # first post whenever the caller could write, with no [P]ost/
        # [B]ack choice first -- the exact same "walked into it" problem
        # issue #39/#40 already fixed for the non-empty case, just never
        # extended to this one. Still skips the full Older/Newer/Edit
        # navigation loop (nothing to browse either way), but offers the
        # same explicit choice before composing anything.
        header_color = effective_header_color_256(db)
        await session.write_line(
            f"\r\n{screen_title(board_name, breadcrumb=(session.node_display_name, 'Message boards'), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed, header_color=header_color, node_name_gradient=session.node_name_gradient)}"
        )
        await session.write_line(
            f"\r\n{empty_state('This message board has no posts yet', detail='It is ready for its first conversation.', width=session.terminal_width, header_color=header_color)}"
        )
        if not can_post:
            return
        while True:
            has_draft = _has_saved_draft()
            if has_draft:
                await session.write_line(colored(f"\r\n{_SAVED_DRAFT_NOTICE}", fg_color=MUTED_COLOR))
            options = [MenuEntry(label=menu_key("P", "ost"), brief="Write the first post")]
            if has_draft:
                options.append(_DRAFT_MENU_ENTRY)
            options.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to the previous menu"))
            await session.write_line(
                "\r\n" + menu_row(
                    options, width=session.terminal_width, height=session.terminal_height,
                    description_level=description_level,
                )
            )
            await session.write("Choice: ")
            choice = (await session.read_key()).lower()
            if choice == "b":
                await session.write_line("")
                return
            if choice in ("p", "d") and (choice == "p" or has_draft):
                await session.write_line("")
                if choice == "p":
                    await _compose_new_post()
                else:
                    await _saved_draft_menu()
                page = list_posts_page(db, board, user)
                if page.posts:
                    # A post was actually created (not cancelled) --
                    # fall through to the ordinary render+navigation
                    # loop below, same post-then-refresh behavior the
                    # non-empty case's own [P]ost option already has.
                    break
                continue
            await session.write(reject_unhandled_key(choice))

    await _render_and_advance_cursor(page)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "o" and page.has_older:
            await session.write_line("")
            oldest = page.posts[0]
            page_anchor = ("before", (oldest.created_at, oldest.post_id))
            page = _refetch_current_page()
            await _render_and_advance_cursor(page)
        elif choice == "n" and page.has_newer:
            await session.write_line("")
            newest = page.posts[-1]
            page_anchor = ("after", (newest.created_at, newest.post_id))
            page = _refetch_current_page()
            await _render_and_advance_cursor(page)
        elif choice == "r" and page.has_newer:
            await session.write_line("")
            page_anchor = None
            page = _refetch_current_page()
            await _render_and_advance_cursor(page)
        elif choice == "e" and any(_can_edit_post(db, post, user) for post in page.posts):
            await session.write_line("")
            await _edit_existing_post(session, db, board, page, user, link_context=link_context)
            page = _refetch_current_page()
            await _render_and_advance_cursor(page)
        elif choice == "t" and any(_can_tombstone_post(db, post, user) for post in page.posts):
            await session.write_line("")
            await _tombstone_existing_post(session, db, board, page, user, link_context=link_context)
            page = _refetch_current_page()
            await _render_and_advance_cursor(page)
        elif choice == "p" and can_post:
            await session.write_line("")
            await _compose_new_post()
            page_anchor = None  # a freshly-created post always lands on the newest page
            page = _refetch_current_page()
            await _render_and_advance_cursor(page)
        elif choice == "d" and _has_saved_draft():
            await session.write_line("")
            await _saved_draft_menu()
            page_anchor = None  # a resumed-and-posted draft lands on the newest page too
            page = _refetch_current_page()
            await _render_and_advance_cursor(page)
        elif choice == "b":
            await session.write_line("")
            return
        else:
            await session.write(reject_unhandled_key(choice))


async def _edit_existing_post(
    session: Session,
    db: Database,
    board: Board,
    page: PostPage,
    user: User,
    *,
    link_context: LinkContext | None = None,
) -> None:
    """
    Edit one of the posts currently on screen -- selected by the
    page-relative `[N]` position `_render_post_page` prints next to
    each one, since a board page is at most 5 posts, too small to
    justify pulling in the real picker (`netbbs.net.picker.pick_item`)
    just to choose one (design doc).

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
    await write_prompt(session, f"Edit which post number [1-{len(page.posts)}]? ")
    choice = (await session.read_key()).strip()
    if not choice.isdigit() or not (1 <= int(choice) <= len(page.posts)):
        await session.write_line(colored("\r\nNot a valid post number.", fg_color=MUTED_COLOR))
        return
    post = page.posts[int(choice) - 1]
    await session.write_line("")

    if not _can_edit_post(db, post, user):
        await session.write_line(colored("You can't edit that post.", fg_color=MUTED_COLOR))
        return

    await write_prompt(session, f"Subject [{post.subject}] (Enter to keep): ")
    subject = (await session.read_line()).strip() or post.subject

    edit_draft_path = _post_draft_path(
        db, kind="edit", board=board, user=user, root_post_id=post.root_post_id
    )
    body = await _compose_body(session, db, user, initial_text=post.body, draft_path=edit_draft_path)
    if body is None:
        # Issue #149: /exit or /quit leaves this revision's draft on
        # disk instead of deleting it -- same distinguishing check as
        # _compose_new_post's own. There's no board-entry prompt for an
        # in-progress *edit* (see _offer_saved_draft_if_any's own
        # docstring for why), so it's only ever resurfaced by picking
        # [E]dit on this same post again.
        if edit_draft_path.exists():
            await session.write_line(
                colored(
                    "Draft saved -- you'll be offered it next time you edit this post.", fg_color=MUTED_COLOR
                )
            )
        else:
            await session.write_line(colored("Edit cancelled.", fg_color=MUTED_COLOR))
        return

    try:
        edited = edit_post(db, post, board, subject=subject, body=body, edited_by=user)
    except PostError as exc:
        await session.write_line(colored(f"Could not save edit: {exc}", fg_color=MUTED_COLOR))
        return
    if link_context is not None:
        queue_board_post_edit_if_linked(db, edited, board, node_identity=link_context.node_identity, edited_by=user)
        queue_board_post_moderator_edit_if_linked(
            db, edited, board, node_identity=link_context.node_identity, edited_by=user
        )
    await session.write_line("Post updated.")


async def _tombstone_existing_post(
    session: Session,
    db: Database,
    board: Board,
    page: PostPage,
    user: User,
    *,
    link_context: LinkContext | None = None,
) -> None:
    """
    `[T]ombstone` one of the posts currently on screen (design doc §9.5,
    issue #88) -- selected the same page-relative way `_edit_existing_
    post` already is. Redacts the post to a placeholder revision
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
    await write_prompt(session, f"Tombstone which post number [1-{len(page.posts)}]? ")
    choice = (await session.read_key()).strip()
    if not choice.isdigit() or not (1 <= int(choice) <= len(page.posts)):
        await session.write_line(colored("\r\nNot a valid post number.", fg_color=MUTED_COLOR))
        return
    post = page.posts[int(choice) - 1]
    await session.write_line("")

    if not _can_tombstone_post(db, post, user):
        await session.write_line(colored("You can't tombstone that post.", fg_color=MUTED_COLOR))
        return

    if not await prompt_yes_no(session, "Redact this post? This cannot be undone.", default=False):
        await session.write_line(colored("Cancelled.", fg_color=MUTED_COLOR))
        return

    try:
        tombstoned = tombstone_post(db, post, board, tombstoned_by=user)
    except PostError as exc:
        await session.write_line(colored(f"Could not tombstone: {exc}", fg_color=MUTED_COLOR))
        return
    if link_context is not None:
        queue_board_post_tombstone_if_linked(db, tombstoned, board, node_identity=link_context.node_identity)
    await session.write_line("Post tombstoned.")


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
    return sanitize_text(present_link_author_label(db, post.author_label))


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


async def _render_post_page(
    session: Session,
    db: Database,
    board_name: str,
    page: PostPage,
    user: User,
    *,
    name_requirement: str | None,
    redraw_in_place: bool = False,
    unicode_style: bool = False,
    collapsed: bool = False,
) -> None:
    header = screen_title(
        board_name,
        breadcrumb=(session.node_display_name, "Message boards"),
        subtitle=f"{len(page.posts)} post{'s' if len(page.posts) != 1 else ''} on this page",
        width=session.terminal_width,
        clear=redraw_in_place,
        unicode_style=unicode_style, collapsed=collapsed,
        header_color=effective_header_color(session, db),
    node_name_gradient=session.node_name_gradient)
    await session.write_line(f"\r\n{header}")
    accent = effective_accent_color(session, db)
    for position, post in enumerate(page.posts, start=1):
        if position > 1:
            rule_char = "─" if unicode_style else "-"
            divider_color = 238 if effective_truecolor(session, db, user) else MUTED_COLOR
            await session.write_line(colored(rule_char * min(session.terminal_width, 78), fg_color=divider_color))
        when = format_for_display(post.created_at, db)
        edited_marker = f" {badge('edited')}" if post.is_edited else ""
        author_display = _author_display_name(db, post, name_requirement=name_requirement)
        # Position numbers are 1-indexed *within this page only* -- not
        # a stable identity across page changes, purely a same-screen
        # selector for [E]dit (design doc -- prose editor:
        # editing an existing post), the same "how do you pick one item
        # currently on screen" role a picker's page-relative numbering
        # already plays elsewhere, just inline here since a board page
        # is at most 5 posts, too small to need a real picker for it.
        #
        # Built from three separately-colored segments, not one
        # colored() call wrapping the whole line -- author_display may
        # already contain its own colored+reset unit (the
        # verified-name formatting), and nesting that inside a single
        # outer colored() would have the inner segment's own reset code
        # clear the outer ACCENT_COLOR early, leaving the trailing
        # "(timestamp)" text in the terminal's default color instead.
        post_header = (
            colored(f"[{position}] {sanitize_text(post.subject)} -- ", fg_color=accent)
            + author_display
            + colored(f" ({when})", fg_color=METADATA_COLOR)
            + edited_marker
        )
        await session.write_line(f"\r\n{post_header}")
        # Reflowed to this specific session's actual detected width
        # (NAWS-negotiated, or the 80-column default — see
        # netbbs.net.session.Session.terminal_width), not a fixed
        # assumption, per the design doc's "must degrade gracefully
        # above 40x24 minimum" requirement. Sanitized *before* reflow,
        # not after — textwrap's width math counts raw characters, so a
        # stray control byte would also throw off wrapping, not just be
        # a display-safety concern. allow_newlines=True: a post body is
        # genuinely multi-line content (paragraph breaks), unlike the
        # single-line fields above -- see sanitize_text's docstring.
        body = sanitize_text(post.body, allow_newlines=True)
        await session.write_line(_render_quoted_body(body, session.terminal_width))
