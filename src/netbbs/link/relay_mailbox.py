"""
Bounded relay store-and-forward mailbox (design doc §12 round 95, issue
#58) -- a relay only ever custodies opaque, already-encrypted `link_
message` envelopes addressed to a fingerprint it has granted `i_relay_
for` consent to (`netbbs.link.protocol.LinkNode.relaying_for`), bounded
per recipient so a careless or hostile depositor can't grow one
recipient's held mail without limit (CLAUDE.md's own "bound remotely
influenced resources" principle, applied here the same way `netbbs.
link.protocol`'s own `_MAX_PEER_LIST_ENTRIES_PER_REQUEST`/`_MAX_
CANDIDATE_DESCRIPTORS` bound peer-list state).

Deliberately narrower than every `link_message`-family event this round
-- only `link_message` itself is deposit-able via a relay; routing
`link_message_accepted`/`link_message_bounced` back through a relay too
is a known, not yet built, follow-up (this codebase's recurring "known
boundary, not a gap found late" framing — see e.g. `BoardOriginTransfer
Offer`'s own docstring for the same shape of note), not attempted here.

This module never verifies a deposited envelope's signature or reads
its plaintext -- it can't (the ciphertext is sealed to the recipient's
own key, design doc §12's own "never content" limitation) and doesn't
need to (the recipient re-runs full protocol-level verification via
`netbbs.link.protocol.LinkNode.handle_events` after pickup, exactly the
same way it already verifies anything else — see `netbbs.link.sync`'s
own relay-pickup wiring, issue #58 task #25, for where that happens).
`deposit_relay_mailbox_envelope` only checks the *shape* is a well-
formed `link_message` (a signature it can't validate, but a payload
whose fields it can at least parse), so it isn't storing arbitrary
non-Link garbage.

Plain, synchronous, `db`-first functions dispatched via `DatabaseLane.
run`, same convention as `netbbs.link.store`/`netbbs.link.reliability`.
"""

from __future__ import annotations

import json

from netbbs.link.events import LINK_MESSAGE_OBJECT_TYPE, LinkMessage
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

# Design doc §12 round 95: "bounded storage/bandwidth ... at once" --
# per-recipient, so one recipient's abandoned/never-collected mail can't
# starve every other recipient this node also relays for.
MAX_MAILBOX_ENVELOPES_PER_RECIPIENT = 50


class RelayMailboxFullError(Exception):
    """Raised when depositing would push a recipient's held mail past
    `MAX_MAILBOX_ENVELOPES_PER_RECIPIENT`."""


def deposit_relay_mailbox_envelope(db: Database, recipient_fingerprint: str, message: LinkMessage) -> None:
    """
    Store one opaque `link_message` for `recipient_fingerprint` to pick
    up next time it dials this relay (`pickup_relay_mailbox_envelopes`).
    Idempotent on `content_id` (a resend deposits nothing new, same
    `ON CONFLICT ... DO NOTHING` shape `netbbs.link.store.save_event`
    already uses).

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

    sender_fingerprint = message.payload.get("sender", {}).get("home_node_fingerprint", "unknown")
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
            LINK_MESSAGE_OBJECT_TYPE,
            json.dumps(message.to_dict()),
            utc_now_iso(),
        ),
    )
    db.connection.commit()


def mailbox_sizes(db: Database) -> dict[str, int]:
    """
    recipient_fingerprint -> count of envelopes currently held for them,
    across every recipient this relay is holding mail for. A
    non-destructive peek, unlike `pickup_relay_mailbox_envelopes` below
    (which reads *and deletes*) -- issue #60's SysOp Link-status screen
    is the only caller, and status visibility must never itself empty
    the mailbox it's reporting on.
    """
    return {
        row["recipient_fingerprint"]: row["n"]
        for row in db.connection.execute(
            "SELECT recipient_fingerprint, COUNT(*) AS n FROM link_relay_mailbox GROUP BY recipient_fingerprint"
        )
    }


def pickup_relay_mailbox_envelopes(db: Database, recipient_fingerprint: str) -> list[LinkMessage]:
    """
    Return every envelope currently held for `recipient_fingerprint`,
    oldest first, and delete them from this relay's own storage --
    design doc §12: "picked up and deleted the next time that recipient
    dials this relay." A crash between this read and the caller actually
    delivering what it returns loses at most the held mail, never
    duplicates or corrupts it — matching this project's declared scale
    (§14): no outbox-style two-phase handoff is built for this narrow a
    loss window.

    Returns raw, **not yet verified** `LinkMessage` objects — this
    module has no idea whether the recipient even has a completed hello
    with each one's claimed sender, let alone a currently-valid signing
    key to check it against (both are the recipient's own state, not
    this relay's). The caller re-runs full protocol-level verification
    via `LinkNode.handle_events` after pickup (issue #58 task #25) —
    exactly the same acceptance rule an ordinarily-received `link_
    message` already goes through, applied here regardless of which
    path the bytes physically arrived by.
    """
    rows = db.connection.execute(
        "SELECT content_id, envelope_json FROM link_relay_mailbox "
        "WHERE recipient_fingerprint = ? ORDER BY received_at ASC",
        (recipient_fingerprint,),
    ).fetchall()
    db.connection.execute(
        "DELETE FROM link_relay_mailbox WHERE recipient_fingerprint = ?", (recipient_fingerprint,)
    )
    db.connection.commit()
    return [LinkMessage.from_dict(json.loads(row["envelope_json"])) for row in rows]
