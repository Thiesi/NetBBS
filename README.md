# NetBBS

A modern, TCP/IP-native BBS package: not stuck at 80x24, not stuck on an
EOL operating system, and built around **NetBBS Link** — an ad-hoc mesh
network that lets independent NetBBS nodes discover each other, exchange
message boards and personal messages, and (later) real-time chat, without
requiring any central authority.

Primary deployment target: **NetBSD** (via pkgsrc). Expected to run on
other POSIX systems too.

## Status

Design is complete and signed off — see [`docs/NetBBS-design-doc.md`](docs/NetBBS-design-doc.md)
for the full architecture, rationale, and phased roadmap. Implementation
is in **Phase 1 (Foundation)**.

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

## Project layout

```
netbbs/
├── docs/                 Design documentation
├── src/netbbs/           Main package (modular, not monolithic — see
│                         design doc §3 for why)
│   ├── identity/         Keypair generation, storage, addressing (§5)
│   ├── storage/          SQLite connection + schema migrations (§3)
│   ├── auth/             Account creation, password + keypair login (§5)
│   ├── permissions/      User-level gating plumbing (§13)
│   ├── net/              Telnet transport + Session abstraction, login
│   │                     flow (SSH/web to follow on the same Session
│   │                     abstraction)
│   ├── boards/           Local message boards + posts, content-addressed
│   │                     IDs from day one (§7) so Linked-board support
│   │                     later needs no ID-scheme migration
│   ├── chat/             Local real-time chat: channels (content-
│   │                     addressed IDs, same reasoning as boards) + an
│   │                     in-memory per-node broadcast hub
│   ├── moderation/       Local blocklist (moderation stub, pre-dates the
│   │                     full reputation system)
│   ├── config.py         Node-wide key-value settings (currently just
│   │                     display timestamp format)
│   ├── __main__.py       Minimal runnable entry point for manual testing
│   └── timeutil.py       Storage-format timestamps (utc_now_iso) and
│                         user-facing display formatting, kept separate
├── scripts/
│   ├── create_test_user.py    Dev utility: create an account to test the
│   │                          login flow with (no self-registration UI
│   │                          exists yet)
│   ├── create_test_board.py   Dev utility: create a board (+ seed post)
│   │                          to test board browsing with
│   ├── create_test_channel.py Dev utility: create a chat channel to
│   │                          test real-time chat with
│   ├── block_user.py          Dev/admin utility: block a user from
│   │                          logging in
│   ├── unblock_user.py        Dev/admin utility: remove a user from the
│   │                          blocklist
│   └── set_node_config.py     Dev/admin utility: set a node-wide config
│                               value, e.g. the display timestamp format
├── tests/                Test suite (pytest; conftest.py speeds up
│                         Argon2id-heavy tests automatically)
├── pyproject.toml
└── README.md
```

As phases progress, expect new top-level modules under `src/netbbs/`
roughly mirroring the design doc's sections: `transport/`, `link/`
(DAG/gossip/sync), `boards/`, `areas/` (files), `chat/`, etc. Each stays a
separate, testable module — see design doc §3 for the reasoning against a
single monolithic script.

## Manually testing the Telnet connection

```sh
python scripts/create_test_user.py netbbs.db thiesi hunter2 100
python scripts/create_test_board.py netbbs.db general "General discussion"
python scripts/create_test_channel.py netbbs.db lobby "General chat"
python scripts/set_node_config.py netbbs.db display_timezone Europe/Berlin
python -m netbbs netbbs.db
```

For real-time chat specifically, open two separate `telnet localhost
2323` sessions (two terminals, or one real connection plus you at the
console testing solo won't show the broadcast effect) and join the same
channel from both — messages sent from one should appear in the other
immediately.

To test the blocklist:

```sh
python scripts/block_user.py netbbs.db thiesi "testing the blocklist"
```

Then try logging in as `thiesi` — you should see "Your access to this
system has been revoked." instead of reaching the main menu. Reverse with
`python scripts/unblock_user.py netbbs.db thiesi`.

Then, from another terminal:

```sh
telnet localhost 2323
```

Port 2323, not 23 — binding 23 needs root. See `src/netbbs/__main__.py`
for why, and for what a real deployment would need instead.

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
