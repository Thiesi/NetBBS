"""Helpers for tests that open a database on a deliberately old schema.

A migration test truncates `MIGRATIONS`, opens a database, fills it the way a
real node of that era would have, and then reopens it to watch the migration
run. The filling step cannot always use today's domain functions, because
today's code may touch a table the old schema does not have yet.
"""

from __future__ import annotations

from netbbs.auth.users import User, get_user_by_username
from netbbs.storage.database import Database


def insert_user_on_old_schema(db: Database, username: str, *, user_level: int = 0) -> User:
    """A password-only account, written the way every schema since the first has stored one.

    `create_user` consults `retired_usernames` (issue #594), which a schema
    truncated before that migration does not have. The placeholder hash is not
    a valid Argon2 string, so the account cannot log in, which no migration
    test needs it to.
    """
    db.connection.execute(
        "INSERT INTO users (username, password_hash, user_level, created_at) "
        "VALUES (?, 'not-a-real-hash', ?, '2026-01-01T00:00:00.000000Z')",
        (username, user_level),
    )
    db.connection.commit()
    return get_user_by_username(db, username)
