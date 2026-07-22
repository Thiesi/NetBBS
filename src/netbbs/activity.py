"""
Per-user read cursors and follow/favourite state (design doc §6.6, issue
#56) -- what a user has already seen on a board/channel/file area, and
what they've chosen to follow, both deliberately separate from every
existing access concept (channel membership/invitations, node carry
policy, Community assignment) they sit beside.

A read cursor is the newest item a user has been shown in one container,
not a per-item flag -- a per-item table would itself be unbounded for a
busy board. Boards and file areas already page with a stable
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

Plain, synchronous, `db`-first functions (CLAUDE.md), matching
`netbbs.user_preferences`/`netbbs.chat.membership`'s own convention: every
write commits itself, and none of this calls `record_action` -- follow/
read state is user self-service, not an administrative action, the same
reasoning `user_preferences` already applies to its own writes.
"""

from __future__ import annotations

from netbbs.auth.users import User
from netbbs.boards.boards import Board
from netbbs.boards.posts import Post
from netbbs.chat.channels import Channel
from netbbs.chat.scrollback import ChannelMessage
from netbbs.files.areas import FileArea
from netbbs.files.entries import FileEntry
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


def _get_cursor(db: Database, user: User, object_type: str, object_id: int) -> tuple[str, str] | None:
    row = db.connection.execute(
        "SELECT last_seen_created_at, last_seen_stable_id FROM user_read_cursors "
        "WHERE user_id = ? AND object_type = ? AND object_id = ?",
        (user.id, object_type, object_id),
    ).fetchone()
    if row is None:
        return None
    return row["last_seen_created_at"], row["last_seen_stable_id"]


def _record_seen_string_ordered(
    db: Database, user: User, object_type: str, object_id: int, *, created_at: str, stable_id: str
) -> None:
    existing = _get_cursor(db, user, object_type, object_id)
    if existing is not None and existing >= (created_at, stable_id):
        return  # never retreat -- an older/equal page view must not un-mark newer content
    _upsert_cursor(db, user, object_type, object_id, last_seen_created_at=created_at, last_seen_stable_id=stable_id)


def _record_seen_int_ordered(
    db: Database, user: User, object_type: str, object_id: int, *, created_at: str, stable_id: int
) -> None:
    existing = _get_cursor(db, user, object_type, object_id)
    if existing is not None and int(existing[1]) >= stable_id:
        return
    _upsert_cursor(
        db, user, object_type, object_id, last_seen_created_at=created_at, last_seen_stable_id=str(stable_id)
    )


def _upsert_cursor(
    db: Database, user: User, object_type: str, object_id: int, *, last_seen_created_at: str, last_seen_stable_id: str
) -> None:
    db.connection.execute(
        """
        INSERT INTO user_read_cursors
            (user_id, object_type, object_id, last_seen_created_at, last_seen_stable_id, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, object_type, object_id) DO UPDATE SET
            last_seen_created_at = excluded.last_seen_created_at,
            last_seen_stable_id = excluded.last_seen_stable_id,
            updated_at = excluded.updated_at
        """,
        (user.id, object_type, object_id, last_seen_created_at, last_seen_stable_id, utc_now_iso()),
    )
    db.connection.commit()


def record_board_seen(db: Database, user: User, board: Board, post: Post) -> None:
    """Advance `user`'s read cursor for `board` to (at least) `post` --
    `post` should be the newest post on whatever page was just shown
    (its root `created_at`/`post_id`, stable across later edits)."""
    _record_seen_string_ordered(db, user, _BOARD, board.id, created_at=post.created_at, stable_id=post.post_id)


def board_read_cursor(db: Database, user: User, board: Board) -> tuple[str, str] | None:
    """`user`'s raw `(created_at, post_id)` cursor for `board`, or
    `None` if never visited -- for a caller (issue #56's `[N]ew scan`)
    that needs to jump straight to the first unread post via
    `list_posts_page`'s own `after=` parameter, not just a count."""
    return _get_cursor(db, user, _BOARD, board.id)


def unread_post_count(db: Database, user: User, board: Board) -> int | None:
    """`None` if `user` has never visited `board` (no baseline cursor
    yet -- distinct from `0`, which means visited and fully caught up).
    Mirrors `list_posts_page`'s own root/approved-chain eligibility
    exactly, so this never counts a post the feed itself wouldn't show."""
    cursor = _get_cursor(db, user, _BOARD, board.id)
    if cursor is None:
        return None
    last_created_at, last_post_id = cursor
    row = db.connection.execute(
        """
        SELECT COUNT(*) AS n FROM posts root
        WHERE root.board_id = ? AND root.post_id = root.root_post_id
          AND (root.created_at, root.post_id) > (?, ?)
          AND EXISTS (
              SELECT 1 FROM posts v
              WHERE v.root_post_id = root.root_post_id AND v.board_id = root.board_id
                AND v.status = 'approved'
          )
        """,
        (board.id, last_created_at, last_post_id),
    ).fetchone()
    return row["n"]


def unread_replies_to(db: Database, user: User) -> list[Post]:
    """Every approved post, on any board, replying to one of `user`'s
    own posts, newer than that board's own read cursor for `user` --
    reuses the existing `parent_post_id`/`author_user_id` columns
    directly; no new schema. A board `user` has never visited is
    included in full (no baseline cursor means everything on it,
    including any reply, is still unread)."""
    rows = db.connection.execute(
        """
        SELECT root.* FROM posts root
        JOIN posts parent ON parent.post_id = root.parent_post_id
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
    replies = [_root_row_to_post(row) for row in rows]

    unread = []
    for reply in replies:
        cursor = _get_cursor(db, user, _BOARD, reply.board_id)
        if cursor is None or (reply.created_at, reply.post_id) > cursor:
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
    )


def record_file_area_seen(db: Database, user: User, area: FileArea, entry: FileEntry) -> None:
    """Advance `user`'s read cursor for `area` to (at least) `entry`."""
    _record_seen_string_ordered(db, user, _FILE_AREA, area.id, created_at=entry.created_at, stable_id=entry.file_id)


def file_area_read_cursor(db: Database, user: User, area: FileArea) -> tuple[str, str] | None:
    """`user`'s raw `(created_at, file_id)` cursor for `area`, or
    `None` if never visited -- same purpose as `board_read_cursor`."""
    return _get_cursor(db, user, _FILE_AREA, area.id)


def unread_file_count(db: Database, user: User, area: FileArea) -> int | None:
    """`None` if never visited. Mirrors `list_files_page`'s own
    `status = 'approved'` filter (files have no edit-chain, unlike
    posts)."""
    cursor = _get_cursor(db, user, _FILE_AREA, area.id)
    if cursor is None:
        return None
    last_created_at, last_file_id = cursor
    row = db.connection.execute(
        """
        SELECT COUNT(*) AS n FROM files
        WHERE area_id = ? AND status = 'approved' AND (created_at, file_id) > (?, ?)
        """,
        (area.id, last_created_at, last_file_id),
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
    last_message_id = int(cursor[1])
    placeholders = ",".join("?" for _ in _CHANNEL_CONTENT_KINDS)
    row = db.connection.execute(
        f"""
        SELECT COUNT(*) AS n FROM channel_messages
        WHERE channel_id = ? AND id > ? AND kind IN ({placeholders})
        """,
        (channel.id, last_message_id, *_CHANNEL_CONTENT_KINDS),
    ).fetchone()
    return row["n"]


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
