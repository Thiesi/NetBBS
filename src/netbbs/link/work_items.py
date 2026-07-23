"""
Generic outbound work items (design doc §13.7, issue #60's second
operational slice): a `pending -> retrying -> pushed | dead_lettered |
cancelled` tracker, scoped deliberately narrow -- only Link mail
delivery and Link mail acknowledgement delivery, the two existing
retry-shaped mechanisms in `netbbs.link` that actually fit this shape.
Board/identity event gossip and relay selection/consent maintenance are
*not* covered here on purpose (see this module's own design doc section
for the audit that ruled them out): forcing either into a per-target
attempt/backoff/dead-letter model would invent a failure mode neither
one actually has.

**"Pushed" is not "delivered."** A work item resolving successfully
means its payload was handed to the recipient's transport (or deposited
at a relay) at least once -- never that the recipient confirmed
receipt. For Link mail specifically, that confirmation is a completely
separate, pre-existing thing (`netbbs.link.mail.apply_link_message_
accepted`/`apply_link_message_bounced`, driven by a genuine signed event
coming back), unrelated to whether a push attempt succeeded. This
module knows nothing about `mail_messages`/`link_mail_acknowledgements`
at all -- kind-agnostic by design, storing only a `reference_id` pointer
never a payload copy. The one integration point (a dead-lettered/
cancelled `link_mail_delivery` item implying `mail_messages.
link_delivery_status = 'expired'`) lives in the caller
(`netbbs.link.sync`), not here.

Plain, synchronous, `db`-first functions, matching every other
`netbbs.link.*` module's calling convention -- dispatched via
`DatabaseLane.run` from async call sites.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from netbbs.auth.users import User
from netbbs.moderation.log import record_action
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

KIND_LINK_MAIL_DELIVERY = "link_mail_delivery"
KIND_LINK_MAIL_ACK = "link_mail_ack"

# Backoff/dead-letter thresholds (design doc §13.7): product judgment,
# not derived from anything load-bearing -- adjustable later without a
# migration, since they're plain Python constants, not stored config.
_INITIAL_BACKOFF_SECONDS = 300.0  # 5 minutes -- the sync loop's own default interval
_MAX_BACKOFF_SECONDS = 21_600.0  # 6 hours
_MAX_ATTEMPTS = 10
_MAX_AGE_SECONDS = 5 * 86_400.0  # 5 days

_TERMINAL_STATUSES = ("pushed", "dead_lettered", "cancelled")
_UNRESOLVED_STATUSES = ("pending", "retrying")


class WorkItemError(Exception):
    """Raised for an invalid work-item state transition (replaying
    something that isn't dead-lettered/cancelled, cancelling something
    already resolved) or an unknown work-item id."""


@dataclass(frozen=True)
class WorkItem:
    id: int
    kind: str
    reference_id: str
    target_fingerprint: str
    status: str
    attempts: int
    next_attempt_at: str
    created_at: str
    last_attempt_at: str | None
    last_error: str | None
    resolved_at: str | None


def _backoff_seconds(attempts: int) -> float:
    """Exponential backoff from `_INITIAL_BACKOFF_SECONDS`, doubling per
    failed attempt, capped at `_MAX_BACKOFF_SECONDS` -- see this
    module's design doc section for why these particular numbers."""
    return min(_INITIAL_BACKOFF_SECONDS * (2**attempts), _MAX_BACKOFF_SECONDS)


def _offset_iso(seconds: float) -> str:
    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
    return when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _cutoff_iso(seconds: float) -> str:
    """The ISO timestamp `seconds` in the past from now -- comparable
    directly against a stored `created_at` string, same convention as
    `netbbs.boards.posts._cutoff_iso`."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def enqueue_work_item(db: Database, *, kind: str, reference_id: str, target_fingerprint: str) -> WorkItem:
    """Schedule a new work item for immediate first attempt, or return
    the existing one unchanged if this exact `(kind, reference_id,
    target_fingerprint)` is already tracked -- idempotent, matching
    `INSERT ... ON CONFLICT DO NOTHING`'s own established shape
    elsewhere in this codebase (`netbbs.activity.follow`)."""
    item = enqueue_work_item_without_commit(
        db, kind=kind, reference_id=reference_id, target_fingerprint=target_fingerprint
    )
    db.connection.commit()
    return item


def enqueue_work_item_without_commit(
    db: Database, *, kind: str, reference_id: str, target_fingerprint: str
) -> WorkItem:
    """Like `enqueue_work_item`, but for a caller (`netbbs.link.mail.
    compose_link_message`/`_queue_acknowledgement`) that needs this
    insert to be part of the same already-open transaction as the row
    it's scheduling delivery for -- a crash between the two inserts must
    never leave a message with no work item ever tracking it. The
    caller is responsible for eventually committing (or rolling back);
    see `netbbs.moderation.log.record_action_without_commit`'s own
    docstring for the identical reasoning."""
    created_at = utc_now_iso()
    db.connection.execute(
        """
        INSERT INTO link_work_items
            (kind, reference_id, target_fingerprint, status, attempts, next_attempt_at, created_at)
        VALUES (?, ?, ?, 'pending', 0, ?, ?)
        ON CONFLICT(kind, reference_id, target_fingerprint) DO NOTHING
        """,
        (kind, reference_id, target_fingerprint, created_at, created_at),
    )
    return _get_by_key(db, kind=kind, reference_id=reference_id, target_fingerprint=target_fingerprint)


def _get_by_key(db: Database, *, kind: str, reference_id: str, target_fingerprint: str) -> WorkItem:
    row = db.connection.execute(
        "SELECT * FROM link_work_items WHERE kind = ? AND reference_id = ? AND target_fingerprint = ?",
        (kind, reference_id, target_fingerprint),
    ).fetchone()
    return _row_to_work_item(row)


def get_work_item(db: Database, work_item_id: int) -> WorkItem:
    row = db.connection.execute("SELECT * FROM link_work_items WHERE id = ?", (work_item_id,)).fetchone()
    if row is None:
        raise WorkItemError(f"no such work item: {work_item_id!r}")
    return _row_to_work_item(row)


def load_due_work_items(db: Database, *, kind: str, limit: int = 100) -> list[WorkItem]:
    """Every `kind` work item currently eligible for an attempt --
    `status` still unresolved *and* `next_attempt_at` has passed --
    ordered oldest-due-first. What `netbbs.link.sync`'s push loop reads
    every pass instead of today's "resend every pending row
    unconditionally" query."""
    rows = db.connection.execute(
        """
        SELECT * FROM link_work_items
        WHERE kind = ? AND status IN ('pending', 'retrying') AND next_attempt_at <= ?
        ORDER BY next_attempt_at ASC
        LIMIT ?
        """,
        (kind, utc_now_iso(), limit),
    ).fetchall()
    return [_row_to_work_item(row) for row in rows]


def list_work_items(db: Database, *, status: str | None = None, kind: str | None = None) -> list[WorkItem]:
    """Every work item matching the given filters, newest first -- the
    SysOp inspection surface (`netbbs.net.admin_flow`'s outbox screen)."""
    query = "SELECT * FROM link_work_items"
    conditions = []
    params: list[str] = []
    if status is not None:
        conditions.append("status = ?")
        params.append(status)
    if kind is not None:
        conditions.append("kind = ?")
        params.append(kind)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at DESC"
    rows = db.connection.execute(query, params).fetchall()
    return [_row_to_work_item(row) for row in rows]


def record_success(db: Database, work_item: WorkItem) -> WorkItem:
    """The payload was successfully pushed -- terminal, no further
    attempts. Does *not* touch any domain table (`mail_messages`,
    `link_mail_acknowledgements`) -- see module docstring."""
    now = utc_now_iso()
    db.connection.execute(
        "UPDATE link_work_items SET status = 'pushed', last_attempt_at = ?, resolved_at = ? WHERE id = ?",
        (now, now, work_item.id),
    )
    db.connection.commit()
    return get_work_item(db, work_item.id)


def record_failure(db: Database, work_item: WorkItem, *, error: str) -> WorkItem:
    """One failed attempt -- either schedules the next one (`retrying`)
    or, once `_MAX_ATTEMPTS` or `_MAX_AGE_SECONDS` is exceeded (whichever
    first), dead-letters the item. Returns the updated item; the caller
    is responsible for checking `.status == 'dead_lettered'` and acting
    on its own domain table if it needs to (e.g. `netbbs.link.sync`
    setting `mail_messages.link_delivery_status = 'expired'`)."""
    attempts = work_item.attempts + 1
    now = utc_now_iso()
    too_old = work_item.created_at < _cutoff_iso(_MAX_AGE_SECONDS)
    if attempts >= _MAX_ATTEMPTS or too_old:
        db.connection.execute(
            """
            UPDATE link_work_items
            SET status = 'dead_lettered', attempts = ?, last_attempt_at = ?, last_error = ?, resolved_at = ?
            WHERE id = ?
            """,
            (attempts, now, error, now, work_item.id),
        )
    else:
        next_attempt_at = _offset_iso(_backoff_seconds(attempts))
        db.connection.execute(
            """
            UPDATE link_work_items
            SET status = 'retrying', attempts = ?, last_attempt_at = ?, last_error = ?, next_attempt_at = ?
            WHERE id = ?
            """,
            (attempts, now, error, next_attempt_at, work_item.id),
        )
    db.connection.commit()
    return get_work_item(db, work_item.id)


def replay_work_item(db: Database, work_item_id: int, *, replayed_by: User) -> WorkItem:
    """Reset a `dead_lettered`/`cancelled` item back to `pending` with a
    clean attempt count, eligible for an immediate retry next pass.
    Refuses on anything not already terminal -- replaying a `pending`/
    `retrying` item makes no sense (it's already going to be tried
    again on its own), and replaying an already-`pushed` one would
    re-attempt a delivery that already succeeded."""
    item = get_work_item(db, work_item_id)
    if item.status not in ("dead_lettered", "cancelled"):
        raise WorkItemError(
            f"cannot replay work item {work_item_id}: status is {item.status!r}, "
            "not 'dead_lettered' or 'cancelled'"
        )
    now = utc_now_iso()
    db.connection.execute(
        """
        UPDATE link_work_items
        SET status = 'pending', attempts = 0, next_attempt_at = ?, last_error = NULL, resolved_at = NULL
        WHERE id = ?
        """,
        (now, work_item_id),
    )
    record_action(
        db, actor=replayed_by, action="replay_link_work_item", object_type="link_work_item",
        object_id=work_item_id, detail=f"kind={item.kind} target={item.target_fingerprint}",
    )
    return get_work_item(db, work_item_id)


def cancel_work_item(db: Database, work_item_id: int, *, cancelled_by: User) -> WorkItem:
    """Give up on a still-unresolved item deliberately, distinct from an
    automatic dead-letter -- e.g. a SysOp who knows a recipient is gone
    for good and doesn't want to wait out the remaining backoff/age
    budget. Refuses on anything already terminal."""
    item = get_work_item(db, work_item_id)
    if item.status not in _UNRESOLVED_STATUSES:
        raise WorkItemError(f"cannot cancel work item {work_item_id}: status is already {item.status!r}")
    now = utc_now_iso()
    db.connection.execute(
        "UPDATE link_work_items SET status = 'cancelled', resolved_at = ? WHERE id = ?",
        (now, work_item_id),
    )
    record_action(
        db, actor=cancelled_by, action="cancel_link_work_item", object_type="link_work_item",
        object_id=work_item_id, detail=f"kind={item.kind} target={item.target_fingerprint}",
    )
    return get_work_item(db, work_item_id)


def _row_to_work_item(row) -> WorkItem:
    return WorkItem(
        id=row["id"],
        kind=row["kind"],
        reference_id=row["reference_id"],
        target_fingerprint=row["target_fingerprint"],
        status=row["status"],
        attempts=row["attempts"],
        next_attempt_at=row["next_attempt_at"],
        created_at=row["created_at"],
        last_attempt_at=row["last_attempt_at"],
        last_error=row["last_error"],
        resolved_at=row["resolved_at"],
    )
