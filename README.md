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
<<<<<<< HEAD
│   ├── identity/         Keypair generation, storage, addressing (§5)
│   ├── storage/          SQLite connection + schema migrations (§3)
│   ├── auth/             Account creation, password + keypair login (§5)
│   ├── permissions/      User-level gating plumbing (§13)
│   └── timeutil.py       Shared deterministic timestamp formatting
├── tests/                Test suite (pytest; conftest.py speeds up
│                         Argon2id-heavy tests automatically)
=======
│   └── identity/         Keypair generation, storage, addressing (§5)
├── tests/                Test suite
>>>>>>> 990e1ebcd3991edd8236e769d1f86bb3a15d2bb9
├── pyproject.toml
└── README.md
```

As phases progress, expect new top-level modules under `src/netbbs/`
roughly mirroring the design doc's sections: `transport/`, `link/`
<<<<<<< HEAD
(DAG/gossip/sync), `boards/`, `areas/` (files), `chat/`, etc. Each stays a
separate, testable module — see design doc §3 for the reasoning against a
single monolithic script.
=======
(DAG/gossip/sync), `boards/`, `areas/` (files), `chat/`, `permissions/`,
etc. Each stays a separate, testable module — see design doc §3 for the
reasoning against a single monolithic script.
>>>>>>> 990e1ebcd3991edd8236e769d1f86bb3a15d2bb9

## Development

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## License

TBD.
