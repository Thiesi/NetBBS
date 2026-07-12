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
