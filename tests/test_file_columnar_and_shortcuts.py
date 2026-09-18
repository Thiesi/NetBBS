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
    AUTHOR_COLOR,
    DATE_COLOR,
    EMPHASIS_COLOR,
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
    def __init__(self, keys=None, lines=None, width=80, height=24):
        self._keys = iter(keys or [])
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

    async def read_key(self, echo: bool = True) -> str:
        # Raises rather than returning "" forever: a key the file
        # listing does not handle changes nothing, so a fake that never
        # runs out would spin the loop belling instead of failing.
        key = next(self._keys, None)
        if key is None:
            raise AssertionError("FakeSession.read_key() called with no more scripted keys")
        return key

    async def read_line(self, echo: bool = True, **kwargs) -> str:
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
    def __init__(self, editor_keys=None, keys=None, lines=None, width=80, height=24):
        super().__init__(keys=keys, lines=lines, width=width, height=height)
        self._editor_keys = iter(editor_keys or [])

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False) -> EditorKey:
        try:
            return next(self._editor_keys)
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
    session = FakeSession(keys=["b"], width=80)
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

    session = FakeSession(keys=["b"], width=80)
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, alice))

    # Full verified name displayed without truncation
    assert "(=Alice Wonderland=)" in session.output

    lane.close()
    db.close()


# -- Row presentation (dogfood feedback) --


def test_each_column_of_a_row_is_separately_colored(tmp_path, monkeypatch):
    """Dogfood feedback: "descriptions are barely readable, file sizes and
    uploader names are better".

    They were better because there were only ever three shades on the row
    -- VALUE_COLOR for the size, METADATA_COLOR for the date, MUTED_COLOR
    for the description -- plus an uploader column with no color at all,
    inheriting whatever the caller's terminal defaults to. Five fields now
    read as five fields.
    """
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, user = _setup_area(db, count=2, monkeypatch=monkeypatch)
    session = FakeSession(keys=["b"], width=80)
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))
    output = session.output

    # The size, right-aligned in its column and now the brightest field
    # on the row -- it is the figure a caller compares down the column.
    assert f"\x1b[38;5;{EMPHASIS_COLOR}m" in output
    # The date and the uploader, each its own hue rather than a grey and
    # the terminal default.
    assert f"\x1b[38;5;{DATE_COLOR}m" in output
    assert f"\x1b[38;5;{AUTHOR_COLOR}m" in output
    # The uploader is no longer the one field on the row with no color.
    assert f"\x1b[38;5;{AUTHOR_COLOR}malice" in output

    # The description, lifted off the muted floor onto the shade the size
    # and uploader used to have.
    assert colored("Package 0 archive release.", fg_color=VALUE_COLOR) in output
    assert colored("Package 0 archive release.", fg_color=MUTED_COLOR) not in output

    lane.close()
    db.close()


def test_the_highlighted_row_is_a_reverse_video_bar(tmp_path, monkeypatch):
    """Dogfood feedback: "the cursor is small, and the color change
    highlighting the selected row barely noticeable, not least because it
    uses the same color as some elements of the line do".

    It was the accent color the filename already carried, so the only
    thing separating a highlighted row from its neighbours was bold.
    """
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, user = _setup_area(db, count=3, monkeypatch=monkeypatch)
    session = FakeInteractiveSession(editor_keys=[EditorKey(EditorKeyKind.DOWN)])
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))
    output = session.output
    reverse = "\x1b[7m"

    rows = [line for line in output.split(chr(10)) if ">[ 1]" in line]
    assert rows, "the highlighted row was never drawn"
    bar = rows[-1]
    assert bar.startswith(reverse), bar
    # One inverted run for the whole row, not a reversed cell beside
    # colored ones: nothing on the row sets a foreground color, and the
    # filename's own accent is gone while it is under the cursor.
    assert bar.count(reverse) == 1, bar
    assert "\x1b[38;5;" not in bar, bar
    assert "pkg0.tar.gz" in bar

    # The rows either side of it are untouched, so the bar reads as one
    # row rather than a change of theme.
    others = [line for line in output.split(chr(10)) if " [ 2]" in line]
    assert others and reverse not in others[-1], others

    lane.close()
    db.close()


def test_a_verified_uploader_does_not_stripe_the_highlighted_bar(tmp_path, monkeypatch):
    """Codex review. `format_name_for_resource` returns the `(=...=)`
    unit already wrapped in VERIFIED_COLOR and terminated by its own
    reset -- and an SGR reset ends whatever run it lands inside. Nesting
    that in one outer reverse span stopped the bar halfway along the
    uploader column, which is the striped highlight this change exists to
    remove.
    """
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    area = create_file_area(db, "verified_docs", creator=alice, name_requirement="verified_and_displayed")
    upload_file(db, area, alice, "release.zip", b"zip data")
    attest_name(db, alice, "Alice Wonderland", verifier=sysop)

    session = FakeInteractiveSession(editor_keys=[EditorKey(EditorKeyKind.DOWN)], width=100)
    lane = DatabaseLane(db_path)
    asyncio.run(_show_area(session, lane, area, alice))
    lane.close()
    db.close()

    rows = [line for line in session.output.split(chr(10)) if ">[ 1]" in line]
    assert rows, "the highlighted row was never drawn"
    bar = rows[-1]
    # One inverted run, opened once and closed once: no colour of any
    # kind survives inside it, so nothing can end it early.
    assert bar.count("\x1b[7m") == 1, bar
    assert "\x1b[38;5;" not in bar, bar
    assert bar.count("\x1b[0m") == 1, bar
    # The verified name is still legible under the cursor, markers and
    # all -- `set_display_name` refuses `=` at write time, so the unit is
    # still unforgeable without its colour.
    assert "(=Alice Wonderland=)" in bar, bar


# -- Numbered Download Shortcuts (no editor-key support) --
#
# These drive the `read_key()` fallback path: one keystroke per action,
# exactly the keys the cursor path below answers to. The screen used to
# read whole typed lines here and also carried `#<n>`, `/download <n>`
# and a `d <n>` alias; those forms are gone (design doc §3.5), and
# `[D]` is the keystroke that names a file without typing its number.


def test_download_via_direct_number_shortcut(tmp_path, monkeypatch):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, user = _setup_area(db, count=2, monkeypatch=monkeypatch)
    # Pressing '1' downloads the 1st file on the page (pkg0.tar.gz)
    session = FakeSession(keys=["1"])
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    assert "Starting Zmodem send of 'pkg0.tar.gz'" in session.output

    lane.close()
    db.close()


def test_download_via_second_number_shortcut(tmp_path, monkeypatch):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, user = _setup_area(db, count=2, monkeypatch=monkeypatch)
    # Pressing '2' downloads the 2nd file on the page (pkg1.tar.gz)
    session = FakeSession(keys=["2"])
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    assert "Starting Zmodem send of 'pkg1.tar.gz'" in session.output

    lane.close()
    db.close()


def test_download_key_on_a_single_file_page_needs_no_number(tmp_path, monkeypatch):
    """`[D]` on a page holding one file means that file: there is
    nothing to disambiguate, so no picker and no prompt. This is what
    the `#1`/`/download 1` typed forms were for."""
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, user = _setup_area(db, count=1, monkeypatch=monkeypatch)
    session = FakeSession(keys=["d"])
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    assert "Starting Zmodem send of 'pkg0.tar.gz'" in session.output

    lane.close()
    db.close()


def test_download_key_on_a_multi_file_page_picks_through_the_picker(tmp_path, monkeypatch):
    """`[D]` with several files and no cursor asks which one the way
    this codebase asks any "which one?" question -- `pick_item`, whose
    own two-digit selection ("01") names the first entry. The replaced
    `d 1` alias answered the same question inline."""
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, user = _setup_area(db, count=2, monkeypatch=monkeypatch)
    session = FakeSession(keys=["d", "0", "1"])
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    assert "Starting Zmodem send of 'pkg0.tar.gz'" in session.output

    lane.close()
    db.close()


def test_download_key_backed_out_of_the_picker_downloads_nothing(tmp_path, monkeypatch):
    """Backing out of the picker is not a refusal: the listing comes
    back (it was drawn over) and nothing is sent."""
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, user = _setup_area(db, count=2, monkeypatch=monkeypatch)
    session = FakeSession(keys=["d", "b", "b"])
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    assert "Starting Zmodem send" not in session.output

    lane.close()
    db.close()


def test_download_out_of_range_number_beeps_and_stays(tmp_path, monkeypatch):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, user = _setup_area(db, count=2, monkeypatch=monkeypatch)
    # '9' is out of range for a two-file page; then 'b' to back out
    session = FakeSession(keys=["9", "b"])
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    assert "\a" in session.output
    assert "Starting Zmodem send" not in session.output

    lane.close()
    db.close()


@pytest.mark.parametrize("char", ["²", "٣"])
def test_a_non_ascii_digit_key_is_refused_rather_than_crashing(tmp_path, monkeypatch, char):
    """`str.isdigit()` is true for two kinds of character this screen
    must not treat as a file number, and they fail differently:

    - `'²'` (AltGr+2 on a German keyboard, so a key a caller really
      presses): `isdigit()` is true but `isdecimal()` is false and
      `int('²')` raises `ValueError`, which nothing on the read path
      catches -- it left the screen through `_show_area`.
    - `'٣'` (Arabic-Indic three): `int()` accepts it as `3`, so the
      screen would have started a transfer of the third file for a key
      its own `1-3` hint never offered.

    Both arrive as an `EditorKeyKind.CHAR`, the path a real terminal
    takes, and both must land on the same bell every other unhandled
    key gets."""
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, user = _setup_area(db, count=3, monkeypatch=monkeypatch)
    session = FakeInteractiveSession(
        editor_keys=[
            EditorKey(EditorKeyKind.CHAR, char=char),
            EditorKey(EditorKeyKind.CHAR, char="b"),
        ]
    )
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
    session = FakeSession(keys=["b"])
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    # Shows the "1-3" number range plus the [D]ownload key that
    # replaced the typed `/download` form.
    assert "1-3" in session.visible_output
    assert "[D]ownload" in session.visible_output
    assert "/download" not in session.visible_output

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


def test_interactive_download_key_acts_on_the_cursor_entry(tmp_path, monkeypatch):
    """`[D]` with a cursor on the page means the file under it -- no
    number, no picker. Enter does the same thing; this is the hotkey
    half of it, and between them they cover what `/download <n>` and
    `/download <name>` used to do on the page in front of the caller."""
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, user = _setup_area(db, count=3, monkeypatch=monkeypatch)

    keys = [
        EditorKey(EditorKeyKind.DOWN),  # highlights row 0 (pkg0)
        EditorKey(EditorKeyKind.DOWN),  # highlights row 1 (pkg1)
        EditorKey(EditorKeyKind.CHAR, char="d"),
    ]
    session = FakeInteractiveSession(editor_keys=keys)
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

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
