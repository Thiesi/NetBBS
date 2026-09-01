"""
Integration tests for the post-draft save/resume feature (dogfood
feature request, issue #149): `/exit`/`/quit` (line editor) and "Keep
draft & exit" (fullscreen editor) leave the in-progress post body on
disk instead of discarding it, and `_show_board` proactively offers to
[E]dit/[D]elete/[I]gnore a saved draft the next time the caller enters
that same board -- before the ordinary post list/navigation flow.

Same `FakeSession` shape as tests/test_login_flow_fullscreen_editor.py
(a single ordered input queue serves read_key/read_line/
read_editor_key alike), reused here rather than imported, matching
this codebase's existing per-file convention.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.boards.boards import create_board
from netbbs.boards.posts import list_posts_page
from netbbs.net import board_flow
from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.editor_preference import set_fullscreen_editor_enabled
from netbbs.net.session import Session
from netbbs.storage.database import Database

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
    def __init__(self, inputs: list[str] | None = None):
        self._inputs = list(inputs or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.node_display_name = "NetBBS"
        self.terminal_height = 24
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

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False) -> EditorKey:
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


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


# -- line editor: /exit and /quit -------------------------------------------


def test_exit_from_the_line_editor_saves_a_draft_and_does_not_post(db, alice):
    board = create_board(db, "general", creator=alice)
    session = FakeSession(["p", "Subject", "first line", "/exit", "b"])
    asyncio.run(board_flow._show_board(session, db, board, alice))
    text = _written_text(session)
    assert "Draft saved" in text
    assert "Posted" not in text
    assert list_posts_page(db, board, alice).posts == []


def test_quit_from_the_line_editor_also_saves_a_draft(db, alice):
    board = create_board(db, "general", creator=alice)
    session = FakeSession(["p", "Subject", "first line", "/quit", "b"])
    asyncio.run(board_flow._show_board(session, db, board, alice))
    assert "Draft saved" in _written_text(session)


def test_cancel_from_the_line_editor_does_not_leave_a_draft(db, alice):
    board = create_board(db, "general", creator=alice)
    session = FakeSession(["p", "Subject", "first line", "/cancel", "b"])
    asyncio.run(board_flow._show_board(session, db, board, alice))
    session2 = FakeSession(["b", ""])
    asyncio.run(board_flow._show_board(session2, db, board, alice))
    assert "saved post draft" not in _written_text(session2)


# -- fullscreen editor: "Keep draft & exit" ---------------------------------


def test_keep_draft_from_the_fullscreen_editor_saves_and_does_not_post(db, alice):
    set_fullscreen_editor_enabled(db, alice, True)
    board = create_board(db, "general", creator=alice)
    session = FakeSession(["p", "Subject"] + _type("typed body") + ["CTRL+X", "k", "b"])
    asyncio.run(board_flow._show_board(session, db, board, alice))
    text = _written_text(session)
    assert "Draft saved" in text
    assert "Posted" not in text
    assert list_posts_page(db, board, alice).posts == []


# -- board-entry recovery prompt --------------------------------------------


def test_reentering_the_board_offers_edit_delete_or_ignore(db, alice):
    board = create_board(db, "general", creator=alice)
    exit_session = FakeSession(["p", "Subject", "saved body", "/exit", "b"])
    asyncio.run(board_flow._show_board(exit_session, db, board, alice))

    reentry_session = FakeSession(["i", "b", ""])
    asyncio.run(board_flow._show_board(reentry_session, db, board, alice))
    text = _written_text(reentry_session)
    assert "saved post draft for this message board" in text
    assert "[E]dit it, [D]elete it, or [I]gnore" in text


def test_resuming_a_draft_via_edit_prefills_the_body_and_can_post(db, alice):
    board = create_board(db, "general", creator=alice)
    exit_session = FakeSession(["p", "Subject", "saved body", "/exit", "b"])
    asyncio.run(board_flow._show_board(exit_session, db, board, alice))

    # "e" resumes -> subject prompt again -> /done finishes with the
    # pre-filled body unchanged -> "p" confirms the post -> "b" exits.
    resume_session = FakeSession(["e", "Resumed subject", "/done", "p", "b"])
    asyncio.run(board_flow._show_board(resume_session, db, board, alice))
    assert "Posted" in _written_text(resume_session)
    saved = list_posts_page(db, board, alice).posts[0]
    assert saved.subject == "Resumed subject"
    assert saved.body == "saved body"


# -- signature auto-append ----------------------------------------------


def test_new_post_appends_the_author_signature(db, alice):
    from netbbs.signature import set_signature

    set_signature(db, alice, "Alice")
    board = create_board(db, "general", creator=alice)
    session = FakeSession(["p", "Subject", "first line", "/done", "p", "b"])
    asyncio.run(board_flow._show_board(session, db, board, alice))

    post = list_posts_page(db, board, alice).posts[0]
    assert post.body == "first line\n-- \nAlice"


def test_new_post_has_no_signature_block_when_none_is_set(db, alice):
    board = create_board(db, "general", creator=alice)
    session = FakeSession(["p", "Subject", "first line", "/done", "p", "b"])
    asyncio.run(board_flow._show_board(session, db, board, alice))

    post = list_posts_page(db, board, alice).posts[0]
    assert post.body == "first line"


def test_resuming_a_draft_does_not_append_the_signature_a_second_time(db, alice):
    """The signature is appended once, at the original compose -- a
    resumed draft's body already reflects that (or its absence, or
    whatever the caller edited it to), so resuming must not re-append
    it on top."""
    from netbbs.signature import set_signature

    set_signature(db, alice, "Alice")
    board = create_board(db, "general", creator=alice)
    exit_session = FakeSession(["p", "Subject", "saved body", "/exit", "b"])
    asyncio.run(board_flow._show_board(exit_session, db, board, alice))

    resume_session = FakeSession(["e", "Resumed subject", "/done", "p", "b"])
    asyncio.run(board_flow._show_board(resume_session, db, board, alice))

    saved = list_posts_page(db, board, alice).posts[0]
    assert saved.body == "saved body\n-- \nAlice"  # appended once, not twice


def test_deleting_a_saved_draft_stops_it_being_offered_again(db, alice):
    board = create_board(db, "general", creator=alice)
    exit_session = FakeSession(["p", "Subject", "saved body", "/exit", "b"])
    asyncio.run(board_flow._show_board(exit_session, db, board, alice))

    delete_session = FakeSession(["d", "b", ""])
    asyncio.run(board_flow._show_board(delete_session, db, board, alice))
    assert "Draft deleted" in _written_text(delete_session)

    reentry_session = FakeSession(["b", ""])
    asyncio.run(board_flow._show_board(reentry_session, db, board, alice))
    assert "saved post draft" not in _written_text(reentry_session)


def test_ignoring_a_saved_draft_offers_it_again_next_time(db, alice):
    board = create_board(db, "general", creator=alice)
    exit_session = FakeSession(["p", "Subject", "saved body", "/exit", "b"])
    asyncio.run(board_flow._show_board(exit_session, db, board, alice))

    ignore_session = FakeSession(["i", "b", ""])
    asyncio.run(board_flow._show_board(ignore_session, db, board, alice))
    assert "Draft deleted" not in _written_text(ignore_session)

    # The draft is still there, so re-entering offers it again -- "i"
    # here answers *that* prompt a second time, not the board's own
    # [P]ost/[B]ack choice.
    reentry_session = FakeSession(["i", "b", ""])
    asyncio.run(board_flow._show_board(reentry_session, db, board, alice))
    assert "saved post draft" in _written_text(reentry_session)


def test_no_prompt_offered_when_no_draft_is_saved(db, alice):
    board = create_board(db, "general", creator=alice)
    session = FakeSession(["b", ""])
    asyncio.run(board_flow._show_board(session, db, board, alice))
    assert "saved post draft" not in _written_text(session)


def test_no_prompt_offered_to_a_caller_who_cannot_post(db, alice):
    """The board-entry prompt is gated on can_post the same way [P]ost
    itself already is -- a caller with no write access is never asked
    about a draft they couldn't act on if they resumed it, even if one
    genuinely exists on disk (e.g. saved before a permission change)."""
    from netbbs.net.draft_storage import save_draft

    board = create_board(db, "general", creator=alice, min_write_level=100)
    save_draft(board_flow._post_draft_path(db, kind="new", board=board, user=alice), "an orphaned draft")

    session = FakeSession(["b", ""])
    asyncio.run(board_flow._show_board(session, db, board, alice))
    assert "saved post draft" not in _written_text(session)


# -- editing an existing post: /exit is scoped separately from the ----------
# -- board-entry prompt (see _offer_saved_draft_if_any's own docstring) ----


def test_exit_while_editing_an_existing_post_saves_a_draft(db, alice):
    from netbbs.boards.posts import create_post

    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "Original subject", "Original body")
    session = FakeSession(["e", "1", "", "/exit", "b", ""])
    asyncio.run(board_flow._show_board(session, db, board, alice))
    text = _written_text(session)
    assert "Draft saved" in text
    assert "you'll be offered it next time you edit this post" in text
    saved = list_posts_page(db, board, alice).posts[0]
    assert saved.body == "Original body"  # unchanged -- nothing was actually saved as an edit
    assert saved.is_edited is False


def test_reopening_an_edited_post_offers_recovery_of_its_saved_draft(db, alice):
    from netbbs.boards.posts import create_post

    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "Original subject", "Original body")
    exit_session = FakeSession(["e", "1", "", "/exit", "b", ""])
    asyncio.run(board_flow._show_board(exit_session, db, board, alice))

    # Re-opening the *same* post's [E]dit again hits the existing
    # crash-recovery prompt inside _compose_body -- "y" resumes it.
    reopen_session = FakeSession(["e", "1", "", "y", "/done", "b", ""])
    asyncio.run(board_flow._show_board(reopen_session, db, board, alice))
    assert "Post updated" in _written_text(reopen_session)
