"""The file listing's prompt belongs to a render, not to a keystroke
(issue #527).

A caller leaning on Enter in a file area used to get

    Choice or command: Choice or command: Choice or command: ...

marching across the line, because the key reader wrote the prompt
itself and the loop called it once per keystroke. These tests hold the
two halves of the rule apart: a key that changes nothing leaves the
screen exactly as it was, and an action whose own echo consumed the
prompt line puts it back.
"""

from __future__ import annotations

import asyncio
import re

from netbbs.auth.users import create_user
from netbbs.files import entries as entries_module
from netbbs.files.areas import create_file_area
from netbbs.files.entries import upload_file
from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.file_flow import _CHOICE_PROMPT, _show_area
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


class FakeInteractiveSession(FakeSession):
    def __init__(self, editor_keys=None, lines=None, width=80, height=24):
        super().__init__(lines=lines, width=width, height=height)
        self._keys = iter(editor_keys or [])

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False) -> EditorKey:
        try:
            return next(self._keys)
        except StopIteration:
            # Leave the screen rather than spinning, however the test ends.
            return EditorKey(EditorKeyKind.CHAR, char="b")


def _prompt_count(session: FakeSession) -> int:
    return session.output.count(_CHOICE_PROMPT)


def _setup_area(db, count: int = 2, monkeypatch=None):
    user = create_user(db, "alice", password="hunter2", user_level=10)
    area = create_file_area(db, "downloads", creator=user)
    if monkeypatch:
        timestamps = iter(f"2026-01-01T00:00:{i:02d}.000000Z" for i in range(count))
        monkeypatch.setattr(entries_module, "utc_now_iso", lambda: next(timestamps))
    for i in range(count):
        upload_file(db, area, user, f"pkg{i}.tar.gz", f"file payload {i}".encode())
    return area, user


def test_unhandled_keys_do_not_reprint_the_prompt(tmp_path, monkeypatch):
    """The reported bug. Nothing echoed, nothing changed, so the one
    prompt already on screen is still the live one."""
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, user = _setup_area(db, monkeypatch=monkeypatch)

    # read_editor_key does not echo, so each of these leaves the cursor
    # sitting exactly where the prompt left it.
    keys = [EditorKey(EditorKeyKind.BACKSPACE) for _ in range(6)]
    keys.append(EditorKey(EditorKeyKind.CHAR, char="b"))
    session = FakeInteractiveSession(editor_keys=keys)
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    assert _prompt_count(session) == 1
    # Each rejection is still audible.
    assert session.output.count("\a") == 6

    lane.close()
    db.close()


def test_rejected_keys_never_run_prompts_together_on_one_line(tmp_path, monkeypatch):
    """Guards the exact visual the report showed, independently of the
    count above: two prompts must never end up adjacent."""
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, user = _setup_area(db, monkeypatch=monkeypatch)

    keys = [EditorKey(EditorKeyKind.BACKSPACE) for _ in range(4)]
    keys.append(EditorKey(EditorKeyKind.CHAR, char="b"))
    session = FakeInteractiveSession(editor_keys=keys)
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    # What the caller actually sees: SGR sequences draw nothing, and a
    # bell occupies no column, so two prompts separated only by those
    # are adjacent on screen even though they are not in the byte
    # stream. That is precisely how the reported screenshot happened.
    plain = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", session.output).replace("\a", "")
    assert _CHOICE_PROMPT + _CHOICE_PROMPT not in plain

    lane.close()
    db.close()


def test_nav_key_refused_at_the_edge_reprints_the_prompt(tmp_path, monkeypatch):
    """The other half of the rule: `o` echoes itself and a newline
    before this screen discovers there is no older page, so the prompt
    it scrolled away has to come back."""
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, user = _setup_area(db, monkeypatch=monkeypatch)

    keys = [
        EditorKey(EditorKeyKind.CHAR, char="o"),  # no older page exists
        EditorKey(EditorKeyKind.CHAR, char="b"),
    ]
    session = FakeInteractiveSession(editor_keys=keys)
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    # One from the initial render, one put back after the refusal.
    assert _prompt_count(session) == 2
    assert "\a" in session.output

    lane.close()
    db.close()


def test_unknown_typed_command_reprints_the_prompt(tmp_path, monkeypatch):
    """A typed line is a deliberate act that failed on its own terms,
    not a stray keystroke: Enter consumed the prompt line, so the
    caller is owed a fresh one to type the next thing at."""
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, user = _setup_area(db, monkeypatch=monkeypatch)

    # No editor-key support at all: the read_line() fallback path.
    session = FakeSession(lines=["frobnicate", "b"])
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    assert _prompt_count(session) == 2

    lane.close()
    db.close()


def test_every_redraw_carries_its_own_prompt(tmp_path, monkeypatch):
    """A real state change redraws, and the prompt rides along with the
    render rather than being written separately afterwards."""
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    area, user = _setup_area(db, monkeypatch=monkeypatch)

    keys = [
        EditorKey(EditorKeyKind.DOWN),  # highlight row 1 -> redraw
        EditorKey(EditorKeyKind.DOWN),  # highlight row 2 -> redraw
        EditorKey(EditorKeyKind.ESCAPE),  # drop the highlight -> redraw
        EditorKey(EditorKeyKind.CHAR, char="b"),
    ]
    session = FakeInteractiveSession(editor_keys=keys)
    lane = DatabaseLane(db_path)

    asyncio.run(_show_area(session, lane, area, user))

    # Initial render plus three redraws.
    assert _prompt_count(session) == 4

    lane.close()
    db.close()
