# NetBBS v7.8.1

The managed netbbs.org name service is running, and this release is the
address of it.

Since the feature shipped, `DEFAULT_SERVICE_URL` — the address an
ordinary node reaches when its operator configures nothing — was `None`,
because the backend stood nowhere. v7.8.0 gave it an operator side; this
release gives it a home. The project now runs one instance of
`services.managed_dns` at `https://dns.netbbs.org`, and this is the
one-line code change that points every node at it. That is roadmap
tracker step 2 (#612).

**This release does not migrate anything.** Node schema stays 67,
Voidrunner careers stay at save schema 2, War Dialer worlds at world
schema 10, and neither Link protocol version moves
(`NETBBS_PROTOCOL_VERSION` 1, `REALTIME_PROTOCOL_VERSION` 4). No new
config key. Rolling back is a wheel swap, with nothing to undo first.

## The managed name service has an address (#612)

`netbbs.managed_dns.state.DEFAULT_SERVICE_URL` is `https://dns.netbbs.org`.

A node whose SysOp opted a board into a managed `name.netbbs.org`
subdomain, and set nothing else, now reaches a real service: register a
name, keep it pointed at the node's current address, release or rename
it, all from the SysOp console's `[D]NS` screen. Until this release the
same screen recorded the opt-in and said the service was not running
yet, because there was no address to dial.

Nothing changes for a node that set `[managed_dns] service_url` by hand
to talk to its own or a staging instance; that override still wins, and
removing it falls back to this shipped address on the next start. A node
already holding a registration keeps presenting its credential only to
the service that issued it.

**The blocklist now reserves the names the netbbs.org zone already
carries** — `dns`, `relink`, `ns`, `ns1`, `ns2`, alongside `netbbs`,
`www`, `mail` and the other conventional ones. The DNS provider
*replaces* a name's address records rather than adding to them, so a
registration for a name the zone serves statically would have
overwritten the project's own record rather than being refused. It is
refused now.

The service's operations runbook (`services/managed_dns/README.md`)
gained the two things standing the instance up taught that it had not
said: a reverse proxy must drop a client's own `X-Forwarded-For` before
forwarding, because the service trusts the leftmost entry and a proxy
appends rather than replaces; and once a zone is made dynamic, static
edits to it go through `rndc freeze`/`thaw`.

## Upgrade and rollback

Replace the wheel and restart. Nothing migrates — node database, career
saves, worlds and both Link protocol versions are all unchanged from
7.8.0 — so a 7.8.0 wheel and a 7.8.1 wheel open each other's databases,
and there is no config line to remove before rolling back. A node that
had reached the managed service under 7.8.1 simply stops having a
default address again under 7.8.0; a registration it already holds is
untouched.

**MANUAL — for every SysOp:** nothing. A node that already carries a
managed-DNS opt-in will, on its next start under this release, be able
to register from the `[D]NS` screen where before it was told the
service was not running. That is the whole of it.

**MANUAL — for an operator running their own `services.managed_dns`:**
nothing changes for you; `DEFAULT_SERVICE_URL` is only the fallback a
node uses when it is told nothing, and you point your nodes at your own
instance with `[managed_dns] service_url`.

## Verification boundaries

What this release does **not** establish:

- **The default address was not driven from an ordinary node's own
  opt-in flow against the live service in this release's tests.** The
  suite exercises the node client against a loopback instance, and the
  live instance was verified out of band during deployment: a name
  registered, matured past the age gate, resolved on both public
  secondaries and a public resolver, refused a forged `X-Forwarded-For`,
  and returned to cooldown on release. The value shipped here is checked
  by a test that validates it against the same rules an operator's
  override must satisfy, not by a node dialing it inside the suite.
- **ReLink is the only node pointed at the service so far**, by a manual
  `service_url`, and it does not register a name of its own — it holds
  the admin token and is the revocation console. OutBound reaches the
  service once this release is deployed to it, and registers from its
  own session.
- **The in-BBS `[A]dminister service` console was not driven from a live
  session.** The node stores the token and address and the service's
  loopback admin route answers, but the SysOp screen itself has been
  exercised only in the suite.
- Everything v7.8.0's notes left open that this release did not touch —
  the password screens unrun on a live node, the door chrome judged from
  the gallery, the attestation disclosure gap (#596) — remains as stated
  there.
