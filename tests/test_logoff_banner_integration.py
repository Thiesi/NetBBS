"""Integration tests for the logoff banner and final call summary actually
being shown by `netbbs.net.login_flow.run_authenticated_session` on a
clean sign-out -- distinct from tests/test_logoff_banner.py's isolated
loader/status tests.

Drives a real login-then-logoff round trip via `handle_session`/
`FakeSession`, the same pattern tests/test_login_throttling.py already
uses for its own "log in as alice, then log off" scenario."""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.auth.users import create_user
from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
from netbbs.net import login_flow
from netbbs.net.logoff_banner import logoff_banner_path, set_logoff_banner_enabled
from netbbs.net.maintenance import MaintenanceMode
from netbbs.net.nodeconfig import ThrottleConfig
from netbbs.net.profile_flow import (
    _LOGOFF_SUMMARY_GRADIENT,
    _show_logoff_summary_screen,
)
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.net.throttle import LoginThrottle
from netbbs.net.unicode_style_preference import set_unicode_style_enabled
from netbbs.rendering import display_width
from netbbs.session_history import SessionHistoryEntry
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


class FakeSession:
    def __init__(self, lines: list[str] | None = None, keys: list[str] | None = None):
        self._lines = iter(lines or [])
        self._keys = iter(keys or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.node_display_name = "NetBBS"
        self.node_name_gradient = None
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"
        self.supports_truecolor = False

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_line(self, echo: bool = True, **kwargs) -> str:
        return next(self._lines)

    async def read_key(self, echo: bool = True) -> str:
        return next(self._keys)

    @property
    def output(self) -> str:
        return "".join(self.written)


def _throttle_config(**overrides) -> ThrottleConfig:
    return ThrottleConfig(**overrides)


def _visible(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _throttle(config: ThrottleConfig) -> LoginThrottle:
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


async def _run_login(session, db, config=None) -> None:
    config = config or _throttle_config()
    throttle = _throttle(config)
    await login_flow.handle_session(
        session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), throttle, config,
        ActiveSessionRegistry(), MaintenanceMode(),
    )


def test_logoff_banner_shown_on_an_intentional_log_off(db):
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    set_unicode_style_enabled(db, alice, True)
    logoff_banner_path(db).write_bytes(b"THANKS FOR VISITING")
    set_logoff_banner_enabled(db, True)
    # The Unicode preference is already set above; "l" then "y" is the
    # "Log off?" confirmation.
    session = FakeSession(["alice", "hunter2pw", "y"], keys=["l"])
    session.supports_truecolor = True

    asyncio.run(_run_login(session, db))

    visible = _visible(session.output)
    assert visible.index("THANKS FOR VISITING") < visible.index("C A L L   C O M P L E T E")
    assert visible.index("C A L L   C O M P L E T E") < visible.index("Goodbye!")
    assert "CONNECTED" in visible
    assert "SIGNED OFF" in visible
    assert "TIME ONLINE" in visible
    assert "80 × 24  •  TRUECOLOR" in visible
    assert len(set(re.findall(r"\x1b\[38;2;\d+;\d+;\d+m", session.output))) >= 10


def test_logoff_and_welcome_banners_fill_their_field_slots(db):
    """Issue #929: `{user}`, `{node}` and `{level}` in a log-off banner, and
    `{node}` in the welcome banner, are filled in rather than shown raw."""
    from netbbs.config import set_node_display_name
    from netbbs.net.welcome_banner import banner_path as welcome_banner_path, set_welcome_banner_enabled

    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    set_unicode_style_enabled(db, alice, True)
    set_node_display_name(db, "Nib Quill")
    welcome_banner_path(db).write_bytes(b"WELCOME TO {node 12}   |")
    set_welcome_banner_enabled(db, True)
    logoff_banner_path(db).write_bytes(b"BYE {user 8}FROM {node 12}   AT {level 9}|")
    set_logoff_banner_enabled(db, True)
    session = FakeSession(["alice", "hunter2pw", "y"], keys=["l"])

    asyncio.run(_run_login(session, db))

    visible = _visible(session.output)
    assert "WELCOME TO Nib Quill   |" in visible
    assert "BYE alice   FROM Nib Quill   AT level 10 |" in visible
    assert "{node" not in visible and "{user" not in visible


def test_logoff_summary_formats_duration_and_fits_256_color_terminal(db):
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    set_unicode_style_enabled(db, alice, False)
    session = FakeSession()
    session.terminal_width = 40
    entry = SessionHistoryEntry(
        id=1,
        user_id=alice.id,
        username_label="alice",
        connected_at="2026-01-02T10:00:00.000000Z",
        disconnected_at="2026-01-02T11:02:03.000000Z",
        interrupted_at=None,
        name_visible_fallback=True,
    )

    asyncio.run(_show_logoff_summary_screen(session, db, alice, entry))

    visible = _visible(session.output)
    assert "02.01.2026 10:00" in visible
    assert "02.01.2026 11:02" in visible
    assert "1h 02m 03s" in visible
    assert "40 x 24  /  256 COLOR" in visible
    assert "\x1b[38;2;" not in session.output
    assert all(display_width(line) <= 40 for line in visible.splitlines())


def _bar_colors(output: str) -> list[tuple[str, str]]:
    """The (left, right) truecolor SGR of every framed row's side bars."""
    bars = []
    for line in output.split("\n"):
        found = re.findall(r"(\x1b\[[0-9;]*38;2;(\d+;\d+;\d+)m)[^\x1b]*║", line)
        if len(found) == 2:
            bars.append((found[0][1], found[1][1]))
    return bars


def test_logoff_summary_side_bars_keep_the_rule_colors(db):
    """The side bars used to take each row's content color -- the
    header's, then each fact label's -- instead of continuing the
    rules' corners: the gradient's first stop on the left, its last on
    the right."""
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    set_unicode_style_enabled(db, alice, True)
    session = FakeSession()
    session.supports_truecolor = True
    entry = SessionHistoryEntry(
        id=1,
        user_id=alice.id,
        username_label="alice",
        connected_at="2026-01-02T10:00:00.000000Z",
        disconnected_at="2026-01-02T11:02:03.000000Z",
        interrupted_at=None,
        name_visible_fallback=True,
    )

    asyncio.run(_show_logoff_summary_screen(session, db, alice, entry))

    first = ";".join(map(str, _LOGOFF_SUMMARY_GRADIENT[0]))
    last = ";".join(map(str, _LOGOFF_SUMMARY_GRADIENT[-1]))
    bars = _bar_colors(session.output)
    # Title, subtitle and the four facts.
    assert len(bars) == 6, bars
    assert set(bars) == {(first, last)}, bars


def test_logoff_summary_is_skipped_when_main_menu_exit_is_not_voluntary(db, monkeypatch):
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    set_unicode_style_enabled(db, alice, False)

    async def involuntary_exit(*args, **kwargs):
        return False

    monkeypatch.setattr(login_flow, "_main_menu", involuntary_exit)
    session = FakeSession()

    asyncio.run(
        login_flow.run_authenticated_session(
            session,
            db,
            ChatHub(),
            PresenceRegistry(),
            MessageMailbox(),
            alice,
        )
    )

    assert "C A L L   C O M P L E T E" not in _visible(session.output)


def test_disabled_logoff_banner_still_shows_call_summary_and_goodbye(db):
    create_user(db, "alice", password="hunter2pw", user_level=10)
    session = FakeSession(["alice", "hunter2pw", "y"], keys=["l"])

    asyncio.run(_run_login(session, db))

    visible = _visible(session.output)
    assert "C A L L   C O M P L E T E" in visible
    assert "Goodbye!" in visible


def test_logoff_banner_not_shown_when_login_never_reaches_the_main_menu(db):
    # A failed/abandoned connection never runs the authenticated body at
    # all -- the logoff banner call site is unreachable, not merely
    # skipped.
    logoff_banner_path(db).write_bytes(b"THANKS FOR VISITING")
    set_logoff_banner_enabled(db, True)
    session = FakeSession(["nobody", "wrong-password", "", "", ""])

    asyncio.run(_run_login(session, db, _throttle_config(max_attempts_per_connection=2)))

    assert "THANKS FOR VISITING" not in session.output
