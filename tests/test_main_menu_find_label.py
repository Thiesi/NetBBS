"""The main menu's Find entry names what Find searches (issue #811).

It said "Search boards, files, and mail", but Find searches posts, files and
retained chat; mail has only the mailbox's folder-local [F]ind.
"""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.storage.database import Database
from tests.test_first_time_caller import _menu, _MenuSession
from tests.test_new_scan import _visible_text


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.mark.parametrize("width", [80, 40])
def test_find_names_posts_files_and_chat_not_mail(db, width):
    lena = create_user(db, "lena_h", password="hunter2", user_level=10)
    session = _MenuSession(["l", "y"])
    session.terminal_width = width
    _menu(db, session, lena)
    text = _visible_text(session)

    assert "Search posts, files, and chat" in text
    assert "and mail" not in text
