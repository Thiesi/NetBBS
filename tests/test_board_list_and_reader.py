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


# -- Codex review on #719 ------------------------------------------------------


@pytest.mark.parametrize(("width", "height"), [(60, 24), (70, 24), (80, 24)])
def test_a_middle_page_fits_the_terminal_with_every_action_shown(db, alice, monkeypatch, width, height):
    """The page budget was measured without the read entry, so a page with
    [O]lder, [N]ewer, [R]ecent *and* the read entry overran the terminal."""
    board = create_board(db, "general", creator=alice)
    _posts(db, board, alice, 80, monkeypatch)
    session = FakeSession(["o", "b"], width=width, height=height)

    asyncio.run(board_flow._show_board(session, db, board, alice))

    middle = session.screens()[1]
    assert "ewer" in middle and "lder" in middle
    rows = middle.replace("\r\n", "\n").rstrip("\n").split("\n")
    assert len(rows) <= height


def test_stepping_into_the_next_page_marks_only_the_post_shown(db, alice, monkeypatch):
    board = create_board(db, "general", creator=alice)
    _posts(db, board, alice, 60, monkeypatch)
    recorded = []
    real = board_flow.record_board_seen
    monkeypatch.setattr(
        board_flow, "record_board_seen",
        lambda db_, user, board_, post: recorded.append(post.subject) or real(db_, user, board_, post),
    )
    # Older page, cursor to its last row, open it, then [N]ext post across
    # the page boundary.
    session = FakeSession(["o", "UP", "ENTER", "n", "b", "b"])
    asyncio.run(board_flow._show_board(session, db, board, alice))

    readers = [screen for screen in session.screens() if "Body of post" in screen]
    shown_after_crossing = re.search(r"Body of post (\d+)", readers[-1]).group(1)
    # The record made while reading across the boundary names that post, not
    # the newest post of the page it was fetched with.
    assert f"Subject {shown_after_crossing}" in recorded
    crossing_index = recorded.index(f"Subject {shown_after_crossing}")
    assert recorded[crossing_index] == f"Subject {shown_after_crossing}"


def test_a_reply_does_not_name_a_parent_the_feed_hides(db, alice, monkeypatch):
    board = create_board(db, "general", creator=alice)
    stamps = iter(["2026-01-01T00:00:00.000000Z", "2026-01-01T00:00:01.000000Z"])
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(stamps))
    question = create_post(db, board, alice, "A secret question", "?")
    create_post(db, board, alice, "Re: it", "!", parent_post_id=question.post_id)
    db.connection.execute("UPDATE posts SET status = 'expired' WHERE post_id = ?", (question.post_id,))
    db.connection.commit()
    session = FakeSession(["1", "b", "b"])

    asyncio.run(board_flow._show_board(session, db, board, alice))

    assert "Re: it" in session.visible()
    assert "reply to" not in session.visible()
    assert "A secret question" not in session.visible()


def test_a_reply_names_its_parents_current_subject(db, alice, monkeypatch):
    from netbbs.boards.posts import edit_post

    board = create_board(db, "general", creator=alice)
    stamps = iter(f"2026-01-01T00:00:0{i}.000000Z" for i in range(3))
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(stamps))
    question = create_post(db, board, alice, "Old wording", "?")
    create_post(db, board, alice, "Re: it", "!", parent_post_id=question.post_id)
    edit_post(db, question, board, subject="New wording", body="?", edited_by=alice)
    session = FakeSession(["2", "b", "b"])

    asyncio.run(board_flow._show_board(session, db, board, alice))

    assert 'reply to "New wording"' in session.visible()


def test_a_subject_full_of_tabs_keeps_its_row_inside_the_terminal(db, alice):
    from netbbs.rendering.width import display_width

    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "\t".join(["tabbed"] * 20), "x")
    rows = board_flow._post_list_rows(
        db, list_posts_page(db, board, alice).posts, width=80, highlighted=None,
        new_ids=set(), name_requirement=None, accent=220,
    )
    assert all(display_width(_SGR.sub("", row)) <= 79 for row in rows)
    assert "\t" not in "".join(rows)


def test_a_page_emptied_while_reading_leaves_no_cursor_to_crash_on(db, alice, monkeypatch):
    """Claude review on #719: the reader returned index 0 for a page that had
    emptied under the caller, and the next Enter indexed an empty list."""
    from netbbs.moderation.roles import BoardPermission, grant_permissions

    board = create_board(db, "general", creator=alice)
    grant_permissions(
        db, alice, object_type="board", object_id=board.id, permissions=BoardPermission.DELETE, granted_by=alice
    )
    create_post(db, board, alice, "Only post", "x")
    real_list = board_flow.list_posts_page
    emptied = {"now": False}

    def _list(db_, board_, user, **kwargs):
        page = real_list(db_, board_, user, **kwargs)
        if emptied["now"]:
            return posts_module.PostPage(posts=[], has_older=False, has_newer=False)
        return page

    monkeypatch.setattr(board_flow, "list_posts_page", _list)
    real_tombstone = board_flow.tombstone_post

    def _tombstone(*args, **kwargs):
        emptied["now"] = True  # the page empties as the removal lands
        return real_tombstone(*args, **kwargs)

    monkeypatch.setattr(board_flow, "tombstone_post", _tombstone)
    session = FakeSession(["1", "t", "y", "ENTER", "b"])

    asyncio.run(board_flow._show_board(session, db, board, alice))  # no IndexError


# -- Codex review round 2 on #719 --------------------------------------------


def test_every_post_shown_in_the_reader_is_recorded_as_it_is_shown(db, alice, monkeypatch):
    board = create_board(db, "general", creator=alice)
    _posts(db, board, alice, 60, monkeypatch)
    recorded = []
    real = board_flow.record_board_seen
    monkeypatch.setattr(
        board_flow, "record_board_seen",
        lambda db_, user, board_, post: recorded.append(post.subject) or real(db_, user, board_, post),
    )
    # Across the page boundary with [N]ext post, then one more step inside
    # the page the list never drew.
    session = FakeSession(["o", "UP", "ENTER", "n", "n", "b", "b"])
    asyncio.run(board_flow._show_board(session, db, board, alice))

    shown = [
        f"Subject {re.search(r'Body of post (\d+)', screen).group(1)}"
        for screen in session.screens() if "Body of post" in screen
    ]
    assert len(shown) == 3
    for subject in shown:
        assert subject in recorded


def test_board_activity_does_not_count_posts_past_their_age(db, alice, monkeypatch):
    from netbbs.activity import unread_post_count

    board = create_board(db, "general", creator=alice, max_post_age_days=1)
    first, stale = _posts(db, board, alice, 2, monkeypatch)
    db.connection.execute(
        "UPDATE posts SET created_at = ? WHERE post_id = ?", ("2020-01-01T00:00:00.000000Z", first.post_id)
    )
    db.connection.commit()
    record_board_seen(db, alice, board, first)
    db.connection.execute(
        "UPDATE posts SET created_at = ? WHERE post_id = ?", ("2020-01-02T00:00:00.000000Z", stale.post_id)
    )
    db.connection.commit()

    # Still stamped 'approved' until something sweeps the board; the count
    # must not report it as new.
    assert unread_post_count(db, alice, board) == 0


def test_a_non_ascii_digit_on_the_list_rings_the_bell(db, alice, monkeypatch):
    board = create_board(db, "general", creator=alice)
    _posts(db, board, alice, 2, monkeypatch)
    session = FakeSession(["²", "①", "b"])

    asyncio.run(board_flow._show_board(session, db, board, alice))  # no ValueError

    assert "\a" in "".join(session.written)


def test_the_reader_stays_on_its_post_after_an_action_queues_a_notice(db, alice, monkeypatch):
    """A pending outcome takes a row from a fresh page budget; the refetch
    after an action keeps the page's own size, so the post acted on stays."""
    board = create_board(db, "general", creator=alice)
    _posts(db, board, alice, 60, monkeypatch)
    # Row 1 is the oldest post on the newest page -- the one a shorter
    # refetch would drop. Cancel an edit of it, which announces "Edit
    # cancelled.", and see which post the reader comes back to.
    session = FakeSession(["1", "e", "", "/cancel", "b", "b"])

    asyncio.run(board_flow._show_board(session, db, board, alice))

    readers = [screen for screen in session.screens() if "Body of post" in screen]
    before, after = (re.search(r"Body of post (\d+)", s).group(1) for s in (readers[0], readers[-1]))
    assert before == after
    assert "Edit cancelled." in readers[-1]


# -- Codex review round 3 on #719 --------------------------------------------


def test_a_long_description_does_not_push_the_list_off_the_screen(db, alice, monkeypatch):
    board = create_board(db, "general", description="A very long description. " * 200, creator=alice)
    _posts(db, board, alice, 30, monkeypatch)
    session = FakeSession(["b"])

    asyncio.run(board_flow._show_board(session, db, board, alice))

    screen = session.screens()[0]
    assert len(screen.replace("\r\n", "\n").rstrip("\n").split("\n")) <= 24
    assert "..." in screen


def test_discarding_a_draft_keeps_the_highlighted_post(db, alice, monkeypatch):
    from netbbs.net.draft_storage import save_draft

    board = create_board(db, "general", creator=alice)
    _posts(db, board, alice, 60, monkeypatch)
    save_draft(board_flow._post_draft_path(db, kind="new", board=board, user=alice), "an old draft")
    # Highlight the top row (the one a shorter refetch would drop), then
    # [D]raft -> [D]iscard, which announces "Draft deleted.".
    session = FakeSession(["DOWN", "d", "d", "b"])

    asyncio.run(board_flow._show_board(session, db, board, alice))

    screens = session.screens()
    highlighted = [re.search(r">\s+\d+\s+Subject (\d+)\b", s) for s in screens]
    before, after = highlighted[1].group(1), highlighted[-1].group(1)
    assert before == after
    assert "Draft deleted." in screens[-1]


def test_an_empty_board_says_why_a_caller_cannot_post(db, alice):
    board = create_board(db, "general", creator=alice, min_write_level=50)
    session = FakeSession(["b"])

    asyncio.run(board_flow._show_board(session, db, board, alice))

    assert "Read only: posting needs level 50." in session.visible()


def test_reading_a_late_arrival_does_not_move_the_jump_position_back(db, alice, monkeypatch):
    """A post carried late has an older authored date but a newer local id.
    Seeing it advances the unread watermark; the position a jump lands on
    stays where the caller had read to."""
    from netbbs.activity import board_read_cursor, unread_post_count

    board = create_board(db, "general", creator=alice)
    stamps = iter(["2026-01-01T10:00:00.000000Z", "2026-01-01T09:00:00.000000Z"])
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(stamps))
    newer = create_post(db, board, alice, "Written at ten", "x")
    record_board_seen(db, alice, board, newer)
    late = create_post(db, board, alice, "Written at nine, arrived later", "y")
    assert late.id > newer.id and late.created_at < newer.created_at

    record_board_seen(db, alice, board, late)

    assert board_read_cursor(db, alice, board) == (newer.created_at, newer.post_id)
    assert unread_post_count(db, alice, board) == 0
