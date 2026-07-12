"""
Channel categories: mirrors `netbbs.boards.categories` exactly — same
two-level design, same depth-cap enforcement reasoning. Kept as a
separate module/table rather than sharing one with boards, consistent
with boards and channels already being fully independent subsystems
everywhere else in this codebase.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

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
) -> Category:
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
            INSERT INTO channel_categories (name, description, parent_category_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (name, description, parent_category_id, created_at),
        )
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        raise CategoryError(f"could not create category {name!r} — name already in use?") from exc

    return get_category_by_name(db, name)


def get_category_by_id(db: Database, category_id: int) -> Category:
    row = db.connection.execute(
        "SELECT * FROM channel_categories WHERE id = ?", (category_id,)
    ).fetchone()
    if row is None:
        raise CategoryError(f"no such category id: {category_id!r}")
    return _row_to_category(row)


def get_category_by_name(db: Database, name: str) -> Category:
    row = db.connection.execute(
        "SELECT * FROM channel_categories WHERE name = ?", (name,)
    ).fetchone()
    if row is None:
        raise CategoryError(f"no such category: {name!r}")
    return _row_to_category(row)


def list_top_level_categories(db: Database) -> list[Category]:
    rows = db.connection.execute(
        "SELECT * FROM channel_categories WHERE parent_category_id IS NULL ORDER BY name"
    ).fetchall()
    return [_row_to_category(row) for row in rows]


def list_subcategories(db: Database, parent_category_id: int) -> list[Category]:
    rows = db.connection.execute(
        "SELECT * FROM channel_categories WHERE parent_category_id = ? ORDER BY name",
        (parent_category_id,),
    ).fetchall()
    return [_row_to_category(row) for row in rows]


def _row_to_category(row: sqlite3.Row) -> Category:
    return Category(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        parent_category_id=row["parent_category_id"],
        created_at=row["created_at"],
    )
