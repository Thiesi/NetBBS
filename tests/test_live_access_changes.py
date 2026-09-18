"""
Issue #659: a SysOp's change to an account's level (or its
verify-identity permission) reaches that account's live sessions without
a re-login.

These drive the real `run_authenticated_session` -- the function that
starts the account watcher -- with a session whose keys are fed one at a
time, so a test can change the account while the session sits on a
screen and then keep using the same session afterwards. The level
changes are made straight against the database, the way the standalone
`python -m netbbs.admin` CLI makes them, unless a test is specifically
about the in-process wake-up.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import (
    SYSOP_LEVEL,
    UserManagementError,
    create_user,
    get_user_by_id,
    set_can_verify_identity,
    set_user_level,
)
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.net import login_flow
from netbbs.net.admin_flow import admin_menu
from netbbs.net.maintenance import MaintenanceMode
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.net.shutdown import NodeControls
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

_CONSOLE_TITLE = "SysOp operations console"


class FedSession:
    """Keys arrive when the test feeds them, and every read blocks until
    one does -- a caller sitting on a screen for as long as the test
    needs."""

    def __init__(self) -> None:
        self._keys: asyncio.Queue[str] = asyncio.Queue()
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.node_display_name = "NetBBS"
        self.node_name_gradient = None
        self.peer_address = "203.0.113.5"
        self.pinned_notice_hook = None
        self.supports_truecolor = False
        self.discarded = 0

    def feed(self, *keys: str) -> None:
        for key in keys:
            self._keys.put_nowait(key)

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_key(self, echo: bool = True) -> str:
        return await self._keys.get()

    async def read_any_key(self) -> str:
        return await self._keys.get()

    async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
        return await self._keys.get()

    async def discard_buffered_input(self) -> None:
        self.discarded += 1
        while not self._keys.empty():
            self._keys.get_nowait()

    def text(self) -> str:
        return "".join(self.written)

    def text_since(self, mark: int) -> str:
        return "".join(self.written[mark:])


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch):
    monkeypatch.setattr(login_flow, "_REVOCATION_CHECK_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(login_flow, "_REVOCATION_NOTICE_TIMEOUT_SECONDS", 0.05)


@pytest.fixture(autouse=True)
def _skip_sysop_onboarding(monkeypatch):
    # A SysOp's first login stops at the first-run onboarding screen;
    # these tests are about what happens after it.
    async def _answered(session, lane):
        return None

    monkeypatch.setattr(login_flow, "offer_onboarding", _answered)


def _node_controls(registry: ActiveSessionRegistry) -> NodeControls:
    return NodeControls(
        session_registry=registry,
        maintenance=MaintenanceMode(),
        shutdown_event=asyncio.Event(),
        graceful_delay_seconds=0.0,
    )


async def _drive(db, registry, user, session):
    lane = DatabaseLane(db.path)
    registry.enter(session)
    try:
        await login_flow.run_authenticated_session(
            session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), user,
            node_controls=_node_controls(registry), lane=lane,
        )
    finally:
        registry.leave(session)
        lane.close()


async def _until(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


async def _finish(task: asyncio.Task) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _sysops(db):
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    boss = create_user(db, "boss", password="hunter2", user_level=SYSOP_LEVEL)
    return sysop, boss


def test_a_demoted_sysop_is_taken_out_of_the_console_and_the_session_carries_on(db):
    sysop, boss = _sysops(db)
    registry = ActiveSessionRegistry()
    session = FedSession()

    async def scenario():
        task = asyncio.create_task(_drive(db, registry, sysop, session))
        session.feed("n", "s")  # answer the Unicode prompt, open the console
        await _until(lambda: _CONSOLE_TITLE in session.text())
        mark = len(session.written)
        set_user_level(db, sysop, 10, changed_by=boss)
        await _until(lambda: "Your access level is now 10." in session.text_since(mark))
        assert not task.done()
        after = session.text_since(mark)
        # Back on the main menu, drawn for the new level.
        assert "Main menu" in after
        assert "level 10" in after
        assert "ysOp" not in after.split("Your access level is now 10.")[0].split("Main menu")[-1]
        # The console key no longer opens anything.
        mark = len(session.written)
        session.feed("s")
        await asyncio.sleep(0.05)
        assert _CONSOLE_TITLE not in session.text_since(mark)
        # And the session is still a working one: it logs off normally.
        session.feed("l", "y")
        await asyncio.wait_for(task, timeout=2.0)
        assert "Goodbye!" in session.text()

    asyncio.run(scenario())


def test_a_sysop_demoted_from_another_screen_of_the_console_is_unwound_too(db):
    sysop, boss = _sysops(db)
    registry = ActiveSessionRegistry()
    session = FedSession()

    async def scenario():
        task = asyncio.create_task(_drive(db, registry, sysop, session))
        session.feed("n", "s", "u", "p")  # Users -> the promote/demote picker
        await _until(lambda: "Promote/demote which user?" in session.text())
        mark = len(session.written)
        set_user_level(db, sysop, 10, changed_by=boss)
        await _until(lambda: "Your access level is now 10." in session.text_since(mark))
        assert not task.done()
        # Keys typed into the interrupted screen are not replayed at the menu.
        assert session.discarded >= 1
        session.feed("l", "y")
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(scenario())


def test_a_promotion_redraws_an_idle_main_menu_without_interrupting_anything(db):
    caller = create_user(db, "caller", password="hunter2", user_level=10)
    boss = create_user(db, "boss", password="hunter2", user_level=SYSOP_LEVEL)
    registry = ActiveSessionRegistry()
    session = FedSession()

    async def scenario():
        task = asyncio.create_task(_drive(db, registry, caller, session))
        session.feed("n")
        await _until(lambda: "Choice" in session.text())
        mark = len(session.written)
        set_user_level(db, caller, SYSOP_LEVEL, changed_by=boss)
        await _until(lambda: f"Your access level is now {SYSOP_LEVEL}." in session.text_since(mark))
        assert "ysOp" in session.text_since(mark)
        assert session.discarded == 0  # nothing was unwound
        session.feed("s")
        await _until(lambda: _CONSOLE_TITLE in session.text_since(mark))
        await _finish(task)

    asyncio.run(scenario())


def test_a_promotion_while_inside_a_screen_waits_for_the_caller_to_come_back(db):
    caller = create_user(db, "caller", password="hunter2", user_level=10)
    boss = create_user(db, "boss", password="hunter2", user_level=SYSOP_LEVEL)
    registry = ActiveSessionRegistry()
    session = FedSession()

    async def scenario():
        task = asyncio.create_task(_drive(db, registry, caller, session))
        session.feed("n", "p")
        await _until(lambda: "Your public identity and caller preferences." in session.text())
        mark = len(session.written)
        set_user_level(db, caller, 50, changed_by=boss)
        await asyncio.sleep(0.1)  # several watcher ticks
        assert "Your access level" not in session.text_since(mark)
        assert session.discarded == 0  # the profile screen was not interrupted
        session.feed("b")
        await _until(lambda: "Your access level is now 50." in session.text_since(mark))
        assert "level 50" in session.text_since(mark)
        await _finish(task)

    asyncio.run(scenario())


def test_a_verify_permission_revoked_while_idle_is_applied_and_announced(db):
    caller = create_user(db, "caller", password="hunter2", user_level=10)
    boss = create_user(db, "boss", password="hunter2", user_level=SYSOP_LEVEL)
    caller = set_can_verify_identity(db, caller, True, changed_by=boss)
    registry = ActiveSessionRegistry()
    session = FedSession()

    async def scenario():
        task = asyncio.create_task(_drive(db, registry, caller, session))
        session.feed("n")
        await _until(lambda: "erify" in session.text())
        mark = len(session.written)
        set_can_verify_identity(db, caller, False, changed_by=boss)
        await _until(lambda: "You can no longer verify callers' identities." in session.text_since(mark))
        assert "erify" not in session.text_since(mark).split("You can no longer")[0].split("Main menu")[-1]
        await _finish(task)

    asyncio.run(scenario())


def test_an_in_process_change_is_applied_without_waiting_for_the_poll(db, monkeypatch):
    monkeypatch.setattr(login_flow, "_REVOCATION_CHECK_INTERVAL_SECONDS", 30.0)
    sysop, boss = _sysops(db)
    registry = ActiveSessionRegistry()
    session = FedSession()

    async def scenario():
        task = asyncio.create_task(_drive(db, registry, sysop, session))
        session.feed("n", "s")
        await _until(lambda: _CONSOLE_TITLE in session.text())
        mark = len(session.written)
        set_user_level(db, sysop, 10, changed_by=boss)
        registry.request_account_recheck("sysop")  # what the [L]evel action does
        await _until(lambda: "Your access level is now 10." in session.text_since(mark), timeout=1.0)
        await _finish(task)

    asyncio.run(scenario())


def test_drain_stops_treating_a_demoted_sysop_as_exempt(db):
    sysop, boss = _sysops(db)
    registry = ActiveSessionRegistry()
    session = FedSession()

    async def scenario():
        task = asyncio.create_task(_drive(db, registry, sysop, session))
        session.feed("n")
        await _until(lambda: "Choice" in session.text())
        await registry.broadcast_to_all("before", exclude_sysops=True)
        assert "before" not in session.text()
        set_user_level(db, sysop, 10, changed_by=boss)
        await _until(lambda: "Your access level is now 10." in session.text())
        await registry.broadcast_to_all("after", exclude_sysops=True)
        assert "after" in session.text()
        await _finish(task)

    asyncio.run(scenario())


def test_a_real_disconnect_during_a_pending_unwind_still_disconnects(db):
    sysop, _boss = _sysops(db)
    registry = ActiveSessionRegistry()
    session = FedSession()

    async def scenario():
        task = asyncio.create_task(_drive(db, registry, sysop, session))
        session.feed("n", "s")
        await _until(lambda: _CONSOLE_TITLE in session.text())
        # Both land before the session task runs again.
        assert registry.request_level_unwind(session)
        assert registry.cancel_one(session)
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=2.0)
        assert task.done()
        assert "Goodbye!" not in session.text()
        assert len(registry) == 0

    asyncio.run(scenario())


def test_a_refused_demotion_changes_nothing_live(db):
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    registry = ActiveSessionRegistry()
    session = FedSession()

    async def scenario():
        task = asyncio.create_task(_drive(db, registry, sysop, session))
        session.feed("n", "s")
        await _until(lambda: _CONSOLE_TITLE in session.text())
        mark = len(session.written)
        with pytest.raises(UserManagementError):
            set_user_level(db, sysop, 10, changed_by=sysop)  # the node's last SysOp
        registry.request_account_recheck("sysop")
        await asyncio.sleep(0.1)
        assert "Your access level" not in session.text_since(mark)
        assert "Main menu" not in session.text_since(mark)
        await _finish(task)

    asyncio.run(scenario())


def test_the_console_closes_itself_when_its_operator_loses_sysop_level(db):
    """The standalone CLI runs `admin_menu` with no live node, so no
    watcher: the console's own boundary is what stops a demoted
    operator there."""
    sysop, boss = _sysops(db)
    session = FedSession()

    async def scenario():
        lane = DatabaseLane(db.path)
        try:
            task = asyncio.create_task(admin_menu(session, lane, sysop))
            await _until(lambda: _CONSOLE_TITLE in session.text())
            set_user_level(db, sysop, 10, changed_by=boss)
            session.feed("r")
            await asyncio.wait_for(task, timeout=2.0)
        finally:
            lane.close()

    asyncio.run(scenario())
    assert "Your account no longer has SysOp access." in session.text()
    assert session.text().count(_CONSOLE_TITLE) == 1  # the [R]efresh never ran
    assert get_user_by_id(db, sysop.id).user_level == 10


def test_an_unwind_is_refused_outside_the_main_menu_and_while_one_is_pending():
    registry = ActiveSessionRegistry()
    session = object()

    async def scenario():
        registry.enter(session)
        assert not registry.request_level_unwind(session)  # not armed
        registry.arm_level_unwind(session, True)
        blocker = asyncio.Event()

        async def victim():
            try:
                await blocker.wait()
            except asyncio.CancelledError:
                pass  # a screen that swallows the unwind
            return asyncio.current_task().cancelling()

        # The registry's entry has to name the victim's task.
        registry._sessions[session].task = asyncio.create_task(victim())
        await asyncio.sleep(0)
        assert registry.request_level_unwind(session)
        assert not registry.request_level_unwind(session)  # already pending
        assert await registry._sessions[session].task == 1
        # The main menu's ordinary path retires the swallowed request.
        assert registry.finish_level_unwind(session) is True
        assert registry._sessions[session].task.cancelling() == 0
        assert registry.finish_level_unwind(session) is False

    asyncio.run(scenario())
