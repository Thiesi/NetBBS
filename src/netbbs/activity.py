"""
Per-user read cursors and follow/favourite state (design doc §6.6, issue
#56) -- what a user has already seen on a board/channel/file area, and
what they've chosen to follow, both deliberately separate from every
existing access concept (channel membership/invitations, node carry
policy, Community assignment) they sit beside.

A read cursor is the newest item a user has been shown in one container,
not a per-item flag -- a per-item table would itself be unbounded for a
busy board. Boards refine this (issue #710): a board post counts as read
only once it is opened, so a board's cursor is a *floor* -- everything at
or below it is read -- plus a bounded set of the posts opened above it
(`user_board_opened_posts`). The set holds only out-of-order reads: an
unbroken run of opened posts from the floor is folded into the floor, and
past `OPENED_POSTS_CAP` rows the floor moves up to the oldest kept one. Boards and file areas already page with a stable
`(created_at, stable_id)` keyset cursor (`netbbs.boards.posts.
list_posts_page`/`netbbs.files.entries.list_files_page`); this module
reuses that exact tuple shape and comparison. A channel has no revision
concept and is already ordered by a plain monotonic `channel_messages.id`
(`netbbs.chat.scrollback.get_scrollback`), so its cursor compares on that
integer alone, never as a string (`"9" > "10"` as strings, wrong as ids)
-- every function below hides this per-type difference so no caller has
to know it.

A cursor never retreats: paging backward into a board's history must not
un-mark already-read content, so every `record_*_seen` call only writes
when the new position is strictly newer than whatever is already stored.

**Two different orderings, issue #72.** A post/file's own `created_at` is
authored chronology -- for a carried Link post, the remote author's own
claimed timestamp, which can be arbitrarily old if it only reaches this
node after a partition or delayed catch-up. `last_seen_arrival_id`
tracks a *different* axis: this node's own local, node-assigned
`posts`/`files` row id (SQLite's `INTEGER PRIMARY KEY` rowid, assigned
in strict insertion order for both a locally created row and a
materialized carried one -- the same property GitHub issue #68 already
relies on for edit-chain tie-breaking) at the moment content became
locally visible. `unread_post_count`/`unread_file_count`/
`unread_replies_to` compare against this arrival axis, not `created_at`,
so a late-arriving post with an old claimed timestamp is still correctly
reported as unread rather than silently sorting behind an
already-advanced cursor. `board_read_cursor`/`file_area_read_cursor`
(used for feed-position jump-to) are unchanged and still return
`(created_at, stable_id)` -- jump-to positioning stays authored-
chronology-based; only *whether something counts as unread at all*
changed. A known consequence: jumping to "first unread" can still land
on the ordinary newest page rather than a specific out-of-order arrival
buried elsewhere in feed history -- see design doc §6.6's "Read/unread
state" subsection for why that gap is an accepted, documented scope
boundary rather than silently unhandled.

Plain, synchronous, `db`-first functions (CLAUDE.md), matching
`netbbs.user_preferences`/`netbbs.chat.membership`'s own convention: every
write commits itself, and none of this calls `record_action` -- follow/
read state is user self-service, not an administrative action, the same
reasoning `user_preferences` already applies to its own writes.
"""

from __future__ import annotations

from dataclasses import dataclass

from netbbs.auth.users import User
from netbbs.boards.boards import Board
from netbbs.boards.posts import Post, count_visible_roots, iter_visible_roots, sweep_expired_posts
from netbbs.chat.channels import Channel
from netbbs.chat.scrollback import ChannelMessage
from netbbs.files.areas import FileArea
from netbbs.files.entries import FileEntry
from netbbs.link.enforcement import envelope_content_visible
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

_BOARD = "board"
_CHANNEL = "channel"
_FILE_AREA = "file_area"

# Channel messages a user would actually consider "activity" to catch up
# on -- join/leave/mute/unmute/ban/unban/kick/nick/daybreak are system
# notices, not content, and are excluded from unread counting the same
# way they'd never be mistaken for a reply or a mention.
_CHANNEL_CONTENT_KINDS = ("message", "action")


@dataclass(frozen=True)
class _Cursor:
    created_at: str
    stable_id: str
    # Node-local arrival order (issue #72) -- may be `None` only for a
    # pre-migration cursor row whose backfill couldn't resolve it because
    # the post/file it named was already hard-deleted at migration time.
    # `_arrival_is_at_or_past` falls back to the pre-#72 created_at/
    # stable_id comparison in that one rare case.
    arrival_id: int | None


def _get_cursor(db: Database, user: User, object_type: str, object_id: int) -> _Cursor | None:
    row = db.connection.execute(
        "SELECT last_seen_created_at, last_seen_stable_id, last_seen_arrival_id FROM user_read_cursors "
        "WHERE user_id = ? AND object_type = ? AND object_id = ?",
        (user.id, object_type, object_id),
    ).fetchone()
    if row is None:
        return None
    return _Cursor(
        created_at=row["last_seen_created_at"],
        stable_id=row["last_seen_stable_id"],
        arrival_id=row["last_seen_arrival_id"],
    )


def _arrival_is_at_or_past(cursor: _Cursor, arrival_id: int, created_at: str, stable_id: str) -> bool:
    """Whether `cursor` already covers `arrival_id` -- the "has this
    content already been marked seen" comparison `unread_*_count`/
    `unread_replies_to` use. Falls back to the legacy created_at/
    stable_id tuple only for the rare pre-#72 cursor whose backfill left
    `arrival_id` unresolved (see `_Cursor`'s own docstring)."""
    if cursor.arrival_id is not None:
        return cursor.arrival_id >= arrival_id
    return (cursor.created_at, cursor.stable_id) >= (created_at, stable_id)


def _record_seen_string_ordered(
    db: Database, user: User, object_type: str, object_id: int, *, created_at: str, stable_id: str, arrival_id: int
) -> None:
    existing = _get_cursor(db, user, object_type, object_id)
    if existing is None:
        _upsert_cursor(
            db, user, object_type, object_id,
            last_seen_created_at=created_at, last_seen_stable_id=stable_id, last_seen_arrival_id=arrival_id,
        )
        return
    if existing.arrival_id is None:
        # Legacy cursor with no arrival axis: the pre-#72 rule, as before.
        if _arrival_is_at_or_past(existing, arrival_id, created_at, stable_id):
            return
        _upsert_cursor(
            db, user, object_type, object_id,
            last_seen_created_at=created_at, last_seen_stable_id=stable_id, last_seen_arrival_id=arrival_id,
        )
        return
    # The arrival watermark (what unread counts compare) and the feed
    # position (where a jump to the first unread lands) are separate axes
    # (§6.6), and each only ever moves forward. A late-arriving post has a
    # newer arrival id but an older authored position: seeing it advances
    # the watermark and must leave the feed position where it was, or a
    # later jump would land on history already read (Codex review on #719).
    arrival_advances = arrival_id > existing.arrival_id
    feed_advances = (created_at, stable_id) > (existing.created_at, existing.stable_id)
    if not arrival_advances and not feed_advances:
        return  # never retreat -- an older/equal view must not un-mark newer content
    _upsert_cursor(
        db, user, object_type, object_id,
        last_seen_created_at=created_at if feed_advances else existing.created_at,
        last_seen_stable_id=stable_id if feed_advances else existing.stable_id,
        last_seen_arrival_id=arrival_id if arrival_advances else existing.arrival_id,
    )


def _record_seen_int_ordered(
    db: Database, user: User, object_type: str, object_id: int, *, created_at: str, stable_id: int
) -> None:
    # A channel message's own id already is both the stable feed position
    # and the arrival order (netbbs.chat.scrollback assigns it via a plain
    # INSERT the same as everything else) -- no separate arrival axis to
    # track here, unlike boards/file areas.
    existing = _get_cursor(db, user, object_type, object_id)
    if existing is not None and existing.arrival_id is not None and existing.arrival_id >= stable_id:
        return
    _upsert_cursor(
        db, user, object_type, object_id,
        last_seen_created_at=created_at, last_seen_stable_id=str(stable_id), last_seen_arrival_id=stable_id,
    )


def _upsert_cursor(
    db: Database, user: User, object_type: str, object_id: int, *,
    last_seen_created_at: str, last_seen_stable_id: str, last_seen_arrival_id: int,
) -> None:
    db.connection.execute(
        """
        INSERT INTO user_read_cursors
            (user_id, object_type, object_id, last_seen_created_at, last_seen_stable_id,
             last_seen_arrival_id, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, object_type, object_id) DO UPDATE SET
            last_seen_created_at = excluded.last_seen_created_at,
            last_seen_stable_id = excluded.last_seen_stable_id,
            last_seen_arrival_id = excluded.last_seen_arrival_id,
            updated_at = excluded.updated_at
        """,
        (
            user.id, object_type, object_id, last_seen_created_at, last_seen_stable_id,
            last_seen_arrival_id, utc_now_iso(),
        ),
    )
    db.connection.commit()


# How many posts opened above a board's floor are remembered per user and
# board (issue #710). Only out-of-order reads land here -- reading in
# arrival order folds straight into the floor -- so the bound is reached
# only by someone skipping around a very busy board, and then the oldest
# gaps are given up as read.
OPENED_POSTS_CAP = 500


def _board_newest(db: Database, board: Board) -> tuple[int, str, str] | None:
    """The newest visible root of `board` by arrival id, and the newest by
    feed position, folded into one `(arrival_id, created_at, post_id)`:
    the arrival id is the floor's axis, the pair the jump position's."""
    newest_id = 0
    feed: tuple[str, str] | None = None
    for row_id, created_at, post_id in iter_visible_roots(db, board.id):
        newest_id = max(newest_id, row_id)
        if feed is None or (created_at, post_id) > feed:
            feed = (created_at, post_id)
    if feed is None:
        return None
    return newest_id, feed[0], feed[1]


def ensure_board_baseline(db: Database, user: User, board: Board) -> None:
    """Give `user` a read floor on `board` if they have none: a first visit
    counts everything already there as read, and only what arrives after
    it is new -- a caller new to a busy board is not handed its whole
    history as unread. A no-op for a board visited before."""
    if _get_cursor(db, user, _BOARD, board.id) is not None:
        return
    sweep_expired_posts(db, board)
    newest = _board_newest(db, board)
    arrival_id, created_at, post_id = newest if newest is not None else (0, "", "")
    _upsert_cursor(
        db, user, _BOARD, board.id,
        last_seen_created_at=created_at, last_seen_stable_id=post_id, last_seen_arrival_id=arrival_id,
    )


def _opened_ids(db: Database, user: User, board_id: int) -> set[int]:
    return {
        row[0] for row in db.connection.execute(
            "SELECT post_row_id FROM user_board_opened_posts WHERE user_id = ? AND board_id = ?",
            (user.id, board_id),
        )
    }


def _compact(db: Database, user: User, board: Board, floor: int) -> int:
    """Fold `user`'s opened posts on `board` into the floor where they run
    unbroken from it, apply the cap, and drop every row the floor now
    covers. Returns the new floor.

    "Unbroken" is over the posts a reader may see: a post pending
    approval, trust-hidden or deleted is not a gap a caller could have
    read, so it does not hold the floor back."""
    opened = {row_id for row_id in _opened_ids(db, user, board.id) if row_id > floor}
    while opened:
        for row_id, _created_at, _post_id in iter_visible_roots(db, board.id, after_id=floor):
            if row_id not in opened:
                break
            floor = row_id
        opened = {row_id for row_id in opened if row_id > floor}
        if len(opened) <= OPENED_POSTS_CAP:
            break
        # Past the cap: keep the newest rows, and the floor moves up to the
        # oldest of them -- the posts skipped below it count as read.
        floor = sorted(opened)[-OPENED_POSTS_CAP]
        opened = {row_id for row_id in opened if row_id > floor}
    db.connection.execute(
        "DELETE FROM user_board_opened_posts WHERE user_id = ? AND board_id = ? AND post_row_id <= ?",
        (user.id, board.id, floor),
    )
    return floor


def record_post_opened(db: Database, user: User, board: Board, post: Post) -> None:
    """`user` opened `post` (a root) on `board`: it counts as read from now
    on (issue #710). Only opening marks a post read -- showing it in a
    list does not.

    The jump position (`board_read_cursor`) moves forward to `post` if it
    is newer by feed position; the unread floor moves only as `_compact`
    allows, so opening the newest post of a board does not mark the
    posts under it read."""
    ensure_board_baseline(db, user, board)
    existing = _get_cursor(db, user, _BOARD, board.id)
    assert existing is not None
    floor = existing.arrival_id or 0
    if post.id > floor:
        db.connection.execute(
            "INSERT OR IGNORE INTO user_board_opened_posts (user_id, board_id, post_row_id) VALUES (?, ?, ?)",
            (user.id, board.id, post.id),
        )
        floor = _compact(db, user, board, floor)
    feed_advances = (post.created_at, post.post_id) > (existing.created_at, existing.stable_id)
    _upsert_cursor(
        db, user, _BOARD, board.id,
        last_seen_created_at=post.created_at if feed_advances else existing.created_at,
        last_seen_stable_id=post.post_id if feed_advances else existing.stable_id,
        last_seen_arrival_id=floor,
    )


def mark_board_read(db: Database, user: User, board: Board) -> None:
    """Everything `user` may see on `board` counts as read (issue #710's
    `[M]ark all read`): the floor moves to the newest visible post and the
    jump position to the newest by feed position. A post still pending
    approval above it stays unread for when it appears."""
    ensure_board_baseline(db, user, board)
    existing = _get_cursor(db, user, _BOARD, board.id)
    assert existing is not None
    sweep_expired_posts(db, board)
    newest = _board_newest(db, board)
    if newest is None:
        return
    arrival_id, created_at, post_id = newest
    floor = max(existing.arrival_id or 0, arrival_id)
    feed = max((existing.created_at, existing.stable_id), (created_at, post_id))
    floor = _compact(db, user, board, floor)
    _upsert_cursor(
        db, user, _BOARD, board.id,
        last_seen_created_at=feed[0], last_seen_stable_id=feed[1], last_seen_arrival_id=floor,
    )


def unread_post_ids(db: Database, user: User, board: Board, posts: list[Post]) -> set[int]:
    """Which of `posts` (roots of `board`) are unread for `user`: above the
    floor and never opened. Empty for a board never visited -- nothing is
    new on a first visit (`ensure_board_baseline`)."""
    cursor = _get_cursor(db, user, _BOARD, board.id)
    if cursor is None:
        return set()
    floor = cursor.arrival_id or 0
    above = [post.id for post in posts if post.id > floor]
    if not above:
        return set()
    return set(above) - _opened_ids(db, user, board.id)


def board_read_cursor(db: Database, user: User, board: Board) -> tuple[str, str] | None:
    """`user`'s raw `(created_at, post_id)` cursor for `board`, or
    `None` if never visited -- for a caller (issue #56's `[N]ew scan`)
    that needs to jump straight to the first unread post via
    `list_posts_page`'s own `after=` parameter, not just a count.
    Feed-position based, unchanged by issue #72 -- see this module's
    own docstring for why that's a separate axis from unread counting."""
    cursor = _get_cursor(db, user, _BOARD, board.id)
    if cursor is None:
        return None
    return cursor.created_at, cursor.stable_id


def unread_post_count(db: Database, user: User, board: Board) -> int | None:
    """`None` if `user` has never visited `board` (no baseline cursor
    yet -- distinct from `0`, which means visited and fully caught up).
    Mirrors `list_posts_page`'s own root/approved-chain eligibility
    exactly, so this never counts a post the feed itself wouldn't show.

    Compares each root post's own local arrival order (`posts.id`, issue
    #72), not `created_at` -- a carried post materialized after a
    partition/catch-up keeps its remote author's own old claimed
    timestamp, which must not let it silently sort behind an
    already-advanced cursor."""
    cursor = _get_cursor(db, user, _BOARD, board.id)
    if cursor is None:
        return None
    sweep_expired_posts(db, board)
    # Above the floor and never opened (issue #710). Trust-hidden carried
    # posts are excluded (issue #677): [N]ew scan must not report posts the
    # board page will never show.
    count, _ = count_visible_roots(
        db, board.id,
        extra_sql=(
            "AND root.id > ? AND root.id NOT IN ("
            "SELECT post_row_id FROM user_board_opened_posts WHERE user_id = ? AND board_id = ?)"
        ),
        extra_params=(cursor.arrival_id or 0, user.id, board.id),
    )
    return count


def unread_replies_to(db: Database, user: User) -> list[Post]:
    """Every approved post, on any board, replying to one of `user`'s
    own posts, newer than that board's own read cursor for `user` --
    reuses the existing `parent_post_id`/`author_user_id` columns
    directly; no new schema. A board `user` has never visited is
    included in full (no baseline cursor means everything on it,
    including any reply, is still unread)."""
    rows = db.connection.execute(
        """
        SELECT root.*, e.envelope_json AS link_envelope_json FROM posts root
        JOIN posts parent ON parent.post_id = root.parent_post_id
        JOIN boards b ON b.id = root.board_id AND b.link_hidden_at IS NULL
        LEFT JOIN link_events e ON e.content_id = root.post_id
        WHERE parent.author_user_id = ?
          AND root.post_id = root.root_post_id
          AND EXISTS (
              SELECT 1 FROM posts v
              WHERE v.root_post_id = root.root_post_id AND v.board_id = root.board_id
                AND v.status = 'approved'
          )
        """,
        (user.id,),
    ).fetchall()
    # Trust-hidden replies are skipped, deciding once per author (issue #677).
    author_cache: dict = {}
    replies = [
        _root_row_to_post(row) for row in rows
        if row["link_envelope_json"] is None
        or envelope_content_visible(db, row["link_envelope_json"], author_cache=author_cache)
    ]

    # Unread as the board list decides it (issue #710): above the board's
    # floor and never opened. Each board's floor and opened set are read
    # once, however many replies sit on it.
    floors: dict[int, int | None] = {}
    opened: dict[int, set[int]] = {}
    unread = []
    for reply in replies:
        if reply.board_id not in floors:
            cursor = _get_cursor(db, user, _BOARD, reply.board_id)
            floors[reply.board_id] = None if cursor is None else (cursor.arrival_id or 0)
            opened[reply.board_id] = set() if cursor is None else _opened_ids(db, user, reply.board_id)
        floor = floors[reply.board_id]
        if floor is None or (reply.id > floor and reply.id not in opened[reply.board_id]):
            unread.append(reply)
    return unread


def _root_row_to_post(row) -> Post:
    """A root post's raw row as a `Post` -- deliberately not resolved
    to its latest approved edit (`netbbs.boards.posts._resolve_current_
    version`, module-private, not reused here): `unread_replies_to`
    only needs identity/position (`post_id`/`board_id`/`created_at`) to
    decide unread-ness and let a caller jump to it via `get_post`; it
    isn't rendering full post content inline."""
    return Post(
        id=row["id"],
        post_id=row["post_id"],
        board_id=row["board_id"],
        parent_post_id=row["parent_post_id"],
        author_user_id=row["author_user_id"],
        author_label=row["author_label"],
        author_fingerprint=row["author_fingerprint"],
        subject=row["subject"],
        body=row["body"],
        created_at=row["created_at"],
        status=row["status"],
        pinned=bool(row["pinned"]),
        exempt_from_expiry=bool(row["exempt_from_expiry"]),
        root_post_id=row["root_post_id"],
        edit_of_post_id=row["edit_of_post_id"],
        tombstoned_at=row["tombstoned_at"],
    )


def record_file_area_seen(db: Database, user: User, area: FileArea, entry: FileEntry) -> None:
    """Advance `user`'s read cursor for `area` to (at least) `entry` --
    `entry.id` (issue #72) is the arrival-order watermark, the same
    reasoning issue #72 documents for posts."""
    _record_seen_string_ordered(
        db, user, _FILE_AREA, area.id, created_at=entry.created_at, stable_id=entry.file_id, arrival_id=entry.id
    )


def file_area_read_cursor(db: Database, user: User, area: FileArea) -> tuple[str, str] | None:
    """`user`'s raw `(created_at, file_id)` cursor for `area`, or
    `None` if never visited -- same purpose as `board_read_cursor`."""
    cursor = _get_cursor(db, user, _FILE_AREA, area.id)
    if cursor is None:
        return None
    return cursor.created_at, cursor.stable_id


def unread_file_count(db: Database, user: User, area: FileArea) -> int | None:
    """`None` if never visited. Mirrors `list_files_page`'s own
    `status = 'approved'` filter (files have no edit-chain, unlike
    posts). Compares each file's own local arrival order (`files.id`,
    issue #72), not `created_at` -- see `unread_post_count`'s own
    docstring for why."""
    cursor = _get_cursor(db, user, _FILE_AREA, area.id)
    if cursor is None:
        return None
    if cursor.arrival_id is not None:
        row = db.connection.execute(
            "SELECT COUNT(*) AS n FROM files WHERE area_id = ? AND status = 'approved' AND id > ?",
            (area.id, cursor.arrival_id),
        ).fetchone()
    else:
        # Legacy fallback -- see _Cursor's own docstring for when this applies.
        row = db.connection.execute(
            """
            SELECT COUNT(*) AS n FROM files
            WHERE area_id = ? AND status = 'approved' AND (created_at, file_id) > (?, ?)
            """,
            (area.id, cursor.created_at, cursor.stable_id),
        ).fetchone()
    return row["n"]


def record_channel_seen(db: Database, user: User, channel: Channel, message: ChannelMessage) -> None:
    """Advance `user`'s read cursor for `channel` to (at least)
    `message` -- compared purely on `message.id` (a plain monotonic
    integer), never as a string."""
    _record_seen_int_ordered(db, user, _CHANNEL, channel.id, created_at=message.created_at, stable_id=message.id)


def unread_channel_count(db: Database, user: User, channel: Channel) -> int | None:
    """`None` if never visited. Only counts message kinds a user would
    consider actual content (`_CHANNEL_CONTENT_KINDS`) -- join/leave/
    nick/daybreak system notices don't count as unread activity. Bounded
    by whatever scrollback is still retained (`netbbs.chat.scrollback`'s
    own ring-buffer trim) -- a message trimmed before this user's next
    visit is simply gone, not counted, the same as it already is for a
    session that was never connected to see it live."""
    cursor = _get_cursor(db, user, _CHANNEL, channel.id)
    if cursor is None:
        return None
    last_message_id = int(cursor.stable_id)
    placeholders = ",".join("?" for _ in _CHANNEL_CONTENT_KINDS)
    # A carried message trust suppresses is hidden from scrollback, so it
    # is not unread activity either (issue #677). The retained ring bounds
    # the rows; the trust decision is made once per author.
    rows = db.connection.execute(
        f"""
        SELECT e.envelope_json FROM channel_messages m
        LEFT JOIN link_events e ON e.content_id = m.link_content_id
        WHERE m.channel_id = ? AND m.id > ? AND m.kind IN ({placeholders})
        """,
        (channel.id, last_message_id, *_CHANNEL_CONTENT_KINDS),
    )
    author_cache: dict = {}
    return sum(
        1 for row in rows
        if row["envelope_json"] is None
        or envelope_content_visible(db, row["envelope_json"], author_cache=author_cache)
    )


def is_following(db: Database, user: User, object_type: str, object_id: int) -> bool:
    row = db.connection.execute(
        "SELECT 1 FROM user_follows WHERE user_id = ? AND object_type = ? AND object_id = ?",
        (user.id, object_type, object_id),
    ).fetchone()
    return row is not None


def follow(db: Database, user: User, object_type: str, object_id: int) -> None:
    db.connection.execute(
        """
        INSERT INTO user_follows (user_id, object_type, object_id, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, object_type, object_id) DO NOTHING
        """,
        (user.id, object_type, object_id, utc_now_iso()),
    )
    db.connection.commit()


def unfollow(db: Database, user: User, object_type: str, object_id: int) -> None:
    db.connection.execute(
        "DELETE FROM user_follows WHERE user_id = ? AND object_type = ? AND object_id = ?",
        (user.id, object_type, object_id),
    )
    db.connection.commit()


def list_followed(db: Database, user: User, object_type: str) -> list[int]:
    """Every `object_id` of `object_type` `user` follows, oldest first.
    A followed object that no longer exists or is no longer visible to
    `user` is not filtered out here -- callers already have the actual
    resource list in hand (from `list_boards`/`list_channels`/
    `list_file_areas`) and should just check membership against it,
    the same lazy-filter approach category/board listings already use
    elsewhere for resources no longer visible."""
    rows = db.connection.execute(
        "SELECT object_id FROM user_follows WHERE user_id = ? AND object_type = ? ORDER BY created_at ASC",
        (user.id, object_type),
    ).fetchall()
    return [row["object_id"] for row in rows]
