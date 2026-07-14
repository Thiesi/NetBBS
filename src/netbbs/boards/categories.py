"""
Board categories: at most two levels (a top-level category, optionally
containing sub-categories) — never deeper. Prompted by a real usability
problem Thiesi raised: a flat list of boards mixes unrelated topics
together (e.g. one politics board sitting in the middle of a dozen
vintage-computing boards), and full arbitrary-depth hierarchy was judged
more complexity than the benefit justifies for now — two levels covers
the realistic cases without needing breadcrumb navigation or recursive
depth handling.

The depth cap is enforced here, in application code, at creation time —
not as a database CHECK constraint, since SQLite can't express "does this
row's parent itself have a parent" (a self-join condition) as a plain
CHECK. A category being created with a parent is rejected if that parent
is itself a sub-category (has its own parent) — this is the only rule
that needs enforcing to guarantee at most two levels ever exist.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from netbbs.auth.users import User
from netbbs.moderation.log import record_action
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


class CategoryError(Exception):
    """Raised for category creation/lookup failures, including an
    attempted third level of nesting."""


@dataclass(frozen=True)
class Category:
    id: int
    name: str
    description: str | None
    parent_category_id: int | None
    created_at: str

    @property
    def is_top_level(self) -> bool:
        return self.parent_category_id is None


def create_category(
    db: Database,
    name: str,
    *,
    description: str | None = None,
    parent_category_id: int | None = None,
    created_by: User,
) -> Category:
    """
    Create a new board category, optionally as a sub-category of an
    existing top-level category.

    No permission check here — same precedent as board/channel creation
    elsewhere in Phase 1: an admin-level action with no SysOp/moderator
    concept defined yet, left to whatever calls this. `created_by` is
    only for the audit-log entry (design doc -- board/area management
    round) — this function itself still has no authorization check of
    its own.
    """
    if parent_category_id is not None:
        parent = get_category_by_id(db, parent_category_id)
        if not parent.is_top_level:
            raise CategoryError(
                f"cannot create a sub-category under {parent.name!r} — "
                f"it is itself a sub-category; only two levels are allowed"
            )

    created_at = utc_now_iso()
    try:
        db.connection.execute(
            """
            INSERT INTO board_categories (name, description, parent_category_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (name, description, parent_category_id, created_at),
        )
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        raise CategoryError(f"could not create category {name!r} — name already in use?") from exc

    new_category = get_category_by_name(db, name)
    record_action(
        db, actor=created_by, action="create_board_category", object_type="board_category",
        object_id=new_category.id, detail=f"created category {name!r}",
    )
    return new_category


def get_category_by_id(db: Database, category_id: int) -> Category:
    row = db.connection.execute(
        "SELECT * FROM board_categories WHERE id = ?", (category_id,)
    ).fetchone()
    if row is None:
        raise CategoryError(f"no such category id: {category_id!r}")
    return _row_to_category(row)


def get_category_by_name(db: Database, name: str) -> Category:
    row = db.connection.execute(
        "SELECT * FROM board_categories WHERE name = ?", (name,)
    ).fetchone()
    if row is None:
        raise CategoryError(f"no such category: {name!r}")
    return _row_to_category(row)


def list_top_level_categories(db: Database) -> list[Category]:
    rows = db.connection.execute(
        "SELECT * FROM board_categories WHERE parent_category_id IS NULL ORDER BY name"
    ).fetchall()
    return [_row_to_category(row) for row in rows]


def list_subcategories(db: Database, parent_category_id: int) -> list[Category]:
    rows = db.connection.execute(
        "SELECT * FROM board_categories WHERE parent_category_id = ? ORDER BY name",
        (parent_category_id,),
    ).fetchall()
    return [_row_to_category(row) for row in rows]


def delete_category(db: Database, category: Category, *, deleted_by: User) -> None:
    """
    Permanently remove `category` (design doc -- board/area management
    round). Any board currently assigned to it, and any sub-category
    whose parent it is, falls back to "uncategorized"/top-level
    (`category_id`/`parent_category_id` set to `NULL`) rather than being
    deleted or blocking this — handled here at the application level,
    the same reasoning as `netbbs.boards.boards.delete_board`'s own
    docstring on why a schema `ON DELETE` clause isn't used for this
    (verified unsafe by direct testing, not by inspection, when the
    referencing and referenced tables are rebuilt in the same
    migration).
    """
    record_action(
        db, actor=deleted_by, action="delete_board_category", object_type="board_category",
        object_id=category.id, detail=f"deleted category {category.name!r} (id {category.id})",
    )
    db.connection.execute("UPDATE boards SET category_id = NULL WHERE category_id = ?", (category.id,))
    db.connection.execute(
        "UPDATE board_categories SET parent_category_id = NULL WHERE parent_category_id = ?",
        (category.id,),
    )
    db.connection.execute("DELETE FROM board_categories WHERE id = ?", (category.id,))
    db.connection.commit()


def _row_to_category(row: sqlite3.Row) -> Category:
    return Category(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        parent_category_id=row["parent_category_id"],
        created_at=row["created_at"],
    )
