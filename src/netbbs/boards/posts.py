"""
Board posts. Content-addressed IDs (design doc §7) computed now, even
though actual Link signing/relay is Phase 3 — see
`netbbs.boards.content_id` for why that's a deliberate choice, not
premature complexity.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from netbbs.auth.users import User
from netbbs.boards.boards import Board
from netbbs.boards.content_id import compute_content_id
from netbbs.permissions import require_level
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


class PostError(Exception):
    """Raised for post creation/lookup failures."""


@dataclass(frozen=True)
class Post:
    id: int
    post_id: str
    board_id: int
    parent_post_id: str | None
    author_user_id: int
    author_label: str
    author_fingerprint: str | None
    subject: str
    body: str
    created_at: str


def create_post(
    db: Database,
    board: Board,
    author: User,
    subject: str,
    body: str,
    *,
    parent_post_id: str | None = None,
) -> Post:
    """
    Create a new post on `board`.

    Enforces `board.min_write_level` via the same level-gating plumbing
    (`netbbs.permissions.require_level`) built in Phase 1 — this is the
    first real feature to plug into it, which is exactly the point of
    building that plumbing before any gated feature existed.

    `author_fingerprint` is recorded from the author's account when they
    have a keypair, but posting never requires one. See design doc §15's
    node-vouching decision: a password-only user's posts are still fully
    valid content, just not personally, cryptographically non-repudiable
    the way a keypair holder's would be once Link signing exists in
    Phase 3 — a Phase 3 concern that nothing here blocks on.
    """
    require_level(author, board.min_write_level)

    created_at = utc_now_iso()
    author_identifier = author.fingerprint or author.username
    post_id = compute_content_id(
        {
            "type": "board_post",
            "board_id": board.board_id,
            "parent_post_id": parent_post_id,
            "author": author_identifier,
            "subject": subject,
            "body": body,
            "created_at": created_at,
        }
    )

    if parent_post_id is not None:
        parent = db.connection.execute(
            "SELECT 1 FROM posts WHERE post_id = ? AND board_id = ?",
            (parent_post_id, board.id),
        ).fetchone()
        if parent is None:
            raise PostError(f"parent post {parent_post_id!r} not found on this board")

    try:
        db.connection.execute(
            """
            INSERT INTO posts
                (post_id, board_id, parent_post_id, author_user_id, author_label,
                 author_fingerprint, subject, body, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                post_id,
                board.id,
                parent_post_id,
                author.id,
                author.username,
                author.fingerprint,
                subject,
                body,
                created_at,
            ),
        )
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        raise PostError(
            "could not create post — identical content posted twice in the same instant?"
        ) from exc

    return get_post(db, post_id)


def get_post(db: Database, post_id: str) -> Post:
    row = db.connection.execute("SELECT * FROM posts WHERE post_id = ?", (post_id,)).fetchone()
    if row is None:
        raise PostError(f"no such post: {post_id!r}")
    return _row_to_post(row)


def list_posts(db: Database, board: Board, requesting_user: User) -> list[Post]:
    """List all posts on `board`, oldest first, after checking the
    requesting user meets the board's `min_read_level`."""
    require_level(requesting_user, board.min_read_level)
    rows = db.connection.execute(
        "SELECT * FROM posts WHERE board_id = ? ORDER BY created_at", (board.id,)
    ).fetchall()
    return [_row_to_post(row) for row in rows]


def _row_to_post(row: sqlite3.Row) -> Post:
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
    )
