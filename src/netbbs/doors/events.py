"""Door events and standings (door API 5; issue #1408, design §16 #1378).

A door whose SysOp switched game events on may report what a caller earned
live -- a rank, a top-tier achievement, a season's medals, a capture, a
perfect round -- and a top-ten standings snapshot, through the same file
drop as its outbound posts (`netbbs.doors.outbound`).

**The door supplies only references.** An event is a fixed type, the
`user_id` of each caller it names and a short title. The host stores it in a
bounded journal and composes the chat text itself, when a bot announces it,
resolving each handle at that moment: a rename shows, and a caller who has
since opted out, been disabled or been deleted is not named. A door cannot
add a type or word the line.

**Two gates beside the SysOp's switch.** The door's audience: events are
announced only while every caller may play the door (`door_not_public`,
the door's version of the bot rule `bots.sources._public`). And the
caller's own consent, the profile switch "Game results announced in chat",
on by default and checked per caller both when the event is accepted and
when it is published.

A door is a trusted program and may name any `user_id`. That is attribution,
not security: the journal row and the receipt name the door.
"""

from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass

from netbbs.auth.users import User, get_user_by_id
from netbbs.moderation.log import record_action
from netbbs.rendering import sanitize_text
from netbbs.storage.database import Database
from netbbs.timeutil import utc_iso, utc_now_iso
from netbbs.user_preferences import get_user_preference, set_user_preference

#: The fixed event types, in the order the door guide lists them.
EVENT_TYPES = ("rank", "achievement", "medal", "capture", "perfect", "season")
#: Types that name no caller (issue #1561): a new season is the game's news,
#: not anyone's result, so no consent or account check applies to it.
UNNAMED_TYPES = frozenset({"season"})
#: A season close names its medallists in placement order.
MEDALS = ("gold", "silver", "bronze")
TITLE_MAX = 80
DEFAULT_EVENTS_PER_HOUR = 12
MAX_EVENTS_PER_HOUR = 240
#: An event is a few short fields; a standings snapshot ten rows of them.
#: Checked on the request file as the door wrote it, escapes included.
EVENT_REQUEST_MAX_BYTES = 2048
STANDINGS_REQUEST_MAX_BYTES = 8192
STANDINGS_ROWS = 10
STANDINGS_TITLE_MAX = 40
STANDINGS_VALUE_MAX = 24
STANDINGS_LABEL_MAX = 40

GAME_RESULTS_KEY = "game_results_announced"

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_RATE_WINDOW = datetime.timedelta(hours=1)


# -- the caller's consent ------------------------------------------------------


def game_results_announced(db: Database, user: User) -> bool:
    """Whether bots may announce `user`'s game results and list them in
    `!top`. On by default, like "Name shown to other callers": a rank or an
    achievement is game standing, not new personal content (#1378 Decision 2)."""
    return get_user_preference(db, user, GAME_RESULTS_KEY, default="1") == "1"


def set_game_results_announced(db: Database, user: User, announced: bool) -> None:
    set_user_preference(db, user, GAME_RESULTS_KEY, "1" if announced else "0")


def announceable(db: Database, user_id: object) -> User | None:
    """The account `user_id` names, if a bot may name it now: it exists, is
    neither disabled nor pending, is not the shared guest account, and its
    owner has not switched game results off."""
    if type(user_id) is not int or user_id <= 0:
        return None
    user = get_user_by_id(db, user_id)
    if user is None or user.disabled_at or user.pending_approval:
        return None
    from netbbs.guest import is_guest_account

    if is_guest_account(db, user) or not game_results_announced(db, user):
        return None
    return user


# -- the door's switch ---------------------------------------------------------


def door_events_refusal(db: Database, door) -> str | None:
    """Why `door`'s events are not announced right now, or `None`: its
    events switch is off, or not every caller may play it."""
    from netbbs.doors.outbound import door_not_public, outbound_config

    config = outbound_config(db, door.id)
    if config is None or not config.events_enabled:
        return "game events are not switched on for this door"
    return door_not_public(db, door)


def set_events_enabled(db: Database, door, enabled: bool, *, changed_by: User):
    """Switch this door's game events on or off. Off keeps the journal and
    the standings, which are simply not announced or answered while off."""
    from netbbs.doors.outbound import OutboundError, outbound_config

    if outbound_config(db, door.id) is None:
        raise OutboundError("this door's outbound hook is not switched on")
    db.connection.execute("UPDATE door_outbound SET events_enabled = ? WHERE door_id = ?",
                          (int(enabled), door.id))
    db.connection.commit()
    record_action(db, actor=changed_by, action="door_outbound", object_type="door", object_id=door.id,
                  detail=f"door={door.name!r} action={'events_on' if enabled else 'events_off'}")
    return outbound_config(db, door.id)


def set_events_ceiling(db: Database, door, ceiling: int, *, changed_by: User):
    from netbbs.doors.outbound import OutboundError, outbound_config

    if not 1 <= ceiling <= MAX_EVENTS_PER_HOUR:
        raise OutboundError(f"the hourly events ceiling must be between 1 and {MAX_EVENTS_PER_HOUR}")
    if outbound_config(db, door.id) is None:
        raise OutboundError("this door's outbound hook is not switched on")
    db.connection.execute("UPDATE door_outbound SET events_per_hour = ? WHERE door_id = ?", (ceiling, door.id))
    db.connection.commit()
    record_action(db, actor=changed_by, action="door_outbound", object_type="door", object_id=door.id,
                  detail=f"door={door.name!r} action=events_rate ceiling={ceiling}")
    return outbound_config(db, door.id)


def forget_door(db: Database, door_id: int) -> None:
    """What switching the hook off releases: journal, standings, debits.
    Run inside the caller's transaction (no commit)."""
    db.connection.execute("DELETE FROM door_events WHERE door_id = ?", (door_id,))
    db.connection.execute("DELETE FROM door_standings WHERE door_id = ?", (door_id,))
    db.connection.execute("DELETE FROM door_event_history WHERE door_id = ?", (door_id,))


# -- requests --------------------------------------------------------------------


def _text(value: object, limit: int, what: str, *, required: bool = True) -> tuple[str | None, str]:
    """A door's short text as one plain line, or why not."""
    if value is None and not required:
        return "", ""
    if not isinstance(value, str):
        return None, f"'{what}' must be a string"
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None, f"'{what}' is not valid Unicode"
    text = " ".join(_CONTROL.sub(" ", value).split())
    if required and not text:
        return None, f"'{what}' must not be empty"
    if len(text) > limit:
        return None, f"'{what}' may be at most {limit} characters"
    return text, ""


def _event_count(db: Database, door_id: int) -> int:
    cutoff = utc_iso(datetime.datetime.now(datetime.timezone.utc) - _RATE_WINDOW)
    db.connection.execute("DELETE FROM door_event_history WHERE door_id = ? AND created_at < ?", (door_id, cutoff))
    db.connection.commit()
    return db.connection.execute(
        "SELECT COUNT(*) FROM door_event_history WHERE door_id = ? AND created_at >= ?", (door_id, cutoff),
    ).fetchone()[0]


def handle_event(db: Database, door, config, request: object, size: int, answer, *,
                 rehearsal: bool, rehearsed: dict | None) -> str | None:
    """Journal one `{"event": {...}}` request, or return why it was refused.

    A caller who switched game results off is left out of the event, and an
    event left naming nobody is dropped: answered, but neither journaled nor
    debited, and not a refusal the moderation log records."""
    if not config.events_enabled:
        reason = "game events are not switched on for this door"
        answer({"status": "rejected", "reason": reason})
        return reason
    if size > EVENT_REQUEST_MAX_BYTES:
        answer({"status": "rejected", "reason": f"an event request may be at most {EVENT_REQUEST_MAX_BYTES} bytes"})
        return "oversized request"
    if not isinstance(request, dict):
        answer({"status": "rejected", "reason": "'event' must be a JSON object"})
        return "malformed request"
    kind = request.get("type")
    if kind not in EVENT_TYPES:
        answer({"status": "rejected", "reason": "'type' must be one of " + ", ".join(EVENT_TYPES)})
        return "malformed request"
    title, problem = _text(request.get("title"), TITLE_MAX, "title", required=kind != "perfect")
    if title is None:
        answer({"status": "rejected", "reason": problem})
        return "malformed request"
    if kind == "medal":
        named = request.get("user_ids")
        if (not isinstance(named, list) or not 1 <= len(named) <= len(MEDALS)
                or any(type(i) is not int or i <= 0 for i in named) or len(set(named)) != len(named)):
            answer({"status": "rejected",
                    "reason": "a medal event needs 'user_ids': 1-3 different account ids, gold first"})
            return "malformed request"
    elif kind in UNNAMED_TYPES:
        if "user_id" in request or "user_ids" in request:
            answer({"status": "rejected", "reason": f"a {kind} event names no caller"})
            return "malformed request"
        named = []
    else:
        one = request.get("user_id")
        if type(one) is not int or one <= 0:
            answer({"status": "rejected", "reason": f"a {kind} event needs 'user_id', an account id"})
            return "malformed request"
        named = [one]
    for user_id in named:
        user = get_user_by_id(db, user_id)
        if user is None:
            reason = f"no account has user_id {user_id}"
            answer({"status": "rejected", "reason": reason})
            return reason
    # Positions are kept, so the bronze medallist stays bronze when silver
    # is left out.
    subjects = [(position, user_id) for position, user_id in enumerate(named) if announceable(db, user_id)]
    if not subjects and kind not in UNNAMED_TYPES:
        answer({"status": "dropped",
                "reason": "no caller it names can be announced: guest, disabled or pending accounts, "
                          "and callers who switched game results off, never are"})
        return None
    spent = _event_count(db, door.id) + (rehearsed or {}).get("events", 0)
    if spent >= config.events_per_hour:
        reason = f"rate limit reached ({config.events_per_hour} events per hour)"
        answer({"status": "rejected", "reason": reason})
        return reason
    if rehearsal:
        if rehearsed is not None:
            rehearsed["events"] = rehearsed.get("events", 0) + 1
        answer({"status": "accepted", "type": kind, "named": len(subjects)})
        return None
    now = utc_now_iso()
    db.connection.execute("BEGIN IMMEDIATE")
    try:
        cursor = db.connection.execute(
            "INSERT INTO door_events (door_id, type, title, created_at) VALUES (?, ?, ?, ?)",
            (door.id, kind, title, now))
        db.connection.executemany(
            "INSERT INTO door_event_subjects (event_id, position, user_id) VALUES (?, ?, ?)",
            [(cursor.lastrowid, position, user_id) for position, user_id in subjects])
        db.connection.execute("INSERT INTO door_event_history (door_id, created_at) VALUES (?, ?)", (door.id, now))
        db.connection.commit()
    except BaseException:
        db.connection.rollback()
        raise
    answer({"status": "accepted", "type": kind, "named": len(subjects), "event_id": cursor.lastrowid})
    return None


def handle_standings(db: Database, door, config, request: object, size: int, answer, *,
                     rehearsal: bool) -> str | None:
    """Replace this door's standings snapshot, or return why not. Rows are
    stored as the door ranked them; the opt-out and account checks apply
    when `top` is asked, so a caller switching game results back on
    reappears without the door writing again."""
    if not config.events_enabled:
        reason = "game events are not switched on for this door"
        answer({"status": "rejected", "reason": reason})
        return reason
    if size > STANDINGS_REQUEST_MAX_BYTES:
        answer({"status": "rejected",
                "reason": f"a standings request may be at most {STANDINGS_REQUEST_MAX_BYTES} bytes"})
        return "oversized request"
    if not isinstance(request, dict) or not isinstance(request.get("rows"), list):
        answer({"status": "rejected", "reason": "'standings' needs a 'title' and a 'rows' list"})
        return "malformed request"
    title, problem = _text(request.get("title"), STANDINGS_TITLE_MAX, "title")
    if title is None:
        answer({"status": "rejected", "reason": problem})
        return "malformed request"
    raw_rows = request["rows"]
    if len(raw_rows) > STANDINGS_ROWS:
        answer({"status": "rejected", "reason": f"standings may have at most {STANDINGS_ROWS} rows"})
        return "malformed request"
    rows = []
    for row in raw_rows:
        if not isinstance(row, dict) or type(row.get("user_id")) is not int or row["user_id"] <= 0:
            answer({"status": "rejected", "reason": "each standings row needs 'user_id', an account id"})
            return "malformed request"
        value, problem = _text(row.get("value"), STANDINGS_VALUE_MAX, "value")
        label, label_problem = _text(row.get("label"), STANDINGS_LABEL_MAX, "label", required=False)
        if value is None or label is None:
            answer({"status": "rejected", "reason": problem or label_problem})
            return "malformed request"
        rows.append({"user_id": row["user_id"], "value": value, "label": label})
    if len({r["user_id"] for r in rows}) != len(rows):
        answer({"status": "rejected", "reason": "a caller may appear in the standings only once"})
        return "malformed request"
    if not rehearsal:
        db.connection.execute(
            "INSERT INTO door_standings (door_id, title, rows_json, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(door_id) DO UPDATE SET title = excluded.title, rows_json = excluded.rows_json, "
            "updated_at = excluded.updated_at",
            (door.id, title, json.dumps(rows), utc_now_iso()))
        db.connection.commit()
    answer({"status": "accepted", "standings": len(rows)})
    return None


# -- publication -------------------------------------------------------------------


def _plain(text: str) -> str:
    return " ".join(sanitize_text(text).split())


def event_text(kind: str, title: str, door_name: str, names: list[tuple[int, str]]) -> str:
    """The host's sentence for one event. `names` are (position, handle)."""
    door_name, title = _plain(door_name), _plain(title)
    if kind == "season":
        return f"A new {door_name} season begins: {title}."
    if kind == "medal":
        medals = ", ".join(f"{MEDALS[position]} {name}" for position, name in names)
        return f"{title} of {door_name} closed: {medals}."
    name = names[0][1]
    if kind == "rank":
        return f"{name} reached {title} in {door_name}."
    if kind == "achievement":
        return f'{name} earned the achievement "{title}" in {door_name}.'
    if kind == "capture":
        return f"{name} captured {title} in {door_name}."
    return f"{name} answered every question in a {door_name} round" + (f" ({title})." if title else ".")


@dataclass(frozen=True)
class AnnouncedEvent:
    kind: str
    title: str
    names: tuple[str, ...]
    text: str


def announced_event(db: Database, door, event_id: int) -> AnnouncedEvent | None:
    """One journaled event as a bot may announce it now, or `None` when it
    names no one who may still be named. The door gates are the caller's."""
    row = db.connection.execute("SELECT * FROM door_events WHERE id = ? AND door_id = ?",
                                (event_id, door.id)).fetchone()
    if row is None:
        return None
    names = []
    for subject in db.connection.execute(
            "SELECT position, user_id FROM door_event_subjects WHERE event_id = ? ORDER BY position", (event_id,)):
        user = announceable(db, subject["user_id"])
        if user is not None:
            names.append((subject["position"], _plain(user.username)))
    if not names and row["type"] not in UNNAMED_TYPES:
        return None
    return AnnouncedEvent(row["type"], _plain(row["title"]), tuple(n for _, n in names),
                          event_text(row["type"], row["title"], door.name, names)[:500])


@dataclass(frozen=True)
class StandingsRow:
    rank: int
    name: str
    value: str
    label: str


def public_standings(db: Database, door) -> tuple[str, list[StandingsRow], str] | None:
    """(title, rows, updated_at) for `top`, or `None` when the door has
    none. Rows of callers who may not be named are left out and the rest
    renumbered, so a hidden caller leaves no gap that points at them."""
    row = db.connection.execute("SELECT * FROM door_standings WHERE door_id = ?", (door.id,)).fetchone()
    if row is None:
        return None
    try:
        stored = json.loads(row["rows_json"])
    except ValueError:
        stored = []
    rows = []
    for entry in stored:
        user = announceable(db, entry.get("user_id"))
        if user is not None:
            rows.append(StandingsRow(len(rows) + 1, _plain(user.username), _plain(entry.get("value", "")),
                                     _plain(entry.get("label") or "")))
    return _plain(row["title"]), rows, row["updated_at"]


def public_doors(db: Database) -> list:
    """Doors a bot may be granted as an event source: every caller may play
    them. Remote services are left out, since they can never report."""
    from netbbs.doors.outbound import door_not_public
    from netbbs.doors.profiles import is_remote
    from netbbs.doors.registry import list_doors

    result = []
    for door in list_doors(db):
        if is_remote(door.profile):
            continue
        if door_not_public(db, door) is None:
            result.append(door)
    return result
