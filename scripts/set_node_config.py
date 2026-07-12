#!/usr/bin/env python3
"""
Dev/admin utility: set a node-wide config value.

No SysOp admin UI exists yet (that's later phase work) — this exists to
unblock manually testing/using node config, starting with the display
timestamp format.

Usage:
    python scripts/set_node_config.py <db_path> <key> <value>

Example (switch to US-style month/day, 12-hour clock):
    python scripts/set_node_config.py netbbs.db display_timestamp_format "%m/%d/%Y %I:%M %p"
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from netbbs.config import set_config  # noqa: E402
from netbbs.storage.database import Database  # noqa: E402
from netbbs.timeutil import DISPLAY_FORMAT_CONFIG_KEY, set_display_format  # noqa: E402


def main() -> None:
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)

    db_path = Path(sys.argv[1])
    key = sys.argv[2]
    value = sys.argv[3]

    db = Database(db_path)

    if key == DISPLAY_FORMAT_CONFIG_KEY:
        # Validated: strftime's handling of an invalid directive is
        # platform-dependent and often doesn't raise (see timeutil.py),
        # so catching a bad format here at set-time — with an immediate,
        # actionable error — matters more than it would for a typical
        # string setting.
        try:
            set_display_format(db, value)
        except ValueError as exc:
            print(f"Error: {exc}")
            sys.exit(1)
    else:
        set_config(db, key, value)

    print(f"Set {key!r} = {value!r} in {db_path}")


if __name__ == "__main__":
    main()
