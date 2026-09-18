"""
Integration tests for the interactive file-area post-pagination
navigation in netbbs.net.file_flow._show_area (issue #10's file-area
follow-up) -- mirrors tests/test_board_pagination_ui.py's structure
and coverage, plus a test specific to file areas: downloading a file
from deep history, which now means paging back to it and pressing its
number rather than naming it.

This screen used to read whole typed *lines* and carry `/download`,
`/upload`, `/describe`, `/weblink` and `/remote` command forms -- the
only screen in NetBBS that did. It is keystroke-only now (design doc
§3.5), so every script here is `keys=`, not `lines=`. `/download
<filename>`'s area-wide name lookup was the one form that reached a
file on another page; `[F]ind` (netbbs.net.scan_and_find) reaches it
instead, entering this area with that file as row 1 of its page. What
stays covered here is the pagination half of that reach -- paging back
into history and downloading from an older page.

`netbbs.net.file_flow` is the second module migrated onto the two-lane
database execution model (issue #57) -- `_show_area` now takes a
`DatabaseLane` instead of a `Database`. Setup
calls (`create_user`, `create_file_area`, `upload_file`, `attest_name`,
etc.) still use a plain `Database` directly, same as every other test
file's style -- only the call *into* file_flow.py needs a lane.
"""

from __future__ import annotations

import asyncio
import re

from netbbs.activity import record_file_area_seen, unread_file_count
from netbbs.auth.users import create_user
from netbbs.files import entries as entries_module
from netbbs.files.areas import create_file_area
from netbbs.files.entries import list_files_page, upload_file
from netbbs.net.file_flow import _show_area
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

_PAGE_SIZE = entries_module._DEFAULT_PAGE_SIZE


class FakeSession:
    def __init__(self, keys=None, lines=None):
        self._keys = iter(keys or [])
        self._lines = iter(lines or [])
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

    async def read_key(self, echo: bool = True) -> str:
        # Deliberately raises rather than falling back to "" once the
        # scripted keys run out (the same house pattern as
        # tests/test_board_pagination_ui.py): a key this screen does
        # not handle bells and changes nothing, so a silent "" forever
        # would spin _show_area in an infinite loop and hang the test
        # instead of failing it. A test that needs the loop to end must
        # script an explicit "b".
        key = next(self._keys, None)
        if key is None:
            raise AssertionError("FakeSession.read_key() called with no more scripted keys")
        return key

    # Kept for the sub-screens this one opens -- the description editor,
    # pick_item and confirmations still read whole lines; only the file
    # listing itself is keystroke-only.
    async def read_line(self, echo: bool = True, **kwargs) -> str:
        return next(self._lines, "")

    async def write_raw(self, data: bytes) -> None:
        # Real transports implement this for Zmodem transfer; this fake
        # only cares about download *dispatch* (the keystroke reaching
        # the right entry), not the actual transfer mechanics, so it
        # fails the same deliberate way netbbs.net.web.WebSession does
        # for a transport that can't carry raw bytes.
        raise NotImplementedError("write_raw not supported by FakeSession")

    async def read_byte(self):
        raise NotImplementedError("read_byte not supported by FakeSession")

    @property
    def output(self) -> str:
        return "".join(self.written)

    @property
    def visible_output(self) -> str:
        return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", self.output)


def _make_area_with_files(db, count: int, monkeypatch):
    user = create_user(db, "alice", password="hunter2", user_level=10)
    area = create_file_area(db, "docs", creator=user)
    timestamps = iter(f"2026-01-01T00:00:{i:02d}.000000Z" for i in range(count))
    monkeypatch.setattr(entries_module, "utc_now_iso", lambda: next(timestamps))
    for i in range(count):
        upload_file(db, area, user, f"file{i}.txt", f"content {i}".encode())
    return area, user


def test_opening_a_multi_page_area_shows_only_the_newest_page(tmp_path, monkeypatch):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    total = _PAGE_SIZE * 3 + 2
    area, user = _make_area_with_files(db, total, monkeypatch)
    session = FakeSession(keys=["b"])  # view the newest page, then back out
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    assert "NetBBS › Files › docs" in session.visible_output
    assert f"{_PAGE_SIZE} files on this page" in session.output
    shown = sum(1 for i in range(total) if f"file{i}.txt " in session.output)
    assert shown == _PAGE_SIZE
    for i in range(total - _PAGE_SIZE, total):
        assert f"file{i}.txt " in session.output
    for i in range(0, total - _PAGE_SIZE):
        assert f"file{i}.txt " not in session.output
    assert "lder" in session.output
    assert "ewer" not in session.output
    lane.close()
    db.close()


def test_older_command_navigates_to_the_previous_page(tmp_path, monkeypatch):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    total = _PAGE_SIZE * 2
    area, user = _make_area_with_files(db, total, monkeypatch)
    session = FakeSession(keys=["o", "b"])  # newest page, then older, then back out
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    for i in range(0, _PAGE_SIZE):
        assert f"file{i}.txt " in session.output
    lane.close()
    db.close()


def test_recent_command_jumps_straight_back_to_the_newest_page(tmp_path, monkeypatch):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    total = _PAGE_SIZE * 3
    area, user = _make_area_with_files(db, total, monkeypatch)
    session = FakeSession(keys=["o", "o", "r", "b"])
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    output = session.output
    newest_index = output.rfind(f"file{total - 1}.txt ")
    older_index = output.rfind("file0.txt ")
    assert newest_index > older_index
    lane.close()
    db.close()


def test_single_page_area_offers_no_older_newer_recent_options(tmp_path, monkeypatch):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, user = _make_area_with_files(db, count=2, monkeypatch=monkeypatch)
    session = FakeSession(keys=["b"])
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    assert "lder" not in session.output
    assert "ewer" not in session.output
    assert "ecent" not in session.output
    assert "ack" in session.output
    lane.close()
    db.close()


def test_empty_area_has_a_guided_empty_state(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    user = create_user(db, "alice", password="hunter2", user_level=10)
    area = create_file_area(db, "docs", creator=user)
    # The empty state's action bar is keystrokes too: `b` leaves it,
    # where an empty typed line used to.
    session = FakeSession(keys=["b"])
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    assert "NetBBS › Files › docs" in session.visible_output
    assert "This file area has no files yet" in session.output
    assert "Uploads and fetched Link files will appear here" in session.output
    lane.close()
    db.close()


def test_download_works_for_a_file_reached_by_paging_back_into_history(tmp_path, monkeypatch):
    """A file from deep history is still downloadable from this screen,
    which is what pagination itself put at risk -- but the way there is
    now `[O]lder` until the file is on the page and then its number,
    not a `/download <filename>` lookup across the whole area. (The
    one-step reach that lookup gave is `[F]ind`'s job now, and belongs
    to its own tests.)"""
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    total = _PAGE_SIZE * 2
    area, user = _make_area_with_files(db, total, monkeypatch)
    # `o` pages back to the oldest page, where file0.txt is entry 1.
    session = FakeSession(keys=["o", "1"])
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    # send_file_to_caller already catches the FakeSession's NotImplementedError
    # (real transports don't raise it -- see FakeSession.write_raw) and
    # reports it as a normal "Download failed" message rather than
    # propagating -- this test only cares that the keystroke reached the
    # right entry before that point.
    assert "Starting Zmodem send of 'file0.txt'" in session.output
    lane.close()
    db.close()


# -- identity attestation: verified-name display + age/name gating (design doc §18) --


def test_file_listing_shows_verified_and_displayed_real_name(tmp_path):
    from netbbs.attestation import attest_name
    from netbbs.auth.users import SYSOP_LEVEL

    db_path = tmp_path / "node.db"
    db = Database(db_path)
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    area = create_file_area(db, "docs", creator=alice, name_requirement="verified_and_displayed")
    upload_file(db, area, alice, "file.txt", b"hello")
    attest_name(db, alice, "Alice Smith", verifier=sysop)

    session = FakeSession(keys=["b"])
    lane = DatabaseLane(db_path)
    asyncio.run(_show_area(session, lane, area, alice))

    assert "(=Alice Smith=)" in session.output
    lane.close()
    db.close()


def test_file_listing_does_not_leak_current_display_name_for_ungated_area(tmp_path):
    from netbbs.attestation import set_display_name

    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    area = create_file_area(db, "docs", creator=alice)  # no name_requirement
    upload_file(db, area, alice, "file.txt", b"hello")
    set_display_name(db, alice, "New Display Name")

    session = FakeSession(keys=["b"])
    lane = DatabaseLane(db_path)
    asyncio.run(_show_area(session, lane, area, alice))

    assert "New Display Name" not in session.output
    assert "alice" in session.output
    lane.close()
    db.close()


def test_min_age_gate_hides_the_upload_hint_when_unmet(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    area = create_file_area(db, "adults", creator=alice, min_age=18)
    upload_file(db, area, alice, "file.txt", b"hello")
    session = FakeSession(keys=["b"])
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, alice))

    assert "[U]pload" not in session.visible_output
    lane.close()
    db.close()


def test_min_age_gate_allows_upload_hint_once_met(tmp_path):
    from datetime import date

    from netbbs.attestation import set_birthdate

    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    set_birthdate(db, alice, date(1990, 1, 1))
    area = create_file_area(db, "adults", creator=alice, min_age=18)
    upload_file(db, area, alice, "file.txt", b"hello")
    session = FakeSession(keys=["b"])
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, alice))

    assert "[U]pload" in session.visible_output
    lane.close()
    db.close()


def test_name_requirement_hides_the_upload_hint_when_unmet(tmp_path):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    area = create_file_area(db, "verified-only", creator=alice, name_requirement="verified")
    upload_file(db, area, alice, "file.txt", b"hello")
    session = FakeSession(keys=["b"])
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, alice))

    assert "[U]pload" not in session.visible_output
    lane.close()
    db.close()


# -- issue #56: viewing a file area advances the read cursor -----------------


def test_opening_an_area_advances_the_viewers_read_cursor(tmp_path, monkeypatch):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, alice = _make_area_with_files(db, 3, monkeypatch)
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    assert unread_file_count(db, bob, area) is None
    lane = DatabaseLane(db_path)

    session = FakeSession(keys=["b"])
    asyncio.run(_show_area(session, lane, area, bob))

    assert unread_file_count(db, bob, area) == 0
    lane.close()
    db.close()


def test_paging_to_an_older_page_does_not_regress_the_cursor(tmp_path, monkeypatch):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    total = _PAGE_SIZE * 2
    area, alice = _make_area_with_files(db, total, monkeypatch)
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    newest_page = list_files_page(db, area, bob)
    record_file_area_seen(db, bob, area, newest_page.entries[-1])
    lane = DatabaseLane(db_path)

    session = FakeSession(keys=["o", "b"])  # newest page already recorded above, then page backward
    asyncio.run(_show_area(session, lane, area, bob))

    assert unread_file_count(db, bob, area) == 0  # still caught up, not regressed
    lane.close()
    db.close()


def test_jump_to_first_unread_opens_on_the_file_right_after_the_cursor(tmp_path, monkeypatch):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, alice = _make_area_with_files(db, _PAGE_SIZE + 1, monkeypatch)
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    files = db.connection.execute("SELECT file_id, created_at FROM files ORDER BY created_at ASC").fetchall()
    cursor = (files[0]["created_at"], files[0]["file_id"])
    lane = DatabaseLane(db_path)

    session = FakeSession(keys=["b"])
    asyncio.run(_show_area(session, lane, area, bob, initial_cursor=cursor))

    assert "file0.txt" not in session.output
    assert "file1.txt" in session.output
    lane.close()
    db.close()


def test_jump_to_first_unread_falls_back_to_the_newest_page_once_caught_up(tmp_path, monkeypatch):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, alice = _make_area_with_files(db, 3, monkeypatch)
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    newest = db.connection.execute(
        "SELECT file_id, created_at FROM files ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    cursor = (newest["created_at"], newest["file_id"])
    lane = DatabaseLane(db_path)

    session = FakeSession(keys=["b"])
    asyncio.run(_show_area(session, lane, area, bob, initial_cursor=cursor))

    assert "has no files yet" not in session.output
    assert "file2.txt" in session.output
    lane.close()
    db.close()
