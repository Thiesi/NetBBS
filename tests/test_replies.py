"""Replying to a post, and quoting in replies (issue #675).

`create_post` took a `parent_post_id` and the Link carried it, but no screen
passed one, so nothing produced a reply, `[N]ew scan`'s "replies to you"
pass never found one, and mail's Reply quoted nothing either.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.activity import unread_replies_to
from netbbs.auth.users import create_user
from netbbs.boards.boards import create_board
from netbbs.boards.posts import create_post, list_posts_page
from netbbs.net import board_flow
from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.session import Session
from netbbs.quoting import MAX_QUOTED_LINES, quote_body, reply_subject
from netbbs.storage.database import Database

_SGR = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


# -- the subject ------------------------------------------------------------------


def test_reply_subject_adds_re_once():
    assert reply_subject("Hello", max_bytes=300) == "Re: Hello"
    assert reply_subject("Re: Hello", max_bytes=300) == "Re: Hello"
    assert reply_subject("RE: Hello", max_bytes=300) == "RE: Hello"
    assert reply_subject("  spaced  ", max_bytes=300) == "Re: spaced"


def test_reply_subject_stays_within_the_limit():
    subject = "ä" * 150  # 300 bytes
    replied = reply_subject(subject, max_bytes=300)
    assert replied.startswith("Re: ")
    assert len(replied.encode("utf-8")) <= 300


# -- the quote --------------------------------------------------------------------


def test_quote_names_the_author_and_quotes_every_line():
    assert quote_body("first line\nsecond line", author="alice") == (
        "alice wrote:\n> first line\n> second line\n"
    )


def test_quote_stops_at_the_signature():
    body = "the point\n-- \nAlice, SysOp of Somewhere"
    assert quote_body(body, author="alice") == "alice wrote:\n> the point\n"


def test_quote_nests_an_earlier_quote_and_keeps_paragraphs():
    body = "bob wrote:\n> the question\n\nthe answer\n\n"
    assert quote_body(body, author="alice") == (
        "alice wrote:\n> bob wrote:\n> > the question\n>\n> the answer\n"
    )


def test_quote_is_bounded():
    body = "\n".join(f"line {i}" for i in range(500))
    quoted = quote_body(body, author="alice").split("\n")
    assert quoted[-2] == "> [...]"
    assert len(quoted) == MAX_QUOTED_LINES + 3  # header, lines, elision, the empty line


def test_nothing_to_quote_is_empty():
    assert quote_body("\n\n-- \nsig", author="alice") == ""


# -- replying on a board -------------------------------------------------------------


class FakeSession(Session):
    def __init__(self, inputs, *, width=80, height=24):
        self._inputs = list(inputs)
        self.written: list[str] = []
        self.read_line_calls: list[dict] = []
        self.terminal_width = width
        self.terminal_height = height
        self.node_display_name = "NetBBS"
        self.peer_address = None

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
        self.read_line_calls.append(kwargs)
        if not self._inputs:
            raise AssertionError("ran out of scripted input (read_line)")
        return self._inputs.pop(0)

    async def read_key(self, echo: bool = True) -> str:
        if not self._inputs:
            raise AssertionError("ran out of scripted input (read_key)")
        return self._inputs.pop(0)

    async def read_editor_key(self, **kwargs) -> EditorKey:
        if not self._inputs:
            raise AssertionError("ran out of scripted input (read_editor_key)")
        raw = self._inputs.pop(0)
        if raw.startswith("CTRL+"):
            return EditorKey(EditorKeyKind.CTRL, char=raw[len("CTRL+"):].lower())
        return EditorKey(EditorKeyKind.CHAR, char=raw)

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError

    def visible(self) -> str:
        return _SGR.sub("", "".join(self.written))


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


def test_a_caller_replies_from_the_reader(db, alice, bob):
    board = create_board(db, "general", creator=alice)
    original = create_post(db, board, alice, "Lunch?", "Anyone for lunch?\n-- \nAlice")
    # Open post 1, [R]eply, keep the subject (Enter), write one line and
    # finish, [P]ost from review, then back out of the list.
    session = FakeSession(["1", "r", "", "Count me in.", "", "p", "b"])
    asyncio.run(board_flow._show_board(session, db, board, bob))

    posts = list_posts_page(db, board, bob).posts
    reply = next(p for p in posts if p.post_id != original.post_id)
    assert reply.subject == "Re: Lunch?"
    assert reply.parent_post_id == original.root_post_id
    assert reply.body == "alice wrote:\n> Anyone for lunch?\n\nCount me in."
    # The subject field opened on "Re: Lunch?".
    assert any(call.get("initial") == "Re: Lunch?" for call in session.read_line_calls)
    # And the author is told: this is the pass [N]ew scan runs.
    assert [p.post_id for p in unread_replies_to(db, alice)] == [reply.post_id]
    # And the caller is told it went out.
    assert "Posted." in session.visible()


def test_a_cancelled_reply_stays_on_the_post(db, alice, bob):
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "Lunch?", "Anyone for lunch?")
    # [R]eply, then /cancel in the line editor: back on the same post, whose
    # [B]ack returns to the list, and [B]ack again leaves.
    session = FakeSession(["1", "r", "", "/cancel", "b", "b"])
    asyncio.run(board_flow._show_board(session, db, board, bob))
    assert "Reply cancelled." in session.visible()
    assert len(list_posts_page(db, board, bob).posts) == 1


def test_reply_is_not_offered_where_the_caller_cannot_post(db, alice):
    board = create_board(db, "general", creator=alice, min_write_level=50)
    create_post(db, board, create_user(db, "sysop", password="hunter2", user_level=100), "News", "Read this.")
    session = FakeSession(["1", "b", "b"])
    asyncio.run(board_flow._show_board(session, db, board, alice))
    assert "[R]eply" not in session.visible()


def test_an_art_post_is_answered_without_a_quote(db, alice, bob):
    board = create_board(db, "art", creator=alice, allow_color=True)
    create_post(db, board, alice, "Sunset", "\x1b[33m###\x1b[0m", layout="art")
    session = FakeSession(["1", "r", "", "Lovely.", "", "p", "b"])
    asyncio.run(board_flow._show_board(session, db, board, bob))
    reply = next(p for p in list_posts_page(db, board, bob).posts if p.subject.startswith("Re:"))
    assert reply.body == "Lovely."


def test_a_color_board_quotes_the_text_without_its_codes(db, alice, bob):
    board = create_board(db, "general", creator=alice, allow_color=True)
    create_post(db, board, alice, "Hi", "|12red\x1b[1m bold")
    session = FakeSession(["1", "r", "", "ok", "", "p", "b"])
    asyncio.run(board_flow._show_board(session, db, board, bob))
    reply = next(p for p in list_posts_page(db, board, bob).posts if p.subject.startswith("Re:"))
    assert reply.body.startswith("alice wrote:\n> red bold\n")


# -- mail --------------------------------------------------------------------------


def test_mail_reply_quotes_the_message(tmp_path):
    from netbbs.mail import list_sent, send_mail
    from netbbs.net.mail_flow import browse_mail
    from netbbs.storage.execution import DatabaseLane
    from tests.test_mail_flow import FakeSession as MailSession

    path = tmp_path / "node.db"
    db = Database(path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    send_mail(db, alice, bob, "Hello", "How are you?\n-- \nAlice")
    session = MailSession(keys=["i", "0", "1", "r", "s", "b", "b", "b"], lines=["", "Fine, thanks.", ""])
    lane = DatabaseLane(path)
    try:
        asyncio.run(browse_mail(session, lane, bob))
    finally:
        lane.close()
    sent = list_sent(db, bob)
    assert sent[0].subject == "Re: Hello"
    assert sent[0].body.startswith("alice wrote:\n> How are you?\n\nFine, thanks.")
    db.close()


# -- review (Codex on #786) ------------------------------------------------------------


def test_quoting_a_huge_body_of_blank_lines_is_quick():
    """A carried body can hold 200,000 blank lines; quoting it must stay
    linear (the front of the list was popped one line at a time)."""
    import time

    started = time.monotonic()
    assert quote_body("\n" * 200_000 + "x", author="a") == "a wrote:\n> x\n"
    assert time.monotonic() - started < 1.0


def test_the_fullscreen_editor_writes_a_reply_like_the_line_editor(db, alice, bob):
    """The blank line between quote and reply survives, whichever editor
    the caller uses."""
    from netbbs.net.editor_preference import set_fullscreen_editor_enabled

    set_fullscreen_editor_enabled(db, bob, True)
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "Lunch?", "Anyone for lunch?")
    session = FakeSession(["1", "r", "", *"Count me in.", "CTRL+O", "p", "b"])
    asyncio.run(board_flow._show_board(session, db, board, bob))
    reply = next(p for p in list_posts_page(db, board, bob).posts if p.subject.startswith("Re:"))
    assert reply.body == "alice wrote:\n> Anyone for lunch?\n\nCount me in."


def test_a_long_quote_opens_on_its_end(tmp_path):
    from netbbs.net.prose_editor import edit_prose

    text = "\n".join(f"quoted line {i}" for i in range(60)) + "\n"
    session = FakeSession(["CTRL+O"], height=20)
    result = asyncio.run(edit_prose(
        session, initial_text=text, draft_path=tmp_path / "d.draft", max_bytes=100_000, cursor_at_end=True,
    ))
    assert result == text + "\n"
    # The renderer places words with cursor moves, not spaces.
    first_screen = session.visible().replace(" ", "")
    assert "quotedline59" in first_screen
    assert "quotedline0" not in first_screen


def test_a_post_removed_while_it_was_read_cannot_be_replied_to(db, alice, bob):
    from netbbs.boards.posts import get_post, tombstone_post
    from netbbs.moderation import BoardPermission, grant_permissions

    board = create_board(db, "general", creator=alice)
    grant_permissions(
        db, alice, object_type="board", object_id=board.id, permissions=BoardPermission.DELETE, granted_by=alice
    )
    post = create_post(db, board, alice, "Lunch?", "Anyone for lunch?")

    class RemovingSession(FakeSession):
        async def read_editor_key(self, **kwargs):
            key = await super().read_editor_key(**kwargs)
            if key.char == "r":
                tombstone_post(db, get_post(db, post.post_id), board, tombstoned_by=alice)
            return key

    # [R]eply is refused and the reader goes back to the list; [B]ack leaves.
    session = RemovingSession(["1", "r", "b"])
    asyncio.run(board_flow._show_board(session, db, board, bob))
    assert "no longer available to reply to" in session.visible()
    assert not any(p.subject.startswith("Re:") for p in list_posts_page(db, board, bob).posts)


def test_a_post_past_its_age_cannot_be_replied_to_before_anyone_lists_the_board(db, alice):
    """Expiry is applied lazily; the reply check sweeps first (Codex review
    on #786)."""
    import datetime

    board = create_board(db, "news", creator=alice, max_post_age_days=30)
    post = create_post(db, board, alice, "Old news", "Stale.")
    assert board_flow._reply_target(db, post, board) is not None
    stamp = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=40)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    db.connection.execute("UPDATE posts SET created_at = ? WHERE id = ?", (stamp, post.id))
    db.connection.commit()
    assert board_flow._reply_target(db, post, board) is None


def test_a_recovered_reply_draft_is_kept_as_saved(tmp_path):
    """No separator is added to a draft the caller saved themselves."""
    from netbbs.net.prose_editor import edit_prose

    draft = tmp_path / "d.draft"
    saved = "alice wrote:\n> Lunch?\n\nI would, but\n"
    draft.write_text(saved, encoding="utf-8")
    session = FakeSession(["y", "CTRL+O"])
    result = asyncio.run(edit_prose(
        session, initial_text="alice wrote:\n> Lunch?\n", draft_path=draft, max_bytes=100_000, cursor_at_end=True,
    ))
    assert result == saved


def test_a_quote_carries_no_control_sequences():
    """A mail body or sender label from another node can hold escape
    sequences; the quote an editor opens on must not (claude review on
    #786)."""
    quoted = quote_body("hi\x1b[2J\x1b]0;owned\x07 there\x07", author="evil\x1b[31m")
    assert "\x1b" not in quoted and "\x07" not in quoted
    assert quoted.startswith("evil[31m wrote:\n> hi")
