"""Tests for netbbs.doors.runtime — the real subprocess sandbox/relay,
exercised against tiny real Python scripts standing in for doors (not
mocked subprocess calls). FakeSession mirrors tests/test_zmodem.py's own
in-memory duplex-pipe double, the same read_byte/write_raw surface this
module actually uses."""

from __future__ import annotations

import asyncio
import base64
import collections
import importlib.util
import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from netbbs.auth.users import create_user
from netbbs.doors import create_door
from netbbs.doors.runtime import DoorRunResult, run_door
from netbbs.net.session import Session, SessionClosedError
from netbbs.rendering.ansi import strip_ansi
from netbbs.rendering.width import display_width
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


@pytest.mark.parametrize("end", ["drain", "disconnect", "timeout"])
@pytest.mark.parametrize("rows", [600, 18000])
def test_final_output_waits_for_slow_caller(end, rows, db, lane, player, tmp_path):
    script = _write_script(tmp_path, "final_output.py",
                           f"import sys; sys.stdout.buffer.write(b'Final score: 42\\n' * {rows}); sys.stdout.buffer.flush()")
    door = create_door(db, "Final output", sys.executable, args=(str(script),), creator=player)

    async def scenario():
        blocked, release = asyncio.Event(), asyncio.Event()

        class SlowSession(FakeSession):
            async def write_raw(self, data):
                blocked.set()
                await release.wait()
                await super().write_raw(data)

        session = SlowSession()
        task = asyncio.create_task(run_door(session, lane, door, player,
                                           wall_time_limit_seconds=1 if end == "timeout" else 10))
        try:
            await asyncio.wait_for(blocked.wait(), 5)
            # The old leader-exit drain deadline silently cancels this write.
            await asyncio.sleep(0.6)
            assert not task.done(), "normal exit discarded blocked terminal output"
            if end == "drain":
                release.set()
            elif end == "disconnect":
                session.disconnect()
            result = await asyncio.wait_for(task, 5)
            assert result.reason == {"drain": "exited", "disconnect": "caller_disconnected",
                                     "timeout": "timed_out"}[end]
            if end == "drain":
                assert session.written == b"Final score: 42\n" * rows
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


@pytest.fixture
def player(db):
    return create_user(db, "keeper", password="hunter2", user_level=10)


# -- fake in-memory duplex Session (mirrors test_zmodem.py's own) ----------


class _BytePipe:
    def __init__(self):
        self._buffer: collections.deque[int] = collections.deque()
        self._event = asyncio.Event()
        self._closed = False

    def feed(self, data: bytes) -> None:
        self._buffer.extend(data)
        self._event.set()

    def close(self) -> None:
        self._closed = True
        self._event.set()

    async def read_byte(self) -> int:
        while not self._buffer:
            if self._closed:
                raise SessionClosedError("pipe closed")
            self._event.clear()
            await self._event.wait()
        return self._buffer.popleft()


class FakeSession(Session):
    def __init__(self):
        self._to_door = _BytePipe()
        self.written = bytearray()

    async def write(self, text: str) -> None:
        self.written.extend(text.encode())

    async def write_raw(self, data: bytes) -> None:
        self.written.extend(data)

    async def read_line(self, echo: bool = True) -> str:
        raise NotImplementedError

    async def read_key(self, echo: bool = True) -> str:
        raise NotImplementedError

    async def read_editor_key(self):
        raise NotImplementedError

    async def close(self) -> None:
        self._to_door.close()

    async def read_byte(self) -> int | None:
        return await self._to_door.read_byte()

    def type_in(self, text: str) -> None:
        self._to_door.feed(text.encode())

    def disconnect(self) -> None:
        self._to_door.close()


def _write_script(tmp_path, name: str, body: str):
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


async def _run(session, lane, door, player, **kwargs) -> DoorRunResult:
    return await run_door(session, lane, door, player, **kwargs)


def test_door_reads_the_drop_file_and_echoes_input(db, lane, player, tmp_path):
    script = _write_script(
        tmp_path, "echo_door.py",
        """
        import json, os, sys
        info = json.load(open(os.environ["NETBBS_DOOR_INFO"]))
        sys.stdout.write("HELLO " + info["handle"] + "\\n")
        sys.stdout.flush()
        line = sys.stdin.readline()
        sys.stdout.write("ECHO " + line.strip() + "\\n")
        sys.stdout.flush()
        """,
    )
    door = create_door(db, "Echo", sys.executable, args=(str(script),), creator=player)

    session = FakeSession()

    async def scenario():
        task = asyncio.create_task(_run(session, lane, door, player))
        await asyncio.sleep(0.2)
        session.type_in("hi there\n")
        return await task

    result = asyncio.run(scenario())

    assert result.reason == "exited"
    assert result.exit_code == 0
    output = bytes(session.written).decode()
    assert "HELLO keeper" in output
    assert "ECHO hi there" in output


def test_drop_file_carries_terminal_size_and_default_color_depth(db, lane, player, tmp_path):
    script = _write_script(
        tmp_path, "dump_info.py",
        """
        import json, os, sys
        info = json.load(open(os.environ["NETBBS_DOOR_INFO"]))
        sys.stdout.write(json.dumps(info))
        sys.stdout.flush()
        """,
    )
    door = create_door(db, "Dump", sys.executable, args=(str(script),), creator=player)
    session = FakeSession()
    session.terminal_width = 100
    session.terminal_height = 40

    result = asyncio.run(_run(session, lane, door, player))

    assert result.exit_code == 0
    info = json.loads(bytes(session.written).decode())
    assert info["terminal_width"] == 100
    assert info["terminal_height"] == 40
    assert info["color_depth"] == "256"
    assert info["user_id"] == player.id


def test_nonzero_exit_is_reported_as_crashed(db, lane, player, tmp_path):
    script = _write_script(tmp_path, "crash_door.py", "import sys; sys.exit(7)")
    door = create_door(db, "Crasher", sys.executable, args=(str(script),), creator=player)
    session = FakeSession()

    result = asyncio.run(_run(session, lane, door, player))

    assert result.reason == "crashed"
    assert result.exit_code == 7


def test_door_that_never_exits_is_killed_on_wall_time_timeout(db, lane, player, tmp_path):
    script = _write_script(tmp_path, "hang_door.py", "import time; time.sleep(60)")
    door = create_door(db, "Hanger", sys.executable, args=(str(script),), creator=player)
    session = FakeSession()

    result = asyncio.run(_run(session, lane, door, player, wall_time_limit_seconds=0.3))

    assert result.reason == "timed_out"


def test_caller_disconnect_ends_the_session_and_kills_the_door(db, lane, player, tmp_path):
    script = _write_script(tmp_path, "wait_forever.py", "import time; time.sleep(60)")
    door = create_door(db, "Waiter", sys.executable, args=(str(script),), creator=player)
    session = FakeSession()

    async def scenario():
        task = asyncio.create_task(_run(session, lane, door, player))
        await asyncio.sleep(0.2)
        session.disconnect()
        return await task

    result = asyncio.run(scenario())

    assert result.reason == "caller_disconnected"


def test_bad_executable_path_is_reported_as_failed_to_start(db, lane, player):
    door = create_door(db, "Broken", "/no/such/executable-netbbs-test", creator=player)
    session = FakeSession()

    result = asyncio.run(_run(session, lane, door, player))

    assert result.reason == "failed_to_start"
    assert result.exit_code is None


@pytest.mark.parametrize("game,keys,expected", [
    ("retro_trivia.py", "A" * 9, b"Final score:"),
    ("voidrunner.py", "\rYQ", b"Docking clamps engaged"),
    ("war_dialer.py", " Q", b"W A R"),
])
def test_web_bundled_trivia_round_restores_menu_on_same_websocket(db, lane, player, tmp_path, monkeypatch, game, keys, expected):
    import aiohttp
    from netbbs.net.web import WebServer

    monkeypatch.setenv("USERPROFILE" if os.name == "nt" else "HOME", str(tmp_path / "door-home"))
    door = create_door(db, "Web bundled", sys.executable, args=(str(_BUNDLED_DOORS_DIR / game),), creator=player)
    results = []

    async def handler(session):
        # Includes interpreter startup on real, potentially emulated POSIX
        # hosts; watchdog behavior has its own short, dedicated tests.
        results.append(await run_door(session, lane, door, player, wall_time_limit_seconds=60))
        await session.write("MENU")
        assert await session.read_key(echo=False) == "B"
        await session.write("BACK")

    async def scenario():
        server = WebServer(host="127.0.0.1", port=0, session_handler=handler)
        await server.start()
        output = bytearray()
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws") as ws:
                    mode = await ws.receive_json(timeout=3)
                    assert mode["type"] == "door_mode" and mode["active"]
                    if game != "war_dialer.py":
                        await ws.send_json({"type": "door_key", "stream": mode["stream"], "data": keys})
                    help_acknowledged = False
                    quit_sent = False
                    while True:
                        msg = await ws.receive_json(timeout=75)
                        if msg["type"] == "door_output":
                            output.extend(base64.b64decode(msg["data"]))
                            if game == "war_dialer.py":
                                # War Dialer rejects queued/pasted action bursts.
                                # Exercise actual single keys at their screens.
                                if not help_acknowledged and b"Press any key to continue..." in output:
                                    await ws.send_json({"type": "door_key", "stream": mode["stream"], "data": " "})
                                    help_acknowledged = True
                                if not quit_sent and b">\x1b[0m " in output:
                                    await ws.send_json({"type": "door_key", "stream": mode["stream"], "data": "Q"})
                                    quit_sent = True
                        elif msg["type"] == "door_mode":
                            assert not msg["active"]
                        elif msg.get("data") == "MENU":
                            await ws.send_json({"type": "key", "data": "B"})
                        elif msg.get("data") == "BACK":
                            break
        finally:
            await server.stop()
        assert expected in output

    asyncio.run(scenario())
    assert results[0].reason == "exited"


def test_play_door_is_audit_logged(db, lane, player, tmp_path):
    from netbbs.moderation.log import list_actions_for_object

    script = _write_script(tmp_path, "quick_exit.py", "pass")
    door = create_door(db, "Quick", sys.executable, args=(str(script),), creator=player)
    session = FakeSession()

    asyncio.run(_run(session, lane, door, player))

    entries = list_actions_for_object(db, object_type="door", object_id=door.id)
    play_entries = [e for e in entries if e.action == "play_door"]
    assert len(play_entries) == 1
    assert play_entries[0].actor_user_id == player.id
    assert "reason=exited" in play_entries[0].detail


# -- the real demo door (netbbs.doors.bundled.retro_trivia) ----------------
#
# Not a throwaway test fixture like every script above -- the actual
# shipped proof-of-concept door, run for real through this same
# run_door pipeline, proving the whole vertical end to end rather than
# just the sandbox mechanics in isolation. Ships as real installed
# package data now (issue #172 follow-up), not a loose examples/ file.

_BUNDLED_DOORS_DIR = Path(__file__).resolve().parent.parent / "src" / "netbbs" / "doors" / "bundled"
_RETRO_TRIVIA_PATH = _BUNDLED_DOORS_DIR / "retro_trivia.py"


@pytest.mark.parametrize(
    "path",
    (
        _BUNDLED_DOORS_DIR / "retro_trivia.py",
        _BUNDLED_DOORS_DIR / "voidrunner.py",
        _BUNDLED_DOORS_DIR / "war_dialer.py",
    ),
)
def test_bundled_door_wrappers_normalize_tabs_and_keep_indentation_with_content(path):
    name = f"door_wrap_under_test_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        tabbed = module._wrap_output("1234\t56789", 10)
        indented = module._wrap_output("  0123456789", 5)
        combining = module._wrap_output("aaa\u0301b", 3)
        boxed = module._wrap_output(
            "\x1b[35m│\x1b[37m" + ("word " * 12) + "\x1b[35m│\x1b[0m",
            20,
        )
    finally:
        sys.modules.pop(name, None)

    assert tabbed == "1234 56789"
    assert indented.split("\r\n") == ["  012", "34567", "89"]
    assert combining.split("\r\n") == ["aaa\u0301", "b"]
    boxed_rows = boxed.split("\r\n")
    assert len(boxed_rows) > 1
    assert all(row.startswith("\x1b[35m│\x1b[37m") for row in boxed_rows)


def test_the_real_demo_door_plays_a_full_round_through_run_door(db, lane, player):
    door = create_door(db, "Retro Trivia", sys.executable, args=(str(_RETRO_TRIVIA_PATH),), creator=player)
    session = FakeSession()
    session.terminal_width = 40

    async def scenario():
        task = asyncio.create_task(_run(session, lane, door, player))
        # 8 questions this round, one keystroke each, then one more to
        # dismiss the final "press any key to leave" prompt.
        for _ in range(9):
            await asyncio.sleep(0.05)
            session.type_in("A")
        return await task

    result = asyncio.run(scenario())

    assert result.reason == "exited"
    assert result.exit_code == 0
    output = bytes(session.written).decode()
    assert "R E T R O" in output  # the title screen's letter-spaced wordmark
    assert "Welcome, " in output
    assert "keeper" in output  # the real caller handle, from the drop-file
    assert "Question 1/8" in output
    assert "Question 8/8" in output
    assert "Final score:" in output
    assert all(display_width(strip_ansi(line)) <= 40 for line in output.splitlines())


# -- the space-trading door (netbbs.doors.bundled.voidrunner) --------------
#
# Same "run the real shipped file through the real pipeline" reasoning as
# Retro Trivia above -- run directly against the real installed file, no
# tmp_path copy needed: voidrunner.py's default save directory is no
# longer relative to its own __file__ (it ships as real installed package
# data now, whose own directory is routinely read-only/wiped on upgrade
# -- see that module's own docstring), so where the script itself lives
# no longer affects where it saves.
#
# What that default *does* still depend on is a real user's home
# directory. `run_door` replaces a door's environment outright but
# explicitly supplies the platform home locator alongside NETBBS_DOOR_INFO,
# keeping persistent state outside the disposable scratch directory.

_VOIDRUNNER_PATH = _BUNDLED_DOORS_DIR / "voidrunner.py"


def test_the_real_space_trading_door_plays_a_full_opening_loop_through_run_door(
    db, lane, player, tmp_path, monkeypatch,
):
    door_home = tmp_path / "door-home"
    monkeypatch.setenv("USERPROFILE" if os.name == "nt" else "HOME", str(door_home))
    door = create_door(db, "Voidrunner", sys.executable, args=(str(_VOIDRUNNER_PATH),), creator=player)
    session = FakeSession()
    session.terminal_width = 40
    save_dir = door_home / ".netbbs" / "voidrunner_saves"
    save_path = save_dir / f"{player.id}.json"

    async def scenario():
        task = asyncio.create_task(_run(session, lane, door, player))
        await asyncio.sleep(0.2)
        # Accept the default callsign, confirm career start, buy 3 Food
        # in the market, back out, check the status screen, then quit.
        session.type_in("\rYMAB3\rQS Q")
        return await task

    result = asyncio.run(scenario())

    assert result.reason == "exited"
    assert result.exit_code == 0
    output = bytes(session.written).decode()
    assert "V O I D R U N N E R" in output  # the title screen's letter-spaced wordmark
    assert "keeper" in output  # the real caller handle, from the drop-file
    assert "Bought 3x Food" in output
    assert "Docking clamps engaged" in output
    overflows = [
        strip_ansi(line)
        for line in output.splitlines()
        if display_width(strip_ansi(line)) > 40
    ]
    assert not overflows
    assert save_path.exists()  # the door manages its own save, unmediated by NetBBS


def test_voidrunner_directory_override_reaches_real_door_without_parent_secrets(
    db, lane, player, tmp_path, monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VOIDRUNNER_SAVE_DIR", "node-two-careers")
    monkeypatch.setenv("NETBBS_TEST_SECRET", "must-not-reach-door")
    script = _write_script(tmp_path, "check_env.py", "import os, json; print(json.dumps(dict(os.environ)))")
    door = create_door(db, "Environment check", sys.executable, args=(str(script),), creator=player)
    session = FakeSession()
    result = asyncio.run(_run(session, lane, door, player))
    assert result.exit_code == 0
    env = json.loads(bytes(session.written).decode())
    assert env["VOIDRUNNER_SAVE_DIR"] == str(tmp_path / "node-two-careers")
    assert "NETBBS_TEST_SECRET" not in env


def test_voidrunner_recovery_back_is_a_normal_door_exit(db, lane, player, tmp_path, monkeypatch):
    save_dir = tmp_path / "careers"
    save_dir.mkdir()
    path = save_dir / f"{player.id}.json"
    path.write_bytes(b"damaged career")
    monkeypatch.setenv("VOIDRUNNER_SAVE_DIR", str(save_dir))
    door = create_door(db, "Voidrunner", sys.executable, args=(str(_VOIDRUNNER_PATH),), creator=player)
    session = FakeSession()
    session.type_in("B")
    result = asyncio.run(_run(session, lane, door, player, wall_time_limit_seconds=5))
    assert result.exit_code == 0 and result.reason == "exited"
    assert b"Career recovery" in bytes(session.written)
    assert path.read_bytes() == b"damaged career"


def test_war_dialer_timeout_does_not_leave_bracketed_paste_enabled(db, lane, player, tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE" if os.name == "nt" else "HOME", str(tmp_path / "door-home"))
    door = create_door(
        db, "War Dialer timeout", sys.executable,
        args=(str(_BUNDLED_DOORS_DIR / "war_dialer.py"),), creator=player,
    )
    session = FakeSession()
    result = asyncio.run(_run(session, lane, door, player, wall_time_limit_seconds=3))
    assert result.reason == "timed_out"
    assert b"W A R" in session.written
    assert b"\x1b[?2004h" not in session.written


def test_war_dialer_default_paths_separate_nodes_without_using_display_names(db, player, tmp_path, monkeypatch):
    from dataclasses import replace
    from netbbs.doors.runtime import war_dialer_world_path
    monkeypatch.delenv("WAR_DIALER_DB_PATH", raising=False)
    door = create_door(db, "First title", sys.executable, args=(str(_BUNDLED_DOORS_DIR / "war_dialer.py"),), creator=player)
    first = war_dialer_world_path(db, door)
    assert first == db.path.resolve().parent / "node.db.doors" / "war-dialer.db"
    assert war_dialer_world_path(db, replace(door, name="Renamed game")) == first
    other = Database(tmp_path / "other.db")
    try:
        assert war_dialer_world_path(other, door) != first
    finally:
        other.close()
    assert not first.exists()


def test_war_dialer_override_is_absolute_and_does_not_forward_parent_secrets(db, lane, player, tmp_path, monkeypatch):
    from netbbs.doors.profiles import DoorProfile
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WAR_DIALER_DB_PATH", "global/world.db")
    monkeypatch.setenv("NETBBS_TEST_SECRET", "not-a-door-setting")
    script = _write_script(tmp_path, "wardialer_wrapper.py", "import os,json; print(json.dumps(dict(os.environ)))")
    profile = DoorProfile(environment={"WAR_DIALER_DB_PATH": "per-profile/world.db"})
    door = create_door(db, "Wrapper", sys.executable, args=(str(script),), creator=player, profile=profile)
    session = FakeSession()
    result = asyncio.run(_run(session, lane, door, player))
    assert result.exit_code == 0
    env = json.loads(bytes(session.written))
    assert env["WAR_DIALER_DB_PATH"] == str(tmp_path / "per-profile" / "world.db")
    assert "NETBBS_TEST_SECRET" not in env


def test_war_dialer_legacy_world_requires_an_explicit_migration_choice(db, lane, player, tmp_path, monkeypatch):
    from netbbs.doors.runtime import war_dialer_world_path
    monkeypatch.delenv("WAR_DIALER_DB_PATH", raising=False)
    home = tmp_path / "legacy-home"
    monkeypatch.setenv("USERPROFILE" if os.name == "nt" else "HOME", str(home))
    legacy = home / ".netbbs" / "wardialer.db"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"original world is preserved")
    door = create_door(db, "War Dialer", sys.executable, args=(str(_BUNDLED_DOORS_DIR / "war_dialer.py"),), creator=player)
    target = war_dialer_world_path(db, door)
    session = FakeSession()
    result = asyncio.run(_run(session, lane, door, player))
    assert result.reason == "failed_to_start"
    assert "Legacy War Dialer world found" in result.diagnostic
    assert str(target) in result.diagnostic
    assert not target.exists()
    assert legacy.read_bytes() == b"original world is preserved"


def test_war_dialer_global_override_and_unrelated_doors(db, player, tmp_path, monkeypatch):
    from netbbs.doors.runtime import war_dialer_world_path, war_dialer_path_problem
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WAR_DIALER_DB_PATH", "selected/world.db")
    door = create_door(db, "War Dialer", sys.executable, args=(str(_BUNDLED_DOORS_DIR / "war_dialer.py"),), creator=player)
    target = war_dialer_world_path(db, door)
    assert target == tmp_path / "selected" / "world.db"
    assert war_dialer_path_problem(door, target) is None
    unrelated = create_door(db, "Other", sys.executable, args=("-c", "print('not this game')"), creator=player)
    assert war_dialer_world_path(db, unrelated) is None


def test_real_war_dialer_launches_keep_two_node_worlds_separate(tmp_path, monkeypatch):
    import sqlite3
    from netbbs.doors.runtime import war_dialer_world_path
    monkeypatch.delenv("WAR_DIALER_DB_PATH", raising=False)
    monkeypatch.setenv("USERPROFILE" if os.name == "nt" else "HOME", str(tmp_path / "shared-home"))
    paths = []
    for name in ("Alpha", "Beta"):
        database = Database(tmp_path / (name + ".db"))
        database_lane = DatabaseLane(database.path)
        try:
            actor = create_user(database, name, password="hunter2", user_level=10)
            door = create_door(database, "Same display title", sys.executable,
                               args=(str(_BUNDLED_DOORS_DIR / "war_dialer.py"),), creator=actor)
            paths.append(war_dialer_world_path(database, door))
            async def scenario():
                session = FakeSession()
                task = asyncio.create_task(_run(session, database_lane, door, actor))
                async def wait_for(marker):
                    while marker not in session.written:
                        if task.done():
                            pytest.fail(f"Door exited early: {task.result()}")
                        await asyncio.sleep(0.01)
                try:
                    await asyncio.wait_for(wait_for(b"Press any key to continue..."), 8)
                    session.type_in(" ")
                    await asyncio.wait_for(wait_for(b">\x1b[0m "), 8)
                    session.type_in("q")
                    assert (await asyncio.wait_for(task, 8)).exit_code == 0
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            asyncio.run(scenario())
        finally:
            database_lane.close()
            database.close()
    assert paths[0] != paths[1]
    for path, name in zip(paths, ("Alpha", "Beta")):
        conn = sqlite3.connect(path)
        try:
            assert conn.execute("SELECT user_id, handle FROM players").fetchall() == [(1, name)]
        finally:
            conn.close()
