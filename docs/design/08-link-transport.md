# NetBBS design — §8. NetBBS Link transport, discovery, and distribution

Part of the [NetBBS design](../NetBBS-design-doc.md). Section numbers are stable: cite this area's rules by their section numbers (§8.n).

## 8. NetBBS Link transport, discovery, and distribution

### 8.1 Traffic-family split

Asynchronous/store-and-forward features use signed HTTP+JSON:

- key and endpoint state;
- boards and Link messages;
- future file catalogues and chunk requests;
- governance events.

Real-time Link chat will use a persistent mutually authenticated Noise channel
with the node transport key. Do not force asynchronous and real-time traffic
through one protocol merely for uniformity.

### 8.2 Hello and endpoint state

A hello is self-authenticating and carries enough root and transition state to
resolve the current signing key, plus a signed endpoint descriptor.

Endpoint descriptors may advertise ordered addresses and relay information.
The newest valid descriptor wins; stale repeats are harmless.

A descriptor also lists `capabilities`: the optional Link behaviours the
signing node's code understands (issue #669). A peer uses such a behaviour only
with a node that advertises it; a descriptor without the list advertises
nothing. It describes the software, not a setting, so every descriptor a given
version signs carries the same list. Among them are `inventory_not_carried`
(issue #669) and `inventory_pages` (issue #685), both for signed
`InventoryRequest` fields (§8.8), and `link_communities` (issue #1308): the node
understands Link Communities (§6.5) -- their three event types and the
request's `communities` map -- and is the only kind of peer they are sent to.

A descriptor may also carry `dial_in`: up to four URLs at which a *caller*
reaches the signing board, as opposed to the Link `addresses` a node dials
(issue #767, §8.12). Each is `telnet://host:port`, `ssh://host:port` or an
`https://` URL, at most 300 bytes. It is the SysOp's own statement, since no
node can see the port-forward or proxy in front of its listeners. A node that has
never stated it publishes `[web] public_url` when that is an `https://` URL
within the same 300 bytes,
the one caller-facing address a SysOp has already declared, or nothing. The field is display-only: a reader
drops a malformed entry and a malformed list reads as empty, as for
`live_relays`, and never refuses the hello over it. An older node keeps and
forwards the field inside the signed envelope without reading it.

A descriptor may also carry `node_page`: the SysOp's choice about the node's
public page on www.netbbs.org (issue #1165, §8.13). It is `"indexed"` (shown,
and search engines may index it) or `"off"` (no page); a descriptor without it
asks for the default, a page shown but marked `noindex`. The field is omitted
for the default, so every older descriptor reads as the default. A reader takes
any other value as `"off"`, since a claim it does not understand is not consent
to publish, and never refuses the hello over it.

While the page is not `"off"`, a descriptor also carries what the page shows
beyond the node map (issue #1171, §8.13): `software_version`, the NetBBS
release as major.minor (`"7.17"`), and `public_boards`, the Linked boards its
guest account may read, as `{"board_id", "name"}` with the name from the
board's genesis, at most 24 and sorted by name. Both are omitted when empty.
A reader drops a version that is not two small numbers and any board entry
without a well-formed board id and a printable name of at most 64
characters, keeps each board once, and never refuses the hello over either.

Under the same condition it may carry `tagline` (issue #1172, §8.13): one line
of the SysOp's own words, at most 80 printable characters with no control,
format or bidi characters. It is omitted when empty, and a reader drops any
other value without refusing the hello.

The protocol logic remains transport-independent. The `aiohttp` adapter is the
boundary translating protocol messages to real HTTP requests and responses.

### 8.3 Bootstrap and peer discovery

Bootstrap sources are combined, not exclusive:

1. operator-configured seeds;
2. the software-shipped reliable-nodes fallback (Reliable Link first; §16,
   issue #219);
3. the live reliable-nodes roster, fetched daily from
   `https://www.netbbs.org/reliable-nodes.json` and preferred over the
   fallback once any fetch has succeeded -- one list serving default seeds,
   asynchronous relay candidates, and the live-relay anchors (§8.10.3), and
   dialed only after the SysOp accepts reliable-node participation;
4. signed/verified peer-list exchange after contact;
5. bounded fallback attempts to discovered candidates when normal seeds fail.

Seed or peer introduction never implies trust. Identity verification is
cryptographic and independent of the network address which introduced a peer.

A compromised bootstrap source can attempt an eclipse or steer connection
attempts, but cannot impersonate an existing node without its key.

### 8.4 Full and outgoing-only nodes

A full peer advertises reachable addresses and accepts inbound Link traffic.
An outgoing-only node initiates connections but cannot be dialed directly.

Multiple addresses are tried in order. Simultaneous HTTP dials require no
connection-role tiebreak because they are independent idempotent request/
response exchanges, not competing persistent sessions.

### 8.5 Relay service for outgoing-only nodes

Outgoing-only nodes select a small redundant set of reachable full peers based
on direct-observation reliability. Relay participation requires signed consent.
A node may opt out of serving relays and may cap the clients/resources it serves.

Accepted relays are published through endpoint state and replaced when observed
reliability degrades.

The relay mailbox currently supports opaque encrypted Link-message envelopes:

- relays see routing metadata and size, not message content;
- storage is bounded in number and in time (below);
- pickup authenticates the intended recipient;
- the recipient re-runs normal event verification rather than trusting the
  relay’s claim;
- relaying does not introduce strangers or weaken the rule that sender and
  recipient identities must already be known sufficiently to verify and
  encrypt.

A relay holds at most `MAX_MAILBOX_ENVELOPES_PER_RECIPIENT` (50) envelopes
per recipient, refusing a further deposit with HTTP 507, and keeps each for
at most `RELAY_MAILBOX_RETENTION_DAYS` (30 days) from its deposit (issue
#891). Every sync pass drops what has waited longer, acknowledgements
(`link_message_accepted`/`_bounced`) as well as letters, and logs a WARNING
naming each recipient and how many went. Without the time limit, a recipient
that never came back -- retired, reinstalled under a new key, gone -- kept
its 50 slots forever and every later letter for it was refused.

Dropping is silent toward both ends. The relay can neither read nor sign
anything for the recipient, so it cannot bounce, and nothing announces the
retention on the wire. The sender learns of the loss from its own timeout on
relay handoffs: a letter handed to a relay expires on the sending side 14
days after the handoff (issue #874), with a notice saying no answer came
back. The relay's retention must therefore stay comfortably longer than that
timeout, so that a letter the sender is still waiting on is never the one
dropped. For that reason it is a constant rather than a SysOp setting: a
relay configured to keep mail a week would turn the sender's "may not have
arrived" into "certainly did not". The SysOp's **Link status** relay section
lists each recipient held for, with its count and the age of its oldest
deposit, oldest first.

A relay also holds sealed attestation bundles (issue #632) for the nodes it
relays for, in a table of their own: one slot per (issuer, recipient), a newer
bundle replacing an older one, bundles from at most 32 issuers per recipient,
each kept at most 90 days. They never take one of a recipient's mail slots.
Pickup hands them over beside the mail, in a `bundles` key an older recipient
ignores; an issuer deposits one only at a relay whose descriptor advertises
`sealed_attestations`. An issuer that is itself one of the recipient's relays
-- the usual shape, an outgoing-only node relayed by the reachable node it
dials -- puts the bundle straight into its own slot (issue #1046). A bundle is deposited with its issuer's hello bundle;
the relay accepts it only if its outer signature verifies under the issuer's
current key as that hello (merged with any chain on file) establishes it, so
only the issuer can fill or replace its slot.

Reliability scoring is direct-observation operational data, not Phase-4 social
reputation.

### 8.6 Current synchronization model

Current background sync:

- contacts configured/cached seeds and candidates;
- performs hello/peer discovery;
- pushes locally originated events the peer has said it lacks, plus
  `key_transition`s unconditionally (§8.8's *Push direction*, issue #478);
- relies on idempotent acceptance;
- sends targeted Link mail directly or through a selected relay;
- requests and applies bounded inventory/pull-based catch-up for linked
  boards, channels, and file-area catalogues from every seed dialed that
  pass. Responders include carried resources absent from the request, so
  an empty inventory can discover a first resource through a carrier
  (§8.8, issues #85/#94).

This is intentionally simple but incomplete.

Not yet present:

- efficient per-peer deltas beyond a full per-board known-ID list (fine at
  this project's declared scale; a compact digest would be needed beyond it) —
  this bounds the size of one inventory request, and therefore of the push it
  provokes, in *both* directions;
- complete retained-event and dedup-purge policy — `key_transition` alone
  is purged (§8.9, issue #86, closed); every board-scoped type stays
  unbounded, stated explicitly as still-needed, not silently deferred;
- public-network backpressure and abuse handling.

### 8.7 Store-and-forward goal

The eventual model supports nodes which are offline for extended periods and
resume synchronization later. Causal relationships come from parent/chain
references; timestamps are secondary ordering data, and content IDs provide a
deterministic final tiebreak for truly concurrent siblings.

Persistent dedup uses exact IDs, not Bloom filters. False-positive data loss is
unacceptable. Retention cleanup must never turn an old state-changing event into
something re-applicable.

### 8.8 Inventory/pull-based catch-up and multi-hop relay (issue #85)

§8.6 named two concrete gaps in the push-only, direct-pairwise sync model:
no pull-based catch-up, and no multi-hop propagation (a node carrying
Alice's board events never relays them to Carol). This section specifies
both, deliberately reusing existing machinery wherever possible rather than
adding new protocol-verification surface.

**Scope.** Signed board-scoped events, linked-channel genesis/messages, and
linked file-area catalogue metadata are included. File bytes are fetched
separately and never appear in inventory. Identity (`key_transition`) events are
already gossiped to every configured seed every pass regardless (§12) and
are small enough that this has never been the gap; Link messages are
point-to-point by design (§10) and are explicitly excluded from any
multi-hop relay, matching their existing "no relay from a stranger" routing
boundary (§10.4) — nothing here changes how `link_message` is delivered.

**`InventoryRequest` — signed, destination-bound, fresh, and not a canonical
event (revised by issues #106/#124; originally shipped unsigned).** This is a bookkeeping request about
what the requester already has, not durable authored content, so it still
needs no content-addressing or gossip-replay semantics of its own — no
chain and no `content_id`. It **is** always signed by the requester's own
current operational signing key, the same "always signed by the
requester's own current key" shape §12's `relay_consent_request` already
established. Before Link v1 interoperability is frozen, issue #124 makes the
additional fields below required rather than preserving the replayable
pre-freeze request shape.

```
InventoryRequest {
  requester_fingerprint: string,
  responder_fingerprint: string,
  created_at: timestamp,
  nonce: 128-bit random hex string,
  signature: bytes,
  boards: { board_id: [known_content_id, ...], ... },
  channels: { channel_id: [known_content_id, ...], ... },
  file_areas: { area_id: [known_content_id, ...], ... },
  not_carried?: { kind: [resource_id, ...], ... },   // issue #669
  page?: { index: int, count: int },                 // issue #685
  communities?: { community_id: [known_content_id, ...], ... }  // issue #1308
}
```

The signature covers every field except `signature` itself. A responder
requires `responder_fingerprint` to equal its own root fingerprint, rejects
timestamps more than five minutes old or ahead of its clock, and rejects a
recently seen `(requester_fingerprint, nonce)` pair. The replay cache is
process-local and capped at 4,096 entries; timestamp freshness preserves the
bounded replay window across restart. This prevents a captured request from
being redirected to enumerate a different peer or replayed indefinitely.

`boards` is keyed by every `board_id` the requester itself currently
carries (bounded by its own `max_carried_boards` quota, §13.9) mapped
to that board's full set of content IDs the requester already has for it.
`channels`/`file_areas` (§9.6, §11) are the identical shape for linked
channels and linked file-area catalogues respectively.

**Link Communities are a fourth kind (issue #1308, §6.5).** `communities` maps
every Link Community the requester holds -- promoted there, or a copy it
subscribes to -- to the community events it has for it: at most a genesis, the
membership list in force and a closure. It is sent only to a responder
advertising `link_communities` (§8.2), and is then present even when empty:
its presence is what tells the responder that the requester understands
community events, and a responder answers with them only to a request that
carries it. The responder answers for every Community it promoted or carries,
and for one whose closure it holds without a copy, so a closure keeps
travelling; it walks Communities before boards, since they are a few events
each and must not wait behind a board backlog. `not_carried` gains the kind
`communities` (an offered, left or deleted Community, or a closed one with no
copy), named only to the same responders, and `wanted` covers declared
community events with the usual rule: a Community seen and not carried wants
nothing, one never seen wants everything. The requester pushes its own
community events only to a peer advertising the capability. The map is paged
with the rest. An older node never relays community events (§7.5 relays an
unknown type only under a board it carries), so a node's Communities reach its
upgraded peers and theirs.

**A declaration too large for one request is sent in pages (issue #685).** The
quotas bound the keys, not the history under them. At about 30,000 held content
IDs the request outgrew the responder's 2 MiB `client_max_size` and was refused
with 413 on every pass, so pull, and the `wanted` push that rides on it,
stopped for good. When the IDs (and `not_carried`, below) would exceed 1 MiB,
half the limit, the requester splits them into `count` pages by
`inventory_page(id, count, nonce)` — the first eight bytes of the SHA-256 of
the request's nonce and the ID, big endian, modulo `count` — and each request
carries one page. The nonce is random, signed and fresh, so the split is new
every request: unsalted, a peer that authors events could vary them until
their IDs shared one page and push that page past the limit for good. Every key is still present; each list holds only its IDs on that
page, and `not_carried` only the resources on it. The signed `page` field tells
the responder which page it has, and the responder narrows its answer to match:
for a declared resource it compares only its own events on that page, and an
undeclared resource it answers for only when the resource ID itself is on that
page, because only there is the requester's `not_carried` complete and "absent"
still means "never seen". Any given item is on the page sent with chance
1/`count`, so a node past the limit catches up in about `count` passes rather
than never.
`count` is at most 4,096. Rejected: a per-resource digest with a full list
where it differs (a second round trip, and one very large resource, which a
busy linked channel becomes since channel events are never pruned, still
outgrows the body); a per-resource cursor or high-water mark (content IDs
carry no order the two sides share, and a gap below the mark would never be
asked for again); paging whole resources (the same single-resource ceiling).
Splitting within a resource lets a reply or edit arrive before what it refers
to, which the materializers already tolerate: gossip arrives out of order anyway.

`page` is signed and omitted for a one-page request, and it is sent only to a
responder whose descriptor advertises `inventory_pages` (§8.2), for the same
reason as `not_carried` below. An older responder is sent the same slice
without being told. It takes the rest as missing and answers with events the
requester already holds, which past its 200-event page can fill every response,
so pull from it may make no progress until it upgrades; the push half still
works, since `wanted` is computed from what the page declares. Nothing an older
responder understands can do better, and the whole declaration moved nothing in
either direction.

**Route: `POST {LINK_PATH_PREFIX}/inventory/{fingerprint}`**, mirroring
`/events/{fingerprint}`'s existing convention (`fingerprint` names the
requester — and, since issue #106, is now actually checked: see below).
The response is **not** a new envelope type either — it is the same raw
JSON event-list shape `push_events`'s request body already uses:

```
{ "events": [ <raw event dict>, ... ], "more_available": bool,
  "wanted": [ content_id, ... ] }
```

`wanted` is the push direction's half of the same exchange (issue #478,
below). It is required: a response omitting it is malformed.

**Responder-side diff.** For each `board_id` this responder itself
currently carries — whether or not it appears as a key in the request at
all (see the discovery paragraph below) — return every board-scoped event
on file for that board whose `content_id` is not in the requester's
declared list for it (an absent key is treated as an empty declared
list). The diff unions three differently-shaped sources, not `link_events`
alone: this node's own self-originated genesis/lifecycle (`boards.
link_genesis_json`/`link_lifecycle_json`, never routed through
`handle_events` at all, so never in `link_events`), any post/edit a
*local* user authored on any Linked board regardless of whether this node
originated or merely carries it (`posts.link_event_json`, populated only
by self-authorship, per `netbbs.link.boards.queue_board_post_if_linked`'s
own scope), and every peer-received event this node has accepted
(`link_events`, filtered by the new `board_id` column — see the schema
change below). A `board_id` the responder does not itself carry is
silently skipped, never an error — "not carrying this board" is already a
legitimate, honestly-represented answer (§9.3). **This is the entire
multi-hop mechanism**: a node that only *carries* board X (never
originated it) can now answer an inventory request for X from a third
node, because the diff draws from everything this node has on file for
that board, not only what it originated.

**Empty-request discovery (issue #94) and its authentication precondition
(issue #106).** The diff above answers for every `board_id`/`channel_id`/
`area_id` the *responder* carries, not only ones present as keys in the
request — so a requester with nothing carried yet can send an entirely
empty `InventoryRequest` and still discover its first Linked board/
channel/file area, rather than being stuck needing to already know an ID
it has no way to learn. Before this discovery behavior existed, the lack
of any authentication on this route cost nothing: an arbitrary caller
still had to already know a specific ID to ask about. Once an empty
request could return *everything* a node carries, the same unauthenticated
route became a resource-enumeration/content-disclosure endpoint for
anyone on the network, not just configured/verified Link peers. `LinkNode.
handle_inventory_request` therefore requires, before any diff logic runs:
`fingerprint` (the URL path segment) must already be a completed peer (the
same "no pull from a stranger" boundary `handle_events`/`handle_peer_list`
already enforce); the signed `requester_fingerprint` inside the request
must equal that same `fingerprint` (a completed peer cannot enumerate on
some *other* peer's behalf); and `signature` must verify against that
peer's current resolved signing key — proving current possession of the
identity, not merely a previously-observed, publicly-discoverable
fingerprint (fingerprints are exactly that: discoverable via the
deliberately-unauthenticated `/peers` route, §8.3). The signed responder,
freshness, and nonce checks above then prevent cross-responder reuse and
replay. The governing
invariant: a completed, cryptographically verified peer may send an empty
inventory and discover everything this node carries; an arbitrary
unauthenticated HTTP client may not enumerate anything. A request failing
any of these checks is refused outright (HTTP 403) — there is no
degraded/partial-answer tier.

**`handle_events` itself needs one correctness fix to make this
verifiable, not zero changes.** Every board-scoped branch (`board_genesis`,
`board_post`, `board_post_edit`, `board_origin_transfer_offer`/
`_accepted`) previously required the wire-level `sender_fingerprint` to
*equal* the content's own claimed origin/author, resolving the signing key
to verify against from `self.peers[sender_fingerprint]` — correct for
direct delivery, but structurally incompatible with relay: a genuinely
relayed event's wire sender (the carrier) is a different node than its
signed author/origin, so requiring equality made multi-hop content
unconditionally unverifiable, not merely unsupported. The fix resolves each
branch's signing key against the **content's own claimed origin/author
fingerprint** (already present in its payload) instead of the wire sender —
but only if that origin/author fingerprint is *itself* already a peer this
node has independently completed a hello with (`self.peers.get(...)`,
raising the same `LinkProtocolError` "no relay from a stranger" shape
otherwise). This preserves the exact same safety property in spirit —
nothing is ever accepted whose signing key this node can't independently
verify via its own previously-established trust — while correctly relocating
*which* fingerprint that trust check applies to: the content's author, not
whoever happened to relay the bytes. The wire-level `sender_fingerprint`
must still itself be a completed peer (unchanged, checked at the top of
`handle_events` as before) — relay only ever happens between two nodes that
have completed a hello with each other. The *author* of what is relayed need
not have: see §8.11. `key_transition` and the `link_message` family are explicitly
untouched — messages remain point-to-point by design (§10) and were never
part of this issue's scope.

**Applying the response needs no *new* acceptance path beyond that fix.**
The requester feeds the returned `events` list through the now-corrected
`LinkNode.handle_events` exactly as it already does for a push response —
chain/dedup logic and materialization are otherwise unchanged. The new
implementation surface is (a) the `handle_events` fix above, (b) the
responder's three-source diff query, and (c) a client-side loop that issues
the request and applies the response.

**Who signed it has to be verifiable, and a hello is not the only way to
learn that (issue #630, §8.11).** Multi-hop propagates *content* through an
intermediary; it does not substitute for the receiving node's own
verification of who signed it. Requiring a completed hello with the author
or origin is not an option: a node dials its seeds and turns to candidates
(§8.3) only when they fail, onboarding gives every new node the same seed,
and two outgoing-only nodes cannot dial each other at all, so two nodes that
share a board through a common seed have, as a rule, never met. The
receiving node learns such an author's identity from the carrier, as §8.11
describes, and verifies the content against that. A node whose identity it
cannot learn cannot have its content accepted, and an event in that position
is set aside without costing the rest of the response.

**Empty inventory is discovery, not "ask about nothing" (issue #94).**
Although each request dictionary lists what the requester currently
carries, the responder walks the union of requested and locally carried
IDs. A missing key therefore means "the requester has never seen this,"
and the responder returns that resource's events subject to the same
authentication, origin-verification, quota, and response-size boundaries.
This lets a node discover its first board/channel/file-area catalogue
through a carrier without weakening the independent-known-origin rule
above.

**Bounded response size.** Capped at the existing `_MAX_EVENTS_PER_REQUEST`
(200, §13.9) — the same constant `handle_events` already enforces on the
receiving end, not a new number. If more than that many events are missing
for the requested boards, `more_available` is `true` and the requester
simply asks again next pass; because its own `known_content_id` list for
each board grows after every partial response, each subsequent pass
naturally asks for a shrinking remainder — no separate pagination cursor is
needed.

**Requester side (`netbbs.link.sync`).** Each pass, every seed whose hello
completed receives one `InventoryRequest` covering every board this node
carries, and the push to that seed (§12) then runs on the answer — issue
#478 reversed the original order, because the response is now what tells
the push what to send. The peer-list request comes last of the three: all
three draw on one per-source request budget at the seed (§13.9), and
candidate discovery is a resilience path for a later pass, while
hello/inventory/push are what the pass exists for. Not sent to one arbitrary "best" peer — every seed
already dialed that pass gets asked, since not every peer necessarily
carries every board this node does, and the push runs against all of them
regardless. A seed that carries none of the requested boards simply
returns an empty event list; this is indistinguishable from (and no more
expensive than) today's existing per-seed push tolerance for an
uncooperative peer.

**Push direction: send what the peer asked for, not everything (issue
#478).** An `InventoryRequest` is already exhaustive — every board,
channel and file area the requester carries, each mapped to the full set
of content IDs it holds. That body therefore already states everything
the *responder* could want from the requester, so the responder answers
with both halves of one comparison: `events` (what the requester lacks,
above) and `wanted` (the declared content IDs the responder itself lacks).
The requester then pushes exactly the `wanted` events it originated. No
second round trip, no new request field, and no per-peer push cursor to
persist.

**Scope: a resource the requester declared, and nothing else.** `wanted`
answers "do you hold this?" for every content ID the request lists, so what
it may consult is a disclosure boundary, not an implementation detail. It is
computed per declared resource — the materialized sources union whatever
`link_events` holds under that same resource id — never against this node's
global dedup set. That set spans `link_message`s, their acknowledgements and
`key_transition`s, all of which this section deliberately excludes from
inventory; consulting it would let any completed peer file a known content ID
under a fabricated resource and read an exact membership answer off the
response. A signed request identifies the asker; it does not make an ID belong
to the resource it was filed under.

A resource this node has **seen and not taken on** — offered or excluded
(§9.3), which is what a carry-quota refusal or a SysOp's exclusion leaves
behind — wants nothing further. Asking on would be asking for content this node
has not taken on, and would not terminate: such a board's posts never reach
`link_events` at all, so they would be wanted every pass forever and occupy the
requester's whole push page. A resource never seen is the opposite case and
still wants everything declared for it, which is how a genesis arrives by push.

The same resource needs the mirror rule on the other side of the exchange
(issue #669). It is absent from the requester's maps, and absent means "never
seen" (issue #94), so every responder carrying it would send its genesis and
every event under it on every pass, under the one `_MAX_EVENTS_PER_REQUEST`
budget all three kinds share: a declined board with a couple of hundred posts,
sorting early, would be the only thing the requester ever received from anyone
carrying it. So the request carries a signed `not_carried` list, by kind, of
the resources the requester holds a genesis for and has no local copy of, and
the responder leaves them out. Deleting a Linked resource keeps its genesis on
file for this, including one this node originated, whose genesis otherwise
lives only in its own row. The list is capped at 5,000 IDs per request: the stored
genesis set is not bounded by anything the node controls, since a peer can keep
sending geneses to a node past its cap, and an unbounded list would grow until
every request was refused. Over the cap each request declares a fresh random
sample, so what goes undeclared costs a resend, never a fixed starvation.
Against a responder that takes pages (issue #685, above), the requester instead
uses enough pages that each page's share stays well under the cap, so each
request declares every declined resource on its page, the responder skips every
undeclared resource off it, and none is ever resent; the sample remains only as
the backstop. The existing maps cannot say this, since their
values are known-ID sets and an offered board's posts were never received. The
field is part of the signed payload only when it names something, so a request
without it signs exactly as before; and it is sent only to a responder whose
descriptor advertises `inventory_not_carried` (§8.2), because an older
responder would rebuild the payload without it and refuse the whole request.
Against an older responder the old behaviour remains. Until issue #683 records
carry states, the list is inferred from `link_events` the same way as "seen and
not taken on" above.

**The cap goes on the push, not on `wanted`.** `wanted` is returned whole:
it can never exceed the content IDs the requester itself just declared, which
the responder's `client_max_size` already bounds, and prefix-capping a list the
*responder* orders is the one thing that must not happen here. A requester
carrying more events originated elsewhere than the cap allows would see those
unsendable IDs fill the page, drop every one of them at the filter below, leave
the responder's state unchanged, and get the identical page back next pass —
its own events never offered at all. The requester caps instead, after
filtering `wanted` down to what it originated: that truncation only ever drops
events it can actually send, so the responder has them next pass and the list
strictly shrinks. No cursor is needed for the remainder, for the same reason the
pull direction needs none.

Two things stay outside this. `key_transition`s are pushed
unconditionally every pass, because identity events are outside inventory
scope (the Scope paragraph above) and a peer has no way to ask for one;
they are few and dedup makes a re-send a no-op. They ride in the same request
while there is room, so an ordinary node's whole push is one request — but
resource events keep at least half a request's capacity whatever the rotation
history looks like, and a long history simply costs a second request rather
than starving them. And the push still only ever carries *self-originated*
content — a `wanted` entry the requester merely carries is skipped, preserving
the "no relay from a stranger" scope note; the responder reaches that content
through its own inventory pull, which is what the multi-hop diff exists for.

`wanted` is required, not optional: a 200 response without it is malformed
and refused. Every node on this mesh runs the same release, so there is no
older peer to accommodate, and accepting a missing key would quietly turn a
broken responder into a degraded-but-working exchange.

That leaves exactly one way to finish a pass without a `wanted` list — the
inventory exchange itself failing, e.g. a peer whose `/inventory` route errors
while `/events` still accepts a push. Such a peer still gets pushed to, from a
rotating starting point held in memory for the lifetime of one sync loop, so
successive passes walk the originated history instead of re-offering its head.
Without that, a node with more than one page of own events would never deliver
the rest to a peer whose inventory route stayed broken — and in an asymmetric
topology, where that peer never dials back, its own pull cannot make up the
difference. The offset is deliberately not persisted: a restart simply begins
the walk again, which dedup makes free.

**What this replaced, and why it was a real defect.** The push previously
sent every locally originated event to every seed every pass, sliced into
requests of `_MAX_EVENTS_PER_REQUEST`. Past roughly 3,800 originated
events — reachable once §11.2 began announcing every upload — that
exceeded the peer's entire per-source request budget (§13.9,
`request_rate_capacity`): a pass spent the whole budget on the same early
slices, took an HTTP 429, and began again at the first slice next pass.
The tail was never reached at any point. Pull-based catch-up still
converged the peer, so this was a starved optimization rather than lost
content, but a push that provably cannot deliver its own tail is not a
push.

**No loop or amplification guard is needed beyond what already exists.**
This is pull-based and diff-first by construction: nothing is transmitted
unless a requester explicitly asks for a board it has already decided to
carry, and the diff is always relative to what the requester already
reports having. A fully-connected mesh does not flood — it converges,
because every node's own request naturally shrinks once it has caught up,
and dedup (`known_event_ids`) makes any redundant delivery a no-op
regardless.

**Schema change.** `link_events` gains a nullable `board_id` column,
populated for the five object types above (read directly from each one's
own `payload["board_id"]`) and left `NULL` for every other object type.
Backfilled for existing rows via `json_extract` against the stored
envelope, never requiring re-verification of already-accepted events. A
covering index on `(board_id, object_type)` keeps the diff query cheap as
`link_events` grows — this is exactly the kind of query the table did not
need to serve before this issue, since nothing previously asked "everything
for board X" rather than "everything from sender Y."

**Deliberately not addressed here** — sending a requester's complete
per-board content-ID list every pass does not scale indefinitely for a
board with a very large post history; a compact digest (Merkle-tree-style
or otherwise) would reduce request size for that case. Not worth building
at this project's declared scale (§2.3: dozens-to-low-hundreds of
concurrent sessions, small-to-medium Link deployments) — the same
"exact IDs, not Bloom filters" simplicity §8.7 already chooses for local
dedup storage applies here to the wire exchange too. Revisit only if a real
deployment shows this cost is actually a problem, not preemptively.

**Explicitly deferred to issue #86, not part of this issue.** No retention
or purging of `link_events` changes as part of this work — every event
handled here is durable, unbounded-lifetime state exactly as it already is
today. Issue #86's retention/purge policy must be designed with this
issue's shape in mind (a purged event a slow-to-reconnect peer still needs
for catch-up must never be silently unavailable, or "eventually converges"
above would stop being true) — that is precisely why #86 is sequenced
after this issue, not the other way around.

### 8.9 Event/dedup retention (issue #86)

**Part 1: the chain-idempotency gap `netbbs.link.store` named.** Every
board-scoped `handle_events` branch self-heals an exact resend against its
own *authoritative* state (never against `known_event_ids` alone) — except
`board_origin_transfer_offer`/`_accepted`, which previously depended
entirely on the fast dedup cache still holding the content_id. A resend of
a still-pending offer, or an already-accepted transfer, after a
hypothetical cache purge would have been misread as a genuine conflict
("already has an outstanding offer" / "no outstanding offer on file") and
rejected — not a security hole (nothing is ever mis-applied), but exactly
the gap blocking any purge policy from being provably safe. Fixed the same
way `key_transition`/`board_post_edit` already do it: check whether the
incoming event's own `content_id` already matches the current pending
offer (`board_lifecycle.pending_offer`) or the current lifecycle head
(`board_lifecycle_head`) *before* treating a second sighting as a
conflict, self-healing `known_event_ids` from that authoritative state
rather than erroring.

**Part 2: what can actually be purged.** Before choosing a retention
window, each object type was traced for what *else* depends on its
`link_events` row surviving — restart reconstruction (`load_link_node`)
and, since issue #85, this node's own ability to answer an inventory
request for it:

| Object type | Durable elsewhere? | Purgeable in this issue? |
|---|---|---|
| `key_transition` | Yes — `link_peers.transitions_json` is the authoritative source `load_link_node` reconstructs `sender.transitions` from; the `link_events` row exists only to fast-path a resend and was never itself load-bearing. | **Yes.** |
| `board_genesis` | Yes — `boards.link_genesis_json` durably holds it for both self-originated and carried boards, read unconditionally by both `load_link_node` and §8.8's own `_all_board_events`. | Not in this cut (see below). |
| `board_post` | **No**, for a peer-received post — `posts.link_event_json` is only ever populated for a *locally-authored* post (`queue_board_post_if_linked`), never for a materialized/carried one. Its `link_events` row is the *only* record `board_post_edit`'s own `self.events.get(root_post_id)` acceptance check and §8.8's inventory diff can draw on. | No. |
| `board_post_edit` | Same gap as `board_post` for a peer-received edit — `post_edits[root_post_id]` reconstruction and inventory serving both depend on the row. | No. |
| `board_origin_transfer_offer`/`_accepted` | **No** — `board_lifecycle_head`/`pending_origin_transfers`/`board_origin` are reconstructed *entirely* from `link_events` rows for a peer-received transfer; nothing else durably records "what the current lifecycle head is." | No. |
| `link_message` family | Not traced in this issue — deferred with the rest of this row. | No. |

**Policy actually implemented: a bounded, age-based purge for
`key_transition` only.** `netbbs.link.store.purge_expired_key_transitions`
deletes `link_events` rows where `object_type = 'key_transition'` and
`received_at` is older than a fixed retention window (90 days — a plain
module constant, not a new SysOp-configurable `LinkConfig` field, matching
this project's own restraint principle: a low-volume event type doesn't
need a dedicated tunable yet). Called inline on every accepted
`key_transition`, the same "purge on write, scoped to the same table this
write just touched" shape `LinkDiagnosticLogHandler.emit` already
established for `link_diagnostic_log` — not a separate scheduled task.

**Everything else stays unbounded in this issue, explicitly, not
silently.** `board_genesis` turned out to already be redundant with
`boards.link_genesis_json` and could plausibly be purged too, but is left
alone here to keep the rule simple (nothing board-scoped is purged this
round) rather than special-casing one board-family type while the other
four remain load-bearing. Purging `board_post`/`board_post_edit` safely
would need a real answer to "has every peer that might still need this via
inventory already caught up" — a harder question than this issue's own
scope, and a legitimate follow-up if `link_events` growth from board
content specifically ever becomes an operational problem in practice
(§13.6's `[L]ink status` already gives a SysOp visibility into growth via
the database file size, per the existing diagnostic-log-growth precedent).

### 8.10 Real-time Noise sessions (issue #148)

Real-time Link traffic uses a persistent TCP connection protected by
`Noise_XX_25519_ChaChaPoly_BLAKE2s`. XX is required because either side may
first encounter the other's current operational key during the connection;
neither side assumes the remote static key is preconfigured. This is a
separate traffic family from signed HTTP+JSON. A live chat frame is not a
canonical Link event and never enters inventory, relay mailboxes, event
retention, or asynchronous retry queues.

The Noise static X25519 key is derived from the existing Ed25519 operational
transport key using PyNaCl/libsodium's supported Ed25519-to-Curve25519
conversion. NetBBS does not create a fourth long-lived node key or introduce a
second transport-key rotation mechanism. During the encrypted XX handshake
each side sends a versioned identity payload containing its stable root
fingerprint, root public key, and root-signed transport transition chain. The
receiver:

1. verifies the root fingerprint and transition chain;
2. resolves the current authorized Ed25519 transport key;
3. converts that public key to X25519;
4. requires it to equal the Noise static key authenticated by the handshake;
5. applies the local Phase-4 node transport decision before accepting any
   application frame.

A stale, revoked, forked, malformed, or differently bound key fails the
connection. The remote node label, endpoint, DNS name, and TCP address are
never identity authority. Transport-key rotation ends sessions using the old
key; reconnect performs a fresh handshake against the new verified chain.

The endpoint descriptor advertises real-time TCP addresses separately from
HTTP addresses. An outgoing-only node may dial a reachable full node. Two
nodes which both cannot accept inbound connections meet through a live relay
(§8.10.3, issue #168) -- a raw-socket proxy below the Noise layer, a separate
mechanism from the asynchronous relay mailbox, never tunneled through it.
Asynchronous linked-channel events continue to work regardless.

A node whose only way out is an HTTP proxy opens every real-time socket as a
`CONNECT` tunnel through the proxy asynchronous Link uses (`HTTP_PROXY`, else
`HTTPS_PROXY`, subject to `NO_PROXY`) and runs the same bytes over it: the
attach preamble, the Noise handshake and the session are unchanged, and of the
Link traffic the proxy sees no more than an on-path observer of a direct
connection would (Basic proxy credentials, where used, are the proxy's own and
cross to it in the clear). The target authority is validated strictly before it
is sent. The tunnel is the only attempt when a proxy applies, an unsupported
proxy URL fails the dial rather than dialling direct, authentication is Basic
from the proxy URL or `~/.netrc`, and a refused tunnel or a failed handshake
over an open one is recorded for the Link status screen. A proxy that inspects
TLS inside the tunnel cannot carry Noise. Decisions and rationale: §16, issue
#628.

#### 8.10.1 Session framing and ownership

Handshake and transport records use an unsigned two-byte big-endian length
prefix followed by exactly one Noise message. Zero-length records are invalid.
The Noise limit of 65,535 bytes is an absolute ciphertext ceiling; NetBBS sets
a lower application plaintext limit of 16 KiB. Decrypted application payloads
are strict UTF-8 JSON objects: duplicate keys, floats, unsafe integers,
unknown protocol versions, missing required fields, and trailing data are
rejected. Unknown message types produce a bounded protocol error and do not
gain side effects.

Every application object contains `version`, `type`, and a session-local
`message_id`. IDs are bounded strings and deduplicated within a bounded
per-session replay window. The first implementation supports:

- `subscribe` and `unsubscribe`;
- `presence_snapshot` and `presence_delta` (channel-scoped; both ways and
  naming the caller's node since issue #1281 -- see §8.10.2);
- `node_presence_snapshot` and `node_presence_delta` (issue #164, node-wide
  -- see §8.10.2);
- `channel_message`;
- `scrollback_snapshot` (issue #194);
- `relay_request`, `relay_waiting`, `relay_ready`, `relay_reject` (issue
  #168, §8.10.3 -- the live-relay rendezvous);
- `direct_message` (issue #168, §8.10.3);
- `direct_chat_invite`, `direct_chat_answer`, `direct_chat_line`,
  `direct_chat_end` (issue #1368 -- only to a peer advertising
  `direct_chat_v1`);
- `ping`, `pong`, `error`, and `close`.

One node owns at most one live session per remote fingerprint. If simultaneous
inbound and outbound connections exist, the lower fingerprint keeps its
outbound connection and the higher fingerprint keeps its inbound connection;
the rule is applied only while both candidates exist, so a sole usable
connection is never discarded. Reader, writer, heartbeat, and reconnect tasks
are owned by one session/supervisor object. Its close path cancels and gathers
every task without masking the initiating failure.

Outbound frames use a bounded queue and never let one slow peer block another.
Subscriptions, remote presence entries, message rate, protocol strikes, and
concurrent handshakes are bounded per peer and node. A full queue drops the
session with an explicit slow-consumer reason rather than silently losing a
state transition. Heartbeat leases expire silent peers; reconnect uses bounded
exponential backoff with jitter and resets only after a stable authenticated
session.

#### 8.10.2 First live linked-channel vertical

A subscription names an already carried `channel_id`. The receiving node
checks that the channel exists, is linked, is locally allowed by trust policy,
and is available to the subscribing peer. Authorization is checked again for
every received message; a successful subscription is not a permanent grant.

Live channel messages are ephemeral node-attested assertions. They carry the
canonical local user ID and display label at the authenticated sending node,
the channel ID, body, creation time, and session message ID. They are not
individually signed canonical events: the authenticated Noise session
attributes them to the sending node, and the UI renders the human identity as
`user@node`. Password-only users can therefore participate without acquiring
a personal signing key. The sending node remains responsible for enforcing its
local membership, mute, and moderation rules before transmission; the
receiving node independently enforces its own node/user/content policy before
display. The receiving node retains the authenticated sending-node fingerprint
on the in-memory message even though ordinary rendering shows the friendly
label. If that fingerprint has an undismissed cryptographic-identity collision,
the live channel line carries the same non-blocking caution used at mail and
direct-message interaction boundaries.

Live lines flow both ways over the one session a subscriber holds to the
channel's origin (issue #860). A subscriber sends its callers' lines up that
session, and the origin shows them to its own callers and relays them to its
other live subscribers, so a Linked channel is one room live. The relayed frame
names the author's node in an optional `author_node_fingerprint`. A receiver accepts
`author_node_fingerprint` only from the channel's origin; anyone else naming
another node is a protocol strike. A relayed line meets the same policy as its
signed event: the author's node must be one allowed to publish here (`EVENTS`),
and the caller must not be quarantined or blocked. A node that has not
established the author's node sees the line neither live nor by sync. Older
nodes refuse unknown frame keys, so that key goes only to peers advertising the
`channel_relay` capability. A frame's optional `content_id` is accepted and
ignored: it is the sender's unverified claim. An older subscriber gets relayed
lines through async catch-up instead; an older origin still shows a
subscriber's plain frame to its own callers.

The signed event's optional `block` and `announcement` fields (§9.6) ride the
live `channel_message` frame and `scrollback_snapshot` entries the same way
(a block also rides a `bot_result`'s private answer):
as optional keys sent only to peers advertising the capability for each
(working names `chat_bot_blocks_v1` and `chat_announcements_v1`, issues #1373
and #1380). Every other peer gets the plain line, with a block's host-built
fallback as its body. A remote request's reply that goes directly to the
caller's node carries the same block, so live deduplication sees one line.
Bot request and result frames are a separate, optional family under
`chat_bots_v1` (§16, issue #1208). A private answer travels only inside
`bot_result`, and a privately asked request is flagged in `bot_request`,
both under `chat_bot_private_v1` (issue #1372): never as a direct message and
never as a channel event, and never sent to a peer without that capability.

A line that arrives by async sync is shown at once to callers already in its
channel, not only on their next join. The bridge remembers the lines it has
shown (bounded), so the signed event that follows a live line is not shown
twice. A line is keyed on what it is -- author's home node, author's local user
id, `created_at` and body -- derived locally the same way from the live frame
(author node: the session peer, or the node the origin names) and from the
signed event's verified payload; the sender builds both from one stored row. A
peer therefore can only mark its own callers' lines as shown, never suppress
another node's line by claiming its id.

Presence is leased, scoped to subscribed linked channels, and advisory. A
snapshot establishes current state after subscription; deltas update it.
Disconnect or lease expiry removes that node's remote presence without
persisting synthetic leave events.

Presence travels both ways over the same session, as lines do (issue #1281).
A subscriber reports its own callers to the origin: a `presence_snapshot` of
who is in the channel right after each `subscribe` -- a caller who joined
before the live session existed sent a join that went nowhere -- then a
`presence_delta` per join and leave. A leave while another session of the same
account is still in the channel is no leave. The origin keeps one roster per
node, announces a subscriber's callers joining and leaving to its own callers,
and relays them to its other subscribers in a `presence_delta` or snapshot entry
naming the caller's node in an optional `node_fingerprint`; a subscriber's own
callers are never echoed back to it. As with `author_node_fingerprint`, only the
origin may name another node: a subscriber reporting someone else's caller, or
any other node reporting presence for a channel it does not originate, is a
protocol strike. A subscriber's report arriving after its `unsubscribe` is
dropped quietly; the unsubscribe itself takes that node's callers out, with a
leave notice, since the last caller's own leave follows it. A subscriber's
session closing takes them out without one. Every node applies its own policy
to every remote caller it lists, the same as to their relayed line: a caller
who is quarantined or blocked, or whose node may not publish here, is not
listed. Each channel's roster stays bounded across all nodes. Older nodes refuse
unknown frame keys, so a subscriber reports up only to an origin advertising
the `presence_relay` capability, and the origin relays other nodes' callers only
to subscribers advertising it. With an older node the old one-way behaviour
remains: an older origin knows only its own callers, and an older subscriber
sees only the origin's.

**Node-wide presence (issue #164)** is a second, independent presence
concept, not a generalization of the channel-scoped one above: it answers
"who's online on this node, right now" -- the same question the local Who's
Online screen already answers -- across every node a live session currently
exists with, not gated on shared channel membership. A node broadcasts a
`node_presence_snapshot` (the local online roster) the moment it starts
tracking a peer's session, then `node_presence_delta` on each subsequent
local login/logout (the account's *first* concurrent session and *last*
remaining one only -- multi-session accounts don't flap). No new trust check
was needed: establishing a live session at all already requires `ESTABLISHED`
transport trust (§12), so node-wide presence inherits that gate for free.
Caller-facing: `[W]ho's online` mixes in every currently-known remote entry
alongside local sessions: since Link-wide live private chat doesn't exist yet
(§8.10 above), selecting a remote entry states that plainly rather than
silently failing or offering an action that doesn't work.

A freshly-subscribing peer also receives a bounded, ephemeral
`scrollback_snapshot` of the origin's own recent local scrollback,
rendered once and never durably stored on the subscribing side (§16,
issue #194) — a shrunk window before the existing async catch-up path
below fills in what a live-only subscribe would otherwise miss, not a
new durability promise. It never carries a `daybreak` line -- the midnight
message or a scheduled announcement in a channel not approved for
announcements, both this node's own local notices (§6.3, issue #1380); a subscriber still accepts one
from an older origin. The first vertical still does not offer multiple
background channel subscriptions per caller (issue #159, closed — decided
against, not a gap); live private messages and relayed sessions arrived
with §8.10.3 (issue #168). A disconnect does not queue or replay live
frames. Callers see `connecting`, `live`, and `offline/degraded` state
plus an honest notice that live traffic may have been missed;
asynchronous signed linked-channel events remain the durable catch-up
mechanism until a later decision changes that product model.

That asynchronous catch-up path (issue #164) now enforces the identical
author-trust-state visibility linked board posts already do: a linked
channel's scrollback silently omits a message whose signed author's home
node is currently `BLOCKED`/`QUARANTINED`, keyed on the event the message
carries (`link_content_visible`), never on which node happened to relay it.
`PROBATIONARY` and local messages are unaffected -- boards and channels now
share one visibility policy instead of two independently-decided ones.

#### 8.10.3 Relayed sessions and live direct messages (issue #168)

**Live relay.** A relay is any full peer with relay serving enabled
(`[link] relay_serving_enabled`, the same switch as the asynchronous
mailbox) that both parties currently hold an ordinary authenticated
real-time session with; the reliable-nodes roster (§8.3, §16 issue #219
Decision 4) is how an outgoing-only node knows which relays to stand by
at -- it keeps a reconnecting session to every reliable node it knows
while participation is accepted, since by definition nobody can dial it
first. The relay is a raw-socket proxy below Noise (§16 issue #168
Decision 1): it never holds key material, sees only ciphertext, and the
two parties run the unchanged Noise XX mutual handshake with *each other*
through it.

Rendezvous rides over the parties' existing sessions to the relay:
`relay_request {target_fingerprint, requester_fingerprint}` from the
requester; `relay_waiting` back while the target is asked; the same
`relay_request` shape forwarded to the target as an invitation (its own
fingerprint as target); the target's agreement (a `relay_request` naming
the requester) or `relay_reject {reason: declined}`; then `relay_ready
{bridge_id, peer_fingerprint, role, attach_token, attach_address,
attach_port}` to both. Each party opens a fresh TCP connection to the
attach address, sends one plaintext `NETBBS-BRIDGE/1 <token>` record, and
runs Noise XX in its assigned role (requester initiates). Both roles
verify the authenticated fingerprint against the one `relay_ready` named
before admitting the session -- the relay is an intermediary and could
pair anyone with anyone; this check is what stops that. The relay makes
no trust decision about the pair; each party applies its own `REALTIME`
policy to the other before agreeing and again after the handshake, as
for any direct session. Relaying carries no Phase-4 implication.

Bounds (Decision 2), all operator-adjustable under `[link]`, each breach
an explicit reject/close: `live_relay_max_concurrent_pairs` (8),
`live_relay_max_pending_rendezvous` (32) with
`live_relay_rendezvous_timeout_seconds` (30, reported back as
`relay_reject {timeout}`), `live_relay_max_bytes_per_second` per
direction per bridge (64 KiB -- a byte-rate bound, since the relay never
parses frames), and `live_relay_idle_timeout_seconds` (120, a dumb
"no bytes either way" timer the endpoints' own ping/pong keeps from
firing). One leg closing tears down the other. Reject reasons are a
closed set: `not_serving`, `invalid_target`, `target_unreachable`,
`at_capacity`, `pending_full`, `declined`, `timeout`, `attach_failed`,
`policy_refused` (the requester's own standing session no longer passes
the relay's `REALTIME` policy; a *target* that no longer passes is
reported to the requester only as `target_unreachable`). Every relay
answer echoes the requester's `relay_request` message id as `request_id`;
an invitation's message id is echoed by the target's agreement or
decline; a forwarding relay's upstream request id is echoed on the
upstream's answers -- so no answer to an earlier, expired attempt can
ever settle a fresh one for the same pair.

These frames made the real-time application protocol **version 3**; requiring
the invitation id on party agreements and declines makes it **version 4**.
A version-3 relay rejects that new field, just as a version-2 peer cannot
accept the version-3 frame types, so mixed versions fail once at the
authenticated handshake with the caller-visible upgrade notice rather than
timing out a rendezvous or dropping a shared channel/relay-anchor session.
A `relay_reject` carries `origin` (`relay` or `party`) so a node
that is both a relay and a party can never misroute one. Every relay
frame a party honours is correlated: a `relay_ready` or `relay_reject`
counts only from the relay this node asked, for the target it asked
about, or from the relay whose invitation it accepted, within the
rendezvous timeout -- an authenticated peer can never make a node open an
outbound connection to an address of its choosing.

**Live direct messages.** `direct_message {to_user_id, from_user_id,
from_display_label, body, created_at}` is one private line between a
user on the sending node and a user on the receiving node, over whichever
session exists or can be established: the registry's existing session,
a direct dial of the peer's advertised real-time address, or a relayed
session -- in that order. It is ephemeral and node-attested like a
channel message (§8.10.2): never stored, never a canonical event. The
receiving node re-checks the sending node's `REALTIME` policy at delivery,
then delivers exactly as a local `/msg` does (live chat sessions via the
hub, every other session via the mailbox); an unknown, opted-out, or
offline recipient, or one who blocks the sender's `user@<fingerprint>`
(§6.4 Blocked people, issue #925), is dropped silently -- the sender already checked the
peer's node-wide presence (a node that has not yet pushed its presence
gets "couldn't confirm who is online there", never a blind send), which
the peer pushes the moment the session is tracked -- and receiving a
node-presence snapshot is itself what makes the receiving side track a
session that never subscribes to a channel, so the presence is answered
and later cleared. A remote user list is not a caller's to probe.

**Anchor advertisement and chained bridges (issue #270).** A node's
signed endpoint descriptor carries an optional `live_relays` list -- the
reliable nodes it is currently standing by at (§8.3; omitted when empty,
like `relays`) -- refreshed with every hello. Session establishment then
tries, in order: the registry; a direct dial; a single-hop rendezvous at
each relay the *target* advertises (reusing a session or dialing that
relay directly, since a relay is a full peer); a single-hop rendezvous at
each of the node's own anchors; and, only for a target relay the node
could not reach itself, a chained rendezvous: `relay_request {target,
requester, via_relay}` to its own anchor R1, which reuses or dials R2 and
forwards the request with `hops: 1`. R2 runs the ordinary rendezvous with
the target, treating R1's session as the requester's side, and its
`relay_ready` to R1 names `for_fingerprint` (whom the leg is for). R1
attaches to R2 as a raw leg -- no handshake; it is a pipe -- issues the
requester its own `relay_ready`, and splices the two legs: A–R1–R2–B,
Noise still end to end, both relays seeing ciphertext only. A forwarded
request is never forwarded again (`hops` is capped at one), so a chain
is at most two relays; each relay counts its bridge against its own pair
cap and applies its own byte-rate and idle bounds, and every failure
along the chain surfaces to the requester as an explicit `relay_reject`.

Caller-facing (Decision 3): `/msg user@node-fingerprint <text>` and
`/private user@node-fingerprint` in chat, and `[M]essage` on a remote
entry in Who's online. When no path exists, whatever the reason, the
caller sees one reason-free refusal -- "can't be reached for live chat
right now" -- pointing at Link mail; nothing fails silently. Cross-node
`/dm` invites stayed local-only in this vertical; issue #1368 added them.

---

### 8.11 Learning a third node's identity from a carrier (issue #630)

A hello bundle authenticates itself (§8.2, §10.6): only the holder of a root
key can produce a transition chain that verifies against it and a descriptor
signed by the key that chain currently authorizes. Who delivers the bundle
therefore does not enter into its verification, and a carrier can serve a
third node's bundle without being trusted for anything. It can withhold one;
it cannot forge one.

**The exchange.** A node that meets, in a carrier's inventory response, an
author or origin it cannot verify asks that carrier for the identities
concerned: `POST /link/v1/identities/{requester}` with a signed
`identity_request` naming up to 32 fingerprints. The request is authenticated
as every other pull is (a completed hello with the requester, the signed
requester matching the wire peer, this node named as responder, a signature
under the requester's current key, a five-minute freshness window, a bounded
nonce cache) and is gated by the same policy action as the inventory it
accompanies. The response carries the bundles the carrier holds and omits the
rest. A carrier answers for nodes behind content it has recently served (a
bounded, in-memory set of 4,096): the authors and origins its events name,
and the current origin of each event's resource, which is who signs a
closure, a tombstone, a moderator's edit or a file descriptor without being
named in it. It also answers for a node it relays for (§8.5), whose trust
objects it may be carrying (§12.7) and which names this relay in its own
descriptor anyway. It answers for nobody else. Answering for any fingerprint a peer names would let a peer on
probation, which is refused the peer list (§8.3), read the carrier's peer set
one guess at a time, addresses included. A page of events can name more nodes
than one request may, so a requester sends up to four requests at a time,
once before policy is consulted and once more for signers that only handling
the events revealed: a bundle gone stale, or an origin its event does not
name. An identity a carrier answered without, or could not be asked for
because it lacks the route, is not asked of it again for an hour; a request
that merely failed is repeated on the next occasion. A carrier serves the identities of its peers and of nodes it was itself
introduced to: the peer list shares only first-hand knowledge because a
secondhand address is a weaker claim the further it travels, but a bundle is
not a claim, and refusing to pass one on would break a board carried across
two hops. The requester accepts only bundles it asked for, and verifies each
exactly as it verifies a hello received directly.

**What an introduced identity is.** It can be used to verify what it signed
and a third party delivered: carried board, channel and file-area events, and
trust objects fetched from a carrier (§12.7). It is not a peer. Membership of the peer set is what every route checks
to decide who may push events, pull, serve as a relay or be sent Link mail,
and an introduction grants none of that; all of it still requires a completed
hello. Introduced identities are stored apart from peers for that reason. A
completed hello supersedes an introduction, and an introduction never
replaces a peer's record. At most 1,024 are kept, in memory and on disk; at
the cap the oldest goes, since a displaced identity is simply asked for again
when next needed. It takes with it what its introduction alone created: its
name observations, and its trust subject unless a SysOp has decided something
about it or evidence is held on it. A file area whose origin is known only by
introduction can be listed and not fetched from, since chunk transfer is
never relayed (§11.3), and the file screen says so.

**Trust.** An introduced node is registered as a trust subject and starts on
probation like any other (§12.4). Under the policy a running node enforces,
its content is therefore verified and still withheld until the SysOp
establishes it, by setting both its identity integrity and its resource
behavior. That is the point of introducing it *before* policy is consulted:
a node that is not a subject reads as probationary too, and its content is
refused with no way for the SysOp to see the node, let alone establish it.
It never graduates on its own: the thirty-day age requirement counts from the
introduction, but graduation also needs days of direct activity, which a node
never met cannot have. Establishing the node does not establish its callers.
A remote user is a subject of its own (§12.4) and starts on probation, so its
first posts arrive pending approval in the board's queue like any other
probationary remote user's. The familiar-name warning of §4.4 applies to an
introduced node, and matters more than for a peer: its name is what callers
read beside every post. The comparison is one-sided. An introduced node is
compared with every node on file; a node this one has met is compared only
with others it has met, and meeting a node re-judges any introduced node that
wears its name. Otherwise anyone could have a real peer flagged as an
impostor across the network by naming a node after it and posting once on a
shared board. Mail addressing resolves names among met nodes only, for the
same reason.

**Chain length.** A hello or introduction bundle carries at most 256 key
transitions (issue #1039). A chain is walked, one signature check per
transition, on every hello, introduction and compromise sweep, so its length
must not be the sender's choice. One past the cap is refused when the bundle is
parsed, before anything verifies it. Each rotation adds two transitions, so the
cap is over a hundred rotations.

**Stale bundles.** A third node's key transitions are not gossiped as events;
a `key_transition` event is accepted only from its own subject. They do travel
as part of a chain, though. A carrier's inventory response names, under
`key_chains`, the signing-key history of each node whose content it serves in
that response, when that history holds more than the first authorization and
is no longer than 64 transitions; at most 32 chains per response, never the
carrier's own or the requester's (issue #914). It also names the chain of
whoever signed what the requester declares it holds, when that signer has
marked a key compromised: a requester holding a stale copy is served nothing of
that signer's, and would otherwise never hear (issue #672). The requester merges each into
an identity it knows **by introduction only**, checked against the root key
already on file. A direct peer's chain comes only from that peer; an identity
the requester has not been introduced to is learned whole, by introduction.
Without this, a compromise never reached an introduced-only node: its bundle
still held the compromised key as current, old-key copies verified and were
accepted, and nothing failed to prompt a refresh. With it the same pull skips
them, per object.

**Stale copies.** A copy a node already holds, signed only by a key its
signer has since marked compromised, is not re-checked by being kept; it was
accepted when that key was current. When the node learns the compromise --
from the signer's own revoke or hello, or from a carried chain -- it looks
through what it stores of the inventory-carried kinds, among the copies that
name that signer as author or origin, for those that verify under that key and
under none the signer still stands behind, once per newly
compromised key, and marks them stale (`link_events.stale_signer`). A stale
copy is neither declared in inventory nor served, so the next exchange asks
for it again. The signer re-signed its content in its compromise response
(§4.5); a content id covers the envelope and not the signature, so the
re-signed copy has the same id and the same envelope. It replaces the stale
copy in place -- the stored envelope, and a carried genesis on its board,
channel or file area row -- and the projection built from that envelope is
left as it is: the post, line or file stays visible throughout and is never
doubled. Each hop learns the compromise in turn, so re-signed copies travel
through any chain of carriers without a change on the wire (issue #672). A
second stale copy offered meanwhile, by a carrier that has not heard yet, is
dropped.

A bundle on file can still be stale another way: after a rotation it no
longer verifies what the new key signs. The requester knows which identity the
failed check was made against, whether or not the event names it, and asks the
carrier for a fresher bundle. An event that builds on one set aside is set
aside with it, whatever its own check reports: the second edit in a chain fails
as not extending the current head, which is otherwise a refusal. A fresher
bundle, like a carried chain, is **merged** into the one on file: transitions
are append-only, so a bundle from before a compromise, which verifies against
itself alone, cannot make the compromised key current again. Its descriptor
replaces the one on file only if it is not older and is signed by the current
key of the merged chain.

**Events that cannot be used yet.** The inventory is a diff (§8.8): a node
declares what it holds and is sent the rest, one page of 200 events per pass.
An event it cannot accept would be sent again on every pass, and 200 of them
would be all it ever received. A carrier's response is therefore handled one
event at a time. One whose signer is unknown, or that builds on something not
yet received, or that policy refuses, is set aside and declared in later
requests as seen, so it is neither downloaded again nor allowed to crowd the
page. It is declared under its resource whether or not this node
carries that resource: a board whose origin is on probation here is not
carried *because* its genesis was set aside. It is asked for again when the
identity it waited for becomes known, or after an hour, or when trust changes
(issue #700):

- the SysOp overrides or clears an override for that node, which also starts a
  sync pass at once;
- a trust domain, reporter or sole-authority exception is changed in the
  trust console, which releases everything set aside and starts a pass;
- the node, or a user of it, graduates or recovers on its own, found by the
  re-evaluation every pass runs (§12);
- any of these is done where the running node cannot be told, with
  `python -m netbbs.admin`. The sync loop compares a generation read from the
  trust tables those writes already leave rows in (the config and policy audits,
  the overrides), once a pass, and releases everything set aside when it moves.
  No marker of its own is written.

**Every hour-long wait is shown and can be ended (issue #700).** Besides events
set aside here, a node also waits an hour before offering again an own event a
peer refused one by one (#897), asking a carrier again about an identity it
could not introduce (and refreshing a reporter known only by introduction),
and depositing trust objects again at a relay that refused them (#627). Each
hour keeps an unfixable refusal from costing a download or a request every pass;
none of them told a SysOp anything, so an operator who had just fixed the cause
could not see whether they had. Link status lists them under **Waiting**, with
when each is next tried, and **[R]etry now** ends them all and wakes the sync
loop for a pass at once: `LinkNode.sync_wake`, an event the loop's sleep waits
on beside its stop event. The deposit backoff is in the database, so retrying
moves the refusal's time back past the backoff and keeps its text for the vouch
screen. A newer descriptor for a node known only by introduction, arriving in a
peer list and verifying against its signing key on file, replaces the
introduced copy as a peer's would (issue #270), so a reporter that has just gained
a relay is reachable without waiting for the hourly refresh. At most 10,000
events are set aside, a third of what an inventory request can declare. Past
that the oldest are offered again and can crowd a page, and the remedy is the
SysOp's: establish or block the nodes concerned. Declaring it is not a claim to hold it, and costs nothing if read as
one: a responder that lacks the event would ask for it back, and a node only
ever pushes what it originated. An event that is *wrong*, from a peer, still
ends the response there; what was accepted before it is kept and persisted.

### 8.12 The node map (issue #767)

The node map lists the other NetBBS boards this node knows, how it knows each
one, when it last heard of it, and how a caller dials it. It is the network's
BBS list, as one board sees it. Callers and the SysOp see the same screen; the
SysOp's shows more.

**What is listed.** Every node this one has completed a hello with, every
node a carrier has introduced (§8.11), and every node that is the origin of a
board, file area or linked channel this node carries, even after its introduced identity was
displaced from the bounded store; such a node is listed under whatever name
this node still has for it, or the unknown-node label, and every field no longer
on file (provenance, dial-in, last heard, addresses) reads *unknown*. A node is
listed once, from its best-verified source: met before introduced, introduced
before candidate. For callers the list leaves out every node
that is quarantined or blocked in any of the identity, resource or content
dimensions (§12.2): this node refuses or withholds something of theirs,
so leaving them off tells a caller the truth about what reaches them here.
Operational reachability is not a trust state and hides nothing. Probation is not a reason to leave a node off; nearly every new
node is on probation. A node cannot ask to be left off. The list's worth is
that a missing board is one that callers cannot reach through this board,
because this board either does not know it or refuses its content. A list
that other SysOps could punch holes in would lose that meaning. The
list is not the whole network: peers pass on only the nodes they have met
themselves (§8.3), and there is no directory. The screen says so in its title,
"Nodes known to <board>", and nowhere else.

**What a caller sees.** Per node: its friendly name, with its DNS name beside
it only where two listed nodes share a friendly name (the DNS name is otherwise
in the detail view, and search matches it); how this board knows it, *direct* (met) or *via <carrier>* (introduced; *via another
node* when the carrier is itself left off the caller's list); when it was
last heard of, as a relative time, marked stale past 30 days; and, in its
detail view, the `dial_in` addresses its descriptor carries (§8.2) and the
boards and file areas this board carries from it that this caller could open
anyway, filtered by the same read gates as ordinary browsing. Link addresses, relay roles
and reliability are never shown to callers: they are how nodes reach each
other, and a caller cannot use them.

**Last heard.** The later of this node's own last direct contact with it (a
completed hello or events exchange, or an authenticated real-time session for
as long as it stays open; issue #766) and the `created_at` of its
newest valid descriptor, but never later than the time this node first
stored that descriptor, so a descriptor dated in the future cannot keep a node
fresh. A node signs a fresh
descriptor for every hello it builds, so that time is the node's own signed
statement that it was running, and a carrier who passes it on can withhold a
newer one but not forge one. A node that has never been heard from directly
still has one. Knowledge secondhand about a node's *address*, which peer lists
also refresh, is not contact and never advances it. Stale nodes are marked,
not removed.

**The SysOp's view.** The same list, plus the nodes callers do not see:
peer-list candidates, marked unverified and never shown to callers, since a
candidate has completed no hello and names nobody who vouched for it. A
candidate's descriptor is unverified, so it has no last-heard time; its row
shows *never heard from* and, labelled as such, when a peer list first
named it; and
quarantined and blocked nodes, with the state of each dimension. Each row adds the Link
addresses, relay roles and reliability. It replaces the peer list behind the
Link status screen. A node that is a trust subject here (issue #820) has trust
actions on its own screen, the same code as the subject's screen under Policy
trust: `[E]stablish` and `Bloc[k]` open the override editor with every
dimension and the state already chosen (the reason, the audited-deviation
confirmation and the audit are unchanged), `[C]lear override` can clear all of
a subject's overrides at once, and `[T]rust details` opens the subject's full
trust screen. A peer-list candidate has none: nothing about it is verified.

**Who may open it.** A node-wide minimum level set in the SysOp console,
defaulting to 0. A guest is an account (§4.6), so a SysOp who wants the map
kept from guests sets the level above the guest account's. On a node with Link
disabled the entry is not shown.

### 8.13 Public node pages (issue #1165)

A node that holds a managed netbbs.org name (the §16 issue #201 entry) and has
met Reliable Link gets a page at `https://www.netbbs.org/~<name>`. The page
says what the project knows about the node: its friendly name, its DNS name,
how callers dial in, when its name was registered, how long Reliable Link has
known it, when it was last heard, and its technical identity. A `/nodes/`
index lists every page, and each page offers a badge (`/~<name>/badge.svg`,
"member since" and the state, with no text the node supplied) for the
board's own website. The pages are the project's publication, not a
node's: they are built by the project, from two sources the project runs.

**Who gets a page.** A node whose managed-DNS registration is matured and whose
fingerprint Reliable Link has met directly (§8.12: a completed hello or
events exchange). Introduced nodes and peer-list candidates do not: Reliable
Link has not verified them itself. The DNS name is the address, because the
service already keeps it unique and screened.

**What it shows, and from where.** The registration date comes from the
managed-DNS service's registration. Everything else comes from Reliable Link's
node map *as a caller sees it* (§8.12), so a fact a caller there could not see
is never on the page either: Link addresses, relay roles, reliability, trust
states, and every node quarantined or blocked there. The friendly name and
`dial_in` addresses are read from the node's signed descriptor by the same
validating readers the node map uses, and are remote text, escaped where they
are written. "Known to Reliable Link since" is the first direct contact
Reliable Link has on record (`link_peers.first_contact_at`), set at a peer's
first direct contact and never moved. For a peer met before that column
existed (v7.18.0), migration 123 could only take the earliest time the row
held, and those times move with every contact, so on a node upgraded while in
regular contact the date is about the upgrade day (issue #1181). No record
holds the true first contact: `link_trust_subjects.first_verified_hello_at`
is also set when a node is only introduced, authors carried content or is
vouched for, and direct-activity days start at issue #1035. Reliable Link's
rows were corrected by hand from its own records, so the pages are right;
another node's `export-node-map` keeps the approximate dates for peers it
knew before upgrading. "Last heard" is the node map's
(§8.12). Reliable Link writes the caller's map as JSON with `python -m
netbbs.admin export-node-map`, which copies a fixed list of fields from each
entry, so a field added to the map later stays off the pages until it is
listed there.

**Release and boards (issue #1171).** The page also says which NetBBS release
the node runs, as major.minor only, and lists the Linked boards it carries
that its guest account may read. The patch level stays off the page: it
would tell the web which boards still run a release with a known hole. A
board is listed only when the node's designated guest, if guest login is on
and the account may sign in, passes the board's effective read and age gates,
the same check a caller meets; hidden and closed boards are left out. A board
behind a level, an age gate or an account is never named on the open web,
and a node without guest login lists none. Both facts travel in the node's
descriptor (§8.2) and only while its page is not off, so a SysOp who turned
the page off publishes nothing more for it.

**A tagline (issue #1172).** One line of the SysOp's own words, under the
name. It is remote text published on the project's domain, so it is held to
a hard shape on both sides: at most 80 characters, every one printable (no
control, format or bidi characters), runs of spaces folded to one. The
node's console refuses anything else and says why; every reader -- the
descriptor's, the export's, the generator's -- drops it instead of repairing
it. The page writes it HTML-escaped as a paragraph, never markup, never a
link (a URL stays text), and never on the badge. It travels in the
descriptor as `tagline`, omitted when empty, and only while the page is not
off. The project can take one node's tagline down without touching its
page: the generator's `--hide-taglines` file lists names or fingerprints,
and a list that cannot be read stops the build rather than letting a hidden
line come back.

**What it never shows.** The address the managed-DNS service last saw, which
is where the node's operator lives as often as where the board does; the
service's contact times; and any free text beyond the friendly name and the
tagline.

**Still on NetBBS Link.** A state with its date, not a yes or no: *active* when
last heard within 7 days, *quiet* within 30 days (the node map's stale point),
and *left* after that, or once the name is released. A page that has left
keeps its history and says so; it goes when the service forgets the released
name or another node takes it. A name the service operator revoked on a
complaint loses its page at once: the page would otherwise keep showing what
the complaint was about.

**Built without state of its own.** Every run of the generator
(`services/node_pages`) derives every page from the two sources, read-only,
and replaces the whole site. A name that passes to another node therefore
gives it a fresh page without anything to forget, and a page that is turned
off is gone at the next run. A run that cannot read a source writes nothing.

**Pages belong to fingerprints.** A page's history is keyed by the node's
fingerprint, which key rotation never changes (the §16 issue #624 entry). A
name released and registered again by another node starts a fresh page; the
new holder never inherits the old one's dates. A rename moves the page to the new name once it has matured.

**The SysOp's choice.** Shown but `noindex` by default; the SysOp can let search
engines index it, or turn it off, on the DNS screen's `[W]eb page`. The choice
travels as the descriptor's `node_page` field (§8.2), so the node states it
itself and nobody needs a login on the website; it takes effect at Reliable
Link's next contact with the node. The setting is offered wherever the name
has a page or will have one: pending, matured, and abandoned or released, since
a page that has left stays until it is turned off (issue #1177). A revoked name
has no page, so nothing to set.
