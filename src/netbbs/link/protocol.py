"""
Transport-agnostic NetBBS Link handshake and gossip protocol (design doc
§11/§12/§13, rounds 116/124/125) — the first real Phase 3 protocol
slice: bootstrap first contact between two nodes (mutual endpoint-
descriptor exchange, §12) plus event gossip, originally exercised with
just `key_transition` (round 116; `endpoint_descriptor` is exchanged
directly in the hello bundle rather than gossiped as an ordinary event)
and extended in round 125 to also accept `board_genesis`/`board_post`
(design doc round 124) through the same `handle_events` dispatch.
Reuses round 89/90's key-lifecycle and event-envelope machinery for
verification rather than inventing a parallel path.

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
the round 116 sign-off note. Round 125 applies this exact same
boundary to the two new event types: a `board_genesis` is only
accepted directly from its own claimed origin (`origin_fingerprint ==
sender_fingerprint`), and a `board_post` is only accepted for a
`board_id` this node already holds a verified `board_genesis` for,
directly from the post's own claimed author's home node
(`home_node_fingerprint == sender_fingerprint`) — none of this
speculatively stores anything waiting for a genesis or hello that
might arrive later.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

import nacl.signing

from netbbs.identity.keys import fingerprint_from_verify_key
from netbbs.link.events import (
    BOARD_GENESIS_OBJECT_TYPE,
    BOARD_ORIGIN_TRANSFER_ACCEPTED_OBJECT_TYPE,
    BOARD_ORIGIN_TRANSFER_OFFER_OBJECT_TYPE,
    BOARD_POST_EDIT_OBJECT_TYPE,
    BOARD_POST_OBJECT_TYPE,
    KEY_TRANSITION_OBJECT_TYPE,
    LINK_MESSAGE_ACCEPTED_OBJECT_TYPE,
    LINK_MESSAGE_BOUNCED_OBJECT_TYPE,
    LINK_MESSAGE_OBJECT_TYPE,
    BoardGenesis,
    BoardOriginTransferAccepted,
    BoardOriginTransferOffer,
    BoardPost,
    BoardPostEdit,
    EndpointDescriptor,
    KeyTransition,
    LinkMessage,
    LinkMessageAccepted,
    LinkMessageBounced,
    RelayConsentRequest,
    RelayConsentResponse,
    build_endpoint_descriptor,
    verify_board_genesis,
    verify_board_origin_transfer_accepted,
    verify_board_origin_transfer_offer,
    verify_board_post,
    verify_board_post_edit,
    verify_endpoint_descriptor,
    verify_link_message,
    verify_link_message_accepted,
    verify_link_message_bounced,
    verify_relay_consent_request,
    verify_relay_consent_response,
)
from netbbs.link.node_identity import NodeIdentity, NodeIdentityError, resolve_current_operational_key

# Round 95: bounds on remotely-influenced peer-list state (design doc's
# own "every remotely influenced ... collection needs an explicit
# bound" principle). A single request carrying an absurd number of
# descriptors is refused outright, matching other malformed/abusive-
# input rejection in this module; the total candidate set this node
# will ever remember is separately capped so a peer can't grow it
# without limit across many requests either.
_MAX_PEER_LIST_ENTRIES_PER_REQUEST = 100
_MAX_CANDIDATE_DESCRIPTORS = 500

# Design doc §13.9 (issue #60's third operational slice): `handle_events`
# had no per-request cap at all, unlike `handle_peer_list` above -- a
# single push could carry an unbounded event list. Same "reject the
# whole batch" idiom as `_MAX_PEER_LIST_ENTRIES_PER_REQUEST`.
_MAX_EVENTS_PER_REQUEST = 200

# Design doc §13.9: `board_post`/`board_post_edit` had no size
# validation at all on receive, unlike a locally created post
# (`netbbs.boards.posts.MAX_SUBJECT_BYTES`/`MAX_BODY_BYTES`). Duplicated
# here at the same values rather than imported -- this module stays
# free of any `netbbs.boards` dependency, the same "duplicate one small
# piece of logic rather than reach across the module boundary" precedent
# `netbbs.link.mail._make_room_or_bounce` already documents for itself.
_MAX_BOARD_POST_SUBJECT_BYTES = 300
_MAX_BOARD_POST_BODY_BYTES = 200_000


class LinkProtocolError(Exception):
    """Raised for a hello or event message that fails verification: an
    inconsistent/forked/unverifiable key_transition chain, a descriptor
    signed by a key the chain doesn't currently authorize, a descriptor
    or event whose claimed subject doesn't match who actually sent it,
    an event from a peer with no completed hello on file, a board_post
    for a board_id with no verified board_genesis on file, a board_post
    author kind this node doesn't yet know how to verify (design doc
    round 124: only node_vouched_user is built), a board_genesis
    conflicting with a different one already on file for the same
    board_id, a board_post_edit for an unknown root post, a board_post_
    edit whose author doesn't match the root post's own author (design
    doc round 129: moderator edits aren't supported this round), or a
    board_post_edit whose previous_event_id doesn't match the current
    head of its chain (round 129: reordering is refused outright, the
    same push-and-retry recovery model round 122 already established
    for key_transition); a link_message not addressed to this node
    (design doc round 93: strictly point-to-point, never speculatively
    stored on behalf of a different intended recipient), a link_message
    whose sender doesn't match who actually sent it, or a link_message_
    accepted/link_message_bounced referencing a message this node never
    actually sent, or vouching for a recipient node other than whoever
    actually sent the acknowledgement."""


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
class PeerListMessage:
    """
    A bundle of `EndpointDescriptor`s shared between two already-
    completed peers (design doc round 95, §12's "signed peer-list
    exchange") — deliberately not a canonical `netbbs.link.events`
    envelope of its own: this is ephemeral discovery data ("addresses
    worth trying"), not durable state that needs content-addressing,
    dedup, or gossip-replay semantics the way a `board_post` does.

    Each individual descriptor inside is already self-signed by its own
    claimed subject (round 116) — nothing here adds an outer signature
    over the bundle, since a stale or malicious bundle only ever costs
    a failed connection attempt on the receiving end (the same "connecting
    to the wrong address just fails the handshake" property §12 already
    claims for endpoint advertisement generally), never a safety issue.
    """

    descriptors: tuple[EndpointDescriptor, ...]

    def to_dict(self) -> dict:
        return {"descriptors": [d.to_dict() for d in self.descriptors]}

    @classmethod
    def from_dict(cls, data: dict) -> "PeerListMessage":
        return cls(descriptors=tuple(EndpointDescriptor.from_dict(d) for d in data["descriptors"]))


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
    # Round 125: board_id -> the verified board_genesis on file for it.
    # A board_post is only ever accepted for a board_id already present
    # here (module docstring's "no relay from a stranger" boundary,
    # applied to boards).
    boards: dict[str, BoardGenesis] = field(default_factory=dict)
    # Round 129: root_post_id (a board_post's own content_id) -> its
    # verified edit chain, oldest first. Deliberately not a generic
    # reusable chain-walker (design doc round 129) -- a single linear
    # list per post, ordering enforced by "does previous_event_id match
    # the current head" alone, the same shape simpler than key_
    # transition's two-interleaved-purposes chain.
    post_edits: dict[str, tuple[BoardPostEdit, ...]] = field(default_factory=dict)
    # Round 94/issue #53: board_id -> the fingerprint currently
    # authoritative for it, once a board_origin_transfer_accepted has
    # been verified -- absent means "still the genesis's own origin,"
    # see current_board_origin.
    board_origin: dict[str, str] = field(default_factory=dict)
    # Round 94/issue #53: board_id -> the content_id a new lifecycle
    # event (an offer or an acceptance) must reference as its own
    # previous_event_id -- absent means "still genesis," see current_
    # board_lifecycle_head.
    board_lifecycle_head: dict[str, str] = field(default_factory=dict)
    # Round 94/issue #53: board_id -> its single outstanding, not-yet-
    # accepted board_origin_transfer_offer, if any -- at most one may be
    # in flight per board at a time (see BoardOriginTransferOffer's own
    # docstring for why this slice doesn't support more).
    pending_origin_transfers: dict[str, BoardOriginTransferOffer] = field(default_factory=dict)
    # Round 95: fingerprint -> an unverified endpoint descriptor learned
    # secondhand via peer-list exchange -- "worth trying," never
    # promoted to `peers` until a real hello with that fingerprint
    # actually completes. See `handle_peer_list`'s own docstring for why
    # nothing here is ever cryptographically checked at receipt time.
    candidate_descriptors: dict[str, EndpointDescriptor] = field(default_factory=dict)
    # Round 95/issue #58: relay_fingerprint -> the still-outstanding
    # RelayConsentRequest this node itself sent and hasn't yet gotten a
    # reply to -- self-origination bookkeeping the caller sets directly
    # before dialing out (`netbbs.link.transport.request_relay_consent`),
    # mirroring `pending_origin_transfers`' own "this node's own request,
    # never routed through handle_events" shape (see that field's own
    # docstring for the general pattern).
    pending_own_relay_requests: dict[str, RelayConsentRequest] = field(default_factory=dict)
    # Round 95/issue #58: requester_fingerprint -> when this node (acting
    # as the relay) agreed to serve it, once granted. Whether to grant is
    # a resource-cap/opt-out policy decision this pure/in-memory layer
    # has no config to make (see `handle_relay_consent_request`'s own
    # docstring) -- the caller applies the decision to this dict directly
    # after calling that method, the same "verify here, mutate there"
    # split `record_board_origin_change` already uses for a bystander
    # node's own board state.
    relaying_for: dict[str, str] = field(default_factory=dict)
    # Round 95/issue #58: relay_fingerprint -> when that candidate
    # accepted this node's own relay_consent_request, once granted --
    # what `netbbs.link.boards`-equivalent code for endpoint descriptors
    # (issue #58 task #23) reads to populate this node's own published
    # `relays` field.
    relays_serving_me: dict[str, str] = field(default_factory=dict)

    def build_hello(
        self, *, addresses: list[dict] | None, outgoing_only: bool, created_at: str
    ) -> HelloMessage:
        """Build this node's own hello bundle. `addresses`/
        `outgoing_only`/`created_at` are the caller's to supply (node
        network configuration and the current time are not this
        method's concern — see `tests/link_harness.py`'s `FakeClock`
        for how tests keep this deterministic). `relays` (round 95/
        issue #58) is **not** a caller-supplied parameter the way those
        three are -- unlike deployment config, `relays_serving_me` is
        already this node's own in-memory state (populated by
        `netbbs.link.transport.request_relay_consent`), so build_hello
        reads it directly rather than making every caller re-thread it
        through."""
        descriptor = build_endpoint_descriptor(
            signing_identity=self.identity.signing_key,
            subject_fingerprint=self.identity.fingerprint,
            addresses=addresses,
            outgoing_only=outgoing_only,
            created_at=created_at,
            relays=list(self.relays_serving_me.keys()) or None,
        )
        return HelloMessage(
            root_public_key=bytes(self.identity.root.verify_key),
            transitions=_signing_transitions(self.identity.transitions, self.identity.fingerprint),
            descriptor=descriptor,
        )

    def handle_hello(self, message: HelloMessage, *, max_peers: int | None = None) -> PeerRecord:
        """
        Verify and record an incoming hello. Raises `LinkProtocolError`
        if anything about the bundle doesn't check out. A hello from an
        already-known peer updates that peer's record only if its
        descriptor is newer (`created_at`) than what's currently on
        file — round 116's "latest signed descriptor wins" rule (see
        `EndpointDescriptor`'s own docstring) applied to a *repeated*
        hello, not just the first one.

        `max_peers` (design doc §13.9, issue #60's third operational
        slice): `self.peers` had no cap at all before this -- the
        mirror-image gap to `_MAX_CANDIDATE_DESCRIPTORS` above, since
        any node that completes a *real* hello (not just a claimed
        candidate) became a permanent peer unconditionally. `None`
        (the default) means unbounded, preserving every existing caller
        that doesn't pass it. Same admission idiom as `handle_peer_
        list`'s own candidate cap: a hello from an already-known peer
        (a refresh) is always accepted regardless of the count; only
        admitting a genuinely *new* fingerprint is refused once the cap
        is reached. Deliberately caller-supplied rather than a fixed
        module constant like `_MAX_CANDIDATE_DESCRIPTORS` -- unlike that
        internal bookkeeping cap, this one is meant to be a SysOp-
        tunable quota (`netbbs.net.nodeconfig.LinkConfig.max_peers`),
        matching issue #60's own "configurable with safe defaults"
        wording. Only threaded into the *inbound* hello path today
        (`netbbs.link.transport.LinkServer._handle_hello`/`_handle_
        relay_mailbox_pickup`) -- outbound dialing (`dial_hello`) is
        left unbounded by this specific cap, since it's already
        indirectly bounded by `_MAX_CANDIDATE_DESCRIPTORS` plus the
        operator's own deliberately small configured seed list, not an
        open admission surface a stranger controls the way inbound
        hellos are.
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

        if existing is None and max_peers is not None and len(self.peers) >= max_peers:
            raise LinkProtocolError(
                f"hello from {claimed_fingerprint} refused: already at this node's own "
                f"max_peers limit ({max_peers})"
            )

        record = PeerRecord(
            fingerprint=claimed_fingerprint,
            root_public_key=message.root_public_key,
            transitions=message.transitions,
            descriptor=message.descriptor,
        )
        self.peers[claimed_fingerprint] = record
        # Now a real, verified peer -- an unverified candidate entry for
        # the same fingerprint (round 95) is superseded, not left
        # sitting alongside the real thing.
        self.candidate_descriptors.pop(claimed_fingerprint, None)
        return record

    def build_peer_list(self) -> PeerListMessage:
        """This node's own currently-verified peers' endpoint
        descriptors, to share with a directly-connected peer (design
        doc round 95, §12's "signed peer-list exchange"). Only ever
        drawn from `self.peers` (each one verified via a real completed
        hello) -- never re-shares a candidate this node itself learned
        secondhand, so a claim's provenance never grows past one hop of
        "someone I've actually talked to vouches this address is worth
        trying.\""""
        return PeerListMessage(descriptors=tuple(peer.descriptor for peer in self.peers.values()))

    def handle_peer_list(self, sender_fingerprint: str, message: PeerListMessage) -> list[str]:
        """
        Record candidate addresses shared by `sender_fingerprint`, who
        must already be a completed peer (same "no relay from a
        stranger" boundary `handle_events` enforces). Returns the
        fingerprints newly recorded or refreshed, purely informational.

        **Nothing here is cryptographically verified against a resolved
        signing key, deliberately** -- a descriptor's own signature
        can't be checked without that subject's root key/transition
        chain, which this node doesn't have for a stranger yet (design
        doc round 95: "a weak prior worth trying, not trusting
        outright"). Real trust only ever happens once this node dials a
        candidate directly and completes its own hello with it, the
        same self-authenticating process any first contact already
        goes through.

        Skips: this node's own fingerprint; any fingerprint already a
        verified peer (nothing gained from a secondhand claim about
        someone already directly known -- `handle_hello` is the only
        path that ever populates `self.peers`); a stale descriptor
        (older `created_at` than a candidate already on file for the
        same fingerprint); and, once `_MAX_CANDIDATE_DESCRIPTORS` is
        reached, any fingerprint not already present (refreshing an
        existing candidate's own descriptor is still allowed past the
        cap, adding a brand new one is not).
        """
        if sender_fingerprint not in self.peers:
            raise LinkProtocolError(
                f"received a peer list from {sender_fingerprint}, which has no completed hello"
            )
        if len(message.descriptors) > _MAX_PEER_LIST_ENTRIES_PER_REQUEST:
            raise LinkProtocolError(
                f"peer list from {sender_fingerprint} carries "
                f"{len(message.descriptors)} descriptors, more than the "
                f"{_MAX_PEER_LIST_ENTRIES_PER_REQUEST} this node accepts in one request -- refusing"
            )

        recorded: list[str] = []
        for descriptor in message.descriptors:
            candidate_fingerprint = descriptor.payload.get("subject_fingerprint")
            if candidate_fingerprint is None:
                continue
            if candidate_fingerprint == self.identity.fingerprint:
                continue
            if candidate_fingerprint in self.peers:
                continue

            existing = self.candidate_descriptors.get(candidate_fingerprint)
            if existing is not None and descriptor.payload.get("created_at", "") <= existing.payload.get(
                "created_at", ""
            ):
                continue
            if existing is None and len(self.candidate_descriptors) >= _MAX_CANDIDATE_DESCRIPTORS:
                continue

            self.candidate_descriptors[candidate_fingerprint] = descriptor
            recorded.append(candidate_fingerprint)
        return recorded

    def handle_relay_consent_request(self, sender_fingerprint: str, request: RelayConsentRequest) -> None:
        """
        Verify an incoming `relay_consent_request` (design doc §12,
        round 95/issue #58) from `sender_fingerprint`, who must already
        be a completed peer -- the same "no relay from a stranger"
        boundary every other acceptance rule in this module applies,
        satisfied here by the mutual hello a candidate relay and a
        prospective requester must have already exchanged before either
        one calls this (round 116's hello is mutual by construction --
        see `handle_hello`).

        Raises `LinkProtocolError` if anything doesn't check out.
        **Deliberately does not decide accept/decline, and does not
        touch `relaying_for`** -- a resource-cap/opt-out check needs
        config this pure/in-memory layer doesn't have (module docstring:
        transport-agnostic, no idea about deployment). The caller
        (`netbbs.link.transport`'s `/relay-consent` route handler) makes
        that policy call and records its own outcome into `relaying_for`
        directly once this method returns without raising.
        """
        if sender_fingerprint not in self.peers:
            raise LinkProtocolError(
                f"received a relay_consent_request from {sender_fingerprint}, which has no "
                "completed hello -- refusing (no relay from a stranger yet)"
            )
        sender = self.peers[sender_fingerprint]

        if request.payload.get("requester_fingerprint") != sender_fingerprint:
            raise LinkProtocolError(
                f"{sender_fingerprint} sent a relay_consent_request claiming a different "
                f"requester_fingerprint ({request.payload.get('requester_fingerprint')!r}) -- refusing"
            )
        if request.payload.get("relay_fingerprint") != self.identity.fingerprint:
            raise LinkProtocolError(
                f"relay_consent_request from {sender_fingerprint} names a different relay "
                f"({request.payload.get('relay_fingerprint')!r}), not this node "
                f"({self.identity.fingerprint!r}) -- refusing"
            )

        signing_verify_key = self._resolve_sender_signing_key(sender, sender_fingerprint, "relay_consent_request")
        if not verify_relay_consent_request(request, signing_verify_key):
            raise LinkProtocolError(
                f"relay_consent_request from {sender_fingerprint} does not verify against its "
                "current signing key"
            )

    def handle_relay_consent_response(
        self, sender_fingerprint: str, response: RelayConsentResponse, *, original_request: RelayConsentRequest
    ) -> None:
        """
        Verify an incoming `relay_consent_response` from `sender_
        fingerprint` (the candidate relay this node itself asked),
        answering `original_request` -- the caller's own still-
        outstanding request it's tracking in `pending_own_relay_
        requests` (the same cross-check discipline `_resolve_own_link_
        message` applies to a message acknowledgement: a reply is only
        meaningful about something this node itself actually sent, not
        an arbitrary content_id a peer could otherwise name).

        Raises `LinkProtocolError` if anything doesn't check out.
        **Deliberately does not touch `relays_serving_me`** -- same
        "verify here, mutate there" split as `handle_relay_consent_
        request`; the caller applies `response.payload["accepted"]`
        after this returns without raising.
        """
        if sender_fingerprint not in self.peers:
            raise LinkProtocolError(
                f"received a relay_consent_response from {sender_fingerprint}, which has no "
                "completed hello -- refusing (no relay from a stranger yet)"
            )
        sender = self.peers[sender_fingerprint]

        if response.payload.get("relay_fingerprint") != sender_fingerprint:
            raise LinkProtocolError(
                f"{sender_fingerprint} sent a relay_consent_response claiming a different "
                f"relay_fingerprint ({response.payload.get('relay_fingerprint')!r}) -- refusing"
            )
        if response.payload.get("relay_fingerprint") != original_request.payload.get("relay_fingerprint"):
            # Catches a *different*, also-completed peer answering on
            # behalf of whoever the outstanding request actually named --
            # the content_id match alone doesn't rule this out, since
            # nothing above ties "who answered" back to "who was asked"
            # except this explicit cross-check.
            raise LinkProtocolError(
                f"{sender_fingerprint} answered a relay_consent_request that was addressed to "
                f"a different relay ({original_request.payload.get('relay_fingerprint')!r}) -- refusing"
            )
        if response.payload.get("requester_fingerprint") != self.identity.fingerprint:
            raise LinkProtocolError(
                f"relay_consent_response from {sender_fingerprint} names a different requester "
                f"({response.payload.get('requester_fingerprint')!r}), not this node "
                f"({self.identity.fingerprint!r}) -- refusing"
            )
        if response.payload.get("request_content_id") != original_request.content_id:
            raise LinkProtocolError(
                f"relay_consent_response from {sender_fingerprint} answers "
                f"{response.payload.get('request_content_id')!r}, not the outstanding request "
                f"({original_request.content_id!r}) -- refusing"
            )

        signing_verify_key = self._resolve_sender_signing_key(sender, sender_fingerprint, "relay_consent_response")
        if not verify_relay_consent_response(response, signing_verify_key):
            raise LinkProtocolError(
                f"relay_consent_response from {sender_fingerprint} does not verify against its "
                "current signing key"
            )

    def _resolve_sender_signing_key(self, sender: "PeerRecord", sender_fingerprint: str, kind: str) -> nacl.signing.VerifyKey:
        """Shared by the `board_genesis`/`board_post` branches below:
        resolve `sender`'s *current* signing key from its own tracked
        transition chain — the same verification shape `handle_hello`
        already uses for a descriptor, applied here to a gossiped
        event instead."""
        signing_key_b64 = resolve_current_operational_key(
            sender.transitions,
            root_verify_key=sender.root_verify_key,
            subject_fingerprint=sender_fingerprint,
            purpose="signing",
        )
        if signing_key_b64 is None:
            raise LinkProtocolError(f"rejected {kind} from {sender_fingerprint}: no currently-authorized signing key")
        return nacl.signing.VerifyKey(base64.b64decode(signing_key_b64))

    def _check_board_post_content_size(self, payload: dict, sender_fingerprint: str, kind: str) -> None:
        """Shared by the `board_post`/`board_post_edit` branches below
        (design doc §13.9, issue #60's third operational slice): neither
        had any size validation on receive before this, unlike a locally
        created post (`netbbs.boards.posts.create_post`'s own `MAX_
        SUBJECT_BYTES`/`MAX_BODY_BYTES` check) -- a peer could push an
        arbitrarily large signed post and this node would verify,
        accept, and persist the full envelope regardless of size."""
        subject_bytes = len(payload.get("subject", "").encode("utf-8"))
        if subject_bytes > _MAX_BOARD_POST_SUBJECT_BYTES:
            raise LinkProtocolError(
                f"{sender_fingerprint} sent a {kind} with a {subject_bytes}-byte subject, more "
                f"than the {_MAX_BOARD_POST_SUBJECT_BYTES} bytes this node accepts -- refusing"
            )
        body_bytes = len(payload.get("body", "").encode("utf-8"))
        if body_bytes > _MAX_BOARD_POST_BODY_BYTES:
            raise LinkProtocolError(
                f"{sender_fingerprint} sent a {kind} with a {body_bytes}-byte body, more than "
                f"the {_MAX_BOARD_POST_BODY_BYTES} bytes this node accepts -- refusing"
            )

    def _resolve_own_link_message(self, message_content_id: str | None) -> LinkMessage:
        """Shared by the `link_message_accepted`/`link_message_bounced`
        branches below: an acknowledgement is only meaningful about a
        `link_message` this node itself actually originated -- not an
        arbitrary content_id a peer could otherwise use to smuggle an
        acknowledgement for someone else's message past this node's own
        acceptance rules."""
        original_raw = self.events.get(message_content_id)
        if original_raw is None or original_raw["envelope"]["object_type"] != LINK_MESSAGE_OBJECT_TYPE:
            raise LinkProtocolError(
                f"received an acknowledgement for {message_content_id!r}, which is not a "
                "link_message this node knows about -- refusing"
            )
        original_message = LinkMessage.from_dict(original_raw)
        if original_message.payload.get("sender", {}).get("home_node_fingerprint") != self.identity.fingerprint:
            raise LinkProtocolError(
                f"received an acknowledgement for {message_content_id!r}, which this node "
                "did not originate -- refusing"
            )
        return original_message

    def current_board_origin(self, board_id: str) -> str:
        """The fingerprint currently authoritative for `board_id`
        (design doc §13, round 94/issue #53) -- `self.board_origin`'s
        override if a transfer has ever completed, else the board's own
        genesis claim. Mirrors `netbbs.link.boards.board_origin_
        fingerprint`'s exact same two-tier resolution, applied here to
        this node's in-memory state instead of a DB row -- both must
        agree, since the DB-side version is this same fact persisted."""
        return self.board_origin.get(board_id, self.boards[board_id].payload["origin_fingerprint"])

    def current_board_lifecycle_head(self, board_id: str) -> str:
        """The content_id a *new* lifecycle event for `board_id` (an
        offer or an acceptance) must reference as its own `previous_
        event_id` (design doc §13, round 94/issue #53) -- the latest
        accepted lifecycle event if one exists, else the board's own
        genesis. Mirrors `netbbs.link.boards._current_lifecycle_head`'s
        own reasoning, applied to this node's in-memory state."""
        return self.board_lifecycle_head.get(board_id, self.boards[board_id].content_id)

    def handle_events(self, sender_fingerprint: str, raw_events: list[dict]) -> list[str]:
        """
        Accept zero or more incoming signed events from a peer that has
        already completed a hello (see module docstring — a stranger's
        events are refused this round, not queued/relayed). Returns the
        content_ids of events newly accepted; already-seen ones are
        silently skipped (§7: transport-level dedup is a performance
        optimization, not a safety mechanism -- round 121 makes that
        true in practice, not just in intent for `key_transition`:
        idempotency for an already-applied one no longer depends solely
        on `known_event_ids` still holding the entry, see below).

        Nine recognized `object_type`s: `key_transition` (round 116),
        `board_genesis`/`board_post` (design doc round 124, wired up
        here in round 125/126), `board_post_edit` (design doc round
        129/130), `board_origin_transfer_offer`/`board_origin_transfer_
        accepted` (design doc round 94/issue #53), and `link_message`/
        `link_message_accepted`/`link_message_bounced` (design doc round
        93). `board_genesis`/`board_post` have no per-object chain to
        self-heal against (`board_post` is immutable content per round
        90, nothing to project beyond "does it exist"; `board_genesis`
        itself is still one-per-board, never resent as a candidate
        extension of anything) -- for both, `known_event_ids` dedup
        alone is what's built so far, which is already correct for
        content that's never resent as a *candidate extension* of
        anything. `board_post_edit` *is* a candidate extension (a single
        linear chain per root post, round 129) and gets the same chain-
        membership self-heal `key_transition` has had since round 121,
        just against `self.post_edits` instead of a peer's own
        `transitions`. `board_origin_transfer_offer`/`_accepted` extend
        a *different* per-board chain (`board_lifecycle_head`, starting
        from the board's own genesis) with the same "does previous_
        event_id match the current head" discipline, plus their own
        mutual-consent rule: an offer is only accepted directly from a
        board's own *current* origin (`current_board_origin`, not
        merely `board_genesis`'s original claim -- a board can change
        hands more than once), and an acceptance is only accepted
        directly from that specific offer's own named new origin, with
        at most one outstanding offer tolerated per board at a time
        (see `BoardOriginTransferOffer`'s own docstring for why).

        `link_message`/`link_message_accepted`/`link_message_bounced`
        are immutable, single-shot content like `board_post` -- `known_
        event_ids` dedup alone, no chain. Their acceptance rule is
        stricter than any board event's, though: a `link_message` is
        accepted only when `recipient.home_node_fingerprint` names
        *this* node specifically (round 93's point-to-point framing,
        never "anyone carrying this board"); an accepted/bounced
        acknowledgement is accepted only when it references a
        `link_message` this node itself actually originated, from the
        node that message's own recipient names. Mailbox delivery,
        ciphertext decryption, and building the reply acknowledgement
        are deliberately **not** done here -- see this module's own
        docstring on why this layer stays pure/synchronous/in-memory;
        that's `netbbs.link.mail`'s job, called by whichever transport
        handler persists what `handle_events` accepted (the same
        division `LinkServer._handle_events` already applies to board
        events).
        """
        sender = self.peers.get(sender_fingerprint)
        if sender is None:
            raise LinkProtocolError(f"received events from {sender_fingerprint}, which has no completed hello")
        if len(raw_events) > _MAX_EVENTS_PER_REQUEST:
            raise LinkProtocolError(
                f"{sender_fingerprint} sent {len(raw_events)} events in one request, more than "
                f"the {_MAX_EVENTS_PER_REQUEST} this node accepts in one request -- refusing "
                "(design doc §13.9: a genuine backlog still drains over several passes)"
            )

        accepted: list[str] = []
        for raw in raw_events:
            object_type = raw["envelope"]["object_type"]

            if object_type == KEY_TRANSITION_OBJECT_TYPE:
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

            elif object_type == BOARD_GENESIS_OBJECT_TYPE:
                genesis = BoardGenesis.from_dict(raw)
                if genesis.content_id in self.known_event_ids:
                    continue

                origin_fingerprint = genesis.payload.get("origin_fingerprint")
                if origin_fingerprint != sender_fingerprint:
                    raise LinkProtocolError(
                        f"{sender_fingerprint} sent a board_genesis for a different origin "
                        f"({origin_fingerprint!r}) -- refusing (no relay from a stranger yet)"
                    )

                board_id = genesis.payload["board_id"]
                existing_genesis = self.boards.get(board_id)
                if existing_genesis is not None and existing_genesis.content_id != genesis.content_id:
                    raise LinkProtocolError(
                        f"received a conflicting board_genesis for board_id {board_id!r} -- a "
                        "different genesis is already on file for it"
                    )

                signing_verify_key = self._resolve_sender_signing_key(sender, sender_fingerprint, "board_genesis")
                if not verify_board_genesis(genesis, signing_verify_key):
                    raise LinkProtocolError(
                        f"board_genesis from {sender_fingerprint} does not verify against its "
                        "current signing key"
                    )

                self.boards[board_id] = genesis
                self.known_event_ids.add(genesis.content_id)
                self.events[genesis.content_id] = raw
                accepted.append(genesis.content_id)

            elif object_type == BOARD_POST_OBJECT_TYPE:
                post = BoardPost.from_dict(raw)
                if post.content_id in self.known_event_ids:
                    continue

                board_id = post.payload.get("board_id")
                if board_id not in self.boards:
                    raise LinkProtocolError(
                        f"received a board_post for board_id {board_id!r}, which has no verified "
                        "board_genesis on file -- refusing (no relay from a stranger yet)"
                    )
                self._check_board_post_content_size(post.payload, sender_fingerprint, "board_post")

                author = post.payload.get("author", {})
                author_kind = author.get("kind")
                if author_kind != "node_vouched_user":
                    raise LinkProtocolError(
                        f"board_post author kind {author_kind!r} is not yet supported (design doc "
                        "round 124: only node_vouched_user is built)"
                    )
                home_node_fingerprint = author.get("home_node_fingerprint")
                if home_node_fingerprint != sender_fingerprint:
                    raise LinkProtocolError(
                        f"{sender_fingerprint} sent a board_post vouching for a different home "
                        f"node ({home_node_fingerprint!r}) -- refusing (no relay from a stranger yet)"
                    )

                signing_verify_key = self._resolve_sender_signing_key(sender, sender_fingerprint, "board_post")
                if not verify_board_post(post, signing_verify_key):
                    raise LinkProtocolError(
                        f"board_post from {sender_fingerprint} does not verify against its "
                        "current signing key"
                    )

                self.known_event_ids.add(post.content_id)
                self.events[post.content_id] = raw
                accepted.append(post.content_id)

            elif object_type == BOARD_POST_EDIT_OBJECT_TYPE:
                edit = BoardPostEdit.from_dict(raw)
                if edit.content_id in self.known_event_ids:
                    continue

                root_post_id = edit.payload.get("root_post_id")
                root_raw = self.events.get(root_post_id)
                if root_raw is None:
                    raise LinkProtocolError(
                        f"received a board_post_edit for root post {root_post_id!r}, which is "
                        "unknown -- refusing (no relay from a stranger yet)"
                    )
                root_post = BoardPost.from_dict(root_raw)
                self._check_board_post_content_size(edit.payload, sender_fingerprint, "board_post_edit")

                edit_author = edit.payload.get("author")
                if edit_author != root_post.payload.get("author"):
                    raise LinkProtocolError(
                        f"board_post_edit for root post {root_post_id!r} has an author that "
                        "doesn't match the root post's own author -- moderator edits aren't "
                        "supported this round (design doc round 129)"
                    )
                home_node_fingerprint = edit_author.get("home_node_fingerprint")
                if home_node_fingerprint != sender_fingerprint:
                    raise LinkProtocolError(
                        f"{sender_fingerprint} sent a board_post_edit vouching for a different "
                        f"home node ({home_node_fingerprint!r}) -- refusing (no relay from a "
                        "stranger yet)"
                    )

                existing_chain = self.post_edits.get(root_post_id, ())
                if any(existing.content_id == edit.content_id for existing in existing_chain):
                    # Exact resend of an already-integrated edit -- round 121's lesson applied
                    # here too: a safe no-op, self-healing known_event_ids, not a fork attempt.
                    self.known_event_ids.add(edit.content_id)
                    self.events.setdefault(edit.content_id, raw)
                    continue

                current_head = existing_chain[-1].content_id if existing_chain else root_post_id
                if edit.payload.get("previous_event_id") != current_head:
                    raise LinkProtocolError(
                        f"board_post_edit for root post {root_post_id!r} does not extend the "
                        f"current head ({current_head!r}) -- refusing (round 129: reordering "
                        "isn't tolerated, a full resend recovers, same model as key_transition)"
                    )

                signing_verify_key = self._resolve_sender_signing_key(
                    sender, sender_fingerprint, "board_post_edit"
                )
                if not verify_board_post_edit(edit, signing_verify_key):
                    raise LinkProtocolError(
                        f"board_post_edit from {sender_fingerprint} does not verify against its "
                        "current signing key"
                    )

                self.post_edits[root_post_id] = existing_chain + (edit,)
                self.known_event_ids.add(edit.content_id)
                self.events[edit.content_id] = raw
                accepted.append(edit.content_id)

            elif object_type == BOARD_ORIGIN_TRANSFER_OFFER_OBJECT_TYPE:
                offer = BoardOriginTransferOffer.from_dict(raw)
                if offer.content_id in self.known_event_ids:
                    continue

                board_id = offer.payload.get("board_id")
                if board_id not in self.boards:
                    raise LinkProtocolError(
                        f"received a board_origin_transfer_offer for board_id {board_id!r}, "
                        "which has no verified board_genesis on file -- refusing (no relay "
                        "from a stranger yet)"
                    )

                current_origin = self.current_board_origin(board_id)
                if sender_fingerprint != current_origin:
                    raise LinkProtocolError(
                        f"{sender_fingerprint} sent a board_origin_transfer_offer for board_id "
                        f"{board_id!r}, but is not its current origin ({current_origin!r}) -- "
                        "refusing (no relay from a stranger yet)"
                    )
                old_origin_fingerprint = offer.payload.get("old_origin_fingerprint")
                if old_origin_fingerprint != current_origin:
                    raise LinkProtocolError(
                        f"board_origin_transfer_offer for board_id {board_id!r} claims an "
                        f"old_origin_fingerprint ({old_origin_fingerprint!r}) that doesn't "
                        f"match its actual current origin ({current_origin!r})"
                    )
                if board_id in self.pending_origin_transfers:
                    raise LinkProtocolError(
                        f"board_id {board_id!r} already has an outstanding, unaccepted "
                        "origin-transfer offer -- at most one may be in flight at a time"
                    )

                current_head = self.current_board_lifecycle_head(board_id)
                if offer.payload.get("previous_event_id") != current_head:
                    raise LinkProtocolError(
                        f"board_origin_transfer_offer for board_id {board_id!r} does not "
                        f"extend the current lifecycle head ({current_head!r})"
                    )

                signing_verify_key = self._resolve_sender_signing_key(
                    sender, sender_fingerprint, "board_origin_transfer_offer"
                )
                if not verify_board_origin_transfer_offer(offer, signing_verify_key):
                    raise LinkProtocolError(
                        f"board_origin_transfer_offer from {sender_fingerprint} does not "
                        "verify against its current signing key"
                    )

                self.pending_origin_transfers[board_id] = offer
                self.board_lifecycle_head[board_id] = offer.content_id
                self.known_event_ids.add(offer.content_id)
                self.events[offer.content_id] = raw
                accepted.append(offer.content_id)

            elif object_type == BOARD_ORIGIN_TRANSFER_ACCEPTED_OBJECT_TYPE:
                transfer_accepted = BoardOriginTransferAccepted.from_dict(raw)
                if transfer_accepted.content_id in self.known_event_ids:
                    continue

                board_id = transfer_accepted.payload.get("board_id")
                offer = self.pending_origin_transfers.get(board_id)
                if offer is None:
                    raise LinkProtocolError(
                        f"received a board_origin_transfer_accepted for board_id {board_id!r}, "
                        "which has no outstanding offer on file -- refusing (no relay from a "
                        "stranger yet)"
                    )
                if transfer_accepted.payload.get("previous_event_id") != offer.content_id:
                    raise LinkProtocolError(
                        f"board_origin_transfer_accepted for board_id {board_id!r} does not "
                        f"reference the outstanding offer ({offer.content_id!r})"
                    )

                new_origin_fingerprint = offer.payload.get("new_origin_fingerprint")
                if transfer_accepted.payload.get("new_origin_fingerprint") != new_origin_fingerprint:
                    raise LinkProtocolError(
                        f"board_origin_transfer_accepted for board_id {board_id!r} names a "
                        "new_origin_fingerprint that doesn't match the offer it's accepting"
                    )
                if sender_fingerprint != new_origin_fingerprint:
                    raise LinkProtocolError(
                        f"{sender_fingerprint} sent a board_origin_transfer_accepted for "
                        f"board_id {board_id!r}, but is not the offer's named new origin "
                        f"({new_origin_fingerprint!r}) -- refusing (no relay from a stranger yet)"
                    )

                signing_verify_key = self._resolve_sender_signing_key(
                    sender, sender_fingerprint, "board_origin_transfer_accepted"
                )
                if not verify_board_origin_transfer_accepted(transfer_accepted, signing_verify_key):
                    raise LinkProtocolError(
                        f"board_origin_transfer_accepted from {sender_fingerprint} does not "
                        "verify against its current signing key"
                    )

                self.board_origin[board_id] = new_origin_fingerprint
                self.board_lifecycle_head[board_id] = transfer_accepted.content_id
                del self.pending_origin_transfers[board_id]
                self.known_event_ids.add(transfer_accepted.content_id)
                self.events[transfer_accepted.content_id] = raw
                accepted.append(transfer_accepted.content_id)

            elif object_type == LINK_MESSAGE_OBJECT_TYPE:
                message = LinkMessage.from_dict(raw)
                if message.content_id in self.known_event_ids:
                    continue

                recipient = message.payload.get("recipient", {})
                if recipient.get("home_node_fingerprint") != self.identity.fingerprint:
                    raise LinkProtocolError(
                        f"received a link_message addressed to "
                        f"{recipient.get('home_node_fingerprint')!r}, not this node "
                        f"({self.identity.fingerprint!r}) -- refusing (round 93: strictly "
                        "point-to-point, no relay on behalf of a different recipient)"
                    )

                sender_info = message.payload.get("sender", {})
                sender_kind = sender_info.get("kind")
                if sender_kind != "node_vouched_user":
                    raise LinkProtocolError(
                        f"link_message sender kind {sender_kind!r} is not yet supported "
                        "(design doc round 93: only node_vouched_user is built)"
                    )
                home_node_fingerprint = sender_info.get("home_node_fingerprint")
                if home_node_fingerprint != sender_fingerprint:
                    raise LinkProtocolError(
                        f"{sender_fingerprint} sent a link_message vouching for a different "
                        f"home node ({home_node_fingerprint!r}) -- refusing (no relay from a "
                        "stranger yet)"
                    )

                signing_verify_key = self._resolve_sender_signing_key(sender, sender_fingerprint, "link_message")
                if not verify_link_message(message, signing_verify_key):
                    raise LinkProtocolError(
                        f"link_message from {sender_fingerprint} does not verify against its "
                        "current signing key"
                    )

                self.known_event_ids.add(message.content_id)
                self.events[message.content_id] = raw
                accepted.append(message.content_id)

            elif object_type == LINK_MESSAGE_ACCEPTED_OBJECT_TYPE:
                accepted_ack = LinkMessageAccepted.from_dict(raw)
                if accepted_ack.content_id in self.known_event_ids:
                    continue

                message_content_id = accepted_ack.payload.get("message_content_id")
                original_message = self._resolve_own_link_message(message_content_id)

                recipient_node_fingerprint = accepted_ack.payload.get("recipient_node_fingerprint")
                if recipient_node_fingerprint != sender_fingerprint:
                    raise LinkProtocolError(
                        f"{sender_fingerprint} sent a link_message_accepted vouching for a "
                        f"different recipient node ({recipient_node_fingerprint!r}) -- refusing"
                    )
                expected_recipient = original_message.payload.get("recipient", {}).get("home_node_fingerprint")
                if sender_fingerprint != expected_recipient:
                    raise LinkProtocolError(
                        f"{sender_fingerprint} sent a link_message_accepted for "
                        f"{message_content_id!r}, but that message was addressed to a "
                        f"different recipient node ({expected_recipient!r}) -- refusing"
                    )

                signing_verify_key = self._resolve_sender_signing_key(
                    sender, sender_fingerprint, "link_message_accepted"
                )
                if not verify_link_message_accepted(accepted_ack, signing_verify_key):
                    raise LinkProtocolError(
                        f"link_message_accepted from {sender_fingerprint} does not verify "
                        "against its current signing key"
                    )

                self.known_event_ids.add(accepted_ack.content_id)
                self.events[accepted_ack.content_id] = raw
                accepted.append(accepted_ack.content_id)

            elif object_type == LINK_MESSAGE_BOUNCED_OBJECT_TYPE:
                bounced = LinkMessageBounced.from_dict(raw)
                if bounced.content_id in self.known_event_ids:
                    continue

                message_content_id = bounced.payload.get("message_content_id")
                original_message = self._resolve_own_link_message(message_content_id)

                recipient_node_fingerprint = bounced.payload.get("recipient_node_fingerprint")
                if recipient_node_fingerprint != sender_fingerprint:
                    raise LinkProtocolError(
                        f"{sender_fingerprint} sent a link_message_bounced vouching for a "
                        f"different recipient node ({recipient_node_fingerprint!r}) -- refusing"
                    )
                expected_recipient = original_message.payload.get("recipient", {}).get("home_node_fingerprint")
                if sender_fingerprint != expected_recipient:
                    raise LinkProtocolError(
                        f"{sender_fingerprint} sent a link_message_bounced for "
                        f"{message_content_id!r}, but that message was addressed to a "
                        f"different recipient node ({expected_recipient!r}) -- refusing"
                    )

                signing_verify_key = self._resolve_sender_signing_key(
                    sender, sender_fingerprint, "link_message_bounced"
                )
                if not verify_link_message_bounced(bounced, signing_verify_key):
                    raise LinkProtocolError(
                        f"link_message_bounced from {sender_fingerprint} does not verify "
                        "against its current signing key"
                    )

                self.known_event_ids.add(bounced.content_id)
                self.events[bounced.content_id] = raw
                accepted.append(bounced.content_id)

            else:
                raise LinkProtocolError(f"unrecognized event object_type: {object_type!r}")

        return accepted
