"""
Schema migrations, applied in order and tracked via `PRAGMA user_version`.

This follows the first attempt's actual approach (design doc
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
        description="Bounded, disk-backed chat scrollback (design doc) "
        "— solves a channel looking empty after a local node "
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
        "got them retrofitted later) — same anti-retrofit reasoning "
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
            "(design doc, issue #10)."
        ),
        sql="""
        -- netbbs.boards.posts.list_posts_page orders/filters by exactly
        -- (board_id, created_at, post_id) -- this index lets SQLite
        -- satisfy that with an index range scan instead of a full table
        -- scan + sort. Deliberately left idx_posts_board_id (an earlier
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
            "(design doc, issue #10's file-area follow-up)."
        ),
        sql="""
        -- Same reasoning as the posts composite index directly above:
        -- netbbs.files.entries.list_files_page orders/filters by
        -- exactly (area_id, created_at, file_id). idx_files_area_id
        -- (an earlier migration) deliberately left in place for the same
        -- reason idx_posts_board_id was -- redundant for query planning
        -- once this composite index exists, but dropping a shipped
        -- index is its own separate, non-urgent cleanup.
        CREATE INDEX idx_files_area_id_created_at_file_id
            ON files(area_id, created_at, file_id);
        """,
    ),
    Migration(
        description=(
            "Moderator grants (design doc §13): the "
            "read/write/edit/delete/approve (boards/file areas) and "
            "edit/moderate/manage_members (channels) permission model, "
            "plus a generic moderation-action audit log shared by grants "
            "now and mute/ban/kick/approve once those exist."
        ),
        sql="""
        CREATE TABLE moderator_grants (
            id                  INTEGER PRIMARY KEY,
            user_id             INTEGER NOT NULL REFERENCES users(id),
            object_type         TEXT NOT NULL CHECK (object_type IN ('board', 'file_area', 'channel')),
            -- NULL = local-blanket grant over every local object of
            -- object_type (design doc §13's three moderator scope
            -- tiers). Link-blanket ("global") is Phase 6 scope and
            -- deliberately not modeled by a third sentinel here --
            -- see netbbs.moderation.roles' module docstring.
            object_id           INTEGER,
            -- Bitmask over netbbs.moderation.roles.BoardPermission or
            -- ChannelPermission, chosen by object_type. A single
            -- integer column rather than one row per permission, per
            -- §13's own "settable individually or combined" phrasing
            -- (design doc).
            permissions         INTEGER NOT NULL,
            granted_by_user_id  INTEGER NOT NULL REFERENCES users(id),
            created_at          TEXT NOT NULL
        );

        -- Two partial unique indexes rather than one compound UNIQUE,
        -- same reasoning as the blocklist table above: SQLite's
        -- UNIQUE treats every NULL as distinct, so a single
        -- UNIQUE(user_id, object_type, object_id) would not stop the
        -- same user from getting two separate local-blanket
        -- (object_id IS NULL) grant rows for the same object_type.
        CREATE UNIQUE INDEX idx_moderator_grants_per_object
            ON moderator_grants(user_id, object_type, object_id)
            WHERE object_id IS NOT NULL;
        CREATE UNIQUE INDEX idx_moderator_grants_blanket
            ON moderator_grants(user_id, object_type)
            WHERE object_id IS NULL;
        CREATE INDEX idx_moderator_grants_object
            ON moderator_grants(object_type, object_id);

        CREATE TABLE moderation_log (
            id                INTEGER PRIMARY KEY,
            actor_user_id     INTEGER NOT NULL REFERENCES users(id),
            -- Free-text action label ("grant", "revoke", and later
            -- "mute"/"ban"/"kick"/"approve"/etc.) rather than a
            -- CHECK-constrained allowlist -- this table is
            -- deliberately generic so future action types reuse it
            -- without a schema change, and a CHECK would need editing
            -- a shipped migration (disallowed per this file's own
            -- module docstring) every time a new action type is added.
            action            TEXT NOT NULL,
            object_type       TEXT,
            object_id         INTEGER,
            target_user_id    INTEGER REFERENCES users(id),
            detail            TEXT,
            created_at        TEXT NOT NULL
        );

        CREATE INDEX idx_moderation_log_object ON moderation_log(object_type, object_id, created_at);
        CREATE INDEX idx_moderation_log_target_user ON moderation_log(target_user_id, created_at);
        """,
    ),
    Migration(
        description=(
            "Moderated-board approval flow and the post maintenance/"
            "expiry state machine (design doc §13/§15). Boards "
            "gain a moderated flag and a per-board max post age; posts "
            "gain an approval/expiry status plus independent pin and "
            "exempt-from-expiry flags."
        ),
        sql="""
        -- Whether new posts on this board need moderator approval
        -- (netbbs.moderation.roles.BoardPermission.APPROVE) before
        -- other users can see them, and this board's own maximum post
        -- age (NULL = retain indefinitely, matching design doc §13's
        -- own "default: retain indefinitely" phrasing). The grace
        -- period between a post expiring and actually being deleted
        -- is deliberately a single node-wide default (see
        -- netbbs.config) rather than a per-board column -- nothing in
        -- the design doc asks for per-board control over it.
        ALTER TABLE boards ADD COLUMN moderated INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE boards ADD COLUMN max_post_age_days INTEGER;

        -- 'pending' posts are only visible to their author and to
        -- users holding APPROVE on this board (see
        -- netbbs.boards.posts.list_pending_posts); 'expired' posts are
        -- delisted from normal browsing (list_posts_page) but still
        -- individually reachable (reply-parent lookup, direct-by-ID)
        -- until the grace period elapses and the row is actually
        -- deleted -- there is no 'deleted' status value because that
        -- state is the row's absence, not a value it holds.
        --
        -- pinned here is a different concept from boards.pinned
        -- (which board sorts first in the board list) -- this is
        -- post-level, sorting a post first within its own board's
        -- listing. pinned and exempt_from_expiry are independent
        -- flags (design doc §13 lists "exempt... and pin..." as two
        -- separate moderator actions), both gated by the same `edit`
        -- permission bit per the existing pin/exempt sign-off note.
        ALTER TABLE posts ADD COLUMN status TEXT NOT NULL DEFAULT 'approved'
            CHECK (status IN ('pending', 'approved', 'expired'));
        ALTER TABLE posts ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE posts ADD COLUMN exempt_from_expiry INTEGER NOT NULL DEFAULT 0;

        -- Supports list_posts_page's new `status = 'approved'` filter
        -- (a plain equality, not an OR, so this stays a clean index
        -- range scan) as well as the expiry sweep's own board-scoped
        -- UPDATE/DELETE statements.
        CREATE INDEX idx_posts_board_id_status_created_at_post_id
            ON posts(board_id, status, created_at, post_id);
        """,
    ),
    Migration(
        description=(
            "File-area mirror of the board/post moderation "
            "lifecycle (design doc §13/§15): moderated-area "
            "approval flow and the file maintenance/expiry state "
            "machine, structurally identical to boards/posts."
        ),
        sql="""
        -- Mirrors boards.moderated/boards.max_post_age_days exactly --
        -- see that migration's comments for the full reasoning, not
        -- repeated here. Named max_file_age_days rather than
        -- max_post_age_days for this table -- "post" doesn't fit a
        -- file the way it fits a board entry, even though design doc
        -- §13 itself loosely says "post age" for both.
        ALTER TABLE file_areas ADD COLUMN moderated INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE file_areas ADD COLUMN max_file_age_days INTEGER;

        -- Mirrors posts.status/posts.pinned/posts.exempt_from_expiry
        -- exactly -- see that migration's comments for the full
        -- reasoning, not repeated here. files.pinned is a distinct
        -- concept from file_areas.pinned, same as posts.pinned vs.
        -- boards.pinned.
        ALTER TABLE files ADD COLUMN status TEXT NOT NULL DEFAULT 'approved'
            CHECK (status IN ('pending', 'approved', 'expired'));
        ALTER TABLE files ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE files ADD COLUMN exempt_from_expiry INTEGER NOT NULL DEFAULT 0;

        CREATE INDEX idx_files_area_id_status_created_at_file_id
            ON files(area_id, status, created_at, file_id);
        """,
    ),
    Migration(
        description=(
            "Chat channel moderation restrictions -- mute/ban (design "
            "doc §13), gated by "
            "netbbs.moderation.roles.ChannelPermission.MODERATE. Also "
            "widens channel_messages.kind's CHECK constraint to admit "
            "the five new scrollback event kinds mute/ban/kick land."
        ),
        sql="""
        -- SQLite has no ALTER TABLE to change a CHECK constraint in
        -- place, so this is the standard rebuild: new table with the
        -- widened CHECK, copy every row across, drop the old table,
        -- rename the new one into place, recreate its index. Nothing
        -- else has a foreign key pointing *into* channel_messages, so
        -- this is safe with PRAGMA foreign_keys = ON.
        CREATE TABLE channel_messages_new (
            id                  INTEGER PRIMARY KEY,
            channel_id          INTEGER NOT NULL REFERENCES channels(id),
            kind                TEXT NOT NULL CHECK (
                kind IN ('message', 'join', 'leave', 'mute', 'unmute', 'ban', 'unban', 'kick')
            ),
            author_label        TEXT NOT NULL,
            author_fingerprint  TEXT,
            body                TEXT,
            created_at          TEXT NOT NULL
        );

        INSERT INTO channel_messages_new
            SELECT id, channel_id, kind, author_label, author_fingerprint, body, created_at
            FROM channel_messages;

        DROP TABLE channel_messages;

        ALTER TABLE channel_messages_new RENAME TO channel_messages;

        CREATE INDEX idx_channel_messages_channel_id ON channel_messages(channel_id, id);

        -- One table for both mute and ban, discriminated by kind --
        -- mirrors the channel_messages kind-discriminator precedent:
        -- mute and ban are structurally identical (same duration/
        -- expiry shape, same "is there a live, non-expired row for
        -- (channel, user)" check). Unlike moderator_grants'
        -- object_id, none of these three key columns are nullable, so
        -- a single UNIQUE constraint is sufficient here -- no need
        -- for that table's two-partial-index workaround.
        CREATE TABLE channel_restrictions (
            id                   INTEGER PRIMARY KEY,
            channel_id           INTEGER NOT NULL REFERENCES channels(id),
            user_id              INTEGER NOT NULL REFERENCES users(id),
            kind                 TEXT NOT NULL CHECK (kind IN ('mute', 'ban')),
            -- NULL = indefinite (design doc §13: "no argument = indefinite").
            expires_at           TEXT,
            imposed_by_user_id   INTEGER NOT NULL REFERENCES users(id),
            reason               TEXT,
            created_at           TEXT NOT NULL,

            UNIQUE (channel_id, user_id, kind)
        );

        CREATE INDEX idx_channel_restrictions_lookup
            ON channel_restrictions(channel_id, user_id, kind);
        """,
    ),
    Migration(
        description=(
            "Generic per-user preference store (design doc §13) -- a "
            "per-user mirror of node_config, backing the "
            "vCard bio/visibility fields now and any future per-user "
            "setting (e.g. a per-user chat timestamp preference) "
            "without needing its own storage mechanism."
        ),
        sql="""
        CREATE TABLE user_preferences (
            user_id  INTEGER NOT NULL REFERENCES users(id),
            key      TEXT NOT NULL,
            value    TEXT NOT NULL,
            PRIMARY KEY (user_id, key)
        );
        """,
    ),
    Migration(
        description=(
            "Widens channel_messages.kind's CHECK constraint again to "
            "admit 'action' (/me, design doc) -- same "
            "standard SQLite table-rebuild as the previous widening, "
            "since there's still no ALTER TABLE for changing a CHECK "
            "in place. Deliberately widened only for what this "
            "migration needs, not speculatively for /nick's not-yet-designed "
            "event kind too."
        ),
        sql="""
        CREATE TABLE channel_messages_new (
            id                  INTEGER PRIMARY KEY,
            channel_id          INTEGER NOT NULL REFERENCES channels(id),
            kind                TEXT NOT NULL CHECK (
                kind IN ('message', 'join', 'leave', 'mute', 'unmute', 'ban', 'unban', 'kick', 'action')
            ),
            author_label        TEXT NOT NULL,
            author_fingerprint  TEXT,
            body                TEXT,
            created_at          TEXT NOT NULL
        );

        INSERT INTO channel_messages_new
            SELECT id, channel_id, kind, author_label, author_fingerprint, body, created_at
            FROM channel_messages;

        DROP TABLE channel_messages;

        ALTER TABLE channel_messages_new RENAME TO channel_messages;

        CREATE INDEX idx_channel_messages_channel_id ON channel_messages(channel_id, id);
        """,
    ),
    Migration(
        description=(
            "Widens channel_messages.kind's CHECK constraint again to "
            "admit 'nick' (/nick, design doc) -- same "
            "standard SQLite table-rebuild pattern as the earlier widenings."
        ),
        sql="""
        CREATE TABLE channel_messages_new (
            id                  INTEGER PRIMARY KEY,
            channel_id          INTEGER NOT NULL REFERENCES channels(id),
            kind                TEXT NOT NULL CHECK (
                kind IN (
                    'message', 'join', 'leave', 'mute', 'unmute', 'ban', 'unban', 'kick',
                    'action', 'nick'
                )
            ),
            author_label        TEXT NOT NULL,
            author_fingerprint  TEXT,
            body                TEXT,
            created_at          TEXT NOT NULL
        );

        INSERT INTO channel_messages_new
            SELECT id, channel_id, kind, author_label, author_fingerprint, body, created_at
            FROM channel_messages;

        DROP TABLE channel_messages;

        ALTER TABLE channel_messages_new RENAME TO channel_messages;

        CREATE INDEX idx_channel_messages_channel_id ON channel_messages(channel_id, id);
        """,
    ),
    Migration(
        description=(
            "Adds channels.topic (design doc §8) -- a moderator-editable /topic, distinct from "
            "the existing description column (a creation-time blurb "
            "shown in listings, never edited afterward). Nullable, no "
            "default: a channel starts with no topic set."
        ),
        sql="""
        ALTER TABLE channels ADD COLUMN topic TEXT;
        """,
    ),
    Migration(
        description=(
            "Adds invite-only/hidden channel support (design doc §8): channels.hidden "
            "and channels.members_only are independent axes (a "
            "members_only-but-not-hidden channel still appears in "
            "listings -- 'hidden + open is obscurity, not access "
            "control'), channels.allow_member_invites "
            "is the opt-in described in the design doc. All three "
            "default to 0 (off) -- an existing channel's behavior is "
            "unchanged unless a moderator explicitly opts in. "
            "channel_members is persistent membership -- deliberately "
            "its own table rather than folded into moderator_grants: "
            "membership is access/visibility eligibility, not a "
            "permission bit. channel_invitations tracks the invite-then-"
            "accept flow separately from that direct-grant path; expiry "
            "follows channel_restrictions' precedent (filter at check "
            "time via expires_at, no sweep-on-access needed) rather "
            "than posts/files' sweep-and-delete pattern -- an "
            "invitation row isn't storage anyone needs reclaimed the "
            "way expired content is, it just needs to stop granting "
            "access once past its expiry."
        ),
        sql="""
        ALTER TABLE channels ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE channels ADD COLUMN members_only INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE channels ADD COLUMN allow_member_invites INTEGER NOT NULL DEFAULT 0;

        CREATE TABLE channel_members (
            channel_id          INTEGER NOT NULL REFERENCES channels(id),
            user_id             INTEGER NOT NULL REFERENCES users(id),
            granted_by_user_id  INTEGER NOT NULL REFERENCES users(id),
            created_at          TEXT NOT NULL,

            PRIMARY KEY (channel_id, user_id)
        );

        CREATE TABLE channel_invitations (
            id                   INTEGER PRIMARY KEY,
            channel_id           INTEGER NOT NULL REFERENCES channels(id),
            invited_user_id      INTEGER NOT NULL REFERENCES users(id),
            invited_by_user_id   INTEGER NOT NULL REFERENCES users(id),
            status               TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'revoked')),
            created_at           TEXT NOT NULL,
            -- NULL = indefinite (same convention channel_restrictions
            -- already uses) -- nothing in the command surface sets this
            -- yet (no duration argument on /invite), but the schema
            -- supports it without a further migration once/if one is
            -- added.
            expires_at           TEXT,

            UNIQUE (channel_id, invited_user_id)
        );

        CREATE INDEX idx_channel_invitations_lookup
            ON channel_invitations(channel_id, invited_user_id, status);
        """,
    ),
    Migration(
        description=(
            "SysOp soft-disable (design doc -- SysOp foundation): "
            "a nullable ISO timestamp marking an account as reversibly "
            "login-blocked, following the created_at/last_login_at "
            "TEXT-ISO convention already used on this table. NULL means "
            "not disabled."
        ),
        sql="""
        ALTER TABLE users ADD COLUMN disabled_at TEXT;
        """,
    ),
    Migration(
        description=(
            "Hard-delete support for user accounts (design doc -- SysOp "
            "foundation): every foreign key into users(id) "
            "currently uses SQLite's bare default (NO ACTION), so "
            "deleting a user row today just raises IntegrityError if "
            "anything still references it. This adds real ON DELETE "
            "behavior across every referencing table, via the same "
            "table-rebuild pattern already used for "
            "channel_messages (SQLite has no ALTER TABLE to add a "
            "foreign-key clause in place). Two shapes, chosen per "
            "table: content authorship (posts/files) goes ON DELETE SET "
            "NULL -- both tables already carry a denormalized "
            "author/uploader label+fingerprint specifically so display "
            "survives account removal, so only the live FK needs to go; "
            "everything else referencing users(id) is administrative "
            "data that's meaningless once the account is gone "
            "(moderator grants, channel membership/invitations, "
            "preferences, restrictions, and both blocklist columns), "
            "so those go ON DELETE CASCADE -- blocklist.local_user_id "
            "specifically cannot be SET NULL despite being nullable: "
            "its own CHECK requires exactly one of fingerprint/"
            "local_user_id to be set, and a locally-blocked row has no "
            "fingerprint to fall back on, so SET NULL would leave both "
            "columns NULL and violate that CHECK the moment the "
            "blocked account is deleted (caught by actually running "
            "the migration-cascade test against a seeded row, not by "
            "inspection). The "
            "audit log is its own case: both actor and target go ON "
            "DELETE SET NULL, since an audit trail should survive the "
            "account it names, not be truncated or blocked by its "
            "removal. posts.parent_post_id's self-reference is safe to "
            "rebuild the same way as every other table here -- SQLite "
            "only checks foreign-key constraints at the end of a "
            "statement, not per-row, so the implicit DELETE a DROP "
            "TABLE performs under a table that is its own parent still "
            "ends with zero rows and nothing left to violate. Nothing "
            "outside this migration's own nine tables holds a foreign "
            "key into any of them except that one self-reference."
        ),
        sql="""
        CREATE TABLE posts_new (
            id                  INTEGER PRIMARY KEY,
            post_id             TEXT NOT NULL UNIQUE,
            board_id            INTEGER NOT NULL REFERENCES boards(id),
            parent_post_id      TEXT REFERENCES posts(post_id),
            author_user_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
            author_label        TEXT NOT NULL,
            author_fingerprint  TEXT,
            subject             TEXT NOT NULL,
            body                TEXT NOT NULL,
            created_at          TEXT NOT NULL,
            status              TEXT NOT NULL DEFAULT 'approved' CHECK (status IN ('pending', 'approved', 'expired')),
            pinned              INTEGER NOT NULL DEFAULT 0,
            exempt_from_expiry  INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO posts_new
            SELECT id, post_id, board_id, parent_post_id, author_user_id, author_label,
                author_fingerprint, subject, body, created_at, status, pinned, exempt_from_expiry
            FROM posts;
        DROP TABLE posts;
        ALTER TABLE posts_new RENAME TO posts;
        CREATE INDEX idx_posts_board_id ON posts(board_id);
        CREATE INDEX idx_posts_board_id_created_at_post_id ON posts(board_id, created_at, post_id);
        CREATE INDEX idx_posts_board_id_status_created_at_post_id ON posts(board_id, status, created_at, post_id);

        CREATE TABLE files_new (
            id                    INTEGER PRIMARY KEY,
            file_id               TEXT NOT NULL UNIQUE,
            area_id               INTEGER NOT NULL REFERENCES file_areas(id),
            filename              TEXT NOT NULL,
            description           TEXT,
            size_bytes            INTEGER NOT NULL,
            sha256                TEXT NOT NULL,
            storage_path          TEXT NOT NULL,
            uploader_user_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
            uploader_label        TEXT NOT NULL,
            uploader_fingerprint  TEXT,
            created_at            TEXT NOT NULL,
            status                TEXT NOT NULL DEFAULT 'approved' CHECK (status IN ('pending', 'approved', 'expired')),
            pinned                INTEGER NOT NULL DEFAULT 0,
            exempt_from_expiry    INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO files_new
            SELECT id, file_id, area_id, filename, description, size_bytes, sha256, storage_path,
                uploader_user_id, uploader_label, uploader_fingerprint, created_at, status, pinned,
                exempt_from_expiry
            FROM files;
        DROP TABLE files;
        ALTER TABLE files_new RENAME TO files;
        CREATE INDEX idx_files_area_id ON files(area_id);
        CREATE INDEX idx_files_area_id_created_at_file_id ON files(area_id, created_at, file_id);
        CREATE INDEX idx_files_area_id_status_created_at_file_id ON files(area_id, status, created_at, file_id);

        CREATE TABLE moderator_grants_new (
            id                  INTEGER PRIMARY KEY,
            user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            object_type         TEXT NOT NULL CHECK (object_type IN ('board', 'file_area', 'channel')),
            object_id           INTEGER,
            permissions         INTEGER NOT NULL,
            granted_by_user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at          TEXT NOT NULL
        );
        INSERT INTO moderator_grants_new
            SELECT id, user_id, object_type, object_id, permissions, granted_by_user_id, created_at
            FROM moderator_grants;
        DROP TABLE moderator_grants;
        ALTER TABLE moderator_grants_new RENAME TO moderator_grants;
        CREATE UNIQUE INDEX idx_moderator_grants_per_object
            ON moderator_grants(user_id, object_type, object_id)
            WHERE object_id IS NOT NULL;
        CREATE UNIQUE INDEX idx_moderator_grants_blanket
            ON moderator_grants(user_id, object_type)
            WHERE object_id IS NULL;
        CREATE INDEX idx_moderator_grants_object
            ON moderator_grants(object_type, object_id);

        CREATE TABLE channel_restrictions_new (
            id                   INTEGER PRIMARY KEY,
            channel_id           INTEGER NOT NULL REFERENCES channels(id),
            user_id              INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind                 TEXT NOT NULL CHECK (kind IN ('mute', 'ban')),
            expires_at           TEXT,
            imposed_by_user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            reason               TEXT,
            created_at           TEXT NOT NULL,
            UNIQUE (channel_id, user_id, kind)
        );
        INSERT INTO channel_restrictions_new
            SELECT id, channel_id, user_id, kind, expires_at, imposed_by_user_id, reason, created_at
            FROM channel_restrictions;
        DROP TABLE channel_restrictions;
        ALTER TABLE channel_restrictions_new RENAME TO channel_restrictions;
        CREATE INDEX idx_channel_restrictions_lookup
            ON channel_restrictions(channel_id, user_id, kind);

        CREATE TABLE channel_members_new (
            channel_id          INTEGER NOT NULL REFERENCES channels(id),
            user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            granted_by_user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at          TEXT NOT NULL,
            PRIMARY KEY (channel_id, user_id)
        );
        INSERT INTO channel_members_new
            SELECT channel_id, user_id, granted_by_user_id, created_at
            FROM channel_members;
        DROP TABLE channel_members;
        ALTER TABLE channel_members_new RENAME TO channel_members;

        CREATE TABLE channel_invitations_new (
            id                   INTEGER PRIMARY KEY,
            channel_id           INTEGER NOT NULL REFERENCES channels(id),
            invited_user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            invited_by_user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            status               TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'revoked')),
            created_at           TEXT NOT NULL,
            expires_at           TEXT,
            UNIQUE (channel_id, invited_user_id)
        );
        INSERT INTO channel_invitations_new
            SELECT id, channel_id, invited_user_id, invited_by_user_id, status, created_at, expires_at
            FROM channel_invitations;
        DROP TABLE channel_invitations;
        ALTER TABLE channel_invitations_new RENAME TO channel_invitations;
        CREATE INDEX idx_channel_invitations_lookup
            ON channel_invitations(channel_id, invited_user_id, status);

        CREATE TABLE user_preferences_new (
            user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            key      TEXT NOT NULL,
            value    TEXT NOT NULL,
            PRIMARY KEY (user_id, key)
        );
        INSERT INTO user_preferences_new SELECT user_id, key, value FROM user_preferences;
        DROP TABLE user_preferences;
        ALTER TABLE user_preferences_new RENAME TO user_preferences;

        CREATE TABLE blocklist_new (
            id                  INTEGER PRIMARY KEY,
            fingerprint         TEXT,
            -- CASCADE, not SET NULL: this table's own CHECK requires
            -- exactly one of fingerprint/local_user_id to be set, and
            -- a locally-blocked row has no fingerprint to fall back on
            -- -- SET NULL would leave both columns NULL and violate
            -- the CHECK the moment the blocked account is deleted.
            -- With no persistent (fingerprint-based) identity left to
            -- keep blocking, removing the row entirely is correct.
            local_user_id       INTEGER REFERENCES users(id) ON DELETE CASCADE,
            reason              TEXT,
            blocked_by_user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at          TEXT NOT NULL,
            CHECK ((fingerprint IS NOT NULL) != (local_user_id IS NOT NULL))
        );
        INSERT INTO blocklist_new
            SELECT id, fingerprint, local_user_id, reason, blocked_by_user_id, created_at
            FROM blocklist;
        DROP TABLE blocklist;
        ALTER TABLE blocklist_new RENAME TO blocklist;
        CREATE UNIQUE INDEX idx_blocklist_fingerprint
            ON blocklist(fingerprint) WHERE fingerprint IS NOT NULL;
        CREATE UNIQUE INDEX idx_blocklist_local_user_id
            ON blocklist(local_user_id) WHERE local_user_id IS NOT NULL;

        CREATE TABLE moderation_log_new (
            id                INTEGER PRIMARY KEY,
            actor_user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
            action            TEXT NOT NULL,
            object_type       TEXT,
            object_id         INTEGER,
            target_user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
            detail            TEXT,
            created_at        TEXT NOT NULL
        );
        INSERT INTO moderation_log_new
            SELECT id, actor_user_id, action, object_type, object_id, target_user_id, detail, created_at
            FROM moderation_log;
        DROP TABLE moderation_log;
        ALTER TABLE moderation_log_new RENAME TO moderation_log;
        CREATE INDEX idx_moderation_log_object ON moderation_log(object_type, object_id, created_at);
        CREATE INDEX idx_moderation_log_target_user ON moderation_log(target_user_id, created_at);
        """,
    ),
    Migration(
        description=(
            "Case-insensitive usernames: login (get_user_by_username, "
            "password login, both keypair login paths) compared "
            "`username = ?` under SQLite's default BINARY collation, so "
            "logging in required typing the exact case a username was "
            "registered with, and `users.username`'s UNIQUE constraint "
            "was likewise case-sensitive, so 'Thiesi' and 'thiesi' could "
            "coexist as two distinct, mutually-invisible accounts. A "
            "plain `CREATE UNIQUE INDEX ... (username COLLATE NOCASE)` "
            "closes both, without the table-rebuild this project's "
            "'never edit a shipped migration' rule and the earlier "
            "CHECK-widening/hard-delete migrations would otherwise suggest: `users` "
            "is the referenced *parent* of nine tables' foreign keys "
            "(several with ON DELETE CASCADE/SET NULL from the "
            "hard-delete migration above), and SQLite's DROP TABLE performs an "
            "implicit DELETE FROM first when foreign_keys=ON (as this "
            "connection always runs) -- rebuilding `users` itself the "
            "same way would cascade-wipe every other user's moderator "
            "grants, channel membership, preferences, and blocklist "
            "entries, and null out post/file authorship, as a side "
            "effect of fixing a login bug. An index-only fix has no "
            "such risk. Existing case-variant duplicate usernames, if "
            "any, would make this migration fail loudly rather than "
            "silently pick a winner -- acceptable at this project's "
            "current single-sysop-node stage (see the auth-users "
            "sign-off note for the full reasoning)."
        ),
        sql="""
        CREATE UNIQUE INDEX idx_users_username_nocase ON users(username COLLATE NOCASE);
        """,
    ),
    Migration(
        description=(
            "Post editing (design doc -- prose editor, planning "
            "phase): `posts.root_post_id` groups every revision of the same "
            "logical post together (a post's own post_id for an original "
            "creation; the *original* post's post_id for every edit of it, "
            "not the immediately-preceding revision's), and "
            "`posts.edit_of_post_id` records the specific immediate "
            "predecessor each edit revises -- kept for a future edit-history "
            "view, not surfaced anywhere yet. Editing never mutates a row "
            "in place: post_id is a content hash of the body itself "
            "(netbbs.boards.content_id), so an in-place UPDATE would leave "
            "post_id silently mismatched against its own row's content, and "
            "existing replies reference a specific post_id directly -- "
            "mutating it out from under them would orphan every reply to a "
            "since-edited parent. An edit is instead a brand-new row with "
            "its own fresh content-addressed post_id, chained back via "
            "these two columns. Plain ADD COLUMN, not a table rebuild -- "
            "posts is a live parent of several other tables' foreign keys, "
            "and the design doc's own sign-off notes already found the hard way "
            "that SQLite's DROP TABLE (the rebuild pattern's first step) "
            "applies its own cascade/SET-NULL side effects to *any* row "
            "still referencing the dropped table, independent of that "
            "column's declared ON DELETE behavior -- avoided entirely by "
            "not dropping posts at all here. Every pre-existing post "
            "becomes the root of its own single-row group."
        ),
        sql="""
        ALTER TABLE posts ADD COLUMN root_post_id TEXT REFERENCES posts(post_id);
        ALTER TABLE posts ADD COLUMN edit_of_post_id TEXT REFERENCES posts(post_id);
        UPDATE posts SET root_post_id = post_id WHERE root_post_id IS NULL;
        CREATE INDEX idx_posts_root_post_id_board_id_status_created_at
            ON posts(root_post_id, board_id, status, created_at);
        """,
    ),
    Migration(
        description=(
            "Self-service registration + node-wide approval gate (design "
            "doc): users.pending_approval marks an account "
            "created via self-service Telnet/SSH/web registration as not "
            "yet allowed to log in, when the node-wide "
            "'require_registration_approval' SysOp setting (netbbs.config) "
            "is turned on -- default off, instant activation. A dedicated "
            "column rather than reusing disabled_at/set_user_disabled: "
            "that mechanism's changed_by/moderation-log semantics assume "
            "a human SysOp actively disabled an existing account for a "
            "reason worth recording, which doesn't fit a system-generated "
            "'brand new, awaiting first review' state with no actor yet -- "
            "and a SysOp reviewing the user list needs to tell 'awaiting "
            "approval' apart from 'disabled/banned' at a glance, not see "
            "them collapsed into one status. Plain ADD COLUMN, not a "
            "table rebuild -- same reasoning as the earlier "
            "posts.root_post_id migration above (users is itself a live parent "
            "of many other tables' foreign keys, and SQLite's DROP TABLE "
            "cascade side effect during a rebuild is exactly what that "
            "migration's own comment already warns against)."
        ),
        sql="""
        ALTER TABLE users ADD COLUMN pending_approval INTEGER NOT NULL DEFAULT 0;
        """,
    ),
    Migration(
        description=(
            "Widens channel_messages.kind's CHECK constraint again to "
            "admit 'daybreak' (design doc) -- a local, per-node "
            "system announcement broadcast to every channel that "
            "currently has at least one participant, once at local "
            "midnight (netbbs.chat.daybreak). Same standard SQLite "
            "table-rebuild pattern as the earlier CHECK widenings, since there's "
            "still no ALTER TABLE for changing a CHECK in place."
        ),
        sql="""
        CREATE TABLE channel_messages_new (
            id                  INTEGER PRIMARY KEY,
            channel_id          INTEGER NOT NULL REFERENCES channels(id),
            kind                TEXT NOT NULL CHECK (
                kind IN (
                    'message', 'join', 'leave', 'mute', 'unmute', 'ban', 'unban', 'kick',
                    'action', 'nick', 'daybreak'
                )
            ),
            author_label        TEXT NOT NULL,
            author_fingerprint  TEXT,
            body                TEXT,
            created_at          TEXT NOT NULL
        );

        INSERT INTO channel_messages_new
            SELECT id, channel_id, kind, author_label, author_fingerprint, body, created_at
            FROM channel_messages;

        DROP TABLE channel_messages;

        ALTER TABLE channel_messages_new RENAME TO channel_messages;

        CREATE INDEX idx_channel_messages_channel_id ON channel_messages(channel_id, id);
        """,
    ),
    Migration(
        description=(
            "Identity attestation (design doc, "
            "netbbs.attestation): users.can_verify_identity is a new, "
            "narrow, SysOp-grantable permission independent of the "
            "four moderator scope tiers -- a plain boolean, since "
            "verifying a real-world fact about a person isn't authority "
            "over a specific board/area/channel. user_attestations "
            "records a verifier's signed claim about a subject's age or "
            "real name -- attested_value is the actual determined value "
            "(a birthdate or a real name), not a threshold-specific "
            "pass/fail, so one attestation stays valid against any "
            "future gate. verifier_user_id/fingerprint/signature are "
            "all nullable: a verifier without a personal keypair has no "
            "node identity to sign with yet (node-vouching is Phase 3 "
            "scope, same nullable-until-Phase-3 shape already used for "
            "boards.origin_node_fingerprint) -- local accountability for "
            "an unsigned attestation still comes from moderation_log, "
            "which every verify action also writes to. min_age/"
            "name_requirement on boards/channels/file_areas are "
            "nullable -- NULL currently just means no gate, but the "
            "same nullable-means-inherit shape already used elsewhere "
            "will let a future Community cascade a default "
            "onto them without a second migration."
        ),
        sql="""
        ALTER TABLE users ADD COLUMN can_verify_identity INTEGER NOT NULL DEFAULT 0;

        CREATE TABLE user_attestations (
            id                   INTEGER PRIMARY KEY,
            subject_user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            attribute            TEXT NOT NULL CHECK (attribute IN ('age', 'name')),
            attested_value       TEXT NOT NULL,
            verifier_user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
            verifier_fingerprint TEXT,
            signature            TEXT,
            created_at           TEXT NOT NULL,
            link_visible         INTEGER NOT NULL DEFAULT 0,

            -- One current attestation per (subject, attribute) -- a new
            -- verification replaces the old one rather than
            -- accumulating a history nothing here needs yet.
            UNIQUE (subject_user_id, attribute)
        );

        CREATE INDEX idx_user_attestations_subject ON user_attestations(subject_user_id);

        ALTER TABLE boards ADD COLUMN min_age INTEGER;
        ALTER TABLE boards ADD COLUMN name_requirement TEXT
            CHECK (name_requirement IN ('verified', 'verified_and_displayed') OR name_requirement IS NULL);

        ALTER TABLE channels ADD COLUMN min_age INTEGER;
        ALTER TABLE channels ADD COLUMN name_requirement TEXT
            CHECK (name_requirement IN ('verified', 'verified_and_displayed') OR name_requirement IS NULL);

        ALTER TABLE file_areas ADD COLUMN min_age INTEGER;
        ALTER TABLE file_areas ADD COLUMN name_requirement TEXT
            CHECK (name_requirement IN ('verified', 'verified_and_displayed') OR name_requirement IS NULL);
        """,
    ),
    Migration(
        description=(
            "Local asynchronous personal mail (design doc, "
            "netbbs.mail) -- deliberately not the same mechanism as "
            "/msg (netbbs.chat.mailbox), which stays ephemeral and "
            "online-only. One row per message; sender_user_id is "
            "nullable (SET NULL on account deletion, same reasoning as "
            "posts.author_user_id) with sender_label denormalized "
            "alongside it so a message's provenance survives the "
            "sender's account being deleted, matching an existing "
            "precedent. recipient_user_id is NOT NULL and CASCADEs --  "
            "unlike a post, this row has no meaning independent of the "
            "one recipient's inbox it belongs to. sender_deleted_at/ "
            "recipient_deleted_at are independent: each side manages "
            "their own view of the same message, and the row is hard-"
            "deleted at the application level once both are set, "
            "rather than a background sweep -- there is no time-based "
            "expiry here, only two point-in-time user actions to check "
            "after either one fires."
        ),
        sql="""
        CREATE TABLE mail_messages (
            id                    INTEGER PRIMARY KEY,
            sender_user_id        INTEGER REFERENCES users(id) ON DELETE SET NULL,
            sender_label          TEXT NOT NULL,
            recipient_user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            subject               TEXT NOT NULL,
            body                  TEXT NOT NULL,
            created_at            TEXT NOT NULL,
            read_at               TEXT,
            sender_deleted_at     TEXT,
            recipient_deleted_at  TEXT
        );

        CREATE INDEX idx_mail_messages_recipient
            ON mail_messages(recipient_user_id, recipient_deleted_at, created_at);
        CREATE INDEX idx_mail_messages_sender
            ON mail_messages(sender_user_id, sender_deleted_at, created_at);
        """,
    ),
    Migration(
        description=(
            "Communities (design doc §16): a "
            "topic-oriented navigation/container layer above boards/"
            "channels/file areas. Local Communities only in this "
            "migration -- Link Communities are a property "
            "layered onto the same `communities` row once Phase 6's "
            "signed-event machinery exists, not a separate table. "
            "community_id is a nullable FK on all three resource "
            "tables (zero-or-one, never several -- see the "
            "design doc for why many-to-many and a mandatory-with-"
            "default-'Uncategorized' shape were both rejected). "
            "boards.min_read_level/min_write_level and file_areas."
            "min_read_level/min_write_level become nullable here: "
            "NULL now means 'inherit this Community's "
            "default, or the hardcoded system default of 0 if the "
            "resource has no Community' -- an explicit stored value, "
            "including 0, always wins outright and is never overridden "
            "by a Community default. Existing rows keep their current "
            "explicit 0 unchanged by this migration (a plain column-"
            "relax, no data rewritten) -- a SysOp opts a given resource "
            "into inheriting by explicitly clearing it afterwards. "
            "Uses the same table-rebuild pattern as the "
            "user-deletion-cascade migration above (SQLite has no ALTER "
            "TABLE to relax a NOT NULL/remove a DEFAULT in place). "
            "channels.min_level is deliberately NOT touched by this "
            "migration -- the design doc's Community edit-"
            "screen spec names exactly four inheritable defaults "
            "(min_read_level, min_write_level, min_age, "
            "name_requirement), all sized for boards/file-areas' read/"
            "write-pair or all-three-types' age/name shape; it never "
            "names a channel-specific level default, and channels "
            "already deliberately have no read/write split "
            "that a hypothetical single inherited level would map onto "
            "cleanly. Filling that in as a new field the design doc "
            "never specified would be scope creep, not implementation "
            "-- channels.min_level stays exactly what it is today, "
            "not gated through Community, while min_age/"
            "name_requirement (already nullable on all three tables "
            "since the identity attestation migration above) gain Community inheritance for channels "
            "same as boards/file-areas. "
            "moderator_grants gains a nullable community_id column for "
            "the new Community-blanket grant tier ('the same "
            "shape local-blanket already has over the whole node,' "
            "narrowed to one Community's membership) -- the two "
            "existing partial unique indexes are replaced: local-"
            "blanket uniqueness now also requires community_id IS NULL "
            "(so a Community-blanket row for the same user/object_type "
            "no longer collides with it under SQLite's own NULL-"
            "distinctness rule), plus a new partial unique index for "
            "Community-blanket uniqueness itself."
        ),
        sql="""
        CREATE TABLE communities (
            id                         INTEGER PRIMARY KEY,
            name                       TEXT NOT NULL UNIQUE,
            description                TEXT,
            hidden                     INTEGER NOT NULL DEFAULT 0,
            default_min_read_level     INTEGER,
            default_min_write_level    INTEGER,
            default_min_age            INTEGER,
            default_name_requirement   TEXT
                CHECK (default_name_requirement IN ('verified', 'verified_and_displayed')
                       OR default_name_requirement IS NULL),
            created_at                 TEXT NOT NULL
        );

        ALTER TABLE channels ADD COLUMN community_id INTEGER REFERENCES communities(id);
        CREATE INDEX idx_channels_community_id ON channels(community_id);

        CREATE TABLE boards_new (
            id                       INTEGER PRIMARY KEY,
            board_id                 TEXT NOT NULL UNIQUE,
            name                     TEXT NOT NULL UNIQUE,
            description              TEXT,
            origin_node_fingerprint  TEXT,
            min_read_level           INTEGER,
            min_write_level          INTEGER,
            created_at               TEXT NOT NULL,
            category_id              INTEGER REFERENCES board_categories(id),
            pinned                   INTEGER NOT NULL DEFAULT 0,
            moderated                INTEGER NOT NULL DEFAULT 0,
            max_post_age_days        INTEGER,
            min_age                  INTEGER,
            name_requirement         TEXT
                CHECK (name_requirement IN ('verified', 'verified_and_displayed') OR name_requirement IS NULL),
            community_id             INTEGER REFERENCES communities(id)
        );
        INSERT INTO boards_new
            SELECT id, board_id, name, description, origin_node_fingerprint, min_read_level,
                   min_write_level, created_at, category_id, pinned, moderated, max_post_age_days,
                   min_age, name_requirement, NULL AS community_id
            FROM boards;
        DROP TABLE boards;
        ALTER TABLE boards_new RENAME TO boards;
        CREATE INDEX idx_boards_board_id ON boards(board_id);
        CREATE INDEX idx_boards_community_id ON boards(community_id);

        CREATE TABLE file_areas_new (
            id                       INTEGER PRIMARY KEY,
            area_id                  TEXT NOT NULL UNIQUE,
            name                     TEXT NOT NULL UNIQUE,
            description              TEXT,
            origin_node_fingerprint  TEXT,
            min_read_level           INTEGER,
            min_write_level          INTEGER,
            category_id              INTEGER REFERENCES file_area_categories(id),
            pinned                   INTEGER NOT NULL DEFAULT 0,
            created_at               TEXT NOT NULL,
            moderated                INTEGER NOT NULL DEFAULT 0,
            max_file_age_days        INTEGER,
            min_age                  INTEGER,
            name_requirement         TEXT
                CHECK (name_requirement IN ('verified', 'verified_and_displayed') OR name_requirement IS NULL),
            community_id             INTEGER REFERENCES communities(id)
        );
        INSERT INTO file_areas_new
            SELECT id, area_id, name, description, origin_node_fingerprint, min_read_level,
                   min_write_level, category_id, pinned, created_at, moderated, max_file_age_days,
                   min_age, name_requirement, NULL AS community_id
            FROM file_areas;
        DROP TABLE file_areas;
        ALTER TABLE file_areas_new RENAME TO file_areas;
        CREATE INDEX idx_file_areas_area_id ON file_areas(area_id);
        CREATE INDEX idx_file_areas_community_id ON file_areas(community_id);

        ALTER TABLE moderator_grants ADD COLUMN community_id INTEGER REFERENCES communities(id);

        DROP INDEX idx_moderator_grants_blanket;
        CREATE UNIQUE INDEX idx_moderator_grants_blanket
            ON moderator_grants(user_id, object_type)
            WHERE object_id IS NULL AND community_id IS NULL;
        CREATE UNIQUE INDEX idx_moderator_grants_community_blanket
            ON moderator_grants(user_id, object_type, community_id)
            WHERE object_id IS NULL AND community_id IS NOT NULL;
        """,
    ),
    Migration(
        description=(
            "NetBBS Link persistent storage (design doc): peer table and "
            "seen-event/event-body store, replacing netbbs.link.protocol.LinkNode's "
            "previously in-memory-only peers/known_event_ids/events."
        ),
        sql="""
        CREATE TABLE link_peers (
            fingerprint       TEXT PRIMARY KEY,
            root_public_key   TEXT NOT NULL,  -- base64
            transitions_json  TEXT NOT NULL,  -- JSON list of KeyTransition.to_dict()
            descriptor_json   TEXT NOT NULL,  -- JSON EndpointDescriptor.to_dict()
            updated_at        TEXT NOT NULL
        );

        -- Doubles as both the seen-event dedup table and the event-body
        -- store (§7 already describes them as one concept, not two). No retention-
        -- window purging yet -- blocked on the real chain-
        -- idempotency gap that has to close first.
        CREATE TABLE link_events (
            content_id          TEXT PRIMARY KEY,
            sender_fingerprint  TEXT NOT NULL,
            object_type         TEXT NOT NULL,
            envelope_json       TEXT NOT NULL,  -- the raw event dict as received
            received_at         TEXT NOT NULL
        );
        CREATE INDEX idx_link_events_sender ON link_events(sender_fingerprint);
        """,
    ),
    Migration(
        description=(
            "Local-origination columns for linked boards (design doc): "
            "NULL means 'not Linked yet' / 'no board_"
            "post built yet' -- an explicit value means this row's own signed "
            "board_genesis/board_post envelope (netbbs.link.events.BoardGenesis/"
            "BoardPost .to_dict()), built and stored once, then pushed to peers "
            "every sync pass same as a key_transition ('harmless "
            "no-op resend' model) rather than tracked as delivered per-peer."
        ),
        sql="""
        ALTER TABLE boards ADD COLUMN link_genesis_json TEXT;
        ALTER TABLE posts ADD COLUMN link_event_json TEXT;
        """,
    ),
    Migration(
        description=(
            "Link messages (design doc): mail_messages rebuilt so one "
            "row can also represent an outbound message addressed to a remote "
            "Link user, or a message actually received via Link -- confirmed "
            "with Thiesi to unify into this table rather than a separate "
            "outbox, so Sent-folder listing stays one query regardless of "
            "destination, at the cost of a rebuild rather than a plain ADD "
            "COLUMN (SQLite has no ALTER TABLE to relax an existing NOT NULL "
            "in place; same table-rebuild technique as the 'Hard-delete "
            "support for user accounts' migration above). "
            "recipient_user_id becomes nullable; recipient_remote_address is "
            "populated instead for an outbound Link-addressed row -- the new "
            "CHECK constraint enforces exactly one of the two is ever set, "
            "for every row including every pre-existing local-mail one. "
            "link_event_json is this node's own signed LinkMessage for an "
            "outbound row, re-pushed every sync pass until link_delivery_"
            "status leaves 'pending' (same 'push everything, dedup handles "
            "idempotency' model board_post/board_post_edit already use); "
            "'expired' is included in that CHECK now even though nothing "
            "produces it yet (link_message_expired isn't built yet) "
            "since the design doc already names it and widening this CHECK later "
            "would mean yet another rebuild for one enum value. "
            "link_source_event_id marks a row as arrived via Link (the "
            "original message's own content_id) -- an explicit marker, not "
            "inferred from sender_user_id IS NULL, since that already means "
            "something else on this table (the local sender's account was "
            "deleted) and the two must never be confused. Tier 1 "
            "(tier1_home_node_key) only (design doc's "
            "tier-2 finding: the server can never hold a tier-2 user's "
            "decryption key, so nothing here assumes or names a tier at all). "
            "The new link_mail_acknowledgements table holds pending outbound "
            "accepted/bounced acknowledgements -- deliberately not columns on "
            "mail_messages, since a bounced incoming message never gets a "
            "mail_messages row at all (nothing was actually delivered), so "
            "the two concerns can't share one row's lifecycle. "
            "link_event_content_id is an outbound row's own link_event_json's "
            "content_id, stored redundantly (content_id is otherwise only a "
            "computed hash of the envelope) so an incoming acknowledgement can "
            "find the row it's about with an indexed lookup, not by "
            "recomputing that hash for every pending row."
        ),
        sql="""
        CREATE TABLE mail_messages_new (
            id                        INTEGER PRIMARY KEY,
            sender_user_id            INTEGER REFERENCES users(id) ON DELETE SET NULL,
            sender_label              TEXT NOT NULL,
            recipient_user_id         INTEGER REFERENCES users(id) ON DELETE CASCADE,
            recipient_remote_address  TEXT,
            subject                   TEXT NOT NULL,
            body                      TEXT NOT NULL,
            created_at                TEXT NOT NULL,
            read_at                   TEXT,
            sender_deleted_at         TEXT,
            recipient_deleted_at      TEXT,
            link_event_json           TEXT,
            link_event_content_id     TEXT,
            link_delivery_status      TEXT CHECK (
                link_delivery_status IN ('pending', 'delivered', 'bounced', 'expired')
                OR link_delivery_status IS NULL
            ),
            link_source_event_id      TEXT,
            CHECK ((recipient_user_id IS NOT NULL) <> (recipient_remote_address IS NOT NULL))
        );
        INSERT INTO mail_messages_new
            (id, sender_user_id, sender_label, recipient_user_id, subject, body,
             created_at, read_at, sender_deleted_at, recipient_deleted_at)
            SELECT id, sender_user_id, sender_label, recipient_user_id, subject, body,
                created_at, read_at, sender_deleted_at, recipient_deleted_at
            FROM mail_messages;
        DROP TABLE mail_messages;
        ALTER TABLE mail_messages_new RENAME TO mail_messages;
        CREATE INDEX idx_mail_messages_recipient
            ON mail_messages(recipient_user_id, recipient_deleted_at, created_at);
        CREATE INDEX idx_mail_messages_sender
            ON mail_messages(sender_user_id, sender_deleted_at, created_at);
        CREATE INDEX idx_mail_messages_link_event_content_id
            ON mail_messages(link_event_content_id)
            WHERE link_event_content_id IS NOT NULL;
        CREATE INDEX idx_mail_messages_link_pending
            ON mail_messages(link_delivery_status)
            WHERE link_delivery_status = 'pending';

        CREATE TABLE link_mail_acknowledgements (
            id                       INTEGER PRIMARY KEY,
            message_content_id       TEXT NOT NULL,
            target_node_fingerprint  TEXT NOT NULL,
            ack_event_json           TEXT NOT NULL,
            created_at               TEXT NOT NULL,
            sent_at                  TEXT
        );
        CREATE INDEX idx_link_mail_acknowledgements_pending
            ON link_mail_acknowledgements(target_node_fingerprint)
            WHERE sent_at IS NULL;
        """,
    ),
    Migration(
        description=(
            "Peer-list exchange (design doc): unverified candidate "
            "endpoint descriptors learned secondhand from an already-verified "
            "peer -- deliberately a separate table from link_peers, never "
            "promoted into it until a real hello with that fingerprint "
            "actually completes (netbbs.link.protocol.LinkNode.handle_hello "
            "already deletes the matching row here when that happens, in "
            "memory; the corresponding on-disk delete is "
            "netbbs.link.store function's job, not a trigger)."
        ),
        sql="""
        CREATE TABLE link_peer_candidates (
            fingerprint      TEXT PRIMARY KEY,
            descriptor_json  TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        );
        """,
    ),
    Migration(
        description=(
            "Origin succession/transfer (design doc, issue #53): "
            "two new nullable columns on boards, both NULL for every "
            "existing row including already-Linked ones -- deliberately no "
            "backfill (this codebase parses link_genesis_json in Python "
            "everywhere else, never json_extract() in SQL; introducing that "
            "pattern for a one-time backfill wasn't worth it). "
            "link_origin_fingerprint is an *override*: NULL means 'no "
            "transfer has ever completed for this board, the origin is "
            "still link_genesis_json's own origin_fingerprint' -- callers "
            "resolve the current origin by checking this column first and "
            "falling back to the genesis's own field, never reading either "
            "alone. Only ever written once a board_origin_transfer_accepted "
            "is actually verified and accepted. link_lifecycle_json holds "
            "this node's own latest self-originated lifecycle event (an "
            "offer this node made as current origin, or an acceptance this "
            "node made as a newly-accepted origin) -- re-pushed every sync "
            "pass alongside link_genesis_json, same 'push everything, dedup "
            "handles idempotency' model board_post/board_post_edit already "
            "use. NULL means this node has never originated a lifecycle "
            "event for this board (the overwhelmingly common case)."
        ),
        sql="""
        ALTER TABLE boards ADD COLUMN link_origin_fingerprint TEXT;
        ALTER TABLE boards ADD COLUMN link_lifecycle_json TEXT;
        """,
    ),
    Migration(
        description=(
            "WAN reachability / relay selection (design doc §12, "
            "issue #58). link_reliability: this node's own direct-observation "
            "dial-outcome tally per fingerprint (netbbs.link.reliability) -- "
            "the from-scratch tracker built because §6's own reputation "
            "system, which the design doc assumed relay scoring would reuse, "
            "turned out not to exist anywhere in this codebase. "
            "link_relay_consents: relays this node has accepted to serve for "
            "(as the relay), or that have accepted to serve for this node "
            "(as the outgoing-only party) -- direction distinguished by "
            "`role`. link_relay_mailbox: opaque envelopes a relay is "
            "temporarily holding for a specific recipient fingerprint, "
            "bounded per recipient (netbbs.link.relay) -- picked up and "
            "deleted the next time that recipient dials this relay as part "
            "of its own outbound sync pass, never decrypted or inspected "
            "here (a relay only ever custodies ciphertext, design doc's own "
            "confidentiality-tier guarantee applying unchanged)."
        ),
        sql="""
        CREATE TABLE link_reliability (
            fingerprint  TEXT PRIMARY KEY,
            attempts     INTEGER NOT NULL DEFAULT 0,
            successes    INTEGER NOT NULL DEFAULT 0,
            updated_at   TEXT NOT NULL
        );

        CREATE TABLE link_relay_consents (
            id            INTEGER PRIMARY KEY,
            fingerprint   TEXT NOT NULL,
            role          TEXT NOT NULL CHECK (role IN ('relay_for_me', 'i_relay_for')),
            accepted_at   TEXT NOT NULL,
            UNIQUE(fingerprint, role)
        );

        CREATE TABLE link_relay_mailbox (
            content_id            TEXT PRIMARY KEY,
            recipient_fingerprint TEXT NOT NULL,
            sender_fingerprint    TEXT NOT NULL,
            object_type           TEXT NOT NULL,
            envelope_json         TEXT NOT NULL,
            received_at           TEXT NOT NULL
        );
        CREATE INDEX idx_link_relay_mailbox_recipient
            ON link_relay_mailbox(recipient_fingerprint, received_at);
        """,
    ),
    Migration(
        description=(
            "Issue #56: per-user read cursors and follow/favourite state "
            "(design doc §6.6). user_read_cursors is the newest item a user "
            "has been shown per board/channel/file_area -- object_id is "
            "polymorphic (that resource's own local integer id), so it gets "
            "no FK, the same shape moderator_grants already uses for the "
            "identical reason. last_seen_stable_id is the content-addressed "
            "post_id/file_id for boards/file_areas, or a channel message's "
            "plain integer id stored as text (netbbs.activity encapsulates "
            "the comparison difference so no caller has to know). "
            "user_follows is a separate, independent table (never folded "
            "into channel_members/moderator_grants or any node-carry "
            "concept) -- object_type also allows 'community', for a future "
            "follow-a-whole-Community convenience layered on top of these "
            "same per-resource rows."
        ),
        sql="""
        CREATE TABLE user_read_cursors (
            user_id               INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            object_type           TEXT NOT NULL CHECK (object_type IN ('board', 'channel', 'file_area')),
            object_id             INTEGER NOT NULL,
            last_seen_created_at  TEXT NOT NULL,
            last_seen_stable_id   TEXT NOT NULL,
            updated_at            TEXT NOT NULL,
            PRIMARY KEY (user_id, object_type, object_id)
        );

        CREATE TABLE user_follows (
            user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            object_type  TEXT NOT NULL CHECK (object_type IN ('community', 'board', 'channel', 'file_area')),
            object_id    INTEGER NOT NULL,
            created_at   TEXT NOT NULL,
            PRIMARY KEY (user_id, object_type, object_id)
        );
        """,
    ),
    Migration(
        description=(
            "Local search (design doc §6.6, issue #56's last piece): FTS5 "
            "virtual tables for board posts, files, and channel messages."
        ),
        sql="""
        -- Fails loudly with sqlite3.OperationalError("no such module: fts5")
        -- on a SQLite build lacking FTS5, rather than degrading silently
        -- (CLAUDE.md "fail clearly") -- traced, not just hoped, for this
        -- project's actual NetBSD/pkgsrc target: lang/python312's Makefile
        -- buildlinks against databases/sqlite3 (not an amalgamation bundled
        -- into Python itself), and that package's own Makefile passes
        -- --fts5 unconditionally in CONFIGURE_ARGS -- so pkgsrc's Python
        -- sqlite3 module should always have it, not just this dev machine.
        --
        -- One row per currently-authoritative piece of content, kept in
        -- sync by explicit application-level calls from
        -- netbbs.boards.posts/netbbs.files.entries/netbbs.chat.scrollback
        -- at every write path (create/edit/approve/delete/expire/trim) --
        -- deliberately not SQL triggers, matching this schema's existing
        -- convention of zero triggers anywhere else and keeping the sync
        -- logic visible in Python rather than hidden in SQL. See
        -- netbbs.search for the reindex functions and the query-time
        -- authorization that reuses the exact same level/age/visibility
        -- gates normal browsing already enforces.
        --
        -- A post has an edit chain (root_post_id); only the resolved
        -- *current* approved revision for a root is ever indexed, matching
        -- netbbs.boards.posts._resolve_current_version and
        -- list_posts_page's own visibility -- never a superseded or
        -- not-yet-approved revision. Files have no edit chain, so
        -- file_search mirrors the files table one-to-one. Channel messages
        -- are trimmed by netbbs.chat.scrollback's own bounded ring buffer;
        -- channel_message_search is pruned the same way, so a message
        -- search can never surface content already trimmed out of
        -- scrollback.
        CREATE VIRTUAL TABLE post_search USING fts5(
            subject, body, board_id UNINDEXED, root_post_id UNINDEXED
        );

        CREATE VIRTUAL TABLE file_search USING fts5(
            filename, description, area_id UNINDEXED, file_id UNINDEXED
        );

        CREATE VIRTUAL TABLE channel_message_search USING fts5(
            body, channel_id UNINDEXED, message_id UNINDEXED
        );
        """,
    ),
    Migration(
        description=(
            "Outbound work items (design doc §13.7, issue #60's second "
            "operational slice): a generic pending/retrying/pushed/"
            "dead_lettered/cancelled tracker, scoped to exactly the two "
            "existing Link mechanisms that fit this shape -- Link mail "
            "delivery and Link mail acknowledgement delivery -- which "
            "retry forever today with no cap."
        ),
        sql="""
        -- reference_id is a pointer (mail_messages.link_event_content_id
        -- for kind='link_mail_delivery', or link_mail_acknowledgements.id
        -- as text for kind='link_mail_ack'), never a payload copy --
        -- netbbs.link.work_items never stores or looks at the actual
        -- signed event bytes, only the caller (netbbs.link.sync) does.
        --
        -- 'pushed' means the payload was successfully handed to the
        -- recipient's transport/relay, never that the recipient confirmed
        -- receipt -- that remains mail_messages.link_delivery_status's own
        -- independent accepted/bounced-event-driven vocabulary. This is a
        -- deliberate distinction (design doc §13.7): conflating "pushed"
        -- with "delivered" would have been a real, if subtle, bug.
        --
        -- UNIQUE(kind, reference_id, target_fingerprint) makes
        -- enqueue_work_item naturally idempotent -- composing the same
        -- message/queuing the same acknowledgement twice (should never
        -- happen, but costs nothing to make impossible) can't create a
        -- second competing work item for the same delivery.
        CREATE TABLE link_work_items (
            id                  INTEGER PRIMARY KEY,
            kind                TEXT NOT NULL,
            reference_id        TEXT NOT NULL,
            target_fingerprint  TEXT NOT NULL,
            status              TEXT NOT NULL
                                CHECK (status IN ('pending', 'retrying', 'pushed', 'dead_lettered', 'cancelled')),
            attempts            INTEGER NOT NULL DEFAULT 0,
            next_attempt_at     TEXT NOT NULL,
            created_at          TEXT NOT NULL,
            last_attempt_at     TEXT,
            last_error          TEXT,
            resolved_at         TEXT,
            UNIQUE(kind, reference_id, target_fingerprint)
        );

        -- Matches list_posts_page's own precedent for "pending work"
        -- tables: a partial index over exactly the
        -- predicate load_due_work_items filters on, not a full-table scan
        -- of every resolved row too.
        CREATE INDEX idx_link_work_items_due
            ON link_work_items(next_attempt_at) WHERE status IN ('pending', 'retrying');
        """,
    ),
    Migration(
        description=(
            "Bounded Link diagnostic log (design doc §13.11, issue #60): "
            "distinct from moderation_log, which is a permanent audit trail "
            "-- this one is deliberately non-permanent, pruned by "
            "netbbs.link.diagnostics.LinkDiagnosticLogHandler on every "
            "write against operator-configured age/row bounds "
            "(LinkConfig.diagnostic_log_max_age_days/max_rows). Populated "
            "by attaching a logging.Handler to the netbbs.link logger "
            "namespace at WARNING level and above -- catches every "
            "existing _logger.warning/.error call already scattered across "
            "netbbs.link.sync/.transport/.seedlist via ordinary logger "
            "propagation, no per-call-site instrumentation needed. Every "
            "one of those existing calls is already about protocol/dial/ "
            "sync events (a URL, a fingerprint, an exception message), "
            "never a Link message's decrypted body or a board post's "
            "content -- 'metadata only' is a property of which call sites "
            "exist today, confirmed by audit, not a filter this table "
            "enforces itself."
        ),
        sql="""
        CREATE TABLE link_diagnostic_log (
            id           INTEGER PRIMARY KEY,
            level        TEXT NOT NULL,
            logger_name  TEXT NOT NULL,
            message      TEXT NOT NULL,
            created_at   TEXT NOT NULL
        );
        CREATE INDEX idx_link_diagnostic_log_created_at ON link_diagnostic_log(created_at);
        """,
    ),
    Migration(
        description=(
            "Issue #72: node-local arrival order for unread state, distinct "
            "from a post/file's own authored created_at. A remote post's "
            "claimed created_at can predate a user's existing read cursor "
            "even though the post only just arrived after a partition/catch- "
            "up, silently hiding it from unread counts and [N]ew scan -- "
            "authored chronology and 'became available on this node' "
            "chronology are different things. user_read_cursors gains "
            "last_seen_arrival_id (nullable INTEGER): the posts/files row's "
            "own INTEGER PRIMARY KEY rowid at the moment it became locally "
            "visible, whether created locally (netbbs.boards.posts."
            "create_post) or materialized from a carried Link event "
            "(netbbs.link.boards.materialize_carried_post inserts a fresh "
            "row the exact same way) -- no new column on posts/files "
            "themselves, since SQLite already assigns their rowid in strict "
            "insertion order for both origins (the same property GitHub "
            "issue #68 already relies on for edit-chain tie-breaking). "
            "Backfilled for every existing cursor row from the post/file it "
            "already names via last_seen_stable_id, so existing read state "
            "is preserved exactly, never reset to unread by this migration "
            "itself. Channels are unaffected: channel_messages.id already "
            "serves this role for that container (last_seen_stable_id "
            "already stores it directly for channel cursors), which is why "
            "channels never had this bug in the first place."
        ),
        sql="""
        ALTER TABLE user_read_cursors ADD COLUMN last_seen_arrival_id INTEGER;

        UPDATE user_read_cursors
        SET last_seen_arrival_id = (
            SELECT posts.id FROM posts WHERE posts.post_id = user_read_cursors.last_seen_stable_id
        )
        WHERE object_type = 'board';

        UPDATE user_read_cursors
        SET last_seen_arrival_id = (
            SELECT files.id FROM files WHERE files.file_id = user_read_cursors.last_seen_stable_id
        )
        WHERE object_type = 'file_area';

        UPDATE user_read_cursors
        SET last_seen_arrival_id = CAST(last_seen_stable_id AS INTEGER)
        WHERE object_type = 'channel';
        """,
    ),
    Migration(
        description=(
            "Issue #85: link_events gains a nullable board_id column, "
            "populated for the five board-scoped object types (board_genesis, "
            "board_post, board_post_edit, board_origin_transfer_offer, "
            "board_origin_transfer_accepted) that already carry "
            "payload.board_id directly -- everything else (key_transition, "
            "link_message and its acknowledgements) stays NULL, since neither "
            "belongs to a board. Backfilled via json_extract against the "
            "already-stored envelope rather than re-verifying anything. "
            "Serves the new inventory/pull-based catch-up diff query (design "
            "doc §8.8): 'everything this node has for board X' was not a "
            "query this table needed to answer efficiently before this issue "
            "-- previously only sender_fingerprint was indexed, since nothing "
            "asked 'by board' rather than 'by sender.' The covering index "
            "keeps that query cheap as link_events grows."
        ),
        sql="""
        ALTER TABLE link_events ADD COLUMN board_id TEXT;

        UPDATE link_events
        SET board_id = json_extract(envelope_json, '$.envelope.payload.board_id')
        WHERE object_type IN (
            'board_genesis', 'board_post', 'board_post_edit',
            'board_origin_transfer_offer', 'board_origin_transfer_accepted'
        );

        CREATE INDEX idx_link_events_board_id ON link_events(board_id, object_type);
        """,
    ),
]
