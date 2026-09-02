"""
Tests for services.managed_dns.dns_provider (issue #201).

`Rfc2136DnsProvider` needs a real BIND server to fully exercise over the
network -- that's a manual verification step (see the issue's own
implementation plan), not something CI can sandbox. What *is* tested
here without one: that it builds a correct, well-formed RFC 2136 update
message (right zone, right owner name, right rdata, right TSIG key) by
capturing what would have been sent rather than actually sending it, and
that a non-NOERROR response is surfaced as `DnsProviderError` rather than
silently accepted.
"""

from __future__ import annotations

import dns.message
import dns.rcode
import dns.rdatatype
import pytest

from services.managed_dns.dns_provider import (
    DnsProviderError,
    LoggingDnsProvider,
    Rfc2136DnsProvider,
)


def test_logging_provider_records_upserts():
    provider = LoggingDnsProvider()
    provider.upsert_record("myboard.netbbs.org.", "A", "203.0.113.5")
    assert provider.upserts == [("myboard.netbbs.org.", "A", "203.0.113.5")]
    assert provider.records == {"myboard.netbbs.org.": {"A": "203.0.113.5"}}


def test_logging_provider_upsert_replaces_the_same_kind():
    provider = LoggingDnsProvider()
    provider.upsert_record("myboard.netbbs.org.", "A", "203.0.113.5")
    provider.upsert_record("myboard.netbbs.org.", "A", "203.0.113.9")
    assert provider.records == {"myboard.netbbs.org.": {"A": "203.0.113.9"}}


def test_logging_provider_upsert_replaces_the_previous_address_family():
    provider = LoggingDnsProvider()
    provider.upsert_record("myboard.netbbs.org.", "A", "203.0.113.5")
    provider.upsert_record("myboard.netbbs.org.", "AAAA", "2001:db8::5")
    assert provider.records == {"myboard.netbbs.org.": {"AAAA": "2001:db8::5"}}


def test_logging_provider_delete_removes_every_kind():
    provider = LoggingDnsProvider()
    provider.upsert_record("myboard.netbbs.org.", "A", "203.0.113.5")
    provider.upsert_record("myboard.netbbs.org.", "AAAA", "2001:db8::5")
    provider.delete_record("myboard.netbbs.org.")
    assert provider.deletes == ["myboard.netbbs.org."]
    assert "myboard.netbbs.org." not in provider.records


def test_logging_provider_delete_is_a_safe_no_op_when_nothing_is_there():
    provider = LoggingDnsProvider()
    provider.delete_record("myboard.netbbs.org.")  # must not raise
    assert provider.deletes == ["myboard.netbbs.org."]


def _capture_sent_update(monkeypatch):
    """Patches `dns.query.tcp` to capture the `dns.update.Update`
    message a provider call would have sent, instead of opening a real
    socket, and returns the list it appends to."""
    sent = []

    def fake_tcp(update, server, port=53, timeout=10.0):
        sent.append((update, server, port))
        response = dns.message.make_response(update)
        response.set_rcode(dns.rcode.NOERROR)
        return response

    monkeypatch.setattr("dns.query.tcp", fake_tcp)
    return sent


def _provider() -> Rfc2136DnsProvider:
    return Rfc2136DnsProvider(
        server="127.0.0.1", zone="netbbs.org", keyname="managed-dns-key", secret="c2VjcmV0", port=53
    )


def test_rfc2136_upsert_sends_a_replace_update_for_the_right_zone_and_name(monkeypatch):
    sent = _capture_sent_update(monkeypatch)

    _provider().upsert_record("myboard.netbbs.org.", "A", "203.0.113.5")

    assert len(sent) == 1
    update, server, port = sent[0]
    assert server == "127.0.0.1"
    assert port == 53
    assert str(update.origin) == "netbbs.org."
    # replace() emits two RRsets of the same rdtype: an empty
    # "delete=ANY" marker clearing whatever was there before, and the
    # actual add carrying the new address -- distinguish by content,
    # not just rdtype.
    a_rrsets = [rrset for rrset in update.update if rrset.rdtype == dns.rdatatype.A]
    assert len(a_rrsets) == 2
    added = [rrset for rrset in a_rrsets if len(rrset) > 0]
    assert len(added) == 1
    assert str(added[0].name) == "myboard.netbbs.org."
    assert str(added[0][0]) == "203.0.113.5"


def test_rfc2136_upsert_aaaa_uses_the_right_rdtype(monkeypatch):
    sent = _capture_sent_update(monkeypatch)

    _provider().upsert_record("myboard.netbbs.org.", "AAAA", "2001:db8::5")

    update, _server, _port = sent[0]
    aaaa_rrsets = [rrset for rrset in update.update if rrset.rdtype == dns.rdatatype.AAAA]
    added = [rrset for rrset in aaaa_rrsets if len(rrset) > 0]
    assert len(added) == 1
    assert str(added[0][0]) == "2001:db8::5"


def test_rfc2136_delete_sends_a_delete_update_for_the_right_name(monkeypatch):
    sent = _capture_sent_update(monkeypatch)

    _provider().delete_record("myboard.netbbs.org.")

    update, _server, _port = sent[0]
    assert len(update.update) == 2
    assert {rrset.rdtype for rrset in update.update} == {dns.rdatatype.A, dns.rdatatype.AAAA}
    assert all(str(rrset.name) == "myboard.netbbs.org." for rrset in update.update)


def test_rfc2136_update_is_tsig_signed_with_the_configured_key(monkeypatch):
    sent = _capture_sent_update(monkeypatch)

    _provider().upsert_record("myboard.netbbs.org.", "A", "203.0.113.5")

    update, _server, _port = sent[0]
    assert update.keyname is not None
    assert str(update.keyname) == "managed-dns-key."


def test_rfc2136_a_non_noerror_response_raises_dns_provider_error(monkeypatch):
    def fake_tcp(update, server, port=53, timeout=10.0):
        response = dns.message.make_response(update)
        response.set_rcode(dns.rcode.REFUSED)
        return response

    monkeypatch.setattr("dns.query.tcp", fake_tcp)

    with pytest.raises(DnsProviderError, match="REFUSED"):
        _provider().upsert_record("myboard.netbbs.org.", "A", "203.0.113.5")


def test_rfc2136_a_transport_failure_raises_dns_provider_error(monkeypatch):
    def fake_tcp(update, server, port=53, timeout=10.0):
        raise OSError("connection refused")

    monkeypatch.setattr("dns.query.tcp", fake_tcp)

    with pytest.raises(DnsProviderError, match="connection refused"):
        _provider().upsert_record("myboard.netbbs.org.", "A", "203.0.113.5")
