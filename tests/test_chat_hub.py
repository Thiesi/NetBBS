"""Tests for netbbs.chat.hub — in-memory real-time broadcast."""

from __future__ import annotations

import asyncio

from netbbs.chat.hub import ChatHub, ParticipantId, QueueOverflowNotice


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


# -- GitHub issue #31: bounded queues, no blocking on a full one -----------


def test_participant_queue_is_bounded():
    hub = ChatHub(queue_maxsize=5)
    queue = hub.join("lobby", "alice")
    assert queue.maxsize == 5


def test_flooding_a_slow_consumer_does_not_grow_its_queue_unbounded():
    async def scenario():
        hub = ChatHub(queue_maxsize=3)
        queue = hub.join("lobby", "alice")
        for i in range(50):  # far more than the queue can hold
            await hub.broadcast("lobby", f"message {i}")
        return queue

    queue = asyncio.run(scenario())
    assert queue.qsize() <= 3


def test_overflow_drops_oldest_and_inserts_a_notice():
    async def scenario():
        hub = ChatHub(queue_maxsize=2)
        queue = hub.join("lobby", "alice")
        await hub.broadcast("lobby", "first")
        await hub.broadcast("lobby", "second")
        await hub.broadcast("lobby", "third")  # queue was full -- overflow
        return queue

    queue = asyncio.run(scenario())
    # "first" was dropped to make room; "second" and the overflow notice remain.
    remaining = [queue.get_nowait() for _ in range(queue.qsize())]
    assert "first" not in remaining
    assert "second" in remaining
    assert any(isinstance(item, QueueOverflowNotice) for item in remaining)


def test_broadcast_never_blocks_on_one_full_slow_consumer():
    """A full queue must not stall delivery to participants after it in
    the same broadcast -- verified by asserting the fast consumer still
    gets every message even while the slow one is permanently full."""

    async def scenario():
        hub = ChatHub(queue_maxsize=1)
        slow = hub.join("lobby", "slow")  # never drained
        fast = hub.join("lobby", "fast")
        for i in range(20):
            await asyncio.wait_for(hub.broadcast("lobby", f"message {i}"), timeout=1)
            fast.get_nowait()  # keep the fast consumer's queue drained
        return slow

    slow = asyncio.run(scenario())
    assert slow.qsize() <= 1


# -- GitHub issue #26: participants_for_username -----------------------


def test_participants_for_username_finds_only_exact_matches():
    """Regression test for the core bug: the old string encoding
    (f"{username}:{id(session)}") let a session belonging to
    "alice:alt" be matched by a startswith(f"{'alice'}:") check meant
    for canonical user "alice". A real ParticipantId.username equality
    check can't confuse the two."""
    hub = ChatHub()
    hub.join("lobby", ParticipantId(username="alice", session_key=1))
    hub.join("lobby", ParticipantId(username="alice:alt", session_key=2))
    hub.join("lobby", ParticipantId(username="bob", session_key=3))

    found = hub.participants_for_username("lobby", "alice")

    assert len(found) == 1
    assert found[0].username == "alice"
    assert found[0].session_key == 1


def test_participants_for_username_finds_every_session_for_an_account():
    hub = ChatHub()
    hub.join("lobby", ParticipantId(username="alice", session_key=1))
    hub.join("lobby", ParticipantId(username="alice", session_key=2))
    hub.join("lobby", ParticipantId(username="bob", session_key=3))

    found = hub.participants_for_username("lobby", "alice")

    assert {pid.session_key for pid in found} == {1, 2}


def test_participants_for_username_with_no_matches_returns_empty_list():
    hub = ChatHub()
    hub.join("lobby", ParticipantId(username="bob", session_key=1))
    assert hub.participants_for_username("lobby", "alice") == []


def test_send_to_a_full_queue_overflows_instead_of_blocking():
    async def scenario():
        hub = ChatHub(queue_maxsize=1)
        queue = hub.join("lobby", "alice")
        await hub.broadcast("lobby", "first")  # fills the queue
        delivered = await asyncio.wait_for(hub.send_to("lobby", "alice", "second"), timeout=1)
        return queue, delivered

    queue, delivered = asyncio.run(scenario())
    assert delivered is True
    assert queue.qsize() <= 1
