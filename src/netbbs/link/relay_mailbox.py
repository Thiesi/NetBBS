"""
Bounded relay store-and-forward mailbox (design doc §12, issue
#58) -- a relay only ever custodies opaque, already-encrypted `link_
message` envelopes addressed to a fingerprint it has granted `i_relay_
for` consent to (`netbbs.link.protocol.LinkNode.relaying_for`), bounded
per recipient so a careless or hostile depositor can't grow one
recipient's held mail without limit (CLAUDE.md's own "bound remotely
influenced resources" principle, applied here the same way `netbbs.
link.protocol`'s own `_MAX_PEER_LIST_ENTRIES_PER_REQUEST`/`_MAX_
CANDIDATE_DESCRIPTORS` bound peer-list state).

Covers the full `link_message`-family round trip (issue #94's sibling
fix): `link_message` itself, and -- since issue #83's live dogfood run
found that an outgoing-only sender's own acknowledgement had nowhere to
go, a documented-but-unbuilt follow-up until now -- `link_message_
accepted`/`link_message_bounced` too, so a recipient who can't be
dialed directly (an outgoing-only node acknowledging mail sent *to* it,
or an outgoing-only node's own mail to a full peer finally resolving to
"delivered" on that node's own side) has a real path back. All three
share the identical `envelope`/`signature` shape (see each dataclass's
own definition in `netbbs.link.events`), so nothing here needs a
per-type storage format -- only the `object_type` used to reconstruct
the right class on pickup differs.

This module never verifies a deposited envelope's signature or reads
its plaintext -- it can't (the ciphertext is sealed to the recipient's
own key, design doc §12's own "never content" limitation, which also
covers `link_message_accepted`/`_bounced` -- neither carries plaintext
either) and doesn't need to (the recipient re-runs full protocol-level
verification via `netbbs.link.protocol.LinkNode.handle_events` after
pickup, exactly the same way it already verifies anything else — see
`netbbs.link.sync`'s own relay-pickup wiring, issue #58 task #25, for
where that happens). `deposit_relay_mailbox_envelope` only checks the
*shape* is one of the three well-formed types above (a signature it
can't validate, but a payload whose fields it can at least parse), so
it isn't storing arbitrary non-Link garbage.

Held envelopes are bounded in time as well as in number (issue #891):
anything not collected within `RELAY_MAILBOX_RETENTION_DAYS` is dropped by
`prune_expired_relay_mailbox_envelopes`, which `netbbs.link.sync` runs once
per pass.

Plain, synchronous, `db`-first functions dispatched via `DatabaseLane.
run`, same convention as `netbbs.link.store`/`netbbs.link.reliability`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from netbbs.link.events import (
    LINK_MESSAGE_ACCEPTED_OBJECT_TYPE,
    LINK_MESSAGE_BOUNCED_OBJECT_TYPE,
    LINK_MESSAGE_OBJECT_TYPE,
    LinkMessage,
    LinkMessageAccepted,
    LinkMessageBounced,
)
from netbbs.storage.database import Database
from netbbs.timeutil import utc_iso, utc_now_iso

RelayableEnvelope = LinkMessage | LinkMessageAccepted | LinkMessageBounced

_ENVELOPE_TYPES_BY_OBJECT_TYPE: dict[str, type[RelayableEnvelope]] = {
    LINK_MESSAGE_OBJECT_TYPE: LinkMessage,
    LINK_MESSAGE_ACCEPTED_OBJECT_TYPE: LinkMessageAccepted,
    LINK_MESSAGE_BOUNCED_OBJECT_TYPE: LinkMessageBounced,
}

# Design doc §12: "bounded storage/bandwidth ... at once" --
# per-recipient, so one recipient's abandoned/never-collected mail can't
# starve every other recipient this node also relays for.
MAX_MAILBOX_ENVELOPES_PER_RECIPIENT = 50

# Issue #891 (design doc §8.5): how long an envelope may wait here for its
# recipient to collect it. Without a limit, a recipient node that never
# comes back -- retired, reinstalled under a new key, gone -- held its
# `MAX_MAILBOX_ENVELOPES_PER_RECIPIENT` slots forever and every later
# deposit for it was refused. Dropping an expired envelope is silent toward
# both ends: the relay can neither read nor sign anything for the recipient,
# and the sender learns of the loss from its own timeout on relay handoffs
# (issue #874, 14 days after the handoff). This has to stay comfortably
# longer than that timeout, so a letter the sender has not yet given up on
# is never the one dropped here -- which is why it is a constant, not a
# SysOp setting like `max_relay_clients`: a relay set to a week would turn
# the sender's "may not have arrived" into "certainly did not".
RELAY_MAILBOX_RETENTION_DAYS = 30


class RelayMailboxFullError(Exception):
    """Raised when depositing would push a recipient's held mail past
    `MAX_MAILBOX_ENVELOPES_PER_RECIPIENT`."""


def deposit_relay_mailbox_envelope(
    db: Database, recipient_fingerprint: str, message: RelayableEnvelope
) -> None:
    """
    Store one opaque `link_message`/`link_message_accepted`/`link_
    message_bounced` for `recipient_fingerprint` to pick up next time it
    dials this relay (`pickup_relay_mailbox_envelopes`). Idempotent on
    `content_id` (a resend deposits nothing new, same `ON CONFLICT ...
    DO NOTHING` shape `netbbs.link.store.save_event` already uses).

    Raises `RelayMailboxFullError` if `recipient_fingerprint` already
    holds `MAX_MAILBOX_ENVELOPES_PER_RECIPIENT` envelopes — the caller
    (`netbbs.link.transport`'s deposit route) is responsible for
    surfacing that as a clear rejection, never a silently dropped
    message (CLAUDE.md's "fail clearly" principle).
    """
    existing = db.connection.execute(
        "SELECT 1 FROM link_relay_mailbox WHERE content_id = ?", (message.content_id,)
    ).fetchone()
    if existing is not None:
        return

    count = db.connection.execute(
        "SELECT COUNT(*) AS n FROM link_relay_mailbox WHERE recipient_fingerprint = ?", (recipient_fingerprint,)
    ).fetchone()["n"]
    if count >= MAX_MAILBOX_ENVELOPES_PER_RECIPIENT:
        raise RelayMailboxFullError(
            f"{recipient_fingerprint} already has {count} envelopes held at this relay -- refusing to "
            "deposit more"
        )

    object_type = message.envelope["object_type"]
    # `link_message`'s own sender is a *local user* on the sender's home
    # node (`payload.sender.home_node_fingerprint`); `link_message_
    # accepted`/`_bounced` have no such field -- their signer *is* the
    # node identified by `payload.recipient_node_fingerprint` (the
    # original message's recipient, now acknowledging it). Diagnostic
    # bookkeeping only (this module never verifies either), but worth
    # getting right for the same reason `mailbox_holdings` exists at all:
    # a SysOp reading this later shouldn't see "unknown" for every ack.
    if object_type == LINK_MESSAGE_OBJECT_TYPE:
        sender_fingerprint = message.payload.get("sender", {}).get("home_node_fingerprint", "unknown")
    else:
        sender_fingerprint = message.payload.get("recipient_node_fingerprint", "unknown")
    db.connection.execute(
        """
        INSERT INTO link_relay_mailbox
            (content_id, recipient_fingerprint, sender_fingerprint, object_type, envelope_json, received_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(content_id) DO NOTHING
        """,
        (
            message.content_id,
            recipient_fingerprint,
            sender_fingerprint,
            object_type,
            json.dumps(message.to_dict()),
            utc_now_iso(),
        ),
    )
    db.connection.commit()


@dataclass(frozen=True)
class RelayMailboxHolding:
    """What this relay holds for one recipient: how many envelopes, and when
    the oldest of them was deposited (`utc_now_iso` form)."""

    recipient_fingerprint: str
    count: int
    oldest_received_at: str


def mailbox_holdings(db: Database) -> list[RelayMailboxHolding]:
    """
    Every recipient this relay is holding mail for, with its count and
    oldest deposit, oldest first -- the recipient closest to losing mail to
    `RELAY_MAILBOX_RETENTION_DAYS` leads. A non-destructive peek, unlike
    `pickup_relay_mailbox_envelopes` below (which reads *and deletes*) --
    the SysOp Link-status screen (issues #60, #891) is the only caller, and
    status visibility must never itself empty the mailbox it's reporting on.
    """
    return [
        RelayMailboxHolding(row["recipient_fingerprint"], row["n"], row["oldest"])
        for row in db.connection.execute(
            "SELECT recipient_fingerprint, COUNT(*) AS n, MIN(received_at) AS oldest "
            "FROM link_relay_mailbox GROUP BY recipient_fingerprint ORDER BY oldest, recipient_fingerprint"
        )
    ]


def prune_expired_relay_mailbox_envelopes(db: Database, *, now: datetime | None = None) -> dict[str, int]:
    """
    Delete every envelope deposited more than `RELAY_MAILBOX_RETENTION_DAYS`
    ago, whatever its type -- an acknowledgement (`link_message_accepted`/
    `_bounced`) left for a node that never collects it is as abandoned as a
    letter. Returns recipient_fingerprint -> how many were dropped for it,
    empty when nothing had expired, so the caller can say what went.

    Cheap enough to run every sync pass: the table is bounded by
    `MAX_MAILBOX_ENVELOPES_PER_RECIPIENT` per recipient, and on the usual
    pass nothing has expired and nothing is written. `received_at` is always
    this node's own `utc_now_iso()`, so comparing it as text against a
    cutoff in the same form orders correctly.
    """
    moment = now or datetime.now(timezone.utc)
    cutoff = utc_iso(moment - timedelta(days=RELAY_MAILBOX_RETENTION_DAYS))
    dropped = {
        row["recipient_fingerprint"]: row["n"]
        for row in db.connection.execute(
            "SELECT recipient_fingerprint, COUNT(*) AS n FROM link_relay_mailbox "
            "WHERE received_at < ? GROUP BY recipient_fingerprint",
            (cutoff,),
        )
    }
    if dropped:
        db.connection.execute("DELETE FROM link_relay_mailbox WHERE received_at < ?", (cutoff,))
        db.connection.commit()
    return dropped


def pickup_relay_mailbox_envelopes(db: Database, recipient_fingerprint: str) -> list[RelayableEnvelope]:
    """
    Return every envelope currently held for `recipient_fingerprint`,
    oldest first, and delete them from this relay's own storage --
    design doc §12: "picked up and deleted the next time that recipient
    dials this relay." A crash between this read and the caller actually
    delivering what it returns loses at most the held mail, never
    duplicates or corrupts it — matching this project's declared scale
    (§14): no outbox-style two-phase handoff is built for this narrow a
    loss window.

    Returns raw, **not yet verified** `LinkMessage`/`LinkMessageAccepted`/
    `LinkMessageBounced` objects, reconstructed by each row's own stored
    `object_type` -- this module has no idea whether the recipient even
    has a completed hello with each one's claimed sender/signer, let
    alone a currently-valid signing key to check it against (both are
    the recipient's own state, not this relay's). The caller re-runs
    full protocol-level verification via `LinkNode.handle_events` after
    pickup (issue #58 task #25) — exactly the same acceptance rule an
    ordinarily-received event of any of these three types already goes
    through, applied here regardless of which path the bytes physically
    arrived by.
    """
    rows = db.connection.execute(
        "SELECT content_id, object_type, envelope_json FROM link_relay_mailbox "
        "WHERE recipient_fingerprint = ? ORDER BY received_at ASC",
        (recipient_fingerprint,),
    ).fetchall()
    db.connection.execute(
        "DELETE FROM link_relay_mailbox WHERE recipient_fingerprint = ?", (recipient_fingerprint,)
    )
    db.connection.commit()
    return [
        _ENVELOPE_TYPES_BY_OBJECT_TYPE[row["object_type"]].from_dict(json.loads(row["envelope_json"]))
        for row in rows
    ]
