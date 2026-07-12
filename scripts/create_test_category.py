#!/usr/bin/env python3
"""
Dev utility: create a board or channel category (optionally as a
sub-category of an existing top-level category — at most two levels are
ever allowed, see design doc round 17 sign-off notes).

Usage:
    python scripts/create_test_category.py <db_path> board <name> [parent_name] [description]
    python scripts/create_test_category.py <db_path> channel <name> [parent_name] [description]

Examples:
    python scripts/create_test_category.py netbbs.db board "Vintage Computing"
    python scripts/create_test_category.py netbbs.db board "Commodore" "Vintage Computing"
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from netbbs.storage.database import Database  # noqa: E402


def main() -> None:
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)

    db_path = Path(sys.argv[1])
    kind = sys.argv[2]
    name = sys.argv[3]
    parent_name = sys.argv[4] if len(sys.argv) > 4 else None
    description = sys.argv[5] if len(sys.argv) > 5 else None

    if kind not in ("board", "channel"):
        print(f"kind must be 'board' or 'channel', got {kind!r}")
        sys.exit(1)

    if kind == "board":
        from netbbs.boards.categories import CategoryError, create_category, get_category_by_name
    else:
        from netbbs.chat.categories import CategoryError, create_category, get_category_by_name

    db = Database(db_path)

    parent_id = None
    if parent_name is not None:
        try:
            parent = get_category_by_name(db, parent_name)
        except CategoryError as exc:
            print(f"Error: {exc}")
            sys.exit(1)
        parent_id = parent.id

    try:
        category = create_category(db, name, description=description, parent_category_id=parent_id)
    except CategoryError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    level = "top-level" if category.is_top_level else f"sub-category of {parent_name!r}"
    print(f"Created {kind} category {category.name!r} ({level}) in {db_path}")


if __name__ == "__main__":
    main()
