"""SysOp console moderation queue.

Held posts and pending files, reviewed from the staff menu, the board and
file-area screens and the caller-side queue. Split out of
`netbbs.net.admin_flow` (issue #1311).
"""

from __future__ import annotations

import datetime
import dataclasses
from dataclasses import dataclass
from typing import Any

from netbbs.auth.users import User
from netbbs.boards.boards import Board, list_boards
from netbbs.boards.moderation_notices import submitter_id
from netbbs.boards.posts import (
    Post,
    PostError,
    approve_post,
    delete_post,
    get_post,
    list_node_pending_posts,
    list_pending_posts,
    revision_for_moderation,
    set_post_exempt,
    set_post_pinned,
    shown_post_refs,
)
from netbbs.files.areas import FileArea, list_file_areas
from netbbs.files.diz import MAX_DESCRIPTION_LINES
from netbbs.files.entries import (
    FileEntry,
    FileEntryError,
    approve_file,
    delete_file,
    list_node_pending_files,
    list_pending_files,
    set_file_exempt,
    set_file_pinned,
)
from netbbs.link.boards import LinkContext, queue_approved_board_post_if_linked
from netbbs.ftn.scanner import export_post_if_ftn
from netbbs.qwknet.exchange import queue_post_if_qwknet
from netbbs.link.files import queue_file_descriptor_if_linked
from netbbs.staff import moderation_scope
from netbbs.moderation.roles import BoardPermission, has_permission
from netbbs.net.char_input import HELP_KEY, reject_unhandled_key
from netbbs.net.help_overlay import show_menu_help
from netbbs.net.location import crumbs, located
from netbbs.net.picker import ListColumn
from netbbs.net.session import Session, post_body_width, write_prompt
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.color_depth_preference import effective_truecolor
from netbbs.net.post_color_preference import post_colors_enabled
from netbbs.net.node_theme import effective_accent_color_256, effective_header_color_256
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.rendering import (
    AUTHOR_COLOR,
    DATE_COLOR,
    HEADER_COLOR,
    METADATA_COLOR,
    MUTED_COLOR,
    VALUE_COLOR,
    WARNING_COLOR,
    MenuEntry,
    colored,
    menu_key,
    sanitize_text,
    screen_title,
)
from netbbs.rendering.detail import Field, Section, Styled
from netbbs.file_refs import open_ref, post_refs
from netbbs.net.file_ref_view import ref_rows
from netbbs.rendering.post_body import post_body_mode, post_body_rows
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import (
    _parse_stored_timestamp,
    format_for_display,
    resolve_display_preferences,
)
from netbbs.net.admin_trust import _help_entries
from netbbs.net.admin_ui import (
    _announce,
    _announce_line,
    _choice_prompt,
    _fitted_menu,
    _format_bytes,
    _write_sections,
    _yes_no,
    pick_item,
    show_detail,
)


@dataclass(frozen=True)
class _PendingItem:
    """One row of a moderation queue (issue #678): a held post, reply or
    edit, or a held upload, with what a moderator scans the queue for."""

    stable_id: int
    title: str
    kind: str
    where: str
    author: str
    when: str
    post: Post | None = None
    board: Board | None = None
    entry: FileEntry | None = None
    area: FileArea | None = None


_PENDING_COLUMNS = [
    ListColumn("kind", 5, VALUE_COLOR),
    ListColumn("by", 16, AUTHOR_COLOR),
    ListColumn("submitted", 16, DATE_COLOR),
]


# The node-wide queue says where each item waits as well, in narrower
# columns: at 80 columns the subject still needs room to be read.
_PENDING_REVIEW_COLUMNS = [
    ListColumn("where", 12, VALUE_COLOR),
    ListColumn("kind", 5, VALUE_COLOR),
    ListColumn("by", 12, AUTHOR_COLOR),
    ListColumn("submitted", 16, DATE_COLOR),
]


# How many held items a queue screen lists at once, oldest first. Held
# content can arrive from other nodes without end; the rest is listed as
# these are decided (Codex review on #795).
MAX_QUEUE_ITEMS = 200


def _when_or_raw(iso_timestamp: str, db: Database | None = None, **overrides) -> str:
    """A submission time as the node shows times, or the stored text when
    it cannot be shown: a carried item's time is its origin's, and one bad
    value must not keep the SysOp from the queue that could reject it
    (Codex review on #795)."""
    try:
        return format_for_display(iso_timestamp, db, **overrides)
    except ValueError:
        return sanitize_text(iso_timestamp)


def _submitted_instant(iso_timestamp: str) -> datetime.datetime:
    """Oldest first by instant, not by text: a carried time may carry its
    own offset (Codex review on #795). An unreadable one sorts last."""
    try:
        return _parse_stored_timestamp(iso_timestamp)
    except ValueError:
        return datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)


def _submitter_label(db: Database, post: Post) -> str:
    """Who submitted a held post. A revision keeps its root's author, but a
    moderator may have made the edit (Codex review on #795): the moderation
    log names the editor."""
    if post.post_id != post.root_post_id:
        editor_id = submitter_id(db, post)
        if editor_id is not None and editor_id != post.author_user_id:
            row = db.connection.execute("SELECT username FROM users WHERE id = ?", (editor_id,)).fetchone()
            if row is not None:
                return row["username"]
    return post.author_label


def _held_or_current(db: Database, post_id: str | None) -> Post | None:
    """The post `post_id` belongs to as its readers see it -- or, when none
    of it is approved yet, the exact revision `post_id` names as it waits
    (Codex review on #795): a carried post can arrive with its replies and
    edits on a board this node moderates, all of it held."""
    if post_id is None:
        return None
    current = revision_for_moderation(db, post_id)
    if current is not None:
        return current
    try:
        held = get_post(db, post_id)
    except PostError:
        return None
    return held if held.status == "pending" else None


def _reply_parent_for_moderation(db: Database, post: Post) -> Post | None:
    """The post a held reply answers (see `_held_or_current`)."""
    return _held_or_current(db, post.parent_post_id)


def _edit_base_for_moderation(db: Database, post: Post) -> Post | None:
    """What a held edit would replace (see `_held_or_current`): the post's
    current text, or, when nothing of it is approved yet, the held
    revision this edit amends."""
    return _held_or_current(db, post.edit_of_post_id or post.root_post_id)


def _pending_post_kind(post: Post) -> str:
    """"edit" for a held revision of an approved post, "reply" for a held
    answer to another, else "post"."""
    if post.post_id != post.root_post_id:
        return "edit"
    return "reply" if post.parent_post_id is not None else "post"


def _load_pending_items(
    db: Database, actor: User, *, boards: list[Board] | None = None, areas: list[FileArea] | None = None
) -> tuple[list[_PendingItem], bool]:
    """Everything `actor` may decide on in `boards` and `areas` (every
    board and area when both are `None`), oldest first, at most
    `MAX_QUEUE_ITEMS` of it, and whether more waits. Each item keeps its
    own id as its picker identity; in the node-wide queue, where a post and a file
    may share an id, a file's is negative, as category pickers do
    (worklog, "Stable identity and pagination")."""
    node_wide = boards is None and areas is None
    cap = MAX_QUEUE_ITEMS + 1
    posts: list[tuple[Post, Board]] = []
    entries: list[tuple[FileEntry, FileArea]] = []
    scope = moderation_scope(db, actor) if node_wide else None
    if scope is not None:
        # A moderator's `Approvals (n)` queue (design doc §5.2): one queue
        # across what their grants cover, drawn like the SysOp's.
        boards, areas = scope
    if node_wide and scope is None:
        # One bounded query each, however many boards and areas there are
        # (Codex review on #795).
        boards_by_id = {board.id: board for board in list_boards(db)}
        areas_by_id = {area.id: area for area in list_file_areas(db)}
        posts = [
            (post, boards_by_id[post.board_id])
            for post in list_node_pending_posts(db, requesting_user=actor, limit=cap)
            if post.board_id in boards_by_id
        ]
        entries = [
            (entry, areas_by_id[entry.area_id])
            for entry in list_node_pending_files(db, requesting_user=actor, limit=cap)
            if entry.area_id in areas_by_id
        ]
    else:
        for board in boards or []:
            posts += [(post, board) for post in list_pending_posts(db, board, requesting_user=actor, limit=cap)]
        for area in areas or []:
            entries += [(entry, area) for entry in list_pending_files(db, area, requesting_user=actor, limit=cap)]
    more = len(posts) > MAX_QUEUE_ITEMS or len(entries) > MAX_QUEUE_ITEMS
    items: list[_PendingItem] = []
    for post, board in posts:
        items.append(_PendingItem(
            stable_id=post.id, title=post.subject, kind=_pending_post_kind(post), where=board.name,
            author=_submitter_label(db, post), when=_when_or_raw(post.created_at, db), post=post, board=board,
        ))
    for entry, area in entries:
        items.append(_PendingItem(
            stable_id=entry.id, title=entry.filename, kind="file", where=area.name,
            author=entry.uploader_label, when=_when_or_raw(entry.created_at, db), entry=entry, area=area,
        ))
    items.sort(key=lambda item: (
        _submitted_instant((item.post or item.entry).created_at), item.entry is not None, item.stable_id,
    ))
    more = more or len(items) > MAX_QUEUE_ITEMS
    items = items[:MAX_QUEUE_ITEMS]
    if node_wide:
        items = [dataclasses.replace(item, stable_id=-item.stable_id) if item.entry else item for item in items]
    return items, more


async def _pick_pending_item(
    session: Session, lane: DatabaseLane, actor: User, loaded: tuple[list[_PendingItem], bool], *,
    title: str, empty_message: str, node_wide: bool = False,
) -> _PendingItem | None:
    items, more = loaded
    return await pick_item(
        session, items,
        masthead=colored(
            f"The oldest {MAX_QUEUE_ITEMS} are listed; the rest follow as these are decided.",
            fg_color=MUTED_COLOR,
        ) if more else "",
        name_of=lambda item: item.title,
        stable_id_of=lambda item: item.stable_id,
        # What a terminal too narrow for the table shows instead.
        description_of=lambda item: (
            f"{item.where}: {item.kind} by {item.author}, {item.when}" if node_wide
            else f"{item.kind} by {item.author}, {item.when}"
        ),
        columns=_PENDING_REVIEW_COLUMNS if node_wide else _PENDING_COLUMNS,
        column_values_of=lambda item: (
            [item.where, item.kind, item.author, item.when] if node_wide else [item.kind, item.author, item.when]
        ),
        title=title, own=True,
        help_about=(
            "Posts, edits and uploads waiting for approval, oldest first; callers see none of them "
            "yet. Picking one shows it in full, with approve and reject."
        ),
        empty_message=empty_message,
        redraw_in_place=await lane.run(redraw_in_place_enabled, actor),
        unicode_style=await lane.run(unicode_style_enabled, actor),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, actor),
        accent_color=await lane.run(effective_accent_color_256),
        header_color=await lane.run(effective_header_color_256),
    )


@located("Pending review")
async def _pending_review_screen(
    session: Session, lane: DatabaseLane, actor: User, *,
    link_context: LinkContext | None = None, transfers: Any = None,
) -> None:
    """Every held post and upload on the node in one queue (issue #678),
    so a SysOp need not open each board and area to find what waits -- or,
    for anyone else, every one their grants let them decide (issue #836)."""
    while True:
        loaded = await lane.run(_load_pending_items, actor)
        selected = await _pick_pending_item(
            session, lane, actor, loaded, title="Pending review", empty_message="Nothing awaits review.",
            node_wide=True,
        )
        if selected is None:
            return
        if selected.post is not None:
            await _post_action_screen(session, lane, actor, selected.post, selected.board, link_context=link_context)
        else:
            await _file_action_screen(
                session, lane, actor, selected.entry, selected.area, link_context=link_context, transfers=transfers,
            )


async def moderation_queue(
    session: Session, lane: DatabaseLane, actor: User, *,
    link_context: LinkContext | None = None, transfers: Any = None,
) -> None:
    """`Approvals (n)` on the main menu (design doc §5.2, issue #836): the
    node-wide queue, narrowed by `_load_pending_items` to the boards and
    areas `actor` approves on -- one queue, not a visit to each board."""
    await _pending_review_screen(session, lane, actor, link_context=link_context, transfers=transfers)


@located("Pending posts")
async def _pending_posts_screen(
    session: Session, lane: DatabaseLane, actor: User, board: Board, *, link_context: LinkContext | None = None
) -> None:
    while True:
        loaded = await lane.run(lambda db: _load_pending_items(db, actor, boards=[board]))
        selected = await _pick_pending_item(
            session, lane, actor, loaded, title=f"Pending posts in {board.name!r}", empty_message="No pending posts.",
        )
        if selected is None:
            return
        await _post_action_screen(session, lane, actor, selected.post, board, link_context=link_context)


async def _post_action_screen(
    session: Session,
    lane: DatabaseLane,
    actor: User,
    post: Post,
    board: Board,
    *,
    link_context: LinkContext | None = None,
) -> None:
    """One pending post, on the same paged reader a caller reads posts with
    (issue #679): the facts and the action bar stay on screen while a long
    body pages under them, which matters most here -- a moderator reads the
    whole post before approving it."""
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    display_format, display_timezone = await lane.run(resolve_display_preferences)
    when = _when_or_raw(post.created_at, override_format=display_format, override_timezone=display_timezone)
    body_mode = post_body_mode(
        board_allows_color=board.allow_color, reader_wants_color=await lane.run(post_colors_enabled, actor)
    )
    truecolor = await lane.run(lambda db: effective_truecolor(session, db, actor))
    # What the held post changes or answers, as readers see it now (issue
    # #678): an edit is judged against the text it would replace.
    kind = _pending_post_kind(post)
    current, parent, submitter = await lane.run(lambda db: (
        _edit_base_for_moderation(db, post) if kind == "edit" else None,
        _reply_parent_for_moderation(db, post) if kind == "reply" else None,
        _submitter_label(db, post),
    ))
    # An edit made before the one readers now see (Codex review on #795):
    # approving it changes nothing they see, and the screen must say so
    # rather than offer it as a replacement.
    superseded = kind == "edit" and await lane.run(lambda db: (db.connection.execute(
        "SELECT MAX(id) FROM posts WHERE root_post_id = ? AND board_id = ? AND status = 'approved'",
        (post.root_post_id, post.board_id),
    ).fetchone()[0] or 0) > post.id)
    # The files the held post or edit points at (issue #842), as the
    # moderator finds them, and an edit's current ones where it changes them.
    accent = await lane.run(effective_accent_color_256)
    proposed_files, current_files = await lane.run(lambda db: (
        [open_ref(db, actor, ref) for ref in post_refs(db, post.post_id)],
        [open_ref(db, actor, ref) for ref in shown_post_refs(db, current)] if current is not None else [],
    ))
    # Pinning and keeping take EDIT, which an approver need not hold.
    can_flag = await lane.run(lambda db: has_permission(
        db, actor, object_type="board", object_id=board.id, permission=BoardPermission.EDIT
    ))
    actions = [
        ("a", menu_key("A", "pprove")),
        ("r", menu_key("R", "eject")),
        *([("p", menu_key("P", "in toggle")), ("x", menu_key("X", "empt toggle"))] if can_flag else []),
        ("b", menu_key("B", "ack")),
    ]
    page = 0
    while True:
        title = screen_title(
            sanitize_text(post.subject),
            breadcrumb=crumbs(session), width=session.terminal_width, clear=False,
            unicode_style=unicode_style, collapsed=collapsed,
            header_color=header_color, node_name_gradient=session.node_name_gradient,
        )
        # As the board's readers will see it, in every mode (issue #711).
        body_rows = post_body_rows(
            post.body, post_body_width(session, post.layout), body_mode, truecolor=truecolor, layout=post.layout
        )
        # What the moderator is deciding about, then the post itself under its
        # own heading, with the pin and exempt state the toggles change.
        facts = [
            # Which board it waits on (Codex review on #795): reached from
            # the node-wide queue, the list that said so is gone.
            Field("Board", board.name),
            Field("By", submitter, color=AUTHOR_COLOR),
            Field("Posted", when, color=DATE_COLOR),
            Field("Pinned", _yes_no(post.pinned)),
            Field("Exempt from expiry", _yes_no(post.exempt_from_expiry)),
        ]
        if parent is not None:
            facts.append(Field("Reply to", sanitize_text(parent.subject) + {
                "expired": " (expired)", "pending": " (awaiting approval)",
            }.get(parent.status, "")))
        if current is not None and current.subject != post.subject:
            facts.append(Field("Current subject", sanitize_text(current.subject)))
        if superseded:
            facts.append(Field(
                "Superseded", "a later edit is already approved; approving this one changes nothing readers see",
                color=WARNING_COLOR,
            ))
        sections = [
            Section(f"Pending {kind}", facts, paired=not superseded),
            Section("Proposed text" if current is not None else "Message", [Styled(body_rows)]),
        ]
        files_changed = [item.ref for item in proposed_files] != [item.ref for item in current_files]
        if (current is None and proposed_files) or (current is not None and files_changed):
            # "none" when a held edit drops every file (review on #913): the
            # moderator must see that approving it removes them.
            sections.append(Section(
                "Proposed files" if current is not None else "Files",
                [Styled(ref_rows(proposed_files, accent=accent)[1:] or ["none"])],
            ))
        if current is not None and files_changed:
            sections.append(Section(
                "Current files",
                [Styled(ref_rows(current_files, accent=accent)[1:] or ["none"])],
            ))
        if current is not None:
            sections.append(Section(
                {"expired": "Current text (expired)", "pending": "Current text (awaiting approval)"}.get(
                    current.status, "Current text"
                ),
                [Styled(post_body_rows(
                    current.body, post_body_width(session, current.layout), body_mode, truecolor=truecolor,
                    layout=current.layout,
                ))],
            ))
        choice, page = await show_detail(
            session, title=title, sections=sections, actions=actions,
            redraw_in_place=redraw_in_place, unicode_style=unicode_style, page=page,
            help_title="Held post help",
            help_about=(
                "A post or edit held for moderation, shown as readers will see it; for an edit, the "
                "current text follows."
            ),
            help_entries=_help_entries(actions, {
                "a": "Publish it; on to Link, FTN, QWKnet if carried",
                "r": "Delete it; the author is mailed your reason",
                "p": "Keep it at the top of the board, or not",
                "x": "Never expire it, or let it expire again",
            }),
        )
        if choice == "b":
            return
        if choice == "a":
            try:
                approved = await lane.run(approve_post, post, approved_by=actor)
            except PostError as exc:
                _announce(session, f"Error: {exc}", error=True)
                continue
            if link_context is not None:
                await lane.run(
                    queue_approved_board_post_if_linked, approved, board, node_identity=link_context.node_identity
                )
            await lane.run(export_post_if_ftn, approved, board)
            await lane.run(queue_post_if_qwknet, approved, board)  # issue #1481
            _announce_line(session, "Approved.")
            return
        if choice == "r":
            # The last step before an irreversible action; Enter leaves the
            # reason out. It goes to the author with the rejection (#678).
            await session.write_line("")
            await write_prompt(session, "Reason for the author (optional, Enter for none): ")
            reason = (await session.read_line()).strip() or None
            try:
                await lane.run(delete_post, post, deleted_by=actor, reason=reason)
            except PostError as exc:
                _announce(session, f"Error: {exc}", error=True)
                continue
            _announce_line(session, "Rejected.")
            return
        # Refused, not raised, when the post was removed or its board hidden
        # meanwhile -- the same as approve and reject just above.
        try:
            if choice == "p":
                post = await lane.run(set_post_pinned, post, not post.pinned, changed_by=actor)
            else:
                post = await lane.run(set_post_exempt, post, not post.exempt_from_expiry, changed_by=actor)
        except PostError as exc:
            _announce(session, f"Error: {exc}", error=True)


@located("Pending files")
async def _pending_files_screen(
    session: Session, lane: DatabaseLane, actor: User, area: FileArea, *,
    link_context: LinkContext | None = None, transfers: Any = None,
) -> None:
    while True:
        loaded = await lane.run(lambda db: _load_pending_items(db, actor, areas=[area]))
        selected = await _pick_pending_item(
            session, lane, actor, loaded, title=f"Pending files in {area.name!r}", empty_message="No pending files.",
        )
        if selected is None:
            return
        await _file_action_screen(
            session, lane, actor, selected.entry, area, link_context=link_context, transfers=transfers,
        )


async def _write_file_record(
    session: Session, entry: FileEntry, *,
    heading: str,
    fields: list[Field],
    redraw_in_place: bool,
    unicode_style: bool,
    collapsed: bool,
    header_color: int | tuple[int, int, int],
) -> int:
    """Title, the file's panel and its description -- what
    the pending review and the expired-file recovery screens both show
    above their action bars. Returns the rows used, for `_fitted_menu`."""
    await session.write_line(
        "\r\n" + screen_title(sanitize_text(entry.filename),
            breadcrumb=crumbs(session), width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            header_color=header_color, node_name_gradient=session.node_name_gradient)
    )
    panel_rows = await _write_sections(session, [Section(heading, fields)], unicode_style=unicode_style)
    # Line by line (issue #463): a description may be a FILE_ID.DIZ
    # block now, and running its ten lines together into one is exactly
    # what a moderator deciding whether to approve the upload should not
    # be shown. Same cap the file listing applies, for the same reason.
    description_lines = (entry.description or "").splitlines()[:MAX_DESCRIPTION_LINES]
    await session.write_line("")
    await session.write_line(colored("DESCRIPTION", fg_color=METADATA_COLOR, bold=True))
    for description_line in description_lines or ["(none)"]:
        await session.write_line(
            colored(sanitize_text(description_line), fg_color=VALUE_COLOR if description_lines else MUTED_COLOR)
        )
    return panel_rows + 6 + max(1, len(description_lines))


async def _draw_file_action(
    session: Session, entry: FileEntry, description_level: str, redraw_in_place: bool,
    unicode_style: bool,
    collapsed: bool,
    header_color: int | tuple[int, int, int] = HEADER_COLOR,
    *,
    when: str,
    can_download: bool = False,
    area_name: str | None = None,
    can_flag: bool = True,
) -> None:
    # Which area it waits in (Codex review on #795): reached from the
    # node-wide queue, the list that said so is gone.
    area_field = [Field("Area", area_name)] if area_name is not None else []
    used_rows = await _write_file_record(
        session, entry, heading="Pending file", fields=[
            *area_field,
            Field("By", entry.uploader_label, color=AUTHOR_COLOR),
            Field("Uploaded", when, color=DATE_COLOR),
            Field("Size", f"{_format_bytes(entry.size_bytes)} ({entry.size_bytes} bytes)"),
            Field("SHA-256", entry.sha256, color=METADATA_COLOR),
            Field("Pinned", _yes_no(entry.pinned)),
            Field("Exempt from expiry", _yes_no(entry.exempt_from_expiry)),
        ],
        redraw_in_place=redraw_in_place, unicode_style=unicode_style,
        collapsed=collapsed, header_color=header_color,
    )
    entries = _file_action_entries(can_download=can_download, can_flag=can_flag)
    options = _fitted_menu(entries, description_level, session=session, used_rows=used_rows)
    await session.write_line(f"\r\n{options}")
    await _choice_prompt(session)


def _file_action_entries(*, can_download: bool, can_flag: bool) -> list[MenuEntry]:
    """The pending-file screen's menu, shared by its draw and its help."""
    entries = [
        MenuEntry(label=menu_key("A", "pprove"), brief="Publish this pending file"),
        MenuEntry(label=menu_key("R", "eject"), brief="Delete this pending file"),
    ]
    if can_download:
        # A moderator can read the upload's FILE_ID.DIZ above, but
        # this screen asks them to approve the *bytes*. The file
        # area's own listing carries approved rows only, so while
        # `/download <filename>` existed a pending upload was
        # inspected by typing its name there; nothing else reaches
        # one.
        entries.append(
            MenuEntry(label=menu_key("D", "ownload"), brief="Fetch it before deciding")
        )
    if can_flag:
        entries += [
            MenuEntry(label=menu_key("P", "in toggle"), brief="Toggle showing at the top"),
            MenuEntry(label=menu_key("X", "empt toggle"), brief="Toggle exempt from expiry"),
        ]
    entries.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to the pending list"))
    return entries


async def _file_action_screen(
    session: Session, lane: DatabaseLane, actor: User, entry: FileEntry, area: FileArea, *,
    link_context: LinkContext | None = None, transfers: Any = None,
) -> None:
    """Act on one pending upload. Approving it is where a Linked area's
    catalogue entry is signed and queued (issue #464) -- the file-area
    counterpart of `_post_action_screen`'s own
    `queue_approved_board_post_if_linked` call, and the moderated half of the
    split `netbbs.net.file_flow._handle_upload` documents: a pending
    upload is never announced, an approved one always is."""
    description_level = await lane.run(menu_description_level, actor)
    unicode_style = await lane.run(unicode_style_enabled, actor)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, actor)
    redraw_in_place = await lane.run(redraw_in_place_enabled, actor)
    header_color = await lane.run(effective_header_color_256)
    # As on the pending-post screen: pinning or keeping takes EDIT, which an
    # approver need not hold.
    can_flag = await lane.run(lambda db: has_permission(
        db, actor, object_type="file_area", object_id=area.id, permission=BoardPermission.EDIT
    ))
    display_format, display_timezone = await lane.run(resolve_display_preferences)
    when = _when_or_raw(entry.created_at, override_format=display_format, override_timezone=display_timezone)
    # Imported here rather than at module scope: this is the only
    # place `admin_flow` reaches into the caller-facing file screens,
    # and `door_profile_flow` above sets the same local-import
    # precedent for exactly that reason.
    from netbbs.net.file_flow import send_file_to_caller, supports_zmodem

    # Either the terminal can carry a Zmodem send, or the node can mint
    # a browser link -- `send_file_to_caller` picks between them exactly
    # as the caller-facing screen does (issue #475), so the key is
    # offered only when one of the two can actually happen.
    can_download = supports_zmodem(session) or transfers is not None

    async def _draw() -> None:
        await _draw_file_action(
            session, entry, description_level, redraw_in_place, unicode_style, collapsed, header_color,
            when=when, can_download=can_download, area_name=area.name,
            can_flag=can_flag,
        )

    await _draw()
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "d" and can_download:
            await session.write_line("")
            # `send_file_to_caller` announces its own outcome -- "Sent
            # 'x.zip'.", a failure, or the browser link the moderator is
            # meant to open (`netbbs.net.notices`, issue #680) -- so the
            # redraw below shows it. Its setup lines (the heading, "Starting
            # Zmodem send") are progress, not outcomes, and are meant to be
            # cleared: no stand-in session is needed to hold them.
            await send_file_to_caller(session, lane, area, entry, actor, transfers=transfers)
            await _draw()
        elif choice == "a":
            await session.write_line("")
            try:
                approved = await lane.run(approve_file, entry, approved_by=actor)
            except FileEntryError as exc:
                _announce(session, f"Error: {exc}", error=True)
                await _draw()
                continue
            if link_context is not None:
                await lane.run(
                    queue_file_descriptor_if_linked, approved, area,
                    node_identity=link_context.node_identity,
                )
            _announce_line(session, "Approved.")
            return
        elif choice == "r":
            await session.write_line("")
            try:
                await lane.run(delete_file, entry, deleted_by=actor)
            except FileEntryError as exc:
                _announce(session, f"Error: {exc}", error=True)
                await _draw()
                continue
            _announce_line(session, "Rejected.")
            return
        elif choice in ("p", "x") and can_flag:
            await session.write_line("")
            # Refused, not raised, when the file is gone or its area hidden
            # meanwhile -- the same as approve and reject just above.
            try:
                if choice == "p":
                    entry = await lane.run(set_file_pinned, entry, not entry.pinned, changed_by=actor)
                else:
                    entry = await lane.run(set_file_exempt, entry, not entry.exempt_from_expiry, changed_by=actor)
            except FileEntryError as exc:
                _announce(session, f"Error: {exc}", error=True)
            await _draw()
        elif choice == HELP_KEY:
            await show_menu_help(
                session, "Pending file help", _file_action_entries(can_download=can_download, can_flag=can_flag),
                about="An upload waiting for approval. Callers see it in the area only once it is "
                      "approved, which also offers it to Link nodes if the area is Linked. Rejecting "
                      "removes it from the area for good.",
                header_color=header_color, unicode_style=unicode_style,
            )
            await _draw()
        else:
            await session.write(reject_unhandled_key(choice))
