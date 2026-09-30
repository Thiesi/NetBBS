"""Issue #964 finding 13: SyncTERM 1.9 over SSH failed with "Error -30
activating session" -- Cryptlib's "Server sent an excessive number of
consecutive no-op packets, it may be stuck in a loop".

Cryptlib's client (session/ssh2_rd.c, `readHSPacketSSH2`) reads handshake
packets in a loop that skips SSH_MSG_IGNORE, SSH_MSG_DEBUG and
SSH_MSG_USERAUTH_BANNER, and fails once that loop has run more than three
times -- counting the real packet that ends it. AsyncSSH puts an empty
SSH_MSG_IGNORE in front of every encrypted packet past key exchange, so the
pre-auth banner reached the client as IGNORE, BANNER, IGNORE, FAILURE: four.

These tests watch what a client actually receives during authentication and
hold the run of no-ops (plus the packet that ends it) to at most three.
"""

from __future__ import annotations

import asyncio

import asyncssh
import pytest
from asyncssh import connection as ssh_connection
from asyncssh.constants import MSG_DEBUG, MSG_IGNORE, MSG_USERAUTH_BANNER, MSG_USERAUTH_SUCCESS

from netbbs.auth.users import create_user
from netbbs.net.session import Session
from netbbs.net.ssh import SSHServer
from netbbs.storage.database import Database

_NOOPS = {MSG_IGNORE, MSG_DEBUG, MSG_USERAUTH_BANNER}
# Cryptlib's limit: the skip loop may run at most three times, the packet
# that ends the run included.
CRYPTLIB_MAX_RUN = 3


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


def _received_types(monkeypatch) -> list[int]:
    """Record every packet type a *client* connection receives, in wire
    order. `log_received_packet` is called for every received packet on the
    handler it's dispatched to: the connection itself, its auth object, or a
    channel -- each of which can reach its client connection."""
    received: list[int] = []
    original = ssh_connection.SSHPacketHandler.log_received_packet

    def record(self, pkttype, pktid, packet, note=""):
        conn = self if isinstance(self, ssh_connection.SSHConnection) else getattr(self, "_conn", None)
        if isinstance(conn, ssh_connection.SSHClientConnection):
            received.append(pkttype)
        return original(self, pkttype, pktid, packet, note)

    monkeypatch.setattr(ssh_connection.SSHPacketHandler, "log_received_packet", record)
    return received


def _longest_cryptlib_run(types: list[int]) -> int:
    """The largest count Cryptlib's skip loop reaches before auth succeeds:
    consecutive no-ops plus the real packet that ends them."""
    run, worst = 0, 0
    for pkttype in types:
        if pkttype in _NOOPS:
            run += 1
            continue
        worst = max(worst, run + 1)
        run = 0
        if pkttype == MSG_USERAUTH_SUCCESS:
            break
    return worst


async def _run_server(db):
    async def handler(session: Session):
        pass

    server = SSHServer(host="127.0.0.1", port=0, db=db, session_handler=handler)
    await server.start()
    return server


def test_password_login_after_a_refused_key_never_sends_cryptlib_too_many_no_ops(db, monkeypatch):
    # SyncTERM's own flow: an auth query first, then its RSA key (refused),
    # then the password -- each step answered while the banner is shown.
    create_user(db, "alice", password="hunter2hunter2", user_level=10)
    received = _received_types(monkeypatch)

    async def scenario():
        server = await _run_server(db)
        try:
            key = asyncssh.generate_private_key("ssh-rsa", 2048)
            async with asyncssh.connect(
                "127.0.0.1",
                server.port,
                username="alice",
                password="hunter2hunter2",
                client_keys=[key],
                preferred_auth="publickey,password",
                known_hosts=None,
            ):
                pass
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert MSG_USERAUTH_BANNER in received, "the pre-auth banner must still be sent"
    assert _longest_cryptlib_run(received) <= CRYPTLIB_MAX_RUN, received


def test_a_refused_password_never_sends_cryptlib_too_many_no_ops(db, monkeypatch):
    create_user(db, "alice", password="hunter2hunter2", user_level=10)
    received = _received_types(monkeypatch)

    async def scenario():
        server = await _run_server(db)
        try:
            with pytest.raises(asyncssh.PermissionDenied):
                async with asyncssh.connect(
                    "127.0.0.1",
                    server.port,
                    username="alice",
                    password="wrong-password",
                    preferred_auth="password",
                    known_hosts=None,
                ):
                    pass
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert MSG_USERAUTH_BANNER in received
    assert _longest_cryptlib_run(received + [MSG_USERAUTH_SUCCESS]) <= CRYPTLIB_MAX_RUN, received


def test_ignore_packets_still_precede_packets_after_login(db, monkeypatch):
    # Only the pre-auth padding is dropped; once the caller is in, AsyncSSH
    # behaves exactly as before.
    create_user(db, "alice", password="hunter2hunter2", user_level=10)
    received = _received_types(monkeypatch)

    async def scenario():
        server = await _run_server(db)
        try:
            async with asyncssh.connect(
                "127.0.0.1",
                server.port,
                username="alice",
                password="hunter2hunter2",
                preferred_auth="password",
                known_hosts=None,
            ) as conn:
                process = await conn.create_process(term_type="xterm", term_size=(80, 24))
                process.close()
        finally:
            await server.stop()

    asyncio.run(scenario())
    after_login = received[received.index(MSG_USERAUTH_SUCCESS) + 1 :]
    assert MSG_IGNORE in after_login, received


def test_the_server_offers_no_cbc_cipher(db):
    # Dropping the pre-auth IGNORE padding is only safe because no CBC
    # cipher can be negotiated: that padding exists to protect CBC's
    # predictable IVs. If a CBC cipher ever becomes negotiable, revisit
    # `_drop_ignore_padding_before_auth`.
    create_user(db, "alice", password="hunter2hunter2", user_level=10)

    async def scenario():
        server = await _run_server(db)
        try:
            with pytest.raises((asyncssh.KeyExchangeFailed, asyncssh.DisconnectError)):
                async with asyncssh.connect(
                    "127.0.0.1",
                    server.port,
                    username="alice",
                    password="hunter2hunter2",
                    encryption_algs=["aes128-cbc", "aes256-cbc", "3des-cbc"],
                    known_hosts=None,
                ):
                    pass
        finally:
            await server.stop()

    asyncio.run(scenario())
