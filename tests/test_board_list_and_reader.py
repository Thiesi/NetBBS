"""The board post list and the one-post reader (issue #679).

A board page is a list, one row per post, sized to the terminal, with a
cursor; a post is read one at a time on `show_detail`, where its actions are.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.activity import record_board_seen
from netbbs.auth.users import create_user
from netbbs.boards import posts as posts_module
from netbbs.boards.boards import create_board
from netbbs.boards.posts import create_post, list_posts_page
from netbbs.communities import create_community
from netbbs.net import board_flow
from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.redraw_preference import set_redraw_in_place_enabled
from netbbs.net.session import Session
from netbbs.storage.database import Database

_SGR = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_CLEAR = "\x1b[2J"
_KINDS = {
    "UP": EditorKeyKind.UP,
    "DOWN": EditorKeyKind.DOWN,
    "ENTER": EditorKeyKind.ENTER,
    "PGDN": EditorKeyKind.PAGE_DOWN,
}


class FakeSession(Session):
    def __init__(self, inputs, *, width=80, height=24):
        self._inputs = list(inputs)
        self.written: list[str] = []
        self.terminal_width = width
        self.terminal_height = height
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
        if raw in _KINDS:
            return EditorKey(_KINDS[raw])
        if raw.startswith("CTRL+"):
            return EditorKey(EditorKeyKind.CTRL, char=raw[len("CTRL+"):].lower())
        return EditorKey(EditorKeyKind.CHAR, char=raw)

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError

    def screens(self) -> list[str]:
        """Each redraw-in-place screen, as visible text."""
        return [_SGR.sub("", part) for part in "".join(self.written).split(_CLEAR) if part.strip()]

    def visible(self) -> str:
        return _SGR.sub("", "".join(self.written))


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


def _posts(db, board, author, count, monkeypatch):
    stamps = iter(f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}.000000Z" for i in range(count))
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(stamps))
    return [create_post(db, board, author, f"Subject {i}", f"Body of post {i}") for i in range(count)]


def _listed(screen: str) -> list[int]:
    return [int(n) for n in re.findall(r"Subject (\d+)\b", screen)]


# -- the list ----------------------------------------------------------------


@pytest.mark.parametrize(("width", "height"), [(80, 24), (80, 40), (100, 30)])
def test_the_list_fills_the_terminal_without_scrolling_it(db, alice, monkeypatch, width, height):
    """As many rows as fit: a taller terminal lists more, and the screen
    never runs past its own height (the old page of five full posts did)."""
    board = create_board(db, "general", creator=alice)
    _posts(db, board, alice, 60, monkeypatch)
    session = FakeSession(["b"], width=width, height=height)

    asyncio.run(board_flow._show_board(session, db, board, alice))

    screen = session.screens()[0]
    rows = screen.replace("\r\n", "\n").rstrip("\n").split("\n")
    assert len(rows) <= height
    listed = _listed(screen)
    assert listed == list(range(60 - len(listed), 60))
    assert len(listed) >= 5


def test_a_taller_terminal_lists_more_posts(db, alice, monkeypatch):
    board = create_board(db, "general", creator=alice)
    _posts(db, board, alice, 60, monkeypatch)
    short = FakeSession(["b"], height=24)
    tall = FakeSession(["b"], height=48)
    asyncio.run(board_flow._show_board(short, db, board, alice))
    asyncio.run(board_flow._show_board(tall, db, board, alice))
    assert len(_listed(tall.screens()[0])) > len(_listed(short.screens()[0]))


def test_down_and_enter_open_the_highlighted_post(db, alice, monkeypatch):
    board = create_board(db, "general", creator=alice)
    _posts(db, board, alice, 3, monkeypatch)
    session = FakeSession(["DOWN", "DOWN", "ENTER", "b", "b"])

    asyncio.run(board_flow._show_board(session, db, board, alice))

    reader = next(screen for screen in session.screens() if "Body of post" in screen)
    assert "Body of post 1" in reader  # oldest first: row 2 is Subject 1
    assert "Subject 1" in reader.split("\n")[0] + reader.split("\n")[1]


def test_a_number_opens_its_post_and_back_returns_with_the_cursor_on_it(db, alice, monkeypatch):
    board = create_board(db, "general", creator=alice)
    _posts(db, board, alice, 3, monkeypatch)
    session = FakeSession(["3", "b", "b"])

    asyncio.run(board_flow._show_board(session, db, board, alice))

    screens = session.screens()
    assert "Body of post 2" in screens[1]
    assert re.search(r">\s+3\s+Subject 2\b", screens[2])


def test_ctrl_h_shows_the_lists_keys(db, alice, monkeypatch):
    board = create_board(db, "general", creator=alice)
    _posts(db, board, alice, 1, monkeypatch)
    session = FakeSession(["CTRL+h", " ", "b"])

    asyncio.run(board_flow._show_board(session, db, board, alice))

    assert "Message board keys" in session.visible()
    assert "read the highlighted post" in session.visible()


# -- the reader --------------------------------------------------------------


def test_next_post_steps_into_the_newer_page(db, alice, monkeypatch):
    """Next and previous cross page boundaries instead of stopping at them."""
    board = create_board(db, "general", creator=alice)
    _posts(db, board, alice, 40, monkeypatch)
    # Older page first, so the last row of that page has a newer neighbour
    # on the page after it.
    session = FakeSession(["o", "b"])
    asyncio.run(board_flow._show_board(session, db, board, alice))
    older = _listed(session.screens()[1])
    last_on_older = max(older)

    session = FakeSession(["o", str(len(older)) if len(older) < 10 else "UP", *(["ENTER"] if len(older) >= 10 else []), "n", "b", "b"])
    asyncio.run(board_flow._show_board(session, db, board, alice))

    readers = [screen for screen in session.screens() if "Body of post" in screen]
    assert f"Body of post {last_on_older}\r" in readers[0] or f"Body of post {last_on_older}\n" in readers[0]
    assert f"Body of post {last_on_older + 1}" in readers[1]


def test_a_long_post_pages_under_its_title(db, alice):
    board = create_board(db, "general", creator=alice)
    body = "\n\n".join(f"Paragraph {i}: " + "words " * 60 for i in range(12))
    create_post(db, board, alice, "Long one", body)
    session = FakeSession(["1", "PGDN", "b", "b"])

    asyncio.run(board_flow._show_board(session, db, board, alice))

    first, second = [screen for screen in session.screens() if "Long one" in screen and "Paragraph" in screen][:2]
    assert "Paragraph 0" in first and "Paragraph 11" not in first
    assert "Page 1 of" in first and "Page 2 of" in second
    assert "Long one" in second  # the title stays on every page


def test_the_byline_says_whose_post_it_answers(db, alice, monkeypatch):
    board = create_board(db, "general", creator=alice)
    # Distinct timestamps, so the reply is row 2 however fast the clock ticks.
    stamps = iter(["2026-01-01T00:00:00.000000Z", "2026-01-01T00:00:01.000000Z"])
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(stamps))
    question = create_post(db, board, alice, "A question", "?")
    create_post(db, board, alice, "Re: A question", "!", parent_post_id=question.post_id)
    session = FakeSession(["2", "b", "b"])

    asyncio.run(board_flow._show_board(session, db, board, alice))

    assert 'reply to "A question"' in session.visible()


# -- what the list tells a caller ----------------------------------------------


def test_posts_new_since_the_last_visit_are_marked_on_this_visit(db, alice, monkeypatch):
    board = create_board(db, "general", creator=alice)
    posts = _posts(db, board, alice, 4, monkeypatch)
    record_board_seen(db, alice, board, list_posts_page(db, board, alice).posts[1])  # seen up to Subject 1
    session = FakeSession(["b"])

    asyncio.run(board_flow._show_board(session, db, board, alice))

    screen = session.screens()[0]
    rows = {n: line for line in screen.split("\n") for n in _listed(line)}
    assert " new " in rows[2] and " new " in rows[3]
    assert " new " not in rows[0] and " new " not in rows[1]
    assert "2 new" in screen


def test_a_caller_who_can_only_read_is_told_why(db, alice, monkeypatch):
    board = create_board(db, "general", creator=alice, min_write_level=50)
    poster = create_user(db, "poster", password="hunter2", user_level=60)
    _posts(db, board, poster, 1, monkeypatch)
    session = FakeSession(["b"])

    asyncio.run(board_flow._show_board(session, db, board, alice))

    assert "Read only: posting needs level 50." in session.visible()
    assert "[P]ost" not in session.visible()


def test_the_board_description_is_shown(db, alice):
    board = create_board(db, "general", description="Anything goes here.", creator=alice)
    create_post(db, board, alice, "Hi", "x")
    session = FakeSession(["b"])

    asyncio.run(board_flow._show_board(session, db, board, alice))

    assert "Anything goes here." in session.visible()


def test_a_linked_board_this_node_originated_says_linked(db, alice):
    from netbbs.link.boards import LinkContext, link_board
    from netbbs.link.node_identity import bootstrap_node_identity
    from netbbs.link.protocol import LinkNode

    identity = bootstrap_node_identity("roanoke")
    board = create_board(db, "general", creator=alice)
    link_board(db, board, node_identity=identity)
    create_post(db, board, alice, "Hi", "x")
    session = FakeSession(["b"])

    asyncio.run(board_flow._show_board(
        session, db, board, alice, link_context=LinkContext(link_node=LinkNode(identity=identity))
    ))

    assert "Newest posts · Linked" in session.visible() or "Newest posts - Linked" in session.visible()


def test_the_breadcrumb_carries_the_community_the_caller_came_through(db, alice, monkeypatch):
    community = create_community(db, "Retro", creator=alice)
    board = create_board(db, "hardware", creator=alice, community_id=community.id)
    create_post(db, board, alice, "Hi", "x")
    session = FakeSession(["0", "1", "b", "b"])

    asyncio.run(board_flow._browse_boards(
        session, db, alice, community_id=community.id, community_scoped=True, title_prefix="Retro",
    ))

    assert "NetBBS › Retro › Message boards › hardware" in session.visible()


# -- the board picker ----------------------------------------------------------


def test_the_board_list_shows_activity_and_linked_and_gate_notes(db, alice):
    from netbbs.link.boards import link_board
    from netbbs.link.node_identity import bootstrap_node_identity

    busy = create_board(db, "Busy", creator=alice)
    quiet = create_board(db, "Quiet", creator=alice)
    create_board(db, "Unvisited", creator=alice, name_requirement="verified")
    seen = create_post(db, busy, alice, "a", "b")
    record_board_seen(db, alice, busy, seen)
    create_post(db, busy, alice, "c", "d")
    create_post(db, busy, alice, "e", "f")
    last = create_post(db, quiet, alice, "x", "y")
    record_board_seen(db, alice, quiet, last)
    link_board(db, busy, node_identity=bootstrap_node_identity("roanoke"))
    session = FakeSession(["b"])

    asyncio.run(board_flow._browse_boards(session, db, alice))

    text = session.visible()
    assert "ACTIVITY" in text
    assert re.search(r"Busy\s+2 new\s+\[LINK\]", text)
    assert re.search(r"Quiet\s+caught up", text)
    assert re.search(r"Unvisited\s+not visited yet\s+needs verification", text)
