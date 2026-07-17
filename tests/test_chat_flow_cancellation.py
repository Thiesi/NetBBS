"""
Regression test for a real bug hit on Thiesi's own NetBSD deployment
(Ctrl-C with a chat session connected, not just reasoned about in the
abstract): `_chat_loop` runs two concurrent child tasks
(`receive_task`/`send_task`) and awaits `asyncio.wait(...,
FIRST_COMPLETED)` on them. If the *outer* task running `_chat_loop`
itself gets cancelled from outside (e.g. deliberate node shutdown,
design doc round 51's `ActiveSessionRegistry.disconnect_all()`), that
`CancelledError` is raised at the `asyncio.wait(...)` call site --
but `asyncio.wait()` being cancelled does not cancel the tasks it was
waiting on. Without an explicit fix, both child tasks were left
orphaned: still scheduled, with nothing left to await their result.
One of them would then raise `SessionClosedError` the moment the
underlying socket actually closed, and asyncio logged "Task exception
was never retrieved" at process exit since there was no one left to
retrieve it.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.net import chat_flow
from netbbs.net.char_input import InputHistory
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_chat_flow_moderation import FakeSession


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def channel(db, alice):
    return create_channel(db, "general", creator=alice)


def test_cancelling_the_outer_task_does_not_orphan_child_tasks(lane, alice, channel):
    """
    Checks the actual mechanism directly via `asyncio.all_tasks()`
    rather than trying to reproduce the exact "Task exception was never
    retrieved" console warning: `FakeSession.read_line()` blocks on a
    bare `asyncio.Event().wait()`, which -- unlike a real socket whose
    pending read fails with `SessionClosedError` once the connection is
    actually torn down -- never raises anything on its own, so a naive
    "assert no exception was ever logged" version of this test passed
    even *without* the fix (verified by hand, then thrown out). What
    both the real bug and this fake session genuinely share is the
    structural defect: after the outer task finishes, `receive_task`/
    `send_task` are left as still-pending, still-scheduled tasks nobody
    is awaiting -- observable directly, and unambiguous either way.
    """

    async def scenario():
        hub = ChatHub()
        presence = PresenceRegistry()
        mailbox = MessageMailbox()
        history = InputHistory()
        session = FakeSession()  # never types anything -- send_loop blocks forever

        tasks_before = asyncio.all_tasks()
        outer = asyncio.create_task(
            chat_flow._chat_loop(session, lane, hub, presence, mailbox, history, channel, alice)
        )
        await asyncio.sleep(0)  # let _chat_loop start and create its two child tasks

        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer

        await asyncio.sleep(0)  # let any cleanup scheduled by the cancellation run

        leftover = asyncio.all_tasks() - tasks_before - {asyncio.current_task()}
        still_pending = [task for task in leftover if not task.done()]
        assert still_pending == [], f"orphaned, still-running tasks left behind: {still_pending}"

    asyncio.run(scenario())


def test_cancelling_the_outer_task_still_runs_leave_cleanup(lane, alice, channel):
    """The `finally:` block's own cleanup (hub.leave, the "has left the
    channel" broadcast) must still run on this path -- confirms the fix
    re-raises CancelledError rather than swallowing it or returning
    early before the existing finally: block."""

    async def scenario():
        hub = ChatHub()
        presence = PresenceRegistry()
        mailbox = MessageMailbox()
        history = InputHistory()
        session = FakeSession()

        outer = asyncio.create_task(
            chat_flow._chat_loop(session, lane, hub, presence, mailbox, history, channel, alice)
        )
        while hub.participant_count(channel.name) < 1:
            await asyncio.sleep(0)
        assert hub.participant_count(channel.name) == 1

        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer

        assert hub.participant_count(channel.name) == 0

    asyncio.run(scenario())
