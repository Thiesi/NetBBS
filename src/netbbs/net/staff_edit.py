"""
The screens of a member's Profile, opened by a SysOp or account manager from
the user editor (design doc §5.6): each write goes through
`netbbs.profile_admin.write_as_staff`, and a refusal -- a permission revoked
while the screen was open -- surfaces as `StaffEditRefused`, which Profile
turns into a message and the value it had (`refusable`).
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from netbbs.auth.users import User, UserManagementError
from netbbs.net.notices import announce
from netbbs.net.session import Session
from netbbs.profile_admin import write_as_staff
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

T = TypeVar("T")


class StaffEditRefused(Exception):
    """The account may no longer be edited by this member of staff."""


async def write_member(
    lane: DatabaseLane, actor: User, target: User, write: Callable[[Database, User], tuple[T, str | None]]
) -> T:
    """`write_as_staff` on the lane, with its refusal as `StaffEditRefused`."""
    try:
        return await lane.run(write_as_staff, actor, target, write)
    except UserManagementError as exc:
        raise StaffEditRefused(str(exc)) from exc


async def as_staff(call: Awaitable[T]) -> T:
    """An already-audited staff mutator (`change_display_name`, say), with
    its refusal as `StaffEditRefused`."""
    try:
        return await call
    except UserManagementError as exc:
        raise StaffEditRefused(str(exc)) from exc


def refusable(prompt: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    """A field prompt that, refused, puts the draft back the way it was and
    says why on the redraw -- a toggle advances its value before it writes.
    Keeps whatever the prompt is marked with (`inline_field`)."""

    @functools.wraps(prompt)
    async def guarded(session: Session, lane: DatabaseLane, draft: dict[str, Any]) -> None:
        before = dict(draft)
        try:
            await prompt(session, lane, draft)
        except StaffEditRefused as exc:
            draft.clear()
            draft.update(before)
            announce(session, f"Not changed: {exc}.", tone="error")

    return guarded
