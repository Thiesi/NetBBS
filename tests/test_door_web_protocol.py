"""Door mode is separate from menu input, including on broken connections."""
import asyncio
import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from netbbs.net.session import SessionClosedError
from netbbs.net.web import WebSession


class Socket:
    closed = False

    def __init__(self):
        self.sent = []
        self.done = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self.done.wait()
        raise StopAsyncIteration

    async def send_json(self, value):
        self.sent.append(value)

    async def close(self, **kwargs):
        self.closed = True
        self.done.set()


def test_web_mode_raw_sequences_stale_keys_bounds_and_restoration():
    async def scenario():
        ws = Socket()
        session = WebSession(ws)
        try:
            await session._handle_event({"type": "key", "data": "old menu input"})
            await session.enter_door_mode(encoding="cp437", width=80, height=25)
            stream = ws.sent[-1]["stream"]
            assert session._char_queue.empty()
            await session._handle_event({"type": "key", "data": "ignored"})
            await session._handle_event({"type": "door_key", "stream": stream - 1, "data": "stale"})
            await session._handle_event({"type": "resize", "cols": 100, "rows": 40})
            assert session._door_queue.empty()
            data = "é\x1b[A\r\x00"
            await session._handle_event({"type": "door_key", "stream": stream, "data": data})
            assert bytes([await session.read_byte() for _ in data.encode()]) == data.encode()
            payload = b"x" * 4095 + "█".encode() + b"\x1b[0m"
            await session.write_raw(payload)
            frames = [v for v in ws.sent if v["type"] == "door_output"]
            assert b"".join(base64.b64decode(v["data"]) for v in frames) == payload
            assert all(len(base64.b64decode(v["data"])) <= 4096 for v in frames)
            await session._handle_event({"type": "door_key", "stream": stream, "data": "Qleftover"})
            await session.leave_door_mode()
            assert session._door_queue.empty()
            await session._handle_event({"type": "door_key", "stream": stream, "data": "B"})
            await session._handle_event({"type": "key", "data": "A"})
            assert await session.read_key() == "A"
            with pytest.raises(NotImplementedError):
                await session.write_raw(b"zmodem")
            await session.enter_door_mode()
            current = ws.sent[-1]["stream"]
            assert current > stream
            with pytest.raises(SessionClosedError):
                for _ in range(3):
                    await session._handle_event({"type": "door_key", "stream": current, "data": "x" * 4096})
            assert ws.closed
            await session.leave_door_mode()
            with pytest.raises(SessionClosedError):
                await asyncio.wait_for(session.read_key(), 1)
        finally:
            await session.close()
    asyncio.run(scenario())


def test_browser_shim_executes_streaming_decoder_and_mode_changes():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required for JavaScript shim execution")
    root = Path(__file__).resolve().parent.parent
    subprocess.run([node, str(root / "tests/fixtures/door_web_shim.cjs"),
                    str(root / "src/netbbs/web/static/netbbs-terminal.js")], check=True, timeout=15)
