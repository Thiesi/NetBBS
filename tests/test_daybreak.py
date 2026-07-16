"""
Tests for the local per-node "new day" chat announcement (design doc
round 78) -- `netbbs.net.daybreak`. Pure-function pieces (message
formatting, midnight scheduling math, the "which channels are
occupied" filter) are tested directly; `announce_new_day` and
`run_daybreak_announcer` are driven through a real `ChatHub`/`Database`
the same way other chat integration tests in this suite are, with
`now`/`sleep` injected on the announcer loop so nothing here actually
waits in real time (same shape as `netbbs.net.throttle.LoginThrottle`'s
own `clock` injection).
"""

from __future__ import annotations

import asyncio
import datetime

import pytest
from zoneinfo import ZoneInfo

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub, ParticipantId
from netbbs.chat.scrollback import get_scrollback, record_message
from netbbs.net.chat_flow import _TimestampedNotice, _render_scrollback_message
from netbbs.net.daybreak import (
    _channels_with_participants,
    _ordinal_suffix,
    _seconds_until_next_local_midnight,
    announce_new_day,
    format_daybreak_message,
    run_daybreak_announcer,
)
from netbbs.storage.database import Database
from netbbs.timeutil import set_display_timezone


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


# -- format_daybreak_message / _ordinal_suffix -------------------------


def test_message_matches_thiesis_own_worked_example():
    text = format_daybreak_message(datetime.date(2029, 4, 3))
    assert text == (
        "Good morning, chatters! A new day has just begun: where this node "
        "lives, it is now Tuesday, April 3rd, 2029."
    )


@pytest.mark.parametrize(
    "day,suffix",
    [(1, "st"), (2, "nd"), (3, "rd"), (4, "th"), (11, "th"), (12, "th"), (13, "th"),
     (21, "st"), (22, "nd"), (23, "rd"), (24, "th"), (31, "st")],
)
def test_ordinal_suffix(day, suffix):
    assert _ordinal_suffix(day) == suffix


# -- _seconds_until_next_local_midnight ---------------------------------


def test_seconds_until_midnight_just_before():
    tz = ZoneInfo("UTC")
    now = datetime.datetime(2029, 4, 3, 23, 59, 0, tzinfo=tz)
    assert _seconds_until_next_local_midnight(now) == 60.0


def test_seconds_until_midnight_just_after():
    tz = ZoneInfo("UTC")
    now = datetime.datetime(2029, 4, 3, 0, 0, 1, tzinfo=tz)
    assert _seconds_until_next_local_midnight(now) == 86399.0


def test_seconds_until_midnight_is_always_positive_at_midnight_itself():
    """At exactly midnight, the *next* midnight is a full day away, not
    zero -- this function is only ever called right after waking from a
    sleep that already landed on the previous midnight."""
    tz = ZoneInfo("UTC")
    now = datetime.datetime(2029, 4, 3, 0, 0, 0, tzinfo=tz)
    assert _seconds_until_next_local_midnight(now) == 86400.0


# -- _channels_with_participants -----------------------------------------


def test_channels_with_participants_excludes_empty_channels(db, alice):
    occupied = create_channel(db, "occupied", creator=alice)
    create_channel(db, "empty", creator=alice)
    hub = ChatHub()
    hub.join(occupied.name, ParticipantId(username="alice", session_key=1))

    result = _channels_with_participants(db, hub)

    assert [c.name for c in result] == ["occupied"]


def test_channels_with_participants_is_empty_when_no_one_is_anywhere(db, alice):
    create_channel(db, "lobby", creator=alice)
    hub = ChatHub()
    assert _channels_with_participants(db, hub) == []


# -- announce_new_day -----------------------------------------------------


def test_announce_new_day_broadcasts_only_to_occupied_channels(db, alice):
    occupied = create_channel(db, "occupied", creator=alice)
    create_channel(db, "empty", creator=alice)
    hub = ChatHub()
    participant = ParticipantId(username="alice", session_key=1)
    queue = hub.join(occupied.name, participant)

    asyncio.run(announce_new_day(db, hub, local_date=datetime.date(2029, 4, 3)))

    assert queue.qsize() == 1
    notice = queue.get_nowait()
    assert isinstance(notice, _TimestampedNotice)
    assert "April 3rd, 2029" in notice.text


def test_announce_new_day_records_to_scrollback(db, alice):
    channel = create_channel(db, "lobby", creator=alice)
    hub = ChatHub()
    hub.join(channel.name, ParticipantId(username="alice", session_key=1))

    asyncio.run(announce_new_day(db, hub, local_date=datetime.date(2029, 4, 3)))

    scrollback = get_scrollback(db, channel)
    assert len(scrollback) == 1
    assert scrollback[0].kind == "daybreak"
    assert "April 3rd, 2029" in scrollback[0].body


def test_announce_new_day_is_a_no_op_with_no_channels_at_all(db):
    hub = ChatHub()
    asyncio.run(announce_new_day(db, hub, local_date=datetime.date(2029, 4, 3)))  # must not raise


def test_announce_new_day_defaults_to_the_node_configured_timezone(db, alice):
    """No `local_date` override -- resolves via `get_node_timezone` and
    the real wall clock, same as every other display-facing feature in
    this codebase. Doesn't pin an exact date (that depends on whenever
    this test happens to run) -- just confirms the no-override branch
    exercises `get_node_timezone` and produces a real announcement
    rather than crashing or silently doing nothing."""
    set_display_timezone(db, "UTC")
    channel = create_channel(db, "lobby", creator=alice)
    hub = ChatHub()
    hub.join(channel.name, ParticipantId(username="alice", session_key=1))

    asyncio.run(announce_new_day(db, hub))

    scrollback = get_scrollback(db, channel)
    assert len(scrollback) == 1
    assert scrollback[0].kind == "daybreak"
    assert "Good morning, chatters!" in scrollback[0].body


# -- run_daybreak_announcer (sleep/now injected -- no real waiting) -------


def test_announcer_sleeps_until_midnight_then_announces(db, alice):
    """
    `now` is fixed (23:59 UTC, one minute to midnight) for every call --
    it doesn't need to advance, since only the *first* loop iteration's
    outcome is being observed here. What makes this test terminate
    deterministically is `fake_sleep`: it returns immediately the first
    time (letting the loop reach `announce_new_day` once), then parks
    forever on an un-set `asyncio.Event` every time after -- a real
    suspension point the scheduler can actually interleave around,
    unlike a `fake_sleep` that always returns immediately. Without
    that, `run_daybreak_announcer`'s `while True` loop never contains a
    genuine `await` suspension at all (an `asyncio.Queue.put()` that
    still has room doesn't suspend either), so it starves every other
    task in the event loop -- including this test's own polling
    coroutine and even `task.cancel()` itself, since cancellation only
    takes effect at the next suspension point -- discovered the hard
    way via a real hang, not reasoned out in advance.
    """
    channel = create_channel(db, "lobby", creator=alice)
    hub = ChatHub()
    queue = hub.join(channel.name, ParticipantId(username="alice", session_key=1))

    sleep_calls: list[float] = []
    parked = asyncio.Event()

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) > 1:
            await parked.wait()

    def fake_now() -> datetime.datetime:
        return datetime.datetime(2029, 4, 3, 23, 59, 0, tzinfo=datetime.timezone.utc)

    async def scenario():
        task = asyncio.create_task(
            run_daybreak_announcer(db, hub, now=fake_now, sleep=fake_sleep)
        )
        notice = await asyncio.wait_for(queue.get(), timeout=2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return notice

    notice = asyncio.run(scenario())

    assert sleep_calls[0] == 60.0
    assert "April 4th, 2029" in notice.text  # midnight has now passed into the 4th


def test_announcer_stops_cleanly_on_cancellation_while_sleeping(db):
    """Cancellation while sleeping (the common case -- a real day is a
    long wait) must propagate cleanly with no special handling needed
    in `run_daybreak_announcer` itself. `db` must be real (not a bare
    stand-in) -- `get_node_timezone(db)` runs at the top of *every*
    loop iteration, before the sleep, so the loop reaches real
    database access before it ever reaches the (hanging) sleep call;
    `hub` is never actually touched, since cancellation lands before
    `announce_new_day` would ever run."""

    async def scenario():
        never_returns = asyncio.get_event_loop().create_future()

        async def hanging_sleep(seconds: float) -> None:
            await never_returns

        task = asyncio.create_task(
            run_daybreak_announcer(
                db, ChatHub(),
                now=lambda: datetime.datetime(2029, 4, 3, tzinfo=datetime.timezone.utc),
                sleep=hanging_sleep,
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


# -- record_message validation --------------------------------------------


def test_record_message_requires_body_for_daybreak_kind(db, alice):
    channel = create_channel(db, "lobby", creator=alice)
    with pytest.raises(ValueError, match="body is required"):
        record_message(db, channel, kind="daybreak", author_label="system")


# -- scrollback replay rendering -------------------------------------------


def test_scrollback_replay_renders_daybreak_with_no_author(db, alice):
    channel = create_channel(db, "lobby", creator=alice)
    recorded = record_message(
        db, channel, kind="daybreak", author_label="system",
        body="Good morning, chatters! A new day has just begun.",
    )
    rendered = _render_scrollback_message(db, alice, recorded)
    assert "Good morning, chatters!" in rendered
    assert "system" not in rendered
    assert "<" not in rendered  # no "<author>" tag, unlike an ordinary message
