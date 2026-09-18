# NetBBS SysOp Handbook

This handbook takes you from installation to running a community: accounts,
content, networking, games, backups, upgrades, and troubleshooting. You need
basic familiarity with your server's command line, but no Python programming.

[All documentation](README.md) · [User handbook](NetBBS-User-Handbook.md) ·
[Developer handbook](NetBBS-Developer-Handbook.md)

## Contents

- [Installing NetBBS](#installing-netbbs)
- [First account and configuration](#first-account-and-configuration)
- [Running a service](#running-a-service)
- [Caller connections and file transfers](#caller-connections-and-file-transfers)
- [Using the SysOp console](#using-the-sysop-console)
- [Accounts, permissions, and identity](#accounts-permissions-and-identity)
- [Content and Communities](#content-and-communities)
- [Door games](#door-games)
- [NetBBS Link](#netbbs-link)
- [MRC chat bridge](#mrc-chat-bridge)
- [Daily operation](#daily-operation)
- [State, backup, and recovery](#state-backup-and-recovery)
- [Upgrading and removing NetBBS](#upgrading-and-removing-netbbs)
- [Troubleshooting](#troubleshooting)

## Installing NetBBS

**NetBSD is the primary platform; mainstream Linux is supported.** Other
POSIX systems are best effort. Windows is for development and testing.
NetBBS needs Python 3.11 or newer and SQLite with FTS5 support for search.
SQLite is the database engine included with Python; you do not install or
administer a separate database server.

Obtain NetBBS from the official [GitHub releases](https://github.com/Thiesi/NetBBS/releases)
or a tagged source archive. NetBBS itself is not distributed through PyPI,
pkgsrc, or apt. Your operating system's package manager supplies prerequisites.
The commands below use `/var/lib/netbbs` for state, a service account named
`netbbs`, and `/etc/netbbs/netbbs.toml` for configuration. Substitute your paths
consistently if you choose a different layout.

### Prepare the host

**MANUAL — on the host:** install Python and its virtual-environment support.
A virtual environment is a private installation directory for NetBBS and its
libraries; it keeps them separate from other programs on your server.

On Debian/Ubuntu, install `python3-venv` for the Python version you use. On
NetBSD, use pkgsrc packages such as `python312` and `py312-pip`. The SSH extra
uses AsyncSSH and `cryptography`; a source build on NetBSD also needs Rust,
a C compiler, Python headers, OpenSSL, libffi, and pkgconf. A typical pkgin
package set is:

```sh
pkgin install python312 py312-pip rust openssl libffi pkgconf
```

Install NetBSD's `comp` set if the C compiler is missing. Build requirements
can change with dependency releases; use the
[cryptography installation requirements](https://cryptography.io/en/latest/installation/)
for the version pip selects. Rust is a build requirement, not a service you
need to run. On Linux, prebuilt dependency wheels often avoid this toolchain.

Create the unprivileged account and directories **before** installing into them:

```sh
# Linux
sudo useradd --system --user-group --home /var/lib/netbbs --create-home netbbs
sudo install -d -m 750 -o netbbs -g netbbs /var/lib/netbbs /etc/netbbs
```

On NetBSD, the equivalent is:

```sh
sudo groupadd netbbs
sudo useradd -g netbbs -d /var/lib/netbbs -m -s /bin/sh netbbs
sudo install -d -m 750 -o netbbs -g netbbs /var/lib/netbbs /etc/netbbs
```

Skip account/group creation if they already exist. The supplied NetBSD service
script needs a Bourne-compatible account shell such as `/bin/sh` because it
starts the process through `su`; `/sbin/nologin` and csh are unsuitable for it.
Run the application and games as this account, never root.

### Install a release

**MANUAL — on the host:** download the desired wheel asset from GitHub Releases
and make it readable by the service account. Replace the example wheel path
and `VERSION` with the actual downloaded filename. On NetBSD, use `python3.12`
in place of `python3` if that is your installed interpreter's name.

```sh
sudo -u netbbs python3 -m venv /var/lib/netbbs/.venv
sudo -u netbbs /var/lib/netbbs/.venv/bin/python -m pip install --upgrade pip
sudo -u netbbs /var/lib/netbbs/.venv/bin/python -m pip install "/path/to/netbbs-VERSION-py3-none-any.whl[ssh,web]"
/var/lib/netbbs/.venv/bin/python -m netbbs --version
```

`--version` prints the package and database-schema versions without starting
listeners. Choose extras according to what you enable:

| Extra | Needed for |
| --- | --- |
| `ssh` | SSH caller connections, enabled by default |
| `web` | Browser terminal, HTTP file-transfer links, **and NetBBS Link** |
| Neither | A Telnet-only node with SSH, web, and NetBBS Link disabled |

If the release has only a tagged source archive, unpack it, create a temporary
build environment, run `python -m pip install build` and `python -m build`
there, then install its `dist/` wheel as above. Production installs do not
need the `dev` extra or an editable source checkout.

**NetBSD import problem:** if SSH fails with `libssl.so.3 not found` after a
successful install, pkgsrc's `/usr/pkg/lib` may be missing from the runtime
library search path. The supplied rc.d script sets `LD_LIBRARY_PATH` for the
service. For an attended check, use:

```sh
sudo -u netbbs env LD_LIBRARY_PATH=/usr/pkg/lib /var/lib/netbbs/.venv/bin/python -c "import asyncssh"
```

Do not substitute the base system's differently versioned OpenSSL library.

## First account and configuration

**MANUAL — on the host:** save this as `/etc/netbbs/netbbs.toml`, readable by
the service account. This starts with SSH; browser access is covered below.

```toml
[node]
identity_dir = "/var/lib/netbbs/netbbs_identity"
name = "MyCommunity"

[database]
path = "/var/lib/netbbs/netbbs.db"

[ssh]
enabled = true
host = "0.0.0.0"
port = 2222

[telnet]
enabled = false

[web]
enabled = false
```

Create your first SysOp before starting a public listener:

```sh
sudo -u netbbs /var/lib/netbbs/.venv/bin/python -m netbbs.admin --db /var/lib/netbbs/netbbs.db
```

The interactive admin console asks for the first account and its password
and/or public key. Use this supported bootstrap tool; `create_test_user.py`
is a development helper. SysOp accounts have level **255**.

First-run onboarding offers two independent choices:

- **Join NetBBS Link through the reliable nodes:** outgoing-only participation,
  using the project's bootstrap and relay nodes. A distinct node name is required.
- **Managed `<name>.netbbs.org` subdomain:** a public hostname managed through
  the project's DNS service. This does not configure your router, TLS, or ports.

Read the choices before pressing Enter: the first-run choices default to yes.
If not completed here, onboarding is offered at the first SysOp login.

Transport and path settings come from the TOML file and command-line options;
accounts, content, and many live settings are stored in the database. Command-line
options override TOML settings. Run `python -m netbbs --help` using the installed
interpreter for all supported switches. A TOML file is read only when supplied
with `--config`; placing `netbbs.toml` in the working directory is not enough.

Leave `[link] enabled` unset if you want the onboarding/Settings choice to decide
participation. An explicit `enabled = false` or `true` overrides that choice.
The TOML `[node] name` labels the cryptographic key files; it does not set the
public display name. Set that through onboarding or **Settings → Node name**.
Changing a label does not change a node's cryptographic identity.

## Running a service

**MANUAL — on the host:** download the service example from the same release's
source tree. Examples are not installed by the wheel. Adjust paths and account
names before installing it; [examples/README.md](../examples/README.md) describes
both files.

For Linux, install [netbbs.service](../examples/netbbs.service) as
`/etc/systemd/system/netbbs.service`, then:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now netbbs
sudo systemctl status netbbs
```

For NetBSD, install [netbbs.rc](../examples/netbbs.rc) as executable
`/etc/rc.d/netbbs`, add `netbbs=YES` to `/etc/rc.conf`, then:

```sh
sudo service netbbs start
sudo service netbbs status
```

Check a real login as well as service status. The systemd example restarts on
failure; NetBSD rc.d does not automatically restart a crashed node. If you
need that behavior, arrange an external health check or supervisor yourself.

NetBBS runs in the foreground. The service manager handles backgrounding.
`systemctl stop netbbs` or `service netbbs stop` requests a graceful shutdown:
callers are warned, then disconnected after the configured delay (60 seconds
by default). Cleanup takes additional time. Increase the service stop timeout
if you raise that delay or configure slow-stopping door services.

The Linux unit restricts writable paths to `/var/lib/netbbs`. **MANUAL:** extend
its `ReadWritePaths` if you put games or state elsewhere. NetBSD's example sets
`HOME` to `netbbs_statedir`; remember this when locating Voidrunner saves.

## Caller connections and file transfers

| Connection | Default | What to arrange |
| --- | --- | --- |
| SSH | Enabled, all interfaces, port 2222 | Install `ssh` extra; allow the chosen port |
| Telnet | Disabled, loopback, port 2323 | Enable explicitly; plaintext credentials and traffic |
| Web | Disabled, loopback, port 8080 | Install `web` extra; provide HTTPS through a reverse proxy |

**MANUAL — outside NetBBS:** configure firewall rules, router forwarding,
DNS, and the reverse proxy for the connection methods you offer. A bind
address of `0.0.0.0` means all IPv4 interfaces; it is not an address to give
callers. A loopback listener is reachable only from the same host.

For a browser terminal and transfer links behind a proxy, replace the existing
`[web]` table with:

```toml
[web]
enabled = true
host = "127.0.0.1"
port = 8080
public_url = "https://bbs.example.org"
```

Point the HTTPS proxy at that local port, forwarding WebSocket upgrades as
well as ordinary requests. Set its upload body limit at least as high as
NetBBS's configured upload cap. Use a real hostname and certificate;
`bbs.example.org` is a placeholder. Restart after changing listener settings.

`public_url` supplies the externally reachable base address for transfer links.
Without it, a node bound to a wildcard or loopback address cannot give remote
terminal callers a usable link. A specific reachable bind address may be used
as a fallback, but explicit configuration is preferable behind a proxy.

File transfers work through the browser, through short-lived browser links
shown to terminal callers, or through Zmodem in a capable terminal client.
PuTTY and ordinary SSH clients lack Zmodem. They need the web listener for
browser transfers. SFTP is not provided by the NetBBS SSH server.

Transfer URLs contain bearer tokens: anyone holding one can use that transfer.
Use HTTPS, avoid sharing or logging those links, and request a new link after
expiry. The node's upload limit and any lower proxy limit both apply.

## Using the SysOp console

Log in as a level-255 account and press **[S]ysOp**. The dashboard shows
node mode, active sessions, pending approvals, backup/update status, and
NetBBS Link health when enabled. Refresh it after making changes elsewhere.

| Area | Purpose |
| --- | --- |
| Users | Accounts, registration, levels, approval, identity-verifier grants |
| Content | Message boards, file areas, chat channels, Communities, doors, moderation |
| Operations | Sessions, maintenance, audit log, backups, Link diagnostics |
| Settings | Branding, timestamps, node name, network participation, update checks |

Quick actions lead to the same screens. Use the displayed keys rather than
old menu letters from release notes. **Back** leaves a screen. Draft editors
keep changes local until **Save**; Back discards the draft. Arrow keys move
between fields, and Enter or Space opens the selected field. Where available,
**Ctrl+H** explains fields.

In current field prompts, the existing value is already in the input line:
Enter accepts it, Escape cancels the field edit, and deleting all text clears
it where the field permits an empty value. Do not assume an empty line means
“keep the old value.”

`python -m netbbs.admin --db /var/lib/netbbs/netbbs.db` is a second,
**interactive** route to account/content administration without a caller
connection. It cannot control a running process's sessions, maintenance, or
live networking. Its Backup screen is status-only; use the backup CLI there.

## Accounts, permissions, and identity

Choose registration mode under **Users → Registration**:

- **Open:** new accounts can log in immediately.
- **Approval required:** approve a pending account from its detail screen.
- **Closed:** only SysOps create accounts.

An account's numeric level controls level-based access. Level 255 grants SysOp
administration; ordinary levels express your local policy. NetBBS refuses an
account change that would leave no enabled, approved SysOp. Disabling an account
revokes its access; deletion is permanent and requires its exact name. Existing
content retains its recorded author label.

Once your node has run NetBBS Link, deleting an account also retires its
username: nobody can register it again. On the Link a username is the
account's identity, so whoever took a freed name would receive Link mail
written to the previous holder and inherit the authorship of their carried
posts and whatever other nodes had recorded about them. Callers who try a
retired name are told only that it is unavailable. If you deleted a test
account, or a caller you removed has come back, release the name under
**Users → Retired names**; the release is audit-logged. A node that has never
run Link keeps reusable names.

A caller who forgot their password cannot recover it themselves: NetBBS has no
e-mail or other out-of-band channel. Open the account's detail screen and
choose **Password** to set a new one; the old password is neither shown nor
needed. If you are locked out of your own SysOp account, run
`python -m netbbs.admin reset-password YOURNAME --db /path/to/netbbs.db` on
the host. Both routes are audit-logged with your name. An account that also
has an SSH key can have its password removed from the same screen, which
leaves key login as its only way in.

Access can also depend on resource-specific grants, verified age, and verified
name. Raising a level does not replace identity verification. When access looks
wrong, inspect both the account and the resource's effective settings, including
Community defaults.

### Verified age and names

A SysOp, or an account explicitly granted **Identity verification**, records
attestations through **Verify** on the main menu. Granting this permission is
separate from making someone a moderator. Establish your own verification
procedure outside NetBBS; the software records the attestation, not proof
that a document or a person's claim is authentic.

Name requirements cycle through:

| Setting | Effect |
| --- | --- |
| None | No verified-name requirement |
| Verified | A name attestation is required; the name is not displayed by this setting |
| Verified and displayed | Verification is required and the attested name is shown alongside contributions in that resource |

A verified name does not replace the account's chosen display name. Tell callers
about disclosure before requiring it. Age and name verification are separate;
review the available attributes and expiry on the attestation screen.

## Content and Communities

Create resources under **Content**, or use **Create** in a resource picker.
An empty picker remains usable for creating the first resource. Review the
draft and save it explicitly.

- **Message boards:** set read/write levels, moderation, retention, and gates.
  Pending posts remain in an approval queue. Edits retain revision identity.
- **File areas:** set access, upload policy, size/retention rules, and moderation.
  Uploads may include a description extracted from `FILE_ID.DIZ`. A remote
  catalogue entry is metadata; fetching its bytes is a separate action.
- **Chat channels:** set join gates, visibility, invitations, and moderation.
  Hidden or invite-only channels need more than a sufficient account level.
- **Communities:** group related resources and provide inherited defaults.
  Inspect effective gates on child resources before changing a shared default.
  A Community is not an automatic grant to every resource inside it.
- **Doors:** attach registered games to a Community or leave them uncategorized.

Moderator grants belong to a resource or Community. Membership alone does not
make someone a moderator. Grant only the scope required; use approval queues
and the audit log to review actions. Chat moderation commands are used inside
the channel by someone with the appropriate authority.

The [user handbook](NetBBS-User-Handbook.md) covers posting, drafts, follows,
search, mail, and everyday chat. **New scan** and **Find** only show content the
caller can access on this node, including carried linked content.

## Door games

Open **Content → Doors → Gallery** to register Retro Trivia, Voidrunner, or
War Dialer. Gallery entries fill a draft with the installed interpreter and
sensible defaults; review and Save. Callers reach games through Jump to or a
Community.

Third-party native programs, DOS games through DOSBox-X, and remote RLogin
services use compatibility profiles. Start with minimum play level 255,
choose a template, set your actual paths, run **Check setup**, then **Test as
SysOp**. Testing launches real programs and may change game data, even if you
later discard the configuration draft. Test normal quit, disconnect, timeout,
and each offered caller transport before opening it to users.

**MANUAL — outside NetBBS:** obtain games and licenses, install their runtimes,
prepare writable installation directories, and configure any remote-service
tunnel and credentials. NetBBS does not install these components. Use the
[door setup guide](NetBBS-door-guide.md) for exact profiles, game versions,
DOSBox-X patches, service controls, and troubleshooting.

Native doors run as the NetBBS service account. Resource limits do **not**
prevent them reading files or using the network as that account. Install only
trusted programs. A door's temporary launch directory is deleted afterward;
persistent game data must live elsewhere.

A companion service keeps a game's world process alive between callers.
NetBBS can supervise it and exposes Start, Halt, Restart, and its recent log
on the door detail screen. Install/update the service yourself; stop it before
copying its data. Do not raise a game's session count until its concurrent
save handling has been tested.

A door may request permission to post to message boards. **Outbound** is off
by default and configured per door: choose an allowlist and posting limit.
Posts use a distinct door label, not the caller's identity. Normal board
moderation applies. Allowing a linked board can distribute posts to peers;
local deletion cannot retrieve copies already received elsewhere.

## NetBBS Link

NetBBS Link connects independent NetBBS nodes. It carries selected message
boards, chat channels, file catalogues, and mail. Your node decides what to
carry and which peers to trust. A signature identifies who sent something;
it does not make that sender trustworthy.

**Current boundary:** asynchronous services, trust controls, live chat,
presence, and live relay are implemented. Federation remains private and
experimental pending [public-readiness validation](NetBBS-phase4-readiness.md)
and independently operated dogfood. Independent implementation compatibility
is not yet established. Local Communities are available; Link Communities
and cross-node `/dm` invitation chats are not. Cross-node `/msg` and `/private`
do work when a live path exists.

### Join and share

Install the `web` extra even if callers use only SSH. Enable participation
through first-run onboarding or **Settings → Join NetBBS Link**, unless TOML
explicitly overrides it. Use a distinct node name. The default outgoing-only
mode uses reliable nodes for discovery and relay; no Link port forwarding is
needed. Caller access still needs its own reachable listener.

Reliable nodes are bootstrap/relay infrastructure, not owners of your BBS.
Manual seeds can supplement them. A full peer additionally advertises a
reachable Link address and needs the corresponding network setup.

For a full peer, these are the two Link listeners, separate from caller ports:

```toml
[link]
enabled = true
host = "0.0.0.0"
port = 7862
realtime_port = 8862
outgoing_only = false
advertised_host = "bbs.example.org"
advertised_port = 7862
realtime_advertised_port = 8862
```

**MANUAL — outside NetBBS:** replace the hostname and allow/forward both TCP
ports to this node. Restart and have another operator check asynchronous
exchange and live chat separately. If omitted, the real-time port is the HTTP
Link port plus 1000. Keep outgoing-only mode if you cannot provide this reachability.

The
[developer handbook's configuration reference](NetBBS-Developer-Handbook.md#node-configuration-reference)
lists the less common transport and quota settings.

Create a local resource before promoting it to linked scope. To carry a
remote resource, use its Link browsing/carry actions. Carrying a message board
creates a local browsable copy; file catalogues do not automatically download
all file contents. Ask the other SysOp to verify both sides when first testing
publication. Hello/discovery alone does not prove content arrived.

Asynchronous delivery can continue after a peer reconnects. Live chat and
private messages require a working live session; a failed live message is not
silently converted to mail. Link mail is encrypted to the recipient's home
node for ordinary accounts; the home-node operator can read it.

### Trust and recovery

Use **Link status** for peers and relay state, **Outbox** for pending or failed
work, and **Diagnostics / Follow log** for explanations. Policy trust settings
separate identity integrity, resource behavior, and content conduct: a complaint
about content should not be treated as proof of a forged identity.

Under **Settings → Policy trust**, inspect a subject's effective state and
history before applying an override. Domains, anchors, and reporters determine
whose signals count; multiple identities from one trust domain do not become
independent votes. Quarantine restricts exchange; it does not erase previously
accepted content. A sole-authority exception deliberately weakens the usual
multi-source policy and is marked as a safety deviation.

Verified ages and names cross the Link only when three people agree. The
caller switches sharing on in their Profile. You name the receiving node under
**Settings → Policy trust → Published identity → Recipients**; the list starts
empty, and until you add a node nothing leaves yours whatever callers have
switched on. The other SysOp names your node under **Identity authorities** on
their side. If you have named an authority and nothing arrives, look in
**Diagnostics**: a refusal there means its SysOp has not added your node as a
recipient yet. Removing a recipient stops future sharing only. That node keeps
what it already pulled until each attestation expires, within 90 days, and it
receives no further withdrawals. When a caller switches sharing off, your node
stops serving the value, deletes its signed copy, and tells its recipients to
forget it. Backups you took while it was shared still contain it.

A familiar name attached to a new fingerprint produces an identity warning.
Confirm the full **Technical identity** with the other operator before trusting
it; renaming a node does not transfer its reputation. Managed DNS changes and
node display-name changes are separate. Use the DNS screen's staged rename
and recovery controls; do not release and re-register a name as a rename shortcut.

A managed name stays yours while this node keeps checking in with the service,
which it does every 15 minutes on its own; the **Dynamic IP** choice only
decides whether the published record follows your address. A node the service
has not heard from for about a week has its record taken out of DNS and the
name held for it; the node reclaims the name by itself once it is back, and
the DNS screen shows the last attempt's outcome under the ABANDONED badge
until it succeeds. **Release** is the one exit the node never undoes, and a
name the service operator has **revoked** on a complaint is the other: the
screen says so, names the operator's contact channel, and a different name
can be registered as usual.

Callers reach a managed name on the standard ports (SSH 22, Telnet 23, HTTPS
443); the record itself carries no port. NetBBS listens on 2222/2323/8080 by
default, so a bare `myboard.netbbs.org` reaches your board only through a
port-forward or proxy in front of it. The DNS screen states this against your
configured listeners; the service cannot check it for you.

If you also run the managed-DNS service itself, `[managed_dns] admin_token` in
`netbbs.toml` adds **[A]dminister service** to the DNS screen: the service's
registrations as a table, each in full, and revocation with a reason and a
typed-name confirmation. `services/managed_dns/README.md` §8 is the runbook.

## MRC chat bridge

MRC is a separate, public inter-BBS chat network. It is not NetBBS Link and
does not provide Link's authenticated identities or trust model.

1. Under **Settings → Inter-BBS chat (MRC)**, configure and enable the hub
   connection and the site name/details it announces. Saving applies live.
2. Set **MRC room** on each channel you want bridged. One room maps to one
   channel. Callers see an MRC badge and a notice that their handle is visible.
3. Optionally enable **Open rooms** so callers can select or name rooms through
   the Multi Relay Chat picker. Set its access gates, room cap, retention, and
   blocklist. Adopt a room to retain it, or retire it deliberately.

IP-address reporting and extra caller metadata are separate opt-in settings.
Terminal size is sent for reply formatting. Private MRC messages require each
caller's opt-in and are not confidential from the network. Do not type MRC
passwords as ordinary chat; `/mrc register`, `/mrc identify`, and the related
password commands ask separately without echo.

Use **Node → Chat bridge (MRC)** for connection state, round-trip time, errors,
and reconnect. Pause one channel's bridge or disable the whole bridge without
removing local chat. MRC moderation and room rules also depend on the remote hub.

The room picker learns the network directory when a caller enters an MRC
room; `/rooms` refreshes it. Reported user counts are snapshots, with stale
readings marked. The chat status bar labels local occupancy separately from
MRC occupancy; the hub roster does not supply a remote away count. A hub
nickname or room correction that cannot identify one of several local callers
produces a notice and diagnostic instead of being applied to an arbitrary
account.

## Daily operation

Check pending registrations/posts/files, backup recency, free disk space,
and recent errors. Choose welcome/masthead/banner presets through Settings;
preview before applying. Timestamp format and display timezone are node-wide.

Under **Operations → Node and sessions**:

- **Maintenance** blocks new non-SysOp logins.
- **Drain** warns and disconnects non-SysOp sessions after a delay.
- **Lock & drain** combines both for planned work.
- **Shutdown** stops the node after warning callers.

These controls require a live node session. For a stopped node, use the host's
service controls. Use **Audit log** to see administrative and moderation
activity. Storage garbage collection and draft pruning show the proposed work
before confirmation; review it instead of deleting files directly.

## State, backup, and recovery

### Know what belongs to the node

The database stores accounts, configuration, metadata, and network state.
Files, keys, and game saves also live outside it. For `/var/lib/netbbs/netbbs.db`:

| State | Usual location |
| --- | --- |
| Database | `/var/lib/netbbs/netbbs.db` and live SQLite side files |
| Uploaded content | `/var/lib/netbbs/netbbs_files/` |
| Node identity | Configured `identity_dir` |
| Managed-DNS state and credentials | Database plus credential files beside it |
| SSH host key and custom banners | Files beginning `netbbs_` beside the database |
| War Dialer world | `/var/lib/netbbs/netbbs.db.doors/war-dialer.db`, unless overridden |
| Voidrunner careers | Service account's `~/.netbbs/voidrunner_saves/`, unless `VOIDRUNNER_SAVE_DIR` overrides it |
| Third-party games | Each door's installation directory and any author-documented external state |
| TOML and service configuration | `/etc/netbbs/` and the installed service files |
| Logs | `netbbs.log` beside the database, plus service-manager output |

Keep your configuration, service settings, and external-game state with your
recovery records. The built-in backup is not a copy of every file the service
account can access. Do not copy only a running `.db` file: SQLite's write-ahead
log can contain changes not yet written into that file.

### Create and verify a backup

Stop active games before capture; halt companion services. The BBS itself may
stay running for a supported database backup. From a live SysOp console,
**Backup → Create backup now** writes a timestamped directory under
`netbbs_backups/` beside the database. Check the reported path and game coverage.

For a scheduled job or chosen destination, use the installed backup CLI. Run it
as an account able to read all node state. **Pin the real Voidrunner path**:

```sh
sudo -u netbbs /var/lib/netbbs/.venv/bin/python -m netbbs.backup create \
  --db /var/lib/netbbs/netbbs.db \
  --identity-dir /var/lib/netbbs/netbbs_identity \
  --voidrunner-save-dir /var/lib/netbbs/.netbbs/voidrunner_saves \
  --to /var/lib/netbbs/backups/pre-upgrade-20260913
```

The destination must not already exist; choose a fresh name for each backup.
The Voidrunner path above matches the service layout in this handbook. Use the
path shown by the live Backup screen if yours differs.

**Why the path is worth pinning** ([#555](https://github.com/Thiesi/NetBBS/issues/555)):
Voidrunner keeps its careers under the home directory of whichever account
started the node, and `examples/netbbs.rc` starts it with `HOME` set to the
state directory. A backup CLI run from your own shell has your own `HOME`. Up
to v7.4.0 the two resolved differently and the CLI silently captured no
careers at all -- exit 0, with a line reading "no save directory found", which
sounds like a statement about the node and was a statement about the shell.

From v7.4.1 the node records its own save directory at startup and the CLI
reads it, so the default is correct without a flag. Two cases still need you:

- A node that has not yet started since upgrading has recorded nothing. The
  CLI says so in as many words -- `Voidrunner: NOT CAPTURED` -- rather than
  reporting an empty directory as an empty node. Start the node once, or pass
  the flag.
- A layout that differs from this handbook's still needs `--voidrunner-save-dir`.
  Use the path shown on the live Backup screen.

Read the coverage output either way. An absent Voidrunner component will not
magically reappear during restore.

War Dialer has separate world capture and restore rules; see the
[door guide](NetBBS-door-guide.md). Third-party installation directories are
excluded unless **Backup → Door installations** is enabled. That option copies
them without stopping their writers and does not automatically restore them.
Stop games/services first. A missing or unreadable requested installation fails
the backup. Symlinks are copied as links, not followed to external data.

**MANUAL — outside NetBBS:** copy completed backups off the machine, protect
them as secrets, encrypt them if needed, and arrange retention. Backups contain
private keys and account data. Neither off-site transfer nor rotation is built in.
Also preserve TOML, service configuration, and any game data outside the captured
paths. Inspect `manifest.json` and the coverage messages before relying on an archive.

Door outbound result receipts (`door-outbound/` beside the database) are
included automatically and need no separate copy. Restore replaces them with the
archive's own, so receipts a door wrote after that backup was taken go to the
rollback generation rather than staying beside an older database; an archive made
before NetBBS captured receipts clears them for the same reason. After recovery,
a missing receipt still does not mean its post was never published.

### Restore

Stop the node and every game/service. Keep the current state until the restored
node has been verified. Use the backup tool, which validates and stages the
restore and refuses to overwrite an active node:

```sh
sudo -u netbbs /var/lib/netbbs/.venv/bin/python -m netbbs.backup restore \
  --from /path/to/backup \
  --db /var/lib/netbbs/netbbs.db \
  --identity-dir /var/lib/netbbs/netbbs_identity \
  --voidrunner-to /var/lib/netbbs/.netbbs/voidrunner_saves \
  --war-dialer-to 1=/var/lib/netbbs/netbbs.db.doors/war-dialer.db
```

Use `--voidrunner-to` when the archive includes that component; omit it for an
archive without it. The example also assumes one captured War Dialer world,
key `1`. Every captured world requires an explicit `--war-dialer-to KEY=PATH`
mapping, even for its default location; omit this option if there is no world
component. Read the keys in the backup's manifest and follow the door guide for
multiple or overridden worlds. Restore preserves replaced state in a rollback
directory and reports its location; it does not delete that directory later.

**MANUAL:** restore external door installations from `door-installs/` if included,
restore service/TOML settings, and ensure the service uses the restored game paths.
Set `VOIDRUNNER_SAVE_DIR` to the chosen destination if it differs from the service's
default. Restore never changes that environment setting for you. Do not run the
same restored Link identity on two live nodes.

Restart, log in, read a post, retrieve a file, check Link identity/peers, and enter
a real saved game. Rehearse this first on disposable state using the
[disaster recovery drill](NetBBS-disaster-recovery-drill.md). Automated backup
tests do not replace a recovery exercise on your host.

## Upgrading and removing NetBBS

The node checks GitHub Releases on startup and every 24 hours by default;
a startup within 15 minutes of the last attempt skips another check. Results
appear on the SysOp dashboard and **Settings → Update**. You can check manually,
toggle the schedule, and set an optional GitHub token for a higher API limit.
These checks do not download, install, restart, or interrupt callers.

**MANUAL — on the host:**

1. Read the selected release's notes. Record the current version and paths.
2. Stop games/services and create a backup; verify its game coverage and keep
   an off-node copy.
3. Stop NetBBS through its service manager.
4. Install the selected GitHub-release wheel into the same virtual environment,
   using the same extras as before.
5. Start the service and verify a real login, content, file transfer, and games.

Startup applies required database migrations. An older build refuses a database
with a newer schema. To roll back after a migration, stop the service, reinstall
the earlier release, and restore the pre-upgrade state using compatible tooling;
do not attempt to downgrade the database by hand. Keep the newer state separately
if callers have contributed since the upgrade.

Uninstalling with the environment's `python -m pip uninstall netbbs` removes the
package, not node data. Disable the service first. Removing state, keys, games,
DNS registration, or backups is a separate, deliberate operator action.

## Troubleshooting

| Symptom | Check and next action |
| --- | --- |
| Service will not start | Read service output; check config paths, file permissions, selected interpreter, extras, and port conflicts. Do not treat a zero exit status alone as a working listener. |
| A caller forgot their password, or you are locked out | Set a new password from the account's detail screen (**Password**), or run `python -m netbbs.admin reset-password USERNAME` on the host. Nothing recovers the old one. |
| SSH import fails on NetBSD | Check pkgsrc libraries and `LD_LIBRARY_PATH`; see installation above. |
| Caller cannot log in | Check maintenance mode, pending approval, disabled account, and login throttling before resetting credentials. |
| Caller can read but cannot contribute | Check write/join gates, age/name attestations, moderator grants, and inherited Community settings. |
| Browser terminal or upload fails | Check HTTPS proxy/WebSocket forwarding, upload limits, web listener, and `public_url`. |
| Terminal offers no file-transfer link | Enable/configure the web listener and its public URL, or use a Zmodem-capable client. |
| Link will not start | Check the `web` extra, effective participation setting, and a non-placeholder node name. |
| Peers connect but content is missing | Check carry/subscription decisions, trust state, Outbox, and Diagnostics. Use Repair carried posts only for local materialization repair. |
| Game is busy, fails, or loses state | Check its session limit, Compatibility setup, Last diagnostic, service state, and actual persistent paths. |
| Backup says `Voidrunner: NOT CAPTURED` | The node has not started since v7.4.1, so it has recorded no save directory and the CLI fell back to your shell's home. Start the node once, or rerun with explicit `--voidrunner-save-dir`. |
| Backup says `Voidrunner: no saves at ...` | The node named that directory itself and there is nothing in it -- nobody has played. Nothing to do. |
| Startup refuses the database version | Install a compatible release or restore the matching pre-upgrade backup. Do not edit the schema number. |

On Linux, start with `journalctl -u netbbs`. On NetBSD, the example service's
capture file is `netbbs.service.log` in the state directory; it catches failures
before the application's rotating log opens. That service capture is not
self-rotating, so arrange rotation or an appropriate output policy yourself.
The application's `netbbs.log` rotates at 10 MiB with five retained backups
(up to about 60 MiB including the active file).

Link Diagnostics is a bounded warning/error log, not a transcript of all
content. The administrative Audit log answers who changed a setting or moderated
an item. Inspect those before attempting filesystem repairs.

For unresolved problems, [file an issue](https://github.com/Thiesi/NetBBS/issues)
with the release, operating system, transport, steps, and relevant error text.
Remove passwords, private keys, tokens, personal data, and transfer URLs.
