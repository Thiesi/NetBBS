"""
DNS mutation backend for the managed-DNS service (design doc §16, issue
#201) -- a small `Protocol` behind which the actual DNS write lives, so
`services.managed_dns.server`'s registration/heartbeat/release logic
never depends on which DNS mechanism is behind it.

Two implementations: `LoggingDnsProvider` (records intended mutations in
memory, never touches a real network -- the default for development and
every automated test) and `Rfc2136DnsProvider` (the real one, TSIG-
signed RFC 2136 dynamic updates against a BIND authoritative server --
the user's own infrastructure, not a commercial DNS API). Both fully
interchangeable: `services.managed_dns.server` is built and tested
entirely against the protocol, never against either implementation's own
internals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

import dns.query
import dns.rcode
import dns.rdataclass
import dns.rdatatype
import dns.tsig
import dns.tsigkeyring
import dns.update

RecordKind = Literal["A", "AAAA"]

# RFC 2136 updates default to 300s -- deliberately short, since a
# dynamic-DNS record is expected to change; a long TTL would keep
# resolvers pointed at a stale address for longer than this service's
# own heartbeat interval can correct.
_DEFAULT_TTL_SECONDS = 300


class DnsProviderError(Exception):
    """Raised by any `DnsProvider` implementation when a mutation could
    not be applied -- callers (`services.managed_dns.server`) treat this
    as a failed heartbeat/registration attempt, never a crash."""


class DnsProvider(Protocol):
    def upsert_record(self, name: str, kind: RecordKind, address: str) -> None:
        """Point `name` (a full FQDN, e.g. `"myboard.netbbs.org."`) at
        `address`, replacing whatever record of the same `kind` was
        there before. Idempotent: calling this again with the same
        arguments is a no-op change."""
        ...

    def delete_record(self, name: str) -> None:
        """Remove every address record (`A` and `AAAA`) for `name`.
        A no-op, not an error, if none exist -- release/reclaim call
        this unconditionally without first checking what's there."""
        ...


@dataclass
class LoggingDnsProvider:
    """Records intended mutations in memory instead of calling any real
    DNS mechanism -- the default until a deployment actually configures
    `Rfc2136DnsProvider`, and the only provider any automated test ever
    exercises (design doc §16: "which DNS provider... is implementation-
    time detail, not blocking" -- this is what makes the rest of the
    service buildable and testable without that decision being made
    first)."""

    upserts: list[tuple[str, RecordKind, str]] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)
    # name -> {kind: address}, the provider's own view of "what's
    # currently published" -- lets a test assert end state, not just
    # that a call happened.
    records: dict[str, dict[RecordKind, str]] = field(default_factory=dict)

    def upsert_record(self, name: str, kind: RecordKind, address: str) -> None:
        self.upserts.append((name, kind, address))
        self.records[name] = {kind: address}

    def delete_record(self, name: str) -> None:
        self.deletes.append(name)
        self.records.pop(name, None)


@dataclass
class Rfc2136DnsProvider:
    """Real BIND integration via RFC 2136 dynamic updates, TSIG-signed.
    Requires the zone's own `named.conf` to grant this TSIG key an
    `allow-update` policy for the records it needs to touch -- that
    server-side configuration is an operational step, not performed
    here (see `services/managed_dns/README.md`).

    `server` is the BIND server's own address to send updates to
    (usually the zone's primary/master, not necessarily a public-facing
    resolver). `zone` is the zone name updates are scoped to (e.g.
    `"netbbs.org"`) -- distinct from `name` in `upsert_record`/
    `delete_record`, which is always the full record owner name within
    that zone.
    """

    server: str
    zone: str
    keyname: str
    secret: str
    algorithm: str = "hmac-sha256"
    port: int = 53
    timeout_seconds: float = 10.0

    def _keyring(self):
        return dns.tsigkeyring.from_text({self.keyname: self.secret})

    def _send(self, update: "dns.update.Update") -> None:
        try:
            response = dns.query.tcp(update, self.server, port=self.port, timeout=self.timeout_seconds)
        except Exception as exc:  # dnspython raises several distinct exception types for network/protocol failure
            raise DnsProviderError(f"RFC 2136 update to {self.server} failed: {exc}") from exc
        rcode = response.rcode()
        if rcode != dns.rcode.NOERROR:
            raise DnsProviderError(
                f"RFC 2136 update to {self.server} was rejected: {dns.rcode.to_text(rcode)}"
            )

    def upsert_record(self, name: str, kind: RecordKind, address: str) -> None:
        try:
            update = dns.update.Update(
                self.zone, keyring=self._keyring(), keyname=self.keyname, keyalgorithm=self.algorithm
            )
            rdtype = dns.rdatatype.A if kind == "A" else dns.rdatatype.AAAA
            other_rdtype = dns.rdatatype.AAAA if kind == "A" else dns.rdatatype.A
            # An address-family change replaces the published endpoint,
            # rather than leaving the old family reachable indefinitely.
            update.delete(name, other_rdtype)
            update.replace(name, _DEFAULT_TTL_SECONDS, rdtype, address)
            self._send(update)
        except DnsProviderError:
            raise
        except Exception as exc:
            raise DnsProviderError(f"could not construct RFC 2136 update: {exc}") from exc

    def delete_record(self, name: str) -> None:
        try:
            update = dns.update.Update(
                self.zone, keyring=self._keyring(), keyname=self.keyname, keyalgorithm=self.algorithm
            )
            # Never issue delete-ANY: unrelated TXT/MX/etc. data at this
            # owner name is outside this provider's authority.
            update.delete(name, dns.rdatatype.A)
            update.delete(name, dns.rdatatype.AAAA)
            self._send(update)
        except DnsProviderError:
            raise
        except Exception as exc:
            raise DnsProviderError(f"could not construct RFC 2136 deletion: {exc}") from exc
