"""
Local-origination and receiving-side bridge for Link messages (design
doc) -- turns a locally-composed message addressed to a remote
`user@node-fingerprint` into a signed `link_message` event, and turns an
accepted incoming `link_message` into a real local mailbox delivery plus
the signed `link_message_accepted`/`link_message_bounced` acknowledgement
queued to send back. **Tier 1 (`tier1_home_node_key`) only**
-- nothing here ever selects or
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
from the persisted `link_peers` table, never from a live
`LinkNode` -- unlike linking a board, composing a message never mutates
`LinkNode` state, so there is no event-loop-only step here at all.

**Issue #69:** because of the above, a composed-but-not-yet-pushed
message does not exist in `LinkNode.events` yet either -- and can't,
since no live node is in scope here at all. `netbbs.link.sync._push_
pending_link_mail` is what actually registers it (`node.events`/`known_
event_ids`, then persisted via `netbbs.link.store.save_event`) at the
one point every caller of `compose_link_message` funnels through before
a `link_message` ever leaves this node -- not here, and not at any
individual call site of this function. Skipping that registration is
exactly the bug #69 fixed: `_resolve_own_link_message` (`netbbs.link.
protocol`) only ever checks `node.events`, so an unregistered message's
own `link_message_accepted`/`_bounced` acknowledgement could never
resolve and was rejected every time, forever.
"""

from __future__ import annotations

import base64
import datetime
import json

import nacl.signing

import netbbs.mail as mail_module
from netbbs.auth.users import AuthError, User, get_user_by_username
from netbbs.identity.addressing import AddressError, is_valid_user_part, parse_address
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
from netbbs.link.node_profiles import identity_for_fingerprint, link_address_label
from netbbs.link.work_items import KIND_LINK_MAIL_ACK, KIND_LINK_MAIL_DELIVERY, enqueue_work_item_without_commit
from netbbs.mail import MailError
from netbbs.rendering.width import cut_to_width
from netbbs.storage.database import Database
from netbbs.timeutil import parse_utc_iso, utc_iso, utc_now_iso


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
    (`netbbs.identity.encryption`, tier 1 only), resolved
    from this node's own persisted `link_peers` row for that
    fingerprint. Raises `LinkMailError` if that fingerprint has never
    exchanged a hello with this node (nothing on file to encrypt to).
    """
    try:
        address = parse_address(recipient_address)
    except AddressError as exc:
        raise LinkMailError(str(exc)) from exc
    # The sender's name goes out as the user half of its address, which the
    # recipient reads and replies to (issue #807). A name the address
    # grammar refuses -- only an account older than the username rules can
    # hold one -- would be an address nobody could type back.
    if not is_valid_user_part(sender.username):
        raise LinkMailError(
            f"your user name {sender.username!r} cannot be written as a Link address, so "
            "a reply could never reach you. Ask the SysOp to rename the account."
        )

    subject = mail_module.validate_mail_fields(subject, body)

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
    read directly from the persisted `link_peers` table --
    never the live `LinkNode`, since this is the one Link-mail operation
    that doesn't need it (see module docstring)."""
    peer_row = db.connection.execute(
        "SELECT root_public_key, transitions_json FROM link_peers WHERE fingerprint = ?",
        (node_fingerprint,),
    ).fetchone()
    if peer_row is None:
        raise LinkMailError(
            f"this BBS is not linked with {node_fingerprint} yet. Address a node it is "
            "linked with, or ask the SysOp to link with that one."
        )
    root_verify_key = nacl.signing.VerifyKey(base64.b64decode(peer_row["root_public_key"]))
    transitions = tuple(KeyTransition.from_dict(t) for t in json.loads(peer_row["transitions_json"]))
    signing_key_b64 = resolve_current_operational_key(
        transitions, root_verify_key=root_verify_key,
        subject_fingerprint=node_fingerprint, purpose="signing",
    )
    if signing_key_b64 is None:
        raise LinkMailError(
            f"the keys on file for {node_fingerprint} are no longer valid. Try again once "
            "that node has linked with this BBS again."
        )
    return nacl.signing.VerifyKey(base64.b64decode(signing_key_b64))


def _open_sealed(node_identity: NodeIdentity, ciphertext: bytes) -> bytes:
    """Open mail sealed to this node's current signing key or a retired one.

    A sender seals to the key it last learned, so a message composed before
    it heard of a rotation arrives sealed to the key that rotation retired
    (issue #624). Newest first, since that is the likely one.
    """
    for key in (node_identity.signing_key, *reversed(node_identity.retired_signing_keys)):
        try:
            return decrypt_with(key, ciphertext)
        except EncryptionError:
            continue
    raise EncryptionError("sealed to none of this node's signing keys")


# How far ahead of this node's clock a letter's signed `created_at` may be
# and still be believed (issue #808): ordinary clock drift between two
# nodes, the same five minutes Link's signed requests allow. Anything
# later is dated by its arrival instead.
MAX_LINK_MAIL_CLOCK_SKEW = datetime.timedelta(minutes=5)
# The earliest date a received letter may claim. No NetBBS node wrote mail
# before this, and a date near year 1 cannot be shown in a timezone west of
# UTC (`format_for_display` runs off the representable range), which would
# take the whole inbox down (#878 review).
EARLIEST_LINK_MAIL_DATE = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)


def _written_at(created_at: object, arrived_at: str) -> str:
    """When a received letter was written, for its Date: line (issue #808).

    The sender's signed `created_at`, so a letter that took days to arrive
    says so rather than looking new. One dated beyond the clock-skew
    allowance, before `EARLIEST_LINK_MAIL_DATE`, or not a timestamp at all,
    is dated by its arrival instead: the sender's clock cannot be trusted
    to put a letter in the future or in an age no display can show.
    Mailbox order does not depend on this; it is arrival order."""
    if not isinstance(created_at, str):
        return arrived_at
    try:
        written = parse_utc_iso(created_at)
    except ValueError:
        return arrived_at
    if written < EARLIEST_LINK_MAIL_DATE or written > parse_utc_iso(arrived_at) + MAX_LINK_MAIL_CLOCK_SKEW:
        return arrived_at
    return utc_iso(written)


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

    Everything the sender's node chose is checked here, where it enters
    (issue #808): the sender's `local_user_id` against the address
    grammar, since it becomes the address a reply goes to, and the
    decrypted subject and body against the limits local mail keeps. A
    letter that fails is bounced `malformed`; one that cannot be opened
    at all is bounced `undecryptable`. Neither is ever dropped silently:
    the envelope is already stored and known by now, so an exception
    here would lose the letter without a word to anyone.
    """
    message = LinkMessage.from_dict(raw_message)
    sender_info = message.payload["sender"]
    sender_user = sender_info.get("local_user_id")
    recipient_local_user_id = message.payload["recipient"].get("local_user_id")
    origin_node_fingerprint = sender_info["home_node_fingerprint"]

    def _bounce(reason: str) -> LinkMessageBounced:
        return bounce_link_message(db, raw_message, reason, node_identity=node_identity)

    # A NetBBS node only ever sends a name its own username rules allow,
    # which is this grammar; an account older than those rules on a node
    # older than #807 is the one honest source of anything else, and a
    # reply to it could never be addressed anyway.
    if not is_valid_user_part(sender_user) or not isinstance(recipient_local_user_id, str):
        return _bounce("malformed")
    sender_address = f"{sender_user}@{origin_node_fingerprint}"

    try:
        recipient = get_user_by_username(db, recipient_local_user_id)
    except AuthError:
        return _bounce("unknown_recipient")
    # The shared guest account has no mailbox (issue #816): every guest
    # caller would read what arrived there.
    if mail_module.mail_recipient_refusal(db, recipient) is not None:
        return _bounce("no_mailbox")
    # The recipient blocked this sender (issue #817), by the address the
    # letter came from, never by the node's changeable display name.
    if mail_module.mail_sender_refusal(db, recipient, sender_address=sender_address) is not None:
        return _bounce("blocked_by_recipient")

    try:
        ciphertext = base64.b64decode(message.payload["ciphertext"], validate=True)
        plaintext = _open_sealed(node_identity, ciphertext)
    except (EncryptionError, KeyError, TypeError, ValueError):
        # handle_events already confirmed this node is the named
        # recipient -- a decryption failure here means the ciphertext
        # was sealed to a key this node never held, or is damaged; it is
        # not a routing mistake, so it is not `unknown_recipient`.
        return _bounce("undecryptable")

    try:
        decoded = json.loads(plaintext)
        subject = mail_module.validate_mail_fields(decoded["subject"], decoded["body"])
        body = decoded["body"]
    except (MailError, KeyError, TypeError, ValueError):
        return _bounce("malformed")

    if _make_room_or_report_full(db, recipient):
        return _bounce("mailbox_full")

    arrived_at = utc_now_iso()
    db.connection.execute(
        """
        INSERT INTO mail_messages
            (sender_user_id, sender_label, recipient_user_id, subject, body, created_at, link_source_event_id)
        VALUES (NULL, ?, ?, ?, ?, ?, ?)
        """,
        (
            sender_address, recipient.id, subject, body,
            _written_at(message.payload.get("created_at"), arrived_at), message.content_id,
        ),
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


def bounce_link_message(
    db: Database, raw_message: dict, reason: str, *, node_identity: NodeIdentity
) -> LinkMessageBounced:
    """Answer an accepted incoming `link_message` with a signed bounce
    instead of delivering it, queued back to the sender's home node.

    `deliver_link_message` bounces this way for its own reasons; trust
    policy calls it directly with `"blocked_sender"` for mail that arrived
    by a path with no synchronous answer, a relay mailbox pickup (issue
    #804)."""
    message = LinkMessage.from_dict(raw_message)
    bounced = build_link_message_bounced(
        signing_identity=node_identity.signing_key,
        recipient_node_fingerprint=node_identity.fingerprint,
        message_content_id=message.content_id,
        reason=reason,
        created_at=utc_now_iso(),
    )
    _queue_acknowledgement(
        db, bounced, target_node_fingerprint=message.payload["sender"]["home_node_fingerprint"]
    )
    return bounced


def _make_room_or_report_full(db: Database, recipient: User) -> bool:
    """`netbbs.mail.make_room`, the one quota rule local delivery uses
    too: evicts the oldest already-read message (a system notice first,
    issue #819) and returns `False`, or returns `True` (the caller should
    bounce rather than deliver) only when the inbox is at cap *and* every
    message in it is still unread -- the "never silently drop something
    unread" rule `netbbs.mail.MailboxFullError` enforces locally, applied
    here as a bounce since there is no synchronous caller to catch it."""
    return not mail_module.make_room(db, recipient)


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
    # Delivered after all (a bounce or an expiry can only have come first
    # through a replay): nothing is left to tell the sender about.
    db.connection.execute(
        "UPDATE mail_messages SET link_delivery_status = 'delivered', link_delivery_reason = NULL, "
        "link_delivery_notice_pending = 0 WHERE link_event_content_id = ?",
        (accepted.payload["message_content_id"],),
    )
    db.connection.commit()


def apply_link_message_bounced(db: Database, raw_ack: dict) -> None:
    """Counterpart to `apply_link_message_accepted` for a `link_message_
    bounced` acknowledgement. Keeps the recipient node's reason and flags
    the message for its sender's next main menu (issue #806)."""
    bounced = LinkMessageBounced.from_dict(raw_ack)
    _record_bounce(db, bounced.payload["message_content_id"], bounced.payload["reason"], only_pending=False)


def record_link_message_refused(db: Database, message_content_id: str, reason_code: str) -> None:
    """The recipient's node answered this node's push of an outbound
    `link_message` with a trust-policy refusal (HTTP 403 carrying a
    `link_policy_*` reason code, issue #804): it holds this node or the
    sending user on probation, in quarantine or blocked. That answer is
    final for this message, so it is a bounce, not a failure to retry.

    Only a still-`pending` row changes. `reason_code` is the recipient's
    `netbbs.link.enforcement` reason, kept for the Sent screen (issue
    #806)."""
    _record_bounce(db, message_content_id, reason_code, only_pending=True)


# A reason code comes from another node: kept only up to this length. Every
# code NetBBS defines is far shorter.
_MAX_REASON_LENGTH = 64


def _record_bounce(db: Database, message_content_id: str, reason: str, *, only_pending: bool) -> None:
    # A message that was already bounced is not told about twice; one that
    # had expired is, since the bounce is the first real answer.
    db.connection.execute(
        "UPDATE mail_messages SET link_delivery_status = 'bounced', link_delivery_reason = ?, "
        "link_delivery_notice_pending = CASE WHEN link_delivery_status = 'bounced' "
        "THEN link_delivery_notice_pending ELSE 1 END "
        "WHERE link_event_content_id = ?"
        + (" AND link_delivery_status = 'pending'" if only_pending else ""),
        (str(reason)[:_MAX_REASON_LENGTH], message_content_id),
    )
    db.connection.commit()


# -- work-item-driven delivery (design doc §13.7, issue #60's second -------
# -- operational slice) -- replaces this module's old "load every pending --
# -- row, resend unconditionally every pass, no cap" functions. -----------
#
# `link_mail_acknowledgements.sent_at` is no longer read or written by
# anything (`netbbs.link.work_items`' own status now tracks this) -- left
# in the schema rather than dropped in a follow-up migration purely to
# avoid unrelated churn; it's dead, not harmful.


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


def expire_link_message_delivery(db: Database, content_id: str, *, reason: str | None = None) -> None:
    """Called when a `link_mail_delivery` work item dead-letters or is
    cancelled -- the payload could never even be successfully pushed
    (or a SysOp gave up on it), so this finally gives `mail_messages.
    link_delivery_status`'s long-reserved `'expired'` value
    a real producer. Guarded on the row still being `'pending'`: a
    genuine accepted/bounced event racing in first (this node's own
    push actually did succeed, just not yet reflected in the work item)
    must win, never be overwritten by a stale dead-letter outcome.

    `reason` is `EXPIRED_BY_OWN_POLICY` when this node's own trust policy
    held the mail back to the end; `None` means no route worked. The
    sender is told either way at their next main menu (issue #806)."""
    db.connection.execute(
        "UPDATE mail_messages SET link_delivery_status = 'expired', link_delivery_reason = ?, "
        "link_delivery_notice_pending = 1 "
        "WHERE link_event_content_id = ? AND link_delivery_status = 'pending'",
        (reason, content_id),
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
        "UPDATE mail_messages SET link_delivery_status = 'pending', link_delivery_reason = NULL, "
        "link_delivery_notice_pending = 0 "
        "WHERE link_event_content_id = ? AND link_delivery_status = 'expired'",
        (content_id,),
    )
    db.connection.commit()


# -- what the sender is told (issue #806) -----------------------------------

DELIVERY_STATUS_LABELS = {
    "pending": "pending",
    "delivered": "delivered",
    "bounced": "bounced",
    "expired": "expired",
}

# Each reason a recipient node can give, in words a caller understands. The
# signed bounce reasons (`netbbs.link.events`) and the trust-policy refusal
# codes (`netbbs.link.enforcement`) share one table; the sender cannot tell
# which route a bounce took and does not need to.
_BOUNCE_REASON_TEXT = {
    "unknown_recipient": "there is no user by that name on that BBS",
    "mailbox_full": "the recipient's mailbox is full of unread mail",
    "blocked_sender": "that BBS does not accept mail from you or from this BBS",
    "blocked_by_recipient": "the recipient does not accept mail from you",
    "undecryptable": "that BBS could not decrypt it, so it may have been sealed to a key that BBS no longer holds",
    "malformed": "that BBS could not accept it as a letter (a bad sender name, subject or body)",
    "no_mailbox": "that account takes no mail (it is the BBS's shared guest account)",
    "link_policy_manual_block": "that BBS has blocked you or this BBS",
    "link_policy_node_quarantined": "that BBS has quarantined this BBS",
    "link_policy_node_probationary_read_only": "that BBS does not trust this BBS yet; its SysOp has to establish it",
    "link_policy_user_quarantined": "that BBS has quarantined your account",
    "link_policy_user_probationary_approval_required": "that BBS does not trust your account yet",
    "link_policy_probation_budget_exceeded": "this BBS is new to that BBS and has sent it as much as it accepts for now",
}
_UNKNOWN_BOUNCE_TEXT = "that BBS refused it"

# The one expiry with a reason of its own: this node's trust policy stopped
# holding the peer as one it sends mail to, and never allowed it again
# before the delivery gave up.
EXPIRED_BY_OWN_POLICY = "own_policy"
_EXPIRED_TEXT = {
    EXPIRED_BY_OWN_POLICY: "this BBS stopped exchanging mail with that BBS before it could be sent",
}
_EXPIRED_NO_ROUTE_TEXT = "no route to that BBS worked before delivery gave up"


def delivery_explanation(status: str | None, reason: str | None) -> str | None:
    """One line saying where a sent Link message stands, for the Sent view
    (issue #806). `None` for local mail, which has no delivery state."""
    if status == "pending":
        return "Pending: that BBS has not confirmed it yet."
    if status == "delivered":
        return "Delivered to the recipient's mailbox."
    if status == "bounced":
        return f"Bounced: {bounce_reason_text(reason)}."
    if status == "expired":
        return f"Expired: {expiry_reason_text(reason)}. It was not delivered."
    return None


def bounce_reason_text(reason: str | None) -> str:
    return _BOUNCE_REASON_TEXT.get(reason or "", _UNKNOWN_BOUNCE_TEXT)


def expiry_reason_text(reason: str | None) -> str:
    return _EXPIRED_TEXT.get(reason or "", _EXPIRED_NO_ROUTE_TEXT)


# How many undelivered messages the main menu names at once; the rest are
# counted in one line (the same bound as moderation notices).
MAX_DELIVERY_NOTICES_SHOWN = 10
# How much of a subject a notice quotes.
_NOTICE_SUBJECT_COLUMNS = 40


def pending_delivery_notices(db: Database, sender: User) -> tuple[list[str], list[int]]:
    """`sender`'s Link mail that bounced or expired since they were last
    told, as notice lines oldest first, and the ids to acknowledge once
    the lines are on screen. Persistent, so a sender who was offline when
    the bounce arrived is told at their next main menu (issue #806).
    Nothing is marked here: a caller who drops before the menu is drawn is
    told next time. Mail the sender deleted from Sent is left out: they are
    done with it, and Sent can no longer show it."""
    rows = db.connection.execute(
        """
        SELECT id, subject, recipient_remote_address, link_delivery_status, link_delivery_reason
        FROM mail_messages
        WHERE sender_user_id = ? AND link_delivery_notice_pending = 1 AND sender_deleted_at IS NULL
        ORDER BY id
        """,
        (sender.id,),
    ).fetchall()
    lines = [_delivery_notice_text(db, row) for row in rows[:MAX_DELIVERY_NOTICES_SHOWN]]
    if len(rows) > MAX_DELIVERY_NOTICES_SHOWN:
        more = len(rows) - MAX_DELIVERY_NOTICES_SHOWN
        lines.append(f"...and {more} more message{'s' if more != 1 else ''} not delivered; see E-mail, Sent.")
    return lines, [row["id"] for row in rows]


def acknowledge_delivery_notices(db: Database, mail_ids: list[int]) -> None:
    """The sender has been told: stop flagging these messages."""
    if not mail_ids:
        return
    db.connection.execute(
        f"UPDATE mail_messages SET link_delivery_notice_pending = 0 WHERE id IN ({','.join('?' * len(mail_ids))})",
        tuple(mail_ids),
    )
    db.connection.commit()


def _delivery_notice_text(db: Database, row) -> str:
    subject = row["subject"]
    cut = cut_to_width(subject, _NOTICE_SUBJECT_COLUMNS)
    subject = subject if cut == subject else cut.rstrip() + "..."
    address = row["recipient_remote_address"] or ""
    user, separator, fingerprint = address.rpartition("@")
    to_label = (
        link_address_label(user, identity_for_fingerprint(db, fingerprint).label) if separator else address
    )
    if row["link_delivery_status"] == "bounced":
        return f'Your mail "{subject}" to {to_label} bounced: {bounce_reason_text(row["link_delivery_reason"])}.'
    return f'Your mail "{subject}" to {to_label} was not delivered: {expiry_reason_text(row["link_delivery_reason"])}.'
