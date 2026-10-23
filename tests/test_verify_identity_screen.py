"""
Tests for the `[V]erify` main-menu screen's per-user page
(`netbbs.net.profile_flow._verify_user`): a resource's own screen in the
draft editor, `01` the verified birthdate and `02` the verified real
name, edited where they stand and recorded only by `[S]ave` after one
confirmation that says what it replaces.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, timedelta

import pytest

from netbbs.attestation import (
    AttestationChanged,
    AttestationError,
    attest_age,
    attest_identity,
    attest_name,
    get_attestation,
    revoke_attestation,
    set_attestation_link_visible,
    set_birthdate,
    set_display_name,
)
from netbbs.auth.users import create_user
from netbbs.moderation.log import list_actions_for_target_user
from netbbs.net import profile_flow
from netbbs.net.char_input import InputCancelled
from netbbs.net.session import Session
from netbbs.storage.database import Database

ESC = object()


class FakeSession(Session):
    def __init__(self, inputs: list | None = None):
        self._inputs = list(inputs or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.node_display_name = "NetBBS"
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"
        self.initials: list[str] = []

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_key(self, echo: bool = True) -> str:
        return self._inputs.pop(0)

    async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
        self.initials.append(kwargs.get("initial", ""))
        value = self._inputs.pop(0)
        if value is ESC:
            raise InputCancelled()
        return value

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False):
        raise NotImplementedError

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _visible(session: FakeSession) -> str:
    return _ANSI_ESCAPE_RE.sub("", "".join(session.written))


def _squeezed(session: FakeSession) -> str:
    return " ".join(_visible(session).split())


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


def _run(db, verifier, subject, inputs: list) -> FakeSession:
    session = FakeSession(inputs)
    asyncio.run(profile_flow._verify_user(session, db, verifier, subject))
    assert session._inputs == []
    return session


def test_back_shows_status_and_asks_nothing(db, sysop, alice):
    set_birthdate(db, alice, date(1990, 5, 1))
    set_display_name(db, alice, "Alice W")
    text = _squeezed(_run(db, sysop, alice, ["b"]))
    assert "Verifying alice" in text
    assert "They say: born 1990-05-01 (age" in text and "display name Alice W" in text
    assert "01 Verified birthdate: (not verified)" in text
    assert "02 Verified real name: (not verified)" in text
    assert "[B]ack" in text
    # Nothing on record: nothing to revoke, and the old keys are gone.
    assert "[R]evoke" not in text and "[A]ge" not in text and "[N]ame" not in text
    assert get_attestation(db, alice, "age") is None
    assert get_attestation(db, alice, "name") is None


def test_both_values_are_drafted_then_recorded_together_on_save(db, sysop, alice):
    session = _run(db, sysop, alice, ["0", "1", "1990-05-01", "0", "2", "Alice Wonderland", "s", "y", "b"])
    text = _squeezed(session)
    assert "Record for 'alice': born 1990-05-01 (age" in text and "and real name Alice Wonderland." in text
    assert "Recorded the verified age and verified real name." in text
    assert get_attestation(db, alice, "age").attested_value == "1990-05-01"
    assert get_attestation(db, alice, "name").attested_value == "Alice Wonderland"
    actions = [a.action for a in list_actions_for_target_user(db, alice.id)]
    assert sorted(actions) == ["attest_age", "attest_name"]


def test_nothing_is_stored_before_save(db, sysop, alice):
    # Draft both, then leave: [B]ack asks before discarding, and nothing is stored.
    session = _run(db, sysop, alice, ["0", "1", "1990-05-01", "0", "2", "Alice W", "b", "y"])
    assert "Discard unsaved changes?" in _visible(session)
    assert get_attestation(db, alice, "age") is None
    assert get_attestation(db, alice, "name") is None


def test_saying_no_keeps_the_draft_and_records_nothing(db, sysop, alice):
    session = _run(db, sysop, alice, ["0", "2", "Alice W", "s", "n", "s", "y", "b"])
    text = _squeezed(session)
    assert "Cancelled. Nothing recorded; your changes are still here." in text
    assert "Could not save" not in text
    # The second [S]ave recorded the same draft.
    assert get_attestation(db, alice, "name").attested_value == "Alice W"


def test_a_field_opens_on_the_value_on_record(db, sysop, alice):
    attest_name(db, alice, "Alice W", verifier=sysop)
    session = _run(db, sysop, alice, ["0", "2", ESC, "b"])
    assert session.initials == ["Alice W"]
    assert get_attestation(db, alice, "name").attested_value == "Alice W"


def test_emptying_a_stored_value_is_refused_and_points_at_revoke(db, sysop, alice):
    attest_age(db, alice, date(1990, 5, 1), verifier=sysop)
    text = _squeezed(_run(db, sysop, alice, ["0", "1", "", "b"]))
    assert "Can't empty a verified age: use [R]evoke to withdraw it. Unchanged." in text
    assert get_attestation(db, alice, "age") is not None


def test_an_invalid_date_is_refused_in_the_field(db, sysop, alice):
    text = _squeezed(_run(db, sysop, alice, ["0", "1", "not-a-date", "b"]))
    assert "Not a valid date (expected YYYY-MM-DD) -- unchanged." in text
    assert get_attestation(db, alice, "age") is None


def test_a_future_date_is_refused_in_the_field(db, sysop, alice):
    future = (date.today() + timedelta(days=400)).isoformat()
    text = _squeezed(_run(db, sysop, alice, ["0", "1", future, "b"]))
    assert f"Can't verify {future}: birthdate cannot be in the future -- unchanged." in text
    # Refused before the draft: [B]ack leaves without a discard question.
    assert "Discard unsaved changes?" not in text


def test_the_age_is_shown_beside_the_date(db, sysop, alice):
    attest_age(db, alice, date(1990, 5, 1), verifier=sysop)
    text = _squeezed(_run(db, sysop, alice, ["b"]))
    assert re.search(r"01 Verified birthdate: 1990-05-01 \(age \d+\)", text)
    assert "Age verified by you on" in text


def test_the_confirmation_names_what_it_replaces_and_the_link_switch_off(db, sysop, alice):
    attest_name(db, alice, "Alice W", verifier=sysop)
    set_attestation_link_visible(db, alice, "name", True)
    text = _squeezed(_run(db, sysop, alice, ["0", "2", "Alice Wonderland", "s", "n", "b", "y"]))
    assert "Real name verified by you on" in text and "shared over Link" in text
    assert "It replaces the verified real name Alice W, by you on" in text
    assert "and switches off alice's sharing of it over Link until they share the new one." in text
    assert get_attestation(db, alice, "name").link_visible


def test_a_verification_changed_meanwhile_is_not_replaced(db, sysop, alice, monkeypatch):
    other = create_user(db, "other", password="hunter2", user_level=255)
    real = profile_flow.prompt_yes_no

    async def answer_after_someone_else(session, prompt, *, default):
        if prompt == "Record it?":
            attest_name(db, alice, "Alice Other", verifier=other)
        return await real(session, prompt, default=default)

    monkeypatch.setattr(profile_flow, "prompt_yes_no", answer_after_someone_else)
    text = _squeezed(_run(db, sysop, alice, ["0", "2", "Alice W", "s", "y", "b", "y"]))
    assert "Could not save: the verified real name of 'alice' changed since this screen was drawn" in text
    # The screen now shows what is stored, by whom.
    assert "Real name verified by other on" in text
    assert get_attestation(db, alice, "name").attested_value == "Alice Other"


def test_revoke_is_offered_once_something_is_verified(db, sysop, alice):
    attest_name(db, alice, "Alice W", verifier=sysop)
    text = _squeezed(_run(db, sysop, alice, ["r", "y", "b"]))
    assert "Verified real name revoked." in text
    assert get_attestation(db, alice, "name") is None


def test_revoke_is_not_offered_while_the_draft_differs(db, sysop, alice):
    attest_name(db, alice, "Alice W", verifier=sysop)
    session = _run(db, sysop, alice, ["0", "2", "Alice X", "r", "b", "y"])
    assert "Revoke the verified" not in _visible(session)
    assert get_attestation(db, alice, "name").attested_value == "Alice W"


# -- attest_identity ----------------------------------------------------------------


def test_attest_identity_is_all_or_nothing(db, sysop, alice):
    with pytest.raises(AttestationError):
        attest_identity(
            db, alice, verifier=sysop, birthdate=date(1990, 5, 1), real_name="  ",
            expected={"age": None, "name": None},
        )
    assert get_attestation(db, alice, "age") is None
    attest_name(db, alice, "Alice Other", verifier=sysop)
    with pytest.raises(AttestationChanged):
        attest_identity(
            db, alice, verifier=sysop, birthdate=date(1990, 5, 1), real_name="Alice W",
            expected={"age": None, "name": None},
        )
    assert get_attestation(db, alice, "age") is None
    assert get_attestation(db, alice, "name").attested_value == "Alice Other"


def test_attest_identity_refuses_a_revoked_one_it_was_shown(db, sysop, alice):
    shown = attest_age(db, alice, date(1990, 5, 1), verifier=sysop)
    revoke_attestation(db, alice, "age", actor=sysop)
    with pytest.raises(AttestationChanged):
        attest_identity(db, alice, verifier=sysop, birthdate=date(1991, 5, 1), expected={"age": shown})
    assert get_attestation(db, alice, "age") is None


def test_attest_identity_resets_link_sharing_and_records_history(db, sysop, alice):
    shown = attest_name(db, alice, "Alice W", verifier=sysop)
    shown = set_attestation_link_visible(db, alice, "name", True)
    stored = attest_identity(db, alice, verifier=sysop, real_name="Alice Wonderland", expected={"name": shown})
    assert stored["name"].attested_value == "Alice Wonderland" and not stored["name"].link_visible
    assert [a.action for a in list_actions_for_target_user(db, alice.id)].count("attest_name") == 2
