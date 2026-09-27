"""Operations → Node → Monitor, the live session table (issue #763)."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.moderation.log import list_recent_actions
from netbbs.net import sysop_monitor
from netbbs.net.admin_flow import admin_menu, disconnect_session_draft
from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.live_screen import SHOW_CURSOR, KeyOutcome, run_live_screen
from netbbs.net.maintenance import MaintenanceMode
from netbbs.net.session import Session
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.net.shutdown import NodeControls
from netbbs.net.sysop_monitor import DROP_ORDER, MonitorState, layout_columns, paint_monitor, short_duration
from netbbs.rendering.ansi import strip_ansi
from netbbs.rendering.screen_buffer import ScreenBuffer
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

_KINDS = {"UP": EditorKeyKind.UP, "DOWN": EditorKeyKind.DOWN, "ESCAPE": EditorKeyKind.ESCAPE}


class QueueSession(Session):
    """Keys and lines arrive when a test puts them, so ticks can pass
    between them."""

    transport_name = "ssh"

    def __init__(self, width: int = 80, height: int = 24):
        self.terminal_width = width
        self.terminal_height = height
        self.node_display_name = "ReLink"
        self.peer_address = "192.0.2.10"
        self.written: list[str] = []
        self.inputs: asyncio.Queue[str] = asyncio.Queue()

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
        return await self.inputs.get()

    async def read_key(self, echo: bool = True) -> str:
        return await self.inputs.get()

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False) -> EditorKey:
        raw = await self.inputs.get()
        if raw in _KINDS:
            return EditorKey(_KINDS[raw])
        if raw.startswith("CTRL+"):
            return EditorKey(EditorKeyKind.CTRL, char=raw[5:])
        return EditorKey(EditorKeyKind.CHAR, char=raw)

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError

    def text(self) -> str:
        return strip_ansi("".join(self.written))


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
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)


def _controls() -> NodeControls:
    return NodeControls(
        session_registry=ActiveSessionRegistry(),
        maintenance=MaintenanceMode(),
        shutdown_event=asyncio.Event(),
        graceful_delay_seconds=60.0,
    )


async def _connect(registry: ActiveSessionRegistry, session: Session, username: str | None) -> asyncio.Task:
    """A caller's connection task, as `handle_session` runs one."""
    ready = asyncio.Event()

    async def connection():
        registry.enter(session)
        if username is not None:
            registry.mark_authenticated(session, username)
        ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            registry.leave(session)

    task = asyncio.create_task(connection())
    await ready.wait()
    return task


def _monitor(viewer, lane, sysop, controls):
    return sysop_monitor.monitor_screen(
        viewer, lane, sysop, controls,
        disconnect=lambda entry: disconnect_session_draft(viewer, lane, sysop, controls, entry),
    )


def _rows(buffer: ScreenBuffer) -> list[str]:
    return ["".join(cell.char for cell in row) for row in buffer.snapshot()]


async def _until(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.005)


# -- layout ------------------------------------------------------------------


def test_every_column_fits_at_80():
    columns, doing = layout_columns(80)
    assert [c.key for c in columns] == ["id", "user", "via", "peer", "on", "idle", "term"]
    assert doing >= sysop_monitor.DOING_MIN_WIDTH


@pytest.mark.parametrize("width", range(12, 201))
def test_narrow_terminals_drop_whole_columns_in_the_fixed_order(width):
    columns, doing = layout_columns(width)
    keys = [c.key for c in columns]
    dropped = [key for key in DROP_ORDER if key not in keys]
    # Dropped in order: whatever went, went before everything after it.
    assert dropped == list(DROP_ORDER[: len(dropped)])
    for key in ("id", "user", "on", "idle"):
        assert key in keys
    # The row never needs more than the terminal has.
    assert sum(c.width + 1 for c in columns) + doing <= max(width, sum(c.width + 1 for c in columns))


def test_a_long_trail_keeps_its_most_specific_end():
    trail = ("Communities", "Boards", "Retro computing")
    assert sysop_monitor.fit_doing(trail, 60, authenticated=True) == "Communities › Boards › Retro computing"
    assert sysop_monitor.fit_doing(trail, 28, authenticated=True) == "… › Boards › Retro computing"
    assert sysop_monitor.fit_doing(trail, 20, authenticated=True) == "… › Retro computing"


def test_short_durations():
    assert short_duration(3) == "0:03"
    assert short_duration(125) == "2:05"
    assert short_duration(3600 * 12 + 300) == "12h05"
    assert short_duration(86400 * 3 + 3600 * 4) == "3d04h"
    assert all(len(short_duration(s)) <= 5 for s in (0, 59, 3599, 3600, 86399, 86400, 86400 * 99, 86400 * 400))


# -- painting ----------------------------------------------------------------


def test_the_table_shows_who_is_doing_what():
    async def scenario():
        controls = _controls()
        registry = controls.session_registry
        viewer, alice, stranger = QueueSession(), QueueSession(), QueueSession()
        alice.activity = ("Boards", "Retro")
        alice.peer_address = "203.0.113.9"
        tasks = [
            await _connect(registry, viewer, "sysop"),
            await _connect(registry, alice, "alice"),
            await _connect(registry, stranger, None),
        ]
        buffer = ScreenBuffer(80, 24)
        paint_monitor(buffer, MonitorState(viewer=viewer), controls)
        rows = _rows(buffer)
        assert rows[0].startswith("ReLink · 3 callers · up ")
        assert rows[1].split() == ["#", "USER", "VIA", "FROM", "ON", "IDLE", "TERM", "DOING"]
        alice_row = next(row for row in rows if "alice" in row)
        assert "203.0.113.9" in alice_row and "80x24" in alice_row
        assert "Boards › Retro" in alice_row
        assert any("(login)" in row and "Logging in" in row for row in rows)
        assert any("sysop" in row and "Main menu" in row for row in rows)
        assert "login (ssh): alice" in "\n".join(rows)
        assert "[M]essage" in rows[-1] and "[K]ick" in rows[-1] and "[U]nwind" in rows[-1]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_a_narrow_terminal_drops_columns_instead_of_wrapping():
    async def scenario():
        controls = _controls()
        viewer = QueueSession(width=50)
        task = await _connect(controls.session_registry, viewer, "sysop")
        buffer = ScreenBuffer(50, 24)
        paint_monitor(buffer, MonitorState(viewer=viewer), controls)
        heading = _rows(buffer)[1].split()
        assert "FROM" not in heading and "TERM" not in heading
        assert "[Q]uit" in _rows(buffer)[-1]
        assert heading[:2] == ["#", "USER"] and "IDLE" in heading and "DOING" in heading
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_the_selection_stays_visible_when_the_list_is_longer_than_the_screen():
    async def scenario():
        controls = _controls()
        registry = controls.session_registry
        sessions = [QueueSession() for _ in range(30)]
        tasks = [await _connect(registry, s, f"user{i:02d}") for i, s in enumerate(sessions)]
        state = MonitorState(viewer=sessions[0])
        state.selected_id = registry.list_entries()[-1].session_id
        buffer = ScreenBuffer(80, 24)
        paint_monitor(buffer, state, controls)
        assert any("user29" in row for row in _rows(buffer))
        assert state.top > 0
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


# -- the live screen ---------------------------------------------------------


def test_ticks_repaint_only_what_changed_and_never_touch_the_database(db, lane, sysop, monkeypatch):
    monkeypatch.setattr(sysop_monitor, "REFRESH_SECONDS", 0.01)
    calls = []
    real_run = lane.run

    async def counting_run(*args, **kwargs):
        calls.append(args[0])
        return await real_run(*args, **kwargs)

    monkeypatch.setattr(lane, "run", counting_run)

    async def scenario():
        controls = _controls()
        viewer, alice = QueueSession(), QueueSession()
        tasks = [await _connect(controls.session_registry, viewer, "sysop"),
                 await _connect(controls.session_registry, alice, "alice")]
        monitor = asyncio.create_task(_monitor(viewer, lane, sysop, controls))
        await _until(lambda: "Main menu" in viewer.text())
        entry_calls = len(calls)
        alice.activity = ("Doors", "Voidrunner")
        await _until(lambda: "Voidrunner" in viewer.text())
        await asyncio.sleep(0.05)  # several more ticks
        assert len(calls) == entry_calls, "a refresh tick went to the database"
        # After the first frame, ticks send diffs: exactly one full clear.
        assert sum(chunk.count("\x1b[2J") for chunk in viewer.written) == 1
        viewer.inputs.put_nowait("q")
        await monitor
        assert viewer.written[-1].startswith(SHOW_CURSOR)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_resize_and_ctrl_l_repaint_everything(db, lane, sysop, monkeypatch):
    monkeypatch.setattr(sysop_monitor, "REFRESH_SECONDS", 0.01)

    async def scenario():
        controls = _controls()
        viewer = QueueSession()
        task = await _connect(controls.session_registry, viewer, "sysop")
        monitor = asyncio.create_task(_monitor(viewer, lane, sysop, controls))
        clears = lambda: sum(chunk.count("\x1b[2J") for chunk in viewer.written)  # noqa: E731
        await _until(lambda: clears() == 1)
        viewer.terminal_width = 100
        await _until(lambda: clears() == 2)
        viewer.inputs.put_nowait("CTRL+l")
        await _until(lambda: clears() == 3)
        viewer.inputs.put_nowait("q")
        await monitor
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_a_pending_key_read_survives_ticks_and_is_cleaned_up_on_cancel():
    async def scenario():
        session = QueueSession()
        reads = []
        original = session.read_editor_key

        async def counted(**kwargs):
            reads.append(1)
            return await original(**kwargs)

        session.read_editor_key = counted

        async def on_key(key):
            return KeyOutcome.CONTINUE

        screen = asyncio.create_task(
            run_live_screen(session, paint=lambda b: None, on_key=on_key, on_notice=lambda text: None, interval=0.01)
        )
        await asyncio.sleep(0.08)
        assert len(reads) == 1, "a tick started a second key read"
        screen.cancel()
        with pytest.raises(asyncio.CancelledError):
            await screen
        assert session.written[-1].startswith(SHOW_CURSOR)

    asyncio.run(scenario())


# -- actions -----------------------------------------------------------------


def _select(viewer: QueueSession, controls: NodeControls, username: str) -> None:
    """Move the highlight onto `username` with Down presses."""
    ids = [e.username for e in sorted(controls.session_registry.list_entries(), key=lambda e: e.session_id)]
    for _ in range(ids.index(username)):
        viewer.inputs.put_nowait("DOWN")


def test_message_reaches_the_selected_caller(db, lane, sysop):
    async def scenario():
        controls = _controls()
        viewer, alice = QueueSession(), QueueSession()
        tasks = [await _connect(controls.session_registry, viewer, "sysop"),
                 await _connect(controls.session_registry, alice, "alice")]
        monitor = asyncio.create_task(_monitor(viewer, lane, sysop, controls))
        _select(viewer, controls, "alice")
        for key in ("m", "time to log off soon"):
            viewer.inputs.put_nowait(key)
        await _until(lambda: "Message sent to alice." in viewer.text())
        assert "Message from the SysOp: time to log off soon" in strip_ansi("".join(alice.written))
        viewer.inputs.put_nowait("q")
        await monitor
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_kick_disconnects_logs_and_says_so(db, lane, sysop):
    async def scenario():
        controls = _controls()
        viewer, alice = QueueSession(), QueueSession()
        create_user(db, "alice", password="hunter2")
        tasks = [await _connect(controls.session_registry, viewer, "sysop"),
                 await _connect(controls.session_registry, alice, "alice")]
        monitor = asyncio.create_task(_monitor(viewer, lane, sysop, controls))
        _select(viewer, controls, "alice")
        # Who's disconnect draft: [M]essage, then [D]isconnect and confirm.
        for key in ("k", "m", "maintenance", "d", "y"):
            viewer.inputs.put_nowait(key)
        await _until(lambda: "'alice' disconnected." in viewer.text())
        assert tasks[1].done()
        assert "*** maintenance ***" in strip_ansi("".join(alice.written))
        assert any("disconnected by sysop: alice" in e.text for e in controls.session_registry.recent_events())
        viewer.inputs.put_nowait("q")
        await monitor
        tasks[0].cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())
    (action,) = [a for a in list_recent_actions(db) if a.action == "disconnect_session"]
    assert "maintenance" in action.detail


def test_kick_can_be_declined(db, lane, sysop):
    async def scenario():
        controls = _controls()
        viewer, alice = QueueSession(), QueueSession()
        tasks = [await _connect(controls.session_registry, viewer, "sysop"),
                 await _connect(controls.session_registry, alice, "alice")]
        monitor = asyncio.create_task(_monitor(viewer, lane, sysop, controls))
        _select(viewer, controls, "alice")
        mark = len(viewer.written)
        for key in ("k", "d", "n", "b"):
            viewer.inputs.put_nowait(key)
        await _until(lambda: "DOING" in strip_ansi("".join(viewer.written[mark:])) and viewer.inputs.empty())
        await asyncio.sleep(0.05)
        assert not tasks[1].done()
        assert "Cancelled." in viewer.text()
        viewer.inputs.put_nowait("q")
        await monitor
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_unwind_is_refused_outside_the_main_menu_and_sent_inside_it(db, lane, sysop):
    async def scenario():
        controls = _controls()
        registry = controls.session_registry
        viewer, alice = QueueSession(), QueueSession()
        tasks = [await _connect(registry, viewer, "sysop"), await _connect(registry, alice, "alice")]
        monitor = asyncio.create_task(_monitor(viewer, lane, sysop, controls))
        _select(viewer, controls, "alice")
        viewer.inputs.put_nowait("u")
        await _until(lambda: "can't be sent back right now" in viewer.text())
        registry.arm_level_unwind(alice, True)
        viewer.inputs.put_nowait("u")
        await _until(lambda: "on their way back to the main menu" in viewer.text())
        viewer.inputs.put_nowait("q")
        await monitor
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_actions_on_your_own_session_are_refused(db, lane, sysop):
    async def scenario():
        controls = _controls()
        viewer = QueueSession()
        task = await _connect(controls.session_registry, viewer, "sysop")
        monitor = asyncio.create_task(_monitor(viewer, lane, sysop, controls))
        viewer.inputs.put_nowait("k")
        await _until(lambda: "That's your own session." in viewer.text())
        viewer.inputs.put_nowait("q")
        await monitor
        assert not task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_the_node_menu_opens_the_monitor(db, lane, sysop):
    async def scenario():
        controls = _controls()
        viewer = QueueSession()
        task = await _connect(controls.session_registry, viewer, "sysop")
        for key in ("o", "n", "o", "q", "b", "b", "b"):
            viewer.inputs.put_nowait(key)
        await admin_menu(viewer, lane, sysop, node_controls=controls)
        text = viewer.text()
        assert "M[o]nitor" in text
        assert "DOING" in text
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(40, 12), (30, 8), (12, 3), (1, 1), (200, 60)])
def test_any_terminal_size_paints_without_error(size):
    async def scenario():
        controls = _controls()
        registry = controls.session_registry
        sessions = [QueueSession() for _ in range(5)]
        tasks = [await _connect(registry, s, f"user{i}") for i, s in enumerate(sessions)]
        buffer = ScreenBuffer(*size)
        paint_monitor(buffer, MonitorState(viewer=sessions[0]), controls)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


# -- review follow-ups -------------------------------------------------------


def test_a_notice_for_the_sysop_lands_on_the_outcome_line_not_over_the_table(db, lane, sysop, monkeypatch):
    monkeypatch.setattr(sysop_monitor, "REFRESH_SECONDS", 10.0)  # only the notice may repaint

    async def scenario():
        controls = _controls()
        viewer = QueueSession()
        task = await _connect(controls.session_registry, viewer, "sysop")
        monitor = asyncio.create_task(_monitor(viewer, lane, sysop, controls))
        await _until(lambda: "DOING" in viewer.text())
        mark = len(viewer.written)
        assert await controls.session_registry.notify_one(viewer, "\r\n*** Node going down in 5 minutes ***")
        await _until(lambda: "Node going down in 5 minutes" in strip_ansi("".join(viewer.written[mark:])))
        # Painted as cells, never written raw with its line breaks.
        assert not any("\r\n" in chunk for chunk in viewer.written[mark:])
        viewer.inputs.put_nowait("q")
        await monitor
        assert viewer.pinned_notice_hook is None
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_a_notice_during_an_action_takes_the_ordinary_route(db, lane, sysop):
    async def scenario():
        controls = _controls()
        viewer, alice = QueueSession(), QueueSession()
        tasks = [await _connect(controls.session_registry, viewer, "sysop"),
                 await _connect(controls.session_registry, alice, "alice")]
        monitor = asyncio.create_task(_monitor(viewer, lane, sysop, controls))
        _select(viewer, controls, "alice")
        viewer.inputs.put_nowait("m")
        await _until(lambda: "Message to alice" in viewer.text())
        # The prompt owns the terminal: no live-screen hook is installed.
        assert viewer.pinned_notice_hook is None
        assert await controls.session_registry.notify_one(viewer, "*** Node going down ***")
        assert "*** Node going down ***" in viewer.text()
        viewer.inputs.put_nowait("hi")
        await _until(lambda: "Message sent to alice." in viewer.text())
        assert viewer.pinned_notice_hook is not None
        viewer.inputs.put_nowait("q")
        await monitor
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_the_kick_draft_gets_a_visible_cursor(db, lane, sysop):
    async def scenario():
        controls = _controls()
        viewer, alice = QueueSession(), QueueSession()
        tasks = [await _connect(controls.session_registry, viewer, "sysop"),
                 await _connect(controls.session_registry, alice, "alice")]
        monitor = asyncio.create_task(_monitor(viewer, lane, sysop, controls))
        _select(viewer, controls, "alice")
        mark = len(viewer.written)
        viewer.inputs.put_nowait("k")
        await _until(lambda: "Disconnect alice" in strip_ansi("".join(viewer.written[mark:])))
        shown = "".join(viewer.written[mark:])
        assert shown.rfind(SHOW_CURSOR) > shown.rfind("\x1b[?25l")
        viewer.inputs.put_nowait("b")
        viewer.inputs.put_nowait("q")
        await monitor
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_a_long_outcome_wraps_instead_of_losing_its_tail():
    async def scenario():
        controls = _controls()
        viewer = QueueSession(width=40)
        task = await _connect(controls.session_registry, viewer, "sysop")
        state = MonitorState(viewer=viewer)
        state.say("averyveryverylongusername_of_32ch can't be sent back right now. Try again in a moment.")
        buffer = ScreenBuffer(40, 24)
        paint_monitor(buffer, state, controls)
        text = " ".join(row.strip() for row in _rows(buffer))
        assert "Try again in a moment." in text
        assert "[Q]" in _rows(buffer)[-1]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_a_wide_node_name_still_leaves_the_flags_room():
    async def scenario():
        controls = _controls()
        viewer = QueueSession(width=40)
        viewer.node_display_name = "掲示板" * 8
        task = await _connect(controls.session_registry, viewer, "sysop")
        controls.shutdown_scheduler.is_scheduled = lambda: True
        controls.shutdown_scheduler.remaining_seconds = lambda: 120
        controls.shutdown_scheduler.is_cancellable = lambda: True
        buffer = ScreenBuffer(40, 24)
        paint_monitor(buffer, MonitorState(viewer=viewer), controls)
        assert "shutdown in" in _rows(buffer)[0]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_caller_names_use_the_node_accent():
    async def scenario():
        controls = _controls()
        viewer, alice = QueueSession(), QueueSession()
        tasks = [await _connect(controls.session_registry, viewer, "sysop"),
                 await _connect(controls.session_registry, alice, "alice")]
        buffer = ScreenBuffer(80, 24)
        paint_monitor(buffer, MonitorState(viewer=viewer, accent_color=141), controls)
        row = next(i for i, text in enumerate(_rows(buffer)) if "alice" in text)
        column = _rows(buffer)[row].index("alice")
        assert buffer.get_cell(row, column).fg == 141
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_combining_marks_keep_their_accent():
    from netbbs.net.live_screen import paint_text

    buffer = ScreenBuffer(10, 1)
    end = paint_text(buffer, 0, 0, "Cafe\u0301!")
    assert end == 5
    assert _rows(buffer)[0].startswith("Cafe\u0301!")


def test_long_ids_and_names_stay_distinguishable():
    async def scenario():
        controls = _controls()
        registry = controls.session_registry
        registry._next_session_id = 1000
        viewer, a, b = QueueSession(), QueueSession(), QueueSession()
        tasks = [await _connect(registry, viewer, "sysop"),
                 await _connect(registry, a, "averyverylongname_one"),
                 await _connect(registry, b, "averyverylongname_two")]
        buffer = ScreenBuffer(80, 24)
        paint_monitor(buffer, MonitorState(viewer=viewer), controls)
        text = "\n".join(_rows(buffer))
        assert "1001" in text and "1002" in text
        assert "averyverylo…" in text
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_time_online_follows_the_monotonic_clock(monkeypatch):
    from netbbs.net import session_registry

    clock = [500.0]
    monkeypatch.setattr(session_registry.time, "monotonic", lambda: clock[0])

    async def scenario():
        registry = ActiveSessionRegistry()
        session = QueueSession()
        registry.enter(session)
        clock[0] = 500.0 + 3725
        (entry,) = registry.list_entries()
        assert entry.connected_seconds == 3725
        registry.leave(session)

    asyncio.run(scenario())


def test_the_more_marker_fits_a_very_narrow_terminal():
    async def scenario():
        controls = _controls()
        sessions = [QueueSession() for _ in range(20)]
        tasks = [await _connect(controls.session_registry, s, f"u{i}") for i, s in enumerate(sessions)]
        paint_monitor(ScreenBuffer(8, 14), MonitorState(viewer=sessions[0]), controls)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_the_header_keeps_its_flags_on_a_narrow_terminal():
    async def scenario():
        controls = _controls()
        viewer = QueueSession(width=40)
        viewer.node_display_name = "A Very Long Bulletin Board Node Name!!"
        task = await _connect(controls.session_registry, viewer, "sysop")
        controls.drain_scheduler.is_scheduled = lambda: True
        controls.drain_scheduler.remaining_seconds = lambda: 90
        buffer = ScreenBuffer(40, 24)
        paint_monitor(buffer, MonitorState(viewer=viewer), controls)
        header = _rows(buffer)[0]
        assert "drain in" in header
        assert header.startswith("A Very")

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_the_most_urgent_flag_survives_when_flags_alone_overflow():
    async def scenario():
        controls = _controls()
        viewer = QueueSession(width=40)
        task = await _connect(controls.session_registry, viewer, "sysop")
        controls.maintenance.is_lockdown_active = lambda: True
        for scheduler in (controls.drain_scheduler, controls.shutdown_scheduler):
            scheduler.is_scheduled = lambda: True
            scheduler.remaining_seconds = lambda: 5400
        buffer = ScreenBuffer(40, 24)
        paint_monitor(buffer, MonitorState(viewer=viewer), controls)
        assert "shutdown in" in _rows(buffer)[0]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_ascii_glyphs_when_unicode_styling_is_off():
    async def scenario():
        controls = _controls()
        registry = controls.session_registry
        sessions = [QueueSession() for _ in range(30)]
        sessions[1].activity = ("Communities", "Boards", "A board with a long name")
        tasks = [await _connect(registry, s, f"user{i}") for i, s in enumerate(sessions)]
        buffer = ScreenBuffer(80, 24)
        paint_monitor(buffer, MonitorState(viewer=sessions[0], glyphs=sysop_monitor.ASCII_GLYPHS), controls)
        text = "\n".join(_rows(buffer))
        assert all(ord(ch) < 128 for ch in text), [ch for ch in text if ord(ch) >= 128]
        assert "... > " in text and "Up/Dn select" in text and "v " in text
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_the_node_name_wears_the_node_branding():
    async def scenario():
        controls = _controls()
        viewer = QueueSession()
        task = await _connect(controls.session_registry, viewer, "sysop")
        buffer = ScreenBuffer(80, 24)
        paint_monitor(buffer, MonitorState(viewer=viewer, header_color=129), controls)
        assert buffer.get_cell(0, 0).fg == 129
        buffer = ScreenBuffer(80, 24)
        paint_monitor(buffer, MonitorState(viewer=viewer, name_gradient="rainbow"), controls)
        assert buffer.get_cell(0, 0).fg != buffer.get_cell(0, 5).fg
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_a_notice_is_on_screen_before_delivery_is_reported(db, lane, sysop, monkeypatch):
    # A kick with a message disconnects right after notify_one returns;
    # the message must already have been drawn by then.
    monkeypatch.setattr(sysop_monitor, "REFRESH_SECONDS", 10.0)

    async def scenario():
        controls = _controls()
        viewer = QueueSession()
        task = await _connect(controls.session_registry, viewer, "sysop")
        monitor = asyncio.create_task(_monitor(viewer, lane, sysop, controls))
        await _until(lambda: "DOING" in viewer.text())
        assert await controls.session_registry.notify_one(viewer, "*** Bye now ***")
        assert "Bye now" in viewer.text()
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        await controls.session_registry.disconnect_one(viewer)
        assert task.done()

    asyncio.run(scenario())


# -- snoop (issue #764) ------------------------------------------------------


class CopyingSession(QueueSession):
    """A caller whose output goes through the real shared output layer,
    so its screen copy is fed exactly as a transport's would be."""

    write = Session.write

    async def _send_text(self, text: str) -> None:
        self.written.append(text)


def test_snoop_shows_the_callers_screen_with_their_cursor():
    async def scenario():
        controls = _controls()
        viewer, alice = QueueSession(), CopyingSession()
        tasks = [await _connect(controls.session_registry, viewer, "sysop"),
                 await _connect(controls.session_registry, alice, "alice")]
        await alice.write("\x1b[2J\x1b[HWelcome to the boards\r\n\x1b[31mRetro\x1b[0m> ")
        entry = next(e for e in controls.session_registry.list_entries() if e.username == "alice")
        buffer = ScreenBuffer(80, 24)
        sysop_monitor.paint_snoop(buffer, entry, controls)
        rows = _rows(buffer)
        assert rows[0].startswith("Watching alice · 80x24")
        assert rows[1].startswith("Welcome to the boards")
        assert rows[2].startswith("Retro> ")
        assert buffer.get_cell(2, 0).fg == 1
        # The caller's cursor, after the prompt, is shown in reverse video.
        assert buffer.get_cell(2, 7).reverse
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_snoop_crops_a_larger_screen_and_says_so():
    async def scenario():
        controls = _controls()
        viewer, alice = QueueSession(), CopyingSession(width=132, height=50)
        tasks = [await _connect(controls.session_registry, viewer, "sysop"),
                 await _connect(controls.session_registry, alice, "alice")]
        await alice.write("x" * 132)
        entry = next(e for e in controls.session_registry.list_entries() if e.username == "alice")
        buffer = ScreenBuffer(80, 24)
        sysop_monitor.paint_snoop(buffer, entry, controls)
        assert "(cropped)" in _rows(buffer)[0]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_snooping_is_logged_and_follows_live_output(db, lane, sysop, monkeypatch, caplog):
    monkeypatch.setattr(sysop_monitor, "SNOOP_REFRESH_SECONDS", 0.01)

    async def scenario():
        controls = _controls()
        viewer, alice = QueueSession(), CopyingSession()
        tasks = [await _connect(controls.session_registry, viewer, "sysop"),
                 await _connect(controls.session_registry, alice, "alice")]
        monitor = asyncio.create_task(_monitor(viewer, lane, sysop, controls))
        _select(viewer, controls, "alice")
        viewer.inputs.put_nowait("s")
        await _until(lambda: "Watching alice" in viewer.text())
        await alice.write("typing_live")
        await _until(lambda: "typing_live" in viewer.text())
        mark = len(viewer.written)
        viewer.inputs.put_nowait("x")  # any key stops
        await _until(lambda: "DOING" in strip_ansi("".join(viewer.written[mark:])))
        viewer.inputs.put_nowait("q")
        await monitor
        assert any("snoop by sysop: alice" in e.text for e in controls.session_registry.recent_events())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    with caplog.at_level("INFO", logger="netbbs.net.sysop_monitor"):
        asyncio.run(scenario())
    messages = [r.getMessage() for r in caplog.records]
    assert any("sysop started watching" in m and "alice" in m for m in messages)
    assert any("sysop stopped watching" in m and "alice" in m for m in messages)


def test_you_cannot_snoop_yourself(db, lane, sysop):
    async def scenario():
        controls = _controls()
        viewer = QueueSession()
        task = await _connect(controls.session_registry, viewer, "sysop")
        monitor = asyncio.create_task(_monitor(viewer, lane, sysop, controls))
        viewer.inputs.put_nowait("s")
        await _until(lambda: "That's your own session." in viewer.text())
        viewer.inputs.put_nowait("q")
        await monitor
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_a_caller_who_leaves_while_watched_is_reported():
    async def scenario():
        controls = _controls()
        viewer, alice = QueueSession(), CopyingSession()
        tasks = [await _connect(controls.session_registry, viewer, "sysop"),
                 await _connect(controls.session_registry, alice, "alice")]
        entry = next(e for e in controls.session_registry.list_entries() if e.username == "alice")
        tasks[1].cancel()
        await asyncio.gather(tasks[1], return_exceptions=True)
        buffer = ScreenBuffer(80, 24)
        sysop_monitor.paint_snoop(buffer, entry, controls)
        assert "alice has disconnected" in _rows(buffer)[0]
        tasks[0].cancel()
        await asyncio.gather(tasks[0], return_exceptions=True)

    asyncio.run(scenario())


def test_a_notice_during_snoop_is_shown_not_swallowed(db, lane, sysop, monkeypatch):
    monkeypatch.setattr(sysop_monitor, "SNOOP_REFRESH_SECONDS", 10.0)

    async def scenario():
        controls = _controls()
        viewer, alice = QueueSession(), CopyingSession()
        tasks = [await _connect(controls.session_registry, viewer, "sysop"),
                 await _connect(controls.session_registry, alice, "alice")]
        monitor = asyncio.create_task(_monitor(viewer, lane, sysop, controls))
        _select(viewer, controls, "alice")
        viewer.inputs.put_nowait("s")
        await _until(lambda: "Watching alice" in viewer.text())
        assert await controls.session_registry.notify_one(viewer, "*** Node going down ***")
        assert "Node going down" in viewer.text()
        viewer.inputs.put_nowait("x")
        viewer.inputs.put_nowait("q")
        await monitor
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_the_cursor_is_hidden_again_after_snoop(db, lane, sysop, monkeypatch):
    monkeypatch.setattr(sysop_monitor, "SNOOP_REFRESH_SECONDS", 0.01)

    async def scenario():
        controls = _controls()
        viewer, alice = QueueSession(), CopyingSession()
        tasks = [await _connect(controls.session_registry, viewer, "sysop"),
                 await _connect(controls.session_registry, alice, "alice")]
        monitor = asyncio.create_task(_monitor(viewer, lane, sysop, controls))
        _select(viewer, controls, "alice")
        viewer.inputs.put_nowait("s")
        await _until(lambda: "Watching alice" in viewer.text())
        mark = len(viewer.written)
        viewer.inputs.put_nowait("x")
        await _until(lambda: "DOING" in strip_ansi("".join(viewer.written[mark:])))
        after = "".join(viewer.written[mark:])
        assert after.rfind("\x1b[?25l") > after.rfind(SHOW_CURSOR)
        viewer.inputs.put_nowait("q")
        await monitor
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())
