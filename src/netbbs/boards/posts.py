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


_DEFAULT_PAGE_SIZE = 5

PostCursor = tuple[str, str]  # (created_at, post_id) -- see PostPage/list_posts_page


@dataclass(frozen=True)
class PostPage:
    """One bounded page of posts, always in chronological (oldest-
    first) order *within the page* regardless of which direction it
    was fetched from — matches normal top-to-bottom reading order on
    screen, even though page selection itself works backward from the
    newest post (see `list_posts_page`)."""

    posts: list[Post]
    has_older: bool
    has_newer: bool


def list_posts_page(
    db: Database,
    board: Board,
    requesting_user: User,
    *,
    before: PostCursor | None = None,
    after: PostCursor | None = None,
    limit: int = _DEFAULT_PAGE_SIZE,
) -> PostPage:
    """
    Fetch one bounded page of posts on `board` (design doc round 30,
    issue #10) — never the whole board, however large its history.
    Enforces `board.min_read_level`, same as the unbounded function
    this replaces.

    Ordering is `(created_at, post_id)`, ascending, with `post_id`
    (globally unique, per design doc §7) as a deterministic tie-
    breaker for the rare case of two posts sharing a `created_at`
    timestamp — `created_at` alone is not a total order. Matches the
    composite index `idx_posts_board_id_created_at_post_id`.

    Cursor-based (keyset) pagination, not `OFFSET`/`LIMIT`: stable
    under concurrent inserts (a new post arriving between two page
    loads can't shift already-seen posts into an adjacent page or
    duplicate one across pages, the way an offset-based page boundary
    would), and doesn't pay an ever-growing `OFFSET` scan cost when
    paging deep into an old board's history.

    Three mutually exclusive modes, matching how a caller navigates:
    - Neither `before` nor `after`: the **newest** page — the default
      view when opening a board (design doc round 30, confirmed with
      Thiesi over keeping the old oldest-first default: an active
      board's most recent activity, not its oldest history, is what's
      actually useful to see first).
    - `before=(created_at, post_id)`: the page of up to `limit` posts
      immediately *older* than that cursor — paging backward through
      history. Callers pass the oldest post's cursor from the
      currently displayed page.
    - `after=(created_at, post_id)`: the page of up to `limit` posts
      immediately *newer* than that cursor — paging forward, back
      toward now. Callers pass the newest post's cursor from the
      currently displayed page.

    `has_older`/`has_newer` are computed with their own small indexed
    existence checks against the page's actual boundary, not inferred
    from which mode was requested — correct regardless of navigation
    direction, including the empty-page edge case (both `False`),
    rather than assuming (for example) "arrived via `before`, so
    there's always something newer", which doesn't hold if the cursor
    passed in was already at the newest post.
    """
    require_level(requesting_user, board.min_read_level)
    if before is not None and after is not None:
        raise ValueError("specify at most one of before/after")

    if after is not None:
        created_at, post_id = after
        rows = db.connection.execute(
            """
            SELECT * FROM posts
            WHERE board_id = ? AND (created_at, post_id) > (?, ?)
            ORDER BY created_at ASC, post_id ASC
            LIMIT ?
            """,
            (board.id, created_at, post_id, limit),
        ).fetchall()
        posts = [_row_to_post(row) for row in rows]
    elif before is not None:
        created_at, post_id = before
        rows = db.connection.execute(
            """
            SELECT * FROM posts
            WHERE board_id = ? AND (created_at, post_id) < (?, ?)
            ORDER BY created_at DESC, post_id DESC
            LIMIT ?
            """,
            (board.id, created_at, post_id, limit),
        ).fetchall()
        posts = [_row_to_post(row) for row in reversed(rows)]
    else:
        rows = db.connection.execute(
            """
            SELECT * FROM posts
            WHERE board_id = ?
            ORDER BY created_at DESC, post_id DESC
            LIMIT ?
            """,
            (board.id, limit),
        ).fetchall()
        posts = [_row_to_post(row) for row in reversed(rows)]

    if not posts:
        return PostPage(posts=[], has_older=False, has_newer=False)

    oldest, newest = posts[0], posts[-1]
    has_older = db.connection.execute(
        """
        SELECT EXISTS(
            SELECT 1 FROM posts
            WHERE board_id = ? AND (created_at, post_id) < (?, ?)
        )
        """,
        (board.id, oldest.created_at, oldest.post_id),
    ).fetchone()[0]
    has_newer = db.connection.execute(
        """
        SELECT EXISTS(
            SELECT 1 FROM posts
            WHERE board_id = ? AND (created_at, post_id) > (?, ?)
        )
        """,
        (board.id, newest.created_at, newest.post_id),
    ).fetchone()[0]
    return PostPage(posts=posts, has_older=bool(has_older), has_newer=bool(has_newer))


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
