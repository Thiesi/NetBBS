"""
File area categories: at most two levels, identical shape and reasoning
to `netbbs.boards.categories` — see that module's docstring. Kept as a
separate table/module rather than a shared polymorphic implementation,
consistent with the design doc's explicit choice to keep board and
channel categories independent ("boards and channels already being fully
independent subsystems everywhere else in the schema") — file areas are
a third, equally independent subsystem.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from netbbs.auth.users import User
from netbbs.moderation.log import record_action
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


class FileAreaCategoryError(Exception):
    """Raised for category creation/lookup failures, including an
    attempted third level of nesting."""


@dataclass(frozen=True)
class FileAreaCategory:
    id: int
    name: str
    description: str | None
    parent_category_id: int | None
    created_at: str
    # Its place among its siblings, first at 0 (issue #681): the SysOp
    # orders categories, and every listing follows that order.
    position: int = 0

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
) -> FileAreaCategory:
    """Create a new file area category, optionally as a sub-category of
    an existing top-level category. No permission check here — same
    precedent as board/channel category creation. `created_by` is only
    for the audit-log entry."""
    if parent_category_id is not None:
        parent = get_category_by_id(db, parent_category_id)
        if not parent.is_top_level:
            raise FileAreaCategoryError(
                f"cannot create a sub-category under {parent.name!r} — "
                f"it is itself a sub-category; only two levels are allowed"
            )

    created_at = utc_now_iso()
    try:
        db.connection.execute(
            """
            INSERT INTO file_area_categories (name, description, parent_category_id, created_at, position)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, description, parent_category_id, created_at, _next_position(db, parent_category_id)),
        )
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        raise FileAreaCategoryError(
            f"could not create category {name!r} — name already in use?"
        ) from exc

    new_category = get_category_by_name(db, name)
    record_action(
        db, actor=created_by, action="create_file_area_category", object_type="file_area_category",
        object_id=new_category.id, detail=f"created category {name!r}",
    )
    return new_category


def get_category_by_id(db: Database, category_id: int) -> FileAreaCategory:
    row = db.connection.execute(
        "SELECT * FROM file_area_categories WHERE id = ?", (category_id,)
    ).fetchone()
    if row is None:
        raise FileAreaCategoryError(f"no such category id: {category_id!r}")
    return _row_to_category(row)


def get_category_by_name(db: Database, name: str) -> FileAreaCategory:
    row = db.connection.execute(
        "SELECT * FROM file_area_categories WHERE name = ?", (name,)
    ).fetchone()
    if row is None:
        raise FileAreaCategoryError(f"no such category: {name!r}")
    return _row_to_category(row)


def list_top_level_categories(db: Database) -> list[FileAreaCategory]:
    rows = db.connection.execute(
        "SELECT * FROM file_area_categories WHERE parent_category_id IS NULL ORDER BY position, name"
    ).fetchall()
    return [_row_to_category(row) for row in rows]


def list_subcategories(db: Database, parent_category_id: int) -> list[FileAreaCategory]:
    rows = db.connection.execute(
        "SELECT * FROM file_area_categories WHERE parent_category_id = ? ORDER BY position, name",
        (parent_category_id,),
    ).fetchall()
    return [_row_to_category(row) for row in rows]


def update_category(
    db: Database,
    category: FileAreaCategory,
    *,
    name: str,
    description: str | None,
    parent_category_id: int | None,
    changed_by: User,
) -> FileAreaCategory:
    """Rename `category`, change its description, or move it under
    another top-level category or to the top level (issue #681). The
    two-level rule holds as at creation: the new parent must be a
    top-level category, and a category with sub-categories of its own
    stays top-level. Moved to another parent, it goes last there."""
    name = name.strip()
    if not name:
        raise FileAreaCategoryError("name cannot be blank")
    if parent_category_id is not None:
        if parent_category_id == category.id:
            raise FileAreaCategoryError("a category cannot be its own parent")
        parent = get_category_by_id(db, parent_category_id)
        if not parent.is_top_level:
            raise FileAreaCategoryError(
                f"cannot move {category.name!r} under {parent.name!r} — "
                f"it is itself a sub-category; only two levels are allowed"
            )
        if list_subcategories(db, category.id):
            raise FileAreaCategoryError(
                f"{category.name!r} has sub-categories of its own; move or delete them first, "
                f"since only two levels are allowed"
            )
    position = (
        category.position if parent_category_id == category.parent_category_id
        else _next_position(db, parent_category_id)
    )
    try:
        db.connection.execute(
            "UPDATE file_area_categories SET name = ?, description = ?, parent_category_id = ?, position = ? WHERE id = ?",
            (name, description, parent_category_id, position, category.id),
        )
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        raise FileAreaCategoryError(f"could not rename to {name!r} — name already in use?") from exc
    record_action(
        db, actor=changed_by, action="update_file_area_category", object_type="file_area_category",
        object_id=category.id, detail=f"updated category {name!r}",
    )
    return get_category_by_id(db, category.id)


def move_category(db: Database, category: FileAreaCategory, offset: int, *, moved_by: User) -> bool:
    """Move `category` `offset` places among its siblings (-1 up, +1
    down), and renumber them all (issue #681). Returns whether it moved:
    the first cannot go up, nor the last down."""
    siblings = (
        list_top_level_categories(db) if category.parent_category_id is None
        else list_subcategories(db, category.parent_category_id)
    )
    ids = [sibling.id for sibling in siblings]
    if category.id not in ids:
        raise FileAreaCategoryError(f"no such category: {category.name!r}")
    index = ids.index(category.id)
    target = index + offset
    if not 0 <= target < len(ids):
        return False
    ids.insert(target, ids.pop(index))
    for place, category_id in enumerate(ids):
        db.connection.execute("UPDATE file_area_categories SET position = ? WHERE id = ?", (place, category_id))
    db.connection.commit()
    record_action(
        db, actor=moved_by, action="move_file_area_category", object_type="file_area_category",
        object_id=category.id, detail=f"moved category {category.name!r} to place {target + 1}",
    )
    return True


def _next_position(db: Database, parent_category_id: int | None) -> int:
    """The place after the last of these siblings."""
    row = db.connection.execute(
        "SELECT MAX(position) FROM file_area_categories WHERE parent_category_id IS ?", (parent_category_id,)
    ).fetchone()
    return 0 if row[0] is None else row[0] + 1


def delete_category(db: Database, category: FileAreaCategory, *, deleted_by: User) -> None:
    """Permanently remove `category` -- mirrors
    `netbbs.boards.categories.delete_category` exactly, see that
    function's docstring for the full reasoning."""
    record_action(
        db, actor=deleted_by, action="delete_file_area_category", object_type="file_area_category",
        object_id=category.id, detail=f"deleted category {category.name!r} (id {category.id})",
    )
    db.connection.execute("UPDATE file_areas SET category_id = NULL WHERE category_id = ?", (category.id,))
    db.connection.execute(
        "UPDATE file_area_categories SET parent_category_id = NULL WHERE parent_category_id = ?",
        (category.id,),
    )
    db.connection.execute(
        "DELETE FROM user_sort_preferences WHERE resource_kind = 'file_area' AND category_id = ?", (category.id,)
    )
    db.connection.execute("DELETE FROM file_area_categories WHERE id = ?", (category.id,))
    db.connection.commit()


def _row_to_category(row: sqlite3.Row) -> FileAreaCategory:
    return FileAreaCategory(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        parent_category_id=row["parent_category_id"],
        created_at=row["created_at"],
        position=row["position"],
    )
