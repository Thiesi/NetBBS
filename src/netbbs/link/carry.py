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
import sqlite3
from dataclasses import dataclass

from netbbs.link.boards import (
    BoardCarryLimitError,
    materialize_carried_board,
    materialize_carried_board_closure,
    record_board_origin_change,
)
from netbbs.link.channels import ChannelCarryLimitError, materialize_carried_channel
from netbbs.link.events import (
    BOARD_CLOSURE_OBJECT_TYPE,
    BOARD_GENESIS_OBJECT_TYPE,
    BOARD_ORIGIN_TRANSFER_ACCEPTED_OBJECT_TYPE,
    CHANNEL_GENESIS_OBJECT_TYPE,
    FILE_AREA_GENESIS_OBJECT_TYPE,
    BoardClosure,
    BoardGenesis,
    BoardOriginTransferAccepted,
    ChannelGenesis,
    FileAreaGenesis,
)
from netbbs.link.files import FileAreaCarryLimitError, materialize_carried_file_area
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
    (d.kind = 'boards' AND d.resource_id IN (SELECT board_id FROM boards WHERE link_genesis_json IS NOT NULL))
    OR (d.kind = 'channels' AND d.resource_id IN (SELECT channel_id FROM channels WHERE link_genesis_json IS NOT NULL))
    OR (d.kind = 'file_areas' AND d.resource_id IN (SELECT area_id FROM file_areas WHERE link_genesis_json IS NOT NULL))
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
                     ORDER BY e.received_at LIMIT 1) AS genesis_json
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
            name=str(payload.get("name") or row["resource_id"]),
            description=payload.get("description"),
            origin_fingerprint=str(payload.get("origin_fingerprint") or ""),
            ref=row["ref"],
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
        try:
            _materialize(db, kind, envelope, own_fingerprint=own_fingerprint, cap=cap)
            outcome = "carried"
        except _CARRY_LIMIT_ERRORS as exc:
            outcome = _refusal_reason(exc)
            record_carry_decision(
                db, kind, str(envelope["envelope"]["payload"][_id_field(kind)]), OFFERED, outcome, commit=False,
            )
    except BaseException:
        db.connection.rollback()
        raise
    db.connection.commit()
    return outcome


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


def accept_offer(db: Database, kind: str, resource_id: str, *, actor: User | None) -> None:
    """Carry an offered resource: materialize it from the stored genesis (not
    capped -- the SysOp chose it), apply what was accepted for it meanwhile,
    and clear the offer, in one transaction with its moderation-log entry. The
    next sync pass declares it as carried and pulls its content like any newly
    carried resource."""
    db.connection.execute("BEGIN IMMEDIATE")
    try:
        if carry_decision_state(db, kind, resource_id) != OFFERED:
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
        db.connection.execute(
            "DELETE FROM link_carry_decisions WHERE kind = ? AND resource_id = ?", (kind, resource_id)
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
