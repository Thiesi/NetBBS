"""
Transport-agnostic NetBBS Link handshake and gossip protocol (design doc
§11/§12, round 116) — the first real Phase 3 protocol slice: bootstrap
first contact between two nodes (mutual endpoint-descriptor exchange,
§12) plus event gossip, currently exercised with `key_transition` (the
one event type that existed before this round; `endpoint_descriptor`,
this round's other new type, is exchanged directly in the hello bundle
rather than gossiped as an ordinary event). Reuses round 89/90's
key-lifecycle and event-envelope machinery for verification rather than
inventing a parallel path.

Deliberately has no idea how a message actually reaches its peer —
`LinkNode.build_hello()`/`handle_hello()`/`handle_events()` operate on
plain in-memory messages, never sockets or HTTP requests, so this
module is fully testable against `tests/link_harness.py`'s
`ScriptedTransport` with no real network call anywhere. A real
aiohttp-based client/server sits *outside* this module, translating
"send this message to this peer" into an actual HTTP POST — not built
this round; see design doc round 116 sign-off note for why the scope
stopped here (this round is "does the handshake/gossip logic work,"
not yet "does it work over a real wire").

**Message-passing, not request/response** — a deliberate departure from
this round's own earlier request/response-flavored sketch, made before
any code was written, once the existing test harness was checked
against it. Every interaction here is "I received a message, here is
what I now want to send in reply" rather than a blocking call that must
return its answer inline. This is required, not just stylistic: §7's
store-and-forward promise ("a node offline for days/weeks... a
returning node just resumes gossip and catches up") is incompatible
with a model where a reply must arrive on the same call, and it maps
directly onto `ScriptedTransport`'s existing fire-and-forget
`send()`/`deliver()` shape with no adaptation needed. A real HTTP
transport adapter can still implement "send" as a POST whose response
body happens to carry the reply promptly when the peer is online —
that's an implementation detail this layer does not need to know
about.

**This first slice deliberately does not accept events from a stranger**
(`handle_events` requires a completed `handle_hello` for that sender
first) — real flood-fill gossip (an event arriving via relay from a
peer you've never spoken to directly) is later scope, not attempted
here. Persistent on-disk event/dedup storage (§7: "persistent seen-
event table, not Bloom filters") is also not attempted this round —
`LinkNode` keeps its peer table and seen-event set in memory only; see
the round 116 sign-off note.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

import nacl.signing

from netbbs.identity.keys import fingerprint_from_verify_key
from netbbs.link.events import (
    KEY_TRANSITION_OBJECT_TYPE,
    EndpointDescriptor,
    KeyTransition,
    build_endpoint_descriptor,
    verify_endpoint_descriptor,
)
from netbbs.link.node_identity import NodeIdentity, NodeIdentityError, resolve_current_operational_key


class LinkProtocolError(Exception):
    """Raised for a hello or event message that fails verification: an
    inconsistent/forked/unverifiable key_transition chain, a descriptor
    signed by a key the chain doesn't currently authorize, a descriptor
    or event whose claimed subject doesn't match who actually sent it,
    or an event from a peer with no completed hello on file."""


def _signing_transitions(transitions: tuple[KeyTransition, ...], fingerprint: str) -> tuple[KeyTransition, ...]:
    """This node's own `"signing"`-purpose transition history — what a
    hello bundle includes (round 116: the `"transport"`-purpose chain is
    Noise's own concern, §11, not this handshake's)."""
    return tuple(
        t
        for t in transitions
        if t.payload.get("subject_fingerprint") == fingerprint and t.payload.get("purpose") == "signing"
    )


@dataclass(frozen=True)
class HelloMessage:
    """
    First-contact bundle (design doc round 116): everything a receiver
    needs to independently verify who's saying hello, with zero prior
    state required — the sender's root public key, its complete
    `"signing"`-purpose transition history (enough to resolve which
    operational key is currently authorized), and a descriptor signed
    by that current key.

    Self-authenticating by construction, the same property §12 already
    claims for endpoint advertisement generally: a peer that didn't
    actually hold the claimed root's private key could not have
    produced a transitions chain that both verifies against that root
    *and* whose resolved current signing key's signature matches the
    descriptor — there is nothing else in this bundle to forge.
    """

    root_public_key: bytes
    transitions: tuple[KeyTransition, ...]
    descriptor: EndpointDescriptor

    def to_dict(self) -> dict:
        return {
            "root_public_key": base64.b64encode(self.root_public_key).decode("ascii"),
            "transitions": [t.to_dict() for t in self.transitions],
            "descriptor": self.descriptor.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "HelloMessage":
        return cls(
            root_public_key=base64.b64decode(data["root_public_key"]),
            transitions=tuple(KeyTransition.from_dict(t) for t in data["transitions"]),
            descriptor=EndpointDescriptor.from_dict(data["descriptor"]),
        )


@dataclass
class PeerRecord:
    """What this node has learned about one peer via a completed hello
    exchange — enough to independently verify any further signed
    message (events, a refreshed descriptor) that peer sends, without
    re-deriving it from scratch each time. `transitions` grows as
    `handle_events` accepts further `key_transition`s from this peer,
    so later messages are checked against its *current* known state,
    not just what the original hello contained."""

    fingerprint: str
    root_public_key: bytes
    transitions: tuple[KeyTransition, ...]
    descriptor: EndpointDescriptor

    @property
    def root_verify_key(self) -> nacl.signing.VerifyKey:
        return nacl.signing.VerifyKey(self.root_public_key)


@dataclass
class LinkNode:
    """
    This node's own Link protocol state: its identity, what it's
    learned about its peers, and which events it's already seen.
    Transport-agnostic (see module docstring) — a caller wires this up
    to however messages actually travel.
    """

    identity: NodeIdentity
    peers: dict[str, PeerRecord] = field(default_factory=dict)
    known_event_ids: set[str] = field(default_factory=set)
    events: dict[str, dict] = field(default_factory=dict)

    def build_hello(
        self, *, addresses: list[dict] | None, outgoing_only: bool, created_at: str
    ) -> HelloMessage:
        """Build this node's own hello bundle. `addresses`/
        `outgoing_only`/`created_at` are the caller's to supply (node
        network configuration and the current time are not this
        method's concern — see `tests/link_harness.py`'s `FakeClock`
        for how tests keep this deterministic)."""
        descriptor = build_endpoint_descriptor(
            signing_identity=self.identity.signing_key,
            subject_fingerprint=self.identity.fingerprint,
            addresses=addresses,
            outgoing_only=outgoing_only,
            created_at=created_at,
        )
        return HelloMessage(
            root_public_key=bytes(self.identity.root.verify_key),
            transitions=_signing_transitions(self.identity.transitions, self.identity.fingerprint),
            descriptor=descriptor,
        )

    def handle_hello(self, message: HelloMessage) -> PeerRecord:
        """
        Verify and record an incoming hello. Raises `LinkProtocolError`
        if anything about the bundle doesn't check out. A hello from an
        already-known peer updates that peer's record only if its
        descriptor is newer (`created_at`) than what's currently on
        file — round 116's "latest signed descriptor wins" rule (see
        `EndpointDescriptor`'s own docstring) applied to a *repeated*
        hello, not just the first one.
        """
        root_verify_key = nacl.signing.VerifyKey(message.root_public_key)
        claimed_fingerprint = fingerprint_from_verify_key(root_verify_key)

        try:
            current_signing_key_b64 = resolve_current_operational_key(
                message.transitions,
                root_verify_key=root_verify_key,
                subject_fingerprint=claimed_fingerprint,
                purpose="signing",
            )
        except NodeIdentityError as exc:
            raise LinkProtocolError(f"hello from {claimed_fingerprint} has an unverifiable transition chain: {exc}") from exc
        if current_signing_key_b64 is None:
            raise LinkProtocolError(f"hello from {claimed_fingerprint} has no currently-authorized signing key")

        signing_verify_key = nacl.signing.VerifyKey(base64.b64decode(current_signing_key_b64))
        if not verify_endpoint_descriptor(message.descriptor, signing_verify_key):
            raise LinkProtocolError(
                f"hello from {claimed_fingerprint}'s descriptor does not verify against its "
                "current signing key"
            )
        if message.descriptor.payload.get("subject_fingerprint") != claimed_fingerprint:
            raise LinkProtocolError(
                f"hello claiming to be {claimed_fingerprint} carries a descriptor for a "
                f"different subject ({message.descriptor.payload.get('subject_fingerprint')!r})"
            )

        existing = self.peers.get(claimed_fingerprint)
        if existing is not None and message.descriptor.payload["created_at"] < existing.descriptor.payload["created_at"]:
            return existing  # stale hello -- keep what's on file, not an error

        record = PeerRecord(
            fingerprint=claimed_fingerprint,
            root_public_key=message.root_public_key,
            transitions=message.transitions,
            descriptor=message.descriptor,
        )
        self.peers[claimed_fingerprint] = record
        return record

    def handle_events(self, sender_fingerprint: str, raw_events: list[dict]) -> list[str]:
        """
        Accept zero or more incoming signed events from a peer that has
        already completed a hello (see module docstring — a stranger's
        events are refused this round, not queued/relayed). Returns the
        content_ids of events newly accepted; already-seen ones are
        silently skipped (§7: transport-level dedup is a performance
        optimization, not a safety mechanism -- round 121 makes that
        true in practice, not just in intent: idempotency for an
        already-applied `key_transition` no longer depends solely on
        `known_event_ids` still holding the entry, see below).

        The only recognized `object_type` so far is `key_transition`
        (round 116) — accepting one also extends the sending peer's own
        tracked `transitions`, so a later transition building on it
        verifies correctly against this node's up-to-date view of that
        peer's chain, not a stale snapshot from the original hello.
        """
        sender = self.peers.get(sender_fingerprint)
        if sender is None:
            raise LinkProtocolError(f"received events from {sender_fingerprint}, which has no completed hello")

        accepted: list[str] = []
        for raw in raw_events:
            object_type = raw["envelope"]["object_type"]
            if object_type != KEY_TRANSITION_OBJECT_TYPE:
                raise LinkProtocolError(f"unrecognized event object_type: {object_type!r}")

            transition = KeyTransition.from_dict(raw)
            if transition.content_id in self.known_event_ids:
                continue

            if any(existing.content_id == transition.content_id for existing in sender.transitions):
                # Round 121: already integrated into sender's own chain
                # (permanent, never-purged key-lifecycle state, round 89)
                # even though known_event_ids doesn't currently have it
                # -- a legitimate resend (round 119's own "push every
                # transition every pass," or a future purged-then-resent
                # dedup entry), not a fork attempt: a genuine fork
                # carries a *different* content_id claiming the same
                # previous_transition_id, so it never matches here and
                # still reaches -- and is still rejected by -- the
                # resolve_current_operational_key check below. Self-
                # heals the fast-path cache from the authoritative chain
                # state rather than re-verifying from scratch.
                self.known_event_ids.add(transition.content_id)
                self.events.setdefault(transition.content_id, raw)
                continue

            if transition.payload.get("subject_fingerprint") != sender_fingerprint:
                raise LinkProtocolError(
                    f"{sender_fingerprint} sent a key_transition for a different subject "
                    f"({transition.payload.get('subject_fingerprint')!r}) -- refusing"
                )

            candidate_transitions = sender.transitions + (transition,)
            try:
                resolve_current_operational_key(
                    candidate_transitions,
                    root_verify_key=sender.root_verify_key,
                    subject_fingerprint=sender_fingerprint,
                    purpose=transition.payload["purpose"],
                )
            except NodeIdentityError as exc:
                raise LinkProtocolError(f"rejected key_transition from {sender_fingerprint}: {exc}") from exc

            sender.transitions = candidate_transitions
            self.known_event_ids.add(transition.content_id)
            self.events[transition.content_id] = raw
            accepted.append(transition.content_id)

        return accepted
