"""
`[N]ew scan` and `[F]ind` (issue #56): a caller's unread-activity summary
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
    is_following,
    unread_channel_count,
    unread_file_count,
    unread_post_count,
    unread_replies_to,
)
from netbbs.attestation import meets_age
from netbbs.auth.users import User
from netbbs.boards import Board, Post, list_boards
from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
from netbbs.chat.channels import Channel
from netbbs.communities import get_effective_min_age, get_effective_min_read_level
from netbbs.files.areas import FileArea, list_file_areas
from netbbs.link.boards import LinkContext
from netbbs.mrc.bridge import MrcBridge
from netbbs.net.board_flow import _show_board
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.char_input import InputHistory
from netbbs.net.chat_flow import browse_channels, list_visible_channels_for
from netbbs.net.file_flow import enter_file_area
from netbbs.net.node_theme import effective_accent_color, effective_header_color, effective_header_color_256
from netbbs.net.picker import pick_item
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.session import Session
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.permissions import meets_level
from netbbs.rendering import MUTED_COLOR, colored, sanitize_text, screen_title
from netbbs.search import (
    ChannelMessageSearchHit,
    FileSearchHit,
    PostSearchHit,
    file_jump_cursor,
    post_jump_cursor,
    search_channel_messages,
    search_files,
    search_posts,
)
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


@dataclass(frozen=True)
class _ScanItem:
    """One row in issue #56's `[N]ew scan` picker -- a board, channel,
    or file area `user` can currently access, with its computed unread
    state and follow status. Built fresh on every screen entry, never
    persisted -- see `_new_scan_screen`'s own docstring for why
    `stable_id_of=lambda item: id(item)` is the correct idiom here."""

    kind: str  # "board" | "channel" | "file_area"
    name: str
    unread: int | None  # None = never visited, 0 = caught up, >0 = unread count
    followed: bool
    board: Board | None = None
    channel: Channel | None = None
    file_area: FileArea | None = None


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
    list, never persisted -- so `stable_id_of=lambda item: id(item)`
    is the correct idiom (same as `_who_screen`'s sessions, or the
    Link status screen's in-memory peers), not a database id.

    Selecting a board/file area jumps straight to its first unread post/
    file via `initial_cursor`; selecting a channel enters it directly
    via `initial_channel`. Channels have no page concept to jump within
    (`get_scrollback` always replays the same bounded buffer), so
    entering one from here is just the ordinary join.
    """

    def _load(db: Database) -> tuple[list[_ScanItem], list[Post], dict[int, Board]]:
        items: list[_ScanItem] = []
        boards_by_id: dict[int, Board] = {}

        for board in list_boards(db):
            boards_by_id[board.id] = board
            if not (
                meets_level(user, get_effective_min_read_level(db, board))
                and meets_age(db, user, get_effective_min_age(db, board))
            ):
                continue
            items.append(
                _ScanItem(
                    kind="board", name=board.name, unread=unread_post_count(db, user, board),
                    followed=is_following(db, user, "board", board.id), board=board,
                )
            )

        for channel in list_visible_channels_for(db, user):
            items.append(
                _ScanItem(
                    kind="channel", name=channel.name, unread=unread_channel_count(db, user, channel),
                    followed=is_following(db, user, "channel", channel.id), channel=channel,
                )
            )

        for area in list_file_areas(db):
            if not (
                meets_level(user, get_effective_min_read_level(db, area))
                and meets_age(db, user, get_effective_min_age(db, area))
            ):
                continue
            items.append(
                _ScanItem(
                    kind="file_area", name=area.name, unread=unread_file_count(db, user, area),
                    followed=is_following(db, user, "file_area", area.id), file_area=area,
                )
            )

        # Followed items first; a stable sort preserves each source
        # list's own activity-based order within both groups.
        items.sort(key=lambda item: not item.followed)
        replies = unread_replies_to(db, user)
        return items, replies, boards_by_id

    items, replies, boards_by_id = await lane.run(_load)

    await session.write_line(colored("\r\nNew scan:", fg_color=effective_header_color(session, db), bold=True))
    if replies:
        await session.write_line(f"Replies to you: {len(replies)}")
        for reply in replies[:10]:
            reply_board = boards_by_id.get(reply.board_id)
            board_label = sanitize_text(reply_board.name) if reply_board is not None else "unknown message board"
            await session.write_line(f"  {sanitize_text(reply.subject)} ({board_label})")
        if len(replies) > 10:
            await session.write_line(f"  ...and {len(replies) - 10} more.")
    else:
        await session.write_line(colored("Replies to you: none.", fg_color=MUTED_COLOR))

    def _description(item: _ScanItem) -> str:
        prefix = "* " if item.followed else ""
        if item.unread is None:
            status = "not yet visited"
        elif item.unread == 0:
            status = "caught up"
        else:
            status = f"{item.unread} unread"
        return f"{prefix}{item.kind.replace('_', ' ')}, {status}"

    selected = await pick_item(
        session, items,
        name_of=lambda item: item.name,
        stable_id_of=lambda item: id(item),
        description_of=_description,
        title="New scan",
        empty_message="Nothing accessible yet.",
        redraw_in_place=redraw_in_place_enabled(db, user),
        unicode_style=unicode_style_enabled(db, user),
        collapsed=breadcrumb_collapsed_enabled(db, user),
        accent_color=effective_accent_color(session, db),
        header_color=effective_header_color(session, db),
    )
    if selected is None:
        return

    if selected.kind == "board":
        cursor = await lane.run(board_read_cursor, user, selected.board)
        await _show_board(session, db, selected.board, user, link_context=link_context, initial_cursor=cursor)
    elif selected.kind == "channel":
        await browse_channels(
            session, lane, hub, presence, mailbox, history, user,
            initial_channel=selected.channel, link_context=link_context, mrc_bridge=mrc_bridge,
        )
    else:
        cursor = await lane.run(file_area_read_cursor, user, selected.file_area)
        await enter_file_area(session, lane, selected.file_area, user, initial_cursor=cursor, link_context=link_context)


@dataclass(frozen=True)
class _SearchResultItem:
    """One row in issue #56's `[F]ind` results picker -- a matched post,
    file, or retained channel message, already filtered to what `user`
    can currently access (`search_posts`/`search_files`/
    `search_channel_messages`'s own authorization). Built fresh per
    query, never persisted.

    `result_index` (dogfood follow-up), not `id(item)`, is this item's
    `stable_id_of` -- a plain 1-based position in this one query's own
    result list. `root_post_id`/`file_id` are long content-addressed
    hash strings, not small integers a caller could ever type back into
    `pick_item`'s `[G]oto #` prompt (which is exactly why `_ScanItem`
    elsewhere in this module still uses the unreachable `id(item)`
    idiom -- there's no natural typeable id for those items either).
    Search results are a fixed, never-reordered, never-re-paginated
    list for the lifetime of one query, unlike a board/category
    listing `goto` is designed to keep working across -- so a plain
    per-query sequential number is a real, honest identifier here, not
    a leaky abstraction, and matches the `(#N)` reference already
    printed next to every row."""

    kind: str  # "post" | "file" | "channel_message"
    name: str
    description: str
    result_index: int
    post: PostSearchHit | None = None
    file: FileSearchHit | None = None
    message: ChannelMessageSearchHit | None = None


# A search result row renders as "  NN. (#N) name - description",
# colored_truncate()d to terminal_width -- front-to-back, so anything
# past the cutoff is dropped wholesale, not shortened (`netbbs.net.
# picker.pick_item`). A channel message's whole body -- and, since the
# same dogfood follow-up that added post/file snippets below, a
# post's/file's own matched body/description text -- would otherwise
# stand in as (or bloat) the name/description field with no budget left
# for the row prefix, the `(#N)` goto reference, and each other on an
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
    text = text.strip()
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
    """
    await session.write_line(
        "\r\n" + screen_title(
            "Search",
            breadcrumb=(session.node_display_name,),
            subtitle="Find posts, files, and retained chat on this node.",
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
                    description=f"[CHAT] #{hit.channel.name} by {hit.author_label}",
                    result_index=next_index, message=hit,
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
                session, db, selected.post.board, user, link_context=link_context, initial_cursor=cursor
            )
        elif selected.kind == "file":
            cursor = await lane.run(file_jump_cursor, selected.file.area.id, selected.file.file_id)
            await enter_file_area(
                session, lane, selected.file.area, user, initial_cursor=cursor, link_context=link_context
            )
        else:
            await browse_channels(
                session, lane, hub, presence, mailbox, history, user,
                initial_channel=selected.message.channel, link_context=link_context, mrc_bridge=mrc_bridge,
            )
