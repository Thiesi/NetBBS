"""
Tests for the pinned chat input row (design doc round 79): a fixed row
just above the status line, kept out of ordinary scrolling the same way
the status line itself is (`netbbs.rendering.set_scroll_region`), safe
against an incoming message arriving mid-keystroke.

Most of this file drives the real `_chat_loop` through the same
whole-line-scripted `FakeSession` other chat tests use (`tests.
test_chat_flow_moderation.FakeSession`) -- adequate for confirming the
row is painted, redrawn after a command, etc. The one test that
actually matters most for this round (`test_...`) needs something more
capable: `_LiveTypingSession`, a real byte-fed session driving
`netbbs.net.char_input.read_line`'s genuine per-keystroke loop, so
`live_buffer`/`lock` are genuinely exercised rather than bypassed --
`FakeSession`'s whole-line scripting never touches either.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.net import char_input, chat_flow
from netbbs.net.char_input import InputHistory
from netbbs.net.session import Session
from netbbs.rendering import move_cursor, set_scroll_region
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
def hub():
    return ChatHub()


@pytest.fixture
def presence():
    return PresenceRegistry()


@pytest.fixture
def mailbox():
    return MessageMailbox()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


@pytest.fixture
def channel(db, alice):
    return create_channel(db, "lobby", creator=alice)


def _written_text(session) -> str:
    return "".join(session.written)


async def _run(lane, hub, presence, mailbox, channel, user, lines):
    session = FakeSession(lines)
    history = InputHistory()
    action = await asyncio.wait_for(
        chat_flow._chat_loop(session, lane, hub, presence, mailbox, history, channel, user),
        timeout=2,
    )
    return session, action


# -- initial paint / redraw-after-command, via the whole-line FakeSession --


def test_input_row_is_painted_on_entry_with_the_prompt_marker(lane, hub, presence, mailbox, channel, alice):
    session, _ = asyncio.run(_run(lane, hub, presence, mailbox, channel, alice, ["/quit"]))
    text = _written_text(session)
    # Row 24 on the default 80x24 terminal (row 22 is the last scrolling
    # row, row 23 is the status row, row 24 is the pinned input row).
    assert "\x1b[24;1H\x1b[2K> " in text


def test_input_row_is_redrawn_empty_after_a_command(lane, hub, presence, mailbox, channel, alice):
    session, _ = asyncio.run(_run(lane, hub, presence, mailbox, channel, alice, ["/away brb", "/quit"]))
    text = _written_text(session)
    # At least two distinct "row 24, cleared, prompt-only" repaints --
    # one on entry, at least one more after /away's own dispatch.
    assert text.count("\x1b[24;1H\x1b[2K> ") >= 2


def test_input_row_repaint_reflects_a_long_line_via_truncation(db, hub, presence, mailbox, channel, alice):
    """A pure-function check on `_repaint_input_row`'s own truncation
    behavior (design doc round 79) -- doesn't need a real `_chat_loop`
    at all, just a `LiveInputBuffer` with more text than an narrow
    terminal can show."""

    class _NarrowSession:
        def __init__(self):
            self.written = []
            self.terminal_width = 10
            self.terminal_height = 24

        async def write(self, text: str) -> None:
            self.written.append(text)

    live_buffer = char_input.LiveInputBuffer()
    live_buffer.update(list("this is a very long in-progress message"), 10)

    session = _NarrowSession()
    asyncio.run(chat_flow._repaint_input_row(session, live_buffer, session.terminal_height))
    text = "".join(session.written)
    # Truncated to fit -- never the full, untruncated string.
    assert "this is a very long in-progress message" not in text
    assert "..." in text


def test_pinned_ui_min_height_requires_three_rows(lane, hub, presence, mailbox, channel, alice):
    """Design doc round 79: one more than the status-line-only minimum
    (2, round 75) -- at least one row of actual scrolling content, plus
    both reserved rows."""
    session = FakeSession(["/quit"])
    session.terminal_height = 2  # one below _PINNED_UI_MIN_HEIGHT (3)
    history = InputHistory()
    asyncio.run(
        asyncio.wait_for(
            chat_flow._chat_loop(session, lane, hub, presence, mailbox, history, channel, alice), timeout=2
        )
    )
    text = _written_text(session)
    assert "\x1b[r" not in text  # scroll region never set, so never reset
    assert "> " not in text  # no pinned input row painted either


# -- the real, byte-fed session: live_buffer/lock genuinely exercised -----


class _LiveTypingSession(Session):
    """
    A `Session` backed by a live, externally-feedable byte queue --
    unlike `FakeSession`'s whole-line scripting (which returns an
    entire logical line instantly, bypassing `netbbs.net.char_input`
    entirely), this drives the real character-by-character `read_line`
    loop, so `live_buffer`/`lock` (design doc round 79) are genuinely
    exercised rather than trivially unreachable. `feed()` pushes bytes
    from the test at whatever pace the test wants; `read_byte()` blocks
    until more arrive, the same way a real transport waits on a slow
    typist -- this is what lets a test pause mid-line and interject a
    broadcast message before resuming.
    """

    def __init__(self):
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"

    def feed(self, text: str) -> None:
        for byte in text.encode("utf-8"):
            self._queue.put_nowait(byte)

    def feed_enter(self) -> None:
        # A full CRLF, not a lone CR: `char_input._consume_optional_lf_
        # or_nul` peeks for a following LF with a real (50ms)
        # `_FOLLOWUP_BYTE_TIMEOUT`-bounded wait when none is queued yet
        # -- sending the LF immediately lets that peek resolve as soon
        # as it's read instead of always paying the full timeout,
        # which otherwise races against any test that itself sleeps
        # ~50ms right after calling this to observe post-Enter state.
        self._queue.put_nowait(0x0D)
        self._queue.put_nowait(0x0A)

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\r\n")

    async def read_line(
        self, echo: bool = True, history=None, completer=None, *,
        live_buffer=None, lock=None, list_candidates=None,
    ) -> str:
        return await char_input.read_line(
            self, self.write, echo, history, completer,
            live_buffer=live_buffer, lock=lock, list_candidates=list_candidates,
        )

    async def read_key(self, echo: bool = True) -> str:
        raise NotImplementedError

    async def read_editor_key(self):
        raise NotImplementedError

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        return await self._queue.get()

    async def read_byte_with_timeout(self, timeout: float) -> int | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError

    @property
    def output(self) -> str:
        return "".join(self.written)


def test_in_progress_typing_survives_an_incoming_message(lane, hub, presence, mailbox, channel, alice, bob):
    """
    The actual bug this whole round exists to fix: an incoming chat
    message arriving while alice is mid-keystroke must not corrupt or
    lose what she's already typed -- it should scroll in above the
    pinned input row, which is then redrawn showing her still-intact
    in-progress text (design doc round 79).
    """

    async def scenario():
        alice_session = _LiveTypingSession()
        alice_task = asyncio.create_task(
            chat_flow._chat_loop(
                alice_session, lane, hub, presence, mailbox, InputHistory(), channel, alice
            )
        )
        await asyncio.sleep(0.05)  # let alice join and reach her first read_line()

        alice_session.feed("hel")  # mid-word, not yet submitted
        await asyncio.sleep(0.05)  # let the per-keystroke loop consume it

        # bob sends a complete message and quits -- broadcast to alice.
        bob_session = FakeSession(["hello there", "/quit"])
        await asyncio.wait_for(
            chat_flow._chat_loop(
                bob_session, lane, hub, presence, mailbox, InputHistory(), channel, bob
            ),
            timeout=2,
        )
        await asyncio.sleep(0.05)  # let alice's receive_loop process the broadcast

        # alice resumes typing and submits.
        alice_session.feed("lo")
        alice_session.feed_enter()
        await asyncio.sleep(0.05)

        alice_task.cancel()
        try:
            await alice_task
        except asyncio.CancelledError:
            pass
        return alice_session

    alice_session = asyncio.run(scenario())
    text = alice_session.output

    # bob's message actually arrived.
    assert "hello there" in text
    # The input row was redrawn showing alice's in-progress text intact
    # -- not silently dropped or corrupted by the interruption.
    assert "> hel" in text
    # The interruption's redraw happens strictly *after* alice's own
    # first three keystrokes were echoed, and *before* she resumes --
    # confirms this isn't a coincidental substring match from some
    # unrelated point in the transcript.
    hel_echo_index = text.index("l", text.index("he"))  # end of the raw "hel" echo
    broadcast_index = text.index("hello there")
    assert hel_echo_index < broadcast_index

    # And -- the actual point of preserving the buffer, not just the
    # screen -- the full submitted line ("hel" + "lo" == "hello") came
    # through correctly on the far side of the interruption, proving the
    # underlying buffer (not just its on-screen echo) survived intact.
    # Anchored to alice's own self-colored echo of her own message
    # (`<alice>` immediately followed by an SGR reset, then " hello")
    # specifically, not a bare "hello" substring, which bob's own
    # distinct "hello there" would also satisfy.
    assert "<alice>\x1b[0m hello" in text


def test_tab_completion_candidate_list_does_not_land_on_the_status_row(
    lane, hub, presence, mailbox, channel, alice, bob
):
    """
    Before this fix, `apply_tab_completion`'s multi-candidate branch
    wrote a bare `"\\r\\n" + candidates + "\\r\\n"` with no idea the
    terminal's cursor sits on the pinned input row, outside the scroll
    region -- an unconstrained newline there lands on, and overwrites,
    the status row directly below instead of scrolling normally above
    it (this is also what produced garbled-looking candidate text:
    Thiesi's report of e.g. "Thiesi" showing up as "hiesi" was this same
    overwrite, not a completion-matching bug). Now the candidate list
    goes through the exact same "new line in the content region, then
    redraw the pinned input row" primitive every other pinned-row print
    already uses.
    """

    async def scenario():
        alice_session = _LiveTypingSession()
        alice_task = asyncio.create_task(
            chat_flow._chat_loop(
                alice_session, lane, hub, presence, mailbox, InputHistory(), channel, alice
            )
        )
        await asyncio.sleep(0.05)  # let alice join and reach her first read_line()

        alice_session.feed("/whois ")
        await asyncio.sleep(0.05)
        alice_session.feed("\t")  # multiple registered users -> candidate list
        await asyncio.sleep(0.05)

        alice_task.cancel()
        try:
            await alice_task
        except asyncio.CancelledError:
            pass
        return alice_session

    alice_session = asyncio.run(scenario())
    text = alice_session.output

    # Both registered usernames appear, correctly cased -- and complete,
    # not truncated as the pre-fix overwrite bug made them look.
    assert "alice" in text
    assert "bob" in text

    # The candidate list is printed via the same content-region primitive
    # every other pinned-row print uses (scroll region + jump to its
    # bottom row -- row 22 on the default 80x24 terminal, one above the
    # status row at 23 and the pinned input row at 24), not a bare,
    # region-unaware "\r\n" that would instead land wherever the cursor
    # already was (the input row, the terminal's true last row).
    scroll_bottom = alice_session.terminal_height - 2
    assert set_scroll_region(1, scroll_bottom) + move_cursor(scroll_bottom, 1) in text


# -- GitHub issue #45: Enter completion must be one atomic critical section -


def test_char_input_enter_completion_is_atomic_with_concurrent_lock_holder():
    """
    Reproduces the exact race the issue describes at the `char_input.
    read_line` level: a `write()` that blocks specifically on the final
    "\\r\\n" (standing in for a transport whose write doesn't land on the
    wire synchronously). While that write is pending, a concurrent
    holder of the *same* `lock` (standing in for `_repaint_input_row`
    racing in from `receive_loop`) must not be able to acquire it, and
    `live_buffer` must not yet be observable as reset -- both the
    terminal and the externally-visible state must change together, not
    in two separately-timed steps with a gap between them.
    """

    async def scenario():
        queue: asyncio.Queue[int] = asyncio.Queue()

        class _FeedableSource:
            async def read_byte(self) -> int | None:
                return await queue.get()

            async def read_byte_with_timeout(self, timeout: float) -> int | None:
                try:
                    return await asyncio.wait_for(queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    return None

        written: list[str] = []
        entered_final_write = asyncio.Event()
        release_final_write = asyncio.Event()

        async def write(text: str) -> None:
            if text == "\r\n":
                entered_final_write.set()
                await release_final_write.wait()
            written.append(text)

        lock = asyncio.Lock()
        live_buffer = char_input.LiveInputBuffer()

        read_task = asyncio.create_task(
            char_input.read_line(_FeedableSource(), write, live_buffer=live_buffer, lock=lock)
        )

        for byte in b"hi":
            queue.put_nowait(byte)
        await asyncio.sleep(0.02)  # let "hi" be consumed and echoed

        queue.put_nowait(0x0D)  # Enter
        await asyncio.wait_for(entered_final_write.wait(), timeout=2)

        # read_line is now blocked mid-write, *inside* the lock (per the
        # fix) -- a concurrent lock holder must not get in.
        outsider_ran = asyncio.Event()

        async def outsider() -> None:
            async with lock:
                outsider_ran.set()

        outsider_task = asyncio.create_task(outsider())
        await asyncio.sleep(0.05)
        assert not outsider_ran.is_set()
        # And the externally-visible buffer must not yet show "reset" --
        # the terminal write hasn't completed, so a concurrent redraw
        # reading this now would still match what's on screen.
        assert live_buffer.text == "hi"

        release_final_write.set()
        result = await asyncio.wait_for(read_task, timeout=2)
        await asyncio.wait_for(outsider_task, timeout=2)

        assert result == "hi"
        assert live_buffer.text == ""
        assert live_buffer.cursor == 0
        assert written[-1] == "\r\n"

    asyncio.run(scenario())


def test_web_session_enter_completion_is_atomic_with_concurrent_lock_holder():
    """Same property as the byte-oriented test above, for `WebSession.
    _read_line_editable`'s separate reimplementation (GitHub issue
    #45) -- the web transport is the one where this bug was hardest to
    mask, since its `write()` really does await a WebSocket send
    directly rather than a byte stream that might happen to buffer
    synchronously."""
    from netbbs.net.web import WebSession

    class _IdleReadLoopSession(WebSession):
        # The real `_read_loop` iterates a live websocket; this test
        # feeds `_char_queue` directly and has no real socket, so the
        # background reader is parked instead of touching `self._ws`.
        async def _read_loop(self) -> None:
            await asyncio.Event().wait()

    async def scenario():
        session = _IdleReadLoopSession(ws=object(), peer_address="203.0.113.5")

        written: list[str] = []
        entered_final_write = asyncio.Event()
        release_final_write = asyncio.Event()

        async def write(text: str) -> None:
            if text == "\r\n":
                entered_final_write.set()
                await release_final_write.wait()
            written.append(text)

        session.write = write  # type: ignore[method-assign]

        lock = asyncio.Lock()
        live_buffer = char_input.LiveInputBuffer()

        for ch in "hi":
            session._char_queue.put_nowait(ch)

        read_task = asyncio.create_task(
            session._read_line_editable(None, live_buffer=live_buffer, lock=lock)
        )
        await asyncio.sleep(0.02)

        session._char_queue.put_nowait("\r")
        await asyncio.wait_for(entered_final_write.wait(), timeout=2)

        outsider_ran = asyncio.Event()

        async def outsider() -> None:
            async with lock:
                outsider_ran.set()

        outsider_task = asyncio.create_task(outsider())
        await asyncio.sleep(0.05)
        assert not outsider_ran.is_set()
        assert live_buffer.text == "hi"

        release_final_write.set()
        result = await asyncio.wait_for(read_task, timeout=2)
        await asyncio.wait_for(outsider_task, timeout=2)

        session._reader_task.cancel()
        try:
            await session._reader_task
        except asyncio.CancelledError:
            pass

        assert result == "hi"
        assert live_buffer.text == ""
        assert live_buffer.cursor == 0
        assert written[-1] == "\r\n"

    asyncio.run(scenario())


# -- GitHub issue #46: pinned UI must track resize dynamically, not once --


def test_shrink_below_minimum_mid_session_then_submit_does_not_crash(lane, hub, presence, mailbox, channel, alice):
    """The core defect: `_chat_loop` used to decide `pinned_ui_enabled`
    once at entry and trust it for the whole session. Shrinking below
    `_PINNED_UI_MIN_HEIGHT` afterward made the next submitted line's own
    repaint attempt `set_scroll_region(1, height - 2)` with `height - 2
    <= 0`, raising `ValueError` and killing the session."""

    async def scenario():
        session = _LiveTypingSession()  # starts at 24 rows
        task = asyncio.create_task(
            chat_flow._chat_loop(session, lane, hub, presence, mailbox, InputHistory(), channel, alice)
        )
        await asyncio.sleep(0.05)  # join + initial pinned-UI paint

        session.terminal_height = 2  # below _PINNED_UI_MIN_HEIGHT (3)
        session.feed("hello")
        session.feed_enter()
        await asyncio.sleep(0.05)  # would have raised here, pre-fix

        # The session is still alive -- prove it with one more round-trip.
        session.feed("/quit")
        session.feed_enter()
        return await asyncio.wait_for(task, timeout=2)

    action = asyncio.run(scenario())
    assert isinstance(action, chat_flow._Quit)


def test_shrink_below_minimum_mid_session_then_receive_broadcast_does_not_crash(
    lane, hub, presence, mailbox, channel, alice, bob
):
    """Same defect, hit via `receive_loop`'s `deliver()` instead of
    `send_loop` -- an incoming broadcast while too-short must not crash
    either."""

    async def scenario():
        alice_session = _LiveTypingSession()
        alice_task = asyncio.create_task(
            chat_flow._chat_loop(
                alice_session, lane, hub, presence, mailbox, InputHistory(), channel, alice
            )
        )
        await asyncio.sleep(0.05)

        alice_session.terminal_height = 2  # below _PINNED_UI_MIN_HEIGHT

        bob_session = FakeSession(["hi there", "/quit"])
        await asyncio.wait_for(
            chat_flow._chat_loop(bob_session, lane, hub, presence, mailbox, InputHistory(), channel, bob),
            timeout=2,
        )
        await asyncio.sleep(0.05)  # let alice's receive_loop process the broadcast

        alice_session.feed("/quit")
        alice_session.feed_enter()
        action = await asyncio.wait_for(alice_task, timeout=2)
        return alice_session, action

    alice_session, action = asyncio.run(scenario())
    assert "hi there" in alice_session.output
    assert isinstance(action, chat_flow._Quit)


def test_grow_above_minimum_mid_session_reinitializes_pinned_rows(lane, hub, presence, mailbox, channel, alice):
    """The reverse transition, also broken before this fix (design doc
    round 79's pinned UI never rechecked height after entry at all): a
    session that enters chat too short to support the pinned UI, then
    grows past the threshold, must have the scroll region and both
    pinned rows (re-)initialized -- not remain permanently unpinned."""

    async def scenario():
        session = _LiveTypingSession()
        session.terminal_height = 2  # too short from the very start
        task = asyncio.create_task(
            chat_flow._chat_loop(session, lane, hub, presence, mailbox, InputHistory(), channel, alice)
        )
        await asyncio.sleep(0.05)

        session.terminal_height = 24  # grows past _PINNED_UI_MIN_HEIGHT
        session.feed("hello")
        session.feed_enter()
        await asyncio.sleep(0.05)

        session.feed("/quit")
        session.feed_enter()
        await asyncio.wait_for(task, timeout=2)
        return session

    session = asyncio.run(scenario())
    text = session.output
    # The pinned input row's prompt marker appears once the terminal
    # grew back above the threshold -- it never could have before.
    assert "> " in text
    # And the scroll region set at that point matches the *current*
    # (24-row) height, not left unset or computed from the stale
    # too-short one.
    assert "\x1b[1;22r" in text  # set_scroll_region(1, 24 - 2)


def test_repeated_threshold_crossings_track_the_current_height_each_time(
    lane, hub, presence, mailbox, channel, alice
):
    """Shrink, grow, shrink again within one session -- each transition
    must reflect the terminal's size *at that moment*, and exit cleanup
    must be driven by the last-known real state
    (`_PinnedUIState.active`), not the entry-time snapshot `send_loop`'s
    own local reassignment doesn't even propagate to (GitHub issue
    #46)."""

    async def scenario():
        session = _LiveTypingSession()  # starts tall (24 rows)
        task = asyncio.create_task(
            chat_flow._chat_loop(session, lane, hub, presence, mailbox, InputHistory(), channel, alice)
        )
        await asyncio.sleep(0.05)

        session.terminal_height = 2  # shrink: hand the screen back
        session.feed("one")
        session.feed_enter()
        await asyncio.sleep(0.05)

        session.terminal_height = 24  # grow back: re-pin at the new size
        session.feed("two")
        session.feed_enter()
        await asyncio.sleep(0.05)

        session.feed("/quit")  # ends "active" -- exit cleanup must run
        session.feed_enter()
        await asyncio.wait_for(task, timeout=2)
        return session

    session = asyncio.run(scenario())
    text = session.output
    # Growing back re-set the region at the (still) 24-row size.
    assert text.count("\x1b[1;22r") >= 2  # once at entry, once on regrowth
    # Exit cleanup actually ran (ended "active" -- must reset before
    # handing control to whatever screen comes after /quit).
    assert text.endswith("\x1b[r" + "\x1b[2J\x1b[H")
