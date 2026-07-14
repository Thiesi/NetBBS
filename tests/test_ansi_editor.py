"""Integration tests for netbbs.net.ansi_editor.edit_ansi_art (design
doc -- welcome banner round B1), driven with a scripted FakeSession --
the WYSIWYG ANSI art editor, the first real consumer of the
screen-buffer/diff abstraction. `pick_item` (used for the glyph/color
pickers) reads two-digit selections via `read_key`, so this
FakeSession's single ordered `_inputs` queue serves `read_key`/
`read_line`/`read_editor_key` alike, matching tests/test_admin_flow.py's
established convention."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.net.ansi_editor import edit_ansi_art
from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.session import Session
from netbbs.rendering.ansi_art import decode_ansi_bytes
from netbbs.rendering.ansi_parse import parse_ansi_into_buffer
from netbbs.rendering.screen_buffer import ScreenBuffer

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
    def __init__(self, inputs: list[str] | None = None):
        self._inputs = list(inputs or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
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
            # Blocks forever once scripted input runs out -- the same
            # shape a real session has while genuinely waiting for the
            # next keystroke, which is what lets the autosave-runs-
            # independently-of-the-main-loop test hold the editor
            # suspended here while its background task fires.
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


def _buffer_from(result: bytes, width: int = 80, height: int = 24) -> ScreenBuffer:
    buf = ScreenBuffer(width, height)
    parse_ansi_into_buffer(decode_ansi_bytes(result), buf)
    return buf


# -- typing / cursor movement ----------------------------------------------


def test_typing_paints_and_advances_the_cursor(tmp_path):
    async def scenario():
        session = FakeSession(["A", "B", "C", "CTRL+S"])
        return await edit_ansi_art(
            session, initial_bytes=None, draft_path=tmp_path / "d.draft", autosave_interval_seconds=9999
        )

    result = asyncio.run(scenario())
    buf = _buffer_from(result)
    assert buf.get_cell(0, 0).char == "A"
    assert buf.get_cell(0, 1).char == "B"
    assert buf.get_cell(0, 2).char == "C"


def test_typing_wraps_to_the_next_row_at_the_canvas_width(tmp_path):
    async def scenario():
        session = FakeSession(["X"] * 80 + ["Y", "CTRL+S"])
        return await edit_ansi_art(
            session, initial_bytes=None, draft_path=tmp_path / "d.draft",
            width=80, height=3, autosave_interval_seconds=9999,
        )

    result = asyncio.run(scenario())
    buf = _buffer_from(result, height=3)
    assert buf.get_cell(0, 79).char == "X"
    assert buf.get_cell(1, 0).char == "Y"


def test_arrow_keys_move_the_cursor(tmp_path):
    async def scenario():
        session = FakeSession(["RIGHT", "RIGHT", "DOWN", "A", "CTRL+S"])
        return await edit_ansi_art(
            session, initial_bytes=None, draft_path=tmp_path / "d.draft",
            width=10, height=5, autosave_interval_seconds=9999,
        )

    result = asyncio.run(scenario())
    buf = _buffer_from(result, width=10, height=5)
    assert buf.get_cell(1, 2).char == "A"


def test_cursor_cannot_move_above_row_zero(tmp_path):
    async def scenario():
        session = FakeSession(["UP", "UP", "UP", "A", "CTRL+S"])
        return await edit_ansi_art(
            session, initial_bytes=None, draft_path=tmp_path / "d.draft",
            width=10, height=5, autosave_interval_seconds=9999,
        )

    result = asyncio.run(scenario())
    buf = _buffer_from(result, width=10, height=5)
    assert buf.get_cell(0, 0).char == "A"


def test_cursor_cannot_move_past_the_last_row_or_column(tmp_path):
    async def scenario():
        session = FakeSession(["DOWN"] * 10 + ["RIGHT"] * 10 + ["A", "CTRL+S"])
        return await edit_ansi_art(
            session, initial_bytes=None, draft_path=tmp_path / "d.draft",
            width=3, height=3, autosave_interval_seconds=9999,
        )

    result = asyncio.run(scenario())
    buf = _buffer_from(result, width=3, height=3)
    assert buf.get_cell(2, 2).char == "A"


def test_home_and_end_jump_within_the_row(tmp_path):
    async def scenario():
        session = FakeSession(["RIGHT", "RIGHT", "HOME", "A", "END", "B", "CTRL+S"])
        return await edit_ansi_art(
            session, initial_bytes=None, draft_path=tmp_path / "d.draft",
            width=5, height=2, autosave_interval_seconds=9999,
        )

    result = asyncio.run(scenario())
    buf = _buffer_from(result, width=5, height=2)
    assert buf.get_cell(0, 0).char == "A"
    assert buf.get_cell(0, 4).char == "B"


def test_backspace_erases_and_moves_the_cursor_back(tmp_path):
    async def scenario():
        session = FakeSession(["A", "B", "BACKSPACE", "C", "CTRL+S"])
        return await edit_ansi_art(
            session, initial_bytes=None, draft_path=tmp_path / "d.draft",
            width=10, height=2, autosave_interval_seconds=9999,
        )

    result = asyncio.run(scenario())
    buf = _buffer_from(result, width=10, height=2)
    assert buf.get_cell(0, 0).char == "A"
    assert buf.get_cell(0, 1).char == "C"  # overwrote the erased "B"


def test_delete_clears_the_cell_without_moving_the_cursor(tmp_path):
    async def scenario():
        session = FakeSession(["A", "LEFT", "DELETE", "CTRL+S"])
        return await edit_ansi_art(
            session, initial_bytes=None, draft_path=tmp_path / "d.draft",
            width=10, height=2, autosave_interval_seconds=9999,
        )

    result = asyncio.run(scenario())
    buf = _buffer_from(result, width=10, height=2)
    assert buf.get_cell(0, 0).char == " "


# -- glyph / color pickers -------------------------------------------------


def test_glyph_picker_changes_what_typing_places(tmp_path):
    async def scenario():
        # Ctrl+G opens the glyph picker; "0","1" selects the first item
        # (Full block, per _GLYPHS' own ordering) via pick_item's
        # two-digit selection.
        session = FakeSession(["CTRL+G", "0", "1", "A", "CTRL+S"])
        return await edit_ansi_art(
            session, initial_bytes=None, draft_path=tmp_path / "d.draft",
            width=10, height=2, autosave_interval_seconds=9999,
        )

    result = asyncio.run(scenario())
    buf = _buffer_from(result, width=10, height=2)
    assert buf.get_cell(0, 0).char == "█"


def test_foreground_color_picker_changes_what_typing_places(tmp_path):
    async def scenario():
        # Ctrl+P opens the foreground picker; pick_item's selection is
        # 1-indexed page position, so "0","3" selects the 3rd item --
        # index 2, "Green".
        session = FakeSession(["CTRL+P", "0", "3", "A", "CTRL+S"])
        return await edit_ansi_art(
            session, initial_bytes=None, draft_path=tmp_path / "d.draft",
            width=10, height=2, autosave_interval_seconds=9999,
        )

    result = asyncio.run(scenario())
    buf = _buffer_from(result, width=10, height=2)
    assert buf.get_cell(0, 0).fg == 2


def test_background_color_picker_changes_what_typing_places(tmp_path):
    async def scenario():
        # 4th page position (index 3, "Yellow").
        session = FakeSession(["CTRL+B", "0", "4", "A", "CTRL+S"])
        return await edit_ansi_art(
            session, initial_bytes=None, draft_path=tmp_path / "d.draft",
            width=10, height=2, autosave_interval_seconds=9999,
        )

    result = asyncio.run(scenario())
    buf = _buffer_from(result, width=10, height=2)
    assert buf.get_cell(0, 0).bg == 3


# -- save / quit ------------------------------------------------------------


def test_save_returns_bytes_and_deletes_the_draft(tmp_path):
    draft = tmp_path / "d.draft"
    draft.write_bytes(b"stale draft that should be cleaned up")

    async def scenario():
        session = FakeSession(["n", "A", "CTRL+S"])  # decline resuming the stale draft
        return await edit_ansi_art(session, initial_bytes=None, draft_path=draft, autosave_interval_seconds=9999)

    result = asyncio.run(scenario())
    assert result is not None
    assert not draft.exists()


def test_quit_without_editing_returns_none_with_no_prompt(tmp_path):
    async def scenario():
        session = FakeSession(["ESCAPE"])  # nothing typed -- no confirm prompt expected
        return await edit_ansi_art(
            session, initial_bytes=None, draft_path=tmp_path / "d.draft", autosave_interval_seconds=9999
        )

    result = asyncio.run(scenario())
    assert result is None


def test_quit_after_editing_prompts_and_discard_returns_none(tmp_path):
    draft = tmp_path / "d.draft"

    async def scenario():
        session = FakeSession(["A", "ESCAPE", "d"])
        return await edit_ansi_art(session, initial_bytes=None, draft_path=draft, autosave_interval_seconds=9999)

    result = asyncio.run(scenario())
    assert result is None
    assert not draft.exists()


def test_quit_after_editing_save_choice_saves_and_returns_bytes(tmp_path):
    async def scenario():
        session = FakeSession(["A", "ESCAPE", "s"])
        return await edit_ansi_art(
            session, initial_bytes=None, draft_path=tmp_path / "d.draft", autosave_interval_seconds=9999
        )

    result = asyncio.run(scenario())
    buf = _buffer_from(result)
    assert buf.get_cell(0, 0).char == "A"


def test_quit_after_editing_cancel_choice_returns_to_the_editor(tmp_path):
    async def scenario():
        session = FakeSession(["A", "ESCAPE", "c", "CTRL+S"])
        return await edit_ansi_art(
            session, initial_bytes=None, draft_path=tmp_path / "d.draft", autosave_interval_seconds=9999
        )

    result = asyncio.run(scenario())
    buf = _buffer_from(result)
    assert buf.get_cell(0, 0).char == "A"


# -- loading existing content -----------------------------------------------


def test_initial_bytes_are_loaded_into_the_canvas(tmp_path):
    async def scenario():
        session = FakeSession(["CTRL+S"])
        return await edit_ansi_art(
            session, initial_bytes=b"Hello", draft_path=tmp_path / "d.draft", autosave_interval_seconds=9999
        )

    result = asyncio.run(scenario())
    buf = _buffer_from(result)
    assert buf.get_cell(0, 0).char == "H"


# -- draft recovery / autosave -----------------------------------------------


def test_pre_existing_draft_is_offered_and_resumed(tmp_path):
    draft = tmp_path / "d.draft"
    draft.write_bytes(b"DRAFT")

    async def scenario():
        session = FakeSession(["y", "CTRL+S"])  # resume the draft
        return await edit_ansi_art(
            session, initial_bytes=b"INITIAL", draft_path=draft, autosave_interval_seconds=9999
        )

    result = asyncio.run(scenario())
    buf = _buffer_from(result)
    assert buf.get_cell(0, 0).char == "D"  # from the draft, not initial_bytes


def test_declining_a_pre_existing_draft_uses_initial_bytes_instead(tmp_path):
    draft = tmp_path / "d.draft"
    draft.write_bytes(b"DRAFT")

    async def scenario():
        session = FakeSession(["n", "CTRL+S"])
        return await edit_ansi_art(
            session, initial_bytes=b"INITIAL", draft_path=draft, autosave_interval_seconds=9999
        )

    result = asyncio.run(scenario())
    buf = _buffer_from(result)
    assert buf.get_cell(0, 0).char == "I"


def test_autosave_writes_the_draft_while_dirty(tmp_path):
    draft = tmp_path / "d.draft"

    async def scenario():
        session = FakeSession(["A"])  # one edit, then nothing further -- blocks
        task = asyncio.create_task(
            edit_ansi_art(session, initial_bytes=None, draft_path=draft, autosave_interval_seconds=0.02)
        )
        await asyncio.sleep(0.15)
        assert draft.exists()
        buf = _buffer_from(draft.read_bytes())
        assert buf.get_cell(0, 0).char == "A"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())


def test_autosave_does_not_write_when_nothing_changed(tmp_path):
    draft = tmp_path / "d.draft"

    async def scenario():
        session = FakeSession([])  # no edits at all -- blocks immediately
        task = asyncio.create_task(
            edit_ansi_art(session, initial_bytes=None, draft_path=draft, autosave_interval_seconds=0.02)
        )
        await asyncio.sleep(0.15)
        assert not draft.exists()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
