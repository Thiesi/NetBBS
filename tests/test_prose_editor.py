"""Integration tests for netbbs.net.prose_editor.edit_prose (design doc
-- prose editor round B2), driven with a scripted FakeSession -- same
convention tests/test_ansi_editor.py established."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.prose_editor import edit_prose
from netbbs.net.session import Session, SessionClosedError
from netbbs.rendering import clear_screen

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
        return await edit_prose(session, initial_text=None, draft_path=tmp_path / "d.draft", max_bytes=100_000)

    result = asyncio.run(scenario())
    assert result == "hello"


def test_enter_inserts_a_real_line_break(tmp_path):
    async def scenario():
        session = FakeSession(_type("first") + ["ENTER"] + _type("second") + ["CTRL+O"])
        return await edit_prose(session, initial_text=None, draft_path=tmp_path / "d.draft", max_bytes=100_000)

    result = asyncio.run(scenario())
    assert result == "first\nsecond"


def test_backspace_erases_the_preceding_character(tmp_path):
    async def scenario():
        session = FakeSession(_type("abx") + ["BACKSPACE"] + _type("c") + ["CTRL+O"])
        return await edit_prose(session, initial_text=None, draft_path=tmp_path / "d.draft", max_bytes=100_000)

    result = asyncio.run(scenario())
    assert result == "abc"


def test_arrow_keys_move_the_cursor_for_mid_line_insertion(tmp_path):
    async def scenario():
        session = FakeSession(_type("ac") + ["LEFT"] + _type("b") + ["CTRL+O"])
        return await edit_prose(session, initial_text=None, draft_path=tmp_path / "d.draft", max_bytes=100_000)

    result = asyncio.run(scenario())
    assert result == "abc"


def test_home_and_end_jump_within_the_line(tmp_path):
    async def scenario():
        session = FakeSession(_type("bcd") + ["HOME"] + _type("a") + ["END"] + _type("e") + ["CTRL+O"])
        return await edit_prose(session, initial_text=None, draft_path=tmp_path / "d.draft", max_bytes=100_000)

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
        return await edit_prose(session, initial_text=None, draft_path=tmp_path / "d.draft", max_bytes=100_000)

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
        return await edit_prose(session, initial_text=None, draft_path=tmp_path / "d.draft", max_bytes=100_000)

    result = asyncio.run(scenario())
    assert result is None


def test_quit_after_editing_prompts_and_discard_returns_none(tmp_path):
    draft = tmp_path / "d.draft"

    async def scenario():
        session = FakeSession(_type("x") + ["CTRL+X", "d"])
        return await edit_prose(session, initial_text=None, draft_path=draft, max_bytes=100_000)

    result = asyncio.run(scenario())
    assert result is None
    assert not draft.exists()


def test_quit_after_editing_save_choice_saves_and_returns_text(tmp_path):
    async def scenario():
        session = FakeSession(_type("hello") + ["CTRL+X", "s"])
        return await edit_prose(session, initial_text=None, draft_path=tmp_path / "d.draft", max_bytes=100_000)

    result = asyncio.run(scenario())
    assert result == "hello"


def test_quit_after_editing_cancel_choice_returns_to_the_editor(tmp_path):
    async def scenario():
        session = FakeSession(_type("hi") + ["CTRL+X", "c"] + _type("!") + ["CTRL+O"])
        return await edit_prose(session, initial_text=None, draft_path=tmp_path / "d.draft", max_bytes=100_000)

    result = asyncio.run(scenario())
    assert result == "hi!"


# -- GitHub issue #38: no leftover status line after exit -------------------


def test_saving_clears_the_screen_before_returning(tmp_path):
    async def scenario():
        session = FakeSession(_type("hello") + ["CTRL+O"])
        await edit_prose(session, initial_text=None, draft_path=tmp_path / "d.draft", max_bytes=100_000)
        return session

    session = asyncio.run(scenario())
    assert session.written[-1] == clear_screen()


def test_quit_without_editing_clears_the_screen_before_returning(tmp_path):
    async def scenario():
        session = FakeSession(["CTRL+X"])
        await edit_prose(session, initial_text=None, draft_path=tmp_path / "d.draft", max_bytes=100_000)
        return session

    session = asyncio.run(scenario())
    assert session.written[-1] == clear_screen()


def test_quit_after_confirmed_discard_clears_the_screen_before_returning(tmp_path):
    async def scenario():
        session = FakeSession(_type("x") + ["CTRL+X", "d"])
        await edit_prose(session, initial_text=None, draft_path=tmp_path / "d.draft", max_bytes=100_000)
        return session

    session = asyncio.run(scenario())
    assert session.written[-1] == clear_screen()


def test_save_deletes_a_stale_draft(tmp_path):
    draft = tmp_path / "d.draft"
    draft.write_bytes(b"stale draft that should be cleaned up")

    async def scenario():
        session = FakeSession(["n"] + _type("x") + ["CTRL+O"])  # decline resuming the stale draft
        return await edit_prose(session, initial_text=None, draft_path=draft, max_bytes=100_000)

    result = asyncio.run(scenario())
    assert result == "x"
    assert not draft.exists()


# -- loading existing content ------------------------------------------------


def test_initial_text_is_loaded_into_the_buffer(tmp_path):
    async def scenario():
        session = FakeSession(["END"] + _type("!") + ["CTRL+O"])
        return await edit_prose(session, initial_text="hello", draft_path=tmp_path / "d.draft", max_bytes=100_000)

    result = asyncio.run(scenario())
    assert result == "hello!"


# -- draft recovery / autosave -------------------------------------------------


def test_pre_existing_draft_is_offered_and_resumed(tmp_path):
    draft = tmp_path / "d.draft"
    draft.write_text("recovered text", encoding="utf-8")

    async def scenario():
        session = FakeSession(["y"] + ["END"] + _type("!") + ["CTRL+O"])
        return await edit_prose(session, initial_text="ignored", draft_path=draft, max_bytes=100_000)

    result = asyncio.run(scenario())
    assert result == "recovered text!"


def test_declining_a_pre_existing_draft_uses_initial_text_instead(tmp_path):
    draft = tmp_path / "d.draft"
    draft.write_text("recovered text", encoding="utf-8")

    async def scenario():
        session = FakeSession(["n"] + ["END"] + _type("!") + ["CTRL+O"])
        return await edit_prose(session, initial_text="original", draft_path=draft, max_bytes=100_000)

    result = asyncio.run(scenario())
    assert result == "original!"


# -- GitHub issue #32: content ceiling ---------------------------------


def test_typing_beyond_max_bytes_is_refused(tmp_path):
    async def scenario():
        session = FakeSession(_type("abcde") + ["CTRL+O"])
        return await edit_prose(session, initial_text=None, draft_path=tmp_path / "d.draft", max_bytes=3)

    result = asyncio.run(scenario())
    assert result == "abc"  # only the first 3 bytes were accepted


def test_typing_at_max_bytes_sounds_the_bell(tmp_path):
    async def scenario():
        session = FakeSession(_type("abcd") + ["CTRL+O"])
        await edit_prose(session, initial_text=None, draft_path=tmp_path / "d.draft", max_bytes=3)
        return session

    session = asyncio.run(scenario())
    assert "\a" in _written_text(session)


def test_multibyte_character_respects_the_byte_ceiling_not_char_count(tmp_path):
    # "é" is 2 UTF-8 bytes -- a 3-byte ceiling must accept it (1 byte
    # remaining after "a") but refuse a second one.
    async def scenario():
        session = FakeSession(_type("aéé") + ["CTRL+O"])
        return await edit_prose(session, initial_text=None, draft_path=tmp_path / "d.draft", max_bytes=3)

    result = asyncio.run(scenario())
    assert result == "aé"
    assert len(result.encode("utf-8")) == 3


def test_enter_beyond_max_bytes_is_refused(tmp_path):
    async def scenario():
        session = FakeSession(_type("ab") + ["ENTER"] + ["CTRL+O"])
        return await edit_prose(session, initial_text=None, draft_path=tmp_path / "d.draft", max_bytes=2)

    result = asyncio.run(scenario())
    assert result == "ab"  # the newline was refused, no trailing "\n"


def test_pre_existing_content_at_the_limit_shows_in_the_status_line(tmp_path):
    async def scenario():
        session = FakeSession(["CTRL+O"])
        await edit_prose(session, initial_text="abc", draft_path=tmp_path / "d.draft", max_bytes=3)
        return session

    session = asyncio.run(scenario())
    assert "AT LENGTH LIMIT" in _written_text(session)


def test_deleting_below_the_limit_allows_typing_again(tmp_path):
    async def scenario():
        # Cursor loads at the start of the buffer, not the end -- END
        # first so BACKSPACE actually removes the trailing "c".
        session = FakeSession(["END", "BACKSPACE"] + _type("x") + ["CTRL+O"])
        return await edit_prose(session, initial_text="abc", draft_path=tmp_path / "d.draft", max_bytes=3)

    result = asyncio.run(scenario())
    assert result == "abx"


def test_autosave_writes_the_draft_while_dirty(tmp_path):
    draft = tmp_path / "d.draft"

    async def scenario():
        session = FakeSession(_type("A"))
        task = asyncio.create_task(
            edit_prose(session, initial_text=None, draft_path=draft, autosave_interval_seconds=0.05, max_bytes=100_000)
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
            edit_prose(session, initial_text="unchanged", draft_path=draft, autosave_interval_seconds=0.05, max_bytes=100_000)
        )
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())


# -- disconnect cleanup (GitHub issue #43) ------------------------------


class _DisconnectingSession(FakeSession):
    """Simulates a genuinely dead transport: the very next key read
    raises SessionClosedError, and every write from that point on
    (including the editor's own best-effort clear_screen() in its
    finally block) raises too -- a real closed connection, not just one
    bad write."""

    def __init__(self):
        super().__init__([])
        self._closed = False

    async def read_editor_key(self) -> EditorKey:
        self._closed = True
        raise SessionClosedError("connection lost")

    async def write(self, text: str) -> None:
        if self._closed:
            raise SessionClosedError("connection lost")
        await super().write(text)


def test_disconnect_still_cancels_the_autosave_task_even_when_the_screen_clear_also_fails(tmp_path):
    """Before this fix, the finally block's `await
    session.write(clear_screen())` ran before autosave_task.cancel() --
    a SessionClosedError there (a genuinely dead transport, exactly
    what this session simulates) skipped cancellation entirely and
    leaked the autosave task running forever against a session nothing
    will ever read from again."""
    draft = tmp_path / "d.draft"

    async def scenario():
        session = _DisconnectingSession()
        with pytest.raises(SessionClosedError):
            await edit_prose(
                session, initial_text=None, draft_path=draft, autosave_interval_seconds=9999, max_bytes=100_000
            )
        current = asyncio.current_task()
        leaked = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        assert leaked == []

    asyncio.run(scenario())


def test_repeated_disconnects_never_leave_autosave_tasks_pending(tmp_path):
    draft = tmp_path / "d.draft"

    async def scenario():
        for _ in range(5):
            session = _DisconnectingSession()
            with pytest.raises(SessionClosedError):
                await edit_prose(
                    session, initial_text=None, draft_path=draft, autosave_interval_seconds=9999, max_bytes=100_000
                )
        current = asyncio.current_task()
        leaked = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        assert leaked == []

    asyncio.run(scenario())
    assert not draft.exists()
