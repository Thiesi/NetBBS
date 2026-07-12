#!/usr/bin/env python3
"""
Dev utility: create a local chat channel for manual testing.

No channel-creation UI exists yet (that's admin/SysOp tooling, not built
in Phase 1) — this exists purely to unblock manual testing. Open two
terminals with `telnet localhost 2323` and join the same channel from
both to see real-time chat working.

Usage:
    python scripts/create_test_channel.py <db_path> <channel_name> [description]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from netbbs.auth.users import get_user_by_username  # noqa: E402
from netbbs.chat import create_channel  # noqa: E402
from netbbs.storage.database import Database  # noqa: E402


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    db_path = Path(sys.argv[1])
    channel_name = sys.argv[2]
    description = sys.argv[3] if len(sys.argv) > 3 else None

    db = Database(db_path)

    row = db.connection.execute("SELECT username FROM users LIMIT 1").fetchone()
    if row is None:
        print("No users exist yet — run scripts/create_test_user.py first.")
        sys.exit(1)
    creator = get_user_by_username(db, row["username"])

    channel = create_channel(db, channel_name, description=description, creator=creator)
    print(f"Created channel #{channel.name} (channel_id {channel.channel_id[:16]}...) in {db_path}")


if __name__ == "__main__":
    main()
