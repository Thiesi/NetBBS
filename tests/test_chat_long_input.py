"""A chat line wider than the terminal (issue #926).

Chat's input row is the terminal's last line, outside the scroll region.
Typed past the right edge, it soft-wrapped with no row to wrap onto, the
cursor landed in column 1 of the same row, and typing overwrote what was
there; an incoming message then redrew the head of the line and left the
cursor at the edge. Both chat prompts now scroll within the row, and the
pinned-row repaint draws the line editor's own window.

These tests replay the written bytes onto a small screen model, because
the claim is about what the caller sees: **nothing is ever printed past
the last column of the input row**, the row shows the part of the line
around the cursor, and the cursor is where the next keystroke acts.
"""

from __future__ import annotations

import asyncio
import re

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.chat.scrollback import get_scrollback
from netbbs.net import chat_flow
from netbbs.net.char_input import InputHistory
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_chat_flow_moderation import FakeSession
from tests.test_chat_pinned_input import _LiveTypingSession

import pytest

_WIDTH = 40
_HEIGHT = 24
_TOKEN = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])|\x1b([78])|(\r|\n)|([^\x1b\r\n])")


class _Screen:
    """Just enough of a VT100 to follow chat's writes: absolute and
    relative cursor moves, erase in line, save/restore cursor, CR/LF.
    Colors and scroll regions are ignored; printing past the last column
    is recorded rather than wrapped, since that is the bug."""

    def __init__(self, width: int, height: int):
        self.width, self.height = width, height
        self.rows = [[" "] * width for _ in range(height)]
        self.row = self.col = 0
        self.saved = (0, 0)
        self.overflow_on_input_row = 0

    def feed(self, text: str) -> None:
        for csi_args, csi_final, esc, control, char in _TOKEN.findall(text):
            if csi_final:
                self._csi(csi_args, csi_final)
            elif esc == "7":
                self.saved = (self.row, self.col)
            elif esc == "8":
                self.row, self.col = self.saved
            elif control == "\r":
                self.col = 0
            elif control == "\n":
                self.row = min(self.height - 1, self.row + 1)
            elif char:
                if self.col >= self.width:
                    if self.row == self.height - 1:
                        self.overflow_on_input_row += 1
                    continue
                self.rows[self.row][self.col] = char
                self.col += 1

    def _csi(self, args: str, final: str) -> None:
        numbers = [int(part) if part.isdigit() else None for part in args.lstrip("?").split(";")]
        count = numbers[0] or 1
        if final == "H":
            row = numbers[0] or 1
            col = (numbers[1] if len(numbers) > 1 else None) or 1
            self.row, self.col = row - 1, col - 1
        elif final == "C":
            self.col = min(self.width - 1, self.col + count)
        elif final == "D":
            self.col = max(0, self.col - count)
        elif final == "A":
            self.row = max(0, self.row - count)
        elif final == "K":
            mode = numbers[0] or 0
            if mode == 2:
                self.rows[self.row] = [" "] * self.width
            else:
                for index in range(self.col, self.width):
                    self.rows[self.row][index] = " "
        elif final == "J" and (numbers[0] or 0) == 2:
            self.rows = [[" "] * self.width for _ in range(self.height)]

    @property
    def input_row(self) -> str:
        return "".join(self.rows[self.height - 1]).rstrip()


def _screen_of(session: _LiveTypingSession) -> _Screen:
    screen = _Screen(session.terminal_width, session.terminal_height)
    screen.feed(session.output)
    return screen


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
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


@pytest.fixture
def channel(db, alice):
    return create_channel(db, "lobby", creator=alice)


_LINE = "".join(f"{n:02d}" for n in range(30))  # 60 distinct characters


def _narrow() -> _LiveTypingSession:
    session = _LiveTypingSession()
    session.terminal_width = _WIDTH
    session.terminal_height = _HEIGHT
    return session


def _repainted_after(session: _LiveTypingSession, text: str) -> bool:
    """Whether `text` has arrived and the input row has been redrawn since:
    the window's erase-to-end follows the prompt on every repaint."""
    output = session.output
    return text in output and output.rfind("\x1b[K") > output.rfind(text)


async def _until(condition, what: str, timeout: float = 5.0) -> None:
    """Poll `condition` with a generous bound rather than sleeping a fixed
    time, which a loaded machine (the suite under `-n auto`) outruns."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


def test_a_long_channel_line_scrolls_within_the_row_and_survives_a_message(db, lane, alice, bob, channel):
    hub, presence, mailbox = ChatHub(), PresenceRegistry(), MessageMailbox()

    async def scenario():
        session = _narrow()
        task = asyncio.create_task(
            chat_flow._chat_loop(session, lane, hub, presence, mailbox, InputHistory(), channel, alice)
        )
        session.feed(_LINE)
        await _until(lambda: _screen_of(session).input_row.endswith(_LINE[-10:]), "the typed line")
        typed = _screen_of(session)

        await asyncio.wait_for(
            chat_flow._chat_loop(
                FakeSession(["hello there", "/quit"]), lane, hub, presence, mailbox, InputHistory(), channel, bob,
            ),
            timeout=5,
        )
        await _until(lambda: _repainted_after(session, "has left the channel"), "the repaint after bob's lines")
        after_message = _screen_of(session)

        session.feed("\x1b[H")  # Home
        await _until(lambda: _LINE[:10] in _screen_of(session).input_row, "Home to scroll back")
        at_home = _screen_of(session)

        session.feed_enter()
        await _until(
            lambda: _LINE in [message.body for message in get_scrollback(db, channel)], "the line to be sent",
        )
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return session, typed, after_message, at_home

    session, typed, after_message, at_home = asyncio.run(scenario())

    for screen in (typed, after_message, at_home):
        assert screen.overflow_on_input_row == 0
    # The tail of the line, up to the cursor at its end, with a marker for
    # what scrolled off to the left -- and the caret right after it.
    assert typed.input_row.endswith(_LINE[-30:])
    assert "<" in typed.input_row
    assert typed.col == len(typed.input_row)
    # An incoming message redraws the same window, not the head of the line.
    assert "hello there" in session.output
    assert after_message.input_row == typed.input_row
    assert (after_message.row, after_message.col) == (_HEIGHT - 1, typed.col)
    # Home scrolls back to the start, the caret on the first character.
    assert _LINE[:30] in at_home.input_row
    assert at_home.col == chat_flow._INPUT_PROMPT_WIDTH + 1  # after the prompt and the " " marker


def test_a_long_direct_chat_line_scrolls_within_the_row(db, alice, bob):
    async def scenario():
        hub, presence = ChatHub(), PresenceRegistry()
        room_token = "long-line"
        room = f"{chat_flow._DM_CHANNEL_PREFIX}{room_token}"
        peer_id = chat_flow.ParticipantId(username=bob.username, session_key=99)
        peer_queue = hub.join(room, peer_id)
        session = _narrow()
        task = asyncio.create_task(chat_flow.run_direct_chat_loop(session, hub, presence, alice, bob, room_token))
        await _until(lambda: hub.participant_count(room) == 2, "alice to join the direct chat")
        session.feed(_LINE)
        await _until(lambda: _screen_of(session).input_row.endswith(_LINE[-10:]), "the typed line")
        typed = _screen_of(session)
        await hub.broadcast(
            room, chat_flow._render_direct_chat_message(bob.username, "incoming", self_message=False),
            exclude={peer_id},
        )
        await _until(lambda: _repainted_after(session, "incoming"), "the repaint after bob's message")
        after_message = _screen_of(session)
        session.feed_enter()
        sent = await asyncio.wait_for(peer_queue.get(), timeout=5)
        session.feed("/close")
        session.feed_enter()
        await asyncio.wait_for(task, timeout=5)
        return typed, after_message, sent

    typed, after_message, sent = asyncio.run(scenario())

    assert typed.overflow_on_input_row == 0 and after_message.overflow_on_input_row == 0
    assert typed.input_row.endswith(_LINE[-30:])
    assert after_message.input_row == typed.input_row
    assert (after_message.row, after_message.col) == (_HEIGHT - 1, typed.col)
    assert _LINE in sent


def test_tab_completion_in_a_long_line_stays_on_the_row(lane, alice, channel):
    # Completion looks at the text before the cursor, so a command typed at
    # the start of a long line completes there while the rest scrolls on.
    hub, presence, mailbox = ChatHub(), PresenceRegistry(), MessageMailbox()

    async def scenario():
        session = _narrow()
        task = asyncio.create_task(
            chat_flow._chat_loop(session, lane, hub, presence, mailbox, InputHistory(), channel, alice)
        )
        session.feed(_LINE + "\x1b[H" + "/wh\t")
        await _until(lambda: _repainted_after(session, "/whois"), "the repaint after the candidates")
        screen = _screen_of(session)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return screen

    screen = asyncio.run(scenario())

    assert screen.overflow_on_input_row == 0
    # "/wh" is both /who and /whois: extended to what they share, and both
    # listed above the row, which is then redrawn around the cursor.
    assert "/who  /whois" in "\n".join("".join(row) for row in screen.rows)
    body = screen.input_row[chat_flow._INPUT_PROMPT_WIDTH:]
    assert body.startswith(" /who" + _LINE[:10])
    assert body.endswith(">")  # the rest of the line, scrolled off to the right
    assert screen.col == chat_flow._INPUT_PROMPT_WIDTH + len(" /who")
