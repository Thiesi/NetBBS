"""Outcomes carried into the next redraw, and screens that wait for [B]ack
(issue #680, from the message boards audit #674).

With redraw-in-place on, a line written just before a screen redraws is
erased by that redraw's clear. Every test here turns the preference on and
asserts that the outcome appears *after* the last clear -- on the screen the
caller actually lands on -- rather than merely somewhere in the output.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.auth.users import create_user
from netbbs.boards.boards import create_board
from netbbs.boards.posts import create_post
from netbbs.net import board_flow, mail_flow
from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.notices import announce, pending_notices, take_notices
from netbbs.net.picker import pick_item
from netbbs.net.redraw_preference import set_redraw_in_place_enabled
from netbbs.net.session import Session
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

_CLEAR = "\x1b[2J"
_SGR = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class FakeSession(Session):
    """One scripted queue for keys, lines and editor keys."""

    def __init__(self, inputs):
        self._inputs = list(inputs)
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.node_display_name = "NetBBS"
        self.peer_address = None

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
        if not self._inputs:
            raise AssertionError("ran out of scripted input (read_line)")
        return self._inputs.pop(0)

    async def read_key(self, echo: bool = True) -> str:
        if not self._inputs:
            raise AssertionError("ran out of scripted input (read_key)")
        return self._inputs.pop(0)

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False) -> EditorKey:
        if not self._inputs:
            raise AssertionError("ran out of scripted input (read_editor_key)")
        raw = self._inputs.pop(0)
        if raw == "ENTER":
            return EditorKey(EditorKeyKind.ENTER)
        return EditorKey(EditorKeyKind.CHAR, char=raw)

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError

    def after_last_clear(self) -> str:
        """Visible text of the screen currently on the terminal."""
        text = "".join(self.written)
        return re.sub(r"\s+", " ", _SGR.sub("", text[text.rfind(_CLEAR):]))

    def visible(self) -> str:
        return re.sub(r"\s+", " ", _SGR.sub("", "".join(self.written)))


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    user = create_user(db, "alice", password="hunter2", user_level=10)
    set_redraw_in_place_enabled(db, user, True)
    return user


def _stays_on_screen_until_the_next_prompt(session, marker):
    """`marker` was written, and no clear came between it and the next
    `Choice:` prompt -- so the caller is looking at it when asked what to
    do next. Returns that screen's visible text, from its clear on."""
    text = "".join(session.written)
    index = text.rfind(marker)
    assert index != -1, f"{marker!r} never written"
    rest = text[index:]
    prompt = rest.find("Choice:")
    assert prompt != -1, f"no prompt followed {marker!r}"
    assert _CLEAR not in rest[:prompt], f"{marker!r} was cleared before the next prompt"
    start = text.rfind(_CLEAR, 0, index)
    return re.sub(r"\s+", " ", _SGR.sub("", text[start:index + prompt]))


# -- boards ------------------------------------------------------------------


def test_a_board_outcome_is_shown_on_the_redrawn_page(db, alice):
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "Subject", "Body")
    session = FakeSession(["1", "e", "", "/edit 1", "Revised", "", "s", "b", "b"])

    asyncio.run(board_flow._show_board(session, db, board, alice))

    # An edit returns to the post it edited (issue #679's reader), and the
    # outcome is on that screen, right above its prompt -- not on the
    # review screen the reader replaced.
    screen = _stays_on_screen_until_the_next_prompt(session, "Post updated.")
    assert "[E]dit" in screen
    assert pending_notices(session) == []


def test_a_refused_post_is_reported_on_the_review_screen_it_returns_to(db, alice, monkeypatch):
    """A post the signature carries over the length limit (issue #812:
    over-long subjects are refused at their own prompt now, so this is
    the refusal left for review to report)."""
    from netbbs.signature import set_signature

    monkeypatch.setattr(board_flow, "MAX_BODY_BYTES", 30)
    set_signature(db, alice, "A signature of some length")
    board = create_board(db, "general", creator=alice)
    session = FakeSession(["p", "Hello", "Body", "", "p", "c", "b"])

    asyncio.run(board_flow._show_board(session, db, board, alice))

    screen = _stays_on_screen_until_the_next_prompt(session, "characters too long")
    assert "Review composition" in screen


def test_an_empty_board_a_caller_cannot_post_to_waits_for_back(db, alice):
    """It used to draw its empty state and return straight into the board
    list's redraw, so the screen flashed and vanished."""
    board = create_board(db, "general", creator=alice, min_write_level=100)
    session = FakeSession(["x", "b"])

    asyncio.run(board_flow._show_board(session, db, board, alice))

    text = session.visible()
    assert "This message board has no posts yet" in text
    assert "[B]ack" in text
    assert "[P]ost" not in text
    assert session._inputs == []  # it read a key -- the stray "x" -- and then [B]ack


def test_the_remove_action_uses_one_verb_throughout(db, alice):
    from netbbs.moderation.roles import BoardPermission, grant_permissions

    board = create_board(db, "general", creator=alice)
    grant_permissions(
        db, alice, object_type="board", object_id=board.id, permissions=BoardPermission.DELETE, granted_by=alice
    )
    create_post(db, board, alice, "Subject", "Body")
    session = FakeSession(["1", "t", "y", "b", "b"])

    asyncio.run(board_flow._show_board(session, db, board, alice))

    text = session.visible()
    assert "Remove pos[t]" in text
    assert 'Remove "Subject"?' in text
    assert "ombstone" not in text


# -- the picker ----------------------------------------------------------------


def _pick(session):
    return pick_item(
        session, ["one", "two"], name_of=str, stable_id_of=lambda item: 1 if item == "one" else 2,
        title="Files", empty_message="Nothing here.", redraw_in_place=True,
    )


def test_a_picker_shows_an_announced_outcome_above_its_list(db, alice):
    session = FakeSession(["b"])
    announce(session, "Sent 'game.zip'.")

    asyncio.run(_pick(session))

    assert "Sent 'game.zip'." in session.visible()
    assert take_notices(session) == []


def test_a_picker_with_nothing_announced_draws_as_before(db, alice):
    plain = FakeSession(["b"])
    asyncio.run(_pick(plain))
    assert "Sent" not in plain.visible()


# -- mail ----------------------------------------------------------------------


def test_a_refused_mail_is_reported_on_the_review_screen_it_returns_to(db, alice, monkeypatch):
    import netbbs.mail

    create_user(db, "bob", password="hunter2", user_level=10)
    monkeypatch.setattr(netbbs.mail, "MAX_MAIL_PER_RECIPIENT", 0)
    lane = DatabaseLane(db.path)
    try:
        session = FakeSession(["bob", "Subject", "Body", "", "s", "c"])
        asyncio.run(mail_flow._compose_mail(session, lane, alice))
    finally:
        lane.close()

    screen = _stays_on_screen_until_the_next_prompt(session, "mailbox is full")
    assert "Review composition" in screen


# -- SysOp console: shared queue and stand-in sessions -------------------------


def test_a_flow_given_the_consoles_stand_in_announces_to_the_real_session(db, alice):
    from netbbs.net.admin_flow import _TrailingOutput, _take_notices

    real = FakeSession([])
    announce(_TrailingOutput(real), "Sent 'game.zip'.")

    assert [_SGR.sub("", line) for line in _take_notices(real)] == ["Sent 'game.zip'."]


# -- file areas ----------------------------------------------------------------


def test_an_empty_file_area_a_caller_cannot_use_waits_for_back(db, alice):
    """Same flash-and-vanish as the empty board: no upload right, nothing
    of theirs waiting, no Link catalogue -- and it returned at once."""
    from netbbs.files.areas import create_file_area
    from netbbs.net.file_flow import _show_area

    area = create_file_area(db, "downloads", creator=alice, min_write_level=100)
    lane = DatabaseLane(db.path)
    try:
        session = FakeSession(["x", "b"])
        asyncio.run(_show_area(session, lane, area, alice))
    finally:
        lane.close()

    text = session.visible()
    assert "This file area has no files yet" in text
    assert "[B]ack" in text
    assert session._inputs == []


# -- menus that flows unwind back to (Codex review on #701) ------------------


def test_the_main_menu_shows_an_outcome_a_flow_unwound_back_to_it(db, alice):
    """A download whose browser link was the whole of the transfer returns
    through the file area and the area list to the main menu; its clear
    would otherwise erase the link."""
    from netbbs.chat.mailbox import MessageMailbox
    from netbbs.net.main_menu import _draw_main_menu

    session = FakeSession([])
    announce(session, "Open this in a browser to download 'game.zip': https://example.test/t/abc")

    asyncio.run(_draw_main_menu(session, db, MessageMailbox(), alice))

    screen = session.after_last_clear()
    assert "https://example.test/t/abc" in screen
    assert screen.index("https://example.test/t/abc") < screen.rindex("Choice:")
    assert pending_notices(session) == []


def test_message_sent_is_shown_on_the_mail_menu_it_returns_to(db, alice):
    create_user(db, "bob", password="hunter2", user_level=10)
    lane = DatabaseLane(db.path)
    try:
        session = FakeSession(["c", "bob", "Subject", "Body", "", "s", "b"])
        asyncio.run(mail_flow.browse_mail(session, lane, alice))
    finally:
        lane.close()

    _stays_on_screen_until_the_next_prompt(session, "Message sent.")


def test_an_empty_inbox_says_so_on_the_mail_menu(db, alice):
    """The picker has nothing to pick and returns at once; its message goes
    to the menu it returns to rather than under that menu's clear."""
    lane = DatabaseLane(db.path)
    try:
        session = FakeSession(["i", "b"])
        asyncio.run(mail_flow.browse_mail(session, lane, alice))
    finally:
        lane.close()

    _stays_on_screen_until_the_next_prompt(session, "Your inbox is empty.")


def test_a_stray_key_on_the_empty_board_does_not_redraw_it(db):
    """Claude review on #701: without redraw-in-place, redrawing the screen
    per unrecognized key stacks copies of it."""
    bob = create_user(db, "bob", password="hunter2", user_level=10)  # redraw-in-place off
    board = create_board(db, "general", creator=bob, min_write_level=100)
    session = FakeSession(["x", "y", "b"])

    asyncio.run(board_flow._show_board(session, db, board, bob))

    assert session.visible().count("This message board has no posts yet") == 1
