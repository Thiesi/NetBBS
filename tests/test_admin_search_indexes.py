"""Operations -> Search indexes (issue #724): the console's route to what
`python -m netbbs.search check|rebuild` (issue #74) does from the host."""

from __future__ import annotations

from netbbs.boards.boards import create_board
from netbbs.boards.posts import create_post
from netbbs.chat.channels import create_channel
from netbbs.chat.scrollback import record_message
from netbbs.files.areas import create_file_area
from netbbs.files.entries import upload_file
from netbbs.moderation.log import list_recent_actions
from netbbs.search import check_index_integrity, search_posts
from tests.test_admin_flow import (  # noqa: F401 -- fixtures
    FakeSession,
    _normalized_visible,
    _run,
    _written_text,
    db,
    lane,
    sysop,
)


def _drift(db, sysop):
    board = create_board(db, "general", creator=sysop)
    post = create_post(db, board, sysop, "hello world", "a secret body")
    area = create_file_area(db, "downloads", creator=sysop)
    upload_file(db, area, sysop, "readme.txt", b"data", description="a guide")
    channel = create_channel(db, "lobby", creator=sysop)
    record_message(db, channel, kind="message", author_label="sysop", body="hi there")
    db.connection.execute("DELETE FROM post_search WHERE root_post_id = ?", (post.root_post_id,))
    db.connection.execute("UPDATE file_search SET filename = 'wrong.txt'")
    db.connection.execute(
        "INSERT INTO channel_message_search (body, channel_id, message_id) VALUES ('orphan', ?, ?)",
        (channel.id, 999999),
    )
    db.connection.commit()


def test_operations_offers_search_indexes(db, lane, sysop):
    session = FakeSession(["o", "b", "b"])
    _run(session, lane, sysop)
    assert "[S]earch indexes" in _normalized_visible(_written_text(session))


def test_search_indexes_screen_reports_counts_and_rebuilds(db, lane, sysop):
    _drift(db, sysop)

    session = FakeSession(["o", "s", "r", "b", "b", "b"])
    _run(session, lane, sysop)

    text = _normalized_visible(_written_text(session))
    assert "Message posts: 1 missing, 0 stale, 0 extra" in text
    assert "Files: 0 missing, 1 stale, 0 extra" in text
    assert "Chat messages: 0 missing, 0 stale, 1 extra" in text
    # Counts only: neither the drifted ids nor the indexed text.
    assert "secret body" not in text and "999999" not in text
    assert "Rebuilt the search indexes: 3 entries corrected." in text
    # After the rebuild the screen shows the clean re-check and no longer
    # offers a rebuild.
    after = text[text.index("3 entries corrected"):]
    assert "Message posts: consistent" in text[text.rindex("Search indexes"):]
    assert "[R]ebuild" not in after

    assert check_index_integrity(db).is_clean
    assert [hit.subject for hit in search_posts(db, sysop, "hello")] == ["hello world"]
    entries = [e for e in list_recent_actions(db, limit=10) if e.action == "rebuild_search_indexes"]
    assert len(entries) == 1
    assert entries[0].detail == "entries corrected=3"


def test_search_indexes_screen_with_clean_indexes_offers_no_rebuild(db, lane, sysop):
    session = FakeSession(["o", "s", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _normalized_visible(_written_text(session))
    assert "Message posts: consistent" in text
    assert "Files: consistent" in text
    assert "Chat messages: consistent" in text
    assert "[R]ebuild" not in text
    assert "[C]heck again" in text


def test_search_indexes_leaving_without_rebuild_changes_nothing(db, lane, sysop):
    _drift(db, sysop)
    session = FakeSession(["o", "s", "b", "b", "b"])
    _run(session, lane, sysop)
    assert not check_index_integrity(db).is_clean
    assert not [e for e in list_recent_actions(db, limit=10) if e.action == "rebuild_search_indexes"]


def test_search_indexes_check_again_picks_up_new_drift(db, lane, sysop):
    board = create_board(db, "general", creator=sysop)
    create_post(db, board, sysop, "hello world", "body")

    class _DriftOnCheck(FakeSession):
        """Breaks an index between the first check and [C]heck again."""

        def _maybe_drift(self):
            if self._inputs and self._inputs[0] == "c":
                db.connection.execute("DELETE FROM post_search")
                db.connection.commit()

        async def read_key(self, echo: bool = True):
            self._maybe_drift()
            return await super().read_key(echo)

        async def read_editor_key(self, **kwargs):
            self._maybe_drift()
            return await super().read_editor_key(**kwargs)

    session = _DriftOnCheck(["o", "s", "c", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _normalized_visible(_written_text(session))
    assert "Message posts: consistent" in text
    assert "Message posts: 1 missing, 0 stale, 0 extra" in text
    assert text.index("Message posts: consistent") < text.index("1 missing")
