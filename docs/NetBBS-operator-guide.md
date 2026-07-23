# NetBBS operator guide

The complete path from "I found this project" to "I am running a
persistent NetBBS node I can safely upgrade and recover" (issue #82).
This is the operator-facing counterpart to the README's own two-node
Link *developer* quickstart — that page is for exercising Link from a
source checkout; this one is for actually running a node.

No paid hosting, containers, or orchestration platform is required or
assumed anywhere below. See design doc §2.1 for the full platform-tier
policy this guide follows; the short version: **NetBSD/pkgsrc is the
primary, fully-supported target; mainstream Linux is supported;
Windows is development-only and not covered here as a deployment
target.**

## 1. Installing NetBBS

Two documented paths. Neither requires an editable/development source
checkout — that workflow (`pip install -e ".[dev]"`) is for
*contributing to* NetBBS, covered in the README, not for running one.

### 1a. Generic POSIX/Linux, via pip (Tier 2)

Tested for this guide: built a real wheel (`python -m build`), installed
it into a brand-new, otherwise-empty virtualenv with no source checkout
present at all, and ran a real node from it — `python -m netbbs
--version`, a real SysOp/board created through the installed package's
own code, a real listener, and a real Telnet login, all from a plain
working directory with no relationship to this repository.

```sh
python3 -m venv /var/lib/netbbs/.venv
/var/lib/netbbs/.venv/bin/pip install "netbbs[ssh,web]"
/var/lib/netbbs/.venv/bin/python -m netbbs --version
```

(Until signed releases are published to PyPI, substitute a wheel built
from a tagged release checkout: `pip install build && python -m build`
in that checkout, then `pip install dist/netbbs-*.whl` instead of the
PyPI name above. Building requires only `setuptools`/`wheel`, no Rust
toolchain — see design doc §2.1 on why PyNaCl was chosen specifically
to keep that true.)

The `ssh`/`web` extras are optional per transport (see the README's own
Requirements section) — a Telnet-only node needs neither.

### 1b. NetBSD, via pkgsrc (Tier 1, primary)

A `pkgsrc` package is not yet published. Sketch of what one looks like,
for whoever picks this up (a real `Makefile`/`PLIST`/`DESCR` under
`lang/python312`-style `USE_TOOLS`/`PYTHON_VERSIONED_DEPENDENCIES`
conventions, distributed once this project has a tagged release worth
packaging):

```make
# pkgsrc/misc/netbbs/Makefile (sketch, not yet submitted)
DISTNAME=       netbbs-${NETBBS_VERSION}
CATEGORIES=     misc
MASTER_SITES=   https://github.com/Thiesi/NetBBS/archive/refs/tags/
DISTFILES=      v${NETBBS_VERSION}.tar.gz

MAINTAINER=     you@example.com
HOMEPAGE=       https://github.com/Thiesi/NetBBS
COMMENT=        Modern TCP/IP-native BBS with ad-hoc mesh federation
LICENSE=        modified-bsd

DEPENDS+=       ${PYPKGPREFIX}-nacl>=1.5:../../security/py-nacl
DEPENDS+=       ${PYPKGPREFIX}-cryptography-[0-9]*:../../security/py-cryptography  # ssh extra
DEPENDS+=       ${PYPKGPREFIX}-aiohttp-[0-9]*:../../www/py-aiohttp                 # web/link extras

USE_LANGUAGES=  # none
PYTHON_VERSIONED_DEPENDENCIES=  yes

.include "../../lang/python/pyversion.mk"
.include "../../mk/bsd.pkg.mk"
```

Until this is submitted and merged, a NetBSD operator installs the same
way as 1a, from the base system's own `pkgin`-provided Python
(`pkgin install python312 py312-pip`), into a venv — every dependency
choice in this project (PyNaCl over `cryptography` for the core
identity system, FTS5 traced through the actual pkgsrc build chain
rather than assumed, no Rust-toolchain requirement) was already made
specifically so this works cleanly on NetBSD today, pkgsrc package or
not.

## 2. First run

Create a dedicated, unprivileged system user and a state directory —
this guide uses `netbbs`/`/var/lib/netbbs`, matching the example
service files in `examples/`:

```sh
sudo useradd --system --home /var/lib/netbbs --create-home netbbs   # Linux
# or, NetBSD: useradd -d /var/lib/netbbs -m netbbs
```

Write a config file (see the README's "Running a node" section for the
full option reference) at `/etc/netbbs/netbbs.toml`:

```toml
[node]
identity_dir = "/var/lib/netbbs/netbbs_identity"
name = "my-node"

[database]
path = "/var/lib/netbbs/netbbs.db"

[ssh]
enabled = true
host = "0.0.0.0"
port = 2222
```

Create the first SysOp account **before** starting the node as a
service, using the standalone admin CLI (no network listener, no
running node needed) — the same tool used for all subsequent account
maintenance:

```sh
sudo -u netbbs /var/lib/netbbs/.venv/bin/python -m netbbs.admin --db /var/lib/netbbs/netbbs.db
```

With no SysOp account yet on the database, this prompts to create one
interactively (username, then a password and/or a public key) and exits
— see `netbbs.admin.__main__._bootstrap_first_sysop` for exactly what
it does. This is the real, supported bootstrap path; the `scripts/
create_test_user.py`-style helpers elsewhere in this repository are
development conveniences, not an operator-facing tool.

## 3. Running as a service

Copy the example unit for your platform from `examples/` (see that
directory's own README for both), adjust the config path/user if you
didn't use the layout above, then enable it with your platform's
ordinary tooling (`systemctl enable --now netbbs` / NetBSD's
`rc.conf`+`service netbbs start`). NetBBS never daemonizes itself
(design doc §13.8) — it runs in the foreground and expects the service
supervisor to background and restart it, which both example units do.

Graceful shutdown: sending `SIGTERM` (what `systemctl stop`/`service
... stop` both do) warns any connected users, waits up to
`shutdown.graceful_delay_seconds` (60s default), then disconnects and
exits cleanly — not an abrupt kill. Give your supervisor's own stop
timeout enough headroom above that value (the example systemd unit
sets `TimeoutStopSec=90`).

## 4. Persistent state

Everything NetBBS writes to disk, all derived from the database path
you configured (`/var/lib/netbbs/netbbs.db` in the examples above) —
back up all of it together, not just the database (see §5):

| What | Path (relative to your configured `--db`) |
|---|---|
| Database | the configured path itself |
| Uploaded file content | `<db-stem>_files/` |
| Node identity (Link keys) | your configured `identity_dir` |
| SSH host key | `<db-stem>_ssh_host_key` |
| Welcome banner (if customized) | `<db-stem>_welcome_banner.ans` |
| Config file | wherever `--config` points (not derived from `--db`) |
| Logs | not written to a file by NetBBS itself — captured by your
  service supervisor (`journalctl -u netbbs` under systemd; syslog/
  `daemon` facility under NetBSD's `rc.d`, see `examples/netbbs.rc`) |
| Backups | wherever you choose with `--to` (§5) — not a fixed path |

Uninstalling the package (`pip uninstall netbbs`, or removing a pkgsrc
package) only ever removes the installed Python package itself — every
path above lives outside that package entirely, so uninstalling never
silently deletes node state. Removing a node's actual data is a
separate, deliberate action an operator takes themselves.

## 5. Backup and restore

Use the supported tooling, never a raw filesystem copy of a live
database (SQLite WAL mode makes a plain `cp` of the `.db` file
inconsistent):

```sh
python -m netbbs.backup create --db /var/lib/netbbs/netbbs.db \
  --identity-dir /var/lib/netbbs/netbbs_identity --to /path/to/backups/$(date +%F)
```

Restore is staged and validated, refusing against a still-running node
rather than overwriting live state (design doc §13.10). See
`docs/NetBBS-disaster-recovery-drill.md` for a complete, actually-run
walkthrough of both directions, including what a corrupted backup and a
concurrent-writer conflict each look like.

## 6. Upgrading

1. **Back up first**, unconditionally (§5) — this is the rollback path
   if anything goes wrong.
2. Stop the service (`systemctl stop netbbs` / `service netbbs stop`).
3. Upgrade the package with your platform's ordinary tooling:
   `pip install --upgrade netbbs` (inside the same venv), or the
   pkgsrc equivalent once a package exists (§1b).
4. Start the service again.

On startup, NetBBS compares the database's own recorded schema version
(SQLite's `PRAGMA user_version`) against what the running build
expects (`python -m netbbs --version` prints both the release version
and this schema number). Three outcomes, all deliberate (design doc
§13, worklog §10):

- **Same or older schema, newer build:** pending migrations (if any)
  apply automatically and safely on this same startup — migrations are
  additive and tested against realistic data, never edited after
  release.
- **Newer schema than this build knows about** (e.g. a downgrade, or a
  database touched by a later version): startup fails immediately with
  a clear error rather than silently misreading data it doesn't
  understand. Restore the pre-upgrade backup from step 1, or install a
  build new enough to match.
- **Corrupt or inconsistent state** (a broken key-transition chain, a
  database that fails its own integrity check): startup fails clearly
  rather than degrading silently.

**Known rollback limitation:** once a migration has applied, the *code*
can be downgraded freely, but the *database* generally cannot be read
by the older build afterward (this is exactly the newer-schema case
above, now self-inflicted by rolling back). Restoring the step-1 backup
is the supported way back, not attempting to run old code against an
already-migrated database.

`netbbs.selfupdate` also has real, unit-tested plumbing for a
git-checkout-style deployment to check GitHub Releases and download/
extract a new tarball on its own (visible today as the SysOp menu's
manual "check for updates" action) — but the actual *apply and restart*
half of that flow (`prepare_update`/`confirm_update`/`roll_back_update`)
is intentionally not yet wired into any command or menu action. That is
a deliberately deferred, higher-stakes decision (see that module's own
docstrings), not an oversight; the package-manager-based upgrade path
above is the currently supported one.

## 7. Uninstalling

`pip uninstall netbbs` (or the pkgsrc equivalent) removes the installed
package only. Your database, identity, uploaded files, and config are
untouched — see §4's path table. Delete them yourself, deliberately,
if you actually want the node's data gone; nothing in the uninstall
path does this for you.
