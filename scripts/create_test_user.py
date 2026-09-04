#!/usr/bin/env python3
"""
Dev utility: create a local test account to manually exercise the
Telnet login flow with.

Not part of the package proper — there's no self-registration flow yet
(that's a menu/UI feature, not built as part of bare connectivity), so
this exists purely to unblock manual testing of `python -m netbbs`.

Usage:
    python scripts/create_test_user.py <db_path> <username> <password> [user_level] [node_display_name]

`node_display_name` sets the node's own display name at the same time --
a node that enables NetBBS Link refuses to start under the shipped
placeholder "NetBBS" (design doc §16, issue #219), so the two-node
quickstart passes one for each node.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from netbbs.auth.users import SYSOP_LEVEL, create_user  # noqa: E402
from netbbs.config import is_node_display_name_placeholder, set_node_display_name  # noqa: E402
from netbbs.rendering.reflow import print_wrapped  # noqa: E402
from netbbs.storage.database import Database  # noqa: E402


def main() -> None:
    if len(sys.argv) < 4:
        print_wrapped(__doc__ or "")
        sys.exit(1)

    db_path = Path(sys.argv[1])
    username = sys.argv[2]
    password = sys.argv[3]
    user_level = int(sys.argv[4]) if len(sys.argv) > 4 else 0

    node_display_name = sys.argv[5] if len(sys.argv) > 5 else None
    db = Database(db_path)
    user = create_user(db, username, password=password, user_level=user_level)
    print_wrapped(f"Created user {user.username!r} (level {user.user_level}) in {db_path}")
    if node_display_name:
        set_node_display_name(db, node_display_name)
        print_wrapped(f"Set the node display name to {node_display_name!r}")
    elif user_level >= SYSOP_LEVEL and is_node_display_name_placeholder(db):
        print_wrapped(
            "Note: the node display name is still the placeholder 'NetBBS'. A node with "
            "NetBBS Link enabled won't start under it -- pass a name as the fifth argument, "
            "or set one later under Settings > Node name."
        )


if __name__ == "__main__":
    main()
