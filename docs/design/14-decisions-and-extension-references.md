# NetBBS design — 16. Decisions and extension references

This is the normative design reference for this area of NetBBS. Section numbers are preserved from the design index so existing `§n.n` references remain stable.

---

## 16. Decisions and extension references

GitHub issues are authoritative and may evolve beyond this summary.

### Issue #11 — canonical Link format

§7.2/§7.3/§7.4 now state the complete rule: canonical byte encoding (sorted
keys, recursive NFC, compact separators), the safe-integer bound, duplicate-
key wire rejection, omitted-versus-null field semantics, mandatory
object-type domain separation, event-identity distinctness (a nonce for
immutable creation events; `previous_event_id` chains, never `created_at`,
for per-object chains), `(home_node_fingerprint, local_user_id)` as a
node-vouched author's globally-scoped identity, and golden test vectors
(`tests/fixtures/link_canonical_vectors.json`).

Still open: this specification and its vectors exist only as this
codebase's own Python implementation plus one fixture file. No independent,
non-Python implementation has yet exercised the vectors to prove real
cross-language interoperability, and the rule is not yet published as an
external protocol document outside this repository. Closing that gap is
implementation/publication follow-up, not a further design decision.

### Issue #56 — unread, follows, activity, and search

§6.6 now states the complete design: cursor-based read/unread state for
boards, file areas, and channels (mail already had this); replies/mentions
derived from existing `parent_post_id`/message-body fields with no new
schema; a follow/favourite table independent of channel membership and node
carry; a `[N]ew scan` activity surface covering every accessible resource,
not only followed ones, with a direct jump to the first unread item; local
FTS5-backed search scoped to this node's own carried content, explicitly
never broadcast over Link; and a zero-backfill migration story (existing
users' read cursors start empty; first post-upgrade visit sets the
baseline).

Implemented: the read-cursor table (`netbbs.activity`), the follow table, and
the `[N]ew scan` main-menu screen, wired into board/file-area viewing and
channel scrollback replay. Verified against a real Telnet session, not just
scripted tests.

Also implemented: local FTS5-backed search (`netbbs.search`) over board
posts, files, and channel scrollback, synced from every write path, gated by
the exact same visibility rules browsing already enforces, and surfaced as a
new `[/] Find` main-menu entry that jumps straight to a selected hit. FTS5
availability, this round's stated blocker, was resolved by tracing pkgsrc's
actual build chain rather than empirical access to a NetBSD box: `lang/
python312` buildlinks against `databases/sqlite3`, whose own Makefile passes
`--fts5` unconditionally, so the target Python build should always have it —
and a build that doesn't fails the migration loudly rather than silently
disabling search.

Issue #56 is fully implemented; all four §6.6 subsections have shipped.

### Issue #72 — node-local arrival order for unread state — closed

§6.6's "Node-local arrival order for carried content" subsection now
states the complete design: `user_read_cursors.last_seen_arrival_id`,
sourced from `posts`/`files`' own rowid rather than authored
`created_at`, with existing cursors backfilled on upgrade. The one
accepted, documented scope boundary: jump-to-first-unread still uses
the `created_at`-based cursor and may not navigate precisely to an
out-of-order arrival, even though unread counting now correctly flags
it.

### Issue #78 — decompose LinkNode protocol state — closed

The engineering record's "LinkNode internal state organization" entry
(§9) is the design pass this issue asked for: which of `LinkNode`'s
eleven flat fields belong together (`PeerDirectory`, `BoardEventState`,
`BoardLifecycleState`, `RelayState`), and which stay directly on the
façade (`identity`, `known_event_ids`, `events`, as the shared
substrate every object type uses). Every external consumer
(`netbbs.link.store`/`.sync`/`.transport`/`.relay_selection`,
`netbbs.net.admin_flow`, and their tests) still reads the old flat
attribute names unchanged, via backward-compatible properties over the
same live dicts -- zero test changes, zero wire/serialized-shape
changes. A future state family (inventory/pull, linked-channel
lifecycle) should follow the same shape: its own small dataclass with
narrow methods, not a further flat field.

### Issue #80 — end-to-end regression tests for cross-subsystem Link orchestration — closed

§14.1's "Cross-subsystem end-to-end scenarios" subsection now states
the complete design: `tests/test_link_end_to_end.py`, a real-transport,
real-domain-read-path vertical slice per implemented Link product
surface. It now covers linked boards, linked channels, remote file
catalogue/fetch, and Link mail, with restart and duplicate-delivery
coverage where the surface has persisted state. The mail vertical also
covers a dead-letter -> replay -> real-redelivery cycle end to end. Confirmed the consolidated
mail scenario (and its restart variant) would fail on the pre-fix
issue #69 implementation by temporarily reverting the fix and observing
both fail, then restoring it. Future Link vertical slices extend this
file before being considered complete.

### Issue #81 — supported platform tiers — closed

§2.1's "Platform support tiers" subsection now states the complete
policy: NetBSD (Tier 1, primary, with dependencies preferably supplied
by pkgsrc), mainstream Linux/systemd
(Tier 2, supported), other POSIX systems (Tier 3, best-effort), and
Windows (development-only, no production-semantics promise). Auditing
every existing `sys.platform`/`os.name` branch (`netbbs.net.
local_terminal`, `netbbs.backup._process_is_running`,
`netbbs.__main__`'s signal-handler setup) found the codebase already
drew exactly this line in practice, each in its own narrow, already-
isolated function — this closes the gap between that existing practice
and an explicit, written contributor-facing policy, rather than
requiring code changes.

### Issue #82 — operator-ready installation and release path — closed

[SysOp handbook](NetBBS-SysOp-Handbook.md) is the complete operator lifecycle:
install (a real, tested non-editable wheel build/install with no
source-checkout dependency, now sourced only from official GitHub
releases), first-SysOp bootstrap via the existing `netbbs.admin`
CLI, running under systemd/rc.d (`examples/netbbs.service`/`netbbs.rc`,
the rc.d script since confirmed working on real NetBSD hardware,
including its `LD_LIBRARY_PATH` handling for the pkgsrc-vs-base OpenSSL
runtime-linking gap documented in the worklog §10), persistent
state paths, backup/restore (linking the existing disaster-recovery
drill), upgrading, version/schema compatibility, and uninstalling
without losing data. `python -m netbbs --version` (issue #82) prints
the release version and expected schema number together. Installing a
release from the console arrived later (§6.7, issue #731); manually
installing an official GitHub-release wheel into the node's virtual
environment remains supported.

### Issue #74 — FTS index integrity checks and rebuild tooling — closed

§6.6's "Integrity checking and rebuild" subsection now states the
complete design: `netbbs.search.check_index_integrity`/`rebuild_indexes`,
a standalone `python -m netbbs.search check|rebuild` command, and the
explicit decision that startup does not run this check automatically
(unlike `Database.check_integrity`). Reports drift by id only, never
indexed content, for all three FTS tables.

### Issue #60 — production operations — closed

Implemented across four slices, all merged: backup/restore (§13.4,
`netbbs.backup`, verified against a real running node including a
create-wipe-restore round trip and the live-lock restore refusal); outbound
work items/retry/dead-letter (§13.7, `netbbs.link.work_items`, scoped to
Link mail delivery and acknowledgement delivery specifically — not gossip or
relay maintenance, which don't fit the same shape — wired into
`netbbs.link.mail`/`netbbs.link.sync` and surfaced as an `[O]utbox` SysOp
screen); bounded quotas (§13.9); and startup integrity checking, diagnostic
log retention, protocol/database upgrade compatibility, and graceful Link
drain on shutdown (§13.11). Staged/validated restore (§13.10) shipped
separately as issue #75.

Rehearsing these controls against a real long-running node (not just their
original implementation tests) is tracked by the Phase 3 stabilization gate
above, not by this issue.

### Issue #85 — inventory/pull-based catch-up and multi-hop relay

§8.8 now states the complete design: a signed `InventoryRequest` bundle
(not a canonical event, matching `PeerListMessage`'s own precedent), a new
`POST {LINK_PATH_PREFIX}/inventory/{fingerprint}` route whose response
reuses the exact `push_events` raw-event-list wire shape, a responder-side
diff query unioning three sources (self-originated, locally-authored on any
carried board, and peer-received) so it is genuinely multi-hop, and a
nullable `board_id` column added to `link_events` to make that query cheap.
Bounded by the existing `_MAX_EVENTS_PER_REQUEST`/`max_carried_boards`
quotas, not new numbers.

**One necessary correctness fix, not zero protocol changes**, discovered
while implementing: `handle_events`'s board-scoped branches previously
required the wire-level sender to equal the content's own claimed
origin/author, which made a genuinely relayed event structurally
unverifiable (the relay is a different node than the author). Fixed by
resolving each branch's signing key against the content's own claimed
origin/author instead, gated on that fingerprint independently already
being a completed peer — preserving the same "never accept from a stranger"
property while correctly relocating which fingerprint it applies to. See
§8.8's own "real, worth-stating limitation" note for what this does and
does not enable.

Issue #94 subsequently widened responder enumeration to resources absent
from the request, so an empty inventory can discover a wholly novel board,
channel, or file-area catalogue through a carrier. The signed requester
authentication from issue #106 and destination/freshness/replay binding
from issue #124 are now part of that disclosure boundary; §8.8 is the
normative current request format.

Explicitly excludes retention/purging (issue #86, sequenced after this one)
and Link messages (already point-to-point by design, §10, untouched by the
`handle_events` fix above).

### Issue #86 — event/dedup retention

§8.9 now states the complete design: the chain-idempotency gap was in
`board_origin_transfer_offer`/`_accepted`, the only two board-scoped types
whose idempotency depended solely on the fast `known_event_ids` cache
rather than a self-heal check against their own authoritative state
(`pending_offer`/`board_lifecycle_head`) — fixed with the same self-heal
shape `key_transition`/`board_post_edit` already use. Tracing what else
depends on each object type's `link_events` row surviving (restart
reconstruction, and issue #85's own inventory diff) found only
`key_transition` genuinely redundant with an already-durable separate
source (`link_peers.transitions_json`); everything board-scoped stays
unbounded in this issue, stated explicitly rather than silently assumed
safe. `netbbs.link.store.purge_expired_key_transitions` purges on write
(90-day fixed window), the same shape `LinkDiagnosticLogHandler.emit`
already established for `link_diagnostic_log`.

### Issue #90 — tier-2 Link message routing scope — deferred

§10.6 now states the complete answer: distinct from the unrelated,
already-decided `tier2_personal_key` non-goal (§10.2, a hard architectural
blocker); this is a recipient-*reachability* question with no equivalent
blocker, just not built. Neither issue #85's relay nor issue #630's
identity introduction provides tier-2 reachability: the first carries
content, the second lets a receiver verify the author of carried content,
and Link mail still requires a completed hello with the recipient's node.
The relayed, self-certifying `HelloMessage` bundle exchange this entry
first named as the missing piece was built for #630, for verification only;
§10.6 lists what mail would need beyond it. Deliberately deferred rather
than scoped as active work: no architectural blocker, just not needed yet.

### Issue #87 — linked channels — closed

§9.6 states the complete design and is now fully implemented:
`channel_genesis`/`channel_message` event types mirroring `board_genesis`/
`board_post` minus the fields that don't apply (no edit chain — channel
messages have no local edit concept at all; no `default_min_write_level`/
`_moderated`/`_max_post_age_days` equivalents — `Channel` has none of those
settings to recommend). Origin succession is reused by reference (§9.4's
model applies unchanged, if ever needed) rather than a new
`channel_origin_transfer_offer`/`_accepted` pair built in this issue —
genesis, promotion, materialization, and message propagation are the
actual scope, matching what actually shipped. `netbbs.link.channels`
mirrors `netbbs.link.boards` closely; `ChannelEventState` (genesis only, no
edit-chain half) mirrors `BoardEventState`; `handle_events` gained
`channel_genesis`/`channel_message` branches with issue #85's multi-hop
verification model from day one (never had the older restriction to begin
with). Issue #85's inventory mechanism (`InventoryRequest.channels`,
`channel_event_diff`) and issue #86's restart reconstruction both extend
to channels the same way they already cover boards.

Carry/materialization follows §9.3's exact shape, with one real, stated
consequence of reusing the existing bounded scrollback rather than
inventing unbounded storage for channel content: a materialized linked
message is subject to the same trim a local one already is, and a
self-originated message trimmed before any sync pass pushes it is simply
never propagated — bounded and honestly scoped, not a silent surprise,
since the identical bound already governs every channel's own local
history today. `channel_messages.link_content_id` is a new column solving
a real gap found while implementing: unlike `posts.post_id`,
`channel_messages.id` is a plain autoincrement with no existing
content-addressed column to key idempotent materialization off of.

**Not done in this issue, closed by issue #91:** wiring `queue_channel_
message_if_linked` into `netbbs.net.chat_flow`'s live interactive send
path was deferred here (`chat_flow.py`'s message-send code had no existing
`link_context` threading at all, unlike `login_flow.py`'s board-post path)
and completed separately — see issue #91's own §16 entry.

### Issue #88 — board closure, moderator edits, tombstones — closed

§9.5 states the complete design and is now fully implemented: `board_closure`
(extends §9.4's own `board_lifecycle_head` chain, terminal), `board_post_
moderator_edit`/`board_post_tombstone` (both extend the same per-post chain
`board_post_edit` already does, origin-signed rather than author-signed, the
latter terminal too). No new authorization primitive — all three verify
against the board's current origin's signing key exactly like `board_origin_
transfer_offer` already does; the two post-scoped types rely on a local
`BoardPermission.EDIT`/`DELETE` check already made on the origin node, before
`netbbs.link.boards.queue_board_post_moderator_edit_if_linked`/`queue_board_
post_tombstone_if_linked` ever builds the event — never a new gossiped grant.

`posts.tombstoned_at` is a plain nullable `ALTER TABLE ADD COLUMN`, not a
`CHECK`-widening rebuild: an earlier migration (post-`root_post_id`) already
found and documented that `posts` cannot safely go through the usual
drop/rebuild pattern, since it is a live *self-referencing* FK parent
(`parent_post_id`/`root_post_id`/`edit_of_post_id` all reference `posts.
post_id`) — the additive-column path sidesteps that risk entirely rather
than re-testing it. `netbbs.boards.posts.tombstone_post` is a new local
function, not a repurposed `delete_post`: it inserts a further
content-addressed revision (placeholder subject/body, `tombstoned_at` set)
rather than removing the row, so the edit chain and any reply's
`parent_post_id` stay intact — `delete_post` itself is unchanged, still
reserved for a still-`'pending'` post's rejection.

**Live UI wiring, stated explicitly:** `board_closure` and `board_post_
moderator_edit` reach real interactive call sites this issue — a `[C]lose`
option on the board admin screen (`netbbs.net.admin_flow`, gated the same way
`[T]ransfer origin` already is: origin only, board not already closed), and
the existing `[E]dit` flow (`netbbs.net.board_flow._edit_existing_post`)
building a moderator-edit event instead of a self-authored one whenever
`edited_by` isn't the post's own author and this node is the board's origin.
`board_post_tombstone` did not have any existing "delete an approved,
already-published post" UI action to extend (the only existing `delete_post`
call site handles pending-post rejection, which never reaches an already-
`board_post`-queued row) — a new `[T]ombstone` option was added alongside
`[E]dit` for exactly this reason, gated on `BoardPermission.DELETE`.

### Issue #89 — remote file catalogue and chunk transfer — closed

§11 states the complete design and is now fully implemented: `file_area_
genesis`/`file_descriptor` mirror `board_genesis`/`board_post` for catalogue
discovery (no content, no edit chain, no origin succession — the same
deliberate deferrals issue #87 already set for channels); chunk transfer
(`netbbs.link.file_transfer`, `FileChunkRequest`/`FileChunkDescriptor`) is
genuinely new — a direct point-to-point pull against the file's own origin,
never gossiped, never passed through `handle_events`. `remote_files` is a
new table, not a row in the real `files` table — that table's own invariant
("bytes always exist before the row does") stays true unconditionally, and
a catalogued-but-not-fetched file is a state `files` was never designed to
represent; `netbbs.files.storage`'s existing content-addressed layout is
reused once a transfer completes and verifies, so a fetched file dedups by
hash automatically, same as a local upload. `transfer_id` is deterministic
(`file_id` + this node's own fingerprint), and `chunk_id` (the chunk's own
sha256) is the exact-dedup key for a resent/duplicate chunk request —
found and fixed a real bug while writing tests: `materialize_carried_file_
descriptor` initially keyed `remote_files.file_id` off the signed *event's*
own `content_id` rather than `payload["file_id"]` (the file's actual local
identity) — two different hashes for the same object, the same distinction
`FileDescriptor`'s own docstring already calls out as the reason `file_id`
is carried explicitly in the payload rather than reusing `BoardPost`'s
"the event's content_id already is the object's identity" precedent.

Bounded per §13.5: `max_carried_file_areas`/`max_remote_files_per_area`
(carry-limit errors, tolerated the same way `BoardCarryLimitError` already
is), and `max_concurrent_file_transfers_per_peer` on the serving side,
tracked in memory only (never persisted — serving one chunk is otherwise
fully stateless). Chunk transfer is deliberately not folded into issue #60's
work-item/DLQ abstraction, the same "not every retry-shaped mechanism fits"
reasoning §13.7 already documents — it already has a natural resumable-by-
construction terminal state.

**Explicitly out of scope at the time, tracked separately and since
closed:** file-area origin succession remains untracked (no issue yet);
inventory/pull catch-up (§8.8) extended to file areas was deferred here and
closed by issue #93 (§11.4); a live SysOp/user TUI action to browse a
remote area's catalogue or trigger a fetch was deferred here and closed by
issue #92.

### Issue #91 — wire linked-channel messages into the live chat send path — closed

Closes the gap issue #87's own §16 entry named: `netbbs.net.chat_flow._chat_
loop`'s message-send path now threads an optional `link_context` (mirroring
`netbbs.net.board_flow._show_board`'s own parameter for board posts exactly)
down from `browse_channels`, and calls `netbbs.link.channels.queue_channel_
message_if_linked` right after a self-authored message is locally recorded,
whenever `channel` is Linked. Fire-and-forget, no separate success/failure
message shown to the sender — the same shape `queue_board_post_if_linked`'s
own call site already established; the actual outbound push, and its own
failure handling, lives entirely in `netbbs.link.sync`'s existing background
loop, unchanged by this issue. An unlinked channel, or a session with no
`link_context` at all (Link disabled, or a caller bypassing `netbbs.net.
login_flow.handle_session`'s real wiring), behaves exactly as before —
local chat stays fully usable and Link-unaware.

Minimal threading, no broader `chat_flow` refactor: only `browse_channels`/
`_chat_loop` gained the new parameter, and only the three existing `netbbs.
net.login_flow` call sites (`[N]ew scan`, `[/] Find`, the main channel-browse
menu) needed updating to pass their own already-in-scope `link_context`
through. A real two-node end-to-end test (`tests/test_link_end_to_end.py`)
drives `_chat_loop` itself with a scripted `FakeSession`, not a direct
`queue_channel_message_if_linked` call, proving the interactive send path
specifically, per the issue's own acceptance criterion.

### Issue #92 — interactive browse/fetch UI for remote file catalogues — closed

Closes the UI gap issue #89 left open: `netbbs.net.file_flow._show_area`
gains a remote-catalogue screen (reachable from both the ordinary paginated
file listing and the "has no files yet" action bar, since a Linked area can
have remote catalogue entries with zero *local* uploads of its own),
offered whenever an optional `link_context` is given — threaded down from
`enter_file_area`/`browse_file_areas` the same way `netbbs.net.login_flow`'s
board/channel paths already thread it. It lists every catalogued
`RemoteFile` for the area via `netbbs.link.files.list_remote_files`
(fetched and not-yet-fetched alike, clearly labeled, so a user can tell
"catalogued and I already have it" from "catalogued and I don't" at a
glance — no separate hidden state); picking an already-fetched entry
reports that and stops, never re-offering a redundant fetch. Picking a
not-yet-fetched entry, after a yes/no confirmation, drives `netbbs.link.
transport.fetch_next_file_chunk` in a loop until the transfer completes,
fails, or the origin turns out unreachable (`dialable_base_urls_for_peer`,
new — chunk transfer is never relayed, so an origin with no advertised
direct address is reported clearly rather than attempted). Success is
reported once the transfer's own existing verification/promotion path
(unchanged by this issue) has already placed the content in the ordinary
local `files` table; the file is then reachable through the listing's
pre-existing download keys like any other, no new download path introduced.

No per-file access check inside the catalogue screen beyond what already gated
entering `_show_area` in the first place (design doc's own "merely knowing
a descriptor exists must not bypass local policy" acceptance criterion) —
a `RemoteFile` carries no independent moderation state of its own the way
a pending local upload does, so the area-level read/age/name-requirement
gate already enforced by whichever picker offered the area is sufficient.

`aiohttp`/`netbbs.link.transport` are imported lazily, inside the one
function that actually dials out (`_fetch_remote_file`) — `netbbs.net.
file_flow` is loaded unconditionally by every node, including one with
`aiohttp` not installed, matching the same lazy-import convention
`netbbs.__main__`'s own Link-server startup already established.

A full interactive-flow regression test (`tests/test_link_end_to_end.py`)
drives `_show_area` itself against a real second node — the
`[L]ink catalogue` key, `pick_item` selection, the fetch confirmation prompt
— proving browse -> fetch -> verify/promote -> ordinary download visibility
end to end, per
the issue's own acceptance criterion; `tests/test_file_flow_remote.py`
covers the UI-level edge cases that don't need a real second node (no
catalogue entries, an already-fetched entry, a declined fetch, an
unreachable origin).

### Issue #93 — inventory/pull catch-up for linked file-area catalogues — closed

§11.4 states the complete design and is now fully implemented: `Inventory
Request` gains a third `file_areas` key alongside `boards`/`channels`, the
identical shape issue #87 already added for channels; `netbbs.link.store.
file_area_event_diff`/`_all_file_area_events` mirror `board_event_diff`/
`channel_event_diff` exactly, unioning the same three sources (self-
originated genesis, self-authored descriptors, peer-received `link_events`
rows) those functions already established. `_handle_inventory` now shares
one overall `_MAX_EVENTS_PER_REQUEST` budget across all three diffs in
sequence (board, then channel, then file area) rather than three
independent caps.

`link_events` gains a nullable `file_area_id` column, populated at two
call sites rather than one: `netbbs.link.store.save_event`'s generic
dispatch (for `file_area_genesis`, mirroring how `channel_id` is populated
for `channel_genesis`) and `netbbs.link.files.materialize_carried_file_
descriptor`'s own direct insert (for `file_descriptor`, which — like
`board_post`/`channel_message` before it — skips `save_event` entirely).
`sync.py`'s inventory-response step also gained `max_carried_file_areas`/
`max_remote_files_per_area` parameters, threaded through from `__main__.py`
the same way `max_carried_boards`/`max_carried_channels` already are — a
real pre-existing gap, not a cosmetic one: before this issue, an inventory
response could carry an unbounded number of new file areas/descriptors
even though `LinkServer`'s direct-push path already enforced these same
two quotas (§13.9).

No restart-reconstruction changes were needed: issue #89's own `load_link_
node` work already rebuilt `node.file_areas` from both sources this
issue's diff query also reads, and `file_descriptor` has no chain state
beyond `known_event_ids`/`events` to begin with. Proven with the same two-
layer test shape issues #85/#87 established: a deterministic three-node
`ScriptedTransport` test (`tests/test_link_convergence.py`) proving the
protocol-level multi-hop mechanism in isolation, and a real-transport test
(`tests/test_link_end_to_end.py`) proving a node recovers a missed `file_
descriptor` through an intermediary while the original origin's server is
never started again for that stage — genuinely unavailable, not merely
unqueried. Chunk bytes remain outside inventory entirely and unchanged by
this issue, confirmed by both tests: a recovered catalogue entry lands
with `fetched_file_id` still `NULL`.

### Issue #55 — trust and quarantine — implemented; operational validation open

§12 specifies the Phase-4 attacker model, evidence classes, explicit reporter
trust domains, Sybil/weight rules, signal bounds and lifetimes, probation,
quarantine, reversibility, explainability, and required validation. The Phase-4
implementation is shipped; #131 retains the human/operational readiness gate.
Completing the implementation does not itself establish public federation safety.

### Issue #63 — door isolation — superseded by the shipped door model

Issue #172 established the native door model; #296/#297 added legacy and browser
adapters. Phase 7 above records the current contract. Native doors are trusted,
same-user programs with resource limits and supervised cleanup, not filesystem
or network containment. Door metadata is API version 3; companion services and
the SysOp-enabled outbound board-posting hook are implemented.

The [developer handbook](NetBBS-Developer-Handbook.md#developing-a-native-door)
is the integration entry point. The outbound hook posts as a distinct door label,
with an explicit per-door board allowlist and rate ceiling. A linked destination
can federate the post through the live node's identity. There is no generic
session-capability API for chat, mail, real-time game moves, or federated scores;
those extensions need their own protocol and authority decisions.

### Issue #474 — foreign-platform native doors in a VM — closed

A door that exists only for another platform -- the motivating case was
`aempire`, a statically linked Linux x86_64 Free Pascal door with no NetBSD
build and no source -- runs under `adapter: "vm"`: one qemu guest per caller,
booted from a kernel and initramfs the SysOp builds. The Phase 7 bullet above
states the runtime contract; the door guide's "Foreign-platform doors in a VM"
states the guest image contract and the manual setup.

**Measured, not assumed.** The scoping questions were answered on the
production host that motivated it (NetBSD 11.0 amd64, itself a VMware guest,
qemu 11.1.1 from pkgsrc):

| Question | Answer |
| --- | --- |
| Q1: does 9p `local` work on a NetBSD host? | Yes. Both exports mount (`9p2000.L`, `cache=none`); guest and host each see the other's writes. The persistent installation stays a directory, so backup and "install with ordinary file tools" are unchanged. |
| Q2: does `-chardev socket,fd=N` accept an inherited descriptor? | Yes, for the door's terminal and for a second socketpair carrying QMP. |
| Q3: qemu's address space? | About twice guest RAM plus 500 MiB: a 256 MiB guest fails at 768 MiB and starts at 1000; with `tb-size=64`, steady VSZ ~500 MiB. Validation requires `memory_mb >= 2 * guest_memory_mb + 512`. |
| Q4: cold boot? | 3.65 s ±0.05 to the door's first byte (microvm, PVH direct kernel boot, TCG), 3.7-4.0 s through the full runtime. `pc` with PVH: 6.4 s; `pc` with a bzImage: 7.7 s. |
| Q5: grant `/dev/nvmm`? | The operator accepted it (2026-09-12). Moot on this host: `nvmm` refuses to load there (`cpu not supported`), so everything above is software emulation (TCG). |

**Adapter, not runner.** A `runner` script could spawn qemu, but host
placeholders mean nothing inside a guest, and only a command line NetBBS
builds can *guarantee* `-nic none`, `-nodefaults`, `-no-user-config`, no
display and exactly two exports. `vm.py` owns the argv the way `dosbox.py`
owns `dosbox.conf`; the profile supplies paths, the guest command, the
accelerator and sizes, never qemu flags.

**Per-session, at 3.7 s.** The rescoping comment on the issue put the
threshold at ~3 s for per-session and ~5 s for a long-lived guest serving
callers as a door service (#466). The measurement fell in between, and
per-session was chosen: it keeps isolation per caller rather than per door,
needs no multiplexing inside the guest, confines a guest crash to one
caller, and reuses the existing reap-the-group lifecycle unchanged. A
caller waits about as long as a DOSBox-X start. The measurement is on the
slowest configuration there is -- nested virtualization with no hardware
acceleration -- so the choice only gets better where `nvmm` or `kvm` works.
A VM-backed door service remains possible later if a busy node needs it.

**Channel.** The door's existing socketpair becomes a virtio console
(`virtconsole`, `/dev/hvc0` in the guest): the tty lives in the guest, so
the host PTY's edge cases do not apply, and the door gets a real terminal.
vsock (Linux-only host support), SSH into the guest (needs guest networking
and a credential) and a bespoke multiplexer were rejected, as scoped.

**Exit status and stop.** qemu exits 0 whatever the game did, so the guest
writes the door's status to `exit.status` in the node export; missing or
unreadable is a failure, and `success_exit_codes` maps normal returns, as
`EXIT.ERR`/`RETURN.OK` do for DOS. The guest touches `booted` once its door
is about to start, which bounds a guest that cannot boot by
`boot_timeout_seconds` instead of the caller's time limit. qemu exits on
SIGTERM without informing its guest, so every stop path first presses the
guest's ACPI power button over a private QMP socketpair (the guest hangs
the door up and powers off), then signals the process group as for any door.
qemu stays in that group, so the kill still guarantees no guest outlives
its session.

**What the boundary buys.** A guest cannot read the node's database, keys,
configuration, other doors or anything outside its two exports, and has no
network -- strictly more than a native door. It is still the SysOp's
boundary: the installation export is writable, qemu runs as the service
account, the guest image and its patching are the SysOp's, and an
accelerator is a host-wide privilege grant. Documentation says "an isolation
boundary you provision and own", never "sandboxed doors".

**No shipped image.** NetBBS ships a build recipe
(`examples/doors/vm/build-alpine-guest.sh`, from Alpine's own packages) and
the reference init, not an image whose security updates it would own
forever. The capability probe boots the SysOp's own image with a NetBBS
fixture script and checks the console in both directions with CP437, a
write through the installation export and the exit-status handshake. It can
only be operator-run: unlike the DOS probe, it needs an image that no CI
carries. The runtime contract is instead tested against a fake qemu that
plays the guest's half over the descriptors it is given.

**Deliberately not built.** Resize forwarding into the guest (the console's
geometry is fixed at launch), guests for other architectures (the adapter
is `qemu-system-x86_64` and `microvm`), and the sibling `ssh-exec` adapter
for running a door on a second machine where it is native.

### Issue #165 — MRC gateway scoping — closed

**Goal:** let NetBBS callers reach the existing cross-BBS MRC (Multi Relay
Chat) network without coupling NetBBS's own premium chat model — Noise
XX transport, Link-wide presence, per-message trust-filtered scrollback,
all closed by issue #164 — to a third-party network's pace or ceiling.
Sequenced after #164, which is done (shipped in v5.3.0): this scoping
pass was unblocked the moment that landed.

**Reference protocol, since no formal MRC spec exists:** verified
directly against ENiGMA½ BBS's own reference client/multiplexer
(`core/mrc.js`, `core/servers/chat/mrc_multiplexer.js`), the most
actively maintained modern implementation. MRC is a single central hub
(historically `mrc.bottomlessabyss.net`), not a federated or
peer-to-peer network — every participating BBS is one more client of the
same hub. The wire protocol is a single persistent TCP socket (TLS
optional, separate port), newline-delimited, tilde-separated 7-field
lines: `from_user~from_site~from_room~to_user~to_site~to_room~body~`.
`to_user`/`to_room` double as addressing and as an ad hoc control
channel (`to_user="SERVER"`/`"CLIENT"` carries heartbeat/roster/registration
commands like `IAMHERE`, `USERLIST`, `STATS`, `LOGOFF`, `INFOSYS`/`INFOWEB`/
etc.). The only "handshake" is one unauthenticated line the client sends
on connect — `{boardName}~{clientSoftware}/{os}/{version}` — no password,
token, or signature ties a connection, a user name, or a claimed board
name to anything real; any client can claim to be any board. User and
site name fields are constrained to ASCII 33–125, 30 chars, room names
to 20 (the protocol page's `string[20]`, issue #376), with Mystic `|NN`
pipe-color codes stripped; message bodies to ASCII 32–125. There is no
history or
backfill concept at all — a message reaches only whatever clients happen
to be connected at the instant it's sent.

This resolves several of the issue's own open questions directly, rather
than leaving them for a later pass: MRC has no identity a gateway could
verify, so nothing about it can ever feed Phase 4's trust/reputation
model, and nothing resembling NetBBS's own trusted-scrollback concept
has an MRC-side equivalent to bridge to.

**Existing precedent this reuses, not reinvents:** `netbbs.link.
realtime_channels.LiveChannelBridge` is the architectural template — one
instance per running node, holding no storage of its own, forwarding
everything through the same `netbbs.chat.hub.ChatHub.broadcast()` a
purely local participant's own message already goes through. Its own
`_handle_channel_message` already renders a remote-authored message with
`author_fingerprint=None` and a descriptive `author_label` (`f"{user_id}
@{fingerprint}"`) — the shape an MRC-authored message reuses, with one
storage rule added on review: `channel_messages.external_source` marks
such a row so it is never attested as this node's own content (§6.3;
trusted-scrollback snapshots skip it and Link queueing refuses to sign
it). The outbound hook
point already exists too: `netbbs.net.chat_flow`'s per-message send path
calls `link_context.realtime_bridge.broadcast_local_message_live(channel,
recorded_message)` immediately after `record_message`/`hub.broadcast` —
a sibling MRC bridge attaches at that identical call site, independent
of `link_context` and of whether Link itself is even configured.

**Decision 1 (locked in) — one in-process bridge per running node, no
separate daemon.** Unlike ENiGMA½'s own two-tier design (a per-connection
client process plus a separate local "multiplexer" process fanning
multiple local sessions into one hub connection), NetBBS is already a
single asyncio process per node — there is no per-connection-process
architecture here to multiplex in the first place. One `MrcBridge`
instance, analogous to `LiveChannelBridge`, owns the one outbound hub
socket for the whole node.

**Decision 2 (locked in, amended by issue #300) — a local channel is
bridged only per channel, explicitly, and off by default; an MRC room a
caller opens is a channel of its own, behind a second node-wide switch
that is also off by default.** MRC rooms are flat, global, and
unauthenticated with no ACL concept at all — "every local channel
bridges by default" would silently leak channel contents onto a public,
unauthenticated network the moment a SysOp enables MRC at all. A SysOp
must name which local channel maps to which MRC room, the same opt-in
shape every other Link-adjacent per-channel setting already uses. The
amendment (issue #300) keeps that rationale intact: an *open room* has
no local content to leak because it is born on the network — a caller
asks for a room by name and it materializes as a real channel named
`mrc:<room>` (design doc §6.3), so enabling MRC alone still bridges
nothing, and the SysOp decides separately whether callers may open
rooms at all.

**Decision 3 (locked in, amended by issue #305) — channels by default;
private messages only for a caller who asked for them.** The protocol
argument stands: MRC's `to_user` targeting carries no real
confidentiality (the hub, or any client willing to lie about its own
identity, can see or spoof it), so NetBBS must never present an MRC
private line as private. What the original decision got wrong is the
remedy: refusing them outright leaves a caller who reads "bob@Other
tried to message you privately" with no way to answer, while every
other client on the network delivers such lines. So the bridge delivers
and sends private lines for a caller who switched them on in their
Profile (off by default, one switch for both directions), tells them
once per session what "private" means on that network, and never
records a private line anywhere. A caller who left the switch off gets
exactly what they got before: one notice per sender, nothing shown.

**Decision 4 (locked in) — inbound content is always rendered as
external/untrusted, and never enters Phase 4 at all.** An inbound MRC
message becomes a `ChannelMessage` with `author_fingerprint=None` and an
`author_label` such as `f"{mrc_user}@{mrc_site} (MRC)"` — visually
distinct from both a local and a genuine Link-originated author label.
It is never passed to `decide_node_action`/quarantine/trust scoring:
there is no cryptographic identity on the MRC side to hang a trust
decision on, so inventing one would be theater, not protection.

**Decision 5 (locked in) — bounded failure containment, matching this
project's existing "bound remotely influenced resources" invariant.**
Reconnect-with-backoff on the one outbound socket (mirroring the retry
behavior every MRC implementation already converges on); a bounded
outbound send queue so a stalled or unreachable hub degrades to that one
bridge going quiet, never blocking local chat delivery or the caller who
just sent a message; malformed or oversized inbound lines are dropped
and logged, never allowed to crash the local channel. Every outbound
field is sanitized to MRC's own documented charset/length limits before
it's sent. Inbound Mystic `|NN` pipe codes are untrusted text, never
raw ANSI: identity fields lose all of them, a body keeps only the
color subset (`|00`-`|23`), and those become NetBBS's own SGR solely
through `netbbs.rendering.pipe_codes`, after sanitization and per
viewer (issue #298, below) — "sanitize before styling" holds exactly.

**Implemented as issue #275** (`netbbs.mrc`), with every decision above
as specified and the three questions this scoping pass left open
answered as follows. Two further choices were made at implementation
time, both verified against four independent MRC implementations
(ENiGMA½, Synchronet, uMRC, ANetBBS) since the hub operator's own spec
page is not publicly reachable:

- **Identity on the MRC side is one MRC user per caller** — the
  caller's canonical username sanitized to MRC's rules (spaces to
  underscores, printable ASCII, 30 characters, reserved routing names
  suffixed), presented at this node's site name, with `NEWROOM` on
  entering a bridged channel, `IAMHERE` every minute and `LOGOFF` on
  leaving. Every reference implementation multiplexes this way; a
  single relay identity prefixing each line with the handle was
  rejected because MRC users could not see who is present or address
  anyone. The announced set is derived from the `ChatHub` roster, so a
  channel mapped while callers are inside it, or a reconnect after a
  hub outage, re-announces exactly who is there. The join banner tells a
  caller their handle is about to appear on a public network before
  the announcement is sent.
- **Inbound room messages are recorded into the channel's scrollback**
  (`record_message`, `author_fingerprint=NULL`, label `user@site (MRC)`),
  bounded by the existing scrollback limit and search-indexed like a
  carried Link message, so a caller joining later sees a coherent
  conversation rather than only the local half; the hub's own echo of
  this node's traffic is dropped by site name. Presence chatter, room
  topics and server notices stay ephemeral. Private (`to_user`) lines
  are never delivered; the addressed caller gets one muted notice per
  sender.
- **Configuration is DB-backed and edited live**, not a TOML section:
  Settings › Inter-BBS chat (MRC) holds the node-wide hub settings
  (enabled, host, port, TLS on by default with certificate
  verification, site name, INFO fields) and saving asks the running
  bridge to reload and reconnect; a channel's own detail screen holds
  its `[M]RC room` mapping and a `[P]ause` that keeps the mapping while
  relaying nothing — the per-bridge disable asked for above; Node ›
  Inter-BBS chat (MRC), named as under Settings, shows link state, hub,
  last error, attempt and drop counters and every bridged channel with the hub's roster, with
  `[R]econnect now`. Inside a bridged channel, the status line carries
  an `[MRC]` badge, `/who` and `/names` list the room's MRC users, and
  `/mrc` shows room, hub, link state and roster.
- **Bounds as shipped:** reconnect backoff 1–60 s with jitter, reset
  after 30 s stable; outbound queue of 200 lines (oldest dropped) behind
  a node-wide 5 lines/s bucket and a 3-line burst / 1 line/s bucket per
  caller (the sender is told quietly when a line was not relayed), and
  on the way out packets from one nick are spaced at least 0.5 s apart
  -- the hub's own per-user limit (MRCDoc rev 1.26; issue #375) -- with
  held-back lines counted under the same 200-line cap; the spec's field
  limits are enforced where a caller or SysOp types (room names 20,
  refused rather than cut; topics 55; passwords 20, room passwords 32;
  LASTSEEN and HELP arguments 20), and the handshake names the client
  as `NETBBS/<Os.arch>/<NetBBS version>` (issue #376);
  inbound 40-line burst / 20 lines/s ahead of any database write, 4 KiB
  line cap -- except the hub's reply to one caller (`/BBSES`, `LIST`,
  `HELP`), shown to them alone and written nowhere, which pays that
  caller's own 300-line burst / 10 lines/s allowance instead and says so
  when it cuts (a `/BBSES` listing of every connected board stopped,
  silently, after 39 boards); a local line is split into at most three
  140-character wire chunks. An `OLDVERSION` rejection from the hub is fatal until a
  SysOp changes settings, since every retry would start a fresh
  rejected session. Bridge warnings land in the same bounded
  diagnostic log Link already uses.

**Issue #298 (body convention, colors, CTCP, per-caller hub replies)**
corrected one shipped assumption and added what falls out of it. Every
reference client embeds the sender's own colored handle *inside* the
message body (Mystic `|03<|11Alice|03>|16|07 text`, ENiGMA½
`|00|10<|02Alice|10>|00 |03text`, Synchronet `Alice |07text`, ANetBBS
`|07Alice|07 text`; actions `|15* |13Alice text`) and displays an
inbound body verbatim; the hub adds nothing. The bridge therefore
sends every chunk in a house style (`|08<|14nick|08>|16|07 text`, the
prefix paid for out of the same 140-character budget) and peels an
inbound prefix only when the embedded name equals `from_user` -- an
unmatched body is recorded whole, nothing is guessed. Issue #1152 added
handles no reference client writes but users choose, such as
`+Nick+[CASTLE BBS]` or `^Nick<tag>`. The spec makes a body's first word
the sender's handle, so a first word that names `from_user` is peeled too,
with only punctuation touching the name. The word runs to the first
whitespace outside brackets, because a tag may hold a space. A body that is
nothing but that word, or has an unbalanced bracket, stays whole. How each
caller sees the peeled handle is their choice (issue #1156, §16). Decisions
taken with it:

- **Color codes are content, not markup.** `|00`-`|23` survive in a
  stored MRC body (printable ASCII, safe to store) and are turned into
  SGR by `netbbs.rendering.pipe_codes` only after sanitization, per
  viewer, under a Profile toggle that defaults to on; every other pipe
  token is stripped at the parse boundary, identity fields lose all of
  them, and the search index receives the plain words. The recognized sender
  prefix's foreground is stored separately as `mrc_nick_color` and applied
  to the author on live display and replay under the same color toggle.
  Old rows keep their text and default author styling. A caller's own
  typed codes are still stripped: letting them through would put codes
  into local scrollback and Link-signed exports of mapped channels.
- **The hub's reply to one caller goes to that caller.** A `SERVER`
  packet addressed to an announced nick is that caller's reply
  (`LIST`, `CHATTERS`, `INFO`, `MOTD`, `HELP`, ...), delivered to their
  sessions alone under a per-caller line allowance, never recorded;
  `/mrc <subcommand>` sends the asks. `USERROOM` re-announces the
  caller in the mapped room at most once per keepalive tick,
  `USERNICK` retargets the announced nick. Generic-client corrections apply
  only when exactly one caller is announced; with multiple callers they
  produce a bounded diagnostic and a caller notice without guessing an
  identity. Generic-client `STATS` replies update the network summary and
  are shown only to callers with outstanding explicit requests.
  `TERMINATE` is fatal like
  `OLDVERSION`. An empty `to_room` is a network broadcast, shown in
  every active bridged channel (ENiGMA½'s reading).
- **CTCP lives in the bridge.** `VERSION`, `TIME` (UTC only), `PING`
  and `CLIENTINFO` are answered for any announced nick, bounded per
  remote sender because every request costs a reply; no SysOp or caller
  wiring exists for it.

Decision 3 (channels only) is unchanged by this issue; its amendment
(opt-in private messages) shipped as issue #305, below. Decision 2's
amendment shipped as issue #300, next.

**Issue #300 (open rooms)** lets a caller reach any room on the network
without a SysOp mapping it first, without a door-style client, and
without teaching native chat anything about MRC. Decisions:

- **An open room is a real channel row, materialized on demand** — the
  same precedent as a carried Link channel (§9.6): the first caller to
  open `lobby` gets a `channels` row named `mrc:lobby` with
  `mrc_room='lobby'` and `mrc_origin='caller'`, gates copied from the
  node-wide open-room defaults (level, age, name requirement), content-
  addressed on the room name so a room reopened after retirement keeps
  its id. A room the SysOp already mapped *is* that channel (one room,
  one channel), so opening it lands the caller in the SysOp's channel
  under its own gates. Every existing chat feature works unchanged
  because nothing but the origin marker differs.
- **The `mrc:` prefix is reserved.** No local user or SysOp screen may
  create or rename a channel into it (`netbbs.chat.channels` refuses),
  so a hub `lobby` never collides with a local `lobby` and no local
  channel can impersonate a room.
- **Lifecycle is bounded and SysOp-tuned:** a cap on open rooms (default
  32; opening refuses past it, nothing is evicted), a retention period
  (default 7 days) after which a room with no local participant, no
  activity and no follower is retired by the bridge's sweeper together
  with its scrollback, a blocklist of rooms callers may not open — or
  enter, once blocked — and `[A]dopt`/`Re[t]ire` on the channel
  screen. The sweeper runs on its own task, independent of the hub
  connection and of the switch, so a long outage or switching MRC off
  never strands the cap. It is a node action with no `User` behind it
  and the moderation log requires an actor, so a retirement is reported
  to the MRC diagnostic log and counted on the status screen instead of
  audited; a SysOp's own retire is audited as usual. The gates are
  checked before a room is materialized, so an account they turn away
  cannot spend the cap on rooms it can never enter.
- **Never Link, in either direction.** `link_channel` refuses a
  caller-origin row and the channel screen offers no `[L]ink` for it:
  MRC content is that network's, not this node's, and must never be
  re-broadcast under this node's signature. Inbound, a peer's genesis
  named into the `mrc:` prefix is carried as `local-mrc:...` (a channel
  Linked before the prefix was reserved keeps propagating; a squat
  lands nowhere), one claiming an open room's id is refused outright,
  and a carried message is projected only into a channel with a genesis
  on file. An adopted room keeps its `mrc:` name until the SysOp
  renames it, and Link asks for that rename before signing. An open
  room's id is content-addressed on the
  room *and* a per-node secret, so two nodes opening the same room
  never share an id and no peer can compute one. Adopting a room
  clears the origin and makes it an ordinary mapped channel again,
  which may be Linked like any other. The migration renames any local
  channel that already wore the prefix (it was typeable before this
  release) to `local-mrc:...`.
- **One MRC identity per account, in one room.** The hub knows one
  `nick@site` in one room; a second session of the same account
  entering a different bridged channel is refused naming the room the
  identity already holds. The rule is decided from local occupancy of
  the bridged channels, not from what has been announced to the hub
  (announcements lapse during backoff), and enforced once, atomically,
  at the chat loop's own hub join. The session that is leaving a room
  on `/join` does not count against itself unless another session of
  the same account stays behind. A nick suffix was rejected: it would
  present a second person to the network.
- **Finding rooms.** With MRC and open rooms enabled, Chat's Multi Relay
  Chat section lists retained rooms and discovered network rooms with hub
  user counts and topics. A fresh directory offers `lobby` as a starting
  point, plus opening a room by name. Entering a room requests `LIST` in
  the background, also on reconnect and roster refresh, at most once per
  five minutes per connection without spending callers' message allowance;
  `/rooms` refreshes it
  explicitly and shows a readable listing. The first MRC room of a
  session answers the background listing with one line -- how many more
  rooms there are, and that `/rooms` lists them and `/join <room>` moves
  there -- from the listing already in hand when the five-minute floor
  suppresses the request; never the table. Discovery never opens the
  listed rooms. Local access rules and the blocklist still gate entry.
  Refusals stay in the picker's header through redraws. Open rooms appear
  in this section, not the ordinary channel list. Inside MRC, `/join <name>`
  finds an open MRC room, then a local channel, then opens an MRC room;
  `/join mrc:<name>` opens or finds that MRC room from anywhere.
  `/join <room>` and `/mrc` subcommands and recipients have completion.
- **Directory evidence.** The hub's room-list layout observed during live
  use supplies the parser's anchored rows. Only replies to recent `LIST`
  requests populate the bounded cache; unfamiliar output remains text,
  never guessed room names. Counts and topics are advisory snapshots, with
  stale readings labeled with their age. Reconfiguring the hub clears its
  directory and observed room names, then reloads locally retained
  mappings. Unknown
  generic-client LIST lines stay scoped to explicit requesters and are
  suppressed for automatic requests, never broadcast to other callers.
- **The directory outlives the run (issue #636).** The hub lists rooms
  only to a user who is already in one: `LIST` is a session-context verb
  (MRCDoc protocol page, rev 1.26), and a probe of the development hub on
  2026-09-18 met both a client-context `LIST` and a `LIST` from a nick
  never announced with `NEWROOM` with silence. So the node never asks by
  itself -- no caller-less request, and no service nick parked in a room,
  which would put a fake user on the network from every NetBBS node.
  Instead the last *complete* listing (rows closed by the hub's footer)
  is kept in `node_config`, bound to the hub's host and port, bounded to
  the directory's own cap, every field validated again on load, and
  readings older than thirty days dropped -- room names change slowly, and
  every count is shown with its age; after a restart the section
  shows those rooms at once, each with its real age, until a caller's
  entry refreshes them. A complete listing replaces rather than only
  adds: a room the hub no longer lists has emptied (an MRC room exists
  only while someone is in it) and leaves the directory, unless chatter
  or a caller named it after the listing was asked for. A footer with no
  row before it proves nothing and changes nothing.
- **MRC on, nothing reachable.** Decision 2 stands: switching MRC on
  bridges nothing. With open rooms off and no active mapping, saving the
  settings says that no caller can reach MRC yet and names both ways out.
- **Occupancy.** The status line separates local participants and their
  local away count from the hub's remote roster count. An absent roster
  reads `?`, not zero; stale snapshots are marked. MRC roster replies do
  not supply structured away flags, so NetBBS does not invent a remote
  away count. The status refreshes at most five seconds after roster
  changes even in an otherwise quiet room.

**Issue #304 (presence and welcome)** carries the presence a caller
already has here onto the network and lets the network's own life show
through, without teaching native chat anything about MRC:

- **Away state is mirrored, never separate.** NetBBS's `/away` is the
  one away state; the bridge sends the hub `STATUS AFK <message>` and
  `IAMHERE:AWAY` for every room the caller is announced in, repeats
  them on every announcement (reconnects included) so the hub is never
  behind, and reports `IAMHERE:ACTIVE` on return. Those are the
  documented forms (MRCDoc protocol page, rev 1.26, read 2026-09-09;
  issue #373): there is no `AFK` verb, the away message is 55
  characters, and the spec names no verb that clears AFK -- the hub's
  own activity tracking decides when a returned caller stops showing
  as away.
- **The hub's welcome, once per session.** On a caller's first MRC room
  in a session the bridge shows the hub's `BANNER:` lines as remembered
  from connect and asks for `MOTD` as that caller; the reply reaches
  them alone through the per-caller path of #298. Once per session, not
  per room.
- **The network's size where callers look for company.** The bridge asks
  `STATS` once per roster refresh, parses `bbses rooms users` (the
  Mystic layout; an unparseable reply keeps the raw line for the status
  screen and shows "unknown" elsewhere), and Who's online, the picker's
  MRC section and the bridge status screen show "N users on M boards"
  with the reading's age. The bridge's own ask is parsed, not shown; a
  caller's `/mrc stats` is shown.
- **An open room's topic is the hub's.** Inbound `ROOMTOPIC` for an
  open room is stored on the row (a direct update; the node is not a
  `User`) so the status line shows it; `/topic <text>` inside an open
  room sends `NEWTOPIC` and the hub decides (MRC Trust applies there),
  a bare `/topic` shows the topic rather than clearing it. Inside a
  mapped channel `/topic` keeps its local meaning and audit.
- **Identity commands with masked input, nothing stored.** `/mrc
  register`, `/mrc identify`, `/mrc update password` and `/mrc
  roompass` ask for the secret separately with echo off and send it
  once as the caller's nick; the secret reaches no scrollback, log or
  input history (decision 5 stands: the node stores no credentials that
  are not its own).
- **A personal nick color.** The house-style body's handle color is a
  per-caller choice among the sixteen CGA colors (Profile, default
  yellow), read when the caller is announced; brackets and text color
  stay the house's, and typed pipe codes stay stripped (#298).

**Issue #305 (private messages, opt-in)** amends Decision 3 as the
planning pass intended:

- **One switch, off by default, both directions.** `[P]rofile` →
  `[P]rivate MRC messages` (Communication section). The bridge reads it
  when it announces the caller, alongside the nick color, and forgets
  it with their last announcement; switching it on applies the next
  time they enter an MRC room. Sending requires the same switch:
  starting conversations while refusing replies is not offered.
- **Delivered as a notice, never as chat.** An inbound private line for
  a caller who opted in is an `MrcNotice` of kind `private`, shown as
  `[MRC private] bob@Other: text` with the bell, to that caller's
  sessions only, through the per-caller path of #298 and bounded per
  remote sender ahead of it (a flood from one nick must not evict
  chat). The sender's embedded handle is peeled exactly like a room
  line's and the color codes kept. It is never recorded: no scrollback,
  no search index, no log body.
- **Sent as the caller's nick, in the house style.** `/mrc msg <nick>
  <text>` and `/mrc r <text>` (to whoever last messaged them this
  connection) send a packet with `to_user` set and fields 5 and 6
  empty, the spec's private-message transaction (MRCDoc protocol page,
  rev 1.26; issue #374). Field 5 is `MsgExt`, reserved for extensions
  such as encryption, never routing -- "remove any reference to
  ToBBS" -- and the hub routes on the nick, which `USERNICK` keeps
  unique across boards. The site a nick was last seen at is still
  learned from inbound traffic, bounded, but only for the `nick@site`
  the sender's own echo shows. The body wears the house style so the
  recipient's client shows who wrote it; chunking and the caller's own
  send allowance apply as for a room line. The sender sees a local
  echo, never a channel line.
- **What "private" means, said once.** The first private line a
  session sends or receives comes with one note: private MRC messages
  are not private on that network; the hub and any client can read or
  spoof them. Once per session, whichever direction comes first.
- **Nothing for the SysOp to configure.** The switch is the caller's;
  the node-wide MRC switch and the channel mappings already decide
  whether they are on the network at all.

**Issue #377 (what the hub is told)** follows the protocol page's
control-context verbs, read on 2026-09-09:

- **Per caller, on announcement:** `TERMSIZE` always (the hub formats
  wide replies with it; it says nothing about the person); `USERIP` and
  `BBSMETA` only behind two SysOp switches under Inter-BBS chat (MRC),
  both off. The infrastructure principle applies: the node provides
  the interface, the SysOp owns the decision to hand a third party a
  caller's address or level. The spec's warning that a caller without
  `USERIP` "may get removed from room traffic routing" is stated on the
  switch and in the Handbook rather than used to justify a default.
- **Per connection:** `CAPABILITIES` lists what the bridge really
  handles (`MCI`, `CTCP`, `USERROOM`, `GOODBYE`, `SSL` when on) and
  carries the bridge module's SHA256 in the spec's hash field;
  `IMALIVE` carries the process id and a timestamp the hub echoes in
  `PONG`, from which the status screen shows the round trip.
- The `INFO*` fields the issue asked for already existed (issue #275).

**Issue #378 (smaller spec follow-ups)**, same reading:

- A server `NOTIFY:` is a one-time notice shown in every bridged
  channel like a banner and never remembered; the `STATS` reply's
  fourth field is the hub's activity level, shown beside the network
  size; handles are displayed with underscores as spaces, as the spec
  asks, while matching and addressing keep the wire spelling.
- The hub is moving its identity verbs to `!helper` chat text. Typed
  as chat, `!identify secret` would be recorded in scrollback and,
  the moment the channel is bridged, relayed to the room, so those
  four helpers are refused as chat in every channel while the node
  has an MRC bridge -- a paused mapping or a local channel is no
  safer a place for a password -- with a pointer to the masked `/mrc`
  forms, and the line is dropped from the input history. Nothing else
  typed with a `!` is touched: the hub's own helpers (`!time`,
  `!weather`) are ordinary chat.
- `STATUS LASTSEEN OFF` is a caller's choice on the Profile (on by
  default, the hub's default), sent on every announcement like the
  away state.
- A CTCP request whose target is `*` (blank `to_user`) is not answered:
  every wildcard would cost one reply per announced nick.
- Hub facts worth keeping: the handshake must arrive within one second
  of connecting, the heartbeat times out at 125 s, a client may hold
  at most eight connections per address, and the operator's public
  pool is `na-multi`, `eu-multi` and `au-multi.relaychat.net` (5000
  plain, 5001 TLS) with `mrcdev.relaychat.net` for development; the
  default host stays what it was until the operator retires it.

### Issue #194 — trusted scrollback-on-join — closed

**Goal:** decide whether/how a node gets recent scrollback the instant it
live-subscribes to a linked channel. Today it gets presence plus
messages going forward only (§8.10.2's own "does not offer shared recent
scrollback"), a deliberate v1 scope cut, not an oversight — but a real
gap against this feature's own "frictionless" bar.

**Not a permanent-loss problem, and not shaped like issue #168 at all.**
Every channel message on a linked channel is already a signed, durable
`channel_message` event (`queue_channel_message_if_linked`), delivered to
every linked node eventually through the existing inventory/pull-based
catch-up path (issue #85) regardless of live-connection status.
`netbbs.link.sync`'s scheduling loop runs every `sync_interval_seconds`
(five minutes by default), so today a freshly live-subscribed channel
can sit silent for up to that long before the *existing* async path
fills in what was missed. This decision only shrinks that window at
subscribe time — it never touches durability. It also carries none of
issue #168's relay/crypto-fork complexity: `ensure_live_subscription`
(`netbbs.link.realtime_channels`) already always dials the channel's
*origin* node specifically (`channel_origin_fingerprint`), never a third
node, so there is exactly one source of truth to ask, not a multi-hop
question.

**Decision 1 (locked in) — source is the origin's own local scrollback,
sent as a new frame alongside the existing subscribe-time
`presence_snapshot`.** `_handle_subscribe` already sends a channel's live
presence roster the moment a peer subscribes (§8.10.2); a
`scrollback_snapshot` frame is a sibling addition at the identical call
site, sourced from `netbbs.chat.scrollback.get_scrollback` — the same
already-bounded, origin-policy-filtered function the local UI renders
from. A subscriber accepts the frame only from the channel's current
authenticated origin and only when its `request_id` matches a still-pending
subscribe attempt; unsolicited or late snapshots cannot fill a later join.

**Decision 2 (locked in) — bounded the same way every other snapshot
already is, and rendered once, never durably stored on the subscribing
side.** A received `scrollback_snapshot` is ephemeral and render-only —
exactly the same "Live channel messages are ephemeral node-attested
assertions" principle §8.10.2 already states for ordinary live messages.
Writing it into the subscriber's own `channel_messages` was considered
and rejected: the same content already arrives durably, independently,
through the existing async materialization path, and persisting both
would risk duplicate or conflicting rows for one message with no natural
dedup key across the two paths. Snapshot entries therefore carry the signed
event's existing content ID when one exists, letting the join flow suppress
anything already rendered from local materialized history. A caller sees
only the remaining gap once at subscribe time; the durable copy still lands
separately, on its own schedule, as it already does today.

**Decision 3 (locked in) — issue #164's author-trust rule is enforced at
both nodes.** The origin's `get_scrollback` applies its own policy first.
Each entry also carries the author identity the origin derived from the
accepted signed event (or the origin plus local user ID for origin-local
content), so the subscriber independently suppresses authors it marks
`BLOCKED` or `QUARANTINED`. Origin-local display labels are qualified with
the authenticated origin; they must never resolve as same-named local
accounts on the subscriber.

The revised snapshot attribution contract is real-time protocol v2. The Noise
identity payload declares that application version, and incompatible peers are
rejected during the authenticated handshake before either side advertises a
usable live session. The join flow reports this as an explicit upgrade
requirement rather than folding it into generic transient unavailability.
Frame versions remain an inner defensive boundary.
The subscriber reconstructs every authored display label from the attested
`user@node` identity rather than trusting the wire label. Moderation entries
retain their target label for rendering but are authorless system events, so a
target's trust state cannot suppress audit history the target did not author.

`chat/scrollback.py`'s own module docstring — "the separate, harder
question of a newly-joined Link node needing catch-up scrollback from
peers... stays explicitly deferred to whenever Phase 5 starts" — is
updated alongside this decision: Phase 5 has been active for a while now,
and this issue is that decision, not a further deferral of it.

### Issue #200 — War Dialer: async multiplayer BBS-crew door game — closed

**War Dialer is vendored, and its design lives with it.** The game is developed
in its own repository, Thiesi/WarDialer. Its rules, Rank, seasons, economy,
crews, world schema, administration and presentation contract are specified in
that repository's `docs/design.md`, which carries this section's former text
(issues #1223, #1228). NetBBS ships one tagged release of its single file,
unchanged: `scripts/vendor_door.py war_dialer <tag>` fetches it and records its
origin in `src/netbbs/doors/bundled/VENDORED.json`, and the test suite refuses
a copy that differs from that record. A fix to the game goes upstream and
arrives with the next vendored release.

NetBBS owns how it hosts the door, as below. Its backup and SysOp console
reach into the door through five names: `inspect_world`, `world_session`,
`administer_world`, `WorldStateError` and `_event_plain`. A release that
renamed them would break NetBBS's backups and console rather than the game, so
NetBBS's suite pins them.

**World paths (issue #362, slice 4).** The native runtime supplies the bundled
War Dialer with `<resolved-node-db-filename>.doors/war-dialer.db` beside that node
database. This derives from the persistent database locator, never mutable node
or door display names. Distinct database paths get distinct defaults; renaming or
relocating the node database requires moving its companion world or an explicit
override. Standalone execution keeps the legacy home-directory default.

A profile's `WAR_DIALER_DB_PATH` overrides the process setting, which overrides
the node default. Overrides expand to absolute paths before entering the door's
temporary/installation cwd. Only the bundled entrypoint/module or an explicit
profile override receives this setting; the full parent environment stays private.
SysOps can inspect the effective path through Compatibility setup / Check setup.

When the node default is absent and a legacy home-directory world exists, launch
requires explicit migration or override rather than silently replacing player data.
Migration is manual, with stopped sessions, a SQLite-consistent backup and verified
node-local user-ID ownership. An explicit override is an operator choice and must
not point independent nodes at the same world.

**World ownership and node backup.** First host launch binds the world to an opaque
random namespace retained in the node database as `war_dialer_owner` and forwarded
only in War Dialer metadata. Subsequent launches must match it; a bound world
cannot be entered as a standalone Guest. Legacy adoption requires the documented
manual user-ID check. A standalone launch outside NetBBS owns its world itself
(§16, issue #1223). Backups preserve the node namespace with all discovered
existing worlds: the node-default companion and registered profile/process
overrides, deduplicated by resolved path. A world component contains
SQLite-consistent snapshots, supported schema versions and checksums, with a
limit of 64 worlds and 512 MiB per world.

Backup rejects active game sessions; restore requires stopped node services,
explicit destinations for every archived world and the paired node database.
Read-only verification checks schema, integrity and namespace ownership. World
staging/rollback stays on each destination filesystem; old WAL/SHM/journal files
participate in the switch and rollback. The door's session guard holds shared
read leases during play and an exclusive lease during maintenance; backup and
restore take it through `world_session`. Restore retains the prior generation
and journals unresolved failures. Service/profile path activation and
old-binary exclusion are manual SysOp actions, as documented in the door guide.

**SysOp competition controls.** The door administers its world itself
(`--admin`, issue #1227); NetBBS wraps it. The SysOp console (a War Dialer
door's **[W]orld** screen) and `python -m netbbs.doors.war_dialer_admin` provide
read-only status, persistent maintenance and confirmed season/reset operations
through the door's `administer_world`. NetBBS adds the host's checks: the world
must be one of the node's discovered worlds and owned by its namespace, and a
destructive control requires maintenance on, an exact-filename confirmation, a
reason, and a freshly created and verified complete node backup. The CLI
additionally requires a stopped node; the console does not (issue #726, the
maintainer's decision), since maintenance already keeps callers out, the
world's session guard refuses anyone still inside, and the backup is the same
live backup the console's Backup screen takes. The console needs a live node
for the identity directory that backup captures, and records the SysOp account
in the world's audit and NetBBS's audit log. Maintenance stays on until the
SysOp explicitly reopens the world.

Host metadata is either absent (a standalone launch; the world is then
self-owned, see §16, issue #1223) or valid.
Unreadable, oversized, malformed or invalid identity/dimension metadata is refused
before world creation; it never falls back to Guest. Caller messages remain clear
and SysOp diagnostics are bounded. Commands do not activate or redeploy services.

### Issue #168 — real-time relay for Link direct chat

**Goal:** decide between the two structurally different designs the issue
itself poses for live (Noise XX) relay between two mutually-unreachable
(outgoing-only-to-each-other) nodes: double-hop relay-as-participant
(the relay terminates one Noise session per leg and re-encrypts between
them) versus a raw-socket/TCP-level proxy below the Noise layer (the
relay blindly forwards bytes, never touching the handshake or its keys).
Not a newly-discovered gap — the design doc already named this as a
deferred "separate future protocol" (§8.10); this issue is that design
pass.

**Context that made this tractable now, not a decision in itself:** a
second, related idea is in discussion — turning ReLink (the project's
own persistent, internet-reachable test node) into a stable, always-up
default relay so a home SysOp who can't or won't expose a port has a
frictionless path onto the mesh, without that being a hard dependency
(any other willing peer, or a commercial provider for the parallel
managed-DNS idea under #201, works exactly as well — nothing forces
ReLink specifically). That product/infrastructure question is real but
still early and not decided here. What it *does* settle is the async-
relay model's own objection to the raw-proxy design: `relay_selection.py`'s
reliability-ranking machinery exists to route around relays that might
disappear, and a relay explicitly committed to staying up removes the
need to solve general reliability-ranked live-relay discovery before a
v1 can ship. The protocol decision below stands on its own regardless of
who ends up operating such a relay.

**Decision 1 (locked in) — raw-socket/TCP-level proxy, not double-hop.**
The deciding factor: raw-proxy requires **zero changes** to the already-
shipped `LinkRealtimeSession`/Noise XX handshake code. Two directly-
handshaked endpoints run the exact same mutual authentication they'd run
if actually adjacent; the relay is as invisible to Noise as any ordinary
router hop, since it never participates in the Diffie-Hellman exchange
and structurally cannot decrypt anything. That confines all new code to
connection setup (a small rendezvous exchange — "I want to reach
fingerprint X" / "I'm X, waiting"), not the confidentiality-critical
path itself, and it is a well-understood pattern elsewhere (a TURN
server, an SSH jump host, Tailscale's DERP relays), not a novel design.

A real alternative was considered and rejected for now, not dismissed:
double-hop can be built as a *hybrid* where the relay stays a genuine
protocol participant for control-plane frames (subscribe/presence/ping)
it's fine to see, while chat-content frames carry their own additional
encryption hop the relay can't read — giving real per-frame-type abuse
mitigation (raw-proxy can only see bytes/timing, never structure) on top
of content confidentiality. Rejected for v1 because it's solving a
structural-abuse-mitigation problem with no evidence yet that it's
needed, at real cryptographic-design cost paid up front: a second key-
exchange scheme layered inside the double-hop transport, a new session
shape (today's `LinkRealtimeSession` assumes exactly one remote
fingerprint, not a triangulated A-relay-B relationship), and a new frame
family. If frame-level abuse mitigation becomes a real operational
problem later, the hybrid design is the documented answer to revisit —
not re-derived from scratch.

**Trade-off stated plainly:** raw-proxy does not get either design to
zero metadata exposure — the relay still learns which two fingerprints
talked, for how long, and roughly how much traffic, same as the hybrid
double-hop's control-plane visibility would show. Raw-proxy also doesn't
plug into `relay_selection.py`'s existing reliability-ranking/consent
model at all — it's a structurally different kind of "relay" than the
async store-and-forward one, sharing a name but no code or selection
mechanism. If a fully decentralized *marketplace* of live relays (not
one well-known anchor) is ever wanted, that discovery/ranking layer
would need to be built fresh for this model rather than reusing the
async one — an accepted, deferred cost, not an oversight.

**Decision 2 (locked in) — bounded resource limits for live relay.** Modeled
on, but distinct from, the two existing bound families this touches: each
leg of a bridge is an ordinary `LinkRealtimeSession` (`netbbs.link.
transport`), already governed by its own `REALTIME_DEFAULT_*` bounds
(64-frame outbound queue, 100 frames per 10s window, 45s heartbeat lease,
5 protocol strikes); the async relay mailbox (`relay_mailbox.py`) already
bounds *its* resource at 50 envelopes per recipient. Neither transfers
as-is, because raw-proxy's actual cost shape is different from both:

- **Concurrent bridged-pair limit.** A relay running one bridge is a real
  third participant holding *two* live Noise sessions (one to each chat
  party) for that pair's entire conversation — meaningfully more
  standing cost per pair than either a single direct session or an
  at-rest mailbox entry. A new node-level `max_concurrent_relayed_pairs`
  limit (SysOp-configurable, same `nodeconfig.py` dataclass-field-plus-
  validation shape `ShutdownConfig` already uses) caps this directly,
  independent of the per-session frame-rate bound above, which caps a
  *single* session's chattiness, not how many a relay carries at once.
- **Per-pair byte-rate bound, not a frame-rate one.** `max_frames_per_
  window` counts discrete parsed frames — meaningless for raw-proxied
  bytes, which the relay by design never frames or parses at all. A
  bridge needs a bytes/second ceiling instead; exceeding it closes the
  bridge, the same "drop rather than silently degrade" precedent
  `LinkRealtimeSession.send()`'s existing slow-consumer handling already
  sets for a full outbound queue.
- **Idle-bridge timeout is protocol-agnostic, unlike the existing
  heartbeat lease.** `LinkRealtimeSession`'s own dead-peer detection
  inspects real ping/pong frames — raw-proxy structurally cannot do
  that, since the relay never sees frame semantics, only opaque
  ciphertext bytes. A bridge instead needs a dumb "zero bytes observed
  in either direction for N seconds" timer: the two actual endpoints'
  own (relay-invisible, encrypted) heartbeat traffic keeps a genuinely
  live bridge from ever tripping it, with no frame-aware logic required
  on the relay's side at all.
- **Bounded, timed-out pending-rendezvous table.** A node that shows up
  first, before its counterpart, waits — bounded in count (a cap on
  simultaneous pending requests per relay, the same "bound remotely
  influenced resources" principle `MAX_MAILBOX_ENVELOPES_PER_RECIPIENT`
  already applies to the async mailbox) and in time (a lone request
  that waits past its own timeout expires and is reported back to the
  requester as an explicit failure — CLAUDE.md's "fail clearly," not a
  request silently forgotten).
- **The existing slow-consumer-drops-the-session behavior composes
  across two hops for free, unneeding any new logic of its own**: each
  leg is its own ordinary `LinkRealtimeSession` with its own existing
  bound: a slow leg drops only that one session exactly as today, and
  the relay tears down the other leg in response (a bridge with only
  one live end isn't a bridge) — no bespoke two-hop-aware queue ever
  needs writing.

**Decision 3 (locked in) — v1 fallback UX for two mutually-unreachable
nodes.** Extends this project's own already-shipped local convention
rather than inventing a new one: `netbbs.net.chat_flow`'s `/msg`/
`/private` already require the recipient currently online, refusing
plainly ("X is not currently online.") when they aren't, with local mail
as the standing async alternative. The cross-node case gets the identical
shape: attempting live Link direct chat with a peer whose node can't
currently be bridged (no relay reachable, the relay's own pending-
rendezvous request times out, or the concurrent-pair cap above is full)
produces an explicit, never-silent refusal naming the situation plainly
(e.g. "<user> can't be reached for live chat right now.") and points at
Link mail (`link_message`/`relay_mailbox.py`, the *already-shipped* async
cross-node messaging path — not a new mechanism) as the immediate
alternative. The caller-facing message deliberately does not distinguish
*which* of the possible reasons applied — offline peer, no relay, relay
at capacity, rendezvous timeout — mirroring this project's existing
stance elsewhere (design doc §12) that such operational detail about a
*remote* node is not something a caller needs and could leak more than
intended about the other side's situation.

**Decision 4 (locked in) — rendezvous frame shape.** New frame types
added to the existing `REALTIME_FRAME_TYPES` set (`netbbs.link.
protocol`), not a new protocol version or frame family: extending that
frozenset without bumping `REALTIME_PROTOCOL_VERSION` is already this
file's own established pattern (e.g. the presence frames joined the
original subscribe/channel_message set the same way), and an old peer
encountering an unrecognized new type already fails cleanly via the
existing "unsupported real-time frame type" rejection — no separate
negotiation needed. Rides over the *requesting* node's own already-
authenticated `LinkRealtimeSession` to the relay (the ordinary session
that already exists from `relaying_for`/relay-consent setup) — no new
authentication mechanism, matching raw-proxy's own core premise of
confining new code to connection setup, never the confidentiality-
critical path:

- `relay_request` `{target_fingerprint}` — sent by either party wanting
  to reach the other through this relay.
- `relay_waiting` — the relay's reply when only this side has shown up
  so far (bounded by the pending-rendezvous timeout, Decision 2).
- `relay_ready` — sent to *both* sides once the counterpart has also
  shown up; raw-proxy byte-pumping between the two begins immediately
  after.
- `relay_reject` `{reason}` — the relay declines outright (pending-table
  full, no `relaying_for` relationship covering this pair, concurrent-
  pair cap reached) — explicit, matching Decision 3's fail-clearly
  requirement, never a silent drop.

This closes every acceptance criterion issue #168 named.

**Implemented** (§8.10.3 is the normative description): `netbbs.link.
realtime_relay` (relay server half and party half), `netbbs.link.
realtime_direct` (session establishment order, direct messages, the
reliable-node anchor connectors), `netbbs.net.link_direct` (the `/msg
user@node` flow and the receiving-side deliverer), five `[link]
live_relay_*` bounds with the defaults §8.10.3 lists, and a `relay_ready`
payload that names the relay's attach address so a party never has to
remember which address it reached the relay at. Two implementation
choices beyond the four decisions: the invitation reuses the
`relay_request` shape with the invitee's own fingerprint as target
(no fifth frame type), and a bridge is only ever offered between two
nodes both currently connected to the relay -- consent is the standing
session, so the asynchronous `relaying_for` model is not consulted.

### Issue #201 — managed netbbs.org subdomain + dynamic DNS — closed

**Goal:** since the project controls the `netbbs.org` domain, offer SysOps
an easy way to publish their board under it (e.g. `myboard.netbbs.org`)
and, for boards on residential/dynamic IPs, keep that record pointed at
the node's current address without manual DNS maintenance. Two
components, not one: a one-time subdomain *registration* (name
reservation + initial DNS record), and a recurring *updater* that keeps
the node in contact with the service and, for a board that asks for it,
pushes a record update when the node's public address changes. A board
on a static IP still wants the friendly subdomain without the address
tracking, and that is the `dynamic` choice at registration; what it
cannot opt out of is the contact itself, which is the liveness signal
every managed name owes the service (Decision 10 — this paragraph used
to call the updater optional, and issue #600 found the code had never
agreed).

**Decision 1 (locked in) — offered via a prominent prompt on first-SysOp
bootstrap or first authenticated SysOp login, not a silent default and
not a toggle a SysOp has to go discover.** Two different concerns were
in tension here and both are real: a bare opt-in-only design loses most
of the feature's actual value (the whole pitch is removing first-run
friction — a setting nobody discovers might as well not exist), but a
silent default also isn't right, because this makes the node contact and
register public presence with project infrastructure before the SysOp
has decided whether they want that at all — a private/test node would
get unexpectedly enrolled. This is the same *kind* of decision as Link
participation itself, which already isn't automatic on a fresh node
(seeds must be configured before a node reaches out and joins the mesh)
— for internal consistency, "does my node touch external infrastructure
and become discoverable" should stay an explicit choice here too, just
asked at the moment it matters instead of requiring discovery later.
Code review follow-up (PR #218): a supported persistent deployment
bootstraps its first SysOp via `netbbs.admin` and then runs headlessly
under systemd/rc.d — a literal "first daemon run" prompt has no
interactive input channel at that point and would either block startup
or silently be skipped. The prompt is anchored to an existing
interactive surface instead — first-SysOp bootstrap, or that SysOp's
first authenticated login if bootstrap itself stays a non-interactive
CLI invocation — with the accept/decline answer persisted so it is
asked exactly once, not on every subsequent login.

*Default flipped by issue #219 Decision 7:* on the shared first-run screen the
prompt's bare-Enter default is now accept (both first-run choices are pre-set
to accept so accepting everything is two keystrokes); an explicit "n" still
declines, and the decision is recorded once either way.

*The accept and the registration are separable in time (issue #634).*
Accepting continues straight into choosing a name, because an opt-in
that gets the SysOp nothing without a second trip through the console
loses the friction argument above. But the earlier of the two anchors —
first-SysOp bootstrap in `netbbs.admin` — runs before the node has ever
started, and the fingerprint the service knows a node by is cached by
the node's own startup; `netbbs.admin` is given a database path, not the
node's configuration, so it cannot load or create the identity itself
without risking a second one in the wrong directory. On such a node the
accept is recorded as always, the SysOp is told the name is picked once
the node has started, and the registration is *owed*: the next
interactive surface that can register — a SysOp's authenticated login,
or `netbbs.admin` run again, which is the only one a headless deployment
ever uses — opens the name editor, once, exactly as the prompt would
have. Backing out of it is a final answer, like declining the prompt;
registering by any route settles it; the DNS screen's `[R]egister` is
the same editor from then on.

**Decision 2 (locked in) — the managed-service credential is a separate,
auto-generated, per-registration secret, not the node's own Ed25519 key.**
Both the self-hosted path (SysOp supplies their own dynamic-DNS
provider's credentials, no design question) and the managed path stay
available, not exclusive. For the managed path specifically: reusing the
node's existing Ed25519 key (the Link protocol trust root — Noise XX
handshake identity, canonical event signing) was considered and
rejected. The convenience argument for reuse doesn't actually hold — a
minted credential, generated at registration and stored transparently by
the node, gives the identical zero-manual-handling experience reuse
would. What reuse *would* cost for no real gain: blast-radius coupling
(a compromised node key would also hijack the SysOp's public DNS name,
not just Link identity) and coupling any future node-key rotation/
recovery work to also remember to propagate to DNS. A separate
credential keeps the two systems', and their compromise/recovery
stories, fully independent.

**Decision 3 (locked in) — name governance is first-come-first-served
plus a reserved-word blocklist and a one-name-per-node cap; a registered
name only actually goes live once the node has maintained a minimum age
of successful contact with the registration service; new registrations
service-wide are rate-limited, rejected outright once that rate is
exceeded; and total active managed registrations are capped by a
separate cumulative ceiling, refused once reached — no preventive
identity vetting, and no human review queue, beyond that.** Matches how the project
treats registration/content elsewhere (SysOp owns the trust decision,
best-effort not gatekept) — requiring identity *verification* before
registering a subdomain would add real friction against the feature's
own point, for a comparatively low-stakes resource. The blocklist covers
only the obvious cases (the project's own names, trademarks, slurs) —
not a general dispute-avoidance mechanism.

Code review follow-up (PR #218): first-come-first-served plus a
blocklist bounds *which* names can be taken but not *how many* —
without a cap, one node could hold indefinitely many names, consuming
DNS-provider records/cost and squatting desirable ones. A first fix
attempt (a bare cap of one name per node/registration-credential) was
itself found insufficient on further Codex review (PR #221): a node
identity and a registration credential (Decision 2) are both free for a
remote client to mint — an attacker can generate a fresh node identity
per desired hostname and hold all of them simultaneously, each
individually satisfying a "one-per-node" cap.

**A second fix attempt kept the one-per-node cap and added a minimum-age
gate** on when a reservation actually becomes a live DNS record,
deliberately decoupled from Decision 1's first-run opt-in: accepting the
offer at first run (or first SysOp login) still costs nothing and
happens immediately, but the node's registration *intent* is recorded,
not yet published, until the node has maintained a minimum period of
successful contact with the registration service. **This was itself
found insufficient on yet another Codex review, same PR (#223):** the
gate only costs an attacker *wall-clock time*, not *effort per
identity* — a single process can run a trivial heartbeat for thousands
of fake node identities in parallel, all maturing simultaneously, at
essentially zero marginal cost per additional name. The gate delays the
attack once; it doesn't bound how many names come out the other end of
that delay, so the DNS-provider cost/squatting problem this decision
exists to prevent recurs in full once the qualifying period passes.

**The age-gate is kept anyway, as a mild friction layer, but the actual
bound is a separate, service-enforced admission control layered on
top: a rate limit on new registrations across the whole managed
service (not per node/identity — the thing an attacker cannot multiply
by minting more identities), automatic up to that rate, with anything
beyond it originally specified as queued for the project maintainer to
review** — the same "a human reviews it" shape Decision 4 already uses
for contested-name disputes, extended from content disputes to
registration *volume*. This is what actually closes the gap:
DNS-provider record cost and squatting are bounded by how fast the
*service* will create new records at all, independent of how many
identities a single attacker can mint and age in parallel.

Two further Codex findings, same PR (#225), both about resources a
review queue would itself introduce or leave unbounded, not about the
rate limit's own logic: the queue would need its own explicit-capacity
bound (itself a remotely-influenced resource a Sybil attacker submitting
past the rate limit could otherwise fill without limit); and separately,
a rate limit alone bounds *speed*, not *total count* — an attacker
patient enough to always submit exactly at (never over) the threshold,
keeping every registration's contact alive indefinitely so nothing
qualifies as abandoned, could still accumulate an unbounded number of
active records over a long enough time. The second finding needed its
own fix regardless of the queue question: a separate cumulative cap on
total *active* managed registrations service-wide, refused (not queued)
once reached, freed only as existing registrations are voluntarily
released or genuinely abandoned (Decision 5) — the rate limit alone
never stands in for a real total ceiling.

**The review queue itself was dropped entirely during implementation
planning, not carried forward as a bounded-capacity queue.** Once a
request is queued at all, the realistic resolution is the same either
way: the maintainer hears the SysOp's explanation and decides by hand.
A capacity-bounded queue adds real code and a genuine single point of
(human) failure without actually simplifying that manual conversation —
the same outcome is reached faster, with less to maintain, by simply
rejecting outright once the rate limit or the cumulative cap is
exceeded, symmetric with each other, and telling the caller plainly
that the service is at capacity with a contact channel for the rare
legitimate exception. Both are hard-reject and service-wide. A reclaim
(Decision 5) bypasses the *rate* limiter because it is not a new
registration, but it must still fit the cumulative active-registration
ceiling and the one-active-name-per-node limit: reactivation consumes a
real active slot, and release/reclaim cycling must not create more live
rows than either bound permits.

*Issue #598:* for three releases neither refusal carried a channel, and
the cumulative-cap refusal said "try again later", which is wrong for
that cap specifically — the rate limiter refills, but the ceiling frees a
slot only when some other registration is released or swept as
abandoned, so a SysOp told to wait retries against a wall instead of
getting in touch. The channel is the operator's own setting
(`MANAGED_DNS_CONTACT`, `services/managed_dns/README.md` §4), not a
project address baked into a service anyone may self-host; an instance
without one says so in words. The cap refusal now says what frees a
slot and that retrying will not; the rate-limit refusal keeps "try again
shortly", which is true. The node shows the service's sentence, not the
JSON it arrived in.

*Two things this decision states as flat rules that the rename workflow
(Decision 9) deliberately bends (issue #602).* The one-name-per-node cap
is one **except across a rename**, where a node holds two active rows —
the live name and its maturing replacement — until the replacement
matures; make-before-break requires exactly that, and it is bounded at
two (the node refuses a second rename while one is pending, and the
service treats a retry against an existing `replaces_name` link as
recovery of the same replacement, never a third row). And a rename
**spends a rate-limit token** where a reclaim does not: a reclaim
reactivates a row and a record that already exist, whereas a rename
reserves a new name and will create a new DNS record, which is precisely
the cost the limiter exists to bound. Both were accidents of
implementation until written here; both are now the rule.

**Decision 4 (locked in) — contested-name disputes are manual and
complaint-driven, stated as such, not implied automation; the operator's
end of that process is a revocation which takes the name away from the
registrant who held it, and nothing more.** At this project's current
scale, there is no realistic alternative to a human (the project
maintainer) reviewing a reported impersonation/abuse claim and revoking
if warranted. Documented explicitly so this isn't mistaken for a more
automated process than actually exists.

Issue #599: for three releases that was the whole of it. The decision
said a human revokes, and nothing existed for a human to revoke *with* —
no channel a complaint could arrive through, no endpoint or command, and
no runbook step. Doing it by hand meant editing the service's SQLite and
deleting the record out of BIND separately, with nothing to stop the
holder's node republishing on its next heartbeat. A decision whose
enforcement is entirely manual still has to say what the manual act is.

**The act is `POST /admin/revoke`, on the running service, gated by a
bearer token that is unset by default.** Inside the service rather than
a separate CLI because it must share the same transition lane as the
heartbeat, the sweep and every SysOp-driven transition — a second
process editing the same database could commit from a snapshot the
sweep had already moved past. Unset by default because a public-facing
service should not carry an administrative route that merely hopes
nobody finds it; an instance whose operator has not configured a token
has no administrative surface at all, and the refusal is identical for
"no token configured", "wrong token" and "no header" so the response
cannot be used to learn whether this instance has one.

**A revoked name is a fourth terminal status, not a reuse of
`released`.** The difference that matters is reclaim: a released or
abandoned row is deliberately reclaimable by the credential that held
it, for the length of the cooldown, which is Decision 5's whole point.
Applied to a takedown that same rule would undo it — the registrant's
node still holds the credential, and its registration draft prefills the
name it just lost, so a reclaim is one keystroke. A revoked row is
reclaimable by nothing.

**It expires on the same cooldown as the other two exits.** Revocation
blocks the registrant who was taken down, not the name forever: once
`released_at` ages past the shared cooldown the name is available to a
genuinely new registrant, including in principle the person it was taken
from. Permanent retirement was considered and rejected for the same
reason Decision 5 rejected it for voluntary release — an ever-growing
set of names retired for the life of the project — and because a
mechanism for names nobody may ever hold already exists: the
`blocklist` module's `RESERVED_NAMES`, curated by hand and changed by a
code change, which is where a permanently-barred name belongs.

**Revoking one half of a rename takes both halves.** A rename is the one
state in which a single registrant holds two names (see Decision 3's own
note on the per-node cap), so revoking the live name and leaving the
replacement to mature would hand the taken-down registrant a working
name. The voluntary release path refuses in this state and tells the
SysOp to cancel the rename first; an operator acting on a complaint has
nobody to ask, so revocation takes both rather than refusing.

**Only the rows that are still this registrant's, and still the rows
that were checked.** A replacement can outlive the name it replaced, so
the same node-fingerprint check rename completion and cancellation apply
before mutating anything through a `replaces_name` link applies here
too: a name whose cooldown elapsed and was reissued to a different node
is never taken by a complaint about the node that used to hold it. And
because a fresh registration does not pass through the transition lane
that a revocation holds, each row is re-read after the provider awaits
and written only if its credential hash is unchanged — proof it is still
the registration that was checked, rather than one that claimed the name
in between.

**Publication is undone before the rows move, and a provider failure
revokes nothing** — the same rule voluntary release already follows.
A takedown recorded in the database while the record is still resolving
would be worse than no takedown, because it looks finished. Only a row
which can still hold a published record is worth a provider call at all:
release and abandonment leave `last_known_address` behind on a row whose
record they already deleted, and failing a revocation on a deletion that
had nothing to delete would leave the credential reclaimable during
exactly the provider outage an operator cannot wait out.

**The registrant is told that, and where to write — not why.** The
first cut of this left the taken-down SysOp with a 401 the node read as
`abandoned`, and a `[R]egister` refusal that spoke of a cooldown: their
board went dark under a badge that blamed their own uptime. The service's
uniform "unknown or inactive registration" exists so that a caller
presenting a stale or invented credential learns nothing; the credential
that *held* a revoked name proves the caller is the one person the fact
belongs to, and the only thing the answer reveals is their own
registration's state. So every credential-bearing route — heartbeat,
release, rename, cancellation, and a reclaim by either path — answers a
revoked credential with `status: revoked` and the operator's contact
channel (Decision 3's `MANAGED_DNS_CONTACT`), and the node adopts that as
a terminal state of its own: the updater stops, the DNS screen shows
REVOKED with the channel, and `[R]egister` with a different name works as
usual. The reason stays the operator's. Anyone else asking for the name
still gets the cooldown refusal and learns nothing.

**The operator acts from inside NetBBS, not from a shell on the service
host.** The node whose operator also runs the service carries
`[managed_dns] admin_token` in its `netbbs.toml` — config file only,
never a command-line flag, since a secret in `argv` is visible to every
process on the host — mirrored into the node database at startup like
`service_url`, absence included. Its presence is what makes the Managed
DNS status screen offer `[A]dminister service`: the service's whole
table (`POST /admin/registrations`, same token, same uniform refusal,
every row but its credential hash, inactive rows included because a
name inside its cooldown can still be the subject of a complaint), one
registration in full — when it was registered, whose node, when it last
checked in, what it publishes, whether a rename is in flight, the reason
if already revoked — and `[R]evoke` behind a required reason and a
type-the-name confirmation, the same shape every delete in the console
uses. That is README §8's checklist made executable, minus the `dig`,
which stays the operator's. The reason prompt before the type-the-name
confirmation is a deliberate exception to §3.5's one-value rule: the
reason is the audit record the runbook requires, and a revocation is an
action against somebody else's board.

**Reports arrive through the project's issue tracker**, which is a
public channel and a real trade-off: an impersonation complaint tends to
name the impersonated party. The alternative considered was a dedicated
address, which is better suited to the content but is infrastructure
this project does not otherwise run, and an unmonitored one would be
worse than the tracker. Nothing in the mechanism assumes a report
arrived there. `services/managed_dns/README.md` §8 carries the
operational half: what to check before revoking, what separates abuse
from an ordinary naming dispute this decision deliberately does not
cover, the request itself, and what the former holder sees afterwards.

**Decision 5 (locked in) — every exit path shares one deliberately
generous cooldown before a name becomes assignable to a *different*
registrant; this is an accepted, bounded residual risk, not a solved
one.** There are four ways a name stops being live, not the two this
decision originally named (issue #601): voluntary release, abandonment
by the sweep, completion of a rename (the old name is released the
moment the replacement's record is published — Decision 9), and
operator revocation (Decision 4). All four set the same `released_at`
and start the same timer; the first three stay reclaimable by the
credential that held the name for as long as it runs, revocation by
nothing. A SysOp planning a rename should read that as: the old name is
not free the moment the new one goes live, and not re-registerable by
them either while they hold their one name under the new label. §8.10 states that
"the remote node label, endpoint, DNS name, and TCP address are never
identity authority" — real *Link* node identity is verified by the
Noise XX handshake against the Ed25519-derived key, independent of how a
connection was dialed, so a reassigned DNS name cannot impersonate a
node at the Link protocol level; that fact is why Decision 4's manual,
complaint-driven dispute process can stay lightweight for
*impersonation* claims specifically. It does not make reassignment safe
in general: ordinary Telnet and plain-HTTP callers are never protected
by that handshake, carry plaintext passwords, and a caller who still has
the old hostname bookmarked after reassignment can have credentials
harvested by whoever holds the name now — even an HTTPS caller can be
handed a convincing fake board once the new registrant obtains a
legitimate certificate for the name.

A Codex review (PR #221) correctly pointed out that a finite cooldown
only *delays* this exposure, it doesn't bound it to zero — a caller can
in principle hold a bookmark longer than any cooldown. That observation
is true but was weighed against the wrong bar: zero residual risk isn't
the standard any real identifier-reassignment system actually meets.
Domain registries drop-catch expired names — commonly with *no* grace
period at all — and telecom carriers recycle phone numbers after a
dormancy window (typically 90 days to a couple of years), both fully
aware that a returning party can be phished by whoever holds the
identifier now; neither treats "never reassign" as the answer. A
permanent-retirement design was tried in this entry and reverted:
correct in principle, but strictly more conservative than the
registries and carriers this project is directly comparable to, and it
trades a security property nobody else in this space provides for an
unbounded, ever-growing cost (a permanently-retired name is retired for
the life of the project, not just until interest fades). **The bounded-
cooldown design is kept, deliberately set longer than commercial
practice needs to be** (on the order of 90 days, well past a typical
registrar's ~30–45-day redemption window) **since NetBBS has no
commercial pressure to recycle a name quickly and generosity here costs
nothing.** A SysOp choosing to leave stops their own renewal and DNS-
updater contact immediately; the name itself does not become claimable
by a different registrant until the cooldown elapses. Exact cooldown
length is an implementation-time parameter (see the closing note
below), not fixed here, but it is the same parameter for both exit
paths, not two.

**Settled without being a real fork:** how this interacts with existing
Link node addressing — a full peer's descriptor already advertises a
host/port (`advertised_host`/`advertised_port`) for other nodes to dial;
a stable managed hostname is exactly what belongs there instead of a raw
dynamic IP. No new addressing concept, no conflict with fingerprint-
based node identity, which stays the actual trust root regardless. This
covers only the Link-to-Link dial path, not the caller-facing address a
human dials — see Decision 6.

**Decision 6 (locked in) — the managed hostname's caller-facing address
is standard ports on a fixed, documented convention, not a new discovery
mechanism.** Code review follow-up (PR #218): an A/AAAA record alone
cannot tell a human caller which transport or port to use, and
`advertised_host`/`advertised_port` (Decision 5's "settled without being
a real fork" above) describes only the Link HTTP listener — a
Link-disabled or outgoing-only board has no dialable address there at
all, so reusing it doesn't by itself deliver on "myboard.netbbs.org" as
a caller-facing promise. Resolved by convention rather than a new
protocol: a managed subdomain implies the node's Telnet (23) and SSH
(22) listeners sit on their standard ports, the same assumption every
plain hostname-based BBS address already carries. Web is the same
convention with one added, non-optional requirement: `netbbs.net.
nodeconfig`'s own web listener provides no TLS of its own, only through
an external TLS-terminating reverse proxy, so "web (443)" specifically
means that proxy bound to 443 and forwarding to the node's loopback web
listener — never NetBBS's own listener bound to 443 directly, which
would silently serve plaintext HTTP (including password entry) on the
port every caller assumes is HTTPS (code review follow-up, PR #221).
A board that cannot or will not run on standard ports (web's TLS-proxy
requirement included) keeps its managed DNS record (useful for the
dynamic-IP-tracking half of this feature alone) but does not get a bare
`myboard.netbbs.org` caller address as part of it; publishing a
nonstandard port remains the SysOp's own responsibility to communicate,
same as today.

*The convention is advisory and unverifiable, and it is stated where the
name is (issue #603).* Neither the node nor the service can see a
port-forward, a proxy or a firewall in front of a listener, so nothing
enforces this and nothing pretends to. What the node *does* know with
certainty is its own configured ports — and NetBBS's shipped defaults
are 2323/2222/8080 on purpose, since binding below 1024 needs privilege
this process should not want, so the default node is exactly the one
this decision says gets no bare caller address. Until issue #603 the
convention lived in one help panel behind a `[W]eb behind HTTPS proxy`
field whose answer was discarded, and the status screen showed
`myboard.netbbs.org` with a LIVE badge and no port in sight. Now the
registration editor and the Managed DNS status screen both state the
convention and measure it against the listeners the node recorded at
its last startup: "SSH: this node is configured for 2222, so a caller dialling
22 needs a port-forward or proxy in front of it", "Telnet: not enabled
on this node", and for web whether `[web] public_url` names an HTTPS
front — the one statement a SysOp has already made about TLS, which
replaces the discarded question. "Cannot verify" is not "cannot
mention". The LIVE badge itself is now made only once the service has
confirmed a published record; `matured` alone reads "NOT YET PUBLISHED".

**Decision 7 (locked in, implemented) — the managed-DNS credential is
in scope for node backup/restore, as an addition to §13.4's contract,
not a separate ceremony.** Code review follow-up (PR #218): §13.4
already treats a node's recoverable state as one atomic set of specific
artifacts (database, node identity, SSH host key, banners) precisely
because a partial backup silently loses things a SysOp needs after
restoring from disk loss. The managed-DNS credential (Decision 2) is
exactly that kind of durable node state — without it, a restored node
cannot update, voluntarily release, or benefit from Decision 5's
same-owner reclaim window for its existing registration. Its credential
file (`netbbs.managed_dns.credential`) joined §13.4's backup manifest as
its thirteenth artifact, the same plain-file-copy handling already used
for node identity and the SSH host key — see §13.4's own table.

**Decision 8 (locked in) — a node learns the managed service's address
from a shipped constant, overridable per node; it is not waiting on an
instruction from an operator there was never a way to give.** Issue
#583: a freshly bootstrapped 7.6.0 node accepted the opt-in — the
pre-set answer under issue #219 Decision 7 — and stopped immediately at "ask your
operator to set the service address." `netbbs.managed_dns.state.
set_service_url` had no caller anywhere in the installed package: no CLI
flag, no `netbbs.toml` key, no admin screen. The address had no route
into the database every other part of the feature reads it from, so
every node that accepted the offer dead-ended identically, and the
message sent each SysOp looking for an operator who was themselves. The
original reasoning — that the production address is an operational
decision independent of this client code — was right about *where the
decision lives* and silent about *how it travels*; an operational
decision still has to be expressible somewhere. Both halves exist now.
`DEFAULT_SERVICE_URL` (`netbbs.managed_dns.state`) carries the project's
own instance, the same shape and the same reason as `netbbs.link.
reliable_nodes.RELIABLE_NODES_URL` — a project-run service a node must
not need to be told about in order to use. `[managed_dns] service_url`
(or `--managed-dns-service-url`) points a node at a different one, which
is what a self-hoster running their own `services.managed_dns`, or the
project pointing a node at a staging deployment, needs. The configured
value is mirrored into the node database once per startup, *including
its absence*: an operator who removes the setting returns that node to
the shipped address rather than leaving it pinned to an override it was
told about once.

Making the address configurable makes it *changeable*, which the
credential's own design (Decision 2) has to answer: a managed-DNS
credential is a bearer secret for one service's registration, so a node
that already holds one must never present it to a service that did not
issue it. The issuing address is recorded in the same transaction as the
registration it belongs to, and every path that would send the
credential checks it first: the updater pauses its heartbeat (saying so
once in the log), release, rename and cancellation refuse and name the
two addresses that disagree, and registration against a new service
starts over as a fresh registration rather than presenting the old
secret — behind one confirmation, because it replaces the credential
file and leaves the registration at the old service to lapse on its own.
For the same reason the setting is restricted to `https://` unless it
names a loopback address: the service's own process speaks plain HTTP
behind a TLS-terminating proxy, so the node's side of it is the proxy's
address, and a plaintext hop to a remote host would put the credential
on the wire on every heartbeat.

`DEFAULT_SERVICE_URL` is `https://dns.netbbs.org`: the project's
instance on Roanoke, behind the same Apache that serves the website,
against the same BIND that serves the zone (roadmap tracker #612, step
2). From v7.7.0 until that deployment the constant was `None` and a
node simply had no service to register against. The opt-in was still
asked, and still recorded exactly once, per Decision 1 and issue #219
Decision 7; what differed was only that the node said plainly that the
service was not running yet, and it still says so if the constant is
ever reverted. Deliberately not "hide the question until the service
exists": the decision being asked is whether this node may contact
project infrastructure at all, the first-run screen is the one moment
that question is naturally in front of a SysOp, and deferring it would
mean either re-opening a settled consent question later or enrolling a
node that had said yes to something narrower.

**Decision 9 (locked in, implemented) — a SysOp may change their
managed name, and the change is make-before-break: the old name stays
live until the replacement has matured and published, then enters the
ordinary cooldown.** The workflow shipped with its invariants recorded
(the closing paragraphs below) but without the decision above them
(issue #601), so a reader asking "can I change my board's name, and
what happens to the old one?" found the guarantees only by knowing to
look among implementation notes. The decision: `Change [N]ame` on the
DNS screen reserves the replacement as a second, pending registration
linked to the current one (`replaces_name`), holding a second bearer
credential on disk beside the first. The node heartbeats both. While the
replacement matures — the same age gate as any registration — the old
name is canonical and keeps resolving; the status screen shows the
current name and the reserved one, and `[C]ancel change` is offered the
whole time. The heartbeat that first publishes the replacement's record
is the one that releases the old name, in that order (publish, delete,
then commit both rows), so callers never see a gap; the old name then
sits in Decision 5's cooldown, reclaimable only by the credential that
held it. Why a second credential and a transition journal rather than
something simpler: the two rows are two registrations the service
authenticates independently, a crash between "new credential issued"
and "node knows about it" would otherwise strand one of them, and the
journal is what makes the swap replayable in either direction. The
alternative — release, then register the new name — goes dark for the
whole age gate and forfeits the old name's reclaim window, which is why
the SysOp handbook says not to use it as a rename shortcut.

**Decision 10 (locked in, implemented) — every managed name requires
continuous contact from the node to stay alive; `dynamic` selects only
whether the published record follows the node's address; and a name the
service has abandoned is reclaimed by the node itself when it returns.**
Issue #600 found the Goal describing a "registration without the
updater" that the code had never offered: going live requires a day of
uninterrupted contact, staying live requires contact within every
abandonment window, and the node has no setting that stops the pass
while keeping the name. That is the right design, so the Goal was
amended rather than the code: the heartbeat is the liveness signal the
abandonment sweep uses to free squatted names, which is a Decision 3
abuse control, not a dynamic-DNS implementation detail, and a static
registration that never had to check in would be a name nothing could
ever reclaim. What a static board gets instead is that its record is
written once and never rewritten.

The real cost of that rule was a node back from an outage. Take a board
down for a fortnight's holiday: the sweep abandons the name after a
week, the record leaves DNS, and on the node's first heartbeat back the
service answers 401 — at which point the updater used to stop for good.
The name was held for the node for the whole cooldown, reclaimable by
one keystroke, but nothing said so; a SysOp who did not happen to open
the DNS screen lost it when the cooldown purged it. Abandonment is the
service noticing the node was away, not the SysOp choosing to leave, so
the node now performs that keystroke itself: on every pass while its
cached status is `abandoned` (and no rename is outstanding), it sends
`POST /reclaim` with the credential it still holds, and on success
carries straight on into the ordinary heartbeat. The route is the
safety of it: the service performs the reclaim the credential entitles
the node to, or refuses — it never registers afresh on the node's
behalf. A fresh registration mints a new credential, spends a
rate-limit token and, once the cooldown has purged the row, is for a
name that may no longer be this node's; all of that stays the SysOp's
own `[R]egister`. It is a route of its own rather than a flag on
`/register` so that a service older than it answers 404 and the node
fails closed, where an older service ignoring an unknown flag would
have registered afresh from a background task. A refusal is recorded
as a recovery note the DNS screen shows beside the ABANDONED badge, with
the sentence the service gave — and the screen then stops claiming the
name is held. Which refusals are retried follows from what can change
without the SysOp: the service at capacity, unreachable, or without the
route (an upgrade will bring it) are retried next pass; a 409 — the row
purged, held by another credential, or revoked — is *final*, since no
later pass can change what this credential is entitled to, and the
updater sends nothing more for that name until the SysOp's own
`[R]egister`. Three refusals are answers rather than failures: a row that is *already
active* under the same credential is the retry of a reclaim whose
response was lost, and is answered with the row's state so the node
resumes heartbeating; a `released` row is refused with that word, since
release is the SysOp's decision to stop (Decision 5) and a node
restored from a backup taken before it would otherwise undo it on its
first pass — the node adopts the service's word instead; and a
`revoked` row refuses the automatic path exactly as it refuses the
manual one (Decision 4).

*A check-in that does not happen is never silent (issue #640).* Because
the name depends on contact, a node that holds a `pending` or `matured`
name and did not reach the service says so on the DNS screen: "Last
contact: never" rather than no row, the reason the latest attempt
failed, and no "nothing to do". Two independent v7.9.0 nodes registered,
were told the name would go live once the node had stayed in contact,
and never contacted the service again, and every way that can happen was
silent: a credential file the node's account cannot read (written by
`netbbs.admin` run as another account) raised and ended the updater task
for the rest of the node's uptime, and a node whose only route out is an
HTTP proxy failed every check-in into the log. Hence three rules. The first check-in is
sent by the register flow itself, the moment a registration succeeds, so
the SysOp is told of a failure while still looking rather than promised
a wait. Every reason a pass sends nothing for such a name — unreadable
or missing credential, no service address, a credential another service
issued, an opt-in that is not `accepted`, a request that got no usable
answer, a pass that raised — is recorded for that screen, and the task
survives a pass that raises, since what raises there is repaired without
a restart. And the heartbeat's direct connection stays: the service
publishes the address a check-in arrives from, which through a forward
proxy is the proxy's, so `/register` honouring `HTTPS_PROXY` while
`/heartbeat` does not is deliberate — it is stated where the failure is
shown, since a SysOp cannot guess it. A node with no direct route cannot
hold a managed name today; letting one declare a static address instead
is an open question, not something the node works around.

**Implemented.** Node-side client (`src/netbbs/managed_dns/`, shipped
inside the installable `netbbs` package: opt-in prompt, credential
storage, the periodic heartbeat/updater task, the SysOp status/register/
release admin screen) and the managed-service backend
(`services/managed_dns/`, a separate deployable the project itself
operates, never packaged into a node install, the same relationship the
already-live netbbs.org website has to its own separate deployment) —
including a real `Rfc2136DnsProvider` (TSIG-signed RFC 2136 dynamic
updates against a self-hosted BIND server, this project's own
infrastructure choice, not a commercial DNS API) behind the same
`DnsProvider` interface a `LoggingDnsProvider` satisfies for every
automated test. Decision 3's originally-locked review queue was dropped
during implementation planning (see that decision's own closing
paragraph above) in favor of a simpler, symmetric hard-reject shape for
both the rate limit and the cumulative cap. Shipped implementation-time
parameters, all constructor-injectable and reasoned defaults rather than
values this design doc fixes: a 24-hour minimum-age qualifying period
(Decision 3), a 5-per-hour-refilling, 5-capacity service-wide
registration rate limit (Decision 3), a cumulative cap of 1000 active
registrations (Decision 3), a 7-day no-contact abandonment threshold
before a registration is swept as abandoned, and a 90-day cooldown
shared by both voluntary release and abandonment (Decision 5, "on the
order of 90 days" as locked in above). A fourth terminal status, `revoked`, and the
token-gated `/admin/revoke` behind it carry Decision 4's manual dispute
process (issue #599); `MANAGED_DNS_CONTACT` is the channel Decision 3's
refusals name (issue #598). Actually standing the backend up
— a host, DNS delegation, a real BIND server's `allow-update` ACL and
matching TSIG key — is an operational step the code does not perform on
its own; see `services/managed_dns/README.md`. It is done: the project
instance answers at `https://dns.netbbs.org`, and the shipped
`DEFAULT_SERVICE_URL` names it (Decision 8). Turning the zone dynamic
had two consequences the runbook now records: hand edits to the zone
file go through `rndc freeze`/`thaw`, and every static name in the zone
is in the service's blocklist first, because the provider *replaces* a
name's address records rather than adding to them.

The minimum-age period measures uninterrupted successful heartbeat
contact, not wall-clock time since registration: first contact starts
the window and a gap beyond the abandonment threshold resets it. DNS
provider mutations run outside the HTTP event loop and share one bounded
transition lane with their surrounding database mutation. A sweep,
heartbeat, release, or reclaim therefore cannot commit from state made
stale by another provider await; concurrent HTTP transitions receive a
retryable rejection before entering the worker queue. Publication and
deletion failures remain retryable state (release is not finalized until
deletion succeeds, and a failed static publication is retried). Successful
replacement maturation carries that publication result through the rest of the
heartbeat and never issues a second publication from the stale pre-transition
row. The service-wide token-bucket state survives backend restarts.

Node-side updater passes and interactive registration, release, rename, and
cancellation transitions share one process-local transition lock per node
database. The lock covers the remote mutation and its local reconciliation, but
never a human prompt, so a heartbeat response from an older snapshot cannot
overwrite a completed SysOp transition. Node-side rename state and interactive
registration/reclaim results are each one local database transaction: the
replacement name, pending/unpublished state, and previous-name presentation state
never become visible in a partial combination, nor can a reclaimed status appear
with a stale published flag. Heartbeat reconciliation follows the same
rule for the active and previous names, statuses, publication flags, and contact
time. A credential-specific HTTP 401 is authoritative independently of the
other credential's transient result, and all inactive outcomes from one pass
are applied in that single transaction. Cancellation and updater-led recovery
first commit the remotely revived
previous name as a coherent local state, then use the credential transition
journal in reverse to restore the previous bearer secret and remove the
replacement-era extra secret. The service's cancellation response supplies the
revived name's authoritative publication state; a failed republish cannot revive
a stale local publication claim. A crash on either side remains recoverable by
the retained two credentials and the updater. Point-in-time restore likewise
removes any primary, previous, or journal credential artifact absent from the
backup generation. A replacement abandoned during a rename remains eligible
for credential-recovery retry only while its own release/abandonment cooldown
is still open, and reactivation restarts its contact window at the retry time.
Recovery of a replacement which is still marked pending also records fresh
contact and restarts its maturation window only if the prior contact gap crossed
the abandonment threshold. A successful standalone registration or reclaim
clears any expired previous-name presentation state and obsolete previous
credential rather than leaving a phantom cancellable transition.
Rename completion releases or deletes the previous row only when it still belongs
to the same node fingerprint; a cooldown-expired name reissued to another node is
never mutated through the stale replacement link. Cancellation performs the
same ownership check before any provider deletion, including when the reissued
previous row is already active. Rename retry likewise treats an existing
`replaces_name` relationship as recoverable only when the replacement and
currently authenticated registration have the same node fingerprint; reissue
never transfers control of an old replacement credential. If its heartbeat
becomes
authoritatively inactive while the old
name's heartbeat fails transiently, the node continues heartbeating the retained
old credential on later passes rather than letting the still-live name expire.
A successful old-name heartbeat is also reconciled when the replacement
heartbeat fails transiently: the replacement's cached state remains unchanged,
while the old name's authoritative status and publication result are committed
atomically.
An authenticated successful rename also refreshes the current registration's
contact window before reserving or recovering its replacement, so a waiting
abandonment sweep cannot immediately withdraw the promised old name. An inactive
rename target whose cooldown has elapsed is deleted and admitted as a fresh
replacement immediately, matching ordinary registration rather than waiting for
the periodic sweep. Successful cancellation refreshes the retained
previous registration's last-contact time even when it was still active. An
uninterrupted pending name preserves its earned maturation window, while one whose
last contact crossed the abandonment threshold restarts that window just like a
heartbeat.

### Issue #219 — Reliable Link as default onboarding infrastructure

**Goal:** turn Reliable Link (the project's own persistent, internet-
reachable node, run on hosting with roughly a decade-plus operating
history) into the first entry of a small, resilient "reliable nodes"
list new installs can use to get online frictionlessly — as a default
Link seed, an async relay candidate, and (once #168 ships) a live-chat
relay anchor — without becoming a hard dependency or a single point of
failure or responsibility. Builds directly on #168 (real-time relay
protocol) and #201 (managed DNS) without duplicating either's own
decisions; this entry is the product/architecture layer that sits above
both.

**Terminology (locked in):** official prose always says "Reliable
Link" — the same "spell it out in anything official" convention already
established for "NetBBS Link" itself (never "the Link" outside casual
shorthand). "ReLink.NetBBS.org" stays the DNS-only shorthand — a
hostname, not the node's name in any document or UI copy.

**Decision 1 (locked in) — purely a configurable default, never
protocol-privileged.** Nothing in the Link protocol may treat any
reliable node's fingerprint as special, required, or protocol-blessed.
Technically indistinguishable from a SysOp typing in any other seed —
this is the concrete answer to "does this violate the no-central-master
design," not just an assertion of it.

**Decision 2 (locked in) — a list, not a single hardcoded node.**
Resilience by actually having more than one reliable node from day one,
not resilience-in-theory because the field happens to be configurable.
Reliable Link is the flagship/first entry. If it were ever to stop
operating, the intended failure mode is "some other already-established
reliable node is already in the list," not "someone has to
specifically step up" at that moment.

**Decision 3 (locked in) — discovery is hybrid.** A hardcoded fallback
list ships in source (always works, even offline or if the live
endpoint is briefly unreachable on a SysOp's very first run) plus a
live-fetched list from a stable project-controlled endpoint (under
`netbbs.org`, which the project already controls and presumably outlives
any one node), preferred when reachable. Keeps the roster current
without requiring every installed node to upgrade NetBBS itself to
learn about a change.

**Decision 4 (locked in) — one list, three consumers, not three
separate mechanisms.** Default Link seeds for peer discovery; async
relay candidates (falls out of the *existing* `relay_selection.py`
reliability-ranking automatically, once a reliable node is a known peer
with relay serving enabled — no new selection mechanism needed); and
the live-relay anchor for #168's raw-proxy design. This is also the
answer to one of #168's own still-open questions ("does live relay need
its own consent/capacity concept, or reuse the async model's?") — for
v1, neither: just try reachable reliable nodes from the same list.

**Decision 5 (locked in) — relaying carries zero Phase 4 trust
implications.** Using a reliable node as a relay is not that node
vouching for the relayed traffic's origin, and trusting a reliable node
as infrastructure is not a signal about anyone else who also relays
through it. A reachability decision, not a trust decision — matches how
existing async relay consent already works.

**Decision 6 (locked in) — a node cannot participate in Link with an
unset/placeholder display name.** Surfaced during this discussion, not
originally part of it, but adopted alongside it: today's default node
name is the literal placeholder `"NetBBS"`; every default-installed node
sharing that same display name would make human-to-human conversation
about "which node am I talking to" incoherent the moment Link
participation is easy enough that many nodes actually use it. Enforced
before Link participation, not before local-only operation — a
SysOp running a purely local board never has to touch this.

**Decision 7 (locked in) — first-run UX is one screen, two separate,
defaulted-to-accept choices, not one bundled yes/no.** Relay/seed
participation and the public DNS name (#201) are offered together on
one friendly first-run screen, each pre-set to accept (so accepting
both is a two-Enter-keystrokes path) but each independently declinable
with a plain-English explanation of what accepting means. Deliberately
not collapsed into one "get online easily" choice: both declines have a
real, coherent meaning a SysOp might actually want (relay without a
public name — mesh reachability without independent discoverability; or
a public name without relay — already has direct connectivity sorted),
and bundling them would silently reintroduce the exact consent problem
that made #201 land on opt-in in the first place, for whichever half of
that combination a given SysOp didn't actually want.

**Implemented (issue #266), with the previously-open implementation
choices settled as follows.** The live roster is
`https://www.netbbs.org/reliable-nodes.json` — a JSON object
`{"version": 1, "nodes": [{"name", "url"}, ...]}`, served as plain static
content from the project's own web host (source copy and runbook in
`services/reliable_nodes/`), fetched once a day by
`netbbs.link.reliable_nodes.run_scheduled_reliable_nodes_refresh` under the
same off-switch as the release check, bounded at 32 entries with per-field
length caps, and rejected as a whole on any other format version so an old
build keeps its last good copy. A successful fetch *replaces* the built-in
fallback rather than merging with it, so removing a node from the roster
actually stops it being dialed. `[link] enabled` is now tri-state: an
explicit TOML/CLI value always wins; a silent configuration defers to the
node-wide participation decision (`netbbs.link.onboarding`), resolved once
at startup, so accepting on the first-run screen turns Link on as an
outgoing-only node with no port to open. The participation decision also
gates whether the roster is dialed at all, independently of how Link came
to be enabled — a node upgraded in place never dials project
infrastructure until its SysOp says so. Decision 6 is enforced at the
startup boundary: with Link effectively enabled and the placeholder name,
`python -m netbbs` refuses with a `StartupError` naming the fix, the same
shape as the no-SysOp refusal; the first-run screen asks for the name
before recording an accept so a fresh install never reaches that refusal,
and the console's `Settings > Join NetBBS Link` screen refuses to accept
under the placeholder for the same reason. The first-run screen
(`netbbs.net.onboarding_flow`) lives at the two anchors issue #201's prompt
already used — `netbbs.admin`'s first-SysOp bootstrap and, as a fallback,
a SysOp's authenticated login — each choice checking its own state so an
upgraded node is asked only what it never answered. Still open: Reliable
Link's own operational configuration (relay-serving is already on by
default; live-relay capacity planning waits for issue #168).

### Issue #270 — multi-hop live relay and cross-node /private

**Goal:** close the two gaps §8.10.3's first vertical left open --
relay-of-relay live sessions and the private-conversation mode across
nodes -- without reopening the raw-proxy decision (§16 "Issue #168").

**Decision 1 (locked in) — three steps, all shipped together:** anchor
advertisement (`live_relays` in the signed endpoint descriptor),
dial-the-anchor (a requester that can reach the target's advertised relay
meets it there, single hop), and the chained bridge (two relays, A–R1–R2–B)
only when the requester cannot reach any of the target's anchors. Most
cross-anchor cases never chain; chaining is the resilience path, not the
common one.

**Decision 2 (locked in) — discovery is the target's advertised anchors,
not relay-side search.** The requester names the relay to forward toward
(`via_relay`), taken from the target's own descriptor. A relay-side
fan-out ("ask every reliable node whether B is there") was rejected: it
turns one request into up to a roster's worth, adds latency, and needs no
descriptor change only at the cost of search traffic on every miss.

**Decision 3 (locked in) — hop bound of one forward.** A forwarded request
(`hops: 1`) is never forwarded again, so a chain is at most two relays and
every relay's own caps and byte/idle bounds apply per hop. Longer chains
would need routing state no relay currently keeps and are not a v1 need.

**Decision 4 (locked in) — `/private user@node-fingerprint` ships; cross-
node `/dm` invites do not.** Private mode rides the existing live
direct-message path line by line (small); a cross-node fullscreen direct
chat needs mutual invite/accept frames and a shared room spanning two
nodes -- a separate step, roughly the size of the direct-message vertical.

All frame additions (`via_relay`, `hops`, `for_fingerprint`) ride real-time
protocol v3, unreleased at the time, so no further bump was needed.
Normative description: §8.10.3.

### Issue #611 — password change and reset — closed

Until this issue shipped, `users.password_hash` was written once, at account
creation, and had no update path anywhere: no Profile field, no user-detail action, no admin
CLI command. A forgotten password meant delete-and-recreate, which loses the
account's history and, per #594, frees its Link identity for the next
registrant. Normative description: §4.1.

**Decision 1 — one domain function, three surfaces.** `set_password` is the
only writer, in the same transactional shape as the other account setters;
the Profile field, the user-detail `[P]assword` action and the CLI subcommand
all go through it, and the two screens share one implementation
(`netbbs.net.password_screen`), the same shape the SSH-key screen already
has. Who may call it is the caller's decision; what must hold regardless
(no blank password, no clearing without a key) is the function's.

**Decision 2 — the proof depends on who acts, not on the target's level.**
An account acting on itself proves the current password first; a SysOp
acting on another account does not, since they cannot know it, and the audit
row names them. A SysOp may reset another SysOp's password, matching what the
key screen already allows a SysOp to do to any account; the usable-SysOp
invariant (§4.3) is unaffected because a reset never removes a way in. The
local CLI never asks for a current password: filesystem access to the
database is its trust boundary, as for the rest of that tool, and its
purpose is the locked-out SysOp.

**Decision 3 — the in-session proof is throttled by the login throttle.**
`Session.login_throttle` is set once at login and the current-password
prompt charges it before verifying, in the same order as the login prompt.
Rejected: a separate per-session counter, which would have been a second
budget with its own limits to explain.

**Decision 4 — a key-only account sets its first password without proof.**
The session that reached the screen authenticated by key. Rejected: refusing
until a SysOp intervenes, which would make the only self-service route out of
key-only depend on someone else.

**Decision 5 — masked entry is a documented exception to §3.5, not a draft
editor.** Current password, new password, confirmation: three masked
prompts, each cancelling on a blank line, nothing written before the last.
Rejected: a draft editor, which would have to hold the plaintext password in
the draft across redraws so that `[S]ave` had something to save, and whose
"inspect before saving" value is nil for a value the caller cannot see. §3.5
and `AGENTS.md` both list the exception.

**Decision 6 — a caller's own choice meets the registration floor.**
Self-service applies `MIN_REGISTRATION_PASSWORD_LENGTH`, as both
registration prompts do for a password a remote caller picks; otherwise
Profile would be a way around the floor one screen after registration. A
SysOp setting someone's password keeps the latitude the create-user screen
already gives them.

**Decision 7 — Argon2 stays off the database lane.** The foreground
`DatabaseLane` has one worker; a hash or verification there stalls every
other interactive database operation for its duration and bypasses the
bounded password worker login uses. Both screens and the CLI therefore hash
and verify through that worker (`hash_password_off_loop`,
`verify_password_off_loop`) and run only the short transaction
(`set_password_hash`, `load_password_hash`) on the lane. The synchronous
`set_password`/`password_matches` remain for tests and for callers that own
their thread.

**Declined — guarding this setter against SQLite rowid reuse.** A target
deleted and another account created while the screen waits for input could
inherit the id and receive the update. Every setter in `netbbs.auth.users`
re-fetches by id the same way, and the case needs the highest-id account
deleted and re-created under an open SysOp screen, which is past the
single-operator boundary this project calibrates against. The identity-reuse
question is issue #594's, and its answer applies to all the setters at once.

**Not done, deliberately.** A password change does not end the account's
other live sessions; a caller who suspects a compromise asks the SysOp to
disable the account, which does. No self-service recovery exists: there is no
email or other out-of-band channel to send anything through, so "forgot my
password" is a SysOp action, and the Profile help text says so.

### Issue #596 — who may pull an attestation, and what opting out retracts — closed

Found by the review of the v7.7.0 release. Two facets of one gap between what
a caller is told when they share a SysOp-verified birthdate or real name over
Link and what the node does.

*Disclosure scope.* `_handle_attestation_pull` admits any completed peer whose
signed request is fresh and whom `LinkPolicyAction.TRUST` allows, and
`load_issued_attestation_page` takes no requester at all. The receiver's
`link_attestation_authorities` is a list the issuer never sees. So every
federated peer can read every attestation this node has signed, while §5.5
will not show the same value to the node's own SysOp.

*Retraction.* `_revoke_issued` stamps the row and leaves `envelope_json`, which
carries `attested_value`, in place; the page query filters on nothing. A peer
that links next year and pulls from the start reads a value whose subject
opted out this year.

v7.7.0 shipped consent text that says both things honestly and enforces
neither. These decisions replace that text with enforcement. Normative
description: §5.5.

**Decision 1 — an issuer-side recipient list, and it starts empty.** A new
SysOp-configured, audited set of node fingerprints: the nodes this node tells.
The pull handler keeps every gate it has and adds one: the requester must be
on the list. An empty list means nothing leaves the node whatever its callers
have toggled. An upgrade seeds the list from nothing. In particular not from
the attestation authorities: whose verifications a SysOp accepts says nothing
about whom they are willing to tell, and the two lists point in opposite
directions. Rejected: correcting the consent text and enforcing nothing, which
is what v7.7.0 did as a stopgap and which leaves a value §5.5 withholds from
the local SysOp readable by every peer.

**Decision 2 — a recipient is a node, not a node and an attribute.** The
receiving side scopes an authority to `age` and `name` separately, and the
mirror image was considered. Rejected for two reasons. The caller already
scopes per attribute, with two separate toggles, so the finer knob exists
where the consent is. And a per-attribute grant makes each requester's stream
a function of its grant history, which the protocol cannot express: the pull
cursor is the subscriber's, it names a position, and a requester whose scope
widens has already moved past the rows it was not entitled to last week.
With a per-node list that cannot happen, because of Decision 3.

**Decision 3 — a node that is not a recipient is refused outright, and told
so.** HTTP 403 with `reason_code` `not_an_attestation_recipient`, in the shape
a policy rejection already has. Not a revocations-only stream, although a
revocation carries no value: serving part of the stream advances the
requester's cursor past the attestations it was not shown, and a later grant
would then deliver none of them until each came up for renewal. A refused
requester's cursor does not move, so a grant, a removal and a re-grant all
resume from the right place with no cleverness on either side. The refusal is
visible rather than silent because what it discloses is a relationship between
two nodes that the other SysOp has to act on, by asking to be added. §12.8's
silence protects a per-user gate decision; this is not one.

A node removed from the list stops receiving revocations too. That cost was
accepted here and is reversed by issue #632 Decision 2: removal now sends the
node an empty snapshot, which makes it forget what it held.

**Decision 4 — the caller is told how many, the SysOp sees which.** The
consent text says the value reaches only the nodes the SysOp has named, that
the SysOp may name more later, and the toggle shows how many that currently
is, so a caller can see that sharing to nobody shares nothing. Rejected:
per-caller approval of each recipient. A caller cannot evaluate a node
fingerprint, and re-asking every sharing caller at every grant would make the
list unusable by the one person who can evaluate it. The SysOp is already the
party the caller trusted with the verified value itself; choosing where the
node's assertion of it goes is the same trust, not a new one.

**Decision 5 — a retired attestation stops being served and loses its
value.** When an issued attestation is revoked or expires, the issuer blanks
the columns that carry the value (`attested_value`, the envelope, the
signature) and keeps the row: content ID, attribute, timestamps, what revoked
it. That removes the value from the live, served database; it is not forensic
erasure, and "Not done" below says what it leaves. The
page query serves only live attestations and every revocation, and filters on
liveness at read time so that what is served never depends on when a sweep
last ran. The migration redacts rows already revoked. This supersedes two
sentences of §5.5: "neither expiry nor revocation deletes the signed
historical row" and "the served stream includes expired and revoked objects".

Resumability survives, which was the stated reason for serving history whole.
A cursor names a position, and the tombstone keeps that position resolvable
for a subscriber whose last object was the one redacted. What a returning
subscriber needs from history is the revocation of anything it holds, and
every revocation is still in the stream, after the object it retires. A
retired attestation the subscriber never received is nothing it needs:
acceptance already ignores it, and serving it would hand over a value whose
subject has withdrawn it. A revocation whose target the subscriber never
received is skipped per object, as it is today.

**Decision 6 — a receiver forgets what it is told is withdrawn.** On ingesting
a revocation, and once an attestation it holds has expired, a node blanks the
stored value and envelope and keeps the row, which its revocation, effective
projection and audit rows reference. This is what "switching it off withdraws
it" can honestly mean between two nodes running this software, and the consent
text says the rest: a node that already copied the value cannot be forced to
forget it.

**Not done, deliberately.** No per-caller recipient choice and no
per-attribute recipient scope, for the reasons above. No revocation delivery
to a removed recipient. No way for an issuer to learn what a recipient did
with a value. No secure erasure: redaction blanks columns in the live
database, so the write-ahead log until its next checkpoint, freed pages, and
any backup taken while a value was shared can still hold it, and the issuing
node's own verification record keeps the value for as long as the SysOp keeps
the verification. The threat answered is a peer pulling a value, not someone
reading the disk; turning on SQLite's `secure_delete` for the whole node to
narrow that further was not judged worth its write cost.

**On upgrade.** Attestation sharing stops until the SysOp names recipients.
That is the intended default, and the release notes have to say it, because a
node that was sharing will otherwise read as broken.

### Issue #594 — a deleted account's username stays retired on a Link node — closed

`local_user_id` is the username, and `users.username` is unique only among
live rows, so deleting an account frees its Link identity for the next
registrant. Link mail sends and delivers by it (`deliver_link_message` resolves
the recipient with `get_user_by_username`), a carried post's `author_label` is
`username@home-node-fingerprint` and `link_events` keeps it, the trust subject
is derived from it, and a remote identity attestation names it. A
re-registered name inherits all of that: authorship, trust state, a live
attestation until its revocation propagates, and mail a remote sender wrote to
the previous holder. Normative description: §4.3.

**Decision 1 — retire the name; do not change the identifier.** A stable,
never-reissued per-account identifier was the alternative. It needs a
migration assigning one to every account, a wire decision about the
`node_vouched_user` payload every peer already has on disk, a display
decision because an opaque ID is exactly what must not be shown, and a
compatibility story for labels peers retain. It also would not be enough: a
Link mail address is `name@node` by design, so whoever holds the name gets
the mail whatever identifier sits underneath. Retiring the name is the part
every answer needs, so it is the part built.

**Decision 2 — recorded at deletion, on a node that has ever run Link.**
`delete_user` writes the retired name in the transaction that deletes the
account, when the node runs Link now or has at any time before. "Ever" is a
sticky marker the node sets the first time it starts with Link effectively
on. The migration that introduces it seeds it on a node that ran Link before
the marker existed, from any artifact Link leaves behind: a stored peer, a
retained event, a linked board, channel or file area, a piece of Link mail,
or a recorded decision to run Link. Stored peers alone would not do, since a
node can originate a linked board and republish it later without ever having
stored one. A node that has never run Link records nothing and
its names stay reusable, which is both BBS tradition and the cost the issue
names: a small board should not lose names forever to a network it is not on.
A recorded name stays retired whatever the Link setting does afterwards.
Registration checks the record case-insensitively, as the uniqueness index
does.

One kind of account is not retired: a registration still awaiting approval
that was never sent Link mail. Declining registrations is routine on an
approval-required node, and retiring those names would let strangers
permanently consume names merely by asking for them. The exemption rests on
the pending state because that state proves the account never had a session:
it is set only at registration, cleared only by approval, and refused by
every login path. "Never logged in" was considered and rejected as the test,
because it proves nothing: open registration hands a new caller straight into
a first session without recording a login, so an account that registered,
posted on a linked board and never returned has no login on record. Received
Link mail is part of the test because it reaches an account by name without
the account doing anything: delivery resolves the recipient by username,
pending approval or not, and acknowledges it. An approved account that was
never used is retired like any other; the SysOp can release it. The screen that confirms a deletion and the
deletion itself ask one shared predicate, so the warning cannot promise a hold
that does not happen.

The test is "ever", not "now", because what peers hold does not evaporate
when Link is switched off. Keying on the setting at the moment of deletion
would let an account that was Link-visible be deleted during a maintenance
window with Link off, re-registered, and then carried back onto the network
under an identity its peers already know. The price is over-reservation on a
node that once ran Link and no longer does, where an account that never
federated is retired anyway; the SysOp can release it (Decision 3), and the
alternative is tracking per account whether it was ever Link-visible, which
every Link subsystem would have to feed.

**Decision 3 — the SysOp can release a name, on purpose.** A list of retired
names reachable from user administration, with release as a confirmed,
audited action. A SysOp who deleted a test account, or whose caller comes
back after asking to be removed, is making the trust decision this project
leaves with the SysOp. Without a release the only repair is editing the
database.

**Decision 4 — a remote caller is told the name is unavailable; a SysOp is
told why.** Self-service registration refuses a retired name in the words it
uses for a taken one, so registration is no oracle for who used to have an
account. The create-user screen and the admin CLI say that the name belonged
to a deleted account, that it is held because the node is on Link, and where
to release it.

**Decision 5 — forward only.** The migration creates an empty table. The
moderation log names past deletions, but in free text, and reconstructing
reservations from a log line is the kind of inference that retires the wrong
name. Names already freed stay freed.

**Not done, deliberately.** Nothing repairs an identity a peer already holds
for a name reused before this ships. Nothing in the codebase renames an
account; a rename, if one is ever built, frees a name the same way a deletion
does and has to retire it the same way.

### Issue #589 — automatic equivocation signals — decided

The open half of the entry below: when a node accuses another. Of everything
the code observes, only equivocation -- one node signing two different objects
into the same slot of one chain -- carries proof a receiver checks itself
(#1036). Revoked-key use, invalid signatures, bad authority, floods and
malformed traffic are observer claims nobody else can reconstruct, and no
production code records them as measurements. Normative description: §12.5,
§12.6, §12.9; the Handbook's "Policy trust settings".

**Decision 1 — fully automatic, equivocation only** (the maintainer's choice;
the alternatives were the node proposing and the SysOp signing, or no
issuance at all). Where a chain refuses an extension because its slot is
taken -- a key-transition chain, a post's content chain, a board's lifecycle
chain -- and both objects verify under one node's keys, the node keeps both,
records a self-verifying local observation, which quarantines that node's
identity integrity here (§12.8), and signs a `signed_equivocation` signal with
both objects embedded. Reordering (an extension of a head not on file yet) and
an exact resend are not forks.

**Decision 2 — receivers verify, and two domains still have to agree.** The
signal counts at a subscriber only once its proof reproduces there (#1036),
and only toward the two-domain threshold, so one node's automatic accusation
does not by itself quarantine its subject anywhere else.

**Decision 3 — bounded, switchable, revocable.** At most one live signal per
subject and five signed in any 24 hours; never about the issuing node itself.
A node-wide switch (Settings → Policy trust → Signals, on by default) stops
issuing and revokes every live signal on the next pass. A SysOp can revoke one
signal and keep the evidence, and clearing the evidence revokes its signal.
Honest forks happen -- a node restored from an old backup, a cloned VM -- so
the Handbook says what such a signal about one's own node means.

**Decision 4 — recovery waits for a SysOp** (§12.9), as the section already
required; until this, every trigger recovered after the 24-hour hold.

**Decision 5 — nothing left unwired.** The digest-evidence path went with
#1036 (its Decision 4). `build_trust_signal`, `build_equivocation_evidence`,
`record_local_observation` and `clear_local_observation` now have callers, so
#589 leaves the production-callers allowlist.

### Issue #589 — trust-object issuance — slice 1 decided and built; signals open

Until this slice no node could issue a trust object. `trust_wire` verified,
stored, re-served and enforced on a peer's signed objects, and nothing in
`src/` called a builder, so the carrier store was empty on every node and
§12.7's pull served nothing. No dogfood run, however long, could have shown
trust propagating. Normative description: §12.4 and §12.7.

**Decision 1 — slice 1 is the vouch, and only by a SysOp's act.** A vouch is
the one trust object whose trigger needs no judgment about evidence: a person
decides to stand behind an identity. When a node *accuses* another, which
observations become a signed signal, and whether any of that is automatic,
stays open in the issue. `build_trust_signal`, the digest-evidence path and
the local-observation lifecycle remain without a production caller, and the
ratchet in `tests/test_link_production_callers.py` still lists them.

**Decision 2 — an intent, and one reconcile that signs.** The screen records
an intent; `reconcile_issued_vouches` decides what should exist and signs it.
This is the shape remote attestation already has, for its reasons. The offline
admin console has no node identity, so a screen that signed would not work
there. And two code paths that each decide when a signed object should exist
eventually disagree, so the sync pass and the SysOp console call the same
function, and the listing derives its status from the same predicate.
Rejected: signing in the screen, with the sync pass only renewing.

**Decision 3 — own objects live in the carrier store.** A vouch this node
signs is stored in `link_trust_wire_objects` under its own fingerprint, which
is exactly what the existing pull serves for a requested issuer. No new
endpoint, no new wire type, no separate issued-objects table. It is not
recorded in `link_trust_vouches`: that is what local policy counts, and it
counts only configured reporters.

**Decision 4 — the reason is mandatory, bounded and published.** It is inside
the signed payload, so every subscriber stores it, and the screen says so
before asking for it. Replacing it retires the old vouch and signs a new one,
since signed bytes cannot be edited. 280 characters, because nothing on the
receiving side bounds it short of the response's byte limit.

**Decision 5 — 90 days issued, renewed at 30, reissued on key rotation.** The
180-day ceiling is what a receiver tolerates; a node that goes dark cannot
withdraw, so the issued lifetime is the window in which an unseen withdrawal
still leaves support standing. The same numbers as an issued attestation, so
there is one pair to remember. Rotation is detected by verifying the stored
signature under the current key rather than by remembering which key signed,
which needs no column and cannot drift.

**Decision 6 — a restriction here suspends the vouch, and the intent
survives it.** Vouching for an identity the issuing node has quarantined or
blocked says something the node does not act on, so the reconcile revokes it
and the screen refuses to record one. §12.4 makes that safe: a revocation
accuses nobody. The intent is kept so that lifting the restriction restores
the vouch without anyone having to remember it.

A rotation also re-signs what it would otherwise orphan. A subscriber that has
learned the new key can verify nothing the old one signed. For a vouch the
reissue covers that. A revocation is reissued by nothing, so one signed shortly
before a rotation and not yet pulled would be skipped as an old-key object,
and the vouch it retires would stay live at that subscriber until it ran out.
The reconcile therefore signs such a revocation again under the current key
while its target has not expired; a subscriber that already holds the first
one skips the second as a repeat.

**Decision 7 — the receiving side stops treating an unusable object as a
hostile response.** Three rejections that were fatal to a whole batch became
per-object skips, and changing a reporter's grant now resets its cursor
(§12.7). None of this could be reached while nothing was ever issued. With one
real issuer and one subscriber that has established it, it is reached as soon
as the issuer's SysOp vouches for a caller and the subscriber only granted it
node vouches: the batch is abandoned and that subscription never moves again.
The same would go for the first operational-key rotation. A slice that let
nodes issue vouches and left their subscribers wedged would not have made the
subsystem real.

The rotation handling above became reachable with issue #624, which built the
rotation itself; the Phase 4 exercise gained its rotation row there.

The same decision covers a responder that no longer knows a subscriber's
cursor, filed as #621 while this slice was in review and fixed with it for
both pulls, because it is the same wedge by another route and the recovery
exercise restores a node on purpose.

An object signed by a key the subscriber does not know is the one case that is
*not* skipped, because there the subscriber may be the stale party, and a
skip would lose every object the issuer re-signed. The first cut of this slice
skipped it; review caught that it turned a rotation into permanent loss.

**Decision 8 — pages in storage order.** See §12.7. The attestation page made
the same change for the same reason.

**Not done, deliberately.** No vouch for the node's own users and none for
itself. No trust signals. A subscriber told nothing about *why* an object was
skipped beyond its diagnostics log. (Carrier pulls in the sync loop, also
left out of this slice, followed in issue #627.)

### Issue #630 — content from a node this one has never met — closed

Found by running the planned three-node deployment in the loop harness: one
full node and two outgoing-only nodes that seed off it and share one of its
boards. Normative description: §8.11.

A node could verify nothing signed by a node it had not completed a hello
with, and two nodes complete one only when one dials the other. §8.8 accepted
that as "rarely restrictive" on the expectation that small deployments end up
fully meshed. They do not: a node dials its seeds, everyone is given the same
seed, and two outgoing-only nodes cannot dial each other. So in the ordinary
topology the two never saw each other's posts. Under enforced policy the
unknown author read as probationary and was refused quietly, was not listed
as a trust subject, and so could not be established; each refusal was
downloaded again every pass, and 200 of them starved the subscription. With
the author established by some other route, the stranger check rejected the
carrier's whole response from the first post.

**Decision 1 — a carrier may serve a third node's hello bundle.** §10.6 and
the #90 entry had already worked out why this is safe and deferred it as not
needed. It is needed. The bundle verifies against itself, so the carrier is
trusted with nothing and can at worst withhold.

**Decision 2 — introduced is not met.** An introduced identity verifies what
it signed and does nothing else. It cannot push, pull, relay or be mailed,
and it is stored apart from peers so that no present or future check of "is
this a peer" can mistake it for one. Rejected: a flag on the peer record,
which would have made every reader of that table responsible for remembering
it, the mail path among them.

**Decision 3 — introduce before policy, and start on probation.** The
introduction grants nothing, and it is what makes the node visible to the
SysOp who alone can establish it. Rejected: introducing only authors policy
already admits, which is circular, since a node that is not a subject is
never admitted.

**Decision 4 — carriers pass on introduced identities too.** Rejected: the
peer list's first-hand-only rule. That rule exists because a secondhand
address degrades with distance; a bundle does not.

**Decision 5 — one unusable event costs one event.** Responses from a carrier
are handled per event, and an event that cannot be used yet is set aside and
declared as seen. Rejected: only skipping it, which leaves it to be sent
again every pass and lets a page fill with such events. A refusal that does end
a response is returned with what was accepted before it, which the caller
persists: a batch that raised half-way had already put its earlier events
into memory, the caller persisted none of them, and being now "known" they
were never accepted, and so never persisted, again.

**Decision 6 — a stale bundle is refreshed from the carrier.** Key
transitions of third nodes are not gossiped, and this slice does not change
that; it asks again instead.

**Decision 7 — a met node outranks an introduced one by name.** Introduction
puts names on file that nobody here vouched for by dialing. The
familiar-name comparison is therefore one-sided (§8.11), mail addresses
resolve among met nodes only, and meeting a node re-judges every introduced
node that wears its name, whichever was on file first. Rejected: first-seen
wins throughout, which hands a real peer's name to whoever posts first.
Between two nodes neither of which this node has met it is still the rule,
there being nothing to tell them apart.

**Decision 8 — a carrier answers only for whose content it served.**
Rejected: gating the route like the peer list, which a peer on probation is
refused. A new node is on probation at its seed for its first month, so that
would have withheld introductions from exactly the nodes that need them.

**Not done, deliberately.** Verifying §5.5 attestations against an introduced
identity; trust objects followed in issue #627. Link mail to
an introduced node (§10.6 stays deferred). Automatic graduation for nodes
never met. Any way for an introduced node to be dialled, which includes
fetching its files. Persistence of the set-aside list, which is rebuilt at
the cost of one repeat per event after a restart. A way for a requester to
tell a responder which signers to leave out, which is what would lift the
10,000-event bound.

### Issue #627 — what a node nobody can dial issues — trust objects closed, attestations open

Found while working out how the Phase 4 exercise could run on the three live
nodes, two of which are outgoing-only. Trust objects and attestations are both
pulled from their issuer, and an outgoing-only node has no address, so a vouch
its SysOp issued was signed, stored and served to nobody, and said nothing
about that. Normative description: §12.7.

**Decision 1 — the issuer deposits at its relays; subscribers pull the
carrier form.** The wire already defined a pull whose responder is not the
issuer, and the cursor was already kept per responder and issuer. Rejected:
flooding trust objects with content, which §12.7 rules out for the reason it
always did, and having subscribers wait to be dialed, which an outgoing-only
subscriber never is.

**Decision 2 — deposited objects are stored apart from admitted ones.**
Carriage and authority are different facts about an object, as "introduced"
and "met" are about a node (#630). Rejected: a flag in the admitted store,
where an object present counts as replayed and would never be applied after a
later reporter grant.

**Decision 2a — a relay admits what it carries in its own pass, never on
arrival.** Rejected: admitting inside the deposit handler, which only ever
sees one request's objects. A depositor named a reporter afterwards, a
widened grant and a batch rejected for a reason time undoes would each have
left objects that are never offered again.

**Decision 2b — a deposit names the object it continues from.** A depositor
sends only what is new, so nothing else would ever tell it that a relay has
lost something. Rejected: depositing everything on every pass, and trusting
the relay's count.

**Decision 3 — relay consent is the gate.** A relay already decides whom it
holds mail for and how many; the same decision covers trust objects. Rejected:
accepting deposits from any established peer, which would make every full
node a store for every node it knows.

**Decision 4 — only the issuer deposits.** The objects authenticate
themselves; their order does not.

**Decision 5 — verification accepts an introduced issuer; authentication of a
wire peer does not.** `resolve_known_signing_key` is for checking something a
third party delivered. Every route that authenticates its caller keeps
`resolve_peer_signing_key`.

**Decision 6 — attestations take their own path.** Decided in issue #632's
entry: a sealed snapshot per recipient, through the recipient's relays.

**Not done, deliberately.** A node that drops a relay tells nobody, it only
stops naming it, so a relay does not forget what it carried for it; expiry and
the per-depositor bound are what limit that, and revocations, which do not
expire, count against the bound. For the same reason a relay decides whether
to read its own copy by the depositor's descriptor and not by its own record
of whom it agreed to relay for. A
subscriber does not try relays it has not met. SysOp-written trust signals
remain #589's next slice.

### Issue #639 — what expiry means to a caller — closed

Found when issue #638 made the file area keystroke-only and took
`/download <filename>` with it, which was the last path to a file that had
expired. The contract of `list_files_page` promised expired files were
"delisted from normal browsing though still individually reachable", and
`get_file_by_name` kept returning them, so the promise survived its only
surface. Normative description: §5.3.

Two things found while deciding it, which changed the shape of the answer:

- **`posts.py` makes the same promise and never had a surface either.**
  `get_post`'s contract says "expired content stays individually reachable,
  only delisted from normal browsing", and `netbbs.net.board_flow` contains
  no reference to an expired post and no call to `get_post`. So this was not
  a regression #638 introduced in file areas; it was one sentence written in
  two subsystems, of which files happened to hold an accidental instance.
- **`get_file_by_name` had no production caller left.** Every remaining
  mention was a docstring in the past tense. The promise was being kept by a
  function nothing called.

**Decision 1 — expiry ends a caller's reach, in both subsystems.** A caller
may rely on this: expired means gone. Rejected: a `show expired` toggle on
the file listing, and returning expired rows from `[/] Find` labelled. Both put
delisted content back in front of callers, which is the one thing expiry
exists to stop, and the second costs the most to build — the expiry sweep
calls `reindex_file`, which *deletes* a row from `file_search` as soon as it
stops being `approved`, so search would need a status dimension carried
through every consumer in order to re-expose what it had just removed.

**Decision 2 — the domain keeps its unfiltered lookups, for two named
consumers.** Reply-parent resolution needs an expired thread to resolve, and
SysOp recovery needs an expired file's row to reach its bytes. What changes is
the contracts, which stop describing either as something a caller can do.
Rejected: filtering expired rows out of the domain, which would break replies
to a thread that expired mid-conversation.

**Decision 3 — the SysOp recovery surface is a file-area screen, not a
caller-facing one.** Expired files are listed on the file area's admin detail
screen with `[D]ownload`, mirroring the pending-file review screen #638
already gave that action to, and offered under the same transport rule
(issue #475).

**Decision 5 — `get_file_by_name` is deleted.** Decision 3 was first expected
to give it a production caller again. Building the screen showed it does not:
the recovery screen is a picker, which hands over the row the SysOp chose, and
#638 had already moved `send_file_to_caller` off re-reading a file by name
because a filename is not unique within an area. Routing recovery through the
by-name lookup would have handed back the oldest row of that name, possibly an
approved file rather than the expired one picked. With no caller the function
went, rather than stay as another implemented, tested and unreachable name. Its
pending rule lives on where it is enforced: `list_pending_files` and the
transfer path.

**Decision 6 — a transfer link does not outlive expiry.** A download link
minted while a file was listed is refused once the file expires, its uploader
included, the same way an outstanding link does not outlive a lockout. It
serves an expired file only to a holder of `APPROVE` on the area, which is who
the recovery screen mints it for.

**Decision 4 — posts get no equivalent screen.** A file is an artifact that
is unrecoverable once the grace period ends; a post is text in a board. The
recovery case that justifies decision 3 does not arise, and inventing a
screen for symmetry would be building for nobody.

**Not done, deliberately.** `test_expired_file_still_reachable_by_name` stays,
renamed `test_expired_file_keeps_its_row_until_purged` and pinned through
`get_file` now that the by-name lookup is gone, since the domain behaviour it
guards is still true and still load-bearing: what was wrong was the promise
about callers, not the return value. The recovery screen has no restore
action; a re-upload is how a SysOp puts a file back. The §3.5 bullet recording #638's trade also
cited §11 for the delisting rule, which §11 does not state; it now points at
§5.3, which does.

### Issue #624 — guided operational-key rotation — closed

§4.5 said rotation was "a guided SysOp action", but no screen, command or
task rotated an operational key. The receiving half was built and tested and
could not be reached. Checking what a rotation would actually do found a
second gap. Every event branch verified a signature against the sender's
*current* key only, which made §4.5's "historical signatures remain
verifiable" false. So the first signing-key rotation on a real node would
have left all of its boards, posts, files and in-flight mail unverifiable to
any peer that had not received them yet, carriers' copies included, and a
board it originated could no longer be joined.

**Decision 1 — two kinds of rotation, stated by the root.** A rotation either
*retires* the old key or declares it *compromised*. The distinction is an
optional `"compromised": true` on the root-signed `revoke`. A retirement is
byte-identical to every revoke built before the field existed, and older
receivers ignore the field. Since no node had ever rotated, the meaning of a
plain revoke was still free to choose. It means retirement, because the
common case must not cost anything. Rejected: one mode in which every
rotation is a compromise, which would make routine hygiene re-sign and
re-push the node's whole history. Also rejected: honouring every key the
chain ever authorized, which would make rotation no answer to a leak, the
very case the issue exists for.

**Decision 2 — which checks accept a retired key.** Long-lived events accept
one: genesis, posts and their revisions, lifecycle events, channel messages,
file descriptors, mail and its acknowledgements. They are checked against the
current key and then every key retired without being called compromised
(`verifying_operational_keys`). Everything signed fresh for one exchange
still checks the current key alone: hellos, descriptors, requests, chunk
descriptors and withdrawals. So do trust objects and attestations, which
their issuer re-issues on rotation (§12; #622 for vouches, #623 for
attestation revocations). There an old-key signature is a stale copy or a
replay, never history.

**Decision 3 — a compromise response re-signs the node's own objects under
the same ids.** A content id does not cover the signature, so a re-signed
object is the same object to every peer. `resign_own_content` selects rows by
the signature itself: an object is re-signed exactly when it verifies under
one of the node's compromised keys. It runs after the rotation and again at
every startup, where it costs nothing on a node with no compromised key and
finishes a response that a stop interrupted.

A copy that another node already holds is not reached. It stays accepted
there, and a node that later pulls it from that carrier skips it *per
object*, as it does a trust object signed by a superseded key: a stale copy
must never end the response it arrives in. A carrier that has learned the compromise holds such a copy as
stale and asks for it again, so the re-signed copy reaches it and then the
nodes behind it (§8.11 "Stale copies", issue #672).

**Decision 4 — a rotation is saved before anything live changes.** It is also
journaled, because the node is running. The new key is staged beside its
file, `transitions.json` is replaced (the commit point), and only then is the
key moved into place. `NodeIdentity.load` finishes a staged key the chain
already names and discards one it does not. A save that fails leaves the
running node exactly as it was. Retired signing keys are kept under
`retired/` for one purpose: opening mail a peer sealed to the old key before
it learned the new one. Nothing signs with them.

**Decision 5 — the running node reads its identity at the moment of use.**
`LinkContext.node_identity` and `LiveDirectChat` read through
`LinkNode.identity` instead of holding a copy. A caller who opened a screen
before the rotation would otherwise go on signing with the revoked key. The
real-time listener and every standing anchor connector hold their own copy,
so a transport rotation hands them the new identity *before* it closes the
sessions keyed to the old one. Otherwise a reconnect could race ahead with the
key being retired.

**Decision 6 — the surfaces.** On the running node the action is **Link
status → Keys**. The screen shows the root fingerprint, which rotation never
changes, and each key's history. **Signing key** and **Transport key** each
open a screen with **Rotate** and **Compromised**, and each of those takes
one confirmation. On a stopped node the action is `python -m netbbs.admin
rotate-key {signing,transport} [--compromised] [--identity-dir DIR]`, which
refuses while a node process holds the database. Both surfaces write a
`rotate_node_key` audit entry. The Phase 4 exercise gained a rotation row.

Not built: a screen action that declares an already-retired key compromised.
The chain and `operational_key_history` accept that second revoke, and
nothing issues it yet.

### Issue #628 — real-time Link through an HTTP `CONNECT` proxy

Decided during the roadmap re-evaluation of 2026-09-18 (issue #612): a network
that permits outbound traffic only through an HTTP proxy is one of the settings
Link is meant for, and there asynchronous Link works (every outbound
`aiohttp.ClientSession` sets `trust_env=True`) while live chat does not.
Normative description: §8.10.

What the code does today, which set the shape of the answer:

- **Three places open a real-time socket, and they all end the same way.**
  `dial_realtime_session` (direct dials of a peer, a relay, an anchor or a
  linked channel's origin), `attach_relayed_session` (a party joining a
  relayed bridge, which writes the plaintext `NETBBS-BRIDGE/1` attach record
  before Noise starts) and `RealtimeRelay._handle_upstream_ready` (a relay
  forwarding to an upstream relay, issue #270, which never runs Noise at all).
  Each calls `asyncio.open_connection(host, port)` on an address taken from the
  peer's signed descriptor and then works on the stream pair. Nothing below
  that line knows or cares where the bytes go, so a tunnelled stream
  substitutes cleanly — including for the attach preamble, which is just more
  bytes written ahead of the handshake.
- **An outgoing-only node dials directly as well as through relays.** No
  real-time dial checks `outgoing_only`: a DM tries the target's advertised
  real-time addresses first, a linked channel dials its origin, and every
  participating node keeps anchor connections to the reliable nodes. So the
  gap is not the relay leg alone; on a proxy-only network every real-time
  path fails.
- **The advertised port is already independent of the bound one.**
  `realtime_advertised_port` exists and is what the signed descriptor carries,
  so a node can bind 8862 and advertise 443 behind a forwarder today.

**Decision 1 — one helper opens every real-time socket, and it tunnels when a
proxy applies.** All three sites call a single function in place of
`asyncio.open_connection`. When the environment names a proxy for the target,
the helper connects to the proxy, sends `CONNECT host:port HTTP/1.1` with a
`Host: host:port` header, requires a `2xx` answer, and returns the same stream
pair; Noise, the attach preamble and the relay pipe then run over it unchanged.
The target comes from a peer's descriptor, which is signed but whose addresses
nothing validates today, so the helper accepts only a strict authority — a DNS
hostname, an IPv4 literal or a bracketed IPv6 literal, and a port in range —
and refuses anything else before a byte reaches the proxy; otherwise a peer
could inject request lines into the operator's authenticated proxy. The target
host is sent to the proxy unresolved, because on a proxy-only network the local
resolver commonly cannot resolve outside names. Of the Link traffic, the proxy
sees what any on-path observer of a direct connection sees: the dialled address
and, on a relayed attach, the plaintext `NETBBS-BRIDGE/1` record with its
single-use attach token, which travels in the clear on a direct connection too.
Everything after it is Noise, authenticated end to end (§8.10). What a direct
connection does not have is the proxy's own credentials: with Basic
authentication (decision 4) they cross the `http://` leg to the proxy in the
clear, exactly as they already do for the asynchronous half's proxied requests. The descriptor-pinning rules for relay
attach addresses are unaffected because they compare the address before the
socket is opened. The upstream-relay site
is included although a relay is by definition reachable: the rule "every
real-time socket is opened here" is what keeps a fourth site from quietly
reopening the gap. Rejected: a proxy-aware variant of each caller, which is
three implementations of one handshake.

**Decision 2 — the proxy comes from the environment only, and it is the one
asynchronous Link uses.** A peer's Link endpoint is advertised as `http`, so
`aiohttp` sends that peer's boards and mail through `HTTP_PROXY`; the tunnel
uses the same variable, falling back to `HTTPS_PROXY` when only that is set, and
`NO_PROXY` exempts targets, all read with the standard library's own rules
rather than a parser of ours. Using the proxy that already carries this node's
asynchronous traffic is what keeps the two halves from disagreeing when the
variables differ. Only `http://` proxy URLs are supported. A SOCKS or `https://`
proxy URL that applies to a target fails that dial with a visible configuration
error and is never treated as absent: a configured proxy is the only way out,
and falling back to a direct dial would bypass the operator's egress policy
wherever direct traffic happens to be possible. Rejected: a `[link] proxy`
setting. It would be a second source of truth beside the one the
async half of Link already reads, and the two halves of one node could then
disagree about how that node reaches the network.

**Decision 3 — when a proxy applies, the tunnel is the only attempt.** There is
no direct dial first and no fallback to direct afterwards. A network that needs
a proxy usually drops rather than refuses direct outbound connections, so a
direct-first rule would make every live dial wait out its full timeout, once
per advertised address, before the path that works. This is also what `aiohttp`
does for the async half: with a proxy configured it uses it. A SysOp whose
proxy should not carry some peers says so with `NO_PROXY`, which is the
standard lever for exactly that. Rejected: tunnel-on-failure, for the latency
above and because it makes the path a dial takes depend on timing rather than
on configuration.

**Decision 4 — proxy authentication is Basic, from the same sources
asynchronous Link reads, and nothing else.** Credentials in the proxy URL's
userinfo, or failing that the `~/.netrc` entry for the proxy host (which
`aiohttp` consults under `trust_env`), become a `Proxy-Authorization: Basic`
header on the `CONNECT`. NTLM, Negotiate and Kerberos are out of scope: the
async half of Link does not speak them either (`aiohttp` does not), so building
them for live chat alone would leave a node with live chat and no boards. The
supported answer for such a network is a local authenticating shim that
presents an unauthenticated proxy on loopback, which serves both halves at
once; the operator documentation names that pattern.

**Decision 5 — no NetBBS change for the port; it is an operator recipe.**
Corporate proxies commonly allow `CONNECT` only to 443 (Squid's stock
`deny CONNECT !SSL_ports`). A reliable node that wants proxy-only callers to
reach it live advertises its real-time address on 443 with
`realtime_advertised_port` and forwards that port to its real-time listener.
NetBBS does not bind 443 itself — the same rule as the web transport (issue
#201) — and does not multiplex Noise with TLS on one port; a host whose 443 is
already taken by an HTTPS front end needs a second address, or a protocol
demultiplexer in front of both, and either is the operator's choice. Rejected for now: a
second advertised real-time address per node. The descriptor already carries a
list and dialers already try each entry in order, so adding one later is a
compatible change, but nothing needs it until a reliable node cannot move its
only real-time port.

**Decision 6 — the tunnel is bounded, and its failures are recorded where they
happen.** One timeout of the helper's own covers connecting to the proxy and
reading its answer, and the answer has a header-size ceiling, since
`dial_realtime_session` itself has no overall timeout and the anchor connector
calls it without one; a proxy that blackholes the connection, or accepts it and
then says nothing, must not hold a dial open. On every path that does not
return the stream — timeout, an oversized or malformed answer, a refusal,
cancellation — the helper closes the proxy connection and waits for it to close
before raising, since no caller holds the writer yet; anchor retries would
otherwise leak a socket per attempt. A refusal
(`403`, `407`, `502`, anything not `2xx`) raises a distinct transport error
naming the status. The helper also records the last tunnel outcome on a
node-owned status object, and a dial whose tunnel opened but whose handshake
then failed records that too — "tunnel opened, handshake failed" is its own
outcome and never shows as a success, because the anchor connector swallows every dial
exception and a caller's `DirectChatUnreachable` deliberately carries no
reason; the Link status screen shows the proxy in use and that last outcome
("tunnel refused: 407 Proxy Authentication Required"), and a change of outcome
is logged at WARNING once rather than on every retry. Callers keep the generic
unreachable message: a caller cannot fix a proxy and the SysOp now can see it.

**Decision 7 — a TLS-inspecting proxy is a known limit.** A proxy that
terminates or inspects TLS inside the tunnel (Squid `ssl_bump`, most
"SSL inspection" appliances) will reject a stream that is not TLS, and Noise is
not TLS. That shows up as a tunnel that opens and then fails its handshake, which is
the outcome decision 6 records. Wrapping real-time Link in TLS to pass such a proxy is
not part of this decision; it would be a separate one, taken only if a real
deployment meets it.

**Testing.** A real loopback `CONNECT` proxy in the suite (an asyncio server
that parses the request, rejects one without `Host`, optionally demands Basic
credentials, and pipes bytes), per the rule about real boundaries: a direct dial, a relayed attach
with its preamble, and an upstream-relay leg each through it; `NO_PROXY`
bypass; a `407` surfacing as the recorded outcome; a silent proxy and a
blackholed one both bounded by the timeout; an unsupported proxy URL failing
the dial rather than dialling direct; a descriptor address with CR/LF refused
before the proxy sees it; the proxy observing EOF after each failed setup. A ratchet test that no `asyncio.open_connection` remains in
`netbbs.link` outside the helper. The live check is the proxy-only node joining
the dogfood deployment and holding an anchor session to a reliable node.

**Not done, deliberately.** Managed DNS stays as it is: its heartbeat connects
direct on purpose, because the service publishes the address a check-in
arrives from and a proxy-only node has no address worth publishing (issue
#201). This
decision does not change that asymmetry, and the helper is not used there.
Implementation is its own issue.

### Issue #561 — the Link carry model: what the cap decides, and what "declined" means

Carry is opt-out: joining Link is the only decision a SysOp makes, and each
verified genesis becomes a local board, channel or file area on arrival, up to
`max_carried_boards` / `max_carried_channels` / `max_carried_file_areas`
(500 each). The issue asked whether the property that falls out of that —
*which* resources a node carries is decided by sync arrival order, and the
rest are declined forever — was chosen or accidental. It was accidental.
Normative description: §9.3.

What the code does today, which set the shape of the answer:

- **"Declined" is never recorded; it is inferred.** A genesis in `link_events`
  with no local row reads as "seen and declined" (`inventory_wanted_ids`), and
  three different histories produce that state: a quota refusal, a SysOp
  deleting a carried resource (the delete functions leave `link_events`
  alone), and a crash between `save_event` and `materialize_carried_board`,
  which are separate lane calls with no repair path. Nothing can tell them
  apart, so §9.3's promise of a visible local exclusion "represented honestly
  as 'not carried on this node'" had no implementation behind it.
- **Arrival order decides, and nothing revisits it.** Responders walk
  resources in sorted-id order; the requester dials configured seeds before the
  reliable roster. The 501st genesis of a type is refused and, being inferred
  as declined, never asked about again.
- **The declined state also starves sync.** A resource this node does not carry
  is absent from its `InventoryRequest`, so every responder carrying it sends
  its genesis and every event under it on every pass, under the one 200-event
  budget shared by all three types. Recorded as issue #669, with a
  reproduction.
- **The cap is exempt for what already landed**, so lowering it sheds nothing,
  and only boards have a readout (`Carried boards: N/500`); channels and file
  areas have none.

**Decision 1 — carry stays opt-out.** A node that joins Link carries what
arrives, and scenario 1's "small node joins a busy Link" keeps working without a
SysOp curating anything. What changes is everything past that default.

**Decision 2 — a genesis this node has accepted is in exactly one recorded
state: carried, offered or excluded.** *Carried* has a local row. *Offered* and
*excluded* are rows in a carry-decision record keyed by resource kind and id,
holding the state, when it was set and by whom (the node, or a SysOp). Nothing
is inferred from absence any more. Every transition is one transaction: the genesis save
with its carry outcome (materialization or the record), and each later move
between states — accepting, excluding, deleting, restoring — with the local row
and the record changing together. That needs non-committing variants of today's
`materialize_carried_*` and `delete_*` helpers, which commit internally. It
closes the genesis crash window the way issue #73 closed it for posts, rather
than adding a repair pass for it. On migration, every existing genesis without a
local row becomes *offered*: its history cannot be recovered, and offered is
the state that loses nothing and forces nothing.

**Decision 3 — past the cap a new resource is offered, not declined.** The
cap bounds what the node takes on automatically; it no longer decides what the
node can ever have. An offered resource is listed for the SysOp with its name,
description and origin node, and `[A]ccept` materializes it from the stored
genesis, replays any other events this node already holds for it (a migrated
resource, or lifecycle events accepted beside a genesis past the cap, would
otherwise be declared as known and never projected), and then the next pass
backfills the rest by ordinary inventory pull. Accepting is the SysOp's choice and is not
itself capped. A genesis whose name is already taken locally is carried under a
disambiguated local name, on automatic intake and on accept alike, and the SysOp
can rename it as any carried resource; names are not identities in Link.
Today such a genesis aborts one sync pass and is then never carried (issue
#671). While offered, a resource wants nothing further, for the reason
§8.8 gives for declined ones: asking about content this node has not taken on
does not terminate. Rejected: reordering automatic intake to prefer reliable
nodes, configured seeds or busier resources. Reliable nodes are never
protocol-privileged (issue #219, decision 1), and any order is still an order;
once nothing is lost past the cap, which resources arrive first decides only
which ones need no click.

**Decision 4 — the cap governs intake, not holdings, and a cap of 0 is a
curated node.** Lowering a cap sheds nothing; a SysOp prunes by excluding, which
is a choice about named resources rather than a second arrival-order rule run
in reverse. A cap of 0 offers everything new and carries nothing unasked, which
is scenario 2's "these ten, nothing else" without a separate mode. A slot freed
by an exclusion is not filled from the offered list: once a resource has been
put in front of the SysOp, accepting it is theirs to do, and an offer that can
silently turn into a carried resource later makes the list untrustworthy.

**Decision 5 — deleting a carried resource hides it; Restore un-hides it; Purge
deletes it (amended 2026-09-26, the maintainer's decision).** Deleting a carried
resource keeps its local row and everything in it and sets `link_hidden_at`:
callers and content administration no longer see it, it is not carried or
declared as carried, no new content is projected into it, and it is recorded as
excluded (`deleted`) -- all in one transaction with its audit entry. An
`Excluded` list under Link status shows it with `[R]estore`, which clears the
mark so the resource is back exactly as it was, and the next sync pass pulls
what arrived meanwhile; and `[P]urge`, behind a typed-name confirmation, which
deletes it for real and leaves it excluded (`purged`). After a purge, and for an
offer declined without ever being carried, `[R]estore` takes the resource on
again from its stored genesis, like an accepted offer. A hidden resource's name
stays taken, and creating or renaming onto it says which excluded resource
holds it.

The first form of this decision deleted the rows and restored by replaying
`link_events`, keeping this node's own users' envelopes and a local-moderation
overlay so the replay could be faithful. Mapping the code showed it could not
be: this node's own posts are keyed by a local content hash rather than their
event's content ID, so replayed they come back detached from their local
accounts; and a carrying node's moderation is not in the signed history at all.
Hiding keeps both exactly, costs one column, and leaves reclaiming the space to
an explicit Purge.

Exclusion applies only to a resource whose *current* origin, resolved as §9.4
resolves it, is another node — not merely one whose genesis came from
elsewhere, since a board transferred to this node makes it the authority peers
depend on for closure, moderator edits and further transfer. Removing a linked
resource this node is the current origin of is not a carry choice and is outside
this decision: for boards it goes through closure or origin transfer (§9.5, §9.4);
linked channels and file areas have no transfer or closure events yet, so their
delete path stays as it is today, a gap this decision records rather than
closes -- deleting such a resource stays a real delete. The SysOp can also
exclude an offered resource without ever carrying it. This answers scenario 5 (it stays gone, and
visibly so) and scenario 3 (pruning frees slots for automatic intake; nothing
that was offered is lost). Excluded and offered resources are never visible to
callers.

**Decision 6 — what a node does not carry, it tells its peers.** Offered and
excluded resources are declared in every `InventoryRequest` so responders stop
sending events under them, which is the fix for #669 and the #630 precedent
(events set aside under probation are declared as seen). The existing maps cannot
say it: their values are known-ID sets, and a responder sends every event whose
ID is absent, so declaring an offered resource's genesis alone still brings
every post. The fix therefore needs an explicit "not carried here" field,
signed, and sent only to a responder that has said it understands it: an older
responder rebuilds the signature payload without an unknown field and would
reject the whole request, and bumping the protocol version would stop hello
from completing, since versions must match exactly. So the responder advertises
an inventory capability — in its hello or descriptor — and the requester sends
the field only where it is advertised. Against an older responder there is no
suppression: nothing it understands can express it, and declaring received IDs
the #630 way would need a retained-ID store for events this node never keeps,
since posts, messages and descriptors under a resource without a local row are
dropped without reaching `link_events`. Mixed versions keep today's behaviour
until the responder upgrades. The declaration costs one resource ID per offered or
excluded resource; the carried maps beside it list every content ID of every
carried resource and are far larger, so keeping the whole request under the
responder's size limit is one problem for both. Issue #685 settled it for both
at once by paging the declaration (§8.8), rather than a separate bound on the
carry-decision record.

**Decision 7 — the bound stays a per-type count, and all three get a
readout.** 500 stays the default, and the declared scale (§2.3) stays small.
Link status shows `carried/cap` for boards, channels and file areas alike, and
the offered and excluded counts beside them. Rejected for now: a storage or
activity bound. Two of the three types are already bounded where their cost
concentrates — `max_remote_files_per_area` for a file area's catalogue and the
node-wide scrollback retention limit for a channel's history — and boards are
not: a carried board's posts are durable, unbounded-lifetime state (§8.9), and
no cap shape changes that, since a board grows after it is accepted. A storage
bound would need the node-wide disk accounting §13.9 lists as its own future
slice; until then a count is the number a SysOp can reason about when choosing
what to accept, and excluding a board that grew too large is the lever.

**Decision 8 — the vocabulary is the one Link Communities will use.** §6.5 says
carrying a Community carries its present and future members with per-resource
and whole-Community exclusions. In these terms a Community subscription is a
standing rule that accepts offers naming that Community; opting one member out
is excluding it; and a resource in no Community stays under the node-wide
automatic intake, so unfiled content is never uncarryable by construction.
Leaving a Community does not shed members already carried, for the reason in
decision 4. Whether a subscription has a bound of its own, and how a subscribed
Community is told apart from a same-named local one, stay Phase 6 questions;
nothing here forecloses them.

**Not done, deliberately.** Offers add a row per genesis this node already
stores, so they grow nothing a peer could not already make it store; bounding
genesis intake as a whole is a separate question, and this decision does not
change it. Bulk actions on the offered list (exclude everything from one
origin) wait until a list long enough to need them exists. Implementation is its
own issue, after #669.

### Issue #511 — HTTP transfer hardening — closed

Split out of PR #508, whose fixes did not converge. Normative description: §6.2.

**Decision 1 — two ceilings on an HTTP upload.** `max_upload_bytes` keeps
meaning file bytes, as over Zmodem; the request gets that plus a fixed framing
allowance. Rejected: one ceiling on the whole body, which makes a browser's own
framing refuse files within the configured maximum and, with a small enough
setting, makes browser upload impossible.

**Decision 2 — `HEAD` tells the truth.** It answers as a `GET` would, without
spending the grant. It used to answer 204 for every token so as not to be an
oracle for guessing them; with 256-bit tokens an oracle offers a guesser
nothing, and the page needs the answer. Rejected: a separate check route, which
exposes the same information on a second path.

**Decision 3 — probe, then stream.** The page probes with `HEAD` and saves
through `<a download>` only on a yes. Rejected: `fetch` plus `blob()`, which
buffers the whole file in the page; a CORS header on the endpoint, which widens
who may read a transfer response; and the `public_url` link as given, which
fails cross-origin. The page resolves the token against its own address
instead, which is same-origin and keeps a reverse-proxy prefix.

**Not done, deliberately.** The probe is not a reservation: a file deleted, or a
slot taken, between probe and `GET` still saves that `GET`'s error body. The
window is one round trip.

### Issue #711 — an art post's layout on the Link

A post drawn in the ANSI art editor keeps its lines: each line stays a line, and
only a line wider than the terminal wraps (§6.1). Which layout a post has is set
by the editor that wrote it, not by an author-managed flag. For a carried post
to keep its lines everywhere, and not only on its origin, the layout has to
travel with it. Normative description: §6.1 and §9.2.

**Decision 1 — an optional `layout` field on `board_post`.** `"layout": "art"`
for an art post. The key is omitted for prose, never `null`, per §7.2's omission
rule. No `netbbs_protocol` bump: §7.5 allows optional fields that old peers can
safely preserve, and they do. A node that predates the field verifies the
signature over the bytes as received, keeps the envelope and relays it
unchanged, and shows the post as prose, which is the intended fallback. A value
this node does not know is prose too, so a later layout degrades the same way.
Rejected:
- a marker inside the body, which every older node would display;
- a new event type, which older nodes would store opaquely and never show at
  all (§7.5).

**Decision 2 — the root carries it; edits follow.** An edit is written in the
editor that drew the post, so `board_post_edit` and the moderator edit carry no
layout: a revision takes its root's. The local `posts.layout` column is
meaningful on the root row. Rejected: a layout per revision, which would let one
edit turn a drawing into reflowed prose with nothing to say why.

### Issue #675 — an author's withdrawal on the Link

An author may withdraw their post: its text becomes "[withdrawn by author]"
(§6.1). An ordinary author edit to that text would reach every node, but a
node that carries the board with its own moderation, or that holds this
author's posts for trust review, holds an incoming author edit for approval.
There the withdrawn text would stay up until that node's moderator acted.
Normative description: §6.1.

**Decision 1 — an optional `withdrawn` field on `board_post_edit`.**
`"withdrawn": true` on the author's edit; omitted otherwise, never `false`,
per §7.2's omission rule. It is signed by the author's home node like any
author edit, so it needs no new authorization. No `netbbs_protocol` bump, for
the same reasons as issue #711's `layout`: §7.5 allows optional fields that old
peers can safely preserve, and they do. A node that predates the field
verifies and relays the edit unchanged and shows it as an ordinary edit, held
where it holds edits. That is the old behaviour, so nothing gets worse there.
Rejected:
- a new event type, which older nodes would store opaquely and never show
  (§7.5);
- reusing `board_post_tombstone`, which is origin-signed and terminal; a
  withdrawal is neither.

**Decision 2 — honored only for a withdrawal and nothing else.** A carrying
node lets the edit through without local moderation or a trust hold only when
it takes the text away and changes nothing else: the body is exactly the
placeholder and the subject is the predecessor's. Otherwise the flag would
carry new text past every node's moderators. Anything else carrying the flag
is an ordinary edit, moderated as one. A local tombstone still ends the chain,
and a post nobody here approved stays unpublished either way.

**Decision 3 — it clears the pin and the keep.** On every node, locally
(`posts.withdrawn`, `trg_posts_withdrawal_clears_flags`), so a withdrawn post
neither stays at the top of a board nor outlives its expiry.

### Issue #767 — the node map — decided

A caller had no way to see which boards the network holds, and a SysOp could
see only the nodes this one had met. Normative description: §8.12, and §8.2 for
`dial_in`.

**Decision 1 — callers and the SysOp, one screen.** The SysOp's view is the
same list with more rows and columns, and replaces the peer list behind Link
status. Rejected: a SysOp-only screen, which leaves callers the one audience
that needs a BBS list most; and two screens, which would drift apart.

**Decision 2 — guests through the level, not a toggle.** Guest login is an
account and no code branches on it (§4.6), so the map has a minimum level like
everything else a SysOp gates. Rejected: a guest switch on the map, which is
the first thing §4.6 rules out.

**Decision 3 — unverified candidates are the SysOp's alone.** A peer-list
candidate has completed no hello and names nobody who vouched for it, and
anyone can put a name on one. Introduced nodes are listed for callers: they
carry a verified bundle and name their carrier.

**Decision 4 — no opt-out.** A board missing from the list must mean callers
cannot reach it through this board. With an opt-out it could also mean its
SysOp asked, and a caller could no longer tell whether a missing board is
unreachable or merely unlisted; a list that says it may be incomplete is one
nobody trusts.
The one gap that remains is structural, that nodes pass on only nodes they have
met, and the title states it. Rejected: an `unlisted` descriptor flag. Leaving
quarantined and blocked nodes off the caller view is not an opt-out: it is
this board's own judgement, and such a node is unreachable through this board,
which is exactly what its absence tells a caller.

**Decision 5 — "last heard" is first-hand or signed by the node.** Contact
this node observed, or a descriptor time the node signed itself. Rejected: the
peer record's update time, which a peer list refreshes secondhand and which
made dead peers look live (issue #766); and the time a carrier's bundle
arrived, which says when this node asked, not when the other one ran. The
signed time is capped at when this node first stored it, because a skewed
or dishonest clock could otherwise date a descriptor years ahead and never
go stale.

**Decision 6 — dial-in addresses are signed by the board they describe, and
stated by its SysOp.** They travel in the descriptor because a descriptor
reaches every node that knows the board, introduced ones included, and a
carrier cannot alter it. They are stated rather than derived because a node
cannot see what is in front of its listeners (the issue #201 entry, Decision
6); the SysOp's screen suggests entries from the node's DNS name and
listener ports, and publishes nothing the SysOp has not saved, except an
`https://` `[web] public_url`, which the SysOp has already stated. Rejected: deriving them from
the listener ports, which on the shipped defaults would advertise ports that
nobody can dial from outside; and a new wire message, when an optional
descriptor field is how #669 already extended the descriptor. Plain `http://`
is not accepted, for the reason the #201 entry gives for the web listener.

**Decision 7 — a bad dial-in claim costs only itself.** Malformed entries are
dropped where they are read and never fail a hello. Rejected: refusing the
hello, as invalid profile claims are, which would let a typo in a display field
cut a node off from Link.

**Not done, deliberately.** A drawn topology: nodes do not record who passed
on a candidate, and edges beyond this node's own would be guesses. Jumping from
a node's detail view into its boards. Removing stale nodes by age.
Reachability claims beyond direct and introduced, such as whether Link mail
reaches a node.

### Issue #761 — the SysOp's live session monitor, snoop and break-in chat — decided

A SysOp had no live view of what callers were doing and no way to act on one
session beyond a message or a disconnect. The work is split across #762
(activity and idle), #763 (the Monitor), #764 (screen copy and snoop) and #765
(break-in chat). The decisions below are the maintainer's, recorded on the
tracker on 2026-09-27.

**Decision 1: an in-BBS screen, not a local command.** The monitor is
Operations → Node and sessions → Monitor, inside the SysOp console. All live
session state lives in the node process, and `python -m netbbs.admin` reaches
the node only through the database. Rejected: a local `netbbs top`. It would
need a new IPC channel and wire protocol, with no portable form (asyncio can't
serve Unix sockets on Windows), and it would add attack surface. A SysOp who
wants it locally connects to `localhost`.

**Decision 2: the Monitor shows places, never content.** A caller's activity
is a trail of place names, such as `Boards › Retro` or `Doors › Voidrunner`.
It never includes a message subject, a mail or direct-chat partner, a file
name or a search string. Idle time counts caller input only; transport
keepalive does not count.

**Decision 3: snoop is silent, and callers are told in advance.** A SysOp may
watch any live session, the caller's screen exactly as they see it, private
messages included, without the caller being told at the time. What a caller
may rely on:
- **Disclosure.** The last line of chat's `/help` says it: "The SysOp can
  watch any live session." The User Handbook's "What the SysOp can see" says
  it in full.
- **Record.** Every snoop is logged to the node log with the SysOp, the
  caller and the duration.
- **Passwords.** Masked input is never echoed, so it never reaches the screen
  copy that snoop shows.

Rejected:
- an on-screen "being watched" indicator, the classic BBS alternative, which
  the maintainer declined;
- no snoop at all. The per-session screen copy is needed for break-in either
  way.

**Decision 4: break-in chat puts the caller back exactly where they were.**
The SysOp takes over the caller's terminal for a two-pane chat. When it ends,
the caller's screen is repainted as their program left it, including anything
it printed meanwhile and a half-typed line, and they carry on. The mechanism
suspends the session's I/O and never its task (worklog, "Break-in suspends a
session's I/O, never its task").
- A break-in is refused during a binary transfer, and a transfer waits for a
  break-in to end.
- A break-in is refused while the caller is typing a password. A password
  prompt reached during a chat shows `*` there.
- A caller in a door keeps playing unattended during the chat, so the SysOp is
  warned first.
- Only the SysOp ends the chat. Every chat is logged.

Rejected: an "invite to direct chat" stand-in, which the maintainer judged not
enough; and cancelling the caller's task back to the main menu, which loses
their place.

**Decision 5: narrow terminals drop columns, never wrap.** Below 80 columns
the Monitor drops the address, terminal size and transport columns, in that
order. User, idle time and activity always stay. How narrow the console must
still work is decided in §3.4 (issue #662).

### Issue #836 — delegation short of SysOp — decided

A SysOp who stepped away left signups and held posts that nobody could act
on, and the only way to hand them over was level 255, which includes power
over the SysOp who gave it. Normative description: §5.6, with §4.3 and §5.2.

**Decision 1 — named staff permissions, with Co-SysOp as a preset.** Approve
accounts, manage accounts and moderate everything, granted per account, and
a one-step Co-SysOp preset that sets all three. Rejected: a Co-SysOp level
band such as 250-254, the classic BBS convention, because nodes already use
those levels as resource gates, and an upgrade would silently give console
powers to accounts a SysOp raised only to open a board; and account authority
as a new kind of moderator grant, which would stretch a per-resource table
into node-wide authority and still leave no one-step way to hand over the
node's routine work.

**Decision 2 — staff never reach the SysOp.** Staff act only on accounts below
255 that hold no staff permission, cannot raise anyone to 255, and cannot
grant anything. A second SysOp at 255 keeps full power, including over the first;
that is what 255 means, and staff is how to give less.

**Decision 3 — everyday account work, but no deleting and no 255.** Manage
accounts covers what a helper needs while the SysOp is away: disabling and
enabling, password resets for callers locked out, and levels up to 254 so a
helper can open level-gated boards to members. Deletion is permanent and, on a
Link node, retires the name (§4.3), where a disable can be undone by the SysOp
on their return. Level 255 is the SysOp's own authority and is given only by a
SysOp.

**Decision 4 — read and write grants pass level gates.** The bits already
existed and did nothing. Giving them meaning lets a SysOp open an
announcements board to a helper without a new concept. Age and verified-name
gates still hold, because they are facts about the person, not trust the SysOp
extends.

**Decision 5 — away is per person, and never outlives what it says.** A
node-wide notice would be wrong the moment one of two SysOps came back. A
return date that passes ends the notice. A notice without one is shown with
the day it was set and reminded to its owner at each login, so callers can
judge a stale one and its owner is prompted to end it. Rejected: requiring a
return date, which a SysOp who does not know when they will be back could
only guess.

### Issue #843 — aliases that pass for the SysOp; invitations nobody saw — decided

The persona test found `/nick InkWell[sysop]` accepted and shown as
`<~InkWell[sysop]~>`, beside a SysOp whose own status bar reads
"InkWell[sysop]". A direct-chat invitation to a caller in Who's online sat
unseen while the inviter waited. Normative description: §6.3.

**Decision 1 — the username beside every alias.** Refusing look-alikes
narrows the gap; showing `alias|username` closes it, since no alias can then
stand alone. Issue #64 kept the stream to the alias alone as less cluttered.
The field test is the evidence that clutter was the lesser cost.

**Decision 2 — every username is protected from aliases, only SysOp names
from display names.** An alias is chosen to be a different name, so refusing
one that reads as another caller costs nothing. A display name is meant to be
a person's own, and two callers named Anna must both be able to use it.

**Decision 3 — local aliases only.** A Link name is shown as `name@node` and an
MRC name with its board, and neither is granted here, so this node has nothing
to refuse. Rejected: comparing aliases against remote names too, which would
make a local alias depend on who happens to be linked.

**Decision 4 — tell a busy invitee, do not interrupt their screen.** The
notice uses the path a SysOp's message already takes into any screen. Rejected:
answering the invitation from inside every picker, which would spread the
invite handshake across screens that own their own keys; and writing into a
door or a Zmodem transfer, which would corrupt what that screen is drawing or
sending.

### Issue #899 — the speaker label on a chat line — decided

A chat line from a linked node read `<Phase4Ops@OutBound · outbound.netbbs.org>`:
fine once, a lot of width on every line of a conversation, and one run of one
color that did not show where the user ended and the node began. Normative
description: §4.4 and §6.3.

**Decision 1 — the friendly name alone, qualified only when shared.** The DNS
name stays on the screens that have room for it. On a chat line the friendly
name is the only thing marking where a caller comes from, which #843 Decision 3
relies on, so a name another known node or this BBS also uses keeps its
qualifier, in the form the node map already used. The existing caution for an
undismissed cryptographic-identity observation stays in front of the line.
"The same name" includes one that reads the same (issue #900).

**Decision 2 — `Name · abc123` replaces `Name abc123`.** The node map's form
without a DNS name could not be typed back; with the reserved `·` it can, and it
matches the DNS-qualified form. Rejected: a fingerprint prefix alone, which
drops the name a reader knows the node by.

**Decision 3 — style the parts, not the whole.** Brackets and `@` muted, the user
in the speaker color, the node in `NODE_COLOR`. The same split fixes the
alias label: it read `Quill|lena_h` with the username at the alias's weight,
so readers could not tell which was the alias and the alias no longer read as
the chosen name. The alias now leads in its color with the username muted.

**Decision 4 — `alias (username)`, not `alias|username`.** Color alone did not
settle it: a pipe has no settled meaning, and a reader could not tell whether
it joined two names, separated them, or which side was the account.
"Name (handle)" already reads as a name and the account behind it, needs no
fallback on a terminal without UTF-8, and keeps #843's rule that no alias
stands alone. Aliases may no longer contain parentheses, so none can carry a
forged second account, and one set before that is no longer shown: with the
account's own marker inside it, the real username beside it no longer keeps it
honest. The chat status bar, which showed `username (alias)`, now reads
`alias (username)` like the stream, and `/names` marks MRC callers `(on MRC)`
as `/who` does. The verified-name unit keeps its own `(=...=)`
marker. Rejected: `aka`, English and longer on every line; an arrow, which has
no settled meaning and could read as "talking to".

Rejected for Decision 3:
restricting node friendly names to the alias character set, which would make
this node refuse the hello of any existing peer whose name uses one, and would
undo #807's quoting of names containing `@`.

### Issue #900 — look-alike node names — decided

Friendly names were compared by Unicode form and case only, so "0utBound" beside
a known "OutBound" raised no warning and needed no qualifier. With #899 dropping
the DNS name from chat lines, that left the friendly name as the only anchor in
exactly the place where passing for a familiar node pays. Normative description:
§4.4.

**Decision 1 — two keys for two questions.** "Could a reader confuse these?"
uses the presentation skeleton (`look_alike_key`): the cryptographic-identity
warning, and every screen that qualifies a shared name (chat, node map, Who,
the board-origin picker). "Is this the same name?" keeps the exact key: a rename
under one fingerprint, the claim history's deduplication, and resolving a typed
reference. Rejected: resolving typed references by skeleton, which would let a
look-alike node receive what a caller addressed to the real one.

**Decision 2 — DNS names are not folded.** Registration already makes them
unique, and `presentation_skeleton` drops `-` and `.`, so `out-bound.example.org`
and `outbound.example.org` would become one. Screens that show a DNS name show
all of it, so Who and the board-origin picker add a technical identity only
where full labels could still be confused: the friendly names read alike and
the DNS names do not tell the nodes apart, because one has none or both share
it.

**Decision 3 — this node's own look-alike name is a warning, not a refusal.** A
SysOp who renames their node to read like a node it knows is told so and the
name is kept: two hobbyists choosing one name is harmless, and the nodes that
know both already flag whichever arrived second.

### Issue #929 — CP437 terminals: a character set per session — decided

NetBBS sent UTF-8 to everyone, with a Unicode or ASCII choice of decoration.
Classic BBS terminals such as SyncTERM read bytes as CP437, so they showed every
non-ASCII character as two or three characters of noise and never saw ANSI art
as drawn. Normative description: §3.2, "Character set per session".

**Decision 1 — map at `Session.write`, with a curated table.** Every text write
already goes through `Session.write`, so one mapping covers every screen,
caller-written text, preset names and copy with `—` or `…` alike. The table
gives NetBBS's own glyphs deliberate CP437 substitutes. Rejected: a glyph set
passed to every renderer in place of the Unicode-style flag. That flag reaches
975 call sites in 29 files, and even then caller text and ordinary copy would
still carry characters CP437 lacks, so it would need the central mapping as a
safety net anyway.

**Decision 2 — the preference is Auto, Unicode, CP437 or ASCII.** Auto is the
default, and an explicit choice beats detection, so one caller can use SyncTERM
at home and PuTTY at work. The old style preference migrates: off becomes
ASCII, on becomes Auto. Rejected: keeping a separate style flag beside the
character set, which would allow meaningless pairs such as ASCII with Unicode
decoration.

**Decision 3 — an undetected Telnet caller gets ASCII before sign-in, including
the SysOp's banner.** CP437 would garble a UTF-8 terminal and UTF-8 garbles a
CP437 one; ASCII is readable on both. After login the "which line looks right?"
question settles it. Rejected: guessing UTF-8 (the pre-#929 behaviour, which
gave SyncTERM callers noise on their first screen) and guessing CP437.

**Decision 4 — ASCII is true 7-bit.** Before this, turning Unicode off only
swapped decorative glyphs; em dashes, ellipses, arrows and accented letters in
posts still arrived as UTF-8. Now nothing above 0x7F reaches an ASCII session.

**Decision 5 — substitutes keep the display width.** Width is measured once, on
the Unicode text, and every substitute fills exactly the cells its original
did. That keeps every layout rule in §3.2 and §3.6 valid unchanged.
`truncate_to_width` asks for the set's own ellipsis instead of mapping `…` to
`...`, which would be wider. Rejected: re-measuring after mapping, which would
make every width-aware screen depend on the caller's character set.

**Decision 6 — `ansi` means CP437.** That is the PC-ANSI convention, and it is
what older SyncTERM versions reported; the question after login is the safety
net for a UTF-8 terminal that says `ansi`.

**Scope.** CP437 is the only code page; SyncTERM's other fonts (CP850, CP866 and
others) are not detected or served, though the layer can take another codec
later. The CTerm device-attributes answer (`CSI = 67;84;101;114;109;… c`) would
also identify SyncTERM on any transport, but is not used until TTYPE proves too
weak. SysOp art storage with SAUCE, art with live slots, hand-drawn menu items
and animation pacing are later steps of #929 and build on this layer.

### Issue #929 — SysOp art: SAUCE and live slots — decided

Steps 3 to 6 of #929. Normative description: §3.2, "SysOp art: storage and
SAUCE" and "Art with live slots", including its list screens, hand-drawn items
and pacing.

**Decision 1 — the `.ans` file is the only source.** SAUCE, pictographs and
iCE colours are handled when the file is read. Rejected: a normalised copy in
the database with its metadata, which would give two sources of truth while
SysOps edit art files over SFTP, and would need a migration.

**Decision 2 — the art path owns the control-range pictographs.** Only art
treats 0x01–0x1F and 0x7F as glyphs; everywhere else they stay controls, so
caller text cannot smuggle control bytes in by calling them art.

**Decision 3 — iCE colours for everyone.** Blink with a background colour
becomes a bright background on every session. Rejected: switching the
terminal into iCE mode with CTerm's own sequence, which only SyncTERM
understands and would still leave UTF-8 terminals blinking.

**Decision 4 — too-wide art falls back.** Rejected: wrapping or cutting it,
which turns a drawing into noise.

**Decision 5 — plain tokens in the art, not ENiGMA½-style codes plus a theme
file.** ENiGMA½ marks views with `%VM1`-style codes and sets their size and
style in a separate `theme.hjson`. `{menu WxH}` tokens keep position, size and
style in the one file the SysOp draws, and give the console something to
check. Rejected for the same reason: region definitions in SAUCE comments,
which editors strip and nobody sees.

**Decision 6 — items that do not fit fall back to the generated menu.**
Rejected: filling the region and moving the rest behind a "more" entry, which
silently moves items a caller can use out of sight.

**Decision 7 — lists in art show a compact row.** A list region shows the
number, the name and one column per screen; descriptions and tables stay on
the generated list. Rejected: drawing the generated list inside the region,
whose tables and descriptions do not fit a drawn box and whose page arithmetic
assumes the full screen; and hiding NetBBS's own list under the art, which
shows stale rows.

**Decision 8 — hand-drawn items are found, not declared.** A drawn `[K]` is
the item, with no extra token. Rejected: a sidecar map or SAUCE comments
holding coordinates, which editors strip and SysOps cannot see.

**Decision 9 — items a caller cannot use are blanked.** Chosen over dimming
them after a mockup: blanking matches the generated menu, which hides them,
and does not advertise SysOp keys. Dimming keeps the art's full shape but
shows keys that do nothing, and relies on a colour some terminals render
faintly.

**Decision 10 — pacing on the server, not CTerm's speed sequence.** SyncTERM
can emulate a line speed itself (`CSI Ps1 ; Ps2 * r`), but bytes already sent
cannot be skipped, it works only in SyncTERM, and turning it off again queues
behind the art. Server-side pacing works on every terminal and stops at once
on a key. The skipping key is swallowed, not passed on as Voidrunner passes
its interrupting key, because a prompt follows the art. A time limit, when the
SysOp sets one, sends the rest at once; a limit that sped the art up instead
was rejected, since the speed is part of how the art was meant to look.

**Decision 11 — a chosen speed plays to the end; a time limit is the SysOp's
choice.** Until v7.18.1 every paced draw stopped after a fixed 5 seconds and
dumped the rest, which the maintainer found looked broken: a SysOp who picks
2400 bps wants the art drawn at 2400 bps. A per-banner **Time limit** (off, 10,
30 or 60 seconds) now does what the cap did, and is off by default, so a speed
is obeyed unless the SysOp asks otherwise. A caller can still skip with any
key. Rejected: keeping a fixed cap, and a longer fixed cap, both of which cut
off a slow piece the SysOp chose the speed for.

### Issue #1004 — the access map — decided

Dogfooding found that a SysOp could not tell what giving an account a level
meant without opening every board, file area, channel, door, Community and
setting, nor be sure a promotion opened nothing unwanted. Normative
description: §5.7. Step 1 (#1005) builds the map; the screens follow.

**Decision 1 — one map, held to the checks by tests.** The map is computed
from the same effective-level functions the checks use, a test compares each
gate with the real check at every threshold, and a scan of the source fails
on any level check the map does not account for. Rejected: routing every
check through the map, which would rewrite every flow's gate for no change in
behaviour; and a hand-kept list, which is the drift the map exists to end.

**Decision 2 — levels only; conditions named, grants left out.** Age,
verified-name, members-only and hidden appear as a gate's conditions, and
read or write grants are not on the map, because they belong to accounts,
not to levels. Rejected: answering "what can this account do" in the same
structure, which the change preview (#1006) does per account where it needs
to.

**Decision 3 — a write gate opens at the higher of its read and write
levels.** That is what a caller experiences. The gate still shows its own
write level and where it comes from.

**Decision 4 — the change preview is a screen, not a question** (#1006). It
shows its content with `[A]pply` and `[B]ack` in the action bar, as §3.5 asks
of every screen. Rejected: a yes/no prompt after the list, which §3.5 keeps
for irreversible or network-touching actions, and a level change can be
undone. A change that opens and closes nothing skips the screen, since
there is nothing to check. Refusals are checked first, so a SysOp never reads
a preview for a change that would then be refused.

**Decision 5 — levels run from 0 to 255.** Nothing enforced the range before:
an account could be created at, or changed to, a negative level or one above
255. Both are refused now.

**Decision 6 — level names are labels in node configuration** (#1009).
Rejected: named levels as the stored value, with gates and accounts
referring to a name, which would make renaming a level a migration of every
gate and leave Link genesis events, which carry numbers, needing a mapping.
A JSON object in the config table, rather than a table of its own, because
it is a handful of short strings read together.

**Decision 7 — a name is typed where a level is asked for on the user
screen and the Levels screen, not in the resource editors.** Those are where
a SysOp thinks in names (promote alice to Member). A resource editor's level
fields keep taking numbers, and show the name beside the value.

### Issue #992 — automatic level promotion — decided

A public node either checked new accounts by hand every day or opened
everything to brand-new accounts; ReLink ran its own cron script. Normative
description: §4.3.

**Decision 1 — checked at login, not on a timer.** A level matters only
while the account is logged in, so the login is the moment to decide, and
the caller can be told. Rejected: a periodic sweep, which promotes accounts
nobody is using and needs a task of its own.

**Decision 2 — a level set by hand takes the account out of the rules**
(the maintainer's decision). Otherwise a rule undoes a SysOp's demotion at
the next login. The account screen turns the rules back on. At upgrade only
accounts the moderation log shows demoted start marked; earlier promotions
cannot be told from ReLink's scripted ones, and are left free to climb.

**Decision 3 — logins counted on the account.** Session history keeps at
most a few rows per account and prunes them as others log in, so a quiet
newcomer's count could fall. `users.login_count` starts from the rows still
there.

**Decision 4 — the node acts, and the log says so.** An automatic promotion
is logged with no acting account ("(system)"), not in a SysOp's name: no
person made that change at that moment. Rejected: naming the SysOp who set
the rule, which is what ReLink's script did.

### Issue #993 — who may post on a Linked board — decided

ReLink Linked an announcements board, and any caller of any node carrying it
could post there: a carried post was held to nothing but the board's identity
policy. Normative description: §9.3.

**Decision 1 — origin authority, not a recommendation.** The origin chooses
who posts, and every carrying node enforces it, like closure. Rejected: a
local "accept carried posts" switch on each node, which cleans one node's
copy and leaves every other node's to its SysOp.

**Decision 2 — three modes, the origin chooses** (the maintainer's
decision): anyone, origin starts threads, origin only. "Announcements" and
"announcements with discussion" are both common.

**Decision 3 — its own event, latest wins, outside the lifecycle chain.**
Rejected: a field in `board_genesis`, which cannot change and so would only
reach boards Linked afterwards; and a chained lifecycle event, since an
origin keeps only its latest lifecycle event and a setting would push an
earlier transfer out of reach of a peer that missed it.

**Decision 4 — no capability gating.** Every node is updated before origins
set this (the maintainer's call); an older node would refuse the unknown
event type (#1022).

### Issue #632 — delivering attestations from a node nobody can dial — decided

A remote identity attestation used to travel only by being pulled from its
issuer, with a cursor, and the issuer enforced its recipient list when it was
pulled. A node nobody can dial cannot serve a pull, and most real nodes are
outgoing-only, so nothing such a node published about its callers reached
anyone. Trust objects solved the same problem by depositing at the issuer's
relays (#627); that does not carry over, because an attestation carries a
caller's verified birthdate or real name and must reach only its recipients.

**Decision 1 — a sealed, signed snapshot per recipient** (the maintainer's
choice). The issuer sends each recipient node one `sealed_attestation_bundle`:
every signed attestation and revocation that recipient should currently hold
from it, unchanged, sealed to the recipient's key (§10's `encrypt_for`) and
signed by the issuer. Its outer envelope names only the issuer, the recipient
node, a `sequence` and a time. The issuer pushes it directly when the
recipient can be dialed and otherwise deposits it at the *recipient's* relays,
named in the recipient's descriptor. A relay keeps one slot per (issuer,
recipient), in its own table so it never takes a mail slot; a newer bundle
replaces an older one. What replaces the cursor is the recipient's last
applied `sequence` per issuer: an older or equal one is refused, so a relay
cannot replay a stale snapshot. A recipient that was away needs only the
newest bundle, which is what its slot holds. Rejected: a sealed *pull*, which
still needs a dialable issuer; and a push of individual changes, which brings
the cursor back as a per-recipient ledger and needs acknowledgements to notice
a relay losing one.

**Decision 2 — a snapshot is authoritative, so removing a recipient retracts.**
Whatever a recipient holds from an issuer and the latest snapshot leaves out is
forgotten, audited as "withdrawn for this recipient". Removing a recipient
sends it a final, empty snapshot, retried until a route takes it and given up
after 90 days. This reverses the cost #596 Decision 3 accepted, that a removed
recipient kept what it held until expiry. A revocation is not used for this,
because a revocation is a universal signed statement: sent to a removed node,
it could be passed to another and retract a value still valid there, whereas an
empty snapshot is sealed to and names one recipient. The cost: a relay that
withholds a newer bundle delays a retraction by up to the attestation's 90-day
lifetime, the window §5.5 already accepts for an issuer that goes dark.

**Decision 3 — a relay learns that A sent B something, roughly how much, and
when.** The plaintext is padded to a power of two from 4 KiB, so the size says
little, and no user identifier is outside the seal. The relay is the
recipient's chosen agent, under its signed consent, and already sees more of
a letter (sender and recipient user IDs). Because a bundle can displace
another in its slot, a relay never takes one on an unverifiable claim, unlike
a letter: the deposit carries the issuer's own hello bundle, which
authenticates itself against its root key (whose hash is the issuer's
fingerprint), is merged with any chain the relay already holds so a stale one
cannot revive a compromised key, and names the key the snapshot's outer
signature must verify under. Only the issuer can fill or replace its slot; a
deposit in another issuer's name is refused, whatever sequence it claims. The
recipient still checks everything itself.

**Decision 4 — the recipient's relays, learned from its descriptor.** Sealing
needs the recipient's key and verifying needs the issuer's, and two
outgoing-only nodes never complete a hello, so both sides accept a node known
by introduction (`resolve_known_signing_key`, #627 Decision 5). A recipient
with no route — no relay published, or none the issuer has met — is a visible
per-recipient status on the issuer's Published identity screen, never a silent
drop.

**Decision 5 — one path, and no overlap.** Everything moves to the sealed
push. A bundle is sent only to a recipient, and deposited only at a relay,
whose descriptor advertises `sealed_attestations`. The plan was to keep the
pull for one release for recipients that do not; the maintainer decided on
2026-10-02 to remove it in the same release instead (issue #1046), with its
route, its signed request type and its cursor table (migration 116). The
consequence, accepted: a node on v7.14.0 or earlier stops receiving
attestations from an upgraded issuer until it upgrades; the issuer's
Published identity says so per recipient ("needs a newer NetBBS to receive
this"), and the caller's count does not include it. Rejected: keeping the
pull for dialable issuers, which would give receivers two sets of semantics
and leave removed recipients unreachable.

**Bounds.** Snapshot plaintext at most 768 KiB (sealed and encoded it stays
under the 2 MiB request limit), refused visibly beyond that; at most 4,000
objects. A relay holds bundles from at most 32 issuers per recipient and drops
one after 90 days. An issuer sends to at most 20 recipients per sync pass,
re-sends when a recipient's snapshot changes and at least every 7 days, and
uses a time-based `sequence` so a restore from backup does not move it
backwards.

### Issue #1036 — self-verifying evidence is verified — decided

`ingest_trust_objects` stored an embedded `self_verifying` signal and counted
it on its label: a configured reporter in scope could quarantine a node,
together with a second domain, with "evidence" that proved nothing.
Normative description: §12.6.

**Decision 1 — only signed equivocation reproduces.** It is the one violation
whose evidence a receiver checks against its own record of the subject's keys,
trusting nothing about the issuer. Revoked-key use, invalid authority and an
invalid signature delivery stay observer claims when they come from elsewhere.

**Decision 2 — unverified is kept, not refused.** The signal is stored and
shown as unverified, so a SysOp sees what was claimed, and re-checked once the
subject's keys are known. It counts toward no quarantine.

**Decision 3 — verified evidence counts toward the threshold, and is not
promoted to a local observation.** This supersedes the earlier §12.6 sentence
that made reproduced evidence the receiver's own observation, written for the
digest path. With automatic issuance (#589), promotion would let one issuing
node quarantine its subject everywhere; the two-domain rule keeps the
independence §12.7 is built on. Existing signals stop counting until their
evidence reproduces here (migration 112; release note).

**Decision 4 — the digest-evidence path is deleted.** `fetch_trust_evidence`,
`verify_evidence_bytes` and `activate_reproduced_digest_signal` had no
production caller, and the last promoted reproduced evidence to a local
observation, which Decision 3 rejects. Equivocation evidence always fits
inline, and fetching an issuer-named locator was network access with no
remaining use.

### Issue #914 — a compromise reaching a node that knows the signer only by introduction — decided

The Phase 4 exercise's row 9: B knew A only through R. After A rotated its
signing key as compromised, R served B a copy of A's post signed with the old
key, which B accepted, because B's bundle for A predated the rotation and still
held that key as current. Nothing failed, so nothing asked for a fresher
bundle. Normative description: §8.11, "Stale bundles".

**Decision 1 — the carrier sends the chain beside the content** (the
maintainer's choice). Rejected: refreshing introduced bundles by age, which
leaves a window of up to the refresh interval and works only if a carrier
answers an identity request; and gossiping third-party `key_transition`
events, which would overturn the rule that a transition event is accepted
only from its own subject.

**Decision 2 — introduced identities only.** A direct peer's chain comes from
the peer, by hello and gossip; a third node is never an authority over it. An
identity the receiver has never been introduced to is not created from a chain,
since a chain carries no descriptor.

**Decision 3 — merged, never replaced.** Both a carried chain and a fresher
bundle are merged into what is on file, checked against the stored root key.
Without that, a stale bundle could undo a learned compromise.

**Decision 4 — bounded, and absent when there is no news.** At most 32 chains
of at most 64 transitions; a chain holding only the first authorization is not
sent. An ordinary response is unchanged, an older requester ignores the field,
and an older carrier simply sends none. Longer chains fall back to the
stale-bundle refresh.

### Issue #672 — carriers holding copies signed by a compromised key — decided

After a compromise rotation the origin re-signs its own content, but a re-signed
object keeps its content id, and inventory diffs by content id: carriers kept
serving the old-signed copies, and the re-signed ones never spread past the
origin's own peers. Normative description: §8.11, "Stale copies".

**Decision 1 — a node that learns a compromise treats what that key alone
signed as missing** (the maintainer's choice). It stops declaring and serving
those copies, keeps their projections, and takes the re-signed copy in place
when the ordinary inventory exchange offers it. No change on the wire. Rejected:
the origin pushing its re-signed events once to each peer, which reaches only
its own peers and leaves every carrier further out, which already holds the old
copy, as stale as before; and naming the signer in every inventory entry, which
spreads through every hop but changes the inventory format of every event for a
rare case.

**Decision 2 — learned from any source.** A direct peer's revoke or hello, an
introduction, or a chain a carrier sends. A carrier also sends the chain of a
signer whose content the requester only declares, when that signer has marked a
key compromised, since a requester holding a stale copy is served nothing of
that signer's (§8.11).

**Decision 3 — bounded.** An identity's chain is looked at again only when it
has changed since the sweep last saw it, and a hello or introduction looks only
at the identity it changed, so an ordinary hello verifies nothing. Each newly
compromised key costs one look through the stored events of the kinds inventory
carries, once per process; a stale mark is stored, so a restart does not forget
it. Mail, its acknowledgements and key transitions travel outside inventory and
are left as they are.

**Decision 4 — only an event's own signer speaks for it.** A chain is any
root's to write, and nothing stops a node from authorizing another node's
public key as its own and then declaring it compromised. A copy is therefore
tested only against the compromised keys of the identity it names as its
signer (author, origin), never against another identity's; such a claim
affects nothing but the claiming node's own content. Rejecting a chain that
claims a key another identity already holds was considered and not done: the
receiver cannot know every identity's keys, so the rule could not be enforced
consistently, and attributing by signer already makes the claim harmless.

### Issue #1081 — a console resource opens on its own fields — decided

The first field test's SysOp had to press `[E]dit` on every board, area and
channel before changing anything, one extra screen on the most common path.
Normative description: §3.5.

**Decision 1 — one screen, still a draft** (the maintainer's decision).
Changes wait for `[S]ave`. Rejected: saving each field as it changes, the way
the Profile does. A resource's fields are checked together (a blank name, a
door's executable against its arguments), a Linked board's or area's name and
description travel to its peers once per save, the moderation log keeps one
entry per save, and with the cursor resting on a live field a stray Enter or
arrow key would change a resource at once.

**Decision 2 — fields without letters, actions by their keys.** On every one
of these screens a field letter collided with an action letter (on a board,
`D` was Description and Down, `R` Read level and Remove, `P` Pinned and
Pending posts; on a door, `D` was Description and Delete). Fields were chosen
by the cursor alone until issue #1158 numbered every editor's fields (its
Decision 8); numbers cannot collide with the action letters. Rejected:
keeping field letters and moving the actions to a second page, which adds a
keystroke to every action instead of removing one from every edit.

**Decision 3 — a changed draft hides the actions.** While the draft differs
from what is stored only `[S]ave` and `[B]ack` are offered. Otherwise `[U]p`,
`[L]ink` or `[S]tart service` would act on a resource whose screen shows
values that are not stored yet. It also settles the one clash between two
actions: on a door, `[S]ave` and `[S]tart service` are never offered together.

**Decision 4 — six screens, no more.** Accounts keep their screen: each key
there is a separate, confirmed operation with its own log entry, not a field
of one form. The settings screens already open into their editors, and the
network and login limits screen is a hub of six groups that would not fit
80x24 as one form.

### Issue #1082 — a minimum age can require a verified age — decided

A SysOp could not run an area that needs a verified age: every age gate
accepted a self-entered birthdate when the account had no age attestation.
Normative description: §5.5.

**Decision 1 — shaped like the name requirement.** A nullable
`age_requirement` (`NULL` or `verified`) on boards, file areas and channels,
`default_age_requirement` on Communities, and an MRC open-room setting, with
the same Community cascade. Rejected: a separate boolean, which would not
inherit the way `NULL` does, and folding the flag into `min_age`, which would
change the meaning of a stored number. Existing gates are `NULL` on upgrade, so
none changes meaning.

**Decision 2 — old enough by one's own birthdate is not hidden.** Such a
caller sees the resource marked "needs verification" and is refused on entry
with how to get verified, mirroring the name requirement. Too young, or no
birthdate, still hides it: those callers have nothing to act on.

**Decision 3 — a local SysOp passes; nobody else does.** *Amended
2026-10-06.* The issue first decided that nobody bypasses the gate, level 255
included, matching the name requirement. The maintainer reversed that for both
gates: a SysOp could otherwise be locked out of an area on their own node, and
level 255 already overrides every level gate and moderator grant. Level 255
now passes the minimum age, the verified-age requirement and the name
requirement (§5.5); staff permissions and level 254 still do not, and remote
authors are still held to their own attestations. Rejected: a bypass for staff
too, which would let a delegated role skip a legal gate the SysOp set.

**Decision 4 — carried over Link as a recommendation.** A genesis carries
`default_age_requirement` beside `default_name_requirement`, omitted when
unset, so a genesis from a node that sets none is unchanged. A carrying node
stores a known value and drops anything else.

**Decision 5 — edited in the Min age field.** The editors show
"18, verified only" and take `18v`, so no editor gains a row: the area and
channel screens must still fit 80x24 whole.

### Issue #1105 — a gated resource names its gates on entry — decided

A caller entering a gated board, file area or channel saw nothing about why it
was gated until someone was refused. Normative description: §3.6.

**Decision 1 — one header line, only for gates that restrict someone.** The
values are the effective ones after the Community cascade, the ones the checks
use. A level of 0, an explicit minimum age of 0 and a write level no higher
than the read level are left out, and an ungated resource gets no line, so most
screens are unchanged. Rejected: a line on every resource ("Requires:
nothing"), which adds a row everywhere to say nothing.

**Decision 2 — shown to everyone who can enter.** A caller who already passes
learns why others cannot; a SysOp, who passes every gate (§5.5), sees what
the resource asks of callers. Rejected: staff and moderators only, which hides
the explanation from the callers it is for.

**Decision 3 — on the entry screens, not in lists.** Board, file area and
channel entry carry the line. Lists keep their "needs verification" note and
the console's gate column (§3.6); a row has no room for the full set, and a
list's slot art (§3.2) is the SysOp's own drawing.

**Decision 4 (issue #1115) — the caller's unmet gates are marked on the same
line.** A caller who could read a board but not post saw "Requires: level
255+ to post" and, below it, "Read only: posting needs level 255.", the same
fact twice. The gates the caller fails, asked with the access checks
themselves (`unmet_gates`), are drawn in `ERROR_COLOR`, with ` (not met)` for
an ASCII caller, and the separate line is dropped wherever the marked gate
explains it. The maintainer's wording was "the warning colour"; it is the
error colour because `WARNING_COLOR` (amber, 214) sits next to `GATE_COLOR`
(orange, 208) and the mark would not show. Rejected: keeping both lines and
rewording the second, which still says one thing twice.
When the line is too wide for the screen, the unmet gates move to its front,
since it is cut from the end; and the separate line is dropped only when every
unmet gate survives the cut whole (`unmet_gates_shown`), so a narrow screen
keeps the reason.

### Issue #1110 — a SysOp corrects a caller's display name and birthdate — decided

Only the caller could set their display name and birthdate (Profile → Name &
details), so a SysOp could not fix a typo or remove a display name that had to
go. Normative description: §5.5 and §5.6.

**Decision 1 — the same reach as a password reset.** A SysOp may edit any
account, and a staff member with manage accounts may edit an account below 255
that holds no staff permission, never their own. Both fields are details of
the account the way its password is, so they follow that permission rather
than a new one. Rejected: SysOp only, which would leave the helper who runs
the node while the SysOp is away unable to remove an offending display name.

**Decision 2 — the caller's own rules, and one more for both.** The same
display-name rules as the caller's own edit apply (the reserved `=` marker,
the byte cap, the look-alike and staff-title checks of issues #835 and #843).
A birthdate may be neither in the future nor before 1900-01-01, for the caller
too: a typo such as `0198` would otherwise make anyone old enough for every
age gate. Blank clears either field, which a SysOp needs. The caller's own
screen takes the same keys for the same values (Decision 6).

**Decision 3 — audited, without the birthdate.** Each edit is recorded in the
account's admin history with who made it. A display name is shown to everyone,
so the record keeps the old and new name; a birthdate is private unless its
owner shows it, and the history is read by every SysOp and manager, so the
record says only that it was set, changed or cleared.

**Decision 4 — verified values are separate.** An age or real-name
attestation is not touched by these edits and still decides every gate; the
account screen says so when one is on record. The caller's own visibility
settings for both fields are left as they are. Display names do not travel
over NetBBS Link, so nothing is carried.

**Decision 5 (issue #1115) — revoking is an action of its own.** Clearing a
birthdate left the verified age standing, which is Decision 4 working, but
there was no way to take a verification back at all. Whoever may verify may
now revoke (`revoke_attestation`): a SysOp from the account screen's
`[V]erification: revoke`, offered only while one is on record, or anyone with
"Can verify identity" from the verify screen's `[R]evoke`. It asks first,
names the gates that will refuse the caller again, and is recorded in the
account's history. A verification the caller had shared over Link needs no
step of its own: the next sync signs a revocation for every live object whose
attestation is gone (`reconcile_issued_attestations`). Clearing the plain
field still never revokes. Rejected: clearing the birthdate or display name
also revoking, which makes a typo fix withdraw a verification someone checked
an ID for.

**Decision 6 (issue #1115) — the caller sees both, and can clear their own.**
A caller's Profile used to say only "Verified: age"; it now shows what this
node verified, with the value ("Verified by this node: born 1980-01-01, real
name …"), apart from the self-reported fields below it, so a caller who
clears their own birthdate can see why an age still counts. The display
name, location and birthdate open in a line of their own with the value in
it, and take the same keys as the SysOp's edit of them: Enter saves, blank
clears, Esc keeps. Before #1115 a caller could
change these but never remove them; #1115 cleared them with a typed `-`, and
the prompt that explained it left two columns of an 80-column screen to type
a birthdate into, so it was replaced. A birthdate still showing after a SysOp cleared
it was a screen opened before the clear: the stored value was gone.

### Issue #1158 — keys that work everywhere — decided

Every new setting meant hunting for a free letter: Profile had `j` and `z` left
when #1156 needed one. Labels bracketed a letter mid-word where the first was
taken (`Bl[o]cked`), and the same function sat on different keys on different
screens (`[N]ext`/`[P]rev` in a picker, `[O]lder`/`[N]ewer` in a post list,
where `P` meant `[P]ost`). Tracker #1158 settles it in three steps: reserved
keys (this decision), the first-letter rule for menus, and two-digit numbered
fields on settings screens. Normative description: §3.5, "Keys that work
everywhere".

**Decision 1 — reserved keys are punctuation plus `B`.** Paging is `<` `>`
with PgUp/PgDn and ←→, find is `/`, help is `?` with F1 and Ctrl-H, back is
`[B]` or Esc, and Enter chooses. Rejected: `N`/`P` reserved everywhere, which
moves New scan, Profile and Post. Rejected: `N`/`P` reserved only on screens
that page; nearly every busy screen pages, and a hybrid such as the post list
makes "is this a list?" a judgement call for callers and for the enforcing
test.

**Decision 2 — ←→ step a value on forms.** On a draft editor ←→ already step
the highlighted field's value, and they keep doing so; the form's sections
page with `<` `>` and PgUp/PgDn. Everywhere else ←→ page. Rejected: ←→ paging
on forms too, which loses the quickest way to change a value.

**Decision 3 — Esc drops a highlight first.** With a row or field
highlighted, Esc clears the highlight as before; with none it goes back like
`[B]`. Rejected: Esc always back, which would leave the screen for a caller
who only meant to clear the highlight.

**Decision 4 — every hotkey screen has help.** `?`, F1 and Ctrl-H open the
same help, and screens without any get some written in this step, so the keys
never answer with a bell. F1 is decoded on Telnet, SSH and the browser
terminal. Rejected: the keys only where help already exists.

**Decision 5 — no global quit, and doors keep their own keys.** Log off stays
`[L]ogoff` on the main menu; a key that ends the session from anywhere is too
easy to hit in the middle of a form. Bundled doors keep their own action bars
under their presentation contracts.

**Decision 6 — callers are told once.** After the first login on the release
that changes a key callers use, a short screen says that paging moved to `<`
`>` and which menu keys were renamed (Decision 7 ships in the same release),
dismissed with any key. SysOps see it on the console too. Only an account
that knew the old keys sees it: the migration that ships with the change marks
every account existing then (`keys_notice_1158 = pending`), and the screen
(`netbbs.net.keys_notice`) is shown at that account's next login, after the
previous-callers screen, then marked seen. An account made later has no mark,
and a guest, signed in to a shared account, is never shown it. Rejected:
counting logins, which would also show it to an account made after the
upgrade on its second login.

**Decision 7 — a menu key is its label's very first letter.** Step 2 of the
tracker. Every letter key on a menu, an action bar or a one-key question
starts its label. A collision is resolved by rewording one label with a
synonym, and a renamed key is a clean switch with no hidden alias for the
old one. Rejected: allowing the first letter of a later word (`Reply [a]ll`,
`Change [N]ame`), which reads naturally but leaves the eye hunting for the
key inside the label.

- **Toggles show their state.** `[F]ollow: off` / `[F]ollow: on`,
  `[K]ept: no` / `[K]ept: yes`. The key and the label's start never
  change with the state. Where the highlighted row already shows the state
  (the mail list's `*` mark and `new`), the toggle keeps one label,
  `[M]ark` and `[U]nread`, so the bar still fits 40x12. Rejected: the key followed by the action
  (`[F] Unfollow`), which sets the key apart from its label, and separate
  verbs (`[F]ollow` / `[U]nfollow`), which move the key with the state and
  collide with other `U` keys.
- **The main menu.** `C[o]mmunities` becomes `[T]opics` (its own description
  already called them topic spaces; the screens keep the word Communities),
  `S[t]aff list` becomes `[O]perators`, `P[r]evious callers` becomes
  `[R]ecent callers` and `Moder[a]tion` becomes `[A]pprovals`.
- **Settings and field screens are left to step 3**, which numbers them
  (Decision 8), so their fields are not reworded twice.
- **Row numbers are two digits on every list**, as decided for the tracker:
  a board's posts, the mailbox and a file area took one digit and now read
  `01`-`99` like the picker (two digits, or one and Enter).

**Decision 8 — a field is chosen by its number, straight through.** Step 3 of
the tracker. Every draft field editor draws a two-digit number in front of each
field and reads it like a list's row number; ↑↓ and Enter still work, and a
field has no letter. The numbers run through the whole screen instead of
restarting in each section, so a number names one field on every page, a
SysOp helping a caller can say "Profile, 13", and typing it from another page
turns there. A resource's own screen (issue #1081) numbers its fields too:
numbers cannot collide with its action letters, which is what kept letters
off its fields. Rejected: restarting at `01` in each section, which keeps the
numbers small but makes `03` a different field on every page; keeping
resource screens on the cursor alone, which leaves the one field list
without numbers; and numbers only where letters ran out, which keeps two
schemes.

### Issue #1156 — how an MRC sender's styled name is shown — decided

Since #1152 a decorated sender handle (`+Nick+[CASTLE BBS]`, `^Nick<tag>`) is
peeled off an MRC line. The maintainer asked for a per-caller choice instead
of one fixed rendering. The #298 body convention describes the peeling; this
records how a peeled handle is shown.

**Decision — three styles, combined by default.** A Profile setting
(`mrc_names`): *combined* puts the handle's styled name in place of the plain
one in the `<nick@site>` label and shows the rest of the handle as a tag in
round brackets inside it, `<+Nick+@Castle_BBS (CASTLE BBS)>`; *both* shows the
plain label and then the handle as sent (v7.17.0); *label only* shows the label
alone (v7.17.1). It applies to room lines, live and on replay, and to MRC
private and broadcast lines.

- **The site is never taken from the handle.** The label keeps the packet's
  `from_site`; only the name part changes.
- **The label's own characters stay out of it.** The name part is the sender's
  name plus the ASCII marks `+^!#$%&-_.` directly against it. Everything else
  in the handle becomes the tag, with `( ) [ ] { } < > @ | ~ * =` removed,
  punctuation trimmed from its ends, and a cap of 24 columns. These are the
  characters #916 keeps out of local aliases, for the same reason: inside
  `<…@…>` they make a label ambiguous.
- **The handle is stored, the choice is not.** `channel_messages.mrc_handle`
  keeps the raw handle, colour codes included, only for a decorated one, so a
  change of setting re-renders scrollback. Older lines have none and look the
  same in every style. Nothing about it travels over Link.

Rejected: the handle as sent inside the label
(`<+Nick+[CASTLE BBS]@Castle_BBS>`), and the alias shape
`<+Nick+[CASTLE BBS] (Nick)@Castle_BBS>`, because both put user-chosen
brackets inside NetBBS's own label.

### Issue #1104 — Linked resources by colour — decided

The pre-release check of the first field test's fixes asked to tell Linked
boards, areas and channels apart without another column. Normative
description: §3.6.

**Decision 1 — the name's colour, not a word or a column.** Rejected: the
`[LINK]` word the board list's "about" text led with, which a narrow terminal,
a short description level and SysOp list art all dropped, and a column, which
costs every other column width. The colour is the one a linked node already
has after a chat speaker's `@`, so "from the wider network" reads one way
everywhere.

**Decision 2 — a marker where colour can't carry it.** A plain-ASCII caller
and a row inside list art get ` ~` after the name. Rejected: a legend row
above the list, which would cost a list row on every page of a full list;
Ctrl-H carries the explanation instead.

### Issue #1165 — public node pages on www.netbbs.org — decided

A node that registers a netbbs.org name and joins NetBBS Link got nothing it
could point people at. Normative description: §8.13, and §8.2 for
`node_page`.

**Decision 1 — managed-DNS names, met by Reliable Link.** The name is already
unique and screened, so it is the page's address without another namespace.
Rejected: a page for every node any part of the network knows, which would
publish unverified claims under the project's domain.

**Decision 2 — built ahead of time, not served live.** A periodic job writes
static pages next to the rest of www.netbbs.org. Rejected: a route in the
managed-DNS service, which would put a public page renderer in front of the
credential store.

**Decision 3 — Reliable Link's caller view, not its tables.** The page shows no
more than a caller on Reliable Link may see, which keeps out every SysOp-only
fact by construction. Rejected: reading the tables directly, where one
forgotten column would publish addresses or trust states. The address the
managed-DNS service last saw is never published.

**Decision 4 — an opt-out here, unlike the node map.** The node map has none
(issue #767 Decision 4), because a board missing from it must mean callers
cannot reach it through that board. A web page carries no such meaning: it is
the project describing a node to the whole internet, and a SysOp may not want
that. Published by default with `noindex`, so a new node shows up without
being found by every search engine; indexing is the SysOp's choice too. The
choice is a signed descriptor field, so no website account exists to lose or
to phish.

**Decision 5 — history follows the fingerprint.** A released name can pass to
another node, and the dates are about the node, not the name.

**Not done, deliberately.** Free text such as a tagline, which would put remote
prose under the project's domain; a reliability or uptime history, which reads
as shaming for a hobbyist board that sleeps at night. Software version and
carried boards waited for the descriptor to publish them; issue #1171 below
adds both.

### Issue #1171 — release and guest-readable boards on node pages — decided

Follow-up to issue #1165. Normative description: §8.13, and §8.2 for the
fields.

**Decision 1 — major.minor, never the patch level.** "Runs NetBBS 7.17" tells a
visitor what the board can do. "7.17.1" would also tell anyone scanning the
pages which boards have not taken a security fix. Rejected: the full version.

**Decision 2 — only boards the node's guest may read.** A board's name can say
more than its SysOp wants on the open web, and a board behind a level, an age
gate or an account is not open to the public anyway. The guest account is the
node's own statement of what a stranger may read (§4.6), so the check is the
read and age gate a guest meets, not a new setting. Rejected: every carried
board, and a per-board publish flag, which would be one more setting for the
same answer.

**Decision 3 — nothing extra while the page is off.** The two fields ride the
descriptor, which every peer sees, so a SysOp who turned the page off would
otherwise still publish them network-wide for nobody's benefit.

**Decision 4 — the genesis name, not the local one.** A carried board can have a
different local name on each node (§9.3, issue #671); the page lists the
board by the name its origin gave it, so the same board reads the same on
every page.

### Staff edit a member's Profile — decided

The maintainer asked (2026-10-09) that whoever may edit user accounts can edit
every field of one through NetBBS itself. The account detail showed a few of
an account's settings; the rest -- bio, signature, location, blocks, display,
mail and chat settings -- only the member could change. Normative
description: §5.6.

**Decision 1 — their Profile, not more rows.** `[E]dit profile` opens the
member's own Profile screen acting on their account. Rejected: a row per
setting on the account detail, which would run to several pages at 80x24 and
repeat Profile's fields and help in a second place.

**Decision 2 — the reach of the display name and birthdate (issue #1110).** A
SysOp, or a staff member with manage accounts on an account below 255 that
holds no staff permission; never your own account, and never the guest
account. Each write is checked again in the database. The SSH keys stay a
SysOp's, and sharing a verification over Link stays the member's opt-in.

**Decision 3 — the username stays read-only.** It names the account to other
nodes: password-only Link identity, mail addressed by name, post authors, and
retired names. A rename is its own design if it is ever wanted.

**Decision 4 — recorded, without private text.** Every change is in the
account's admin history; a setting with its new value, text the member wrote
or names they block only as changed.

**Decision 5 — the guest defaults are a SysOp's (maintainer, 2026-10-09).** At
first `[E]dit profile` was not offered on the guest account. But what the guest
account stores is what every guest starts with, under each guest's own
per-call changes, and nothing could set it short of turning guest login off and
signing in as the account. A SysOp now sets it with `[E]dit profile` on the
guest account: the display defaults and the shared account's public face (bio,
signature, messaging and MRC settings), without Name & details, read receipts,
sort preferences, keys or password. Staff with manage accounts do not get it:
the defaults are the whole node's.

### SFTP over the SSH transport — declined

Listed as a possible follow-on while issue #475 was open, on the reasoning that
SSH callers already have an authenticated connection and SFTP would ride it.
Declined; this is the decision, not a deferral.

**The motivating problem is gone.** #475 existed because most callers could not
transfer at all — Zmodem needs an emulator that implements it, and PuTTY,
Windows Terminal, an ordinary OpenSSH client and this project's own browser
terminal do not. The session-bound HTTP path (§6.2) answers that for every
transport. SFTP would not reach anyone who is currently stuck.

**What it would add is bulk and scripted transfer**, and the cost of that is a
second enforcement path for every gate the file screen applies: `min_read_level`
/`min_write_level`, `min_age`, `name_requirement`, Community inheritance,
moderation state, `get_max_upload_bytes`, and the rule that a pending upload is
visible only to its uploader and to moderators. Writes carry nearly all of it,
and would additionally have to route through `upload_file_from_temp` so that
content-addressed storage, `FILE_ID.DIZ` reading (issue #463) and Link
descriptor queueing (issue #464) behave exactly as they do elsewhere. A file
area is not a directory tree; presenting it as one means re-deriving each of
those rules in a filesystem vocabulary that cannot express them, which is how
the two surfaces drift apart.

**And bulk seeding does not want a live network surface anyway.** The realistic
case is a SysOp arriving from other BBS software with an existing collection to
bring across — a migration, run once, against a node that is not serving it yet.
That is a local job on the machine holding the files, where the work is reading
someone else's catalogue format and converting it, not moving bytes over a
protocol. It belongs in a standalone CLI tool alongside `python -m netbbs.admin`,
not in the SSH listener. Recorded as issue #505.

Revisit only if a concrete caller-facing need appears that HTTP transfer cannot
serve — not because SFTP would be convenient to have.

### Issue #166 — FidoNet/BinkP gateway — decided

The issue scoped an FTN gateway as a hedge against Link's adoption: FidoNet
and its othernets are populated networks today. The maintainer decided the
design on 2026-10-07. Normative description: §6.8.

**Research.**
- **Prior art.** Every current BBS package runs its FTN mailer and tosser
  inside the BBS:
  - Synchronet: SBBSecho and BinkIT;
  - Mystic: MIS and MUTIL;
  - ENiGMA½: a native BinkP 1.1 mailer;
  - Talisman: Postie and Binki;
  - ANetBBS: Python, MIT.

  The external binkd + hpt combination survives on older Linux setups. Python
  has no maintained FTN or BinkP library: python-ftn is GPL-3 and archived,
  and no Python BinkP implementation exists outside ANetBBS. ANetBBS keeps
  its queue in the database rather than in BSO directories, as this design
  does.
- **The usual on-ramp** for a new BBS is fsxNet (zone 21). A node applies by
  email. A node that only polls, with mail held at the hub, is accepted. Its
  infopack forbids gating fsxNet traffic to another network without the zone
  coordinator's approval.

**Decision 1 — native BinkP, not an external mailer.** The node speaks
binkp/1.0 with CRAM-MD5 and NR in-process. Rejected: binkd with BSO
directories, which means a second program with its own configuration and
passwords for the SysOp to keep in step. It is also awkward on Windows.

**Decision 2 — the first version answers calls as well as polling.**
Rejected: poll-only first. Answering needs an open port, so it stays a
per-network switch, but hubs deliver crash mail sooner to a node that
answers, and a node this one netmails directly (Decision 5) can only answer
the same way if this node takes the call.

**Decision 3 — a board is local, Linked or FTN, never two.** Rejected: both,
opt-in behind a warning. fsxNet's gating rule forbids carrying its traffic
onto Link, and Link would sign FTN posts as this node's own
(`queue_board_post_if_linked`). Keeping them apart keeps the gateway at the
boundary, as the issue asked.

**Decision 4 — full caller netmail, in Mail.** Rejected:
- AreaFix only;
- AreaFix plus netmail for the SysOp alone.

Netmail is personal mail with an FTN address. The To line takes
`Name (zone:net/node)`, the form an FTN author already has on boards, since
`@` names a Link identity. Rejected: a separate Netmail item with its own
folder, which splits one inbox into two.

**Decision 5 — netmail goes direct when it can.** The node processes the
network's nodelist and calls a destination that lists a BinkP host. Anything
else, and a failed direct call, goes to the uplink. Rejected: everything via
the uplink. That needs no nodelist, but it is hours slower, through every hub
on the way.

**Decision 6 — sending netmail needs a minimum level, which starts at SysOp
only.** Netmail leaves under the node's address, so the hub holds the SysOp
answerable for it. Rejected: every account by default.

**Decision 7 — netmail to an unknown name goes to the SysOp.** This is the
usual FTN practice, and nothing is lost to a typo. Rejected: bouncing it to
the sender, which loses a mistyped letter and can loop with a misconfigured
node.

**Defaults set alongside, without a separate question:**
- one echo area per board, off by default, as MRC rooms are;
- FTN authors shown as `Name (address)`, never trust-evaluated, through the
  board's own moderation;
- FTN dates kept;
- duplicates dropped by MSGID;
- SEEN-BY and PATH kept, and no re-export of FTN-sourced posts;
- CP437 when the text fits, UTF-8 otherwise, named by `^ACHRS`;
- file echoes, file requests, hub mode and QWK out of scope.

### Issue #1208 — chat bots — decided

The issue scoped chat bots: something that gives a node personality without
compromising Link's openness or its callers' privacy. The maintainer decided
the design on 2026-10-10 and 2026-10-11; implementing it is separate work.

**Implementation boundary (#1210).** Bot identity recognition, the reserved
suffix, live/replay marking, remote roster marking and local channel policy
are implemented. `/bots` shows the policy; `/bots all`, `/bots local` and
`/bots none` change it with channel moderation permission and an audit entry.
The receiving policy starts at *all bots*; this grants no process, event,
query or publishing permission. It filters live display, history, search
and remote rosters without deleting retained messages or signed events.
MRC mappings, including paused mappings, always refuse bots. The local
runtime now supervises BOT API v1 programs, with addressed-only grants,
per-channel output limits, mutes, local bot presence/counts and persistent
stop controls. Baud provides help and public channel reminders through that
same API. Instances are off by default and configured in Content > Chat bots.
Linked participation requires a per-instance, per-channel approval confirmed
by the SysOp on Save. Approved bots publish ordinary signed/live channel
messages and advertise separate channel-scoped presence. The optional
`chat_bots_v1` endpoint capability enables bounded `bot_request`, `bot_result`
and `bot_presence` frames without changing mandatory realtime version 4.
Requests travel directly from the caller's authenticated home to the selected
bot host; ordinary channel frames and replay never dispatch them. Only a
channel origin may relay another node's bot presence. Remote `/mute` is a
local receiving filter, including history/search, keyed by bot label and home
fingerprint. Remote caller ids are distinct from local account ids and from
identically named callers on other nodes. A remote request's public reply
also goes directly to the caller's node, with ordinary live deduplication,
so an offline channel origin does not swallow that reply. Node backups now
capture bounded API-managed state files with checksums before the database
snapshot, and restore the matching generation, including absence. SysOps may
delete a stopped instance and its state after explicit confirmation; channel
history and existing backups remain. Separate, default-off statistics and
last-seen query grants now serve Baud and external programs through the same
host-bound request API. Statistics expose node name, uptime and online human
count. Last-seen exposes public online status or the latest recorded login or
logout on the bot's host, respecting hidden session history even for SysOp
requests because replies are public. No private activity is queried.
Whole-channel reading and channel-specific live join/leave grants are now
available separately. Baud's optional greetings require join events, not
conversation access. Channel headings, `/who` and live grant-change notices
disclose readers; `chat_bot_readers_v1` carries that disclosure over Link to
supporting peers. A local receiving mute cannot hide a remote reader's access.
Older peers retain ordinary bot traffic but lack the extended disclosure.
Channel history and presence snapshots never become fresh channel events.
Board/file events and optional Baud announcements now use explicit source
lists and one destination per instance. The initial audience proof accepts
only sources readable by everyone, including Community inheritance, so it
holds for local and Linked destinations without assuming peer admission rules.
Restricted mappings are refused. Only public metadata is delivered; source,
content, trust and destination are rechecked inside publication. Events are
bounded, best-effort notices of new approvals/arrivals while the program runs,
not replay on startup. Edits and fetching already-catalogued files do not
reannounce them. Optional model conversation is implemented behind separate
default-off node and instance switches. The host owns provider credentials,
initial disclosure, isolated temporary context/reset and persistent budget
accounting; programs may request it only using a live addressed-message token.
The optional `chat_bot_callers_v1` extension supplies the caller home node's
authenticated/guest classification and level solely for model admission. Older
peers retain ordinary bot commands but cannot assume authenticated model access.
See `NetBBS-bot-api.md` for the implemented protocol and limits.

**Grounding.** Door output already speaks in chat without being a person
(issue #520):
- it uses a label, `<Name>.door`, that is not an account, and the username
  validator reserves the suffix;
- `/mute` works on it;
- it is held to 30 lines an hour;
- it is refused in MRC-bridged channels, and a Linked channel needs the
  SysOp's confirmation first.

A peer's door line is recognized by the same suffix, both live
(`x.door@Node`) and as a stored line whose authenticated author is `x.door`
(`_is_door_line`). Bots reuse this model.

**Decision 1 (locked in): a house bot built on an open bot API.** NetBBS gets
a bot API, and the bot that ships with it uses that API like any other bot.
- **Rejected: a bot built into NetBBS only.** Every new ability would need a
  release, and SysOps could not add their own.
- **Rejected: one bot for the whole network.** It needs a central host on a
  mesh that has none, and that host's operator would see every node's chat.
  MRC operators also frown on bots.

**Decision 2 (locked in): a bot is never taken for a person.**
- **Name.** It speaks as `<Name>.bot`, a label that is not an account, and
  the suffix is reserved as `.door` is.
- **Marking.** It carries a `[BOT]` badge, has its own entry in `/who` and
  `/names`, and the status line counts it apart from people
  (`4 online (1 bot)`). Scrollback records it as a bot, so replay marks it
  too.
- **Control.** Moderators can `/mute` it. The SysOp has a kill switch for
  each bot and one for all of them. NetBBS enforces a per-channel rate limit
  that a bot cannot opt out of.
- **Peers.** A peer's bot line is recognized by its suffix, the way a door
  line is. A person on an older peer whose name ends in `.bot` would be shown
  as a bot; the same edge already exists for `.door`, and it is accepted.

**Decision 3 (locked in): where a bot may speak.**
- **Local channels:** the default.
- **Linked channels:** only after the SysOp confirms it for that channel.
- **MRC-bridged channels:** refused. Hub rules, and the rule never to send
  the hub a request shape it does not document, make bot traffic there
  unsafe.
- **A per-channel bot setting:** *all bots*, *only this node's own*, or
  *none*. The SysOp or the channel's moderators set it. In a Linked channel
  each node decides what it shows, so no SysOp is made to carry another
  node's bot.

**Decision 4 (locked in): the same rules for every bot.** Scopes (Decision 5)
are granted by the SysOp. They never depend on where a bot's code came from.
The bot that ships with NetBBS gets no privilege that others lack. What
protects callers is consent and transparency, and a code privilege would
add nothing: NetBBS is open source, and a SysOp can change its chat code.

The SysOp bot editor selects channels, board sources and file-area sources
from searchable lists with selection markers; it does not require typing
resource names. Channel-specific grants offer the draft's allowed channels,
Linked approvals offer its Linked channels, and sources offer only resources
readable by everyone. The announcement destination is a single-choice picker
with an explicit none option. Toggling a list updates only the draft; Back
returns to the editor and Save applies it with the existing access approvals.
Removing an allowed channel clears its dependent grants and destination in
the draft. Existing unavailable grants stay visible for removal. Names,
program arguments, provider details and numeric budgets remain editable text.

**Decision 5 (locked in): the bot API.**
- **Process.** A bot is an external program that NetBBS starts and
  supervises, like a native door. A crash restarts it with backoff and never
  takes the node down.
- **Protocol.** JSON lines over stdin and stdout, versioned as
  `BOT_API_VERSION`.
  - NetBBS sends events: a line, a join or a leave, a new post or file in an
    area the bot watches, and a timer tick.
  - The bot sends actions: say a line in a channel, reply to a user, read
    or write its own state, and make specifically permitted node queries.
  - The door hook's file drop is rejected for this: a bot has to receive
    events as they happen.
- **Scopes** are explicit and additive. The SysOp grants them per bot and
  per source channel, board or file area as applicable:
  - `addressed`, the default: the bot receives only messages directed to it,
    through explicit naming or the channel's default-bot commands below;
  - `channel`: everything said in the channel;
  - `events`: posts, files, joins and leaves from specifically approved
    sources. Greetings require join events from the approved channel;
    announcements require events from approved boards or file areas.

  A grant of `channel` comes with a warning. While a bot holds it, the
  channel's heading and `/who` say that the bot reads the channel.
  Enabling an ability shows the SysOp the additional access it needs and
  requires the corresponding explicit grants.
- **Node queries** have specific permissions for statistics and `!seen`,
  rather than general database access. `!seen` follows the requesting
  caller's visibility rules and reports when someone was last online,
  without exposing their private-channel activity.
- **Announcement audiences.** Initially, automatic announcements may carry
  information only from boards and file areas readable by every caller
  allowed into the destination channel. NetBBS refuses a source-to-channel
  mapping if it cannot establish this; even a restricted post's title must
  not leak through an announcement.
- **Addressing.** Explicit names work for commands and conversation alike:
  `Baud.bot: help` or `Baud.bot: <message>`. Each channel may have one default
  bot, selected locally by each node. Short commands such as `!help` and
  `!seen` go only to that bot. Without a default, NetBBS explains how to
  address one explicitly. Ordinary chat never triggers the default bot.
  - A remote bot requires its node-qualified identity, such as
    `Baud.bot@Node: help`, resolved through existing Link identity rules.
    A command is never broadcast to every bot with the same name.
  - Only the caller's own node handles their shorthand commands; other
    nodes in a Linked channel do not dispatch that shorthand to their
    defaults.
  - Each eligible live human message selects at most one bot. Bot messages,
    announcements and replayed scrollback never become new requests;
    bots cannot automatically trigger other bots.
- **Memory.** Each bot has a store of its own under `<db>.bots/<name>/`,
  mirroring doors' `<db>.doors/<name>/`, and backups include it. Persistent
  state such as reminders is separate from the temporary language-model
  conversation context described below.

**Decision 6 (locked in): the house bot, `Baud`.**
- **Off by default.** The SysOp switches it on, chooses its channels and may
  rename it. A node can run several instances, each with its own name,
  channels, temperament, limits and memory.
- **Temperament.** A few presets shape its fixed phrases. They need no
  language model.
- **Abilities:**
  - `!help`;
  - `!seen`;
  - node statistics;
  - reminders;
  - greetings and announcements of new posts and files, each switched on
    separately.
- **Scope.** Baud starts with `addressed` alone; help and reminders work
  with that scope. Enabling greetings or announcements requires the
  corresponding source-specific `events` grants. Statistics and `!seen`
  require their specific node-query permissions. These abilities do not
  require access to ordinary channel conversation.

**Decision 7 (locked in): conversation through a language model is narrow
and off by default.**
- **Consent.** Only the SysOp's explicit choice turns it on, after being told
  what is sent where.
- **Provider.** Any endpoint that speaks the OpenAI-compatible API. A model on
  the SysOp's own machine (Ollama, llama.cpp) is offered first: no chat
  leaves the node and nothing costs money per request.
- **What the model sees.** Only the caller's message addressed to the bot,
  plus at most N lines of prior context; the SysOp sets N, and it may be 0.
  Context is separate for each authenticated caller, bot instance and
  channel, and contains only that caller's addressed messages and the bot's
  replies to them. Ordinary chat and other callers' exchanges never enter
  it. Replies remain visible in the channel where the caller addressed the
  bot; separate context does not make channel conversation private.
- **Context lifetime.** Context stays in memory only, expires after 30
  minutes of inactivity, and is cleared on restart. A caller has an explicit
  command to clear their current conversation context. The command explains
  that it does not delete channel scrollback or provider-held records.
- **Callers are told.** The first conversational request receives a notice
  identifying the provider, context policy and whether prompt logging is
  enabled. That request is neither forwarded nor retained as context; the
  caller submits again to proceed. Disclosure is repeated when any of those
  settings change.
- **Limits.** NetBBS enforces these on top of the provider's own:
  - a node budget per day and per month;
  - a per-user daily quota;
  - a per-user cooldown;
  - a per-channel rate;
  - a minimum level to use it, with guests excluded by default;
  - a cap on reply length.

  A limit that is hit is said in a reply, never shown as silence.
- **Accounting.**
  - **Tokens, always.** NetBBS counts the token usage each response reports.
  - **Money, optional.** It is worked out from prices the SysOp enters per
    million input tokens and per million output tokens. There is no standard
    API that reports prices.
  - **Local models** need token or rate limits only.
  - **The console** shows spending, users and limits hit.
- **Secrets and logs.**
  - The API key is stored as a secret, the way FTN passwords are.
  - Prompts are not logged unless the SysOp turns logging on, and callers are
    told when it is on.
- **Persona.** NetBBS ships a default system prompt for Baud, and the SysOp
  can edit it.

### Issue #1214 — Voidrunner on other BBS software — decided

The issue asked what it takes to run Voidrunner as a door under other BBS
packages, such as Synchronet, Mystic and ENiGMA½. The maintainer decided on
2026-10-11. Implementation is tracked in Thiesi/Voidrunner#1–#6 and #1216.

**Starting point.** The door is already one stdlib-only Python file that
imports nothing from `netbbs` and runs standalone. It manages its own
storage: JSON careers, a session lock and Hall of Fame records under
`VOIDRUNNER_SAVE_DIR`. Four assumptions tie it to NetBBS:
- the `NETBBS_DOOR_INFO` JSON drop file;
- UTF-8 output, while classic BBS callers mostly use CP437 terminals;
- stdio as the only transport, while Windows BBSes pass a Winsock handle;
- NetBBS's resize protocol: a drop-file rewrite followed by SIGUSR1.

**Decision 1 — both platforms.** The door supports Unix hosts that launch it
over stdio or a PTY, and Windows hosts that pass a DOOR32 socket handle. On
a socket, the door does the telnet duties itself: it filters IAC sequences,
escapes 0xFF in its output, and treats CR NUL as Enter.

**Decision 2 — its own repository.** Voidrunner lives in
Thiesi/Voidrunner, with its history and tests (Thiesi/Voidrunner#1). Its
game design text moved with it, to that repository's `docs/design.md`
(#1216). This document covers only how NetBBS hosts the door (Phase 7).

**Decision 3 — DOOR32.SYS and DOOR.SYS.** Both drop files are read, mapped
onto the same fields `NETBBS_DOOR_INFO` provides. When `NETBBS_DOOR_INFO`
is set, it wins, so behaviour under NetBBS does not change. A foreign drop
file defaults to 16 colours. Outside NetBBS the save directory and branding
are neutral.

**Decision 4 — CP437 inside the door.** The CP437 encoding and its glyph
mapping live in the door's own file and are not shared with NetBBS's CP437
work (#929). A portable door depends on nothing outside itself, and the
table is small enough that duplicating it is cheaper than coupling the two.

Encoding and colour depth are separate settings, and neither drop file
names a character set. The door picks its encoding as follows:
- **Under NetBBS:** always UTF-8. `door_info.json` carries no charset
  either; NetBBS converts the door's UTF-8 output for a caller on a CP437
  terminal profile.
- **Under a foreign drop file:** CP437 by default, because that is what
  classic BBS callers' terminals expect.
- **Either way:** the player can override the encoding with a new setting,
  saved with the career as the colour preset is. The setting is separate
  from the colour and artwork presets. Those presets still never change
  the encoding, as Voidrunner's presentation contract says; `plain` stays an
  artwork fallback, not a transport change.

**Decision 5 — Synchronet is the test target.** Synchronet runs native
doors on both platforms: stdio mode on Linux and DOOR32 sockets on Windows.
The field test uses SyncTERM in CP437. Mystic or ENiGMA½ is an optional
second Unix check.

**Decision 6 — NetBBS vendors a tagged release.** NetBBS keeps bundling
Voidrunner by copying the single file from a tagged release into
`netbbs.doors.bundled`, recording where it came from
(`scripts/vendor_door.py`, `VENDORED.json`). A new door version
reaches NetBBS through a deliberate, reviewable bump. Rejected: making the
door a pip dependency, which adds a package to every install for one file.
NetBBS keeps the door's registration, `VOIDRUNNER_SAVE_DIR`, and the
backup/restore integration, so existing careers stay where they are and
load unchanged.

Resize stays a NetBBS-only feature: other hosts neither rewrite a drop file
nor signal, so the door keeps its starting size there.

### Issue #1223 — Retro Trivia and War Dialer as standalone doors — decided

The issue asked whether the other two bundled games can follow Voidrunner
(#1214, #1216): each in its own repository, vendored into NetBBS as tagged
releases, and runnable under other BBS software. The maintainer decided on
2026-10-11.

**Starting point.**
- **Retro Trivia** imports nothing outside the standard library, and NetBBS
  imports nothing from it. It keeps no state between launches, so there are
  no saves, backups or migrations to carry.
- **War Dialer** is standard-library only too, but it is coupled to NetBBS in
  three places:
  - It refuses to launch without `war_dialer_owner` and binds its world to
    that ID.
  - Its SysOp administration and backup checks live in NetBBS
    (`netbbs.doors.war_dialer_admin`, `netbbs.backup`), and both import its
    internals.
  - Its idle detection uses `select()`, which Windows pipes do not support.

  Its shared SQLite world (WAL, `WAR_DIALER_DB_PATH`) works unchanged under
  any host whose nodes run on one machine.

**Decision 1 — one repository per game.** The games live in Thiesi/RetroTrivia
and Thiesi/WarDialer, shaped like Thiesi/Voidrunner: own issues, CI, tags and
releases. Each stays one self-contained file. The drop-file parser and the
CP437 table are copied into each one rather than shared (#1214 Decision 4).
Rejected: one shared repository for the doors, where releases and issues would
have to say which game they mean.

**Decision 2 — Retro Trivia gets full portability.** It is extracted and
vendored, then gains DOOR32.SYS/DOOR.SYS, CP437 and Windows socket mode,
following Voidrunner's implementation.

**Decision 3 — outside NetBBS, a War Dialer world owns itself.** The first
launch against a new world, without `war_dialer_owner`, writes a random owner
ID into it and records the world as *self-owned*. Later launches trust that ID
only for a self-owned world. Each BBS points `WAR_DIALER_DB_PATH` at its own
world, so the file's location already keeps one BBS's user numbers out of
another's world. A world NetBBS created stays *host-bound*: every launch must
present the matching `war_dialer_owner`, exactly as the world ownership rule
above requires. A launch without one is refused, never quietly trusted, so a
foreign BBS pointed at a NetBBS world by mistake cannot create or open rows in
that node's user-ID namespace. Under NetBBS, `war_dialer_owner` still wins.

A world that is neither, an *unbound* world, is adopted on its next launch,
because nothing in it belongs to a host. Today's standalone launches create
such worlds; any NetBBS launch would already have bound them.
- **A launch without `war_dialer_owner`** marks it self-owned and writes its
  owner ID.
- **A launch with `war_dialer_owner`** binds it to that host, exactly as a
  first host launch does now.

Either way the adoption is one transaction before any player row is read, and
an unbound world never stays unbound past its first launch. Rejected:
- an ID the SysOp must set, which is one more setup step for no added safety;
- an ID derived from the drop file's BBS name, which a rename would turn into
  a lockout.

**Decision 4 — administration moves into the door, and NetBBS wraps it.**
`war_dialer.py --admin` offers `status`, `maintenance`, `close-season`
and `reset`. Its backup is a SQLite-consistent snapshot taken with SQLite's
backup API under the door's own maintenance lock, never a copy of the `.db`
file alone, whose latest committed pages may still be in the `-wal` file.
This is the same rule as the node backup's world component above.
NetBBS's `war_dialer_admin` calls these commands and keeps its own
requirement of a verified node backup before a destructive operation.
The documented `administer_world` function is the in-process equivalent of
the CLI; NetBBS uses it so the console and CLI share the same transactions.
`status` remains read-only. Every mutation records an audit entry, and reset
retains player identities, account age, ownership and the latest 100 entries.
Destructive commands require maintenance on, the exact world filename and a
reason; maintenance stays on afterward. Snapshots default to a fresh file
beside the world, or `--snapshot` names a new file; existing files are refused.
`--world` overrides `WAR_DIALER_DB_PATH`, and `--json` selects machine-readable
status/errors. Backup inspection uses `inspect_world`; hosts still enforce
their own path and ownership policy and use `world_session` for maintenance.
Rejected: administration that only NetBBS provides, which would leave
another BBS's SysOp no season or reset tools.

**Decision 5 — narrow the boundary, then pin what is left.** NetBBS goes
through the `--admin` commands wherever it can. The few names it still
imports, such as the world inspection backup needs, are pinned by a test,
as Voidrunner's three are (#1216).

**Decision 6 — Retro Trivia first.** Retro Trivia is extracted, vendored
and made portable first. War Dialer follows in three steps: the admin
entry point, then extraction and vendoring, then portability.

### Deliberately deferred without active issue

- social/M-of-N node-root recovery;
- true client-side Link-mail encryption;
- schema fingerprinting beyond SQLite `user_version`;
- Community defaults as mandatory floors/ceilings.

A deferred topic becomes normative only after an explicit design decision. Do
not infer commitment from its appearance in this list.

---
