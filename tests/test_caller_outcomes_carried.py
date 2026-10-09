"""
Issue #1124: what a caller's own action reports is carried into the screen
drawn next, as the SysOp console's outcomes are (#680, #1121), instead of
being written above a screen the next redraw clears.

Every test turns redraw-in-place on and asserts the outcome appears after
the last clear -- on the screen the caller lands on -- and that no
"[Enter] Continue" pause was used to hold it.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date
from pathlib import Path

import pytest

from netbbs.attestation import attest_age, set_display_name
from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.net import profile_flow
from netbbs.net.notices import FAILED_OUTCOMES, announce_line, take_notices
from netbbs.net.redraw_preference import set_redraw_in_place_enabled
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_login_flow_identity_details_screen import FakeSession

_CLEAR = "\x1b[2J"
_SGR = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _after_last_clear(session: FakeSession) -> str:
    text = "".join(session.written)
    assert _CLEAR in text, "the screen never redrew in place"
    return _SGR.sub("", text.rsplit(_CLEAR, 1)[1])


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
def carol(db):
    user = create_user(db, "carol", password="hunter2pw", user_level=10)
    set_redraw_in_place_enabled(db, user, True)
    return user


@pytest.fixture
def sysop(db):
    user = create_user(db, "inkwell", password="hunter2", user_level=SYSOP_LEVEL)
    set_redraw_in_place_enabled(db, user, True)
    return user


# -- Profile -> Name & details ----------------------------------------------------


def test_clearing_a_display_name_is_confirmed_on_the_redrawn_screen(db, lane, carol):
    set_display_name(db, carol, "Caro")
    session = FakeSession(["0", "1", "", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, carol))
    # The redraw after the field drew it; [B]ack leaves without another one.
    shown = _after_last_clear(session)
    assert "Display name cleared." in shown


def test_a_date_that_is_not_one_is_refused_on_the_redrawn_screen(db, lane, carol):
    session = FakeSession(["0", "5", "31.12.1980", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, carol))
    shown = _after_last_clear(session)
    assert "Not a valid date (expected YYYY-MM-DD) -- unchanged." in shown


def test_a_saved_birthdate_is_confirmed_on_the_redrawn_screen(db, lane, carol):
    session = FakeSession(["0", "5", "1980-01-01", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, carol))
    assert "Birthdate updated." in _after_last_clear(session)


# -- Verify -----------------------------------------------------------------------


def test_attesting_an_age_is_confirmed_on_the_redrawn_status_without_a_pause(db, sysop, carol):
    session = FakeSession(["a", "1980-01-01", "b"])
    asyncio.run(profile_flow._verify_user(session, db, sysop, carol))
    shown = _after_last_clear(session)
    assert "Age attested." in shown
    assert "Attested birthdate: 1980-01-01" in shown
    assert "[Enter]" not in _SGR.sub("", "".join(session.written))


def test_revoking_is_confirmed_on_the_redrawn_status_without_a_pause(db, sysop, carol):
    attest_age(db, carol, date(1980, 1, 1), verifier=sysop)
    session = FakeSession(["r", "y", "b"])
    asyncio.run(profile_flow._verify_user(session, db, sysop, carol))
    shown = _after_last_clear(session)
    assert "Verified age revoked." in shown
    assert "[Enter]" not in _SGR.sub("", "".join(session.written))


def test_a_cancelled_attestation_says_so_on_the_redrawn_status(db, sysop, carol):
    session = FakeSession(["n", "", "b"])
    asyncio.run(profile_flow._verify_user(session, db, sysop, carol))
    assert "Cancelled." in _after_last_clear(session)


# -- the shared classification ----------------------------------------------------


class _Plain:
    pass


@pytest.mark.parametrize(
    "line, mark",
    [
        ("Display name updated.", "✓"),
        ("Could not save bio: too long", "✗"),
        ("Not a valid date (expected YYYY-MM-DD).", "✗"),
        ("Cancelled.", None),
    ],
)
def test_a_plain_outcome_reads_as_what_it_reports(line, mark):
    session = _Plain()
    announce_line(session, line)
    [queued] = take_notices(session)
    if mark is None:
        assert "✓" not in queued and "✗" not in queued
    else:
        assert mark in queued
    assert line.split(":")[0] in _SGR.sub("", queued)


def test_a_failure_prefix_list_covers_a_refused_date():
    assert "Not a valid date (expected YYYY-MM-DD).".startswith(FAILED_OUTCOMES)


# -- guard ------------------------------------------------------------------------

# A caller-side outcome written straight to the screen is erased by the
# redraw that follows it. These modules announce outcomes instead; a line
# that is part of a live stream (chat, a door) or of a screen that is not
# redrawn afterwards is listed here with why.
_CALLER_MODULES = sorted(
    p for p in (Path(__file__).resolve().parents[1] / "src" / "netbbs" / "net").glob("*.py")
    if p.name not in {"admin_flow.py", "notices.py"}
)
_OUTCOME_WRITE = re.compile(
    r"write_line\((?:colored\()?f?[\"'](?:\\r\\n)?"
    r"(?:Could not|Couldn't|Cancelled|[A-Z][^\"'\n]*\b(?:updated|cleared|saved|attested|revoked|removed|deleted|renamed)\.)"
)
_ALLOWED = {
    # Chat's /nick reply is a line in the live chat stream, not a screen.
    ("chat_flow.py", "Could not set alias"),
    # Sign-up's failure is written to the login conversation, which scrolls.
    ("login_flow.py", "Could not create account"),
}


def test_no_caller_screen_writes_an_outcome_its_redraw_would_erase():
    found = []
    for path in _CALLER_MODULES:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _OUTCOME_WRITE.search(line) and not any(
                path.name == name and text in line for name, text in _ALLOWED
            ):
                found.append(f"{path.name}:{number}: {line.strip()}")
    assert not found, "announce these instead of writing them:\n" + "\n".join(found)


# -- Who's online -----------------------------------------------------------------


def test_a_message_sent_from_whos_online_is_confirmed_on_the_redrawn_list(tmp_path):
    from tests.test_who_online import FakeSession as WhoSession
    from tests.test_who_online import _hold_registered, _node_controls, _run_main_menu

    database = Database(tmp_path / "node.db")
    alice = create_user(database, "alice", password="hunter2", user_level=10)
    set_redraw_in_place_enabled(database, alice, True)
    create_user(database, "bob", password="hunter2", user_level=10)

    async def scenario():
        node_controls = _node_controls()
        registry = node_controls.session_registry
        other = WhoSession()
        other_task = asyncio.create_task(_hold_registered(registry, other, "bob"))
        await asyncio.sleep(0)
        session = WhoSession(["w", "0", "1", "m", "Hi there!", "b", "l", "y"])
        registry.enter(session)
        registry.mark_authenticated(session, "alice")
        try:
            await _run_main_menu(session, database, alice, node_controls)
        finally:
            registry.leave(session)
            other_task.cancel()
            await asyncio.gather(other_task, return_exceptions=True)
        return "".join(session.written)

    text = asyncio.run(scenario())
    database.close()
    after_send = text.split("Message to bob", 1)[1]
    # The list redraws (clears) and only then shows the outcome above its prompt.
    assert _CLEAR in after_send.split("Message sent.", 1)[0]
    assert "[Enter]" not in _SGR.sub("", text)


# -- doors ------------------------------------------------------------------------


class _Keys:
    """A session that counts the keys it is asked for."""

    def __init__(self):
        self.written: list[str] = []
        self.keys_asked = 0

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_key(self, echo: bool = True) -> str:
        self.keys_asked += 1
        return "x"

    async def read_any_key(self) -> str:
        self.keys_asked += 1
        return "x"


def test_a_doors_exit_keeps_its_pause_so_the_doors_last_screen_can_be_read():
    """Review on #1126: a door's own last screen -- War Dialer's "needs
    40x12" refusal, a final message -- sits above the host's epilogue, which
    both bundled doors reserve rows for (`HOST_EPILOGUE_ROWS`). That is a
    screen to read, not an outcome to carry, so the pause stays."""
    from types import SimpleNamespace

    from netbbs.doors.runtime import DoorRunResult
    from netbbs.net import door_flow

    session = _Keys()
    door = SimpleNamespace(name="War Dialer")
    assert asyncio.run(door_flow._report_door_result(session, door, DoorRunResult(0, 1.0, "normal"))) is True
    shown = _SGR.sub("", "".join(session.written))
    assert "Left War Dialer." in shown
    assert "[Enter]" in shown
    assert session.keys_asked == 1


# -- Find -------------------------------------------------------------------------


def test_a_cancelled_search_is_said_on_the_menu_it_returns_to(db, lane, carol):
    from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
    from netbbs.net import scan_and_find
    from netbbs.net.char_input import InputHistory

    session = FakeSession([""])
    asyncio.run(scan_and_find._find_screen(
        session, db, lane, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), carol,
    ))
    assert any("Search cancelled." in _SGR.sub("", line) for line in take_notices(session))
