"""
Integration tests for the interactive file-area post-pagination
navigation in netbbs.net.file_flow._show_area (design doc round 31,
issue #10's file-area follow-up) -- mirrors tests/test_board_
pagination_ui.py's structure and coverage, plus a test specific to
file areas: /download working for a file that isn't on the currently
displayed page (get_file_by_name, the fix that pagination itself made
necessary).
"""

from __future__ import annotations

import asyncio

from netbbs.auth.users import create_user
from netbbs.files import entries as entries_module
from netbbs.files.areas import create_file_area
from netbbs.files.entries import upload_file
from netbbs.net.file_flow import _show_area
from netbbs.storage.database import Database

_PAGE_SIZE = entries_module._DEFAULT_PAGE_SIZE


class FakeSession:
    def __init__(self, lines=None):
        self._lines = iter(lines or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_line(self, echo: bool = True) -> str:
        return next(self._lines, "")

    async def write_raw(self, data: bytes) -> None:
        # Real transports implement this for Zmodem transfer; this fake
        # only cares about download *dispatch* (finding the right file
        # by name), not the actual transfer mechanics, so it fails the
        # same deliberate way netbbs.net.web.WebSession does for a
        # transport that can't carry raw bytes.
        raise NotImplementedError("write_raw not supported by FakeSession")

    async def read_byte(self):
        raise NotImplementedError("read_byte not supported by FakeSession")

    @property
    def output(self) -> str:
        return "".join(self.written)


def _make_area_with_files(db, count: int, monkeypatch):
    user = create_user(db, "alice", password="hunter2", user_level=10)
    area = create_file_area(db, "docs", creator=user)
    timestamps = iter(f"2026-01-01T00:00:{i:02d}.000000Z" for i in range(count))
    monkeypatch.setattr(entries_module, "utc_now_iso", lambda: next(timestamps))
    for i in range(count):
        upload_file(db, area, user, f"file{i}.txt", f"content {i}".encode())
    return area, user


def test_opening_a_multi_page_area_shows_only_the_newest_page(tmp_path, monkeypatch):
    db = Database(tmp_path / "node.db")
    total = _PAGE_SIZE * 3 + 2
    area, user = _make_area_with_files(db, total, monkeypatch)
    session = FakeSession(lines=["b"])  # view the newest page, then back out

    asyncio.run(_show_area(session, db, area, user))

    shown = sum(1 for i in range(total) if f"file{i}.txt " in session.output)
    assert shown == _PAGE_SIZE
    for i in range(total - _PAGE_SIZE, total):
        assert f"file{i}.txt " in session.output
    for i in range(0, total - _PAGE_SIZE):
        assert f"file{i}.txt " not in session.output
    assert "lder" in session.output
    assert "ewer" not in session.output
    db.close()


def test_older_command_navigates_to_the_previous_page(tmp_path, monkeypatch):
    db = Database(tmp_path / "node.db")
    total = _PAGE_SIZE * 2
    area, user = _make_area_with_files(db, total, monkeypatch)
    session = FakeSession(lines=["o", "b"])  # newest page, then older, then back out

    asyncio.run(_show_area(session, db, area, user))

    for i in range(0, _PAGE_SIZE):
        assert f"file{i}.txt " in session.output
    db.close()


def test_recent_command_jumps_straight_back_to_the_newest_page(tmp_path, monkeypatch):
    db = Database(tmp_path / "node.db")
    total = _PAGE_SIZE * 3
    area, user = _make_area_with_files(db, total, monkeypatch)
    session = FakeSession(lines=["o", "o", "r", "b"])

    asyncio.run(_show_area(session, db, area, user))

    output = session.output
    newest_index = output.rfind(f"file{total - 1}.txt ")
    older_index = output.rfind("file0.txt ")
    assert newest_index > older_index
    db.close()


def test_single_page_area_offers_no_older_newer_recent_options(tmp_path, monkeypatch):
    db = Database(tmp_path / "node.db")
    area, user = _make_area_with_files(db, count=2, monkeypatch=monkeypatch)
    session = FakeSession(lines=["b"])

    asyncio.run(_show_area(session, db, area, user))

    assert "lder" not in session.output
    assert "ewer" not in session.output
    assert "ecent" not in session.output
    assert "ack" in session.output
    db.close()


def test_download_works_for_a_file_not_on_the_currently_displayed_page(tmp_path, monkeypatch):
    """The specific regression pagination would otherwise introduce:
    /download must still find a file from deep history, not just
    whatever happens to be on the newest page currently in memory."""
    db = Database(tmp_path / "node.db")
    total = _PAGE_SIZE * 3
    area, user = _make_area_with_files(db, total, monkeypatch)
    # Never navigate to an older page -- straight from the newest page,
    # /download the very first (oldest) uploaded file by name.
    session = FakeSession(lines=["/download file0.txt"])

    asyncio.run(_show_area(session, db, area, user))

    # _handle_download already catches the FakeSession's NotImplementedError
    # (real transports don't raise it -- see FakeSession.write_raw) and
    # reports it as a normal "Download failed" message rather than
    # propagating -- this test only cares that the file was actually
    # *found* by name (no "No file named" error) before that point.
    assert "No file named" not in session.output
    assert "Starting Zmodem send of 'file0.txt'" in session.output
    db.close()


def test_download_reports_a_clear_error_for_a_truly_nonexistent_file(tmp_path, monkeypatch):
    db = Database(tmp_path / "node.db")
    area, user = _make_area_with_files(db, count=2, monkeypatch=monkeypatch)
    session = FakeSession(lines=["/download does-not-exist.txt"])

    asyncio.run(_show_area(session, db, area, user))

    assert "No file named 'does-not-exist.txt' in this area." in session.output
    db.close()


# -- identity attestation: verified-name display + age/name gating (design doc §18, round 103) --


def test_file_listing_shows_verified_and_displayed_real_name(tmp_path):
    from netbbs.attestation import attest_name
    from netbbs.auth.users import SYSOP_LEVEL

    db = Database(tmp_path / "node.db")
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    area = create_file_area(db, "docs", creator=alice, name_requirement="verified_and_displayed")
    upload_file(db, area, alice, "file.txt", b"hello")
    attest_name(db, alice, "Alice Smith", verifier=sysop)

    session = FakeSession(lines=["b"])
    asyncio.run(_show_area(session, db, area, alice))

    assert "(=Alice Smith=)" in session.output
    db.close()


def test_file_listing_does_not_leak_current_display_name_for_ungated_area(tmp_path):
    from netbbs.attestation import set_display_name

    db = Database(tmp_path / "node.db")
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    area = create_file_area(db, "docs", creator=alice)  # no name_requirement
    upload_file(db, area, alice, "file.txt", b"hello")
    set_display_name(db, alice, "New Display Name")

    session = FakeSession(lines=["b"])
    asyncio.run(_show_area(session, db, area, alice))

    assert "New Display Name" not in session.output
    assert "alice" in session.output
    db.close()


def test_min_age_gate_hides_the_upload_hint_when_unmet(tmp_path):
    db = Database(tmp_path / "node.db")
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    area = create_file_area(db, "adults", creator=alice, min_age=18)
    upload_file(db, area, alice, "file.txt", b"hello")
    session = FakeSession(lines=["b"])

    asyncio.run(_show_area(session, db, area, alice))

    assert "/upload" not in session.output
    db.close()


def test_min_age_gate_allows_upload_hint_once_met(tmp_path):
    from datetime import date

    from netbbs.attestation import set_birthdate

    db = Database(tmp_path / "node.db")
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    set_birthdate(db, alice, date(1990, 1, 1))
    area = create_file_area(db, "adults", creator=alice, min_age=18)
    upload_file(db, area, alice, "file.txt", b"hello")
    session = FakeSession(lines=["b"])

    asyncio.run(_show_area(session, db, area, alice))

    assert "/upload" in session.output
    db.close()


def test_name_requirement_hides_the_upload_hint_when_unmet(tmp_path):
    db = Database(tmp_path / "node.db")
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    area = create_file_area(db, "verified-only", creator=alice, name_requirement="verified")
    upload_file(db, area, alice, "file.txt", b"hello")
    session = FakeSession(lines=["b"])

    asyncio.run(_show_area(session, db, area, alice))

    assert "/upload" not in session.output
    db.close()
