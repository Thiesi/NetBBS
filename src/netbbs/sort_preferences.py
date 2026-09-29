"""
Per-user sort-mode preference for the channel/board/file-area pickers
(design doc, dogfood feature request), with Community- and
category-scoped overrides layered on top of a per-resource-kind global
default -- three specificity levels resolved by
`get_effective_sort_mode`, same cascading-preference shape as (for
example) git's system/global/local config layers or CSS specificity,
just three levels deep rather than open-ended.

Deliberately its own top-level module, not nested under
`netbbs.boards`/`netbbs.chat`/`netbbs.files`: it spans all three
resource kinds, the same reasoning `netbbs.activity`'s `user_follows`
wrapper (`is_following`/`follow`/`unfollow`/`list_followed`) and
`netbbs.moderation`'s `object_type`/`object_id` polymorphism already
established for cross-kind per-user state.

`resource_kind` disambiguates `category_id`: `netbbs.boards.categories`/
`netbbs.chat.categories`/`netbbs.files.categories` are three separate
tables with independent id sequences, so a bare `category_id` alone is
ambiguous the same way `object_id` alone already is for
`moderator_grants`/`user_follows`.
"""

from __future__ import annotations

from dataclasses import dataclass

from netbbs.auth.users import User
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

# Every resource kind this module covers, and every sort mode a
# picker's [O]rder command can offer. "volume" means a different
# underlying signal per kind (post/file count for boards/file areas,
# live participant count for channels, since chat has no persisted
# history to count) -- see each list_*'s own order_by docstring for the
# exact per-kind meaning; this module only stores/resolves the mode
# name, never computes it.
VALID_RESOURCE_KINDS = ("channel", "board", "file_area")
VALID_SORT_MODES = ("sysop", "activity", "alphabetical", "recent", "volume")
# "sysop" -- the SysOp's own order (issue #839) -- exists for boards and
# file areas only: channels have no `position` to follow.
_MODES_BY_KIND: dict[str, tuple[str, ...]] = {
    "channel": ("activity", "alphabetical", "recent", "volume"),
    "board": VALID_SORT_MODES,
    "file_area": VALID_SORT_MODES,
}

# The node-wide default when a user has never set any preference at any
# scope, per resource kind -- boards/file areas follow the SysOp's order
# (issue #839). They used to default to "activity", which re-sorted a
# list between two visits: the field test's caller came back to find
# "03" meant a different board. Channels default to
# "alphabetical" instead, deliberately *not* matching boards/areas:
# unlike them, a channel's "activity" is only ever `netbbs.chat.hub.
# ChatHub.last_activity` -- in-memory, per-node, reset on restart --
# so defaulting every user to it silently reintroduces the exact
# cross-node-disagreement dogfood bug fixed before this whole feature
# existed (`netbbs.net.chat_flow._pick_channel`'s own docstring).
# "activity" stays fully available as an explicit, opt-in choice --
# only the *unset* fallback differs.
DEFAULT_SORT_MODE_BY_KIND: dict[str, str] = {
    "channel": "alphabetical",
    "board": "sysop",
    "file_area": "sysop",
}


def _check_resource_kind(resource_kind: str) -> None:
    if resource_kind not in VALID_RESOURCE_KINDS:
        raise ValueError(f"resource_kind must be one of {VALID_RESOURCE_KINDS}, got {resource_kind!r}")


def _check_sort_mode(sort_mode: str) -> None:
    if sort_mode not in VALID_SORT_MODES:
        raise ValueError(f"sort_mode must be one of {VALID_SORT_MODES}, got {sort_mode!r}")


@dataclass(frozen=True)
class SortPreference:
    """One saved override row, as returned by `list_sort_preferences` --
    `community_id`/`category_id` are mutually exclusive and both `None`
    means this is the bare per-kind global default (see the migration's
    own `CHECK` for the same invariant enforced at the schema level)."""

    id: int
    resource_kind: str
    community_id: int | None
    category_id: int | None
    sort_mode: str
    created_at: str


def get_effective_sort_mode(
    db: Database,
    user: User,
    resource_kind: str,
    *,
    community_id: int | None = None,
    category_id: int | None = None,
) -> str:
    """
    Resolves `user`'s sort mode for `resource_kind`, most specific
    override first: a `category_id` match, then a `community_id` match,
    then the bare per-kind global default, then
    `DEFAULT_SORT_MODE_BY_KIND[resource_kind]` if the user has never
    set anything at any scope.

    Pass whichever of `community_id`/`category_id` describe *where* the
    picker being rendered actually is -- e.g. browsing boards inside
    Community "Retro Computing"'s "Amiga" category passes both (the
    category match wins if set; the community_id is only consulted as
    the fallback if it isn't). Passing neither resolves only the global
    default, appropriate for a top-level, uncategorized listing.
    """
    _check_resource_kind(resource_kind)

    if category_id is not None:
        row = db.connection.execute(
            "SELECT sort_mode FROM user_sort_preferences "
            "WHERE user_id = ? AND resource_kind = ? AND category_id = ?",
            (user.id, resource_kind, category_id),
        ).fetchone()
        if row is not None:
            return row["sort_mode"]

    if community_id is not None:
        row = db.connection.execute(
            "SELECT sort_mode FROM user_sort_preferences "
            "WHERE user_id = ? AND resource_kind = ? AND community_id = ?",
            (user.id, resource_kind, community_id),
        ).fetchone()
        if row is not None:
            return row["sort_mode"]

    row = db.connection.execute(
        "SELECT sort_mode FROM user_sort_preferences "
        "WHERE user_id = ? AND resource_kind = ? AND community_id IS NULL AND category_id IS NULL",
        (user.id, resource_kind),
    ).fetchone()
    if row is not None:
        return row["sort_mode"]

    return DEFAULT_SORT_MODE_BY_KIND[resource_kind]


def set_sort_preference(
    db: Database,
    user: User,
    resource_kind: str,
    sort_mode: str,
    *,
    community_id: int | None = None,
    category_id: int | None = None,
) -> None:
    """
    Saves `sort_mode` as `user`'s preference at exactly one scope:
    global (both `community_id`/`category_id` `None`), Community-wide
    (`community_id` only), or category-scoped (`category_id` only) --
    passing both raises, matching the migration's own `CHECK`.

    Upserts against whichever of the three partial unique indexes
    applies (`ON CONFLICT` needs the specific index, since SQLite can't
    infer which one a mixed-NULL row should match on its own).
    """
    _check_resource_kind(resource_kind)
    _check_sort_mode(sort_mode)
    if sort_mode not in _MODES_BY_KIND[resource_kind]:
        raise ValueError(f"sort_mode {sort_mode!r} does not apply to {resource_kind!r}")
    if community_id is not None and category_id is not None:
        raise ValueError("set_sort_preference takes at most one of community_id/category_id, not both")

    if category_id is not None:
        conflict_target = "(user_id, resource_kind, category_id) WHERE category_id IS NOT NULL"
    elif community_id is not None:
        conflict_target = "(user_id, resource_kind, community_id) WHERE community_id IS NOT NULL"
    else:
        conflict_target = "(user_id, resource_kind) WHERE community_id IS NULL AND category_id IS NULL"

    db.connection.execute(
        f"""
        INSERT INTO user_sort_preferences
            (user_id, resource_kind, community_id, category_id, sort_mode, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT{conflict_target} DO UPDATE SET sort_mode = excluded.sort_mode
        """,
        (user.id, resource_kind, community_id, category_id, sort_mode, utc_now_iso()),
    )
    db.connection.commit()


def clear_sort_preference(
    db: Database,
    user: User,
    resource_kind: str,
    *,
    community_id: int | None = None,
    category_id: int | None = None,
) -> None:
    """Removes the override at exactly one scope (same three-way shape
    as `set_sort_preference`) -- a no-op if none existed there."""
    _check_resource_kind(resource_kind)
    if community_id is not None and category_id is not None:
        raise ValueError("clear_sort_preference takes at most one of community_id/category_id, not both")

    if category_id is not None:
        db.connection.execute(
            "DELETE FROM user_sort_preferences "
            "WHERE user_id = ? AND resource_kind = ? AND category_id = ?",
            (user.id, resource_kind, category_id),
        )
    elif community_id is not None:
        db.connection.execute(
            "DELETE FROM user_sort_preferences "
            "WHERE user_id = ? AND resource_kind = ? AND community_id = ?",
            (user.id, resource_kind, community_id),
        )
    else:
        db.connection.execute(
            "DELETE FROM user_sort_preferences "
            "WHERE user_id = ? AND resource_kind = ? AND community_id IS NULL AND category_id IS NULL",
            (user.id, resource_kind),
        )
    db.connection.commit()


def list_sort_preferences(db: Database, user: User) -> list[SortPreference]:
    """Every override `user` has saved at any scope, for a review/
    management screen -- global rows first, then Community-scoped, then
    category-scoped, each group oldest-first. Resolving `community_id`/
    `category_id` to display names is the caller's job (this module has
    no dependency on `netbbs.communities`/the three category modules,
    the same layering `list_followed`'s own docstring already argues
    for: callers already have the real resource lists in hand)."""
    rows = db.connection.execute(
        """
        SELECT id, resource_kind, community_id, category_id, sort_mode, created_at
        FROM user_sort_preferences
        WHERE user_id = ?
        ORDER BY
            CASE
                WHEN community_id IS NULL AND category_id IS NULL THEN 0
                WHEN category_id IS NULL THEN 1
                ELSE 2
            END,
            created_at ASC
        """,
        (user.id,),
    ).fetchall()
    return [
        SortPreference(
            id=row["id"],
            resource_kind=row["resource_kind"],
            community_id=row["community_id"],
            category_id=row["category_id"],
            sort_mode=row["sort_mode"],
            created_at=row["created_at"],
        )
        for row in rows
    ]
