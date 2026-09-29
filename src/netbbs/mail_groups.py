"""
Sending one letter to several people (issue #827).

A letter to several people is one ordinary letter per recipient (see
`netbbs.mail`'s "one letter to several people" section): local copies by
`netbbs.mail.send_mail_without_commit`, Link copies by
`netbbs.link.mail.compose_link_message`, all carrying the same group. This
module only puts the two together, which `netbbs.mail` cannot do itself: a
standalone node's mail must not import Link code.

Every copy is checked before any is written, and all of them are written in
one transaction. So a letter goes to everyone it is addressed to or to no
one, and a caller told that one recipient cannot take it fixes the To and
sends again without anyone getting it twice. What is checked here is what
the database can say -- the account takes mail, has not blocked the sender,
has room; the node can be written to. Whether this node will send another
node mail at all (its trust policy) is the To prompt's check, made again
at Send by the caller of this module, as for a letter to one person.

Link delivery happens later, one copy at a time: a Link copy that bounces
is that recipient's alone, told in the sender's Sent like any other.

Files a letter points at (issue #830, `netbbs.file_refs`) are written with
each local copy, and checked for each recipient with the rest: a recipient
who may not read the file area one is in is named, and nothing is sent. A
Link copy carries no reference -- its body names each file in a line of
text (`netbbs.file_refs.body_with_link_text`) -- so a Link recipient is
never refused for one.
"""

from __future__ import annotations

from dataclasses import dataclass

from netbbs.auth.users import User
from netbbs.file_refs import FileRef, body_with_link_text, recipient_ref_problem, sender_ref_problem
from netbbs.link.mail import check_link_mail_recipient, compose_link_message
from netbbs.link.node_identity import NodeIdentity
from netbbs.mail import (
    ALREADY_SENT_TEXT,
    MAX_MAIL_RECIPIENTS,
    GroupMember,
    LetterGroup,
    MailError,
    encode_group_to,
    letter_already_sent,
    mail_has_room,
    mail_recipient_refusal,
    mail_sender_refusal,
    mailbox_full_text,
    send_mail_without_commit,
    validate_mail_fields,
)
from netbbs.storage.database import Database


@dataclass(frozen=True)
class LetterRecipient:
    """One person a letter goes to: an account here, or a Link address
    `user@<home-node-fingerprint>`."""
    user: User | None = None
    address: str | None = None

    @property
    def key(self) -> tuple:
        """What makes two recipients the same person."""
        if self.user is not None:
            return ("local", self.user.id)
        assert self.address is not None
        user_part, _, fingerprint = self.address.rpartition("@")
        return ("link", user_part.casefold(), fingerprint)


class LetterRefused(MailError):
    """Some recipients cannot take the letter; nothing was sent.
    `problems` names each, with why, in the order they were given."""

    def __init__(self, problems: list[tuple[LetterRecipient, str]]) -> None:
        super().__init__("; ".join(reason for _recipient, reason in problems))
        self.problems = problems


def too_many_recipients_text(count: int) -> str:
    return f"A letter can go to at most {MAX_MAIL_RECIPIENTS} people; this one names {count}."


def group_addresses(members: list[GroupMember], *, own_fingerprint: str) -> list[str]:
    """The letter's To as another BBS reads it: everyone as
    `user@<home-node-fingerprint>`, an account here with this node's own
    fingerprint."""
    addresses = []
    for member in members:
        if member.address is not None:
            addresses.append(member.address)
        elif member.name:
            addresses.append(f"{member.name}@{own_fingerprint}")
    return addresses


def recipient_problem(
    db: Database, sender: User, recipient: LetterRecipient, *, node_identity: NodeIdentity | None,
    files: list[FileRef] = (),
) -> str | None:
    """Why `recipient`'s copy cannot be written, or `None`: the checks
    `send_mail` and `compose_link_message` make, asked first -- and for a
    local recipient, whether they may read the file area of each of `files`
    (issue #830)."""
    if recipient.user is not None:
        refusal = mail_recipient_refusal(db, recipient.user)
        if refusal is None:
            refusal = mail_sender_refusal(db, recipient.user, sender=sender)
        if refusal is None and not mail_has_room(db, recipient.user):
            refusal = mailbox_full_text(recipient.user)
        if refusal is None and files:
            refusal = recipient_ref_problem(db, recipient.user, list(files))
        return refusal
    assert recipient.address is not None
    if node_identity is None:
        return "This BBS is not linked with other BBSes right now."
    return check_link_mail_recipient(db, sender, recipient.address)


def send_letter(
    db: Database,
    sender: User,
    recipients: list[LetterRecipient],
    subject: str,
    body: str,
    *,
    group_id: str,
    node_identity: NodeIdentity | None = None,
    files: list[FileRef] = (),
) -> int:
    """Send one letter to each of `recipients` (two or more; a repeat of
    someone already named is left out), linked by `group_id`. Returns how
    many copies were written.

    Raises `LetterRefused` when any recipient cannot take it, naming each,
    and writes nothing; `MailError` for a letter already sent under this
    `group_id` (`netbbs.mail.letter_already_sent`), too many recipients, a
    subject or body the limits refuse, or a file in `files` the sender
    cannot open."""
    subject = validate_mail_fields(subject, body)
    files = list(files)
    _check_sender_files(db, sender, files)
    unique: dict[tuple, LetterRecipient] = {}
    for recipient in recipients:
        unique.setdefault(recipient.key, recipient)
    recipients = list(unique.values())
    if len(recipients) > MAX_MAIL_RECIPIENTS:
        raise MailError(too_many_recipients_text(len(recipients)))
    if letter_already_sent(db, sender, group_id):
        raise MailError(ALREADY_SENT_TEXT)
    problems = [
        (recipient, problem)
        for recipient in recipients
        if (problem := recipient_problem(db, sender, recipient, node_identity=node_identity, files=files))
        is not None
    ]
    if problems:
        raise LetterRefused(problems)

    members = [
        GroupMember(user_id=recipient.user.id, name=recipient.user.username) if recipient.user is not None
        else GroupMember(address=recipient.address)
        for recipient in recipients
    ]
    group = LetterGroup(id=group_id, to=encode_group_to(members))
    addresses = (
        group_addresses(members, own_fingerprint=node_identity.fingerprint) if node_identity is not None else []
    )
    link_body = body_with_link_text(db, body, files)
    try:
        for recipient in recipients:
            if recipient.user is not None:
                send_mail_without_commit(db, sender, recipient.user, subject, body, group=group, files=files)
            else:
                assert recipient.address is not None and node_identity is not None
                compose_link_message(
                    db, sender, recipient.address, subject, link_body, node_identity=node_identity,
                    group=group, group_addresses=addresses, commit=False,
                )
        db.connection.commit()
    except BaseException:
        db.connection.rollback()
        raise
    return len(recipients)


def _check_sender_files(db: Database, sender: User, files: list[FileRef]) -> None:
    if files:
        problem = sender_ref_problem(db, sender, files)
        if problem is not None:
            raise MailError(problem)
