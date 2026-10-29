# NetBBS design — §9. Linked boards and resource lifecycle

Part of the [NetBBS design](../NetBBS-design-doc.md). Section numbers are stable: cite this area's rules by their section numbers (§9.n).

## 9. Linked boards and resource lifecycle

### 9.1 Promotion and genesis

An existing local board may be promoted into Link scope. Promotion creates one
signed `board_genesis` referencing the existing stable board ID; it does not
replace the board with another local object.

The node identity is the origin authority. The genesis includes descriptive
metadata and recommended defaults for carrying nodes.

### 9.2 Posts and edits

Only approved local posts are originated as `board_post` events. Password-only
users currently use the `node_vouched_user` author tier.

A post drawn in the ANSI art editor carries an optional `"layout": "art"` in its
`board_post` payload. The key is omitted for prose, and an absent or unknown
value is prose. Edits carry no layout: a revision follows its root (§16, issue
#711).

Each Linked copy of a cross-post (issue #1298) is its own `board_post` with an
optional `"crosspost": "<group id>"`, omitted for an ordinary post and never
on a reply. A node that predates it keeps and relays the signed bytes and
shows the copy as an ordinary post, so there is no protocol bump. A node that
carries two copies of one group ties them locally under `<id>@<home node
fingerprint>`, the author's home node scoping the id so no other node's
group can share it; an id longer than 64 characters or outside
`[A-Za-z0-9_-]` is not stored.

Self-authored edits become chained `board_post_edit` events. The original post
remains immutable. Moderator edits and tombstones require separate authorized
event types and advanced governance.

**Polls** (§6.1, issue #1299). A poll's `board_post` carries an optional
`"poll": {"options", "max_choices", "closes_at", "results"}`; a node that
predates it, or cannot read it, shows the post without its poll. Because the
root event is immutable, a Linked poll's answers are fixed when it is posted.
Its closing date travels as `"poll": {"closes_at"}` on the author's
`board_post_edit` or the origin's `board_post_moderator_edit`; `[C]lose poll`
moves the closing date to now and sends such a revision, with the text
unchanged and never held for approval. A carrying node's own moderator closes
its copy only.

A vote is a `board_poll_vote` event naming the board, the poll's root
`board_post` content id, the voter (`node_vouched_user`) and the chosen answer
positions, signed by the voter's home node. It depends only on the board's
genesis: the poll's post may come later, and the vote is kept and counted when
it does. Each node keeps one vote per author identity and poll -- the latest by
`created_at`, the content id breaking a tie -- and drops the signed bytes of a
vote it replaced. A voter renamed on their home node is one voter: votes
under the old and the new name resolve through the aliases #1306 keeps, the
latest wins, and the per-node cap counts them once; a vote signed under a
name already renamed away is refused, as other content is. A vote is refused where the board's posting setting would
refuse the voter's reply, and on a closed board. A node counts:
- no vote dated after the poll closes;
- no vote whose author or home node trust hides (§12), as their posts are hidden;
- at most 50 voters per other home node per poll, in arrival order;
and keeps at most 5,000 voter rows per poll, counting what it turned away. The
results say how many votes were not counted. Each node's tally is its own
view. Votes are served in the board's inventory and pushed with a node's own
events; a node that predates the type relays them opaquely (#1022). The vote
names its voter, so it is not secret from the network: every carrying node's
operator can read who voted what, and the poll says so before a caller's first
vote on it.

### 9.3 Carry and local materialization

A peer accepting a valid board genesis materializes a real local board copy so
users can browse carried content through the normal board UI. Carrying is more
than retaining raw protocol events — the same principle extends to a carried
board's *content*, not just the board shell itself (issue #73): an accepted
`board_post`/`board_post_edit` must become an ordinary local `posts` row, not
remain a protocol-layer record a caller-facing screen can never reach. Before
this, a carried board could verifiably receive posts while still showing
empty to every reader — `link_events` is necessary for protocol verification
and replay safety, but it is not the product database.

**Mechanism.** `netbbs.link.boards` gains `materialize_carried_post`/
`materialize_carried_post_edit`, mirroring `materialize_carried_board`'s own
shape: idempotent (keyed on the event's own `content_id`), bypassing
`netbbs.boards.posts.create_post`/`edit_post` entirely (those require a local
`User` author and mint a fresh local ID, neither of which fits received
content) in favor of a direct insert using the **event's own `content_id`
verbatim as the local `post_id`** — the same "never mint a second ID for the
same thing" precedent `materialize_carried_board` already established for
`board_id`. This has a valuable side effect: since a `board_post_edit`'s own
`root_post_id`/`previous_event_id` payload fields already name other events'
`content_id`s, and those become the corresponding local `post_id`s verbatim,
`posts.root_post_id`/`edit_of_post_id` resolve directly from the Link
payload with no separate ID-translation table.

Like genesis intake since issue #683 (which writes the genesis and its carry
outcome in one transaction), the new functions perform the `link_events` insert and
the `posts` projection in the same call, one transaction, one commit: a crash
between them is no longer possible for posts/edits specifically. `LinkServer.
_handle_events` calls the combined function once per accepted `board_post`/
`board_post_edit`, replacing today's separate `save_event` dispatch for those
two object types.

A reply's `parent_post_id` is set only if that parent is *already* locally
materialized — the same "no backfill, no speculative storage" rule this
project already applies everywhere gossip can arrive out of order (§8, §9.1):
an orphaned reply is materialized as a top-level post rather than blocked or
queued waiting for a parent that may never arrive.

**Author identity.** A materialized post's `author_user_id` is `NULL` — no
local account is implied or required by carrying content (issue #73's own
required test scenario) — with `author_label` synthesized as `local_user_id@
home_node_fingerprint` (the same address shape Link mail already uses) and
`author_fingerprint` left `NULL` (that column is a *local* user's own
personal keypair fingerprint, a different concept from a remote node's
fingerprint — never conflated). This requires a prerequisite fix: `Post.
author_user_id` is currently typed as a required `int` even though the
column has been nullable since the account-deletion migration (round 60's
`ON DELETE SET NULL`) — display code (`netbbs.net.login_flow`'s post-reading
screen, notably) currently calls `get_user_by_id(db, post.author_user_id)`
unconditionally. Widening the type to `int | None` and guarding every such
call site is corrected as part of this work, not deferred — a locally
deleted user's own old posts were already silently exercising this exact gap
before a remote author's posts could.

**Display and resolution.** No new resolution logic is needed:
`_resolve_current_version`'s existing `root_post_id`/`created_at DESC, id
DESC` query already picks the correct latest revision for a materialized
chain, since materialization always processes a verified edit chain in
accepted order — local `id` (strict insertion order) therefore agrees with
logical edit recency even when the remote-claimed `created_at` doesn't (clock
skew, out-of-order network delivery), the same tie-break reasoning issue #68
already established for purely local edits. `created_at` on a materialized
row is the *authored* timestamp from the signed event, never the local
arrival time — see the separate node-local-arrival-order issue (#72) for why
unread/New Scan ordering is a distinct concern from this display field.

**Local moderation stays event-history-safe by construction.** `delete_post`
only ever touches `posts`, never `link_events` — deleting a materialized
post's local row (subject to its own existing FK-blocker rules: no deleting a
post with local replies or edit-chain descendants) already cannot rewrite or
lose the signed record needed for replay safety, with no new mechanism
required. Origin recommendations (§9.1) never override this local policy,
exactly as they never override any other local access/moderation/retention
decision on a carried board.

Concretely, since issue #677:
- A carrying node whose copy of a board is **moderated** holds every received
  `board_post` in its own pending queue, as it holds local posts.
- A received `board_post_edit` is held there too when:
  - this node's copy is moderated;
  - the author's trust requires approval (§12.8); or
  - no revision of that post is approved here yet.
- An origin's `board_post_moderator_edit` is not held for local moderation,
  since it is the origin's own moderation. It is still held while no revision
  of the post is approved here, because a revision must never publish a post
  nobody here approved.
- Approving a held carried post only publishes it locally. It is already on
  the network under its author's signed event, and it is never re-signed as
  this node's own. Approving a held *edit* signs the edit event matching
  whoever made it: `board_post_edit` for the author, `board_post_moderator_edit`
  for a moderator on the origin. The editor is read from the moderation log,
  because `edit_post` carries the author forward onto every revision.

**Idempotency, New Scan, and search.** Duplicate delivery of an
already-materialized event is a no-op (existing `post_id` found, row returned
unchanged) — no duplicate local posts or revisions. `[N]ew scan`/unread
counts (`netbbs.activity`, issue #56) need no new wiring at all: they compare
a stored cursor against `posts` rows directly, with no separate
"mark as new" call site, so any newly materialized row is automatically new
activity. Local search does need an explicit call — `netbbs.search.
reindex_post(db, board_id, root_post_id)`, the same call every other
`posts` write path already makes, right after each materialization.

**Repairing a gap.** Because persistence and projection are now atomic for
new events, a `board_post`/`board_post_edit` in `link_events` lacks a
corresponding `posts` row only where a node carried boards *before* this
feature shipped, where the expiry sweep deleted it, or where a moderator
rejected it -- and a rejection is recorded so that it stays that way (below). A repair pass — scan `link_events` for `board_post`/
`board_post_edit` rows with no matching `posts.post_id`, and materialize them
in chain order — closes that one-time gap and doubles as the "supported
rebuild path" issue #73's own acceptance criteria ask for, the same
"derived state must be rebuildable from authoritative data" principle issue
#74 applies to FTS indexes. Exposed as `[R]epair carried posts` in the
SysOp `[S]ystem` submenu (only shown when Link is enabled), the same
explicit-SysOp-trigger-only shape `netbbs.files.gc`'s reference-aware blob
reclaim already established — purely additive (fills in a missing row from
an already-verified signed event, never deletes or rewrites anything), so
unlike blob reclaim it needs no dry-run/confirm step.

**A rejection is a record, not only a deletion** (issue #692). Rejecting a
held post or edit deletes its `posts` row, but a carried one's signed event
stays in `link_events`. The repair pass would take that as a gap and publish
the refused post again. So every rejection, local or carried, is written to
`post_rejections`: the post id (for a carried post, its event's `content_id`),
the board, who, when, and an optional reason. Every materialization path
skips a recorded id: the repair pass, and a carried post or author edit
arriving again. The signed event is kept, since local moderation never
rewrites it. A post the repair pass does restore gets the status sync would
have given it: this node's moderation, and a hold where the author's trust
requires approval. The same record is where a rejection's reason and the
author's notice come from (issue #678).

Linked resources are carried by default within the supported topology. Every
genesis a node has accepted is in exactly one recorded state (issue #561):
**carried** (a local row), **offered** (it arrived past the automatic-intake
cap `max_carried_*` and waits for the SysOp to accept it) or **excluded** (the
SysOp declined it, or deleted it while carried, and it stays out until the SysOp
restores it). Deleting a carried resource whose current origin is another node
hides it rather than destroying it: its row and content are kept but invisible
to callers and administration, not carried, and closed to new content, until
Restore un-hides it exactly as it was or Purge deletes it for real. The genesis save and its carry outcome are one transaction. The
caps bound automatic intake only: lowering one sheds nothing, a SysOp's accept
or restore is not capped, and a cap of 0 offers everything new. Offered and
excluded resources want nothing further (§8.8), are declared to peers so that
their events are not sent (issue #669), and are never visible to callers; the
SysOp sees and acts on both lists from Link status. That is how a local
exclusion is represented honestly as “not carried on this node,” not
indistinguishable disappearance.

Carrying stays automatic, and the SysOp is told (issue #681, decided with the
maintainer). A resource carried on its own arrives Uncategorized with its
origin's settings, so it is news until the SysOp looks at it:
`link_carried_to_review` records it, keyed by its Link id. The SysOp
dashboard's ATTENTION panel counts newly carried resources and offers waiting at
the cap, drawing neither count while it is zero. The console's board, file-area
and channel lists mark each unreviewed one `to review`. Opening its detail screen
is the look that clears it. A resource the SysOp accepted from an offer is not
news: they chose it.

Names are not identities in Link, and independently run nodes reuse them. A
carried resource whose name is already in use here, compared without regard
to case, is carried under that name suffixed with the first 8, then 16, then
all characters of its own id, whichever is free first (issue #671); the SysOp
can rename it like any carried resource, and peers keep seeing the genesis
name. Only with every candidate taken is it refused, as the cap refuses.

A Link Community (§6.5, issue #1308) is a fourth carry-decision kind with one
difference: its genesis is always offered, never carried on arrival, since
subscribing is a standing rule about future content. Accepting the offer
subscribes, and the subscription then accepts the members its lists name
within these same caps, as described in §6.5. Leaving a Community -- deleting
its copy -- records it excluded like any resource; there is nothing to hide,
since a Community holds no content of its own.

Origin recommendations never override the carrying node’s local access,
moderation, retention, or legal policy. Two origin settings are not
recommendations but authority, binding on every carrying node: closure
(§9.5) and who may post.

**Who may post** (issue #993). The board's current origin signs a
`board_posting` event naming one of three modes:

- `anyone`, the default: any node's callers post, each node's own write
  level holding its own callers;
- `origin_threads`: only the origin node's own callers start threads;
  anyone may reply;
- `origin_only`: only the origin node's own callers post, replies included.

A carried `board_post` whose author's home node (the signed
`author.home_node_fingerprint`, not the node that relayed it) is not the
board's current origin is kept as a signed event but not shown when the mode
forbids it. A reply counts as one only when its parent is on this board: one
naming a parent this node does not have would be shown as a new thread, so
under `origin_threads` it waits as a kept event until a rebuild finds its
parent. A node that is not the origin does not offer its callers `[P]ost` or
`[R]eply` where the mode forbids them, and says why; it asks again just
before a post is written, in case the mode changed meanwhile, and a door's
post there is refused before it is written. The rule follows an origin
transfer: a post the old origin wrote before the transfer and that arrives
after it is refused, since `created_at` is not authoritative (§7.4).

`board_posting` is not part of the board's lifecycle chain (§9.4). An origin
keeps only its latest lifecycle event, so a setting chained in would hide an
earlier transfer from a peer that missed it. Instead each node verifies the
event against the board's origin at the time it arrives, and the setting with
the latest `created_at` is in force; an origin transfer leaves the old
origin's setting in force until the new origin sets one. A setting signed by
a node that is not the board's current origin (a former origin's, still
relayed) is skipped on receipt, not refused, so it cannot fail the batch it
came in; a former origin drops its own setting when the origin moves, and
stops pushing it. A node with Link turned off still knows whether it is a
board's origin from the fingerprint it records at every start. The origin
keeps its own latest setting (`boards.link_posting_json`) and re-pushes and
serves it like its lifecycle event; carrying nodes keep every one in
`link_events`.

### 9.4 Origin succession

Routine node signing-key rotation is handled by the node key-transition chain
and does not transfer resource ownership.

Voluntary board-origin transfer requires mutual consent:

1. the current origin signs an offer naming the proposed new origin;
2. the proposed origin signs acceptance;
3. peers project the new origin only after both valid events.

Because this transfers authority, a SysOp picker must disambiguate peers whose
friendly/DNS presentation labels collide by showing their full fingerprints
before selection and confirmation.

Only one outstanding transfer offer is meaningful at a time.

If the current origin loses all valid signing authority and cannot publish a
transfer, the board is locally recognizable as orphaned. Existing content
remains available, but no new origin-authorized state is accepted.

A fork is a new resource/genesis with a new origin and an optional
non-authoritative `forked_from` reference. Each node independently chooses
whether to carry the original, the fork, both, or neither.

Channel-side Link lifecycle will reuse these principles after linked channels
exist; there is currently no channel genesis protocol.

### 9.5 Board closure and moderator-authorized post changes (issue #88)

Three further origin-authorized event types complete the board-lifecycle and
per-post governance surfaces left open by §9.4:

**`board_closure`.** A terminal board-lifecycle event, extending the same
`board_lifecycle_head` chain §9.4's transfer offer/acceptance already extend
— signed by the board's *current* origin, referencing the chain's current
head as `previous_event_id`. Once accepted, no further lifecycle event
(another closure, or a fresh origin-transfer offer) is accepted for that
board — closure is terminal, not reversible in this slice. Closure stops new
posts (`board_post`) to the board; it does not restrict moderator edits or
tombstones of existing content, since an archived board may still need
cleanup. Materializes locally as a `boards.link_closed_at` timestamp,
enforced by `netbbs.boards.posts.create_post` for this node's own callers and
by `materialize_carried_post` for posts carried in from other nodes (issue
#1021: before, a carried post still landed on a closed board). A post that
arrives after the closure is known is not shown, whenever it was written.

**`board_post_moderator_edit`.** Structurally identical to `board_post_edit`
(§9.2) — extends the same per-post `previous_event_id` chain — but signed by
the board's *current origin* instead of the edited post's own author's home
node, and carries no `author` field to cross-check against (the "self-authored
only" rule `board_post_edit` enforces on receipt simply doesn't apply to this
type). This is deliberately *not* a new cross-network moderator-grant
primitive: `netbbs.boards.posts.edit_post` already allows any local user
holding `BoardPermission.EDIT` to edit someone else's post (existing
behavior, unchanged by this issue) — that local permission check happens
once, on the origin node, before `netbbs.link.boards.queue_board_post_
moderator_edit_if_linked` ever builds and signs the event. A carrying
(non-origin) node's own local moderator action on a post it doesn't own
stays purely local and is never propagated — it has no origin authority to
assert an edit the rest of the network would recognize. Linked-board
moderator *grants and revocations* (a network-visible delegation of that
authority to non-origin nodes) remain out of scope, unchanged from this
section's prior framing.

**`board_post_tombstone`.** Also extends the per-post chain, as a terminal
entry — no further `board_post_edit`/`board_post_moderator_edit`/a second
tombstone is accepted once a post's chain head is already a tombstone. Same
origin-signed authorization model as a moderator edit; carries its own
placeholder `subject`/`body` (redaction content chosen once by
`netbbs.boards.posts.tombstone_post`, not reconstructed by convention on each
receiving node) plus an optional `reason`. Locally, a tombstone is a further
content-addressed revision — never an in-place mutation, and never
`netbbs.boards.posts.delete_post`'s hard delete, which stays reserved for a
still-`'pending'` post's rejection and refuses outright if any row still
references the target — so the edit chain, and any reply's `parent_post_id`,
stay intact. `posts.tombstoned_at` (nullable, plain `ALTER TABLE`, no
`CHECK`-widening table rebuild — `posts` is a live self-referencing FK parent,
and an earlier migration already documents why rebuilding it is
specifically unsafe) marks the terminal revision; `edit_post`/`tombstone_
post` both refuse to extend a chain whose current head is already
tombstoned. Requires `BoardPermission.DELETE`, no author bypass, matching
`delete_post`'s existing rule exactly.

An origin's moderator edit or tombstone of a post written by a *remote*
author is chained from that post's retained events: a carried revision keeps
its signed event only in `link_events`, never in `posts.link_event_json`
(issue #677; until then such posts looked off-chain and the origin's
moderation of them was never sent).

**A local tombstone is terminal on the node that made it** (issue #677). A
carrying node's moderator may remove a post locally even though the removal
is never propagated. A `board_post_edit` or `board_post_moderator_edit`
received afterwards is retained as a signed event, so relay and inventory
are unaffected, but it is never projected over the tombstone. Otherwise a
later revision would sort above the tombstone and bring the removed content
back. Removing or editing someone else's post on a non-origin node therefore
changes this node's copy only, and the moderator is told so: the confirmation
asks to remove the post "on this node only", and the outcome says that other
nodes carrying the board keep the original. On the origin, the confirmation
still says the removal cannot be undone. A closed board offers callers no
`[P]ost` action and says it is closed, rather than letting a caller write a
post that `create_post` would then refuse.

All three share `board_origin_transfer_offer`'s verification shape: resolve
the board's current origin (`current_board_origin`, not the genesis's
original claim), confirm it's an independently-known peer, verify the
signature against its current signing key. None invents a new authorization
primitive — they reuse the origin's existing signing identity and, for the
two post-scoped types, an already-existing local permission check.

Delegated moderators for linked boards and channels, which let a non-origin
node's moderator act across the Link instead of only the origin asserting a
moderator edit or tombstone as above, are built (§16, issue #1357, #1359); the
decision records the roster, signing and reach rules.

Still future or incomplete:

- general public-network anti-entropy beyond the current bounded
  full-known-ID inventory exchange;
- Link-blanket governance surfaces and audit feeds (Phase 6).

### 9.6 Linked channels (issue #87)

Mirrors §9.1-§9.3's promotion/genesis/carry model as closely as possible
rather than inventing a parallel one, with two differences that follow
directly from how local channels already differ from local boards, not
from anything specific to Link:

**No edit chain.** Local channel messages have no edit concept at all
(chat access is participate-or-not, no read/write split, §5.4) — there is
no `channel_message_edit` mirroring `board_post_edit`, because there is
nothing locally to mirror. A `channel_message` is immutable, single-shot
content exactly like a `board_post` with no reply/parent structure (chat
scrollback is flat and chronological, never threaded).

**Origin succession is reused by reference, not reimplemented in this
issue.** §9.4's mutual-consent transfer/orphan/fork model applies
unchanged if a channel ever needs it — the same signed-offer-then-signed-
acceptance shape, the same "at most one outstanding offer" rule, the same
"orphaned means no new origin-authorized state, existing content stays"
behavior for a channel whose origin loses all signing authority. This
issue does not add `channel_origin_transfer_offer`/`_accepted` event
types — genesis/promotion/materialization/messages are the actual scope
(the "Recommended direction"'s own list), matching how governance is
explicitly deferred to Phase 6 rather than half-built here. `channels`
gains no `link_lifecycle_json` column in this issue for the same reason
— add it alongside the transfer event types themselves, when and if a
future issue actually needs it, rather than carrying an unused column now.

**Event family.** Two new object types, `channel_genesis`/
`channel_message`, structurally identical to `board_genesis`/`board_post`
minus the fields above:

- `channel_genesis`: `origin_fingerprint`, `channel_id` (the *existing*
  local content-addressed `channel_id`, never newly minted — same "promote
  an existing local resource" rule §9.1 already states for boards), `name`,
  `created_at`, and optional cascading-recommendation fields mirroring
  `Channel`'s own settable columns (`description`, `min_level`, `min_age`,
  `name_requirement`) — no `default_min_write_level`/`default_moderated`/
  `default_max_post_age_days` equivalents, since `Channel` has none of
  those to recommend a default for. Signed by the origin's current signing
  key, same as `board_genesis`. One per `channel_id`, ever — a different
  genesis for the same `channel_id` is a conflict, identical to
  `has_conflicting_genesis`'s existing rule.
- `channel_message`: `channel_id`, `author` (the same tagged union
  `board_post` uses — only `node_vouched_user` has a real build/verify
  path today, for the same reason), `body`, `created_at`, `nonce`. No
  `subject` (channel messages don't have one) and no
  `parent_post_id`/reply structure (scrollback is flat). Two optional,
  bounded fields extend it, each with its own size check, and an older node
  keeps a field it does not know and shows `body` (the poll precedent, §16,
  issue #1299):
  - `block`: a bot's titled block (a title and up to 12 rows of an optional
    label and a text), beside the plain fallback `body` the bot's host built
    from it. A malformed block is shown as its fallback and an oversized one
    is refused (§16, issue #1373).
  - `announcement`: marks a SysOp's scheduled announcement, authored by the
    SysOp who saved the rule and sent only into a channel that node's SysOp
    approved for announcements. A newer node draws it as an announcement
    naming its node; an older one shows an ordinary line from that SysOp
    (§16, issue #1380).

Canonical encoding, event-identity, and verification follow §7.2-§7.4
exactly as already specified for every other event family — no new rule
needed. `handle_events` gains two new branches following the identical
shape `board_genesis`/`board_post` already use, including issue #85's own
verify-against-claimed-origin-not-wire-sender fix from the start (no
separate "add multi-hop later" step this time — channels get it on day
one, since #85 already generalized `handle_events`'s verification model
before this issue landed).

**Carry and materialization.** A peer accepting a valid `channel_genesis`
materializes a real, locally browsable `Channel` row —
`materialize_carried_channel`, mirroring `materialize_carried_board`'s
exact shape: bypasses `netbbs.chat.channels.create_channel` (which mints a
fresh content-addressed ID from the *local* creator/timestamp, wrong for
carried content), inserts directly using the genesis's own `channel_id`
verbatim, seeds settings from the genesis's cascading recommendations, and
is idempotent (a resend, or a second peer relaying the same genesis,
returns the existing row unchanged).

A `channel_message` materializes into an ordinary `channel_messages` row
— `materialize_carried_channel_message`, using `netbbs.chat.scrollback`'s
existing insert-and-trim shape (`record_message`'s own logic, not a
parallel path), keyed by the event's own `content_id` as the row's local
identity for dedup purposes the same way a `board_post`'s `content_id`
becomes its local `post_id`. `author_label` follows the same
`local_user_id@home_node_fingerprint` synthesis `materialize_carried_post`
already uses; no local account is implied.

**A real, worth-stating consequence of reusing the existing bounded
scrollback rather than inventing unbounded storage for channel content:**
`channel_messages` is a trimmed, bounded scrollback by local design
(`netbbs.chat.scrollback`'s own configured limit, default 100), not a
permanent archive the way `posts` is. A materialized linked message is
subject to the exact same trim as a local one — old linked-channel history
ages out of scrollback precisely as old local history already does. This
is a deliberate consequence of treating a linked channel as genuinely the
same kind of resource as a local one (bounded live chat), not a silent
data-loss surprise unique to Link: the identical bound already applies
today to every channel's own local messages. A self-originated message
queued for push (`channel_messages.link_event_json`, the messages-table
counterpart to `posts.link_event_json`) that gets trimmed before any sync
pass ever pushes it is simply never propagated over Link — bounded,
honestly-scoped, matching this project's own "explicit bound, defined
behavior" principle rather than an indefinite queue.

**Idempotent duplicate delivery, restart reconstruction, and inventory
serving** all follow the identical shape §9.3 and §8.8 already establish
for boards: `channels.link_genesis_json` (new nullable column) is the
restart-safe source for a carried channel's genesis, read unconditionally
the same way `boards.link_genesis_json` already is; `channel_messages.
link_event_json` mirrors `posts.link_event_json` for self-authored
tracking. Issue #85's inventory diff extends to `channel_id`-scoped
`link_events` rows the same way it already covers `board_id`-scoped ones.
