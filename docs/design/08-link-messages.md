# NetBBS design — 10. Link messages

This is the normative design reference for this area of NetBBS. Section numbers are preserved from the design index so existing `§n.n` references remain stable.

---

## 10. Link messages

### 10.1 Product model

A Link message extends the ordinary local mailbox. The user composes to a
`user@node` address (§4.4) and reads the result in the same inbox/sent UI
as local mail. The To prompt checks a Link address as it is typed, the way it
checks a local name, and asks again in place with what to type instead: a
malformed address, a node this BBS is not linked with, a name more than one
linked node goes by, and a peer this node will not send mail to (§12.4) are
all refused there, never after the message is written. Send repeats the
checks, because the review screen's `[T]o` can change the address. Once
resolved, the address is kept by the node's technical identity, not the name
typed (issue #826).

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
- bounced because of unknown recipient, full mailbox, blocking, a letter the
  recipient node cannot decrypt, a malformed letter, a recipient that takes no
  mail (`no_mailbox`: the node's shared guest account, issue #816, §6.4), an
  account that takes none at the moment (`recipient_unavailable`: disabled or
  awaiting approval, not saying which, issue #818, §6.4), or another defined
  terminal failure. Blocking has two codes:
  `blocked_sender` is the recipient *node's* trust policy refusing the
  sender or its node (§12.4), and `blocked_by_recipient` is the recipient
  *person* having blocked this sender (issue #817, §6.4). The sender is told
  which: "that BBS does not accept mail from you or from this BBS" against
  "the recipient does not accept mail from you";
- future expiry where retry policy requires it.

Outbound messages remain pending until an accepted or bounced event arrives.
Delivery through a relay does not change the acceptance semantics. One answer
is not a signed event: a recipient node whose trust policy refuses a direct
push says so with HTTP 403 and a `link_policy_*` reason code, and the sending
node records that as a bounce, since asking again would get the same answer,
unless another of the recipient's addresses or relays takes the message
(§12.4, issue #804).

The sending node shows the state to the sending user (issue #806). Sent marks
each Link message pending, with relay (§10.3, below), delivered, bounced or
expired, and the message's
Delivery line gives a bounce's reason in plain words. The reason is the signed
bounce's `reason` or the refusal's `link_policy_*` code, stored with the
message (at most 64 characters, since another node chose it); a code this
node does not know reads as "that BBS refused it". Expired means the delivery
work item dead-lettered: no route took the message, or this node's own trust
policy held it back to the end, which is recorded as its own reason
(`own_policy`), or that a letter left at a relay got no answer in time
(`no_answer`, below). A bounce
or expiry flags the message until its sender is told: once, at their next
main menu, which covers a sender who was offline when it happened, or by
opening it in Sent. Mail the sender already deleted from Sent is not told
about. A later acceptance clears the flag and wins; a later bounce wins too,
and flags the message again unless it was already bounced. A bounce
message in the inbox would need a system sender (issue #819) and is not sent.

The recipient node checks everything the sending node chose where it enters,
in `deliver_link_message` (issue #808), and answers each failure with a
signed bounce rather than an exception that would lose the letter in silence:

- the sender's `local_user_id` must match the address grammar
  (`[A-Za-z0-9_.-]{1,32}`, §4.4), because it becomes the address a reply goes
  to. A NetBBS node only sends names its username rules allow; the one honest
  exception is an account older than those rules on a node older than #807,
  and a reply to it could not be addressed either. Anything else bounces
  `malformed`;
- the decrypted letter must be a JSON object whose subject and body are text
  within the limits local mail keeps (a subject that is not blank, 200 bytes;
  a body of up to 20,000 bytes; both encodable as UTF-8). Anything else
  bounces `malformed`;
- a ciphertext sealed to none of this node's current or retired signing keys,
  or not base64 at all, bounces `undecryptable`. It used to bounce
  `unknown_recipient`, which sent the sender looking for a typo.

`undecryptable`, `malformed`, `no_mailbox`, `blocked_by_recipient` and
`recipient_unavailable` are bounce reasons added after v7.13.0. Every
earlier release keeps a received bounce's reason without checking it (v7.13
and older ignore it entirely and just mark the message bounced), so a new code
reaches an older sender as a plain bounce and costs it nothing but the
wording.

A received letter is dated by its signed `created_at`, when its sender wrote
it, so a letter that took days to arrive says so. A `created_at` more than
five minutes ahead of the recipient node's clock (the skew Link's signed
requests allow), one before 2000 (no NetBBS node wrote mail then, and a date
near year 1 cannot be shown in a timezone west of UTC), or one that is not a
timestamp, is replaced by the arrival time: the sender's clock cannot put a
letter in the future or out of range. Inbox and Sent
list mail by arrival (row id), not by that date, so late mail lands at the top
of the inbox instead of below letters read long ago; making room in a full
mailbox likewise removes the earliest-arrived read letter.

A letter left at a relay is not delivered on the relay's word (issue #874).
The relay is not the recipient, and some answers never come back: a recipient
node that holds the sending node quarantined or blocked refuses such mail at
pickup without a bounce (§12.4), and an acknowledgement can be lost. So the
sending node records when it handed the letter over
(`mail_messages.link_relay_handoff_at`); the letter stays `pending`, and Sent
shows it as "with relay" ("With a relay, no answer yet") rather than plain
pending. Each sync pass expires a letter still pending 14 days after its
handoff, with reason `no_answer`, and its sender is told as for any expiry,
in words that say it may have arrived all the same: no answer came back. An
answer that arrives later still wins, as above. The timeout is the sending
node's alone: nothing changes on the wire, and it works the same with old
relays and old recipients. Mail pushed directly to the recipient never times
out this way, since the push itself reached the recipient's node; mail left at
a relay before the upgrade that added the handoff time cannot be told apart
from it and keeps waiting.

A relay must hold a deposit longer than this timeout. Any limit on how long a
relay mailbox keeps an uncollected deposit (30 days since issue #891) must stay
well above 14 days, so that a recipient that is merely slow to collect still
answers inside the sender's window; a limit below the timeout would let a
letter vanish while its sender still reads "no answer yet".

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

### 10.6 Tier-2 recipient scope (issue #90) — deferred, not permanently

**Not to be confused with `tier2_personal_key` (§10.2).** That is a
different, already-decided, permanently-out-of-scope concept: client-side
end-to-end encryption against the home node itself, blocked by a hard
architectural constraint (no client-side decryption exists). This section
is only about *recipient reachability* — delivering `tier1_home_node_key`
messages to a peer the sender has never directly, verifiably completed a
hello with — which has no equivalent hard blocker, just isn't built yet.

**What §10.4's "no total strangers" boundary actually rests on.** A
`HelloMessage` bundle is *self-authenticating* by construction (§12): a
peer that didn't hold the claimed root key's private half could not have
produced a transitions chain that both verifies against that root and
whose resolved current signing key matches the descriptor's own
signature. Verifying someone's identity has never required trusting
*who* handed you the bundle — only the bundle's own internal
cryptographic consistency. Today's requirement that this always happens
via a completed two-way hello is a stronger condition than the
verification itself actually needs.

**Neither issue #85 nor issue #630 provides tier-2 reachability.** Relay
(#85) carries content between nodes; introduction (#630, §8.11) lets a
receiving node verify content signed by a node it has never met, by
fetching that node's self-certifying bundle from the carrier. Neither
reaches mail. An introduced identity is kept apart from peers precisely so
that it cannot be addressed, pushed to or pulled from, and a carrier
answers only for nodes whose content it has just served or that it relays
for (§8.11), which a mail recipient need not be.

**What a real tier-2 design would require, concretely.** The bundle
exchange exists (§8.11) and verifies a bundle exactly as if its subject had
dialed in directly, whoever served it: a tampered or fabricated bundle
simply fails verification. Still missing for mail: a way to ask for the
identity of a *recipient*, who has signed nothing the asker was served; the
same in reverse for the recipient verifying the sender; a delivery path,
since a node known only by introduction was never dialed and may not be
dialable; and the decision to let an identity learned that way be
encrypted to, with no other change to §10.2's confidentiality model.

**Confidentiality and abuse implications, if built.** No new
confidentiality exposure to message *content* — encryption still targets
the real recipient's real key, independently verified, regardless of who
relayed the bundle that made verification possible. The exposure is
metadata: a relay learns that someone is asking about a specific
fingerprint, an availability/traffic-analysis concern rather than a
confidentiality break of any message body. A dishonest relay can only
withhold or refuse to relay a bundle (availability), never forge one
(self-certification), matching the same "worth trying, never blindly
trusted" property peer-list exchange already established for addresses.

**Decision: deferred, not scoped as active work.** Unlike
`tier2_personal_key`, this is not a permanent non-goal — there is no
architectural blocker, only that it is not needed to unblock or validate
current work. Revisit if a real deployment need appears (e.g. issue #83's
dogfood run surfaces callers who actually want to message someone they've
never directly synced with) rather than building it speculatively now.

---
