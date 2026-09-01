"""
Regression tests for the main menu's SysOp entry: keystroke "s"
(BBS-conventional "SysOp" naming, Thiesi's own explicit request),
replacing the previous generic "a"/"Admin" -- `netbbs.net.login_flow.
_draw_main_menu`'s label and `_main_menu`'s dispatch branch.
"""

from __future__ import annotations

import asyncio
import re

from netbbs.auth.users import create_user
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.net.char_input import InputHistory
from netbbs.net.main_menu import _draw_main_menu, _main_menu
from netbbs.net.maintenance import MaintenanceMode
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.net.shutdown import NodeControls
from netbbs.rendering import CLOCK_COLOR, MUTED_COLOR
from netbbs.rendering.ansi import fg
from netbbs.storage.database import Database


class FakeSession:
    def __init__(self, keys=None):
        self._keys = iter(keys or [])
        # Every scripted `keys` list here ends with "l" to leave the
        # main menu cleanly, which now requires a logoff confirmation
        # -- a single "y" answer, and nothing beyond it, since no test
        # in this file exercises any other read_line-driven prompt.
        self._lines = iter(["y"])
        self.written: list[str] = []
        self.terminal_width = 80
        self.node_display_name = "NetBBS"
        self.node_name_gradient = None
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_key(self, echo: bool = True) -> str:
        key = next(self._keys, None)
        if key is None:
            raise AssertionError("FakeSession.read_key() called with no more scripted keys")
        return key

    async def read_line(self, echo: bool = True) -> str:
        line = next(self._lines, None)
        if line is None:
            raise AssertionError("read_line should not be reached by these tests")
        return line


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _visible_text(session: FakeSession) -> str:
    """`_written_text` with SGR escape sequences stripped -- needed
    since `menu_key` colors only the bracketed hotkey letter itself, so
    e.g. "SysOp" is split across a run of raw bytes as "S" + an SGR
    reset + "ysOp", not one contiguous substring, in the unstripped
    written text."""
    return _ANSI_ESCAPE_RE.sub("", _written_text(session))


def test_main_menu_shows_sysop_option_for_a_sysop_level_user(tmp_path):
    db = Database(tmp_path / "node.db")
    sysop = create_user(db, "root", password="hunter2", user_level=255)
    session = FakeSession()

    asyncio.run(_draw_main_menu(session, db, MessageMailbox(), sysop))

    text = _visible_text(session)
    assert "[S]ysOp" in text
    assert "Admin" not in text
    db.close()


def test_main_menu_hides_sysop_option_for_an_ordinary_user(tmp_path):
    db = Database(tmp_path / "node.db")
    user = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession()

    asyncio.run(_draw_main_menu(session, db, MessageMailbox(), user))

    assert "SysOp" not in _visible_text(session)
    db.close()


def test_main_menu_is_a_two_column_home_surface_at_classic_width(tmp_path):
    db = Database(tmp_path / "node.db")
    user = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession()

    asyncio.run(_draw_main_menu(session, db, MessageMailbox(), user))

    lines = _visible_text(session).splitlines()
    # Style spec (round following the pre-5.0.0 "beautify" audit): the
    # main menu -- the single most-viewed screen in the app -- now
    # actually uses this account's own unicode_style preference (on by
    # default) instead of silently falling back to plain ASCII.
    assert "NetBBS › Main menu" in lines
    assert "alice › level 10 › mail caught up" in lines
    assert any("EXPLORE" in line and "YOU" in line for line in lines)
    db.close()


def test_main_menu_collapses_sections_at_minimum_width(tmp_path):
    db = Database(tmp_path / "node.db")
    user = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession()
    session.terminal_width = 40

    asyncio.run(_draw_main_menu(session, db, MessageMailbox(), user))

    lines = _visible_text(session).splitlines()
    assert "EXPLORE" in lines
    assert "YOU" in lines
    assert not any("EXPLORE" in line and "YOU" in line for line in lines)
    assert all(len(line) <= 40 for line in lines if "Choice:" not in line)
    db.close()


def test_main_menu_shows_descriptions_by_default(tmp_path):
    # GitHub issue #160: descriptions on by default -- a caller who has
    # never touched the setting still gets the discoverability benefit.
    db = Database(tmp_path / "node.db")
    user = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession()

    asyncio.run(_draw_main_menu(session, db, MessageMailbox(), user))

    text = _visible_text(session)
    assert "Look up other callers" in text  # [D]irectory's own description
    db.close()


def test_main_menu_hides_descriptions_when_the_user_turns_them_off(tmp_path):
    from netbbs.net.menu_description_preference import set_menu_description_level

    db = Database(tmp_path / "node.db")
    user = create_user(db, "alice", password="hunter2", user_level=10)
    set_menu_description_level(db, user, "off")
    session = FakeSession()

    asyncio.run(_draw_main_menu(session, db, MessageMailbox(), user))

    assert "Look up other callers" not in _visible_text(session)
    db.close()


def test_pressing_s_reaches_the_sysop_branch_for_a_sysop_level_user(tmp_path):
    db = Database(tmp_path / "node.db")
    sysop = create_user(db, "root", password="hunter2", user_level=255)
    # No `lane` supplied (matches every other bare _main_menu() test call
    # in this suite) -- routes into the "not available in this context"
    # fallback rather than a real admin_menu, but that's still enough to
    # prove "s" reaches the SysOp branch at all, which is what's under
    # test here, not admin_menu's own behavior (covered separately in
    # tests/test_admin_flow.py).
    session = FakeSession(keys=["s", "l"])

    asyncio.run(
        _main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), sysop)
    )

    assert "SysOp menu is not available in this context." in _written_text(session)
    db.close()


def test_pressing_a_is_now_an_invalid_key_for_a_sysop_level_user(tmp_path):
    """"a" used to be the Admin keystroke -- confirms it's been fully
    retired, not left as a silent second way in, the same "only one
    real keystroke per option" invariant every other menu here has."""
    db = Database(tmp_path / "node.db")
    sysop = create_user(db, "root", password="hunter2", user_level=255)
    session = FakeSession(keys=["a", "l"])

    asyncio.run(
        _main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), sysop)
    )

    text = _written_text(session)
    assert "\b \b\a" in text  # rejected as an invalid keystroke -- bell only
    assert "SysOp menu is not available in this context." not in text
    db.close()


def test_pressing_ctrl_r_on_the_main_menu_only_bells_no_erase(tmp_path):
    """Real dogfood-reported bug fix: Ctrl-R (REFRESH_KEY) has no
    meaning on the main menu (unlike inside a picker), and is returned
    *unechoed* by read_key() -- before the fix, falling through to the
    ordinary reject_keystroke() here erased the previous real character
    on screen instead of nothing, since nothing was actually echoed for
    this keystroke. Contrast with the "a" test above, which presses an
    ordinary rejected key and *does* expect an erase."""
    db = Database(tmp_path / "node.db")
    sysop = create_user(db, "root", password="hunter2", user_level=255)
    session = FakeSession(keys=["\x12", "l"])

    asyncio.run(
        _main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), sysop)
    )

    text = _written_text(session)
    assert "\a" in text
    assert "\b \b" not in text
    db.close()


def test_pressing_s_does_nothing_for_a_non_sysop_user(tmp_path):
    db = Database(tmp_path / "node.db")
    user = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(keys=["s", "l"])

    asyncio.run(
        _main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), user)
    )

    text = _written_text(session)
    assert "\b \b\a" in text  # rejected -- meets_level(SYSOP_LEVEL) fails
    assert "SysOp menu is not available in this context." not in text
    db.close()


# -- the Choice: prompt's own BBS-time/status-tag prefix (design doc -- ------
# -- node management, Thiesi's own request) -----------------------------


def _node_controls() -> NodeControls:
    return NodeControls(
        session_registry=ActiveSessionRegistry(),
        maintenance=MaintenanceMode(),
        shutdown_event=asyncio.Event(),
        graceful_delay_seconds=60.0,
    )


def test_prompt_is_unchanged_without_node_controls(tmp_path):
    """The many existing tests in this file (and elsewhere) that call
    `_draw_main_menu`/`_main_menu` without a `node_controls` at all must
    see the exact same prompt they always have -- no time, no tag."""
    db = Database(tmp_path / "node.db")
    user = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession()

    asyncio.run(_draw_main_menu(session, db, MessageMailbox(), user))

    assert _written_text(session).endswith("Choice: ")
    db.close()


def test_prompt_shows_bbs_time_with_node_controls_and_nothing_scheduled(tmp_path):
    db = Database(tmp_path / "node.db")
    user = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession()
    node_controls = _node_controls()

    asyncio.run(_draw_main_menu(session, db, MessageMailbox(), user, node_controls=node_controls))

    text = _written_text(session)
    assert text.endswith("Choice: ")
    assert "[DRAINING" not in text
    assert "[SHUTDOWN" not in text
    assert "[MAINT MODE]" not in text


def test_prompt_shows_a_draining_tag_when_a_drain_is_scheduled(tmp_path):
    async def scenario():
        db = Database(tmp_path / "node.db")
        try:
            user = create_user(db, "alice", password="hunter2", user_level=10)
            session = FakeSession()
            node_controls = _node_controls()
            loop = asyncio.get_running_loop()
            task = asyncio.create_task(asyncio.Event().wait())
            node_controls.drain_scheduler.schedule(task, deadline=loop.time() + 42.0, message=None)

            await _draw_main_menu(session, db, MessageMailbox(), user, node_controls=node_controls)

            assert "[DRAINING" in _written_text(session)

            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            db.close()

    asyncio.run(scenario())


def test_prompt_shows_both_shutdown_and_drain_tags_when_both_scheduled(tmp_path):
    """Design doc §13.8, Thiesi's own dogfood-testing report: every
    currently-applicable tag is shown, not just the single most urgent
    one -- `[L]ock & drain` makes "more than one active" a common case,
    not a rare edge case, so dropping one silently would recreate the
    exact blind spot `_draw_node_menu`'s own docstring already describes
    for the separate-toggle case."""
    async def scenario():
        db = Database(tmp_path / "node.db")
        try:
            user = create_user(db, "alice", password="hunter2", user_level=10)
            session = FakeSession()
            node_controls = _node_controls()
            loop = asyncio.get_running_loop()
            drain_task = asyncio.create_task(asyncio.Event().wait())
            shutdown_task = asyncio.create_task(asyncio.Event().wait())
            node_controls.drain_scheduler.schedule(drain_task, deadline=loop.time() + 42.0, message=None)
            node_controls.shutdown_scheduler.schedule(shutdown_task, deadline=loop.time() + 99.0, message=None)

            await _draw_main_menu(session, db, MessageMailbox(), user, node_controls=node_controls)

            text = _written_text(session)
            assert "[SHUTDOWN" in text
            assert "[DRAINING" in text
            # Documented order: shutdown first, then drain.
            assert text.index("[SHUTDOWN") < text.index("[DRAINING")

            for task in (drain_task, shutdown_task):
                task.cancel()
            await asyncio.gather(drain_task, shutdown_task, return_exceptions=True)
        finally:
            db.close()

    asyncio.run(scenario())


def test_prompt_shows_a_maintenance_mode_tag_for_a_sysop_when_lockdown_is_on(tmp_path):
    db = Database(tmp_path / "node.db")
    sysop = create_user(db, "root", password="hunter2", user_level=255)
    session = FakeSession()
    node_controls = _node_controls()
    node_controls.maintenance.enable_lockdown()

    asyncio.run(_draw_main_menu(session, db, MessageMailbox(), sysop, node_controls=node_controls))

    assert "[MAINT MODE]" in _written_text(session)
    db.close()


def test_prompt_shows_drain_and_maintenance_tags_together(tmp_path):
    """The actual scenario `[L]ock & drain` (design doc §13.8) makes
    common: both a scheduled drain and maintenance-mode lockdown active
    at once, for a SysOp -- both tags must appear, not just one."""
    async def scenario():
        db = Database(tmp_path / "node.db")
        try:
            sysop = create_user(db, "root", password="hunter2", user_level=255)
            session = FakeSession()
            node_controls = _node_controls()
            node_controls.maintenance.enable_lockdown()
            loop = asyncio.get_running_loop()
            drain_task = asyncio.create_task(asyncio.Event().wait())
            node_controls.drain_scheduler.schedule(drain_task, deadline=loop.time() + 42.0, message=None)

            await _draw_main_menu(session, db, MessageMailbox(), sysop, node_controls=node_controls)

            text = _written_text(session)
            assert "[DRAINING" in text
            assert "[MAINT MODE]" in text
            assert text.index("[DRAINING") < text.index("[MAINT MODE]")

            drain_task.cancel()
            await asyncio.gather(drain_task, return_exceptions=True)
        finally:
            db.close()

    asyncio.run(scenario())


def test_prompt_clock_is_time_only_two_toned_and_has_no_date(tmp_path):
    """Thiesi's own follow-up request: the date (static clutter across an
    entire session -- see `_main_menu_prompt`'s own docstring) is gone,
    and the remaining `HH:MM:SS` is a two-tone "digital clock" -- digit
    groups in `CLOCK_COLOR`, `:` separators in `MUTED_COLOR` -- rather
    than one flat color. `CLOCK_COLOR`, not `HEADER_COLOR`: a second
    follow-up request after the first version shared `HEADER_COLOR`
    with the "Main menu" label and read as part of it."""
    db = Database(tmp_path / "node.db")
    user = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession()
    node_controls = _node_controls()

    asyncio.run(_draw_main_menu(session, db, MessageMailbox(), user, node_controls=node_controls))

    prompt = session.written[-1]
    stripped = re.sub(r"\x1b\[[0-9;]*m", "", prompt)
    assert re.match(r"^\d{2}:\d{2}:\d{2} Choice: $", stripped)
    # No leftover date component (e.g. "24.07.2026") anywhere.
    assert not re.search(r"\d{2}\.\d{2}\.\d{4}", stripped)
    assert prompt.count(fg(CLOCK_COLOR)) == 3  # HH, MM, SS
    assert prompt.count(fg(MUTED_COLOR)) == 2  # the two ":" separators
    db.close()
