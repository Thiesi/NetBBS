"""The SSH server offers an RSA host key beside Ed25519 (issue #964,
finding 8).

SyncTERM up to its Cryptlib-based builds (before 2026-04) could not use an
Ed25519 host key: Cryptlib's SSH host key table (`session/ssh2_algo.c`)
lists `ecdsa-sha2-nistp256`, `rsa-sha2-256` and `ssh-rsa` only, so a node
offering nothing but `ssh-ed25519` failed with "Error -20 activating
session" (CRYPT_ERROR_NOTAVAIL). An RSA key served as `rsa-sha2-256` and
`rsa-sha2-512` reaches those clients; SHA-1 `ssh-rsa` is never offered."""

from __future__ import annotations

import asyncio

import asyncssh
import pytest

import netbbs.net.ssh as ssh_module
from netbbs.backup import _extra_artifact_paths
from netbbs.net.session import Session
from netbbs.net.ssh import SSHServer, ensure_host_keys
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


def _algorithm_of(path) -> str:
    return asyncssh.read_private_key(path).get_algorithm()


def test_a_new_node_gets_an_ed25519_and_an_rsa_host_key(db):
    paths = ensure_host_keys(db)
    assert [_algorithm_of(path) for path in paths] == ["ssh-ed25519", "ssh-rsa"]


def test_the_keys_are_kept_across_starts(db):
    first = [path.read_bytes() for path in ensure_host_keys(db)]
    second = [path.read_bytes() for path in ensure_host_keys(db)]
    assert first == second


def test_an_existing_node_keeps_its_ed25519_key_and_gains_an_rsa_one(db):
    ed25519_path = db.path.parent / f"{db.path.stem}_ssh_host_key"
    asyncssh.generate_private_key("ssh-ed25519").write_private_key(ed25519_path)
    before = ed25519_path.read_bytes()
    paths = ensure_host_keys(db)
    assert paths[0] == ed25519_path
    assert ed25519_path.read_bytes() == before
    assert _algorithm_of(paths[1]) == "ssh-rsa"


def test_production_rsa_keys_are_3072_bits(db, monkeypatch):
    # conftest shrinks the size for speed; a real node gets 3072 bits.
    monkeypatch.setattr(ssh_module, "RSA_HOST_KEY_BITS", 3072)
    rsa_path = ensure_host_keys(db)[1]
    assert asyncssh.read_private_key(rsa_path).pyca_key.key_size == 3072


def test_a_backup_carries_the_rsa_host_key(db):
    assert db.path.parent / f"{db.path.stem}_ssh_host_key_rsa" in _extra_artifact_paths(db.path)


async def _handshake(port: int, algorithms: list[str]) -> str:
    """Connect offering only `algorithms` for the host key; return the one
    negotiated. Authentication fails (no such user), which is after the
    key exchange this is about."""
    try:
        async with asyncssh.connect(
            "127.0.0.1", port, username="nobody", password="x", known_hosts=None,
            server_host_key_algs=algorithms,
        ) as conn:
            return conn.get_extra_info("server_host_key_algs") or ""
    except asyncssh.PermissionDenied:
        return "authenticated-past-kex"


def _serve_and_try(db, algorithms):
    async def handler(session: Session):
        pass

    async def scenario():
        server = SSHServer(host="127.0.0.1", port=0, db=db, session_handler=handler)
        await server.start()
        try:
            return await _handshake(server.port, algorithms)
        finally:
            await server.stop()

    return asyncio.run(scenario())


@pytest.mark.parametrize("algorithm", ["ssh-ed25519", "rsa-sha2-256", "rsa-sha2-512"])
def test_the_server_accepts_each_offered_host_key_algorithm(db, algorithm):
    assert _serve_and_try(db, [algorithm]) == "authenticated-past-kex"


def test_sha1_ssh_rsa_is_never_offered(db):
    with pytest.raises(asyncssh.KeyExchangeFailed):
        _serve_and_try(db, ["ssh-rsa"])
