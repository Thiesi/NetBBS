# NetBBS design — §4. Accounts, authentication, identity, and addressing

Part of the [NetBBS design](../NetBBS-design-doc.md). Section numbers are stable: cite this area's rules by their section numbers (§4.n).

## 4. Accounts, authentication, identity, and addressing

### 4.1 Account authentication

Local users may authenticate with:

- username and password, the default path;
- an optional personal Ed25519 keypair for passwordless challenge-response
  login.

The server never needs a personal user private key. Password-only users are
expected to remain the majority and must not be treated as second-class users.

A password has a lifecycle after creation (issue #611). An account changes
its own password from the Profile screen after proving the current one; an
account with no password (key-only) sets its first one without that proof,
on the strength of the login that reached the screen. A SysOp sets a new
password on any account from that account's detail screen, or from
`python -m netbbs.admin reset-password USERNAME` when locked out of the
console; neither route asks for or reveals the old password. A password may
be cleared only while the account keeps at least one public key, the same
"never leave an account with no way in" rule key removal already applies.
Every change is audit-logged with the actor and carries no other detail. A
guest session (§4.6) cannot change the guest account's password, because it
proved no credential. The current-password proof inside a session charges the
same login throttle as the login prompt, so an unattended session is not an
unthrottled place to guess.

An account's numeric id is never given to another account on the same node
(issue #1131). SQLite reuses the highest free rowid, so before this a deleted
newest account's id went to the next registrant, and a door or BBSLink game
keyed on `user_id` handed the deleted player's saves to a stranger. The node
keeps the highest id ever used and gives each new account the next one above
it. Two limits remain. Restoring a backup takes the high-water mark back with
it, so an id issued after the backup was taken can be issued again. And the
mark starts, on upgrade, at the highest id still in use or named by a deletion
in the moderation log; an id freed with no such record (before the log named
ids, or by a hand edit) can still go to one more account. Code that must
survive either pairs the id with the account's `created_at` (the guest
designation, §4.6).

### 4.2 Registration modes

A node has one registration mode:

- `open`: self-registration creates an immediately usable account;
- `approval_required`: self-registration creates a pending account which
  cannot authenticate until approved;
- `closed`: the public registration option is absent and accounts are
  SysOp-created.

A pending account that presents the right password is told it is waiting
for approval and the connection ends (issue #835). Any other failure stays the
generic "Login failed": the distinction is made only after the credential
has matched, so it tells no one anything they could not learn by logging in.
SSH shows the same notice as an authentication banner on a password login.
It stays generic for a public-key lookup, because SSH asks that before the
client has signed anything and a public key is public. A signup that created a
pending account is not charged as a failed login attempt on its connection.

Self-registration checks the desired username as soon as it is typed, before
the password prompts, and spends a login-throttle token doing so: whether a
name is taken is the same existence answer account creation used to give,
only earlier. Beyond the grammar every account shares, a caller may not
register:

- a reserved name: `sysop`, `cosysop`, `admin`, `administrator`, `root`,
  `moderator`, `mod`, `staff`, `support`, `system`, `operator`, `postmaster`,
  `guest`, `netbbs`;
- a name containing `sysop`;
- a look-alike of a level-255 account's name.

These names are compared by a skeleton: case folded, `_ - .` dropped, and
`0/o`, `1/l/i`, `3/e`, `4/a`, `5/s`, `7/t`, `8/b`, `9/g`, `2/z`, `rn/m` and
`vv/w` folded together. The rules apply to self-registration only. A SysOp
creating an account by hand may use any name the grammar allows. Only SysOp
names are protected, not every account's, because impersonating the operator
is the harm the persona test found. Blocking look-alikes of every caller would
refuse ordinary names for no gain. A signup is turned down with Decline on the
pending account, which deletes it after a yes/no. Deletion's typed-name ritual
guards content and Link history that a never-approved account cannot have.

On an approval-required node the SysOp may set one signup question, up to 200
characters. Self-registration asks it after the password. The answer is
optional, cut to 300 characters, and stored with the question as asked. It is
shown on the pending account's detail screen and deleted when the account is
approved. It was given for that one decision, and keeping it would build a
profile nobody agreed to. Declining removes it with the account. An open node
never asks it, because nobody reads the answer before the account is usable.

Registration determines whether an account may exist and log in. Link
probation and reputation determine what an active identity may do; these are
separate axes.

### 4.3 Account levels and the usable-SysOp invariant

One integer level drives ordinary level gating. `SYSOP_LEVEL = 255` is the
reserved top level; SysOp is not a parallel role flag. Levels below 255 grant
no authority by themselves. Authority short of SysOp comes from staff
permissions (§5.6) and moderator grants (§5.2), never from a level band.

Promote, demote, disable, enable, approve, and hard-delete operations must never
leave the node with zero **usable SysOps**. A usable SysOp:

- has level at least `SYSOP_LEVEL`;
- is not disabled;
- is not pending approval.

The invariant is enforced transactionally against fresh database state, not
against a stale object supplied by a caller.

A change to an account's level, its verify-identity permission or its staff
permissions (§5.6) applies to that account's live sessions without a
re-login (issue #659), whichever process made it. Each session's account watcher re-reads the account every
few seconds, and an in-node change wakes it at once. A gain is picked up the
next time the main menu is drawn, straight away if the caller is sitting on
the menu, and nothing is interrupted. A loss interrupts the caller's current
screen and returns them to the main menu, redrawn for the new level with a
line saying what changed. Every screen was entered under the old access, the
SysOp console above all, and interrupting is the only way to reach a screen
that is waiting for a key. The interruption ends whatever the caller was
doing, a running door included. An editor keeps its text as a recoverable
draft. The SysOp console also re-checks its operator at its own menu, which
is what stops a demoted operator in the standalone CLI. Moderator grants
(§5.2) need no watcher: they are read from the database at each check, so a
grant or a revocation governs the holder's next action in every session.

**Automatic promotion** (issue #992). A SysOp can set promotion rules
(`Users ▸ Promotion rules`). Each rule raises an account from one level to a
higher one once the account is old enough, has logged in often enough and,
optionally, has posted enough.

- **When.** Rules are checked at login, after the login is counted and before
  the session takes its level. The session starts at the new level and the
  caller is told once, on the first main menu. An account that qualifies
  while away is promoted at its next login, the first time the level
  matters. Nothing runs on a timer.
- **Shape.** One rule per starting level, so which rule applies is never a
  guess. A rule never demotes and never reaches 255. One rule applies per
  login, so an account climbs a ladder of rules one step per call.
- **Who is left alone.** The guest account (whether guest login is on or
  not), pending and disabled accounts, staff and SysOps, and every account
  whose level a person has set. A level set by hand takes the account out of
  the rules, so a demotion is not undone at the next login; a starting level
  other than 0 counts as set by hand. The account screen shows whether the
  rules apply and turns them back on.
- **Counting.** Logins are counted on the account itself, since session
  history keeps only a few rows per account. Posts are the account's
  approved posts on this node, each counted once however often edited.
- **Audit.** An automatic promotion is recorded in the moderation log with
  no acting account, shown as "(system)", and names the rule.

Rules are node configuration and travel in backups.

Hard deletion preserves content provenance through denormalized display labels
or nullable author/uploader references. Personal access rows and private state
which cannot meaningfully outlive the account are deleted according to explicit
foreign-key policy.

On a node that has ever run NetBBS Link, hard deletion also retires the
username (issue #594). The opaque local user identifier of §4.5 is the
username, so on the Link an account is its name: mail is addressed to it, a
carried post's author label is built from it, the trust subject is derived
from it, and a remote attestation names it. A freed name would hand all of
that to the next registrant. The name is recorded in the deleting transaction
and refused at registration, case-insensitively, whatever the Link setting
does afterwards; "ever" is a sticky marker set when the node first starts with
Link effectively on, and seeded at upgrade, on a node that ran Link before the
marker existed, from any artifact Link leaves behind. A node that has never run Link records nothing
and its names stay reusable. Nor is the name retired of a registration that
was still awaiting approval and was never sent Link mail: such an account has
provably never had a session, so there is no Link identity to inherit. A SysOp can release a retired name, as a
confirmed and audited action. Self-service registration refuses a retired
name in the words it uses for a taken one; a SysOp surface says why and where
to release it.

**Renaming an account** (issue #1111). A caller asks for a new username from
their Profile; the request is checked exactly as self-registration checks a
name and waits beside pending registrations, where the SysOp approves it or
refuses it with a reason the caller sees at their next login. A SysOp can also
rename an account directly from its detail screen. Approving, refusing and
renaming are a SysOp's alone, as deletion is. One open request per account,
one approved rename per account per 30 days (a SysOp's direct rename is not
limited, and counts as the latest), and each step is audited
(`request_username_change`, `refuse_username_change`, `rename_user`).

- **One transaction.** The username changes and the account id, with
  everything keyed on it, stays. Under the retirement rule above the old name
  is retired in the same transaction, and the retired row records the account
  it now belongs to; Link mail, FTN netmail and `/msg` to the old name reach
  that account. A name retired by deletion records no account and bounces. A
  later deletion of the renamed account clears the account from its old names,
  which stay held.
- **History keeps its name.** Posts, files and sent mail keep the author name
  they were stored and signed under; this node's copy never disagrees with a
  peer's. What looks an account up live follows it. Chat scrollback records
  the writing account's id on every line, so a line resolves to its author
  after a rename and never to a later holder of the name; a line from before
  that id existed resolves to its author only if the account holding the name
  existed when the line was written, and otherwise to nobody.
- **Live sessions end.** A rename ends the account's live sessions with a line
  naming the new username; they log in with it. Presence, chat and MRC state
  follow from there. The remote attestations this node issued about the
  account are revoked and reissued under the new name. An RLogin remote door
  keyed on the handle sees a new player, and the confirmation says so.
- **On the Link** the rename is announced in the node's endpoint descriptor
  (§4.4) and peers carry the user's state over to the new name (§12.4).

### 4.4 Human-facing Link addresses

**A renamed user's address** (issue #1111). A node lists its recent username
renames in its signed endpoint descriptor as `user_renames`: `{from, to, at}`
entries, at most 64, none older than 400 days, newest first. A reader
validates each entry and skips what does not validate; it never refuses a
hello or an introduction over the list. A peer keeps what it learns, keyed by
node fingerprint and old name, and does not re-read it, since entries age out
of the list. From then on `new@node` is the same user as `old@node`: chains
(`A -> B -> C`) resolve to the current name, a letter to the old address is
sent to the new one (a reply to an old letter is the usual case), and a mail
block recorded against either address covers both. A peer's views of the
user -- a trust screen, a block list -- show the current name with "formerly"
and the old one. Content signed before the rename keeps showing the name it
was signed under. A peer that has not heard from the node within 400 days of
the rename meets the new name as a new user.

The normal human-facing cross-node address is:

`user@friendly-name` or, where a friendly name is ambiguous,
`user@canonical-dns-name`

Link endpoint descriptors carry both claims inside the node-signed hello
bundle. User-facing screens show the friendly name, qualified by the canonical
DNS name where useful, and do not expose fingerprints by default. DNS names are
unique routing names, but neither a friendly name nor DNS is cryptographic
identity authority. Transport endpoints in the descriptor's `addresses` list
are connectivity data, not presentation claims; an older peer which omits
`canonical_dns_name` does not implicitly claim its endpoint host as a DNS name.

The underlying address and every protocol/persistence relationship remain
`user@node-fingerprint`. Durable linked content -- channel scrollback, mail,
carried board posts, fetched Link files -- therefore persists the fingerprint
and resolves the home node's *current* friendly identity when rendered, so a
benign rename is followed and nothing stored has to be rewritten. When that
technical author identity has an undismissed cryptographic-identity warning,
durable board and file attribution shows the warning and full fingerprint
alongside the friendly label; browsing remains available. A node with
no authenticated profile (one that has never been admitted, or is known only by
an administratively configured fingerprint) is shown by that fingerprint rather
than a shared placeholder. A full fingerprint is available as **Technical
identity** in the relevant SysOp detail view and remains accepted as an
advanced/backward-compatible input. Friendly-name resolution must be unique;
an ambiguous presentation name is refused, and the refusal spells out the
address to type for each candidate -- `user@<technical identity>`, with the
name that node goes by -- rather than repeating the already-ambiguous claim.
DNS and friendly claims share that one
namespace, so a reference matching one node's DNS claim and another node's
friendly claim is ambiguous rather than silently preferring either. Presentation
claims and abbreviated fingerprint input likewise resolve as one candidate set:
a name which equals another node's fingerprint prefix is ambiguous rather than
silently shadowing that technical address. Abbreviated fingerprint input counts
only from six characters, the length the node map shows (issue #807): below it
a one- or two-letter friendly name collided with every peer whose fingerprint
started with the same letters -- one peer in 32 for a single letter -- and
could never be used, while a deliberate six-character imitation of a prefix is
still ambiguous. Exact full fingerprints retain precedence. Fingerprints remain
hidden in the ordinary unique-name path.

What a caller reads after the `@` is what they can type back (issue #807). A
reference also matches a node's full display label (`Name · dns.example`; the
`·` is reserved, so a label never equals another node's name), and a node name
that contains `@` is shown in double quotes -- `bob@"Cats @ Night"` -- so a
reader can tell where the user name ends; one pair of enclosing quotes is
dropped from a typed reference, which is unambiguous because no friendly name,
DNS name or fingerprint may contain a double quote. A typed address splits at
its first `@`, since a user name cannot contain one and a node name can.

A chat line, and each line of a Link private conversation, names a linked
speaker's node by its friendly name alone (issue #899): the DNS name repeated
on every line cost more width than it told anyone. Where another node this BBS
knows of claims the same name -- as its friendly name or its DNS name, the one
namespace the identity warning uses -- or this BBS claims it now or did
recently, the name is qualified -- `Name · dns.example`, or `Name · abc123`, the first six characters
of the technical identity, when the node has no DNS name -- and the node map
disambiguates a shared name the same way. Both qualified forms resolve when
typed back. The speaker is styled in parts: the brackets and the `@` muted,
the user in the speaker color, the node in its own color (`NODE_COLOR`).

The user half of an address follows the local username grammar (ASCII letters,
digits, `_`, `-`, `.`, at most 32 characters), capitals included: a name is
addressed exactly as it is displayed, and the recipient node looks it up
case-insensitively, so `OldNib@Q` and `oldnib@Q` reach the same account. A
sender's name goes out as it is spelled; an account older than the username
rules whose name falls outside that grammar cannot send Link mail, since no
reply could reach it, and is told to ask for a rename. Friendly names are compared in one Unicode
normalization form (NFC), so canonically
equivalent spellings are one name, never two claims. UI delimiters, invisible
control/format characters, and the `Unnamed linked node` and `Unknown linked
node` fallback labels are reserved and cannot be claimed as friendly names. A
complete 32-character
base32 node fingerprint is reserved too: exact fingerprint lookup has precedence,
so allowing the same shape as a friendly name would make that claim unreachable
or misleading.

Peers retain authenticated observations of all three values. A friendly-name
change under the same fingerprint is an informational continuity notice. A DNS
change under the same fingerprint is a more prominent routing notice. Reuse of
a familiar friendly or DNS name by a different fingerprint is a strong
cryptographic-identity warning. A friendly name counts as familiar when it reads
as one -- compared by §4.2's skeleton as §6.3 extends it for aliases, the key they are
checked with, so "0utBound", "Out Bound" and "OutBоund" with a Cyrillic о are
all "OutBound" (issue #900) -- while a DNS name must match exactly: it is unique
by registration, and folding would equate different real hosts. The UI explains that recovery/replacement may
be legitimate but impersonation is possible, and it does not prevent the user
from continuing. Presentation names never transfer trust or reputation between
fingerprints.
An undismissed cryptographic-identity warning continues to be shown at
interaction boundaries even if a newer benign profile change is observed;
only SysOp acknowledgement dismisses it. Bounded observation pruning therefore
retains undismissed security warnings ahead of newer benign observations. The
file catalogue re-reads the selected origin's warning after the picker returns,
before asking for fetch consent. Trust-subject details show the same warning and
full technical identity; applying an override to a warned subject requires a
fresh, default-no confirmation of that fingerprint immediately before mutation.
Neither warning prevents continued interaction. The
local node likewise retains a bounded history of its previous friendly and DNS
claims so another fingerprint cannot adopt a just-renamed local identity without
raising the same warning. Reusing a retired claim moves it to the recent end of
that bounded history, so retention follows the latest use rather than the claim's
first retirement. The last friendly/DNS pair actually advertised in an
outbound hello remains part of that collision namespace until it has been moved
into history; an inbound hello racing the local rename therefore cannot claim
the just-replaced name in the gap before the next outbound descriptor is built.
Committing a changed local friendly or canonical-DNS claim also re-evaluates
every peer descriptor already on disk against the new claim, because a peer
which claimed that name first sends no further hello merely because this node
renamed; an identical already-recorded collision is not duplicated.
Startup primes the current local friendly/DNS pair before opening the Link
listener. Thereafter the shared own-hello cache is refreshed through the
background database lane before an inbound peer is persisted and once per
outbound sync pass. Each outbound hello repeats that refresh after its network
wait and immediately before persisting the authenticated peer; candidate
fallback uses the same path. A verified peer-list refresh likewise repeats it
after its network wait and immediately before persisting any updated known peer.
Every endpoint which accepts a signed hello, including
relay-mailbox pickup, performs that refresh before peer persistence; building
the signed response itself performs no synchronous
database I/O on the event loop. Authenticated live scrollback snapshots retain
their authors' home-node fingerprints in memory as well as their friendly
labels, so the same non-blocking identity warnings remain available even before
the corresponding durable event has arrived.

High-impact consent screens retain the same non-blocking model but disclose
security context before confirmation. Offering or accepting a board-origin
transfer involving a node with an undismissed cryptographic-identity warning
shows that node's full technical identity before the default-no prompt. If an
incoming offer's authenticated origin profile is unavailable, the confirmation
falls back to the signed origin fingerprint rather than an anonymous placeholder.
Likewise, a remote-file catalogue marks warned origins with their full technical
identity and repeats that caution before the fetch confirmation, before any bytes
are transferred.

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

An opt-in user may register one or more keypairs (issue #222 — multiple
simultaneous personal keys/devices, shipped) for passwordless login, each
independently valid; none is a root/operational hierarchy the way node
identity has. Author-identity purposes elsewhere in this design (Link
event authorship, display) that need exactly one fingerprint per account
use the account's *primary* key — the first one registered, or another
automatically promoted if the primary is later removed while other keys
remain.

**Promotion is a mechanical fallback, not an identity claim (code review
follow-up, PR #225).** There is no signed key-transition chain linking a
newly-promoted key to the one it replaced — unlike node identity, whose
root key signs each operational-key transition specifically so remote
peers can verify continuity across a rotation. A promoted personal key
is, from every remote Link peer's point of view, simply a different,
unrelated fingerprint: content authored after a promotion is *not*
cryptographically provable as continuing the same reputation history as
content authored before it, even though the local account is unchanged.
This is the same limitation the single-key model already accepted below
("losing the key loses that key-based identity and reputation
continuity"), now reachable as a side effect of removing one key among
several rather than only by losing your one and only key — a real,
disclosed gap, not a hidden one, and one this feature does not attempt
to close (a signed personal-key transition chain, mirroring node
identity's, is a real future option if reputation continuity across a
personal key change ever becomes a stated goal — not built now, not
implied by anything here).

There is no bespoke recovery mechanism for any individual key. Losing
every registered key loses key-based identity and reputation
continuity; the local account may still use ordinary account recovery
policy.

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

Replacing the root identity necessarily changes the fingerprint and is treated
as a new cryptographic identity. Keeping the same friendly or DNS name makes
that replacement recognizable to humans but does not establish continuity;
peers raise the strong warning above and continue to permit interaction.

Root and operational keys are generated at initial bootstrap. Rotation is a
guided SysOp action (issue #624): **Link status → Keys** on the running node,
or `python -m netbbs.admin rotate-key` on a stopped one. Either operational
key rotates on its own, and a rotation is one of two kinds, which the root
states in the revoke it signs:

- **Routine.** The old key is *retired*. What it signed while current stays
  valid, so the node's boards, posts, files and mail remain usable by peers
  that have not received them yet, including through carriers.
- **Compromise response.** The revoke carries `"compromised": true`. Peers stop
  believing anything the old key signed, and the node signs its own stored
  objects again under the new key. A key retired routinely can be declared
  compromised later by a second revoke that says so. A node that knows the
  rotating node only by introduction learns the revoke from whoever carries
  that node's content, beside the content (§8.11, issue #914). A copy a node
  already held when it learned the revoke, signed only by the compromised key,
  is held as stale from then on: not declared, not served, and replaced in
  place by the re-signed copy when a peer offers it (§8.11, issue #672).

What counts as signed "while current" is decided per object family. A
long-lived event is checked against the current key and then every key retired
without being called compromised. Anything signed fresh for one exchange (a
hello, a request, a withdrawal) is checked against the current key only. So is
anything the issuer re-issues on rotation (trust objects and attestations,
§12). §16's issue #624 entry has the rationale. Root-key custody is part of
ordinary node backup and restore rather than requiring an HSM or offline
ceremony.
### 4.6 Guest login (issue #531)

A node may designate one **existing account** as its guest identity. Typing
that account's name at the login prompt starts a session as that user without
a password prompt.

That is the whole feature, and the boundary is deliberate: guest login is an
*authentication* shortcut and never an authorization model. The guest is an
ordinary account, so levels, per-object permissions, age and name gates,
moderation, auditing and Link trust apply to it exactly as to any other
caller, and **no code branches on whether a caller is a guest** -- with the
exceptions below: mail, what a session that signed in without a credential
may change about the account it shares or keep after the call, and the
privileges the guest account may hold. A SysOp says what a guest may do the same way they say
it for anybody else: by setting the guest account's level, and by granting or
withholding per-object access.

Three consequences follow, and are intended rather than gaps:

- Writing is not blocked structurally. A guest meeting a board's write level
  may post; the level is the mechanism.
- The account keeps its password. The designation does not touch the
  credential, and turning guest access off -- one configuration change, leaving
  the account untouched -- restores ordinary sign-in for it.

  While guest access is *on*, though, typing that name on Telnet or web signs
  in as the guest: that is the feature, and there is deliberately no second
  path that offers the password prompt for it instead. The designated account
  is a node identity rather than a person's, so a SysOp who needs to act on it
  either turns guest access off for the moment or works on it from the SysOp
  console, which reaches everything about it including its keys. SSH is
  unaffected -- a key has already proven identity before the login flow runs.
- Everything after authentication still runs, and is re-checked at the moment
  of use rather than when the designation was saved. A blocked guest is
  refused. A disabled or not-yet-approved account is refused. An account
  promoted to SysOp after being designated is refused -- otherwise designating
  an ordinary account and then promoting it would be a passwordless route to
  SysOp. And a deleted guest stops being special, falling through to the
  ordinary password prompt.
- The designation records the account's **id and creation timestamp**, and both
  must match. Neither a name nor an id alone is an identity: a name resolves to
  whatever row holds it now, and an id, though never reused in normal running
  (§4.1), comes round again after a restore from an older backup. Either alone
  would hand passwordless access to a replacement account.
- **The guest account has no mail** (issue #816, §6.4). This is the one place
  the guest is treated as a guest, because a mailbox is not an area an account
  may or may not enter: it is the account's own correspondence. Every guest
  signs in as the same account, so its inbox would be read by strangers and
  anything sent from it would go out under one name many people type into.
  No level expresses that without also closing mail to every ordinary account
  at the guest's level. "The guest account" means the account guest login
  signs in without a password right now (`guest_is_eligible`); turning guest
  login off gives it its mail back.
- A guest session **may not manage the account's credentials.** The guest is an
  ordinary account in every other respect, but whether a session may touch an
  SSH key is a question about how that session authenticated, not about the
  account. The rule covers the whole key screen, not adding alone: a caller who
  proved nothing must not be able to mint a credential that outlives guest
  access being switched off, nor to strip the keys off a password-backed
  account -- removing the primary key changes the fingerprint its Link events
  are authored under. Signing in with the account's own password reaches key
  management normally.
- A guest session **may not change what other callers see of the account**
  (issue #1073). Every anonymous caller shares it, so whatever one guest
  writes there every later guest and every other caller gets: its bio and
  bio visibility, signature, display name, location, birthdate and their
  visibility switches, the verified-badge and Link-sharing switches, whether
  it takes direct messages, read receipts, whether its name is shown on
  Previous callers, who it blocks (from Profile or from Who's online), its
  MRC settings that the node-wide bridge reads per handle (private messages,
  last-seen, nick color), its chat alias (`/nick`, which is announced and kept
  in scrollback) and its MRC hub registration (`/mrc register`, `identify`,
  `roompass`, `update password`, and raw `/mrc send`, whose free text could
  carry any of them). Like the key screen, this is a check on how
  the session got in (`authenticated_without_credential`), not on the
  account: the entries stay where they are and say why when pressed, and the
  account signed in with its own password changes all of it as before.
  Birthdate matters beyond defacement: a self-entered birthdate is what local
  `min_age` gates check when the account has no age attestation (§18), so a
  guest who could set one would have opened every age gate for every later
  guest.
- A guest session's **display settings last for the call** (issue #1073). A
  guest on a plain-ASCII or 16-colour terminal needs a character set, colour
  depth, redraw style, banner speed, editor, colour toggles, sort orders and
  so on as much as anyone, so these stay open; but one guest's choice must not
  become the next guest's. For such a session every write to the per-user
  preference store and to the sort-order store stays in memory
  (`netbbs.user_preferences.session_scoped_preferences`, entered when the
  signed-in session starts) and its reads see its own choices over the
  account's stored values. The stored values are what every guest starts
  from; a SysOp sets them by signing in as the account with guest login off.
- A guest session's **drafts are its own and last for the call** (issue
  #1075). Post and file-description drafts are files named after the account,
  so on the shared account one guest's unfinished post was offered to the
  next, and two guests at once wrote the same file. A guest call keeps its
  drafts in a directory of its own (`netbbs.guest_call`, entered with the
  preference overlay), deleted when the call ends. Kept as files rather than
  in memory because that is what both editors write: autosave, `/exit` and a
  refused save's kept draft all work as for anyone else, within the call.
- A guest session **may edit or withdraw only what it wrote during the call**
  (issue #1075). Every guest's posts and uploads carry the one account as
  author, so "your own post" meant every guest's: any guest could rewrite or
  withdraw an earlier guest's words, and the edit was carried over the Link
  as the author's. The session records the posts (by root post id) and
  uploads (Zmodem or a web link) it creates; editing, withdrawing or
  describing anything else the account wrote is refused before an editor
  opens, with the reason on screen. Nothing else lets an author change their
  own content -- deleting a post or file is a moderator's, and mail is closed
  to the guest.
- A guest session **plays the bundled doors without a save that outlives the
  call** (issue #1075). Voidrunner and War Dialer key their saves on the drop
  file's `user_id`, so every guest shared one career and one crew, and the
  callsign a guest typed showed in Voidrunner's public Hall of Fame. A guest
  call plays a bundled door under a `user_id` of its own (above any rowid, so
  never an account's) with `VOIDRUNNER_SAVE_DIR` and `WAR_DIALER_DB_PATH` in
  the call's directory: Voidrunner starts from a copy of the node's Hall of
  Fame records and War Dialer from a copy of the world (SQLite's backup API),
  so the guest sees the real standings and rivals, and nothing it does
  reaches them. Lunar Lander (issue #1542) is handled as War Dialer is:
  `DOOR_LL_DATA_DIR` names a directory in the call holding a backup-API
  copy of the node's `pilots.db`. The door's outbound hook answers it as a rehearsal. The
  call's save stays for the rest of the call, so a guest can leave a game and
  come back. A SysOp's own doors keep receiving the guest account: what an
  external game stores is its own, a fresh identity every call would fill a
  legacy door's player list, and whether a guest reaches the door at all is
  its level.
- **The guest account holds no privilege a SysOp grants a person** (issue
  #1075): staff permissions, identity verification, or a moderator grant
  (edit, delete or approve on a board or file area; any channel permission).
  Each would belong to every anonymous caller at once. Granting one to the
  designated account is refused where it is written, with the reason; an
  account holding one cannot be designated; and `guest_is_eligible` refuses
  it at sign-in, for a grant made before either check existed. Read and post
  grants are not privileges in this sense: they open an area to the guest the
  way its level does, which is the mechanism this section describes.
- The re-checks are applied to the row that is **ultimately returned**, not
  only to the one first resolved. The login path awaits transport I/O and then
  re-reads the account to stamp `last_login_at`; a promotion, a block or a
  deletion landing in that window would otherwise have been read too early to
  matter. A deletion is a refusal like any other, not an error.

A SysOp account may not be designated. Everything else about guest access is
policy the SysOp chooses, but a passwordless SysOp login is not a choice worth
offering.

The **pre-login notice** is a short SysOp-authored line shown above the
sign-in screen -- the place a caller learns the guest account exists at all.
Unlike the welcome banner, which is authored ANSI art placed on the node's
filesystem and deliberately neither sanitized nor wrapped, the notice is typed
in the BBS and goes through the ordinary text path. It reaches Telnet and web
callers only: SSH has proven identity before there is any pre-login moment to
use.
