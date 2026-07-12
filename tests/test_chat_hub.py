"""Tests for netbbs.chat.hub — in-memory real-time broadcast."""

from __future__ import annotations

import asyncio

from netbbs.chat.hub import ChatHub


def test_join_returns_a_queue():
    hub = ChatHub()
    queue = hub.join("lobby", "alice")
    assert isinstance(queue, asyncio.Queue)


def test_broadcast_delivers_to_all_participants():
    async def scenario():
        hub = ChatHub()
        qa = hub.join("lobby", "alice")
        qb = hub.join("lobby", "bob")
        await hub.broadcast("lobby", "hello")
        assert qa.get_nowait() == "hello"
        assert qb.get_nowait() == "hello"

    asyncio.run(scenario())


def test_broadcast_excludes_given_participants():
    async def scenario():
        hub = ChatHub()
        qa = hub.join("lobby", "alice")
        qb = hub.join("lobby", "bob")
        await hub.broadcast("lobby", "hello", exclude={"alice"})
        assert qa.empty()
        assert qb.get_nowait() == "hello"

    asyncio.run(scenario())


def test_broadcast_to_empty_channel_does_nothing():
    async def scenario():
        hub = ChatHub()
        await hub.broadcast("empty-channel", "hello")  # must not raise

    asyncio.run(scenario())


def test_leave_removes_participant():
    hub = ChatHub()
    hub.join("lobby", "alice")
    assert hub.participant_count("lobby") == 1
    hub.leave("lobby", "alice")
    assert hub.participant_count("lobby") == 0


def test_leave_nonexistent_participant_does_not_raise():
    hub = ChatHub()
    hub.leave("lobby", "nobody-here")  # must not raise


def test_leaving_participant_does_not_receive_further_broadcasts():
    async def scenario():
        hub = ChatHub()
        queue = hub.join("lobby", "alice")
        hub.leave("lobby", "alice")
        await hub.broadcast("lobby", "hello")
        assert queue.empty()

    asyncio.run(scenario())


def test_channels_are_independent():
    async def scenario():
        hub = ChatHub()
        q_lobby = hub.join("lobby", "alice")
        q_dev = hub.join("dev", "alice")
        await hub.broadcast("lobby", "lobby message")
        assert q_lobby.get_nowait() == "lobby message"
        assert q_dev.empty()

    asyncio.run(scenario())


def test_participant_count_reflects_joins_and_leaves():
    hub = ChatHub()
    assert hub.participant_count("lobby") == 0
    hub.join("lobby", "alice")
    hub.join("lobby", "bob")
    assert hub.participant_count("lobby") == 2
    hub.leave("lobby", "alice")
    assert hub.participant_count("lobby") == 1


def test_broadcast_survives_concurrent_leave_mid_iteration():
    """
    Regression test for a real, verified concurrency bug: iterating the
    live participant dict while awaiting inside the loop (queue.put
    yields control back to the event loop) allows another coroutine to
    call leave() mid-broadcast and mutate the dict being iterated, which
    raises "RuntimeError: dictionary changed size during iteration".
    Confirmed this reproduces with a naive implementation before fixing
    it with a snapshot in ChatHub.broadcast — this test guards against
    that fix ever being lost in a future refactor.
    """

    async def scenario():
        hub = ChatHub()
        qa = hub.join("lobby", "alice")
        hub.join("lobby", "bob")
        qc = hub.join("lobby", "carol")

        async def broadcaster():
            await hub.broadcast("lobby", "hello everyone")

        async def leaver():
            await asyncio.sleep(0)  # let broadcast begin first
            hub.leave("lobby", "bob")

        await asyncio.gather(broadcaster(), leaver())  # must not raise

        assert qa.get_nowait() == "hello everyone"
        assert qc.get_nowait() == "hello everyone"
        assert hub.participant_count("lobby") == 2

    asyncio.run(scenario())


def test_multiple_broadcasts_are_delivered_in_order():
    async def scenario():
        hub = ChatHub()
        queue = hub.join("lobby", "alice")
        await hub.broadcast("lobby", "first")
        await hub.broadcast("lobby", "second")
        assert queue.get_nowait() == "first"
        assert queue.get_nowait() == "second"

    asyncio.run(scenario())
