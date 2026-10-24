# NetBBS design — §13. Runtime, persistence, and operations

Part of the [NetBBS design](../NetBBS-design-doc.md). Section numbers are stable: cite this area's rules by their section numbers (§13.n).

## 13. Runtime, persistence, and operations

### 13.1 Database execution model

Interactive and background Link work use separate single-worker database lanes,
each with its own SQLite connection and bounded submission depth.

This isolates human-paced foreground work from sustained background federation
traffic while preserving simple synchronous domain functions.

SQLite retains its normal single-writer behavior. `busy_timeout` handles short
cross-lane contention; no application-wide write mutex is introduced.

A cancelled awaiting coroutine does not abort an already-running worker-thread
operation. The database operation completes or rolls back even when the caller
no longer receives the result. Callers must account for that semantic when
performing follow-up state changes.

Shared live `LinkNode` projections are event-loop-owned. A lane-dispatched
function may build and persist events but must not mutate live Link state from a
worker thread.

### 13.2 Atomic invariants

A read-check-write invariant across connections requires one explicit write
transaction:

- begin the write transaction before reading;
- re-fetch current state;
- evaluate safety and no-op conditions from fresh rows;
- write the mutation and audit record atomically;
- roll back on every failure.

The last-usable-SysOp guard is the reference pattern.

### 13.3 Migrations

Migrations are append-only. Never edit a migration which may already have
shipped.

SQLite table rebuilds are dangerous when the rebuilt table is a foreign-key
parent: dropping it can trigger cascade or `SET NULL` actions before the
replacement exists. Prefer `ALTER TABLE ADD COLUMN`, indexes, and explicit
cleanup over rebuilds. When a rebuild is unavoidable, test it against realistic
related rows and the actual dependency graph.

A database from a newer build must fail startup clearly. A matching
`user_version` cannot prove an operator has not manually changed old schema;
manual schema mutation is unsupported unless a future schema fingerprint is
introduced.

### 13.4 Backup and restore (issue #60's first operational slice)

War Dialer coverage (issue #362): node backups also include existing configured
shared worlds as checksummed SQLite snapshots paired with the node's user-ID
namespace. Restore requires an explicit destination for every world and excludes
active game sessions. The world ownership, limits and rollback contract is recorded
in the War Dialer storage decision in ?16; manual activation is in the door guide.

Voidrunner coverage (issue #310): ordinary CLI and SysOp node backups include the
node's Voidrunner save directory (`<db>.doors/voidrunner/`, or the SysOp's
`VOIDRUNNER_SAVE_DIR`), when present, under a checksummed `voidrunner/` component.

How that directory is chosen is itself part of the contract (issue #555), because
the door and the backup CLI are different processes and need not share a `HOME`:
`examples/netbbs.rc` starts a node with `HOME` set to the state directory, while
an operator runs the backup from their own shell. The node therefore records the
directory it would hand a door, once its listeners are bound, and the CLI reads
that. Three provenances exist and are reported differently, because they carry
different amounts of confidence: an **operator-supplied** `--voidrunner-save-dir`
wins over everything; a **node-recorded** path is authoritative, so finding it
empty is a fact about the node; an unrecorded node whose `<db>.doors/voidrunner/`
exists is the same fact, derived from the database path rather than read; and an
**unrecorded** lookup with neither falls back to the calling process's own home
and is a guess, reported as one whether or not that directory happens to exist. A guess that finds files is the dangerous case -- an
operator who once ran a node from their shell has exactly that directory, holding
exactly the wrong careers -- so it is captured but never presented as a finding.
The chosen directory and its provenance are written into the manifest as captured,
not re-derived afterwards: a backup is live-safe, so the node may start midway and
a later lookup can describe a directory the archive never inspected. Capture preserves career, previous,
recovery and score JSON bytes, including damaged careers needed for repair;
temporary files and OS lock files are excluded. A bounded maintenance lease
prevents new game launches and refuses capture while an existing pilot is active.
The gate lives inside the save directory. Restore preserves that directory and
its lock inodes, switching its data entries under the lease; existing provisioned
save directories require no parent write permission for normal play or capture.
The BBS may keep running, but Voidrunner sessions must be closed. Capture precedes
the database snapshot so a newly registered user's captured career cannot refer
to a user ID newer than that snapshot. This is an offline game-data snapshot,
not a guarantee that every node artifact was written at one global instant.

Restoring a backup containing this component requires an explicit
`--voidrunner-to` destination; archive metadata never chooses a live path. The
operator must use that directory for the restored service. The component is
staged and rolled back beside its destination, allowing a different filesystem
from the database. Node and game switches share one recovery journal; failures
roll back both, and any retained external rollback location is recorded beside
the ordinary rollback generation. Journal updates use flushed atomic replacement;
a failed post-switch update also triggers rollback, retaining the last usable
journal if recovery fails. Existing backups without Voidrunner leave
external careers alone. Cross-host shared directories and simultaneously running
different game builds remain unsupported. Limits are 10,000 captured files,
4 MiB per file and 512 MiB total; unsupported entries or exceeded limits fail
clearly rather than silently producing incomplete coverage.

Door outbound receipt coverage (issue #556): a door's result receipts under
`db_path.parent / "door-outbound"` are node state and are captured as a
checksummed component with the rest of it. They were beside the database from
the start so that a backup could carry them, which is not the same as a backup
carrying them; before this component a restored node came back holding a door's
posts and none of their outcomes. Capture precedes the database snapshot, in the
direction that matters: a `"posted"` receipt is written only after its post is
committed, so every receipt in an archive names a post that archive's snapshot
contains. The reverse pairing — a post whose receipt is missing — remains
possible and is what the door contract already describes; a receipt naming a
post the restored database never issued is the one a door could act on, and this
ordering rules it out for every post the node still had. A post the node itself
deleted is the exception and is deliberately preserved: deleting a board removes
its posts and leaves the receipts, so the running node already holds that pair,
and an archive that quietly dropped those receipts would restore a node tidier
than the one it was taken from. Capture is not a validation pass over a door's
history. Only what NetBBS itself wrote is captured (a
regular file named as `_write_result` names one, within its size), bounded by
the door module's own retention rule per door and by 64 door directories
overall; anything else found there is left in place and counted in the manifest
rather than captured or called corruption. Restore replaces the live receipts in
whole with the archive's own — including replacing them with nothing, when the
archive predates this component or the node had none — because receipts from a
later generation standing beside an older database claim post IDs it never
issued. The previous generation goes to the ordinary rollback directory like
every other replaced artifact. None of this makes receipt retention a
publication ledger: it restores the same bounded, best-effort record a running
node keeps, and a door still must not infer exactly-once publication from it.

A node's recoverable state includes the following artifacts at derived,
`db_path`-relative paths, captured together by the node backup tool:

| Artifact | Location | Written by |
|---|---|---|
| Database | `db_path` | every domain write |
| Content blobs | `db_path.parent / f"{db_path.stem}_files"` (git-style `xx/xxxx...` sharding; excludes its own `.incoming/` staging subdirectory, which is always crash-orphan garbage — see `purge_incoming_staging`) | `netbbs.files.storage` |
| Node identity | `identity_dir` (`root.identity`, `signing.identity`, `transport.identity`, `transitions.json`) | `netbbs.link.node_identity` |
| SSH host key | `db_path.parent / f"{db_path.stem}_ssh_host_key"` | `netbbs.net.ssh.ensure_host_key`, once, at first startup. Both host keys are created owner-only (0600); a start that finds one readable by group or others restricts it to 0600 and logs a warning (issue #976) |
| SSH RSA host key | `db_path.parent / f"{db_path.stem}_ssh_host_key_rsa"` | `netbbs.net.ssh.ensure_rsa_host_key`, once, at the first startup that lacks it (issue #964). 3072 bits, offered as `rsa-sha2-512` and `rsa-sha2-256` only, never SHA-1 `ssh-rsa`, after Ed25519. It exists for clients without Ed25519 host keys, such as SyncTERM's Cryptlib-based builds |
| Managed-DNS credential | `db_path.parent / f"{db_path.stem}_managed_dns_credential"` | `netbbs.managed_dns.credential`, once, at registration (§16 Decision 7, issue #201) |
| Managed-DNS rename credentials | Previous credential plus the temporary credential-transition journal beside `db_path`; restore preserves the presence and absence of the primary, previous, and journal artifacts | `netbbs.managed_dns.credential`, during a managed-name transition |
| Welcome banner | `db_path.parent / f"{db_path.stem}_welcome_banner.ans"` | SysOp, via the welcome-banner menu screen |
| Main-menu masthead | `db_path.parent / f"{db_path.stem}_main_menu_banner.ans"` | SysOp, via the masthead menu screen (issue #161) |
| Logoff banner | `db_path.parent / f"{db_path.stem}_logoff_banner.ans"` | SysOp, via the logoff-banner menu screen (issue #177) |
| New-account banner (before signup) | `db_path.parent / f"{db_path.stem}_new_account_banner_before.ans"` | SysOp, via its own menu screen (issue #177) |
| New-account banner (after signup) | `db_path.parent / f"{db_path.stem}_new_account_banner_after.ans"` | SysOp, via its own menu screen (issue #177) |
| Board list masthead | `db_path.parent / f"{db_path.stem}_board_list_banner.ans"` | SysOp, via its own menu screen (issue #176) |
| File area masthead | `db_path.parent / f"{db_path.stem}_file_area_banner.ans"` | SysOp, via its own menu screen (issue #176) |
| Chat channel picker masthead | `db_path.parent / f"{db_path.stem}_chat_channel_picker_banner.ans"` | SysOp, via its own menu screen (issue #176) |
| Door outbound receipts | `db_path.parent / "door-outbound"` (one directory per door, bounded by that door's own retention) | `netbbs.doors.outbound`, once per answered request (issue #556) |
| Bot API state | `<database-filename>.bots/<original-instance-name>/state.json` | `netbbs.bots.state`; bounded atomic JSON captured before the database snapshot, restored as a whole generation including absence (issue #1210) |

A backup covering only the database silently loses the SSH host key (every
client gets a MITM warning on next connect after restore) and, far more
seriously, the Link node identity (root-key custody is explicitly "part of
ordinary node backup and restore" per §4.5's node identity model, not a
separate ceremony) — so this design treats these as one complete backup
operation, never a DB-only one.

**Mechanism**: a new `netbbs.backup` module (synchronous, path-based — no
`Database` wrapper needed, since a backup must be safely takeable against a
*live, running* node, not only an offline one) with two entry points, plus a
`python -m netbbs.backup {create,restore}` CLI in the same spirit as
`python -m netbbs.admin`. `create_backup` is also exposed through the live
SysOp `[K] Backup` screen: it uses the running node's effective database and
identity paths, chooses a fresh timestamped directory under the backup
destination -- `db_path.parent / f"{db_path.stem}_backups"` unless the SysOp
configured another directory (issue #727) -- requires confirmation, and runs
the blocking snapshot/copy work off the asyncio event loop. The CLI remains
the path-selectable, scriptable entry point. Restore remains CLI-only and
offline because it replaces the node's state.

**Scheduled backups (issue #727).** The node itself can run backups on a
schedule the SysOp sets on the Backup screen: off (the default), daily, or
weekly on one weekday, at a wall-clock time in the node's display timezone.
This is a deliberate exception to "maintenance runs only when a SysOp asks"
(the repair and GC screens), for the same reason the release check is one: a
backup nobody remembers to take is the failure it exists to prevent, and the
alternative was a cron job every SysOp had to write and keep beside the node.
The rules:

- A *slot* is one scheduled moment, handled at most once whatever its
  outcome. A failed or skipped slot (an active War Dialer world, a missing
  identity directory, a destination that has gone) is recorded in the backup
  history and not retried; the next slot is the retry.
- A node that was down across one or more slots makes exactly one catch-up
  backup when it next runs. Saving a changed schedule counts from that
  moment, so switching one on never fires for a slot already in the past.
- Retention keeps the newest N (default 7) of the schedule's *own* backups.
  Each is recorded in `scheduled_backups` when it succeeds, and only recorded
  directories that still hold a manifest are ever deleted; a manual backup,
  or anything else in the destination, is never touched. A failed deletion is
  reported and retried on the next pass.
- The destination, when set, is an existing absolute directory the node can
  write to, outside the file storage and identity trees a backup copies. It
  is never created: a destination that has disappeared (an unmounted disk)
  fails the backup rather than filling the disk beneath it.
- Scheduled runs follow the same Door installations toggle as manual ones.
- A pass runs in a worker thread with its own database handle. Shutdown does
  not cut a running backup off: the task's cancellation leaves the worker to
  finish (and logs its outcome), so the process exits once the backup is
  complete rather than leaving a half-written directory.
- The standalone `python -m netbbs.admin` edits the schedule but does not
  run it.

Copying backups off the machine, and encrypting them, remain the SysOp's.

`create_backup(*, db_path, identity_dir, destination)`:

1. **Database and managed-DNS credential generation**: reuses
   `netbbs.selfupdate.snapshot_database` verbatim
   (`sqlite3.Connection.backup()`, already proven safe against a live WAL
   database in `test_snapshot_and_restore_database_round_trip`) — written to
   `destination/<configured-database-filename>` and recorded in the manifest
   (older backups without that field retain the legacy `netbbs.db` name).
   Never a raw file copy. The primary, previous, and
   transition-journal credentials are double-collected around that snapshot,
   and the snapshot's `managed_dns_*` configuration is compared with the live
   database afterward. Any detected overlap with a credential/name transition
   retries the bounded collection, so a backup cannot pair one transition generation's
   database with another generation's bearer secrets.
2. **Content blobs**: `shutil.copytree` of the blob root into
   `destination/files/`, `.incoming/` excluded. Must run strictly *after*
   step 1, not before or concurrently — this is what makes the DB-then-
   blobs ordering below actually safe, not just a stated convention:
   `netbbs.files.entries`'s own invariant is that a `files` row is only ever
   created after its bytes are already durably written to storage, never
   the other way around. So every blob a given DB snapshot's rows could
   possibly reference was already on disk before that snapshot was even
   taken — copying blobs afterward is guaranteed to include all of them,
   plus possibly a few newer, still-unreferenced ones from uploads that
   landed in between (harmless — an orphaned blob a future GC pass could
   still reclaim, never a dangling reference). Reversing the order would
   risk the opposite, genuinely broken case: a DB snapshot referencing a
   blob the copy hadn't reached yet.
3. **Node identity, SSH host key, and every banner/masthead singleton**
   (welcome banner, main-menu masthead, logoff banner, both new-account
   banners): plain file copies (each is either static after creation or
   already rewritten via its own atomic-replace pattern — `node_identity.
   py`'s `transitions.json`, notably — so no read-tearing hazard). Every
   banner/masthead singleton is the accepted exception, with no atomicity
   guarantee on its own writes; a backup landing mid-edit could capture a
   half-written file. Accepted as-is: purely cosmetic, no correctness
   consequence, not worth an atomic-write retrofit just for backup's sake.

Writes `destination/manifest.json` last (timestamp, `netbbs.__version__`,
the database's own `PRAGMA user_version`, and a checksum per captured
artifact outside the content-addressed blob tree) — lets an operator (or a
future restore-time check) confirm what a
given backup directory actually is before trusting it. Also records
`last_backup_at`/`last_backup_path` into the live node's own `node_config`
table (same key-value store `netbbs.selfupdate`'s update-check state already
uses) — for the SysOp Backup screen and dashboard status (letter `K` for
"bacKup", since `B` is already every submenu's universal `[B]ack`), not
required for restore itself. A live-node screen can create another complete
backup; standalone `python -m netbbs.admin` remains status-only because it
does not own the running node's effective identity path.

`restore_backup(*, source, db_path, identity_dir)` reverses each of the
copies above -- **superseded by §13.10's staged/validated workflow (issue
#75)**: the
original mechanism restored each artifact in place, sequentially, with no
validation before the first live path was overwritten and no recoverable
state if interrupted partway. §13.10 replaces the restore side of this
mechanism; `create_backup` and the artifact table above are unchanged.

Restoration always resumes the same node identity; there is still no
supported way to run an old and a restored instance simultaneously -- a
second instance of the same identity already running on a *different*
machine remains an accepted, documented operator responsibility (§13.10's
own PID-file check only ever covers *this* machine).

**Explicitly deferred**: encrypting backup contents at rest (identity
material is already unencrypted-by-default on a live node — see §4.5 — and
this tool preserves whatever it finds rather than changing that policy), and
off-site/remote transport of a completed backup directory. The SysOp screen
explicitly identifies its output as local; both remain operator
responsibilities. Scheduling and retention of the schedule's own backups are
built in (issue #727, above); retention of backups a SysOp made by hand stays
theirs.

### 13.5 Bounded remote influence

Every remotely influenced queue, mailbox, retry set, retained-event collection,
transfer, relay store, and bandwidth consumer needs:

- an explicit limit;
- defined backpressure/rejection behavior;
- retry and terminal-failure policy;
- SysOp-visible state;
- safe defaults.

Security state and unread user data must not be silently discarded.

A caller's address is the key for per-source limits such as the login
throttle, so it must be one the caller cannot choose. For Telnet and SSH it is
the TCP peer. The web transport normally sits behind a reverse proxy, where the
TCP peer is the proxy for every browser caller. `[web] trusted_proxies` (issue
#980; empty by default) names the proxies, as IP addresses or networks, never
hostnames. Only for a connection from one of them does NetBBS read
`X-Forwarded-For`, and then it takes the rightmost entry that is not itself a
trusted proxy: each proxy appends the address it received the request from, so
everything left of that entry was written by the caller. A missing or malformed
entry falls back to the proxy's address. The address is decided once, when the
web session is built, so the throttle, the logs and the SysOp's screens agree.
The `Forwarded` header (RFC 7239) is not read: the proxies the Handbook
documents all write `X-Forwarded-For`.

### 13.6 Operational control surface

Issue #60 remains the authority for the incomplete production operating model,
including:

- generic persistent outbound-work items, retry, backoff, dead-letter,
  replay, and cancellation (§13.7 specifies this — `netbbs.link.work_items`,
  implemented, wired into `netbbs.link.mail`/`netbbs.link.sync`, and
  surfaced as an `[O]utbox` SysOp screen);
- sync-lag and historical/trend peer-health visibility (a read-only current-
  state view — peer count/mode, dial-reliability score, last contact (the
  last hello or events exchange with the peer itself; its descriptor
  refreshed secondhand from another node's peer list, or its mail picked
  up from a relay, does not count — issue #766), relay
  activity, board/event counters, and relay-mailbox size — is available in
  the SysOp menu's `[L]ink status` screen; per-seed health has nothing to
  show yet, since no per-seed success/failure tracking exists);
- disk, event, mailbox, relay, and bandwidth quotas (§13.9 — peer-count,
  events-per-request, carried-board-count, received-post-size, request-
  body-size, and request-rate quotas, implemented. Event-retention/purging
  and node-wide disk quota are explicitly deferred out of that slice — see
  §13.9's own reasoning);
- integrity checks and crash recovery (§13.11 — a startup `PRAGMA integrity_
  check`, plus confirming migration/incoming-upload/work-item crash safety
  already held by construction — implemented);
- bounded diagnostic log retention without content logging (§13.11 — a new
  `link_diagnostic_log` table and `[D]iagnostic log` SysOp screen, warning-
  level-and-above only, age/row-bounded — implemented);
- protocol/database upgrade and rollback compatibility (§13.11 — database
  upgrades use atomic migrations plus an operator-created pre-upgrade backup
  for rollback; `netbbs.selfupdate`'s automated snapshot/rollback orchestration
  remains unwired; the wire-protocol half, `netbbs_protocol` version-checked on
  receipt for the first time, is implemented);
- graceful drain of Link work during shutdown (§13.11 — `run_link_sync`
  finishes its current pass before stopping, including waking early from
  its own idle interval sleep rather than waiting it out, bounded by the
  existing `graceful_delay_seconds`, falling back to today's hard cancel
  only past that bound — implemented);
- disaster recovery drills exercising a restore under realistic conditions
  (§13.4 specifies the backup/restore mechanism itself — `netbbs.backup`,
  implemented; §13.10 replaces its original restore mechanism with a
  staged, validated, interruption-recoverable one and proves it against
  corrupt/truncated backups, missing components, and mid-switch
  interruption — issue #75, implemented, including a documented drill at
  `docs/NetBBS-disaster-recovery-drill.md`).

An externally operated persistent Link node should not be considered production
ready before these controls exist and have been exercised.

### 13.7 Outbound work items and retry (issue #60's second operational slice)

**Scope decision, made here rather than assumed**: this does *not* uniformly
cover every retry-shaped mechanism in the Link subsystem — only the two that
actually share the same shape. Auditing what exists today:

| Mechanism | Current behavior | Fits a work-item model? |
|---|---|---|
| Board/identity event gossip (`netbbs.link.sync`) | Every node-owned event is unconditionally re-pushed to every seed, every fixed-interval pass, forever — no attempt counter, no per-peer state at all. Safe and cheap only because the receiving side's own dedup (`link_events`) makes redundant delivery free. | **No.** There is no terminal "gave up" state that makes sense — a node's own content should be gossiped for as long as the node exists. Forcing this into a per-target attempt/backoff/dead-letter model would be inventing a failure mode (and per-peer tracking overhead) this mechanism deliberately has never needed. |
| Relay selection/consent maintenance (`_maintain_relay_selection`) | Continuously re-evaluated every pass against an evolving reliability score (`netbbs.link.reliability`), not a single item that must eventually resolve once. | **No.** This is ongoing re-optimization among many candidates, not "keep trying this one specific thing until it succeeds or we give up." It already has its own retry-like model (score-driven re-ranking); wrapping it in a second, differently-shaped abstraction would just be two competing retry policies for the same decision. |
| Link mail delivery (`mail_messages.link_delivery_status`) | Every `'pending'` row is re-pushed to its recipient every sync pass, forever, with **no cap** — the schema already reserves an unused `'expired'` status value for exactly this gap (round 93), never produced by any code path today. | **Yes.** A specific payload to a specific fingerprint that must eventually be confirmed or abandoned — the canonical case. |
| Link mail acknowledgement delivery (`link_mail_acknowledgements.sent_at IS NULL`) | Identical shape and identical gap: re-pushed every pass forever, no cap, no dead-letter. | **Yes.** Same reasoning as mail delivery. |

So `netbbs.link.work_items` is scoped to Link mail delivery and Link mail
acknowledgement delivery only — the two mechanisms that are both (a) a
specific payload addressed to a specific fingerprint, and (b) currently
missing exactly the retry/backoff/dead-letter/inspection issue #60 asks for.
Gossip and relay maintenance keep their existing, already-fit-for-purpose
models unchanged.

**A second scope narrowing, discovered while designing this**: a *work
item* resolving successfully means "the payload was successfully pushed to
the recipient's transport (or deposited at a relay)" — never "the recipient
confirmed receipt." That confirmation, for mail specifically, is a separate,
higher-level thing: `apply_link_message_accepted`/`apply_link_message_
bounced` already handle it, driven by a genuine signed event coming back,
completely unrelated to whether the push itself succeeded. Conflating the
two was a real risk in an earlier draft of this design — a work item is
**"pushed"** or **"dead_lettered"**/**"cancelled"**, never **"delivered"**;
`mail_messages.link_delivery_status` keeps its own independent
`'pending'`/`'delivered'`/`'bounced'` vocabulary, driven by accepted/bounced
events exactly as today. The one integration point is one-directional: when
a `link_mail_delivery` work item dead-letters or is cancelled (the payload
could never even be successfully pushed, or a SysOp gave up on it
manually), its letter's `mail_messages.link_delivery_status` becomes
`'expired'`, finally giving that reserved value a real producer, and a
replay puts it back to `'pending'`. The sync loop expires a dead-lettered
letter, since only it knows why delivery failed; `cancel_work_item` and
`replay_work_item` expire and un-expire in the same transaction as their
own status change, so no Outbox screen can cancel a letter and leave its
sender's Sent folder saying it is pending (issue #1535). A cancelled
letter's reason says the SysOp stopped trying to send it. A
successfully **pushed** work item changes nothing on `mail_messages`: it
still waits for accepted/bounced exactly as it does today, except the sync
loop stops wastefully re-pushing bytes that already arrived once — a real
efficiency fix, not just new capability.

**Schema** (`link_work_items`, matching this project's established Link-table
conventions — `TEXT NOT NULL` ISO timestamps, a `status` CHECK-constraint
enum, a partial index on the still-pending predicate):

```sql
CREATE TABLE link_work_items (
    id                  INTEGER PRIMARY KEY,
    kind                TEXT NOT NULL,  -- 'link_mail_delivery' | 'link_mail_ack'
    reference_id        TEXT NOT NULL,  -- mail_messages.link_event_content_id, or the ack row's own id
    target_fingerprint  TEXT NOT NULL,
    status              TEXT NOT NULL
                        CHECK (status IN ('pending', 'retrying', 'pushed', 'dead_lettered', 'cancelled')),
    attempts            INTEGER NOT NULL DEFAULT 0,
    next_attempt_at     TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    last_attempt_at     TEXT,
    last_error          TEXT,
    resolved_at         TEXT,
    UNIQUE(kind, reference_id, target_fingerprint)
);

CREATE INDEX idx_link_work_items_due
    ON link_work_items(next_attempt_at) WHERE status IN ('pending', 'retrying');
```

`reference_id` is a pointer, not a payload copy — `netbbs.link.work_items`
never stores or looks at the actual signed event bytes; the caller (mail
delivery/ack-push code in `sync.py`) already has those via the referenced
row and is the only thing that knows how to actually attempt the push.

**State machine**: `pending` (never attempted) → `retrying` (attempted at
least once, not yet resolved) → one of `pushed` / `dead_lettered` /
`cancelled` (terminal). `enqueue_work_item` is idempotent on
`(kind, reference_id, target_fingerprint)` — creating a mail message or an
acknowledgement always enqueues its work item at that same moment, so
there's no separate "did we remember to schedule this" step to forget.

**Backoff and dead-letter thresholds** (product judgment, not derived from
anything load-bearing — adjustable later): each failed attempt schedules
the next one at `min(300s * 2^attempts, 6h)` (starting at the sync loop's
own default interval, since backing off faster than the loop even runs is
meaningless, doubling from there, capped at six hours so a long-unreachable
target still gets retried a few times a day rather than trailing off to
nothing). Dead-lettered once `attempts >= 10` **or** `now - created_at >= 5
days`, whichever comes first — the attempts cap is what actually fires in
the common case (already ~29 hours of real spacing by the tenth attempt);
the age cap is a safety net for a node that was itself offline for a
stretch and so accumulated attempts slower than wall-clock time would
suggest.

**Mechanism**: no new background loop. `netbbs.link.sync.run_link_sync`'s
existing fixed-interval loop already iterates several independently-loaded
"pending work" lists every pass (seeds, pending mail, pending
acknowledgements, relay candidates) — `_push_pending_link_mail`'s and the
acknowledgement-pushing code's own `load_pending_link_mail`/
`load_pending_link_mail_acknowledgements` calls are replaced by
`load_due_work_items(kind=...)` (status in `pending`/`retrying` *and*
`next_attempt_at <= now`), and each attempt's outcome is recorded via
`record_success`/`record_failure` instead of silently falling through to
"try again next pass regardless." Everything else about the loop — lane
dispatch, per-item try/except-log-and-continue tolerance, the fixed sleep
— is unchanged.

**SysOp surface**: `list_work_items` (filterable by status/kind) plus
`replay_work_item`/`cancel_work_item`/`delete_work_item` (all audit-logged
via `record_action`, matching every other SysOp-triggered mutation in this
codebase) — the Outbox screen under Operations, listing dead-lettered/
retrying items with a picker to inspect one and replay, cancel or delete
it. `replay_work_item` resets a `dead_lettered`/`cancelled` item to
`pending` with `attempts = 0` and a fresh `created_at`, so a replay gets
the whole retry schedule again rather than meeting the age cap on its first
failure (issue #1245), and, for `link_mail_delivery`, is the one place the
caller undoes the `mail_messages.link_delivery_status = 'expired'` side
effect back to `'pending'` — symmetric with how dead-lettering set it.
`delete_work_item` removes a `dead_lettered`/`cancelled` row outright, after
a yes/no, for a recipient that is gone for good, where every replay would
only fail again and keep Link reporting ATTENTION (issue #1245). It touches
nothing else: a letter stays `expired`, so its sender still knows it was
not delivered.

**Explicitly deferred, not part of this slice**: retention/purge of old
`dead_lettered`/`cancelled`/`pushed` rows (this table will otherwise grow
without bound — a real gap, but a generic "how long do resolved audit-
shaped rows live" question this project doesn't have an established answer
to yet, not specific to work items); applying this abstraction to any
future work kind beyond the two named here without first checking it
actually fits the shape (see the scope-decision table above — the fit
matters more than the count of kinds). The quotas/integrity-check/
log-retention/upgrade-rollback/graceful-shutdown bullets in §13.6, separate
pieces of issue #60, were open when this slice was written and have since
been closed by §13.9/§13.11.

Implemented: `netbbs.link.work_items` (schema, backoff/dead-letter,
replay/cancel, all audit-logged); `netbbs.link.mail.compose_link_message`/
`_queue_acknowledgement` enqueue a work item in the same transaction as
the row it tracks; `netbbs.link.sync._push_pending_link_mail` now attempts
only currently-due work items instead of unconditionally resending every
pending row every pass, and already skips a push entirely once a message
has resolved through some other path (a genuine accepted/bounced event, or
an earlier dead-letter); a new `[O]utbox` SysOp screen (`System` submenu,
gated on Link being configured, same as `[L]ink status`) lists
retrying/dead-lettered items and lets a SysOp replay or cancel one.
Issue #804: an attempt this node's own trust policy stops records the
`last_error` `link policy refused target` and counts toward dead-lettering
like any failed push, so such mail expires too; each sync pass first makes
every item held that way due at once when the policy now allows its target,
without resetting its attempts or age.
Verified end to end via `tests/test_link_sync.py`'s existing real-socket
sync tests (unchanged, still passing against the refactored push loop)
plus new dedicated tests for the state machine, the mail/ack integration,
and the SysOp screen.

### 13.8 Session lockdown, drain, and shutdown

Three related but distinct SysOp `[N]ode`-menu controls over who can
connect and who stays connected, each answering a different question:

| Control | Question it answers | New logins | Already-connected sessions | Reversible | Ends the node process |
|---|---|---|---|---|---|
| `[S]hutdown` | "Take this node down" | Blocked for everyone, no bypass | Warned (staged), then all disconnected (immediately, or after an operator-chosen grace period) | Yes, while still counting down — see below | Yes, once the countdown finishes |
| `[M]aintenance mode` | "Stop admitting ordinary users for now" | Blocked for non-SysOps; a SysOp can still log in | Untouched | Yes — toggle again | No |
| `[D]rain` | "Clear ordinary users off, right now" | Unaffected (not this control's job — see the reminder below) | Non-SysOps warned (staged), then disconnected after an operator-chosen delay; SysOps (including the issuer) untouched | Yes, while still counting down | No |

`[S]hutdown`'s own sequence (round 51) already had the right order in
code — lock out new logins and warn everyone immediately, then disconnect
once the delay elapses — but its confirmation prompt's wording had drifted
out of sync with that, describing disconnect happening *before* the
lockout; fixed to match the actual, unchanged behavior.

**`[M]aintenance mode`** (`netbbs.net.maintenance.MaintenanceMode.
enable_lockdown`/`disable_lockdown`/`is_lockdown_active`) is a second,
independent flag on the same class that already holds shutdown's
`activate`/`is_active` — deliberately not the same flag, and deliberately
checked at a different point in the connection lifecycle: shutdown's gate
fires before login even begins (nothing is known yet about who's
connecting, so no bypass is possible or desired — the whole node is going
away regardless); lockdown is checked *after* credentials verify
(`netbbs.net.login_flow.run_authenticated_session`), specifically so a
SysOp can still reach the menu that turns it back off. A SysOp who logs in
while it's active sees a `(Maintenance mode is ON.)` notice appended to
their welcome line; a non-SysOp sees `LOCKDOWN_MESSAGE` and is disconnected
before ever reaching the main menu. Turning lockdown on does nothing to
sessions already connected — that is `[D]rain`'s job, a deliberately
separate action, not an implied side effect. The `[D]rain` screen's own
intro text now says so explicitly too (issue below): draining alone does
not block new non-SysOp logins, only `[M]aintenance mode` does that — a
real point of confusion in practice (Thiesi's own dogfood-testing report:
a SysOp assumed draining alone would leave the node empty afterward, when
in fact it only guarantees an empty node at the one instant the disconnect
actually fires).

**`[D]rain`** (`netbbs.net.shutdown.run_drain_sequence`) borrows
`run_shutdown_sequence`'s warn-then-disconnect shape but never touches
`maintenance`/`shutdown_event` — the node keeps running throughout.
`ActiveSessionRegistry.broadcast_to_all`/`disconnect_all` both gained an
`exclude_sysops` parameter (backed by a new `is_sysop` flag recorded on
each session's entry at `mark_authenticated` time) so a SysOp, including
whoever issued the drain, is never warned or disconnected by it — the
whole point is staying connected to keep managing the node while ordinary
users clear out for a change that needs a reconnect to take effect.

**The intended workflow** (Thiesi's own framing): turn on `[M]aintenance
mode` first — if nobody else is online, or existing sessions don't need
disturbing, that alone is enough. If someone connected already needs to be
moved along, follow up with `[D]rain`. The two are composable, not
coupled: `[D]rain` never enables lockdown itself, and enabling lockdown
never triggers a drain — each is a deliberate, separate SysOp decision,
matching how follow/membership/node-carry are kept independent elsewhere
in this design (§6.6) rather than one silently implying another.

#### 13.8.1 Scheduling, cancellation, staged reminders, and visibility (round-trip closed after real dogfood use)

A real multi-day dogfood run of the three controls above (§17's own
"run it for real, not just test it" mandate) surfaced five concrete UX
gaps, all closed together since they share one root cause and one fix:

**The bug.** `[D]rain`/`[S]hutdown` each launched their sequence as a bare
`asyncio.create_task(...)` with nothing tracking that one was already in
flight. Running the same command twice launched two independent,
uncoordinated countdowns racing each other — a second, shorter delay could
disconnect everyone while a reconnecting user still had to wait out
whatever remained of the *first*, now-orphaned countdown too, with zero
visibility into any of it.

**The fix: `netbbs.net.shutdown.SequenceScheduler`.** One instance per
node *per sequence kind* (`drain_scheduler`, `shutdown_scheduler` —
constructed in `netbbs.__main__` alongside `session_registry`/
`maintenance`, threaded the same way) tracks at most one currently
in-flight sequence: its deadline, its custom message (if any), and the
`asyncio.Task` actually running it. `schedule()` always cancels-and-
replaces any existing one first — the actual fix for the stacking bug.
Deadlines are `asyncio.get_running_loop().time()`-based (monotonic,
process-local); nothing here needs to survive a restart or be compared
across processes. A signal-triggered shutdown (SIGTERM/SIGINT) registers
with the same `shutdown_scheduler` a live SysOp session's `[S]hutdown`
command would use — one shared source of truth regardless of what
triggered it, so a connected SysOp sees accurate status either way.

**Provenance and cancellability (issue #108, revised after this
subsection originally shipped).** Each tracked sequence also records
`source` (`"sysop"`, `"sigterm"`, `"sigint"`) and `cancellable`, both
defaulting to the SysOp-created shape every pre-existing caller already
had. `netbbs.__main__._install_signal_handlers` is the only caller that
passes `cancellable=False` — a service supervisor's SIGTERM/SIGINT
outranks an in-BBS choice to keep the node running, so a connected SysOp
must never be able to cancel (or, equivalently, silently *replace*) a
shutdown that supervisor triggered; escalating to SIGKILL if NetBBS kept
running anyway would defeat the whole point of a graceful stop.
`SequenceScheduler.cancel()` — the explicit "cancel it?" action — refuses
outright for a non-cancellable sequence; `schedule()` itself stays
unconditional regardless (a second real SIGTERM must still be able to
reset an in-flight SIGTERM countdown), so `netbbs.net.admin_sessions.
_shutdown_screen` is the one responsible for never reaching its own
`schedule()` call in that case either — a non-cancellable, already-
scheduled shutdown gets a status-only message and returns immediately,
never the ordinary "schedule a new one" prompts. A SysOp-created shutdown
remains fully cancellable/replaceable exactly as this subsection
originally described.

**`[L]ock & drain` ownership (issue #109).** `[M]aintenance mode` and
`[D]rain` are deliberately separate, composable primitives; `[L]ock &
drain` (added after this subsection originally shipped) just composes
them for the common case a SysOp wants both together. Its own screen
(`netbbs.net.admin_sessions._lock_and_drain_screen`) must not infer whether
*it* is active from `maintenance.is_lockdown_active()` alone — that bit
is shared with the plain `[M]` toggle, so a lockdown enabled
independently would otherwise be misreported as "Lock & drain already
active," refusing to start the requested drain at all (the concrete
dogfood-adjacent bug this closes). `MaintenanceMode.enable_lockdown()`
therefore also records `source` (`"maintenance"` for the plain toggle,
`"lock_and_drain"` for the composite command — the same provenance
concept as the scheduler's own `source` above, reused rather than
inventing a parallel mechanism, exactly as the issue asked), and the
composite's own drain is tagged `source="lock_and_drain"` on
`drain_scheduler` too. The screen only ever reports itself "active," or
offers to undo anything, when it actually owns the lock
(`lockdown_source() == "lock_and_drain"`) — and even then only cancels
the drain half if it owns that too, never a drain some other, later,
independent action scheduled while its own lock was still up. Lockdown
already on for an unrelated reason is left completely untouched; the
composite command only ever adds a drain on top of it, never reclaims
or later disables it.

This one piece of state is what makes the remaining four gaps closable as
straightforward reads/writes against it, not four separate mechanisms:

**1. Explicit cancel-or-replace, not silent stacking.** Re-running
`[D]rain`/`[S]hutdown` while one is already scheduled shows its remaining
time and offers `[C]ancel it`, `[R]eplace it`, and `[B]ack` (unless it's a
non-cancellable signal-triggered shutdown — see the provenance paragraph
above, which gets a status-only message and an immediate return instead).
Cancel cancels cleanly and stops; Replace continues into the ordinary field
editor, and the resulting new schedule replaces the old one via
`schedule()`; Back leaves the existing schedule untouched (issue #282
replaced the earlier "Cancel it?" yes/no, whose "no" fell into the editor
regardless). `[S]hutdown` also gained a per-invocation delay prompt for
the first time (previously a fixed `graceful_delay_seconds` config value
with no override) — it now behaves exactly like `[D]rain`, Thiesi's own
explicit ask to close a "these two feel like different features" mental
disconnect that had never been intentional, just an artifact of the two
having been built in separate rounds. The config value is now only the
*prefill default* for the prompt, and what the SIGTERM/SIGINT signal path
still uses (no one to prompt there).

A SIGTERM shutdown ends its countdown early once nobody is connected,
checked before the first warning and about once a second after it (issue
#845). A service-manager stop or restart runs on the configured default,
not a delay anyone chose for the occasion, and with no caller left there is
nobody to wait for. A console `[S]hutdown` and the Update restart keep the
full delay the SysOp chose even if everyone, themselves included, leaves:
the SysOp may come back to cancel it.

Cancelling a *scheduled* graceful shutdown needed one real design
decision: `MaintenanceMode.activate()`'s own docstring already stated "no
way back" — true once a shutdown reaches its actual disconnect step, but
no longer true for the countdown window before that, now that the window
is cancellable. `MaintenanceMode.deactivate()` is the one narrow
exception, called only from `run_shutdown_sequence`'s own
`except asyncio.CancelledError: maintenance.deactivate(); raise` handling
around the graceful countdown — reopens new-login admission if (and only
if) the countdown itself is what got cancelled, never after
`disconnect_all()` has actually run.

**2. Staged countdown broadcasts, Unix-`shutdown`-style.** Both sequences
now broadcast on schedule, then again at 5 minutes remaining and 1 minute
remaining (only the ones the total delay actually reaches — a 30-second
drain never fabricates a "5 minutes remaining" reminder it could never
pass through), then once more immediately before disconnecting —
`netbbs.net.shutdown._run_staged_countdown`, shared by both. A custom
message, if given, is broadcast verbatim at every stage rather than
varying with the remaining-time phrase — consistent with the existing
"message replaces the default text entirely" rule (Thiesi's own wording,
unchanged from before this round), just now applied at more than one
point in time.

**3. A freshly-connecting or freshly-logged-in user is told what a
one-off broadcast alone never could.** Before this round, drain state was
purely a one-shot broadcast — a user not connected at the moment it fired
(including one reconnecting *after* an earlier drain pass already
disconnected them) had no way to know a drain was still in progress until
it silently disconnected them again. Now the first main menu after login
tells a non-SysOp, once, above its prompt, that a drain is scheduled and
roughly how long until disconnection — SysOp-exempt, since drain never
actually affects them. It is told there, with the pending chat
invitation count, and not written during login, because the menu's
redraw-in-place clear would wipe it unseen (issue #923).
Separately, `netbbs.net.maintenance.LOCKDOWN_NOTICE` (deliberately
distinct wording from `LOCKDOWN_MESSAGE`, the actual non-SysOp rejection)
is shown to *every* connecting client right after the welcome banner,
before credentials are even checked — SysOp-ness isn't known yet, so this
can't be targeted any more narrowly, and a SysOp who's about to
successfully log in anyway must not be told "please try again later." The
existing hard pre-login `MAINTENANCE_MESSAGE` (shutdown's own unconditional
gate) also gained the scheduled shutdown's own remaining time, read from
`shutdown_scheduler`, when one is available.

**4. A visual, persistent indicator — not just a one-time line easy to
scroll past or never see at all.** The real dogfood incident this closes:
a SysOp turned maintenance mode on, then forgot, and only found out when a
user reported being unable to log in. `netbbs.net.login_flow.
_draw_main_menu`'s own `Choice: ` prompt (given a live `node_controls`) now
carries a compact prefix: the current BBS time (a snapshot at draw time,
deliberately not a ticking live clock — this codebase has no per-session
background refresh mechanism, and building one just for a clock would be
disproportionate to what was actually asked for), plus at most one alert
tag, most urgent first — `[SHUTDOWN M:SS]`, else `[DRAINING M:SS]`, else
(SysOps only, by construction: a non-SysOp who reached the menu at all
already implies lockdown isn't blocking them) `[MAINT MODE]`. The `[N]ode`
admin menu itself also gained an unconditional status line (maintenance
on/off, plus either scheduled control's own remaining time) — the SysOp's
own dashboard for the exact "did I leave this on" question that prompted
the whole fix. `netbbs.rendering.ALERT_COLOR` (a new palette entry) marks
all of this consistently, distinct from `PRIVILEGE_COLOR` (an account's
own permanent access badge) and `MUTED_COLOR` (routine informational
text) — this specifically means "something time-sensitive is happening to
the node itself."

**5. Operator-visible in stdout/the process log, not only the DB-backed
moderation log.** Scheduling or cancelling a drain/shutdown, and toggling
maintenance mode, now each log one `INFO`-level line
(`netbbs.net.admin_sessions`' own `_logger`) alongside the existing
`record_action` audit row — an operator watching a foreground terminal or
`journalctl` sees it happen without a separate DB query. `netbbs.__main__.
run()` also gained one `"NetBBS is ready to accept connections"` line,
logged once every configured listener/background task has actually
started successfully (immediately before the process blocks on
`shutdown_event.wait()`) — previously each transport only logged its own
"listening on..." line individually, with no single line marking that
startup as a whole had actually finished.

### 13.9 Quotas: closing the remaining bounded-remote-influence gaps (issue #60's third operational slice)

**Audit before design, same discipline §13.7 used for work items.** §13.5
already states the general rule (every remotely influenced resource needs an
explicit limit, defined rejection behavior, and SysOp visibility); this
section is the concrete audit of where that rule is and isn't met yet, and
the scope decision for what this slice actually closes.

**Already bounded, no change needed here** — peer-list entries per request
(`_MAX_PEER_LIST_ENTRIES_PER_REQUEST = 100`), unverified candidate
descriptors (`_MAX_CANDIDATE_DESCRIPTORS = 500`, new-fingerprint admission
capped but refreshing an already-tracked candidate is always allowed),
relay-serving slots (`max_relay_clients`, decline-not-error), relay mailbox
envelopes per recipient (`MAX_MAILBOX_ENVELOPES_PER_RECIPIENT = 50`, HTTP 507
on overflow, no eviction; since issue #891 each envelope is also dropped after
`RELAY_MAILBOX_RETENTION_DAYS`, §8.5), Link mail delivery/acknowledgement retry
(§13.7's backoff-then-dead-letter), local mailbox size (`MAX_MAIL_PER_
RECIPIENT`, evict-oldest-read/refuse-if-all-unread — already applied to
incoming Link mail too, bouncing rather than silently dropping), and Zmodem
upload size (`max_upload_bytes`, checked against both claimed and actual
running size).

**Gaps this slice closes** — every currently-uncapped *admission* point in
the Link protocol, plus the two content-validation gaps that most directly
let one peer impose unbounded cost on another node:

| Gap | Fix | Enforcement idiom |
|---|---|---|
| `LinkNode.peers`/`link_peers` — any node that completes a hello becomes a permanent peer, no cap (mirror-image gap to candidate descriptors, which *are* capped) | New `LinkConfig.max_peers` (default 1000 — generous relative to §14's declared small-network scale, but no longer infinite) | Same shape as candidate descriptors: admitting a genuinely *new* fingerprint past the cap is refused; a hello from an *already-known* peer (key rotation, descriptor refresh) is always accepted regardless of the count. Implemented as `handle_hello`'s own optional `max_peers` keyword (`None` default, unbounded, preserving every prior caller) rather than `handle_relay_consent_request`'s "caller decides" split — that idiom exists specifically for relay-consent's in-band `accepted=False` reply shape, which `handle_hello` has no equivalent of; refusing a hello is a whole-request failure either way, the same shape peer-list's own internal cap already uses. Only threaded into the *inbound* path (`LinkServer._handle_hello`/`_handle_relay_mailbox_pickup`) — `dial_hello` (outbound) is left unbounded by this cap, already indirectly bounded by `_MAX_CANDIDATE_DESCRIPTORS` plus the operator's own small configured seed list. |
| `handle_events` batch size — no per-request cap, unlike peer-list's own `_MAX_PEER_LIST_ENTRIES_PER_REQUEST` from the same design round | New `_MAX_EVENTS_PER_REQUEST = 200` beside the existing constant in `protocol.py` | Reject the whole batch with `LinkProtocolError`, identical to the peer-list precedent — a genuine sync backlog still drains over several passes rather than one unbounded request. |
| `board_post`/`board_post_edit` content size — zero validation on receive, unlike locally created posts (`netbbs.boards.posts.MAX_SUBJECT_BYTES`/`MAX_BODY_BYTES`) | Apply the same two constants inside `handle_events`'s `board_post`/`board_post_edit` branches | `LinkProtocolError`, matching every other malformed-event rejection already in that method. |
| Carried-board count — `materialize_carried_board` turns any verified `board_genesis` into a local `Board` row unconditionally | New `LinkConfig.max_carried_boards` | The `board_genesis` event is still verified, accepted, and gossiped on past this node (dedup and chain integrity for *other* nodes must not depend on this node's own local storage choices) — only *materializing* a local, browsable `Board` row is refused once the cap is hit. Past the cap the resource is recorded as *offered* for the SysOp to accept, not declined (issue #561, §9.3). |
| Link HTTP request body size — neither `LinkServer` nor `WebServer` sets `client_max_size`, so both silently inherit aiohttp's implicit 1 MiB default | Set `client_max_size` explicitly on `LinkServer`'s `web.Application()`, sized to comfortably fit `_MAX_EVENTS_PER_REQUEST` worth of events (2 MiB) | Turns an accidental library default into a deliberate, documented value; aiohttp's own 413 response is unchanged (not worth reshaping into a `LinkProtocolError` payload for a request that was rejected before any handler ran). |
| Link HTTP request rate — no throttling on any Link route at all, including the two unauthenticated ones (`/hello`, `/peers`) | New `netbbs.net.throttle.LinkRequestThrottle` (a small public wrapper around the existing `_KeyedTokenBuckets` machinery `LoginThrottle` already uses internally), keyed by source address, applied via an aiohttp middleware on every route -- constructed once in `netbbs.__main__` from three new flat `LinkConfig` fields (`request_rate_capacity`/`request_rate_refill_per_minute`/`request_rate_max_tracked_sources`, not a nested sub-dataclass) and passed into `LinkServer`, the same "build once, node-lifetime, threaded into the one real server" shape `_build_throttle` already uses for `LoginThrottle` | Exceeding it returns a plain HTTP 429, no signed payload needed (an unauthenticated-route response can't be signed meaningfully anyway). `None` throttle (every caller predating this) is a middleware no-op, not a hard requirement. |

**Explicitly deferred, not part of this slice** — following §13.7's own
precedent of naming what's excluded and why, rather than silently narrowing
scope:

- **`link_events` retention/purging.** The two places this gap already exists
  in code (`storage/migrations.py`, `link/store.py`) both name the same
  blocker themselves: purging has to reckon with `handle_events`'s own
  chain-idempotency self-heal logic for `key_transition`/`board_post_edit`/
  lifecycle events first, or a purge could resurrect a fork-detection false
  positive for an event this node legitimately already integrated. That is
  its own design pass, not a quota default.
- **Node-wide disk quota on blob storage** — the literal "disk" word in issue
  #60's own bullet, and still the single largest true gap (no `shutil.disk_
  usage`, no running byte counter, no code anywhere aware of aggregate
  storage consumption). Needs genuinely new disk-usage-tracking machinery
  this codebase doesn't have yet, not a threshold check against an existing
  number — sized as its own future slice rather than folded in here.
- **`link_work_items` terminal-row retention.** Locally driven growth (a
  node's own composed mail history), not remote-influenced admission — pairs
  more naturally with issue #60's separate "log retention" bullet than with
  quotas, and every individual item is already bounded by dead-lettering.
- **Zmodem transfer-rate (time) limiting.** The existing byte-size cap plus
  `ThrottleConfig`'s connection/session limits already bound the worst case
  tolerably; true transfer-rate instrumentation isn't worth building until a
  real problem is observed.

**SysOp visibility.** `[L]ink status` gains a peer-count line showing
`current/max_peers` (matching the existing `relaying_for`-slots-in-use
display precedent) and `current/max_carried_*` lines for boards, channels
and file areas, with offered and excluded counts (issue #683);
rate-limit rejections are logged the same way `LoginThrottle` rejections
already are, not surfaced as a separate screen in this slice.

Implemented.

### 13.10 Staged, validated restore (issue #75)

**Problem, confirmed by reading the code, not assumed from the issue alone**:
the original `restore_backup` copies each of the five artifacts (§13.4)
straight into its live path, sequentially, in place -- `shutil.copy2`/
`copytree` directly onto `db_path`/`identity_dir`/etc., with the previous
live directory `shutil.rmtree`d immediately before the replacement copy
starts. Nothing about the backup is checked before the first live path is
touched (the manifest's own fields today are metadata only -- no
checksums), and an interruption mid-copy leaves neither the old state (already
half-deleted) nor a complete new one -- exactly the "restore intended to
recover a node can make it less recoverable" failure the issue describes.
The write-lock probe (`_require_not_in_use`) also only ever catches a
transaction genuinely in flight at that instant, not an idle-but-running
node holding no lock between transactions -- its own docstring already
said so.

**Mechanism**: validate everything first, stage a full copy, then switch
staged artifacts into their live paths with atomic renames -- never restore
by copying directly onto a live path again.

**1. Manifest gains per-artifact checksums.** `create_backup` now writes
`manifest["checksums"] = {relative_path: sha256_hex}` for every file it
captures *outside* the content-addressed blob tree: the database snapshot,
each of the four identity files, the SSH host key, and the welcome banner.
The blob tree needs no manifest entry at all -- `netbbs.files.storage`
already lays every blob out at `root/{sha256[:2]}/{sha256}`, so a blob's own
path *is* its claimed hash; restore verifies the tree by recomputing each
blob's hash and checking it against its own filename, catching truncation/
corruption with no extra bookkeeping and no manifest growth as a node's
file area grows.

**2. Full validation before any live path is touched.** A new
`_validate_backup_source(source) -> Manifest`, called first, unconditionally:
manifest exists and parses; every checksummed file listed is present and its
hash matches; the database snapshot passes `PRAGMA integrity_check` *and*
opens cleanly as a real `netbbs.storage.database.Database` (this reuses,
rather than reimplements, that class's own existing "refuse a schema newer
than this build supports" guard from `_apply_migrations` -- restoring a
backup taken by a newer NetBBS version onto an older install was already a
real risk this makes checked, not just checked-if-someone-remembers); the
identity directory, if present, actually loads via `netbbs.link.node_
identity.NodeIdentity.load` (a genuine functional check -- chain-to-key
consistency and all -- not just "the files exist"). Any failure raises
`BackupError` with the specific problem before a single live byte moves.

**3. Node-liveness check gains a PID file, kept as a second layer alongside
the existing lock probe, not a replacement for it.** `netbbs.__main__`
writes its own PID to `db_path.parent / f"{db_path.stem}.pid"` once
started, removed in the same `finally` that already closes the database on
every exit path (SIGTERM, SIGINT, and startup failure alike). Restore reads
this file if present and checks the PID is still alive with a portable,
best-effort liveness check (`os.kill(pid, 0)` on POSIX; a `tasklist` shell-
out on Windows for local dev/test convenience -- the deployment target is
NetBSD, where the POSIX path is what actually matters) -- refuses if alive,
catching the idle-but-running case the lock probe alone could not. A PID
file present but pointing at a dead process is treated as a stale leftover
from an unclean exit (warn, proceed) rather than a hard refusal -- the same
"an operator responsibility, not a load-bearing distributed lock" framing
this section already applies to the cross-machine case.

**4. Stage before touching anything live.** Every artifact is copied (with
its checksum reverified against the fresh copy, catching corruption
introduced by the staging copy itself, not just the original backup) into
`db_path.parent / f".netbbs-restore-staging-{token}"` -- a sibling directory
on the same filesystem as the live targets, which is what makes step 5's
renames atomic rather than a second copy.

**5. Switch via rename, not copy, with a non-silent marker for the gap
between the first and last rename.** Before switching anything, restore
writes a small state file (`db_path.parent / ".netbbs-restore-state.json"`)
naming the staging directory, a per-token rollback directory, and which
artifacts remain to switch. For each of the five targets in turn: rename
the current live artifact (if any) into `db_path.parent /
f".netbbs-restore-rollback-{token}"` under its own name, then rename the
staged artifact into the live path, updating the state file after each
completed step. If any single rename fails, everything already switched is
renamed back from the rollback directory (best-effort, since the renames
already succeeded once and are switching back onto paths that still exist)
before re-raising -- recovering the previous generation automatically in
the common case. The state file is removed only once every artifact has
switched (success) or every switched artifact has been rolled back
(recovered failure); if the process is killed outright mid-switch rather
than raising a catchable exception, the state file survives as the "clearly
identified... not a silent mixture" record the acceptance criteria asks
for, and a subsequent `restore` invocation refuses to start a new one over
an unresolved marker rather than compounding the mess.

**6. The rollback generation is not auto-deleted on success.** A completed
restore leaves `.netbbs-restore-rollback-{token}` on disk holding the
*previous* live state, not silently discarded -- matching this project's
"never silently discard state a human might still need" stance (§13.5).
The CLI prints its path; cleanup is an
explicit operator/cron action (retention/rotation stays out of scope here,
same as §13.4's own already-deferred list), not automatic.

**Disaster-recovery drill.** Documented at `docs/NetBBS-disaster-recovery-
drill.md`: stop the node; corrupt or truncate a real backup and confirm
restore refuses before touching anything live; interrupt the restore
process mid-switch and confirm the previous generation is intact or the
state file clearly names what to do; complete a real restore and confirm
identity continuity (same fingerprint), every configured transport still
authenticates, previously created local content is still browsable, and
Link resumes gossiping with its peers on restart. Proven functionally,
live, against a real running/killed/restarted node process during
implementation -- corruption refusal (checksum mismatch caught before any
live byte moved, confirmed via before/after hashing), the PID-file check
against a genuinely running process, a stale PID file from a hard-killed
process correctly tolerated rather than blocking restore, and a full
restore-then-restart cycle with identity/content verified. Running the
drill specifically on NetBSD hardware remains the one piece this design
enables but does not itself execute from a non-NetBSD development
environment.

Implemented.

### 13.10a Operating policy is tunable from the console (issue #730)

The Link policy settings (carry caps, peering, relay capacity, catalogue and
transfer limits, request rate, diagnostic retention, live-relay bounds), the
login throttle and the shutdown delays are operating policy, not bootstrap
plumbing. A SysOp tunes them over time and must not need a shell for it. Each
resolves per key: an explicit config-file or command-line value, then a value
saved from **Settings → Network & login limits**, then the built-in default.
This is the precedence `[link] enabled` already has over the participation
answer. A config file that sets nothing behaves exactly as before, and one that
sets a key keeps it: the console shows that key as set in config and does not
offer it.

Resolution happens once, at startup. None of these values is read live -- the
caps, rates and retention are handed by value to objects built at startup -- so
a saved change applies at the next start, and the console says so and shows the
running value until then. Making individual keys live is a later, per-key
change, not a promise of this design. A stored value that no longer validates
is skipped with a warning rather than keeping the node from starting.

Bind addresses and ports, `public_url`, advertised addresses, paths and
`[managed_dns]` stay config-only: a wrong listener set from inside the BBS can
lock the SysOp out of the session they would need to fix it, paths are needed
before the database opens, and the managed-DNS values are service-bound and
secret.

### 13.11 Closing issue #60: integrity, diagnostics, protocol compatibility, graceful Link drain

Four remaining, previously-open bullets from §13.6 — audited individually
below, same discipline §13.7/§13.9 already used, each narrower in places
than its one-line issue wording once actually read against the code.

**1. Startup integrity check and crash recovery.** Confirmed by grep: `PRAGMA
integrity_check` exists nowhere in this codebase except inside issue #75's
own backup-validation path — an ordinary node startup opens the database
with no corruption check at all, so a corrupted file (disk failure, an
interrupted non-WAL filesystem operation, bit rot) surfaces only later, the
first time some unlucky query happens to touch the damaged page, as a raw,
confusing `sqlite3.DatabaseError` rather than a clear diagnosis at the one
point an operator can still act on it before real damage compounds.
`netbbs.__main__.run()` now runs `PRAGMA integrity_check` immediately after
`Database(config.db_path)` opens, wrapped into the same `StartupError`
message shape that already handles "wrong build/version" — refusing to
serve traffic against known corruption, matching round 56's own "refuses to
start with zero SysOps" precedent for a startup condition worth failing
loudly on rather than limping past. **Deliberately not folded into
`Database.__init__` itself** — a full-database scan on *every* `Database()`
construction would tax every admin script and the entire test suite (2500+
constructions) for a check only the one long-lived node process actually
needs once, at its own startup; `netbbs.__main__` calls it explicitly, once,
itself.

Crash recovery beyond that single check turns out to already exist,
confirmed by reading rather than assumed: `_apply_migrations` commits a
migration's schema change and its `user_version` bump in the *same*
transaction, so a crash mid-migration simply leaves `user_version`
unadvanced — the next startup resumes migrating from the correct point,
never re-applies a partial migration, never needs new code. `purge_incoming_
staging` already treats every leftover `.incoming` file as crash debris from
a previous run and removes it before any listener starts. `netbbs.link.
work_items` are DB-row-backed with their own retry/backoff already, so a
crash mid-processing just leaves an item `pending`/`retrying`, picked up
normally next pass. This slice adds one regression test proving the
migration-crash-safety claim directly (kill a `Database()` open partway
through applying migrations, confirm a fresh open resumes and completes
correctly) rather than leaving it as an untested assertion.

**2. Bounded Link diagnostic log, metadata only.** No Link operational log
exists today beyond whatever `logging.basicConfig(level=logging.INFO)`
sends to stderr — ephemeral, unbounded (retention is entirely the process
supervisor's problem), and gone the moment a terminal's scrollback rotates
or the service manager's own log rotation fires. A SysOp investigating "why
did sync with peer X stop working three days ago" has nothing durable to
look at. Deliberately **not** a general application-logging overhaul — the
existing `moderation_log` table is already this project's precedent for a
structured, DB-backed log, and the new one is explicitly its bounded,
non-permanent counterpart: a `link_diagnostic_log` table (`id`, `level`,
`logger_name`, `message`, `created_at`), populated by a small `logging.
Handler` subclass attached to the `netbbs.link` logger namespace at startup
(catching every existing `_logger.warning`/`.error` call already scattered
across `netbbs.link.sync`/`.transport`/`.reliable_nodes` via ordinary logger
propagation — no per-call-site instrumentation needed) at `WARNING` level
and above only; routine `INFO`-level chatter stays stderr-only, ephemeral,
exactly as today. Audited every existing call site this handler will now
capture (§13.9's own audit-before-design habit, applied here to *existing*
log statements rather than a new feature): every one is already about
protocol/dial/sync *events* — a URL, a fingerprint, an exception message —
never a Link message's decrypted body, a board post's content, or any other
user-authored payload. "Metadata only, never content" is therefore a
property of which fourteen call sites happen to exist today, not a new
filter this handler has to enforce — worth re-checking whenever a future
Link module adds a new `_logger` call inside this namespace.

Both `LinkConfig.diagnostic_log_max_age_days` (default 30) and
`diagnostic_log_max_rows` (default 5,000) bound it — the handler prunes
against both on every write, cheap at this log's realistic warning-only
volume. Browsable via a new `[D]iagnostic log` SysOp screen under `[S]ystem`
(alongside `[L]ink status`/`[O]utbox`/`[R]epair carried posts`, same
`link_context is not None`-gated visibility), the same paginated-picker
shape `[O]utbox` already uses.

**3. Link wire-protocol version compatibility.** A real, confirmed gap, not
a hypothetical: every canonical event envelope already carries `netbbs_
protocol` (`build_envelope`, `NETBBS_PROTOCOL_VERSION = 1`, round 27) — but
grep confirms nothing anywhere ever reads it back on receipt. A future
protocol revision bumping this field would today be silently ignored by
`handle_hello`/`handle_events`, which would then either crash on an
unfamiliar payload shape with a confusing low-level error, or — worse —
successfully parse a subset of fields that happen to still match and
silently misinterpret the rest. `netbbs.link.protocol` gains one shared
check, applied once per envelope at the single point `handle_events`
already extracts `object_type` before dispatch (covering all nine event
types from one call site, not nine), and separately against the hello
bundle's own embedded transitions/descriptor envelopes in `handle_hello` —
rejecting a `netbbs_protocol` that doesn't exactly equal this build's own
`NETBBS_PROTOCOL_VERSION` with a clear `LinkProtocolError` naming both
versions, never a raw parse failure. "Exactly equal," not a supported range
— there is no forward/backward-compatibility promise to honor yet, since
version 1 is the only version that has ever existed; the point of this
slice is having a real, tested gate *before* a version 2 ever needs one, not
guessing at compatibility rules for a wire change nobody has designed.

The database and protocol halves use separate compatibility mechanisms.
Database migrations are atomic, a newer-than-supported schema is rejected at
startup, and the supported operator procedure requires a complete pre-upgrade
backup so code and schema can be restored together. An install from the
console (§6.7) takes that backup itself; automated database rollback is not
implemented.
The wire-protocol check above is independently implemented in
`netbbs.link.protocol`, where received envelopes are version-gated without
coupling Link compatibility to release installation.

**4. Graceful drain of Link work during shutdown.** `run_link_sync` accepts
an optional `stop_event: asyncio.Event | None`, checked once at the top of
the outer loop (before starting a new pass, not mid-pass — deliberately
simple: passes are normally sub-second, so the value of checking more
granularly inside one is marginal against the complexity of doing so) so a
currently in-flight pass, including whatever HTTP call it's in the middle
of, finishes naturally rather than being aborted mid-request against
whatever peer is on the other end — the one asymmetry ordinary user-session
shutdown didn't have, since that path already warns and waits before
disconnecting anyone. Shutdown sets the event, then `asyncio.wait_for`s the
task against `ShutdownConfig.background_task_drain_seconds` (5s default,
a dedicated timer — not `graceful_delay_seconds`, which answers a
different question, "how long does a *human* get to notice a shutdown
warning before disconnection," and is already fully spent by the time
teardown starts), falling back to a hard `.cancel()` only if that bound is
exceeded (a pass stuck on an unreachable seed's own connect timeout, say).

The loop's own trailing `await asyncio.sleep(interval_seconds)` is
interruptible the same way: `stop_event`-provided callers wait on
`stop_event.wait()` bounded by `asyncio.wait_for(..., timeout=
interval_seconds)` in place of the plain sleep, waking immediately once
shutdown signals rather than waiting out however much of
`sync_interval_seconds` (default 300s) remains — an idle sleep has no
in-flight work to protect, so cutting it short costs nothing the way
interrupting a live HTTP call would. Callers that don't pass a
`stop_event` (`None`, the default) still get the original unconditional
`asyncio.sleep`, unchanged.

`daybreak_task`/`update_check_task`/`reliable_nodes_refresh_task` get a narrower
treatment: none of them talks to a Link peer (`reliable_nodes_refresh_task` fetches
the project's own reliable-nodes roster from `www.netbbs.org`, not a peer's
Link endpoint, and already retries on its own forgiving 24h cadence), so
*graceful* draining — letting current work finish rather than aborting
it — would be solving a problem none of them actually has; all three are
still cancelled immediately, exactly as before. But `update_check_task`/
`reliable_nodes_refresh_task` both reach a blocking `urllib.request.urlopen` call via
`asyncio.to_thread`, and cancelling the *awaiting* coroutine does not stop
that underlying worker thread, which keeps running the blocking call to
completion regardless (Python cannot forcibly abort a thread) — the
shutdown teardown step that then directly `await`s the cancelled task
needs its own ceiling, or an unresponsive fetch there hangs shutdown
independent of anything `link_sync_task` does. All three now share the
same `background_task_drain_seconds` bound `link_sync_task` uses above,
applied to the cancellation-*await* rather than to "let it finish
first" — `daybreak_task` never needed this in practice (a bare
`asyncio.sleep` always cancels promptly), but gets the same treatment for
consistency, at zero real cost.

**Known residual gap (Codex review, PR #228):** the bound above only
covers `netbbs.__main__.run`'s own await of the task. The worker thread
itself is not owned by asyncio and cannot be cancelled — verified by
direct repro, it keeps running (bounded by `urlopen`'s own `timeout=30`,
so tens of seconds, not indefinitely) even after teardown gives up
waiting on it, and Python's interpreter-exit machinery
(`concurrent.futures.thread`'s `atexit` hook) still joins every
outstanding `ThreadPoolExecutor` worker before the process actually
exits, regardless of this bound. A shutdown that looks prompt in the logs
is not yet a guarantee the OS process has actually exited. See
`docs/NetBBS-worklog.md` for the fuller mechanism and candidate fixes;
unresolved as of this writing.

**5. Lingering transport connections at listener stop.** A second real
~9-minute Ctrl+C hang on ReLink (2026-09-04), distinct from the one above:
a caller's client had vanished without a TCP FIN (asleep laptop, dropped
Wi-Fi). Ending its session only exits the SSH *channel*
(`SSHSession.close`); the connection underneath stays up until the client
disconnects, which a dead peer never does. `SSHAcceptor.close()` (and
asyncio's `Server.close()` for Telnet) only stops accepting, and on Python
3.12+ `wait_closed()` blocks until every admitted connection has actually
dropped -- so teardown's listener-stop step sat for the kernel's whole TCP
retransmission timeout (`[Errno 60] Operation timed out` on macOS, about
nine minutes; longer on Linux). Both listeners now track every connection
they admit -- authenticated or not, since a connection still in its auth
handshake or Telnet option negotiation never reaches the session registry
-- wait `background_task_drain_seconds` for them to close on their own,
then `abort()` whatever is left (no disconnect handshake, which a dead
peer could never complete anyway). `TelnetSession.close` bounds its own
`wait_closed()` the same way, because `StreamWriter.close()` flushes
buffered output first and shutdown gathers every session's close.
Rejected: closing the SSH connection from `SSHSession.close` -- a live
client exits on channel close by itself, so the abort belongs at the one
place that knows every connection, and only after the normal path has had
its chance.

The same class of dead peer also holds a node slot during normal operation
until that kernel timeout, since asyncssh sends no keepalives unless asked.
The SSH listener now enables transport keepalives (30s interval, three
misses), so a vanished client is detected within about ninety seconds
instead. Deliberately not an operator setting: an idle live client answers
keepalives for free and NAT mappings benefit from the traffic, so there is
nothing a SysOp would tune. Telnet has no protocol-level equivalent; its
dead peers surface on the next write, and shutdown no longer waits on them.

**Closes issue #60.** Every acceptance criterion that issue names is now
either implemented (this slice; §13.4/§13.7/§13.9/§13.10 before it) or an
explicitly deferred, separately-tracked follow-up with its own stated
reasoning (node-wide disk quota and event-retention/purging, §13.9;
per-seed historical/trend health visibility, §13.6) — not a silently
abandoned acceptance criterion.

Implemented.
