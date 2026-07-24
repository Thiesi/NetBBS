"""
Persistent storage for `netbbs.link.protocol.LinkNode`'s peer table and
seen-event/event-body store (design doc) -- backs the
`link_peers`/`link_events` tables (`netbbs.storage.migrations`) with
plain `db`-first sync functions, matching every other lane-dispatched
function's existing convention (`netbbs.storage.execution.DatabaseLane.
run` injects `db` as the first positional argument).

Deliberately outside `netbbs.link.protocol` itself -- `LinkNode` stays
pure, synchronous, in-memory
(`tests/link_harness.py`'s `ScriptedTransport` calls `handle_hello`/
`handle_events` directly with zero I/O, a property this module doesn't
disturb). Callers -- `netbbs.link.transport`'s `LinkServer` and
`dial_hello`, the only places that already own I/O at this layer --
call these functions themselves, via `DatabaseLane.run`, after a
successful `handle_hello`/`handle_events`.

Issue #86: `key_transition` rows get a bounded, age-based purge
(`purge_expired_key_transitions`, called inline on every accepted
`key_transition` -- see that function's own docstring for why it alone,
among every object type this table stores, is provably safe to purge).
Every board-scoped object type stays unbounded -- restart reconstruction
(`load_link_node`, this module) and issue #85's own inventory diff
(`board_event_diff`) both still depend on those rows surviving; see
design doc §8.9 for the full per-type trace.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta

from netbbs.link.events import (
    BOARD_CLOSURE_OBJECT_TYPE,
    BOARD_GENESIS_OBJECT_TYPE,
    BOARD_ORIGIN_TRANSFER_ACCEPTED_OBJECT_TYPE,
    BOARD_ORIGIN_TRANSFER_OFFER_OBJECT_TYPE,
    BOARD_POST_EDIT_OBJECT_TYPE,
    BOARD_POST_MODERATOR_EDIT_OBJECT_TYPE,
    BOARD_POST_TOMBSTONE_OBJECT_TYPE,
    CHANNEL_GENESIS_OBJECT_TYPE,
    KEY_TRANSITION_OBJECT_TYPE,
    BoardClosure,
    BoardGenesis,
    BoardOriginTransferAccepted,
    BoardOriginTransferOffer,
    BoardPostEdit,
    BoardPostModeratorEdit,
    BoardPostTombstone,
    ChannelGenesis,
    ChannelMessage,
    EndpointDescriptor,
    KeyTransition,
    event_content_id,
)
from netbbs.link.node_identity import NodeIdentity
from netbbs.link.protocol import InventoryRequest, LinkNode, PeerRecord
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


def load_link_node(db: Database, identity: NodeIdentity) -> LinkNode:
    """
    Reconstruct a `LinkNode` from persisted `link_peers`/`link_events`
    rows -- the replacement for a bare `LinkNode(identity=
    ...)` construction, so a restarted node doesn't forget its peers or
    reprocess/re-forward events it has already seen.

    `LinkServer._handle_events` already persists *any* accepted event
    generically, board_genesis/board_post included, with no type-specific
    code of its own -- so `link_events` already holds board_genesis rows
    that this function must do something with. Without also rebuilding
    `node.boards` here, a restarted node would forget every board_
    genesis it had already verified, and `handle_events` would then
    wrongly reject a legitimate resent board_post as having "no
    verified board_genesis on file" -- the same restart-forgets-
    state hazard already fixed for peers/events, extended to this
    derived index too.

    A board this node itself originated (`netbbs.link.
    boards.link_board`) never goes through `handle_events` at all --
    there's no peer to verify it against, it's self-signed -- so its
    genesis lives only on the local `boards` row's own `link_genesis_
    json` column, a completely different source from `link_events`
    above. Both are reconstructed into the same `node.boards` index
    here; without this half too, a restarted node would forget its
    *own* Linked boards and wrongly reject a remote user's legitimate
    `board_post` on a board it itself originated.

    The same two-source shape applies to `node.post_edits`
    -- peer-received `board_post_edit` rows come from `link_events`
    (queried in `received_at` order, since a chain reconstructed out of
    order would fail its own "does previous_event_id match the current
    head" check the moment it's used); self-originated ones
    (`netbbs.link.boards.queue_board_post_edit_if_linked`) live on the
    edited revision's own `posts.link_event_json` column instead, same
    as a self-originated `board_genesis` lives on `boards.link_genesis_
    json` rather than ever passing through `handle_events`.

    Issue #53: the same two-source shape again, applied to
    `node.board_origin`/`board_lifecycle_head`/`pending_origin_
    transfers` -- peer-received `board_origin_transfer_offer`/
    `_accepted` come from `link_events`; this node's own self-
    originated one (`netbbs.link.boards.offer_board_origin_transfer`/
    `accept_board_origin_transfer`) lives on `boards.link_lifecycle_
    json`. Reconstructed *before* `link_events` this time, not after --
    see the inline comment at that block for why the ordering hazard is
    different here than it is for genesis.
    """
    node = LinkNode(identity=identity)

    for row in db.connection.execute(
        "SELECT fingerprint, root_public_key, transitions_json, descriptor_json FROM link_peers"
    ):
        node.peers[row["fingerprint"]] = PeerRecord(
            fingerprint=row["fingerprint"],
            root_public_key=base64.b64decode(row["root_public_key"]),
            transitions=tuple(KeyTransition.from_dict(t) for t in json.loads(row["transitions_json"])),
            descriptor=EndpointDescriptor.from_dict(json.loads(row["descriptor_json"])),
        )

    # Issue #53: this node's own self-originated lifecycle
    # event (an offer made as current origin, or an acceptance made as
    # a newly-accepted origin), if any -- deliberately reconstructed
    # *before* the link_events loop below, not after (unlike genesis's
    # own self-originated block further down, which has no such
    # ordering hazard: one board has exactly one genesis, self-
    # originated XOR peer-received, never both). A board's lifecycle
    # state can legitimately move from self-authored to peer-observed
    # over time (this node offered, then the new origin's own
    # acceptance arrives back via ordinary sync) -- if this node has
    # since learned of that same transfer completing via link_events
    # (below, processed in received_at order), that more current fact
    # must win over this node's own now-stale offer, not the reverse.
    for row in db.connection.execute(
        "SELECT link_lifecycle_json FROM boards WHERE link_lifecycle_json IS NOT NULL"
    ):
        raw = json.loads(row["link_lifecycle_json"])
        lifecycle_object_type = raw["envelope"]["object_type"]
        if lifecycle_object_type == BOARD_ORIGIN_TRANSFER_OFFER_OBJECT_TYPE:
            offer = BoardOriginTransferOffer.from_dict(raw)
            board_id = offer.payload["board_id"]
            node.pending_origin_transfers[board_id] = offer
            node.board_lifecycle_head[board_id] = offer.content_id
        elif lifecycle_object_type == BOARD_CLOSURE_OBJECT_TYPE:
            # Design doc §9.5, issue #88: this node's own self-originated
            # closure of a board it's the current origin of.
            own_closure = BoardClosure.from_dict(raw)
            board_id = own_closure.payload["board_id"]
            node.board_closures[board_id] = own_closure
            node.board_lifecycle_head[board_id] = own_closure.content_id
        else:
            own_accepted = BoardOriginTransferAccepted.from_dict(raw)
            board_id = own_accepted.payload["board_id"]
            node.board_origin[board_id] = own_accepted.payload["new_origin_fingerprint"]
            node.board_lifecycle_head[board_id] = own_accepted.content_id

    for row in db.connection.execute(
        "SELECT content_id, object_type, envelope_json FROM link_events ORDER BY received_at ASC"
    ):
        envelope = json.loads(row["envelope_json"])
        node.known_event_ids.add(row["content_id"])
        node.events[row["content_id"]] = envelope
        if row["object_type"] == BOARD_GENESIS_OBJECT_TYPE:
            genesis = BoardGenesis.from_dict(envelope)
            node.boards[genesis.payload["board_id"]] = genesis
        elif row["object_type"] == CHANNEL_GENESIS_OBJECT_TYPE:
            # Design doc §9.6, issue #87: same restart-forgets-state
            # reasoning as BOARD_GENESIS_OBJECT_TYPE above, applied to
            # a carried channel's genesis.
            channel_genesis = ChannelGenesis.from_dict(envelope)
            node.channels[channel_genesis.payload["channel_id"]] = channel_genesis
        elif row["object_type"] == BOARD_POST_EDIT_OBJECT_TYPE:
            edit = BoardPostEdit.from_dict(envelope)
            root_post_id = edit.payload["root_post_id"]
            node.post_edits[root_post_id] = node.post_edits.get(root_post_id, ()) + (edit,)
        elif row["object_type"] == BOARD_POST_MODERATOR_EDIT_OBJECT_TYPE:
            # Design doc §9.5, issue #88: same restart reconstruction as
            # BOARD_POST_EDIT_OBJECT_TYPE above, same shared chain.
            mod_edit = BoardPostModeratorEdit.from_dict(envelope)
            root_post_id = mod_edit.payload["root_post_id"]
            node.post_edits[root_post_id] = node.post_edits.get(root_post_id, ()) + (mod_edit,)
        elif row["object_type"] == BOARD_POST_TOMBSTONE_OBJECT_TYPE:
            tombstone = BoardPostTombstone.from_dict(envelope)
            root_post_id = tombstone.payload["root_post_id"]
            node.post_edits[root_post_id] = node.post_edits.get(root_post_id, ()) + (tombstone,)
        elif row["object_type"] == BOARD_ORIGIN_TRANSFER_OFFER_OBJECT_TYPE:
            offer = BoardOriginTransferOffer.from_dict(envelope)
            board_id = offer.payload["board_id"]
            node.pending_origin_transfers[board_id] = offer
            node.board_lifecycle_head[board_id] = offer.content_id
        elif row["object_type"] == BOARD_ORIGIN_TRANSFER_ACCEPTED_OBJECT_TYPE:
            peer_accepted = BoardOriginTransferAccepted.from_dict(envelope)
            board_id = peer_accepted.payload["board_id"]
            node.board_origin[board_id] = peer_accepted.payload["new_origin_fingerprint"]
            node.board_lifecycle_head[board_id] = peer_accepted.content_id
            node.pending_origin_transfers.pop(board_id, None)
        elif row["object_type"] == BOARD_CLOSURE_OBJECT_TYPE:
            # Design doc §9.5, issue #88: a bystander's own restart
            # reconstruction of someone else's board being closed.
            peer_closure = BoardClosure.from_dict(envelope)
            board_id = peer_closure.payload["board_id"]
            node.board_closures[board_id] = peer_closure
            node.board_lifecycle_head[board_id] = peer_closure.content_id

    for row in db.connection.execute(
        "SELECT link_genesis_json FROM boards WHERE link_genesis_json IS NOT NULL"
    ):
        genesis = BoardGenesis.from_dict(json.loads(row["link_genesis_json"]))
        node.boards[genesis.payload["board_id"]] = genesis

    # Design doc §9.6, issue #87: same two-source shape as boards above,
    # minus the board_post_edit-chain half -- channels have no edit
    # chain to reconstruct.
    for row in db.connection.execute(
        "SELECT link_genesis_json FROM channels WHERE link_genesis_json IS NOT NULL"
    ):
        channel_genesis = ChannelGenesis.from_dict(json.loads(row["link_genesis_json"]))
        node.channels[channel_genesis.payload["channel_id"]] = channel_genesis

    for row in db.connection.execute(
        "SELECT link_event_json FROM posts "
        "WHERE link_event_json IS NOT NULL AND post_id != root_post_id "
        # Issue #11's ordering-tie-break question, applied here: `created_at`
        # is the edit's own claimed payload timestamp, not a locally-assigned
        # monotonic value, so two edits made within the same timestamp
        # resolution would otherwise leave SQLite's tie order unspecified.
        # `id` (this table's own INTEGER PRIMARY KEY rowid) is assigned in
        # true insertion order regardless of any timestamp tie, matching the
        # peer-received loop above, which already orders by the locally-
        # assigned `received_at` for the identical reason -- neither loop may
        # trust the payload's own `created_at` alone to reconstruct chain
        # order, since nothing here re-verifies `previous_event_id` linkage
        # the way live `handle_events` acceptance does.
        "ORDER BY created_at ASC, id ASC"
    ):
        envelope = json.loads(row["link_event_json"])
        post_object_type = envelope["envelope"]["object_type"]
        if post_object_type == BOARD_POST_EDIT_OBJECT_TYPE:
            edit: BoardPostEdit | BoardPostModeratorEdit | BoardPostTombstone = BoardPostEdit.from_dict(envelope)
        elif post_object_type == BOARD_POST_MODERATOR_EDIT_OBJECT_TYPE:
            # Design doc §9.5, issue #88: same self-originated
            # reconstruction as BOARD_POST_EDIT_OBJECT_TYPE, since
            # queue_board_post_moderator_edit_if_linked only ever builds
            # one when this node is the board's own current origin.
            edit = BoardPostModeratorEdit.from_dict(envelope)
        elif post_object_type == BOARD_POST_TOMBSTONE_OBJECT_TYPE:
            edit = BoardPostTombstone.from_dict(envelope)
        else:
            continue
        root_post_id = edit.payload["root_post_id"]
        if edit.content_id in {e.content_id for e in node.post_edits.get(root_post_id, ())}:
            continue
        node.post_edits[root_post_id] = node.post_edits.get(root_post_id, ()) + (edit,)

    for row in db.connection.execute("SELECT fingerprint, descriptor_json FROM link_peer_candidates"):
        # Skip a fingerprint that's since become a real verified peer --
        # handle_hello's own in-memory candidate cleanup
        # could predate this row's own on-disk delete if a crash landed
        # between the two; never resurrect a stale candidate for
        # someone already directly known.
        if row["fingerprint"] in node.peers:
            continue
        node.candidate_descriptors[row["fingerprint"]] = EndpointDescriptor.from_dict(
            json.loads(row["descriptor_json"])
        )

    # Issue #58: both directions of a completed relay-consent
    # exchange (`netbbs.link.transport`'s `/relay-consent` route) --
    # `relaying_for` (this node granted, acting as the relay) and
    # `relays_serving_me` (a candidate granted this node's own request)
    # are otherwise only ever set in-memory at the moment consent
    # completes, the same restart-forgets-state gap already fixed
    # for peers/events. An outstanding, not-yet-answered `relay_consent_
    # request` this node itself sent is deliberately NOT reconstructed
    # here -- `pending_own_relay_requests` only ever holds something
    # between a request going out and its synchronous reply coming back
    # on the very same HTTP call (see `request_relay_consent`'s own
    # docstring); nothing is ever "still outstanding" across a restart
    # the way a gossiped mutual-consent offer can be.
    for row in db.connection.execute("SELECT fingerprint, role, accepted_at FROM link_relay_consents"):
        if row["role"] == "i_relay_for":
            node.relaying_for[row["fingerprint"]] = row["accepted_at"]
        else:
            node.relays_serving_me[row["fingerprint"]] = row["accepted_at"]

    return node


def load_peer_last_contact(db: Database) -> dict[str, str]:
    """
    fingerprint -> `link_peers.updated_at` (ISO 8601) for every peer
    this node has ever completed a hello or events exchange with --
    issue #60's SysOp Link-status screen needs "when did we last hear
    from this peer" for display, but `load_link_node` above deliberately
    doesn't reconstruct this column onto the in-memory `PeerRecord`
    (no protocol-shape change for a value nothing but display ever
    needs). Callers wanting it query separately, here.
    """
    return {
        row["fingerprint"]: row["updated_at"]
        for row in db.connection.execute("SELECT fingerprint, updated_at FROM link_peers")
    }


def save_peer(db: Database, peer: PeerRecord) -> None:
    """
    Upsert one peer's current record. Called after any successful
    `handle_hello`/`handle_events` unconditionally, not only when the
    caller can prove something changed -- matching this codebase's
    "harmless no-op" tolerance for a redundant write at this project's
    declared scale (§14), rather than this module owning the extra
    complexity of tracking what's already on disk.

    Also deletes any on-disk candidate row for the same fingerprint --
    mirrors `LinkNode.handle_hello`'s own in-memory
    cleanup automatically, so a caller that already calls `save_peer`
    after every successful hello (every caller does) doesn't need a
    second explicit cleanup call to keep the on-disk candidate table
    from resurrecting someone who's now a real peer after a restart.
    """
    db.connection.execute(
        """
        INSERT INTO link_peers (fingerprint, root_public_key, transitions_json, descriptor_json, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(fingerprint) DO UPDATE SET
            root_public_key = excluded.root_public_key,
            transitions_json = excluded.transitions_json,
            descriptor_json = excluded.descriptor_json,
            updated_at = excluded.updated_at
        """,
        (
            peer.fingerprint,
            base64.b64encode(peer.root_public_key).decode("ascii"),
            json.dumps([t.to_dict() for t in peer.transitions]),
            json.dumps(peer.descriptor.to_dict()),
            utc_now_iso(),
        ),
    )
    db.connection.execute("DELETE FROM link_peer_candidates WHERE fingerprint = ?", (peer.fingerprint,))
    db.connection.commit()


def save_candidate_descriptor(db: Database, fingerprint: str, descriptor: EndpointDescriptor) -> None:
    """
    Upsert one unverified candidate descriptor -- called for
    each fingerprint `LinkNode.handle_peer_list` newly recorded or
    refreshed. No "is this already a real peer" guard here -- `handle_
    peer_list` itself already refuses to record a candidate for an
    existing peer in the first place (see that method's own docstring),
    so this function has no reason to duplicate that check.
    """
    db.connection.execute(
        """
        INSERT INTO link_peer_candidates (fingerprint, descriptor_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(fingerprint) DO UPDATE SET
            descriptor_json = excluded.descriptor_json,
            updated_at = excluded.updated_at
        """,
        (fingerprint, json.dumps(descriptor.to_dict()), utc_now_iso()),
    )
    db.connection.commit()


_BOARD_SCOPED_OBJECT_TYPES = frozenset(
    {
        BOARD_GENESIS_OBJECT_TYPE,
        BOARD_ORIGIN_TRANSFER_OFFER_OBJECT_TYPE,
        BOARD_ORIGIN_TRANSFER_ACCEPTED_OBJECT_TYPE,
        BOARD_CLOSURE_OBJECT_TYPE,
    }
)


def save_event(db: Database, *, sender_fingerprint: str, content_id: str, object_type: str, envelope: dict) -> None:
    """
    Record one newly-accepted event. Called once per content_id
    `handle_events` returned as newly accepted -- an event already on
    disk (a resend `handle_events` itself never re-accepts, since its
    own in-memory `known_event_ids` check already skipped it) never
    reaches here, so `ON CONFLICT ... DO NOTHING` is a defensive
    no-op, not the primary dedup mechanism.

    Issue #85: `board_id` is populated directly from
    `envelope["envelope"]["payload"]` for the four board-scoped object
    types this function still handles
    (`board_genesis`, `board_origin_transfer_offer`, `_accepted`,
    `board_closure` -- the last added by issue #88) --
    `board_post`/`board_post_edit`/`board_post_moderator_edit`/`board_
    post_tombstone` never reach here at all
    (`netbbs.link.boards.materialize_carried_post`/`_edit`/`_moderator_
    edit`/`_tombstone` insert their own `link_events` row directly, in
    the same transaction as their `posts` projection, and populate
    `board_id` themselves the same way). `None` for every other object
    type (`key_transition`, `link_message` and its acknowledgements),
    which don't belong to a board.

    Issue #87: `channel_id` is populated the same way for `channel_
    genesis`, the one channel-scoped type this function still handles --
    `channel_message` never reaches here either, for the identical
    reason (`netbbs.link.channels.materialize_carried_channel_message`
    inserts its own row directly).
    """
    # Conditional, not unconditional -- a malformed/tampered envelope for
    # an object type that needs neither field (e.g. a resend attempt
    # with a deliberately corrupted payload, see the ON CONFLICT test for
    # this function) must never raise just from this lookup, since
    # `envelope["envelope"]["payload"]` isn't guaranteed to exist at all
    # once the row already conflicts and this insert is a no-op anyway.
    board_id = envelope["envelope"]["payload"].get("board_id") if object_type in _BOARD_SCOPED_OBJECT_TYPES else None
    channel_id = (
        envelope["envelope"]["payload"].get("channel_id") if object_type == CHANNEL_GENESIS_OBJECT_TYPE else None
    )
    now = utc_now_iso()
    db.connection.execute(
        """
        INSERT INTO link_events
            (content_id, sender_fingerprint, object_type, envelope_json, received_at, board_id, channel_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(content_id) DO NOTHING
        """,
        (content_id, sender_fingerprint, object_type, json.dumps(envelope), now, board_id, channel_id),
    )
    db.connection.commit()
    if object_type == KEY_TRANSITION_OBJECT_TYPE:
        # Issue #86: purge on write, scoped to the same object type this
        # write just touched -- the same shape LinkDiagnosticLogHandler.
        # emit already established for link_diagnostic_log. Only
        # key_transition rows are purged here; see this module's own
        # docstring and design doc §8.9 for why every other object type
        # this table stores must stay unbounded.
        purge_expired_key_transitions(db, now_iso=now)


_KEY_TRANSITION_RETENTION_DAYS = 90


def purge_expired_key_transitions(db: Database, *, now_iso: str | None = None) -> int:
    """
    Delete `link_events` rows older than `_KEY_TRANSITION_RETENTION_DAYS`
    whose `object_type` is `key_transition` -- design doc §8.9, issue #86.

    Provably safe, unlike every other object type this table stores:
    `netbbs.link.store.load_link_node` never reconstructs `sender.
    transitions` from a `key_transition`'s own `link_events` row at all --
    `link_peers.transitions_json` (updated by `save_peer` whenever
    `handle_events` accepts a new transition) is the actual authoritative
    source, and `handle_events`'s own self-heal branch for a resent,
    already-integrated transition checks `sender.transitions` directly,
    never `known_event_ids`. The `link_events` row exists only to make a
    resend a fast no-op via the dedup-cache check one line above the
    self-heal branch; losing it just means a subsequent resend takes the
    (still perfectly safe) self-heal path instead of the (also safe, one
    check earlier) dedup-cache path.

    Returns the number of rows deleted -- purely informational, no caller
    currently needs it beyond tests.
    """
    now = now_iso if now_iso is not None else utc_now_iso()
    cutoff = _days_before(now, _KEY_TRANSITION_RETENTION_DAYS)
    cursor = db.connection.execute(
        "DELETE FROM link_events WHERE object_type = ? AND received_at < ?",
        (KEY_TRANSITION_OBJECT_TYPE, cutoff),
    )
    db.connection.commit()
    return cursor.rowcount


def _days_before(now_iso: str, days: int) -> str:
    # Same "ISO-8601 timestamps sort lexically" reasoning as every other
    # created_at/received_at column in this codebase -- no separate
    # date-parsing needed to compare against "now minus N days" as a
    # plain string. Duplicated from netbbs.link.diagnostics's own
    # private helper of the same name rather than imported -- this
    # codebase's established per-module convention for a helper this
    # small (see e.g. tests/test_link_sync.py and tests/test_link_
    # transport.py independently duplicating their own small fixtures).
    parsed = datetime.fromisoformat(now_iso)
    return (parsed - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def save_relay_consent(db: Database, fingerprint: str, *, role: str, accepted_at: str) -> None:
    """
    Persist one completed relay-consent grant (design doc §12,
    issue #58) -- `role` is `"i_relay_for"` (this node, as the relay,
    granted `fingerprint`'s request) or `"relay_for_me"` (`fingerprint`,
    as a candidate relay, granted this node's own request), matching
    `link_relay_consents`' own `CHECK` constraint exactly. Called after
    `netbbs.link.transport`'s `/relay-consent` route handler (relay
    side) or `request_relay_consent` (requester side) applies its own
    in-memory `relaying_for`/`relays_serving_me` update -- same
    "verify/mutate in-memory first, persist after" order every other
    accepted-event write in this module already follows.

    A decline is never persisted -- there's nothing durable about it (a
    future pass may simply ask again), matching this module's existing
    "only ever persist an accepted/completed fact" scope.
    """
    db.connection.execute(
        """
        INSERT INTO link_relay_consents (fingerprint, role, accepted_at)
        VALUES (?, ?, ?)
        ON CONFLICT(fingerprint, role) DO UPDATE SET
            accepted_at = excluded.accepted_at
        """,
        (fingerprint, role, accepted_at),
    )
    db.connection.commit()


def delete_relay_consent(db: Database, fingerprint: str, *, role: str) -> None:
    """
    Remove one previously-granted relay-consent row (design doc §12,
    issue #58) -- called when self-healing drops a relay whose
    observed reliability has fallen below `netbbs.link.relay_selection`'s
    floor (`role="relay_for_me"`), so a restarted node doesn't resurrect
    a relay it already gave up on via `load_link_node`'s own
    reconstruction. Harmless no-op if no such row exists, same tolerance
    every other delete in this module already has.
    """
    db.connection.execute(
        "DELETE FROM link_relay_consents WHERE fingerprint = ? AND role = ?", (fingerprint, role)
    )
    db.connection.commit()


def carried_board_ids(db: Database) -> list[str]:
    """
    Every `board_id` this node currently has *some* Linked copy of
    (design doc §8.8, issue #85) -- self-originated or merely carried,
    the same `boards.link_genesis_json IS NOT NULL` test `netbbs.link.
    boards.load_own_board_events` already uses, minus its own
    `origin_fingerprint` filter (that filter exists there specifically
    to separate "mine to re-push as my own" from "carried"; inventory
    scope wants both, since a carrying node offering an inventory
    request for a board it merely carries is exactly the multi-hop case
    this issue closes)."""
    return [
        row["board_id"]
        for row in db.connection.execute("SELECT board_id FROM boards WHERE link_genesis_json IS NOT NULL")
    ]


def carried_channel_ids(db: Database) -> list[str]:
    """Every `channel_id` this node currently has *some* Linked copy of
    -- mirrors `carried_board_ids` exactly (design doc §9.6, issue #87)."""
    return [
        row["channel_id"]
        for row in db.connection.execute("SELECT channel_id FROM channels WHERE link_genesis_json IS NOT NULL")
    ]


def build_inventory_request(db: Database) -> InventoryRequest:
    """
    This node's own `InventoryRequest` to send as requester (design doc
    §8.8, issue #85; §9.6, issue #87): every board/channel it currently
    carries, each mapped to the full set of content IDs it already has
    for it -- reusing `_all_board_events`/`_all_channel_events` for
    exactly the same "union self-authored, carried, and peer-received
    sources" reasoning those functions' own docstrings already give,
    since a requester's own gap-detection needs the identical complete
    picture a responder's diff needs, just read from the requester's own
    database instead of the responder's."""
    boards = {board_id: tuple(_all_board_events(db, board_id)) for board_id in carried_board_ids(db)}
    channels = {channel_id: tuple(_all_channel_events(db, channel_id)) for channel_id in carried_channel_ids(db)}
    return InventoryRequest(boards=boards, channels=channels)


def _all_board_events(db: Database, board_id: str) -> dict[str, dict]:
    """
    Every board-scoped event this node has on file for `board_id`,
    keyed by `content_id` (so a genesis that happens to appear in two
    sources below, see the carried-board case, collapses naturally) --
    the full picture `board_event_diff` diffs against, unioning three
    differently-shaped sources exactly the way `netbbs.link.boards.
    load_own_board_events` already does for the analogous "everything
    self-originated" case, minus that function's own-fingerprint filter
    on genesis (inventory scope wants both self-originated and merely
    carried boards, see `carried_board_ids`'s own docstring for why):

    1. `boards.link_genesis_json`/`link_lifecycle_json` for this board's
       own row -- covers a self-originated board's genesis (never
       received through `handle_events`, so never in `link_events`
       either) and this node's own lifecycle actions
       (`offer_board_origin_transfer`/`accept_board_origin_transfer`).
       Included unconditionally, unlike `load_own_board_events`'s own
       origin-filtered genesis half -- a carried board's genesis is
       *also* stored here (`materialize_carried_board` populates it),
       redundantly with `link_events` below; the `content_id`-keyed dict
       here is exactly what makes that redundancy harmless.
    2. `posts.link_event_json` for every post/edit on this board --
       `netbbs.link.boards.queue_board_post_if_linked`/`_edit_if_linked`
       populate this column *only* for a locally-authored post/edit,
       regardless of whether this node originated or merely carries the
       board it's on (a local user can reply on a carried board too) --
       never populated for a materialized/carried post, which has no
       equivalent local-authorship event of its own to queue.
    3. `link_events.board_id = ?` -- peer-received content: a carried
       board's genesis (redundant with source 1 above) and every
       `board_post`/`board_post_edit`/lifecycle event this node accepted
       from a peer, whether it originated the board or not.
    """
    events: dict[str, dict] = {}

    board_row = db.connection.execute(
        "SELECT id, link_genesis_json, link_lifecycle_json FROM boards WHERE board_id = ?", (board_id,)
    ).fetchone()
    if board_row is None:
        return events
    if board_row["link_genesis_json"] is not None:
        raw = json.loads(board_row["link_genesis_json"])
        events[BoardGenesis.from_dict(raw).content_id] = raw
    if board_row["link_lifecycle_json"] is not None:
        # Design doc §9.5, issue #88: `link_lifecycle_json` can now also
        # be a self-originated `board_closure` -- `event_content_id`
        # works directly off the envelope regardless of which of the
        # three lifecycle object types this actually is, so there's no
        # need to reconstruct the specific dataclass just to key this
        # dict by its content_id.
        raw = json.loads(board_row["link_lifecycle_json"])
        events[event_content_id(raw["envelope"])] = raw

    for row in db.connection.execute(
        "SELECT link_event_json FROM posts WHERE board_id = ? AND link_event_json IS NOT NULL", (board_row["id"],)
    ):
        # Design doc §9.5, issue #88: same reasoning -- a self-originated
        # board_post/board_post_edit/board_post_moderator_edit/board_
        # post_tombstone all key by their own envelope's content_id alike.
        raw = json.loads(row["link_event_json"])
        events[event_content_id(raw["envelope"])] = raw

    for row in db.connection.execute(
        "SELECT content_id, envelope_json FROM link_events WHERE board_id = ? ORDER BY received_at ASC", (board_id,)
    ):
        events.setdefault(row["content_id"], json.loads(row["envelope_json"]))

    return events


def board_event_diff(
    db: Database, requested_boards: dict[str, list[str]], *, limit: int
) -> tuple[list[dict], bool]:
    """
    The responder side of one `InventoryRequest` (design doc §8.8, issue
    #85): for each `board_id` in `requested_boards` that this node also
    currently carries (a `board_id` this node doesn't carry is silently
    skipped, never an error -- §9.3's existing "not carried on this
    node" honest-exclusion principle, applied here to a request rather
    than a push), return every board-scoped event on file for it
    (`_all_board_events`, above) whose `content_id` is not already in
    that board's declared known-ID list -- this is the actual multi-hop
    mechanism: a node that only ever *carries* board X, never originated
    it, can still answer for it here, because `_all_board_events`
    doesn't care who authored what it has on file, only what it has.

    Bounded by `limit` (the caller's own `_MAX_EVENTS_PER_REQUEST`,
    §13.9) across the *whole* response, not per board -- boards are
    walked in sorted `board_id` order for determinism (this function's
    own caller has no meaningful priority between them). Returns the
    raw envelope dicts (exactly the wire shape `push_events` already
    sends, so the caller can feed the combined list through `LinkNode.
    handle_events` with no translation) plus whether more remain beyond
    `limit` -- the caller's own next pass will ask again with a
    by-then-larger known-ID list, so nothing here needs to track a
    pagination cursor.
    """
    collected: list[dict] = []
    truncated = False
    for board_id in sorted(requested_boards):
        if truncated:
            break
        known_ids = set(requested_boards[board_id])
        for content_id, envelope in _all_board_events(db, board_id).items():
            if content_id in known_ids:
                continue
            if len(collected) >= limit:
                truncated = True
                break
            collected.append(envelope)
    return collected, truncated


def _all_channel_events(db: Database, channel_id: str) -> dict[str, dict]:
    """
    Every channel-scoped event this node has on file for `channel_id`,
    keyed by `content_id` -- mirrors `_all_board_events` exactly, minus
    that function's own lifecycle-event source (no origin succession for
    channels yet, design doc §9.6) and its board_post_edit branch (no
    edit chain for channel messages):

    1. `channels.link_genesis_json` for this channel's own row -- a
       self-originated channel's genesis (never in `link_events`) or a
       carried channel's genesis (redundant with `link_events` below,
       harmless -- same `content_id`-keyed-dict reasoning).
    2. `channel_messages.link_event_json` for every message on this
       channel -- populated only for a locally-authored message
       (`netbbs.link.channels.queue_channel_message_if_linked`),
       regardless of whether this node originated or merely carries the
       channel.
    3. `link_events.channel_id = ?` -- peer-received content: a carried
       channel's genesis (redundant with source 1) and every
       `channel_message` this node accepted from a peer.
    """
    events: dict[str, dict] = {}

    channel_row = db.connection.execute(
        "SELECT id, link_genesis_json FROM channels WHERE channel_id = ?", (channel_id,)
    ).fetchone()
    if channel_row is None:
        return events
    if channel_row["link_genesis_json"] is not None:
        raw = json.loads(channel_row["link_genesis_json"])
        events[ChannelGenesis.from_dict(raw).content_id] = raw

    for row in db.connection.execute(
        "SELECT link_event_json FROM channel_messages WHERE channel_id = ? AND link_event_json IS NOT NULL",
        (channel_row["id"],),
    ):
        raw = json.loads(row["link_event_json"])
        events[ChannelMessage.from_dict(raw).content_id] = raw

    for row in db.connection.execute(
        "SELECT content_id, envelope_json FROM link_events WHERE channel_id = ? ORDER BY received_at ASC",
        (channel_id,),
    ):
        events.setdefault(row["content_id"], json.loads(row["envelope_json"]))

    return events


def channel_event_diff(
    db: Database, requested_channels: dict[str, list[str]], *, limit: int
) -> tuple[list[dict], bool]:
    """
    The channel-side responder logic for one `InventoryRequest` --
    mirrors `board_event_diff` exactly, using `_all_channel_events`
    instead. Callers combining both board and channel results (`netbbs.
    link.transport._handle_inventory`) are responsible for sharing one
    overall `_MAX_EVENTS_PER_REQUEST` budget across both calls -- this
    function's own `limit` is whatever budget remains after the board
    half already ran, not a second independent cap.
    """
    collected: list[dict] = []
    truncated = False
    for channel_id in sorted(requested_channels):
        if truncated:
            break
        known_ids = set(requested_channels[channel_id])
        for content_id, envelope in _all_channel_events(db, channel_id).items():
            if content_id in known_ids:
                continue
            if len(collected) >= limit:
                truncated = True
                break
            collected.append(envelope)
    return collected, truncated
