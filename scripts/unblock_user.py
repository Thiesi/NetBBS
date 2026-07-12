#!/usr/bin/env python3
"""
Dev/admin utility: remove a local user from the blocklist.

Usage:
    python scripts/unblock_user.py <db_path> <username>
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from netbbs.auth.users import get_user_by_username  # noqa: E402
from netbbs.moderation import unblock_user  # noqa: E402
from netbbs.storage.database import Database  # noqa: E402


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)

    db_path = Path(sys.argv[1])
    username = sys.argv[2]

    db = Database(db_path)
    target = get_user_by_username(db, username)
    unblock_user(db, target)
    print(f"Unblocked {username!r} in {db_path}")


if __name__ == "__main__":
    main()
