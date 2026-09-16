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
as `MANAGED_DNS_BIND_SERVER` below -- point it at the zone's primary
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
- `MANAGED_DNS_BIND_SERVER` -- the BIND primary's address.
- `MANAGED_DNS_ZONE` -- the zone updates are scoped to (e.g.
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
- `MANAGED_DNS_CONTACT` -- the channel a SysOp refused by the rate
  limit or the cumulative cap is told to use for a legitimate exception
  (design doc §16 Decision 3, issue #598): a URL or an address, quoted
  verbatim in the refusal. The project's own instance names its issue
  tracker. Unset, the refusal says the operator has not named a channel,
  and the service warns once at startup -- a self-hosted copy must not
  send its SysOps to this project.
- `MANAGED_DNS_ADMIN_TOKEN` -- the bearer token for the `/admin/`
  routes (Decision 4, section 8 below). Unset by default, which leaves
  them refusing every request: an instance whose operator has not set
  one has no administrative surface at all. Set it before you need it.
  Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
  The same value goes into the operator's own node as
  `[managed_dns] admin_token` (section 8), which is what puts the
  administration screen on that node's SysOp console. Keep `/admin/` off
  the public reverse proxy unless that node lives elsewhere: on the
  service host the node reaches the loopback listener directly.

## 5. Run it

```
MANAGED_DNS_DB_PATH=/var/lib/netbbs-managed-dns/registrations.db \
MANAGED_DNS_TSIG_KEYNAME=managed-dns-key \
MANAGED_DNS_TSIG_SECRET=<base64 secret from step 2> \
MANAGED_DNS_BIND_SERVER=<BIND primary address> \
MANAGED_DNS_ZONE=netbbs.org \
python -m services.managed_dns
```

Logs to stdout; responds to `SIGTERM`/`SIGINT` with a clean shutdown
(stops accepting new connections, then returns). Put it under
systemd/rc.d supervision the same way the netbbs.org website's own
deployment is supervised -- this service has no built-in restart-on-
crash behavior of its own.

## 6. Point nodes at it

A running instance is not reachable by anybody until nodes know its
address. There are exactly two ways one gets there, and neither is a
thing a SysOp is ever asked to type (design doc §16 Decision 8, issue
#583):

**The shipped address, for every ordinary node.** Set
`DEFAULT_SERVICE_URL` in `src/netbbs/managed_dns/state.py` to this
instance's public base URL -- the reverse proxy's `https://` address,
not the `MANAGED_DNS_HOST`/`MANAGED_DNS_PORT` bind -- and release. It is
`None` until then, which is why a node today records the SysOp's opt-in
and says the service is not running yet. `tests/test_managed_dns_state.
py` has a test asserting it is still `None`; flip that test in the same
commit.

**`[managed_dns] service_url` in a node's `netbbs.toml`**, or
`--managed-dns-service-url` on its command line, for a node that should
talk to a *different* instance -- a developer running this service
locally, or a staging deployment:

```toml
[managed_dns]
service_url = "https://dns.example.org"
```

The value is a base URL with no trailing slash, no query and no
fragment; `/register`, `/heartbeat` and the rest are appended to it. It
is read into the node's database at startup, so a node has to be
restarted after the setting changes, and *removing* the setting returns
that node to the shipped address on its next start.

It must be `https://` unless it names a loopback address: every request
carries the node's bearer credential, so a plaintext hop to a remote
host hands that secret to anyone on the path. `http://127.0.0.1:<port>`
is the development case, and is why the exception exists -- a node dials
a loopback service address directly, ignoring `HTTP_PROXY`, so the
"nothing leaves the machine" premise holds on a proxied host too.

It may not contain `@` at all: the node records the address in its
database and prints it in logs and SysOp-facing messages, so a URL
embedding a username or password would leak it there, and a rule with
nothing to slip past beats one that has to recognise every spelling. Put
any access control in the service's own reverse proxy; a literal `@` in
a path is `%40`.

A node that already holds a registration and is then pointed somewhere
else does **not** carry its credential across. The address that issued
it is recorded alongside the registration; the updater pauses, and
release, rename and cancellation refuse, until the node is pointed back
or registers fresh with the new service. That means moving this service
to a new address is not transparent to nodes already registered with it
-- they keep the name at the old address until they re-register, and
their old registrations lapse through the ordinary abandonment sweep.

## 7. Verify end to end

Before pointing real SysOps at this instance:

1. Register a test subdomain from a node's admin screen (or directly
   via `POST /register`) and confirm it does *not* resolve yet.
2. Wait past `MANAGED_DNS_MIN_AGE_SECONDS` (or temporarily set it low
   for this one check), send a heartbeat, and confirm the name now
   resolves to the address that heartbeat carried.
3. Release it and confirm the record is gone (`dig` returns nothing)
   but a *different* registrant's `/register` for the same name is
   still rejected until `MANAGED_DNS_COOLDOWN_SECONDS` elapses.
   Then stop the node's heartbeats for longer than
   `MANAGED_DNS_ABANDONMENT_SECONDS` (set it low for the check) and let
   the sweep abandon a second test registration: on the node's next
   pass it must reclaim the name by itself and the record must return
   (design doc §16 Decision 10). The node's DNS screen shows the
   attempt's outcome beside the ABANDONED badge in the meantime.
4. Rename the test registration with `POST /rename`. Confirm the old
   record stays published while the new name is pending, then heartbeat
   the new credential after the age gate and confirm the new record is
   published before the old record is removed. Exercise `POST
   /cancel-rename` during a second pending rename and confirm the old
   registration and credential remain valid.

The node SysOp menu exposes these as `Change [n]ame` and `[C]ancel
change`, alongside `[R]egister` and `Re[l]ease`. The rename is deliberately
make-before-break: the service reserves and matures the new name while the old
one remains canonical. Only the heartbeat which successfully publishes the new
record releases the old registration. Names in any other DNS infrastructure are
never registered or released by this service; operators update that DNS first,
then change the node's configured advertised host.

Every automated test in `tests/test_managed_dns_*.py` runs against
`LoggingDnsProvider` only -- none of them exercise real BIND. This
manual pass is the only verification that the TSIG key, the
`allow-update` ACL, and the zone's primary address are actually
correct together.

## 8. Handling an abuse report

Design doc §16 Decision 4: contested-name disputes are manual and
complaint-driven. There is no automated detection and no appeal queue --
a person reads the complaint and decides. This section is the executable
half of that.

### Where a report arrives

Reports come in through the project's issue tracker:

https://github.com/Thiesi/NetBBS/issues

That is a public channel, which is a real trade-off. A complaint about
impersonation usually names the impersonated party and sometimes carries
evidence its author would rather not publish. Nothing about the
mechanism below assumes the report arrived there; if a report reaches
you privately, act on it the same way.

### What to check before revoking

Revocation removes a live DNS record from under a running board, and
inside the cooldown the holder cannot get the name back by any means.
Being slow here costs a few hours; being wrong costs someone their
board's address.

1. **Resolve it.** `dig +short <name>.netbbs.org` — confirm the name is
   actually live and points where the complaint says.
2. **Look at the row.** Open the service's database read-only and read
   the registration: when it was created, its node fingerprint, its
   last contact. A name registered an hour ago that already resolves to
   a copy of somebody's login page is a different case from a five-year
   board in a naming dispute.
3. **Decide which problem it is.** Impersonation and abuse are what this
   is for. A *naming dispute* between two legitimate boards is not:
   Decision 3 is first-come-first-served deliberately, and revoking on
   "we wanted that name" turns a governance rule into a matter of who
   complains loudest.
4. **Reach the SysOp first where the case allows it.** A board whose
   name merely collides with a trademark may simply rename it — that
   costs them nothing (`Change [N]ame` on their own SysOp console keeps
   the old name live until the new one matures) and costs you a
   revocation you would rather not make.

### Looking and revoking from the SysOp console

The node you operate the service from gets the whole of this section as
screens, once its `netbbs.toml` carries the token the service was
started with:

```toml
[managed_dns]
service_url = "http://127.0.0.1:8080"   # the service's own listener, on its host
admin_token = "<the MANAGED_DNS_ADMIN_TOKEN value>"
```

Restart the node. **Settings → DNS** now offers `[A]dminister service`:
every registration the service holds, as a table (status, last contact,
node fingerprint); pick one and it is shown in full — registered when,
whose node, last contact, the published address, whether a rename is in
flight, the reason if it was already revoked; `[R]evoke` asks for the
reason, then for the name typed back, then does exactly what the `curl`
below does and shows the answer. Steps 2 and 3 of the checklist above
are that screen; step 1, the `dig`, is still yours.

Two things to know about that node:

- **Use the loopback address on the service host.** `service_url` is
  the same address the node would register through, and a registration
  made over loopback publishes `127.0.0.1` as its address -- so the
  operator's node should decline the managed-DNS opt-in (Roanoke serves
  `netbbs.org` itself and does exactly that). A node elsewhere needs
  `/admin/` exposed on the public `https://` address; the token is the
  only gate, so decide that deliberately.
- **The token is in that node's database and backups**, mirrored from
  the config file at every startup like `service_url`, absence included:
  removing it from `netbbs.toml` withdraws the screen on the next start.
  It shares the node's backup archive with the managed-DNS credential
  (Decision 7), which is the same class of secret.

### Revoking by hand

The same act from a shell, for a service with no such node. Requires
`MANAGED_DNS_ADMIN_TOKEN` to be set in the running service's
environment. It is unset by default, which leaves `/admin/revoke`
refusing every request — so set it before you need it, not during an
incident:

```
curl -sS -X POST http://127.0.0.1:8080/admin/revoke \
  -H "Authorization: Bearer $MANAGED_DNS_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "badname", "reason": "impersonation, report 2026-09-16"}'
```

Address this at the service's own listener, not through the public
reverse proxy — the token is a bearer secret and the loopback listener
is the shorter path. `reason` is required, stored on the row, and
printed to the log; write the one sentence that will make sense to you
in six months.

The response names every row that moved:

```json
{"revoked": ["badname"], "status": "revoked", "revoked_at": "..."}
```

Two rows come back when the registrant had a rename in flight — a
rename is one registrant holding two names, and revoking one while the
other matured would hand them a working name.

If DNS deletion fails, nothing is revoked and the call returns 503:
fix the provider problem and run it again rather than leaving a row
marked revoked while the record is still resolving.

### What the former holder sees

- Their record stops resolving immediately.
- Their node's next heartbeat is told the registration was revoked and,
  if `MANAGED_DNS_CONTACT` is set, whom to write to. The updater stops;
  the SysOp console's DNS screen shows **REVOKED** with that channel.
- Pressing `[R]egister` with the name prefilled — the obvious thing for
  them to try — is refused with the same answer. A revoked row is not
  reclaimable by any credential, which is the difference between this
  and an ordinary release; the node's automatic reclaim of an abandoned
  name (Decision 10) is refused the same way.
- They can still register a *different* name: revocation takes the name,
  not the node.

They are told *that*, and where to write, never *why*: the reason is
yours. If the complaint was legitimate and the SysOp is reachable,
telling them yourself is still better than leaving it to a status line.

### After the cooldown

A revoked name expires on `MANAGED_DNS_COOLDOWN_SECONDS`, the same timer
as release and abandonment, after which it is available to a genuinely
new registrant — including, in principle, the person you took it from.
That is deliberate: revocation blocks the holder, not the name.

For a name nobody should ever hold, add it to `RESERVED_NAMES` in
`services/managed_dns/blocklist.py` and redeploy. That is the mechanism
for permanence, and it is a code change on purpose — the blocklist is
curated and reviewed, not appended to by a running process.
