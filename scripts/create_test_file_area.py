#!/usr/bin/env python3
"""
Dev utility: create a local test file area to manually exercise file area
browsing over Telnet.

No area-creation UI exists yet (that's admin/SysOp tooling, not built in
Phase 1) — this exists purely to unblock manual testing. Run
scripts/create_test_file.py afterward to seed it with an actual file.

Usage:
    python scripts/create_test_file_area.py <db_path> <area_name> [description] [category_name] [pinned:yes/no]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from netbbs.auth.users import get_user_by_username  # noqa: E402
from netbbs.files import create_file_area  # noqa: E402
from netbbs.files.categories import FileAreaCategoryError, get_category_by_name  # noqa: E402
from netbbs.storage.database import Database  # noqa: E402


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    db_path = Path(sys.argv[1])
    area_name = sys.argv[2]
    description = sys.argv[3] if len(sys.argv) > 3 else None
    category_name = sys.argv[4] if len(sys.argv) > 4 else None
    pinned = len(sys.argv) > 5 and sys.argv[5].lower() in ("yes", "true", "1")

    db = Database(db_path)

    row = db.connection.execute("SELECT username FROM users LIMIT 1").fetchone()
    if row is None:
        print("No users exist yet — run scripts/create_test_user.py first.")
        sys.exit(1)
    creator = get_user_by_username(db, row["username"])

    category_id = None
    if category_name is not None:
        try:
            category_id = get_category_by_name(db, category_name).id
        except FileAreaCategoryError as exc:
            print(f"Error: {exc}")
            sys.exit(1)

    area = create_file_area(
        db,
        area_name,
        description=description,
        category_id=category_id,
        pinned=pinned,
        creator=creator,
    )
    pinned_note = " (pinned)" if area.pinned else ""
    print(f"Created file area {area.name!r} (area_id {area.area_id[:16]}...){pinned_note} in {db_path}")


if __name__ == "__main__":
    main()
