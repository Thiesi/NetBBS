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
    Migration(
        description=(
            "Moderator grants (design doc §13/round 34): the "
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
            -- (design doc round 34).
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
            -- deliberately generic so later tracks' actions reuse it
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
            "expiry state machine (design doc §13/§15, round 35). Boards "
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
            "File-area mirror of round 35's board/post moderation "
            "lifecycle (design doc §13/§15, round 36): moderated-area "
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
            "doc §13, round 37), gated by "
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
            "Generic per-user preference store (design doc §13, round "
            "38) -- a per-user mirror of node_config, backing the "
            "vCard bio/visibility fields now and any future per-user "
            "setting (e.g. a per-user chat timestamp preference, "
            "round 32) without needing its own storage mechanism."
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
            "admit 'action' (/me, design doc round 32/40) -- same "
            "standard SQLite table-rebuild as round 37's widening, "
            "since there's still no ALTER TABLE for changing a CHECK "
            "in place. Deliberately widened only for what this round "
            "needs, not speculatively for /nick's not-yet-designed "
            "event kind too -- see round 40's sign-off note."
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
            "admit 'nick' (/nick, design doc round 32/41) -- same "
            "standard SQLite table-rebuild pattern as rounds 37/40."
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
            "Adds channels.topic (design doc §8/round 33 point 5, Phase "
            "2 Track 5d) -- a moderator-editable /topic, distinct from "
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
            "Adds invite-only/hidden channel support (design doc §8/"
            "round 33 points 8/9/11, Phase 2 Track 5h): channels.hidden "
            "and channels.members_only are independent axes (a "
            "members_only-but-not-hidden channel still appears in "
            "listings -- 'hidden + open is obscurity, not access "
            "control', round 33 point 9), channels.allow_member_invites "
            "is the opt-in described in round 33 point 11. All three "
            "default to 0 (off) -- an existing channel's behavior is "
            "unchanged unless a moderator explicitly opts in. "
            "channel_members is persistent membership -- deliberately "
            "its own table rather than folded into moderator_grants: "
            "membership is access/visibility eligibility, not a "
            "permission bit. channel_invitations tracks the invite-then-"
            "accept flow separately from that direct-grant path; expiry "
            "follows channel_restrictions' precedent (filter at check "
            "time via expires_at, no sweep-on-access needed) rather "
            "than posts/files' round 35 sweep-and-delete pattern -- an "
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
]
