"""
Message boards: local-only — no Link yet (see design doc §15 phasing).
Board IDs are already content-addressed (see `netbbs.boards.content_id`)
so a board doesn't need an ID-scheme migration when Linked-board support
arrives later. Moderator/permission grants (`netbbs.moderation.roles`)
and per-board moderation settings (`moderated`, `max_post_age_days`)
layer on top of the coarse `min_read_level`/`min_write_level` gate here
— see `netbbs.boards.posts` for where those settings actually change
post behavior.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from netbbs.auth.users import User
from netbbs.boards.content_id import compute_content_id
from netbbs.moderation.log import record_action
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

# Supported list_boards() sort orders. "activity" is the current default
# (see design doc round 17 sign-off notes) -- creation-order was judged a
# pure implementation convenience that doesn't match how anyone would
# actually want to browse boards (Thiesi's example: a single politics
# board created between two batches of unrelated vintage-computing boards
# ends up sitting in the middle of them under creation-order, satisfying
# nobody). "volume" (total post count) is a genuinely different signal
# from "activity" (most recent post) -- a board with one post today but
# otherwise dead ranks high under activity but low under volume; a board
# with huge historical traffic but nothing new today is the reverse.
# Per-user sort preference is real future scope once user preferences
# exist (not yet) -- this is the node-wide default in the meantime.
_VALID_SORT_ORDERS = ("activity", "alphabetical", "volume")


class BoardError(Exception):
    """Raised for board creation/lookup failures."""


@dataclass(frozen=True)
class Board:
    id: int
    board_id: str
    name: str
    description: str | None
    min_read_level: int
    min_write_level: int
    category_id: int | None
    pinned: bool
    created_at: str
    moderated: bool
    max_post_age_days: int | None
    # Age/name-gating (design doc §18, rounds 85/86/101) -- nullable,
    # NULL means no gate. Enforced alongside min_read_level/
    # min_write_level wherever those already are; see
    # netbbs.net.login_flow's board-browsing/posting checks.
    min_age: int | None
    name_requirement: str | None  # None | "verified" | "verified_and_displayed"


def create_board(
    db: Database,
    name: str,
    *,
    description: str | None = None,
    min_read_level: int = 0,
    min_write_level: int = 0,
    category_id: int | None = None,
    pinned: bool = False,
    moderated: bool = False,
    max_post_age_days: int | None = None,
    min_age: int | None = None,
    name_requirement: str | None = None,
    creator: User,
) -> Board:
    """
    Create a new local board.

    `min_read_level`/`min_write_level` are a simple, coarse level-gate —
    the finer-grained per-board moderator/permission model from design
    doc §13 (named read/write/edit/delete/approve grants) layers on top
    of this rather than replacing it — see `netbbs.moderation.roles`.

    `category_id` optionally places the board under a
    `netbbs.boards.categories.Category` (top-level or sub-category — this
    function doesn't care which, that distinction only matters to the
    category itself). `pinned` boards always sort first, in whatever
    order is otherwise chosen — see `list_boards`.

    `moderated` gates whether new posts start `'pending'` (requiring a
    holder of `BoardPermission.APPROVE` to approve them before other
    users can see them) or go straight to `'approved'` — see
    `netbbs.boards.posts.create_post`. `max_post_age_days` is this
    board's own maintenance/expiry threshold (design doc §13); `None`
    means retain indefinitely, the default.

    `min_age`/`name_requirement` (design doc §18, round 101) are the
    same nullable-means-no-gate shape as everything else here — see
    `netbbs.attestation.meets_age`/`meets_name_requirement` for the
    actual check, enforced by callers alongside `min_read_level`/
    `min_write_level` rather than inside this function.

    No permission check on *creating* a board here — board creation is an
    admin-level action with no SysOp/moderator concept defined yet in
    Phase 1; gating who's allowed to call this is left to whatever calls
    it (a future admin tool), not baked in here.
    """
    if name_requirement not in (None, "verified", "verified_and_displayed"):
        raise BoardError(f"invalid name_requirement: {name_requirement!r}")
    created_at = utc_now_iso()
    board_id = compute_content_id(
        {
            "type": "board",
            "name": name,
            "creator": creator.fingerprint or creator.username,
            "created_at": created_at,
        }
    )

    try:
        db.connection.execute(
            """
            INSERT INTO boards
                (board_id, name, description, min_read_level, min_write_level,
                 category_id, pinned, created_at, moderated, max_post_age_days,
                 min_age, name_requirement)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                board_id,
                name,
                description,
                min_read_level,
                min_write_level,
                category_id,
                int(pinned),
                created_at,
                int(moderated),
                max_post_age_days,
                min_age,
                name_requirement,
            ),
        )
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        raise BoardError(f"could not create board {name!r} — name already in use?") from exc

    new_board = get_board_by_name(db, name)
    record_action(
        db, actor=creator, action="create_board", object_type="board", object_id=new_board.id,
        detail=f"created board {name!r}",
    )
    return new_board


def get_board_by_name(db: Database, name: str) -> Board:
    row = db.connection.execute("SELECT * FROM boards WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise BoardError(f"no such board: {name!r}")
    return _row_to_board(row)


def list_boards(db: Database, *, order_by: str = "activity") -> list[Board]:
    """
    List all boards. Pinned boards always sort first, then the rest in
    the chosen `order_by`:

      - "activity" (default): most recent *approved* post first (a
        board with no approved posts yet falls back to its own creation
        time). Pending and expired posts don't count -- ranking a board
        as active from content ordinary readers can't even see would
        leak that hidden activity exists. An edit does count as fresh
        activity even though it deliberately doesn't move its post's
        own position within the board's own feed (design doc -- prose
        editor round B2) -- those are different concerns at different
        granularities: intra-board feed position vs. board-list
        activity ranking (GitHub issue #36).
      - "alphabetical": by name, case-insensitive.
      - "volume": count of logical posts with a currently-approved
        version, highest first -- not a raw row count, which would
        double-count every edit revision of the same logical post as
        if it were separate content (GitHub issue #36).

    Both "activity" and "volume" also exclude *effectively* expired
    content, not just rows already physically stamped `'expired'`
    (GitHub issue #36, reopened): expiry sweeping is lazy (see
    `netbbs.boards.posts._sweep_expired_posts`'s own docstring for why
    -- no background job exists anywhere in this codebase), so a post
    already past its board's `max_post_age_days` can sit stored as
    `'approved'` indefinitely until *something* actually browses that
    specific board and triggers its sweep. Without this, such a post
    kept counting toward both rankings the whole time it sat in that
    state -- this function has no sweep of its own to run (a listing
    function silently mutating rows as a side effect would be a
    surprising, easy-to-miss write path), so effective expiry is instead
    computed inline: `julianday(post.created_at) >= julianday(now) -
    max_post_age_days` is the same "not yet past its age limit" test the
    sweep itself applies, just expressed as a read-only predicate rather
    than a write. Deliberately excludes the grace period
    (`netbbs.config.get_expiry_grace_period_days`) -- that only governs
    when an already-`'expired'` row is hard-deleted, not when it stops
    being live content a reader would actually see, which is the
    question ranking needs answered. `exempt_from_expiry` posts are
    excluded from this check entirely, same as the sweep. One `now`
    value is reused across every placeholder in a single call, so every
    row is judged against the same instant.

    Deliberately does *not* filter by any requesting user's level here —
    unlike `netbbs.boards.posts.list_posts_page`, which enforces
    `min_read_level` before returning anything. "List every board for an
    admin view" and "list boards a given user can actually read" are both
    legitimate, different needs built on this same function; filtering
    (via `netbbs.permissions.meets_level` against each board's
    `min_read_level`) is left to the caller rather than baked in here.
    """
    if order_by not in _VALID_SORT_ORDERS:
        raise ValueError(f"order_by must be one of {_VALID_SORT_ORDERS}, got {order_by!r}")

    if order_by == "alphabetical":
        rows = db.connection.execute(
            "SELECT * FROM boards ORDER BY pinned DESC, name COLLATE NOCASE ASC"
        ).fetchall()
    elif order_by == "volume":
        now = utc_now_iso()
        rows = db.connection.execute(
            """
            SELECT b.*, COUNT(p.id) AS post_count
            FROM boards b
            LEFT JOIN posts p ON p.board_id = b.id AND p.post_id = p.root_post_id
                AND EXISTS (
                    SELECT 1 FROM posts v
                    WHERE v.root_post_id = p.root_post_id AND v.board_id = p.board_id
                          AND v.status = 'approved'
                          AND (
                                v.exempt_from_expiry = 1
                                OR b.max_post_age_days IS NULL
                                OR julianday(v.created_at) >= julianday(?) - b.max_post_age_days
                          )
                )
            GROUP BY b.id
            ORDER BY b.pinned DESC, post_count DESC, b.name COLLATE NOCASE ASC
            """,
            (now,),
        ).fetchall()
    else:  # "activity"
        now = utc_now_iso()
        rows = db.connection.execute(
            """
            SELECT b.*, COALESCE(MAX(p.created_at), b.created_at) AS last_activity
            FROM boards b
            LEFT JOIN posts p ON p.board_id = b.id
                AND p.status = 'approved'
                AND (
                      p.exempt_from_expiry = 1
                      OR b.max_post_age_days IS NULL
                      OR julianday(p.created_at) >= julianday(?) - b.max_post_age_days
                )
            GROUP BY b.id
            ORDER BY b.pinned DESC, last_activity DESC
            """,
            (now,),
        ).fetchall()

    return [_row_to_board(row) for row in rows]


def update_board(
    db: Database,
    board: Board,
    *,
    name: str,
    description: str | None,
    min_read_level: int,
    min_write_level: int,
    category_id: int | None,
    pinned: bool,
    moderated: bool,
    max_post_age_days: int | None,
    min_age: int | None,
    name_requirement: str | None,
    changed_by: User,
) -> Board:
    """
    Replace `board`'s editable settings with the given full state
    (design doc -- board/area management round) -- every field is
    required, not a partial/PATCH-style update; the admin UI is
    responsible for pre-filling a caller's edits with the board's
    current values as defaults, keeping this function itself simple.
    `board_id`/`created_at` are immutable, not accepted here.

    `min_age`/`name_requirement` follow design doc §18 (round 101) --
    see `create_board`'s docstring.
    """
    if name_requirement not in (None, "verified", "verified_and_displayed"):
        raise BoardError(f"invalid name_requirement: {name_requirement!r}")
    try:
        db.connection.execute(
            """
            UPDATE boards
            SET name = ?, description = ?, min_read_level = ?, min_write_level = ?,
                category_id = ?, pinned = ?, moderated = ?, max_post_age_days = ?,
                min_age = ?, name_requirement = ?
            WHERE id = ?
            """,
            (
                name, description, min_read_level, min_write_level,
                category_id, int(pinned), int(moderated), max_post_age_days,
                min_age, name_requirement, board.id,
            ),
        )
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        raise BoardError(f"could not update board {board.name!r} — name already in use?") from exc

    updated = get_board_by_name(db, name)
    record_action(
        db, actor=changed_by, action="update_board", object_type="board", object_id=board.id,
        detail=f"updated board {board.name!r}",
    )
    return updated


def delete_board(db: Database, board: Board, *, deleted_by: User) -> None:
    """
    Permanently remove `board`, along with its posts and any moderator
    grants scoped to it (design doc -- board/area management round).

    No `ON DELETE` behavior exists in the schema for this -- rebuilding
    `boards`/`posts` together to add it was found, by direct testing
    rather than by inspection, to risk silently deleting/nulling rows
    in *other*, not-yet-rebuilt tables as a side effect of the rebuild
    itself (SQLite's `DROP TABLE` under FK enforcement applies its own
    SET-NULL/cascade-delete fallback to referencing rows regardless of
    the referencing column's actual declared behavior). Handled here at
    the application level instead, the same way `moderator_grants`
    cleanup already has to be (it has no FK at all, being polymorphic)
    -- explicit deletes, in the correct order, inside one transaction.
    Logged before deleting, not after, matching `delete_user`'s own
    "log first" reasoning (design doc round 57).
    """
    record_action(
        db, actor=deleted_by, action="delete_board", object_type="board", object_id=board.id,
        detail=f"deleted board {board.name!r} (id {board.id})",
    )
    db.connection.execute("DELETE FROM posts WHERE board_id = ?", (board.id,))
    db.connection.execute(
        "DELETE FROM moderator_grants WHERE object_type = 'board' AND object_id = ?", (board.id,)
    )
    db.connection.execute("DELETE FROM boards WHERE id = ?", (board.id,))
    db.connection.commit()


def _row_to_board(row: sqlite3.Row) -> Board:
    return Board(
        id=row["id"],
        board_id=row["board_id"],
        name=row["name"],
        description=row["description"],
        min_read_level=row["min_read_level"],
        min_write_level=row["min_write_level"],
        category_id=row["category_id"],
        pinned=bool(row["pinned"]),
        created_at=row["created_at"],
        moderated=bool(row["moderated"]),
        max_post_age_days=row["max_post_age_days"],
        min_age=row["min_age"],
        name_requirement=row["name_requirement"],
    )
