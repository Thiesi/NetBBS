"""
A caller's QWK offline mail (issue #1389, design doc §6.10): which boards a
packet carries and under which conference numbers, what a packet holds, the
read state of what it carried, and what a reply packet posts.

Synchronous and `db`-first like every domain module; nothing here knows
about sessions or transfers (`netbbs.net.qwk_flow` is the screen, and the
browser transfer link calls `apply_rep` through its own lane). The packet
and reply formats are `netbbs.qwk.packet` and `netbbs.qwk.rep`.

**Conferences** (§16 "Issue #1154", Decision 21). Conference 0 is the
caller's mail. Every board gets a number the first time it is in one of the
caller's packets, and keeps it: a reader files its messages by number, and
renumbering would mix two boards' threads together. A board is in a packet
while the caller follows it, unless the QWK conferences screen took it out
(`remove`), and a board they don't follow is in it when that screen put it
in (`add`). Follows themselves are never changed here.

**Read state** (Decision 22). Building a packet records what it carried in
`qwk_sent`; nothing is marked read then. The next reply packet, or [M]ark
read, marks every one of them read -- a post as opened (`record_post_opened`),
a letter as read -- and empties the list. A packet built while earlier
messages are still waiting carries them again only when the caller says so.

**Replies are untrusted input** (Decision 23 and the batch's principle).
Each goes the way the same message written online goes: a post through
`create_post` with the board's checks first (write level, name and age
gates, the origin's posting rule, the real-name refusal), held on a
moderated board, then signed onto Link and queued for FTN; a letter
through Mail's own checks. The author is always the uploader. Each reply
gets a line in the report saying what became of it.
"""

from __future__ import annotations

import datetime
import hashlib
from dataclasses import dataclass, field

from netbbs import __version__
from netbbs.activity import (
    ensure_board_baseline,
    list_followed,
    record_post_opened,
    unread_root_posts,
)
from netbbs.attestation import format_stored_name_for_resource, meets_name_requirement
from netbbs.auth.users import AuthError, User, get_user_by_id, get_user_by_username, is_usable_sysop, list_users
from netbbs.boards.boards import Board, BoardError, get_board_by_id, list_boards
from netbbs.boards.limits import MAX_BODY_BYTES
from netbbs.boards.polls import PollError
from netbbs.boards.posts import Post, PostError, create_post, crosspost_group_of, shown_post_refs, visible_post
from netbbs.communities import (
    get_effective_min_write_level,
    get_effective_name_requirement,
    meets_read_gate,
    meets_write_gate,
    resource_age_gate,
)
from netbbs.config import get_node_display_name, get_qwk_bbs_id, get_qwk_packet_messages
from netbbs.file_refs import body_with_link_text
from netbbs.guest import guest_is_eligible
from netbbs.link.boards import is_board_closed, is_board_linked, posting_refusal, queue_board_post_if_linked
from netbbs.link.node_profiles import present_link_author_label
from netbbs.mail import (
    MailError,
    MailboxFullError,
    MailMessage,
    list_inbox,
    mail_access_refusal,
    mark_read,
    send_mail,
    sender_display_label,
    split_link_address,
)
from netbbs.qwk.packet import Conference, PacketInfo, PacketMessage, build_packet
from netbbs.qwk.rep import RepError, RepLimits, RepMessage, parse_rep
from netbbs.rendering import sanitize_text
from netbbs.rendering.ansi import strip_ansi
from netbbs.rendering.post_body import plain_post_body, post_body_text
from netbbs.storage.database import Database
from netbbs.timeutil import get_node_timezone, parse_utc_iso, utc_now_iso

MAIL_CONFERENCE = 0
MAIL_CONFERENCE_NAME = "Personal mail"

#: The text a packet may carry, in bytes, before the rest waits for the
#: next one -- the size half of the per-packet cap beside the SysOp's
#: message count.
MAX_PACKET_TEXT_BYTES = 16 * 1024 * 1024

#: Messages carried but not yet marked read, per caller. Only reached by a
#: caller who downloads over and over without uploading or marking read;
#: past it a packet carries nothing new until they do.
MAX_SENT_PER_CALLER = 20_000

#: Reply digests kept per caller, and for how long: enough to catch the
#: same packet sent again after a dropped transfer.
MAX_REP_SEEN_PER_CALLER = 2000
REP_SEEN_DAYS = 90

#: The browser and Zmodem uploads of a reply packet are bounded like this.
REP_LIMITS = RepLimits(body_bytes=MAX_BODY_BYTES)

_MAX_MESSAGE_NUMBER = 9_999_999


class QwkError(Exception):
    """Offline mail refused as a whole, with the reason for the caller."""


def qwk_refusal(db: Database, user: User) -> str | None:
    """Why `user` cannot use offline mail, or `None`. The guest account is
    shared, so its read state and replies would be everyone's."""
    if guest_is_eligible(db, user):
        return "Offline mail needs an account of your own; the guest account is shared."
    return None


def packet_filename(db: Database) -> str:
    return f"{get_qwk_bbs_id(db)}.QWK"


def rep_filename(db: Database) -> str:
    return f"{get_qwk_bbs_id(db)}.REP"


# -- conferences -----------------------------------------------------------


@dataclass(frozen=True)
class QwkConference:
    """One board as offline mail sees it for a caller. `number` is `None`
    for a board never in one of their packets."""

    board: Board
    number: int | None
    choice: str
    followed: bool

    @property
    def included(self) -> bool:
        return self.choice == "add" or (self.choice == "follow" and self.followed)


def _readable(db: Database, user: User, board: Board) -> bool:
    return meets_read_gate(db, user, board) and resource_age_gate(db, user, board) == "pass"


def _rows(db: Database, user: User) -> dict[str, tuple[int, str]]:
    return {
        row["board_key"]: (row["number"], row["choice"])
        for row in db.connection.execute(
            "SELECT board_key, number, choice FROM qwk_conferences WHERE user_id = ?", (user.id,)
        )
    }


def _next_number(db: Database, user: User) -> int:
    row = db.connection.execute(
        "SELECT COALESCE(MAX(number), 0) FROM qwk_conferences WHERE user_id = ?", (user.id,)
    ).fetchone()
    if row[0] >= 65535:
        raise QwkError("You have used every QWK conference number there is.")
    return row[0] + 1


def _set_choice_without_commit(db: Database, user: User, board: Board, choice: str) -> int:
    """Give `board` its number if it has none, and set the caller's choice."""
    row = db.connection.execute(
        "SELECT number FROM qwk_conferences WHERE user_id = ? AND board_key = ?", (user.id, board.board_id)
    ).fetchone()
    if row is not None:
        db.connection.execute(
            "UPDATE qwk_conferences SET choice = ? WHERE user_id = ? AND board_key = ?",
            (choice, user.id, board.board_id),
        )
        return row["number"]
    number = _next_number(db, user)
    db.connection.execute(
        "INSERT INTO qwk_conferences (user_id, number, board_key, choice) VALUES (?, ?, ?, ?)",
        (user.id, number, board.board_id, choice),
    )
    return number


def list_conferences(db: Database, user: User) -> list[QwkConference]:
    """Every board `user` may read, in the SysOp's order, with its number
    and whether packets carry it. Read only."""
    rows = _rows(db, user)
    followed = set(list_followed(db, user, "board"))
    out = []
    for board in list_boards(db):
        if not _readable(db, user, board):
            continue
        number, choice = rows.get(board.board_id, (None, "follow"))
        out.append(QwkConference(board, number, choice, board.id in followed))
    return out


def packet_conferences(db: Database, user: User) -> list[QwkConference]:
    """The boards the next packet carries, each numbered: a board carried
    for the first time is given its number now, and keeps it."""
    chosen = [conference for conference in list_conferences(db, user) if conference.included]
    if any(conference.number is None for conference in chosen):
        out = []
        for conference in chosen:
            if conference.number is None:
                number = _set_choice_without_commit(db, user, conference.board, conference.choice)
                conference = QwkConference(conference.board, number, conference.choice, conference.followed)
            out.append(conference)
        db.connection.commit()
        chosen = out
    return sorted(chosen, key=lambda conference: conference.number)


def save_conference_choices(db: Database, user: User, included: dict[int, bool]) -> int:
    """Apply the QWK conferences screen's draft: `included` by board row id.
    A board whose wish matches its follow goes back to following it; one
    that differs is added or removed. Returns how many changed."""
    changed = 0
    current = {conference.board.id: conference for conference in list_conferences(db, user)}
    try:
        for board_id, wanted in included.items():
            conference = current.get(board_id)
            if conference is None or conference.included == wanted:
                continue
            choice = "follow" if wanted == conference.followed else ("add" if wanted else "remove")
            _set_choice_without_commit(db, user, conference.board, choice)
            changed += 1
        db.connection.commit()
    except BaseException:
        db.connection.rollback()
        raise
    return changed


def _apply_reader_choices(db: Database, user: User, dropped: list[int], added: list[int]) -> list[str]:
    """QWKE's TODOOR.EXT: a reader dropping or taking back conferences the
    caller already has. Unknown numbers, and boards they may no longer
    read, are ignored. Returns a line per board changed."""
    if not dropped and not added:
        return []
    by_number = {
        conference.number: conference
        for conference in list_conferences(db, user) if conference.number is not None
    }
    lines = []
    for number, wanted in [*((n, False) for n in dropped), *((n, True) for n in added)]:
        conference = by_number.get(number)
        if conference is None or conference.included == wanted:
            continue
        choice = "follow" if wanted == conference.followed else ("add" if wanted else "remove")
        _set_choice_without_commit(db, user, conference.board, choice)
        lines.append(
            f"{sanitize_text(conference.board.name)} is {'back in' if wanted else 'out of'} your packets, "
            "as your reader asked."
        )
    db.connection.commit()
    return lines


# -- building a packet -----------------------------------------------------


def sent_count(db: Database, user: User) -> int:
    """Messages carried by packets and not yet marked read."""
    return db.connection.execute("SELECT COUNT(*) FROM qwk_sent WHERE user_id = ?", (user.id,)).fetchone()[0]


def _sent_keys(db: Database, user: User) -> set[tuple[str, int]]:
    return {
        (row["kind"], row["ref_id"])
        for row in db.connection.execute("SELECT kind, ref_id FROM qwk_sent WHERE user_id = ?", (user.id,))
    }


def _local_time(db: Database, iso: str) -> datetime.datetime:
    try:
        moment = parse_utc_iso(iso)
    except (ValueError, TypeError):
        return datetime.datetime(1980, 1, 1)
    return moment.astimezone(get_node_timezone(db)).replace(tzinfo=None)


def _author_name(db: Database, board: Board, post: Post) -> str:
    """Who wrote `post`, as its board shows it (without the colours)."""
    label = sanitize_text(present_link_author_label(db, post.author_label))
    requirement = get_effective_name_requirement(db, board)
    if requirement != "verified_and_displayed" or post.author_shown_name is None:
        return label
    carried = post.author_user_id is None and "@" in post.author_label
    return strip_ansi(format_stored_name_for_resource(
        label if carried else post.author_shown_name, post.author_verified_name, name_requirement=requirement,
    ))


def _post_text(db: Database, board: Board, post: Post) -> str:
    text = plain_post_body(post.body) if board.allow_color else post_body_text(post.body)
    return body_with_link_text(db, text, shown_post_refs(db, post))


def _row_id(db: Database, post_id: str | None) -> int | None:
    if post_id is None:
        return None
    row = db.connection.execute("SELECT id FROM posts WHERE post_id = ?", (post_id,)).fetchone()
    return row["id"] if row is not None else None


@dataclass
class BuiltPacket:
    """A packet ready to send, and what it holds."""

    data: bytes
    filename: str
    messages: int
    mail: int
    boards: int
    #: Messages left for the next packet: past the cap or the size limit.
    #: `skipped_sent` counts those earlier packets carried and the caller
    #: did not want again.
    more_waiting: bool
    skipped_sent: int = 0
    first_visits: list[str] = field(default_factory=list)


def _user_label(user: User) -> str:
    return sanitize_text(user.username)


def build_caller_packet(db: Database, user: User, *, include_sent: bool) -> BuiltPacket:
    """Build `user`'s next packet: their unread mail as conference 0, then
    the unread posts of each board packets carry, oldest first, up to the
    SysOp's message cap and `MAX_PACKET_TEXT_BYTES`. What it carries is
    recorded as sent (not read). `include_sent` carries again what earlier
    packets carried and nothing has marked read yet.

    A carried board the caller has never visited is visited now, as
    opening it would: nothing on it counts as new before that, so it adds
    nothing to this packet, and what arrives from now on goes in the next
    (`ensure_board_baseline`)."""
    refusal = qwk_refusal(db, user)
    if refusal is not None:
        raise QwkError(refusal)
    cap = get_qwk_packet_messages(db)
    room = MAX_SENT_PER_CALLER - sent_count(db, user)
    if include_sent:
        # What is carried again is already counted.
        room = MAX_SENT_PER_CALLER
    sent = _sent_keys(db, user)
    messages: list[PacketMessage] = []
    recorded: list[tuple[str, int, str]] = []
    budget = MAX_PACKET_TEXT_BYTES
    more = False
    skipped = 0
    first_visits: list[str] = []
    mail_count = 0

    def take(kind: str, ref_id: int, message: PacketMessage, key: str) -> bool:
        nonlocal budget, more
        cost = 128 + len(message.body.encode("utf-8", errors="replace")) + 256
        if len(messages) >= cap or (messages and cost > budget) or len(recorded) >= room:
            more = True
            return False
        messages.append(message)
        recorded.append((kind, ref_id, key))
        budget -= cost
        return True

    user_name = _user_label(user)
    if mail_access_refusal(db, user) is None:
        letters = [letter for letter in list_inbox(db, user) if not letter.is_read]
        for letter in reversed(letters):  # oldest arrival first
            if letter.id > _MAX_MESSAGE_NUMBER:
                continue
            if ("mail", letter.id) in sent and not include_sent:
                skipped += 1
                continue
            if not take("mail", letter.id, _mail_message(db, letter, user_name), str(letter.id)):
                break
            mail_count += 1

    conferences = packet_conferences(db, user)
    board_conferences: list[Conference] = []
    seen_groups: set[str] = set()
    for conference in conferences:
        board = conference.board
        board_conferences.append(Conference(conference.number, sanitize_text(board.name), _flags(db, user, board)))
        if len(messages) >= cap:
            more = True
            continue
        unread = unread_root_posts(db, user, board)
        if unread is None:
            ensure_board_baseline(db, user, board)
            first_visits.append(sanitize_text(board.name))
            continue
        for row_id, post_id in unread:
            if row_id > _MAX_MESSAGE_NUMBER:
                continue
            if ("post", row_id) in sent and not include_sent:
                skipped += 1
                continue
            post = visible_post(db, post_id)
            if post is None:
                continue
            group = crosspost_group_of(db, post)
            if group is not None and group in seen_groups:
                # Another copy is in this packet already; this one is
                # recorded with it, and marked read with it.
                recorded.append(("post", row_id, post.post_id))
                continue
            if not take("post", row_id, _post_message(db, board, post, conference.number, user), post.post_id):
                break
            if group is not None:
                seen_groups.add(group)

    info = PacketInfo(
        bbs_id=get_qwk_bbs_id(db),
        bbs_name=get_node_display_name(db),
        user_name=user_name,
        created_at=_local_time(db, utc_now_iso()),
        sysop_name=_sysop_name(db),
        door="NetBBS",
        version=__version__,
        hello=_hello(db, len(messages), more),
    )
    all_conferences = [Conference(MAIL_CONFERENCE, MAIL_CONFERENCE_NAME, "pP"), *board_conferences]
    data = build_packet(info, all_conferences, messages)
    now = utc_now_iso()
    try:
        db.connection.executemany(
            "INSERT INTO qwk_sent (user_id, kind, ref_id, ref_key, sent_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, kind, ref_id) DO UPDATE SET ref_key = excluded.ref_key, sent_at = excluded.sent_at",
            [(user.id, kind, ref_id, key, now) for kind, ref_id, key in recorded],
        )
        db.connection.commit()
    except BaseException:
        db.connection.rollback()
        raise
    return BuiltPacket(
        data=data, filename=packet_filename(db), messages=len(messages), mail=mail_count,
        boards=len({message.conference for message in messages if message.conference != MAIL_CONFERENCE}),
        more_waiting=more, skipped_sent=skipped, first_visits=first_visits,
    )


def _flags(db: Database, user: User, board: Board) -> str:
    """QWKE's AREA flags for a board: all messages, public only, read-only
    when the caller can't post, echomail or local."""
    from netbbs.ftn.networks import is_board_ftn

    flags = "aO"
    if _board_refusal(db, user, board, is_reply=True) is not None:
        flags += "R"
    flags += "E" if is_board_ftn(db, board) or is_board_linked(db, board) else "L"
    return flags


def _mail_message(db: Database, letter: MailMessage, user_name: str) -> PacketMessage:
    return PacketMessage(
        conference=MAIL_CONFERENCE,
        number=letter.id,
        when=_local_time(db, letter.created_at),
        to=user_name,
        sender=sanitize_text(sender_display_label(db, letter)),
        subject=sanitize_text(letter.subject),
        body=plain_post_body(letter.body),
        private=True,
        personal=True,
    )


def _post_message(db: Database, board: Board, post: Post, number: int, user: User) -> PacketMessage:
    to = "All"
    reference = None
    personal = False
    if post.parent_post_id is not None:
        reference = _row_id(db, post.parent_post_id)
        parent = visible_post(db, post.parent_post_id)
        if parent is not None:
            to = _author_name(db, board, parent)
            personal = parent.author_user_id == user.id
    return PacketMessage(
        conference=number,
        number=post.id,
        when=_local_time(db, post.created_at),
        to=to,
        sender=_author_name(db, board, post),
        subject=sanitize_text(post.subject),
        body=_post_text(db, board, post),
        reference=reference,
        personal=personal,
    )


def _sysop_name(db: Database) -> str:
    sysops = [account for account in list_users(db) if is_usable_sysop(account)]
    if not sysops:
        return "SysOp"
    return sanitize_text(min(sysops, key=lambda account: account.id).username)


def _hello(db: Database, count: int, more: bool) -> str:
    lines = [
        f"Welcome to {sanitize_text(get_node_display_name(db))}.",
        "",
        f"This packet holds {count} message{'' if count == 1 else 's'}."
        + (" More are waiting for your next packet." if more else ""),
        "Reply in your reader and upload the reply packet from Offline mail (QWK) under Message boards.",
        "Uploading it marks the messages in this packet read.",
    ]
    return "\n".join(lines)


# -- marking read ----------------------------------------------------------


def mark_sent_read(db: Database, user: User) -> int:
    """Mark everything packets carried read, and empty the list. Returns how
    many messages that was. A post gone since, or a letter deleted, is
    simply dropped from the list."""
    rows = db.connection.execute(
        "SELECT kind, ref_id, ref_key FROM qwk_sent WHERE user_id = ?", (user.id,)
    ).fetchall()
    boards: dict[int, Board | None] = {}
    marked = 0
    for row in rows:
        if row["kind"] == "mail":
            letter = db.connection.execute(
                "SELECT id FROM mail_messages WHERE id = ? AND recipient_user_id = ? AND recipient_deleted_at IS NULL",
                (row["ref_id"], user.id),
            ).fetchone()
            if letter is not None:
                from netbbs.mail import get_mail

                mark_read(db, user, get_mail(db, user, letter["id"]))
                marked += 1
            continue
        post_row = db.connection.execute(
            "SELECT * FROM posts WHERE id = ? AND post_id = ?", (row["ref_id"], row["ref_key"])
        ).fetchone()
        if post_row is None:
            continue
        board_id = post_row["board_id"]
        if board_id not in boards:
            try:
                boards[board_id] = get_board_by_id(db, board_id)
            except BoardError:
                boards[board_id] = None
        board = boards[board_id]
        if board is None:
            continue
        from netbbs.boards.posts import get_post

        record_post_opened(db, user, board, get_post(db, row["ref_key"]))
        marked += 1
    db.connection.execute("DELETE FROM qwk_sent WHERE user_id = ?", (user.id,))
    db.connection.commit()
    return marked


# -- replies ---------------------------------------------------------------


@dataclass(frozen=True)
class ReplyOutcome:
    """What became of one reply. `status` is `posted`, `held` (waiting for a
    moderator), `sent` (a letter) or `refused`."""

    index: int
    subject: str
    where: str
    status: str
    reason: str | None = None

    @property
    def line(self) -> str:
        subject = self.subject or "(no subject)"
        base = f"{self.index}. {subject!r} -- "
        if self.status == "posted":
            return base + f"posted on {self.where}." + (f" {self.reason}" if self.reason else "")
        if self.status == "held":
            return base + f"submitted to {self.where}; others see it once a moderator approves it."
        if self.status == "sent":
            return base + f"sent to {self.where}."
        return base + f"not posted: {self.reason}"


@dataclass
class RepResult:
    outcomes: list[ReplyOutcome]
    problems: list[str]
    marked_read: int
    notes: list[str] = field(default_factory=list)

    def count(self, *statuses: str) -> int:
        return sum(1 for outcome in self.outcomes if outcome.status in statuses)

    @property
    def summary(self) -> str:
        done = self.count("posted", "held", "sent")
        refused = self.count("refused")
        parts = [f"{done} of {len(self.outcomes)} repl{'y' if len(self.outcomes) == 1 else 'ies'} went out"]
        if refused:
            parts.append(f"{refused} refused")
        if self.problems:
            parts.append(f"{len(self.problems)} could not be read")
        return ", ".join(parts) + "."

    @property
    def lines(self) -> list[str]:
        return [
            *[outcome.line for outcome in self.outcomes],
            *[f"Packet: {problem}" for problem in self.problems],
            *self.notes,
            f"{self.marked_read} message{'' if self.marked_read == 1 else 's'} from your earlier packets marked read.",
        ]


def _board_refusal(db: Database, user: User, board: Board, *, is_reply: bool) -> str | None:
    """Why `user` may not post on `board` now -- the checks the board's own
    screen makes before it offers [P]ost or [R]eply."""
    if is_board_closed(db, board):
        return "the board is closed to new posts"
    if not meets_write_gate(db, user, board):
        return f"posting there needs level {get_effective_min_write_level(db, board)}"
    if not meets_name_requirement(db, user, get_effective_name_requirement(db, board)):
        return "posting there needs a verified name"
    if resource_age_gate(db, user, board) != "pass":
        return "the board has an age requirement you do not meet"
    from netbbs.ftn.scanner import real_name_refusal

    problem = real_name_refusal(db, board, user, short=True)
    if problem is not None:
        return problem
    # `None` reads the node's cached fingerprint (`posting_here`), so the
    # origin's own callers are told apart with Link off too.
    refusal = posting_refusal(db, board, own_fingerprint=None, is_reply=is_reply)
    if refusal is not None:
        return refusal[:1].lower() + refusal[1:].rstrip(".")
    return None


def _digest(message: RepMessage) -> str:
    joined = "\x00".join([
        str(message.conference), message.to.casefold(), message.subject, message.body, str(message.reference or ""),
    ])
    return hashlib.sha256(joined.encode("utf-8", errors="replace")).hexdigest()


def _seen(db: Database, user: User, digest: str) -> bool:
    return db.connection.execute(
        "SELECT 1 FROM qwk_rep_seen WHERE user_id = ? AND digest = ?", (user.id, digest)
    ).fetchone() is not None


def _remember(db: Database, user: User, digest: str) -> None:
    db.connection.execute(
        "INSERT OR IGNORE INTO qwk_rep_seen (user_id, digest, seen_at) VALUES (?, ?, ?)",
        (user.id, digest, utc_now_iso()),
    )
    db.connection.commit()


def _trim_seen(db: Database, user: User) -> None:
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=REP_SEEN_DAYS)).isoformat()
    db.connection.execute("DELETE FROM qwk_rep_seen WHERE user_id = ? AND seen_at < ?", (user.id, cutoff))
    db.connection.execute(
        "DELETE FROM qwk_rep_seen WHERE user_id = ? AND digest NOT IN ("
        "SELECT digest FROM qwk_rep_seen WHERE user_id = ? ORDER BY seen_at DESC LIMIT ?)",
        (user.id, user.id, MAX_REP_SEEN_PER_CALLER),
    )
    db.connection.commit()


def apply_rep(db: Database, user: User, data: bytes, *, node_identity=None) -> RepResult:
    """Read `user`'s reply packet and post what it says may be posted.
    Raises `RepError` (nothing used, nothing marked read) when the packet
    can't be read at all, and `QwkError` when `user` may not use offline
    mail. Otherwise the messages earlier packets carried are marked read
    first -- this upload is the caller's word that they read them -- and
    each reply is then posted, sent or refused on its own.

    `node_identity` signs posts onto Linked boards and letters to other
    BBSes; without it (Link off) a Linked board's post stays here, as one
    written online does, and a reply to a letter from another BBS is
    refused."""
    refusal = qwk_refusal(db, user)
    if refusal is not None:
        raise QwkError(refusal)
    packet = parse_rep(data, bbs_id=get_qwk_bbs_id(db), limits=REP_LIMITS)
    marked = mark_sent_read(db, user)
    _trim_seen(db, user)
    by_number = {
        conference.number: conference.board
        for conference in list_conferences(db, user) if conference.number is not None
    }
    outcomes = []
    for message in packet.messages:
        digest = _digest(message)
        if _seen(db, user, digest):
            outcomes.append(ReplyOutcome(
                message.index, message.subject, "", "refused", "an earlier upload already posted it.",
            ))
            continue
        if message.conference == MAIL_CONFERENCE:
            outcome = _send_letter(db, user, message, node_identity=node_identity)
        else:
            outcome = _post_reply(db, user, message, by_number.get(message.conference), node_identity=node_identity)
        if outcome.status != "refused":
            _remember(db, user, digest)
        outcomes.append(outcome)
    notes = _apply_reader_choices(db, user, packet.dropped, packet.added)
    return RepResult(
        outcomes=outcomes,
        problems=[
            (f"message {problem.index}: " if problem.index else "") + problem.reason for problem in packet.problems
        ],
        marked_read=marked,
        notes=notes,
    )


@dataclass(frozen=True)
class RepReport:
    """What an upload came to, for the screen and the browser page alike:
    `ok` false when the packet was not used at all."""

    ok: bool
    title: str
    lines: list[str]
    #: Replies there were, and none of them went out.
    all_refused: bool = False

    def as_page(self) -> dict:
        return {"title": self.title, "lines": self.lines, "ok": self.ok, "all_refused": self.all_refused}


def receive_rep(db: Database, user: User, data: bytes, *, node_identity=None) -> RepReport:
    """`apply_rep`, with a packet that can't be used turned into a report
    rather than an exception -- the form both upload paths show."""
    try:
        result = apply_rep(db, user, data, node_identity=node_identity)
    except (RepError, QwkError) as exc:
        return RepReport(False, "Reply packet not used", [f"{exc}.".replace("..", "."), "Nothing was posted."])
    refused_all = bool(result.outcomes) and result.count("posted", "held", "sent") == 0
    return RepReport(True, "Replies received", [result.summary, *result.lines], all_refused=refused_all)


def receive_rep_file(db: Database, user: User, path, filename: str, *, node_identity=None) -> dict:
    """The browser upload's `receive` (`netbbs.net.file_transfer.
    PrivateTransfer`): the staged file, already bounded by the link's size
    limit, read and applied."""
    del filename  # the BBS ID inside the packet is what counts
    with open(path, "rb") as handle:
        data = handle.read(REP_LIMITS.archive_bytes + 1)
    return receive_rep(db, user, data, node_identity=node_identity).as_page()


def _post_reply(db: Database, user: User, message: RepMessage, board: Board | None, *, node_identity) -> ReplyOutcome:
    subject = message.subject.strip()

    def refused(reason: str, where: str = "") -> ReplyOutcome:
        return ReplyOutcome(message.index, subject, where, "refused", reason)

    if board is None or not _readable(db, user, board):
        return refused(f"conference {message.conference} is not one of your boards.")
    where = sanitize_text(board.name)
    if message.private:
        return refused("a board has no private messages; write it as a letter in conference 0.", where)
    if not subject:
        return refused("it has no subject.", where)
    if not message.body.strip():
        return refused("it has no text.", where)
    parent_post_id = None
    note = None
    if message.reference:
        row = db.connection.execute(
            "SELECT post_id FROM posts WHERE id = ? AND board_id = ? AND post_id = root_post_id",
            (message.reference, board.id),
        ).fetchone()
        parent = visible_post(db, row["post_id"]) if row is not None else None
        if parent is not None and parent.tombstoned_at is None:
            parent_post_id = parent.root_post_id
        else:
            note = "The message it answers is gone, so it starts a new thread."
    reason = _board_refusal(db, user, board, is_reply=parent_post_id is not None)
    if reason is not None:
        return refused(reason + ".", where)
    try:
        post = create_post(db, board, user, subject, message.body, parent_post_id=parent_post_id)
    except (PostError, PollError) as exc:
        return refused(f"{exc}.", where)
    record_post_opened(db, user, board, post)
    if node_identity is not None:
        queue_board_post_if_linked(db, post, board, node_identity=node_identity)
    from netbbs.ftn.scanner import export_post_if_ftn

    export_post_if_ftn(db, post, board)
    if post.status == "pending":
        return ReplyOutcome(message.index, subject, where, "held")
    return ReplyOutcome(message.index, subject, where, "posted", note)


def _sysop_alias(db: Database, name: str) -> str:
    """`SysOp` as a To name: the node's first usable SysOp, unless an
    account is called that (the online To prompt's rule)."""
    if name.strip().lower() != "sysop":
        return name
    try:
        return get_user_by_username(db, name).username
    except AuthError:
        pass
    sysops = [account for account in list_users(db) if is_usable_sysop(account)]
    return min(sysops, key=lambda account: account.id).username if sysops else name


def _send_letter(db: Database, user: User, message: RepMessage, *, node_identity) -> ReplyOutcome:
    """A message in conference 0: a letter. A reply goes back to whoever
    wrote the letter it answers, the way [R]eply in the mailbox does; a
    new letter goes to the account its To names, or to an FTN address."""
    from netbbs.ftn.netmail import is_ftn_recipient, send_netmail

    subject = message.subject.strip()

    def refused(reason: str, where: str = "") -> ReplyOutcome:
        return ReplyOutcome(message.index, subject, where, "refused", reason)

    refusal = mail_access_refusal(db, user)
    if refusal is not None:
        return refused(refusal)
    if not subject:
        return refused("it has no subject.")
    original = None
    if message.reference:
        row = db.connection.execute(
            "SELECT id FROM mail_messages WHERE id = ? AND recipient_user_id = ?", (message.reference, user.id)
        ).fetchone()
        if row is not None:
            from netbbs.mail import get_mail

            original = get_mail(db, user, row["id"])
    try:
        if original is not None:
            if original.from_system:
                return refused("a letter from the BBS itself takes no reply.")
            if original.sender_user_id is None and is_ftn_recipient(original.sender_label):
                send_netmail(db, user, original.sender_label, subject, message.body)
                return ReplyOutcome(message.index, subject, sanitize_text(original.sender_label), "sent")
            link_address = split_link_address(original.sender_label) if original.sender_user_id is None else None
            if link_address is not None:
                return _send_link_letter(db, user, message, original.sender_label, node_identity=node_identity)
            sender = get_user_by_id(db, original.sender_user_id) if original.sender_user_id is not None else None
            if sender is None:
                return refused("the account that wrote the letter it answers no longer exists.")
            send_mail(db, user, sender, subject, message.body)
            return ReplyOutcome(message.index, subject, sanitize_text(sender.username), "sent")
        to = message.to.strip()
        if not to or to.casefold() == "all":
            return refused("a letter needs a To name.")
        if is_ftn_recipient(to):
            send_netmail(db, user, to, subject, message.body)
            return ReplyOutcome(message.index, subject, sanitize_text(to), "sent")
        if "@" in to:
            return refused("a new letter to another BBS is written online; a reply to one works here.")
        try:
            recipient = get_user_by_username(db, _sysop_alias(db, to))
        except AuthError:
            return refused(f"there is no account called {sanitize_text(to)!r} here.")
        send_mail(db, user, recipient, subject, message.body)
        return ReplyOutcome(message.index, subject, sanitize_text(recipient.username), "sent")
    except MailboxFullError:
        return refused("the recipient's mailbox is full.")
    except MailError as exc:
        return refused(f"{exc}.")


def _send_link_letter(
    db: Database, user: User, message: RepMessage, address: str, *, node_identity,
) -> ReplyOutcome:
    from netbbs.link.mail import LinkMailError, compose_link_message
    from netbbs.mail import link_address_display_label
    from netbbs.net.mail_recipients import link_mail_refusal

    subject = message.subject.strip()
    where = sanitize_text(link_address_display_label(db, address))
    if node_identity is None:
        return ReplyOutcome(
            message.index, subject, where, "refused",
            "this BBS is not linked with other BBSes right now, so the reply can't reach them.",
        )
    _user, fingerprint = split_link_address(address)
    refusal = link_mail_refusal(db, fingerprint)
    if refusal is not None:
        return ReplyOutcome(message.index, subject, where, "refused", refusal.sentence)
    try:
        compose_link_message(db, user, address, subject, message.body, node_identity=node_identity)
    except (LinkMailError, MailError) as exc:
        return ReplyOutcome(message.index, subject, where, "refused", f"{exc}.")
    return ReplyOutcome(message.index, subject, where, "sent")


__all__ = [
    "BuiltPacket",
    "MAIL_CONFERENCE",
    "QwkConference",
    "QwkError",
    "RepError",
    "RepResult",
    "ReplyOutcome",
    "apply_rep",
    "build_caller_packet",
    "list_conferences",
    "mark_sent_read",
    "packet_conferences",
    "packet_filename",
    "qwk_refusal",
    "rep_filename",
    "save_conference_choices",
    "sent_count",
]
