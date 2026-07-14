"""
Board posts. Content-addressed IDs (design doc §7) computed now, even
though actual Link signing/relay is Phase 3 — see
`netbbs.boards.content_id` for why that's a deliberate choice, not
premature complexity.

Moderated-board approval and the maintenance/expiry state machine
(design doc §13/§15, sign-off round 35) live here too: a post's
`status` moves `pending → approved → expired`, with actual row
deletion as the fourth, unlabeled state (there is no `'deleted'`
status value — that state is the row's absence). See
`list_posts_page`'s new `status = 'approved'` filter and
`_sweep_expired_posts` for how `approved → expired → (deleted)`
actually happens with no background scheduler anywhere in this
codebase (confirmed absent during round 35's design work).
"""

from __future__ import annotations

import datetime
import sqlite3
from dataclasses import dataclass, replace

from netbbs.auth.users import User
from netbbs.boards.boards import Board
from netbbs.boards.content_id import compute_content_id
from netbbs.config import get_expiry_grace_period_days
from netbbs.moderation import BoardPermission, has_permission, record_action
from netbbs.permissions import require_level
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


class PostError(Exception):
    """Raised for post creation/lookup/moderation failures."""


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
    status: str
    pinned: bool
    exempt_from_expiry: bool
    root_post_id: str
    edit_of_post_id: str | None
    # True only on a Post resolved by list_posts_page/list_pinned_posts
    # whose displayed subject/body came from a later edit, not this row
    # itself -- see _resolve_current_version. Always False on a Post
    # from get_post/list_pending_posts, which return exact, unresolved
    # rows and have no concept of "is there a newer version of this."
    is_edited: bool = False


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

    Starts `'pending'` if `board.moderated`, else `'approved'` — see
    `approve_post`/`delete_post` for how a pending post gets resolved,
    and `list_pending_posts` for the moderation queue view.
    """
    require_level(author, board.min_write_level)

    status = "pending" if board.moderated else "approved"
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
                 author_fingerprint, subject, body, created_at, status, root_post_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                status,
                post_id,  # a fresh post is the root of its own edit chain
            ),
        )
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        raise PostError(
            "could not create post — identical content posted twice in the same instant?"
        ) from exc

    return get_post(db, post_id)


def edit_post(
    db: Database,
    post: Post,
    board: Board,
    *,
    subject: str,
    body: str,
    edited_by: User,
) -> Post:
    """
    Create a new revision of `post` (design doc -- prose editor round
    B2 planning). Never mutates the existing row in place: `post_id` is
    a content hash of the subject/body themselves
    (`netbbs.boards.content_id.compute_content_id`), so an in-place
    `UPDATE` would leave a row's own `post_id` silently mismatched
    against its current content, and an existing reply's
    `parent_post_id` references a specific `post_id` directly, which an
    in-place edit would orphan. Instead inserts a brand-new row with a
    fresh content-addressed `post_id`, chained back via
    `root_post_id`/`edit_of_post_id` -- see `_resolve_current_version`
    for how readers only ever see the latest approved revision, at the
    original post's stable feed position.

    Allowed for the post's own original author, no permission grant
    needed -- this project has no other "you may act on it because you
    own it" concept for posts today, but that's exactly the point of a
    personal composer -- or for anyone holding `BoardPermission.EDIT`
    on `board`, matching the existing moderator-edit model (design doc
    §13). `post` may be any resolved or raw `Post` for the post being
    edited (e.g. from `list_posts_page`); this always re-resolves the
    actual current approved version itself via `post.root_post_id`
    rather than trusting `post.post_id`, which is the *root's* id, not
    necessarily the immediate predecessor being amended if the post has
    already been edited before.
    """
    if post.author_user_id != edited_by.id:
        _require_board_permission(db, post, edited_by, BoardPermission.EDIT)

    current = db.connection.execute(
        """
        SELECT * FROM posts
        WHERE root_post_id = ? AND board_id = ? AND status = 'approved'
        ORDER BY created_at DESC, post_id DESC
        LIMIT 1
        """,
        (post.root_post_id, board.id),
    ).fetchone()
    if current is None:
        raise PostError("no currently-approved version of this post exists to edit")

    status = "pending" if board.moderated else "approved"
    created_at = utc_now_iso()
    author_identifier = post.author_fingerprint or post.author_label
    new_post_id = compute_content_id(
        {
            "type": "board_post",
            "board_id": board.board_id,
            "parent_post_id": post.parent_post_id,
            "author": author_identifier,
            "subject": subject,
            "body": body,
            "created_at": created_at,
        }
    )

    try:
        db.connection.execute(
            """
            INSERT INTO posts
                (post_id, board_id, parent_post_id, author_user_id, author_label,
                 author_fingerprint, subject, body, created_at, status,
                 root_post_id, edit_of_post_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_post_id,
                board.id,
                post.parent_post_id,
                post.author_user_id,
                post.author_label,
                post.author_fingerprint,
                subject,
                body,
                created_at,
                status,
                post.root_post_id,
                current["post_id"],
            ),
        )
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        raise PostError(
            "could not save edit — identical content already exists for this post?"
        ) from exc

    record_action(
        db,
        actor=edited_by,
        action="edit",
        object_type="board",
        object_id=board.id,
        target_user_id=post.author_user_id,
        detail=new_post_id,
    )
    return get_post(db, new_post_id)


def get_post(db: Database, post_id: str) -> Post:
    """
    Unbounded by-ID lookup — deliberately not status-filtered, unlike
    `list_posts_page`. Used for `create_post`'s own return path and
    reply-parent lookup, both of which need to work regardless of
    status (including replying to an `'expired'` thread, per design
    doc sign-off round 35: expired content stays individually
    reachable, only delisted from normal browsing). Reaching a
    `'pending'` post this way requires already knowing its exact
    `post_id`, which isn't discoverable through any listing a
    non-author, non-moderator would see — an accepted, practically
    unreachable gap rather than added complexity for it.
    """
    row = db.connection.execute("SELECT * FROM posts WHERE post_id = ?", (post_id,)).fetchone()
    if row is None:
        raise PostError(f"no such post: {post_id!r}")
    return _row_to_post(row)


def _resolve_current_version(db: Database, root_row: sqlite3.Row) -> Post:
    """Given a root post's raw row, build the `Post` actually shown to
    readers (design doc -- prose editor round B2 planning): identity/
    position fields (id, post_id, created_at, pinned, exempt_from_expiry,
    author_*) always come from the root row itself, so a page's cursors
    and a post's feed position never move just because it was edited --
    only `subject`/`body` are substituted from whichever row sharing its
    `root_post_id` is the newest currently `'approved'` one, which is
    the root row itself if it's never been edited (or no edit has been
    approved yet)."""
    latest = db.connection.execute(
        """
        SELECT * FROM posts
        WHERE root_post_id = ? AND board_id = ? AND status = 'approved'
        ORDER BY created_at DESC, post_id DESC
        LIMIT 1
        """,
        (root_row["root_post_id"], root_row["board_id"]),
    ).fetchone()
    root = _row_to_post(root_row)
    if latest is None or latest["post_id"] == root.post_id:
        return root
    return replace(root, subject=latest["subject"], body=latest["body"], is_edited=True)


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

    Post identity/position here is always the *root* of a post's edit
    chain (design doc -- prose editor round B2 planning), regardless of
    whether it's the currently-displayed content: `post_id`/`created_at`
    on every returned `Post` are the root's, so pagination cursors
    (built from a page's own boundary posts, see below) stay stable
    across edits -- editing a post never bumps its position or breaks
    an in-flight cursor. Displayed `subject`/`body`/`status` are
    resolved to whichever row sharing that root is the newest currently
    `'approved'` one (`_resolve_current_version`) -- which may be the
    root row itself (never edited, or every edit still pending/
    rejected) or a later edit. A root eligible for the page only needs
    *some* row in its chain currently approved, not the root row
    itself: an old root can expire on its own schedule while a fresher
    edit keeps the post alive, exactly as if editing had refreshed it.

    `'pending'` posts/edits belong to the moderation queue
    (`list_pending_posts`, unchanged -- it already returns exact,
    unresolved rows regardless of root/edit status), and `'expired'`
    content is delisted from normal browsing, though still individually
    reachable (see `get_post`). Sweeps the board's own posts for
    expiry/deletion first (`_sweep_expired_posts`) so this always
    reflects an up-to-date view, given there's no background job doing
    that separately.
    """
    require_level(requesting_user, board.min_read_level)
    if before is not None and after is not None:
        raise ValueError("specify at most one of before/after")

    _sweep_expired_posts(db, board)

    # A root qualifies as long as *some* row sharing its root_post_id is
    # currently approved -- not necessarily the root row itself, which
    # may have expired while a later edit stayed fresh. Position/cursor
    # ordering still uses the root's own (created_at, post_id), never
    # the approved row's, so editing can't reorder the feed.
    _has_approved_version = """
        EXISTS (
            SELECT 1 FROM posts v
            WHERE v.root_post_id = root.root_post_id AND v.board_id = root.board_id
              AND v.status = 'approved'
        )
    """

    if after is not None:
        created_at, post_id = after
        rows = db.connection.execute(
            f"""
            SELECT root.* FROM posts root
            WHERE root.board_id = ? AND root.post_id = root.root_post_id
              AND (root.created_at, root.post_id) > (?, ?)
              AND {_has_approved_version}
            ORDER BY root.created_at ASC, root.post_id ASC
            LIMIT ?
            """,
            (board.id, created_at, post_id, limit),
        ).fetchall()
        roots = list(rows)
    elif before is not None:
        created_at, post_id = before
        rows = db.connection.execute(
            f"""
            SELECT root.* FROM posts root
            WHERE root.board_id = ? AND root.post_id = root.root_post_id
              AND (root.created_at, root.post_id) < (?, ?)
              AND {_has_approved_version}
            ORDER BY root.created_at DESC, root.post_id DESC
            LIMIT ?
            """,
            (board.id, created_at, post_id, limit),
        ).fetchall()
        roots = list(reversed(rows))
    else:
        rows = db.connection.execute(
            f"""
            SELECT root.* FROM posts root
            WHERE root.board_id = ? AND root.post_id = root.root_post_id
              AND {_has_approved_version}
            ORDER BY root.created_at DESC, root.post_id DESC
            LIMIT ?
            """,
            (board.id, limit),
        ).fetchall()
        roots = list(reversed(rows))

    posts = [_resolve_current_version(db, row) for row in roots]

    if not posts:
        return PostPage(posts=[], has_older=False, has_newer=False)

    oldest, newest = posts[0], posts[-1]
    has_older = db.connection.execute(
        f"""
        SELECT EXISTS(
            SELECT 1 FROM posts root
            WHERE root.board_id = ? AND root.post_id = root.root_post_id
              AND (root.created_at, root.post_id) < (?, ?)
              AND {_has_approved_version}
        )
        """,
        (board.id, oldest.created_at, oldest.post_id),
    ).fetchone()[0]
    has_newer = db.connection.execute(
        f"""
        SELECT EXISTS(
            SELECT 1 FROM posts root
            WHERE root.board_id = ? AND root.post_id = root.root_post_id
              AND (root.created_at, root.post_id) > (?, ?)
              AND {_has_approved_version}
        )
        """,
        (board.id, newest.created_at, newest.post_id),
    ).fetchone()[0]
    return PostPage(posts=posts, has_older=bool(has_older), has_newer=bool(has_newer))


def approve_post(db: Database, post: Post, *, approved_by: User) -> Post:
    """
    Approve a `'pending'` post, requiring `approved_by` to hold
    `BoardPermission.APPROVE` on its board. Logged via
    `netbbs.moderation.log.record_action`.
    """
    _require_board_permission(db, post, approved_by, BoardPermission.APPROVE)

    db.connection.execute("UPDATE posts SET status = 'approved' WHERE id = ?", (post.id,))
    db.connection.commit()
    record_action(
        db,
        actor=approved_by,
        action="approve",
        object_type="board",
        object_id=post.board_id,
        target_user_id=post.author_user_id,
        detail=post.post_id,
    )
    return get_post(db, post.post_id)


def delete_post(db: Database, post: Post, *, deleted_by: User) -> None:
    """
    Delete a post outright, requiring `deleted_by` to hold
    `BoardPermission.DELETE` on its board. Doubles as "reject" for a
    still-`'pending'` post — there is no separate rejected status
    (design doc sign-off round 35) — and the moderation log records
    which of the two actually happened, distinguished by the post's
    status at the moment of deletion.
    """
    _require_board_permission(db, post, deleted_by, BoardPermission.DELETE)

    action = "reject" if post.status == "pending" else "delete"
    db.connection.execute("DELETE FROM posts WHERE id = ?", (post.id,))
    db.connection.commit()
    record_action(
        db,
        actor=deleted_by,
        action=action,
        object_type="board",
        object_id=post.board_id,
        target_user_id=post.author_user_id,
        detail=post.post_id,
    )


def set_post_pinned(db: Database, post: Post, pinned: bool, *, changed_by: User) -> Post:
    """
    Pin or unpin a post within its own board's listing — a distinct
    concept from `netbbs.boards.boards.Board.pinned` (which board
    sorts first among *all* boards). Requires `BoardPermission.EDIT`,
    per the existing pin/exempt-under-`edit` sign-off note.

    Does not reorder `list_posts_page`'s cursor-paginated feed itself
    (that would break keyset pagination's stability guarantees) — see
    `list_pinned_posts` for the dedicated pinned view.
    """
    _require_board_permission(db, post, changed_by, BoardPermission.EDIT)

    db.connection.execute("UPDATE posts SET pinned = ? WHERE id = ?", (int(pinned), post.id))
    db.connection.commit()
    record_action(
        db,
        actor=changed_by,
        action="pin" if pinned else "unpin",
        object_type="board",
        object_id=post.board_id,
        target_user_id=post.author_user_id,
        detail=post.post_id,
    )
    return get_post(db, post.post_id)


def set_post_exempt(db: Database, post: Post, exempt: bool, *, changed_by: User) -> Post:
    """Exempt or unexempt a post from the expiry sweep. Requires
    `BoardPermission.EDIT`, per the existing pin/exempt-under-`edit`
    sign-off note."""
    _require_board_permission(db, post, changed_by, BoardPermission.EDIT)

    db.connection.execute(
        "UPDATE posts SET exempt_from_expiry = ? WHERE id = ?", (int(exempt), post.id)
    )
    db.connection.commit()
    record_action(
        db,
        actor=changed_by,
        action="exempt" if exempt else "unexempt",
        object_type="board",
        object_id=post.board_id,
        target_user_id=post.author_user_id,
        detail=post.post_id,
    )
    return get_post(db, post.post_id)


def list_pending_posts(db: Database, board: Board, *, requesting_user: User) -> list[Post]:
    """
    The moderation queue for `board`: every pending post if
    `requesting_user` holds `BoardPermission.APPROVE`, otherwise only
    their own pending posts (so an author isn't left wondering where
    their own submission went).

    Deliberately not cursor-paginated like `list_posts_page` —
    moderation queues are expected to be much smaller than full board
    history, and this keeps that already-intricate pagination code
    untouched (design doc sign-off round 35).
    """
    if has_permission(
        db, requesting_user, object_type="board", object_id=board.id, permission=BoardPermission.APPROVE
    ):
        rows = db.connection.execute(
            "SELECT * FROM posts WHERE board_id = ? AND status = 'pending' ORDER BY created_at",
            (board.id,),
        ).fetchall()
    else:
        rows = db.connection.execute(
            """
            SELECT * FROM posts WHERE board_id = ? AND status = 'pending' AND author_user_id = ?
            ORDER BY created_at
            """,
            (board.id, requesting_user.id),
        ).fetchall()
    return [_row_to_post(row) for row in rows]


def list_pinned_posts(db: Database, board: Board, *, requesting_user: User) -> list[Post]:
    """
    Every currently-pinned, approved post on `board`, oldest first.
    Requires only `board.min_read_level` — pinning is a display
    convenience, not an access restriction, so anyone who can read the
    board can see what's pinned. A dedicated view rather than
    reordering `list_posts_page`'s feed (see `set_post_pinned`).

    `pinned` is a root-level property (`set_post_pinned` operates on
    whatever `Post` it's given, and every caller passes it an already-
    resolved, root-identified `Post` -- see `_resolve_current_version`),
    so filtering on it here already naturally selects root rows only;
    each is still resolved to its current content the same as
    `list_posts_page`, so a pinned-then-edited post shows its latest
    body here too, not a stale snapshot from when it was pinned.
    """
    require_level(requesting_user, board.min_read_level)
    rows = db.connection.execute(
        """
        SELECT * FROM posts WHERE board_id = ? AND status = 'approved' AND pinned = 1
        ORDER BY created_at
        """,
        (board.id,),
    ).fetchall()
    return [_resolve_current_version(db, row) for row in rows]


def _require_board_permission(db: Database, post: Post, user: User, permission: BoardPermission) -> None:
    if not has_permission(db, user, object_type="board", object_id=post.board_id, permission=permission):
        raise PostError(
            f"{user.username!r} does not hold {permission.name} permission on this board"
        )


def _cutoff_iso(days: int) -> str:
    """The ISO timestamp `days` ago from now, in the same fixed format
    `netbbs.timeutil.utc_now_iso` produces — comparable directly against
    stored `created_at` strings, same as `list_posts_page`'s cursors."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _sweep_expired_posts(db: Database, board: Board) -> None:
    """
    Lazily bring `board`'s post statuses up to date: age `'approved'`
    posts past `board.max_post_age_days` into `'expired'`, then
    hard-delete any already-`'expired'` post whose grace period has
    also elapsed. `exempt_from_expiry` posts are skipped by both
    steps.

    Runs at the top of `list_posts_page` — the natural "someone is
    looking at this board" trigger — rather than via a background job,
    since none exists anywhere in this codebase (confirmed absent
    during design doc sign-off round 35's design work). A no-op
    whenever `board.max_post_age_days` is `None` (retain indefinitely,
    the default). Not logged to `netbbs.moderation.log` — that log is
    for explicit human moderation decisions, not mechanical time-based
    housekeeping.

    The delete step deliberately excludes any post still referenced by
    another live row — as a reply's `parent_post_id`, an edit chain's
    `root_post_id`, or a later edit's `edit_of_post_id` (design doc --
    prose editor round B2 planning; a real, pre-existing bug found
    while testing that round, fixed here rather than there since it
    predates and is independent of post editing). `posts.parent_post_id
    REFERENCES posts(post_id)` with no `ON DELETE` clause means SQLite
    raises `FOREIGN KEY constraint failed` on any attempt to delete a
    still-referenced row — confirmed to already reproduce this against
    a plain reply, with none of round B2's own new columns involved.
    Changing that FK's `ON DELETE` behavior would need the drop/rebuild
    migration pattern rounds 37/40/41/56-57/60 use elsewhere, and round
    60 already found the hard way that rebuilding `posts` specifically
    — a live parent of several other tables' own foreign keys — risks
    SQLite's `DROP TABLE` applying its own cascade/SET-NULL side
    effects to *all* of those relationships at once, not just the one
    column being fixed. Handling it here instead, application-level,
    is the same choice round 60 made for board/area/category deletion
    for exactly that reason: a referenced post simply stays in
    `'expired'` status indefinitely rather than being purged — already
    a valid, harmless state (delisted from browsing, still individually
    reachable via `get_post`), not a new one this introduces.
    """
    if board.max_post_age_days is None:
        return

    expiry_cutoff = _cutoff_iso(board.max_post_age_days)
    db.connection.execute(
        """
        UPDATE posts SET status = 'expired'
        WHERE board_id = ? AND status = 'approved' AND exempt_from_expiry = 0
              AND created_at < ?
        """,
        (board.id, expiry_cutoff),
    )

    grace_days = get_expiry_grace_period_days(db)
    deletion_cutoff = _cutoff_iso(board.max_post_age_days + grace_days)
    db.connection.execute(
        """
        DELETE FROM posts
        WHERE board_id = ? AND status = 'expired' AND exempt_from_expiry = 0
              AND created_at < ?
              AND NOT EXISTS (
                  SELECT 1 FROM posts child
                  WHERE child.post_id != posts.post_id
                    AND (child.parent_post_id = posts.post_id
                         OR child.root_post_id = posts.post_id
                         OR child.edit_of_post_id = posts.post_id)
              )
        """,
        (board.id, deletion_cutoff),
    )
    db.connection.commit()


def _row_to_post(row: sqlite3.Row, *, is_edited: bool = False) -> Post:
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
        is_edited=is_edited,
    )
