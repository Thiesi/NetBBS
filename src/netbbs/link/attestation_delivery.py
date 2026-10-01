"""
Delivering remote identity attestations as sealed snapshots (design doc §16,
issue #632).

Both halves of `netbbs.link.attestation_bundles`' wire format in use:

- **Issuer.** Each sync pass, for every recipient whose snapshot changed or
  was last sent a week ago -- and for every recipient removed within 90 days,
  which is owed one final, empty snapshot -- build the snapshot, seal it to the
  recipient and hand it over directly or at one of the recipient's relays. A
  per-recipient ledger records what was sent, by which route, and why the last
  attempt failed, for the Published identity screen.
- **Recipient.** A bundle, however it arrived, is checked (addressed here,
  signed by an issuer this node subscribes to, newer than the last one from
  it), opened, and applied as a whole: every object is ingested as a pulled
  one would be, and whatever this node holds from that issuer which the
  snapshot leaves out is forgotten, "withdrawn for this recipient".

The database functions here are plain, synchronous and `db`-first, dispatched
through `DatabaseLane.run` like the rest of `netbbs.link`; the two async
helpers at the end take the node and the lane, and are shared by the sync loop
and the transport's direct-delivery route.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import nacl.signing

from netbbs.boards.content_id import ContentIdError
from netbbs.identity.encryption import EncryptionError
from netbbs.link.attestation_bundles import (
    MalformedBundle,
    SealedAttestationBundle,
    open_sealed_attestation_bundle,
    snapshot_digest,
)
from netbbs.link.events import canonical_bytes
from netbbs.link.remote_attestation import (
    REMOTE_ATTESTATION_OBJECT_TYPE,
    REMOTE_ATTESTATION_REVOCATION_OBJECT_TYPE,
    UnknownAttestationSubject,
    _audit,
    _forget_received_values,
    _recompute_subject_id,
    _verify_wire,
    ingest_remote_attestation,
    list_attestation_authority_fingerprints,
)
from netbbs.storage.database import Database
from netbbs.timeutil import utc_iso

if TYPE_CHECKING:
    from netbbs.link.protocol import LinkNode
    from netbbs.storage.execution import DatabaseLane

_logger = logging.getLogger("netbbs.link.sync")

#: A recipient's snapshot is re-sent at least this often while it is unchanged,
#: so one a relay lost, or a recipient lost to a crash after pickup, comes back.
BUNDLE_REFRESH_INTERVAL = timedelta(days=7)

#: How long a removed recipient is owed its final, empty snapshot. Matches the
#: issued attestations' own 90-day lifetime: past it, everything it could
#: still hold from this node has expired anyway.
REMOVED_RECIPIENT_RETRY_WINDOW = timedelta(days=90)

#: After a failed attempt, how long before that recipient is tried again.
#: A recipient with no route would otherwise cost a seal and a dial every pass.
FAILED_DELIVERY_BACKOFF = timedelta(hours=1)

#: Recipients handed a bundle per sync pass, oldest attempt first.
MAX_DELIVERIES_PER_PASS = 20

#: Objects waiting for a subject this node has not met yet, per issuer.
MAX_PENDING_OBJECTS_PER_ISSUER = 1000

WITHDRAWN_FOR_RECIPIENT = "withdrawn_for_recipient"


def _now(now: datetime | None) -> tuple[str, datetime]:
    moment = now or datetime.now(timezone.utc)
    return utc_iso(moment), moment


def _object_content_id(raw: dict[str, Any]) -> str:
    envelope = raw.get("envelope")
    if not isinstance(envelope, dict):
        raise ValueError("attestation object has no envelope")
    try:
        return hashlib.sha256(canonical_bytes(envelope)).hexdigest()
    except ContentIdError as exc:
        # Not a ValueError on every version: an object carrying a value
        # canonical JSON refuses (a float, an unsafe integer) is unusable,
        # and must not escape as anything else (review of #1042).
        raise ValueError(f"attestation object cannot be canonicalized: {exc}") from exc


# -- issuer ---------------------------------------------------------------


def snapshot_objects(db: Database, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Everything a recipient should currently hold from this node: every live
    attestation, and every revocation whose target has not yet expired.

    The same for every recipient, because a recipient is a node and the caller
    already chose which attributes leave at all (issue #596, Decision 2). A
    revocation is included as long as its target could still be held, so a
    recipient that took the object from an older snapshot or the pull is told
    explicitly; past that, absence says the same."""
    now_value, _ = _now(now)
    rows = db.connection.execute(
        """SELECT i.envelope_json, i.signature_b64 FROM link_issued_remote_attestations AS i
           WHERE (i.object_type = ? AND i.redacted_at IS NULL AND i.revoked_at IS NULL AND i.expires_at > ?)
              OR (i.object_type = ? AND EXISTS (
                    SELECT 1 FROM link_issued_remote_attestations AS t
                    WHERE t.content_id = json_extract(i.envelope_json, '$.payload.revoked_content_id')
                      AND t.expires_at > ?))
           ORDER BY i.rowid""",
        (REMOTE_ATTESTATION_OBJECT_TYPE, now_value, REMOTE_ATTESTATION_REVOCATION_OBJECT_TYPE, now_value),
    ).fetchall()
    return [{"envelope": json.loads(row[0]), "signature": row[1]} for row in rows]


@dataclass(frozen=True)
class PlannedDelivery:
    recipient_fingerprint: str
    objects: list[dict[str, Any]]
    digest: str
    final: bool  # a removed recipient's empty snapshot
    sequence: int

    @property
    def content_ids(self) -> list[str]:
        return sorted(_object_content_id(item) for item in self.objects)


def plan_attestation_deliveries(db: Database, *, now: datetime | None = None) -> list[PlannedDelivery]:
    """The bundles this pass should send, at most `MAX_DELIVERIES_PER_PASS`.

    Due: a recipient never sent one, whose snapshot changed, or whose last one
    is older than `BUNDLE_REFRESH_INTERVAL`; and a removed recipient owed its
    empty snapshot. A recipient whose last attempt failed waits out
    `FAILED_DELIVERY_BACKOFF`. A removed recipient past
    `REMOVED_RECIPIENT_RETRY_WINDOW` is given up on and forgotten. Each planned
    delivery reserves its sequence now: time-based, so a restore from backup
    does not move it backwards, and never below the last one used."""
    now_value, moment = _now(now)
    with db.connection:
        db.connection.execute(
            "DELETE FROM link_attestation_bundle_ledger WHERE removed_at IS NOT NULL AND removed_at < ?",
            (utc_iso(moment - REMOVED_RECIPIENT_RETRY_WINDOW),),
        )
        current = [row[0] for row in db.connection.execute(
            "SELECT fingerprint FROM link_attestation_recipients ORDER BY fingerprint"
        )]
        for fingerprint in current:
            db.connection.execute(
                "INSERT OR IGNORE INTO link_attestation_bundle_ledger (recipient_fingerprint) VALUES (?)",
                (fingerprint,),
            )
        ledger = db.connection.execute(
            """SELECT recipient_fingerprint, sequence, sent_digest, sent_at, last_attempt_at,
                      last_error, removed_at, route
               FROM link_attestation_bundle_ledger
               ORDER BY COALESCE(last_attempt_at, ''), recipient_fingerprint"""
        ).fetchall()
        objects = snapshot_objects(db, now=moment)
        digest = snapshot_digest(objects)
        empty_digest = snapshot_digest([])
        refresh_before = utc_iso(moment - BUNDLE_REFRESH_INTERVAL)
        retry_before = utc_iso(moment - FAILED_DELIVERY_BACKOFF)
        now_ms = int(moment.timestamp() * 1000)
        plans: list[PlannedDelivery] = []
        for row in ledger:
            final = row["removed_at"] is not None
            if not final and row["recipient_fingerprint"] not in current:
                continue
            wanted = empty_digest if final else digest
            due = (
                row["sent_at"] is None or row["sent_digest"] != wanted or row["sent_at"] < refresh_before
            )
            if not due:
                continue
            if (row["last_error"] is not None or row["route"] == "pull") \
                    and row["last_attempt_at"] is not None and row["last_attempt_at"] > retry_before:
                continue
            sequence = max(int(row["sequence"]) + 1, now_ms)
            db.connection.execute(
                "UPDATE link_attestation_bundle_ledger SET sequence = ?, last_attempt_at = ? "
                "WHERE recipient_fingerprint = ?",
                (sequence, now_value, row["recipient_fingerprint"]),
            )
            plans.append(PlannedDelivery(
                row["recipient_fingerprint"], [] if final else objects, wanted, final, sequence,
            ))
            if len(plans) >= MAX_DELIVERIES_PER_PASS:
                break
    return plans


def record_attestation_delivery(
    db: Database, recipient_fingerprint: str, *, digest: str, route: str, final: bool,
    content_ids: list[str] | None = None, now: datetime | None = None,
) -> None:
    """A route took the bundle. A removed recipient's final one closes its ledger row.

    `content_ids` are the objects the snapshot held: a snapshot is
    authoritative, so they replace whatever the recipient was recorded as
    holding."""
    now_value, _ = _now(now)
    with db.connection:
        if final:
            db.connection.execute(
                "DELETE FROM link_attestation_bundle_ledger WHERE recipient_fingerprint = ? "
                "AND removed_at IS NOT NULL",
                (recipient_fingerprint,),
            )
            return
        db.connection.execute(
            """UPDATE link_attestation_bundle_ledger
               SET sent_digest = ?, sent_at = ?, route = ?, last_error = NULL, delivered_ids_json = ?
               WHERE recipient_fingerprint = ?""",
            (digest, now_value, route, json.dumps(sorted(content_ids or [])), recipient_fingerprint),
        )


def record_legacy_attestation_recipient(db: Database, recipient_fingerprint: str, *, now: datetime | None = None) -> None:
    """A recipient whose NetBBS does not take sealed snapshots yet: for this
    release it fetches by pull instead, so it is not a failure (review of
    #1045). Its route reads "pull" and what it fetched is recorded as it is
    served (`record_attestation_pull`)."""
    now_value, _ = _now(now)
    with db.connection:
        db.connection.execute(
            "UPDATE link_attestation_bundle_ledger SET route = 'pull', last_error = NULL, last_attempt_at = ? "
            "WHERE recipient_fingerprint = ? AND removed_at IS NULL",
            (now_value, recipient_fingerprint),
        )


def record_attestation_pull(
    db: Database, recipient_fingerprint: str, objects: list[dict[str, Any]], *, now: datetime | None = None,
) -> None:
    """A recipient pulled `objects` (the legacy path). Added to what it is
    recorded as holding: a pull is incremental, unlike a snapshot."""
    if not objects:
        return
    now_value, _ = _now(now)
    served = set()
    for item in objects:
        try:
            served.add(_object_content_id(item))
        except ValueError:
            continue
    with db.connection:
        db.connection.execute(
            "INSERT OR IGNORE INTO link_attestation_bundle_ledger (recipient_fingerprint) VALUES (?)",
            (recipient_fingerprint,),
        )
        row = db.connection.execute(
            "SELECT delivered_ids_json, route FROM link_attestation_bundle_ledger WHERE recipient_fingerprint = ?",
            (recipient_fingerprint,),
        ).fetchone()
        held = set(json.loads(row["delivered_ids_json"])) | served
        db.connection.execute(
            """UPDATE link_attestation_bundle_ledger
               SET delivered_ids_json = ?, sent_at = ?, route = COALESCE(route, 'pull')
               WHERE recipient_fingerprint = ?""",
            (json.dumps(sorted(held)), now_value, recipient_fingerprint),
        )


def record_attestation_delivery_failure(
    db: Database, recipient_fingerprint: str, error: str, *, now: datetime | None = None,
) -> None:
    """No route took the bundle, or it could not be built; said on the
    Published identity screen, never silently dropped."""
    now_value, _ = _now(now)
    with db.connection:
        db.connection.execute(
            "UPDATE link_attestation_bundle_ledger SET last_error = ?, last_attempt_at = ? "
            "WHERE recipient_fingerprint = ?",
            (error[:200], now_value, recipient_fingerprint),
        )


@dataclass(frozen=True)
class AttestationDeliveryStatus:
    recipient_fingerprint: str
    route: str | None  # "direct" | "relay" | None (never delivered)
    sent_at: str | None
    current: bool  # the last bundle sent is the current snapshot
    last_error: str | None
    removed: bool  # owed a final, empty snapshot
    delivered_ids: frozenset[str] = frozenset()  # the objects it was given


def _holds_current(row, snapshot_ids: set[str]) -> bool:
    """Whether a recipient holds exactly what it should now (review of #1042).

    A snapshot replaces what the recipient holds, so a pushed one is current
    only if it held exactly the live set: when an object drops out (expiry, a
    retracted value), the recipient still holds it until the resend withdraws
    it. A pull only ever adds, and is withdrawn by revocations it fetches, so
    for the pull route holding everything live is what counts."""
    delivered = set(json.loads(row["delivered_ids_json"]))
    if row["route"] == "pull":
        return snapshot_ids <= delivered
    return snapshot_ids == delivered


def list_attestation_delivery_status(db: Database, *, now: datetime | None = None) -> list[AttestationDeliveryStatus]:
    """Per recipient: how this node last reached it, whether that delivered
    the current snapshot, and why the last attempt failed. For the Published
    identity screen and the Profile toggle's counts."""
    snapshot_ids = {_object_content_id(item) for item in snapshot_objects(db, now=now)}
    current = {row[0] for row in db.connection.execute("SELECT fingerprint FROM link_attestation_recipients")}
    rows = {row["recipient_fingerprint"]: row for row in db.connection.execute(
        "SELECT * FROM link_attestation_bundle_ledger"
    )}
    result = []
    for fingerprint in sorted(current | set(rows)):
        row = rows.get(fingerprint)
        removed = row is not None and row["removed_at"] is not None
        if fingerprint not in current and not removed:
            continue
        result.append(AttestationDeliveryStatus(
            recipient_fingerprint=fingerprint,
            route=row["route"] if row is not None else None,
            sent_at=row["sent_at"] if row is not None else None,
            current=row is not None and row["sent_at"] is not None
            and _holds_current(row, snapshot_ids),
            delivered_ids=frozenset(json.loads(row["delivered_ids_json"])) if row is not None else frozenset(),
            last_error=row["last_error"] if row is not None else None,
            removed=removed,
        ))
    return result


def attestation_delivery_counts(
    db: Database, user_id: int, attribute: str, *, now: datetime | None = None,
) -> tuple[int, int]:
    """(delivered, named) for one caller's value: how many of the currently
    named recipients hold this caller's current signed object for
    `attribute`, by snapshot or by pull, and how many are named. What the
    Profile toggle shows the caller -- counts only, never which nodes (#596
    Decision 4).

    Per caller, not per snapshot (review of #1045): another caller's change
    makes every recipient's snapshot out of date without touching what it
    holds of this one. A caller with no live object yet -- sharing switched on
    since the last sync pass -- has been sent nothing."""
    now_value, _ = _now(now)
    ids = {
        row[0] for row in db.connection.execute(
            """SELECT content_id FROM link_issued_remote_attestations
               WHERE object_type = ? AND user_id = ? AND attribute = ?
                 AND revoked_at IS NULL AND redacted_at IS NULL AND expires_at > ?""",
            (REMOTE_ATTESTATION_OBJECT_TYPE, user_id, attribute, now_value),
        )
    }
    statuses = [s for s in list_attestation_delivery_status(db, now=now) if not s.removed]
    if not ids:
        return 0, len(statuses)
    return sum(1 for s in statuses if ids <= s.delivered_ids), len(statuses)


# -- recipient ------------------------------------------------------------


@dataclass(frozen=True)
class AppliedSnapshot:
    applied: bool
    reason: str
    ingested: int = 0
    pending: int = 0
    withdrawn: int = 0
    skipped: int = 0


def has_attestation_snapshot_from(db: Database, issuer_fingerprint: str) -> bool:
    """Whether this node has applied a snapshot from `issuer_fingerprint`;
    from then on it stops pulling that issuer (one path, Decision 5)."""
    return db.connection.execute(
        "SELECT 1 FROM link_attestation_bundles_received WHERE issuer_fingerprint = ?", (issuer_fingerprint,)
    ).fetchone() is not None


def last_applied_sequence(db: Database, issuer_fingerprint: str) -> int:
    row = db.connection.execute(
        "SELECT sequence FROM link_attestation_bundles_received WHERE issuer_fingerprint = ?",
        (issuer_fingerprint,),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _verify_key_for(raw: dict[str, Any], verify_keys: list[nacl.signing.VerifyKey]) -> nacl.signing.VerifyKey | None:
    for key in verify_keys:
        try:
            _verify_wire(raw, key)
            return key
        except (ValueError, ContentIdError):
            continue
    return None


def _ingest_objects(
    db: Database, issuer: str, objects: list[dict[str, Any]], verify_keys: list[nacl.signing.VerifyKey],
    now_value: str,
) -> tuple[int, list[dict[str, Any]], int]:
    """Ingest each object as a pulled one would be. Returns (ingested,
    pending, skipped): pending objects wait for a subject this node has not
    met yet; skipped ones are not this issuer's, or do not verify."""
    ingested, skipped = 0, 0
    pending: list[dict[str, Any]] = []
    for raw in objects:
        payload = raw.get("envelope", {}).get("payload") if isinstance(raw.get("envelope"), dict) else None
        if not isinstance(payload, dict) or payload.get("issuer_fingerprint") != issuer:
            skipped += 1
            continue
        key = _verify_key_for(raw, verify_keys)
        if key is None:
            skipped += 1
            continue
        try:
            ingest_remote_attestation(db, raw, issuer_verify_key=key, now_iso=now_value)
            ingested += 1
        except UnknownAttestationSubject:
            if len(pending) < MAX_PENDING_OBJECTS_PER_ISSUER:
                pending.append(raw)
        except (ValueError, ContentIdError) as exc:
            _logger.info("Link attestation snapshot: skipped an object from %s: %s", issuer, exc)
            skipped += 1
    return ingested, pending, skipped


def apply_attestation_snapshot(
    db: Database,
    issuer_fingerprint: str,
    sequence: int,
    objects: list[dict[str, Any]],
    verify_keys: list[nacl.signing.VerifyKey],
    *,
    now: datetime | None = None,
) -> AppliedSnapshot:
    """Apply one opened snapshot from `issuer_fingerprint` as a whole.

    Refused unless the issuer is one of this node's attestation authorities
    (the same subscription set the pull uses) and `sequence` is newer than the
    last applied from it. Then every object is ingested, and whatever this
    node holds from that issuer which the snapshot does not contain -- live,
    and not already revoked -- is withdrawn: marked revoked with no revoking
    object, its value forgotten, audited as `withdrawn_for_recipient`. Objects
    whose subject this node has not met yet are kept and retried each pass
    (`retry_pending_attestation_objects`); they count as present."""
    now_value, _ = _now(now)
    if issuer_fingerprint not in list_attestation_authority_fingerprints(db):
        return AppliedSnapshot(False, "not_an_attestation_authority")
    if sequence <= last_applied_sequence(db, issuer_fingerprint):
        return AppliedSnapshot(False, "stale_sequence")
    present: set[str] = set()
    for raw in objects:
        try:
            present.add(_object_content_id(raw))
        except ValueError:
            continue
    ingested, pending, skipped = _ingest_objects(db, issuer_fingerprint, objects, verify_keys, now_value)
    withdrawn = 0
    with db.connection:
        rows = db.connection.execute(
            """SELECT content_id, subject_id FROM link_remote_attestations
               WHERE issuer_fingerprint = ? AND revoked_at IS NULL AND redacted_at IS NULL""",
            (issuer_fingerprint,),
        ).fetchall()
        for row in rows:
            if row["content_id"] in present:
                continue
            db.connection.execute(
                "UPDATE link_remote_attestations SET revoked_at = ? WHERE content_id = ?",
                (now_value, row["content_id"]),
            )
            _forget_received_values(db, now_value, content_id=row["content_id"])
            _audit(db, row["subject_id"], "attestation", row["content_id"], WITHDRAWN_FOR_RECIPIENT,
                   {"issuer": issuer_fingerprint, "sequence": sequence}, None, now_value)
            _recompute_subject_id(db, row["subject_id"], now_value)
            withdrawn += 1
        db.connection.execute(
            """INSERT INTO link_attestation_bundles_received (issuer_fingerprint, sequence, applied_at, pending_json)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(issuer_fingerprint) DO UPDATE SET sequence = excluded.sequence,
                 applied_at = excluded.applied_at, pending_json = excluded.pending_json""",
            (issuer_fingerprint, sequence, now_value, json.dumps(pending)),
        )
    return AppliedSnapshot(True, "applied", ingested, len(pending), withdrawn, skipped)


def issuers_with_pending_attestation_objects(db: Database) -> list[str]:
    return [row[0] for row in db.connection.execute(
        "SELECT issuer_fingerprint FROM link_attestation_bundles_received WHERE pending_json != '[]' "
        "ORDER BY issuer_fingerprint"
    )]


def retry_pending_attestation_objects(
    db: Database, issuer_fingerprint: str, verify_keys: list[nacl.signing.VerifyKey],
    *, now: datetime | None = None,
) -> int:
    """Ingest objects that waited for a subject this node had not met; returns
    how many went in. What still cannot is kept for the next pass."""
    now_value, _ = _now(now)
    row = db.connection.execute(
        "SELECT pending_json FROM link_attestation_bundles_received WHERE issuer_fingerprint = ?",
        (issuer_fingerprint,),
    ).fetchone()
    if row is None:
        return 0
    ingested, pending, _ = _ingest_objects(db, issuer_fingerprint, json.loads(row[0]), verify_keys, now_value)
    with db.connection:
        db.connection.execute(
            "UPDATE link_attestation_bundles_received SET pending_json = ? WHERE issuer_fingerprint = ?",
            (json.dumps(pending), issuer_fingerprint),
        )
    return ingested


# -- shared async helpers -------------------------------------------------


def _issuer_verify_keys(node: "LinkNode", issuer: str) -> list[nacl.signing.VerifyKey]:
    """The issuer's current signing key only, as the pull verifies (review of
    #1042). A snapshot is signed fresh, and the issuer re-signs what it still
    asserts after a rotation (#623), so a superseded key's signature -- on the
    bundle or on an object inside it -- is skipped for good, never accepted."""
    from netbbs.link.node_identity import NodeIdentityError
    from netbbs.link.protocol import LinkProtocolError

    try:
        return [node.resolve_known_signing_key(issuer, "attestation bundle")]
    except (LinkProtocolError, NodeIdentityError, ValueError):
        return []


async def receive_attestation_bundle_safely(
    node: "LinkNode", lane: "DatabaseLane", bundle: SealedAttestationBundle,
    *, enforce_trust_policy: bool = False, via: str = "direct",
) -> AppliedSnapshot:
    """`receive_attestation_bundle`, whose failure costs that bundle and
    nothing else (review of #1042). Both callers -- the sync pass's relay
    pickup and the direct-delivery route -- handle bundles a peer chose; one
    that raises past every check must not end the sync task or answer 500."""
    import sqlite3

    try:
        return await receive_attestation_bundle(
            node, lane, bundle, enforce_trust_policy=enforce_trust_policy, via=via,
        )
    except (ValueError, TypeError, KeyError, ContentIdError, sqlite3.Error) as exc:
        _logger.warning(
            "Link attestations: skipped a snapshot from %s (%s): %s", bundle.issuer_fingerprint, via, exc,
        )
        return AppliedSnapshot(False, "unusable")


async def receive_attestation_bundle(
    node: "LinkNode", lane: "DatabaseLane", bundle: SealedAttestationBundle,
    *, enforce_trust_policy: bool = False, via: str = "direct",
) -> AppliedSnapshot:
    """Check, open and apply one bundle, however it arrived.

    The issuer must be one this node can verify (a peer or a node known by
    introduction, #627 Decision 5) and, under trust enforcement, established
    here, as the pull requires."""
    issuer = bundle.issuer_fingerprint
    if bundle.recipient_fingerprint != node.identity.fingerprint:
        return AppliedSnapshot(False, "not_addressed_here")
    keys = _issuer_verify_keys(node, issuer)
    if not keys:
        return AppliedSnapshot(False, "issuer_unknown")
    if not bundle.verifies(keys):
        return AppliedSnapshot(False, "bad_signature")
    if enforce_trust_policy:
        from netbbs.link.enforcement import node_transport_state
        from netbbs.link.trust import TrustState

        if await lane.run(node_transport_state, issuer) != TrustState.ESTABLISHED:
            return AppliedSnapshot(False, "issuer_not_established")
    if bundle.sequence <= await lane.run(last_applied_sequence, issuer):
        return AppliedSnapshot(False, "stale_sequence")
    try:
        objects = open_sealed_attestation_bundle(
            bundle, (node.identity.signing_key, *reversed(node.identity.retired_signing_keys))
        )
    except (EncryptionError, MalformedBundle) as exc:
        _logger.warning("Link attestation snapshot from %s could not be opened: %s", issuer, exc)
        return AppliedSnapshot(False, "unreadable")
    result = await lane.run(apply_attestation_snapshot, issuer, bundle.sequence, objects, keys)
    if result.applied:
        _logger.info(
            "Link attestations: applied snapshot %d from %s via %s (%d in, %d waiting, %d withdrawn)",
            bundle.sequence, issuer, via, result.ingested, result.pending, result.withdrawn,
        )
    return result


async def retry_pending_attestations(node: "LinkNode", lane: "DatabaseLane") -> None:
    """Each pass: objects that waited for a subject this node has since met."""
    for issuer in await lane.run(issuers_with_pending_attestation_objects):
        keys = _issuer_verify_keys(node, issuer)
        if keys:
            await lane.run(retry_pending_attestation_objects, issuer, keys)
