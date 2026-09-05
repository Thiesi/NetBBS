"""Outbound RFC 1282 service adapter. No caller-controlled destination or root port."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import select
import socket
import stat
import struct
import string
from pathlib import Path


def validate_remote(profile):
    options = profile.options
    host, port = options.get("host"), options.get("port", 513)
    if not isinstance(host, str) or not host or len(host) > 253 or any(c.isspace() for c in host):
        raise ValueError("Remote service needs a fixed destination host")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Remote port must be between 1 and 65535")
    allowlist = options.get("allowed_destinations", [])
    if not isinstance(allowlist, list) or f"{host}:{port}" not in allowlist:
        raise ValueError("Remote destination must appear in allowed_destinations as host:port")
    if options.get("insecure_acknowledged") is not True:
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
        if not local:
            raise ValueError("Use an SSH/TLS tunnel on a loopback IP, or explicitly acknowledge insecure RLogin")
    if not isinstance(options.get("service_name"), str) or not options["service_name"].strip() or len(options["service_name"]) > 160:
        raise ValueError("Remote service identity must be shown to callers (service_name)")
    if options.get("credential_file"):
        path = options["credential_file"]
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ValueError("Credential file must be an absolute path")
    for key, default in (("local_user", "{user_id}"), ("remote_user", "{handle}")):
        try:
            _field(options.get(key, default), {"handle": "probe", "user_id": 1})
        except (ValueError, KeyError) as exc:
            raise ValueError(f"Invalid provider {key} template: {exc}") from exc
    return host, port


def _credentials(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("Credential file must be an absolute path")
    # A mistaken FIFO/device path must not block a preflight worker forever.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or (os.name == "posix" and metadata.st_mode & 0o077):
            raise ValueError("Credential file must be a private regular file (chmod 600)")
        raw = source.read(4097)
    if len(raw) > 4096:
        raise ValueError("Credential file exceeds 4 KiB")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Credential file is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) - {"local_user", "remote_user"}:
        raise ValueError("Credential file permits local_user and remote_user only")
    return value


def _field(value, info):
    if not isinstance(value, str):
        raise ValueError("RLogin identity fields must be strings")
    for _, name, spec, conversion in string.Formatter().parse(value):
        if name is not None and (name not in ("handle", "user_id") or spec or conversion):
            raise ValueError("Provider identity accepts only {handle} and {user_id} substitutions")
    result = value.format_map({"handle": info["handle"], "user_id": info["user_id"]})
    if any(ord(c) < 32 or ord(c) == 127 for c in result):
        raise ValueError("RLogin identity contains control characters")
    data = result.encode("utf-8")
    if not data or len(data) > 128:
        raise ValueError("RLogin identity must contain 1-128 UTF-8 bytes")
    return data + b"\x00"


class RemoteEndpoint:
    def __init__(self, sock, width, height):
        self.sock = sock
        self.window = b"\xff\xffss" + struct.pack("!HHHH", height, width, 0, 0)
        self.write_lock = asyncio.Lock()
        self.urgent = asyncio.create_task(self._urgent_loop())

    async def _urgent_loop(self):
        # asyncio has no portable exceptional-fd callback; bounded polling handles
        # RFC 1282's urgent window request on both NetBSD and Linux.
        while True:
            if select.select([], [], [self.sock], 0)[2]:
                try:
                    control = self.sock.recv(1, socket.MSG_OOB)
                except BlockingIOError:
                    control = b""
                if control and control[0] & 0x80:
                    await self.write(self.window)
            await asyncio.sleep(0.05)

    async def read(self, size=4096):
        read = asyncio.create_task(asyncio.get_running_loop().sock_recv(self.sock, size))
        try:
            done, _ = await asyncio.wait([read, self.urgent], return_when=asyncio.FIRST_COMPLETED)
            if self.urgent in done:
                self.urgent.result()
            return await read
        finally:
            if not read.done():
                read.cancel()
            await asyncio.gather(read, return_exceptions=True)

    async def write(self, data):
        async with self.write_lock:
            await asyncio.get_running_loop().sock_sendall(self.sock, data)

    async def close(self):
        self.urgent.cancel()
        await asyncio.gather(self.urgent, return_exceptions=True)
        self.sock.close()


async def connect_remote(profile, info, width, height):
    host, port = validate_remote(profile)
    options = profile.options
    credentials = _credentials(options["credential_file"]) if options.get("credential_file") else {}
    local = credentials.get("local_user", options.get("local_user", "{user_id}"))
    remote = credentials.get("remote_user", options.get("remote_user", "{handle}"))
    handshake = b"\x00" + _field(local, info) + _field(remote, info) + f"ansi/{profile.baud}".encode() + b"\x00"
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(10):
        addresses = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        family, kind, proto, _, address = addresses[0]
        sock = socket.socket(family, kind, proto)
        sock.setblocking(False)
        try:
            await loop.sock_connect(sock, address)
            await loop.sock_sendall(sock, handshake)
            ack = await loop.sock_recv(sock, 1)
            if ack != b"\x00":
                raise ValueError("Remote RLogin service refused the handshake; check provider access and credentials")
            return RemoteEndpoint(sock, width, height)
        except BaseException:
            sock.close()
            raise
