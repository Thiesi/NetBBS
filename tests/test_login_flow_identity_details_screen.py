"""
Tests for the "Name & details" screen
(`netbbs.net.profile_flow._identity_details_screen`, reached from
`_edit_profile`'s own `[N]ame & details` option) -- previously
untested; converted onto `edit_resource_draft` alongside the profile
screen itself (issue #160's cursor-nav follow-up).
"""

from __future__ import annotations

import asyncio
import re
from datetime import date

import pytest

from netbbs.attestation import (
    attest_age,
    set_display_name,
    set_display_name_visible,
    set_location,
    set_location_visible,
    set_birthdate,
    set_birthdate_visible,
    set_attestation_link_visible,
    attest_name,
    compute_age,
    get_birthdate,
    get_display_name,
    get_location,
    is_birthdate_visible,
    is_display_name_visible,
    is_location_visible,
    is_verified_badge_visible,
)
from netbbs.attestation import get_attestation
from netbbs.auth.users import create_user
from netbbs.net import profile_flow
from netbbs.net.char_input import HELP_KEY
from netbbs.net.session import Session
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


class FakeSession(Session):
    """One ordered input queue serves both `read_key()` and `read_line()`
    -- same shape tests/test_login_flow_sort_preferences_screen.py's own
    FakeSession already established; `read_editor_key` isn't implemented,
    so `edit_resource_draft`'s cursor navigation falls back to plain
    `read_key()`, exactly like every hotkey-only test double."""

    def __init__(self, inputs: list[str] | None = None):
        self._inputs = list(inputs or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.node_display_name = "NetBBS"
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_key(self, echo: bool = True) -> str:
        return self._inputs.pop(0)

    async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
        return self._inputs.pop(0)

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False):
        raise NotImplementedError

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _visible(session: FakeSession) -> str:
    return _ANSI_ESCAPE_RE.sub("", _written_text(session))


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


def test_shows_current_state_with_nothing_set(db, lane, alice):
    session = FakeSession(["b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    text = _visible(session)
    assert "Display name: (not set)" in text
    assert "Display name visibility: private" in text
    assert "Location: (not set)" in text
    assert "Location visibility: private" in text
    assert "Birthdate: (not set)" in text
    assert "Age visibility: private" in text
    assert "Verified: (none)" in text
    assert "Share verified age over Link: (not verified)" in text
    assert "Share verified name over Link: (not verified)" in text


def test_ctrl_h_shows_real_help_text_for_every_field(db, lane, alice):
    # Dogfood feature request: this screen's five fields previously had
    # no help= authored at all, so Ctrl-H was a discoverable dead end
    # ("No help is available for ... yet" for every one of them).
    session = FakeSession([HELP_KEY, " ", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    text = _visible(session)
    assert "No help is available" not in text
    assert "self-reported and unverified" in text.lower()
    assert "minimum age to post or join" in text
    assert "trust/vouch policy" in text


def test_display_name_edit_sets_only_the_value(db, lane, alice):
    session = FakeSession(["d", "Alice W", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    assert get_display_name(db, alice) == "Alice W"
    assert is_display_name_visible(db, alice) is False
    assert "Display name: Alice W" in _visible(session)


def test_display_name_visibility_is_its_own_toggle(db, lane, alice):
    session = FakeSession(["i", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    assert is_display_name_visible(db, alice) is True
    assert "Display name visibility: public" in _visible(session)


def test_blank_value_prompt_leaves_visibility_untouched(db, lane, alice):
    # Issue #282 regression: the value prompt used to be followed by an
    # unconditional "Show it publicly? [y/N]" whose answer was always
    # written, so pressing Enter twice just to look at a field silently
    # set it private. Now a blank keeps the value and touches nothing else.
    set_display_name(db, alice, "Alice W")
    set_display_name_visible(db, alice, True)
    set_location(db, alice, "Retro City")
    set_location_visible(db, alice, True)
    set_birthdate(db, alice, date(2000, 1, 1))
    set_birthdate_visible(db, alice, True)
    session = FakeSession(["d", "", "l", "", "a", "", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    assert get_display_name(db, alice) == "Alice W"
    assert is_display_name_visible(db, alice) is True
    assert get_location(db, alice) == "Retro City"
    assert is_location_visible(db, alice) is True
    assert get_birthdate(db, alice) == date(2000, 1, 1)
    assert is_birthdate_visible(db, alice) is True
    assert "Show it publicly" not in _written_text(session)


def test_location_edit_and_visibility_toggle(db, lane, alice):
    session = FakeSession(["l", "Retro City", "o", "o", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    assert get_location(db, alice) == "Retro City"
    # Two presses of the toggle return to the starting state.
    assert is_location_visible(db, alice) is False
    text = _visible(session)
    assert "Location: Retro City" in text
    assert "Location visibility: public" in text
    assert "Location visibility: private" in text


def test_birthdate_edit_sets_value_and_age(db, lane, alice):
    session = FakeSession(["a", "2000-01-01", "g", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    assert get_birthdate(db, alice) == date(2000, 1, 1)
    assert is_birthdate_visible(db, alice) is True
    text = _visible(session)
    assert f"(age {compute_age(date(2000, 1, 1))})" in text
    assert "Age visibility: public" in text


def test_birthdate_rejects_an_invalid_date_format(db, lane, alice):
    session = FakeSession(["a", "not-a-date", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    assert get_birthdate(db, alice) is None
    assert "Not a valid date" in _written_text(session)


def test_verified_badge_visibility_toggles(db, lane, alice):
    assert is_verified_badge_visible(db, alice) is False  # default
    session = FakeSession(["v", "v", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    # Two presses of a bool toggle return to the starting state.
    assert is_verified_badge_visible(db, alice) is False


def test_verified_summary_shows_attested_attributes(db, lane, alice):
    verifier = create_user(db, "sysop", password="hunter2", user_level=255)
    attest_age(db, alice, date(1990, 5, 1), verifier=verifier)
    session = FakeSession(["b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    text = _visible(session)
    assert "Verified: age" in text
    assert "Share verified age over Link: off" in text
    assert "Share verified name over Link: (not verified)" in text


def test_remote_sharing_rejects_an_attribute_with_no_attestation(db, lane, alice):
    session = FakeSession(["s", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    assert "No age attestation exists" in _written_text(session)
    assert get_attestation(db, alice, "age") is None


def test_remote_sharing_toggles_both_ways_without_a_question(db, lane, alice):
    verifier = create_user(db, "sysop", password="hunter2", user_level=255)
    attest_name(db, alice, "Alice Wonderland", verifier=verifier)
    session = FakeSession(["h", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    assert get_attestation(db, alice, "name").link_visible is True
    assert "Share verified name over Link: on" in _visible(session)
    assert "Allow this verified" not in _written_text(session)

    # Turning it off is the same single keystroke -- previously the
    # sub-screen toggled off silently but asked a yes/no before turning
    # on (issue #282).
    session = FakeSession(["h", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    assert get_attestation(db, alice, "name").link_visible is False
    assert "Share verified name over Link: off" in _visible(session)


def test_remote_sharing_refreshes_instead_of_toggling_a_changed_attestation(db, lane, alice):
    # Codex review (PR #284): the screen shows sharing "on", then a SysOp
    # re-attests while it is open -- which clears link_visible (worklog
    # invariant). The caller's press was meant to turn the displayed
    # "on" off; re-reading and blindly negating would instead *enable*
    # sharing of the replacement attestation they have never seen. The
    # toggle must refresh and report, and only act on a second press.
    verifier = create_user(db, "sysop", password="hunter2", user_level=255)
    attest_age(db, alice, date(1990, 5, 1), verifier=verifier)
    set_attestation_link_visible(db, alice, "age", True)

    class ReattestingSession(FakeSession):
        """Re-attests between the first draw and the first keypress."""

        def __init__(self, inputs):
            super().__init__(inputs)
            self.reattested = False

        async def read_key(self, echo: bool = True) -> str:
            if not self.reattested:
                self.reattested = True
                attest_age(db, alice, date(1991, 6, 2), verifier=verifier)
            return await super().read_key(echo)

    session = ReattestingSession(["s", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    text = _visible(session)
    assert "Share verified age over Link: on" in text  # what the caller saw first
    assert "changed since this screen was drawn" in text
    assert get_attestation(db, alice, "age").link_visible is False
    assert get_attestation(db, alice, "age").attested_value == "1991-06-02"
    # The redraw now shows the real state; a second press acts on it.
    session = FakeSession(["s", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    assert get_attestation(db, alice, "age").link_visible is True


def test_remote_sharing_reports_an_attestation_removed_since_the_draw(db, lane, alice):
    verifier = create_user(db, "sysop", password="hunter2", user_level=255)
    attest_name(db, alice, "Alice Wonderland", verifier=verifier)

    class RevokingSession(FakeSession):
        def __init__(self, inputs):
            super().__init__(inputs)
            self.revoked = False

        async def read_key(self, echo: bool = True) -> str:
            if not self.revoked:
                self.revoked = True
                # No removal API exists yet (a re-attestation replaces in
                # place); model a SysOp-side deletion at the storage level.
                db.connection.execute(
                    "DELETE FROM user_attestations WHERE subject_user_id = ? AND attribute = 'name'",
                    (alice.id,),
                )
                db.connection.commit()
            return await super().read_key(echo)

    session = RevokingSession(["h", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, alice))
    text = _visible(session)
    assert "was removed since this screen was drawn" in text
    assert "Share verified name over Link: (not verified)" in text
    assert get_attestation(db, alice, "name") is None
