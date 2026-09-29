"""What this node refused of the Link mail sent to it (issue #820).

A refusal is visible to its sender as a bounce (issue #804, #806), but until
this module nothing on the receiving node kept it: the SysOp whose policy
refused a letter could not see that it had happened, from whom, or why, and so
could not act on it. This is the smallest record that answers those questions.

What a row holds, and what it deliberately does not:

* who sent it -- the sender's home node and user name as the letter names
  them -- why it was refused, how it arrived, and when; the letter's
  `content_id` only as the key that folds a sender's retries of one letter into
  one row;
* never the recipient, the subject or the body. Mail is private, and a SysOp
  tool that showed what a refused letter said would be a way to read mail
  sent to someone else. A refused letter's ciphertext is not kept either.

The log is bounded (`MAX_LINK_MAIL_REFUSALS_KEPT` in all and
`MAX_LINK_MAIL_REFUSALS_PER_NODE` from one node, the newest by last refusal
win) because the sender decides how many letters it sends: a node on
probation here must not be able to grow this table without limit, or crowd
every other node's refusals out of it.

Plain, synchronous and `db`-first like the rest of `netbbs.link`: async
callers dispatch through `DatabaseLane`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from netbbs.link.events import LinkMessage
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

#: At most this many refused letters are kept; the oldest go first.
MAX_LINK_MAIL_REFUSALS_KEPT = 500
#: And at most this many from any one node, so that one node signing letters
#: by the hundred cannot push every other node's refusals out of the log.
MAX_LINK_MAIL_REFUSALS_PER_NODE = 50

#: How the refused letter reached this node.
VIA_DIRECT = "direct"
VIA_RELAY = "relay"

# A reason or name comes from another node: kept only up to these lengths.
# Every reason code NetBBS defines, and every valid address user part, is far
# shorter.
_MAX_REASON_LENGTH = 64
_MAX_USER_LENGTH = 64

# Each reason, in words for the SysOp of the node that refused the letter --
# the other side of `netbbs.link.mail._BOUNCE_REASON_TEXT`, which says the same
# things to the sender.
_REASON_TEXT = {
    "link_policy_node_probationary_read_only": "its node is still on probation here",
    "link_policy_node_quarantined": "its node is quarantined here",
    "link_policy_manual_block": "the sender or its node is blocked here",
    "link_policy_user_quarantined": "the sender is quarantined here",
    "link_policy_user_probationary_approval_required": "the sender is still on probation here",
    "link_policy_probation_budget_exceeded": "its node sent more than a node on probation may",
    "unknown_recipient": "it was addressed to no account here",
    "mailbox_full": "the recipient's mailbox was full of unread mail",
    "malformed": "it was not a valid letter (sender name, subject or body)",
    "undecryptable": "it could not be decrypted with this node's key",
    "no_mailbox": "it was addressed to the guest account, which takes no mail",
}

#: Reasons a SysOp answers with trust: establishing, or lifting a block.
TRUST_REASONS = frozenset(code for code in _REASON_TEXT if code.startswith("link_policy_"))


def refusal_reason_text(reason: str) -> str:
    return _REASON_TEXT.get(reason, "refused for a reason this version does not know")


@dataclass(frozen=True)
class LinkMailRefusal:
    id: int
    sender_node_fingerprint: str
    # The user part of the sender's address; None when the refusal came before
    # the letter could be read as one (a push refused for its node).
    sender_user: str | None
    reason: str
    via: str
    first_refused_at: str
    last_refused_at: str
    attempts: int


def record_link_mail_refusal(
    db: Database, raw_message: dict[str, Any], reason: str, *, via: str,
    sender_node_fingerprint: str | None = None,
) -> None:
    """Keep one refused letter. A letter refused again (a sender retrying the
    same message by another route) updates its row rather than adding one.

    `sender_node_fingerprint` is the node a direct push came from; the letter's
    sender is taken only when it names that same node. The caller verifies
    the letter's signature first (`LinkNode.is_signed_letter_from`): a push
    is refused before `handle_events` has checked anything.

    Never raises for a letter it cannot read: a record of a refusal must not be
    what breaks the refusal."""
    try:
        message = LinkMessage.from_dict(raw_message)
        content_id = message.content_id
        sender = message.payload.get("sender") or {}
        claimed_home = sender.get("home_node_fingerprint")
        user = sender.get("local_user_id")
    except Exception:  # noqa: BLE001 -- unverified input; any failure means "not a letter"
        return
    home = sender_node_fingerprint or claimed_home
    if not isinstance(home, str) or not home:
        return
    if sender_node_fingerprint is not None and claimed_home != sender_node_fingerprint:
        user = None
    if not isinstance(user, str) or not user:
        user = None
    now = utc_now_iso()
    with db.connection:
        db.connection.execute(
            """
            INSERT INTO link_mail_refusals
                (message_content_id, sender_node_fingerprint, sender_user, reason, via,
                 first_refused_at, last_refused_at, attempts)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(message_content_id) DO UPDATE SET
                reason = excluded.reason, via = excluded.via,
                last_refused_at = excluded.last_refused_at,
                attempts = link_mail_refusals.attempts + 1
            """,
            (
                content_id, home, user[:_MAX_USER_LENGTH] if user else None,
                str(reason)[:_MAX_REASON_LENGTH], via, now, now,
            ),
        )
        db.connection.execute(
            """
            DELETE FROM link_mail_refusals WHERE sender_node_fingerprint = ? AND id NOT IN (
                SELECT id FROM link_mail_refusals WHERE sender_node_fingerprint = ?
                ORDER BY last_refused_at DESC, id DESC LIMIT ?
            )
            """,
            (home, home, MAX_LINK_MAIL_REFUSALS_PER_NODE),
        )
        db.connection.execute(
            """
            DELETE FROM link_mail_refusals WHERE id NOT IN (
                SELECT id FROM link_mail_refusals ORDER BY last_refused_at DESC, id DESC LIMIT ?
            )
            """,
            (MAX_LINK_MAIL_REFUSALS_KEPT,),
        )


def list_link_mail_refusals(db: Database) -> list[LinkMailRefusal]:
    """Every kept refusal, the most recent first."""
    rows = db.connection.execute(
        """
        SELECT id, sender_node_fingerprint, sender_user, reason, via,
               first_refused_at, last_refused_at, attempts
        FROM link_mail_refusals ORDER BY last_refused_at DESC, id DESC
        """
    ).fetchall()
    return [
        LinkMailRefusal(
            id=row["id"], sender_node_fingerprint=row["sender_node_fingerprint"],
            sender_user=row["sender_user"], reason=row["reason"], via=row["via"],
            first_refused_at=row["first_refused_at"], last_refused_at=row["last_refused_at"],
            attempts=row["attempts"],
        )
        for row in rows
    ]
