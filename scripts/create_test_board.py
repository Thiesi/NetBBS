#!/usr/bin/env python3
"""
Dev utility: create a local test board (and optionally seed it with one
post) to manually exercise board browsing over Telnet.

No board-creation UI exists yet (that's admin/SysOp tooling, not built
in Phase 1) — this exists purely to unblock manual testing.

Usage:
    python scripts/create_test_board.py <db_path> <board_name> [description]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from netbbs.auth.users import get_user_by_username  # noqa: E402
from netbbs.boards import create_board, create_post  # noqa: E402
from netbbs.storage.database import Database  # noqa: E402


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    db_path = Path(sys.argv[1])
    board_name = sys.argv[2]
    description = sys.argv[3] if len(sys.argv) > 3 else None

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

    board = create_board(db, board_name, description=description, creator=creator)
    print(f"Created board {board.name!r} (board_id {board.board_id[:16]}...) in {db_path}")

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
