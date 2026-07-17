"""Tests for `netbbs.net.confirm.prompt_yes_no` — the shared yes/no
prompt that actually honors a shown default on a bare Enter, fixing
the `read_key()`-swallows-CR/LF defect 38 call sites previously had."""

from __future__ import annotations

import asyncio

from netbbs.net.confirm import prompt_yes_no, prompt_yes_no_or_keep
from netbbs.net.session import Session


class FakeSession(Session):
    def __init__(self, lines: list[str]):
        self._lines = list(lines)
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.peer_address = None

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def read_line(self, echo: bool = True, history=None, completer=None) -> str:
        return self._lines.pop(0)

    async def read_key(self, echo: bool = True) -> str:
        raise NotImplementedError

    async def read_editor_key(self):
        raise NotImplementedError

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


def test_bare_enter_selects_the_default_true():
    session = FakeSession([""])
    result = asyncio.run(prompt_yes_no(session, "Confirm?", default=True))
    assert result is True


def test_bare_enter_selects_the_default_false():
    session = FakeSession([""])
    result = asyncio.run(prompt_yes_no(session, "Confirm?", default=False))
    assert result is False


def test_explicit_y_is_true_regardless_of_default():
    session = FakeSession(["y"])
    assert asyncio.run(prompt_yes_no(session, "Confirm?", default=False)) is True


def test_explicit_n_is_false_regardless_of_default():
    session = FakeSession(["n"])
    assert asyncio.run(prompt_yes_no(session, "Confirm?", default=True)) is False


def test_full_words_yes_and_no_are_accepted():
    assert asyncio.run(prompt_yes_no(FakeSession(["yes"]), "Confirm?", default=False)) is True
    assert asyncio.run(prompt_yes_no(FakeSession(["no"]), "Confirm?", default=True)) is False


def test_case_insensitive():
    assert asyncio.run(prompt_yes_no(FakeSession(["Y"]), "Confirm?", default=False)) is True
    assert asyncio.run(prompt_yes_no(FakeSession(["N"]), "Confirm?", default=True)) is False


def test_garbage_input_falls_back_to_default():
    session = FakeSession(["maybe"])
    result = asyncio.run(prompt_yes_no(session, "Confirm?", default=True))
    assert result is True


def test_hint_reflects_the_default():
    session = FakeSession([""])
    asyncio.run(prompt_yes_no(session, "Confirm?", default=True))
    assert "[Y/n]" in _written_text(session)

    session2 = FakeSession([""])
    asyncio.run(prompt_yes_no(session2, "Confirm?", default=False))
    assert "[y/N]" in _written_text(session2)


# -- prompt_yes_no_or_keep -----------------------------------------------------


def test_bare_enter_keeps_current_true():
    session = FakeSession([""])
    assert asyncio.run(prompt_yes_no_or_keep(session, "Pinned?", current=True)) is True


def test_bare_enter_keeps_current_false():
    session = FakeSession([""])
    assert asyncio.run(prompt_yes_no_or_keep(session, "Pinned?", current=False)) is False


def test_explicit_y_overrides_current_false():
    session = FakeSession(["y"])
    assert asyncio.run(prompt_yes_no_or_keep(session, "Pinned?", current=False)) is True


def test_explicit_n_overrides_current_true():
    session = FakeSession(["n"])
    assert asyncio.run(prompt_yes_no_or_keep(session, "Pinned?", current=True)) is False


def test_garbage_input_keeps_current():
    session = FakeSession(["maybe"])
    assert asyncio.run(prompt_yes_no_or_keep(session, "Pinned?", current=True)) is True


def test_keep_hint_shows_only_the_current_value():
    session = FakeSession([""])
    asyncio.run(prompt_yes_no_or_keep(session, "Pinned?", current=True))
    assert "[y]" in _written_text(session)

    session2 = FakeSession([""])
    asyncio.run(prompt_yes_no_or_keep(session2, "Pinned?", current=False))
    assert "[N]" in _written_text(session2)
