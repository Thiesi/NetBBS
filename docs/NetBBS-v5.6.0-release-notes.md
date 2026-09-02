# NetBBS v5.6.0

Two roadmap issues shipped end to end: a managed `netbbs.org` subdomain with
dynamic DNS (issue #201), and trusted scrollback-on-join for live Link
channel chat (issue #194). Alongside them: a bare `pip install netbbs`
startup crash fixed, and the long-standing `login_flow.py` monolith split
into six focused modules. No database migration for a node; the real-time
Link chat protocol moves to version 2 (see Upgrade notes).

## Managed netbbs.org subdomain + dynamic DNS (issue #201)

A SysOp can now give their node a public `<name>.netbbs.org` hostname
without owning a domain, running a DNS server, or touching a registrar —
and, optionally, have that record follow the node's address as it changes.
Every decision in design doc §16 for this issue is implemented; the closing
paragraph there records the shipped parameters.

**What a SysOp sees.** At first-SysOp bootstrap (and, as a fallback, on the
first authenticated login while the node is still undecided) a plain-English
opt-in explains what registering means. It is off unless accepted; declining
records the decision so the prompt never repeats. Accepting proceeds straight
into naming the subdomain, choosing whether the record should track address
changes, confirming whether the web listener sits behind an HTTPS-terminating
proxy on the standard ports (informational only — never enforced remotely),
and registering — no second trip through the admin menu. A new `[D]NS`
quick action on the SysOp console shows the decision, the registered name,
its status, and the last successful contact, with `[R]egister` when nothing
is active and `Re[l]ease` when something is. The typed name is sanitized
before it is echoed, and every remote error message is sanitized before it
is styled.

**How it works.** Two independently deployed components share this
repository, the same way the website already does: `src/netbbs/managed_dns/`
is the node-side client shipped in every install; `services/managed_dns/` is
the project-operated backend, kept outside `src/` so it is never packaged
into a node. Registration mints a separate per-registration bearer secret —
never the node's Ed25519 key — returned once and stored server-side only as
a hash; the node keeps it on disk owner-only (`0600`, created exclusively,
atomically replaced) and it now joins the backup manifest as the thirteenth
artifact, rebased onto the restore target's database stem on restore.

A registration matures after 24 hours of continuous contact (a contact
window that resets only after a gap beyond the 7-day abandonment
threshold), publishes exactly once at that transition, and — for dynamic
registrations — republishes whenever the observed source address changes,
replacing a stale record of the other address family. The node sends a
heartbeat every 15 minutes from its own background task, starts on the very
next pass after a later registration without a restart, and stops after a
voluntary release. Release deletes the published record before the row is
finalized (a provider failure keeps it retryable). Reclaim reuses
`/register` with the old credential during the shared 90-day cooldown,
reactivating the original row and its earned contact time rather than
minting a new one, and honours the dynamic choice given at reclaim. Names
are validated as RFC 1035 labels, case-insensitively unique, with a narrow
reserved-word blocklist; one active name per node.

**Bounds.** Service-wide (not per-identity, the one thing a Sybil attacker
can't multiply): a rate limit of five new registrations per hour that
survives a service restart, and a cumulative cap of 1,000 active
registrations. Both hard-reject with a plain message; the originally-locked
human-review queue was dropped during implementation because the realistic
resolution already requires manual contact regardless. Every DNS-provider
call runs off the request event loop under an explicit in-flight limit; the
hourly sweep releases the transition lock between rows so a slow authoritative
server cannot stall heartbeats for hours; all four endpoints return the
documented JSON 400 for non-object payloads; the server stops on every exit
path, preserving the initiating error over a cleanup failure.

**Backend.** `python -m services.managed_dns`, configured entirely by
environment variables, with a real RFC 2136 (TSIG-signed dynamic update)
provider for BIND and an in-memory provider for tests. Deletions are limited
to A/AAAA records. `services/managed_dns/README.md` is the operations
runbook: key generation, the `allow-update` ACL, every variable, and a manual
end-to-end checklist. `X-Forwarded-For` is ignored unless the operator
explicitly declares a trusted reverse proxy.

## Trusted scrollback-on-join for live Link channel chat (issue #194)

Joining a linked channel over the live (Noise XX) transport used to show
only what arrived after you joined; anything said in the minutes before was
invisible until the asynchronous catch-up sync ran. A freshly subscribing
peer now receives a bounded, ephemeral scrollback snapshot alongside the
existing presence snapshot — the origin's own recent scrollback, already
trust-filtered by the issue #164 rules — rendered exactly once under the
same `HISTORY` heading the local join-time replay uses. It is never stored;
the async sync remains the durable mechanism.

Hardened through three review rounds before release:

- **The subscriber enforces its own trust policy.** Each entry carries the
  attested author node fingerprint and user ID; the receiving node applies
  its own blocked/quarantined decisions before rendering, and reconstructs
  the displayed `user@node` label from those attested fields rather than
  trusting the wire label — so a remote `alice` can never be styled as the
  local Alice.
- **Snapshots bind to the channel's current origin and to the specific
  subscribe attempt** that requested them; a late or unsolicited snapshot
  from any other peer is discarded rather than pre-populating the next
  join.
- **Moderation rows are authorless system events**, so blocking the target
  of a kick or ban never hides the audit line.
- **Already-materialized history is not shown twice**; bodies over 400
  bytes carry a visible truncation marker; the complete serialized frame is
  size-checked before it is queued, so a worst-case snapshot can never close
  the whole session from the writer loop.
- **Protocol validation** rejects a non-string `kind`, a missing body on a
  body-bearing kind, and malformed entries through the bounded
  protocol-strike path instead of tearing the session down.

## Real-time Link protocol version 2

The snapshot's attribution contract above is not something a version 1 peer
can interpret safely, so the real-time application protocol is now version
2, advertised inside the authenticated Noise identity payload. A mixed pair
fails at the handshake — before the session is ever reported live — and the
caller sees a specific message ("This channel's origin uses an incompatible
real-time protocol version — upgrade one of the nodes; synchronized messages
will still arrive.") instead of a generic transient-failure notice. A
legacy identity payload without a version field is recognized as version 1;
an unauthenticated responder at a stale address cannot trigger that notice
or block the origin's remaining addresses. Per-frame version checks remain
as defence in depth.

## Bare `pip install netbbs` startup crash (issue #245)

A no-extras install crashed at `python -m netbbs --version` with
`ModuleNotFoundError: No module named 'aiohttp'`: `netbbs.net.chat_flow` —
imported unconditionally on every node — pulled `LiveChannelBridge` in at
module top level, which reaches the `aiohttp`-backed transport. The class
was only ever used as a type annotation; it now lives behind
`TYPE_CHECKING`. Reproduced by building a wheel and installing it into a
clean venv with zero extras. Every later change this cycle that needs
`aiohttp` (managed-DNS client, updater task) follows the same lazy-import
convention and was verified with `aiohttp` blocked from `sys.path`.

## `login_flow.py` split into six modules

The 4,912-line `login_flow.py` is now 1,254 lines of pure session-entry and
authentication logic. Board browsing/posting moved to `board_flow.py`,
`[N]ew scan` and `[F]ind` to `scan_and_find.py`, the user directory and
`[W]ho's online` to `directory_flow.py`, profile/identity editing and
session history to `profile_flow.py`, and the main menu, the direct-chat
invite race, and the shared resource-type sub-menu to `main_menu.py`. Purely
structural — no behavior change — but the mechanical moves caught two real
issues (a plain `import hashlib` the pruning script missed, and a circular
import avoided by moving `_show_pending_invitations` with its only caller),
and a repo-wide sweep fixed every stale `netbbs.net.login_flow.*` reference
in source, tests, and docs.

## Smaller fixes

- The System menu's trust screen title now reads "Policy trust", matching
  its `[P]olicy trust` menu entry.
- `.codex/` and `.test-tmp/` (agent-local worktrees and sandbox scratch)
  are ignored by git and by the codebase-memory indexer.
- `pyproject.toml`'s pytest `pythonpath` now includes the repo root, so
  `services.managed_dns`'s own tests run from a bare `pytest tests/`.

## Upgrade notes

- **No node database migration.** Schema version stays at 58. Managed-DNS
  state lives in the existing `config` table and a per-node credential
  file beside the database.
- **Real-time Link chat requires both nodes on v5.6.0 or later.** A v5.6.0
  node and an older node can still exchange everything through the
  asynchronous Link sync — boards, mail, files, and channel messages all
  arrive — but a *live* channel subscription between them fails at the
  handshake with the caller-visible message above. Upgrade linked nodes
  that share live channels together.
- **Managed DNS is opt-in and needs the `web` extra** (`aiohttp`) on the
  node; a Telnet/SSH-only install without it sees a recoverable
  dependency message if it accepts the prompt, not a crash. The prompt
  fires once for existing nodes on the next SysOp login.
- **Backups now include the managed-DNS credential** as a thirteenth
  artifact when one exists; restore handles it automatically.
- **Backend operators:** `services/managed_dns` needs `aiohttp` and
  `dnspython` (`services/managed_dns/requirements.txt`) and its own
  SQLite store, which migrates itself (schema version 3) on start.

## Validation

- The complete pytest suite passes (4827 passed, 7 skipped) at this
  release's exact commit.
- `python -m netbbs --version` reports v5.6.0 and schema version 58.
- Managed DNS was smoke-tested as a real running backend process (env
  config, HTTP serving, a live `POST /register` round trip) and against
  real loopback `aiohttp` round trips in tests — never mocked HTTP.
  Scrollback-on-join is tested over real loopback Noise sessions. Both
  went through multiple Codex review rounds (PRs #255, #262, #263), each
  finding fixed with a regression test.
