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
from netbbs.file_refs import forget_orphaned_post_refs_without_commit
from netbbs.moderation.log import record_action
from netbbs.storage.database import Database
from netbbs.age_requirement import UNCHANGED, check_age_requirement, row_age_requirement, store_age_requirement
from netbbs.timeutil import utc_now_iso

# Supported list_boards() sort orders. "sysop" -- the SysOp's own order,
# `position` -- is the default (issue #839): under "activity", the default
# before it, a list re-sorted itself between two visits, so the "03" a
# caller remembered meant a different board a minute later. The SysOp's
# order is also what fixes the problem creation order had (a politics
# board created between two batches of vintage-computing boards sat in
# the middle of them): the SysOp moves it. "volume" (total post count) is a genuinely different signal
# from "activity" (most recent post) -- a board with one post today but
# otherwise dead ranks high under activity but low under volume; a board
# with huge historical traffic but nothing new today is the reverse.
# Per-user sort preference (netbbs.sort_preferences, design doc) now
# resolves which of these a given caller actually passes -- this
# remains the node-wide default a caller falls back to before any
# per-user resolution, and what a caller passes when it has none to
# resolve against (e.g. an admin listing with no requesting user).
_VALID_SORT_ORDERS = ("sysop", "activity", "alphabetical", "recent", "volume")


def _check_max_post_age(max_post_age_days: int | None) -> None:
    """A maximum age is a whole number of days, at least one. Zero would
    expire every post on the next browse, and a negative value moves the
    deletion cutoff (`age + grace`) into the past, hard-deleting
    unreferenced posts almost at once. `None` means posts never expire."""
    if max_post_age_days is not None and max_post_age_days < 1:
        raise BoardError(f"maximum post age must be at least 1 day, got {max_post_age_days}")


def usable_max_age_days(value: object) -> int | None:
    """A Link genesis's recommended maximum age as this node can store it.

    The value comes from a remote origin and is unvalidated on the wire.
    Anything but a whole number of at least one day is dropped to `None`
    (no expiry) rather than refusing the genesis: a bad recommendation
    must not cost this node the board, and must not reach the sweep,
    where 0 or a negative age deletes posts (see `_check_max_post_age`).
    Shared with file-area genesis, whose `max_file_age_days` has the
    same rule."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return None


class BoardError(Exception):
    """Raised for board creation/lookup failures."""


@dataclass(frozen=True)
class Board:
    id: int
    board_id: str
    name: str
    description: str | None
    # Nullable (design doc §16): NULL means
    # "inherit this Community's default, or the system default of 0 if
    # this board has no Community" -- see
    # netbbs.communities.get_effective_min_read_level/
    # get_effective_min_write_level. An explicit stored value, including
    # 0, always wins outright over any Community default.
    min_read_level: int | None
    min_write_level: int | None
    category_id: int | None
    pinned: bool
    created_at: str
    moderated: bool
    max_post_age_days: int | None
    # Age/name-gating (design doc §18) -- nullable,
    # NULL means no gate *and* (§16) "inherit this
    # Community's default" if this board belongs to one. Enforced
    # alongside min_read_level/min_write_level wherever those already
    # are; see netbbs.net.login_flow's board-browsing/posting checks.
    min_age: int | None
    name_requirement: str | None  # None | "verified" | "verified_and_displayed"
    # Zero-or-one, nullable FK (design doc §16) -- a board
    # never belongs to more than one Community; NULL is a real,
    # distinct, common state ("Uncategorized"), not a fallback.
    community_id: int | None
    # Whether posts here show their author's color (issue #711,
    # `netbbs.rendering.post_body`). This node's own choice, carried
    # boards included.
    allow_color: bool = False
    # The SysOp's order (issue #839): `list_boards`' default "sysop" sort
    # follows it, and `move_board` changes it. A new board goes last (a
    # trigger sets it, for carried boards too).
    position: int = 0
    # How `min_age` accepts an age (issue #1082, `netbbs.age_requirement`):
    # None inherits the Community's default, "verified" wants an age
    # attestation rather than a self-entered birthdate.
    age_requirement: str | None = None


def create_board(
    db: Database,
    name: str,
    *,
    description: str | None = None,
    min_read_level: int | None = 0,
    min_write_level: int | None = 0,
    category_id: int | None = None,
    pinned: bool = False,
    moderated: bool = False,
    max_post_age_days: int | None = None,
    min_age: int | None = None,
    name_requirement: str | None = None,
    community_id: int | None = None,
    allow_color: bool = False,
    age_requirement: str | None = None,
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

    `min_age`/`name_requirement` (design doc §18) are the
    same nullable-means-no-gate shape as everything else here — see
    `netbbs.attestation.meets_age`/`meets_name_requirement` for the
    actual check, enforced by callers alongside `min_read_level`/
    `min_write_level` rather than inside this function.

    `allow_color` (issue #711) lets posts here show the color their
    authors put in them; off by default.

    `min_read_level`/`min_write_level` are also nullable (design doc
    §16) -- `None` means inherit `community_id`'s
    Community default (or the system default of 0 if `community_id` is
    also `None`), while the default of `0` here preserves this
    function's original always-explicit behavior for every existing
    caller. `community_id` optionally places the board under
    a `netbbs.communities.Community` -- zero-or-one, same shape as
    `category_id` but the outer layer, not a replacement for it.

    No permission check on *creating* a board here — board creation is an
    admin-level action with no SysOp/moderator concept defined yet in
    Phase 1; gating who's allowed to call this is left to whatever calls
    it (a future admin tool), not baked in here.
    """
    if name_requirement not in (None, "verified", "verified_and_displayed"):
        raise BoardError(f"invalid name_requirement: {name_requirement!r}")
    check_age_requirement(age_requirement, BoardError)
    _check_max_post_age(max_post_age_days)
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
                 min_age, name_requirement, community_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                community_id,
            ),
        )
        if allow_color:
            # Set apart from the INSERT, which stays valid on every schema
            # a board can be created on.
            db.connection.execute("UPDATE boards SET allow_color = 1 WHERE board_id = ?", (board_id,))
        if age_requirement is not None:
            db.connection.execute("UPDATE boards SET age_requirement = ? WHERE board_id = ?", (age_requirement, board_id))
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        if _name_held_by_hidden(db, name):
            raise BoardError(f"the name {name!r} is held by a Link resource excluded from this node (Link status -> Excluded): restore or purge it there first") from exc
        raise BoardError(f"could not create board {name!r} — name already in use?") from exc

    new_board = _read_back_by_name(db, name)
    record_action(
        db, actor=creator, action="create_board", object_type="board", object_id=new_board.id,
        detail=f"created board {name!r}",
    )
    return new_board


def _name_held_by_hidden(db: Database, name: str) -> bool:
    """Issue #683: whether a hidden (excluded) carried board holds `name` --
    it stays taken while hidden, and a SysOp must be told why."""
    return db.connection.execute(
        "SELECT 1 FROM boards WHERE name = ? AND link_hidden_at IS NOT NULL", (name,)
    ).fetchone() is not None


def _read_back_by_name(db: Database, name: str):
    """A row this module has just written, read back by name. Not filtered on
    `link_hidden_at` (issue #683): a row just created or renamed is never
    hidden, and this keeps the write paths free of the newer column."""
    row = db.connection.execute("SELECT * FROM boards WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise BoardError(f"no such board: {name!r}")
    return _row_to_board(row)


def get_board_by_name(db: Database, name: str) -> Board:
    # Issue #683: a hidden (excluded) carried board is invisible here and in
    # `list_boards`, which every caller-facing and admin listing goes through.
    row = db.connection.execute(
        "SELECT * FROM boards WHERE name = ? AND link_hidden_at IS NULL", (name,)
    ).fetchone()
    if row is None:
        raise BoardError(f"no such board: {name!r}")
    return _row_to_board(row)


def list_boards(db: Database, *, order_by: str = "sysop") -> list[Board]:
    """
    List all boards. Pinned boards always sort first, then the rest in
    the chosen `order_by`:

      - "sysop" (default, issue #839): the SysOp's order, `position`,
        which `move_board` changes. A new board goes last.
      - "activity": most recent *approved* post first (a
        board with no approved posts yet falls back to its own creation
        time). Pending and expired posts don't count -- ranking a board
        as active from content ordinary readers can't even see would
        leak that hidden activity exists. An edit does count as fresh
        activity even though it deliberately doesn't move its post's
        own position within the board's own feed -- those are different
        concerns at different
        granularities: intra-board feed position vs. board-list
        activity ranking (GitHub issue #36).
      - "alphabetical": by name, case-insensitive.
      - "recent": newest board first, by the board's own creation
        time -- unlike "activity", not affected by anything that
        happens inside the board after it's created.
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
    computed inline: `<the later of the revision's and its post's original
    created_at, as a julianday> >= julianday(now) - max_post_age_days`
    (issue #793) is the same "not yet past its age limit" test the
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

    if order_by == "sysop":
        rows = db.connection.execute(
            "SELECT * FROM boards ORDER BY pinned DESC, position ASC, id ASC"
        ).fetchall()
    elif order_by == "alphabetical":
        rows = db.connection.execute(
            "SELECT * FROM boards ORDER BY pinned DESC, name COLLATE NOCASE ASC"
        ).fetchall()
    elif order_by == "recent":
        rows = db.connection.execute(
            "SELECT * FROM boards ORDER BY pinned DESC, created_at DESC"
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
                                OR MAX(julianday(v.created_at), julianday(COALESCE((SELECT origin.created_at FROM posts origin WHERE origin.post_id = v.root_post_id), v.created_at))) >= julianday(?) - b.max_post_age_days
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
                      OR MAX(julianday(p.created_at), julianday(COALESCE((SELECT origin.created_at FROM posts origin WHERE origin.post_id = p.root_post_id), p.created_at))) >= julianday(?) - b.max_post_age_days
                )
            GROUP BY b.id
            ORDER BY b.pinned DESC, last_activity DESC
            """,
            (now,),
        ).fetchall()

    return [_row_to_board(row) for row in rows if row["link_hidden_at"] is None]


def update_board(
    db: Database,
    board: Board,
    *,
    name: str,
    description: str | None,
    min_read_level: int | None,
    min_write_level: int | None,
    category_id: int | None,
    pinned: bool,
    moderated: bool,
    max_post_age_days: int | None,
    min_age: int | None,
    name_requirement: str | None,
    community_id: int | None,
    allow_color: bool,
    age_requirement=UNCHANGED,
    changed_by: User,
) -> Board:
    """
    Replace `board`'s editable settings with the given full state --
    every field is required, not a partial/PATCH-style update; the admin
    UI is responsible for pre-filling a caller's edits with the board's
    current values as defaults, keeping this function itself simple.
    `board_id`/`created_at` are immutable, not accepted here.

    `min_age`/`name_requirement` follow design doc §18 --
    see `create_board`'s docstring. `min_read_level`/`min_write_level`
    (nullable, §16) and `community_id` (§16) follow
    that same docstring's Community-inheritance reasoning.
    """
    if name_requirement not in (None, "verified", "verified_and_displayed"):
        raise BoardError(f"invalid name_requirement: {name_requirement!r}")
    if age_requirement is not UNCHANGED:
        check_age_requirement(age_requirement, BoardError)
    _check_max_post_age(max_post_age_days)
    try:
        if age_requirement is not UNCHANGED:
            store_age_requirement(db, "boards", board.id, age_requirement)
        db.connection.execute(
            """
            UPDATE boards
            SET name = ?, description = ?, min_read_level = ?, min_write_level = ?,
                category_id = ?, pinned = ?, moderated = ?, max_post_age_days = ?,
                min_age = ?, name_requirement = ?, community_id = ?, allow_color = ?
            WHERE id = ?
            """,
            (
                name, description, min_read_level, min_write_level,
                category_id, int(pinned), int(moderated), max_post_age_days,
                min_age, name_requirement, community_id, int(allow_color), board.id,
            ),
        )
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        if _name_held_by_hidden(db, name):
            raise BoardError(f"the name {name!r} is held by a Link resource excluded from this node (Link status -> Excluded): restore or purge it there first") from exc
        raise BoardError(f"could not update board {board.name!r} — name already in use?") from exc

    updated = _read_back_by_name(db, name)
    record_action(
        db, actor=changed_by, action="update_board", object_type="board", object_id=board.id,
        detail=f"updated board {board.name!r}",
    )
    return updated


def board_siblings(db: Database, board: Board) -> list[Board]:
    """The boards `board` is ordered among, in order, itself included:
    those in the same category and the same Community, with the same
    pinned flag. A caller's list shows one category at a time, pinned
    boards first, and a Community's list only its own boards, so swapping
    with one of these changes what every list holding both shows."""
    return [
        b for b in list_boards(db, order_by="sysop")
        if b.category_id == board.category_id
        and b.community_id == board.community_id
        and b.pinned == board.pinned
    ]


def move_board(db: Database, board: Board, offset: int, *, moved_by: User) -> bool:
    """Move `board` one place earlier (`offset` -1) or later (+1) among
    `board_siblings` (issue #839), by swapping `position` with the
    neighbour. Returns whether it moved: the first cannot go up, nor the
    last down."""
    siblings = board_siblings(db, board)
    ids = [b.id for b in siblings]
    if board.id not in ids:
        raise BoardError(f"no such board: {board.name!r}")
    index = ids.index(board.id)
    target = index + offset
    if offset not in (-1, 1) or not 0 <= target < len(ids):
        return False
    here, there = siblings[index], siblings[target]
    db.connection.execute("UPDATE boards SET position = ? WHERE id = ?", (there.position, here.id))
    db.connection.execute("UPDATE boards SET position = ? WHERE id = ?", (here.position, there.id))
    db.connection.commit()
    record_action(
        db, actor=moved_by, action="move_board", object_type="board", object_id=board.id,
        detail=f"moved board {board.name!r} to place {target + 1} of {len(ids)}",
    )
    return True


def delete_board(db: Database, board: Board, *, deleted_by: User) -> None:
    """
    Permanently remove `board`, along with its posts, any moderator
    grants scoped to it,
    and any per-user read-cursor/follow rows for it (issue #56).

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
    "log first" reasoning.
    """
    record_action(
        db, actor=deleted_by, action="delete_board", object_type="board", object_id=board.id,
        detail=f"deleted board {board.name!r} (id {board.id})",
    )
    db.connection.execute("DELETE FROM posts WHERE board_id = ?", (board.id,))
    forget_orphaned_post_refs_without_commit(db)
    db.connection.execute(
        "DELETE FROM moderator_grants WHERE object_type = 'board' AND object_id = ?", (board.id,)
    )
    db.connection.execute(
        "DELETE FROM user_read_cursors WHERE object_type = 'board' AND object_id = ?", (board.id,)
    )
    db.connection.execute(
        "DELETE FROM user_follows WHERE object_type = 'board' AND object_id = ?", (board.id,)
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
        community_id=row["community_id"],
        # Absent on a schema older than issue #711's migration.
        allow_color=bool(row["allow_color"]) if "allow_color" in row.keys() else False,
        # Absent on a schema older than issue #839's migration.
        position=row["position"] if "position" in row.keys() else 0,
        age_requirement=row_age_requirement(row),
    )
