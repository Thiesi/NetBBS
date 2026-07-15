"""
Regression tests for GitHub issue #29, reopened a second time: the
cross-process disable/delete revalidation (`netbbs.auth.users.
account_still_active`) only ever ran at the main-menu boundary and,
after the first reopening, at chat's own send-loop boundary too -- but
several other long-running authenticated loops (board browsing/posting,
file areas, the profile screen, and the whole admin menu tree) had none
at all, and none of the per-loop checks ever caught a session that was
simply *idle*, waiting on input that would never come.

`netbbs.net.login_flow._watch_for_account_revocation` is the fix: a
background task, one per authenticated session, that periodically
re-checks the account and forcibly cancels the session from outside --
covering every loop at once, present or future, and idle sessions too.
These tests drive the real `run_authenticated_session` (the function
that actually creates the watcher, not `_main_menu` in isolation) with
`_REVOCATION_CHECK_INTERVAL_SECONDS` patched down to a test-friendly
interval.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user, delete_user, set_user_disabled
from netbbs.boards.boards import create_board
from netbbs.boards.posts import list_posts_page
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.directory import get_bio
from netbbs.files.areas import create_file_area
from netbbs.net import login_flow
from netbbs.net.maintenance import MaintenanceMode
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.net.shutdown import NodeControls
from netbbs.storage.database import Database


class FakeSession:
    """One ordered queue serves both `read_key()` and `read_line()` --
    lets a single scripted list drive a scenario through the main menu
    and into whichever submenu loop a test needs. Blocks forever once
    exhausted, the same shape a real session has while genuinely idle,
    waiting on input that never comes -- exactly the condition this
    watcher exists to catch."""

    def __init__(self, inputs=None):
        self._inputs = list(inputs or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_key(self, echo: bool = True) -> str:
        if self._inputs:
            return self._inputs.pop(0)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def read_line(self, echo: bool = True, history=None, completer=None) -> str:
        if self._inputs:
            return self._inputs.pop(0)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch):
    # Real interval (5s) would make every test glacially slow -- this
    # is purely an implementation timing knob, not behavior under test.
    monkeypatch.setattr(login_flow, "_REVOCATION_CHECK_INTERVAL_SECONDS", 0.02)


def _node_controls(registry: ActiveSessionRegistry) -> NodeControls:
    return NodeControls(
        session_registry=registry,
        maintenance=MaintenanceMode(),
        shutdown_event=asyncio.Event(),
        graceful_delay_seconds=0.0,
    )


async def _drive(db, hub, presence, mailbox, registry, user, session):
    """Mirrors what netbbs.net.login_flow.handle_session actually does
    around run_authenticated_session -- registering/deregistering the
    session with the registry from the same task that runs it, exactly
    as the watcher's own cancel_one() call requires (it looks the
    session up by identity in that registry)."""
    registry.enter(session)
    try:
        await login_flow.run_authenticated_session(
            session, db, hub, presence, mailbox, user, node_controls=_node_controls(registry)
        )
    finally:
        registry.leave(session)


async def _wait_until_done(task: asyncio.Task, *, timeout: float = 2.0) -> None:
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=timeout)


def test_watcher_disconnects_a_genuinely_idle_session_at_the_main_menu(db):
    """No in-loop check exists that would ever catch this on its own --
    nothing is ever typed after login, so there's no "next keystroke"
    for _main_menu's own check to run on. Only the watcher can end
    this."""
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    hub, presence, mailbox = ChatHub(), PresenceRegistry(), MessageMailbox()
    registry = ActiveSessionRegistry()
    session = FakeSession([])  # never types anything at all

    async def scenario():
        task = asyncio.create_task(_drive(db, hub, presence, mailbox, registry, alice, session))
        await asyncio.sleep(0.05)  # let it actually reach the idle main-menu read
        set_user_disabled(db, alice, True, changed_by=alice)
        await _wait_until_done(task)

    asyncio.run(scenario())
    assert "no longer active" in _written_text(session)


def test_watcher_does_not_disconnect_a_still_active_session(db):
    hub, presence, mailbox = ChatHub(), PresenceRegistry(), MessageMailbox()
    registry = ActiveSessionRegistry()
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["l"])  # logs off normally, on its own

    async def scenario():
        task = asyncio.create_task(_drive(db, hub, presence, mailbox, registry, alice, session))
        await _wait_until_done(task)

    asyncio.run(scenario())
    assert "no longer active" not in _written_text(session)
    assert "Goodbye!" in _written_text(session)


def test_watcher_task_does_not_leak_after_normal_logoff(db):
    hub, presence, mailbox = ChatHub(), PresenceRegistry(), MessageMailbox()
    registry = ActiveSessionRegistry()
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["l"])

    async def scenario():
        current = asyncio.current_task()
        await _drive(db, hub, presence, mailbox, registry, alice, session)
        leaked = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        assert leaked == []

    asyncio.run(scenario())


def test_watcher_disconnects_a_session_stuck_composing_a_board_post(db):
    """The board-browsing/posting loop has no in-loop revalidation of
    its own -- proves the watcher, not some pre-existing check, is what
    ends this, and that the post genuinely never gets created."""
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=alice)
    hub, presence, mailbox = ChatHub(), PresenceRegistry(), MessageMailbox()
    registry = ActiveSessionRegistry()
    # m -> boards, 01 -> pick "general" (only board, empty -- skips
    # straight to the compose prompt), then block on the subject read.
    session = FakeSession(["m", "0", "1"])

    async def scenario():
        task = asyncio.create_task(_drive(db, hub, presence, mailbox, registry, alice, session))
        await asyncio.sleep(0.05)  # let it actually reach the blocked subject prompt
        set_user_disabled(db, alice, True, changed_by=alice)
        await _wait_until_done(task)

    asyncio.run(scenario())
    assert list_posts_page(db, board, alice).posts == []


def test_watcher_disconnects_a_session_stuck_inside_a_file_area(db):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    create_file_area(db, "docs", creator=alice)
    hub, presence, mailbox = ChatHub(), PresenceRegistry(), MessageMailbox()
    registry = ActiveSessionRegistry()
    # f -> file areas, 01 -> pick "docs" (only area, empty -- lands on
    # its own "Command (or press Enter to go back):" prompt), block.
    session = FakeSession(["f", "0", "1"])

    async def scenario():
        task = asyncio.create_task(_drive(db, hub, presence, mailbox, registry, alice, session))
        await asyncio.sleep(0.05)
        set_user_disabled(db, alice, True, changed_by=alice)
        await _wait_until_done(task)
        return task

    task = asyncio.run(scenario())
    assert task.done()


def test_watcher_disconnects_a_session_stuck_inside_the_profile_screen(db):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    hub, presence, mailbox = ChatHub(), PresenceRegistry(), MessageMailbox()
    registry = ActiveSessionRegistry()
    # p -> profile, then block on the profile sub-menu's own key read.
    session = FakeSession(["p"])

    async def scenario():
        task = asyncio.create_task(_drive(db, hub, presence, mailbox, registry, alice, session))
        await asyncio.sleep(0.05)
        set_user_disabled(db, alice, True, changed_by=alice)
        await _wait_until_done(task)

    asyncio.run(scenario())
    assert get_bio(db, alice) is None  # no edit ever began, let alone committed


def test_watcher_disconnects_a_disabled_sysop_stuck_inside_the_admin_menu(db):
    """The sharpest scenario from the reopened issue: a SysOp already
    inside admin_menu (or any of its nested screens) when disabled/
    deleted through a separate process must not be able to keep issuing
    privileged commands indefinitely just by never returning to the
    main menu."""
    sysop = create_user(db, "sysop", password="hunter2", user_level=100)
    hub, presence, mailbox = ChatHub(), PresenceRegistry(), MessageMailbox()
    registry = ActiveSessionRegistry()
    # a -> admin menu, then block on its own top-level key read.
    session = FakeSession(["a"])

    async def scenario():
        task = asyncio.create_task(_drive(db, hub, presence, mailbox, registry, sysop, session))
        await asyncio.sleep(0.05)
        delete_user(db, sysop, deleted_by=sysop)
        await _wait_until_done(task)
        return task

    task = asyncio.run(scenario())
    assert task.done()
    # No further users exist -- confirms nothing this now-deleted SysOp
    # could have typed next (e.g. creating another account) ever ran.
    from netbbs.auth.users import list_users

    assert [u.username for u in list_users(db)] == []
