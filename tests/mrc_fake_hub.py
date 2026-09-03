"""
A loopback MRC hub for tests (issue #275): speaks the real
tilde-delimited wire protocol `netbbs.mrc.protocol` documents --
handshake line in, `HELLO` out, `PING`/`IMALIVE`, `NEWROOM`/`LOGOFF`/
`USERLIST` bookkeeping, and the one behaviour that matters most for a
bridge, echoing every room message back to *every* connected site
including the sender. Deliberately minimal and fully scriptable
(`send_line`, `ping`, `drop_clients`, `reject_version`) so bridge tests
can exercise reconnects and hostile input without a real hub.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from netbbs.mrc.protocol import MrcPacket, build_line, parse_line, parse_server_command


class FakeMrcHub:
    def __init__(self, *, reject_version: str | None = None, hello: bool = True) -> None:
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []
        self._reject_version = reject_version
        self._hello = hello
        self.port = 0
        self.handshakes: list[str] = []
        self.received: list[MrcPacket] = []
        self.connections = 0
        # (site lower, nick lower) -> room
        self.users: dict[tuple[str, str], str] = {}
        self._event = asyncio.Event()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        await self.drop_clients()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def drop_clients(self) -> None:
        writers, self._writers = self._writers, []
        for writer in writers:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def send_line(self, line: str) -> None:
        if not line.endswith("\n"):
            line += "\n"
        for writer in list(self._writers):
            writer.write(line.encode("ascii", errors="replace"))
            await writer.drain()

    async def send_packet(self, packet: MrcPacket) -> None:
        await self.send_line(build_line(packet))

    async def ping(self) -> None:
        await self.send_packet(MrcPacket("SERVER", "", "", "CLIENT", "", "", "PING"))

    async def wait_for(self, predicate: Callable[[MrcPacket], bool], *, timeout: float = 2.0) -> MrcPacket:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            for packet in self.received:
                if predicate(packet):
                    return packet
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise AssertionError(f"no packet matched within {timeout}s; got {self.received!r}")
            self._event.clear()
            try:
                await asyncio.wait_for(self._event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass

    async def wait_for_connections(self, count: int, *, timeout: float = 2.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while self.connections < count:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"expected {count} connections, saw {self.connections}")
            await asyncio.sleep(0.01)

    def packets(self, *, body_prefix: str | None = None, from_user: str | None = None) -> list[MrcPacket]:
        return [
            packet for packet in self.received
            if (body_prefix is None or packet.body.upper().startswith(body_prefix.upper()))
            and (from_user is None or packet.from_user.lower() == from_user.lower())
        ]

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self._writers.append(writer)
        try:
            handshake = await reader.readline()
            if not handshake:
                return
            self.handshakes.append(handshake.decode("ascii", errors="replace").rstrip("\r\n"))
            if self._reject_version is not None:
                writer.write(build_line(
                    MrcPacket("SERVER", "", "", "CLIENT", "", "", f"OLDVERSION:{self._reject_version}")
                ).encode("ascii"))
                await writer.drain()
                return
            if self._hello:
                writer.write(build_line(MrcPacket("SERVER", "", "", "CLIENT", "", "", "HELLO")).encode("ascii"))
                await writer.drain()
            while True:
                raw = await reader.readline()
                if not raw:
                    return
                packet = parse_line(raw.decode("ascii", errors="replace"))
                if packet is None:
                    continue
                self.received.append(packet)
                self._event.set()
                await self._handle(packet, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if writer in self._writers:
                self._writers.remove(writer)
            try:
                writer.close()
            except Exception:
                pass

    async def _handle(self, packet: MrcPacket, writer: asyncio.StreamWriter) -> None:
        if packet.to_user.upper() == "SERVER":
            command, params = parse_server_command(packet.body)
            key = (packet.from_site.lower(), packet.from_user.lower())
            if command == "NEWROOM":
                _old, _, new_room = params.partition(":")
                self.users[key] = new_room or "lobby"
            elif command == "LOGOFF":
                self.users.pop(key, None)
            elif command == "USERLIST":
                room = (packet.to_room or self.users.get(key, "")).lower()
                names = ",".join(
                    f"{nick}@{site}" for (site, nick), user_room in sorted(self.users.items())
                    if user_room.lower() == room
                )
                reply = MrcPacket("SERVER", "", "", packet.from_user, "", packet.to_room or room, f"USERLIST:{names}")
                writer.write(build_line(reply).encode("ascii"))
                await writer.drain()
            return
        # Room traffic (and anything else): fan out to every site, the
        # sender included -- exactly what the real hub does.
        await self.send_packet(packet)
