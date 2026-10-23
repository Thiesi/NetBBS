"""
`[T]o boards`: one new post on several message boards (issue #1298, design
doc §6.1).

The review screen of a new post -- prose or art, never a reply or an edit --
offers `[T]o boards` (`board_flow._review_loop`). It opens a multi-select
picker of the boards the caller may read, the one the post was written on
ticked. A board the caller cannot post on is listed muted with the reason
and cannot be ticked, as the mail picker shows who cannot take a letter
(#920), and so is a board without color for an art post. The choice lives on
the review screen until `[P]ost`; nothing is saved before it.

`[P]ost` writes one ordinary copy per board (`netbbs.boards.posts.
create_crosspost`), all or nothing, and each copy then leaves the node the way
a post on its board does: a Linked copy as its own `board_post` carrying the
group id, an FTN copy as its own echomail message.

Everything here is synchronous on the session's `db`, as the rest of the
board screens are, except the picker itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from netbbs.attestation import meets_name_requirement
from netbbs.auth.users import User
from netbbs.boards.boards import Board
from netbbs.boards.limits import MAX_BODY_BYTES
from netbbs.boards.posts import Post, create_crosspost
from netbbs.communities import (
    get_effective_min_write_level,
    get_effective_name_requirement,
    meets_read_gate,
    meets_resource_age,
    meets_write_gate,
    resource_age_visible,
)
from netbbs.config import get_crosspost_board_limit
from netbbs.file_refs import FileRef, body_with_link_text
from netbbs.link.boards import is_board_closed, is_board_linked, posting_refusal
from netbbs.net.composition import characters_over
from netbbs.net.notices import announce
from netbbs.net.picker import pick_item
from netbbs.net.session import Session
from netbbs.rendering import LABEL_COLOR, MUTED_COLOR, colored, sanitize_text
from netbbs.rendering.menu import menu_key
from netbbs.storage.database import Database

#: The review-screen key that chooses the boards.
CROSSPOST_KEY = "t"


@dataclass
class CrosspostTargets:
    """The boards a new post goes to, held by its review screen: `home`, the
    board it was written on, always first, then the others ticked so far."""

    home: Board
    layout: str
    own_fingerprint: str | None
    boards: list[Board] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.boards:
            self.boards = [self.home]

    @property
    def crossposted(self) -> bool:
        return len(self.boards) > 1


def crosspost_action() -> tuple[str, str, str | None]:
    """`[T]o boards`, as `review_composition`'s `extra_actions` entry."""
    return (CROSSPOST_KEY, menu_key("T", "o boards"), "Post this on other message boards too")


def crosspost_rows(targets: CrosspostTargets, *, accent: int) -> list[str]:
    """The review screen's row under the subject naming the boards, once
    there is more than the one it was written on."""
    if not targets.crossposted:
        return []
    names = ", ".join(sanitize_text(board.name) for board in targets.boards)
    return [colored("  Boards: ", fg_color=LABEL_COLOR) + colored(names, fg_color=accent)]


def crosspost_refusal(
    db: Database, user: User, board: Board, *, layout: str, own_fingerprint: str | None,
) -> str | None:
    """Why `user` cannot send a new post to `board`, as the picker shows it,
    or `None`: the checks `[P]ost` makes on a board's own screen."""
    if is_board_closed(db, board):
        return "closed to new posts"
    if not meets_write_gate(db, user, board):
        return f"posting needs level {get_effective_min_write_level(db, board)}"
    if not meets_name_requirement(db, user, get_effective_name_requirement(db, board)):
        return "posting needs a verified name"
    if not meets_resource_age(db, user, board):
        return "posting has an age requirement you do not meet"
    from netbbs.ftn.scanner import real_name_refusal

    # Issue #1443: a board sending real names to its FTN network.
    real_name_problem = real_name_refusal(db, board, user, short=True)
    if real_name_problem is not None:
        return real_name_problem
    refusal = posting_refusal(db, board, own_fingerprint=own_fingerprint, is_reply=False)
    if refusal is not None:
        return "only the board's origin node starts threads here"
    if layout == "art" and not board.allow_color:
        return "no color here, so no art posts"
    return None


def _post_refusal(
    db: Database, user: User, board: Board, *, targets: CrosspostTargets, body: str, files: list[FileRef],
) -> str | None:
    """`crosspost_refusal`, plus a Linked board's length with the file lines
    other nodes get (issue #842), checked at `[P]ost`."""
    refusal = crosspost_refusal(db, user, board, layout=targets.layout, own_fingerprint=targets.own_fingerprint)
    if refusal is not None:
        return refusal
    if files and is_board_linked(db, board) and characters_over(body_with_link_text(db, body, files), MAX_BODY_BYTES):
        return "with the file lines added for other BBSes, the post is too long for this Linked board"
    return None


@dataclass(frozen=True)
class _Choice:
    board: Board
    refusal: str | None


def _choices(db: Database, user: User, targets: CrosspostTargets) -> list[_Choice]:
    """Every board `user` may read, in the SysOp's order, each with why it
    cannot take the post. The home board is always one they may post on."""
    from netbbs.boards.boards import list_boards

    choices = []
    for board in list_boards(db):
        if board.id == targets.home.id:
            choices.append(_Choice(board, None))
            continue
        if not (meets_read_gate(db, user, board) and resource_age_visible(db, user, board)):
            continue
        choices.append(_Choice(board, crosspost_refusal(
            db, user, board, layout=targets.layout, own_fingerprint=targets.own_fingerprint,
        )))
    return choices


async def choose_crosspost_boards(
    session: Session, db: Database, user: User, targets: CrosspostTargets, *, style: dict,
) -> None:
    """`[T]o boards`: tick and untick boards in a picker, changing
    `targets.boards`. The home board stays ticked; a tick past the SysOp's
    cap is refused with a line naming it."""
    cap = get_crosspost_board_limit(db)
    choices = _choices(db, user, targets)
    linked = {choice.board.id for choice in choices if is_board_linked(db, choice.board)}
    chosen = {board.id for board in targets.boards}
    order = {choice.board.id: index for index, choice in enumerate(choices)}

    async def toggle(choice: _Choice) -> None:
        board = choice.board
        if board.id == targets.home.id:
            announce(
                session, f"The post always goes to {sanitize_text(board.name)}, where you wrote it.", tone="muted",
            )
            return
        if board.id in chosen:
            chosen.discard(board.id)
            return
        if len(chosen) >= cap:
            announce(
                session,
                f"A post can go to {cap} message board{'' if cap == 1 else 's'} at most, this one included.",
                tone="error",
            )
            return
        chosen.add(board.id)

    def describe(choice: _Choice) -> str | None:
        if choice.refusal is not None:
            return choice.refusal
        if choice.board.id == targets.home.id:
            return "written here"
        return choice.board.description or None

    await pick_item(
        session, choices,
        name_of=lambda choice: choice.board.name,
        stable_id_of=lambda choice: choice.board.id,
        name_segments_of=lambda choice: (
            ("[x] " if choice.board.id in chosen else "[ ] ", MUTED_COLOR),
            (sanitize_text(choice.board.name), style.get("accent_color")),
        ),
        description_of=describe,
        selectable_of=lambda choice: choice.refusal is None,
        linked_of=lambda choice: choice.board.id in linked,
        title="Post to which message boards?",
        empty_message="There are no message boards you can read.",
        preamble=(
            f"Tick each board the post goes to, {cap} at most with this one. [B]ack returns to the review; "
            "nothing is posted before [P]ost."
        ),
        live_label=lambda: f"{len(chosen)} of {cap} chosen",
        start_stable_id=targets.home.id,
        on_toggle=toggle,
        **style,
    )
    boards_by_id = {choice.board.id: choice.board for choice in choices}
    targets.boards = [targets.home] + sorted(
        (boards_by_id[board_id] for board_id in chosen if board_id != targets.home.id and board_id in boards_by_id),
        key=lambda board: order[board.id],
    )


def publish_crosspost(
    session: Session, db: Database, user: User, targets: CrosspostTargets, *,
    subject: str, body: str, files: list[FileRef] | None, link_context,
) -> list[Post] | None:
    """`[P]ost` for a post going to several boards: every copy written, or
    none with the refusing board named on the review screen (`None`). Each
    copy then leaves the node as a post on its board does."""
    from netbbs.activity import record_post_opened
    from netbbs.boards.posts import CrosspostRefused, PostError
    from netbbs.ftn.scanner import export_post_if_ftn
    from netbbs.net.shared_account import note_created_this_call
    from netbbs.qwknet.exchange import queue_post_if_qwknet
    from netbbs.link.boards import queue_board_post_if_linked

    files = list(files or [])
    try:
        posts = create_crosspost(
            db, targets.boards, user, subject, body, layout=targets.layout, files=files,
            refusal_of=lambda board: _post_refusal(db, user, board, targets=targets, body=body, files=files),
        )
    except CrosspostRefused as exc:
        announce(
            session,
            f"Nothing was posted: {sanitize_text(exc.board.name)} refused it ({exc.reason}). "
            "Take it off with [T]o boards, or change the post.",
            tone="error",
        )
        return None
    except PostError as exc:
        announce(session, f"Could not create post: {exc}", tone="muted")
        return None
    for board, post in zip(targets.boards, posts):
        note_created_this_call(session, "post", post.root_post_id)
        # A caller's own post is not news to them on any board (issue #710).
        record_post_opened(db, user, board, post)
        if link_context is not None:
            queue_board_post_if_linked(db, post, board, node_identity=link_context.node_identity)
        export_post_if_ftn(db, post, board)
        queue_post_if_qwknet(db, post, board)  # issue #1481
    posted = [sanitize_text(board.name) for board, post in zip(targets.boards, posts) if post.status != "pending"]
    held = [sanitize_text(board.name) for board, post in zip(targets.boards, posts) if post.status == "pending"]
    if not held:
        announce(session, f"Posted to {', '.join(posted)}.")
    elif not posted:
        announce(session, f"Submitted to {', '.join(held)}. Others will see it once a moderator approves it.")
    else:
        announce(
            session,
            f"Posted to {', '.join(posted)}. {', '.join(held)} "
            f"{'holds' if len(held) == 1 else 'hold'} it until a moderator approves it.",
        )
    return posts
