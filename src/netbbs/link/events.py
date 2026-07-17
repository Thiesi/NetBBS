"""
Canonical NetBBS Link event envelope (design doc §7, rounds 27/90/110/116).

Round 27 fixed the outer envelope shape (`netbbs_protocol`/
`object_type`/`payload`); round 90 fixed the semantic model (event
chains with head pointers, replacing per-feature special-casing); round
110 fixed the byte-level canonicalization rule (reusing
`netbbs.boards.content_id.canonical_json_bytes` rather than a second
implementation) and the one concrete event type needed to unblock round
89's node key-lifecycle work: `key_transition`. Round 116 adds
`endpoint_descriptor` (design doc §12), the second concrete event type,
needed to unblock the first real handshake/gossip protocol code
(`netbbs.link.protocol`).

No other event type (board posts, moderator grants, etc.) is specified
here yet — each gets its own payload-shape decision when it's actually
being built, following this same envelope pattern (round 110's own
scope note).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

import nacl.signing

from netbbs.boards.content_id import canonical_json_bytes, compute_content_id
from netbbs.identity.keys import Identity, verify_signature

# Round 27: versioning mandatory from the first byte, not inferred.
NETBBS_PROTOCOL_VERSION = 1

# Round 110: the one event type specified so far.
KEY_TRANSITION_OBJECT_TYPE = "key_transition"

# Round 116: a node's signed, periodically-refreshed reachability claim
# (design doc §12).
ENDPOINT_DESCRIPTOR_OBJECT_TYPE = "endpoint_descriptor"

_VALID_PURPOSES = ("signing", "transport")
_VALID_ACTIONS = ("authorize", "revoke")


class EventError(Exception):
    """Raised for a malformed or invalid canonical event envelope, or an
    invalid `key_transition` specifically (bad purpose/action, or a
    signature that doesn't verify against the claimed root key)."""


def build_envelope(object_type: str, payload: dict) -> dict:
    """
    The round-27 envelope shape. Plain construction only — this doesn't
    canonicalize or sign anything itself; see `canonical_bytes`/
    `event_content_id` for that, and `build_key_transition` for a
    complete signed example.
    """
    return {
        "netbbs_protocol": NETBBS_PROTOCOL_VERSION,
        "object_type": object_type,
        "payload": payload,
    }


def canonical_bytes(envelope: dict) -> bytes:
    """
    The exact bytes a signature over `envelope` is made over — reuses
    `netbbs.boards.content_id.canonical_json_bytes` directly (round 110)
    so Link events and Phase 1/2 local content-IDs share exactly one
    canonicalization implementation, not two independently-maintained
    ones that could quietly drift apart.
    """
    return canonical_json_bytes(envelope)


def event_content_id(envelope: dict) -> str:
    """The content-ID of a canonical event envelope — same
    canonicalization as `canonical_bytes`, hashed (round 110)."""
    return compute_content_id(envelope)


@dataclass(frozen=True)
class KeyTransition:
    """
    One signed `key_transition` event (design doc round 89/110): an
    authorize-or-revoke record for one of a node's two operational keys
    (signing, transport), always signed by that node's root key —
    "any node can verify a signature by walking the transition chain
    back to the root" (round 89) — never by another operational key, so
    verification is a flat, direct signature check rather than a
    multi-hop delegation chain.
    """

    envelope: dict
    signature: bytes

    @property
    def payload(self) -> dict:
        return self.envelope["payload"]

    @property
    def content_id(self) -> str:
        """This transition's own content-ID — what the *next* transition
        in the same `(subject_fingerprint, purpose)` chain references as
        its `previous_transition_id` (round 90's head-pointer model)."""
        return event_content_id(self.envelope)

    def to_dict(self) -> dict:
        """JSON-serializable form for persistence — see
        `netbbs.link.node_identity`'s save/load."""
        return {
            "envelope": self.envelope,
            "signature": base64.b64encode(self.signature).decode("ascii"),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "KeyTransition":
        return cls(envelope=data["envelope"], signature=base64.b64decode(data["signature"]))


def build_key_transition(
    *,
    root: Identity,
    purpose: str,
    action: str,
    operational_key: nacl.signing.VerifyKey,
    previous_transition_id: str | None,
    created_at: str,
) -> KeyTransition:
    """
    Build and sign one `key_transition` event, per design doc round 110.

    `purpose` is `"signing"` or `"transport"` — round 89's two
    independently-rotatable operational-key chains. `action` is
    `"authorize"` (introduces a new operational key — covers both a
    node's initial bootstrap and a planned rotation) or `"revoke"`
    (marks a specific operational key invalid, round 89's "compromise
    response" case, without necessarily authorizing a replacement in the
    same record). `previous_transition_id` is `None` only for the first
    transition of a given `(subject, purpose)` pair — round 90's event-
    chain/head-pointer model applied to this object type, omitted
    entirely rather than stored as `null` (round 110 point 6).

    Always signed by `root` — never by an operational key (see
    `KeyTransition`'s own docstring).
    """
    if purpose not in _VALID_PURPOSES:
        raise EventError(f"invalid key_transition purpose: {purpose!r}")
    if action not in _VALID_ACTIONS:
        raise EventError(f"invalid key_transition action: {action!r}")

    payload = {
        "subject_fingerprint": root.fingerprint,
        "purpose": purpose,
        "action": action,
        "operational_key": base64.b64encode(bytes(operational_key)).decode("ascii"),
        "created_at": created_at,
    }
    if previous_transition_id is not None:
        payload["previous_transition_id"] = previous_transition_id

    envelope = build_envelope(KEY_TRANSITION_OBJECT_TYPE, payload)
    signature = root.sign(canonical_bytes(envelope))
    return KeyTransition(envelope=envelope, signature=signature)


def verify_key_transition(transition: KeyTransition, root_verify_key: nacl.signing.VerifyKey) -> bool:
    """Verify `transition`'s signature against the claimed root key —
    just the signature check; chain-walking/ordering validation is
    `netbbs.link.node_identity.resolve_current_operational_key`'s job,
    since it needs the *set* of transitions for a chain, not one alone."""
    return verify_signature(root_verify_key, canonical_bytes(transition.envelope), transition.signature)


@dataclass(frozen=True)
class EndpointDescriptor:
    """
    One signed `endpoint_descriptor` event (design doc §12, round 116):
    a node's own claim about how to reach it — a list of (protocol,
    address, port) tuples for a full peer, or an outgoing-only marker —
    self-authenticated by the node's own *current signing key* (round
    116), not its root key. Unlike `key_transition`, this is
    deliberately **not** a head-pointer chain: round 90's chain model
    exists for state whose *history* matters (audit, "what did this
    used to be"); a stale reachability claim only ever costs a failed
    connection attempt (design doc §12: "connecting to the wrong
    address just fails the handshake"), never a safety issue, so
    "whichever signed descriptor has the newest `created_at` wins" is
    sufficient — no chain-walking machinery needed to interpret one.
    """

    envelope: dict
    signature: bytes

    @property
    def payload(self) -> dict:
        return self.envelope["payload"]

    @property
    def content_id(self) -> str:
        return event_content_id(self.envelope)

    def to_dict(self) -> dict:
        return {
            "envelope": self.envelope,
            "signature": base64.b64encode(self.signature).decode("ascii"),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EndpointDescriptor":
        return cls(envelope=data["envelope"], signature=base64.b64decode(data["signature"]))


def build_endpoint_descriptor(
    *,
    signing_identity: Identity,
    subject_fingerprint: str,
    addresses: list[dict] | None,
    outgoing_only: bool,
    created_at: str,
) -> EndpointDescriptor:
    """
    Build and sign one `endpoint_descriptor` event, per design doc §12/
    round 116. `addresses` is a list of `{"protocol", "address", "port"}`
    dicts, tried in order by a peer (§12: "multiple simultaneous
    addresses... supported; peers try them in order") — required unless
    `outgoing_only` is true, matching §12's two deployment modes
    exactly: a full peer *must* publish where it can be reached, an
    outgoing-only node publishes nothing but the marker itself.

    Always signed by `signing_identity` — the subject's *current*
    signing key (round 89), never the root key directly (root only ever
    signs `key_transition`, per that round's own scope). `subject_
    fingerprint` is the subject's root fingerprint, included explicitly
    in the payload (not merely implied by "whoever signed this") so a
    verifier can cross-check it against whichever peer's transition
    chain it resolved the signing key from.
    """
    if not outgoing_only and not addresses:
        raise EventError("a full peer's endpoint_descriptor must include at least one address")

    payload = {
        "subject_fingerprint": subject_fingerprint,
        "outgoing_only": outgoing_only,
        "created_at": created_at,
    }
    if addresses:
        payload["addresses"] = addresses

    envelope = build_envelope(ENDPOINT_DESCRIPTOR_OBJECT_TYPE, payload)
    signature = signing_identity.sign(canonical_bytes(envelope))
    return EndpointDescriptor(envelope=envelope, signature=signature)


def verify_endpoint_descriptor(
    descriptor: EndpointDescriptor, signing_verify_key: nacl.signing.VerifyKey
) -> bool:
    """Verify `descriptor`'s signature against the claimed *current
    signing key* — resolving which key that currently is (walking the
    subject's `key_transition` chain) is the caller's job
    (`netbbs.link.protocol.handle_hello`), same division of
    responsibility as `verify_key_transition`."""
    return verify_signature(signing_verify_key, canonical_bytes(descriptor.envelope), descriptor.signature)
