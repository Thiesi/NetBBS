"""
Per-user preference storage: a simple key-value store backed by the
database, mirroring `netbbs.config`'s node-wide store exactly (design
doc §13) — that module's own docstring already
anticipated this as "a separate, later layer that sits on top of"
node-wide config, not a replacement for it.

Deliberately generic rather than scoped to any one feature: the
directory/vCard system (`netbbs.directory`) is the first consumer, but
any future per-user setting (e.g. a per-user chat timestamp
preference, design doc) can reuse this same store via its own
typed wrapper functions, the same way `netbbs.timeutil` wraps
`netbbs.config`'s generic store for the node-wide display format/
timezone settings.
"""

from __future__ import annotations

from netbbs.auth.users import User
from netbbs.storage.database import Database


def get_user_preference(db: Database, user: User, key: str, default: str | None = None) -> str | None:
    row = db.connection.execute(
        "SELECT value FROM user_preferences WHERE user_id = ? AND key = ?", (user.id, key)
    ).fetchone()
    return row["value"] if row is not None else default


def set_user_preference(db: Database, user: User, key: str, value: str) -> None:
    db.connection.execute(
        """
        INSERT INTO user_preferences (user_id, key, value) VALUES (?, ?, ?)
        ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value
        """,
        (user.id, key, value),
    )
    db.connection.commit()
