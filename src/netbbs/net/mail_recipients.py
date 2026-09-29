"""
Finding someone to write to at the mail To prompt (issue #826).

Two ways, both at the prompt itself so nothing new has to be learned to
leave it:

- **Tab** completes what is typed: a local member's name, the address of
  someone the caller has had mail with lately, and, after an `@`, the name
  of a linked BBS. Several matches are listed under the prompt, as chat's
  completion lists them.
- **`?`** and Enter opens a list of everyone the caller can write to --
  recent correspondents first, then the members, then the linked BBSes.
  `name@?` lists just the linked BBSes, for that name. Choosing a BBS
  without a name asks for the name there.

What either offers is an `AddressBook`, gathered once as the prompt opens
(the completer runs inside the line editor and cannot reach the
database). It never holds the caller or the guest account. The list also
shows, muted, with `-` for a number and the reason beside it, the people
and BBSes the caller cannot write to right now (issue #920): someone who
blocked the caller (`mail_sender_refusal`), a disabled account or a signup
waiting for approval (`mail_recipient_refusal`), and a met BBS still on
probation (`link_mail_refusal`). Each reason is the `tag` of the same
`MailRefusal` whose sentence the To prompt answers with when the address
is typed. A BBS whose mail the SysOp closed (quarantined or blocked) is
left out. Tab offers only what can be picked: completion types an address
the To prompt will take.

Whatever is completed or chosen is only text in the To field: the To
prompt checks it exactly as it checks a typed address, and Send checks
it again. A BBS chosen from the list goes in by its technical identity,
so the letter reaches the node that was chosen even if another takes its
name.

The To field can name several people (issue #827), separated by commas:
`bob, carol@Farpoint`. Tab completes the address after the last comma,
and `?` as the last address opens the list for that one. A comma inside
double quotes -- a quoted node name, `bob@"Cats, Dogs"` -- does not
separate (`split_recipients`).
"""

from __future__ import annotations

from dataclasses import dataclass

from netbbs.auth.users import User, list_users
from netbbs.identity.addressing import is_valid_user_part
from netbbs.link.enforcement import LinkPolicyAction, decide_node_action
from netbbs.link.node_profiles import (
    UNKNOWN_NODE_NAME,
    UNNAMED_NODE_NAME,
    identity_for_fingerprint,
    link_address_label,
    met_peer_identities,
    name_key,
    resolve_stored_peer_reference,
)
from netbbs.link.trust import TrustState
from netbbs.mail import (
    MailRefusal,
    mail_recipient_refusal_detail,
    mail_sender_refusal_detail,
    recent_correspondents,
    split_link_address,
)
from netbbs.net.char_input import CandidateListPrinter, Completer, InputCancelled, move_cursor
from netbbs.net.picker import pick_item
from netbbs.net.session import Session, write_prompt
from netbbs.rendering import MUTED_COLOR, colored, sanitize_text
from netbbs.rendering.width import display_width
from netbbs.storage.database import Database

#: How many recent correspondents the book offers.
RECENT_LIMIT = 10

#: More Tab matches than this are counted, not listed: a list longer than a
#: screen scrolls the prompt away.
MAX_LISTED_MATCHES = 24

PICKER_REQUEST = "?"

#: What separates the people a letter is for in the To field (issue #827).
RECIPIENT_SEPARATOR = ","


def _separator_positions(text: str) -> list[int]:
    """Where the separating commas are in `text`: not inside double
    quotes, which a node name with `@` in it is shown in."""
    positions, quoted = [], False
    for index, char in enumerate(text):
        if char == '"':
            quoted = not quoted
        elif char == RECIPIENT_SEPARATOR and not quoted:
            positions.append(index)
    return positions


def split_recipients(text: str) -> list[str]:
    """The addresses a To field names, in order, each stripped; empty ones
    (`bob,, carol`, a trailing comma) left out."""
    parts, start = [], 0
    for position in _separator_positions(text):
        parts.append(text[start:position])
        start = position + 1
    parts.append(text[start:])
    return [part.strip() for part in parts if part.strip()]


def join_recipients(addresses: list[str]) -> str:
    """`split_recipients` undone: the To field for `addresses`."""
    return f"{RECIPIENT_SEPARATOR} ".join(addresses)


def _last_address_start(typed: str) -> int:
    """Where the address being typed starts: after the last separating
    comma and the spaces after it, else at the start."""
    positions = _separator_positions(typed)
    if not positions:
        return 0
    start = positions[-1] + 1
    while start < len(typed) and typed[start] == " ":
        start += 1
    return start


@dataclass(frozen=True)
class RecipientChoice:
    """One entry of the book. `text` is what goes in the To field: a
    member's name or a Link address `user@<reference>`; for a BBS it is the
    node's fingerprint. `completion` is what Tab types for it, `label` what
    the list shows."""

    kind: str  # "person" or "node"
    text: str
    completion: str
    label: str
    recent: bool = False
    linked: bool = False
    #: A BBS's DNS name, which Tab also matches.
    dns_name: str | None = None
    #: Why this one can't be written to right now (a `MailRefusal.tag`), or
    #: `None` when it can (issue #920). The list shows it and does not let
    #: it be picked; Tab does not offer it.
    refusal: str | None = None


@dataclass(frozen=True)
class AddressBook:
    people: tuple[RecipientChoice, ...]
    nodes: tuple[RecipientChoice, ...]


def _node_reference(db: Database, fingerprint: str, identity) -> str:
    """The shortest thing to type after `@` that names this node and no
    other this BBS has met: its friendly name, else its DNS name, else its
    technical identity."""
    for reference in (identity.friendly_name, identity.dns_name):
        if not reference or reference in (UNNAMED_NODE_NAME, UNKNOWN_NODE_NAME):
            continue
        if resolve_stored_peer_reference(db, reference, met_only=True) == fingerprint:
            return reference
    return fingerprint


#: The list's words for a BBS still on probation (issue #920).
NOT_LINKED_TAG = "not linked yet"


def link_mail_refusal(db: Database, fingerprint: str) -> MailRefusal | None:
    """Why this node will not send mail to `fingerprint`, or `None` when it
    will (issue #804): the To prompt's sentence for a typed address, and
    the list's words for it (issue #920). A node on probation is listed,
    as not linked yet; one whose mail is closed (quarantined or blocked) is
    not, so its `tag` is `None`."""
    decision = decide_node_action(db, fingerprint, LinkPolicyAction.LINK_MAIL)
    if decision.allowed:
        return None
    label = sanitize_text(identity_for_fingerprint(db, fingerprint).label)
    if decision.state == TrustState.PROBATIONARY:
        return MailRefusal(f"{label} is not linked yet; mail opens once the SysOp establishes it.", NOT_LINKED_TAG)
    return MailRefusal(f"Mail to {label} is closed on this BBS.", None)


def _listed_nodes(db: Database) -> dict[str, tuple[object, str, str | None]]:
    """Met nodes the list shows, by fingerprint: their identity, the
    reference Tab types for them, and why mail can't go there yet, if it
    can't (see `link_mail_refusal`)."""
    nodes = {}
    for identity in met_peer_identities(db):
        refusal = link_mail_refusal(db, identity.fingerprint)
        if refusal is not None and refusal.tag is None:
            continue
        nodes[identity.fingerprint] = (
            identity,
            _node_reference(db, identity.fingerprint, identity),
            None if refusal is None else refusal.tag,
        )
    return nodes


def _member_refusal(db: Database, account: User, sender: User) -> MailRefusal | None:
    """The To prompt's own checks of a member, in its order: whether the
    account takes mail at all, then whether it takes it from `sender`."""
    return mail_recipient_refusal_detail(db, account) or mail_sender_refusal_detail(db, account, sender=sender)


def gather_address_book(db: Database, user: User, *, link_enabled: bool) -> AddressBook:
    """Who `user` can write to from here, and who not and why, for the To
    prompt's completion and list (see the module docstring)."""
    members: dict[int, tuple[User, str | None]] = {}
    for account in list_users(db):
        if account.id == user.id:
            continue
        refusal = _member_refusal(db, account, user)
        if refusal is not None and refusal.tag is None:
            continue
        members[account.id] = (account, None if refusal is None else refusal.tag)
    nodes = _listed_nodes(db) if link_enabled else {}

    recent: list[RecipientChoice] = []
    for who in recent_correspondents(db, user, limit=RECENT_LIMIT):
        if isinstance(who, int):
            if who not in members:
                continue
            account, refusal = members[who]
            name = sanitize_text(account.username)
            recent.append(RecipientChoice("person", name, name, name, recent=True, refusal=refusal))
            continue
        split = split_link_address(who)
        if split is None or split[1] not in nodes or not is_valid_user_part(split[0]):
            continue
        user_part, fingerprint = split
        identity, reference, refusal = nodes[fingerprint]
        recent.append(RecipientChoice(
            "person",
            f"{user_part}@{fingerprint}",
            sanitize_text(link_address_label(user_part, reference)),
            sanitize_text(link_address_label(user_part, identity.label)),
            recent=True, linked=True, refusal=refusal,
        ))
    recent_ids = {choice.text for choice in recent}
    everyone = [
        RecipientChoice("person", name, name, name, refusal=refusal)
        for name, refusal in (
            (sanitize_text(account.username), refusal) for account, refusal in members.values()
        )
        if name not in recent_ids
    ]
    everyone.sort(key=lambda choice: choice.text.casefold())
    linked = sorted(
        (
            RecipientChoice(
                "node", fingerprint, sanitize_text(reference), sanitize_text(identity.label), linked=True,
                dns_name=identity.dns_name, refusal=refusal,
            )
            for fingerprint, (identity, reference, refusal) in nodes.items()
        ),
        key=lambda choice: choice.label.casefold(),
    )
    return AddressBook(people=tuple(recent + everyone), nodes=tuple(linked))


# -- Tab ---------------------------------------------------------------------


class RecipientCompleter:
    """Tab at the To prompt (see `netbbs.net.char_input.apply_tab_completion`).

    Completes the address after the last comma (issue #827), or the whole
    field when there is none: a name, an address, or after `@` a linked
    BBS for the name before it. The line editor replaces only the word
    since the last space, and a BBS's name can hold spaces, so each match
    is handed back from that word on. `last_matches` keeps the matched
    addresses whole, for `print_matches` to list.

    Only what can be written to is offered (issue #920): the list shows
    the rest with the reason, but a completion is an address the To
    prompt would refuse."""

    def __init__(self, book: AddressBook, session: Session, prompt: str) -> None:
        self._book = book
        self._session = session
        self._prompt = prompt
        self.last_matches: list[str] = []

    def matches(self, typed: str) -> list[str]:
        """Every whole To text that `typed` is the start of, ignoring case."""
        typed = typed.lstrip()
        if "@" in typed:
            user_part, _, node_part = typed.partition("@")
            needle = name_key(node_part.strip('"'))
            found = [
                link_address_label(user_part, node.completion)
                for node in self._book.nodes
                if node.refusal is None
                and (
                    name_key(node.completion).startswith(needle)
                or name_key(node.label).startswith(needle)
                    or (node.dns_name is not None and node.dns_name.startswith(needle))
                )
            ]
        else:
            needle = typed.casefold()
            found = [
                person.completion for person in self._book.people
                if person.refusal is None and person.completion.casefold().startswith(needle)
            ]
        return list(dict.fromkeys(found))

    def __call__(self, text_before_cursor: str) -> list[str]:
        typed = text_before_cursor.lstrip()
        start = _last_address_start(typed)
        head = typed[:start]
        word = typed.rsplit(" ", 1)[-1]
        lead = typed[: len(typed) - len(word)]
        found = [
            (address, head + address) for address in self.matches(typed[start:])
            if (head + address).casefold().startswith(lead.casefold())
        ]
        self.last_matches = [address for address, _field in found]
        return [field[len(lead):] for _address, field in found]

    async def print_matches(self, candidates, line: str, cursor: int) -> None:
        """List several matches under the prompt, wrapped, and draw the
        prompt and the line again below them with the cursor where it
        was."""
        session = self._session
        await session.write("\r\n")
        matches = self.last_matches or list(candidates)
        if len(matches) > MAX_LISTED_MATCHES:
            await session.write_line(colored(
                f"{len(matches)} addresses match. Type more, or ? and Enter for the list.",
                fg_color=MUTED_COLOR,
            ))
        else:
            await session.write_line("   ".join(matches))
        await write_prompt(session, self._prompt)
        await session.write(line)
        back = display_width(line[cursor:])
        if back:
            await session.write(move_cursor(back, forward=False))


def read_to_line_options(completer: RecipientCompleter) -> dict:
    """The `read_line` arguments that give a To prompt its Tab."""
    printer: CandidateListPrinter = completer.print_matches
    as_completer: Completer = completer
    return {"completer": as_completer, "list_candidates": printer}


# -- ? -----------------------------------------------------------------------


def picker_request(text: str, *, link_enabled: bool) -> tuple[str, str | None] | None:
    """Whether a To answer asks for the list: `("all", None)` for `?`,
    `("nodes", name)` for `name@?` on a linked BBS (the name may be empty),
    else `None`."""
    text = text.strip()
    if text == PICKER_REQUEST:
        return "all", None
    if link_enabled and "@" in text:
        user_part, _, node_part = text.partition("@")
        if node_part.strip() == PICKER_REQUEST:
            return "nodes", user_part.strip()
    return None


async def choose_recipient(
    session: Session,
    book: AddressBook,
    request: tuple[str, str | None],
    **picker_style,
) -> str | None:
    """The list `picker_request` asked for; returns the To text chosen, or
    `None` when the caller went back. A BBS chosen without a name asks for
    the name there; the address then names the BBS by its technical
    identity (see the module docstring). A row that can't be written to
    shows its reason in place of its description and can't be picked
    (issue #920)."""
    scope, name = request
    choices = list(book.nodes) if scope == "nodes" else [*book.people, *book.nodes]
    if scope == "nodes":
        title, empty = "Linked BBSes", "No linked BBS takes mail from here right now."
    else:
        title, empty = "Write to", "There is no one here you can write to yet."

    positions = {id(choice): index for index, choice in enumerate(choices)}

    def describe(choice: RecipientChoice) -> str | None:
        if choice.refusal is not None:
            return choice.refusal
        if choice.kind == "node":
            return "linked BBS"
        if choice.recent:
            return "recent, linked BBS" if choice.linked else "recent"
        return None

    picked = await pick_item(
        session, choices,
        name_of=lambda choice: choice.label,
        stable_id_of=lambda choice: positions[id(choice)],
        description_of=describe,
        selectable_of=lambda choice: choice.refusal is None,
        title=title, breadcrumb=("Mail",), empty_message=empty,
        **picker_style,
    )
    if picked is None:
        return None
    if picked.kind == "person":
        return picked.text
    if not name:
        await write_prompt(session, f"Their user name at {picked.label}: ")
        try:
            name = (await session.read_line(cancellable=True)).strip()
        except InputCancelled:
            name = ""
        if not name:
            return None
        if "@" in name:
            # A whole address after all: checked as typed.
            return name
    return f"{name}@{picked.text}"
