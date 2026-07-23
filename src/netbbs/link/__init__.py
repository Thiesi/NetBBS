"""
NetBBS Link protocol foundation (design doc §7/§11, Phase 3).

Nothing in this package talks to a network yet — no transport, no
gossip, no sync. `netbbs.link.events` is the canonical event-envelope
primitive; `netbbs.link.node_identity` is
the first real consumer of it, implementing the node key-
lifecycle model. Everything downstream of these two (Linked boards,
Link messages, remote file-area discovery, the actual HTTP+JSON
transport) is later Phase 3 work, per the design doc §15 dependency
matrix — this package exists now specifically because that matrix
requires the event model and key lifecycle settled and built *before*
any wire-visible sync work begins.
"""

from netbbs.link.events import (
    NETBBS_PROTOCOL_VERSION,
    EventError,
    KeyTransition,
    build_envelope,
    build_key_transition,
    canonical_bytes,
    event_content_id,
    verify_key_transition,
)
from netbbs.link.node_identity import (
    NodeIdentity,
    NodeIdentityError,
    bootstrap_node_identity,
    load_or_bootstrap_node_identity,
    resolve_current_operational_key,
    rotate_operational_key,
)

__all__ = [
    "NETBBS_PROTOCOL_VERSION",
    "EventError",
    "KeyTransition",
    "build_envelope",
    "build_key_transition",
    "canonical_bytes",
    "event_content_id",
    "verify_key_transition",
    "NodeIdentity",
    "NodeIdentityError",
    "bootstrap_node_identity",
    "load_or_bootstrap_node_identity",
    "resolve_current_operational_key",
    "rotate_operational_key",
]
