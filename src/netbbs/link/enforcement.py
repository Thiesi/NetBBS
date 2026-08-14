"""Local Phase-4 trust enforcement decisions for Link boundaries (issue #128).

This module is synchronous and ``db``-first.  It exposes non-leaking stable
reason codes and keeps policy evaluation out of protocol parsing, transport,
storage, and rendering code.  Callers dispatch it through ``DatabaseLane``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from typing import Any

from netbbs.link.trust import (
    TrustDimension,
    TrustState,
    TrustSubject,
    get_effective_trust_state,
    register_subject,
)
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


class LinkPolicyAction(StrEnum):
    HELLO = "hello"
    KEY_LIFECYCLE = "key_lifecycle"
    EVENTS = "events"
    INVENTORY = "inventory"
    FILE = "file"
    LINK_MAIL = "link_mail"
    RELAY = "relay"
    PEER_LIST = "peer_list"
    TRUST = "trust"
    OUTBOUND_SYNC = "outbound_sync"


REASON_MANUAL_BLOCK = "link_policy_manual_block"
REASON_NODE_QUARANTINED = "link_policy_node_quarantined"
REASON_NODE_PROBATIONARY = "link_policy_node_probationary_read_only"
REASON_USER_QUARANTINED = "link_policy_user_quarantined"
REASON_USER_PROBATIONARY = "link_policy_user_probationary_approval_required"
REASON_PROBATION_BUDGET = "link_policy_probation_budget_exceeded"

PROBATION_INVENTORY_BUDGET_DIVISOR = 4


@dataclass(frozen=True)
class LinkPolicyDecision:
    allowed: bool
    reason_code: str | None
    state: TrustState
    budget_divisor: int = 1
    requires_approval: bool = False


def ensure_node_subject(
    db: Database, fingerprint: str, *, accepted_at: str | None = None
) -> None:
    """Register a verified hello subject without resetting its first-seen time."""
    subject = TrustSubject.node(fingerprint)
    if db.connection.execute(
        "SELECT 1 FROM link_trust_subjects WHERE subject_id = ?", (subject.subject_id,)
    ).fetchone() is None:
        now = accepted_at or utc_now_iso()
        register_subject(db, subject, first_accepted_at=now, now_iso=now)


def ensure_event_author_subject(
    db: Database, envelope: dict[str, Any], *, accepted_at: str | None = None
) -> TrustSubject | None:
    """Register an author only after protocol verification accepted its event."""
    subject = event_author(envelope)
    if subject is None:
        return None
    if db.connection.execute(
        "SELECT 1 FROM link_trust_subjects WHERE subject_id = ?", (subject.subject_id,)
    ).fetchone() is None:
        now = accepted_at or utc_now_iso()
        register_subject(db, subject, first_accepted_at=now, now_iso=now)
    return subject


def _state(db: Database, subject: TrustSubject, dimension: TrustDimension) -> TrustState:
    try:
        return get_effective_trust_state(db, subject, dimension).state
    except ValueError:
        return TrustState.PROBATIONARY


def _strongest(states: list[TrustState]) -> TrustState:
    for candidate in (
        TrustState.BLOCKED, TrustState.QUARANTINED,
        TrustState.PROBATIONARY, TrustState.ESTABLISHED,
    ):
        if candidate in states:
            return candidate
    return TrustState.PROBATIONARY


def node_transport_state(db: Database, fingerprint: str) -> TrustState:
    """Return node state from transport-relevant dimensions only.

    Content-conduct state is intentionally excluded: subjective content input
    cannot quarantine transport.
    """
    subject = TrustSubject.node(fingerprint)
    return _strongest([
        _state(db, subject, TrustDimension.IDENTITY_INTEGRITY),
        _state(db, subject, TrustDimension.RESOURCE_BEHAVIOR),
    ])


def decide_node_action(
    db: Database, fingerprint: str, action: LinkPolicyAction | str
) -> LinkPolicyDecision:
    action = LinkPolicyAction(action)
    state = node_transport_state(db, fingerprint)
    if state == TrustState.BLOCKED:
        return LinkPolicyDecision(False, REASON_MANUAL_BLOCK, state)
    if state == TrustState.QUARANTINED:
        if action in {LinkPolicyAction.HELLO, LinkPolicyAction.KEY_LIFECYCLE}:
            return LinkPolicyDecision(True, None, state)
        return LinkPolicyDecision(False, REASON_NODE_QUARANTINED, state)
    if state == TrustState.PROBATIONARY:
        if action in {LinkPolicyAction.HELLO, LinkPolicyAction.KEY_LIFECYCLE}:
            return LinkPolicyDecision(True, None, state)
        if action == LinkPolicyAction.INVENTORY:
            return LinkPolicyDecision(True, None, state, PROBATION_INVENTORY_BUDGET_DIVISOR)
        return LinkPolicyDecision(False, REASON_NODE_PROBATIONARY, state)
    return LinkPolicyDecision(True, None, state)


def decide_user_authorship(
    db: Database, home_node_fingerprint: str, opaque_user_id: str
) -> LinkPolicyDecision:
    subject = TrustSubject.user(home_node_fingerprint, opaque_user_id)
    state = _strongest([
        _state(db, subject, TrustDimension.IDENTITY_INTEGRITY),
        _state(db, subject, TrustDimension.RESOURCE_BEHAVIOR),
        _state(db, subject, TrustDimension.CONTENT_CONDUCT),
    ])
    if state == TrustState.BLOCKED:
        return LinkPolicyDecision(False, REASON_MANUAL_BLOCK, state)
    if state == TrustState.QUARANTINED:
        return LinkPolicyDecision(False, REASON_USER_QUARANTINED, state)
    if state == TrustState.PROBATIONARY:
        return LinkPolicyDecision(True, REASON_USER_PROBATIONARY, state, requires_approval=True)
    return LinkPolicyDecision(True, None, state)


def event_author(envelope: dict[str, Any]) -> TrustSubject | None:
    """Extract the independently signed author/origin from a verified envelope."""
    payload = envelope.get("envelope", envelope).get("payload", {})
    object_type = envelope.get("envelope", envelope).get("object_type")
    if object_type == "key_transition":
        fingerprint = payload.get("subject_fingerprint")
        return TrustSubject.node(fingerprint) if fingerprint else None
    identity = payload.get("author") or payload.get("sender")
    if isinstance(identity, dict):
        home = identity.get("home_node_fingerprint")
        # Link v1's node-vouched author payload predates Phase 4 and calls
        # this stable home-node-local identifier ``local_user_id``.  Trust
        # subjects name the same value ``opaque_user_id``; accept both wire
        # spellings without changing or exposing the identifier.
        opaque = identity.get("opaque_user_id") or identity.get("local_user_id")
        if home and opaque:
            return TrustSubject.user(home, opaque)
        if home:
            return TrustSubject.node(home)
    origin = payload.get("origin_fingerprint")
    if origin:
        return TrustSubject.node(origin)
    return None


def decide_event_authorship(
    db: Database, envelope: dict[str, Any], *, transport_peer_fingerprint: str
) -> LinkPolicyDecision:
    inner = envelope.get("envelope", envelope)
    object_type = inner.get("object_type")
    author = event_author(envelope)
    if author is None:
        return decide_node_action(db, transport_peer_fingerprint, LinkPolicyAction.EVENTS)
    if author.kind == "user":
        home = decide_node_action(db, author.node_fingerprint, LinkPolicyAction.EVENTS)
        if not home.allowed:
            return home
        user = decide_user_authorship(db, author.node_fingerprint, author.opaque_user_id or "")
        if user.requires_approval and object_type not in {"board_post", "board_post_edit"}:
            return LinkPolicyDecision(False, REASON_USER_PROBATIONARY, user.state)
        return user
    return decide_node_action(db, author.node_fingerprint, LinkPolicyAction.EVENTS)


def content_visible_for_subject(db: Database, subject: TrustSubject) -> bool:
    """Suppress current projections only; never delete accepted source bytes."""
    dimensions = list(TrustDimension) if subject.kind == "user" else [TrustDimension.CONTENT_CONDUCT]
    return all(_state(db, subject, dimension) not in {TrustState.BLOCKED, TrustState.QUARANTINED}
               for dimension in dimensions)


def link_content_visible(db: Database, content_id: str) -> bool:
    """Return current local visibility for one retained signed Link event."""
    row = db.connection.execute(
        "SELECT envelope_json FROM link_events WHERE content_id = ?", (content_id,)
    ).fetchone()
    if row is None:
        return True
    try:
        author = event_author(json.loads(row[0]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if author is None:
        return True
    # Historical projections follow the independently signed author identity.
    # A quarantined relay therefore cannot taint content it merely carried,
    # while quarantine of the author's home node suppresses that node's users.
    home = node_transport_state(db, author.node_fingerprint)
    if home in {TrustState.BLOCKED, TrustState.QUARANTINED}:
        return False
    return content_visible_for_subject(db, author)
