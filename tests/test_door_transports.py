"""Real Telnet/SSH clients through the supervisor and CP437 adapter."""
import asyncio
import base64
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from netbbs.doors import create_door
from netbbs.doors.profiles import DoorProfile
from netbbs.doors.runtime import run_door
from tests.test_doors_runtime import db, lane, player, _write_script


@pytest.mark.parametrize("transport", ["telnet", "ssh", "web"])
@pytest.mark.parametrize("dos", [False, True])
def test_cp437_play_and_return_over_real_transport(transport, dos, db, lane, player, tmp_path):
    if dos:
        emulator, assembler = shutil.which("dosbox-x"), shutil.which("nasm")
        if os.name != "posix" or not emulator or not assembler:
            pytest.skip("Real DOS transport certification requires POSIX, DOSBox-X and NASM")
        subprocess.run([assembler, "-f", "bin", "-o", str(tmp_path / "SERIAL.COM"),
                        str(Path(__file__).parent / "fixtures/door_serial.asm")], check=True)
        executable, argv = emulator, ()
        profile = DoorProfile(adapter="dosbox", endpoint="socketpair", encoding="cp437", width=80, height=25,
                              install_dir=str(tmp_path), memory_mb=1024, options={"command":"SERIAL.COM"})
    else:
        script = _write_script(tmp_path, "cp437.py", '''
            import os
            os.write(1, b'\\x1b[32mDOS READY \\xdb\\x1b[0m\\r\\n')
            assert os.read(0,1) == b'\\x82'
            assert os.read(0,1) == b'Q'
            os.write(1,b'\\x82Q')
        ''')
        executable, argv = sys.executable, (str(script),)
        profile = DoorProfile(encoding="cp437", width=80, height=25)
    door = create_door(db, "Codec", executable, args=argv, creator=player, profile=profile)
    results = []
    async def handler(session):
        await session.write("SYNC")
        assert await session.read_key() == "X"
        results.append(await run_door(session, lane, door, player, wall_time_limit_seconds=15))
        await session.write("MENU")
        assert await session.read_key() == "B"
        await session.write("RETURNED")

    async def exchange(reader, writer, telnet=False):
        if telnet:
            from tests.test_telnet import skip_initial_negotiation
            from netbbs.net.telnet import IAC, WILL, BINARY, SB, SE, NAWS
            await skip_initial_negotiation(reader)
            writer.write(bytes([IAC,WILL,BINARY,IAC,SB,NAWS,0,80,0,25,IAC,SE]))
        await reader.readuntil(b"SYNC")
        writer.write(b"X")
        await writer.drain()
        output = await reader.readuntil(b"DOS READY")
        writer.write("éQ".encode())
        await writer.drain()
        output += await reader.readuntil(b"MENU")
        assert "█".encode() in output
        assert "éQ".encode() in output
        writer.write(b"B")
        await writer.drain()
        await reader.readuntil(b"RETURNED")

    async def scenario():
        if transport == "telnet":
            from netbbs.net.telnet import TelnetServer
            server = TelnetServer(host="127.0.0.1", port=0, session_handler=handler)
        elif transport == "ssh":
            from netbbs.net.ssh import SSHServer
            server = SSHServer(host="127.0.0.1", port=0, db=db, session_handler=handler)
        else:
            from netbbs.net.web import WebServer
            server = WebServer(host="127.0.0.1", port=0, session_handler=handler)
        await server.start()
        try:
            async with asyncio.timeout(25):
                if transport == "telnet":
                    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
                    try:
                        await exchange(reader, writer, True)
                    finally:
                        writer.close()
                        await writer.wait_closed()
                elif transport == "ssh":
                    import asyncssh
                    async with asyncssh.connect("127.0.0.1", server.port, username=player.username,
                                                password="hunter2", known_hosts=None) as conn:
                        async with conn.create_process(term_type="ansi", term_size=(80,25), encoding=None) as process:
                            await exchange(process.stdout, process.stdin)
                else:
                    import aiohttp
                    async with aiohttp.ClientSession() as client:
                        async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws") as ws:
                            output = bytearray()
                            sent = False
                            stream = None
                            restored = False
                            while True:
                                frame = await ws.receive_json()
                                if frame["type"] == "output":
                                    if "SYNC" in frame["data"]:
                                        await ws.send_json({"type": "resize", "cols": 80, "rows": 25})
                                        await ws.send_json({"type": "key", "data": "X"})
                                    if "MENU" in frame["data"]:
                                        assert restored
                                        assert "█".encode() in output and "éQ".encode() in output
                                        await ws.send_json({"type": "key", "data": "B"})
                                    if "RETURNED" in frame["data"]:
                                        break
                                elif frame["type"] == "door_mode":
                                    if frame["active"]:
                                        stream = frame["stream"]
                                    else:
                                        restored = True
                                elif frame["type"] == "door_output":
                                    assert frame["stream"] == stream
                                    output.extend(base64.b64decode(frame["data"]))
                                    if b"DOS READY" in output and not sent:
                                        sent = True
                                        await ws.send_json({"type": "door_key", "stream": stream, "data": "éQ"})
        finally:
            await server.stop()
    asyncio.run(scenario())
    assert len(results) == 1 and results[0].reason == "exited", results
