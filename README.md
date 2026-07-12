# NetBBS

A modern, TCP/IP-native BBS package: not stuck at 80x24, not stuck on an
EOL operating system, and built around **the Link** — an ad-hoc mesh
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
rewrite once mesh networking entered scope, because the Link wasn't
designed in from the start. This attempt builds the Link in as a
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
│   ├── __main__.py       Minimal runnable entry point for manual testing
│   └── timeutil.py       Shared deterministic timestamp formatting
├── scripts/
│   └── create_test_user.py  Dev utility: create an account to test the
│                             login flow with (no self-registration UI
│                             exists yet)
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
python -m netbbs netbbs.db
```

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

## License

TBD.
