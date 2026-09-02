"""Tests for services.managed_dns.blocklist (issue #201)."""

from __future__ import annotations

from services.managed_dns.blocklist import is_reserved


def test_reserved_names_are_rejected():
    assert is_reserved("netbbs") is True
    assert is_reserved("admin") is True


def test_ordinary_names_are_not_reserved():
    assert is_reserved("myboard") is False


def test_is_reserved_is_case_sensitive_by_itself():
    """`is_reserved` trusts its caller to already normalize (see its
    own docstring) -- `services.managed_dns.server` always normalizes
    before checking, so this documents the boundary rather than
    asserting a preference either way."""
    assert is_reserved("NETBBS") is False
