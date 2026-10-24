# NetBBS design — §12. Trust, reputation, probation, and quarantine

Part of the [NetBBS design](../NetBBS-design-doc.md). Section numbers are stable: cite this area's rules by their section numbers (§12.n).

## 12. Trust, reputation, probation, and quarantine

Phase 4 defines the public-network security model. This section is the
normative threat model and policy contract. It does not make a Phase-3 build
safe for public federation by documentation alone; the persistence,
enforcement, operator UI, and tests described here must exist first.

### 12.1 Security goals and attackers

The model protects a node's availability, storage, users, and local policy
without creating a network-wide authority. It must remain safe when facing:

- a malicious user acting through an honest home node;
- a malicious node which signs abuse, lies, selectively relays, or vouches for
  abusive users;
- Sybil identities and colluding nodes;
- a compromised established node or configured trust reporter;
- replaying, withholding, reordering, or selectively forwarding intermediaries;
- partitions, clock skew, stale observations, and compromise recovery.

A signature proves key control, not honesty or independence. A successful dial
proves reachability, not trust. Seeds, discovery, peer introduction, relay
consent, and carrying the same resource confer no reputation.

Every enforcement decision remains local. Nodes may disagree and may override
automatic policy. No signal can force another operator to hide, delete, relay,
or accept anything.

### 12.2 Separate trust dimensions

NetBBS does not compute one scalar reputation score. A local trust view keeps
these dimensions separate:

- **identity and protocol integrity:** signatures, key lifecycle,
  canonicalization, equivocation, and authorization;
- **resource behavior:** flooding, quota evasion, retries, availability claims,
  and relay/storage use;
- **content conduct:** spam, harassment, illegality, off-topic behavior, and
  other moderation judgments;
- **operational reachability:** this node's own dial outcomes.

Operational reachability is routing data only. `netbbs.link.reliability` may
rank dial or relay candidates but must never affect security or content trust.

Node and user trust are separate. Establishing a home node does not establish
every user on it; one abusive user does not quarantine an otherwise honest home
node. A password-only `node_vouched_user` is evaluated as the stable pair
`(home_node_fingerprint, opaque_local_user_id)`, never by display label.

Remote identity attestations have a separate local trust list. Trust-report
authority does not grant age/name-attestation authority, or vice versa.

### 12.3 Local roles, states, and precedence

Establishment and authority to influence policy are different roles:

- an **established identity** has graduated or was established manually;
- a **trusted reporter** is explicitly configured for named dimensions and
  categories. `dimension:*` is shorthand, expanded when the grant is saved into
  every category of that dimension the node's version knows; a category added
  by a later version is never granted implicitly. A category the version does
  not know may still be named (its signals are retained without effect), and
  the console says so when the grant is saved (issue #745);
- a **trust domain** locally groups reporters which may share control or
  incentives.

There is no trust-anchor role. An earlier version let a SysOp record trust
anchors, but no trust decision read them, so they were removed (§16, issue
#1528).

No role is inferred transitively. A vouch may help probation graduation but
cannot create a reporter, attestation authority, or trust domain.
When a SysOp selects a node by friendly or DNS name for one of these
security-policy roles, the UI shows the resolved technical identity. If that
fingerprint has an undismissed cryptographic-identity warning, the warning is
shown and explicit confirmation of that technical identity defaults to no
before policy can change. Entering the complete fingerprint already supplies
that confirmation.

Per identity and trust dimension, local state is `probationary`, `established`,
`quarantined`, or `blocked`. Manual block has highest precedence, followed by
explicit SysOp overrides, automatic quarantine, then ordinary probation or
establishment. Overrides are scoped, reasoned, timestamped, and audited. Node
sovereignty permits overriding even self-verifying evidence, but the UI must
keep the evidence and risk visible.

### 12.4 Probation and vouching

A remote node starts probationary. Default automatic graduation requires:

- 30 elapsed days since the first verified hello;
- verified direct interaction on three distinct UTC dates;
- no active local integrity or resource trigger;
- active vouches from two trusted reporters in two trust domains.

A remote user starts probationary independently. Default graduation requires:

- 14 elapsed days since first accepting that stable user identity;
- accepted activity on three distinct UTC dates;
- no active trigger in any applicable dimension;
- one authorized user vouch, or explicit SysOp establishment.

A node's day of direct interaction is a hello completed in either direction or
a push from it that this node accepted. A user's day of activity is an
accepted event they authored, whichever node delivered it. Content a carrier
brings is not an interaction with the node that originated it. Each counts once
per UTC date, and only where trust policy is enforced (issue #1035; before it,
nothing recorded these days and no subject graduated automatically). A subject
quarantined or blocked in any dimension banks no days, and only days after its
most recent quarantine or block count toward graduation: recovery returns to
probation, and probation starts its count again.

A home node's identity vouch binds an opaque user ID to that node; it is not a
behavioral vouch. Probation does not follow a changed home node or signing
identity without a future signed identity-transition protocol.

By default, probation is read-only from the subject's perspective. A
probationary node may complete hello/key-lifecycle exchanges and make bounded
inventory pulls at one quarter of the established-peer request budget. This
node may accept through it content independently signed by an established
author, but refuses or holds for explicit local approval new content authored
or node-vouched by the probationary identity. A probationary node contributes
no trust-signal weight and is not selected to serve as a relay. A probationary
user's posts/uploads enter applicable local approval flow. Private operators may
establish a known node manually instead of waiting for automatic graduation.

Probation is shown to the SysOp in both directions (issue #844), because a
new node otherwise cannot tell "working, just new" from "broken". Held-back
content is counted and named per node from the running node's set-aside list.
Whether a peer holds this node on probation is learned from the policy 403 on
this node's own push, and whether it holds a linked resource from that push or
from an inventory exchange that declared the resource and did not ask for it.
Both are kept in memory and known only for peers this node dials; a peer that
only dials this node reads as unknown rather than guessed.

**Node trust covers Link mail (issue #804).** A `link_message` is private mail
to one recipient, not publication, so user probation does not gate it: a
message from a user whose home node is established here is delivered even
while that user is still probationary. The sender's home node must be
established; a message from a node still on probation, or from a quarantined
or blocked user or node, is refused, and never silently. A direct push is
refused with the policy 403 and its reason code, which the sending node
records as a bounce rather than retrying, once none of the recipient's other
addresses or relays took the message (the 403 is unsigned, and a stale address
now answered by another node refuses the same way). Mail picked up from a
relay mailbox has no synchronous answer, so the refusal becomes a signed
`link_message_bounced` with reason `blocked_sender`, sent back even to a node
on probation here since it carries no content. A node quarantined or blocked
here gets no bounce by that route, because this node sends it nothing and the
bounce could only pile up; it learns of a direct push's refusal from the 403.
The decision is made before the message or its sender is kept, so a refused
node cannot grow this node's trust subjects or retained events by inventing
senders. Delivered mail registers its
sender as a trust subject like any accepted event, so the receiving SysOp can
find and establish them; a node refused as a whole is already a subject from
its hello, and establishing that node is what opens its users' mail. A
recipient's own control over who may write to them is a per-user block list
(issue #817), not probation.

**The receiving SysOp sees what was refused (issue #820).** A bounce tells the
sender; until #820 nothing told the SysOp whose policy refused the letter. Every
letter addressed to this node that it refuses -- by policy on a direct push or a
relay pickup, or by a delivery bounce (no such account, a full mailbox, a
malformed or undecryptable letter) -- is kept in `link_mail_refusals`: the
sender's home node and user name, the reason code, how it arrived, when it was
first and last refused, and how many times. One row per letter (its
`content_id`), so a sender retrying by another route counts up rather than
adding rows; bounded to the 500 most recent, and 50 from any one node,
because the sender decides how many letters arrive. A direct push is refused
before `handle_events` has verified anything, and the node it names is only its
URL, so a pushed letter is recorded only once its own signature verifies
against the keys of that node, a peer this node has met
(`LinkNode.is_signed_letter_from`); otherwise anyone could put a node on the
refused list, with Establish beside it. A push that reached this node for
another node (a stale address) is not kept. The record never holds the recipient, subject, body or ciphertext: the
console's mail tools (§6.4) show senders and reasons only. The console's
refused-letter screen leads to the node's and the sender's trust screen, where
establishing or blocking happens; a refused sender whose node was refused as a
whole was never registered (above), so its node is what the SysOp acts on.

The sending node applies its own policy before anything is queued: a caller
addressing a peer this node still holds on probation is told at the To prompt
that "<node> is not linked yet; mail opens once the SysOp establishes it", and a
quarantined or blocked peer that mail to it is closed. Mail queued before a
peer lost standing waits in the outbox, expires when its work item
dead-letters, and is woken on the next sync pass once the policy allows the
peer again (§13.7), rather than after the rest of a back-off of up to six
hours.

This relaxes the earlier default, under which a probationary user's Link
messages were refused. In practice that swallowed every message between newly
linked nodes: the refusal happened before the sender was registered, so the
receiving SysOp had nobody to establish, and no bounce reached the sender.

Configuration may make these defaults stricter. Relaxing them is an explicit,
audited SysOp safety deviation. Vouches are signed, scoped, expire after at
most 180 days, and may be renewed or revoked. Revocation removes current
support but neither erases history nor accuses the subject of abuse.

A node issues a vouch only because its SysOp chose to (issue #589). The SysOp
records an intent to vouch for an identity the node has met, with a reason
that is published inside the signed object; a reconcile then keeps the node's
signed vouches in line with its standing intents. It runs on every sync pass,
and at once from the SysOp console when that console is running inside a Link
node; the offline admin console has no node identity, so there an intent waits
for Link. The reconcile signs a vouch for an intent that has none, renews one
with 30 of its 90 issued days left or signed by a key the node has since
rotated away from, and signs a revocation when the intent is withdrawn, its
reason replaced, or the identity becomes quarantined or blocked on the issuing
node: a node does not tell the network it stands behind an identity it refuses
to deal with. The intent survives such a restriction, so lifting it restores
the vouch. A renewal overlaps the vouch it replaces rather than revoking it,
which adds no weight because receivers count domains, never vouches.

An issued vouch has no effect on the issuing node's own policy. Local counting
admits a vouch only from a configured reporter, and a node is not its own
reporter; establishing an identity locally is an override. A node does not
vouch for itself or for its own users: the home node is the one party that
cannot be independent of them, and what such a statement should mean is not
decided.

**A renamed user** (issue #1111). When a peer learns from a node's descriptor
that the node renamed `old` to `new` (§4.4), `new@node` takes the old
subject's state: overrides, quarantine and block states, signals, vouches,
local observations, activity days and probation age. A rename neither launders
a block nor resets probation. Whatever is recorded or asked about the old name
afterwards is the new name's. A peer accepts a rename only when it grants the
home node nothing it does not already have -- a home node speaks for its users
-- and refuses two kinds, each logged for the SysOp and never shown to callers:

- **A merge.** An entry whose new name this node already holds as a separate
  subject with history from *before* the entry's `at` would merge two users.
  History from `at` onwards is the renamed user's own (the same home node signs
  it, and it can arrive before the notice does), so an honest rename noticed
  late is never refused for it.
- **A rename back** to a name the same node renamed away from earlier: names
  are retired at home, so an honest node never does this. Together with the
  first rule this keeps chains acyclic.

A node could backdate `at` to slip past the merge test, so when both names
already carry state the stricter of each kind stays: of two active overrides
on a dimension the stricter is kept and the other cleared, a quarantine or
block on either side stays, and a quarantine in its recovery hold keeps its
hold. A rename can never lift a restriction. Attestations a peer received
about the old name are not moved; the issuer revokes them and issues them
again under the new name.

### 12.5 Evidence classes and attribution

“Objective” has two classes because not every receiver measurement is
independently provable:

1. **Self-verifying protocol evidence** includes the signed objects needed to
   reproduce the violation, such as conflicting valid extensions of one head.
2. **Observer-attested protocol/resource evidence** records measurements a
   third party cannot reconstruct, such as flooding, malformed requests,
   timeouts, non-delivery, or receipt of an invalid signature.
3. **Subjective content reports** express moderation judgments. Signed content
   may be referenced, but the judgment is still opinion.

Protocol-integrity categories are `signed_equivocation`, `revoked_key_use`,
`invalid_authority`, and `invalid_signature_delivery`. Resource categories are
`malformed_traffic`, `request_flood`, `quota_evasion`, `inventory_nondelivery`,
and `relay_abuse`. Content categories are `spam`, `harassment`,
`illegal_content`, `off_topic`, and `other`. The object version may add
categories later; an unknown category may be retained for diagnostics but has
no automatic policy effect until local software/configuration understands it.

An invalid signature does not prove that its claimed signer created or sent
it. Locally it may justify action against the direct delivery peer; remotely it
remains that observer's claim about the delivery peer. Unsigned malformed
traffic is attributable only to the connection identity available at receipt.

Subjective reports affect only content conduct and can never automatically
trigger transport quarantine. Resource reports affect resource policy;
self-verifying integrity evidence affects integrity policy. UI code must not
collapse these dimensions.

### 12.6 Signed trust signals

A trust signal is an immutable signed object containing protocol/object
version, issuer, stable signal ID, canonical node/user subject, dimension,
category, evidence class, embedded evidence or digest plus locator, observation
and issuance times, expiry, optional explanation, and—when revoking—the exact
signal content ID.

On Link v1 the durable object types are `trust_signal`, `trust_revocation`,
`trust_vouch`, and `trust_vouch_revocation`. They use the ordinary canonical
Link envelope (`netbbs_protocol`, `object_type`, `payload`) plus a detached
base64 signature, but they are stored separately from `link_events` and never
enter content-event flood gossip. `issuer_fingerprint` is the stable node
fingerprint; the detached signature is made by that node's currently
authorized operational signing key. Node subjects contain `kind` and
`node_fingerprint`; user subjects additionally contain `opaque_user_id`.

Evidence has one of two exact forms. Embedded evidence is `{mode: embedded,
data: ...}`. Referenced evidence is `{mode: digest, sha256, size, locator}`.
The signed size may not exceed the evidence limit. A revocation names one exact
`revoked_content_id`, must have the same issuer as its target, and uses the
signal- or vouch-specific revocation type so an ambiguous target lookup cannot
change object-family semantics.

Revocation is a new signed object and never deletes the original. Receivers
reject invalid category/evidence combinations, expiry before issuance, and
issue times over five minutes in the future. Receipt time is retained
independently. Content IDs provide replay deduplication.

Receivers clamp active lifetimes:

- self-verifying protocol evidence: 90 days;
- observer-attested protocol/resource evidence: 7 days;
- subjective content reports: 30 days;
- vouches: 180 days.

Renewal requires a fresh signal. Expired/revoked signals leave automatic policy
but remain under bounded audit retention. Digest-only evidence (`mode: digest`)
is stored and served like any object and never counts: a node does not fetch
from a locator, so nothing could reproduce it (issue #1036). Failure to fetch is
not evidence against the subject; an issuer that wants its signal to count
embeds the proof.

A self-verifying signal counts only when this receiver reproduces its
evidence itself (issue #1036). On Link v1 the one integrity violation a
receiver can prove to itself is `signed_equivocation`: embedded evidence
`{"kind": "signed_equivocation", "objects": [a, b]}` holding two different
signed Link objects in the same slot of one append-only chain -- the same
`previous_transition_id` of one subject's key-transition chain (signed by its
root key), the same `(root_post_id, previous_event_id)` of a post's content
chain (edit, moderator edit, tombstone), or the same `(board_id,
previous_event_id)` of a board's lifecycle chain (origin transfer, closure) --
both verifying under keys this receiver itself attributes to the subject node:
its root key, or an operational signing key its chain has not called
compromised. A compromised key's signature proves nothing about who made it.
The other integrity categories (`revoked_key_use`, `invalid_authority`,
`invalid_signature_delivery`) assert what a third party cannot reconstruct, so
from a remote issuer they never verify. A signal that does not reproduce is
kept, listed as unverified, and counts toward no quarantine threshold; like
any claim, it still holds back graduation. One about a node this receiver does
not know yet is re-checked, a bounded number per sync pass, once it does.

Verified evidence counts toward the two-domain threshold; it does not become
the receiver's own local observation by itself. A proof is a proof whoever
delivers it, but independent domains still have to report it before a remote
report quarantines, so one reporter -- and, since #589, one node's automatic
issuance -- cannot quarantine a subject at every subscriber on its own. An
honestly forked node (restored from an old backup, a cloned VM) is still
quarantined once two domains have seen it, and its SysOp's remedy is the
recovery rule of §12.9. Inactive signals and evidence are retained for 365
days by default, unless an active decision or explicit legal/diagnostic hold
still references them. Later pruning preserves the content digest and decision
audit so historical enforcement remains explainable without unbounded blobs.

### 12.7 Propagation, independence, and Sybil resistance

Trust signals are not flood-gossiped with content events. A node explicitly
subscribes to selected reporters and pulls their issuer-signed signals. A
carrier may serve an unchanged signal, but the receiver verifies the issuer
and ignores unconfigured issuers.

The pull is an authenticated Link request rather than durable content. It
binds requester, responder, requested issuer, optional last-content-ID cursor,
page limit, creation time, a 128-bit nonce, and a `revocations_only` containment
flag under the requester's current
operational signing key. The responder requires a completed hello, matching
requester/responder identities, a valid signature, a five-minute freshness
window, and a bounded nonce replay cache. Responses contain only unchanged
stored objects for the requested issuer, in the order the responder stored
them, plus `more_available`. Storage order rather than receipt time, because
a revocation is always stored after the object it retires whatever the wall
clock said, and a subscriber that met them the other way round would skip the
revocation and then admit what it revoked. A node's own signed objects are
stored with the ones it has admitted, under its own fingerprint, so the same
pull serves them. A carrier gains no authority: the request
is addressed to the carrier, while every returned object's independent issuer
signature and local reporter configuration still control admission.
When a configured reporter is quarantined, ordinary subscription sync stops;
the receiver may use only `revocations_only=true`, which serves unchanged
signal/vouch revocation objects and never advances the ordinary subscription
cursor. A manually blocked reporter receives no containment exchange.

**An issuer nobody can dial (issue #627).** A pull needs an address, and an
outgoing-only node (§8.4) advertises none, which is the ordinary case for a
node behind a home connection. Such a node deposits what it signs at the
nodes that relay for it (§8.5): `POST /link/v1/trust-deposit/{fingerprint}`,
carrying the node's own objects in the order it signed them and the signed,
fresh, replay-bounded authorization the file route also uses. A relay accepts
a deposit only from a node it has agreed to relay for, which is the existing
opt-in and the existing cap on whom it holds things for; only objects the
depositor issued itself; and only those that verify under the depositor's
current key, counting the rest and leaving them out, since an issuer's store
keeps what its earlier keys signed and re-signs what still matters. Only the
issuer may deposit, although the objects would verify whoever sent them: the
order they are stored in is the order subscribers read them in, and a third
party replaying a withdrawn vouch ahead of its revocation would revive it.
The depositor keeps a position per relay, sends only what is new, and starts
over at a relay it selects afresh. Each deposit names the last object handed
over before it, kept by the relay or not, and with nothing new to send the
depositor still makes one such request a pass. A relay that does not
remember that object has lost
something, to a restored backup above all, and could otherwise be left
serving a vouch without the revocation that followed it; it answers 409 and
stores nothing, and the depositor starts over there, where what the relay
still holds keeps its place in the order. A relay that refuses for any other
reason (it does not relay for the depositor, lacks the route, or is full) is
logged, left alone for an hour, and named on the vouch screen of the
depositing node as not having taken its vouches at the last attempt.

What is deposited is *carried*, and kept apart from what the relay has
admitted. Admission is application: an object already in the admitted store
counts as replayed and is never applied, and a vouch the relay merely carries
must not look like one it counts. A relay whose own SysOp names the depositor
a reporter cannot pull from it any more than anyone else can, so in its own
sync pass, for as long as that node's own descriptor names it as a relay, it
reads what it carries exactly as a subscriber reads a carrier:
under a cursor, object by object, with the same skips and the same stall.
That works whether the depositor was named before the deposit or after it,
follows a widened grant, and leaves a rejected batch to be retried. Nothing is
admitted when the deposit arrives. Carriage is bounded per depositor at 4,000
objects and 32 MiB, refused beyond that; an object already expired is not
taken, and one that expires is dropped the next time anything is deposited.
An issuer's stream is served from exactly one store, the carried one while
it holds anything of that issuer's, because a cursor is a position in one; a
cursor from the other reads as unknown and the subscriber starts over.

A subscriber whose reporter cannot be dialed reads the relays that reporter
publishes in its own descriptor, and pulls the carrier form of the request
from one it has itself completed a hello with and can dial. It verifies what
comes back against the reporter's identity however it learned it, by hello or
by introduction (§8.11); a relay answers an identity request for a node it
relays for, and a page that stops at a key the subscriber has not learned
makes it ask that relay for a fresher bundle at once. A reporter this node has
not met, and has not blocked, is asked about at the relays it publishes that
this node has met, or failing those at up to three dialable peers, and for
a reporter already known by introduction no more than once an hour: one named
by fingerprint
alone so that it becomes a trust subject the SysOp can establish at all, and
one known by introduction so that its descriptor is refreshed, since it may
have published no relay when it was learned or have moved to another since.
That happens before probation ends the matter. None of this relaxes
what §12.4 requires of a reporter: it has to be established here before it
is pulled, which for a node never met means by override.

Remote attestations (§5.5) are not carried this way. They hold a caller's
birthdate or real name, and a carrier would either read them or enforce the
recipient list on the issuer's behalf. They travel instead as a snapshot sealed
to each recipient, through the recipient's relays (issue #632).

Distinct fingerprints do not prove independence. Automatic policy counts
locally assigned trust domains:

- reporters in one domain contribute at most that domain's weight;
- default and normal maximum domain weight is `1.0`;
- remote-signal quarantine requires two domains and total weight `>= 2.0`;
- vouch/reputation paths never multiply weight;
- reporter, domain, weight, and category changes are audited SysOp actions.

An operator may configure a jurisdictional/emergency key as sole authority for
named categories. This explicit local exception is displayed as such; weight
alone never bypasses the two-domain rule. A sole authority speaks alone but
does not skip proof: its self-verifying signal counts only once the evidence
reproduces here, like any reporter's (issue #1036).

Default trust-ingress bounds are 100 signals or 1 MiB per response, 1,000
active signals per issuer, 10 active signals per issuer/subject/category, 256
KiB embedded evidence per signal, and a separate ingestion budget in addition
to the ordinary request throttle. Over-limit input is rejected or deferred
visibly, never converted into evidence.

A subscriber distinguishes an object it cannot use from a response it must
not trust, and both from an object it cannot use *yet*. A rejection that time
or state can undo, such as an issue time in the future or a full quota,
rejects the batch, which is retried from the same cursor. An object outside
what the subscriber configured its reporter for, a revocation naming an object
the subscriber does not hold or one already revoked, and an object signed by a
key the issuer has since replaced, which is what an issuer's stream holds
after it rotates, are each skipped while the rest is admitted and the cursor
moves on. Treating those as fatal would wedge the subscription: the batch is
abandoned, the cursor stays, and every later pass meets the same object first.
A skipped object is not stored, so changing a reporter's grant resets that
reporter's cursor and the next pass re-reads its stream, where everything
already held is a replay.

An object that verifies under no key the subscriber knows for its issuer is
different. The ordinary cause is that the subscriber is the stale party: the
issuer rotated and re-signed everything, and the subscriber has not completed
a hello with it since. Skipping would move the cursor past every re-issued
vouch and every revocation. The subscriber therefore tells the two apart by
the issuer's own transition chain. A superseded key's signature is skipped for
good; an unknown key's stops the page there, with the cursor left on the last
settled object, and the next pass retries after the next hello.

A cursor the responder cannot resolve is its own case (issue #621). A
responder restored from an older backup, or recreated, will never again hold
the object a subscriber's cursor names. It answers HTTP 400 with `reason_code`
`unknown_pull_cursor`, and the subscriber forgets the cursor and re-reads that
issuer from the start, where everything it holds is a replay and the page
budget bounds the cost. This also
means a cursor saved from something a responder never stored cannot wedge a
subscription for longer than one pass. The subscriber's cursor in any case
moves only past objects it could authenticate: one signed by the issuer's
current key, or by a key the issuer has replaced. A served entry whose shape
does not allow a signature check rejects the page.

A reporter has to be established before any of this happens. Under the policy
a running node enforces, a probationary, quarantined or blocked reporter is not
pulled in the ordinary way, and a vouch from a reporter that is not established
in identity integrity and resource behavior does not count. Naming a reporter
is the grant of authority; establishing it is a separate act, by override or by
graduation.

### 12.8 Quarantine effects

Direct local self-verifying integrity evidence may quarantine immediately.
Remote signals must satisfy their configured category rule and the independence
threshold. Observer reports normally tighten limits or extend probation before
quarantine. Subjective reports may hide or moderate named user/content
projections but never quarantine transport automatically.

Node quarantine stops ordinary outbound sync; rejects ordinary events,
inventory, Link messages, files, relays, and peer introductions before new
remote state is persisted; removes the node from relay/candidate selection;
and suppresses applicable locally displayed content. Previously accepted
events, content, identity history, and audits are preserved, not deleted.

A narrow, separately rate-limited containment path may accept verified hello,
key transitions, signal revocations, and recovery metadata. It cannot carry
ordinary content or services. User quarantine affects that user, not unrelated
users, the whole home node, or content merely relayed by the node.

Manual block is harder: it denies even containment until removed. Existing
bytes still are not deleted. Rejections and suppression use stable reason codes
for protocol, diagnostics, and SysOp UI while keeping private reporters, notes,
and policy configuration undisclosed.

The Link HTTP enforcement point runs only after enough cryptographic parsing to
attribute a request, but before remotely influenced persistence or service work.
The normal runtime enables this gate for hello, events, inventory, trust pulls
and deposits, file chunks, relay consent/mailboxes, and peer introduction.
Peer-list exchange, file-chunk pulls and trust deposits use authenticated
POSTs from completed peers; Link v1
reuses an empty, signed inventory-request authorization envelope so requester,
responder, freshness, nonce replay protection, and current operational-key
verification have one existing definition rather than a second near-identical
request type. The URL fingerprint is routing information, never attribution.
Probationary inventory responses use one quarter of the established event
budget. Valid board posts from probationary users enter the local pending
approval queue, and so do their edits: an approved post must not be rewritten
with unreviewed text. Link mail (issue #804) and Linked chat lines (issue
#860) from a probationary user of a node allowed here are delivered: mail goes
to one recipient, and chat has no approval queue, so refusing a probationary
caller's line left every caller of a newly met node unheard in a Linked channel
until they graduated. Quarantine and block still refuse both. Other services
without an approval projection, such as file uploads, refuse such content with
a stable reason code.

A pushed events request is judged event by event once the sending node itself
is allowed (issue #897). An event refused for its author does not refuse the
rest: the receiver takes what it may and answers 200 with
`refused: [{content_id, reason_code}]` beside `accepted`. Only a request with
nothing acceptable left is refused outright with 403, and that body carries the
same `refused` list, so a single pushed letter keeps its refusal. A refusal
about the sending node remains a 403 for the whole request. The sender sets
each refused event aside for that peer for `DEFERRED_EVENT_RETRY_SECONDS` and
then offers it again while the peer's inventory still asks for it, so content
from an author on probation there arrives once that author may post, and
refused events never fill a request ahead of everything else. A partial
refusal is about authors, not the sending node, so it does not mark the peer
as refusing this node's content. Senders older than this rule ignore
`refused`, and a 200 means only that the rest arrived.

Enforcement attributes independently signed content to its author/home node,
not to a carrier recorded in `link_events.sender_fingerprint`. Current display
suppression is evaluated from retained signed authorship at read time; changing
or clearing local policy therefore hides or restores projections without
rewriting or deleting the accepted event bytes.

Suppression applies to every surface that shows or counts content, not only
the page that lists it (issue #677):
- A board page is filled from visible posts only. Its older/newer links count
  only visible posts, so hidden posts neither shorten a page nor produce an
  empty one.
- Post counts, `[N]ew scan` unread counts for boards and channels, and
  "replies to you" exclude hidden content.
- Local search excludes hidden posts and hidden channel messages.

Visibility is decided per event in Python rather than in SQL, but it depends
only on the event's author. A count therefore looks up each distinct author
once, not each post. Paging over a long run of hidden posts costs further
query batches rather than returning a short page.

A local tombstone stays terminal once the expiry sweep has aged it: the check
is for any tombstone revision in the chain, whatever its status.

### 12.9 Recovery, partitions, and explainability

Absence, failed dials, and partitions are never evidence. Partitions create no
reports and do not multiply old signals. Signal expiry continues on local time
so an accusation cannot become permanent through disconnection.

Automatic quarantine ends only after every trigger is cleared, expired, or
revoked and a 24-hour recovery hold passes without a fresh trigger. Recovery
returns to probationary, not established. Self-verifying equivocation or
confirmed key compromise also requires SysOp review or verified root-key
recovery; scoped resource/content restrictions may recover automatically. For
equivocation a node observed itself (issue #589), the observation expiring is
not that review: the subject stays quarantined, reason
`equivocation_review_required`, until a SysOp clears the evidence on its trust
screen, and then takes the ordinary hold.

Effective state is a persisted projection recomputed transactionally on input
changes, at startup, and on every Link sync pass. The last is for changes due to
time alone (a recovery hold's release, an override's or a signal's expiry,
probation's age requirement), which have no input change of their own; without
it a running node applied them only at its next restart (issue #802). For every restriction the SysOp can inspect the subject,
dimension, effects, rule/threshold, evidence, counted domains/weights, times,
overrides, audit history, and requirements for release. Counted domains and
weight appear whenever the dimension has a self-verifying identity report that
counts toward the two-domain threshold, whatever decided its state; a
dimension that cannot quarantine by that threshold never shows them. The
console states them as the distance to quarantine
("1 of 2 domains, weight 1.0 of 2.0"), so a SysOp sees how close one more
report would bring the subject (issue #752). Probation's
`active_trigger_count` counts every applicable dimension, because any trigger
blocks graduation in all of them; `dimension_trigger_count` beside it counts
only the dimension shown. Caller-facing behavior
states that local policy restricted content/delivery without claiming a
network-wide verdict or leaking private evidence.

### 12.10 Required validation

Phase-4 implementation must test Sybils in one domain, colluding domains below
and above threshold, compromised reporters, expiry/revocation, replay/stale/
future/oversized signals, reproducible and false evidence, invalid-signature
attribution, subjective-report isolation, partitions and recovery, restart
reconstruction, overrides, preservation without deletion, user/node scoping,
containment recovery, and real SQLite/transport resource bounds.

Public readiness additionally requires a SysOp trust/explanation surface,
manual block/quarantine/recovery workflows, and dogfood with independently
administered nodes. Unit tests alone are not a public-network claim.

### 12.11 Public-readiness checklist (issue #131)

This section is the honest statement §12.10 itself requires: what is actually
validated as of this writing, and what still is not. Update it in place as
coverage changes rather than appending a superseded status below it.

**§12.10's scenario list — validated:**

Sybils in one domain; colluding domains below and above threshold;
compromised-reporter removal (with preservation, not deletion, of the
underlying signal); expiry/revocation; replay (both at the signal level and
the request/nonce level); future-dated signals; oversized signals;
reproducible and false evidence; invalid-signature attribution; subjective
content reports never escalating to transport quarantine; restart
reconstruction; overrides (both application and clearing) as an audited,
reversible transition; preservation without deletion; user/node subject
scoping; containment recovery; and real SQLite/transport resource bounds
(per-subject/category quotas, bounded evidence fetch). All of the above are
covered by tests exercising the real policy/enforcement code paths, most of
them (Sybil weighting, replay/staleness, resource bounds) over a real
loopback transport, not only in-process function calls — see
`tests/test_link_trust.py`, `tests/test_link_trust_wire.py`, and
`tests/test_link_transport.py`.

**Known, accepted gaps in that list** (small, not believed to hide a real
policy hole, but not independently proven either):

- A trust signal that is already past its own declared expiry strictly *at
  the moment it arrives* has no dedicated test distinguishing it from
  ordinary expiry-driven exclusion — it is caught by the same general
  expiry filter every other expired signal is, but that filter's coverage
  of this exact timing has never been asserted directly.
- Trust state under a genuine network partition is validated only as two
  separate proofs, not one combined scenario: `tests/test_link_convergence.py`
  proves generic Link partition/restart convergence with no trust content
  involved, and `tests/test_link_trust.py`'s recovery-hold tests prove
  trust-state recovery timing on a single node with no partition involved.
  Nothing currently drives a real multi-node partition *of trust-affecting
  traffic specifically* through to convergence.

**Public-readiness items:**

- **SysOp trust/explanation surface** — built and reachable through the real
  Telnet menu: subjects, per-dimension state and explanation, override
  application, override clearing, and decision history are all exercised
  end to end through `admin_menu` in `tests/test_admin_flow.py`, not only
  at the `netbbs.link.trust` function level.
- **Manual block/quarantine/recovery workflows** — same real-menu coverage
  for both directions: applying a block through the UI and clearing one
  back to a recomputed state are each tested.
- **One real multi-node exercise covering configuration, quarantine,
  explanation, and recovery together** —
  `tests/test_link_transport.py::test_real_transport_enforces_probation_
  quarantine_block_explains_and_recovers` drives two real nodes over a real
  loopback HTTP connection through the full sequence: probation, an
  established override, escalation to quarantine, escalation to a manual
  block (with the previously-accepted bytes still present and the
  quarantined event still absent), reading the exact SysOp explanation
  surface for that blocked state, clearing both overrides, an explicit
  re-vouch, and a previously-refused push and hello both succeeding again
  over that same connection afterward.
- **Dogfood with independently administered nodes** — **not yet done.**
  Issue #83 tracks this and is deliberately independent in duration from
  this gate (its calendar length does not block the rest of #131), but it
  has not itself happened. This is the one item on this whole checklist
  that automated tests cannot substitute for.

**What this checklist does and does not claim:** every scenario above having
a real, passing test means the *implemented* Phase 4 model behaves
correctly against every adversarial case design doc §12 currently specifies,
including through real transport, storage, and restart. It does **not**
mean NetBBS Link is ready for public, stranger-to-stranger federation —
that additionally requires the independently-administered dogfood above,
and remains explicitly out of scope until it happens. Treat any future
claim of public readiness that does not point back to a completed dogfood
exercise as premature.
