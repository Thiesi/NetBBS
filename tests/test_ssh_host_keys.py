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
import logging
import os
import stat

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


_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="Windows mode bits do not say who can read a file")


def test_new_host_keys_are_created_owner_only(db, monkeypatch):
    # Issue #976: asyncssh's write_private_key took the umask's mode, so a
    # host key was world-readable under umask 022. Recorded on every
    # platform: the mode each key file is created with.
    created: dict[str, int] = {}
    real_open = os.open

    def recording_open(path, flags, mode=0o777, *args, **kwargs):
        if flags & os.O_CREAT:
            created[os.path.basename(path)] = mode
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(ssh_module.os, "open", recording_open)
    ensure_host_keys(db)
    assert created == {
        f"{db.path.stem}_ssh_host_key.tmp": 0o600,
        f"{db.path.stem}_ssh_host_key_rsa.tmp": 0o600,
    }
    assert not list(db.path.parent.glob("*.tmp"))


@_POSIX_ONLY
def test_new_host_keys_are_mode_0600_under_a_permissive_umask(db):
    previous = os.umask(0o022)
    try:
        paths = ensure_host_keys(db)
    finally:
        os.umask(previous)
    assert [stat.S_IMODE(path.stat().st_mode) for path in paths] == [0o600, 0o600]


def test_an_existing_readable_host_key_is_restricted_and_logged(db, monkeypatch, caplog):
    # A key written before #976, or restored from a backup taken then.
    monkeypatch.setattr(ssh_module, "_POSIX_MODES", True)
    ed25519_path = db.path.parent / f"{db.path.stem}_ssh_host_key"
    asyncssh.generate_private_key("ssh-ed25519").write_private_key(ed25519_path)
    os.chmod(ed25519_path, 0o644)
    before = ed25519_path.read_bytes()
    with caplog.at_level(logging.WARNING, logger=ssh_module._logger.name):
        ensure_host_keys(db)
    assert ed25519_path.read_bytes() == before
    assert any("readable by other accounts" in record.getMessage() and str(ed25519_path) in record.getMessage()
               for record in caplog.records)
    if os.name != "nt":
        assert stat.S_IMODE(ed25519_path.stat().st_mode) == 0o600


def test_a_host_key_that_cannot_be_restricted_still_starts_the_listener(db, monkeypatch, caplog):
    # Review of #976: a key owned by another account (the service user only
    # in its group) or on a read-only mount cannot be chmod-ed. That was a
    # working start before this check existed, so it must stay one.
    monkeypatch.setattr(ssh_module, "_POSIX_MODES", True)
    paths = ensure_host_keys(db)
    for path in paths:
        os.chmod(path, 0o644)

    def refuse(path, mode, *args, **kwargs):
        raise PermissionError(1, "Operation not permitted", str(path))

    monkeypatch.setattr(ssh_module.os, "chmod", refuse)
    with caplog.at_level(logging.WARNING, logger=ssh_module._logger.name):
        assert ensure_host_keys(db) == paths
    messages = [record.getMessage() for record in caplog.records]
    assert sum("could not be restricted" in message for message in messages) == 2


@_POSIX_ONLY
def test_an_owner_only_host_key_is_left_alone(db, caplog):
    ensure_host_keys(db)
    with caplog.at_level(logging.WARNING, logger=ssh_module._logger.name):
        ensure_host_keys(db)
    assert not [record for record in caplog.records if "readable by other accounts" in record.getMessage()]


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
