"""
Real `aiohttp`-based transport for `netbbs.link.protocol` (design doc
§11) — the client dial functions and server route handlers
that translate `LinkNode`'s message-passing interface into
actual HTTP+JSON requests over a real socket.

Mirrors `netbbs.net.web.WebServer`'s own `AppRunner`/`TCPSite` start/
stop/`port` lifecycle — the shape every server this codebase stands up
already uses, not a new one invented for Link.

This module is deliberately the *only* place that imports both
`aiohttp` and `netbbs.link.protocol` together — `protocol.py` itself
stays untouched and provably transport-agnostic, matching the
whole point in building it that way. `LinkNode.handle_hello`/
`handle_events` do all the actual verification; this module's job is
only "get bytes to the right place and hand what arrives to the right
method."

Route shape: `POST {LINK_PATH_PREFIX}/hello` (mutual — a peer's own
hello comes back in the response body, matching the design-doc
note on how store-and-forward's *promise* is preserved even though a
successful dial's response can still opportunistically carry a prompt
reply) and `POST {LINK_PATH_PREFIX}/events/{fingerprint}` (gossip push,
`fingerprint` naming whose own events these are — this design only
ever gossips a node's *own* key_transitions, never relays on another's
behalf yet, matching the "no relay from a stranger" scope
note).

**`LinkServer`/`dial_hello` require a `lane: DatabaseLane`** — the only
three call sites in this codebase that
mutate a `LinkNode`'s peer table or event store (`_handle_hello`,
`_handle_events`, and `dial_hello`'s own trailing `handle_hello` call)
persist what changed via `netbbs.link.store`, off the event loop,
after `netbbs.link.protocol`'s own in-memory verification succeeds.
`push_events` is untouched — it never mutates local `LinkNode` state.
See the design doc for the full reasoning on why persistence
lives here rather than inside `netbbs.link.protocol` itself.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import random
from dataclasses import dataclass
from typing import Awaitable, Callable, Sequence
from urllib.parse import urljoin, urlparse

import nacl.bindings
import nacl.exceptions
import nacl.public
import nacl.signing
from aiohttp import ClientError, ClientSession, ClientTimeout, web

from netbbs.link.boards import (
    BoardCarryLimitError,
    materialize_carried_board,
    materialize_carried_board_closure,
    materialize_carried_board_post_moderator_edit,
    materialize_carried_board_post_tombstone,
    materialize_carried_post,
    materialize_carried_post_edit,
    record_board_origin_change,
)
from netbbs.link.channels import (
    ChannelCarryLimitError,
    materialize_carried_channel,
    materialize_carried_channel_message,
)
from netbbs.link.events import (
    BOARD_CLOSURE_OBJECT_TYPE,
    BOARD_GENESIS_OBJECT_TYPE,
    BOARD_ORIGIN_TRANSFER_ACCEPTED_OBJECT_TYPE,
    BOARD_POST_EDIT_OBJECT_TYPE,
    BOARD_POST_MODERATOR_EDIT_OBJECT_TYPE,
    BOARD_POST_OBJECT_TYPE,
    BOARD_POST_TOMBSTONE_OBJECT_TYPE,
    CHANNEL_GENESIS_OBJECT_TYPE,
    CHANNEL_MESSAGE_OBJECT_TYPE,
    FILE_AREA_GENESIS_OBJECT_TYPE,
    FILE_DESCRIPTOR_OBJECT_TYPE,
    LINK_MESSAGE_ACCEPTED_OBJECT_TYPE,
    LINK_MESSAGE_BOUNCED_OBJECT_TYPE,
    LINK_MESSAGE_OBJECT_TYPE,
    BoardClosure,
    BoardGenesis,
    BoardOriginTransferAccepted,
    BoardOriginTransferOffer,
    BoardPost,
    BoardPostEdit,
    BoardPostModeratorEdit,
    BoardPostTombstone,
    ChannelGenesis,
    ChannelMessage,
    FileAreaGenesis,
    FileChunkDescriptor,
    FileDescriptor,
    KeyTransition,
    LinkMessage,
    LinkMessageAccepted,
    LinkMessageBounced,
    RelayConsentRequest,
    RelayConsentResponse,
    build_file_chunk_descriptor,
    build_relay_consent_request,
    build_relay_consent_response,
    strict_json_loads,
    verify_file_chunk_descriptor,
)
from netbbs.link.enforcement import (
    LinkPolicyAction,
    LinkPolicyDecision,
    decide_event_authorship,
    decide_node_action,
    ensure_event_author_subject,
    ensure_node_subject,
)
from netbbs.link.file_transfer import (
    FileTransferError,
    TransferState,
    apply_received_chunk,
    build_chunk_for_serving,
    get_or_create_transfer,
)
from netbbs.link.files import (
    FileAreaCarryLimitError,
    RemoteFile,
    RemoteFileCatalogueLimitError,
    get_remote_file,
    materialize_carried_file_area,
    materialize_carried_file_descriptor,
)
from netbbs.link.mail import apply_link_message_accepted, apply_link_message_bounced, deliver_link_message
from netbbs.link.node_identity import NodeIdentity, resolve_current_operational_key, rotate_operational_key
from netbbs.identity.encryption import derive_encryption_private_key
from netbbs.link.protocol import (
    _MAX_EVENTS_PER_REQUEST,
    FileChunkRequest,
    HelloMessage,
    InventoryRequest,
    LinkNode,
    LinkProtocolError,
    PeerListMessage,
    PeerRecord,
    RealtimeFrame,
    RealtimeIdentityPayload,
    RealtimeReplayWindow,
    build_close_frame,
    build_error_frame,
    build_ping_frame,
    build_pong_frame,
    validate_realtime_frame_payload,
)
from netbbs.link.relay_mailbox import (
    RelayableEnvelope,
    RelayMailboxFullError,
    deposit_relay_mailbox_envelope,
    pickup_relay_mailbox_envelopes,
)
from netbbs.link.store import (
    board_event_diff,
    build_inventory_request,
    channel_event_diff,
    file_area_event_diff,
    save_candidate_descriptor,
    save_event,
    save_peer,
    save_relay_consent,
)
from netbbs.link.trust_wire import (
    MAX_EMBEDDED_EVIDENCE_BYTES,
    TrustPullRequest,
    TrustWireError,
    load_trust_object_page,
    verify_evidence_bytes,
)
from netbbs.net.throttle import LinkRequestThrottle
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import utc_now_iso

_logger = logging.getLogger(__name__)

_LINK_THROTTLE_APP_KEY: web.AppKey[LinkRequestThrottle] = web.AppKey("link_throttle", LinkRequestThrottle)

LINK_PATH_PREFIX = "/link/v1"

_DEFAULT_TIMEOUT_SECONDS = 10.0

# Issue #58: `LinkServer`'s own default resource cap on relay-
# serving when a caller doesn't supply `max_relay_clients` explicitly
# (every test in this codebase predating that parameter, plus any
# caller that doesn't care to tune it) -- `netbbs.net.nodeconfig.
# LinkConfig.max_relay_clients` carries the real, SysOp-adjustable
# value for an actual running node (see `netbbs.__main__`'s own
# `LinkServer(...)` construction).
_DEFAULT_MAX_RELAY_CLIENTS = 20

# Design doc §13.9 (issue #60's third operational slice): same "own
# default, real config value lives in netbbs.net.nodeconfig.LinkConfig"
# split as _DEFAULT_MAX_RELAY_CLIENTS above, for the three quotas added
# this slice that `LinkServer` itself now enforces.
_DEFAULT_MAX_PEERS = 1000
_DEFAULT_MAX_CARRIED_BOARDS = 500
# Design doc §9.6, issue #87: same shape as _DEFAULT_MAX_CARRIED_BOARDS
# above, the channel-side counterpart.
_DEFAULT_MAX_CARRIED_CHANNELS = 500
# Design doc §11, issue #89: same shape, the file-area-side counterpart
# (carried areas) and its own further per-area catalogue-entry bound
# (§13.5's bounded-remote-influence principle, applied to a carrying
# node's own remote_files rows rather than the carried-area count).
_DEFAULT_MAX_CARRIED_FILE_AREAS = 500
_DEFAULT_MAX_REMOTE_FILES_PER_AREA = 5000
# Design doc §11.3, issue #89: how many concurrent chunk-transfer
# `transfer_id`s this node will serve for one requesting peer at a time
# -- bounded per §13.5, tracked in memory only (LinkServer._active_
# transfers_by_peer), never persisted, since serving is otherwise
# stateless per chunk request.
_DEFAULT_MAX_CONCURRENT_FILE_TRANSFERS_PER_PEER = 4

# Turns aiohttp's implicit 1 MiB `client_max_size` default into a
# deliberate, documented value -- sized to comfortably fit `netbbs.link.
# protocol._MAX_EVENTS_PER_REQUEST` (200) worth of events.
_LINK_CLIENT_MAX_SIZE_BYTES = 2 * 1024 * 1024

# Design doc §11.3, issue #89: `file_transfer.build_chunk_for_serving`
# already clamps to its own internal ceiling, but the server also refuses
# a request naming an obviously abusive max_chunk_size outright, the same
# "reject the whole request" idiom other malformed-input rejection in
# this module already uses.
_MAX_ALLOWED_CHUNK_SIZE_BYTES = 1024 * 1024
# fetch_next_file_chunk's own default -- matches netbbs.link.file_
# transfer's internal default exactly (kept as a separate constant
# rather than importing that module's private one across module
# boundaries).
_DEFAULT_FILE_CHUNK_SIZE = 256 * 1024

REALTIME_MAX_CIPHERTEXT_BYTES = 65_535


def encode_realtime_record(ciphertext: bytes) -> bytes:
    """Prefix one bounded Noise ciphertext with its two-byte wire length."""
    if not isinstance(ciphertext, bytes):
        raise LinkTransportError("real-time ciphertext must be bytes")
    if not 1 <= len(ciphertext) <= REALTIME_MAX_CIPHERTEXT_BYTES:
        raise LinkTransportError("real-time ciphertext must be between 1 and 65,535 bytes")
    return len(ciphertext).to_bytes(2, "big") + ciphertext


async def read_realtime_record(reader: asyncio.StreamReader) -> bytes:
    """Read exactly one bounded ciphertext record from a live Link stream."""
    try:
        header = await reader.readexactly(2)
        length = int.from_bytes(header, "big")
        if length == 0:
            raise LinkTransportError("zero-length real-time record")
        return await reader.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        raise LinkTransportError("truncated real-time record") from exc


async def write_realtime_record(writer: asyncio.StreamWriter, ciphertext: bytes) -> None:
    writer.write(encode_realtime_record(ciphertext))
    try:
        await writer.drain()
    except (ConnectionError, OSError) as exc:
        raise LinkTransportError("could not write real-time record") from exc


@web.middleware
async def _rate_limit_middleware(request: web.Request, handler):
    """Design doc §13.9: applied to every route on this server,
    including the two unauthenticated ones (`/hello`, `/peers`) -- a
    stranger's request must be rate-limited before anything else runs,
    not just an already-verified peer's. `request.app["link_throttle"]`
    is `None` when a caller didn't supply one (every test predating this
    middleware, plus any caller that doesn't care to tune it) -- a no-op
    pass-through in that case, matching this project's existing
    opt-in-by-construction convention for every other optional resource
    cap in this module."""
    throttle: LinkRequestThrottle | None = request.app.get(_LINK_THROTTLE_APP_KEY)
    if throttle is not None and not throttle.allow(request.remote):
        return web.json_response({"error": "rate limit exceeded"}, status=429)
    return await handler(request)


async def persist_accepted_events(
    lane: DatabaseLane,
    node: LinkNode,
    accepted: list[str],
    *,
    sender_fingerprint: str,
    max_carried_boards: int | None,
    max_carried_channels: int | None = None,
    max_carried_file_areas: int | None = None,
    max_remote_files_per_area: int | None = None,
    enforce_trust_policy: bool = False,
) -> None:
    """
    Persist and follow up on every content_id `LinkNode.handle_events`
    just returned as newly accepted -- shared by `LinkServer._handle_
    events` (direct push, `sender_fingerprint` is the wire-level peer)
    and `netbbs.link.sync`'s inventory-response handling (issue #85,
    `sender_fingerprint` is whichever peer this node happened to pull
    the response from -- possibly a relay, not the content's own
    author/origin; harmless here, since this parameter only ever feeds
    `link_events.sender_fingerprint` bookkeeping/`materialize_carried_
    post`'s own diagnostic column, never anything `handle_events` has
    already independently verified by the time accepted content reaches
    this function).

    `sender.transitions` growing (a `key_transition` acceptance) is
    **not** persisted here -- callers with a real peer relationship to
    update (`LinkServer._handle_events`) do that themselves afterward,
    since an inventory response has no `key_transition` events to begin
    with (design doc §8.8's board-only scope) and no single
    `sender_fingerprint` here is guaranteed to even be an existing
    `node.peers` entry worth re-saving.
    """
    for content_id in accepted:
        envelope = node.events[content_id]
        object_type = envelope["envelope"]["object_type"]
        if enforce_trust_policy:
            await lane.run(ensure_event_author_subject, envelope)
        # Design doc §9.3/issue #73: board_post/board_post_edit skip
        # the generic save_event dispatch below entirely --
        # materialize_carried_post/_edit each persist the underlying
        # link_events row themselves, in the same transaction as the
        # posts projection, closing the crash window every other
        # object type here still has between save_event and its own
        # follow-up (materialize_carried_board's own docstring notes
        # this same gap, not fixed for genesis).
        if object_type == BOARD_POST_OBJECT_TYPE:
            initial_status = "approved"
            if enforce_trust_policy:
                decision = await lane.run(
                    decide_event_authorship, envelope,
                    transport_peer_fingerprint=sender_fingerprint,
                )
                if decision.requires_approval:
                    initial_status = "pending"
            await lane.run(
                materialize_carried_post, BoardPost.from_dict(envelope),
                sender_fingerprint=sender_fingerprint, initial_status=initial_status,
            )
            continue
        elif object_type == BOARD_POST_EDIT_OBJECT_TYPE:
            await lane.run(
                materialize_carried_post_edit, BoardPostEdit.from_dict(envelope), sender_fingerprint=sender_fingerprint
            )
            continue
        elif object_type == BOARD_POST_MODERATOR_EDIT_OBJECT_TYPE:
            # Design doc §9.5, issue #88: same "skip the generic save_
            # event dispatch" shape as BOARD_POST_EDIT_OBJECT_TYPE above.
            await lane.run(
                materialize_carried_board_post_moderator_edit,
                BoardPostModeratorEdit.from_dict(envelope), sender_fingerprint=sender_fingerprint,
            )
            continue
        elif object_type == BOARD_POST_TOMBSTONE_OBJECT_TYPE:
            await lane.run(
                materialize_carried_board_post_tombstone,
                BoardPostTombstone.from_dict(envelope), sender_fingerprint=sender_fingerprint,
            )
            continue
        elif object_type == CHANNEL_MESSAGE_OBJECT_TYPE:
            # Design doc §9.6, issue #87: same "skip the generic save_
            # event dispatch, materialize does its own link_events
            # insert in the same transaction" shape as board_post above.
            await lane.run(
                materialize_carried_channel_message, ChannelMessage.from_dict(envelope),
                sender_fingerprint=sender_fingerprint,
            )
            continue
        elif object_type == FILE_DESCRIPTOR_OBJECT_TYPE:
            # Design doc §11.2, issue #89: same shape -- catalogue
            # metadata only, into remote_files, never the real files
            # table (see materialize_carried_file_descriptor's own
            # docstring for why).
            try:
                await lane.run(
                    materialize_carried_file_descriptor,
                    FileDescriptor.from_dict(envelope), sender_fingerprint=sender_fingerprint,
                    max_remote_files_per_area=max_remote_files_per_area,
                )
            except RemoteFileCatalogueLimitError as exc:
                _logger.warning("Link sync: %s", exc)
            continue

        await lane.run(
            save_event,
            sender_fingerprint=sender_fingerprint,
            content_id=content_id,
            object_type=object_type,
            envelope=envelope,
        )
        # Link messages (design doc) need real follow-up
        # beyond persisting the envelope -- decrypt/deliver into a
        # local mailbox or bounce, and apply an incoming
        # acknowledgement to the outbound row it's about.
        # Issue #53's carry-materialization gap means board_genesis
        # and board_origin_transfer_accepted both need real follow-up
        # too: a received genesis has nothing a local user could
        # browse without also becoming a real Board row (see
        # materialize_carried_board's own docstring for why this was
        # missing even for a board this node has carried all along),
        # and an accepted transfer must update this node's own
        # locally-materialized copy's current-origin record even when
        # this node was only a bystander to the transfer, not a party
        # to it (see record_board_origin_change's own docstring).
        if object_type == LINK_MESSAGE_OBJECT_TYPE:
            await lane.run(deliver_link_message, envelope, node_identity=node.identity)
        elif object_type == LINK_MESSAGE_ACCEPTED_OBJECT_TYPE:
            await lane.run(apply_link_message_accepted, envelope)
        elif object_type == LINK_MESSAGE_BOUNCED_OBJECT_TYPE:
            await lane.run(apply_link_message_bounced, envelope)
        elif object_type == BOARD_GENESIS_OBJECT_TYPE:
            try:
                await lane.run(
                    materialize_carried_board,
                    BoardGenesis.from_dict(envelope),
                    own_fingerprint=node.identity.fingerprint,
                    max_carried_boards=max_carried_boards,
                )
            except BoardCarryLimitError as exc:
                # Design doc §13.9: the genesis event above is
                # already accepted/persisted (save_event, earlier in
                # this loop) and keeps gossiping normally -- only
                # this node's own local materialization is refused,
                # logged rather than surfaced as a failed request
                # (the peer that pushed it did nothing wrong; this
                # node simply declined to carry one more board).
                _logger.warning("Link sync: %s", exc)
        elif object_type == CHANNEL_GENESIS_OBJECT_TYPE:
            # Design doc §9.6, issue #87: mirrors BOARD_GENESIS_OBJECT_
            # TYPE above exactly, including the same carry-limit
            # tolerance.
            try:
                await lane.run(
                    materialize_carried_channel,
                    ChannelGenesis.from_dict(envelope),
                    own_fingerprint=node.identity.fingerprint,
                    max_carried_channels=max_carried_channels,
                )
            except ChannelCarryLimitError as exc:
                _logger.warning("Link sync: %s", exc)
        elif object_type == BOARD_ORIGIN_TRANSFER_ACCEPTED_OBJECT_TYPE:
            transfer_accepted = BoardOriginTransferAccepted.from_dict(envelope)
            await lane.run(
                record_board_origin_change,
                transfer_accepted.payload["board_id"],
                transfer_accepted.payload["new_origin_fingerprint"],
            )
        elif object_type == BOARD_CLOSURE_OBJECT_TYPE:
            # Design doc §9.5, issue #88: a bystander witnessing someone
            # else's board being closed needs the same local-materialization
            # follow-up BOARD_ORIGIN_TRANSFER_ACCEPTED_OBJECT_TYPE above
            # needs -- the closing origin's own case is handled directly
            # by close_board_if_linked itself.
            await lane.run(materialize_carried_board_closure, BoardClosure.from_dict(envelope))
        elif object_type == FILE_AREA_GENESIS_OBJECT_TYPE:
            # Design doc §11, issue #89: mirrors BOARD_GENESIS_OBJECT_TYPE
            # above exactly, including the same carry-limit tolerance.
            try:
                await lane.run(
                    materialize_carried_file_area,
                    FileAreaGenesis.from_dict(envelope),
                    own_fingerprint=node.identity.fingerprint,
                    max_carried_file_areas=max_carried_file_areas,
                )
            except FileAreaCarryLimitError as exc:
                _logger.warning("Link sync: %s", exc)


class LinkTransportError(Exception):
    """Raised for anything wrong at the transport level: a connection
    failure, a request timeout, a non-200 response, or a response body
    that doesn't parse as the message it was supposed to carry. Kept
    distinct from `LinkProtocolError` (still raised unwrapped, never
    caught here) — that one means "the message arrived fine but didn't
    verify," a different failure a caller may want to handle
    differently (e.g. log-and-drop a hostile peer vs. retry a flaky
    connection)."""


_NOISE_PROTOCOL_NAME = b"Noise_XX_25519_ChaChaPoly_BLAKE2s"
_NOISE_HASHLEN = 32
_NOISE_TAGLEN = nacl.bindings.crypto_aead_chacha20poly1305_ietf_ABYTES


def _noise_hash(data: bytes) -> bytes:
    return hashlib.blake2s(data, digest_size=_NOISE_HASHLEN).digest()


def _noise_hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.blake2s).digest()


def _noise_hkdf(chaining_key: bytes, input_key_material: bytes, outputs: int) -> tuple[bytes, ...]:
    if outputs not in {2, 3}:
        raise ValueError("Noise HKDF supports two or three outputs")
    temp_key = _noise_hmac(chaining_key, input_key_material)
    result: list[bytes] = []
    previous = b""
    for index in range(1, outputs + 1):
        previous = _noise_hmac(temp_key, previous + bytes([index]))
        result.append(previous)
    return tuple(result)


class _NoiseCipherState:
    def __init__(self, key: bytes | None = None) -> None:
        self._key = key
        self._nonce = 0

    @property
    def has_key(self) -> bool:
        return self._key is not None

    def initialize_key(self, key: bytes) -> None:
        if len(key) != 32:
            raise LinkTransportError("Noise cipher key must be 32 bytes")
        self._key = key
        self._nonce = 0

    def _next_nonce(self) -> bytes:
        if self._nonce >= (1 << 64) - 1:
            raise LinkTransportError("Noise cipher nonce exhausted")
        nonce = b"\x00" * 4 + self._nonce.to_bytes(8, "little")
        self._nonce += 1
        return nonce

    def encrypt_with_ad(self, associated_data: bytes, plaintext: bytes) -> bytes:
        if self._key is None:
            return plaintext
        return nacl.bindings.crypto_aead_chacha20poly1305_ietf_encrypt(
            plaintext, associated_data, self._next_nonce(), self._key
        )

    def decrypt_with_ad(self, associated_data: bytes, ciphertext: bytes) -> bytes:
        if self._key is None:
            return ciphertext
        try:
            return nacl.bindings.crypto_aead_chacha20poly1305_ietf_decrypt(
                ciphertext, associated_data, self._next_nonce(), self._key
            )
        except nacl.exceptions.CryptoError as exc:
            raise LinkTransportError("Noise ciphertext authentication failed") from exc


class _NoiseSymmetricState:
    def __init__(self) -> None:
        if len(_NOISE_PROTOCOL_NAME) <= _NOISE_HASHLEN:
            self.handshake_hash = _NOISE_PROTOCOL_NAME.ljust(_NOISE_HASHLEN, b"\x00")
        else:
            self.handshake_hash = _noise_hash(_NOISE_PROTOCOL_NAME)
        self.chaining_key = self.handshake_hash
        self.cipher = _NoiseCipherState()

    def mix_hash(self, data: bytes) -> None:
        self.handshake_hash = _noise_hash(self.handshake_hash + data)

    def mix_key(self, input_key_material: bytes) -> None:
        self.chaining_key, temp_key = _noise_hkdf(self.chaining_key, input_key_material, 2)
        self.cipher.initialize_key(temp_key)

    def encrypt_and_hash(self, plaintext: bytes) -> bytes:
        ciphertext = self.cipher.encrypt_with_ad(self.handshake_hash, plaintext)
        self.mix_hash(ciphertext)
        return ciphertext

    def decrypt_and_hash(self, ciphertext: bytes) -> bytes:
        plaintext = self.cipher.decrypt_with_ad(self.handshake_hash, ciphertext)
        self.mix_hash(ciphertext)
        return plaintext

    def split(self) -> tuple[_NoiseCipherState, _NoiseCipherState]:
        first, second = _noise_hkdf(self.chaining_key, b"", 2)
        return _NoiseCipherState(first), _NoiseCipherState(second)


@dataclass(frozen=True)
class NoiseTransportCiphers:
    """Directional cipher pair returned after a completed XX handshake."""

    sending: _NoiseCipherState
    receiving: _NoiseCipherState
    handshake_hash: bytes


class NoiseXXHandshake:
    """Strict three-message Noise XX state machine for the Link cipher suite."""

    def __init__(
        self, *, initiator: bool, static_private_key: bytes,
        ephemeral_private_key: bytes | None = None, prologue: bytes = b"",
    ) -> None:
        if len(static_private_key) != 32:
            raise LinkTransportError("Noise static private key must be 32 bytes")
        if ephemeral_private_key is not None and len(ephemeral_private_key) != 32:
            raise LinkTransportError("Noise ephemeral private key must be 32 bytes")
        self.initiator = initiator
        self._static_private = static_private_key
        self.static_public_key = bytes(nacl.public.PrivateKey(static_private_key).public_key)
        self._ephemeral_private = ephemeral_private_key
        self._ephemeral_public: bytes | None = None
        self.remote_static_key: bytes | None = None
        self._remote_ephemeral: bytes | None = None
        self._symmetric = _NoiseSymmetricState()
        self._symmetric.mix_hash(prologue)
        self._step = 0

    def _ensure_ephemeral(self) -> bytes:
        if self._ephemeral_private is None:
            self._ephemeral_private = bytes(nacl.public.PrivateKey.generate())
        self._ephemeral_public = bytes(nacl.public.PrivateKey(self._ephemeral_private).public_key)
        return self._ephemeral_public

    @staticmethod
    def _dh(private_key: bytes, public_key: bytes) -> bytes:
        try:
            return nacl.bindings.crypto_scalarmult(private_key, public_key)
        except nacl.exceptions.CryptoError as exc:
            raise LinkTransportError("invalid Noise Diffie-Hellman public key") from exc

    def write_message(self, payload: bytes = b"") -> tuple[bytes, NoiseTransportCiphers | None]:
        if self.initiator and self._step == 0:
            ephemeral = self._ensure_ephemeral()
            self._symmetric.mix_hash(ephemeral)
            message = ephemeral + self._symmetric.encrypt_and_hash(payload)
            self._step = 1
            return message, None
        if not self.initiator and self._step == 1:
            if self._remote_ephemeral is None:
                raise LinkTransportError("Noise XX responder has no initiator ephemeral key")
            ephemeral = self._ensure_ephemeral()
            self._symmetric.mix_hash(ephemeral)
            self._symmetric.mix_key(self._dh(self._ephemeral_private, self._remote_ephemeral))
            encrypted_static = self._symmetric.encrypt_and_hash(self.static_public_key)
            self._symmetric.mix_key(self._dh(self._static_private, self._remote_ephemeral))
            message = ephemeral + encrypted_static + self._symmetric.encrypt_and_hash(payload)
            self._step = 2
            return message, None
        if self.initiator and self._step == 2:
            if self._remote_ephemeral is None:
                raise LinkTransportError("Noise XX initiator has no responder ephemeral key")
            encrypted_static = self._symmetric.encrypt_and_hash(self.static_public_key)
            self._symmetric.mix_key(self._dh(self._static_private, self._remote_ephemeral))
            message = encrypted_static + self._symmetric.encrypt_and_hash(payload)
            first, second = self._symmetric.split()
            self._step = 3
            return message, NoiseTransportCiphers(first, second, self._symmetric.handshake_hash)
        raise LinkTransportError("Noise XX write_message called out of order")

    def read_message(self, message: bytes) -> tuple[bytes, NoiseTransportCiphers | None]:
        if not self.initiator and self._step == 0:
            if len(message) < 32:
                raise LinkTransportError("truncated first Noise XX message")
            self._remote_ephemeral = message[:32]
            self._symmetric.mix_hash(self._remote_ephemeral)
            payload = self._symmetric.decrypt_and_hash(message[32:])
            self._step = 1
            return payload, None
        if self.initiator and self._step == 1:
            minimum = 32 + 32 + _NOISE_TAGLEN
            if len(message) < minimum:
                raise LinkTransportError("truncated second Noise XX message")
            self._remote_ephemeral = message[:32]
            self._symmetric.mix_hash(self._remote_ephemeral)
            self._symmetric.mix_key(self._dh(self._ephemeral_private, self._remote_ephemeral))
            static_end = 32 + 32 + _NOISE_TAGLEN
            self.remote_static_key = self._symmetric.decrypt_and_hash(message[32:static_end])
            self._symmetric.mix_key(self._dh(self._ephemeral_private, self.remote_static_key))
            payload = self._symmetric.decrypt_and_hash(message[static_end:])
            self._step = 2
            return payload, None
        if not self.initiator and self._step == 2:
            minimum = 32 + _NOISE_TAGLEN
            if len(message) < minimum:
                raise LinkTransportError("truncated third Noise XX message")
            static_end = 32 + _NOISE_TAGLEN
            self.remote_static_key = self._symmetric.decrypt_and_hash(message[:static_end])
            self._symmetric.mix_key(self._dh(self._ephemeral_private, self.remote_static_key))
            payload = self._symmetric.decrypt_and_hash(message[static_end:])
            first, second = self._symmetric.split()
            self._step = 3
            return payload, NoiseTransportCiphers(second, first, self._symmetric.handshake_hash)
        raise LinkTransportError("Noise XX read_message called out of order")


async def establish_noise_xx_initiator(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    identity: NodeIdentity,
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    expected_fingerprint: str | None = None,
) -> tuple[RealtimeIdentityPayload, NoiseTransportCiphers]:
    """Run the initiator side of XX and verify the responder's Link identity."""
    handshake = NoiseXXHandshake(
        initiator=True,
        static_private_key=bytes(derive_encryption_private_key(identity.transport_key)),
    )
    own_payload = RealtimeIdentityPayload.for_node(identity).to_json_bytes()
    try:
        first, _ = handshake.write_message()
        await asyncio.wait_for(write_realtime_record(writer, first), timeout_seconds)
        second = await asyncio.wait_for(read_realtime_record(reader), timeout_seconds)
        remote_bytes, _ = handshake.read_message(second)
        remote = RealtimeIdentityPayload.from_json_bytes(
            remote_bytes, defer_version_check=True
        )
        remote.verify_noise_static(handshake.remote_static_key)
        if expected_fingerprint is not None and remote.root_fingerprint != expected_fingerprint:
            raise LinkProtocolError(
                f"expected a real-time session with {expected_fingerprint}, "
                f"but authenticated as {remote.root_fingerprint}"
            )
        remote.require_supported_version()
        third, ciphers = handshake.write_message(own_payload)
        await asyncio.wait_for(write_realtime_record(writer, third), timeout_seconds)
    except TimeoutError as exc:
        raise LinkTransportError("Noise XX initiator handshake timed out") from exc
    if ciphers is None:
        raise LinkTransportError("Noise XX initiator did not produce transport keys")
    return remote, ciphers


async def establish_noise_xx_responder(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    identity: NodeIdentity,
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    first_message: bytes | None = None,
) -> tuple[RealtimeIdentityPayload, NoiseTransportCiphers]:
    """Run the responder side of XX and verify the initiator's Link
    identity. `first_message` (issue #168) is the initiator's first
    handshake record when the caller has already read it -- the real-
    time listener peeks at the first record to tell a bridge-attach
    preamble from a Noise handshake, and must not consume it twice."""
    handshake = NoiseXXHandshake(
        initiator=False,
        static_private_key=bytes(derive_encryption_private_key(identity.transport_key)),
    )
    own_payload = RealtimeIdentityPayload.for_node(identity).to_json_bytes()
    try:
        if first_message is None:
            first_message = await asyncio.wait_for(read_realtime_record(reader), timeout_seconds)
        handshake.read_message(first_message)
        second, _ = handshake.write_message(own_payload)
        await asyncio.wait_for(write_realtime_record(writer, second), timeout_seconds)
        third = await asyncio.wait_for(read_realtime_record(reader), timeout_seconds)
        remote_bytes, ciphers = handshake.read_message(third)
        remote = RealtimeIdentityPayload.from_json_bytes(
            remote_bytes, defer_version_check=True
        )
        remote.verify_noise_static(handshake.remote_static_key)
        remote.require_supported_version()
    except TimeoutError as exc:
        raise LinkTransportError("Noise XX responder handshake timed out") from exc
    if ciphers is None:
        raise LinkTransportError("Noise XX responder did not produce transport keys")
    return remote, ciphers


# Issue #168 (design doc §16 Decision 1, raw-socket proxy): a party
# attaching to a relayed bridge sends exactly one plaintext record --
# this magic followed by the attach token the relay handed it in
# `relay_ready` -- before the ordinary Noise XX handshake begins with
# its counterpart *through* the relay. The relay never sees anything
# after this record except opaque ciphertext. A real Noise first message
# starts with a random 32-byte ephemeral key, so a record starting with
# this ASCII prefix is unambiguous in practice.
BRIDGE_ATTACH_MAGIC = b"NETBBS-BRIDGE/1 "
BRIDGE_ATTACH_TOKEN_BYTES = 32  # hex-encoded 128-bit token


def encode_bridge_attach_record(attach_token: str) -> bytes:
    token = attach_token.encode("ascii")
    if len(token) != BRIDGE_ATTACH_TOKEN_BYTES or not all(c in b"0123456789abcdef" for c in token):
        raise LinkTransportError("bridge attach token must be 32 lowercase hex characters")
    return encode_realtime_record(BRIDGE_ATTACH_MAGIC + token)


def decode_bridge_attach_record(record: bytes) -> str | None:
    """The attach token if `record` is a bridge-attach preamble, else
    `None` (an ordinary Noise handshake record)."""
    if not record.startswith(BRIDGE_ATTACH_MAGIC):
        return None
    token = record[len(BRIDGE_ATTACH_MAGIC):]
    if len(token) != BRIDGE_ATTACH_TOKEN_BYTES or not all(c in b"0123456789abcdef" for c in token):
        raise LinkTransportError("malformed bridge attach record")
    return token.decode("ascii")


REALTIME_DEFAULT_OUTBOUND_QUEUE_SIZE = 64
REALTIME_DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 15.0
REALTIME_DEFAULT_HEARTBEAT_LEASE_SECONDS = 45.0
REALTIME_DEFAULT_MAX_FRAMES_PER_WINDOW = 100
REALTIME_DEFAULT_FRAME_WINDOW_SECONDS = 10.0
REALTIME_DEFAULT_MAX_PROTOCOL_STRIKES = 5


class LinkRealtimeSession:
    """
    Owns one live Noise-authenticated Link session end to end (design doc
    §8.10.1): the encrypted reader/writer loop, a bounded outbound queue
    so one slow peer can never block another session, and the ping/pong
    heartbeat lease that detects a silently dead peer. This class knows
    nothing about channel subscriptions, presence, or trust policy --
    `on_frame` is the single seam a caller (`LinkServer`'s inbound
    accept path / an outbound dialer, plus the channel-authorization
    layer above both) uses to react to every frame this session
    receives except the ones this object already owns end to end
    (`ping`/`pong`/`close`).

    Construction alone starts nothing -- call `start()` once whatever
    admission a caller wants to run first (trust policy, duplicate-
    session resolution) has already happened. `start()` spawns exactly
    the reader, writer, and heartbeat tasks this object owns; `close()`
    cancels and gathers all three on every exit path -- normal close,
    protocol failure, rate/strike limits, or external cancellation --
    without letting a cleanup failure mask the original reason it
    closed. `closed` is set exactly once, after that teardown completes,
    so a caller (e.g. a session registry) can await it to learn when
    this session is genuinely done.

    A frame that `on_frame` rejects by raising `LinkProtocolError` does
    not end the session by itself -- an `error` frame goes back to the
    peer and a strike is recorded; only repeated rejection past
    `max_protocol_strikes` closes the connection. This lets one
    unauthorized subscribe attempt fail cleanly without nuking an
    otherwise-good session, while still bounding how much of that a
    single peer gets for free.
    """

    def __init__(
        self,
        *,
        remote_fingerprint: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        ciphers: NoiseTransportCiphers,
        is_initiator: bool,
        on_frame: Callable[["LinkRealtimeSession", RealtimeFrame], Awaitable[None]],
        outbound_queue_size: int = REALTIME_DEFAULT_OUTBOUND_QUEUE_SIZE,
        heartbeat_interval_seconds: float = REALTIME_DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        heartbeat_lease_seconds: float = REALTIME_DEFAULT_HEARTBEAT_LEASE_SECONDS,
        max_frames_per_window: int = REALTIME_DEFAULT_MAX_FRAMES_PER_WINDOW,
        frame_window_seconds: float = REALTIME_DEFAULT_FRAME_WINDOW_SECONDS,
        max_protocol_strikes: int = REALTIME_DEFAULT_MAX_PROTOCOL_STRIKES,
    ) -> None:
        self.remote_fingerprint = remote_fingerprint
        self.is_initiator = is_initiator
        self._reader = reader
        self._writer = writer
        self._send_cipher = ciphers.sending
        self._recv_cipher = ciphers.receiving
        self._on_frame = on_frame
        self._outbound: asyncio.Queue[RealtimeFrame] = asyncio.Queue(maxsize=outbound_queue_size)
        self._heartbeat_interval = heartbeat_interval_seconds
        self._heartbeat_lease = heartbeat_lease_seconds
        self._max_frames_per_window = max_frames_per_window
        self._frame_window_seconds = frame_window_seconds
        self._max_protocol_strikes = max_protocol_strikes
        self._replay_window = RealtimeReplayWindow()
        self._last_activity: float | None = None
        self._frame_window_start: float | None = None
        self._frame_window_count = 0
        self._protocol_strikes = 0
        self._tasks: list[asyncio.Task] = []
        self._closing = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_reason: str | None = None

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._last_activity = loop.time()
        self._tasks = [
            loop.create_task(self._reader_loop(), name=f"link-realtime-reader-{self.remote_fingerprint}"),
            loop.create_task(self._writer_loop(), name=f"link-realtime-writer-{self.remote_fingerprint}"),
            loop.create_task(self._heartbeat_loop(), name=f"link-realtime-heartbeat-{self.remote_fingerprint}"),
        ]

    async def send(self, frame: RealtimeFrame) -> None:
        """Enqueue `frame` for the writer task. Raises `LinkTransportError`
        if the session is already closed, or if the outbound queue is
        already full -- in the full case this also closes the session
        with an explicit slow-consumer reason (design doc §8.10.1: "A
        full queue drops the session ... rather than silently losing a
        state transition") rather than blocking or silently dropping."""
        if self._closing.is_set():
            raise LinkTransportError(f"real-time session to {self.remote_fingerprint} is already closed")
        try:
            self._outbound.put_nowait(frame)
        except asyncio.QueueFull:
            await self.close(reason="slow_consumer")
            raise LinkTransportError(f"real-time session to {self.remote_fingerprint} dropped: slow consumer")

    async def close(self, *, reason: str, send_close_frame: bool = False) -> None:
        if self._closing.is_set():
            await self.closed.wait()
            return
        self._closing.set()
        self.close_reason = reason
        if send_close_frame:
            try:
                ciphertext = self._send_cipher.encrypt_with_ad(b"", build_close_frame(reason).to_json_bytes())
                await asyncio.wait_for(write_realtime_record(self._writer, ciphertext), timeout=2.0)
            except Exception:
                pass  # best-effort courtesy notice; teardown proceeds regardless
        current = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except Exception:
            pass
        self.closed.set()

    def _register_frame_arrival(self, *, now: float) -> bool:
        if self._frame_window_start is None or now - self._frame_window_start > self._frame_window_seconds:
            self._frame_window_start = now
            self._frame_window_count = 0
        self._frame_window_count += 1
        return self._frame_window_count <= self._max_frames_per_window

    def _register_protocol_strike(self) -> bool:
        self._protocol_strikes += 1
        return self._protocol_strikes >= self._max_protocol_strikes

    async def _reader_loop(self) -> None:
        try:
            while True:
                ciphertext = await read_realtime_record(self._reader)
                plaintext = self._recv_cipher.decrypt_with_ad(b"", ciphertext)
                frame = RealtimeFrame.from_json_bytes(plaintext)
                loop = asyncio.get_running_loop()
                self._last_activity = loop.time()
                if not self._register_frame_arrival(now=self._last_activity):
                    await self.close(reason="frame_rate_exceeded")
                    return
                if self._replay_window.seen_before(frame.message_id):
                    continue
                if frame.type == "ping":
                    await self.send(build_pong_frame())
                    continue
                if frame.type == "pong":
                    continue
                if frame.type == "close":
                    await self.close(reason=f"peer_closed: {frame.payload.get('reason', '')}")
                    return
                try:
                    validate_realtime_frame_payload(frame)
                    await self._on_frame(self, frame)
                except LinkProtocolError as exc:
                    if self._register_protocol_strike():
                        await self.close(reason="too_many_protocol_strikes")
                        return
                    await self.send(build_error_frame("rejected", str(exc)[:200]))
        except asyncio.CancelledError:
            raise
        except (LinkTransportError, LinkProtocolError) as exc:
            await self.close(reason=f"protocol_error: {exc}")
        except Exception as exc:
            await self.close(reason=f"reader_error: {exc}")

    async def _writer_loop(self) -> None:
        try:
            while True:
                frame = await self._outbound.get()
                ciphertext = self._send_cipher.encrypt_with_ad(b"", frame.to_json_bytes())
                await write_realtime_record(self._writer, ciphertext)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.close(reason=f"writer_error: {exc}")

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval)
                loop = asyncio.get_running_loop()
                assert self._last_activity is not None
                if loop.time() - self._last_activity > self._heartbeat_lease:
                    await self.close(reason="heartbeat_lease_expired")
                    return
                await self.send(build_ping_frame())
        except asyncio.CancelledError:
            raise
        except LinkTransportError:
            return  # send() already closed the session (queue full or already closed)


def _decide_realtime_admission(db, fingerprint: str) -> bool:
    """Phase 4 transport trust gate for real-time sessions (design doc
    §8.10: "applies the local Phase-4 node transport decision before
    accepting any application frame"). Runs after the Noise handshake
    has already authenticated `fingerprint`, before either side of a
    new `LinkRealtimeSession` is admitted to the registry."""
    ensure_node_subject(db, fingerprint)
    return decide_node_action(db, fingerprint, LinkPolicyAction.REALTIME).allowed


async def _reject_before_session(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass


class LinkRealtimeSessionRegistry:
    """
    Tracks at most one live `LinkRealtimeSession` per remote fingerprint
    for one node, resolving the "simultaneous inbound and outbound"
    collision with the deterministic rule design doc §8.10.1 specifies:
    the lower fingerprint keeps its outbound connection, the higher
    fingerprint keeps its inbound connection, applied only while both
    candidates genuinely exist so a sole usable connection is never
    discarded. Because every node applies the identical rule using the
    identical two fingerprints, both ends converge on the same single
    connection without any coordination round-trip.

    Also owns exactly one small watcher task per admitted session (to
    deregister it once it closes) -- bounded by construction, since
    there is at most one such task per registry entry, and gathered by
    `close_all()` on node shutdown.
    """

    def __init__(self, *, own_fingerprint: str) -> None:
        self._own_fingerprint = own_fingerprint
        self._sessions: dict[str, LinkRealtimeSession] = {}
        self._watchers: set[asyncio.Task] = set()

    def get(self, fingerprint: str) -> LinkRealtimeSession | None:
        return self._sessions.get(fingerprint)

    def all_sessions(self) -> list[LinkRealtimeSession]:
        return list(self._sessions.values())

    async def admit(self, session: LinkRealtimeSession) -> bool:
        """Register `session` as the live session for its remote
        fingerprint. Returns whether it survived -- if not, `session`
        has already been closed with reason `"duplicate_session"` and
        the caller must not use it further."""
        fingerprint = session.remote_fingerprint
        existing = self._sessions.get(fingerprint)
        if existing is None or existing is session:
            self._register(session)
            return True
        if existing.is_initiator == session.is_initiator:
            # Not actually a simultaneous in/out collision -- e.g. a
            # stale entry whose own watcher hasn't run yet. Prefer the
            # newer one rather than leaving two sessions unreachable.
            await existing.close(reason="duplicate_session", send_close_frame=True)
            self._register(session)
            return True
        own_keeps_outbound = self._own_fingerprint < fingerprint
        new_wins = session.is_initiator == own_keeps_outbound
        if new_wins:
            await existing.close(reason="duplicate_session", send_close_frame=True)
            self._register(session)
            return True
        await session.close(reason="duplicate_session", send_close_frame=True)
        return False

    def _register(self, session: LinkRealtimeSession) -> None:
        self._sessions[session.remote_fingerprint] = session
        watcher = asyncio.get_running_loop().create_task(self._watch(session))
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)

    async def _watch(self, session: LinkRealtimeSession) -> None:
        await session.closed.wait()
        if self._sessions.get(session.remote_fingerprint) is session:
            del self._sessions[session.remote_fingerprint]

    async def close_all(self, *, reason: str) -> None:
        for session in list(self._sessions.values()):
            await session.close(reason=reason, send_close_frame=True)
        if self._watchers:
            await asyncio.gather(*self._watchers, return_exceptions=True)


async def rotate_realtime_transport_key(
    identity: NodeIdentity,
    *,
    registry: LinkRealtimeSessionRegistry,
    server: LinkRealtimeServer | None = None,
    connectors: Sequence[LinkRealtimeConnector] = (),
) -> NodeIdentity:
    """
    Rotates `identity`'s transport key and makes the rotation actually take
    effect for real-time traffic (design doc §8.10: "transport-key rotation
    ends sessions using the old key; reconnect performs a fresh handshake
    against the new verified chain") -- `rotate_operational_key` alone is
    silent to any of this, since it returns a new `NodeIdentity` with no
    reference to what's currently live.

    Every session `registry` currently holds was authenticated during a
    handshake that presented the *pre-rotation* chain -- post-handshake, a
    Noise session's symmetric keys never touch the static key again, so
    nothing about an established session's own traffic would ever notice a
    rotation on its own. Closing it is therefore the only way to actually
    retire it; its peer sees an ordinary session close and, if it wants to
    keep talking to this node, redials.

    `server`/`connectors` -- if given -- are switched to the rotated
    identity *before* `registry.close_all()` runs, not after: both hold a
    fixed `NodeIdentity` reference from construction and reuse it for every
    future handshake, inbound or outbound, so a redial that raced ahead of
    an after-the-fact swap would still complete against the very chain
    being retired, silently defeating the rotation. `connectors` is this
    node's own outbound reconnect loops (`LinkRealtimeConnector`) for this
    identity, if any -- their automatic reconnect after `close_all` closes
    their current session must dial with the new key too.

    Does not save `identity` to disk -- same contract as
    `rotate_operational_key` itself; the caller persists the returned
    identity.
    """
    rotated = rotate_operational_key(identity, purpose="transport")
    if server is not None:
        server.update_identity(rotated)
    for connector in connectors:
        connector.update_identity(rotated)
    await registry.close_all(reason="transport_key_rotated")
    return rotated


class LinkRealtimeServer:
    """
    Accepts real inbound Noise-authenticated Link real-time sessions
    (design doc §8.10) on a persistent TCP listener, deliberately
    separate from `LinkServer`'s own `aiohttp` HTTP+JSON port -- a live
    chat frame is a different traffic family, never gossiped, never
    stored as a canonical event.

    Applies the Phase 4 transport trust decision (`_decide_realtime_
    admission`, `LinkPolicyAction.REALTIME`) after the handshake has
    authenticated the peer's identity but strictly before any
    application frame is processed, same "verify, then decide, then
    admit" order `LinkServer` already uses for its own HTTP routes.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        identity: NodeIdentity,
        registry: LinkRealtimeSessionRegistry,
        on_frame: Callable[[LinkRealtimeSession, RealtimeFrame], Awaitable[None]],
        lane: DatabaseLane | None = None,
        enforce_trust_policy: bool = False,
        bridge_attach: Callable[[str, asyncio.StreamReader, asyncio.StreamWriter], Awaitable[bool]] | None = None,
    ) -> None:
        if enforce_trust_policy and lane is None:
            raise ValueError("enforce_trust_policy requires a lane")
        self._host = host
        self._port = port
        self._identity = identity
        self._registry = registry
        self._on_frame = on_frame
        self._lane = lane
        self._enforce_trust_policy = enforce_trust_policy
        # Issue #168: where a bridge-attach preamble (see
        # BRIDGE_ATTACH_MAGIC) hands its connection off -- the node's
        # `RealtimeRelay`, when this node serves live relay. `None` means
        # an attach record is simply an invalid handshake and is closed.
        self._bridge_attach = bridge_attach
        self._server: asyncio.base_events.Server | None = None
        self._accepting: set[asyncio.Task] = set()

    @property
    def port(self) -> int:
        if self._server is None or not self._server.sockets:
            raise LinkTransportError("real-time server has not been started")
        return self._server.sockets[0].getsockname()[1]

    def update_identity(self, identity: NodeIdentity) -> None:
        """Swap the identity presented to every future inbound handshake
        -- e.g. after `rotate_realtime_transport_key` rotates the transport
        key -- without needing to stop and rebuild the listener. Already-
        admitted sessions are unaffected; only a *new* handshake reads
        `self._identity`."""
        self._identity = identity

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self._host, self._port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                # Bounded, not awaited unconditionally: this server's job
                # is to stop *accepting new* connections -- already-
                # admitted sessions are independently owned by
                # `LinkRealtimeSession`/the registry, not by this object,
                # so nothing here should need to wait for them to close
                # too. `wait_closed()` has been observed to block on an
                # already-admitted connection's socket under Windows'
                # Proactor event loop even though `close()` itself already
                # stopped new accepts, so this is a defensive ceiling, not
                # a normal-path wait.
                await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
            except TimeoutError:
                pass
        if self._accepting:
            for task in self._accepting:
                task.cancel()
            await asyncio.gather(*self._accepting, return_exceptions=True)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._accepting.add(task)
        try:
            await self._admit_inbound(reader, writer)
        finally:
            self._accepting.discard(task)

    async def _admit_inbound(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            first = await asyncio.wait_for(read_realtime_record(reader), _DEFAULT_TIMEOUT_SECONDS)
            attach_token = decode_bridge_attach_record(first)
        except (LinkTransportError, TimeoutError):
            await _reject_before_session(writer)
            return
        if attach_token is not None:
            # A relayed bridge's raw leg (issue #168), not a session with
            # this node at all -- the relay takes the socket over and this
            # listener never sees another byte of it.
            accepted = False
            if self._bridge_attach is not None:
                accepted = await self._bridge_attach(attach_token, reader, writer)
            if not accepted:
                await _reject_before_session(writer)
            return
        try:
            remote, ciphers = await establish_noise_xx_responder(
                reader, writer, self._identity, first_message=first
            )
        except (LinkTransportError, LinkProtocolError):
            await _reject_before_session(writer)
            return
        fingerprint = remote.root_fingerprint
        if self._enforce_trust_policy:
            assert self._lane is not None
            allowed = await self._lane.run(_decide_realtime_admission, fingerprint)
            if not allowed:
                await _reject_before_session(writer)
                return
        session = LinkRealtimeSession(
            remote_fingerprint=fingerprint, reader=reader, writer=writer, ciphers=ciphers,
            is_initiator=False, on_frame=self._on_frame,
        )
        session.start()
        await self._registry.admit(session)


async def dial_realtime_session(
    host: str,
    port: int,
    identity: NodeIdentity,
    *,
    on_frame: Callable[[LinkRealtimeSession, RealtimeFrame], Awaitable[None]],
    registry: LinkRealtimeSessionRegistry,
    lane: DatabaseLane | None = None,
    enforce_trust_policy: bool = False,
    expected_fingerprint: str | None = None,
) -> LinkRealtimeSession:
    """Dial `host`/`port`, complete the Noise XX handshake as initiator,
    apply the same Phase 4 trust gate `LinkRealtimeServer` applies to an
    inbound connection, and hand the resulting session to `registry`.
    Raises `LinkTransportError`/`LinkProtocolError` for a connection,
    handshake, or trust-policy failure; raises `LinkTransportError` if
    this session loses the duplicate-session tiebreak (design doc
    §8.10.1) -- the caller should not treat that as a dial failure to
    retry so much as "a session to this peer already exists".

    Code review follow-up: Noise XX authenticates that *some* node with
    a valid, chain-verified key answered at `host`/`port` -- it says
    nothing about whether that's the *specific* node the caller meant to
    reach. A caller dialing a known peer's own advertised address (the
    only real caller today, `netbbs.link.realtime_channels.
    ensure_live_subscription`, reaching a linked channel's origin) must
    pass `expected_fingerprint` so a stale/reassigned/reused address --
    or, once a real-time relay design lands, a relay terminating the
    connection itself instead of forwarding it -- gets refused here
    rather than silently treated as the intended peer by whatever code
    called this. `None` (a caller with no specific peer in mind, e.g. a
    future generic listen-for-anyone bootstrap path) skips the check."""
    if enforce_trust_policy and lane is None:
        raise ValueError("enforce_trust_policy requires a lane")
    reader, writer = await asyncio.open_connection(host, port)
    try:
        remote, ciphers = await establish_noise_xx_initiator(
            reader, writer, identity, expected_fingerprint=expected_fingerprint
        )
    except (LinkTransportError, LinkProtocolError):
        await _reject_before_session(writer)
        raise
    fingerprint = remote.root_fingerprint
    if enforce_trust_policy:
        assert lane is not None
        allowed = await lane.run(_decide_realtime_admission, fingerprint)
        if not allowed:
            await _reject_before_session(writer)
            raise LinkTransportError(f"real-time session to {fingerprint} refused by local trust policy")
    session = LinkRealtimeSession(
        remote_fingerprint=fingerprint, reader=reader, writer=writer, ciphers=ciphers,
        is_initiator=True, on_frame=on_frame,
    )
    session.start()
    survived = await registry.admit(session)
    if not survived:
        raise LinkTransportError(f"real-time session to {fingerprint} lost the duplicate-session tiebreak")
    return session


async def attach_relayed_session(
    host: str,
    port: int,
    identity: NodeIdentity,
    *,
    attach_token: str,
    role: str,
    expected_fingerprint: str,
    on_frame: Callable[[LinkRealtimeSession, RealtimeFrame], Awaitable[None]],
    registry: LinkRealtimeSessionRegistry,
    lane: DatabaseLane | None = None,
    enforce_trust_policy: bool = False,
) -> LinkRealtimeSession:
    """Issue #168: the party side of a relayed bridge. Open the attach
    connection to the relay, send the one plaintext preamble record, then
    run the ordinary Noise XX handshake *with the counterpart* through the
    relay's spliced sockets in the role `relay_ready` assigned, and admit
    the result exactly like `dial_realtime_session` would a direct one.

    `expected_fingerprint` is mandatory here, not optional: the relay is
    a network intermediary that could pair this node with anyone it likes,
    so the authenticated fingerprint must equal the one the rendezvous
    named -- on *both* roles (the responder side verifies too; the worklog
    records this as the binding gap any relay design must close)."""
    if enforce_trust_policy and lane is None:
        raise ValueError("enforce_trust_policy requires a lane")
    if role not in ("initiator", "responder"):
        raise LinkTransportError(f"unknown relay role {role!r}")
    reader, writer = await asyncio.open_connection(host, port)
    owned_by_session = False
    try:
        writer.write(encode_bridge_attach_record(attach_token))
        await writer.drain()
        if role == "initiator":
            remote, ciphers = await establish_noise_xx_initiator(
                reader, writer, identity, expected_fingerprint=expected_fingerprint
            )
        else:
            remote, ciphers = await establish_noise_xx_responder(reader, writer, identity)
            if remote.root_fingerprint != expected_fingerprint:
                raise LinkProtocolError(
                    f"expected a relayed session with {expected_fingerprint}, "
                    f"but authenticated as {remote.root_fingerprint}"
                )
        fingerprint = remote.root_fingerprint
        if enforce_trust_policy:
            assert lane is not None
            allowed = await lane.run(_decide_realtime_admission, fingerprint)
            if not allowed:
                raise LinkTransportError(f"relayed session to {fingerprint} refused by local trust policy")
        session = LinkRealtimeSession(
            remote_fingerprint=fingerprint, reader=reader, writer=writer, ciphers=ciphers,
            is_initiator=(role == "initiator"), on_frame=on_frame,
        )
        owned_by_session = True
    except BaseException:
        # Includes cancellation (a caller's own rendezvous timeout firing
        # mid-handshake): until a session owns the socket, close it here.
        if not owned_by_session:
            await _reject_before_session(writer)
        raise
    session.start()
    survived = await registry.admit(session)
    if not survived:
        raise LinkTransportError(f"relayed session to {fingerprint} lost the duplicate-session tiebreak")
    return session


REALTIME_RECONNECT_MIN_BACKOFF_SECONDS = 1.0
REALTIME_RECONNECT_MAX_BACKOFF_SECONDS = 60.0
REALTIME_RECONNECT_STABLE_AFTER_SECONDS = 30.0


class LinkRealtimeConnector:
    """
    Owns the reconnect loop for one outbound real-time destination
    (design doc §8.10.1: "reconnect uses bounded exponential backoff
    with jitter and resets only after a stable authenticated session").
    Dials, runs until the resulting session closes for any reason, then
    waits a random `[0, backoff)` jittered delay before retrying,
    doubling `backoff` up to `max_backoff_seconds` each time -- reset
    back to `min_backoff_seconds` only once a session has stayed up for
    at least `stable_after_seconds`.

    One task, owned end to end: `start()` spawns it, `stop()` cancels
    and gathers it. A session that loses the duplicate-session tiebreak
    (`dial_realtime_session` raising because a session to this peer
    already exists) is treated the same as any other dial failure here
    -- back off and retry -- since the winning session may itself close
    later and leave this destination genuinely unconnected again.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        identity: NodeIdentity,
        on_frame: Callable[[LinkRealtimeSession, RealtimeFrame], Awaitable[None]],
        registry: LinkRealtimeSessionRegistry,
        lane: DatabaseLane | None = None,
        enforce_trust_policy: bool = False,
        expected_fingerprint: str | None = None,
        min_backoff_seconds: float = REALTIME_RECONNECT_MIN_BACKOFF_SECONDS,
        max_backoff_seconds: float = REALTIME_RECONNECT_MAX_BACKOFF_SECONDS,
        stable_after_seconds: float = REALTIME_RECONNECT_STABLE_AFTER_SECONDS,
        rng: random.Random | None = None,
        on_session: Callable[[LinkRealtimeSession], Awaitable[None]] | None = None,
        addresses: list[tuple[str, int]] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        # Every advertised address, tried in order across successive
        # attempts (`host`/`port` alone otherwise): a stale first address
        # must not pin the reconnect loop to an endpoint that never answers.
        self._addresses: list[tuple[str, int]] = list(addresses) if addresses else [(host, port)]
        self._attempt = 0
        self._identity = identity
        self._on_frame = on_frame
        self._registry = registry
        self._lane = lane
        self._enforce_trust_policy = enforce_trust_policy
        self._expected_fingerprint = expected_fingerprint
        self._on_session = on_session
        self._min_backoff = min_backoff_seconds
        self._max_backoff = max_backoff_seconds
        self._stable_after = stable_after_seconds
        self._rng = rng or random.Random()
        self._task: asyncio.Task | None = None
        self._current_session: LinkRealtimeSession | None = None
        self._stopping = False

    def start(self) -> None:
        self._stopping = False
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._current_session is not None:
            await self._current_session.close(reason="local_shutdown", send_close_frame=True)
            self._current_session = None

    def update_identity(self, identity: NodeIdentity) -> None:
        """Swap the identity `_run`'s next dial (including the reconnect
        this connector performs on its own after a rotation-triggered
        close) will present -- see `LinkRealtimeServer.update_identity`."""
        self._identity = identity

    async def _run(self) -> None:
        backoff = self._min_backoff
        loop = asyncio.get_running_loop()
        while not self._stopping:
            try:
                connected_at = loop.time()
                host, port = self._addresses[self._attempt % len(self._addresses)]
                self._attempt += 1
                session = await dial_realtime_session(
                    host, port, self._identity, on_frame=self._on_frame,
                    registry=self._registry, lane=self._lane,
                    enforce_trust_policy=self._enforce_trust_policy,
                    expected_fingerprint=self._expected_fingerprint,
                )
                self._current_session = session
                if self._on_session is not None:
                    await self._on_session(session)
                await session.closed.wait()
                self._current_session = None
                if loop.time() - connected_at >= self._stable_after:
                    backoff = self._min_backoff
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # dial/handshake/trust-policy/tiebreak failure: fall through to backoff
            if self._stopping:
                return
            await asyncio.sleep(self._rng.uniform(0, backoff))
            backoff = min(backoff * 2, self._max_backoff)


class LinkServer:
    """
    Accepts real inbound Link HTTP+JSON traffic for one `LinkNode`.

    `own_hello_provider` is a plain callable returning this node's
    current `HelloMessage` on demand — deliberately not something this
    class computes itself (addresses/outgoing-only/timestamp are
    deployment/node-config concerns, out of scope here, same reasoning
    `LinkNode.build_hello` itself already applies at the protocol
    layer, one level down).

    `lane`: the background `DatabaseLane` this server
    persists accepted peers/events through, off the event loop, after
    `node`'s own in-memory verification accepts them.

    `relay_serving_enabled`/`max_relay_clients` (issue #58):
    this node's own policy for `_handle_relay_consent` -- whether to
    ever grant a relay-consent request at all, and the cap on how many
    simultaneous grants to hold once serving is enabled (design doc
    §12: "a conservative resource cap... and an easy opt-out"). Plain
    constructor parameters, not read from `netbbs.net.nodeconfig`
    directly -- this module has no config-loading concern of its own
    (matching `own_hello_provider`'s own "deployment concerns are the
    caller's job" reasoning just above); `netbbs.__main__` is the one
    real caller that threads `LinkConfig`'s values through.

    `max_peers`/`max_carried_boards`/`throttle` (design doc §13.9,
    issue #60's third operational slice): same "plain constructor
    parameter, safe default, real value threaded through by `netbbs.
    __main__`" shape as `max_relay_clients` just above. `throttle`
    (`netbbs.net.throttle.LinkRequestThrottle`) defaults to `None` --
    unbounded, matching every other quota parameter's own default here
    -- rather than manufacturing one internally, since its token-bucket
    state is meant to be node-lifetime and constructed once, the same
    reasoning `LoginThrottle` is already built once in `netbbs.__main__`
    rather than per-server.
    """

    def __init__(
        self,
        host: str,
        port: int,
        node: LinkNode,
        own_hello_provider: Callable[[], HelloMessage],
        lane: DatabaseLane,
        *,
        relay_serving_enabled: bool = True,
        max_relay_clients: int = _DEFAULT_MAX_RELAY_CLIENTS,
        max_peers: int | None = _DEFAULT_MAX_PEERS,
        max_carried_boards: int | None = _DEFAULT_MAX_CARRIED_BOARDS,
        max_carried_channels: int | None = _DEFAULT_MAX_CARRIED_CHANNELS,
        max_carried_file_areas: int | None = _DEFAULT_MAX_CARRIED_FILE_AREAS,
        max_remote_files_per_area: int | None = _DEFAULT_MAX_REMOTE_FILES_PER_AREA,
        max_concurrent_file_transfers_per_peer: int = _DEFAULT_MAX_CONCURRENT_FILE_TRANSFERS_PER_PEER,
        throttle: LinkRequestThrottle | None = None,
        enforce_trust_policy: bool = False,
    ) -> None:
        self._host = host
        self._port = port
        self._node = node
        self._own_hello_provider = own_hello_provider
        self._lane = lane
        self._relay_serving_enabled = relay_serving_enabled
        self._max_relay_clients = max_relay_clients
        self._max_peers = max_peers
        self._max_carried_boards = max_carried_boards
        self._max_carried_channels = max_carried_channels
        self._max_carried_file_areas = max_carried_file_areas
        self._max_remote_files_per_area = max_remote_files_per_area
        self._max_concurrent_file_transfers_per_peer = max_concurrent_file_transfers_per_peer
        # Design doc §11.3, issue #89: fingerprint -> the set of transfer_ids
        # currently being served for it -- in-memory only, the bounded-
        # concurrent-transfer counter _handle_file_chunk_request enforces.
        # Never persisted (serving one chunk is otherwise fully stateless);
        # a restart harmlessly resets every peer back to zero in flight.
        self._active_transfers_by_peer: dict[str, set[str]] = {}
        self._throttle = throttle
        self._enforce_trust_policy = enforce_trust_policy
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @property
    def port(self) -> int:
        if self._site is None:
            raise RuntimeError("server has not been started yet")
        return self._site.port

    async def start(self) -> None:
        app = web.Application(client_max_size=_LINK_CLIENT_MAX_SIZE_BYTES, middlewares=[_rate_limit_middleware])
        app[_LINK_THROTTLE_APP_KEY] = self._throttle
        app.router.add_post(f"{LINK_PATH_PREFIX}/hello", self._handle_hello)
        app.router.add_post(f"{LINK_PATH_PREFIX}/events/{{fingerprint}}", self._handle_events)
        app.router.add_post(f"{LINK_PATH_PREFIX}/peers/{{fingerprint}}", self._handle_peers)
        if not self._enforce_trust_policy:
            # Compatibility-only route for synthetic/legacy harnesses. The
            # real runtime enables policy and therefore exposes only the
            # authenticated POST route above.
            app.router.add_get(f"{LINK_PATH_PREFIX}/peers", self._handle_peers)
        app.router.add_post(f"{LINK_PATH_PREFIX}/relay-consent/{{fingerprint}}", self._handle_relay_consent)
        app.router.add_post(
            f"{LINK_PATH_PREFIX}/relay-mailbox/{{fingerprint}}/deposit", self._handle_relay_mailbox_deposit
        )
        app.router.add_post(f"{LINK_PATH_PREFIX}/relay-mailbox/pickup", self._handle_relay_mailbox_pickup)
        app.router.add_post(f"{LINK_PATH_PREFIX}/inventory/{{fingerprint}}", self._handle_inventory)
        app.router.add_post(f"{LINK_PATH_PREFIX}/trust-pull/{{fingerprint}}", self._handle_trust_pull)
        app.router.add_post(f"{LINK_PATH_PREFIX}/file-chunk/{{fingerprint}}", self._handle_file_chunk_request)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    @staticmethod
    def _policy_rejection(decision: LinkPolicyDecision) -> web.Response:
        return web.json_response(
            {"error": "Link policy rejected this request", "reason_code": decision.reason_code},
            status=403,
        )

    async def _decide(self, fingerprint: str, action: LinkPolicyAction) -> LinkPolicyDecision | None:
        if not self._enforce_trust_policy:
            return None
        return await self._lane.run(decide_node_action, fingerprint, action)

    async def _handle_hello(self, request: web.Request) -> web.Response:
        try:
            body = await request.json(loads=strict_json_loads)
            hello = HelloMessage.from_dict(body)
        except (KeyError, ValueError, TypeError) as exc:
            return web.json_response({"error": f"malformed hello: {exc}"}, status=400)

        try:
            peer = self._node.handle_hello(hello, max_peers=self._max_peers)
        except LinkProtocolError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        decision = await self._decide(peer.fingerprint, LinkPolicyAction.HELLO)
        if decision is not None and not decision.allowed:
            self._node.peers.pop(peer.fingerprint, None)
            return self._policy_rejection(decision)
        if self._enforce_trust_policy:
            await self._lane.run(ensure_node_subject, peer.fingerprint)
        await self._lane.run(save_peer, peer)
        return web.json_response(self._own_hello_provider().to_dict())

    async def _handle_events(self, request: web.Request) -> web.Response:
        fingerprint = request.match_info["fingerprint"]
        try:
            raw_events = await request.json(loads=strict_json_loads)
        except ValueError as exc:
            return web.json_response({"error": f"malformed events: {exc}"}, status=400)

        if self._enforce_trust_policy:
            object_types = {
                item.get("envelope", {}).get("object_type") for item in raw_events
                if isinstance(item, dict)
            }
            action = (
                LinkPolicyAction.KEY_LIFECYCLE
                if object_types and object_types <= {"key_transition"}
                else LinkPolicyAction.EVENTS
            )
            decision = await self._lane.run(decide_node_action, fingerprint, action)
            if not decision.allowed:
                return self._policy_rejection(decision)
            if action != LinkPolicyAction.KEY_LIFECYCLE:
                for raw in raw_events:
                    author_decision = await self._lane.run(
                        decide_event_authorship, raw, transport_peer_fingerprint=fingerprint
                    )
                    if not author_decision.allowed:
                        return self._policy_rejection(author_decision)

        try:
            accepted = self._node.handle_events(fingerprint, raw_events)
        except LinkProtocolError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except (KeyError, TypeError) as exc:
            return web.json_response({"error": f"malformed events: {exc}"}, status=400)

        await persist_accepted_events(
            self._lane, self._node, accepted,
            sender_fingerprint=fingerprint, max_carried_boards=self._max_carried_boards,
            max_carried_channels=self._max_carried_channels,
            max_carried_file_areas=self._max_carried_file_areas,
            max_remote_files_per_area=self._max_remote_files_per_area,
            enforce_trust_policy=self._enforce_trust_policy,
        )
        if accepted:
            # sender.transitions grew -- one updated write, not one per
            # accepted event.
            await self._lane.run(save_peer, self._node.peers[fingerprint])

        return web.json_response({"accepted": accepted})

    async def _handle_inventory(self, request: web.Request) -> web.Response:
        """
        Design doc §8.8, issue #85: the responder side of pull-based
        catch-up.

        **Issue #106: `fingerprint` (the URL path segment, previously
        never even read here) now gates every response.** Before issue
        #94, an arbitrary caller still had to already know a resource ID
        to ask about, so the lack of a peer-membership/signature check
        cost nothing worth gating. Issue #94 correctly made each `*_
        event_diff` call also return anything this node carries that's
        simply *absent* from the request -- which turned an all-empty
        request into "list everything you have," i.e. unauthenticated
        resource enumeration. `LinkNode.handle_inventory_request` now
        requires `fingerprint` to already be a completed peer *and* the
        request's signature to verify against that peer's current signing
        key (proving current possession, not merely a publicly-
        discoverable fingerprint) before any of the diff logic below
        runs -- see that method's and `InventoryRequest`'s own docstrings
        for the full before/after reasoning. A completed peer may still
        send an empty inventory and discover everything; an
        unauthenticated caller gets refused outright, not a truncated or
        degraded answer.

        Bounded the same way every other route here is: the rate-limit
        middleware and `client_max_size`, plus `board_event_diff`'s own
        `limit` argument capping the response itself.

        Design doc §9.6, issue #87: `channel_event_diff` shares the same
        overall `_MAX_EVENTS_PER_REQUEST` budget as the board half, not a
        second independent cap -- run board first, then channels with
        whatever budget remains, so one combined request/response still
        obeys one combined bound.

        Design doc §11, issue #93: `file_area_event_diff` extends the
        same shared-budget chain a third step -- run after board and
        channel, with whatever remains of the one overall bound. Only
        catalogue metadata (`file_area_genesis`/`file_descriptor`) is
        ever included; chunk bytes stay outside inventory entirely (see
        `file_area_event_diff`'s own docstring).
        """
        fingerprint = request.match_info["fingerprint"]
        try:
            body = await request.json(loads=strict_json_loads)
            inventory_request = InventoryRequest.from_dict(body)
        except (KeyError, ValueError, TypeError) as exc:
            return web.json_response({"error": f"malformed inventory request: {exc}"}, status=400)

        try:
            self._node.handle_inventory_request(fingerprint, inventory_request)
        except LinkProtocolError as exc:
            return web.json_response({"error": str(exc)}, status=403)

        decision = await self._decide(fingerprint, LinkPolicyAction.INVENTORY)
        if decision is not None and not decision.allowed:
            return self._policy_rejection(decision)
        response_limit = _MAX_EVENTS_PER_REQUEST // (decision.budget_divisor if decision else 1)

        # Issue #94: each `*_event_diff` call runs unconditionally, not
        # just when the *request* mentions boards/channels/file areas --
        # each now also returns anything *this* node carries that's
        # simply absent from the request (an empty request included), so
        # gating the call on the request being non-empty would silently
        # undo that fix for exactly the bootstrap case it exists for (a
        # requester with nothing carried yet sends an all-empty request).
        # Still gated on `remaining > 0`: that's the shared response-size
        # budget, unrelated to whether the request itself was empty.
        board_events, board_truncated = await self._lane.run(
            board_event_diff, inventory_request.boards, limit=response_limit
        )
        remaining = response_limit - len(board_events)
        if remaining > 0:
            channel_events, channel_truncated = await self._lane.run(
                channel_event_diff, inventory_request.channels, limit=remaining
            )
        else:
            channel_events, channel_truncated = [], True
        remaining -= len(channel_events)
        if remaining > 0:
            file_area_events, file_area_truncated = await self._lane.run(
                file_area_event_diff, inventory_request.file_areas, limit=remaining
            )
        else:
            file_area_events, file_area_truncated = [], True
        events = board_events + channel_events + file_area_events
        more_available = board_truncated or channel_truncated or file_area_truncated
        return web.json_response({"events": events, "more_available": more_available})

    async def _handle_trust_pull(self, request: web.Request) -> web.Response:
        """Serve one authenticated, issuer-filtered trust subscription page."""
        fingerprint = request.match_info["fingerprint"]
        try:
            body = await request.json(loads=strict_json_loads)
            pull = TrustPullRequest.from_dict(body)
            self._node.handle_trust_pull_request(fingerprint, pull)
            decision = await self._decide(fingerprint, LinkPolicyAction.TRUST)
            if decision is not None and not decision.allowed:
                return self._policy_rejection(decision)
            objects, more = await self._lane.run(
                load_trust_object_page,
                issuer_fingerprint=pull.issuer_fingerprint,
                after_content_id=pull.after_content_id,
                limit=pull.limit,
                revocations_only=pull.revocations_only,
            )
        except (KeyError, TypeError, ValueError, TrustWireError) as exc:
            return web.json_response({"error": f"malformed trust pull: {exc}"}, status=400)
        except LinkProtocolError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        return web.json_response({"objects": objects, "more_available": more})

    async def _handle_file_chunk_request(self, request: web.Request) -> web.Response:
        """
        Design doc §11.3, issue #89: the serving side of on-demand chunk
        transfer -- unlike `/inventory`, this route serves raw content
        bytes, a genuinely new resource exposure `_handle_inventory`'s
        own "already gossiped, nothing new" reasoning doesn't cover, so
        `fingerprint` is required to already be a completed peer (the
        same "no relay from a stranger" precondition `_handle_events`
        already enforces), and concurrent transfers per peer are bounded
        in memory (`self._active_transfers_by_peer`) -- serving one
        chunk is otherwise fully stateless, so this is the only place
        left to bound.

        The response carries the chunk's raw bytes as the literal body
        (never base64-embedded) plus a signed `FileChunkDescriptor` in
        the `X-NetBBS-Chunk-Envelope` header, base64-encoded JSON --
        `request_file_chunk`'s own docstring covers the client side of
        this same split.
        """
        fingerprint = request.match_info["fingerprint"]
        peer = self._node.peers.get(fingerprint)
        if peer is None:
            return web.json_response(
                {"error": f"{fingerprint} has no completed hello with this node -- refusing"}, status=403
            )

        decision = await self._decide(fingerprint, LinkPolicyAction.FILE)
        if decision is not None and not decision.allowed:
            return self._policy_rejection(decision)

        try:
            body = await request.json(loads=strict_json_loads)
            chunk_request = FileChunkRequest.from_dict(body)
        except (KeyError, ValueError, TypeError) as exc:
            return web.json_response({"error": f"malformed file chunk request: {exc}"}, status=400)

        if self._enforce_trust_policy:
            if chunk_request.authorization is None:
                return web.json_response(
                    {"error": "authenticated file chunk authorization required"}, status=403
                )
            try:
                self._node.handle_inventory_request(fingerprint, chunk_request.authorization)
            except LinkProtocolError as exc:
                return web.json_response({"error": str(exc)}, status=403)
            if any((
                chunk_request.authorization.boards,
                chunk_request.authorization.channels,
                chunk_request.authorization.file_areas,
            )):
                return web.json_response(
                    {"error": "file chunk authorization must carry an empty inventory"}, status=400
                )

        if not (0 < chunk_request.max_chunk_size <= _MAX_ALLOWED_CHUNK_SIZE_BYTES):
            return web.json_response(
                {"error": f"max_chunk_size must be between 1 and {_MAX_ALLOWED_CHUNK_SIZE_BYTES}"}, status=400
            )

        active = self._active_transfers_by_peer.setdefault(fingerprint, set())
        if chunk_request.transfer_id not in active:
            if len(active) >= self._max_concurrent_file_transfers_per_peer:
                return web.json_response(
                    {
                        "error": f"{fingerprint} already has {len(active)} concurrent file transfers "
                        "with this node -- refusing a new one"
                    },
                    status=429,
                )
            active.add(chunk_request.transfer_id)

        try:
            chunk_bytes, chunk_size, total_size, is_last = await self._lane.run(
                build_chunk_for_serving,
                file_id=chunk_request.file_id, chunk_index=chunk_request.chunk_index,
                max_chunk_size=chunk_request.max_chunk_size,
            )
        except FileTransferError as exc:
            active.discard(chunk_request.transfer_id)
            return web.json_response({"error": str(exc)}, status=400)

        if is_last:
            active.discard(chunk_request.transfer_id)

        descriptor = build_file_chunk_descriptor(
            signing_identity=self._node.identity.signing_key,
            file_id=chunk_request.file_id,
            chunk_index=chunk_request.chunk_index,
            chunk_sha256=hashlib.sha256(chunk_bytes).hexdigest(),
            chunk_size=chunk_size,
            total_size=total_size,
            is_last=is_last,
            created_at=utc_now_iso(),
        )
        envelope_b64 = base64.b64encode(json.dumps(descriptor.to_dict()).encode("utf-8")).decode("ascii")
        return web.Response(
            body=chunk_bytes,
            status=200,
            headers={"X-NetBBS-Chunk-Envelope": envelope_b64},
            content_type="application/octet-stream",
        )

    async def _handle_peers(self, request: web.Request) -> web.Response:
        """
        Peer-list exchange: shares this node's own currently-
        verified peers' endpoint descriptors with whoever asks.
        Deliberately unauthenticated, like `/hello` itself — the design doc
        already treats reachability information as discoverable
        bootstrap data, not something trust-gated ("a seed only ever
        supplies reachability information; it grants no trust"). A
        bodyless GET carries no signed claim about who's asking, so
        there is nothing here to verify even if this endpoint wanted to
        gate on it.
        """
        if self._enforce_trust_policy:
            fingerprint = request.match_info["fingerprint"]
            try:
                body = await request.json(loads=strict_json_loads)
                signed_request = InventoryRequest.from_dict(body)
                if signed_request.boards or signed_request.channels or signed_request.file_areas:
                    raise ValueError("peer-list authorization request must have an empty inventory")
                self._node.handle_inventory_request(fingerprint, signed_request)
            except (KeyError, TypeError, ValueError, LinkProtocolError) as exc:
                return web.json_response({"error": f"invalid peer-list request: {exc}"}, status=403)
            decision = await self._decide(fingerprint, LinkPolicyAction.PEER_LIST)
            if decision is not None and not decision.allowed:
                return self._policy_rejection(decision)
        return web.json_response(self._node.build_peer_list().to_dict())

    async def _handle_relay_consent(self, request: web.Request) -> web.Response:
        """
        Issue #58: answer a `relay_consent_request` synchronously,
        in the *same* HTTP response -- the only shape that works for a
        requester who may itself be outgoing-only and can never be dialed
        back (see `RelayConsentRequest`'s own docstring). Mirrors `_handle_
        hello`'s own "reply carried in the response body" shape exactly.

        The opt-out/resource-cap policy decision (`self._relay_serving_
        enabled`/`self._max_relay_clients`, issue #58) lives
        here, not in `LinkNode` itself -- `handle_relay_consent_request`
        only ever verifies, deliberately never decides (see that
        method's own docstring: this pure/in-memory layer has no config
        to judge capacity against). A declined request -- whether from
        the opt-out or the cap -- is still a normal, signed `accepted=
        False` response, not an HTTP error: declining is an ordinary
        outcome of this exchange, not a protocol violation.
        """
        fingerprint = request.match_info["fingerprint"]
        try:
            body = await request.json(loads=strict_json_loads)
            consent_request = RelayConsentRequest.from_dict(body)
        except (KeyError, ValueError, TypeError) as exc:
            return web.json_response({"error": f"malformed relay_consent_request: {exc}"}, status=400)

        try:
            self._node.handle_relay_consent_request(fingerprint, consent_request)
        except LinkProtocolError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        decision = await self._decide(fingerprint, LinkPolicyAction.RELAY)
        if decision is not None and not decision.allowed:
            return self._policy_rejection(decision)

        accepted = self._relay_serving_enabled and len(self._node.relaying_for) < self._max_relay_clients
        decided_at = utc_now_iso()

        response = build_relay_consent_response(
            signing_identity=self._node.identity.signing_key,
            request_content_id=consent_request.content_id,
            relay_fingerprint=self._node.identity.fingerprint,
            requester_fingerprint=fingerprint,
            accepted=accepted,
            created_at=decided_at,
        )

        if accepted:
            self._node.relaying_for[fingerprint] = decided_at
            await self._lane.run(save_relay_consent, fingerprint, role="i_relay_for", accepted_at=decided_at)

        return web.json_response(response.to_dict())

    async def _handle_relay_mailbox_deposit(self, request: web.Request) -> web.Response:
        """
        Issue #58 (widened by issue #94's ack-relay sibling fix): accept
        one opaque `link_message`/`link_message_accepted`/`link_message_
        bounced` for `recipient_fingerprint`, held until that recipient
        itself picks it up (`_handle_relay_mailbox_pickup`). Unlike every
        other route on this server, the depositing caller need not be a
        completed peer -- receiving on behalf of a stranger is the
        entire point of relaying (see `netbbs.link.relay_mailbox`'s own
        module docstring for why no signature verification happens here
        either: this node can't meaningfully check a signature for an
        identity chain it may have never seen, and doesn't need to -- the
        recipient re-verifies everything itself after pickup).
        """
        recipient_fingerprint = request.match_info["fingerprint"]
        try:
            body = await request.json(loads=strict_json_loads)
            object_type = body["envelope"]["object_type"]
            envelope_cls = {
                LINK_MESSAGE_OBJECT_TYPE: LinkMessage,
                LINK_MESSAGE_ACCEPTED_OBJECT_TYPE: LinkMessageAccepted,
                LINK_MESSAGE_BOUNCED_OBJECT_TYPE: LinkMessageBounced,
            }.get(object_type)
            if envelope_cls is None:
                return web.json_response(
                    {"error": f"{object_type!r} may not be deposited into a relay mailbox"}, status=400
                )
            message: RelayableEnvelope = envelope_cls.from_dict(body)
        except (KeyError, ValueError, TypeError) as exc:
            return web.json_response({"error": f"malformed relay mailbox deposit: {exc}"}, status=400)

        if recipient_fingerprint not in self._node.relaying_for:
            return web.json_response(
                {"error": f"this node is not currently relaying for {recipient_fingerprint}"}, status=404
            )

        recipient_decision = await self._decide(recipient_fingerprint, LinkPolicyAction.RELAY)
        if recipient_decision is not None and not recipient_decision.allowed:
            return self._policy_rejection(recipient_decision)
        if self._enforce_trust_policy:
            author_decision = await self._lane.run(
                decide_event_authorship, message.to_dict(),
                transport_peer_fingerprint=recipient_fingerprint,
            )
            if not author_decision.allowed:
                return self._policy_rejection(author_decision)

        try:
            await self._lane.run(deposit_relay_mailbox_envelope, recipient_fingerprint, message)
        except RelayMailboxFullError as exc:
            return web.json_response({"error": str(exc)}, status=507)

        return web.json_response({"deposited": True})

    async def _handle_relay_mailbox_pickup(self, request: web.Request) -> web.Response:
        """
        Issue #58: hand back (and clear) whatever mail this
        relay is currently holding for the caller. Authenticated by
        requiring a fresh, verifiable `hello` as the request body rather
        than inventing a new signed message type — a hello already
        cryptographically proves the caller's identity (its descriptor
        signature verifies against the claimed fingerprint's own
        resolved signing key, the same check `_handle_hello` already
        performs), which is exactly the property picking up someone
        else's held mail needs and a bare GET keyed only by a URL path
        fingerprint would not have (see this method's own module-level
        context: `netbbs.link.relay_mailbox` deliberately has no notion
        of who's *allowed* to pick up, since it isn't the layer that can
        check that).
        """
        try:
            body = await request.json(loads=strict_json_loads)
            hello = HelloMessage.from_dict(body)
        except (KeyError, ValueError, TypeError) as exc:
            return web.json_response({"error": f"malformed hello: {exc}"}, status=400)

        try:
            peer = self._node.handle_hello(hello, max_peers=self._max_peers)
        except LinkProtocolError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        decision = await self._decide(peer.fingerprint, LinkPolicyAction.RELAY)
        if decision is not None and not decision.allowed:
            return self._policy_rejection(decision)
        await self._lane.run(save_peer, peer)

        envelopes = await self._lane.run(pickup_relay_mailbox_envelopes, peer.fingerprint)
        return web.json_response({"envelopes": [e.to_dict() for e in envelopes]})


async def dial_hello(
    node: LinkNode,
    session: ClientSession,
    base_url: str,
    hello: HelloMessage,
    lane: DatabaseLane,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> PeerRecord:
    """
    Say hello to a peer at `base_url` (e.g. `"http://198.51.100.7:7862"`,
    no trailing slash): POST `hello`, feed the peer's own hello — carried
    back in the response — into `node.handle_hello`, persist the
    resulting `PeerRecord` via `lane`, and return it.

    Raises `LinkTransportError` for anything transport-level gone wrong
    (connection failure, timeout, non-200, an unparseable response
    body). If the peer's own returned hello fails verification,
    `LinkProtocolError` propagates unwrapped from `node.handle_hello` —
    same exception every other caller of that method already handles.
    """
    url = f"{base_url}{LINK_PATH_PREFIX}/hello"
    try:
        async with session.post(
            url, json=hello.to_dict(), timeout=ClientTimeout(total=timeout)
        ) as response:
            if response.status != 200:
                text = await response.text()
                raise LinkTransportError(f"hello to {url} failed: HTTP {response.status}: {text}")
            body = await response.json(loads=strict_json_loads)
    except (ClientError, TimeoutError, ValueError) as exc:
        raise LinkTransportError(f"could not reach {url}: {exc}") from exc

    try:
        peer_hello = HelloMessage.from_dict(body)
    except (KeyError, ValueError, TypeError) as exc:
        raise LinkTransportError(f"malformed hello response from {url}: {exc}") from exc

    peer = node.handle_hello(peer_hello)
    await lane.run(save_peer, peer)
    return peer


async def push_events(
    node: LinkNode,
    session: ClientSession,
    base_url: str,
    events: list[
        KeyTransition | BoardGenesis | BoardPost | BoardPostEdit
        | BoardPostModeratorEdit | BoardPostTombstone
        | BoardOriginTransferOffer | BoardOriginTransferAccepted | BoardClosure
        | LinkMessage | LinkMessageAccepted | LinkMessageBounced
    ],
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> list[str]:
    """
    Push `events` — this node's *own* originated events (`key_
    transition`s, `board_genesis`/`board_post`/`board_post_edit`/`board_
    post_moderator_edit`/`board_post_tombstone` (issue #88),
    `board_origin_transfer_offer`/`board_origin_transfer_
    accepted`/`board_closure` (issues #53/#88), and `link_
    message`/`link_message_accepted`/`link_message_bounced`) — per the
    "no relay from a stranger" scope note —
    to a peer at `base_url`. Returns whichever content_ids the peer
    newly accepted; purely informational, since the sender's own copies
    are already known-good on its own side.

    Raises `LinkTransportError` for a transport-level failure. A
    peer rejecting one of the pushed events (e.g. an inconsistent
    chain) also surfaces as `LinkTransportError` here — unlike
    `dial_hello`, the rejection reason lives only in the peer's HTTP
    error body, not as a `LinkProtocolError` raised locally, since
    nothing on this side re-runs the peer's own verification.
    """
    url = f"{base_url}{LINK_PATH_PREFIX}/events/{node.identity.fingerprint}"
    payload = [e.to_dict() for e in events]
    try:
        async with session.post(
            url, json=payload, timeout=ClientTimeout(total=timeout)
        ) as response:
            if response.status != 200:
                text = await response.text()
                raise LinkTransportError(f"events push to {url} failed: HTTP {response.status}: {text}")
            body = await response.json(loads=strict_json_loads)
    except (ClientError, TimeoutError, ValueError) as exc:
        raise LinkTransportError(f"could not reach {url}: {exc}") from exc

    try:
        return body["accepted"]
    except (KeyError, TypeError) as exc:
        raise LinkTransportError(f"malformed events response from {url}: {exc}") from exc


async def request_inventory(
    node: LinkNode,
    session: ClientSession,
    base_url: str,
    inventory_request: InventoryRequest,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> tuple[list[dict], bool]:
    """
    Design doc §8.8, issue #85: ask a peer at `base_url` what it has for
    `inventory_request.boards` that this node doesn't already. Returns
    the raw event dicts it reports (already in `push_events`'s own wire
    shape -- the caller feeds them through `LinkNode.handle_events`
    exactly as it would a push response, with no translation) and
    whether more remain beyond the peer's own response cap.

    Deliberately returns the raw dicts rather than applying them itself
    -- unlike `push_events` (whose sender already trusts its own
    events), this side must run real verification before trusting
    anything the peer claims to have, and `handle_events` is a `LinkNode`
    method with no I/O of its own; the caller (`netbbs.link.sync`) is
    the one already holding both `node` and a `DatabaseLane` to persist
    whatever gets accepted, the same shape `_pickup_relay_mail` already
    uses for an analogous "verify and persist what a fetch returned"
    step.

    Raises `LinkTransportError` for a transport-level failure, matching
    every other client function in this module.
    """
    url = f"{base_url}{LINK_PATH_PREFIX}/inventory/{node.identity.fingerprint}"
    try:
        async with session.post(
            url, json=inventory_request.to_dict(), timeout=ClientTimeout(total=timeout)
        ) as response:
            if response.status != 200:
                text = await response.text()
                raise LinkTransportError(f"inventory request to {url} failed: HTTP {response.status}: {text}")
            body = await response.json(loads=strict_json_loads)
    except (ClientError, TimeoutError, ValueError) as exc:
        raise LinkTransportError(f"could not reach {url}: {exc}") from exc

    try:
        return body["events"], bool(body["more_available"])
    except (KeyError, TypeError) as exc:
        raise LinkTransportError(f"malformed inventory response from {url}: {exc}") from exc


async def request_trust_objects(
    node: LinkNode,
    session: ClientSession,
    base_url: str,
    pull_request: TrustPullRequest,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> tuple[list[dict], bool]:
    """Pull one bounded page of unchanged issuer-signed trust objects."""
    url = f"{base_url}{LINK_PATH_PREFIX}/trust-pull/{node.identity.fingerprint}"
    try:
        async with session.post(
            url, json=pull_request.to_dict(), timeout=ClientTimeout(total=timeout)
        ) as response:
            if response.status != 200:
                text = await response.text()
                raise LinkTransportError(
                    f"trust pull from {url} failed: HTTP {response.status}: {text}"
                )
            body = await response.json(loads=strict_json_loads)
    except (ClientError, TimeoutError, ValueError) as exc:
        raise LinkTransportError(f"could not reach {url}: {exc}") from exc
    try:
        objects = body["objects"]
        if not isinstance(objects, list):
            raise TypeError("objects is not a list")
        return objects, bool(body["more_available"])
    except (KeyError, TypeError) as exc:
        raise LinkTransportError(f"malformed trust pull response from {url}: {exc}") from exc


async def fetch_trust_evidence(
    session: ClientSession,
    reporter_base_url: str,
    evidence: dict,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> tuple[bytes, object]:
    """Fetch one signed digest locator without granting it arbitrary network access."""
    locator = evidence.get("locator")
    if not isinstance(locator, str):
        raise LinkTransportError("trust evidence locator is missing")
    base = urlparse(reporter_base_url)
    url = urljoin(reporter_base_url.rstrip("/") + "/", locator)
    target = urlparse(url)
    if (
        base.scheme not in {"http", "https"}
        or (target.scheme, target.hostname, target.port) != (base.scheme, base.hostname, base.port)
        or target.username is not None
        or target.password is not None
        or target.fragment
    ):
        raise LinkTransportError("trust evidence locator must stay on the reporter origin")
    try:
        async with session.get(url, timeout=ClientTimeout(total=timeout)) as response:
            if response.status != 200:
                raise LinkTransportError(
                    f"trust evidence fetch from {url} failed: HTTP {response.status}"
                )
            if response.content_length is not None and response.content_length > MAX_EMBEDDED_EVIDENCE_BYTES:
                raise LinkTransportError("trust evidence body exceeds the 256 KiB limit")
            content = await response.content.read(MAX_EMBEDDED_EVIDENCE_BYTES + 1)
    except (ClientError, TimeoutError) as exc:
        raise LinkTransportError(f"could not reach {url}: {exc}") from exc
    if len(content) > MAX_EMBEDDED_EVIDENCE_BYTES:
        raise LinkTransportError("trust evidence body exceeds the 256 KiB limit")
    try:
        return content, verify_evidence_bytes(evidence, content)
    except TrustWireError as exc:
        raise LinkTransportError(f"invalid trust evidence from {url}: {exc}") from exc


async def request_file_chunk(
    node: LinkNode,
    session: ClientSession,
    base_url: str,
    chunk_request: FileChunkRequest,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> tuple[bytes, FileChunkDescriptor]:
    """
    Design doc §11.3, issue #89: ask the peer at `base_url` (always the
    requested file's own origin -- chunk transfer is never relayed) for
    one chunk. Returns the raw bytes body exactly as received (never
    base64-decoded here -- there was never any base64 to begin with) and
    the signed `FileChunkDescriptor` parsed out of the `X-NetBBS-Chunk-
    Envelope` response header. Deliberately returns the descriptor
    unverified -- same division of responsibility `request_inventory`
    already documents: this function only does I/O and parsing, the
    caller (`fetch_next_file_chunk`) is the one holding `node`'s own peer
    table to verify against.

    Raises `LinkTransportError` for a transport-level failure or a
    missing/malformed envelope header, matching every other client
    function in this module.
    """
    url = f"{base_url}{LINK_PATH_PREFIX}/file-chunk/{node.identity.fingerprint}"
    try:
        async with session.post(
            url, json=chunk_request.to_dict(), timeout=ClientTimeout(total=timeout)
        ) as response:
            if response.status != 200:
                text = await response.text()
                raise LinkTransportError(f"file chunk request to {url} failed: HTTP {response.status}: {text}")
            chunk_bytes = await response.read()
            envelope_header = response.headers.get("X-NetBBS-Chunk-Envelope")
    except (ClientError, TimeoutError) as exc:
        raise LinkTransportError(f"could not reach {url}: {exc}") from exc

    if envelope_header is None:
        raise LinkTransportError(f"file chunk response from {url} carried no X-NetBBS-Chunk-Envelope header")
    try:
        descriptor = FileChunkDescriptor.from_dict(
            strict_json_loads(base64.b64decode(envelope_header).decode("utf-8"))
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise LinkTransportError(f"malformed chunk envelope header from {url}: {exc}") from exc

    return chunk_bytes, descriptor


async def fetch_next_file_chunk(
    node: LinkNode,
    session: ClientSession,
    base_url: str,
    lane: DatabaseLane,
    remote_file: RemoteFile,
    *,
    chunk_size: int = _DEFAULT_FILE_CHUNK_SIZE,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> TransferState:
    """
    Design doc §11.3, issue #89: fetch exactly one more chunk of
    `remote_file` from `base_url` (its own origin) and apply it --
    the top-level orchestration a caller (a future interactive "fetch
    this file" action, or a background catch-up pass) calls repeatedly
    until the returned `TransferState.status` is no longer
    `'in_progress'`. Idempotent to call again after `'completed'` --
    returns the already-completed state without another round trip.

    Raises `LinkProtocolError` if `remote_file.origin_fingerprint` has no
    completed hello with this node (chunk transfer is never relayed --
    there is no "no relay from a stranger" exception for content bytes),
    or if the response's signed `FileChunkDescriptor` doesn't verify
    against the origin's *current* signing key, or doesn't actually
    describe the chunk this node just asked for (`file_id`/`chunk_index`
    cross-checked against the outgoing request, the same "the response
    must match what was asked" discipline `request_relay_consent`
    already applies to its own reply). `netbbs.link.file_transfer.
    FileTransferError` propagates unwrapped for a content-integrity
    failure (chunk bytes not matching their own claimed hash, or the
    completed reassembly not matching the file's own catalogued hash).
    """
    origin_peer = node.peers.get(remote_file.origin_fingerprint)
    if origin_peer is None:
        raise LinkProtocolError(
            f"file {remote_file.file_id!r}'s own origin ({remote_file.origin_fingerprint!r}) has no "
            "completed hello with this node -- refusing (chunk transfer is never relayed)"
        )

    transfer = await lane.run(
        get_or_create_transfer,
        remote_file, requester_fingerprint=node.identity.fingerprint, chunk_size=chunk_size,
    )
    if transfer.status != "in_progress":
        return transfer

    chunk_index = transfer.next_chunk_index
    authorization = await lane.run(
        build_inventory_request,
        signing_identity=node.identity.signing_key,
        requester_fingerprint=node.identity.fingerprint,
        responder_fingerprint=remote_file.origin_fingerprint,
        include_inventory=False,
    )
    chunk_request = FileChunkRequest(
        transfer_id=transfer.transfer_id, file_id=remote_file.file_id,
        chunk_index=chunk_index, max_chunk_size=transfer.chunk_size,
        authorization=authorization,
    )
    chunk_bytes, descriptor = await request_file_chunk(node, session, base_url, chunk_request, timeout=timeout)

    if descriptor.payload.get("file_id") != remote_file.file_id or descriptor.payload.get("chunk_index") != chunk_index:
        raise LinkProtocolError(
            f"file chunk response from {base_url} describes a different file/chunk than requested -- refusing"
        )

    signing_key_b64 = resolve_current_operational_key(
        origin_peer.transitions,
        root_verify_key=origin_peer.root_verify_key,
        subject_fingerprint=remote_file.origin_fingerprint,
        purpose="signing",
    )
    if signing_key_b64 is None:
        raise LinkProtocolError(
            f"rejected file_chunk_descriptor from {remote_file.origin_fingerprint}: no currently-"
            "authorized signing key"
        )
    signing_verify_key = nacl.signing.VerifyKey(base64.b64decode(signing_key_b64))
    if not verify_file_chunk_descriptor(descriptor, signing_verify_key):
        raise LinkProtocolError(
            f"file_chunk_descriptor from origin {remote_file.origin_fingerprint} does not verify "
            "against its current signing key"
        )

    return await lane.run(
        apply_received_chunk,
        transfer, chunk_index=chunk_index, chunk_bytes=chunk_bytes,
        claimed_chunk_sha256=descriptor.payload["chunk_sha256"], is_last=descriptor.payload["is_last"],
        remote_file=remote_file,
    )


def dialable_base_urls_for_peer(node: LinkNode, fingerprint: str) -> list[str]:
    """
    Design doc §11.3/§12, issue #92: every advertised address on file for
    `fingerprint`, as dialable base URLs, in the order the descriptor
    itself lists them -- used by an interactive "fetch this remote file"
    UI action (`netbbs.net.file_flow`) to find where to reach a file's
    own origin directly. Chunk transfer is never relayed (`fetch_next_
    file_chunk`'s own docstring), so an outgoing-only origin with no
    advertised direct address is simply unreachable for this purpose --
    an empty list, same as `netbbs.link.sync._dialable_addresses` already
    returns for the identical case in its own (push-only) context.

    Empty if `fingerprint` has no completed hello on file at all -- the
    same "no relay from a stranger" precondition `fetch_next_file_chunk`
    itself independently enforces.

    Filtered to the HTTP-family `protocol` tags only -- an
    `endpoint_descriptor`'s `addresses` list can also carry a
    `LINK_REALTIME_PROTOCOL_TAG` entry (design doc §8.10: "advertises
    real-time TCP addresses separately from HTTP addresses"), which is
    not a dialable base URL and would otherwise silently corrupt this
    list for an HTTP-only caller like `netbbs.net.file_flow`. See
    `dialable_realtime_addresses_for_peer` for that entry's own reader.
    """
    peer = node.peers.get(fingerprint)
    if peer is None:
        return []
    addresses = peer.descriptor.payload.get("addresses")
    if not addresses:
        return []
    return [
        f"{a['protocol']}://{a['address']}:{a['port']}"
        for a in addresses
        if a["protocol"] in ("http", "https")
    ]


# design doc §8.10: "The endpoint descriptor advertises real-time TCP
# addresses separately from HTTP addresses." One more `addresses` entry
# `protocol` tag, distinguished from "http"/"https" the same way those
# two are distinguished from each other -- no new descriptor field.
LINK_REALTIME_PROTOCOL_TAG = "link-realtime-tcp"


def dialable_realtime_addresses_for_peer(node: LinkNode, fingerprint: str) -> list[tuple[str, int]]:
    """Every advertised real-time (Noise/TCP) address on file for
    `fingerprint`, as `(host, port)` pairs, in descriptor order --
    the real-time counterpart to `dialable_base_urls_for_peer`."""
    peer = node.peers.get(fingerprint)
    if peer is None:
        return []
    addresses = peer.descriptor.payload.get("addresses")
    if not addresses:
        return []
    return [(a["address"], a["port"]) for a in addresses if a["protocol"] == LINK_REALTIME_PROTOCOL_TAG]


async def request_peer_list(
    node: LinkNode,
    session: ClientSession,
    base_url: str,
    peer_fingerprint: str,
    lane: DatabaseLane,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> list[str]:
    """
    Request `base_url`'s own peer list (design doc §12) and feed it into
    `node.handle_peer_list`, persisting each newly recorded/refreshed
    candidate via `lane` (`netbbs.link.store.save_candidate_descriptor`)
    the same way `dial_hello` persists its own resulting `PeerRecord` —
    returns the fingerprints newly recorded.

    `peer_fingerprint` is the caller's to supply, not derived from the
    response — unlike a hello, a bodyless peer-list response carries no
    self-identifying claim about who answered it, so the caller (who
    already completed a real hello with whoever is at `base_url` before
    ever calling this) is the only one who actually knows. Raises
    `LinkProtocolError` unwrapped if `peer_fingerprint` turns out not to
    be a completed peer after all — same division of responsibility
    `dial_hello`'s own `node.handle_hello` call already has.
    """
    url = f"{base_url}{LINK_PATH_PREFIX}/peers/{node.identity.fingerprint}"
    authorization = await lane.run(
        build_inventory_request,
        signing_identity=node.identity.signing_key,
        requester_fingerprint=node.identity.fingerprint,
        responder_fingerprint=peer_fingerprint,
        include_inventory=False,
    )
    try:
        async with session.post(
            url, json=authorization.to_dict(), timeout=ClientTimeout(total=timeout)
        ) as response:
            if response.status != 200:
                text = await response.text()
                raise LinkTransportError(f"peer list request to {url} failed: HTTP {response.status}: {text}")
            body = await response.json(loads=strict_json_loads)
    except (ClientError, TimeoutError, ValueError) as exc:
        raise LinkTransportError(f"could not reach {url}: {exc}") from exc

    try:
        message = PeerListMessage.from_dict(body)
    except (KeyError, ValueError, TypeError) as exc:
        raise LinkTransportError(f"malformed peer list response from {url}: {exc}") from exc

    recorded = node.handle_peer_list(peer_fingerprint, message)
    for candidate_fingerprint in recorded:
        await lane.run(
            save_candidate_descriptor, candidate_fingerprint, node.candidate_descriptors[candidate_fingerprint]
        )
    return recorded


async def request_relay_consent(
    node: LinkNode,
    session: ClientSession,
    base_url: str,
    relay_fingerprint: str,
    lane: DatabaseLane,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> RelayConsentResponse:
    """
    Ask the peer at `base_url` (already a completed peer named
    `relay_fingerprint` -- same "caller already completed a real hello"
    precondition `request_peer_list` documents) to relay for this node
    (design doc §12, issue #58): build and sign a `relay_
    consent_request`, POST it to `/relay-consent/{this node's own
    fingerprint}`, and verify the answer carried back in the *same* HTTP
    response (`LinkServer._handle_relay_consent`'s own synchronous-reply
    shape -- see `RelayConsentRequest`'s docstring for why this can't be
    a `push_events`-style fire-and-forget the way every gossiped event
    pair is).

    On an accepted response, records `relay_fingerprint` into `node.
    relays_serving_me` and persists the grant via `lane`. A declined
    response is returned as-is, unpersisted (see `save_relay_consent`'s
    own docstring for why) -- not an error, an ordinary outcome of this
    exchange the caller (relay *selection*, issue #58 task #25) decides
    what to do about, e.g. trying the next-ranked candidate.

    Raises `LinkTransportError` for a transport-level failure. If the
    returned response fails verification, `LinkProtocolError` propagates
    unwrapped from `node.handle_relay_consent_response` — same division
    of responsibility every other caller of a `handle_*` method already
    has.
    """
    created_at = utc_now_iso()
    consent_request = build_relay_consent_request(
        signing_identity=node.identity.signing_key,
        requester_fingerprint=node.identity.fingerprint,
        relay_fingerprint=relay_fingerprint,
        created_at=created_at,
    )
    node.pending_own_relay_requests[relay_fingerprint] = consent_request

    url = f"{base_url}{LINK_PATH_PREFIX}/relay-consent/{node.identity.fingerprint}"
    try:
        async with session.post(
            url, json=consent_request.to_dict(), timeout=ClientTimeout(total=timeout)
        ) as response:
            if response.status != 200:
                text = await response.text()
                raise LinkTransportError(f"relay consent request to {url} failed: HTTP {response.status}: {text}")
            body = await response.json(loads=strict_json_loads)
    except (ClientError, TimeoutError, ValueError) as exc:
        raise LinkTransportError(f"could not reach {url}: {exc}") from exc
    finally:
        node.pending_own_relay_requests.pop(relay_fingerprint, None)

    try:
        consent_response = RelayConsentResponse.from_dict(body)
    except (KeyError, ValueError, TypeError) as exc:
        raise LinkTransportError(f"malformed relay consent response from {url}: {exc}") from exc

    node.handle_relay_consent_response(relay_fingerprint, consent_response, original_request=consent_request)

    if consent_response.payload["accepted"]:
        accepted_at = consent_response.payload["created_at"]
        node.relays_serving_me[relay_fingerprint] = accepted_at
        await lane.run(save_relay_consent, relay_fingerprint, role="relay_for_me", accepted_at=accepted_at)

    return consent_response


async def deposit_into_relay_mailbox(
    session: ClientSession,
    relay_base_url: str,
    recipient_fingerprint: str,
    message: RelayableEnvelope,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """
    Leave `message` (a `link_message`/`link_message_accepted`/`link_
    message_bounced` this node couldn't deliver directly) at the relay
    reachable at `relay_base_url`, for `recipient_fingerprint` to pick up
    on its own next outbound sync pass (design doc §12, issue #58; issue
    #94 widened this from `link_message` alone to all three). Does not
    require a completed hello with the relay first — see `LinkServer.
    _handle_relay_mailbox_deposit`'s own docstring for why depositing is
    the one route on this server that's intentionally open to a stranger.

    Raises `LinkTransportError` for a transport-level failure, including
    the relay reporting it isn't currently relaying for `recipient_
    fingerprint`, or that its mailbox for that recipient is full — both
    surface as a non-200 response, same as any other rejected request
    on this transport.
    """
    url = f"{relay_base_url}{LINK_PATH_PREFIX}/relay-mailbox/{recipient_fingerprint}/deposit"
    try:
        async with session.post(
            url, json=message.to_dict(), timeout=ClientTimeout(total=timeout)
        ) as response:
            if response.status != 200:
                text = await response.text()
                raise LinkTransportError(f"relay mailbox deposit to {url} failed: HTTP {response.status}: {text}")
    except (ClientError, TimeoutError) as exc:
        raise LinkTransportError(f"could not reach {url}: {exc}") from exc


async def pickup_from_relay_mailbox(
    session: ClientSession,
    relay_base_url: str,
    hello: HelloMessage,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> list[RelayableEnvelope]:
    """
    Pick up (and clear) whatever mail the relay at `relay_base_url` is
    currently holding for this node -- design doc §12, issue
    #58. `hello` is this node's own current hello bundle, the caller's
    to supply (same "deployment config isn't this layer's concern"
    reasoning `dial_hello` already applies to its own `hello` parameter)
    -- it's what authenticates this call (`_handle_relay_mailbox_
    pickup`'s own docstring explains why a hello, not a new signed
    message type).

    Returns raw, **not yet verified** `LinkMessage`/`LinkMessageAccepted`/
    `LinkMessageBounced` objects (issue #94 widened this from `link_
    message` alone), each reconstructed by its own envelope's `object_
    type` -- the caller (issue #58 task #25's sync-loop wiring) is
    responsible for running each one through `LinkNode.handle_events`
    (keyed by that envelope's own claimed sender/signer, not this relay)
    before treating it as accepted, same as `netbbs.link.relay_mailbox.
    pickup_relay_mailbox_envelopes`'s own docstring already documents on
    the server side.

    Raises `LinkTransportError` for a transport-level failure.
    """
    url = f"{relay_base_url}{LINK_PATH_PREFIX}/relay-mailbox/pickup"
    try:
        async with session.post(
            url, json=hello.to_dict(), timeout=ClientTimeout(total=timeout)
        ) as response:
            if response.status != 200:
                text = await response.text()
                raise LinkTransportError(f"relay mailbox pickup from {url} failed: HTTP {response.status}: {text}")
            body = await response.json(loads=strict_json_loads)
    except (ClientError, TimeoutError, ValueError) as exc:
        raise LinkTransportError(f"could not reach {url}: {exc}") from exc

    try:
        raw_envelopes = body["envelopes"]
    except (KeyError, TypeError) as exc:
        raise LinkTransportError(f"malformed relay mailbox pickup response from {url}: {exc}") from exc

    envelope_types_by_object_type = {
        LINK_MESSAGE_OBJECT_TYPE: LinkMessage,
        LINK_MESSAGE_ACCEPTED_OBJECT_TYPE: LinkMessageAccepted,
        LINK_MESSAGE_BOUNCED_OBJECT_TYPE: LinkMessageBounced,
    }
    try:
        result = []
        for raw in raw_envelopes:
            envelope_cls = envelope_types_by_object_type[raw["envelope"]["object_type"]]
            result.append(envelope_cls.from_dict(raw))
        return result
    except (KeyError, ValueError, TypeError) as exc:
        raise LinkTransportError(f"malformed envelope in relay mailbox pickup response from {url}: {exc}") from exc
