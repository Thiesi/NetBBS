"""
Local-origination and receiving-side bridge for Link messages (design
doc round 93) -- turns a locally-composed message addressed to a remote
`user@node-fingerprint` into a signed `link_message` event, and turns an
accepted incoming `link_message` into a real local mailbox delivery plus
the signed `link_message_accepted`/`link_message_bounced` acknowledgement
queued to send back. **Tier 1 (`tier1_home_node_key`) only this round**
-- design doc round 93's tier-2 finding: nothing here ever selects or
builds a tier-2 message.

Deliberately lives here, not in `netbbs.mail` -- the same one-way-
dependency reasoning `netbbs.link.boards` already established for
`netbbs.boards` (see that module's own docstring): a standalone,
non-Link node must never pull in Phase 3 code just by using its mail
package. `netbbs.mail`'s own quota/eviction logic (`_make_room_if_
needed`/`_hard_delete_or_mark`) is private to that module and correctly
so -- `_make_room_or_bounce` below duplicates that one small piece of
logic rather than reaching into it, matching `netbbs.link.boards`' own
precedent of doing its own direct SQL rather than sharing helpers
across the module boundary.

Every function here is plain and synchronous, `db`-first, matching
`netbbs.link.boards`/`netbbs.link.store`'s own calling convention --
dispatched via `DatabaseLane.run` from async call sites. Composing an
outbound message resolves the recipient's current signing key directly
from the persisted `link_peers` table (round 120), never from a live
`LinkNode` -- unlike linking a board, composing a message never mutates
`LinkNode` state, so there is no event-loop-only step here at all.
"""

from __future__ import annotations

import base64
import json

import nacl.signing

import netbbs.mail as mail_module
from netbbs.auth.users import AuthError, User, get_user_by_username
from netbbs.identity.addressing import AddressError, parse_address
from netbbs.identity.encryption import EncryptionError, decrypt_with, encrypt_for
from netbbs.link.events import (
    KeyTransition,
    LinkMessage,
    LinkMessageAccepted,
    LinkMessageBounced,
    build_link_message,
    build_link_message_accepted,
    build_link_message_bounced,
)
from netbbs.link.node_identity import NodeIdentity, resolve_current_operational_key
from netbbs.link.work_items import KIND_LINK_MAIL_ACK, KIND_LINK_MAIL_DELIVERY, enqueue_work_item_without_commit
from netbbs.mail import MailError
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


class LinkMailError(Exception):
    """Raised for a Link-mail-specific composition failure: a malformed
    `user@node-fingerprint` address, or an address for a node this node
    has never exchanged a hello with (no signing key on file to encrypt
    to yet) -- the same "no relay from a stranger" boundary applied to
    composing, not just receiving."""


def compose_link_message(
    db: Database,
    sender: User,
    recipient_address: str,
    subject: str,
    body: str,
    *,
    node_identity: NodeIdentity,
) -> LinkMessage:
    """
    Build, sign, encrypt, and queue one outbound `link_message`
    addressed to `recipient_address`.

    Always encrypts to the *recipient's home node's* derived key
    (`netbbs.identity.encryption`, tier 1 only this round), resolved
    from this node's own persisted `link_peers` row for that
    fingerprint. Raises `LinkMailError` if that fingerprint has never
    exchanged a hello with this node (nothing on file to encrypt to).
    """
    try:
        address = parse_address(recipient_address)
    except AddressError as exc:
        raise LinkMailError(str(exc)) from exc

    subject = subject.strip()
    if not subject:
        raise MailError("subject cannot be blank")
    subject_bytes = len(subject.encode("utf-8"))
    if subject_bytes > mail_module.MAX_MAIL_SUBJECT_BYTES:
        raise MailError(f"subject cannot exceed {mail_module.MAX_MAIL_SUBJECT_BYTES} bytes, got {subject_bytes}")
    body_bytes = len(body.encode("utf-8"))
    if body_bytes > mail_module.MAX_MAIL_BODY_BYTES:
        raise MailError(f"body cannot exceed {mail_module.MAX_MAIL_BODY_BYTES} bytes, got {body_bytes}")

    recipient_signing_verify_key = _resolve_peer_signing_key(db, address.node_fingerprint)

    plaintext = json.dumps({"subject": subject, "body": body}).encode("utf-8")
    ciphertext = encrypt_for(recipient_signing_verify_key, plaintext)

    created_at = utc_now_iso()
    message = build_link_message(
        signing_identity=node_identity.signing_key,
        home_node_fingerprint=node_identity.fingerprint,
        local_user_id=sender.username,
        recipient_home_node_fingerprint=address.node_fingerprint,
        recipient_local_user_id=address.user,
        confidentiality_tier="tier1_home_node_key",
        ciphertext=ciphertext,
        created_at=created_at,
    )

    db.connection.execute(
        """
        INSERT INTO mail_messages
            (sender_user_id, sender_label, recipient_remote_address, subject, body,
             created_at, link_event_json, link_event_content_id, link_delivery_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        """,
        (
            sender.id, sender.username, str(address), subject, body, created_at,
            json.dumps(message.to_dict()), message.content_id,
        ),
    )
    # Same transaction as the insert above (design doc §13.7): a crash
    # between the two must never leave a message with no work item ever
    # tracking its delivery.
    enqueue_work_item_without_commit(
        db, kind=KIND_LINK_MAIL_DELIVERY, reference_id=message.content_id,
        target_fingerprint=address.node_fingerprint,
    )
    db.connection.commit()

    return message


def _resolve_peer_signing_key(db: Database, node_fingerprint: str) -> nacl.signing.VerifyKey:
    """The current signing verify key on file for `node_fingerprint`,
    read directly from the persisted `link_peers` table (round 120) --
    never the live `LinkNode`, since this is the one Link-mail operation
    that doesn't need it (see module docstring)."""
    peer_row = db.connection.execute(
        "SELECT root_public_key, transitions_json FROM link_peers WHERE fingerprint = ?",
        (node_fingerprint,),
    ).fetchone()
    if peer_row is None:
        raise LinkMailError(
            f"this node has never exchanged a hello with {node_fingerprint!r} yet -- "
            "cannot compose a message to it"
        )
    root_verify_key = nacl.signing.VerifyKey(base64.b64decode(peer_row["root_public_key"]))
    transitions = tuple(KeyTransition.from_dict(t) for t in json.loads(peer_row["transitions_json"]))
    signing_key_b64 = resolve_current_operational_key(
        transitions, root_verify_key=root_verify_key,
        subject_fingerprint=node_fingerprint, purpose="signing",
    )
    if signing_key_b64 is None:
        raise LinkMailError(
            f"{node_fingerprint!r} has no currently-authorized signing key -- cannot "
            "compose a message to it"
        )
    return nacl.signing.VerifyKey(base64.b64decode(signing_key_b64))


def deliver_link_message(
    db: Database, raw_message: dict, *, node_identity: NodeIdentity
) -> LinkMessageAccepted | LinkMessageBounced:
    """
    Called once for each `link_message` `LinkNode.handle_events` newly
    accepted (the same division `LinkServer._handle_events` already
    uses for board events -- protocol-layer acceptance stays pure/
    in-memory, actual persistence/delivery happens here, off the event
    loop via a `DatabaseLane`).

    Decrypts, resolves the local recipient, and either delivers into
    their mailbox (queuing a `link_message_accepted` acknowledgement) or
    queues a `link_message_bounced` one -- never both, never silence.
    `handle_events` has already confirmed this message is addressed to
    this node's own fingerprint; it has no way to know whether `local_
    user_id` actually names a real local account, which is this
    function's first job.
    """
    message = LinkMessage.from_dict(raw_message)
    sender_info = message.payload["sender"]
    sender_address = f"{sender_info['local_user_id']}@{sender_info['home_node_fingerprint']}"
    recipient_local_user_id = message.payload["recipient"]["local_user_id"]
    origin_node_fingerprint = sender_info["home_node_fingerprint"]

    def _bounce(reason: str) -> LinkMessageBounced:
        bounced = build_link_message_bounced(
            signing_identity=node_identity.signing_key,
            recipient_node_fingerprint=node_identity.fingerprint,
            message_content_id=message.content_id,
            reason=reason,
            created_at=utc_now_iso(),
        )
        _queue_acknowledgement(db, bounced, target_node_fingerprint=origin_node_fingerprint)
        return bounced

    try:
        recipient = get_user_by_username(db, recipient_local_user_id)
    except AuthError:
        return _bounce("unknown_recipient")

    try:
        ciphertext = base64.b64decode(message.payload["ciphertext"])
        plaintext = decrypt_with(node_identity.signing_key, ciphertext)
        decoded = json.loads(plaintext)
    except EncryptionError:
        # handle_events already confirmed this node is the named
        # recipient -- a decryption failure here means the ciphertext
        # itself is malformed/corrupted, not a routing mistake.
        return _bounce("unknown_recipient")

    if _make_room_or_report_full(db, recipient):
        return _bounce("mailbox_full")

    db.connection.execute(
        """
        INSERT INTO mail_messages
            (sender_user_id, sender_label, recipient_user_id, subject, body, created_at, link_source_event_id)
        VALUES (NULL, ?, ?, ?, ?, ?, ?)
        """,
        (sender_address, recipient.id, decoded["subject"], decoded["body"], utc_now_iso(), message.content_id),
    )
    db.connection.commit()

    accepted = build_link_message_accepted(
        signing_identity=node_identity.signing_key,
        recipient_node_fingerprint=node_identity.fingerprint,
        message_content_id=message.content_id,
        created_at=utc_now_iso(),
    )
    _queue_acknowledgement(db, accepted, target_node_fingerprint=origin_node_fingerprint)
    return accepted


def _make_room_or_report_full(db: Database, recipient: User) -> bool:
    """Mirrors `netbbs.mail`'s own `MAX_MAIL_PER_RECIPIENT` quota rule
    exactly -- evicts the oldest already-read message to make room and
    returns `False`, or returns `True` (the caller should bounce rather
    than deliver) only when the inbox is at cap *and* every message in
    it is still unread, the same "never silently drop something unread"
    rule `netbbs.mail.MailboxFullError` enforces locally, applied here
    as a bounce instead of a raised exception since there is no
    synchronous caller to catch it."""
    count = db.connection.execute(
        "SELECT COUNT(*) AS n FROM mail_messages WHERE recipient_user_id = ? AND recipient_deleted_at IS NULL",
        (recipient.id,),
    ).fetchone()["n"]
    if count < mail_module.MAX_MAIL_PER_RECIPIENT:
        return False

    oldest_read = db.connection.execute(
        """
        SELECT id, sender_deleted_at FROM mail_messages
        WHERE recipient_user_id = ? AND recipient_deleted_at IS NULL AND read_at IS NOT NULL
        ORDER BY created_at ASC LIMIT 1
        """,
        (recipient.id,),
    ).fetchone()
    if oldest_read is None:
        return True

    if oldest_read["sender_deleted_at"] is not None:
        db.connection.execute("DELETE FROM mail_messages WHERE id = ?", (oldest_read["id"],))
    else:
        db.connection.execute(
            "UPDATE mail_messages SET recipient_deleted_at = ? WHERE id = ?",
            (utc_now_iso(), oldest_read["id"]),
        )
    db.connection.commit()
    return False


def _queue_acknowledgement(
    db: Database, ack: LinkMessageAccepted | LinkMessageBounced, *, target_node_fingerprint: str
) -> None:
    cursor = db.connection.execute(
        """
        INSERT INTO link_mail_acknowledgements
            (message_content_id, target_node_fingerprint, ack_event_json, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (ack.payload["message_content_id"], target_node_fingerprint, json.dumps(ack.to_dict()), utc_now_iso()),
    )
    # Same transaction as the insert above -- see compose_link_message's
    # identical reasoning. reference_id is this row's own id: acks have
    # no content-addressed id of their own to point at instead.
    enqueue_work_item_without_commit(
        db, kind=KIND_LINK_MAIL_ACK, reference_id=str(cursor.lastrowid),
        target_fingerprint=target_node_fingerprint,
    )
    db.connection.commit()


def apply_link_message_accepted(db: Database, raw_ack: dict) -> None:
    """Marks the originating outbound row `delivered` once this node
    receives the corresponding `link_message_accepted` back. A no-op if
    no matching row is found (`link_event_content_id` unrecognized) --
    already-seen dedup at the protocol layer means this should only ever
    be called once per genuinely new acknowledgement, but a missing row
    is a quiet no-op here rather than an error, matching `netbbs.mail.
    mark_read`'s own no-op-if-unchanged shape for an unexpected but
    harmless case."""
    accepted = LinkMessageAccepted.from_dict(raw_ack)
    _set_delivery_status(db, accepted.payload["message_content_id"], "delivered")


def apply_link_message_bounced(db: Database, raw_ack: dict) -> None:
    """Counterpart to `apply_link_message_accepted` for a `link_message_
    bounced` acknowledgement."""
    bounced = LinkMessageBounced.from_dict(raw_ack)
    _set_delivery_status(db, bounced.payload["message_content_id"], "bounced")


def _set_delivery_status(db: Database, message_content_id: str, status: str) -> None:
    db.connection.execute(
        "UPDATE mail_messages SET link_delivery_status = ? WHERE link_event_content_id = ?",
        (status, message_content_id),
    )
    db.connection.commit()


# -- work-item-driven delivery (design doc §13.7, issue #60's second -------
# -- operational slice) -- replaces this module's old "load every pending --
# -- row, resend unconditionally every pass, no cap" functions. -----------
#
# `link_mail_acknowledgements.sent_at` is no longer read or written by
# anything (`netbbs.link.work_items`' own status now tracks this) -- left
# in the schema rather than dropped in a follow-up migration purely for
# this round's own churn budget; it's dead, not harmful.


def get_link_message_for_delivery(db: Database, content_id: str) -> tuple[LinkMessage, str] | None:
    """The `LinkMessage` and current `link_delivery_status` for a due
    work item's `reference_id` -- `None` if the row is somehow gone (it
    never should be; `mail_messages` rows are never deleted by this
    delivery path), which the caller (`netbbs.link.sync`) tolerates as
    "nothing left to push" rather than treating as an error."""
    row = db.connection.execute(
        "SELECT link_event_json, link_delivery_status FROM mail_messages WHERE link_event_content_id = ?",
        (content_id,),
    ).fetchone()
    if row is None:
        return None
    return LinkMessage.from_dict(json.loads(row["link_event_json"])), row["link_delivery_status"]


def get_link_mail_acknowledgement(db: Database, ack_id: str) -> LinkMessageAccepted | LinkMessageBounced | None:
    """The acknowledgement a due `link_mail_ack` work item's
    `reference_id` (this table's own `id`, as text) points at -- `None`
    if somehow gone, tolerated the same way as `get_link_message_for_
    delivery`."""
    row = db.connection.execute(
        "SELECT ack_event_json FROM link_mail_acknowledgements WHERE id = ?", (int(ack_id),)
    ).fetchone()
    if row is None:
        return None
    raw = json.loads(row["ack_event_json"])
    if raw["envelope"]["object_type"] == "link_message_accepted":
        return LinkMessageAccepted.from_dict(raw)
    return LinkMessageBounced.from_dict(raw)


def expire_link_message_delivery(db: Database, content_id: str) -> None:
    """Called when a `link_mail_delivery` work item dead-letters or is
    cancelled -- the payload could never even be successfully pushed
    (or a SysOp gave up on it), so this finally gives `mail_messages.
    link_delivery_status`'s long-reserved `'expired'` value (round 93)
    a real producer. Guarded on the row still being `'pending'`: a
    genuine accepted/bounced event racing in first (this node's own
    push actually did succeed, just not yet reflected in the work item)
    must win, never be overwritten by a stale dead-letter outcome."""
    db.connection.execute(
        "UPDATE mail_messages SET link_delivery_status = 'expired' "
        "WHERE link_event_content_id = ? AND link_delivery_status = 'pending'",
        (content_id,),
    )
    db.connection.commit()


def unexpire_link_message_delivery(db: Database, content_id: str) -> None:
    """The other half of `expire_link_message_delivery`, called when a
    SysOp replays a dead-lettered/cancelled `link_mail_delivery` work
    item (`netbbs.link.work_items.replay_work_item`) -- undoes the
    expiry so the next successful push can still lead to a genuine
    accepted/bounced resolution, rather than the message staying
    permanently `'expired'` even though delivery is being retried again."""
    db.connection.execute(
        "UPDATE mail_messages SET link_delivery_status = 'pending' "
        "WHERE link_event_content_id = ? AND link_delivery_status = 'expired'",
        (content_id,),
    )
    db.connection.commit()
