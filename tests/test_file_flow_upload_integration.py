"""
End-to-end regression test for GitHub issue #34, reopened a second
time: the real `/upload` path (`netbbs.net.file_flow._handle_upload`)
now streams received Zmodem content to a temp file
(`netbbs.files.storage.new_incoming_temp_path`) and moves it into
permanent storage (`netbbs.files.entries.upload_file_from_temp`) rather
than ever holding the complete upload as one in-memory `bytes` object.

`netbbs.net.zmodem`'s own test suite already exercises the protocol
layer directly (`receive_file` against a real sender, in isolation);
this drives the real `_show_area`/`_handle_upload` menu flow on top of
that, the same "menu text, then the session's raw byte stream, on the
same session object" combination a real Telnet/SSH connection actually
has -- proving the whole chain (menu -> real Zmodem handshake -> temp
file -> content-addressed storage -> a queryable FileEntry) works
together, not just each piece in isolation.
"""

from __future__ import annotations

import asyncio
import collections
from pathlib import Path

import pytest

from netbbs.auth.users import create_user
from netbbs.files.areas import create_file_area
from netbbs.files.entries import list_files_page
from netbbs.files.storage import storage_root
from netbbs.net import file_flow
from netbbs.net import zmodem
from netbbs.net.session import Session
from netbbs.storage.database import Database


class _BytePipe:
    """Same shape as tests/test_zmodem.py's own -- a real client task
    driving zmodem.send_file against a real server task running
    zmodem.receive_file (via _handle_upload), connected by an in-memory
    duplex byte pipe rather than a socket."""

    def __init__(self):
        self._buffer: collections.deque[int] = collections.deque()
        self._event = asyncio.Event()

    def feed(self, data: bytes) -> None:
        self._buffer.extend(data)
        self._event.set()

    async def read_byte(self) -> int:
        while not self._buffer:
            self._event.clear()
            await self._event.wait()
        return self._buffer.popleft()


class _ServerSession(Session):
    """The NetBBS-side session: `read_line`/`write`/`write_line` drive
    _show_area's ordinary menu text, `read_byte`/`write_raw` (backed by
    a real duplex byte pipe) carry the Zmodem exchange once /upload
    switches the session into raw mode -- both live on the same session
    object, matching a real transport."""

    def __init__(self, lines: list[str], read_pipe: _BytePipe, write_pipe: _BytePipe):
        self._lines = list(lines)
        self._read_pipe = read_pipe
        self._write_pipe = write_pipe
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_line(self, echo: bool = True) -> str:
        return self._lines.pop(0) if self._lines else ""

    async def read_key(self, echo: bool = True) -> str:
        raise NotImplementedError

    async def read_editor_key(self):
        raise NotImplementedError

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        return await self._read_pipe.read_byte()

    async def write_raw(self, data: bytes) -> None:
        self._write_pipe.feed(data)


class _ClientSession(Session):
    """The simulated sending terminal's side of the same byte pipes --
    only what zmodem.send_file itself needs."""

    def __init__(self, read_pipe: _BytePipe, write_pipe: _BytePipe):
        self._read_pipe = read_pipe
        self._write_pipe = write_pipe

    async def write(self, text: str) -> None:
        raise NotImplementedError

    async def write_line(self, text: str = "") -> None:
        raise NotImplementedError

    async def read_line(self, echo: bool = True) -> str:
        raise NotImplementedError

    async def read_key(self, echo: bool = True) -> str:
        raise NotImplementedError

    async def read_editor_key(self):
        raise NotImplementedError

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        return await self._read_pipe.read_byte()

    async def write_raw(self, data: bytes) -> None:
        self._write_pipe.feed(data)


def _written_text(session: _ServerSession) -> str:
    return "".join(session.written)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


def test_upload_via_show_area_streams_to_storage_with_no_leftover_temp_file(db, alice):
    area = create_file_area(db, "docs", creator=alice)
    payload = b"hello from a real zmodem upload" * 100  # spans multiple subpackets

    client_to_server, server_to_client = _BytePipe(), _BytePipe()
    server_session = _ServerSession(["/upload"], read_pipe=client_to_server, write_pipe=server_to_client)
    client_session = _ClientSession(read_pipe=server_to_client, write_pipe=client_to_server)

    async def scenario():
        server_task = asyncio.create_task(file_flow._show_area(server_session, db, area, alice))
        client_task = asyncio.create_task(zmodem.send_file(client_session, "upload.bin", payload))
        await asyncio.wait_for(server_task, timeout=5)
        try:
            await asyncio.wait_for(client_task, timeout=1)
        except Exception:
            pass  # the client side tearing down after the transfer completes isn't under test here

    asyncio.run(scenario())

    assert "Uploaded" in _written_text(server_session)
    page = list_files_page(db, area, alice)
    assert len(page.entries) == 1
    entry = page.entries[0]
    assert entry.filename == "upload.bin"
    assert entry.size_bytes == len(payload)
    assert entry.storage_path
    assert Path(entry.storage_path).read_bytes() == payload

    # GitHub issue #34's actual point: nothing left behind in staging.
    incoming_dir = storage_root(db) / ".incoming"
    assert not incoming_dir.exists() or list(incoming_dir.iterdir()) == []


def test_upload_exceeding_the_node_limit_leaves_no_temp_file_and_no_entry(db, alice, monkeypatch):
    monkeypatch.setattr(file_flow, "get_max_upload_bytes", lambda db: 10)

    area = create_file_area(db, "docs", creator=alice)
    payload = b"x" * 1000

    client_to_server, server_to_client = _BytePipe(), _BytePipe()
    server_session = _ServerSession(["/upload"], read_pipe=client_to_server, write_pipe=server_to_client)
    client_session = _ClientSession(read_pipe=server_to_client, write_pipe=client_to_server)

    async def scenario():
        server_task = asyncio.create_task(file_flow._show_area(server_session, db, area, alice))
        client_task = asyncio.create_task(zmodem.send_file(client_session, "toobig.bin", payload))
        await asyncio.wait_for(server_task, timeout=5)
        try:
            await asyncio.wait_for(client_task, timeout=1)
        except Exception:
            pass

    asyncio.run(scenario())

    assert "Upload failed" in _written_text(server_session)
    assert list_files_page(db, area, alice).entries == []
    incoming_dir = storage_root(db) / ".incoming"
    assert not incoming_dir.exists() or list(incoming_dir.iterdir()) == []
