"""
Integration tests for SSH self-service registration --
`netbbs.net.ssh._NetBBSSSHServer`'s keyboard-interactive
(kbdint) auth handlers. Spins up a real `SSHServer` and connects a real
`asyncssh` client, same as tests/test_ssh.py, with a scripted
`asyncssh.SSHClient` subclass answering each kbdint round in turn --
the client-side equivalent of a human typing a username, then a
password, then confirming it, at three separate prompts.

Registration always ends by *failing* the SSH auth attempt on purpose
(see _NetBBSSSHServer's own docstring on why) -- every scenario here
expects `asyncssh.PermissionDenied`, then inspects the database
directly, and/or opens a *second*, ordinary connection with the new
credentials, to confirm what actually happened server-side.
"""

from __future__ import annotations

import asyncio

import asyncssh
import pytest

from netbbs.auth.users import AuthError, approve_pending_user, create_user, get_user_by_username
from netbbs.config import RegistrationMode, set_registration_mode
from netbbs.net.session import Session
from netbbs.net.ssh import SSHServer
from netbbs.net.throttle import LoginThrottle
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


async def _noop_handler(session: Session) -> None:
    """A session handler for scenarios where registration (during SSH's
    auth phase) is the only thing under test -- no real session should
    ever be reached, since registration always ends by failing the auth
    attempt (see this module's docstring)."""


async def _run_server(db, session_handler, **server_kwargs):
    server = SSHServer(host="127.0.0.1", port=0, db=db, session_handler=session_handler, **server_kwargs)
    await server.start()
    return server


def _throttle(**overrides) -> LoginThrottle:
    defaults = dict(
        per_source_capacity=10.0,
        per_source_refill_per_minute=5.0,
        per_username_capacity=10.0,
        per_username_refill_per_minute=5.0,
        global_capacity=100.0,
        global_refill_per_minute=60.0,
        max_tracked_keys=10_000,
        max_concurrent_unauthenticated_sessions=100,
    )
    defaults.update(overrides)
    return LoginThrottle(**defaults)


class _ScriptedKbdIntClient(asyncssh.SSHClient):
    """Answers each keyboard-interactive round with the next scripted
    response -- a message-only round (no prompts, used by
    `_NetBBSSSHServer._finish_registration`) is answered with `[]`
    rather than consuming a scripted response, matching what a real
    terminal client does for an instructions-only round."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.messages: list[str] = []

    def kbdint_auth_requested(self):
        # Base SSHClient only offers kbdint as a fallback for piggy-
        # backing an already-provided `password=` -- irrelevant here,
        # since registration needs its own multi-round exchange. An
        # empty string means "let the server pick the submethod",
        # which is exactly what _NetBBSSSHServer.get_kbdint_challenge
        # does (it ignores submethods entirely).
        return ""

    async def kbdint_challenge_received(self, name, instructions, lang, prompts):
        if instructions:
            self.messages.append(instructions)
        if not prompts:
            return []
        return [self._responses.pop(0) for _ in prompts]


async def _connect_kbdint(port, *, username="new", responses: list[str], throttle=None):
    client = _ScriptedKbdIntClient(responses)
    conn = await asyncssh.connect(
        "127.0.0.1", port, username=username, known_hosts=None,
        client_factory=lambda: client,
        public_key_auth=False, password_auth=False, kbdint_auth=True,
    )
    return conn, client  # pragma: no cover -- unreachable if PermissionDenied fires first


async def _attempt_kbdint_registration(port, *, username="new", responses: list[str]) -> _ScriptedKbdIntClient:
    """Drives one scripted kbdint registration attempt to completion.
    Registration always ends in PermissionDenied by design (see this
    module's docstring) -- callers assert on `client.messages` and the
    database afterward, not on a successful connection."""
    client = _ScriptedKbdIntClient(responses)
    with pytest.raises(asyncssh.PermissionDenied):
        async with asyncssh.connect(
            "127.0.0.1", port, username=username, known_hosts=None,
            client_factory=lambda: client,
            public_key_auth=False, password_auth=False, kbdint_auth=True,
        ):
            pass
    return client


def test_registering_via_kbdint_creates_the_account(db):
    async def scenario():
        server = await _run_server(db, _noop_handler, throttle=_throttle())
        try:
            client = await _attempt_kbdint_registration(
                server.port, responses=["carol", "hunter2pw", "hunter2pw"]
            )
            return client
        finally:
            await server.stop()

    client = asyncio.run(scenario())
    created = get_user_by_username(db, "carol")
    assert created.pending_approval is False
    assert any("created" in message for message in client.messages)
    assert any("reconnect" in message.lower() for message in client.messages)


def test_registered_account_can_log_in_on_a_fresh_connection(db):
    calls = []

    async def handler(session: Session):
        calls.append("in")

    async def scenario():
        server = await _run_server(db, handler, throttle=_throttle())
        try:
            await _attempt_kbdint_registration(server.port, responses=["carol", "hunter2pw", "hunter2pw"])
            async with asyncssh.connect(
                "127.0.0.1", server.port, username="carol", password="hunter2pw", known_hosts=None
            ) as conn:
                async with conn.create_process(term_type="ansi", term_size=(80, 24), encoding=None):
                    pass
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert calls == ["in"]


def test_registration_with_approval_required_leaves_account_unable_to_log_in(db):
    set_registration_mode(db, RegistrationMode.APPROVAL_REQUIRED)
    sysop = create_user(db, "sysop", password="hunter2", user_level=255)

    async def scenario():
        server = await _run_server(db, _noop_handler, throttle=_throttle())
        try:
            client = await _attempt_kbdint_registration(
                server.port, responses=["carol", "hunter2pw", "hunter2pw"]
            )
            assert any("approve" in message.lower() for message in client.messages)

            # Not yet approved -- an ordinary password login must still fail.
            with pytest.raises(asyncssh.PermissionDenied):
                async with asyncssh.connect(
                    "127.0.0.1", server.port, username="carol", password="hunter2pw", known_hosts=None
                ):
                    pass
        finally:
            await server.stop()

    asyncio.run(scenario())

    pending = get_user_by_username(db, "carol")
    assert pending.pending_approval is True

    approve_pending_user(db, pending, approved_by=sysop)

    calls = []

    async def handler(session: Session):
        calls.append("in")

    async def scenario2():
        server = await _run_server(db, handler, throttle=_throttle())
        try:
            async with asyncssh.connect(
                "127.0.0.1", server.port, username="carol", password="hunter2pw", known_hosts=None
            ) as conn:
                async with conn.create_process(term_type="ansi", term_size=(80, 24), encoding=None):
                    pass
        finally:
            await server.stop()

    asyncio.run(scenario2())
    assert calls == ["in"]


def test_closed_mode_never_offers_the_kbdint_registration_challenge(db):
    # `closed` mode hides registration entirely -- the server
    # never offers the kbdint challenge at all, so a client requesting
    # only kbdint auth fails immediately with no registration prompt
    # ever shown, the SSH-side equivalent of Telnet/web's hidden prompt
    # option.
    set_registration_mode(db, RegistrationMode.CLOSED)

    async def scenario():
        server = await _run_server(db, _noop_handler, throttle=_throttle())
        try:
            return await _attempt_kbdint_registration(
                server.port, responses=["carol", "hunter2pw", "hunter2pw"]
            )
        finally:
            await server.stop()

    client = asyncio.run(scenario())
    assert client.messages == []
    with pytest.raises(AuthError):
        get_user_by_username(db, "carol")


def test_registration_refuses_the_reserved_sentinel_as_a_desired_username(db):
    async def scenario():
        server = await _run_server(db, _noop_handler, throttle=_throttle())
        try:
            client = await _attempt_kbdint_registration(
                server.port, responses=["new", "hunter2pw", "hunter2pw"]
            )
            return client
        finally:
            await server.stop()

    client = asyncio.run(scenario())
    assert any("reserved" in message.lower() for message in client.messages)
    with pytest.raises(Exception):
        get_user_by_username(db, "new")


def test_registration_rejects_mismatched_passwords(db):
    async def scenario():
        server = await _run_server(db, _noop_handler, throttle=_throttle())
        try:
            client = await _attempt_kbdint_registration(
                server.port, responses=["carol", "hunter2pw", "different"]
            )
            return client
        finally:
            await server.stop()

    client = asyncio.run(scenario())
    assert any("did not match" in message.lower() for message in client.messages)
    with pytest.raises(Exception):
        get_user_by_username(db, "carol")


def test_registration_rejects_a_too_short_password(db):
    async def scenario():
        server = await _run_server(db, _noop_handler, throttle=_throttle())
        try:
            client = await _attempt_kbdint_registration(server.port, responses=["carol", "short", "short"])
            return client
        finally:
            await server.stop()

    client = asyncio.run(scenario())
    assert any("at least" in message.lower() for message in client.messages)
    with pytest.raises(Exception):
        get_user_by_username(db, "carol")


def test_registration_is_throttled_by_the_shared_login_throttle(db):
    throttle = _throttle(per_source_capacity=0.0, per_source_refill_per_minute=0.0)

    async def scenario():
        server = await _run_server(db, _noop_handler, throttle=throttle)
        try:
            client = await _attempt_kbdint_registration(
                server.port, responses=["carol", "hunter2pw", "hunter2pw"]
            )
            return client
        finally:
            await server.stop()

    client = asyncio.run(scenario())
    assert any("too many registration attempts" in message.lower() for message in client.messages)
    with pytest.raises(Exception):
        get_user_by_username(db, "carol")


def test_kbdint_is_not_offered_for_an_ordinary_username(db):
    """The reserved-sentinel gating: kbdint must only ever engage for
    NEW_ACCOUNT_SENTINEL, never conditioned on some other username --
    otherwise it could become a new enumeration oracle (see
    _NetBBSSSHServer's own docstring)."""
    create_user(db, "alice", password="hunter2", user_level=10)

    async def scenario():
        server = await _run_server(db, _noop_handler, throttle=_throttle())
        try:
            with pytest.raises(asyncssh.PermissionDenied):
                await _connect_kbdint(server.port, username="alice", responses=["whatever"])
        finally:
            await server.stop()

    asyncio.run(scenario())
