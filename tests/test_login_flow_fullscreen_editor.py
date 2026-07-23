"""
Integration tests for the fullscreen-prose-editor wiring in
netbbs.net.login_flow: the
Profile screen's on/off toggle, and that composing a post / editing a
bio actually routes through netbbs.net.prose_editor.edit_prose once a
user has opted in, instead of the plain read_line() flow every account
still sees by default (already covered, unaffected, by the existing
test_board_pagination_ui.py/test_directory_ui.py suites).
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.boards.boards import create_board
from netbbs.boards.posts import MAX_SUBJECT_BYTES, create_post, list_posts_page
from netbbs.directory import get_bio
from netbbs.net import login_flow
from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.editor_preference import fullscreen_editor_enabled, set_fullscreen_editor_enabled
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
    """Same shape tests/test_ansi_editor.py's FakeSession established:
    a single ordered input queue serves read_key/read_line/
    read_editor_key alike, so one scripted list can drive a scenario
    that passes through both an ordinary menu and a fullscreen editor."""

    def __init__(self, inputs: list[str] | None = None):
        self._inputs = list(inputs or [])
        self.written: list[str] = []
        self.terminal_width = 80
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

    async def read_editor_key(self) -> EditorKey:
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


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


def _type(text: str) -> list[str]:
    return list(text)


# -- Profile screen toggle ------------------------------------------------


def test_profile_toggle_switches_the_preference_on_and_off(db, alice):
    session = FakeSession(["f", "f", "b"])
    asyncio.run(login_flow._edit_profile(session, db, alice))
    # First "f" turns it on, second turns it back off.
    assert fullscreen_editor_enabled(db, alice) is False
    assert "now on" in _written_text(session)
    assert "now off" in _written_text(session)


# -- composing a new post ---------------------------------------------------


def test_compose_post_uses_plain_read_line_by_default(db, alice):
    # A board with no posts yet skips _show_board's own read_key()
    # reading loop entirely (the `else: while True: ...` branch never
    # runs) -- straight to the "Post a new message?" prompt, so no
    # leading "b" belongs in the scripted input here.
    board = create_board(db, "general", creator=alice)
    session = FakeSession(["Hello there", "A plain single-line body"])
    asyncio.run(login_flow._show_board(session, db, board, alice))
    assert "Posted" in _written_text(session)


def test_compose_post_uses_fullscreen_editor_once_opted_in(db, alice):
    set_fullscreen_editor_enabled(db, alice, True)
    board = create_board(db, "general", creator=alice)
    session = FakeSession(
        ["Hello there"] + _type("A body typed in the fullscreen editor") + ["CTRL+O"]
    )
    asyncio.run(login_flow._show_board(session, db, board, alice))
    assert "Posted" in _written_text(session)
    saved = list_posts_page(db, board, alice).posts[0]
    assert saved.subject == "Hello there"
    assert saved.body == "A body typed in the fullscreen editor"


def test_compose_post_cancelled_from_the_fullscreen_editor_does_not_post(db, alice):
    set_fullscreen_editor_enabled(db, alice, True)
    board = create_board(db, "general", creator=alice)
    session = FakeSession(["Hello there", "CTRL+X"])  # quit editor without typing anything
    asyncio.run(login_flow._show_board(session, db, board, alice))
    text = _written_text(session)
    assert "Posted" not in text
    assert "cancelled" in text.lower()


def test_compose_post_with_oversized_subject_shows_a_friendly_error(db, alice):
    """Regression test for GitHub issue #32 (reopened): the plain
    single-line prompt has no length cap of its own (only the 4,096-
    char line editor ceiling), so a subject can clear that and still
    exceed create_post()'s own MAX_SUBJECT_BYTES domain limit. Before
    this fix, the resulting PostError propagated straight out of
    _compose_new_post() and terminated the session instead of being
    shown as a normal rejection."""
    board = create_board(db, "general", creator=alice)
    oversized_subject = "x" * (MAX_SUBJECT_BYTES + 1)
    session = FakeSession([oversized_subject, "A normal body"])
    asyncio.run(login_flow._show_board(session, db, board, alice))
    text = _written_text(session)
    assert "Posted" not in text
    assert "Could not create post" in text
    assert list_posts_page(db, board, alice).posts == []


def test_compose_post_with_oversized_multibyte_subject_shows_a_friendly_error(db, alice):
    """A subject that's well under any plausible character-based cap
    can still exceed MAX_SUBJECT_BYTES once UTF-8 encoded -- the limit
    is counted in bytes, not characters (see MAX_SUBJECT_BYTES's own
    docstring), so multibyte content must be rejected the same way."""
    board = create_board(db, "general", creator=alice)
    oversized_subject = "€" * 150  # each euro sign is 3 UTF-8 bytes
    assert len(oversized_subject) < MAX_SUBJECT_BYTES
    assert len(oversized_subject.encode("utf-8")) > MAX_SUBJECT_BYTES
    session = FakeSession([oversized_subject, "A normal body"])
    asyncio.run(login_flow._show_board(session, db, board, alice))
    text = _written_text(session)
    assert "Posted" not in text
    assert "Could not create post" in text
    assert list_posts_page(db, board, alice).posts == []


def test_compose_post_with_subject_exactly_at_the_byte_boundary_succeeds(db, alice):
    board = create_board(db, "general", creator=alice)
    subject = "x" * MAX_SUBJECT_BYTES
    session = FakeSession([subject, "A normal body"])
    asyncio.run(login_flow._show_board(session, db, board, alice))
    assert "Posted" in _written_text(session)
    assert list_posts_page(db, board, alice).posts[0].subject == subject


# -- editing an existing post -------------------------------------------------


def test_edit_option_hidden_when_nothing_on_the_page_is_editable(db, alice):
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "Subject", "Body")
    session = FakeSession(["b", ""])
    asyncio.run(login_flow._show_board(session, db, board, bob))
    assert "[E]dit" not in _written_text(session)


def test_edit_existing_post_via_plain_line_flow(db, alice):
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "Original subject", "Original body")
    # e -> pick post 1 -> keep subject (Enter) -> new body -> back -> skip new post
    session = FakeSession(["e", "1", "", "Edited body", "b", ""])
    asyncio.run(login_flow._show_board(session, db, board, alice))
    assert "Post updated" in _written_text(session)
    saved = list_posts_page(db, board, alice).posts[0]
    assert saved.subject == "Original subject"
    assert saved.body == "Edited body"
    assert saved.is_edited is True


def test_edit_existing_post_via_fullscreen_editor(db, alice):
    set_fullscreen_editor_enabled(db, alice, True)
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "Original subject", "Original body")
    session = FakeSession(
        ["e", "1", "New subject"] + ["END"] + _type(" -- revised") + ["CTRL+O", "b", ""]
    )
    asyncio.run(login_flow._show_board(session, db, board, alice))
    assert "Post updated" in _written_text(session)
    saved = list_posts_page(db, board, alice).posts[0]
    assert saved.subject == "New subject"
    assert saved.body == "Original body -- revised"  # editor was pre-filled with the current body


def test_edit_existing_post_cancelled_leaves_it_unchanged(db, alice):
    set_fullscreen_editor_enabled(db, alice, True)
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "Subject", "Body")
    session = FakeSession(["e", "1", "Subject", "CTRL+X", "d", "b", ""])
    asyncio.run(login_flow._show_board(session, db, board, alice))
    assert "cancelled" in _written_text(session).lower()
    saved = list_posts_page(db, board, alice).posts[0]
    assert saved.body == "Body"
    assert saved.is_edited is False


def test_edit_existing_post_rejects_an_invalid_post_number(db, alice):
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "Subject", "Body")
    session = FakeSession(["e", "9", "b", ""])
    asyncio.run(login_flow._show_board(session, db, board, alice))
    assert "Not a valid post number" in _written_text(session)


def test_editing_a_post_does_not_reset_to_the_newest_page(db, alice):
    board = create_board(db, "general", creator=alice)
    # 6 posts -> 2 pages at the default page size of 5; back up one
    # page, edit the post shown there, and confirm the view stays on
    # that same older page rather than jumping back to page one.
    posts = [create_post(db, board, alice, f"Subject {i}", f"Body {i}") for i in range(6)]
    session = FakeSession(["o", "e", "1", "", "Edited", "b", ""])
    asyncio.run(login_flow._show_board(session, db, board, alice))
    text = _written_text(session)
    assert "Post updated" in text
    assert "Subject 0" in text  # the oldest post, only visible on the older page


# -- editing the bio ---------------------------------------------------------


def test_edit_bio_uses_fullscreen_editor_once_opted_in(db, alice):
    set_fullscreen_editor_enabled(db, alice, True)
    session = FakeSession(["e"] + _type("My new bio") + ["CTRL+O", "b"])
    asyncio.run(login_flow._edit_profile(session, db, alice))
    assert get_bio(db, alice) == "My new bio"
    assert "Bio updated" in _written_text(session)


def test_edit_bio_prefills_the_fullscreen_editor_with_the_current_bio(db, alice):
    from netbbs.directory import set_bio

    set_bio(db, alice, "Original bio")
    set_fullscreen_editor_enabled(db, alice, True)
    session = FakeSession(["e", "END"] + _type(" - updated") + ["CTRL+O", "b"])
    asyncio.run(login_flow._edit_profile(session, db, alice))
    assert get_bio(db, alice) == "Original bio - updated"
