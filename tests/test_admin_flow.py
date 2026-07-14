"""
Tests for the shared SysOp admin menu (design doc -- SysOp foundation
round), `netbbs.net.admin_flow.admin_menu` -- the single implementation
both the in-BBS [A]dmin option and the standalone `python -m
netbbs.admin` CLI tool call. Driven with a scripted `FakeSession`
(single ordered input queue serving both `read_key`/`read_line`, same
as a real terminal has no concept of "key mode" vs "line mode" beyond
what the caller asks for).
"""

from __future__ import annotations

import asyncio
import base64

import nacl.signing
import pytest

from netbbs.auth.users import SYSOP_LEVEL, count_sysops, create_user, list_users
from netbbs.net.admin_flow import admin_menu
from netbbs.net.session import Session
from netbbs.storage.database import Database


class FakeSession(Session):
    def __init__(self, inputs: list[str] | None = None):
        self._inputs = list(inputs or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.peer_address = None

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def read_line(self, echo: bool = True, history=None, completer=None) -> str:
        if not self._inputs:
            raise AssertionError("FakeSession ran out of scripted input (read_line)")
        return self._inputs.pop(0)

    async def read_key(self, echo: bool = True) -> str:
        if not self._inputs:
            raise AssertionError("FakeSession ran out of scripted input (read_key)")
        return self._inputs.pop(0)

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


def _openssh_line(verify_key: nacl.signing.VerifyKey) -> str:
    def encode_string(b: bytes) -> bytes:
        return len(b).to_bytes(4, "big") + b

    blob = encode_string(b"ssh-ed25519") + encode_string(bytes(verify_key))
    return "ssh-ed25519 " + base64.b64encode(blob).decode() + " test@comment"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)


def _run(session, db, user):
    asyncio.run(admin_menu(session, db, user))


# -- create user ----------------------------------------------------------


def test_create_user_with_password_only(db, sysop):
    session = FakeSession(["c", "alice", "y", "hunter2", "hunter2", "n", "10", "b"])
    _run(session, db, sysop)
    created = next(u for u in list_users(db) if u.username == "alice")
    assert created.user_level == 10
    assert "Created 'alice'" in _written_text(session)


def test_create_user_with_pubkey_only_raw_base64(db, sysop):
    verify_key = nacl.signing.SigningKey.generate().verify_key
    raw_b64 = base64.b64encode(bytes(verify_key)).decode()
    session = FakeSession(["c", "bob", "n", "y", raw_b64, "0", "b"])
    _run(session, db, sysop)
    created = next(u for u in list_users(db) if u.username == "bob")
    assert created.fingerprint is not None


def test_create_user_with_pubkey_only_openssh_line(db, sysop):
    verify_key = nacl.signing.SigningKey.generate().verify_key
    session = FakeSession(["c", "carol", "n", "y", _openssh_line(verify_key), "0", "b"])
    _run(session, db, sysop)
    created = next(u for u in list_users(db) if u.username == "carol")
    assert created.fingerprint is not None


def test_create_user_with_both_password_and_pubkey(db, sysop):
    verify_key = nacl.signing.SigningKey.generate().verify_key
    raw_b64 = base64.b64encode(bytes(verify_key)).decode()
    session = FakeSession(["c", "dave", "y", "hunter2", "hunter2", "y", raw_b64, "0", "b"])
    _run(session, db, sysop)
    created = next(u for u in list_users(db) if u.username == "dave")
    assert created.fingerprint is not None


def test_create_user_with_neither_is_cancelled(db, sysop):
    session = FakeSession(["c", "eve", "n", "n", "b"])
    _run(session, db, sysop)
    assert not any(u.username == "eve" for u in list_users(db))
    assert "needs a password" in _written_text(session)


def test_create_user_with_blank_username_is_cancelled(db, sysop):
    session = FakeSession(["c", "", "b"])
    _run(session, db, sysop)
    assert "cannot be blank" in _written_text(session)


# -- list / detail ---------------------------------------------------------


def test_list_users_and_select_shows_detail(db, sysop):
    session = FakeSession(["l", "0", "1", "b"])
    _run(session, db, sysop)
    assert "sysop" in _written_text(session)
    assert "Level: 255" in _written_text(session)


# -- promote/demote ---------------------------------------------------------


def test_promote_demote_changes_level(db, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    # alice sorts before sysop alphabetically -- item 01.
    session = FakeSession(["p", "0", "1", "20", "b"])
    _run(session, db, sysop)
    updated = next(u for u in list_users(db) if u.username == "alice")
    assert updated.user_level == 20


def test_promote_demote_shows_lockout_guard_message(db, sysop):
    # sysop is the only user, and the only active SysOp -- demoting
    # them must be refused, with the message shown on screen, not a
    # crash.
    session = FakeSession(["p", "0", "1", "10", "b"])
    _run(session, db, sysop)
    assert "only active SysOp-level account" in _written_text(session)
    assert count_sysops(db) == 1


# -- enable/disable ---------------------------------------------------------


def test_disable_enable_toggles_status(db, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["e", "0", "1", "y", "b"])
    _run(session, db, sysop)
    updated = next(u for u in list_users(db) if u.username == "alice")
    assert updated.disabled_at is not None


def test_disable_declining_confirmation_leaves_account_active(db, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["e", "0", "1", "n", "b"])
    _run(session, db, sysop)
    updated = next(u for u in list_users(db) if u.username == "alice")
    assert updated.disabled_at is None


def test_disable_shows_lockout_guard_message(db, sysop):
    session = FakeSession(["e", "0", "1", "y", "b"])
    _run(session, db, sysop)
    assert "only active SysOp-level account" in _written_text(session)


# -- delete -----------------------------------------------------------------


def test_delete_with_correct_username_confirmation_deletes(db, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["d", "0", "1", "alice", "b"])
    _run(session, db, sysop)
    assert not any(u.username == "alice" for u in list_users(db))
    assert "deleted" in _written_text(session)


def test_delete_with_mismatched_confirmation_does_not_delete(db, sysop):
    create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["d", "0", "1", "not-alice", "b"])
    _run(session, db, sysop)
    assert any(u.username == "alice" for u in list_users(db))
    assert "Cancelled" in _written_text(session)


def test_delete_with_blank_confirmation_does_not_delete(db, sysop):
    create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["d", "0", "1", "", "b"])
    _run(session, db, sysop)
    assert any(u.username == "alice" for u in list_users(db))


# -- invalid key: bell only (design doc round 52 convention) ---------------


def test_invalid_key_writes_only_a_bell(db, sysop):
    session = FakeSession(["z", "b"])
    _run(session, db, sysop)
    bell_index = session.written.index("\a")
    assert session.written[bell_index] == "\a"
    assert session.written[:bell_index].count("Choice: ") == 1
