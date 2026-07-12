#!/usr/bin/env python3
"""
Dev utility: create a local test account to manually exercise the
Telnet login flow with.

Not part of the package proper — there's no self-registration flow yet
(that's a menu/UI feature, not built as part of bare connectivity), so
this exists purely to unblock manual testing of `python -m netbbs`.

Usage:
    python scripts/create_test_user.py <db_path> <username> <password> [user_level]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from netbbs.auth.users import create_user  # noqa: E402
from netbbs.storage.database import Database  # noqa: E402


def main() -> None:
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)

    db_path = Path(sys.argv[1])
    username = sys.argv[2]
    password = sys.argv[3]
    user_level = int(sys.argv[4]) if len(sys.argv) > 4 else 0

    db = Database(db_path)
    user = create_user(db, username, password=password, user_level=user_level)
    print(f"Created user {user.username!r} (level {user.user_level}) in {db_path}")


if __name__ == "__main__":
    main()
