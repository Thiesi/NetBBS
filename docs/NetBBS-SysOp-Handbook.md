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
- [FTN networks (FidoNet, fsxNet)](#ftn-networks-fidonet-fsxnet)
- [QWKnet networks (DOVE-Net)](#qwknet-networks-dove-net)
- [Daily operation](#daily-operation)
- [State, backup, and recovery](#state-backup-and-recovery)
- [Upgrading and removing NetBBS](#upgrading-and-removing-netbbs)
- [Troubleshooting](#troubleshooting)

## Installing NetBBS

| At a glance | |
| --- | --- |
| **Platforms** | NetBSD (primary), mainstream Linux (supported); other POSIX systems best effort; Windows for development and testing |
| **Needs** | Python 3.11 or newer, SQLite with FTS5 support for search |
| **Get it from** | The official [GitHub releases](https://github.com/Thiesi/NetBBS/releases) or a tagged source archive |
| **Decide first** | Which extras you need: `ssh`, `web`, or neither |

In this chapter: [Prepare the host](#prepare-the-host) ·
[Install a release](#install-a-release)

SQLite is the database engine included with Python; you do not install or
administer a separate database server. NetBBS itself is not distributed
through PyPI, pkgsrc, or apt. Your operating system's package manager
supplies prerequisites.

The commands below use this layout. Substitute your paths consistently if you
choose a different one.

| What | Path or name |
| --- | --- |
| State | `/var/lib/netbbs` |
| Service account | `netbbs` |
| Configuration | `/etc/netbbs/netbbs.toml` |

### Prepare the host

> [!IMPORTANT]
> **Manual, on the host.** These steps run on your server's command line.

#### 1. Install Python and virtual-environment support

A virtual environment is a private installation directory for NetBBS and its
libraries; it keeps them separate from other programs on your server.

Check the version with `python3 --version`: it must say 3.11 or newer.

- **Debian/Ubuntu:** `python3 -m venv` fails until the matching `venv`
  package is installed:

  ```sh
  sudo apt install python3 python3-venv
  ```

- **NetBSD:** use pkgsrc packages such as `python312` and `py312-pip`. A
  typical pkgin package set is:

  ```sh
  pkgin install python312 py312-pip rust openssl libffi pkgconf
  ```

<details>
<summary>Why NetBSD needs Rust and a C compiler</summary>

The SSH extra uses AsyncSSH and `cryptography`; a source build on NetBSD also
needs Rust, a C compiler, Python headers, OpenSSL, libffi, and pkgconf.
Install NetBSD's `comp` set if the C compiler is missing.

Build requirements can change with dependency releases; use the
[cryptography installation requirements](https://cryptography.io/en/latest/installation/)
for the version pip selects. Rust is a build requirement, not a service you
need to run. On Linux, prebuilt dependency wheels often avoid this toolchain.

</details>

#### 2. Create the service account and directories

Create the unprivileged account and directories **before** installing into
them. Skip account/group creation if they already exist.

**Linux:**

```sh
sudo useradd --system --user-group --home /var/lib/netbbs --create-home netbbs
sudo install -d -m 750 -o netbbs -g netbbs /var/lib/netbbs /etc/netbbs
```

**NetBSD:**

```sh
sudo groupadd netbbs
sudo useradd -g netbbs -d /var/lib/netbbs -m -s /bin/sh netbbs
sudo install -d -m 750 -o netbbs -g netbbs /var/lib/netbbs /etc/netbbs
```

> [!WARNING]
> Run the application and games as this account, never root.

> [!NOTE]
> The supplied NetBSD service script needs a Bourne-compatible account shell
> such as `/bin/sh` because it starts the process through `su`;
> `/sbin/nologin` and csh are unsuitable for it.

### Install a release

> [!IMPORTANT]
> **Manual, on the host.** These steps run on your server's command line.

> [!TIP]
> Tried NetBBS with the website's local trial first? That trial database is
> unrelated to this installation and can simply be deleted with its
> directory. This installation starts with a fresh database, and you create
> your SysOp account again below.

#### Choose the extras

Choose extras according to what you enable:

| Extra | Needed for |
| --- | --- |
| `ssh` | SSH caller connections, enabled by default |
| `web` | Browser terminal, HTTP file-transfer links, **and NetBBS Link** |
| Neither | A Telnet-only node with SSH, web, and NetBBS Link disabled |

The commands below install both (`[ssh,web]`). Production installs do not
need the `dev` extra or an editable source checkout.

#### Install the wheel

1. Download the wheel (the `.whl` file) from GitHub Releases.
2. Copy it somewhere the service account can read it, such as `/tmp`. The
   service account usually cannot read files in your home directory, and pip
   reports a wheel it cannot read as "does not exist".
3. Run the commands below. Replace `VERSION` with the version number only, for
   example `7.13.0`, and `~/Downloads` with the directory you downloaded to.
   On NetBSD, use `python3.12` in place of `python3` if that is your installed
   interpreter's name.

```sh
sudo install -m 644 ~/Downloads/netbbs-VERSION-py3-none-any.whl /tmp/
sudo -u netbbs python3 -m venv /var/lib/netbbs/.venv
sudo -u netbbs /var/lib/netbbs/.venv/bin/python -m pip install --upgrade pip
sudo -u netbbs /var/lib/netbbs/.venv/bin/python -m pip install "/tmp/netbbs-VERSION-py3-none-any.whl[ssh,web]"
sudo -u netbbs /var/lib/netbbs/.venv/bin/python -m netbbs --version
```

`--version` prints the package and database-schema versions without starting
listeners.

<details>
<summary>The release has only a tagged source archive</summary>

Unpack it, create a temporary build environment, run
`python -m pip install build` and `python -m build` there, then install its
`dist/` wheel as above.

</details>

<details>
<summary>NetBSD: SSH fails with <code>libssl.so.3 not found</code></summary>

If SSH fails with `libssl.so.3 not found` after a successful install,
pkgsrc's `/usr/pkg/lib` may be missing from the runtime library search path.
The supplied rc.d script sets `LD_LIBRARY_PATH` for the service. For an
attended check, use:

```sh
sudo -u netbbs env LD_LIBRARY_PATH=/usr/pkg/lib /var/lib/netbbs/.venv/bin/python -c "import asyncssh"
```

Do not substitute the base system's differently versioned OpenSSL library.

</details>

## First account and configuration

| At a glance | |
| --- | --- |
| **Config file** | `/etc/netbbs/netbbs.toml`, readable by the service account |
| **From the shell** | `python -m netbbs.admin` (first SysOp, onboarding), `python -m netbbs --help` (all switches) |
| **Decide first** | Which listeners to open, and whether to join NetBBS Link and take a managed subdomain |

In this chapter: [Write the configuration](#write-the-configuration) ·
[Create the first SysOp](#create-the-first-sysop) ·
[Where settings live](#where-settings-live)

### Write the configuration

> [!IMPORTANT]
> **Manual, on the host.** Save this as `/etc/netbbs/netbbs.toml`, readable
> by the service account.

This starts with SSH; browser access is covered below.

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

Telnet and the web listener are off here, and both listen on loopback
(`127.0.0.1`), which only this host can reach.

- **To offer Telnet,** set `enabled = true` **and** `host = "0.0.0.0"`, as
  `[ssh]` has.
- **The web listener** stays on loopback behind an HTTPS proxy, as described
  in [Caller connections and file transfers](#caller-connections-and-file-transfers).

> [!WARNING]
> With `enabled = true` alone, outside callers cannot connect.

### Create the first SysOp

Create your first SysOp before starting a public listener:

```sh
sudo -u netbbs /var/lib/netbbs/.venv/bin/python -m netbbs.admin --db /var/lib/netbbs/netbbs.db
```

The interactive admin console asks for the first account and its password
and/or public key. Use this supported bootstrap tool; `create_test_user.py`
is a development helper. SysOp accounts have level **255**.

> [!IMPORTANT]
> Run `netbbs.admin` as the account the node runs as, as shown above. It
> writes owner-only files beside the database (the managed-DNS credential
> among them), and a copy written as root is one the node cannot read; if you
> had to use root, `chown` the state directory back afterwards.

#### First-run onboarding

First-run onboarding offers three independent choices:

| Choice | Default | What it does |
| --- | --- | --- |
| **Join NetBBS Link through the reliable nodes** | Yes | Outgoing-only participation, using the project's bootstrap and relay nodes. A distinct node name is required. |
| **Managed `<name>.netbbs.org` subdomain** | Yes | A public hostname managed through the project's DNS service. This does not configure your router, TLS, or ports. |
| **Switch on the house bot Baud** | No | Creates Baud in one channel you pick: it answers messages addressed to it and greets callers who join. |

> [!WARNING]
> Read the choices before pressing <kbd>Enter</kbd>: the first two default
> to yes.

If not completed here, onboarding is offered at the first SysOp login.

The Baud choice is asked only while the node has no bot instance and has a
chat channel to offer. A brand-new node has no channels, so it is asked at the
first SysOp login after you create one. Saying yes:

1. Lists the channels, local ones whose policy shows bots.
2. Takes a number or a name; <kbd>Enter</kbd> picks the first.
3. Creates the instance enabled, addressed in that channel, with greetings
   and the presence-event grant they need there, and the friendly
   temperament. Nothing else is granted, and it is not that channel's
   default bot.

Change any of it later under **Content ▸ Open chat bots**
(see [Run a bot](#run-a-bot)).

<details>
<summary>The managed subdomain on a node that has never been started</summary>

On a node that has never been started, accepting the managed subdomain
records your answer but cannot pick the name yet: the service knows a node by
an identity it creates on its first start. Start the node, then sign in as
SysOp or run `netbbs.admin` again, and the name editor opens by itself, once.
After that it is <kbd>R</kbd> **Register** on the console's
<kbd>D</kbd> **DNS** screen.

</details>

### Where settings live

| Kind of setting | Where it lives |
| --- | --- |
| Transport and path settings | The TOML file and command-line options |
| Accounts, content, and many live settings | The database |
| Link limits, the login throttle and the shutdown delays | Either: TOML/command line, or **Settings ▸ Operating limits** |
| Listeners, `public_url`, Link addresses, paths and the managed-DNS service | TOML and the command line only |

Command-line options override TOML settings. Run `python -m netbbs --help`
using the installed interpreter for all supported switches.

> [!IMPORTANT]
> A TOML file is read only when supplied with `--config`; placing
> `netbbs.toml` in the working directory is not enough.

#### Operating limits from the console

**Settings ▸ Operating limits** holds the carry caps, peering, Link limits,
real-time relay bounds, login throttle and shutdown delays, grouped.

- A value there applies the next time the node starts.
- A key your TOML file or a command-line option sets still wins: the screen
  shows it as *set in config* and does not let you change it there. Remove it
  from the TOML file to manage it from the console.

<kbd>A</kbd> **Apply capacity preset** sets the Link groups (carry caps,
peering, Link limits and real-time relay) for the size of node you run:

| Setting | Small | Default | Hub |
| --- | --- | --- | --- |
| Carried boards / channels / file areas | 50 each | built-in | 2,000 each |
| Peers remembered | 200 | built-in | 5,000 |
| Relay for others | — | built-in | on |
| Relay clients | — | built-in | 100 |
| Files per carried area | 1,000 | built-in | 20,000 |
| Request burst / per minute | — | built-in | 60 / 180 |
| Diagnostics kept (days / entries) | 14 / 2,000 | built-in | 90 / 20,000 |
| Live bridges / pending rendezvous | — | built-in | 32 / 128 |
| Relay bytes per second | — | built-in | 262,144 |

"—" leaves the value as it is. **Default** forgets the values saved on this
screen, so the built-in defaults apply, including any later change to them.

1. Pick a tier. The preview lists each value it would change as
   `before → after`, and under **Not changed** each key your config file or
   command line sets.
2. Read the warnings. Hub's say that higher caps let other nodes fill your
   storage and that relaying spends bandwidth on nodes you do not know.
3. Press <kbd>A</kbd> **Apply**, or <kbd>B</kbd> **Back** to write nothing.
   The values apply at the next start, and one audit entry lists them all.

> [!NOTE]
> A preset cannot make an outgoing-only node a full peer, so on one Hub's
> relay settings do nothing. The preview shows the TOML still to set; see
> [Run as a full peer](#run-as-a-full-peer).

The login throttle, shutdown delays, seeds and sync interval are not part of
any tier.

#### Settings kept out of the console

The listeners, `public_url`, Link addresses, paths and managed-DNS service
stay in TOML and on the command line only, because a wrong value set from
inside NetBBS could lock you out of the session you would need to fix it.

You can still see them: **Settings ▸ Operating limits ▸ Node configuration**
lists each one as the node resolved it at its last start, with its TOML key
and whether the value came from the config file, the command line or the
default. It works in `python -m netbbs.admin` too, and never shows the
managed-DNS admin token.

#### Link participation and the node name

- Leave `[link] enabled` unset if you want the onboarding/Settings choice to
  decide participation. An explicit `enabled = false` or `true` overrides
  that choice.
- The TOML `[node] name` labels the cryptographic key files; it does not set
  the public display name. Set that through onboarding or
  **Settings ▸ Node name**. Changing a label does not change a node's
  cryptographic identity.

## Running a service

| At a glance | |
| --- | --- |
| **Linux** | systemd unit [netbbs.service](../examples/netbbs.service) |
| **NetBSD** | rc.d script [netbbs.rc](../examples/netbbs.rc) |
| **Shutdown delay** | 60 seconds by default: **Settings ▸ Operating limits**, or `[shutdown] graceful_delay_seconds` |

> [!IMPORTANT]
> **Manual, on the host.** Download the service example from the same
> release's source tree. Examples are not installed by the wheel. Adjust paths
> and account names before installing it;
> [examples/README.md](../examples/README.md) describes both files.

### Install the service

**Linux:** install [netbbs.service](../examples/netbbs.service) as
`/etc/systemd/system/netbbs.service`, then:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now netbbs
sudo systemctl status netbbs
```

**NetBSD:** install [netbbs.rc](../examples/netbbs.rc) as executable
`/etc/rc.d/netbbs`, add `netbbs=YES` to `/etc/rc.conf`, then:

```sh
sudo service netbbs start
sudo service netbbs status
```

> [!TIP]
> Check a real login as well as service status.

> [!WARNING]
> The systemd example restarts on failure; NetBSD rc.d does not automatically
> restart a crashed node. If you need that behavior, arrange an external
> health check or supervisor yourself.

### Stopping and the shutdown delay

NetBBS runs in the foreground. The service manager handles backgrounding.

`systemctl stop netbbs` or `service netbbs stop` requests a graceful shutdown:

- Callers are warned, then disconnected after the configured delay (60
  seconds by default).
- With nobody connected, or once the last caller leaves, it stops without
  waiting out the delay.
- A shutdown you schedule from the console keeps the delay you chose.

Change the delay under **Settings ▸ Operating limits**, or as
`[shutdown] graceful_delay_seconds`. Cleanup takes additional time. Increase
the service stop timeout if you raise that delay or configure slow-stopping
door services.

### Paths the service can write

- The Linux unit restricts writable paths to `/var/lib/netbbs`.
  **Manual:** extend its `ReadWritePaths` if you put games or state
  elsewhere.
- NetBSD's example sets `HOME` to `netbbs_statedir`; remember this when
  locating Voidrunner saves.

## Caller connections and file transfers

| Connection | Default | What to arrange |
| --- | --- | --- |
| SSH | Enabled, all interfaces, port 2222 | Install `ssh` extra; allow the chosen port |
| Telnet | Disabled, loopback, port 2323 | Enable explicitly; plaintext credentials and traffic |
| Web | Disabled, loopback, port 8080 | Install `web` extra; provide HTTPS through a reverse proxy |

In this chapter: [Reaching the node](#reaching-the-node) ·
[Browser access behind a proxy](#browser-access-behind-a-proxy) ·
[File transfers](#file-transfers)

### Reaching the node

> [!IMPORTANT]
> **Manual, outside NetBBS.** Configure firewall rules, router forwarding,
> DNS, and the reverse proxy for the connection methods you offer.

- A bind address of `0.0.0.0` means all IPv4 interfaces; it is not an
  address to give callers.
- A loopback listener is reachable only from the same host.
- Enabling Telnet or web with `enabled = true` alone leaves it on loopback;
  the node log says so at every start, naming the `host` line to add.

### Browser access behind a proxy

#### 1. Configure the web listener

For a browser terminal and transfer links behind a proxy, replace the
existing `[web]` table with:

```toml
[web]
enabled = true
host = "127.0.0.1"
port = 8080
public_url = "https://bbs.example.org"
trusted_proxies = ["127.0.0.1", "::1"]
```

Use a real hostname and certificate; `bbs.example.org` is a placeholder.
Restart after changing listener settings.

#### 2. Point an HTTPS proxy at it

The proxy must:

- forward to that local port, WebSocket upgrades as well as ordinary
  requests;
- allow an upload body at least 1 MiB above NetBBS's upload cap
  (**Settings ▸ Limits & retention**, 100 MiB by default): a browser upload
  carries form framing on top of the file.

Pick the one you run:

<details>
<summary><b>Caddy</b>: the shortest route if you don't run a web server yet</summary>

[Caddy](https://caddyserver.com/) obtains and renews the certificate itself,
forwards WebSocket upgrades, and sets no upload limit of its own. The
hostname must already point at this host, with ports 80 and 443 reachable
from outside. Its whole `Caddyfile` is below. Debian and Ubuntu packages read
`/etc/caddy/Caddyfile`; on NetBSD, pkgsrc's Caddy reads
`/usr/pkg/etc/caddy/Caddyfile`.

```
bbs.example.org {
    reverse_proxy 127.0.0.1:8080
}
```

</details>

<details>
<summary><b>nginx</b></summary>

Inside the `server` block that already holds your certificate:

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

</details>

<details>
<summary><b>Apache httpd</b> 2.4.47 or later</summary>

With `mod_proxy` and `mod_proxy_http` loaded, inside the `VirtualHost` that
already holds your certificate:

```apache
ProxyPreserveHost On
ProxyPass "/" "http://127.0.0.1:8080/" upgrade=websocket timeout=1800
ProxyPassReverse "/" "http://127.0.0.1:8080/"
LimitRequestBody 105906176
```

NetBBS sends no WebSocket pings, so without `timeout=` Apache cuts off an idle
browser caller at its global `Timeout`. `LimitRequestBody` is in bytes,
101 MiB here.

> [!WARNING]
> On NetBSD, one host refused every backend connection with
> `AH00957 (22)Invalid argument` when `timeout=` was set to 3600, while 600
> and 1800 work; keep `timeout=` at 1800 or below there.

</details>

#### 3. Let NetBBS see each browser caller's own address

Behind any of these proxies, every browser caller reaches NetBBS from the
proxy's address. `trusted_proxies` tells NetBBS which addresses are your
proxies: for a connection from one of them, NetBBS takes the caller's address
from the `X-Forwarded-For` header the proxy adds (Caddy and Apache add it by
default; the nginx example above has the line).

> [!WARNING]
> Without it, all web callers share one address: one caller mistyping a
> password, or one script guessing, soon throttles **every** web login on the
> node, and the SysOp screens cannot tell browser callers apart.

- List only proxies you run. A caller can write anything into the header, so
  NetBBS reads it only from a connection whose own address is listed, and
  then only the entry your proxy added.
- Entries are IP addresses or networks such as `10.0.0.0/8`, never
  hostnames.

#### Why `public_url` matters

`public_url` supplies the externally reachable base address for transfer
links. Without it, a node bound to a wildcard or loopback address cannot give
remote terminal callers a usable link. A specific reachable bind address may
be used as a fallback, but explicit configuration is preferable behind a
proxy. When the web listener is on and SSH or Telnet callers would get no
link, the node log warns at every start.

### File transfers

File transfers work three ways:

| Route | Who can use it |
| --- | --- |
| Through the browser | Browser callers |
| Short-lived browser links shown to terminal callers | Terminal callers |
| Zmodem | Callers with a capable terminal client |

PuTTY and ordinary SSH clients lack Zmodem. They need the web listener for
browser transfers. SFTP is not provided by the NetBBS SSH server.

> [!CAUTION]
> Transfer URLs contain bearer tokens: anyone holding one can use that
> transfer. Use HTTPS, avoid sharing or logging those links, and request a
> new link after expiry.

The node's upload limit and any lower proxy limit both apply.

## Using the SysOp console

| At a glance | |
| --- | --- |
| **Open it** | Log in as a level-255 account and press <kbd>S</kbd> **SysOp** |
| **From the shell** | `python -m netbbs.admin --db /var/lib/netbbs/netbbs.db` |
| **Helpers** | Staff get a smaller <kbd>S</kbd> **Staff** console instead ([Sharing the work](#sharing-the-work-staff-permissions)) |
| **Decide first** | Who may sign up: step 1 of [A first day, in order](#a-first-day-in-order) |

The dashboard shows node mode, active sessions, pending approvals,
backup/update status, and NetBBS Link health when enabled. Refresh it after
making changes elsewhere.

| Area | Purpose |
| --- | --- |
| <kbd>U</kbd> **Users** | Accounts, registration, levels, approval, staff permissions, identity-verifier grants |
| <kbd>C</kbd> **Content** | Message boards, file areas, chat channels, Communities, doors, moderation |
| <kbd>O</kbd> **Operations** | Sessions, maintenance, audit log, backups, Link diagnostics |
| <kbd>S</kbd> **Settings** | Branding, timestamps, node name, network participation, update checks, limits and retention |

### A first day, in order

Most first-time SysOps get furthest by doing these before telling anyone the
address:

1. Choose who may sign up under **Users ▸ Registration**
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

### Keys that work on every screen

Quick actions lead to the same screens. Use the displayed keys rather than
old menu letters from release notes.

| Key | What it does |
| --- | --- |
| <kbd>B</kbd> **Back** or <kbd>Esc</kbd> | Leaves the screen. <kbd>Esc</kbd> first clears a highlighted row or field. |
| <kbd>&lt;</kbd> <kbd>&gt;</kbd> or <kbd>PgUp</kbd>/<kbd>PgDn</kbd> | Turn a page or a form's section |
| <kbd>Left</kbd>/<kbd>Right</kbd> | On a list, turn the page too; on a form, step the highlighted field's value |
| <kbd>/</kbd> | Finds |
| <kbd>?</kbd>, <kbd>F1</kbd> or <kbd>Ctrl</kbd>+<kbd>H</kbd> | Show help |

### Forms and fields

- Draft editors keep changes local until <kbd>S</kbd> **Save**; Back
  discards the draft after asking.
- Fields are numbered **01**, **02** and on through every section of the
  screen. Type a field's number (two digits, or one and <kbd>Enter</kbd>) to
  open it from any page, or move with <kbd>Up</kbd>/<kbd>Down</kbd> and press
  <kbd>Enter</kbd> or <kbd>Space</kbd>.
- Letters on these screens are only their actions.

In current field prompts, the existing value is already in the input line:

- <kbd>Enter</kbd> accepts it.
- <kbd>Esc</kbd> cancels the field edit.
- Deleting all text clears it where the field permits an empty value.

> [!IMPORTANT]
> Do not assume an empty line means "keep the old value."

### The console from the shell

`python -m netbbs.admin --db /var/lib/netbbs/netbbs.db` is a second,
**interactive** route to account/content administration without a caller
connection.

- It cannot control a running process's sessions, maintenance, or live
  networking.
- Its Backup screen is status-only; use the backup CLI there.

## Accounts, permissions, and identity

| At a glance | |
| --- | --- |
| **Where** | **Users** in the SysOp console |
| **From the shell** | `python -m netbbs.admin last` (levels), `python -m netbbs.admin reset-password` |
| **Decide first** | Who may sign up, and whether you need any level above 0 at all |

In this chapter: [Registration](#registration) · [Levels](#levels) ·
[Account lifecycle](#account-lifecycle) ·
[Sharing the work](#sharing-the-work-staff-permissions) ·
[Verified age and names](#verified-age-and-names)

### Registration

Choose who may sign up under **Users ▸ Registration**:

| Mode | What happens |
| --- | --- |
| <kbd>O</kbd> **Open** | New accounts can log in immediately. |
| <kbd>A</kbd> **Approval required** | New accounts wait for you and cannot log in until you approve them. |
| <kbd>C</kbd> **Closed** | Only SysOps create accounts. |

With approval required:

- Open a pending account under **Users ▸ User list** and press
  <kbd>A</kbd> **Approve**, or <kbd>D</kbd> **Decline** to remove it after a
  yes/no.
- Until you approve it, the account cannot log in at all, not even to look
  around. When the caller tries, they are told it is waiting for your
  approval.
- <kbd>Q</kbd> **Question** sets an optional question new callers answer when
  they sign up ("What do you write with?"). The answer shows on the pending
  account and is deleted once you approve it.

A caller cannot register:

- a reserved name (`sysop`, `admin`, `root`, `moderator`, `guest` and a few
  more);
- a name containing "sysop";
- a name that reads like a SysOp's own (`lnkwell` for `InkWell`).

You can still create such an account yourself under **Users ▸ Create user**.

### Levels

Every account has a level from 0 to 255:

| Level | Meaning |
| --- | --- |
| **0** | Where every account starts, whether a caller signed up or you created it. Approving a pending account leaves it at 0. |
| **1–254** | Grant nothing by themselves. They matter only where you set a minimum. |
| **255** | SysOp: the whole console and power over every account. |

Every minimum starts at 0. You can set one on:

- a message board's or file area's **Min read level** and **Min write
  level**, which a Community can supply as defaults;
- a chat channel's single **Min level**, to join it;
- a door's **Min play level**;
- node-wide minimums under **Settings**, such as the Mail level and the Node
  map level.

> [!NOTE]
> A channel's and a door's level are always set on the channel or door
> itself, never inherited from a Community.

#### A level plan for a small club

A small club rarely needs more than this:

| Level | Who | Example use |
| --- | --- | --- |
| 0 | Everyone, including new callers | General boards, file areas, chat channels and games, every level left at 0 |
| 10 | Members you know | A members' lounge board with read and write level 10, and a members' channel with Min level 10 |
| 255 | You | An announcements board with read level 0 and write level 255 |

> [!WARNING]
> Be careful with 255 as a minimum. An announcements board with write level
> 255 shuts out everyone who is not a full SysOp, including a helper you
> trust later. Do not raise that helper to 255, which hands them the whole
> node, your own account included. Give them a **Read and post** grant on
> that board instead ([Content and Communities](#content-and-communities)),
> and staff permissions for account work
> ([Sharing the work](#sharing-the-work-staff-permissions)).

#### Change an account's level

1. Open the account under **Users ▸ User list**.
2. Press <kbd>L</kbd> **Level** and type the new level (0 to 255).
3. Read the preview of what the change opens and closes for that account:
   - **Gains:** each board to read or post on, file area to download from or
     upload to, channel, door and node-wide feature (node map, mail, opening
     MRC rooms, the oneliner wall, the SysOp console) the new level lets them
     into.
   - **Loses:** what the new level takes away.
   - **Still blocked:** what the new level opens, but another gate still
     keeps this account out of, such as an age or verified-name requirement
     or a members-only channel.
4. Press <kbd>A</kbd> **Apply**, or <kbd>B</kbd> **Back** to leave the level
   as it was.

The preview counts the account's own read and write grants, so a helper who
can post on a board through a grant does not "lose" it in a demotion. A
change that opens and closes nothing is applied straight away, and the line
under the screen says so.

#### Promote new callers automatically

Under **Users ▸ Auto-promotion rules**, <kbd>C</kbd> **Create** makes a rule:
accounts at one level are raised to a higher one once they

- are old enough (hours since signup),
- have logged in often enough, and
- optionally, have posted enough here.

> [!TIP]
> A common first rule is `0 → 10` after 24 hours and 2 logins.

Each row shows what an account needs, how many accounts would be promoted at
their next login (**ready**), and what the step opens. How the rules apply:

- Rules are checked when a caller logs in. The caller starts that session at
  the new level and is told so.
- One rule applies per login, and only one rule can start from each level.
- A rule never raises anyone to 255.
- The rules never touch the guest account, staff, SysOps, or pending and
  disabled accounts.
- They leave alone any account whose level you set by hand, so a caller you
  demote stays demoted.
- Each automatic promotion appears in the moderation log as **(system)**,
  naming the rule.

The account screen shows **Auto promotion: on**, **off (level set by hand)**,
or **on, but skipped** with the reason (a pending account, for example).
<kbd>U</kbd> **Use promotion rules** turns it on or off.

#### Start with a ladder

<kbd>A</kbd> **Apply levels ladder** under **Users ▸ Levels (LAST)** sets up
the club plan above in one step:

- level 0 is named **New** and level 10 **Member**;
- a rule raises accounts from 0 to 10 after 24 hours and 2 logins;
- the Mail level and the Oneliner level become 10.

It replaces only those: a name you gave level 0 or 10, or your rule from
level 0, shows in the preview as `before → after`, and every other name and
rule stays. A name another level already has is listed under **Not changed**.

> [!WARNING]
> Accounts at level 0 lose mail and the oneliner wall until the rule raises
> them at a later login. The preview counts the level-0 accounts the rule never
> raises (level set by hand, staff, pending, disabled, the guest account):
> raise those by hand if they should keep mail.

Press <kbd>A</kbd> **Apply** to write it all at once, with one audit entry.
It applies straight away.

#### See what every level opens (LAST)

The Level Admin SysOp Tool, **Users ▸ Levels (LAST)**, lists each level that
matters on your node: one some board, file area, channel, door or setting is
gated at, or one an account holds. A row shows how many enabled accounts hold
exactly that level and what it adds to the levels below it, for example
`2 read · 1 post · 1 door · Mail`.

Pick a level to see its gates one by one, each with what it is (a board, file
area, channel, door, or a node-wide feature such as mail) and where its level
comes from: set on the resource itself, inherited from a Community, the
default of 0, or a setting. There:

- <kbd>V</kbd> **View** steps the list through what is new at this level,
  everything it opens, what stays closed to it, and what it opens while
  another gate (an age or verified-name requirement, a members-only channel)
  still applies.
- Picking a board, file area, channel or door opens its own screen, where you
  can change its levels.

On the level list, <kbd>G</kbd> **Go to level** shows any level, in use or
not.

**Name a level**, for example "Member" for 10: in LAST, press
<kbd>N</kbd> **Name** on its row and type the name, or a blank line to clear
it. From then on the console shows that level as `10 (Member)`, and the level
prompt on a user's screen and **Go to level** in LAST take `member` as well as
`10`.

- 255 is always "SysOp".
- A name is up to 12 characters, needs at least one letter, and no two levels
  can share one.
- A name is only a label: renaming or clearing it changes nobody's access.

The same overview is available from the shell, without opening the console.
The command only reads; it changes nothing. Add `--json` to any of them for
scripts.

```sh
python -m netbbs.admin last                          # every level in use
python -m netbbs.admin last member                   # what one level opens
python -m netbbs.admin last --user alice --to 50     # one account's change, previewed
```

#### Level fields in editors

Every level field in an editor also says what its value means:

- Next to the level it shows how many enabled accounts that level lets in,
  for example `10 · 41 users`, and the count follows what you type before you
  save.
- A board or file area level left to inherit shows what it inherits and from
  where, for example `none: 10 from Community Market · 41 users`.
- A Community's default levels show how many boards and file areas inherit
  them.

The count goes by level only: an age or verified-name requirement can still
keep some of those accounts out.

### Account lifecycle

Open an account under **Users ▸ User list**. Its detail screen lists what you
can change in one column, in the order the arrow keys walk it: level, status,
blocklist, display name, birthdate, public key, password, staff permissions,
the identity-verifier permission and automatic promotion. Below that, under
**Record**, is what you can't change there: when the account was created, how
many admin actions it has, and every moderator grant it holds, such as
`board "News": approve`.

> [!IMPORTANT]
> NetBBS refuses an account change that would leave no enabled, approved
> SysOp.

#### Correct a display name or birthdate

A caller sets their own display name and birthdate under
**Your profile ▸ Name & details**. If one needs correcting, a typo or a
display name that has to go, use <kbd>N</kbd> **Name** or
<kbd>W</kbd> **When born** on the account's screen.

- The value opens in the line to edit; a blank line clears it,
  <kbd>Esc</kbd> keeps it. The same rules apply as for the caller's own
  change.
- The account's **History** records each edit with your name, the old and new
  display name, and for a birthdate only that it changed.
- A verified age or real name is a separate record that these never change,
  and the screen says so when one is on record.

Staff with **Manage accounts** can do the same for accounts within their
reach.

#### Edit a member's profile

Everything else a member sets for themselves is under
<kbd>E</kbd> **Edit profile** on the same screen. It opens their own
**Profile**, acting on their account: bio, signature, location, who they
block, and their display, mail and chat settings, with the same help they see.

- Your own screen settings stay as they are while you work there, and a
  character set you pick is theirs, not yours.
- Each change goes into the account's **History** with your name: a setting
  with its new value, and their bio, signature, location or blocks only as
  changed, never what they say.
- A member who is signed in sees some display changes only when they next
  sign in.

Some things stay as they are:

- Whether a verified age or name is shared over Link stays their own choice.
- Only you, not staff, can change an account's SSH keys.
- The username is changed from the account's screen, not here (see
  [Rename an account](#rename-an-account)).

Staff with **Manage accounts** get **Edit profile** too, for accounts within
their reach; nobody gets it on their own account. On the guest account only
you get it, and it sets the **guest defaults** (see
**Settings ▸ Guest access**).

#### Reset a password

A caller who forgot their password cannot recover it themselves: NetBBS has
no e-mail or other out-of-band channel.

- **For a caller:** press <kbd>P</kbd> **Password** on the account's screen
  and set a new one. The old password is neither shown nor needed.
- **For your own SysOp account, if you are locked out:** run this on the host:

  ```sh
  python -m netbbs.admin reset-password YOURNAME --db /path/to/netbbs.db
  ```

Both routes are audit-logged with your name. An account that also has an SSH
key can have its password removed from the same screen, which leaves key
login as its only way in.

#### Disable or delete an account

<kbd>T</kbd> **Toggle enable/disabled** revokes or restores an account's
access. Deleting one, under **Users ▸ Delete user**, is permanent and
requires the account's exact name. Existing
content retains its recorded author label.

Mail and a disabled or pending account:

- A disabled account, and a signup still awaiting approval, receive no mail.
  Callers here are told so at the To prompt, and Link mail bounces with "that
  account is not taking mail at the moment", which does not tell the other
  node the account is disabled.
- Mail already in a disabled account stays there until you enable it again.
- Deleting an account removes its own mail, but letters it sent or received
  stay in the other person's Sent or Inbox. Their Sent shows the deleted
  recipient as, for example, `bob (deleted account)`.

> [!CAUTION]
> Once your node has run NetBBS Link, deleting an account also **retires its
> username**: nobody can register it again. The one exception is a
> registration you decline while it is still awaiting approval, unless
> someone on another node had already sent it Link mail. Callers who try a
> retired name are told only that it is unavailable. To free one, for a deleted test
> account or a removed caller who has come back, use
> **Users ▸ Held names** ▸ <kbd>R</kbd> **Release**; the release is
> audit-logged.

<details>
<summary>Why NetBBS holds deleted names</summary>

On the Link a username is the account's identity, so whoever took a freed
name would receive Link mail written to the previous holder and inherit the
authorship of their carried posts and whatever other nodes had recorded about
them. A node that has never run Link keeps reusable names.

</details>

#### Rename an account

A caller asks for a new username under **Profile ▸ Username**. The request is
checked as a signup is, and waits under **Users ▸ Name requests**; the Users
screen and the console's attention panel count it.

- <kbd>A</kbd> **Approve** renames the account. The name is checked again
  first, since it may have been taken meanwhile.
- <kbd>R</kbd> **Refuse** asks for a reason. The caller sees it when they
  next log in.

You can also rename an account yourself with <kbd>C</kbd> **Change username**
on its screen: for a name chosen to offend, or a request that came by mail.
Type the new name; the next screen says what the rename does, and
<kbd>A</kbd> **Apply** makes it.
Your own renames are not limited; a caller's request is, to one approved
change every 30 days. Only you, not staff, rename accounts.

A rename takes effect at once:

- The account's live sessions end, each with a line naming the new username.
- Everything the account owns stays: its id, level, mail, posts, settings and
  door saves.
- Posts, files and sent mail keep the name they were written under. Chat lines
  show the new name.
- Once your node has run NetBBS Link, the old name is **held** for the
  account, under **Users ▸ Held names** as "renamed to ...". Link mail, FTN
  netmail and `/msg` written to it still reach the account. Releasing it
  frees it for anyone, and mail to it then goes to whoever takes it.
- Each request, refusal and rename is in the account's **History**.
- Linked nodes learn the rename from your node's next hello and carry what
  they recorded about the old name over to the new one: a block or a
  quarantine stays. A node that has not heard from yours for 400 days meets
  the new name as a new user.

> [!WARNING]
> An RLogin remote door that keys its players on the handle sees a renamed
> caller as a new player. The confirmation says so.

#### Changes reach callers who are online

A level change, a granted or revoked identity-verifier permission, or a change
to staff permissions reaches a caller who is already logged in without them
reconnecting. This also applies to a change made with
`python -m netbbs.admin`, within a few seconds.

- A raised level shows on their main menu.
- A lowered one takes them out of whatever they were doing, a door game
  included, and back to the main menu with a line naming their new level.
  Text they were composing is kept as a draft.

> [!TIP]
> When access looks wrong, inspect both the account and the resource's
> effective settings, including Community defaults. Access can also depend on
> resource-specific grants, verified age, and verified name, and raising a
> level does not replace identity verification.

### Sharing the work: staff permissions

To hand someone the node's routine work without making them a second SysOp,
give them staff permissions instead of level 255.

**Moderator, staff member or second SysOp?** Pick the smallest that does the
job:

| Role | Covers | Cannot |
| --- | --- | --- |
| **Moderator grant** | One board, file area or chat channel, or, with a blanket scope, every one of a kind in a Community or on the node. On boards and file areas it can allow editing, deleting and approving what callers post or upload; in a channel, changing the topic, muting or banning, and managing an invite-only channel's members. | Open a console, accounts or settings |
| **Staff permissions** | Account work: approving signups, and with **Manage accounts**, disabling accounts, resetting passwords and setting levels up to 254. | Touch your account |
| **Second SysOp (level 255)** | Everything you can do, to your account too: demote it, disable it, reset its password or delete it. | Leave the node without an enabled, approved SysOp; that is the only guard |

> [!WARNING]
> Make someone a second SysOp only if you would hand them the node.

#### Give someone staff permissions

Open their account under **Users ▸ User list** and press
<kbd>S</kbd> **Staff**:

| Permission | What it allows |
| --- | --- |
| <kbd>A</kbd> **Approve accounts** | Approve or decline signups waiting under approval-required registration |
| <kbd>M</kbd> **Manage accounts** | Disable and re-enable accounts, reset passwords, set levels from 0 to 254 |
| <kbd>G</kbd> **Global moderation** | Act as moderator on every board, file area and chat channel, local and carried. It also lets them read and post on every board and file area whatever its level; age and verified-name gates still apply |

- <kbd>C</kbd> **Co-SysOp preset** sets all three in one step.
- <kbd>V</kbd> **Verify identity** beside it gives or takes the right to
  verify identity ([Verified age and names](#verified-age-and-names)).
- <kbd>N</kbd> **None** removes every staff permission; you can also remove
  any one on its own later.

Every change asks for confirmation first and is audit-logged. The account's
level stays whatever it was.

A staff member never acts on a level-255 account or on another staff member,
so they cannot demote, disable or lock out you or each other, and they cannot
widen their own permissions. They also cannot:

- raise anyone to 255;
- delete an account;
- grant staff permissions or moderator grants;
- reach Settings, Link, Node, DNS or backups.

#### What staff and moderators see

A staff member sees <kbd>S</kbd> **Staff** on their main menu instead of
**SysOp**. It opens a reduced console that counts what waits for them and
offers only what their permissions reach:

| Entry | With |
| --- | --- |
| <kbd>A</kbd> **Accounts waiting** | Approve accounts |
| <kbd>U</kbd> **Users** | Manage accounts |
| <kbd>M</kbd> **Moderation** | Global moderation or any moderator grant |
| <kbd>P</kbd> **Pages** | Any staff permission |

These are your own screens with fewer actions: a staff member can open your
account or another staff member's, but only to look.

- Whoever can approve accounts, you included, is told on the main menu when
  signups are waiting ("2 accounts awaiting approval").
- A moderator who may approve posts or uploads anywhere sees
  **Moderation (n)** on the main menu, with how many wait. It opens one queue
  across every board and file area their grants cover.

> [!TIP]
> To make someone a moderator of everything local in one step, grant a
> moderator under **Content ▸ Grant moderator** with the scope
> <kbd>E</kbd> **Everything**: it writes the board, file area and channel
> grants together.

#### Operators and time away

Members find everyone who runs the node under <kbd>O</kbd> **Operators** on
their main menu: SysOps, staff members, and moderators with what they look
after, each with the date of their last session. Being listed is the point,
so a member's choice to stay off Recent callers does not hide them here.
Guests don't see the list.

Going away for a while? Choose <kbd>T</kbd> **Time away** on your console's
landing screen (it is on the Staff console too) and leave one short line,
such as "At a pen show", with the date you expect to be back, or none if you
don't know.

- **Operators** shows it beside your name.
- When everyone who can approve accounts is away, a caller waiting for
  approval is told who is expected back first.
- A notice with a date ends by itself after that day. One without a date
  stays until you end it, reads "away since" the day you set it, and your
  landing screen reminds you of it each time.
- Being away changes nobody's permissions.

#### Pages: callers asking for you

A caller asks for help with `/sysop <reason>` in chat. The page reaches every
SysOp and every account with a staff permission, and waits in one shared
queue: <kbd>P</kbd> **Pages** on your console's landing screen, and on the
Staff console. The reason never appears in the channel, its history, Link or
MRC, and the Monitor and the node log name only the caller and where they
were.

- **In chat or the Monitor** the page shows at once, and chat rings the bell
  as for a private message.
- **Elsewhere** it shows above the prompt of your next screen, and the main
  menu says "1 page waiting: SysOp → Pages". In a door you see it once you
  leave.
- The dashboard counts open pages under **ATTENTION**, and the Staff console
  under **Pages waiting**.

Pick a page to see who sent it, when, from where, their reason and whether
they are still online. Then choose one:

| Key | Does |
| --- | --- |
| <kbd>C</kbd> **Chat** | Opens a break-in chat on their screen, as the Monitor's <kbd>C</kbd> **Chat** does. Shown while they are connected. |
| <kbd>M</kbd> **Mail reply** | Writes to them with their reason quoted. Members only: a guest has no mailbox. |
| <kbd>D</kbd> **Done** | Closes the page. |

Any of the three closes the page for everyone; whoever opens it afterwards is
told who handled it.

> [!NOTE]
> The limits are fixed. A caller has one open page at a time, and pages at
> most once every 10 minutes; paging again replaces the reason. A guest can
> page only while one of you is online. Handled pages are kept for 30 days.
> You and your staff can't page your own node.

### Verified age and names

A SysOp, or an account explicitly granted the right, records attestations
through <kbd>V</kbd> **Verify** on the main menu.

> [!IMPORTANT]
> Establish your own verification procedure outside NetBBS. The software
> records the attestation, not proof that a document or a person's claim is
> authentic.

#### Who can verify

- As SysOp you can always verify; the account screen's **Can verify
  identity** row says so. You can verify yourself too: your own account is
  in **Verify**'s list, and its history entry says `(self)`. A verification
  you make of yourself is never shared over Link, because other nodes
  couldn't tell it from one someone else made.
- For anyone else, switch it on with <kbd>I</kbd> **Identity verification**
  on their account, or <kbd>V</kbd> **Verify identity** on their staff
  screen. They verify others only, never themselves.

The right is separate from making someone a moderator, and the
**Co-SysOp preset** does not include it: vouching for someone's age or name is
a separate trust you give on its own. Applying the preset reminds you of it.

Callers enter the birthdate you verify in **Your profile ▸ Name & details**.

#### Record a verification

1. Press <kbd>V</kbd> **Verify** on the main menu and pick the account.
   The screen shows what the caller says about themselves (birthdate, with
   the age it gives, and display name) and who verified what, and when.
2. Type `01` for **Verified birthdate** or `02` for **Verified real name**,
   or choose one with the arrow keys and <kbd>Enter</kbd>. The value is
   edited where it stands, opened on what is on record:
   <kbd>Enter</kbd> takes it, <kbd>Esc</kbd> leaves it as it was. The
   birthdate is `YYYY-MM-DD`, shown with the age it gives, and a date in the
   future is refused.
3. Press <kbd>S</kbd> **Save**. NetBBS says what it records, what it
   replaces and, if the caller shared the old value over Link, that their
   sharing is switched off until they share the new one. Answer
   <kbd>Y</kbd> to record it. Both values are recorded together, each with
   its own **History** entry.

> [!NOTE]
> Nothing is recorded before you answer <kbd>Y</kbd>. If you answer
> <kbd>N</kbd>, your changes stay on the screen. <kbd>B</kbd> **Back** asks
> before it discards them. If someone else verified or revoked the same value
> while you were typing, **Save** refuses, and the screen shows what is
> stored now.

Emptying a verified value does not withdraw it. Use <kbd>R</kbd> **Revoke**
(below) for that.

#### Require a verified name

A resource's name requirement cycles through:

| Setting | Effect |
| --- | --- |
| None | No verified-name requirement |
| Verified | A name attestation is required; the name is not displayed by this setting |
| Verified and displayed | Verification is required and the attested name is shown alongside contributions in that resource |

A verified name does not replace the account's chosen display name. Tell
callers about disclosure before requiring it.

Each post, upload and chat line keeps the name it was shown under when it was
written. Verifying someone does not add their real name to what they wrote
before, and replacing or revoking a verification, or a new display name, does
not change what they wrote after. An edit, yours included, keeps the post's
original name; to take a real name down, remove the post. Turning **Verified
and displayed** off hides the names; turning it on shows names only on what is
written from then on. Age and name verification are
separate; review the available attributes and expiry on the attestation
screen.

#### Require a verified age

A minimum age accepts the birthdate a caller entered in their profile when
nobody has verified their age. To accept only a verified age, type the age
with a **v** in the **Min age** field (`18v`); the field then reads
`18, verified only`. A Community's default works the same way and is
inherited.

| Caller | What they see |
| --- | --- |
| Old enough by their own birthdate, not verified | The board, area or channel, marked `needs verification`; opening it tells them to ask you |
| Too young, or no birthdate at all | Nothing: it stays hidden |

#### Who is held to it

- As a SysOp (level 255) you pass every minimum age, verified-age and
  verified-name requirement on your own node without verifying yourself, the
  same way you pass every level. A board that sends real names to an FTN
  echo is the exception: it needs your verified name to post (see
  [Real-name echoes](#real-name-echoes)).
- Staff and other accounts do not: a helper you gave staff permissions still
  needs the age or name the resource asks for.
- Callers from other nodes are always held to their own verified age and
  name.

#### Revoke a verification

Use <kbd>V</kbd> **Verification: revoke** on the account's screen; it appears
only while the account has a verified age or real name. With both on record
it asks which, then asks you to confirm, and the account's **History**
records it.

- Anything that asks for a verified age or name refuses the caller again (a
  SysOp excepted).
- Anyone allowed to verify can also revoke, from <kbd>R</kbd> **Revoke** on
  the main menu's **Verify** screen, offered while nothing there is unsaved.
- A verification the caller shared over Link is withdrawn there at the next
  sync.

## Content and Communities

| At a glance | |
| --- | --- |
| **Where** | **Content** in the SysOp console |
| **Callers find it under** | <kbd>M</kbd> **Message boards**, <kbd>C</kbd> **Chat**, <kbd>F</kbd> **Files**, <kbd>G</kbd> **Games** and <kbd>T</kbd> **Topics** on the main menu |
| **Decide first** | Whether you need categories or Communities at all; a node with a handful of boards needs neither |

In this chapter: [Create and edit resources](#create-and-edit-resources) ·
[Each kind of resource](#each-kind-of-resource) ·
[Where callers find it](#where-callers-find-it) ·
[Categories and Communities](#categories-and-communities) ·
[Moderators and grants](#moderators-and-grants)

### Create and edit resources

Create resources under **Content**, or use <kbd>C</kbd> **Create** in a
resource picker. An empty picker remains usable for creating the first
resource. Review the draft and save it explicitly.

Until the node has a board, chat channel or file area, your own main menu
shows where to create one.

**A resource's screen is its editor.** Choosing a Community, category,
message board, file area, chat channel or door opens it on its own fields,
with the cursor on the first.

- To change a field, type its number, or move with <kbd>Up</kbd> and
  <kbd>Down</kbd> and press <kbd>Enter</kbd> (or <kbd>Space</kbd>).
  <kbd>Left</kbd> and <kbd>Right</kbd> step a choice.
- A line above the fields says what the screen is about: a board's posts and
  latest activity, its place in the list and whether it is Linked; a
  channel's MRC room; a door's compatibility.
- Longer details follow the fields: a Linked board's or area's
  **NetBBS Link** section, a channel's **Sharing**, a door's companion
  service. When the screen is paged with <kbd>&lt;</kbd> <kbd>&gt;</kbd> or
  <kbd>PgUp</kbd>/<kbd>PgDn</kbd>, they are on the last page.
- The resource's actions keep their letters (**Up**, **Down**, **Remove**,
  **Pending posts**, **History**, **Link**, **Restrictions**,
  **Compatibility**, the service controls and the rest) and are offered while
  nothing waits to be saved.
- Once a field has changed, only **Save** and **Back** are offered, and
  **Back** asks before discarding the change.

Creating a resource uses the same screen, empty and without actions.

### Each kind of resource

| Resource | What you set and how it behaves |
| --- | --- |
| **Message boards** | Set read/write levels, moderation, retention, and gates. Pending posts remain in an approval queue. Edits retain revision identity. |
| **File areas** | Set access, upload policy, size/retention rules, and moderation. Uploads may include a description extracted from `FILE_ID.DIZ`. A remote catalogue entry is metadata; fetching its bytes is a separate action. |
| **Chat channels** | Set join gates, visibility, invitations, and moderation. Hidden or invite-only channels need more than a sufficient account level. Schedule notices into them with **Announcements** (below). |
| **Communities** | Group related resources and provide inherited defaults. A Community is not an automatic grant to every resource inside it. |
| **Doors** | Attach registered games to a Community, or to none. See [Door games](#door-games). |

Posts and letters can point at files in your areas, and readers download them
from here:

- deleting or expiring a file turns those into "no longer available";
- raising an area's read level hides the file's and area's names from readers
  it now shuts out.

A caller may post one new message to several boards at once (**To boards**,
up to the cap in **Settings ▸ Limits & retention**). Each board gets an
ordinary copy of its own:

- each board's moderation applies to its own copy, so a moderated board may
  hold its copy while the others appear;
- delete, take down, pin and keep act on one copy, and so does an author's
  edit or withdrawal;
- a copy on a Linked board goes out as a post there, and a copy on an echo
  as its own echomail message, starting with `* Crossposted in` and the other
  echoes of that network it went to. Your local and Linked board names never
  go to an FTN network.

> [!TIP]
> Inspect effective gates on child resources before changing a Community's
> shared default.

#### Schedule announcements in chat

The node can say a short notice in a channel on a schedule, with no bot:
a net meeting every Friday, a tournament on the first Saturday of the month,
a maintenance window once.

1. Open **Content ▸ Chat channels**, choose the channel, and press
   <kbd>N</kbd> **Notices**.
2. Press <kbd>C</kbd> **Create** and type the **Text** (up to 160
   characters). It is shown muted as `Announcement: ` and your text.
3. Set **Repeat** to once, daily, weekly or monthly, and fill in the fields
   for it: a **Date** as `2026-10-23`, the **Weekdays**, or the **Week of
   month** (first to fourth) and **Weekday**.
4. Set the **Time** as `19:00`, in the node's display timezone. The line
   above the fields shows when it is next said.
5. Press <kbd>S</kbd> **Save**.

- It is recorded in the channel even when no one is there, so a caller who
  arrives later finds it in the scrollback.
- A channel holds at most 8 announcements, and Save refuses one whose time
  of day is within an hour of another in the same channel, whatever their
  days.
- A slot the node misses (it was not running) is skipped, not said late.
- <kbd>D</kbd> **Delete selected** removes one. A once announcement already
  said stays listed as `done` until you delete it.

> [!NOTE]
> In a Linked channel announcements stay on this node until you approve the
> channel for them: turn on **Other nodes (whole channel)** in any of its
> announcements, and Save asks you to confirm. Each announcement then goes
> to the other nodes as well, signed by the SysOp who saved it. Newer nodes
> show it as an announcement from your BBS; older ones as an ordinary line
> from that SysOp. If that account is deleted or is no longer a SysOp, its
> announcements stay on this node until another SysOp saves them.

#### Order boards, file areas and Communities

Callers' lists follow your order, not the latest activity, so the number a
caller remembers keeps its board.

- A new board, area or Community goes last.
- <kbd>U</kbd> **Up** and <kbd>D</kbd> **Down** on its screen move it. A board
  or area moves among the others in the same category and Community; pinned
  ones stay first and move among themselves.
- <kbd>R</kbd> **Remove** deletes it. A removed Community's resources stay,
  without a Community.
- A caller can still sort a list by activity with <kbd>O</kbd> **Order**.

### Where callers find it

- The main menu's <kbd>M</kbd> **Message boards**, <kbd>C</kbd> **Chat** and
  <kbd>F</kbd> **Files** list everything of that kind on the node, whichever
  Community it belongs to. A resource with no Community is simply listed
  there, so you do not need a Community to make a board easy to find.
- <kbd>G</kbd> **Games** appears for a caller once a registered door is open
  to their level.
- <kbd>T</kbd> **Topics** appears once a Community exists and shows each
  Community's description and what it holds. Give each a description,
  because that is what callers read first.

The [user handbook](NetBBS-User-Handbook.md) covers posting, drafts, follows,
search, mail, and everyday chat. **New scan** and **Find** only show content
the caller can access on this node, including carried linked content.

### Categories and Communities

They are different things, and a resource can have both:

| | Category | Community |
| --- | --- | --- |
| **Groups** | The list of one kind (board categories group boards) | Every kind at once, around a topic |
| **Shows up as** | A folder in that list | An entry under **Topics** |
| **Create under** | **Content ▸ Categories** | **Content ▸ Topics** |

Pick a category in a board's, area's or channel's **Category** field.
<kbd>Ctrl</kbd>+<kbd>H</kbd> on the Categories screen says the same. A node
with a handful of boards needs neither.

### Moderators and grants

Moderator grants belong to a resource or Community. Membership alone does not
make someone a moderator. Grant only the scope required; use approval queues
and the audit log to review actions. Chat moderation commands are used inside
the channel by someone with the appropriate authority.

When a moderator rejects a post, its author gets a mail from **System** --
the BBS, not the moderator's own account -- with the reason, who decided, and
the text.

- It cannot be replied to.
- It counts toward the author's mailbox limit but is the first read mail
  removed to make room.

#### Let a helper past a level

A read or write grant on a board or file area lets its holder past that
resource's minimum read or write level. To let a helper post on an
announcements board whose write level is 255, grant them the
**Read and post** preset on that board rather than raising their level
(**Read only** opens reading alone).

> [!WARNING]
> A grant opens only what its scope covers: one board or area, or, with a
> blanket scope, every board or area of that kind on the node or in one
> Community. Pick the scope with care.

The minimum age and verified-name requirements still apply.

## Door games

| At a glance | |
| --- | --- |
| **Where** | **Content ▸ Doors** in the SysOp console |
| **Callers find it under** | <kbd>G</kbd> **Games** on the main menu, or a Community's page |
| **Outside NetBBS** | Games, licenses, runtimes and installation directories: see the [door setup guide](NetBBS-door-guide.md) |

### Register a bundled game

Open **Content ▸ Doors ▸ Gallery** to register Retro Trivia, Voidrunner,
War Dialer, or Lunar Lander. Lunar Lander needs a terminal of at least 80x24;
it pauses the flight on anything smaller until the caller enlarges it. Gallery entries fill a draft with the installed interpreter and
sensible defaults; review and <kbd>S</kbd> **Save**.

The games release on their own schedule. To run a newer release of one before
NetBBS bundles it, follow the door guide's
[Running a newer game release](NetBBS-door-guide.md#running-a-newer-game-release).

### Set up any other door

Third-party native programs, DOS games through DOSBox-X, doors built only for
another platform (in a per-caller qemu VM you provision), remote RLogin
services and BBSLink use compatibility profiles.

1. Start with minimum play level 255.
2. Choose a template.
3. Set your actual paths.
4. Run **Check setup**.
5. Run **Test as SysOp**.
6. Test normal quit, disconnect, timeout, and each offered caller transport
   before opening it to users.

> [!WARNING]
> Testing launches real programs and may change game data, even if you later
> discard the configuration draft.

> [!IMPORTANT]
> **MANUAL — outside NetBBS:** obtain games and licenses, install their
> runtimes, prepare writable installation directories, and configure any
> remote-service tunnel and credentials. NetBBS does not install these
> components. Use the [door setup guide](NetBBS-door-guide.md) for exact
> profiles, game versions, DOSBox-X patches, service controls, and
> troubleshooting.

#### Upload your own door scripts

Your own door scripts can be sent from inside NetBBS:
**Content ▸ Doors ▸ Upload** asks for the file's name, then takes one file
over Zmodem or a browser link into the node's doors folder, the one
<kbd>F</kbd> **From disk** lists.

- Replacing a file of the same name asks first.
- The upload registers nothing and changes no permissions; register it with
  **From disk**.
- It is capped at the node's upload limit and recorded in the audit log.

### What a door can reach

> [!CAUTION]
> Native doors run as the NetBBS service account. Resource limits do **not**
> prevent them reading files or using the network as that account. Install
> only trusted programs.

A door's temporary launch directory is deleted afterward; persistent game
data must live elsewhere.

### Companion services

A companion service keeps a game's world process alive between callers.
NetBBS can supervise it, and the door detail screen offers
<kbd>S</kbd> **Start service**, <kbd>H</kbd> **Halt service**,
<kbd>R</kbd> **Restart service** and <kbd>V</kbd> **View service log** (its
recent log).

- Install/update the service yourself.
- Stop it before copying its data.
- Do not raise a game's session count until its concurrent save handling has
  been tested.

### War Dialer's world

A War Dialer door's detail screen adds <kbd>W</kbd> **World**: the world's
status and recent SysOp operations, and a maintenance switch
(<kbd>M</kbd>) that closes it to new callers. On a live node with maintenance
on, it also offers <kbd>N</kbd> **Next season** and
<kbd>C</kbd> **Competition reset**. Both take a verified node backup first and
ask for a reason and the world's filename. The
[door guide](NetBBS-door-guide.md) has the details and the stopped-node CLI.

### Doors that post to boards

A door may request permission to post to message boards.
<kbd>O</kbd> **Outbound** is off by default and configured per door: choose an
allowlist and posting limit.

- Posts use a distinct door label, not the caller's identity.
- Normal board moderation applies.

> [!WARNING]
> Allowing a linked board can distribute posts to peers; local deletion
> cannot retrieve copies already received elsewhere.

### Announce door game results

A door can report ranks, top achievements, season medals, captures and perfect
rounds, and a top-ten table, for a chat bot to announce and for `!top <door>`.

1. Open the door's <kbd>O</kbd> **Outbound** screen and switch the hook on.
2. <kbd>E</kbd> **Events on**. <kbd>G</kbd> **Game events per hour** sets its
   own ceiling (12 by default).
3. In the bot's editor under **Content ▸ Open chat bots**, add the door to
   **Door-event sources**, choose an **Announcement channel**, and switch on
   **Baud door announcements**. **Grant door standings query** enables
   `!top`.

> [!NOTE]
> NetBBS words each announcement. Only doors every caller may play (minimum
> play level 0) can be sources, and a door whose level you raise later stops
> being announced. Callers who switch **Game results announced in chat** off
> are never named, and are left out of `!top`. Guests and test launches are
> never announced. The [door guide](NetBBS-door-guide.md#letting-it-report-game-events)
> has the details.

## NetBBS Link

NetBBS Link connects independent NetBBS nodes. It carries selected message
boards, chat channels, file catalogues, and mail. Your node decides what to
carry and which peers to trust. A signature identifies who sent something;
it does not make that sender trustworthy.

| At a glance | |
| --- | --- |
| **Where** | **Settings ▸ Join NetBBS Link**, **Operations ▸ Link status**, **Settings ▸ Policy trust**, **Operations ▸ Mail** |
| **From the shell** | `python -m netbbs.admin export-node-map`, `python -m netbbs.admin rotate-key` |
| **Decide first** | Outgoing-only (the default, no port forwarding) or a full peer |

> [!NOTE]
> **Current boundary:** asynchronous services, trust controls, live chat,
> presence, and live relay are implemented. Federation remains private and
> experimental pending [public-readiness validation](NetBBS-phase4-readiness.md)
> and independently operated dogfood. Independent implementation
> compatibility is not yet established. Link Communities are available;
> cross-node `/dm` invitation chats are not. Cross-node `/msg` and
> `/private` do work when a live path exists.

In this chapter: [Join and share](#join-and-share) ·
[Link mail](#link-mail) · [Behind an HTTP proxy](#behind-an-http-proxy) ·
[Trust and recovery](#trust-and-recovery) · [Managed names](#managed-names) ·
[Node keys](#node-keys)

### Join and share

#### Join the Link

1. Install the `web` extra, even if callers use only SSH.
2. Enable participation through first-run onboarding or
   **Settings ▸ Join NetBBS Link**, unless TOML explicitly overrides it.
3. Use a distinct node name.

The default outgoing-only mode uses reliable nodes for discovery and relay; no
Link port forwarding is needed. Caller access still needs its own reachable
listener.

Reliable nodes are bootstrap/relay infrastructure, not owners of your BBS.
Manual seeds can supplement them. A full peer additionally advertises a
reachable Link address and needs the corresponding network setup.

#### Run as a full peer

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

> [!IMPORTANT]
> **Manual, outside NetBBS:** replace the hostname and allow/forward both TCP
> ports to this node. Restart and have another operator check asynchronous
> exchange and live chat separately.

If omitted, the real-time port is the HTTP Link port plus 1000. Keep
outgoing-only mode if you cannot provide this reachability. The
[developer handbook's configuration reference](NetBBS-Developer-Handbook.md#node-configuration-reference)
lists the less common transport and quota settings.

#### What your node carries

A board, channel or file area needs no subscription: what an established peer
shares arrives on its own, within the caps. Linked boards, channels and file
areas from other nodes are carried automatically up to the `max_carried_boards` /
`max_carried_channels` / `max_carried_file_areas` caps (500 each;
**Settings ▸ Operating limits ▸ Carry caps**).

- **Past a cap**, a new one is not lost: it waits under
  **Link status ▸ Offered**, with its origin and why, until you
  <kbd>A</kbd> **Accept** it (not limited by the cap) or
  <kbd>E</kbd> **Exclude** it.
- Set a cap to 0 to carry only what you accept.
- Lowering a cap removes nothing already carried.
- Link status shows `carried/cap` for all three kinds and how many are offered
  and excluded.

A node's screen under **Link status ▸ Peers** lists what this node already
carries from it and, while it is on probation here, what it offers that is
being held back.

- Carrying a message board creates a local browsable copy; file catalogues do
  not automatically download all file contents.
- Carried boards, channels and file areas appear in callers'
  **Message boards**, **Chat** and **Files** lists, outside any Community,
  unless you subscribe to the other node's Community
  ([Subscribe to a Community](#subscribe-to-a-community)). Edit one and set
  its **Community** to put it in one of yours.

> [!TIP]
> Ask the other SysOp to verify both sides when first testing publication.
> Hello/discovery alone does not prove content arrived.

#### Remove a carried resource

Deleting a carried resource that another node originated excludes it without
destroying it: callers stop seeing it and this node stops carrying it, but
everything in it -- your users' posts, your moderation -- is kept.
**Link status ▸ Excluded** lists it:

- <kbd>R</kbd> **Restore** brings it back exactly as it was (what arrived
  meanwhile follows with the next sync).
- <kbd>P</kbd> **Purge** deletes it for good.

Its name stays taken until you do one or the other.

> [!CAUTION]
> Deleting a board, channel or file area this node originated is still a real
> delete.

#### Share your own boards, channels and file areas

Create a local resource before promoting it to linked scope. Linking one of
your own boards, channels or file areas sends it to the peers you have
established, on a later sync pass. Its **NetBBS Link** rows then show, for each
peer, whether it:

- holds it,
- refused it because your node is on probation there, or
- is not sent it while it is on probation here.

A node learns this only from peers it dials itself, so a peer that only dials
yours reads "not known". **Fork of** is for a board that carries on another
node's board under yours, for example after that one was closed; leave it
empty for a board of your own.

#### Share a Community

A Community of yours can be Linked too, so other nodes can carry its topic as
one. Open it under **Content ▸ Topics ▸ List** and press
<kbd>L</kbd> **Link this Community**, then confirm.

- Other nodes are offered it with its name, description and default levels and
  gates, and the list of its message boards, chat channels and file areas that
  are already Linked from here.
- Linking the Community links nothing else. A private board filed under it stays
  private: link it on its own screen when you want it shared, and it joins the
  list with the next sync pass. Moving a Linked board in or out, or unlinking
  it, updates the list the same way.
- Boards, channels and file areas you carry from other nodes are never listed:
  a Community lists only what this node shares itself.
- The Community's screen shows its **NetBBS Link** section: whether it is
  Linked and how many members it lists.

<kbd>C</kbd> **Close on Link** closes it on every node that carries it, for good.
They keep every board, channel and file area they carry from it; their copy
becomes an ordinary Community there. It stays here as a local Community, and you
can link it again later as a new one. <kbd>R</kbd> **Remove** on a Linked
Community closes it the same way before deleting it.

> [!NOTE]
> Removing a Linked Community needs the running node, which signs its closure:
> the shell's `python -m netbbs.admin` console refuses it.

#### Subscribe to a Community

A Community another node shares is never carried on its own. It waits under
**Link status ▸ Offered** with its origin and how many members it lists:

- <kbd>A</kbd> **Accept** subscribes. A copy of the Community is created here
  (if one of yours already has its name, a short suffix is added), with its
  origin's default levels and gates, which you may change.
- Its boards, channels and file areas are carried and filed in the copy, now
  and as the origin adds more, within the carry caps. One past a cap waits
  under **Offered** with the reason "cap", like any other; accept it there.
- Accepting also carries what the Community lists that is already waiting
  under **Offered**, within the caps. After that, something offered to you
  before is left for you to decide, even when a later list names it again.
- A member you file under another Community stays where you put it. A member
  the origin takes out of the Community moves out of the copy and stays
  carried.
- To keep one member out, <kbd>E</kbd> **Exclude** it on its own offer, or
  remove it as any carried resource.

The copy shows in callers' **Topics** and in your Community list with the
Linked marker. Its screen names the origin.

To leave a Community, <kbd>R</kbd> **Remove** its copy. Nothing more arrives
from it and nothing you carry is removed; the members just lose their
Community. It is listed under **Link status ▸ Excluded**, where
<kbd>R</kbd> **Restore** subscribes again.

When the origin unlinks a Community, your copy becomes an ordinary Community,
marked as closed at its origin. You keep everything in it, and you may keep or
remove it.

#### Who may post on a linked board

On a Linked board this node originated, the board's screen has
<kbd>W</kbd> **Who posts**, which steps through three settings that every node
carrying the board follows:

| Setting | Use |
| --- | --- |
| **anyone** (the default) | Every node's callers post, each node's own write level holding its own callers |
| **the origin's callers start threads; anyone replies** | Announcements people can answer |
| **the origin's callers only** | Announcements nobody else writes in |

- A post from another node that the setting does not allow is not shown
  anywhere, and callers on other nodes are not offered <kbd>P</kbd> **Post**
  (or <kbd>R</kbd> **Reply**) where it would be refused; the board tells them
  why.
- The setting reaches other nodes with the next sync pass. Every board's
  screen shows it as **Who posts**.
- Your own write level still holds your own callers: an announcements board
  is write level 255 here and "origin's callers only" everywhere.
- A closed board takes no new post from any node.

#### Moderators on other nodes

**Link status ▸ Moderators** lets users of other nodes moderate the linked
boards and channels this node originates, for everyone who carries them.
Your own node's moderators need no grant here: their moderation already
reaches every node.

- <kbd>A</kbd> **Add** opens a form. **User** is the user as `name@TheirBBS`.
  **On** is what they moderate: one linked board or channel, one of this
  node's Link Communities, or everything this node originates. **May** is what
  they may do there: edit and remove posts, mute and ban, set the topic, or a
  part of that. Nothing is granted until you <kbd>S</kbd> **Save**.
- <kbd>R</kbd> **Remove** takes a grant back. Each node stops accepting that
  user's moderation as the change reaches it with its next sync pass.
- The section **Held by this node's users** lists what your own users may
  moderate on other nodes. Such a user edits and removes posts on those
  boards here as a moderator would, and the change reaches every node
  carrying the board. Their confirmation says so, instead of "on this node
  only".

**Mutes and bans in linked channels.** In a linked channel, `/mute`, `/unmute`,
`/ban` and `/unban` also take a caller from another node as `name@TheirBBS`.

- On a channel this node originates, a moderator's mute or ban reaches every
  node carrying the channel, for this node's callers and other nodes' alike.
  So does one by a user this node's grant names, on their own node.
- Every carrying node then hides that caller's messages in the channel. The
  caller's own node also enforces it: a muted caller cannot speak there, and
  a banned one cannot enter, or speak if already inside.
- A moderator on a carrying node without such a grant mutes or bans a caller
  from another node on this node only. That never lifts a mute or ban from
  the channel's origin, and the origin's always outranks a delegated
  moderator's.

**Topics of linked channels.** A linked channel has one topic everywhere: the
newest one set on the node it comes from, or by a user that node's grant
names. On a channel this node carries, `/topic` is refused for anyone else,
because a local topic would contradict the shared one. A linked channel's
topic is at most 300 bytes.

#### Handing a linked board or channel to another node

The node a linked board or chat channel comes from is its *origin*. It can
hand that role to another node, for example before it shuts down.

1. On the origin, open the board or channel and choose <kbd>T</kbd>
   **Transfer origin**. Pick the node and confirm. This only makes an offer:
   nothing changes yet.
2. On the node offered it, the same screen shows the offer. Its SysOp chooses
   <kbd>A</kbd> **Accept transfer**.
3. From then on every carrying node treats the new node as the origin, as
   each one learns of the acceptance with its next sync pass. The new origin
   decides the channel's topic, mutes and bans, and its own moderator
   grants count there.

Only one offer can wait at a time, and an offer cannot be withdrawn.

#### Trust and history

- A mute, ban, topic, or post edit or removal from another node does not take
  effect here when that node is quarantined or blocked here, or when the
  moderator who made it is. Nor does a grant from an origin quarantined or
  blocked here: your users lose what it let them do on its boards and channels.
- A linked board's or chat channel's <kbd>H</kbd> **History** lists those
  that took effect, with who made them, under its moderators. The moderators
  include the users of other nodes that the origin's grants name.

> [!NOTE]
> Grants reach only nodes running a version that understands Link moderation.
> An older node keeps showing what newer nodes hide.

#### Polls

Callers ask polls with <kbd>N</kbd> **New poll** on a board's list (not on a
board gated to an FTN network). A poll is a post: moderation, expiry,
withdrawal and takedown apply to it, and a takedown or withdrawal closes the
poll and hides its results. You and the board's moderators can end a poll
with <kbd>C</kbd> **Close poll**.

- **The ballot is secret on screen only.** No screen shows who voted what. The
  database records each account's vote (`board_poll_votes`), so anyone with the
  database file can read it; the User Handbook tells callers so.
- **On a linked board** the poll travels with its post, and each vote is a signed
  event naming its voter, so every carrying node holds who voted what. Its
  answers are fixed when it is posted; only the closing date changes, by the
  author or, on the origin, a moderator. A moderator elsewhere closes a poll on
  their own node only.
- **Counting is per node.** Votes from a node or author your trust policy
  hides are not counted, at most 50 voters from each other node are counted per
  poll, and at most 5,000 voters are kept per poll. The results say how many
  votes were not counted, so two nodes can show different totals for one poll.

### Link mail

Asynchronous delivery can continue after a peer reconnects. Live chat and
private messages require a working live session; a failed live message is not
silently converted to mail. Link mail is encrypted to the recipient's home
node for ordinary accounts; the home-node operator can read it.

Link mail follows node trust:

- Once you establish a node -- <kbd>E</kbd> **Establish** on its screen under
  **Link status ▸ Peers**, or under **Settings ▸ Policy trust ▸ Subjects** --
  mail from all its callers is delivered here, even from callers still on
  probation; their posts still wait in the approval queue.
- Mail from a node you have not established yet, or from a caller or node you
  quarantined or blocked, is refused and bounced back to its sender.
- The same holds the other way: your callers cannot address mail to a node you
  have not established, and are told so as they type the address.
- Mail already waiting in the **Outbox** for a node goes out on the next Link
  pass after you establish it, and expires if the node is not established
  before its retries run out.

An address is written as its name is displayed, capitals included. An account
from before the username rules whose name has a space or other punctuation
cannot send Link mail, because no reply could reach it; rename it if its owner
needs to.

#### Mail screens

**Operations ▸ Mail** shows the other side: mail other nodes sent here that
this node refused, and how full your callers' mailboxes are.

<kbd>R</kbd> **Refused Link mail** lists each refused letter with its sender,
the reason in plain words (its node is still on probation here, the sender or
node is blocked, no such account, a mailbox full of unread mail, ...), when it
was last refused and how many times its sender tried.

- Open one to see its node's and its sender's trust here.
  <kbd>N</kbd> **Node trust** or <kbd>U</kbd> **User trust** takes you to that
  subject's trust screen to establish or block it.
- A letter refused because its node is on probation is the common case:
  establish the node, and the sender's next attempt is delivered.
- "The recipient's account is disabled or still awaiting approval" is mail for
  an account that takes none at the moment.
- The list keeps the 500 most recent refusals, and no more than 50 from any one
  node.

<kbd>M</kbd> **Mailboxes** lists every account with mail, fullest first
(<kbd>O</kbd> **Order** switches to by name): letters, unread, read, kept (in
the caller's Kept folder), notices from the BBS itself, and how much of the
500-letter cap the Inbox takes.

- Kept letters are part of Letters but not of the cap: Kept has its own limit
  of 100 per caller, and the Kept column is flagged when one is full (the
  caller cannot keep another until they move some back or delete some). So
  each caller holds at most 600 letters.
- A full inbox makes room by dropping its oldest read letter; an inbox full of
  unread mail refuses new mail, and a Link sender gets a "mailbox full"
  bounce.
- Callers see the same count in their own Inbox header, a warning from 450,
  and a main-menu line when old read mail was removed to make room.

> [!NOTE]
> These screens never show what a letter says. Mail is private: you see
> counts, account names, senders and reasons, never a subject or a body, and a
> refused letter does not even record whom it was for. Keep in mind that Link
> mail is encrypted to the recipient's *node*, not to the person, so a SysOp
> with access to the database could read it there; NetBBS gives you no screen
> for doing so.

#### Write to every caller

Two entries on the same screen send one letter to every account on this BBS
that takes mail:

| Entry | Sent as |
| --- | --- |
| <kbd>W</kbd> **Write to all callers** | From your own account: signed like your other mail, it sits in your **Sent** as one letter, and callers can reply to you |
| <kbd>N</kbd> **Notice to all callers** | From the BBS itself, as **System**: unsigned, not in your Sent, and no one can reply to it; it reaches your own Inbox too |

- Both open the ordinary compose screens -- Subject, your editor, the review
  screen -- and keep a draft of their own. The review screen says how many
  accounts it will reach.
- The guest account, disabled accounts and signups awaiting approval are left
  out, and so is anyone whose mailbox is full of unread mail: each caller gets
  their own copy, under the same 500-letter cap as any letter.
- After Send you are told how many callers it reached, whose mailbox turned it
  away (by name), and how many accounts were left out.
- It is local only: callers on linked BBSes are not written to.
- A letter already sent is never sent twice, even if you send its draft again
  after a dropped connection.
- <kbd>A</kbd> **Attach file** on the review screen points it at files in your
  file areas, as any letter can; a caller who may not open one of those file
  areas is skipped, and named after Send, like a full mailbox.

#### What callers see of their Link mail

Your callers see each Link message's state in their **Sent** mail: pending,
with relay, delivered, bounced (with the other node's reason in plain words) or
expired. A caller whose mail bounces or expires is told once at their next main
menu.

"With relay" is mail your node left at a relay because the recipient's node
cannot be dialed directly. The relay is not the recipient, and one refusal
never comes back as a bounce: mail for a node that has *your* node quarantined
or blocked, which sends yours nothing. So:

- If no answer comes back within 14 days of the handoff, the letter expires
  and its sender is told that no answer came back, so it may not have arrived.
- An answer that arrives later still counts: the letter turns delivered, or
  bounced.
- A relay keeps a letter well past those 14 days, so a recipient that is only
  slow to collect still answers in time.

In the **Operations ▸ Outbox**, <kbd>C</kbd> **Cancel** on a delivery still
retrying gives up on it at once: its letter expires, and its sender is told
you stopped trying to send it. A delivery that gave up or was cancelled stays
in the Outbox's list. <kbd>R</kbd> **Replay now** puts it back to pending,
with its full retry schedule again, and its letter is pending again too. When
the recipient is gone for good, <kbd>D</kbd> **Delete** it instead: the work
item goes, the letter stays expired, and Link stops reporting it as needing
attention.

#### Mail arriving here

Mail arriving here is checked the way your own callers' mail is:

- A sender name that is not a valid address, a blank or oversized subject, or
  an oversized body is bounced as malformed.
- A letter this node cannot decrypt is bounced as such rather than as "no such
  user".
- A received letter shows the date its sender wrote it, unless that date is
  before 2000 or more than five minutes ahead of your clock, when it shows the
  arrival time instead.
- The inbox lists mail in the order it arrived, so a letter that took days to
  get here is still at the top.

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

#### Reachable through corporate proxies (reliable-node operators)

> [!IMPORTANT]
> **Manual, outside NetBBS, for reliable-node operators:** many corporate
> proxies allow `CONNECT` only to port 443. To be reachable live from such
> networks, advertise the real-time port as 443 and forward it to the
> real-time listener.

```toml
[link]
realtime_port = 8862
realtime_advertised_port = 443
```

NetBBS does not bind 443 itself. If 443 on that address already serves HTTPS,
use a second address, or a protocol demultiplexer in front of both.

### Trust and recovery

| Look at | For |
| --- | --- |
| **Operations ▸ Link status** | Peers and relay state |
| **Operations ▸ Outbox** | Pending or failed work |
| **Operations ▸ Diagnostics** / **Operations ▸ Watch log** | Explanations |

#### End probation

Every node starts on probation with every other, in both directions:

- A peer on probation here sends nothing this node accepts, and this node
  sends it nothing of yours.
- Your node is on probation at each peer the same way until that peer's SysOp
  establishes it.

Automatic graduation takes at least 30 days, three days of contact and vouches
from two trust domains, so on a node with no trusted reporters only
**Establish** ends it.

1. Establish a peer once you know who runs it.
2. Ask its SysOp to establish yours.

**Link status** counts the peers on probation here and says whether the peers
your node dials still hold yours on probation; the SysOp console's LINK line
counts them too.

#### The Waiting list

Some things Link cannot use or deliver are put off for an hour rather than
retried every pass:

- content held back because its sender is on probation here or not yet known;
- your node's content a peer refused because its author is on probation
  there;
- a node no carrier could introduce;
- trust objects a relay refused.

**Link status ▸ Waiting** lists each with when it is next tried; the section is
absent when nothing waits. These are waits, not failures. Once you have fixed
the cause -- established a node, asked a peer's SysOp to establish yours, named
a reporter -- press <kbd>R</kbd> **Retry now**: it ends every wait and runs a
sync pass at once.

A trust decision in **Settings ▸ Policy trust** already retries what it
concerns and starts a pass by itself, and a change made with
`python -m netbbs.admin` is picked up by the running node on its next pass.

#### The node map (Peers)

**Link status ▸ Peers** is the node map: every node this one knows, as callers
see it under **Directory ▸ Node map** ("Nodes known to" your board), plus what
callers do not see.

- A node on probation here says when probation could end by itself and what is
  still missing; its **Exchange** rows say what is held back from it and
  whether it takes what yours sends.
- Peer-list candidates are marked unverified and "never heard from".
- Nodes you quarantine or block in any trust dimension are marked, with each
  dimension's state, because callers do not see them at all.
- Each node's screen adds its Link addresses, relay roles and reliability.
- Last heard is your own last contact with the node, or the time its newest
  descriptor says it was signed, never later than when you first stored it.
- A node not heard of for 30 days is marked stale, not removed.

#### Act on a node's trust

A node's screen also acts on its trust, when it is a trust subject here (every
node that has exchanged a hello with yours, or been introduced to it; not a
peer-list candidate):

- <kbd>E</kbd> **Establish** and <kbd>D</kbd> **Deny** (a block) open the
  override editor with all three dimensions and the state already chosen, so
  you only give a reason and save.
- <kbd>C</kbd> **Clear override** removes one override, or all of them at
  once.
- <kbd>T</kbd> **Trust details** opens the node's full trust screen from Policy
  trust.

The same **Establish** and **Deny** are on every subject's screen under
**Settings ▸ Policy trust ▸ Subjects**, and <kbd>O</kbd> **Override**'s
**Dimension** offers <kbd>A</kbd> **All three**.

#### Relay mailbox

**Link status** also shows what your node holds as a relay. When it relays for
outgoing-only nodes, mail and delivery answers for them wait here until they
dial in and collect them.

- The **Relay mailbox** line counts what is held, and a table below it lists
  each node held for, with how many envelopes it has (at most 50) and how long
  the oldest has waited, oldest first.
- Anything left uncollected for 30 days is dropped on the next sync pass, and
  the diagnostic log gets a warning naming the node and how many went.
- Neither end is told by your node, which cannot read or sign that mail; the
  sender's own node gives up on a letter handed to a relay after 14 days and
  tells its writer that no answer came back.

> [!TIP]
> A node whose count stays at 50 for weeks is most likely not coming back; the
> time limit clears it without you doing anything.

#### Dial-in addresses

**Link status ▸ Dial-in** sets the addresses other nodes show callers on their
node maps: up to four, each `telnet://host:port`, `ssh://host:port` or an
`https://` URL (plain `http://` is refused).

1. Choose the **Suggested** row to fill the draft from your DNS name, your
   enabled Telnet and SSH listeners and an `https://` `[web] public_url`.
2. Adjust the addresses.
3. Press <kbd>S</kbd> **Save**. Nothing is saved until then, and peers get the
   list with the next hello.

Until you save a list, the node publishes `[web] public_url` when it is
`https://` and the web listener was on at the last start. Saving an empty list
publishes nothing. The **Dial-in** row on Link status shows what is published
now and where it came from.

#### Policy trust

Policy trust settings separate identity integrity, resource behavior, and
content conduct: a complaint about content should not be treated as proof of a
forged identity.

Under **Settings ▸ Policy trust**, inspect a subject's effective state and
history before applying an override.

- Domains and reporters determine whose signals and vouches count; multiple
  identities from one trust domain do not become independent votes.
- A reporter's signals count only within its scopes, and only once it is
  established here.
- Quarantine restricts exchange; it does not erase previously accepted
  content.
- A sole-authority exception deliberately weakens the usual multi-source
  policy and is marked as a safety deviation.

#### Nodes you have never spoken to

Your node will carry content from nodes it has never spoken to. Two nodes that
share a board through the same seed usually never contact each other, and two
outgoing-only nodes cannot. Your node learns who such an author's node is from
the node that carried the post, checks that identity for itself, and lists it
under **Settings ▸ Policy trust ▸ Subjects**, where its screen says which node
it was learned from.

- Like every new identity it starts on probation, so its content is withheld
  until you set both its identity integrity and its resource behavior to
  established with <kbd>O</kbd> **Override**.
- Its callers are subjects of their own and start on probation too, so their
  first posts then arrive in the board's approval queue.
- Such a node never leaves probation by itself, because your node has no
  direct dealings with it to judge.
- A name you recognise under a different technical identity is flagged, and
  when the name belongs to a node yours has met, the warning falls on the other
  one.

> [!TIP]
> If a board looks quieter than you expect, look in both places: the subject's
> override and the board's approval queue.

#### Vouch for an identity

To stand behind an identity for the benefit of other nodes:

1. Open it under **Settings ▸ Policy trust ▸ Subjects**.
2. Press <kbd>V</kbd> **Vouch** and give a reason, which is published with the
   vouch.
3. Confirm once.

Your node signs the vouch and serves it to nodes that have named yours a
trusted reporter, where it counts toward ending that identity's probation. It
changes nothing on your own node; to establish an identity locally, use
**Override**.

- The other side of that: naming a node a trusted reporter is not enough for
  its vouches to reach you. Your node pulls and counts a reporter only once
  that reporter is established here, by override or by graduating from
  probation.
- **Policy trust ▸ Vouches** lists everything your node vouches for and
  withdraws any of it.
- Vouches renew themselves.
- If you quarantine or block an identity, your node stops vouching for it on
  its own and resumes if you lift the restriction.
- Your node cannot vouch for itself or for its own callers.

If nobody can dial your node, which is the ordinary case behind a home
connection, what it vouches for is handed to the nodes that relay for it and
fetched from there. The vouch screen says how many those are, warns when there
are none, in which case a vouch reaches nobody yet, and warns when one of them
did not take your vouches at the last attempt, which the Link log explains. A
node that names yours a trusted reporter needs to have met one of your relays.
Verified ages and names travel differently, sealed to each recipient
([below](#share-verified-ages-and-names)), so they reach recipients whether or
not anyone can dial your node.

#### Equivocation

When your node sees another node sign two different objects into the same
place in one chain -- two edits of the same version of a post, two successors
of one key change, two closures of one board -- it keeps both, quarantines that
node's identity integrity on yours, and logs it.

Unless you turn it off, it also signs a short signal saying so, with both
objects attached, for the nodes that name yours a trusted reporter. They check
the proof themselves, and it counts there only together with a report from a
second, independent trust domain. **Policy trust ▸ Conflict signals** lists
what your node published, turns automatic signals off (which also withdraws
the published ones) or back on, and revokes one signal while keeping the
evidence. At most five are signed a day, and never about your own node.

> [!IMPORTANT]
> The quarantine does not lift on its own when the evidence expires: open the
> node under **Subjects**, look at **Observed here**, and press
> <kbd>R</kbd> **Remove evidence**. That also revokes your node's signal about
> it.

Honest nodes fork too. A node restored from an older backup, or a copied VM
running with the same identity, will sign things its earlier self already
signed. If another node quarantines yours for equivocation, that is usually
what happened: make sure only one copy of your node runs, then ask that node's
SysOp to clear the evidence.

#### Share verified ages and names

Verified ages and names cross the Link only when three people agree:

1. The caller switches sharing on in their Profile.
2. You name the receiving node under
   **Settings ▸ Policy trust ▸ Published identity ▸ Recipients**. The list
   starts empty, and until you add a node nothing leaves yours whatever callers
   have switched on.
3. The other SysOp names your node under **Identity authorities** on their
   side.

A verification you made of yourself never crosses: your Profile shows its
sharing as `off (you verified it yourself)`.

Your node then sends each recipient one sealed snapshot of everything it should
hold from you: directly if the recipient can be dialed, otherwise through the
relays that recipient names. Only the recipient can open it; a relay sees that
your node sent it something, roughly how large, and when.

- A changed snapshot goes out on the next Link sync pass, and an unchanged one
  again every week.
- **Published identity** lists each recipient with how it was last reached,
  when, and whether that was the current snapshot, or why the last attempt
  failed (for example, a recipient nobody can dial that names no relay your
  node has met).
- A recipient running NetBBS v7.14.0 or earlier receives nothing from yours
  until its SysOp upgrades; its row says it needs a newer NetBBS.
- Each caller sees how many nodes hold their own value, never which.

> [!TIP]
> If you have named an authority and nothing arrives, its SysOp has probably
> not added your node as a recipient yet, or your node runs a NetBBS too old to
> receive sealed snapshots; ask them which.

Taking it back:

- Removing a recipient takes back what it holds: your node sends it a final,
  empty snapshot, and a node running NetBBS forgets everything it had from
  yours. **Published identity** shows it as retracting until that is
  delivered, for up to 90 days.
- When a caller switches sharing off, your node stops sending the value,
  deletes its signed copy, and its recipients forget it with the next
  snapshot. Backups you took while it was shared still contain it.

#### Identity warnings

A familiar name attached to a new fingerprint produces an identity warning.
Confirm the full **Technical identity** with the other operator before
trusting it; renaming a node does not transfer its reputation.

### Managed names

Managed DNS changes and node display-name changes are separate.

> [!WARNING]
> Use the **DNS** screen's staged rename and recovery controls; do not release
> and re-register a name as a rename shortcut.

A managed name stays yours while this node keeps checking in with the service,
which it does every 15 minutes on its own; the **Dynamic IP** choice only
decides whether the published record follows your address.

- A node the service has not heard from for about a week has its record taken
  out of DNS and the name held for it. The node reclaims the name by itself
  once it is back, and the DNS screen shows the last attempt's outcome under
  the ABANDONED badge until it succeeds.
- <kbd>G</kbd> **Give up name** (a release) is the one exit the node never
  undoes, and a name the service operator has **revoked** on a complaint is
  the other: the screen says so, names the operator's contact channel, and a
  different name can be registered as usual.

Registering sends the first check-in at once and tells you if it failed. From
then on the DNS screen shows **Last contact** (or *never*) and, whenever the
node's latest check-in did not get through, why. Check-ins always connect to
the service directly, never through an HTTP proxy, because the service
publishes the address they arrive from; a node whose only way out is a proxy
can register a name but cannot keep one.

> [!NOTE]
> Callers reach a managed name on the standard ports (SSH 22, Telnet 23, HTTPS
> 443); the record itself carries no port. NetBBS listens on 2222/2323/8080 by
> default, so a bare `myboard.netbbs.org` reaches your board only through a
> port-forward or proxy in front of it. The DNS screen states this against
> your configured listeners; the service cannot check it for you.

#### The public node page

A node with a managed name also gets a public page at
`https://www.netbbs.org/~<name>` once Reliable Link has met it:

- its name, the dial-in addresses it publishes, when the name was registered,
  how long Reliable Link has known it and when it was last heard;
- which NetBBS release you run, as major.minor (7.17, never the patch level);
- the Linked boards your guest account may read; with guest login off, no
  boards are listed;
- a tagline, if you set one: one line of up to 80 characters about your
  board, under its name.

Nothing on it is more than a caller on Reliable Link can see, and never the
address the DNS service sees.

The DNS screen's <kbd>W</kbd> **Web page** steps through the three settings:
shown but hidden from search engines (the default), shown and indexed, or off.
Your node signs the choice into what it sends Reliable Link, so a change takes
effect at the next contact. Giving up or releasing the name does not take the
page down: it stays, marked as left, and **Web page** is still there to turn
it off.

<kbd>T</kbd> **Tagline** edits that line in place: <kbd>Enter</kbd> saves,
an empty line clears it, and <kbd>Esc</kbd> keeps it. It is plain text only:
no colour codes, no markup and no links, and a URL stays plain text. It
reaches the page with the next contact, like the setting, and is published
only while the page is not off.

`python -m netbbs.admin export-node-map [--output FILE]` prints your own node
map as your callers see it, as JSON: the same document Reliable Link builds the
pages from. It reads the node's fingerprint from `--identity-dir` (default
`netbbs_identity`), needs no passphrase, and is safe while the node runs.

#### Running the managed-DNS service

If you also run the managed-DNS service itself, `[managed_dns] admin_token` in
`netbbs.toml` adds <kbd>A</kbd> **Administer service** to the DNS screen: the
service's registrations as a table, each in full, and revocation with a reason
and a typed-name confirmation. `services/managed_dns/README.md` §8 is the
runbook.

### Node keys

Your node's **Technical identity** is its root key and never changes. Day to
day it signs with an operational *signing key* and connects live with a
*transport key*, and either can be replaced without changing the node's
address or reputation. **Link status ▸ Keys** shows both keys and their
history. <kbd>S</kbd> **Signing key** and <kbd>T</kbd> **Transport key** each
offer two ways to replace the key:

| Choice | When | Effect |
| --- | --- | --- |
| <kbd>R</kbd> **Rotate** | Routine replacement | Retires the old key. Everything it signed stays valid, so peers notice nothing except that new content carries the new key. |
| <kbd>C</kbd> **Compromised** | You believe someone else holds the key | Peers stop trusting anything that key ever signed, and your node signs its own boards, posts, files and mail again under the new key. Nodes that already copied your content keep their copies. A node fetching your older content from one of them skips it and gets it from yours instead. |

Replacing the transport key ends every live chat session at once. Peers
reconnect on their own, but a caller watching a channel linked from another
node is told the live link dropped and gets it back by re-entering the
channel.

To act with the node stopped, run this; add `--compromised` where it applies
and `--identity-dir` if yours is not the default. It refuses while the node is
running.

```sh
python -m netbbs.admin rotate-key signing      # or: rotate-key transport
```

> [!IMPORTANT]
> Back up the node after a rotation: an older backup restores the old key.

## Chat bot policy

| At a glance | |
| --- | --- |
| **Where** | **Content ▸ Open chat bots** in the SysOp console; `/bots` inside a channel |
| **Reference** | The [bot API guide](NetBBS-bot-api.md) describes each grant and how to install an external bot |
| **Decide first** | Which bot messages each channel shows, and which grants a bot gets beyond answering what is addressed to it |

In this chapter: [Which bots a channel shows](#which-bots-a-channel-shows) ·
[Run a bot](#run-a-bot) · [Manage running bots](#manage-running-bots) ·
[Try a bot](#try-a-bot) ·
[Linked channels and external bots](#linked-channels-and-external-bots) ·
[Reminders](#reminders) · [Model conversation](#model-conversation)

### Which bots a channel shows

In a channel, `/bots` shows which bot messages this node displays. A SysOp or
a moderator with that channel's moderation permission can set:

| Command | Effect |
| --- | --- |
| `/bots all` | Show all bots (the initial receiving policy) |
| `/bots local` | Show only this node's own bots |
| `/bots none` | Hide bot messages |

- Changes are audited and apply to new live output, history replay, search
  results and remote rosters.
- They do not erase retained messages or change another node's policy.
  Already displayed terminal text is not recalled.
- MRC-bridged channels refuse bots even when the mapping is paused.

Bot authors use a reserved `.bot` label and a `[BOT]` marker. Existing local
accounts with that suffix remain people; an older peer's account with the
suffix is classified as a bot on this node.

> [!NOTE]
> Receiving policy alone does not enable or authorize a bot.

### Run a bot

**Content ▸ Open chat bots** installs and edits bot instances.

1. Create Baud.
2. Choose its local channels, and optionally select it as their default.
3. Enable it and <kbd>S</kbd> **Save**.

First-run onboarding can also create Baud in one channel, with greetings
there (see [First-run onboarding](#first-run-onboarding)); it then appears
here like any other instance.

Baud reads only messages addressed to it. It answers `help` and a caller's
`reminders` list privately, to that caller alone; its other replies follow
how the caller asked, publicly for a line typed in the channel and privately
for `/msg Baud.bot` or `/private Baud.bot`. Reminders are said publicly in
their channel. Friendly, terse and retro temperaments change its fixed
phrases. Its `help` is a titled block,
drawn like `/who`: one row per command, the help opening first.

#### Baud's phrases

Each instance can say its own words, in any language. Under **Phrases** in
the instance's editor:

1. Pick **Greeting**, **Help opening**, **Busy message**,
   **Reminder-limit message** or **Memory-full message**. A blank one shows the temperament's text as
   `(preset)`.
2. Type the phrase. `{name}` is replaced by the caller's name; write `{{` or
   `}}` for a literal brace.
3. <kbd>S</kbd> **Save**. The running bot picks it up within about a second.

> [!NOTE]
> Save refuses any other placeholder, an unmatched brace, terminal controls,
> or a phrase over 400 characters (200 for the help opening, a row of Baud's
> help block; each `{name}` counts as 32), and keeps your draft. Clear a phrase to go back to the preset.

#### Dice, quotes and quick polls

Baud's `!roll` and `!coin` are always on: they store nothing and only answer
the caller. Quotes and quick polls keep callers' words and post later, so each
has its own switch under **Social** in the instance's editor, off by default:

| Switch | What callers get |
| --- | --- |
| **Baud quotes** | Per-channel quotes with `!quote add`, 30 per instance and 10 per caller. The adder or a channel moderator deletes one, and a moderator's deletion is said in the channel. |
| **Baud quick polls** | `!poll` and `!vote`: one open poll per channel and 4 per instance, 2-6 answers, 10 minutes by default and 24 hours at most. Results are said once, at close. |

- Guests can vote but cannot add quotes or open polls. A caller from another
  BBS is never a channel moderator here.
- Quotes and polls share Baud's 16 KiB of saved state with reminders. When it
  is full, Baud answers "Baud's memory is full; ask the SysOp."; deleting old
  quotes frees room.

> [!TIP]
> Dice and poll results spend the channel's **Lines per hour** like any reply.
> A caller who rolls dice all evening can use them up and delay a reminder or
> a poll's results. `/mute Baud.bot` in the channel, or `/bots none`, is the
> remedy; the moderator's `/mute` on the caller works too.

Everything else is a separate grant in the editor, off until you set it:

- reading whole channels;
- joins and leaves (for greetings);
- statistics, last-seen and door standings (`!top`) queries;
- board, file-area and door-event announcements;
- Linked participation;
- model conversation.

The [bot API guide](NetBBS-bot-api.md) describes each. The editor groups its
fields under Bot, Channels, Greetings and announcements, Queries and
conversation, Behaviour and program, Phrases, Social, Door games, and Try.

### Manage running bots

The catalogue lists each bot's state (running in green, restarting in red,
off once its process is gone), how many channels it may use and its last
error.

| Action | Effect |
| --- | --- |
| <kbd>K</kbd> **Kill selected** | Stops one bot |
| <kbd>S</kbd> **Stop all** | Stops every bot |
| <kbd>D</kbd> **Delete selected** | Removes a bot whose state reads off, with its state directory; channel history and existing backups remain |
| <kbd>T</kbd> **Try selected** | Runs the saved bot on a scratch node (see [Try a bot](#try-a-bot)) |

- Each action reports its outcome above the prompt.
- Both stops persist across node restarts; enable an instance again in its
  editor to resume.
- Changes are applied within about a second.

In channels:

- Moderators can `/mute Baud.bot [duration] [reason]` and `/unmute Baud.bot`.
- Output limits apply per bot and channel and survive a restart.
- Callers can ask a bot privately with `/msg Baud.bot <text>` or
  `/private Baud.bot`, and a bot may answer any request privately. Neither
  the question nor the answer enters the channel, its history, Link or MRC,
  and bots with **Read whole channels** do not see the question. Private
  answers need no grant of their own: the bot must be allowed in the
  caller's channel, and mutes and the channel's `/bots` setting apply. They
  have a fixed limit of 30 an hour per caller per bot instead of the
  channel's output limit.
- A bot may answer with a titled block of up to 12 rows instead of a line
  (Baud's `help` does). It counts as one line against the output limit, and
  your node keeps and searches a plain one-line version of it, which is also
  what nodes running an older NetBBS show in a Linked channel. Mutes and
  `/bots` hide a block like any bot line.
- `/who`, `/names` and the status line distinguish bots from people.

### Try a bot

Try runs a bot on a scratch node: its own database, state and channel in a
temporary directory, with the bot's grants. Nothing reaches your node's
database, the bot's real state, its output limits or your callers, so Try asks
no question first, and nothing is kept afterwards.

1. Open it:
   - **Content ▸ Open chat bots ▸ Try selected** (<kbd>T</kbd>, then the
     bot's number) tries the saved instance.
   - The **Try** field, last in the bot editor, tries the draft as it stands,
     before you save it.
2. Type lines as yourself, as in a channel: `!help`, `Baud.bot: help`, or
   plain chat, which only a bot granted **Read whole channels** receives.
3. Act out other events:

   | Command | Effect |
   | --- | --- |
   | `/join <name>` | Another caller joins (reaches bots with presence events) |
   | `/leave <name>` | That caller leaves |
   | `/post <title>` | A new post in the first board-event source |
   | `/file <name>` | A new file in the first file-event source |
   | `/quit` or <kbd>Esc</kbd> | Ends the trial |

4. Read the result under each line: `host >` is each event NetBBS sent,
   `bot >` each action the bot took, followed by its result or the reason it
   was refused, then the chat lines as callers would see them. Any stderr the
   program wrote follows, its last 8 KiB kept. Enter alone shows what has
   happened since, such as a reminder falling due.

> [!NOTE]
> Model conversation is rehearsed in a trial: the bot is told what would have
> been asked, and nothing is sent to the provider or counted against its
> budget.

NetBBS also ships a second example bot, **Tally**, which counts karma
(`bob++`) in channels where it may read every line. It is installed but never
created for you. To use it, create a bot, enter
`["<the node's python>", "-u", "-m", "netbbs.bots.tally"]` under **Program
arguments**, and grant **Read whole channels** where it should count. The
[bot API guide](NetBBS-bot-api.md#the-second-example-tally) describes it.

### Linked channels and external bots

A Linked channel also needs its own **Linked approvals** entry, confirmed on
Save; callers on other nodes then address it as `Baud.bot@Node`. MRC channels
are refused, even while the bridge is paused.

See the [bot API guide](NetBBS-bot-api.md) to install an external bot.

> [!CAUTION]
> External programs run as the NetBBS OS user; the API is not an
> operating-system sandbox.

### Reminders

Reminders survive process and node restarts in `<db>.bots/<original-name>/`.

- Renaming an instance keeps its original state directory.
- Node backups include these directories and restore them with the database.
- Reminders can be delayed by mutes, disabled channels or output limits; a
  reminder refused eight times is dropped so it cannot hold up the others.
- A crash just after delivery may repeat a reminder; delivery is not an
  exactly-once guarantee.
- Absolute times (`remind fri 20:00`) are read on the node's display clock.
  Changing the display timezone restarts running bots; saved reminders keep
  their moment.
- A reminder whose owner is away when it falls due, and a caller's `!tell`
  note, wait in the node database (not in the bot's state) until that caller
  next joins the channel: a week for reminders, 14 days for notes. Both are
  in node backups with the database.

### Model conversation

Model conversation is optional and stays off until you set it up:

1. Set up a provider under **Content ▸ Open chat bots ▸ Model settings** (any
   OpenAI-compatible endpoint, such as a local Ollama server).
2. Install `netbbs[bots]` or `netbbs[web]` for its HTTP client.
3. Grant it to an instance.

**Mode** chooses what the model is for:

| Mode | What it does |
| --- | --- |
| **Help desk** (default) | Answers "how do I..." questions from excerpts of the User Handbook and this node's facts |
| **Companion** | Open chat with your system prompt and nothing else |

Each mode has its own default **System prompt**. If you have not edited it,
changing the mode switches it to the other mode's default; a prompt you
wrote is kept. Changing the mode shows callers the notice again.

In help desk, NetBBS adds to each request:

- the two or three handbook sections that best match the question, under a
  fixed size of about 4 KB. A copy of the User Handbook ships with NetBBS, so
  nothing is fetched;
- node facts: the node name and tagline, its SysOps, whether it has MRC,
  browser links, doors and FTN networks, and the boards and file areas. A
  public answer names only boards and areas everyone can read, with no age or
  name requirement; a private one names those the asking caller can see.
  Hidden Communities stay out. Link callers always get the public list;
- the `stats` answer, if you granted that query to the bot.

The answer ends with the section it came from and a hint, such as
`(Handbook: Exchange files. May be wrong; ? for help)`, within the reply
length. The model is told to name only menus and keys found in the excerpts
and otherwise to point callers to **Help** (<kbd>?</kbd>). No tools are offered
to the model.

> [!NOTE]
> The token budgets count this grounding: a help-desk request reserves up to
> about 5,900 tokens more than a companion one before the provider reports
> what it really used. Allow for it in **Node daily token budget**.

What callers and you see:

- A caller's first request only shows a notice naming the provider and what
  is sent; replies are public unless asked privately.
- Token and optional money budgets, per-caller quotas and channel rates bound
  it.
- <kbd>U</kbd> **Usage** shows today's and this month's requests, tokens and
  cost, the busiest callers and which limits callers reached.
- Prompt logging is off by default.

#### Budget presets

<kbd>A</kbd> **Apply budget preset**, beside **Model settings**, sets the
eight limits at once:

| Field | Frugal | Normal | Generous |
| --- | --- | --- | --- |
| Node daily token budget | 25,000 | 100,000 | 1,000,000 |
| Node monthly token budget | 250,000 | 1,000,000 | 20,000,000 |
| Requests per caller per day | 5 | 20 | 100 |
| Caller cooldown (seconds) | 60 | 10 | 5 |
| Model requests per channel/minute | 1 | 2 | 6 |
| Maximum output tokens | 128 | 256 | 512 |
| Maximum reply characters | 300 | 400 | 500 |
| Prior exchange lines | 2 | 6 | 10 |

Normal is the default. The preview lists only what would change; press
<kbd>A</kbd> **Apply** to save it, and it applies from the next request. A
preset never changes prices, money budgets, the provider, the key, the system
prompt, prompt logging, guest access, or whether model conversation is on.

> [!WARNING]
> When a tier raises the token budgets and no money budget is set, the preview
> warns you: with a paid provider, only the token counts then cap the bill.

## MRC chat bridge

| At a glance | |
| --- | --- |
| **Set up under** | **Settings ▸ Inter-BBS chat (MRC)** |
| **Watch under** | **Node ▸ Inter-BBS chat (MRC)** |
| **Decide first** | Which channels to bridge, and whether callers may open rooms themselves |

> [!IMPORTANT]
> MRC is a separate, public inter-BBS chat network. It is not NetBBS Link and
> does not provide Link's authenticated identities or trust model.

### Bridge a channel

1. Under **Settings ▸ Inter-BBS chat (MRC)**, configure and enable the hub
   connection and the site name/details it announces. Saving applies live.
2. Set **MRC room** on each channel you want bridged. One room maps to one
   channel. Callers see an MRC badge and a notice that their handle is
   visible.
3. Optionally enable **Open rooms** so callers can select or name rooms
   through the Multi Relay Chat picker. Set its access gates, room cap,
   retention, and blocklist. Adopt a room to retain it, or retire it
   deliberately.

> [!NOTE]
> The open-room gates (minimum level, minimum age and name requirement) are
> copied onto a room when a caller opens it. Changing them affects only rooms
> opened afterwards; edit an open room's gates on its own channel screen. The
> cap is checked only when a room opens, so lowering it closes nothing.
> Retention and the blocklist apply to every open room as soon as you save:
> a newly blocked room stops admitting callers, though anyone already in it
> stays. Switching **Open rooms** off stops new rooms opening; rooms opened
> earlier stay until they retire.

### What MRC sees

- IP-address reporting and extra caller metadata are separate opt-in
  settings.
- Terminal size is sent for reply formatting.
- Private MRC messages require each caller's opt-in and are not confidential
  from the network.

> [!WARNING]
> Do not type MRC passwords as ordinary chat; `/mrc register`,
> `/mrc identify`, and the related password commands ask separately without
> echo.

### Run the bridge

Use **Node ▸ Inter-BBS chat (MRC)** for connection state, round-trip time,
errors, and reconnect. Pause one channel's bridge or disable the whole bridge
without removing local chat. MRC moderation and room rules also depend on the
remote hub.

<details>
<summary>Room directory, user counts and roster corrections</summary>

- The room picker learns the network directory when a caller enters an MRC
  room; `/rooms` refreshes it.
- Reported user counts are snapshots, with stale readings marked.
- The chat status bar labels local occupancy separately from MRC occupancy;
  the hub roster does not supply a remote away count.
- A hub nickname or room correction that cannot identify one of several local
  callers produces a notice and diagnostic instead of being applied to an
  arbitrary account.

</details>

## FTN networks (FidoNet, fsxNet)

| At a glance | |
| --- | --- |
| **Set up under** | **Settings ▸ Echomail & netmail (FTN)** |
| **Watch under** | **Node ▸ Echomail & netmail (FTN)** |
| **From the shell** | `python -m netbbs.admin ftn-import-nodelist`, `python -m netbbs.admin ftn-listen` |
| **Decide first** | Which network to join; fsxNet is the usual first network for a new BBS |

NetBBS can join FidoNet or an FTN othernet, such as fsxNet, as an ordinary FTN
node. Echomail arrives on boards, file echoes in file areas, and netmail in
Mail.

> [!IMPORTANT]
> It is a gateway, not NetBBS Link. An FTN board or file area is never also
> Linked, and nothing that arrives over FTN is trust-evaluated.

In this chapter: [Join a network](#join-a-network) ·
[AreaFix and FileFix](#areafix-and-filefix) ·
[Join echoes from the hub's list](#join-echoes-from-the-hubs-list) ·
[File echoes](#file-echoes) · [Netmail](#netmail) ·
[Mail bundles](#mail-bundles) ·
[Held items and status](#held-items-and-status) ·
[Unknown echoes](#unknown-echoes) · [IPv6 and TLS](#ipv6-and-tls) ·
[Points and downlinks](#points-and-downlinks) ·
[Not supported](#not-supported)

### Join a network

1. Apply to the network for an address. fsxNet, the usual first network for
   a new BBS, takes applications by email on the form in its infopack. It
   gives you an address such as `21:1/199`, a hub, and one password for both
   BinkP and AreaFix.
2. Under **Settings ▸ Echomail & netmail (FTN)**, press
   <kbd>C</kbd> **Create**, and pick the network to start from by its number
   (see [Network templates](#network-templates) below).
   - The template fills the new draft only; nothing is stored before
     <kbd>S</kbd> **Save**, and every value stays editable.
   - Enter **Our address** and the passwords, and whatever the template
     left empty, then set **Enabled**.
   - **Answer calls** lets the hub, and other nodes, call this BBS on the
     **Answer port** (24554 by default), which must be reachable from the
     internet.
   - Without it, the BBS only calls out: once every **Poll every** interval,
     and within five minutes when mail waits for the hub.
3. On each board that should carry an echo, use <kbd>E</kbd> **Echo (FTN)**
   on the board's screen and enter the echo's tag, such as `FSX_GEN`. Each
   echo goes to one board, and a Linked board can't carry one.
4. Ask the hub to send those echoes. Under
   **Node ▸ Echomail & netmail (FTN)**, press <kbd>A</kbd> **AreaFix** and
   enter `+FSX_GEN`, or `%LIST` to see what the hub carries. The hub's answer
   arrives in your Mail. To join many echoes at once, import the hub's echo
   list instead (see [Join echoes from the hub's list](#join-echoes-from-the-hubs-list)).

#### Network templates

<kbd>C</kbd> **Create** first asks which network the new one starts from.
Each template fills only the network's public facts, taken from its own
infopack; a value the infopack does not confirm is left empty.

| Template | Fills | You still enter |
| --- | --- | --- |
| fsxNet | Name `fsxNet`, domain `fsxnet`, uplink `21:1/100` at `net1.fsxnet.nz` | Our address, the passwords; the nodelist echo and list name if you want lists imported |
| nixNet | Name `nixNet`, domain `nixnet`, nodelist echo `NIX_LIST`, robots AreaFix and FileFix | Our address (zone 86), your regional hub's address and host, the passwords, and **List name** from the nixNet infopack |
| FidoNet | Name `FidoNet`, domain `fidonet`, nodelist names `NODELIST` and `NODEDIFF` | Our address and the uplink your Net coordinator assigns, its host, the passwords, and **Nodelist echo** if your zone sends the list in one |
| Blank | Nothing | Every value, from the network's infopack |

- nixNet and FidoNet fill no uplink: nixNet assigns each node a regional
  hub, and FidoNet's Net coordinators assign uplinks.
- No template fills a password, the TLS pin, a certificate or key path,
  **Our address**, or the answer host and ports.
- None switches on **Enabled**, **Answer calls**, **Answer requests** (file
  requests) or **Held mail, no password**, and none sets **Who may ask** to
  anyone: it stays at secure links only.
- A nodelist echo needs its list's name: nixNet's draft refuses to save until
  you enter **List name**, or empty **Nodelist echo** to import lists by hand.

#### How echomail behaves

- An echomail message from the network is posted under its author's FTN name
  and address, and goes through the board's own moderation.
- What callers post on an FTN board goes out with the next call.
- Edits and deletions stay on this BBS, because FTN has no way to carry them.
- Posts go out under the author's username, except on a real-name echo
  (below).
- Each message ends with an Origin line: the network's **Origin**, or the
  BBS's name when that is empty, then this node's address.

#### A board's own Origin line

A board carrying an echo can sign its echomail with its own Origin, such as
the name of a club that meets there. On the board's screen, press
<kbd>O</kbd> **Origin (FTN)** and type it. The board's FTN section shows
the Origin in use.

- An empty line goes back to the network's **Origin**.
- The whole line, ` * Origin: ` and this node's address included, stays
  within the 79 characters FTN allows. The prompt says how much fits:
  50 characters for the address `21:1/199@fsxnet`. A longer one is
  refused, never cut.
- Making the board local again with <kbd>E</kbd> **Echo (FTN)**, or giving
  it an echo of another network, drops its Origin.

#### Real-name echoes

Some echoes take posts under real names only. nixNet, for example, allows
aliases in every echo except NIX_ADMIN. A board sends its authors' verified
real names to its echo when its **Name requirement** is **Verified and
displayed**, set on the board or inherited from its Community. Only that
board does: the same account's posts on other echoes, on Link and in chat
keep the username.

To carry such an echo:

1. On the board's screen, give it the echo with <kbd>E</kbd> **Echo (FTN)**,
   such as `NIX_ADMIN`.
2. Edit the board and set **Name requirement** to **Verified and displayed**.
3. Verify the real name of everyone who will post there, yourself included,
   with <kbd>V</kbd> **Verify** on the main menu: your own account is in its
   list. Enter the name as the network should see it, such as `Jane Doe`.

> [!IMPORTANT]
> Your SysOp level lets you past the board's name requirement, but not past
> this one: the network still needs a name. Until your own real name is
> verified, the board reads `Read only` for you too.

The From field holds 35 bytes. A name with letters outside plain ASCII, such
as `é`, can take two bytes each when the message goes out in UTF-8, which
happens when the post has characters CP437 lacks. A post whose name doesn't
fit is refused with the name's length; verify a shorter form of the name,
such as an initial for a middle name. Nothing is ever cut short or sent
under the username instead.

- **Replies.** A reply to a local post on the board is addressed to that
  author's real name, the name the network saw.
- **Netmail.** Netmail to a verified real name reaches its account (see
  [Netmail](#netmail)).
- **Doors** can't post there: a door's label is nobody's real name.
- **Held posts.** A post a moderator approves later goes out under the name
  it was written under, which is also what the board shows, even if the
  verification was revoked or changed meanwhile. A post written before the
  board asked for displayed names has none, so it stays on this BBS, and the
  node log says why.

### AreaFix and FileFix

The hub's robots link and unlink echoes for you: AreaFix for echomail,
FileFix for file echoes. Under **Node ▸ Echomail & netmail (FTN)**, press
<kbd>A</kbd> **AreaFix** or <kbd>F</kbd> **FileFix** and type the request on
one line. The robot's answer arrives as netmail in your Mail.

| You type | It asks |
| --- | --- |
| `+NIX_BSD` or `NIX_BSD` | Link the echo |
| `-NIX_BSD` | Unlink it |
| `%LIST`, `%QUERY`, `%LINKED`, `%UNLINKED`, `%AVAIL`, `%HELP` | Lists and help |
| `%PAUSE`, `%RESUME` | Stop and restart all your echoes for a while |
| `%RESCAN NIX_BSD R=10` | Send an echo's old messages again, with the robot's own options |

- One request holds up to 50 commands, such as `+NIX_BSD +NIX_BBS %LIST`.
- A `%` command NetBBS doesn't know goes to the robot exactly as you typed
  it, with the words after it, up to the next `%` command. Put it last on the
  line.
- `%PWD`, `%PASSWORD`, `%PKTPWD` and `%TICPWD` are refused here: they change a
  password NetBBS stores.

The robots' names and FileFix's password are in the network's editor under
**Robots**:

- **AreaFix name** and **FileFix name** are `AreaFix` and `FileFix` unless
  the hub's robots answer to others, such as `AllFix` or `FileMgr`.
- **FileFix password** stays empty when the hub uses the AreaFix password for
  both, as most do.

**Change a robot's password.** Under **Settings ▸ Echomail & netmail (FTN)**,
press <kbd>R</kbd> **Robot password** on the network:

1. Choose AreaFix or FileFix.
2. Type the new password twice. It is sent to the robot as `%PWD`, under the
   old password, and stored at once.
3. Read the robot's reply in your Mail. If it refused, set the old password
   back in the network's editor.

When AreaFix's password changes and FileFix had none of its own, FileFix keeps
the old one: the hub only heard about AreaFix. If your hub uses one password
for both robots, set **FileFix password** once AreaFix confirms. Session and
packet passwords are agreed with the hub's coordinator and are set in the
editor.

### Join echoes from the hub's list

A hub's infopack lists its echoes: a `.NA` file for echomail and a `.FA`
file for file echoes, such as nixNet's `NIXNET.NA`. To join many at once:

1. Unpack the list onto the host.
2. Under **Node ▸ Echomail & netmail (FTN)**, press <kbd>I</kbd> **Import
   echo list** and enter the file's path.
3. In the draft:
   - **List of** says whether the list is echomail (boards) or file echoes
     (file areas). It follows the file's extension; change it for a list
     named otherwise.
   - **Echoes** opens the checklist. Echoes this network already carries are
     marked `[=]` and can't be checked.
   - **Community** is where the new boards or file areas go.
4. <kbd>S</kbd> **Save**. Each checked echo gets a board or file area named
   from its description, cut to 40 characters, or from its tag when that
   name is already taken. Each is mapped to its echo, and one AreaFix or
   FileFix request asks for all of them, split into requests of 50.

Nothing is created before **Save**. If anything can't be created, nothing is,
and the draft says why. The list may be up to 1 MiB and 10,000 lines; lines
that aren't `TAG description` are skipped and counted.

### File echoes

A file echo distributes files the way an echo distributes messages. nixNet,
for example, sends its weekly nodelist in NIX_LIST and its infopack in
NIX_INFO. Each file comes with a small `.TIC` file that names it, its echo,
its CRC and a password.

1. In the network's settings, enter the **TIC password** the hub gave you.
   Until you do, every file echo is held for you to look at, never accepted.
2. On each file area that should carry a file echo, use
   <kbd>F</kbd> **File echo (FTN)** on the area's screen and enter the tag,
   such as `NIX_INFO`. Each file echo goes to one area, and a Linked area
   can't carry one.
3. Ask the hub to send it with FileFix.

#### How file echoes behave

- A file is checked before it is stored: it came from your hub in a secure
  session (or from one of your points or downlinks, under its own TIC
  password: see [Points and downlinks](#points-and-downlinks)), its TIC
  password matches, its area is carried here, its name is
  a plain file name, and its size and CRC match what arrived. It must be
  within your **Upload cap** (Settings, Limits & retention), and never over
  64 MiB. A file that fails a check is held with its TIC and the reason.
- It goes through the file area's own moderation, as an upload does: in a
  moderated area it waits under <kbd>P</kbd> **Pending files**.
- Its description comes from the TIC, else from the archive's
  `FILE_ID.DIZ`. Its uploader reads as the network and the address that
  sent it out, such as `nixNet (86:549/1)`.
- The same file arriving again is dropped, and the call's summary counts it.
- A TIC may say the new file **replaces** older ones, such as last week's
  nodelist. That removes only files that also came by TIC into the same
  area, once the new file is visible. A caller's upload is never replaced:
  a TIC file with the same name as one is held instead.
- A file and its TIC can come in different calls. The first to arrive waits
  in the held store and is paired when the other comes. One still alone
  after 7 days is deleted, and the log says so.

#### Hatching your own files

To send one of your own files into a file echo, *hatch* it. Your hub passes it
on to every system that carries the echo, and your own points and downlinks
that carry it get it from you.

1. Open the file area that carries the echo from the **Files** menu, as a
   caller would, and put the cursor on the file.
2. Press <kbd>H</kbd> **Hatch**. Only a SysOp sees the key, and only in an area
   that carries a file echo.
3. Answer <kbd>Y</kbd>. The file and its TIC wait for the next call with your
   hub, and **Status** counts them as files waiting.

- The file keeps its name. A name longer than DOS's 8.3 is sent under a short
  name, such as `NETBBS4F.ZIP`, and the TIC carries the long one for the
  systems that read it.
- The TIC's description is the file's own, so describe the file before you
  hatch it.
- A file is hatched once. Press <kbd>H</kbd> on it again and you are asked
  whether to hatch it again: the new copy tells the receiving systems that it
  replaces the first one.
- These files are refused, with the reason:
  - a file that came over FTN (by TIC, with a netmail, or saved from the held
    store), so nothing is sent back to the network it came from;
  - a file still waiting for approval;
  - a file over 64 MiB;
  - a file named like a control file (`.TIC`, `.PKT` or `.REQ`);
  - any file, while the network has no **TIC password**.
- Each hatch is in the area's <kbd>H</kbd> **History**.

> [!NOTE]
> **Auto-hatch** sends callers' uploads into the network without you looking
> at them first. Turn it on only where that is what you want, and keep the
> area moderated if you want to see each file first.

To hatch callers' uploads automatically, press <kbd>A</kbd> **Auto-hatch** on
the file area's screen, beside <kbd>F</kbd> **File echo (FTN)**. It is off until you turn it
on. With it on, each upload is hatched once, as soon as it is approved: at
once in an unmoderated area, or when you approve it in a moderated one. An
upload that can't be hatched is still stored, and the log says why.

### File requests

A file request (FREQ) asks another node for files by name, and other nodes
can ask yours. Both work in the call itself: the files come back in the
same call when both sides speak binkp/1.1, as NetBBS, binkd and most
current mailers do.

#### Ask for files

1. Open **Node ▸ Echomail & netmail (FTN)** and press
   <kbd>R</kbd> **Request files**.
2. **From node** is your hub. Change it to ask another node, which must be
   listed in the network's nodelist as answering BinkP.
3. Under **Files**, type the names, separated by spaces. `*` and `?` match
   any characters. Three names ask for something special: `FILES`, the
   node's list of files; `ABOUT`, its description; `NODELIST`, the network's
   nodelist. A password the node gave you follows its file:
   `THIS.ZIP !secret`.
4. Press <kbd>S</kbd> **Send**. The request goes out at once.

What comes back waits under <kbd>H</kbd> **Held items**, marked as
requested. Save it to a file area with <kbd>S</kbd> **Save**; it does not
expire. Files that come in a later call are recognised for 14 days.

#### Answer requests

Answering is off until you turn it on, for each network.

1. On each file area whose files other nodes may ask for, press
   <kbd>O</kbd> **Offer to file requests: no** on the area's screen, which
   turns it to yes. Only an area anyone could read can be offered: no read
   level, age gate, verified name or hidden Community. A Linked area can't be offered, and an area
   carrying another network's file echo is served only on that network.
2. In the network's settings, under **File requests**, turn on
   **Answer requests**.
3. **Who may ask** is **secure links only** by default: your hub, which
   proves itself with the session password. **Anyone** lets any node ask.
4. Set the limits: **Files per call** and **MiB per call**, and
   **Files per node a day** and **MiB per node a day**.
5. Optionally name an **ABOUT file**, and a **NODELIST file**. Without a
   NODELIST file, NODELIST gets the network's nodelist as you imported it;
   turn **Serve NODELIST** off to refuse it.

`FILES` is always answered with a list of every file the request could get.
A name with a folder or drive in it, or a pattern of wildcards alone such
as `*.*`, is refused. Passwords are not used: offered files are public, and
a password in a request is never stored or shown. Whatever the settings, a
network sends at most 1,000 files and 1 GiB a day by file request.

> [!NOTE]
> A node calling with binkp/1.0 can't get an answer in the call, and
> NetBBS does not keep its request for a later one. The request is refused,
> and the log says why.

Every request, answered or refused, is written to the log. The status
screen shows the network's last five, made and answered.

### Netmail

Netmail is personal mail to an address written as `Name (zone:net/node)`.

| Netmail | |
| --- | --- |
| **Who may send it** | Each network has a **Netmail level**, and only accounts at that level may send netmail. It starts at 255, SysOp only, because netmail leaves under this BBS's address. |
| **Who receives it** | Every account, by username, a username it had before a rename, or its verified real name. Netmail to `SysOp` always comes to you, even if an account is called that. |
| **No such account** | Netmail no account here can take goes back to its sender (below). Set **Bounce unknown** to **no** and it comes to you instead. |
| **Direct delivery** | Keep the network's nodelist current (below). Netmail to a node whose nodelist entry gives a BinkP host then goes to it directly. |
| **Via the hub** | The rest goes through the hub, and so does anything a direct call fails to deliver after three tries. |
| **Flavour** | Each netmail is **Normal**, **Crash** or **Hold**, chosen on the review screen with <kbd>F</kbd> **Flavour** (below). |
| **Files** | Your own netmail can carry files from a file area (below). Files sent to this BBS land in the network's **Attach area**. |

#### Returned netmail

Netmail for a name no account has, for a verified real name two accounts
share, or for an account that takes no mail goes back to its sender. The
reply comes from `Mailer-Daemon` at this BBS's address, says why, and
quotes the first lines. **Bounce unknown**, in the network's **Netmail**
section, is on for every network; set it to **no** and such netmail comes to
you instead, with a line saying who it was for.

Some netmail is never returned, and comes to you with a line saying why:

- netmail from a robot, such as AreaFix, FileFix or `Mailer-Daemon`;
- netmail that is a returned netmail itself;
- netmail from an address that already got five returned netmails from this
  BBS today.

That keeps two misconfigured systems from bouncing mail back and forth.

#### Crash and hold

The <kbd>F</kbd> **Flavour** key on the review screen goes round three
choices:

- **Normal**, the default: direct when the nodelist lists the node, else
  through the hub.
- **Crash**: this BBS calls the node directly and at once, never through the
  hub. It needs a BinkP address for the node in the nodelist, unless the node
  is your hub. After five failed calls the netmail is dropped, and its sender
  gets a letter from the BBS saying so.
- **Hold**: nothing calls out. The netmail waits for a call with the node
  that a password proves: your hub, or one of your points or downlinks
  (below). Held netmail still waiting after 30 days is dropped, and its
  sender is told.

> [!WARNING]
> Netmail held for any other node waits, by default, until it expires: that
> node has no password here, and anyone can call claiming its address.
> **Held mail, no password** in the network's **Netmail** section gives held
> netmail and files to a node that calls in, or that this BBS calls, without
> a password. Turn it on only if you accept that a stranger pretending to be
> that node could collect them.

**Node ▸ Echomail & netmail (FTN)** counts held netmail under **Waiting**.

#### Files with netmail

To send a file to another node, write the netmail and press
<kbd>A</kbd> **Attach file** on the review screen. Pick it from a file area,
as for any letter. When you send, the file goes with the netmail, through
the hub or directly, on the netmail's flavour.

- Only your own netmail sends files. When others who may send netmail point
  at a file, the far side gets its name and file area as text, as with a
  letter to a linked BBS.
- A file is at most the node's upload limit, and never more than 64 MiB.
- On the network, the netmail's subject is the files' names, as FTN
  requires. Your Sent copy keeps the subject you wrote.

Files sent to this BBS with a netmail are stored in the network's
**Attach area**, set in the **Netmail** section, and listed in the letter
like any file a letter points at.

- A file shows its sender as its uploader, and goes through the area's
  moderation. In a moderated area, the letter shows it as not available
  until you approve it.
- Whoever may read the area may download the file, so pick an area only the
  right people can read.
- Without an attach area, each file is held for you, and the letter says
  so. A file is also held when its name is already in the area, it is over
  the limit, or it came from a caller that didn't prove a password.
- A file can come before or after its netmail, in another call. Whichever
  comes first waits up to 7 days for the other.

#### The nodelist

Most networks send their nodelist every week in a file echo, such as nixNet's
`NIX_LIST`, and some send a small daily diff in between. NetBBS can import
both as they arrive:

1. Under **Settings ▸ Echomail & netmail (FTN)**, open the network and fill in
   the **Nodelist** fields:
   - **Nodelist echo**: the echo's tag, such as `NIX_LIST`;
   - **List name**: the list's file name without the day number, such as
     `NIXLIST` for `NIXLIST.289`, or `NODELIST` on FidoNet;
   - **Diff name**: the diffs' file name, such as `NODEDIFF`, if the network
     sends diffs. Leave it empty if it doesn't.
2. Ask the hub to send the echo: press <kbd>F</kbd> **FileFix** and enter
   `+NIX_LIST` (see [AreaFix and FileFix](#areafix-and-filefix)). The echo
   needs no file area. If you map it to one as well, the files are stored
   there too.

What happens to each file:

- **A full list**, plain or in a ZIP (such as `NIXLIST.Z89`), is imported once
  its CRC-16 matches the check value in its first line.
- **A diff** is applied to the last list, and the list it makes must pass the
  same check.
- **A file that fails** changes nothing: the list you have stays in use, and
  the file goes to **Held** with the reason. After a failed diff, the network
  waits for the next full list, and later diffs are not applied until it
  arrives.

> [!NOTE]
> A list in an LHA, ARC, 7-Zip, RAR or ARJ archive needs that format's
> unpacker (see [Mail bundles](#mail-bundles)). Without it, the file is held
> with the tool to install.

**Node ▸ Echomail & netmail (FTN)** shows, for each network, the list's day
number, how many nodes it lists, when it was imported and how (full list,
diff or by hand), whether its CRC-16 was verified, and the last failure.

To import a list by hand, press <kbd>N</kbd> **Nodelist import** on that
screen, or run on the host:

```sh
python -m netbbs.admin ftn-import-nodelist fsxNet FSXNET.280
```

Either way the file may be the list itself or an archive holding it. A list whose
first line carries a check value that doesn't match is refused. A list
imported by hand is kept like one from the echo, so later diffs apply to it.

### Mail bundles

A hub sends echomail as *bundles*: archives of packets, named for the day of
the week (such as `00000063.we0`). NetBBS opens ZIP bundles by itself. For
other formats it uses an unpacker you install on the host. Installing one is
all the setup there is:

| Format | Tool | NetBSD (pkgsrc) | Debian, Ubuntu |
| --- | --- | --- | --- |
| LHA | `lha`, or 7-Zip | `archivers/lha` | `lhasa` |
| ARC | `nomarch` | `archivers/nomarch` | `nomarch` |
| 7-Zip, ARJ | `7z`, `7za` or `7zz` | `archivers/p7zip` or `archivers/7-zip` | `7zip` or `p7zip-full` |
| RAR | `unrar`, or 7-Zip | `archivers/unrar` | `unrar` |

On NetBSD, for example:

```sh
pkgin install lha nomarch p7zip unrar
```

The same tools read `FILE_ID.DIZ` from uploads in these formats. NetBBS runs
them in a sandbox and reads what they unpack without writing it to disk. It
refuses a bundle that would unpack to more than 64 MiB or hold more than 256
files, or that has a member whose name could escape or be read as an option.

A bundle that no installed tool can open is held, and the reason names the
tool to install. Install it, then press <kbd>R</kbd> **Release** on the held
bundle under **Held items** to toss it. A held bundle waits 7 days for this,
like any file waiting for its TIC.

#### Send bundles

**Bundle outgoing mail** in the network's **Mail bundles** section sends each
call's mail as one ZIP bundle instead of a bare packet. It is off by default,
because every hub takes bare packets. Turn it on if your hub asks for
bundles. The mail counts as sent only once the hub confirms it received the
bundle.

### Held items and status

Some packets and files are held instead of tossed:

- everything from a caller that didn't prove a password, unopened;
- a packet addressed to another address;
- a message that couldn't be stored, for example because a mailbox was full;
- a file echo's file and TIC that failed a check, together;
- a file waiting for its TIC, or a TIC waiting for its file. An archive with
  no packets in it is held whole, and any other file inside a mail bundle is
  held on its own beside the tossed packets;
- a mail bundle that no installed tool can open (see
  [Mail bundles](#mail-bundles));
- a file sent with a netmail that couldn't be stored, such as when the
  network has no **Attach area** (see [Netmail](#netmail));
- a file another node sent for your file request (see
  [File requests](#file-requests)).

The store holds 200 items and 256 MiB. If it is full, what can't be kept is
counted as lost in the last call's summary and written to the log.

Look at them under **Node ▸ Echomail & netmail (FTN)** with
<kbd>H</kbd> **Held items**:

- <kbd>R</kbd> **Release** tosses a packet as if it came from a known
  system. On a held mail bundle it opens the bundle and tosses the packets
  inside. For any other file it says why it can't.
- <kbd>S</kbd> **Save** stores a held file in a file area you pick, as your
  own upload. A file held with its TIC keeps the TIC's description. A name
  already in the area is refused.
- <kbd>E</kbd> **Export** writes a held file to a folder or path on the
  host. An existing file is never overwritten, nothing is written through a
  symbolic link, and the item stays held until you delete it.
- <kbd>D</kbd> **Delete** throws it away.

**Node ▸ Echomail & netmail (FTN)** also shows each network's last call,
errors, waiting messages and recently answered calls.
<kbd>P</kbd> **Poll now** calls the hub at once.

### Unknown echoes

An echo the hub sends that no board carries, or a file echo no file area
carries, is an *unknown echo*. Its messages and files wait for you instead of
being lost. **Node ▸ Echomail & netmail (FTN)** counts them, and
<kbd>U</kbd> **Unknown echoes** lists one row per tag, with how many wait and
when the first and last came:

- <kbd>C</kbd> **Create** opens a new board, or a new file area for a file
  echo, with the tag as its name. Saving it carries the echo there and tosses
  what waited.
- <kbd>M</kbd> **Map** picks an existing local board or file area that
  carries no echo, and tosses what waited into it.
- <kbd>D</kbd> **Delete** drops what waited for that tag.

Tossing again goes through the board's moderation and duplicate checks, and a
file echo's files through the checks above. At most 1,000 messages wait per
tag and 10,000 (or 64 MiB) in all; beyond that the oldest are dropped, and
the status screen counts them.

### IPv6 and TLS

The BBS calls a hub's host name over IPv6 or IPv4, whichever answers first.
To call an IPv6 address, write it in brackets in **Host**, with the port if it
is not 24554: `[2001:db8::1]:24554`.

It answers calls on every address, IPv6 and IPv4. To answer on some addresses
only, list them in **Answer addresses** in a network's **Transport** section,
such as `192.0.2.7 2001:db8::7`. Like the **Answer port**, it applies to every
network.

#### Call the hub over TLS

Only if the hub offers BinkP over TLS (binkps):

1. Set the network's **Port** to the hub's TLS port, usually 24553.
2. Set **Call over TLS** in **Transport**:
   - **on, certificate pinned** for a hub with a self-signed certificate, the
     usual case. The first call learns the certificate. If a later call meets
     a different one, the call is refused and **Last error** shows the new
     fingerprint.
   - **on, certificate verified** for a hub whose certificate is signed by a
     known authority for its host name.

> [!IMPORTANT]
> When a pinned call is refused, ask the hub whether it renewed its
> certificate before you accept the new one. If it did, empty **Pinned
> certificate** and save; the next call learns the new one.

The session password still proves this BBS to the hub, over TLS or not.

#### Answer over TLS

1. Put a certificate and its private key, in PEM files, where the node can
   read them. A self-signed certificate is common for BinkP, and the one a
   web server on the same machine uses works too.
2. In a network's **Transport** section, set **TLS answer port** (usually
   24553), **Certificate file** and, if the key is in its own file, **Key
   file**. Open the port to the internet as you did the answer port.

The plain answer port goes on answering. **Node ▸ Echomail & netmail (FTN)**
shows both ports under **Calls answered**, and a **TLS error** row if the
files could not be loaded.

From the shell, `python -m netbbs.admin ftn-listen` shows where the BBS
answers, and sets it with `--host`, `--port`, `--tls-port` (or `off`),
`--tls-cert` and `--tls-key`. A running node picks a change up within a
minute.

#### More than one batch per call

NetBBS speaks binkp/1.1 with multiple batches, so mail and files that become
ready during a call go out in the same call. With an older mailer it falls
back to binkp/1.0 by itself; there is nothing to set.

### Points and downlinks

Your BBS can serve systems under its own address: **points**, such as a
user's own offline reader setup, and **downlinks**, other nodes you feed.

| Links | |
| --- | --- |
| **Where** | **Settings ▸ Echomail & netmail (FTN)**, highlight the network, <kbd>L</kbd> **Links** |
| **A point** | Your address with a point number: `21:1/199.1`, `.2` ... It calls you for its mail; you never call it. |
| **A downlink** | A node you feed, such as `21:1/200`. It calls you, or you call it when mail waits. Its own points are reached through it. |
| **Password** | Every link needs a session password. Only a call that proves it gets the link's mail. |
| **Areas** | <kbd>A</kbd> **Areas** on a link: the echoes and file echoes it carries. <kbd>R</kbd> **Robot areas**: what links may link themselves. |
| **Status** | **Node ▸ Echomail & netmail (FTN)** counts each network's links and what waits for them. |

> [!IMPORTANT]
> Check your network's rules first. Many networks let a node feed points but
> not other nodes. nixNet, for one, allows feeding approved points only.

#### Add a point

1. In **Settings ▸ Echomail & netmail (FTN)**, highlight the network and
   press <kbd>L</kbd> **Links**.
2. Press <kbd>C</kbd> **Create**. A new link starts as your next free point.
3. Set **Session password**, and **Packet password** if the point uses one.
   Set **AreaFix password** so it can link echoes itself, and **TIC
   password** if it is to get file echoes. Give the point's owner the
   passwords, with your address and the answer port.
4. Press <kbd>S</kbd> **Save**.
5. Give it its echoes: press <kbd>A</kbd> **Areas** on the point, then
   <kbd>A</kbd> **Add** for each one (see below).

The point's software calls your answer port, so **Answer calls** must be on
for the network.

#### Add a downlink

Create a link as above, then set **Kind** to **downlink** and type its
address. Under **Calls**:

- **hold**: it calls you for its mail, as a point does.
- **poll**: you call it when mail waits, at most every five minutes. Fill in
  **Host** with where it answers BinkP, and **Port** if it isn't 24554.

**Bundle mail** sends its packets in a ZIP, if it prefers that.

#### Give a link its echoes and file echoes

A link gets an echo or a file echo only when it carries it, and it can post
only in an echo it carries read-write.

1. On **Links**, highlight the link and press <kbd>A</kbd> **Areas**.
2. Press <kbd>A</kbd> **Add** and pick an echo or file echo. Only areas a
   board or file area of yours carries are offered.
3. Pick an area in the list to switch it between **read-write** and
   **read-only**. A read-only link gets the area's messages and files, but
   what it sends to that area is held for you.
4. <kbd>D</kbd> **Delete** takes an area away from the link.

#### Let links link areas themselves (AreaFix and FileFix)

Your BBS answers its points' and downlinks' AreaFix and FileFix requests,
as your hub answers yours. A link can link only the areas you offer.

1. On **Links**, press <kbd>R</kbd> **Robot areas**.
2. Pick each echo or file echo you want links to be able to link. Pick it
   again to stop offering it; links that carry it keep it.
3. Give each link an **AreaFix password** (and a **FileFix password**, or it
   uses the AreaFix one) in its editor.

A request is netmail to `AreaFix` or `FileFix` (the names set on the
network) at your address, with the link's password as the subject. Your BBS
acts on it only when it comes in a call that proved the link's session
password; anyone else's request reaches you in Mail, not acted on. It
answers by netmail with what it did. A link can send:

| Command | What it does |
| --- | --- |
| `+TAG` or `TAG` | Link an area you offer; `*` and `?` match several |
| `-TAG` | Unlink an area |
| `%LIST`, `%AVAIL` | The areas offered to it, marking the ones it carries |
| `%QUERY`, `%UNLINKED` | What it carries; what it could link but doesn't |
| `%RESCAN [TAG] [n]` | AreaFix only: send the newest `n` messages again (50 by default, at most 200 per echo) |
| `%PAUSE`, `%RESUME` | Stop and start its echomail and file echoes; netmail still goes |
| `%PWD new` | Change its own AreaFix or FileFix password |
| `%HELP` | The list of commands |

> [!NOTE]
> A link's request never makes your BBS ask your hub for an area. To carry a
> new echo, link it from your hub and map a board to it first, then offer it.

#### What a link gets

- **Echomail** in the echoes it carries: what your hub sends, what your
  other links post, and what is written on your boards. Your BBS adds itself
  and each downlink to SEEN-BY and itself to PATH, so nothing is sent to a
  system that has seen it, and a message that has passed through your BBS
  before goes nowhere. Points never appear in SEEN-BY.
- **Echomail a link posts** goes on to your hub and your other links, with
  your address in SEEN-BY and PATH. A point's goes as if you wrote it. On a
  moderated board it goes once you approve the post.
- **File echoes** it carries: each file that arrives by TIC, from your hub
  or another link, and each one you hatch, goes on with a TIC under that
  link's **TIC password**. A link with no TIC password gets no files. A
  file the link sends goes on to your hub and your other links. In a
  moderated file area a file goes once you approve it.
- **Netmail** to a point, a downlink or one of the downlink's points waits
  for that link alone, of any flavour, and goes in its next call that
  proves the password. Netmail to a point you haven't set up is refused.
- **Netmail a point or downlink sends** to another node goes to your hub,
  with a Via line naming your BBS. Netmail your hub sends for one of your
  links goes to that link. Files attached to such netmail follow it on the
  same way. Netmail for any other node that reaches you is still not yours
  to route, as before.
- **File requests** from a link are answered in its call, as from any
  secure link, when the network answers them. A request you make to a
  point or downlink waits for its next call.
- A point's mail must say it was written at the point. Anything else it
  sends is held for you, under **Held items**.

Some mail is held instead of passed on, with the reason: echomail or a TIC
for an area the link doesn't carry read-write, a TIC with the wrong
password, a netmail that has passed through your BBS before (a loop), one
that has passed 30 systems, and one for a point you don't serve.

Mail a link hasn't collected after 30 days is dropped, and its sender is
told. At most 2,000 messages and 50 files wait for one link; past that,
what doesn't fit is not sent to it, and the node log says so.

#### Switch a link off or remove it

- **Status** **disabled** keeps the link's address safe: a caller claiming
  it must still know the password, but gets nothing, and what it sends is
  held for you. **paused** is set by the link itself with `%PAUSE` and
  stops its echomail and file echoes, not its netmail.
- <kbd>D</kbd> **Delete** removes a link after asking. Mail waiting for it is
  dropped, and its senders are told.

### Publish a pointlist and a nodelist segment

Your BBS can publish two lists for a network:

- a **pointlist** of your points, in the "Boss" format networks collect
  (FTS-5002);
- a **nodelist segment** for the net you coordinate, to send to your zone or
  region coordinator (FTS-5000).

| Publishing | |
| --- | --- |
| **Where** | **Settings ▸ Echomail & netmail (FTN)**, highlight the network, <kbd>P</kbd> **Publishing** |
| **Files** | `POINTS.296`, `NET549.296` ...: the name you set and the day of the year. With **ZIP** on, also `POINTS.Z96`. |
| **Folder** | The folder you set, or the BBS's own (shown on the screen). Earlier editions stay there. |
| **Hatch into** | The file echo a list goes out in, if you set one. |

Both lists are off until you switch them on. The screen shows each list's
last edition, with its day number and CRC-16, and the last failure.

#### List your points

1. On the **Publishing** screen, press <kbd>E</kbd> **Entries for points**. Each point
   from your **Links** screen is listed with its line.
2. Pick a point to fill in its line: **System name**, **Location**,
   **Sysop**, **Phone**, **Speed** and **Flags**. An empty name or sysop
   uses the link's name, so a point you never edit is still listed.
3. Set **Listed** to **no** for a point that shouldn't appear. A disabled
   link is always left out.
4. Under <kbd>S</kbd> **Settings**, switch **Pointlist** on.

The list starts with `Boss,` and your address, and each point follows as
`,1,Pat_Point,Auckland,Pat_Smith,-Unpublished-,300,IBN`.

#### Keep a nodelist segment for your net

1. Under <kbd>S</kbd> **Settings**, fill in the **Host line**: your net's
   name, location and coordinator, and its flags, such as
   `IBN,INA:bbs.example.org`. Switch **Nodelist segment** on.
2. Press <kbd>N</kbd> **Nodes**, then <kbd>C</kbd> **Create** for each node
   in your net. A new line starts from your first downlink in the net that
   isn't listed yet, with its name and BinkP host filled in. Add its
   location and sysop.
3. **Listed as** is a plain node, or **Pvt**, **Hold** or **Down**.

The segment is checked the way your BBS checks a nodelist it receives: its
first line carries the CRC-16 of the rest.

#### Publish

- Press <kbd>P</kbd> **Publish now**. Today's edition of each list that is
  on is written, and hatched where a file echo is set. You are asked first
  only when something goes to the network.
- Or set **Schedule** to **daily**, or **weekly** on a **Weekday** (Friday
  by default). A scheduled edition comes out on the first check of that
  day, while the network is on.

A hatched list goes to your hub with a TIC, as a hatched file does, and
tells the receiving systems that it replaces your previous edition. It
needs the network's **TIC password**. Your points and downlinks that carry
the file echo get it from you too, each under its own **TIC password**; a
link without one, or one that paused itself, gets nothing.

Fields are plain ASCII. Spaces become underscores, and a comma is refused,
since commas separate the fields. A line longer than 157 characters is
refused too, since some list processors drop it.

> [!NOTE]
> A list that can't be built (no point listed, say) is not written, and the
> other one still is. A list written but not hatched stays in the folder;
> the screen says why it wasn't hatched.

### Not supported

- carrying an echo for links without a board of your own (pass-through
  areas), or asking your hub for an area a link wants;
- sending bundles in any format but ZIP.

> [!NOTE]
> Check a network's rules before gating its echoes anywhere else.

## QWKnet networks (DOVE-Net)

| At a glance | |
| --- | --- |
| **Set up under** | **Settings ▸ DOVE-Net and QWKnet** |
| **Decide first** | Which network to join, and whether other systems will call yours for it |

NetBBS can join a QWK network, such as DOVE-Net, the network most
Synchronet BBSes are on, as a node of a hub. It can also be the hub other
systems call. Each conference of the network is carried by one of your
boards.

> [!IMPORTANT]
> It is a gateway, not NetBBS Link. A QWKnet board is never also Linked or
> FTN, and nothing that arrives over QWKnet is trust-evaluated.

In this chapter: [Join a QWK network](#join-a-qwk-network) ·
[Carry conferences](#carry-conferences) ·
[Held messages](#held-messages) · [Serve other systems](#serve-other-systems) ·
[What QWKnet does not carry](#what-qwknet-does-not-carry)

### Join a QWK network

1. Pick your QWK ID: 2-8 letters and digits, starting with a letter, unique
   on the network. DOVE-Net lists the IDs in use in `nodes.dat` in
   `dove-net.zip`.
2. At the hub's BBS, create an account whose user name is your QWK ID, and
   say it is for QWK networking. On Vertrauen (`vert.synchro.net`), log in
   as `New` over Telnet and answer **Yes** to "Is this account to be used for
   QWK Networking (DOVE-Net)?". Note the password.
3. In the account's QWK settings at the hub, turn **Include HEADERS.DAT** on.
   Ctrl-A codes may stay in; NetBBS strips them.
4. In **Settings ▸ DOVE-Net and QWKnet**, press <kbd>C</kbd> **Create**. A new
   network starts as DOVE-Net through Vertrauen, the only template, so there
   is nothing to pick. It fills the name `DOVE-Net`, **Hub QWK ID** `VERT`,
   **Host** `vert.synchro.net` and **Transport** HTTPS; **Our QWK ID** starts
   as your QWK BBS ID. Check **Our QWK ID**, then set **Password** to the
   account's password and **Enabled** to yes. The template fills no password
   and switches nothing on, **Serve nodes** included, and nothing is stored
   before <kbd>S</kbd> **Save**.
5. Press <kbd>S</kbd> **Save**, then map the conferences (below), and press
   <kbd>P</kbd> **Poll now**.

| Setting | Default | Effect |
| --- | --- | --- |
| Our QWK ID | your QWK BBS ID | Your account at the hub, and how other systems' routes name your BBS. |
| Tagline | the BBS's name | The line under every post written on your BBS: ` ■ NetBBS ■ <tagline>`. |
| Hub QWK ID | VERT | The hub's ID. It names the packets: `VERT.QWK` from the hub, `VERT.REP` to it. Empty when your BBS is the network's top hub. |
| Host, Port | vert.synchro.net | Where the hub answers. Port 0 uses the transport's usual one. |
| Transport | HTTPS | **HTTPS** or **HTTP** call the hub's web server the way Synchronet's `qnet-http` does. **FTP** and **FTPS** do what `qnet-ftp` does, in passive mode only. |
| Poll every | 240 minutes | How often to call. Posts written on your BBS also go up within a quarter of an hour. |

> [!WARNING]
> Plain HTTP and FTP send the password readable. Use HTTPS or FTPS wherever
> the hub offers them.

Each call uploads your new posts and then fetches what waits. The network
list shows what the last call came to; a failed call is tried again after a
few minutes. A packet that can't be read, or that names another BBS, is held
whole under <kbd>H</kbd> **Held**.

### Carry conferences

Highlight the network and press <kbd>M</kbd> **Map conferences**. The list
shows the boards that can carry a conference: local boards, and those
carrying one of this network's already. Pick a board and type its conference
number, such as `2001` for DOVE-Net's General. An empty line makes the board
local again; its posts stay.

- Messages from the network are posted as `Name (ORIGIN)`, where ORIGIN is
  the QWK ID of the BBS the message was written on.
- A moderated board holds them for you, as it holds local posts.
- What your callers post on the board goes to the hub, with your tagline.
- Polls are not offered on a QWKnet board.
- Mail the hub sends to `SYSOP` or your QWK ID reaches you; mail to one of
  your accounts by name reaches that account.

> [!NOTE]
> A board carries one network: a Linked or FTN board can't carry a
> conference until you make it local.

### Held messages

A message for a conference no board carries is held, by conference. Highlight
the network and press <kbd>H</kbd> **Held**:

- <kbd>R</kbd> **Re-toss** posts a conference's messages once a board carries
  it.
- <kbd>D</kbd> **Delete** drops them.

The newest 2,000 messages per network are kept, for 30 days.

### Serve other systems

Your BBS can be the hub for other systems, NetBBS or Synchronet, for a
network you run or for one you pass on.

1. In the network's settings, turn **Serve nodes** on. For a network of your
   own, leave **Hub QWK ID** empty; your QWK ID there is the hub's.
2. Highlight the network, press <kbd>N</kbd> **Nodes**, then
   <kbd>C</kbd> **Create** for each system.
3. Set **QWK ID** to the system's QWK ID and **Password**, typed unseen.
   **Conferences** starts with all of the network's; untick what it doesn't
   carry.
4. Press <kbd>S</kbd> **Save** and give the system's SysOp the details:

| They need | Value |
| --- | --- |
| Hub ID | your QWK ID on this network |
| User name | their QWK ID |
| Password | the one you set |
| A Synchronet call-out command | `?qnet-http -s %s <your host> <password>` |
| A NetBBS network | Transport HTTPS, Host your host |

Nodes call your web listener at `/qwk.ssjs`, so the web listener must be
reachable from the internet.

> [!IMPORTANT]
> Put the web listener behind TLS, as for callers' browser logins. Nodes
> send their password with every call, readable over plain HTTP.

- Each node gets what was posted after its account was made: your callers'
  posts, the other nodes' and, on a network you pass on, the hub's. It never
  gets its own posts back.
- **Packet size** (2,000 messages) and **Reply packet limit** (4,096 KiB)
  bound each call. A packet is served again until the node confirms it.
- The node list shows each account's last fetch, upload and outcome.
- An address that fails 10 logins in 15 minutes is refused until the window
  passes. A node account that is off is refused too.

Your BBS serves nodes over HTTP only, never FTP: a node whose software can't
call a hub over HTTP can't use your BBS as its hub.

### What QWKnet does not carry

- routed QWKnet netmail, and mail between your BBS and its nodes;
- Synchronet's control messages, such as `ADD` and `DROP`: change a node's
  conferences in its account;
- polls and votes;
- file requests and attachments;
- messages passed from one network to another.

## Daily operation

| At a glance | |
| --- | --- |
| **Where** | The SysOp console's dashboard, **Settings** and **Operations** |
| **From the shell** | `python -m netbbs.admin` (the console with the node stopped), `python -m netbbs.search check\|rebuild --db PATH` |
| **Decide first** | Your display timezone, and whether guests may use the node map and your own doors |

In this chapter: [Routine checks](#routine-checks) ·
[Limits and retention](#limits-and-retention) ·
[The guest account](#the-guest-account) ·
[Node and sessions](#node-and-sessions) ·
[Logs and search indexes](#logs-and-search-indexes) ·
[Custom banners and mastheads](#custom-banners-and-mastheads)

### Routine checks

Check the dashboard's:

- pending registrations, posts and files;
- backup recency;
- free disk space;
- recent errors.

Staff and moderators see what waits for them under **Moderation (n)** and on
the Staff console, so a queue can empty while you are away.

Choose welcome/masthead/banner presets through **Settings**; preview before
applying, or place your own files as described under
[Custom banners and mastheads](#custom-banners-and-mastheads). The default
welcome banner mentions NetBBS Link only when the node ran with Link on at its
last start.

> [!NOTE]
> Timestamp format and display timezone are node-wide. Until you choose a
> timezone under **Settings ▸ Timestamp format**, times are shown in UTC: the
> main-menu clock says `UTC`, and the node log reminds you at every start.

**Settings ▸ Recent callers** cycles through three states: the panel after
login in its default neon style, the same panel plain (your header colour and
a quiet heading), and hidden.

**Settings ▸ Wall of oneliners** cycles the oneliner wall after login through
the same three states: neon, plain and hidden. The wall is the short lines
callers leave, drawn as a lower **Oneliners** section of the Recent callers
panel, in that panel's style. With Recent callers hidden, the wall is shown
alone in its own style. ▸ **Recent callers** on the main menu shows both
whatever these two settings say.

- A line is plain text, up to 60 columns.
- A caller can add one line per call and three a day. Deleting a line does
  not give one back. Every guest shares the guest account's three.
- The wall keeps its newest 200 lines.
- Who may add lines is the **Oneliner level** under
  [Limits and retention](#limits-and-retention).

To delete a line, open ▸ **Recent callers** ▸ <kbd>O</kbd> **Oneliners**,
press <kbd>D</kbd> **Delete**, type its number and answer <kbd>Y</kbd>. A
SysOp and staff with **moderate everything** can delete any line, and the
audit log records it with the line's text. Callers delete their own lines.

> [!NOTE]
> A block does not hide anyone's lines: the wall is public, like a chat
> channel. Delete a line that should not be there.

### Limits and retention

**Settings ▸ Limits & retention** holds eight node-wide values, saved together
and applied without a restart:

| Setting | Default | Effect |
| --- | --- | --- |
| Upload cap | 100 MiB | Largest single upload, over Zmodem and the browser. Files arriving by FTN file echo or netmail attachment are held to it too, at most 64 MiB. Keep a reverse proxy's body limit at least 1 MiB higher. |
| Grace before deletion | 7 days | How long an expired post or file waits before it is deleted. An expired file can be recovered from its area until then. A change counts for items already expired. |
| Invitation expiry | 7 days | When an unaccepted channel invitation lapses. Clear the field for invitations that never expire. A change applies to invitations sent afterwards. |
| Chat scrollback | 100 messages | Lines each channel keeps, carried Link channels included. Lowering it trims a channel the next time someone speaks there. |
| Node map level | 0 | The lowest level that may open **Directory ▸ Node map**. A guest is an ordinary account: set this above the guest account's level to keep the map from guests. |
| Mail level | 0 | The lowest level that may open **E-mail**: read, write and reply, here and to other BBSes. Below it the main menu offers no E-mail. Mail sent to an account below it still arrives and waits until you raise the account's level. |
| Cross-post boards | 5 boards | The most message boards one new post may go to with **To boards** on its review screen, the board it was written on included. Each board gets its own copy, so this bounds what one post writes. 1 turns cross-posting off. |
| Oneliner level | 0 | The lowest level that may add a line to the oneliner wall. Every caller can read it. A guest is an ordinary account: set this above the guest account's level to keep guests read-only. |

### Offline mail (QWK)

Callers find offline mail as <kbd>Q</kbd> **QWK offline mail** on the
message board list. A packet holds their unread mail and the boards they
follow, to read in a QWK reader such as MultiMail; the reply packet the
reader makes is posted here.

- **Replies go through the usual checks.** Each reply is posted as if the
  caller had written it online: a board's write level, name and age gates
  and moderation all apply, a post on a shared or FTN board goes out to the
  other BBSes, and each reply is reported back to the caller, refused ones
  with the reason. The guest account gets no packets.
- **Transfers** use Zmodem or the browser links the file areas use. A node
  without a public web address hands browser links only to callers in its
  browser terminal.

**Settings ▸ QWK offline mail** holds its two node-wide values, saved
together:

| Setting | Default | Effect |
| --- | --- | --- |
| QWK packet size | 2,000 messages | The most messages one packet holds (50-10,000). The rest wait for the caller's next packet. A packet also stops at 16 MiB of text. |
| QWK BBS ID | from the node name | The name packets carry (`ID.QWK`) and that a reply packet (`ID.REP`) must match: 1-8 letters and digits. Empty uses the node's display name, cut to fit. |

> [!WARNING]
> Set the BBS ID once. Readers file packets by it, and reply packets made
> for an old ID are refused.

### The guest account

Guest login is set under **Settings ▸ Guest access**. Every guest signs in as
the same account.

#### Set what every guest starts with

1. Open the guest account under **Users ▸ User list**.
2. Press <kbd>E</kbd> **Edit profile**. The screen is titled
   **Guest defaults**.

It offers:

- the guest account's bio and signature;
- whether it takes messages;
- who it blocks (their live messages to guests are refused);
- its MRC settings;
- whether its name is shown to other callers;
- the display settings.

Each guest can still change the display settings (except the MRC nick
colour), the fullscreen editor and stylized MRC names for their own call; the
rest is the same for every guest. Name and details, read receipts, sort
preferences, keys and password are not on it. Only a SysOp can set the guest
defaults, and each change goes into the account's **History**.

#### What a guest call cannot change

A session that came in through guest login can't change what other callers
see of the guest account: its bio, signature, name and details, who it
blocks, whether it takes messages, its MRC settings, its chat alias or its MRC
hub registration. A guest may still change display settings (character set,
colors, redraw style, banners, sort orders), but only for that call.

<details>
<summary>Why guests can't change the shared profile</summary>

Without that rule, any anonymous caller could rewrite the profile every caller
sees, and a self-entered birthdate would open age-gated areas for every later
guest.

</details>

Each guest call is kept apart from the next in three more ways:

- **Drafts:** its drafts are its own and are deleted at hang-up.
- **Posts and files:** it may edit, withdraw or describe only the posts and
  files it wrote during that call. Whatever earlier guests wrote under the
  account stays as it is, and says so.
- **Bundled games:** it plays Voidrunner, War Dialer, Lunar Lander and Retro
  Trivia under an identity of its own, with a save that is thrown away at
  hang-up. A guest sees the real Hall of Fame, War Dialer world and best
  pilots, but its career, its callsign, its moves and its landings never
  reach them.

> [!WARNING]
> Your own (non-bundled) doors still see the guest account; gate them by
> level if a guest should not reach them.

#### Guests and mail

The guest account never has mail, whatever its level and the mail level:
every guest signs in as the same account, so its inbox would be shared by
strangers and its letters sent under one name.

- A caller who writes to it is told the account has no mailbox.
- Link mail to it bounces, and the sender is told the account takes no mail.
- If the guest account has mail from before this rule, it stays in the
  database, unreadable by guests. Turn guest login off and sign in as the
  account to read or delete it.

#### Permissions the guest account can't hold

The guest account can't be given staff permissions, identity verification or
a moderator grant: anyone who signs in as the guest would hold it.

- Each screen that grants one says so.
- An account holding one can't be designated as the guest.
- A guest account that somehow holds one signs nobody in until you remove it.

Read and post grants still work, to open an area to guests above its level.

### Node and sessions

Under **Operations ▸ Node and sessions**:

| Entry | What it does |
| --- | --- |
| <kbd>W</kbd> **Who** | Lists sessions and disconnects one. |
| <kbd>O</kbd> **Observe live** | Opens the Monitor (below). |
| <kbd>M</kbd> **Maintenance mode** | Blocks new non-SysOp logins until you turn it off or the node restarts; connected callers stay. |
| <kbd>D</kbd> **Drain** | Warns and disconnects non-SysOp sessions after a delay. |
| <kbd>L</kbd> **Lock & drain** | Combines both for planned work. |
| <kbd>S</kbd> **Shutdown** | Stops the node after warning callers. |

> [!IMPORTANT]
> These controls require a live node session. For a stopped node, use the
> host's service controls.

#### Watch callers live: the Monitor

The Monitor is a live table of everyone connected, refreshed every two
seconds. It shows each caller's transport, address, time on, idle time,
terminal size and where they are, for example "Boards › Retro" or
"Doors › Voidrunner". On a terminal narrower than 80 columns, the address,
terminal size and transport columns are left out, in that order.

Up/Down selects a caller. Then:

| Key | What it does |
| --- | --- |
| <kbd>S</kbd> **Snoop** | Shows their screen live, as they see it, until you press any key. |
| <kbd>C</kbd> **Chat** | Breaks into their session for a two-pane chat. |
| <kbd>M</kbd> **Message** | Sends them a line. |
| <kbd>K</kbd> **Kick** | Opens the same disconnect screen as **Who**: an optional message, then <kbd>D</kbd> **Disconnect**, which is recorded in the audit log. |
| <kbd>U</kbd> **Unwind** | Sends them back to the main menu. |

<kbd>O</kbd> **Order** sorts the whole table by time on, idle time or name; a
mark in the heading shows which.

**Snoop:** the caller is not told at the time. Chat's `/help` and the User
Handbook say that you can do this, and the node log records every snoop: who
watched whom, and for how long.

**Chat:** you type in the top pane, they type in the bottom one, and
<kbd>Esc</kbd> ends it. Their screen is then repainted exactly as they left
it, with anything it printed during the chat and any half-typed line
included, and they carry on.

- It is refused during a file transfer.
- For a caller in a door you are warned first, because the door keeps running
  while they chat.
- The node log records every chat.

Below the table are the most recent logins, logoffs and disconnects. A message
or broadcast sent to you while the Monitor is open appears on the line above
the keys.

### Logs and search indexes

Under **Operations**:

- **Audit log** shows administrative and moderation activity.
- **Error log** reads the newest part of `netbbs.log`: warnings and errors
  first, <kbd>L</kbd> **Level** to show errors only or everything,
  <kbd>F</kbd> **Follow** to watch new lines.

Storage garbage collection and draft pruning show the proposed work before
confirmation; review it instead of deleting files directly.

**Operations ▸ Search indexes** compares what **Find** searches with the posts,
files, chat messages and mail themselves, and shows how many entries are
missing, stale or left over: counts only, never a letter's words.
<kbd>R</kbd> **Rebuild** replaces the indexes from that content; it cannot lose
any. It also works from `python -m netbbs.admin`, and this does the same from a
script:

```sh
python -m netbbs.search check|rebuild --db PATH
```

### Custom banners and mastheads

**Settings ▸ Mastheads & banners** holds eight optional pieces of
caller-facing art:

- **Session banners** are the welcome greeting, the log-off screen, and the
  screens shown before and after self-service signup.
- **Mastheads** sit above the main menu, the message-board list, the
  file-area list, and the chat channel picker.

Each piece has three ways in:

- <kbd>G</kbd> **Gallery**: bundled samples.
- <kbd>F</kbd> **From disk**: loads a file you have already put on the node.
- <kbd>U</kbd> **Upload**: sends one from your own computer.

You can also put your own `.ans` file where the node looks for it.

#### Upload a file

**Upload** needs the same route a caller's file upload does:

- a terminal that speaks Zmodem;
- NetBBS's own browser terminal (which needs no `public_url`);
- or, from any other terminal, a single-use browser link, which needs the web
  listener and its `public_url`.

Whatever you send is saved as that piece's own file, whatever it was called on
your side, up to 256 KiB. Uploading over an existing file asks first; if that
piece is enabled, callers see the new art at once. An upload never enables a
piece by itself, and it is recorded in the audit log.

#### Place a file yourself

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

For the `netbbs.db` in this handbook's examples, the prefix is `netbbs_`.

> [!TIP]
> Press <kbd>Ctrl</kbd>+<kbd>H</kbd> (<kbd>F1</kbd> in SyncTERM) on a piece's
> own screen to see the exact path for your node.

#### Turn a piece on and off

> [!IMPORTANT]
> Placing the file does not turn it on. Each piece has its own switch, off by
> default, and callers keep seeing the built-in default until you open that
> piece's screen under **Settings ▸ Mastheads & banners** and choose
> <kbd>E</kbd> **Enable**.

- Until you do, its status line shows `disabled -- file: <name> (N bytes)`.
- <kbd>P</kbd> **Preview** shows your saved art even while it is switched off,
  and says under it what callers see meanwhile; with nothing saved it says
  that too.
- **Enable** refuses a missing file or one over 256 KiB. If a file that was
  enabled later goes missing or grows past that limit, callers get the default
  silently and the node logs a warning.
- <kbd>D</kbd> **Disable** turns a piece off and leaves its file in place.
- To get rid of the file, <kbd>R</kbd> **Remove file** (offered while one is
  saved) deletes it after one yes/no and switches the piece off with it. It is
  recorded in the audit trail. The piece's **Mode** and **Speed** stay as they
  were and apply to the next file you place.

Empty rows at the bottom of a piece are not sent, so a banner drawn in the top
seven rows of the 24-row editor takes seven rows on a caller's screen. Empty
rows between parts of the art are kept.

#### Draw in the art editor

<kbd>O</kbd> **Open editor** opens the art editor on an 80x24 canvas. Typing
(a space too) paints over whatever is at the cursor. At the end of a row the
cursor stays put, so press <kbd>Enter</kbd> for the next row; <kbd>End</kbd>
goes to just after the row's last character. Retyping a shorter line leaves the
end of the old one in place: <kbd>Ctrl</kbd>+<kbd>K</kbd> clears it.

| Key | Action |
| --- | --- |
| <kbd>Ctrl</kbd>+<kbd>K</kbd> | Clear from the cursor to the end of the row |
| <kbd>Ctrl</kbd>+<kbd>T</kbd> | Pick a block or line glyph |
| <kbd>Ctrl</kbd>+<kbd>P</kbd> | Pick the foreground colour |
| <kbd>Ctrl</kbd>+<kbd>B</kbd> | Pick the background colour |
| <kbd>Ctrl</kbd>+<kbd>L</kbd> | Repaint the screen |
| <kbd>Ctrl</kbd>+<kbd>G</kbd> | List every key |
| <kbd>Ctrl</kbd>+<kbd>O</kbd> | Save |
| <kbd>Ctrl</kbd>+<kbd>X</kbd> | Quit |

#### Bundled designs and scene art

The welcome gallery ends with three quiet designs for clubs that don't want
neon: **Paper & Ink**, **Library Card** and **Garden Gate**. Every bundled
design uses only characters CP437 has, so it looks the same in SyncTERM,
PuTTY's default font and a browser. Your own art can use anything; on a
classic terminal, a character CP437 lacks is drawn as the nearest shape it
has.

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
- **Credit line:** on the welcome banner's screen, <kbd>C</kbd> **Credit line**
  switches on a line under your banner crediting the art from its SAUCE
  record, for example `art: Nib Logo by InkWell/Quill`. It is off by default.

#### The main menu as your own art

The main-menu masthead can also be the menu itself. On its screen,
<kbd>M</kbd> **Mode** switches between *above the menu* (the default) and
*the menu itself*. In the second mode your art is the whole main menu, and
NetBBS draws each caller's own items and a few live values into places you
mark in it. You mark a place by drawing a token in plain text, in the colour
its content should take:

| Token | What goes there |
| --- | --- |
| `{menu 74x7}` | The caller's menu items, in a region 74 columns wide and 7 rows tall, starting at the `{`. Required. |
| `{prompt}` | The prompt. Without one, it goes below the art. |
| `{user 16}` | The caller's name, cut to 16 columns |
| `{node 30}` | Your node's name |
| `{level 3}` | The caller's level, such as `20` |
| `{mail 4}` | How many unread letters, such as `3` (`0` when caught up) |
| `{time 5}`, `{date 10}` | The node's time and date, in its timezone |
| `{online 3}` | How many callers are signed in, such as `4` |

- Without a number, a field is as wide as its token.
- Brace text that is not one of these stays part of the art.
- Counts and levels are bare numbers, so draw the words around them yourself
  (`mail {mail 4}`, `{online 3} online`).
- A value wider than its field is cut; a field too narrow for `...` plus two
  characters is cut without it, so a number keeps its digits.

The art only decorates. Each caller sees exactly the items the normal menu
would show them, with the same keys; the art can't add or hide any. A caller
gets the normal menu instead whenever the art can't be used as drawn:

- **their items don't fit** the `{menu}` region (a SysOp's menu is the
  longest);
- **the art has a problem**, such as no `{menu}` and no drawn items, or two
  slots overlapping;
- **their terminal is too small**, narrower than the art or too short for it;
- **they read plain ASCII.**

<kbd>C</kbd> **Check** lists the tokens it found and any problems, and says
whether your menu and a level-0 caller's fit, on a terminal the size of yours.
**Preview** shows the menu as you see it, then as a level-0 caller sees it.
The gallery's **Quill Ledger** and **Inkwell Blocks** samples are drawn this
way, in 16 colours with characters classic terminals have; applying one
switches the mode for you.

##### Draw the menu items yourself

You can also draw the menu's items into the art yourself, such as
`[M]essage boards` or `[A]pprovals`, instead of leaving all of them to
`{menu}`:

- **Any bracketed key is an item.** You don't need a token. The item is the
  text around the key, up to two spaces or a frame character on either side,
  which is also what a browser caller can click. Put at least two spaces or
  a frame character such as `│` between items: `[M]essage boards [E]-mail`
  with one space is one item, blanked only for a caller who can use neither
  key, and a browser click on it always means `[M]`, while
  `[M]essage boards│ [E]-mail` is two.
- **Items a caller can't use are blanked.** A level-20 caller doesn't see your
  drawn `[S]ysOp console` at all: its cells are painted over in the colour
  behind them, so your frame and fill stay whole. Putting staff-only items on
  their own row keeps the gap tidy. A staff member's `[S]` opens the Staff
  console, so they don't see a drawn `[S]ysOp` either: their `[S]taff` goes
  into `{menu}` instead.
- **Items you didn't draw go into `{menu}`.** Games, Topics, Who's online,
  Approvals and Invitations appear only for some callers. Leave a `{menu}`
  region for them: without one, a caller with an item you didn't draw gets the
  normal menu.
- **Other bracketed text stays as drawn.** `[x] marks the spot` is decoration,
  since X is no menu key.

> [!WARNING]
> **Redraw items whose key moved.** Every main-menu key is now its label's
> first letter, so Communities moved from `[O]` to `[T]opics` and the staff
> list from `[T]` to `[O]perators`. Art that still draws `C[o]mmunities` or
> `S[t]aff list` would send a caller to the wrong screen, so those drawn items
> are blanked and the real ones go into `{menu}` until you redraw them.
> `P[r]evious callers` and `Moder[a]tion` keep working (now `[R]ecent callers`
> and `[A]pprovals`), but read better redrawn.

**Check** lists the items it found and says which items it blanks, and which
go into `{menu}`, for you and for a level-0 caller. **Preview** shows both
menus with those items already blanked and moved. The gallery's **Card
Catalogue** sample is drawn this way.

#### The board, file-area and channel lists as your own art

The three list mastheads (message boards, file areas, chat channels) work the
same way. **Mode** on each one's screen makes the art the list itself, and a
page of the list is drawn into a region you mark:

| Token | What goes there |
| --- | --- |
| `{list 74x12}` | One page of the list, one entry per row, in a region 74 columns wide and 12 rows tall. Required. |
| `{title 40}` | The list's title, such as `Available message boards` |
| `{page 6}` | The page, such as `2/5` |
| `{count 5}` | How many entries, such as `14` |

`{prompt}`, `{user}`, `{node}`, `{level}`, `{time}` and `{date}` work as on the
main menu.

- Each row shows the number to press, the name, and one value: what is new on
  a board, how many files an area holds, or who is in a channel.
- A board, area or channel the caller can't use yet because it asks for a
  verified name says `needs verification` there instead.
- Descriptions stay on the normal list.
- A page holds as many entries as the region has rows, and the keys work as on
  the normal list: type the number, move with the arrow keys, page with
  <kbd>N</kbd> and <kbd>P</kbd>, and search with <kbd>S</kbd>.

A caller gets the normal list:

- when the art has a problem;
- when they read plain ASCII;
- when their terminal is too small;
- when the region is under 3 rows or leaves names fewer than 12 columns.

**Check** says whether a list fits, including one with a `needs verification`
entry, whose value is the widest. **Preview** draws the first page of your own
list into the art. Each gallery has a sample drawn this way: **Board Ledger**,
**Filing Cabinet** and **Parlour**; applying one switches the mode for you.

#### Tokens in the welcome and log-off banners

The welcome and log-off banners take the field tokens too, but not `{menu}`
or `{prompt}`. Any other field is left blank, and a banner without tokens is
sent exactly as before.

| Banner | Fills |
| --- | --- |
| Welcome (shown before anyone signs in) | `{node}`, `{time}`, `{date}`, `{online}` |
| Log-off | The welcome banner's, plus `{user}` and `{level}` |
| SSH's sign-in banner | `{node}`, `{time}`, `{date}` |

#### Opening animations

The welcome banner, the main-menu masthead and the three list mastheads
(Boards, file areas, Chat channels) can play as an opening animation.

- On the piece's screen, <kbd>S</kbd> **Speed** steps through off (the
  default), 2400, 9600 and 38400 bps: NetBBS then draws the art at that speed,
  the way a modem of the day would have, so ANSI art made as an animation
  plays as one and still art builds up from the top.
- Any key draws the rest at once, and that key is used up, so it doesn't also
  act at the prompt that follows. A paced draw plays to the end.
- If you'd rather a slow piece not hold callers up, **Time limit**
  (<kbd>T</kbd>) next to **Speed** steps through off (the default), 10, 30 and
  60 seconds: after that long the rest is drawn at once.

Each piece plays once per connection: the welcome banner when a caller
connects, the masthead on their first main menu, a list's art on the first
visit to that list, never on a redraw, a page change or a cursor move.
Callers who read plain ASCII, and callers who set
**Profile ▸ Banner animations** to quick, get the art at once. The piece's
**Preview** plays it at its speed every time you use it, so you can see the
animation without signing in as a caller.

#### Character sets callers get

Every caller gets text in the character set their terminal reads.

**Detection when they connect:** a Telnet caller's terminal is asked what
it is.

| Caller | Gets |
| --- | --- |
| SyncTERM and other classic BBS terminals | CP437, so your art arrives exactly as drawn |
| Modern terminals | Unicode |
| A Telnet client that doesn't say | Plain ASCII for everything before sign-in, your banner included: boxes become `+`, `-` and `\|` |
| SSH | Detected from their terminal type, but the banner SSH shows during login is always ASCII |
| Browser | Always Unicode |

The node log has one line per
Telnet or SSH connection saying what the terminal called itself and which
character set it got, for example:

```text
telnet caller 203.0.113.9 terminal type: 'syncterm' (answered); character set cp437 (certain)
```

Look there if a caller's screen looks wrong.

Classic terminals:

- Classic terminals such as SyncTERM jump to the next line as soon as they
  fill the last column. NetBBS lays out every screen one column narrower for
  them, so nothing gets double-spaced and the screen doesn't scroll when the
  bottom row is written. Your banners keep their full width.
- Plain spaces at the end of each banner row are dropped when it's shown, so
  draw background colours out to the edge if a row should reach it.
- SyncTERM also gets full colour (truecolor), so doors and colourful presets
  look the way they do in a modern terminal. Other classic terminals, and a
  Telnet caller whose terminal reports nothing, get the 16 classic ANSI
  colours: many of them, such as the fTelnet **Connect** button on BBS
  directory sites, blink or garble text sent in 256 colours. Your banners
  and presets are drawn in the nearest of the 16. A caller can still pick a
  colour depth in their Profile.

**After sign-in:**

- Each caller's own **Profile ▸ Character set** choice applies.
- A caller whose terminal wasn't recognised is asked once which of two sample
  lines looks right.

### A welcome letter for new accounts

**Settings ▸ Mastheads & banners ▸ Session banners ▸ New-account letter** sets
up a letter that every new account finds unread in its mailbox.

- **When it arrives:** as soon as the account can sign in. That is right away
  when anyone may sign up, when you approve the account if signups need
  approval, and when you create the account on the console. Accounts that
  already exist never get it, and no account gets it twice.
- **Who it's from:** a real account, so a reply reaches a person. Whoever
  switches the letter on is the sender until you pick another with
  <kbd>F</kbd> **From**: any SysOp or staff account that may write mail.
- **What it says:** <kbd>E</kbd> **Edit text** opens the letter in the mail
  editor, and <kbd>S</kbd> **Subject** changes its subject. `{user}`, `{node}`
  and `{date}` are filled in for each new account. A letter already sent stays
  as it was when you change the text.
- **Before you switch it on:** it starts off, with a sample text to rewrite.
  <kbd>P</kbd> **Preview** shows it as a new caller will read it, and
  <kbd>O</kbd> **On/off** switches it.

> [!NOTE]
> A letter that can't be sent never stops the account. If the sender has
> lost its staff access or can no longer write mail, the account is created
> without a letter, and the screen shows what went wrong and when.

## State, backup, and recovery

| At a glance | |
| --- | --- |
| **Where** | **Operations ▸ Full backups** in the SysOp console |
| **From the shell** | `python -m netbbs.backup create`, `python -m netbbs.backup restore` |
| **Decide first** | Where backups go, how often, and how you copy them off the machine |

In this chapter: [Know what belongs to the node](#know-what-belongs-to-the-node) ·
[Create and verify a backup](#create-and-verify-a-backup) ·
[Restore](#restore)

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
| Lunar Lander pilot records | `/var/lib/netbbs/netbbs.db.doors/lunar_lander/pilots.db`, unless the door's profile sets `DOOR_LL_DATA_DIR` |
| Voidrunner careers | `/var/lib/netbbs/netbbs.db.doors/voidrunner/`, unless `VOIDRUNNER_SAVE_DIR` overrides it (a node upgraded from the old default copies the service account's `~/.netbbs/voidrunner_saves/` there at its first start) |
| Third-party games | Each door's installation directory and any author-documented external state |
| TOML and service configuration | `/etc/netbbs/` and the installed service files |
| Logs | `netbbs.log` beside the database, plus service-manager output |

Keep your configuration, service settings, and external-game state with your
recovery records. The built-in backup is not a copy of every file the service
account can access.

> [!WARNING]
> Do not copy only a running `.db` file: SQLite's write-ahead log can contain
> changes not yet written into that file.

### Create and verify a backup

#### Back up from the console

1. Stop active games before capture, and halt companion services. The BBS
   itself may stay running for a supported database backup.
2. From a live SysOp console, open **Operations ▸ Full backups** and press
   <kbd>C</kbd> **Create backup now**. It writes a timestamped directory under
   `netbbs_backups/` beside the database, or under the destination you set.
3. Check the reported path and game coverage.

#### Schedule backups

<kbd>S</kbd> **Schedule & destination** on the same screen sets both without a
cron job:

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
run is not retried until the next scheduled time.

> [!NOTE]
> The running node makes the backups; `python -m netbbs.admin` only changes
> the settings.

#### Back up from the shell

For a one-off destination or a script, use the installed backup CLI. Run it
as an account able to read all node state:

```sh
sudo -u netbbs /var/lib/netbbs/.venv/bin/python -m netbbs.backup create \
  --db /var/lib/netbbs/netbbs.db \
  --identity-dir /var/lib/netbbs/netbbs_identity \
  --to /var/lib/netbbs/backups/pre-upgrade-20260913
```

The destination must not already exist; choose a fresh name for each backup.

#### Voidrunner careers

Voidrunner careers live beside the database, in `netbbs.db.doors/voidrunner/`,
so the CLI finds them from `--db` alone. The node also records the directory it
uses at startup, and the CLI reads that first, so a `VOIDRUNNER_SAVE_DIR`
override is followed too. Two cases still need you:

- **A node upgraded from the old home-directory default that has not yet
  started since.** The CLI says so in as many words -- `Voidrunner: NOT
  CAPTURED` or `GUESSED LOCATION` -- rather than reporting an empty directory
  as an empty node. Start the node once (it copies the careers into place), or
  pass `--voidrunner-save-dir` with the old directory.
- **A directory you chose yourself, when the node has not recorded it.** Pass
  `--voidrunner-save-dir` with the path shown on the live Backup screen.

> [!IMPORTANT]
> A node upgraded straight from v7.4.0 or earlier never recorded that
> directory, so it does **not** copy the careers: set `VOIDRUNNER_SAVE_DIR` to
> the old directory before its first start, or copy its contents into
> `netbbs.db.doors/voidrunner/` with the node stopped.

Read the coverage output either way. An absent Voidrunner component will not
magically reappear during restore.

<details>
<summary>Why older nodes need this</summary>

Careers used to live under the home directory of whichever account started
the node, which a CLI run from your own shell does not share: up to v7.4.0
that silently captured no careers at all
([#555](https://github.com/Thiesi/NetBBS/issues/555)).

</details>

#### Other games and door installations

War Dialer has separate world capture and restore rules; see the
[door guide](NetBBS-door-guide.md).

Lunar Lander's pilot records are included automatically and can be backed up
while callers play. Restore puts them back beside the database; stop the node
first, as for any restore.

Third-party installation directories are excluded unless you include them:
press <kbd>D</kbd> on the Backup screen to switch its **Door installations**
setting on or off. The Backup screen shows the door sections, and that option,
only once a door is set up.

- That option copies them without stopping their writers and does not
  automatically restore them. Stop games/services first.
- A missing or unreadable requested installation fails the backup.
- Symlinks are copied as links, not followed to external data.

Door outbound result receipts (`door-outbound/` beside the database) are
included automatically and need no separate copy. Restore replaces them with
the archive's own, so receipts a door wrote after that backup was taken go to
the rollback generation rather than staying beside an older database; an
archive made before NetBBS captured receipts clears them for the same reason.
After recovery, a missing receipt still does not mean its post was never
published.

#### Keep backups safe

> [!CAUTION]
> Backups contain private keys and account data. Copy completed backups off
> the machine, protect them as secrets, and encrypt them if needed.

This is **manual work outside NetBBS**:

- Off-site transfer is not built in, and retention covers only the schedule's
  own backups on this machine.
- Also preserve TOML, service configuration, and any game data outside the
  captured paths.
- Inspect `manifest.json` and the coverage messages before relying on an
  archive.

### Restore

1. Stop the node and every game/service. Keep the current state until the
   restored node has been verified.
2. Run the backup tool, which validates and stages the restore and refuses to
   overwrite an active node:

   ```sh
   sudo -u netbbs /var/lib/netbbs/.venv/bin/python -m netbbs.backup restore \
     --from /path/to/backup \
     --db /var/lib/netbbs/netbbs.db \
     --identity-dir /var/lib/netbbs/netbbs_identity \
     --voidrunner-to /var/lib/netbbs/netbbs.db.doors/voidrunner \
     --war-dialer-to 1=/var/lib/netbbs/netbbs.db.doors/war-dialer.db
   ```

3. Do the **manual** part: restore external door installations from
   `door-installs/` if included, restore service/TOML settings, and ensure the
   service uses the restored game paths.
4. Restart, log in, read a post, retrieve a file, check Link identity/peers,
   and enter a real saved game.

About the restore options:

- Use `--voidrunner-to` when the archive includes that component; omit it for
  an archive without it.
- The example assumes one captured War Dialer world, key `1`. Every captured
  world requires an explicit `--war-dialer-to KEY=PATH` mapping, even for its
  default location; omit this option if there is no world component. Read the
  keys in the backup's manifest and follow the door guide for multiple or
  overridden worlds.
- Careers restored into the node's own `netbbs.db.doors/voidrunner/` need no
  setting. Anywhere else, set `VOIDRUNNER_SAVE_DIR` to that destination;
  restore never changes that environment setting for you.

Restore preserves replaced state in a rollback directory and reports its
location; it does not delete that directory later.

> [!WARNING]
> Do not run the same restored Link identity on two live nodes.

> [!TIP]
> Rehearse this first on disposable state using the
> [disaster recovery drill](NetBBS-disaster-recovery-drill.md). Automated
> backup tests do not replace a recovery exercise on your host.

## Upgrading and removing NetBBS

| At a glance | |
| --- | --- |
| **Where** | **Settings ▸ Update** in the SysOp console |
| **From the shell** | `pip install` of the release wheel into the node's virtual environment |
| **Decide first** | Whether NetBBS may restart itself after an install, and how you would roll back |

In this chapter: [Update checks](#update-checks) ·
[Install from the console](#install-from-the-console) ·
[Upgrade by hand](#upgrade-by-hand) ·
[Roll back](#roll-back) · [Remove NetBBS](#remove-netbbs)

### Update checks

The node checks GitHub Releases on startup and every 24 hours by default; a
startup within 15 minutes of the last attempt skips another check. Results
appear on the SysOp dashboard and **Settings ▸ Update**, where you can:

- check manually with <kbd>C</kbd> **Check now**;
- toggle the schedule with <kbd>A</kbd> (**Auto-check on** or **off**);
- set an optional GitHub token for a higher API limit with
  <kbd>T</kbd> **Token**.

These checks do not download, install, restart, or interrupt callers.

### Install from the console

When a check has found a newer release, **Install vX** (<kbd>I</kbd>) appears on
**Settings ▸ Update** on the live node. It first shows the plan, and does
nothing until you choose <kbd>I</kbd> **Install now** and answer yes.

> [!IMPORTANT]
> Read the release notes first, and stop games and companion services, as for
> any upgrade.

The steps run in order, and a failure stops the rest and says why:

1. Download the release's wheel from GitHub and check it against the SHA-256
   digest the release publishes. A wheel without a published digest is not
   installed.
2. Back up this node to the backup destination (`netbbs_backups/` beside the
   database unless you set another), the same as <kbd>C</kbd>
   **Create backup now** under **Operations ▸ Full backups**.
3. `pip install` the wheel into the environment NetBBS runs from, with the same
   extras. pip also fetches any newer dependency the release needs. A failed
   pip run shows its last lines of output. The install is refused for:
   - a system Python (not a virtual environment);
   - a development checkout;
   - an environment the service account cannot write.
4. Restart, or not, as <kbd>R</kbd> **Restart after install** says:

   | Setting | Effect |
   | --- | --- |
   | **auto** | Restarts under systemd, detected, and not under NetBSD rc.d |
   | **yes** | Declares that your service manager restarts NetBBS when it exits |
   | **no** | Always stops after installing |

   A restart warns callers and waits the configured shutdown delay. The node
   then exits with status 75, and the service manager starts the new version.

> [!WARNING]
> Without a restart, the screen tells you to restart the service yourself. Do
> it promptly: until then, the old version runs with the new files on disk.

After the restart, the Update screen says whether the node came back as the
version that was installed.

> [!NOTE]
> **Manual, on the host, for units installed from an earlier release:** the
> shipped `netbbs.service` now carries `RestartForceExitStatus=75` and
> `SuccessExitStatus=75`. An older unit with `Restart=on-failure` already
> restarts on 75, but logs it as a failure. Add both lines, then run
> `systemctl daemon-reload`.

### Upgrade by hand

This route is **manual, on the host**, and always available:

1. Read the selected release's notes. Record the current version and paths.
2. Stop games/services and create a backup; verify its game coverage and keep
   an off-node copy.
3. Stop NetBBS through its service manager.
4. Install the selected GitHub-release wheel into the same virtual environment,
   using the same extras as before.
5. Start the service and verify a real login, content, file transfer, and
   games.

Startup applies required database migrations. An older build refuses a
database with a newer schema.

### Roll back

Rolling back stays a host procedure:

1. Stop the service.
2. Install the previous release's wheel.
3. Restore the pre-upgrade backup: the one the console install made in its
   step 2, or the one you made by hand.
4. Start the service.

> [!CAUTION]
> After a migration, restore the pre-upgrade state using compatible tooling;
> do not attempt to downgrade the database by hand. Keep the newer state
> separately if callers have contributed since the upgrade.

### Remove NetBBS

Disable the service first. Uninstalling with the environment's
`python -m pip uninstall netbbs` removes the package, not node data. Removing
state, keys, games, DNS registration, or backups is a separate, deliberate
operator action.

## Troubleshooting

| At a glance | |
| --- | --- |
| **Where** | **Operations ▸ Error log**, **Operations ▸ Diagnostics**, **Operations ▸ Audit log** |
| **From the shell** | `journalctl -u netbbs` (Linux), `netbbs.service.log` (NetBSD), `python -m netbbs.admin` |
| **Start with** | The symptom below, then [the logs](#reading-the-logs) |

| Area | Symptoms |
| --- | --- |
| [Starting the node](#starting-the-node) | [Service will not start](#service-will-not-start) · [Startup refuses the database version](#startup-refuses-the-database-version) · [Link will not start](#link-will-not-start) |
| [Callers and accounts](#callers-and-accounts) | [Caller cannot log in](#caller-cannot-log-in) · [Forgotten password or locked out](#forgotten-password-or-locked-out) · [Caller can read but cannot contribute](#caller-can-read-but-cannot-contribute) |
| [Connections](#connections) | [SSH import fails on NetBSD](#ssh-import-fails-on-netbsd) · [SSH client cannot connect](#ssh-client-cannot-connect) · [SSH host key was readable by other accounts](#ssh-host-key-was-readable-by-other-accounts) · [Browser terminal or upload fails](#browser-terminal-or-upload-fails) · [No file-transfer link](#no-file-transfer-link) · [Callers or peers cannot reach your address](#callers-or-peers-cannot-reach-your-address) |
| [Content, Link and games](#content-link-and-games) | [Find misses or lists the wrong content](#find-misses-or-lists-the-wrong-content) · [Peers connect but content is missing](#peers-connect-but-content-is-missing) · [Game is busy, fails, or loses state](#game-is-busy-fails-or-loses-state) |
| [Keys and backups](#keys-and-backups) | [A node key may have leaked](#a-node-key-may-have-leaked) · [Voidrunner: NOT CAPTURED](#voidrunner-not-captured) · [Voidrunner: no saves at](#voidrunner-no-saves-at) |

### Starting the node

#### Service will not start

- **Check:** read the service output; check config paths, file permissions,
  selected interpreter, extras, and port conflicts.
- Do not treat a zero exit status alone as a working listener.

#### Startup refuses the database version

- **Fix:** install a compatible release or restore the matching pre-upgrade
  backup.
- Do not edit the schema number.

#### Link will not start

- **Check:** the `web` extra, the effective participation setting, and a
  non-placeholder node name.

### Callers and accounts

#### Caller cannot log in

- **Check:** maintenance mode, pending approval, disabled account, and login
  throttling before resetting credentials.

#### Forgotten password or locked out

A caller forgot their password, or you are locked out:

- **Fix:** set a new password from the account's detail screen
  (<kbd>P</kbd> **Password**), or run
  `python -m netbbs.admin reset-password USERNAME` on the host.
- Nothing recovers the old one.

#### Caller can read but cannot contribute

- **Check:** write/join gates, age/name attestations, moderator grants, and
  inherited Community settings.

### Connections

#### SSH import fails on NetBSD

- **Check:** pkgsrc libraries and `LD_LIBRARY_PATH`; see
  [installation](#installing-netbbs).

#### SSH client cannot connect

For example, SyncTERM says "Error -20 activating session" or "Error -30
activating session".

- **"Error -30"** with "an excessive number of consecutive no-op packets"
  means the node is older than the fix for issue #964; upgrade it.
- **"Error -20":** the node offers an Ed25519 host key and, for clients
  without Ed25519 such as older SyncTERM builds, an RSA one
  (`netbbs_ssh_host_key_rsa`, created at the first start that lacks it). Make
  sure the node was restarted after upgrading so the RSA key exists. A caller
  who connected before sees a new fingerprint only if their client picks the
  RSA key.

#### SSH host key was readable by other accounts

The log says an SSH host key "was readable by other accounts". The key file
was created before NetBBS restricted it, or restored from a backup taken then.
NetBBS has already made it owner-only (0600).

- **On a host only you can log in to:** no action is needed.
- **If other people have accounts on the host,** assume they could have copied
  it:
  1. Tell callers first: their SSH clients will warn that the host key
     changed.
  2. Stop the node.
  3. Delete `netbbs_ssh_host_key` and `netbbs_ssh_host_key_rsa`.
  4. Start it again to get new keys.

#### Browser terminal or upload fails

- **Check:** HTTPS proxy/WebSocket forwarding, upload limits, the web
  listener, and `public_url`.

#### No file-transfer link

The terminal offers no file-transfer link.

- **Fix:** enable/configure the web listener and its public URL, or use a
  Zmodem-capable client.

#### Callers or peers cannot reach your address

Callers or peers cannot reach the address you expect.

- **Check:** compare
  **Settings ▸ Operating limits ▸ Node configuration** with your TOML file and
  the service's command line. That screen shows the configuration the node
  resolved at its last start, the addresses it advertises, and where each
  value came from.
- A listener whose optional extra is missing is listed there but was not
  started, and the service output says so.

### Content, Link and games

#### Find misses or lists the wrong content

**Find** misses content callers can open, or lists removed content.

- **Fix:** open **Operations ▸ Search indexes**, which checks them, then press
  <kbd>R</kbd> **Rebuild** if it reports drift.

#### Peers connect but content is missing

- **Check:** **Link status ▸ Offered** and **Link status ▸ Excluded**, trust
  state, **Outbox**, and **Diagnostics**.
- Use **Repair carried posts** only for local materialization repair.

#### Game is busy, fails, or loses state

- **Check:** its session limit, <kbd>C</kbd> **Compatibility** setup,
  <kbd>L</kbd> **Last diagnostic**, service state, and actual persistent
  paths.

### Keys and backups

#### A node key may have leaked

You think `signing.identity` or `transport.identity` leaked.

- **Fix:** replace that key as **Compromised** under **Link status ▸ Keys**,
  or with `python -m netbbs.admin rotate-key signing --compromised` while the
  node is stopped. The address and reputation stay; see
  [Node keys](#node-keys).

> [!CAUTION]
> If `root.identity` leaked too, as it does with a whole copied identity
> directory or backup, rotation is no remedy: the holder can authorize keys
> of their own, and only a new node identity ends that.

#### Voidrunner: NOT CAPTURED

Backup says `Voidrunner: NOT CAPTURED`. The node has not started since
v7.4.1, so it has recorded no save directory and the CLI fell back to your
shell's home.

- **Fix:** start the node once, or rerun with explicit
  `--voidrunner-save-dir`.

#### Voidrunner: no saves at

Backup says `Voidrunner: no saves at ...`. The node named that directory
itself and there is nothing in it -- nobody has played. Nothing to do.

### Reading the logs

| Where | What |
| --- | --- |
| Linux | Start with `journalctl -u netbbs`. |
| NetBSD | The example service's capture file is `netbbs.service.log` in the state directory; it catches failures before the application's rotating log opens. That service capture is not self-rotating, so arrange rotation or an appropriate output policy yourself. |
| `netbbs.log` | The application's log rotates at 10 MiB with five retained backups (up to about 60 MiB including the active file). |

**Operations ▸ Error log** reads that file from inside NetBBS, including from
`python -m netbbs.admin` while the node is stopped.

- That console runs outside the node, so its health panel has no live
  controls; it says whether the node is running.
- It shows the newest 512 KiB, topped up from `netbbs.log.1` after a
  rotation, and says when older lines exist that it does not show.
- It cannot show failures from before the log opened; for those, use the
  service manager's output above.

Some lines are routine and need nothing from you:

- A caller who hangs up is one `INFO` line naming their address.
- Every node yours meets on NetBBS Link starts on probation here, and the log
  says once per node, since the node started, that its content is held back.
- Your node is on probation at each peer in the same way, and the log says
  once per peer when one does not take what yours sends yet.
- A relay candidate that cannot be reached is also mentioned once.

A `WARNING` or `ERROR` line, and any traceback, is worth reading.

Link **Diagnostics** is a bounded warning/error log, not a transcript of all
content. The administrative **Audit log** answers who changed a setting or
moderated an item. Inspect those before attempting filesystem repairs.

### Filing an issue

For unresolved problems, [file an issue](https://github.com/Thiesi/NetBBS/issues)
with the release, operating system, transport, steps, and relevant error text.

> [!WARNING]
> Remove passwords, private keys, tokens, personal data, and transfer URLs
> before you post.
