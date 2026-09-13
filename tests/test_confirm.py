"""Tests for the shared single-key yes/no confirmation primitive."""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.confirm import prompt_yes_no, prompt_yes_no_or_keep
from netbbs.net.session import Session, SessionClosedError


class FakeSession(Session):
    def __init__(self, keys: list[EditorKey | BaseException]):
        self._keys = list(keys)
        self.written: list[str] = []
        self.terminal_width = 80
        self.node_display_name = "NetBBS"
        self.terminal_height = 24
        self.peer_address = None

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
        raise AssertionError("single-key confirmation must not call read_line")

    async def read_key(self, echo: bool = True) -> str:
        raise NotImplementedError

    async def read_editor_key(self) -> EditorKey:
        item = self._keys.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _visible(session: FakeSession) -> str:
    """Strips the SGR highlight `_highlighted` now wraps the default
    Y/N letter in, so a hint-text assertion can match the plain
    letters/brackets without hardcoding the exact escape sequence."""
    return _ANSI_RE.sub("", _written_text(session))


def _char(value: str) -> EditorKey:
    return EditorKey(EditorKeyKind.CHAR, char=value)


_ENTER = EditorKey(EditorKeyKind.ENTER)


def test_bare_enter_selects_the_default_true():
    session = FakeSession([_ENTER])
    result = asyncio.run(prompt_yes_no(session, "Confirm?", default=True))
    assert result is True


def test_bare_enter_selects_the_default_false():
    session = FakeSession([_ENTER])
    result = asyncio.run(prompt_yes_no(session, "Confirm?", default=False))
    assert result is False


def test_explicit_y_is_true_regardless_of_default():
    session = FakeSession([_char("y")])
    assert asyncio.run(prompt_yes_no(session, "Confirm?", default=False)) is True


def test_explicit_n_is_false_regardless_of_default():
    session = FakeSession([_char("n")])
    assert asyncio.run(prompt_yes_no(session, "Confirm?", default=True)) is False


def test_case_insensitive():
    assert asyncio.run(prompt_yes_no(FakeSession([_char("Y")]), "Confirm?", default=False)) is True
    assert asyncio.run(prompt_yes_no(FakeSession([_char("N")]), "Confirm?", default=True)) is False


def test_invalid_keys_bell_and_retry_without_selecting_default():
    session = FakeSession([_char("x"), EditorKey(EditorKeyKind.LEFT), _char("n")])
    result = asyncio.run(prompt_yes_no(session, "Confirm?", default=True))
    assert result is False
    assert _written_text(session).count("\a") == 2


def test_hint_reflects_the_default():
    session = FakeSession([_ENTER])
    asyncio.run(prompt_yes_no(session, "Confirm?", default=True))
    assert "[Y/n]" in _visible(session)

    session2 = FakeSession([_ENTER])
    asyncio.run(prompt_yes_no(session2, "Confirm?", default=False))
    assert "[y/N]" in _visible(session2)


def test_hint_highlights_the_default_letter():
    # Dogfood request: the letter Enter would actually choose is
    # visually highlighted -- the *other* letter stays plain either way.
    session = FakeSession([_ENTER])
    asyncio.run(prompt_yes_no(session, "Confirm?", default=True))
    text = _written_text(session)
    assert "\x1b[38;5;46mY\x1b[0m" in text
    assert "\x1b[38;5;46mn\x1b[0m" not in text

    session2 = FakeSession([_ENTER])
    asyncio.run(prompt_yes_no(session2, "Confirm?", default=False))
    text2 = _written_text(session2)
    assert "\x1b[38;5;46mN\x1b[0m" in text2
    assert "\x1b[38;5;46my\x1b[0m" not in text2


# -- prompt_yes_no_or_keep -----------------------------------------------------


def test_bare_enter_keeps_current_true():
    session = FakeSession([_ENTER])
    assert asyncio.run(prompt_yes_no_or_keep(session, "Pinned?", current=True)) is True


def test_bare_enter_keeps_current_false():
    session = FakeSession([_ENTER])
    assert asyncio.run(prompt_yes_no_or_keep(session, "Pinned?", current=False)) is False


def test_explicit_y_overrides_current_false():
    session = FakeSession([_char("y")])
    assert asyncio.run(prompt_yes_no_or_keep(session, "Pinned?", current=False)) is True


def test_explicit_n_overrides_current_true():
    session = FakeSession([_char("n")])
    assert asyncio.run(prompt_yes_no_or_keep(session, "Pinned?", current=True)) is False


def test_invalid_key_cannot_keep_current():
    session = FakeSession([_char("x"), _char("n")])
    assert asyncio.run(prompt_yes_no_or_keep(session, "Pinned?", current=True)) is False
    assert "\a" in _written_text(session)


def test_keep_hint_shows_only_the_current_value():
    session = FakeSession([_ENTER])
    asyncio.run(prompt_yes_no_or_keep(session, "Pinned?", current=True))
    assert "[y]" in _visible(session)

    session2 = FakeSession([_ENTER])
    asyncio.run(prompt_yes_no_or_keep(session2, "Pinned?", current=False))
    assert "[N]" in _visible(session2)


def test_accepted_choice_is_echoed_and_ends_the_input_row():
    session = FakeSession([_char("Y")])
    asyncio.run(prompt_yes_no(session, "Confirm?", default=False))
    assert _written_text(session).endswith("Y\r\n")


def test_disconnect_propagates_without_choosing_a_default():
    session = FakeSession([SessionClosedError("gone")])
    with pytest.raises(SessionClosedError, match="gone"):
        asyncio.run(prompt_yes_no(session, "Confirm?", default=True))


def test_line_only_adapter_retries_invalid_input_for_compatibility():
    class LineOnlySession:
        def __init__(self):
            self.lines = iter(["maybe", "n"])
            self.written: list[str] = []

        async def write(self, text: str) -> None:
            self.written.append(text)

        async def read_line(self, **kwargs) -> str:
            return next(self.lines)

    session = LineOnlySession()
    assert asyncio.run(prompt_yes_no(session, "Confirm?", default=True)) is False
    assert "\a" in "".join(session.written)
