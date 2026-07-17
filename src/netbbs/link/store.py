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

from netbbs.link.events import EndpointDescriptor, KeyTransition
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

    for row in db.connection.execute("SELECT content_id, envelope_json FROM link_events"):
        node.known_event_ids.add(row["content_id"])
        node.events[row["content_id"]] = json.loads(row["envelope_json"])

    return node


def save_peer(db: Database, peer: PeerRecord) -> None:
    """
    Upsert one peer's current record. Called after any successful
    `handle_hello`/`handle_events` unconditionally, not only when the
    caller can prove something changed -- matching round 119's own
    "harmless no-op" tolerance for a redundant write at this project's
    declared scale (§14), rather than this module owning the extra
    complexity of tracking what's already on disk.
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
