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
    entries, error = follower.poll()
    assert error is None and [e.message for e in entries] == ["new one"]

    with open(path, "a", encoding="utf-8") as handle:
        handle.write("ne\n")
    entries, _ = follower.poll()
    assert [e.message for e in entries] == ["half a line"]


def test_follower_restarts_at_the_top_of_a_rotated_file(tmp_path):
    path = tmp_path / "netbbs.log"
    path.write_text("".join(_line("INFO", f"old {i}") for i in range(20)), encoding="utf-8")
    follower = NodeLogFollower(path)

    path.replace(tmp_path / "netbbs.log.1")
    path.write_text(_line("ERROR", "fresh file"), encoding="utf-8")

    entries, _ = follower.poll()
    assert [e.message for e in entries] == ["fresh file"]


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
