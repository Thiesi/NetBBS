# NetBBS

A modern, TCP/IP-native BBS package: not stuck at 80x24, not stuck on an
EOL operating system, and built around **NetBBS Link** — an ad-hoc mesh
network that lets independent NetBBS nodes discover each other, exchange
message boards and personal messages, and (later) real-time chat, without
requiring any central authority.

Primary deployment target: **NetBSD** (via pkgsrc). Expected to run on
other POSIX systems too.

## Status

**Phases 1 and 2 are complete.** A genuinely full-featured standalone
single-node BBS — no NetBBS Link connectivity yet (that's Phase 3):

- Keypair + password auth, three connectivity methods (Telnet, SSH,
  web/xterm.js), all hardened through a post-launch security audit
  (secure-by-default transports, cross-connection login throttling,
  terminal-rendering sanitization, bounded queues/uploads/allocations).
- Local message boards (paginated, content-addressed IDs, post editing)
  and file areas with real Zmodem upload/download.
- Real-time chat: channels with scrollback, private messages, `/nick`
  display aliases, `/away`, invite-only/hidden channels, and a full
  local moderation set (mute/ban/kick, moderated-board/area approval
  queues, permission grants).
- A user directory (vCard/finger-style lookup) and per-user
  preferences.
- A fullscreen WYSIWYG ANSI art editor (for welcome banners) and a
  nano-keybound prose editor (for post bodies/bios), both reachable as
  an opt-in per-user preference over any of the three transports.
- SysOp admin tooling: user/board/area/channel management, node
  management (who's online, disconnect a session, graceful shutdown),
  and reference-aware garbage collection for uploaded file storage.

See [`docs/NetBBS-design-doc.md`](docs/NetBBS-design-doc.md) for the
full architecture, rationale, and phased roadmap — Phase 3 (Link
connectivity & sync core) is next.

This is a second attempt at this project. The first attempt got a long way
(multi-user chat, file areas, message boards) but needed a significant
rewrite once mesh networking entered scope, because NetBBS Link wasn't
designed in from the start. This attempt builds NetBBS Link in as a
foundational principle from day one — see the design doc for the full
history and the lessons carried forward.

## Requirements

- Python 3.11+ (asyncio-based)
- [PyNaCl](https://pynacl.readthedocs.io/) for identity/cryptography
  (`security/py-nacl` in pkgsrc) — chosen specifically because it wraps
  libsodium (C) rather than pulling in a Rust toolchain, unlike
  `cryptography`'s recent pkgsrc versions.
- SQLite (bundled with Python's standard library)
- Optional, per transport — a node only needs to install what it
  actually enables:
  - `pip install -e ".[ssh]"` (`asyncssh`) for the SSH transport.
    **SSH is enabled by default** (see "Running a node" below), so
    most nodes will want this.
  - `pip install -e ".[web]"` (`aiohttp`) for the web/xterm.js
    transport.
  - A node that only wants Telnet needs neither extra.

## Project layout

```
netbbs/
├── docs/                 Design documentation (see below)
├── examples/             Sample assets a node can reuse directly, e.g.
│                         two placeholder ANSI welcome banners in
│                         different styles — see `examples/README.md`
├── src/netbbs/           Main package (modular, not monolithic — see
│                         design doc §3 for why)
│   ├── identity/         Keypair generation, storage, addressing (§5)
│   ├── storage/          SQLite connection + schema migrations (§3)
│   ├── auth/             Account creation, password + keypair login,
│   │                     central username validation (§5)
│   ├── permissions/      User-level gating plumbing (§13)
│   ├── moderation/       Local moderator/permission grants, the
│   │                     moderation action log, and the blocklist (§13)
│   ├── boards/           Local message boards + posts: content-
│   │                     addressed IDs (§7), post editing (edit = a new
│   │                     linked revision, never an in-place mutation),
│   │                     moderated-board approval, expiry/maintenance,
│   │                     categories, pinning, configurable sort order
│   ├── files/            Local file areas: content-addressed blob
│   │                     storage, real Zmodem-compatible upload/
│   │                     download, moderated-area approval, expiry,
│   │                     and reference-aware garbage collection for
│   │                     orphaned blobs
│   ├── chat/             Local real-time chat: channels (content-
│   │                     addressed IDs), an in-memory per-node
│   │                     broadcast hub, membership/invitations for
│   │                     invite-only channels, mute/ban/kick, presence,
│   │                     scrollback, per-session private-message
│   │                     delivery, `/nick` aliases
│   ├── rendering/        ANSI rendering framework (§4): 256-color/
│   │                     cursor helpers, text reflow, untrusted-input
│   │                     sanitization, and the screen-buffer/diff
│   │                     ("TUI") abstraction the fullscreen ANSI art
│   │                     editor and prose editor are both built on
│   │                     (`screen_buffer.py`, `prose_buffer.py`)
│   ├── net/              Telnet, SSH, and web/xterm.js transports,
│   │                     all implementing one transport-agnostic
│   │                     `Session` abstraction (`session.py`) so
│   │                     everything above this layer is transport-
│   │                     independent. Login/main-menu flow
│   │                     (`login_flow.py`), board/chat/file-area
│   │                     screens (`*_flow.py`), the SysOp admin menu
│   │                     (`admin_flow.py`), the fullscreen editors
│   │                     (`ansi_editor.py`/`prose_editor.py`),
│   │                     `picker.py` (shared paginated list selector),
│   │                     cross-connection login throttling
│   │                     (`throttle.py`), node/session management and
│   │                     graceful shutdown (`session_registry.py`/
│   │                     `shutdown.py`/`maintenance.py`), and the real
│   │                     Zmodem protocol implementation (`zmodem.py`)
│   ├── admin/            Standalone `python -m netbbs.admin` CLI for
│   │                     account/node maintenance without a running,
│   │                     network-facing node
│   ├── web/              Vendored xterm.js static assets served by
│   │                     `netbbs.net.web`
│   ├── directory.py      User directory / vCard-style finger lookups
│   ├── user_preferences.py  Generic per-user key-value preference store
│   │                     (fullscreen editor opt-in, chat timestamps,
│   │                     etc. are all built on this)
│   ├── config.py         Node-wide key-value settings (display
│   │                     timestamp format, upload size limits, etc.)
│   ├── __main__.py       Configuration-driven node entry point: builds a
│   │                     NodeConfig (net/nodeconfig.py), starts every
│   │                     enabled listener, and shuts down cleanly on
│   │                     SIGTERM/SIGINT
│   └── timeutil.py       Storage-format timestamps (utc_now_iso) and
│                         user-facing display formatting, kept separate
├── scripts/               Dev utilities for exercising features without
│                         a self-registration UI or full admin session —
│                         create/block/unblock test users, boards,
│                         channels, categories, file areas/files, and
│                         set node config values directly
├── tests/                Test suite (pytest; conftest.py speeds up
│                         Argon2id-heavy tests automatically)
├── pyproject.toml
└── README.md
```

`python -m netbbs.admin --db path/to/netbbs.db` runs the same SysOp
admin menu (`netbbs.net.admin_flow.admin_menu`) directly against a
node's database from the local controlling terminal, with no network
listener involved — useful for account/node maintenance when the node
isn't running, or you'd rather not open a real Telnet/SSH/web
connection just to fix a stuck account. `src/netbbs/net/local_cli.py`/
`local_terminal.py` are the `Session` implementation this is built on.

## Running a node

`python -m netbbs` is configuration-driven (design doc round 28), not a
positional `db_path` argument anymore. What listens where, and the
login-throttling policy protecting it, come from an optional TOML config
file plus CLI overrides (CLI wins):

```sh
python -m netbbs --config /etc/netbbs/netbbs.toml
# or, with no file at all, defaults + CLI flags only:
python -m netbbs --db netbbs.db --enable-telnet --telnet-host 127.0.0.1
```

Example `netbbs.toml`:

```toml
[database]
path = "/var/db/netbbs/netbbs.db"

[ssh]
enabled = true
host = "0.0.0.0"
port = 2222

[telnet]
enabled = false

[web]
enabled = false

[link]
# NetBBS Link (design doc §11/§12) -- experimental peer-to-peer
# federation, disabled by default. outgoing_only=true (the default)
# runs the listener so peers this node dials can reply, without
# claiming to be reachable from outside; a full peer needs
# advertised_host (and, if different from port, advertised_port) set.
enabled = false
host = "0.0.0.0"
port = 7862
outgoing_only = true
# advertised_host = "203.0.113.5"
# advertised_port = 7862
# seeds -- operator-configured bootstrap peers this node dials on its
# own (design doc §12, round 119); empty by default, meaning Link
# answers inbound traffic (if enabled) but never originates any.
seeds = []
# seeds = ["http://198.51.100.7:7862", "http://203.0.113.9:7862"]
sync_interval_seconds = 300

[throttle]
# All optional -- shown here with their built-in defaults.
max_attempts_per_connection = 3
per_source_capacity = 10
per_source_refill_per_minute = 5
per_username_capacity = 10
per_username_refill_per_minute = 5
global_capacity = 100
global_refill_per_minute = 60
max_concurrent_unauthenticated_sessions = 100
login_deadline_seconds = 120
unauthenticated_idle_timeout_seconds = 60
```

**Secure by default (issue #1):** SSH is the only transport enabled out
of the box. Telnet and the plain-HTTP web transport both default to
*disabled* — passwords are never exposed over plaintext by default —
and even when explicitly enabled (`--enable-telnet`, `[telnet] enabled =
true`, etc.) without an explicit `host`, they bind to `127.0.0.1` rather
than every interface. Enabling either one on a non-loopback address logs
a prominent warning on startup. The web transport has no built-in TLS —
put a TLS-terminating reverse proxy (nginx, relayd, etc.) in front of a
loopback-bound instance for HTTPS/WSS; see
`src/netbbs/net/nodeconfig.py`'s module docstring for why that's the
supported path instead of certificate handling built into `aiohttp`
directly.

**Cross-connection login throttling (issue #3):** per-source-address,
per-username, and node-wide budgets persist for the node's whole
lifetime — reconnecting doesn't reset them, unlike the still-present
per-connection 3-attempt limit. See `src/netbbs/net/throttle.py`.

**Graceful shutdown:** SIGTERM/SIGINT stop every listener and close the
database in an orderly `finally`, rather than however the OS happens to
tear down the process. For an rc.d-style NetBSD service, run this in the
foreground and let the service supervisor manage backgrounding/restart
— `netbbs` does not daemonize itself.

**Config validation:** an invalid config (bad port, empty host, no
transport enabled at all, an unreadable/malformed file) is reported as a
clear one-line error and a non-zero exit, not a raw traceback or a node
silently listening for nobody.

## Manually testing a node

Telnet is off by default (see "Secure by default" above) — enable it
explicitly for the simplest local testing loop:

```sh
python scripts/create_test_user.py netbbs.db thiesi hunter2 100
python scripts/create_test_board.py netbbs.db general "General discussion"
python scripts/create_test_channel.py netbbs.db lobby "General chat"
python scripts/create_test_file_area.py netbbs.db downloads "Downloads"
python scripts/set_node_config.py netbbs.db display_timezone Europe/Berlin
python -m netbbs --db netbbs.db --enable-telnet
```

Then, from another terminal: `telnet localhost 2323`. Port 2323, not
23 — binding 23 needs root. See `src/netbbs/net/nodeconfig.py` for why,
and for what a real deployment would need instead.

**SSH** is enabled by default (install the `ssh` extra first — see
Requirements above): `python -m netbbs --db netbbs.db` alone is enough
to bring up an SSH listener on `127.0.0.1:2222`. Connect with any
standard client, e.g. `ssh -p 2222 thiesi@127.0.0.1` (password) or
register a keypair account for public-key auth — either way, a
successfully SSH-authenticated connection goes straight to the main
menu, no second NetBBS-level password prompt.

For real-time chat specifically, open two separate sessions (two
terminals/clients, or one real connection plus you at the console
testing solo won't show the broadcast effect) and join the same
channel from both — messages sent from one should appear in the other
immediately.

To see terminal-width-aware reflow in action, resize your terminal
narrower (e.g. ~40 columns) *before* connecting — most clients report
their window size on connect (NAWS over Telnet, the PTY channel over
SSH), and post bodies should wrap to match. A client that doesn't
report a size falls back to an 80-column assumption.

**Line editing:** the server handles echo and editing itself
(character mode), not the client. Full cursor-addressable editing is
implemented — Left/Right/Home/End move within the line, Backspace/
Delete work at the cursor position (not just at the end), and Up/Down
recall previous lines per connection.

The main menu dispatches immediately on a single keystroke — no Enter
needed for `M`/`C`/`F`/`D`/`P`/`A`/`L`. Only the single letter works,
not a full word.

Your own chat messages now show in a distinct color (magenta) from
everyone else's (gold), so they stand out in the conversation.

**Board and channel selection** now uses a shared paginated picker
instead of typing exact names: browse with 2-digit numbers, `[S]earch`
by substring (auto-selects if there's a unique match), `[G]oto #` to
jump straight to a stable absolute index shown as `(#N)` next to every
item — that number stays valid regardless of paging or an active search
filter.

**Categories, pinning, and sort order:** boards and channels can now be
organized into categories (at most two levels — a category and,
optionally, sub-categories under it), pinned to always sort first, and
sort by activity (default), alphabetically, or by post volume (boards
only). Try it:

```sh
python scripts/create_test_category.py netbbs.db board "Vintage Computing"
python scripts/create_test_category.py netbbs.db board "Commodore" "Vintage Computing"
python scripts/create_test_board.py netbbs.db c64 "Commodore 64 talk" Commodore
python scripts/create_test_board.py netbbs.db announcements "" "" yes
```

Browsing boards should now show "Vintage Computing" as a category to
drill into (revealing "Commodore" as a sub-category, then `c64` inside
that), while `announcements` (pinned, uncategorized) appears at the top
level, ahead of anything else.

To test the blocklist:

```sh
python scripts/block_user.py netbbs.db thiesi "testing the blocklist"
```

Then try logging in as `thiesi` — you should see "Your access to this
system has been revoked." instead of reaching the main menu. Reverse with
`python scripts/unblock_user.py netbbs.db thiesi`.

**The SysOp admin menu** (`[A]dmin` from the main menu, for any account
at or above the SysOp level) covers user/board/area/channel/category
management, moderator permission grants, node management (who's
online, disconnect a session, trigger a graceful shutdown), and
file-storage garbage collection. `python -m netbbs.admin --db
netbbs.db` reaches the same menu without a network connection at all.

**The fullscreen editors** are opt-in per account: from `[P]rofile`,
toggle "Fullscreen editor" on, then composing a board post or editing
your bio opens the nano-keybound prose editor (Ctrl+O save, Ctrl+X
quit) instead of the plain line prompt. A welcome-banner WYSIWYG ANSI
art editor is reachable from `[A]dmin` → `[S]ystem` → `[W]elcome
banner` → `[X] edit`; see `examples/README.md` for two ready-made
placeholder banners to drop in and try it against instead of starting
from a blank canvas.

**File transfer** uses real Zmodem — `/upload`/`/download` inside a
file area work with any Zmodem-capable terminal (SyncTERM, `lrzsz`'s
`rz`/`sz`, etc.), not just NetBBS-aware clients.

## Development

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest
```

To enable the repo's pre-commit hook (blocks commits containing
unresolved git merge conflict markers — see `.githooks/pre-commit`):

```sh
git config core.hooksPath .githooks
```
