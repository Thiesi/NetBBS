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

Example (set node-wide display timezone):
    python scripts/set_node_config.py netbbs.db display_timezone Europe/Berlin
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from netbbs.config import set_config  # noqa: E402
from netbbs.storage.database import Database  # noqa: E402
from netbbs.timeutil import (  # noqa: E402
    DISPLAY_FORMAT_CONFIG_KEY,
    DISPLAY_TIMEZONE_CONFIG_KEY,
    set_display_format,
    set_display_timezone,
)


def main() -> None:
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)

    db_path = Path(sys.argv[1])
    key = sys.argv[2]
    value = sys.argv[3]

    db = Database(db_path)

    # Both display settings are validated at set-time rather than
    # silently discovered as broken later — see timeutil.py for why this
    # matters especially for the format string (strftime's handling of
    # bad input is platform-dependent and often doesn't raise at all).
    try:
        if key == DISPLAY_FORMAT_CONFIG_KEY:
            set_display_format(db, value)
        elif key == DISPLAY_TIMEZONE_CONFIG_KEY:
            set_display_timezone(db, value)
        else:
            set_config(db, key, value)
    except ValueError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    print(f"Set {key!r} = {value!r} in {db_path}")


if __name__ == "__main__":
    main()
