"""Tests for netbbs.net.welcome_banner (design doc -- welcome banner
round) -- the loader/status functions, in isolation from the
netbbs.net.admin_flow UI that drives them (covered separately in
tests/test_admin_flow.py) and the login-flow integration point
(covered separately below via a direct handle_session smoke test)."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.net.welcome_banner import (
    DEFAULT_WELCOME_BANNER,
    MAX_BANNER_SIZE_BYTES,
    WelcomeBannerStatus,
    banner_path,
    is_welcome_banner_enabled,
    load_welcome_banner,
    set_welcome_banner_enabled,
    welcome_banner_status,
)
from netbbs.rendering import RESET
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


# -- banner_path -------------------------------------------------------


def test_banner_path_is_colocated_with_db_and_named_by_convention(db):
    assert banner_path(db) == db.path.parent / f"{db.path.stem}_welcome_banner.ans"


def test_banner_path_does_not_auto_create_anything(db):
    path = banner_path(db)
    assert not path.exists()


# -- enabled flag --------------------------------------------------------


def test_disabled_by_default(db):
    assert is_welcome_banner_enabled(db) is False


def test_set_enabled_then_read_back(db):
    set_welcome_banner_enabled(db, True)
    assert is_welcome_banner_enabled(db) is True


def test_set_disabled_after_enabled(db):
    set_welcome_banner_enabled(db, True)
    set_welcome_banner_enabled(db, False)
    assert is_welcome_banner_enabled(db) is False


# -- load_welcome_banner --------------------------------------------------


def test_disabled_by_default_returns_default_banner(db):
    # DEFAULT_WELCOME_BANNER already ends in its own reset (colored()'s
    # own wrapping) -- the fallback paths return it as-is, unlike the
    # custom-file path, which appends an extra RESET defensively since a
    # SysOp's file might leave color state open (see
    # test_result_always_ends_with_reset_sequence below for the
    # "always ends in RESET, one way or another" invariant).
    assert load_welcome_banner(db) == DEFAULT_WELCOME_BANNER


def test_enabled_but_file_missing_falls_back_to_default(db):
    set_welcome_banner_enabled(db, True)
    assert load_welcome_banner(db) == DEFAULT_WELCOME_BANNER


def test_enabled_with_valid_utf8_file_returns_file_content(db):
    banner_path(db).write_bytes("MY CUSTOM BANNER".encode("utf-8"))
    set_welcome_banner_enabled(db, True)
    result = load_welcome_banner(db)
    assert "MY CUSTOM BANNER" in result
    assert result.endswith(RESET)


def test_enabled_with_cp437_file_decodes_correctly(db):
    banner_path(db).write_bytes(bytes([0xB0, 0xB1, 0xB2, 0xDB]))
    set_welcome_banner_enabled(db, True)
    result = load_welcome_banner(db)
    assert "░▒▓█" in result


def test_oversized_file_falls_back_to_default(db):
    banner_path(db).write_bytes(b"x" * (MAX_BANNER_SIZE_BYTES + 1))
    set_welcome_banner_enabled(db, True)
    assert load_welcome_banner(db) == DEFAULT_WELCOME_BANNER


def test_file_at_exactly_the_size_limit_is_not_rejected(db):
    banner_path(db).write_bytes(b"x" * MAX_BANNER_SIZE_BYTES)
    set_welcome_banner_enabled(db, True)
    result = load_welcome_banner(db)
    assert result != DEFAULT_WELCOME_BANNER + RESET


def test_result_always_ends_with_reset_sequence(db):
    assert load_welcome_banner(db).endswith(RESET)
    banner_path(db).write_bytes(b"custom")
    set_welcome_banner_enabled(db, True)
    assert load_welcome_banner(db).endswith(RESET)


# -- welcome_banner_status -------------------------------------------------


def test_status_when_disabled_and_missing(db):
    status = welcome_banner_status(db)
    assert status == WelcomeBannerStatus(
        enabled=False, path=banner_path(db), exists=False, size_bytes=None
    )


def test_status_when_enabled_and_present(db):
    banner_path(db).write_bytes(b"hello")
    set_welcome_banner_enabled(db, True)
    status = welcome_banner_status(db)
    assert status.enabled is True
    assert status.exists is True
    assert status.size_bytes == 5


def test_status_does_not_read_file_content(db, monkeypatch):
    # Confirms welcome_banner_status is genuinely cheap (stat-only) --
    # patch read_bytes to explode if it's ever called from this path.
    from pathlib import Path

    banner_path(db).write_bytes(b"hello")

    def _boom(self):
        raise AssertionError("welcome_banner_status must not read file content")

    monkeypatch.setattr(Path, "read_bytes", _boom)
    welcome_banner_status(db)  # must not raise


# -- login integration (the real risk this design defends against) --------


class _LoginFakeSession:
    """Mirrors tests/test_login_outcomes.py's own FakeSession exactly --
    a minimal, proven-working stand-in for driving handle_session end to
    end, distinct from tests/test_admin_flow.py's FakeSession (different
    constructor/read_line signature, built for the admin menu instead)."""

    def __init__(self, lines: list[str]):
        self._lines = iter(lines)
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_line(self, echo: bool = True) -> str:
        return next(self._lines)

    async def read_key(self, echo: bool = True) -> str:
        raise AssertionError("main menu must not be entered")

    @property
    def output(self) -> str:
        return "".join(self.written)


def _throttle_config(**overrides):
    from netbbs.net.nodeconfig import ThrottleConfig

    return ThrottleConfig(**overrides)


def _throttle(config=None):
    from netbbs.net.throttle import LoginThrottle

    config = config or _throttle_config()
    return LoginThrottle(
        per_source_capacity=config.per_source_capacity,
        per_source_refill_per_minute=config.per_source_refill_per_minute,
        per_username_capacity=config.per_username_capacity,
        per_username_refill_per_minute=config.per_username_refill_per_minute,
        global_capacity=config.global_capacity,
        global_refill_per_minute=config.global_refill_per_minute,
        max_tracked_keys=config.max_tracked_keys,
        max_concurrent_unauthenticated_sessions=config.max_concurrent_unauthenticated_sessions,
    )


def _run_login(db, session) -> None:
    from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
    from netbbs.net.login_flow import handle_session
    from netbbs.net.maintenance import MaintenanceMode
    from netbbs.net.session_registry import ActiveSessionRegistry

    asyncio.run(
        asyncio.wait_for(
            handle_session(
                session,
                db,
                ChatHub(),
                PresenceRegistry(),
                MessageMailbox(),
                _throttle(),
                _throttle_config(),
                ActiveSessionRegistry(),
                MaintenanceMode(),
            ),
            timeout=5,
        )
    )


def test_login_flow_does_not_crash_with_missing_banner_file(db):
    set_welcome_banner_enabled(db, True)  # enabled, but no file exists

    session = _LoginFakeSession(["nosuchuser", "wrongpass"] * 3)
    _run_login(db, session)
    # Login proceeded far enough to actually prompt for a username --
    # the whole point being this never raised due to the missing file.
    assert "Too many failed attempts" in session.output


def test_login_flow_does_not_crash_with_oversized_banner_file(db):
    banner_path(db).write_bytes(b"x" * (MAX_BANNER_SIZE_BYTES + 1))
    set_welcome_banner_enabled(db, True)

    session = _LoginFakeSession(["nosuchuser", "wrongpass"] * 3)
    _run_login(db, session)
    assert "Too many failed attempts" in session.output
