"""
Tests for the Link-messages schema itself:
`mail_messages` rebuilt with `recipient_user_id` nullable plus new
Link-only columns, and the new `link_mail_acknowledgements` table.
Deliberately at the raw-SQL level, matching `tests/test_storage.py`'s
own style for schema-level assertions -- no business-logic module
(`netbbs.link.mail`) exists yet to drive these through.
"""

from __future__ import annotations

import sqlite3

import pytest

from netbbs.auth.users import create_user
from netbbs.storage.database import Database


def _now() -> str:
    return "2026-01-01T00:00:00+00:00"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


# -- mutual-exclusivity CHECK constraint ------------------------------------


def test_ordinary_local_mail_still_inserts_fine(db):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    db.connection.execute(
        """
        INSERT INTO mail_messages (sender_user_id, sender_label, recipient_user_id, subject, body, created_at)
        VALUES (?, 'alice', ?, 'hello', 'world', ?)
        """,
        (alice.id, bob.id, _now()),
    )
    db.connection.commit()
    row = db.connection.execute("SELECT recipient_user_id, recipient_remote_address FROM mail_messages").fetchone()
    assert row["recipient_user_id"] == bob.id
    assert row["recipient_remote_address"] is None


def test_link_outbound_row_with_remote_address_inserts_fine(db):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    db.connection.execute(
        """
        INSERT INTO mail_messages
            (sender_user_id, sender_label, recipient_remote_address, subject, body,
             created_at, link_event_json, link_delivery_status)
        VALUES (?, 'alice', 'bob@somefingerprint', 'hello', 'world', ?, '{}', 'pending')
        """,
        (alice.id, _now()),
    )
    db.connection.commit()
    row = db.connection.execute("SELECT recipient_user_id, recipient_remote_address FROM mail_messages").fetchone()
    assert row["recipient_user_id"] is None
    assert row["recipient_remote_address"] == "bob@somefingerprint"


def test_rejects_a_row_with_neither_recipient_shape_set(db):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            """
            INSERT INTO mail_messages (sender_user_id, sender_label, subject, body, created_at)
            VALUES (?, 'alice', 'hello', 'world', ?)
            """,
            (alice.id, _now()),
        )


def test_rejects_a_row_with_both_recipient_shapes_set(db):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            """
            INSERT INTO mail_messages
                (sender_user_id, sender_label, recipient_user_id, recipient_remote_address, subject, body, created_at)
            VALUES (?, 'alice', ?, 'bob@somefingerprint', 'hello', 'world', ?)
            """,
            (alice.id, bob.id, _now()),
        )


# -- link_delivery_status enum -----------------------------------------------


def test_link_delivery_status_accepts_each_named_value(db):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    for status in ("pending", "delivered", "bounced", "expired"):
        db.connection.execute(
            """
            INSERT INTO mail_messages
                (sender_user_id, sender_label, recipient_remote_address, subject, body,
                 created_at, link_event_json, link_delivery_status)
            VALUES (?, 'alice', 'bob@somefingerprint', 'hello', 'world', ?, '{}', ?)
            """,
            (alice.id, _now(), status),
        )
    db.connection.commit()
    assert db.connection.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0] == 4


def test_link_delivery_status_rejects_an_unnamed_value(db):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            """
            INSERT INTO mail_messages
                (sender_user_id, sender_label, recipient_remote_address, subject, body,
                 created_at, link_event_json, link_delivery_status)
            VALUES (?, 'alice', 'bob@somefingerprint', 'hello', 'world', ?, '{}', 'not-a-real-status')
            """,
            (alice.id, _now()),
        )


# -- isolation from local-only queries ---------------------------------------


def test_link_outbound_row_never_appears_in_anyones_local_inbox(db):
    """A Link-outbound row's recipient_user_id is NULL -- confirms the
    existing recipient_user_id = ? query shape (netbbs.mail.list_inbox's
    own filter) naturally never matches it, no extra WHERE clause
    needed anywhere in netbbs.mail itself."""
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    db.connection.execute(
        """
        INSERT INTO mail_messages
            (sender_user_id, sender_label, recipient_remote_address, subject, body,
             created_at, link_event_json, link_delivery_status)
        VALUES (?, 'alice', 'bob@somefingerprint', 'hello', 'world', ?, '{}', 'pending')
        """,
        (alice.id, _now()),
    )
    db.connection.commit()
    for user_id in (alice.id, None):
        rows = db.connection.execute(
            "SELECT COUNT(*) FROM mail_messages WHERE recipient_user_id = ?", (user_id,)
        ).fetchone()[0]
        assert rows == 0


def test_link_source_event_id_is_distinct_from_a_deleted_local_sender(db):
    """sender_user_id IS NULL already means something on this table (the
    local sender's account was later deleted) -- link_source_event_id is
    the explicit, unambiguous marker for "this row arrived via Link",
    never inferred from sender_user_id alone."""
    bob = create_user(db, "bob", password="hunter2", user_level=10)

    # An ordinary local message whose sender's account has since been
    # deleted (sender_user_id NULL, but never touched Link at all).
    db.connection.execute(
        """
        INSERT INTO mail_messages (sender_user_id, sender_label, recipient_user_id, subject, body, created_at)
        VALUES (NULL, 'gone-user', ?, 'old message', 'body', ?)
        """,
        (bob.id, _now()),
    )
    # A message actually delivered via Link.
    db.connection.execute(
        """
        INSERT INTO mail_messages
            (sender_user_id, sender_label, recipient_user_id, subject, body, created_at, link_source_event_id)
        VALUES (NULL, 'alice@somefingerprint', ?, 'from afar', 'body', ?, 'the-link-message-content-id')
        """,
        (bob.id, _now()),
    )
    db.connection.commit()

    local = db.connection.execute(
        "SELECT link_source_event_id FROM mail_messages WHERE subject = 'old message'"
    ).fetchone()
    assert local["link_source_event_id"] is None

    via_link = db.connection.execute(
        "SELECT link_source_event_id FROM mail_messages WHERE subject = 'from afar'"
    ).fetchone()
    assert via_link["link_source_event_id"] == "the-link-message-content-id"


# -- link_mail_acknowledgements -----------------------------------------------


def test_link_mail_acknowledgements_insert_and_query(db):
    db.connection.execute(
        """
        INSERT INTO link_mail_acknowledgements
            (message_content_id, target_node_fingerprint, ack_event_json, created_at)
        VALUES ('the-message-content-id', 'sender-node-fp', '{}', ?)
        """,
        (_now(),),
    )
    db.connection.commit()
    row = db.connection.execute(
        "SELECT message_content_id, target_node_fingerprint, sent_at FROM link_mail_acknowledgements"
    ).fetchone()
    assert row["message_content_id"] == "the-message-content-id"
    assert row["target_node_fingerprint"] == "sender-node-fp"
    assert row["sent_at"] is None


def test_link_mail_acknowledgements_pending_index_excludes_sent(db):
    db.connection.execute(
        """
        INSERT INTO link_mail_acknowledgements
            (message_content_id, target_node_fingerprint, ack_event_json, created_at, sent_at)
        VALUES ('acked-already', 'sender-node-fp', '{}', ?, ?)
        """,
        (_now(), _now()),
    )
    db.connection.execute(
        """
        INSERT INTO link_mail_acknowledgements
            (message_content_id, target_node_fingerprint, ack_event_json, created_at)
        VALUES ('still-pending', 'sender-node-fp', '{}', ?)
        """,
        (_now(),),
    )
    db.connection.commit()
    pending = db.connection.execute(
        "SELECT message_content_id FROM link_mail_acknowledgements WHERE sent_at IS NULL"
    ).fetchall()
    assert [r["message_content_id"] for r in pending] == ["still-pending"]
