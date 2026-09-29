"""
Finding someone to write to at the mail To prompt (issue #826).

Two ways, both at the prompt itself so nothing new has to be learned to
leave it:

- **Tab** completes what is typed: a local member's name, the address of
  someone the caller has had mail with lately, and, after an `@`, the name
  of a linked BBS. Several matches are listed under the prompt, as chat's
  completion lists them.
- **`?`** and Enter opens a list of everyone the caller can write to --
  recent correspondents first, then the linked BBSes, then the members.
  `name@?` lists just the linked BBSes, for that name. Choosing a BBS
  without a name asks for the name there.

What either offers is an `AddressBook`, gathered once as the prompt opens
(the completer runs inside the line editor and cannot reach the
database). It lists only people mail can reach from this caller: not the
caller, not the guest account, a disabled account or a signup waiting for
approval (`mail_recipient_refusal`), not someone who blocked the caller
(`mail_sender_refusal`), and on Link only nodes this BBS has met and will
send mail to. Nobody is revealed who could not be written to anyway.

Whatever is completed or chosen is only text in the To field: the To
prompt checks it exactly as it checks a typed address, and Send checks
it again. A BBS chosen from the list goes in by its technical identity,
so the letter reaches the node that was chosen even if another takes its
name.
"""

from __future__ import annotations

from dataclasses import dataclass

from netbbs.auth.users import User, list_users
from netbbs.identity.addressing import is_valid_user_part
from netbbs.link.enforcement import LinkPolicyAction, decide_node_action
from netbbs.link.node_profiles import (
    UNKNOWN_NODE_NAME,
    UNNAMED_NODE_NAME,
    link_address_label,
    met_peer_identities,
    name_key,
    resolve_stored_peer_reference,
)
from netbbs.mail import mail_recipient_refusal, mail_sender_refusal, recent_correspondents, split_link_address
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


def _mail_open_nodes(db: Database) -> dict[str, tuple[object, str]]:
    """Met nodes this BBS will send mail to (issue #804), by fingerprint:
    their identity and the reference Tab types for them."""
    nodes = {}
    for identity in met_peer_identities(db):
        if not decide_node_action(db, identity.fingerprint, LinkPolicyAction.LINK_MAIL).allowed:
            continue
        nodes[identity.fingerprint] = (identity, _node_reference(db, identity.fingerprint, identity))
    return nodes


def gather_address_book(db: Database, user: User, *, link_enabled: bool) -> AddressBook:
    """Who `user` can write to from here, for the To prompt's completion
    and list (see the module docstring for who is left out)."""
    members = {
        account.id: account
        for account in list_users(db)
        if account.id != user.id
        and mail_recipient_refusal(db, account) is None
        and mail_sender_refusal(db, account, sender=user) is None
    }
    nodes = _mail_open_nodes(db) if link_enabled else {}

    recent: list[RecipientChoice] = []
    for who in recent_correspondents(db, user, limit=RECENT_LIMIT):
        if isinstance(who, int):
            account = members.get(who)
            if account is None:
                continue
            name = sanitize_text(account.username)
            recent.append(RecipientChoice("person", name, name, name, recent=True))
            continue
        split = split_link_address(who)
        if split is None or split[1] not in nodes or not is_valid_user_part(split[0]):
            continue
        user_part, fingerprint = split
        identity, reference = nodes[fingerprint]
        recent.append(RecipientChoice(
            "person",
            f"{user_part}@{fingerprint}",
            sanitize_text(link_address_label(user_part, reference)),
            sanitize_text(link_address_label(user_part, identity.label)),
            recent=True, linked=True,
        ))
    recent_ids = {choice.text for choice in recent}
    everyone = [
        RecipientChoice("person", name, name, name)
        for name in (sanitize_text(account.username) for account in members.values())
        if name not in recent_ids
    ]
    everyone.sort(key=lambda choice: choice.text.casefold())
    linked = sorted(
        (
            RecipientChoice(
                "node", fingerprint, sanitize_text(reference), sanitize_text(identity.label), linked=True,
            )
            for fingerprint, (identity, reference) in nodes.items()
        ),
        key=lambda choice: choice.label.casefold(),
    )
    return AddressBook(people=tuple(recent + everyone), nodes=tuple(linked))


# -- Tab ---------------------------------------------------------------------


class RecipientCompleter:
    """Tab at the To prompt (see `netbbs.net.char_input.apply_tab_completion`).

    Completes the whole field: a name, an address, or after `@` a linked
    BBS for the name before it. The line editor replaces only the word
    since the last space, and a BBS's name can hold spaces, so each match
    is handed back from that word on. `last_matches` keeps the matches
    whole, for `print_matches` to list."""

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
            needle = name_key(node_part.lstrip('"'))
            found = [
                link_address_label(user_part, node.completion)
                for node in self._book.nodes
                if name_key(node.completion).startswith(needle)
                or name_key(node.label).startswith(needle)
            ]
        else:
            needle = typed.casefold()
            found = [
                person.completion for person in self._book.people
                if person.completion.casefold().startswith(needle)
            ]
        return list(dict.fromkeys(found))

    def __call__(self, text_before_cursor: str) -> list[str]:
        typed = text_before_cursor.lstrip()
        word = typed.rsplit(" ", 1)[-1]
        lead = typed[: len(typed) - len(word)]
        self.last_matches = [
            match for match in self.matches(typed) if match.casefold().startswith(lead.casefold())
        ]
        return [match[len(lead):] for match in self.last_matches]

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
    identity (see the module docstring)."""
    scope, name = request
    choices = list(book.nodes) if scope == "nodes" else [*book.people, *book.nodes]
    if scope == "nodes":
        title, empty = "Linked BBSes", "No linked BBS takes mail from here right now."
    else:
        title, empty = "Write to", "There is no one here you can write to yet."

    positions = {id(choice): index for index, choice in enumerate(choices)}

    def describe(choice: RecipientChoice) -> str | None:
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
