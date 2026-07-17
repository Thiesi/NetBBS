"""
Local-origination bridge for linked boards (design doc round 124,
round 128 wiring) -- turns an existing local board/post into a signed
`board_genesis`/`board_post` Link event, persists it on the board's/
post's own row, and (for a board's genesis specifically) registers it
with the running node so it's reachable both ways: recognized when a
remote user's `board_post` for it arrives, and pushed out to peers by
`netbbs.link.sync` like any other of this node's own originated events.

Deliberately lives here, not in `netbbs.boards` -- `netbbs.boards` is
Phase 1/2 scope with zero Link dependency (a standalone, non-Link node
must never pull in Phase 3 code just by using its board_ package);
`netbbs.link` already depends on `netbbs.boards` one-way (`netbbs.link.
events` already reuses its content-ID canonicalization), never the
reverse. The two real call sites that create/approve a post
(`netbbs.net.login_flow`, `netbbs.net.admin_flow`) call `queue_board_
post_if_linked` themselves, explicitly, right after the local
operation succeeds (design doc round 128, confirmed with Thiesi) --
`netbbs.boards.posts` itself stays untouched.

Every function here is plain and synchronous, `db`-first -- the same
calling convention `netbbs.link.store`'s persistence functions already
use, dispatched via `DatabaseLane.run` from async call sites. None of
them mutate `netbbs.link.protocol.LinkNode` directly (see `link_board`'s
own docstring for why that step belongs to the caller instead, on the
event loop, never inside a lane-dispatched function body).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from netbbs.auth.users import User
from netbbs.boards.boards import Board
from netbbs.boards.posts import Post
from netbbs.link.events import (
    BOARD_POST_EDIT_OBJECT_TYPE,
    BoardGenesis,
    BoardPost,
    BoardPostEdit,
    build_board_genesis,
    build_board_post,
    build_board_post_edit,
    event_content_id,
)
from netbbs.link.node_identity import NodeIdentity
from netbbs.link.protocol import LinkNode
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


@dataclass(frozen=True)
class LinkContext:
    """Everything an in-session `[L]ink this board` command needs,
    bundled as one optional parameter -- same shape as `netbbs.net.
    shutdown.NodeControls`, and for the same reason: threaded through
    `netbbs.net.login_flow`'s whole call chain down to `netbbs.net.
    admin_flow`'s board screens, `None` whenever this node has Link
    disabled (design doc round 87: Phase 3 is opt-in/experimental) or
    when reached via the standalone `netbbs.admin` CLI, which has no
    running `LinkNode` at all."""

    node_identity: NodeIdentity
    link_node: LinkNode


class LinkBoardsError(Exception):
    """Raised for re-Linking an already-Linked board."""


def is_board_linked(db: Database, board: Board) -> bool:
    """Whether `board` already has a `board_genesis` on file -- the
    single source of truth for "is this board Linked," queried
    directly against its row rather than duplicated into the `Board`
    dataclass itself (design doc round 124/128: `netbbs.boards` stays
    Link-unaware)."""
    row = db.connection.execute(
        "SELECT link_genesis_json FROM boards WHERE id = ?", (board.id,)
    ).fetchone()
    return row is not None and row["link_genesis_json"] is not None


def link_board(
    db: Database,
    board: Board,
    *,
    node_identity: NodeIdentity,
    description: str | None = None,
    default_min_read_level: int | None = None,
    default_min_write_level: int | None = None,
    default_moderated: bool | None = None,
    default_max_post_age_days: int | None = None,
    default_min_age: int | None = None,
    default_name_requirement: str | None = None,
) -> BoardGenesis:
    """
    Put `board` into Link scope: build and sign a `board_genesis`
    event referencing its existing `board_id` (design doc round 124 --
    never mints a new one; this is exactly the "promote an existing
    local board" case round 124 confirmed as the normal one, not a
    special one) and persist it on the board's own row.

    Raises `LinkBoardsError` if `board` is already Linked -- one
    genesis per board, ever, this round (design doc round 124: closure/
    transfer are separately-scoped later object types, not built here).

    Deliberately does **not** register the result with a live
    `netbbs.link.protocol.LinkNode` -- that mutation must happen on the
    event loop directly, the same division of responsibility `netbbs.
    link.transport`'s `_handle_hello`/`_handle_events` already use
    (`LinkNode` mutation always direct, persistence always via a
    separate `DatabaseLane.run` call) -- doing it here instead, inside
    a lane-dispatched function running on the lane's own worker thread,
    would mutate `LinkNode.boards`/`known_event_ids`/`events` from a
    thread other than the one `handle_events`/`netbbs.link.sync`
    already read and write it from, a real data race. Callers (the
    `[L]ink` admin command) do this themselves, right after this
    function returns.
    """
    if is_board_linked(db, board):
        raise LinkBoardsError(f"board {board.name!r} is already Linked")

    genesis = build_board_genesis(
        signing_identity=node_identity.signing_key,
        origin_fingerprint=node_identity.fingerprint,
        board_id=board.board_id,
        name=board.name,
        created_at=utc_now_iso(),
        description=description if description is not None else board.description,
        default_min_read_level=default_min_read_level,
        default_min_write_level=default_min_write_level,
        default_moderated=default_moderated,
        default_max_post_age_days=default_max_post_age_days,
        default_min_age=default_min_age,
        default_name_requirement=default_name_requirement,
    )

    db.connection.execute(
        "UPDATE boards SET link_genesis_json = ? WHERE id = ?",
        (json.dumps(genesis.to_dict()), board.id),
    )
    db.connection.commit()

    return genesis


def queue_board_post_if_linked(
    db: Database,
    post: Post,
    board: Board,
    *,
    node_identity: NodeIdentity,
) -> BoardPost | None:
    """
    If `board` is Linked and `post` is currently `'approved'`, build
    and sign a `board_post` event for it and store it on the post's
    own row for `netbbs.link.sync` to push -- a no-op returning `None`
    otherwise (design doc round 124: a board_post is only ever built
    once a post reaches `'approved'`, never while still `'pending'` --
    a moderated board's queue must never leak onto the Link before its
    own moderation acts on it).

    Idempotent: a post that already has a queued event returns it
    as-is rather than building (and re-signing, with a fresh `nonce`
    and `created_at`) a second, different one for the same logical
    post -- called from both of the two places a post can reach
    `'approved'` (`netbbs.boards.posts.create_post` on an unmoderated
    board, `approve_post` on a moderated one), so a caller never needs
    to know which path got it there.

    Only the `node_vouched_user` author tier is built (design doc round
    124) -- `local_user_id` is always `post.author_label` (the plain
    username), never `post.author_fingerprint`, regardless of whether
    the author happens to hold a personal keypair: that fingerprint is
    a local display/attestation detail (§18), not a signal that `user_
    key`-tier signing is available, which this round doesn't build
    (see `build_board_post`'s own docstring for why).

    `parent_post_id` is set only if `post`'s local parent (if any) is
    *itself* already Linked (has its own queued `board_post`) -- a
    reply to a post that predates this board going Linked has no
    Link-native parent to point at (design doc round 124: pre-Link
    history lives in an incompatible ID space, never backfilled), so
    it's queued as a Link-native top-level post instead of silently
    dropping the reply relationship or referencing an ID nothing else
    can resolve.
    """
    if post.status != "approved":
        return None
    if not is_board_linked(db, board):
        return None

    existing = db.connection.execute(
        "SELECT link_event_json FROM posts WHERE post_id = ?", (post.post_id,)
    ).fetchone()
    if existing is not None and existing["link_event_json"] is not None:
        return BoardPost.from_dict(json.loads(existing["link_event_json"]))

    link_parent_post_id: str | None = None
    if post.parent_post_id is not None:
        parent_row = db.connection.execute(
            "SELECT link_event_json FROM posts WHERE post_id = ?", (post.parent_post_id,)
        ).fetchone()
        if parent_row is not None and parent_row["link_event_json"] is not None:
            parent_post = BoardPost.from_dict(json.loads(parent_row["link_event_json"]))
            link_parent_post_id = parent_post.content_id

    board_post = build_board_post(
        signing_identity=node_identity.signing_key,
        home_node_fingerprint=node_identity.fingerprint,
        local_user_id=post.author_label,
        board_id=board.board_id,
        subject=post.subject,
        body=post.body,
        created_at=post.created_at,
        parent_post_id=link_parent_post_id,
    )

    db.connection.execute(
        "UPDATE posts SET link_event_json = ? WHERE post_id = ?",
        (json.dumps(board_post.to_dict()), post.post_id),
    )
    db.connection.commit()

    return board_post


def queue_board_post_edit_if_linked(
    db: Database,
    edited_post: Post,
    board: Board,
    *,
    node_identity: NodeIdentity,
    edited_by: User,
) -> BoardPostEdit | None:
    """
    If `board` is Linked, `edited_post` is currently `'approved'`, and
    `edited_by` is the post's *original author* (design doc round 129:
    moderator edits aren't propagated this round -- the local model
    has no author-bypass for `delete_post` at all, so a tombstone has
    no honest simple slice either, and a moderator edit needs grant
    verification that doesn't exist yet), build and sign a `board_post_
    edit` for it and store it on the edited revision's own row.

    `netbbs.boards.posts.edit_post` copies `author_user_id` forward
    unchanged across every revision regardless of who actually
    performed a given edit -- `edited_by` is the only place "who edited
    this, this time" is actually known, which is why it's a required
    parameter here rather than inferred from `edited_post` itself.

    Requires an **unbroken** local chain back to a Linked root: both
    the root post (`edited_post.root_post_id`) and the immediate local
    predecessor (`edited_post.edit_of_post_id`) must already have their
    own queued Link event, or this returns `None` rather than guessing
    at a `previous_event_id` to reference. A gap appears only if the
    board was Linked partway through a post's edit history (some
    revisions predate Linking) -- an edge case this round accepts
    rather than solves, the same "no backfill" shape round 124 already
    applies to pre-Link posts generally.

    `author` is copied **verbatim** from the root `board_post`'s own
    payload (never reconstructed) -- guarantees the "self-authored
    only" check `netbbs.link.protocol.handle_events` performs on the
    receiving end actually matches by construction.
    """
    if edited_post.status != "approved":
        return None
    if not is_board_linked(db, board):
        return None
    if edited_by.id != edited_post.author_user_id:
        return None

    existing = db.connection.execute(
        "SELECT link_event_json FROM posts WHERE post_id = ?", (edited_post.post_id,)
    ).fetchone()
    if existing is not None and existing["link_event_json"] is not None:
        return BoardPostEdit.from_dict(json.loads(existing["link_event_json"]))

    root_row = db.connection.execute(
        "SELECT link_event_json FROM posts WHERE post_id = ?", (edited_post.root_post_id,)
    ).fetchone()
    if root_row is None or root_row["link_event_json"] is None:
        return None
    root_post = BoardPost.from_dict(json.loads(root_row["link_event_json"]))

    predecessor_row = db.connection.execute(
        "SELECT link_event_json FROM posts WHERE post_id = ?", (edited_post.edit_of_post_id,)
    ).fetchone()
    if predecessor_row is None or predecessor_row["link_event_json"] is None:
        return None
    previous_event_id = event_content_id(json.loads(predecessor_row["link_event_json"])["envelope"])

    edit = build_board_post_edit(
        signing_identity=node_identity.signing_key,
        author=root_post.payload["author"],
        board_id=board.board_id,
        root_post_id=root_post.content_id,
        previous_event_id=previous_event_id,
        subject=edited_post.subject,
        body=edited_post.body,
        created_at=edited_post.created_at,
    )

    db.connection.execute(
        "UPDATE posts SET link_event_json = ? WHERE post_id = ?",
        (json.dumps(edit.to_dict()), edited_post.post_id),
    )
    db.connection.commit()

    return edit


def load_own_board_events(db: Database) -> list[BoardGenesis | BoardPost | BoardPostEdit]:
    """
    This node's own originated `board_genesis`/`board_post`/`board_
    post_edit` events, read directly off the `boards`/`posts` tables'
    own columns -- what `netbbs.link.sync`'s push loop sends to every
    seed every pass, mirroring how `node.identity.transitions` is
    already re-pushed in full regardless of per-peer delivery history
    (round 119's "harmless no-op" model) rather than tracked per-peer
    here either.

    `posts.link_event_json` holds a `board_post` on a root row or a
    `board_post_edit` on a later revision row (round 130) -- decided by
    peeking at the stored envelope's own `object_type`, not by which
    row it came from, since that's already unambiguous and avoids a
    second local-schema-dependent branch.
    """
    events: list[BoardGenesis | BoardPost | BoardPostEdit] = []
    for row in db.connection.execute(
        "SELECT link_genesis_json FROM boards WHERE link_genesis_json IS NOT NULL"
    ):
        events.append(BoardGenesis.from_dict(json.loads(row["link_genesis_json"])))
    for row in db.connection.execute(
        "SELECT link_event_json FROM posts WHERE link_event_json IS NOT NULL"
    ):
        raw = json.loads(row["link_event_json"])
        if raw["envelope"]["object_type"] == BOARD_POST_EDIT_OBJECT_TYPE:
            events.append(BoardPostEdit.from_dict(raw))
        else:
            events.append(BoardPost.from_dict(raw))
    return events
