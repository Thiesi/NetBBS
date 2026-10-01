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

Check the version with `python3 --version`: it must say 3.11 or newer. On
Debian/Ubuntu, `python3 -m venv` fails until the matching `venv` package is
installed:

```sh
sudo apt install python3 python3-venv
```

On NetBSD, use pkgsrc packages such as `python312` and `py312-pip`. The SSH extra
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

**MANUAL — on the host:** download the wheel (the `.whl` file) from GitHub
Releases. The service account usually cannot read files in your home directory, so
copy the wheel somewhere it can, such as `/tmp`, first; pip reports a wheel
it cannot read as "does not exist". In the commands below, replace `VERSION`
with the version number only, for example `7.13.0`, and `~/Downloads` with
the directory you downloaded to. On NetBSD, use `python3.12` in place of
`python3` if that is your installed interpreter's name.

```sh
sudo install -m 644 ~/Downloads/netbbs-VERSION-py3-none-any.whl /tmp/
sudo -u netbbs python3 -m venv /var/lib/netbbs/.venv
sudo -u netbbs /var/lib/netbbs/.venv/bin/python -m pip install --upgrade pip
sudo -u netbbs /var/lib/netbbs/.venv/bin/python -m pip install "/tmp/netbbs-VERSION-py3-none-any.whl[ssh,web]"
sudo -u netbbs /var/lib/netbbs/.venv/bin/python -m netbbs --version
```

Tried NetBBS with the website's local trial first? That trial database is
unrelated to this installation and can simply be deleted with its directory.
This installation starts with a fresh database, and you create your SysOp
account again below.

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
Telnet and the web listener are off here, and both listen on loopback
(`127.0.0.1`), which only this host can reach. To offer Telnet, set
`enabled = true` **and** `host = "0.0.0.0"`, as `[ssh]` has; with
`enabled = true` alone, outside callers cannot connect. The web
listener stays on loopback behind an HTTPS proxy, as described below.

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
host = "127.0.0.1"
port = 2323

[web]
enabled = false
host = "127.0.0.1"
port = 8080
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

On a node that has never been started, accepting the managed subdomain records
your answer but cannot pick the name yet: the service knows a node by an
identity it creates on its first start. Start the node, then sign in as SysOp or
run `netbbs.admin` again, and the name editor opens by itself, once. After that
it is **[R]egister** on the DNS screen.

Run `netbbs.admin` as the account the node runs as, as shown above. It writes
owner-only files beside the database (the managed-DNS credential among them),
and a copy written as root is one the node cannot read; if you had to use root,
`chown` the state directory back afterwards.

Transport and path settings come from the TOML file and command-line options;
accounts, content, and many live settings are stored in the database. Command-line
options override TOML settings.

Link limits, the login throttle and the shutdown delays can be set either way.
**Settings → Network & login limits** holds the carry caps, peering, Link limits,
live relay bounds, login throttle and shutdown delays, grouped. A value there
applies the next time the node starts. A key your TOML file or a command-line
option sets still wins: the screen shows it as *set in config* and does not let
you change it there. Remove it from the TOML file to manage it from the console.

The listeners, `public_url`, Link addresses, paths and managed-DNS service stay
in TOML and on the command line only, because a wrong value set from inside
NetBBS could lock you out of the session you would need to fix it. You can still
see them: **Settings → Network & login limits → Node c[o]nfiguration** lists each
one as the node resolved it at its last start, with its TOML key and whether the
value came from the config file, the command line or the default. It works in
`python -m netbbs.admin` too, and never shows the managed-DNS admin token. Run `python -m netbbs --help` using the installed
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
by default). With nobody connected, or once the last caller leaves, it stops
without waiting out the delay. A shutdown you schedule from the console keeps
the delay you chose. Change the delay under **Settings → Network &
login limits**, or as `[shutdown] graceful_delay_seconds`. Cleanup takes
additional time. Increase the service stop timeout
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
callers. A loopback listener is reachable only from the same host. Enabling
Telnet or web with `enabled = true` alone leaves it on loopback; the node log
says so at every start, naming the `host` line to add.

For a browser terminal and transfer links behind a proxy, replace the existing
`[web]` table with:

```toml
[web]
enabled = true
host = "127.0.0.1"
port = 8080
public_url = "https://bbs.example.org"
trusted_proxies = ["127.0.0.1", "::1"]
```

Point the HTTPS proxy at that local port, forwarding WebSocket upgrades as
well as ordinary requests. Set its upload body limit at least 1 MiB above
NetBBS's upload cap (**Settings → Limits & retention**, 100 MiB by default):
a browser upload carries form framing on top of the file.
Use a real hostname and certificate;
`bbs.example.org` is a placeholder. Restart after changing listener settings.

If you don't run a web server yet, [Caddy](https://caddyserver.com/) is the
shortest route: it obtains and renews the certificate itself, forwards
WebSocket upgrades, and sets no upload limit of its own. The hostname must
already point at this host, with ports 80 and 443 reachable from outside.
Its whole `Caddyfile` is below. Debian and Ubuntu packages read
`/etc/caddy/Caddyfile`; on NetBSD, pkgsrc's Caddy reads
`/usr/pkg/etc/caddy/Caddyfile`.

```
bbs.example.org {
    reverse_proxy 127.0.0.1:8080
}
```

With nginx, inside the `server` block that already holds your certificate:

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 1h;
    client_max_body_size 101m;
}
```

`proxy_read_timeout` keeps nginx from cutting off a browser caller who sits
idle for a minute, and `client_max_body_size` follows the upload cap.

With Apache httpd 2.4.47 or later, with `mod_proxy` and `mod_proxy_http`
loaded, inside the `VirtualHost` that already holds your certificate:

```apache
ProxyPreserveHost On
ProxyPass "/" "http://127.0.0.1:8080/" upgrade=websocket timeout=1800
ProxyPassReverse "/" "http://127.0.0.1:8080/"
LimitRequestBody 105906176
```

NetBBS sends no WebSocket pings, so without `timeout=` Apache cuts off an idle
browser caller at its global `Timeout`. On NetBSD, one host refused every
backend connection with `AH00957 (22)Invalid argument` when it was set to
3600, while 600 and 1800 work; stay at 1800 or below there.
`LimitRequestBody` is in bytes, 101 MiB here.

**Each browser caller's own address.** Behind any of these proxies, every
browser caller reaches NetBBS from the proxy's address. `trusted_proxies`
tells NetBBS which addresses are your proxies: for a connection from one of
them, NetBBS takes the caller's address from the `X-Forwarded-For` header the
proxy adds (Caddy and Apache add it by default; the nginx example above has
the line). Without it, all web callers share one address: one caller
mistyping a password, or one script guessing, soon throttles **every** web
login on the node, and the SysOp screens cannot tell browser callers apart.
List only proxies you run. A caller can write anything into the header, so
NetBBS reads it only from a connection whose own address is listed, and then
only the entry your proxy added. Entries are IP addresses or networks such as
`10.0.0.0/8`, never hostnames.

`public_url` supplies the externally reachable base address for transfer links.
Without it, a node bound to a wildcard or loopback address cannot give remote
terminal callers a usable link. A specific reachable bind address may be used
as a fallback, but explicit configuration is preferable behind a proxy. When
the web listener is on and SSH or Telnet callers would get no link, the node
log warns at every start.

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
Helpers you give staff permissions get a smaller **[S]taff** console instead
([Sharing the work](#sharing-the-work-staff-permissions)).

| Area | Purpose |
| --- | --- |
| Users | Accounts, registration, levels, approval, staff permissions, identity-verifier grants |
| Content | Message boards, file areas, chat channels, Communities, doors, moderation |
| Operations | Sessions, maintenance, audit log, backups, Link diagnostics |
| Settings | Branding, timestamps, node name, network participation, update checks, limits and retention |

**A first day, in order.** Most first-time SysOps get furthest by doing these
before telling anyone the address:

1. Choose who may sign up under **Users → Registration**
   ([Accounts](#accounts-permissions-and-identity)).
2. Decide what, if anything, levels will mean on your node
   ([Levels](#levels)). Everyone starts at 0, which is enough for a club
   where every member sees everything.
3. Create a few message boards, a chat channel and a file area under
   **Content**. Add Communities only if your node has separate topics, and
   categories only once a list gets long
   ([Content and Communities](#content-and-communities)).
4. Give the node its look under **Settings** and
   [Mastheads & banners](#custom-banners-and-mastheads).
5. Set up backups ([Create and verify a backup](#create-and-verify-a-backup)).
6. When someone offers to help, give them staff permissions or a moderator
   grant, not level 255
   ([Sharing the work](#sharing-the-work-staff-permissions)).
7. Join NetBBS Link later, if at all, once the node works on its own
   ([Join and share](#join-and-share)).

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
- **Approval required:** approve a pending account from its detail screen,
  or turn it down there with **Decline**, which removes it after a yes/no.
  Until you approve it, the account cannot log in at all, not even to look
  around; when the caller tries, they are told it is waiting for your
  approval. **Question** sets an optional question new callers answer
  when they sign up ("What do you write with?"). The answer shows on the
  pending account and is deleted once you approve it.
- **Closed:** only SysOps create accounts.

A caller cannot register a reserved name (`sysop`, `admin`, `root`,
`moderator`, `guest` and a few more), a name containing "sysop", or a name
that reads like a SysOp's own (`lnkwell` for `InkWell`). You can still create
such an account yourself under **Users → Create user**.

### Levels

Every account has a level from 0 to 255. A new account starts at **0**,
whether a caller signed up or you created it, and approving a pending account
leaves it at 0. **255** is SysOp: the whole console and power over every
account. Levels 1 to 254 grant nothing by themselves. They matter only where
you set a minimum, and every minimum starts at 0:

- a message board's or file area's **Min read level** and **Min write
  level**, which a Community can supply as defaults;
- a chat channel's single **Min level**, to join it;
- a door's **Min play level**;
- node-wide minimums under **Settings**, such as the Mail level and the Node
  map level.

A channel's and a door's level are always set on the channel or door itself,
never inherited from a Community. Raise an account's level from its detail
screen under **Users**.

A small club rarely needs more than this:

| Level | Who | Example use |
| --- | --- | --- |
| 0 | Everyone, including new callers | General boards, file areas, chat channels and games, every level left at 0 |
| 10 | Members you know | A members' lounge board with read and write level 10, and a members' channel with Min level 10 |
| 255 | You | An announcements board with read level 0 and write level 255 |

Be careful with 255 as a minimum. An announcements board with write level
255 shuts out everyone who is not a full SysOp, including a helper you trust
later. Do not raise that helper to 255, which hands them the whole node,
your own account included. Give them a **Read and post** grant on that board
instead ([Content and Communities](#content-and-communities)), and staff
permissions for account work ([Sharing the work](#sharing-the-work-staff-permissions)).

NetBBS refuses an account change that would leave no enabled, approved
SysOp. Disabling an account revokes its access; deletion is permanent and
requires its exact name. Existing content retains its recorded author label.

### Account lifecycle

A disabled account, and a signup still awaiting approval, receive no mail:
callers here are told so at the To prompt, and Link mail bounces with "that
account is not taking mail at the moment", which does not tell the other node
the account is disabled. Mail already in a disabled account stays there until
you enable it again. Deleting an account removes its own mail, but letters it
sent or received stay in the other person's Sent or Inbox; their Sent shows
the deleted recipient as, for example, `bob (deleted account)`.

A level change, a granted or revoked identity-verifier permission, or a change
to staff permissions reaches a caller who is already logged in without them
reconnecting. This also applies to a change made with `python -m
netbbs.admin`, within a few seconds. A raised
level shows on their main menu. A lowered one takes them out of whatever they
were doing, a door game included, and back to the main menu with a line naming
their new level. Text they were composing is kept as a draft.

Once your node has run NetBBS Link, deleting an account also retires its
username: nobody can register it again. The one exception is a registration
you decline while it is still awaiting approval, unless someone on another
node had already sent it Link mail. On the Link a username is the
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

### Sharing the work: staff permissions

To hand someone the node's routine work without making them a second SysOp,
give them **staff permissions** instead of level 255. Open their account
under **Users** and choose **Staff**:

| Permission | What it allows |
| --- | --- |
| Approve accounts | Approve or decline signups waiting under approval-required registration |
| Manage accounts | Disable and re-enable accounts, reset passwords, set levels from 0 to 254 |
| Moderate everything | Act as moderator on every board, file area and chat channel, local and carried. It also lets them read and post on every board and file area whatever its level; age and verified-name gates still apply |

**Co-SysOp** on the same screen sets all three in one step. Every change asks
for confirmation first and is audit-logged, and you can remove any permission
on its own later. The account's level stays whatever it was.

A staff member never acts on a level-255 account or on another staff member,
so they cannot demote, disable or lock out you or each other, and they cannot
widen their own permissions. They cannot raise anyone to 255, delete an
account, grant staff permissions or moderator grants, or reach Settings, Link,
Node, DNS or backups.

The **Privileges** group on an account's detail screen shows its staff
permissions, the identity-verifier permission, and every moderator grant it
holds, such as `board "News": approve`.

A staff member sees **[S]taff** on their main menu instead of [S]ysOp. It
opens a reduced console that counts what waits for them and offers only what
their permissions reach: **Accounts waiting** with approve accounts,
**Users** with manage accounts, and **Moderation** when they moderate
anything. These are your own screens with fewer actions: a staff member can
open your account or another staff member's, but only to look.

Whoever can approve accounts, you included, is told on the main menu when
signups are waiting ("2 accounts awaiting approval"). A moderator who may
approve posts or uploads anywhere sees **Moderation (n)** on the main menu,
with how many wait, and it opens one queue across every board and file area
their grants cover. To make someone a moderator of everything local in one
step, grant a moderator with the scope **blanket across everything**: it
writes the board, file area and channel grants together.

Members find everyone who runs the node under **Staff list** on their main
menu: SysOps, staff members, and moderators with what they look after, each
with the date of their last session. Being listed is the point, so a
member's choice to stay off Previous callers does not hide them here. Guests
don't see the list.

Going away for a while? Choose **Away** on your console's landing screen (it
is on the Staff console too) and leave one short line, such as "At a pen
show", with the date you expect to be back, or none if you don't know. The
Staff list shows it beside your name. When everyone who can approve accounts
is away, a caller waiting for approval is told who is expected back first.
A notice with a date ends by itself after that day; one without a date stays
until you end it, reads "away since" the day you set it, and your landing
screen reminds you of it each time. Being away changes nobody's permissions.

**Moderator, staff member or second SysOp?** Pick the smallest that does the
job:

- A **moderator grant** (**Content → Grant moderator**) covers one board,
  file area or chat channel, or, with a blanket scope, every one of a kind in
  a Community or on the node. On boards and file areas it can allow editing,
  deleting and approving what callers post or upload; in a channel, changing
  the topic, muting or banning, and managing an invite-only channel's
  members. It opens no console, no accounts and no settings.
- **Staff permissions** add account work: approving signups, and with
  **Manage accounts**, disabling accounts, resetting passwords and setting
  levels up to 254. A staff member can never touch your account.
- A **second SysOp at level 255** can do everything you can, to your account
  too: demote it, disable it, reset its password or delete it. The only
  guard is that NetBBS never leaves the node without an enabled, approved
  SysOp. Make someone a second SysOp only if you would hand them the node.

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
  Posts and letters can point at files in your areas, and readers download
  them from here: deleting or expiring a file turns those into "no longer
  available", and raising an area's read level hides the file's and area's
  names from readers it now shuts out.
- **Order of boards and file areas:** callers' lists follow your order, not
  the latest activity, so the number a caller remembers keeps its board. A
  new board or area goes last. **Up** and **Down** on its screen move it among
  the others in the same category and Community; pinned ones stay first and
  move among themselves. **Remove** deletes it. A caller can still sort a list by
  activity with **[O]rder**.
- **Chat channels:** set join gates, visibility, invitations, and moderation.
  Hidden or invite-only channels need more than a sufficient account level.
- **Communities:** group related resources and provide inherited defaults.
  Inspect effective gates on child resources before changing a shared default.
  A Community is not an automatic grant to every resource inside it.
  Callers see Communities in your order: a new one goes last, and **Up** and
  **Down** on its screen move it. **Remove** deletes it; its resources stay,
  without a Community.
- **Doors:** attach registered games to a Community, or to none.

**Where callers find it.** The main menu's **Message boards**, **Chat** and
**Files** list everything of that kind on the node, whichever Community it
belongs to; a resource with no Community is simply listed there, so you do
not need a Community to make a board easy to find. **Games** appears for a
caller once a registered door is open to their level. **Communities** (key **O**) appears once one exists and
shows each Community's description and what it holds; give each a
description, because that is what callers read first.

**Categories and Communities are different things.** A category groups the
list of one kind (board categories group boards) and shows up as a folder in
that list. A Community is a topic that holds every kind at once. A resource
can have both. Create categories under **Content → Categories** and pick one
in a board's, area's or channel's **Category** field; Communities are under
**Content → Communities**. Ctrl-H on the Categories screen says the same. A
node with a handful of boards needs neither.

Until the node has a board, chat channel or file area, your own main menu
shows where to create one.

Moderator grants belong to a resource or Community. Membership alone does not
make someone a moderator. Grant only the scope required; use approval queues
and the audit log to review actions. When a moderator rejects a post, its
author gets a mail from **System** -- the BBS, not the moderator's own
account -- with the reason, who decided, and the text; it cannot be
replied to. Such a notice counts toward the author's mailbox limit but is
the first read mail removed to make room. Chat moderation commands are used inside
the channel by someone with the appropriate authority.

A read or write grant on a board or file area lets its holder past that
resource's minimum read or write level. To let a helper post on an
announcements board whose write level is 255, grant them the **Read and post**
preset on that board rather than raising their level (**Read only** opens
reading alone). A grant opens only what its scope covers: one board or area,
or, with a blanket scope, every board or area of that kind on the node or in
one Community, so pick the scope with care. The minimum age and
verified-name requirements still apply.

The [user handbook](NetBBS-User-Handbook.md) covers posting, drafts, follows,
search, mail, and everyday chat. **New scan** and **Find** only show content the
caller can access on this node, including carried linked content.

## Door games

Open **Content → Doors → Gallery** to register Retro Trivia, Voidrunner, or
War Dialer. Gallery entries fill a draft with the installed interpreter and
sensible defaults; review and Save. Callers reach games through **Games** on
the main menu or a Community's page.

Third-party native programs, DOS games through DOSBox-X, doors built only for
another platform (in a per-caller qemu VM you provision), remote RLogin
services and BBSLink use compatibility profiles. Start with minimum play level 255,
choose a template, set your actual paths, run **Check setup**, then **Test as
SysOp**. Testing launches real programs and may change game data, even if you
later discard the configuration draft. Test normal quit, disconnect, timeout,
and each offered caller transport before opening it to users.

Your own door scripts can be sent from inside NetBBS: **Content → Doors →
Upload** asks for the file's name, then takes one file over Zmodem or a
browser link into the node's doors folder, the one **From disk** lists.
Replacing a file of the same name asks first. The upload registers nothing and
changes no permissions; register it with **From disk**. It is capped at the
node's upload limit and recorded in the audit log.

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

A War Dialer door's detail screen adds **[W]orld**: the world's status and recent
SysOp operations, a maintenance switch that closes it to new callers, and, on a
live node with maintenance on, **[N]ext season** and **Reset [c]ompetition**. Both
take a verified node backup first and ask for a reason and the world's filename.
The [door guide](NetBBS-door-guide.md) has the details and the stopped-node CLI.

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

Create a local resource before promoting it to linked scope. Linked boards,
channels and file areas from other nodes are carried automatically up to the
`max_carried_boards` / `max_carried_channels` / `max_carried_file_areas` caps
(500 each; **Settings → Network & login limits → Carry caps**). Past a cap, a new one is not lost: it waits under **Link status →
Offered**, with its origin and why, until you **Accept** it (not limited by the
cap) or **Exclude** it. Set a cap to 0 to carry only what you accept. Lowering a
cap removes nothing already carried. Deleting a carried resource that another
node originated excludes it without destroying it: callers stop seeing it and
this node stops carrying it, but everything in it -- your users' posts, your
moderation -- is kept. **Link status → Excluded** lists it: **Restore** brings it
back exactly as it was (what arrived meanwhile follows with the next sync), and
**Purge** deletes it for good. Its name stays taken until you do one or the
other. Deleting a board, channel or file area this node originated is still a
real delete. Link status shows `carried/cap` for all three kinds and how many are
offered and excluded.

There is nothing to subscribe to: what an established peer shares arrives on
its own, within the caps. A node's screen under **Link status → Peers** lists
what this node already carries from it and, while it is on probation here,
what it offers that is being held back. Carrying a message board creates a
local browsable copy; file catalogues do not automatically download all file
contents. Carried boards, channels and file areas appear in callers' **Message
boards**, **Chat** and **Files** lists, outside any Community, because Link
Communities do not exist; edit one and set its **Community** to put it in one
of yours. Ask the other SysOp to verify both sides when first testing
publication. Hello/discovery alone does not prove content arrived.

Linking one of your own boards, channels or file areas sends it to the peers
you have established, on a later sync pass. Its **NetBBS Link** rows then show,
for each peer, whether it holds it, refused it because your node is on
probation there, or is not sent it while it is on probation here. A node
learns this only from peers it dials itself, so a peer that only dials yours
reads "not known". **Fork of** is for a board that carries on another node's
board under yours, for example after that one was closed; leave it empty for
a board of your own.

Asynchronous delivery can continue after a peer reconnects. Live chat and
private messages require a working live session; a failed live message is not
silently converted to mail. Link mail is encrypted to the recipient's home
node for ordinary accounts; the home-node operator can read it.

Link mail follows node trust. Once you establish a node -- **Establish** on
its screen under **Link status → Peers**, or under **Settings → Policy trust →
Subjects** -- mail from all its callers is delivered here, even from callers still on probation;
their posts still wait in the approval queue. Mail from a node you have not
established yet, or from a caller or node you quarantined or blocked, is
refused and bounced back to its sender. The same holds the other way: your
callers cannot address mail to a node you have not established, and are told
so as they type the address. An address is written as its name is displayed,
capitals included. An account from before the username rules whose name has
a space or other punctuation cannot send Link mail, because no reply could
reach it; rename it if its owner needs to. Mail already waiting in the **Outbox** for a
node goes out on the next Link pass after you establish it, and expires if
the node is not established before its retries run out.

**Operations → Mail** shows the other side: mail other nodes sent here that
this node refused, and how full your callers' mailboxes are.

- **Refused Link mail** lists each refused letter with its sender, the reason
  in plain words (its node is still on probation here, the sender or node is
  blocked, no such account, a mailbox full of unread mail, ...), when it was
  last refused and how many times its sender tried. **Open** one to see its
  node's and its sender's trust here, and **Node trust** or **User trust**
  takes you to that subject's trust screen to establish or block it. A letter
  refused because its node is on probation is the common case: establish the
  node, and the sender's next attempt is delivered. The list keeps the 500 most
  recent refusals, and no more than 50 from any one node.
- **Mailboxes** lists every account with mail, fullest first (**Order**
  switches to by name): letters, unread, read, kept (in the caller's Kept
  folder), notices from the BBS itself, and how much of the 500-letter cap
  the Inbox takes. Kept letters are part of Letters but not of the cap: Kept
  has its own limit of 100 per caller, and the Kept column is flagged when
  one is full (the caller cannot keep another until they move some back or
  delete some). So each caller holds at most 600 letters. A full inbox makes
  room by dropping its oldest read letter; an inbox full of unread mail
  refuses new mail, and a Link sender gets a "mailbox full" bounce. Callers
  see the same count in their own Inbox header, a warning from 450, and a
  main-menu line when old read mail was removed to make room.
- In **Refused Link mail**, "the recipient's account is disabled or still
  awaiting approval" is mail for an account that takes none at the moment.
- **[W]rite to all callers** sends one letter to every account on this BBS
  that takes mail, from your own account: it is signed like your other mail,
  sits in your **Sent** as one letter, and callers can reply to you.
  **[N]otice to all callers** sends it from the BBS itself, as **System**:
  unsigned, not in your Sent, and no one can reply to it; it reaches your own
  Inbox too. Both open the ordinary compose screens -- Subject, your editor,
  the review screen -- and keep a draft of their own. The review screen says
  how many accounts it will reach. The guest account, disabled accounts and
  signups awaiting approval are left out, and so is anyone whose mailbox is
  full of unread mail: each caller gets their own copy, under the
  same 500-letter cap as any letter. After Send you are told how many callers
  it reached, whose mailbox turned it away (by name), and how many accounts
  were left out. It is local only: callers on linked BBSes are not written
  to. A letter already sent is never sent twice, even if you send its draft
  again after a dropped connection. **[A]ttach file** on the review screen
  points it at files in your file areas, as any letter can; a caller who may
  not open one of those file areas is skipped, and named after Send, like a
  full mailbox.

These screens never show what a letter says. Mail is private: you see counts,
account names, senders and reasons, never a subject or a body, and a refused
letter does not even record whom it was for. Keep in mind that Link mail is
encrypted to the recipient's *node*, not to the person, so a SysOp with access
to the database could read it there; NetBBS gives you no screen for doing so.

Your callers see each Link message's state in their **Sent** mail: pending,
with relay, delivered, bounced (with the other node's reason in plain words)
or expired. A caller whose mail bounces or expires is told once at their next
main menu.

"With relay" is mail your node left at a relay because the recipient's node
cannot be dialed directly. The relay is not the recipient, and one refusal
never comes back as a bounce: mail for a node that has *your* node
quarantined or blocked, which sends yours nothing. So if no answer comes back
within 14 days of the handoff, the letter expires and its sender is told that
no answer came back, so it may not have arrived. An answer that arrives later
still counts: the letter turns delivered, or bounced. A relay keeps a letter
well past those 14 days, so a recipient that is only slow to collect still
answers in time. Replaying an expired delivery from the **Outbox** puts it
back to pending.

Mail arriving here is checked the way your own callers' mail is: a sender
name that is not a valid address, a blank or oversized subject, or an
oversized body is bounced as malformed, and a letter this node cannot decrypt
is bounced as such rather than as "no such user". A received letter shows the
date its sender wrote it, unless that date is before 2000 or more than five minutes ahead of
your clock, when it shows the arrival time instead. The inbox lists mail in
the order it arrived, so a letter that took days to get here is still at the
top.

### Behind an HTTP proxy

If your node's only way out is an HTTP proxy, set the standard variables in
the environment NetBBS starts in (the service unit, rc.d script, or shell):

```sh
HTTP_PROXY=http://proxy.example:3128
HTTPS_PROXY=http://proxy.example:3128
NO_PROXY=localhost,127.0.0.1
```

Boards, mail and files use them as ordinary HTTP. Live chat uses the same
proxy, as a `CONNECT` tunnel, and runs its encrypted session through it
unchanged; the proxy sees which address is dialled, not what is said. When a
proxy is set it is the only way live chat goes out: there is no direct attempt
first. List peers the proxy should not carry in `NO_PROXY`.

- **Proxy login.** A username and password in the proxy URL
  (`http://user:password@proxy.example:3128`), or a `~/.netrc` entry for the
  proxy host, are sent as Basic authentication. NTLM and Kerberos proxies are
  not supported directly; run a local authenticating proxy such as CNTLM or
  px, and point the variables at it on `127.0.0.1`.
- **Only `http://` proxy URLs.** A SOCKS or `https://` proxy URL makes live
  connections fail with a stated reason rather than go direct.
- **Proxies that inspect TLS** (SSL inspection, Squid `ssl_bump`) cannot carry
  live chat, which is not TLS. Boards and mail still work.
- **A managed name cannot be kept behind a proxy:** its check-ins always
  connect directly, because the service publishes the address they come from.

**Link status** shows a **Live proxy** line: the proxy and the last outcome,
for example `tunnel refused: 407 Proxy Authentication Required`, or
`tunnel opened, handshake failed` for an inspecting proxy. A change of outcome
is also written to the log once.

**MANUAL — outside NetBBS, for reliable-node operators:** many corporate
proxies allow `CONNECT` only to port 443. To be reachable live from such
networks, advertise the real-time port as 443 and forward it to the real-time
listener:

```toml
[link]
realtime_port = 8862
realtime_advertised_port = 443
```

NetBBS does not bind 443 itself. If 443 on that address already serves HTTPS,
use a second address, or a protocol demultiplexer in front of both.

### Trust and recovery

Use **Link status** for peers and relay state, **Outbox** for pending or failed
work, and **Diagnostics / Follow log** for explanations.

Every node starts on probation with every other, in both directions. A peer
on probation here sends nothing this node accepts, and this node sends it
nothing of yours; your node is on probation at each peer the same way until
that peer's SysOp establishes it. Automatic graduation takes at least 30 days,
three days of contact and vouches from two trust domains, so on a node with no
trusted reporters only **Establish** ends it. Establish a peer once you know who
runs it, and ask its SysOp to establish yours. **Link status** counts the peers
on probation here and says whether the peers your node dials still hold yours
on probation; the SysOp console's LINK line counts them too.

**Link status → Peers** is the node map: every node this one knows, as callers
see it under **Directory → Node map** ("Nodes known to" your board), plus what
callers do not see. A node on probation here says when probation could end
by itself and what is still missing; its **Exchange** rows say what is held
back from it and whether it takes what yours sends. Peer-list candidates are marked unverified and "never heard
from"; nodes you quarantine or block in any trust dimension are marked, with
each dimension's state, because callers do not see them at all. Each node's
screen adds its Link addresses, relay roles and reliability. Last heard is your
own last contact with the node, or the time its newest descriptor says it was
signed, never later than when you first stored it; a node not heard of for 30
days is marked stale, not removed.

**Link status** also shows what your node holds as a relay. When it relays
for outgoing-only nodes, mail and delivery answers for them wait here until
they dial in and collect them. The **Relay mailbox** line counts what is held,
and a table below it lists each node held for, with how many envelopes it has
(at most 50) and how long the oldest has waited, oldest first. Anything left
uncollected for 30 days is dropped on the next sync pass, and the diagnostic
log gets a warning naming the node and how many went. Neither end is told by
your node, which cannot read or sign that mail; the sender's own node gives up
on a letter handed to a relay after 14 days and tells its writer that no
answer came back. A node whose count stays at 50 for weeks is most likely not
coming back; the time limit clears it without you doing anything.

A node's screen also acts on its trust, when it is a trust subject here (every
node that has exchanged a hello with yours, or been introduced to it; not a
peer-list candidate): **Establish** and **Block** open the override editor with
all three dimensions and the state already chosen, so you only give a reason
and save; **Clear override** removes one override, or all of them at once;
**Trust details** opens the node's full trust screen from Policy trust. The same
**Establish** and **Block** are on every subject's screen under **Settings →
Policy trust → Subjects**, and **Override**'s **Dimension** offers **All
three**.

**Link status → Dial-in** sets the addresses other nodes show callers on their
node maps: up to four, each `telnet://host:port`, `ssh://host:port` or an
`https://` URL (plain `http://` is refused). **Use suggestions** fills the
draft from your DNS name, your enabled Telnet and SSH listeners and an
`https://` `[web] public_url`; nothing is saved until **Save**, and peers get
the list with the next hello. Until you save a list, the node publishes
`[web] public_url` when it is `https://` and the web listener was on at the
last start. Saving an empty list publishes nothing. The **Dial-in** row on
Link status shows what is published now and where it came from.

Policy trust settings
separate identity integrity, resource behavior, and content conduct: a complaint
about content should not be treated as proof of a forged identity.

Under **Settings → Policy trust**, inspect a subject's effective state and
history before applying an override. Domains, anchors, and reporters determine
whose signals count; multiple identities from one trust domain do not become
independent votes. Quarantine restricts exchange; it does not erase previously
accepted content. A sole-authority exception deliberately weakens the usual
multi-source policy and is marked as a safety deviation.

Your node will carry content from nodes it has never spoken to. Two nodes
that share a board through the same seed usually never contact each other, and
two outgoing-only nodes cannot. Your node learns who such an author's node is
from the node that carried the post, checks that identity for itself, and
lists it under **Settings → Policy trust → Subjects**, where its screen says
which node it was learned from. Like every new identity it starts on
probation, so its content is withheld until you set both its identity
integrity and its resource behavior to established with **Override**. Its
callers are subjects of their own and start on probation too, so their first
posts then arrive in the board's approval queue. If a board looks quieter
than you expect, look in both places. Such a node never leaves probation by
itself, because your node has no direct dealings with it to judge. A name you
recognise under a different technical identity is flagged, and when the name
belongs to a node yours has met, the warning falls on the other one.

If nobody can dial your node, which is the ordinary case behind a home
connection, what it vouches for is handed to the nodes that relay for it and
fetched from there; the vouch screen says how many those are, warns when
there are none, in which case a vouch reaches nobody yet, and warns when one
of them did not take your vouches at the last attempt, which the Link log
explains. A node that names
yours a trusted reporter needs to have met one of your relays. Verified ages
and names are not carried this way: on a node nobody can dial, **Published
identity** and each caller's Profile say that nothing is delivered.

To stand behind an identity for the benefit of other nodes, open it under
**Settings → Policy trust → Subjects** and choose **Vouch**. You give a reason,
which is published with the vouch, and confirm once. Your node signs the vouch
and serves it to nodes that have named yours a trusted reporter, where it
counts toward ending that identity's probation. It changes nothing on your own
node; to establish an identity locally, use **Override**. The other side of
that: naming a node a trusted reporter is not enough for its vouches to reach
you. Your node pulls and counts a reporter only once that reporter is
established here, by override or by graduating from probation. **Policy trust →
Vouches** lists everything your node vouches for and withdraws any of it.
Vouches renew themselves. If you quarantine or block an identity, your node
stops vouching for it on its own and resumes if you lift the restriction. Your
node cannot vouch for itself or for its own callers.

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

Registering sends the first check-in at once and tells you if it failed. From
then on the DNS screen shows **Last contact** (or *never*) and, whenever the
node's latest check-in did not get through, why. Check-ins always connect to
the service directly, never through an HTTP proxy, because the service
publishes the address they arrive from; a node whose only way out is a proxy
can register a name but cannot keep one.

Callers reach a managed name on the standard ports (SSH 22, Telnet 23, HTTPS
443); the record itself carries no port. NetBBS listens on 2222/2323/8080 by
default, so a bare `myboard.netbbs.org` reaches your board only through a
port-forward or proxy in front of it. The DNS screen states this against your
configured listeners; the service cannot check it for you.

If you also run the managed-DNS service itself, `[managed_dns] admin_token` in
`netbbs.toml` adds **[A]dminister service** to the DNS screen: the service's
registrations as a table, each in full, and revocation with a reason and a
typed-name confirmation. `services/managed_dns/README.md` §8 is the runbook.

### Node keys

Your node's **Technical identity** is its root key and never changes. Day to
day it signs with an operational *signing key* and connects live with a
*transport key*, and either can be replaced without changing the node's
address or reputation. **Link status → Keys** shows both keys and their
history. **Signing key** and **Transport key** each offer two ways to
replace the key:

- **Rotate** retires the old key. Everything it signed stays valid, so peers
  notice nothing except that new content carries the new key. Use this for
  routine replacement.
- **Compromised** is for a key you believe someone else holds. Peers stop
  trusting anything that key ever signed, and your node signs its own boards,
  posts, files and mail again under the new key. Nodes that already copied
  your content keep their copies. A node fetching your older content from one
  of them skips it and gets it from yours instead.

Replacing the transport key ends every live chat session at once. Peers
reconnect on their own, but a caller watching a channel linked from another
node is told the live link dropped and gets it back by re-entering the
channel. To act with the node stopped, run `python -m
netbbs.admin rotate-key signing` (or `transport`), adding `--compromised`
where it applies and `--identity-dir` if yours is not the default. It refuses
while the node is running. Back up the node after a rotation: an older backup
restores the old key.

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

Check the dashboard's pending registrations, posts and files, backup
recency, free disk space, and recent errors. Staff and moderators see what
waits for them under **Moderation (n)** and on the Staff console, so a queue
can empty while you are away. Choose welcome/masthead/banner presets through Settings;
preview before applying, or place your own files as described under
[Custom banners and mastheads](#custom-banners-and-mastheads). Timestamp format and display timezone are node-wide.
Until you choose a timezone under **Settings → Timestamp format**, times are
shown in UTC: the main-menu clock says `UTC`, and the node log reminds you at
every start. The default welcome banner mentions NetBBS Link only when the
node ran with Link on at its last start.

**Settings → Limits & retention** holds six node-wide values, saved together
and applied without a restart:

| Setting | Default | Effect |
| --- | --- | --- |
| Upload cap | 100 MiB | Largest single upload, over Zmodem and the browser. Keep a reverse proxy's body limit at least 1 MiB higher. |
| Grace before deletion | 7 days | How long an expired post or file waits before it is deleted. An expired file can be recovered from its area until then. |
| Invitation expiry | 7 days | When an unaccepted channel invitation lapses. Clear the field for invitations that never expire. |
| Chat scrollback | 100 messages | Lines each channel keeps, carried Link channels included. Lowering it trims a channel the next time someone speaks there. |
| Node map level | 0 | The lowest level that may open **Directory → Node map**. A guest is an ordinary account: set this above the guest account's level to keep the map from guests. |
| Mail level | 0 | The lowest level that may open **E-mail**: read, write and reply, here and to other BBSes. Below it the main menu offers no E-mail. Mail sent to an account below it still arrives and waits until you raise the account's level. |

The guest account (**Settings → Guest access**) never has mail, whatever its
level and the mail level: every guest signs in as the same account, so its
inbox would be shared by strangers and its letters sent under one name. A
caller who writes to it is told the account has no mailbox; Link mail to it
bounces, and the sender is told the account takes no mail. If the guest
account has mail from before this rule, it stays in the database, unreadable
by guests; turn guest login off and sign in as the account to read or delete
it.

Under **Operations → Node and sessions**:

- **Monitor** is a live table of everyone connected, refreshed every two
  seconds:
  - It shows each caller's transport, address, time on, idle time, terminal
    size and where they are, for example "Boards › Retro" or
    "Doors › Voidrunner".
  - Up/Down selects a caller. **Snoop** shows their screen live, as they see
    it, until you press any key. The caller is not told at the time. Chat's
    `/help` and the User Handbook say that you can do this, and the node log
    records every snoop: who watched whom, and for how long.
    **Chat** breaks into their session for a two-pane chat: you type in the
    top pane, they type in the bottom one, and Esc ends it. Their screen is
    then repainted exactly as they left it, with anything it printed during
    the chat and any half-typed line included, and they carry on. It is
    refused during a file transfer. For a caller in a door you are warned
    first, because the door keeps running while they chat. The node log
    records every chat.
    **Message** sends them a line. **Kick** opens
    the same disconnect screen as **Who**: an optional message, then
    **Disconnect**, which is recorded in the audit log. **Unwind** sends them
    back to the main menu.
  - **Order** sorts by time on, idle time or name; a mark in the heading
    shows which. Below the table are the
    most recent logins, logoffs and disconnects. A message or broadcast sent
    to you while the Monitor is open appears on the line above the keys.
  - On a terminal narrower than 80 columns, the address, terminal size and
    transport columns are left out, in that order.
- **Who** lists sessions and disconnects one.
- **Maintenance** blocks new non-SysOp logins.
- **Drain** warns and disconnects non-SysOp sessions after a delay.
- **Lock & drain** combines both for planned work.
- **Shutdown** stops the node after warning callers.

These controls require a live node session. For a stopped node, use the host's
service controls. Use **Audit log** to see administrative and moderation
activity, and **Node log** to read the newest part of `netbbs.log`: warnings and
errors first, **Level** to show errors only or everything, **Follow** to watch
new lines. Storage garbage collection and draft pruning show the proposed work
before confirmation; review it instead of deleting files directly.

**Operations → Search indexes** compares what **Find** searches with the posts,
files, chat messages and mail themselves, and shows how many entries are
missing, stale or left over: counts only, never a letter's words. **Rebuild** replaces the indexes from that content; it
cannot lose any. It also works from `python -m netbbs.admin`, and
`python -m netbbs.search check|rebuild --db PATH` does the same from a script.

### Custom banners and mastheads

**Settings → Mastheads & banners** holds eight optional pieces of caller-facing
art. **Banners** are the welcome greeting, the log-off screen, and the screens
shown before and after self-service signup. **Mastheads** sit above the main
menu, the message-board list, the file-area list, and the chat channel picker.
Each has a gallery of bundled samples, **From disk** loads a file you have
already put on the node, and **Upload** sends one from your own computer. You
can also put your own `.ans` file where the node looks for it.

**Upload** needs the same route a caller's file upload does: a terminal that
speaks Zmodem, NetBBS's own browser terminal (which needs no `public_url`), or,
from any other terminal, a single-use browser link, which needs the web listener
and its `public_url`. Whatever you send is saved as that piece's own file, whatever it
was called on your side, up to 256 KiB. Uploading over an existing file asks
first; if that piece is enabled, callers see the new art at once. An upload
never enables a piece by itself, and it is recorded in the audit log.

The file goes beside the database and is named after the database file, minus
`.db`, plus a suffix for the piece. A node whose `[database] path` is
`EmptinessMachine.db` uses:

| Piece | File |
| --- | --- |
| Welcome banner | `EmptinessMachine_welcome_banner.ans` |
| Log-off banner | `EmptinessMachine_logoff_banner.ans` |
| Before-signup banner | `EmptinessMachine_new_account_banner_before.ans` |
| After-signup banner | `EmptinessMachine_new_account_banner_after.ans` |
| Main-menu masthead | `EmptinessMachine_main_menu_banner.ans` |
| Message-board masthead | `EmptinessMachine_board_list_banner.ans` |
| File-area masthead | `EmptinessMachine_file_area_banner.ans` |
| Chat-channel masthead | `EmptinessMachine_chat_channel_picker_banner.ans` |

For the `netbbs.db` in this handbook's examples, the prefix is `netbbs_`. Press
**Ctrl-H** on a piece's own screen to see the exact path for your node.

Placing the file does not turn it on. Each piece has its own switch, off by
default, and callers keep seeing the built-in default until you open that
piece's screen under **Settings → Mastheads & banners** and choose **Enable**.
Its status line shows `disabled -- file: <name> (N bytes)` until you do.
**Preview** shows your saved art even while it is switched off, and says under
it what callers see meanwhile; with nothing saved it says that too. Enable
refuses a missing file or one over 256 KiB. If a file that was enabled later
goes missing or grows past that limit, callers get the default silently and the
node logs a warning.

Empty rows at the bottom of a piece are not sent, so a banner drawn in the top
seven rows of the 24-row editor takes seven rows on a caller's screen. Empty
rows between parts of the art are kept.

**Edit** opens the art editor on an 80x24 canvas. Typing (a space too) paints
over whatever is at the cursor. At the end of a row the cursor stays put, so
press **Enter** for the next row; **End** goes to just after the row's last
character. Retyping a shorter line leaves the end of the old one in place:
**Ctrl+K** clears from the cursor to the end of the row. **Ctrl+T** picks a
block or line glyph, **Ctrl+P** and **Ctrl+B** the foreground and background
colour, **Ctrl+L** repaints the screen, **Ctrl+G** lists every key, **Ctrl+O**
saves, and **Ctrl+X** quits.

The welcome gallery ends with three quiet designs for clubs that don't want
neon: **Paper & Ink**, **Library Card** and **Garden Gate**.

Art from scene tools such as PabloDraw or Moebius works as it is:

- **SAUCE records** (the title, author and group a scene tool saves at the end
  of the file) are not shown to callers. Each piece's status shows the credit,
  the width the art was drawn for, and a warning if it was made for a font
  other than the IBM PC one; such art is still shown with CP437's characters.
  The editor writes a SAUCE record when it saves, keeping any credit the file
  had.
- **Art wider than a caller's screen** is not drawn for them when its SAUCE
  record says how wide it is: the welcome banner falls back to the default,
  the other pieces to no art. Without a record, NetBBS can't tell.
- **Smileys, suits, notes and arrows** drawn with CP437's low characters (☺ ♥
  ♫ ► ▲) reach every caller: as the original bytes on a classic terminal, as
  the same symbols on a modern one, and as plain stand-ins in ASCII.
- **iCE colours** (blink used as a bright background) show as bright
  backgrounds everywhere, not as blinking text.
- **Credit line:** on the welcome banner's screen, **Credit line** switches on
  a line under your banner crediting the art from its SAUCE record, for
  example `art: Nib Logo by InkWell/Quill`. It is off by default.

#### The main menu as your own art

The main-menu masthead can also be the menu itself. On its screen, **Mode**
switches between *above the menu* (the default) and *the menu itself*. In the
second mode your art is the whole main menu, and NetBBS draws each caller's own
items and a few live values into places you mark in it. You mark a place by
drawing a token in plain text, in the colour its content should take:

| Token | What goes there |
| --- | --- |
| `{menu 74x7}` | The caller's menu items, in a region 74 columns wide and 7 rows tall, starting at the `{`. Required. |
| `{prompt}` | The prompt. Without one, it goes below the art. |
| `{user 16}` | The caller's name, cut to 16 columns |
| `{node 30}` | Your node's name |
| `{level 9}` | `level 20` |
| `{mail 18}` | `3 unread` or `mail caught up` |
| `{time 5}`, `{date 10}` | The node's time and date, in its timezone |
| `{online 12}` | `4 online` |

Without a number, a field is as wide as its token. Brace text that is not one
of these stays part of the art.

The art only decorates. Each caller sees exactly the items the normal menu
would show them, with the same keys; the art can't add or hide any. A caller
gets the normal menu instead whenever the art can't be used as drawn:
- **their items don't fit** the `{menu}` region (a SysOp's menu is the longest);
- **the art has a problem**, such as no `{menu}` or two slots overlapping;
- **their terminal is too small**, narrower than the art or too short for it;
- **they read plain ASCII.**

**Check** lists the tokens it found and any problems, and says whether your menu
and a level-0 caller's fit, on a terminal the size of yours. **Preview** shows
the menu as you see it, then as a level-0 caller sees it. The gallery's
**Quill Ledger** and **Inkwell Blocks** samples are drawn this way, in 16
colours with characters classic terminals have; applying one switches the mode
for you.

The welcome and log-off banners take the field tokens too, but not `{menu}`
or `{prompt}`. The welcome banner is shown before anyone signs in, so it fills
only `{node}`, `{time}`, `{date}` and `{online}`. The log-off banner adds
`{user}` and `{level}`. Any other field is left blank, and SSH's sign-in banner
fills `{node}`, `{time}` and `{date}`. A banner without tokens is sent exactly
as before.

Every caller gets text in the character set their terminal reads:

- **Detection when they connect:**
  - A Telnet caller's terminal is asked what it is. SyncTERM and other classic
    BBS terminals get CP437, so your art arrives exactly as drawn.
  - Modern terminals get Unicode.
  - A Telnet client that doesn't say gets plain ASCII for everything before
    sign-in, your banner included: boxes become `+`, `-` and `|`.
  - SSH callers are detected from their terminal type, but the banner SSH shows
    during login is always ASCII.
  - The browser always gets Unicode.
  - The node log has one line per Telnet or SSH connection saying what the
    terminal called itself and which character set it got, for example
    `telnet caller 203.0.113.9 terminal type: 'syncterm' (answered); character
    set cp437 (certain)`. Look there if a caller's screen looks wrong.
  - Classic terminals such as SyncTERM jump to the next line as soon as they
    fill the last column. NetBBS lays out every screen one column narrower for
    them, so nothing gets double-spaced and the screen doesn't scroll when the
    bottom row is written. Your banners keep their full width. Plain spaces at
    the end of each banner row are dropped when it's shown, so draw background
    colours out to the edge if a row should reach it.
  - SyncTERM also gets full colour (truecolor), so doors and colourful
    presets look the way they do in a modern terminal. Other classic terminals
    get 256 colours. A caller can still pick a colour depth in their Profile.
- **After sign-in:**
  - Each caller's own **Profile → Unicode or CP437** choice applies.
  - A caller whose terminal wasn't recognised is asked once which of two sample
    lines looks right.

**Settings → Previous callers** cycles through three states: the panel after
login in its default neon style, the same panel plain (your header colour and
a quiet heading), and hidden.

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
| SSH host key and custom banners | Files beside the database that begin with its file name minus `.db`, e.g. `netbbs_ssh_host_key`, `netbbs_ssh_host_key_rsa` and `netbbs_welcome_banner.ans` (see [Custom banners and mastheads](#custom-banners-and-mastheads)) |
| War Dialer world | `/var/lib/netbbs/netbbs.db.doors/war-dialer.db`, unless overridden |
| Voidrunner careers | `/var/lib/netbbs/netbbs.db.doors/voidrunner/`, unless `VOIDRUNNER_SAVE_DIR` overrides it (a node upgraded from the old default copies the service account's `~/.netbbs/voidrunner_saves/` there at its first start) |
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
`netbbs_backups/` beside the database, or under the destination you set. Check
the reported path and game coverage.

**Backup → Schedule & destination** sets both without a cron job:

- **Frequency** off, daily or weekly, at a **Time** (24-hour, in the node's
  display timezone) and, for weekly, a **Weekday**. A node that was not running
  at that time makes one backup when it next starts.
- **Keep** the newest N scheduled backups (default 7). Older *scheduled* ones
  are deleted; backups you create yourself, and anything else in the folder,
  are never touched.
- **Destination**: an existing folder the node's account can write to, used by
  every backup. Empty means the default beside the database. A destination that
  disappears (an unmounted disk) makes the backup fail rather than write to the
  disk underneath.

The Backup screen and the dashboard show the next run, and the backup history
lists each scheduled run's outcome, including a skipped one and why. A failed
run is not retried until the next scheduled time. The running node makes the
backups; `python -m netbbs.admin` only changes the settings.

For a one-off destination or a script, use the installed backup CLI. Run it
as an account able to read all node state:

```sh
sudo -u netbbs /var/lib/netbbs/.venv/bin/python -m netbbs.backup create \
  --db /var/lib/netbbs/netbbs.db \
  --identity-dir /var/lib/netbbs/netbbs_identity \
  --to /var/lib/netbbs/backups/pre-upgrade-20260913
```

The destination must not already exist; choose a fresh name for each backup.

Voidrunner careers live beside the database, in `netbbs.db.doors/voidrunner/`,
so the CLI finds them from `--db` alone. The node also records the directory it
uses at startup, and the CLI reads that first, so a `VOIDRUNNER_SAVE_DIR`
override is followed too. They used to live under the home directory of
whichever account started the node, which a CLI run from your own shell does
not share: up to v7.4.0 that silently captured no careers at all
([#555](https://github.com/Thiesi/NetBBS/issues/555)). Two cases still need you:

- A node upgraded from that home-directory default that has not yet started
  since. The CLI says so in as many words -- `Voidrunner: NOT CAPTURED` or
  `GUESSED LOCATION` -- rather than reporting an empty directory as an empty
  node. Start the node once (it copies the careers into place), or pass
  `--voidrunner-save-dir` with the old directory. A node upgraded straight from
  v7.4.0 or earlier never recorded that directory, so it does **not** copy the
  careers: set `VOIDRUNNER_SAVE_DIR` to the old directory before its first start,
  or copy its contents into `netbbs.db.doors/voidrunner/` with the node stopped.
- A directory you chose yourself, when the node has not recorded it. Pass
  `--voidrunner-save-dir` with the path shown on the live Backup screen.

Read the coverage output either way. An absent Voidrunner component will not
magically reappear during restore.

War Dialer has separate world capture and restore rules; see the
[door guide](NetBBS-door-guide.md). Third-party installation directories are
excluded unless **Backup → Door installations** is enabled. The Backup screen
shows the door sections, and that option, only once a door is set up. That option copies
them without stopping their writers and does not automatically restore them.
Stop games/services first. A missing or unreadable requested installation fails
the backup. Symlinks are copied as links, not followed to external data.

**MANUAL — outside NetBBS:** copy completed backups off the machine, protect
them as secrets, and encrypt them if needed. Backups contain private keys and
account data. Off-site transfer is not built in, and retention covers only the
schedule's own backups on this machine.
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
  --voidrunner-to /var/lib/netbbs/netbbs.db.doors/voidrunner \
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
Careers restored into the node's own `netbbs.db.doors/voidrunner/` need no
setting. Anywhere else, set `VOIDRUNNER_SAVE_DIR` to that destination; restore
never changes that environment setting for you. Do not run the
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

### Installing from Settings → Update

When a check has found a newer release, **[I]nstall vX** appears on
**Settings → Update** on the live node. It first shows the plan, and does
nothing until you choose **[I]nstall now** and answer yes. Read the release
notes first, and stop games and companion services, as for any upgrade. The
steps run in order, and a failure stops the rest and says why:

1. Download the release's wheel from GitHub and check it against the SHA-256
   digest the release publishes. A wheel without a published digest is not
   installed.
2. Back up this node to `netbbs_backups/` beside the database, the same as
   **Backup → Create backup now**.
3. `pip install` the wheel into the environment NetBBS runs from, with the same
   extras. pip also fetches any newer dependency the release needs. The install
   is refused for a system Python (not a virtual environment), a development
   checkout, or an environment the service account cannot write. A failed pip
   run shows its last lines of output.
4. Restart, or not, as **[R]estart after install** says:
   - **auto** restarts under systemd, detected, and not under NetBSD rc.d.
   - **yes** declares that your service manager restarts NetBBS when it exits.
   - **no** always stops after installing.

   A restart warns callers and waits the configured shutdown delay. The node
   then exits with status 75, and the service manager starts the new version.
   Without a restart, the screen tells you to restart the service yourself. Do
   it promptly: until then, the old version runs with the new files on disk.

After the restart, the Update screen says whether the node came back as the
version that was installed.

**MANUAL — on the host, units installed from an earlier release:** the shipped
`netbbs.service` now carries `RestartForceExitStatus=75` and
`SuccessExitStatus=75`. An older unit with `Restart=on-failure` already restarts
on 75, but logs it as a failure. Add both lines, then run
`systemctl daemon-reload`.

Rolling back stays a host procedure: stop the service, install the previous
release's wheel, restore the backup from step 2, and start the service.

**MANUAL — on the host (the by-hand route, always available):**

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
| You think `signing.identity` or `transport.identity` leaked | Replace that key as **Compromised** under **Link status → Keys**, or with `python -m netbbs.admin rotate-key signing --compromised` while the node is stopped. The address and reputation stay; see [Node keys](#node-keys). If `root.identity` leaked too, as it does with a whole copied identity directory or backup, rotation is no remedy: the holder can authorize keys of their own, and only a new node identity ends that. |
| SSH import fails on NetBSD | Check pkgsrc libraries and `LD_LIBRARY_PATH`; see installation above. |
| An SSH client cannot connect, e.g. SyncTERM says "Error -20 activating session" or "Error -30 activating session" | "Error -30" with "an excessive number of consecutive no-op packets" means the node is older than the fix for issue #964; upgrade it. For "Error -20": the node offers an Ed25519 host key and, for clients without Ed25519 such as older SyncTERM builds, an RSA one (`netbbs_ssh_host_key_rsa`, created at the first start that lacks it). Make sure the node was restarted after upgrading so the RSA key exists. A caller who connected before sees a new fingerprint only if their client picks the RSA key. |
| The log says an SSH host key "was readable by other accounts" | The key file was created before NetBBS restricted it, or restored from a backup taken then. NetBBS has already made it owner-only (0600). If other people have accounts on the host, assume they could have copied it: stop the node, delete `netbbs_ssh_host_key` and `netbbs_ssh_host_key_rsa`, and start it again to get new keys. Callers' SSH clients will then warn that the host key changed, so tell them first. On a host only you can log in to, no action is needed. |
| Caller cannot log in | Check maintenance mode, pending approval, disabled account, and login throttling before resetting credentials. |
| Caller can read but cannot contribute | Check write/join gates, age/name attestations, moderator grants, and inherited Community settings. |
| Browser terminal or upload fails | Check HTTPS proxy/WebSocket forwarding, upload limits, web listener, and `public_url`. |
| Callers or peers cannot reach the address you expect | Compare **Settings → Network & login limits → Node configuration** (the configuration the node resolved at its last start, the addresses it advertises, and where each value came from; a listener whose optional extra is missing is listed but was not started, and the service output says so) with your TOML file and the service's command line. |
| Terminal offers no file-transfer link | Enable/configure the web listener and its public URL, or use a Zmodem-capable client. |
| Link will not start | Check the `web` extra, effective participation setting, and a non-placeholder node name. |
| **Find** misses content callers can open, or lists removed content | **Operations → Search indexes**: check, then **Rebuild** if it reports drift. |
| Peers connect but content is missing | Check **Link status → Offered** and **Excluded**, trust state, Outbox, and Diagnostics. Use Repair carried posts only for local materialization repair. |
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

Some lines are routine and need nothing from you. A caller who hangs up is one
`INFO` line naming their address. Every node yours meets on NetBBS Link starts
on probation here, and the log says once per node, since the node started,
that its content is held back. Your node is on probation at each peer in the
same way, and the log says once per peer when one does not take what yours
sends yet. A relay candidate that cannot be reached is also mentioned once. A `WARNING` or `ERROR` line, and any traceback, is worth
reading.

**Operations → Node log** reads that file from inside NetBBS, including from
`python -m netbbs.admin` while the node is stopped. That console runs outside
the node, so its health panel has no live controls; it says whether the node
is running. It shows the newest 512 KiB,
topped up from `netbbs.log.1` after a rotation, and says when older lines exist
that it does not show. It cannot show failures from before the log opened; for
those, use the service manager's output above.

Link Diagnostics is a bounded warning/error log, not a transcript of all
content. The administrative Audit log answers who changed a setting or moderated
an item. Inspect those before attempting filesystem repairs.

For unresolved problems, [file an issue](https://github.com/Thiesi/NetBBS/issues)
with the release, operating system, transport, steps, and relevant error text.
Remove passwords, private keys, tokens, personal data, and transfer URLs.
