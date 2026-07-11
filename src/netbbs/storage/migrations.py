"""
Schema migrations, applied in order and tracked via `PRAGMA user_version`.

This follows the first attempt's actual approach (design doc round 2
sign-off notes) rather than a separate migrations-tracking table — one
integer pragma is enough to know how far a given database has been
migrated, and SQLite persists it for free.

Rule: never edit an already-shipped migration's SQL after it has been
released to any deployed node. Add a new migration instead — the same
discipline as any other schema-versioned system (Rails, Django, etc.).
Editing a shipped migration in place means nodes that already ran it and
nodes that haven't yet disagree about what version N actually means.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    description: str
    sql: str


MIGRATIONS = [
    Migration(
        description="Initial schema: users table for local BBS accounts.",
        sql="""
        CREATE TABLE users (
            id              INTEGER PRIMARY KEY,
            username        TEXT NOT NULL UNIQUE,

            -- Design doc §5: both password and keypair login are
            -- supported, and either may be used alone or together.
            password_hash   TEXT,
            public_key      TEXT,   -- base64-encoded raw Ed25519 public key
            fingerprint     TEXT UNIQUE,  -- derived from public_key; see
                                           -- netbbs.identity.fingerprint_from_verify_key

            user_level      INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL,
            last_login_at   TEXT,

            CHECK (password_hash IS NOT NULL OR public_key IS NOT NULL),
            -- fingerprint must be present exactly when public_key is —
            -- it's a derived value, not independently settable.
            CHECK ((public_key IS NULL) = (fingerprint IS NULL))
        );

        CREATE INDEX idx_users_fingerprint ON users(fingerprint);
        """,
    ),
]
