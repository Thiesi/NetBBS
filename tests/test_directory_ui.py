"""
Integration tests for the user directory/profile screens in
netbbs.net.login_flow (design doc §13) — distinct
from tests/test_directory.py, which tests netbbs.directory in
isolation. Same lightweight duck-typed FakeSession as
tests/test_board_pagination_ui.py (no need to subclass the Session
ABC — these functions only ever call the methods they actually use).
"""

from __future__ import annotations

import asyncio

from netbbs.auth.users import create_user
from netbbs.directory import get_bio, is_bio_visible, set_bio, set_bio_visible
from netbbs.net.login_flow import _browse_directory, _edit_profile
from netbbs.storage.database import Database


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
        # scripted keys run out -- real transports never return "" from
        # read_key() (see netbbs.net.char_input.read_key), so silently
        # returning it forever here would just trade an under-scripted
        # test hanging in an infinite loop for a clear, fast failure.
        key = next(self._keys, None)
        if key is None:
            raise AssertionError("FakeSession.read_key() called with no more scripted keys")
        return key

    async def read_line(self, echo: bool = True) -> str:
        return next(self._lines, "")

    @property
    def output(self) -> str:
        return "".join(self.written)


# -- _browse_directory ------------------------------------------------------


def test_browse_directory_lists_all_users(tmp_path):
    db = Database(tmp_path / "node.db")
    viewer = create_user(db, "alice", password="hunter2", user_level=10)
    create_user(db, "bob", password="hunter2", user_level=10)
    session = FakeSession(keys=["b"])  # back out of the picker without selecting

    asyncio.run(_browse_directory(session, db, viewer))

    assert "alice" in session.output
    assert "bob" in session.output
    db.close()


def test_browse_directory_shows_private_bio_state_by_default(tmp_path):
    db = Database(tmp_path / "node.db")
    viewer = create_user(db, "alice", password="hunter2", user_level=10)
    create_user(db, "bob", password="hunter2", user_level=10)
    session = FakeSession(keys=["b"])

    asyncio.run(_browse_directory(session, db, viewer))

    assert "bio: private" in session.output
    db.close()


def test_browse_directory_shows_public_bio_state_once_visible(tmp_path):
    db = Database(tmp_path / "node.db")
    viewer = create_user(db, "alice", password="hunter2", user_level=10)
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    set_bio(db, bob, "Hi there")
    set_bio_visible(db, bob, True)
    session = FakeSession(keys=["b"])

    asyncio.run(_browse_directory(session, db, viewer))

    assert "bio: public" in session.output
    db.close()


def test_selecting_a_directory_entry_shows_their_vcard(tmp_path):
    db = Database(tmp_path / "node.db")
    viewer = create_user(db, "alice", password="hunter2", user_level=10)
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    set_bio(db, bob, "Retro computing enthusiast")
    set_bio_visible(db, bob, True)
    # alice sorts before bob alphabetically -> bob is item "02" on the page.
    session = FakeSession(keys=["0", "2"])

    asyncio.run(_browse_directory(session, db, viewer))

    assert "Retro computing enthusiast" in session.output
    db.close()


def test_selecting_a_directory_entry_hides_private_bio(tmp_path):
    db = Database(tmp_path / "node.db")
    viewer = create_user(db, "alice", password="hunter2", user_level=10)
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    set_bio(db, bob, "Secret hobby list")
    session = FakeSession(keys=["0", "2"])

    asyncio.run(_browse_directory(session, db, viewer))

    assert "Secret hobby list" not in session.output
    assert "no public bio" in session.output
    db.close()


def test_directory_of_one_still_lists_the_viewer_themselves(tmp_path):
    # A registered viewer is always in the directory too -- there's no
    # "truly empty" case to exercise here (pick_item's empty-list path
    # is already covered by tests/test_picker.py); this just confirms
    # a single-entry directory still renders and quits cleanly.
    db = Database(tmp_path / "node.db")
    viewer = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(keys=["b"])

    asyncio.run(_browse_directory(session, db, viewer))

    assert "alice" in session.output
    db.close()


# -- _edit_profile ------------------------------------------------------


def test_edit_profile_shows_current_state(tmp_path):
    db = Database(tmp_path / "node.db")
    user = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(keys=["b"])

    asyncio.run(_edit_profile(session, db, user))

    assert "no bio set" in session.output
    assert "Visibility: private" in session.output
    db.close()


def test_edit_profile_bio_updates_stored_bio(tmp_path):
    db = Database(tmp_path / "node.db")
    user = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(keys=["e", "b"], lines=["Hi, I'm Alice.", "I collect old modems.", ""])

    asyncio.run(_edit_profile(session, db, user))

    assert get_bio(db, user) == "Hi, I'm Alice.\nI collect old modems."


def test_edit_profile_bio_blank_first_line_clears_it(tmp_path):
    db = Database(tmp_path / "node.db")
    user = create_user(db, "alice", password="hunter2", user_level=10)
    set_bio(db, user, "Old bio")
    session = FakeSession(keys=["e", "b"], lines=[""])

    asyncio.run(_edit_profile(session, db, user))

    assert get_bio(db, user) == ""


def test_edit_profile_visibility_toggles_from_private_to_public(tmp_path):
    db = Database(tmp_path / "node.db")
    user = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(keys=["v", "b"])

    asyncio.run(_edit_profile(session, db, user))

    assert is_bio_visible(db, user) is True


def test_edit_profile_visibility_toggles_from_public_to_private(tmp_path):
    db = Database(tmp_path / "node.db")
    user = create_user(db, "alice", password="hunter2", user_level=10)
    set_bio_visible(db, user, True)
    session = FakeSession(keys=["v", "b"])

    asyncio.run(_edit_profile(session, db, user))

    assert is_bio_visible(db, user) is False


def test_edit_profile_invalid_key_does_not_redraw_the_screen(tmp_path):
    # Regression test: reprinting the "Choice: " prompt after the bell
    # would add no value -- an unrecognized key must produce genuinely
    # nothing beyond the bell, not even a fresh prompt line.
    db = Database(tmp_path / "node.db")
    user = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(keys=["z", "b"])

    asyncio.run(_edit_profile(session, db, user))

    assert session.output.count("Your profile:") == 1
    assert session.output.count("Choice: ") == 1
    assert "\a" in session.output
