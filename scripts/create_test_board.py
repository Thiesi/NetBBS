#!/usr/bin/env python3
"""
Dev utility: create a local test board (and optionally seed it with one
post) to manually exercise board browsing over Telnet.

No board-creation UI exists yet (that's admin/SysOp tooling, not built
in Phase 1) — this exists purely to unblock manual testing.

Usage:
    python scripts/create_test_board.py <db_path> <board_name> [description] [category_name] [pinned:yes/no]

Run scripts/create_test_category.py first if you want to assign a
category — this script looks it up by name, it doesn't create one.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from netbbs.auth.users import get_user_by_username  # noqa: E402
from netbbs.boards import create_board, create_post  # noqa: E402
from netbbs.boards.categories import CategoryError, get_category_by_name  # noqa: E402
from netbbs.storage.database import Database  # noqa: E402


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    db_path = Path(sys.argv[1])
    board_name = sys.argv[2]
    description = sys.argv[3] if len(sys.argv) > 3 else None
    category_name = sys.argv[4] if len(sys.argv) > 4 else None
    pinned = len(sys.argv) > 5 and sys.argv[5].lower() in ("yes", "true", "1")

    db = Database(db_path)

    # Boards need a creator identity — reuse whatever the first existing
    # user account is, since there's no SysOp/admin concept yet to
    # attribute board creation to. Good enough for manual dev testing;
    # not how real board creation should work once admin tooling exists.
    row = db.connection.execute("SELECT username FROM users LIMIT 1").fetchone()
    if row is None:
        print("No users exist yet — run scripts/create_test_user.py first.")
        sys.exit(1)
    creator = get_user_by_username(db, row["username"])

    category_id = None
    if category_name is not None:
        try:
            category_id = get_category_by_name(db, category_name).id
        except CategoryError as exc:
            print(f"Error: {exc}")
            sys.exit(1)

    board = create_board(
        db,
        board_name,
        description=description,
        category_id=category_id,
        pinned=pinned,
        creator=creator,
    )
    pinned_note = " (pinned)" if board.pinned else ""
    print(f"Created board {board.name!r} (board_id {board.board_id[:16]}...){pinned_note} in {db_path}")

    post = create_post(
        db,
        board,
        creator,
        subject="Welcome",
        body="This is the first post on this board.",
    )
    print(f"Seeded with post {post.post_id[:16]}... by {post.author_label}")


if __name__ == "__main__":
    main()
