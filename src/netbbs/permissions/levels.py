"""
User-level gating: the permission primitive design doc §13 requires as
first-class plumbing from Phase 1 onward.

§13: "level/permission gating must be first-class plumbing in the menu/
command dispatch layer from Phase 1 onward, not retrofitted per-feature
later — even though most gated features don't exist until later phases."
This module is that plumbing, built before there's a menu/command
dispatch layer to plug it into, precisely so that layer (later in Phase 1)
never has to be retrofitted.

This module deliberately only knows about the single numeric `user_level`
already present on every account (`netbbs.auth.users.User`). The
finer-grained per-board permissions from §13 — read/write/edit/delete/
approve, moderator roles, per-object grants that bypass a level
requirement — build on top of this in Phase 2; they are a different,
richer permission model, not a replacement for level-gating.
"""

from __future__ import annotations

import inspect
from functools import wraps
from typing import Callable, TypeVar

from netbbs.auth.users import User


class InsufficientLevelError(Exception):
    """
    Raised when a user's level is too low for a level-gated action.

    Carries both the required and actual levels so a caller — typically
    the menu/command dispatch layer, once it exists — can decide how to
    present the failure. Deliberately not this module's job to decide
    between, say, "hide the option entirely" vs "show it but explain why
    it's unavailable": §13 explicitly wants both behaviors available
    (channels can be level-gated invisibly, not just inaccessibly), and
    baking one choice into this exception would make the other harder to
    build later.
    """

    def __init__(self, required_level: int, actual_level: int):
        self.required_level = required_level
        self.actual_level = actual_level
        super().__init__(f"requires level {required_level}, user is level {actual_level}")


def require_level(user: User, minimum_level: int) -> None:
    """
    Raise `InsufficientLevelError` if `user` doesn't meet `minimum_level`.

    The imperative form — call this at the top of any handler that needs
    an inline level check. See `requires_level` below for the decorator
    form, and `meets_level` for the non-raising boolean form.
    """
    if user.user_level < minimum_level:
        raise InsufficientLevelError(minimum_level, user.user_level)


def meets_level(user: User, minimum_level: int) -> bool:
    """
    Non-raising check: does `user` meet `minimum_level`?

    Exists specifically for menu-building code that needs a plain yes/no
    to decide *whether to show an option at all* — raising and catching
    an exception for that would be the wrong tool for a boolean question
    that might get asked once per menu item on every screen render.
    """
    return user.user_level >= minimum_level


_F = TypeVar("_F", bound=Callable)


def requires_level(minimum_level: int) -> Callable[[_F], _F]:
    """
    Decorator form of `require_level`, for gating an entire command
    handler rather than checking inline partway through one.

    Works on both sync and async handlers. The eventual menu/command
    dispatch layer (design doc — Telnet/SSH/web, all asyncio-driven) will
    have both: quick synchronous actions and handlers that need to await
    I/O (database queries, and Link calls once Phase 3 exists).

    Convention: the decorated function's first positional argument is
    always the acting `User` — every command handler in the dispatch
    layer is expected to follow this convention once it exists, so
    gating can be applied uniformly with a single decorator regardless of
    what a given command actually does.
    """

    def decorator(func: _F) -> _F:
        if inspect.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(user: User, *args, **kwargs):
                require_level(user, minimum_level)
                return await func(user, *args, **kwargs)

            return async_wrapper  # type: ignore[return-value]

        @wraps(func)
        def sync_wrapper(user: User, *args, **kwargs):
            require_level(user, minimum_level)
            return func(user, *args, **kwargs)

        return sync_wrapper  # type: ignore[return-value]

    return decorator
