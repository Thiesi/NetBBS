"""
Persistent storage for `netbbs.link.protocol.LinkNode`'s peer table and
seen-event/event-body store (design doc round 120) -- backs the
`link_peers`/`link_events` tables (`netbbs.storage.migrations`) with
plain `db`-first sync functions, matching every other lane-dispatched
function's existing convention (`netbbs.storage.execution.DatabaseLane.
run` injects `db` as the first positional argument).

Deliberately outside `netbbs.link.protocol` itself -- `LinkNode` stays
pure, synchronous, in-memory; see round 120's sign-off note for why
(`tests/link_harness.py`'s `ScriptedTransport` calls `handle_hello`/
`handle_events` directly with zero I/O, a property this module doesn't
disturb). Callers -- `netbbs.link.transport`'s `LinkServer` and
`dial_hello`, the only places that already own I/O at this layer --
call these functions themselves, via `DatabaseLane.run`, after a
successful `handle_hello`/`handle_events`.

No retention-window purging here (round 120: `link_events` rows
accumulate indefinitely) -- see that round's sign-off note for the
real chain-idempotency gap in `netbbs.link.protocol.handle_events`
that has to close first before purging a `key_transition` dedup entry
would be safe.
"""

from __future__ import annotations

import base64
import json

from netbbs.link.events import (
    BOARD_GENESIS_OBJECT_TYPE,
    BOARD_ORIGIN_TRANSFER_ACCEPTED_OBJECT_TYPE,
    BOARD_ORIGIN_TRANSFER_OFFER_OBJECT_TYPE,
    BOARD_POST_EDIT_OBJECT_TYPE,
    BoardGenesis,
    BoardOriginTransferAccepted,
    BoardOriginTransferOffer,
    BoardPostEdit,
    EndpointDescriptor,
    KeyTransition,
)
from netbbs.link.node_identity import NodeIdentity
from netbbs.link.protocol import LinkNode, PeerRecord
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


def load_link_node(db: Database, identity: NodeIdentity) -> LinkNode:
    """
    Reconstruct a `LinkNode` from persisted `link_peers`/`link_events`
    rows -- the round-120 replacement for a bare `LinkNode(identity=
    ...)` construction, so a restarted node doesn't forget its peers or
    reprocess/re-forward events it has already seen.

    Round 126 (found by tracing, not hypothetical): `LinkServer._handle_
    events` already persists *any* accepted event generically, board_
    genesis/board_post included, with no type-specific code of its own
    -- so `link_events` already held board_genesis rows before this
    function knew to do anything with them. Without also rebuilding
    `node.boards` here, a restarted node would forget every board_
    genesis it had already verified, and `handle_events` would then
    wrongly reject a legitimate resent board_post as having "no
    verified board_genesis on file" -- exactly the restart-forgets-
    state shape round 120 already fixed for peers/events, just not yet
    extended to this newer derived index.

    Round 128: a board this node itself originated (`netbbs.link.
    boards.link_board`) never goes through `handle_events` at all --
    there's no peer to verify it against, it's self-signed -- so its
    genesis lives only on the local `boards` row's own `link_genesis_
    json` column, a completely different source from `link_events`
    above. Both are reconstructed into the same `node.boards` index
    here; without this half too, a restarted node would forget its
    *own* Linked boards and wrongly reject a remote user's legitimate
    `board_post` on a board it itself originated.

    Round 130: the same two-source shape, applied to `node.post_edits`
    -- peer-received `board_post_edit` rows come from `link_events`
    (queried in `received_at` order, since a chain reconstructed out of
    order would fail its own "does previous_event_id match the current
    head" check the moment it's used); self-originated ones
    (`netbbs.link.boards.queue_board_post_edit_if_linked`) live on the
    edited revision's own `posts.link_event_json` column instead, same
    as a self-originated `board_genesis` lives on `boards.link_genesis_
    json` rather than ever passing through `handle_events`.

    Round 94/issue #53: the same two-source shape again, applied to
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

    # Round 94/issue #53: this node's own self-originated lifecycle
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
        if raw["envelope"]["object_type"] == BOARD_ORIGIN_TRANSFER_OFFER_OBJECT_TYPE:
            offer = BoardOriginTransferOffer.from_dict(raw)
            board_id = offer.payload["board_id"]
            node.pending_origin_transfers[board_id] = offer
            node.board_lifecycle_head[board_id] = offer.content_id
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
        elif row["object_type"] == BOARD_POST_EDIT_OBJECT_TYPE:
            edit = BoardPostEdit.from_dict(envelope)
            root_post_id = edit.payload["root_post_id"]
            node.post_edits[root_post_id] = node.post_edits.get(root_post_id, ()) + (edit,)
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

    for row in db.connection.execute(
        "SELECT link_genesis_json FROM boards WHERE link_genesis_json IS NOT NULL"
    ):
        genesis = BoardGenesis.from_dict(json.loads(row["link_genesis_json"]))
        node.boards[genesis.payload["board_id"]] = genesis

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
        if envelope["envelope"]["object_type"] != BOARD_POST_EDIT_OBJECT_TYPE:
            continue
        edit = BoardPostEdit.from_dict(envelope)
        root_post_id = edit.payload["root_post_id"]
        if edit.content_id in {e.content_id for e in node.post_edits.get(root_post_id, ())}:
            continue
        node.post_edits[root_post_id] = node.post_edits.get(root_post_id, ()) + (edit,)

    for row in db.connection.execute("SELECT fingerprint, descriptor_json FROM link_peer_candidates"):
        # Skip a fingerprint that's since become a real verified peer --
        # handle_hello's own in-memory candidate cleanup (round 95)
        # could predate this row's own on-disk delete if a crash landed
        # between the two; never resurrect a stale candidate for
        # someone already directly known.
        if row["fingerprint"] in node.peers:
            continue
        node.candidate_descriptors[row["fingerprint"]] = EndpointDescriptor.from_dict(
            json.loads(row["descriptor_json"])
        )

    # Round 95/issue #58: both directions of a completed relay-consent
    # exchange (`netbbs.link.transport`'s `/relay-consent` route) --
    # `relaying_for` (this node granted, acting as the relay) and
    # `relays_serving_me` (a candidate granted this node's own request)
    # are otherwise only ever set in-memory at the moment consent
    # completes, same restart-forgets-state gap round 120 already fixed
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


def save_peer(db: Database, peer: PeerRecord) -> None:
    """
    Upsert one peer's current record. Called after any successful
    `handle_hello`/`handle_events` unconditionally, not only when the
    caller can prove something changed -- matching round 119's own
    "harmless no-op" tolerance for a redundant write at this project's
    declared scale (§14), rather than this module owning the extra
    complexity of tracking what's already on disk.

    Also deletes any on-disk candidate row for the same fingerprint
    (round 95) -- mirrors `LinkNode.handle_hello`'s own in-memory
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
    Upsert one unverified candidate descriptor (round 95) -- called for
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


def save_event(db: Database, *, sender_fingerprint: str, content_id: str, object_type: str, envelope: dict) -> None:
    """
    Record one newly-accepted event. Called once per content_id
    `handle_events` returned as newly accepted -- an event already on
    disk (a resend `handle_events` itself never re-accepts, since its
    own in-memory `known_event_ids` check already skipped it) never
    reaches here, so `ON CONFLICT ... DO NOTHING` is a defensive
    no-op, not the primary dedup mechanism.
    """
    db.connection.execute(
        """
        INSERT INTO link_events (content_id, sender_fingerprint, object_type, envelope_json, received_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(content_id) DO NOTHING
        """,
        (content_id, sender_fingerprint, object_type, json.dumps(envelope), utc_now_iso()),
    )
    db.connection.commit()


def save_relay_consent(db: Database, fingerprint: str, *, role: str, accepted_at: str) -> None:
    """
    Persist one completed relay-consent grant (design doc §12, round
    95/issue #58) -- `role` is `"i_relay_for"` (this node, as the relay,
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
    round 95/issue #58) -- called when self-healing drops a relay whose
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
