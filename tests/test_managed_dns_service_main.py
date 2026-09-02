"""Tests for services.managed_dns.__main__'s environment-driven config loading (issue #201 Phase 6)."""

from __future__ import annotations

import pytest

from services.managed_dns.__main__ import (
    ConfigError,
    _build_dns_provider,
    _build_server,
    _env_bool,
    _env_float,
    _env_int,
)
from services.managed_dns.dns_provider import LoggingDnsProvider, Rfc2136DnsProvider
from services.managed_dns.server import ManagedDnsServer


def test_env_float_returns_default_when_unset(monkeypatch):
    monkeypatch.delenv("SOME_FLOAT", raising=False)
    assert _env_float("SOME_FLOAT", 1.5) == 1.5


def test_env_float_parses_a_set_value(monkeypatch):
    monkeypatch.setenv("SOME_FLOAT", "42.5")
    assert _env_float("SOME_FLOAT", 1.5) == 42.5


def test_env_float_rejects_a_non_numeric_value(monkeypatch):
    monkeypatch.setenv("SOME_FLOAT", "not-a-number")
    with pytest.raises(ConfigError):
        _env_float("SOME_FLOAT", 1.5)


def test_env_int_parses_a_set_value(monkeypatch):
    monkeypatch.setenv("SOME_INT", "7")
    assert _env_int("SOME_INT", 1) == 7


def test_env_int_rejects_a_non_integer_value(monkeypatch):
    monkeypatch.setenv("SOME_INT", "7.5")
    with pytest.raises(ConfigError):
        _env_int("SOME_INT", 1)


@pytest.mark.parametrize("value,expected", [("1", True), ("true", True), ("True", True), ("yes", True), ("on", True), ("0", False), ("false", False), ("no", False), ("", False), ("anything-else", False)])
def test_env_bool_parses_common_truthy_falsy_spellings(monkeypatch, value, expected):
    monkeypatch.setenv("SOME_BOOL", value)
    assert _env_bool("SOME_BOOL", False) is expected


def test_env_bool_returns_default_when_unset(monkeypatch):
    monkeypatch.delenv("SOME_BOOL", raising=False)
    assert _env_bool("SOME_BOOL", True) is True
    assert _env_bool("SOME_BOOL", False) is False


def test_build_dns_provider_falls_back_to_logging_when_tsig_unset(monkeypatch):
    monkeypatch.delenv("MANAGED_DNS_TSIG_KEYNAME", raising=False)
    monkeypatch.delenv("MANAGED_DNS_TSIG_SECRET", raising=False)
    assert isinstance(_build_dns_provider(), LoggingDnsProvider)


def test_build_dns_provider_falls_back_when_only_one_tsig_var_is_set(monkeypatch):
    monkeypatch.setenv("MANAGED_DNS_TSIG_KEYNAME", "my-key")
    monkeypatch.delenv("MANAGED_DNS_TSIG_SECRET", raising=False)
    assert isinstance(_build_dns_provider(), LoggingDnsProvider)


def test_build_dns_provider_builds_rfc2136_when_tsig_is_fully_set(monkeypatch):
    monkeypatch.setenv("MANAGED_DNS_TSIG_KEYNAME", "my-key")
    monkeypatch.setenv("MANAGED_DNS_TSIG_SECRET", "c2VjcmV0")
    monkeypatch.setenv("MANAGED_DNS_BIND_SERVER", "10.0.0.5")
    monkeypatch.setenv("MANAGED_DNS_ZONE", "example.org")

    provider = _build_dns_provider()

    assert isinstance(provider, Rfc2136DnsProvider)
    assert provider.keyname == "my-key"
    assert provider.secret == "c2VjcmV0"
    assert provider.server == "10.0.0.5"
    assert provider.zone == "example.org"


def test_build_server_requires_db_path(monkeypatch):
    monkeypatch.delenv("MANAGED_DNS_DB_PATH", raising=False)
    with pytest.raises(ConfigError, match="MANAGED_DNS_DB_PATH"):
        _build_server()


def test_build_server_builds_a_real_server_with_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("MANAGED_DNS_DB_PATH", str(tmp_path / "managed_dns.db"))
    monkeypatch.delenv("MANAGED_DNS_TSIG_KEYNAME", raising=False)
    monkeypatch.delenv("MANAGED_DNS_TSIG_SECRET", raising=False)

    server = _build_server()
    try:
        assert isinstance(server, ManagedDnsServer)
        assert server.host == "127.0.0.1"
    finally:
        server._db.close()
