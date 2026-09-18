"""Carrying the trust objects of a node nobody can dial (issue #627, design doc §12.7).

Signed trust objects are distributed by pull: a subscriber dials the issuer and
asks for a page. An outgoing-only node advertises no address, so nothing it
issued could be fetched by anyone, and most real nodes are outgoing-only. Such
a node therefore deposits its own signed objects at the nodes that already
relay for it (§8.5), and a subscriber uses the carrier form of the trust pull
the wire has always defined, in which the responder is not the issuer.

What is deposited is kept here, in `link_trust_carried_objects`, and never in
`link_trust_wire_objects`. That table is what this node has *admitted*:
`ingest_trust_objects` is admission control and application at once, and an
object already present there counts as replayed and is never applied. A
deposit stored there would make a vouch this node merely carries
indistinguishable from one it acts on, and would silently swallow it if the
SysOp later named its issuer a trusted reporter. Carriage grants nothing. The
objects are issuer-signed, so a carrier can withhold one and cannot forge one,
the same position a carrier of a hello bundle is in (§8.11).
"""

from __future__ import annotations

import base64
import json
from typing import Any

from netbbs.link.trust_wire import (
    MAX_TRUST_OBJECTS_PER_RESPONSE,
    MAX_TRUST_RESPONSE_BYTES,
    SignedTrustObject,
    TrustWireError,
    UnknownTrustPullCursor,
    load_trust_object_page,
)
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

# What one depositor may have carried here at once. An issuer's own active
# quota is 1,000 signals and 1,000 vouches (`trust_wire`), each of which may
# later gain a revocation, so twice their sum is everything a well-behaved
# issuer can have outstanding. Remotely influenced, so bounded: past either
# limit a deposit is refused and says so, which the depositor logs.
MAX_CARRIED_TRUST_OBJECTS_PER_ISSUER = 4000
MAX_CARRIED_TRUST_BYTES_PER_ISSUER = 32 * 1024 * 1024


class TrustCarriageFull(TrustWireError):
    """A depositor has reached what this node will carry for it."""


def store_deposited_trust_objects(
    db: Database, issuer_fingerprint: str, objects: list[SignedTrustObject], *, now_iso: str | None = None
) -> tuple[list[str], list[str]]:
    """Keep `objects`, already verified as `issuer_fingerprint`'s own, for carriage.

    Returns `(stored, already_held)` content IDs. Idempotent by content ID, so
    a depositor that lost its cursor simply deposits again. Objects whose own
    expiry has passed are dropped first, the depositor's and everybody else's:
    nothing else ever removes a row here. All or nothing past a limit, so that
    a revocation is never stored without the object before it in the same
    deposit.
    """
    now = now_iso or utc_now_iso()
    stored: list[str] = []
    held: list[str] = []
    with db.connection:
        db.connection.execute(
            "DELETE FROM link_trust_carried_objects WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,)
        )
        count, size = db.connection.execute(
            """SELECT COUNT(*), COALESCE(SUM(LENGTH(envelope_json)), 0)
               FROM link_trust_carried_objects WHERE issuer_fingerprint = ?""",
            (issuer_fingerprint,),
        ).fetchone()
        for obj in objects:
            if obj.issuer_fingerprint != issuer_fingerprint:
                raise TrustWireError("a node may deposit only the trust objects it issued itself")
            if db.connection.execute(
                "SELECT 1 FROM link_trust_carried_objects WHERE content_id = ?", (obj.content_id,)
            ).fetchone() is not None:
                held.append(obj.content_id)
                continue
            envelope_json = json.dumps(obj.envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            count += 1
            size += len(envelope_json)
            if count > MAX_CARRIED_TRUST_OBJECTS_PER_ISSUER or size > MAX_CARRIED_TRUST_BYTES_PER_ISSUER:
                raise TrustCarriageFull(
                    f"this node already carries all it will for {issuer_fingerprint}"
                )
            db.connection.execute(
                """INSERT INTO link_trust_carried_objects
                   (content_id, issuer_fingerprint, object_type, envelope_json, signature_b64,
                    expires_at, received_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    obj.content_id, issuer_fingerprint, str(obj.object_type), envelope_json,
                    base64.b64encode(obj.signature).decode("ascii"),
                    # An object this release does not understand is carried all
                    # the same (see `LinkNode.handle_trust_deposit`), so its
                    # payload is read defensively.
                    expires_at if isinstance(expires_at := obj.payload.get("expires_at"), str) else None,
                    now,
                ),
            )
            stored.append(obj.content_id)
    return stored, held


def carries_trust_objects_for(db: Database, issuer_fingerprint: str) -> bool:
    return db.connection.execute(
        "SELECT 1 FROM link_trust_carried_objects WHERE issuer_fingerprint = ? LIMIT 1",
        (issuer_fingerprint,),
    ).fetchone() is not None


def load_trust_page_for_pull(
    db: Database, *, own_fingerprint: str, issuer_fingerprint: str,
    after_content_id: str | None = None, limit: int = MAX_TRUST_OBJECTS_PER_RESPONSE,
    revocations_only: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    """One page of `issuer_fingerprint`'s objects, from whichever store holds its stream.

    An issuer's stream is served from exactly one table, because a cursor is a
    position in one. This node's own objects, and those of an issuer nothing
    was ever deposited for, come from the admitted store as before. An issuer
    that deposits here is served from what it deposited: that is the issuer's
    own complete stream in the issuer's own order, where the admitted store
    holds only what this node's reporter grant let through. A subscriber
    whose cursor belongs to the other table is told it is unknown and starts
    over, which the pull already recovers from (issue #621).
    """
    if issuer_fingerprint == own_fingerprint or not carries_trust_objects_for(db, issuer_fingerprint):
        return load_trust_object_page(
            db, issuer_fingerprint=issuer_fingerprint, after_content_id=after_content_id,
            limit=limit, revocations_only=revocations_only,
        )
    limit = max(1, min(limit, MAX_TRUST_OBJECTS_PER_RESPONSE))
    after_rowid = 0
    if after_content_id:
        row = db.connection.execute(
            "SELECT rowid FROM link_trust_carried_objects WHERE content_id = ? AND issuer_fingerprint = ?",
            (after_content_id, issuer_fingerprint),
        ).fetchone()
        if row is None:
            raise UnknownTrustPullCursor("unknown trust pull cursor")
        after_rowid = row[0]
    type_filter = (
        "AND object_type IN ('trust_revocation', 'trust_vouch_revocation')" if revocations_only else ""
    )
    rows = db.connection.execute(
        f"""SELECT envelope_json, signature_b64 FROM link_trust_carried_objects
            WHERE issuer_fingerprint = ? {type_filter} AND rowid > ?
            ORDER BY rowid LIMIT ?""",
        (issuer_fingerprint, after_rowid, limit + 1),
    ).fetchall()
    more = len(rows) > limit
    result: list[dict[str, Any]] = []
    total = 2
    for envelope_json, signature_b64 in rows[:limit]:
        item = {"envelope": json.loads(envelope_json), "signature": signature_b64}
        item_size = len(json.dumps(item, separators=(",", ":")).encode("utf-8")) + 1
        if result and total + item_size > MAX_TRUST_RESPONSE_BYTES:
            more = True
            break
        result.append(item)
        total += item_size
    return result, more


# -- the depositor's side ---------------------------------------------------------------------


def load_own_trust_objects_to_deposit(
    db: Database, *, own_fingerprint: str, relay_fingerprint: str,
    limit: int = MAX_TRUST_OBJECTS_PER_RESPONSE,
) -> tuple[list[dict[str, Any]], int | None]:
    """This node's own signed objects not yet deposited at `relay_fingerprint`, oldest first.

    Returns the wire objects and the position to record once the relay has
    taken them, or `([], None)` when there is nothing new. By `rowid`, like the
    page this node serves: a revocation is always inserted after the object it
    retires, whatever the clock says, and must arrive after it too. Bounded in
    bytes as a response is, since the relay's request limit is no larger.
    """
    row = db.connection.execute(
        "SELECT last_rowid FROM link_trust_deposit_cursors WHERE relay_fingerprint = ?",
        (relay_fingerprint,),
    ).fetchone()
    after_rowid = row[0] if row is not None else 0
    rows = db.connection.execute(
        """SELECT rowid, envelope_json, signature_b64 FROM link_trust_wire_objects
           WHERE issuer_fingerprint = ? AND rowid > ? ORDER BY rowid LIMIT ?""",
        (own_fingerprint, after_rowid, max(1, min(limit, MAX_TRUST_OBJECTS_PER_RESPONSE))),
    ).fetchall()
    objects: list[dict[str, Any]] = []
    position: int | None = None
    total = 2
    for rowid, envelope_json, signature_b64 in rows:
        item = {"envelope": json.loads(envelope_json), "signature": signature_b64}
        item_size = len(json.dumps(item, separators=(",", ":")).encode("utf-8")) + 1
        if objects and total + item_size > MAX_TRUST_RESPONSE_BYTES:
            break
        objects.append(item)
        total += item_size
        position = rowid
    return objects, position


def save_trust_deposit_position(db: Database, relay_fingerprint: str, position: int) -> None:
    with db.connection:
        db.connection.execute(
            """INSERT INTO link_trust_deposit_cursors (relay_fingerprint, last_rowid, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(relay_fingerprint) DO UPDATE SET
                   last_rowid = excluded.last_rowid, updated_at = excluded.updated_at""",
            (relay_fingerprint, position, utc_now_iso()),
        )


def clear_trust_deposit_position(db: Database, relay_fingerprint: str) -> None:
    """Start over at a relay: it stopped serving this node and may have dropped what it held."""
    with db.connection:
        db.connection.execute(
            "DELETE FROM link_trust_deposit_cursors WHERE relay_fingerprint = ?", (relay_fingerprint,)
        )
