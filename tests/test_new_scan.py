"""
Tests for issue #56's `[N]ew scan` activity summary --
`netbbs.net.main_menu._draw_main_menu`'s always-shown entry and
`netbbs.net.scan_and_find._new_scan_screen` itself: never-visited/caught-up/unread status per
board/channel/file area, the cross-board "replies to you" section, and
jumping straight to the first unread post/file. Channel-entry wiring
(`browse_channels`'s own `initial_channel` parameter) is proved for real
in tests/test_chat_flow_join.py -- this file only checks that `[N]ew
scan` calls into it with the right channel, via monkeypatch, since the
chat-capable `FakeSession` there can't script `pick_item`'s digit-based
selection the way this file's simpler session can.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.auth.users import create_user
from netbbs.boards import posts as posts_module
from netbbs.boards.boards import create_board
from netbbs.boards.posts import create_post
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.files.areas import create_file_area
from netbbs.files.entries import upload_file
from netbbs.net import scan_and_find
from netbbs.net.char_input import InputHistory
from netbbs.net.main_menu import _draw_main_menu, _main_menu
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


class FakeSession:
    def __init__(self, inputs: list[str] | None = None):
        self._inputs = list(inputs or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.node_display_name = "NetBBS"
        self.node_name_gradient = None
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
        if not self._inputs:
            raise AssertionError("FakeSession ran out of scripted input (read_line)")
        return self._inputs.pop(0)

    async def read_key(self, echo: bool = True) -> str:
        if not self._inputs:
            raise AssertionError("FakeSession ran out of scripted input (read_key)")
        return self._inputs.pop(0)


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


def _visible_text(session: FakeSession) -> str:
    return _ANSI_ESCAPE_RE.sub("", _written_text(session))


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


def _run_main_menu(db, lane, user, keys):
    session = FakeSession(keys)
    asyncio.run(
        _main_menu(
            session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), user, lane=lane
        )
    )
    return session


# -- menu visibility ----------------------------------------------------


def test_new_scan_is_always_shown_regardless_of_level(db, alice):
    session = FakeSession()
    asyncio.run(_draw_main_menu(session, db, MessageMailbox(), alice))
    assert "[N]ew scan" in _visible_text(session)


def test_new_scan_is_not_available_without_a_lane(db, alice):
    session = FakeSession(["n", "l", "y"])
    asyncio.run(
        _main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), alice)
    )
    assert "New scan is not available in this context." in _written_text(session)


# -- unread status: never-visited / caught-up / unread -----------------


def test_new_scan_shows_never_visited_for_an_unvisited_board(db, lane, alice):
    other = create_user(db, "bob", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=other)
    create_post(db, board, other, "hello", "world")

    session = _run_main_menu(db, lane, alice, ["n", "b", "b", "l", "y"])

    assert "not yet visited" in _written_text(session)


def test_new_scan_shows_caught_up_once_the_board_has_been_visited(db, lane, alice, monkeypatch):
    other = create_user(db, "bob", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=other)
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: "2026-01-01T00:00:00.000000Z")
    create_post(db, board, other, "hello", "world")

    # First visit: pick the board (only item, "01"), back out of it, back to main menu.
    _run_main_menu(db, lane, alice, ["n", "0", "1", "b", "b", "l", "y"])
    # Second new scan: now caught up.
    session = _run_main_menu(db, lane, alice, ["n", "b", "l", "y"])

    assert "caught up" in _written_text(session)
    assert "not yet visited" not in _written_text(session)


def test_new_scan_shows_unread_count_for_new_activity(db, lane, alice, monkeypatch):
    other = create_user(db, "bob", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=other)
    timestamps = iter([f"2026-01-01T00:00:0{i}.000000Z" for i in range(2)])
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(timestamps))
    create_post(db, board, other, "first", "1")

    _run_main_menu(db, lane, alice, ["n", "0", "1", "b", "b", "l", "y"])  # visit once, catch up
    create_post(db, board, other, "second", "2")  # new activity after the visit

    session = _run_main_menu(db, lane, alice, ["n", "b", "l", "y"])

    assert "1 unread" in _written_text(session)


# -- replies to you -------------------------------------------------------


def test_new_scan_shows_replies_to_you(db, lane, alice, monkeypatch):
    board = create_board(db, "general", creator=alice)
    timestamps = iter([f"2026-01-01T00:00:0{i}.000000Z" for i in range(2)])
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(timestamps))
    alices_post = create_post(db, board, alice, "question", "how do I do X?")
    other = create_user(db, "bob", password="hunter2", user_level=10)
    create_post(db, board, other, "Re: question", "like this", parent_post_id=alices_post.post_id)

    session = _run_main_menu(db, lane, alice, ["n", "b", "l", "y"])

    assert "Replies to you: 1" in _written_text(session)
    assert "Re: question" in _written_text(session)


def test_new_scan_shows_no_replies_when_there_are_none(db, lane, alice):
    create_board(db, "general", creator=alice)
    session = _run_main_menu(db, lane, alice, ["n", "b", "l", "y"])
    assert "Replies to you: none." in _written_text(session)


# -- jump to first unread -------------------------------------------------


def test_selecting_a_board_jumps_to_the_first_unread_post(db, lane, alice, monkeypatch):
    from netbbs.activity import ensure_board_baseline, record_post_opened

    other = create_user(db, "bob", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=other)
    ensure_board_baseline(db, alice, board)
    timestamps = iter([f"2026-01-01T00:00:0{i}.000000Z" for i in range(2)])
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(timestamps))
    first = create_post(db, board, other, "first", "1")
    create_post(db, board, other, "second", "2")

    record_post_opened(db, alice, board, first)

    # New scan, board 01, Back to the scan (issue #839), Back, log off.
    session = _run_main_menu(db, lane, alice, ["n", "0", "1", "b", "b", "l", "y"])

    # The board opens as its post list (issue #679) on the page an ordinary
    # visit shows, read posts included and numbered as they always are
    # (issue #839), with the list's cursor on the first unread post.
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", _written_text(session))
    assert re.search(r">\s+2\s+second\b", text)
    assert re.search(r"\b1\s+first\b", text)


def test_selecting_a_file_area_jumps_to_the_first_unread_file(db, lane, alice, monkeypatch):
    from netbbs.activity import record_file_area_seen
    from netbbs.files import entries as entries_module

    other = create_user(db, "bob", password="hunter2", user_level=10)
    area = create_file_area(db, "downloads", creator=other)
    timestamps = iter([f"2026-01-01T00:00:0{i}.000000Z" for i in range(2)])
    monkeypatch.setattr(entries_module, "utc_now_iso", lambda: next(timestamps))
    first = upload_file(db, area, other, "a.txt", b"hello")
    upload_file(db, area, other, "b.txt", b"world")

    record_file_area_seen(db, alice, area, first)

    session = _run_main_menu(db, lane, alice, ["n", "0", "1", "b", "b", "l", "y"])

    # The ordinary newest page, the cursor on the first unseen file (#839).
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", _written_text(session))
    assert "a.txt" in text
    assert re.search(r">.*b\.txt", text)


# -- channel dispatch (proved for real in tests/test_chat_flow_join.py) -----


def test_selecting_a_channel_calls_browse_channels_with_that_channel(db, lane, alice, monkeypatch):
    channel = create_channel(db, "lobby", creator=alice)

    calls = []

    async def fake_browse_channels(session, lane, hub, presence, mailbox, history, user, **kwargs):
        calls.append(kwargs.get("initial_channel"))

    # Patched on scan_and_find, not login_flow -- _new_scan_screen (the
    # actual call site) lives there now, with its own independent
    # `browse_channels` import binding (netbbs.net.login_flow's own
    # binding is a different reference to the same underlying function,
    # unaffected by patching the other module's copy).
    # Patched on scan_and_find, not login_flow -- _new_scan_screen (the
    # actual call site) lives there now, with its own independent
    # `browse_channels` import binding (netbbs.net.login_flow's own
    # binding is a different reference to the same underlying function,
    # unaffected by patching the other module's copy).
    monkeypatch.setattr(scan_and_find, "browse_channels", fake_browse_channels)

    _run_main_menu(db, lane, alice, ["n", "0", "1", "b", "l", "y"])

    assert len(calls) == 1
    assert calls[0].id == channel.id


def test_new_scan_rows_print_no_reference_number(db, lane, alice):
    """`pick_item` used to print each row's stable id as a `(#N)`
    reference, and `id(item)` is about fifteen digits -- at 40 columns
    that prefix plus an ordinary name consumed the whole row (issue
    #541). Issue #838 removed the reference from every picker; a row
    shows only the number that selects it."""
    import re

    other = create_user(db, "bob", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=other)
    create_post(db, board, other, "hello", "world")

    session = _run_main_menu(db, lane, alice, ["n", "b", "b", "l", "y"])
    text = _written_text(session)

    assert re.search(r"01\. ", text)
    assert "(#" not in text


# -- issue #710: [M]ark read -------------------------------------------------


def test_mark_read_counts_a_boards_posts_read_without_entering_it(db, lane, alice, monkeypatch):
    from netbbs.activity import ensure_board_baseline, unread_post_count

    other = create_user(db, "bob", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=other)
    ensure_board_baseline(db, alice, board)
    timestamps = iter([f"2026-01-01T00:00:0{i}.000000Z" for i in range(2)])
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(timestamps))
    create_post(db, board, other, "first", "1")
    create_post(db, board, other, "second", "2")

    # Nothing highlighted on this session, so [M] asks which row.
    session = _run_main_menu(db, lane, alice, ["n", "m", "1", "b", "l", "y"])

    text = _visible_text(session)
    assert "[M]ark read" in text
    assert "general: every post marked read." in text
    assert "caught up" in text
    assert unread_post_count(db, alice, board) == 0


def test_mark_read_says_a_channel_cannot_be_marked(db, lane, alice):
    create_channel(db, "lobby", creator=alice)

    session = _run_main_menu(db, lane, alice, ["n", "m", "1", "b", "l", "y"])

    assert "Only a message board can be marked read here." in _visible_text(session)


def test_mark_read_brings_the_replies_summary_up_to_date(db, lane, alice, monkeypatch):
    board = create_board(db, "general", creator=alice)
    timestamps = iter([f"2026-01-01T00:00:0{i}.000000Z" for i in range(2)])
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(timestamps))
    alices_post = create_post(db, board, alice, "question", "how do I do X?")
    other = create_user(db, "bob", password="hunter2", user_level=10)
    create_post(db, board, other, "Re: question", "like this", parent_post_id=alices_post.post_id)

    session = _run_main_menu(db, lane, alice, ["n", "m", "1", "b", "l", "y"])

    text = _visible_text(session)
    marked = text.index("general: every post marked read.")
    assert "Replies to you: 1" in text[:marked]
    assert "Replies to you: none." in text[text.rindex("Replies to you", 0, marked):]


def test_mark_read_keeps_each_row_where_it_was(db, lane, alice, monkeypatch):
    """Activity can reorder the boards while [M]ark read reloads them; the
    rows stay where they were, so a row's number still names the same board."""
    from netbbs.activity import ensure_board_baseline

    first = create_board(db, "first-board", creator=alice)
    second = create_board(db, "second-board", creator=alice)
    for board in (first, second):
        ensure_board_baseline(db, alice, board)
    real = scan_and_find.list_boards
    calls = {"n": 0}

    def _reordering(database):
        calls["n"] += 1
        boards = real(database)
        return boards if calls["n"] == 1 else list(reversed(boards))

    monkeypatch.setattr(scan_and_find, "list_boards", _reordering)
    session = _run_main_menu(db, lane, alice, ["n", "m", "1", "0", "1", "b", "b", "l", "y"])

    text = _visible_text(session)
    marked = re.search(r"(\S+-board): every post marked read\.", text).group(1)
    # Row 1 is opened after the reload: the board just marked, not the other.
    assert f"Message boards › {marked}" in text or f"Message boards > {marked}" in text


def test_with_nothing_to_list_the_replies_summary_still_shows(db, lane, alice):
    session = _run_main_menu(db, lane, alice, ["n", "l", "y"])

    text = _visible_text(session)
    assert "Nothing accessible yet." in text
    assert "Replies to you: none." in text


# -- following (issue #675) ------------------------------------------------------


def test_follow_from_new_scan_lists_the_row_first(db, lane, alice):
    from netbbs.activity import is_following

    # Boards are listed before file areas, so the file area is row 2
    # whatever order two same-millisecond boards would have come in.
    create_board(db, "aardvarks", creator=alice)
    area = create_file_area(db, "zebras", creator=alice)
    session = _run_main_menu(db, lane, alice, ["n", "f", "2", "b", "l", "y"])
    assert "Following zebras: it is listed first here." in _visible_text(session)
    assert is_following(db, alice, "file_area", area.id)

    session = _run_main_menu(db, lane, alice, ["n", "b", "l", "y"])
    text = _visible_text(session)
    assert text.index("zebras") < text.index("aardvarks")
    assert "* file area" in text


def test_view_followed_narrows_the_list_and_back(db, lane, alice):
    from netbbs.activity import follow

    create_board(db, "aardvarks", creator=alice)
    area = create_file_area(db, "zebras", creator=alice)
    follow(db, alice, "file_area", area.id)
    session = _run_main_menu(db, lane, alice, ["n", "v", "v", "b", "l", "y"])
    text = _visible_text(session)
    # Each outcome is written above the list it redrew.
    following_only = text.split("Showing what you follow.")[1].split("Showing everything.")[0]
    everything = text.split("Showing everything.")[1]
    assert "zebras" in following_only and "aardvarks" not in following_only
    assert "zebras" in everything and "aardvarks" in everything


def test_view_followed_with_nothing_followed_says_so(db, lane, alice):
    create_board(db, "aardvarks", creator=alice)
    session = _run_main_menu(db, lane, alice, ["n", "v", "b", "l", "y"])
    assert "You follow nothing yet" in _visible_text(session)


def test_a_board_is_followed_from_its_own_list(db, lane, alice):
    from netbbs.activity import is_following
    from netbbs.net.board_flow import _show_board

    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "hello", "world")
    session = FakeSession(["f", "f", "b"])
    asyncio.run(_show_board(session, db, board, alice))
    text = _visible_text(session)
    assert "Following this board: New scan lists it first." in text
    assert "No longer following this board." in text
    assert "Un[f]ollow" in text
    assert not is_following(db, alice, "board", board.id)


def test_a_file_area_is_followed_from_its_own_screen(db, lane, alice):
    from netbbs.activity import is_following
    from netbbs.net.file_flow import _show_area

    area = create_file_area(db, "downloads", creator=alice)
    upload_file(db, area, alice, "a.txt", b"a")
    session = FakeSession(["f", "b"])
    asyncio.run(_show_area(session, lane, area, alice))
    assert "Following this file area: New scan lists it first." in _visible_text(session)
    assert is_following(db, alice, "file_area", area.id)


def test_an_empty_board_can_be_followed(db, lane, alice):
    from netbbs.activity import is_following
    from netbbs.net.board_flow import _show_board

    board = create_board(db, "general", creator=alice)
    session = FakeSession(["f", "b"])
    asyncio.run(_show_board(session, db, board, alice))
    text = _visible_text(session)
    assert "Following this board: New scan lists it first." in text
    assert "Un[f]ollow" in text  # the redrawn bar
    assert is_following(db, alice, "board", board.id)


def test_an_empty_file_area_can_be_followed(db, lane, alice):
    from netbbs.activity import is_following
    from netbbs.net.file_flow import _show_area

    area = create_file_area(db, "downloads", creator=alice)
    session = FakeSession(["f", "b"])
    asyncio.run(_show_area(session, lane, area, alice))
    text = _visible_text(session)
    assert "Following this file area: New scan lists it first." in text
    assert "Un[f]ollow" in text
    assert is_following(db, alice, "file_area", area.id)


def test_unfollowing_in_the_followed_view_keeps_the_row_there(db, lane, alice):
    """The view keeps the rows it was switched on with, so the list does
    not change under the highlight (Codex review on #788)."""
    from netbbs.activity import follow, is_following

    create_board(db, "aardvarks", creator=alice)
    area = create_file_area(db, "zebras", creator=alice)
    follow(db, alice, "file_area", area.id)
    # [V]iew followed, then [F]ollow row 1 (zebras, the only one) off.
    session = _run_main_menu(db, lane, alice, ["n", "v", "f", "1", "b", "l", "y"])
    text = _visible_text(session)
    after = text.split("No longer following zebras.")[1]
    assert "zebras" in after and "aardvarks" not in after.split("Choice")[0]
    assert not is_following(db, alice, "file_area", area.id)


# -- walking the scan (issue #839) ---------------------------------------------


def test_an_unvisited_board_says_how_much_it_holds(db, lane, alice):
    other = create_user(db, "bob", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=other)
    for subject in ("one", "two", "three"):
        create_post(db, board, other, subject, "x")

    session = _run_main_menu(db, lane, alice, ["n", "b", "l", "y"])

    assert "not yet visited, 3 posts" in _visible_text(session)


def test_back_from_a_board_returns_to_the_scan_on_the_next_with_something_new(db, lane, alice):
    from netbbs.net.char_input import EditorKey, EditorKeyKind

    other = create_user(db, "bob", password="hunter2", user_level=10)
    for name in ("Pens", "Inks"):
        create_post(db, create_board(db, name, creator=other), other, f"about {name}", "x")

    class _EnterSession(FakeSession):
        """Enter as the picker reads it from a real terminal."""

        async def read_editor_key(self, **kwargs):
            key = await self.read_key()
            return EditorKey(EditorKeyKind.ENTER) if key == "\r" else EditorKey(EditorKeyKind.CHAR, char=key)

    # New scan, board 01, Back: the scan again, its cursor on the other
    # board, so Enter opens it; Back, Back out of the scan, log off.
    session = _EnterSession(["n", "0", "1", "b", "\r", "b", "b", "l", "y"])
    asyncio.run(
        _main_menu(
            session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), alice, lane=lane
        )
    )
    text = _visible_text(session)
    second = "Inks" if "01. Pens" in text else "Pens"

    assert f"Next with something new: {second}. Enter opens it." in text
    assert text.count("NetBBS \u203a New scan") >= 3  # the scan came back after each board
    assert f"about {second}" in text  # Enter opened the next board
    assert "Nothing else is new." in text


def test_replies_to_you_can_be_opened_from_the_scan(db, lane, alice, monkeypatch):
    board = create_board(db, "general", creator=alice)
    timestamps = iter([f"2026-01-01T00:00:0{i}.000000Z" for i in range(2)])
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(timestamps))
    question = create_post(db, board, alice, "question", "how do I do X?")
    other = create_user(db, "bob", password="hunter2", user_level=10)
    create_post(db, board, other, "Re: question", "like this", parent_post_id=question.post_id)

    # New scan, [R]eplies, reply 01: its board opens with the cursor on it.
    session = _run_main_menu(db, lane, alice, ["n", "r", "0", "1", "b", "b", "l", "y"])
    text = _visible_text(session)

    assert "Replies to you" in text and "[R]eplies" in text
    assert re.search(r">\s+\d+\s+Re: question\b", text)
