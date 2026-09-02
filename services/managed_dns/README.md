# Managed netbbs.org subdomain + dynamic DNS -- operations runbook

This is the project-operated backend for design doc §16 (issue #201):
one instance, run by the project, serving every SysOp who opts a node
into a managed `name.netbbs.org` subdomain. It is a separate deployable
from the installable `netbbs` package -- see `__init__.py` in this
directory for why -- and is *not* something any node installs.

This document is the operational side of standing that instance up:
generating a TSIG key, configuring BIND to accept updates signed with
it, and configuring this service's own environment. It does not cover
the node-side opt-in flow (`src/netbbs/managed_dns/`, design doc §16
Decision 1) or the abuse-control tuning already summarized in the
design doc's "Implemented" paragraph -- only what an operator needs to
actually run this process against a real BIND server.

## 1. Install dependencies

This directory has its own `requirements.txt`, deliberately not part of
`pyproject.toml`'s node-install dependencies:

```
pip install -r services/managed_dns/requirements.txt
```

## 2. Generate a TSIG key

Use BIND's own `tsig-keygen` (ships with `bind9utils` on most
distributions):

```
tsig-keygen -a hmac-sha256 managed-dns-key > managed-dns-key.conf
```

This prints a `key "managed-dns-key" { algorithm hmac-sha256; secret
"<base64>"; };` block. Keep the secret -- it's needed both by BIND
(step 3) and by this service's own environment (step 4). Never commit
it to the repository.

## 3. Configure BIND to accept updates signed with that key

Include the generated key block in `named.conf` (or a file it
`include`s), then scope an `allow-update` policy on the `netbbs.org`
zone to that key specifically -- never `allow-update { any; }`, and
never the zone's own transfer/notify ACL, which is a different concern:

```
zone "netbbs.org" {
    type master;
    file "/etc/bind/db.netbbs.org";
    allow-update { key managed-dns-key; };
};
```

Reload BIND (`rndc reload`) after editing. Confirm the zone actually
accepted the new config with `rndc zonestatus netbbs.org` before moving
on -- a typo here fails silently at update time otherwise, surfacing
only as `DnsProviderError` from this service once it's already live.

`Rfc2136DnsProvider` sends updates over TCP to whichever host you name
as `MANAGED_DNS_TSIG_SERVER` below -- point it at the zone's primary
(the host actually authoritative for writes), not a secondary or a
public-facing resolver.

## 4. Configure this service

`services/managed_dns/__main__.py` reads its entire configuration from
environment variables -- no TOML file, matching a lightweight
systemd/rc.d-friendly convention rather than the main node's
`nodeconfig` model. Required:

- `MANAGED_DNS_DB_PATH` -- path to this service's own SQLite database
  file (created on first run if absent). Has nothing to do with any
  node's own database; this is the `registrations` table alone.

TSIG / real BIND integration (all four required together, or the
service falls back to `LoggingDnsProvider` and logs a warning instead
of touching DNS at all -- useful for a dry run, wrong for production):

- `MANAGED_DNS_TSIG_KEYNAME` -- the key name from step 2 (e.g.
  `managed-dns-key`).
- `MANAGED_DNS_TSIG_SECRET` -- the base64 secret from step 2.
- `MANAGED_DNS_TSIG_SERVER` -- the BIND primary's address.
- `MANAGED_DNS_TSIG_ZONE` -- the zone updates are scoped to (e.g.
  `netbbs.org`).

Optional, all with working defaults (see `services/managed_dns/
__main__.py` for the exact default values currently shipped):

- `MANAGED_DNS_HOST` / `MANAGED_DNS_PORT` -- bind address for this
  service's own HTTP listener. Put a real reverse proxy in front for
  TLS; this process itself speaks plain HTTP.
- `MANAGED_DNS_TRUST_X_FORWARDED_FOR` -- set to `1`/`true` only once
  that reverse proxy is actually in place and this service can trust
  the header it sets. Leaving this on without a trusted proxy in front
  lets any caller spoof its own source address into a dynamic-DNS
  record.
- `MANAGED_DNS_MIN_AGE_SECONDS` -- Decision 3's age gate before a
  `pending` registration first publishes.
- `MANAGED_DNS_COOLDOWN_SECONDS` -- Decision 5's shared cooldown before
  a released or abandoned name becomes claimable by a *different*
  registrant.
- `MANAGED_DNS_ABANDONMENT_SECONDS` -- how long without a heartbeat
  before a `matured` registration is swept to `abandoned`.
- `MANAGED_DNS_RATE_LIMIT_CAPACITY` / `MANAGED_DNS_RATE_LIMIT_REFILL_PER_MINUTE`
  -- the service-wide registration rate limiter (Decision 3, hard
  reject once exceeded, no queue).
- `MANAGED_DNS_CUMULATIVE_CAP` -- the ceiling on total active
  registrations (Decision 3, also a hard reject).

## 5. Run it

```
MANAGED_DNS_DB_PATH=/var/lib/netbbs-managed-dns/registrations.db \
MANAGED_DNS_TSIG_KEYNAME=managed-dns-key \
MANAGED_DNS_TSIG_SECRET=<base64 secret from step 2> \
MANAGED_DNS_TSIG_SERVER=<BIND primary address> \
MANAGED_DNS_TSIG_ZONE=netbbs.org \
python -m services.managed_dns
```

Logs to stdout; responds to `SIGTERM`/`SIGINT` with a clean shutdown
(stops accepting new connections, then returns). Put it under
systemd/rc.d supervision the same way the netbbs.org website's own
deployment is supervised -- this service has no built-in restart-on-
crash behavior of its own.

## 6. Verify end to end

Before pointing real SysOps at this instance:

1. Register a test subdomain from a node's admin screen (or directly
   via `POST /register`) and confirm it does *not* resolve yet.
2. Wait past `MANAGED_DNS_MIN_AGE_SECONDS` (or temporarily set it low
   for this one check), send a heartbeat, and confirm the name now
   resolves to the address that heartbeat carried.
3. Release it and confirm the record is gone (`dig` returns nothing)
   but a *different* registrant's `/register` for the same name is
   still rejected until `MANAGED_DNS_COOLDOWN_SECONDS` elapses.

Every automated test in `tests/test_managed_dns_*.py` runs against
`LoggingDnsProvider` only -- none of them exercise real BIND. This
manual pass is the only verification that the TSIG key, the
`allow-update` ACL, and the zone's primary address are actually
correct together.
