"""The fullscreen editor's line cut and paste, word delete, quote rewrap
and size counter (issue #815), and the keys each transport delivers for
them."""

from __future__ import annotations

import asyncio

import aiohttp

from netbbs.net.char_input import EditorKey, EditorKeyKind, read_editor_key
from netbbs.net.prose_editor import _size_in_characters, edit_prose
from netbbs.net.session import Session
from netbbs.net.web import WebServer
from netbbs.quoting import quote_body
from netbbs.rendering.prose_buffer import ProseBuffer, rewrap_quote
from netbbs.rendering.width import display_width

from tests.test_char_input import FakeByteSource
from tests.test_prose_editor import FakeSession as _BaseFakeSession
from tests.test_prose_editor import _type, _written_text


class FakeSession(_BaseFakeSession):
    """`WORD_BACKSPACE` scripts Alt+Backspace; the rest as the base."""

    async def read_editor_key(self) -> EditorKey:
        if self._inputs and self._inputs[0] == "WORD_BACKSPACE":
            self._inputs.pop(0)
            return EditorKey(EditorKeyKind.WORD_BACKSPACE)
        return await super().read_editor_key()


def _edit(inputs: list[str], tmp_path, *, initial_text: str | None = None, max_bytes: int = 100_000, **kwargs):
    async def scenario():
        session = FakeSession(inputs, **kwargs)
        result = await edit_prose(session, initial_text=initial_text, draft_path=tmp_path / "d.draft", max_bytes=max_bytes)
        return result, session

    return asyncio.run(scenario())


# -- cut and paste -------------------------------------------------------


def test_ctrl_k_cuts_the_line_and_ctrl_y_pastes_it_elsewhere(tmp_path):
    result, _ = _edit(["CTRL+K", "DOWN", "CTRL+Y", "CTRL+O"], tmp_path, initial_text="one\ntwo\nthree")
    assert result == "two\none\nthree"


def test_ctrl_k_pressed_again_cuts_the_next_line_too(tmp_path):
    result, _ = _edit(["CTRL+K", "CTRL+K", "DOWN", "CTRL+Y", "CTRL+O"], tmp_path, initial_text="a\nb\nc\nd")
    assert result == "c\na\nb\nd"


def test_a_cut_after_another_key_starts_afresh(tmp_path):
    result, _ = _edit(["CTRL+K", "DOWN", "CTRL+K", "CTRL+Y", "CTRL+O"], tmp_path, initial_text="a\nb\nc")
    # The second cut replaced "a" with "c"; pasting gives back "c" only.
    assert result == "b\nc\n"


def test_the_cut_lines_can_be_pasted_more_than_once(tmp_path):
    result, _ = _edit(["CTRL+K", "CTRL+Y", "CTRL+Y", "CTRL+O"], tmp_path, initial_text="x\ny")
    assert result == "x\nx\ny"


def test_cutting_the_last_line_empties_it(tmp_path):
    result, _ = _edit(["DOWN", "CTRL+K", "CTRL+O"], tmp_path, initial_text="keep\ngone")
    assert result == "keep\n"


def test_ctrl_k_on_the_empty_last_line_does_nothing(tmp_path):
    result, _ = _edit(["CTRL+K", "CTRL+X"], tmp_path)
    # Nothing changed, so Ctrl-X leaves without asking.
    assert result is None


def test_ctrl_y_with_nothing_cut_rings_the_bell(tmp_path):
    result, session = _edit(["CTRL+Y", "CTRL+O"], tmp_path, initial_text="hi")
    assert result == "hi"
    assert "\a" in _written_text(session)


def test_a_paste_past_the_limit_is_refused_whole(tmp_path):
    result, session = _edit(["CTRL+K", "CTRL+Y", "CTRL+Y", "CTRL+O"], tmp_path, initial_text="abcd\nz", max_bytes=8)
    # The first paste puts the 6 bytes back; a second would make 11.
    assert result == "abcd\nz"
    assert "\a" in _written_text(session)


def test_mid_line_paste_splits_the_line(tmp_path):
    result, _ = _edit(["CTRL+K", "RIGHT", "CTRL+Y", "CTRL+O"], tmp_path, initial_text="cut\nab")
    assert result == "a" + "cut\n" + "b"


# -- word delete ------------------------------------------------------------


def test_ctrl_w_deletes_the_word_before_the_cursor(tmp_path):
    result, _ = _edit(_type("hello big world") + ["CTRL+W", "CTRL+O"], tmp_path)
    assert result == "hello big "


def test_ctrl_w_takes_the_spaces_before_the_cursor_with_the_word(tmp_path):
    result, _ = _edit(_type("hello big   ") + ["CTRL+W", "CTRL+O"], tmp_path)
    assert result == "hello "


def test_ctrl_w_mid_line_keeps_the_text_after_the_cursor(tmp_path):
    result, _ = _edit(["END", "LEFT", "LEFT", "CTRL+W", "CTRL+O"], tmp_path, initial_text="one two three")
    assert result == "one two ee"


def test_ctrl_w_at_the_start_of_a_line_joins_it_to_the_one_above(tmp_path):
    result, _ = _edit(["DOWN", "CTRL+W", "CTRL+O"], tmp_path, initial_text="up\ndown")
    assert result == "updown"


def test_alt_backspace_deletes_a_word_too(tmp_path):
    result, _ = _edit(_type("keep this") + ["WORD_BACKSPACE", "CTRL+O"], tmp_path)
    assert result == "keep "


# -- rewrapping a quote --------------------------------------------------------

_LONG = "This is a long quoted line that the original author wrote as a single paragraph " * 3


def test_ctrl_r_rewraps_the_quoted_paragraph_under_the_cursor(tmp_path):
    quote = quote_body(_LONG + "\n\nSecond paragraph.", author="bob")
    result, _ = _edit(["DOWN", "CTRL+R", "CTRL+O"], tmp_path, initial_text=quote)
    lines = result.split("\n")
    assert lines[0] == "bob wrote:"
    rewrapped = lines[1 : lines.index(">")]
    assert len(rewrapped) > 1
    assert all(line.startswith("> ") for line in rewrapped)
    assert all(display_width(line) <= 72 for line in rewrapped)
    assert " ".join(line[2:] for line in rewrapped) == _LONG.strip()
    # The next paragraph is untouched.
    assert lines[lines.index(">") + 1] == "> Second paragraph."


def test_ctrl_r_rewraps_to_a_narrow_screen(tmp_path):
    quote = quote_body(_LONG, author="bob")
    result, _ = _edit(["DOWN", "CTRL+R", "CTRL+O"], tmp_path, initial_text=quote, width=40)
    assert all(display_width(line) <= 40 for line in result.split("\n"))


def test_ctrl_r_leaves_the_cursor_at_the_end_of_the_paragraph(tmp_path):
    result, _ = _edit(["DOWN", "CTRL+R"] + _type("!") + ["CTRL+O"], tmp_path, initial_text="bob wrote:\n> a\n> b\n\nme")
    assert result == "bob wrote:\n> a b!\n\nme"


def test_ctrl_r_off_a_quote_rings_the_bell_and_changes_nothing(tmp_path):
    result, session = _edit(["CTRL+R", "CTRL+O"], tmp_path, initial_text="plain\ntext")
    assert result == "plain\ntext"
    assert "\a" in _written_text(session)


def test_a_rewrap_past_the_limit_is_refused(tmp_path):
    text = "> " + "word " * 30
    result, session = _edit(["CTRL+R", "CTRL+O"], tmp_path, initial_text=text, max_bytes=len(text))
    assert result == text
    assert "\a" in _written_text(session)


def test_rewrap_quote_keeps_a_nested_quote_apart():
    lines = ["> > deep one", "> > deep two", "> shallow", "> shallow too"]
    assert rewrap_quote(lines, 0, 72) == (0, 2, ["> > deep one deep two"])
    assert rewrap_quote(lines, 3, 72) == (2, 4, ["> shallow shallow too"])


def test_rewrap_quote_counts_depth_not_spelling():
    assert rewrap_quote([">> a", "> > b"], 0, 72) == (0, 2, [">> a b"])


def test_rewrap_quote_stops_at_a_separator_and_at_the_elision_mark():
    lines = ["> one", ">", "> two", "> [...]"]
    assert rewrap_quote(lines, 0, 72) == (0, 1, ["> one"])
    assert rewrap_quote(lines, 2, 72) == (2, 3, ["> two"])
    assert rewrap_quote(lines, 1, 72) is None
    assert rewrap_quote(lines, 3, 72) is None


def test_rewrap_quote_keeps_a_long_url_whole():
    url = "https://example.org/" + "x" * 80
    new_lines = rewrap_quote([f"> see {url} here"], 0, 40)[2]
    assert f"> {url}" in new_lines


def test_rewrap_quote_ignores_an_unquoted_line():
    assert rewrap_quote(["hello"], 0, 72) is None


# -- buffer operations ----------------------------------------------------------


def test_insert_text_with_line_breaks_leaves_the_cursor_after_it():
    buffer = ProseBuffer.from_text("ab")
    buffer.cursor_col = 1
    buffer.insert_text("X\nY")
    assert buffer.lines == ["aX", "Yb"]
    assert (buffer.cursor_line, buffer.cursor_col) == (1, 1)


# -- the size counter ------------------------------------------------------


def test_the_status_line_counts_characters_against_the_limit(tmp_path):
    _, session = _edit(_type("hello") + ["CTRL+O"], tmp_path, max_bytes=100)
    assert "5/100" in _written_text(session)


def test_the_counter_is_in_characters_not_bytes(tmp_path):
    _, session = _edit(_type("éé") + ["CTRL+O"], tmp_path, max_bytes=100)
    # Two characters used; the four bytes they take leave room for 96
    # plain letters more, so 98 in all -- never "4/100".
    assert "2/98" in _written_text(session)
    assert "4/100" not in _written_text(session)


def test_size_in_characters_over_the_limit():
    # A text handed in over its limit (a signature appended, say) reports
    # the limit as the characters that fit.
    assert _size_in_characters("abcdef", 4) == (6, 4)
    assert _size_in_characters("", 10) == (0, 10)


def test_at_the_limit_the_counter_says_so_ahead_of_the_key_hints(tmp_path):
    _, session = _edit(["CTRL+O"], tmp_path, initial_text="abc", max_bytes=3)
    assert "3/3 AT LENGTH LIMIT  Ctrl+O save" in _written_text(session)


def test_the_counter_survives_the_narrowest_screen(tmp_path):
    _, session = _edit(["CTRL+O"], tmp_path, initial_text="x" * 1234, max_bytes=16_000, width=40, height=12)
    assert "1234/16000" in _written_text(session)


# -- help ---------------------------------------------------------------------


def test_ctrl_g_help_lists_the_new_keys(tmp_path):
    _, session = _edit(["CTRL+G", " ", "CTRL+O"], tmp_path, initial_text="x")
    text = _written_text(session)
    for key in ("Ctrl+W", "Alt+Backspace", "Ctrl+K", "Ctrl+Y", "Ctrl+R", "characters used/limit"):
        assert key in text


def test_a_cut_is_autosaved(tmp_path):
    async def scenario():
        session = FakeSession(["CTRL+K"])
        draft = tmp_path / "d.draft"
        task = asyncio.create_task(
            edit_prose(session, initial_text="a\nb", draft_path=draft, max_bytes=100, autosave_interval_seconds=0.01)
        )
        for _ in range(200):
            await asyncio.sleep(0.01)
            if draft.exists():
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return draft.read_text(encoding="utf-8")

    assert asyncio.run(scenario()) == "b"


# -- what the transports deliver ----------------------------------------------


def test_telnet_and_ssh_deliver_the_ctrl_keys():
    async def scenario():
        source = FakeByteSource(bytes([0x0B, 0x19, 0x17, 0x12]))
        return [await read_editor_key(source) for _ in range(4)]

    keys = asyncio.run(scenario())
    assert [(k.kind, k.char) for k in keys] == [(EditorKeyKind.CTRL, c) for c in "kywr"]


def test_telnet_and_ssh_deliver_alt_backspace_as_a_word_delete():
    async def scenario():
        source = FakeByteSource(b"\x1b\x7f\x1b\x08x")
        return [await read_editor_key(source) for _ in range(3)]

    keys = asyncio.run(scenario())
    assert [k.kind for k in keys] == [EditorKeyKind.WORD_BACKSPACE, EditorKeyKind.WORD_BACKSPACE, EditorKeyKind.CHAR]


def test_a_lone_escape_is_still_an_escape():
    async def scenario():
        return await read_editor_key(FakeByteSource(b"\x1b"))

    assert asyncio.run(scenario()).kind == EditorKeyKind.ESCAPE


def test_the_web_client_delivers_alt_backspace_and_the_ctrl_keys():
    received: list[EditorKey] = []

    async def handler(session: Session):
        for _ in range(5):
            received.append(await session.read_editor_key())
        await session.write_line("done")

    async def scenario():
        server = WebServer(host="127.0.0.1", port=0, session_handler=handler)
        await server.start()
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws") as ws:
                    # xterm.js sends Alt+Backspace as ESC DEL in one event.
                    for data in ("\x1b\x7f", "\x0b", "\x19", "\x17", "\x12"):
                        await ws.send_json({"type": "key", "data": data})
                    while True:
                        msg = await ws.receive_json(timeout=2)
                        if "done" in msg.get("data", ""):
                            break
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received[0].kind == EditorKeyKind.WORD_BACKSPACE
    assert [(k.kind, k.char) for k in received[1:]] == [(EditorKeyKind.CTRL, c) for c in "kywr"]
