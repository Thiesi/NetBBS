"""Composition limits are checked where the text is typed (issue #812).

The mail audit (#803) found a 229-byte subject accepted at its prompt and
refused only at Send -- after the body was written -- as "subject cannot
exceed 200 bytes". A caller counts characters, and with accents 200 bytes
is as few as 100 of them. `read_subject` checks at the prompt and says
how many characters to remove; `characters_over` is the arithmetic.
Mail's own flow is covered in tests/test_mail_flow.py; this file covers
the shared pieces and the board composer that uses them too.
"""

from __future__ import annotations

import asyncio
import re

from netbbs.auth.users import create_user
from netbbs.boards import posts as posts_module
from netbbs.boards.boards import create_board
from netbbs.boards.posts import list_posts_page
from netbbs.net.board_flow import _show_board
from netbbs.net.char_input import InputCancelled
from netbbs.net.composition import characters_over, edit_line_body, read_subject, too_long_message
from netbbs.storage.database import Database

ESC = object()
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class FakeSession:
    def __init__(self, lines=(), keys=()):
        self._lines = iter(lines)
        self._keys = iter(keys)
        self.written: list[str] = []
        self.seeded: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.node_display_name = "NetBBS"
        self.node_name_gradient = None
        self.peer_address = "203.0.113.5"

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_key(self, echo: bool = True) -> str:
        key = next(self._keys, None)
        if key is None:
            raise AssertionError("no more scripted keys")
        return key

    async def read_line(self, echo: bool = True, **kwargs) -> str:
        self.seeded.append(kwargs.get("initial", ""))
        line = next(self._lines, None)
        if line is None:
            raise AssertionError("no more scripted lines")
        if line is ESC:
            assert kwargs.get("cancellable"), "Esc pressed at a prompt that does not accept it"
            raise InputCancelled()
        return line

    @property
    def text(self) -> str:
        return _ANSI.sub("", "".join(self.written))


# -- the arithmetic ------------------------------------------------------


def test_characters_over_is_zero_when_it_fits():
    assert characters_over("x" * 200, 200) == 0


def test_characters_over_counts_characters_not_bytes():
    assert characters_over("x" * 229, 200) == 29
    # 150 two-byte letters are 300 bytes: 50 of them have to go.
    assert characters_over("é" * 150, 200) == 50
    # A three-byte character straddling the limit is removed whole.
    assert characters_over("x" * 199 + "中", 200) == 1


def test_too_long_message_never_mentions_bytes():
    assert too_long_message("That subject is", 1) == "That subject is 1 character too long"
    assert too_long_message("That subject is", 2) == "That subject is 2 characters too long"


# -- read_subject --------------------------------------------------------


def _read(session, **kwargs):
    return asyncio.run(read_subject(session, max_bytes=10, **kwargs))


def test_an_empty_subject_is_asked_for_again():
    session = FakeSession(["", "  ", "Hello"])
    assert _read(session) == "Hello"
    assert session.text.count("A subject is required -- type one, or press Esc to cancel.") == 2


def test_esc_on_a_fresh_subject_cancels():
    session = FakeSession(["", ESC])
    assert _read(session) is None


def test_a_long_subject_is_refused_and_reopened_for_shortening():
    session = FakeSession(["abcdefghijkl", "abcdefghij"])
    assert _read(session) == "abcdefghij"
    assert "That subject is 2 characters too long -- shorten it, or press Esc to cancel." in session.text
    assert "bytes" not in session.text
    assert session.seeded == ["", "abcdefghijkl"]


def test_a_prefilled_subject_keeps_its_value_on_esc_or_an_emptied_line():
    assert _read(FakeSession([ESC]), current="Re: hi") == "Re: hi"
    assert _read(FakeSession([""]), current="Re: hi") == "Re: hi"


def test_an_empty_stored_subject_is_still_a_prefilled_one():
    """A stored or carried post can have an empty subject (Codex review):
    editing it must hand a string back on Esc, never the fresh prompt's
    `None`, which review would then try to measure."""
    assert _read(FakeSession([ESC]), current="") == ""
    assert _read(FakeSession([""]), current="") == ""
    assert _read(FakeSession(["New"]), current="") == "New"


def test_a_prefilled_subject_refuses_a_long_edit_and_esc_keeps_the_old_one():
    session = FakeSession(["Re: much too long", ESC])
    assert _read(session, current="Re: hi") == "Re: hi"
    assert "press Esc to keep the previous subject" in session.text


def test_blank_cancels_keeps_the_boards_enter_to_cancel():
    session = FakeSession([""])
    assert _read(session, blank_cancels=True) is None
    assert "Subject (or press Enter to cancel): " in session.text
    assert "A subject is required" not in session.text


def test_a_subject_prompt_edits_in_a_one_row_window():
    """So a long subject scrolls instead of wrapping onto a second row
    (issue #546), which is what `read_prefilled_field` already did."""
    captured = {}

    class Recording(FakeSession):
        async def read_line(self, echo: bool = True, **kwargs):
            captured.update(kwargs)
            return await super().read_line(echo, **kwargs)

    _read(Recording(["ok"]))
    assert captured["cancellable"] is True
    assert captured["viewport"]() == 80 - len("Subject: ")


# -- the line editor's own refusal ---------------------------------------


def test_the_line_editor_refuses_growth_in_characters():
    session = FakeSession(["é" * 6, "/done"])
    body = asyncio.run(edit_line_body(session, initial_text="ab", max_bytes=10, max_lines=10))
    assert body == "ab"
    # "ab\n" is 3 bytes and six two-byte letters 12 more: 15 of 10, so
    # three letters have to go.
    assert "That would make the text 3 characters too long." in session.text
    assert "bytes" not in session.text


# -- boards ---------------------------------------------------------------


def test_a_board_post_subject_is_checked_at_its_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr("netbbs.net.board_flow.MAX_SUBJECT_BYTES", 10)
    db = Database(tmp_path / "node.db")
    user = create_user(db, "alice", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=user)
    session = FakeSession(["Much too long", "Short", "Body", "/done"], keys=["p", "p", "b"])

    asyncio.run(_show_board(session, db, board, user))

    assert "That subject is 3 characters too long" in session.text
    assert "Posted" in session.text
    assert [post.subject for post in list_posts_page(db, board, user).posts] == ["Short"]
    db.close()


def test_a_board_post_over_the_limit_is_said_on_review_and_not_published(tmp_path, monkeypatch):
    """A signature added after the editor can carry a post over; review
    says so in characters, and Publish waits for a shorter body."""
    from netbbs.signature import set_signature

    monkeypatch.setattr("netbbs.net.board_flow.MAX_BODY_BYTES", 30)
    db = Database(tmp_path / "node.db")
    user = create_user(db, "alice", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=user)
    set_signature(db, user, "A signature of some length")
    session = FakeSession(["Hello", "Body", "/done"], keys=["p", "p", "c", "b"])

    asyncio.run(_show_board(session, db, board, user))

    assert "The post is" in session.text and "characters too long -- shorten it with [B]ody." in session.text
    assert "bytes" not in session.text
    assert list_posts_page(db, board, user).posts == []
    db.close()
