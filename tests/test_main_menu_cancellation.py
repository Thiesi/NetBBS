"""
Regression test for a real bug reported live during dogfooding on the
NetBSD test deployment (a caller sitting idle at the main menu
prompt): `_main_menu`'s direct-invite race (design doc §6.3) awaits
`asyncio.wait({key_task, invite_task}, FIRST_COMPLETED)`. If the
*outer* task running `_main_menu` itself gets cancelled from outside
(e.g. deliberate node shutdown/drain, or the transport noticing an
abrupt disconnect elsewhere), that `CancelledError` is raised at the
`asyncio.wait(...)` call site -- but `asyncio.wait()` being cancelled
does not cancel the tasks it was waiting on. Without an explicit fix,
both `key_task` and `invite_task` were left orphaned: still scheduled,
with nothing left to await their result. `key_task` then raised
`SessionClosedError` the moment the underlying socket actually closed,
and asyncio logged "Task exception was never retrieved" since there
was no one left to retrieve it -- the exact same class of bug
`netbbs.net.chat_flow._chat_loop` already hit and fixed (see
tests/test_chat_flow_cancellation.py), just in the main menu's own
copy of the same race-a-read-against-an-event pattern.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.direct_invites import DirectChatInvites
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.net import main_menu
from netbbs.net.char_input import InputHistory
from netbbs.net.session import Session
from netbbs.storage.database import Database


class _BlockingSession(Session):
    """`read_key()` blocks forever, never resolves on its own -- the
    same shape a real session has while genuinely idle at the main
    menu, which is exactly what lets `_main_menu`'s
    `asyncio.wait(..., FIRST_COMPLETED)` stay pending until something
    external (a real keystroke, an invite, or -- this test's own
    concern -- outer cancellation) ends it."""

    def __init__(self):
        self.terminal_width = 80
        self.node_display_name = "NetBBS"
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"

    async def write(self, text: str) -> None:
        pass

    async def read_key(self, echo: bool = True) -> str:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
        raise AssertionError("not exercised by this test")

    async def read_editor_key(self):
        raise NotImplementedError

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


def test_cancelling_the_outer_task_does_not_orphan_the_invite_race(db, alice):
    """
    Checks the actual mechanism directly via `asyncio.all_tasks()`
    rather than trying to reproduce the exact "Task exception was
    never retrieved" console warning -- same approach as
    `tests/test_chat_flow_cancellation.py`'s equivalent test, for the
    same reason: what both the real bug and this fake session
    genuinely share is the structural defect, observable directly and
    unambiguous either way.
    """

    async def scenario():
        session = _BlockingSession()
        direct_invites = DirectChatInvites()

        tasks_before = asyncio.all_tasks()
        outer = asyncio.create_task(
            main_menu._main_menu(
                session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), alice,
                direct_invites=direct_invites,
            )
        )
        await asyncio.sleep(0)  # let _main_menu start and create key_task/invite_task

        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer

        await asyncio.sleep(0)  # let any cleanup scheduled by the cancellation run

        leftover = asyncio.all_tasks() - tasks_before - {asyncio.current_task()}
        still_pending = [task for task in leftover if not task.done()]
        assert still_pending == [], f"orphaned, still-running tasks left behind: {still_pending}"

    asyncio.run(scenario())
