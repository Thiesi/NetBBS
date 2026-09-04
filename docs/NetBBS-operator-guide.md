# NetBBS operator guide

The complete path from "I found this project" to "I am running a
persistent NetBBS node I can safely upgrade and recover" (issue #82).
This is the operator-facing counterpart to the README's own two-node
Link *developer* quickstart — that page is for exercising Link from a
source checkout; this one is for actually running a node.

No paid hosting, containers, or orchestration platform is required or
assumed anywhere below. See design doc §2.1 for the full platform-tier
policy this guide follows; the short version: **NetBSD is the primary,
fully-supported target; mainstream Linux is supported; Windows is
development-only and not covered here as a deployment target.** NetBBS
itself always comes from the project's official GitHub releases or
tagged source. On NetBSD, pkgsrc supplies its external dependencies, not
NetBBS itself.

## 1. Installing NetBBS

Use an official GitHub release for an operator installation. An
editable checkout (`pip install -e ".[dev]"`) is for *contributing to*
NetBBS, covered in the README, not for running one. Distribution through
PyPI, pkgsrc, apt, or another external package manager is not an
official installation or update path.

### 1a. System-independent release installation

Tested for this guide: built a real wheel (`python -m build`), installed
it into a brand-new, otherwise-empty virtualenv with no source checkout
present at all, and ran a real node from it — `python -m netbbs
--version`, a real SysOp/board created through the installed package's
own code, a real listener, and a real Telnet login, all from a plain
working directory with no relationship to this repository.

**Debian/Ubuntu:** the `venv` module's `ensurepip` support is split into
a separate package from base `python3` and is commonly absent on
minimal/server installs — `python3 -m venv` then fails with
`ensurepip is not available`. Install it first:

```sh
sudo apt install python3-venv   # or python3.12-venv, matching your python3 -V
```

```sh
python3 -m venv /var/lib/netbbs/.venv
/var/lib/netbbs/.venv/bin/pip install --upgrade pip
# Download the wheel asset for the desired version from:
# https://github.com/Thiesi/NetBBS/releases
/var/lib/netbbs/.venv/bin/pip install "/path/to/netbbs-VERSION-py3-none-any.whl[ssh,web]"
/var/lib/netbbs/.venv/bin/python -m netbbs --version
```

If a release has no wheel asset, download its tagged source archive from
the same GitHub release, verify that the tag/version is the one intended,
then run `pip install build && python -m build` in the unpacked tree and
install `dist/netbbs-*.whl` as above. Building the NetBBS wheel itself
needs only setuptools and wheel; installing the `ssh` extra may then
build its `cryptography` dependency and need the prerequisites below.

The `ssh`/`web` extras are optional per transport (see the README's own
Requirements section) — a Telnet-only node needs neither.

### 1b. NetBSD prerequisites (Tier 1, primary)

Use the same GitHub release installation as §1a. On NetBSD, pip does not
currently receive an upstream binary wheel for `cryptography`, which is
pulled in by NetBBS's optional `ssh` extra, so expect a source build.
Install its toolchain and headers through pkgsrc first:

```sh
pkgin install python312 py312-pip rust openssl libffi pkgconf
rustc --version
cc --version
```

`cryptography` currently requires Rust 1.83.0 or newer. It also requires
a C compiler, Python headers, and OpenSSL and libffi headers; `pkgconf`
provides the pkg-config-compatible discovery used to find those pkgsrc
libraries. The current pkgsrc Rust package satisfies the version floor.
Ensure the NetBSD `comp` set is installed if `cc` is missing. Rust and
the compiler are needed to build `cryptography`, not to run it after a
successful installation.

This recipe deliberately includes OpenSSL and `pkgconf`: they were not
incidental packages on the reported vanilla-system deployment. They are
part of the documented source-build prerequisites, together with
`libffi`. See PyCA's authoritative
[`cryptography` installation requirements](https://cryptography.io/en/latest/installation/).

A Telnet-only node with SSH, web, and Link disabled does not install the
`ssh` extra and therefore does not acquire `cryptography` through
AsyncSSH. On mainstream Linux, upgrade pip before installing; supported
Linux platforms commonly receive a binary `cryptography` wheel and then
do not need this build toolchain. If pip falls back to a source build,
install the equivalent Rust, C compiler, Python-development, OpenSSL-
development, libffi-development, and pkg-config packages for that
distribution.

**A successful build can still fail to *import* on NetBSD.** Confirmed on
real hardware: `cryptography` builds cleanly against pkgsrc's `openssl`
(pkgconf finds `/usr/pkg/lib/libssl.so.3` fine at build time), but `python -c
"import asyncssh"` then fails with `ImportError: ...bindings/_rust.abi3.so:
Shared object "libssl.so.3" not found`. NetBSD's runtime linker does not
search `/usr/pkg/lib` by default, and the base system's own
`/usr/lib/libssl.so.16` is a different, incompatible version — so the package
*is* present (`pkg_info` will confirm it) but unreachable at runtime. Add
`/usr/pkg/lib` to the linker's search path before starting NetBBS:

```sh
echo /usr/pkg/lib | sudo tee -a /etc/ld.so.conf
sudo ldconfig
```

`examples/netbbs.rc` sets `LD_LIBRARY_PATH` for the rc.d service
automatically; a manual/foreground run needs one of the two fixes above (or
`LD_LIBRARY_PATH=/usr/pkg/lib` on that one invocation) if SSH is enabled.

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

Right after the account is created, the same command shows the one-time
first-run screen: two independent choices, each defaulting to yes on a
bare Enter. **Join NetBBS Link through the reliable nodes** turns Link on
as an outgoing-only node (no port to open) that dials the project's
reliable nodes as seeds and, when it can't be reached directly, uses them
as relays — it asks for a node display name first, since a node can't join
under the shipped placeholder. **A managed `<name>.netbbs.org` subdomain**
registers a public hostname for the node. Both can be changed later from
the SysOp console (`Settings > Join NetBBS Link`, and the `[D]NS` quick
action). If you skip the screen here, it appears once at the first SysOp
login instead. Note that the config above deliberately has no `[link]`
table: leaving `enabled` unset is what lets the first-run answer decide;
an explicit `enabled = true`/`false` always overrides it.

Once that account can log in, `docs/NetBBS-SysOp-Handbook.md` is the
reference for actually running the node day to day — accounts,
permissions and moderation, content areas, identity attestation and name
requirements, node/session operations, and trust policy. This guide stays
focused on the install/deploy/upgrade lifecycle below.

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
exits cleanly — not an abrupt kill. A separate, much shorter
`shutdown.background_task_drain_seconds` (5s default) then bounds how
long teardown itself waits for each of a few internal background tasks
to notice cancellation, and how long each listener waits for a
connection whose client silently vanished (an asleep laptop, a dropped
Wi-Fi link) before dropping it outright -- worst case a few times that
value, not `graceful_delay_seconds` again -- an *immediate* shutdown
(Ctrl+C in an attended terminal) skips the warning wait entirely but
still pays this smaller, unavoidable teardown cost. Such vanished SSH
clients are also detected on their own within about ninety seconds
(transport keepalives), so they no longer hold a node slot until the
operating system's own multi-minute TCP timeout. Give your supervisor's own stop
timeout enough headroom above `graceful_delay_seconds` plus that
worst case (the example systemd unit sets `TimeoutStopSec=90`).

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
| Main-menu masthead (if customized) | `<db-stem>_main_menu_banner.ans` |
| Logoff banner (if customized) | `<db-stem>_logoff_banner.ans` |
| New-account banner, before signup (if customized) | `<db-stem>_new_account_banner_before.ans` |
| New-account banner, after signup (if customized) | `<db-stem>_new_account_banner_after.ans` |
| Board list masthead (if customized) | `<db-stem>_board_list_banner.ans` |
| File area masthead (if customized) | `<db-stem>_file_area_banner.ans` |
| Chat channel picker masthead (if customized) | `<db-stem>_chat_channel_picker_banner.ans` |
| Config file | wherever `--config` points (not derived from `--db`) |
| Logs | `netbbs.log` next to the database, self-rotating at 10 MiB
  with 5 backups kept (50 MiB worst case, never unbounded) — also
  visible via your service supervisor (`journalctl -u netbbs` under
  systemd; syslog/`daemon` facility under NetBSD's `rc.d`, see
  `examples/netbbs.rc`) since NetBBS still logs to stderr/stdout too |
| Backups | wherever you choose with `--to` (§5) — not a fixed path |

Uninstalling the package (`pip uninstall netbbs`) only ever removes the
installed Python package itself — every
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

### 6a. Learning that a release exists

By default, a running node checks the official GitHub Releases API once on
startup and then every 24 hours. A restart within 15 minutes of the last
attempt skips the startup request. These checks only record an outcome; they do
not download or apply anything and do not interrupt a logged-in SysOp with a
notification.

The recorded outcome appears on the SysOp dashboard. `[S]ettings` ->
`[U]pdate` shows the running version, the last result, recent check history,
and whether scheduled checks are enabled. From there a SysOp can check
immediately, turn startup/daily checks on or off, and set, replace, or clear an
optional fine-grained GitHub token for a higher API rate limit. A manual check
still works when scheduled checks are off. The same update screen is available
through the standalone `python -m netbbs.admin` console when the node is
stopped.

A SysOp may instead follow the official GitHub Releases page directly and
choose a particular published release. In either case, discovering a release
and deploying it are separate actions.

### 6b. Deploying a selected release

1. **Back up first**, unconditionally (§5) — this is the rollback path
   if anything goes wrong.
2. Stop the service (`systemctl stop netbbs` / `service netbbs stop`).
3. Download the desired wheel from the official GitHub release and
   install it into the same venv with pip, using the same extras as the
   existing installation (§1a).
4. Start the service again.

**Upgrading a Link-enabled node from a release before the reliable-nodes
onboarding (issue #219):** a node with `[link] enabled = true` now refuses
to start while its display name is still the shipped placeholder
"NetBBS" (design doc §16 Decision 6 — every node on the mesh needs a name
of its own). The refusal is a clear startup error naming the fix, but a
headless service would just restart-loop, so before step 4 set a name
once, offline: `python -m netbbs.admin --db <db>` → `[S]ettings` →
`[N]ode name`. Nodes that already have a name, and local-only nodes,
are unaffected.

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

`netbbs.selfupdate` contains unit-tested release-checking, safe tarball
extraction, database-snapshot, and pending/confirm/rollback primitives. Only
release checking is connected to the SysOp menu and node lifecycle. Nothing
currently calls the download/prepare/confirm/rollback path from a command,
menu, or live node, and there is no implemented process re-exec. This is a
deliberately deferred, higher-stakes decision, not an oversight; the official
GitHub-release wheel installation above is the currently supported upgrade
path.

## 7. Uninstalling

`pip uninstall netbbs` removes the installed package only. Your database,
identity, uploaded files, and config are
untouched — see §4's path table. Delete them yourself, deliberately,
if you actually want the node's data gone; nothing in the uninstall
path does this for you.
