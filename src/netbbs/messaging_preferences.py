"""
Who may send a caller a live message: the per-user opt-out from direct
messages (issue #99) and, since issue #925, the caller's block list.

The opt-out is a thin wrapper over `netbbs.user_preferences`'s generic
store, the same pattern `netbbs.directory`'s bio-visibility preference
already establishes -- except defaulted the *other* way. Bio visibility
defaults private (opt-in to share) because it's about disclosing
personal content; this defaults to accepting messages (opt-out to
block) because most callers presumably want to stay reachable, and the
feature this gates is unsolicited-but-visible (the sender always knows
who they're messaging, unlike, say, unsolicited chat invites) rather
than a disclosure risk.

`live_message_refusal` is the one sender-aware check every live message
path makes before it delivers: `/msg` and `/private`, `/dm` and the Who's
online chat invite, Who's online's one-off message, and an inbound Link
direct message (`netbbs.net.link_direct.build_direct_message_deliverer`).
It combines the opt-out with the same block list mail uses
(`netbbs.mail`'s `mail_blocks`, issue #817): one list, so blocking someone
stops their mail and their live messages alike. Public chat channels are
not covered -- a block does not hide someone's lines in a shared room.
"""

from __future__ import annotations

from netbbs.auth.users import User, is_usable_sysop
from netbbs.mail import blocks_link_sender, blocks_local_sender
from netbbs.storage.database import Database
from netbbs.user_preferences import get_user_preference, set_user_preference

_ACCEPTS_DIRECT_MESSAGES_KEY = "accepts_direct_messages"

OPTED_OUT_REFUSAL = "{name} has opted out of direct messages."
# The live counterpart of `netbbs.mail.SENDER_BLOCK_REFUSAL`: a blocked
# sender is told, as a blocked letter's sender is (issue #817's choice).
LIVE_BLOCK_REFUSAL = "{name} does not accept messages from you."


def accepts_direct_messages(db: Database, user: User) -> bool:
    """Default `True` (opt-out, not opt-in) -- see module docstring."""
    return get_user_preference(db, user, _ACCEPTS_DIRECT_MESSAGES_KEY, default="1") == "1"


def set_accepts_direct_messages(db: Database, user: User, accepts: bool) -> None:
    set_user_preference(db, user, _ACCEPTS_DIRECT_MESSAGES_KEY, "1" if accepts else "0")


def live_message_refusal(
    db: Database, recipient: User, *, sender: User | None = None, sender_address: str | None = None,
) -> str | None:
    """Why `recipient` takes no live message from this sender -- a local
    account (`sender`) or a Link sender (`sender_address`,
    `user@<home-node-fingerprint>`) -- as the line to show the sender, or
    `None` when the message may be delivered.

    The opt-out is answered first: it is the recipient's general choice
    and says nothing about the sender. A block is answered only when the
    recipient does take live messages. As with mail, a SysOp of this node
    cannot be blocked (the opt-out still applies to them, as it always
    has); SysOp and system notices do not come through here at all."""
    if not accepts_direct_messages(db, recipient):
        return OPTED_OUT_REFUSAL.format(name=recipient.username)
    if sender is not None:
        blocked = not is_usable_sysop(sender) and blocks_local_sender(db, recipient, sender)
    else:
        blocked = sender_address is not None and blocks_link_sender(db, recipient, sender_address)
    return LIVE_BLOCK_REFUSAL.format(name=recipient.username) if blocked else None
