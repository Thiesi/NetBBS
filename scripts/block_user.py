#!/usr/bin/env python3
"""
Dev/admin utility: block a local user from logging in.

No SysOp admin UI exists yet (that's later phase work) — this exists to
unblock manually testing/using the blocklist.

Usage:
    python scripts/block_user.py <db_path> <username> [reason]

The account performing the block is whichever existing user has the
highest user_level — a stand-in for a real SysOp/admin concept, which
doesn't exist yet in Phase 1.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from netbbs.auth.users import get_user_by_username  # noqa: E402
from netbbs.moderation import BlocklistError, block_user  # noqa: E402
from netbbs.storage.database import Database  # noqa: E402


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    db_path = Path(sys.argv[1])
    username = sys.argv[2]
    reason = sys.argv[3] if len(sys.argv) > 3 else None

    db = Database(db_path)

    target = get_user_by_username(db, username)

    row = db.connection.execute(
        "SELECT username FROM users ORDER BY user_level DESC LIMIT 1"
    ).fetchone()
    blocker = get_user_by_username(db, row["username"])

    try:
        entry = block_user(db, target, blocked_by=blocker, reason=reason)
    except BlocklistError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    key = entry.fingerprint or f"local user id {entry.local_user_id}"
    print(f"Blocked {username!r} ({key}) in {db_path}")


if __name__ == "__main__":
    main()
