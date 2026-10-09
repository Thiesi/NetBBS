"""Trimming a reply's quote and clearing a text (issue #837): the line
editor's ranged /delete and /unquote, its pointer to the fullscreen editor,
and the fullscreen editor's Ctrl+E."""

from __future__ import annotations

import asyncio

from netbbs.net.composition import FULLSCREEN_EDITOR_HINT, edit_line_body
from netbbs.quoting import quote_body

from tests.test_composition import FakeSession, _text
from tests.test_prose_editor_keys import _edit


def _line_edit(lines, *, initial_text=None):
    async def scenario():
        session = FakeSession(lines=lines)
        body = await edit_line_body(session, initial_text=initial_text, max_bytes=10_000, max_lines=200)
        return body, session

    return asyncio.run(scenario())


_QUOTE = quote_body("First line.\nSecond line.\n\nThird line.", author="lena_h")


# -- /delete N-M ---------------------------------------------------------


def test_delete_takes_a_range():
    body, session = _line_edit(["/delete 2-4", "/done"], initial_text="a\nb\nc\nd\ne")
    assert body == "a\ne"
    assert "Deleted lines 2-4." in _text(session)


def test_delete_of_one_line_still_names_it():
    body, session = _line_edit(["/delete 2", "/done"], initial_text="a\nb\nc")
    assert body == "a\nc"
    assert "Deleted line 2: b" in _text(session)


def test_a_backwards_or_out_of_range_span_is_refused():
    body, session = _line_edit(["/delete 3-2", "/delete 2-9", "/delete x-y", "/done"], initial_text="a\nb\nc")
    assert body == "a\nb\nc"
    assert _text(session).count("Usage: /delete N or /delete N-M (1-3)") == 3


def test_a_superscript_digit_is_refused_not_a_crash():
    commands = ["/delete 1-\u00b2", "/delete \u00b2", "/edit \u00b2", "/insert \u00b3", "/done"]
    body, session = _line_edit(commands, initial_text="a\nb\nc")
    assert body == "a\nb\nc"
    assert "Usage: /delete" in _text(session)
    assert "Usage: /edit" in _text(session)
    assert "Usage: /insert" in _text(session)


def test_deleting_above_an_insert_point_keeps_writing_in_the_same_place():
    body, _ = _line_edit(["/insert 4", "/delete 1-2", "new", "/done"], initial_text="a\nb\nc\nd")
    assert body == "c\nnew\nd"


# -- /unquote ------------------------------------------------------------


def test_unquote_removes_the_quote_and_who_wrote_it():
    body, session = _line_edit(["My answer.", "/unquote", "/done"], initial_text=_QUOTE)
    assert body == "My answer."
    assert "Removed the quote" in _text(session)


def test_unquote_keeps_answers_written_between_quoted_lines():
    text = "lena_h wrote:\n> Question one?\nAnswer one.\n> Question two?\nAnswer two."
    body, _ = _line_edit(["/unquote", "/done"], initial_text=text)
    assert body == "Answer one.\nAnswer two."


def test_unquote_without_a_quote_says_so():
    body, session = _line_edit(["/unquote", "/done"], initial_text="Just text.")
    assert body == "Just text."
    assert "There is no quote to remove." in _text(session)


def test_a_reply_says_how_to_trim_its_quote():
    _, session = _line_edit(["/unquote", "Hi.", "/done"], initial_text=_QUOTE)
    assert "/unquote removes the quote; /delete N-M removes some of its lines." in _text(session)
    _, plain = _line_edit(["Hi.", "/done"])
    assert "/unquote removes" not in _text(plain)


def test_help_lists_the_new_commands_and_the_fullscreen_editor():
    _, session = _line_edit(["/help", "x", "/done"])
    text = _text(session)
    assert "/delete N-M" in text
    assert "/unquote" in text
    assert FULLSCREEN_EDITOR_HINT in text


def test_the_line_editor_points_to_the_fullscreen_editor_when_it_opens():
    _, session = _line_edit(["x", "/done"])
    assert "Profile > Fullscreen editor" in _text(session)


# -- Ctrl+E in the fullscreen editor -------------------------------------


def test_ctrl_e_erases_everything_after_a_yes(tmp_path):
    result, _ = _edit(["CTRL+E", "y", "new", "CTRL+O"], tmp_path, initial_text="old bio\nsecond line")
    assert result == "new"


def test_ctrl_e_answered_no_keeps_the_text(tmp_path):
    result, _ = _edit(["CTRL+E", "n", "CTRL+O"], tmp_path, initial_text="keep me")
    assert result == "keep me"


def test_ctrl_y_brings_erased_text_back(tmp_path):
    result, _ = _edit(["CTRL+E", "y", "CTRL+Y", "CTRL+O"], tmp_path, initial_text="one\ntwo")
    assert result == "one\ntwo"


def test_ctrl_y_brings_back_a_text_erased_at_the_size_limit(tmp_path):
    text = "a" * 20 + "\n" + "b" * 19
    result, _ = _edit(["CTRL+E", "y", "CTRL+Y", "CTRL+O"], tmp_path, initial_text=text, max_bytes=len(text))
    assert result == text


def test_ctrl_y_restores_a_quote_with_its_blank_line_once(tmp_path):
    text = "lena_h wrote:\n> Hi\n"
    result, _ = _edit(["CTRL+E", "y", "CTRL+Y", "CTRL+O"], tmp_path, initial_text=text)
    assert result == text


def test_ctrl_e_is_in_the_help(tmp_path):
    _, session = _edit(["CTRL+G", " ", "CTRL+O"], tmp_path, initial_text="x")
    assert "Ctrl+E" in "".join(session.written)
