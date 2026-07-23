"""Tests for `netbbs.link.relay_mailbox` (design doc §12, issue
#58) -- the bounded relay store-and-forward mailbox. `tests/
test_link_transport.py` already exercises deposit/pickup through the
real HTTP layer; this file covers the module's own plain `db`-first
functions directly, including `mailbox_sizes` (issue #60's non-
destructive peek, added for the SysOp Link-status screen)."""

from __future__ import annotations

import pytest

from netbbs.link.events import build_link_message
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.relay_mailbox import (
    deposit_relay_mailbox_envelope,
    mailbox_sizes,
    pickup_relay_mailbox_envelopes,
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


def test_mailbox_sizes_is_empty_for_a_fresh_mailbox(db):
    assert mailbox_sizes(db) == {}


def test_mailbox_sizes_counts_per_recipient(db):
    deposit_relay_mailbox_envelope(db, "recipient-a", _link_message(recipient_fingerprint="recipient-a"))
    deposit_relay_mailbox_envelope(db, "recipient-a", _link_message(recipient_fingerprint="recipient-a"))
    deposit_relay_mailbox_envelope(db, "recipient-b", _link_message(recipient_fingerprint="recipient-b"))

    assert mailbox_sizes(db) == {"recipient-a": 2, "recipient-b": 1}


def test_mailbox_sizes_is_non_destructive(db):
    deposit_relay_mailbox_envelope(db, "recipient-a", _link_message(recipient_fingerprint="recipient-a"))

    assert mailbox_sizes(db) == {"recipient-a": 1}
    assert mailbox_sizes(db) == {"recipient-a": 1}  # calling it again doesn't consume anything

    picked_up = pickup_relay_mailbox_envelopes(db, "recipient-a")
    assert len(picked_up) == 1  # still there for the real, destructive read path
    assert mailbox_sizes(db) == {}  # pickup itself is what empties it
