"""
Message boards: local-only in Phase 1 — no Link, no moderators yet (see
design doc §15 phasing). Board IDs are already content-addressed (see
`netbbs.boards.content_id`) so a board doesn't need an ID-scheme
migration when Linked-board support arrives later.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from netbbs.auth.users import User
from netbbs.boards.content_id import compute_content_id
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


def create_board(
    db: Database,
    name: str,
    *,
    description: str | None = None,
    min_read_level: int = 0,
    min_write_level: int = 0,
    category_id: int | None = None,
    pinned: bool = False,
    creator: User,
) -> Board:
    """
    Create a new local board.

    `min_read_level`/`min_write_level` are a simple, coarse level-gate —
    the finer-grained per-board moderator/permission model from design
    doc §13 (named read/write/edit/delete/approve grants, moderated-board
    approval flows) is explicitly Phase 2 scope and is meant to layer on
    top of this later, not replace it — so this isn't the final word on
    board permissions, just a Phase-1-appropriate default Phase 2 can
    extend without a migration.

    `category_id` optionally places the board under a
    `netbbs.boards.categories.Category` (top-level or sub-category — this
    function doesn't care which, that distinction only matters to the
    category itself). `pinned` boards always sort first, in whatever
    order is otherwise chosen — see `list_boards`.

    No permission check on *creating* a board here — board creation is an
    admin-level action with no SysOp/moderator concept defined yet in
    Phase 1; gating who's allowed to call this is left to whatever calls
    it (a future admin tool), not baked in here.
    """
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
                 category_id, pinned, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        raise BoardError(f"could not create board {name!r} — name already in use?") from exc

    return get_board_by_name(db, name)


def get_board_by_name(db: Database, name: str) -> Board:
    row = db.connection.execute("SELECT * FROM boards WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise BoardError(f"no such board: {name!r}")
    return _row_to_board(row)


def list_boards(db: Database, *, order_by: str = "activity") -> list[Board]:
    """
    List all boards. Pinned boards always sort first, then the rest in
    the chosen `order_by`:

      - "activity" (default): most recent post first (a board with no
        posts yet falls back to its own creation time).
      - "alphabetical": by name, case-insensitive.
      - "volume": total post count, highest first.

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
        rows = db.connection.execute(
            """
            SELECT b.*, COUNT(p.id) AS post_count
            FROM boards b
            LEFT JOIN posts p ON p.board_id = b.id
            GROUP BY b.id
            ORDER BY b.pinned DESC, post_count DESC, b.name COLLATE NOCASE ASC
            """
        ).fetchall()
    else:  # "activity"
        rows = db.connection.execute(
            """
            SELECT b.*, COALESCE(MAX(p.created_at), b.created_at) AS last_activity
            FROM boards b
            LEFT JOIN posts p ON p.board_id = b.id
            GROUP BY b.id
            ORDER BY b.pinned DESC, last_activity DESC
            """
        ).fetchall()

    return [_row_to_board(row) for row in rows]


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
    )
