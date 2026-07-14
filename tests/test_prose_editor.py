"""Integration tests for netbbs.net.prose_editor.edit_prose (design doc
-- prose editor round B2), driven with a scripted FakeSession -- same
convention tests/test_ansi_editor.py established."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.prose_editor import edit_prose
from netbbs.net.session import Session

_EDITOR_KEY_SENTINELS: dict[str, EditorKeyKind] = {
    "ENTER": EditorKeyKind.ENTER,
    "BACKSPACE": EditorKeyKind.BACKSPACE,
    "DELETE": EditorKeyKind.DELETE,
    "TAB": EditorKeyKind.TAB,
    "ESCAPE": EditorKeyKind.ESCAPE,
    "UP": EditorKeyKind.UP,
    "DOWN": EditorKeyKind.DOWN,
    "LEFT": EditorKeyKind.LEFT,
    "RIGHT": EditorKeyKind.RIGHT,
    "HOME": EditorKeyKind.HOME,
    "END": EditorKeyKind.END,
    "PAGE_UP": EditorKeyKind.PAGE_UP,
    "PAGE_DOWN": EditorKeyKind.PAGE_DOWN,
}


class FakeSession(Session):
    def __init__(self, inputs: list[str] | None = None, *, width: int = 80, height: int = 24):
        self._inputs = list(inputs or [])
        self.written: list[str] = []
        self.terminal_width = width
        self.terminal_height = height
        self.peer_address = None

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def read_line(self, echo: bool = True, history=None, completer=None) -> str:
        if not self._inputs:
            raise AssertionError("FakeSession ran out of scripted input (read_line)")
        return self._inputs.pop(0)

    async def read_key(self, echo: bool = True) -> str:
        if not self._inputs:
            raise AssertionError("FakeSession ran out of scripted input (read_key)")
        return self._inputs.pop(0)

    async def read_editor_key(self) -> EditorKey:
        if not self._inputs:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        raw = self._inputs.pop(0)
        if raw in _EDITOR_KEY_SENTINELS:
            return EditorKey(_EDITOR_KEY_SENTINELS[raw])
        if raw.startswith("CTRL+"):
            return EditorKey(EditorKeyKind.CTRL, char=raw[len("CTRL+") :].lower())
        return EditorKey(EditorKeyKind.CHAR, char=raw)

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


def _type(text: str) -> list[str]:
    return list(text)


# -- typing / basic editing --------------------------------------------


def test_typing_produces_the_typed_text(tmp_path):
    async def scenario():
        session = FakeSession(_type("hello") + ["CTRL+O"])
        return await edit_prose(session, initial_text=None, draft_path=tmp_path / "d.draft")

    result = asyncio.run(scenario())
    assert result == "hello"


def test_enter_inserts_a_real_line_break(tmp_path):
    async def scenario():
        session = FakeSession(_type("first") + ["ENTER"] + _type("second") + ["CTRL+O"])
        return await edit_prose(session, initial_text=None, draft_path=tmp_path / "d.draft")

    result = asyncio.run(scenario())
    assert result == "first\nsecond"


def test_backspace_erases_the_preceding_character(tmp_path):
    async def scenario():
        session = FakeSession(_type("abx") + ["BACKSPACE"] + _type("c") + ["CTRL+O"])
        return await edit_prose(session, initial_text=None, draft_path=tmp_path / "d.draft")

    result = asyncio.run(scenario())
    assert result == "abc"


def test_arrow_keys_move_the_cursor_for_mid_line_insertion(tmp_path):
    async def scenario():
        session = FakeSession(_type("ac") + ["LEFT"] + _type("b") + ["CTRL+O"])
        return await edit_prose(session, initial_text=None, draft_path=tmp_path / "d.draft")

    result = asyncio.run(scenario())
    assert result == "abc"


def test_home_and_end_jump_within_the_line(tmp_path):
    async def scenario():
        session = FakeSession(_type("bcd") + ["HOME"] + _type("a") + ["END"] + _type("e") + ["CTRL+O"])
        return await edit_prose(session, initial_text=None, draft_path=tmp_path / "d.draft")

    result = asyncio.run(scenario())
    assert result == "abcde"


# -- word-wrap-aware up/down navigation ----------------------------------


def test_down_from_a_wrapped_row_moves_by_visual_row_not_logical_line(tmp_path):
    # Well over 40 columns (edit_prose's enforced width floor) worth of
    # text, so it wraps across several screen rows even at that floor;
    # Down from the first row must land on the *second visual row* of
    # the same paragraph, not skip past the whole thing.
    long_line = " ".join(f"word{i}" for i in range(30))

    async def scenario():
        session = FakeSession(
            _type(long_line) + ["HOME", "DOWN"] + _type("|") + ["CTRL+O"],  # marks where the cursor landed
            width=40,
        )
        return await edit_prose(session, initial_text=None, draft_path=tmp_path / "d.draft")

    result = asyncio.run(scenario())
    # The inserted "|" must NOT be at the very start (row 0) or the
    # very end (past all wrapped rows) -- it landed somewhere inside
    # the wrapped text, proving Down moved one screen row, not zero
    # (stuck) and not past the whole paragraph.
    assert "|" in result
    assert not result.startswith("|")
    assert not result.endswith("|")


# -- save / quit ------------------------------------------------------------


def test_quit_without_editing_returns_none_with_no_prompt(tmp_path):
    async def scenario():
        session = FakeSession(["CTRL+X"])
        return await edit_prose(session, initial_text=None, draft_path=tmp_path / "d.draft")

    result = asyncio.run(scenario())
    assert result is None


def test_quit_after_editing_prompts_and_discard_returns_none(tmp_path):
    draft = tmp_path / "d.draft"

    async def scenario():
        session = FakeSession(_type("x") + ["CTRL+X", "d"])
        return await edit_prose(session, initial_text=None, draft_path=draft)

    result = asyncio.run(scenario())
    assert result is None
    assert not draft.exists()


def test_quit_after_editing_save_choice_saves_and_returns_text(tmp_path):
    async def scenario():
        session = FakeSession(_type("hello") + ["CTRL+X", "s"])
        return await edit_prose(session, initial_text=None, draft_path=tmp_path / "d.draft")

    result = asyncio.run(scenario())
    assert result == "hello"


def test_quit_after_editing_cancel_choice_returns_to_the_editor(tmp_path):
    async def scenario():
        session = FakeSession(_type("hi") + ["CTRL+X", "c"] + _type("!") + ["CTRL+O"])
        return await edit_prose(session, initial_text=None, draft_path=tmp_path / "d.draft")

    result = asyncio.run(scenario())
    assert result == "hi!"


def test_save_deletes_a_stale_draft(tmp_path):
    draft = tmp_path / "d.draft"
    draft.write_bytes(b"stale draft that should be cleaned up")

    async def scenario():
        session = FakeSession(["n"] + _type("x") + ["CTRL+O"])  # decline resuming the stale draft
        return await edit_prose(session, initial_text=None, draft_path=draft)

    result = asyncio.run(scenario())
    assert result == "x"
    assert not draft.exists()


# -- loading existing content ------------------------------------------------


def test_initial_text_is_loaded_into_the_buffer(tmp_path):
    async def scenario():
        session = FakeSession(["END"] + _type("!") + ["CTRL+O"])
        return await edit_prose(session, initial_text="hello", draft_path=tmp_path / "d.draft")

    result = asyncio.run(scenario())
    assert result == "hello!"


# -- draft recovery / autosave -------------------------------------------------


def test_pre_existing_draft_is_offered_and_resumed(tmp_path):
    draft = tmp_path / "d.draft"
    draft.write_text("recovered text", encoding="utf-8")

    async def scenario():
        session = FakeSession(["y"] + ["END"] + _type("!") + ["CTRL+O"])
        return await edit_prose(session, initial_text="ignored", draft_path=draft)

    result = asyncio.run(scenario())
    assert result == "recovered text!"


def test_declining_a_pre_existing_draft_uses_initial_text_instead(tmp_path):
    draft = tmp_path / "d.draft"
    draft.write_text("recovered text", encoding="utf-8")

    async def scenario():
        session = FakeSession(["n"] + ["END"] + _type("!") + ["CTRL+O"])
        return await edit_prose(session, initial_text="original", draft_path=draft)

    result = asyncio.run(scenario())
    assert result == "original!"


def test_autosave_writes_the_draft_while_dirty(tmp_path):
    draft = tmp_path / "d.draft"

    async def scenario():
        session = FakeSession(_type("A"))
        task = asyncio.create_task(
            edit_prose(session, initial_text=None, draft_path=draft, autosave_interval_seconds=0.05)
        )
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert draft.exists()
    assert draft.read_text(encoding="utf-8") == "A"


def test_autosave_does_not_write_when_nothing_changed(tmp_path):
    draft = tmp_path / "d.draft"

    async def scenario():
        session = FakeSession([])
        task = asyncio.create_task(
            edit_prose(session, initial_text="unchanged", draft_path=draft, autosave_interval_seconds=0.05)
        )
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert not draft.exists()
