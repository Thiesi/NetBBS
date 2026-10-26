"""
Shared pytest configuration.

Automatically downgrades the Argon2id cost parameters used for identity
file encryption (`netbbs.identity.keys`) and password hashing
(`netbbs.auth.passwords`) to libsodium's cheapest ("MIN") tier for the
whole test session.

Production code defaults to much more expensive tiers — SENSITIVE for
identity files, INTERACTIVE for password hashing — appropriate for real
use, but multiplied across dozens of tests it adds real wall-clock time
for no benefit, since none of our tests are testing Argon2id's own
cost/security properties. Applying this via an autouse fixture means
individual test files never need to know this is happening — no test
call site needs to change.
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import socket
import sqlite3
import tempfile
from pathlib import Path

import nacl.pwhash
import pytest

import netbbs.auth.passwords as passwords_module
import netbbs.storage.database as database_module
import netbbs.storage.execution as execution_module
import netbbs.identity.keys as keys_module
import netbbs.net.ssh as ssh_module


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "timing_sensitive: the test's subject is a real-time window (an input "
        "timeout, a burst threshold), so a starved process fails it by design. "
        "Skipped under pytest-xdist; run them with `pytest -m timing_sensitive`.",
    )


def pytest_collection_modifyitems(config, items):
    # A parallel worker cannot give these tests the scheduling they measure.
    # Skipped rather than deselected, so they stay visible in the summary.
    if not hasattr(config, "workerinput"):
        return
    skip = pytest.mark.skip(reason="timing_sensitive: run serially with `pytest -m timing_sensitive`")
    for item in items:
        if "timing_sensitive" in item.keywords:
            item.add_marker(skip)


_real_getaddrinfo = socket.getaddrinfo


def _local_only_getaddrinfo(host, *args, **kwargs):
    """`socket.getaddrinfo` that refuses to look up any host outside this machine.

    Issue #714: a test that accepted reliable-node participation started a
    real node, which dialled the shipped roster -- the live ReLink seed --
    and registered a fake peer there on every suite run. `src/` holds four
    production endpoints a test can reach that way (the reliable-node
    fallback and roster, managed DNS, the GitHub releases API). Refusing the
    name lookup closes all of them at once, whatever a test configures. IP
    literals and loopback names resolve as before, which is all the suite's
    real sockets use.
    """
    name = host.decode() if isinstance(host, bytes) else host
    if name in (None, "", "localhost") or str(name).endswith(".localhost"):
        return _real_getaddrinfo(host, *args, **kwargs)
    try:
        ipaddress.ip_address(str(name).split("%", 1)[0])
    except ValueError:
        raise socket.gaierror(
            socket.EAI_NONAME, f"test suite: refusing to resolve {name!r} -- tests must not reach real hosts"
        ) from None
    return _real_getaddrinfo(host, *args, **kwargs)


# Environment proxies would route around the name check: aiohttp
# (`trust_env=True`) and urllib resolve only the proxy and hand it the real
# hostname (Codex review of #715).
_PROXY_VARIABLES = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "WS_PROXY", "WSS_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "ws_proxy", "wss_proxy",
)


@pytest.fixture(autouse=True)
def _no_door_splash(monkeypatch):
    """Bundled doors launched by a test open straight on their first screen.

    A finished launch splash waits for a key, so a test that starts a real door
    and waits for its first prompt before typing would wait forever. The
    splash's own tests take this back off (`monkeypatch.delenv`). The games'
    mid-game scenes are switched off the same way, so a scripted session never
    types into one.

    `run_door` hands a door only a narrow environment, so the switches are
    forwarded into it here too; a SysOp sets them through the door profile."""
    monkeypatch.setenv("DOOR_SPLASH", "0")
    monkeypatch.setenv("DOOR_SCENES", "0")
    from netbbs.doors import runtime

    door_environment = runtime._door_environment

    def with_splash_switch(*args, **kwargs):
        env = door_environment(*args, **kwargs)
        for switch in ("DOOR_SPLASH", "DOOR_SCENES"):
            if switch in os.environ:
                env[switch] = os.environ[switch]
        return env

    monkeypatch.setattr(runtime, "_door_environment", with_splash_switch)
    yield


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _local_only_getaddrinfo)
    for variable in _PROXY_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    yield


@pytest.fixture(autouse=True)
def _fast_rsa_host_keys(monkeypatch):
    # Every SSH test server on a fresh database generates an RSA host key
    # (issue #964); 3072 bits costs about 0.3 s each. None of the tests
    # are about RSA's strength, as with Argon2id below.
    monkeypatch.setattr(ssh_module, "RSA_HOST_KEY_BITS", 1024)
    yield


@pytest.fixture(autouse=True)
def _fast_argon2id(monkeypatch):
    monkeypatch.setattr(keys_module, "_SAVE_OPSLIMIT", nacl.pwhash.argon2id.OPSLIMIT_MIN)
    monkeypatch.setattr(keys_module, "_SAVE_MEMLIMIT", nacl.pwhash.argon2id.MEMLIMIT_MIN)
    monkeypatch.setattr(passwords_module, "_PASSWORD_OPSLIMIT", nacl.pwhash.argon2id.OPSLIMIT_MIN)
    monkeypatch.setattr(passwords_module, "_PASSWORD_MEMLIMIT", nacl.pwhash.argon2id.MEMLIMIT_MIN)
    yield


_FULL_MIGRATIONS = database_module.MIGRATIONS
_original_apply_migrations = database_module.Database._apply_migrations
_template_path: Path | None = None


def _migrated_template() -> Path:
    """One database migrated through every migration, made once per test
    process (each xdist worker has its own) and copied from then on."""
    global _template_path
    if _template_path is None:
        path = Path(tempfile.mkdtemp(prefix="netbbs-template-")) / "template.db"
        connection = sqlite3.connect(path)
        try:
            template = database_module.Database.__new__(database_module.Database)
            template.path, template.connection = path, connection
            connection.row_factory = sqlite3.Row
            template._configure_pragmas()
            for name, function in (("netbbs_plain_post_body", database_module._plain_post_body),
                                   ("netbbs_link_user_subject_id", database_module._link_user_subject_id)):
                connection.create_function(name, 1, function, deterministic=True)
            _original_apply_migrations(template)
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()
        _template_path = path
    return _template_path


def _fresh(connection: sqlite3.Connection) -> bool:
    return (connection.execute("PRAGMA user_version").fetchone()[0] == 0
            and connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0)


@pytest.fixture(autouse=True)
def _migrated_template_databases(monkeypatch):
    """Issue #1569: a fresh `Database` starts as a copy of one migrated once.

    Every migration, run from nothing, costs about 0.3 s per database on an
    idle Windows host and several times that under a parallel suite, and the
    suite opens thousands. Only a brand-new, empty file with the full
    migration list is copied: an existing database still migrates from its
    own version, and a test that truncates `MIGRATIONS` to build an older
    schema still runs the real migrations it kept. The copy is SQLite's own
    backup into the open connection, so the pragmas and SQL functions set on
    it are untouched, and the template was made by those same migrations."""
    def apply_migrations(self):
        if database_module.MIGRATIONS is _FULL_MIGRATIONS and _fresh(self.connection):
            with contextlib.closing(sqlite3.connect(_migrated_template())) as template:
                template.backup(self.connection)
            return
        _original_apply_migrations(self)

    monkeypatch.setattr(database_module.Database, "_apply_migrations", apply_migrations)
    yield


@pytest.fixture(autouse=True)
def _strict_lane_transactions(monkeypatch):
    """Issue #1059: every lane job in the suite must return with no open
    transaction. A job that leaks raises `LeakedTransactionError`, so the
    whole suite is the regression net for the class."""
    monkeypatch.setattr(execution_module, "STRICT_TRANSACTIONS", True)
