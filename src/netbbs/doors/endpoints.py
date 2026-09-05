"""Owned duplex endpoints and cross-process node leases."""
from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import socket
from pathlib import Path


class StreamEndpoint:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    async def read(self, size=4096):
        return await self.reader.read(size)

    async def write(self, data):
        self.writer.write(data)
        await self.writer.drain()

    async def close(self):
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass


class FdEndpoint:
    def __init__(self, fd):
        self.fd = fd
        os.set_blocking(fd, False)

    async def _ready(self, write=False):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        def ready():
            if not future.done():
                future.set_result(None)
        (loop.add_writer if write else loop.add_reader)(self.fd, ready)
        try:
            await future
        finally:
            (loop.remove_writer if write else loop.remove_reader)(self.fd)

    async def read(self, size=4096):
        while True:
            try:
                return os.read(self.fd, size)
            except BlockingIOError:
                await self._ready()
            except OSError as exc:
                if exc.errno == errno.EIO:  # PTY slave hung up (Linux; NetBSD returns EOF)
                    return b""
                raise

    async def write(self, data):
        view = memoryview(data)
        while view:
            try:
                count = os.write(self.fd, view)
                view = view[count:]
            except BlockingIOError:
                await self._ready(write=True)

    async def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def socket_endpoint():
    parent, child = socket.socketpair()
    return FdEndpoint(parent.detach()), child


def pty_endpoint(width, height):
    import fcntl
    import pty
    import struct
    import termios
    import tty
    master, slave = pty.openpty()
    try:
        tty.setraw(slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))
        return FdEndpoint(master), slave
    except BaseException:
        os.close(master)
        os.close(slave)
        raise


class NodeLease:
    """OS advisory locks survive neither process death nor reboot; no stale-PID guessing."""
    def __init__(self, root: Path, identity: str, maximum: int):
        directory = root / hashlib.sha256(identity.encode()).hexdigest()[:24]
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.file = None
        self.gate = None
        if os.name == "posix":
            import fcntl
            gate = (directory / "installation.lock").open("a+b")
            try:
                # A one-session registration also excludes another registration
                # of this installation configured for several nodes.
                fcntl.flock(gate, (fcntl.LOCK_EX if maximum == 1 else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BaseException:
                gate.close()
                raise
            self.gate = gate
        for number in range(1, maximum + 1):
            try:
                lock = (directory / f"node{number}.lock").open("a+b")
            except OSError:
                self.close()
                raise
            try:
                if os.name == "posix":
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    import msvcrt
                    lock.seek(0)
                    if not lock.read(1):
                        lock.write(b"0")
                        lock.flush()
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                lock.close()
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                    self.close()
                    raise
                continue
            self.file, self.number = lock, number
            return
        self.close()
        raise BlockingIOError("All configured door nodes are busy")

    def close(self):
        if self.file is not None:
            self.file.close()
            self.file = None
        if self.gate is not None:
            self.gate.close()
            self.gate = None
