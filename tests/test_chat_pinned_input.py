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
from netbbs.storage.database import Database
from tests.test_chat_flow_moderation import FakeSession


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


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


async def _run(db, hub, presence, mailbox, channel, user, lines):
    session = FakeSession(lines)
    history = InputHistory()
    action = await asyncio.wait_for(
        chat_flow._chat_loop(session, db, hub, presence, mailbox, history, channel, user),
        timeout=2,
    )
    return session, action


# -- initial paint / redraw-after-command, via the whole-line FakeSession --


def test_input_row_is_painted_on_entry_with_the_prompt_marker(db, hub, presence, mailbox, channel, alice):
    session, _ = asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/quit"]))
    text = _written_text(session)
    # Row 23 on the default 80x24 terminal (row 22 is the last scrolling
    # row, row 24 is the status row -- design doc round 79).
    assert "\x1b[23;1H\x1b[2K> " in text


def test_input_row_is_redrawn_empty_after_a_command(db, hub, presence, mailbox, channel, alice):
    session, _ = asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/away brb", "/quit"]))
    text = _written_text(session)
    # At least two distinct "row 23, cleared, prompt-only" repaints --
    # one on entry, at least one more after /away's own dispatch.
    assert text.count("\x1b[23;1H\x1b[2K> ") >= 2


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


def test_pinned_ui_min_height_requires_three_rows(db, hub, presence, mailbox, channel, alice):
    """Design doc round 79: one more than the status-line-only minimum
    (2, round 75) -- at least one row of actual scrolling content, plus
    both reserved rows."""
    session = FakeSession(["/quit"])
    session.terminal_height = 2  # one below _PINNED_UI_MIN_HEIGHT (3)
    history = InputHistory()
    asyncio.run(
        asyncio.wait_for(
            chat_flow._chat_loop(session, db, hub, presence, mailbox, history, channel, alice), timeout=2
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
        self._queue.put_nowait(0x0D)

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\r\n")

    async def read_line(
        self, echo: bool = True, history=None, completer=None, *, live_buffer=None, lock=None
    ) -> str:
        return await char_input.read_line(
            self, self.write, echo, history, completer, live_buffer=live_buffer, lock=lock
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


def test_in_progress_typing_survives_an_incoming_message(db, hub, presence, mailbox, channel, alice, bob):
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
                alice_session, db, hub, presence, mailbox, InputHistory(), channel, alice
            )
        )
        await asyncio.sleep(0.05)  # let alice join and reach her first read_line()

        alice_session.feed("hel")  # mid-word, not yet submitted
        await asyncio.sleep(0.05)  # let the per-keystroke loop consume it

        # bob sends a complete message and quits -- broadcast to alice.
        bob_session = FakeSession(["hello there", "/quit"])
        await asyncio.wait_for(
            chat_flow._chat_loop(
                bob_session, db, hub, presence, mailbox, InputHistory(), channel, bob
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
