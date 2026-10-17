# NetBBS design — 11. Remote file areas

This is the normative design reference for this area of NetBBS. Section numbers are preserved from the design index so existing `§n.n` references remain stable.

---

## 11. Remote file areas (issue #89)

A linked file area remains owned and stored by its source node — unlike a
linked board, where a carrying node eagerly materializes a full local copy of
every post (§9.3), file content is deliberately **not** eagerly replicated:
bytes can be large, so they are fetched on demand, in bounded resumable
chunks, only when a local user actually wants one. The catalogue (what files
exist, their names/sizes/hashes) is still gossiped and fully browsable
without ever fetching any content — mirroring §9's promotion/genesis model
for the metadata half, then adding one genuinely new mechanism (chunk
transfer) for the content half that boards/channels never needed.

### 11.1 Promotion and genesis

An existing local file area may be promoted into Link scope exactly like a
board (§9.1): one signed `file_area_genesis` referencing the existing stable
`area_id`, never a newly minted one. `file_areas` gains `link_genesis_json`/
`link_origin_fingerprint` columns mirroring `boards`' own pair exactly (the
table's pre-existing `origin_node_fingerprint` column is unrelated dead
Phase-1/2 scaffolding, left untouched, the same precedent `boards.
origin_node_fingerprint` already set — a fresh column, not a repurposed old
one). No `link_lifecycle_json`/origin-succession event types in this issue —
same deliberate deferral §9.6 already applied to channels, for the same
reason: genesis, catalogue, and transfer are the actual scope; add transfer
event types alongside a future issue that actually needs them rather than
carrying an unused column now.

`file_area_genesis` payload: `origin_fingerprint`, `area_id`, `name`,
`created_at`, and optional cascading-recommendation fields mirroring
`board_genesis`'s own shape (`description`, `default_min_read_level`,
`default_min_write_level`, `default_moderated`, `default_max_file_age_days`,
`default_min_age`, `default_name_requirement`) — one per `area_id`, ever,
same conflict rule §9.1 already states.

### 11.2 File descriptors (catalogue, no content)

`file_descriptor`: the catalogue entry for one file, gossiped the same way a
`board_post` is, but describing metadata only — `area_id`, `file_id` (the
existing local content-addressed id, computed the same way
`netbbs.files.entries.upload_file` already computes one), `filename`,
`description`, `size_bytes`, `sha256`, `created_at`. No `parent_post_id`
(files aren't threaded); no `author` tagged union the way `board_post` has
one — attribution is a local admin-log concern (§18), not something a
catalogue entry needs to assert network-wide, and an uploader's identity
carries no bearing on whether a peer should fetch the bytes. Only an
`area.moderated`-approved, locally-uploaded file is ever queued as a
`file_descriptor` — the identical "never leak a moderation queue onto the
network" rule §9.2 already states for `board_post`. Immutable, single-shot,
like `board_post`/`channel_message` — no edit chain; a changed file is a new
upload with its own new `file_id`, not a revision of an old one. Immutable is
not the same as permanent: see *Withdrawal* below.

Queued from exactly two places, mirroring `board_post`'s own pair (issue #464,
which fixed a period where neither existed and a Linked area consequently
announced nothing it held): the upload itself, once the transfer finishes, and
the SysOp's pending-file approval, for a moderated area. That split is what
implements the rule above, rather than restating it — the upload-side call is
simply a no-op while the file is `'pending'`.

Only the area's own origin ever queues one. A carried area is a real, writable
local area, but a peer verifies each `file_descriptor` against the signing key
of the area's genesis origin, so a descriptor signed by anyone else verifies
nowhere; unlike a `board_post`, which carries an author tier precisely so it
can travel from any node into a carried board, describing a file is the
origin's act. An upload into a carried area is stored, listed and downloadable
locally, and never enters the catalogue peers see.

A receiving node's catalogue materialization is genuinely different from a
carried board's: the *area* becomes a real local `FileArea` row (browsable
via the ordinary `list_file_areas`), but an individual `file_descriptor`
does **not** become a `files` row — `netbbs.files.entries`' own invariant
("a file row is only ever created after its bytes are already safely
written to storage") stays true unconditionally, so a catalogued-but-not-
yet-fetched file cannot live there. It lives instead in a new `remote_files`
table (`file_id`, `area_id`, `origin_fingerprint`, `filename`, `description`,
`size_bytes`, `sha256`, `created_at`, `link_event_json`, `fetched_file_id`)
— catalogue metadata only, browsable and listable (the acceptance
criterion's own "discover and list... without fetching any file content"),
with `fetched_file_id` set only once §11.3's transfer completes and
verifies, at which point the content is promoted into a genuine `files` row
indistinguishable from a local upload for browsing/download purposes.

**Withdrawal — a catalogue entry may outlive its file, and stops when it is
asked to (issue #479).** Once an area announces its uploads, a peer's
catalogue can describe a file its origin no longer has: the SysOp deleted it,
or `_sweep_expired_files` purged it past the area's grace period. Either way
the only origin row that could serve the bytes is gone, and nothing in the
catalogue knows.

The origin says so when asked. A chunk request (§11.3) for a `file_id` this
node holds no row for is answered **HTTP 410 with a signed `file_withdrawal`**
— `file_id`, `requester_fingerprint`, `transfer_id`, `request_nonce`,
`created_at`, `nonce`, signed by the origin's **current** signing key. That is
the same origin *identity* that signed the `file_descriptor` being withdrawn,
but not necessarily the same *key*: a descriptor is immutable and keeps the
signature it was created with, while an operational signing key rotates (§12).
An implementation that verified a withdrawal against the descriptor's own key
would reject every legitimate one issued after a rotation; the current key is
resolved through the origin's transition chain, as everywhere else.

Acting on one is irreversible in a way discarding a bad chunk is not: once the
`remote_files` row is gone, the `file_descriptor` still in `link_events` means
ordinary redelivery will not bring it back. So the requester checks four things,
all of them before deleting anything:

- **the envelope is a `file_withdrawal`.** The descriptor being withdrawn is
  gossiped to the whole mesh and carries the very same `file_id` — and until the
  origin rotates its signing key, is signed by the very key a withdrawal is
  verified against, which is exactly when the confusion is exploitable — so "signed by the origin, names this file" describes a
  document any interceptor already holds, and `object_type` is the only thing
  separating the two. It is a precondition of the signature check, not a
  formality;
- **`requester_fingerprint` is this node, `transfer_id` is this fetch, and
  `request_nonce` echoes the chunk request's own authorization nonce.**
  `transfer_id` is content-derived from `(file_id, requester)` and so is
  identical across retries — it names the transfer, never the individual
  request. `request_nonce` is what makes the answer single-use, so a withdrawal
  captured off the wire cannot be replayed even at the same node for the same
  file;
- **`created_at` is fresh**, on the same five-minute window and for the same
  reason `InventoryRequest` has one: a signature is durable, and without
  freshness a recorded 410 stays usable indefinitely — including after the
  origin restores the file from backup, when the entry it deletes would describe
  bytes the origin is serving again;
- **the signature verifies** against the origin's current signing key — and an
  envelope that cannot be canonicalized fails this check rather than raising,
  since it could not have carried a valid signature anyway.

Any failure refuses the withdrawal and changes nothing. On success the requester
deletes its `remote_files` row along with the fetch state that existed only to
serve it (`link_file_transfers`, its chunk records, and any staging file), and
tells the caller the origin no longer has the file rather than reporting a
generic transfer error. A chunk still in flight for a transfer withdrawn this
way fails as an ordinary `FileTransferError` rather than writing into a row that
no longer exists.

A withdrawal is **not** a gossiped tombstone, and deliberately so. A
`file_descriptor_tombstone` mirroring `board_post_tombstone` would have to be
retained and re-offered forever — one per deleted file, and one per file every
expiry sweep purges, growing without bound in exactly the place §8.8's push
direction had just finished bounding. A point-to-point answer to a
point-to-point request covers deletion and expiry alike with no retained state
anywhere, in the same never-gossiped, never-through-`handle_events` shape
`file_chunk_descriptor` already uses.

The cost is stated rather than hidden: **a stale entry survives until somebody
tries to fetch it.** Listing is honest about what the origin last announced,
not about what it still holds; the first attempt is what reconciles them, and
it reconciles them permanently. The event dedup that makes `handle_events`
idempotent is what keeps a re-delivered `file_descriptor` from resurrecting a
withdrawn row.

An entry whose `fetched_file_id` is already set is never withdrawn. Those
bytes are local, verified and promoted into a real `files` row; the origin
dropping its own copy is not a reason to un-list this node's.

### 11.3 On-demand chunk transfer

Unlike every other Link mechanism so far, chunk transfer is a direct,
point-to-point pull against one specific peer — the file's own
`origin_fingerprint` (§11.2), never relayed, never gossiped, and not part of
`handle_events`'s dispatch at all: nothing here is a candidate extension of
a shared chain the way board/channel events are, so there is nothing to
verify against a "current origin" the way `board_post_edit` does.

**Wire shape.** A new pair of HTTP routes (`netbbs.link.transport`),
alongside `/events`/`/inventory`: `POST /link/v1/file-chunk/{fingerprint}`.
The **request** is a small, unsigned JSON bundle (mirroring
`InventoryRequest`'s own "not a candidate chain extension, nothing to sign"
reasoning) — `transfer_id`, `file_id`, `chunk_index`, `max_chunk_size`. The
**response** carries the chunk's raw bytes as the literal HTTP body — never
base64-embedded in JSON, per the issue's own explicit requirement — plus a
*signed* `file_chunk_descriptor` (`file_id`, `chunk_index`, `chunk_sha256`,
`chunk_size`, `total_size`, `is_last`, `created_at`), delivered in a response
header (`X-NetBBS-Chunk-Envelope`, base64 JSON) rather than the body, so the
body stays purely raw bytes. Signed by the origin's current signing key —
the same `_resolve_sender_signing_key` resolution every other Link
verification already uses — so a requester can verify the chunk's
authenticity and integrity (`chunk_sha256` against the actual bytes
received) independent of transport-level trust, the same "objectively
verifiable" standard §12.3 holds every piece of Link content to.

**Requester side is required to already be a completed peer** of the
origin (unlike `/inventory`, which serves already-gossiped small metadata to
anyone) — serving arbitrary bytes to an unauthenticated caller is a new
resource-exposure the metadata-only routes don't have, so this route
requires the same "no relay from a stranger" hello precondition
`/events` already enforces.

**Deduplication and resume.** `transfer_id` is deterministic — a content
hash of `(file_id, requester_fingerprint)` — so a retried or resumed fetch
naturally reuses the same id rather than minting a new one every attempt;
the origin uses it to bound concurrent transfers per requester (§13.5's
bounded-remote-influence principle: an explicit `max_concurrent_file_
transfers`-per-peer cap, visible rejection once exceeded, never silent
unbounded growth). `chunk_id` is the chunk's own `sha256` — an exact-content
dedup key, not a sequence number alone: a resent identical chunk (the same
request repeated, or a resumed transfer re-requesting a chunk it turns out
it already has) is recognized as already-applied and skipped rather than
re-written, the same idempotent-resend discipline every gossiped chain
already applies, just against a `link_file_transfer_chunks` row instead of
`known_event_ids`. `link_file_transfers` tracks one row per transfer
(`transfer_id`, `remote_file_id`, `total_size`, `chunk_size`,
`bytes_received`, `status`, staging path) — resuming an interrupted transfer
means asking for the next chunk index past what's already recorded, nothing
more elaborate. Once every chunk is received, the reassembled content's
sha256 is verified against the file's own catalogued claim (§11.2) before
promotion into real storage — a peer claiming a `file_descriptor` with a
hash that doesn't match what it actually serves is refused at that point,
never silently accepted.

**Completed content is stored once by hash.** The verified reassembly is
handed to `netbbs.files.storage.move_temp_file_into_storage` — the exact
same content-addressed layout local uploads already use, so a file fetched
from two different catalogues (or already locally uploaded, coincidentally
identical bytes) shares one stored blob automatically, no special-casing
needed; `remote_files.fetched_file_id` then references the resulting real
`files` row.

**Not a generic work-item/DLQ instance.** Chunk transfer is deliberately
*not* folded into issue #60's outbound-retry abstraction (§13.7) — like
board/channel gossip (§13.7's own "not every retry-shaped mechanism fits"
lesson), it already has a natural resumable-by-construction terminal state
(`link_file_transfers.status`) and no correct "give up" state distinct from
"the requester stopped asking"; a second, differently-shaped retry
abstraction bolted on top would only compete with that.

**Explicitly out of scope for this issue** (mirroring §9.6's own precedent
for channels): file-area origin succession (reused by reference, §9.4's
model, if ever needed); write-back/uploading to a remote area (§11 itself:
"remains owned and stored by its source node"); public/untrusted file
discovery (Phase 4). Inventory/pull catch-up (§8.8) extended to file areas
was left out of this issue too, but is no longer an open gap — see §11.4
(issue #93).

### 11.4 Inventory/pull-based catch-up for file-area catalogues (issue #93)

§8.8's `InventoryRequest`/diff mechanism extends to file-area catalogues
the identical way §9.6 already extended it to channels: a third key,
`file_areas`, alongside `boards`/`channels`, keyed by every `area_id` this
node currently carries (bounded by its own `max_carried_file_areas` quota,
same "request size already bounded by an existing cap" reasoning §8.8
states for boards) mapped to the full set of content IDs already known for
it. The responder's diff (`netbbs.link.store.file_area_event_diff`) unions
the same three sources §8.8/§9.6 already established for boards/channels:
this node's own self-originated genesis (`file_areas.link_genesis_json`,
never routed through `handle_events`), any `file_descriptor` a *local* user
queued regardless of whether this node originated or merely carries the
area (`files.link_event_json`, populated only by self-authorship, per
`netbbs.link.files.queue_file_descriptor_if_linked`'s own scope), and every
peer-received event this node has accepted (`link_events`, filtered by a
new `file_area_id` column, the file-area-scoped counterpart to `board_id`/
`channel_id`). `_handle_inventory` shares one overall `_MAX_EVENTS_PER_
REQUEST` budget across all three diffs now — board, then channel, then
file area, each with whatever remains — not three independent caps.

Only catalogue metadata (`file_area_genesis`/`file_descriptor`) is ever
recoverable this way — this section changes nothing about §11.3's chunk
transfer, which stays a direct point-to-point pull the requester still
must initiate explicitly against the file's own origin once it learns of
a descriptor. A node that recovers a missed `file_descriptor` through an
intermediary carrier therefore ends up with a real, browsable catalogue
entry (`remote_files`, `fetched_file_id` still `NULL`), not fetched bytes
— turning inventory into automatic content replication was explicitly out
of scope for this issue, matching the acceptance criteria's own "recover
metadata/catalogue divergence only" framing.

`file_area_genesis` already went through `netbbs.link.store.save_event`'s
generic dispatch (the same path `board_genesis`/`channel_genesis` use);
`file_descriptor` does not (`materialize_carried_file_descriptor` inserts
its own `link_events` row directly, same shape `materialize_carried_post`/
`materialize_carried_channel_message` already established) — so populating
the new `file_area_id` column needed two call sites, not one, unlike
`channel_id` (only `channel_genesis` needed it, since `channel_message`
does its own insert too, but happened to need no scoped column of its own
until this issue). Restart reconstruction needed no new code: `node.
file_areas` (via `FileAreaEventState`) was already rebuilt from both
`file_areas.link_genesis_json` and `link_events` by issue #89's own
`load_link_node` changes, and `file_descriptor` has no chain state beyond
`known_event_ids`/`events` to rebuild in the first place — the same "no
branch needed" reasoning issue #89 already documented for descriptors.

---
