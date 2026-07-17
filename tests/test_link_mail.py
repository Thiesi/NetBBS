"""
Tests for `netbbs.link.mail` -- the local-origination and receiving-side
bridge for Link messages (design doc round 93). Tier 1
(`tier1_home_node_key`) only, per that round's tier-2 finding.
"""

from __future__ import annotations

import base64
import json

import pytest

from netbbs.auth.users import create_user
from netbbs.identity.encryption import decrypt_with, encrypt_for
from netbbs.link.events import (
    LinkMessage,
    build_endpoint_descriptor,
    build_link_message,
    build_link_message_accepted,
    build_link_message_bounced,
)
from netbbs.link.mail import (
    LinkMailError,
    apply_link_message_accepted,
    apply_link_message_bounced,
    compose_link_message,
    deliver_link_message,
    load_pending_link_mail,
    load_pending_link_mail_acknowledgements,
    mark_link_mail_acknowledgement_sent,
)
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import PeerRecord
from netbbs.link.store import save_peer
from netbbs.mail import MailError
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


@pytest.fixture
def node_identity():
    return bootstrap_node_identity("roanoke")


@pytest.fixture
def remote_node_identity():
    return bootstrap_node_identity("farpoint")


def _seed_peer(db, identity, *, created_at="2026-01-01T00:00:00+00:00"):
    descriptor = build_endpoint_descriptor(
        signing_identity=identity.signing_key,
        subject_fingerprint=identity.fingerprint,
        addresses=None,
        outgoing_only=True,
        created_at=created_at,
    )
    peer = PeerRecord(
        fingerprint=identity.fingerprint,
        root_public_key=bytes(identity.root.verify_key),
        transitions=identity.transitions,
        descriptor=descriptor,
    )
    save_peer(db, peer)
    return peer


# -- compose_link_message -----------------------------------------------------


def test_compose_link_message_addresses_the_recipient_correctly(db, alice, node_identity, remote_node_identity):
    _seed_peer(db, remote_node_identity)

    message = compose_link_message(
        db, alice, f"bob@{remote_node_identity.fingerprint}", "hello", "world",
        node_identity=node_identity,
    )

    assert message.payload["sender"] == {
        "kind": "node_vouched_user",
        "home_node_fingerprint": node_identity.fingerprint,
        "local_user_id": "alice",
    }
    assert message.payload["recipient"] == {
        "home_node_fingerprint": remote_node_identity.fingerprint,
        "local_user_id": "bob",
    }
    assert message.payload["confidentiality_tier"] == "tier1_home_node_key"


def test_compose_link_message_ciphertext_is_not_the_plaintext_but_decrypts_correctly(
    db, alice, node_identity, remote_node_identity
):
    _seed_peer(db, remote_node_identity)

    message = compose_link_message(
        db, alice, f"bob@{remote_node_identity.fingerprint}", "a subject", "a secret body",
        node_identity=node_identity,
    )

    ciphertext = base64.b64decode(message.payload["ciphertext"])
    assert b"a secret body" not in ciphertext

    # tier 1: the recipient's *home node* can decrypt with its own key.
    plaintext = decrypt_with(remote_node_identity.signing_key, ciphertext)
    decoded = json.loads(plaintext)
    assert decoded == {"subject": "a subject", "body": "a secret body"}


def test_compose_link_message_persists_a_pending_outbound_row_with_plaintext_for_the_senders_own_view(
    db, alice, node_identity, remote_node_identity
):
    _seed_peer(db, remote_node_identity)

    message = compose_link_message(
        db, alice, f"bob@{remote_node_identity.fingerprint}", "hello", "world",
        node_identity=node_identity,
    )

    row = db.connection.execute("SELECT * FROM mail_messages").fetchone()
    assert row["sender_user_id"] == alice.id
    assert row["recipient_user_id"] is None
    assert row["recipient_remote_address"] == f"bob@{remote_node_identity.fingerprint}"
    assert row["subject"] == "hello"
    assert row["body"] == "world"
    assert row["link_delivery_status"] == "pending"
    assert row["link_event_content_id"] == message.content_id
    assert LinkMessage.from_dict(json.loads(row["link_event_json"])).content_id == message.content_id


def test_compose_link_message_rejects_an_unknown_peer_node(db, alice, node_identity, remote_node_identity):
    # no _seed_peer -- this node has never said hello to remote_node_identity
    with pytest.raises(LinkMailError):
        compose_link_message(
            db, alice, f"bob@{remote_node_identity.fingerprint}", "hello", "world",
            node_identity=node_identity,
        )


def test_compose_link_message_rejects_a_malformed_address(db, alice, node_identity):
    with pytest.raises(LinkMailError):
        compose_link_message(db, alice, "not-a-valid-address", "hello", "world", node_identity=node_identity)


def test_compose_link_message_rejects_blank_subject(db, alice, node_identity, remote_node_identity):
    _seed_peer(db, remote_node_identity)
    with pytest.raises(MailError):
        compose_link_message(
            db, alice, f"bob@{remote_node_identity.fingerprint}", "   ", "world",
            node_identity=node_identity,
        )


# -- deliver_link_message ------------------------------------------------------


def _incoming_message(node_identity, remote_node_identity, *, recipient="bob", subject="hello", body="world"):
    plaintext = json.dumps({"subject": subject, "body": body}).encode("utf-8")
    ciphertext = encrypt_for(node_identity.signing_key.verify_key, plaintext)
    return build_link_message(
        signing_identity=remote_node_identity.signing_key,
        home_node_fingerprint=remote_node_identity.fingerprint,
        local_user_id="alice",
        recipient_home_node_fingerprint=node_identity.fingerprint,
        recipient_local_user_id=recipient,
        confidentiality_tier="tier1_home_node_key",
        ciphertext=ciphertext,
        created_at="2026-01-01T00:00:00Z",
    )


def test_deliver_link_message_lands_in_the_local_recipients_mailbox(db, bob, node_identity, remote_node_identity):
    message = _incoming_message(node_identity, remote_node_identity)

    result = deliver_link_message(db, message.to_dict(), node_identity=node_identity)

    row = db.connection.execute("SELECT * FROM mail_messages").fetchone()
    assert row["recipient_user_id"] == bob.id
    assert row["sender_user_id"] is None
    assert row["sender_label"] == f"alice@{remote_node_identity.fingerprint}"
    assert row["subject"] == "hello"
    assert row["body"] == "world"
    assert row["link_source_event_id"] == message.content_id
    assert result.payload["message_content_id"] == message.content_id


def test_deliver_link_message_queues_an_accepted_acknowledgement_for_the_origin_node(
    db, bob, node_identity, remote_node_identity
):
    message = _incoming_message(node_identity, remote_node_identity)

    deliver_link_message(db, message.to_dict(), node_identity=node_identity)

    ack_row = db.connection.execute("SELECT * FROM link_mail_acknowledgements").fetchone()
    assert ack_row["message_content_id"] == message.content_id
    assert ack_row["target_node_fingerprint"] == remote_node_identity.fingerprint
    assert ack_row["sent_at"] is None


def test_deliver_link_message_bounces_an_unknown_recipient(db, node_identity, remote_node_identity):
    # no bob created locally
    message = _incoming_message(node_identity, remote_node_identity, recipient="nobody")

    deliver_link_message(db, message.to_dict(), node_identity=node_identity)

    assert db.connection.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0] == 0
    ack_row = db.connection.execute("SELECT ack_event_json FROM link_mail_acknowledgements").fetchone()
    envelope = json.loads(ack_row["ack_event_json"])["envelope"]
    assert envelope["object_type"] == "link_message_bounced"
    assert envelope["payload"]["reason"] == "unknown_recipient"


def test_deliver_link_message_bounces_when_mailbox_is_full_and_all_unread(
    db, bob, node_identity, remote_node_identity, monkeypatch
):
    import netbbs.mail as mail_module

    monkeypatch.setattr(mail_module, "MAX_MAIL_PER_RECIPIENT", 1)
    db.connection.execute(
        """
        INSERT INTO mail_messages (sender_user_id, sender_label, recipient_user_id, subject, body, created_at)
        VALUES (NULL, 'someone', ?, 'already here', 'body', '2026-01-01T00:00:00Z')
        """,
        (bob.id,),
    )
    db.connection.commit()

    message = _incoming_message(node_identity, remote_node_identity)
    deliver_link_message(db, message.to_dict(), node_identity=node_identity)

    subjects = [r["subject"] for r in db.connection.execute("SELECT subject FROM mail_messages")]
    assert subjects == ["already here"]  # the new one was never stored
    ack_row = db.connection.execute("SELECT ack_event_json FROM link_mail_acknowledgements").fetchone()
    envelope = json.loads(ack_row["ack_event_json"])["envelope"]
    assert envelope["payload"]["reason"] == "mailbox_full"


def test_deliver_link_message_evicts_the_oldest_read_message_when_full_but_some_are_read(
    db, bob, node_identity, remote_node_identity, monkeypatch
):
    import netbbs.mail as mail_module

    monkeypatch.setattr(mail_module, "MAX_MAIL_PER_RECIPIENT", 1)
    db.connection.execute(
        """
        INSERT INTO mail_messages
            (sender_user_id, sender_label, recipient_user_id, subject, body, created_at, read_at)
        VALUES (NULL, 'someone', ?, 'already here', 'body', '2026-01-01T00:00:00Z', '2026-01-01T00:01:00Z')
        """,
        (bob.id,),
    )
    db.connection.commit()

    message = _incoming_message(node_identity, remote_node_identity)
    deliver_link_message(db, message.to_dict(), node_identity=node_identity)

    # recipient_deleted_at IS NULL, matching netbbs.mail.list_inbox's own
    # filter -- the evicted row is a soft-delete (recipient_deleted_at
    # set), not a hard-deleted row, since its sender_deleted_at is still
    # NULL (netbbs.mail._hard_delete_or_mark's own precedent).
    subjects = [
        r["subject"]
        for r in db.connection.execute(
            "SELECT subject FROM mail_messages WHERE recipient_deleted_at IS NULL"
        )
    ]
    assert subjects == ["hello"]  # the old read one was evicted to make room


# -- apply_link_message_accepted / apply_link_message_bounced ------------------


def test_apply_link_message_accepted_marks_the_outbound_row_delivered(
    db, alice, node_identity, remote_node_identity
):
    _seed_peer(db, remote_node_identity)
    message = compose_link_message(
        db, alice, f"bob@{remote_node_identity.fingerprint}", "hello", "world",
        node_identity=node_identity,
    )

    ack = build_link_message_accepted(
        signing_identity=remote_node_identity.signing_key,
        recipient_node_fingerprint=remote_node_identity.fingerprint,
        message_content_id=message.content_id,
        created_at="2026-01-01T00:05:00Z",
    )
    apply_link_message_accepted(db, ack.to_dict())

    status = db.connection.execute("SELECT link_delivery_status FROM mail_messages").fetchone()[0]
    assert status == "delivered"


def test_apply_link_message_bounced_marks_the_outbound_row_bounced(db, alice, node_identity, remote_node_identity):
    _seed_peer(db, remote_node_identity)
    message = compose_link_message(
        db, alice, f"bob@{remote_node_identity.fingerprint}", "hello", "world",
        node_identity=node_identity,
    )

    bounced = build_link_message_bounced(
        signing_identity=remote_node_identity.signing_key,
        recipient_node_fingerprint=remote_node_identity.fingerprint,
        message_content_id=message.content_id,
        reason="unknown_recipient",
        created_at="2026-01-01T00:05:00Z",
    )
    apply_link_message_bounced(db, bounced.to_dict())

    status = db.connection.execute("SELECT link_delivery_status FROM mail_messages").fetchone()[0]
    assert status == "bounced"


# -- load_pending_link_mail / load_pending_link_mail_acknowledgements ----------


def test_load_pending_link_mail_excludes_resolved_rows(db, alice, node_identity, remote_node_identity):
    _seed_peer(db, remote_node_identity)
    still_pending = compose_link_message(
        db, alice, f"bob@{remote_node_identity.fingerprint}", "pending one", "world",
        node_identity=node_identity,
    )
    delivered = compose_link_message(
        db, alice, f"bob@{remote_node_identity.fingerprint}", "delivered one", "world",
        node_identity=node_identity,
    )
    ack = build_link_message_accepted(
        signing_identity=remote_node_identity.signing_key,
        recipient_node_fingerprint=remote_node_identity.fingerprint,
        message_content_id=delivered.content_id,
        created_at="2026-01-01T00:05:00Z",
    )
    apply_link_message_accepted(db, ack.to_dict())

    pending = load_pending_link_mail(db)

    assert [m.content_id for _fp, m in pending] == [still_pending.content_id]
    assert pending[0][0] == remote_node_identity.fingerprint


def test_load_pending_link_mail_acknowledgements_excludes_sent(db, bob, node_identity, remote_node_identity):
    message = _incoming_message(node_identity, remote_node_identity)
    deliver_link_message(db, message.to_dict(), node_identity=node_identity)

    [pending] = load_pending_link_mail_acknowledgements(db)
    fingerprint, ack = pending
    assert fingerprint == remote_node_identity.fingerprint
    assert ack.payload["message_content_id"] == message.content_id

    mark_link_mail_acknowledgement_sent(db, ack)

    assert load_pending_link_mail_acknowledgements(db) == []
