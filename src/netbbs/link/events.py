"""
Canonical NetBBS Link event envelope (design doc §7, rounds
27/90/110/116/124).

Round 27 fixed the outer envelope shape (`netbbs_protocol`/
`object_type`/`payload`); round 90 fixed the semantic model (event
chains with head pointers, replacing per-feature special-casing); round
110 fixed the byte-level canonicalization rule (reusing
`netbbs.boards.content_id.canonical_json_bytes` rather than a second
implementation) and the one concrete event type needed to unblock round
89's node key-lifecycle work: `key_transition`. Round 116 adds
`endpoint_descriptor` (design doc §12), the second concrete event type,
needed to unblock the first real handshake/gossip protocol code
(`netbbs.link.protocol`). Round 124/125 adds `board_genesis` and
`board_post` (design doc §13/§7, the first Phase 3 board-related event
types) — see each type's own docstring below for the design doc round
124 decisions they encode. Round 129/130 adds `board_post_edit` —
self-authored edits only this round; moderator edits and tombstones
stay deferred to Phase 6 (design doc round 129).

Design doc round 93 adds `link_message`, `link_message_accepted`, and
`link_message_bounced` (Link's extension of local mail, §7). This
implementation slice covers building/signing/verifying the three
envelope shapes only — protocol-level acceptance rules
(`netbbs.link.protocol.LinkNode.handle_events`), the delivery/routing
mechanism that actually reaches a specific recipient node, and
`link_message_expired` (which depends on that same routing/retry
design not yet settled) are deliberately not part of this slice; see
each class's own docstring for the boundary.

Design doc round 94/issue #53 adds `board_origin_transfer_offer` and
`board_origin_transfer_accepted` (§13's origin-succession policy) — the
mutual-consent pair a board's current origin and a prospective new one
exchange to hand off authority, plus an optional `forked_from` pointer
on `board_genesis` itself. Orphan detection needs no event type of its
own — it's a computed property of the origin's existing key-transition
chain (`netbbs.link.node_identity.resolve_current_operational_key`),
not a signal on the wire (round 94: "no cryptographic proof an origin
is gone versus merely offline," so no node's observation gets an
automatic network-wide effect).

No other event type (moderator grants, tombstones, etc.) is specified
here yet — each gets its own payload-shape decision when it's actually
being built, following this same envelope pattern (round 110's own
scope note).
"""

from __future__ import annotations

import base64
import secrets
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

# Round 124: the signed announcement putting an existing local board
# into Link scope, and an individual Link-native post on one.
BOARD_GENESIS_OBJECT_TYPE = "board_genesis"
BOARD_POST_OBJECT_TYPE = "board_post"

# Round 129: a self-authored edit to an existing board_post -- never a
# moderator edit or a tombstone this round (design doc round 129).
BOARD_POST_EDIT_OBJECT_TYPE = "board_post_edit"

# Design doc round 94/issue #53: the mutual-consent origin-succession
# pair -- the current origin's handoff offer, and the new origin's
# acceptance. Neither alone changes anything (see each class's own
# docstring).
BOARD_ORIGIN_TRANSFER_OFFER_OBJECT_TYPE = "board_origin_transfer_offer"
BOARD_ORIGIN_TRANSFER_ACCEPTED_OBJECT_TYPE = "board_origin_transfer_accepted"

# Design doc round 93: Link's extension of local mail. A signed message
# to one specific recipient node, and the two acknowledgement shapes the
# recipient's node sends back toward the sender's. `link_message_expired`
# is named in round 93 but not built yet -- see module docstring.
LINK_MESSAGE_OBJECT_TYPE = "link_message"
LINK_MESSAGE_ACCEPTED_OBJECT_TYPE = "link_message_accepted"
LINK_MESSAGE_BOUNCED_OBJECT_TYPE = "link_message_bounced"

_VALID_PURPOSES = ("signing", "transport")
_VALID_ACTIONS = ("authorize", "revoke")
_VALID_NAME_REQUIREMENTS = ("verified", "verified_and_displayed")

# Round 124: the only `board_post` author tag with a real build/verify
# path this round — see `build_board_post`'s docstring for why
# `user_key`/`node` are named in the design but not built yet. Round 93's
# `link_message` sender reuses the same tag for the same reason.
_NODE_VOUCHED_USER_AUTHOR_KIND = "node_vouched_user"

# Design doc round 93's two confidentiality tiers -- which key a
# `link_message`'s ciphertext is sealed to. See `netbbs.identity.
# encryption` for the actual derive-and-seal mechanism.
_TIER1_HOME_NODE_KEY = "tier1_home_node_key"
_TIER2_PERSONAL_KEY = "tier2_personal_key"
_VALID_CONFIDENTIALITY_TIERS = (_TIER1_HOME_NODE_KEY, _TIER2_PERSONAL_KEY)

# Design doc round 93's named bounce reasons.
_VALID_BOUNCE_REASONS = ("mailbox_full", "blocked_sender", "unknown_recipient")


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


@dataclass(frozen=True)
class BoardGenesis:
    """
    One signed `board_genesis` event (design doc §13, round 124): the
    announcement that puts an *existing* local board into Link scope —
    not a separate creation act. Round 124's central decision:
    `payload["board_id"]` is the board's existing local content-
    addressed ID (`netbbs.boards.content_id.compute_content_id`), never
    a newly-minted one, so a board created Linked-from-the-start and a
    board Linked years into its local life go through the exact same
    event; only timing differs. This is the head of what design doc
    round 94 already describes as an eventual lifecycle chain (later
    closure/transfer entries append, each its own object type per
    round 124's own scope note) — no `previous_event_id` here, since
    nothing precedes genesis.

    Always signed by the origin node's current *signing* operational
    key (round 89), never its root key directly — round 90 already
    names board creation as the canonical `node`-tier authored event,
    so there is no author tagged-union here at all, unlike `BoardPost`.
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
    def from_dict(cls, data: dict) -> "BoardGenesis":
        return cls(envelope=data["envelope"], signature=base64.b64decode(data["signature"]))


def build_board_genesis(
    *,
    signing_identity: Identity,
    origin_fingerprint: str,
    board_id: str,
    name: str,
    created_at: str,
    description: str | None = None,
    default_min_read_level: int | None = None,
    default_min_write_level: int | None = None,
    default_moderated: bool | None = None,
    default_max_post_age_days: int | None = None,
    default_min_age: int | None = None,
    default_name_requirement: str | None = None,
    forked_from: str | None = None,
) -> BoardGenesis:
    """
    Build and sign one `board_genesis` event, per design doc round 124.

    `board_id` is the board's *existing* local content-addressed ID
    (see `BoardGenesis`'s own docstring for why this round deliberately
    doesn't mint a new one). `origin_fingerprint` is the origin node's
    root fingerprint, included explicitly (not merely implied by
    "whoever signed this") so a verifier can cross-check it against
    whichever peer's transition chain it resolved the signing key from
    — same reasoning `build_endpoint_descriptor` already documents for
    its own `subject_fingerprint` field.

    The six `default_*` fields are optional, non-binding cascading-
    scalar-default recommendations (design doc round 86, applied to
    boards for the first time in round 124) — a superset of Community's
    own four `default_*` fields, since a board owns `moderated`/
    `max_post_age_days` directly where a Community doesn't. Each is
    omitted entirely when `None` (round 110 point 6), never stored as
    `null`; a carrying node's own local value always wins regardless of
    what's recommended here.

    `forked_from` (design doc §13, round 94/issue #53) is an optional,
    **non-authoritative** pointer to a different board's own `board_id`
    — purely a discoverability hint for readers/other nodes ("this board
    started as a copy of that one"), never verified or enforced, and
    never implies any relationship the protocol actually acts on. A
    fork is simply a new board with its own fresh genesis; each carrying
    node independently decides whether to carry the original, the fork,
    both, or neither, exactly like any other board (round 94's own
    "purely local" framing for orphan/fork handling generally).

    Always signed by `signing_identity` — the origin's *current signing
    key* (round 89), matching `build_endpoint_descriptor`'s own
    signing choice, never the root key directly.
    """
    if default_name_requirement is not None and default_name_requirement not in _VALID_NAME_REQUIREMENTS:
        raise EventError(f"invalid default_name_requirement: {default_name_requirement!r}")

    payload = {
        "origin_fingerprint": origin_fingerprint,
        "board_id": board_id,
        "name": name,
        "created_at": created_at,
    }
    if description is not None:
        payload["description"] = description
    if default_min_read_level is not None:
        payload["default_min_read_level"] = default_min_read_level
    if default_min_write_level is not None:
        payload["default_min_write_level"] = default_min_write_level
    if default_moderated is not None:
        payload["default_moderated"] = default_moderated
    if default_max_post_age_days is not None:
        payload["default_max_post_age_days"] = default_max_post_age_days
    if default_min_age is not None:
        payload["default_min_age"] = default_min_age
    if default_name_requirement is not None:
        payload["default_name_requirement"] = default_name_requirement
    if forked_from is not None:
        payload["forked_from"] = forked_from

    envelope = build_envelope(BOARD_GENESIS_OBJECT_TYPE, payload)
    signature = signing_identity.sign(canonical_bytes(envelope))
    return BoardGenesis(envelope=envelope, signature=signature)


def verify_board_genesis(genesis: BoardGenesis, signing_verify_key: nacl.signing.VerifyKey) -> bool:
    """Verify `genesis`'s signature against the claimed origin's
    *current signing key* — resolving which key that currently is
    (walking `origin_fingerprint`'s `key_transition` chain) is the
    caller's job, same division of responsibility as
    `verify_endpoint_descriptor`."""
    return verify_signature(signing_verify_key, canonical_bytes(genesis.envelope), genesis.signature)


@dataclass(frozen=True)
class BoardPost:
    """
    One signed `board_post` event (design doc §7/§13, round 124): an
    immutable content-creation event (round 90's other event class,
    alongside the mutable per-object chains `KeyTransition`/
    `BoardGenesis` belong to) — content-addressed by this event's own
    envelope hash, causally ordered by `parent_post_id`, nothing to
    project beyond "does it exist." No separate `post_id` field inside
    the payload — `content_id` already is this event's stable identity,
    the same precedent `KeyTransition`/`EndpointDescriptor` already set
    of never storing their own ID inline.

    `payload["author"]` is round 90's tagged union — but round 124
    confirmed only the `node_vouched_user` tag gets a real build/verify
    path this round (see `build_board_post`'s own docstring); `user_key`
    and `node` are named in the design but have no code path here.
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
    def from_dict(cls, data: dict) -> "BoardPost":
        return cls(envelope=data["envelope"], signature=base64.b64decode(data["signature"]))


def build_board_post(
    *,
    signing_identity: Identity,
    home_node_fingerprint: str,
    local_user_id: str,
    board_id: str,
    subject: str,
    body: str,
    created_at: str,
    parent_post_id: str | None = None,
    nonce: str | None = None,
) -> BoardPost:
    """
    Build and sign one `board_post` event, per design doc round 124.

    Only the `node_vouched_user` author tier is built this round
    (design doc round 124, confirmed with Thiesi): the server holds no
    user's private personal key today, and passwordless/keypair login
    itself isn't implemented yet, so there's no session-level signing
    capacity to hang a genuine `user_key`-tier signature off of —
    building that now would mean designing a separate, currently-
    undesigned feature (client-side signing at compose time) just to
    unblock a payload shape. `local_user_id` is the posting user's
    plain username — already immutable post-creation (design doc §5)
    and already the exact opaque local identifier
    `netbbs.boards.posts.create_post` falls back to when an author has
    no personal keypair fingerprint.

    Always signed by `signing_identity` — the posting user's *home
    node's* current signing key (round 89), matching round 90's own
    framing exactly ("their content carries their home node's
    signature, not a signature of their own") — never the user's own
    key, since a password-only user has none. `board_id` is the
    board's existing local ID, the same value its own `BoardGenesis`
    announced (see that class's docstring) — never separately minted
    here.

    `nonce` distinguishes two genuinely identical posting actions
    submitted in the same instant (round 90), which would otherwise
    hash identically and look like a dedup hit; auto-generated if not
    given, since callers have no reason to manage this themselves.
    """
    payload = {
        "board_id": board_id,
        "author": {
            "kind": _NODE_VOUCHED_USER_AUTHOR_KIND,
            "home_node_fingerprint": home_node_fingerprint,
            "local_user_id": local_user_id,
        },
        "subject": subject,
        "body": body,
        "created_at": created_at,
        "nonce": nonce if nonce is not None else secrets.token_hex(16),
    }
    if parent_post_id is not None:
        payload["parent_post_id"] = parent_post_id

    envelope = build_envelope(BOARD_POST_OBJECT_TYPE, payload)
    signature = signing_identity.sign(canonical_bytes(envelope))
    return BoardPost(envelope=envelope, signature=signature)


def verify_board_post(post: BoardPost, signing_verify_key: nacl.signing.VerifyKey) -> bool:
    """Verify `post`'s signature against the claimed home node's
    *current signing key* — resolving which key that currently is
    (walking `payload["author"]["home_node_fingerprint"]`'s
    `key_transition` chain) is the caller's job, same division of
    responsibility as `verify_endpoint_descriptor`/`verify_board_
    genesis`."""
    return verify_signature(signing_verify_key, canonical_bytes(post.envelope), post.signature)


@dataclass(frozen=True)
class BoardPostEdit:
    """
    One signed `board_post_edit` event (design doc §7/§13, round 129):
    a self-authored revision of an existing `board_post` — never a
    moderator edit, never a tombstone, both explicitly deferred to
    Phase 6 (design doc round 129: the local model has no "delete your
    own post" capability to propagate even for the simple tombstone
    case, and a moderator edit needs grant verification that doesn't
    exist yet).

    Unlike `BoardGenesis`/`KeyTransition`, this is **never** the head of
    its own chain — `payload["previous_event_id"]` is always present,
    never omitted (round 90 point 6 only applies to a chain's first
    entry, and the immutable `BoardPost` this extends already fills
    that role). `payload["root_post_id"]` is the original `BoardPost`'s
    content_id, stable across the whole edit chain — the same concept
    `netbbs.boards.posts.Post.root_post_id` already tracks locally.
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
    def from_dict(cls, data: dict) -> "BoardPostEdit":
        return cls(envelope=data["envelope"], signature=base64.b64decode(data["signature"]))


def build_board_post_edit(
    *,
    signing_identity: Identity,
    author: dict,
    board_id: str,
    root_post_id: str,
    previous_event_id: str,
    subject: str,
    body: str,
    created_at: str,
    nonce: str | None = None,
) -> BoardPostEdit:
    """
    Build and sign one `board_post_edit` event, per design doc round
    129.

    `author` is copied **verbatim** from the root `board_post`'s own
    `payload["author"]` dict by the caller (`netbbs.link.boards`) —
    never reconstructed from the editor's identity here, which
    guarantees an exact match with the root post's author by
    construction rather than by separately re-deriving the same fields
    and hoping they stay in sync. Whether this edit is actually
    self-authored (as opposed to a moderator edit, unsupported this
    round) is decided by the caller before ever reaching this function
    — see `netbbs.link.boards.queue_board_post_edit_if_linked`.

    `root_post_id`/`previous_event_id` are both always required (never
    optional the way a chain's first entry's predecessor field is
    elsewhere) — a `board_post_edit` is never the head of its own
    chain. Signed by `signing_identity` — the same home node's current
    signing key `build_board_post` itself uses, never a personal user
    key.
    """
    payload = {
        "board_id": board_id,
        "root_post_id": root_post_id,
        "previous_event_id": previous_event_id,
        "author": author,
        "subject": subject,
        "body": body,
        "created_at": created_at,
        "nonce": nonce if nonce is not None else secrets.token_hex(16),
    }

    envelope = build_envelope(BOARD_POST_EDIT_OBJECT_TYPE, payload)
    signature = signing_identity.sign(canonical_bytes(envelope))
    return BoardPostEdit(envelope=envelope, signature=signature)


def verify_board_post_edit(edit: BoardPostEdit, signing_verify_key: nacl.signing.VerifyKey) -> bool:
    """Verify `edit`'s signature against the claimed home node's
    *current signing key* — same division of responsibility as
    `verify_board_post`. Checking that `edit`'s author actually matches
    the root post's author (the mechanical expression of "self-authored
    only") is the caller's job (`netbbs.link.protocol.LinkNode.handle_
    events`), since it needs the root post's own payload, not just this
    edit's."""
    return verify_signature(signing_verify_key, canonical_bytes(edit.envelope), edit.signature)


@dataclass(frozen=True)
class BoardOriginTransferOffer:
    """
    One signed `board_origin_transfer_offer` event (design doc §13,
    round 94/issue #53): the *first* half of a mutual-consent origin
    handoff — a board's current origin proposing that a different node
    become the new one. Alone, this changes nothing: every other node
    keeps trusting the *old* origin until the matching
    `BoardOriginTransferAccepted` is also seen (round 94's own framing,
    directly reusing `netbbs.chat.membership`'s "an invitation alone
    never creates membership" pattern).

    Extends the board's own lifecycle chain the same way `BoardPostEdit`
    extends a post's content chain — `payload["previous_event_id"]` is
    always present, referencing the chain's current head (the board's
    own `BoardGenesis.content_id` for a board's first-ever transfer, or
    a prior `BoardOriginTransferAccepted.content_id` for a later one).
    Deliberately simple, not a general revocable-offer state machine: at
    most one outstanding offer may exist per board at a time
    (`netbbs.link.protocol.LinkNode.pending_origin_transfers` enforces
    this) — there is no way to cancel/retarget an outstanding offer in
    this slice, a known, accepted limitation rather than a gap found
    late, matching `link_message`'s own "route selection... deliberately
    not part of this slice" precedent.
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
    def from_dict(cls, data: dict) -> "BoardOriginTransferOffer":
        return cls(envelope=data["envelope"], signature=base64.b64decode(data["signature"]))


def build_board_origin_transfer_offer(
    *,
    signing_identity: Identity,
    board_id: str,
    previous_event_id: str,
    old_origin_fingerprint: str,
    new_origin_fingerprint: str,
    created_at: str,
    nonce: str | None = None,
) -> BoardOriginTransferOffer:
    """
    Build and sign one `board_origin_transfer_offer` event, per design
    doc round 94.

    Always signed by `signing_identity` — the *current* origin's own
    current signing key, matching `build_board_genesis`'s own signing
    choice. `old_origin_fingerprint` is included explicitly (not merely
    implied by who signed this) for the same cross-check reason
    `build_board_genesis`'s own `origin_fingerprint` field is: a
    verifier can confirm it matches the board's current origin without
    trusting the signer's claim alone.
    """
    payload = {
        "board_id": board_id,
        "previous_event_id": previous_event_id,
        "old_origin_fingerprint": old_origin_fingerprint,
        "new_origin_fingerprint": new_origin_fingerprint,
        "created_at": created_at,
        "nonce": nonce if nonce is not None else secrets.token_hex(16),
    }

    envelope = build_envelope(BOARD_ORIGIN_TRANSFER_OFFER_OBJECT_TYPE, payload)
    signature = signing_identity.sign(canonical_bytes(envelope))
    return BoardOriginTransferOffer(envelope=envelope, signature=signature)


def verify_board_origin_transfer_offer(
    offer: BoardOriginTransferOffer, signing_verify_key: nacl.signing.VerifyKey
) -> bool:
    """Verify `offer`'s signature against the claimed current origin's
    *current signing key* — resolving which key that currently is, and
    confirming the sender actually *is* the board's current origin, are
    both the caller's job (`netbbs.link.protocol.LinkNode.handle_
    events`), same division of responsibility as `verify_board_post_
    edit`."""
    return verify_signature(signing_verify_key, canonical_bytes(offer.envelope), offer.signature)


@dataclass(frozen=True)
class BoardOriginTransferAccepted:
    """
    One signed `board_origin_transfer_accepted` event (design doc §13,
    round 94/issue #53): the *second*, consent-completing half of an
    origin handoff — signed by the *new* origin, referencing the
    specific offer it accepts. Only once this is seen (never from the
    offer alone) does `netbbs.link.protocol.LinkNode.board_origin`
    actually flip which fingerprint is authoritative for the board —
    the mechanical expression of "mutual consent" round 94 requires.

    `payload["previous_event_id"]` is always the accepted offer's own
    `content_id` — an acceptance is never the head of its own chain, and
    never accepts anything other than the single currently-outstanding
    offer for its board (see `BoardOriginTransferOffer`'s own docstring
    for why at most one can exist at a time).
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
    def from_dict(cls, data: dict) -> "BoardOriginTransferAccepted":
        return cls(envelope=data["envelope"], signature=base64.b64decode(data["signature"]))


def build_board_origin_transfer_accepted(
    *,
    signing_identity: Identity,
    board_id: str,
    previous_event_id: str,
    new_origin_fingerprint: str,
    created_at: str,
    nonce: str | None = None,
) -> BoardOriginTransferAccepted:
    """
    Build and sign one `board_origin_transfer_accepted` event, per
    design doc round 94.

    Always signed by `signing_identity` — the *new* origin's own current
    signing key. `previous_event_id` is always the offer's own
    `content_id` (see `BoardOriginTransferAccepted`'s own docstring).
    `new_origin_fingerprint` is included explicitly, matching `board_
    origin_transfer_offer`'s own reasoning, so a verifier can confirm it
    matches both the offer's own claim and the signer's identity without
    trusting either alone.
    """
    payload = {
        "board_id": board_id,
        "previous_event_id": previous_event_id,
        "new_origin_fingerprint": new_origin_fingerprint,
        "created_at": created_at,
        "nonce": nonce if nonce is not None else secrets.token_hex(16),
    }

    envelope = build_envelope(BOARD_ORIGIN_TRANSFER_ACCEPTED_OBJECT_TYPE, payload)
    signature = signing_identity.sign(canonical_bytes(envelope))
    return BoardOriginTransferAccepted(envelope=envelope, signature=signature)


def verify_board_origin_transfer_accepted(
    accepted: BoardOriginTransferAccepted, signing_verify_key: nacl.signing.VerifyKey
) -> bool:
    """Verify `accepted`'s signature against the claimed new origin's
    *current signing key* — resolving which key that currently is, and
    confirming the sender actually *is* the offer's named new origin,
    are both the caller's job, same division of responsibility as
    `verify_board_origin_transfer_offer`."""
    return verify_signature(signing_verify_key, canonical_bytes(accepted.envelope), accepted.signature)


@dataclass(frozen=True)
class LinkMessage:
    """
    One signed `link_message` event (design doc §7, round 93): Link's
    extension of local mail, addressed to exactly one recipient node —
    not gossiped to "everyone carrying this board" the way `board_post`
    is. Always signed by the sender's *home node's* current signing key
    (round 89), matching `build_board_post`'s own precedent exactly:
    `payload["sender"]` is the same `node_vouched_user` tagged union,
    since a password-only user has no personal signing key of their own
    to sign with either.

    `payload["ciphertext"]` is opaque here — this class and its
    `build_link_message`/`verify_link_message` only sign/verify the
    envelope; deciding which confidentiality tier applies and actually
    sealing the plaintext (`netbbs.identity.encryption.encrypt_for`) is
    the caller's job (`netbbs.link.mail`, not yet built). Unlike
    `board_post`, no `nonce` field: `SealedBox` embeds a fresh ephemeral
    sender key on every call, so the ciphertext -- and therefore this
    event's own content_id -- already differs between two otherwise-
    identical messages without one.
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
    def from_dict(cls, data: dict) -> "LinkMessage":
        return cls(envelope=data["envelope"], signature=base64.b64decode(data["signature"]))


def build_link_message(
    *,
    signing_identity: Identity,
    home_node_fingerprint: str,
    local_user_id: str,
    recipient_home_node_fingerprint: str,
    recipient_local_user_id: str,
    confidentiality_tier: str,
    ciphertext: bytes,
    created_at: str,
) -> LinkMessage:
    """
    Build and sign one `link_message` event, per design doc round 93.

    `ciphertext` must already be sealed by the caller (`netbbs.identity.
    encryption.encrypt_for`, called against whichever key
    `confidentiality_tier` names) — this function has no opinion on
    which tier applies to a given recipient, only on producing a validly
    shaped, signed envelope around whatever ciphertext it's handed.
    `confidentiality_tier` is one of `"tier1_home_node_key"` (the
    ciphertext is sealed to the recipient's *home node's* derived
    encryption key) or `"tier2_personal_key"` (sealed to the recipient's
    own personal key) — recorded so the receiving node knows which of
    its own identities to decrypt with, without guessing.

    Always signed by `signing_identity` — the sending user's home node's
    current signing key (round 89), never the user's own key, matching
    `build_board_post`'s identical reasoning.
    """
    if confidentiality_tier not in _VALID_CONFIDENTIALITY_TIERS:
        raise EventError(f"invalid confidentiality_tier: {confidentiality_tier!r}")

    payload = {
        "sender": {
            "kind": _NODE_VOUCHED_USER_AUTHOR_KIND,
            "home_node_fingerprint": home_node_fingerprint,
            "local_user_id": local_user_id,
        },
        "recipient": {
            "home_node_fingerprint": recipient_home_node_fingerprint,
            "local_user_id": recipient_local_user_id,
        },
        "confidentiality_tier": confidentiality_tier,
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        "created_at": created_at,
    }

    envelope = build_envelope(LINK_MESSAGE_OBJECT_TYPE, payload)
    signature = signing_identity.sign(canonical_bytes(envelope))
    return LinkMessage(envelope=envelope, signature=signature)


def verify_link_message(message: LinkMessage, signing_verify_key: nacl.signing.VerifyKey) -> bool:
    """Verify `message`'s signature against the claimed sender's home
    node's *current signing key* — resolving which key that currently is
    (walking `payload["sender"]["home_node_fingerprint"]`'s
    `key_transition` chain) is the caller's job, same division of
    responsibility as `verify_board_post`."""
    return verify_signature(signing_verify_key, canonical_bytes(message.envelope), message.signature)


@dataclass(frozen=True)
class LinkMessageAccepted:
    """
    One signed `link_message_accepted` event (design doc §7, round 93):
    the recipient's node vouching that it placed a specific
    `link_message` (`payload["message_content_id"]`) into that user's
    local mailbox. A transport-level HTTP ACK only means the bytes
    arrived (round 93's own distinction) — this is the separate,
    explicit, user-level delivery confirmation the sender's node can
    show the sending user.

    Always signed by the *recipient's own* current signing key, never
    the original sender's — this event originates on the opposite side
    of the exchange from `LinkMessage` itself.
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
    def from_dict(cls, data: dict) -> "LinkMessageAccepted":
        return cls(envelope=data["envelope"], signature=base64.b64decode(data["signature"]))


def build_link_message_accepted(
    *,
    signing_identity: Identity,
    recipient_node_fingerprint: str,
    message_content_id: str,
    created_at: str,
) -> LinkMessageAccepted:
    """
    Build and sign one `link_message_accepted` event, per design doc
    round 93. `recipient_node_fingerprint` is included explicitly (not
    merely implied by "whoever signed this") so a verifier can cross-
    check it against whichever peer's transition chain it resolved the
    signing key from — same reasoning `build_board_genesis`'s own
    `origin_fingerprint` field already documents.
    """
    payload = {
        "recipient_node_fingerprint": recipient_node_fingerprint,
        "message_content_id": message_content_id,
        "created_at": created_at,
    }
    envelope = build_envelope(LINK_MESSAGE_ACCEPTED_OBJECT_TYPE, payload)
    signature = signing_identity.sign(canonical_bytes(envelope))
    return LinkMessageAccepted(envelope=envelope, signature=signature)


def verify_link_message_accepted(
    accepted: LinkMessageAccepted, signing_verify_key: nacl.signing.VerifyKey
) -> bool:
    """Verify `accepted`'s signature against the claimed recipient
    node's *current signing key* — same division of responsibility as
    `verify_link_message`."""
    return verify_signature(signing_verify_key, canonical_bytes(accepted.envelope), accepted.signature)


@dataclass(frozen=True)
class LinkMessageBounced:
    """
    One signed `link_message_bounced` event (design doc §7, round 93):
    the recipient's node explicitly refusing a specific `link_message`
    (`payload["message_content_id"]`) with a named `payload["reason"]`
    (`"mailbox_full"`, `"blocked_sender"`, or `"unknown_recipient"`) —
    round 93's own requirement that a rejection is a distinct, explicit
    signed event rather than silence, so the sender gets a specific
    reason instead of an ambiguous timeout.

    Always signed by the *recipient's own* current signing key, same as
    `LinkMessageAccepted`.
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
    def from_dict(cls, data: dict) -> "LinkMessageBounced":
        return cls(envelope=data["envelope"], signature=base64.b64decode(data["signature"]))


def build_link_message_bounced(
    *,
    signing_identity: Identity,
    recipient_node_fingerprint: str,
    message_content_id: str,
    reason: str,
    created_at: str,
) -> LinkMessageBounced:
    """Build and sign one `link_message_bounced` event, per design doc
    round 93. `reason` must be one of the three named in this class's
    own docstring."""
    if reason not in _VALID_BOUNCE_REASONS:
        raise EventError(f"invalid bounce reason: {reason!r}")

    payload = {
        "recipient_node_fingerprint": recipient_node_fingerprint,
        "message_content_id": message_content_id,
        "reason": reason,
        "created_at": created_at,
    }
    envelope = build_envelope(LINK_MESSAGE_BOUNCED_OBJECT_TYPE, payload)
    signature = signing_identity.sign(canonical_bytes(envelope))
    return LinkMessageBounced(envelope=envelope, signature=signature)


def verify_link_message_bounced(
    bounced: LinkMessageBounced, signing_verify_key: nacl.signing.VerifyKey
) -> bool:
    """Verify `bounced`'s signature against the claimed recipient
    node's *current signing key* — same division of responsibility as
    `verify_link_message`."""
    return verify_signature(signing_verify_key, canonical_bytes(bounced.envelope), bounced.signature)
