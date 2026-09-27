#!/usr/bin/env python3
"""
Dev utility: write a raw node-wide config value.

Every setting a SysOp is meant to change has a screen in the SysOp console
(Settings), which validates what it writes: timestamps under Timestamp
format, the upload cap, expiry grace, invitation expiry and chat scrollback
under Limits & retention (issue #725). Use those. This script writes any
`node_config` key unchecked apart from the two timestamp keys, so it can
also write internal keys the node manages itself; it exists for
development and testing only.

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
from netbbs.rendering.reflow import print_wrapped  # noqa: E402
from netbbs.storage.database import Database  # noqa: E402
from netbbs.timeutil import (  # noqa: E402
    DISPLAY_FORMAT_CONFIG_KEY,
    DISPLAY_TIMEZONE_CONFIG_KEY,
    set_display_format,
    set_display_timezone,
)


def main() -> None:
    if len(sys.argv) != 4:
        print_wrapped(__doc__ or "")
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
        print_wrapped(f"Error: {exc}")
        sys.exit(1)

    print_wrapped(f"Set {key!r} = {value!r} in {db_path}")


if __name__ == "__main__":
    main()
