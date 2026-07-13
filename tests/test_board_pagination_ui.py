"""
Integration tests for the interactive board post-pagination navigation
in netbbs.net.login_flow._show_board (design doc round 30, issue #10)
-- distinct from tests/test_post_pagination.py, which tests
list_posts_page in isolation. These drive the real _show_board loop
with a FakeSession to confirm the Older/Newer/Recent keys actually
navigate correctly and that a single page never renders the whole
board.
"""

from __future__ import annotations

import asyncio

from netbbs.auth.users import create_user
from netbbs.boards import posts as posts_module
from netbbs.boards.boards import create_board
from netbbs.boards.posts import create_post
from netbbs.net.login_flow import _show_board
from netbbs.storage.database import Database

_PAGE_SIZE = posts_module._DEFAULT_PAGE_SIZE


class FakeSession:
    def __init__(self, keys=None, lines=None):
        self._keys = iter(keys or [])
        self._lines = iter(lines or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_key(self, echo: bool = True) -> str:
        # Deliberately raises rather than falling back to "" once
        # scripted keys run out: real transports never return "" from
        # read_key() (Enter/CR/LF are discarded, not returned as a key —
        # see netbbs.net.char_input.read_key), and _show_board no longer
        # treats "" as an implicit "back" the way its old dead code path
        # once did. Silently returning "" forever here would just trade
        # one bug for another -- an under-scripted test hanging in an
        # infinite loop instead of failing clearly. A test that actually
        # needs the loop to end must script an explicit "b".
        key = next(self._keys, None)
        if key is None:
            raise AssertionError("FakeSession.read_key() called with no more scripted keys")
        return key

    async def read_line(self, echo: bool = True) -> str:
        return next(self._lines, "")

    @property
    def output(self) -> str:
        return "".join(self.written)


def _make_board_with_posts(db, count: int, monkeypatch):
    user = create_user(db, "alice", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=user)
    timestamps = iter(f"2026-01-01T00:00:{i:02d}.000000Z" for i in range(count))
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(timestamps))
    for i in range(count):
        create_post(db, board, user, f"Subject {i}", f"Body {i}")
    return board, user


def test_opening_a_multi_page_board_shows_only_the_newest_page(tmp_path, monkeypatch):
    db = Database(tmp_path / "node.db")
    total = _PAGE_SIZE * 3 + 2
    board, user = _make_board_with_posts(db, total, monkeypatch)
    session = FakeSession(keys=["b"])  # view the newest page, then back out

    asyncio.run(_show_board(session, db, board, user))

    # The core acceptance criterion: a bounded number of posts
    # rendered, not the whole board's history. Matched against
    # "Subject {i} --" (the exact post-header separator), not bare
    # "Subject {i}" -- otherwise "Subject 1" would falsely match
    # inside "Subject 10", "Subject 12", etc.
    shown = sum(1 for i in range(total) if f"Subject {i} --" in session.output)
    assert shown == _PAGE_SIZE
    # Specifically the *newest* posts (highest-numbered subjects).
    for i in range(total - _PAGE_SIZE, total):
        assert f"Subject {i} --" in session.output
    for i in range(0, total - _PAGE_SIZE):
        assert f"Subject {i} --" not in session.output
    assert "lder" in session.output  # "[O]lder" offered -- there's more history
    assert "ewer" not in session.output  # already on the newest page
    db.close()


def test_older_key_navigates_to_the_previous_page(tmp_path, monkeypatch):
    db = Database(tmp_path / "node.db")
    total = _PAGE_SIZE * 2
    board, user = _make_board_with_posts(db, total, monkeypatch)
    session = FakeSession(keys=["o", "b"])  # view newest page, go older, then back out

    asyncio.run(_show_board(session, db, board, user))

    # The older page's posts (subjects 0..PAGE_SIZE-1) must appear;
    # confirms "O" actually re-queried and re-rendered, not a no-op.
    for i in range(0, _PAGE_SIZE):
        assert f"Subject {i}" in session.output
    db.close()


def test_recent_key_jumps_straight_back_to_the_newest_page(tmp_path, monkeypatch):
    db = Database(tmp_path / "node.db")
    total = _PAGE_SIZE * 3
    board, user = _make_board_with_posts(db, total, monkeypatch)
    # Page back twice, then jump straight to "recent" -- if this only
    # moved one page forward instead of jumping all the way, the
    # newest subject wouldn't be the last thing rendered.
    session = FakeSession(keys=["o", "o", "r", "b"])

    asyncio.run(_show_board(session, db, board, user))

    output = session.output
    newest_subject_index = output.rfind(f"Subject {total - 1}")
    older_subject_index = output.rfind("Subject 0")
    assert newest_subject_index > older_subject_index
    db.close()


def test_single_page_board_offers_no_older_newer_recent_options(tmp_path, monkeypatch):
    db = Database(tmp_path / "node.db")
    board, user = _make_board_with_posts(db, count=2, monkeypatch=monkeypatch)
    session = FakeSession(keys=["b"])

    asyncio.run(_show_board(session, db, board, user))

    assert "lder" not in session.output  # "[O]lder" -- not shown, nothing to page to
    assert "ewer" not in session.output
    assert "ecent" not in session.output
    assert "ack" in session.output  # "[B]ack" is still always offered
    db.close()


def test_back_choice_exits_without_navigating(tmp_path, monkeypatch):
    db = Database(tmp_path / "node.db")
    total = _PAGE_SIZE * 2
    board, user = _make_board_with_posts(db, total, monkeypatch)
    session = FakeSession(keys=["b"])

    asyncio.run(_show_board(session, db, board, user))

    # Never paged -- only ever the newest page's subjects appear.
    for i in range(0, _PAGE_SIZE):
        assert f"Subject {i}" not in session.output
    db.close()


def test_empty_board_never_enters_the_navigation_loop(tmp_path):
    db = Database(tmp_path / "node.db")
    user = create_user(db, "alice", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=user)
    session = FakeSession()  # no keys queued at all -- read_key must never be called

    asyncio.run(_show_board(session, db, board, user))

    assert "has no posts yet" in session.output
    db.close()
