"""
End-to-end tests for the ON DELETE SET NULL/CASCADE behavior added
across every table that references users(id) (design doc -- SysOp
foundation round). Seeds a real row in each affected table directly
via SQL (matching tests/test_storage.py's own style for schema-level
assertions, rather than wiring up five unrelated subsystems' business
APIs just to get one row into each table), then calls
netbbs.auth.users.delete_user and checks the actual on-disk result --
not just that the schema declares the right ON DELETE clause, but that
SQLite actually enforces it.
"""

from __future__ import annotations

from netbbs.auth.users import create_user, delete_user
from netbbs.storage.database import Database


def _now() -> str:
    return "2026-01-01T00:00:00+00:00"


def test_delete_user_cascades_and_nulls_correctly_across_every_table(tmp_path):
    db = Database(tmp_path / "node.db")
    conn = db.connection

    sysop = create_user(db, "sysop", password="hunter2", user_level=255)
    alice = create_user(db, "alice", password="hunter2", user_level=10)

    conn.execute(
        "INSERT INTO boards (board_id, name, created_at) VALUES ('b1', 'general', ?)", (_now(),)
    )
    board_id = conn.execute("SELECT id FROM boards").fetchone()[0]
    conn.execute(
        """
        INSERT INTO posts (post_id, board_id, author_user_id, author_label, subject, body, created_at)
        VALUES ('p1', ?, ?, 'alice', 'subject', 'body', ?)
        """,
        (board_id, alice.id, _now()),
    )

    conn.execute(
        "INSERT INTO file_areas (area_id, name, created_at) VALUES ('a1', 'files', ?)", (_now(),)
    )
    area_id = conn.execute("SELECT id FROM file_areas").fetchone()[0]
    conn.execute(
        """
        INSERT INTO files
            (file_id, area_id, filename, size_bytes, sha256, storage_path,
             uploader_user_id, uploader_label, created_at)
        VALUES ('f1', ?, 'readme.txt', 3, 'deadbeef', '/tmp/x', ?, 'alice', ?)
        """,
        (area_id, alice.id, _now()),
    )

    conn.execute(
        """
        INSERT INTO moderator_grants (user_id, object_type, object_id, permissions, granted_by_user_id, created_at)
        VALUES (?, 'board', ?, 1, ?, ?)
        """,
        (alice.id, board_id, sysop.id, _now()),
    )

    conn.execute(
        "INSERT INTO channels (channel_id, name, created_at) VALUES ('c1', 'lobby', ?)", (_now(),)
    )
    channel_id = conn.execute("SELECT id FROM channels").fetchone()[0]
    conn.execute(
        """
        INSERT INTO channel_restrictions (channel_id, user_id, kind, imposed_by_user_id, created_at)
        VALUES (?, ?, 'mute', ?, ?)
        """,
        (channel_id, alice.id, sysop.id, _now()),
    )
    conn.execute(
        "INSERT INTO channel_members (channel_id, user_id, granted_by_user_id, created_at) VALUES (?, ?, ?, ?)",
        (channel_id, alice.id, sysop.id, _now()),
    )
    conn.execute(
        """
        INSERT INTO channel_invitations (channel_id, invited_user_id, invited_by_user_id, status, created_at)
        VALUES (?, ?, ?, 'pending', ?)
        """,
        (channel_id, alice.id, sysop.id, _now()),
    )

    conn.execute("INSERT INTO user_preferences (user_id, key, value) VALUES (?, 'k', 'v')", (alice.id,))

    conn.execute(
        "INSERT INTO blocklist (local_user_id, blocked_by_user_id, created_at) VALUES (?, ?, ?)",
        (alice.id, sysop.id, _now()),
    )
    # A second blocklist entry where alice is the *blocker*, not the target.
    conn.execute(
        "INSERT INTO blocklist (fingerprint, blocked_by_user_id, created_at) VALUES ('somefp', ?, ?)",
        (alice.id, _now()),
    )

    # alice as both actor and target -- tests that ON DELETE SET NULL
    # fires independently on both columns of the same row.
    conn.execute(
        "INSERT INTO moderation_log (actor_user_id, action, target_user_id, detail, created_at) VALUES (?, 'test', ?, 'd', ?)",
        (alice.id, alice.id, _now()),
    )
    conn.commit()

    delete_user(db, alice, deleted_by=sysop)

    # posts/files: content authorship survives via the denormalized
    # label, only the live FK goes NULL.
    post = conn.execute("SELECT author_user_id, author_label FROM posts WHERE post_id = 'p1'").fetchone()
    assert post["author_user_id"] is None
    assert post["author_label"] == "alice"

    file_row = conn.execute("SELECT uploader_user_id, uploader_label FROM files WHERE file_id = 'f1'").fetchone()
    assert file_row["uploader_user_id"] is None
    assert file_row["uploader_label"] == "alice"

    # Administrative rows tied to the account are cascade-removed.
    assert conn.execute("SELECT COUNT(*) FROM moderator_grants WHERE user_id = ?", (alice.id,)).fetchone()[0] == 0
    assert (
        conn.execute("SELECT COUNT(*) FROM channel_restrictions WHERE user_id = ?", (alice.id,)).fetchone()[0]
        == 0
    )
    assert conn.execute("SELECT COUNT(*) FROM channel_members WHERE user_id = ?", (alice.id,)).fetchone()[0] == 0
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM channel_invitations WHERE invited_user_id = ?", (alice.id,)
        ).fetchone()[0]
        == 0
    )
    assert conn.execute("SELECT COUNT(*) FROM user_preferences WHERE user_id = ?", (alice.id,)).fetchone()[0] == 0

    # blocklist: both a row *about* alice (local_user_id) and a row
    # alice herself created (blocked_by_user_id) are removed entirely
    # -- local_user_id is CASCADE, not SET NULL, since this table's own
    # CHECK requires exactly one of fingerprint/local_user_id to be
    # set, and a locally-blocked row has no fingerprint to fall back on.
    assert conn.execute("SELECT COUNT(*) FROM blocklist").fetchone()[0] == 0

    # moderation_log: the audit trail survives, both actor and target go NULL.
    log_row = conn.execute("SELECT actor_user_id, target_user_id, detail FROM moderation_log WHERE action = 'test'").fetchone()
    assert log_row["actor_user_id"] is None
    assert log_row["target_user_id"] is None
    assert log_row["detail"] == "d"

    # The account itself is gone.
    assert conn.execute("SELECT COUNT(*) FROM users WHERE id = ?", (alice.id,)).fetchone()[0] == 0

    db.close()


def test_posts_self_reference_survives_a_rebuild_migration(tmp_path):
    """posts.parent_post_id is a self-reference -- the one FK pointing
    *into* any of the nine rebuilt tables. Regression check that the
    rebuild migration didn't silently break reply-chain data (see the
    migration's own comment on why this is safe)."""
    db = Database(tmp_path / "node.db")
    conn = db.connection

    author = create_user(db, "alice", password="hunter2", user_level=10)
    conn.execute("INSERT INTO boards (board_id, name, created_at) VALUES ('b1', 'general', ?)", (_now(),))
    board_id = conn.execute("SELECT id FROM boards").fetchone()[0]
    conn.execute(
        """
        INSERT INTO posts (post_id, board_id, author_user_id, author_label, subject, body, created_at)
        VALUES ('parent', ?, ?, 'alice', 'subject', 'body', ?)
        """,
        (board_id, author.id, _now()),
    )
    conn.execute(
        """
        INSERT INTO posts (post_id, board_id, parent_post_id, author_user_id, author_label, subject, body, created_at)
        VALUES ('child', ?, 'parent', ?, 'alice', 'reply', 'body', ?)
        """,
        (board_id, author.id, _now()),
    )
    conn.commit()

    reply = conn.execute("SELECT parent_post_id FROM posts WHERE post_id = 'child'").fetchone()
    assert reply["parent_post_id"] == "parent"

    db.close()
