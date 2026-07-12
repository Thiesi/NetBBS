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
    created_at: str


def create_board(
    db: Database,
    name: str,
    *,
    description: str | None = None,
    min_read_level: int = 0,
    min_write_level: int = 0,
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
                (board_id, name, description, min_read_level, min_write_level, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (board_id, name, description, min_read_level, min_write_level, created_at),
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


def list_boards(db: Database) -> list[Board]:
    """
    List all boards, in creation order.

    Deliberately does *not* filter by any requesting user's level here —
    unlike `netbbs.boards.posts.list_posts`, which enforces
    `min_read_level` before returning anything. "List every board for an
    admin view" and "list boards a given user can actually read" are both
    legitimate, different needs built on this same function; filtering
    (via `netbbs.permissions.meets_level` against each board's
    `min_read_level`) is left to the caller rather than baked in here.
    """
    rows = db.connection.execute("SELECT * FROM boards ORDER BY created_at").fetchall()
    return [_row_to_board(row) for row in rows]


def _row_to_board(row: sqlite3.Row) -> Board:
    return Board(
        id=row["id"],
        board_id=row["board_id"],
        name=row["name"],
        description=row["description"],
        min_read_level=row["min_read_level"],
        min_write_level=row["min_write_level"],
        created_at=row["created_at"],
    )
