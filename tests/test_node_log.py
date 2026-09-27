"""Issue #729: the node's own netbbs.log, read from inside NetBBS."""

from __future__ import annotations

import asyncio
import os

import pytest

from netbbs.net import admin_flow
from netbbs.net.admin_flow import admin_menu
from netbbs.node_log import (
    MAX_FOLLOW_BYTES,
    NodeLogFollower,
    entries_at_or_above,
    node_log_path,
    parse_log_lines,
    read_node_log,
)
from tests.test_admin_flow import (  # noqa: F401 -- fixtures
    FakeSession,
    _normalized_visible,
    _visible,
    _written_text,
    db,
    isolated_door_career_directory,
    lane,
    sysop,
)


def _line(level: str, message: str, *, when: str = "2026-09-27 10:00:00", logger: str = "netbbs.net") -> str:
    return f"{when} {level}:{logger}:{message}\n"


# -- domain ----------------------------------------------------------------


def test_node_log_path_is_beside_the_database(tmp_path):
    assert node_log_path(tmp_path / "node.db") == tmp_path / "netbbs.log"


def test_traceback_lines_belong_to_the_entry_above_them():
    lines = [
        "tail of an entry the read cut into",
        "2026-09-27 10:00:00 ERROR:netbbs.web:upload failed",
        "Traceback (most recent call last):",
        '  File "x.py", line 1, in <module>',
        "OSError: disk full",
        "2026-09-27 10:00:01 INFO:netbbs.net:caller connected",
    ]
    entries = parse_log_lines(lines)
    assert [(e.level, e.message) for e in entries] == [("ERROR", "upload failed"), ("INFO", "caller connected")]
    assert entries[0].continuation == (
        "Traceback (most recent call last):", '  File "x.py", line 1, in <module>', "OSError: disk full",
    )
    assert [e.id for e in entries] == [0, 1]


def test_transfer_tokens_are_masked(tmp_path):
    """The web listener's access log records `/transfer/<token>`, a bearer
    credential that a HEAD request leaves unspent."""
    path = tmp_path / "netbbs.log"
    path.write_text(
        _line("INFO", '127.0.0.1 "HEAD /transfer/AbC-123_xyz HTTP/1.1" 200', logger="aiohttp.access")
        + _line("ERROR", "upload failed")
        + '  while serving "POST /transfer/SeCrEt?x=1"\n',
        encoding="utf-8",
    )

    entries = read_node_log(path).entries
    follower_path = tmp_path / "follow.log"
    follower_path.write_text("", encoding="utf-8")
    follower = NodeLogFollower(follower_path)
    with open(follower_path, "a", encoding="utf-8") as handle:
        handle.write(_line("INFO", "GET /transfer/LiveToken HTTP/1.1", logger="aiohttp.access"))
    followed, _ = follower.poll()

    shown = " ".join(e.full_text for e in [*entries, *followed])
    assert "AbC-123_xyz" not in shown and "SeCrEt" not in shown and "LiveToken" not in shown
    assert "/transfer/<token>" in shown


def test_missing_log_is_reported_as_missing(tmp_path):
    result = read_node_log(tmp_path / "netbbs.log")
    assert result.missing and result.entries == [] and result.error is None


def test_read_is_bounded_to_the_newest_bytes_and_says_so(tmp_path):
    path = tmp_path / "netbbs.log"
    path.write_text("".join(_line("WARNING", f"message {i:04d}") for i in range(200)), encoding="utf-8")

    result = read_node_log(path, max_bytes=1024)

    assert result.truncated
    assert result.entries[-1].message == "message 0199"
    # The read started mid-line; that partial line is not an entry.
    assert all(e.message.startswith("message ") and len(e.message) == len("message 0000") for e in result.entries)
    assert sum(len(_line("WARNING", e.message)) for e in result.entries) <= 1024


def test_short_active_file_is_topped_up_from_the_newest_rotation_only(tmp_path):
    path = tmp_path / "netbbs.log"
    (tmp_path / "netbbs.log.2").write_text(_line("ERROR", "two generations ago"), encoding="utf-8")
    (tmp_path / "netbbs.log.1").write_text(_line("ERROR", "before rotation"), encoding="utf-8")
    path.write_text(_line("ERROR", "after rotation"), encoding="utf-8")

    result = read_node_log(path)

    assert [e.message for e in result.entries] == ["before rotation", "after rotation"]
    assert result.truncated  # netbbs.log.2 exists and was not read


def test_entry_count_is_capped_to_the_newest(tmp_path):
    path = tmp_path / "netbbs.log"
    path.write_text("".join(_line("INFO", f"m{i}") for i in range(50)), encoding="utf-8")

    result = read_node_log(path, max_entries=10)

    assert [e.message for e in result.entries] == [f"m{i}" for i in range(40, 50)]
    assert [e.id for e in result.entries] == list(range(10))
    assert result.truncated


def test_level_floor_filters():
    entries = parse_log_lines([
        _line("INFO", "i").rstrip("\n"), _line("WARNING", "w").rstrip("\n"),
        _line("ERROR", "e").rstrip("\n"), _line("CRITICAL", "c").rstrip("\n"),
    ])
    assert [e.message for e in entries_at_or_above(entries, "WARNING")] == ["w", "e", "c"]
    assert [e.message for e in entries_at_or_above(entries, "ERROR")] == ["e", "c"]


def test_symlinked_log_is_not_followed(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text(_line("ERROR", "not the node log"), encoding="utf-8")
    link = tmp_path / "netbbs.log"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("this platform or account cannot create symlinks")

    result = read_node_log(link)

    assert result.entries == [] and result.error is not None and "symbolic link" in result.error
    entries, error = NodeLogFollower(link).poll()
    assert entries == [] and "symbolic link" in error


def test_follower_reads_only_what_is_appended_and_holds_partial_lines(tmp_path):
    path = tmp_path / "netbbs.log"
    path.write_text(_line("ERROR", "already there"), encoding="utf-8")
    follower = NodeLogFollower(path)
    assert follower.poll() == ([], None)

    with open(path, "a", encoding="utf-8") as handle:
        handle.write(_line("ERROR", "new one"))
        handle.write("2026-09-27 10:00:02 WARNING:netbbs.net:half a li")
    # "new one" may still be followed by a traceback: held back for now.
    entries, error = follower.poll()
    assert error is None and entries == []

    with open(path, "a", encoding="utf-8") as handle:
        handle.write("ne\n")
    # The next entry has started, so "new one" is complete.
    entries, _ = follower.poll()
    assert [e.message for e in entries] == ["new one"]
    # A poll that finds nothing new releases the held entry.
    entries, _ = follower.poll()
    assert [e.message for e in entries] == ["half a line"]


def test_follower_restarts_at_the_top_of_a_rotated_file(tmp_path):
    path = tmp_path / "netbbs.log"
    path.write_text("".join(_line("INFO", f"old {i}") for i in range(20)), encoding="utf-8")
    follower = NodeLogFollower(path)

    path.replace(tmp_path / "netbbs.log.1")
    path.write_text(_line("ERROR", "fresh file"), encoding="utf-8")

    entries, _ = follower.poll()
    entries += follower.poll()[0]
    assert [e.message for e in entries] == ["fresh file"]


def test_follower_keeps_a_traceback_split_across_polls_with_its_entry(tmp_path, monkeypatch):
    """Review round 2 on PR #739: a poll ending between an error line and
    its traceback emitted the entry, and the next poll dropped the orphaned
    traceback lines."""
    import netbbs.node_log as node_log

    path = tmp_path / "netbbs.log"
    path.write_text("", encoding="utf-8")
    follower = NodeLogFollower(path)
    header = _line("ERROR", "upload failed")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(header + "Traceback (most recent call last):\nOSError: disk full\n" + _line("INFO", "next"))
    # The first read ends right after the error line.
    monkeypatch.setattr(node_log, "MAX_FOLLOW_BYTES", len(header.encode()))

    entries = []
    for _ in range(10):
        entries += follower.poll()[0]

    error = next(e for e in entries if e.message == "upload failed")
    assert error.continuation == ("Traceback (most recent call last):", "OSError: disk full")


def test_follower_decodes_a_character_split_across_polls(tmp_path, monkeypatch):
    import netbbs.node_log as node_log

    path = tmp_path / "netbbs.log"
    path.write_text("", encoding="utf-8")
    follower = NodeLogFollower(path)
    line = _line("ERROR", "user Jürgen failed")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line)
    # Split inside the two-byte "ü".
    monkeypatch.setattr(node_log, "MAX_FOLLOW_BYTES", line.encode().index("ü".encode()) + 1)

    entries = []
    for _ in range(10):
        entries += follower.poll()[0]

    assert [e.message for e in entries] == ["user Jürgen failed"]


def test_a_tail_starting_exactly_at_a_line_keeps_that_line(tmp_path):
    path = tmp_path / "netbbs.log"
    newest = _line("ERROR", "the one that matters").encode()
    path.write_bytes(_line("INFO", "older").encode() + newest)

    result = read_node_log(path, max_bytes=len(newest))

    assert [e.message for e in result.entries] == ["the one that matters"]
    assert result.truncated


def test_a_rotated_file_ending_mid_line_does_not_swallow_the_newest_entry(tmp_path):
    path = tmp_path / "netbbs.log"
    (tmp_path / "netbbs.log.1").write_text(_line("INFO", "before the crash") + "2026-09-27 09:00:00 ERROR:x:cut sh", encoding="utf-8")
    path.write_text(_line("CRITICAL", "startup failed"), encoding="utf-8")

    result = read_node_log(path)

    assert result.entries[-1].level == "CRITICAL" and result.entries[-1].message == "startup failed"


def test_follower_drains_the_rotated_file_before_switching(tmp_path):
    """Review round 3 on PR #739: lines written between the last poll and a
    rotation were never read."""
    path = tmp_path / "netbbs.log"
    path.write_text(_line("INFO", "before following"), encoding="utf-8")
    follower = NodeLogFollower(path)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(_line("ERROR", "written just before rotation"))
    path.replace(tmp_path / "netbbs.log.1")
    path.write_text(_line("ERROR", "first in the new file"), encoding="utf-8")

    entries = []
    for _ in range(3):
        entries += follower.poll()[0]

    assert [e.message for e in entries] == ["written just before rotation", "first in the new file"]


def test_follower_bounds_an_overlong_line(tmp_path, monkeypatch):
    """Review round 3 on PR #739: one record longer than a poll grew the
    pending buffer without limit."""
    import netbbs.node_log as node_log

    monkeypatch.setattr(node_log, "MAX_HELD_CHARS", 200)
    monkeypatch.setattr(node_log, "MAX_FOLLOW_BYTES", 64)
    path = tmp_path / "netbbs.log"
    path.write_text("", encoding="utf-8")
    follower = NodeLogFollower(path)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(_line("ERROR", "huge " + "y" * 5000) + _line("ERROR", "after the huge one"))

    entries = []
    for _ in range(200):
        entries += follower.poll()[0]
        assert len(follower._partial) <= 200 + len(" [line cut: longer than NetBBS shows]")

    assert entries[0].message.startswith("huge yyy") and entries[0].message.endswith("[line cut: longer than NetBBS shows]")
    assert entries[-1].message == "after the huge one"


@pytest.mark.parametrize("screen", ["node", "diagnostic"])
def test_follow_screens_raise_a_failed_key_read(db, lane, sysop, screen):
    """Review round 3 on PR #739: a caller hanging up during Follow left the
    read's exception unretrieved and the screen carried on."""

    class _HangUp(FakeSession):
        async def read_key(self, echo: bool = True) -> str:
            raise ConnectionResetError("caller went away")

    session = _HangUp()
    if screen == "node":
        path = node_log_path(db.path)
        path.write_text("", encoding="utf-8")
        run = admin_flow._node_log_tail_screen(session, lane, path, floor="WARNING")
    else:
        run = admin_flow._diagnostic_log_tail_screen(session, lane)
    with pytest.raises(ConnectionResetError):
        asyncio.run(run)


def test_follower_poll_is_bounded(tmp_path):
    path = tmp_path / "netbbs.log"
    path.write_text("", encoding="utf-8")
    follower = NodeLogFollower(path)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("".join(_line("INFO", "x" * 100) for _ in range(2000)))

    entries, _ = follower.poll()

    assert 0 < len(entries) <= MAX_FOLLOW_BYTES // 100


# -- screen ----------------------------------------------------------------


def _write_log(db, text: str) -> None:
    node_log_path(db.path).write_text(text, encoding="utf-8")


def test_operations_offers_node_log_without_a_live_node(db, lane, sysop):
    """The standalone console has no node_controls; the log is a file, and a
    node that will not start is when it is wanted most."""
    _write_log(db, _line("INFO", "hello"))
    session = FakeSession(["o", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=None, link_context=None))
    assert "Node lo[g]" in _visible(_written_text(session))


def test_node_log_shows_warnings_and_errors_by_default_and_level_widens(db, lane, sysop):
    _write_log(db, _line("INFO", "caller connected") + _line("WARNING", "banner file missing")
               + _line("ERROR", "listener failed"))

    session = FakeSession(["o", "g", "l", "l", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))

    text = _visible(_written_text(session))
    first = text[text.index("Node log"):text.index("Showing errors only")]
    assert "banner file missing" in first and "listener failed" in first
    assert "caller connected" not in first
    assert "Showing warnings and errors" in first
    everything = text[text.index("Showing everything") - 2000:]
    assert "caller connected" in everything


def test_node_log_entry_detail_shows_its_traceback(db, lane, sysop):
    _write_log(db, _line("ERROR", "upload failed") + "Traceback (most recent call last):\nOSError: disk full\n")

    session = FakeSession(["o", "g", "0", "1", "b", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))

    visible = _visible(_written_text(session))
    detail = _normalized_visible(visible[visible.index("Node log › Log entry"):])
    assert "Level: ERROR" in detail and "Message: upload failed" in detail
    assert "Traceback (most recent call last):" in detail and "OSError: disk full" in detail


def test_node_log_screen_sanitizes_logged_text(db, lane, sysop):
    """A log line can carry caller-influenced text (a username, a filename):
    it must not reach the SysOp's terminal as control sequences."""
    _write_log(db, _line("ERROR", "bad name \x1b[2J\x1b]0;pwned\x07here"))

    session = FakeSession(["o", "g", "0", "1", "b", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))

    raw = _written_text(session)
    assert "\x1b[2J" not in raw and "\x1b]0;" not in raw


def test_missing_node_log_says_where_it_looked(db, lane, sysop):
    session = FakeSession(["o", "g", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))

    text = _normalized_visible(_written_text(session))
    assert "There is no netbbs.log beside the database yet" in text


def test_follow_prints_new_lines_at_or_above_the_floor(db, lane, sysop, monkeypatch):
    path = node_log_path(db.path)
    path.write_text(_line("ERROR", "before"), encoding="utf-8")
    monkeypatch.setattr(admin_flow, "_DIAGNOSTIC_TAIL_POLL_INTERVAL_SECONDS", 0.05)

    class _SlowKeySession(FakeSession):
        async def read_key(self, echo: bool = True) -> str:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(_line("INFO", "quiet line") + _line("ERROR", "loud line"))
            await asyncio.sleep(0.5)
            return "x"

    session = _SlowKeySession()
    asyncio.run(admin_flow._node_log_tail_screen(session, lane, path, floor="WARNING"))

    text = _visible(_written_text(session))
    assert "loud line" in text
    assert "quiet line" not in text
    assert "before" not in text


# -- review round 1 (PR #739) ----------------------------------------------


def test_a_non_regular_log_is_refused_not_opened(tmp_path):
    """A directory (or, on POSIX, a FIFO) where the log belongs is refused
    with a message instead of blocking or raising."""
    (tmp_path / "netbbs.log").mkdir()
    result = read_node_log(tmp_path / "netbbs.log")
    assert result.entries == [] and result.error is not None


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_a_fifo_in_place_of_the_log_does_not_block(tmp_path):
    path = tmp_path / "netbbs.log"
    os.mkfifo(path)

    result = read_node_log(path)
    assert "not a regular file" in (result.error or "")
    entries, error = NodeLogFollower(path).poll()
    assert entries == [] and "not a regular file" in (error or "")


def test_a_failed_reload_keeps_the_last_read_and_says_why(db, lane, sysop, monkeypatch):
    from netbbs.node_log import NodeLogRead

    _write_log(db, _line("ERROR", "still shown"))
    real = admin_flow.read_node_log
    calls = []

    def _flaky(path, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return real(path, **kwargs)
        return NodeLogRead(path=path, error="could not read netbbs.log: Permission denied")

    monkeypatch.setattr(admin_flow, "read_node_log", _flaky)
    # [F]ollow then any key returns through a reload.
    session = FakeSession(["o", "g", "f", "x", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))

    text = _normalized_visible(_written_text(session))
    after = text[text.rindex("Reload failed"):]
    assert "Permission denied" in after
    assert "still shown" in text[text.rindex("Node log"):]


def test_follow_shows_whole_entries_with_their_traceback(db, lane, sysop, monkeypatch):
    path = node_log_path(db.path)
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(admin_flow, "_DIAGNOSTIC_TAIL_POLL_INTERVAL_SECONDS", 0.05)
    long_message = "listener failed " + "x" * 150 + " THE-END"

    class _SlowKeySession(FakeSession):
        async def read_key(self, echo: bool = True) -> str:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(_line("ERROR", long_message) + "Traceback (most recent call last):\nOSError: boom\n")
            await asyncio.sleep(0.5)
            return "x"

    session = _SlowKeySession()
    asyncio.run(admin_flow._node_log_tail_screen(session, lane, path, floor="WARNING"))

    text = "".join(_visible(_written_text(session)).split())
    assert "THE-END" in text
    assert "Traceback(mostrecentcalllast):" in text and "OSError:boom" in text


# -- review round 4 (PR #739) ----------------------------------------------


def test_a_forged_entry_inside_a_logged_message_stays_part_of_it(tmp_path):
    """A Link peer's error body can carry a newline and a line shaped like an
    entry; the file formatter indents continuation lines, so only the node's
    own entries start at column 0."""
    import logging

    from netbbs.__main__ import _create_log_file_handler

    path = tmp_path / "netbbs.log"
    handler = _create_log_file_handler(path)
    logger = logging.getLogger("netbbs.test_forgery")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.warning("peer said: %s", "oops\n2026-09-27 10:00:00 CRITICAL:netbbs:forged by a peer")
        try:
            raise ValueError("bad\n2026-09-27 10:00:01 CRITICAL:netbbs:forged in a traceback")
        except ValueError:
            logger.exception("request failed")
    finally:
        logger.removeHandler(handler)
        handler.close()

    entries = read_node_log(path).entries
    assert [(e.level, e.message) for e in entries] == [("WARNING", "peer said: oops"), ("ERROR", "request failed")]
    assert any("forged by a peer" in line for line in entries[0].continuation)
    assert any("forged in a traceback" in line for line in entries[1].continuation)


def test_a_rollover_between_the_two_reads_is_not_shown_twice(tmp_path):
    """If `.1` is the very file just read as the active log (the handler
    rolled over in between), its text appears once."""
    path = tmp_path / "netbbs.log"
    path.write_text(_line("ERROR", "only once"), encoding="utf-8")
    try:
        os.link(path, tmp_path / "netbbs.log.1")
    except (OSError, NotImplementedError):
        pytest.skip("hard links are unavailable here")

    result = read_node_log(path)

    assert [e.message for e in result.entries] == ["only once"]


def test_a_burst_before_rotation_says_what_follow_did_not_show(tmp_path, monkeypatch):
    import netbbs.node_log as node_log

    monkeypatch.setattr(node_log, "MAX_FOLLOW_BYTES", 256)
    path = tmp_path / "netbbs.log"
    path.write_text("", encoding="utf-8")
    follower = NodeLogFollower(path)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("".join(_line("ERROR", f"burst {i:03d}") for i in range(50)))
    path.replace(tmp_path / "netbbs.log.1")
    path.write_text(_line("ERROR", "after"), encoding="utf-8")

    entries, notice = follower.poll()

    assert notice is not None and "rotated after a burst" in notice and "netbbs.log.1" in notice
    assert entries and entries[0].message == "burst 000"


def test_an_entry_released_at_its_size_limit_says_it_was_cut(tmp_path, monkeypatch):
    import netbbs.node_log as node_log

    monkeypatch.setattr(node_log, "MAX_HELD_CHARS", 300)
    monkeypatch.setattr(node_log, "MAX_FOLLOW_BYTES", 128)
    path = tmp_path / "netbbs.log"
    path.write_text("", encoding="utf-8")
    follower = NodeLogFollower(path)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(_line("ERROR", "long traceback") + "".join(f"  frame {i:03d}\n" for i in range(100)))
        handle.write(_line("INFO", "next"))

    entries = []
    for _ in range(100):
        entries += follower.poll()[0]

    cut = next(e for e in entries if e.message == "long traceback")
    assert cut.continuation[-1].strip() == node_log.ENTRY_CUT_MARKER.strip()
    assert entries[-1].message == "next"


def test_follow_says_when_the_log_is_gone(tmp_path):
    path = tmp_path / "netbbs.log"
    path.write_text(_line("ERROR", "x"), encoding="utf-8")
    follower = NodeLogFollower(path)
    path.unlink()

    entries, error = follower.poll()

    assert entries == [] and "is gone" in error


def test_follow_retrieves_a_failed_read_even_when_the_body_fails_first(db, lane, monkeypatch):
    """The read finished with an error while a poll was failing for its own
    reason; the finally block must still retrieve it."""
    import gc

    path = node_log_path(db.path)
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(admin_flow, "_DIAGNOSTIC_TAIL_POLL_INTERVAL_SECONDS", 0.02)

    def _slow_failing_poll(self):
        import time

        time.sleep(0.3)
        raise RuntimeError("poll broke")

    monkeypatch.setattr(admin_flow.NodeLogFollower, "poll", _slow_failing_poll)

    class _DropsDuringPoll(FakeSession):
        async def read_key(self, echo: bool = True) -> str:
            await asyncio.sleep(0.1)
            raise ConnectionResetError("caller went away")

    unretrieved = []

    async def scenario():
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: unretrieved.append(context))
        with pytest.raises(RuntimeError, match="poll broke"):
            await admin_flow._node_log_tail_screen(_DropsDuringPoll(), lane, path, floor="WARNING")
        gc.collect()

    asyncio.run(scenario())
    gc.collect()
    assert not [c for c in unretrieved if "never retrieved" in str(c.get("message", ""))]


# -- Claude review (PR #739) -------------------------------------------------


def test_an_unreadable_rotated_file_keeps_the_active_entries(tmp_path, monkeypatch):
    import netbbs.node_log as node_log

    path = tmp_path / "netbbs.log"
    path.write_text(_line("ERROR", "active and fine"), encoding="utf-8")
    (tmp_path / "netbbs.log.1").write_text(_line("ERROR", "older"), encoding="utf-8")
    real = node_log._read_tail

    def _deny_rotated(target, budget):
        if target.name.endswith(".1"):
            raise PermissionError(13, "Permission denied")
        return real(target, budget)

    monkeypatch.setattr(node_log, "_read_tail", _deny_rotated)
    result = read_node_log(path)

    assert result.error is None
    assert [e.message for e in result.entries] == ["active and fine"]
    assert result.truncated


def test_the_empty_message_does_not_name_a_stale_level(db, lane, sysop):
    _write_log(db, _line("WARNING", "only a warning"))

    # Cycle to "errors only", which is empty.
    session = FakeSession(["o", "g", "l", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    text = _normalized_visible(_written_text(session))

    after = text[text.rindex("Showing errors only") - 400:]
    assert "Nothing at this level" in after
    assert "No warnings and errors" not in text


def test_info_entries_are_muted_not_coloured_as_warnings():
    from netbbs.rendering import MUTED_COLOR

    assert admin_flow._node_log_level_color("INFO") == MUTED_COLOR
    assert admin_flow._node_log_level_color("DEBUG") == MUTED_COLOR
    assert admin_flow._node_log_level_color("WARNING") != MUTED_COLOR
    assert admin_flow._node_log_level_color("ERROR") != MUTED_COLOR


def test_entry_numbers_survive_a_refresh_that_moves_the_window():
    """Codex review, PR #739: the picker shows an entry's number as a
    permanent reference, so a refresh that drops old lines must not
    renumber the ones still shown."""
    from netbbs.node_log import StableEntryIds

    ids = StableEntryIds()
    first = ids.apply(parse_log_lines([_line("ERROR", m).rstrip("\n") for m in ("a", "b", "b", "c")]))
    second = ids.apply(parse_log_lines([_line("ERROR", m).rstrip("\n") for m in ("b", "b", "c", "d")]))

    by_first = {(e.message, e.id) for e in first}
    assert [e.id for e in first] == [1, 2, 3, 4]
    assert [(e.message, e.id) for e in second[:3]] == [("b", 2), ("b", 3), ("c", 4)]
    assert second[3].id == 5 and ("d", 5) not in by_first


def test_an_idle_poll_keeps_an_entry_whose_traceback_line_is_half_written(tmp_path):
    """Codex review, PR #739."""
    path = tmp_path / "netbbs.log"
    path.write_text("", encoding="utf-8")
    follower = NodeLogFollower(path)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(_line("ERROR", "upload failed") + "Traceback (most rec")
    assert follower.poll()[0] == []
    assert follower.poll()[0] == [], "idle, but the traceback line is unfinished"

    with open(path, "a", encoding="utf-8") as handle:
        handle.write("ent call last):\n")
    entries = follower.poll()[0] + follower.poll()[0]

    assert [e.message for e in entries] == ["upload failed"]
    assert entries[0].continuation == ("Traceback (most recent call last):",)


def test_the_entry_number_map_forgets_entries_gone_from_the_window():
    """Codex review, PR #739: the map is as bounded as a read."""
    from netbbs.node_log import StableEntryIds

    ids = StableEntryIds()
    for start in range(0, 500, 50):
        ids.apply(parse_log_lines([_line("ERROR", f"m{i}").rstrip("\n") for i in range(start, start + 50)]))

    assert len(ids._known) == 50
    last = ids.apply(parse_log_lines([_line("ERROR", "fresh").rstrip("\n")]))
    assert last[0].id == 501, "numbers are never reused"


def test_an_unreadable_rotated_file_during_follow_is_reported(tmp_path, monkeypatch):
    """Codex review, PR #739: a failed drain must not look like a clean
    switch to the new file."""
    import netbbs.node_log as node_log

    path = tmp_path / "netbbs.log"
    path.write_text(_line("INFO", "before"), encoding="utf-8")
    follower = NodeLogFollower(path)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(_line("ERROR", "written just before rotation"))
    path.replace(tmp_path / "netbbs.log.1")
    path.write_text(_line("ERROR", "new file"), encoding="utf-8")
    real = node_log._open_regular

    def _deny_rotated(target):
        if target.name.endswith(".1"):
            raise PermissionError(13, "Permission denied")
        return real(target)

    monkeypatch.setattr(node_log, "_open_regular", _deny_rotated)
    _, notice = follower.poll()

    assert notice is not None and "rotated" in notice and "Permission denied" in notice
