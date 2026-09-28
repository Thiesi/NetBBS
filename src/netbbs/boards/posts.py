"""
Board posts. Content-addressed IDs (design doc §7) computed now, even
though actual Link signing/relay is Phase 3 — see
`netbbs.boards.content_id` for why that's a deliberate choice, not
premature complexity.

Moderated-board approval and the maintenance/expiry state machine
(design doc §13/§15) live here too: a post's
`status` moves `pending → approved → expired`, with actual row
deletion as the fourth, unlabeled state (there is no `'deleted'`
status value — that state is the row's absence). See
`list_posts_page`'s `status = 'approved'` filter and
`_sweep_expired_posts` for how `approved → expired → (deleted)`
actually happens with no background scheduler anywhere in this
codebase.
"""

from __future__ import annotations

import datetime
import sqlite3
from dataclasses import dataclass, replace

from netbbs.attestation import meets_age
from netbbs.auth.users import SYSOP_LEVEL, User
from netbbs.boards.boards import Board
from netbbs.boards.content_id import compute_content_id
from netbbs.boards.limits import MAX_BODY_BYTES, MAX_SUBJECT_BYTES
from netbbs.boards.moderation_notices import record_moderation_outcome
from netbbs.communities import get_effective_min_age, get_effective_min_read_level
from netbbs.config import get_expiry_grace_period_days
from netbbs.link.enforcement import envelope_content_visible, link_content_visible
from netbbs.moderation import BoardPermission, has_permission, record_action
from netbbs.permissions import require_level
from netbbs.search import reindex_post
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


class PostError(Exception):
    """Raised for post creation/lookup/moderation failures."""


# Re-exported from netbbs.boards.limits (issue #79) so existing callers/
# tests importing MAX_SUBJECT_BYTES/MAX_BODY_BYTES from this module keep
# working unchanged. The only existing input-length safeguard before
# this (GitHub issue #32) was the transport line editors' 4,096-char
# single-line cap, which the fullscreen prose editor doesn't share,
# letting an authenticated user grow an in-memory document (and
# eventually a stored row) without bound purely by opting into that
# editor. Enforced here, at the domain layer, rather than only in the
# editor UI (see netbbs.net.prose_editor's own ceiling) -- a caller
# going through create_post()/edit_post() directly must not be able to
# bypass a UI-only restriction.


def _check_content_length(subject: str, body: str) -> None:
    subject_bytes = len(subject.encode("utf-8"))
    if subject_bytes > MAX_SUBJECT_BYTES:
        raise PostError(f"subject too long: {subject_bytes} bytes (max {MAX_SUBJECT_BYTES})")
    body_bytes = len(body.encode("utf-8"))
    if body_bytes > MAX_BODY_BYTES:
        raise PostError(f"body too long: {body_bytes} bytes (max {MAX_BODY_BYTES})")


@dataclass(frozen=True)
class Post:
    id: int
    post_id: str
    board_id: int
    parent_post_id: str | None
    # Nullable due to the account-deletion migration (ON DELETE
    # SET NULL) -- also, per design doc §9.3/issue #73, the shape a
    # materialized Link-carried post's remote author naturally takes:
    # no local account is implied or required by carrying content.
    # Every reader of this field already treats it as optional in
    # practice (netbbs.net.board_flow._author_display_name/get_user_by_id
    # degrade to author_label correctly for either case) -- this only
    # makes the type honest about behavior that already existed.
    author_user_id: int | None
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
    # Nullable ISO timestamp (design doc §9.5, issue #88): set once
    # `tombstone_post` redacts this revision -- a *further* chain
    # revision, never an in-place mutation, so `root_post_id`/`edit_of_
    # post_id` above stay intact. `edit_post`/`tombstone_post` both
    # refuse to extend a chain whose current head already has this set.
    tombstoned_at: str | None = None
    # How the body is laid out (issue #711): "prose" reflows, "art" keeps
    # its lines. Set by the editor that wrote the post, on the root; an
    # edit follows its root.
    layout: str = "prose"
    # Issue #675: this revision is its author's withdrawal. On a resolved
    # Post, whether the current revision is.
    withdrawn: bool = False
    # True only on a Post resolved by list_posts_page/list_pinned_posts
    # whose displayed subject/body came from a later edit, not this row
    # itself, and False when that later revision is a tombstone -- see
    # _resolve_current_version. Always False on a Post
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
    layout: str = "prose",
) -> Post:
    """
    Create a new post on `board`.

    `layout` (issue #711) is "art" for a body written in the ANSI art
    editor, whose lines are kept as drawn, and "prose" otherwise.

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

    Refuses with a `PostError` if `board` has been closed (design doc
    §9.5, issue #88 -- `boards.link_closed_at` set by a verified
    `board_closure`) -- a closed board accepts no further posts of any
    kind, replies included.
    """
    require_level(author, board.min_write_level)
    _check_content_length(subject, body)
    closed_row = db.connection.execute(
        "SELECT * FROM boards WHERE id = ?", (board.id,)
    ).fetchone()
    if closed_row is not None and closed_row["link_closed_at"] is not None:
        raise PostError(f"board {board.name!r} is closed and no longer accepts new posts")
    if closed_row is not None and "link_hidden_at" in closed_row.keys() and closed_row["link_hidden_at"] is not None:
        # Issue #683: a caller who opened the board before the SysOp excluded
        # it still holds the `Board`; the write is refused here.
        raise PostError(f"board {board.name!r} is no longer available on this node")

    if layout not in ("prose", "art"):
        raise PostError(f"invalid layout: {layout!r}")
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
        if layout != "prose":
            # Apart from the INSERT, which stays valid on every schema a
            # post can be created on.
            db.connection.execute("UPDATE posts SET layout = ? WHERE post_id = ?", (layout, post_id))
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        raise PostError(
            "could not create post — identical content posted twice in the same instant?"
        ) from exc

    reindex_post(db, board.id, post_id)  # a fresh post is its own root
    return get_post(db, post_id)


def create_labelled_post(
    db: Database,
    board: Board,
    author_label: str,
    subject: str,
    body: str,
    *,
    commit: bool = True,
) -> Post:
    """
    Create a post authored by a *label* rather than by a local account
    (issue #520): `author_user_id` and `author_fingerprint` are NULL and
    `author_label` alone carries the identity.

    This is not a new concept. `netbbs.link.boards` already inserts
    exactly this shape for a post carried from a peer -- NetBBS has had
    "a post authored by something that is not a local account" since
    Link boards existed. A door's outbound hook is the second such
    author, which is why it needs no service *account*: `users` carries
    a CHECK constraint requiring at least one credential
    (`password_hash IS NOT NULL OR public_key IS NOT NULL`), so a
    credential-less service row cannot exist without weakening a
    constraint whose whole job is making sure an account is never
    locked out of itself.

    Deliberately **not** routed through `create_post`, and not merely to
    skip building a throwaway `User`: `create_post` opens with
    `require_level(author, board.min_write_level)`, and a door already
    passes a SysOp-set allowlist to get here. Running both gates means
    they can disagree -- the SysOp allowlists a board, and the post is
    refused at 3am in a door's result file for a reason the SysOp never
    saw. The allowlist is the only gate on this path.

    Everything a post owes the rest of the system is kept: the closed-
    board refusal, the content-length limits, the content-addressed id,
    the moderated-board `'pending'` status, and the search reindex.
    Notably `'pending'` is why a moderated board is the recommended way
    to switch a door's outbound on for the first time -- the SysOp reads
    what it wrote before anyone else does, with no new mechanism.

    Callers that want the post to reach a Linked board's peers pass the
    returned post to `netbbs.link.boards.queue_board_post_if_linked`,
    exactly as the interactive path does; it needs a `Post`, not a
    `User`, and builds `local_user_id` from `author_label`, so a label
    author federates with no special case.

    `commit=False` leaves the row in the caller's open transaction and
    skips the search reindex, so a caller can make the post and whatever
    must accompany it -- a rate debit, an audit entry -- succeed or fail
    together. Such a caller owns the commit *and* must call
    `netbbs.search.reindex_post` afterwards. Leaving the reindex out of
    the transaction is deliberate rather than an oversight: it commits on
    its own, which would end the caller's transaction early, and it is
    documented as idempotent and safe to call after any mutation. A crash
    between the two leaves a post missing from the search index until the
    next reindex, which is a far smaller thing to lose than the audit
    entry saying a door wrote it.
    """
    _check_content_length(subject, body)
    closed_row = db.connection.execute(
        "SELECT * FROM boards WHERE id = ?", (board.id,)
    ).fetchone()
    if closed_row is not None and closed_row["link_closed_at"] is not None:
        raise PostError(f"board {board.name!r} is closed and no longer accepts new posts")
    if closed_row is not None and "link_hidden_at" in closed_row.keys() and closed_row["link_hidden_at"] is not None:
        # Issue #683: a caller who opened the board before the SysOp excluded
        # it still holds the `Board`; the write is refused here.
        raise PostError(f"board {board.name!r} is no longer available on this node")

    status = "pending" if board.moderated else "approved"
    created_at = utc_now_iso()
    post_id = compute_content_id(
        {
            "type": "board_post",
            "board_id": board.board_id,
            "parent_post_id": None,
            "author": author_label,
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
                 author_fingerprint, subject, body, created_at, status, root_post_id)
            VALUES (?, ?, NULL, NULL, ?, NULL, ?, ?, ?, ?, ?)
            """,
            (post_id, board.id, author_label, subject, body, created_at, status, post_id),
        )
        if commit:
            db.connection.commit()
    except sqlite3.IntegrityError as exc:
        raise PostError(
            "could not create post — identical content posted twice in the same instant?"
        ) from exc

    if commit:
        reindex_post(db, board.id, post_id)
    return get_post(db, post_id)


def _refuse_if_board_hidden(db: Database, board_local_id: int) -> None:
    """Issue #683: every change to a board's posts is refused once the SysOp
    has excluded it -- a caller who opened the board before still holds its
    objects, and Restore must bring the board back exactly as it was hidden."""
    row = db.connection.execute("SELECT * FROM boards WHERE id = ?", (board_local_id,)).fetchone()
    if row is not None and "link_hidden_at" in row.keys() and row["link_hidden_at"] is not None:
        raise PostError(f"board {row['name']!r} is no longer available on this node")


def edit_post(
    db: Database,
    post: Post,
    board: Board,
    *,
    subject: str,
    body: str,
    edited_by: User,
    withdrawal: bool = False,
) -> Post:
    """
    Create a new revision of `post`. Never mutates the existing row in place: `post_id` is
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
    _refuse_if_board_hidden(db, post.board_id)
    if post.author_user_id != edited_by.id:
        _require_board_permission(db, post, edited_by, BoardPermission.EDIT)
    _check_content_length(subject, body)

    # Tie-broken on id, not post_id -- see _resolve_current_version's
    # docstring (GitHub issue #68) for why a content-hash tie-break is
    # wrong here.
    current = db.connection.execute(
        """
        SELECT * FROM posts
        WHERE root_post_id = ? AND board_id = ? AND status = 'approved'
        ORDER BY id DESC
        LIMIT 1
        """,
        (post.root_post_id, board.id),
    ).fetchone()
    if current is None:
        raise PostError("no currently-approved version of this post exists to edit")
    if current["tombstoned_at"] is not None:
        raise PostError("this post has been tombstoned and can no longer be edited")

    if subject == current["subject"] and body == current["body"] and not withdrawal:
        # No-op edit (GitHub issue #41): every edit gets a fresh
        # created_at, which alone would produce a new content-addressed
        # post_id and make list_posts_page/_resolve_current_version
        # treat this as a genuine newer revision -- misleadingly marking
        # an unchanged post "(edited)". Skip the new row/is_edited flip
        # entirely when nothing actually changed. Not for a withdrawal,
        # though: text that already reads as the placeholder is still not
        # withdrawn until a withdrawal revision says so (Codex review on
        # #789) -- it is what clears the pin and carries the Link flag.
        return _row_to_post(current)

    # A withdrawal (`withdraw_post`) only takes text away, so it is not
    # held for approval: held, the post would keep showing what its author
    # withdrew until a moderator got to it.
    status = "pending" if board.moderated and not withdrawal else "approved"
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

    # `withdrawn` is named only on a withdrawal; everything else takes the
    # column's default, which also keeps an edit working on a schema older
    # than issue #675's migration.
    withdrawn_column, withdrawn_value = (", withdrawn", ", ?") if withdrawal else ("", "")
    try:
        db.connection.execute(
            f"""
            INSERT INTO posts
                (post_id, board_id, parent_post_id, author_user_id, author_label,
                 author_fingerprint, subject, body, created_at, status,
                 root_post_id, edit_of_post_id{withdrawn_column})
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?{withdrawn_value})
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
                *((1,) if withdrawal else ()),
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
        action="withdraw" if withdrawal else "edit",
        object_type="board",
        object_id=board.id,
        target_user_id=post.author_user_id,
        detail=new_post_id,
    )
    reindex_post(db, board.id, post.root_post_id)
    return get_post(db, new_post_id)


# What a withdrawn post says instead of its text (issue #675).
WITHDRAWN_PLACEHOLDER = "[withdrawn by author]"


def withdraw_post(db: Database, post: Post, board: Board, *, withdrawn_by: User) -> Post:
    """
    The author takes their post back (issue #675, decided with the
    maintainer): a revision whose text is `WITHDRAWN_PLACEHOLDER`, marked
    `withdrawn`. The subject stays, so replies still read as answers to
    something.

    - Not held for approval on a moderated board (`edit_post`'s
      `withdrawal`): held, the post would keep showing what its author
      took back until a moderator got to it.
    - Clears the post's pin and expiry exemption
      (`trg_posts_withdrawal_clears_flags`).
    - Not final: the author may edit the post again, and a moderator
      still sees the withdrawn text in its history. The text is hidden,
      not deleted.
    - On the Link it is a `board_post_edit` carrying `"withdrawn": true`
      (`netbbs.link.boards.queue_board_post_edit_if_linked`), which a
      carrying node applies without holding it either; a node that
      predates the field shows it as an ordinary edit (design doc §16).
    """
    if post.author_user_id is None or post.author_user_id != withdrawn_by.id:
        raise PostError("only the post's author can withdraw it")
    # The subject as it is now, not as the author's open reader had it: a
    # moderator may have changed it since, and the withdrawal must not put
    # the old one back (Codex review on #789).
    current = db.connection.execute(
        "SELECT subject, withdrawn FROM posts WHERE root_post_id = ? AND board_id = ? AND status = 'approved' "
        "ORDER BY id DESC LIMIT 1",
        (post.root_post_id, post.board_id),
    ).fetchone()
    if current is None:
        raise PostError("no currently-approved version of this post exists to withdraw")
    if current["withdrawn"]:
        raise PostError("this post is already withdrawn")
    return edit_post(
        db, post, board, subject=current["subject"], body=WITHDRAWN_PLACEHOLDER, edited_by=withdrawn_by,
        withdrawal=True,
    )


def get_post(db: Database, post_id: str) -> Post:
    """
    Unbounded by-ID lookup — deliberately not status-filtered, unlike
    `list_posts_page`. Used for `create_post`'s own return path and
    reply-parent lookup, both of which need to work regardless of
    status: a reply to a thread that expired mid-conversation must still
    find its parent. That is a statement about the domain, not a promise
    to callers (design doc §5.3, issue #639) -- expiry ends a caller's
    reach, and no caller screen may use this to show an expired post.
    Reaching a
    `'pending'` post this way requires already knowing its exact
    `post_id`, which isn't discoverable through any listing a
    non-author, non-moderator would see — an accepted, practically
    unreachable gap rather than added complexity for it.
    """
    row = db.connection.execute("SELECT * FROM posts WHERE post_id = ?", (post_id,)).fetchone()
    if row is None:
        raise PostError(f"no such post: {post_id!r}")
    return _row_to_post(row)


def visible_post(db: Database, post_id: str) -> Post | None:
    """The post `post_id` belongs to, as the feed would show it right now --
    its current approved revision -- or `None` when the feed would not show
    it at all: expired, pending, deleted, or its signed author hidden by
    trust (design doc §12.8).

    For a caller-facing screen that names another post, such as a reply's
    "reply to ..." (issue #679). `get_post` is deliberately unfiltered and
    exact-revision, which is right for reply-parent resolution and wrong
    for anything a caller reads."""
    row = db.connection.execute(
        "SELECT root_post_id, board_id FROM posts WHERE post_id = ?", (post_id,)
    ).fetchone()
    if row is None:
        return None
    root = db.connection.execute(
        f"""
        SELECT root.* FROM posts root
        WHERE root.post_id = ? AND root.board_id = ? AND {_HAS_APPROVED_VERSION_SQL}
        """,
        (row["root_post_id"], row["board_id"]),
    ).fetchone()
    if root is None or not link_content_visible(db, root["post_id"]):
        return None
    return _resolve_current_version(db, root)


def _resolve_current_version(db: Database, root_row: sqlite3.Row) -> Post:
    """Given a root post's raw row, build the `Post` actually shown to
    readers: identity/position fields (id, post_id, created_at, pinned, exempt_from_expiry,
    author_*) always come from the root row itself, so a page's cursors
    and a post's feed position never move just because it was edited --
    only `subject`/`body` are substituted from whichever row sharing its
    `root_post_id` is the newest currently `'approved'` one, which is
    the root row itself if it's never been edited (or no edit has been
    approved yet).

    Tie-broken on `id` (this row's own `INTEGER PRIMARY KEY`/rowid),
    never `post_id` (GitHub issue #68) -- `post_id` is a content-
    addressed hash, unrelated to creation order, so two revisions
    landing in the same real-clock microsecond (confirmed to happen
    often enough to matter) would otherwise let this pick whichever
    hash sorts lexicographically larger instead of the one actually
    created last. `id` is assigned by SQLite in strict insertion order
    with no explicit value ever supplied on `INSERT` (`create_post`/
    `edit_post`), so it's a genuine monotonic tie-break -- unlike
    `list_posts_page`'s own `(created_at, post_id)` cursor tuple, which
    orders *distinct* root posts' feed positions (an accepted rare-tie
    display-order pick, not "which revision is the true current one"),
    this query picks among competing revisions of the *same* post, where
    picking wrong is a real correctness bug, not just a display quirk.

    Which revision is newest is local receipt order (`id`), not
    `created_at` (issue #675, Codex review on #789): a carried revision's
    `created_at` is its author's clock, display metadata that may run
    backwards (design doc §7.2), while the Link accepts an edit only when
    it extends the chain's current head, so rows arrive -- and the
    rebuild re-inserts them -- in chain order. A local edit's `id` and time
    agree anyway. Every "current revision" query in this module, the
    search index and the Link's predecessor lookup use the same order.

    `tombstoned_at` is also substituted from the latest revision (design
    doc §9.5, issue #88), same as `subject`/`body` -- without this, a
    tombstoned post's placeholder content would display correctly but
    `_can_edit_post`/`_can_tombstone_post` would still see `tombstoned_
    at=None` from the never-tombstoned root row and wrongly keep
    offering `[E]dit`/`[T]ombstone` for it."""
    latest = db.connection.execute(
        """
        SELECT * FROM posts
        WHERE root_post_id = ? AND board_id = ? AND status = 'approved'
        ORDER BY id DESC
        LIMIT 1
        """,
        (root_row["root_post_id"], root_row["board_id"]),
    ).fetchone()
    root = _row_to_post(root_row)
    if latest is None or latest["post_id"] == root.post_id:
        return root
    # A tombstone is a later revision too, but not an edit a reader
    # should be told about: "[removed by moderator] [edited]" says the
    # placeholder text itself was revised. `tombstoned_at` carries it.
    return replace(
        root,
        subject=latest["subject"],
        body=latest["body"],
        tombstoned_at=latest["tombstoned_at"],
        is_edited=latest["tombstoned_at"] is None,
        withdrawn=bool(latest["withdrawn"]) if "withdrawn" in latest.keys() else False,
    )


_DEFAULT_PAGE_SIZE = 5

PostCursor = tuple[str, str]  # (created_at, post_id) -- see PostPage/list_posts_page

# A root qualifies as long as *some* row sharing its root_post_id is
# currently approved -- not necessarily the root row itself, which may
# have expired while a later edit stayed fresh. Shared between
# `list_posts_page` and `count_visible_posts` so both agree on exactly
# which posts "count" as visible.
_HAS_APPROVED_VERSION_SQL = """
    EXISTS (
        SELECT 1 FROM posts v
        WHERE v.root_post_id = root.root_post_id AND v.board_id = root.board_id
          AND v.status = 'approved'
    )
"""


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
    # Issue #675: how many of `posts`, from the front, are pinned posts
    # listed above the dated feed on the newest page. The page's cursors
    # come from the feed, never from them.
    pinned_count: int = 0
    # The feed's own (oldest, newest) cursors, when they are not simply
    # the first and last feed row on the page: the newest page drops the
    # feed rows its pinned block already shows.
    feed_bounds: tuple[PostCursor, PostCursor] | None = None
    # Issue #678: the `root_post_id`s of listed posts with an edit the
    # requesting caller submitted that still awaits a moderator. Their own
    # held *posts* are listed as `status == "pending"` rows instead.
    held_edits: frozenset[str] = frozenset()

    @property
    def oldest_cursor(self) -> PostCursor | None:
        """The cursor for the page before this one."""
        if self.feed_bounds is not None:
            return self.feed_bounds[0]
        feed = self.posts[self.pinned_count:]
        return (feed[0].created_at, feed[0].post_id) if feed else None

    @property
    def newest_cursor(self) -> PostCursor | None:
        """The cursor for the page after this one."""
        if self.feed_bounds is not None:
            return self.feed_bounds[1]
        feed = self.posts[self.pinned_count:]
        return (feed[-1].created_at, feed[-1].post_id) if feed else None


def _require_board_readable(db: Database, board: Board, user: User) -> None:
    """Whether `user` may read `board` at all (issue #675): its effective
    read level -- the board's own, raised by its Community's (the cascade)
    -- and its effective minimum age, the two gates on reading a board.
    (The name requirement gates posting, not reading.) Checked by every
    listing of a board's posts, not only by the screens that lead to one."""
    require_level(user, get_effective_min_read_level(db, board))
    if not meets_age(db, user, get_effective_min_age(db, board)):
        raise PostError("this message board has an age requirement you do not meet")


def list_posts_page(
    db: Database,
    board: Board,
    requesting_user: User,
    *,
    before: PostCursor | None = None,
    after: PostCursor | None = None,
    limit: int = _DEFAULT_PAGE_SIZE,
    with_pinned: bool = False,
    pinned_block_rows: int = 0,
) -> PostPage:
    """
    Fetch one bounded page of posts on `board` (design doc, issue #10)
    — never the whole board, however large its history.
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
      view when opening a board (chosen over an oldest-first default:
      an active board's most recent activity, not its oldest history,
      is what's actually useful to see first).
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
    chain, regardless of whether it's the currently-displayed content:
    `post_id`/`created_at`
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
    content is gone as far as a caller is concerned (design doc §5.3,
    issue #639) -- `get_post` still resolves it for reply-parent lookup,
    which is not a caller-facing surface. Sweeps the board's own posts for
    expiry/deletion first (`_sweep_expired_posts`) so this always
    reflects an up-to-date view, given there's no background job doing
    that separately.

    `with_pinned` (issue #675): on the page a board opens on -- the newest,
    asked for with no cursor -- pinned posts are listed first, in
    `PostPage.pinned_count` rows of its `limit`, at most half of it. They
    stay in the dated feed as well, so a pin the block has no room for is
    still reached by paging; the newest page only leaves out the feed rows
    its block already shows. A page reached by a cursor -- paging, or a
    `[N]ew scan`/`[F]ind` jump that must open on its target -- never gets
    the block.

    `pinned_block_rows` is what the screen draws around a pinned block
    beyond its rows (the "Pinned" rule, issue #675), taken from the feed's
    share of `limit` only when there is a block.

    The requesting caller's own held posts are listed too, in their dated
    place, as `status == "pending"` rows (issue #678): the author sees
    what awaits a moderator where the post will appear, and nobody else
    does. `PostPage.held_edits` names the listed posts with an edit of
    theirs still held.
    """
    _require_board_readable(db, board, requesting_user)
    if before is not None and after is not None:
        raise ValueError("specify at most one of before/after")

    _sweep_expired_posts(db, board)

    held_for = requesting_user.id
    if after is not None:
        roots = _visible_roots(db, board, newer_than=after, limit=limit, held_for=held_for)
    elif before is not None:
        roots = list(reversed(_visible_roots(db, board, older_than=before, limit=limit, held_for=held_for)))
    else:
        roots = list(reversed(_visible_roots(db, board, limit=limit, held_for=held_for)))

    posts = [_resolve_current_version(db, row) for row in roots]
    pinned: list[Post] = []
    feed_bounds = None
    if with_pinned and before is None and after is None:
        pinned = list_pinned_posts(db, board, requesting_user=requesting_user, limit=max(1, limit // 2))
        if pinned and posts:
            feed_bounds = ((posts[0].created_at, posts[0].post_id), (posts[-1].created_at, posts[-1].post_id))
            shown = {post.post_id for post in pinned}
            room = max(0, limit - len(pinned) - pinned_block_rows)
            posts = [post for post in posts if post.post_id not in shown][-room:] if room else []
            if posts:
                feed_bounds = ((posts[0].created_at, posts[0].post_id), feed_bounds[1])
    if not posts and not pinned:
        return PostPage(posts=[], has_older=False, has_newer=False)

    oldest, newest = feed_bounds or (
        ((posts[0].created_at, posts[0].post_id), (posts[-1].created_at, posts[-1].post_id)) if posts else (None, None)
    )
    has_older = oldest is not None and bool(
        _visible_roots(db, board, older_than=oldest, limit=1, held_for=held_for)
    )
    has_newer = newest is not None and bool(
        _visible_roots(db, board, newer_than=newest, limit=1, held_for=held_for)
    )
    return PostPage(
        posts=pinned + posts, has_older=has_older, has_newer=has_newer,
        pinned_count=len(pinned), feed_bounds=feed_bounds,
        held_edits=_held_edits(db, board, requesting_user, [post.root_post_id for post in pinned + posts]),
    )


def _held_edits(db: Database, board: Board, user: User, root_post_ids: list[str]) -> frozenset[str]:
    """Which of `root_post_ids` have an edit `user` submitted still held
    for a moderator (issue #678). Who submitted an edit is its `edit`
    entry in the moderation log: a revision keeps its root's author even
    when a moderator made it."""
    if not root_post_ids:
        return frozenset()
    rows = db.connection.execute(
        f"""
        SELECT DISTINCT v.root_post_id FROM posts v
        JOIN moderation_log m
          ON m.action = 'edit' AND m.object_type = 'board' AND m.object_id = v.board_id
         AND m.detail = v.post_id AND m.actor_user_id = ?
        WHERE v.board_id = ? AND v.status = 'pending' AND v.post_id != v.root_post_id
          AND v.root_post_id IN ({','.join('?' * len(root_post_ids))})
        """,
        (user.id, board.id, *root_post_ids),
    ).fetchall()
    return frozenset(row["root_post_id"] for row in rows)


# How many candidate roots `_visible_roots` reads per query. Hidden roots
# are skipped in Python, so a run of them costs further batches rather
# than a short page.
_VISIBLE_ROOTS_BATCH = 50


def _visible_roots(
    db: Database,
    board: Board,
    *,
    newer_than: PostCursor | None = None,
    older_than: PostCursor | None = None,
    limit: int,
    held_for: int | None = None,
) -> list[sqlite3.Row]:
    """Up to `limit` root rows of `board` that a reader may see, nearest
    the cursor first: ascending after `newer_than`, descending before
    `older_than`, or descending from the newest root when neither is
    given.

    "May see" is the feed's whole rule: some revision in the chain is
    approved (`_HAS_APPROVED_VERSION_SQL`) *and* the root's signed Link
    event is not suppressed by trust (`link_content_visible`, design doc
    §12.8). The second half is decided per event in Python, so it cannot
    sit in the `LIMIT`ed query: filtering a page after fetching it let
    five hidden roots produce an empty page on a board with posts, and
    let `has_older`/`has_newer` count roots no reader could reach
    (issue #677). Batches continue past hidden roots until `limit`
    visible ones are found or the board runs out.

    `held_for` adds that user's own held roots (issue #678): a post only
    its author and the moderation queue see until it is approved."""
    if newer_than is not None and older_than is not None:
        raise ValueError("specify at most one of newer_than/older_than")
    ascending = newer_than is not None
    boundary = newer_than if ascending else older_than
    found: list[sqlite3.Row] = []
    author_cache: dict = {}
    held_sql, held_params = (
        ("", ()) if held_for is None
        else ("OR (root.status = 'pending' AND root.author_user_id = ?)", (held_for,))
    )
    while len(found) < limit:
        if boundary is None:
            position_sql, params = "", ()
        else:
            position_sql = f"AND (root.created_at, root.post_id) {'>' if ascending else '<'} (?, ?)"
            params = boundary
        order = "ASC" if ascending else "DESC"
        rows = db.connection.execute(
            f"""
            SELECT root.*, e.envelope_json AS link_envelope_json FROM posts root
            LEFT JOIN link_events e ON e.content_id = root.post_id
            WHERE root.board_id = ? AND root.post_id = root.root_post_id
              {position_sql}
              AND ({_HAS_APPROVED_VERSION_SQL} {held_sql})
            ORDER BY root.created_at {order}, root.post_id {order}
            LIMIT ?
            """,
            (board.id, *params, *held_params, _VISIBLE_ROOTS_BATCH),
        ).fetchall()
        for row in rows:
            envelope_json = row["link_envelope_json"]
            if envelope_json is None or envelope_content_visible(db, envelope_json, author_cache=author_cache):
                found.append(row)
                if len(found) == limit:
                    break
        if len(rows) < _VISIBLE_ROOTS_BATCH:
            break
        boundary = (rows[-1]["created_at"], rows[-1]["post_id"])
    return found


def count_visible_posts(db: Database, board: Board) -> tuple[int, str | None]:
    """
    Total visible (approved) posts on `board`, plus the most recent
    one's `created_at` (`None` if there are none).

    For admin/reporting surfaces (`netbbs.net.admin_flow`'s board
    detail screen -- dogfood follow-up: a SysOp trying to spot a dead
    board versus an active one had no way to tell without leaving
    admin and browsing it as an ordinary reader) -- not gated by
    `min_read_level` since only a SysOp already inside the admin
    console reaches this. Uses the same root-eligibility rule as
    `list_posts_page` (`count_visible_roots`) so the count matches what
    an actual reader would see, trust-hidden carried posts excluded.
    """
    _sweep_expired_posts(db, board)
    return count_visible_roots(db, board.id)


def count_visible_roots(
    db: Database, board_id: int, *, extra_sql: str = "", extra_params: tuple = ()
) -> tuple[int, str | None]:
    """How many roots of board `board_id` a reader may see, and the newest
    one's `created_at` -- the counting form of `_visible_roots`' rule,
    for every surface that reports a number instead of a page
    (`count_visible_posts`, `netbbs.activity.unread_post_count`).

    `extra_sql` narrows the roots further (an `AND ...` clause over the
    `root` alias). Roots with no retained Link event are local and always
    visible, so SQL counts those; only carried roots pay the per-event
    trust check (issue #677)."""
    local = db.connection.execute(
        f"""
        SELECT COUNT(*), MAX(root.created_at) FROM posts root
        WHERE root.board_id = ? AND root.post_id = root.root_post_id
          AND NOT EXISTS (SELECT 1 FROM link_events e WHERE e.content_id = root.post_id)
          {extra_sql}
          AND {_HAS_APPROVED_VERSION_SQL}
        """,
        (board_id, *extra_params),
    ).fetchone()
    count, newest = local[0], local[1]
    # Streamed rather than fetched whole, with each root's envelope joined
    # in: the trust decision depends only on the author, so it is looked up
    # once per distinct author, not once per carried post.
    author_cache: dict = {}
    carried = db.connection.execute(
        f"""
        SELECT root.created_at, e.envelope_json FROM posts root
        JOIN link_events e ON e.content_id = root.post_id
        WHERE root.board_id = ? AND root.post_id = root.root_post_id
          {extra_sql}
          AND {_HAS_APPROVED_VERSION_SQL}
        """,
        (board_id, *extra_params),
    )
    for row in carried:
        if envelope_content_visible(db, row["envelope_json"], author_cache=author_cache):
            count += 1
            if newest is None or row["created_at"] > newest:
                newest = row["created_at"]
    return count, newest


def iter_visible_roots(
    db: Database, board_id: int, *, after_id: int = 0, newest_first: bool = False,
    by_feed: bool = False, extra_sql: str = "", extra_params: tuple = (),
):
    """Board `board_id`'s roots a reader may see, as `(id, created_at,
    post_id)` above arrival id `after_id`: in arrival order (`posts.id`,
    issue #72), or with `by_feed` in feed order (`created_at`, `post_id`),
    oldest first or `newest_first`. `extra_sql` narrows further, as
    `count_visible_roots`' does. The same rule as `count_visible_roots`,
    streamed, so a caller that needs only the first few stops paying there
    -- per-post read state walks up from its floor and stops at the first
    post not opened, and looks up a board's newest post without reading
    the rest (issue #710)."""
    order = "DESC" if newest_first else "ASC"
    order_by = f"root.created_at {order}, root.post_id {order}" if by_feed else f"root.id {order}"
    rows = db.connection.execute(
        f"""
        SELECT root.id, root.created_at, root.post_id, e.envelope_json FROM posts root
        LEFT JOIN link_events e ON e.content_id = root.post_id
        WHERE root.board_id = ? AND root.post_id = root.root_post_id AND root.id > ?
          {extra_sql}
          AND {_HAS_APPROVED_VERSION_SQL}
        ORDER BY {order_by}
        """,
        (board_id, after_id, *extra_params),
    )
    author_cache: dict = {}
    for row in rows:
        if row["envelope_json"] is None or envelope_content_visible(
            db, row["envelope_json"], author_cache=author_cache
        ):
            yield row["id"], row["created_at"], row["post_id"]


def approve_post(db: Database, post: Post, *, approved_by: User) -> Post:
    """
    Approve a `'pending'` post, requiring `approved_by` to hold
    `BoardPermission.APPROVE` on its board. Logged via
    `netbbs.moderation.log.record_action`.

    Refuses a post that is no longer pending -- approved by another
    moderator, rejected, or expired since the queue was drawn. Without the
    status condition an expired post would be quietly brought back.
    """
    _refuse_if_board_hidden(db, post.board_id)
    _require_board_permission(db, post, approved_by, BoardPermission.APPROVE)

    cursor = db.connection.execute(
        "UPDATE posts SET status = 'approved' WHERE id = ? AND status = 'pending'", (post.id,)
    )
    db.connection.commit()
    if cursor.rowcount == 0:
        raise PostError("this post is no longer waiting for approval")
    record_action(
        db,
        actor=approved_by,
        action="approve",
        object_type="board",
        object_id=post.board_id,
        target_user_id=post.author_user_id,
        detail=post.post_id,
    )
    reindex_post(db, post.board_id, post.root_post_id)
    # The author is told (issue #678).
    record_moderation_outcome(db, post, outcome="approved", moderator=approved_by)
    return get_post(db, post.post_id)


def delete_post(db: Database, post: Post, *, deleted_by: User, reason: str | None = None) -> None:
    """
    Delete a post outright, requiring `deleted_by` to hold
    `BoardPermission.DELETE` on its board. Doubles as "reject" for a
    still-`'pending'` post — there is no separate rejected status
    — and the moderation log records which of the two actually
    happened, distinguished by the post's status at the moment of
    deletion.

    Refuses with a `PostError` (GitHub issue #37), rather than letting
    SQLite's FK constraint raise `sqlite3.IntegrityError`, if this post
    is still referenced by another row as a reply's `parent_post_id`,
    an edit chain's `root_post_id`, or a later edit's `edit_of_post_id`
    -- the same three relationships `_sweep_expired_posts`
    already excludes from its own hard-delete step, for the same
    reason: changing those FKs' `ON DELETE` behavior needs the
    drop/rebuild migration pattern, which risks cascading
    SQLite's implicit `DELETE FROM` to *all* of a live parent table's
    relationships at once. A referenced post simply can't be deleted
    until whatever references it is gone first -- an explicit, catchable
    refusal rather than a session-crashing exception.
    """
    _refuse_if_board_hidden(db, post.board_id)
    _require_board_permission(db, post, deleted_by, BoardPermission.DELETE)

    blockers = db.connection.execute(
        """
        SELECT
            EXISTS(SELECT 1 FROM posts WHERE parent_post_id = ?) AS has_reply,
            EXISTS(SELECT 1 FROM posts WHERE root_post_id = ? AND post_id != ?) AS has_edit,
            EXISTS(SELECT 1 FROM posts WHERE edit_of_post_id = ?) AS has_later_edit
        """,
        (post.post_id, post.post_id, post.post_id, post.post_id),
    ).fetchone()
    if blockers["has_reply"] or blockers["has_edit"] or blockers["has_later_edit"]:
        reasons = []
        if blockers["has_reply"]:
            reasons.append("has one or more replies")
        if blockers["has_edit"]:
            reasons.append("is the root of an edit chain with other revisions")
        if blockers["has_later_edit"]:
            reasons.append("is referenced as the predecessor of a later edit")
        raise PostError("cannot delete this post: it " + ", and ".join(reasons))

    action = "reject" if post.status == "pending" else "delete"
    current = db.connection.execute("SELECT status FROM posts WHERE id = ?", (post.id,)).fetchone()
    if current is None or current["status"] != post.status:
        # Another moderator decided first -- approved it, or rejected it
        # already. A decision made on a stale copy must not delete an
        # approved post, nor record a rejection nobody made of it (Codex
        # review on #780).
        raise PostError("this post was already decided by another moderator")
    if action == "reject":
        # A rejection is recorded, not only carried out (issue #692): for a
        # carried post the signed event is kept, and without this record
        # `[R]epair carried posts` would publish the refused post again.
        # `reason` is optional and kept with it.
        db.connection.execute(
            "INSERT OR REPLACE INTO post_rejections (post_id, board_id, rejected_by_user_id, rejected_at, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (post.post_id, post.board_id, deleted_by.id, utc_now_iso(), reason),
        )
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
    reindex_post(db, post.board_id, post.root_post_id)
    if action == "reject":
        # The author is told, with the reason and -- by mail -- their text
        # (issue #678).
        record_moderation_outcome(db, post, outcome="rejected", moderator=deleted_by, reason=reason)


_TOMBSTONE_PLACEHOLDER = "[removed by moderator]"


def tombstone_post(db: Database, post: Post, board: Board, *, tombstoned_by: User) -> Post:
    """
    Redact `post` to a placeholder revision (design doc §9.5, issue
    #88) rather than deleting its row outright. Requires `tombstoned_by`
    to hold `BoardPermission.DELETE`, no author bypass -- the same
    authority `delete_post` already requires, just expressed as a
    further chain revision instead of a row removal, which is what lets
    a Linked board propagate it (`netbbs.link.boards.queue_board_post_
    tombstone_if_linked`) without breaking the edit chain a
    `board_post_tombstone` must extend, or orphaning a reply's
    `parent_post_id` the way an in-place delete would.

    Like `edit_post`, always re-resolves the actual current approved
    revision via `post.root_post_id` rather than trusting `post.
    post_id`. Refuses with a `PostError` if that revision is already
    tombstoned (a chain accepts at most one terminal tombstone, mirrored
    by `netbbs.link.protocol.LinkNode.handle_events`' own rejection of a
    second `board_post_tombstone` for the same root post).
    """
    _refuse_if_board_hidden(db, post.board_id)
    _require_board_permission(db, post, tombstoned_by, BoardPermission.DELETE)

    current = db.connection.execute(
        """
        SELECT * FROM posts
        WHERE root_post_id = ? AND board_id = ? AND status = 'approved'
        ORDER BY id DESC
        LIMIT 1
        """,
        (post.root_post_id, board.id),
    ).fetchone()
    if current is None:
        raise PostError("no currently-approved version of this post exists to tombstone")
    if current["tombstoned_at"] is not None:
        raise PostError("this post has already been tombstoned")

    created_at = utc_now_iso()
    author_identifier = current["author_fingerprint"] or current["author_label"]
    new_post_id = compute_content_id(
        {
            "type": "board_post_tombstone",
            "board_id": board.board_id,
            "parent_post_id": current["parent_post_id"],
            "author": author_identifier,
            "created_at": created_at,
        }
    )

    db.connection.execute(
        """
        INSERT INTO posts
            (post_id, board_id, parent_post_id, author_user_id, author_label,
             author_fingerprint, subject, body, created_at, status,
             root_post_id, edit_of_post_id, tombstoned_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'approved', ?, ?, ?)
        """,
        (
            new_post_id, board.id, current["parent_post_id"], current["author_user_id"],
            current["author_label"], current["author_fingerprint"],
            _TOMBSTONE_PLACEHOLDER, _TOMBSTONE_PLACEHOLDER, created_at,
            post.root_post_id, current["post_id"], created_at,
        ),
    )
    db.connection.commit()

    record_action(
        db,
        actor=tombstoned_by,
        action="tombstone",
        object_type="board",
        object_id=board.id,
        target_user_id=current["author_user_id"],
        detail=new_post_id,
    )
    reindex_post(db, board.id, post.root_post_id)
    return get_post(db, new_post_id)


def _refuse_if_withdrawn(db: Database, post: Post) -> None:
    """A withdrawn post is not pinned or kept either (issue #675): the
    withdrawal cleared both, and setting them again would put the
    placeholder back at the top of the board or keep it past its expiry
    (Codex review on #789). Unpinning or un-keeping stays allowed."""
    current = db.connection.execute(
        "SELECT withdrawn FROM posts WHERE root_post_id = ? AND board_id = ? AND status = 'approved' "
        "ORDER BY id DESC LIMIT 1",
        (post.root_post_id, post.board_id),
    ).fetchone()
    if current is not None and current["withdrawn"]:
        raise PostError("this post has been withdrawn by its author")


def _refuse_if_removed(db: Database, post: Post) -> None:
    """A removed post is neither pinned nor kept (issue #675): removal
    clears both, and a reader left open since must not set them again on
    the placeholder (Codex review on #783)."""
    removed = db.connection.execute(
        "SELECT 1 FROM posts WHERE root_post_id = ? AND board_id = ? AND tombstoned_at IS NOT NULL LIMIT 1",
        (post.root_post_id, post.board_id),
    ).fetchone()
    if removed is not None:
        raise PostError("this post has been removed")


def set_post_pinned(db: Database, post: Post, pinned: bool, *, changed_by: User) -> Post:
    """
    Pin or unpin a post within its own board's listing — a distinct
    concept from `netbbs.boards.boards.Board.pinned` (which board
    sorts first among *all* boards). Requires `BoardPermission.EDIT`,
    per the existing pin/exempt-under-`edit` sign-off note.

    A pinned post is listed first on the page a board opens on
    (`list_posts_page(with_pinned=True)`, issue #675).

    The flag belongs to the post, not to one revision: every row of the
    edit chain is set, and a later revision takes its root's flag
    (`trg_posts_revision_flags`).
    """
    _refuse_if_board_hidden(db, post.board_id)
    _require_board_permission(db, post, changed_by, BoardPermission.EDIT)
    _refuse_if_removed(db, post)
    if pinned:
        _refuse_if_withdrawn(db, post)

    db.connection.execute(
        "UPDATE posts SET pinned = ? WHERE root_post_id = ? AND board_id = ?",
        (int(pinned), post.root_post_id, post.board_id),
    )
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
    sign-off note.

    Set on every revision of the post, as `set_post_pinned` does (issue
    #675): the sweep ages rows one by one, and an exempt post whose edit
    was not exempt used to fall back to its pre-edit text when the edit
    expired."""
    _refuse_if_board_hidden(db, post.board_id)
    _require_board_permission(db, post, changed_by, BoardPermission.EDIT)
    _refuse_if_removed(db, post)
    if exempt:
        _refuse_if_withdrawn(db, post)

    db.connection.execute(
        "UPDATE posts SET exempt_from_expiry = ? WHERE root_post_id = ? AND board_id = ?",
        (int(exempt), post.root_post_id, post.board_id),
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


# The moderation queue's order, oldest first by instant (issue #678, Codex
# review on #795): a carried item's `created_at` is its origin's and may
# carry an offset, so text order is not time order. `julianday` reads both
# spellings; an unreadable time sorts last, and `id` breaks ties.
PENDING_ORDER_SQL = "julianday(created_at) IS NULL, julianday(created_at), id"


def list_pending_posts(
    db: Database, board: Board, *, requesting_user: User, limit: int | None = None
) -> list[Post]:
    """
    The moderation queue for `board`: every pending post if
    `requesting_user` holds `BoardPermission.APPROVE`, otherwise only
    their own pending posts (so an author isn't left wondering where
    their own submission went).

    Deliberately not cursor-paginated like `list_posts_page` —
    moderation queues are expected to be much smaller than full board
    history, and this keeps that already-intricate pagination code
    untouched.

    `limit` caps how many are read, oldest first (issue #678): held
    content can be carried in from other nodes without end, and a screen
    showing the queue must not read all of it.
    """
    cap = -1 if limit is None else limit
    if has_permission(
        db, requesting_user, object_type="board", object_id=board.id, permission=BoardPermission.APPROVE
    ):
        rows = db.connection.execute(
            f"SELECT * FROM posts WHERE board_id = ? AND status = 'pending' ORDER BY {PENDING_ORDER_SQL} LIMIT ?",
            (board.id, cap),
        ).fetchall()
    else:
        rows = db.connection.execute(
            f"""
            SELECT * FROM posts WHERE board_id = ? AND status = 'pending' AND author_user_id = ?
            ORDER BY {PENDING_ORDER_SQL} LIMIT ?
            """,
            (board.id, requesting_user.id, cap),
        ).fetchall()
    return [_row_to_post(row) for row in rows]


def list_node_pending_posts(db: Database, *, requesting_user: User, limit: int) -> list[Post]:
    """The oldest `limit` held posts on every board, for the SysOp's
    node-wide queue (issue #678) -- one bounded query however many boards
    the node carries (Codex review on #795). SysOp only: a SysOp may
    decide on every board, so no board is left out."""
    require_level(requesting_user, SYSOP_LEVEL)
    # A board this node excluded from a carried Link keeps its rows, but no
    # screen lists it: its held posts must not take the queue's places
    # (Codex review on #795).
    rows = db.connection.execute(
        f"""
        SELECT * FROM posts WHERE status = 'pending'
          AND board_id IN (SELECT id FROM boards WHERE link_hidden_at IS NULL)
        ORDER BY {PENDING_ORDER_SQL} LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [_row_to_post(row) for row in rows]


def revision_for_moderation(db: Database, post_id: str) -> Post | None:
    """The newest approved or expired revision of the post `post_id`
    belongs to: what its readers see, or last saw. A moderator deciding on
    a held reply or edit judges it against this (issue #678). Unlike
    `visible_post`, expiry doesn't hide it (Codex review on #795): a held
    edit approved after the post expired brings it back, and the moderator
    must see what it replaces."""
    row = db.connection.execute(
        "SELECT root_post_id, board_id FROM posts WHERE post_id = ?", (post_id,)
    ).fetchone()
    if row is None:
        return None
    latest = db.connection.execute(
        """
        SELECT * FROM posts WHERE root_post_id = ? AND board_id = ? AND status IN ('approved', 'expired')
        ORDER BY id DESC LIMIT 1
        """,
        (row["root_post_id"], row["board_id"]),
    ).fetchone()
    return _row_to_post(latest) if latest is not None else None


# At most this many pinned posts are listed at once. Pins are set by this
# node's moderators only, never carried; a pin past the page's share is
# still in the dated feed.
MAX_PINNED_POSTS = 50


def list_pinned_posts(
    db: Database, board: Board, *, requesting_user: User, limit: int = MAX_PINNED_POSTS
) -> list[Post]:
    """
    Up to `limit` pinned posts on `board` that a reader may see, oldest
    first, each resolved to its current revision.

    "May see" is the feed's rule (`_visible_roots`): some revision is
    approved and the post is not hidden by trust. Reading the board at
    all is checked here too, as `list_posts_page` checks it: the effective
    read level through the Community cascade, and the minimum age.
    """
    _require_board_readable(db, board, requesting_user)
    # Batched past trust-hidden roots, as `_visible_roots` is (Codex review
    # on #783): a hidden pin must not take a visible one's place.
    found: list[Post] = []
    author_cache: dict = {}
    after: PostCursor | None = None
    while len(found) < limit:
        position_sql = "AND (root.created_at, root.post_id) > (?, ?)" if after is not None else ""
        rows = db.connection.execute(
            f"""
            SELECT root.*, e.envelope_json AS link_envelope_json FROM posts root
            LEFT JOIN link_events e ON e.content_id = root.post_id
            WHERE root.board_id = ? AND root.pinned = 1 AND root.post_id = root.root_post_id
              {position_sql}
              AND {_HAS_APPROVED_VERSION_SQL}
            ORDER BY root.created_at, root.post_id
            LIMIT ?
            """,
            (board.id, *(after or ()), _VISIBLE_ROOTS_BATCH),
        ).fetchall()
        for row in rows:
            envelope_json = row["link_envelope_json"]
            if envelope_json is None or envelope_content_visible(db, envelope_json, author_cache=author_cache):
                found.append(_resolve_current_version(db, row))
                if len(found) == limit:
                    break
        if len(rows) < _VISIBLE_ROOTS_BATCH:
            break
        after = (rows[-1]["created_at"], rows[-1]["post_id"])
    return found


# The most recent revisions a history lists. A carried post's chain is
# written by another node, so its length is not this node's to bound.
MAX_LISTED_REVISIONS = 50

# The Link event type of an origin's moderator edit (`netbbs.link.events`),
# spelled here rather than imported: that module depends on this one.
_MODERATOR_EDIT_EVENT = "board_post_moderator_edit"


@dataclass(frozen=True)
class Revision:
    """One version of a post, as `list_post_revisions` lists it: the
    revision row exactly as stored, and whether a moderator wrote it
    rather than the post's author."""

    post: Post
    by_moderator: bool


def list_post_revisions(db: Database, post: Post, board: Board, *, requesting_user: User) -> list[Revision]:
    """
    Every version of `post` (issue #675), oldest first: the approved
    revisions of its chain, the newest `MAX_LISTED_REVISIONS` of them.

    For moderators only (decided with the maintainer): `requesting_user`
    must hold `BoardPermission.EDIT` on the board, the permission a
    moderator edit needs; anyone else gets `PostError`. A moderator sees
    every version, those of a removed or withdrawn post included.

    In local receipt order (`id`), which is the chain's order -- see
    `_resolve_current_version` -- and never `created_at`, another node's
    clock. The read itself is bounded to the tail: a carried chain's
    length is set by another node (Codex review on #789). Expired and
    pending revisions are left out, and so is the removal placeholder.
    Expiry is swept first: it is applied lazily, and a version may pass
    its age while the reader is open.
    """
    _require_board_permission(db, post, requesting_user, BoardPermission.EDIT)
    _sweep_expired_posts(db, board)
    rows = db.connection.execute(
        """
        SELECT * FROM posts
        WHERE root_post_id = ? AND board_id = ? AND status = 'approved' AND tombstoned_at IS NULL
        ORDER BY id DESC
        LIMIT ?
        """,
        (post.root_post_id, post.board_id, MAX_LISTED_REVISIONS),
    ).fetchall()
    return [Revision(_row_to_post(row), _is_moderator_revision(db, row)) for row in reversed(rows)]


def _is_moderator_revision(db: Database, row: sqlite3.Row) -> bool:
    """Whether a moderator wrote this revision rather than the post's
    author: an origin's carried moderator edit is its own event type, and
    a local edit is logged with who made it."""
    if row["post_id"] == row["root_post_id"]:
        return False
    carried = db.connection.execute(
        "SELECT 1 FROM link_events WHERE content_id = ? AND object_type = ?",
        (row["post_id"], _MODERATOR_EDIT_EVENT),
    ).fetchone()
    if carried is not None:
        return True
    return db.connection.execute(
        """
        SELECT 1 FROM moderation_log
        WHERE action = 'edit' AND object_type = 'board' AND detail = ?
          AND actor_user_id IS NOT target_user_id
        LIMIT 1
        """,
        (row["post_id"],),
    ).fetchone() is not None


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


def sweep_expired_posts(db: Database, board: Board) -> None:
    """Bring `board`'s expiry state up to date. Expiry is swept lazily, on
    the reads that show a board; any other surface that reports on posts
    -- an unread count on the board list or in [N]ew scan -- runs this
    first, or it counts posts past their age that the board itself would
    no longer show (Codex review on #719)."""
    _sweep_expired_posts(db, board)


def _sweep_expired_posts(db: Database, board: Board) -> None:
    """
    Lazily bring `board`'s post statuses up to date: age `'approved'`
    posts past `board.max_post_age_days` into `'expired'`, then
    hard-delete any already-`'expired'` post whose grace period has
    also elapsed. `exempt_from_expiry` posts are skipped by both
    steps.

    Runs at the top of `list_posts_page` — the natural "someone is
    looking at this board" trigger — rather than via a background job,
    since none exists anywhere in this codebase. A no-op
    whenever `board.max_post_age_days` is `None` (retain indefinitely,
    the default). Not logged to `netbbs.moderation.log` — that log is
    for explicit human moderation decisions, not mechanical time-based
    housekeeping.

    The delete step deliberately excludes any post still referenced by
    another live row — as a reply's `parent_post_id`, an edit chain's
    `root_post_id`, or a later edit's `edit_of_post_id`; this predates
    and is independent of post editing. `posts.parent_post_id
    REFERENCES posts(post_id)` with no `ON DELETE` clause means SQLite
    raises `FOREIGN KEY constraint failed` on any attempt to delete a
    still-referenced row — reproducible against a plain reply alone,
    with no edit-chain columns involved.
    Changing that FK's `ON DELETE` behavior would need the drop/rebuild
    migration pattern used elsewhere, and rebuilding `posts` specifically
    — a live parent of several other tables' own foreign keys — risks
    SQLite's `DROP TABLE` applying its own cascade/SET-NULL side
    effects to *all* of those relationships at once, not just the one
    column being fixed. Handling it here instead, application-level,
    is the same choice made for board/area/category deletion
    for exactly that reason: a referenced post simply stays in
    `'expired'` status indefinitely rather than being purged — already
    a valid, harmless state (delisted from browsing, still individually
    reachable via `get_post`), not a new one this introduces.
    """
    if board.max_post_age_days is None:
        return

    expiry_cutoff = _cutoff_iso(board.max_post_age_days)
    # Collected before either bulk statement runs below (issue #56's
    # search index): both are set-based SQL, not a per-row Python loop,
    # so the roots each one touches have to be captured with the exact
    # same WHERE clause first -- reindex_post is then called once per
    # affected root afterward, since a bulk status flip or hard-delete
    # could shift, or remove entirely, which revision is the currently
    # "resolved current" one for post_search.
    expiring_roots = {
        row["root_post_id"]
        for row in db.connection.execute(
            """
            SELECT DISTINCT root_post_id FROM posts
            WHERE board_id = ? AND status = 'approved' AND exempt_from_expiry = 0
                  AND created_at < ?
            """,
            (board.id, expiry_cutoff),
        ).fetchall()
    }
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
    _deletable_where = """
        board_id = ? AND status = 'expired' AND exempt_from_expiry = 0
              AND created_at < ?
              AND NOT EXISTS (
                  SELECT 1 FROM posts child
                  WHERE child.post_id != posts.post_id
                    AND (child.parent_post_id = posts.post_id
                         OR child.root_post_id = posts.post_id
                         OR child.edit_of_post_id = posts.post_id)
              )
    """
    deleting_roots = {
        row["root_post_id"]
        for row in db.connection.execute(
            f"SELECT DISTINCT root_post_id FROM posts WHERE {_deletable_where}",
            (board.id, deletion_cutoff),
        ).fetchall()
    }
    db.connection.execute(
        f"DELETE FROM posts WHERE {_deletable_where}",
        (board.id, deletion_cutoff),
    )
    db.connection.commit()

    for root_post_id in expiring_roots | deleting_roots:
        reindex_post(db, board.id, root_post_id)


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
        tombstoned_at=row["tombstoned_at"],
        is_edited=is_edited,
        # Absent on a schema older than issue #711's migration.
        layout=row["layout"] if "layout" in row.keys() else "prose",
        # Absent on a schema older than issue #675's migration.
        withdrawn=bool(row["withdrawn"]) if "withdrawn" in row.keys() else False,
    )
