"""Tests for `netbbs.link.relay_mailbox` (design doc §12, issue
#58) -- the bounded relay store-and-forward mailbox. `tests/
test_link_transport.py` already exercises deposit/pickup through the
real HTTP layer; this file covers the module's own plain `db`-first
functions directly, including `mailbox_holdings` (issue #60's non-
destructive peek for the SysOp Link-status screen, with the oldest
deposit since issue #891) and the retention prune (issue #891)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from netbbs.link.events import LinkMessageAccepted, build_link_message, build_link_message_accepted
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.relay_mailbox import (
    RELAY_MAILBOX_RETENTION_DAYS,
    deposit_relay_mailbox_envelope,
    mailbox_holdings,
    pickup_relay_mailbox_envelopes,
    prune_expired_relay_mailbox_envelopes,
)
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


def _link_message(*, recipient_fingerprint: str, local_user_id: str = "wanderer"):
    sender_identity = bootstrap_node_identity("sender")
    return build_link_message(
        signing_identity=sender_identity.signing_key,
        home_node_fingerprint=sender_identity.fingerprint,
        local_user_id=local_user_id,
        recipient_home_node_fingerprint=recipient_fingerprint,
        recipient_local_user_id="recipient",
        confidentiality_tier="tier1_home_node_key",
        ciphertext=b"opaque-ciphertext",
        created_at="2026-01-01T00:00:00+00:00",
    )


def _sizes(db):
    return {holding.recipient_fingerprint: holding.count for holding in mailbox_holdings(db)}


def _backdate(db, content_id: str, received_at: str) -> None:
    db.connection.execute(
        "UPDATE link_relay_mailbox SET received_at = ? WHERE content_id = ?", (received_at, content_id)
    )
    db.connection.commit()


def _link_message_accepted(*, recipient_node_fingerprint: str):
    signer_identity = bootstrap_node_identity("original-recipient")
    return build_link_message_accepted(
        signing_identity=signer_identity.signing_key,
        recipient_node_fingerprint=recipient_node_fingerprint,
        message_content_id="some-original-message-content-id",
        created_at="2026-01-01T00:00:00+00:00",
    )


def test_mailbox_holdings_is_empty_for_a_fresh_mailbox(db):
    assert _sizes(db) == {}


def test_mailbox_holdings_counts_per_recipient(db):
    deposit_relay_mailbox_envelope(db, "recipient-a", _link_message(recipient_fingerprint="recipient-a"))
    deposit_relay_mailbox_envelope(db, "recipient-a", _link_message(recipient_fingerprint="recipient-a"))
    deposit_relay_mailbox_envelope(db, "recipient-b", _link_message(recipient_fingerprint="recipient-b"))

    assert _sizes(db) == {"recipient-a": 2, "recipient-b": 1}


def test_mailbox_holdings_is_non_destructive(db):
    deposit_relay_mailbox_envelope(db, "recipient-a", _link_message(recipient_fingerprint="recipient-a"))

    assert _sizes(db) == {"recipient-a": 1}
    assert _sizes(db) == {"recipient-a": 1}  # calling it again doesn't consume anything

    picked_up = pickup_relay_mailbox_envelopes(db, "recipient-a")
    assert len(picked_up) == 1  # still there for the real, destructive read path
    assert _sizes(db) == {}  # pickup itself is what empties it


def test_deposit_and_pickup_round_trip_a_link_message_accepted(db):
    """Issue #94: before this, only `link_message` itself could be
    deposited/picked up here at all -- `link_message_accepted`/`_
    bounced` had no relay path back to a sender who can't be dialed
    directly. Proves the plain db-level round trip reconstructs the
    correct type from what was actually stored, not just whatever type
    happened to be passed in most recently."""
    ack = _link_message_accepted(recipient_node_fingerprint="original-recipient-fingerprint")
    deposit_relay_mailbox_envelope(db, "original-sender-fingerprint", ack)

    assert _sizes(db) == {"original-sender-fingerprint": 1}

    [picked_up] = pickup_relay_mailbox_envelopes(db, "original-sender-fingerprint")
    assert isinstance(picked_up, LinkMessageAccepted)
    assert picked_up.content_id == ack.content_id
    assert picked_up.payload == ack.payload


def test_deposit_counts_a_mixed_link_message_and_acknowledgement_together(db):
    """A relay's mailbox for one recipient can hold both an inbound
    `link_message` and an outbound-bound acknowledgement at once (they
    address different, unrelated exchanges) -- `mailbox_holdings` counts
    both without caring which is which, same as it never cared about
    `link_message`'s own internal shape before this issue widened what
    could be stored here at all."""
    deposit_relay_mailbox_envelope(
        db, "shared-fingerprint", _link_message(recipient_fingerprint="shared-fingerprint")
    )
    deposit_relay_mailbox_envelope(
        db, "shared-fingerprint", _link_message_accepted(recipient_node_fingerprint="shared-fingerprint")
    )

    assert _sizes(db) == {"shared-fingerprint": 2}
    picked_up = pickup_relay_mailbox_envelopes(db, "shared-fingerprint")
    assert len(picked_up) == 2


# -- retention (issue #891) ---------------------------------------------------

NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def test_mailbox_holdings_reports_the_oldest_deposit_oldest_recipient_first(db):
    fresh = _link_message(recipient_fingerprint="recipient-a")
    older = _link_message(recipient_fingerprint="recipient-a", local_user_id="other")
    deposit_relay_mailbox_envelope(db, "recipient-a", fresh)
    deposit_relay_mailbox_envelope(db, "recipient-a", older)
    deposit_relay_mailbox_envelope(db, "recipient-b", _link_message(recipient_fingerprint="recipient-b"))
    _backdate(db, fresh.content_id, _stamp(NOW - timedelta(days=2)))
    _backdate(db, older.content_id, _stamp(NOW - timedelta(days=20)))

    holdings = mailbox_holdings(db)

    assert [(h.recipient_fingerprint, h.count) for h in holdings] == [("recipient-a", 2), ("recipient-b", 1)]
    assert holdings[0].oldest_received_at == _stamp(NOW - timedelta(days=20))


def test_prune_drops_only_envelopes_past_the_retention_time(db):
    expired = _link_message(recipient_fingerprint="gone-for-good")
    kept = _link_message(recipient_fingerprint="gone-for-good", local_user_id="later")
    deposit_relay_mailbox_envelope(db, "gone-for-good", expired)
    deposit_relay_mailbox_envelope(db, "gone-for-good", kept)
    _backdate(db, expired.content_id, _stamp(NOW - timedelta(days=RELAY_MAILBOX_RETENTION_DAYS, seconds=1)))
    _backdate(db, kept.content_id, _stamp(NOW - timedelta(days=RELAY_MAILBOX_RETENTION_DAYS) + timedelta(minutes=1)))

    assert prune_expired_relay_mailbox_envelopes(db, now=NOW) == {"gone-for-good": 1}

    [left] = pickup_relay_mailbox_envelopes(db, "gone-for-good")
    assert left.content_id == kept.content_id


def test_prune_gives_acknowledgements_the_same_retention(db):
    """Issue #891: an acceptance or bounce left for a node that never
    collects it holds a slot just as a letter does."""
    ack = _link_message_accepted(recipient_node_fingerprint="original-recipient")
    deposit_relay_mailbox_envelope(db, "original-sender", ack)
    _backdate(db, ack.content_id, _stamp(NOW - timedelta(days=RELAY_MAILBOX_RETENTION_DAYS + 1)))

    assert prune_expired_relay_mailbox_envelopes(db, now=NOW) == {"original-sender": 1}
    assert _sizes(db) == {}


def test_prune_with_nothing_expired_changes_nothing(db):
    deposit_relay_mailbox_envelope(db, "recipient-a", _link_message(recipient_fingerprint="recipient-a"))

    assert prune_expired_relay_mailbox_envelopes(db) == {}
    assert _sizes(db) == {"recipient-a": 1}


def test_prune_frees_a_full_mailbox_for_new_deposits(db):
    """The point of issue #891: a recipient that never came back held its
    slots forever, and every later deposit for it was refused."""
    from netbbs.link.relay_mailbox import MAX_MAILBOX_ENVELOPES_PER_RECIPIENT, RelayMailboxFullError

    for index in range(MAX_MAILBOX_ENVELOPES_PER_RECIPIENT):
        deposit_relay_mailbox_envelope(
            db, "abandoned", _link_message(recipient_fingerprint="abandoned", local_user_id=f"u{index}")
        )
    newest = _link_message(recipient_fingerprint="abandoned", local_user_id="newest")
    with pytest.raises(RelayMailboxFullError):
        deposit_relay_mailbox_envelope(db, "abandoned", newest)

    db.connection.execute(
        "UPDATE link_relay_mailbox SET received_at = ?", (_stamp(NOW - timedelta(days=RELAY_MAILBOX_RETENTION_DAYS + 1)),)
    )
    db.connection.commit()
    assert prune_expired_relay_mailbox_envelopes(db, now=NOW) == {"abandoned": MAX_MAILBOX_ENVELOPES_PER_RECIPIENT}

    deposit_relay_mailbox_envelope(db, "abandoned", newest)
    assert _sizes(db) == {"abandoned": 1}
