"""
Tests for `netbbs.link.mail` -- the local-origination and receiving-side
bridge for Link messages. Tier 1 (`tier1_home_node_key`) only; tier-2
Link messages are not yet implemented.
"""

from __future__ import annotations

import base64
import datetime
import json

import pytest

from netbbs.auth.users import create_user, get_user_by_id
from netbbs.identity.encryption import decrypt_with, encrypt_for
from netbbs.link.events import (
    LinkMessage,
    build_endpoint_descriptor,
    build_link_message,
    build_link_message_accepted,
    build_link_message_bounced,
)
from netbbs.link.mail import (
    MAX_DELIVERY_NOTICES_SHOWN,
    LinkMailError,
    acknowledge_delivery_notices,
    apply_link_message_accepted,
    apply_link_message_bounced,
    bounce_reason_text,
    compose_link_message,
    deliver_link_message,
    delivery_display_status,
    delivery_explanation,
    expire_link_message_delivery,
    expire_unanswered_relay_mail,
    get_link_mail_acknowledgement,
    get_link_message_for_delivery,
    pending_delivery_notices,
    record_link_message_refused,
    record_relay_handoff,
    unexpire_link_message_delivery,
)
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import PeerRecord
from netbbs.link.store import save_peer
from netbbs.link.work_items import KIND_LINK_MAIL_ACK, KIND_LINK_MAIL_DELIVERY, load_due_work_items, record_success
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


def test_compose_link_message_keeps_capitals_in_both_addresses(db, node_identity, remote_node_identity):
    """Issue #807: a name is addressed as it is displayed. The recipient's
    capitals are kept, and the sender's name goes out spelled as shown, in
    the grammar a caller can type back."""
    _seed_peer(db, remote_node_identity)
    old_nib = create_user(db, "OldNib", password="hunter2pw")

    message = compose_link_message(
        db, old_nib, f"BobCase@{remote_node_identity.fingerprint}", "hello", "world",
        node_identity=node_identity,
    )

    assert message.payload["sender"]["local_user_id"] == "OldNib"
    assert message.payload["recipient"]["local_user_id"] == "BobCase"
    row = db.connection.execute("SELECT recipient_remote_address FROM mail_messages").fetchone()
    assert row["recipient_remote_address"] == f"BobCase@{remote_node_identity.fingerprint}"


def test_compose_link_message_refuses_a_sender_name_nobody_could_type_back(
    db, node_identity, remote_node_identity,
):
    """An account older than the username rules may hold a name the
    address grammar refuses; mail from it could never be answered."""
    _seed_peer(db, remote_node_identity)
    legacy = create_user(db, "legacy", password="hunter2pw")
    db.connection.execute("UPDATE users SET username = 'old name' WHERE id = ?", (legacy.id,))
    db.connection.commit()
    legacy = get_user_by_id(db, legacy.id)

    with pytest.raises(LinkMailError, match="Ask the SysOp to rename the account"):
        compose_link_message(
            db, legacy, f"bob@{remote_node_identity.fingerprint}", "hello", "world",
            node_identity=node_identity,
        )
    assert db.connection.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0] == 0


def test_compose_link_message_to_an_unlinked_node_says_what_to_do(db, alice, node_identity, remote_node_identity):
    with pytest.raises(LinkMailError) as excinfo:
        compose_link_message(
            db, alice, f"bob@{remote_node_identity.fingerprint}", "hello", "world",
            node_identity=node_identity,
        )
    assert "hello" not in str(excinfo.value)
    assert "Address a node it is linked with" in str(excinfo.value)


# -- deliver_link_message ------------------------------------------------------


def _incoming_message(
    node_identity, remote_node_identity, *, recipient="bob", subject="hello", body="world",
    sender="alice", created_at="2026-01-01T00:00:00Z", plaintext=None, ciphertext=None,
):
    if plaintext is None:
        plaintext = json.dumps({"subject": subject, "body": body}).encode("utf-8")
    if ciphertext is None:
        ciphertext = encrypt_for(node_identity.signing_key.verify_key, plaintext)
    return build_link_message(
        signing_identity=remote_node_identity.signing_key,
        home_node_fingerprint=remote_node_identity.fingerprint,
        local_user_id=sender,
        recipient_home_node_fingerprint=node_identity.fingerprint,
        recipient_local_user_id=recipient,
        confidentiality_tier="tier1_home_node_key",
        ciphertext=ciphertext,
        created_at=created_at,
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


def test_deliver_link_message_finds_the_recipient_whatever_the_capitals(db, node_identity, remote_node_identity):
    """Issue #807: `OldNib@Q` and `oldnib@Q` reach the same account."""
    old_nib = create_user(db, "OldNib", password="hunter2pw")
    for spelling in ("OldNib", "oldnib", "OLDNIB"):
        message = _incoming_message(node_identity, remote_node_identity, recipient=spelling, subject=spelling)
        deliver_link_message(db, message.to_dict(), node_identity=node_identity)

    rows = db.connection.execute(
        "SELECT recipient_user_id FROM mail_messages ORDER BY id"
    ).fetchall()
    assert [row["recipient_user_id"] for row in rows] == [old_nib.id] * 3


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


def test_deliver_link_message_evicts_a_read_system_notice_before_a_read_letter(
    db, bob, node_identity, remote_node_identity, monkeypatch
):
    """Link delivery shares the local quota rule (issue #819): a notice the
    BBS sent goes before a letter a person wrote."""
    import netbbs.mail as mail_module

    monkeypatch.setattr(mail_module, "MAX_MAIL_PER_RECIPIENT", 2)
    db.connection.execute(
        """
        INSERT INTO mail_messages
            (sender_user_id, sender_label, recipient_user_id, subject, body, created_at, read_at)
        VALUES (NULL, 'someone', ?, 'a letter', 'body', '2026-01-01T00:00:00Z', '2026-01-01T00:01:00Z')
        """,
        (bob.id,),
    )
    notice = mail_module.send_system_mail(db, bob, "a notice", "body")
    mail_module.mark_read(db, bob, notice)

    deliver_link_message(db, _incoming_message(node_identity, remote_node_identity).to_dict(), node_identity=node_identity)

    subjects = [m.subject for m in mail_module.list_inbox(db, bob)]
    assert subjects == ["hello", "a letter"]


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


# -- work-item integration (design doc §13.7, issue #60's second slice) -----


def test_compose_link_message_enqueues_a_due_delivery_work_item(db, alice, node_identity, remote_node_identity):
    _seed_peer(db, remote_node_identity)
    message = compose_link_message(
        db, alice, f"bob@{remote_node_identity.fingerprint}", "pending one", "world",
        node_identity=node_identity,
    )

    [work_item] = load_due_work_items(db, kind=KIND_LINK_MAIL_DELIVERY)
    assert work_item.reference_id == message.content_id
    assert work_item.target_fingerprint == remote_node_identity.fingerprint
    assert work_item.status == "pending"


def test_a_pushed_and_delivered_messages_work_item_is_no_longer_due(db, alice, node_identity, remote_node_identity):
    _seed_peer(db, remote_node_identity)
    delivered = compose_link_message(
        db, alice, f"bob@{remote_node_identity.fingerprint}", "delivered one", "world",
        node_identity=node_identity,
    )
    [work_item] = load_due_work_items(db, kind=KIND_LINK_MAIL_DELIVERY)
    record_success(db, work_item)

    ack = build_link_message_accepted(
        signing_identity=remote_node_identity.signing_key,
        recipient_node_fingerprint=remote_node_identity.fingerprint,
        message_content_id=delivered.content_id,
        created_at="2026-01-01T00:05:00Z",
    )
    apply_link_message_accepted(db, ack.to_dict())

    assert load_due_work_items(db, kind=KIND_LINK_MAIL_DELIVERY) == []


def test_deliver_link_message_enqueues_a_due_ack_work_item(db, bob, node_identity, remote_node_identity):
    message = _incoming_message(node_identity, remote_node_identity)
    deliver_link_message(db, message.to_dict(), node_identity=node_identity)

    [work_item] = load_due_work_items(db, kind=KIND_LINK_MAIL_ACK)
    assert work_item.target_fingerprint == remote_node_identity.fingerprint
    ack = get_link_mail_acknowledgement(db, work_item.reference_id)
    assert ack.payload["message_content_id"] == message.content_id

    record_success(db, work_item)

    assert load_due_work_items(db, kind=KIND_LINK_MAIL_ACK) == []


def test_get_link_message_for_delivery_returns_message_and_status(db, alice, node_identity, remote_node_identity):
    _seed_peer(db, remote_node_identity)
    message = compose_link_message(
        db, alice, f"bob@{remote_node_identity.fingerprint}", "subject", "world",
        node_identity=node_identity,
    )

    found_message, status = get_link_message_for_delivery(db, message.content_id)
    assert found_message.content_id == message.content_id
    assert status == "pending"


def test_get_link_message_for_delivery_returns_none_for_an_unknown_content_id(db):
    assert get_link_message_for_delivery(db, "nonexistent") is None


def test_expire_and_unexpire_link_message_delivery(db, alice, node_identity, remote_node_identity):
    _seed_peer(db, remote_node_identity)
    message = compose_link_message(
        db, alice, f"bob@{remote_node_identity.fingerprint}", "subject", "world",
        node_identity=node_identity,
    )

    expire_link_message_delivery(db, message.content_id)
    _, status = get_link_message_for_delivery(db, message.content_id)
    assert status == "expired"

    unexpire_link_message_delivery(db, message.content_id)
    _, status = get_link_message_for_delivery(db, message.content_id)
    assert status == "pending"


def test_expire_link_message_delivery_never_overwrites_a_genuine_resolution(
    db, alice, node_identity, remote_node_identity
):
    """A real accepted/bounced event racing in first must win -- a
    stale dead-letter outcome (e.g. a slow retry loop iteration) must
    never clobber it."""
    _seed_peer(db, remote_node_identity)
    message = compose_link_message(
        db, alice, f"bob@{remote_node_identity.fingerprint}", "subject", "world",
        node_identity=node_identity,
    )
    ack = build_link_message_accepted(
        signing_identity=remote_node_identity.signing_key,
        recipient_node_fingerprint=remote_node_identity.fingerprint,
        message_content_id=message.content_id,
        created_at="2026-01-01T00:05:00Z",
    )
    apply_link_message_accepted(db, ack.to_dict())

    expire_link_message_delivery(db, message.content_id)

    _, status = get_link_message_for_delivery(db, message.content_id)
    assert status == "delivered"


# -- delivery state for the sender (issue #806) --------------------------------


def _delivery_row(db):
    return db.connection.execute(
        "SELECT link_delivery_status, link_delivery_reason, link_delivery_notice_pending FROM mail_messages"
    ).fetchone()


def _bounce(remote_node_identity, message, reason="unknown_recipient"):
    return build_link_message_bounced(
        signing_identity=remote_node_identity.signing_key,
        recipient_node_fingerprint=remote_node_identity.fingerprint,
        message_content_id=message.content_id,
        reason=reason,
        created_at="2026-01-01T00:05:00Z",
    ).to_dict()


def _sent(db, alice, remote_node_identity, node_identity, subject="subject"):
    _seed_peer(db, remote_node_identity)
    return compose_link_message(
        db, alice, f"bob@{remote_node_identity.fingerprint}", subject, "world", node_identity=node_identity,
    )


def test_a_signed_bounce_keeps_its_reason_and_flags_the_sender(db, alice, node_identity, remote_node_identity):
    message = _sent(db, alice, remote_node_identity, node_identity)
    assert tuple(_delivery_row(db)) == ("pending", None, 0)

    apply_link_message_bounced(db, _bounce(remote_node_identity, message, "mailbox_full"))

    assert tuple(_delivery_row(db)) == ("bounced", "mailbox_full", 1)


def test_a_policy_refusal_keeps_its_reason_code(db, alice, node_identity, remote_node_identity):
    message = _sent(db, alice, remote_node_identity, node_identity)

    record_link_message_refused(db, message.content_id, "link_policy_node_quarantined")

    assert tuple(_delivery_row(db)) == ("bounced", "link_policy_node_quarantined", 1)


def test_a_reason_code_from_another_node_is_kept_only_up_to_a_bound(db, alice, node_identity, remote_node_identity):
    message = _sent(db, alice, remote_node_identity, node_identity)

    record_link_message_refused(db, message.content_id, "link_policy_" + "x" * 5000)

    assert len(_delivery_row(db)["link_delivery_reason"]) == 64


def test_a_repeated_bounce_is_not_told_twice(db, alice, node_identity, remote_node_identity):
    message = _sent(db, alice, remote_node_identity, node_identity)
    apply_link_message_bounced(db, _bounce(remote_node_identity, message))
    acknowledge_delivery_notices(db, [_id(db)])

    apply_link_message_bounced(db, _bounce(remote_node_identity, message))

    assert _delivery_row(db)["link_delivery_notice_pending"] == 0


def test_expiry_flags_the_sender_and_a_replay_takes_it_back(db, alice, node_identity, remote_node_identity):
    message = _sent(db, alice, remote_node_identity, node_identity)

    expire_link_message_delivery(db, message.content_id)
    assert tuple(_delivery_row(db)) == ("expired", None, 1)

    unexpire_link_message_delivery(db, message.content_id)
    assert tuple(_delivery_row(db)) == ("pending", None, 0)


def test_a_late_acceptance_clears_an_expiry_the_sender_was_not_yet_told(
    db, alice, node_identity, remote_node_identity
):
    message = _sent(db, alice, remote_node_identity, node_identity)
    expire_link_message_delivery(db, message.content_id)

    apply_link_message_accepted(db, build_link_message_accepted(
        signing_identity=remote_node_identity.signing_key,
        recipient_node_fingerprint=remote_node_identity.fingerprint,
        message_content_id=message.content_id,
        created_at="2026-01-01T00:05:00Z",
    ).to_dict())

    assert tuple(_delivery_row(db)) == ("delivered", None, 0)


def _id(db):
    return db.connection.execute("SELECT id FROM mail_messages").fetchone()[0]


# -- mail left at a relay that gets no answer (issue #874) ----------------------

_HANDOFF = "2026-03-01T12:00:00.000000Z"
_HANDOFF_MOMENT = datetime.datetime(2026, 3, 1, 12, tzinfo=datetime.timezone.utc)


def _handoff_row(db):
    return db.connection.execute(
        "SELECT link_delivery_status, link_delivery_reason, link_delivery_notice_pending, link_relay_handoff_at "
        "FROM mail_messages"
    ).fetchone()


def test_a_relay_handoff_keeps_the_letter_pending_and_records_when(db, alice, node_identity, remote_node_identity):
    message = _sent(db, alice, remote_node_identity, node_identity)

    record_relay_handoff(db, message.content_id, now=_HANDOFF)

    assert tuple(_handoff_row(db)) == ("pending", None, 0, _HANDOFF)


def test_a_relay_handoff_does_not_touch_a_letter_already_answered(db, alice, node_identity, remote_node_identity):
    """The recipient can collect and answer before this node's own pass
    records the deposit; the answer stands."""
    message = _sent(db, alice, remote_node_identity, node_identity)
    apply_link_message_bounced(db, _bounce(remote_node_identity, message))

    record_relay_handoff(db, message.content_id, now=_HANDOFF)

    assert _handoff_row(db)["link_relay_handoff_at"] is None


def test_a_letter_left_at_a_relay_expires_after_fourteen_days_without_an_answer(
    db, alice, node_identity, remote_node_identity
):
    message = _sent(db, alice, remote_node_identity, node_identity)
    record_relay_handoff(db, message.content_id, now=_HANDOFF)

    just_before = _HANDOFF_MOMENT + datetime.timedelta(days=14) - datetime.timedelta(seconds=1)
    assert expire_unanswered_relay_mail(db, now=just_before) == 0
    assert _delivery_row(db)["link_delivery_status"] == "pending"

    assert expire_unanswered_relay_mail(db, now=_HANDOFF_MOMENT + datetime.timedelta(days=14)) == 1
    assert tuple(_delivery_row(db)) == ("expired", "no_answer", 1)
    # Once is enough: a second pass finds nothing left to expire.
    assert expire_unanswered_relay_mail(db, now=_HANDOFF_MOMENT + datetime.timedelta(days=30)) == 0


def test_a_letter_pushed_directly_never_times_out(db, alice, node_identity, remote_node_identity):
    """Direct pushes are unaffected (issue #874): only a relay handoff
    starts the clock."""
    _sent(db, alice, remote_node_identity, node_identity)

    assert expire_unanswered_relay_mail(db, now=_HANDOFF_MOMENT + datetime.timedelta(days=365)) == 0
    assert _delivery_row(db)["link_delivery_status"] == "pending"


def test_a_late_acceptance_wins_over_a_relay_timeout(db, alice, node_identity, remote_node_identity):
    message = _sent(db, alice, remote_node_identity, node_identity)
    record_relay_handoff(db, message.content_id, now=_HANDOFF)
    expire_unanswered_relay_mail(db, now=_HANDOFF_MOMENT + datetime.timedelta(days=15))

    apply_link_message_accepted(db, build_link_message_accepted(
        signing_identity=remote_node_identity.signing_key,
        recipient_node_fingerprint=remote_node_identity.fingerprint,
        message_content_id=message.content_id,
        created_at="2026-01-01T00:05:00Z",
    ).to_dict())

    assert tuple(_delivery_row(db)) == ("delivered", None, 0)


def test_a_late_bounce_wins_over_a_relay_timeout_and_is_told_again(db, alice, node_identity, remote_node_identity):
    """The sender already read "may not have arrived"; the bounce is the
    first real answer, so they are told that too."""
    message = _sent(db, alice, remote_node_identity, node_identity)
    record_relay_handoff(db, message.content_id, now=_HANDOFF)
    expire_unanswered_relay_mail(db, now=_HANDOFF_MOMENT + datetime.timedelta(days=15))
    acknowledge_delivery_notices(db, [_id(db)])

    apply_link_message_bounced(db, _bounce(remote_node_identity, message, "blocked_sender"))

    assert tuple(_delivery_row(db)) == ("bounced", "blocked_sender", 1)


def test_a_relay_timeout_is_told_as_maybe_not_arrived(db, alice, node_identity, remote_node_identity):
    message = _sent(db, alice, remote_node_identity, node_identity, subject="Lunch")
    record_relay_handoff(db, message.content_id, now=_HANDOFF)
    expire_unanswered_relay_mail(db, now=_HANDOFF_MOMENT + datetime.timedelta(days=15))

    lines, _ids = pending_delivery_notices(db, alice)

    assert lines == [
        'Your mail "Lunch" to bob@Unnamed linked node expired: no answer came back in the 14 days '
        "since it was left at a relay for that BBS, so it may not have arrived."
    ]


def test_a_replay_forgets_an_old_relay_handoff(db, alice, node_identity, remote_node_identity):
    message = _sent(db, alice, remote_node_identity, node_identity)
    record_relay_handoff(db, message.content_id, now=_HANDOFF)
    expire_link_message_delivery(db, message.content_id)

    unexpire_link_message_delivery(db, message.content_id)

    assert tuple(_handoff_row(db)) == ("pending", None, 0, None)


@pytest.mark.parametrize(
    ("status", "handoff", "shown"),
    [
        ("pending", None, "pending"),
        ("pending", _HANDOFF, "relayed"),
        ("delivered", _HANDOFF, "delivered"),
        ("expired", _HANDOFF, "expired"),
        (None, None, None),
    ],
)
def test_delivery_display_status(status, handoff, shown):
    assert delivery_display_status(status, handoff) == shown


def test_every_expiry_reason_has_plain_words():
    """A sender-side reason: this node gives it to itself, so it belongs in
    the expiry table and in neither bounce table."""
    from netbbs.link import mail
    from netbbs.link.mail import _BOUNCE_REASON_TEXT, _EXPIRED_TEXT

    codes = {value for name, value in vars(mail).items() if name.startswith("EXPIRED_") and isinstance(value, str)}
    assert codes == set(_EXPIRED_TEXT) == {"own_policy", "no_answer"}
    assert not codes & set(_BOUNCE_REASON_TEXT)


def test_pending_delivery_notices_name_the_message_and_the_reason_until_acknowledged(
    db, alice, bob, node_identity, remote_node_identity
):
    message = _sent(db, alice, remote_node_identity, node_identity, subject="Lunch on Friday")
    apply_link_message_bounced(db, _bounce(remote_node_identity, message))

    assert pending_delivery_notices(db, bob) == ([], [])
    lines, ids = pending_delivery_notices(db, alice)
    assert lines == [
        # The same label Sent shows: a peer with no name is not a fingerprint.
        'Your mail "Lunch on Friday" to bob@Unnamed linked node bounced: '
        "there is no user by that name on that BBS."
    ]
    # Nothing is marked until the lines are on screen.
    assert pending_delivery_notices(db, alice)[1] == ids

    acknowledge_delivery_notices(db, ids)
    assert pending_delivery_notices(db, alice) == ([], [])


def test_pending_delivery_notices_leave_out_mail_deleted_from_sent(db, alice, node_identity, remote_node_identity):
    from netbbs.mail import delete_for_sender, list_sent

    message = _sent(db, alice, remote_node_identity, node_identity)
    delete_for_sender(db, alice, list_sent(db, alice)[0])

    apply_link_message_bounced(db, _bounce(remote_node_identity, message))

    assert pending_delivery_notices(db, alice) == ([], [])


def test_pending_delivery_notices_cap_the_list_and_count_the_rest(db, alice, node_identity, remote_node_identity):
    _seed_peer(db, remote_node_identity)
    for index in range(MAX_DELIVERY_NOTICES_SHOWN + 3):
        message = compose_link_message(
            db, alice, f"bob@{remote_node_identity.fingerprint}", f"m{index}", "world", node_identity=node_identity,
        )
        expire_link_message_delivery(db, message.content_id)

    lines, ids = pending_delivery_notices(db, alice)

    assert len(ids) == MAX_DELIVERY_NOTICES_SHOWN + 3
    assert len(lines) == MAX_DELIVERY_NOTICES_SHOWN + 1
    assert "was not delivered: no route to that BBS worked" in lines[0]
    assert lines[-1] == "...and 3 more messages not delivered; see E-mail, Sent."


def test_every_reason_a_node_can_give_has_plain_words():
    from netbbs.link import enforcement
    from netbbs.link.events import _VALID_BOUNCE_REASONS
    from netbbs.link.mail import _BOUNCE_REASON_TEXT

    codes = set(_VALID_BOUNCE_REASONS) | {
        value for name, value in vars(enforcement).items() if name.startswith("REASON_")
    }
    assert codes <= set(_BOUNCE_REASON_TEXT)
    for code in codes:
        assert "_" not in bounce_reason_text(code)
    assert bounce_reason_text("something_new") == "that BBS refused it"
    assert bounce_reason_text(None) == "that BBS refused it"


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        ("pending", None, "Pending: that BBS has not confirmed it yet."),
        ("delivered", None, "Delivered to the recipient's mailbox."),
        ("bounced", "unknown_recipient", "Bounced: there is no user by that name on that BBS."),
        ("expired", None, "Expired: no route to that BBS worked before delivery gave up. It was not delivered."),
        (
            "expired", "own_policy",
            "Expired: this BBS stopped exchanging mail with that BBS before it could be sent. It was not delivered.",
        ),
        (
            "relayed", None,
            "With a relay, no answer yet: it was left at a relay for that BBS to collect. "
            "If no answer comes back within 14 days, it expires.",
        ),
        (
            "expired", "no_answer",
            "Expired: no answer came back in the 14 days since it was left at a relay for that BBS, "
            "so it may not have arrived.",
        ),
        (None, None, None),
    ],
)
def test_delivery_explanation(status, reason, expected):
    assert delivery_explanation(status, reason) == expected


# -- what a received letter is checked against (issue #808) ------------------


def _bounce_reason(db):
    row = db.connection.execute("SELECT ack_event_json FROM link_mail_acknowledgements").fetchone()
    envelope = json.loads(row["ack_event_json"])["envelope"]
    assert envelope["object_type"] == "link_message_bounced"
    return envelope["payload"]["reason"]


def _mail_count(db):
    return db.connection.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0]


def test_a_received_letter_is_dated_when_its_sender_wrote_it(db, bob, node_identity, remote_node_identity):
    message = _incoming_message(node_identity, remote_node_identity, created_at="2026-01-01T09:30:00+02:00")

    deliver_link_message(db, message.to_dict(), node_identity=node_identity)

    row = db.connection.execute("SELECT created_at FROM mail_messages").fetchone()
    assert row["created_at"] == "2026-01-01T07:30:00.000000Z"


def test_a_letter_dated_in_the_future_or_before_2000_is_dated_by_its_arrival(db, bob, node_identity, remote_node_identity):
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    within = (now + datetime.timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    beyond = (now + datetime.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    for created_at in (within, beyond, "yesterday-ish", "0001-01-01T00:00:00.000000Z", "1999-12-31T23:59:59Z"):
        message = _incoming_message(
            node_identity, remote_node_identity, created_at=created_at, subject=created_at,
        )
        deliver_link_message(db, message.to_dict(), node_identity=node_identity)

    rows = db.connection.execute("SELECT created_at FROM mail_messages ORDER BY id").fetchall()
    assert rows[0]["created_at"] == within
    for row in rows[1:]:
        stored = datetime.datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
        assert now - datetime.timedelta(seconds=5) <= stored <= now + datetime.timedelta(minutes=1)


def test_a_late_letter_is_listed_by_its_arrival_not_buried_by_its_date(db, alice, bob, node_identity, remote_node_identity):
    from netbbs.mail import list_inbox, send_mail

    send_mail(db, alice, bob, "local, sent today", "body")
    late = _incoming_message(
        node_identity, remote_node_identity, subject="written last year", created_at="2025-06-01T00:00:00Z",
    )
    deliver_link_message(db, late.to_dict(), node_identity=node_identity)

    assert [message.subject for message in list_inbox(db, bob)] == ["written last year", "local, sent today"]


@pytest.mark.parametrize(
    "sender", ['alice@"Trusted Node"', "bad name", "x" * 33, "alice\n", "", "Böb"],
)
def test_a_sender_name_outside_the_address_grammar_bounces_malformed(
    db, bob, node_identity, remote_node_identity, sender
):
    message = _incoming_message(node_identity, remote_node_identity, sender=sender)

    deliver_link_message(db, message.to_dict(), node_identity=node_identity)

    assert _mail_count(db) == 0
    assert _bounce_reason(db) == "malformed"


def test_a_sender_name_the_grammar_allows_is_delivered_as_written(db, bob, node_identity, remote_node_identity):
    message = _incoming_message(node_identity, remote_node_identity, sender="Old.Nib_2-x")

    deliver_link_message(db, message.to_dict(), node_identity=node_identity)

    row = db.connection.execute("SELECT sender_label FROM mail_messages").fetchone()
    assert row["sender_label"] == f"Old.Nib_2-x@{remote_node_identity.fingerprint}"


@pytest.mark.parametrize(
    "plaintext",
    [
        json.dumps({"subject": "   ", "body": "world"}).encode("utf-8"),
        json.dumps({"subject": "s" * 201, "body": "world"}).encode("utf-8"),
        json.dumps({"subject": "hello", "body": "b" * 20_001}).encode("utf-8"),
        json.dumps({"subject": "hello", "body": 42}).encode("utf-8"),
        json.dumps({"subject": ["hello"], "body": "world"}).encode("utf-8"),
        json.dumps({"body": "world"}).encode("utf-8"),
        json.dumps({"subject": "hello"}).encode("utf-8"),
        json.dumps(["hello", "world"]).encode("utf-8"),
        b'{"subject": "\\ud800", "body": "world"}',
        b"not json at all",
        b"\xff\xfe",
    ],
    ids=[
        "blank-subject", "long-subject", "long-body", "body-not-text", "subject-not-text",
        "no-subject", "no-body", "not-an-object", "lone-surrogate", "not-json", "not-utf8",
    ],
)
def test_a_letter_local_mail_would_refuse_bounces_malformed(db, bob, node_identity, remote_node_identity, plaintext):
    message = _incoming_message(node_identity, remote_node_identity, plaintext=plaintext)

    deliver_link_message(db, message.to_dict(), node_identity=node_identity)

    assert _mail_count(db) == 0
    assert _bounce_reason(db) == "malformed"


def test_a_letter_at_the_limits_is_delivered_with_its_subject_trimmed(db, bob, node_identity, remote_node_identity):
    message = _incoming_message(
        node_identity, remote_node_identity, subject="  " + "s" * 200 + " ", body="b" * 20_000,
    )

    deliver_link_message(db, message.to_dict(), node_identity=node_identity)

    row = db.connection.execute("SELECT subject, body FROM mail_messages").fetchone()
    assert (row["subject"], len(row["body"])) == ("s" * 200, 20_000)


def test_a_letter_this_node_cannot_decrypt_bounces_undecryptable(db, bob, node_identity, remote_node_identity):
    stranger = bootstrap_node_identity("stranger")
    sealed_elsewhere = encrypt_for(stranger.signing_key.verify_key, b'{"subject": "hi", "body": "x"}')
    message = _incoming_message(node_identity, remote_node_identity, ciphertext=sealed_elsewhere)

    deliver_link_message(db, message.to_dict(), node_identity=node_identity)

    assert _mail_count(db) == 0
    assert _bounce_reason(db) == "undecryptable"


def test_a_ciphertext_that_is_not_base64_bounces_undecryptable(db, bob, node_identity, remote_node_identity):
    raw = _incoming_message(node_identity, remote_node_identity).to_dict()
    raw["envelope"]["payload"]["ciphertext"] = "!!! not base64 !!!"

    deliver_link_message(db, raw, node_identity=node_identity)

    assert _mail_count(db) == 0
    assert _bounce_reason(db) == "undecryptable"


def test_a_new_bounce_reason_reaches_the_sender_in_words(db, alice, node_identity, remote_node_identity):
    message = _sent(db, alice, remote_node_identity, node_identity)

    apply_link_message_bounced(db, _bounce(remote_node_identity, message, reason="undecryptable"))

    row = _delivery_row(db)
    assert (row["link_delivery_status"], row["link_delivery_reason"]) == ("bounced", "undecryptable")
    assert "could not decrypt it" in delivery_explanation("bounced", "undecryptable")
