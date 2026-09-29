"""
`[N]ew scan` and `[/] Find` (issue #56): a caller's unread-activity summary
across every board/channel/file area they can currently access, and a
local free-text search over approved post/file/retained-chat content.

Split out of `netbbs.net.login_flow` (that module's own maintenance
split -- see its module docstring), the smallest and most self-
contained piece of it: both screens are reached only from the main
menu, call nothing else in `login_flow` (only `netbbs.net.board_flow.
_show_board`, already its own module by the time this was extracted),
and share two small module-private types plus a couple of search-
result formatting helpers that exist only to serve them.
"""

from __future__ import annotations

from dataclasses import dataclass

from netbbs.activity import (
    board_read_cursor,
    file_area_read_cursor,
    follow,
    is_following,
    unfollow,
    mark_board_read,
    unread_channel_count,
    unread_file_count,
    unread_post_count,
    unread_replies_to,
)
from netbbs.attestation import meets_age
from netbbs.auth.users import User
from netbbs.boards import Board, Post, list_boards
from netbbs.boards.posts import count_listed_posts
from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
from netbbs.chat.channels import Channel
from netbbs.communities import get_effective_min_age, meets_read_gate
from netbbs.files.areas import FileArea, list_file_areas
from netbbs.files.entries import count_listed_files
from netbbs.link.boards import LinkContext
from netbbs.mrc.bridge import MrcBridge
from netbbs.mrc.protocol import MRC_LABEL_SUFFIX, mrc_sender
from netbbs.net.board_flow import _show_board
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.char_input import InputHistory
from netbbs.net.chat_flow import (
    NAME_GATE_NOTE,
    browse_channels,
    channel_name_gate_unmet,
    list_visible_channels_for,
)
from netbbs.net.file_flow import enter_file_area
from netbbs.mail import unread_count as unread_mail_count
from netbbs.net.mail_flow import browse_mail, caller_mail_refusal, open_letter
from netbbs.net.notices import announce, announce_styled
from netbbs.net.node_theme import effective_accent_color, effective_header_color, effective_header_color_256
from netbbs.net.picker import pick_item
from netbbs.rendering import GATE_COLOR, MenuEntry, SegmentColor, menu_key
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.session import Session
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.rendering import MUTED_COLOR, colored, reject_keystroke, sanitize_text, screen_title
from netbbs.rendering.post_body import plain_post_body
from netbbs.search import (
    ChannelMessageSearchHit,
    FileSearchHit,
    MailSearchHit,
    PostSearchHit,
    file_jump_cursor,
    post_jump_cursor,
    search_channel_messages,
    search_files,
    search_mail,
    search_posts,
)
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


def _identity(item: _ScanItem) -> tuple[str, int]:
    """Which resource a [N]ew scan row is, across reloads."""
    resource = item.board or item.channel or item.file_area
    return item.kind, resource.id if resource is not None else -1


# Replies listed by subject above [N]ew scan's list; the rest are counted.
# The summary is redrawn with every page, so it is kept to a few rows.
_REPLIES_SHOWN = 3


@dataclass(frozen=True)
class _ScanItem:
    """One row in issue #56's `[N]ew scan` picker -- a board, channel,
    or file area `user` can currently access, with its computed unread
    state and follow status. Built fresh on every screen entry, never
    persisted -- see `_new_scan_screen`'s own docstring for why the row's
    own position is the right stable id here."""

    kind: str  # "board" | "channel" | "file_area"
    name: str
    unread: int | None  # None = never visited, 0 = caught up, >0 = unread count
    followed: bool
    board: Board | None = None
    channel: Channel | None = None
    file_area: FileArea | None = None
    # Issue #541: a channel this caller can see but not enter, because
    # its effective name requirement asks for an attestation they have
    # not got. Resolved while the rows are built, in the pass that is
    # already reading them, and shown in the row -- otherwise this
    # picker reproduces exactly the pick-it-and-be-refused behaviour the
    # channel picker was just fixed for.
    name_gate_unmet: bool = False
    # For a board or area never visited, how many posts or files it holds
    # (issue #839): "not yet visited" alone gave no reason to go in.
    held: int | None = None

    @property
    def has_something(self) -> bool:
        """Whether a walk through the scan stops here: something unread,
        or a board or area never visited that holds anything."""
        return bool(self.unread) or (self.unread is None and bool(self.held))


def _next_with_something(rows: list[_ScanItem], after: tuple[str, int]) -> _ScanItem | None:
    """The row after the one at `after` that a walk stops at next,
    wrapping round to the top, or `None` when nothing else is waiting."""
    index = next((i for i, row in enumerate(rows) if _identity(row) == after), -1)
    for row in rows[index + 1:] + rows[:max(index, 0)]:
        if row.has_something and _identity(row) != after:
            return row
    return None


async def _new_scan_screen(
    session: Session,
    db: Database,
    lane: DatabaseLane,
    hub: ChatHub,
    presence: PresenceRegistry,
    mailbox: MessageMailbox,
    history: InputHistory,
    user: User,
    *,
    link_context: LinkContext | None = None,
    # Issue #475: the node's transfer grants, so an area entered
    # from here offers the same browser-transfer path the picker
    # route does.
    transfers=None,
    mrc_bridge: MrcBridge | None = None,
) -> None:
    """
    Issue #56's activity summary: every board/channel/file area `user`
    can currently access, each showing whether it's never been visited,
    fully caught up, or has unread activity -- plus a distinct "replies
    to you" section, always shown regardless of follow state (a reply
    is always worth surfacing). Followed items are listed first, but
    new scan itself always covers everything accessible, not only
    followed items -- matches the traditional meaning of a BBS
    "new scan" and avoids a brand-new account with nothing followed
    yet seeing an empty screen.

    Built fresh every time this screen is entered -- a plain Python
    list, never persisted -- so there is no database id to use, the way
    `_who_screen`'s sessions and the Link status screen's in-memory peers
    have none either.

    This one numbers its rows instead of using `id(item)` (issue #541,
    Codex review). A `CPython` object address is around fifteen digits,
    and `pick_item` used to print the stable id beside every row as its `(#N)`
    reference: at 40 columns that prefix plus an ordinary channel name
    consumed the whole row, so the description -- including the
    "needs a verified name" note this screen had just been given --
    was clipped away before a caller could read it. A row number is
    short, equally stable for as long as this list exists, and makes
    `[G]oto #` mean something for the first time on this screen.

    Selecting a board/file area jumps straight to its first unread post/
    file via `initial_cursor`; selecting a channel enters it directly
    via `initial_channel`. Channels have no page concept to jump within
    (`get_scrollback` always replays the same bounded buffer), so
    entering one from here is just the ordinary join.
    """

    def _load(db: Database) -> tuple[list[_ScanItem], list[Post], dict[int, Board], int | None]:
        items: list[_ScanItem] = []
        boards_by_id: dict[int, Board] = {}

        for board in list_boards(db):
            boards_by_id[board.id] = board
            if not (
                meets_read_gate(db, user, board)
                and meets_age(db, user, get_effective_min_age(db, board))
            ):
                continue
            unread = unread_post_count(db, user, board)
            items.append(
                _ScanItem(
                    kind="board", name=board.name, unread=unread,
                    followed=is_following(db, user, "board", board.id), board=board,
                    held=count_listed_posts(db, board)[0] if unread is None else None,
                )
            )

        for channel in list_visible_channels_for(db, user):
            items.append(
                _ScanItem(
                    kind="channel", name=channel.name, unread=unread_channel_count(db, user, channel),
                    followed=is_following(db, user, "channel", channel.id), channel=channel,
                    name_gate_unmet=channel_name_gate_unmet(db, user, channel),
                )
            )

        for area in list_file_areas(db):
            if not (
                meets_read_gate(db, user, area)
                and meets_age(db, user, get_effective_min_age(db, area))
            ):
                continue
            unread = unread_file_count(db, user, area)
            items.append(
                _ScanItem(
                    kind="file_area", name=area.name, unread=unread,
                    followed=is_following(db, user, "file_area", area.id), file_area=area,
                    held=count_listed_files(db, area)[0] if unread is None else None,
                )
            )

        # Followed items first; a stable sort preserves each source
        # list's own order (the SysOp's, issue #839) within both groups.
        items.sort(key=lambda item: not item.followed)
        # Only replies on boards this caller may still read: [R]eplies opens
        # the board, and a board whose gate was raised since would refuse
        # them there (review on #869).
        readable = {item.board.id for item in items if item.board is not None}
        replies = [reply for reply in unread_replies_to(db, user) if reply.board_id in readable]
        # Issue #823: the caller's unread mail, `None` for a caller mail is
        # closed to (issue #816), who is told nothing about it.
        mail = unread_mail_count(db, user) if caller_mail_refusal(session, db, user) is None else None
        return items, replies, boards_by_id, mail

    items, replies, boards_by_id, mail = await lane.run(_load)
    state = {"replies": replies, "boards": boards_by_id, "mail": mail}

    def _mail_summary() -> str | None:
        """The caller's mail, the first line above the list (issue #823):
        like the replies, it is theirs rather than a place, so it is a line
        with a key and not a row."""
        unread = state["mail"]
        if unread is None:
            return None
        if not unread:
            return colored("Mail: nothing unread.", fg_color=MUTED_COLOR)
        return f"Mail: {unread} unread -- [E]-mail to read {'it' if unread == 1 else 'them'}"

    async def _replies_summary() -> str:
        """Replies to the caller, above the list on every redraw: the
        picker's masthead, so a redraw in place keeps it and [M]ark read
        brings it up to date (Codex review on #723)."""
        current, boards = state["replies"], state["boards"]
        mail = _mail_summary()
        if not current:
            replies_none = colored("Replies to you: none.", fg_color=MUTED_COLOR)
            return "\r\n".join(line for line in (mail, replies_none) if line)
        lines = [*([mail] if mail else []), f"Replies to you: {len(current)} -- [R]eplies to read them"]
        for reply in current[:_REPLIES_SHOWN]:
            reply_board = boards.get(reply.board_id)
            board_label = sanitize_text(reply_board.name) if reply_board is not None else "unknown message board"
            lines.append(f"  {sanitize_text(reply.subject)} ({board_label})")
        if len(current) > _REPLIES_SHOWN:
            lines.append(f"  ...and {len(current) - _REPLIES_SHOWN} more.")
        return "\r\n".join(lines)

    def _description(item: _ScanItem) -> str:
        prefix = "* " if item.followed else ""
        if item.unread is None and item.held:
            noun = "post" if item.kind == "board" else "file"
            status = f"not yet visited, {item.held} {noun}{'s' if item.held != 1 else ''}"
        elif item.unread is None:
            status = "not yet visited"
        elif item.unread == 0:
            status = "caught up"
        else:
            status = f"{item.unread} unread"
        # The gate is not repeated here: it is already in front of the
        # name, and `pick_item` renders both callbacks every time, so a
        # copy would simply say it twice (Codex review).
        return f"{prefix}{item.kind.replace('_', ' ')}, {status}"

    positions: dict[int, int] = {}

    def _number(scan_items: list[_ScanItem]) -> None:
        positions.clear()
        positions.update({id(item): index for index, item in enumerate(scan_items, start=1)})

    _number(items)
    shown = {"items": items, "all": items, "followed_only": False, "view": set()}
    accent = effective_accent_color(session, db)

    async def _reload_in_place() -> list[_ScanItem]:
        """The list reloaded in the order already on screen, so the
        highlight and every row number still name the row they did (Codex review
        on #723). Anything new goes last. Narrowed to followed items while
        that view is on."""
        reloaded, state["replies"], state["boards"], state["mail"] = await lane.run(_load)
        place = {_identity(row): index for index, row in enumerate(shown["all"])}
        reloaded.sort(key=lambda row: place.get(_identity(row), len(place)))
        shown["all"] = reloaded
        visible = _in_view(reloaded)
        shown["items"] = visible
        _number(visible)
        return visible

    def _in_view(rows: list[_ScanItem]) -> list[_ScanItem]:
        """Everything, or -- in the followed view -- the rows followed when
        the view was switched on. Unfollowing one there keeps it, unmarked,
        until the view is switched off: the list does not change under the
        highlight (Codex review on #788)."""
        if not shown["followed_only"]:
            return rows
        return [row for row in rows if _identity(row) in shown["view"]]

    async def _toggle_follow(item: _ScanItem) -> list[_ScanItem] | None:
        """[F]ollow (issue #675): follow or stop following the row's board,
        channel or file area. A followed one is listed first on the next
        visit and is what [V]iew followed narrows the list to."""
        object_id = _identity(item)[1]
        if item.followed:
            await lane.run(unfollow, user, item.kind, object_id)
            announce(session, f"No longer following {sanitize_text(item.name)}.", tone="muted")
        else:
            await lane.run(follow, user, item.kind, object_id)
            announce(session, f"Following {sanitize_text(item.name)}: it is listed first here.")
        return await _reload_in_place()

    async def _toggle_followed_only() -> list[_ScanItem] | None:
        """[V]iew followed (issue #675, design doc §6.6): the follows-only
        view, one keystroke from the full one and back."""
        shown["followed_only"] = not shown["followed_only"]
        if shown["followed_only"] and not any(row.followed for row in shown["all"]):
            shown["followed_only"] = False
            announce(session, "You follow nothing yet: [F]ollow a row first.", tone="muted")
            return None
        shown["view"] = {_identity(row) for row in shown["all"] if row.followed}
        visible = _in_view(shown["all"])
        shown["items"] = visible
        _number(visible)
        announce(
            session, "Showing what you follow." if shown["followed_only"] else "Showing everything.", tone="muted"
        )
        return visible

    async def _mark_read(item: _ScanItem) -> list[_ScanItem] | None:
        """[M]ark read (issue #710): every post on one board counts as
        read, without going in. The list is reloaded in the same order, so
        the row numbers and the highlight still point where they did."""
        if item.kind != "board" or item.board is None:
            announce(session, "Only a message board can be marked read here.", tone="muted")
            return None
        await lane.run(mark_board_read, user, item.board)
        announce(session, f"{sanitize_text(item.name)}: every post marked read.", tone="muted")
        return await _reload_in_place()

    def _name_segments(item: _ScanItem) -> list[tuple[str, SegmentColor]]:
        """The gate note rides with the name here too (issue #541).

        In the description it sat behind an unbounded name and was the
        first thing a 40-column row lost, so a gated channel looked
        exactly like an ungated one -- which is the whole bug (Codex
        review).
        """
        # The accent the picker would have used for a plain name --
        # a segment list is taken verbatim, so `None` strips it (Codex
        # review).
        segments: list[tuple[str, SegmentColor]] = []
        if item.name_gate_unmet:
            # In *front* of the name, exactly as `channel_name_segments`
            # does it (Codex review -- this copy kept the old order
            # while claiming the fix). `colored_truncate` cuts from the
            # end, so a name long enough to fill the row takes anything
            # behind it with it.
            segments.append((f"({NAME_GATE_NOTE}) ", GATE_COLOR))
        segments.append((item.name, accent))
        return segments

    if not items:
        # The picker has nothing to draw and returns at once, announcing its
        # empty message for the screen this returns to; the summary goes
        # with it rather than being lost (Codex review on #723).
        for line in (await _replies_summary()).split("\r\n"):
            announce_styled(session, line)
    async def _open(item: _ScanItem) -> None:
        if item.kind == "board":
            cursor = await lane.run(board_read_cursor, user, item.board)
            await _show_board(
                session, db, item.board, user, link_context=link_context, initial_cursor=cursor,
                transfers=transfers,
            )
        elif item.kind == "channel":
            await browse_channels(
                session, lane, hub, presence, mailbox, history, user,
                initial_channel=item.channel, link_context=link_context, mrc_bridge=mrc_bridge,
            )
        else:
            cursor = await lane.run(file_area_read_cursor, user, item.file_area)
            await enter_file_area(
                session, lane, item.file_area, user, initial_cursor=cursor,
                link_context=link_context, transfers=transfers,
            )

    async def _read_mail() -> list[_ScanItem] | None:
        """[E]-mail (issue #823): the mailbox, as the main menu opens it;
        Back comes back to the scan with the count brought up to date."""
        if state["mail"] is None:
            await session.write(reject_keystroke())
            return None
        await browse_mail(session, lane, user, link_context=link_context, transfers=transfers)
        return await _reload_in_place()

    async def _read_replies() -> list[_ScanItem] | None:
        """[R]eplies (issue #839, F121): the replies to the caller, one to
        a row. Picking one opens its board on that post; Back from the
        board, or from this list, comes back to the scan."""
        current, boards = state["replies"], state["boards"]
        if not current:
            announce(session, "No replies to you are waiting.", tone="muted")
            return None
        numbered = list(enumerate(current, start=1))

        def _where(entry: tuple[int, Post]) -> str:
            reply_board = boards.get(entry[1].board_id)
            return sanitize_text(reply_board.name) if reply_board is not None else "unknown message board"

        chosen = await pick_item(
            session, numbered,
            name_of=lambda entry: entry[1].subject,
            stable_id_of=lambda entry: entry[0],
            description_of=_where,
            title="Replies to you",
            breadcrumb=("New scan",),
            empty_message="No replies to you are waiting.",
            redraw_in_place=redraw_in_place_enabled(db, user),
            unicode_style=unicode_style_enabled(db, user),
            collapsed=breadcrumb_collapsed_enabled(db, user),
            accent_color=accent,
            header_color=effective_header_color(session, db),
        )
        if chosen is not None:
            reply = chosen[1]
            reply_board = boards.get(reply.board_id)
            if reply_board is not None:
                cursor = await lane.run(post_jump_cursor, reply_board.id, reply.root_post_id)
                await _show_board(
                    session, db, reply_board, user, link_context=link_context, initial_cursor=cursor,
                    transfers=transfers,
                )
        return await _reload_in_place()

    # Back from a row comes back here (issue #839, F045): the scan used to
    # end there, and a caller went round the main menu for every board. The
    # cursor moves on to the next row with something waiting, so Enter walks
    # the scan (F075).
    reopen_at: int | None = None
    while True:
        selected = await pick_item(
            session, shown["items"],
            name_of=lambda item: item.name,
            stable_id_of=lambda item: positions[id(item)],
            name_segments_of=_name_segments,
            description_of=_description,
            title="New scan",
            empty_message="Nothing accessible yet.",
            item_keys={"m": _mark_read, "f": _toggle_follow},
            live_keys={"v": _toggle_followed_only, "r": _read_replies, "e": _read_mail},
            masthead=_replies_summary,
            live_nav=[
                MenuEntry(label=menu_key("M", "ark read"), brief="Count a message board's posts as read"),
                MenuEntry(label=menu_key("F", "ollow"), brief="Follow a board, channel or file area, or stop"),
                MenuEntry(label=menu_key("V", "iew followed"), brief="Only what you follow, or everything again"),
                MenuEntry(label=menu_key("R", "eplies"), brief="Read the replies to your posts"),
                *([MenuEntry(label=menu_key("E", "-mail"), brief="Read your mail")] if state["mail"] is not None else []),
            ],
            redraw_in_place=redraw_in_place_enabled(db, user),
            unicode_style=unicode_style_enabled(db, user),
            collapsed=breadcrumb_collapsed_enabled(db, user),
            accent_color=accent,
            header_color=effective_header_color(session, db),
            start_stable_id=reopen_at,
        )
        if selected is None:
            return
        visited = _identity(selected)
        await _open(selected)
        rows = await _reload_in_place()
        following = _next_with_something(rows, visited)
        if following is not None:
            announce(
                session, f"Next with something new: {sanitize_text(following.name)}. Enter opens it.",
                tone="muted",
            )
            reopen_at = positions[id(following)]
        else:
            announce(session, "Nothing else is new.", tone="muted")
            back_on = next((row for row in rows if _identity(row) == visited), None)
            reopen_at = positions[id(back_on)] if back_on is not None else None


@dataclass(frozen=True)
class _SearchResultItem:
    """One row in issue #56's `[/] Find` results picker -- a matched post,
    file, retained channel message, or one of the caller's own letters
    (issue #824), already filtered to what `user` can currently access
    (`search_posts`/`search_files`/`search_channel_messages`/
    `search_mail`'s own authorization). Built fresh per query, never
    persisted.

    `result_index` (dogfood follow-up), not `id(item)`, is this item's
    `stable_id_of` -- a plain 1-based position in this one query's own
    result list. `root_post_id`/`file_id` are long content-addressed
    hash strings, not the small integer `pick_item` wants as a row's
    identity. Search results are a fixed, never-reordered list for the
    lifetime of one query, so a plain per-query sequential number is a
    real, honest identifier here. It is not printed: rows show only the
    number that selects them (issue #838)."""

    kind: str  # "post" | "file" | "channel_message" | "mail"
    name: str
    description: str
    result_index: int
    post: PostSearchHit | None = None
    file: FileSearchHit | None = None
    message: ChannelMessageSearchHit | None = None
    mail: MailSearchHit | None = None


# A search result row renders as "  NN. name - description",
# colored_truncate()d to terminal_width -- front-to-back, so anything
# past the cutoff is dropped wholesale, not shortened (`netbbs.net.
# picker.pick_item`). A channel message's whole body -- and, since the
# same dogfood follow-up that added post/file snippets below, a
# post's/file's own matched body/description text -- would otherwise
# stand in as (or bloat) the name/description field with no budget left
# for the row prefix and each other on an
# ordinary 80-column terminal. Trimmed to a scannable length that
# leaves real room for the rest of the row in the common case, same
# spirit as _ScanItem's "replies to you" list capping at 10 (a display
# shaping choice, unrelated to and separate from pick_item's own
# sanitize_text call, which still runs on whatever this produces).
_SEARCH_RESULT_SNIPPET_LENGTH = 20

# Mirrors netbbs.search.search_posts/search_files/search_channel_
# messages' own default `limit` -- passed explicitly (rather than
# relying on that default) so `_load` below can request one extra hit
# per category purely to detect truncation (dogfood follow-up), without
# the two ever silently drifting apart.
_SEARCH_RESULT_LIMIT = 20


def _search_snippet(text: str) -> str:
    # One line: a body's line breaks and indentation are not the row's.
    text = " ".join(text.split())
    if len(text) <= _SEARCH_RESULT_SNIPPET_LENGTH:
        return text
    return text[:_SEARCH_RESULT_SNIPPET_LENGTH] + "..."


# Dogfood follow-up: `netbbs.search._match_expression`'s own docstring
# is explicit that "OR"/"AND"/"NOT" are deliberately never interpreted
# as FTS5 boolean operators -- every typed word is required and matched
# literally instead (so oddly formatted input can never raise a syntax
# error deep inside a MATCH clause). That's correct, intentional
# design, not a bug -- but a caller who tries `cats OR dogs` expecting
# an alternation gets an AND-of-three-literal-words query instead,
# which will essentially never match anything, and the plain "No
# matches" message gives no hint why. Investigated and confirmed live:
# quoting the term changes nothing here either (`"cats" OR dogs` fails
# identically to `cats OR dogs`) -- the standalone word is what matters,
# not any surrounding punctuation.
_BOOLEAN_LOOKING_WORDS = frozenset({"or", "and", "not"})


def _looks_like_attempted_boolean_syntax(query: str) -> bool:
    return any(token.lower() in _BOOLEAN_LOOKING_WORDS for token in query.split())


def _chat_hit_author(author_label: str) -> str:
    """Who said a chat line Find turned up. An MRC sender reads `(on MRC)`,
    as `/who` has it, not the stored `(MRC)`, which after a name now reads
    as an account (issue #899)."""
    if author_label.endswith(MRC_LABEL_SUFFIX):
        return f"{mrc_sender(author_label)} (on MRC)"
    return author_label


async def _find_screen(
    session: Session,
    db: Database,
    lane: DatabaseLane,
    hub: ChatHub,
    presence: PresenceRegistry,
    mailbox: MessageMailbox,
    history: InputHistory,
    user: User,
    *,
    link_context: LinkContext | None = None,
    # Issue #475: the node's transfer grants, so an area entered
    # from here offers the same browser-transfer path the picker
    # route does.
    transfers=None,
    mrc_bridge: MrcBridge | None = None,
) -> None:
    """
    Issue #56's local search: prompts for one free-text query, then
    matches it against approved board posts (subject/body), approved
    files (filename/description), and retained channel scrollback
    (message body) -- `netbbs.search`'s three FTS5-backed queries, each
    already filtered to exactly what `user` can currently access (level/
    age/Community gates for boards and file areas, `netbbs.net.
    chat_flow.list_visible_channels_for` for channels -- the identical
    gates `_new_scan_screen` applies). Never touches Link: search only
    ever queries this node's own locally carried content, and the query
    text itself is never transmitted anywhere (see `netbbs.search`'s own
    module docstring).

    Selecting a hit jumps straight to it: a post/file lands on the exact
    matched item (`netbbs.search.post_jump_cursor`/`file_jump_cursor`,
    the immediately preceding item's own cursor, so the hit becomes the
    first thing shown) rather than just opening its board/area at the
    default newest page. A channel message instead just enters its
    channel -- channels have no "jump to one message" concept (unlike
    boards/files, scrollback is a bounded, revision-less ring buffer),
    the same limitation `_new_scan_screen`'s own channel dispatch
    already accepts.

    The caller's own mail is searched too (issue #824) -- their Inbox and
    Sent, never anyone else's (`netbbs.search.search_mail`) -- unless mail
    is closed to them (`caller_mail_refusal`, issue #816), and a letter
    opens in the mailbox's own message view (`open_letter`).
    """
    mail_open = await lane.run(lambda db: caller_mail_refusal(session, db, user)) is None
    await session.write_line(
        "\r\n" + screen_title(
            "Search",
            breadcrumb=(session.node_display_name,),
            subtitle=(
                "Find posts, files, retained chat, and your own mail on this node." if mail_open
                else "Find posts, files, and retained chat on this node."
            ),
            width=session.terminal_width,
            clear=redraw_in_place_enabled(db, user),
            unicode_style=unicode_style_enabled(db, user), collapsed=breadcrumb_collapsed_enabled(db, user),
            header_color=effective_header_color_256(db), node_name_gradient=session.node_name_gradient)
    )
    await session.write("Search terms (Enter cancels): ")
    query = (await session.read_line()).strip()
    if not query:
        await session.write_line(colored("Search cancelled.", fg_color=MUTED_COLOR))
        return

    def _load(db: Database) -> tuple[list[_SearchResultItem], bool]:
        # Fetched one past the actual display cap, purely to detect
        # truncation (dogfood follow-up) -- a broad query used to
        # silently drop everything past the top `_SEARCH_RESULT_LIMIT`
        # per category with no indication anything was cut, distinct
        # from a genuine "no matches" empty state.
        next_index = 1
        items: list[_SearchResultItem] = []
        truncated = False

        post_hits = search_posts(db, user, query, limit=_SEARCH_RESULT_LIMIT + 1)
        truncated = truncated or len(post_hits) > _SEARCH_RESULT_LIMIT
        for hit in post_hits[:_SEARCH_RESULT_LIMIT]:
            items.append(
                _SearchResultItem(
                    kind="post", name=hit.subject,
                    description=f"[POST] {hit.board.name}: {_search_snippet(hit.body)}",
                    result_index=next_index, post=hit,
                )
            )
            next_index += 1

        file_hits = search_files(db, user, query, limit=_SEARCH_RESULT_LIMIT + 1)
        truncated = truncated or len(file_hits) > _SEARCH_RESULT_LIMIT
        for hit in file_hits[:_SEARCH_RESULT_LIMIT]:
            description = f"[FILE] {hit.area.name}"
            if hit.description:
                description += f": {_search_snippet(hit.description)}"
            items.append(
                _SearchResultItem(
                    kind="file", name=hit.filename, description=description,
                    result_index=next_index, file=hit,
                )
            )
            next_index += 1

        visible_channels = list_visible_channels_for(db, user)
        message_hits = search_channel_messages(
            db, user, query, visible_channels=visible_channels, limit=_SEARCH_RESULT_LIMIT + 1
        )
        truncated = truncated or len(message_hits) > _SEARCH_RESULT_LIMIT
        for hit in message_hits[:_SEARCH_RESULT_LIMIT]:
            items.append(
                _SearchResultItem(
                    kind="channel_message", name=_search_snippet(hit.body),
                    description=f"[CHAT] #{hit.channel.name} by {_chat_hit_author(hit.author_label)}",
                    result_index=next_index, message=hit,
                )
            )
            next_index += 1

        # The caller's own letters (issue #824), for a caller mail is open
        # to: no guest and nobody below the mail level (issue #816).
        if mail_open:
            mail_hits = search_mail(db, user, query, limit=_SEARCH_RESULT_LIMIT + 1)
            truncated = truncated or len(mail_hits) > _SEARCH_RESULT_LIMIT
            for hit in mail_hits[:_SEARCH_RESULT_LIMIT]:
                where = f"to {hit.label}" if hit.sent else f"from {hit.label}"
                items.append(
                    _SearchResultItem(
                        kind="mail", name=hit.message.subject,
                        description=f"[MAIL] {where}: {_search_snippet(plain_post_body(hit.message.body))}",
                        result_index=next_index, mail=hit,
                    )
                )
                next_index += 1
        return items, truncated

    items, truncated = await lane.run(_load)
    if truncated:
        await session.write_line(
            colored(
                f"Showing the top {_SEARCH_RESULT_LIMIT} matches per category -- "
                "narrow your search terms for a complete list.",
                fg_color=MUTED_COLOR,
            )
        )

    # Loops back to the results list after viewing a hit (dogfood
    # follow-up), same "pick, view, pick again" shape `_browse_
    # directory`/mail's inbox/sent already use -- checking hit #2 of #5
    # is the whole point of search results specifically, more so than
    # this screen's own one-shot sibling `_new_scan_screen` (one pick
    # per resource *category*, not per hit within one query). Re-uses
    # the same already-fetched `items` rather than re-querying on every
    # loop -- the query text can't change mid-loop (there's no `[S]earch`
    # re-prompt wired to a new query here), so nothing to refresh.
    empty_message = "No matches. Try fewer or broader search terms."
    if _looks_like_attempted_boolean_syntax(query):
        empty_message = (
            'No matches. "OR"/"AND"/"NOT" are not search operators here -- '
            "every word you type is required and matched literally, so "
            "combining one of these with other terms can make a query "
            "impossible to satisfy. Try searching without them."
        )

    while True:
        selected = await pick_item(
            session, items,
            name_of=lambda item: item.name,
            stable_id_of=lambda item: item.result_index,
            description_of=lambda item: item.description,
            title=f"Search results for {query!r}",
            empty_message=empty_message,
            redraw_in_place=redraw_in_place_enabled(db, user),
            unicode_style=unicode_style_enabled(db, user),
            collapsed=breadcrumb_collapsed_enabled(db, user),
            accent_color=effective_accent_color(session, db),
            header_color=effective_header_color(session, db),
        )
        if selected is None:
            return

        if selected.kind == "post":
            cursor = await lane.run(post_jump_cursor, selected.post.board.id, selected.post.root_post_id)
            await _show_board(
                session, db, selected.post.board, user, link_context=link_context, initial_cursor=cursor,
                transfers=transfers,
            )
        elif selected.kind == "file":
            cursor = await lane.run(file_jump_cursor, selected.file.area.id, selected.file.file_id)
            await enter_file_area(
                session, lane, selected.file.area, user, initial_cursor=cursor,
                link_context=link_context, transfers=transfers,
            )
        elif selected.kind == "mail":
            # The mailbox's own message view and actions; a letter deleted
            # there leaves the results (issue #824).
            still_there = await open_letter(
                session, lane, user, selected.mail.message.id, sent=selected.mail.sent,
                link_context=link_context, transfers=transfers,
            )
            if not still_there:
                items = [item for item in items if item is not selected]
        else:
            await browse_channels(
                session, lane, hub, presence, mailbox, history, user,
                initial_channel=selected.message.channel, link_context=link_context, mrc_bridge=mrc_bridge,
            )
