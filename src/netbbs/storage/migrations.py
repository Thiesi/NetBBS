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
    Migration(
        description="Message boards and posts, with content-addressed IDs.",
        sql="""
        CREATE TABLE boards (
            id                       INTEGER PRIMARY KEY,
            -- Content-addressed (design doc §7) — see
            -- netbbs.boards.content_id. Computed now, ahead of any
            -- actual Link networking, specifically so a board doesn't
            -- need its ID scheme migrated when it later becomes Linked.
            board_id                 TEXT NOT NULL UNIQUE,
            name                     TEXT NOT NULL UNIQUE,
            description              TEXT,
            -- Nullable: no node identity is loaded/available at runtime
            -- yet (that's Phase 3 work). Populated once that exists.
            origin_node_fingerprint  TEXT,
            -- Simple coarse level-gate for Phase 1. The richer §13
            -- moderator/permission model (named read/write/edit/delete/
            -- approve grants) is Phase 2 scope and layers on top of
            -- this, rather than replacing it.
            min_read_level           INTEGER NOT NULL DEFAULT 0,
            min_write_level          INTEGER NOT NULL DEFAULT 0,
            created_at               TEXT NOT NULL
        );

        CREATE INDEX idx_boards_board_id ON boards(board_id);

        CREATE TABLE posts (
            id                  INTEGER PRIMARY KEY,
            post_id             TEXT NOT NULL UNIQUE,
            board_id            INTEGER NOT NULL REFERENCES boards(id),
            -- Content-addressed reference to the post being replied to,
            -- forming the DAG structure design doc §7 describes — even
            -- though there's no actual DAG sync yet, the parent-pointer
            -- shape is already correct.
            parent_post_id      TEXT REFERENCES posts(post_id),
            author_user_id      INTEGER NOT NULL REFERENCES users(id),
            -- Denormalized username at post time, so history still reads
            -- correctly even if the account is later renamed or removed.
            author_label        TEXT NOT NULL,
            -- Nullable: only present if the author has a keypair. A
            -- password-only user's posts are still fully valid — see
            -- the node-vouching decision in the design doc's phasing
            -- sign-off notes.
            author_fingerprint  TEXT,
            subject             TEXT NOT NULL,
            body                TEXT NOT NULL,
            created_at          TEXT NOT NULL
        );

        CREATE INDEX idx_posts_board_id ON posts(board_id);
        """,
    ),
    Migration(
        description="Node-wide configuration key-value store.",
        sql="""
        CREATE TABLE node_config (
            key    TEXT PRIMARY KEY,
            value  TEXT NOT NULL
        );
        """,
    ),
    Migration(
        description="Chat channels (real-time messages are not persisted — "
        "see netbbs.chat.hub).",
        sql="""
        CREATE TABLE channels (
            id          INTEGER PRIMARY KEY,
            -- Content-addressed (design doc §7), same reasoning as
            -- boards.board_id — ready for NetBBS Link Channels later
            -- without needing an ID-scheme migration.
            channel_id  TEXT NOT NULL UNIQUE,
            name        TEXT NOT NULL UNIQUE,
            description TEXT,
            -- Single threshold, not a read/write pair like boards —
            -- chat access has no meaningful read/write split.
            min_level   INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL
        );
        """,
    ),
]
