"""Tests for netbbs.permissions — user-level gating."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import User
from netbbs.permissions import InsufficientLevelError, meets_level, require_level, requires_level


def _make_user(level: int) -> User:
    """Construct a User directly for testing — no need to go through the
    full auth/storage flow just to get an object with a `user_level`."""
    return User(
        id=1,
        username="thiesi",
        user_level=level,
        fingerprint=None,
        created_at="2026-01-01T00:00:00.000000Z",
        last_login_at=None,
    )


# -- require_level ------------------------------------------------------


def test_require_level_passes_when_level_sufficient():
    user = _make_user(level=10)
    require_level(user, 5)  # should not raise


def test_require_level_passes_when_level_exactly_matches():
    user = _make_user(level=5)
    require_level(user, 5)  # should not raise


def test_require_level_raises_when_level_insufficient():
    user = _make_user(level=1)
    with pytest.raises(InsufficientLevelError) as exc_info:
        require_level(user, 5)
    assert exc_info.value.required_level == 5
    assert exc_info.value.actual_level == 1


# -- meets_level ----------------------------------------------------------


def test_meets_level_true_when_sufficient():
    user = _make_user(level=10)
    assert meets_level(user, 5) is True


def test_meets_level_false_when_insufficient():
    user = _make_user(level=1)
    assert meets_level(user, 5) is False


def test_meets_level_never_raises():
    user = _make_user(level=0)
    # Should return False, not raise — this is the whole point of having
    # a separate non-raising check for menu-building code.
    assert meets_level(user, 100) is False


# -- requires_level decorator: sync -----------------------------------------


def test_requires_level_decorator_sync_allows_sufficient_level():
    @requires_level(5)
    def handler(user, message):
        return f"{user.username}: {message}"

    user = _make_user(level=10)
    assert handler(user, "hello") == "thiesi: hello"


def test_requires_level_decorator_sync_blocks_insufficient_level():
    @requires_level(5)
    def handler(user, message):
        return f"{user.username}: {message}"

    user = _make_user(level=1)
    with pytest.raises(InsufficientLevelError):
        handler(user, "hello")


# -- requires_level decorator: async ----------------------------------------


def test_requires_level_decorator_async_allows_sufficient_level():
    @requires_level(5)
    async def handler(user, message):
        return f"{user.username}: {message}"

    user = _make_user(level=10)
    result = asyncio.run(handler(user, "hello"))
    assert result == "thiesi: hello"


def test_requires_level_decorator_async_blocks_insufficient_level():
    @requires_level(5)
    async def handler(user, message):
        return f"{user.username}: {message}"

    user = _make_user(level=1)
    with pytest.raises(InsufficientLevelError):
        asyncio.run(handler(user, "hello"))


def test_requires_level_decorator_preserves_function_metadata():
    @requires_level(5)
    def handler(user):
        """A handler's docstring."""
        return user

    assert handler.__name__ == "handler"
    assert handler.__doc__ == "A handler's docstring."
