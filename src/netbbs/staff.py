"""
What the main menu and the Staff console need to know about who runs the node
(design doc §5.2, §5.6, issue #836): who is told that accounts wait for
approval, what a moderator's `Moderation (n)` queue covers, the Staff list
members see, and the away notice.

Kept out of `netbbs.net` so the main menu can ask on every redraw without
importing the console, and out of `netbbs.auth` because the moderation half
needs boards and file areas.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, replace

from netbbs.auth.users import (
    SYSOP_LEVEL,
    StaffPermission,
    User,
    UserManagementError,
    describe_staff_permissions,
    get_user_by_id,
    is_usable_sysop,
    list_users,
)
from netbbs.boards.boards import Board, list_boards
from netbbs.boards.posts import count_pending_posts
from netbbs.files import FileArea, list_file_areas
from netbbs.files.entries import count_pending_files
from netbbs.moderation.log import record_action
from netbbs.moderation.roles import BoardPermission, ModeratorGrant, has_permission, list_grants_for_user
from netbbs.storage.database import Database
from netbbs.timeutil import format_for_display, get_node_timezone, utc_now_iso


def is_staff(user: User) -> bool:
    """Holds a staff permission and is not a SysOp: whoever gets the
    `[S]taff` console rather than the SysOp's."""
    return bool(user.staff_permissions) and user.user_level < SYSOP_LEVEL


def told_of_pending_accounts(user: User) -> bool:
    """Design doc §5.6: the notice that accounts wait for approval goes to
    usable SysOps and approve-accounts holders, and to no one else."""
    return is_usable_sysop(user) or user.has_staff(StaffPermission.APPROVE_ACCOUNTS)


def count_pending_accounts(db: Database) -> int:
    return db.connection.execute("SELECT COUNT(*) FROM users WHERE pending_approval = 1").fetchone()[0]


def moderates_everything(user: User) -> bool:
    return user.user_level >= SYSOP_LEVEL or user.has_staff(StaffPermission.MODERATE_ALL)


def moderation_scope(db: Database, user: User) -> tuple[list[Board], list[FileArea]] | None:
    """The boards and file areas whose held posts and uploads `user` may
    decide on, or `None` for all of them (a SysOp, or moderate
    everything). A grant on a board or area that no screen lists -- one
    excluded from a carried Link -- is left out, as the node-wide queue
    leaves it out."""
    if moderates_everything(user):
        return None
    has_any = db.connection.execute(
        "SELECT 1 FROM moderator_grants WHERE user_id = ? AND object_type IN ('board', 'file_area') "
        "AND (permissions & ?) != 0 LIMIT 1",
        (user.id, int(BoardPermission.APPROVE)),
    ).fetchone()
    if has_any is None:
        return [], []
    boards = [
        board for board in list_boards(db, order_by="alphabetical")
        if has_permission(db, user, object_type="board", object_id=board.id, permission=BoardPermission.APPROVE)
    ]
    areas = [
        area for area in list_file_areas(db, order_by="alphabetical")
        if has_permission(db, user, object_type="file_area", object_id=area.id, permission=BoardPermission.APPROVE)
    ]
    return boards, areas


def has_moderation_scope(db: Database, user: User) -> bool:
    """Whether `user` approves held posts or uploads anywhere -- whether the
    main menu offers them `Moderation (n)` (design doc §5.2)."""
    scope = moderation_scope(db, user)
    return scope is None or bool(scope[0] or scope[1])


def count_moderation_items(db: Database, user: User) -> int:
    """How many held posts and uploads wait in `user`'s scope."""
    scope = moderation_scope(db, user)
    if scope is None:
        scope = list_boards(db), list_file_areas(db)
    boards, areas = scope
    return sum(count_pending_posts(db, board) for board in boards) + sum(
        count_pending_files(db, area) for area in areas
    )


# -- the away notice (design doc §5.6) ------------------------------------------

#: One short line: it sits beside a name on the Staff list and inside the
#: message a pending caller reads.
MAX_AWAY_MESSAGE_CHARS = 60

# A pipe code is `|` and two letters or digits (`|07`, `|CR`). The notice is
# plain text wherever it is shown, so a code would only ever show as typed.
_PIPE_CODE = re.compile(r"\|[0-9A-Za-z]{2}")


@dataclass(frozen=True)
class AwayNotice:
    user_id: int
    message: str
    since: str  # UTC timestamp, as stored
    until: datetime.date | None  # the day they expect to be back, node-local


def node_today(db: Database) -> datetime.date:
    """Today in the node's display timezone: a return date is a calendar
    day where the SysOp lives, not in UTC."""
    return datetime.datetime.now(get_node_timezone(db)).date()


def _row_to_notice(row) -> AwayNotice:
    return AwayNotice(
        user_id=row["user_id"], message=row["message"], since=row["since"],
        until=datetime.date.fromisoformat(row["until"]) if row["until"] else None,
    )


def away_notice(db: Database, user: User, *, today: datetime.date | None = None) -> AwayNotice | None:
    """`user`'s away notice while it stands, else `None`. One with a return
    date ends by itself the day after that date; nothing needs to delete it."""
    row = db.connection.execute("SELECT * FROM staff_away WHERE user_id = ?", (user.id,)).fetchone()
    if row is None:
        return None
    notice = _row_to_notice(row)
    if notice.until is not None and notice.until < (today or node_today(db)):
        return None
    return notice


def away_problem(message: str, until: datetime.date | None, today: datetime.date) -> str | None:
    """Why this notice can't be set, or `None` -- for a screen to say before
    anything is written."""
    if not message:
        return "the away message can't be empty"
    if len(message) > MAX_AWAY_MESSAGE_CHARS:
        return f"keep the away message to {MAX_AWAY_MESSAGE_CHARS} characters (it has {len(message)})"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in message):
        return "the away message must be one line of plain text"
    if _PIPE_CODE.search(message):
        return "the away message is plain text -- a pipe code such as |07 would show as typed"
    if until is not None and until < today:
        return "that return date has already passed"
    return None


def may_be_away(user: User) -> bool:
    """A usable SysOp, or an active staff member: whoever can mark
    themselves away (design doc §5.6)."""
    return is_usable_sysop(user) or (
        is_staff(user) and user.disabled_at is None and not user.pending_approval
    )


def set_away(db: Database, user: User, message: str, until: datetime.date | None) -> AwayNotice:
    """Mark `user` away, for themselves (design doc §5.6). Replaces any
    notice they had; audited. Being away changes nobody's permissions."""
    message = message.strip()
    fresh = get_user_by_id(db, user.id)
    if fresh is None or not may_be_away(fresh):
        raise UserManagementError("only a SysOp or a staff member can mark themselves away")
    problem = away_problem(message, until, node_today(db))
    if problem is not None:
        raise UserManagementError(problem)
    db.connection.execute(
        "INSERT OR REPLACE INTO staff_away (user_id, message, since, until) VALUES (?, ?, ?, ?)",
        (fresh.id, message, utc_now_iso(), until.isoformat() if until else None),
    )
    db.connection.commit()
    record_action(
        db, actor=fresh, action="set_away", target_user_id=fresh.id,
        detail=f"back {until.isoformat()}" if until else "no return date",
    )
    notice = away_notice(db, fresh)
    assert notice is not None
    return notice


def end_away(db: Database, user: User) -> None:
    """End `user`'s own away notice; a no-op when there is none."""
    cursor = db.connection.execute("DELETE FROM staff_away WHERE user_id = ?", (user.id,))
    db.connection.commit()
    if cursor.rowcount:
        record_action(db, actor=user, action="end_away", target_user_id=user.id)


def describe_away(notice: AwayNotice, since_date: str) -> str:
    """`away, back 2026-10-12 -- At a pen show`, or `away since 2026-09-20 --
    ...` for a notice without a return date: never more than it says (§5.6)."""
    when = f"away, back {notice.until.isoformat()}" if notice.until else f"away since {since_date}"
    return f"{when} -- {notice.message}"


# -- the Staff list ------------------------------------------------------------


@dataclass(frozen=True)
class StaffListEntry:
    user: User
    role: str  # "SysOp", "Staff" or "Moderator"
    # What a SysOp or staff member looks after, in words. A moderator's is
    # their `grants` instead: which of them a member may read about depends
    # on what that member may see (review on #870), so the screen words them.
    looks_after: str
    away: AwayNotice | None
    grants: tuple[ModeratorGrant, ...] = ()


#: Board and file-area bits that are access, not moderation: a read or
#: read-and-post grant lets its holder past a level gate (issue #868) but
#: makes them nobody's moderator.
_ACCESS_BITS = BoardPermission.READ | BoardPermission.WRITE


def _moderation_part(grant: ModeratorGrant) -> ModeratorGrant | None:
    """`grant` with its access bits dropped, or `None` when nothing is left:
    what of it the Staff list names. Grants on one object share a row, so a
    board's "Read and post" and its "Limited" moderator merge into one
    (review on #977). Every channel bit moderates; channels have no access
    bits."""
    if grant.object_type == "channel":
        return grant if grant.permissions else None
    permissions = grant.permissions & ~int(_ACCESS_BITS)
    return replace(grant, permissions=permissions) if permissions else None


def list_staff(db: Database, *, today: datetime.date | None = None) -> list[StaffListEntry]:
    """
    Who runs the node, for every member (design doc §5.6): usable SysOps,
    then staff members, then moderators, each group by name. Disabled and
    pending accounts are on none of them. The Previous callers privacy
    choice hides nobody here -- these are the people members are meant to
    find.
    """
    today = today or node_today(db)
    sysops: list[StaffListEntry] = []
    staff: list[StaffListEntry] = []
    moderators: list[StaffListEntry] = []
    for user in list_users(db):
        if user.disabled_at is not None or user.pending_approval:
            continue
        if is_usable_sysop(user):
            sysops.append(StaffListEntry(user, "SysOp", "runs the node", away_notice(db, user, today=today)))
        elif user.staff_permissions:
            staff.append(StaffListEntry(
                user, "Staff", describe_staff_permissions(user.staff_permissions),
                away_notice(db, user, today=today),
            ))
        else:
            grants = [part for part in map(_moderation_part, list_grants_for_user(db, user)) if part is not None]
            if grants:
                moderators.append(StaffListEntry(user, "Moderator", "", None, tuple(grants)))
    return sysops + staff + moderators


def approvers_away_line(db: Database) -> str | None:
    """
    The sentence a pending caller is told when every account that could
    approve them is away (design doc §5.6), naming whoever is expected back
    first -- or `None` when someone is around, or when nobody could approve
    at all, which is not an absence to report.
    """
    today = node_today(db)
    approvers = [
        user for user in list_users(db)
        if is_usable_sysop(user) or (may_be_away(user) and user.has_staff(StaffPermission.APPROVE_ACCOUNTS))
    ]
    if not approvers:
        return None
    notices: list[tuple[User, AwayNotice]] = []
    for user in approvers:
        notice = away_notice(db, user, today=today)
        if notice is None:
            return None
        notices.append((user, notice))
    user, notice = min(
        notices, key=lambda pair: (pair[1].until is None, pair[1].until or datetime.date.max, pair[1].since)
    )
    if notice.until is not None:
        return (
            f"Everyone who approves accounts is away just now; {user.username} expects to be back "
            f"on {notice.until.isoformat()}: {notice.message}"
        )
    shown = format_for_display(notice.since, db, override_format="%Y-%m-%d")
    return (
        f"Everyone who approves accounts is away just now; {user.username} has been away since "
        f"{shown}: {notice.message}"
    )


def sees_staff_list(db: Database, user: User) -> bool:
    """Every member but the guest account (design doc §5.6); a pending
    account never reaches the main menu at all."""
    from netbbs.guest import guest_user  # netbbs.guest imports netbbs.auth; kept local like its other users

    guest = guest_user(db)
    return not user.pending_approval and (guest is None or guest.id != user.id)
