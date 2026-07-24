"""
Canonical NetBBS Link event envelope (design doc §7).

The outer envelope shape is `netbbs_protocol`/
`object_type`/`payload`. The semantic model is event
chains with head pointers, rather than per-feature special-casing. The
byte-level canonicalization rule reuses
`netbbs.boards.content_id.canonical_json_bytes` rather than a second
implementation. The concrete event types are: `key_transition`, the
event type needed to unblock the node key-lifecycle work
(`netbbs.link.node_identity`); `endpoint_descriptor` (design doc §12),
needed to unblock the handshake/gossip protocol code
(`netbbs.link.protocol`); `board_genesis` and
`board_post` (design doc §13/§7, the Phase 3 board-related event
types) — see each type's own docstring below for the design doc
decisions they encode; and `board_post_edit` —
self-authored edits only; moderator edits and tombstones
stay deferred to Phase 6 (design doc).

Design doc adds `link_message`, `link_message_accepted`, and
`link_message_bounced` (Link's extension of local mail, §7). This
implementation slice covers building/signing/verifying the three
envelope shapes only — protocol-level acceptance rules
(`netbbs.link.protocol.LinkNode.handle_events`), the delivery/routing
mechanism that actually reaches a specific recipient node, and
`link_message_expired` (which depends on that same routing/retry
design not yet settled) are deliberately not part of this slice; see
each class's own docstring for the boundary.

Design doc/issue #53 adds `board_origin_transfer_offer` and
`board_origin_transfer_accepted` (§13's origin-succession policy) — the
mutual-consent pair a board's current origin and a prospective new one
exchange to hand off authority, plus an optional `forked_from` pointer
on `board_genesis` itself. Orphan detection needs no event type of its
own — it's a computed property of the origin's existing key-transition
chain (`netbbs.link.node_identity.resolve_current_operational_key`),
not a signal on the wire (the design doc: "no cryptographic proof an origin
is gone versus merely offline," so no node's observation gets an
automatic network-wide effect).

Design doc §12/issue #58 adds `relay_consent_request` and
`relay_consent_response` — the signed pair an outgoing-only node and a
candidate relay exchange to establish relay consent. Structurally the
odd one out among everything above: every other pair here is either
gossiped (gains no reply, just accepted-or-not) or, for the mutual-
consent pairs, exchanged as two independent gossiped events. Relay
consent instead needs a *synchronous* reply, because the requester is
by definition someone who can never be dialed back — see `netbbs.link.
transport`'s dedicated `/relay-consent` route rather than the general
`/events` push.

No other event type (moderator grants, tombstones, etc.) is specified
here yet — each gets its own payload-shape decision when it's actually
being built, following this same envelope pattern.
"""

from __future__ import annotations

import base64
import json
import secrets
from dataclasses import dataclass
from typing import Any

import nacl.signing

from netbbs.boards.content_id import canonical_json_bytes, compute_content_id
from netbbs.identity.keys import Identity, verify_signature

# Versioning mandatory from the first byte, not inferred.
NETBBS_PROTOCOL_VERSION = 1

KEY_TRANSITION_OBJECT_TYPE = "key_transition"

# A node's signed, periodically-refreshed reachability claim
# (design doc §12).
ENDPOINT_DESCRIPTOR_OBJECT_TYPE = "endpoint_descriptor"

# The signed announcement putting an existing local board
# into Link scope, and an individual Link-native post on one.
BOARD_GENESIS_OBJECT_TYPE = "board_genesis"
BOARD_POST_OBJECT_TYPE = "board_post"

# A self-authored edit to an existing board_post -- never a
# moderator edit or a tombstone (design doc).
BOARD_POST_EDIT_OBJECT_TYPE = "board_post_edit"

# Design doc/issue #53: the mutual-consent origin-succession
# pair -- the current origin's handoff offer, and the new origin's
# acceptance. Neither alone changes anything (see each class's own
# docstring).
BOARD_ORIGIN_TRANSFER_OFFER_OBJECT_TYPE = "board_origin_transfer_offer"
BOARD_ORIGIN_TRANSFER_ACCEPTED_OBJECT_TYPE = "board_origin_transfer_accepted"

# Design doc §9.6, issue #87: the channel-side counterpart to board_
# genesis/board_post -- structurally identical minus what doesn't apply
# (no edit chain; channel messages have no local edit concept at all).
# No channel_origin_transfer_offer/_accepted pair yet -- origin succession
# for channels is reused by reference (§9.4's model, unchanged) rather
# than built in this issue; see ChannelGenesis's own docstring.
CHANNEL_GENESIS_OBJECT_TYPE = "channel_genesis"
CHANNEL_MESSAGE_OBJECT_TYPE = "channel_message"

# Design doc: Link's extension of local mail. A signed message
# to one specific recipient node, and the two acknowledgement shapes the
# recipient's node sends back toward the sender's. `link_message_expired`
# is named in the design doc but not built yet -- see module docstring.
LINK_MESSAGE_OBJECT_TYPE = "link_message"
LINK_MESSAGE_ACCEPTED_OBJECT_TYPE = "link_message_accepted"
LINK_MESSAGE_BOUNCED_OBJECT_TYPE = "link_message_bounced"

# Design doc §12/issue #58: the signed request/response pair an
# outgoing-only node and a candidate relay exchange to establish relay
# consent. Unlike every other pair above, these are never gossiped or
# appended to a chain -- a synchronous round trip over a dedicated route
# (`netbbs.link.transport`'s `/relay-consent`), the request going out and
# the response coming back on the *same* HTTP call, the only shape that
# works for an outgoing-only requester who can never be dialed back (see
# each class's own docstring).
RELAY_CONSENT_REQUEST_OBJECT_TYPE = "relay_consent_request"
RELAY_CONSENT_RESPONSE_OBJECT_TYPE = "relay_consent_response"

_VALID_PURPOSES = ("signing", "transport")
_VALID_ACTIONS = ("authorize", "revoke")
_VALID_NAME_REQUIREMENTS = ("verified", "verified_and_displayed")

# The only `board_post` author tag with a real build/verify
# path — see `build_board_post`'s docstring for why
# `user_key`/`node` are named in the design but not built yet. The
# `link_message` sender reuses the same tag for the same reason.
_NODE_VOUCHED_USER_AUTHOR_KIND = "node_vouched_user"

# Design doc's two confidentiality tiers -- which key a
# `link_message`'s ciphertext is sealed to. See `netbbs.identity.
# encryption` for the actual derive-and-seal mechanism.
_TIER1_HOME_NODE_KEY = "tier1_home_node_key"
_TIER2_PERSONAL_KEY = "tier2_personal_key"
_VALID_CONFIDENTIALITY_TIERS = (_TIER1_HOME_NODE_KEY, _TIER2_PERSONAL_KEY)

# Design doc's named bounce reasons.
_VALID_BOUNCE_REASONS = ("mailbox_full", "blocked_sender", "unknown_recipient")


class EventError(Exception):
    """Raised for a malformed or invalid canonical event envelope, or an
    invalid `key_transition` specifically (bad purpose/action, or a
    signature that doesn't verify against the claimed root key)."""


def build_envelope(object_type: str, payload: dict) -> dict:
    """
    The canonical envelope shape. Plain construction only — this doesn't
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
    `netbbs.boards.content_id.canonical_json_bytes` directly
    so Link events and Phase 1/2 local content-IDs share exactly one
    canonicalization implementation, not two independently-maintained
    ones that could quietly drift apart.
    """
    return canonical_json_bytes(envelope)


def event_content_id(envelope: dict) -> str:
    """The content-ID of a canonical event envelope — same
    canonicalization as `canonical_bytes`, hashed."""
    return compute_content_id(envelope)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    seen: set[str] = set()
    result: dict = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r} in wire JSON object")
        seen.add(key)
        result[key] = value
    return result


def strict_json_loads(data: str | bytes) -> Any:
    """
    Parse untrusted wire JSON for anything that will be canonicalized,
    signed, or verified (design doc §7.2, issue #11's "duplicate-key ...
    handling" requirement) — rejects any JSON object containing the same
    key twice, at any nesting depth, instead of silently resolving to
    "last one wins" the way `json.loads` does by default.

    A canonical envelope is always built from an already-parsed dict, in
    which duplicate keys are structurally impossible — but two different
    JSON parsers can disagree about *which* value "last one wins" picks,
    so a sender and receiver using different implementations could
    reconstruct two different objects from the same wire bytes while
    each believes it processed "the message." Rejecting outright removes
    the ambiguity instead of leaving it to be resolved differently by
    every language's parser.

    Passed as the `loads=` argument to aiohttp's `Request.json`/
    `ClientResponse.json` (`netbbs.link.transport`) rather than only
    validating after the fact — by the time a plain `json.loads` result
    reaches this module, the duplicate key is already gone.
    """
    return json.loads(data, object_pairs_hook=_reject_duplicate_keys)


@dataclass(frozen=True)
class KeyTransition:
    """
    One signed `key_transition` event (design doc): an
    authorize-or-revoke record for one of a node's two operational keys
    (signing, transport), always signed by that node's root key —
    "any node can verify a signature by walking the transition chain
    back to the root" — never by another operational key, so
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
        its `previous_transition_id` (the head-pointer model)."""
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
    Build and sign one `key_transition` event, per design doc.

    `purpose` is `"signing"` or `"transport"` — the two
    independently-rotatable operational-key chains. `action` is
    `"authorize"` (introduces a new operational key — covers both a
    node's initial bootstrap and a planned rotation) or `"revoke"`
    (marks a specific operational key invalid, the "compromise
    response" case, without necessarily authorizing a replacement in the
    same record). `previous_transition_id` is `None` only for the first
    transition of a given `(subject, purpose)` pair — the event-
    chain/head-pointer model applied to this object type, omitted
    entirely rather than stored as `null`.

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
    One signed `endpoint_descriptor` event (design doc §12):
    a node's own claim about how to reach it — a list of (protocol,
    address, port) tuples for a full peer, or an outgoing-only marker —
    self-authenticated by the node's own *current signing key*,
    not its root key. Unlike `key_transition`, this is
    deliberately **not** a head-pointer chain: the chain model
    exists for state whose *history* matters (audit, "what did this
    used to be"); a stale reachability claim only ever costs a failed
    connection attempt (design doc §12: "connecting to the wrong
    address just fails the handshake"), never a safety issue, so
    "whichever signed descriptor has the newest `created_at` wins" is
    sufficient — no chain-walking machinery needed to interpret one.

    Issue #58 adds an optional `payload["relays"]` — the
    fingerprints of candidates that have granted this node's own
    `relay_consent_request` (`netbbs.link.protocol.LinkNode.relays_
    serving_me`), meaningful for an outgoing-only node specifically
    (design doc §12: "accepted relays are named in the requesting
    node's own signed endpoint descriptor, so any sender resolving that
    address automatically learns where to deliver"). A sender resolving
    this descriptor tries `addresses` first when present, falling back
    to `relays` — see `netbbs.link.sync`'s own send-via-relay logic
    (issue #58 task #25) for that resolution order.
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
    relays: list[str] | None = None,
) -> EndpointDescriptor:
    """
    Build and sign one `endpoint_descriptor` event, per design doc §12
    (issue #58 adds `relays`). `addresses` is a list
    of `{"protocol", "address", "port"}` dicts, tried in order by a peer
    (§12: "multiple simultaneous addresses... supported; peers try them
    in order") — required unless `outgoing_only` is true, matching
    §12's two deployment modes exactly: a full peer *must* publish
    where it can be reached, an outgoing-only node publishes nothing
    but the marker itself (plus, optionally now, `relays`).

    `relays` is a list of relay fingerprints (`netbbs.link.protocol.
    LinkNode.relays_serving_me`'s own keys) that have granted this
    node's `relay_consent_request` — omitted entirely, like `addresses`,
    rather than stored as an empty list, when there are none (this
    format's own "omitted rather than null/empty" convention).
    Never validated against `outgoing_only` here — a full peer
    publishing `relays` alongside `addresses` isn't a contradiction this
    layer needs to police (a redundant reachability path costs nothing,
    same "connecting to the wrong address just fails" reasoning this
    class's own docstring already applies to a stale address).

    Always signed by `signing_identity` — the subject's *current*
    signing key, never the root key directly (root only ever
    signs `key_transition`). `subject_
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
    if relays:
        payload["relays"] = relays

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
    One signed `board_genesis` event (design doc §13): the
    announcement that puts an *existing* local board into Link scope —
    not a separate creation act. The central decision:
    `payload["board_id"]` is the board's existing local content-
    addressed ID (`netbbs.boards.content_id.compute_content_id`), never
    a newly-minted one, so a board created Linked-from-the-start and a
    board Linked years into its local life go through the exact same
    event; only timing differs. This is the head of what the design doc
    describes as an eventual lifecycle chain (later
    closure/transfer entries append, each its own object type) — no
    `previous_event_id` here, since nothing precedes genesis.

    Always signed by the origin node's current *signing* operational
    key, never its root key directly — board creation is
    the canonical `node`-tier authored event,
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
    Build and sign one `board_genesis` event, per design doc.

    `board_id` is the board's *existing* local content-addressed ID
    (see `BoardGenesis`'s own docstring for why this deliberately
    doesn't mint a new one). `origin_fingerprint` is the origin node's
    root fingerprint, included explicitly (not merely implied by
    "whoever signed this") so a verifier can cross-check it against
    whichever peer's transition chain it resolved the signing key from
    — same reasoning `build_endpoint_descriptor` already documents for
    its own `subject_fingerprint` field.

    The six `default_*` fields are optional, non-binding cascading-
    scalar-default recommendations (design doc) — a superset of Community's
    own four `default_*` fields, since a board owns `moderated`/
    `max_post_age_days` directly where a Community doesn't. Each is
    omitted entirely when `None`, never stored as
    `null`; a carrying node's own local value always wins regardless of
    what's recommended here.

    `forked_from` (design doc §13, issue #53) is an optional,
    **non-authoritative** pointer to a different board's own `board_id`
    — purely a discoverability hint for readers/other nodes ("this board
    started as a copy of that one"), never verified or enforced, and
    never implies any relationship the protocol actually acts on. A
    fork is simply a new board with its own fresh genesis; each carrying
    node independently decides whether to carry the original, the fork,
    both, or neither, exactly like any other board (the design doc's own
    "purely local" framing for orphan/fork handling generally).

    Always signed by `signing_identity` — the origin's *current signing
    key*, matching `build_endpoint_descriptor`'s own
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
    One signed `board_post` event (design doc §7/§13): an
    immutable content-creation event (the other event class,
    alongside the mutable per-object chains `KeyTransition`/
    `BoardGenesis` belong to) — content-addressed by this event's own
    envelope hash, causally ordered by `parent_post_id`, nothing to
    project beyond "does it exist." No separate `post_id` field inside
    the payload — `content_id` already is this event's stable identity,
    the same precedent `KeyTransition`/`EndpointDescriptor` already set
    of never storing their own ID inline.

    `payload["author"]` is a tagged union — but
    only the `node_vouched_user` tag gets a real build/verify
    path (see `build_board_post`'s own docstring); `user_key`
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
    Build and sign one `board_post` event, per design doc.

    Only the `node_vouched_user` author tier is built
    (design doc, confirmed with Thiesi): the server holds no
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
    node's* current signing key, matching the design doc's own
    framing exactly ("their content carries their home node's
    signature, not a signature of their own") — never the user's own
    key, since a password-only user has none. `board_id` is the
    board's existing local ID, the same value its own `BoardGenesis`
    announced (see that class's docstring) — never separately minted
    here.

    `nonce` distinguishes two genuinely identical posting actions
    submitted in the same instant, which would otherwise
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
    One signed `board_post_edit` event (design doc §7/§13):
    a self-authored revision of an existing `board_post` — never a
    moderator edit, never a tombstone, both explicitly deferred to
    Phase 6 (design doc: the local model has no "delete your
    own post" capability to propagate even for the simple tombstone
    case, and a moderator edit needs grant verification that doesn't
    exist yet).

    Unlike `BoardGenesis`/`KeyTransition`, this is **never** the head of
    its own chain — `payload["previous_event_id"]` is always present,
    never omitted (that omission rule only applies to a chain's first
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
    Build and sign one `board_post_edit` event, per design doc.

    `author` is copied **verbatim** from the root `board_post`'s own
    `payload["author"]` dict by the caller (`netbbs.link.boards`) —
    never reconstructed from the editor's identity here, which
    guarantees an exact match with the root post's author by
    construction rather than by separately re-deriving the same fields
    and hoping they stay in sync. Whether this edit is actually
    self-authored (as opposed to a moderator edit, not yet
    supported) is decided by the caller before ever reaching this function
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
class ChannelGenesis:
    """
    One signed `channel_genesis` event (design doc §9.6, issue #87): the
    channel-side counterpart to `BoardGenesis` -- the announcement that
    puts an *existing* local channel into Link scope, not a separate
    creation act. `payload["channel_id"]` is the channel's existing
    local content-addressed ID (`netbbs.chat.channels.Channel.
    channel_id`), never newly minted, same "promote an existing local
    resource" rule `BoardGenesis` already establishes.

    No `channel_origin_transfer_offer`/`_accepted` pair exists yet --
    origin succession for channels reuses §9.4's model by reference
    (unchanged) if a future issue ever needs it, rather than being
    built here; this issue's own scope is genesis, promotion,
    materialization, and message propagation only.

    Always signed by the origin node's current *signing* operational
    key, matching `BoardGenesis` exactly.
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
    def from_dict(cls, data: dict) -> "ChannelGenesis":
        return cls(envelope=data["envelope"], signature=base64.b64decode(data["signature"]))


def build_channel_genesis(
    *,
    signing_identity: Identity,
    origin_fingerprint: str,
    channel_id: str,
    name: str,
    created_at: str,
    description: str | None = None,
    default_min_level: int | None = None,
    default_min_age: int | None = None,
    default_name_requirement: str | None = None,
) -> ChannelGenesis:
    """
    Build and sign one `channel_genesis` event, per design doc §9.6.

    `channel_id` is the channel's *existing* local content-addressed ID
    (see `ChannelGenesis`'s own docstring). The three `default_*` fields
    are optional, non-binding cascading-scalar-default recommendations,
    the channel-side subset of `build_board_genesis`'s own six -- no
    `default_min_write_level`/`default_moderated`/
    `default_max_post_age_days` equivalents, since `Channel` has none of
    those settings to recommend a default for. Each is omitted entirely
    when `None`, never stored as `null`; a carrying node's own local
    value always wins regardless of what's recommended here, same rule
    `build_board_genesis` already states.
    """
    if default_name_requirement is not None and default_name_requirement not in _VALID_NAME_REQUIREMENTS:
        raise EventError(f"invalid default_name_requirement: {default_name_requirement!r}")

    payload = {
        "origin_fingerprint": origin_fingerprint,
        "channel_id": channel_id,
        "name": name,
        "created_at": created_at,
    }
    if description is not None:
        payload["description"] = description
    if default_min_level is not None:
        payload["default_min_level"] = default_min_level
    if default_min_age is not None:
        payload["default_min_age"] = default_min_age
    if default_name_requirement is not None:
        payload["default_name_requirement"] = default_name_requirement

    envelope = build_envelope(CHANNEL_GENESIS_OBJECT_TYPE, payload)
    signature = signing_identity.sign(canonical_bytes(envelope))
    return ChannelGenesis(envelope=envelope, signature=signature)


def verify_channel_genesis(genesis: ChannelGenesis, signing_verify_key: nacl.signing.VerifyKey) -> bool:
    """Verify `genesis`'s signature against the claimed origin's
    *current signing key* -- same division of responsibility as
    `verify_board_genesis`."""
    return verify_signature(signing_verify_key, canonical_bytes(genesis.envelope), genesis.signature)


@dataclass(frozen=True)
class ChannelMessage:
    """
    One signed `channel_message` event (design doc §9.6, issue #87): an
    immutable, single-shot chat message, the channel-side counterpart to
    `BoardPost` minus reply structure -- channel scrollback is flat and
    chronological, never threaded, so there is no `parent_post_id`
    equivalent. No `subject` either -- chat messages don't have one.

    `payload["author"]` is the same tagged union `BoardPost` uses -- only
    `node_vouched_user` has a real build/verify path, for the identical
    reason (see `build_board_post`'s own docstring).
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
    def from_dict(cls, data: dict) -> "ChannelMessage":
        return cls(envelope=data["envelope"], signature=base64.b64decode(data["signature"]))


def build_channel_message(
    *,
    signing_identity: Identity,
    home_node_fingerprint: str,
    local_user_id: str,
    channel_id: str,
    body: str,
    created_at: str,
    nonce: str | None = None,
) -> ChannelMessage:
    """
    Build and sign one `channel_message` event, per design doc §9.6.
    Mirrors `build_board_post` minus `subject`/`parent_post_id` -- see
    that function's own docstring for the author-tier and signing-key
    reasoning, unchanged here.
    """
    payload = {
        "channel_id": channel_id,
        "author": {
            "kind": _NODE_VOUCHED_USER_AUTHOR_KIND,
            "home_node_fingerprint": home_node_fingerprint,
            "local_user_id": local_user_id,
        },
        "body": body,
        "created_at": created_at,
        "nonce": nonce if nonce is not None else secrets.token_hex(16),
    }

    envelope = build_envelope(CHANNEL_MESSAGE_OBJECT_TYPE, payload)
    signature = signing_identity.sign(canonical_bytes(envelope))
    return ChannelMessage(envelope=envelope, signature=signature)


def verify_channel_message(message: ChannelMessage, signing_verify_key: nacl.signing.VerifyKey) -> bool:
    """Verify `message`'s signature against the claimed home node's
    *current signing key* -- same division of responsibility as
    `verify_board_post`."""
    return verify_signature(signing_verify_key, canonical_bytes(message.envelope), message.signature)


@dataclass(frozen=True)
class BoardOriginTransferOffer:
    """
    One signed `board_origin_transfer_offer` event (design doc §13,
    issue #53): the *first* half of a mutual-consent origin
    handoff — a board's current origin proposing that a different node
    become the new one. Alone, this changes nothing: every other node
    keeps trusting the *old* origin until the matching
    `BoardOriginTransferAccepted` is also seen (the design doc's own framing,
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
    doc.

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
    issue #53): the *second*, consent-completing half of an
    origin handoff — signed by the *new* origin, referencing the
    specific offer it accepts. Only once this is seen (never from the
    offer alone) does `netbbs.link.protocol.LinkNode.board_origin`
    actually flip which fingerprint is authoritative for the board —
    the mechanical expression of "mutual consent" the design doc requires.

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
    design doc.

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
    One signed `link_message` event (design doc §7): Link's
    extension of local mail, addressed to exactly one recipient node —
    not gossiped to "everyone carrying this board" the way `board_post`
    is. Always signed by the sender's *home node's* current signing key,
    matching `build_board_post`'s own precedent exactly:
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
    Build and sign one `link_message` event, per design doc.

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
    current signing key, never the user's own key, matching
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
    One signed `link_message_accepted` event (design doc §7):
    the recipient's node vouching that it placed a specific
    `link_message` (`payload["message_content_id"]`) into that user's
    local mailbox. A transport-level HTTP ACK only means the bytes
    arrived (the design doc's own distinction) — this is the separate,
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
    Build and sign one `link_message_accepted` event, per design doc.
    `recipient_node_fingerprint` is included explicitly (not
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
    One signed `link_message_bounced` event (design doc §7):
    the recipient's node explicitly refusing a specific `link_message`
    (`payload["message_content_id"]`) with a named `payload["reason"]`
    (`"mailbox_full"`, `"blocked_sender"`, or `"unknown_recipient"`) —
    the design doc's own requirement that a rejection is a distinct, explicit
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
    """Build and sign one `link_message_bounced` event, per design doc.
    `reason` must be one of the three named in this class's
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


@dataclass(frozen=True)
class RelayConsentRequest:
    """
    One signed `relay_consent_request` event (design doc §12,
    issue #58): an outgoing-only node's signed ask that a specific
    candidate — `payload["relay_fingerprint"]` — relay for it. Always
    signed by the requester's own current signing key; `payload[
    "requester_fingerprint"]` is included explicitly (not merely implied
    by whoever signed this or whichever URL it was POSTed to), matching
    `LinkMessage`'s own reasoning for its explicit `sender`/`recipient`
    fields — a verifier can cross-check the claim against the actual
    caller rather than trusting position alone.

    Not part of any chain (no `previous_event_id`) — a fresh, disposable
    ask each time, matching `EndpointDescriptor`'s own "no chain-walking
    machinery needed" reasoning: a stale or duplicate request costs
    nothing beyond a redundant round trip, never a safety issue.
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
    def from_dict(cls, data: dict) -> "RelayConsentRequest":
        return cls(envelope=data["envelope"], signature=base64.b64decode(data["signature"]))


def build_relay_consent_request(
    *,
    signing_identity: Identity,
    requester_fingerprint: str,
    relay_fingerprint: str,
    created_at: str,
    nonce: str | None = None,
) -> RelayConsentRequest:
    """
    Build and sign one `relay_consent_request` event, per design doc §12.
    Always signed by `signing_identity` — the requester's own
    current signing key.
    """
    payload = {
        "requester_fingerprint": requester_fingerprint,
        "relay_fingerprint": relay_fingerprint,
        "created_at": created_at,
        "nonce": nonce if nonce is not None else secrets.token_hex(16),
    }
    envelope = build_envelope(RELAY_CONSENT_REQUEST_OBJECT_TYPE, payload)
    signature = signing_identity.sign(canonical_bytes(envelope))
    return RelayConsentRequest(envelope=envelope, signature=signature)


def verify_relay_consent_request(
    request: RelayConsentRequest, signing_verify_key: nacl.signing.VerifyKey
) -> bool:
    """Verify `request`'s signature against the claimed requester's
    *current signing key* — resolving which key that currently is, and
    confirming the caller actually *is* the claimed requester, are both
    the caller's job (`netbbs.link.transport`'s `/relay-consent` route
    handler), same division of responsibility every other `verify_*`
    function in this module already applies."""
    return verify_signature(signing_verify_key, canonical_bytes(request.envelope), request.signature)


@dataclass(frozen=True)
class RelayConsentResponse:
    """
    One signed `relay_consent_response` event (design doc §12,
    issue #58): a candidate's answer to a specific `RelayConsentRequest`
    — accept or decline, per its own local relay-acceptance policy (a
    resource-cap/opt-out decision made by the caller, e.g.
    `netbbs.link.transport`'s route handler; this class only carries the
    already-made decision, same "verification here, policy elsewhere"
    split `netbbs.link.protocol`'s module docstring establishes for
    everything else).

    `payload["request_content_id"]` always names the specific request
    being answered — a response is never free-floating the way a
    request is, so a requester can match a reply to what it actually
    asked even if it has more than one outstanding request in flight at
    once (unlike `BoardOriginTransferOffer`'s "at most one at a time"
    restriction, nothing here limits how many relays a node may be
    simultaneously asking).
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
    def from_dict(cls, data: dict) -> "RelayConsentResponse":
        return cls(envelope=data["envelope"], signature=base64.b64decode(data["signature"]))


def build_relay_consent_response(
    *,
    signing_identity: Identity,
    request_content_id: str,
    relay_fingerprint: str,
    requester_fingerprint: str,
    accepted: bool,
    created_at: str,
    nonce: str | None = None,
) -> RelayConsentResponse:
    """
    Build and sign one `relay_consent_response` event, per design doc
    §12. Always signed by `signing_identity` — the candidate
    relay's own current signing key. `relay_fingerprint`/`requester_
    fingerprint` are both included explicitly, matching `RelayConsent
    Request`'s own reasoning, so a verifier can cross-check both ends of
    the exchange without trusting position (who signed, which route
    answered) alone.
    """
    payload = {
        "request_content_id": request_content_id,
        "relay_fingerprint": relay_fingerprint,
        "requester_fingerprint": requester_fingerprint,
        "accepted": accepted,
        "created_at": created_at,
        "nonce": nonce if nonce is not None else secrets.token_hex(16),
    }
    envelope = build_envelope(RELAY_CONSENT_RESPONSE_OBJECT_TYPE, payload)
    signature = signing_identity.sign(canonical_bytes(envelope))
    return RelayConsentResponse(envelope=envelope, signature=signature)


def verify_relay_consent_response(
    response: RelayConsentResponse, signing_verify_key: nacl.signing.VerifyKey
) -> bool:
    """Verify `response`'s signature against the claimed relay's
    *current signing key* — same division of responsibility as
    `verify_relay_consent_request`."""
    return verify_signature(signing_verify_key, canonical_bytes(response.envelope), response.signature)
