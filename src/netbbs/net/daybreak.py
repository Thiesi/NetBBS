"""
Local per-node "new day" chat announcement (design doc).

Once per local midnight (the node's configured display timezone --
`netbbs.timeutil.get_node_timezone`), broadcasts a short system message
into every channel that currently has at least one live participant --
deliberately *not* into empty channels, so a node with dozens of
dormant channels doesn't spam scrollback nobody is even present to
read.

Deliberately, permanently local-only: NetBBS Link (Phase 3, not
started -- see CLAUDE.md/design doc §15) has no concept of "this
node's local midnight" that would mean anything coherent federated
across nodes sitting in different time zones, so this never crosses a
Link channel's node boundary. That question was raised and answered
explicitly in the design doc before any of this was built, not an
oversight discovered later.

Lives under `netbbs.net`, not `netbbs.chat`, specifically so it can
reuse `netbbs.net.chat_flow`'s existing `_TimestampedNotice` broadcast
envelope -- the same one join/leave/chat messages already use to reach
`_chat_loop`'s `receive_loop` -- without a circular import:
`netbbs.chat` is `chat_flow`'s own dependency, never the reverse.
"""

from __future__ import annotations

import asyncio
import datetime
from typing import Awaitable, Callable

from netbbs.chat import Channel, ChatHub, list_channels, record_message
from netbbs.net.chat_flow import _TimestampedNotice
from netbbs.rendering import MUTED_COLOR, colored, sanitize_text
from netbbs.storage.database import Database
from netbbs.timeutil import get_node_timezone

_ORDINAL_SUFFIXES = {1: "st", 2: "nd", 3: "rd"}


def _ordinal_suffix(day: int) -> str:
    # 11th/12th/13th are the standard English exception to the
    # last-digit rule (not "11st"/"12nd"/"13rd").
    if 11 <= day % 100 <= 13:
        return "th"
    return _ORDINAL_SUFFIXES.get(day % 10, "th")


def format_daybreak_message(local_date: datetime.date) -> str:
    """The announcement text for `local_date` -- e.g. "Good morning,
    chatters! A new day has just begun: where this node lives, it is
    now Monday, April 3rd, 2029." (Thiesi's own example, matched
    exactly)."""
    weekday_and_month = local_date.strftime("%A, %B")
    day = local_date.day
    return (
        "Good morning, chatters! A new day has just begun: where this "
        f"node lives, it is now {weekday_and_month} {day}{_ordinal_suffix(day)}, {local_date.year}."
    )


def _seconds_until_next_local_midnight(current_local: datetime.datetime) -> float:
    """
    `current_local` must already be timezone-aware, in the timezone to
    schedule against.

    Constructs the target midnight directly with that same `tzinfo`
    (rather than just adding a fixed 24-hour `timedelta`) so the
    *target* is the correct wall-clock date across a DST transition --
    `zoneinfo.ZoneInfo` resolves the correct UTC offset for that target
    date when the `datetime` is constructed, not whatever offset was in
    effect a fixed number of seconds ago.

    The elapsed-time calculation itself (GitHub issue #47) then
    converts both ends to UTC before subtracting, rather than
    subtracting the two aware datetimes directly. `next_midnight` and
    `current_local` carry the exact same `tzinfo` *object* (`ZoneInfo`
    instances compare identical when constructed from the same key),
    and CPython's `datetime.__sub__` special-cases that: when both
    operands share one `tzinfo` object, it assumes their UTC offsets
    are equal and subtracts naive wall-clock fields directly, skipping
    `utcoffset()` entirely. That assumption fails exactly on a DST
    transition day, silently returning a flat 24 hours for a day that
    is really 23 (spring-forward) or 25 (fall-back) hours long.
    Explicit `astimezone(UTC)` on both sides forces the real elapsed
    duration to be used instead.
    """
    next_day = current_local.date() + datetime.timedelta(days=1)
    next_midnight = datetime.datetime(
        next_day.year, next_day.month, next_day.day, tzinfo=current_local.tzinfo
    )
    return (
        next_midnight.astimezone(datetime.timezone.utc)
        - current_local.astimezone(datetime.timezone.utc)
    ).total_seconds()


def _channels_with_participants(db: Database, hub: ChatHub) -> list[Channel]:
    """Every channel currently hosting at least one live participant --
    `ChatHub` exposes no "every channel with someone in it" query
    directly, so this cross-references every existing channel
    (`list_channels`) against `hub.participant_count` per name, the
    same pattern `netbbs.net.chat_flow._channel_names_for_user` already
    uses for its own per-channel hub lookups."""
    return [channel for channel in list_channels(db) if hub.participant_count(channel.name) > 0]


async def announce_new_day(db: Database, hub: ChatHub, *, local_date: datetime.date | None = None) -> None:
    """
    Broadcast (and record to scrollback) one daybreak announcement to
    every currently-occupied channel.

    `local_date` is normally left to resolve itself from
    `get_node_timezone` -- overridable so a test can pass a fixed date
    without needing a real node-config round trip. `record_message`
    stores the raw announcement text (not pre-colored), matching every
    other kind's convention -- sanitizing/coloring happens at each
    render site independently (here for the live broadcast,
    `netbbs.net.chat_flow._render_scrollback_message`'s own `"daybreak"`
    branch for replay), not baked into the stored row.
    """
    if local_date is None:
        local_date = datetime.datetime.now(get_node_timezone(db)).date()
    text = format_daybreak_message(local_date)
    rendered = colored(sanitize_text(text), fg_color=MUTED_COLOR)
    for channel in _channels_with_participants(db, hub):
        recorded = record_message(db, channel, kind="daybreak", author_label="system", body=text)
        await hub.broadcast(channel.name, _TimestampedNotice(rendered, recorded.created_at))


async def run_daybreak_announcer(
    db: Database,
    hub: ChatHub,
    *,
    now: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(datetime.timezone.utc),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """
    Runs for the node's lifetime -- constructed in `netbbs.__main__.run`
    alongside `ChatHub` itself, cancelled in that function's existing
    `finally` block the same way every listener is already stopped
    there. Sleeps until the next local midnight, announces, repeats;
    cancellation while sleeping (the common case, since a real day is a
    long wait) propagates immediately and cleanly through `asyncio.
    sleep`, needing no explicit `except asyncio.CancelledError` here.

    `now`/`sleep` are injectable so a test can drive this without a
    real wait -- the same dependency-injection shape
    `netbbs.net.throttle.LoginThrottle`'s own `clock` parameter already
    uses, for the identical reason.

    The date `announce_new_day` announces is computed here, from the
    same `current_local` just used to schedule the sleep, and passed
    through explicitly (`local_date=...`) rather than left for
    `announce_new_day` to re-resolve via its own fresh `now()` call
    after waking -- in production the two would almost always agree
    anyway (the sleep just landed on that exact moment), but re-
    querying real wall-clock time a second time is both an unnecessary
    second source of truth and untestable with a fake `now` that
    doesn't actually advance.
    """
    while True:
        tz = get_node_timezone(db)
        current_local = now().astimezone(tz)
        await sleep(_seconds_until_next_local_midnight(current_local))
        next_local_date = current_local.date() + datetime.timedelta(days=1)
        await announce_new_day(db, hub, local_date=next_local_date)
