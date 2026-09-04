"""
Tests for the `[V]erify` main-menu screen's per-user page
(`netbbs.net.profile_flow._verify_user`) -- previously untested, and
reshaped by issue #282 from two gating yes/no questions into a status
panel with `[A]ttest age` / `Attest [N]ame` / `[B]ack`.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date

import pytest

from netbbs.attestation import get_attestation, set_birthdate, set_display_name
from netbbs.auth.users import create_user
from netbbs.net import profile_flow
from netbbs.net.session import Session
from netbbs.storage.database import Database


class FakeSession(Session):
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


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _visible(session: FakeSession) -> str:
    return _ANSI_ESCAPE_RE.sub("", "".join(session.written))


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=255)


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


def test_back_shows_status_and_asks_nothing(db, sysop, alice):
    set_birthdate(db, alice, date(1990, 5, 1))
    set_display_name(db, alice, "Alice W")
    session = FakeSession(["b"])
    asyncio.run(profile_flow._verify_user(session, db, sysop, alice))
    text = _visible(session)
    assert "Self-reported birthdate: 1990-05-01" in text
    assert "Self-reported display name: Alice W" in text
    assert "Attested birthdate: (not attested)" in text
    assert "Attested real name: (not attested)" in text
    assert "Attest a birthdate?" not in text
    assert "Attest a real name?" not in text
    assert "[A]ttest age" in text and "Attest [N]ame" in text and "[B]ack" in text
    assert "Verifying alice" in text
    assert get_attestation(db, alice, "age") is None
    assert get_attestation(db, alice, "name") is None


def test_attest_age_records_and_redraws(db, sysop, alice):
    session = FakeSession(["a", "1990-05-01", "x", "b"])
    asyncio.run(profile_flow._verify_user(session, db, sysop, alice))
    text = _visible(session)
    assert "Age attested." in text
    assert "Attested birthdate: 1990-05-01" in text
    assert get_attestation(db, alice, "age").attested_value == "1990-05-01"


def test_attest_name_records_and_redraws(db, sysop, alice):
    session = FakeSession(["n", "Alice Wonderland", "x", "b"])
    asyncio.run(profile_flow._verify_user(session, db, sysop, alice))
    text = _visible(session)
    assert "Real name attested." in text
    assert "Attested real name: Alice Wonderland" in text
    assert get_attestation(db, alice, "name").attested_value == "Alice Wonderland"


def test_blank_value_cancels_the_action(db, sysop, alice):
    session = FakeSession(["a", "", "x", "n", "", "x", "b"])
    asyncio.run(profile_flow._verify_user(session, db, sysop, alice))
    assert _visible(session).count("Cancelled.") == 2
    assert get_attestation(db, alice, "age") is None
    assert get_attestation(db, alice, "name") is None


def test_invalid_date_is_reported_and_nothing_recorded(db, sysop, alice):
    session = FakeSession(["a", "not-a-date", "x", "b"])
    asyncio.run(profile_flow._verify_user(session, db, sysop, alice))
    assert "Could not attest age" in _visible(session)
    assert get_attestation(db, alice, "age") is None
