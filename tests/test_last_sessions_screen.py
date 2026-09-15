"""
Tests for the two caller-facing screens over the persisted
`netbbs.session_history` table (covered at the library level in
tests/test_session_history.py), and for the profile toggle that governs
what one of them shows.

`[H]istory` (issue #100, narrowed to its own menu description by issue
#592) is the viewer's own call record. `P[r]evious callers` (issue #592)
is the node-wide roll -- the same panel the post-login splash draws, on
its own main-menu hotkey -- and is where every name-visibility rule now
lives, since it is the only one of the two that other callers appear in.

These drive the real `_main_menu` entry point throughout.
"""

from __future__ import annotations

import asyncio
import re

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
from netbbs.net.char_input import InputHistory
from netbbs.net.main_menu import _main_menu
from netbbs.net.profile_flow import _show_previous_callers_screen
from netbbs.rendering import (
    ACCENT_COLOR,
    LABEL_COLOR,
    METADATA_COLOR,
    MUTED_COLOR,
    SUCCESS_COLOR,
    colored,
    display_width,
)
from netbbs.session_history import (
    reconcile_interrupted_sessions,
    set_previous_callers_enabled,
    record_session_start,
    record_session_end,
    session_history_name_visible,
    set_session_history_name_visible,
)
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


def squeezed(text: str) -> str:
    """`text` with runs of spaces collapsed to one.

    Field screens align values into a shared column (#529), so a label and
    its value are separated by as many spaces as that column needs. An
    assertion about *which* value is shown should not also pin the width of
    the column it is shown in.
    """
    return re.sub(r" {2,}", " ", text)


class FakeSession:
    def __init__(self, inputs: list[str] | None = None):
        self._inputs = list(inputs or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.node_display_name = "NetBBS"
        self.node_name_gradient = None
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"
        self.supports_truecolor = False

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
        if not self._inputs:
            raise AssertionError("FakeSession ran out of scripted input (read_line)")
        return self._inputs.pop(0)

    async def read_key(self, echo: bool = True) -> str:
        if not self._inputs:
            raise AssertionError("FakeSession ran out of scripted input (read_key)")
        return self._inputs.pop(0)

    async def read_any_key(self, echo: bool = True) -> str:
        return await self.read_key(echo=echo)


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def _visible(session: FakeSession) -> str:
    return _ANSI_ESCAPE_RE.sub("", _written_text(session))


async def _run_main_menu(session, db, user, *, lane=None, current_history_id=None):
    await _main_menu(
        session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), user,
        lane=lane, current_history_id=current_history_id,
    )


def db_(tmp_path):
    return Database(tmp_path / "node.db")


def test_history_screen_reports_no_sessions_yet(tmp_path):
    database = db_(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    session = FakeSession(["h", " ", "l", "y"])

    asyncio.run(_run_main_menu(session, database, alice))

    assert "You have no recorded sessions yet." in _written_text(session)
    database.close()


def test_history_screen_shows_only_the_viewers_own_sessions(tmp_path):
    """Issue #592, the whole point: `[H]istory` calls itself "Your recent
    sessions" in the menu it is reached from, and used to list the entire
    node -- every other caller's connect and disconnect times under that
    heading. bob's call must not appear on alice's screen at all."""
    database = db_(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    bob = create_user(database, "bob", password="hunter2", user_level=10)
    record_session_start(database, bob)
    record_session_start(database, alice)

    session = FakeSession(["h", " ", "l", "y"])
    asyncio.run(_run_main_menu(session, database, alice))

    text = _written_text(session)
    assert "Your sessions" in _visible(session)
    assert "connected" in text
    assert "bob" not in text
    database.close()


def test_history_screen_shows_only_the_viewers_own_sessions_for_a_sysop(tmp_path):
    """The SysOp's unconditional see-every-name privilege is about the
    node-wide roll, not about whose calls `[H]istory` lists: a SysOp
    asking for their own history gets their own history."""
    database = db_(tmp_path)
    sysop = create_user(database, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    bob = create_user(database, "bob", password="hunter2", user_level=10)
    record_session_start(database, bob)
    record_session_start(database, sysop)

    session = FakeSession(["h", " ", "l", "y"])
    asyncio.run(_run_main_menu(session, database, sysop))

    assert "bob" not in _written_text(session)
    database.close()


def test_history_screen_shows_how_long_each_of_your_calls_lasted(tmp_path):
    """The width freed by dropping the name column -- the same name on
    every row, once the listing is the viewer's own -- buys the duration,
    which the node-wide roll has no room for."""
    database = db_(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    history_id = record_session_start(database, alice)
    database.connection.execute(
        "UPDATE session_history SET connected_at = ?, disconnected_at = ? WHERE id = ?",
        ("2026-09-15T10:00:00.000000Z", "2026-09-15T11:02:30.000000Z", history_id),
    )
    database.connection.commit()

    session = FakeSession(["h", " ", "l", "y"])
    asyncio.run(_run_main_menu(session, database, alice))

    assert "(1h 02m 30s)" in _visible(session)
    database.close()


def test_previous_callers_screen_is_truecolor_fancy_and_excludes_current_session(tmp_path):
    database = db_(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    bob = create_user(database, "bob", password="hunter2", user_level=10)
    prior_id = record_session_start(database, bob)
    record_session_end(database, prior_id)
    current_id = record_session_start(database, alice)
    session = FakeSession([" "])
    session.supports_truecolor = True

    shown = asyncio.run(
        _show_previous_callers_screen(
            session, database, alice, current_history_id=current_id
        )
    )

    output = _written_text(session)
    assert shown is True
    assert "P R E V I O U S" in _visible(session)
    assert "bob" in _visible(session)
    assert "alice" not in _visible(session)
    truecolor_sequences = set(re.findall(r"\x1b\[38;2;\d+;\d+;\d+m", output))
    assert len(truecolor_sequences) >= 10
    assert "Press any key to continue..." in output
    database.close()


def test_previous_callers_screen_shrinks_to_the_available_terminal_rows(tmp_path):
    database = db_(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    bob = create_user(database, "bob", password="hunter2", user_level=10)
    for _ in range(12):
        history_id = record_session_start(database, bob)
        record_session_end(database, history_id)
    current_id = record_session_start(database, alice)
    session = FakeSession([" "])
    session.terminal_height = 12
    session.terminal_width = 40

    asyncio.run(
        _show_previous_callers_screen(
            session, database, alice, current_history_id=current_id
        )
    )

    visible_lines = _visible(session).splitlines()
    # Read each caller row's own number off the start of the line rather
    # than searching the whole line for a digit pair (issue #507). Every
    # row also renders a timestamp, so `"05" in line` matched the `05:39`
    # in `12.09.2026 05:39` and this test failed for whichever hour, day
    # or minute happened to print the number it was guarding against --
    # passing the rest of the day, which is why it went unnoticed.
    row_numbers = [
        match.group(1)
        for line in visible_lines
        if (match := re.match(r"^\W*(\d{2})\s", line))
    ]
    assert row_numbers == ["01", "02", "03", "04"]
    assert len(visible_lines) <= session.terminal_height
    assert all(display_width(line) <= session.terminal_width for line in visible_lines)
    database.close()


def test_previous_callers_screen_honors_name_privacy_and_256_color_fallback(tmp_path):
    database = db_(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    bob = create_user(database, "bob", password="hunter2", user_level=10)
    set_session_history_name_visible(database, bob, False)
    prior_id = record_session_start(database, bob)
    record_session_end(database, prior_id)
    current_id = record_session_start(database, alice)
    session = FakeSession([" "])

    asyncio.run(
        _show_previous_callers_screen(
            session, database, alice, current_history_id=current_id
        )
    )

    assert "(name hidden)" in _visible(session)
    assert "bob" not in _visible(session)
    assert "\x1b[38;2;" not in _written_text(session)
    database.close()


def test_previous_callers_screen_is_a_no_op_when_node_setting_is_off(tmp_path):
    database = db_(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    bob = create_user(database, "bob", password="hunter2", user_level=10)
    record_session_start(database, bob)
    current_id = record_session_start(database, alice)
    set_previous_callers_enabled(database, False)
    session = FakeSession([])

    shown = asyncio.run(
        _show_previous_callers_screen(
            session, database, alice, current_history_id=current_id
        )
    )

    assert shown is False
    assert session.written == []
    database.close()


def test_history_screen_shows_a_recorded_session(tmp_path):
    database = db_(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    record_session_start(database, alice)

    session = FakeSession(["h", " ", "l", "y"])
    asyncio.run(_run_main_menu(session, database, alice))

    assert colored("connected ", fg_color=LABEL_COLOR) in _written_text(session)
    database.close()


def test_previous_callers_menu_screen_shows_another_caller(tmp_path):
    """The node-wide roll kept its meaning; it moved to its own hotkey."""
    database = db_(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    bob = create_user(database, "bob", password="hunter2", user_level=10)
    record_session_start(database, bob)

    session = FakeSession(["r", " ", "l", "y"])
    asyncio.run(_run_main_menu(session, database, alice))

    text = _visible(session)
    assert "P R E V I O U S   C A L L E R S" in text
    assert "bob" in text
    database.close()


def test_previous_callers_menu_entry_is_offered(tmp_path):
    database = db_(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    session = FakeSession(["l", "y"])

    asyncio.run(_run_main_menu(session, database, alice))

    assert "P[r]evious callers" in _visible(session)
    database.close()


def test_previous_callers_menu_screen_says_so_when_empty(tmp_path):
    """Design doc section 3.5: a hotkey that draws nothing reads as a
    broken key. The splash may skip itself silently -- nobody asked for
    it -- but this screen was asked for."""
    database = db_(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    session = FakeSession(["r", " ", "l", "y"])

    asyncio.run(_run_main_menu(session, database, alice))

    assert "Nobody else has called this node yet." in _visible(session)
    database.close()


def test_previous_callers_menu_screen_ignores_the_post_login_toggle(tmp_path):
    """The SysOp setting reads "shown after login" / "hidden after
    login" and governs exactly that. Turning the splash off must not
    also silently remove a main-menu entry -- the node-wide listing was
    unconditionally reachable before issue #592 too, as `[H]istory`."""
    database = db_(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    bob = create_user(database, "bob", password="hunter2", user_level=10)
    record_session_start(database, bob)
    set_previous_callers_enabled(database, False)

    session = FakeSession(["r", " ", "l", "y"])
    asyncio.run(_run_main_menu(session, database, alice))

    assert "bob" in _visible(session)
    database.close()


def test_previous_callers_menu_screen_excludes_the_viewers_own_session(tmp_path):
    """Same exclusion as the splash, for the same reason: the viewer's
    own connection is not a previous caller, and `ONLINE NOW` against
    their own name would spend one of ten scarce rows telling them they
    are connected."""
    database = db_(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    bob = create_user(database, "bob", password="hunter2", user_level=10)
    record_session_end(database, record_session_start(database, bob))
    history_id = record_session_start(database, alice)

    session = FakeSession(["r", " ", "l", "y"])
    asyncio.run(
        _run_main_menu(session, database, alice, current_history_id=history_id)
    )

    text = _visible(session)
    assert "bob" in text
    # bob's call is the only one on the panel, and it is finished --
    # alice's own still-open row is the one that was excluded.
    assert "SIGNED OFF" in text
    assert "ONLINE NOW" not in text
    database.close()


def test_history_screen_shows_still_connected_for_an_open_session(tmp_path):
    database = db_(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    record_session_start(database, alice)

    session = FakeSession(["h", " ", "l", "y"])
    asyncio.run(_run_main_menu(session, database, alice))

    assert "still connected" in _written_text(session)
    assert colored("still connected", fg_color=SUCCESS_COLOR) in _written_text(session)
    database.close()


def test_history_screen_shows_connection_lost_after_startup_reconciliation(tmp_path):
    """Issue #110's own acceptance criterion: a row left open by a
    process that never reached record_session_end (simulated here by a
    bare record_session_start with no matching end call, then reconciled
    exactly the way netbbs.__main__.run() does at its own startup) must
    never be shown as "still connected" -- it cannot possibly still be,
    across a restart -- but also must not be silently folded into a
    normal clean disconnect."""
    database = db_(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    record_session_start(database, alice)  # never ended -- simulates a crash/kill
    reconcile_interrupted_sessions(database)  # what a real restart would run

    session = FakeSession(["h", " ", "l", "y"])
    asyncio.run(_run_main_menu(session, database, alice))

    text = _written_text(session)
    assert "still connected" not in text
    assert "connection lost" in text
    database.close()


def test_previous_callers_screen_hides_name_when_target_opted_out(tmp_path):
    database = db_(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    bob = create_user(database, "bob", password="hunter2", user_level=10)
    set_session_history_name_visible(database, bob, False)
    record_session_start(database, bob)

    session = FakeSession(["r", " ", "l", "y"])
    asyncio.run(_run_main_menu(session, database, alice))

    text = _written_text(session)
    assert "(name hidden)" in text
    assert "bob" not in text
    database.close()


def test_previous_callers_screen_sysop_always_sees_real_names(tmp_path):
    """Issue #100's own acceptance criterion: SysOps see real names
    unconditionally, regardless of the target's own opt-out. It applies
    to the node-wide roll, the only screen where one caller reads
    another's name."""
    database = db_(tmp_path)
    sysop = create_user(database, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    bob = create_user(database, "bob", password="hunter2", user_level=10)
    set_session_history_name_visible(database, bob, False)
    record_session_start(database, bob)

    session = FakeSession(["r", " ", "l", "y"])
    asyncio.run(_run_main_menu(session, database, sysop))

    text = _written_text(session)
    assert "bob" in text
    assert "(name hidden)" not in text
    database.close()


def test_previous_callers_screen_shows_denormalized_label_for_a_deleted_account(tmp_path):
    """bob never opted out, so the persisted `name_visible_fallback`
    (issue #111) this row was recorded with is `True` -- the label is
    shown as-is once the account is gone, same observable result as
    before #111, just now via the persisted fallback rather than an
    unconditional "no account, no opt-out possible" shortcut."""
    from netbbs.auth.users import delete_user

    database = db_(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    sysop = create_user(database, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    bob = create_user(database, "bob", password="hunter2", user_level=10)
    record_session_start(database, bob)
    delete_user(database, bob, deleted_by=sysop)

    session = FakeSession(["r", " ", "l", "y"])
    asyncio.run(_run_main_menu(session, database, alice))

    assert "bob" in _written_text(session)
    database.close()


def test_previous_callers_screen_keeps_a_deleted_accounts_opted_out_name_hidden(tmp_path):
    """Issue #111's own concrete privacy-reversal scenario, reproduced
    end to end through the real screen: bob opts out, a session is
    recorded, the account is deleted -- an ordinary caller must still
    see "(name hidden)", never "bob", in the exact same historical
    entry."""
    from netbbs.auth.users import delete_user

    database = db_(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    sysop = create_user(database, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    bob = create_user(database, "bob", password="hunter2", user_level=10)
    set_session_history_name_visible(database, bob, False)
    record_session_start(database, bob)
    delete_user(database, bob, deleted_by=sysop)

    session = FakeSession(["r", " ", "l", "y"])
    asyncio.run(_run_main_menu(session, database, alice))

    text = _written_text(session)
    assert "(name hidden)" in text
    assert "bob" not in text
    database.close()


def test_previous_callers_screen_sysop_sees_real_name_even_for_a_deleted_opted_out_account(tmp_path):
    """SysOp administrative visibility (issue #100) is unconditional --
    unaffected by both the target's opt-out and the account's own
    deletion (issue #111 must not accidentally hide names from SysOps
    too, only from ordinary callers)."""
    from netbbs.auth.users import delete_user

    database = db_(tmp_path)
    sysop = create_user(database, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    bob = create_user(database, "bob", password="hunter2", user_level=10)
    set_session_history_name_visible(database, bob, False)
    record_session_start(database, bob)
    delete_user(database, bob, deleted_by=sysop)

    session = FakeSession(["r", " ", "l", "y"])
    asyncio.run(_run_main_menu(session, database, sysop))

    text = _written_text(session)
    assert "bob" in text
    assert "(name hidden)" not in text
    database.close()


def test_profile_screen_toggles_session_history_name_visibility(tmp_path):
    database = db_(tmp_path)
    lane = DatabaseLane(database.path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    assert session_history_name_visible(database, alice) is True  # default

    session = FakeSession(["p", "h", "b", "l", "y"])
    asyncio.run(_run_main_menu(session, database, alice, lane=lane))

    assert session_history_name_visible(database, alice) is False
    # live_choice_field (issue #160's cursor-nav follow-up) has no
    # separate "X is now Y" confirmation of its own -- the redrawn
    # field's own "label: value" line is the confirmation.
    assert "Name shown to other callers: no (hidden)" in squeezed(_visible(session))
    lane.close()
    database.close()


def test_profile_shows_color_capability_provenance(tmp_path):
    # Profile pagination follow-up: Color depth lives on DISPLAY,
    # Profile's 3rd page (of 4) once its 14 fields no longer fit
    # unpaginated at a real 80x24 terminal -- not visible on the
    # initial (Identity) render this test used to check directly. This
    # FakeSession has no `read_editor_key` at all (falls back to plain
    # single-character `read_key()`), so it can't script a `PAGE_DOWN`
    # press -- the only way here to reach the Display page is a
    # hotkey. Uses `r` (In-place redraw), a *different* Display-section
    # field, not `c` (Color depth) itself: activating any field's
    # hotkey also marks it cursor-nav-selected on the next redraw
    # (bold/accent-colored, a different string than this test's own
    # assertion expects), so jumping via Color depth's own hotkey would
    # change the very text being checked. `redraw_in_place`'s prompt
    # (`live_choice_field`, same as Color depth's) cycles and persists
    # immediately, no separate sub-screen to back out of first, so `r`
    # both jumps to the Display page *and* redraws showing it, with
    # Color depth itself still rendered unselected.
    database = db_(tmp_path)
    lane = DatabaseLane(database.path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    session = FakeSession(["p", "r", "b", "l", "y"])
    session.truecolor_diagnostic = "SSH client did not forward COLORTERM; using 256-color"

    asyncio.run(_run_main_menu(session, database, alice, lane=lane))

    text = _written_text(session)
    assert colored("  Color depth:", fg_color=LABEL_COLOR) in text
    assert colored("Transport report: ", fg_color=LABEL_COLOR) in squeezed(text)
    assert colored(session.truecolor_diagnostic, fg_color=METADATA_COLOR) in text
    lane.close()
    database.close()


def test_history_narrow_truncation_preserves_complete_ansi_sequences(tmp_path):
    database = db_(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    record_session_start(database, alice)
    session = FakeSession(["h", " ", "l", "y"])
    session.terminal_width = 28

    asyncio.run(_run_main_menu(session, database, alice))

    ansi = re.compile(r"\x1b\[[0-9;]*m")
    history_lines = [chunk for chunk in session.written if "connected" in chunk]
    assert history_lines
    for line in history_lines:
        visible = ansi.sub("", line).rstrip("\n")
        assert len(visible) <= session.terminal_width
        assert "\x1b" not in visible
    database.close()


def test_previous_callers_menu_screen_says_a_too_narrow_terminal_is_why(tmp_path):
    """Every path through the menu screen ends in something drawn."""
    database = db_(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    bob = create_user(database, "bob", password="hunter2", user_level=10)
    record_session_start(database, bob)
    session = FakeSession(["r", " ", "l", "y"])
    session.terminal_width = 3

    asyncio.run(_run_main_menu(session, database, alice))

    assert "too narrow" in _visible(session)
    database.close()


def test_previous_callers_menu_screen_keeps_one_row_on_a_short_terminal(tmp_path):
    """The splash's height budget would leave no rows at all here and
    skip itself; a screen the caller asked for shows what it can."""
    database = db_(tmp_path)
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    bob = create_user(database, "bob", password="hunter2", user_level=10)
    record_session_start(database, bob)
    session = FakeSession(["r", " ", "l", "y"])
    session.terminal_height = 8

    asyncio.run(_run_main_menu(session, database, alice))

    assert "bob" in _visible(session)
    database.close()
