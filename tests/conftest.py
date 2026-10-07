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

import ipaddress
import socket

import nacl.pwhash
import pytest

import netbbs.auth.passwords as passwords_module
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
    splash's own tests take this back off (`monkeypatch.delenv`)."""
    monkeypatch.setenv("DOOR_SPLASH", "0")
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


@pytest.fixture(autouse=True)
def _strict_lane_transactions(monkeypatch):
    """Issue #1059: every lane job in the suite must return with no open
    transaction. A job that leaks raises `LeakedTransactionError`, so the
    whole suite is the regression net for the class."""
    monkeypatch.setattr(execution_module, "STRICT_TRANSACTIONS", True)
