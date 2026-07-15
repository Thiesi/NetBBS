"""
Node-wide configuration: a simple key-value store backed by the database.

Currently just the display timestamp format (see `netbbs.timeutil`), but
deliberately generic — a `node_config` table rather than a single
hardcoded setting — since more node-wide settings (a node's display name,
welcome banner text, etc.) are inevitable as the project grows, and
there's no reason to invent a new one-off mechanism each time one shows
up.

Per-user overrides (once a user preferences system exists — not yet, see
design doc §13/§15 phasing) are a separate, later layer that sits on top
of this, not a replacement for it: a per-user format preference should
win over the node default when present, and this module doesn't need to
change at all for that to work — see `netbbs.timeutil.format_for_display`
for where that resolution order already lives.
"""

from __future__ import annotations

from netbbs.storage.database import Database


def get_config(db: Database, key: str, default: str | None = None) -> str | None:
    row = db.connection.execute("SELECT value FROM node_config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else default


def set_config(db: Database, key: str, value: str) -> None:
    db.connection.execute(
        """
        INSERT INTO node_config (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    db.connection.commit()


# Config key for the node-wide grace period between a post/file
# expiring and actually being deleted (design doc §13/§15, sign-off
# round 35). Deliberately a single node-wide default rather than a
# per-board/per-area column — nothing in the design doc asks for
# per-object control over it, unlike max post age, which genuinely is
# per-board (see netbbs.boards.boards.Board.max_post_age_days).
EXPIRY_GRACE_PERIOD_CONFIG_KEY = "post_expiry_grace_period_days"

_DEFAULT_EXPIRY_GRACE_PERIOD_DAYS = 7


def get_expiry_grace_period_days(db: Database) -> int:
    value = get_config(db, EXPIRY_GRACE_PERIOD_CONFIG_KEY)
    return int(value) if value is not None else _DEFAULT_EXPIRY_GRACE_PERIOD_DAYS


def set_expiry_grace_period_days(db: Database, days: int) -> None:
    if days < 0:
        raise ValueError(f"grace period must be non-negative, got {days!r}")
    set_config(db, EXPIRY_GRACE_PERIOD_CONFIG_KEY, str(days))


# Config key for the node-wide maximum Zmodem upload size (GitHub issue
# #34) -- a single node-wide default, same shape as the expiry grace
# period above, rather than a per-area column: nothing currently asks
# for per-area control over it, and a single node-wide ceiling is
# already enough to close the actual security gap (unbounded upload
# size/duration), unlike max post age, which genuinely needs to vary
# per board.
MAX_UPLOAD_BYTES_CONFIG_KEY = "max_upload_bytes"

_DEFAULT_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MiB


def get_max_upload_bytes(db: Database) -> int:
    value = get_config(db, MAX_UPLOAD_BYTES_CONFIG_KEY)
    return int(value) if value is not None else _DEFAULT_MAX_UPLOAD_BYTES


def set_max_upload_bytes(db: Database, max_bytes: int) -> None:
    if max_bytes <= 0:
        raise ValueError(f"max upload size must be positive, got {max_bytes!r}")
    set_config(db, MAX_UPLOAD_BYTES_CONFIG_KEY, str(max_bytes))
