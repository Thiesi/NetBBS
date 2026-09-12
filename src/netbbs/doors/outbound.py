"""
A door's outbound hook: a per-door, SysOp-enabled allowlist of boards the
door may post to, and a file-drop protocol it uses to do so (issue #520,
from Blacksite upstream request #470).

**This is not containment, and nothing here should be read as if it were.**
A native door already runs as the BBS user with the node database on disk
beside it, so this grants a door no authority it did not already have. What
it buys is three things worth having anyway: a supported, stable interface,
so doors do not reach into the schema and a schema change does not silently
break every door that did; an audit trail that names the door; and one
SysOp-visible switch, off by default. That is the house rule -- we provide
the interface, the SysOp owns the trust decision -- not a sandbox.

**Identity is a label, not an account.** See
`netbbs.boards.posts.create_labelled_post` for why an account was the wrong
answer and what NetBBS already had instead. The label is minted when a SysOp
switches outbound on, stored, and unique: it must never collide with a real
username, because `netbbs.net.chat_flow._resolve_message_author` resolves a
stored author by *username* where boards resolve by id, so a colliding label
would let a door speak in a real account's nick and verified-name styling.

**The transport is a file drop, and DOS doors decided that.** A socket would
need a listener, an authentication story and a second passed fd -- and it
cannot cross the DOSBox emulator boundary at all, so a DOS door could never
use one. A file can. The drop directory also needs no authentication of its
own: it belongs to one door session, which is the same basis
`door_info.json` already rests on.

**A refusal is always visible and never queued.** Holding a door's post to
publish later means publishing it after a SysOp has revoked the allowlist,
which is the exact surprise the switch exists to prevent. Every request gets
a result file saying what happened and why.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from netbbs.auth.users import DOOR_LABEL_SUFFIX, User, get_user_by_id
from netbbs.boards.boards import Board, _row_to_board
from netbbs.boards.posts import PostError, create_labelled_post
from netbbs.moderation.log import record_action
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

_logger = logging.getLogger(__name__)

#: The drop directory inside a door's per-session working directory. A door
#: writes `<name>.part` and renames it to `<name>.json`; anything not
#: matching the final pattern is ignored, so a half-written request is never
#: read. Same temp-then-rename discipline the runtime's own terminal-size
#: republish uses, in the other direction.
OUTBOUND_DIRNAME = "outbound"
_REQUEST_SUFFIX = ".json"
_RESULT_SUFFIX = ".result.json"

#: Default ceiling, per door, per rolling hour. One number rather than one
#: per target kind: two knobs would be two knobs nobody tunes, and the SysOp
#: who owns the trust decision can raise this one where it matters.
DEFAULT_POSTS_PER_HOUR = 6
_RATE_WINDOW = datetime.timedelta(hours=1)

#: How many requests one drain will look at. A door that fills its drop
#: directory faster than the rate ceiling lets through is misbehaving; the
#: cap keeps one session's cleanup bounded rather than proportional to
#: whatever it wrote.
_MAX_REQUESTS_PER_DRAIN = 64

#: Longest label we will mint, matching `_MAX_USERNAME_LENGTH`, because the
#: label federates as `local_user_id` and a peer validates it as a handle.
_MAX_LABEL_LENGTH = 32
_LABEL_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


class OutboundError(Exception):
    """Raised for outbound configuration failures a SysOp should see."""


@dataclass(frozen=True)
class OutboundConfig:
    door_id: int
    label: str
    posts_per_hour: int
    #: Nullable via `ON DELETE SET NULL`. A door posts on the authority of
    #: the SysOp who switched its hook on, so if that account is gone the
    #: authority has lapsed with it and `drain` refuses until a SysOp
    #: switches the hook on again. That is also what keeps every accepted
    #: post audit-loggable: `record_action` needs a real actor, and this is
    #: who it is.
    enabled_by_user_id: int | None
    last_refusal_logged_at: str | None
    created_at: str


def _row_to_config(row: sqlite3.Row) -> OutboundConfig:
    return OutboundConfig(
        door_id=row["door_id"],
        label=row["label"],
        posts_per_hour=row["posts_per_hour"],
        enabled_by_user_id=row["enabled_by_user_id"],
        last_refusal_logged_at=row["last_refusal_logged_at"],
        created_at=row["created_at"],
    )


def outbound_config(db: Database, door_id: int) -> OutboundConfig | None:
    """This door's outbound configuration, or `None` if it is switched off.

    Off is the absence of a row, not a flag set to false: a door with no
    row has no label reserved and no targets, so switching outbound off
    genuinely releases everything rather than leaving it dormant.
    """
    row = db.connection.execute(
        "SELECT * FROM door_outbound WHERE door_id = ?", (door_id,)
    ).fetchone()
    return _row_to_config(row) if row is not None else None


def _slug(door_name: str) -> str:
    """A door name reduced to the username grammar, without its suffix."""
    slug = _LABEL_UNSAFE.sub("-", door_name).strip("-._")
    # Not merely cosmetic: a slug that collapses to nothing (a door named
    # entirely in CJK, say) would otherwise mint the bare suffix as a label
    # and every such door would collide with the first one.
    return slug[: _MAX_LABEL_LENGTH - len(DOOR_LABEL_SUFFIX)] or "door"


def _label_taken(db: Database, label: str) -> bool:
    """True if any account or any other door already answers to `label`.

    Both halves matter. The account half is the one with teeth, for the
    chat-resolves-by-username reason in this module's own docstring; the
    door half keeps two doors from sharing a posting identity, which would
    make the audit trail unable to say which of them wrote something.
    """
    if db.connection.execute(
        "SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (label,)
    ).fetchone() is not None:
        return True
    return db.connection.execute(
        "SELECT 1 FROM door_outbound WHERE label = ? COLLATE NOCASE", (label,)
    ).fetchone() is not None


def mint_label(db: Database, door_name: str) -> str:
    """Pick this door's posting identity: `<slug>.door`, disambiguated.

    Disambiguates rather than refusing, because refusing would be a dead
    end for the SysOp: the collision they would have to resolve may be an
    account that predates the reserved suffix entirely (see
    `DOOR_LABEL_SUFFIX`), and telling them to go and rename a caller's
    account before they can switch a door on is not a reasonable thing to
    ask. The label a door actually got is shown on its admin screen and
    handed to the door itself in `door_info.json`, so the disambiguated
    form is never a surprise to either party.
    """
    base = _slug(door_name)
    candidate = f"{base}{DOOR_LABEL_SUFFIX}"
    if not _label_taken(db, candidate):
        return candidate
    for ordinal in range(2, 1000):
        tail = f"-{ordinal}{DOOR_LABEL_SUFFIX}"
        candidate = f"{base[: _MAX_LABEL_LENGTH - len(tail)]}{tail}"
        if not _label_taken(db, candidate):
            return candidate
    raise OutboundError(f"could not find a free posting label based on {door_name!r}")


def enable_outbound(db: Database, door, *, enabled_by: User,
                    posts_per_hour: int = DEFAULT_POSTS_PER_HOUR) -> OutboundConfig:
    """Switch this door's outbound hook on, minting its label.

    Idempotent in the useful sense: a door that already has a hook keeps
    its label (so posts it has already made stay attributable to the same
    identity) and simply re-records who most recently vouched for it.
    """
    existing = outbound_config(db, door.id)
    if existing is not None:
        db.connection.execute(
            "UPDATE door_outbound SET enabled_by_user_id = ? WHERE door_id = ?",
            (enabled_by.id, door.id),
        )
        db.connection.commit()
        record_action(db, actor=enabled_by, action="door_outbound", object_type="door",
                      object_id=door.id,
                      detail=f"door={door.name!r} action=reconfirm label={existing.label!r}")
        return outbound_config(db, door.id)

    label = mint_label(db, door.name)
    db.connection.execute(
        """
        INSERT INTO door_outbound (door_id, label, posts_per_hour, enabled_by_user_id, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (door.id, label, posts_per_hour, enabled_by.id, utc_now_iso()),
    )
    db.connection.commit()
    record_action(db, actor=enabled_by, action="door_outbound", object_type="door",
                  object_id=door.id, detail=f"door={door.name!r} action=enable label={label!r}")
    return outbound_config(db, door.id)


def disable_outbound(db: Database, door, *, disabled_by: User) -> None:
    """Switch the hook off, releasing the label and every allowlisted target.

    Deliberately destructive rather than a dormant flag: leaving the row
    would keep a label reserved against a username nobody can register and
    keep an allowlist that no screen is showing any more. Posts the door
    already made keep their stored label, exactly as a post keeps a deleted
    account's name -- `author_label` is denormalized for that reason.
    """
    config = outbound_config(db, door.id)
    if config is None:
        return
    db.connection.execute("DELETE FROM door_outbound_targets WHERE door_id = ?", (door.id,))
    db.connection.execute("DELETE FROM door_outbound_history WHERE door_id = ?", (door.id,))
    db.connection.execute("DELETE FROM door_outbound WHERE door_id = ?", (door.id,))
    db.connection.commit()
    record_action(db, actor=disabled_by, action="door_outbound", object_type="door",
                  object_id=door.id, detail=f"door={door.name!r} action=disable label={config.label!r}")


def set_rate_ceiling(db: Database, door, ceiling: int, *, changed_by: User) -> OutboundConfig:
    """Change how many posts an hour this door may make."""
    if not 1 <= ceiling <= 240:
        raise OutboundError("the hourly ceiling must be between 1 and 240")
    if outbound_config(db, door.id) is None:
        raise OutboundError("this door's outbound hook is not switched on")
    db.connection.execute(
        "UPDATE door_outbound SET posts_per_hour = ? WHERE door_id = ?", (ceiling, door.id)
    )
    db.connection.commit()
    record_action(db, actor=changed_by, action="door_outbound", object_type="door",
                  object_id=door.id, detail=f"door={door.name!r} action=rate ceiling={ceiling}")
    return outbound_config(db, door.id)


def targets(db: Database, door_id: int) -> list[Board]:
    """Every board this door may post to, in the order they were allowed."""
    rows = db.connection.execute(
        """
        SELECT b.* FROM door_outbound_targets t
        JOIN boards b ON b.id = t.board_id
        WHERE t.door_id = ?
        ORDER BY t.id
        """,
        (door_id,),
    ).fetchall()
    return [_row_to_board(row) for row in rows]


def allow_target(db: Database, door, board: Board, *, allowed_by: User) -> None:
    """Add one board to this door's allowlist."""
    if outbound_config(db, door.id) is None:
        raise OutboundError("this door's outbound hook is not switched on")
    db.connection.execute(
        "INSERT OR IGNORE INTO door_outbound_targets (door_id, board_id, created_at) VALUES (?, ?, ?)",
        (door.id, board.id, utc_now_iso()),
    )
    db.connection.commit()
    record_action(db, actor=allowed_by, action="door_outbound", object_type="door",
                  object_id=door.id, detail=f"door={door.name!r} action=allow board={board.name!r}")


def revoke_target(db: Database, door, board: Board, *, revoked_by: User) -> None:
    """Remove one board from this door's allowlist."""
    db.connection.execute(
        "DELETE FROM door_outbound_targets WHERE door_id = ? AND board_id = ?",
        (door.id, board.id),
    )
    db.connection.commit()
    record_action(db, actor=revoked_by, action="door_outbound", object_type="door",
                  object_id=door.id, detail=f"door={door.name!r} action=revoke board={board.name!r}")


def _window_start() -> str:
    """Start of the rate window, in the same fixed format `utc_now_iso`
    produces -- so it compares directly against stored `created_at`
    strings, the same way `netbbs.boards.posts._cutoff_iso` does."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - _RATE_WINDOW
    return cutoff.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _recent_post_count(db: Database, door_id: int) -> int:
    """Posts inside the rate window, pruning everything older first.

    Counted from its own table rather than from `posts` so that deleting a
    door's output cannot silently raise its own ceiling -- which is exactly
    what a SysOp clearing up after a misbehaving door would otherwise do.
    Pruning on every check is what keeps the table bounded by
    `posts_per_hour` per door rather than growing for the node's lifetime.
    """
    cutoff = _window_start()
    db.connection.execute(
        "DELETE FROM door_outbound_history WHERE door_id = ? AND created_at < ?",
        (door_id, cutoff),
    )
    db.connection.commit()
    row = db.connection.execute(
        "SELECT COUNT(*) FROM door_outbound_history WHERE door_id = ? AND created_at >= ?",
        (door_id, cutoff),
    ).fetchone()
    return row[0]


def _log_refusal_once_per_window(db: Database, door, config: OutboundConfig,
                                 actor: User, reason: str) -> None:
    """Record a refusal, but at most once per rate window.

    `record_action` commits immediately, so logging every refusal would let
    a door in a retry loop flood the moderation log until the audit trail
    became the incident. One entry per window still tells a SysOp that the
    door is being turned away, which is the part worth knowing.
    """
    if config.last_refusal_logged_at is not None and config.last_refusal_logged_at >= _window_start():
        return
    now = utc_now_iso()
    db.connection.execute(
        "UPDATE door_outbound SET last_refusal_logged_at = ? WHERE door_id = ?", (now, door.id)
    )
    db.connection.commit()
    record_action(db, actor=actor, action="door_outbound_refused", object_type="door",
                  object_id=door.id, detail=f"door={door.name!r} reason={reason}")


def _resolve_board(db: Database, door_id: int, requested: object) -> tuple[Board | None, str]:
    """Pick the allowlisted board a request names, or say why we cannot."""
    allowed = targets(db, door_id)
    if not allowed:
        return None, "no board is allowlisted for this door"
    if requested is None:
        # A door with exactly one target need not name it. The Chronicle
        # case -- one door, one board, once a season -- is the common one,
        # and making it state a board it has no other way to learn would be
        # asking it to guess.
        if len(allowed) == 1:
            return allowed[0], ""
        return None, "this door has more than one allowlisted board, so a request must name one"
    if not isinstance(requested, str):
        return None, "'board' must be a string"
    for board in allowed:
        if board.name.casefold() == requested.casefold():
            return board, ""
    return None, f"board {requested!r} is not allowlisted for this door"


def _write_result(request: Path, payload: dict) -> None:
    """Leave the outcome beside the request, atomically, then drop the request.

    Written temp-then-rename for the same reason the door is asked to do it:
    a door polling for its result must never read half a file. The request
    itself is removed once answered, so the drop directory does not
    accumulate work that has already been done.
    """
    result = request.with_name(request.name[: -len(_REQUEST_SUFFIX)] + _RESULT_SUFFIX)
    staging = result.with_suffix(".part")
    try:
        staging.write_text(json.dumps(payload), encoding="utf-8")
        staging.replace(result)
        request.unlink(missing_ok=True)
    except OSError as exc:
        _logger.warning("could not write door outbound result %s: %s", result, exc)


def drain(db: Database, door, workdir: Path, *, node_identity=None) -> tuple[int, int]:
    """Process every request a door left behind, returning (posted, refused).

    Called once the door has exited, so nothing here races the door's own
    writes. Every request we look at is answered: a refusal is a result file
    the door can read on its next launch, never a silent drop and never a
    queue -- holding a post to publish later would mean publishing it after a
    SysOp revoked the allowlist. Requests beyond `_MAX_REQUESTS_PER_DRAIN`
    are the exception and are discarded unanswered along with the working
    directory: a door which wrote more than that in one session is already
    past any ceiling a SysOp set, and answering an unbounded pile would make
    session teardown proportional to whatever it chose to write.

    Failures here never propagate into the caller's shutdown path. A door
    that has already exited cleanly must not be reported as having crashed
    because its drop directory was unreadable.
    """
    directory = workdir / OUTBOUND_DIRNAME
    try:
        requests = sorted(
            path for path in directory.iterdir()
            if path.is_file() and path.name.endswith(_REQUEST_SUFFIX)
            and not path.name.endswith(_RESULT_SUFFIX)
        )
    except OSError:
        return 0, 0

    config = outbound_config(db, door.id)
    if config is None:
        for request in requests[:_MAX_REQUESTS_PER_DRAIN]:
            _write_result(request, {"status": "rejected",
                                    "reason": "this door's outbound hook is not switched on"})
        return 0, len(requests[:_MAX_REQUESTS_PER_DRAIN])

    # A door posts on a named SysOp's authority. If that account is gone the
    # authority has lapsed with it, and there would also be no actor to
    # audit-log the post against -- so the hook stops until a SysOp switches
    # it on again, rather than posting unattributably.
    actor = get_user_by_id(db, config.enabled_by_user_id) if config.enabled_by_user_id else None
    if actor is None:
        for request in requests[:_MAX_REQUESTS_PER_DRAIN]:
            _write_result(request, {
                "status": "rejected",
                "reason": "the account which enabled this door's outbound no longer exists; "
                          "a SysOp must switch it on again",
            })
        return 0, len(requests[:_MAX_REQUESTS_PER_DRAIN])

    posted = refused = 0
    for request in requests[:_MAX_REQUESTS_PER_DRAIN]:
        reason = _handle_one(db, door, config, actor, request, node_identity=node_identity)
        if reason is None:
            posted += 1
        else:
            refused += 1
            _log_refusal_once_per_window(db, door, config, actor, reason)
            config = outbound_config(db, door.id) or config
    return posted, refused


def _handle_one(db: Database, door, config: OutboundConfig, actor: User, request: Path,
                *, node_identity=None) -> str | None:
    """Post one request, or return the reason it was refused."""
    try:
        payload = json.loads(request.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _write_result(request, {"status": "rejected", "reason": "request is not readable JSON"})
        return "malformed request"
    if not isinstance(payload, dict):
        _write_result(request, {"status": "rejected", "reason": "request must be a JSON object"})
        return "malformed request"

    subject, body = payload.get("subject"), payload.get("body")
    if not isinstance(subject, str) or not isinstance(body, str) or not subject.strip():
        _write_result(request, {"status": "rejected",
                                "reason": "request needs a non-empty 'subject' and a 'body' string"})
        return "malformed request"

    board, problem = _resolve_board(db, door.id, payload.get("board"))
    if board is None:
        _write_result(request, {"status": "rejected", "reason": problem})
        return problem

    if _recent_post_count(db, door.id) >= config.posts_per_hour:
        reason = f"rate limit reached ({config.posts_per_hour} posts per hour)"
        _write_result(request, {"status": "rejected", "reason": reason})
        return reason

    try:
        post = create_labelled_post(db, board, config.label, subject, body)
    except PostError as exc:
        _write_result(request, {"status": "rejected", "reason": str(exc)})
        return "post refused"

    db.connection.execute(
        "INSERT INTO door_outbound_history (door_id, created_at) VALUES (?, ?)",
        (door.id, utc_now_iso()),
    )
    db.connection.commit()

    if node_identity is not None:
        # Same call the interactive path makes, and for the same reason: a
        # post on a Linked board that is never queued simply never reaches
        # the peers the SysOp linked the board to. It takes a Post, not a
        # User, and builds `local_user_id` from `author_label`, so a label
        # author federates with no special case.
        from netbbs.link.boards import queue_board_post_if_linked

        try:
            queue_board_post_if_linked(db, post, board, node_identity=node_identity)
        except (OSError, ValueError, sqlite3.Error) as exc:
            # The post exists locally and is real; failing to queue it for
            # peers is worth recording but must not turn into a refusal the
            # door might act on by posting again.
            _logger.warning("could not queue door post %s for Link: %s", post.post_id, exc)

    record_action(db, actor=actor, action="door_outbound_post", object_type="board",
                  object_id=board.id,
                  detail=f"door={door.name!r} label={config.label!r} post={post.post_id}")
    _write_result(request, {"status": "posted", "post_id": post.post_id,
                            "board": board.name, "moderated": board.moderated})
    return None


def door_info_block(db: Database, door_id: int) -> dict | None:
    """What a door is told about its own hook, or `None` when it has none.

    A door needs its label to know how it will appear, and its targets to
    know what it may name -- neither is discoverable any other way, and a
    door left to guess would guess wrong.
    """
    config = outbound_config(db, door_id)
    if config is None:
        return None
    return {
        "label": config.label,
        "directory": OUTBOUND_DIRNAME,
        "boards": [board.name for board in targets(db, door_id)],
        "posts_per_hour": config.posts_per_hour,
    }
