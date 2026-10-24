"""The Enter typed after a picker choice is part of the choice (issue #1546).

A two-digit choice completes on its second digit. Callers end it with Enter
anyway, and that Enter used to reach whatever read next: a door's first
question, or the next screen's prompt. These tests drive the real picker over
real Telnet, SSH and web sessions, then read as a door (`DoorTerminal`) or as
the next screen would.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.doors.runtime import DoorTerminal
from netbbs.net import char_input
from netbbs.net.picker import pick_item
from netbbs.net.ssh import SSHSession
from netbbs.net.telnet import TelnetSession
from netbbs.net.web import WebSession

_ITEMS = [f"Door {n}" for n in range(1, 11)]


class _Writer:
    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def is_closing(self) -> bool:
        return False

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class _Stdout(_Writer):
    pass


class _Process:
    term_size = (80, 24, 0, 0)
    env: dict = {}

    def __init__(self):
        self.stdout = _Stdout()
        self.stdin = asyncio.StreamReader()

    def get_extra_info(self, name, default=None):
        return default


class _Socket:
    closed = False

    def __init__(self):
        self.sent = []

    async def send_json(self, value):
        self.sent.append(value)

    async def close(self, **kwargs):
        self.closed = True


class _Clock:
    """The guards' clock, and only theirs: asyncio keeps the real one."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(char_input, "time", fake)
    return fake


class _Line:
    """A byte-stream session with a way to type into it."""

    def __init__(self, transport: str):
        if transport == "telnet":
            self._reader = asyncio.StreamReader()
            self.session = TelnetSession(self._reader, _Writer())
        else:
            process = _Process()
            self._reader = process.stdin
            self.session = SSHSession(process)

    def type(self, data: bytes) -> None:
        self._reader.feed_data(data)


async def _pick(session) -> str:
    return await pick_item(session, _ITEMS, name_of=str, stable_id_of=_ITEMS.index, title="Doors",
                           empty_message="none")


async def _door_reads(session, count: int = 1, timeout: float = 1.0) -> bytes:
    terminal = DoorTerminal(session, "utf-8")
    return bytes([await asyncio.wait_for(terminal.read_byte(), timeout) for _ in range(count)])


@pytest.fixture(params=["telnet", "ssh"])
def transport(request):
    return request.param


@pytest.mark.parametrize("ending", [b"\r\n", b"\r", b"\r\x00"])
def test_the_enter_after_a_two_digit_choice_never_reaches_the_door(transport, ending):
    """`08`, Enter, `2`: the door gets `2` and nothing before it."""

    async def scenario():
        line = _Line(transport)
        line.type(b"08" + ending + b"2")
        picked = await _pick(line.session)
        return picked, await _door_reads(line.session)

    assert asyncio.run(scenario()) == ("Door 8", b"2")


@pytest.mark.parametrize("ending", [b"\r\n", b"\r", b"\r\x00"])
def test_the_door_starts_with_nothing_waiting(transport, ending):
    async def scenario():
        line = _Line(transport)
        line.type(b"08" + ending)
        await _pick(line.session)
        with pytest.raises(asyncio.TimeoutError):
            await _door_reads(line.session, timeout=0.2)

    asyncio.run(scenario())


def test_a_choice_without_enter_leaves_the_next_key_alone(transport):
    async def scenario():
        line = _Line(transport)
        line.type(b"08q\r")
        await _pick(line.session)
        return await _door_reads(line.session, 2)

    assert asyncio.run(scenario()) == b"q\r"


def test_a_slow_habitual_enter_is_still_dropped(transport, clock):
    async def scenario():
        line = _Line(transport)
        line.type(b"08")
        await _pick(line.session)
        clock.now += 0.4
        line.type(b"\r\n2")
        return await _door_reads(line.session)

    assert asyncio.run(scenario()) == b"2"


def test_an_enter_after_the_window_reaches_the_door(transport, clock):
    async def scenario():
        line = _Line(transport)
        line.type(b"08")
        await _pick(line.session)
        clock.now += char_input.ENTER_GUARD_SECONDS + 0.5
        line.type(b"\r")
        return await _door_reads(line.session)

    assert asyncio.run(scenario()) == b"\r"


def test_only_one_enter_is_dropped(transport):
    async def scenario():
        line = _Line(transport)
        line.type(b"08\r\n\r\n")
        await _pick(line.session)
        return await _door_reads(line.session, 2)

    assert asyncio.run(scenario()) == b"\r\n"


def test_one_digit_and_enter_still_picks_and_a_second_enter_still_counts(transport):
    """#840 is unchanged: `8` Enter picks row 8; only the rest of that same
    Enter is dropped, so a second Enter reaches the door."""

    async def scenario():
        line = _Line(transport)
        line.type(b"8\r\n\r")
        picked = await _pick(line.session)
        return picked, await _door_reads(line.session)

    assert asyncio.run(scenario()) == ("Door 8", b"\r")


def test_a_pushed_back_byte_reaches_the_door(transport):
    """A byte some peek pushed back used to sit there through the whole
    door and surface at the BBS prompt after it."""

    async def scenario():
        line = _Line(transport)
        char_input._push_back(line.session, ord("y"))
        line.type(b"z")
        return await _door_reads(line.session, 2)

    assert asyncio.run(scenario()) == b"yz"


def test_the_next_screen_does_not_see_the_enter(transport):
    """A board picker followed by Enter: the next prompt reads what the
    caller typed for it, not an empty line."""

    async def scenario():
        line = _Line(transport)
        line.type(b"08\r\nhello\r\n")
        await _pick(line.session)
        return await asyncio.wait_for(line.session.read_line(), 1)

    assert asyncio.run(scenario()) == "hello"


def test_the_next_screens_paced_art_is_not_ended_by_it(transport):
    async def scenario():
        line = _Line(transport)
        line.type(b"08\r\n")
        await _pick(line.session)
        return await line.session.take_waiting_key(0.1)

    assert asyncio.run(scenario()) is False


# -- web --------------------------------------------------------------------


def test_web_the_enter_after_a_two_digit_choice_never_reaches_the_door():
    async def scenario():
        ws = _Socket()
        session = WebSession(ws)
        try:
            await session._handle_event({"type": "key", "data": "08"})
            picked = await _pick(session)
            await session.enter_door_mode()
            stream = ws.sent[-1]["stream"]
            await session._handle_event({"type": "door_key", "stream": stream, "data": "\r2"})
            return picked, await _door_reads(session)
        finally:
            await session.close()

    assert asyncio.run(scenario()) == ("Door 8", b"2")


def test_web_the_next_screen_does_not_see_the_enter():
    async def scenario():
        ws = _Socket()
        session = WebSession(ws)
        try:
            await session._handle_event({"type": "key", "data": "08\rx"})
            await _pick(session)
            return await asyncio.wait_for(session.read_editor_key(), 1)
        finally:
            await session.close()

    key = asyncio.run(scenario())
    assert (key.kind, key.char) == (char_input.EditorKeyKind.CHAR, "x")


def test_web_an_arrow_ends_the_guard():
    async def scenario():
        ws = _Socket()
        session = WebSession(ws)
        try:
            await session._handle_event({"type": "key", "data": "08\x1b[A\r"})
            await _pick(session)
            first = await asyncio.wait_for(session.read_editor_key(), 1)
            second = await asyncio.wait_for(session.read_editor_key(), 1)
            return first.kind, second.kind
        finally:
            await session.close()

    assert asyncio.run(scenario()) == (char_input.EditorKeyKind.UP, char_input.EditorKeyKind.ENTER)


# -- the guard itself --------------------------------------------------------


class _Bytes:
    def __init__(self, data: bytes):
        self._data = list(data)

    async def read_byte(self):
        return self._data.pop(0) if self._data else None

    async def read_byte_with_timeout(self, timeout):
        return self._data.pop(0) if self._data else None


def test_any_other_key_spends_the_guard():
    source = _Bytes(b"x\r")
    char_input.arm_enter_guard(source)

    async def scenario():
        return bytes([await char_input.read_guarded_byte(source) for _ in range(2)])

    assert asyncio.run(scenario()) == b"x\r"


def test_a_word_guards_enter_takes_its_lf_without_a_peek():
    """Doors and the web never peek, so the LF behind a dropped CR is
    dropped when it is read."""
    source = _Bytes(b"es\r\nq")
    char_input.arm_word_guard(source)
    assert asyncio.run(char_input.read_guarded_byte(source)) == ord("q")
