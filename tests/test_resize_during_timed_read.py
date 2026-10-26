"""A window change during a timed read is kept (issue #1575).

PuTTY over SSH sends its window change a few seconds into the session, when
NetBBS is often inside a timed wait rather than a blocking read. That wait
used to drop the size (SSH) or hand the NAWS bytes to the input (Telnet), so
the caller stayed at the logon size and a door opened at it.
"""
import asyncio
import sys

import pytest

from netbbs.doors import create_door
from netbbs.doors.runtime import run_door
from tests.test_doors_runtime import db, lane, player, _write_script


@pytest.mark.parametrize("transport", ["telnet", "ssh"])
def test_a_resize_during_a_timed_read_reaches_the_next_door(transport, db, lane, player, tmp_path):
    script = _write_script(tmp_path, "size.py", '''
        import json, os
        info = json.load(open(os.environ["NETBBS_DOOR_INFO"]))
        os.write(1, ("SIZE=%sx%s END" % (info["terminal_width"], info["terminal_height"])).encode())
    ''')
    door = create_door(db, "Size", sys.executable, args=(str(script),), creator=player)
    seen = {}

    async def handler(session):
        await session.write("WAIT")
        # A timed wait with nothing typed: the window change lands in it.
        seen["peek"] = await session.read_byte_with_timeout(1.5)
        seen["size"] = (session.physical_width, session.terminal_height)
        await session.write("KEY")
        seen["key"] = await session.read_key()
        await run_door(session, lane, door, player, wall_time_limit_seconds=15)
        await session.write("DONE")
        await session.read_key()

    async def exchange(reader, writer, resize):
        await reader.readuntil(b"WAIT")
        resize()
        await writer.drain()
        await reader.readuntil(b"KEY")
        writer.write(b"Y")
        await writer.drain()
        output = await reader.readuntil(b"END")
        seen["door"] = output[output.index(b"SIZE="):].decode()
        writer.write(b"Z")
        await writer.drain()

    async def scenario():
        if transport == "telnet":
            from netbbs.net.telnet import TelnetServer
            server = TelnetServer(host="127.0.0.1", port=0, session_handler=handler)
        else:
            from netbbs.net.ssh import SSHServer
            server = SSHServer(host="127.0.0.1", port=0, db=db, session_handler=handler)
        await server.start()
        try:
            async with asyncio.timeout(25):
                if transport == "telnet":
                    from netbbs.net.telnet import IAC, NAWS, SB, SE, WILL
                    from tests.test_telnet import skip_initial_negotiation
                    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
                    await skip_initial_negotiation(reader, writer)
                    writer.write(bytes([IAC, WILL, NAWS, IAC, SB, NAWS, 0, 80, 0, 24, IAC, SE]))
                    try:
                        await exchange(reader, writer,
                                       lambda: writer.write(bytes([IAC, SB, NAWS, 0, 122, 0, 36, IAC, SE])))
                    finally:
                        writer.close()
                        await writer.wait_closed()
                else:
                    import asyncssh
                    async with asyncssh.connect("127.0.0.1", server.port, username=player.username,
                                                password="hunter2", known_hosts=None) as conn:
                        async with conn.create_process(term_type="xterm", term_size=(80, 24),
                                                       encoding=None) as process:
                            await exchange(process.stdout, process.stdin,
                                           lambda: process.change_terminal_size(122, 36))
        finally:
            await server.stop()

    asyncio.run(scenario())
    # The resize is not input: the wait ran out with nothing typed.
    assert seen["peek"] is None
    assert seen["size"] == (122, 36)
    assert seen["key"] == "Y"
    assert seen["door"] == "SIZE=122x36 END"
