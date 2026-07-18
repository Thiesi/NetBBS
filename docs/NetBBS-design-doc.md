# NetBBS architecture and product design

This document is the **current normative design** for NetBBS. It describes what
the system means, what users and node operators may rely on, and the boundaries
future implementations must preserve.

It is not a chronological decision diary. The former numbered sign-off rounds,
superseded alternatives, corrections, and intermediate implementation status
remain available through Git history. Do not reconstruct that chronology here.

Use project sources in this order:

1. this document for product, protocol, authority, and long-lived UX decisions;
2. current GitHub issues for unresolved work and acceptance criteria;
3. `docs/NetBBS-worklog.md` for durable implementation constraints and lessons;
4. source, migrations, tests, and Git history for exact implementation detail.

When these sources disagree, investigate and update the stale source. Do not
choose whichever answer is most convenient.

## Current status

- Phases 1 and 2 are complete as working standalone BBS software.
- The post-Phase-2 local additions—Communities, identity attestation,
  asynchronous personal mail, and self-update foundations—are substantially
  implemented.
- Phase 3 is active. NetBBS Link has real identity, canonical event encoding,
  authenticated HTTP transport, persistent peer/event state, seed and peer
  discovery, linked boards, tier-1 Link messages, outgoing-only-node relays,
  and deterministic multi-node fault testing.
- Phase 3 remains **private and experimental federation**. Phase 4 trust,
  reputation, and quarantine are the public-federation readiness gate.
- Later phases—real-time Link chat, advanced Link governance and Link
  Communities, and door-game compatibility—remain future work.

Implementation status belongs beside the relevant design rule below and must be
updated in place. Do not append victory narratives or test-count snapshots.

---

## 1. Product identity, terminology, and principles

### 1.1 Names

- **NetBBS** is the software project.
- **NetBBS Link** is the decentralized network connecting NetBBS nodes. “The
  Link” is acceptable informal shorthand.
- **Board** always means a message board.
- **Area** always means a file area. Never call a file area a board.
- **Link** prefixes features which exist specifically because of NetBBS Link:
  **Link message**, **Link Community**, **Link-wide** presence or chat.
- An ordinary local resource which participates in NetBBS Link keeps its normal
  noun with the adjective **linked**: linked board, linked channel, linked file
  area. Do not rename such resources into “Link boards” or similar proper
  nouns.

### 1.2 Foundational principles

NetBBS Link is foundational, not an add-on. Every durable local feature should
be designed with its possible network extension in mind, even when the local
version ships first.

The standalone BBS must remain complete and useful without NetBBS Link. Local
packages must not import Phase-3 federation code merely to perform ordinary
local work.

Node sovereignty is non-negotiable:

- no master node exists;
- no node, moderator, or majority vote can force another operator to store,
  display, or delete content;
- carrying remote content is always a local decision;
- moderation and trust signals may propagate, but enforcement remains local;
- remote closure or suppression events cannot remotely erase bytes already
  stored by another node.

The design prefers correctness, explicit authority, bounded resource use, and
visible failure over cleverness or silent degradation.

### 1.3 Non-goals

NetBBS is not intended to:

- recreate a centralized social network behind a terminal interface;
- promise anonymity from a user’s own home-node operator for ordinary
  password-only accounts;
- make every local feature network-wide immediately;
- replicate every file byte to every node;
- hide unresolved trust decisions behind signature verification alone;
- preserve historical BBS protocol constraints when they conflict with the
  native NetBBS model.

---

## 2. Platform, architecture, and scale

### 2.1 Target platform and stack

- Primary target: **NetBSD**, distributed eventually through pkgsrc.
- Runtime: Python 3.11+ with asyncio.
- Storage: one SQLite database per node, using WAL mode.
- Cryptography: PyNaCl/libsodium. Avoid dependencies which impose a Rust
  toolchain on the primary NetBSD target without a compelling benefit.
- User transports: Telnet, SSH, and web/xterm.js.
- Asynchronous Link transport: signed HTTP+JSON.
- Future real-time Link chat transport: Noise Protocol Framework.

### 2.2 Modular boundaries

The system is a modular package, not a monolithic script.

- `netbbs.auth` owns accounts and authentication.
- `netbbs.identity` owns cryptographic identities and addressing.
- `netbbs.boards`, `netbbs.files`, `netbbs.chat`, and `netbbs.mail` own local
  domain state.
- `netbbs.communities` owns Community state and inherited-value resolution.
- `netbbs.moderation` owns shared authorization and audit primitives.
- `netbbs.rendering` owns ANSI, reflow, screen-buffer, and editor-independent
  rendering behavior.
- `netbbs.net` owns user-facing flows, sessions, and transport orchestration.
- `netbbs.link` owns Link events, protocol, transport, persistence, discovery,
  synchronization, relaying, and local-to-Link bridges.
- `netbbs.storage` owns migrations, database connections, and execution lanes.

Domain functions are normally synchronous and `db`-first. Async session or
network code dispatches blocking work through a `DatabaseLane`. Link bridge
modules may depend on local domains; local domains must remain Link-unaware.

Rendering, protocol, storage, and transport concerns remain distinct. A generic
transport must not learn every event’s product semantics, and domain storage
must not decide how terminal output looks.

### 2.3 Expected scale

The primary deployment remains one modest self-hosted node operated by one
SysOp. The architecture targets:

- dozens to low hundreds of concurrent interactive sessions;
- small-to-medium Link deployments initially;
- correctness across multiple nodes even before large deployments exist.

SQLite is appropriate at this scale. The first expected scaling pressure is
write contention and queued background work, not raw interactive connection
count. Scale decisions beyond demonstrated workloads must be based on measured
behavior using the deterministic multi-node harness, not estimates alone.

---

## 3. User connectivity, rendering, and interaction

### 3.1 Connection methods

Telnet, SSH, and web/xterm.js are first-class user transports. Product behavior
should be transport-independent unless a capability genuinely requires a byte
stream, browser code, or another transport-specific primitive.

### 3.2 Rendering model

Use hybrid terminal rendering:

- ordinary screens use ANSI/VT100 text with reflow;
- cursor-addressed screen-buffer rendering is reserved for interfaces which
  benefit from it, such as fullscreen editors and pinned chat rows;
- the minimum supported terminal is 40x24;
- screens must degrade clearly rather than corrupting output when a terminal is
  too small or lacks a capability.

Untrusted user text is sanitized before styling. Trusted ANSI is added only
after sanitization. Nested colored fragments are composed independently because
an SGR reset does not restore an outer color.

The project intentionally provides two composition paths:

- a robust simple/line-oriented editor available everywhere;
- a nano-like fullscreen prose editor as a convenience preference.

ANSI art editing and prose editing are separate concerns. Syntax highlighting,
spell checking, and similar enhancements remain optional modules rather than
core editor assumptions.

### 3.3 Future presentation pass

Feature correctness and architectural stability take precedence over visual
polish during active roadmap work. A dedicated presentation and usability pass
is planned after feature completion. This does not excuse inaccessible,
ambiguous, or broken UI in the meantime; it only postpones broad theming and
cosmetic redesign.

---

## 4. Accounts, authentication, identity, and addressing

### 4.1 Account authentication

Local users may authenticate with:

- username and password, the default path;
- an optional personal Ed25519 keypair for passwordless challenge-response
  login.

The server never needs a personal user private key. Password-only users are
expected to remain the majority and must not be treated as second-class users.

### 4.2 Registration modes

A node has one registration mode:

- `open`: self-registration creates an immediately usable account;
- `approval_required`: self-registration creates a pending account which
  cannot authenticate until approved;
- `closed`: the public registration option is absent and accounts are
  SysOp-created.

Registration determines whether an account may exist and log in. Link
probation and reputation determine what an active identity may do; these are
separate axes.

### 4.3 Account levels and the usable-SysOp invariant

One integer level drives ordinary level gating. `SYSOP_LEVEL = 255` is the
reserved top level; SysOp is not a parallel role flag.

Promote, demote, disable, enable, approve, and hard-delete operations must never
leave the node with zero **usable SysOps**. A usable SysOp:

- has level at least `SYSOP_LEVEL`;
- is not disabled;
- is not pending approval.

The invariant is enforced transactionally against fresh database state, not
against a stale object supplied by a caller.

Hard deletion preserves content provenance through denormalized display labels
or nullable author/uploader references. Personal access rows and private state
which cannot meaningfully outlive the account are deleted according to explicit
foreign-key policy.

### 4.4 Human-facing Link addresses

The native cross-node address is:

`user@node-fingerprint`

The node fingerprint is derived from the node’s long-lived root identity, not a
DNS name. Network location may change without changing identity.

### 4.5 Identity tiers

NetBBS has three author/identity tiers.

#### Password-only user

A password-only user has no personal cryptographic identity. Link events use a
`node_vouched_user` author reference containing:

- the home-node fingerprint;
- an opaque local user identifier.

The home node signs on the user’s behalf. Key rotation and recovery are entirely
the node operator’s responsibility.

#### Personal-key user

An opt-in user keypair provides passwordless login and may later support
personally signed events. It is one key, not a root/operational hierarchy.
There is no bespoke recovery mechanism. Losing the key loses that key-based
identity and reputation continuity; the local account may still use ordinary
account recovery policy.

#### Node identity

Every node has:

- one long-lived root key whose fingerprint is the stable node identity;
- one operational signing key for events and content;
- one operational transport key reserved for Noise-based real-time transport.

The root authorizes and revokes operational keys through signed transition
records. Historical signatures remain verifiable by walking the transition
chain back to the root.

Routine operational-key rotation and compromise response do not change the
node address. Root-key loss or compromise has no cryptographic recovery in the
current design. Social/M-of-N recovery remains a possible future extension, not
an assumed capability.

Root and operational keys are generated at initial bootstrap. Rotation is a
guided SysOp action. Root-key custody is part of ordinary node backup and
restore rather than requiring an HSM or offline ceremony.

---

## 5. Authorization, moderation, and identity attestation

### 5.1 Resource gates

Boards, file areas, channels, and Communities may apply:

- minimum user level;
- minimum age;
- verified-name requirements;
- visibility and membership policy appropriate to the resource type.

Resource-level scalar settings are nullable:

- `NULL` means inherit the containing Community’s default, if any, otherwise
  use the system default;
- an explicit value, including `0` or `none`, overrides inheritance.

A Community default is a default, not a mandatory floor or ceiling. A child
resource may currently loosen or tighten it explicitly.

### 5.2 Moderator authority

Boards and file areas distinguish read and write access. Channels use a join/
participation gate rather than asynchronous read/write separation.

Moderator permissions are composable primitives such as read, write, edit,
delete, approve, manage members, mute, ban, and topic control as appropriate.
Moderators need not be SysOps.

Authority scopes are:

1. per-object;
2. Community-blanket, applying to present and future matching resources in one
   Community;
3. local-blanket, applying to local-only matching resources on one node;
4. Link-blanket, applying to linked matching resources carried by a node.

Link-blanket authority does not imply local authority. A person who needs both
must receive both explicitly.

Only a SysOp can grant or revoke blanket authority or change node
configuration. A suitably authorized Link-blanket moderator may initiate a new
linked resource, but:

- the node identity signs and owns the genesis event;
- the initiating human is recorded separately for audit;
- initiation grants no power to appoint further blanket moderators or alter
  unrelated resources.

Every moderation action is audited. Moderator changes to immutable Link content
must be represented as new authorized events, never silent mutation.

### 5.3 Board and file moderation

A board or file area may require approval before new posts/uploads become
visible. Local maintenance follows:

`active -> expired -> deleted`

with a grace period between expiration and deletion. Pin and expiry-exemption
currently use the existing edit permission. Local pruning never becomes a
network-wide deletion instruction.

### 5.4 Channel visibility and membership

Channel visibility and join policy are separate:

- listed or hidden;
- open to otherwise eligible users or members-only.

`hidden + open` is permitted but is obscurity, not access control.

Local invitations may be immediate for online users and retained with expiry
for offline users. Membership persists until revoked unless a channel defines
otherwise. Linked-channel membership eventually becomes signed governance;
it is not represented as end-to-end confidential from participating node
operators.

### 5.5 Identity attestation

Age and verified-name policy is local and jurisdiction-specific. NetBBS provides
mechanism, not a universal legal definition.

Users may provide nullable, independently visible:

- birthdate;
- display name;
- location;
- other profile fields.

Age is computed from birthdate at check time. It is never stored as a derived
current age. If a resource has an age gate and no usable birthdate or verified
age attestation exists, access fails closed.

A `user_attestation` records:

- subject user;
- attribute (`age` or `name`);
- attested value;
- verifier identity;
- signature;
- creation time;
- Link-visibility preference.

A verifier may use a personal key, or the node may vouch for a password-only
verifier. Verified values take precedence over self-reported values.

`can_verify_identity` is a separate SysOp-granted boolean, not another content
moderator tier.

Name requirements are:

- `none`;
- `verified`, requiring a verified name without compulsory display;
- `verified_and_displayed`, requiring resource-scoped visible disclosure.

A verified name never overwrites the user’s self-chosen display name. When a
resource requires display, render:

`display_name_or_username (=Verified Real Name=)`

The complete `(=...=)` unit uses a dedicated trusted color. The `=` marker
remains visible when color is stripped or inaccessible. User-controlled display
names may not contain the reserved `=` marker, and untrusted text cannot inject
ANSI styling.

Disclosure is resource-scoped. A resource which requires visible identity must
not cause the real name to leak into unrelated screens.

Remote propagation of attestations requires:

- the subject’s explicit opt-in;
- Phase-4 trust rules allowing the receiving node to decide whether to trust the
  remote verifier.

A carrying node may always apply its own local attestations to its own users
when enforcing a carried resource’s local age/name policy.

---

## 6. Local product domains

### 6.1 Message boards

Local boards provide:

- categories and stable navigation IDs;
- posts and replies;
- moderation and pending approval;
- expiry, pinning, and exemption;
- immutable revision history for edits;
- simple and fullscreen composition;
- local search/navigation foundations.

A visible edit is a revision, not destructive replacement of history. Any
threading or revision semantics which affect Link event IDs or propagation must
be settled in Phase 3; only presentation refinements may wait until Phase 7.

### 6.2 File areas

Local file metadata lives in SQLite; file bytes use content-addressed filesystem
storage. Areas support permissions, moderation, expiry, and Zmodem transfer on
byte-capable transports.

File bytes are node-local. NetBBS Link will distribute catalogue/descriptor
information and fetch content on demand in bounded resumable chunks. It will
not replicate every file to every node.

### 6.3 Real-time chat

Local chat is typed event traffic, not preformatted strings. Initial event types
include:

- ordinary message;
- `/me` action;
- online private message;
- join/leave;
- alias change;
- system notice.

An optional `/nick` alias is presentation metadata only. Every context retains
the authenticated canonical identity, and permissions, moderation, blocking,
reputation, and addressing always use canonical identity.

Local chat includes bounded persistent channel scrollback, presence, away
state, invitations/membership, `/who`, `/whois`, `/names`, `/list`, `/join`,
`/leave`, `/topic`, completion, and online private conversation.

`/msg` and `/private` remain ephemeral and online-only. They never silently
fall back to asynchronous mail.

Phase 2 uses one active channel per session. Multiple simultaneous memberships,
background delivery, and Link-wide presence wait for Phase 5.

### 6.4 Personal mail

Local asynchronous mail is a persistent domain distinct from chat `/msg`.
Messages have sender/recipient views, subject, body, read state, and independent
delete state. The row is removed when neither side retains it.

Recipient mailboxes are bounded. When full:

- the oldest already-read message may be evicted to make room;
- unread mail is never silently discarded;
- if no safe eviction exists, delivery fails explicitly.

Local mail is the domain extended by Link messages; Link mail does not create a
parallel mailbox UI.

### 6.5 Communities

A Community is a topic-oriented coordination/container object above boards,
channels, and file areas. It does not merge those domains or change their
behavior.

Each board, channel, or file area has zero or one Community. “Uncategorized” is
the absence of a Community, not a synthetic row. Categories remain a separate
layer below Communities.

Communities provide:

- topic-first navigation;
- description and visibility;
- inherited level, age, and name-verification defaults;
- Community-scoped blanket moderator grants;
- a future unit for Link carry and governance.

The main navigation exposes:

- Communities;
- Uncategorized resources;
- Jump/search by resource type.

Each path leads to the same resource-type submenu and then the normal board,
channel, or area browser. Resources unrelated to Communities—mail, directory,
profiles, preferences, and administration—retain their own navigation.

Community-scoped category views must filter at the query layer so a category
used by resources in several Communities does not leak another Community’s
resources into the current view.

Deleting a Community:

- sets member resources to no Community;
- revokes Community-scoped blanket grants;
- shows the blast radius before confirmation.

Existing nodes migrate safely because the nullable Community reference leaves
all existing resources Uncategorized until a SysOp assigns them.

#### Link Communities

A Link Community is the same Community object announced through a signed Link
event, not a separate table or local type.

Two same-named Link Communities from different origins remain distinct.
Existing local Communities may be promoted into Link scope.

Carrying a Link Community is intended to carry its present and future member
resources by default, while retaining visible per-resource and whole-Community
local exclusions. Origin defaults are recommendations; carrying-node overrides
win locally.

Actual Link Community event schemas, signed membership changes, and advanced
governance are Phase 6 work.

### 6.6 Self-update

The updater uses explicit GitHub Releases over HTTPS rather than arbitrary
branch HEAD. Current foundations include version comparison, release checking,
safe archive extraction, persisted state, and database snapshot/restore
primitives. Scheduled release checking exists; complete apply/re-exec and
rollback orchestration still requires operational validation.

Before an update which may migrate the schema, snapshot the database so binary
and schema can roll back together.

The intended apply model is:

- check at startup, manually, and on a daily schedule;
- drain live sessions before a live-node restart;
- replace the on-disk release and re-exec the process;
- retain the previous release and restore it, together with the database
  snapshot, after failed startup.

HTTPS and GitHub are currently the update trust boundary. Additional release
signing is not required by the present design, though it remains a possible
hardening step.

The automatic check/apply policy must have an operator-visible off switch.
Future pkgsrc packaging will need an explicit ownership policy rather than
silently competing with the self-updater.

---

## 7. NetBBS Link: identity, events, and compatibility

### 7.1 Phase-3 safety boundary

Phase 3 is for private, controlled federation. It may use local blocklists as an
interim abuse control, but it is not safe to expose broadly to unknown peers
until Phase 4 defines and implements trust, probation, reputation, and
quarantine.

### 7.2 Canonical event envelope

A durable Link event uses a signed envelope:

```json
{
  "netbbs_protocol": 1,
  "object_type": "...",
  "payload": { ... }
}
```

The content ID and signature cover the entire canonical envelope, including the
object type. The object type therefore provides domain separation.

Current canonical encoding uses compact deterministic JSON, recursive Unicode
NFC normalization, and rejects floating-point values. Protocol-visible schemas
must define allowed values and omit-versus-null behavior deliberately.

Issue #11 remains the authority for unfinished interoperability details,
including complete cross-language canonicalization rules, strict numeric and
unknown-field policy, and golden signed test vectors. Existing Python behavior
must not be mistaken for a complete language-independent specification.

### 7.3 Author references

An event author is a tagged union:

- `node_vouched_user`;
- `user_key`;
- `node`.

The verifier resolves the appropriate current signing key and, for node-owned
keys, validates its transition history back to the root identity.

Only the author tiers implemented for a specific event type are accepted. The
existence of the tagged union does not imply every tier already works for every
feature.

### 7.4 Immutable content and state-changing chains

There are two event classes.

#### Immutable creation events

Examples include a board post or file descriptor. Their content ID identifies
the complete immutable object. Nodes may differ only in whether they possess or
locally suppress it.

A random nonce distinguishes two intentional posting actions with otherwise
identical visible content.

#### Per-object state chains

Edits, metadata changes, grants/revocations, key transitions, origin transfer,
closure, and membership changes extend a per-object chain. Each new event
references the state/event it extends.

Effective state is the projection/fold of the valid chain. An incoming event is:

- a valid extension of the current state;
- an already integrated ancestor, therefore an idempotent no-op;
- a genuine competing extension/fork requiring the object’s defined policy.

Transport deduplication is only a performance optimization. Permanent replay
safety comes from the authoritative object state or chain, not from a purgeable
“seen ID” cache.

Tombstones are chain events, not deletion of history. Local byte pruning cannot
resurrect state if the permanent projection rules remain intact.

### 7.5 Version and unknown-event behavior

`netbbs_protocol` changes only for incompatible wire semantics. Additive event
types or optional fields need not force a protocol bump when old peers can
safely preserve them.

Peers exchange supported protocol information during authenticated contact.
Unknown event types or unsupported versions may be stored and relayed opaquely,
but must not be projected, displayed, or treated as authority by a node which
cannot interpret them.

Unknown fields within a known signed event must be preserved in the original
signed representation. A node must not strip and reserialize them in a way
which changes the signed bytes.

---

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

The protocol logic remains transport-independent. The `aiohttp` adapter is the
boundary translating protocol messages to real HTTP requests and responses.

### 8.3 Bootstrap and peer discovery

Bootstrap sources are combined, not exclusive:

1. operator-configured seeds;
2. software-shipped fallback seeds;
3. a live supplementary seed list fetched over the existing GitHub update
   channel;
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
- storage is bounded;
- pickup authenticates the intended recipient;
- the recipient re-runs normal event verification rather than trusting the
  relay’s claim;
- relaying does not introduce strangers or weaken the rule that sender and
  recipient identities must already be known sufficiently to verify and
  encrypt.

Reliability scoring is direct-observation operational data, not Phase-4 social
reputation.

### 8.6 Current synchronization model

Current background sync:

- contacts configured/cached seeds and candidates;
- performs hello/peer discovery;
- pushes the complete locally originated supported event set;
- relies on idempotent acceptance;
- sends targeted Link mail directly or through a selected relay.

This is intentionally simple but incomplete.

Not yet present:

- generic inventory exchange and pull-based anti-entropy;
- efficient per-peer deltas;
- general multi-hop propagation of arbitrary carried content;
- complete retained-event and dedup-purge policy;
- public-network backpressure and abuse handling.

A node which receives Alice’s board events does not automatically relay them to
Carol under the current direct-pairwise model unless explicit future relay/
anti-entropy behavior is added.

### 8.7 Store-and-forward goal

The eventual model supports nodes which are offline for extended periods and
resume synchronization later. Causal relationships come from parent/chain
references; timestamps are secondary ordering data, and content IDs provide a
deterministic final tiebreak for truly concurrent siblings.

Persistent dedup uses exact IDs, not Bloom filters. False-positive data loss is
unacceptable. Retention cleanup must never turn an old state-changing event into
something re-applicable.

---

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

Self-authored edits become chained `board_post_edit` events. The original post
remains immutable. Moderator edits and tombstones require separate authorized
event types and advanced governance.

### 9.3 Carry and local materialization

A peer accepting a valid board genesis materializes a real local board copy so
users can browse carried content through the normal board UI. Carrying is more
than retaining raw protocol events.

Linked resources are carried by default within the supported topology, with a
visible local exclusion option. A local exclusion must be represented honestly
as “not carried on this node,” not indistinguishable disappearance.

Origin recommendations never override the carrying node’s local access,
moderation, retention, or legal policy.

### 9.4 Origin succession

Routine node signing-key rotation is handled by the node key-transition chain
and does not transfer resource ownership.

Voluntary board-origin transfer requires mutual consent:

1. the current origin signs an offer naming the proposed new origin;
2. the proposed origin signs acceptance;
3. peers project the new origin only after both valid events.

Only one outstanding transfer offer is meaningful at a time.

If the current origin loses all valid signing authority and cannot publish a
transfer, the board is locally recognizable as orphaned. Existing content
remains available, but no new origin-authorized state is accepted.

A fork is a new resource/genesis with a new origin and an optional
non-authoritative `forked_from` reference. Each node independently chooses
whether to carry the original, the fork, both, or neither.

Channel-side Link lifecycle will reuse these principles after linked channels
exist; there is currently no channel genesis protocol.

### 9.5 Remaining board governance

Still future or incomplete:

- signed closure/archive projection where not already implemented;
- linked-board moderator grants and revocations;
- moderator edits and tombstones;
- general relay/anti-entropy beyond direct peers;
- Link-blanket governance surfaces and audit feeds.

---

## 10. Link messages

### 10.1 Product model

A Link message extends the ordinary local mailbox. The user composes to a
`user@node-fingerprint` address and reads the result in the same inbox/sent UI
as local mail.

The message is point-to-point to one recipient node, not flood-filled public
content.

### 10.2 Confidentiality guarantee

The implemented confidentiality tier is `tier1_home_node_key`:

- subject and body are encrypted to the recipient node’s current signing-key
  material converted to X25519;
- network peers and relays cannot read the content;
- the recipient’s home-node operator technically can decrypt it.

This is not end-to-end encryption against the home node and must not be marketed
as such.

The X25519 key is derived from the existing Ed25519 key through libsodium’s
supported conversion. This deliberately couples encryption and signing-key
rotation and accepts the larger compromise blast radius in exchange for a much
simpler lifecycle and no separate key-distribution protocol.

Static recipient keys do not provide forward secrecy. A later compromise can
expose previously captured messages.

`tier2_personal_key` remains reserved vocabulary but is permanently out of the
planned product scope unless NetBBS gains a real client-side decryption
architecture. Server-side terminal sessions cannot render content which the
server is forbidden to decrypt, and a web-only feature is not sufficient to
justify a parallel mail system.

### 10.3 Delivery state

Transport receipt is not user delivery.

Separate signed events represent:

- accepted into the recipient mailbox;
- bounced because of unknown recipient, full mailbox, blocking, or another
  defined terminal failure;
- future expiry where retry policy requires it.

Outbound messages remain pending until an accepted or bounced event arrives.
Delivery through a relay does not change the acceptance semantics.

### 10.4 Routing limitations

Direct delivery requires a known peer with a usable endpoint. An outgoing-only
recipient may be reached through relays it has selected and published.

The current system does not introduce total strangers. The sender must already
know enough authenticated peer/key state to encrypt to the destination, and the
recipient must know enough sender state to verify the message.

### 10.5 Metadata and abuse controls

Only routing information needed by transport or relay infrastructure should be
visible outside the encrypted body. Subjects and bodies remain encrypted.

Mailboxes, relay storage, retries, and pending acknowledgements are bounded.
Blocking and quota failures must be explicit; unread data is not silently
removed to make delivery appear successful.

---

## 11. Remote file areas

A linked file area remains owned and stored by its source node.

NetBBS Link should propagate catalogue/descriptor events sufficient to discover:

- area identity and metadata;
- files, hashes, sizes, and source availability;
- access policy needed before attempting transfer.

File content is fetched on demand in bounded resumable chunks. Chunk transport
may use signed JSON metadata plus raw HTTP bodies; it is not required to embed
large bytes as base64 JSON.

Chunk IDs and transfer IDs provide exact deduplication. Completed content is
stored once by hash even when referenced from several catalogues.

Remote file catalogue discovery and chunk transfer are Phase-3 work and are not
yet implemented end to end.

---

## 12. Trust, reputation, probation, and quarantine

Phase 4 defines the public-network security model. The design direction is
settled; the precise threat model remains issue #55.

### 12.1 Local trust view

There is no global reputation ledger or authoritative network vote. Each node
computes its own trust view from:

- direct observation;
- optional signed signals from identities it already trusts.

Node reputation provides a baseline; user reputation may refine it.

### 12.2 Probation

New nodes and users begin with restricted capability, normally read-only.
Graduation combines elapsed time and vouching by already-established identities.
Exact thresholds and what qualifies as established remain to be specified.

### 12.3 Objective and subjective reports

The protocol must distinguish:

- objectively verifiable abuse, such as invalid signatures, malformed events,
  conflicting claims, or measurable flooding;
- subjective moderation judgments, such as spam, harassment, illegality, or
  off-topic content.

Objective reports may carry evidence which another node can verify. Subjective
reports remain local opinions and must not become a disguised network-wide
verdict.

### 12.4 Quarantine

Quarantine is a local circuit-breaker decision based on signed observations and
the receiving node’s own policy. No node objectively “enters quarantine” for
the entire network.

The eventual mechanism must define:

- report categories and evidence;
- expiry, revocation, and replay protection;
- independent-flagger rules;
- Sybil/collusion resistance;
- reputation weighting;
- bounded signal propagation;
- reversible recovery.

A node may locally give an external jurisdictional authority key unusually high
weight, but this is an operator opt-in policy and has no effect on nodes which
do not configure it.

---

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

### 13.4 Backup and restore

Use SQLite’s online backup API; never copy a live WAL database as one inert file.

Backup order:

1. database snapshot;
2. content blobs;
3. node identity material as part of the same recoverable set.

This order may leave harmless unreferenced blobs but must not leave database
references to absent blob files.

Restoration resumes the same node identity. Running the old and restored
instances simultaneously is unsupported and can create two active copies of one
cryptographic identity.

### 13.5 Bounded remote influence

Every remotely influenced queue, mailbox, retry set, retained-event collection,
transfer, relay store, and bandwidth consumer needs:

- an explicit limit;
- defined backpressure/rejection behavior;
- retry and terminal-failure policy;
- SysOp-visible state;
- safe defaults.

Security state and unread user data must not be silently discarded.

### 13.6 Operational control surface

Issue #60 remains the authority for the incomplete production operating model,
including:

- generic persistent outbound-work items;
- retry, backoff, dead-letter, replay, and cancellation;
- peer health and sync-lag visibility;
- disk, event, mailbox, relay, and bandwidth quotas;
- integrity checks and crash recovery;
- bounded diagnostic log retention without content logging;
- protocol/database upgrade and rollback compatibility;
- graceful drain of Link work during shutdown;
- disaster recovery from stale backups.

An externally operated persistent Link node should not be considered production
ready before these controls exist and have been exercised.

---

## 14. Testing and interoperability requirements

### 14.1 Deterministic distributed testing

Every implemented Link event family must be exercised through independent node
instances and serialization under applicable scenarios:

- duplicate delivery;
- reordering;
- dropped messages;
- partition and healing;
- restart and state reconstruction;
- malformed or forged events;
- key rotation/revocation;
- convergence after valid resends.

The harness grows with real event families. A generic harness which cannot drive
the real protocol is not sufficient.

### 14.2 Real boundaries

Use real:

- SQLite files and independent connections for concurrency and migration tests;
- sockets for transport adapters;
- serialization between separate protocol objects;
- reconstructed objects after restart;
- bounded readiness polling instead of arbitrary sleeps.

Mocks may isolate failures but do not prove the boundary being claimed.

### 14.3 Prove regression tests

When practical, demonstrate that a new regression test fails without the fix.
A test which passes both before and after the supposed fix has not proved the
bug.

Scripted terminal tests must fail fast on input exhaustion and confirm they
reached the intended path after menu or signature changes.

### 14.4 External validation

Automated tests cannot prove visual behavior or third-party interoperability.
Before calling affected functionality production ready, test as applicable with:

- a real OpenSSH client;
- real Telnet terminals;
- SyncTERM/lrzsz or another external Zmodem implementation;
- a real browser/xterm.js session;
- resize, color, CP437 art, editor, bell, and echo behavior;
- long-running operation across midnight and DST changes;
- update, restart, backup, and restore on NetBSD.

---

## 15. Roadmap and phase boundaries

### Phase 1 — Foundation — complete

- modular runtime and SQLite storage;
- node/user identity foundations;
- password and keypair login;
- Telnet, SSH, and web transports;
- ANSI rendering and input plumbing;
- level/permission foundations;
- local boards, file areas, and chat;
- local blocklist foundation.

### Phase 2 — Complete standalone BBS — complete

- local moderation and approval workflows;
- maintenance/expiry;
- user directory, profiles, and finger-style lookup;
- channel visibility, invitations, membership, and moderation;
- local private chat, presence, aliases, and completion;
- SysOp administration and node controls;
- TUI/screen-buffer foundations;
- ANSI and prose editors.

### Post-Phase-2 local additions — substantially complete

- local Communities and Community-scoped authority;
- identity attestation and gates;
- local asynchronous mail;
- self-update foundations and scheduled checks;
- registration-mode and account-lifecycle refinements.

### Phase 3 — Link connectivity and asynchronous services — active

Implemented or substantially working:

- root/operational node-key lifecycle;
- canonical event bytes and signed transition events;
- authenticated hello and endpoint descriptors;
- real HTTP+JSON transport and node startup integration;
- persistent peer and event state;
- foreground/background database lanes;
- configured seeds, live seed refresh, peer-list exchange, and candidate
  fallback;
- deterministic multi-node fault harness;
- linked-board genesis, posts, self-authored edits, local materialization, and
  origin transfer/orphan/fork behavior;
- tier-1 Link messages with accepted/bounced delivery state;
- reliability scoring, relay consent, automatic relay selection, and bounded
  relay mailboxes for outgoing-only recipients.

Still required for Phase 3 completeness:

- finish issue #11’s language-independent canonical protocol specification and
  vectors;
- inventory/pull-based catch-up and efficient synchronization;
- correctness-preserving event/dedup retention;
- linked channels and channel lifecycle;
- remaining linked-board governance, closure, moderator edits, and tombstones;
- remote file catalogue and on-demand chunks;
- unread/follow/activity discovery from issue #56;
- issue #60’s operational controls and recovery model;
- broader real-world multi-node deployment validation.

### Phase 4 — Trust, reputation, and public readiness

- formal threat model from issue #55;
- node/user reputation;
- probation and vouching;
- objective evidence and subjective moderation separation;
- local quarantine decisions and recovery;
- remote attestation trust.

No public/untrusted federation claim precedes this phase.

### Phase 5 — Real-time Link chat

- Noise transport using node transport keys;
- Link-wide typed chat events, presence, and discovery;
- multiple simultaneous channel memberships and unread/background delivery;
- Link-wide live private chat, distinct from asynchronous Link messages;
- decide whether and how trusted recent scrollback is offered to joining nodes.

### Phase 6 — Advanced Link governance and Link Communities

- linked channels and signed membership/topic governance;
- Link-blanket moderator grants and authorized moderation events;
- advanced creation, closure, and lifecycle surfaces;
- Link Communities and signed Community membership/carry changes;
- curated governance audit board and live activity feed.

### Phase 7 — Doors and legacy compatibility

- design and prove the native door sandbox and versioned capability API;
- UI-only threading refinements;
- classic DOS door compatibility through the same constrained capability
  boundary.

Issue #63 must be resolved before door implementation begins.

---

## 16. Open design decisions

GitHub issues are authoritative and may evolve beyond this summary.

### Issue #11 — canonical Link format

Still needs a complete interoperable specification for canonical values,
unknown/duplicate fields, numeric ranges, schema/version behavior, and golden
vectors. Current Python encoding is implementation, not the complete spec.

### Issue #56 — unread, follows, activity, and search

Define stable per-user read state, follows/favourites independent of node carry,
Community activity summaries, replies/mentions, new file discovery, and local
search over carried content. Arbitrary user search queries must not be broadcast
Link-wide by default.

### Issue #60 — production operations

Implement bounded persistent work queues, retry/dead-letter control, quotas,
health/status surfaces, integrity checks, log retention, backup/restore drills,
and upgrade/disaster recovery.

### Issue #55 — trust and quarantine

Define the Phase-4 threat model, evidence types, independence and Sybil rules,
signal rate limits, quarantine thresholds, reversibility, and local policy
semantics.

### Issue #63 — door isolation

Define process/jail/container boundaries, filesystem/network access, resource
limits, terminal mediation, session capability API, audit, crash cleanup, and
DOS adapter behavior.

### Deliberately deferred without active issue

- social/M-of-N node-root recovery;
- multiple simultaneous personal user keys/devices;
- true client-side Link-mail encryption;
- Link-chat initial scrollback policy;
- schema fingerprinting beyond SQLite `user_version`;
- Community defaults as mandatory floors/ceilings;
- cross-network gateway adapters such as FTN/FidoNet.

A deferred topic becomes normative only after an explicit design decision. Do
not infer commitment from its appearance in this list.

---

## 17. Maintaining this document

Add or change text here only when it affects:

- product semantics;
- protocol or compatibility guarantees;
- authority and trust boundaries;
- persisted data meaning;
- long-lived user or SysOp behavior;
- roadmap dependency or phase scope.

Keep one current answer per topic. Replace superseded text instead of appending
correction paragraphs. Preserve only rationale which prevents a plausible but
harmful alternative from being chosen again.

Do not add:

- numbered decision rounds;
- implementation walkthroughs;
- changed-file or test lists;
- passing-test totals;
- debugging transcripts;
- transient “next up” status;
- stale issue-resolution commentary.

Use issues for unresolved work, commit/PR descriptions for change narratives,
the engineering record for durable implementation constraints, and Git history
for archaeology.
