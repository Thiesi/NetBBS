"""
Transport-agnostic NetBBS Link handshake and gossip protocol (design doc
§11/§12/§13) — bootstrap first contact between two nodes (mutual
endpoint-descriptor exchange, §12) plus event gossip across
`key_transition`, `board_genesis`/`board_post`/`board_post_edit`, and
`link_message`/`link_message_accepted` event types, all through the same
`handle_events` dispatch. Reuses the key-lifecycle and event-envelope
machinery (`netbbs.link.node_identity`/`netbbs.link.events`) for
verification rather than inventing a parallel path.

Deliberately has no idea how a message actually reaches its peer —
`LinkNode.build_hello()`/`handle_hello()`/`handle_events()` operate on
plain in-memory messages, never sockets or HTTP requests, so this
module is fully testable against `tests/link_harness.py`'s
`ScriptedTransport` with no real network call anywhere. The real
`aiohttp`-based client/server (`netbbs.link.transport`) sits *outside*
this module, translating "send this message to this peer" into an
actual HTTP POST.

**Message-passing, not request/response.** Every interaction here is "I
received a message, here is what I now want to send in reply" rather
than a blocking call that must return its answer inline. This is
required, not just stylistic: §7's store-and-forward promise ("a node
offline for days/weeks... a returning node just resumes gossip and
catches up") is incompatible with a model where a reply must arrive on
the same call, and it maps directly onto `ScriptedTransport`'s existing
fire-and-forget `send()`/`deliver()` shape with no adaptation needed. A
real HTTP transport adapter can still implement "send" as a POST whose
response body happens to carry the reply promptly when the peer is
online — that's an implementation detail this layer does not need to
know about.

**This protocol does not accept events from a stranger**
(`handle_events` requires a completed `handle_hello` for that sender
first) — real flood-fill gossip (an event arriving via relay from a
peer you've never spoken to directly) is not implemented. `LinkNode`
itself keeps its live peer table and seen-event set in memory only;
on-disk persistence and restart reconstruction are `netbbs.link.
store`'s responsibility, not this module's — see that module for how a
restarted node rebuilds this same in-memory state. The same "only from
a directly verified sender" boundary applies to every event type: a
`board_genesis` is only accepted directly from its own claimed origin
(`origin_fingerprint == sender_fingerprint`), and a `board_post` is
only accepted for a `board_id` this node already holds a verified
`board_genesis` for, directly from the post's own claimed author's home
node (`home_node_fingerprint == sender_fingerprint`) — none of this
speculatively stores anything waiting for a genesis or hello that
might arrive later.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

import nacl.signing

from netbbs.boards.limits import MAX_BODY_BYTES as _MAX_BOARD_POST_BODY_BYTES
from netbbs.boards.limits import MAX_SUBJECT_BYTES as _MAX_BOARD_POST_SUBJECT_BYTES
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
    NETBBS_PROTOCOL_VERSION,
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

# Bounds on remotely-influenced peer-list state (design doc's own
# "every remotely influenced ... collection needs an explicit
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
# validation at all on receive, unlike a locally created post. Imported
# from `netbbs.boards.limits` (issue #79) rather than hard-coded here a
# second time -- that module is just two integers with no other
# imports, so this stays free of `netbbs.boards.posts`' actual
# database/business-logic dependencies while still sharing one
# definition instead of two that could silently drift apart.


class LinkProtocolError(Exception):
    """Raised for a hello or event message that fails verification: an
    inconsistent/forked/unverifiable key_transition chain, a descriptor
    signed by a key the chain doesn't currently authorize, a descriptor
    or event whose claimed subject doesn't match who actually sent it,
    an event from a peer with no completed hello on file, a board_post
    for a board_id with no verified board_genesis on file, a board_post
    author kind this node doesn't yet know how to verify (only
    node_vouched_user is built), a board_genesis conflicting with a
    different one already on file for the same board_id, a
    board_post_edit for an unknown root post, a board_post_edit whose
    author doesn't match the root post's own author (moderator edits
    aren't supported yet), or a board_post_edit whose previous_event_id
    doesn't match the current head of its chain (reordering is refused
    outright, the same push-and-retry recovery model already
    established for key_transition); a link_message not addressed to
    this node (design doc §13: strictly point-to-point, never
    speculatively stored on behalf of a different intended recipient),
    a link_message whose sender doesn't match who actually sent it, or
    a link_message_accepted/link_message_bounced referencing a message
    this node never actually sent, or vouching for a recipient node
    other than whoever actually sent the acknowledgement."""


def _signing_transitions(transitions: tuple[KeyTransition, ...], fingerprint: str) -> tuple[KeyTransition, ...]:
    """This node's own `"signing"`-purpose transition history — what a
    hello bundle includes (the `"transport"`-purpose chain is Noise's
    own concern, §11, not this handshake's)."""
    return tuple(
        t
        for t in transitions
        if t.payload.get("subject_fingerprint") == fingerprint and t.payload.get("purpose") == "signing"
    )


@dataclass(frozen=True)
class HelloMessage:
    """
    First-contact bundle (design doc §12): everything a receiver
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
    completed peers (design doc §12's "signed peer-list exchange") —
    deliberately not a canonical `netbbs.link.events` envelope of its
    own: this is ephemeral discovery data ("addresses worth trying"),
    not durable state that needs content-addressing, dedup, or
    gossip-replay semantics the way a `board_post` does.

    Each individual descriptor inside is already self-signed by its own
    claimed subject — nothing here adds an outer signature
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
class InventoryRequest:
    """
    "What do you have for these boards that I don't already?" (design
    doc §8.8, issue #85) -- the same kind of decision `PeerListMessage`
    already made: deliberately **not** a canonical `netbbs.link.events`
    envelope of its own. This is a bookkeeping request about what the
    requester already has, not durable authored content that needs
    content-addressing, a signature, or gossip-replay semantics. A
    stale or malformed request costs nothing beyond one wasted round
    trip -- the responder's own diff query (`netbbs.link.store.
    board_event_diff`) treats an unrecognized `board_id` exactly like
    one it doesn't carry, silently skipped, never an error.

    `boards` is keyed by every `board_id` the requester currently
    carries (bounded by its own `max_carried_boards` quota, §13.9 --
    this request's size is therefore already bounded by an existing
    cap, not a new one), mapped to that board's full known-`content_id`
    list. The response is not a new message type either -- it reuses
    `push_events`'s existing raw-event-list wire shape, verified and
    applied through the exact same `LinkNode.handle_events` path a push
    response already uses (see `netbbs.link.transport`'s `/inventory`
    route and `request_inventory`).
    """

    boards: dict[str, tuple[str, ...]]

    def to_dict(self) -> dict:
        return {"boards": {board_id: list(ids) for board_id, ids in self.boards.items()}}

    @classmethod
    def from_dict(cls, data: dict) -> "InventoryRequest":
        return cls(boards={board_id: tuple(ids) for board_id, ids in data["boards"].items()})


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
class PeerDirectory:
    """Peer/discovery state (issue #78): verified peers from a completed
    hello, and unverified endpoint-descriptor candidates learned
    secondhand via peer-list exchange. Kept together because a
    fingerprint's presence in one changes what the other means for it
    (see `admit`) -- but each dict is still exactly the shape it was
    directly on `LinkNode` before this split; nothing about the wire
    protocol or persisted shape changes."""

    # fingerprint -> a completed, cryptographically verified hello.
    peers: dict[str, "PeerRecord"] = field(default_factory=dict)
    # fingerprint -> an unverified endpoint descriptor learned
    # secondhand via peer-list exchange -- "worth trying," never
    # promoted to `peers` until a real hello with that fingerprint
    # actually completes. See `handle_peer_list`'s own docstring for why
    # nothing here is ever cryptographically checked at receipt time.
    candidate_descriptors: dict[str, EndpointDescriptor] = field(default_factory=dict)

    def admit(self, record: "PeerRecord") -> None:
        """Record a newly (or freshly re-)verified peer. Clears any
        unverified candidate entry for the same fingerprint -- now
        superseded by the real thing, never left sitting alongside it."""
        self.peers[record.fingerprint] = record
        self.candidate_descriptors.pop(record.fingerprint, None)

    def record_candidate(self, fingerprint: str, descriptor: EndpointDescriptor, *, max_candidates: int) -> bool:
        """Record a secondhand, unverified descriptor. Returns whether it
        was actually recorded -- skipped if `fingerprint` is already a
        verified peer (nothing gained from a secondhand claim about
        someone already directly known), if a candidate already on file
        for it has an equal-or-newer `created_at`, or if it would be a
        brand-new entry past `max_candidates` (refreshing an existing
        candidate's own descriptor is still allowed past the cap)."""
        if fingerprint in self.peers:
            return False
        existing = self.candidate_descriptors.get(fingerprint)
        if existing is not None and descriptor.payload.get("created_at", "") <= existing.payload.get(
            "created_at", ""
        ):
            return False
        if existing is None and len(self.candidate_descriptors) >= max_candidates:
            return False
        self.candidate_descriptors[fingerprint] = descriptor
        return True


@dataclass
class BoardEventState:
    """Board/event projection state (issue #78): verified board_genesis
    per board, and each board_post's verified board_post_edit chain.
    `known_event_ids`/`events` deliberately stay directly on `LinkNode`
    -- they're the shared dedup/store substrate every object type uses
    (key_transition, link_message, ...), not a board-specific concern."""

    # board_id -> the verified board_genesis on file for it. A
    # board_post is only ever accepted for a board_id already present
    # here (module docstring's "no relay from a stranger" boundary,
    # applied to boards).
    boards: dict[str, BoardGenesis] = field(default_factory=dict)
    # root_post_id (a board_post's own content_id) -> its verified edit
    # chain, oldest first. Deliberately not a generic reusable
    # chain-walker -- a single linear list per post, ordering enforced
    # by "does previous_event_id match the current head" alone, a
    # shape simpler than key_transition's two-interleaved-purposes
    # chain.
    post_edits: dict[str, tuple[BoardPostEdit, ...]] = field(default_factory=dict)

    def genesis_for(self, board_id: str) -> BoardGenesis | None:
        return self.boards.get(board_id)

    def has_conflicting_genesis(self, board_id: str, content_id: str) -> bool:
        """Whether a *different* genesis is already on file for
        `board_id` -- one board_id may never acquire two distinct
        geneses."""
        existing = self.boards.get(board_id)
        return existing is not None and existing.content_id != content_id

    def record_genesis(self, genesis: BoardGenesis) -> None:
        self.boards[genesis.payload["board_id"]] = genesis

    def edit_chain(self, root_post_id: str) -> tuple[BoardPostEdit, ...]:
        return self.post_edits.get(root_post_id, ())

    def extend_edit_chain(self, root_post_id: str, edit: BoardPostEdit) -> None:
        self.post_edits[root_post_id] = self.edit_chain(root_post_id) + (edit,)


@dataclass
class BoardLifecycleState:
    """Board lifecycle/origin state (issue #78, issue #53): board-origin
    succession, tracked separately from the board/event projection
    above because it has its own chain (`board_lifecycle_head`,
    starting from the board's own genesis) with its own mutual-consent
    rule, distinct from a `board_post_edit`'s per-post chain."""

    # issue #53: board_id -> the fingerprint currently authoritative for
    # it, once a board_origin_transfer_accepted has been verified --
    # absent means "still the genesis's own origin," see
    # current_origin.
    board_origin: dict[str, str] = field(default_factory=dict)
    # issue #53: board_id -> the content_id a new lifecycle event (an
    # offer or an acceptance) must reference as its own
    # previous_event_id -- absent means "still genesis," see
    # current_lifecycle_head.
    board_lifecycle_head: dict[str, str] = field(default_factory=dict)
    # issue #53: board_id -> its single outstanding, not-yet-accepted
    # board_origin_transfer_offer, if any -- at most one may be in
    # flight per board at a time (see BoardOriginTransferOffer's own
    # docstring for why this doesn't support more).
    pending_origin_transfers: dict[str, BoardOriginTransferOffer] = field(default_factory=dict)

    def current_origin(self, board_id: str, genesis_origin_fingerprint: str) -> str:
        """The fingerprint currently authoritative for `board_id` --
        `board_origin`'s override if a transfer has ever completed, else
        the caller-supplied genesis claim (`LinkNode.current_board_
        origin` supplies `self.board_events.boards[board_id].payload
        ["origin_fingerprint"]`, keeping this type independent of
        `BoardEventState`)."""
        return self.board_origin.get(board_id, genesis_origin_fingerprint)

    def current_lifecycle_head(self, board_id: str, genesis_content_id: str) -> str:
        """The content_id a *new* lifecycle event for `board_id` must
        reference as its own `previous_event_id` -- the latest accepted
        lifecycle event if one exists, else the caller-supplied genesis
        content_id."""
        return self.board_lifecycle_head.get(board_id, genesis_content_id)

    def pending_offer(self, board_id: str) -> BoardOriginTransferOffer | None:
        return self.pending_origin_transfers.get(board_id)

    def record_offer(self, board_id: str, offer: BoardOriginTransferOffer) -> None:
        self.pending_origin_transfers[board_id] = offer
        self.board_lifecycle_head[board_id] = offer.content_id

    def record_acceptance(self, board_id: str, *, new_origin_fingerprint: str, accepted_content_id: str) -> None:
        self.board_origin[board_id] = new_origin_fingerprint
        self.board_lifecycle_head[board_id] = accepted_content_id
        del self.pending_origin_transfers[board_id]


@dataclass
class RelayState:
    """Relay/reachability state (issue #78, issue #58): this node's own
    outstanding/granted relay relationships in both directions. Mostly
    mutated by callers *outside* this module (`netbbs.link.transport`'s
    relay-consent routes) -- `handle_relay_consent_request`/`_response`'s
    own docstrings describe why this layer verifies but deliberately
    does not decide accept/decline here. Grouping these three dicts
    gives that external policy state one named home instead of three
    loose fields directly on `LinkNode`."""

    # issue #58: relay_fingerprint -> the still-outstanding
    # RelayConsentRequest this node itself sent and hasn't yet gotten a
    # reply to -- self-origination bookkeeping the caller sets directly
    # before dialing out (`netbbs.link.transport.request_relay_consent`),
    # mirroring `BoardLifecycleState.pending_origin_transfers`' own
    # "this node's own request, never routed through handle_events"
    # shape.
    pending_own_relay_requests: dict[str, RelayConsentRequest] = field(default_factory=dict)
    # issue #58: requester_fingerprint -> when this node (acting as the
    # relay) agreed to serve it, once granted. Whether to grant is a
    # resource-cap/opt-out policy decision this pure/in-memory layer
    # has no config to make (see `handle_relay_consent_request`'s own
    # docstring) -- the caller applies the decision to this dict directly
    # after calling that method, the same "verify here, mutate there"
    # split `record_board_origin_change` already uses for a bystander
    # node's own board state.
    relaying_for: dict[str, str] = field(default_factory=dict)
    # issue #58: relay_fingerprint -> when that candidate accepted this
    # node's own relay_consent_request, once granted -- what
    # `netbbs.link.boards`-equivalent code for endpoint descriptors
    # (issue #58 task #23) reads to populate this node's own published
    # `relays` field.
    relays_serving_me: dict[str, str] = field(default_factory=dict)


@dataclass
class LinkNode:
    """
    This node's own Link protocol state: its identity, what it's
    learned about its peers, and which events it's already seen.
    Transport-agnostic (see module docstring) — a caller wires this up
    to however messages actually travel.

    Internal state is grouped by concern (issue #78) rather than one
    ever-growing flat collection of unrelated dicts: `peer_directory`
    (peer/discovery), `board_events` (board/event projection),
    `board_lifecycle` (board origin/succession), `relay_state` (relay/
    reachability). `known_event_ids`/`events` stay directly here as the
    shared substrate every object type uses, not owned by any one
    family. A future state family (inventory/pull catch-up, linked-
    channel lifecycle) should become its own such grouped type, not a
    thirteenth flat field here.

    The flat `peers`/`boards`/`post_edits`/`board_origin`/`board_
    lifecycle_head`/`pending_origin_transfers`/`candidate_descriptors`/
    `pending_own_relay_requests`/`relaying_for`/`relays_serving_me`
    properties below exist purely for external backward compatibility:
    `netbbs.link.store`/`.sync`/`.transport`/`.relay_selection` and
    `netbbs.net.admin_flow` (plus their own tests) read and mutate these
    by direct attribute access -- `node.peers[x] = y`, `node.relaying_
    for.pop(...)`, `len(node.board_lifecycle_head)`. Each property
    returns the exact same live dict the grouped state object owns,
    never a copy, so every existing external access pattern keeps
    working unchanged -- this refactor moves where the data is
    *defined*, not what it means to read or mutate it from outside
    `LinkNode`.
    """

    identity: NodeIdentity
    known_event_ids: set[str] = field(default_factory=set)
    events: dict[str, dict] = field(default_factory=dict)
    peer_directory: PeerDirectory = field(default_factory=PeerDirectory)
    board_events: BoardEventState = field(default_factory=BoardEventState)
    board_lifecycle: BoardLifecycleState = field(default_factory=BoardLifecycleState)
    relay_state: RelayState = field(default_factory=RelayState)

    @property
    def peers(self) -> dict[str, "PeerRecord"]:
        return self.peer_directory.peers

    @property
    def candidate_descriptors(self) -> dict[str, EndpointDescriptor]:
        return self.peer_directory.candidate_descriptors

    @property
    def boards(self) -> dict[str, BoardGenesis]:
        return self.board_events.boards

    @property
    def post_edits(self) -> dict[str, tuple[BoardPostEdit, ...]]:
        return self.board_events.post_edits

    @property
    def board_origin(self) -> dict[str, str]:
        return self.board_lifecycle.board_origin

    @property
    def board_lifecycle_head(self) -> dict[str, str]:
        return self.board_lifecycle.board_lifecycle_head

    @property
    def pending_origin_transfers(self) -> dict[str, BoardOriginTransferOffer]:
        return self.board_lifecycle.pending_origin_transfers

    @property
    def pending_own_relay_requests(self) -> dict[str, RelayConsentRequest]:
        return self.relay_state.pending_own_relay_requests

    @property
    def relaying_for(self) -> dict[str, str]:
        return self.relay_state.relaying_for

    @property
    def relays_serving_me(self) -> dict[str, str]:
        return self.relay_state.relays_serving_me

    def build_hello(
        self, *, addresses: list[dict] | None, outgoing_only: bool, created_at: str
    ) -> HelloMessage:
        """Build this node's own hello bundle. `addresses`/
        `outgoing_only`/`created_at` are the caller's to supply (node
        network configuration and the current time are not this
        method's concern — see `tests/link_harness.py`'s `FakeClock`
        for how tests keep this deterministic). `relays` (issue #58)
        is **not** a caller-supplied parameter the way those
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
            relays=list(self.relay_state.relays_serving_me.keys()) or None,
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
        file — the "latest signed descriptor wins" rule (see
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

        for transition in message.transitions:
            self._check_protocol_version(transition.envelope, kind="key_transition", sender_fingerprint=claimed_fingerprint)
        self._check_protocol_version(message.descriptor.envelope, kind="endpoint_descriptor", sender_fingerprint=claimed_fingerprint)

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
        self.peer_directory.admit(record)
        return record

    def build_peer_list(self) -> PeerListMessage:
        """This node's own currently-verified peers' endpoint
        descriptors, to share with a directly-connected peer (design
        doc §12's "signed peer-list exchange"). Only ever
        drawn from `self.peers` (each one verified via a real completed
        hello) -- never re-shares a candidate this node itself learned
        secondhand, so a claim's provenance never grows past one hop of
        "someone I've actually talked to vouches this address is worth
        trying.\""""
        return PeerListMessage(descriptors=tuple(peer.descriptor for peer in self.peer_directory.peers.values()))

    def handle_peer_list(self, sender_fingerprint: str, message: PeerListMessage) -> list[str]:
        """
        Record candidate addresses shared by `sender_fingerprint`, who
        must already be a completed peer (same "no relay from a
        stranger" boundary `handle_events` enforces). Returns the
        fingerprints newly recorded or refreshed, purely informational.

        **Nothing here is cryptographically verified against a resolved
        signing key, deliberately** -- a descriptor's own signature
        can't be checked without that subject's root key/transition
        chain, which this node doesn't have for a stranger yet -- "a
        weak prior worth trying, not trusting outright." Real trust
        only ever happens once this node dials a
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
            if self.peer_directory.record_candidate(
                candidate_fingerprint, descriptor, max_candidates=_MAX_CANDIDATE_DESCRIPTORS
            ):
                recorded.append(candidate_fingerprint)
        return recorded

    def handle_relay_consent_request(self, sender_fingerprint: str, request: RelayConsentRequest) -> None:
        """
        Verify an incoming `relay_consent_request` (design doc §12,
        issue #58) from `sender_fingerprint`, who must already
        be a completed peer -- the same "no relay from a stranger"
        boundary every other acceptance rule in this module applies,
        satisfied here by the mutual hello a candidate relay and a
        prospective requester must have already exchanged before either
        one calls this (a hello is mutual by construction -- see
        `handle_hello`).

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

    def _check_protocol_version(self, envelope: dict, *, kind: str, sender_fingerprint: str | None = None) -> None:
        """
        Design doc §13.11, issue #60: every canonical envelope already
        carries `netbbs_protocol` (`build_envelope`) -- this
        was the first thing anywhere in this codebase to actually read
        it back on receipt. Exact match only, never a supported range:
        there is nothing to be forward/backward-compatible *with* yet,
        since `NETBBS_PROTOCOL_VERSION` has only ever been `1` -- the
        point of this check is having a real, tested gate in place
        *before* a version 2 ever exists, not guessing at compatibility
        rules for a wire change nobody has designed. Called once per
        envelope at `handle_events`' own single object_type-extraction
        point (covering all nine event types from one call site) and
        against `handle_hello`'s own embedded transitions/descriptor
        envelopes -- never duplicated per object type.
        """
        version = envelope.get("netbbs_protocol")
        if version != NETBBS_PROTOCOL_VERSION:
            source = f"{sender_fingerprint} sent" if sender_fingerprint is not None else "received"
            raise LinkProtocolError(
                f"{source} a {kind} with netbbs_protocol={version!r}, this node only "
                f"understands {NETBBS_PROTOCOL_VERSION!r} -- refusing rather than risk "
                "misinterpreting an incompatible payload shape"
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
        (design doc §13, issue #53) -- `self.board_origin`'s
        override if a transfer has ever completed, else the board's own
        genesis claim. Mirrors `netbbs.link.boards.board_origin_
        fingerprint`'s exact same two-tier resolution, applied here to
        this node's in-memory state instead of a DB row -- both must
        agree, since the DB-side version is this same fact persisted."""
        return self.board_lifecycle.current_origin(
            board_id, self.board_events.boards[board_id].payload["origin_fingerprint"]
        )

    def current_board_lifecycle_head(self, board_id: str) -> str:
        """The content_id a *new* lifecycle event for `board_id` (an
        offer or an acceptance) must reference as its own `previous_
        event_id` (design doc §13, issue #53) -- the latest
        accepted lifecycle event if one exists, else the board's own
        genesis. Mirrors `netbbs.link.boards._current_lifecycle_head`'s
        own reasoning, applied to this node's in-memory state."""
        return self.board_lifecycle.current_lifecycle_head(board_id, self.board_events.boards[board_id].content_id)

    def handle_events(self, sender_fingerprint: str, raw_events: list[dict]) -> list[str]:
        """
        Accept zero or more incoming signed events from a peer that has
        already completed a hello (see module docstring — a stranger's
        events are refused, not queued/relayed). Returns the
        content_ids of events newly accepted; already-seen ones are
        silently skipped (§7: transport-level dedup is a performance
        optimization, not a safety mechanism -- idempotency for an
        already-applied event does not depend solely on `known_event_
        ids` still holding the entry, see below).

        Nine recognized `object_type`s: `key_transition`,
        `board_genesis`/`board_post`, `board_post_edit`,
        `board_origin_transfer_offer`/`board_origin_transfer_
        accepted` (design doc §13, issue #53), and `link_message`/
        `link_message_accepted`/`link_message_bounced` (design doc §13).
        `board_genesis`/`board_post` have no per-object chain to
        self-heal against (`board_post` is immutable content, nothing to
        project beyond "does it exist"; `board_genesis` itself is still
        one-per-board, never resent as a candidate extension of
        anything) -- for both, `known_event_ids` dedup alone is what's
        built so far, which is already correct for content that's never
        resent as a *candidate extension* of anything. `board_post_edit`
        *is* a candidate extension (a single linear chain per root post)
        and gets the same chain-membership self-heal `key_transition`
        has, just against `self.post_edits` instead of a peer's own
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
        *this* node specifically (a point-to-point framing,
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
            self._check_protocol_version(raw["envelope"], kind=object_type, sender_fingerprint=sender_fingerprint)

            if object_type == KEY_TRANSITION_OBJECT_TYPE:
                transition = KeyTransition.from_dict(raw)
                if transition.content_id in self.known_event_ids:
                    continue

                if any(existing.content_id == transition.content_id for existing in sender.transitions):
                    # Already integrated into sender's own chain
                    # (permanent, never-purged key-lifecycle state) even
                    # though known_event_ids doesn't currently have it --
                    # a legitimate resend ("push every transition every
                    # pass," or a future purged-then-resent dedup entry),
                    # not a fork attempt: a genuine fork
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

                # Issue #85: verified against the *content's own claimed
                # origin*, not required to equal sender_fingerprint --
                # this is what makes genuine multi-hop relay (an
                # inventory response from a node that merely carries
                # this board) verifiable at all. The origin must still
                # be a peer this node has *independently* completed a
                # hello with at some point (never merely "whoever is on
                # the other end of this HTTP call") -- "no relay from a
                # stranger" now means the *origin*, not the *carrier*,
                # must already be known.
                origin_fingerprint = genesis.payload.get("origin_fingerprint")
                origin_peer = self.peers.get(origin_fingerprint)
                if origin_peer is None:
                    raise LinkProtocolError(
                        f"received a board_genesis originated by {origin_fingerprint}, which has "
                        "no completed hello with this node -- refusing (no relay from a stranger)"
                    )

                board_id = genesis.payload["board_id"]
                if self.board_events.has_conflicting_genesis(board_id, genesis.content_id):
                    raise LinkProtocolError(
                        f"received a conflicting board_genesis for board_id {board_id!r} -- a "
                        "different genesis is already on file for it"
                    )

                signing_verify_key = self._resolve_sender_signing_key(origin_peer, origin_fingerprint, "board_genesis")
                if not verify_board_genesis(genesis, signing_verify_key):
                    raise LinkProtocolError(
                        f"board_genesis from origin {origin_fingerprint} does not verify against "
                        "its current signing key"
                    )

                self.board_events.record_genesis(genesis)
                self.known_event_ids.add(genesis.content_id)
                self.events[genesis.content_id] = raw
                accepted.append(genesis.content_id)

            elif object_type == BOARD_POST_OBJECT_TYPE:
                post = BoardPost.from_dict(raw)
                if post.content_id in self.known_event_ids:
                    continue

                board_id = post.payload.get("board_id")
                if self.board_events.genesis_for(board_id) is None:
                    raise LinkProtocolError(
                        f"received a board_post for board_id {board_id!r}, which has no verified "
                        "board_genesis on file -- refusing (no relay from a stranger yet)"
                    )
                self._check_board_post_content_size(post.payload, sender_fingerprint, "board_post")

                author = post.payload.get("author", {})
                author_kind = author.get("kind")
                if author_kind != "node_vouched_user":
                    raise LinkProtocolError(
                        f"board_post author kind {author_kind!r} is not yet supported "
                        "(only node_vouched_user is built)"
                    )
                # Issue #85: same relaxation as board_genesis above --
                # verified against the author's own home node, which
                # must independently be a known peer, not required to
                # equal sender_fingerprint.
                home_node_fingerprint = author.get("home_node_fingerprint")
                author_peer = self.peers.get(home_node_fingerprint)
                if author_peer is None:
                    raise LinkProtocolError(
                        f"received a board_post vouched for by {home_node_fingerprint}, which has "
                        "no completed hello with this node -- refusing (no relay from a stranger)"
                    )

                signing_verify_key = self._resolve_sender_signing_key(author_peer, home_node_fingerprint, "board_post")
                if not verify_board_post(post, signing_verify_key):
                    raise LinkProtocolError(
                        f"board_post from home node {home_node_fingerprint} does not verify "
                        "against its current signing key"
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
                        "supported yet"
                    )
                # Issue #85: same relaxation as board_post above.
                home_node_fingerprint = edit_author.get("home_node_fingerprint")
                edit_author_peer = self.peers.get(home_node_fingerprint)
                if edit_author_peer is None:
                    raise LinkProtocolError(
                        f"received a board_post_edit vouched for by {home_node_fingerprint}, "
                        "which has no completed hello with this node -- refusing (no relay "
                        "from a stranger)"
                    )

                existing_chain = self.board_events.edit_chain(root_post_id)
                if any(existing.content_id == edit.content_id for existing in existing_chain):
                    # Exact resend of an already-integrated edit: a safe
                    # no-op, self-healing known_event_ids, not a fork attempt.
                    self.known_event_ids.add(edit.content_id)
                    self.events.setdefault(edit.content_id, raw)
                    continue

                current_head = existing_chain[-1].content_id if existing_chain else root_post_id
                if edit.payload.get("previous_event_id") != current_head:
                    raise LinkProtocolError(
                        f"board_post_edit for root post {root_post_id!r} does not extend the "
                        f"current head ({current_head!r}) -- refusing (reordering "
                        "isn't tolerated, a full resend recovers, same model as key_transition)"
                    )

                signing_verify_key = self._resolve_sender_signing_key(
                    edit_author_peer, home_node_fingerprint, "board_post_edit"
                )
                if not verify_board_post_edit(edit, signing_verify_key):
                    raise LinkProtocolError(
                        f"board_post_edit from home node {home_node_fingerprint} does not verify "
                        "against its current signing key"
                    )

                self.board_events.extend_edit_chain(root_post_id, edit)
                self.known_event_ids.add(edit.content_id)
                self.events[edit.content_id] = raw
                accepted.append(edit.content_id)

            elif object_type == BOARD_ORIGIN_TRANSFER_OFFER_OBJECT_TYPE:
                offer = BoardOriginTransferOffer.from_dict(raw)
                if offer.content_id in self.known_event_ids:
                    continue

                board_id = offer.payload.get("board_id")
                if self.board_events.genesis_for(board_id) is None:
                    raise LinkProtocolError(
                        f"received a board_origin_transfer_offer for board_id {board_id!r}, "
                        "which has no verified board_genesis on file -- refusing (no relay "
                        "from a stranger yet)"
                    )

                # Issue #85: same relaxation as board_genesis above --
                # the board's current origin must independently be a
                # known peer, regardless of who relayed this offer.
                current_origin = self.current_board_origin(board_id)
                origin_peer = self.peers.get(current_origin)
                if origin_peer is None:
                    raise LinkProtocolError(
                        f"received a board_origin_transfer_offer for board_id {board_id!r} whose "
                        f"current origin ({current_origin!r}) has no completed hello with this "
                        "node -- refusing (no relay from a stranger)"
                    )
                old_origin_fingerprint = offer.payload.get("old_origin_fingerprint")
                if old_origin_fingerprint != current_origin:
                    raise LinkProtocolError(
                        f"board_origin_transfer_offer for board_id {board_id!r} claims an "
                        f"old_origin_fingerprint ({old_origin_fingerprint!r}) that doesn't "
                        f"match its actual current origin ({current_origin!r})"
                    )
                existing_offer = self.board_lifecycle.pending_offer(board_id)
                if existing_offer is not None:
                    if existing_offer.content_id == offer.content_id:
                        # Issue #86: exact resend of the still-pending
                        # offer -- a safe no-op, self-healing known_
                        # event_ids from the authoritative pending-offer
                        # state rather than treating a legitimate resend
                        # ("push everything every pass," or a
                        # purged-then-resent dedup entry) as a genuine
                        # second, conflicting offer. Same shape key_
                        # transition/board_post_edit already use for
                        # their own chains.
                        self.known_event_ids.add(offer.content_id)
                        self.events.setdefault(offer.content_id, raw)
                        continue
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
                    origin_peer, current_origin, "board_origin_transfer_offer"
                )
                if not verify_board_origin_transfer_offer(offer, signing_verify_key):
                    raise LinkProtocolError(
                        f"board_origin_transfer_offer from current origin {current_origin} does "
                        "not verify against its current signing key"
                    )

                self.board_lifecycle.record_offer(board_id, offer)
                self.known_event_ids.add(offer.content_id)
                self.events[offer.content_id] = raw
                accepted.append(offer.content_id)

            elif object_type == BOARD_ORIGIN_TRANSFER_ACCEPTED_OBJECT_TYPE:
                transfer_accepted = BoardOriginTransferAccepted.from_dict(raw)
                if transfer_accepted.content_id in self.known_event_ids:
                    continue

                board_id = transfer_accepted.payload.get("board_id")
                if self.board_lifecycle.board_lifecycle_head.get(board_id) == transfer_accepted.content_id:
                    # Issue #86: exact resend of the acceptance already
                    # recorded as this board's current lifecycle head --
                    # a safe no-op, self-healing known_event_ids from the
                    # authoritative board_lifecycle_head state rather
                    # than depending solely on the fast dedup cache
                    # still holding this content_id (the same gap key_
                    # transition/board_post_edit/the offer branch above
                    # already close for their own chains). Checked
                    # *before* the "outstanding offer" lookup below,
                    # since record_acceptance already deleted the
                    # pending offer this event once accepted -- that
                    # lookup alone can't distinguish "already applied"
                    # from "no relay from a stranger."
                    self.known_event_ids.add(transfer_accepted.content_id)
                    self.events.setdefault(transfer_accepted.content_id, raw)
                    continue

                offer = self.board_lifecycle.pending_offer(board_id)
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
                # Issue #85: same relaxation as the offer branch above --
                # the offer's named new origin must independently be a
                # known peer, regardless of who relayed this acceptance.
                new_origin_peer = self.peers.get(new_origin_fingerprint)
                if new_origin_peer is None:
                    raise LinkProtocolError(
                        f"received a board_origin_transfer_accepted for board_id {board_id!r} "
                        f"whose named new origin ({new_origin_fingerprint!r}) has no completed "
                        "hello with this node -- refusing (no relay from a stranger)"
                    )

                signing_verify_key = self._resolve_sender_signing_key(
                    new_origin_peer, new_origin_fingerprint, "board_origin_transfer_accepted"
                )
                if not verify_board_origin_transfer_accepted(transfer_accepted, signing_verify_key):
                    raise LinkProtocolError(
                        f"board_origin_transfer_accepted from new origin {new_origin_fingerprint} "
                        "does not verify against its current signing key"
                    )

                self.board_lifecycle.record_acceptance(
                    board_id,
                    new_origin_fingerprint=new_origin_fingerprint,
                    accepted_content_id=transfer_accepted.content_id,
                )
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
                        f"({self.identity.fingerprint!r}) -- refusing (strictly "
                        "point-to-point, no relay on behalf of a different recipient)"
                    )

                sender_info = message.payload.get("sender", {})
                sender_kind = sender_info.get("kind")
                if sender_kind != "node_vouched_user":
                    raise LinkProtocolError(
                        f"link_message sender kind {sender_kind!r} is not yet supported "
                        "(only node_vouched_user is built)"
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
