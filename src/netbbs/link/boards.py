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
    BOARD_ORIGIN_TRANSFER_OFFER_OBJECT_TYPE,
    BOARD_POST_EDIT_OBJECT_TYPE,
    BoardGenesis,
    BoardOriginTransferAccepted,
    BoardOriginTransferOffer,
    BoardPost,
    BoardPostEdit,
    build_board_genesis,
    build_board_origin_transfer_accepted,
    build_board_origin_transfer_offer,
    build_board_post,
    build_board_post_edit,
    event_content_id,
)
from netbbs.link.node_identity import NodeIdentity, resolve_current_operational_key
from netbbs.link.protocol import LinkNode, PeerRecord
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


@dataclass(frozen=True)
class LinkConfigSnapshot:
    """
    Read-only copy of just the `netbbs.net.nodeconfig.LinkConfig`
    fields issue #60's SysOp Link-status screen needs -- plain
    primitives, not the `LinkConfig` object itself, so this module
    never has to import `netbbs.net.nodeconfig`. `netbbs.link` depends
    on `netbbs.boards` one-way, never the reverse (see this module's
    own docstring); `netbbs.net` is exactly the same kind of reverse
    dependency `LinkContext` must not introduce, since `netbbs.net`
    already imports from `netbbs.link`, not the other way around.
    """

    outgoing_only: bool
    advertised_host: str | None
    advertised_port: int | None
    seeds: tuple[str, ...]
    sync_interval_seconds: float
    relay_serving_enabled: bool
    max_relay_clients: int


@dataclass(frozen=True)
class LinkContext:
    """Everything an in-session `[L]ink this board` command needs,
    bundled as one optional parameter -- same shape as `netbbs.net.
    shutdown.NodeControls`, and for the same reason: threaded through
    `netbbs.net.login_flow`'s whole call chain down to `netbbs.net.
    admin_flow`'s board screens, `None` whenever this node has Link
    disabled (design doc round 87: Phase 3 is opt-in/experimental) or
    when reached via the standalone `netbbs.admin` CLI, which has no
    running `LinkNode` at all.

    `link_config` (issue #60) is `None` under the exact same conditions
    as everything else here -- it additionally stays `None` for callers
    (tests, mainly) that build a `LinkContext` without needing config-
    derived display, since it's optional and additive."""

    node_identity: NodeIdentity
    link_node: LinkNode
    link_config: LinkConfigSnapshot | None = None


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
    forked_from: str | None = None,
) -> BoardGenesis:
    """
    Put `board` into Link scope: build and sign a `board_genesis`
    event referencing its existing `board_id` (design doc round 124 --
    never mints a new one; this is exactly the "promote an existing
    local board" case round 124 confirmed as the normal one, not a
    special one) and persist it on the board's own row.

    `forked_from` (design doc §13, round 94/issue #53) optionally names
    a different, already-Linked board's own `board_id` this one started
    as a copy of -- purely a non-authoritative discoverability pointer,
    see `build_board_genesis`'s own docstring for why nothing here
    verifies or acts on it.

    Raises `LinkBoardsError` if `board` is already Linked -- one
    genesis per board, ever (design doc round 124/94: transfer is a
    separate, later object type, built in round 94/issue #53's own
    implementation -- see `offer_board_origin_transfer`).

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
        forked_from=forked_from,
    )

    db.connection.execute(
        "UPDATE boards SET link_genesis_json = ? WHERE id = ?",
        (json.dumps(genesis.to_dict()), board.id),
    )
    db.connection.commit()

    return genesis


def _board_from_row(row) -> Board:
    """Local row->`Board` mapping, deliberately not reusing `netbbs.
    boards.boards`'s own private `_row_to_board` -- this module already
    talks to the `boards`/`posts` tables directly by raw SQL everywhere
    else (see `link_board`'s own docstring for why `netbbs.link` never
    routes through `netbbs.boards`'s functions), so staying consistent
    with that boundary here too, rather than reaching into another
    module's private helper, is worth the few duplicated lines."""
    return Board(
        id=row["id"], board_id=row["board_id"], name=row["name"], description=row["description"],
        min_read_level=row["min_read_level"], min_write_level=row["min_write_level"],
        category_id=row["category_id"], pinned=bool(row["pinned"]), created_at=row["created_at"],
        moderated=bool(row["moderated"]), max_post_age_days=row["max_post_age_days"],
        min_age=row["min_age"], name_requirement=row["name_requirement"], community_id=row["community_id"],
    )


def materialize_carried_board(db: Database, genesis: BoardGenesis) -> Board:
    """
    Turn a *received* (not self-originated) `board_genesis` into a real,
    locally browsable `Board` row (design doc §13's default-carry
    policy, round 94/issue #53's own prerequisite finding) -- before
    this, a node that merely relayed/stored a peer's `board_genesis`
    had nothing a user could actually read or post to through this
    node's own UI; "carrying" meant holding opaque signed bytes, not a
    usable board. Called once per newly-accepted genesis, from
    `netbbs.link.transport.LinkServer._handle_events` -- never for a
    board this node itself originated (`link_board`'s own genesis never
    passes through `handle_events` at all, see that function's own
    docstring for why).

    Idempotent, keyed on `genesis.payload["board_id"]` -- a resend of an
    already-materialized genesis, or a second peer relaying the same
    genesis this node already carries, returns the existing row
    unchanged rather than raising or duplicating.

    Deliberately bypasses `netbbs.boards.boards.create_board` -- that
    function always mints a fresh content-addressed `board_id` from the
    *local* creator/timestamp, which would silently give this board a
    different `board_id` than its own genesis names, breaking every
    future `board_post`/`board_post_edit` lookup by `board_id`. A direct
    insert instead, using the genesis's own `board_id` verbatim (the
    same reasoning `link_board` already documents for why it never mints
    a new one either).

    The genesis's `default_*` fields seed this new row's own settings
    (matching `link_board`'s reverse-direction reasoning: recommended,
    not binding -- this node's own SysOp can freely change them
    afterward via the ordinary board-edit screen, same as any other
    local board, since a carrying node's own local value always wins
    per `build_board_genesis`'s own docstring).
    """
    existing = db.connection.execute(
        "SELECT * FROM boards WHERE board_id = ?", (genesis.payload["board_id"],)
    ).fetchone()
    if existing is not None:
        return _board_from_row(existing)

    payload = genesis.payload
    db.connection.execute(
        """
        INSERT INTO boards
            (board_id, name, description, min_read_level, min_write_level,
             category_id, pinned, created_at, moderated, max_post_age_days,
             min_age, name_requirement, community_id, link_genesis_json)
        VALUES (?, ?, ?, ?, ?, NULL, 0, ?, ?, ?, ?, ?, NULL, ?)
        """,
        (
            payload["board_id"],
            payload["name"],
            payload.get("description"),
            payload.get("default_min_read_level", 0),
            payload.get("default_min_write_level", 0),
            payload["created_at"],
            int(payload.get("default_moderated", False)),
            payload.get("default_max_post_age_days"),
            payload.get("default_min_age"),
            payload.get("default_name_requirement"),
            json.dumps(genesis.to_dict()),
        ),
    )
    db.connection.commit()

    return _board_from_row(
        db.connection.execute("SELECT * FROM boards WHERE board_id = ?", (payload["board_id"],)).fetchone()
    )


def board_origin_fingerprint(db: Database, board: Board) -> str:
    """
    The fingerprint currently authoritative for `board` (design doc §13,
    round 94/issue #53) -- checks the mutable `link_origin_fingerprint`
    override column first (only ever written once a `board_origin_
    transfer_accepted` is verified/accepted, whether this node was a
    party to that transfer or merely witnessed it -- see `netbbs.link.
    transport.LinkServer._handle_events`), falling back to the
    immutable genesis's own `origin_fingerprint` claim when no transfer
    has ever completed for this board. Never read either column alone.

    Raises `LinkBoardsError` if `board` isn't Linked at all -- there is
    no origin to resolve for a purely local board.
    """
    row = db.connection.execute(
        "SELECT link_origin_fingerprint, link_genesis_json FROM boards WHERE id = ?", (board.id,)
    ).fetchone()
    if row is None or row["link_genesis_json"] is None:
        raise LinkBoardsError(f"board {board.name!r} is not Linked")
    if row["link_origin_fingerprint"] is not None:
        return row["link_origin_fingerprint"]
    genesis = BoardGenesis.from_dict(json.loads(row["link_genesis_json"]))
    return genesis.payload["origin_fingerprint"]


def _current_lifecycle_head(db: Database, board: Board) -> str:
    """The content_id a *new* lifecycle event (an offer or an
    acceptance) for `board` must reference as its own `previous_event_
    id` -- the latest self-originated lifecycle event on file
    (`link_lifecycle_json`) if one exists, else the board's own genesis.
    Deliberately local-row-only: verifying a *received* lifecycle event
    against the network's actual current head is `netbbs.link.protocol.
    LinkNode`'s own job (it tracks `board_lifecycle_head` from every
    accepted event, not just this node's own), not this function's."""
    row = db.connection.execute(
        "SELECT link_genesis_json, link_lifecycle_json FROM boards WHERE id = ?", (board.id,)
    ).fetchone()
    if row["link_lifecycle_json"] is not None:
        return event_content_id(json.loads(row["link_lifecycle_json"])["envelope"])
    return BoardGenesis.from_dict(json.loads(row["link_genesis_json"])).content_id


def offer_board_origin_transfer(
    db: Database, board: Board, *, node_identity: NodeIdentity, new_origin_fingerprint: str,
) -> BoardOriginTransferOffer:
    """
    Build, sign, and persist a `board_origin_transfer_offer` proposing
    `new_origin_fingerprint` as `board`'s next origin (design doc §13,
    round 94/issue #53) -- the current origin's half of the mutual-
    consent handoff; see `BoardOriginTransferOffer`'s own docstring for
    why this alone changes nothing until a matching acceptance is seen.

    Raises `LinkBoardsError` if this node isn't currently `board`'s own
    origin (only the current origin may propose handing it off), or if
    an offer for this board is already outstanding -- at most one may
    be in flight at a time, a known, accepted limitation of this slice
    (see `BoardOriginTransferOffer`'s own docstring), not a gap found
    late.
    """
    current_origin = board_origin_fingerprint(db, board)
    if current_origin != node_identity.fingerprint:
        raise LinkBoardsError(f"this node is not board {board.name!r}'s current origin")

    row = db.connection.execute(
        "SELECT link_lifecycle_json FROM boards WHERE id = ?", (board.id,)
    ).fetchone()
    if row["link_lifecycle_json"] is not None:
        pending = json.loads(row["link_lifecycle_json"])
        if pending["envelope"]["object_type"] == BOARD_ORIGIN_TRANSFER_OFFER_OBJECT_TYPE:
            raise LinkBoardsError(f"board {board.name!r} already has an outstanding, unaccepted transfer offer")

    offer = build_board_origin_transfer_offer(
        signing_identity=node_identity.signing_key,
        board_id=board.board_id,
        previous_event_id=_current_lifecycle_head(db, board),
        old_origin_fingerprint=current_origin,
        new_origin_fingerprint=new_origin_fingerprint,
        created_at=utc_now_iso(),
    )

    db.connection.execute(
        "UPDATE boards SET link_lifecycle_json = ? WHERE id = ?",
        (json.dumps(offer.to_dict()), board.id),
    )
    db.connection.commit()

    return offer


def accept_board_origin_transfer(
    db: Database, board: Board, *, node_identity: NodeIdentity, offer: BoardOriginTransferOffer,
) -> BoardOriginTransferAccepted:
    """
    Build, sign, and persist a `board_origin_transfer_accepted` for
    `offer` (design doc §13, round 94/issue #53) -- the new origin's
    consent-completing half; see `BoardOriginTransferAccepted`'s own
    docstring for why nothing else about the board's authority changes
    until this specific event is seen. Immediately records this node's
    own new origin status locally too (`record_board_origin_change`),
    since a node accepting its own offer already knows the outcome
    without waiting to see its own event echoed back.

    Raises `LinkBoardsError` if `offer` doesn't actually name this node
    as the proposed new origin -- accepting somebody else's offer makes
    no sense and would build an event no other node would ever verify
    anyway (`netbbs.link.protocol.LinkNode.handle_events` independently
    enforces the same check on the receiving end).
    """
    if offer.payload.get("new_origin_fingerprint") != node_identity.fingerprint:
        raise LinkBoardsError("this offer does not name this node as the proposed new origin")

    accepted = build_board_origin_transfer_accepted(
        signing_identity=node_identity.signing_key,
        board_id=board.board_id,
        previous_event_id=offer.content_id,
        new_origin_fingerprint=node_identity.fingerprint,
        created_at=utc_now_iso(),
    )

    db.connection.execute(
        "UPDATE boards SET link_lifecycle_json = ? WHERE id = ?",
        (json.dumps(accepted.to_dict()), board.id),
    )
    db.connection.commit()
    record_board_origin_change(db, board.board_id, node_identity.fingerprint)

    return accepted


def record_board_origin_change(db: Database, board_id: str, new_origin_fingerprint: str) -> None:
    """
    Update the locally-materialized board's own `link_origin_fingerprint`
    override to `new_origin_fingerprint` (design doc §13, round 94/issue
    #53) -- called for *every* verified `board_origin_transfer_accepted`
    this node ever sees, whether this node was the accepting party
    (`accept_board_origin_transfer` calls this itself) or merely a
    carrying bystander witnessing a transfer between two other nodes
    (`netbbs.link.transport.LinkServer._handle_events` calls this for
    every such accepted event generically). Without the bystander case,
    a carrying node's own local view of "who currently owns this board"
    would silently go stale the moment it wasn't a direct party to a
    transfer -- `netbbs.link.protocol.LinkNode.board_origin` already
    tracks this correctly in memory for verification purposes; this is
    that same fact, persisted so the local admin UI reflects it too.

    A silent no-op if this node has no local row for `board_id` at all
    (defensive only -- carry-materialization means this shouldn't
    normally happen for a board whose transfer this node could even
    verify, since verifying requires knowing about the board's genesis
    in the first place).
    """
    db.connection.execute(
        "UPDATE boards SET link_origin_fingerprint = ? WHERE board_id = ?",
        (new_origin_fingerprint, board_id),
    )
    db.connection.commit()


def is_board_origin_orphaned(peer: PeerRecord) -> bool:
    """
    Whether `peer` (a board's current origin) has no currently-
    authorized signing key left -- their most recent `key_transition`
    for the `signing` purpose is an unreplaced `revoke` (design doc §13,
    round 94/issue #53). Purely a computed property of data this node
    already has from directly hello-ing `peer` at some point (verifying
    their `board_genesis`/lifecycle events requires exactly that) --
    no new event type, no network-wide signal (round 94's own framing:
    "no cryptographic proof an origin is gone versus merely offline," so
    no single node's observation gets an automatic network-wide
    effect). A board whose origin is orphaned keeps existing exactly as
    last known -- it simply accepts no further origin-authorized events
    (a fresh transfer offer, most concretely) from that origin, since
    nothing could validly sign one anymore.
    """
    return resolve_current_operational_key(
        peer.transitions, root_verify_key=peer.root_verify_key, subject_fingerprint=peer.fingerprint, purpose="signing",
    ) is None


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


def load_own_board_events(
    db: Database, own_fingerprint: str
) -> list[BoardGenesis | BoardPost | BoardPostEdit | BoardOriginTransferOffer | BoardOriginTransferAccepted]:
    """
    This node's own originated `board_genesis`/`board_post`/`board_
    post_edit`/`board_origin_transfer_offer`/`board_origin_transfer_
    accepted` events, read directly off the `boards`/`posts` tables'
    own columns -- what `netbbs.link.sync`'s push loop sends to every
    seed every pass, mirroring how `node.identity.transitions` is
    already re-pushed in full regardless of per-peer delivery history
    (round 119's "harmless no-op" model) rather than tracked per-peer
    here either.

    `own_fingerprint` (design doc round 94/issue #53's own finding,
    required since `materialize_carried_board` started this round) is
    needed now to tell a board this node actually *originated* apart
    from one it merely *carries*: both populate `boards.link_genesis_
    json`, but only the former is this node's own to re-push -- a
    carried board's genesis was signed by (and belongs to) its own
    origin, not this node, and round 116's "no relay from a stranger"
    scope note means this node re-pushing it as if it were its own
    would misrepresent who actually originated it. `link_lifecycle_
    json` (an offer or acceptance) has no such ambiguity -- it's only
    ever populated by this node's own `offer_board_origin_transfer`/
    `accept_board_origin_transfer` calls in the first place, always
    self-originated by construction.

    `posts.link_event_json` holds a `board_post` on a root row or a
    `board_post_edit` on a later revision row (round 130) -- decided by
    peeking at the stored envelope's own `object_type`, not by which
    row it came from, since that's already unambiguous and avoids a
    second local-schema-dependent branch.
    """
    events: list[BoardGenesis | BoardPost | BoardPostEdit | BoardOriginTransferOffer | BoardOriginTransferAccepted] = []
    for row in db.connection.execute(
        "SELECT link_genesis_json, link_lifecycle_json FROM boards WHERE link_genesis_json IS NOT NULL"
    ):
        genesis = BoardGenesis.from_dict(json.loads(row["link_genesis_json"]))
        if genesis.payload["origin_fingerprint"] == own_fingerprint:
            events.append(genesis)
        if row["link_lifecycle_json"] is not None:
            raw = json.loads(row["link_lifecycle_json"])
            if raw["envelope"]["object_type"] == BOARD_ORIGIN_TRANSFER_OFFER_OBJECT_TYPE:
                events.append(BoardOriginTransferOffer.from_dict(raw))
            else:
                events.append(BoardOriginTransferAccepted.from_dict(raw))
    for row in db.connection.execute(
        "SELECT link_event_json FROM posts WHERE link_event_json IS NOT NULL"
    ):
        raw = json.loads(row["link_event_json"])
        if raw["envelope"]["object_type"] == BOARD_POST_EDIT_OBJECT_TYPE:
            events.append(BoardPostEdit.from_dict(raw))
        else:
            events.append(BoardPost.from_dict(raw))
    return events
