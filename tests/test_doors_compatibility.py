"""Real process, terminal, socket, filesystem and web compatibility boundaries."""
import asyncio
import json
import os
import signal
import socket
import shutil
import subprocess
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from netbbs.doors import create_door
from netbbs.doors.dropfiles import drop_file_bytes
from netbbs.doors.profiles import DoorProfile, ProfileError
from netbbs.doors.runtime import run_door
from tests.test_doors_runtime import FakeSession, _write_script, db, lane, player


def test_classic_drop_file_golden_fields_and_crlf():
    profile = DoorProfile(adapter="dosbox", endpoint="socketpair", install_dir=str(Path.cwd()),
                          encoding="cp437", width=80, height=25,
                          drop_files=("DOOR.SYS", "DOOR32.SYS", "DORINFOx.DEF", "CHAIN.TXT"),
                          options={"command": "GAME.EXE"})
    files = drop_file_bytes(profile, {"handle": "Jörg\r\nBAD\x1b", "user_id": 42, "node_name": "Test"},
                            11, now=datetime(2026, 9, 5, 12, 30))
    assert files["DORINFOA.DEF"] == b"TEST\r\nSYSOP\r\n\r\nCOM1\r\n38400 BAUD,N,8,1\r\n0\r\nJ\x99RGBAD\r\n\r\n\r\n1\r\n10\r\n60\r\n-1\r\n"
    assert files["DOOR32.SYS"] == b"0\r\n0\r\n38400\r\nTest\r\n42\r\nJ\x94rgBAD\r\nJ\x94rgBAD\r\n10\r\n60\r\n1\r\n11\r\n"
    door = files["DOOR.SYS"].split(b"\r\n")[:-1]
    assert len(door) == 52
    assert door[13] == b""  # no password
    assert door[17:21] == [b"3600", b"60", b"GR", b"25"]
    assert door[25] == b"42"
    chain = files["CHAIN.TXT"].split(b"\r\n")[:-1]
    assert len(chain) == 32
    assert chain[8:16] == [b"80", b"25", b"10", b"0", b"0", b"1", b"1", b"3600.00"]
    assert files["CHAIN.TXT"] == (
        "42\r\nJörgBAD\r\nJörgBAD\r\n\r\n0\r\n\r\n0.00\r\n09/05/26\r\n80\r\n25\r\n10\r\n"
        "0\r\n0\r\n1\r\n1\r\n3600.00\r\nD:\\\r\nD:\\\r\n\r\n38400\r\n1\r\nTest\r\nSysOp\r\n45000\r\n"
        "0\r\n0\r\n0\r\n0\r\n0\r\n8N1\r\n38400\r\n0\r\n"
    ).encode("cp437")
    assert files["DOOR.SYS"] == (
        "COM1:\r\n38400\r\n8\r\n11\r\n38400\r\nY\r\nN\r\nN\r\nN\r\nJörgBAD\r\n\r\n\r\n\r\n\r\n"
        "10\r\n0\r\n09/05/26\r\n3600\r\n60\r\nGR\r\n25\r\nN\r\n\r\n0\r\n12/31/99\r\n42\r\nN\r\n"
        "0\r\n0\r\n0\r\n0\r\n\r\nD:\\\r\nD:\\\r\nSysOp\r\nJörgBAD\r\n00:00\r\nY\r\nN\r\nY\r\n7\r\n"
        "0\r\n09/05/26\r\n12:30\r\n12:30\r\n0\r\n0\r\n0\r\n0\r\n\r\n0\r\n0\r\n"
    ).encode("cp437")
    assert all(b"\n" not in value.replace(b"\r\n", b"") for value in files.values())


@pytest.mark.parametrize("data", [{"version": 2}, {"max_sessions": 2}, {"environment": {"LD_PRELOAD": "evil"}},
                                  {"drop_subdir": "../x"}, {"width": True}, {"unknown": 1}])
def test_profile_rejects_invalid_or_dangerous_config(data):
    with pytest.raises(ProfileError):
        DoorProfile.from_json(json.dumps(data))


@pytest.mark.parametrize("endpoint", ["stdio", "pty", "socketpair"])
def test_native_endpoints_metadata_and_persistent_scores(endpoint, db, lane, player, tmp_path, monkeypatch):
    if endpoint != "stdio" and os.name != "posix":
        pytest.skip("real POSIX endpoint")
    monkeypatch.setenv("NETBBS_SECRET_TEST", "never inherit")
    script = _write_script(tmp_path, "legacy.py", '''
        import json, os, pathlib, socket, sys
        assert 'NETBBS_SECRET_TEST' not in os.environ
        node = pathlib.Path(os.environ['NETBBS_DOOR_NODE_DIR'])
        assert len((node / 'DOOR.SYS').read_bytes().splitlines()) == 52
        assert os.environ['NETBBS_DOOR_NODE'] == '1'
        if sys.argv[1] == 'pty':
            assert os.isatty(0) and os.isatty(1)
            assert os.get_terminal_size(0) == (80, 25)
            assert os.environ['TERM'] == 'ansi'
            fd = os.open('/dev/tty', os.O_RDWR)
            os.close(fd)
        if sys.argv[1] == 'socketpair':
            fields = (node / 'DOOR32.SYS').read_text().splitlines()
            assert fields[0] == '2'
            sock = socket.socket(fileno=int(fields[1]))
            sock.sendall(b'READY')
            data = sock.recv(1)
        else:
            os.write(1, b'READY')
            data = os.read(0, 1)
        assert data == b'X'
        score = pathlib.Path('score.txt')
        score.write_text(str(int(score.read_text()) + 1) if score.exists() else '1')
    ''')
    profile = DoorProfile(endpoint=endpoint, install_dir=str(tmp_path), width=80, height=25,
                          drop_files=("DOOR.SYS", "DOOR32.SYS"))
    door = create_door(db, "Legacy", sys.executable, args=(str(script), endpoint), creator=player, profile=profile)

    async def scenario():
        for _ in range(2):
            session = FakeSession()
            session.terminal_height = 25
            task = asyncio.create_task(run_door(session, lane, door, player, wall_time_limit_seconds=30))
            async with asyncio.timeout(45):
                while b"READY" not in session.written:
                    if task.done():
                        pytest.fail(str(task.result()))
                    await asyncio.sleep(0.01)
                session.type_in("X")
                result = await task
            assert result.reason == "exited", result
    asyncio.run(scenario())
    assert (tmp_path / "score.txt").read_text() == "2"


def test_concurrent_launch_denied_and_lease_released_on_cancel(db, lane, player, tmp_path):
    script = _write_script(tmp_path, "wait.py", "import os,time; os.write(1,b'READY'); time.sleep(60)")
    door = create_door(db, "Single", sys.executable, args=(str(script),), creator=player,
                       profile=DoorProfile(install_dir=str(tmp_path)))
    async def scenario():
        session = FakeSession()
        task = asyncio.create_task(run_door(session, lane, door, player))
        async with asyncio.timeout(30):
            while b"READY" not in session.written:
                if task.done():
                    pytest.fail(str(task.result()))
                await asyncio.sleep(0.01)
        assert (await run_door(FakeSession(), lane, door, player)).reason == "busy"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await run_door(FakeSession(), lane, door, player, wall_time_limit_seconds=0.1)).reason == "timed_out"
    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "posix", reason="real POSIX process groups")
@pytest.mark.parametrize("end", ["exit", "timeout", "cancel", "disconnect", "crash"])
def test_process_group_kills_descendant_even_after_leader_exits(end, db, lane, player, tmp_path):
    marker = tmp_path / "survived"
    script = _write_script(tmp_path, "tree.py", '''
        import os, pathlib, signal, sys, time
        ready_read, ready_write = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(ready_read)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            os.write(ready_write, b'R')
            os.close(ready_write)
            # Only attempt the leak after the supervisor has returned. This
            # is independent of interpreter startup and the watchdog budget.
            trigger = pathlib.Path(sys.argv[1] + '.check')
            while not trigger.exists(): time.sleep(0.01)
            open(sys.argv[1], 'w').write('leaked')
            os._exit(0)
        os.close(ready_write)
        assert os.read(ready_read, 1) == b'R'
        os.close(ready_read)
        os.write(1, b'READY')
        if sys.argv[2] == 'exit': os._exit(0)
        if sys.argv[2] == 'crash': os._exit(7)
        time.sleep(60)
    ''')
    door = create_door(db, "Tree", sys.executable, args=(str(script), str(marker), end), creator=player)
    async def scenario():
        session = FakeSession()
        task = asyncio.create_task(run_door(session, lane, door, player,
                                          wall_time_limit_seconds=15 if end == "timeout" else 30))
        async with asyncio.timeout(45):
            while b"READY" not in session.written:
                if task.done():
                    pytest.fail(str(task.result()))
                await asyncio.sleep(0.01)
            if end == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                if end == "disconnect":
                    session.disconnect()
                result = await task
                assert result.reason == {"exit": "exited", "crash": "crashed", "timeout": "timed_out", "disconnect": "caller_disconnected"}[end]
        Path(str(marker) + '.check').touch()
        await asyncio.sleep(1.6)
    asyncio.run(scenario())
    assert not marker.exists()


def test_remote_allowlist_tunnel_and_rlogin_handshake(db, lane, player):
    from netbbs.doors.remote import validate_remote
    with pytest.raises(ValueError, match="tunnel"):
        validate_remote(DoorProfile(adapter="rlogin", options={"host": "example.org", "port": 513,
                        "allowed_destinations": ["example.org:513"], "service_name": "Example"}))
    received = []
    async def handler(reader, writer):
        try:
            for _ in range(4):
                received.append(await reader.readuntil(b"\x00"))
            writer.write(b"\x00REMOTE")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    async def scenario():
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        profile = DoorProfile(adapter="rlogin", options={"host": "127.0.0.1", "port": port,
                 "allowed_destinations": [f"127.0.0.1:{port}"], "service_name": "Test service"})
        door = create_door(db, "Remote", "remote", creator=player, profile=profile)
        session = FakeSession()
        try:
            result = await run_door(session, lane, door, player)
            assert result.reason == "exited", result
            assert session.written == b"REMOTE"
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(scenario())
    assert received == [b"\x00", b"1\x00", b"keeper\x00", b"ansi/38400\x00"]


@pytest.mark.skipif(os.name != "posix", reason="DOSBox inherited POSIX socket")
@pytest.mark.parametrize("fossil", [False, True])
def test_real_dosbox_com1_cp437_round_trip(db, lane, player, tmp_path, fossil):
    emulator, assembler = shutil.which("dosbox-x"), shutil.which("nasm")
    if not emulator or not assembler:
        pytest.skip("SysOp must install dosbox-x and nasm to run this platform certification")
    fossil_path = os.environ.get("NETBBS_TEST_FOSSIL")
    if fossil and not fossil_path:
        pytest.skip("Set NETBBS_TEST_FOSSIL to a legally obtained BNU.COM")
    if fossil:
        shutil.copyfile(fossil_path, tmp_path / "BNU.COM")
    subprocess.run([assembler, *(["-DFOSSIL=1"] if fossil else []), "-f", "bin", "-o", str(tmp_path / "SERIAL.COM"),
                    str(Path(__file__).parent / "fixtures" / "door_serial.asm")], check=True)
    profile = DoorProfile(adapter="dosbox", endpoint="socketpair", encoding="cp437", width=80, height=25, memory_mb=1024,
                          install_dir=str(tmp_path), drop_files=("DOOR.SYS",),
                          options={"command": "SERIAL.COM", "fossil": "BNU.COM /L0=38400" if fossil else ""})
    door = create_door(db, "DOS serial", emulator, creator=player, profile=profile)
    async def scenario():
        session = FakeSession()
        session.terminal_height = 25
        task = asyncio.create_task(run_door(session, lane, door, player, wall_time_limit_seconds=30))
        async with asyncio.timeout(45):
            while b"DOS READY" not in session.written:
                if task.done():
                    pytest.fail(str(task.result()))
                await asyncio.sleep(0.01)
            session.type_in("\u00e9Q")
            result = await task
        assert result.reason == "exited", result
        assert "\u2588" in session.written.decode()
        assert "\u00e9Q" in session.written.decode()
    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "posix", reason="POSIX node leases")
def test_node_lease_multi_node_and_exclusive_registration(tmp_path):
    from netbbs.doors.endpoints import NodeLease
    first = NodeLease(tmp_path, "installation", 2)
    second = NodeLease(tmp_path, "installation", 2)
    try:
        assert (first.number, second.number) == (1, 2)
        with pytest.raises(BlockingIOError):
            NodeLease(tmp_path, "installation", 2)
        with pytest.raises(BlockingIOError):
            NodeLease(tmp_path, "installation", 1)
    finally:
        first.close()
        second.close()
    single = NodeLease(tmp_path, "installation", 1)
    try:
        with pytest.raises(BlockingIOError):
            NodeLease(tmp_path, "installation", 2)
    finally:
        single.close()


def test_capability_probe_bytes_match_assembler(tmp_path):
    assembler = shutil.which("nasm")
    if not assembler:
        pytest.skip("nasm required to verify checked-in probe machine code")
    from netbbs.doors.probe import UART_PROBE, FOSSIL_PROBE
    for fossil, expected in ((False, UART_PROBE), (True, FOSSIL_PROBE)):
        output = tmp_path / "probe.com"
        subprocess.run([assembler, *(["-DFOSSIL=1"] if fossil else []), "-f", "bin", "-o", str(output),
                        str(Path(__file__).parent / "fixtures/door_serial.asm")], check=True)
        assert output.read_bytes() == expected


@pytest.mark.skipif(os.name != "posix", reason="POSIX DOSBox")
def test_operator_capability_probe_uses_temporary_game(db, lane, player, tmp_path):
    emulator = shutil.which("dosbox-x")
    if not emulator:
        pytest.skip("DOSBox-X must be installed manually")
    from netbbs.doors.probe import probe_dosbox
    door = create_door(db, "Probe", emulator, creator=player,
                       profile=DoorProfile(adapter="dosbox", endpoint="socketpair", encoding="cp437", memory_mb=1024,
                                           install_dir=str(tmp_path), options={"command": "NEVER.EXE"}))
    result = asyncio.run(probe_dosbox(lane, door, player))
    assert result.reason == "exited", result
    assert not list(tmp_path.glob("PROBE.*"))


def test_profile_migration_preserves_unprofiled_door_and_missing_remote_credentials(db, player, tmp_path):
    from netbbs.doors.registry import get_door_by_name
    original = create_door(db, "Original", sys.executable, creator=player)
    assert get_door_by_name(db, original.name).profile is None
    profile = DoorProfile(adapter="rlogin", options={"host":"127.0.0.1", "port":1513,
                          "allowed_destinations":["127.0.0.1:1513"], "service_name":"Remote",
                          "credential_file":str(tmp_path / "missing.json")})
    door = create_door(db, "Remote", "remote", creator=player, profile=profile)
    assert get_door_by_name(db, door.name).profile == profile  # still editable if an external file disappears
    from netbbs.doors.profiles import preflight
    assert preflight(door)


def test_cancellation_during_spawn_and_broken_terminal_restore_still_reaps(db, lane, player, tmp_path, monkeypatch):
    script = _write_script(tmp_path, "spawn.py", "import time; time.sleep(60)")
    door = create_door(db, "Spawn cancellation", sys.executable, args=(str(script),), creator=player,
                       profile=DoorProfile(install_dir=str(tmp_path)))
    async def scenario():
        real_spawn = asyncio.create_subprocess_exec
        spawned, release = asyncio.Event(), asyncio.Event()
        children = []
        async def delayed_spawn(*args, **kwargs):
            child = await real_spawn(*args, **kwargs)
            children.append(child)
            spawned.set()
            await release.wait()
            return child
        async def broken_restore():
            raise RuntimeError("terminal restore failed")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
        session = FakeSession()
        session.leave_door_mode = broken_restore
        task = asyncio.create_task(run_door(session, lane, door, player))
        await asyncio.wait_for(spawned.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert children[0].returncode is not None
        monkeypatch.setattr(asyncio, "create_subprocess_exec", real_spawn)
        assert (await run_door(FakeSession(), lane, door, player, wall_time_limit_seconds=0.1)).reason == "timed_out"
    asyncio.run(scenario())


def test_terminal_eof_before_slow_process_exit_is_not_a_crash(db, lane, player, tmp_path):
    script = _write_script(tmp_path, "slowexit.py", "import os,time; os.close(1); time.sleep(0.8)")
    door = create_door(db, "Slow exit", sys.executable, args=(str(script),), creator=player)
    result = asyncio.run(run_door(FakeSession(), lane, door, player))
    assert result.reason == "exited", result


@pytest.mark.skipif(os.name != "posix", reason="RFC TCP urgent data on POSIX")
def test_rlogin_urgent_window_request_is_not_terminal_input():
    from netbbs.doors.remote import connect_remote
    import struct
    async def scenario():
        listener = socket.socket()
        listener.bind(("127.0.0.1",0))
        listener.listen()
        listener.setblocking(False)
        port = listener.getsockname()[1]
        loop = asyncio.get_running_loop()
        async def server():
            conn, _ = await loop.sock_accept(listener)
            try:
                greeting = b""
                while greeting.count(b"\0") < 4:
                    greeting += await loop.sock_recv(conn,256)
                await loop.sock_sendall(conn,b"\0")
                conn.send(b"\x80",socket.MSG_OOB)
                window = b""
                while len(window)<12:
                    window += await loop.sock_recv(conn,12-len(window))
                assert window == b"\xff\xffss" + struct.pack("!HHHH",25,80,0,0)
                await loop.sock_sendall(conn,b"VISIBLE")
            finally:
                conn.close()
        task = asyncio.create_task(server())
        profile = DoorProfile(adapter="rlogin", options={"host":"127.0.0.1","port":port,
                              "allowed_destinations":[f"127.0.0.1:{port}"],"service_name":"Test"})
        endpoint = None
        try:
            async with asyncio.timeout(5):
                endpoint = await connect_remote(profile,{"handle":"Caller","user_id":1},80,25)
                assert await endpoint.read() == b"VISIBLE"
                await task
        finally:
            if endpoint:
                await endpoint.close()
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            listener.close()
    asyncio.run(scenario())


def test_manual_config_copy_is_byte_exact_and_never_overwrites(tmp_path):
    import runpy
    root = Path(__file__).resolve().parent.parent
    copy = runpy.run_path(str(root / "scripts/copy_dos_config.py"))["copy_config"]
    target = tmp_path / "NODE1.DAT"
    copy(root / "examples/doors/lord/NODE1.DAT", target)
    assert b"BBSDROP D:\\\r\n" in target.read_bytes()
    assert b"\n" not in target.read_bytes().replace(b"\r\n", b"")
    with pytest.raises(FileExistsError):
        copy(root / "examples/doors/lord/NODE1.DAT", target)
    binary = tmp_path / "TWNODE.DAT"
    copy(root / "examples/doors/tradewars/TWNODE.DAT.hex", binary, hexadecimal=True)
    data = binary.read_bytes()
    assert len(data) == 344
    assert data[253:257] == b"\x03D:\\"
    assert data[327:344] == b"\x04WWIV\x01\x02\x01\x04FOSS\x04\x00\x00\x00"
    global_config = (root / "examples/doors/global-war/WAR.CFG").read_text().splitlines()
    assert len(global_config) == 59 and global_config[8].startswith("U ")
    assert global_config[39].startswith("3F8,4 ")
    assert all(":" in line.split(" ",1)[1] for line in global_config[47:50])


def test_smoke_harness_does_not_certify_an_early_exit(tmp_path):
    import runpy
    from types import SimpleNamespace
    from netbbs.doors.runtime import DoorRunResult
    root = Path(__file__).resolve().parent.parent
    harness = runpy.run_path(str(root / "scripts/door_compat_smoke.py"))
    async def exits_early(*args, **kwargs):
        return DoorRunResult(0,0,"exited")
    harness["scenario"].__globals__["run_door"] = exits_early
    args = SimpleNamespace(profile=str(root / "src/netbbs/doors/presets/native-stdio.json"),
                           install=str(tmp_path),emulator=sys.executable,auto_page=False,
                           input_file=None,input='[["Never reached","Q"]]',seconds=1,no_diagnostics=True)
    assert asyncio.run(harness["scenario"](args)) == 1


def test_profile_import_only_reads_bounded_regular_files(tmp_path):
    from netbbs.doors.profiles import read_profile_file
    small = tmp_path / "profile.json"
    small.write_bytes(b"{}")
    assert read_profile_file(str(small)) == b"{}"
    small.write_bytes(b" " * 16385)
    with pytest.raises(ProfileError,match="16 KiB"):
        read_profile_file(str(small))
    if os.name == "posix":
        fifo = tmp_path / "fifo"
        os.mkfifo(fifo)
        with pytest.raises(ProfileError,match="regular file"):
            read_profile_file(str(fifo))


@pytest.mark.skipif(os.name != "posix", reason="POSIX multi-node certification")
def test_two_native_callers_have_separate_nodes_and_persistent_installation(db, lane, player, tmp_path):
    script = _write_script(tmp_path, "multi.py", '''
        import os, pathlib
        node = os.environ['NETBBS_DOOR_NODE']
        scratch = pathlib.Path(os.environ['NETBBS_DOOR_NODE_DIR'])
        assert (scratch / 'DOOR.SYS').read_text().splitlines()[3] == node
        pathlib.Path('node' + node + '.txt').write_text(str(scratch))
        os.write(1,b'READY')
        assert os.read(0,1) == b'X'
    ''')
    profile = DoorProfile(install_dir=str(tmp_path), max_sessions=2, multinode_certified=True,
                          drop_files=("DOOR.SYS",))
    door = create_door(db,"Multi",sys.executable,args=(str(script),),creator=player,profile=profile)
    async def scenario():
        sessions = [FakeSession(),FakeSession()]
        tasks = [asyncio.create_task(run_door(s,lane,door,player)) for s in sessions]
        try:
            async with asyncio.timeout(10):
                while not all(b"READY" in s.written for s in sessions):
                    await asyncio.sleep(0.01)
                assert (await run_door(FakeSession(),lane,door,player)).reason == "busy"
                for session in sessions:
                    session.type_in("X")
                assert all(r.reason == "exited" for r in await asyncio.gather(*tasks))
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
    asyncio.run(scenario())
    first, second = ((tmp_path / f"node{n}.txt").read_text() for n in (1,2))
    assert first != second and not Path(first).exists() and not Path(second).exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX DOS multi-node certification")
def test_two_dos_callers_keep_separate_persistent_scores(db, lane, player, tmp_path):
    emulator, assembler = shutil.which("dosbox-x"), shutil.which("nasm")
    if not emulator or not assembler:
        pytest.skip("Operator-installed DOSBox-X and NASM required")
    subprocess.run([assembler,"-DSTATE=1","-f","bin","-o",str(tmp_path / "SERIAL.COM"),
                    str(Path(__file__).parent / "fixtures/door_serial.asm")],check=True)
    profile = DoorProfile(adapter="dosbox",endpoint="socketpair",install_dir=str(tmp_path),
                          encoding="cp437",width=80,height=25,memory_mb=1024,
                          max_sessions=2,multinode_certified=True,options={"command":"SERIAL.COM {node}"})
    door = create_door(db,"DOS multi",emulator,creator=player,profile=profile)
    async def scenario():
        for _ in range(2):
            sessions = [FakeSession(),FakeSession()]
            for session in sessions:
                session.terminal_height = 25
            tasks = [asyncio.create_task(run_door(s,lane,door,player,wall_time_limit_seconds=20)) for s in sessions]
            try:
                async with asyncio.timeout(25):
                    while not all(b"DOS READY" in s.written for s in sessions):
                        assert not any(t.done() for t in tasks), [t.result() for t in tasks if t.done()]
                        await asyncio.sleep(0.01)
                    for session in sessions:
                        session.type_in("Q")
                    results = await asyncio.gather(*tasks)
                    assert all(r.reason == "exited" for r in results), results
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
    asyncio.run(scenario())
    scores = {p.name.upper():p.read_bytes() for p in tmp_path.iterdir() if p.suffix.upper()==".DAT"}
    assert scores == {"NODE1.DAT":b"XX","NODE2.DAT":b"XX"}


@pytest.mark.skipif(os.name != "posix", reason="POSIX DOS process ownership")
@pytest.mark.parametrize("ending", ["crash", "timeout", "disconnect", "shutdown"])
def test_real_emulator_abnormal_exit_reaps_and_releases_node(ending, db, lane, player, tmp_path, monkeypatch):
    from netbbs.doors.endpoints import NodeLease
    from netbbs.doors.probe import UART_PROBE

    emulator = shutil.which("dosbox-x")
    if not emulator:
        pytest.skip("Operator-installed DOSBox-X required")
    (tmp_path / "SERIAL.COM").write_bytes(UART_PROBE)
    (tmp_path / "persistent.txt").write_text("keep")
    profile = DoorProfile(adapter="dosbox", endpoint="socketpair", install_dir=str(tmp_path),
                          encoding="cp437", width=80, height=25, memory_mb=1024,
                          options={"command": "SERIAL.COM"})
    door = create_door(db, "DOS cleanup", emulator, creator=player, profile=profile)
    spawned = []
    original_spawn = asyncio.create_subprocess_exec

    async def observe_spawn(*args, **kwargs):
        proc = await original_spawn(*args, **kwargs)
        spawned.append((proc, Path(kwargs["cwd"])))
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_spawn)

    async def scenario():
        session = FakeSession()
        session.terminal_height = 25
        task = asyncio.create_task(run_door(session, lane, door, player, wall_time_limit_seconds=12))
        try:
            # The runtime watchdog starts after spawn; allow slow emulator
            # initialization as well as the twelve-second relay timeout.
            async with asyncio.timeout(40):
                while b"DOS READY" not in session.written:
                    assert not task.done(), task.result()
                    await asyncio.sleep(0.01)
                if ending == "crash":
                    os.kill(spawned[0][0].pid, signal.SIGKILL)
                elif ending == "disconnect":
                    session.disconnect()
                elif ending == "shutdown":
                    task.cancel()
                if ending == "shutdown":
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    result = await task
                    assert result.reason == {"crash": "crashed", "timeout": "timed_out",
                                             "disconnect": "caller_disconnected"}[ending], result
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert len(spawned) == 1
    proc, scratch = spawned[0]
    assert proc.returncode is not None
    with pytest.raises(ChildProcessError):
        os.waitpid(proc.pid, os.WNOHANG)
    with pytest.raises(ProcessLookupError):
        os.killpg(proc.pid, 0)
    assert not scratch.exists()
    assert (tmp_path / "persistent.txt").read_text() == "keep"
    lease = NodeLease(db.path.parent / "door-nodes", str(tmp_path.resolve()), 1)
    lease.close()
