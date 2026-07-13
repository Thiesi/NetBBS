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
    Migration(
        description="Local blocklist — moderation stub, pre-dates the full "
        "reputation system (design doc §6/§13/§15).",
        sql="""
        CREATE TABLE blocklist (
            id                  INTEGER PRIMARY KEY,
            -- Exactly one of these two is set. Fingerprint-based entries
            -- are the form design doc §15's Phase 3 extends to remote
            -- nodes/users once the Link exists ("the local blocklist
            -- mechanism from Phase 1, extended to remote nodes/
            -- traffic"). local_user_id exists specifically for
            -- password-only local accounts, which have no fingerprint to
            -- block by.
            fingerprint         TEXT,
            local_user_id       INTEGER REFERENCES users(id),
            reason              TEXT,
            blocked_by_user_id  INTEGER NOT NULL REFERENCES users(id),
            created_at          TEXT NOT NULL,

            CHECK ((fingerprint IS NOT NULL) != (local_user_id IS NOT NULL))
        );

        CREATE UNIQUE INDEX idx_blocklist_fingerprint
            ON blocklist(fingerprint) WHERE fingerprint IS NOT NULL;
        CREATE UNIQUE INDEX idx_blocklist_local_user_id
            ON blocklist(local_user_id) WHERE local_user_id IS NOT NULL;
        """,
    ),
    Migration(
        description="Board categories — two levels max (a category with a "
        "parent cannot itself have children), enforced in application "
        "code in netbbs.boards.categories rather than here, since SQLite "
        "can't express a self-join depth constraint as a CHECK.",
        sql="""
        CREATE TABLE board_categories (
            id                  INTEGER PRIMARY KEY,
            name                TEXT NOT NULL UNIQUE,
            description         TEXT,
            parent_category_id  INTEGER REFERENCES board_categories(id),
            created_at          TEXT NOT NULL
        );
        """,
    ),
    Migration(
        description="Board pinning and categorization — a board can "
        "optionally belong to a category, and be pinned to always sort "
        "first regardless of chosen sort order.",
        sql="""
        ALTER TABLE boards ADD COLUMN category_id INTEGER REFERENCES board_categories(id);
        ALTER TABLE boards ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
        """,
    ),
    Migration(
        description="Channel categories — same two-level design as board "
        "categories, kept as a separate table rather than one shared "
        "polymorphic table, consistent with boards and channels already "
        "being fully independent subsystems elsewhere in the schema.",
        sql="""
        CREATE TABLE channel_categories (
            id                  INTEGER PRIMARY KEY,
            name                TEXT NOT NULL UNIQUE,
            description         TEXT,
            parent_category_id  INTEGER REFERENCES channel_categories(id),
            created_at          TEXT NOT NULL
        );
        """,
    ),
    Migration(
        description="Channel pinning and categorization, mirroring boards.",
        sql="""
        ALTER TABLE channels ADD COLUMN category_id INTEGER REFERENCES channel_categories(id);
        ALTER TABLE channels ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
        """,
    ),
    Migration(
        description="Bounded, disk-backed chat scrollback (design doc round "
        "19/20) — solves a channel looking empty after a local node "
        "restart, not the separate/harder Link catch-up question, which "
        "stays explicitly deferred. Join/leave events are stored "
        "alongside messages (kind discriminator) so a replayed scrollback "
        "reads coherently instead of showing messages from participants "
        "with no record of them ever being present.",
        sql="""
        CREATE TABLE channel_messages (
            id                  INTEGER PRIMARY KEY,
            channel_id          INTEGER NOT NULL REFERENCES channels(id),
            kind                TEXT NOT NULL CHECK (kind IN ('message', 'join', 'leave')),
            author_label        TEXT NOT NULL,
            -- Nullable: same reasoning as posts.author_fingerprint — only
            -- present if the author has a keypair.
            author_fingerprint  TEXT,
            -- Required for kind='message', NULL for 'join'/'leave' — the
            -- kind itself carries the whole meaning of a presence event
            -- (see netbbs.chat.scrollback), enforced in application code
            -- rather than a CHECK, since SQLite can't easily express
            -- "NULL iff kind != 'message'" alongside the kind allowlist
            -- above without a much messier combined CHECK.
            body                TEXT,
            created_at          TEXT NOT NULL
        );

        CREATE INDEX idx_channel_messages_channel_id ON channel_messages(channel_id, id);
        """,
    ),
    Migration(
        description="File areas (design doc §1 terminology: 'area' always "
        "means file area, never 'board'; §9). Categories/pinning/sort "
        "order are included from the start, unlike boards/channels (which "
        "got them retrofitted in round 18) — same anti-retrofit reasoning "
        "as building level-gating plumbing early (§13).",
        sql="""
        CREATE TABLE file_area_categories (
            id                  INTEGER PRIMARY KEY,
            name                TEXT NOT NULL UNIQUE,
            description         TEXT,
            parent_category_id  INTEGER REFERENCES file_area_categories(id),
            created_at          TEXT NOT NULL
        );

        CREATE TABLE file_areas (
            id                       INTEGER PRIMARY KEY,
            -- Content-addressed (design doc §7), same reasoning as
            -- boards.board_id/channels.channel_id.
            area_id                  TEXT NOT NULL UNIQUE,
            name                     TEXT NOT NULL UNIQUE,
            description              TEXT,
            -- Nullable, populated once node identity exists at runtime
            -- (Phase 3) — same as boards.origin_node_fingerprint.
            origin_node_fingerprint  TEXT,
            min_read_level           INTEGER NOT NULL DEFAULT 0,
            min_write_level          INTEGER NOT NULL DEFAULT 0,
            category_id              INTEGER REFERENCES file_area_categories(id),
            pinned                   INTEGER NOT NULL DEFAULT 0,
            created_at               TEXT NOT NULL
        );

        CREATE INDEX idx_file_areas_area_id ON file_areas(area_id);

        CREATE TABLE files (
            id                    INTEGER PRIMARY KEY,
            -- Content-addressed (design doc §7) from metadata *and* the
            -- uploaded bytes' sha256 -- see netbbs.files.entries.
            file_id               TEXT NOT NULL UNIQUE,
            area_id               INTEGER NOT NULL REFERENCES file_areas(id),
            filename              TEXT NOT NULL,
            description           TEXT,
            size_bytes            INTEGER NOT NULL,
            sha256                TEXT NOT NULL,
            -- Filesystem path bytes are actually stored at (see
            -- netbbs.files.storage) -- not a DB blob, keeping the
            -- database itself small and letting the filesystem handle
            -- what it already does well.
            storage_path          TEXT NOT NULL,
            uploader_user_id      INTEGER NOT NULL REFERENCES users(id),
            -- Denormalized, same reasoning as posts.author_label: history
            -- still reads correctly even if the account is later renamed
            -- or removed.
            uploader_label        TEXT NOT NULL,
            uploader_fingerprint  TEXT,
            created_at            TEXT NOT NULL
        );

        CREATE INDEX idx_files_area_id ON files(area_id);
        """,
    ),
    Migration(
        description=(
            "Composite index matching the paginated post-listing query pattern "
            "(design doc round 30, issue #10)."
        ),
        sql="""
        -- netbbs.boards.posts.list_posts_page orders/filters by exactly
        -- (board_id, created_at, post_id) -- this index lets SQLite
        -- satisfy that with an index range scan instead of a full table
        -- scan + sort. Deliberately left idx_posts_board_id (round 2's
        -- migration) in place rather than dropping it in this same
        -- migration: this composite index's leading column already
        -- makes the single-column one redundant for query planning, but
        -- dropping a shipped index is its own separate, non-urgent
        -- cleanup with no user-visible benefit at this project's
        -- declared scale (design doc §14) -- not worth bundling into a
        -- migration whose actual point is adding the new index.
        CREATE INDEX idx_posts_board_id_created_at_post_id
            ON posts(board_id, created_at, post_id);
        """,
    ),
    Migration(
        description=(
            "Composite index matching the paginated file-listing query pattern "
            "(design doc round 31, issue #10's file-area follow-up)."
        ),
        sql="""
        -- Same reasoning as the posts composite index directly above:
        -- netbbs.files.entries.list_files_page orders/filters by
        -- exactly (area_id, created_at, file_id). idx_files_area_id
        -- (round 2's migration) deliberately left in place for the same
        -- reason idx_posts_board_id was -- redundant for query planning
        -- once this composite index exists, but dropping a shipped
        -- index is its own separate, non-urgent cleanup.
        CREATE INDEX idx_files_area_id_created_at_file_id
            ON files(area_id, created_at, file_id);
        """,
    ),
]
