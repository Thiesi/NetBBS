"""
The Link carry model (design doc §9.3; decisions under §16, issue #561;
implementation issue #683).

Every genesis this node has accepted is in exactly one recorded state:

- **carried** -- it has a local row (board, channel or file area) and no entry
  in `link_carry_decisions`;
- **offered** -- it arrived past the automatic-intake cap (`max_carried_*`), or
  could not be materialized, and waits for the SysOp to accept it;
- **excluded** -- the SysOp declined it, or deleted it while carried, and it
  stays out until the SysOp reverses that.

Before this the second and third states were inferred from "a genesis in
`link_events` with no local row", which a cap refusal, a deletion and a crash
between saving and materializing all produced alike. Each transition here is
one transaction: the genesis save with its carry outcome, and each later move
between states with the local row and the record changing together.

The caps bound automatic intake only. Lowering one sheds nothing, accepting an
offer is the SysOp's choice and is not capped, and a cap of 0 offers everything
new.
"""

from __future__ import annotations

import json
from collections.abc import Callable
import sqlite3
from dataclasses import dataclass

from netbbs.link.boards import (
    BoardCarryLimitError,
    materialize_carried_board,
    materialize_carried_board_closure,
    rebuild_carried_post_materialization,
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
    CHANNEL_GENESIS_OBJECT_TYPE,
    CHANNEL_MESSAGE_OBJECT_TYPE,
    FILE_AREA_GENESIS_OBJECT_TYPE,
    FILE_DESCRIPTOR_OBJECT_TYPE,
    BoardClosure,
    BoardGenesis,
    BoardOriginTransferAccepted,
    ChannelGenesis,
    ChannelMessage,
    FileAreaGenesis,
    FileDescriptor,
)
from netbbs.link.files import (
    FileAreaCarryLimitError,
    materialize_carried_file_area,
    materialize_carried_file_descriptor,
    RemoteFileCatalogueLimitError,
)
from netbbs.link.events import event_content_id
from netbbs.files.areas import delete_file_area_rows, remove_staging_files
from netbbs.link.store import save_event
from netbbs.auth.users import User
from netbbs.moderation.log import record_action_without_commit
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


OFFERED = "offered"
EXCLUDED = "excluded"

KINDS = ("boards", "channels", "file_areas")

KIND_LABELS = {"boards": "message board", "channels": "chat channel", "file_areas": "file area"}

_GENESIS_TYPES = {
    "boards": BOARD_GENESIS_OBJECT_TYPE,
    "channels": CHANNEL_GENESIS_OBJECT_TYPE,
    "file_areas": FILE_AREA_GENESIS_OBJECT_TYPE,
}
_EVENT_COLUMNS = {"boards": "board_id", "channels": "channel_id", "file_areas": "file_area_id"}
_KIND_OF_GENESIS_TYPE = {object_type: kind for kind, object_type in _GENESIS_TYPES.items()}


class CarryDecisionError(Exception):
    """A carry transition the SysOp asked for cannot be made."""


@dataclass(frozen=True)
class CarryDecision:
    kind: str
    resource_id: str
    state: str
    reason: str
    decided_at: str
    actor_user_id: int | None
    name: str
    description: str | None
    origin_fingerprint: str
    ref: int = 0
    hidden: bool = False
    """Issue #683: the resource's local row is kept, hidden (so Purge applies)."""
    """A small, stable number for the SysOp's picker (the row's rowid)."""


MAX_LISTED_DECISIONS = 500
"""The most decisions one listing loads. The offered set grows with what peers
send a node past its cap -- bounded only by the geneses it already stores -- so
the screen shows the newest this many and says how many there are."""


def genesis_kind(object_type: str) -> str | None:
    """The carry kind a genesis object type belongs to, or `None`."""
    return _KIND_OF_GENESIS_TYPE.get(object_type)


def record_carry_decision(
    db: Database, kind: str, resource_id: str, state: str, reason: str,
    *, actor_user_id: int | None = None, commit: bool = True,
) -> None:
    """Set the decision for one resource, replacing any earlier one."""
    if kind not in KINDS or state not in (OFFERED, EXCLUDED):
        raise ValueError(f"not a carry decision: {kind!r}/{state!r}")
    db.connection.execute(
        """
        INSERT INTO link_carry_decisions (kind, resource_id, state, reason, decided_at, actor_user_id)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(kind, resource_id) DO UPDATE SET
            state = excluded.state, reason = excluded.reason,
            decided_at = excluded.decided_at, actor_user_id = excluded.actor_user_id
        """,
        (kind, resource_id, state, reason, utc_now_iso(), actor_user_id),
    )
    if commit:
        db.connection.commit()


def carry_decision_state(db: Database, kind: str, resource_id: str) -> str | None:
    row = db.connection.execute(
        "SELECT state FROM link_carry_decisions WHERE kind = ? AND resource_id = ?", (kind, resource_id)
    ).fetchone()
    return None if row is None else row["state"]


def carry_decision_counts(db: Database) -> dict[tuple[str, str], int]:
    """`{(kind, state): count}` for the Link status readout, not counting a
    stale decision whose resource has a carried local row."""
    return {
        (row["kind"], row["state"]): row["n"]
        for row in db.connection.execute(
            f"SELECT d.kind, d.state, COUNT(*) AS n FROM link_carry_decisions AS d "
            f"WHERE NOT {_CARRIED_ROW} GROUP BY d.kind, d.state"
        )
    }


def _stored_genesis(db: Database, kind: str, resource_id: str) -> dict | None:
    row = db.connection.execute(
        f"""SELECT envelope_json FROM link_events
             WHERE object_type = ? AND {_EVENT_COLUMNS[kind]} = ?
             ORDER BY received_at ASC LIMIT 1""",
        (_GENESIS_TYPES[kind], resource_id),
    ).fetchone()
    return None if row is None else json.loads(row["envelope_json"])


def count_carry_decisions(db: Database, state: str) -> int:
    """How many resources are in `state`, not counting any with a carried
    local row (a decision left stale by an interrupted deletion)."""
    return db.connection.execute(
        f"SELECT COUNT(*) FROM link_carry_decisions AS d WHERE d.state = ? AND NOT {_CARRIED_ROW}",
        (state,),
    ).fetchone()[0]


# A decision whose resource has a carried local row is stale -- an interrupted
# deletion, say -- and the row wins, as it does in `uncarried_resource_ids`.
_CARRIED_ROW = """(
    (d.kind = 'boards' AND d.resource_id IN (SELECT board_id FROM boards WHERE link_genesis_json IS NOT NULL AND link_hidden_at IS NULL))
    OR (d.kind = 'channels' AND d.resource_id IN (SELECT channel_id FROM channels WHERE link_genesis_json IS NOT NULL AND link_hidden_at IS NULL))
    OR (d.kind = 'file_areas' AND d.resource_id IN (SELECT area_id FROM file_areas WHERE link_genesis_json IS NOT NULL AND link_hidden_at IS NULL))
)"""


def list_carry_decisions(db: Database, state: str, *, limit: int = MAX_LISTED_DECISIONS) -> list[CarryDecision]:
    """The resources in `state`, newest decision first, with what their stored
    genesis says about them: at most `limit`, in one query."""
    decisions: list[CarryDecision] = []
    for row in db.connection.execute(
        f"""SELECT d.rowid AS ref, d.kind, d.resource_id, d.state, d.reason, d.decided_at, d.actor_user_id,
                   (SELECT e.envelope_json FROM link_events AS e
                     WHERE e.object_type = CASE d.kind WHEN 'boards' THEN 'board_genesis'
                                                       WHEN 'channels' THEN 'channel_genesis'
                                                       ELSE 'file_area_genesis' END
                       AND CASE d.kind WHEN 'boards' THEN e.board_id
                                       WHEN 'channels' THEN e.channel_id
                                       ELSE e.file_area_id END = d.resource_id
                     ORDER BY e.received_at LIMIT 1) AS genesis_json,
                   CASE d.kind
                     WHEN 'boards' THEN EXISTS (SELECT 1 FROM boards
                                                 WHERE board_id = d.resource_id AND link_hidden_at IS NOT NULL)
                     WHEN 'channels' THEN EXISTS (SELECT 1 FROM channels
                                                   WHERE channel_id = d.resource_id AND link_hidden_at IS NOT NULL)
                     ELSE EXISTS (SELECT 1 FROM file_areas
                                   WHERE area_id = d.resource_id AND link_hidden_at IS NOT NULL)
                   END AS hidden,
                   -- A hidden row may carry a local name (a rename, or a
                   -- collision suffix): that is the name the SysOp knows it by.
                   CASE d.kind
                     WHEN 'boards' THEN (SELECT name FROM boards WHERE board_id = d.resource_id)
                     WHEN 'channels' THEN (SELECT name FROM channels WHERE channel_id = d.resource_id)
                     ELSE (SELECT name FROM file_areas WHERE area_id = d.resource_id)
                   END AS local_name,
                   CASE d.kind
                     WHEN 'boards' THEN (SELECT description FROM boards WHERE board_id = d.resource_id)
                     WHEN 'channels' THEN (SELECT description FROM channels WHERE channel_id = d.resource_id)
                     ELSE (SELECT description FROM file_areas WHERE area_id = d.resource_id)
                   END AS local_description
              FROM link_carry_decisions AS d
             WHERE d.state = ? AND NOT {_CARRIED_ROW}
             ORDER BY d.decided_at DESC, d.kind, d.resource_id
             LIMIT ?""",
        (state, limit),
    ).fetchall():
        genesis = json.loads(row["genesis_json"]) if row["genesis_json"] else None
        payload = genesis["envelope"]["payload"] if genesis is not None else {}
        decisions.append(CarryDecision(
            kind=row["kind"], resource_id=row["resource_id"], state=row["state"],
            reason=row["reason"], decided_at=row["decided_at"], actor_user_id=row["actor_user_id"],
            name=str(row["local_name"] or payload.get("name") or row["resource_id"]),
            description=row["local_description"] if row["local_name"] else payload.get("description"),
            origin_fingerprint=str(payload.get("origin_fingerprint") or ""),
            ref=row["ref"],
            hidden=bool(row["hidden"]),
        ))
    return decisions


def _materialize(db: Database, kind: str, envelope: dict, *, own_fingerprint: str | None, cap: int | None):
    if kind == "boards":
        return materialize_carried_board(
            db, BoardGenesis.from_dict(envelope), own_fingerprint=own_fingerprint,
            max_carried_boards=cap, commit=False,
        )
    if kind == "channels":
        return materialize_carried_channel(
            db, ChannelGenesis.from_dict(envelope), own_fingerprint=own_fingerprint,
            max_carried_channels=cap, commit=False,
        )
    return materialize_carried_file_area(
        db, FileAreaGenesis.from_dict(envelope), own_fingerprint=own_fingerprint,
        max_carried_file_areas=cap, commit=False,
    )


_CARRY_LIMIT_ERRORS = (BoardCarryLimitError, ChannelCarryLimitError, FileAreaCarryLimitError)


def _refusal_reason(exc: Exception) -> str:
    # The `*CarryRefusedError` subclasses mean "cannot be carried as it
    # stands" (no free local name, an MRC room's id); the bare limit errors
    # mean the cap.
    return "refused" if type(exc).__name__.endswith("RefusedError") else "cap"


def accept_genesis(
    db: Database, *, kind: str, envelope: dict, sender_fingerprint: str, content_id: str,
    own_fingerprint: str, cap: int | None,
) -> str:
    """Save a newly accepted genesis and its carry outcome in one transaction:
    materialized under the cap, or recorded as offered. Returns `"carried"`, or
    the offer's reason: `"cap"`, or `"refused"` when it cannot be carried as it
    stands (no free local name, an MRC room's id). Replaces the separate `save_event` and `materialize_carried_*`
    calls whose gap was the genesis crash window."""
    db.connection.execute("BEGIN IMMEDIATE")
    try:
        save_event(
            db, sender_fingerprint=sender_fingerprint, content_id=content_id,
            object_type=_GENESIS_TYPES[kind], envelope=envelope, commit=False,
        )
        resource_id = str(envelope["envelope"]["payload"][_id_field(kind)])
        if _hidden_row(db, kind, resource_id) is not None:
            # Issue #683: the SysOp hid this resource. It stays excluded;
            # returning its row would count it as carried.
            outcome = "hidden"
        else:
            try:
                _materialize(db, kind, envelope, own_fingerprint=own_fingerprint, cap=cap)
                outcome = "carried"
                # Carried on its own, with its origin's settings and no
                # category: the SysOp is told, until they look at it
                # (issue #681, decided with the maintainer).
                db.connection.execute(
                    "INSERT OR IGNORE INTO link_carried_to_review (kind, resource_id, carried_at) VALUES (?, ?, ?)",
                    (kind, resource_id, utc_now_iso()),
                )
            except _CARRY_LIMIT_ERRORS as exc:
                outcome = _refusal_reason(exc)
                record_carry_decision(db, kind, resource_id, OFFERED, outcome, commit=False)
    except BaseException:
        db.connection.rollback()
        raise
    db.connection.commit()
    return outcome


_LOCAL = {"boards": ("boards", "board_id"), "channels": ("channels", "channel_id"), "file_areas": ("file_areas", "area_id")}


def carried_to_review(db: Database, kind: str) -> set[str]:
    """The resource ids of `kind` carried automatically that the SysOp
    has not looked at yet (issue #681) -- still present and not hidden."""
    table, column = _LOCAL[kind]
    return {
        row[0] for row in db.connection.execute(
            f"""
            SELECT r.resource_id FROM link_carried_to_review r
            JOIN {table} local ON local.{column} = r.resource_id AND local.link_hidden_at IS NULL
            WHERE r.kind = ?
            """,
            (kind,),
        ).fetchall()
    }


def count_carried_to_review(db: Database) -> int:
    """How many carried boards, channels and file areas wait for the
    SysOp's first look, for the dashboard's ATTENTION panel."""
    return sum(len(carried_to_review(db, kind)) for kind in _LOCAL)


def mark_carried_reviewed(db: Database, kind: str, resource_id: str) -> None:
    """The SysOp has opened this carried resource's own screen: it is no
    longer news."""
    db.connection.execute(
        "DELETE FROM link_carried_to_review WHERE kind = ? AND resource_id = ?", (kind, resource_id)
    )
    db.connection.commit()


def _hidden_row(db: Database, kind: str, resource_id: str):
    table, column = _LOCAL[kind]
    return db.connection.execute(
        f"SELECT * FROM {table} WHERE {column} = ? AND link_hidden_at IS NOT NULL", (resource_id,)
    ).fetchone()


def _id_field(kind: str) -> str:
    return {"boards": "board_id", "channels": "channel_id", "file_areas": "area_id"}[kind]


_OBJECT_TYPES = {"boards": "board", "channels": "channel", "file_areas": "file_area"}


def _audit_detail(envelope: dict | None, resource_id: str) -> str:
    payload = envelope["envelope"]["payload"] if envelope is not None else {}
    return f"{resource_id} {payload.get('name')!r} from {payload.get('origin_fingerprint')}"


def _replay_board_lifecycle(db: Database, board_id: str) -> None:
    """Apply the origin transfers and closure this node accepted for a board
    while it had no local row: each was saved, and each was a silent no-op
    then, so the row would otherwise come back with its genesis origin and
    open."""
    for row in db.connection.execute(
        """SELECT object_type, envelope_json FROM link_events
            WHERE board_id = ? AND object_type IN (?, ?)
            ORDER BY received_at ASC""",
        (board_id, BOARD_ORIGIN_TRANSFER_ACCEPTED_OBJECT_TYPE, BOARD_CLOSURE_OBJECT_TYPE),
    ).fetchall():
        envelope = json.loads(row["envelope_json"])
        if row["object_type"] == BOARD_ORIGIN_TRANSFER_ACCEPTED_OBJECT_TYPE:
            accepted = BoardOriginTransferAccepted.from_dict(envelope)
            record_board_origin_change(
                db, board_id, accepted.payload["new_origin_fingerprint"], commit=False
            )
        else:
            materialize_carried_board_closure(db, BoardClosure.from_dict(envelope), commit=False)


def accept_offer(
    db: Database, kind: str, resource_id: str, *, actor: User | None,
    max_remote_files_per_area: int | None = None,
) -> None:
    """Carry an offered resource: materialize it from the stored genesis (not
    capped -- the SysOp chose it), apply what was accepted for it meanwhile,
    and clear the offer, in one transaction with its moderation-log entry. The
    next sync pass declares it as carried and pulls its content like any newly
    carried resource."""
    db.connection.execute("BEGIN IMMEDIATE")
    try:
        row = db.connection.execute(
            "SELECT state, reason FROM link_carry_decisions WHERE kind = ? AND resource_id = ?",
            (kind, resource_id),
        ).fetchone()
        if row is None or row["state"] != OFFERED or row["reason"] == "accepting":
            raise CarryDecisionError("that resource is no longer on offer")
        envelope = _stored_genesis(db, kind, resource_id)
        if envelope is None:
            raise CarryDecisionError("this node no longer holds that resource's genesis")
        try:
            materialized = _materialize(db, kind, envelope, own_fingerprint=None, cap=None)
        except _CARRY_LIMIT_ERRORS as exc:
            raise CarryDecisionError(str(exc)) from exc
        except sqlite3.IntegrityError as exc:
            raise CarryDecisionError(f"could not create the local copy: {exc}") from exc
        if kind == "boards":
            _replay_board_lifecycle(db, resource_id)
        # Not deleted yet: the row stays, marked `accepting`, until the
        # stored content is reprojected below. A process that dies in
        # between finishes it at startup (`finish_pending_acceptances`);
        # a carried row beside it keeps it out of every list meanwhile.
        db.connection.execute(
            "UPDATE link_carry_decisions SET reason = 'accepting' WHERE kind = ? AND resource_id = ?",
            (kind, resource_id),
        )
        if actor is not None:
            record_action_without_commit(
                db, actor=actor, action="accept_link_offer", object_type=_OBJECT_TYPES[kind],
                object_id=materialized.id, detail=_audit_detail(envelope, resource_id),
            )
    except BaseException:
        db.connection.rollback()
        raise
    db.connection.commit()
    _finish_acceptance(db, kind, resource_id, max_remote_files_per_area=max_remote_files_per_area)


def _finish_acceptance(db: Database, kind: str, resource_id: str, *, max_remote_files_per_area: int | None) -> None:
    reproject_stored_content(db, kind, resource_id, max_remote_files_per_area=max_remote_files_per_area)
    db.connection.execute(
        "DELETE FROM link_carry_decisions WHERE kind = ? AND resource_id = ? AND reason = 'accepting'",
        (kind, resource_id),
    )
    db.connection.commit()


def finish_pending_acceptances(db: Database, *, max_remote_files_per_area: int | None = None) -> int:
    """Complete any acceptance a previous run committed but did not finish
    reprojecting (issue #683). Called once at startup; every step is
    idempotent. Returns how many were finished."""
    pending = db.connection.execute(
        "SELECT kind, resource_id FROM link_carry_decisions WHERE reason = 'accepting'"
    ).fetchall()
    for row in pending:
        _finish_acceptance(
            db, row["kind"], row["resource_id"], max_remote_files_per_area=max_remote_files_per_area
        )
    return len(pending)


def reproject_stored_content(
    db: Database, kind: str, resource_id: str, *, max_remote_files_per_area: int | None = None
) -> None:
    """Project what this node already holds for a resource it has just taken
    on. A resource carried before, deleted, and offered again by the
    migration (or accepted back later) keeps its posts, messages and file
    descriptors in `link_events` after their local rows are gone; this node
    declares those IDs as known, so no peer would ever send them again, and
    the accepted copy would stay missing its history. Each materializer is
    idempotent on the event's content ID, so this is safe to repeat.

    Scoped to this one resource, and bound by the same limits as intake: the
    file catalogue cap applies, so descriptors refused at the cap stay
    refused."""
    if kind == "boards":
        rebuild_carried_post_materialization(db, board_id=resource_id)
        return
    column, object_type = {
        "channels": ("channel_id", CHANNEL_MESSAGE_OBJECT_TYPE),
        "file_areas": ("file_area_id", FILE_DESCRIPTOR_OBJECT_TYPE),
    }[kind]
    for row in db.connection.execute(
        f"""SELECT sender_fingerprint, envelope_json FROM link_events
             WHERE object_type = ? AND {column} = ? ORDER BY received_at ASC""",
        (object_type, resource_id),
    ).fetchall():
        envelope = json.loads(row["envelope_json"])
        if kind == "channels":
            materialize_carried_channel_message(
                db, ChannelMessage.from_dict(envelope), sender_fingerprint=row["sender_fingerprint"]
            )
            continue
        try:
            materialize_carried_file_descriptor(
                db, FileDescriptor.from_dict(envelope), sender_fingerprint=row["sender_fingerprint"],
                max_remote_files_per_area=max_remote_files_per_area,
            )
        except RemoteFileCatalogueLimitError:
            break


def exclude_offer(db: Database, kind: str, resource_id: str, *, actor: User | None) -> None:
    """Decline an offered resource without ever carrying it."""
    db.connection.execute("BEGIN IMMEDIATE")
    try:
        if carry_decision_state(db, kind, resource_id) != OFFERED:
            raise CarryDecisionError("that resource is no longer on offer")
        record_carry_decision(
            db, kind, resource_id, EXCLUDED, "sysop",
            actor_user_id=actor.id if actor is not None else None, commit=False,
        )
        if actor is not None:
            record_action_without_commit(
                db, actor=actor, action="exclude_link_offer", object_type=_OBJECT_TYPES[kind],
                detail=_audit_detail(_stored_genesis(db, kind, resource_id), resource_id),
            )
    except BaseException:
        db.connection.rollback()
        raise
    db.connection.commit()


# -- hide, restore, purge (issue #683; Thiesi's decision of 2026-09-26) -------
#
# Deleting a carried resource whose origin is another node hides it: the row
# and everything in it are kept, invisible to callers and to content
# administration, not carried, and taking no new content. Restore clears the
# mark and the resource is back exactly as it was -- this node's own users'
# posts with their authorship, and this node's own moderation, neither of which
# a replay of signed events could reproduce (own posts are keyed by a local
# hash, and a carrying node's moderation is not in the signed history). Purge
# deletes it for real; after that, Restore can only take it on again from its
# genesis, like an offer.


class CarryOwnershipUnknown(CarryDecisionError):
    """This node's own fingerprint is not known here, so whether a Linked
    resource is carried from elsewhere cannot be decided."""


def resolve_own_fingerprint(db: Database, own_fingerprint: str | None) -> str | None:
    """This node's root fingerprint: the caller's, or the copy the running node
    caches at every startup (the root key never rotates, so it cannot go
    stale). `None` only on a node that has never started."""
    if own_fingerprint is not None:
        return own_fingerprint
    from netbbs.managed_dns.state import get_node_fingerprint  # deferred: keeps link free of a hard dependency

    return get_node_fingerprint(db)


def carried_from_elsewhere(db: Database, kind: str, resource_id: str, own_fingerprint: str | None) -> bool:
    """Whether deleting this local resource should hide it rather than delete
    it: it is Linked, and its current origin is another node (a board's origin
    can move, §9.4; channels and file areas have no succession). Decided by the
    persisted current origin against this node's own fingerprint -- the
    caller's, or the cached one -- and never guessed from where a genesis
    happens to be stored: a board created here and transferred away has no
    genesis in `link_events`, and one transferred here does. Raises
    `CarryOwnershipUnknown` for a Linked resource when the fingerprint cannot
    be resolved."""
    table, column = _LOCAL[kind]
    row = db.connection.execute(f"SELECT * FROM {table} WHERE {column} = ?", (resource_id,)).fetchone()
    if row is None or row["link_genesis_json"] is None:
        return False
    genesis = json.loads(row["link_genesis_json"])
    origin = genesis["envelope"]["payload"].get("origin_fingerprint")
    if kind == "boards" and row["link_origin_fingerprint"]:
        origin = row["link_origin_fingerprint"]
    own = resolve_own_fingerprint(db, own_fingerprint)
    if own is None:
        raise CarryOwnershipUnknown(
            "this node's Link identity is not known here yet; start the node once, or delete it from "
            "the running node's console"
        )
    return origin != own


def hide_carried_resource(
    db: Database, kind: str, resource_id: str, *, actor: User | None, own_fingerprint: str | None = None
) -> None:
    """Hide a carried resource in one transaction: keep its genesis in
    `link_events`, set `link_hidden_at`, record it excluded (`deleted`) and
    audit it."""
    table, column = _LOCAL[kind]
    db.connection.execute("BEGIN IMMEDIATE")
    try:
        row = db.connection.execute(f"SELECT * FROM {table} WHERE {column} = ?", (resource_id,)).fetchone()
        if row is None or row["link_genesis_json"] is None:
            raise CarryDecisionError("that is not a carried Link resource")
        if row["link_hidden_at"] is not None:
            raise CarryDecisionError("that resource is already excluded")
        # Re-checked under the lock: an origin transfer to this node accepted
        # while the SysOp was confirming makes this node the board's authority,
        # and a resource this node originates is not a carry choice.
        if not carried_from_elsewhere(db, kind, resource_id, own_fingerprint):
            raise CarryDecisionError(
                "this node is now that resource's origin, so it is not a carry choice; delete it again to remove it"
            )
        genesis = json.loads(row["link_genesis_json"])
        save_event(
            db, sender_fingerprint=genesis["envelope"]["payload"]["origin_fingerprint"],
            content_id=event_content_id(genesis["envelope"]), object_type=_GENESIS_TYPES[kind],
            envelope=genesis, commit=False,
        )
        db.connection.execute(f"UPDATE {table} SET link_hidden_at = ? WHERE id = ?", (utc_now_iso(), row["id"]))
        record_carry_decision(
            db, kind, resource_id, EXCLUDED, "deleted",
            actor_user_id=actor.id if actor is not None else None, commit=False,
        )
        if actor is not None:
            record_action_without_commit(
                db, actor=actor, action="hide_link_resource", object_type=_OBJECT_TYPES[kind],
                object_id=row["id"], detail=_audit_detail(genesis, resource_id),
            )
    except BaseException:
        db.connection.rollback()
        raise
    db.connection.commit()


def restore_excluded(
    db: Database, kind: str, resource_id: str, *, actor: User | None,
    max_remote_files_per_area: int | None = None,
) -> None:
    """Take an excluded resource back. A hidden one is un-hidden exactly as it
    was; one with no local row (declined while offered, or purged) is taken on
    from its genesis like an accepted offer. Either way the next sync pass
    declares it carried again and pulls what arrived meanwhile."""
    table, column = _LOCAL[kind]
    db.connection.execute("BEGIN IMMEDIATE")
    reproject = False
    try:
        if carry_decision_state(db, kind, resource_id) != EXCLUDED:
            raise CarryDecisionError("that resource is not excluded")
        row = db.connection.execute(f"SELECT * FROM {table} WHERE {column} = ?", (resource_id,)).fetchone()
        envelope = _stored_genesis(db, kind, resource_id)
        if row is not None:
            db.connection.execute(f"UPDATE {table} SET link_hidden_at = NULL WHERE id = ?", (row["id"],))
            db.connection.execute(
                "DELETE FROM link_carry_decisions WHERE kind = ? AND resource_id = ?", (kind, resource_id)
            )
            object_id = row["id"]
        else:
            if envelope is None:
                raise CarryDecisionError("this node no longer holds that resource's genesis")
            try:
                materialized = _materialize(db, kind, envelope, own_fingerprint=None, cap=None)
            except _CARRY_LIMIT_ERRORS as exc:
                raise CarryDecisionError(str(exc)) from exc
            except sqlite3.IntegrityError as exc:
                raise CarryDecisionError(f"could not create the local copy: {exc}") from exc
            if kind == "boards":
                _replay_board_lifecycle(db, resource_id)
            db.connection.execute(
                "UPDATE link_carry_decisions SET state = 'offered', reason = 'accepting' "
                "WHERE kind = ? AND resource_id = ?",
                (kind, resource_id),
            )
            object_id = materialized.id
            reproject = True
        if actor is not None:
            record_action_without_commit(
                db, actor=actor, action="restore_link_resource", object_type=_OBJECT_TYPES[kind],
                object_id=object_id, detail=_audit_detail(envelope, resource_id),
            )
    except BaseException:
        db.connection.rollback()
        raise
    db.connection.commit()
    if reproject:
        _finish_acceptance(db, kind, resource_id, max_remote_files_per_area=max_remote_files_per_area)


def remove_linked_or_local(
    db: Database, kind: str, resource_id: str, *, actor: User, own_fingerprint: str | None,
    delete: "Callable[[], None] | None",
) -> str:
    """The SysOp's delete, decided and done in one lane job (issue #683), so an
    origin transfer the running node accepts meanwhile -- it runs on the same
    lane -- cannot land between the decision and the act. Carried from
    elsewhere: hidden (reversible), whatever the screen expected. Otherwise
    `delete` runs, after `retain_linked_genesis` keeps a Linked resource's
    genesis and records it excluded; with no `delete` (the hide screen, which
    never warned about a permanent delete) the change of ownership is refused.
    Returns `"hidden"`, `"deleted"` or `"deleted-excluded"`."""
    from netbbs.link.store import clear_deletion_record, retain_linked_genesis

    if carried_from_elsewhere(db, kind, resource_id, own_fingerprint):
        hide_carried_resource(db, kind, resource_id, actor=actor, own_fingerprint=own_fingerprint)
        return "hidden"
    if delete is None:
        raise CarryDecisionError(
            "this node is now that resource's origin, so it is not a carry choice; delete it again to remove it"
        )
    kept = retain_linked_genesis(db, kind, resource_id, actor_user_id=actor.id)
    try:
        delete()
    except BaseException:
        if kept:
            clear_deletion_record(db, kind, resource_id)
        raise
    return "deleted-excluded" if kept else "deleted"


def purge_excluded(db: Database, kind: str, resource_id: str, *, actor: User | None) -> None:
    """Delete a hidden resource for real, in one transaction with its audit
    entry. It stays excluded (`purged`), so it is not carried again unasked;
    Restore can still take it on from its genesis."""
    table, column = _LOCAL[kind]
    db.connection.execute("BEGIN IMMEDIATE")
    staging: list[str] = []
    try:
        row = db.connection.execute(
            f"SELECT * FROM {table} WHERE {column} = ? AND link_hidden_at IS NOT NULL", (resource_id,)
        ).fetchone()
        if row is None:
            raise CarryDecisionError("only a hidden resource can be purged")
        local_id = row["id"]
        object_type = _OBJECT_TYPES[kind]
        if kind == "boards":
            db.connection.execute("DELETE FROM posts WHERE board_id = ?", (local_id,))
        elif kind == "channels":
            db.connection.execute("DELETE FROM channel_messages WHERE channel_id = ?", (local_id,))
            db.connection.execute("DELETE FROM channel_message_search WHERE channel_id = ?", (local_id,))
            for child in ("channel_restrictions", "channel_members", "channel_invitations"):
                db.connection.execute(f"DELETE FROM {child} WHERE channel_id = ?", (local_id,))
        if kind == "file_areas":
            # The remote catalogue goes first, in foreign-key order (#696).
            staging = delete_file_area_rows(db, local_id)
        else:
            for scoped in ("moderator_grants", "user_read_cursors", "user_follows"):
                db.connection.execute(
                    f"DELETE FROM {scoped} WHERE object_type = ? AND object_id = ?", (object_type, local_id)
                )
            db.connection.execute(f"DELETE FROM {table} WHERE id = ?", (local_id,))
        db.connection.execute(
            "UPDATE link_carry_decisions SET reason = 'purged', decided_at = ?, actor_user_id = ? "
            "WHERE kind = ? AND resource_id = ?",
            (utc_now_iso(), actor.id if actor is not None else None, kind, resource_id),
        )
        if actor is not None:
            record_action_without_commit(
                db, actor=actor, action="purge_link_resource", object_type=object_type, object_id=local_id,
                detail=_audit_detail(_stored_genesis(db, kind, resource_id), resource_id),
            )
    except BaseException:
        db.connection.rollback()
        raise
    db.connection.commit()
    remove_staging_files(staging)
