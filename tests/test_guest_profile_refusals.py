"""A guest session cannot change what other callers see of the shared guest
account (issue #1073).

Guest login (issue #531) signs every anonymous caller in to one account.
The password and key screens already refused such a session; the rest of
Profile did not, so any caller could rewrite the bio, signature, display
name, location or birthdate every later guest and every other caller sees.
Each test presses one Profile entry as a session that signed in without a
credential and checks that nothing about the account changed and that the
screen said why.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.attestation import get_birthdate, get_display_name, get_location
from netbbs.auth.users import create_user, list_ssh_keys
from netbbs.directory import get_bio, is_bio_visible, set_bio
from netbbs.mail import list_mail_blocks
from netbbs.net import profile_flow
from netbbs.signature import get_signature
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_REASON = "signed in without a password"


class FakeSession:
    def __init__(self, keys=None, lines=None, *, guest: bool):
        self._keys = iter(keys or [])
        self._lines = iter(lines or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.node_display_name = "NetBBS"
        self.node_name_gradient = None
        self.peer_address = "203.0.113.5"
        self.supports_truecolor = False
        if guest:
            self.authenticated_without_credential = True

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_key(self, echo: bool = True) -> str:
        key = next(self._keys, None)
        if key is None:
            raise AssertionError("read_key() called with no more scripted keys")
        return key

    async def read_line(self, echo: bool = True, **kwargs) -> str:
        # Raises rather than returning "" forever: an editor that opened
        # when it should have refused would otherwise loop on blank lines.
        line = next(self._lines, None)
        if line is None:
            raise AssertionError("read_line() called with no more scripted lines")
        return line

    @property
    def visible(self) -> str:
        return re.sub(r"\s+", " ", _ANSI.sub("", "".join(self.written)))


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
def guest(db):
    account = create_user(db, "guest", password="hunter2", user_level=1)
    set_bio(db, account, "The guest account.")
    create_user(db, "mallory", password="hunter2", user_level=10)
    return account


def _account_state(db: Database, user) -> dict:
    """Everything a Profile entry can write about `user`."""
    return {
        "preferences": sorted(
            tuple(row) for row in db.connection.execute(
                "SELECT key, value FROM user_preferences WHERE user_id = ?", (user.id,)
            )
        ),
        "sort": [tuple(row) for row in db.connection.execute(
            "SELECT resource_kind, sort_mode FROM user_sort_preferences WHERE user_id = ?", (user.id,)
        )],
        "blocks": [block.blocked_user_id for block in list_mail_blocks(db, user)],
        "keys": len(list_ssh_keys(db, user)),
    }


# Profile hotkey, then what the entry would type if it opened.
_SHARED_ENTRIES = {
    "bio": ("e", ["Defaced.", ""]),
    "bio visibility": ("v", []),
    "signature": ("g", ["Defaced.", ""]),
    "name and details": ("n", []),
    "direct messages": ("m", []),
    "blocked people": ("o", []),
    "read receipts": ("x", []),
    "MRC private messages": ("p", []),
    "MRC last seen": ("w", []),
    "name in the callers roll": ("h", []),
    "MRC nick color": ("y", []),
    "SSH keys": ("k", []),
    "password": ("a", []),
}


@pytest.mark.parametrize("entry", sorted(_SHARED_ENTRIES))
def test_a_guest_session_cannot_change_a_shared_profile_entry(db, lane, guest, entry):
    hotkey, lines = _SHARED_ENTRIES[entry]
    before = _account_state(db, guest)
    session = FakeSession(keys=[hotkey, "b"], lines=lines, guest=True)

    asyncio.run(profile_flow._edit_profile(session, lane, guest))

    assert _account_state(db, guest) == before
    assert get_bio(db, guest) == "The guest account."
    assert _REASON in session.visible
    assert "Every guest signs in to this same account." in session.visible


def test_the_profile_says_what_a_guest_can_and_cannot_change(db, lane, guest):
    session = FakeSession(keys=["b"], guest=True)

    asyncio.run(profile_flow._edit_profile(session, lane, guest))

    assert "display settings you change last for this call only" in session.visible


def test_an_ordinary_session_sees_no_guest_note_and_edits_the_bio(db, lane, guest):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(keys=["e", "b"], lines=["Hello.", "", "", ""], guest=False)

    asyncio.run(profile_flow._edit_profile(session, lane, alice))

    assert get_bio(db, alice) == "Hello."
    assert "last for this call" not in session.visible
    assert _REASON not in session.visible


def test_an_ordinary_session_still_toggles_bio_visibility(db, lane, guest):
    session = FakeSession(keys=["v", "b"], guest=False)

    asyncio.run(profile_flow._edit_profile(session, lane, guest))

    assert is_bio_visible(db, guest) is True


# -- the screens refuse themselves, whoever opens them ----------------------


@pytest.mark.parametrize(
    "lines",
    [["Mallory"], ["Nowhere"], ["2001-01-01"]],
    ids=["display name", "location", "birthdate"],
)
def test_the_name_and_details_screen_refuses_a_guest_session(db, lane, guest, lines):
    hotkey = {"Mallory": "d", "Nowhere": "l", "2001-01-01": "a"}[lines[0]]
    session = FakeSession(keys=[hotkey, "b"], lines=lines, guest=True)
    before = _account_state(db, guest)

    asyncio.run(profile_flow._identity_details_screen(session, lane, guest))

    assert _account_state(db, guest) == before
    assert get_display_name(db, guest) is None
    assert get_location(db, guest) is None
    assert get_birthdate(db, guest) is None


def test_the_bio_editor_refuses_a_guest_session(db, lane, guest):
    session = FakeSession(lines=["n", "Defaced.", ""], guest=True)

    asyncio.run(profile_flow._edit_bio(session, lane, guest))

    assert get_bio(db, guest) == "The guest account."


def test_the_signature_editor_refuses_a_guest_session(db, lane, guest):
    session = FakeSession(lines=["Defaced.", ""], guest=True)

    asyncio.run(profile_flow._edit_signature(session, lane, guest))

    assert get_signature(db, guest) is None
