"""Tests for columnar file directory listing layout and numbered download shortcuts (issue #184)."""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.attestation import attest_name
from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.files import entries as entries_module
from netbbs.files.areas import create_file_area
from netbbs.files.entries import upload_file
from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.file_flow import _file_column_widths, _show_area
from netbbs.rendering import (
    HEADER_COLOR,
    MENU_KEY_COLOR,
    MUTED_COLOR,
    VALUE_COLOR,
    colored,
    visible_width,
)
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


class FakeSession:
    def __init__(self, lines=None, width=80, height=24):
        self._lines = iter(lines or [])
        self.written: list[str] = []
        self.terminal_width = width
        self.terminal_height = height
        self.node_display_name = "NetBBS"
        self.node_name_gradient = None
        self.peer_address = "203.0.113.5"
        self.supports_truecolor = False

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_line(self, echo: bool = True) -> str:
        return next(self._lines, "")

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError("write_raw not supported by FakeSession")

    async def read_byte(self):
        raise NotImplementedError("read_byte not supported by FakeSession")

    @property
    def output(self) -> str:
        return "".join(self.written)

    @property
    def visible_output(self) -> str:
        return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", self.output)


class FakeInteractiveSession(FakeSession):
    def __init__(self, editor_keys=None, lines=None, width=80, height=24):
        super().__init__(lines=lines, width=width, height=height)
        self._keys = iter(editor_keys or [])

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False) -> EditorKey:
        try:
            return next(self._keys)
        except StopIteration:
            return EditorKey(EditorKeyKind.CHAR, char="b")


def _setup_area(db, count: int = 3, monkeypatch = None):
    user = create_user(db, "alice", password="hunter2", user_level=10)
    area = create_file_area(db, "downloads", creator=user)
    if monkeypatch:
        timestamps = iter(f"2026-01-01T00:00:{i:02d}.000000Z" for i in range(count))
        monkeypatch.setattr(entries_module, "utc_now_iso", lambda: next(timestamps))
    for i in range(count):
        upload_file(
            db, area, user, f"pkg{i}.tar.gz", f"file payload {i}".encode(),
            description=f"Package {i} archive release." if i % 2 == 0 else None,
        )
    return area, user


# -- Column Width Calculations --


def test_column_widths_geometry_on_standard_80_col():
    idx_w, name_w, size_w, date_w, uploader_w = _file_column_widths(80)
    assert idx_w == 4
    assert name_w == 18
    assert size_w == 9
    assert date_w == 16
    assert uploader_w == 28
    # Total with 1-char gutter between 5 columns: 4 + 1 + 18 + 1 + 9 + 1 + 16 + 1 + 28 = 79 <= 80
    total = idx_w + 1 + name_w + 1 + size_w + 1 + date_w + 1 + uploader_w
    assert total <= 80


def test_column_widths_geometry_on_wide_terminals():
    idx_w, name_w, size_w, date_w, uploader_w = _file_column_widths(100)
    assert idx_w == 4
    assert name_w > 20
    assert uploader_w > 26
    total = idx_w + 1 + name_w + 1 + size_w + 1 + date_w + 1 + uploader_w
    assert total <= 100

    idx_w, name_w, size_w, date_w, uploader_w = _file_column_widths(120)
    total = idx_w + 1 + name_w + 1 + size_w + 1 + date_w + 1 + uploader_w
    assert total <= 120


def test_column_widths_geometry_on_narrow_terminals():
    idx_w, name_w, size_w, date_w, uploader_w = _file_column_widths(70)
    assert idx_w == 4
    assert size_w == 9
    assert date_w == 16
    total = idx_w + 1 + name_w + 1 + size_w + 1 + date_w + 1 + uploader_w
    assert total <= 70


# -- Columnar Header & Directory Layout --


def test_columnar_headers_and_dividers_rendered(tmp_path, monkeypatch):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, user = _setup_area(db, count=2, monkeypatch=monkeypatch)
    session = FakeSession(lines=["b"], width=80)
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    # Header columns present in output
    assert "Filename" in session.output
    assert "Size" in session.output
    assert "Date" in session.output
    assert "Uploader" in session.output

    # Divider row present with rules
    assert "----" in session.output or "────" in session.output

    # File rows formatted with brackets [ 1], [ 2]
    assert "[ 1]" in session.output
    assert "[ 2]" in session.output
    assert "pkg0.tar.gz" in session.output
    assert "pkg1.tar.gz" in session.output

    # Indented description present for files with descriptions
    assert "Package 0 archive release." in session.output

    lane.close()
    db.close()


def test_columnar_verified_name_display_no_truncation(tmp_path, monkeypatch):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    area = create_file_area(db, "verified_docs", creator=alice, name_requirement="verified_and_displayed")
    upload_file(db, area, alice, "release.zip", b"zip data")
    attest_name(db, alice, "Alice Wonderland", verifier=sysop)

    session = FakeSession(lines=["b"], width=80)
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, alice))

    # Full verified name displayed without truncation
    assert "(=Alice Wonderland=)" in session.output

    lane.close()
    db.close()


# -- Numbered Download Shortcuts (Line mode) --


def test_download_via_direct_number_shortcut(tmp_path, monkeypatch):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, user = _setup_area(db, count=2, monkeypatch=monkeypatch)
    # Typing '1' directly downloads the 1st file on the page (pkg0.tar.gz)
    session = FakeSession(lines=["1"])
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    assert "Starting Zmodem send of 'pkg0.tar.gz'" in session.output

    lane.close()
    db.close()


def test_download_via_second_number_shortcut(tmp_path, monkeypatch):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, user = _setup_area(db, count=2, monkeypatch=monkeypatch)
    # Typing '2' downloads the 2nd file on the page (pkg1.tar.gz)
    session = FakeSession(lines=["2"])
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    assert "Starting Zmodem send of 'pkg1.tar.gz'" in session.output

    lane.close()
    db.close()


def test_download_via_hash_number_shortcut(tmp_path, monkeypatch):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, user = _setup_area(db, count=2, monkeypatch=monkeypatch)
    # Typing '#1' downloads the 1st file
    session = FakeSession(lines=["#1"])
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    assert "Starting Zmodem send of 'pkg0.tar.gz'" in session.output

    lane.close()
    db.close()


def test_download_via_download_number(tmp_path, monkeypatch):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, user = _setup_area(db, count=2, monkeypatch=monkeypatch)
    # Typing '/download 1' downloads the 1st file on the page
    session = FakeSession(lines=["/download 1"])
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    assert "Starting Zmodem send of 'pkg0.tar.gz'" in session.output

    lane.close()
    db.close()


def test_download_via_d_shortcut_command(tmp_path, monkeypatch):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, user = _setup_area(db, count=2, monkeypatch=monkeypatch)
    # Typing 'd 1' downloads the 1st file
    session = FakeSession(lines=["d 1"])
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    assert "Starting Zmodem send of 'pkg0.tar.gz'" in session.output

    lane.close()
    db.close()


def test_download_out_of_range_number_beeps_and_stays(tmp_path, monkeypatch):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, user = _setup_area(db, count=2, monkeypatch=monkeypatch)
    # '99' is out of range; then 'b' to back out
    session = FakeSession(lines=["99", "b"])
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    assert "\a" in session.output
    assert "Starting Zmodem send" not in session.output

    lane.close()
    db.close()


def test_download_hints_reflect_page_count(tmp_path, monkeypatch):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, user = _setup_area(db, count=3, monkeypatch=monkeypatch)
    session = FakeSession(lines=["b"])
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    # Shows "1-3 or /download" hint
    assert "1-3" in session.output
    assert "/download" in session.output

    lane.close()
    db.close()


# -- Interactive Arrow Key Navigation & Enter Download --


def test_interactive_arrow_highlight_and_enter_download(tmp_path, monkeypatch):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, user = _setup_area(db, count=3, monkeypatch=monkeypatch)

    # Sequence of keys:
    # 1. DOWN -> highlights row 0 (file 1)
    # 2. DOWN -> highlights row 1 (file 2)
    # 3. ENTER -> downloads highlighted file 2 (pkg1.tar.gz)
    keys = [
        EditorKey(EditorKeyKind.DOWN),
        EditorKey(EditorKeyKind.DOWN),
        EditorKey(EditorKeyKind.ENTER),
    ]
    session = FakeInteractiveSession(editor_keys=keys)
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    # Highlight marker appears
    assert ">[ 1]" in session.output
    assert ">[ 2]" in session.output
    assert "Starting Zmodem send of 'pkg1.tar.gz'" in session.output

    lane.close()
    db.close()


def test_interactive_escape_cancels_highlight(tmp_path, monkeypatch):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, user = _setup_area(db, count=2, monkeypatch=monkeypatch)

    # 1. DOWN -> highlights row 0
    # 2. ESCAPE -> cancels highlight
    # 3. CHAR 'b' -> exit
    keys = [
        EditorKey(EditorKeyKind.DOWN),
        EditorKey(EditorKeyKind.ESCAPE),
        EditorKey(EditorKeyKind.CHAR, char="b"),
    ]
    session = FakeInteractiveSession(editor_keys=keys)
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    assert ">[ 1]" in session.output
    # Did not download anything
    assert "Starting Zmodem send" not in session.output

    lane.close()
    db.close()


def test_interactive_single_digit_direct_download(tmp_path, monkeypatch):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, user = _setup_area(db, count=2, monkeypatch=monkeypatch)

    # Pressing character '1' directly downloads file 1
    keys = [
        EditorKey(EditorKeyKind.CHAR, char="1"),
    ]
    session = FakeInteractiveSession(editor_keys=keys)
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    assert "Starting Zmodem send of 'pkg0.tar.gz'" in session.output

    lane.close()
    db.close()
