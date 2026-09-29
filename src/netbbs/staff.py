"""
What the main menu and the Staff console need to know about who runs the node
(design doc §5.2, §5.6, issue #836): who is told that accounts wait for
approval, and what a moderator's `Moderation (n)` queue covers.

Kept out of `netbbs.net` so the main menu can ask on every redraw without
importing the console, and out of `netbbs.auth` because the moderation half
needs boards and file areas.
"""

from __future__ import annotations

from netbbs.auth.users import SYSOP_LEVEL, StaffPermission, User, is_usable_sysop
from netbbs.boards.boards import Board, list_boards
from netbbs.boards.posts import count_pending_posts
from netbbs.files import FileArea, list_file_areas
from netbbs.files.entries import count_pending_files
from netbbs.moderation.roles import BoardPermission, has_permission
from netbbs.storage.database import Database


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
