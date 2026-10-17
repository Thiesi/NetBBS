# NetBBS design — 7. NetBBS Link: identity, events, and compatibility

This is the normative design reference for this area of NetBBS. Section numbers are preserved from the design index so existing `§n.n` references remain stable.

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
object type. Object type is therefore **mandatory domain separation**: it is
intrinsic to the exact bytes that get hashed and signed, not a caller
convention a future event type could accidentally bypass by reusing another
type's shape.

**Canonicalization rule** (binding and language-independent — issue #11):

- Compact JSON: no insignificant whitespace, `":"`/`","` separators only.
- Serialize using ASCII escapes for non-ASCII characters, matching the
  reference canonicalizer's `ensure_ascii=True`, then encode as UTF-8.
  Supplementary characters use JSON surrogate-pair escapes. Equivalent
  unescaped Unicode JSON is not the same signed byte representation.
- Object keys sorted by exact Unicode codepoint sequence, at every nesting
  depth, after normalization (below).
- Every string is recursively normalized to Unicode NFC before serialization —
  object member names as well as values, at every nesting depth, not values
  alone. Two payloads differing only in normalization form (precomposed versus
  combining-mark sequences) canonicalize identically and share one content ID,
  whether the difference is in a value or in a key. Two distinct source keys
  that would normalize to the same string are a normalization collision and
  are rejected outright, the same way a duplicate wire key is (below) — never
  silently resolved by whichever one happens to overwrite the other.
- Floating-point values are forbidden anywhere in a hashed or signed field:
  float serialization is not reliably deterministic across languages and
  platforms.
- Any other JSON number is an integer and must fall within
  `[-(2^53 - 1), 2^53 - 1]` — the widest range exactly representable as an
  IEEE-754 double, matching JavaScript's/JSON's own safe-integer bound. No
  current field approaches this bound; the rule exists so a future field
  cannot silently produce bytes only an arbitrary-precision-integer language
  can hash consistently.
- `true`/`false` are booleans, never conflated with the integers `1`/`0`.
- A field that does not apply to a given event omits the key entirely.
  Storing it as an explicit JSON `null` is a **different, distinct canonical
  value** — `{"parent_post_id": null}` and `{}` must never share a content ID.
  Each event schema states, field by field, which behavior applies; a builder
  must not choose between omission and `null` ad hoc.
- Wire JSON containing the same key twice in one object, at any nesting
  depth, is rejected outright before it is canonicalized, hashed, or
  verified — never silently resolved by a "last one wins" rule. Two
  different JSON parser implementations can disagree about which duplicate
  value wins; a sender and receiver that disagree would each reconstruct a
  different object from what they would both call "the same bytes."

`netbbs.boards.content_id.canonical_json_bytes` is the sole canonicalization
implementation this codebase uses to produce these bytes. Anything that
signs, verifies, or content-addresses a Link event reuses it directly, never
a second, independently-maintained implementation that could quietly drift.
`netbbs.link.events.strict_json_loads` is the reference implementation of the
duplicate-key rule, applied to every message this node's transport reads off
the wire before that JSON becomes a candidate envelope.

Golden test vectors (`tests/fixtures/link_canonical_vectors.json`, checked by
`tests/test_link_canonical_vectors.py`) pin exact canonical bytes and content
IDs for representative payloads, including Unicode normalization,
omitted-versus-null, and integer-boundary cases. An independent implementation
must reproduce them exactly for canonical-format compatibility. This is
necessary but not sufficient for full Link interoperability: identity,
authority, transport, lifecycle, and trust behavior also require validation.

Existing Python behavior implements the rule above; it is not a separate,
looser specification of its own.

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

A `node_vouched_user` author (or a `link_message` sender/recipient) is
identified by the pair `(home_node_fingerprint, local_user_id)`, never by
`local_user_id` alone — a username is unique only within its own node, so the
pair, not the bare name, is the globally-scoped identity issue #11 asks for,
matching the `user@node-fingerprint` addressing form already used elsewhere.
`local_user_id` is the account's canonical, immutable, stored-case username
(§5); it participates in canonical bytes exactly as stored, after the same
NFC normalization every string field receives — never case-folded the way
local login/uniqueness lookups are, since a signed event fixes one exact
string forever, not a case-insensitive equivalence class.

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

Two events both validly extending the same predecessor at the same instant is
impossible by definition: a chain has exactly one current head, and an
incoming event either extends it (accepted) or does not (rejected as
reordering, or handled as a fork, per the object's own policy). `created_at`
is descriptive metadata for display and audit, never the mechanism that
orders or authorizes a chain extension — two genuinely successive events can
legitimately share one clock's timestamp resolution. Reconstructing a chain
from storage (for example, after a restart) must walk the same
`previous_event_id`/head-pointer links original acceptance already verified,
or rely on the storage layer's own locally-assigned, monotonic receipt
ordering — never re-sort on the payload's own claimed `created_at` alone.
Ordering among unrelated immutable events for local presentation (for
example, a board's post listing) is a separate, local concern with its own
stable tie-break, not a protocol question.

### 7.5 Version and unknown-event behavior

`netbbs_protocol` changes only for incompatible wire semantics. Additive event
types or optional fields need not force a protocol bump when old peers can
safely preserve them.

Peers exchange supported protocol information during authenticated contact.
Unknown event types or unsupported versions may be stored and relayed opaquely,
but must not be projected, displayed, or treated as authority by a node which
cannot interpret them.

**How a node keeps an unknown event type** (issue #1022). An event of a type
the node does not understand is taken with the rest of the batch it came in,
never refused for its type alone, and kept apart from the events the node
has verified (`opaque_events`).

- **What is checked.** Only its shape (a well-formed type name, a payload
  object, a signature string) and its size, at most 64 KiB. Its signature
  cannot be checked, because who must sign depends on the type.
- **Bounds.** At most 500 per sending peer, that peer's oldest going first,
  and none kept past 90 days. An event dropped by the bound is forgotten, so
  it can be taken again later.
- **Relay.** When its payload names a `board_id` this node carries, it is
  declared and served in that board's inventory like any board event, so
  peers pull it. Otherwise it is kept but not passed on. A node never pushes
  what it could not verify.
- **After an update.** At startup, each kept event whose type the node now
  understands goes through the normal checks as if just received from the
  peer that sent it: valid ones are accepted and projected, invalid ones and
  ones the checks pass over (a setting from a board's former origin) dropped,
  and ones waiting for something the node lacks (their signer, what they
  build on) kept for the next start.

A new event type therefore travels through nodes that do not understand it
yet, and takes effect on each of them once it updates.

Unknown fields within a known signed event must be preserved in the original
signed representation. A node must not strip and reserialize them in a way
which changes the signed bytes.

---
