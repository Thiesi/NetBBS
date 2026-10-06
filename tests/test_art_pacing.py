"""Paced SysOp art (issue #929, step 6): `netbbs.net.art_pacing`.

No test here sleeps: the pacing loop takes an injected clock, and the stub
session's `take_waiting_key` advances it instead of waiting."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.net import art_pacing, char_input
from netbbs.net.art_pacing import (
    MAX_PACED_SECONDS,
    art_speed,
    pace,
    set_art_speed,
    will_pace,
    write_paced_art,
    write_paced_art_text,
)
from netbbs.net.session import Session
from netbbs.rendering.ansi_art import decode_banner_bytes, decode_banner_bytes_fitting, revisits_rows
from netbbs.rendering.charset import ASCII, CP437, UTF8

ESC = "\x1b"


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Stub:
    """Just what paced art uses of a session."""

    paces_art = True
    animations_enabled = True
    output_charset = UTF8
    physical_width = 80
    terminal_width = 80
    terminal_height = 24
    wraps_immediately = False

    def __init__(self, clock: _Clock | None = None, keys_at: tuple[int, ...] = ()) -> None:
        self.clock = clock or _Clock()
        self.writes: list[str] = []
        self.waits: list[float] = []
        self.keys_at = keys_at
        self.breaking_in_after: int | None = None

    async def write(self, text: str) -> None:
        self.writes.append(text)

    @property
    def in_break_in(self) -> bool:
        # A property, as on `Session` (Claude review on #1012: a method here
        # hid that the real one is not callable).
        return self.breaking_in_after is not None and len(self.writes) >= self.breaking_in_after

    async def take_waiting_key(self, timeout: float) -> bool:
        self.waits.append(timeout)
        if len(self.waits) in self.keys_at:
            return True
        self.clock.now += timeout
        return False


def _run(coro):
    return asyncio.run(coro)


def test_art_goes_out_in_chunks_at_the_line_speed() -> None:
    session = _Stub()
    text = "x" * 40
    _run(pace(session, text, speed=2400, write=session.write, clock=session.clock))
    # 2400 bps is 240 characters a second, sent 30 times a second: 8 each.
    assert session.writes == ["x" * 8] * 5
    assert "".join(session.writes) == text
    assert session.waits == pytest.approx([8 / 240] * 4)


def test_a_key_draws_the_rest_at_once() -> None:
    session = _Stub(keys_at=(2,))
    text = "y" * 80
    _run(pace(session, text, speed=2400, write=session.write, clock=session.clock))
    assert session.writes == ["y" * 8, "y" * 8, "y" * 64]
    assert len(session.waits) == 2


def test_a_draw_never_takes_longer_than_the_cap() -> None:
    session = _Stub()
    text = "z" * 4000  # 16.7 seconds at 2400 bps
    _run(pace(session, text, speed=2400, write=session.write, clock=session.clock))
    assert "".join(session.writes) == text
    assert session.clock.now <= MAX_PACED_SECONDS + 1e-9
    # The rest goes out in one piece once the cap is reached.
    assert len(session.writes[-1]) > 8


def test_an_escape_sequence_is_never_split() -> None:
    session = _Stub()
    sequence = f"{ESC}[1;33;44m"
    text = ("ab" + sequence) * 10
    _run(pace(session, text, speed=2400, write=session.write, clock=session.clock))
    assert "".join(session.writes) == text
    for part in session.writes:
        assert part.count(ESC) == part.count(sequence)


def test_a_break_in_draws_the_rest_at_once() -> None:
    session = _Stub()
    session.breaking_in_after = 2
    text = "w" * 80
    _run(pace(session, text, speed=2400, write=session.write, clock=session.clock))
    assert session.writes == ["w" * 8, "w" * 8, "w" * 64]


@pytest.mark.parametrize(
    ("change", "speed"),
    [
        ({}, 0),
        ({"paces_art": False}, 9600),
        ({"animations_enabled": False}, 9600),
        ({"output_charset": ASCII}, 9600),
    ],
)
def test_art_is_drawn_at_once_when_it_must_not_be_paced(change: dict, speed: int) -> None:
    session = _Stub()
    for name, value in change.items():
        setattr(session, name, value)
    assert not will_pace(session, speed, "welcome")
    _run(write_paced_art(session, "a" * 100, speed=speed, once="welcome"))
    assert len(session.writes) == 1
    assert session.waits == []


def test_art_plays_once_per_connection() -> None:
    session = _Stub()
    assert will_pace(session, 9600, "main_menu")
    _run(write_paced_art(session, "b" * 200, speed=9600, once="main_menu"))
    assert len(session.writes) > 1
    session.writes.clear()
    assert not will_pace(session, 9600, "main_menu")
    _run(write_paced_art(session, "b" * 200, speed=9600, once="main_menu"))
    assert len(session.writes) == 1
    # Another art still plays.
    assert will_pace(session, 9600, "welcome")


def test_no_pacing_during_a_break_in() -> None:
    session = _Stub()
    session.breaking_in_after = 0
    assert not will_pace(session, 9600, "welcome")


def test_the_speed_setting_accepts_only_the_offered_speeds(tmp_path) -> None:
    from netbbs.storage.database import Database

    db = Database(tmp_path / "node.db")
    try:
        assert art_speed(db, "welcome") == 0
        set_art_speed(db, "welcome", 9600)
        assert art_speed(db, "welcome") == 9600
        assert art_speed(db, "main_menu") == 0
        with pytest.raises(ValueError):
            set_art_speed(db, "welcome", 1200)
    finally:
        db.close()


class _Bytes:
    """A `char_input.ByteSource` holding bytes already typed."""

    def __init__(self, data: bytes) -> None:
        self.data = list(data)

    async def read_byte(self) -> int | None:
        return self.data.pop(0)

    async def read_byte_with_timeout(self, timeout: float) -> int | None:
        return self.data.pop(0) if self.data else None


def test_the_key_that_ends_an_animation_is_swallowed_whole() -> None:
    source = _Bytes(b"\x1b[A\r\n")
    assert _run(char_input.take_waiting_key(source, 0.01))
    assert source.data == []


def test_no_key_means_no_skip() -> None:
    assert not _run(char_input.take_waiting_key(_Bytes(b""), 0.01))


def test_an_ansimation_keeps_its_row_ends() -> None:
    frame = b"\x1b[1;1HHELLO   \r\n\x1b[1;1HHI     \r\n\r\n"
    assert revisits_rows(frame.decode())
    assert decode_banner_bytes(frame) == frame.decode()


def test_an_ansimation_that_fits_keeps_its_row_ends_too() -> None:
    frame = b"\x1b[1;1HHELLO   \r\n\x1b[1;1HHI     \r\n"
    assert decode_banner_bytes_fitting(frame, 80) == frame.decode()


def test_still_art_is_still_trimmed() -> None:
    still = b"HELLO     \r\n\r\n"
    assert not revisits_rows(still.decode())
    assert decode_banner_bytes(still) == "HELLO"


def test_the_art_kinds_have_their_own_speeds() -> None:
    assert art_pacing.WELCOME_ART != art_pacing.MAIN_MENU_ART


def test_paced_art_is_prepared_once_so_no_chunk_loses_its_colours() -> None:
    # iCE colours (blink + background) become a bright background, and a
    # CP437 terminal gets CTerm's bright-background mode around the whole art
    # once -- not around every chunk.
    session = _Stub()
    session.output_charset = CP437
    art = f"{ESC}[5;44m" + "x" * 100 + f"{ESC}[0m"
    _run(write_paced_art_text(session, art, speed=2400, once="main_menu"))
    sent = "".join(session.writes)
    assert len(session.writes) > 1
    assert sent.startswith(f"{ESC}[?33h{ESC}[?35h") and "?33l" not in sent
    assert sent.count(f"{ESC}[?33h") == 1


class _RealSession(Session):
    """A real `Session`: its `in_break_in` is the real property, and art goes
    through the real write path."""

    write = Session.write
    paces_art = True

    def __init__(self) -> None:
        self.output_charset = UTF8
        self.terminal_width = 80
        self.terminal_wraps_immediately = False
        self.sent: list[str] = []
        self.keys = 0

    async def _send_text(self, text: str) -> None:
        self.sent.append(text)

    async def take_waiting_key(self, timeout: float) -> bool:
        self.keys += 1
        return self.keys >= 2

    async def read_line(self, *args, **kwargs) -> str:
        raise AssertionError("unused")

    async def read_key(self, *args, **kwargs) -> str:
        raise AssertionError("unused")

    async def read_editor_key(self):
        raise AssertionError("unused")

    async def close(self) -> None:
        pass


def test_a_real_session_plays_paced_art():
    session = _RealSession()
    assert will_pace(session, 9600, "welcome")
    _run(write_paced_art(session, "q" * 500, speed=9600, once="welcome"))
    assert "".join(session.sent).count("q") == 500
    assert len(session.sent) == 3  # two chunks, then the rest at the key


def test_a_real_session_in_a_break_in_is_not_paced():
    session = _RealSession()
    session._break_in_input = asyncio.Queue()
    assert not will_pace(session, 9600, "welcome")


# -- Preview (issue #1083 finding 9) -----------------------------------------


def test_preview_plays_at_the_set_speed_every_time() -> None:
    """A SysOp previewing art sees it at the speed callers will, as often
    as they preview: the once-per-session rule is for callers."""
    from netbbs.net.art_pacing import write_preview_art

    session = _Stub()
    _run(write_preview_art(session, "x" * 100, speed=2400))
    first = len(session.writes)
    _run(write_preview_art(session, "x" * 100, speed=2400))
    assert first > 1 and len(session.writes) - first > 1


def test_preview_without_a_speed_draws_at_once() -> None:
    from netbbs.net.art_pacing import write_preview_art

    session = _Stub()
    _run(write_preview_art(session, "x" * 100, speed=0))
    assert len(session.writes) == 1


def test_preview_does_not_use_up_a_callers_once_per_session_play() -> None:
    from netbbs.net.art_pacing import WELCOME_ART, write_preview_art

    session = _Stub()
    _run(write_preview_art(session, "x" * 100, speed=2400))
    assert will_pace(session, 2400, WELCOME_ART)
