"""
Interoperability tests for netbbs.net.zmodem against real lrzsz (issue
#963).

`tests/test_zmodem.py` exercises the protocol against NetBBS's own other
half and against hand-written lrzsz-style byte sequences. These run the
real `sz` and `rz` binaries as subprocesses, their stdin/stdout standing
in for the caller's terminal, so a difference between what NetBBS
believes Zmodem is and what an actual implementation does shows up here.

Skipped, visibly, when lrzsz isn't installed (Debian/Ubuntu: `apt install
lrzsz`; NetBSD: `pkgin install lrzsz`).
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from pathlib import Path

import pytest

from netbbs.net import zmodem
from netbbs.net.session import Session, SessionClosedError

SZ = shutil.which("sz") or shutil.which("lsz")
RZ = shutil.which("rz") or shutil.which("lrz")

pytestmark = pytest.mark.skipif(
    SZ is None or RZ is None, reason="lrzsz (sz/rz) is not installed: interop with a real Zmodem not checked"
)

_AWKWARD = bytes([0xFF, 0x18, 0x11, 0x13, 0x91, 0x93, 0x0D, 0x0A, 0x2A, 0x7F, 0x00, 0x10, 0x90])


class _ProcessSession(Session):
    """A Session whose caller is an lrzsz process: what NetBBS writes goes
    to its stdin, what it writes back is read from its stdout."""

    def __init__(self, process: asyncio.subprocess.Process):
        self._process = process

    async def write(self, text: str) -> None:
        await self.write_raw(text.encode())

    async def write_raw(self, data: bytes) -> None:
        try:
            self._process.stdin.write(data)
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise SessionClosedError("lrzsz exited") from exc

    async def read_byte(self) -> int | None:
        b = await self._process.stdout.read(1)
        if not b:
            raise SessionClosedError("lrzsz exited")
        return b[0]

    async def read_line(self, echo: bool = True, **kwargs) -> str:
        raise NotImplementedError

    async def read_key(self, echo: bool = True) -> str:
        raise NotImplementedError

    async def read_editor_key(self):
        raise NotImplementedError

    async def close(self) -> None:
        pass


async def _spawn(*args: str, cwd: Path) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _finish(process: asyncio.subprocess.Process, timeout: float = 20) -> int:
    try:
        return await asyncio.wait_for(process.wait(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise


def _download(tmp_path: Path, payload: bytes, name: str = "payload.bin") -> Path:
    """NetBBS sends, `rz` receives into `tmp_path`."""

    async def scenario():
        rz = await _spawn(RZ, "-q", "-y", cwd=tmp_path)
        await zmodem.send_file(_ProcessSession(rz), name, payload)
        assert await _finish(rz) == 0, (await rz.stderr.read()).decode(errors="replace")

    asyncio.run(scenario())
    return tmp_path / name


def _upload(tmp_path: Path, payload: bytes, *sz_options: str, max_bytes: int = 10_000_000):
    """`sz` sends a file holding `payload`, NetBBS receives it."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "upload me.bin").write_bytes(payload)
    dest = tmp_path / "incoming"

    async def scenario():
        sz = await _spawn(SZ, "-q", *sz_options, "upload me.bin", cwd=source)
        try:
            result = await zmodem.receive_file(_ProcessSession(sz), max_bytes=max_bytes, dest_path=dest)
        except zmodem.ZmodemError:
            code = await _finish(sz)
            raise AssertionError(f"transfer failed; sz exited {code}") from None
        assert await _finish(sz) == 0, (await sz.stderr.read()).decode(errors="replace")
        return result

    return asyncio.run(scenario()), dest


def test_download_to_rz(tmp_path):
    payload = b"hello from NetBBS\r\n" * 10
    assert _download(tmp_path, payload).read_bytes() == payload


def test_download_of_a_megabyte_with_awkward_bytes_to_rz(tmp_path):
    payload = (_AWKWARD + bytes(range(256))) * 3800
    assert len(payload) > 1_000_000
    assert _download(tmp_path, payload).read_bytes() == payload


def test_download_of_an_empty_file_to_rz(tmp_path):
    assert _download(tmp_path, b"").read_bytes() == b""


def test_upload_from_sz(tmp_path):
    payload = b"hello from lrzsz\n" * 10
    result, dest = _upload(tmp_path, payload)
    assert dest.read_bytes() == payload
    assert result.filename == "upload me.bin"
    assert result.sha256 == hashlib.sha256(payload).hexdigest()


def test_upload_of_a_megabyte_with_awkward_bytes_from_sz(tmp_path):
    payload = (_AWKWARD + bytes(range(256))) * 3800
    result, dest = _upload(tmp_path, payload)
    assert dest.read_bytes() == payload
    assert result.size_bytes == len(payload)


def test_upload_from_sz_escaping_every_control_character(tmp_path):
    payload = bytes(range(256)) * 20
    _, dest = _upload(tmp_path, payload, "-e")
    assert dest.read_bytes() == payload


def test_an_upload_netbbs_refuses_stops_sz(tmp_path):
    """NetBBS gives up on an oversized upload; the abort sequence it sends
    makes `sz` stop instead of waiting out its own timeouts."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "big.bin").write_bytes(b"x" * 50_000)

    async def scenario():
        sz = await _spawn(SZ, "-q", "big.bin", cwd=source)
        with pytest.raises(zmodem.ZmodemError):
            await zmodem.receive_file(_ProcessSession(sz), max_bytes=1000, dest_path=tmp_path / "in")
        assert await _finish(sz, timeout=15) != 0

    asyncio.run(scenario())
    assert not (tmp_path / "in").exists()
