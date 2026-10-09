"""
A SysOp's or account manager's edits to someone else's Profile (design doc
§5.6, maintainer request 2026-10-09).

The member's own Profile screen is reused for this, acting on their account.
Most of what it writes -- a display preference, a visibility toggle, the bio
-- has a setter that takes only the account, because until now only the
account's owner reached it. `write_as_staff` puts the same authority check
every other account change makes (`require_account_authority`) in front of
such a setter and records the change in the account's admin history.

The fields that already had a staff path keep it, with its own record: the
display name and birthdate (`netbbs.attestation.change_display_name`,
`change_birthdate`), the password and the SSH keys.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from netbbs.auth.users import (
    StaffPermission,
    User,
    UserManagementError,
    get_user_by_id,
    require_account_authority,
)
from netbbs.guest import is_guest_account
from netbbs.moderation.log import record_action
from netbbs.storage.database import Database

#: The admin-history action every staff Profile edit is recorded under.
EDIT_PROFILE_ACTION = "edit_profile"

T = TypeVar("T")


def check_profile_authority(db: Database, actor: User, target: User) -> User:
    """Refuse unless `actor` may edit `target`'s Profile; returns `target`
    read fresh. The rule of every account change (`require_account_authority`
    with manage accounts), plus two of this screen's own: your own Profile is
    the one on the main menu, and the guest account's preferences belong to
    each guest's call, not to a stored row anyone should set."""
    current = get_user_by_id(db, target.id)
    if current is None:
        raise UserManagementError("that account no longer exists")
    if current.id == actor.id:
        raise UserManagementError("change your own Profile from the main menu")
    if is_guest_account(db, current):
        raise UserManagementError(f"{current.username!r} is the guest account; each guest sets its own preferences")
    require_account_authority(db, actor, current, StaffPermission.MANAGE_ACCOUNTS)
    return current


def write_as_staff(
    db: Database, actor: User, target: User, write: Callable[[Database, User], tuple[T, str | None]]
) -> T:
    """Apply `write` to `target` on `actor`'s behalf and record it.

    `write` gets the fresh account and returns its result and the line for
    the admin history -- what changed, never private text: "Bio changed",
    not the bio. `None` records nothing, for a write that changed nothing
    (a block refused, say). Raises `UserManagementError` when `actor` may
    not edit this account, and whatever `write` raises."""
    current = check_profile_authority(db, actor, target)
    result, detail = write(db, current)
    if detail is not None:
        record_action(db, actor=actor, action=EDIT_PROFILE_ACTION, target_user_id=current.id, detail=detail)
    return result
