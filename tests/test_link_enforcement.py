"""Phase-4 trust enforcement decisions (design §12.4/12.8, issue #128)."""

import json
import pytest

from netbbs.link.enforcement import (
    LinkPolicyAction,
    REASON_MANUAL_BLOCK,
    REASON_NODE_PROBATIONARY,
    REASON_NODE_QUARANTINED,
    REASON_USER_QUARANTINED,
    content_visible_for_subject,
    decide_event_authorship,
    decide_node_action,
    ensure_node_subject,
    link_content_visible,
)
from netbbs.link.trust import (
    TrustDimension, TrustState, TrustSubject, register_subject, set_trust_override,
)
from netbbs.storage.database import Database

NOW = "2026-08-14T12:00:00+00:00"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


def establish(db, subject):
    register_subject(db, subject, first_accepted_at=NOW, now_iso=NOW)
    for dimension in TrustDimension:
        set_trust_override(
            db, subject, dimension, TrustState.ESTABLISHED,
            reason="test establishment", now_iso=NOW,
        )


def test_unknown_and_new_nodes_are_probationary_with_quarter_inventory_budget(db):
    decision = decide_node_action(db, "new-node", LinkPolicyAction.INVENTORY)
    assert decision.allowed and decision.budget_divisor == 4
    assert decide_node_action(db, "new-node", LinkPolicyAction.EVENTS).reason_code == REASON_NODE_PROBATIONARY
    ensure_node_subject(db, "new-node", accepted_at=NOW)
    assert decide_node_action(db, "new-node", LinkPolicyAction.HELLO).allowed


def test_content_conduct_state_never_quarantines_node_transport(db):
    subject = TrustSubject.node("reported-node")
    establish(db, subject)
    set_trust_override(
        db, subject, TrustDimension.CONTENT_CONDUCT, TrustState.QUARANTINED,
        reason="subjective content policy", now_iso=NOW,
    )
    assert decide_node_action(db, "reported-node", LinkPolicyAction.EVENTS).allowed


def test_quarantine_allows_containment_but_manual_block_denies_hello(db):
    subject = TrustSubject.node("risky-node")
    establish(db, subject)
    set_trust_override(
        db, subject, TrustDimension.IDENTITY_INTEGRITY, TrustState.QUARANTINED,
        reason="verified integrity trigger", now_iso=NOW,
    )
    assert decide_node_action(db, "risky-node", LinkPolicyAction.HELLO).allowed
    assert decide_node_action(db, "risky-node", LinkPolicyAction.KEY_LIFECYCLE).allowed
    assert decide_node_action(
        db, "risky-node", LinkPolicyAction.EVENTS
    ).reason_code == REASON_NODE_QUARANTINED
    set_trust_override(
        db, subject, TrustDimension.IDENTITY_INTEGRITY, TrustState.BLOCKED,
        reason="manual block", now_iso=NOW,
    )
    assert decide_node_action(db, "risky-node", LinkPolicyAction.HELLO).reason_code == REASON_MANUAL_BLOCK


def test_relay_identity_does_not_taint_independently_signed_established_author(db):
    establish(db, TrustSubject.node("author-home"))
    establish(db, TrustSubject.user("author-home", "opaque-user"))
    relay = TrustSubject.node("carrier")
    establish(db, relay)
    set_trust_override(
        db, relay, TrustDimension.IDENTITY_INTEGRITY, TrustState.QUARANTINED,
        reason="carrier compromised", now_iso=NOW,
    )
    post = {"envelope": {"object_type": "board_post", "payload": {"author": {
        "home_node_fingerprint": "author-home", "opaque_user_id": "opaque-user"
    }}}}
    # Policy attribution examines canonical identity fields; signature validity
    # remains protocol.py's separate prerequisite.
    decision = decide_event_authorship(db, post, transport_peer_fingerprint="carrier")
    assert decision.allowed

    db.connection.execute(
        """INSERT INTO link_events
           (content_id, sender_fingerprint, object_type, envelope_json, received_at)
           VALUES ('carried-post', 'carrier', 'board_post', ?, ?)""",
        (json.dumps(post), NOW),
    )
    db.connection.commit()
    assert link_content_visible(db, "carried-post")

    set_trust_override(
        db, TrustSubject.node("author-home"), TrustDimension.RESOURCE_BEHAVIOR,
        TrustState.QUARANTINED, reason="author home compromised", now_iso=NOW,
    )
    assert not link_content_visible(db, "carried-post")


def test_user_quarantine_is_scoped_and_suppresses_without_deleting(db):
    establish(db, TrustSubject.node("home"))
    establish(db, TrustSubject.user("home", "quarantined-user"))
    establish(db, TrustSubject.user("home", "unrelated-user"))
    quarantined = TrustSubject.user("home", "quarantined-user")
    set_trust_override(
        db, quarantined, TrustDimension.CONTENT_CONDUCT, TrustState.QUARANTINED,
        reason="local content restriction", now_iso=NOW,
    )
    assert not content_visible_for_subject(db, quarantined)
    assert content_visible_for_subject(db, TrustSubject.user("home", "unrelated-user"))
    assert decide_node_action(db, "home", LinkPolicyAction.EVENTS).allowed
    assert decide_event_authorship(
        db,
        {"envelope": {"object_type": "board_post", "payload": {
            "author": {"home_node_fingerprint": "home", "opaque_user_id": "quarantined-user"}
        }}},
        transport_peer_fingerprint="home",
    ).reason_code == REASON_USER_QUARANTINED

    envelope = {"envelope": {"object_type": "board_post", "payload": {"author": {
        "home_node_fingerprint": "home", "local_user_id": "quarantined-user"
    }}}}
    db.connection.execute(
        """INSERT INTO link_events
           (content_id, sender_fingerprint, object_type, envelope_json, received_at)
           VALUES ('retained-post', 'unrelated-carrier', 'board_post', ?, ?)""",
        (json.dumps(envelope), NOW),
    )
    db.connection.commit()
    assert not link_content_visible(db, "retained-post")
    assert db.connection.execute(
        "SELECT sender_fingerprint FROM link_events WHERE content_id = 'retained-post'"
    ).fetchone()[0] == "unrelated-carrier"


def test_block_and_recovery_are_reconstructed_after_restart(tmp_path):
    path = tmp_path / "restart.db"
    db = Database(path)
    subject = TrustSubject.node("restart-node")
    establish(db, subject)
    override_id = set_trust_override(
        db, subject, TrustDimension.IDENTITY_INTEGRITY, TrustState.BLOCKED,
        reason="manual response", now_iso=NOW,
    )
    db.close()

    reopened = Database(path)
    try:
        assert decide_node_action(
            reopened, "restart-node", LinkPolicyAction.HELLO
        ).reason_code == REASON_MANUAL_BLOCK
        from netbbs.link.trust import clear_trust_override
        clear_trust_override(
            reopened, override_id, now_iso="2026-08-14T12:01:00+00:00"
        )
        # The prior explicit establishment was superseded by the block;
        # clearing it intentionally returns this dimension to probation.
        assert decide_node_action(
            reopened, "restart-node", LinkPolicyAction.EVENTS
        ).reason_code == REASON_NODE_PROBATIONARY
    finally:
        reopened.close()
