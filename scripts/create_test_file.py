#!/usr/bin/env python3
"""
Dev utility: upload a local file into a file area, to manually exercise
file area browsing over Telnet.

There's no in-session upload yet — that needs real Zmodem support
(deliberately scoped as separate, future work; see design doc). This is
the same bootstrap-only path boards/channels used before any browsing UI
existed for them.

Usage:
    python scripts/create_test_file.py <db_path> <area_name> <local_file_path> [description]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from netbbs.auth.users import get_user_by_username  # noqa: E402
from netbbs.files import upload_file  # noqa: E402
from netbbs.files.areas import FileAreaError, get_file_area_by_name  # noqa: E402
from netbbs.storage.database import Database  # noqa: E402


def main() -> None:
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)

    db_path = Path(sys.argv[1])
    area_name = sys.argv[2]
    local_path = Path(sys.argv[3])
    description = sys.argv[4] if len(sys.argv) > 4 else None

    if not local_path.is_file():
        print(f"No such file: {local_path}")
        sys.exit(1)

    db = Database(db_path)

    row = db.connection.execute("SELECT username FROM users LIMIT 1").fetchone()
    if row is None:
        print("No users exist yet — run scripts/create_test_user.py first.")
        sys.exit(1)
    uploader = get_user_by_username(db, row["username"])

    try:
        area = get_file_area_by_name(db, area_name)
    except FileAreaError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    entry = upload_file(
        db, area, uploader, local_path.name, local_path.read_bytes(), description=description
    )
    print(f"Uploaded {entry.filename!r} ({entry.size_bytes} bytes, file_id {entry.file_id[:16]}...) to {area.name!r}")


if __name__ == "__main__":
    main()
